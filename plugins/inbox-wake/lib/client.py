"""Minimal InboxMosire HTTP client for the wake adapter.

Only the read/observe surface the wake-adapter contract allows is implemented here: ``/status``,
``/items``, ``/items/{id}``, occupancy and the SSE stream. Claim / complete / release / push are
*not* implemented on purpose — those are the agent's business tools (InboxMosire MCP), and an
adapter that can claim is an adapter that can silently consume someone else's work.

唯一例外是 ``ack_delivery``（``POST /deliveries/{id}/ack``）：``inbox.push`` 的正文已经原样
注入给 Agent，ack 只表示「这条投递已经交给 Agent 了」，不 claim、不 complete 任何条目。
该例外在 ``docs/DESIGN.md`` 与 ``docs/DSH-PARITY.md`` 登记。
"""

from __future__ import annotations

import json
import logging
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from . import safe


logger = logging.getLogger(__name__)

TOKEN_FILE_MODE = 0o600


class InboxError(RuntimeError):
    """Transport/HTTP failure with a stable, log-safe code."""

    def __init__(self, code: str, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "status": self.status, "message": str(self)}


@dataclass
class TokenRef:
    """A reference to where the bearer token lives — never the token itself."""

    kind: str  # "file" | "env" | ""
    target: str = ""

    @classmethod
    def parse(cls, ref: Any) -> "TokenRef":
        text = str(ref or "").strip()
        if not text:
            return cls("", "")
        lowered = text.lower()
        if lowered.startswith("file:"):
            return cls("file", text[5:].strip())
        if lowered.startswith("env:"):
            return cls("env", text[4:].strip())
        if lowered.startswith("inline:"):
            raise InboxError(
                "TOKEN_INLINE_FORBIDDEN",
                "adapterTokenRef 不接受 inline: 形式；请把 token 放进 0600 文件并用 file:<path>",
            )
        return cls("file", text)

    def describe(self) -> str:
        if self.kind == "file":
            return f"file:{self.target}"
        if self.kind == "env":
            return f"env:{self.target}"
        return "<unset>"

    def resolve(self) -> str:
        if self.kind == "file":
            path = os.path.expanduser(self.target)
            if not path:
                raise InboxError("TOKEN_MISSING", "未配置 adapter token 文件")
            try:
                info = os.stat(path)
            except OSError as exc:
                raise InboxError("TOKEN_UNREADABLE", f"无法读取 token 文件 {path}: {exc.strerror}") from exc
            if info.st_mode & 0o077:
                raise InboxError(
                    "TOKEN_FILE_PERMISSIONS",
                    f"token 文件 {path} 权限过宽（需 0600，当前 {stat.filemode(info.st_mode)}）",
                )
            with open(path, "r", encoding="utf-8") as handle:
                token = handle.read().strip()
            if not token:
                raise InboxError("TOKEN_EMPTY", f"token 文件 {path} 是空的")
            return token
        if self.kind == "env":
            token = os.environ.get(self.target, "").strip()
            if not token:
                raise InboxError("TOKEN_MISSING", f"环境变量 {self.target} 未设置或为空")
            return token
        raise InboxError("TOKEN_MISSING", "未配置 adapter token 引用")


