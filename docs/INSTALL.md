# 安装与启用

> 部署到别的机器 / 生产环境的**部署·启动·停止·验证·排障**手册见 [`docs/RUNBOOK.md`](RUNBOOK.md)。

面向的操作者：已经装好 Hermes、打算把这套插件接到本机 InboxMosire 的人。

## 0. 前提

- Hermes 已安装，`$HERMES_HOME`（默认 `~/.hermes`）对**你自己**可写。
  若 `hermes --version` 报 `install state is not writable by this user (…/.lock)`，
  先修 install 目录所有权 —— 否则 `plugins install` / `plugins doctor` 全部跑不动。
- InboxMosire daemon 在跑：`curl -s http://127.0.0.1:18765/healthz`。
- 拿到两枚 token（都是 0600 文件存放，绝不进 config.yaml、日志或聊天）：
  - **adapter token**：本插件用来读 `/status`、`/items`、订阅 `/events`；
  - **producer token**：外部压消息用（可选）。

## 1. 一条命令

```bash
git clone git@github.com:c1589190/InboxMosire-Hermes.git
cd InboxMosire-Hermes
./scripts/install.sh
```

脚本按顺序做四件事，每一步都可单独重跑：

| 动作 | 命令 | 说明 |
| --- | --- | --- |
| 装插件 | `./scripts/install.sh plugins` | **先删** `$HERMES_HOME/plugins/{inbox-wake,minimal-system,persona-mode}`，再 `hermes plugins install "<repo>#plugins/iibot-hermes" --enable`，或 `--local <path>` 从本地 checkout 复制 |
| 写 token | `./scripts/install.sh token` | 交互式读入（不回显）；无终端时用 `INBOX_TOKEN=… ./scripts/install.sh token`；写 `$HERMES_HOME/inbox-mosire/api-token`（0600，目录 0700） |
| 写设置 | `./scripts/install.sh settings` | 全部走 `hermes config set --force`，不手改 YAML |
| 体检 | `./scripts/install.sh check` | `plugins list` + `plugins doctor --ci` + `healthz` |

`systemPromptFile` 指向操作者自己的 md/纯文本文件：**留空就是 `$HERMES_HOME/SYSTEM.md`**
（Hermes 自己的目录，与 `SOUL.md`、`config.yaml` 同级），要放别处再写绝对路径。按 `(mtime, size)` 缓存，
**改文件即时生效、不用重启 gateway**（改路径才需要重启，配置只在插件加载时读一次）。文件缺失、
不可读或为空时静默回落到 `systemPrompt`。内容原样发送——不解析 markdown、不渲染、不剥 front matter，
所以别写时间戳之类每轮都会变的东西（每轮字节必须一致，否则前缀缓存失效）。

环境变量覆盖：`INBOX_API_URL`（默认 `http://127.0.0.1:18765`）、`INBOX_ID`（`main`）、
`WAKE_MODE`（`claim`）、`WAKE_PROFILE`（`generic`）、`PROMPT_ENABLED`（`0`）、`SYSTEM_PROMPT_FILE`
（写入 `plugins.entries.iibot-hermes.settings.prompt.systemPromptFile`，不写则读 `$HERMES_HOME/SYSTEM.md`、
再不行用一句常量）、`PERSONA_ENABLED`（`0`）、`PERSONA_FILE`、`TOKEN_PATH`、`HERMES_BIN`。

0.4.0 起 `prompt.enabled` / `persona.enabled` **默认都是 false**：装完不会接管 system prompt、
也不会注入人设（D-009）。要回到 0.x 的行为，显式 `PROMPT_ENABLED=1 ./scripts/install.sh settings`（或手写
`hermes config set --force plugins.entries.iibot-hermes.settings.prompt.enabled true`）。**不要**同时打开
`prompt.enabled` 与宿主 `agent.system_prompt_override`，两处会互相覆盖。

## 2. 脚本写进 config.yaml 的东西

