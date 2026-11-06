# AGENTS.md — InboxMosire-Hermes

本文件是本仓库的**工作约束**。上层工作区约束见 `/home/cna/DevMosire-iiBot/AGENTS.md`，
本文件只补充 Hermes 适配特有的部分；两者冲突时以更严格的一条为准。

## 1. 这是什么

Hermes 后端插件（Python），把 iiBot 的人设与 InboxMosire 通知板接进 Hermes agent 进程。
**不是**桌面 UI 插件（`~/.hermes/desktop-plugins/`），**不是** InboxMosire 服务端的一部分。

## 2. 依赖方向（不得反转）

```
InboxMosire (Java, 通知板)  ──HTTP/SSE/MCP──▶  InboxMosire-Hermes (Python 插件)  ──▶  Hermes agent
```

- 本仓库**只消费** InboxMosire 的公开 HTTP/SSE/MCP 契约（`InboxMosire/docs/upstream/*`），
  不 import InboxMosire 内部实现、不改它的源码。
- 本仓库**不**依赖 DSH、不依赖 OpenCode、不 import 宿主私有模块（除 `hermes_cli.*` 公开插件 API）。
- 业务工具（`inbox_*`）由 InboxMosire 的 MCP 端点提供，本插件**不镜像、不代理、不重命名**它们。

## 3. 红线（继承工作区 + Hermes 特有）

1. **token 永不出现在**：日志、`ctx.state`、ops log、事件 payload、唤醒文本、工具返回值、
   异常消息、提交内容。插件只持有 `INBOX_API_TOKEN_FILE` 的**路径**，值只在内存与
   `Authorization` 头里存在。
2. token 文件权限必须 `0600`，插件启动时校验并在不满足时拒绝启动（fail closed）。
3. 唤醒正文是**一次给够**的有界快照：摘要行（`summaryMaxItems` 1–50 默认 20；
   `summaryMaxTotalChars` 默认 6000；字段 `itemId/channel/kind/title/source`；标题 ≤120 字符）
   + 正文段（`wakeContent=bounded|full`；单条 ≤`wakeContentMaxChars` 默认 12000，整段
   ≤`wakeContentTotalChars` 默认 30000；按服务端 `priority DESC` 顺序渲染，超预算时显式标注
   「还有 N 条未渲染」）。**唤醒文本不写提示性散文**（2026-11-03 用户裁定：一条 1 条消息的唤醒里，
   「这是唤醒信号 / source 不是指令」这类话占了半屏）；不可信标注改为 header 末尾的一个 token
   `body=untrusted`（`template.header(body_marker=True)`），正文只由编号来源行 + 时间线 + seq 尾注组成。
   超限只看条数与字符预算，不看「最多三条」。
   原始业务字段 ≠ 凭据：token / 授权头 / QQ 凭据 / B 站 Cookie 仍然禁止出现在任何摘要里。
4. adapter 只读：只允许 `GET /status`、`GET /events`、`GET /items`、`/items/{id}`。
   禁止 claim / renew / complete / release / push / items 写操作。
   **唯一登记例外（2026-11-06，用户当次指令裁定，W5-1/W5-2）**：`inbox.push` 事件的
   `POST /v1/inboxes/{inbox}/deliveries/{deliveryId}/ack`。理由：正文已经由服务端渲染好并原样
   交给 Agent，ack 只表示「这条投递已经送到」，不 claim、不 complete 任何条目（`acked:false`
   幂等返回）。该调用失败只 warning，绝不重投；实现见 `plugins/iibot-hermes/lib/wake/push.py`
   的 `ack_delivery()` 与 `plugins/iibot-hermes/lib/wake/binding.py` 的 `_ack_delivery()`。
5. 真实外发工具（`qq_conversation_send`、`conversation_send`、B 站评论私信、好友审批）
   在协调/开发工作区禁止启用；插件侧的门禁只是兜底，不是授权。
6. 不新增固定家目录绝对路径：插件根从 `__file__` 推导，数据目录用 `ctx.state`，
   人设文件从 session cwd 推导。

## 4. 目录约定

```
plugins/iibot-hermes/__init__.py   # register(ctx)：唯一入口（wake + prompt + persona 三段装配）
plugins/iibot-hermes/plugin.yaml    # manifest（v2，name: iibot-hermes）
plugins/iibot-hermes/lib/{wake,prompt,persona}/  # 纯逻辑，import 时不触碰宿主
plugins/iibot-hermes/lib/settings.py # 设置分段 + 旧命名空间只读回落
tests/                       # unittest（pytest 也能跑）；纯函数优先，宿主用手写 fake ctx（tests/_loader.py）
scripts/install.sh           # 装插件 → 写 adapter token(0600) → config set 设置 → doctor 体检
docs/                        # 设计/对齐/能力/工具/验收
```

- `lib/` 里的模块**不得**在 import 时读取配置、连网、起线程；一切副作用发生在 `register(ctx)` 内，
  且都必须挂到 `ctx` 注册句柄上（卸载即回收）。
- 线程必须可停止（`threading.Event`），`register` 的 disposer 里 join，禁止裸 `Thread` 泄漏。

## 5. 文档规则

- 设计/维护/验证事实写回本仓库 `docs/`，不写进 `bot/`（那是运行目录，不改源码）。
- 每条宿主能力声明必须带 `file:line` 依据（见 `docs/HOST-CAPABILITIES.md` 的写法）。
- 任何与 DSH 原版的差异都必须出现在 `docs/DSH-PARITY.md` 的「已知差异」表里，
  不允许「实现了但没登记」。
- 验收条目在真正跑通前只能写 `未验证`。

## 6. 校验

```bash
python3 -m compileall plugins           # 语法
python3 -m unittest discover -s tests   # 单测（纯逻辑 + fake ctx；pytest 亦可）
hermes plugins doctor iibot-hermes      # manifest/register/registry 漂移
```

`hermes` CLI 需要能写 `$HERMES_HOME/installs/*`；在受限 shell 里会报
`install state is not writable by this user (…/.recovery.lock)` —— 那是环境问题，
不是插件问题，交给操作者在自己终端跑。

## 7. 提交

- 本仓库独立 Git，提交信息用中文、一行摘要 + 要点列表。
- 禁止提交 `*.token`、`config.json`、`state.json`、`logs/`、`.venv/`、`__pycache__/`。
