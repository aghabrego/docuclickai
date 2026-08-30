"""Gestor de carpeta temporal ``tmp/`` con nombres largos y limpieza perezosa.

Convenciones:
- Raíz: ``<proyecto>/tmp/`` (creada al vuelo).
- Subcarpeta de sesión por ejecución: ``tmp/<timestamp>_<token>/``.
- Nombres de archivo largos generados con uuid4 + timestamp + sufijo.
- Limpieza perezosa: al ejecutar se borran entradas con mtime > 24h.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path

MAX_AGE = timedelta(hours=24)
SESSION_PREFIX = "session_"


def tmp_root(project_root: Path) -> Path:
    return project_root / "tmp"


def lazy_cleanup(project_root: Path, *, now: datetime | None = None) -> int:
    """Borra archivos/dirs en tmp/ con mtime > MAX_AGE. Devuelve cantidad."""
    root = tmp_root(project_root)
    if not root.exists():
        return 0
    now = now or datetime.now()
    cutoff = now - MAX_AGE
    removed = 0
    for entry in root.iterdir():
        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime)
        except FileNotFoundError:
            continue
        if mtime < cutoff:
            try:
                if entry.is_dir() and not entry.is_symlink():
                    _rmtree(entry)
                else:
                    entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def new_session_dir(project_root: Path) -> Path:
    """Crea y devuelve ``tmp/session_<timestamp>_<token>/``.

    El identificador de sesión (sid) es el nombre del directorio:
    ``session_<timestamp>_<token>``. Útil para correlacionar artefactos y
    eventos que pertenecen al mismo run.
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    token = secrets.token_hex(6)  # 12 chars
    sid = f"{SESSION_PREFIX}{ts}_{token}"
    path = tmp_root(project_root) / sid
    path.mkdir(parents=True, exist_ok=False)
    return path


def session_id_from_path(session_dir: Path) -> str:
    """Extrae el sid (nombre del directorio) de un path de sesión."""
    return Path(session_dir).name


def long_name(prefix: str, *, suffix: str = "") -> str:
    """Genera un nombre base largo: ``<prefix>_<timestamp>_<uuid>[__<suffix>]``.

    ``suffix`` se usa solo como etiqueta legible; la extensión real la aplica
    el caller con ``Path.with_suffix`` para evitar duplicaciones.
    """
    ts_ms = int(time.time() * 1000)
    uid = secrets.token_hex(16)  # 32 chars
    parts = [prefix, f"{ts_ms:013d}", uid]
    base = "_".join(parts)
    return f"{base}__{suffix}" if suffix else base


def write_artifact(session_dir: Path, prefix: str, content: str, *, suffix: str = "txt") -> Path:
    """Escribe ``content`` en session_dir con nombre largo y devuelve la ruta."""
    name = long_name(prefix, suffix=suffix)
    path = session_dir / Path(name).with_suffix(f".{suffix}")
    path.write_text(content, encoding="utf-8")
    return path


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
