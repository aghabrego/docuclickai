"""Cliente ligero para la API de OpenAI (chat/completions).

Pensado para ser un *drop-in* mínimo: misma firma que ``ollama_client.chat``
y ``ollama_client.chat_with_retry``, así el resto del pipeline no se entera
de cuál backend se está usando.

Modelos soportados: cualquier endpoint compatible con la API de OpenAI
(oficial, Azure OpenAI, OpenRouter, etc.) que implemente
``POST /v1/chat/completions`` con ``response_format: {"type": "json_object"}``.

Soporta ``gpt-4o-mini``, ``gpt-4o``, ``gpt-4.1-mini``, ``gpt-3.5-turbo``, etc.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

# Auto-cargar .env al importarse (idempotente, silencioso si no existe).
# Se hace ANTES de leer cualquier env var abajo.
try:
    from dotenv_loader import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:  # noqa: BLE001  nunca debe romper el import del modulo
    pass

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT = 600          # s — cubre cold-start + secciones grandes
DEFAULT_MAX_TOKENS = 8192      # tokens de salida
RETRY_MAX_TOKENS = 16384       # reintento cuando el JSON viene truncado


class OpenAIError(RuntimeError):
    """Error al comunicarse con la API de OpenAI."""


def _api_key(explicit: str | None) -> str:
    """Resuelve la API key. Prioridad: argumento explícito > $OPENAI_API_KEY."""
    if explicit:
        return explicit
    env = os.environ.get("OPENAI_API_KEY")
    if env:
        return env
    raise OpenAIError(
        "Falta API key de OpenAI. Pásala con --openai-api-key "
        "o exporta OPENAI_API_KEY=sk-... en el entorno."
    )


def chat(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Llama a ``POST {base_url}/chat/completions`` y devuelve el contenido.

    Fuerza ``response_format: {"type": "json_object"}`` y ``temperature: 0``
    para máxima determinismo (mismas reglas que en el cliente de Ollama).
    Lanza ``OpenAIError`` con mensaje legible en caso de error HTTP o de
    payload vacío.
    """
    key = _api_key(api_key)
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Eres un extractor JSON determinista. "
                    "Respondes SOLO con JSON válido, sin markdown ni texto extra."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Cuerpo del error: viene como JSON con {error: {message, type}}
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            msg = (detail.get("error") or {}).get("message") or str(detail)
        except Exception:  # noqa: BLE001
            msg = str(exc)
        raise OpenAIError(
            f"HTTP {exc.code} de {url}: {msg} (model={model})"
        ) from exc
    except urllib.error.URLError as exc:
        raise OpenAIError(f"{exc} (model={model}, base_url={base_url})") from exc
    except TimeoutError as exc:
        raise OpenAIError(
            f"timeout tras {timeout}s (model={model}, base_url={base_url})"
        ) from exc

    try:
        choice = body["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenAIError(
            f"Respuesta inesperada de OpenAI (sin 'choices[0].message.content'): "
            f"{body!r}"
        ) from exc
    if not isinstance(content, str) or not content.strip():
        finish = (body.get("choices") or [{}])[0].get("finish_reason")
        raise OpenAIError(
            f"OpenAI devolvió contenido vacío (finish_reason={finish!r}, "
            f"model={model}). Suele ser por 'max_tokens' insuficiente: "
            f"prueba a subir el timeout o reducir el tamaño del bloque."
        )
    return content


def list_models(
    *, base_url: str = DEFAULT_BASE_URL, api_key: str | None = None,
    timeout: float = 5.0,
) -> list[str]:
    """Lista los modelos visibles para la API key. Útil para preflight."""
    key = _api_key(api_key)
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    out: list[str] = []
    for m in body.get("data") or []:
        mid = m.get("id")
        if isinstance(mid, str):
            out.append(mid)
    return out
