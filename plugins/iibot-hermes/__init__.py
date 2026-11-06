"""iibot-hermes — iiBot 在 Hermes 上的唯一插件（唤醒 + 极简 system + 人设）。

1.0.0 把三个插件合并成一个，宿主侧只装一次（`docs/superpowers/specs/2026-11-06-plugin-consolidation.md`）：

* ``lib/wake/``    ← ``plugins/inbox-wake`` 0.3.1：InboxMosire 唤醒适配器（SSE 游标、绑定、
  bounded summary、`inbox.push` 逐字注入 + 立即 ack、4 个工具 + ``/inbox``）。
* ``lib/prompt/``  ← ``plugins/minimal-system`` 0.2.1：用一句常量/操作者的 md 文件替换出站
  system prompt（公开 ``llm_request`` 中间件）。
* ``lib/persona/`` ← ``plugins/persona-mode`` 0.2.0：角色行 + ``PERSONA.md`` 合成一条
  ``pre_llm_call`` 注入（hash 去重、压缩后重锚），缺文件时封禁外发工具。
* ``lib/vision/``  ← 0.4.0-fix1 新增：用 ``llm_request`` 中间件把 tool 消息里的图片搬进
  ``role="user"`` 消息（OpenAI/DeepSeek 的协议只认 user 里的图），默认**开着**，无图时是 no-op。

**对外契约与 0.x 逐字一致**：工具名/toolset、``/inbox``、hook 语义、``inbox.push`` 正文、ack、
首条引导、``pushMode``/``pushGraceMs`` 全部照旧；变的是"装在哪"和"设置在哪一段"。

设置（``plugins.entries.iibot-hermes.settings``，旧命名空间只读回落，见 ``lib/settings.py``）::

    inbox:   {enabled, apiUrl, inboxId, adapterTokenFile, wakeMode, wakeProfile,
              pushMode, pushGraceMs, firstRunGuide, firstRunGuideText, autoBind,
              debounceMs, pollSeconds, sseReadTimeoutSeconds, wakeContentMaxChars,
              wakeContentTotalChars, summaryMaxItems, ...}
    prompt:  {enabled, systemPrompt, systemPromptFile, logRewrite}
    persona: {enabled, personaFile, maxChars, outboundGuard, outboundTools, denyTools}
    vision:  {enabled, maxImages, placeholder, logHoist}

``prompt.enabled`` / ``persona.enabled`` 默认 **false**：

* 宿主的 ``agent.system_prompt_override``（``patches/0001-…``）与 ``prompt`` 的中间件是同一目的的
  两种实现，同时开会让两处互相覆盖 —— 操作者二选一。
* D-009 已取消人设注入；模块保留，打开后行为与 persona-mode 0.2.0 一致。

``vision.enabled`` 默认 **true**：它只改「图在哪种消息里」，无图时逐字节不动请求；与宿主补丁
``hoist_tool_result_images`` 并存时是 no-op（图已经在 user 行里）。关掉它的理由只有「我不想要插件
碰我的 payload」。

``allow_gateway_injection`` 仍在 ``plugins.entries.iibot-hermes`` 一级（不在 ``settings`` 里），
迁移脚本负责从旧位置搬过来；宿主只读本插件自己的 entry，所以插件在启动时对仍留在旧位置的值
打一条 WARNING。

线程/生命周期：所有副作用都在 ``register(ctx)`` 里；``lib/`` 导入零副作用；token 只在
``lib/wake/client.py`` 按需读取，永不进日志/状态/工具返回。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from .lib import settings as settings_lib
from .lib.persona import guard as guard_lib
from .lib.persona import persona as persona_lib
from .lib.prompt import prompt_file, rewrite
from .lib.vision import hoist as vision_hoist
from .lib.wake import binding as binding_lib
from .lib.wake import commands as commands_lib
from .lib.wake import tools as tools_lib
from .lib.wake.config import Config
from .lib.wake.state import Store

logger = logging.getLogger(__name__)

__version__ = "0.4.0-fix1"


def _flag(value: Any, default: bool) -> bool:
    """YAML/CLI 都可能给出 "false"/"off"/0/None —— 按字面意思理解，而不是 ``bool(str)``。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


