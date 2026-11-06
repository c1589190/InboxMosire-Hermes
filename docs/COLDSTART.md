# 冷启动自举：Hermes 先起，agent 用人设把 iiBot 拉起来

> **0.4.0 提示（2026-11-07）**：插件已三合一为 `plugins/iibot-hermes`。本文写于合并前，文中 `inbox-wake` / `minimal-system` / `persona-mode` 的目录现为 `lib/wake` / `lib/prompt` / `lib/persona`，设置键现为 `plugins.entries.iibot-hermes.settings.{inbox,prompt,persona}.*`，`allow_gateway_injection` 在 `plugins.entries.iibot-hermes.allow_gateway_injection`。映射见 `docs/CHANGELOG.md` 的 0.4.0 条目。

状态：**设计定稿 + 已取证；实现与真机验收见 §6（全部 `未验证`）**
日期：2026-11-03
决策人：用户（m00324「作正式文档记录归档，然后开始吧」）；上一轮结论见 §1。

本文回答一个问题：**能不能先只起 Hermes，让 Hermes 的 agent 按人设自己找到 jar、起 iiBot、再拿到 MCP 工具与 Inbox？**
（用户 m00193 提出的验收方式；在 TUI + 自动 gateway 的日常用法下，**不重启 gateway**。）

---

## 1. 结论（三句话）

1. **起 jar：能，且有现成脚本**。iiBot 是**单进程三端口**，起一次就同时得到业务 MCP、Inbox HTTP、Inbox MCP。
2. **加载 MCP：不用重启 gateway，也不用人工发命令**。宿主有两条内建机制：housekeeping 的
   MCP config reconcile（按 drift 重连）＋ 每轮开头的 between-turns 工具刷新（追加式，不吃 prompt cache）。
3. **唯一硬限制：同一轮拿不到工具**。工具在**下一轮开头**才进快照，所以人设必须是
   「起 jar → 探测就绪 → 结束本回合」，由下一轮（通常是 Inbox 唤醒）继续。`/reload-mcp` 只是
   「跳过连接冷却、立刻重连」的加速键，不是必需路径。

---

## 2. 拓扑事实（取证）

`bot/config/bot/bot.json`（运行配置，非源码）：

| 端口 | 路径 | 谁用 |
|---|---|---|
| `127.0.0.1:8765` | `/mcp/bot` | iiBot 业务 MCP（`qq_*`），Hermes `mcp_servers.iibot` |
| `127.0.0.1:46500` | （HTTP API） | InboxMosire adapter 只读面，`inbox-wake.settings.apiUrl` |
| `127.0.0.1:46501` | `/mcp` | InboxMosire MCP（`inbox_*`），Hermes `mcp_servers.inbox-mosire` |

- 三个端口由**同一个** iibot-app 进程提供（Inbox 内嵌：`InboxMosire/inbox-embedded/src/main/java/io/mosire/inbox/embedded/EmbeddedInboxServer.java`，
  `EmbeddedInboxOptions.java:89` 默认 `mcpPath="/mcp"`）。
- 启动器 `bot/start-bot.sh`：有 systemd --user 时 `systemd-run --user --unit=iibot --collect
  --property=Restart=always --property=RestartSec=5 --property=WorkingDirectory=$PWD`（崩溃 5 秒自愈）；
  无 systemd 回退 `setsid nohup run/boot-env.sh >> run/console.log 2>&1 &`。子命令 `start|stop|restart|status|journal`。
- `bot/run/boot-env.sh`：`set -a; . ~/.config/iibot/llm.env; . ~/.config/iibot/napcat.env; . ~/.config/iibot/control.env; set +a`，
  然后 `exec /usr/bin/java -jar iibot-app-0.1.0-SNAPSHOT.jar --config config/bot/bot.json`。
  **凭据只从这三个 env 文件进**（工作区红线：凭据不进上下文/日志/提交），因此 **禁止裸 `java -jar`**。
- 外部依赖：**NapCat**（`bot.json` 的 `accounts.main.oneBot.eventWebSocketUri` / `httpBaseUri`）是独立服务；
  它不起，iiBot 仍会起、MCP 与 Inbox 都通，但**收不到 QQ 消息**。
- Hermes 侧凭据：`~/.hermes/.env`（`0600`）的 `MCP_IIBOT_API_KEY` / `MCP_INBOX_MOSIRE_API_KEY`，
  由 `config.yaml` 的 `${VAR}` 引用，经 `get_secret` 插值（`tools/mcp_tool_discovery.py:131`）。

---

## 3. 宿主机制（决定"不用重启 gateway"）

