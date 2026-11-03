# 对接提示词（可直接粘给 Agent）

两个自包含的提示词：**A** 在目标机装 iiBot 并起服务，**B** 在 Hermes 侧装插件并接上 A。
版本锚点：iiBot `Alpha20261103`、插件 `v0.2.0`。真实联调前，一切结果按「跑没跑」记录，不许写「应该可以」。

---

## A. iiBot 侧（贴给目标机上的 Agent）

> 在这台机器上装 iiBot 并跑起来，全程不要 sudo，凭据不要出现在命令行历史里。
>
> 1. 下载（全依赖内置的单文件 jar，需要 JDK 21）：
>    ```bash
>    mkdir -p ~/iibot && cd ~/iibot
>    base=https://github.com/c1589190/iiBotMosire/releases/download/Alpha20261103
>    curl -fLO "$base/iibot-app-Alpha20261103.jar"
>    curl -fLO "$base/SHA256SUMS"
>    sha256sum -c SHA256SUMS   # 期望 a362b0e5475ff345e45dca1f3b001875d1377b0203c67e0fc62b3bc181c8ded6
>    ```
> 2. 目录：`~/iibot/{iibot-app-Alpha20261103.jar, config/bot/bot.json, data/, run/, logs/}`。
>    `config/bot/bot.json` 三块必填：
>    - `control`: `host 127.0.0.1` / `port 8765` / `path /mcp/bot`
>    - `inbox`: `inboxId` / `apiHost 127.0.0.1` + `apiPort 46500` / `mcpHost 127.0.0.1` + `mcpPort 46501` / `mcpPath /mcp`
>    - `accounts.main.oneBot`: `eventWebSocketUri` / `httpBaseUri`（NapCat 的地址，NapCat 本身不归这个 jar 管）
>    - 可选 `messageBoard`（推荐）：`maxPreviewMessages: 50`（未读上限）、`contextMessages: 0` + `contextMaxChars: 0`（**不要**把旧消息塞进板，Agent 需要时自己 read）、`summaryMaxChars: 4000`（单条摘要长度）
> 3. 凭据只放 env 文件（`chmod 600`），由启动脚本注入，**不要写进命令行**：
>    ```bash
>    cat > run/boot-env.sh <<'SH'
>    #!/usr/bin/env bash
>    cd "$(dirname "$0")/.."
>    set -a; . "$HOME/.config/iibot/llm.env"; . "$HOME/.config/iibot/napcat.env"; . "$HOME/.config/iibot/control.env"; set +a
>    exec /usr/bin/java -jar iibot-app-Alpha20261103.jar --config config/bot/bot.json
>    SH
>    chmod +x run/boot-env.sh
>    ```
> 4. 启动：`nohup run/boot-env.sh >> run/console.log 2>&1 &`
> 5. 验证（三项都要有输出才算起来）：
>    - `ss -lnt | grep -E '8765|46500|46501'` 三个端口都在听
>    - `curl -s http://127.0.0.1:46500/healthz` 返回 200
>    - `curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:46500/v1/inboxes/<inboxId>/status` 有 `revision`
>    - 日志里有 `event=inbox_embedded_started`
>
> 缺凭据、缺 NapCat、端口被占就**停下来问**，不要猜 token、不要乱改端口。

---

## B. Hermes 侧（贴给装了 Hermes 的 Agent）

