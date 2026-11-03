#!/usr/bin/env bash
# 给宿主 hermes-agent 打/撤「极简 system prompt」补丁（agent.system_prompt_override）。
#
#   ./scripts/patch-hermes.sh check    # 只检查：已打 / 可打 / 冲突
#   ./scripts/patch-hermes.sh apply    # 幂等应用
#   ./scripts/patch-hermes.sh revert   # 幂等撤销
#
# 宿主目录默认 ~/.hermes/hermes-agent，可用 HERMES_AGENT_DIR 覆盖。
# 宿主升级会重置该目录：升级后重跑 apply 即可（补丁以 patches/ 里的 format-patch 为准）。
set -euo pipefail

HOST="${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATCH="$ROOT/patches/0001-host-system-prompt-override.patch"
MARKER="system_prompt_override"

say() { printf '%s\n' "$*"; }

[ -d "$HOST" ] || { say "宿主目录不存在：$HOST"; exit 2; }
[ -f "$PATCH" ] || { say "补丁文件不存在：$PATCH"; exit 2; }

applied() { # 代码里已经有标记 = 已打（不依赖 git 状态，升级后仍然成立）
  grep -q "_system_prompt_override" "$HOST/agent/system_prompt.py" 2>/dev/null
}

case "${1:-check}" in
  check)
    if applied; then
      say "已应用：$HOST/agent/system_prompt.py 有 _system_prompt_override"
      exit 0
    fi
    if git -C "$HOST" apply --check "$PATCH" 2>/dev/null; then
      say "未应用，可直接打：$PATCH"
      exit 1
    fi
    say "未应用且打不上（宿主源码已变，需人工看 patches/README 与 docs/HOST-PATCH.md）"
    exit 3
    ;;
  apply)
    if applied; then say "已应用，跳过"; exit 0; fi
    git -C "$HOST" apply --check "$PATCH" || { say "打不上：宿主源码与该补丁不一致"; exit 3; }
    git -C "$HOST" apply "$PATCH"
    say "已应用。重启 gateway（或新开会话）后生效；验证：config.yaml 里 agent.system_prompt_override 已设"
    ;;
  revert)
    if ! applied; then say "未应用，跳过"; exit 0; fi
    git -C "$HOST" apply --reverse "$PATCH" || { say "反打失败：宿主源码已被改动，手工回退 agent/system_prompt.py 与 agent/agent_init.py"; exit 3; }
    say "已撤销"
    ;;
  *)
    say "用法：$0 [check|apply|revert]"; exit 2;;
esac
