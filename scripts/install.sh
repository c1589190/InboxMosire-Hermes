#!/usr/bin/env bash
# InboxMosire-Hermes 安装脚本。
#
# 必须在**你自己的终端、你自己的用户**下运行：Hermes 的 install/state 目录权限属于安装用户，
# 沙箱内的自动化无法代写 ~/.hermes。
#
#   ./scripts/install.sh                 # 全流程：装插件 → 写 token → 写设置 → 打印后续步骤
#   ./scripts/install.sh --local .       # 从当前 checkout 复制安装（尚未 push 到远端时用）
#   ./scripts/install.sh plugins         # 只装插件
#   ./scripts/install.sh token           # 只写 token 文件
#   ./scripts/install.sh settings        # 只写 config.yaml 设置
#   ./scripts/install.sh check           # 只做体检
#
set -euo pipefail
umask 077

REPO_SLUG="${REPO_SLUG:-git@github.com:c1589190/InboxMosire-Hermes.git}"
HERMES_BIN="${HERMES_BIN:-hermes}"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
INBOX_API_URL="${INBOX_API_URL:-http://127.0.0.1:18765}"
INBOX_ID="${INBOX_ID:-main}"
WAKE_MODE="${WAKE_MODE:-claim}"           # claim | view-ack
WAKE_PROFILE="${WAKE_PROFILE:-generic}"   # generic | iibot
PERSONA_FILE="${PERSONA_FILE:-}"
TOKEN_PATH="${TOKEN_PATH:-$HERMES_HOME_DIR/inbox-mosire/api-token}"
LOCAL_SRC=""
ACTION="${1:-all}"

