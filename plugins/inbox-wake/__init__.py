"""inbox-wake — InboxMosire wake adapter for Hermes.

Mirrors ``@mosire/dsh-inbox-mosire``: keep one SSE cursor per bound session, refresh the truth from
``/status`` before deciding, wake at most once per inbox revision, and carry a bounded summary whose
every field is treated as untrusted data. Claiming, completing and pushing stay in the InboxMosire MCP
server so the adapter can never consume work the agent has not seen.

Wiring here is deliberately thin: settings → store → manager → four tools + ``/inbox`` + four hooks.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .lib import binding as binding_lib
from .lib import commands as commands_lib
from .lib import tools as tools_lib
from .lib.config import Config
from .lib.state import Store

logger = logging.getLogger(__name__)

_manager: Optional[binding_lib.BindingManager] = None
_config: Optional[Config] = None

_EMOJI = {
    "inbox_adapter_status": "\U0001f4e5",  # inbox tray
    "inbox_adapter_bind": "\U0001f517",    # link
    "inbox_adapter_unbind": "\u26d3",      # chains
    "inbox_adapter_log": "\U0001f4dc",     # scroll
}


def _host_inject_capability(ctx: Any) -> str:
    """Which host this process is. Only a live gateway or the classic CLI REPL can deliver an
    injected wake; one-shot/worker processes load the plugin too and must not burn the shared
    backoff with attempts they can never win (``inject_message: no live gateway is available``)."""
    manager = getattr(ctx, "_manager", None)
    if manager is None:
        return "unknown"
    try:
        if bool(getattr(manager, "has_gateway_message_injector", False)):
            return "gateway"
        if getattr(manager, "_cli_ref", None) is not None:
            return "cli"
    except Exception:
        return "unknown"
    return "none"


def _make_inject(ctx: Any):
    """``inject(content, session_key) -> (ok, error)`` around the host's gateway injection."""

    def _inject(content: str, session_key: str) -> tuple[bool, str]:
        try:
            accepted = bool(ctx.inject_message(content, session_key=session_key))
        except Exception as exc:  # host rejected (not allowed, unknown key, ...)
            return False, f"{type(exc).__name__}: {exc}"
        if accepted:
            return True, ""
        return False, (
            "注入被宿主拒绝：确认 config.yaml 里 plugins.entries.inbox-wake.allow_gateway_injection: true，"
            "且该 session key 仍然存在"
        )

    return _inject


def _resolve_key(kwargs: dict[str, Any]) -> str:
    key = tools_lib.caller_session_key(kwargs)
    if key:
        return key
    if _manager is not None:
        session_id = str(kwargs.get("session_id") or kwargs.get("sessionId") or "")
        return _manager.by_session_id(session_id)
    return ""


def _on_pre_llm_call(**_kwargs: Any) -> None:
    """Mark the session busy so a wake never lands in the middle of a turn; never injects."""
    if _manager is None:
        return
    key = _resolve_key(_kwargs)
    if key:
        _manager.on_turn_start(key)
    return None


def _on_post_llm_call(**_kwargs: Any) -> None:
    """Turn finished: release a deferred wake."""
    if _manager is None:
        return
    key = _resolve_key(_kwargs)
    if key:
        _manager.on_turn_end(key)
    return None


def _on_post_tool_call(**_kwargs: Any) -> None:
    """``inbox_rest`` returns ``restSeconds`` — honour it so a snoozed inbox stays quiet."""
    if _manager is None:
        return None
    session_id = str(_kwargs.get("session_id") or "")
    _manager.on_tool_result(session_id, _kwargs.get("tool_name") or "", _kwargs.get("result"))
    return None


def _on_session_gone(**_kwargs: Any) -> None:
    """Session really went away (reset / delete / finalize): stop the worker, keep the binding."""
    if _manager is None:
        return
    key = _resolve_key(_kwargs)
    if key:
        _manager.on_session_gone(key, reason="session-ended")
    return None


