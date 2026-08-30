"""Pre-flight checks para el binario empaquetado (o ejecutable en general).

Dos chequeos, ambos fail-fast con exit codes propios:

1. ``check_ollama()`` → exit 5 si Ollama no responde.
   NO instala Ollama automáticamente. Solo imprime instrucciones.

2. ``check_model()`` → exit 6 si el modelo no está disponible.
   Si TTY y no se pasa ``auto_pull``, pregunta al usuario. Si se pasa
   ``auto_pull=True`` o stdin no es TTY, ejecuta ``ollama pull`` sin
   preguntar.

El objetivo es que el binario pueda copiarse a otro server y funcionar
sin sorpresas invasivas (no ``sudo``, no instalaciones silenciosas).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Sequence

# Reusar DEFAULT_MODEL/DEFAULT_HOST del cliente Ollama para evitar drift
# si el default cambia.
try:
    from ollama_client import DEFAULT_MODEL, DEFAULT_HOST  # type: ignore
except ImportError:  # pragma: no cover - binario standalone sin path
    DEFAULT_MODEL = "qwen2.5:7b"
    DEFAULT_HOST = "http://localhost:11434"

PREFETCH_TIMEOUT = 5.0     # s — ping a /api/tags
PULL_TIMEOUT = 1800.0      # s — 30 min; modelos grandes bajan lentos


class PreflightError(RuntimeError):
    """Fallo de pre-flight. Lleva un exit code sugerido."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def _which_ollama() -> str | None:
    return shutil.which("ollama")


def _http_get_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_ollama(host: str = DEFAULT_HOST,
                 binary: str | None = None) -> None:
    """Verifica que Ollama responde. Lanza PreflightError(exit 5) si no.

    Estrategia:
    1. Si no hay binario ``ollama`` en PATH → fail.
    2. Si el binario existe pero el server no responde → fail.
    """
    bin_path = binary or _which_ollama()
    if not bin_path:
        raise PreflightError(
            "Ollama no detectado en PATH.\n"
            "Instálalo desde: https://ollama.com/download/linux\n"
            "  curl -fsSL https://ollama.com/install.sh | sh\n"
            "Luego verifica con: ollama --version",
            exit_code=5,
        )
    try:
        data = _http_get_json(f"{host}/api/tags", timeout=PREFETCH_TIMEOUT)
        # Si responde, Ok. Si no, levanta excepción.
        if "models" not in data:
            raise PreflightError(
                f"Ollama responde en {host} pero /api/tags no devolvió 'models'. "
                "Versión incompatible?",
                exit_code=5,
            )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PreflightError(
            f"Ollama no responde en {host}.\n"
            f"Asegúrate de que esté corriendo: 'ollama serve' o como servicio.\n"
            f"Detalle: {exc}",
            exit_code=5,
        ) from exc


def _list_local_models(host: str) -> set[str]:
    """Devuelve el set de modelos disponibles localmente."""
    try:
        data = _http_get_json(f"{host}/api/tags", timeout=PREFETCH_TIMEOUT)
    except (urllib.error.URLError, TimeoutError, OSError):
        return set()
    names: set[str] = set()
    for m in data.get("models") or []:
        name = m.get("name") or m.get("model")
        if name:
            names.add(name)
            # También la versión sin ':latest' (Ollama a veces devuelve "qwen2.5:7b"
            # pero el usuario pidió solo "qwen2.5:7b"; manejamos alias aquí).
            base = name.split(":")[0]
            names.add(base)
    return names


