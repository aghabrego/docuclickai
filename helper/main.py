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
        return _process_store_block(tienda, store_text, transfer_meta=None,
                                    model=model, host=host, timeout=timeout,
                                    max_retries=max_retries, log=log)

    transferencias: list = []
    for meta, bloque_texto in bloques:
        tr = _process_store_block(tienda, bloque_texto, transfer_meta=meta,
                                  model=model, host=host, timeout=timeout,
                                  max_retries=max_retries, log=log)
        if tr:
            transferencias.append(tr)

    # Validación de completitud: si Ollama devolvió menos transferencias que
    # las que detectamos en el texto crudo, intentar recuperar las faltantes
    # con reintento individual.
    if len(transferencias) < len(bloques):
        missing = _find_missing_transfers(bloques, transferencias)
        if missing:
            log(f"ollama_{tienda.replace(' ', '_')}_completeness",
                f"faltan {len(missing)} transfers: {missing}")
            for meta, bloque_texto in missing:
                tr = _process_store_block(tienda, bloque_texto, transfer_meta=meta,
                                          model=model, host=host, timeout=timeout,
                                          max_retries=max_retries, log=log,
                                          tag="retry_missing")
                if tr:
                    transferencias.append(tr)
            # Ordenar por datetime para que el output sea estable
            transferencias.sort(key=lambda t: t.get("transfer_datetime", ""))

    coerced: dict = {
        "tienda": tienda,
        "transferencias": transferencias,
        "subtotal_tienda": None,
    }
    # Recalcular subtotal_tienda
    s = 0.0
    for tr in transferencias:
        v = tr.get("transfer_total")
        if isinstance(v, (int, float)):
            s += float(v)
    if s > 0:
        coerced["subtotal_tienda"] = round(s, 2)
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
    """Si ``cost_unit`` parece ser el número de empaque en vez del precio,
    lo recalcula como ``extension / quantity`` y marca el item.

    Heurísticas que disparan corrección (cualquiera):
    1. ``cost_unit * quantity > extension * 2`` (costo_unitario bruto es
       mucho mayor que la extensión real).
    2. ``cost_unit < extension / quantity * 0.5`` y ``cost_unit`` es un
       entero "redondo" sin decimales (señal típica de empaque:
       ``cost=20`` cuando ext=35.45 y qty=1 → real price es 35.45).

    NOTA: hay un caso límite ambiguo (``cost=20``, ``ext=35.45``, ``qty=1``)
    donde 20 cae entre 0.5x y 1x del precio real. Corregirlo silenciosamente
    podría romper items legítimos donde el precio sí es 20. Se deja sin
    tocar a propósito; si se observa, agregar una tercera heurística que
    cruce con ``unit_transferred`` (regex: si el número del empaque aparece
    dentro de la cadena, es el bug).

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
    if c < 1:                  # precios < 1 son válidos (no tocar)
        return False

    # Heurística 1: cost_unit * qty >> extension (caso Metromall 1944, 640, 500)
    if float(c) * float(q) > float(e) * 2.0:
        item["cost_unit"] = round(float(e) / float(q), 4)
        item["_validation_warning"] = "cost_unit_inferred"
        return True

    # Heurística 2: cost_unit es entero y mucho menor que el precio real
    if c == int(c):
        real_unit = float(e) / float(q)
        if real_unit > 0 and float(c) < real_unit * 0.5:
            item["cost_unit"] = round(real_unit, 4)
            item["_validation_warning"] = "cost_unit_inferred"
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
) -> dict | None:
    """Procesa UN bloque de texto (toda la tienda o un solo transfer).

    Si ``transfer_meta`` viene con id+datetime, los fija en el resultado
    (Ollama a veces omite transfer_datetime cuando el bloque es muy
    pequeño). Devuelve el dict de transferencia, o ``None`` si Ollama
    devolvió un array vacío / sin transfers.
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
    coerced["transferencias"] = _sanitize_transfers(
        coerced.get("transferencias") or [])

    # Punto 4: corrección defensiva de cost_unit
    for tr in coerced["transferencias"]:
        for it in tr.get("items") or []:
            _infer_cost_unit(it)

    # Si transfer_meta viene, fijar id y datetime (el LLM chiquito a veces los omite)
    if transfer_meta:
        for tr in coerced["transferencias"]:
            if not tr.get("transfer_id"):
                tr["transfer_id"] = transfer_meta["transfer_id"]
            if not tr.get("transfer_datetime"):
                tr["transfer_datetime"] = transfer_meta["transfer_datetime"]

    # Punto 3: validar transfer_total vs Σ extension. Si no cuadra, reintentar
    # SOLO este bloque (no la tienda entera).
    for tr in coerced["transferencias"]:
        if not _items_match_transfer_total(tr):
            tr["_validation_warning_total"] = "transfer_total != sum(extension)"
            log(f"ollama_{slug}_total_mismatch_{tag}",
                f"transfer {tr.get('transfer_datetime')}: "
                f"total={tr.get('transfer_total')} sum={sum((it.get('extension') or 0) for it in tr.get('items') or [])}")

    # Si el bloque devolvió 0 transfers y transfer_meta viene, crear uno mínimo
    if not coerced["transferencias"] and transfer_meta:
        coerced["transferencias"] = [{
            "transfer_id": transfer_meta["transfer_id"],
            "transfer_datetime": transfer_meta["transfer_datetime"],
            "transfer_date": transfer_meta["transfer_datetime"][:10],
            "items": [],
            "transfer_total": None,
            "_empty_response": True,
        }]

    return coerced


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
) -> dict:
    log = log or (lambda *_a, **_k: None)

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
        in_list.append(_process_one_store(
            s["tienda"], s["texto"],
            model=model, host=host, timeout=timeout, max_retries=max_retries,
            log=log,
        ))

    print(f"[3/4] Procesando {len(stores_out)} tiendas Transfer Out con Ollama ({model})...", flush=True)
    out_list = []
    for s in stores_out:
        print(f"   · {s['tienda']}", flush=True)
        out_list.append(_process_one_store(
            s["tienda"], s["texto"],
            model=model, host=host, timeout=timeout, max_retries=max_retries,
            log=log,
        ))

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
    return parser.parse_args(argv)


# Default de --out para uso directo vía ``python main.py``. El entry point
# ``./docuclickai`` ya inyecta su propio default antes de llamar a ``run``.
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "output" / "transfers.json"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    out_path = args.out if args.out is not None else DEFAULT_OUT
    try:
        run(args.pdf, out_path, model=args.model, host=args.host,
            timeout=args.timeout, max_retries=args.max_retries)
    except SchemaError as exc:
        print(f"Error de esquema: {exc}", file=sys.stderr)
        return 2
    except OllamaError as exc:
        print(f"Error Ollama: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
