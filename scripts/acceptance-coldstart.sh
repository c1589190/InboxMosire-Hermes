#!/usr/bin/env bash
# 冷启动自举的取证脚本（docs/COLDSTART.md §6 / docs/ACCEPTANCE.md §N）。
#
# 它只做**可观察事实**的采集，不做断言式「通过/不通过」：真正的 N1–N5 需要人看会话行为。
# 用法：
#   ./scripts/acceptance-coldstart.sh probe     # 端口 + InboxHTTP + MCP 端点（不启停任何东西）
#   ./scripts/acceptance-coldstart.sh stop      # 停 iiBot（N1 的前置）
#   ./scripts/acceptance-coldstart.sh start     # 起 iiBot（N3）
#   ./scripts/acceptance-coldstart.sh watch     # 盯 agent.log 里的 program-tools-missing / wake.ok
#   ./scripts/acceptance-coldstart.sh all       # probe → stop → probe → start → probe（全程不碰 Hermes）
#
# 环境变量：
#   BOT_DIR        默认 /home/cna/DevMosire-iiBot/bot
#   HERMES_LOG     默认 ~/.hermes/logs/agent.log
#   INBOX_TOKEN_FILE  默认 $BOT_DIR/data/inbox/dsh-wake.token
set -uo pipefail

BOT_DIR="${BOT_DIR:-/home/cna/DevMosire-iiBot/bot}"
HERMES_LOG="${HERMES_LOG:-$HOME/.hermes/logs/agent.log}"
TOKEN_FILE="${INBOX_TOKEN_FILE:-$BOT_DIR/data/inbox/dsh-wake.token}"

say() { printf '%s\n' "$*"; }
hr() { printf '%s\n' "------------------------------------------------------------"; }

port_open() { # 0 = 在听
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3<&-; exec 3>&-; return 0; }
  return 1
}

probe() {
  hr
  say "端口（iiBot 单进程三端口，见 docs/COLDSTART.md §2）"
  for p in 8765 46500 46501; do
    if port_open "$p"; then say "  127.0.0.1:$p  LISTEN"; else say "  127.0.0.1:$p  -"; fi
  done
  say "HTTP /v1/status（需要 0600 的 adapter token）"
  if [ -r "$TOKEN_FILE" ]; then
    code=$(curl -s -o /tmp/coldstart-status.json -w '%{http_code}' \
      -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
      --max-time 5 "http://127.0.0.1:46500/v1/status" 2>/dev/null)
    say "  GET /v1/status → ${code:-no-response}"
    [ -s /tmp/coldstart-status.json ] && head -c 300 /tmp/coldstart-status.json && say ""
  else
    say "  跳过：$TOKEN_FILE 不可读（N3 需要它）"
  fi
  say "MCP 端点（工具面，只握个手，不调用业务工具）"
  for url in "http://127.0.0.1:8765/mcp/bot" "http://127.0.0.1:46501/mcp"; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
      -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"coldstart-probe","version":"0"}}}' \
      "$url" 2>/dev/null)
    say "  $url → ${code:-no-response}"
  done
  hr
  say "下一步（人工）：在 Hermes 会话里看 N1/N4 ——"
  say "  N1 工具面无 qq_conversation_list；inbox_adapter_status 的 health.programMcp 应为 missing"
  say "  N4 起来后不发 /reload-mcp、不重启 gateway，≤1 个 housekeeping tick（60s）+ 1 个回合后应有 qq_*"
}

case "${1:-probe}" in
  probe) probe ;;
  stop)  "$BOT_DIR/start-bot.sh" stop ;;
  start) "$BOT_DIR/start-bot.sh" start ;;
  watch)
    [ -r "$HERMES_LOG" ] || { say "读不到 $HERMES_LOG"; exit 2; }
    say "跟踪 program-tools-missing / wake.ok / wake.skipped（Ctrl-C 结束）"
    tail -f "$HERMES_LOG" | grep --line-buffered -E "program-tools-missing|wake\.ok|wake\.skipped|inbox-wake: loaded|iibot-hermes: effective settings"
    ;;
  all)
    probe; say "停 iiBot…"; "$BOT_DIR/start-bot.sh" stop >/dev/null 2>&1; probe
    say "起 iiBot…"; "$BOT_DIR/start-bot.sh" start >/dev/null 2>&1; sleep 20; probe
    say "（N1/N4 的会话侧断言仍需人工，见 docs/ACCEPTANCE.md §N）"
    ;;
  *) say "用法：$0 [probe|stop|start|watch|all]"; exit 2 ;;
esac
