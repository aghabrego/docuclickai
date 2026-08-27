"""Cliente ligero para Ollama (modelo por defecto: phi4-mini:latest)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT = 600          # s — cubre cold-start + 2 secciones grandes
DEFAULT_NUM_PREDICT = 8192     # tokens de salida
RETRY_NUM_PREDICT = 16384      # reintento cuando el JSON viene truncado
DEFAULT_NUM_CTX = 16384        # context window (forzado; Ollama defaulta a 4096)


class OllamaError(RuntimeError):
    """Error al comunicarse con Ollama."""


def chat(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    format_json: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    num_predict: int = DEFAULT_NUM_PREDICT,
    num_ctx: int = DEFAULT_NUM_CTX,
) -> str:
    """Llama a /api/generate de Ollama y devuelve la respuesta como texto.

    Lanza ``OllamaError`` con mensaje legible en caso de timeout o HTTP error.
    Fija ``num_ctx`` explícitamente porque Ollama defaulta a 4096 y eso
    desborda el prompt completo.
    """
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,        # desactiva thinking para modelos como qwen3.5
        "options": {"num_predict": num_predict, "num_ctx": num_ctx},
    }
    if format_json:
        payload["format"] = "json"

    req = urllib.request.Request(
        f"{host}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise OllamaError(f"{exc} (model={model}, host={host})") from exc
    except TimeoutError as exc:
        raise OllamaError(f"timeout tras {timeout}s (model={model})") from exc
    return body.get("response", "")


def is_truncated_json(raw: str) -> bool:
    """Heurística: el JSON está truncado si no se puede parsear y queda texto
    incompleto (objeto no cerrado o contenido claramente cortado)."""
    text = raw.strip()
    if not text:
        return True
    try:
        json.loads(_strip_fences(text))
        return False
    except json.JSONDecodeError:
        pass
    # contar llaves y corchetes sin cerrar
    opens = closes = 0
    in_str = False
    esc = False
    for ch in text:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            opens += 1
        elif ch in "}]":
            closes += 1
    return opens > closes


def _strip_fences(text: str) -> str:
    """Quita fences ```json ... ``` si están presentes."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    return s


def parse_json(raw: str) -> Any:
    """Intenta parsear JSON aunque venga envuelto en fences ```json ...```."""
    return json.loads(_strip_fences(raw))


def chat_with_retry(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    format_json: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = 1,
) -> str:
    """Llama a ``chat`` y reintenta una vez con más ``num_predict`` si el JSON
    viene truncado."""
    num_predict = DEFAULT_NUM_PREDICT
    raw = chat(prompt, model=model, host=host, format_json=format_json,
               timeout=timeout, num_predict=num_predict)
    if format_json and is_truncated_json(raw) and max_retries > 0:
        num_predict = RETRY_NUM_PREDICT
        raw = chat(prompt, model=model, host=host, format_json=format_json,
                   timeout=timeout, num_predict=num_predict)
    return raw
