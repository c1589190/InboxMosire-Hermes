# 验收清单

状态图例：⬜ 未做 / 🟡 部分 / ✅ 通过 / ❌ 失败。
✅ 表示**离线可证**：`python3 -m unittest discover -s tests` 里有对应用例（78 个，全绿）。
🔒 表示必须在操作者自己的终端跑（本开发者沙箱写不了 `$HERMES_HOME`，也没有真 gateway）。

每条都必须可复现：写清「怎么造条件 + 期望观察到的现象」。标 🔒 的条目只能在
用户自己的终端执行（本开发者沙箱写不了 `$HERMES_HOME`）。

## A. 静态

- 🔒 A1 `hermes plugins doctor persona-mode` 无 drift 告警 🔒
- 🔒 A2 `hermes plugins doctor inbox-wake --ci` 通过 🔒
- ✅ A3 `python -c "import persona_mode"` 无副作用（不建线程、不发 HTTP、不写盘）
- ✅ A4 `plugin.yaml` 里 `provides_tools` / `provides_hooks` 与 `register()` 实际注册集合**完全一致**
- ✅ A5 两个包的 `register()` 在无 `HERMES_HOME` 时也不抛异常（降级为 warning）

## B. persona-mode

- ✅ B1 新 session 首轮：`conversation_history` 中出现 1 条含 `<persona-contract>` 与
  `<!-- persona-hash: X -->` 的消息
- ✅ B2 第二轮不再重复注入（同 hash 命中）
- ✅ B3 修改 `PERSONA.md` 后续轮：注入 1 条**新 hash** 的契约，旧的不删
- ✅ B4 压缩把契约挤出历史后，下一轮自动重锚（模拟：清空 history 再调 hook）
- ✅ B5 `PERSONA.md` 不存在 → 注入 `persona-missing` 提示，且同一 session 只提示一次
- ✅ B6 缺文件时 `pre_tool_call("qq_conversation_send", …)` 返回
  `{"action": "block", …}`；`pre_tool_call("mcp__iibot__qq_conversation_send", …)` 同样被拦
- ✅ B7 缺文件时普通工具（`read`、`bash`）**不受影响**
- ✅ B8 `maxChars` 生效：超长文件被截断到 4000 并附 `<!-- persona-truncated -->`
- ✅ B9 system prompt 段只有 1 段、≤200 字符，且 `id` 符合 1–128 位小写标识符
- ✅ B10 `denyTools` 为空时不改变任何工具可用性（默认行为必须无感）

## C. inbox-wake 绑定

- ✅ C1 `/inbox bind` 缺 `sessionId` 时绑定到**调用方** session
- ✅ C2 `SAFE_ID` 校验：`inboxId=../evil`、`channels=["a b"]` 均返回 `INVALID_ARGUMENT`
- ✅ C3 token 文件 0644 → 拒绝绑定并给出 `文件权限过宽` 提示；0600 → 通过
- 🟡 C4 token 无效（401）→ 状态 `error`，`inbox_adapter_log` 有 `start_failed`，**不含 token**
- 🟡 C5 daemon 未启动（连接被拒）→ `UNREACHABLE`，不崩溃、不无限重试
- 🟡 C6 `unbind` 后 SSE 线程 5s 内退出（`threading.enumerate()` 无残留）
- 🟡 C7 `on_session_end` / `on_session_reset` / `on_session_delete` 都会停 SSE 并清占用
- 🟡 C8 两个 session 绑同一 inbox → 互不影响（各自 cursor、各自水位、日志不串）
- ✅ C9 `inbox_adapter_status` 不返回 token、不返回 item 正文、不返回其他 session 数据

## D. 唤醒

- ✅ D1 push 1 条 item → **恰好 1 次**注入；`inbox_adapter_log` 有 `action=wake`
- ✅ D2 同一 revision 重复 SSE 事件 → 不重复注入
- ✅ D3 `available=0` → 不注入
- 🟡 D4 三连 push（<300ms）→ 去抖后最多 1 次注入
- ✅ D5 turn 进行中到达的唤醒 → 进 `pendingWake`，本轮结束时补投 1 次
- ✅ D6 `pendingWake` 超过 `pendingTtlSeconds` → 丢弃并记 `wake_deferred` 过期
- ✅ D7 `inject_message` 返回 False（未开 `allow_gateway_injection`）→ 退避 30s，
  之后 60s/120s… 上限 900s，日志 `wake_failed` 且不含正文
