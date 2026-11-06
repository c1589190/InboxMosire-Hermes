#!/usr/bin/env bash
# 一键升级 iibot-hermes 插件（1.0.0 合并了 inbox-wake / minimal-system / persona-mode）。
#
# 两种升级路径的区别：
#   * `hermes plugins update iibot-hermes` —— 只有当初用 `hermes plugins install <repo>#...`
#     装过、宿主记下了 provenance 的插件才有效；
#   * 本脚本 —— 对 `cp -a` / `sync-plugins.sh` 装进来的插件同样有效：按 git ref 取一份干净
#     副本整目录替换，备份、清 __pycache__、语法自检、重启 gateway、打印版本与配置键。
#
#   ./scripts/upgrade.sh                     # 升级到默认 ref（v0.4.0）
#   ./scripts/upgrade.sh --ref v0.3.0        # 指定 tag / 分支 / 提交
#   ./scripts/upgrade.sh --latest            # 用远端最新的 v* tag
#   ./scripts/upgrade.sh --dry-run           # 只报告会做什么
#   ./scripts/upgrade.sh --no-restart        # 改完不重启 gateway
#   ./scripts/upgrade.sh --target /home/ops/.hermes
#   ./scripts/upgrade.sh --rollback          # 回到最近一次备份并重启
#
# 只动 $HERMES_HOME/plugins/<id>，不碰 config.yaml、plugin-data、token。
set -euo pipefail

REPO_SLUG="${REPO_SLUG:-git@github.com:c1589190/InboxMosire-Hermes.git}"
HERMES_BIN="${HERMES_BIN:-hermes}"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_IDS=(iibot-hermes)
# 1.0.0 合并前的目录：替换前必须删除，否则同进程里新旧插件会把工具/hook/命令各注册一遍。
LEGACY_PLUGIN_IDS=(inbox-wake minimal-system persona-mode)
DEFAULT_REF="${DEFAULT_REF:-v0.4.0}"

REF="$DEFAULT_REF"
DRY_RUN=0
RESTART=1
ROLLBACK=0
LATEST_TAG=0

log()  { printf '\033[36m›\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) shift; REF="${1:-}"; [ -n "$REF" ] || die "--ref 需要一个 git ref" ;;
    --latest) LATEST_TAG=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --no-restart) RESTART=0 ;;
    --rollback) ROLLBACK=1 ;;
    --target) shift; HERMES_HOME_DIR="${1:-}"; [ -n "$HERMES_HOME_DIR" ] || die "--target 需要目录" ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
  shift
done

PLUGINS_DIR="$HERMES_HOME_DIR/plugins"
BACKUP_DIR="$PLUGINS_DIR/.backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP_DIR=""

cleanup() { [ -n "$TMP_DIR" ] && rm -rf "$TMP_DIR"; }
trap cleanup EXIT

gateway_running() {
  pgrep -f "hermes_cli.main .*gateway run" >/dev/null 2>&1
}

restart_gateway() {
  [ "$RESTART" = "1" ] || { warn "已跳过重启（--no-restart）；插件只在 gateway 启动时被发现一次"; return; }
  command -v "$HERMES_BIN" >/dev/null 2>&1 || { warn "找不到 $HERMES_BIN，跳过重启"; return; }
  if ! gateway_running; then
    warn "gateway 没在跑，未执行重启"
    return
  fi
  log "重启 gateway"
  "$HERMES_BIN" gateway stop >/dev/null 2>&1 || warn "gateway stop 返回非零（可能已在停）"
  sleep 2
  "$HERMES_BIN" gateway start >/dev/null 2>&1 || warn "gateway start 失败，请手动执行：$HERMES_BIN gateway start"
  ok "gateway 已重启"
}

# ---- 回滚 ---------------------------------------------------------------
if [ "$ROLLBACK" = "1" ]; then
  [ -d "$BACKUP_DIR" ] || die "没有备份目录：$BACKUP_DIR"
  for id in "${PLUGIN_IDS[@]}"; do
    latest="$(ls -1d "$BACKUP_DIR/$id"-* 2>/dev/null | sort | tail -1 || true)"
    [ -n "$latest" ] || { warn "$id 没有备份，跳过"; continue; }
    log "回滚 $id ← $latest"
    if [ "$DRY_RUN" = "1" ]; then continue; fi
    rm -rf "$PLUGINS_DIR/$id"
    cp -a "$latest" "$PLUGINS_DIR/$id"
    ok "$id 已回滚到 $(basename "$latest")"
  done
  restart_gateway
  exit 0
