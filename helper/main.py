"""DocuClickAI helper (módulo principal).

Procesa el PDF NCR en dos pasadas (Transfer In / Transfer Out) usando
Ollama (phi4-mini por defecto). Valida estructura mínima del JSON antes
de guardarlo.

Se recomienda usar el entry point ejecutable ``./docuclickai`` que además
gestiona ``tmp/`` con limpieza perezosa y nombres largos.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ollama_client import (
    DEFAULT_MODEL, DEFAULT_HOST, DEFAULT_TIMEOUT, chat_with_retry,
    parse_json, OllamaError, is_truncated_json,
)
from pdf_extractor import extract_text, parse_header, split_sections, split_stores
from events import EventPublisher, RedisPublishError


# ---------------------------------------------------------------------------
# Prompts (compactos, una sola tienda por llamada)
# ---------------------------------------------------------------------------

STORE_PROMPT = """Eres un extractor JSON de transferencias NCR (Wendy's, Panamá).
Devuelve SOLO JSON válido (sin markdown, sin texto extra).

El texto corresponde a UNA SOLA tienda (cabecera ya eliminada): "{tienda}".
Cada transferencia tiene esta estructura:
  "Transfer: <ID> - <DD/MM/YYYY HH:MM:SS>"   ← fecha/hora sistema
  "Transfer Date: <DD/MM/YYYY>"              ← fecha contable
  Filas: description | category | quantity_transferred | unit_transferred | cost_unit | extension
  "Transfer Total: B/.XXX.XX"                ← cierre (suma)

ESQUEMA:
{{
  "tienda": "{tienda}",
  "transferencias": [
    {{
      "transfer_id": "<id entero como string>",
      "transfer_datetime": "DD/MM/YYYY HH:MM:SS",
      "transfer_date": "DD/MM/YYYY",
      "items": [
        {{
          "description": "...",
          "category": "...",
          "quantity_transferred": <float>,
          "unit_transferred": "...",
          "cost_unit": <float>,
          "extension": <float>
        }}
      ],
      "transfer_total": <float>
    }}
  ],
  "subtotal_tienda": <float, suma de transfer_total>
}}

REGLAS:
1. EXTRACCIÓN COMPLETA: copia TODAS las transferencias que aparezcan en el texto, con TODOS sus items. NO omitas ninguno.
2. NÚMEROS EXACTOS: copia los valores monetarios EXACTAMENTE como aparecen tras quitar "B/." y comas de miles. Ejemplos:
   - "B/.56.22"     → 56.22
   - "B/.1,036.70"  → 1036.70
   - "B/.7.90"      → 7.90
   NO dividas ni redondees.
3. FECHAS: conserva DD/MM/YYYY tal cual vienen del PDF. El campo "Transfer: <ID> - DD/MM/YYYY HH:MM:SS" tiene la fecha real de la transferencia en formato DD/MM/YYYY; el campo "Transfer Date: DD/MM/YYYY" puede tener la fecha contable y en PDFs multipágina a veces está pegado al item siguiente — IGNORA el Transfer Date y deriva transfer_date SIEMPRE desde transfer_datetime (los primeros 10 chars después de " - ").
4. CATEGORÍAS válidas: Alimentos, Papeleria, Operaciones, Limpieza.
5. CAMPOS NUMÉRICOS son SIEMPRE float. Si faltan, usa null (NO 0).
6. **NO INVENTES ITEMS**: una línea que empieza con "(" o que es solo un número entre paréntesis (ej. "(720un)", "(2/20)", "(CA=24/50)", "(864un)") es la coletilla de la línea PREVIA, NO un producto. NO la conviertas en item. Lo mismo aplica a líneas como "8/30un (240un)" que aparecen sueltas: NO son items, son continuación de la descripción anterior.
7. **NO TRUNCAR unit_transferred**: cuando el PDF muestre "Bulto = 20 LB" o "CA=8/30 UN", copia la unidad COMPLETA tal cual. NO te quedes solo con la primera palabra ("Bulto", "CA", "PQ").
8. **cost_unit es el PRECIO, no el empaque**: "B/.46.65" para 1944 packets significa cost_unit=0.02, NO 1944. El número grande (1944, 640, 500, 40) que aparece en unit_transferred es la cantidad por empaque, no el precio.
9. subtotal_tienda: SOLO si ves la línea "Transfer Total: B/.XXX.XX" inmediatamente después de las filas de items de una transferencia (cierre del bloque). NO confundas "Transfer In Total" o "Transfer Out Total" (que aparecen UNA vez al final de toda la sección, no al final de cada tienda) con subtotal_tienda.
10. Si no estás seguro del subtotal_tienda, déjalo como null — lo recalcularemos.
11. Devuelve ÚNICAMENTE el objeto raíz con la clave "transferencias" como array.
"""


def build_store_prompt(tienda: str, store_text: str) -> str:
    return (
        f'{STORE_PROMPT.format(tienda=tienda)}\n\n'
        f'Texto de la tienda "{tienda}":\n'
        f'---------------------------\n'
        f'{store_text}\n'
        f'---------------------------\n'
        f'JSON:'
    )


# ---------------------------------------------------------------------------
# Validación mínima de estructura
# ---------------------------------------------------------------------------

REQUIRED_TOP_KEYS = {"origen", "anio_fiscal", "periodo", "rango_fechas",
                     "totales", "transfer_in", "transfer_out"}


class SchemaError(ValueError):
    """El JSON devuelto por Ollama no cumple la estructura mínima."""


def validate_structure(data: dict) -> None:
    missing = REQUIRED_TOP_KEYS - set(data.keys())
    if missing:
        raise SchemaError(f"Faltan claves top-level: {sorted(missing)}")
    if not isinstance(data["transfer_in"], list):
        raise SchemaError("transfer_in debe ser array")
    if not isinstance(data["transfer_out"], list):
        raise SchemaError("transfer_out debe ser array")
    if not isinstance(data["totales"], dict):
        raise SchemaError("totales debe ser objeto")


def _coerce_store_response(parsed) -> dict:
    """Acepta tanto el esquema esperado como envoltorios del modelo."""
    if isinstance(parsed, list):
        # El modelo devolvió solo el array, no el wrapper
        return {"transferencias": parsed, "subtotal_tienda": None}
    if isinstance(parsed, dict):
        transferencias = parsed.get("transferencias", [])
        if not isinstance(transferencias, list):
            # Otros wrappers comunes
            for k in ("transfers", "results", "data", "items"):
                v = parsed.get(k)
                if isinstance(v, list):
                    transferencias = v
                    break
            else:
                transferencias = []
        return {
            "tienda": parsed.get("tienda"),
            "transferencias": transferencias,
            "subtotal_tienda": parsed.get("subtotal_tienda"),
        }
    return {"transferencias": [], "subtotal_tienda": None}


# ---------------------------------------------------------------------------
# Saneamiento post-Ollama: derivar transfer_date, validar items, podar filas
# fantasma (description que es solo paréntesis o número suelto).
# ---------------------------------------------------------------------------

_DT_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2}):(\d{2})$")
# Líneas que NO son descripciones válidas: paréntesis solos, "8/30un", etc.
_GHOST_DESC_RE = re.compile(
    r"^\s*(\([^\)]*\)\s*)+$"           # solo "(...)" o "(...) (...)"
    r"|^\s*\d+/\d+\s*[a-z]+\s*(\([^\)]*\))?\s*$"  # "8/30un (240un)"
    r"|^\s*\d+un\s*$"                  # "720un"
    r"|^\s*\d+\s*$",                   # "2"
    re.IGNORECASE,
)


def _derive_date_from_datetime(transfer: dict) -> dict:
    """Deriva ``transfer_date`` desde ``transfer_datetime`` (formato DD/MM/YYYY).

    El PDF trae la fecha real en ``Transfer: ID - DD/MM/YYYY HH:MM:SS``. La
    línea ``Transfer Date:`` puede estar desfasada o aparecer pegada al item
    siguiente en páginas múltiples, así que se IGNORA y se recalcula.
    Devuelve el dict (no muta in-place para ser puro).
    """
    dt = transfer.get("transfer_datetime")
    if not isinstance(dt, str):
        return transfer
    m = _DT_RE.match(dt.strip())
    if not m:
        return transfer
    dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
    derived = f"{dd}/{mm}/{yyyy}"
    out = dict(transfer)
    out["transfer_date"] = derived
    return out


def _is_ghost_item(item: dict) -> bool:
    """Heurística de item inventado: descripción vacía, solo paréntesis, o
    coletilla pegada (ej. ``"8/30un (240un)"``, ``"(720un)"``)."""
    if not isinstance(item, dict):
        return True
    desc = item.get("description")
    if not isinstance(desc, str):
        return True
    desc_s = desc.strip()
    if not desc_s:
        return True
    if _GHOST_DESC_RE.match(desc_s):
        return True
    # Si tiene descripción pero TODO lo demás es null, es una fila fantasma
    other = [item.get(k) for k in
             ("category", "quantity_transferred", "unit_transferred",
              "cost_unit", "extension")]
    if all(v is None for v in other):
        return True
    return False


def _item_quantity_cost_extension_ok(item: dict, *, tol: float = 0.02) -> bool:
    """Valida que ``quantity_transferred * cost_unit ≈ extension``.

    Devuelve True si la tupla es consistente, False si no cuadra. Si falta
    algún campo, devuelve True (no se puede validar).
    """
    if not isinstance(item, dict):
        return False
    q = item.get("quantity_transferred")
    c = item.get("cost_unit")
    e = item.get("extension")
    if not all(isinstance(v, (int, float)) for v in (q, c, e)):
        return True   # falta data, no se puede validar
    if q == 0 or c == 0:
        return False
    expected = float(q) * float(c)
    return abs(expected - float(e)) <= tol


def _sanitize_transfers(transfers: list, *, log_items: list | None = None) -> list:
    """Aplica las correcciones a la lista de transferencias:

    1. Poda filas fantasma (``_is_ghost_item``).
    2. Recalcula ``transfer_date`` desde ``transfer_datetime``.
    3. Marca items donde ``qty*cost != extension`` en ``_validation_warning``
       (sin eliminarlos: el LLM pudo haber acertado en parte).

    Devuelve una lista nueva.
    """
    out: list = []
    for tr in transfers or []:
        if not isinstance(tr, dict):
            continue
        tr2 = _derive_date_from_datetime(tr)
        items = tr2.get("items") or []
        clean_items: list = []
        bad_count = 0
        for it in items:
            if _is_ghost_item(it):
                continue
            it2 = dict(it)
            if not _item_quantity_cost_extension_ok(it):
                it2["_validation_warning"] = "qty*cost != extension"
                bad_count += 1
            clean_items.append(it2)
        tr2["items"] = clean_items
        if bad_count:
            tr2["_items_with_warning"] = bad_count
        out.append(tr2)
    return out


def _process_one_store(
    tienda: str,
    store_text: str,
    *,
    model: str,
    host: str,
    timeout: int,
    max_retries: int,
    log,
) -> dict:
    """Llama a Ollama para una sola tienda y devuelve el dict normalizado.

    Estrategia de sub-batches: si el texto tiene 2+ líneas ``Transfer:``,
    subdivide el texto por transferencia y llama a Ollama una vez por
    bloque. Esto le da al LLM chunks pequeños y uniformes, lo que reduce
    el riesgo de que omita o fusione transacciones grandes.
    """
    bloques = _split_store_into_transfers(store_text)
    if not bloques:
        # Fallback al comportamiento de bloque único (texto sin marcadores Transfer)
        transferencias = _process_store_block(
            tienda, store_text, transfer_meta=None,
            model=model, host=host, timeout=timeout,
            max_retries=max_retries, log=log,
        ) or []
        return _wrap_store(tienda, transferencias)

    transferencias: list = []
    for meta, bloque_texto in bloques:
        trs = _process_store_block(
            tienda, bloque_texto, transfer_meta=meta,
            model=model, host=host, timeout=timeout,
            max_retries=max_retries, log=log,
        )
        if trs:
            transferencias.extend(trs)

    # Validación de completitud: si Ollama devolvió menos transferencias que
    # las que detectamos en el texto crudo, intentar recuperar las faltantes
    # con reintento individual.
    if len(transferencias) < len(bloques):
        missing = _find_missing_transfers(bloques, transferencias)
        if missing:
            log(f"ollama_{tienda.replace(' ', '_')}_completeness",
                f"faltan {len(missing)} transfers: {missing}")
            for meta, bloque_texto in missing:
                trs = _process_store_block(
                    tienda, bloque_texto, transfer_meta=meta,
                    model=model, host=host, timeout=timeout,
                    max_retries=max_retries, log=log,
                    tag="retry_missing",
                )
                if trs:
                    transferencias.extend(trs)
            # Ordenar por datetime para que el output sea estable
            transferencias.sort(key=lambda t: t.get("transfer_datetime", ""))

    return _wrap_store(tienda, transferencias)


def _wrap_store(tienda: str, transferencias: list) -> dict:
    """Construye el wrapper final de una tienda con subtotal_tienda validado.

    El subtotal se recalcula SIEMPRE desde la suma de ``transfer_total`` de
    las transferencias (Fix #3). Si el modelo también devolvió un
    ``subtotal_tienda`` y difiere significativamente del recalculado, se
    prefiere el recalculado y se marca con ``_subtotal_warning``.
    """
    # Recalcular desde la suma real
    s = 0.0
    for tr in transferencias:
        v = tr.get("transfer_total")
        if isinstance(v, (int, float)):
            s += float(v)
    computed = round(s, 2) if s > 0 else None

    # Si el modelo devolvió un subtotal absurdo (negativo grande, NaN-like),
    # siempre gana el recalculado (Fix #3)
    coerced: dict = {
        "tienda": tienda,
        "transferencias": transferencias,
        "subtotal_tienda": computed,
    }
    return coerced


# Regex para detectar el inicio de cada transferencia individual.
_TRANSFER_HEADER_RE = re.compile(
    r"Transfer:\s+(\d+)\s+-\s+(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})",
)


def _split_store_into_transfers(store_text: str) -> list[tuple[dict, str]]:
    """Divide el texto de una tienda en bloques por transferencia.

    Devuelve ``[(meta, texto_bloque), ...]`` en orden de aparición, donde
    ``meta`` es ``{"transfer_id": "...", "transfer_datetime": "..."}``.
    Si el texto no contiene ningún marcador ``Transfer:`` devuelve ``[]``
    (el caller hará fallback a bloque único).
    """
    if not store_text:
        return []
    matches = list(_TRANSFER_HEADER_RE.finditer(store_text))
    if not matches:
        return []
    bloques: list[tuple[dict, str]] = []
    for i, m in enumerate(matches):
        meta = {
            "transfer_id": m.group(1),
            "transfer_datetime": m.group(2),
        }
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(store_text)
        texto = store_text[start:end].strip()
        bloques.append((meta, texto))
    return bloques


def _find_missing_transfers(
    bloques: list[tuple[dict, str]],
    transferencias: list[dict],
) -> list[tuple[dict, str]]:
    """Devuelve los bloques que NO están representados en ``transferencias``.

    Coincidencia por ``(transfer_id, transfer_datetime)``.
    """
    have = {(t.get("transfer_id"), t.get("transfer_datetime"))
            for t in transferencias if isinstance(t, dict)}
    return [b for b in bloques
            if (b[0]["transfer_id"], b[0]["transfer_datetime"]) not in have]


def _items_match_transfer_total(
    transfer: dict, *, tol: float = 0.05,
) -> bool:
    """True si ``abs(transfer_total - sum(extension)) <= tol``.

    Si el transfer no tiene items, devuelve True (no se puede validar).
    """
    if not isinstance(transfer, dict):
        return False
    t = transfer.get("transfer_total")
    if not isinstance(t, (int, float)):
        return False
    items = transfer.get("items") or []
    if not items:
        return True
    s = 0.0
    for it in items:
        if not isinstance(it, dict):
            continue
        e = it.get("extension")
        if isinstance(e, (int, float)):
            s += float(e)
    return abs(float(t) - s) <= tol


def _infer_cost_unit(item: dict) -> bool:
    """Si ``cost_unit`` parece estar mal interpretado por el LLM, lo
    recalcula y marca el item con un ``_validation_warning`` específico.

    Heurísticas (cualquiera dispara corrección):

    1. ``cost_unit * quantity > extension * 2`` → ``cost_unit`` es el
       número de empaque, no el precio. Recalcula como ``ext / qty``.
       (Fix original, casos Metromall 1944/640/500.)

    2. ``cost_unit`` es entero y ``< 0.5 * (ext / qty)`` → mismo bug que
       (1) pero con precio "redondo" (ej. cost=20 cuando real=35.45).

    3. ``ext / (qty * cost) > 5`` y el swap cost↔ext hace que la
       multiplicación cuadre → el LLM invirtió las dos columnas.
       Caso típico: PDF trae ``cost=11.68, ext=2.34`` y el LLM devuelve
       ``cost=2.34, ext=11.68`` (Galleta Oreo, Brisas del Golf 07/13).

    4. ``cost_unit < 1`` y ``unit_transferred`` contiene un entero N:
       4a) ``qty == N`` → el LLM puso el tamaño de empaque como
           cantidad. Recalcula: ``qty=1, cost=ext``. Caso típico:
           Mozzarella Sticks (qty=384, cost=0.23, unit="CA=384 UN").
       4b) ``qty = k * N`` con ``1 <= k <= 20`` → el LLM multiplicó la
           cantidad real por el tamaño de empaque. Recalcula:
           ``qty=k, cost=ext/k``. Caso típico: Nescafe Vasos 8oz
           (qty=100, cost=0.11, unit="PQ= 50 UN", real qty=2).

    Nota sobre (4): el umbral ``new_c > 5.0`` evita falsos positivos en
    packs baratos (ej. PQ=10 UN a $1.00) donde el LLM podría haber
    acertado. Si un pack legítimo cuesta entre $0.01 y $5.00 con
    ``unit_transferred`` con tamaño de empaque, el item quedará con
    ``_validation_warning: "qty*cost != extension"`` para revisión
    manual.

    Devuelve True si se modificó el item.
    """
    if not isinstance(item, dict):
        return False
    q = item.get("quantity_transferred")
    c = item.get("cost_unit")
    e = item.get("extension")
    if not all(isinstance(v, (int, float)) for v in (q, c, e)):
        return False
    if q == 0 or c == 0 or e == 0:
        return False

    qf, cf, ef = float(q), float(c), float(e)

    # Heurística 1: cost_unit * qty >> extension (caso empaque como precio)
    if cf >= 1 and cf * qf > ef * 2.0:
        item["cost_unit"] = round(ef / qf, 4)
        item["_validation_warning"] = "cost_unit_inferred"
        return True

    # Heurística 2: cost_unit entero, mucho menor que precio real
    if cf >= 1 and c == int(c):
        real_unit = ef / qf
        if real_unit > 0 and cf < real_unit * 0.5:
            item["cost_unit"] = round(real_unit, 4)
            item["_validation_warning"] = "cost_unit_inferred"
            return True

    # Heurística 3: cost_unit ↔ extension invertidos por el LLM
    # Señal: ext / (qty * cost) > 5 (ext es ~5x mayor de lo esperado).
    # Validación: si hacemos swap, ¿qty * new_cost ≈ new_ext?
    if cf >= 1 and qf > 0 and (qf * cf) > 0 and ef / (qf * cf) > 5.0:
        new_c = ef
        new_e = cf
        if abs(qf * new_c - new_e) < 0.05:
            item["cost_unit"] = round(new_c, 4)
            item["extension"] = round(new_e, 4)
            item["_validation_warning"] = "cost_extension_swapped"
            return True

    # Heurística 4: quantity_transferred confundido con tamaño de empaque
    if cf < 1 and cf > 0:
        unit = item.get("unit_transferred")
        if isinstance(unit, str) and unit:
            for n_str in re.findall(r"\d+", unit):
                n = int(n_str)
                if n <= 1:
                    continue
                # 4a) qty == N → real qty=1, real cost=ext
                if n == int(q):
                    new_c = ef
                    if new_c > 5.0:  # pack price mínimo para considerarlo real
                        item["quantity_transferred"] = 1.0
                        item["cost_unit"] = round(new_c, 4)
                        item["_validation_warning"] = "qty_was_package_size"
                        return True
                # 4b) qty = k * N → real qty=k, real cost=ext/k
                if int(q) > 0 and int(q) % n == 0:
                    real_qty = int(q) // n
                    if 1 <= real_qty <= 20:
                        new_c = ef / real_qty
                        if new_c > 5.0 and abs(real_qty * new_c - ef) < 0.05:
                            item["quantity_transferred"] = float(real_qty)
                            item["cost_unit"] = round(new_c, 4)
                            item["_validation_warning"] = "qty_was_qty_times_package"
                            return True

    # Heurística 5: cost_unit correcto pero extension truncado
    # Patron: cost_unit * qty >> extension (más de 5x) y el quotient
    # redondeado a 1 decimal parece un entero de empaque "limpio".
    # Caso 1961-Bag-5# Thank You: cost=0.03, qty=4, ext=0.3, real ext=3.0.
    # Señal: (qty * cost) / ext = 12/0.3 = 40 (razón sospechosa).
    if qf > 0 and ef > 0 and cf > 0 and qf * cf > ef * 5.0:
        ratio = (qf * cf) / ef
        if 5.0 <= ratio <= 1000.0:
            # ratio casi entero sugiere que ext se dividió por un entero
            r_round = round(ratio)
            if r_round >= 2 and abs(ratio - r_round) < 0.1:
                new_ext = round(ef * r_round, 4)
                # Validar que tiene sentido: new_ext debe ser similar a qty*cost
                if abs(new_ext - qf * cf) < 0.05:
                    item["extension"] = new_ext
                    item["_validation_warning"] = "extension_was_divided"
                    return True

    return False


def _process_store_block(
    tienda: str,
    bloque_texto: str,
    *,
    transfer_meta: dict | None,
    model: str,
    host: str,
    timeout: int,
    max_retries: int,
    log,
    tag: str = "raw",
) -> list | None:
    """Procesa UN bloque de texto (toda la tienda o un solo transfer).

    Si ``transfer_meta`` viene con id+datetime, los fija en el resultado
    (Ollama a veces omite transfer_datetime cuando el bloque es muy
    pequeño). Devuelve la LISTA de transferencias del bloque (sin envolver
    en ``{tienda, transferencias, subtotal_tienda}``; eso lo hace
    ``_process_one_store`` al final). Devuelve ``None`` si Ollama devolvió
    respuesta vacía o no parseable.
    """
    raw = chat_with_retry(
        build_store_prompt(tienda, bloque_texto),
        model=model, host=host, timeout=timeout, max_retries=max_retries,
    )
    slug = tienda.replace(' ', '_')
    if tag == "raw":
        log(f"ollama_{slug}_raw", raw)
    else:
        log(f"ollama_{slug}_{tag}", raw)
    if is_truncated_json(raw):
        log(f"ollama_{slug}_truncated_flag_{tag}", "TRUNCATED")
        if tag != "raw":
            # En reintento de completitud, no elevar: aceptar vacío
            return None
        raise SchemaError(f"Tienda {tienda}: respuesta JSON truncada incluso tras reintento")
    try:
        parsed = parse_json(raw)
    except Exception:  # noqa: BLE001
        log(f"ollama_{slug}_parse_error_{tag}", "PARSE_ERROR")
        return None
    coerced = _coerce_store_response(parsed)
    coerced["tienda"] = tienda

    # Saneamiento: podar fantasmas, derivar transfer_date, marcar warnings
    transferencias = _sanitize_transfers(
        coerced.get("transferencias") or [])

    # Punto 4: corrección defensiva de cost_unit
    for tr in transferencias:
        for it in tr.get("items") or []:
            _infer_cost_unit(it)

    # Si transfer_meta viene, fijar id y datetime (el LLM chiquito a veces los omite)
    if transfer_meta:
        for tr in transferencias:
            if not tr.get("transfer_id"):
                tr["transfer_id"] = transfer_meta["transfer_id"]
            if not tr.get("transfer_datetime"):
                tr["transfer_datetime"] = transfer_meta["transfer_datetime"]

    # Fix #4: si transfer_total falta o es null, calcular desde sum(extension)
    # y marcar con warning para auditoría.
    for tr in transferencias:
        total = tr.get("transfer_total")
        items = tr.get("items") or []
        items_sum = sum(
            float(it.get("extension"))
            for it in items
            if isinstance(it, dict) and isinstance(it.get("extension"), (int, float))
        )
        if total is None and items_sum > 0:
            tr["transfer_total"] = round(items_sum, 2)
            tr["_transfer_total_inferred"] = "from_sum_of_extension"
            log(f"ollama_{slug}_total_inferred_{tag}",
                f"transfer {tr.get('transfer_datetime')}: "
                f"total was None → inferred {tr['transfer_total']}")
            continue
        # Punto 3: validar transfer_total vs Σ extension. Si no cuadra,
        # marcar warning (NO reintentar aquí — eso lo hace _process_one_store).
        if not _items_match_transfer_total(tr):
            tr["_validation_warning_total"] = "transfer_total != sum(extension)"
            log(f"ollama_{slug}_total_mismatch_{tag}",
                f"transfer {tr.get('transfer_datetime')}: "
                f"total={tr.get('transfer_total')} sum={items_sum}")

    # Si el bloque devolvió 0 transfers y transfer_meta viene, crear uno mínimo
    if not transferencias and transfer_meta:
        transferencias = [{
            "transfer_id": transfer_meta["transfer_id"],
            "transfer_datetime": transfer_meta["transfer_datetime"],
            "transfer_date": transfer_meta["transfer_datetime"][:10],
            "items": [],
            "transfer_total": None,
            "_empty_response": True,
        }]

    return transferencias


# ---------------------------------------------------------------------------
# Flujo principal
# ---------------------------------------------------------------------------

def run(
    pdf_path: Path,
    out_path: Path,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = 2,
    log=None,  # callable opcional para artefactos en tmp/
    publisher: EventPublisher | None = None,
) -> dict:
    log = log or (lambda *_a, **_k: None)

    pdf_name = Path(pdf_path).name
    pub = publisher  # puede ser None → no se publica nada

    print(f"[1/4] Extrayendo texto de {pdf_path}...", flush=True)
    full_text = extract_text(pdf_path)
    header_meta = parse_header(full_text)
    sections = split_sections(full_text)
    stores_in = split_stores(sections["transfer_in"])
    stores_out = split_stores(sections["transfer_out"])
    log("pdf_full", full_text)
    log("pdf_header", sections["header"])
    log("pdf_transfer_in", sections["transfer_in"])
    log("pdf_transfer_out", sections["transfer_out"])

    print(f"[2/4] Procesando {len(stores_in)} tiendas Transfer In con Ollama ({model})...", flush=True)
    in_list = []
    for s in stores_in:
        print(f"   · {s['tienda']}", flush=True)
        store = _process_one_store(
            s["tienda"], s["texto"],
            model=model, host=host, timeout=timeout, max_retries=max_retries,
            log=log,
        )
        in_list.append(store)
        # Emitir evento por tienda en cuanto Ollama termina con ella
        if pub is not None:
            n = pub.publish_store(
                archivo=pdf_name,
                origen=header_meta["origen"],
                anio_fiscal=header_meta["anio_fiscal"],
                periodo=header_meta["periodo"],
                tipo="transfer_in",
                store_data=store,
            )
            print(f"      → pdf.processed (transfer_in) emitido a {n} suscriptor(es)", flush=True)

    print(f"[3/4] Procesando {len(stores_out)} tiendas Transfer Out con Ollama ({model})...", flush=True)
    out_list = []
    for s in stores_out:
        print(f"   · {s['tienda']}", flush=True)
        store = _process_one_store(
            s["tienda"], s["texto"],
            model=model, host=host, timeout=timeout, max_retries=max_retries,
            log=log,
        )
        out_list.append(store)
        if pub is not None:
            n = pub.publish_store(
                archivo=pdf_name,
                origen=header_meta["origen"],
                anio_fiscal=header_meta["anio_fiscal"],
                periodo=header_meta["periodo"],
                tipo="transfer_out",
                store_data=store,
            )
            print(f"      → pdf.processed (transfer_out) emitido a {n} suscriptor(es)", flush=True)

    print("[4/4] Componiendo y validando JSON final...", flush=True)
    data = {
        "origen": header_meta["origen"],
        "anio_fiscal": header_meta["anio_fiscal"],
        "periodo": header_meta["periodo"],
        "rango_fechas": header_meta["rango_fechas"],
        "totales": {
            "transfer_in_total": _sum_totals(in_list),
            "transfer_out_total": _sum_totals(out_list),
        },
        "transfer_in": in_list,
        "transfer_out": out_list,
    }
    validate_structure(data)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    n_in = sum(len(t["transferencias"]) for t in in_list if isinstance(t, dict))
    n_out = sum(len(t["transferencias"]) for t in out_list if isinstance(t, dict))

    # Evento resumen al terminar el PDF completo
    if pub is not None:
        pub.publish_summary(
            archivo=pdf_name,
            origen=header_meta["origen"],
            anio_fiscal=header_meta["anio_fiscal"],
            periodo=header_meta["periodo"],
            totales=data["totales"],
            tiendas_in=len(in_list),
            tiendas_out=len(out_list),
            transferencias_in=n_in,
            transferencias_out=n_out,
        )
        print(f"      → pdf.processed.summary emitido", flush=True)

    print(f"Listo. IN: {len(in_list)} tiendas / {n_in} transferencias · "
          f"OUT: {len(out_list)} tiendas / {n_out} transferencias -> {out_path}")
    return data


def _sum_totals(stores: list) -> float:
    total = 0.0
    for s in stores:
        if not isinstance(s, dict):
            continue
        if isinstance(s.get("subtotal_tienda"), (int, float)):
            total += float(s["subtotal_tienda"])
            continue
        for t in s.get("transferencias", []) or []:
            v = t.get("transfer_total") if isinstance(t, dict) else None
            if isinstance(v, (int, float)):
                total += float(v)
    return round(total, 2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DocuClickAI helper (Ollama).")
    parser.add_argument("--pdf", required=True, type=Path, help="Ruta al PDF de transferencias")
    parser.add_argument("--out", type=Path, default=None, help=f"Ruta del JSON de salida (default: {DEFAULT_OUT})")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Modelo Ollama (default: {DEFAULT_MODEL})")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Host Ollama (default: {DEFAULT_HOST})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"Timeout por sección en segundos (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--max-retries", type=int, default=2, help="Reintentos si el JSON viene truncado (default: 2)")
    # --- eventos Redis ---
    parser.add_argument("--no-publish", action="store_true",
                        help="No publicar eventos en Redis (solo guarda el JSON)")
    parser.add_argument("--redis-host", default=None,
                        help="Host de Redis (default: env REDIS_HOST o 'localhost')")
    parser.add_argument("--redis-port", type=int, default=None,
                        help="Puerto de Redis (default: env REDIS_PORT o 6379)")
    return parser.parse_args(argv)


# Default de --out para uso directo vía ``python main.py``. El entry point
# ``./docuclickai`` ya inyecta su propio default antes de llamar a ``run``.
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "output" / "transfers.json"


def build_publisher(args: argparse.Namespace, *,
                    session_id: str | None) -> EventPublisher | None:
    """Construye el publisher desde los flags. Devuelve None si --no-publish
    o si no se proporcionó un session_id válido.

    Hace ping explícito: si Redis no responde al construir el publisher,
    propagamos ``RedisPublishError`` (fail-hard). El caller debe abortar
    con exit 4 antes de tocar el PDF / Ollama.

    ``session_id`` se inyecta en cada evento para que el consumidor pueda
    correlacionar todos los mensajes de un mismo run. Si falta o es vacío,
    no se puede publicar (cada evento requiere sid) y devolvemos None.
    """
    if args.no_publish or not session_id:
        return None
    pub = EventPublisher(
        session_id=session_id,
        host=args.redis_host,
        port=args.redis_port,
    )
    pub.ping()  # fail-hard si Redis no responde
    return pub


def _emit_error(publisher: EventPublisher | None, archivo: str,
                error: str, contexto: str) -> None:
    """Best-effort: si el publisher falla al emitir el error, no propagamos
    (ya estamos en un flujo de error y no queremos enmascarar el original)."""
    if publisher is None:
        return
    try:
        publisher.publish_error(
            archivo=archivo, error=error, contexto=contexto,
        )
    except RedisPublishError:
        pass


def main(argv: list[str] | None = None, *,
         log=None, session_id: str | None = None) -> int:
    """Entry point principal.

    ``log`` es un callable opcional para artefactos tmp/ (entry point lo
    inyecta; ``python -m main`` lo deja en None → no se escriben artefactos).

    ``session_id`` es el identificador único de la sesión (sid). El entry
    point lo extrae del directorio de sesión tmp/ y lo inyecta en cada
    evento publicado. Si se omite, los eventos NO se publican (publisher
    queda en None) para que ``python -m main`` funcione sin tocar Redis.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    out_path = args.out if args.out is not None else DEFAULT_OUT
    archivo = Path(args.pdf).name

    try:
        publisher = build_publisher(args, session_id=session_id)
    except RedisPublishError as exc:
        print(f"Error Redis: {exc}", file=sys.stderr)
        return 4

    try:
        run(args.pdf, out_path, model=args.model, host=args.host,
            timeout=args.timeout, max_retries=max_retries_safe(args),
            log=log, publisher=publisher)
    except SchemaError as exc:
        print(f"Error de esquema: {exc}", file=sys.stderr)
        _emit_error(publisher, archivo, str(exc), "schema_error")
        return 2
    except OllamaError as exc:
        print(f"Error Ollama: {exc}", file=sys.stderr)
        _emit_error(publisher, archivo, str(exc), "ollama_error")
        return 3
    except RedisPublishError as exc:
        print(f"Error Redis: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        _emit_error(publisher, archivo, str(exc), "unexpected")
        return 1
    finally:
        if publisher is not None:
            publisher.close()
    return 0


def max_retries_safe(args: argparse.Namespace) -> int:
    """Pequeño helper para mantener ``main`` corto y testeable."""
    return int(args.max_retries)


if __name__ == "__main__":
    raise SystemExit(main())