def _prompt_yes_no(question: str, *, auto_yes: bool) -> bool:
    """Pregunta y/n. Si stdin no es TTY o auto_yes=True, devuelve auto_yes."""
    if auto_yes:
        return True
    if not sys.stdin.isatty():
        # No-interactive: rechaza por seguridad
        return False
    try:
        ans = input(f"{question} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def _run_pull(binary: str, model: str) -> int:
    """Ejecuta ``ollama pull <model>`` y devuelve el exit code."""
    proc = subprocess.run(
        [binary, "pull", model],
        timeout=PULL_TIMEOUT,
    )
    return proc.returncode


def check_model(model: str = DEFAULT_MODEL,
                host: str = DEFAULT_HOST,
                *,
                auto_pull: bool = False,
                binary: str | None = None) -> None:
    """Verifica que el modelo está disponible localmente.

    - Si está → no hace nada.
    - Si NO está y ``auto_pull=True`` → ejecuta ``ollama pull`` sin preguntar.
    - Si NO está y ``auto_pull=False`` y stdin es TTY → pregunta al usuario.
    - Si NO está y no es TTY ni auto_pull → fail con instrucciones.
    """
    local = _list_local_models(host)
    # match exacto o por base (sin tag)
    if model in local or model.split(":")[0] in local:
        return

    bin_path = binary or _which_ollama()
    if not bin_path:
        # check_ollama ya debería haber fallado, pero por defensa:
        raise PreflightError(
            f"Modelo '{model}' no disponible y Ollama no está instalado.",
            exit_code=6,
        )

    question = (
        f"El modelo '{model}' no está descargado (~5 GB). "
        f"¿Descargar ahora?"
    )
    if not _prompt_yes_no(question, auto_yes=auto_pull):
        raise PreflightError(
            f"Modelo '{model}' no disponible.\n"
            f"Descárgalo manualmente con:\n"
            f"  ollama pull {model}\n"
            f"O vuelve a ejecutar el comando aceptando la descarga.",
            exit_code=6,
        )

    print(f"[preflight] Descargando {model}...", flush=True)
    rc = _run_pull(bin_path, model)
    if rc != 0:
        raise PreflightError(
            f"ollama pull {model} falló con código {rc}.",
            exit_code=6,
        )

    # Re-validar
    local = _list_local_models(host)
    if model not in local and model.split(":")[0] not in local:
        raise PreflightError(
            f"Tras 'ollama pull {model}', el modelo sigue sin aparecer en "
            f"{host}/api/tags.",
            exit_code=6,
        )


def run_preflight(model: str = DEFAULT_MODEL,
                  host: str = DEFAULT_HOST,
                  *,
                  auto_pull: bool = False,
                  skip_ollama: bool = False,
                  skip_model: bool = False) -> None:
    """Ejecuta todos los chequeos. Lanza PreflightError si alguno falla."""
    if not skip_ollama:
        check_ollama(host=host)
    if not skip_model:
        check_model(model=model, host=host, auto_pull=auto_pull)


def add_preflight_args(parser) -> None:
    """Agrega flags CLI para los chequeos. Llamar desde parse_args()."""
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Modelo Ollama (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--auto-pull-model", action="store_true",
        help="Descargar el modelo automáticamente si falta (sin preguntar)",
    )
    parser.add_argument(
        "--skip-preflight", action="store_true",
        help="Saltar chequeos de Ollama/modelo (útil para tests)",
    )


def handle_preflight_args(args, host_attr: str = "host") -> None:
    """Ejecuta preflight desde argsparse. Sale con exit code apropriado."""
    host = getattr(args, host_attr, DEFAULT_HOST)
    try:
        run_preflight(
            model=args.model,
            host=host,
            auto_pull=args.auto_pull_model,
            skip_ollama=args.skip_preflight,
            skip_model=args.skip_preflight,
        )
    except PreflightError as exc:
        print(f"Error preflight: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code)


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_HOST",
    "PreflightError",
    "check_ollama",
    "check_model",
    "run_preflight",
    "add_preflight_args",
    "handle_preflight_args",
]


if __name__ == "__main__":  # pragma: no cover - debug manual
    import argparse
    p = argparse.ArgumentParser()
    add_preflight_args(p)
    p.add_argument("--host", default=DEFAULT_HOST)
    a = p.parse_args()
    handle_preflight_args(a)
    print("Preflight OK")