# --------------------------------------------------------------------------------------
# wake（原 plugins/inbox-wake）
# --------------------------------------------------------------------------------------

_manager: Optional[binding_lib.BindingManager] = None
_wake_config: Optional[Config] = None

_EMOJI = {
    "inbox_adapter_status": "\U0001f4e5",  # inbox tray
    "inbox_adapter_bind": "\U0001f517",    # link
    "inbox_adapter_unbind": "\u26d3",      # chains
    "inbox_adapter_log": "\U0001f4dc",     # scroll
}


def _host_inject_capability(ctx: Any) -> str:
    """Which host this process is — asked again on every wake, never cached at register time.

    ``hermes_cli/plugins_injection.py`` routes a keyed wake in this order: CLI queue (only when
    ``_cli_ref`` is set, which the CLI front-ends never do — PLG-7), the TUI/desktop host
    (``has_tui_message_injector``, accepts only sessions it owns), then the messaging gateway
    (``has_gateway_message_injector``). One-shot / worker processes load the plugin too and own
    none of these; they must not burn the shared backoff with attempts they can never win.
    """
    manager = getattr(ctx, "_manager", None)
    if manager is None:
        return "unknown"
    try:
        if bool(getattr(manager, "has_tui_message_injector", False)):
            return "tui"
        if bool(getattr(manager, "has_gateway_message_injector", False)):
            return "gateway"
        if getattr(manager, "_cli_ref", None) is not None:
            return "cli"
    except Exception:
        return "unknown"
    return "none"


