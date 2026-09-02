"""Pre-flight checks para el binario empaquetado (o ejecutable en general).

Dos familias de chequeos, ambos fail-fast con exit codes propios:

* **Ollama** (``check_ollama()`` / ``check_model()``):
  - ``check_ollama()`` → exit 5 si Ollama no responde.
    NO instala Ollama automáticamente. Solo imprime instrucciones.
  - ``check_model()`` → exit 6 si el modelo no está disponible.
    Si TTY y no se pasa ``auto_pull``, pregunta al usuario. Si se pasa
    ``auto_pull=True`` o stdin no es TTY, ejecuta ``ollama pull`` sin
    preguntar.

* **OpenAI-compatible** (``check_openai()``):
  - ``check_openai()`` → exit 7 si la API key falta, es inválida, o el
    modelo no está visible. NO descarga nada; solo valida contra
    ``/v1/models``.

El objetivo es que el binario pueda copiarse a otro server y funcionar
sin sorpresas invasivas (no ``sudo``, no instalaciones silenciosas).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Sequence

# Auto-cargar .env al importarse (idempotente). Igual que en
# openai_client: nunca debe romper el import si dotenv_loader falta.
try:
    from dotenv_loader import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:  # noqa: BLE001
    pass

# Reusar DEFAULT_MODEL/DEFAULT_HOST del cliente Ollama para evitar drift
# si el default cambia.
try:
    from ollama_client import DEFAULT_MODEL, DEFAULT_HOST  # type: ignore
except ImportError:  # pragma: no cover - binario standalone sin path
    DEFAULT_MODEL = "qwen2.5:7b"
    DEFAULT_HOST = "http://localhost:11434"

try:
    from openai_client import (
        DEFAULT_BASE_URL as DEFAULT_OPENAI_BASE_URL,
        DEFAULT_MODEL as DEFAULT_OPENAI_MODEL,
        OpenAIError,
        list_models as openai_list_models,
    )
    from llm_client import _looks_like_openai_model  # type: ignore
except ImportError:  # pragma: no cover - binario standalone sin path
    DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
    DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
    OpenAIError = RuntimeError  # type: ignore
    def _looks_like_openai_model(_m: str) -> bool:  # type: ignore
        return False
    def openai_list_models(*_a, **_k):  # type: ignore
        raise RuntimeError("openai_client no disponible")

PREFETCH_TIMEOUT = 5.0     # s — ping a /api/tags
PULL_TIMEOUT = 1800.0      # s — 30 min; modelos grandes bajan lentos
OPENAI_CHECK_TIMEOUT = 5.0 # s — ping a /v1/models


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


def check_openai(
    model: str = DEFAULT_OPENAI_MODEL,
    base_url: str = DEFAULT_OPENAI_BASE_URL,
    api_key: str | None = None,
) -> None:
    """Verifica que la API key es válida y el modelo está disponible.

    Lanza ``PreflightError(exit 7)`` si:

    - No hay API key (ni en flag ni en ``$OPENAI_API_KEY``).
    - La key es inválida o el endpoint no responde.
    - El modelo pedido no aparece en ``/v1/models``.

    No descarga nada. Solo valida credenciales y catálogo.
    """
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise PreflightError(
            "Falta API key de OpenAI.\n"
            "Pásala con --openai-api-key o exporta OPENAI_API_KEY=sk-... ",
            exit_code=7,
        )
    try:
        available = openai_list_models(
            base_url=base_url, api_key=key, timeout=OPENAI_CHECK_TIMEOUT,
        )
    except (OpenAIError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PreflightError(
            f"No se pudo conectar a {base_url}/v1/models.\n"
            f"Detalle: {exc}",
            exit_code=7,
        ) from exc

    if not available:
        # La key respondió pero el catálogo está vacío (raro pero posible
        # en deployments con scopes limitados). No fallamos por esto: el
        # modelo podría existir y aun así no listarse.
        return
    if model not in available and model.split(":")[0] not in available:
        # Sugerimos los primeros 5 modelos visibles para ayudar al usuario.
        sample = ", ".join(available[:5])
        raise PreflightError(
            f"Modelo '{model}' no está disponible en {base_url}.\n"
            f"Modelos visibles (muestra): {sample}\n"
            f"Prueba con --model <otro> o revisa el scope de tu API key.",
            exit_code=7,
        )


def run_preflight(model: str = DEFAULT_MODEL,
                  host: str = DEFAULT_HOST,
                  *,
                  auto_pull: bool = False,
                  skip_ollama: bool = False,
                  skip_model: bool = False,
                  openai_base_url: str | None = None,
                  openai_api_key: str | None = None) -> None:
    """Ejecuta todos los chequeos. Lanza PreflightError si alguno falla.

    Si ``model`` es de la familia OpenAI (prefijo ``gpt-``, ``o1-``...)
    se valida contra el endpoint de OpenAI en vez de Ollama.
    """
    if _looks_like_openai_model(model or DEFAULT_MODEL):
        # Backend OpenAI: validar API key + modelo.
        if not skip_model:
            check_openai(
                model=model,
                base_url=openai_base_url or DEFAULT_OPENAI_BASE_URL,
                api_key=openai_api_key,
            )
        return

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