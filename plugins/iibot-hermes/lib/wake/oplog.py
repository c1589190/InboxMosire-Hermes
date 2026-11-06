"""Per-session ops ring buffer (``inbox_adapter_log`` / ``/inbox status`` back it)."""

from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable, Optional

from . import safe
from .state import Store

MAX_EVENTS = 200
REDACT_KEYS = ("token", "authorization", "bearer", "secret", "password")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sanitize(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        name = str(key)
        if any(marker in name.lower() for marker in REDACT_KEYS):
            out[name] = "<redacted>"
            continue
        if isinstance(value, (int, float, bool)) or value is None:
            out[name] = value
        elif isinstance(value, (list, tuple)):
            out[name] = [safe.clean_text(item, 64) for item in value][:8]
        else:
            out[name] = safe.clean_text(value, 200)
    return out


def append(store: Store, session_key: str, event: str, **fields: Any) -> dict[str, Any]:
    """Append one entry; the buffer keeps the newest :data:`MAX_EVENTS` entries."""
    entry = {"ts": _now(), "event": safe.clean_text(event, 64) or "event"}
    entry.update(_sanitize(fields))
    entries = store.ops().get(session_key, [])
    entries = (entries + [entry])[-MAX_EVENTS:]
    store.put_ops(session_key, entries)
    return entry


def read(store: Store, session_key: str, limit: int = 20) -> list[dict[str, Any]]:
    entries = store.ops().get(session_key, [])
    if not isinstance(entries, list):
        return []
    count = max(1, min(int(limit), 100))
    return [entry for entry in entries[-count:] if isinstance(entry, dict)]


def format_line(entry: dict[str, Any]) -> str:
    """One compact log line: ``<ts> <event> key=value ...``."""
    ts = safe.clean_text(entry.get("ts"), 32)
    event = safe.clean_text(entry.get("event"), 64)
    rest: Iterable[str] = (
        f"{key}={value}" for key, value in entry.items() if key not in {"ts", "event"}
    )
    tail = " ".join(rest)
    return f"{ts} {event}" + (f" {tail}" if tail else "")
