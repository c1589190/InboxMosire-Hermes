#!/usr/bin/env bash
# kit.sh — 一个目录把 Hermes 挂到 InboxMosire 上：起服务、发 token、挂 MCP、写插件设置、自检。
# 用法：./kit.sh [doctor|up|status|down|probe|token wake|program]
# 红线：任何输出都不打印 token 明文；token 落盘一律 0600。
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$KIT_DIR/run"
mkdir -p "$RUN_DIR"

# ---- 默认值（kit.env 可覆盖）----
JARS_DIR="${JARS_DIR:-$KIT_DIR/jars}"
INBOX_JAR="${INBOX_JAR:-}"
INBOX_DATA="${INBOX_DATA:-$RUN_DIR/inbox-data}"
INBOX_API="${INBOX_API:-http://127.0.0.1:18765}"
INBOX_MCP="${INBOX_MCP:-http://127.0.0.1:18766/mcp}"
INBOX_ID="${INBOX_ID:-main}"
WAKE_TOKEN_FILE="${WAKE_TOKEN_FILE:-$HOME/.hermes/inbox-mosire/api-token}"
PROGRAM_TOKEN_FILE="${PROGRAM_TOKEN_FILE:-$HOME/.hermes/inbox-mosire/program-token}"
IIBOT_JAR="${IIBOT_JAR:-}"
IIBOT_MCP="${IIBOT_MCP:-http://127.0.0.1:8765/mcp/bot}"
IIBOT_TOKEN_FILE="${IIBOT_TOKEN_FILE:-$HOME/.hermes/inbox-mosire/iibot-token}"
IIBOT_ENV_DIR="${IIBOT_ENV_DIR:-$HOME/.config/iibot}"
AUTO_BIND="${AUTO_BIND:-1}"
WAKE_MODE="${WAKE_MODE:-claim}"
WAKE_PROFILE="${WAKE_PROFILE:-generic}"
CHANNELS="${CHANNELS:-}"
RESTART_GATEWAY="${RESTART_GATEWAY:-auto}"
PLUGIN=iibot-hermes
INBOX_SETTINGS="plugins.entries.$PLUGIN.settings.inbox"

# shellcheck disable=SC1091
[ -f "$KIT_DIR/kit.env" ] && . "$KIT_DIR/kit.env"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
need() { command -v "$1" >/dev/null 2>&1; }

JAVA="${JAVA:-java}"

find_inbox_jar() {
  if [ -n "$INBOX_JAR" ] && [ -f "$INBOX_JAR" ]; then echo "$INBOX_JAR"; return 0; fi
  local found
  found="$(ls -1 "$JARS_DIR"/*.jar 2>/dev/null | grep -E 'inbox.*-all\.jar$' | head -1 || true)"
  [ -n "$found" ] && { echo "$found"; return 0; }
  # 从仓库里直接跑时，给一个就近的回退
  for cand in "$KIT_DIR/../../InboxMosire/inbox-server/target/"*inbox-server*-all.jar; do
    [ -f "$cand" ] && { echo "$cand"; return 0; }
  done
  return 1
}

http_ok() { curl -fsS --max-time 3 -o /dev/null "$1" 2>/dev/null; }
tcp_ok()  { (exec 3<>"/dev/tcp/127.0.0.1/${2}") 2>/dev/null && exec 3>&- && return 0 || return 1; }

wait_http() { # url timeout_s
  local url="$1" deadline=$(( $(date +%s) + ${2:-30} ))
  while [ "$(date +%s)" -lt "$deadline" ]; do http_ok "$url" && return 0; sleep 1; done
  return 1
}

token_state() { # file → ok|missing|perms
  local f="$1"
  [ -s "$f" ] || { echo missing; return; }
  local mode; mode="$(stat -c '%a' "$f" 2>/dev/null || echo '')"
  [ "$mode" = "600" ] && echo ok || echo perms
}

issue_token() { # target label scope...
  local target="$1" label="$2"; shift 2
  local jar; jar="$(find_inbox_jar)" || { warn "找不到 InboxMosire jar，无法签发 token"; return 1; }
  local out
  out="$("$JAVA" -jar "$jar" token issue --data-dir "$INBOX_DATA" --label "$label" "$@" 2>&1)" || {
    warn "token issue 失败：$(printf '%s' "$out" | tail -2)"; return 1; }
  local tok
  tok="$(printf '%s\n' "$out" | sed -n 's/^token=//p' | head -1)"
  [ -n "$tok" ] || { warn "token issue 输出里没有 token= 行"; return 1; }
  mkdir -p "$(dirname "$target")"
  ( umask 077; printf '%s\n' "$tok" > "$target" )
  chmod 600 "$target"
  printf '%s\n' "$out" | sed -n 's/^credential_id=/    credential_id=/p'
  unset tok out
}

