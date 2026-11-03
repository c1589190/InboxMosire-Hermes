#!/usr/bin/env python3
"""看这条 Hermes route 到底能不能吃图，以及宿主会不会把图送进模型上下文。

两件事一起查，因为「Bot 说读不了图」有两个完全不同层次的原因：

1. **宿主层**：`agent/image_routing.py:_supports_vision_override()` 只认配置里的
   `model.supports_vision` / `providers.<p>.models.<m>.supports_vision`。目录（models.dev /
   provider profile）里查不到 `custom:<name>` 上的模型时，判定为 `text`，MCP `ImageContent`
   只留 `MEDIA:<path>` 文本 ⇒ 模型根本没见过图。
2. **中转/模型层**：route 本身拒图（400）或有你没设的 aux vision 后端。

用法：
    ./scripts/check-vision.py                 # 读 ~/.hermes/config.yaml + .env
    ./scripts/check-vision.py --image a.png   # 用真图探针（否则发一张 64x64 纯红）
退出码：0 = 两侧都过；1 = 宿主未声明视觉能力；2 = route 拒图/报错。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
PROMPT = "这张图是什么颜色？只回答颜色。"


def red_png(width: int = 64, height: int = 64) -> bytes:
    rows = b"".join(b"\x00" + bytes((200, 30, 30)) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def route_of(cfg: dict) -> tuple[str, str, str, str]:
    """(provider, model, api_base, key_env)"""
    model_cfg = cfg.get("model") or {}
    provider = str(model_cfg.get("provider") or "").strip()
    model = str(model_cfg.get("default") or model_cfg.get("name") or "").strip()
    name = provider[len("custom:"):] if provider.startswith("custom:") else provider
    entry = (cfg.get("providers") or {}).get(name) or {}
    api_base = str(entry.get("api") or entry.get("base_url") or "").strip()
    key_env = str(entry.get("key_env") or "").strip()
    return provider, model, api_base, key_env


def declared_vision(cfg: dict, provider: str, model: str) -> bool | None:
    model_cfg = cfg.get("model") or {}
    for key in ("supports_vision",):
        if isinstance(model_cfg.get(key), bool):
            return model_cfg[key]
    name = provider[len("custom:"):] if provider.startswith("custom:") else provider
    for candidate in (name, provider):
        entry = (cfg.get("providers") or {}).get(candidate) or {}
        models = entry.get("models") or {}
        entry_model = models.get(model) or {}
        if isinstance(entry_model.get("supports_vision"), bool):
            return entry_model["supports_vision"]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 Hermes route 的看图能力")
    parser.add_argument("--image", help="探针图片路径（默认发一张 64x64 纯红）")
    parser.add_argument("--expect", default="红", help="期望答案里出现的词（默认「红」）")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    cfg = load_yaml(HERMES_HOME / "config.yaml")
    provider, model, api_base, key_env = route_of(cfg)
    declared = declared_vision(cfg, provider, model)

    print(f"route          : {provider or '?'} / {model or '?'}")
    print(f"api            : {api_base or '(未配置)'}")
    print(f"supports_vision: {declared if declared is not None else '(未声明 → 宿主按无视觉处理)'}")
    if declared is not True:
        print("\n❌ 宿主层：这条 route 没被声明为有视觉，MCP ImageContent 只会以 `MEDIA:<path>` 文本回给模型。")
        print("   修法：`hermes config set model.supports_vision true` 然后重启 gateway。")
        host_ok = False
    else:
        print("\n✅ 宿主层：声明为有视觉（图片会作为图文块进模型上下文）。")
        host_ok = True

    if not api_base:
        print("⚠️  未找到 providers.<name>.api，跳过中转/模型层探针。")
        return 0 if host_ok else 1

    raw = Path(args.image).read_bytes() if args.image else red_png()
    suffix = Path(args.image).suffix.lower() if args.image else ".png"
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}.get(suffix, "image/png")
    env = read_env_file(HERMES_HOME / ".env")
    key = env.get(key_env or "", "") or os.environ.get(key_env or "", "")
    if not key:
        print(f"⚠️  环境里找不到 {key_env or '(key_env 未配置)'}，跳过中转/模型层探针。")
        return 0 if host_ok else 1

    payload = {
        "model": model,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(raw).decode()}},
        ]}],
    }
    url = api_base.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read()[:400].decode(errors="replace")
        print(f"\n❌ 中转/模型层：HTTP {exc.code} — {body}")
        print("   这条 route 拒图（或参数不合），需要 aux vision 后端或换 route。")
        return 2
    except Exception as exc:  # noqa: BLE001 - 探针只报结果
        print(f"\n❌ 中转/模型层：{type(exc).__name__}: {exc}")
        return 2

    answer = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    answer = answer.strip()
    usage = data.get("usage") or {}
    print(f"\n模型回答       : {answer[:120] or '(空)'}")
    print(f"usage          : prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}")
    if not answer:
        print("⚠️  回答为空：可能是 max_tokens 全被 reasoning 吃掉，重跑一次或调大 max_tokens。")
        return 0 if host_ok else 1
    if args.expect and args.expect in answer:
        print("✅ 中转/模型层：真的读到了图。")
        return 0
    print(f"⚠️  回答里没有「{args.expect}」：图可能没被解码（换个更清晰的探针图试试）。")
    return 0 if host_ok else 1


if __name__ == "__main__":
    sys.exit(main())
