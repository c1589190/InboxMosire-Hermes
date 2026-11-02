# DSH 原版对齐表

对照物：

- 人设：`iiBotMosire-DSH/packages/dsh-persona-mode/`（`index.js`、`lib/persona.js`、`custom-persona.js`、`README.md`）
- 通知板：`InboxMosire-DSH/`（`index.js`、`tools.js`、`lib/adapter.js`、`lib/manager.js`、
  `lib/template.js`、`lib/state.js`、`lib/config.js`、`lib/commands.js`、`lib/wake-profile.js`）

图例：**✅ 对齐**（语义一致，仅宿主 API 不同）/ **🔁 等价映射**（机制不同、行为等价）/
**⚠️ 已知差异**（宿主缺能力，写明降级）/ **❌ 不做**（并说明理由）。

## 1. 人设（persona-mode）

| DSH 原版 | Hermes 实现 | 状态 |
|---|---|---|
| preset 的 system prompt 只有 2 句：`你在扮演 <persona-contract> 定义的角色。在调用工具进行聊天发言时，请遵循 <persona-contract> 中的相关规则。` | `register_system_prompt_section("persona-mode-role", <同文本>, position="after_memory", max_chars=200)` | 🔁 |
| `complete: true` + `includeRuntimeContext: false`：**替换**整个 system prompt，抑制 “You are an AI agent…” 与工具指引 | Hermes 的 system prompt 归核心所有，插件段只能**追加**（`position` 仅支持 `after_memory`，`hermes_cli/plugins_dispatch.py:98`） | ⚠️ 基础提示仍在；角色位文本与 DSH 相同，但不是在「唯一」提示 |
| `custom-persona` 用 `interpolate: false`，人设里的 `{{...}}` 不被模板求值 | Hermes 不对插件注入文本做模板插值，原样送达 | ✅ |
| 首轮注入 `<system-reminder>…<persona-contract>…</persona-contract>` user message | `pre_llm_call` 返回 `{"action": null, "context": <同文本>}`，追加到本轮 user message（`agent/turn_context.py:849-880`） | 🔁 |
| `surfaceHasContract(agent, hash)`：surface 里已有同 hash 契约则跳过 | 扫 `pre_llm_call` 的 `conversation_history` 找 `<!-- persona-hash: … -->` 标记 | ✅ |
| 压缩后 surface 丢契约 → 重新注入 | 同一条 history 扫描自动重锚；`post_compaction` 仅记 oplog（`hermes_cli/plugins.py:203`） | ✅ |
| 缺文件 → `buildMissingMessage()` 操作者提示 + `qq_conversation_send` 门禁 | 同文本提示走 `pre_llm_call`；门禁走 `pre_tool_call` 返回 `{"action":"block","message":…}` | ✅ |
| 门禁按工具名后缀识别（含 MCP 前缀） | 同：`*qq_conversation_send*` / `*conversation_send*` 后缀匹配 | ✅ |
| `PERSONA.md` 从 session cwd 解析 | `ctx.session_cwd()` + `persona_file`（默认 `PERSONA.md`） | ✅ |
| preset 同时把工具组收窄到 Minimal（`read`/`read_image`/`bash`/`subagent_custom`；`write`/`edit` 关） | 插件不改变 agent 工具集（Hermes 无 preset 概念）；需要收窄时用 `deny_tools` 配置 → `pre_tool_call` 屏蔽，或用 Hermes 自己的 toolset 配置 | ⚠️ 默认不收窄，配置可选 |
| 人设文件模板 `PERSONA.md.example` | 仓库附 `PERSONA.md.example`（与 bot/PERSONA.md 同结构） | ✅ |

## 2. 绑定与工具面（inbox-wake）

| DSH 原版 | Hermes 实现 | 状态 |
|---|---|---|
| `inbox_adapter_status`（回源刷新，绝不返回 token） | 同名工具；`refreshPanelStatus` → `GET /status` | ✅ |
| `inbox_adapter_bind`（`sessionId` 缺省取调用方；接受 descriptor 或 inboxId+adapterTokenRef+channels+label+wakeMode） | 同名工具，参数同名同义；调用方身份取 `plugin_session.session_key()` | ✅ |
| `inbox_adapter_unbind` | 同名工具 | ✅ |
| `inbox_adapter_log`（`limit` 1–100，默认 20，仅本会话） | 同名工具，同默认值 | ✅ |
| business tools 留在 `mcp__inbox-mosire__*`（program-owned MCP） | 由 `config.yaml` 的 `mcp_servers` 挂载 InboxMosire MCP（`url` + `headers.Authorization`），插件不镜像 | ✅ |
| `/inbox [status|bind|unbind|retry|help]` | `register_command("inbox", …)`，同子命令同帮助文本 | ✅ |
| `wakeMode: claim | view-ack` | 同枚举、同默认（`claim`） | ✅ |
| wake profile 注册表（Cordis service `inboxWakeProfiles`，`generic` 内置 + `iibot` 零语义别名） | 无 service registry：profile 体在插件内置常量，`wakeProfile` 配置选择（`generic`/`iibot`） | 🔁 |
| 每 session 一条 binding，`agent.id` 作 key | 每 `session_key` 一条 binding | 🔁 |