```yaml
agent:
  system_prompt_override: "You are a helpful software engineer assistant."  # 需宿主补丁，见 docs/HOST-PATCH.md
plugins:
  enabled: [iibot-hermes]                     # 插件默认不加载，必须点名
  entries:
    iibot-hermes:
      allow_gateway_injection: true           # 一级键（不在 settings 下）；不写 true，inject_message 一律返回 False
      settings:
        inbox:                                # 唤醒面（旧 inbox-wake.settings 的全部键都在这里）
          enabled: true
          apiUrl: http://127.0.0.1:18765
          inboxId: main
          adapterTokenFile: file:/home/<you>/.hermes/inbox-mosire/api-token
          wakeMode: claim
          wakeProfile: generic
          pushMode: auto
          pushGraceMs: 2000
          firstRunGuide: true
        prompt:                               # 默认关：system prompt 交给宿主的 agent.system_prompt_override
          enabled: false
          systemPromptFile: ""                # 留空 = $HERMES_HOME/SYSTEM.md；要放别处写绝对路径
                                              # 文件可读时整段替换，优先级高于 systemPrompt
          systemPrompt: "You are a helpful software engineer assistant."  # 回落值；YAML 块标量（|）可写多行
        persona:                              # 默认关（D-009）：人设改由首条 inbox 引导读 AGENTS.md
          enabled: false
          personaFile: /abs/path/to/PERSONA.md  # 不写则读会话工作目录下的 PERSONA.md
```

要点：

- `allow_gateway_injection` 在 **entry 层**（和 `settings` 同级），不在 `settings` 下。0.4.0 起宿主只读
  `plugins.entries.iibot-hermes.allow_gateway_injection`；旧位置 `plugins.entries.inbox-wake.allow_gateway_injection`
  只在启动时告警（`scripts/upgrade.sh` 会自动搬）。
- 设置键都读 `plugins.entries.iibot-hermes.settings.{inbox,prompt,persona}.<key>`；camelCase 合法。
- **整段回落**：`settings.inbox` 缺失 → 读 `plugins.entries.inbox-wake.settings`；`settings.prompt` 缺失 →
  读 `plugins.entries.minimal-system.settings`；`settings.persona` 缺失 → 读 `plugins.entries.persona-mode.settings`。
  回落只读、会打 WARNING，新段存在后旧段失效（可删）。
- `channels` 是**列表**，`hermes config set` 只写标量 → 需要自定义时手改 YAML（默认空 = 订阅全部通道）。
- 插件只在 **gateway 启动时**发现一次 → 改完设置必须重启 gateway。
- **旧目录必须删**：`$HERMES_HOME/plugins/{inbox-wake,minimal-system,persona-mode}` 留着会与新插件在同一进程里
  各注册一遍（4 个工具、`/inbox`、hook 全重、`inbox.push` 投递两次）。`install.sh` / `upgrade.sh` /
  `sync-plugins.sh` 都会先删（upgrade/sync 先备份到 `plugins/.backups/<id>-<时间戳>`）。
- 项目内的 `./.hermes/plugins/` **不会**被加载，只能装到 `$HERMES_HOME/plugins/`。

## 3. 业务工具（InboxMosire MCP）

adapter 只负责「唤醒 + 绑定」四个工具。真正干活的 `inbox_list` / `inbox_claim` /
`inbox_renew` / `inbox_complete` / `inbox_release` / `inbox_push` / `inbox_rest` /
`inbox_occupancy` 来自 InboxMosire 自己的 MCP 端点：

```bash
hermes mcp add inbox-mosire --url http://127.0.0.1:18766/mcp --auth header
hermes mcp test inbox-mosire
```

- token 由 Hermes 交互式提示输入，别写进命令行历史。
- Hermes 的 config **没有** `${ENV}` 展开，落到 `config.yaml` 里的 header 是明文 →
  `chmod 600 ~/.hermes/config.yaml`，且永远不要提交。
- 工具名会带上服务器前缀，如 `mcp__inbox-mosire__inbox_list`；
  `settings.persona.outboundTools` / `denyTools` 的外发门禁按**后缀**匹配，所以
  `mcp__…__qq_conversation_send` 同样会被拦（需先 `persona.enabled: true`）。
- `wakeMode: claim` 需要 `inbox_list` + `inbox_claim` 两个工具在线；
  `inbox_adapter_status` 的 `health.programMcp` 会报 `ready` / `partial` / `missing`。
- 协调/开发工作区**不要**挂 iibot 的 `qq_*` Bot 业务 MCP（工作区红线 7）。

## 4. 校验

