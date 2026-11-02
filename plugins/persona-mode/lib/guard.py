"""Outbound-tool guard: suffix matching that also covers MCP-prefixed tool names."""
from __future__ import annotations

from typing import Iterable, Optional


def normalize(tool_name: str) -> str:
    return str(tool_name or "").strip().lower()


def matches(tool_name: str, patterns: Optional[Iterable[str]]) -> bool:
    """True when *tool_name* ends with any pattern.

    ``qq_conversation_send`` matches ``qq_conversation_send`` and
    ``mcp__iibot__qq_conversation_send``; ``conversation_send`` matches ``qq_conversation_send`` too,
    which is the same over-match the DSH adapter accepts (over-blocking is the safe direction for
    outbound chat actions, and the config list is operator-controlled).
    """
    name = normalize(tool_name)
    if not name:
        return False
    for raw in patterns or ():
        pattern = normalize(raw)
        if not pattern:
            continue
        if name == pattern or name.endswith(pattern) or name.endswith("__" + pattern):
            return True
    return False


def blocked_message(tool_name: str, path: str) -> str:
    return (
        f"人设扮演模式未启动：人设文件缺失（{path}），已阻止调用 {tool_name}。"
        "请先让人设文件就位，或在操作者明确允许后由操作者执行该外发动作。"
    )
