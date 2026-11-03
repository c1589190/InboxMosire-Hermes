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
| 装插件 | `./scripts/install.sh plugins` | `hermes plugins install "<repo>#plugins/<id>" --enable`，或 `--local <path>` 从本地 checkout 复制 |
| 写 token | `./scripts/install.sh token` | 交互式读入（不回显）；无终端时用 `INBOX_TOKEN=… ./scripts/install.sh token`；写 `$HERMES_HOME/inbox-mosire/api-token`（0600，目录 0700） |
| 写设置 | `./scripts/install.sh settings` | 全部走 `hermes config set --force`，不手改 YAML |
| 体检 | `./scripts/install.sh check` | `plugins list` + `plugins doctor --ci` + `healthz` |

环境变量覆盖：`INBOX_API_URL`（默认 `http://127.0.0.1:18765`）、`INBOX_ID`（`main`）、
`WAKE_MODE`（`claim`）、`WAKE_PROFILE`（`generic`）、`PERSONA_FILE`、`TOKEN_PATH`、`HERMES_BIN`。

## 2. 脚本写进 config.yaml 的东西

```yaml
agent:
  system_prompt_override: "You are a helpful software engineer assistant."  # 需宿主补丁，见 docs/HOST-PATCH.md
plugins:
  enabled: [persona-mode, inbox-wake, minimal-system]  # 插件默认不加载，必须点名；persona-mode 依赖 minimal-system
  entries:
    minimal-system:                           # 必需同伴：把整段 system prompt 换成一句
      settings:
        enabled: true
        systemPrompt: "You are a helpful software engineer assistant."
    persona-mode:
      settings:
        enabled: true
        personaFile: /abs/path/to/PERSONA.md  # 不写则读会话工作目录下的 PERSONA.md
    inbox-wake:
      allow_gateway_injection: true           # 不写 true，inject_message 一律返回 False
      settings:
        enabled: true
        apiUrl: http://127.0.0.1:18765
        inboxId: main
        adapterTokenFile: file:/home/<you>/.hermes/inbox-mosire/api-token
        wakeMode: claim
        wakeProfile: generic
```

要点：

- `allow_gateway_injection` 在 **entry 层**（和 `settings` 同级），不在 `settings` 下。
- 设置键都读 `plugins.entries.<id>.settings.<key>`；camelCase 合法。
- `channels` 是**列表**，`hermes config set` 只写标量 → 需要自定义时手改 YAML（默认空 = 订阅全部通道）。
- 插件只在 **gateway 启动时**发现一次 → 改完设置必须重启 gateway。
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
  `persona-mode` 的外发门禁按**后缀**匹配，所以 `mcp__…__qq_conversation_send` 同样会被拦。
- `wakeMode: claim` 需要 `inbox_list` + `inbox_claim` 两个工具在线；
  `inbox_adapter_status` 的 `health.programMcp` 会报 `ready` / `partial` / `missing`。
- 协调/开发工作区**不要**挂 iibot 的 `qq_*` Bot 业务 MCP（工作区红线 7）。

## 4. 校验

```bash
hermes plugins list --user --plain
hermes plugins doctor persona-mode --ci
hermes plugins doctor inbox-wake --ci
hermes plugins validate plugins/inbox-wake --json    # catalog 同款检查（可选）
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

人设侧冒烟：

```bash
# 新开 session 后，历史里应只有一条带同一 persona-hash 的注入
grep -c 'persona-hash' <session 转写>
# 把 PERSONA.md 临时改名 → 外发工具应被 block，且只出现一次 persona-missing 提示
```

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
hermes plugins disable inbox-wake
hermes plugins disable persona-mode
hermes plugins remove inbox-wake
hermes plugins remove persona-mode
# plugin-data（绑定表 + ops 日志）保留在 $HERMES_HOME/plugin-data/{persona-mode,inbox-wake}/
rm -rf ~/.hermes/inbox-mosire/api-token        # 不再需要 adapter 时
```

卸载后记得从 `plugins.enabled` 里去掉两个 id，并重启 gateway。
