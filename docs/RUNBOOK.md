# RUNBOOK：部署 / 启动 / 停止 / 排障

面向"把 iibot-hermes（0.4.0 三合一：唤醒 + prompt + persona）装到另一台机器（含远程服务器）"的操作手册。

> 0.4.0 起插件只有一个：`plugins/iibot-hermes`，设置是
> `plugins.entries.iibot-hermes.settings.{inbox,prompt,persona}.*`，`allow_gateway_injection` 在
> `plugins.entries.iibot-hermes.allow_gateway_injection`。文中出现的旧 id / 旧路径是 0.x 的写法，
> 装了 0.4.0 就用新路径（映射见 `docs/CHANGELOG.md` 的 0.4.0 条目）。
**先读这三条事实，否则一定会踩坑：**

1. 插件的 hooks、4 个 `inbox_adapter_*` 工具、`/inbox` 命令**都运行在 gateway 进程里**
   （`hermes_cli/gateway_plugin_commands.py` 顶部注释：*"the plugin's hooks, tools and MCP servers live in
   the gateway, so its command runs there too … this process never imports the plugin to run it."*）。
   ⇒ **改插件代码或 `plugins.entries.*` 设置后必须重启 gateway**，只重开 CLI 无效。
2. CLI（`hermes chat` / `hermes -z`）只是 gateway 的客户端；gateway 没起时 CLI 会以 `--idle-exit` 方式**临时**拉起一个，
   空闲即退出。生产要唤醒能力就必须让 gateway 常驻。
3. token 只能存在于 0600 文件里。禁止 `set -x`、禁止 `cat` token 到终端/日志/工单。

---

## 0. 前置条件

| 项 | 要求 | 校验 |
|---|---|---|
| 宿主 | Hermes Agent（本仓验证于 `v0.22.5+3377.g0aab6dc`，Python 3.14.9） | `hermes --version` |
| 插件运行时 | 纯 stdlib，无需第三方包 | `python3 -m compileall -q plugins` |
| 后端 | InboxMosire 胖包（Java）或任意实现同一 HTTP/SSE 契约的服务 | `curl -s <api>/healthz` |
| 权限 | `$HERMES_HOME` 当前用户可写（本机 `/home/cna/.hermes`） | `hermes plugins list --user --plain` |
| 网络 | 默认全回环（API 18765 / MCP 18766）；跨机部署见 §6 | — |

后端契约（插件只发这些，只读 + 占位）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 探活 |
| GET | `/v1/inboxes/{inbox}/status` | revision / available |
| GET | `/v1/inboxes/{inbox}/items?limit=&channel=` | 摘要（最多 3 条） |
| GET | `/v1/inboxes/{inbox}/items/{itemId}` | 正文（`wakeContent=bounded` 时） |
| GET | `/v1/inboxes/{inbox}/events` | SSE（`Last-Event-ID` 回放；**建议每 10–15s 发 `: ping\n\n`**） |
| PUT/DELETE | `/v1/inboxes/{inbox}/occupancy` | 会话占位租约（`{"sessionRef","leaseSeconds"}`） |

> 插件**从不**调用 claim / complete / renew / release / push —— 那些属于 agent 侧的程序 MCP。

---

## 1. 部署（三选一）

### A. 仓库脚本（最快，推荐首次）

```bash
cd InboxMosire-Hermes
export INBOX_TOKEN="<从后端签发、只用于本机的 token>"     # 不要 echo 它
./scripts/install.sh --local .                          # 复制到 $HERMES_HOME/plugins、enable、写 settings、建 token 文件(0600)、doctor
hermes gateway stop && hermes gateway run                 # 见 §3 的停止注意事项
```

脚本会把 token 落到 `$HERMES_HOME/inbox-mosire/api-token`（0600）。自检阶段连不上后端是**预期**。

### B. 手动（要精确控制时）