```bash
hermes plugins list --user --plain
hermes plugins doctor iibot-hermes --ci              # 聚合插件：4 tool(s) + 7 hook(s)（prompt/persona 关时只注册 wake 面；persona 打开后 9）
# `WARN: manifest declares hook 'pre_tool_call' but registration did not add it` 是
# persona.enabled=false 的预期结果：manifest 取并集声明，运行时按开关注册。
hermes plugins validate plugins/iibot-hermes --json  # catalog 同款检查（可选）；0.4.0-fix1 起 vision 默认注册
                                                     # llm_request，必须声明 provides_middleware 才能 ok
grep -a "iibot-hermes: effective settings" ~/.hermes/logs/agent.log | tail -1   # 生效值（不含 token）
```

`doctor` 用临时 `HERMES_HOME` 真实跑一遍 `register(ctx)` 并核对 registry 漂移，
所以插件约定：**不在 `register()` 里做网络 IO**（SSE/HTTP 全在 worker 线程，bind 之后才启动）。

## 5. 冒烟

```bash
# 1) 在 Hermes 会话里绑定（token 已在 settings 里，所以 bind 只需 inboxId）
/inbox bind main
/inbox status

# 2) 从外部压一条 item
curl -sS -X POST http://127.0.0.1:18765/v1/inboxes/main/items \
  -H "Authorization: Bearer <producer-token>" -H 'content-type: application/json' \
  -d '{"channel":"channel-a","kind":"smoke","title":"smoke test","payload":{}}'

# 3) 会话应收到一条 [INBOX_HOST_EVENT] user message；
#    /inbox log 里应出现 binding.bind / sse.event / wake.ok
```

人设侧冒烟（先 `hermes config set --force plugins.entries.iibot-hermes.settings.persona.enabled true`）：

```bash
# 新开 session 后，历史里应只有一条带同一 persona-hash 的注入
grep -c 'persona-hash' <session 转写>
# 把 PERSONA.md 临时改名 → 外发工具应被 block，且只出现一次 persona-missing 提示
```

## 5.1 已装旧版怎么升级

```bash
git pull
./scripts/sync-plugins.sh --dry-run   # 已装版本 → 源版本，以及哪些文件会变
./scripts/sync-plugins.sh --restart   # 先删三个 0.x 目录，再整目录替换 iibot-hermes + 清 __pycache__ + 重启 gateway
hermes plugins doctor iibot-hermes
```

远端不 checkout 时用 `rsync -a --delete --exclude __pycache__ plugins/iibot-hermes ops@host:.hermes/plugins/`，
**并** `ssh ops@host 'rm -rf ~/.hermes/plugins/{inbox-wake,minimal-system,persona-mode}'`。
细节与回滚见 `docs/RUNBOOK.md` §4。

> **0.4.0-fix1 起**：`settings.vision.enabled` 默认 `true`，升级后第一次启动会多一行
> `iibot-hermes vision: loaded (maxImages=4, …)`。无配置迁移；要关掉（只在你故意让图留在 tool 行时）：
> `hermes config set plugins.entries.iibot-hermes.settings.vision.enabled false`。
> 端口/权限相关设置不变，`./scripts/install.sh` 不需要为新段做任何事（缺省即默认值）。

## 6. kit：一个目录 + 一句话自动挂载

`kit/` 是给"我只想放个 jar + 一份说明，然后开 hermes 说话就挂上"的场景准备的最小发行包：

```
kit/
├── kit.sh            # doctor / up / status / down / probe / token
├── kit.env.example   # 所有可覆盖项的默认值
├── 引导.md           # 给 agent 读的引导（对话时让它照做）
├── jars/             # 放 inbox-server-*-all.jar（可选：iibot-app-*.jar）
└── run/              # 运行期目录（日志、pid、数据），已 gitignore
```

### 6.1 三步

```bash
cd kit
cp kit.env.example kit.env      # 改 JARS_DIR / INBOX_DATA 等（可全部留默认）
cp /path/to/inbox-server-*-all.jar jars/
./kit.sh doctor                 # 先自检：java / jar / hermes / 端口 / token 权限 / 插件
./kit.sh up                     # 起服务 + 发 token + 挂 MCP + 写插件设置 + 重启 gateway
```

`up` 之后**不需要再手工 `/inbox bind`**：`autoBind` 生效后，任何新会话的首轮会用
settings 里的 `inboxId` / `adapterTokenFile` 自动绑定（失败只写一条 warning，不打断对话）。

### 6.2 `./kit.sh up` 到底做了什么

1. **起 InboxMosire**：缺 `inbox.db` 先 `init --data-dir`，然后 `java -jar … serve --data-dir …`
   （API `18765`、MCP `18766`，日志 `kit/run/inbox.log`，pid 落 `kit/run/inbox.pid`）。
