"""1.0.0 合并：一个插件、三段设置、旧命名空间只读回落、两个默认关掉的开关。

覆盖规格里点名的四条（旧 inbox-wake 回落 / 新 settings.inbox 覆盖 / persona 默认不注册 /
prompt 默认不注册中间件），外加 prompt·persona 的回落与 ``allow_gateway_injection`` 的搬迁告警。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _loader  # noqa: E402

plugin = _loader.plugin()
settings_lib = _loader.settings_lib()

TOKENLESS_INBOX = {"enabled": False}  # 不 resume、不开 SSE，register 只做装配


def _prompt_middleware(ctx):
    """``llm_request`` 上 prompt 那一条（vision 中间件默认也在，按身份挑）。"""
    return [
        callback for callback in ctx.middleware.get("llm_request", [])
        if callback is plugin._prompt_on_llm_request
    ]


def register(ctx) -> None:
    plugin.register(ctx)
    if ctx.unload:
        ctx.unload()


class LegacyInboxSettingsTests(unittest.TestCase):
    """① 旧 ``plugins.entries.inbox-wake.settings`` 仍被读取（并告警一次）。"""

    def test_legacy_inbox_entry_is_read_with_a_warning(self) -> None:
        ctx = _loader.FakeCtx(legacy={"inbox-wake": {
            "enabled": False,
            "apiUrl": "http://10.0.0.9:18765",
            "inboxId": "legacy-inbox",
            "wakeMode": "claim",
            "pushMode": "on",
            "pushGraceMs": 750,
        }})
        with self.assertLogs("iibot_hermes_plugin", level="WARNING") as logs:
            register(ctx)
        text = "\n".join(logs.output)
        self.assertIn("已迁移到 plugins.entries.iibot-hermes.settings.inbox", text)
        self.assertIn("plugins.entries.inbox-wake.settings", text)
        self.assertIn("只读", text)
        self.assertEqual(plugin._wake_config.api_url, "http://10.0.0.9:18765")
        self.assertEqual(plugin._wake_config.inbox_id, "legacy-inbox")
        self.assertEqual(plugin._wake_config.wake_mode, "claim")
        self.assertEqual(plugin._wake_config.push_mode, "on")
        self.assertEqual(plugin._wake_config.push_grace_ms, 750)

    def test_legacy_settings_are_never_written_back(self) -> None:
        ctx = _loader.FakeCtx(legacy={"inbox-wake": dict(TOKENLESS_INBOX)})
        before = {key: dict(value) for key, value in ctx.legacy.items()}
        register(ctx)
        self.assertEqual(ctx.legacy, before)


class NewInboxSettingsTests(unittest.TestCase):
    """② 新 ``settings.inbox`` 存在时覆盖旧值，旧段被忽略。"""

    def test_new_inbox_settings_override_the_legacy_entry(self) -> None:
        ctx = _loader.FakeCtx(
            {"inbox": {"enabled": False, "apiUrl": "http://127.0.0.1:19999", "inboxId": "new-inbox"}},
            legacy={"inbox-wake": {"enabled": False, "apiUrl": "http://10.0.0.9:18765", "inboxId": "old"}},
        )
        with self.assertLogs("iibot_hermes_plugin", level="WARNING") as logs:
            register(ctx)
        text = "\n".join(logs.output)
        self.assertIn("仍存在但已被忽略", text)
        self.assertIn("iibot-hermes.settings.inbox", text)
        self.assertEqual(plugin._wake_config.api_url, "http://127.0.0.1:19999")
        self.assertEqual(plugin._wake_config.inbox_id, "new-inbox")


class DefaultOffTests(unittest.TestCase):
    """③④ 默认配置不接管 persona / system prompt。

    0.4.0-fix1 起 ``llm_request`` 上**始终**有 vision 中间件（把 tool 里的图搬进 user 消息，
    默认开、无图是 no-op），所以这里的断言按身份看 prompt 那一条，而不是看 ``llm_request`` 键。
    """


    def test_empty_settings_register_only_the_wake_surface(self) -> None:
        ctx = _loader.FakeCtx({"inbox": dict(TOKENLESS_INBOX)})
        register(ctx)
        self.assertEqual(sorted(ctx.tools), [
            "inbox_adapter_bind", "inbox_adapter_log", "inbox_adapter_status", "inbox_adapter_unbind",
        ])
        self.assertIn("inbox", ctx.commands)
        # ③ persona.enabled 默认 false → 没有 pre_tool_call，pre_llm_call 只有 wake 那一条
        self.assertNotIn("pre_tool_call", ctx.hooks)
        self.assertEqual(len(ctx.hooks["pre_llm_call"]), 1)
        self.assertIs(ctx.hooks["pre_llm_call"][0], plugin._on_pre_llm_call)
        # ④ prompt.enabled 默认 false → 不挂 prompt 中间件（vision 那条照挂）
        self.assertEqual(_prompt_middleware(ctx), [])
        self.assertEqual(len(ctx.middleware["llm_request"]), 1)
        self.assertIs(ctx.middleware["llm_request"][0], plugin._vision_on_llm_request)
        self.assertFalse(plugin._prompt_config["enabled"])
        self.assertFalse(plugin._persona_config["enabled"])

    def test_prompt_disabled_explicitly_registers_no_prompt_middleware(self) -> None:
        ctx = _loader.FakeCtx({"prompt": {"enabled": False}, "inbox": dict(TOKENLESS_INBOX)})
        register(ctx)
        self.assertEqual(_prompt_middleware(ctx), [])

    def test_persona_disabled_explicitly_registers_no_hooks(self) -> None:
        ctx = _loader.FakeCtx({"persona": {"enabled": False}, "inbox": dict(TOKENLESS_INBOX)})
        register(ctx)
        self.assertNotIn("pre_tool_call", ctx.hooks)
        self.assertEqual(len(ctx.hooks["pre_llm_call"]), 1)


class OptInTests(unittest.TestCase):
    """打开开关后才出现的面，且旧命名空间同样能打开它们。"""

    def test_prompt_enabled_registers_the_middleware(self) -> None:
        ctx = _loader.FakeCtx(
            {"prompt": {"enabled": True, "systemPrompt": "one line"}, "inbox": dict(TOKENLESS_INBOX)}
        )
        register(ctx)
        self.assertEqual(len(_prompt_middleware(ctx)), 1)
        result = _prompt_middleware(ctx)[0](
            request={"messages": [{"role": "system", "content": "host"}, {"role": "user", "content": "hi"}]},
            api_mode="chat_completions",
        )
        self.assertEqual(result["request"]["messages"][0]["content"], "one line")
        self.assertEqual(result["source"], "minimal-system")

    def test_persona_enabled_registers_both_hooks(self) -> None:
        ctx = _loader.FakeCtx(
            {"persona": {"enabled": True, "personaFile": "PERSONA.md"}, "inbox": dict(TOKENLESS_INBOX)}
        )
        register(ctx)
        self.assertIn("pre_tool_call", ctx.hooks)
        self.assertEqual(len(ctx.hooks["pre_llm_call"]), 2)
        self.assertIs(ctx.hooks["pre_llm_call"][-1], plugin._persona_pre_llm_call)

    def test_legacy_prompt_and_persona_entries_still_work(self) -> None:
        ctx = _loader.FakeCtx(
            legacy={
                "minimal-system": {"enabled": True, "systemPrompt": "legacy line"},
                "persona-mode": {"enabled": True, "personaFile": "PERSONA.md"},
            },
        )
        with self.assertLogs("iibot_hermes_plugin", level="WARNING") as logs:
            register(ctx)
        text = "\n".join(logs.output)
        self.assertIn("iibot-hermes.settings.prompt", text)
        self.assertIn("iibot-hermes.settings.persona", text)
        self.assertEqual(len(_prompt_middleware(ctx)), 1)
        self.assertIn("pre_tool_call", ctx.hooks)

    def test_new_section_wins_over_legacy_for_prompt(self) -> None:
        ctx = _loader.FakeCtx(
            {"prompt": {"enabled": False}},
            legacy={"minimal-system": {"enabled": True}},
        )
        register(ctx)
        self.assertEqual(_prompt_middleware(ctx), [])


class StartupLogTests(unittest.TestCase):
    """启动一行生效值（不含 token），另加旧 ``allow_gateway_injection`` 的搬迁告警。"""

    def test_one_line_effective_settings_are_logged(self) -> None:
        ctx = _loader.FakeCtx({"inbox": {
            "enabled": False,
            "apiUrl": "http://127.0.0.1:18765",
            "inboxId": "main",
            "wakeMode": "view-ack",
            "pushMode": "auto",
            "firstRunGuide": True,
        }})
        with self.assertLogs("iibot_hermes_plugin", level="INFO") as logs:
            register(ctx)
        line = [rec for rec in logs.output if "iibot-hermes: effective settings" in rec]
        self.assertEqual(len(line), 1, logs.output)
        self.assertIn("apiUrl=http://127.0.0.1:18765", line[0])
        self.assertIn("inboxId=main", line[0])
        self.assertIn("wakeMode=view-ack", line[0])
        self.assertIn("pushMode=auto", line[0])
        self.assertIn("firstRunGuide=True", line[0])
        self.assertIn("prompt.enabled=False", line[0])
        self.assertIn("persona.enabled=False", line[0])
        self.assertIn("vision.enabled=True", line[0])

    def test_legacy_gateway_flag_warns_about_the_new_key(self) -> None:
        ctx = _loader.FakeCtx(
            {"inbox": dict(TOKENLESS_INBOX)},
            entries={"inbox-wake": {"allow_gateway_injection": True}},
        )
        with self.assertLogs("iibot_hermes_plugin", level="WARNING") as logs:
            register(ctx)
        text = "\n".join(logs.output)
        self.assertIn("plugins.entries.iibot-hermes.allow_gateway_injection: true", text)

    def test_own_gateway_flag_is_silent(self) -> None:
        ctx = _loader.FakeCtx(
            {"inbox": dict(TOKENLESS_INBOX)},
            entries={"iibot-hermes": {"allow_gateway_injection": True}},
        )
        with self.assertLogs("iibot_hermes_plugin", level="INFO") as logs:
            register(ctx)
        self.assertNotIn("allow_gateway_injection", "\n".join(logs.output))


class SectionResolverTests(unittest.TestCase):
    """``lib/settings.py`` 的纯解析逻辑（不经 register）。"""

    def test_dotted_and_nested_spellings_are_equivalent(self) -> None:
        nested = _loader.FakeCtx({"inbox": {"apiUrl": "http://a"}})
        dotted = _loader.FakeCtx({"inbox.apiUrl": "http://a"})
        self.assertEqual(settings_lib.load_section(nested, "inbox"), {"apiUrl": "http://a"})
        self.assertEqual(settings_lib.load_section(dotted, "inbox"), {"apiUrl": "http://a"})

    def test_missing_everywhere_is_empty(self) -> None:
        self.assertEqual(settings_lib.load_section(_loader.FakeCtx(), "persona"), {})

    def test_unknown_section_is_rejected(self) -> None:
        with self.assertRaises(KeyError):
            settings_lib.load_section(_loader.FakeCtx(), "nope")


if __name__ == "__main__":
    unittest.main()
