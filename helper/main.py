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
3. FECHAS: conserva DD/MM/YYYY tal cual vienen del PDF.
4. CATEGORÍAS válidas: Alimentos, Papeleria, Operaciones, Limpieza.
5. CAMPOS NUMÉRICOS son SIEMPRE float. Si faltan, usa null (NO 0).
6. subtotal_tienda: SOLO si ves la línea "Transfer Total: B/.XXX.XX" inmediatamente después de las filas de items de una transferencia (cierre del bloque). NO confundas "Transfer In Total" o "Transfer Out Total" (que aparecen UNA vez al final de toda la sección, no al final de cada tienda) con subtotal_tienda.
7. Si no estás seguro del subtotal_tienda, déjalo como null — lo recalcularemos.
8. Devuelve ÚNICAMENTE el objeto raíz con la clave "transferencias" como array.
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
    """Llama a Ollama para una sola tienda y devuelve el dict normalizado."""
    raw = chat_with_retry(
        build_store_prompt(tienda, store_text),
        model=model, host=host, timeout=timeout, max_retries=max_retries,
    )
    log(f"ollama_{tienda.replace(' ', '_')}_raw", raw)
    if is_truncated_json(raw):
        log(f"ollama_{tienda.replace(' ', '_')}_truncated_flag", "TRUNCATED")
        raise SchemaError(f"Tienda {tienda}: respuesta JSON truncada incluso tras reintento")
    parsed = parse_json(raw)
    coerced = _coerce_store_response(parsed)
    coerced["tienda"] = tienda

    # Recalcular subtotal_tienda desde transfer_total (suma de los items confirmados).
    # Confiamos más en la suma de los transfer_total extraídos que en el valor
    # que el modelo asignó a subtotal_tienda (que a veces es el total de la sección).
    computed_subtotal = 0.0
    for tr in coerced.get("transferencias") or []:
        if not isinstance(tr, dict):
            continue
        v = tr.get("transfer_total")
        if isinstance(v, (int, float)):
            computed_subtotal += float(v)
    if computed_subtotal > 0:
        coerced["subtotal_tienda"] = round(computed_subtotal, 2)

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
    max_retries: int = 1,
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
    parser.add_argument("--max-retries", type=int, default=1, help="Reintentos si el JSON viene truncado (default: 1)")
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