ensure_token() { # target label scope...
  local target="$1"
  case "$(token_state "$target")" in
    ok)    ok "token 已存在且 0600：$target"; return 0;;
    perms) bad "token 文件权限不是 0600（$target）；chmod 600 后重跑，或删掉让它重新签发"; return 1;;
  esac
  say "  · 签发 token → $target"
  issue_token "$@"
}

read_token() { tr -d '\r\n' < "$1"; }

# ---------------- doctor ----------------
cmd_doctor() {
  local problems=0
  say "InboxMosire × Hermes kit · 自检"
  need "$JAVA" && ok "java：$("$JAVA" -version 2>&1 | head -1)" || { bad "没有 java"; problems=$((problems+1)); }
  local jar; if jar="$(find_inbox_jar)"; then ok "jar：$jar"; else bad "找不到 InboxMosire jar（放 $JARS_DIR/ 或设 INBOX_JAR）"; problems=$((problems+1)); fi
  need hermes && ok "hermes：$(hermes --version 2>/dev/null | head -1)" || { bad "没有 hermes CLI"; problems=$((problems+1)); }
  if http_ok "$INBOX_API/healthz"; then ok "InboxMosire API 在跑：$INBOX_API"; else warn "InboxMosire API 没在跑：$INBOX_API（up 会拉起）"; fi
  if tcp_ok 127 18766; then ok "InboxMosire MCP 端口 18766 已监听"; else warn "InboxMosire MCP 端口 18766 未监听（up 会拉起）"; fi
  for pair in "wake:$WAKE_TOKEN_FILE" "program:$PROGRAM_TOKEN_FILE"; do
    local name="${pair%%:*}" file="${pair#*:}" st; st="$(token_state "$file")"
    case "$st" in
      ok) ok "$name token 0600：$file";;
      missing) warn "$name token 不存在（up 会签发）：$file";;
      *) bad "$name token 权限过宽：$file（需 0600）"; problems=$((problems+1));;
    esac
  done
  if hermes plugins list --plain 2>/dev/null | grep -qE "enabled.*$PLUGIN"; then ok "插件 $PLUGIN 已 enabled"; else bad "插件 $PLUGIN 未启用（./scripts/install.sh --local .）"; problems=$((problems+1)); fi
  if hermes gateway status 2>/dev/null | grep -q "running"; then ok "gateway 在跑"; else warn "gateway 没在跑（up 会拉起）"; fi
  local mcp; mcp="$(hermes config get mcp_servers 2>/dev/null || true)"
  if printf '%s' "$mcp" | grep -q "^inbox:"; then ok "mcp_servers.inbox 已挂载"; else warn "mcp_servers.inbox 未挂载（up 会写）"; fi
  say ""
  [ "$problems" -eq 0 ] && say "结论：没有硬性问题，可以 ./kit.sh up" || say "结论：有 $problems 个硬性问题，先修"
  return 0
}