def _make_session_gone(reason: str):
    """Bind the concrete lifecycle event into the log line (``session-reset`` / ``-deleted`` …)."""

    def _handler(**_kwargs: Any) -> None:
        if _manager is None:
            return None
        key = _resolve_key(_kwargs)
        if key:
            _manager.on_session_gone(key, reason=reason)
        return None

    return _handler


def register(ctx: Any) -> None:
    global _manager, _config

    config = Config.from_ctx(ctx)
    _config = config
    for warning in config.warnings:
        logger.warning("inbox-wake: %s", warning)

    store = Store(ctx)
    manager = binding_lib.BindingManager(config, store, _make_inject(ctx))
    _manager = manager

    for name, schema, handler in tools_lib.build_toolset(manager):
        try:
            ctx.register_tool(
                name=name,
                toolset=tools_lib.TOOLSET,
                schema=schema,
                handler=handler,
                description=schema["description"],
                emoji=_EMOJI.get(name, "\U0001f4e5"),
            )
        except Exception as exc:
            logger.warning("inbox-wake: cannot register tool %s: %s", name, exc)

    def _slash(raw_args: str, **kwargs: Any) -> Optional[str]:
        key = _resolve_key(kwargs)
        if not key:
            return "无法确定当前会话的 session key；请在会话内调用，或用 /inbox bind 前先发一条消息。"
        return commands_lib.handle(raw_args, manager, key)

    try:
        ctx.register_command(
            "inbox",
            handler=_slash,
            description="InboxMosire 唤醒适配器：status / bind / unbind / retry / log",
            args_hint="status | bind <descriptor.json|inboxId> [channels] | unbind | retry | log [limit]",
        )
    except Exception as exc:
        logger.warning("inbox-wake: cannot register /inbox: %s", exc)

    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    # Hermes fires ``on_session_end`` at the end of *every turn* (hermes_cli/lifecycle.py: "on_session_end
    # is per-turn and stays observer-only"), so it means "turn finished", never "session gone". Treating
    # it as the latter stopped the SSE worker after the first turn and silenced every later wake.
    ctx.register_hook("on_session_end", _on_post_llm_call)
    for _hook, _reason in (
        ("on_session_reset", "session-reset"),
        ("on_session_delete", "session-deleted"),
        ("on_session_finalize", "session-finalized"),
    ):
        try:
            ctx.register_hook(_hook, _make_session_gone(_reason))
        except Exception as exc:  # older hosts may not accept the newer hook names
            logger.warning("inbox-wake: cannot register hook %s: %s", _hook, exc)

    unload = getattr(ctx, "on_unload", None)
    if callable(unload):
        try:
            unload(manager.shutdown)
        except Exception as exc:
            logger.warning("inbox-wake: cannot register unload hook: %s", exc)

    capability = _host_inject_capability(ctx)
    setter = getattr(manager, "set_inject_capability", None)
    if callable(setter):
        try:
            # Probe, not snapshot: the gateway installs its message injector after plugin
            # discovery, so a frozen "none" here would park the only host that can deliver.
            setter(capability, probe=lambda: _host_inject_capability(ctx))
        except TypeError:  # older manager signature
            setter(capability)
    if config.enabled and capability == "none":
        logger.info(
            "inbox-wake: this process cannot deliver a wake right now (inject=%s); its workers "
            "will re-check the host on every wake attempt", capability,
        )

    resumed = 0
    if config.enabled:
        try:
            resumed = manager.resume_all()
        except Exception as exc:
            logger.warning("inbox-wake: resume failed: %s", exc)
    logger.info(
        "inbox-wake: loaded (api=%s inbox=%s mode=%s profile=%s resumed=%d inject=%s@boot)",
        config.api_url, config.inbox_id, config.wake_mode, config.wake_profile, resumed, capability,
    )
