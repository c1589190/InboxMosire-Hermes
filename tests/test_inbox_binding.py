"""Wake-decision tests: dedupe, backoff, turn-running deferral, summary degradation."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _loader  # noqa: E402

binding_mod = _loader.inbox_lib("binding")
config_mod = _loader.inbox_lib("config")
state_mod = _loader.inbox_lib("state")
client_mod = _loader.inbox_lib("client")
commands_mod = _loader.inbox_lib("commands")

KEY = "agent:main:telegram:dm:4242"
SID = "session-4242"


class FakeClient:
    def __init__(self) -> None:
        self.status_payload = {"revision": 1, "available": 0}
        self.items_payload: list[dict] = []
        self.items_error: client_mod.InboxError | None = None
        self.status_error: client_mod.InboxError | None = None
        self.occupancy: list[tuple] = []
        self.event_timeouts: list = []

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

    def open_events(self, inbox_id: str, *, last_event_id: str = "", timeout=None):
        self.event_timeouts.append(timeout)
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


class PassiveBindingTests(Base):
    """One event must not fan out into one agent per ghost session."""

    def _record(self, key: str, *, host: str = "tui", updated: str = "2026-11-02T22:00:00Z",
                inbox: str = "main") -> dict:
        record = binding_mod.new_record(
            session_key=key,
            inbox_id=inbox,
            api_base_url="http://127.0.0.1:18765",
            token_ref="file:/tmp/inbox.token",
            channels=[],
            wake_mode="view-ack",
            wake_profile="generic",
            session_id=key.split(":")[-1],
        )
        record["host"] = host
        record["updatedAt"] = updated
        self.store.put_binding(key, record)
        self.manager._runtime_for(key)
        return record

    def test_bind_refuses_a_host_that_cannot_deliver(self):
        self.manager.set_inject_capability("none")
        result = self.manager.bind(KEY, {"inboxId": "main"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "NO_INJECT_HOST")
        self.assertIsNone(self.store.get_binding(KEY))

    def test_resume_prunes_ghost_sessions_to_the_newest(self):
        keys = [f"agent:main:local:dm:local-{i:04d}" for i in range(3)]
        for index, key in enumerate(keys):
            self._record(key, updated=f"2026-11-02T22:0{index}:00Z")
        started = self.manager.resume_all()
        self.assertEqual(started, 1)
        active = [k for k in keys if not (self.store.get_binding(k) or {}).get("passive")]
        self.assertEqual(active, [keys[-1]])
        events = [entry.get("event") for entry in self.store.ops().get(keys[0], [])]
        self.assertIn("binding.passive", events)

    def test_resume_skips_bindings_made_by_a_dead_host(self):
        ghost = self._record("agent:main:local:dm:local-ghost", host="none")
        self.assertEqual(self.manager.resume_all(), 0)
        self.assertIn("binding.passive", [e.get("event") for e in self.store.ops().get(ghost["sessionKey"], [])])

    def test_passive_binding_never_wakes(self):
        record = self._record(KEY)
        record["passive"] = True
        self.store.put_binding(KEY, record)
        self.fake.status_payload = {"revision": 4, "available": 2}
        self.wake()
        self.assertEqual(self.injected, [])
        events = [entry.get("event") for entry in self.store.ops().get(KEY, [])]
        self.assertEqual(events.count("wake.skipped"), 1)
        self.assertNotIn("wake.ok", events)

    def test_bind_demotes_an_older_binding_of_the_same_inbox(self):
        self.manager.set_inject_capability("tui")
        old = self._record("agent:main:local:dm:local-old", updated="2026-11-02T21:00:00Z")
        new = self._record("agent:main:local:dm:local-new", updated="2026-11-02T22:00:00Z")
        self.manager._demote_siblings(new)
        self.assertTrue((self.store.get_binding(old["sessionKey"]) or {}).get("passive"))
        self.assertFalse((self.store.get_binding(new["sessionKey"]) or {}).get("passive"))


class OwnershipTests(Base):
    """Exactly one session should eat the inbox: an explicit bind pins, autoBind never steals."""

    SECOND = "agent:main:local:dm:local-second"

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "inbox.token"
        path.write_text("tok", encoding="utf-8")
        os.chmod(path, 0o600)
        self.config.adapter_token_file = f"file:{path}"

    def _bind(self, key: str, source: str) -> dict:
        self.manager.set_inject_capability("tui")
        result = self.manager.bind(key, {"source": source})
        self.assertTrue(result.get("ok"), result)
        return self.store.get_binding(key) or {}

    def _record(self, key: str, *, source: str, updated: str) -> dict:
        record = binding_mod.new_record(
            session_key=key,
            inbox_id="main",
            api_base_url="http://127.0.0.1:18765",
            token_ref="file:/tmp/inbox.token",
            channels=[],
            wake_mode="view-ack",
            wake_profile="generic",
            session_id=key.split(":")[-1],
        )
        record["host"] = "tui"
        record["bindSource"] = source
        record["updatedAt"] = updated
        self.store.put_binding(key, record)
        self.manager._runtime_for(key)
        return record

    def test_manual_bind_pins_and_autobind_defers(self):
        self._bind(KEY, "manual")
        auto = self._bind(self.SECOND, "auto")
        self.assertTrue(auto.get("passive"))
        self.assertEqual(auto.get("passiveReason"), "manual-holder")
        self.assertFalse((self.store.get_binding(KEY) or {}).get("passive"))
        events = [e.get("event") for e in self.store.ops().get(self.SECOND, [])]
        self.assertIn("binding.passive", events)

    def test_manual_bind_supersedes_an_active_autobind(self):
        self._bind(self.SECOND, "auto")
        self._bind(KEY, "manual")
        self.assertTrue((self.store.get_binding(self.SECOND) or {}).get("passive"))
        self.assertFalse((self.store.get_binding(KEY) or {}).get("passive"))

    def test_prune_prefers_manual_over_a_newer_autobind(self):
        self._record("agent:main:local:dm:local-a", source="auto", updated="2026-11-02T21:00:00Z")
        self._record("agent:main:local:dm:local-b", source="auto", updated="2026-11-02T23:00:00Z")
        self._record("agent:main:local:dm:local-c", source="manual", updated="2026-11-02T20:00:00Z")
        self.assertEqual(self.manager.resume_all(), 1)
        active = [
            key
            for key in ("agent:main:local:dm:local-a", "agent:main:local:dm:local-b", "agent:main:local:dm:local-c")
            if not (self.store.get_binding(key) or {}).get("passive")
        ]
        self.assertEqual(active, ["agent:main:local:dm:local-c"])

    def test_owners_lists_active_then_passive(self):
        self._bind(KEY, "manual")
        self._bind(self.SECOND, "auto")
        owners = self.manager.owners()
        self.assertEqual(len(owners), 2)
        self.assertTrue(owners[0]["active"])
        self.assertEqual(owners[0]["source"], "manual")
        self.assertEqual(owners[0]["sessionKey"], KEY)
        self.assertFalse(owners[1]["active"])
        self.assertEqual(owners[1]["passiveReason"], "manual-holder")
        text = commands_mod.handle("owners", self.manager, KEY)
        self.assertIn("活跃 1 条", text)
        self.assertIn("* ACTIVE", text)
        self.assertIn("passive (manual-holder)", text)
        self.assertIn("agent:main:local:dm:local-second", text)

    def test_status_shows_ownership(self):
        self._bind(KEY, "manual")
        text = commands_mod.handle("status", self.manager, KEY)
        self.assertIn("source=manual", text)
        self.assertIn("active     : yes", text)
        auto = self._bind(self.SECOND, "auto")
        self.assertTrue(auto.get("passive"))
        text = commands_mod.handle("status", self.manager, self.SECOND)
        self.assertIn("passive (manual-holder)", text)


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


class InjectHostTests(Base):
    """Only a gateway / classic-CLI host can deliver a wake; a one-shot worker must stay quiet."""

    def test_manager_is_injectable_by_default(self):
        self.assertNotEqual(self.manager.inject_capability, "none")
        self.manager.set_inject_capability("gateway")
        self.seed()
        self.fake.status_payload = {"revision": 5, "available": 1}
        self.wake()
        self.assertEqual(len(self.injected), 1)

    def test_no_inject_host_skips_once_and_never_backs_off(self):
        self.manager.set_inject_capability("none")
        record = self.seed()
        self.fake.status_payload = {"revision": 5, "available": 1}
        self.wake()
        self.wake()  # same revision: the skip is logged once, not per event
        self.assertEqual(self.injected, [], "injectless host must not call inject")
        events = [entry.get("event") for entry in self.store.ops().get(KEY, [])]
        self.assertEqual(events.count("wake.skipped"), 1)
        self.assertNotIn("wake.failed", events)
        self.assertEqual(events.count("wake.ok"), 0)
        self.assertEqual(int(record.get("lastWokenRevision") or 0), 0)
        self.assertEqual((record.get("wake") or {}).get("attempts") or 0, 0)
        self.assertIn((record.get("wake") or {}).get("nextWakeAllowedAt"), (0, None))  # 未设退避

    def test_tui_host_delivers_like_the_gateway(self):
        """The Ink TUI / desktop backend owns its sessions, so a keyed wake goes through
        ``inject_tui_session_message``; the plugin only needs to know it is not a worker."""
        self.manager.set_inject_capability("tui")
        record = self.seed()
        self.fake.status_payload = {"revision": 7, "available": 1}
        self.wake()
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(int(record.get("lastWokenRevision") or 0), 7)
        events = [entry.get("event") for entry in self.store.ops().get(KEY, [])]
        self.assertEqual(events.count("wake.ok"), 1)
        self.assertNotIn("wake.skipped", events)

    def test_probe_is_rechecked_on_every_wake(self):
        """The gateway publishes its injector *after* plugin discovery: a snapshot taken in
        register() says "none" for the one host that can actually deliver."""
        states = ["none", "gateway"]
        self.manager.set_inject_capability(
            "none", probe=lambda: states.pop(0) if states else "gateway"
        )
        record = self.seed()
        self.fake.status_payload = {"revision": 6, "available": 1}
        self.wake()  # probe -> none: skipped, nothing injected
        self.assertEqual(self.injected, [])
        self.assertEqual(self.manager.inject_capability, "none")
        self.wake()  # probe -> gateway: the same binding now wakes
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.manager.inject_capability, "gateway")
        events = [entry.get("event") for entry in self.store.ops().get(KEY, [])]
        self.assertIn("wake.ok", events)
        self.assertEqual(int(self.store.get_binding(KEY).get("lastWokenRevision") or 0), 6)

    def test_status_line_reports_inject_capability(self):
        commands_mod = _loader.inbox_lib("commands")
        self.manager.set_inject_capability("gateway")
        self.seed()
        self.store.put_binding(KEY, self.store.get_binding(KEY))
        result = commands_mod.handle("status", self.manager, KEY)
        self.assertIn("inject     : gateway", result)
        self.manager.set_inject_capability("none")
        self.assertIn("inject     : none", commands_mod.handle("status", self.manager, KEY))


class LocalSessionDeferTests(Base):
    """A refused wake into a *local* session says nothing about the item: the gateway only admits
    keyed wakes into live local sessions, and a classic-REPL session is only live during a turn."""

    LOCAL_KEY = "agent:main:local:dm:local-0123456789abcdef"
    LOCAL_SID = "local-0123456789abcdef"

    def _seed_local(self) -> dict:
        record = binding_mod.new_record(
            session_key=self.LOCAL_KEY,
            inbox_id="main",
            api_base_url="http://127.0.0.1:18765",
            token_ref="file:/tmp/inbox.token",
            channels=[],
            wake_mode="claim",
            wake_profile="generic",
            session_id=self.LOCAL_SID,
        )
        self.store.put_binding(self.LOCAL_KEY, record)
        self.manager._runtime_for(self.LOCAL_KEY)
        return record

    def test_is_local_session_key(self):
        self.assertTrue(binding_mod._is_local_session_key(self.LOCAL_KEY))
        self.assertFalse(binding_mod._is_local_session_key(KEY))

    def test_refused_local_wake_defers_on_a_flat_retry(self):
        self._seed_local()
        self.inject_ok = False
        self.inject_error = (
            "注入被宿主拒绝：确认 config.yaml 里 plugins.entries.inbox-wake.allow_gateway_injection: "
            "true，且该 session key 仍然存在"
        )
        self.fake.status_payload = {"revision": 9, "available": 1}
        self.manager._maybe_wake(self.LOCAL_KEY, "event")
        record = self.store.get_binding(self.LOCAL_KEY)
        self.assertTrue(record["lastWake"]["deferred"])
        self.assertFalse(record["lastWake"]["ok"])
        self.assertEqual(int((record.get("wake") or {}).get("attempts") or 0), 0)
        self.assertEqual(int(record["wake"]["nextWakeAllowedAt"]), int(self.now * 1000) + 15_000)
        self.assertEqual(int(record.get("lastWokenRevision") or 0), 0)
        events = [entry.get("event") for entry in self.store.ops().get(self.LOCAL_KEY, [])]
        self.assertEqual(events.count("wake.deferred"), 1)
        self.assertNotIn("wake.failed", events)

    def test_refused_messaging_wake_still_backs_off(self):
        self.seed()
        self.inject_ok = False
        self.fake.status_payload = {"revision": 9, "available": 1}
        self.wake()
        record = self.store.get_binding(KEY)
        self.assertNotIn("deferred", record["lastWake"])
        self.assertEqual(int(record["wake"]["attempts"]), 1)
        self.assertEqual(int(record["wake"]["nextWakeAllowedAt"]), int(self.now * 1000) + 30_000)
        events = [entry.get("event") for entry in self.store.ops().get(KEY, [])]
        self.assertEqual(events.count("wake.failed"), 1)
        self.assertNotIn("wake.deferred", events)


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


class AutoBindTests(Base):
    """``autoBind``: 首轮自动绑定，让"打开 Hermes 就自动挂载"成立。"""

    def _manager(self, **settings):
        cfg = config_mod.Config.from_mapping(settings)
        manager = binding_mod.BindingManager(cfg, self.store, self._inject, clock=lambda: self.now)
        manager._client = lambda record: self.fake
        self.addCleanup(manager.shutdown)
        return manager

    def _token_file(self, mode: int = 0o600) -> str:
        handle, path = tempfile.mkstemp(prefix="inbox-autobind-")
        os.write(handle, b"test-token\n")
        os.close(handle)
        os.chmod(path, mode)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_autobind_binds_unbound_session_on_first_turn(self):
        path = self._token_file()
        manager = self._manager(autoBind=True, adapterTokenFile=f"file:{path}")
        self.assertIsNone(self.store.get_binding(KEY))
        manager.on_turn_start(KEY)
        record = self.store.get_binding(KEY)
        self.assertIsNotNone(record)
        self.assertEqual(record["inboxId"], "main")
        self.assertEqual(record["tokenRef"], f"file:{path}")
        self.assertEqual(record["wakeMode"], "claim")
        self.assertEqual(self.fake.occupancy[0][0], "put")
        self.assertEqual(self.injected, [])  # bind 不注入，唤醒才注入

    def test_autobind_off_by_default(self):
        manager = self._manager(adapterTokenFile=f"file:{self._token_file()}")
        manager.on_turn_start(KEY)
        self.assertIsNone(self.store.get_binding(KEY))

    def test_autobind_without_token_ref_does_not_raise(self):
        manager = self._manager(autoBind=True)
        manager.on_turn_start(KEY)  # 只 warning，不抛
        self.assertIsNone(self.store.get_binding(KEY))

    def test_autobind_bad_token_permissions_stays_unbound(self):
        path = self._token_file(0o644)
        manager = self._manager(autoBind=True, adapterTokenFile=f"file:{path}")
        manager.on_turn_start(KEY)
        self.assertIsNone(self.store.get_binding(KEY))  # TOKEN_FILE_PERMISSIONS 拦下

    def test_autobind_only_once_per_session(self):
        path = self._token_file()
        manager = self._manager(autoBind=True, adapterTokenFile=f"file:{path}")
        manager.on_turn_start(KEY)
        first = self.store.get_binding(KEY)
        manager.on_turn_start(KEY)
        second = self.store.get_binding(KEY)
        self.assertEqual(first["createdAt"], second["createdAt"])
        self.assertEqual(len(self.fake.occupancy), 1)


class SseTimeoutTests(unittest.TestCase):
    """The SSE socket must not inherit the short request/response timeout."""

    def test_worker_passes_its_own_read_timeout(self):
        sse_mod = _loader.inbox_lib("sse")
        seen: list = []

        class Flaky:
            def open_events(self, inbox_id, *, last_event_id="", timeout=None):
                seen.append(timeout)
                raise client_mod.InboxError("SSE_DOWN", "boom", status=503)

        stop = threading.Event()
        worker = sse_mod.SseWorker(
            client=Flaky(),
            inbox_id="main",
            last_event_id="",
            on_event=lambda *a: None,
            on_state=lambda *a: None,
            stop_event=stop,
            reconnect_base_ms=100,
            reconnect_max_ms=100,
            read_timeout_seconds=7.5,
        )
        worker.start()
        try:
            deadline = time.time() + 2.0
            while not seen and time.time() < deadline:
                time.sleep(0.02)
        finally:
            stop.set()
            worker.join(2)
        self.assertTrue(seen, "worker 应该调用 open_events")
        self.assertEqual(seen[0], 7.5)


class PollReconcileTests(Base):
    """Backends whose /events never sends a heartbeat still get woken by the poll fallback."""

    def test_poll_wakes_without_any_sse_event(self):
        self.config.poll_seconds = 1
        self.config.sse_read_timeout_seconds = 600
        record = self.seed()
        self.manager._start(record, reason="test")
        self.fake.status_payload = {"revision": 5, "available": 1}
        deadline = time.time() + 4.0
        while time.time() < deadline and not self.injected:
            time.sleep(0.05)
        self.assertTrue(self.injected, "pollSeconds 到点应触发一次唤醒")
        self.assertIn("revision=5", self.injected[0][0])
        record = self.store.get_binding(KEY)
        self.assertEqual(record["lastWokenRevision"], 5)

    def test_poll_disabled_by_default(self):
        self.seed()
        self.assertEqual(self.config.poll_seconds, 0)
        self.manager._start(self.store.get_binding(KEY), reason="test")
        self.assertIsNone(self.manager._poll_thread)