- 🟡 D8 SSE 断线 → 1s 起指数退避到 60s，带 `Last-Event-ID` 续传，重连次数进 `health.reconnects`
- 🟡 D9 45s 无心跳 → 判定断线并重连
- ✅ D10 `restSeconds` 工具结果 → `nextWakeAllowedAt` 生效，rest 内不注入

## E. 唤醒文本

- ✅ E1 header 严格为
  `[INBOX_HOST_EVENT]\ninbox=<id> revision=<n> available=<n> channels=[a,b]`
- ✅ E2 摘要行格式 `- {"itemId":…,"channel":…,"kind":…,"summary":…}`（`JSON.stringify` 等价）
- ✅ E3 `kind` 截断到 40、`summary` 截断到 80、去换行与控制字符
- ✅ E4 摘要块 ≥1200 字符时折叠为 `- 还有 N 条未展示`，且总量不超预算
- ✅ E5 `wakeMode=view-ack` 时行内不含 `itemId`/`channel`，只含 `source`/`kind`/`summary`
- 🟡 E6 固定指令段**永不**被截断（即使 content 段很长）
- 🟡 E7 `wakeProfile=iibot` 的指令段与 `generic` 的对应关系符合 `docs/TOOLS.md` §4
- ✅ E8 `summaryEnabled=false` → 只有 header + 指令段

## F. 隐私与红线

- ✅ F1 全仓库 `grep -rn "Bearer\|token=" docs plugins` 无真实 token / 无 token 示例值
- ✅ F2 状态文档、ops log、唤醒文本、工具输出里**都没有** token 值（只允许路径/引用名）
- ✅ F3 唤醒文本不含 item 正文（除非显式 `wakeContent: bounded`，且被标注为不可信数据）
- ✅ F4 adapter **不**调用 claim/complete/push（代码里搜不到这几个方法）
- 🟡 F5 默认 `apiUrl` 是回环地址；非回环需显式配置并打 warning
- 🔒 F6 日志文件权限 0600，`plugin-data/` 目录 0700

## G. 真机（🔒，需用户终端；脚本：`./scripts/install.sh check`）

- ⬜ G1 gateway 重启后 `plugins list` 显示两个插件 enabled
- ⬜ G2 `/inbox bind` → `/inbox status` 显示 `sse=streaming`
- ⬜ G3 真实 push → 会话收到唤醒 → agent 用 `inbox_claim` 处理 → `inbox_complete`
- ⬜ G4 人设：真实 QQ 会话里 agent 自称符合 `PERSONA.md`（本鲸/本鱼/大肥鱼我）
- ⬜ G5 连续 3 天无唤醒泄漏测试（token 不在任何 `hook_outputs/`、日志、状态文件里）
- ⬜ G6 压缩后重锚：长会话触发压缩，下一轮仍带最新 hash 的契约

## H. 未验证项 / 风险

- ⚠️ `pre_llm_call` 的 `conversation_history` 是否包含**注入过**的 contract 消息
  （DSH 的 surface 包含注入消息；Hermes 需要实测确认，否则 B1/B2 会退化为每次注入）
- ⚠️ `on_stream_end` 是否每轮都触发（若只在流式输出时触发，D5 需改用 `post_llm_call`）
- ⚠️ gateway 侧 `inject_message` 对「session 正在跑」的排队语义未实测（D5）
- ⚠️ `hermes plugins doctor` 是否会因插件内部线程而卡住（A1/A2）

### 落地说明（实现完成后补记）

- D6 用 `pending_ttl_seconds`（默认 600，内部常量）在 `_maybe_wake` 里判过期并记
  `wake.defer.expired`。
- D10 由 `post_tool_call` 钩子实现：`inbox_rest`（含 `mcp__<server>__inbox_rest`）返回的
  `restSeconds` 会被写进 `wake.nextWakeAllowedAt`，并记 `rest.applied`；
  非 rest 工具一律忽略（`test_rest_from_dict_and_other_tools_ignored`）。
- C4/C5 的日志事件名与验收文案不同：实际是 `status.error` / `sse.retry` / `wake.failed`。
- 仍未覆盖：真 SSE 断线重连（D8/D9 只有 `_consume` 单测）、`on_stream_end` 替代路径（H2）、
  真机 `inject_message` 排队语义（H3）。
