"""把非 user 消息里的图片搬进 user 消息 —— 出站请求的最后一次整形。

为什么需要它
------------

DeepSeek 视觉文档写明「图片仅支持出现在 **user** 消息中。system 或 assistant 消息携带图片会
返回 400 错误。」（https://api-docs.deepseek.com/zh-cn/guides/vision/ ）；OpenAI Chat
Completions 规范里 tool 消息的 ``content`` 类型是 ``str | Iterable[TextParam]``，没有图片成员。

Hermes 却会把 MCP ``ImageContent`` / ``vision_analyze`` 的图挂在 **tool 消息**上
（``tools/vision_tools.py:573 _native_tool_result_images``、``tools/mcp_tool_content.py:176
_mcp_result_with_native_images``）。宿主只有在带 ``hoist_tool_result_images`` 补丁时
（``agent/tool_dispatch_helpers.py:373``，本仓库自己维护的宿主补丁）才会把图提升成隐藏的
``role="user"`` 行；官方 ``v0.22.6`` 的 tag（``f0d94882e8``）里没有这段 —— 于是图片跟着 tool
消息离开进程，模型只看到一句 "Image loaded into your context"，实际一个像素都没收到。

这个模块是插件侧的答案：**在出站 payload 上重建不变式** ——
任何一条发给 provider 的请求里，图片只出现在 ``role="user"`` 的消息中。

契约
----

* 纯函数：不读配置、不连网、不写盘、不改调用方的 dict（中间件契约是"返回替换用的 payload"，
  同 ``lib/prompt/rewrite.py`` 的注释）。
* 幂等：已经只出现在 user 里的图会被原样留下，再调一次是 no-op。
* 有界：一次最多搬 ``max_images`` 张（默认 4，与宿主 ``_MCP_NATIVE_IMAGE_MAX`` 同量级），
  超出的从**最旧**开始丢弃并计入 ``dropped`` —— 丢弃属于降级，调用方必须留痕。
* 形状兼容：Chat Completions（``messages``）与 Responses（``input``，含
  ``function_call_output.output`` 里的 ``input_image``）都认；不认识的形状原样返回。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

#: 三种图片块的名字：Chat Completions 的 ``image_url``、Responses 的 ``input_image``、
#: Anthropic 风格的 ``image``。宿主内部（``agent/vision_message_prep.py:29``）认前两种。
IMAGE_PART_TYPES: frozenset[str] = frozenset({"image_url", "input_image", "image"})

#: 单次请求最多搬几张 —— 与宿主 ``tools/mcp_tool_content.py:145 _MCP_NATIVE_IMAGE_MAX`` 同量级。
MAX_IMAGES: int = 4

#: 原消息里留下的占位文本：让模型知道"图被挪走了"，而不是"图消失了"。
DEFAULT_PLACEHOLDER: str = "[图片已移出本消息，见随后那条 user 消息]"

#: 搬运后那条 user 消息的开头一句。
CARRIER_PREFIX: str = "以下是刚才工具消息里的图片（按 OpenAI 协议放在 user 消息里）："

#: 文本块的几种写法（Chat/Responses）。
_TEXT_PART_TYPES: frozenset[str] = frozenset({"text", "input_text", "output_text"})

#: payload 里可能承载消息的两个键，按优先级。
_CONTAINER_KEYS: tuple[str, ...] = ("messages", "input")


@dataclass(frozen=True)
class HoistResult:
    """一次搬运的结果。``changed`` 为假时 ``request`` 就是传进来的那个对象。"""

    request: Any
    changed: bool = False
    moved: int = 0
    dropped: int = 0
    carrier_index: int = -1
    sources: tuple[str, ...] = ()

    def reason(self) -> str:
        """给中间件 trace 用的一句话（不含任何图片内容）。"""
        if not self.changed:
            return "no images outside user messages"
        roles = ",".join(self.sources) or "-"
        tail = f"; dropped={self.dropped}" if self.dropped else ""
        return f"hoisted {self.moved} image(s) from {roles} into user message #{self.carrier_index}{tail}"


# --------------------------------------------------------------------------------------
# 判读
# --------------------------------------------------------------------------------------


def is_image_part(part: Any) -> bool:
    """``{"type": "image_url" | "input_image" | "image", …}``。"""
    if not isinstance(part, Mapping):
        return False
    return str(part.get("type") or "").strip().lower() in IMAGE_PART_TYPES


def _role_of(item: Any) -> str:
    if not isinstance(item, Mapping):
        return "unknown"
    role = str(item.get("role") or "").strip().lower()
    if role:
        return role
    # Responses 的工具结果条目没有 role，只有 type（``function_call_output`` 等）。
    return str(item.get("type") or "unknown").strip().lower() or "unknown"


def _container_key(request: Any) -> str:
    if not isinstance(request, Mapping):
        return ""
    for key in _CONTAINER_KEYS:
        if isinstance(request.get(key), list):
            return key
    return ""


def iter_image_parts(request: Any) -> Iterable[tuple[str, str, Mapping[str, Any]]]:
    """``(定位描述, 角色, 图片块)`` —— 只读遍历，用来做自检与测试。"""
    key = _container_key(request)
    if not key:
        return
    responses = key == "input"
    for index, item in enumerate(request[key]):
        if not isinstance(item, Mapping):
            continue
        role = _role_of(item)
        for field_name in ("content", "output"):
            parts = item.get(field_name)
            if not isinstance(parts, list):
                continue
            for position, part in enumerate(parts):
                if is_image_part(part):
                    yield (f"{key}[{index}].{field_name}[{position}]", role, part)
        if responses:  # Responses 的单图简写：{"type": "input_image", "image_url": …}
            if is_image_part(item):
                yield (f"{key}[{index}]", role, item)


def images_outside_user(request: Any) -> list[str]:
    """不变式的违规清单（空 = 图片都只在 user 消息里）。调用方用它做发送前自检。"""
    return [
        f"{where} (role={role})"
        for where, role, _part in iter_image_parts(request)
        if role != "user"
    ]


# --------------------------------------------------------------------------------------
# 搬运
# --------------------------------------------------------------------------------------


def _text_part(text: str, *, responses: bool) -> dict[str, Any]:
    return {"type": "input_text" if responses else "text", "text": text}


def _split_parts(parts: Any, sink: list[Any]) -> tuple[Any, bool]:
    """把 ``parts`` 里的图片块摘进 ``sink``，返回 ``(剩下的, 是否摘过)``。"""
    if not isinstance(parts, list):
        return parts, False
    keep: list[Any] = []
    moved = False
    for part in parts:
        if is_image_part(part):
            sink.append(part)
            moved = True
        else:
            keep.append(part)
    return keep, moved


def _collapse(parts: list[Any]) -> Any:
    """只剩一个文本块时收成纯字符串 —— tool 消息的最保守写法。"""
    if len(parts) == 1 and isinstance(parts[0], Mapping):
        if str(parts[0].get("type") or "").strip().lower() in _TEXT_PART_TYPES:
            return str(parts[0].get("text") or "")
    return parts


def _strip_item(
    item: Any,
    sink: list[Any],
    *,
    placeholder: str,
    responses: bool,
) -> tuple[Any, bool]:
    """去掉一条非 user 消息里的图片块；``sink`` 收集被摘出来的块。"""
    if not isinstance(item, Mapping) or _role_of(item) == "user":
        return item, False

    updates: dict[str, Any] = {}
    touched = False
    for field_name in ("content", "output"):
        if field_name not in item:
            continue
        keep, moved = _split_parts(item.get(field_name), sink)
        if not moved:
            continue
        touched = True
        if isinstance(keep, list):
            keep = [*keep, _text_part(placeholder, responses=responses)]
        updates[field_name] = _collapse(keep)

    if not touched:
        return item, False
    return {**item, **updates}, True


def hoist_images_to_user(
    request: Any,
    *,
    max_images: int = MAX_IMAGES,
    placeholder: str = DEFAULT_PLACEHOLDER,
    carrier_prefix: str = CARRIER_PREFIX,
) -> HoistResult:
    """把非 user 消息里的图片搬进一条紧随其后的 ``role="user"`` 消息。

    没有可搬的图时返回**原对象**（``changed=False``），调用方据此 ``return None`` 让宿主的
    请求保持逐字节不变。
    """
    key = _container_key(request)
    if not key:
        return HoistResult(request=request)

    responses = key == "input"
    try:
        limit = max(1, int(max_images))
    except (TypeError, ValueError):
        limit = MAX_IMAGES

    collected: list[Any] = []
    sources: list[str] = []
    rebuilt: list[Any] = []
    carrier_index = -1
    for item in request[key]:
        sink: list[Any] = []
        new_item, moved = _strip_item(
            item, sink, placeholder=placeholder, responses=responses
        )
        if moved:
            collected.extend(sink)
            sources.append(_role_of(item))
            carrier_index = len(rebuilt)  # 这条消息落在新列表里的位置
        rebuilt.append(new_item)

    if not collected:
        return HoistResult(request=request)

    kept = collected[-limit:]  # 最近的图优先，超限的从最旧的丢
    dropped = len(collected) - len(kept)
    carrier = {
        "role": "user",
        "content": [_text_part(carrier_prefix, responses=responses), *kept],
    }
    rebuilt.insert(carrier_index + 1, carrier)

    return HoistResult(
        request={**request, key: rebuilt},
        changed=True,
        moved=len(kept),
        dropped=dropped,
        carrier_index=carrier_index + 1,
        sources=tuple(sources),
    )