class InboxClient:
    """Blocking client; every call is short-lived and time-boxed."""

    def __init__(self, base_url: str, token_ref: TokenRef, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_ref = token_ref
        self.timeout_seconds = timeout_seconds
        self._token: Optional[str] = None

    # ---- plumbing -------------------------------------------------------

    def _auth_token(self) -> str:
        if self._token is None:
            self._token = self.token_ref.resolve()
        return self._token

    def drop_token_cache(self) -> None:
        self._token = None

    def reload_token(self) -> Optional[str]:
        """Re-read the token file; returns the new value only when it actually changed."""
        previous = self._token
        self._token = None
        try:
            fresh = self._auth_token()
        except InboxError:
            self._token = previous
            return None
        if fresh == previous:
            self._token = fresh
            return None
        return fresh

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._auth_token()}",
            "Accept": "application/json",
            "User-Agent": "hermes-inbox-wake/0.1",
        }
        if extra:
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    def _url(self, path: str, query: Optional[Mapping[str, Any]] = None) -> str:
        url = f"{self.base_url}{path}"
        if query:
            cleaned = {
                k: v for k, v in query.items() if v is not None and (v or v == 0)
            }
            if cleaned:
                # doseq=True 让 list/tuple 变成重复的 key（服务端 channels() 支持 `channel=a&channel=b`）
                url = f"{url}?{urllib.parse.urlencode(cleaned, doseq=True)}"
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        payload = None
        headers = self._headers()
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._url(path, query), data=payload, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            code, message = _error_from_body(exc)
            if exc.code == 401:
                # iiBot 每次启动都重新签发 dsh-wake.token 并覆盖同一个文件；缓存里的旧 token
                # 会一路 401 到进程重启。重读文件，只有真的变了才重试（避免无意义的重放）。
                fresh = self.reload_token()
                if fresh is not None:
                    logger.info("inbox-wake: adapter token rotated on disk; retrying once")
                    return self._request(
                        method, path, body=body, query=query, timeout=timeout
                    )
                message = (
                    f"{message}（adapter token 已失效：确认 adapterTokenFile 指向**当前运行实例**的 "
                    "data 目录；iiBot 每次启动都会重签 dsh-wake.token）"
                )
            raise InboxError(code, message, status=exc.code) from None
        except urllib.error.URLError as exc:
            raise InboxError("TRANSPORT", f"无法连接 InboxMosire: {exc.reason}") from None
        except TimeoutError:
            raise InboxError("TIMEOUT", "InboxMosire 请求超时") from None
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InboxError("BAD_PAYLOAD", "InboxMosire 返回了非 JSON 响应") from None

    def _inbox_path(self, inbox_id: str, suffix: str = "") -> str:
        if not safe.is_safe_id(inbox_id):
            raise InboxError("INVALID_ARGUMENT", f"inboxId 非法: {inbox_id!r}")
        return f"/v1/inboxes/{urllib.parse.quote(inbox_id)}{suffix}"

    # ---- API ------------------------------------------------------------

    def status(
        self, inbox_id: str, *, channels: Optional[Sequence[str]] = None
    ) -> dict[str, Any]:
        # 服务端 GET /status 支持 channel 过滤（InboxHttpServer.status → service.status(id, channels)）；
        # 不传就是「整个 inbox」，多来源的 inbox 必须传，否则 available/revision 会混入别的频道。
        data = self._request(
            "GET",
            self._inbox_path(inbox_id, "/status"),
            query={"channel": list(channels) if channels else None},
        )
        return data if isinstance(data, dict) else {}

    def items(
        self,
        inbox_id: str,
        *,
        limit: int = 3,
        channel: Optional[str] = None,
        channels: Optional[Sequence[str]] = None,
    ) -> list[dict]:
        names = [c for c in (channels if channels is not None else ([channel] if channel else [])) if c]
        data = self._request(
            "GET",
            self._inbox_path(inbox_id, "/items"),
            # 服务端 listAvailable 的合法区间是 1..100（InboxService.MAX_REQUEST_ITEMS=50 是 claim 的上限）
            query={"limit": max(1, min(int(limit), 100)), "channel": names or None},
        )
        if isinstance(data, dict):
            for key in ("items", "data", "results"):
                if isinstance(data.get(key), list):
                    return [item for item in data[key] if isinstance(item, dict)]
            return []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def item(self, inbox_id: str, item_id: str) -> dict[str, Any]:
        if not safe.is_safe_id(item_id):
            raise InboxError("INVALID_ARGUMENT", f"itemId 非法: {item_id!r}")
        data = self._request(
            "GET", self._inbox_path(inbox_id, f"/items/{urllib.parse.quote(item_id)}")
        )
        return data if isinstance(data, dict) else {}

    def ack_delivery(self, inbox_id: str, delivery_id: str) -> dict[str, Any]:
        """``POST /v1/inboxes/{inbox}/deliveries/{deliveryId}/ack`` (scope ``items:manage``).

        这是本插件唯一一次 items 写操作：``inbox.push`` 的正文已经原样注入给 Agent，
        ack 只是把这条投递的条目关掉（重复 ack 服务端返回 ``acked: false``，幂等）。
        ``InboxHttpServer.ackDelivery`` 不读请求体，发一个 JSON ``{}`` 只为带上正确的 Content-Type。
        """
        if not safe.is_safe_id(delivery_id):
            raise InboxError("INVALID_ARGUMENT", f"deliveryId 非法: {delivery_id!r}")
        data = self._request(
            "POST",
            self._inbox_path(inbox_id, f"/deliveries/{urllib.parse.quote(delivery_id)}/ack"),
            body={},
        )
        return data if isinstance(data, dict) else {}

    def occupancy(self, inbox_id: str) -> dict[str, Any]:
        data = self._request("GET", self._inbox_path(inbox_id, "/occupancy"))
        return data if isinstance(data, dict) else {}

    def put_occupancy(self, inbox_id: str, session_ref: str, lease_seconds: int = 300) -> dict[str, Any]:
        data = self._request(
            "PUT",
            self._inbox_path(inbox_id, "/occupancy"),
            body={"sessionRef": session_ref, "leaseSeconds": int(lease_seconds)},
        )
        return data if isinstance(data, dict) else {}

    def delete_occupancy(self, inbox_id: str, session_ref: str) -> None:
        try:
            self._request(
                "DELETE", self._inbox_path(inbox_id, "/occupancy"), query={"sessionRef": session_ref}
            )
        except InboxError as exc:
            if exc.status in {404, 405, 409}:
                return
            raise

    def open_events(
        self, inbox_id: str, *, last_event_id: str = "", timeout: Optional[float] = None
    ) -> Any:
        """Open the SSE stream; the caller owns the returned response object."""
        headers = self._headers(
            {"Accept": "text/event-stream", "Cache-Control": "no-cache, no-store"}
        )
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        request = urllib.request.Request(
            self._url(self._inbox_path(inbox_id, "/events")), headers=headers, method="GET"
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout or self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            code, message = _error_from_body(exc)
            raise InboxError(code, message, status=exc.code) from None
        except urllib.error.URLError as exc:
            raise InboxError("TRANSPORT", f"无法建立 SSE 连接: {exc.reason}") from None
        except TimeoutError:
            raise InboxError("TIMEOUT", "SSE 连接超时") from None


def _error_from_body(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """Map an HTTP error to (code, message) without ever echoing a token."""
    default_code = f"HTTP_{exc.code}"
    try:
        raw = exc.read()
    except Exception:
        return default_code, f"InboxMosire 返回 HTTP {exc.code}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return default_code, f"InboxMosire 返回 HTTP {exc.code}"
    if isinstance(payload, dict):
        code = str(payload.get("code") or payload.get("error") or default_code)
        message = str(payload.get("message") or payload.get("detail") or f"HTTP {exc.code}")
        return safe.clean_text(code, 64) or default_code, safe.clean_text(message, 400)
    return default_code, f"InboxMosire 返回 HTTP {exc.code}"
