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


def content_text(
    raw: Any, *, max_messages: int = 200, per_message_chars: int = 2000
) -> str:
    """Render an Inbox item's ``content`` into readable text for the wake message.

    iiBot 的 digest 把整份 ``InboxDigest`` 序列化成 JSON 放进 content。目标是**一次给够**：
    把待处理窗口里的每条消息都渲染出来（顺带上下文窗口），这样 Agent 不用再补一次
    ``qq_conversation_read``。不是 JSON、或不是这个形状时原样返回。
    """
    if not isinstance(raw, str):
        raw = str(raw or "")
    text = raw.strip()
    if not text or not text.startswith("{"):
        return raw
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return raw
    if not isinstance(payload, Mapping):
        return raw
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return raw

    def render(message: Any) -> str:
        if not isinstance(message, Mapping):
            return ""
        seq = message.get("localSeq")
        sender = clean_text(
            message.get("senderName") or message.get("senderId") or "?", 32
        ) or "?"
        who = f"{seq} {sender}" if seq is not None else sender
        body = (
            message.get("summary")
            or message.get("text")
            or message.get("nonTextSummary")
            or ""
        )
        body = clean_text(body, per_message_chars)
        if not body:
            body = "(无正文，可能只有图片/文件；用 qq_media_get 取)"
        note = clean_text(message.get("note"), 80)
        if note:
            body = f"{body}（备注：{note}）"
        return f"[{who}] {body}"

    head: list[str] = []
    source = clean_text(payload.get("source") if isinstance(payload.get("source"), str) else "", 120)
    if source:
        head.append(f"来源={source}")
    unread = payload.get("unreadCount")
    if isinstance(unread, int):
        head.append(f"未读={unread}")
    if payload.get("hasMoreUnread"):
        head.append("还有更多未读")
    reason = clean_text(payload.get("wakeReason"), 24)
    if reason:
        head.append(f"唤醒原因={reason}")
    lines: list[str] = []
    if head:
        lines.append(" ".join(head))
    lines.append("--- 待处理 ---")
    shown = 0
    for message in messages:
        if shown >= max_messages:
            lines.append(f"(还有 {len(messages) - shown} 条未渲染)")
            break
        line = render(message)
        if not line:
            continue
        lines.append(line)
        shown += 1
    context = payload.get("context")
    if isinstance(context, list) and context:
        lines.append("--- 上文 ---")
        for message in context[-max_messages:]:
            line = render(message)
            if line:
                lines.append(line)
    return "\n".join(lines)


def item_content(item: Any, *, max_chars: int) -> Optional[dict[str, Any]]:
    """Bounded body view for the opt-in ``wakeContent`` modes."""
    if not isinstance(item, Mapping):
        return None
    item_id = item.get("itemId") or item.get("id") or ""
    if not is_safe_id(item_id):
        return None
    raw = item.get("content") or item.get("body") or item.get("text") or ""
    rendered = content_text(raw) if raw else ""
    body = clean_block(rendered, max_chars)
    if not body:
        return None
    return {
        "itemId": item_id,
        "kind": clean_kind(item.get("kind")),
        "source": source_ref(item),
        "content": body,
        "truncated": bool(rendered and len(rendered) > max_chars > 0),
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
