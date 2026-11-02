"""Persistence for inbox-wake bindings and the ops log.

Hermes gives a plugin a namespaced JSON state facade (``ctx.state``) with atomic writes and a 10 MiB
quota. Bindings are small, so the whole map is rewritten on every change: simpler than a journal and
still crash-safe because each ``set`` is a complete value replacement.

Nothing secret is ever stored: bindings keep a *token reference* (a path or env name), never a token.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

BINDINGS_KEY = "bindings"
OPS_KEY = "ops"
SCHEMA_VERSION = 1


class Store:
    """Thin, defensive wrapper around ``ctx.state`` (falls back to memory when absent)."""

    def __init__(self, ctx: Any = None) -> None:
        self._state = getattr(ctx, "state", None)
        self._memory: dict[str, Any] = {}
        self._lock = threading.RLock()

    # ---- plumbing -------------------------------------------------------

    def _read(self, key: str, default: Any) -> Any:
        with self._lock:
            if self._state is not None:
                try:
                    value = self._state.get(key, default)
                except Exception as exc:  # pragma: no cover - host failure
                    logger.warning("inbox-wake: cannot read state %s: %s", key, exc)
                    value = default
            else:
                value = self._memory.get(key, default)
            if value is None:
                return default
            return value

    def _write(self, key: str, value: Any) -> None:
        with self._lock:
            if self._state is not None:
                try:
                    self._state.set(key, value)
                    return
                except Exception as exc:
                    logger.warning("inbox-wake: cannot persist state %s: %s", key, exc)
            self._memory[key] = value

    # ---- bindings -------------------------------------------------------

    def bindings(self) -> dict[str, dict]:
        raw = self._read(BINDINGS_KEY, {})
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def get_binding(self, session_key: str) -> Optional[dict]:
        return self.bindings().get(session_key)

    def put_binding(self, session_key: str, record: dict) -> None:
        with self._lock:
            data = self.bindings()
            data[session_key] = record
            data[session_key]["updatedAt"] = record.get("updatedAt")
            data[session_key]["schemaVersion"] = SCHEMA_VERSION
            self._write(BINDINGS_KEY, data)

    def drop_binding(self, session_key: str) -> Optional[dict]:
        with self._lock:
            data = self.bindings()
            record = data.pop(session_key, None)
            self._write(BINDINGS_KEY, data)
            return record

    # ---- ops log --------------------------------------------------------

    def ops(self) -> dict[str, list]:
        raw = self._read(OPS_KEY, {})
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, list)}

    def put_ops(self, session_key: str, entries: list[dict]) -> None:
        with self._lock:
            data = self.ops()
            data[session_key] = entries
            self._write(OPS_KEY, data)

    def clear_ops(self, session_key: str) -> None:
        with self._lock:
            data = self.ops()
            if data.pop(session_key, None) is not None:
                self._write(OPS_KEY, data)
