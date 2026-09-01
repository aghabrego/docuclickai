"""Dispatcher de backends LLM (Ollama vs OpenAI-compatible).

Decide qué cliente usar según el nombre del modelo y, si hay duda, según
qué backend está disponible. Mantiene una única superficie pública para que
``main.py`` no tenga que conocer los detalles.

Reglas de ruteo (en orden):
1. Si ``model`` empieza con ``gpt-`` (OpenAI) o con un prefijo explícito
   en ``OPENAI_MODEL_PREFIXES`` → OpenAI client.
2. Si ``model`` contiene ``:`` (formato Ollama ``nombre:tag``) → Ollama.
3. Si no se puede decidir por el nombre, se prefiere Ollama si está
   corriendo; si no, se prueba OpenAI con la API key del entorno.
4. Si nada aplica, se lanza ``LLMBackendError`` con instrucciones claras.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from ollama_client import (
    DEFAULT_HOST as DEFAULT_OLLAMA_HOST,
    DEFAULT_MODEL as DEFAULT_OLLAMA_MODEL,
    chat_with_retry as ollama_chat_with_retry,
    is_truncated_json as ollama_is_truncated_json,
    parse_json as ollama_parse_json,
)
from ollama_client import OllamaError

from openai_client import (
    DEFAULT_BASE_URL as DEFAULT_OPENAI_BASE_URL,
    DEFAULT_MODEL as DEFAULT_OPENAI_MODEL,
    OpenAIError,
    chat as openai_chat,
    list_models as openai_list_models,
)

# Prefijos que disparan el backend OpenAI, además de "gpt-".
# Cobertura típica: openai-*, o1-*, chatgpt-*, gpt-4*, etc.
OPENAI_MODEL_PREFIXES: tuple[str, ...] = (
    "gpt-",
    "o1-",
    "o3-",
    "o4-",
    "chatgpt-",
    "openai/",
)


class LLMBackendError(RuntimeError):
    """No se pudo resolver un backend LLM para el modelo pedido."""


@dataclass(frozen=True)
class LLMConfig:
    """Configuración efectiva de un backend (solo lectura)."""
    backend: str                # "ollama" | "openai"
    model: str
    host: str                   # URL del backend (ollama host o openai base_url)
    api_key: str | None = None  # solo para openai

    def describe(self) -> str:
        if self.backend == "ollama":
            return f"Ollama model={self.model} host={self.host}"
        # enmascarar api key al imprimir
        masked = (self.api_key[:4] + "..." + self.api_key[-2:]) if self.api_key else "<missing>"
        return f"OpenAI model={self.model} base_url={self.host} key={masked}"


def _looks_like_ollama_model(model: str) -> bool:
    """Heurística: Ollama usa 'nombre:tag' (p. ej. 'qwen2.5:7b')."""
    return ":" in model and "/" not in model.split(":", 1)[0]


def _looks_like_openai_model(model: str) -> bool:
    low = model.lower()
    return any(low.startswith(p) for p in OPENAI_MODEL_PREFIXES)


def _ollama_alive(host: str, timeout: float = 1.0) -> bool:
    """Ping rápido a Ollama. False silencioso si no responde."""
    try:
        req = urllib.request.Request(f"{host.rstrip('/')}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def resolve_backend(
    model: str | None = None,
    *,
    ollama_host: str = DEFAULT_OLLAMA_HOST,
    openai_base_url: str | None = None,
    openai_api_key: str | None = None,
) -> LLMConfig:
    """Decide el backend a usar y devuelve su ``LLMConfig``.

    ``model`` puede venir ``None`` (caso ``--model`` no pasado) → se usa
    el default de Ollama (``qwen2.5:7b``) para no romper el flujo
    pre-existente. Si el usuario quiere OpenAI debe pasar ``--model
    gpt-4o-mini`` (o similar) explícitamente.
    """
    openai_base_url = (openai_base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/")
    effective_model = model or DEFAULT_OLLAMA_MODEL

    # Regla 1: prefijo explícito OpenAI
    if _looks_like_openai_model(effective_model):
        return LLMConfig(
            backend="openai",
            model=effective_model,
            host=openai_base_url,
            api_key=openai_api_key,
        )

    # Regla 2: tag estilo Ollama
    if _looks_like_ollama_model(effective_model):
        return LLMConfig(
            backend="ollama",
            model=effective_model,
            host=ollama_host,
        )

    # Regla 3: sin pistas claras. Si Ollama responde, usamos Ollama.
    # Si no responde y hay API key, caemos a OpenAI.
    if _ollama_alive(ollama_host):
        return LLMConfig(
            backend="ollama",
            model=effective_model,
            host=ollama_host,
        )
    if openai_api_key:
        return LLMConfig(
            backend="openai",
            model=effective_model,
            host=openai_base_url,
            api_key=openai_api_key,
        )

    raise LLMBackendError(
        f"No se pudo resolver un backend para model={effective_model!r}. "
        f"Ollama no responde en {ollama_host} y no se proporcionó "
        f"--openai-api-key (ni hay $OPENAI_API_KEY en el entorno)."
    )


# ---------------------------------------------------------------------------
# Wrappers unificados (la firma es estable: prompt → str JSON)
# ---------------------------------------------------------------------------

def chat_with_retry(
    prompt: str,
    *,
    config: LLMConfig,
    timeout: int = 600,
    max_retries: int = 2,
) -> str:
    """Llama al backend resuelto y devuelve el texto crudo del modelo."""
    if config.backend == "ollama":
        return ollama_chat_with_retry(
            prompt,
            model=config.model,
            host=config.host,
            timeout=timeout,
            max_retries=max_retries,
        )
    if config.backend == "openai":
        raw = openai_chat(
            prompt,
            model=config.model,
            base_url=config.host,
            api_key=config.api_key,
            timeout=timeout,
        )
        # Si viene truncado, reintentar con más max_tokens.
        if max_retries > 0 and ollama_is_truncated_json(raw):
            from openai_client import RETRY_MAX_TOKENS
            raw = openai_chat(
                prompt,
                model=config.model,
                base_url=config.host,
                api_key=config.api_key,
                timeout=timeout,
                max_tokens=RETRY_MAX_TOKENS,
            )
        return raw
    raise LLMBackendError(f"Backend desconocido: {config.backend!r}")


# Re-exportar utilidades comunes para que ``main.py`` no importe de
# ollama_client directamente.
is_truncated_json: Callable[[str], bool] = ollama_is_truncated_json
parse_json: Callable[[str], Any] = ollama_parse_json
