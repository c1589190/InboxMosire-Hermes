"""End-to-end wiring: register() on a fake PluginContext, then drive the tools and /inbox."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _loader  # noqa: E402

plugin = _loader.inbox_plugin()
binding_mod = _loader.inbox_lib("binding")

KEY = "agent:main:telegram:dm:777"


class FakeNetClient:
    """Stands in for InboxClient so registration never opens a socket."""

    def __init__(self, base_url: str = "", token_ref=None, timeout_seconds: float = 10.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.token_ref = token_ref
        self.status_payload = {"revision": 2, "available": 1}
        self.items_payload = [{"itemId": "i1", "channel": "qq", "kind": "mention", "title": "hi"}]

    def status(self, inbox_id, *, channels=None):
        return dict(self.status_payload)

    def items(self, inbox_id, *, limit=3, channel=None, channels=None):
        return [dict(item) for item in self.items_payload[:limit]]

    def item(self, inbox_id, item_id):
        return {"itemId": item_id, "channel": "qq", "kind": "mention", "content": "正文"}

    def put_occupancy(self, inbox_id, session_ref, lease_seconds=300):
        return {}

    def delete_occupancy(self, inbox_id, session_ref):
        return None

    def open_events(self, inbox_id, *, last_event_id="", timeout=None):
        return iter(())


class HostCapabilityTests(unittest.TestCase):
    """The probe decides whether a wake is even attempted; it must see every host slot."""

    def _ctx(self, manager):
        from types import SimpleNamespace

        return SimpleNamespace(_manager=manager)

    def test_tui_host_is_recognised(self):
        from types import SimpleNamespace

        cap = plugin._host_inject_capability(self._ctx(SimpleNamespace(has_tui_message_injector=True)))
        self.assertEqual(cap, "tui")

    def test_gateway_host_is_recognised(self):
        from types import SimpleNamespace

        cap = plugin._host_inject_capability(
            self._ctx(SimpleNamespace(has_tui_message_injector=False, has_gateway_message_injector=True))
        )
        self.assertEqual(cap, "gateway")

    def test_cli_ref_is_recognised(self):
        from types import SimpleNamespace

        cap = plugin._host_inject_capability(
            self._ctx(SimpleNamespace(has_tui_message_injector=False, has_gateway_message_injector=False,
                                      _cli_ref=object()))
        )
        self.assertEqual(cap, "cli")

    def test_worker_process_is_none(self):
        from types import SimpleNamespace

        cap = plugin._host_inject_capability(
            self._ctx(SimpleNamespace(has_tui_message_injector=False, has_gateway_message_injector=False,
                                      _cli_ref=None))
        )
        self.assertEqual(cap, "none")

    def test_missing_manager_is_unknown(self):
        from types import SimpleNamespace

        self.assertEqual(plugin._host_inject_capability(SimpleNamespace()), "unknown")

    def test_broken_manager_is_unknown_not_none(self):
        class Boom:
            @property
            def has_tui_message_injector(self):
                raise RuntimeError("boom")

        self.assertEqual(plugin._host_inject_capability(self._ctx(Boom())), "unknown")


class RegisterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.token_path = Path(self._tmp.name) / "inbox.token"
        self.token_path.write_text("s3cr3t-value-123", encoding="utf-8")
        os.chmod(self.token_path, 0o600)
        self._real_client = binding_mod.InboxClient
        binding_mod.InboxClient = FakeNetClient
        self.ctx = _loader.FakeCtx(
            {
                "enabled": True,
                "apiUrl": "http://127.0.0.1:18765",
                "adapterTokenFile": f"file:{self.token_path}",
            }
        )
        plugin.register(self.ctx)

    def tearDown(self) -> None:
        if self.ctx.unload:
            self.ctx.unload()
        binding_mod.InboxClient = self._real_client
        self._tmp.cleanup()

    def call(self, tool: str, args: dict, **kwargs) -> dict:
        raw = self.ctx.tools[tool]["handler"](args, **kwargs)
        self.assertIsInstance(raw, str)
        return json.loads(raw)

    def test_registers_the_agreed_surface(self):
        self.assertEqual(
            sorted(self.ctx.tools),
            ["inbox_adapter_bind", "inbox_adapter_log", "inbox_adapter_status", "inbox_adapter_unbind"],
        )
        for entry in self.ctx.tools.values():
            self.assertEqual(entry["toolset"], "inbox-mosire")
            self.assertEqual(entry["schema"]["name"], entry["name"])
            self.assertIn("parameters", entry["schema"])
        self.assertIn("inbox", self.ctx.commands)
        for hook in (
            "pre_llm_call", "post_llm_call", "post_tool_call",
            "on_session_end", "on_session_reset", "on_session_delete", "on_session_finalize",
        ):
            self.assertEqual(len(self.ctx.hooks[hook]), 1, hook)
        self.assertIsNotNone(self.ctx.unload)

    def test_bind_status_log_unbind_round_trip(self):
        bound = self.call(
            "inbox_adapter_bind", {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}"},
            session_key=KEY,
        )
        self.assertTrue(bound["ok"], bound)
        self.assertEqual(bound["sessionKey"], KEY)
        self.assertEqual(bound["binding"]["inboxId"], "main")
        self.assertEqual(bound["binding"]["tokenRef"], f"file:{self.token_path}")

        status = self.call("inbox_adapter_status", {}, session_key=KEY)
        self.assertTrue(status["ok"])
        self.assertEqual(status["binding"]["lastStatus"]["revision"], 2)
        self.assertNotIn("s3cr3t-value-123", json.dumps(status))  # never echo the token value

        log = self.call("inbox_adapter_log", {"limit": 5}, session_key=KEY)
        self.assertTrue(log["ok"])
        self.assertTrue(any(entry["event"] == "binding.bind" for entry in log["entries"]))

        unbound = self.call("inbox_adapter_unbind", {}, session_key=KEY)
        self.assertTrue(unbound["ok"])
        self.assertEqual(self.call("inbox_adapter_status", {}, session_key=KEY)["code"], "NOT_BOUND")

    def test_bind_uses_settings_token_when_omitted(self):
        result = self.call("inbox_adapter_bind", {"inboxId": "main"}, session_key=KEY)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["binding"]["tokenRef"], f"file:{self.token_path}")

    def test_tools_report_missing_session_without_crashing(self):
        result = self.call("inbox_adapter_status", {})
        self.assertFalse(result["ok"])

    def test_explicit_session_id_overrides_caller(self):
        self.call(
            "inbox_adapter_bind", {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}"},
            session_key=KEY,
        )
        status = self.call("inbox_adapter_status", {"sessionId": KEY})
        self.assertTrue(status["ok"])

    def test_slash_command(self):
        slash = self.ctx.commands["inbox"]
        self.assertIn("session key", slash("status", session_key=""))
        out = slash("bind main", session_key=KEY)
        self.assertIn("main", out)
        self.assertIn("inbox", slash("status", session_key=KEY))
        self.assertIn("/inbox", slash("help", session_key=KEY))

    def test_hooks_mark_turn_state(self):
        self.call(
            "inbox_adapter_bind", {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}"},
            session_key=KEY,
        )
        plugin._on_pre_llm_call(session_key=KEY)
        status = self.call("inbox_adapter_status", {}, session_key=KEY)
        self.assertEqual(status["binding"]["health"]["turn"], "running")
        plugin._on_post_llm_call(session_key=KEY)
        status = self.call("inbox_adapter_status", {}, session_key=KEY)
        self.assertEqual(status["binding"]["health"]["turn"], "idle")
        plugin._on_session_gone(session_key=KEY)
        self.assertEqual(self.call("inbox_adapter_status", {}, session_key=KEY)["binding"]["health"]["sse"], "suspended")

    def test_session_end_is_per_turn_and_does_not_suspend(self):
        """Hermes fires ``on_session_end`` after every turn (hermes_cli/lifecycle.py): a wake adapter
        must read it as "turn finished", not "session died", or its SSE worker stops after turn one."""
        self.call(
            "inbox_adapter_bind", {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}"},
            session_key=KEY,
        )
        self.ctx.hooks["on_session_end"][0](session_key=KEY)
        status = self.call("inbox_adapter_status", {}, session_key=KEY)
        self.assertNotEqual(status["binding"]["health"]["sse"], "suspended")
        self.assertEqual(status["binding"]["health"]["turn"], "idle")

    def test_turn_start_revives_a_suspended_worker(self):
        self.call(
            "inbox_adapter_bind", {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}"},
            session_key=KEY,
        )
        plugin._on_session_gone(session_key=KEY)
        status = self.call("inbox_adapter_status", {}, session_key=KEY)
        self.assertEqual(status["binding"]["health"]["sse"], "suspended")
        plugin._on_pre_llm_call(session_key=KEY)
        status = self.call("inbox_adapter_status", {}, session_key=KEY)
        self.assertNotEqual(status["binding"]["health"]["sse"], "suspended")

    def test_inject_hint_when_host_refuses(self):
        refusing = _loader.FakeCtx(accept_injection=False)
        inject = plugin._make_inject(refusing)
        ok, error = inject("hello", KEY)
        self.assertFalse(ok)
        self.assertIn("allow_gateway_injection", error)
        ok, _ = plugin._make_inject(self.ctx)("hello", KEY)
        self.assertTrue(ok)
        self.assertEqual(self.ctx.injected[-1], ("hello", KEY))


class NoTokenConfigTests(unittest.TestCase):
    def test_bind_without_any_token_reference_reports_token_missing(self):
        ctx = _loader.FakeCtx({"enabled": False})
        plugin.register(ctx)
        try:
            raw = ctx.tools["inbox_adapter_bind"]["handler"]({"inboxId": "main"}, session_key=KEY)
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "TOKEN_MISSING")
        finally:
            if ctx.unload:
                ctx.unload()


class DisabledConfigTests(unittest.TestCase):
    def test_disabled_plugin_registers_but_does_not_resume(self):
        ctx = _loader.FakeCtx({"enabled": False})
        plugin.register(ctx)
        self.assertEqual(len(ctx.tools), 4)
        self.assertIsNotNone(ctx.unload)
        ctx.unload()


if __name__ == "__main__":
    unittest.main()
