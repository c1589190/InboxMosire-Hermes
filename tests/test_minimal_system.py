"""minimal-system: 用一句常量（或操作者的 md 文件）替换出站请求里的 system prompt（三种线格式）。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _loader  # noqa: E402

plugin = _loader.prompt_plugin()
rewrite = _loader.prompt_lib("rewrite")
prompt_file = _loader.prompt_lib("prompt_file")

TEXT = plugin.DEFAULT_SYSTEM_PROMPT

#: 空目录当 HERMES_HOME，免得开发机上真实的 ``~/.hermes/SYSTEM.md`` 污染断言。
_HOME_TMP = tempfile.TemporaryDirectory()
_ORIGINAL_HOME = os.environ.get("HERMES_HOME")


def setUpModule() -> None:
    os.environ["HERMES_HOME"] = _HOME_TMP.name


def tearDownModule() -> None:
    if _ORIGINAL_HOME is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = _ORIGINAL_HOME
    _HOME_TMP.cleanup()


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
        # prompt.enabled 默认 false（1.0.0）：这些用例测的是中间件本身，显式打开。
        config.setdefault("enabled", True)
        ctx = _loader.FakeCtx({"prompt": dict(config)})
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
        # 关闭时根本不注册中间件（不是注册后返回 None）——宿主的 system prompt 保持原样。
        ctx = self._ctx(enabled=False)
        self.assertNotIn("llm_request", ctx.middleware)

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



class PromptFileTests(unittest.TestCase):
    """``systemPromptFile``: md 原样发送、缺失回落、改文件即时生效。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "SYSTEM.md"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _hook(self, **config):
        config.setdefault("enabled", True)
        ctx = _loader.FakeCtx({"prompt": dict(config)})
        plugin.register(ctx)
        return ctx.middleware["llm_request"][0]

    def _system(self, hook, request=None) -> str:
        out = hook(request=request or chat_request(), api_mode="chat_completions")
        self.assertIsInstance(out, dict)
        return out["request"]["messages"][0]["content"]

    def test_markdown_body_is_sent_verbatim(self):
        body = "# 大肥鱼\n\n## 硬约束\n\n- 第一行\n- 第二行\n\n```text\n代码块照发\n```"
        self.path.write_text(body + "\n", encoding="utf-8")
        self.assertEqual(self._system(self._hook(systemPromptFile=str(self.path))), body)

    def test_file_wins_over_inline_prompt(self):
        self.path.write_text("from file", encoding="utf-8")
        hook = self._hook(systemPromptFile=str(self.path), systemPrompt="from config")
        self.assertEqual(self._system(hook), "from file")

    def test_missing_file_falls_back_to_inline_prompt(self):
        hook = self._hook(systemPromptFile=str(self.dir / "nope.md"), systemPrompt="from config")
        self.assertEqual(self._system(hook), "from config")

    def test_missing_file_without_inline_falls_back_to_default(self):
        hook = self._hook(systemPromptFile=str(self.dir / "nope.md"), systemPrompt="")
        self.assertEqual(self._system(hook), plugin.DEFAULT_SYSTEM_PROMPT)

    def test_blank_file_falls_back_to_inline_prompt(self):
        self.path.write_text(" \n\n", encoding="utf-8")
        hook = self._hook(systemPromptFile=str(self.path), systemPrompt="from config")
        self.assertEqual(self._system(hook), "from config")

    def test_bom_is_stripped(self):
        self.path.write_text("# BOM 头\n正文", encoding="utf-8-sig")
        self.assertEqual(self._system(self._hook(systemPromptFile=str(self.path))), "# BOM 头\n正文")

    def test_edit_is_picked_up_without_restart(self):
        hook = self._hook(systemPromptFile=str(self.path))
        self.path.write_text("version one", encoding="utf-8")
        self.assertEqual(self._system(hook), "version one")
        self.path.write_text("version two!", encoding="utf-8")  # 长度变化 → (mtime, size) 必变
        self.assertEqual(self._system(hook), "version two!")

    def test_relative_path_resolves_against_cwd(self):
        self.path.write_text("relative", encoding="utf-8")
        previous = os.getcwd()
        os.chdir(self.dir)
        try:
            hook = self._hook(systemPromptFile="SYSTEM.md")
            self.assertEqual(self._system(hook), "relative")
        finally:
            os.chdir(previous)

    def test_blank_path_has_no_reader(self):
        """空字符串本身没有含义了（``_load_config`` 会先换成默认路径），但底层不许炸。"""
        self.assertEqual(prompt_file.resolve_path(""), "")
        self.assertIsNone(prompt_file.PromptReader("").read())

    def test_reader_never_raises_on_a_directory(self):
        self.assertIsNone(prompt_file.PromptReader(str(self.dir)).read())


class DefaultPathTests(unittest.TestCase):
    """留空 ``systemPromptFile`` → ``$HERMES_HOME/SYSTEM.md``（Hermes 自己的目录，与 SOUL.md 同级）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._saved = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._saved
        self.tmp.cleanup()

    def _system(self, **config) -> str:
        config.setdefault("enabled", True)
        ctx = _loader.FakeCtx({"prompt": dict(config)})
        plugin.register(ctx)
        hook = ctx.middleware["llm_request"][0]
        out = hook(request=chat_request(), api_mode="chat_completions")
        self.assertIsInstance(out, dict)
        return out["request"]["messages"][0]["content"]

    def test_default_path_is_inside_the_hermes_home(self):
        self.assertEqual(prompt_file.hermes_home(), str(self.home))
        self.assertEqual(prompt_file.default_prompt_path(), str(self.home / "SYSTEM.md"))

    def test_default_file_is_used_when_the_setting_is_empty(self):
        (self.home / "SYSTEM.md").write_text("# 默认文件\n正文", encoding="utf-8")
        self.assertEqual(self._system(systemPrompt="from config"), "# 默认文件\n正文")

    def test_missing_default_file_falls_back_to_inline_prompt(self):
        self.assertEqual(self._system(systemPrompt="from config"), "from config")

    def test_explicit_path_still_wins_over_the_default(self):
        other = self.home / "other.md"
        other.write_text("explicit", encoding="utf-8")
        (self.home / "SYSTEM.md").write_text("default file", encoding="utf-8")
        self.assertEqual(self._system(systemPromptFile=str(other)), "explicit")


if __name__ == "__main__":
    unittest.main()