```bash
H="$HOME/.hermes"
# 先删 0.x 的三个目录：留着会同进程双重注册
rm -rf "$H/plugins/inbox-wake" "$H/plugins/minimal-system" "$H/plugins/persona-mode"
cp -r plugins/iibot-hermes "$H/plugins/"
hermes plugins enable iibot-hermes

# —— inbox 设置（键名驼峰，值与 config.yaml 一致）——
S=plugins.entries.iibot-hermes.settings.inbox
hermes config set $S.enabled true
hermes config set $S.apiUrl http://127.0.0.1:18765
hermes config set $S.inboxId main
hermes config set $S.adapterTokenFile "file:$H/inbox-mosire/api-token"
hermes config set $S.wakeMode claim                 # claim | view-ack
hermes config set $S.wakeProfile generic            # generic | iibot（iibot 目前等同 generic 文本）
hermes config set $S.autoBind true                  # 会话首轮自动绑定
hermes config set $S.pollSeconds 30                 # 后端不发心跳时的对账兜底；0=关
hermes config set $S.sseReadTimeoutSeconds 120      # SSE 自己的读超时，别跟 requestTimeoutMs 混
hermes config set plugins.entries.iibot-hermes.allow_gateway_injection true   # 一级 entry 键，默认 false
chmod 600 "$H/config.yaml"
```

其余可选项：`channels`、`label`、`summaryEnabled`、`summaryMaxItems`(1–3)、`summaryMaxTitleChars`(8–200)、
`summaryMaxTotalChars`(120–8000)、`wakeContent`、`wakeContentMaxChars`、`debounceMs`(默认 300)、
`heartbeatTimeoutSeconds`（= `sseReadTimeoutSeconds` 的缺省）、`requestTimeoutMs`、`maxBindings`(≤32)。
完整表见 `docs/TOOLS.md`。

### C. kit（连 InboxMosire 一起拉起）

```bash
cd InboxMosire-Hermes
cp kit/kit.env.example kit/kit.env && vi kit/kit.env    # jars 目录、INBOX_DATA、token 路径
./kit.sh doctor        # 只读体检
./kit.sh up            # 起 inboxd → 签两个最小权限 token → 挂 mcp_servers.inbox → 写 settings → 重启 gateway
./kit.sh status
```

`up` 之后 agent 只需对话：「读 `kit/引导.md`，按里面把 inbox 起来并汇报状态」。

---

## 2. 启动

### 2.1 后端（InboxMosire）

```bash
JAR=/path/to/inbox-server-0.1.0-SNAPSHOT-all.jar
java -jar "$JAR" init  --data-dir /var/lib/inbox                 # 首次
java -jar "$JAR" token issue --data-dir /var/lib/inbox --label wake \
     --scope status:read --scope items:read --scope events:read --scope inbox:occupy --inbox main
nohup java -jar "$JAR" serve --data-dir /var/lib/inbox \
      --api-host 127.0.0.1 --api-port 18765 \
      --mcp-host 127.0.0.1 --mcp-port 18766 --mcp-path /mcp \
      > run/inbox.log 2>&1 & echo $! > run/inbox.pid
```

`token issue` 只在签发时打印一次 token，把它重定向进 0600 文件，别让它进 shell 历史：

```bash
umask 077; java -jar "$JAR" token issue ... | sed -n 's/^token=//p' > "$HERMES_HOME/inbox-mosire/api-token"
chmod 600 "$HERMES_HOME/inbox-mosire/api-token"
```

### 2.2 gateway（三档）

```bash
hermes gateway run                                   # 前台（Ctrl+C 停），WSL/调试用
nohup hermes gateway run >> ~/.hermes/logs/gateway.log 2>&1 &   # 常驻（手工）
hermes gateway ensure --timeout 90                   # 幂等：已 ready 立刻 0；没起就拉起并等 ready
```

`ensure` 退出码：`0` ready / `2` 参数错 / `3` 不兼容 / `4` 授权档位不符 / `5` 超时 / `6` draining / `7` 监督者冲突。
`hermes gateway install --start-on-login --start-now`（systemd --user / 系统级）**尚未在本机验证（NOT_RUN）**。

### 2.3 挂载到会话

