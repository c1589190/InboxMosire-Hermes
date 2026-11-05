"""``inbox.push`` delivery tests: verbatim text, delivery dedupe, ack, attachments, changed parity.

The push path is a *different* contract from the ``inbox.changed`` wake path: the server renders
the whole body and the agent must see exactly that string (D-002/D-015), so nothing here is allowed
to add a header, a summary or its own formatting.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _loader  # noqa: E402

binding_mod = _loader.inbox_lib("binding")
client_mod = _loader.inbox_lib("client")
config_mod = _loader.inbox_lib("config")
push_mod = _loader.inbox_lib("push")
state_mod = _loader.inbox_lib("state")
template_mod = _loader.inbox_lib("template")

KEY = "agent:main:telegram:dm:4242"
SID = "session-4242"
INBOX = "main"

# 服务端渲染好的整段正文：多行 + 行内空格 + 结尾换行，逐字注入的原样样本。
PUSH_TEXT = (
    "张三：明天 10 点前把那版 PPT 发我\n"
    "李四：收到，另外看一下 dashboard 的报错\n"
    "  （缩进也要保住）\n"
)


def envelope(delivery_id: str = "dlv_1", **data) -> dict:
    """CloudEvents 1.0 信封形状的 SSE 帧（真实服务端发的是这个形状）。"""
    payload = {
        "inbox": INBOX,
        "deliveryId": delivery_id,
        "itemIds": ["itm_1", "itm_2"],
        "text": PUSH_TEXT,
        "attachments": [],
        "important": False,
        "remaining": 0,
        "revision": 42,
    }
    payload.update(data)
    return {
        "event": push_mod.PUSH_EVENT,
        "data": {
            "specversion": "1.0",
            "id": "evt_1",
            "source": "inbox-server",
            "type": push_mod.PUSH_TYPE,
            "time": "2026-11-06T00:00:00Z",
            "datacontenttype": "application/json",
            "data": payload,
        },
    }


class FakeClient:
    def __init__(self) -> None:
        self.status_payload = {"revision": 1, "available": 0}
        self.items_payload: list[dict] = []
        self.details: list[dict] = []
        self.acks: list[tuple[str, str]] = []
        self.ack_error: Exception | None = None
        self.ack_payload: dict = {}

    def status(self, inbox_id: str, *, channels=None) -> dict:
        return dict(self.status_payload)

    def items(self, inbox_id: str, *, limit: int = 3, channel=None, channels=None) -> list[dict]:
        return [dict(item) for item in self.items_payload[:limit]]

    def item(self, inbox_id: str, item_id: str) -> dict:
        for entry in self.details:
            if entry.get("itemId") == item_id:
                return dict(entry)
        raise client_mod.InboxError("ITEM_NOT_FOUND", f"没有 {item_id}")

    def ack_delivery(self, inbox_id: str, delivery_id: str) -> dict:
        self.acks.append((inbox_id, delivery_id))
        if self.ack_error is not None:
            raise self.ack_error
        return dict(self.ack_payload)

    def open_events(self, inbox_id: str, *, last_event_id: str = "", timeout=None):
        return iter(())


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_000.0
        self.injected: list[tuple[str, str]] = []
        self.inject_kwargs: list[dict] = []
        self.inject_ok = True
        self.ctx = _loader.FakeCtx()
        self.store = state_mod.Store(self.ctx)
        self.config = config_mod.Config.from_mapping({})
        # 首条工作区说明（D-009）默认开启；这堆用例只测推送正文，把它关掉，
        # 专门的行为在 GuideTests 里测。
        self.config.first_run_guide = False
        self.manager = binding_mod.BindingManager(
            self.config, self.store, self._inject, clock=lambda: self.now
        )
        self.fake = FakeClient()
        self.manager._client = lambda record: self.fake
        self.manager.set_inject_capability("gateway", lambda: "gateway")
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.manager.shutdown()
        self._tmp.cleanup()

    # 与宿主 ``inject_message`` 一样默认只吃两个位置参数——没有附件时必须保持这个签名。
    def _inject(self, text: str, session_key: str, **kwargs) -> tuple[bool, str]:
        if kwargs:
            raise TypeError("unexpected keyword arguments")
        self.injected.append((text, session_key))
        return (True, "") if self.inject_ok else (False, "host rejected injection")

    def seed(self, **overrides) -> dict:
        record = binding_mod.new_record(
            session_key=KEY,
            inbox_id=INBOX,
            api_base_url="http://127.0.0.1:18765",
            token_ref="file:/tmp/inbox.token",
            channels=[],
            wake_mode="view-ack",
            wake_profile="generic",
            session_id=SID,
        )
        record.update(overrides)
        self.store.put_binding(KEY, record)
        self.manager._runtime_for(KEY)
        return record

    def dispatch(self, event: dict, event_id: str = "1") -> None:
        self.manager._on_event(KEY, event_id, event)


# ---- pure helpers ------------------------------------------------------


class ParseTests(unittest.TestCase):
    def test_envelope_and_flat_shapes_agree(self):
        wrapped = push_mod.parse_push(envelope())
        flat = push_mod.parse_push(
            {"event": push_mod.PUSH_EVENT, "data": {"deliveryId": "dlv_1", "text": PUSH_TEXT}}
        )
        self.assertIsNotNone(wrapped)
        self.assertIsNotNone(flat)
        self.assertEqual(wrapped.delivery_id, "dlv_1")
        self.assertEqual(flat.delivery_id, "dlv_1")
        self.assertEqual(wrapped.text, PUSH_TEXT)
        self.assertEqual(flat.text, PUSH_TEXT)

    def test_text_is_not_normalised(self):
        parsed = push_mod.parse_push(envelope(text=PUSH_TEXT))
        self.assertEqual(parsed.text, PUSH_TEXT)  # 不压平空白、不 strip、不截断

    def test_item_ids_and_flags(self):
        parsed = push_mod.parse_push(envelope(important=True, remaining=3, revision=42))
        self.assertEqual(parsed.item_ids, ("itm_1", "itm_2"))
        self.assertTrue(parsed.important)
        self.assertEqual(parsed.remaining, 3)
        self.assertEqual(parsed.revision, 42)

    def test_blank_text_rejected(self):
        self.assertIsNone(push_mod.parse_push(envelope(text="   \n ")))

    def test_unsafe_delivery_id_rejected(self):
        self.assertIsNone(push_mod.parse_push(envelope(delivery_id="../../etc/passwd")))

    def test_changed_is_not_push(self):
        event = {"event": push_mod.CHANGED_EVENT, "data": {"revision": 7, "available": 2}}
        self.assertFalse(push_mod.is_push(event))
        self.assertIsNone(push_mod.parse_push(event))


class LocalMediaTests(unittest.TestCase):
    def test_only_existing_absolute_paths_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "chart.png"
            good.write_bytes(b"png")
            urls, skipped = push_mod.local_media(
                [
                    {"name": "chart.png", "ref": str(good)},
                    {"name": "remote", "ref": "https://example.com/a.png"},
                    {"name": "relative", "ref": "rel/a.png"},
                    {"name": "missing", "ref": str(Path(tmp) / "nope.png")},
                ]
            )
        self.assertEqual(urls, [str(good)])
        self.assertEqual(len(skipped), 3)

    def test_plain_string_attachment(self):
        self.assertEqual(push_mod.attachment_ref("/tmp/a.png"), "/tmp/a.png")
        self.assertEqual(push_mod.attachment_ref({"name": "a", "ref": "/tmp/b.png"}), "/tmp/b.png")
        self.assertEqual(push_mod.attachment_ref({"name": "a"}), "")


class AckHelperTests(unittest.TestCase):
    def test_retries_once_on_5xx(self):
        calls: list[str] = []

        def call(inbox_id: str, delivery_id: str) -> dict:
            calls.append(delivery_id)
            if len(calls) == 1:
                raise client_mod.InboxError("UPSTREAM", "boom", status=503)
            return {"acked": True}

        ok, error, attempts = push_mod.ack_delivery(
            call, inbox_id=INBOX, delivery_id="dlv_1", retries=1
        )
        self.assertTrue(ok)
        self.assertEqual(attempts, 2)
        self.assertEqual(len(calls), 2)

    def test_no_retry_on_4xx(self):
        calls: list[str] = []

        def call(inbox_id: str, delivery_id: str) -> dict:
            calls.append(delivery_id)
            raise client_mod.InboxError("FORBIDDEN", "needs items:manage", status=403)

        ok, error, attempts = push_mod.ack_delivery(
            call, inbox_id=INBOX, delivery_id="dlv_1", retries=1
        )
        self.assertFalse(ok)
        self.assertEqual(attempts, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("FORBIDDEN", error)

    def test_acked_false_is_success(self):
        ok, error, attempts = push_mod.ack_delivery(
            lambda inbox_id, delivery_id: {"acked": False},
            inbox_id=INBOX,
            delivery_id="dlv_1",
        )
        self.assertTrue(ok)
        self.assertEqual(error, "acked=false")


# ---- injection ---------------------------------------------------------


class PushInjectionTests(Base):
    def test_push_injects_text_verbatim(self):
        self.seed()
        self.dispatch(envelope())
        self.assertEqual(len(self.injected), 1)
        text, session_key = self.injected[0]
        self.assertEqual(text, PUSH_TEXT)  # 逐字一致：没有 header、没有摘要、没有换行改写
        self.assertEqual(session_key, KEY)
        self.assertNotIn(template_mod.HEADER_TAG, text)

    def test_same_delivery_injected_once(self):
        self.seed()
        self.dispatch(envelope(), event_id="1")
        self.dispatch(envelope(), event_id="2")  # SSE 重连重放同一帧
        self.assertEqual(len(self.injected), 1)
        events = [entry.get("event") for entry in self.store.ops().get(KEY, [])]
        self.assertIn("push.duplicate", events)

    def test_dedupe_survives_restart(self):
        self.seed()
        self.dispatch(envelope())
        self.assertEqual(len(self.injected), 1)
        # 新进程：同一份 ctx.state，新的 manager 实例
        restarted_injected: list[tuple[str, str]] = []
        manager2 = binding_mod.BindingManager(
            self.config,
            state_mod.Store(self.ctx),
            lambda text, session_key: (restarted_injected.append((text, session_key)), (True, ""))[1],
            clock=lambda: self.now,
        )
        manager2._client = lambda record: self.fake
        manager2.set_inject_capability("gateway", lambda: "gateway")
        try:
            manager2._on_event(KEY, "3", envelope())
            self.assertEqual(restarted_injected, [])
        finally:
            manager2.shutdown()

    def test_ack_called_with_path_arguments(self):
        self.seed()
        self.dispatch(envelope(delivery_id="dlv_abc"))
        self.assertEqual(self.fake.acks, [(INBOX, "dlv_abc")])
        record = self.store.get_binding(KEY)
        self.assertEqual(record["lastDeliveryId"], "dlv_abc")
        self.assertTrue(record["lastAck"]["ok"])
        self.assertEqual(record["lastPush"]["deliveryId"], "dlv_abc")

    def test_client_ack_hits_the_documented_route(self):
        token = Path(self._tmp.name) / "inbox.token"
        token.write_text("tok", encoding="utf-8")
        token.chmod(0o600)
        client = client_mod.InboxClient("http://127.0.0.1:18765", f"file:{token}")
        seen: list[tuple[str, str, dict]] = []

        def fake_request(method, path, *, body=None, query=None, timeout=None):
            seen.append((method, path, body or {}))
            return {"deliveryId": "dlv_1", "acked": True, "closedItems": ["itm_1"]}

        client._request = fake_request
        out = client.ack_delivery(INBOX, "dlv_1")
        self.assertTrue(out["acked"])
        self.assertEqual(seen[0][0], "POST")
        self.assertEqual(seen[0][1], "/v1/inboxes/main/deliveries/dlv_1/ack")

    def test_mcp_ack_is_preferred_when_installed(self):
        self.seed()
        mcp_calls: list[tuple[str, str]] = []
        self.manager.set_mcp_ack(lambda inbox_id, delivery_id: (mcp_calls.append((inbox_id, delivery_id)), (True, ""))[1])
        self.dispatch(envelope(delivery_id="dlv_mcp"))
        self.assertEqual(mcp_calls, [(INBOX, "dlv_mcp")])
        self.assertEqual(self.fake.acks, [])

    def test_mcp_ack_failure_falls_back_to_http(self):
        self.seed()
        self.manager.set_mcp_ack(lambda inbox_id, delivery_id: (False, "tool missing"))
        self.dispatch(envelope(delivery_id="dlv_fb"))
        self.assertEqual(self.fake.acks, [(INBOX, "dlv_fb")])

    def test_ack_failure_does_not_reinject(self):
        self.seed()
        self.fake.ack_error = client_mod.InboxError("FORBIDDEN", "needs items:manage", status=403)
        self.dispatch(envelope(), event_id="1")
        self.dispatch(envelope(), event_id="2")  # 重放：ack 失败也不能导致第二次注入
        self.assertEqual(len(self.injected), 1)
        record = self.store.get_binding(KEY)
        self.assertFalse(record["lastAck"]["ok"])
        events = [entry.get("event") for entry in self.store.ops().get(KEY, [])]
        self.assertIn("push.ack.failed", events)

    def test_injection_failure_keeps_delivery_unacked(self):
        self.seed()
        self.inject_ok = False
        self.dispatch(envelope())
        self.assertEqual(self.fake.acks, [])  # 没交给 Agent 就不能 ack
        self.assertFalse(push_mod.delivery_seen(self.store, KEY, "dlv_1", INBOX))
        self.inject_ok = True
        self.dispatch(envelope(), event_id="2")  # 下一次事件重试注入
        self.assertEqual(len(self.injected), 2)

    def test_foreign_inbox_delivery_is_skipped(self):
        self.seed()
        self.dispatch(envelope(inbox="other-board"))
        self.assertEqual(self.injected, [])
        self.assertEqual(self.fake.acks, [])
        events = [entry.get("event") for entry in self.store.ops().get(KEY, [])]
        self.assertIn("push.skipped", events)

    def test_important_does_not_change_text(self):
        self.seed()
        self.dispatch(envelope(important=True))
        self.assertEqual(self.injected[0][0], PUSH_TEXT)


class AttachmentTests(Base):
    def test_no_media_urls_when_attachments_empty(self):
        self.seed()
        self.dispatch(envelope(attachments=[]))
        # ``_inject`` 在收到任何关键字参数时就抛 TypeError —— 旧调用签名必须保持不变
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.inject_kwargs, [])

    def test_local_attachment_passes_media_urls(self):
        self.seed()
        good = Path(self._tmp.name) / "chart.png"
        good.write_bytes(b"png")
        seen: list[dict] = []

        def inject_with_media(text, session_key, **kwargs):
            seen.append(dict(kwargs))
            self.injected.append((text, session_key))
            return True, ""

        self.manager.inject = inject_with_media
        self.dispatch(envelope(attachments=[{"name": "chart.png", "ref": str(good)}]))
        self.assertEqual(seen, [{"media_urls": [str(good)]}])

    def test_remote_attachment_is_skipped_not_downloaded(self):
        self.seed()
        seen: list[dict] = []
        self.manager.inject = lambda text, session_key, **kwargs: (
            seen.append(dict(kwargs)),
            (True, ""),
        )[1]
        self.dispatch(envelope(attachments=[{"name": "x", "ref": "https://example.com/a.png"}]))
        self.assertEqual(seen, [{}])
        events = [entry.get("event") for entry in self.store.ops().get(KEY, [])]
        self.assertIn("push.attachment.skipped", events)

    def test_injector_without_media_support_degrades(self):
        self.seed()  # self._inject raises TypeError on any kwarg
        good = Path(self._tmp.name) / "chart.png"
        good.write_bytes(b"png")
        with self.assertLogs("inbox_wake_plugin", level="WARNING") as logs:
            self.dispatch(envelope(attachments=[{"name": "chart.png", "ref": str(good)}]))
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.injected[0][0], PUSH_TEXT)
        self.assertTrue(any("media_urls" in line for line in logs.output))


# ---- inbox.changed parity ----------------------------------------------


class ChangedPathTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.fake.status_payload = {"revision": 7, "available": 2}
        self.fake.items_payload = [
            {"itemId": "itm_1", "channel": "qq", "kind": "message", "title": "张三：PPT"},
            {"itemId": "itm_3", "channel": "qq", "kind": "message", "title": "王五：周报"},
        ]
        self.fake.details = [
            {"itemId": "itm_1", "content": "张三：明天 10 点前把那版 PPT 发我"},
            {"itemId": "itm_3", "content": "王五：周报已发"},
        ]

    def changed(self) -> dict:
        return {
            "event": push_mod.CHANGED_EVENT,
            "data": {
                "specversion": "1.0",
                "type": push_mod.CHANGED_TYPE,
                "data": {"inbox": INBOX, "revision": 7, "available": 2, "channels": []},
            },
        }

    def test_changed_event_still_wakes(self):
        self.seed()
        self.dispatch(self.changed())
        runtime = self.manager._runtime_for(KEY)
        self.assertIsNotNone(runtime.timer)  # 旧路径：debounce → _maybe_wake
        runtime.timer.cancel()
        self.manager._maybe_wake(KEY, "event")
        self.assertEqual(len(self.injected), 1)
        self.assertIn(template_mod.HEADER_TAG, self.injected[0][0])
        self.assertIn("revision=7", self.injected[0][0])

    def test_changed_stops_waking_once_push_mode_is_on(self):
        """服务端一旦推 push，changed 只推进水位：同一批消息不许再唤醒一次。"""
        self.seed()
        self.dispatch(envelope(itemIds=["itm_1"]))  # push 先把 itm_1 原样注入
        self.assertEqual(len(self.injected), 1)
        runtime = self.manager._runtime_for(KEY)
        if runtime.timer is not None:
            runtime.timer.cancel()
            runtime.timer = None
        self.dispatch(self.changed(), event_id="2")
        runtime = self.manager._runtime_for(KEY)
        runtime.timer.cancel()
        self.manager._maybe_wake(KEY, "event")
        self.assertEqual(len(self.injected), 1)  # 不再有 changed 唤醒
        self.assertTrue(self.store.get_binding(KEY).get("pushMode"))
        events = [entry.get("event") for entry in self.store.ops().get(KEY, [])]
        self.assertIn("push.mode", events)
        self.assertIn("wake.skipped", events)

    def test_guide_precedes_a_changed_wake(self):
        """旧服务端（无 push）也要先看到工作区说明。"""
        self.config.first_run_guide = True
        self.seed()
        self.dispatch(self.changed(), event_id="1")
        runtime = self.manager._runtime_for(KEY)
        runtime.timer.cancel()
        self.manager._maybe_wake(KEY, "event")
        self.assertEqual(self.injected[0][0], binding_mod.BindingManager.DEFAULT_GUIDE)
        self.assertTrue(self.store.get_binding(KEY).get("guideSent"))

    def test_push_mode_on_suppresses_changed_without_waiting_for_a_push(self):
        self.config.push_mode = "on"
        self.seed()
        self.dispatch(self.changed(), event_id="1")
        runtime = self.manager._runtime_for(KEY)
        if runtime.timer is not None:
            runtime.timer.cancel()
            runtime.timer = None
        self.manager._maybe_wake(KEY, "event")
        self.assertEqual(self.injected, [])
        self.assertIn("wake.skipped", [e.get("event") for e in self.store.ops().get(KEY, [])])

    def test_push_mode_off_ignores_push_frames(self):
        self.config.push_mode = "off"
        self.seed()
        self.dispatch(envelope())
        self.assertEqual(self.injected, [])
        self.assertEqual(self.fake.acks, [])
        self.assertIn("push.skipped", [e.get("event") for e in self.store.ops().get(KEY, [])])

    def test_changed_still_wakes_before_any_push(self):
        """旧服务端（不发 push）必须保持原行为。"""
        self.seed()
        self.dispatch(self.changed(), event_id="1")
        runtime = self.manager._runtime_for(KEY)
        runtime.timer.cancel()
        self.manager._maybe_wake(KEY, "event")
        self.assertEqual(len(self.injected), 1)
        self.assertIn("王五：周报已发", self.injected[0][0])

class GuideTests(Base):
    """D-009：首条注入前先发一条极短的工作区说明，之后不再重复。"""

    def enable(self, **mapping):
        self.config.first_run_guide = True
        for key, value in mapping.items():
            setattr(self.config, key, value)

    def test_guide_goes_first_and_once(self):
        self.enable()
        self.seed()
        self.dispatch(envelope(), event_id="1")
        self.assertEqual([text for text, _ in self.injected][0], binding_mod.BindingManager.DEFAULT_GUIDE)
        self.assertEqual([text for text, _ in self.injected][1], PUSH_TEXT)
        self.dispatch(envelope(delivery_id="dlv_2"), event_id="2")
        self.assertEqual(len(self.injected), 3)
        self.assertEqual(self.injected[2][0], PUSH_TEXT)
        self.assertTrue(self.store.get_binding(KEY).get("guideSent"))

    def test_guide_survives_restart(self):
        self.enable()
        self.seed()
        self.dispatch(envelope(), event_id="1")
        again: list[str] = []
        manager2 = binding_mod.BindingManager(
            self.config,
            state_mod.Store(self.ctx),
            lambda text, session_key: (again.append(text), (True, ""))[1],
            clock=lambda: self.now,
        )
        manager2._client = lambda record: self.fake
        manager2.set_inject_capability("gateway", lambda: "gateway")
        try:
            manager2._on_event(KEY, "9", envelope(delivery_id="dlv_9"))
            self.assertEqual(again, [PUSH_TEXT])
        finally:
            manager2.shutdown()

    def test_disabled_by_config(self):
        self.config.first_run_guide = False
        self.seed()
        self.dispatch(envelope())
        self.assertEqual([text for text, _ in self.injected], [PUSH_TEXT])

    def test_custom_text_used(self):
        self.enable(first_run_guide_text="先读 AGENTS.md")
        self.seed()
        self.dispatch(envelope())
        self.assertEqual(self.injected[0][0], "先读 AGENTS.md")

    def test_guide_failure_does_not_block_push(self):
        self.enable()
        self.seed()
        calls: list[str] = []
        guide_failures = {"left": 1}

        def flaky(text: str, session_key: str, **kwargs):
            if kwargs:
                raise TypeError("unexpected keyword arguments")
            calls.append(text)
            if text == binding_mod.BindingManager.DEFAULT_GUIDE and guide_failures["left"] > 0:
                guide_failures["left"] -= 1
                return False, "busy"
            return True, ""

        self.manager.inject = flaky
        self.dispatch(envelope(), event_id="1")
        self.assertEqual(calls[0], binding_mod.BindingManager.DEFAULT_GUIDE)
        self.assertEqual(calls[1], PUSH_TEXT)
        self.assertFalse(self.store.get_binding(KEY).get("guideSent"))
        self.dispatch(envelope(delivery_id="dlv_2"), event_id="2")
        self.assertEqual(calls[2], binding_mod.BindingManager.DEFAULT_GUIDE)
        self.assertTrue(self.store.get_binding(KEY).get("guideSent"))


if __name__ == "__main__":
    unittest.main()
