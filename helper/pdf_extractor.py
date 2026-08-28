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
    # Header de página partido en 1+ líneas por layout mode (Fix #2 / #5).
    # En el PDF real se ve así (1 o 2 líneas, según el caso):
    #   " 18 Wen Versalles                                         45 -Transferencias\n"
    #   "                                                  Año Fiscal: 2026"
    # o todo en una línea. A veces "Transferencias" se rompe como "Transferencia\ns".
    # Solo nos importa que la línea contenga ambos marcadores (Wen + Año Fiscal)
    # O que un bloque de líneas consecutivas los contenga.
    re.compile(
        r"^\s*\d{2}\s+Wen\s+\S+.*?\d+\s*-\s*Transferencias?"
        r".*?"
        r"(?:\n[^\n]*?)?"
        r"Año\s+Fiscal:\s*\d{4}\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # Variante multi-línea explícita (Fix #2 / #5) para los casos en que
    # el header y "Año Fiscal" están en líneas separadas. Acepta que
    # "Transferencias" se rompa como "Transferencia\ns".
    re.compile(
        r"^\s*\d{2}\s+Wen\s+\S+.*?\d+\s*-\s*Transferen[^\n]*\n"
        r"(?:[^\n]*\n)*?"
        r"Año\s+Fiscal:\s*\d{4}\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # Componentes del header repetido (por si quedó algún sueltos)
    re.compile(r"^\s*Año\s+Fiscal:\s*\d{4}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Periodo:\s*\d+\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*\d{2}/\d{2}/\d{4}\s*-\s*\d{2}/\d{2}/\d{4}\s*$", re.MULTILINE),
    # Encabezados de tabla repetidos (Description / Category / Quantity / Unit / Cost/Unit / Extension).
    # En layout mode la línea "Description  Category  Quantity Transferred  Unit Transferred  Cost/Unit  Extension"
    # puede aparecer partida en 2-3 líneas. Aceptamos cualquier fragmento.
    re.compile(r"^\s*Description\s+Category\s+Quantity.*?$",
               re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Quantity\s+Transferred.*?$",
               re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Unit\s+Transferred.*?$",
               re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Cost/Unit\s+Extension\s*$", re.MULTILINE | re.IGNORECASE),
    # Encabezado partido: "Description" / "Category  Quantity Transferred" /
    # "Unit Transferred  Cost/Unit  Extension" cada uno en su línea.
    re.compile(r"^\s*Description\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Category\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Quantity\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Unit\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Cost/Unit\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Extension\s*$", re.MULTILINE | re.IGNORECASE),
    # Separadores explícitos
    re.compile(r"^\s*---\s*PAGE BREAK\s*---\s*$", re.MULTILINE),
]

# Patrón multi-línea para el encabezado de tabla partido por layout mode.
# El PDF NCR envuelve las palabras del header así (2-4 líneas):
#     "Quantit"
#     "y             Unit"
#     "Description  Category  Transferr"
#     "ed       Transferred  Cost/Unit  Extension"
# Identificamos el bloque por la presencia de "Description" o "Quantit" en
# alguna de las líneas, y descartamos todas las líneas conectadas hasta
# encontrar "Extension" (con cierre) o una línea con un item real.
_TABLE_HEADER_FRAGMENTS = re.compile(
    r"^[ \t]*(?:"
    r"Quantit|y|Unit|Description|Category|Transferr|ed|Transferred|Cost/Unit|Extension"
    r")[ \t]*(?:[A-Za-z]+[ \t]+)*[A-Za-z]*[ \t]*$",
    re.MULTILINE,
)

# Keywords que SOLO aparecen en el header (no en items reales)
_HEADER_ONLY_WORDS = {
    "description", "category", "quantity", "unit", "cost/unit", "extension",
    "transferred", "quantit", "transferr",
    # Fragmentos de word-wrap (letras sueltas resultado del corte del layout mode)
    "y", "ed",
    # "cost" separado de "Cost/Unit" cuando el slash se parte
    "cost",
}


def clean_noise(text: str) -> str:
    """Elimina headers repetidos, footers, copyright y separadores."""
    for pat in NOISE_PATTERNS:
        text = pat.sub("", text)
    # Eliminar encabezado de tabla partido por layout mode (Fix #2/#5).
    text = _strip_table_header_fragments(text)
    # Colapsar 3+ saltos de línea a 1
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Quitar espacios al final de cada línea
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


def _strip_table_header_fragments(text: str) -> str:
    """Elimina las líneas del encabezado de tabla que ``pypdf`` partió por
    word-wrap (Fix #2/#5).

    Una línea individual de este encabezado contiene SOLO palabras del set
    ``_HEADER_ONLY_WORDS`` (posiblemente truncadas). Si una línea cumple
    eso Y hay al menos otra línea cercana (saltando blancos) que también
    parece fragmento del header, descartamos el bloque contiguo.

    Para ser conservador: solo descartamos una línea si contiene palabras
    exclusivas del header (no cualquier palabra). Esto evita falsos
    positivos con items reales que tengan una sola palabra como
    descripción (ej. "Bulto = 20 LB" o "Cookie").
    """
    if not text:
        return text

    def _is_header_line(s: str) -> bool:
        words = set(re.findall(r"[A-Za-z]+", s.lower()))
        return bool(words) and words.issubset(_HEADER_ONLY_WORDS)

    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_header_line(line):
            # Mirar si en las siguientes líneas (saltando blancos) hay
            # al menos OTRO fragmento del header → bloque real a descartar.
            j = i + 1
            found_another = False
            # Saltar blancos iniciales
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and _is_header_line(lines[j]):
                found_another = True
            if found_another:
                # Descartar todo el bloque: desde i hasta la línea "real"
                # posterior (o final). Las líneas reales se detectan por
                # tener números, B/., o palabras fuera del header.
                k = i
                while k < len(lines):
                    cur = lines[k]
                    if not cur.strip():
                        k += 1
                        continue
                    if _is_header_line(cur):
                        k += 1
                        continue
                    # Línea no-header: si tiene números o B/., es contenido real
                    if re.search(r"\d|B/\.", cur):
                        break
                    # Si es una línea con palabras no-header pero sin números
                    # (poco probable), paramos también
                    break
                i = k
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


# Detecta el patrón de monto NCR: "B/.12.34" o "B/.1,036.70" (con coma de miles).
# Solo aparecen al final de las filas de items, justo antes del "Transfer Total".
_MONEY_RE = re.compile(r"B/\.\s*\d{1,3}(?:,\d{3})*\.\d{2}")
# Detecta el inicio típico de un item: descripción en PascalCase, sin monto al final.
_ITEM_START_RE = re.compile(r"^[A-ZÁÉÍÓÚÑ]")
# Detecta el inicio de una fila de item "completa" tras un posible pegado:
# categoria conocida + número (quantity) en una posición fija de la línea.
_ROW_HAS_QTY_RE = re.compile(
    r"\b(Alimentos|Papeleria|Operaciones|Limpieza)\b"
    r"\s+\d{1,6}\.\d{2}\s+(?:CA|CA |BO|BL|PQ|BOLSA|UN|LBS|LB|OZ)",
    re.IGNORECASE,
)


def join_broken_lines(text: str) -> str:
    """Une líneas partidas por ``pypdf`` dentro de la misma celda del PDF.

    El layout mode de pypdf suele romper una fila en dos cuando la columna
    ``unit_transferred`` o ``description`` no entra en el ancho de página.
    Por ejemplo::

        Empanizador para Pollo WD 1/40LB          Alimentos   1.00  Bulto = 20 LB
        (2/20)                                                                 B/.35.45

    o::

        Pan Glazzed PREMIUM BUN 4 WD
        8/30un (240un)                          Alimentos   3.00  CA=8/30 UN    B/.149.85

    Esto confunde a Ollama al alinear columnas, dejando ``unit_transferred``
    truncado y ``cost_unit`` con números sin sentido. La heurística:

    1. Si una línea no contiene monto ``B/.X.XX`` y la siguiente SÍ contiene
       un monto, son la misma fila → concatenar con un espacio.
    2. Si una línea empieza con ``(`` (coletilla tipo ``(2/20)``, ``(720un)``,
       ``(CA=24/50)``), es continuación de la línea previa → concatenar.
    3. Si la línea actual parece un item completo (category + qty + unit en
       la misma línea), NO es continuación aunque la anterior no tuviera
       monto — es un item nuevo mal alineado.

    El cierre de transferencia (``Transfer Total: B/.X.XX``) se preserva
    intacto porque la heurística (1) no une líneas que ya contienen monto
    y la (3) requiere el patrón completo de fila de item.
    """
    if not text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        if not out:
            out.append(line)
            continue
        prev = out[-1]
        has_money_prev = bool(_MONEY_RE.search(prev))
        has_money_curr = bool(_MONEY_RE.search(line))
        stripped = line.lstrip()

        # La línea actual ES una fila de item completa (category + qty + unit).
        # NO es continuación: es un item nuevo mal alineado por pypdf.
        # Esto arregla el caso "Wendys 6/6 LB Pollo Rosty 9/18 UN ..." donde
        # la coletilla "Wendys 6/6 LB" se pegó al inicio del item siguiente.
        if _ROW_HAS_QTY_RE.search(line):
            out.append(line)
            continue

        # Regla 2: coletilla entre paréntesis al inicio de la línea
        if stripped.startswith("(") and not has_money_curr and not has_money_prev:
            out[-1] = f"{prev} {stripped}"
            continue

        # Regla 1: la línea previa NO terminó con monto, esta SÍ
        # → es la misma fila partida
        if (not has_money_prev) and has_money_curr:
            # sanity: la línea actual debe ser "corta" o terminar en monto
            # (no debe empezar con un "Transfer:" que sea un nuevo bloque)
            if not re.match(r"^Transfer\s*[:ID-]", stripped):
                out[-1] = f"{prev} {stripped}"
                continue

        # Regla 3 (legacy): línea sin monto, sin mayúscula → continuación
        if (not has_money_curr) and stripped and not _ITEM_START_RE.match(stripped):
            if not re.match(r"^(Transfer\s+(In|Out)\s+Total|Net\s+Transfer)", stripped):
                if not has_money_prev:
                    out[-1] = f"{prev} {stripped}"
                    continue

        out.append(line)
    return "\n".join(out)


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
        sections[f"transfer_{kind.lower()}"] = join_broken_lines(clean_noise(raw))
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
        texto = join_broken_lines(clean_noise(section_text[start:end].strip()))
        blocks.append({"tienda": tienda, "texto": texto})
    return blocks