## 3. 唤醒逻辑与文本

| DSH 原版 | Hermes 实现 | 状态 |
|---|---|---|
| `agent/status: idle` 主触发 + `turn/end` 次触发，合并为一次串行去抖决策 | 无 turn 状态事件：`pre_llm_call` 标 running、`post_llm_call`/`on_stream_end` 标 idle；running 期间唤醒进 `pendingWake`，本轮结束补投 | ⚠️ 近似 |
| `agent.followup(message)` | `ctx.inject_message(text, session_key=…)`（`hermes_cli/plugins.py:588`；contract `hermes_cli/plugins_injection.py`） | 🔁 |
| gateway 注入无需额外授权 | **必须** `plugins.entries.inbox-wake.allow_gateway_injection: true`，否则返回 `False` | ⚠️ 宿主策略，已文档化 |
| 幂等键 `inbox:<id>:revision:<n>:session:<ref>` + `msg_inbox_<sha256[:24]>` 消息 id | 同 key 算法；`inject_message` 无幂等键 → 去重完全靠持久化水位 | ⚠️ 同 DSH 的处置（DSH 也是自己实现） |
| 同一 revision 只唤醒一次 | `lastWokenRevision` 水位 + `pendingWake` | ✅ |
| 先 `GET /status` 回源再决定（不信 SSE 缓存） | 同 | ✅ |
| 退避：30s 起，最高 900s | 同（`wakeBackoffBaseMs`/`wakeBackoffMaxMs`） | ✅ |
| 去抖 300ms | 同（`debounceMs`） | ✅ |
| 摘要 ≤3 条，`itemId/channel/kind/summary`，标题 ≤80 字符，块预算 1200 字符，「还有 N 条未展示」 | 逐条照抄（文本、预算、回退规则、`JSON.stringify` 行格式） | ✅ |
| view-ack 紧凑行（`source/kind/summary`，无 itemId/channel） | 同（`wakeMode: view-ack` 时启用） | ✅ |
| 可选 content 段（`wakeContent: bounded`，独立预算 4000/12000，标为不可信数据） | 同 | ✅ |
| 固定指令段永不截断 | 同 | ✅ |
| header `[INBOX_HOST_EVENT]` + `inbox=… revision=… available=… channels=[…]` | 同 | ✅ |
| SSE + `Last-Event-ID` 续传、45s 心跳超时、1s→60s 重连退避 | 同 | ✅ |
| 只读：不 claim/complete/push | 同（AGENTS.md 红线 4） | ✅ |
| 占用标记 `PUT/DELETE occupancy`，失败降级 | 同 | ✅ |
| `applyRest(restSeconds)`：完成类工具结果带 `restSeconds` → `nextWakeAllowedAt` | `post_tool_call` 观察工具结果里的 `restSeconds`（≤86400，0 清除） | 🔁 |
| program-MCP 就绪诊断（`hasBusinessTools(agent, wakeMode)`） | 查工具注册表里是否存在 `inbox_list`（含 MCP 前缀），结果写 `health.programMcp` 并在 `status` 里显示 | 🔁 |

## 4. 状态、日志、生命周期

| DSH 原版 | Hermes 实现 | 状态 |
|---|---|---|
| `$DSH_HOME/inbox-mosire/state.json`（0600，version 1，v1 迁移保留在 `legacyV1`） | `ctx.state`（`<HERMES_HOME>/plugin-data/inbox-wake/`），同字段、同 version 策略 | 🔁 |
| `ops-log` 文件 + `readOps(sessionId, limit)` | ops log 存 `ctx.state` 环形缓冲（≤200/session），`inbox_adapter_log` 读 | 🔁 |
| `agent/created`（startup/resume/clear/compact）→ 重建 binding | `on_session_start` 等价（含 resume）；状态文档按 session_key 恢复 | 🔁 |
| `agent/disposed` / `workspace/session-stop` → 立即停 SSE、清占用 | `on_session_end` / `on_session_reset` / `on_session_delete` | 🔁 |
| `agent/turn-stopping`（serial） | 无对应；不需要（摘要唤醒不要求 turn 收尾） | ⚠️ 不使用 |
| panel 挂载/卸载 → `resume`/`suspend` | 无 panel；不实现 | ❌ 桌面 UI 不做（用户明确不关心） |
| WebUI inbox 面板（`client.js`/`lib/panel.js`） | 不做 | ❌ |
| `docs/ACCEPTANCE.md` 的真机验收（121 项测试、双 binding 隔离、隐私扫描） | Hermes 侧重建同结构清单，实现后逐项跑 | 🔁 见 `docs/ACCEPTANCE.md` |

## 5. 汇总

- **语义对齐**：19 项 ✅ + 9 项 🔁。
- **已知差异**：6 项 ⚠️（system prompt 不能替换、无 preset 工具收窄、无 turn 状态事件、
  gateway 注入需显式授权、无幂等键、无 `turn-stopping`）。
- **不做**：桌面面板/挂载态 suspend-resume（❌，用户明确不看 UI）。
