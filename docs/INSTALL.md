# 安装与启用

## 0. 前提

- Hermes 已安装，`$HERMES_HOME`（默认 `~/.hermes`）可写。
- 若 `hermes --version` 报
  `install state is not writable by this user (…/.recovery.lock)`，先修 install 目录所有权，
  否则 CLI 全部子命令（含 `plugins doctor`）都跑不动。
- InboxMosire daemon 在跑：`curl -s http://127.0.0.1:18765/healthz`。
- 通知板 token 已签发，落在 0600 文件里（推荐 `~/.hermes/secrets/inbox-mosire.token`）。

## 1. 落盘

期望的仓库结构：

```
InboxMosire-Hermes/
├── plugins/
│   ├── persona-mode/   __init__.py  plugin.yaml  lib/{role,contract,guard,size}.py
│   └── inbox-wake/     __init__.py  plugin.yaml  lib/{binding,sse,client,template,
│                                                  tools,commands,state,config,oplog}.py
├── scripts/install.sh
├── docs/
└── PERSONA.md.example
```

安装 = 把 `plugins/<id>/` 复制到 `$HERMES_HOME/plugins/<id>/`：

```bash
./scripts/install.sh            # 默认装到 $HERMES_HOME/plugins（缺省 ~/.hermes/plugins）
./scripts/install.sh --dry-run  # 只打印动作
```

脚本做的事：复制两包 → 建 `$HERMES_HOME/plugin-data/{persona-mode,inbox-wake}/` →
打印需要你手工粘贴的 `config.yaml` 片段。**它不改你的 config.yaml。**

## 2. 启用（`~/.hermes/config.yaml`）

```yaml
plugins:
  enabled: [persona-mode, inbox-wake]      # 必须点名，插件默认不加载
  entries:
    persona-mode:
      settings:
        personaFile: PERSONA.md
        maxChars: 4000
        outboundGuard: true
    inbox-wake:
      allow_gateway_injection: true        # 不写 true，inject_message 一律返回 False
      settings:
        apiUrl: http://127.0.0.1:18765
        inboxId: desk
        adapterTokenRef: /home/<you>/.hermes/secrets/inbox-mosire.token
        channels: []
        wakeMode: claim
        wakeProfile: generic
```

注意：

- `allow_gateway_injection` 在 **entry 层**，不在 `settings` 下。
- 插件只在 **gateway 进程启动时**发现一次 → 改完必须重启 gateway。
- 项目内的 `./.hermes/plugins/` **不会**被加载，只能装到 `$HERMES_HOME/plugins/`。

## 3. 业务工具（InboxMosire MCP）

adapter 工具只负责「唤醒 + 绑定」。真正的 `inbox_list`/`inbox_claim`/`inbox_complete`
来自 InboxMosire 自己的 MCP 端点，用 Hermes 的 `mcp_servers` 挂载：

```yaml
mcp_servers:
  inbox-mosire:
    url: "http://127.0.0.1:18766/mcp"
    headers:
      Authorization: "Bearer <token>"
    tools:
      exclude: [inbox_push]        # 只读宿主可以排除写入类工具
```

- Hermes 的 config **没有** `${ENV}` 展开，token 只能明文写在这里 →
  `chmod 600 ~/.hermes/config.yaml`，且不要把它提交到任何仓库。
- 工具名会变成 `mcp__inbox-mosire__inbox_list`。
  `persona-mode` 的外发门禁按**后缀**匹配，所以 `mcp__<server>__qq_conversation_send`
  同样会被拦。
- 协调/开发工作区**不要**挂 iibot 的 `qq_*` Bot 业务 MCP（工作区红线 7）。

## 4. 校验

```bash
hermes plugins doctor persona-mode      # manifest / register / registry 漂移
hermes plugins doctor inbox-wake --ci
hermes plugins list                     # 确认两个 id 都在且 enabled
```

`doctor` 会为每个插件起一个临时 `HERMES_HOME`，所以**不要**在 `register(ctx)` 里
做网络 IO（本仓库约定：SSE/HTTP 全部在 worker 线程，且只在 bind 之后启动）。

## 5. 冒烟

```bash
# 1) 绑定（在 Hermes 会话里）
/inbox bind desk file:/home/<you>/.hermes/secrets/inbox-mosire.token
/inbox status

# 2) 从外部压一条 item
curl -sS -X POST http://127.0.0.1:18765/v1/inboxes/desk/items \
  -H "Authorization: Bearer <producer-token>" -H 'content-type: application/json' \
  -d '{"channel":"channel-a","kind":"smoke","title":"smoke test","payload":{}}'

# 3) 会话应收到一条 [INBOX_HOST_EVENT] user message；
#    之后 inbox_adapter_log 里应出现 action=wake 的条目
```

人设侧冒烟：`/personality` 不应改动插件段；新开 session 后
`grep -c persona-hash` 历史里应有且只有一条同 hash 记录；删掉 `PERSONA.md` 后
对外发工具的调用应被 block 并给出操作者提示。

## 6. 升级与卸载

```bash
./scripts/install.sh --upgrade    # 覆盖副本，保留 plugin-data/
./scripts/install.sh --uninstall  # 删除两份插件目录，保留 plugin-data/
```

卸载后记得从 `config.yaml` 的 `plugins.enabled` 里去掉两个 id，并重启 gateway。
