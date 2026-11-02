# 安装与启用

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
| 写 token | `./scripts/install.sh token` | 交互式读入（不回显），写 `$HERMES_HOME/inbox-mosire/api-token`（0600） |
| 写设置 | `./scripts/install.sh settings` | 全部走 `hermes config set --force`，不手改 YAML |
| 体检 | `./scripts/install.sh check` | `plugins list` + `plugins doctor --ci` + `healthz` |

环境变量覆盖：`INBOX_API_URL`（默认 `http://127.0.0.1:18765`）、`INBOX_ID`（`main`）、
`WAKE_MODE`（`claim`）、`WAKE_PROFILE`（`generic`）、`PERSONA_FILE`、`TOKEN_PATH`、`HERMES_BIN`。

## 2. 脚本写进 config.yaml 的东西

```yaml
plugins:
  enabled: [persona-mode, inbox-wake]        # 插件默认不加载，必须点名
  entries:
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

## 6. 卸载

```bash
hermes plugins disable inbox-wake
hermes plugins disable persona-mode
hermes plugins remove inbox-wake
hermes plugins remove persona-mode
# plugin-data（绑定表 + ops 日志）保留在 $HERMES_HOME/plugin-data/{persona-mode,inbox-wake}/
rm -rf ~/.hermes/inbox-mosire/api-token        # 不再需要 adapter 时
```

卸载后记得从 `plugins.enabled` 里去掉两个 id，并重启 gateway。
