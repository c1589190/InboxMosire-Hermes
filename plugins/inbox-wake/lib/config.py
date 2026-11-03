"""Settings for the inbox-wake plugin.

Defaults mirror ``@mosire/dsh-inbox-mosire`` ``lib/config.js`` so an operator moving a session from
DSH to Hermes keeps the same timing and summary behaviour. Every value is clamped: a bad
``config.yaml`` entry degrades to the default instead of killing the wake path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

from . import safe

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

DEFAULT_API_URL = "http://127.0.0.1:18765"
DEFAULT_INBOX_ID = "main"
#: 默认唤醒协议。iiBot 的主流程是 view-ack（读→ack 推进未读），claim 只是另一套宿主协议；
#: 默认给 claim 会让「按 view-ack 部署的 Bot」在没显式配置时拿到拿不动的指令。
DEFAULT_WAKE_MODE = "view-ack"
DEFAULT_WAKE_PROFILE = "generic"
DEFAULT_WAKE_CONTENT = "bounded"

WAKE_MODES = ("claim", "view-ack")
WAKE_PROFILES = ("generic", "iibot")
WAKE_CONTENT_MODES = ("off", "bounded", "full")


@dataclass
class Config:
    enabled: bool = True
    api_url: str = DEFAULT_API_URL
    inbox_id: str = DEFAULT_INBOX_ID
    channels: list[str] = field(default_factory=list)
    adapter_token_file: str = ""
    wake_mode: str = DEFAULT_WAKE_MODE
    wake_profile: str = DEFAULT_WAKE_PROFILE
    summary_enabled: bool = True
    summary_max_items: int = 3
    summary_max_title_chars: int = 80
    summary_max_total_chars: int = 1200
    wake_content: str = DEFAULT_WAKE_CONTENT
    wake_content_max_chars: int = 4000
    wake_content_total_chars: int = 12000
    debounce_ms: int = 300
    heartbeat_timeout_seconds: int = 45
    sse_read_timeout_seconds: int = 45
    poll_seconds: int = 0
    reconnect_base_ms: int = 1000
    reconnect_max_ms: int = 60000
    wake_backoff_base_ms: int = 30000
    wake_backoff_max_ms: int = 900000
    request_timeout_ms: int = 10000
    max_bindings: int = 8
    #: 每个 (apiUrl, inboxId) 只保留这么多个**活跃**绑定；多的降级成 passive（不订阅、不唤醒）。
    #: 僵尸来源：每次 one-shot / CLI 会话都可能 autoBind 一条，会话结束后没人解绑。
    max_bindings_per_inbox: int = 1
    pending_ttl_seconds: int = 600
    # A local (classic REPL / desktop) session is only *live* inside the gateway while a
    # turn is running, so an idle wake there is refused for reasons the item cannot fix.
    # Retry at this fixed short interval instead of climbing the shared backoff ladder.
    wake_retry_seconds: int = 15
    auto_bind: bool = False
    #: 唤醒前必须存在的「程序 MCP」工具名；空 = 按 wakeProfile 选工具组
    #: （generic→inbox_list，iibot→qq_conversation_read）。缺 required 就不唤醒，
    #: 否则会出现「唤醒了却调不了 qq_conversation_read」的假故障。
    program_tools: list[str] = field(default_factory=list)
    #: 关掉它 = 退回旧行为（工具缺失也唤醒）。仅在宿主没有工具 registry 时才有意义。
    program_tools_required: bool = True
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Config":
        cfg = cls()
        warnings: list[str] = []
        get = raw.get

        cfg.enabled = safe.coerce_bool(get("enabled"), True)
        api_url = str(get("apiUrl") or DEFAULT_API_URL).strip().rstrip("/")
        parsed = urlparse(api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            warnings.append(f"apiUrl 不可用（{api_url!r}），已回退 {DEFAULT_API_URL}")
            api_url = DEFAULT_API_URL
        elif parsed.hostname not in LOOPBACK_HOSTS:
            warnings.append(
                f"apiUrl 指向非回环地址 {parsed.hostname}；工作组约定默认监听必须回环，请确认这是有意为之"
            )
        cfg.api_url = api_url

        inbox = str(get("inboxId") or DEFAULT_INBOX_ID).strip()
        if not safe.is_safe_id(inbox):
            warnings.append(f"inboxId 非法（{inbox!r}），已回退 {DEFAULT_INBOX_ID}")
            inbox = DEFAULT_INBOX_ID
        cfg.inbox_id = inbox

        cfg.channels = safe.as_channels(get("channels"))
        cfg.adapter_token_file = str(get("adapterTokenFile") or "").strip()

        mode = str(get("wakeMode") or DEFAULT_WAKE_MODE).strip().lower()
        if mode not in WAKE_MODES:
            warnings.append(f"wakeMode 非法（{mode!r}），已回退 {DEFAULT_WAKE_MODE}")
            mode = DEFAULT_WAKE_MODE
        cfg.wake_mode = mode

        profile = str(get("wakeProfile") or DEFAULT_WAKE_PROFILE).strip().lower()
        if profile not in WAKE_PROFILES:
            warnings.append(f"wakeProfile 非法（{profile!r}），已回退 {DEFAULT_WAKE_PROFILE}")
            profile = DEFAULT_WAKE_PROFILE
        cfg.wake_profile = profile

        cfg.summary_enabled = safe.coerce_bool(get("summaryEnabled"), True)
        # 「最多三条」是原先的人为限制：一张板可能有几十条来源消息，硬钳 3 条只会让
        # Agent 反复补调工具。字符预算（summaryMaxTotalChars）才是真正的护栏。
        cfg.summary_max_items = safe.coerce_int(
            get("summaryMaxItems"), 5, minimum=1, maximum=50
        )
        cfg.summary_max_title_chars = safe.coerce_int(
            get("summaryMaxTitleChars"), 80, minimum=8, maximum=200
        )
        cfg.summary_max_total_chars = safe.coerce_int(
            get("summaryMaxTotalChars"), 1200, minimum=120, maximum=8000
        )

        content = str(get("wakeContent") or DEFAULT_WAKE_CONTENT).strip().lower()
        if content not in WAKE_CONTENT_MODES:
            warnings.append(f"wakeContent 非法（{content!r}），已回退 {DEFAULT_WAKE_CONTENT}")
            content = DEFAULT_WAKE_CONTENT
        cfg.wake_content = content
        cfg.wake_content_max_chars = safe.coerce_int(
            get("wakeContentMaxChars"), 4000, minimum=80, maximum=8000
        )
        cfg.wake_content_total_chars = safe.coerce_int(
            get("wakeContentTotalChars"), 12000, minimum=200, maximum=24000
        )

        cfg.debounce_ms = safe.coerce_int(get("debounceMs"), 300, minimum=0, maximum=10000)
        cfg.heartbeat_timeout_seconds = safe.coerce_int(
            get("heartbeatTimeoutSeconds"), 45, minimum=5, maximum=600
        )
        # The SSE socket must NOT reuse requestTimeoutMs (short request/response budget):
        # a quiet-but-healthy stream would be torn down every few seconds.
        cfg.sse_read_timeout_seconds = safe.coerce_int(
            get("sseReadTimeoutSeconds"),
            cfg.heartbeat_timeout_seconds,
            minimum=10,
            maximum=1800,
        )
        # Optional reconcile poll: makes the wake path independent of SSE heartbeats.
        cfg.poll_seconds = safe.coerce_int(get("pollSeconds"), 0, minimum=0, maximum=3600)
        cfg.reconnect_base_ms = safe.coerce_int(
            get("reconnectBaseMs"), 1000, minimum=100, maximum=60000
        )
        cfg.reconnect_max_ms = safe.coerce_int(
            get("reconnectMaxMs"), 60000, minimum=1000, maximum=900000
        )
        cfg.wake_backoff_base_ms = safe.coerce_int(
            get("wakeBackoffBaseMs"), 30000, minimum=1000, maximum=900000
        )
        cfg.wake_backoff_max_ms = safe.coerce_int(
            get("wakeBackoffMaxMs"), 900000, minimum=1000, maximum=3600000
        )
        cfg.wake_retry_seconds = safe.coerce_int(
            get("wakeRetrySeconds"), cfg.wake_retry_seconds, minimum=5, maximum=300
        )
        cfg.request_timeout_ms = safe.coerce_int(
            get("requestTimeoutMs"), 10000, minimum=500, maximum=60000
        )
        cfg.max_bindings = safe.coerce_int(get("maxBindings"), 8, minimum=1, maximum=32)
        cfg.max_bindings_per_inbox = safe.coerce_int(
            get("maxBindingsPerInbox"), cfg.max_bindings_per_inbox, minimum=1, maximum=64
        )
        cfg.auto_bind = safe.coerce_bool(get("autoBind"), False)
        cfg.program_tools = safe.as_channels(get("programTools"))
        cfg.program_tools_required = safe.coerce_bool(get("programToolsRequired"), True)
        cfg.warnings = warnings
        return cfg

    @classmethod
    def from_ctx(cls, ctx: Any) -> "Config":
        raw: dict[str, Any] = {}
        get = getattr(ctx, "get_config", None)
        for key in (
            "enabled", "apiUrl", "inboxId", "channels", "adapterTokenFile", "wakeMode",
            "wakeProfile", "summaryEnabled", "summaryMaxItems", "summaryMaxTitleChars",
            "summaryMaxTotalChars", "wakeContent", "wakeContentMaxChars",
            "wakeContentTotalChars", "debounceMs", "heartbeatTimeoutSeconds",
            "reconnectBaseMs", "reconnectMaxMs", "wakeBackoffBaseMs", "wakeBackoffMaxMs", "wakeRetrySeconds",
            "requestTimeoutMs", "maxBindings", "maxBindingsPerInbox", "autoBind", "sseReadTimeoutSeconds", "pollSeconds",
            "programTools", "programToolsRequired",
        ):
            if not callable(get):
                break
            try:
                value = get(key, None)
            except Exception:
                continue
            if value is not None:
                raw[key] = value
        return cls.from_mapping(raw)

    def backoff_ms(self, attempts: int) -> int:
        """Exponential wake backoff, capped: base * 2**(attempts-1)."""
        if attempts <= 0:
            return 0
        return min(self.wake_backoff_max_ms, self.wake_backoff_base_ms * (2 ** (attempts - 1)))

    def request_timeout_seconds(self) -> float:
        return max(0.5, self.request_timeout_ms / 1000.0)
