"""Wake-decision tests: dedupe, backoff, turn-running deferral, summary degradation."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _loader  # noqa: E402

binding_mod = _loader.inbox_lib("binding")
config_mod = _loader.inbox_lib("config")
state_mod = _loader.inbox_lib("state")
client_mod = _loader.inbox_lib("client")

KEY = "agent:main:telegram:dm:4242"
SID = "session-4242"


class FakeClient:
    def __init__(self) -> None:
        self.status_payload = {"revision": 1, "available": 0}
        self.items_payload: list[dict] = []
        self.items_error: client_mod.InboxError | None = None
        self.status_error: client_mod.InboxError | None = None
        self.occupancy: list[tuple] = []

    def status(self, inbox_id: str) -> dict:
        if self.status_error:
            raise self.status_error
        return dict(self.status_payload)

    def items(self, inbox_id: str, *, limit: int = 3, channel=None) -> list[dict]:
        if self.items_error:
            raise self.items_error
        return [dict(item) for item in self.items_payload[:limit]]

    def put_occupancy(self, inbox_id: str, session_ref: str, lease_seconds: int = 300) -> dict:
        self.occupancy.append(("put", session_ref))
        return {}

    def delete_occupancy(self, inbox_id: str, session_ref: str) -> None:
        self.occupancy.append(("delete", session_ref))

    def open_events(self, inbox_id: str, *, last_event_id: str = ""):
        return iter(())


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_000.0
        self.injected: list[tuple[str, str]] = []
        self.inject_ok = True
        self.inject_error = "host rejected injection"
        self.ctx = _loader.FakeCtx()
        self.store = state_mod.Store(self.ctx)
        self.config = config_mod.Config.from_mapping({})
        self.manager = binding_mod.BindingManager(
            self.config, self.store, self._inject, clock=lambda: self.now
        )
        self.fake = FakeClient()
        self.manager._client = lambda record: self.fake

    def tearDown(self) -> None:
        self.manager.shutdown()

    def _inject(self, text: str, session_key: str) -> tuple[bool, str]:
        self.injected.append((text, session_key))
        return (True, "") if self.inject_ok else (False, self.inject_error)

    def seed(self, **overrides) -> dict:
        record = binding_mod.new_record(
            session_key=KEY,
            inbox_id="main",
            api_base_url="http://127.0.0.1:18765",
            token_ref="file:/tmp/inbox.token",
            channels=[],
            wake_mode="claim",
            wake_profile="generic",
            session_id=SID,
        )
        record.update(overrides)
        self.store.put_binding(KEY, record)
        self.manager._runtime_for(KEY)
        return record

    def wake(self, reason: str = "test") -> None:
        self.manager._maybe_wake(KEY, reason)


class WakeDecisionTests(Base):
    def test_wakes_once_per_revision(self):
        self.seed()
        self.fake.status_payload = {"revision": 3, "available": 2}
        self.fake.items_payload = [
            {"itemId": "i1", "channel": "qq", "kind": "mention", "title": "有人 @ 你"},
            {"itemId": "i2", "channel": "qq", "kind": "reply", "title": "回复你"},
        ]
        self.wake()
        self.assertEqual(len(self.injected), 1)
        text, key = self.injected[0]
        self.assertEqual(key, KEY)
        self.assertIn("[INBOX_HOST_EVENT]", text)
        self.assertIn("inbox=main", text)
        self.assertIn("revision=3", text)
        self.assertIn("有人 @ 你", text)
        record = self.store.get_binding(KEY)
        self.assertEqual(record["lastWokenRevision"], 3)
        self.assertTrue(record["lastWake"]["ok"])
        self.assertTrue(record["lastWake"]["messageId"].startswith("msg_inbox_"))

        self.wake()  # same revision → no second wake
        self.assertEqual(len(self.injected), 1)

    def test_new_revision_wakes_again(self):
        self.seed(lastWokenRevision=3)
        self.fake.status_payload = {"revision": 4, "available": 1}
        self.wake()
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.store.get_binding(KEY)["lastWokenRevision"], 4)

    def test_empty_inbox_advances_watermark_without_waking(self):
        self.seed(lastWokenRevision=1)
        self.fake.status_payload = {"revision": 9, "available": 0}
        self.wake()
        self.assertEqual(self.injected, [])
        self.assertEqual(self.store.get_binding(KEY)["lastWokenRevision"], 9)

    def test_failed_injection_backs_off(self):
        self.seed()
        self.inject_ok = False
        self.fake.status_payload = {"revision": 5, "available": 1}
        self.wake()
        record = self.store.get_binding(KEY)
        self.assertFalse(record["lastWake"]["ok"])
        self.assertEqual(record["wake"]["attempts"], 1)
        self.assertGreater(record["wake"]["nextWakeAllowedAt"], self.now * 1000)

        self.wake()  # inside the backoff window
        self.assertEqual(len(self.injected), 1)

        self.now += 31  # first backoff step is 30s
        self.inject_ok = True
        self.wake()
        self.assertEqual(len(self.injected), 2)
        record = self.store.get_binding(KEY)
        self.assertEqual(record["wake"]["attempts"], 0)
        self.assertEqual(record["lastWokenRevision"], 5)

    def test_turn_running_defers_then_flushes(self):
        self.seed()
        self.fake.status_payload = {"revision": 6, "available": 1}
        self.manager.on_turn_start(KEY)
        self.wake()
        self.assertEqual(self.injected, [])
        pending = self.store.get_binding(KEY).get("pendingWake")
        self.assertEqual(pending["revision"], 6)

        self.manager.on_turn_end(KEY)
        self.wake("pending-wake")
        self.assertEqual(len(self.injected), 1)

    def test_summary_failure_still_wakes(self):
        self.seed()
        self.fake.status_payload = {"revision": 2, "available": 1}
        self.fake.items_error = client_mod.InboxError("TIMEOUT", "slow")
        self.wake()
        self.assertEqual(len(self.injected), 1)
        record = self.store.get_binding(KEY)
        self.assertEqual(record["summary"]["state"], "failed")
        self.assertIn("TIMEOUT", self.store.get_binding(KEY)["summary"]["code"])

    def test_status_failure_does_not_wake(self):
        self.seed(lastWokenRevision=1)
        self.fake.status_error = client_mod.InboxError("TRANSPORT", "down")
        self.wake()
        self.assertEqual(self.injected, [])

    def test_available_zero_when_first_seen_does_not_wake(self):
        self.seed(lastWokenRevision=0)
        self.fake.status_payload = {"revision": 0, "available": 0}
        self.wake()
        self.assertEqual(self.injected, [])

    def test_plain_text_summary_keeps_body_out(self):
        self.config.wake_content = "off"
        self.seed()
        self.fake.status_payload = {"revision": 8, "available": 1}
        self.fake.items_payload = [
            {"itemId": "i9", "channel": "qq", "kind": "mention", "title": "标题", "content": "正文不该出现"}
        ]
        self.wake()
        text, _ = self.injected[0]
        self.assertNotIn("正文不该出现", text)

    def test_wake_content_bounded_opt_in_includes_body(self):
        self.config.wake_content = "bounded"
        self.seed()
        self.fake.status_payload = {"revision": 8, "available": 1}
        self.fake.items_payload = [
            {"itemId": "i9", "channel": "qq", "kind": "mention", "title": "标题", "content": "正文出现了"}
        ]
        self.wake()
        text, _ = self.injected[0]
        self.assertIn("正文出现了", text)


class RestAndTtlTests(Base):
    def test_rest_seconds_defers_wakes(self):
        self.seed()
        self.fake.status_payload = {"revision": 4, "available": 1}
        self.manager.on_tool_result(SID, "mcp__inbox-mosire__inbox_rest", '{"ok": true, "restSeconds": 600}')
        record = self.store.get_binding(KEY)
        self.assertGreaterEqual(record["wake"]["nextWakeAllowedAt"], self.now * 1000 + 600_000)
        self.wake()
        self.assertEqual(self.injected, [])
        self.now += 601
        self.wake()
        self.assertEqual(len(self.injected), 1)

    def test_rest_from_dict_and_other_tools_ignored(self):
        self.seed()
        self.manager.on_tool_result(SID, "mcp__inbox-mosire__inbox_claim", {"restSeconds": 60})
        self.assertEqual(self.store.get_binding(KEY)["wake"]["nextWakeAllowedAt"], 0)
        self.manager.on_tool_result(SID, "inbox_rest", {"result": {"restSeconds": 30}})
        self.assertGreater(self.store.get_binding(KEY)["wake"]["nextWakeAllowedAt"], self.now * 1000)

    def test_pending_wake_expires_after_ttl(self):
        self.config.pending_ttl_seconds = 10
        self.seed()
        self.fake.status_payload = {"revision": 6, "available": 1}
        self.manager.on_turn_start(KEY)
        self.wake()
        self.manager.on_turn_start(KEY)
        self.now += 60  # pending is now stale
        self.wake()
        record = self.store.get_binding(KEY)
        self.assertEqual(record["pendingWake"]["revision"], 6)  # replaced by the fresh one
        self.manager.on_turn_end(KEY)
        self.wake("pending-wake")
        self.assertEqual(len(self.injected), 1)


class SessionLifecycleTests(Base):
    def test_session_gone_suspends(self):
        self.seed()
        self.manager.on_session_gone(KEY, reason="session-end")
        self.assertEqual(self.store.get_binding(KEY)["health"]["sse"], "suspended")

    def test_sse_event_updates_cursor_and_arms_debounce(self):
        """Regression: _on_event used to raise TypeError (_log() got multiple values for
        argument 'event'), which killed every SSE-driven wake on the real host."""
        self.seed()
        self.fake.status_payload = {"revision": 2, "available": 1}
        self.manager._on_event(KEY, "7", {"event": "inbox.updated", "data": {"revision": 2}})
        record = self.store.get_binding(KEY)
        self.assertEqual(record["lastEventId"], "7")
        runtime = self.manager._runtime_for(KEY)
        self.assertIsNotNone(runtime.timer)  # debounce timer armed → handler did not blow up
        runtime.timer.cancel()
        self.wake("event")
        self.assertEqual(len(self.injected), 1)
        self.assertIn("revision=2", self.injected[0][0])
        events = [entry.get("event") for entry in self.store.ops().get(KEY, [])]
        self.assertIn("sse.event", events)
        self.assertIn("wake.ok", events)

    def test_lookup_by_session_id(self):
        self.seed()
        self.assertEqual(self.manager.by_session_id(SID), KEY)
        self.assertEqual(self.manager.by_session_id("nope"), "")


class BindTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.token_path = Path(self._tmp.name) / "inbox.token"
        self.token_path.write_text("tok", encoding="utf-8")
        os.chmod(self.token_path, 0o600)

    def tearDown(self) -> None:
        super().tearDown()
        self._tmp.cleanup()

    def test_bind_persists_and_starts(self):
        result = self.manager.bind(
            KEY, {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}", "channels": ["qq"]}
        )
        self.assertTrue(result["ok"])
        record = self.store.get_binding(KEY)
        self.assertEqual(record["inboxId"], "main")
        self.assertEqual(record["channels"], ["qq"])
        self.assertEqual(record["sessionId"], KEY)
        self.assertIn(("put", KEY), self.fake.occupancy)

    def test_bind_rejects_bad_arguments(self):
        cases = [
            ({"inboxId": "bad id"}, "INVALID_ARGUMENT"),
            ({"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}", "wakeMode": "claim-all"}, "INVALID_ARGUMENT"),
            ({"inboxId": "main", "adapterTokenRef": "inline:secret"}, "TOKEN_INLINE_FORBIDDEN"),
            ({"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}", "wakeProfile": "nope"}, "INVALID_ARGUMENT"),
            ({"inboxId": "main"}, "TOKEN_MISSING"),
        ]
        for params, code in cases:
            result = self.manager.bind(KEY, params)
            self.assertFalse(result["ok"], msg=str(params))
            self.assertEqual(result["code"], code, msg=str(params))
        self.assertIsNone(self.store.get_binding(KEY))
        result = self.manager.bind("bad key with spaces", {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}"})
        self.assertEqual(result["code"], "NO_SESSION_KEY")

    def test_rebind_keeps_watermarks(self):
        self.manager.bind(KEY, {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}"})
        record = self.store.get_binding(KEY)
        record["lastWokenRevision"] = 11
        record["lastEventId"] = "e-11"
        self.store.put_binding(KEY, record)
        self.manager.bind(KEY, {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}"})
        record = self.store.get_binding(KEY)
        self.assertEqual(record["lastWokenRevision"], 11)
        self.assertEqual(record["lastEventId"], "e-11")

    def test_max_bindings(self):
        self.config.max_bindings = 1
        self.manager.bind(KEY, {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}"})
        result = self.manager.bind("agent:main:other", {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}"})
        self.assertEqual(result["code"], "TOO_MANY_BINDINGS")

    def test_unbind_removes_and_vacates(self):
        self.manager.bind(KEY, {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}"})
        self.manager.unbind(KEY)
        self.assertIsNone(self.store.get_binding(KEY))
        self.assertIn(("delete", KEY), self.fake.occupancy)
        self.assertEqual(self.manager.unbind(KEY)["code"], "NOT_BOUND")

    def test_descriptor_json_string(self):
        descriptor = '{"inboxId": "main", "adapterTokenRef": "file:%s"}' % self.token_path
        self.assertTrue(self.manager.bind(KEY, {"descriptor": descriptor})["ok"])

    def test_status_and_log(self):
        self.manager.bind(KEY, {"inboxId": "main", "adapterTokenRef": f"file:{self.token_path}"})
        self.fake.status_payload = {"revision": 12, "available": 4}
        status = self.manager.status(KEY)
        self.assertTrue(status["ok"])
        self.assertEqual(status["binding"]["lastStatus"]["revision"], 12)
        self.assertEqual(status["binding"]["lastStatus"]["available"], 4)
        self.assertIn(status["binding"]["health"]["programMcp"], {"ready", "partial", "missing", "unknown"})
        log = self.manager.log(KEY, limit=10)
        self.assertTrue(log["ok"])
        self.assertTrue(any(entry["event"] == "binding.bind" for entry in log["entries"]))


if __name__ == "__main__":
    unittest.main()