log()  { printf '\033[36m›\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

need_hermes() {
  command -v "$HERMES_BIN" >/dev/null 2>&1 \
    || die "找不到 $HERMES_BIN；把 Hermes 的可执行文件放进 PATH，或用 HERMES_BIN=/path/to/hermes 指定"
  [ -d "$HERMES_HOME_DIR" ] || die "找不到 HERMES_HOME: $HERMES_HOME_DIR（用 HERMES_HOME=... 指定）"
}

wait_gateway_stopped() {
  if pgrep -f "hermes_cli.main .*gateway run" >/dev/null 2>&1; then
    warn "检测到 Hermes gateway 仍在运行：插件只在进程启动时被发现一次，改完设置后需要重启 gateway。"
  fi
}

# ---- 1. 安装两个插件 -------------------------------------------------------
do_plugins() {
  need_hermes
  if [ -n "$LOCAL_SRC" ]; then
    [ -d "$LOCAL_SRC/plugins/persona-mode" ] || die "本地路径里没有 plugins/persona-mode: $LOCAL_SRC"
    mkdir -p "$HERMES_HOME_DIR/plugins"
    for id in persona-mode inbox-wake; do
      log "复制 $id → $HERMES_HOME_DIR/plugins/$id"
      rm -rf "$HERMES_HOME_DIR/plugins/$id"
      cp -a "$LOCAL_SRC/plugins/$id" "$HERMES_HOME_DIR/plugins/$id"
      find "$HERMES_HOME_DIR/plugins/$id" -name '__pycache__' -type d -prune -exec rm -rf {} +
    done
    for id in persona-mode inbox-wake; do
      log "启用 $id"
      "$HERMES_BIN" plugins enable "$id" || warn "启用 $id 失败，稍后手动执行: $HERMES_BIN plugins enable $id"
    done
  else
    for id in persona-mode inbox-wake; do
      log "从 $REPO_SLUG#plugins/$id 安装并启用"
      "$HERMES_BIN" plugins install "$REPO_SLUG#plugins/$id" --enable \
        || die "安装 $id 失败。若被安装期安全扫描拦下，先看提示；确认无误后可加 --force 重试"
    done
  fi
  ok "插件已就位：$(ls -1 "$HERMES_HOME_DIR/plugins" 2>/dev/null | tr '\n' ' ')"
}

# ---- 2. token 文件 ---------------------------------------------------------
do_token() {
  mkdir -p "$(dirname "$TOKEN_PATH")"
  chmod 700 "$(dirname "$TOKEN_PATH")"
  if [ -s "$TOKEN_PATH" ]; then
    ok "token 文件已存在，跳过：$TOKEN_PATH"
    return
  fi
  if [ -n "${INBOX_TOKEN:-}" ]; then
    warn "从 INBOX_TOKEN 环境变量取 token（会留在 shell 历史里，随后 unset 掉更好）"
    TOKEN="$INBOX_TOKEN"
  elif [ -t 0 ]; then
    printf '输入 InboxMosire adapter token（输入不回显）: '
    read -r TOKEN
    printf '\n'
  else
    die "没有交互终端：先手动创建 $TOKEN_PATH（chmod 600），或用 INBOX_TOKEN=... 重跑"
  fi
  [ -n "${TOKEN:-}" ] || die "token 为空，已退出（没有写任何文件）"
  printf '%s' "$TOKEN" > "$TOKEN_PATH"
  chmod 600 "$TOKEN_PATH"
  unset TOKEN
  ok "已写入 $TOKEN_PATH（0600）"
}

# ---- 3. config.yaml 设置 ---------------------------------------------------
set_cfg() {
  "$HERMES_BIN" config set --force "$1" "$2" >/dev/null \
    || die "写配置失败: $1 = $2"
  ok "$1 = $2"
}

do_settings() {
  need_hermes
  log "写入 persona-mode 设置"
  set_cfg "plugins.entries.persona-mode.settings.enabled" "true"
  if [ -n "$PERSONA_FILE" ]; then
    set_cfg "plugins.entries.persona-mode.settings.personaFile" "$PERSONA_FILE"
  else
    warn "未提供 PERSONA_FILE；插件默认读当前会话工作目录下的 PERSONA.md。如需固定路径："
    warn "  $HERMES_BIN config set --force plugins.entries.persona-mode.settings.personaFile /abs/path/PERSONA.md"
  fi

  log "写入 inbox-wake 设置"
  set_cfg "plugins.entries.inbox-wake.settings.enabled" "true"
  set_cfg "plugins.entries.inbox-wake.settings.apiUrl" "$INBOX_API_URL"
  set_cfg "plugins.entries.inbox-wake.settings.inboxId" "$INBOX_ID"
  set_cfg "plugins.entries.inbox-wake.settings.adapterTokenFile" "file:$TOKEN_PATH"
  set_cfg "plugins.entries.inbox-wake.settings.wakeMode" "$WAKE_MODE"
  set_cfg "plugins.entries.inbox-wake.settings.wakeProfile" "$WAKE_PROFILE"
  # 关键开关：没有它，gateway 会话里的唤醒会被宿主拒绝（inject_message 返回 False）
  set_cfg "plugins.entries.inbox-wake.allow_gateway_injection" "true"
  ok "设置写入完成"
}

# ---- 4. 体检 ---------------------------------------------------------------
do_check() {
  need_hermes
  log "插件清单"; "$HERMES_BIN" plugins list --user --plain || true
  log "运行时契约校验（doctor）"
  for id in persona-mode inbox-wake; do
    "$HERMES_BIN" plugins doctor "$id" --ci || warn "$id doctor 报了问题，见上面的输出"
  done
  log "InboxMosire 连通性"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 2 "$INBOX_API_URL/healthz" >/dev/null \
      && ok "$INBOX_API_URL/healthz 可访问" \
      || warn "$INBOX_API_URL/healthz 不可访问：确认 InboxMosire daemon 已启动"
  fi
  wait_gateway_stopped
  cat <<'EOF'

后续两步（脚本不代跑）：
  1. 业务工具（list/claim/complete/push/rest）走 InboxMosire MCP，用下面的命令挂载；
     token 由 Hermes 交互式提示输入，不要写进命令行历史：
       hermes mcp add inbox-mosire --url http://127.0.0.1:18766/mcp --auth header
       hermes mcp test inbox-mosire
  2. 重启 gateway，让插件与设置生效：
       hermes gateway restart        # 或结束旧进程后 hermes gateway run

会话内可用命令：
  /inbox status | bind <descriptor.json|inboxId> [channels] | unbind | retry | log
工具：
  inbox_adapter_status / inbox_adapter_bind / inbox_adapter_unbind / inbox_adapter_log
EOF
}

# ---- 入口 ------------------------------------------------------------------
case "$ACTION" in
  all)      do_plugins; do_token; do_settings; do_check ;;
  plugins)  do_plugins ;;
  token)    do_token ;;
  settings) do_settings ;;
  check)    do_check ;;
  --local)  LOCAL_SRC="${2:-$PWD}"; do_plugins; do_token; do_settings; do_check ;;
  -h|--help)
    awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
    ;;
  *) die "未知动作: $ACTION（可用：all | plugins | token | settings | check | --local <path>）" ;;
esac
