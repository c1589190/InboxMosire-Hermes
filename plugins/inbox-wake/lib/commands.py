"""``/inbox`` slash command — the operator-side console for the wake adapter."""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

from . import oplog

HELP = """\
/inbox — InboxMosire wake adapter

  status               当前会话的绑定、SSE 健康、游标与最近一次唤醒
  bind <descriptor.json>          用 descriptor 文件绑定
  bind <inboxId> [channels]       快速绑定（token 走 settings.adapterTokenFile）
  unbind [sessionId]   解绑并释放 occupancy
  retry [sessionId]    清退避并重连
  log [limit]          最近适配器事件（默认 20 条）

业务操作（list/claim/complete/push/rest）在 InboxMosire MCP 里，不在本插件。
"""


def handle(raw_args: str, manager: Any, session_key: str) -> Optional[str]:
    argv = (raw_args or "").strip().split()
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return HELP
    sub = argv[0]
    target = session_key

    if sub == "status":
        result = manager.status(target)
        if not result.get("ok"):
            return f"未绑定：{result.get('message')}\n用 /inbox bind <inboxId> 绑定。"
        return _format_status(result["binding"], _inject_capability(manager))
    if sub == "bind":
        if len(argv) < 2:
            return "用法：/inbox bind <descriptor.json | inboxId> [channels]"
        arg = argv[1]
        if arg.endswith(".json"):
            path = os.path.expanduser(arg)
            try:
                with open(path, "r", encoding="utf-8") as handle_file:
                    descriptor = json.load(handle_file)
            except OSError as exc:
                return f"读不了 descriptor：{exc.strerror}"
            except json.JSONDecodeError as exc:
                return f"descriptor 不是合法 JSON：{exc}"
            result = manager.bind(target, {"descriptor": descriptor})
        else:
            channels = []
            if len(argv) > 2:
                channels = [part for part in ",".join(argv[2:]).split(",") if part.strip()]
            result = manager.bind(target, {"inboxId": arg, "channels": channels})
        if not result.get("ok"):
            return f"绑定失败：{result.get('code')} {result.get('message')}"
        return "已绑定\n" + _format_status(result["binding"], _inject_capability(manager))
    if sub == "unbind":
        target = argv[1] if len(argv) > 1 else session_key
        result = manager.unbind(target)
        return "已解绑" if result.get("ok") else f"解绑失败：{result.get('message')}"
    if sub == "retry":
        target = argv[1] if len(argv) > 1 else session_key
        result = manager.retry(target)
        return "已重连" if result.get("ok") else f"重连失败：{result.get('message')}"
    if sub == "log":
        try:
            limit = int(argv[1]) if len(argv) > 1 else 20
        except ValueError:
            limit = 20
        entries = oplog.read(manager.store, target, limit)
        if not entries:
            return "没有事件记录。"
        return "\n".join(oplog.format_line(entry) for entry in entries)
    return f"未知子命令：{sub}\n\n{HELP}"


def _inject_capability(manager: Any) -> str:
    """Which host runs this manager; "none" means a wake here can never be delivered."""
    probe = getattr(manager, "_current_inject_capability", None)
    if callable(probe):
        try:
            return str(probe())
        except Exception:
            pass
    return str(getattr(manager, "inject_capability", "unknown"))


def _format_status(record: dict[str, Any], inject_capability: str = "unknown") -> str:
    health = record.get("health") or {}
    status = record.get("lastStatus") or {}
    wake = record.get("lastWake") or {}
    summary = record.get("summary") or {}
    channels = record.get("channels") or []
    lines = [
        f"inbox      : {record.get('inboxId')}",
        f"api        : {record.get('apiBaseUrl')}",
        f"channels   : {','.join(channels) if channels else '(全部)'}",
        f"wakeMode   : {record.get('wakeMode')} / {record.get('wakeProfile')}",
        f"token      : {record.get('tokenRef')}",
        f"inject     : {inject_capability}",
        f"revision   : observed={record.get('lastObservedRevision')} woken={record.get('lastWokenRevision')}"
        f" available={status.get('available')}",
        f"sse        : {health.get('sse')} turn={health.get('turn')} reconnects={health.get('reconnects')}"
        f" programMcp={health.get('programMcp')}",
        f"lastEventId: {record.get('lastEventId') or '-'}",
        f"lastWake   : {wake.get('at') or '-'} rev={wake.get('revision')} ok={wake.get('ok')}"
        + (" deferred(等活跃会话)" if wake.get("deferred") else "")
        + (f" err={wake.get('error')}" if wake.get("error") else ""),
        f"summary    : {summary.get('state')} shown={summary.get('shown')} remaining={summary.get('remaining')}",
    ]
    if status.get("error"):
        lines.append(f"status.err : {status['error']}")
    return "\n".join(lines)
