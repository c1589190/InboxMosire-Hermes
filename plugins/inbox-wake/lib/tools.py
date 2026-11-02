"""The four adapter tools (bind/unbind/status/log) — the whole agent-facing surface of this plugin.

Business operations (list / claim / renew / complete / release / push / rest / occupancy) intentionally
live in the InboxMosire **MCP server**: this plugin only manages the wake channel, exactly like the DSH
original. Wake mode ``claim`` additionally expects ``inbox_list`` + ``inbox_claim`` to be mounted, and
``status`` reports that as ``health.programMcp``.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

TOOLSET = "inbox-mosire"


def caller_session_key(kwargs: dict[str, Any]) -> str:
    """Resolve the route key of the session that is calling a tool or a slash command."""
    for name in ("session_key", "sessionKey", "HERMES_SESSION_KEY"):
        value = kwargs.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    try:
        from hermes_cli.plugin_session import session_key as _key

        return _key() or ""
    except Exception:
        return ""


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def _pick_session(args: dict[str, Any], kwargs: dict[str, Any]) -> tuple[str, bool]:
    explicit = str(args.get("sessionId") or "").strip()
    if explicit:
        return explicit, True
    return caller_session_key(kwargs), False


_SCHEMA_STATUS = {
    "name": "inbox_adapter_status",
    "description": (
        "Report this session's InboxMosire wake binding: inbox, channels, wake mode, SSE health, "
        "cursor/revision watermarks, last wake, and whether the business inbox MCP tools are mounted. "
        "Read-only; it re-reads /status so the numbers are current."
    ),
    "parameters": {"type": "object", "properties": {}},
}

_SCHEMA_BIND = {
    "name": "inbox_adapter_bind",
    "description": (
        "Bind the current session (or an explicit sessionId) to an InboxMosire inbox so new items wake "
        "this session over SSE. Needs an adapter token *reference* (file:<path> to a 0600 file, or "
        "env:<NAME>); never pass a raw token. Re-binding replaces the previous binding for that session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "inboxId": {"type": "string", "description": "Inbox id (default from settings)."},
            "adapterTokenRef": {
                "type": "string",
                "description": "file:<path> or env:<NAME> pointing at the bearer token.",
            },
            "apiBaseUrl": {"type": "string", "description": "Default http://127.0.0.1:18765"},
            "channels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Channel filter; empty means every channel of the inbox.",
            },
            "wakeMode": {"type": "string", "enum": ["claim", "view-ack"]},
            "wakeProfile": {"type": "string", "enum": ["generic", "iibot"]},
            "label": {"type": "string", "description": "Human label for this binding."},
            "sessionId": {"type": "string", "description": "Override the target session (default: caller)."},
            "descriptor": {
                "type": "object",
                "description": "Descriptor JSON with the same fields; explicit arguments win.",
            },
        },
        "required": ["inboxId"],
    },
}

_SCHEMA_UNBIND = {
    "name": "inbox_adapter_unbind",
    "description": "Stop waking this session for inbox items and release its occupancy lease.",
    "parameters": {
        "type": "object",
        "properties": {
            "sessionId": {"type": "string", "description": "Override the target session (default: caller)."}
        },
    },
}

_SCHEMA_LOG = {
    "name": "inbox_adapter_log",
    "description": "Recent adapter events for this session (SSE reconnects, wakes, occupancy, errors).",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Default 20."}
        },
    },
}


def build_toolset(manager: Any) -> list[tuple[str, dict, Callable[..., str]]]:
    """Return ``[(name, schema, handler), ...]`` ready for ``ctx.register_tool``."""

    def handle_status(args: dict[str, Any], **kwargs: Any) -> str:
        session_key, _explicit = _pick_session(args, kwargs)
        result = manager.status(session_key)
        payload = {"ok": bool(result.get("ok")), "sessionKey": session_key}
        if result.get("ok"):
            payload["binding"] = result["binding"]
        else:
            payload.update({"code": result.get("code"), "message": result.get("message")})
        return _dumps(payload)

    def handle_bind(args: dict[str, Any], **kwargs: Any) -> str:
        session_key, _explicit = _pick_session(args, kwargs)
        params = {key: value for key, value in (args or {}).items() if value not in (None, "")}
        params.pop("sessionId", None)
        result = manager.bind(session_key, params)
        payload = {"ok": bool(result.get("ok")), "sessionKey": session_key}
        if result.get("ok"):
            payload["binding"] = result.get("binding")
            payload["status"] = result.get("status")
        else:
            payload.update({"code": result.get("code"), "message": result.get("message")})
        return _dumps(payload)

    def handle_unbind(args: dict[str, Any], **kwargs: Any) -> str:
        session_key, explicit = _pick_session(args, kwargs)
        result = manager.unbind(session_key)
        payload = {"ok": bool(result.get("ok")), "sessionKey": session_key, "explicitSession": explicit}
        if not result.get("ok"):
            payload.update({"code": result.get("code"), "message": result.get("message")})
        return _dumps(payload)

    def handle_log(args: dict[str, Any], **kwargs: Any) -> str:
        session_key, _explicit = _pick_session(args, kwargs)
        try:
            limit = int(args.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        result = manager.log(session_key, max(1, min(limit, 100)))
        return _dumps({"ok": True, "sessionKey": session_key, "entries": result.get("entries") or []})

    return [
        ("inbox_adapter_status", _SCHEMA_STATUS, handle_status),
        ("inbox_adapter_bind", _SCHEMA_BIND, handle_bind),
        ("inbox_adapter_unbind", _SCHEMA_UNBIND, handle_unbind),
        ("inbox_adapter_log", _SCHEMA_LOG, handle_log),
    ]