2. **发两个最小权限 token（一律 0600）**：
   - `hermes-wake`：`status:read` `items:read` `events:read` `inbox:occupy` → 给插件；
   - `hermes-program`：`status:read` `items:read` `items:manage` → 给 MCP 挂载用。
   只回显 `credential_id`，**从不打印 token 明文**。
3. **挂 MCP**：`mcp_servers.inbox.{url,transport=streamable-http,headers.Authorization}`。
   这会把 InboxMosire 自带的 12 个工具（`inbox_list` / `inbox_claim` / `inbox_complete` /
   `inbox_rest` …）带给 agent，插件的 `health.programMcp` 也因此从 `missing` 变成 `ready`。
4. **写插件设置**：`enabled` / `apiUrl` / `inboxId` / `adapterTokenFile` / `wakeMode` /
   `wakeProfile` / `autoBind`，并确保 `allow_gateway_injection: true`。
   （插件 hook/tool/`/inbox` 都在 gateway 进程里跑，所以设置改完必须重启 gateway。）
5. **重启 gateway**：先 `hermes gateway stop`，等它真的退出，再 `nohup hermes gateway run`；
   有活跃会话时 drain 可能要 1 分钟左右，脚本最多等 150 秒。

### 6.3 其它命令

| 命令 | 作用 |
| --- | --- |
| `./kit.sh doctor` | 不改任何东西，只报告缺什么（jar / java / hermes / 端口 / token 权限 / 插件 / MCP 挂载） |
| `./kit.sh status` | 服务与 token 权限现状、gateway、插件设置、已挂 MCP 列表（都不打印 token 值） |
| `./kit.sh probe` | 跑一次 `hermes -z` 一次性会话，验证 autoBind 真的绑上（会在绑定表留一条） |
| `./kit.sh down` | 停掉 kit 起的 InboxMosire（不动 gateway、不动配置） |
| `./kit.sh token wake\|program` | 只重新签发某个 token（覆盖旧文件，0600） |

### 6.4 配合对话使用

把 `kit/引导.md` 交给 agent（或直接让它在 `kit/` 目录里读），然后说一句
"按 引导.md 把 inbox 起来"。它应当依次跑 `doctor` → `up` → `status` 并汇报，
期间**不得** `cat` token 文件或把 token 贴进对话。

### 6.5 注意

- 只挂 MCP、不跑插件时：把 `kit.env` 里 `AUTO_BIND=0`（默认是 1）。
- 不想让 kit 重启 gateway：`RESTART_GATEWAY=never ./kit.sh up`，然后自己按需重启。
- `kit/run/`、`kit/jars/`、`kit.env` 已在 `kit/.gitignore` 里；jar 不进仓库。
- 端口被占时会直接报出来；`INBOX_API`/`INBOX_MCP` 可以改成别的端口（两处要一致改）。

## 7. 卸载

```bash
hermes plugins disable iibot-hermes
hermes plugins remove iibot-hermes
# plugin-data（绑定表 + ops 日志）保留在 $HERMES_HOME/plugin-data/agent-plugin-iibot-hermes-*/
# 0.x 的旧数据在 plugin-data/{persona-mode,inbox-wake}/，需要留档就先 cp -a 出来
rm -rf ~/.hermes/inbox-mosire/api-token        # 不再需要 adapter 时
```

卸载后记得从 `plugins.enabled` 里去掉 `iibot-hermes`，并重启 gateway。

## 从 0.x 迁移到 0.4.0（三合一）

0.4.0 把 `inbox-wake` / `minimal-system` / `persona-mode` 合成一个 `iibot-hermes`。升级只需：

```bash
./scripts/upgrade.sh --dry-run   # 看会删哪些目录、换哪些文件
./scripts/upgrade.sh             # 删三个旧目录（先备份）+ 装 iibot-hermes + 补配置键 + 重启 gateway
```

设置**不必手工搬**：新段 `plugins.entries.iibot-hermes.settings.{inbox,prompt,persona}` 缺失时，插件会
只读回落旧命名空间（`inbox-wake` / `minimal-system` / `persona-mode` 的 `settings`），每次回落打一条 WARNING。
确认值正确后再显式写到新段并删除旧段（例：

```bash
hermes config set --force plugins.entries.iibot-hermes.settings.inbox.apiUrl http://127.0.0.1:18765
hermes config set --force plugins.entries.iibot-hermes.allow_gateway_injection true
```

