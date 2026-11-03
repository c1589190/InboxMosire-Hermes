# CHANGELOG

按主题归纳，不追每个小修。日期均为本机真机轮次（2026-11-02）。
宿主环境：Hermes `v0.22.5+3377.g0aab6dc`（2026.9.24）/ Python 3.14.9 / 模型 `deepseek-flash`。

## 未发布（HEAD）

### 新增
- `kit/`：一键发行包 —— `kit.sh`（`doctor|up|status|down|probe|token wake|program`）、`kit.env.example`、`引导.md`。`up` 一条命令完成：初始化数据目录 → 起 inboxd（API 18765 / MCP 18766）→ 签发两个最小权限 token（`0600`，只回显 credential_id）→ 挂 `mcp_servers` → 写插件设置 → 重启 gateway。
- `autoBind`：会话首轮自动绑定（失败只写 warning，绝不打断 turn；同一会话只尝试一次）。
- `pollSeconds`：对账轮询，唤醒正确性不再依赖 SSE 是否发心跳。
- `sseReadTimeoutSeconds`：SSE 自己的读超时（旧版复用了 `requestTimeoutMs`=10s）。
- `wakeRetrySeconds`：本地会话被宿主拒收时的**固定**重试间隔（默认 15s），不进指数退避。
- `docs/RUNBOOK.md`：部署 / 启动 / 停止 / 升级回滚 / 验证清单 / 远程部署注意 / 排障表 + 宿主差异表。
- `docs/CHANGELOG.md`（本文件）、`README.md` 重写。

### 冷启动自举 + 程序工具守卫（2026-11-03）

- **背景**：用户要求「只起 Hermes，由 agent 按人设把 iiBot 拉起来」，日常是 TUI + 自动 gateway，
  要求「仅仅重新加载 MCP」、不重启 gateway。宿主侧结论（自动 drift reconcile + 每轮 between-turns 刷新，
  无需重启也无需发命令）归档在 `docs/COLDSTART.md`。
- **`binding.py`**：新增 `_program_tools_state()` / `_program_tool_requirement()` —— 按 `wakeProfile`
  选工具组（`generic`→`inbox_list`；`iibot`→`qq_conversation_read`），`recommended`
  （`qq_conversation_ack`/`qq_media_get`）缺失只降级 `partial`；**缺少 required 时不注入唤醒**，
  记 `wake.skipped reason=program-tools-missing`，且**不推进** `lastWokenRevision`，
  工具就位后同一 revision 补发。宿主进程里没有 `tools.registry` 时 fail open（`unknown` 照常唤醒）。
  这修掉了「唤醒到了却调不了 `qq_conversation_read`」的假故障（先起 Hermes、后起 iiBot 的必经路径）。
- **`status()`**：新增 `health.programTools`（`group/state/required/recommended/missing/missingRecommended`），
  原来的 `health.programMcp` 字符串保留（向后兼容）。
- **配置**：新增 `programTools`（覆盖工具组）、`programToolsRequired`（默认 true）；
  顺带修复 `maxBindingsPerInbox` 从未被 `from_mapping()` 赋值（写 3 也一直是 1）。
- **测试** 103 → 120：新增 `ProgramToolGuardTests` 7 例（缺工具拦截 / 工具就位补发 / 只缺 recommended /
  无 registry fail open / `programTools` 覆盖 / 开关关闭 / status 明细）。

### 归属（ownership）：只让一个会话收事件

- **症状**：重启后"想要的那个会话"继续工作，但别的会话也在被叫醒；一次 inbox 事件 → 多个 agent 各跑一轮。
- **根因**：`autoBind` 和手动绑定没有优先级，`resume_all()` 把所有绑定一起复活；一条事件按各自 `session_key` 注入。
- **修法**：绑定记 `bindSource`（`manual` / `auto`）与 `host`；显式 `/inbox bind` 钉住并降级同 inbox 的旧绑定；
  `autoBind` 遇到 manual 持有者自我降级 `passive (manual-holder)`；`resume_all()` 先裁剪后启动（manual 优先、其次最新）；
  `passive` 绑定不订阅不唤醒；新增 `/inbox owners` 与 `/inbox status` 的 `active` 行；`bind()` 在不能投递的宿主返回 `NO_INJECT_HOST`。

