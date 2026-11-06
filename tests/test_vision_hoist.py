"""0.4.0-fix1：出站请求里的图片只能出现在 role="user" 消息中。

两层测试：

* ``hoist`` 纯函数（无宿主、无配置）：搬运、幂等、上限、形状、不改入参；
* 插件装配：默认挂 ``llm_request`` 中间件、无图返回 None、关掉就不注册、降级留痕。

背景：DeepSeek 文档「图片仅支持出现在 user 消息中」；Hermes 的 MCP ``ImageContent`` 却挂在
tool 消息上，官方 v0.22.6 没有 ``hoist_tool_result_images`` 那段宿主补丁 —— 于是像素在请求
里被 provider 丢掉，模型只看到一句 "Image loaded"。见 ``lib/vision/hoist.py`` 的模块注释。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _loader  # noqa: E402

plugin = _loader.plugin()
hoist = _loader.plugin().lib.vision.hoist
settings_lib = _loader.settings_lib()

TOKENLESS = {"inbox": {"enabled": False}}


def image(url: str = "data:image/png;base64,AAAA", kind: str = "image_url") -> dict:
    return {"type": kind, "image_url": {"url": url}}


def chat_request(*messages: dict) -> dict:
    return {"model": "deepseek-flash", "messages": list(messages)}


def tool_message(text: str = "Image loaded into your context — you can see it natively now.", *images: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": [{"type": "text", "text": text}, *images],
    }


class HoistPureTests(unittest.TestCase):
    """不变式本身：搬完之后 ``images_outside_user()`` 必须是空的。"""

    def test_tool_image_moves_into_a_user_message_right_after_it(self) -> None:
        request = chat_request(
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "看看这张图"},
            {"role": "assistant", "content": "我调一下工具"},
            tool_message("MEDIA:/tmp/a.jpg", image()),
        )
        result = hoist.hoist_images_to_user(request)

        self.assertTrue(result.changed)
        self.assertEqual(result.moved, 1)
        self.assertEqual(result.dropped, 0)
        self.assertEqual(result.sources, ("tool",))
        self.assertEqual(result.carrier_index, 4)  # 紧接着那条 tool 消息

        messages = result.request["messages"]
        self.assertEqual(len(messages), 5)
        carrier = messages[4]
        self.assertEqual(carrier["role"], "user")
        self.assertEqual(carrier["content"][0]["type"], "text")
        self.assertEqual(carrier["content"][1]["type"], "image_url")
        # 原来的 tool 消息只剩文本，且提示"图被挪走了"
        self.assertNotIn("image_url", str(messages[3]["content"]))
        self.assertIn(hoist.DEFAULT_PLACEHOLDER, str(messages[3]["content"]))
        self.assertEqual(hoist.images_outside_user(result.request), [])

    def test_user_images_are_left_where_they_are(self) -> None:
        request = chat_request({"role": "user", "content": [{"type": "text", "text": "hi"}, image()]})
        result = hoist.hoist_images_to_user(request)
        self.assertFalse(result.changed)
        self.assertIs(result.request, request)
        self.assertEqual(len(request["messages"]), 1)

    def test_a_request_without_images_is_returned_by_identity(self) -> None:
        request = chat_request({"role": "user", "content": "just text"})
        result = hoist.hoist_images_to_user(request)
        self.assertFalse(result.changed)
        self.assertIs(result.request, request)
        self.assertEqual(result.reason(), "no images outside user messages")

    def test_the_callers_dict_is_never_mutated(self) -> None:
        request = chat_request(tool_message("t", image()))
        before = repr(request)
        hoist.hoist_images_to_user(request)
        self.assertEqual(repr(request), before)

    def test_hoisting_twice_is_a_no_op(self) -> None:
        first = hoist.hoist_images_to_user(chat_request(tool_message("t", image())))
        second = hoist.hoist_images_to_user(first.request)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertIs(second.request, first.request)

    def test_over_the_cap_keeps_the_most_recent_and_counts_the_dropped(self) -> None:
        images = [image(f"data:image/png;base64,{i}") for i in range(6)]
        request = chat_request(tool_message("t", *images))
        result = hoist.hoist_images_to_user(request, max_images=4)

        self.assertEqual(result.moved, 4)
        self.assertEqual(result.dropped, 2)
        kept = [part["image_url"]["url"] for part in result.request["messages"][1]["content"][1:]]
        self.assertEqual(kept, ["data:image/png;base64,2", "data:image/png;base64,3",
                                "data:image/png;base64,4", "data:image/png;base64,5"])

    def test_placeholder_is_configurable(self) -> None:
        result = hoist.hoist_images_to_user(
            chat_request(tool_message("t", image())), placeholder="[图已搬走]"
        )
        self.assertIn("[图已搬走]", str(result.request["messages"][0]["content"]))

    def test_text_only_tool_content_collapses_to_a_plain_string(self) -> None:
        request = chat_request({"role": "tool", "tool_call_id": "c", "content": [image()]})
        result = hoist.hoist_images_to_user(request)
        self.assertIsInstance(result.request["messages"][0]["content"], str)

    def test_assistant_and_system_images_are_hoisted_too(self) -> None:
        request = chat_request(
            {"role": "system", "content": [{"type": "text", "text": "s"}, image()]},
            {"role": "assistant", "content": [{"type": "text", "text": "a"}, image()]},
        )
        result = hoist.hoist_images_to_user(request)
        self.assertEqual(result.moved, 2)
        self.assertEqual(result.sources, ("system", "assistant"))
        self.assertEqual(hoist.images_outside_user(result.request), [])

    def test_each_image_message_contributes_to_one_carrier_at_the_last_position(self) -> None:
        request = chat_request(
            tool_message("a", image("data:image/png;base64,1")),
            {"role": "assistant", "content": "ok"},
            tool_message("b", image("data:image/png;base64,2")),
            {"role": "user", "content": "继续"},
        )
        result = hoist.hoist_images_to_user(request)
        self.assertEqual(result.moved, 2)
        self.assertEqual(result.carrier_index, 3)
        self.assertEqual(
            [part["type"] for part in result.request["messages"][3]["content"]],
            ["text", "image_url", "image_url"],
        )

    def test_responses_shape_moves_function_call_output_images(self) -> None:
        request = {
            "model": "deepseek-flash",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "看图"}]},
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": [
                        {"type": "input_text", "text": "MEDIA:/tmp/a.jpg"},
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                    ],
                },
            ],
        }
        result = hoist.hoist_images_to_user(request)
        self.assertTrue(result.changed)
        self.assertEqual(result.moved, 1)
        self.assertEqual(hoist.images_outside_user(result.request), [])
        carrier = result.request["input"][2]
        self.assertEqual(carrier["role"], "user")
        self.assertEqual([part["type"] for part in carrier["content"]], ["input_text", "input_image"])

    def test_unknown_shapes_are_returned_untouched(self) -> None:
        for request in (None, "nope", {}, {"messages": "not-a-list"}, {"prompt": "x"}):
            result = hoist.hoist_images_to_user(request)
            self.assertFalse(result.changed, request)
            self.assertIs(result.request, request)

    def test_a_bad_max_images_falls_back_to_the_default(self) -> None:
        request = chat_request(tool_message("t", *[image(f"data:,{i}") for i in range(6)]))
        result = hoist.hoist_images_to_user(request, max_images="four")
        self.assertEqual(result.moved, hoist.MAX_IMAGES)

    def test_is_image_part(self) -> None:
        self.assertTrue(hoist.is_image_part({"type": "image_url"}))
        self.assertTrue(hoist.is_image_part({"type": "input_image"}))
        self.assertTrue(hoist.is_image_part({"type": "IMAGE"}))
        self.assertFalse(hoist.is_image_part({"type": "text", "text": "x"}))
        self.assertFalse(hoist.is_image_part("image_url"))
        self.assertFalse(hoist.is_image_part(None))

    def test_images_outside_user_names_the_offending_slots(self) -> None:
        request = chat_request({"role": "assistant", "content": [image()]})
        offenders = hoist.images_outside_user(request)
        self.assertEqual(len(offenders), 1)
        self.assertIn("messages[0].content[0]", offenders[0])
        self.assertIn("role=assistant", offenders[0])


class VisionMiddlewareTests(unittest.TestCase):
    """插件装配：默认挂载、无图 None、关掉不注册、降级留痕、永不抛。"""

    def _register(self, settings: dict | None = None):
        ctx = _loader.FakeCtx({**TOKENLESS, **(settings or {})})
        plugin.register(ctx)
        if ctx.unload:
            ctx.unload()
        return ctx

    def _vision_hook(self, ctx):
        for callback in ctx.middleware.get("llm_request", []):
            if callback is plugin._vision_on_llm_request:
                return callback
        raise AssertionError("vision middleware not registered")

    def test_registered_by_default(self) -> None:
        ctx = self._register()
        self.assertTrue(plugin._vision_config["enabled"])
        self.assertIn(plugin._vision_on_llm_request, ctx.middleware["llm_request"])

    def test_can_be_disabled(self) -> None:
        ctx = self._register({"vision": {"enabled": False}})
        self.assertNotIn("llm_request", ctx.middleware)
        self.assertFalse(plugin._vision_config["enabled"])
        self._register()  # 还原全局配置，避免污染其他用例

    def test_no_images_returns_none(self) -> None:
        ctx = self._register()
        hook = self._vision_hook(ctx)
        self.assertIsNone(
            hook(request=chat_request({"role": "user", "content": "hi"}), api_mode="chat_completions")
        )

    def test_returns_a_replacement_payload(self) -> None:
        ctx = self._register()
        hook = self._vision_hook(ctx)
        request = chat_request(tool_message("MEDIA:/tmp/a.jpg", image()))
        result = hook(request=request, api_mode="chat_completions", session_id="s1")

        self.assertIsInstance(result, dict)
        self.assertEqual(result["source"], "iibot-hermes-vision")
        self.assertIn("hoisted 1 image(s) from tool", result["reason"])
        self.assertEqual(result["request"]["messages"][1]["role"], "user")
        self.assertEqual(hoist.images_outside_user(result["request"]), [])
        # 入参没被改
        self.assertIn("image_url", str(request["messages"][0]["content"]))

    def test_settings_drive_max_images_and_placeholder(self) -> None:
        ctx = self._register({"vision": {"maxImages": 1, "placeholder": "[搬走了]"}})
        hook = self._vision_hook(ctx)
        request = chat_request(tool_message("t", image("data:,1"), image("data:,2")))
        with self.assertLogs("iibot_hermes_plugin", level="WARNING") as logs:
            result = hook(request=request, api_mode="chat_completions")
        self.assertEqual(result["request"]["messages"][1]["content"][1]["image_url"]["url"], "data:,2")
        self.assertIn("[搬走了]", str(result["request"]["messages"][0]["content"]))
        self.assertIn("dropped 1 image(s)", "\n".join(logs.output))
        self._register()  # 还原

    def test_log_hoist_switches_debug_to_info(self) -> None:
        ctx = self._register({"vision": {"logHoist": True}})
        hook = self._vision_hook(ctx)
        with self.assertLogs("iibot_hermes_plugin", level="INFO") as logs:
            hook(request=chat_request(tool_message("t", image())), api_mode="chat_completions")
        self.assertIn("iibot-hermes vision: hoisted 1 image(s)", "\n".join(logs.output))
        self._register()

    def test_garbage_never_raises(self) -> None:
        ctx = self._register()
        hook = self._vision_hook(ctx)
        for request in (None, "string", {}, {"messages": "nope"}, {"messages": [None, 42]}):
            self.assertIsNone(hook(request=request, api_mode="chat_completions"))

    def test_prompt_and_vision_coexist_in_one_payload(self) -> None:
        ctx = self._register({
            "prompt": {"enabled": True, "systemPrompt": "one line"},
            "vision": {"enabled": True},
        })
        request = chat_request(
            {"role": "system", "content": "host prompt"},
            tool_message("t", image()),
        )
        for callback in ctx.middleware["llm_request"]:
            replacement = callback(request=request, api_mode="chat_completions")
            if replacement:
                request = replacement["request"]
        self.assertEqual(request["messages"][0]["content"], "one line")
        self.assertEqual(request["messages"][-1]["role"], "user")
        self.assertEqual(hoist.images_outside_user(request), [])
        self._register()


class VisionSettingsTests(unittest.TestCase):
    def test_vision_is_a_known_section_without_a_legacy_namespace(self) -> None:
        self.assertIn("vision", settings_lib.SECTIONS)
        self.assertNotIn("vision", settings_lib.LEGACY_IDS)
        ctx = _loader.FakeCtx({"vision": {"maxImages": 2}})
        self.assertEqual(settings_lib.load_section(ctx, "vision")["maxImages"], 2)

    def test_unknown_sections_still_raise(self) -> None:
        ctx = _loader.FakeCtx({})
        with self.assertRaises(KeyError):
            settings_lib.load_section(ctx, "nope")

    def test_dotted_keys_are_accepted_like_the_other_sections(self) -> None:
        ctx = _loader.FakeCtx({"vision.maxImages": 3})
        self.assertEqual(settings_lib.load_section(ctx, "vision")["maxImages"], 3)


if __name__ == "__main__":
    unittest.main()