# ---------------- up ----------------
cmd_up() {
  say "① InboxMosire 服务"
  local jar=""; jar="$(find_inbox_jar)" || { bad "找不到 InboxMosire jar"; exit 1; }
  if http_ok "$INBOX_API/healthz"; then
    ok "API 已在跑：$INBOX_API"
  else
    [ -f "$INBOX_DATA/inbox.db" ] || { say "  · 初始化数据目录 $INBOX_DATA"; "$JAVA" -jar "$jar" init --data-dir "$INBOX_DATA" >/dev/null; }
    say "  · 起服务（日志 $RUN_DIR/inbox.log）"
    nohup "$JAVA" -jar "$jar" serve --data-dir "$INBOX_DATA" >"$RUN_DIR/inbox.log" 2>&1 &
    echo $! > "$RUN_DIR/inbox.pid"
    wait_http "$INBOX_API/healthz" 45 && ok "API 就绪：$INBOX_API" || { bad "服务没起来，看 $RUN_DIR/inbox.log"; exit 1; }
  fi
  tcp_ok 127 18766 && ok "MCP 端口 18766 已监听" || warn "MCP 端口 18766 未监听；inbox_* 工具会不可用"

  say "② token（0600）"
  ensure_token "$WAKE_TOKEN_FILE" "hermes-wake" --scope status:read --scope items:read --scope events:read --scope inbox:occupy --inbox "$INBOX_ID"
  ensure_token "$PROGRAM_TOKEN_FILE" "hermes-program" --scope status:read --scope items:read --scope items:manage --inbox "$INBOX_ID"

  say "③ 挂 MCP"
  mount_mcp inbox "$INBOX_MCP" "$PROGRAM_TOKEN_FILE"
  if [ -n "$IIBOT_JAR" ] || [ -n "${IIBOT_MCP_OPTIONAL:-}" ]; then
    [ -s "$IIBOT_TOKEN_FILE" ] || issue_iibot_token || true
    [ -s "$IIBOT_TOKEN_FILE" ] && mount_mcp iibot "$IIBOT_MCP" "$IIBOT_TOKEN_FILE" || warn "跳过 iibot MCP（没有 token）"
  fi

  say "④ 插件设置"
  local token_ref="file:$WAKE_TOKEN_FILE"
  hermes config set "$INBOX_SETTINGS.enabled" true >/dev/null
  hermes config set "$INBOX_SETTINGS.apiUrl" "$INBOX_API" >/dev/null
  hermes config set "$INBOX_SETTINGS.inboxId" "$INBOX_ID" >/dev/null
  hermes config set "$INBOX_SETTINGS.adapterTokenFile" "$token_ref" >/dev/null
  hermes config set "$INBOX_SETTINGS.wakeMode" "$WAKE_MODE" >/dev/null
  hermes config set "$INBOX_SETTINGS.wakeProfile" "$WAKE_PROFILE" >/dev/null
  hermes config set "$INBOX_SETTINGS.autoBind" "$([ "$AUTO_BIND" = "1" ] && echo true || echo false)" >/dev/null
  [ -n "$CHANNELS" ] && hermes config set "$INBOX_SETTINGS.channels" "[$CHANNELS]" >/dev/null
  # 一级 entry 键（不在 settings 里）；旧位置 plugins.entries.inbox-wake.allow_gateway_injection 已失效
  hermes config set "plugins.entries.$PLUGIN.allow_gateway_injection" true >/dev/null
  ok "apiUrl=$INBOX_API inbox=$INBOX_ID autoBind=$AUTO_BIND token=$token_ref"

  say "⑤ gateway"
  case "$RESTART_GATEWAY" in
    never) warn "RESTART_GATEWAY=never：设置变了也不会生效，记得手动 hermes gateway restart";;
    *)
      if hermes gateway status 2>/dev/null | grep -q "✓ Gateway is running"; then
        say "  · 停旧 gateway（有活跃会话时会等它们收尾）"
        timeout 120 hermes gateway stop >/dev/null 2>&1 || true
        local stop_deadline=$(( $(date +%s) + 30 ))
        while [ "$(date +%s)" -lt "$stop_deadline" ]; do
          hermes gateway status 2>/dev/null | grep -q "✓ Gateway is running" || break
          sleep 2
        done
        if hermes gateway status 2>/dev/null | grep -q "✓ Gateway is running"; then
          bad "旧 gateway 停不下来；换成 hermes -p default gateway restart 或手动重试"
          exit 1
        fi
      fi
      nohup hermes gateway run >"${HERMES_GATEWAY_LOG:-$HOME/.hermes/logs/gateway.log}" 2>&1 &
      local deadline=$(( $(date +%s) + 150 ))
      while [ "$(date +%s)" -lt "$deadline" ]; do
        hermes gateway status 2>/dev/null | grep -q "✓ Gateway is running" && break
        sleep 2
      done
      hermes gateway status 2>/dev/null | grep -q "running" && ok "gateway 已重启" || bad "gateway 没起来（看 ${HERMES_GATEWAY_LOG:-$HOME/.hermes/logs/gateway.log}）"
      ;;
  esac

  say ""
  say "完成。现在打开 hermes 直接说话就行：新会话首轮会自动绑定 inbox=$INBOX_ID（autoBind）。"
  say "自检：./kit.sh status ｜ 会话内 /inbox status、/inbox log"
}