> 把 Hermes 侧的三个插件装好并接到 A 起好的 iiBot 上。要**你自己的用户**跑，不要 sudo。
>
> 1. 取代码并固定版本：
>    ```bash
>    git clone --depth 1 -b v0.2.0 https://github.com/c1589190/InboxMosire-Hermes.git
>    cd InboxMosire-Hermes
>    ```
> 2. 一条命令装三个插件（`persona-mode` / `inbox-wake` / `minimal-system`）+ 写设置：
>    ```bash
>    INBOX_API_URL=http://127.0.0.1:46500 \
>    INBOX_ID=iibot-bot \
>    WAKE_MODE=view-ack \
>    WAKE_PROFILE=iibot \
>    TOKEN_PATH="$HOME/.hermes/inbox-mosire/iibot-wake.token" \
>    PERSONA_FILE=/abs/path/PERSONA.md \
>    ./scripts/install.sh --local .
>    ```
>    adapter token 用 `INBOX_TOKEN=...` 传一次（或交互输入），脚本按 0600 落盘。
>    token 只需要 `status:read,items:read,events:read,inbox:occupy`；**别**给它 `items:manage`。
> 3. 挂两个 MCP（token 由 Hermes 交互提示输入，不要写进命令行）：
>    ```bash
>    hermes mcp add iibot         --url http://127.0.0.1:8765/mcp/bot --auth header
>    hermes mcp add inbox-mosire  --url http://127.0.0.1:46501/mcp    --auth header
>    # 视觉能力：custom:<name> 上的模型宿主目录里查不到，不声明就只能拿到 MEDIA: 路径
>    hermes config set model.supports_vision true
>    hermes mcp test iibot
>    hermes mcp test inbox-mosire
>    ```
>    程序侧 token 才需要 `items:read,items:manage`，和上面的 wake token 分开、不要复用。
> 4. 重启让插件与设置生效：`hermes gateway restart`（或 `hermes gateway stop` + `nohup hermes gateway run &`）。
> 5. 验证：
>    - `./scripts/check-vision.py` 两个 ✅（宿主声明 + 模型真的读到探针图）；显示「未声明」就补
>      `hermes config set model.supports_vision true` 再重启 gateway
>    - `hermes plugins doctor persona-mode` / `inbox-wake` / `minimal-system` 三项都过
>    - 会话里 `/inbox status` 显示 `sse: open|connecting` 且 `observed` 在涨
>    - `grep -a "inbox-wake: loaded" ~/.hermes/logs/agent.log | tail -1` 有输出
>    - 终端里发一条 QQ 消息 → `/inbox log` 出现 `wake.ok`，回答能送回 QQ
>
> 已经装过旧版要升级：`./scripts/sync-plugins.sh --dry-run`（看「已装版本 → 源版本」）→ `./scripts/sync-plugins.sh --restart`。
> 排查见 `docs/RUNBOOK.md` §7；冷启动（先起 Hermes 后起 jar）见 `docs/COLDSTART.md`。

---

## 版本线（两条，互不代表）

| 东西 | 版本长什么样 | 从哪来 |
|---|---|---|
| Hermes 插件（`persona-mode` / `inbox-wake` / `minimal-system`） | `v0.2.4`、插件 `0.2.3` 这类 `0.x.y` | `InboxMosire-Hermes` 的 tag / Release（**只有源码包**） |
| iiBot 本体 | `Alpha20261103`（jar: `iibot-app-Alpha20261103.jar`） | `iiBotMosire` 的 tag / Release（**带 jar + SHA256SUMS**） |

「插件升到 0.2.3 了，是不是要下 0.2.3 的 jar？」——**不存在 0.2.3 的 jar**：插件版本变了不需要换 jar，
除非 iiBot 侧也发了新的 `Alpha<日期>`。反向同理。

---

## 术语对照（避免两边对不上）

| 概念 | iiBot 侧 | Hermes 侧 |
|---|---|---|
| 业务工具面 | `8765/mcp/bot` 的 `qq_*` | `mcp_servers.iibot` → `codex`/`hermes` 工具列表 |
| 通知板 | 内嵌 Inbox `46500`（HTTP）+ `46501/mcp`（MCP） | `inbox-wake` 插件读 HTTP；`inbox_*` 工具走 MCP |
| 唤醒 | 未读来源的 item 变 available | `inbox-wake` 注入一条 user 消息 |
| 已读推进 | `qq_conversation_ack`（per-source `ackThrough`） | **不做**，插件只推进 `lastWokenRevision` |
| 人设 | `PERSONA.md`（运行目录） | `persona-mode` 注入 + `minimal-system` 压 system |
