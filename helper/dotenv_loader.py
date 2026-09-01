"""Loader minimalista de archivos ``.env`` (sin dependencias externas).

Por qué existe: el binario empaquetado con PyInstaller debe poder leer
un ``.env`` al lado del usuario (en el CWD, o en el directorio del
binario) sin obligar a instalar ``python-dotenv``. Esto evita inflar
el binario con una dependencia que solo se usa para 5 variables.

Reglas:
- No pisa variables ya presentes en el entorno (``os.environ`` gana).
- Soporta comentarios (``#``) y líneas vacías.
- Soporta valores con o sin comillas (simples o dobles).
- Si el archivo no existe, no hace nada (silencioso).
- Idempotente: se puede llamar varias veces sin duplicar.

Uso::

    from dotenv_loader import load_dotenv
    load_dotenv()        # busca .env en CWD
    load_dotenv(path)    # o una ruta explícita
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

# Regex de "KEY=VALUE". Acepta comillas dobles o simples envolviendo el
# valor. No soporta escapes complejos (no los necesitamos: una API key
# nunca trae saltos de línea ni comillas adentro).
_LINE_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^#\n]*)\s*(?:#.*)?$"
)


def _parse_lines(text: str) -> Iterable[tuple[str, str]]:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(raw)
        if not m:
            continue
        key = m.group("key")
        val = m.group("val").strip()
        # Quitar comillas envolventes si las hay
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        yield key, val


def _candidate_paths(explicit: str | Path | None) -> list[Path]:
    """Devuelve las rutas candidatas a probar, en orden de prioridad."""
    if explicit is not None:
        return [Path(explicit).expanduser().resolve()]
    here = Path.cwd().resolve()
    return [here / ".env"]


def load_dotenv(path: str | Path | None = None, *,
                override: bool = False) -> list[str]:
    """Carga variables desde ``.env``.

    Args:
        path: ruta explícita al archivo. Si es ``None``, busca ``.env`` en
            el CWD.
        override: si es ``True``, pisa valores ya presentes en
            ``os.environ``. Por defecto es ``False`` (el entorno gana).

    Returns:
        Lista de nombres de variables que se cargaron (no necesariamente
        cambiaron si ya existían y ``override=False``).
    """
    loaded: list[str] = []
    for candidate in _candidate_paths(path):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for key, val in _parse_lines(text):
            if not override and key in os.environ:
                continue
            os.environ[key] = val
            loaded.append(key)
        # Solo leemos el primer .env que encontremos
        break
    return loaded
