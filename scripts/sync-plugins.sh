#!/usr/bin/env bash
# 把本仓库的插件同步到 HERMES_HOME/plugins，供「已装过旧版，要升级」的场景使用。
#
#   ./scripts/sync-plugins.sh --dry-run              # 只报告会改什么
#   ./scripts/sync-plugins.sh                        # 复制 persona-mode / inbox-wake / minimal-system
#   ./scripts/sync-plugins.sh --only inbox-wake      # 只同步一个
#   ./scripts/sync-plugins.sh --target /home/ops/.hermes
#   ./scripts/sync-plugins.sh --restart              # 复制后停+起 gateway（远程部署常用）
#
# 复制语义：整目录替换（先删后拷），并清掉 __pycache__，避免旧模块残留。
# 只动 plugins/<id>，不碰 settings、不碰 plugin-data、不碰 token。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_IDS=(persona-mode inbox-wake minimal-system)
ONLY=""
DRY_RUN=0
RESTART=0

log()  { printf '\033[36m›\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --restart) RESTART=1 ;;
    --only) shift; ONLY="${1:-}"; [ -n "$ONLY" ] || die "--only 需要插件 id" ;;
    --target) shift; HERMES_HOME_DIR="${1:-}"; [ -n "$HERMES_HOME_DIR" ] || die "--target 需要路径" ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "未知参数：$1（--dry-run / --only <id> / --target <dir> / --restart）" ;;
  esac
  shift
done

[ -d "$HERMES_HOME_DIR" ] || die "找不到 HERMES_HOME: $HERMES_HOME_DIR"
TARGET_DIR="$HERMES_HOME_DIR/plugins"
SRC_REV="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"

version_of() {  # $1 = plugin.yaml
  sed -n 's/^version:[[:space:]]*//p' "$1" 2>/dev/null | head -1 | tr -d '"'"'"
}

log "源仓库：$ROOT（$SRC_REV）→ 目标：$TARGET_DIR"
[ -d "$TARGET_DIR" ] || die "目标没有 plugins 目录：$TARGET_DIR（先跑 scripts/install.sh）"

for id in "${PLUGIN_IDS[@]}"; do
  [ -z "$ONLY" ] || [ "$ONLY" = "$id" ] || continue
  src="$ROOT/plugins/$id"
  dst="$TARGET_DIR/$id"
  [ -f "$src/plugin.yaml" ] || { warn "源里没有 $id，跳过"; continue; }
  new_v="$(version_of "$src/plugin.yaml")"
  if [ -f "$dst/plugin.yaml" ]; then
    old_v="$(version_of "$dst/plugin.yaml")"
    log "$id  ${old_v:-?} (已装) → ${new_v:-?} (源)"
  else
    log "$id  (未安装) → ${new_v:-?}"
  fi
  if [ "$DRY_RUN" = "1" ]; then
    diff -rq --exclude=__pycache__ "$dst" "$src" 2>/dev/null | sed 's/^/    /' || true
    continue
  fi
  rm -rf "$dst"
  cp -a "$src" "$dst"
  find "$dst" -name '__pycache__' -type d -prune -exec rm -rf {} +
  ok "$id 已同步到 ${new_v:-?}"
done

if [ "$DRY_RUN" = "1" ]; then
  ok "dry-run 结束，未改动任何文件"
  exit 0
fi

if pgrep -f "hermes_cli.main .*gateway run" >/dev/null 2>&1; then
  warn "检测到 gateway 在运行：插件代码与 hook 只在 register(ctx) 时生效，"
  warn "需要重启 gateway（或 hermes plugins enable <id> 触发 reload-plugins；"
  warn "工具与 system_prompt_section 仍要等下一个 session）。"
else
  log "gateway 未运行，下次启动即生效"
fi

if [ "$RESTART" = "1" ]; then
  log "重启 gateway …"
  hermes gateway stop >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    hermes gateway status 2>/dev/null | grep -q "Gateway is running" || break
    sleep 2
  done
  nohup hermes gateway run >> "$HERMES_HOME_DIR/logs/gateway.log" 2>&1 &
  for _ in $(seq 1 30); do
    hermes gateway status 2>/dev/null | grep -q "Gateway is running" && break
    sleep 2
  done
  hermes gateway status 2>/dev/null | tail -2 || warn "gateway 状态未知，看 $HERMES_HOME_DIR/logs/gateway.log"
fi

ok "完成。验证：hermes plugins doctor persona-mode / inbox-wake / minimal-system"