fi

# ---- 取一份干净的源码 ---------------------------------------------------
command -v git >/dev/null 2>&1 || die "需要 git"

if git -C "$PWD" rev-parse --git-dir >/dev/null 2>&1 \
   && [ -d "$PWD/plugins/iibot-hermes" ]; then
  SRC_REPO="$PWD"
  log "使用当前 checkout：$SRC_REPO"
  git -C "$SRC_REPO" fetch --tags --quiet origin || warn "git fetch 失败，可能用本地已有对象继续"
else
  TMP_DIR="$(mktemp -d)"
  SRC_REPO="$TMP_DIR/repo"
  log "克隆 $REPO_SLUG → $SRC_REPO"
  git clone --quiet --no-checkout "$REPO_SLUG" "$SRC_REPO" || die "克隆失败"
fi

if [ "$LATEST_TAG" = "1" ]; then
  REF="$(git -C "$SRC_REPO" tag -l 'v*' --sort=-v:refname | head -1)"
  [ -n "$REF" ] || die "远端没有 v* tag"
  log "最新 tag：$REF"
fi

git -C "$SRC_REPO" rev-parse --verify --quiet "$REF^{commit}" >/dev/null \
  || die "找不到 ref：$REF"
log "目标版本：$REF（$(git -C "$SRC_REPO" rev-parse --short "$REF^{commit}")）"

mkdir -p "$PLUGINS_DIR"
[ "$DRY_RUN" = "1" ] || mkdir -p "$BACKUP_DIR"

# ---- 清掉 0.x 的三个旧目录 --------------------------------------------------
for id in "${LEGACY_PLUGIN_IDS[@]}"; do
  [ -e "$PLUGINS_DIR/$id" ] || continue
  log "发现旧插件目录 $PLUGINS_DIR/$id（已合并进 iibot-hermes）"
  if [ "$DRY_RUN" = "1" ]; then
    log "dry-run：会先备份再删除 $id"
    continue
  fi
  cp -a "$PLUGINS_DIR/$id" "$BACKUP_DIR/$id-$STAMP"
  # 先停用再删目录：host 是 opt-in，旧 id 留在 plugins.enabled 里会一直报 unknown plugin
  if command -v "$HERMES_BIN" >/dev/null 2>&1; then
    "$HERMES_BIN" plugins disable "$id" >/dev/null 2>&1 && ok "已停用 $id" || true
  fi
  rm -rf "$PLUGINS_DIR/$id"
  ok "已备份并删除 $id（旧目录留着会同进程双重注册）"
done

for id in "${PLUGIN_IDS[@]}"; do
  src_rel="plugins/$id"
  git -C "$SRC_REPO" cat-file -e "$REF:$src_rel/plugin.yaml" 2>/dev/null \
    || { warn "$REF 里没有 $src_rel，跳过"; continue; }
  new_version="$(git -C "$SRC_REPO" show "$REF:$src_rel/plugin.yaml" | awk -F'"' '/^version:/{print $2; exit}')"
  cur_version="—"
  if [ -f "$PLUGINS_DIR/$id/plugin.yaml" ]; then
    cur_version="$(awk -F'"' '/^version:/{print $2; exit}' "$PLUGINS_DIR/$id/plugin.yaml")"
  fi
  log "$id：$cur_version → ${new_version:-?}"

  if [ "$DRY_RUN" = "1" ]; then continue; fi

  # 整目录替换：先备份，再从 git 对象取，不经过工作区（避免带上未提交改动）
  if [ -d "$PLUGINS_DIR/$id" ]; then
    cp -a "$PLUGINS_DIR/$id" "$BACKUP_DIR/$id-$STAMP"
    ok "已备份 → $(basename "$BACKUP_DIR/$id-$STAMP")"
  fi
  rm -rf "$PLUGINS_DIR/$id"
  mkdir -p "$PLUGINS_DIR/$id"
  git -C "$SRC_REPO" archive "$REF" "$src_rel" | tar -x -C "$PLUGINS_DIR/$id" --strip-components=2
  find "$PLUGINS_DIR/$id" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

  if ! python3 -m compileall -q "$PLUGINS_DIR/$id" >/dev/null 2>&1; then
    die "$id 语法自检失败；回滚：$0 --rollback"
  fi
  find "$PLUGINS_DIR/$id" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  ok "$id 已更新到 ${new_version:-$REF}"
done

if [ "$DRY_RUN" = "1" ]; then
  log "dry-run 结束，未改动任何文件"
  exit 0