- 推荐：`autoBind: true` → 会话**第一轮**自动绑定（日志：`inbox-wake: autoBind 已绑定会话 <key>`（子模块日志前缀保留））
- 手动：会话内 `/inbox bind <inboxId>` 或工具 `inbox_adapter_bind`
- 程序 MCP（claim/complete 需要）：`hermes config set mcp_servers.inbox.url http://127.0.0.1:18766/mcp` +
  `hermes config set mcp_servers.inbox.headers.Authorization "Bearer <program token>"`（用 `hermes mcp test inbox` 验证应见 12 个工具）

---

## 3. 停止（由浅入深四档）

| 档 | 目的 | 命令 | 是否要重启 gateway |
|---|---|---|---|
| 1 | 只取消某个会话的唤醒 | `/inbox unbind`（或 `inbox_adapter_unbind`） | 否 |
| 2 | 停用插件（保留安装与设置） | `hermes plugins disable iibot-hermes` | 是 |
| 3 | 停 gateway（进程级全停） | `timeout 120 hermes gateway stop` | — |
| 4 | 停后端 | `kill "$(cat run/inbox.pid)"` 或 `./kit.sh down` | 否 |

**坑（本机实测）**：`hermes gateway stop` 有活跃会话时会 drain，最长 ~120s；必须轮询到不再是 running 再启动：

```bash
timeout 120 hermes gateway stop
until ! hermes gateway status 2>/dev/null | grep -q "Gateway is running"; do sleep 2; done
nohup hermes gateway run >> ~/.hermes/logs/gateway.log 2>&1 &
```

否则新进程只打印 `✓ The host gateway already serves profile 'default' — nothing to start.` 然后退出（看起来"起不来"）。
另：本机 `hermes gateway restart` 会挂住（WSL），用上面的 stop+run 代替。

**停用前建议**：`state.json` 里的绑定会在 gateway 启动时被 `resume_all()` 自动恢复；不再需要时先 unbind 或清空 `bindings`，
否则会对着已停的后端空转重连（`sse.retry … heartbeat timeout`）。

---

## 4. 升级 / 回滚

`plugins/iibot-hermes`（0.4.0 三合一）是纯 Python，升级 = 换目录 + 让进程重新 `register(ctx)`，
不涉及数据迁移。**替换前必须删掉 0.x 的三个旧目录**，否则同进程里新旧插件各注册一遍

### 4.1 一条命令（本地或远端 checkout）

```bash
cd <仓库>
git pull                       # 或 git checkout <tag/commit>
./scripts/sync-plugins.sh --dry-run   # 先看：已装版本 → 源版本、以及哪些文件会变
./scripts/sync-plugins.sh             # 整目录替换（先删后拷）+ 清 __pycache__
./scripts/sync-plugins.sh --restart   # 复制后停+起 gateway（远端常用）
```

脚本只动 `$HERMES_HOME/plugins/<id>`，不碰 settings、`plugin-data`、token；它会先删
`{inbox-wake,minimal-system,persona-mode}` 再复制 `iibot-hermes`。
`--only iibot-hermes` 可只同步这一个，`--target /home/ops/.hermes` 可指定别人的 HOME。

### 4.2 从开发机推到远端（远端不 checkout 时）

```bash
rsync -a --delete \
  --exclude __pycache__ \
  plugins/iibot-hermes \
  ops@host:.hermes/plugins/
ssh ops@host 'rm -rf ~/.hermes/plugins/{inbox-wake,minimal-system,persona-mode}'   # 旧目录必须删
ssh ops@host 'hermes plugins doctor iibot-hermes'
```

体积很小（20–200 KB），不需要打 tar。

### 4.3 升级后必须做的一步

插件代码与 hook 只在 `register(ctx)` 时生效：

- 常驻 gateway：重启 gateway（`sync-plugins.sh --restart`，或 §3 的 stop + run）；
  `hermes plugins enable <id>` 也能让 gateway 跑 `reload-plugins` 重挂 hook，
  但 `tools` 与 `system_prompt_section` 按 prompt-cache 不变量延后到下一个 session。
- 一次性会话：下次启动自然加载。
- 新增/升级插件（0.4.0 只有一个 `iibot-hermes`）：先确认 `~/.hermes/config.yaml` 的
  `plugins.enabled` 里有它，否则装进目录也不会加载。