### 修复
- **SSE 事件唤不醒**（阻断）：`BindingManager._log()` 与调用处的 `event=` 关键字撞名 → `_on_event` 抛 TypeError，在 `_schedule()` 之前中断，事件永远不触发唤醒。参数改名 `name`，调用处改传 `kind=`。
- **`on_session_end` 语义误用**（阻断）：该钩子**每轮**触发（`hermes_cli/lifecycle.py:72` "per-turn and stays observer-only"），旧实现把它当会话销毁 → 首轮之后 SSE 永久 suspended。改为按 turn 结束处理，真正的销毁用 `on_session_finalize` / `on_session_reset` / `on_session_delete`；`on_turn_start` 会复活被 suspend 的 worker。
- **重连风暴**：`urllib.request.urlopen(..., timeout=self.timeout_seconds)` 让静默 SSE 每 10s 判死重连，`heartbeatTimeoutSeconds` 在 SSE 路径上从未生效。
- **`plugin.yaml` 的 `config_keys`**：14 个裸驼峰键被宿主全部跳过（`must be a dotted path at least two levels deep; skipping`），改为 25 条 `plugins.entries.inbox-wake.settings.<key>` 点分路径。
- **哑进程污染共享退避**：oneshot / worker 进程也加载插件，`resume_all()` 起的 SSE worker 每来一个事件就注入失败（`inject_message: no live gateway is available`），却对共享 `state.json` 写 30s 退避，把真正该唤醒的 gateway 一起拖住。改为：唤醒前**每次**重新探测宿主能力，非注入宿主只记 `wake.skipped`（同一 revision 只记一次，不动退避、不推进游标）。
- **宿主能力快照过期**：gateway 在插件发现（`gateway/run_startup.py:1113`）之后才安装 message injector（`:1741`），register 时刻快照必然为 `none` → 改为每次唤醒动态求值。
- **TUI/桌面宿主漏判**：能力探测原先只认 gateway / CLI 两路，会把 `--tui` 后端判成 `none` 并跳过唤醒；补 `has_tui_message_injector` → `inject: tui`（路由顺序见 `hermes_cli/plugins_injection.py`）。
- `on_turn_start` 复活 worker 的竞态：`if alive: return` → `if alive and not suspended: return`。

### 文档
- `docs/ACCEPTANCE.md`：新增 K（kit/autoBind）、L（SSE 读超时 / 对账轮询 / 动态探测 / deferred / 宿主差异）、Z（真机验收记录）段。
- `docs/DSH-PARITY.md`：补「Hermes 宿主语义差异」表。
- `docs/TOOLS.md`：配置键表补 `autoBind` / `pollSeconds` / `sseReadTimeoutSeconds` / `wakeRetrySeconds`。

## 已发布

- `cf2b77f` install: token 不再走终端路径
- `7f05d84` feat: persona-mode + inbox-wake 两个插件
- `d5c477b` docs: 设计/工具/宿主能力/安装/验收/DSH 对照
- `34b2944` fix: SSE 事件唤不醒 + `on_session_end` 语义（真机验收）
- `6f3e3d5` feat: `autoBind` + `kit/`
- `452f0b9` fix: SSE 独立读超时 + 对账轮询
- `c9a06f1` docs: RUNBOOK
- `c43e07c` fix: `config_keys` 点分路径
- `879e6dd` fix: 只在能注入的宿主里唤醒
- `56e3f04` fix: 本地会话固定间隔重试 + 动态能力探测
- `1cb14ae` fix: 能力探测识别 TUI/桌面宿主

## 测试

`python3 -m unittest discover -s tests` → **103 passed**（persona 契约/门禁/模板、HTTP 客户端、SSE 解析、
唤醒决策/去重/去抖/退避/deferred、注册接线、宿主能力探测、autoBind）。
