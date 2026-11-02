"""Binding lifecycle and the wake decision.

A *binding* ties one Hermes session (gateway route key) to one InboxMosire inbox. The manager owns
one SSE thread and one debounce timer per binding, keeps a cursor, and decides when the host should be
woken. It deliberately has no claim/complete/push surface: the agent uses the InboxMosire MCP tools for
that, so the adapter can never consume an item the agent never saw.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import oplog, safe, template
from .client import InboxClient, InboxError, TokenRef
from .state import Store

logger = logging.getLogger(__name__)

SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")
PROGRAM_MCP_TOOLS = ("inbox_list", "inbox_status")


def _is_local_session_key(session_key: str) -> bool:
    """``agent:<profile>:local:<chat_type>:<chat_id>`` — a session owned by a local viewer."""
    parts = str(session_key or "").split(":")
    return len(parts) >= 3 and parts[2] == "local"

# Each runtime object is per-process only; everything durable lives in the binding record below.
@dataclass
class Runtime:
    stop: threading.Event = field(default_factory=threading.Event)
    thread: Any = None
    timer: Optional[threading.Timer] = None
    turn_running: bool = False
    suspended: bool = False
    pending: Optional[dict] = None
    last_event_at: float = 0.0
    lock: threading.RLock = field(default_factory=threading.RLock)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_record(
    *,
    session_key: str,
    inbox_id: str,
    api_base_url: str,
    token_ref: str,
    channels: list[str],
    wake_mode: str,
    wake_profile: str,
    label: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    stamp = _now_iso()
    return {
        "sessionKey": session_key,
        "sessionId": session_id or session_key,
        "label": safe.clean_text(label, 64),
        "inboxId": inbox_id,
        "apiBaseUrl": api_base_url,
        "tokenRef": token_ref,
        "channels": list(channels),
        "wakeMode": wake_mode,
        "wakeProfile": wake_profile,
        "enabled": True,
        "createdAt": stamp,
        "updatedAt": stamp,
        "lastEventId": "",
        "lastObservedRevision": 0,
        "lastWokenRevision": 0,
        "lastStatus": {"revision": 0, "available": 0, "at": None, "error": None},
        "lastWake": {"at": None, "revision": 0, "messageId": "", "ok": None, "error": None},
        "wake": {"attempts": 0, "nextWakeAllowedAt": 0},
        "summary": {"state": "unknown", "shown": 0, "remaining": 0, "at": None, "code": None, "message": None},
        "health": {
            "sse": "idle",
            "sseConnectedAt": None,
            "reconnects": 0,
            "lastEventAt": None,
            "lastError": None,
            "programMcp": "unknown",
        },
    }


class BindingManager:
    """Owns every binding for this Hermes process."""

    def __init__(
        self,
        config: Any,
        store: Store,
        inject: Callable[[str, str], tuple[bool, str]],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.store = store
        self.inject = inject
        self.clock = clock
        self._lock = threading.RLock()
        self._runtime: dict[str, Runtime] = {}
        self._autobind_attempted: set[str] = set()
        self._clients: dict[str, InboxClient] = {}
        self._shutdown = threading.Event()
        self._poll_lock = threading.Lock()
        self._poll_thread: Optional[threading.Thread] = None
        # Which host is running this manager: only a gateway or the classic CLI REPL can
        # actually deliver an injected message. One-shot/worker processes load the plugin too
        # and must never burn the shared backoff with attempts they cannot win.
        self.inject_capability: str = "unknown"
        # The gateway publishes its message injector *after* plugin discovery
        # (gateway/run_startup.py: discover at :1113, _install_plugin_message_injector at :1741),
        # so a value sampled at register() time is stale for the very host that can deliver
        # wakes. Keep a probe and re-ask every time we are about to wake.
        self.inject_probe: Optional[Callable[[], str]] = None
        self._skip_logged: dict[str, int] = {}

    def set_inject_capability(
        self,
        capability: str,
        probe: Optional[Callable[[], str]] = None,
    ) -> None:
        """Record whether this process can deliver an injected wake ("gateway"/"cli"/"none").

        ``probe`` (optional) is re-evaluated on every wake attempt; the frozen string is only the
        fallback used when no probe is installed."""
        self.inject_capability = capability
        self.inject_probe = probe

    def _current_inject_capability(self) -> str:
        """Ask the host right now — an injector may attach long after ``register()``."""
        probe = self.inject_probe
        if probe is None:
            return self.inject_capability
        try:
            value = str(probe() or "unknown")
        except Exception:  # a broken probe must never break the wake path
            return "unknown"
        self.inject_capability = value
        return value

    # ---- helpers --------------------------------------------------------

    def _client(self, record: dict[str, Any]) -> InboxClient:
        key = record["sessionKey"]
        base = str(record.get("apiBaseUrl") or self.config.api_url)
        ref = TokenRef.parse(record.get("tokenRef") or self.config.adapter_token_file)
        cached = self._clients.get(key)
        if cached is not None and cached.base_url == base.rstrip("/") and cached.token_ref == ref:
            return cached
        client = InboxClient(base, ref, timeout_seconds=self.config.request_timeout_seconds())
        self._clients[key] = client
        return client

    def _runtime_for(self, session_key: str) -> Runtime:
        with self._lock:
            runtime = self._runtime.get(session_key)
            if runtime is None:
                runtime = Runtime()
                self._runtime[session_key] = runtime
            return runtime

    def _persist(self, record: dict[str, Any]) -> dict[str, Any]:
        record["updatedAt"] = _now_iso()
        self.store.put_binding(record["sessionKey"], dict(record))
        return record

    def _log(self, record: dict[str, Any], name: str, **fields: Any) -> None:
        try:
            oplog.append(self.store, record["sessionKey"], name, **fields)
        except Exception as exc:  # logging must never break the wake path
            logger.debug("inbox-wake: ops log failed: %s", exc)

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        view = dict(record)
        view.pop("schemaVersion", None)
        return view

    def _program_mcp_state(self) -> str:
        """Do the business inbox tools exist in this process? (claim mode needs them to act.)"""
        try:
            from tools.registry import registry  # type: ignore

            names = set(registry.get_all_tool_names() or [])
        except Exception:
            return "unknown"
        hits = {tool for tool in PROGRAM_MCP_TOOLS if any(n == tool or n.endswith(f"__{tool}") for n in names)}
        if not hits:
            return "missing"
        return "ready" if len(hits) == len(PROGRAM_MCP_TOOLS) else "partial"

    # ---- lifecycle ------------------------------------------------------

    async_mode_unused = False  # keeps dataclass-less class explicit for readers

    def resume_all(self) -> int:
        """Start workers for bindings persisted by a previous process."""
        started = 0
        for key, record in list(self.store.bindings().items()):
            if not record.get("enabled"):
                continue
            try:
                self._start(record, reason="resume")
                started += 1
            except Exception as exc:
                logger.warning("inbox-wake: cannot resume binding %s: %s", key, exc)
        return started

    def _start(self, record: dict[str, Any], *, reason: str) -> None:
        from .sse import SseWorker  # imported lazily so tests can import this module standalone

        key = record["sessionKey"]
        runtime = self._runtime_for(key)
        with runtime.lock:
            # A suspended runtime may still be winding down; its worker already has ``stop`` set,
            # so replacing it is safe and keeps the revive path free of timing races.
            if (
                runtime.thread is not None
                and runtime.thread.is_alive()
                and not runtime.suspended
            ):
                return
            runtime.suspended = False
            stop = threading.Event()
            runtime.stop = stop
        client = self._client(record)
        worker = SseWorker(
            client=client,
            inbox_id=record["inboxId"],
            last_event_id=str(record.get("lastEventId") or ""),
            on_event=lambda event_id, event: self._on_event(key, event_id, event),
            on_state=lambda state, detail: self._on_sse_state(key, state, detail),
            stop_event=stop,
            reconnect_base_ms=self.config.reconnect_base_ms,
            reconnect_max_ms=self.config.reconnect_max_ms,
            read_timeout_seconds=float(self.config.sse_read_timeout_seconds),
        )
        # Occupancy is advisory (lease + wake-adapter contract): a failure is logged, never fatal.
        try:
            client.put_occupancy(
                record["inboxId"], record.get("sessionId") or key, lease_seconds=600
            )
            self._log(record, "occupancy.put", ok=True)
        except InboxError as exc:
            self._log(record, "occupancy.put", ok=False, code=exc.code)
        with runtime.lock:
            runtime.thread = worker
        worker.start()
        self._ensure_poll()
        record["health"]["sse"] = "connecting"
        self._persist(record)
        self._log(record, "binding.start", reason=reason, inbox=record["inboxId"])

    def _ensure_poll(self) -> None:
        """Start the reconcile loop once (only when ``pollSeconds`` > 0)."""
        seconds = int(self.config.poll_seconds or 0)
        if seconds <= 0:
            return
        with self._poll_lock:
            if self._poll_thread is not None and self._poll_thread.is_alive():
                return
            self._shutdown.clear()
            thread = threading.Thread(
                target=self._poll_loop, name="inbox-wake-poll", daemon=True
            )
            self._poll_thread = thread
        thread.start()

    def _poll_loop(self) -> None:
        """GET /status on a timer and run the very same wake decision.

        This is the normalisation layer for backends whose ``/events`` stream never sends a
        ``: ping`` heartbeat comment (SSE liveness then cannot be proven, but revisions can).
        """
        interval = max(1, int(self.config.poll_seconds or 0))
        while not self._shutdown.wait(interval):
            for key in list(self.store.bindings().keys()):
                record = self.store.get_binding(key)
                if record is None or not record.get("enabled"):
                    continue
                try:
                    self._maybe_wake(key, "poll")
                except Exception as exc:  # a single bad inbox must not kill the loop
                    logger.warning("inbox-wake: poll reconcile failed for %s: %s", key, exc)

    def _stop(self, record: dict[str, Any], *, reason: str) -> None:
        key = record["sessionKey"]
        runtime = self._runtime.get(key)
        if runtime is not None:
            with runtime.lock:
                runtime.suspended = True
                runtime.stop.set()
                if runtime.timer is not None:
                    runtime.timer.cancel()
                    runtime.timer = None
                runtime.pending = None
        try:
            client = self._client(record)
            client.delete_occupancy(record["inboxId"], record.get("sessionId") or key)
        except InboxError as exc:
            self._log(record, "occupancy.delete", ok=False, code=exc.code)
        record["health"]["sse"] = "stopped"
        self._persist(record)
        self._log(record, "binding.stop", reason=reason)

    def shutdown(self) -> None:
        """Stop every worker (plugin unload / gateway shutdown)."""
        self._shutdown.set()
        thread = self._poll_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.2)
        with self._lock:
            keys = list(self._runtime)
        for key in keys:
            runtime = self._runtime.get(key)
            if runtime is not None:
                with runtime.lock:
                    runtime.stop.set()
                    if runtime.timer is not None:
                        runtime.timer.cancel()
                        runtime.timer = None

    # ---- public operations ---------------------------------------------

    def bind(self, session_key: str, params: dict[str, Any]) -> dict[str, Any]:
        """Create (or replace) the binding for one session. Returns a public view or an error dict."""
        if not SESSION_KEY_RE.fullmatch(session_key or ""):
            return {"ok": False, "code": "NO_SESSION_KEY", "message": "无法确定当前会话的 session key"}
        existing = self.store.get_binding(session_key)
        if existing is None and len(self.store.bindings()) >= self.config.max_bindings:
            return {
                "ok": False,
                "code": "TOO_MANY_BINDINGS",
                "message": f"已达绑定上限 {self.config.max_bindings}，请先解绑其它会话",
            }

        descriptor = params.get("descriptor")
        if isinstance(descriptor, str) and descriptor.strip():
            try:
                import json

                descriptor = json.loads(descriptor)
            except Exception:
                return {"ok": False, "code": "INVALID_ARGUMENT", "message": "descriptor 不是合法 JSON"}
        if not isinstance(descriptor, dict):
            descriptor = {}

        def pick(name: str, fallback: Any) -> Any:
            value = params.get(name)
            if value not in (None, ""):
                return value
            value = descriptor.get(name)
            if value not in (None, ""):
                return value
            return fallback

        inbox_id = str(pick("inboxId", self.config.inbox_id)).strip()
        if not safe.is_safe_id(inbox_id):
            return {"ok": False, "code": "INVALID_ARGUMENT", "message": f"inboxId 非法: {inbox_id!r}"}

        api_base = str(pick("apiBaseUrl", self.config.api_url)).strip().rstrip("/")
        token_ref = str(pick("adapterTokenRef", self.config.adapter_token_file)).strip()
        try:
            parsed_ref = TokenRef.parse(token_ref)
            parsed_ref.resolve()
        except InboxError as exc:
            return {"ok": False, "code": exc.code, "message": str(exc)}

        channels = safe.as_channels(pick("channels", self.config.channels))
        wake_mode = str(pick("wakeMode", self.config.wake_mode)).strip().lower()
        if wake_mode not in {"claim", "view-ack"}:
            return {"ok": False, "code": "INVALID_ARGUMENT", "message": f"wakeMode 非法: {wake_mode!r}"}
        wake_profile = str(pick("wakeProfile", self.config.wake_profile)).strip().lower()
        if wake_profile not in {"generic", "iibot"}:
            return {"ok": False, "code": "INVALID_ARGUMENT", "message": f"wakeProfile 非法: {wake_profile!r}"}

        record = new_record(
            session_key=session_key,
            inbox_id=inbox_id,
            api_base_url=api_base,
            token_ref=parsed_ref.describe(),
            channels=channels,
            wake_mode=wake_mode,
            wake_profile=wake_profile,
            label=str(pick("label", "")),
            session_id=str(pick("sessionId", session_key)),
        )
        if existing:
            record["lastEventId"] = str(existing.get("lastEventId") or "")
            record["lastObservedRevision"] = int(existing.get("lastObservedRevision") or 0)
            record["lastWokenRevision"] = int(existing.get("lastWokenRevision") or 0)
            record["createdAt"] = existing.get("createdAt") or record["createdAt"]
            self._stop(existing, reason="rebind")
        self._persist(record)
        self._start(record, reason="bind")
        health = self._refresh_status(record)
        self._log(record, "binding.bind", inbox=inbox_id, mode=wake_mode, channels=channels)
        return {"ok": True, "binding": self._public(record), "status": health}

    def unbind(self, session_key: str) -> dict[str, Any]:
        record = self.store.get_binding(session_key)
        if record is None:
            return {"ok": False, "code": "NOT_BOUND", "message": "该会话没有绑定"}
        self._stop(record, reason="unbind")
        self.store.drop_binding(session_key)
        with self._lock:
            self._runtime.pop(session_key, None)
            self._clients.pop(session_key, None)
        return {"ok": True, "unbound": session_key}

    def retry(self, session_key: str) -> dict[str, Any]:
        record = self.store.get_binding(session_key)
        if record is None:
            return {"ok": False, "code": "NOT_BOUND", "message": "该会话没有绑定"}
        record["wake"]["attempts"] = 0
        record["wake"]["nextWakeAllowedAt"] = 0
        record["health"]["lastError"] = None
        self._persist(record)
        self._stop(record, reason="retry")
        self._start(record, reason="retry")
        self._log(record, "binding.retry")
        return {"ok": True, "binding": self._public(record)}

    def by_session_id(self, session_id: str) -> str:
        """Map a Hermes ``session_id`` back to the route key it was bound under (hooks give the former)."""
        target = str(session_id or "").strip()
        if not target:
            return ""
        for key, record in self.store.bindings().items():
            if str(record.get("sessionId") or "") == target:
                return key
        return ""

    def status(self, session_key: str, *, refresh: bool = True) -> dict[str, Any]:
        record = self.store.get_binding(session_key)
        if record is None:
            return {"ok": False, "code": "NOT_BOUND", "message": "该会话没有绑定"}
        if refresh:
            self._refresh_status(record)
        record["health"]["programMcp"] = self._program_mcp_state()
        runtime = self._runtime.get(session_key)
        if runtime is not None and runtime.turn_running:
            record["health"]["turn"] = "running"
        else:
            record["health"]["turn"] = "idle"
        self._persist(record)
        return {"ok": True, "binding": self._public(record)}

    def log(self, session_key: str, limit: int = 20) -> dict[str, Any]:
        entries = oplog.read(self.store, session_key, limit)
        return {"ok": True, "sessionKey": session_key, "entries": entries}

    # ---- turn state -----------------------------------------------------

    def on_turn_start(self, session_key: str) -> None:
        """A turn began: mark the session busy and make sure the SSE worker is running.

        Hermes' ``on_session_end`` is a per-turn hook (``hermes_cli/lifecycle.py``: "on_session_end
        is per-turn and stays observer-only"), and the host fires it at the end of every turn. A
        worker stopped by that event would otherwise stay dead until a manual ``/inbox retry``, so
        revive it here instead of waiting for the operator.

        When ``autoBind`` is on, the first turn of an unbound session binds the configured inbox by
        itself, so an operator only has to start Hermes instead of typing ``/inbox bind`` in every
        new session.
        """
        runtime = self._runtime.get(session_key)
        if runtime is None:
            self._auto_bind(session_key)
            runtime = self._runtime.get(session_key)
        if runtime is None:
            return
        with runtime.lock:
            runtime.turn_running = True
            alive = runtime.thread is not None and runtime.thread.is_alive()
            suspended = runtime.suspended
        # A suspended runtime may still be winding down; ``_start`` is allowed to replace it,
        # and skipping the revive here would leave the session deaf for the whole turn.
        if alive and not suspended:
            return
        record = self.store.get_binding(session_key)
        if record is None or not record.get("enabled"):
            return
        try:
            self._start(record, reason="turn-start")
        except Exception as exc:  # a failed revive must never break the turn
            logger.warning("inbox-wake: cannot restart worker for %s: %s", session_key, exc)

    def _auto_bind(self, session_key: str) -> None:
        """Bind the configured inbox for a brand-new session when ``autoBind`` is enabled.

        Runs once per session key and never raises: an automatic bind must not break the turn. A
        failure is logged (``autoBind`` needs ``adapterTokenFile``; a missing or badly permissioned
        token is the usual cause) and the session simply stays unbound, so ``/inbox bind`` still
        works as the manual fallback.
        """
        if not getattr(self.config, "auto_bind", False):
            return
        if not SESSION_KEY_RE.fullmatch(session_key or ""):
            return
        if session_key in self._autobind_attempted:
            return
        self._autobind_attempted.add(session_key)
        if not str(getattr(self.config, "adapter_token_file", "") or "").strip():
            logger.warning(
                "inbox-wake: autoBind 已开启但未配置 adapterTokenFile，跳过自动绑定（inbox=%s）",
                self.config.inbox_id,
            )
            return
        try:
            result = self.bind(session_key, {})
        except Exception as exc:  # never break the turn
            logger.warning("inbox-wake: autoBind 失败 %s: %s", session_key, exc)
            return
        if result.get("ok"):
            logger.info(
                "inbox-wake: autoBind 已绑定会话 %s（inbox=%s api=%s）",
                session_key, self.config.inbox_id, self.config.api_url,
            )
        else:
            logger.warning(
                "inbox-wake: autoBind 未成功 %s: %s %s",
                session_key, result.get("code"), result.get("message"),
            )

    def on_turn_end(self, session_key: str) -> None:
        runtime = self._runtime.get(session_key)
        if runtime is None:
            return
        pending: Optional[dict] = None
        with runtime.lock:
            runtime.turn_running = False
            pending = runtime.pending
            runtime.pending = None
        if pending:
            # Give the finished turn a beat before the wake lands in the same session.
            self._schedule(session_key, delay_ms=max(50, self.config.debounce_ms), reason="pending-wake")

    def on_tool_result(self, session_id: str, tool_name: str, result: Any) -> None:
        """``inbox_rest`` returns ``restSeconds``; honour it so a snoozed inbox stays quiet."""
        name = str(tool_name or "")
        if not (name == "inbox_rest" or name.endswith("__inbox_rest")):
            return
        seconds = _find_rest_seconds(result)
        if seconds <= 0:
            return
        key = self.by_session_id(session_id) if session_id else ""
        if not key:
            bindings = list(self.store.bindings().keys())
            if len(bindings) != 1:
                return
            key = bindings[0]
        record = self.store.get_binding(key)
        if record is None:
            return
        now_ms = int(self.clock() * 1000)
        record["wake"]["nextWakeAllowedAt"] = max(
            int(record["wake"].get("nextWakeAllowedAt") or 0), now_ms + seconds * 1000
        )
        record["health"]["restUntil"] = _now_iso_offset(seconds)
        self._persist(record)
        self._log(record, "rest.applied", seconds=seconds, tool=name)

    def on_session_gone(self, session_key: str, *, reason: str) -> None:
        """Session end/reset/delete: stop the worker but keep the binding for a later resume."""
        record = self.store.get_binding(session_key)
        runtime = self._runtime.get(session_key)
        if runtime is not None:
            with runtime.lock:
                runtime.suspended = True
                runtime.stop.set()
                if runtime.timer is not None:
                    runtime.timer.cancel()
                    runtime.timer = None
        if record is not None:
            record["health"]["sse"] = "suspended"
            self._persist(record)
            self._log(record, "binding.suspend", reason=reason)

    # ---- wake path ------------------------------------------------------

    def _on_sse_state(self, session_key: str, state: str, detail: Optional[str]) -> None:
        record = self.store.get_binding(session_key)
        if record is None:
            return
        health = record["health"]
        if state == "stopped":
            runtime = self._runtime.get(session_key)
            if runtime is not None and runtime.suspended:
                state = "suspended"
        health["sse"] = state
        if state == "open":
            health["sseConnectedAt"] = _now_iso()
        elif state == "retrying":
            health["reconnects"] = int(health.get("reconnects") or 0) + 1
            health["lastError"] = safe.clean_text(detail, 200) if detail else None
            self._log(record, "sse.retry", detail=detail or "")
        self._persist(record)

    def _on_event(self, session_key: str, event_id: str, event: dict[str, Any]) -> None:
        record = self.store.get_binding(session_key)
        if record is None:
            return
        if event_id:
            record["lastEventId"] = event_id
        record["health"]["lastEventAt"] = _now_iso()
        runtime = self._runtime_for(session_key)
        runtime.last_event_at = self.clock()
        self._persist(record)
        self._log(record, "sse.event", kind=safe.clean_text(event.get("event"), 32))
        self._schedule(session_key, delay_ms=self.config.debounce_ms, reason="event")

    def _schedule(self, session_key: str, *, delay_ms: int, reason: str) -> None:
        runtime = self._runtime.get(session_key)
        if runtime is None:
            return
        with runtime.lock:
            if runtime.timer is not None:
                runtime.timer.cancel()
            timer = threading.Timer(max(0, delay_ms) / 1000.0, self._maybe_wake, args=(session_key, reason))
            timer.daemon = True
            runtime.timer = timer
        timer.start()

    def _refresh_status(self, record: dict[str, Any]) -> dict[str, Any]:
        """GET /status and fold the truth back into the record (the SSE cursor may be stale)."""
        try:
            payload = self._client(record).status(record["inboxId"])
        except InboxError as exc:
            record["lastStatus"] = {
                "revision": int(record.get("lastObservedRevision") or 0),
                "available": int((record.get("lastStatus") or {}).get("available") or 0),
                "at": _now_iso(),
                "error": f"{exc.code}: {safe.clean_text(str(exc), 160)}",
            }
            self._persist(record)
            self._log(record, "status.error", code=exc.code)
            return record["lastStatus"]
        revision = _as_int(payload.get("revision") or payload.get("rev") or payload.get("sequence"))
        available = _as_int(payload.get("available"))
        if revision:
            record["lastObservedRevision"] = revision
        record["lastStatus"] = {
            "revision": revision,
            "available": available,
            "at": _now_iso(),
            "error": None,
        }
        self._persist(record)
        return record["lastStatus"]

    def _summary_items(self, record: dict[str, Any]) -> tuple[list[dict], int, dict]:
        """Fetch the bounded summary view; failures degrade to a summary-less wake."""
        config = self.config
        try:
            items = self._client(record).items(
                record["inboxId"], limit=config.summary_max_items
            )
        except InboxError as exc:
            return [], 0, {"state": "failed", "code": exc.code, "message": safe.clean_text(str(exc), 160)}
        return items, 0, {"state": "ok", "code": None, "message": None}

    def _content_items(self, record: dict[str, Any]) -> list[dict]:
        if self.config.wake_content == "off":
            return []
        try:
            return self._client(record).items(record["inboxId"], limit=self.config.summary_max_items)
        except InboxError:
            return []

    def _maybe_wake(self, session_key: str, reason: str) -> None:
        record = self.store.get_binding(session_key)
        if record is None or not record.get("enabled"):
            return
        runtime = self._runtime.get(session_key)
        if runtime is None:
            return
        if self._current_inject_capability() == "none":
            revision = int(
                (record.get("lastStatus") or {}).get("revision")
                or record.get("lastObservedRevision")
                or 0
            )
            if self._skip_logged.get(session_key) != revision:
                self._skip_logged[session_key] = revision
                self._log(record, "wake.skipped", reason="no-inject-host", revision=revision)
            return
        now = self.clock()

        status = self._refresh_status(record)
        revision = int(status.get("revision") or 0)
        available = int(status.get("available") or 0)
        last_woken = int(record.get("lastWokenRevision") or 0)

        if available <= 0:
            if revision > last_woken:
                record["lastWokenRevision"] = revision
                self._persist(record)
            return
        if revision <= last_woken:
            return
        next_allowed = float((record.get("wake") or {}).get("nextWakeAllowedAt") or 0)
        if next_allowed and (now * 1000.0) < float(next_allowed):
            with runtime.lock:
                runtime.pending = {"revision": revision, "reason": "backoff", "at": _now_iso()}
            self._log(record, "wake.defer", reason="backoff", revision=revision)
            return
        with runtime.lock:
            if runtime.turn_running:
                stale = runtime.pending
                if stale and self.config.pending_ttl_seconds > 0 and _age_seconds(stale.get("epoch"), now) > self.config.pending_ttl_seconds:
                    runtime.pending = None
                    self._log(record, "wake.defer.expired", revision=stale.get("revision"), reason=stale.get("reason"))
                runtime.pending = {"revision": revision, "reason": reason, "at": _now_iso(), "epoch": now}
                self._persist_pending(record, runtime.pending)
                self._log(record, "wake.defer", reason="turn-running", revision=revision)
                return

        summary_items, summary_remaining, summary_state = ([], 0, {"state": "disabled", "code": None, "message": None})
        if self.config.summary_enabled:
            summary_items, summary_remaining, summary_state = self._summary_items(record)
        content_items = self._content_items(record)

        text = template.build_wake(
            inbox_id=record["inboxId"],
            revision=revision,
            available=available,
            channels=record.get("channels") or [],
            mode=record.get("wakeMode") or self.config.wake_mode,
            profile=record.get("wakeProfile") or self.config.wake_profile,
            summary_items=summary_items,
            summary_remaining=summary_remaining,
            summary_max_items=self.config.summary_max_items,
            summary_max_total_chars=self.config.summary_max_total_chars,
            summary_max_title_chars=self.config.summary_max_title_chars,
            content_items=content_items,
            content_max_chars=self.config.wake_content_max_chars,
            content_total_chars=self.config.wake_content_total_chars,
            content_enabled=self.config.wake_content != "off",
        )
        key = template.wake_key(
            inbox_id=record["inboxId"], revision=revision, session_ref=str(record.get("sessionId") or session_key)
        )
        message_id = template.wake_message_id(key)

        ok, error = self.inject(text, session_key)
        record["summary"] = {
            "state": summary_state["state"],
            "shown": len(summary_items),
            "remaining": max(0, available - len(summary_items)),
            "at": _now_iso(),
            "code": summary_state.get("code"),
            "message": summary_state.get("message"),
        }
        if ok:
            record["lastWokenRevision"] = revision
            record["lastWake"] = {
                "at": _now_iso(),
                "revision": revision,
                "messageId": message_id,
                "ok": True,
                "error": None,
            }
            record["wake"] = {"attempts": 0, "nextWakeAllowedAt": 0}
            self._persist(record)
            self._log(record, "wake.ok", revision=revision, messageId=message_id, reason=reason)
            return

        record["lastWake"] = {
            "at": _now_iso(),
            "revision": revision,
            "messageId": message_id,
            "ok": False,
            "error": safe.clean_text(error, 200),
        }
        if _is_local_session_key(session_key):
            # The gateway only admits a keyed wake into a *live* local session
            # (gateway/run_plugin_injection.py: `_plugin_local_session` reads
            # `authority.sessions[chat_id]`). An idle classic-REPL session is not live, so the
            # refusal says nothing about the item — do not climb the shared backoff ladder,
            # just retry at a fixed short interval until a viewer (TUI/desktop/next turn) owns
            # the session again.
            wake_state = dict(record.get("wake") or {})
            wake_state["deferred"] = int(wake_state.get("deferred") or 0) + 1
            wake_state["nextWakeAllowedAt"] = int(now * 1000) + self.config.wake_retry_seconds * 1000
            record["wake"] = wake_state
            record["lastWake"]["deferred"] = True
            self._persist(record)
            self._log(
                record, "wake.deferred", reason="no-live-session", revision=revision, error=error
            )
            return
        attempts = int((record.get("wake") or {}).get("attempts") or 0) + 1
        delay_ms = self.config.backoff_ms(attempts)
        record["wake"] = {
            "attempts": attempts,
            "nextWakeAllowedAt": int(now * 1000) + delay_ms,
        }
        self._persist(record)
        self._log(record, "wake.failed", revision=revision, attempts=attempts, error=error)

    def _persist_pending(self, record: dict[str, Any], pending: dict[str, Any]) -> None:
        record["pendingWake"] = pending
        self._persist(record)


def _find_rest_seconds(result: Any, depth: int = 0) -> int:
    """Find ``restSeconds`` in a tool result, tolerating JSON strings and wrappers."""
    if depth > 3:
        return 0
    if isinstance(result, (bytes, bytearray)):
        try:
            result = result.decode("utf-8", errors="replace")
        except Exception:
            return 0
    if isinstance(result, str):
        text = result.strip()
        if not text.startswith(("{", "[")):
            return 0
        try:
            import json

            result = json.loads(text)
        except Exception:
            return 0
    if isinstance(result, dict):
        for key in ("restSeconds", "rest_seconds"):
            value = result.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0, int(value))
        for value in result.values():
            found = _find_rest_seconds(value, depth + 1)
            if found:
                return found
        return 0
    if isinstance(result, (list, tuple)):
        for value in result:
            found = _find_rest_seconds(value, depth + 1)
            if found:
                return found
    return 0


def _age_seconds(stamp: Any, now: float) -> float:
    try:
        return max(0.0, now - float(stamp))
    except (TypeError, ValueError):
        return 0.0


def _now_iso_offset(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