def _make_inject(ctx: Any):
    """``inject(content, session_key, media_urls=None) -> (ok, error)`` around the host injector.

    ``media_urls`` 只在 ``inbox.push`` 带来**本地绝对路径**附件时才传（宿主
    ``hermes_cli/plugins.py:588 inject_message(..., media_urls=..., media_types=...)`` 自己校验
    「绝对路径 + 存在」，不为 None 时也不下载远端 URL）。主机版本更旧、不接受这个关键字时降级为
    纯文本注入并只 warning 一次——唤醒/投递本身不能因为附件而失败。
    """
    warned_media = False

    def _inject(
        content: str, session_key: str, media_urls: Optional[list[str]] = None
    ) -> tuple[bool, str]:
        nonlocal warned_media
        kwargs: dict[str, Any] = {"session_key": session_key}
        if media_urls:
            kwargs["media_urls"] = list(media_urls)
        try:
            accepted = bool(ctx.inject_message(content, **kwargs))
        except TypeError as exc:
            if not media_urls:
                return False, f"{type(exc).__name__}: {exc}"
            if not warned_media:
                warned_media = True
                logger.warning(
                    "inbox-wake: 宿主 inject_message 不接受 media_urls（%s），附件一律跳过、只注入正文",
                    exc,
                )
            return _inject(content, session_key)
        except Exception as exc:  # host rejected (not allowed, unknown key, ...)
            return False, f"{type(exc).__name__}: {exc}"
        if accepted:
            return True, ""
        return False, (
            "注入被宿主拒绝：确认 config.yaml 里 plugins.entries.iibot-hermes.allow_gateway_injection: true，"
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


def _register_wake(ctx: Any) -> Config:
    """tools + ``/inbox`` + the seven hooks + unload; returns the effective settings."""
    global _manager, _wake_config

    config = Config.from_ctx(ctx)
    _wake_config = config
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
    return config


# --------------------------------------------------------------------------------------
# prompt（原 plugins/minimal-system）
# --------------------------------------------------------------------------------------

#: DSH「极简模式」的默认身份（`dsh-persona-mode/README.md` 的 subagent 一节）。
DEFAULT_SYSTEM_PROMPT = "You are a helpful software engineer assistant."

_prompt_config: dict[str, Any] = {
    "enabled": False,  # 与宿主的 agent.system_prompt_override 二选一，默认交给宿主
    "systemPrompt": DEFAULT_SYSTEM_PROMPT,
    "systemPromptFile": "",
    "logRewrite": False,
}
_prompt_readers: dict[str, prompt_file.PromptReader] = {}
_prompt_lock = threading.Lock()


def _prompt_reader(path: str) -> prompt_file.PromptReader:
    with _prompt_lock:
        reader = _prompt_readers.get(path)
        if reader is None:
            reader = prompt_file.PromptReader(path)
            _prompt_readers[path] = reader
        return reader


def _prompt_text() -> tuple[str, str]:
    """The text to send, and where it came from (``file`` / ``config`` / ``default``)."""
    configured = str(_prompt_config["systemPromptFile"] or "").strip()
    if configured:
        text = _prompt_reader(prompt_file.resolve_path(configured)).read()
        if text:
            return text, "file"
    inline = str(_prompt_config["systemPrompt"] or "").strip()
    return (inline, "config") if inline else (DEFAULT_SYSTEM_PROMPT, "default")


def _prompt_load_config(ctx: Any) -> None:
    try:
        section = settings_lib.load_section(ctx, "prompt")
    except Exception as exc:  # config trouble must never break a request
        logger.warning("minimal-system: cannot read settings, using defaults: %s", exc)
        section = {}
    _prompt_config["enabled"] = _flag(section.get("enabled"), False)
    # Every load re-derives both prompts, so a re-register in the same process
    # (tests, `hermes plugins reload`) never inherits the previous settings.
    _prompt_config["systemPrompt"] = str(section.get("systemPrompt", DEFAULT_SYSTEM_PROMPT) or "").strip()
    # 留空就落到 Hermes 自己的目录（$HERMES_HOME/SYSTEM.md），配置文件不用跟着仓库路径漂移
    _prompt_config["systemPromptFile"] = (
        str(section.get("systemPromptFile", "") or "").strip() or prompt_file.default_prompt_path()
    )
    _prompt_config["logRewrite"] = _flag(section.get("logRewrite"), False)


def _prompt_on_llm_request(**kwargs: Any) -> Optional[dict[str, Any]]:
    """Replace the system prompt of one outbound request; ``None`` leaves it untouched."""
    try:
        if not _prompt_config["enabled"]:
            return None
        request = kwargs.get("request")
        if not isinstance(request, dict):
            return None
        text, source = _prompt_text()
        new_request, shape = rewrite.rewrite_system_prompt(request, text)
        if shape == rewrite.SHAPE_NONE:
            logger.debug("minimal-system: no system prompt slot found (api_mode=%s)", kwargs.get("api_mode"))
            return None
        if new_request == request:
            return None
        if _prompt_config["logRewrite"]:
            before = rewrite.current_system_prompt(request)
            logger.info(
                "minimal-system: system %d chars -> %d chars (shape=%s, source=%s, api_mode=%s)",
                len(before), len(text), shape, source, kwargs.get("api_mode"),
            )
        return {"request": new_request, "source": "minimal-system", "reason": f"system:{shape}"}
    except Exception as exc:  # a middleware bug must not take the turn down
        logger.warning("minimal-system: rewrite failed, sending the request unmodified: %s", exc)
        return None


def _register_prompt(ctx: Any) -> None:
    """``llm_request`` 只在 ``prompt.enabled`` 为真时挂——默认不接管宿主的 system prompt。"""
    _prompt_load_config(ctx)
    if not _prompt_config["enabled"]:
        logger.info(
            "minimal-system: disabled (prompt.enabled=false)；system prompt 交给宿主的 "
            "agent.system_prompt_override（见 docs/HOST-PATCH.md）",
        )
        return
    try:
        ctx.register_middleware("llm_request", _prompt_on_llm_request)
    except Exception as exc:  # an old host without request middleware: say so, do not crash the loader
        logger.warning("minimal-system: llm_request middleware unavailable: %s", exc)
        return
    text, source = _prompt_text()
    logger.info(
        "minimal-system: loaded (enabled=%s, %d chars, source=%s, file=%s)",
        _prompt_config["enabled"], len(text), source, _prompt_config["systemPromptFile"] or "-",
    )


# --------------------------------------------------------------------------------------
# vision（0.4.0-fix1）：把非 user 消息里的图片搬进 user 消息
# --------------------------------------------------------------------------------------

_vision_config: dict[str, Any] = {
    "enabled": True,
    "maxImages": vision_hoist.MAX_IMAGES,
    "placeholder": vision_hoist.DEFAULT_PLACEHOLDER,
    "logHoist": False,
}


def _vision_load_config(ctx: Any) -> None:
    """读 ``settings.vision``；读不到就用默认值（默认开着，理由见 docs/DESIGN.md 第 6 节）。"""
    try:
        section = settings_lib.load_section(ctx, "vision")
    except Exception as exc:  # never let config trouble break registration
        logger.warning("iibot-hermes vision: cannot read settings, using defaults: %s", exc)
        section = {}
    _vision_config["enabled"] = _flag(section.get("enabled"), True)
    try:
        max_images = int(section.get("maxImages", vision_hoist.MAX_IMAGES))
    except (TypeError, ValueError):
        max_images = vision_hoist.MAX_IMAGES
    _vision_config["maxImages"] = max(1, min(16, max_images))
    placeholder = str(section.get("placeholder") or "").strip()
    _vision_config["placeholder"] = placeholder or vision_hoist.DEFAULT_PLACEHOLDER
    _vision_config["logHoist"] = _flag(section.get("logHoist"), False)


def _vision_on_llm_request(**kwargs: Any) -> Optional[dict[str, Any]]:
    """Move images out of non-user roles of one outbound request; ``None`` leaves it untouched.

    无图 → 返回 ``None``（宿主请求逐字节不变）；有图 → 返回替换后的 payload。任何异常都
    fail-open：宁可让这一次请求按原样发出去，也不能让中间件把一轮对话弄挂。
    """
    try:
        if not _vision_config["enabled"]:
            return None
        result = vision_hoist.hoist_images_to_user(
            kwargs.get("request"),
            max_images=int(_vision_config["maxImages"]),
            placeholder=str(_vision_config["placeholder"]),
        )
        if not result.changed:
            return None
        if _vision_config["logHoist"]:
            logger.info("iibot-hermes vision: %s (api_mode=%s)", result.reason(), kwargs.get("api_mode"))
        else:
            logger.debug("iibot-hermes vision: %s", result.reason())
        if result.dropped:  # 丢弃是降级，必须留痕（AGENTS.md）
            logger.warning(
                "iibot-hermes vision: dropped %d image(s) above vision.maxImages=%s "
                "(kept the %d most recent): 调大上限或让模型分次看图",
                result.dropped, _vision_config["maxImages"], result.moved,
            )
        offenders = vision_hoist.images_outside_user(result.request)
        if offenders:  # 已经尽力；仍然发出去，但把位置写进日志便于排查
            logger.warning(
                "iibot-hermes vision: images still outside user messages after hoist: %s",
                "; ".join(offenders[:4]),
            )
        return {
            "request": result.request,
            "source": "iibot-hermes-vision",
            "reason": result.reason(),
        }
    except Exception as exc:  # a middleware bug must not take the turn down
        logger.warning(
            "iibot-hermes vision: hoist failed, sending the request unmodified: %s", exc
        )
        return None


def _register_vision(ctx: Any) -> None:
    """默认挂上：无图时是 no-op，有图时把 payload 改回协议认可的形状。"""
    _vision_load_config(ctx)
    if not _vision_config["enabled"]:
        logger.info("iibot-hermes vision: disabled (vision.enabled=false)")
        return
    try:
        ctx.register_middleware("llm_request", _vision_on_llm_request)
    except Exception as exc:  # an old host without request middleware: say so, never crash
        logger.warning("iibot-hermes vision: llm_request middleware unavailable: %s", exc)
        return
    logger.info(
        "iibot-hermes vision: loaded (maxImages=%s, placeholder=%s, logHoist=%s)",
        _vision_config["maxImages"],
        "custom" if _vision_config["placeholder"] != vision_hoist.DEFAULT_PLACEHOLDER else "default",
        bool(_vision_config["logHoist"]),
    )


# --------------------------------------------------------------------------------------
# persona（原 plugins/persona-mode）
# --------------------------------------------------------------------------------------

ROLE_SECTION_ID = "persona-mode-role"
ROLE_TEXT = (
    "你在扮演 <persona-contract> 定义的角色。"
    "在调用工具进行聊天发言时，请遵循 <persona-contract> 中的相关规则。"
)

#: 角色行依赖「极简 system」：没有它，宿主仍发一大段提示词（角色行只在注入里）。
REQUIRED_COMPANION = "minimal-system"

DEFAULT_PERSONA_FILE = "PERSONA.md"
DEFAULT_MAX_CHARS = 4000
DEFAULT_OUTBOUND_TOOLS = ("qq_conversation_send", "conversation_send")

_persona_config: dict[str, Any] = {
    "enabled": False,  # D-009 取消人设注入；打开后行为与 persona-mode 0.2.0 一致
    "personaFile": DEFAULT_PERSONA_FILE,
    "maxChars": DEFAULT_MAX_CHARS,
    "outboundGuard": True,
    "outboundTools": list(DEFAULT_OUTBOUND_TOOLS),
    "denyTools": [],
}
_persona_readers: dict[str, persona_lib.PersonaReader] = {}
_persona_lock = threading.Lock()


def _session_cwd() -> str:
    try:
        from hermes_cli.plugin_session import session_cwd as _cwd

        return _cwd()
    except Exception:  # pragma: no cover - host import is optional in tests
        return ""


def _persona_reader(path: str) -> persona_lib.PersonaReader:
    with _persona_lock:
        reader = _persona_readers.get(path)
        if reader is None:
            reader = persona_lib.PersonaReader(path)
            _persona_readers[path] = reader
        return reader


def _persona_path() -> str:
    return persona_lib.resolve_path(str(_persona_config["personaFile"]), _session_cwd())


def _as_list(value: Any, fallback: tuple[str, ...] = ()) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
        return [item for item in items if item]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return list(fallback)


def _persona_load_config(ctx: Any) -> None:
    """Read settings once at load time (a settings change needs a gateway restart)."""
    try:
        section = settings_lib.load_section(ctx, "persona")
    except Exception as exc:  # never let config trouble break the turn
        logger.warning("persona-mode: cannot read settings, using defaults: %s", exc)
        section = {}
    _persona_config["enabled"] = _flag(section.get("enabled"), False)
    _persona_config["personaFile"] = str(section.get("personaFile", DEFAULT_PERSONA_FILE) or DEFAULT_PERSONA_FILE)
    try:
        _persona_config["maxChars"] = max(0, int(section.get("maxChars", DEFAULT_MAX_CHARS)))
    except (TypeError, ValueError):
        _persona_config["maxChars"] = DEFAULT_MAX_CHARS
    _persona_config["outboundGuard"] = _flag(section.get("outboundGuard"), True)
    _persona_config["outboundTools"] = _as_list(section.get("outboundTools"), DEFAULT_OUTBOUND_TOOLS)
    _persona_config["denyTools"] = _as_list(section.get("denyTools"), ())


def _persona_pre_llm_call(**kwargs: Any) -> Optional[dict[str, Any]]:
    """Inject the contract when this conversation does not already carry the current body."""
    if not _persona_config["enabled"]:
        return None
    try:
        history = kwargs.get("conversation_history") or []
        path = _persona_path()
        body = _persona_reader(path).read()
        if body is None:
            if persona_lib.has_missing_notice(history):
                return None
            logger.debug("persona-mode: persona file missing at %s", path)
            return {"context": persona_lib.build_missing(path)}
        digest = persona_lib.contract_hash(body)
        if persona_lib.has_contract(history, digest):
            return None
        logger.debug("persona-mode: injecting contract hash=%s from %s", digest, path)
        return {
            "context": persona_lib.build_contract(
                body, int(_persona_config["maxChars"]), role_line=ROLE_TEXT
            )
        }
    except Exception as exc:  # pre_llm_call must never break the turn
        logger.warning("persona-mode: pre_llm_call failed: %s", exc)
        return None


def _persona_pre_tool_call(tool_name: str = "", **_: Any) -> Optional[dict[str, Any]]:
    """Narrow tools when configured; block outbound chat tools while the persona is missing."""
    try:
        if guard_lib.matches(tool_name, _persona_config["denyTools"]):
            return {
                "action": "block",
                "message": f"人设模式下 {tool_name} 不在允许的工具集内（persona-mode denyTools）。",
            }
        if not _persona_config["outboundGuard"]:
            return None
        if not guard_lib.matches(tool_name, _persona_config["outboundTools"]):
            return None
        path = _persona_path()
        if _persona_reader(path).read() is not None:
            return None
        return {"action": "block", "message": guard_lib.blocked_message(tool_name, path)}
    except Exception as exc:  # pre_tool_call failing closed would block every tool; stay out of the way
        logger.warning("persona-mode: pre_tool_call guard failed: %s", exc)
        return None


def _warn_missing_companion(ctx: Any) -> None:
    """角色行依赖极简 system：``prompt.enabled=false`` 时宿主仍发一大段提示词。

    0.x 里这是"另一个插件没装"，合并后是"同一插件里 prompt 段没开"——后者由操作者二选一
    （``prompt.enabled`` 或宿主的 ``agent.system_prompt_override``）。
    """
    get_own = getattr(ctx, "get_config", None)
    enabled = _prompt_config["enabled"]
    if callable(get_own) and not enabled:  # 该进程还没跑过 prompt 段（单独测 persona 时）
        try:
            section = settings_lib.load_section(ctx, "prompt")
            enabled = _flag(section.get("enabled"), False)
        except Exception as exc:  # probing must never break registration
            logger.debug("persona-mode: prompt settings probe failed: %s", exc)
    if enabled:
        return
    logger.warning(
        "persona-mode: prompt.enabled=false（或宿主用 agent.system_prompt_override）——角色行仍在注入里，"
        "但 system prompt 不会变成一句常量；要极简提示词请设 "
        "plugins.entries.iibot-hermes.settings.prompt.enabled: true",
    )


def _register_persona(ctx: Any) -> None:
    """两个 hook 只在 ``persona.enabled`` 为真时注册——默认不注入人设（D-009）。"""
    _persona_load_config(ctx)
    if not _persona_config["enabled"]:
        logger.info("persona-mode: disabled (persona.enabled=false)")
        return
    _warn_missing_companion(ctx)
    ctx.register_hook("pre_llm_call", _persona_pre_llm_call)
    ctx.register_hook("pre_tool_call", _persona_pre_tool_call)
    logger.info(
        "persona-mode: loaded (personaFile=%s, maxChars=%s, outboundGuard=%s)",
        _persona_config["personaFile"], _persona_config["maxChars"], _persona_config["outboundGuard"],
    )


# --------------------------------------------------------------------------------------
# register
# --------------------------------------------------------------------------------------


def _warn_legacy_gateway_flag(ctx: Any) -> None:
    """``allow_gateway_injection`` 是一级 entry 键；宿主换 id 后只读新位置，旧值会静默失效。"""
    try:
        if settings_lib.legacy_gateway_flag(ctx) and not settings_lib.own_gateway_flag(ctx):
            logger.warning(
                "iibot-hermes: plugins.entries.inbox-wake.allow_gateway_injection=true 不会生效——"
                "宿主只读本插件的 entry；请改设 plugins.entries.iibot-hermes.allow_gateway_injection: true",
            )
    except Exception as exc:  # pragma: no cover - never break registration on a probe
        logger.debug("iibot-hermes: legacy gateway flag probe failed: %s", exc)


def register(ctx: Any) -> None:
    """Hooks + middleware + 4 tools + ``/inbox``, all in one plugin.

    顺序固定：wake 先注册（``pre_llm_call`` 里唤醒适配器永远第一），prompt 中间件按开关挂，
    vision 中间件默认挂，persona 两个 hook 按开关挂。两个 ``llm_request`` 中间件互不重叠：
    prompt 只碰 ``messages[0].role=="system"`` 的文本，vision 只碰非 user 消息里的图片块。
    """
    config = _register_wake(ctx)
    _register_prompt(ctx)
    _register_vision(ctx)
    _register_persona(ctx)
    _warn_legacy_gateway_flag(ctx)
    logger.info(
        "iibot-hermes: effective settings apiUrl=%s inboxId=%s wakeMode=%s pushMode=%s "
        "firstRunGuide=%s prompt.enabled=%s persona.enabled=%s vision.enabled=%s",
        config.api_url, config.inbox_id, config.wake_mode, config.push_mode,
        config.first_run_guide, bool(_prompt_config["enabled"]), bool(_persona_config["enabled"]),
        bool(_vision_config["enabled"]),
    )