- `settings.prompt.systemPromptFile` 的**内容**是热更新的（按 mtime 重读，无需重启；需 `prompt.enabled: true`）；
  **路径**（或 `systemPrompt`）改了要重启 gateway。默认路径是 `$HERMES_HOME/SYSTEM.md`，
  所以把 md 放那儿、什么都不配也能生效（文件不存在则回落 `systemPrompt`，不会报错）。

### 4.4 宿主补丁（只有打过极简 system 时才需要）

```bash
./scripts/patch-hermes.sh check   # 0=已应用 / 1=可打 / 3=宿主源码变了需人工
./scripts/patch-hermes.sh apply   # 依赖 patches/0001-host-system-prompt-override.patch
```

宿主升级后补丁通常会被覆盖，`check` 返回 1 就重新 `apply`；返回 3 说明上游改了同一区域，
要人工看。

### 4.5 回滚

```bash
hermes plugins disable iibot-hermes
git checkout <上一个 commit>
./scripts/sync-plugins.sh --restart
hermes plugins enable iibot-hermes
```

状态备份：`~/.hermes/plugin-data/agent-plugin-iibot-hermes-*/state.json`（0600，含 bindings / ops 事件流）。
0.x 的状态在 `~/.hermes/plugin-data/{inbox-wake,persona-mode}/`，**不会**自动迁移（绑定表可重建）。
旧目录不会被脚本保留，需要留档就先 `cp -a ~/.hermes/plugins/iibot-hermes /tmp/iibot-hermes.<rev>`。

---

## 5. 部署后验证清单

| # | 检查 | 命令 | 期望 |
|---|---|---|---|
| 1 | 静态 | `python3 -m compileall -q plugins` | 无输出 |
| 2 | 单测 | `python3 -m unittest discover -s tests` | `Ran N tests OK`（当前 231） |
| 3 | manifest | `hermes plugins validate plugins/iibot-hermes --json` | `"ok": true` |
| 4 | 注册 | `hermes plugins doctor iibot-hermes` | `4 tool(s), 7 hook(s)`（prompt/persona 关时只注册 wake 面；persona 打开后 9）。`WARN: pre_tool_call` 是预期 |
| 5 | 加载 | `grep -a "iibot-hermes: effective settings" ~/.hermes/logs/agent.log \| tail -1` | 含 apiUrl / inboxId / wakeMode / pushMode / prompt.enabled / persona.enabled |
| 6 | 设置 | `hermes config get plugins.entries.iibot-hermes.settings` | inbox/prompt/persona 三段关键值正确 |
| 7 | token | `stat -c '%a %s' <token file>` | `600`，非空 |
| 8 | 后端 | `curl -s <api>/healthz` + `/v1/inboxes/<id>/status` | 200 / `revision` 有值 |
| 9 | MCP | `hermes mcp test inbox` | `Tools discovered: 12` |
| 10 | 会话内 | `/inbox status` | `sse: open|connecting`，`observed` 递增 |
| 11 | 唤醒 | 后端推一条 → `/inbox log` | `wake.ok`；gateway.log `Plugin message injection admitted` |

---

## 6. 远程部署注意

1. **别裸奔**：`serve --api-host 0.0.0.0` 只在有反代/TLS/防火墙时用；默认全回环。
2. **token 最小权限**：wake token 只需 `status:read,items:read,events:read,inbox:occupy`；
   程序 MCP token 才需要 `items:read,items:manage`。两者分开、别跨机复用。
3. **gateway 要常驻**：用 systemd/supervisor 跑 `hermes gateway run`（`--idle-exit` 的临时进程不适合生产）。
4. **日志**：宿主写的 `~/.hermes/logs/*.log` 是 `0644`、`plugin-data/*` 是 `0755`（已知 F6 未修）；
   有合规要求时用 umask/ACL 收口。
5. **时间同步**：revision/lease/退避都依赖本机时钟。
6. **升级窗口**：gateway 停止会 drain 进行中的 turn；选低峰期，或先 `/inbox unbind` 再升。