mount_mcp() { # name url token_file
  local name="$1" url="$2" tokfile="$3"
  if [ ! -s "$tokfile" ]; then warn "没有 $name 的 token（$tokfile），跳过"; return 1; fi
  local tok; tok="$(read_token "$tokfile")"
  hermes config set "mcp_servers.$name.url" "$url" >/dev/null
  hermes config set "mcp_servers.$name.transport" streamable-http >/dev/null
  hermes config set "mcp_servers.$name.headers.Authorization" "Bearer $tok" >/dev/null
  mkdir -p "$(dirname "$tokfile")"; chmod 600 "$tokfile"
  unset tok
  ok "mcp_servers.$name → $url（Bearer 来自 $tokfile）"
}

issue_iibot_token() { # 从 bot 的 env 里取 IIBOT_CONTROL_TOKEN 落盘
  local src="$IIBOT_ENV_DIR/control.env"
  [ -f "$src" ] || { warn "没有 $src"; return 1; }
  local tok; tok="$(sed -n 's/^IIBOT_CONTROL_TOKEN=//p' "$src" | tr -d '\"'"'"'\r' | head -1)"
  [ -n "$tok" ] || { warn "$src 里没有 IIBOT_CONTROL_TOKEN"; return 1; }
  mkdir -p "$(dirname "$IIBOT_TOKEN_FILE")"
  ( umask 077; printf '%s\n' "$tok" > "$IIBOT_TOKEN_FILE" ); chmod 600 "$IIBOT_TOKEN_FILE"
  unset tok; ok "iibot token 落盘 0600：$IIBOT_TOKEN_FILE"
}

# ---------------- status / down / probe ----------------
cmd_status() {
  say "服务"
  http_ok "$INBOX_API/healthz" && ok "API $INBOX_API" || bad "API $INBOX_API 不通"
  tcp_ok 127 18766 && ok "MCP 18766" || bad "MCP 18766 不通"
  say "token 权限（不显示内容）"
  for pair in "wake:$WAKE_TOKEN_FILE" "program:$PROGRAM_TOKEN_FILE" "iibot:$IIBOT_TOKEN_FILE"; do
    local file="${pair#*:}" st; st="$(token_state "$file")"
    [ "$st" = ok ] && ok "${pair%%:*} 0600" || bad "${pair%%:*} $st（$file）"
  done
  say "Hermes"
  hermes gateway status 2>/dev/null | head -1 | sed 's/^/  /'
  hermes plugins list --plain 2>/dev/null | grep -E "$PLUGIN" | sed 's/^/  /' || true
  hermes config get "$INBOX_SETTINGS" 2>/dev/null | sed 's/^/  /' || true
  say "MCP"
  hermes config get mcp_servers 2>/dev/null | grep -E "^[a-zA-Z0-9_-]+:" | sed 's/^/  /' || say "  （无）"
}

cmd_down() {
  if [ -f "$RUN_DIR/inbox.pid" ] && kill -0 "$(cat "$RUN_DIR/inbox.pid")" 2>/dev/null; then
    kill "$(cat "$RUN_DIR/inbox.pid")" && ok "已停 InboxMosire（pid $(cat "$RUN_DIR/inbox.pid")）"
    rm -f "$RUN_DIR/inbox.pid"
  else
    warn "没有 kit 起的 InboxMosire 在跑"
  fi
}

cmd_probe() {
  say "跑一次一次性会话验证 autoBind（会留下一条绑定，可 /inbox unbind 清掉）…"
  hermes -z "调用 inbox_adapter_status，把返回的 JSON 原样输出，不要解释。" 2>&1 | tail -5
}

case "${1:-status}" in
  doctor) cmd_doctor;;
  up)     cmd_up;;
  status) cmd_status;;
  down)   cmd_down;;
  probe)  cmd_probe;;
  token)  shift; case "${1:-wake}" in
            wake)    issue_token "$WAKE_TOKEN_FILE" "hermes-wake" --scope status:read --scope items:read --scope events:read --scope inbox:occupy --inbox "$INBOX_ID";;
            program) issue_token "$PROGRAM_TOKEN_FILE" "hermes-program" --scope status:read --scope items:read --scope items:manage --inbox "$INBOX_ID";;
            *) say "用法：./kit.sh token [wake|program]"; exit 2;;
          esac;;
  *) say "用法：./kit.sh [doctor|up|status|down|probe|token wake|program]"; exit 2;;
esac
