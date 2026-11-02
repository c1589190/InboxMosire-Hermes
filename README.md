# InboxMosire-Hermes

把 iiBot 项目的**人设**（`PERSONA.md`）与 **InboxMosire 通知板**打包成 Hermes 后端插件，
能力对齐 DSH 原版（`InboxMosire-DSH` + `iiBotMosire-DSH/packages/dsh-persona-mode`）。

> 对齐原则：**行为对齐、面（surface）按宿主能力映射**。DSH 有的语义（首轮注入、压缩重锚、
> 缺文件门禁、bounded summary 唤醒、revision 去重、rest/snooze、占用标记、操作日志）
> 在 Hermes 侧一项不少；宿主确实没有的能力（例如 DSH 的 `agent/status` 事件、
> `complete: true` 全量替换 system prompt）在 `docs/DSH-PARITY.md` 里逐条登记为
> **已知差异 + 降级方案**，不静默跳过。

## 组成

两个插件，各自独立启用，互不依赖：

| 插件 id | 作用 | 依赖宿主面 |
|---|---|---|
| `persona-mode` | 首轮注入 `PERSONA.md` 人设契约、压缩后重锚、缺文件时门禁外发 | `pre_llm_call` / `post_compaction` / `pre_tool_call` / system prompt section |
| `inbox-wake` | 每个 session 独立 binding，SSE 订阅 + 有界摘要唤醒 + 绑定工具 + `/inbox` 命令 | `inject_message` / `register_tool` / `register_command` / `post_tool_call` |

> **业务工具不在这里。** 通知板业务工具（`inbox_list` / `inbox_claim` / `inbox_complete` …）
> 由 InboxMosire 自己的 MCP 端点提供（`http://127.0.0.1:18766/mcp`），在 Hermes 里通过
> `config.yaml` 的 `mcp_servers` 挂载。这与 DSH 原版「adapter 只唤醒，业务走 program-owned MCP」
> 的分工一致（见 `InboxMosire-DSH/tools.js` 头部注释与 `docs/upstream/mcp-tools.md`）。

## 安装

```bash
# 1. 落盘插件（本人 shell 若被沙箱限制写 ~/.hermes，请在你自己的终端执行）
./scripts/install.sh

# 2. 在 ~/.hermes/config.yaml 启用（脚本会打印需要粘贴的片段）
plugins:
  enabled: [persona-mode, inbox-wake]

# 3. 校验
hermes plugins doctor persona-mode
hermes plugins doctor inbox-wake
```

详见 `docs/INSTALL.md`（含 token 落盘 0600、`allow_gateway_injection`、MCP 挂载）。

## 使用

```
/inbox status                 # 当前 session 的 binding 状态
/inbox bind desk file:/path/to/wake.token channel-a,channel-b
/inbox unbind
/inbox retry                  # token rotate 后重读 token 并重启 binding
```

人设：会话工作目录放 `PERSONA.md`（QQ bot 的 cwd 是 `/home/cna/DevMosire-iiBot/bot`），
新一轮对话自动注入；文件缺失或为空时插件下发操作者提示并封禁 `qq_conversation_send`。

## 文档

| 文件 | 内容 |
|---|---|
| `docs/DESIGN.md` | 架构、数据流、状态机、唤醒文本格式、幂等与去重 |
| `docs/DSH-PARITY.md` | DSH 原版能力 → Hermes 实现逐项对照表（含已知差异） |
| `docs/HOST-CAPABILITIES.md` | Hermes 宿主能力探测，带 `file:line` 依据 |
| `docs/TOOLS.md` | 工具 schema、`/inbox` 命令、配置键与默认值 |
| `docs/INSTALL.md` | 安装、启用、验证、回滚 |
| `docs/ACCEPTANCE.md` | 验收清单与当前验证状态 |

## 状态

**设计定稿，实现未开始。** 本仓库当前只含文档与（即将添加的）插件源码骨架；
`docs/ACCEPTANCE.md` 里所有条目在实现并跑通前一律标记 `未验证`。

## 红线

沿用 `DevMosire-iiBot/AGENTS.md` 与 InboxMosire wake-adapter 契约：

- token / Bearer / claim token 不进日志、不进事件、不进 LLM 上下文、不进唤醒正文、不进提交；
  token 文件必须 `0600`。
- 唤醒正文只放**有界摘要**（itemId/channel/kind/source/summary，≤3 条，标题 ≤80 字符），
  正文按需用业务 MCP 工具拉取。
- adapter 只读：只用 `GET /status`、`GET /events`、`GET /items`，不做 claim/complete/push。
- 真实外发（`qq_conversation_send`、B 站评论私信、好友审批）必须有人或主 Agent 明确决策。
