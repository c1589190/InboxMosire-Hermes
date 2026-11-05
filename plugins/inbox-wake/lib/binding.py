"""Binding lifecycle and the wake decision.

A *binding* ties one Hermes session (gateway route key) to one InboxMosire inbox. The manager owns
one SSE thread and one debounce timer per binding, keeps a cursor, and decides when the host should be
woken. It deliberately has no claim/complete/push surface: the agent uses the InboxMosire MCP tools for
that, so the adapter can never consume an item the agent never saw.
"""

from __future__ import annotations

import logging
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import oplog, push, safe, template
from .client import InboxClient, InboxError, TokenRef
from .state import Store

logger = logging.getLogger(__name__)

SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")

# 唤醒正文只说「用你的程序 MCP 处理这些 source」。如果这组工具根本不在本进程里，
# 唤醒就是一条无法执行的通知（真实症状：先起 Hermes、后起 iiBot 时唤醒空转）。
# required 缺 = 不许唤醒（等工具就位）；recommended 缺 = 只降级为 partial，仍唤醒。
PROGRAM_TOOL_GROUPS: dict[str, dict[str, tuple[str, ...]]] = {
    "inbox": {"required": ("inbox_list",), "recommended": ("inbox_status",)},
    "iibot": {"required": ("qq_conversation_read",), "recommended": ("qq_conversation_ack", "qq_media_get")},
}
#: wakeProfile → 工具组。generic = 只挂 InboxMosire MCP；iibot = iiBot 业务 MCP。
PROFILE_TOOL_GROUP = {"generic": "inbox", "iibot": "iibot"}
#: 兼容旧语义：inbox 组的 required + recommended（`health.programMcp` 的判定集合）。
PROGRAM_MCP_TOOLS = PROGRAM_TOOL_GROUPS["inbox"]["required"] + PROGRAM_TOOL_GROUPS["inbox"]["recommended"]


def _ownership_key(record: dict[str, Any]) -> tuple[int, str]:
    """显式绑定（manual）优先于 autoBind，同类再比 `updatedAt`。"""
    manual = 1 if str(record.get("bindSource") or "auto") == "manual" else 0
    return (manual, _recency_key(record))


def _recency_key(record: dict[str, Any]) -> str:
    """``updatedAt`` / ``createdAt`` are ISO-8601 strings; lexicographic order matches time order."""
    return str(record.get("updatedAt") or record.get("createdAt") or "")


AUTH_ERROR_CODES = frozenset({"INBOX_AUTH", "UNAUTHORIZED", "UNAUTHENTICATED"})


def _is_auth_error(exc: InboxError) -> bool:
    return exc.status == 401 or exc.code in AUTH_ERROR_CODES


DESCRIPTOR_FILE = "binding-descriptor.json"


