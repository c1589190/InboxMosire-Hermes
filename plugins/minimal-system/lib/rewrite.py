"""Replace the host's system prompt with one constant line.

Pure functions only: given the provider kwargs of one LLM request, return a new dict whose system
prompt is ``text``. Three wire shapes are handled because ``agent._build_api_kwargs`` dispatches on
``api_mode`` (``agent/chat_completion_helpers.py:1522``):

* ``chat_completions`` (and most custom providers) — ``messages[0]`` with ``role == "system"``;
* ``anthropic_messages`` — top-level ``system`` as a string or a list of text blocks;
* ``codex_responses`` — top-level ``instructions``.

Nothing here mutates the caller's dict: the middleware contract is "return a replacement payload".
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

#: Which part of the payload carried the system prompt.
SHAPE_CHAT = "messages"
SHAPE_SYSTEM_TEXT = "system"
SHAPE_SYSTEM_BLOCKS = "system_blocks"
SHAPE_INSTRUCTIONS = "instructions"
SHAPE_NONE = "none"

_CACHE_KEY = "cache_control"


def _text_of(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, Mapping) and isinstance(block.get("text"), str):
        return block["text"]
    return ""


def rewrite_system_prompt(
    request: Mapping[str, Any], text: str
) -> tuple[dict[str, Any], str]:
    """Return ``(new_request, shape)``; ``shape`` is :data:`SHAPE_NONE` when nothing matched.

    The request is only rebuilt when the target field actually exists, so a provider that never
    sends a system prompt is left alone instead of gaining an unexpected one.
    """
    if not isinstance(request, Mapping) or not isinstance(text, str) or not text.strip():
        return dict(request) if isinstance(request, Mapping) else {}, SHAPE_NONE

    messages = request.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, Mapping) and first.get("role") == "system":
            replaced = dict(first)
            replaced["content"] = text
            new_messages = [replaced, *messages[1:]]
            out = dict(request)
            out["messages"] = new_messages
            return out, SHAPE_CHAT

    system = request.get("system")
    if isinstance(system, str):
        out = dict(request)
        out["system"] = text
        return out, SHAPE_SYSTEM_TEXT
    if isinstance(system, list) and system:
        cache_control: Optional[Any] = None
        for block in system:
            if isinstance(block, Mapping) and block.get(_CACHE_KEY):
                cache_control = block[_CACHE_KEY]
                break
        block: dict[str, Any] = {"type": "text", "text": text}
        if cache_control is not None:
            block[_CACHE_KEY] = cache_control
        out = dict(request)
        out["system"] = [block]
        return out, SHAPE_SYSTEM_BLOCKS

    if isinstance(request.get("instructions"), str):
        out = dict(request)
        out["instructions"] = text
        return out, SHAPE_INSTRUCTIONS

    return dict(request), SHAPE_NONE


def current_system_prompt(request: Mapping[str, Any]) -> str:
    """The system prompt this request would send today (for logging/op-log trace)."""
    messages = request.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, Mapping) and first.get("role") == "system":
            return _text_of(first.get("content"))
    system = request.get("system")
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(_text_of(block) for block in system if _text_of(block))
    instructions = request.get("instructions")
    return instructions if isinstance(instructions, str) else ""