---

## 7. 排障表

| 症状 | 先看 | 处理 |
|---|---|---|
| `/inbox` 报 Unknown command / 无输出 | `grep -a "inbox-wake: loaded" ~/.hermes/logs/agent.log` | 插件没加载：`hermes plugins enable iibot-hermes` + 重启 gateway |
| 同一条 push 被注入两次 | `ls ~/.hermes/plugins` | 旧目录 `{inbox-wake,minimal-system,persona-mode}` 没删，新旧插件双重注册 → `rm -rf` 后重启 gateway |
| `绑定失败：TOKEN_MISSING 未配置 adapter token 引用` | `hermes config get plugins.entries.iibot-hermes.settings.inbox` | 多为**旧 gateway 进程配置陈旧**：重启 gateway |
| `TOKEN_FILE_PERMISSIONS` | `stat -c %a <file>` | `chmod 600` |
| `TOKEN_UNREADABLE` | 文件是否存在、当前用户可读 | 修路径/权限 |
| `sse: retrying`，ops 里 `detail: heartbeat timeout` 每 8–10s 一条 | 后端是否发 `: ping` | 后端加心跳；或 `pollSeconds=30~60` 兜底 + 调大 `sseReadTimeoutSeconds` |
| 收不到唤醒 | `allow_gateway_injection`；gateway.log 有没有 `injection admitted` | 开开关；`not routed` 表示会话已死，删绑定 |
| 唤醒太频繁 | `/inbox log` 的 `wake.ok` 间隔 | 调大 `debounceMs`、用 `channels` 过滤、`restSeconds` 静默 |
| `programMcp: missing` | `hermes mcp test inbox` | 挂 `mcp_servers.inbox`（见 §2.3） |
| 一个事件叫醒好几个会话 / 机器上突然跑了多个 agent | `python3 -c` 读 `state.json` 看 `bindings` 条数与 `createdAt` | 僵尸绑定：每次一次性/CLI 会话 autoBind 都会留一条。设 `maxBindingsPerInbox`（默认 1）后重启 gateway 即自动裁剪；手工清：停 gateway 后把 `bindings` 置 `{}`（先备份），下次会话 autoBind 只建一条 |
| `/inbox log` 里 `wake.deferred reason=no-live-session` | `gateway.log` 的 `is not a live local session of the plugin's profile home …` | 目标 local 会话此刻不活跃（经典 REPL 只在 turn 运行中才是 live session）。插件按 `wakeRetrySeconds` 固定重试，不烧退避；要**空闲时**被唤醒，见 §7.2 |
| `status` 里 `lastStatus.error` 是 `INBOX_AUTH: 401` / 唤醒一直不触发（换过 data 目录、重启过 iiBot） | 看 `bot2/data/inbox/binding-descriptor.json` 的 `adapterTokenRef` | iiBot **每次启动都重签** `dsh-wake.token`，旧值立刻失效。0.2.4 起插件 401 会自动重读文件并重放一次；仍失败说明配置指向了别的实例的 data 目录，把 `adapterTokenFile` 指到当前实例，或让插件按同目录描述文件自动接管 |
| Agent 自己写脚本直连多模态才能看图（`vision_analyze` 也拿不到像素） | `./scripts/check-vision.py` | 同一个根因：宿主没声明视觉能力，原生附图链路整条是关的。补 `hermes config set model.supports_vision true` 并重启 gateway，别再让 Agent 维护 `img_read.py` 这类绕过脚本 |
| Bot 说「我读不了图」/ 调了 `qq_media_get` 却只看到 `MEDIA:/…` 路径 | `hermes config get model.supports_vision` | 宿主目录查不到 `custom:newapi` + `deepseek-flash` 的视觉能力 → 路由判定为 `text`，MCP `ImageContent` 只留 `MEDIA:` 路径。设 `model.supports_vision: true` 后重启 gateway（实测该 route 能读 `role=tool` 里的图文块列表）|
| 每轮刷 `config_keys entry 'x' must be a dotted path at least two levels deep; skipping` | — | 已知 manifest 缺陷（裸驼峰键），不影响 settings 生效 |
| 会话卡住/一直转圈 | `~/.hermes/logs/gateway.log` 里 `APIConnectionError` / 长耗时工具 | 见 §7.1 |

