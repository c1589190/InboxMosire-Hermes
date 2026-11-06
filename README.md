# InboxMosire-Hermes

把 iiBot 项目的**人设**（`PERSONA.md`）与 **InboxMosire 通知板**接进 [Hermes](https://github.com/hermes-agent)：
两个独立的后端插件 + 一个「一个目录就够」的发行包 `kit/`。能力对齐 DSH 原版
（`InboxMosire-DSH` + `iiBotMosire-DSH/packages/dsh-persona-mode`）。

> **对齐原则**：行为对齐、面（surface）按宿主能力映射。DSH 有的语义（首轮注入、压缩重锚、
> 缺文件门禁、bounded summary 唤醒、revision 去重、rest/snooze、占用标记、操作日志）在 Hermes
> 侧一项不少；宿主确实没有的能力（DSH 的 `agent/status` 事件、`complete: true` 全量替换
> system prompt）在 `docs/DSH-PARITY.md` 逐条登记为**已知差异 + 降级方案**，不静默跳过。

---

## 这是什么，不是什么

| 是 | 不是 |
|---|---|
| Hermes 的**后端插件**（纯 Python，`lib/` 导入零副作用） | 不是桌面/终端 UI |
| 通知板**唤醒适配器** + 人设注入器 | 不是 InboxMosire 服务端（它有自己的 jar/MCP） |
| 只消费 InboxMosire 公开 HTTP/SSE 契约 | **不含业务工具**（`inbox_list`/`inbox_claim`… 走 InboxMosire 的 MCP） |

> **业务工具不在这里。** `inbox_list` / `inbox_claim` / `inbox_complete` 等 12 个工具由 InboxMosire
> 自己的 MCP 端点提供（`http://127.0.0.1:18766/mcp`），在 Hermes 里用 `mcp_servers` 挂载。这与 DSH
> 原版「adapter 只唤醒，业务走 program-owned MCP」的分工一致。插件的 `programMcp` 健康位会在挂上
> MCP 后变成 `ready`。

---

## 30 秒上手

### A. 已经有 InboxMosire 后端（最常用）

```bash
git clone git@github.com:c1589190/InboxMosire-Hermes.git && cd InboxMosire-Hermes
./scripts/install.sh --local .          # 装插件 → 写 adapter token(0600) → 写设置 → 体检
hermes mcp add inbox-mosire --url http://127.0.0.1:18766/mcp --auth header   # 业务工具
hermes mcp add iibot        --url http://127.0.0.1:8765/mcp/bot --auth header # 可选：机器人业务
timeout 120 hermes gateway stop --yes          # 改完插件/设置必须重启 gateway
nohup hermes gateway run >> ~/.hermes/logs/gateway.log 2>&1 &
hermes --tui -c                                # 开会话（首轮 autoBind 自动绑定）
```

### B. 什么都没有，只有一个目录

```bash
cd InboxMosire-Hermes/kit
cp kit.env.example kit.env        # 改 jar 路径 / 数据目录 / 端口
./kit.sh doctor                   # 体检（不打印 token）
./kit.sh up                       # 起 inboxd → 签两个最小权限 token → 挂 MCP → 写设置 → 重启 gateway
```

`kit/引导.md` 是给 agent 读的引导文件：把 `jar + 这个目录` 丢给一个会话，说「按引导文件把 inbox 起起来」。

### C. 手动分步

`./scripts/install.sh plugins|token|settings|check`，或按 `docs/INSTALL.md` 逐条 `hermes config set`。

---

## 组成

| 组件 | 作用 | 依赖宿主面 |
|---|---|---|
| `plugins/iibot-hermes` | **唯一插件**（0.4.0 由 `inbox-wake` 0.3.1 + `minimal-system` 0.2.1 + `persona-mode` 0.2.0 合并）。三段能力：`lib/wake/` 每 session 独立 binding、SSE 订阅 + 对账轮询、`inbox.push` 正文直投 + 立即 ack、`inbox.changed` 有界摘要唤醒、4 个工具 + `/inbox` 命令；`lib/prompt/` 一句式 system prompt（`llm_request` 中间件，**默认关**，宿主 `agent.system_prompt_override` 优先）；`lib/persona/` 人设契约注入（`pre_llm_call` + `pre_tool_call` 守卫，**默认关**，D-009）；`lib/vision/` **图片必达**（0.4.0-fix1，`llm_request` 中间件把 tool/assistant/system 消息里的图片搬进紧随其后的一条 `role=user` 消息，**默认开**，无图时请求逐字节不变） | `inject_message` / `register_tool` / `register_command` / `register_middleware("llm_request")` / `pre_llm_call` / `pre_tool_call` / `post_llm_call` / `post_tool_call` / `on_session_*` |
| `kit/` | 一键发行包：`kit.sh`（doctor/up/status/down/probe/token）+ `kit.env.example` + `引导.md` | 只用宿主 CLI 与 InboxMosire jar |

---

## 一次唤醒是怎么走完的

```
iiBot / 后端写入
      │
      ▼
InboxMosire inboxd ──18765 API / 18766 MCP（可选）
      │  ① SSE `inbox.push`（服务端渲染好的整段正文）/ `inbox.changed`（只带计数，pollSeconds 兜底）
      ▼
Hermes gateway（常驻；iibot-hermes 的线程 + hook + 4 个工具 + /inbox 都在这个进程里）
      │  ② push：把 data.text **原样**注入（不加 header）→ 立即 POST …/deliveries/{id}/ack
      │     changed：注入 `[INBOX_HOST_EVENT] inbox=… revision=… available=…`
      ▼
会话跑一轮：先 inbox_list 看摘要 → inbox_claim 领取 → 干活 → inbox_complete
      │  （这 13 个工具来自 mcp_servers 挂的 InboxMosire MCP；ack 由插件回，
      │     Agent 侧还有 inbox_ack 可手动补）
      ▼
CLI / TUI 只是客户端，看到的是这一轮的输出
```

**`inbox.push` 路径（W5-1/W5-2，主路径）**

1. 事件到达 → 解析 CloudEvents 信封里的 `data`，正文就是 `data.text`（**逐字**，多行/缩进/结尾换行都不动）
2. 按 `deliveryId` 去重（水位落在 `ctx.state` 的 `pushes` 键，重启后仍生效）
3. 注入 `inject_message(text, session_key=…)`（角色 `user`）；有本地绝对路径附件时带 `media_urls`，
   远端 URL 只记日志不下载
4. 注入成功立即 ack：优先程序 MCP 的 `inbox_ack`，否则 `POST /v1/inboxes/{inbox}/deliveries/{deliveryId}/ack`
   （最多重试一次；失败只 warning，**绝不**因此重投）
5. 注入失败则不 ack，条目留在板上等下一次事件

**`inbox.changed` 路径（兼容旧事件）**

1. 事件到达 → 读 status 拿 `revision` / `available`
2. `available <= 0` → 不唤醒，只推进游标
3. `revision <= lastWokenRevision` → 去重
4. 退避未到期 / turn 正在跑 → 存 pending，稍后补投
5. 组文本：header + 固定指令 + 有界摘要（≤3 条，标题 ≤80）+ 可选正文段；
   push 已经注入过的 `itemIds` 按 `skip_item_ids` 从摘要与正文段里剔除
6. `inject_message(...)` → 宿主按「CLI 队列 → TUI 宿主 → messaging gateway」路由
7. 成功记 `wake.ok`；宿主明确拒绝时按宿主类型分别记 `wake.deferred` / `wake.failed`

---

## ⚠️ 关键前提：谁能承接「空闲时被唤醒」

注入不是「发一条你就会收到」——宿主必须有一个**常驻的会话对象**去接。这是宿主规则，插件绕不过去：

| 会话起法 | session key | 空闲时能收唤醒 | 原因 |
|---|---|---|---|
| `hermes` / `hermes --cli`（经典 REPL） | `agent:<profile>:local:dm:local-<hash>` | ❌ 只有 turn 运行中能 | gateway 只投给 `authority.sessions[chat_id]` 里的 live local session（`gateway/run_plugin_injection.py:_plugin_local_session`） |
| `hermes --tui` / 桌面 / web | 同上 | ✅ | TUI 后端自己持有会话（`tui_gateway/server._sessions`），`install_tui_message_injector` 按 `session_key` 认领，闲时 drain 起一轮 |
| api_server（`X-Hermes-Session-Key`） | `agent:<profile>:api_server:dm:api-<hash>` | ✅ | 会话由 gateway 持有 |
| 常驻 messaging 平台（qq/telegram…） | `agent:<profile>:<platform>:dm:<chat>` | ✅ | 同上，走 adapter 投递 |

所以在经典 REPL 里你会看到 `wake.deferred reason=no-live-session`（按 `wakeRetrySeconds` 固定 15s 重试，
不烧退避）——这不是条目有问题，是宿主此刻没有活会话。**要真·空闲唤醒，请用 `hermes --tui`、api_server
或常驻 messaging 会话。** `/inbox status` 的 `inject:` 行会显示当前宿主：`tui` / `gateway` / `cli` / `none`。

---

## 配置

必需只有三项（其余都有默认值，完整表见 `docs/TOOLS.md`）：

| 键 | 例子 | 说明 |
|---|---|---|
| `plugins.entries.iibot-hermes.settings.inbox.apiUrl` | `http://127.0.0.1:18765` | InboxMosire API |
| `plugins.entries.iibot-hermes.settings.inbox.inboxId` | `main` | 订阅哪个通知板 |
| `plugins.entries.iibot-hermes.settings.inbox.adapterTokenFile` | `file:/path/to/api-token` | **0600 文件**，`inline:` 一律拒绝 |

设置分四段：`settings.inbox.*`（唤醒，键名同旧 `inbox-wake.settings.*`）、`settings.prompt.*`、
`settings.persona.*`、`settings.vision.*`（0.4.0-fix1 新增，无 0.x 前身）。前三段整段缺失时会
**只读回落**到旧命名空间（`plugins.entries.inbox-wake.settings` /
`plugins.entries.minimal-system.settings` / `plugins.entries.persona-mode.settings`）并打一条 WARNING，
迁移步骤见 `docs/INSTALL.md`。`vision` 没有旧命名空间，缺省即用默认值。

常改的几个：

| 键 | 默认 | 用途 |
|---|---|---|
| `autoBind` | `false` | 会话首轮自动用上面三项绑定（失败只 warning，不打断 turn） |
| `wakeMode` | `claim` | `claim`（业务 MCP 领）或 `view-ack`（只读+推进未读） |
| `wakeProfile` | `generic` | `iibot` 是按机器人措辞的别名 |
| `debounceMs` | `300` | 事件去抖窗口 |
| `pollSeconds` | `0` | 后端 `/events` 不发 `: ping` 心跳时的对账兜底（建议 30–60） |
| `sseReadTimeoutSeconds` | = `heartbeatTimeoutSeconds`（45） | SSE 自己的读超时，别跟 `requestTimeoutMs` 混用 |
| `maxBindingsPerInbox` | `1` | 同一 inbox 只保留 1 条活跃绑定，其余降级 `passive`；防止一次性会话留下僵尸绑定（一事件叫醒 N 个会话） |
| `wakeRetrySeconds` | `15` | 本地会话被宿主拒收时的固定重试间隔，不进指数退避 |
| `pushMode` | `auto` | 投递通道：`on` = 只信服务端 `inbox.push`（`inbox.changed` 仅刷水位，推荐；本工作区已设 `on`）；`off` = 只用 `changed`（旧服务端）；`auto` = 见到第一帧 push 前用 `changed` + `pushGraceMs` 宽限 |
| `pushGraceMs` | `2000` | `auto` 模式下 `changed` 唤醒的宽限期（0..30000 ms）：push 若先到就把这次唤醒撤掉，避免同一批消息注入两次 |
| `firstRunGuide` | `true` | 每个会话首次注入前先发一条极短说明，引导 Agent 自己去读工作区的 `AGENTS.md` / `PERSONA.md`（D-009：人设不再由宿主注入） |
| `firstRunGuideText` | `""` | 覆盖上面那条说明的文案；空 = 用内置默认 |
| `allow_gateway_injection`（`plugins.entries.iibot-hermes.`，**一级 entry 键，不在 settings 里**） | — | 宿主侧授权，**必须显式 `true`**；旧位置 `plugins.entries.inbox-wake.allow_gateway_injection` 只在启动时告警，不再生效 |

### prompt / persona（0.4.0 起默认关）

| 键 | 默认 | 说明 |
|---|---|---|
| `plugins.entries.iibot-hermes.settings.prompt.enabled` | `false` | 打开才注册 `llm_request` 中间件（整段替换 system prompt）。与宿主 `agent.system_prompt_override` **二选一** |
| `…settings.prompt.systemPrompt` / `systemPromptFile` | 一句常量 / `""` | 文件优先于常量；留空 = `$HERMES_HOME/SYSTEM.md`，按 mtime 热更新 |
| `plugins.entries.iibot-hermes.settings.persona.enabled` | `false` | 打开才注册 `pre_llm_call`（角色行 + `PERSONA.md` 契约，按 hash 去重、压缩后重锚）与 `pre_tool_call` 守卫（`denyTools` / 缺文件封禁外发）。D-009 之后人设改由首条 inbox 引导读 `AGENTS.md` / `PERSONA.md` |
| `…settings.persona.personaFile` | `PERSONA.md` | 绝对路径，或相对调用会话的工作目录 |
| `…settings.persona.maxChars` / `outboundGuard` / `outboundTools` / `denyTools` | `4000` / `true` / 两个发送工具 / `[]` | 与 persona-mode 0.2.0 同义 |

### vision（0.4.0-fix1 起默认开）

| 键 | 默认 | 说明 |
|---|---|---|
| `plugins.entries.iibot-hermes.settings.vision.enabled` | `true` | 出站前把**非 user 消息**里的图片搬进紧随其后的 `role=user` 消息。OpenAI 的 tool 消息只允许文本块、DeepSeek 视觉文档只认 user 消息；官方 tag 会把 MCP/`vision_analyze` 的图挂在 tool 行，provider 静默丢像素（模型只看到 "Image loaded"）。无图时逐字节不变；宿主带 `hoist_tool_result_images` 补丁时是 no-op |
| `…settings.vision.maxImages` | `4` | 单请求上限（1–16）；超出的从最旧开始丢弃并打 WARNING |
| `…settings.vision.placeholder` | `""` | 原消息里留下的占位文本；空 = 内置「[图片已移出本消息，见随后那条 user 消息]」 |
| `…settings.vision.logHoist` | `false` | 每次搬运记一条 INFO（默认 debug）；只记数量与来源角色 |

排查顺序：`scripts/check-vision.py` 查「宿主有没有声明视觉 + route 吃不吃图」；本中间件查第三层
「图有没有被挂在 tool 消息里」。机理见 `docs/DESIGN.md` 第 6 节，验收见 `docs/ACCEPTANCE.md` 的 Q 段。

另外 Hermes **没有** `register_mcp_server`，业务 MCP 只能写配置：
`hermes config set mcp_servers.<name>.url/.transport/.headers.Authorization`。

---

## 命令与工具

```
/inbox status                          # 绑定、SSE 健康、inject 宿主、游标与最近一次唤醒
/inbox owners                          # 本机所有绑定；只有 1 条 active，其余 passive
/inbox bind iibot-bot                  # 在本会话执行 = 钉住（manual），别的会话自动降级
/inbox bind main                       # 用 settings 里的 token 绑定（或 bind <descriptor.json>）
/inbox bind main channel-a,channel-b   # 只订阅部分通道
/inbox log 20                          # 最近 20 条适配器事件（sse.event / wake.ok / deferred / failed）
/inbox retry                           # 清退避并重连（token rotate 后也要跑）
/inbox unbind                          # 解绑并释放 occupancy
/inbox help
```

工具（toolset `inbox-mosire`，agent 视角）：`inbox_adapter_status` / `inbox_adapter_bind` /
`inbox_adapter_unbind` / `inbox_adapter_log`。

人设：会话工作目录放 `PERSONA.md`（QQ bot 的 cwd 是 `/home/cna/DevMosire-iiBot/bot`，示例见
`PERSONA.md.example`），新一轮对话自动注入；文件缺失/为空时插件下发一次操作者提示，并封禁
`qq_conversation_send` / `mcp__iibot__qq_conversation_send`。

---

## 文档

| 文件 | 内容 |
|---|---|
| **`docs/RUNBOOK.md`** | **部署 / 启动 / 停止 / 升级回滚 / 验证清单 / 远程部署注意 / 排障表（含宿主差异）** |
| `docs/INSTALL.md` | 安装、启用、验证、回滚、kit 用法 |
| `docs/TOOLS.md` | 工具 schema、`/inbox` 命令、全部配置键与默认值 |
| `docs/DESIGN.md` | 架构、数据流、状态机、唤醒文本格式、幂等与去重 |
| `docs/DSH-PARITY.md` | DSH 原版能力 → Hermes 实现逐项对照（含宿主语义差异） |
| `docs/HOST-CAPABILITIES.md` | Hermes 宿主能力探测，带 `file:line` 依据 |
| `docs/ACCEPTANCE.md` | 验收清单（A–L 段）+ 真机验收记录（Z 段） |
| `docs/CHANGELOG.md` | 从首个真机轮次到现在的改动与修复 |

---

## 开发与测试

```bash
python3 -m compileall -q plugins
python3 -m unittest discover -s tests        # 231 个用例
hermes plugins validate plugins/iibot-hermes --json
hermes plugins doctor iibot-hermes           # 4 tool(s), 7 hook(s)（prompt/persona 默认关；persona 打开后 9）
# doctor 会 WARN "manifest declares hook 'pre_tool_call' but registration did not add it"：
# 那是 persona.enabled=false 的预期结果（manifest 取并集声明，运行时按开关注册）。
./scripts/patch-hermes.sh check               # 宿主极简 system 补丁是否已打（见 docs/HOST-PATCH.md）
```

- `lib/` 导入零副作用；所有线程都能被 disposer `join`。
- 唤醒正文 ≤3 条摘要、标题 ≤80、摘要总预算 1200 字符（超出折叠为 `- 还有 N 条未展示`）。
- 端到端自测脚手架（假 InboxMosire + pty/API 驱动）不随仓库分发，见 `docs/ACCEPTANCE.md` 的复现步骤。

---

## 已知限制（`NOT_RUN` 项见 `docs/ACCEPTANCE.md`）

1. **经典 REPL 空闲收不到唤醒**（宿主规则，见上文矩阵）；要空闲唤醒请用 TUI / api_server / 常驻 messaging。
2. 后端 `/events` 若不发 `: ping` 心跳，SSE 会被判超时重连 → 设 `pollSeconds` 兜底，或让后端加心跳。
3. `hermes gateway restart` 在有活跃会话时会挂住/被拒：用 `hermes gateway stop --yes` 再 `run`，并**轮询确认已停**再启动（否则新进程只打印 "already serves profile 'default'" 就退出）。
4. MCP 服务 keepalive 抖动（`MCPError: Session terminated`）会让业务工具在新会话里缺席，重启 gateway 后重新注册。
5. 一次性/CLI 进程**不能**建立绑定（`NO_INJECT_HOST`）：它们投递不了唤醒，绑定只会变成僵尸。
6. 人设门禁的两个 QQ 工具名在本机不存在，真机拦截用例为 `NOT_RUN`（仅单测覆盖）。
7. 图片必达只整形**出站请求**：图仍然只在下一次 API 调用里出现一次，落库仍是 `[screenshot]` 摘要；重新取图要靠业务 MCP（`qq_media_get`）。真机用例（Q6/Q7）需在维护窗口部署后验证。

---

## 红线

沿用 `DevMosire-iiBot/AGENTS.md` 与 InboxMosire wake-adapter 契约：

- token / Bearer / claim token 不进日志、事件、LLM 上下文、唤醒正文、提交；token 文件必须 `0600`。
- 唤醒正文只放**有界摘要**（itemId/channel/kind/source/summary，≤3 条，标题 ≤80），正文按需用业务 MCP 拉。
- adapter 只读：只用 `GET /status`、`GET /events`、`GET /items`，不做 claim/complete/push。
- 真实外发（`qq_conversation_send`、B 站评论私信、好友审批）必须有人或主 Agent 明确决策。
