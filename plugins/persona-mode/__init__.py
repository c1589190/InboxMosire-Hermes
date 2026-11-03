"""persona-mode — inject an operator-authored persona contract into every Hermes session.

Two paths, mirroring ``@mosire/dsh-persona-mode``:

* the contract body as a per-turn user-message injection via ``pre_llm_call`` — re-anchored whenever
  the conversation no longer carries the current body (compaction, file edit);
* the two-sentence role line, placed by ``rolePlacement``: ``injection`` (default — rides inside the
  same injected message, so the host system prompt can be reduced to one line by ``minimal-system``)
  or ``system`` (the DSH layout — a system prompt section via ``register_system_prompt_section``).

While ``PERSONA.md`` is missing, outbound chat tools are blocked by ``pre_tool_call`` (fail closed).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from .lib import guard
from .lib import persona as persona_lib

logger = logging.getLogger(__name__)

ROLE_SECTION_ID = "persona-mode-role"
ROLE_TEXT = (
    "你在扮演 <persona-contract> 定义的角色。"
    "在调用工具进行聊天发言时，请遵循 <persona-contract> 中的相关规则。"
)

#: 角色行放哪：``system``（DSH 原版，系统提示词里两句）或 ``injection``
#: （系统提示词交给 minimal-system 之类压成一句，角色行随契约走首条注入）。
DEFAULT_ROLE_PLACEMENT = "injection"
ROLE_PLACEMENTS = ("injection", "system")

DEFAULT_PERSONA_FILE = "PERSONA.md"
DEFAULT_MAX_CHARS = 4000
DEFAULT_OUTBOUND_TOOLS = ("qq_conversation_send", "conversation_send")

_config: dict[str, Any] = {
    "enabled": True,
    "personaFile": DEFAULT_PERSONA_FILE,
    "rolePlacement": DEFAULT_ROLE_PLACEMENT,
    "maxChars": DEFAULT_MAX_CHARS,
    "outboundGuard": True,
    "outboundTools": list(DEFAULT_OUTBOUND_TOOLS),
    "denyTools": [],
}
_readers: dict[str, persona_lib.PersonaReader] = {}
_lock = threading.Lock()


def _session_cwd() -> str:
    try:
        from hermes_cli.plugin_session import session_cwd as _cwd

        return _cwd()
    except Exception:  # pragma: no cover - host import is optional in tests
        return ""


def _reader(path: str) -> persona_lib.PersonaReader:
    with _lock:
        reader = _readers.get(path)
        if reader is None:
            reader = persona_lib.PersonaReader(path)
            _readers[path] = reader
        return reader


def _persona_path() -> str:
    return persona_lib.resolve_path(str(_config["personaFile"]), _session_cwd())


def _as_list(value: Any, fallback: tuple[str, ...] = ()) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
        return [item for item in items if item]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return list(fallback)


def _load_config(ctx: Any) -> None:
    """Read settings once at load time (a settings change needs a gateway restart)."""
    get = getattr(ctx, "get_config", None)
    if not callable(get):
        return
    try:
        _config["enabled"] = bool(get("enabled", True))
        _config["personaFile"] = str(get("personaFile", DEFAULT_PERSONA_FILE) or DEFAULT_PERSONA_FILE)
        placement = str(get("rolePlacement", DEFAULT_ROLE_PLACEMENT) or DEFAULT_ROLE_PLACEMENT).strip().lower()
        if placement not in ROLE_PLACEMENTS:
            logger.warning("persona-mode: unknown rolePlacement %r, using %s", placement, DEFAULT_ROLE_PLACEMENT)
            placement = DEFAULT_ROLE_PLACEMENT
        _config["rolePlacement"] = placement
        try:
            _config["maxChars"] = max(0, int(get("maxChars", DEFAULT_MAX_CHARS)))
        except (TypeError, ValueError):
            _config["maxChars"] = DEFAULT_MAX_CHARS
        _config["outboundGuard"] = bool(get("outboundGuard", True))
        _config["outboundTools"] = _as_list(get("outboundTools"), DEFAULT_OUTBOUND_TOOLS)
        _config["denyTools"] = _as_list(get("denyTools"), ())
    except Exception as exc:  # never let config trouble break the turn
        logger.warning("persona-mode: cannot read settings, using defaults: %s", exc)


def _on_pre_llm_call(**kwargs: Any) -> Optional[dict[str, Any]]:
    """Inject the contract when this conversation does not already carry the current body."""
    if not _config["enabled"]:
        return None
    try:
        history = kwargs.get("conversation_history") or []
        path = _persona_path()
        body = _reader(path).read()
        if body is None:
            if persona_lib.has_missing_notice(history):
                return None
            logger.debug("persona-mode: persona file missing at %s", path)
            return {"context": persona_lib.build_missing(path)}
        digest = persona_lib.contract_hash(body)
        if persona_lib.has_contract(history, digest):
            return None
        logger.debug("persona-mode: injecting contract hash=%s from %s", digest, path)
        role_line = ROLE_TEXT if _config.get("rolePlacement") == "injection" else ""
        return {
            "context": persona_lib.build_contract(
                body, int(_config["maxChars"]), role_line=role_line
            )
        }
    except Exception as exc:  # pre_llm_call must never break the turn
        logger.warning("persona-mode: pre_llm_call failed: %s", exc)
        return None


def _on_pre_tool_call(tool_name: str = "", **_: Any) -> Optional[dict[str, Any]]:
    """Narrow tools when configured; block outbound chat tools while the persona is missing."""
    try:
        if guard.matches(tool_name, _config["denyTools"]):
            return {
                "action": "block",
                "message": f"人设模式下 {tool_name} 不在允许的工具集内（persona-mode denyTools）。",
            }
        if not _config["outboundGuard"]:
            return None
        if not guard.matches(tool_name, _config["outboundTools"]):
            return None
        path = _persona_path()
        if _reader(path).read() is not None:
            return None
        return {"action": "block", "message": guard.blocked_message(tool_name, path)}
    except Exception as exc:  # pre_tool_call failing closed would block every tool; stay out of the way
        logger.warning("persona-mode: pre_tool_call guard failed: %s", exc)
        return None


def register(ctx: Any) -> None:
    _load_config(ctx)
    if _config.get("rolePlacement") == "system":
        try:
            ctx.register_system_prompt_section(
                ROLE_SECTION_ID, ROLE_TEXT, position="after_memory", max_chars=200
            )
        except Exception as exc:
            logger.warning("persona-mode: system prompt section unavailable: %s", exc)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    logger.info(
        "persona-mode: loaded (personaFile=%s, rolePlacement=%s, maxChars=%s, outboundGuard=%s)",
        _config["personaFile"], _config["rolePlacement"], _config["maxChars"], _config["outboundGuard"],
    )