def descriptor_ref(token_ref: str, inbox_id: str) -> Optional[str]:
    """Read ``binding-descriptor.json`` next to the token file and return its ``adapterTokenRef``.

    iiBot re-issues ``dsh-wake.token`` on **every** boot and writes the matching descriptor
    beside it. A plugin that only trusts ``config.yaml`` therefore 401s forever after the
    runtime directory changes (bot -> bot2, laptop -> server). The descriptor is the runtime's
    own statement of "this is the file you should be reading", and it sits in the same
    directory as the token file the operator already configured.
    """
    try:
        parsed = TokenRef.parse(token_ref)
    except InboxError:
        return None
    if parsed.kind != "file" or not parsed.target:
        return None
    path = os.path.join(os.path.dirname(os.path.expanduser(parsed.target)), DESCRIPTOR_FILE)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("inboxId") not in (None, "", inbox_id):
        # 描述文件描述的是另一个 inbox：不要拿它覆盖当前绑定。
        return None
    ref = payload.get("adapterTokenRef")
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    try:
        TokenRef.parse(ref).resolve()
    except InboxError:
        return None
    return ref


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
    #: ``inbox.push`` deliveries that arrived while a turn was running (flushed on turn end).
    pending_pushes: list = field(default_factory=list)
    push_timer: Any = None
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
        # ``inbox.push`` 路径：不存正文（正文只走注入），只存 id/计数/metadata 供 status 与 ops log 对照。
        "lastDeliveryId": "",
        "lastPush": {"at": None, "deliveryId": "", "items": 0, "ok": None, "error": None},
        "lastAck": {"at": None, "deliveryId": "", "ok": None, "attempts": 0, "error": None},
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
        #: Optional ``(inbox_id, delivery_id) -> (ok, error)`` ack through the program MCP
        #: (``inbox_ack``). Hermes exposes no synchronous tool-call API to a plugin, so this stays
        #: ``None`` in-process and :meth:`_ack_delivery` falls back to the HTTP route.
        self.mcp_ack: Optional[Callable[[str, str], tuple[bool, str]]] = None

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

    def set_mcp_ack(self, ack: Optional[Callable[[str, str], tuple[bool, str]]]) -> None:
        """Install (or clear) an MCP-based ``inbox_ack`` callable; HTTP stays the fallback."""
        self.mcp_ack = ack

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

    def _apply_descriptor(self, record: dict[str, Any]) -> bool:
        """Adopt the runtime's own descriptor when it points somewhere else. True if adopted."""
        ref = descriptor_ref(str(record.get("tokenRef") or ""), str(record.get("inboxId") or ""))
        if not ref or ref == record.get("tokenRef"):
            return False
        record["tokenRef"] = ref
        record["tokenRefSource"] = "descriptor"
        self._clients.pop(record["sessionKey"], None)
        self._log(record, "token.adopt", source="descriptor", inbox=record.get("inboxId"))
        return True

    def _call_with_token_heal(self, record: dict[str, Any], call):
        """Run one Inbox call; on 401 adopt the runtime descriptor and retry once.

        iiBot re-signs ``dsh-wake.token`` on every boot. Without this, a running gateway keeps
        the stale token (``InboxClient`` caches it) and every wake silently 401s until the
        process restarts — while the descriptor on disk already names the new file.
        """
        try:
            return call()
        except InboxError as exc:
            if not _is_auth_error(exc) or not self._apply_descriptor(record):
                raise
            return call()

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

    @staticmethod
    def _tool_names() -> Optional[set[str]]:
        """Live tool names of this process, or ``None`` when the host registry is unreachable.

        ``None`` means "cannot know" (one-shot CLI, tests, older host) and must fail **open**:
        refusing to wake on an unreadable registry would brick the plugin on innocent hosts.
        """
        try:
            from tools.registry import registry  # type: ignore

            return set(registry.get_all_tool_names() or [])
        except Exception:
            return None

    def _program_tool_requirement(
        self, record: Optional[dict[str, Any]] = None
    ) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        """``(group, required, recommended)`` for this binding; ``programTools`` overrides the group."""
        override = tuple(getattr(self.config, "program_tools", ()) or ())
        if override:
            return ("custom", override, ())
        profile = str((record or {}).get("wakeProfile") or self.config.wake_profile or "generic")
        group = PROFILE_TOOL_GROUP.get(profile, "inbox")
        spec = PROGRAM_TOOL_GROUPS.get(group) or PROGRAM_TOOL_GROUPS["inbox"]
        return (group, tuple(spec["required"]), tuple(spec["recommended"]))

    def _program_tools_state(self, record: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Which program-MCP tools this binding needs, and whether they are actually here."""
        group, required, recommended = self._program_tool_requirement(record)
        detail: dict[str, Any] = {
            "group": group,
            "state": "unknown",
            "required": list(required),
            "recommended": list(recommended),
            "missing": [],
            "missingRecommended": [],
        }
        names = self._tool_names()
        if names is None:
            return detail

        def present(tool: str) -> bool:
            return any(name == tool or name.endswith(f"__{tool}") for name in names)

        missing = [tool for tool in required if not present(tool)]
        missing_recommended = [tool for tool in recommended if not present(tool)]
        if required and len(missing) == len(required):
            state = "missing"
        elif missing or missing_recommended:
            state = "partial"
        else:
            state = "ready"
        detail.update(
            {"state": state, "missing": missing, "missingRecommended": missing_recommended}
        )
        return detail

    def _program_mcp_state(self, record: Optional[dict[str, Any]] = None) -> str:
        """Do the business inbox tools exist in this process? (a wake is useless without them.)"""
        return str(self._program_tools_state(record)["state"])

    # ---- lifecycle ------------------------------------------------------

    async_mode_unused = False  # keeps dataclass-less class explicit for readers

    def resume_all(self) -> int:
        """Start workers for bindings persisted by a previous process.

        Bindings are pruned per inbox first: only the newest ``max_bindings_per_inbox`` stay
        active. Without this, every one-shot/CLI session that ever autoBound becomes a ghost that
        the gateway resurrects on start and re-injects into — N agents answering one event.
        """
        started = 0
        self._prune_passive()  # cap first: a ghost must not start a worker at all
        for key, record in list(self.store.bindings().items()):
            if not record.get("enabled"):
                continue
            if record.get("host") == "none" or record.get("passive"):
                self._log(record, "binding.passive", host=record.get("host"), reason="resume-skip")
                continue
            try:
                # 运行目录可能换了（bot -> bot2）：描述文件才是当前实例的权威。
                self._apply_descriptor(record)
                self._start(record, reason="resume")
                started += 1
            except Exception as exc:
                logger.warning("inbox-wake: cannot resume binding %s: %s", key, exc)
        return started

    def _has_manual_holder(self, record: dict[str, Any]) -> bool:
        for other in self.store.bindings().values():
            if other.get("sessionKey") == record.get("sessionKey"):
                continue
            if other.get("inboxId") != record.get("inboxId"):
                continue
            if str(other.get("apiBaseUrl") or "").rstrip("/") != str(
                record.get("apiBaseUrl") or ""
            ).rstrip("/"):
                continue
            if other.get("enabled") and not other.get("passive") and other.get("bindSource") == "manual":
                return True
        return False

    def _demote_siblings(self, record: dict[str, Any]) -> list[str]:
        """Mark the older bindings of the same inbox passive; keep at most N active."""
        keep = max(1, int(self.config.max_bindings_per_inbox))
        same = [
            other
            for other in self.store.bindings().values()
            if other.get("sessionKey") != record.get("sessionKey")
            and other.get("inboxId") == record.get("inboxId")
            and str(other.get("apiBaseUrl") or "").rstrip("/")
            == str(record.get("apiBaseUrl") or "").rstrip("/")
        ]
        same.sort(key=_ownership_key, reverse=True)
        demoted: list[str] = []
        mine_manual = record.get("bindSource") == "manual"
        for other in same[keep - 1 :] if keep > 1 else same:
            if other.get("passive"):
                continue
            if other.get("bindSource") == "manual" and not mine_manual:
                continue  # 显式绑定优先，自动绑定不许顶掉它
            other["passive"] = True
            other["passiveReason"] = "superseded"
            self._persist(other)
            self._stop(other, reason="superseded")
            self._log(other, "binding.passive", reason="superseded", inbox=other.get("inboxId"))
            demoted.append(str(other.get("sessionKey")))
        return demoted

    def _prune_passive(self) -> None:
        """Apply the per-inbox cap to whatever a previous process left behind (newest wins)."""
        keep = max(1, int(self.config.max_bindings_per_inbox))
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in self.store.bindings().values():
            if not record.get("enabled") or record.get("host") == "none":
                continue
            group = (
                str(record.get("inboxId") or ""),
                str(record.get("apiBaseUrl") or "").rstrip("/"),
            )
            groups.setdefault(group, []).append(record)
        for records in groups.values():
            records.sort(key=_ownership_key, reverse=True)
            for stale in records[keep:]:
                if stale.get("passive"):
                    continue
                stale["passive"] = True
                stale["passiveReason"] = "superseded"
                self._persist(stale)
                self._log(stale, "binding.passive", reason="superseded", inbox=stale.get("inboxId"))

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
        # Set the health record before the worker starts: the SSE thread flips it to
        # "open" the moment the stream connects, and writing "connecting" afterwards
        # would overwrite that (health.sse stuck at connecting forever).
        record["health"]["sse"] = "connecting"
        self._persist(record)
        worker.start()
        self._ensure_poll()
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
                if runtime.push_timer is not None:
                    runtime.push_timer.cancel()
                    runtime.push_timer = None
                runtime.pending = None
                runtime.pending_pushes = []
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
                    if runtime.push_timer is not None:
                        runtime.push_timer.cancel()
                        runtime.push_timer = None
                    runtime.pending_pushes = []

    # ---- public operations ---------------------------------------------

    def bind(self, session_key: str, params: dict[str, Any]) -> dict[str, Any]:
        """Create (or replace) the binding for one session. Returns a public view or an error dict."""
        if not SESSION_KEY_RE.fullmatch(session_key or ""):
            return {"ok": False, "code": "NO_SESSION_KEY", "message": "无法确定当前会话的 session key"}
        capability = self._current_inject_capability()
        if capability == "none":
            # A one-shot / worker process can never deliver a wake; binding there only creates a
            # zombie that the next gateway resumes and re-injects into (one live agent per ghost).
            return {
                "ok": False,
                "code": "NO_INJECT_HOST",
                "message": (
                    "当前宿主进程不能投递唤醒（一次性/worker 进程）：请在 gateway、TUI/桌面、"
                    "api_server 或常驻 messaging 会话里绑定"
                ),
            }

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
        explicit_token_ref = bool(
            params.get("adapterTokenRef") or descriptor.get("adapterTokenRef")
        )
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
        record["host"] = capability
        if not explicit_token_ref:
            # 没人显式指定 token：配置里的路径可能还指着上一个运行目录（旧 token 已随 iiBot
            # 重启重签）。同目录的 binding-descriptor.json 是当前实例自己写的，优先信它。
            self._apply_descriptor(record)
        # manual = 会话里显式 /inbox bind；auto = autoBind。显式的那个钉住，不被后来者顶掉。
        record["bindSource"] = str(params.get("source") or "manual")
        if existing:
            record["lastEventId"] = str(existing.get("lastEventId") or "")
            record["lastObservedRevision"] = int(existing.get("lastObservedRevision") or 0)
            record["lastWokenRevision"] = int(existing.get("lastWokenRevision") or 0)
            record["createdAt"] = existing.get("createdAt") or record["createdAt"]
            self._stop(existing, reason="rebind")
        self._persist(record)
        self._start(record, reason="bind")
        health = self._refresh_status(record)
        if record.get("bindSource") == "auto" and self._has_manual_holder(record):
            # 有人在显式会话里绑过：新来的自动绑定排队等，抢不走事件。
            record["passive"] = True
            record["passiveReason"] = "manual-holder"
            self._persist(record)
            self._stop(record, reason="manual-holder")
            self._log(record, "binding.passive", reason="manual-holder", inbox=inbox_id)
            demoted: list[str] = []
        else:
            demoted = self._demote_siblings(record)
        self._log(
            record,
            "binding.bind",
            inbox=inbox_id,
            mode=wake_mode,
            channels=channels,
            source=record.get("bindSource"),
            demoted=len(demoted),
        )
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
        program = self._program_tools_state(record)
        record["health"]["programMcp"] = program["state"]
        record["health"]["programTools"] = program
        runtime = self._runtime.get(session_key)
        if runtime is not None and runtime.turn_running:
            record["health"]["turn"] = "running"
        else:
            record["health"]["turn"] = "idle"
        self._persist(record)
        return {"ok": True, "binding": self._public(record)}

    def owners(self) -> list[dict[str, Any]]:
        """Every binding on this host, active first. Lets the operator see who eats the inbox."""
        records = list(self.store.bindings().values())
        records.sort(key=_ownership_key, reverse=True)
        out: list[dict[str, Any]] = []
        for record in records:
            out.append(
                {
                    "sessionKey": record.get("sessionKey"),
                    "sessionId": record.get("sessionId"),
                    "inboxId": record.get("inboxId"),
                    "apiBaseUrl": record.get("apiBaseUrl"),
                    "active": not bool(record.get("passive")),
                    "passiveReason": record.get("passiveReason"),
                    "source": record.get("bindSource") or "manual",
                    "host": record.get("host") or "unknown",
                    "updatedAt": record.get("updatedAt") or record.get("createdAt"),
                }
            )
        return out

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
            result = self.bind(session_key, {"source": "auto"})
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
        with runtime.lock:
            parked = bool(runtime.pending_pushes)
        if parked:
            # 本轮跑着时到达的 push 正文，等这一轮结束再注入（它走自己的路径，不合成 changed 唤醒）。
            self._schedule_pushes(session_key, delay_ms=max(50, self.config.debounce_ms))

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
        if push.is_push(event):
            # ``inbox.push`` 自带服务端渲染好的整段正文：原样注入 + 立即 ack，不走 revision 唤醒路径
            # （那条路径会重新拉列表、自己渲染，违反 D-002/D-015「只注入那一段 String」）。
            self._on_push_event(session_key, event)
            return
        delay_ms = self.config.debounce_ms
        if getattr(self.config, "push_mode", "auto") == "auto":
            record = self.store.get_binding(session_key) or {}
            if not record.get("pushMode"):
                # 宽限期：等 push 先到；到了就撤，没到才用 changed 兜底。
                delay_ms = max(delay_ms, int(getattr(self.config, "push_grace_ms", 0) or 0))
        self._schedule(session_key, delay_ms=delay_ms, reason="event")

    # ---- push path ------------------------------------------------------

    def _on_push_event(self, session_key: str, event: dict[str, Any]) -> None:
        """Handle one ``inbox.push`` delivery: dedupe → inject → ack (in that order)."""
        record = self.store.get_binding(session_key)
        if record is None:
            return
        if getattr(self.config, "push_mode", "auto") == "off":
            # 显式只用 changed（旧服务端兼容态）：push 一律不消费，也不 ack。
            self._log(record, "push.skipped", reason="mode-off")
            return
        envelope = push.parse_push(event)
        if envelope is None:
            self._log(record, "push.invalid")
            return
        if not record.get("enabled") or record.get("passive"):
            self._log(record, "push.skipped", reason="binding-inactive", deliveryId=envelope.delivery_id)
            return
        inbox_id = str(record.get("inboxId") or "")
        if envelope.inbox and inbox_id and envelope.inbox != inbox_id:
            # 服务端已按订阅过滤；这里再挡一次，免得跨板投递被注进错误的会话。
            self._log(record, "push.skipped", reason="inbox-mismatch", deliveryId=envelope.delivery_id)
            return
        runtime_pending = self._runtime.get(session_key)
        if runtime_pending is not None and runtime_pending.timer is not None:
            # auto 模式下 changed 会先排一个宽限唤醒；push 一到就把它撤掉，避免同一批消息注入两次。
            runtime_pending.timer.cancel()
            runtime_pending.timer = None
            self._log(record, "push.cancelled-changed", deliveryId=envelope.delivery_id)
        if not record.get("pushMode"):
            # 服务端一旦能推 push，就由 push 接管投递：changed 唤醒只会把同一批消息再报一遍
            # （实测：同一条消息先被 changed 唤醒报一次，再被 push 原样注入一次）。
            record["pushMode"] = True
            record["pushModeSince"] = _now_iso()
            self._persist(record)
            self._log(record, "push.mode", deliveryId=envelope.delivery_id)
        if push.delivery_seen(self.store, session_key, envelope.delivery_id, inbox_id):
            # 同一个 deliveryId 只注入一次（水位落在 ctx.state 里，进程重启后依然生效）。
            self._log(record, "push.duplicate", deliveryId=envelope.delivery_id)
            return
        if self._current_inject_capability() == "none":
            # 没注入能力的进程（one-shot / worker）既不能投递也不能 ack：ack 了 Agent 就永远看不到。
            self._log(record, "push.skipped", reason="no-inject-host", deliveryId=envelope.delivery_id)
            return
        runtime = self._runtime_for(session_key)
        with runtime.lock:
            busy = runtime.turn_running
            if busy:
                runtime.pending_pushes.append(envelope)
        if busy:
            self._log(record, "push.defer", reason="turn-running", deliveryId=envelope.delivery_id)
            return
        self._deliver_push(record, envelope)

    def _deliver_push(self, record: dict[str, Any], envelope: push.PushEnvelope) -> None:
        """Inject the server-rendered text verbatim, remember the delivery, then ack once."""
        session_key = record["sessionKey"]
        inbox_id = str(record.get("inboxId") or "")
        media_urls, skipped = push.local_media(envelope.attachments)
        if skipped:
            logger.warning(
                "inbox-wake: push %s 有 %d 个附件不是本地绝对路径，已跳过（不下载远端 URL）：%s",
                envelope.delivery_id,
                len(skipped),
                safe.clean_text(", ".join(item for item in skipped if item), 200),
            )
            self._log(
                record,
                "push.attachment.skipped",
                deliveryId=envelope.delivery_id,
                count=len(skipped),
            )
        self._maybe_send_guide(record, session_key)
        ok, error = self._inject_wake(envelope.text, session_key, media_urls=media_urls)
        if not ok:
            record["lastPush"] = {
                "at": _now_iso(),
                "deliveryId": envelope.delivery_id,
                "items": len(envelope.item_ids),
                "important": envelope.important,
                "media": len(media_urls),
                "ok": False,
                "error": safe.clean_text(error, 200),
            }
            self._persist(record)
            self._log(record, "push.failed", deliveryId=envelope.delivery_id, error=error)
            return  # 没注入成功就不 ack：条目必须还留在板上
        push.mark_delivered(
            self.store, session_key, envelope.delivery_id, envelope.item_ids, inbox_id
        )
        record["lastDeliveryId"] = envelope.delivery_id
        record["lastPush"] = {
            "at": _now_iso(),
            "deliveryId": envelope.delivery_id,
            "revision": envelope.revision,
            "items": len(envelope.item_ids),
            "important": envelope.important,
            "remaining": envelope.remaining,
            "media": len(media_urls),
            "ok": True,
            "error": None,
        }
        self._persist(record)
        self._log(
            record,
            "push.ok",
            deliveryId=envelope.delivery_id,
            items=len(envelope.item_ids),
            important=envelope.important,
            media=len(media_urls),
        )
        ack_ok, ack_error = self._ack_delivery(record, envelope.delivery_id)
        if ack_ok:
            self._log(record, "push.ack", deliveryId=envelope.delivery_id)
            return
        # ack 失败只 warning：正文已经交给 Agent 了，重投只会让同一条消息出现两次。
        logger.warning(
            "inbox-wake: push ack 失败 delivery=%s: %s（不重复注入，也不再重试）",
            envelope.delivery_id,
            ack_error,
        )
        self._log(record, "push.ack.failed", deliveryId=envelope.delivery_id, error=ack_error)

    #: 首条注入前先发一次的工作区说明（D-009）。只说「去读哪两个文件」，不复述内容。
    DEFAULT_GUIDE = (
        "【工作区说明】本工作区常驻两份文档：AGENTS.md 是硬规则与路径约定，"
        "PERSONA.md 是我的身份与说话方式。开工前先读这两份；之后按需查阅，不要假设已经读过。"
    )

    def _maybe_send_guide(self, record: dict[str, Any], session_key: str) -> None:
        """每个会话只发一次：引导 Agent 自己去读 AGENTS.md / PERSONA.md。"""
        if record.get("guideSent") or not bool(getattr(self.config, "first_run_guide", True)):
            return
        text = str(getattr(self.config, "first_run_guide_text", "") or "").strip()
        if not text:
            text = self.DEFAULT_GUIDE
        ok, error = self._inject_wake(text, session_key)
        if not ok:
            logger.warning("inbox-wake: 首条工作区说明注入失败，下条推送再试：%s", safe.clean_text(error, 200))
            self._log(record, "guide.failed", error=error)
            return
        record["guideSent"] = True
        record["guideSentAt"] = _now_iso()
        self._persist(record)
        self._log(record, "guide.sent")

    def _inject_wake(
        self,
        text: str,
        session_key: str,
        *,
        media_urls: Optional[list[str]] = None,
    ) -> tuple[bool, str]:
        """One injection entry for both paths: ``self.inject`` = host ``ctx.inject_message``.

        没有附件时**不多传任何关键字**，保持旧的两参数调用（老宿主与测试的注入器只接受两个参数）；
        有附件时带 ``media_urls``，注入器不认这个关键字（``TypeError``）就降级为纯文本并 warning 一次。
        """
        if media_urls:
            try:
                return self.inject(text, session_key, media_urls=list(media_urls))
            except TypeError:
                logger.warning(
                    "inbox-wake: 注入入口不接受 media_urls，改为纯文本注入（%d 个附件未随行）",
                    len(media_urls),
                )
        return self.inject(text, session_key)

    def _ack_delivery(self, record: dict[str, Any], delivery_id: str) -> tuple[bool, str]:
        """Ack through the program MCP ``inbox_ack`` when one is installed, else over HTTP."""
        inbox_id = str(record.get("inboxId") or "")
        mcp = self.mcp_ack
        if mcp is not None:
            try:
                ok, error = mcp(inbox_id, delivery_id)
            except Exception as exc:  # noqa: BLE001 - a broken MCP must not lose the delivery
                ok, error = False, f"{type(exc).__name__}: {safe.clean_text(str(exc), 200)}"
            if ok:
                return True, ""
            self._log(record, "push.ack.mcp-fallback", deliveryId=delivery_id, error=error)

        def call(target_inbox: str, target_delivery: str) -> dict:
            client = self._client(record)
            return self._call_with_token_heal(
                record, lambda: client.ack_delivery(target_inbox, target_delivery)
            )

        ok, error, attempts = push.ack_delivery(
            call, inbox_id=inbox_id, delivery_id=delivery_id, retries=1
        )
        record["lastAck"] = {
            "at": _now_iso(),
            "deliveryId": delivery_id,
            "ok": ok,
            "attempts": attempts,
            "error": None if ok else error,
        }
        self._persist(record)
        return ok, error

    def _flush_pushes(self, session_key: str) -> None:
        """Deliver pushes that were parked because a turn was running."""
        runtime = self._runtime.get(session_key)
        if runtime is None:
            return
        with runtime.lock:
            runtime.push_timer = None
            pending = list(runtime.pending_pushes)
            runtime.pending_pushes = []
            busy = runtime.turn_running
        if not pending:
            return
        record = self.store.get_binding(session_key)
        if record is None or not record.get("enabled"):
            return
        if busy:
            with runtime.lock:
                runtime.pending_pushes = pending + runtime.pending_pushes
            self._schedule_pushes(session_key, delay_ms=self.config.wake_retry_seconds * 1000)
            return
        inbox_id = str(record.get("inboxId") or "")
        for envelope in pending:
            if push.delivery_seen(self.store, session_key, envelope.delivery_id, inbox_id):
                continue
            self._deliver_push(record, envelope)

    def _schedule_pushes(self, session_key: str, *, delay_ms: int) -> None:
        runtime = self._runtime.get(session_key)
        if runtime is None:
            return
        with runtime.lock:
            if runtime.push_timer is not None:
                runtime.push_timer.cancel()
            timer = threading.Timer(
                max(0, delay_ms) / 1000.0, self._flush_pushes, args=(session_key,)
            )
            timer.daemon = True
            runtime.push_timer = timer
        timer.start()

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
            # 绑定可能只看几个频道：status/items 都带上，否则多来源 inbox 的 available 会串味
            payload = self._call_with_token_heal(
                record,
                lambda: self._client(record).status(
                    record["inboxId"], channels=record.get("channels") or None
                ),
            )
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
            items = self._call_with_token_heal(
                record,
                lambda: self._client(record).items(
                    record["inboxId"],
                    limit=config.summary_max_items,
                    channels=record.get("channels") or None,
                ),
            )
        except InboxError as exc:
            return [], 0, {"state": "failed", "code": exc.code, "message": safe.clean_text(str(exc), 160)}
        return items, 0, {"state": "ok", "code": None, "message": None}

    def _content_items(self, record: dict[str, Any]) -> list[dict]:
        """Opt-in body preview: the list view has no ``content``, so fetch each detail.

        列表接口（GET /items）只返回 ItemSummaryView（itemId/channel/kind/summary/title/source），
        正文只在 GET /items/{id} 的 ItemDetailView 里。以前这里直接拿列表结果去渲染正文，
        于是 ``wakeContent=bounded`` 永远是空的。现在逐条取详情，单条失败不影响其他条目。
        """
        if self.config.wake_content == "off":
            return []
        try:
            listed = self._call_with_token_heal(
                record,
                lambda: self._client(record).items(
                    record["inboxId"],
                    limit=self.config.summary_max_items,
                    channels=record.get("channels") or None,
                ),
            )
        except InboxError:
            return []
        details: list[dict] = []
        for summary in listed:
            item_id = summary.get("itemId") or summary.get("id")
            if not safe.is_safe_id(item_id):
                continue
            try:
                detail = self._call_with_token_heal(
                    record, lambda item_id=item_id: self._client(record).item(
                        record["inboxId"], item_id
                    )
                )
            except InboxError:
                continue
            if isinstance(detail, dict) and detail:
                details.append(detail)
        return details

    def _maybe_wake(self, session_key: str, reason: str) -> None:
        record = self.store.get_binding(session_key)
        if record is None or not record.get("enabled"):
            return
        runtime = self._runtime.get(session_key)
        if runtime is None:
            return
        if record.get("passive"):
            revision = int(record.get("lastObservedRevision") or 0)
            if self._skip_logged.get(session_key) != revision:
                self._skip_logged[session_key] = revision
                self._log(record, "wake.skipped", reason="passive-binding", revision=revision)
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
        push_authoritative = (
            getattr(self.config, "push_mode", "auto") == "on" or bool(record.get("pushMode"))
        )
        if push_authoritative:
            # push 接管后 changed 只用来推进水位：再拼一条唤醒就会重复注入。
            if revision > last_woken:
                record["lastWokenRevision"] = revision
                self._persist(record)
            self._log(record, "wake.skipped", reason="push-authoritative", revision=revision)
            return
        if revision <= last_woken:
            return

        # 冷启动守卫：工具还没就位时唤醒只会得到一条无法执行的通知（agent 会以为 QQ 侧
        # 有东西却调不了 qq_conversation_read）。不推进 lastWokenRevision，工具一出现就补上。
        if getattr(self.config, "program_tools_required", True):
            program = self._program_tools_state(record)
            if program["missing"]:
                if self._skip_logged.get(session_key) != revision:
                    self._skip_logged[session_key] = revision
                    self._log(
                        record,
                        "wake.skipped",
                        reason="program-tools-missing",
                        revision=revision,
                        group=program["group"],
                        missing=program["missing"],
                    )
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
        # push 已经原样注入过的条目不再进 changed 唤醒（同一批消息只出现一次）。
        skip_ids = push.skipped_item_ids(self.store, session_key, str(record.get("inboxId") or ""))

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
            skip_item_ids=skip_ids,
        )
        key = template.wake_key(
            inbox_id=record["inboxId"], revision=revision, session_ref=str(record.get("sessionId") or session_key)
        )
        message_id = template.wake_message_id(key)

        self._maybe_send_guide(record, session_key)
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
