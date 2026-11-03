"""minimal-system — 用一句固定的系统提示词替换宿主的整段 system prompt。

Hermes 的 system prompt 会拼进身份、工具使用强制、环境探针、时间戳等十余段
（`agent/system_prompt.py:738` 的三层组装），在 DSH 里这些由 preset 的
`complete: true` + `includeRuntimeContext: false` 压掉（`dsh-persona-mode/cordis.patch.yml:38-43`）。
Hermes 没有那个开关，所以在**出站请求的最后一步**用公开的 `llm_request` 中间件整段替换
（`hermes_cli/plugins.py:939` 注册、`hermes_cli/middleware.py:88-99` 应用、
调用点在 `agent/turn_api_request.py:156-166`，此时 provider kwargs 已构建完毕）。

替换必须是**字节稳定**的：同一个常量每一轮都发同样的字节，否则前缀缓存每轮失效。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .lib import rewrite

logger = logging.getLogger(__name__)

#: DSH「极简模式」的默认身份（`dsh-persona-mode/README.md` 的 subagent 一节）。
DEFAULT_SYSTEM_PROMPT = "You are a helpful software engineer assistant."

_config: dict[str, Any] = {
    "enabled": True,
    "systemPrompt": DEFAULT_SYSTEM_PROMPT,
    "logRewrite": False,
}


def _load_config(ctx: Any) -> None:
    get = getattr(ctx, "get_config", None)
    if not callable(get):
        return
    try:
        _config["enabled"] = bool(get("enabled", True))
        text = str(get("systemPrompt", DEFAULT_SYSTEM_PROMPT) or "").strip()
        if text:
            _config["systemPrompt"] = text
        _config["logRewrite"] = bool(get("logRewrite", False))
    except Exception as exc:  # config trouble must never break a request
        logger.warning("minimal-system: cannot read settings, using defaults: %s", exc)


def _on_llm_request(**kwargs: Any) -> Optional[dict[str, Any]]:
    """Replace the system prompt of one outbound request; ``None`` leaves it untouched."""
    try:
        if not _config["enabled"]:
            return None
        request = kwargs.get("request")
        if not isinstance(request, dict):
            return None
        text = str(_config["systemPrompt"])
        new_request, shape = rewrite.rewrite_system_prompt(request, text)
        if shape == rewrite.SHAPE_NONE:
            logger.debug("minimal-system: no system prompt slot found (api_mode=%s)", kwargs.get("api_mode"))
            return None
        if new_request == request:
            return None
        if _config["logRewrite"]:
            before = rewrite.current_system_prompt(request)
            logger.info(
                "minimal-system: system %d chars -> %d chars (shape=%s, api_mode=%s)",
                len(before), len(text), shape, kwargs.get("api_mode"),
            )
        return {"request": new_request, "source": "minimal-system", "reason": f"system:{shape}"}
    except Exception as exc:  # a middleware bug must not take the turn down
        logger.warning("minimal-system: rewrite failed, sending the request unmodified: %s", exc)
        return None


def register(ctx: Any) -> None:
    _load_config(ctx)
    try:
        ctx.register_middleware("llm_request", _on_llm_request)
    except Exception as exc:  # an old host without request middleware: say so, do not crash the loader
        logger.warning("minimal-system: llm_request middleware unavailable: %s", exc)
        return
    logger.info(
        "minimal-system: loaded (enabled=%s, %d chars)", _config["enabled"], len(_config["systemPrompt"])
    )
