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
3. 唤醒正文只放有界摘要（≤3 条；`itemId/channel/kind/source/summary`；标题 ≤80 字符；
   摘要块总预算 1200 字符）。正文只能来自可选的 opt-in content 段，且必须标为不可信数据。
4. adapter 只读：只允许 `GET /status`、`GET /events`、`GET /items`、`/items/{id}`。
   禁止 claim / renew / complete / release / push / items 写操作。
5. 真实外发工具（`qq_conversation_send`、`conversation_send`、B 站评论私信、好友审批）
   在协调/开发工作区禁止启用；插件侧的门禁只是兜底，不是授权。
6. 不新增固定家目录绝对路径：插件根从 `__file__` 推导，数据目录用 `ctx.state`，
   人设文件从 session cwd 推导。

## 4. 目录约定

```
plugins/<id>/__init__.py     # register(ctx)：唯一入口
plugins/<id>/plugin.yaml     # manifest（v2）
plugins/<id>/lib/            # 纯逻辑，import 时不触碰宿主
tests/                       # pytest；纯函数优先，宿主用手写 fake ctx
scripts/install.sh           # 落盘到 $HERMES_HOME/plugins/ 并打印启用片段
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
python -m compileall plugins            # 语法
python -m pytest -q                     # 单测（纯逻辑 + fake ctx）
hermes plugins doctor persona-mode      # manifest/register/registry 漂移
hermes plugins doctor inbox-wake
```

`hermes` CLI 需要能写 `$HERMES_HOME/installs/*`；在受限 shell 里会报
`install state is not writable by this user (…/.recovery.lock)` —— 那是环境问题，
不是插件问题，交给操作者在自己终端跑。

## 7. 提交

- 本仓库独立 Git，提交信息用中文、一行摘要 + 要点列表。
- 禁止提交 `*.token`、`config.json`、`state.json`、`logs/`、`.venv/`、`__pycache__/`。