| 机制 | 证据 | 语义 |
|---|---|---|
| **MCP config reconcile**（drift 重连） | `gateway/run.py:4528` `(1, "MCP config reconcile", _mcp_config_reconciler(runner))` → `gateway/run_profile_reconcile.py:613` | 每个 housekeeping tick（默认 60s）调 `reconcile_mcp_servers_with_config()`（`tools/mcp_tool_discovery.py:661`）。docstring 明写「Reconciling on **DRIFT** rather than only on a config EDIT is what brings back a server whose FIRST connect failed (#112445)」 |
| **连接冷却** | `tools/mcp_tool.py:499` `_CONNECT_RETRY_BASE_BACKOFF_SEC, _CONNECT_RETRY_MAX_BACKOFF_SEC = 30.0, 600.0` | 失败次数 n → `min(30·2^(n-1), 600)` 秒；冷却中 `_awaiting_connect` 跳过（`tools/mcp_tool_discovery.py:706-711`） |
| **清冷却** | `tools/mcp_tool_lifecycle.py:113-122` `_clear_connect_cooldowns` | `shutdown_mcp_servers()` 会清，所以 `/reload-mcp` 能立刻重连 |
| **between-turns 工具刷新** | `agent/turn_context.py:556-573`（`_refresh_mcp_tools_between_turns`），调用点 `:1138` | 每轮开头、首次 API 调用组装 `tools=` 之前，把晚连上的 MCP 工具**追加**进本回合快照；`preserve_prefix=True` → **不破坏 provider prompt cache 前缀** |
| **`/reload-mcp`** | `gateway/slash_commands.py:1088-1115`；TUI 侧 `cli.py:1133` → `hermes_cli/cli_info_mixin.py:1007` | 重建工具面，**不重启 gateway**；代价=prompt cache 失效一次（`tools/slash_confirm.py:3`）。确认框可关：`approvals.mcp_reload_confirm: false` |
| **TUI 首轮前晚连接兜底** | `tui_gateway/server.py:1574-1605` `_schedule_mcp_late_refresh` | 只在**首轮之前**自动刷新；开过轮次后靠上面的 between-turns 刷新 |
| **CLI 侧 config 变更自动 reload** | `hermes_cli/cli_info_mixin.py:854-900` `_check_config_mcp_changes` | `mcp.auto_reload_on_config_change` 默认 true，检测到 `mcp_servers` 变化就静默 `_reload_mcp()`。**会 invalidate prompt cache**：安装脚本/插件若要写 `config.yaml`，应把它设成 `false` |

> 因此「先起 Hermes、后起 jar」的正确预期是：最迟 **一个 housekeeping tick（≤60s，冷却已涨到 600s 时最坏 10 分钟）**
> + **一个回合**，工具就位；期间不需要任何人工命令，也不需要重启 gateway。

---

## 4. 生命周期（目标形态）

```
t0  gateway 起（iiBot 不在）        → 启动发现失败，进入 30s 起的冷却阶梯
t1  首轮消息到                        → 人设生效；agent 发现 qq_conversation_list 不存在
t2  agent: ./start-bot.sh start       → 46500 / 8765 / 46501 起来（≤30s 内可达）
t3  agent: curl 127.0.0.1:46500/v1/status → 200，结束本回合（不等待工具）
t4  下一次 housekeeping tick          → reconcile 重连成功，工具进 registry
t5  下一轮（多半是 Inbox 唤醒）        → between-turns 刷新把 qq_*/inbox_* 追加进快照 → 正常干活
```

配套守卫（本仓库负责）：**必需程序工具缺失时不要注入唤醒**，记 `wake.skipped reason=program-tools-missing`
（见 §5 与 `plugins/inbox-wake/lib/binding.py` 的 `_program_tools_state`）。否则 t3→t5 之间会出现
「唤醒到了、它却调不了 `qq_conversation_read`」的假故障。

Inbox 侧不需要重启 Hermes：adapter 的 SSE/对账轮询会自行重连（`plugins/inbox-wake/lib/binding.py` 的
`_ensure_poll`/`_poll_loop`、`lib/sse.py`）；等待期内的消息留在 iiBot 的 Inbox 里，不丢。

---

## 4.1 「第一推动力」只能是外部输入（定调 m00468 / m00614）

`programToolsRequired=true`（默认）时，**程序 MCP 工具不在位就不唤醒**（`wake.skipped
reason=program-tools-missing`）。这条守卫和「靠 Inbox 唤醒触发自启动」在冷启动时**互斥**：
jar 没起 → 46500/46501 不存在 → 没有 Inbox 事件；就算有事件，`qq_*` 也没注册进来。

所以第一推动力只有两种：

- **A（当前形态）**：人在会话里说一句（「起 bot」/「看一下消息」）。人设里的自启动段让模型
  发现 `qq_conversation_list` 缺失 → 跑 `./start-bot.sh start` → 探测端口 → **结束本回合**
  （新工具只在下一轮开头的快照里出现）。
- **B（未验证）**：宿主侧的定时器/外部 cron 在 gateway 里先产生一个 turn。宿主的
  `pre_gateway_dispatch` / cron 槽位存在，但「无人说话也起 turn」这条路径**没有取证**。

不推荐 C（把 `programToolsRequired` 关掉换取「先唤醒再自救」）：那会退化成「唤醒到了却调不了
`qq_conversation_read`」，在 ops log 里制造一堆假故障。

补充：即使将来 Inbox 改成独立进程先于 iiBot 起来，「哪条先来哪条带契约」也不需要特殊处理——
注入判据是「对话表面有没有当前 persona hash」，与第几轮无关（`persona.py:has_contract()`）。

## 5. 人设片段（建议；**未落到 `bot/PERSONA.md`**）

> 工作区约定：`bot/` 是运行目录，本仓库不改它的源码（`AGENTS.md` §5）。以下文本由操作者决定是否粘贴
> 到 `bot/PERSONA.md`；本仓库只负责记录与验收。

```md
- 若 `qq_conversation_list` 工具不存在：执行
  `cd /home/cna/DevMosire-iiBot/bot && ./start-bot.sh start`，
  再用 `curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:46500/v1/status` 确认（最多等 30 秒）。
  确认后**本回合结束**，不要在同一回合里等待工具出现；下一轮再干活。
- 禁止裸 `java -jar` 启动 iiBot：凭据只经 `run/boot-env.sh` 从 `~/.config/iibot/*.env` 注入。
- 若 30 秒内 46500 仍不可达：读 `bot/run/console.log` 末 50 行报告失败原因，不要反复重启。
```

---

## 6. 验收（`未验证`；取证脚本 `scripts/acceptance-coldstart.sh`）

`./scripts/acceptance-coldstart.sh probe|stop|start|watch|all` 采集端口、`/v1/status`、
两个 MCP 端点的握手结果，并把会话侧的 N1/N4 断言留给人工（脚本不碰 Hermes 进程）。

| 编号 | 断言 | 状态 |
|---|---|---|
| N1 | 只停 iiBot、gateway 常驻：`qq_conversation_list` 不在工具面，`health.programMcp == "missing"` | 未验证 |
| N2 | 同上状态下，persona-mode 仍注入契约（`<persona-contract>` + hash），agent 能执行 bash | 未验证 |
| N3 | agent 跑 `./start-bot.sh start` 后 30s 内：46500 `/v1/status` 200，8765/46501 在听 | 未验证 |
| N4 | **不重启 gateway**、不发 `/reload-mcp`：≤1 个 tick + 1 回合后 `qq_*`/`inbox_*` 进工具面，`health.programMcp == "ready"` | 未验证 |
| N5 | 等待期内推一条 QQ 消息：工具就位后收到一次唤醒，agent 调 `qq_conversation_read` → `qq_conversation_ack`，QQ 侧收到回复；期间无 `wake.ok` 空转 | 未验证 |
| N6 | 必需工具缺失时唤醒被抑制：ops log 出现 `wake.skipped reason=program-tools-missing`，且 `lastWokenRevision` 未推进 | ✅ 单测：`tests/test_inbox_binding.py::ProgramToolGuardTests`（7 例，2026-11-03 落地） |

---

## 7. 与 DSH 的差异

DSH 侧没有「MCP 作为 per-session 策略代际」这个概念，也没有 drift reconcile / between-turns 追加刷新。
本能力是 **Hermes 宿主独有**，已在 `docs/DSH-PARITY.md` §5.2 登记。

## 8. 已知风险

- **冷却阶梯**是硬编码的（30s→600s）。Hermes 空转很久后首次重连可能要等约 10 分钟；此时 `/reload-mcp`
  （清冷却）或重启 gateway 才是快速路径。
- **`bot.json` 里的端口**属于运行配置：换了端口要同步改 `~/.hermes/config.yaml` 的 `mcp_servers` 与
  `inbox-wake.settings.apiUrl`，本仓库无从校验。
- **NapCat 不在自举范围内**：本 doc 只保证「iiBot 起来且 MCP/Inbox 可达」，不保证 QQ 侧在线。
- 若将来给 iiBot 加开机自启（systemd unit 常驻），本流程退化为「探测即可」，人设里的启动段仍可保留作兜底。
