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


def clean_block(value: Any, limit: int) -> str:
    """Like :func:`clean_text` but **keeps line breaks** (renderers need them).

    正文段是给模型读的多行文本（``[11 小明] 在吗``），压平成一行会失去逐条结构；
    这里只做三件事：统一换行、去掉制表/控制字符、把连续空行收到一行、超长截断。
    """
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = CONTROL_RE.sub(" ", text)
    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(line)
    text = "\n".join(out).strip()
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


#: Inbox 条目的 ``source`` 是**字符串地址**（iiBot 的 push 写 `source: "qq_group/<account>/<group>"`，
#: InboxMosire 的 ItemSummaryView.source 也是 String）。只放行「像地址」的字符，空间/换行/引号一律丢。
SOURCE_RE = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,256}$")


def source_ref(item: Any) -> str:
    """Normalise one item's ``source`` to a plain address string (``""`` when unusable)."""
    if not isinstance(item, Mapping):
        return ""
    source = item.get("source")
    if isinstance(source, str):
        text = source.strip()
        return text if SOURCE_RE.match(text) else ""
    if isinstance(source, Mapping):
        for key in ("address", "canonical", "id"):
            value = source.get(key)
            if isinstance(value, str):
                text = value.strip()
                if SOURCE_RE.match(text):
                    return text
    return ""


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
    ref = source_ref(item)
    if ref:
        brief["source"] = ref
    return brief


def content_parts(
    raw: Any, *, max_messages: int = 200, per_message_chars: int = 2000
) -> tuple[str, str]:
    """Split a digest ``content`` into ``(meta, timeline)``.

    **只渲染未读（``messages``）**：digest 里的 ``context`` 是更早的已读消息，Agent 需要时
    自己调 ``qq_conversation_read`` 去看，不该在唤醒里重复占用上下文（2026-11-03 真机反馈：
    一条新消息的唤醒里被补进了 7 条旧消息）。

    ``summary`` 由 iiBot 的 ``MessageSummaryBuilder`` 生成，形状已经是
    ``发送者（备注：…）：正文``，所以这里**不再补发送者**，否则名字会出现两次。

    不是我们认识的 digest 时返回 ``("", raw)``。
    """
    if not isinstance(raw, str):
        raw = str(raw or "")
    text = raw.strip()
    if not text or not text.startswith("{"):
        return "", raw
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return "", raw
    if not isinstance(payload, Mapping):
        return "", raw
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return "", raw

    rows: list[Any] = [m for m in messages if isinstance(m, Mapping)]
    if any(m.get("localSeq") is not None for m in rows):
        rows.sort(key=lambda m: (m.get("localSeq") is None, m.get("localSeq") or 0))

    def render(message: Mapping[str, Any]) -> str:
        sender = clean_text(
            message.get("senderName") or message.get("senderId") or "?", 24
        ) or "?"
        # summary 是 iiBot 生成的一整行（已含「发送者：」前缀），原样用；
        # 只有缺 summary 的旧格式才自己拼前缀，避免名字出现两次。
        if message.get("summary"):
            return clean_text(message.get("summary"), per_message_chars)
        body = message.get("text") or ""
        if not body:
            # 媒体消息：板里只有占位符（如「[图片]」），正文留在消息板上。
            body = message.get("nonTextSummary") or ""
        body = clean_text(body, per_message_chars) or "(空消息)"
        return f"{sender}：{body}"

    head: list[str] = []
    unread = payload.get("unreadCount")
    if isinstance(unread, int):
        head.append(f"未读={unread}")
    if payload.get("hasMoreUnread"):
        head.append("还有更多未读")
    reason = clean_text(payload.get("wakeReason"), 24)
    if reason:
        head.append(f"唤醒原因={reason}")

    lines: list[str] = []
    shown = 0
    for message in rows:
        if shown >= max_messages:
            lines.append(f"(还有 {len(rows) - shown} 条未渲染)")
            break
        lines.append(render(message))
        shown += 1
    newest = payload.get("toSeq")
    if newest is None:
        seqs = [m.get("localSeq") for m in rows if m.get("localSeq") is not None]
        newest = max(seqs) if seqs else None
    if newest is not None:
        lines.append(f"(本窗口 {shown} 条，最新 seq={newest})")
    return (" ".join(head), "\n".join(lines))


def content_text(
    raw: Any, *, max_messages: int = 200, per_message_chars: int = 2000
) -> str:
    """``content_parts`` 拼回一段文本（单独使用/日志用）。"""
    meta, timeline = content_parts(
        raw, max_messages=max_messages, per_message_chars=per_message_chars
    )
    return "\n".join(part for part in (meta, timeline) if part)


def item_content(item: Any, *, max_chars: int) -> Optional[dict[str, Any]]:
    """Bounded body view for the opt-in ``wakeContent`` modes."""
    if not isinstance(item, Mapping):
        return None
    item_id = item.get("itemId") or item.get("id") or ""
    if not is_safe_id(item_id):
        return None
    raw = item.get("content") or item.get("body") or item.get("text") or ""
    meta, timeline = content_parts(raw) if raw else ("", "")
    body = clean_block(timeline, max_chars)
    if not body:
        return None
    return {
        "itemId": item_id,
        "kind": clean_kind(item.get("kind")),
        "source": source_ref(item),
        "meta": clean_block(meta, 200),
        "content": body,
        "truncated": bool(timeline and len(timeline) > max_chars > 0),
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
