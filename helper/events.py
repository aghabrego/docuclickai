"""Publicador de eventos Redis Pub/Sub para el pipeline de DocuClickAI.

Convenciones:
- Canal único ``pdf.processed`` con campo ``tipo`` (``transfer_in`` /
  ``transfer_out``) en el payload.
- Canal de error ``pdf.error``.
- Fail-hard: si Redis no responde, se propaga ``RedisPublishError`` y el
  caller debe abortar el run (exit code 4).
- Configurable vía env: ``REDIS_HOST`` (default ``localhost``),
  ``REDIS_PORT`` (default ``6379``), ``REDIS_DB`` (default ``0``).

El publisher es perezoso: la conexión se abre en la primera publicación
y se reutiliza (con ``socket_keepalive`` para detectar cortes).
"""
from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timezone
from typing import Any

try:
    import redis
    from redis.exceptions import RedisError
except ImportError:  # pragma: no cover - redis es dependencia declarada
    redis = None  # type: ignore[assignment]
    RedisError = Exception  # type: ignore[assignment, misc]


CHANNEL_PROCESSED = "pdf.processed"
CHANNEL_ERROR = "pdf.error"

log = logging.getLogger("docuclickai.events")


class RedisPublishError(RuntimeError):
    """No se pudo publicar en Redis. Fail-hard para el caller."""


class EventPublisher:
    """Cliente Redis ligero con reconexión perezosa y fail-hard.

    Cada evento incluye ``session_id`` (sid de la ejecución) para que el
    consumidor pueda correlacionar todos los eventos del mismo run.
    """

    def __init__(
        self,
        *,
        session_id: str,
        host: str | None = None,
        port: int | None = None,
        db: int | None = None,
        socket_timeout: float = 5.0,
    ) -> None:
        if redis is None:
            raise RedisPublishError(
                "El paquete 'redis' no está instalado. "
                "Ejecuta: pip install -r helper/requirements.txt"
            )
        if not session_id:
            raise ValueError("session_id es requerido")
        self._session_id = session_id
        self._host = host or os.environ.get("REDIS_HOST", "localhost")
        self._port = int(os.environ.get("REDIS_PORT", port or 6379))
        self._db = int(os.environ.get("REDIS_DB", db or 0))
        self._socket_timeout = socket_timeout
        self._client: redis.Redis | None = None

    def _connect(self) -> redis.Redis:
        if self._client is not None:
            return self._client
        self._client = redis.Redis(
            host=self._host,
            port=self._port,
            db=self._db,
            socket_timeout=self._socket_timeout,
            socket_connect_timeout=self._socket_timeout,
            socket_keepalive=True,
            decode_responses=True,
        )
        # Ping explícito: si falla aquí, el caller decide.
        try:
            self._client.ping()
        except RedisError as exc:
            self._client = None
            raise RedisPublishError(
                f"Redis no responde en {self._host}:{self._port}/{self._db}: {exc}"
            ) from exc
        return self._client

    def ping(self) -> None:
        """Fuerza la conexión + ping. Útil para fail-hard al construir el
        publisher (caller atrapa ``RedisPublishError`` y aborta con exit 4)."""
        self._connect()

    def __enter__(self) -> "EventPublisher":
        self.ping()
        return self

    def _publish(self, channel: str, payload: dict[str, Any]) -> int:
        try:
            client = self._connect()
            n = client.publish(channel, json.dumps(payload, ensure_ascii=False))
            return int(n)
        except RedisPublishError:
            raise
        except RedisError as exc:
            # Forzar reconexión en el próximo intento
            self._client = None
            raise RedisPublishError(
                f"Falló publish en '{channel}' ({self._host}:{self._port}): {exc}"
            ) from exc
        except (OSError, socket.error) as exc:
            self._client = None
            raise RedisPublishError(
                f"Error de red publicando en '{channel}': {exc}"
            ) from exc

    # --- API pública ----------------------------------------------------

    def publish_store(
        self,
        *,
        archivo: str,
        origen: str,
        anio_fiscal: int | None,
        periodo: int | None,
        tipo: str,           # "transfer_in" | "transfer_out"
        store_data: dict,    # {tienda, subtotal_tienda, transferencias[]}
    ) -> int:
        """Publica 1 evento ``pdf.processed`` por tienda."""
        if tipo not in ("transfer_in", "transfer_out"):
            raise ValueError(f"tipo inválido: {tipo!r}")
        payload = {
            "evento": "pdf.processed",
            "session_id": self._session_id,
            "archivo": archivo,
            "estado": "completado",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tipo": tipo,
            "tienda": store_data.get("tienda"),
            "origen": origen,
            "anio_fiscal": anio_fiscal,
            "periodo": periodo,
            "data": {
                "tienda": store_data.get("tienda"),
                "subtotal_tienda": store_data.get("subtotal_tienda"),
                "transferencias": store_data.get("transferencias") or [],
            },
        }
        return self._publish(CHANNEL_PROCESSED, payload)

    def publish_summary(
        self,
        *,
        archivo: str,
        origen: str,
        anio_fiscal: int | None,
        periodo: int | None,
        totales: dict,
        tiendas_in: int,
        tiendas_out: int,
        transferencias_in: int,
        transferencias_out: int,
    ) -> int:
        """Publica 1 evento ``pdf.processed.summary`` al terminar el PDF."""
        payload = {
            "evento": "pdf.processed.summary",
            "session_id": self._session_id,
            "archivo": archivo,
            "estado": "completado",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "origen": origen,
            "anio_fiscal": anio_fiscal,
            "periodo": periodo,
            "totales": totales,
            "tiendas_procesadas": {
                "transfer_in": tiendas_in,
                "transfer_out": tiendas_out,
            },
            "transferencias_procesadas": {
                "transfer_in": transferencias_in,
                "transfer_out": transferencias_out,
            },
        }
        return self._publish(CHANNEL_PROCESSED, payload)

    def publish_error(
        self,
        *,
        archivo: str,
        error: str,
        contexto: str | None = None,
    ) -> int:
        """Publica un evento ``pdf.error``."""
        payload = {
            "evento": "pdf.error",
            "session_id": self._session_id,
            "archivo": archivo,
            "estado": "fallo",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": error,
        }
        if contexto:
            payload["contexto"] = contexto
        return self._publish(CHANNEL_ERROR, payload)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None