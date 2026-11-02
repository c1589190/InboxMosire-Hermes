"""Validation and sanitising helpers shared by the inbox-wake plugin.

Everything that reaches a wake message is untrusted: channel names and item summaries come from
whoever pushed the item. This module is the single choke point for "make it boring text".
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DEFAULT_KIND = "unknown"

# Item kinds are a small closed vocabulary in practice; anything else is folded to "unknown" so a
# producer cannot smuggle instructions into a field the model treats as trustworthy.
KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")


def is_safe_id(value: Any) -> bool:
    return isinstance(value, str) and bool(SAFE_ID_RE.fullmatch(value))


def as_channels(value: Any) -> list[str]:
    """Accept a list, a comma-separated string, or nothing; drop unsafe entries."""
    if isinstance(value, str):
        raw: Iterable[Any] = [part for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        return []
    out: list[str] = []
    for entry in raw:
        text = clean_text(entry, 64)
        if text and is_safe_id(text) and text not in out:
            out.append(text)
    return out


def clean_text(value: Any, limit: int) -> str:
    """Collapse whitespace/newlines, strip control characters, truncate to *limit* characters."""
    if value is None:
        return ""
    text = str(value)
    text = CONTROL_RE.sub(" ", text)
    text = " ".join(text.split())
    if limit > 0 and len(text) > limit:
        text = text[: max(0, limit - 1)] + "\u2026"
    return text


def clean_kind(value: Any, limit: int = 40) -> str:
    text = clean_text(value, limit).lower()
    if not text or not KIND_RE.fullmatch(text):
        return DEFAULT_KIND
    return text


def json_line(payload: Mapping[str, Any]) -> str:
    """One-line JSON with non-ASCII kept readable and no line breaks inside the string."""
    return json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))


def item_brief(item: Any, *, max_title_chars: int = 80) -> Optional[dict[str, Any]]:
    """Bounded view of one inbox item: ids and a truncated title, never the raw body."""
    if not isinstance(item, Mapping):
        return None
    item_id = item.get("itemId") or item.get("id") or ""
    if not is_safe_id(item_id):
        return None
    kind = clean_kind(item.get("kind"))
    brief: dict[str, Any] = {
        "itemId": item_id,
        "channel": clean_text(item.get("channel"), 64) or "-",
        "kind": kind,
        "title": clean_text(item.get("title") or item.get("summary"), max_title_chars) or "(无标题)",
    }
    source = item.get("source")
    if isinstance(source, Mapping) and is_safe_id(source.get("id")):
        brief["source"] = {"id": source["id"]}
    return brief


def item_content(item: Any, *, max_chars: int) -> Optional[dict[str, Any]]:
    """Bounded body view for the opt-in ``wakeContent`` modes."""
    if not isinstance(item, Mapping):
        return None
    item_id = item.get("itemId") or item.get("id") or ""
    if not is_safe_id(item_id):
        return None
    raw = item.get("content") or item.get("body") or item.get("text") or ""
    body = clean_text(raw, max_chars) if raw else ""
    if not body:
        return None
    return {
        "itemId": item_id,
        "kind": clean_kind(item.get("kind")),
        "content": body,
        "truncated": bool(raw and len(clean_text(raw, 0)) > max_chars > 0),
    }


def redact(token: str) -> str:
    """Never let a token reach a log line, a wake message, or a tool result."""
    if not token:
        return "<none>"
    if len(token) <= 8:
        return "<redacted>"
    return f"{token[:3]}<redacted>{token[-2:]}"


def coerce_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default