fi

# ---- 启用新插件（host 是 opt-in：只装目录不 enable 等于没装）----------------
if command -v "$HERMES_BIN" >/dev/null 2>&1; then
  if "$HERMES_BIN" plugins enable iibot-hermes >/dev/null 2>&1; then
    ok "已启用 iibot-hermes"
  else
    warn "自动启用失败（首次启用会询问权限）。请手动执行：hermes plugins enable iibot-hermes"
  fi
else
  warn "PATH 里没有 $HERMES_BIN，跳过启用；请手动：hermes plugins enable iibot-hermes"
fi

# ---- 配置键自检（只补缺，不覆盖用户已设的值）-----------------------------
if command -v "$HERMES_BIN" >/dev/null 2>&1; then
  ensure_cfg() {
    key="$1"; value="$2"
    if "$HERMES_BIN" config get "$key" >/dev/null 2>&1; then
      return 0
    fi
    "$HERMES_BIN" config set --force "$key" "$value" >/dev/null 2>&1 \
      && ok "补配置 $key = $value" || warn "配置写入失败：$key"
  }
  base="plugins.entries.iibot-hermes.settings"
  ensure_cfg "$base.inbox.pushMode" "auto"
  ensure_cfg "$base.inbox.pushGraceMs" "2000"
  ensure_cfg "$base.inbox.firstRunGuide" "true"
  # 一级 entry 键（不在 settings 里）：宿主只读本插件自己的 entry，旧位置的值必须搬过来
  legacy_gw="$("$HERMES_BIN" config get "plugins.entries.inbox-wake.allow_gateway_injection" 2>/dev/null | tr -d '[:space:]')"
  if [ "$legacy_gw" = "true" ]; then
    ensure_cfg "plugins.entries.iibot-hermes.allow_gateway_injection" "true"
  fi
  # 旧命名空间仍能被只读回落读到，但新段迟早要写；提示而不是代写（值可能被有意保留）
  for legacy_id in "${LEGACY_PLUGIN_IDS[@]}"; do
    if "$HERMES_BIN" config get "plugins.entries.$legacy_id.settings" >/dev/null 2>&1; then
      warn "旧设置 plugins.entries.$legacy_id.settings 仍存在：插件只读回落（会打 WARNING）。"
      warn "  请把需要的值写到 plugins.entries.iibot-hermes.settings.* 后删除旧段（见 docs/INSTALL.md）。"
    fi
  done
  if "$HERMES_BIN" config get "plugins.entries.iibot-hermes.settings.persona.enabled" 2>/dev/null | grep -qi true; then
    warn "persona.enabled=true：按 D-009 人设注入默认关闭；确认这是你要的。"
  fi
fi

restart_gateway

# ---- 收尾 ---------------------------------------------------------------
echo
ok "升级完成。核对："
printf '  版本    : %s\n' "$(awk -F'"' '/^version:/{print $2; exit}' "$PLUGINS_DIR/iibot-hermes/plugin.yaml" 2>/dev/null || echo '?')"
printf '  插件目录: %s\n' "$PLUGINS_DIR/iibot-hermes"
printf '  备份目录: %s\n' "$BACKUP_DIR"
cat <<'TIP'
  日志里应看到 push.ok / push.ack / guide.sent，且不应再出现 wake.ok（那是 changed 路径）。
  回滚：./scripts/upgrade.sh --rollback
TIP

cat <<'NEXT'
  设置仍读旧命名空间（只读回落，会打 WARNING）。要彻底搬干净，逐条执行下面这些
  （值取自你原来的 plugins.entries.inbox-wake.settings，按需改）：

    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.enabled true
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.apiUrl http://127.0.0.1:46500
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.inboxId iibot-bot
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.adapterTokenFile file:/home/cna/DevMosire-iiBot/bot2/data/inbox/dsh-wake.token
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.pushMode on
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.wakeMode view-ack
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.wakeProfile iibot
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.autoBind true
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.debounceMs 1500
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.pollSeconds 60
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.sseReadTimeoutSeconds 600
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.wakeContentMaxChars 30000
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.wakeContentTotalChars 60000
    hermes config set --force plugins.entries.iibot-hermes.settings.inbox.summaryMaxItems 20
    hermes config set --force plugins.entries.iibot-hermes.allow_gateway_injection true

  然后删掉旧段并重启：
    hermes config unset plugins.entries.inbox-wake
    hermes config unset plugins.entries.persona-mode
    hermes gateway stop && sleep 2 && hermes gateway start
NEXT
