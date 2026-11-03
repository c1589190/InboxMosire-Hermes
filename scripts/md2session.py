#!/usr/bin/env python3
"""把 `hermes sessions export --format md` 产出的 Markdown 会话转成 Claude Code JSONL，
再用 `hermes sessions import --from claude <文件>` 灌回另一台机器的会话库。

Hermes 官方的 `sessions import` 只认 Claude Code / Codex 两种 JSONL，markdown 是单向的；
本脚本补上那个缺口：只搬对话正文（User / Assistant），工具调用与附件默认丢弃。

用法：
    python3 md2session.py session.md                 # 生成 <session>.jsonl（同目录）
    python3 md2session.py session.md -o /tmp/x.jsonl
    python3 md2session.py exports/                   # 目录：逐个转
    python3 md2session.py session.md --keep-tools    # 工具输出并入紧随其后的 assistant 文本
    python3 md2session.py session.md --dry-run       # 只报告解析到多少条，不写文件

导入（在目标机器）：
    hermes sessions import --from claude /tmp/x.jsonl
    hermes --resume <imported session id>      # 导入时会打印

红线：导出的 md / 生成的 jsonl 都是**明文对话**，可能含密钥；落到磁盘后 chmod 600，别提交。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable

HEADING_RE = re.compile(r"^###\s+(?P<role>[A-Za-z_]+)(?:\s+—\s+(?P<ts>[0-9T:.+\-Z]+))?\s*$")
TOOL_HEADING_RE = re.compile(r"^###\s+Tool\s+—\s+(?P<name>.+?)\s+—\s+(?P<ts>[0-9T:.+\-Z]+)\s*$")
SECTION_RE = re.compile(r"^##\s+")
FRONT_RE = re.compile(r'^(?P<key>[A-Za-z_]+):\s*"?(?P<value>.*?)"?\s*$')

SKIP_ROLES = {"session_meta", "system", "tool"}


def read_front_matter(lines: list[str]) -> tuple[dict[str, str], int]:
    """Return (meta, index_of_first_body_line). Front matter is optional."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, i + 1
        m = FRONT_RE.match(line)
        if m:
            meta[m.group("key")] = m.group("value")
    return meta, 0  # 没闭合，当作没有 front matter


def split_blocks(lines: list[str]) -> Iterable[tuple[str, str | None, str, str]]:
    """Yield (role, name_or_None, timestamp, body) for every `### ...` block."""
    role = name = ts = None
    body: list[str] = []
    for line in lines:
        tool = TOOL_HEADING_RE.match(line)
        head = tool or HEADING_RE.match(line)
        if head:
            if role is not None:
                yield role, name, ts or "", "\n".join(body).strip()
            if tool:
                role, name, ts, body = "tool", tool.group("name"), tool.group("ts"), []
            else:
                role, name, ts, body = head.group("role"), None, head.group("ts"), []
            continue
        if SECTION_RE.match(line) and role is not None:
            continue  # `## Tool calls` 之类的段落标题，属于当前 block 的边界标记
        body.append(line)
    if role is not None:
        yield role, name, ts or "", "\n".join(body).strip()


def convert(path: Path, *, keep_tools: bool) -> tuple[list[dict[str, Any]], dict[str, str]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    meta, start = read_front_matter(lines)
    records: list[dict[str, Any]] = []
    parent: str | None = None
    pending_tool: str | None = None

    for role, name, ts, body in split_blocks(lines[start:]):
        low = role.lower()
        if low == "tool":
            pending_tool = f"[tool:{name}]\n{body}" if keep_tools and body else None
            continue
        if low in SKIP_ROLES or low not in ("user", "assistant"):
            continue
        text = f"{pending_tool}\n\n{body}" if (pending_tool and low == "assistant") else body
        if low == "user":
            pending_tool = None
        if not text.strip():
            continue
        uid = str(uuid.uuid4())
        records.append({
            "type": low,
            "uuid": uid,
            "parentUuid": parent,
            "sessionId": meta.get("session_id", ""),
            "timestamp": ts,
            "cwd": None,
            "gitBranch": "",
            "version": "md2session/1",
            "userType": "external",
            "isSidechain": False,
            "message": {"role": low, "content": [{"type": "text", "text": text}]},
        })
        parent = uid

    title = meta.get("title") or path.stem
    if records:
        records.insert(0, {"type": "summary", "summary": title})
    return records, meta


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Hermes md 会话导出 → Claude Code JSONL（供 sessions import 使用）")
    ap.add_argument("input", help="md 文件或包含 md 的目录")
    ap.add_argument("-o", "--output", help="输出 jsonl 路径（默认与输入同名，扩展名换成 .jsonl）")
    ap.add_argument("--keep-tools", action="store_true", help="把工具输出并入紧随其后的 assistant 文本")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = ap.parse_args(argv)

    src = Path(args.input).expanduser()
    files = sorted(src.glob("*.md")) if src.is_dir() else [src]
    if not files:
        print(f"没有找到 md 文件：{src}", file=sys.stderr)
        return 2

    rc = 0
    for f in files:
        records, meta = convert(f, keep_tools=args.keep_tools)
        turns = sum(1 for r in records if r["type"] in ("user", "assistant"))
        if turns == 0:
            print(f"✗ {f.name}: 没解析出对话（不是 Hermes md 导出？）", file=sys.stderr)
            rc = 1
            continue
        out = Path(args.output).expanduser() if args.output else f.with_suffix(".jsonl")
        if args.dry_run:
            print(f"· {f.name}: {turns} 条对话（session_id={meta.get('session_id') or '未知'}）→ 将写入 {out}")
            continue
        out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        out.chmod(0o600)
        print(f"✓ {f.name}: {turns} 条对话 → {out}（0600）")
        print(f"  下一步：hermes sessions import --from claude {out}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
