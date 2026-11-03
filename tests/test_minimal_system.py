"""minimal-system: 用一句常量替换出站请求里的 system prompt（三种线格式）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _loader  # noqa: E402

plugin = _loader.load_package(
    "minimal_system_plugin", _loader.ROOT / "plugins" / "minimal-system"
)
rewrite = sys.modules["minimal_system_plugin.lib.rewrite"]

TEXT = plugin.DEFAULT_SYSTEM_PROMPT


def chat_request(system: str = "long host prompt") -> dict:
    return {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "hi"},
        ],
        "tools": [{"name": "read_file"}],
    }


class RewriteTests(unittest.TestCase):
    def test_chat_completions_shape(self) -> None:
        request = chat_request()
        out, shape = rewrite.rewrite_system_prompt(request, TEXT)
        self.assertEqual(shape, rewrite.SHAPE_CHAT)
        self.assertEqual(out["messages"][0]["content"], TEXT)
        self.assertEqual(out["messages"][1], request["messages"][1])
        self.assertEqual(out["tools"], request["tools"])
        self.assertEqual(out["model"], request["model"])
        # 不改原 dict（中间件契约是"返回替换体"）
        self.assertEqual(request["messages"][0]["content"], "long host prompt")

    def test_chat_shape_keeps_message_extras(self) -> None:
        request = {"messages": [{"role": "system", "content": "x", "cache_control": {"type": "ephemeral"}}]}
        out, _ = rewrite.rewrite_system_prompt(request, TEXT)
        self.assertEqual(out["messages"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(out["messages"][0]["content"], TEXT)

    def test_anthropic_string_shape(self) -> None:
        request = {"system": "long", "messages": [{"role": "user", "content": "hi"}]}
        out, shape = rewrite.rewrite_system_prompt(request, TEXT)
        self.assertEqual(shape, rewrite.SHAPE_SYSTEM_TEXT)
        self.assertEqual(out["system"], TEXT)

    def test_anthropic_blocks_shape_collapses_and_keeps_cache_control(self) -> None:
        request = {
            "system": [
                {"type": "text", "text": "identity", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "guidance"},
            ],
            "messages": [],
        }
        out, shape = rewrite.rewrite_system_prompt(request, TEXT)
        self.assertEqual(shape, rewrite.SHAPE_SYSTEM_BLOCKS)
        self.assertEqual(out["system"], [{"type": "text", "text": TEXT, "cache_control": {"type": "ephemeral"}}])

    def test_codex_instructions_shape(self) -> None:
        request = {"instructions": "long", "input": [{"role": "user", "content": "hi"}]}
        out, shape = rewrite.rewrite_system_prompt(request, TEXT)
        self.assertEqual(shape, rewrite.SHAPE_INSTRUCTIONS)
        self.assertEqual(out["instructions"], TEXT)
        self.assertEqual(out["input"], request["input"])

    def test_no_system_slot_is_left_alone(self) -> None:
        request = {"messages": [{"role": "user", "content": "hi"}]}
        out, shape = rewrite.rewrite_system_prompt(request, TEXT)
        self.assertEqual(shape, rewrite.SHAPE_NONE)
        self.assertEqual(out, request)

    def test_only_the_leading_system_message_is_replaced(self) -> None:
        request = {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "system", "content": "mid-conversation note"},
            ]
        }
        out, shape = rewrite.rewrite_system_prompt(request, TEXT)
        self.assertEqual(shape, rewrite.SHAPE_NONE)
        self.assertEqual(out["messages"][1]["content"], "mid-conversation note")

    def test_blank_text_is_a_no_op(self) -> None:
        request = chat_request()
        out, shape = rewrite.rewrite_system_prompt(request, "   ")
        self.assertEqual(shape, rewrite.SHAPE_NONE)
        self.assertEqual(out["messages"][0]["content"], "long host prompt")

    def test_current_system_prompt_reads_every_shape(self) -> None:
        self.assertEqual(rewrite.current_system_prompt(chat_request("sys")), "sys")
        self.assertEqual(rewrite.current_system_prompt({"system": "s"}), "s")
        self.assertEqual(
            rewrite.current_system_prompt({"system": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}),
            "a\nb",
        )
        self.assertEqual(rewrite.current_system_prompt({"instructions": "i"}), "i")
        self.assertEqual(rewrite.current_system_prompt({"messages": []}), "")


class RegisterTests(unittest.TestCase):
    def _ctx(self, **config) -> object:
        ctx = _loader.FakeCtx(dict(config))
        plugin.register(ctx)
        return ctx

    def _hook(self, ctx):
        return ctx.middleware["llm_request"][0]

    def test_register_wires_llm_request_middleware(self) -> None:
        ctx = self._ctx()
        self.assertEqual(list(ctx.middleware), ["llm_request"])
        self.assertEqual(ctx.sections, [])

    def test_hook_returns_request_replacement(self) -> None:
        ctx = self._ctx()
        request = chat_request()
        result = self._hook(ctx)(request=request, api_mode="chat_completions", session_id="s1")
        self.assertIsInstance(result, dict)
        self.assertIn("request", result)
        self.assertEqual(result["request"]["messages"][0]["content"], TEXT)
        self.assertEqual(result["source"], "minimal-system")
        self.assertEqual(result["reason"], "system:messages")

    def test_hook_can_be_disabled(self) -> None:
        ctx = self._ctx(enabled=False)
        self.assertIsNone(self._hook(ctx)(request=chat_request(), api_mode="chat_completions"))

    def test_hook_honours_a_custom_prompt(self) -> None:
        ctx = self._ctx(systemPrompt="You are 大肥鱼.")
        result = self._hook(ctx)(request=chat_request(), api_mode="chat_completions")
        self.assertEqual(result["request"]["messages"][0]["content"], "You are 大肥鱼.")

    def test_hook_is_idempotent_and_byte_stable(self) -> None:
        ctx = self._ctx()
        hook = self._hook(ctx)
        first = hook(request=chat_request(), api_mode="chat_completions")["request"]
        self.assertIsNone(hook(request=first, api_mode="chat_completions"))
        again = hook(request=chat_request(), api_mode="chat_completions")["request"]
        self.assertEqual(first["messages"][0]["content"], again["messages"][0]["content"])

    def test_hook_never_raises_on_garbage(self) -> None:
        ctx = self._ctx()
        hook = self._hook(ctx)
        self.assertIsNone(hook(request=None))
        self.assertIsNone(hook(request="nope"))
        self.assertIsNone(hook())
        self.assertIsNone(hook(request={"messages": "not-a-list"}))


if __name__ == "__main__":
    unittest.main()
