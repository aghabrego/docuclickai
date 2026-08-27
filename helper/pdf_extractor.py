"""Extracción de texto desde el PDF de Transferencias NCR (Versalles).

Además de extraer el texto completo, provee un splitter que separa las
secciones ``Transfer In`` y ``Transfer Out`` para poder procesarlas en
dos pasadas (útil con modelos chicos como phi4-mini).
"""
from __future__ import annotations

import re
from pathlib import Path
from pypdf import PdfReader

# Marca el inicio de cada sección principal del reporte.
SECTION_HEADER = re.compile(r"^\s*Transfer\s+(In|Out)\s*$", re.IGNORECASE | re.MULTILINE)
HEADER_LINE = re.compile(r"(\d{2}\s+Wen\s+\S+)\s+\d+\s*-Transferencias")
PERIODO = re.compile(r"Periodo:\s*(\d+)")
ANIO = re.compile(r"Año Fiscal:\s*(\d+)")
RANGO = re.compile(r"\d{2}/\d{2}/\d{4}\s*-\s*\d{2}/\d{2}/\d{4}")

# Marca el inicio de un bloque de tienda. Debe ser EXACTAMENTE "<CODIGO> Wen <NOMBRE>"
# en una sola línea, sin contenido extra como "45 -Transferencias" o "Año Fiscal".
STORE_HEADER = re.compile(r"^\s*(\d{2}\s+Wen\s+[A-Za-zÀ-ÿ][^\n]*?)\s*$", re.MULTILINE)

# Ruido que aparece en cada página (headers repetidos, footers, copyright).
# Los patrones usan ``\s+`` agresivo para tolerar el espaciado de layout mode.
NOISE_PATTERNS = [
    # Pie de página: "V 21.2.0.381 - 45 - 07/10/2026 9:54 a. m.   Copyright © NCR Corporation 2024"
    # Solo una línea (NO usar DOTALL para no comerse contenido entre medio)
    re.compile(r"^\s*V\s+\d+\.\d+\.\d+\.\d+\s+-\s+\d+\s+-.*?$",
               re.MULTILINE | re.IGNORECASE),
    # "Copyright © NCR Corporation 2024" suelto (por si queda)
    re.compile(r"^\s*Copyright\s*©\s*NCR\s+Corporation\s+\d{4}.*?$",
               re.MULTILINE | re.IGNORECASE),
    # Numeración "1 de 4" (con espacios variables) y pegada
    re.compile(r"^\s*\d+\s+de\s+\d+\s*$", re.MULTILINE),
    re.compile(r"^\s*\d+de\d+\s*$", re.MULTILINE),
    # Header repetido en una sola línea: "<CODIGO> Wen <NOMBRE>   45 -Transferencias"
    re.compile(r"^\s*\d{2}\s+Wen\s+\S+\s+\d+\s*-\s*Transferencias\s*$",
               re.MULTILINE | re.IGNORECASE),
    # Componentes del header repetido
    re.compile(r"^\s*Año\s+Fiscal:\s*\d{4}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Periodo:\s*\d+\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*\d{2}/\d{2}/\d{4}\s*-\s*\d{2}/\d{2}/\d{4}\s*$", re.MULTILINE),
    # Encabezados de tabla repetidos (Description / Category / Quantity / Unit / Cost/Unit / Extension)
    re.compile(r"^\s*Description\s+Category\s+Quantity.*$",
               re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Quantity\s+Transferred.*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Unit\s+Transferred.*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Cost/Unit\s+Extension\s*$", re.MULTILINE | re.IGNORECASE),
    # Separadores explícitos
    re.compile(r"^\s*---\s*PAGE BREAK\s*---\s*$", re.MULTILINE),
]


def clean_noise(text: str) -> str:
    """Elimina headers repetidos, footers, copyright y separadores."""
    for pat in NOISE_PATTERNS:
        text = pat.sub("", text)
    # Colapsar 3+ saltos de línea a 1
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Quitar espacios al final de cada línea
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


def extract_text(pdf_path: str | Path, *, layout: bool = True) -> str:
    """Lee el PDF y devuelve el texto concatenado de todas las páginas.

    ``layout=True`` preserva mejor el posicionamiento (importante para que el
    header ``<CODIGO> Wen <NOMBRE>`` sea detectable). Se concatenan las
    páginas con un separador explícito.
    """
    reader = PdfReader(str(pdf_path))
    parts: list[str] = []
    for page in reader.pages:
        kwargs = {"extraction_mode": "layout"} if layout else {}
        text = page.extract_text(**kwargs) or ""
        parts.append(text)
    return "\n\n--- PAGE BREAK ---\n\n".join(parts)


def parse_header(full_text: str) -> dict:
    """Extrae origen, año fiscal, periodo y rango de fechas del header."""
    m_hdr = HEADER_LINE.search(full_text)
    origen = m_hdr.group(1).strip() if m_hdr else None
    anio = int(ANIO.search(full_text).group(1)) if ANIO.search(full_text) else None
    periodo = int(PERIODO.search(full_text).group(1)) if PERIODO.search(full_text) else None
    rango = RANGO.search(full_text)
    return {
        "origen": origen,
        "anio_fiscal": anio,
        "periodo": periodo,
        "rango_fechas": rango.group(0) if rango else None,
    }


def split_sections(full_text: str) -> dict[str, str]:
    """Divide el texto en ``header``, ``transfer_in`` y ``transfer_out``.

    La detección se hace por marcadores ``Transfer In`` / ``Transfer Out``
    al inicio de línea. Si no se encuentra la marca, devuelve string vacío.
    Cada sección se limpia de headers/footers repetidos vía ``clean_noise``.
    """
    matches = list(SECTION_HEADER.finditer(full_text))
    if not matches:
        return {"header": full_text, "transfer_in": "", "transfer_out": ""}

    header = full_text[: matches[0].start()].rstrip()
    sections: dict[str, str] = {"header": clean_noise(header)}
    for i, m in enumerate(matches):
        kind = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        raw = full_text[start:end].strip()
        sections[f"transfer_{kind.lower()}"] = clean_noise(raw)
    sections.setdefault("transfer_in", "")
    sections.setdefault("transfer_out", "")
    return sections


def split_stores(section_text: str) -> list[dict]:
    """Divide el texto de una sección (``Transfer In`` o ``Transfer Out``)
    en bloques por tienda.

    Devuelve una lista de ``{"tienda": "05 Wen Metromall", "texto": "..."}``
    en el orden de aparición. Si la sección está vacía, devuelve ``[]``.

    Filtra falsos positivos como el header repetido
    ``"18 Wen Versalles    45 -Transferencias   Año Fiscal: 2026"`` que
    podría capturar el regex amplio.
    """
    if not section_text or not section_text.strip():
        return []
    matches = list(STORE_HEADER.finditer(section_text))
    if not matches:
        return []
    blocks: list[dict] = []
    for i, m in enumerate(matches):
        tienda = m.group(1).strip()
        # Validación: el nombre de tienda debe tener EXACTAMENTE este formato
        # "<CODIGO> Wen <NOMBRE>" (sin "-Transferencias", sin "Año Fiscal", etc.)
        if not re.match(r"^\d{2}\s+Wen\s+\S+(\s+\S+)*\s*$", tienda):
            continue
        # Filtrar headers repetidos que se colaron (longitud máxima razonable)
        if len(tienda) > 40:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(section_text)
        texto = clean_noise(section_text[start:end].strip())
        blocks.append({"tienda": tienda, "texto": texto})
    return blocks
