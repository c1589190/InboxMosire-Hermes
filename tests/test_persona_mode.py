"""persona-mode: contract text, dedupe by hash, missing-file guard, registration wiring."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _loader  # noqa: E402

plugin = _loader.persona_plugin()
persona = sys.modules["persona_mode_plugin.lib.persona"]
guard = sys.modules["persona_mode_plugin.lib.guard"]

BODY = "# 大肥鱼\n\n自称本鲸。默认 1-3 句。"


class ContractTests(unittest.TestCase):
    def test_hash_is_stable_and_short(self) -> None:
        first = persona.contract_hash(BODY)
        self.assertEqual(first, persona.contract_hash(BODY))
        self.assertEqual(len(first), 12)
        self.assertNotEqual(first, persona.contract_hash(BODY + "x"))

    def test_contract_block_carries_tag_hash_and_body(self) -> None:
        digest = persona.contract_hash(BODY)
        text = persona.build_contract(BODY, 4000)
        self.assertIn("<persona-contract>", text)
        self.assertIn("</persona-contract>", text)
        self.assertIn(f"persona-hash: {digest}", text)
        self.assertIn("自称本鲸", text)
        self.assertTrue(text.startswith("<system-reminder>"))

    def test_contract_truncation_is_bounded_and_marked(self) -> None:
        long_body = "line\n" * 5000
        text = persona.build_contract(long_body, 500)
        self.assertLessEqual(len(text), 500 + 400)
        self.assertIn(persona.TRUNCATED_TAG, text)

    def test_missing_block_names_the_path(self) -> None:
        text = persona.build_missing("/tmp/nowhere/PERSONA.md")
        self.assertIn(persona.MISSING_TAG, text)
        self.assertIn("/tmp/nowhere/PERSONA.md", text)
        self.assertIn("外发工具", text)

    def test_history_scan_handles_dict_and_object_messages(self) -> None:
        digest = persona.contract_hash(BODY)

        class Msg:
            def __init__(self, content: str) -> None:
                self.content = content

        history = [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": [{"type": "text", "text": persona.build_contract(BODY, 4000)}]},
        ]
        self.assertTrue(persona.has_contract(history, digest))
        self.assertFalse(persona.has_contract([Msg("nope")], digest))
        self.assertTrue(persona.has_missing_notice([Msg(persona.build_missing("/x"))]))
        self.assertFalse(persona.has_missing_notice([]))

    def test_contract_is_found_in_the_api_content_sidecar(self) -> None:
        """宿主把 pre_llm_call 的 context 只写进 api_content，content 仍是用户原话。

        只看第一个非空字段的实现会认不出契约 → 每轮重复注入。
        """
        digest = persona.contract_hash(BODY)
        contract = persona.build_contract(BODY, 4000)
        history = [
            {"role": "user", "content": "今天天气怎么样", "api_content": "今天天气怎么样\n\n" + contract},
        ]
        self.assertTrue(persona.has_contract(history, digest))
        self.assertFalse(persona.has_missing_notice(history))

        class Msg:
            def __init__(self, content: str, api_content: str) -> None:
                self.content = content
                self.api_content = api_content

        self.assertTrue(persona.has_contract([Msg("在吗", contract)], digest))
        self.assertTrue(persona.has_missing_notice([Msg("在吗", persona.build_missing("/x"))]))
        # 两个字段都要扫：content 里是旧 hash、api_content 里是新 hash
        self.assertFalse(persona.has_contract(history, persona.contract_hash(BODY + "v2")))

    def test_message_texts_returns_every_field_once(self) -> None:
        msg = {"content": "hi", "api_content": "hi", "text": "extra"}
        self.assertEqual(persona.message_texts(msg), ["hi", "extra"])
        self.assertEqual(persona.message_text(msg), "hi")
        self.assertEqual(persona.message_texts(None), [])
        self.assertEqual(persona.message_texts({"content": ""}), [])

    def test_reader_caches_by_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PERSONA.md"
            path.write_text("v1", encoding="utf-8")
            reader = persona.PersonaReader(str(path))
            self.assertEqual(reader.read(), "v1")
            self.assertEqual(reader.read(), "v1")
            assert reader._key is not None
            path.write_text("v2", encoding="utf-8")
            os.utime(path, (1, 1))
            self.assertEqual(reader.read(), "v2")

    def test_reader_missing_file_returns_none(self) -> None:
        reader = persona.PersonaReader("/tmp/definitely-not-here/PERSONA.md")
        self.assertIsNone(reader.read())

    def test_resolve_path_relative_uses_cwd(self) -> None:
        self.assertEqual(persona.resolve_path("PERSONA.md", "/work/dir"), "/work/dir/PERSONA.md")
        self.assertEqual(persona.resolve_path("/abs/PERSONA.md", "/work/dir"), "/abs/PERSONA.md")
        self.assertEqual(persona.resolve_path("~/P.md", "/work"), os.path.expanduser("~/P.md"))

    def test_guard_suffix_matching(self) -> None:
        self.assertTrue(guard.matches("qq_conversation_send", ["qq_conversation_send"]))
        self.assertTrue(guard.matches("mcp__iibot__qq_conversation_send", ["qq_conversation_send"]))
        self.assertFalse(guard.matches("conversation_read", ["qq_conversation_send"]))
        self.assertFalse(guard.matches("x", []))


class RegisterTests(unittest.TestCase):
    def _ctx(self, tmp: Path, **config) -> object:
        ctx = _loader.FakeCtx(
            {
                "personaFile": str(tmp / "PERSONA.md"),
                "maxChars": 4000,
                "outboundGuard": True,
                "denyTools": ["write_file"],
            }
        )
        ctx.config.update(config)
        plugin.register(ctx)
        return ctx

    def test_register_never_touches_the_system_prompt(self) -> None:
        """角色模式只有一条路：system prompt 交给 minimal-system / 宿主 override。"""
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp), outboundGuard=False, denyTools=[])
            self.assertEqual(ctx.sections, [])
            self.assertIn("pre_llm_call", ctx.hooks)
            self.assertIn("pre_tool_call", ctx.hooks)

    def test_role_line_rides_with_the_injected_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "PERSONA.md").write_text(BODY, encoding="utf-8")
            ctx = self._ctx(Path(tmp), outboundGuard=False, denyTools=[])
            text = ctx.hooks["pre_llm_call"][0](conversation_history=[])["context"]
            self.assertIn("你在扮演 <persona-contract> 定义的角色", text)
            self.assertIn("自称本鲸", text)
            self.assertLess(text.index("你在扮演"), text.index("<persona-contract>"))

    def test_missing_companion_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp), outboundGuard=False, denyTools=[])
            # 宿主不提供 has_plugin（老版本/假 ctx）时保持沉默，不误报
            with self.assertNoLogs("persona_mode_plugin", level="WARNING"):
                plugin._warn_missing_companion(ctx)
            ctx.has_plugin = lambda name: False
            with self.assertLogs("persona_mode_plugin", level="WARNING") as logs:
                plugin._warn_missing_companion(ctx)
            self.assertIn("minimal-system", "\n".join(logs.output))
            ctx.has_plugin = lambda name: name == plugin.REQUIRED_COMPANION
            with self.assertNoLogs("persona_mode_plugin", level="WARNING"):
                plugin._warn_missing_companion(ctx)

    def test_injects_contract_once_then_stays_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PERSONA.md"
            path.write_text(BODY, encoding="utf-8")
            ctx = self._ctx(Path(tmp), outboundGuard=False, denyTools=[])
            hook = ctx.hooks["pre_llm_call"][0]
            first = hook(conversation_history=[], is_first_turn=True)
            self.assertIn("context", first)
            contract_text = first["context"]
            second = hook(conversation_history=[{"role": "user", "content": contract_text}])
            self.assertIsNone(second)

    def test_reinjects_after_compaction_drops_the_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PERSONA.md"
            path.write_text(BODY, encoding="utf-8")
            ctx = self._ctx(Path(tmp), outboundGuard=False, denyTools=[])
            hook = ctx.hooks["pre_llm_call"][0]
            hook(conversation_history=[])
            after_compaction = [{"role": "user", "content": "summary of earlier turns"}]
            self.assertIn("context", hook(conversation_history=after_compaction))

    def test_missing_persona_blocks_outbound_and_is_injected_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            pre_llm = ctx.hooks["pre_llm_call"][0]
            pre_tool = ctx.hooks["pre_tool_call"][0]
            blocked = pre_tool(tool_name="mcp__iibot__qq_conversation_send", args={})
            self.assertEqual(blocked["action"], "block")
            self.assertIn("PERSONA.md", blocked["message"])
            self.assertIsNone(pre_tool(tool_name="read_file", args={}))
            notice = pre_llm(conversation_history=[])
            self.assertIn(persona.MISSING_TAG, notice["context"])
            self.assertIsNone(pre_llm(conversation_history=[{"role": "user", "content": notice["context"]}]))

    def test_deny_tools_blocks_and_allows_when_file_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "PERSONA.md").write_text(BODY, encoding="utf-8")
            ctx = self._ctx(Path(tmp))
            pre_tool = ctx.hooks["pre_tool_call"][0]
            self.assertEqual(pre_tool(tool_name="write_file", args={})["action"], "block")
            self.assertIsNone(pre_tool(tool_name="read_file", args={}))
            self.assertIsNone(pre_tool(tool_name="qq_conversation_send", args={}))

    def test_hook_never_raises_on_bad_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp), personaFile="", maxChars="not-a-number")
            hook = ctx.hooks["pre_llm_call"][0]
            self.assertIsNotNone(hook(conversation_history=[]))


if __name__ == "__main__":
    unittest.main()