### 7.2 谁能承接"空闲时被唤醒"（宿主差异，实测）

`inject_message` 的可投递性由**宿主**决定，与配置无关：

| 会话起法 | session key 形态 | 空闲时能收唤醒吗 | 原因 |
|---|---|---|---|
| `hermes chat --cli`（经典 REPL） | `agent:<profile>:local:dm:local-<hash>` | **不能**，只有该会话正在跑 turn 时才能 | gateway 只把 wake 投给 `authority.sessions[chat_id]` 里的 *live* local session（`gateway/run_plugin_injection.py:_plugin_local_session`），空闲 REPL 不在里面 |
| `hermes --tui` / 桌面 / web | 同上 | **能** | TUI/desktop 自己发布 injector（`tui_gateway/plugin_inject.py:install_tui_message_injector`），按 `session_key` 认领，空闲时会把 wake 排进该会话的 prompt 队列并起一轮 |
| api_server（`/v1/chat/completions` + `X-Hermes-Session-Key`） | `agent:<profile>:api_server:dm:api-<hash>` | **能** | 会话由 gateway 自己持有；本机实测可绑定并收 wake |
| 常驻 messaging 平台（telegram/qq…） | `agent:<profile>:telegram:dm:<chat>` | **能** | 同上，走 adapter 投递 |

其它宿主的拒收不会污染状态：local 会话的拒收记 `wake.deferred`（固定 `wakeRetrySeconds` 重试，不动 `attempts`/退避），其它会话的失败仍是 `wake.failed`（30s→900s 退避）。

### 7.1 「卡住」的三个常见来源（本机实测）

1. **LLM 请求被打包代理打断**：装了 `billion-context` 之类的代理插件时，日志出现
   `APIConnectionError` + `Detected closed shared OpenAI client; recreated` → 先 `hermes plugins disable billion-context` 复测。
2. **超长工具调用**：`Tool terminal returned error (249.28s): {"output": "[Command interrupted]"}` —— 不是模型卡死，是工具在跑。
3. **唤醒风暴**：`sse.retry` 高频 + 后端 revision 频繁递增 → 按 §7 的心跳/去抖方案处理。

---

## 7.3 只有一个会话收事件（归属 / ownership）

同一个 `(apiUrl, inboxId)` 上只应有一条**活跃**绑定，否则一条事件会有 N 个会话各跑一轮。

| 谁决定的 | 结果 |
|---|---|
| `/inbox bind <inboxId>`（会话内显式执行） | `source=manual`，**钉住**；同 inbox 其它绑定被降级为 `passive (superseded)` |
| `autoBind`（settings 里 `autoBind=true`） | `source=auto`；已有 manual 持有者时自己变 `passive (manual-holder)`，不抢事件 |
| 重启后 `resume_all()` | 先按 `maxBindingsPerInbox` 裁剪（manual 优先，其次最新），再启动；`passive` 不订阅、不唤醒（`wake.skipped reason=passive-binding`） |

查看与收拾：

```bash
# 会话内
/inbox owners         # 活跃 N 条 / 上限 1；列出每条绑定的 sessionKey、source、host、passive 原因
/inbox unbind <sessionKey>   # 把不需要的那条删掉
/inbox status         # active : yes|passive (reason) source=manual|auto host=tui|gateway|…
```

`/inbox owners` 输出里的长 `sessionKey`（`agent:main:…:local-<hash>`）就是 `/inbox unbind` 的参数。

## 8. 未验证项（NOT_RUN）

- `hermes gateway install`（systemd --user 单元、开机自启）——本机只验证了 `stop / run / ensure / status`。
- `hermes gateway restart`：本机 WSL 上会挂住，一律用 stop+run 代替。
- 远程（跨机 + TLS 反代）部署、多 inbox 并发、3 天稳定性压测。
- 后端不发心跳时 `pollSeconds` 与 `sseReadTimeoutSeconds` 的长期最优组合。