）。两个开关在 0.4.0 **默认关**，要恢复 0.x 行为需显式打开：

```bash
hermes config set --force plugins.entries.iibot-hermes.settings.prompt.enabled true    # 与 agent.system_prompt_override 二选一
hermes config set --force plugins.entries.iibot-hermes.settings.persona.enabled true   # D-009 之后默认不注入人设
```

⚠️ **旧目录必须删**：`$HERMES_HOME/plugins/{inbox-wake,minimal-system,persona-mode}` 与新插件同时被加载时，
4 个工具、`/inbox`、hook 会各注册一遍，`inbox.push` 会被投递两次。`install.sh` / `upgrade.sh` /
`sync-plugins.sh` 都会先删；手工升级的话自己 `rm -rf` 那三个目录后重启 gateway。

## 升级到新版本（一键）

两种路径，按当初的安装方式选：

**A. 宿主原生（只对 provenance 有效的插件）**

```bash
hermes plugins update iibot-hermes        # 单插件
hermes plugins update --yes               # 全部已装插件
```

只有当插件是 `hermes plugins install <repo>#plugins/iibot-hermes --ref <tag>` 装进来的、宿主有 provenance 记录时才可用；早期用 `cp -a` / `sync-plugins.sh` 复制进去的会报找不到来源。

**B. 仓内脚本（对复制安装同样有效，推荐）**

```bash
./scripts/upgrade.sh --dry-run            # 先看会改什么
./scripts/upgrade.sh                      # 默认升级到 v0.4.0，自动重启 gateway
./scripts/upgrade.sh --latest             # 取远端最新 v* tag
./scripts/upgrade.sh --ref v0.3.0         # 指定任意 tag / 分支 / 提交
./scripts/upgrade.sh --no-restart         # 只换文件，不重启
./scripts/upgrade.sh --target /home/ops/.hermes
./scripts/upgrade.sh --rollback           # 回到最近一次备份并重启
```

它做的事：**先删** `$HERMES_HOME/plugins/{inbox-wake,minimal-system,persona-mode}`（先备份到
`$HERMES_HOME/plugins/.backups/<id>-<时间戳>`）→ 按 ref 从 git 取干净副本（`git archive`，不经过工作区）
→ 备份并整目录替换 `iibot-hermes`、清 `__pycache__` → `python3 -m compileall` 语法自检（失败就提示回滚）
→ 缺则补 `settings.inbox.{pushMode,pushGraceMs,firstRunGuide}`，并把旧的
`plugins.entries.inbox-wake.allow_gateway_injection` 搬到 `plugins.entries.iibot-hermes.allow_gateway_injection`
→ `hermes gateway stop && sleep 2 && hermes gateway start` → 打印版本、备份目录与核对提示。

不碰 `config.yaml` 里已有的值、不碰 plugin-data、不碰 token。

升级后核对日志：应看到 `iibot-hermes: effective settings …` 一行，以及 `push.ok` / `push.ack` /
`guide.sent`，**不应**再看到 `wake.ok`（那是 changed 路径；只有 `pushMode: off` 时才该出现），
也不应出现重复注册（工具重名 / hook 双份）的告警。

## 推送模式（0.3.0 起）

服务端能推 `inbox.push` 时，插件优先走推送；`changed` 只用来刷水位，不再整窗重投。

```yaml
plugins:
  entries:
    iibot-hermes:
      settings:
        inbox:
          pushMode: on          # auto（默认）| on | off
          pushGraceMs: 2000     # changed 让位给 push 的宽限
          firstRunGuide: true   # 首条注入先说明 AGENTS.md / PERSONA.md
          firstRunGuideText: "" # 留空用内置文案
```

- `auto`：第一次收到合法 push 后自动转 push 优先（老服务端不受影响）。
- `on`：直接 push 优先，`changed` 一律 `wake.skipped reason=push-authoritative`。
- `off`：完全忽略 push（不注入、不 ack），退回旧 changed 唤醒。

注入用 `ctx.inject_message(..., media_urls=[...])`；宿主不认这个参数时自动降级为纯文本并只 warning 一次。
每次投递后按 `deliveryId` 回 `POST /v1/inboxes/{inbox}/deliveries/{id}/ack`（4xx 放弃、5xx/传输错误重试一次）。

核对：日志里应看到 `push.ok` / `push.ack` / `guide.sent`，且**不应**再出现 `wake.ok`（那是 changed 路径）。
