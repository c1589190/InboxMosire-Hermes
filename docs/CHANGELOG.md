# CHANGELOG

按主题归纳，不追每个小修。日期均为本机真机轮次（2026-11-02）。
宿主环境：Hermes `v0.22.5+3377.g0aab6dc`（2026.9.24）/ Python 3.14.9 / 模型 `deepseek-flash`。

## 未发布（HEAD）

### minimal-system 0.2.0：系统提示词可外置为 md 文件（2026-11-04）

- 版本：`plugins/minimal-system/plugin.yaml` 0.1.0 → 0.2.0
- 需求（用户口径）：「人设扮演模式的 Agent 系统提示词应当是一个单独的完整 md 文件」。
- 落法：新增设置 `plugins.entries.minimal-system.settings.systemPromptFile`。**留空 = `$HERMES_HOME/SYSTEM.md`**
  （Hermes 自己的目录，与 `SOUL.md`/`config.yaml` 同级；`HERMES_HOME` 未设时用宿主 `hermes_cli/config.py`
  的 `get_hermes_home()`，再不行 `~/.hermes`），要放别处再写绝对路径。
  优先级 **文件 > `systemPrompt` 多行配置 > 内置常量**；文件按 `(mtime, size)` 缓存，改文件即时生效、
  **不需要重启 gateway**（改路径才需要，配置只在插件加载时读一次）。
- 文件按纯文本读（UTF-8-sig + `strip()`）后**原样发送**：markdown 标题/列表/代码块照发，不解析、不渲染、
  不剥 front matter。缺失 / 不可读 / 空文件 → 静默回落到 `systemPrompt`，任何 I/O 问题都不会让一轮请求失败。
- 同时修掉一处陈旧状态：`systemPrompt` 原来「非空才覆盖」，同一进程里重新 register 会继承上一次的值；
  现在每次加载都重新推导（`plugins/minimal-system/__init__.py` 的 `_load_config`）。
- `scripts/install.sh settings` 增加 `SYSTEM_PROMPT_FILE` 环境变量透传（写法同 `PERSONA_FILE`），
  `docs/INSTALL.md` 的 env 列表与 YAML 样例同步。
- 新文件：`plugins/minimal-system/lib/prompt_file.py`（`resolve_path` + `PromptReader`，读取语义与
  `plugins/persona-mode/lib/persona.py` 的 `PersonaReader` 保持一致）。
- 测试：`tests/test_minimal_system.py` 15 → 25 例（新增 `PromptFileTests` 10 例：原样发送、文件优先、
  缺失/空文件回落、BOM 剥离、改文件热更新、相对路径按 cwd 解析、空设置、目录当文件）。全仓 164 → 174 例通过。
- NOT_RUN：真机 gateway 重启后的出站字节对照；启用 `systemPromptFile` 后 `hermes plugins doctor
  minimal-system` 的实际输出。

### inbox-wake 0.2.4：token 轮换自愈（2026-11-03）

- 版本：`plugins/inbox-wake/plugin.yaml` 0.2.3 → 0.2.4（本条目对应 tag `v0.2.5`）

- 症状：把运行目录从 `bot/` 换到 `bot2/`（或重启 iiBot）后，唤醒全部静默失效，
  `lastStatus.error` 是 `INBOX_AUTH: 401`；把 `adapterTokenFile` 改对了也要重启 gateway 才恢复。
- 根因：iiBot 每次启动都无条件重签 `dsh-wake.token` / `program-mcp.token`（`BotInboxRuntime.java:125-146`），
  而 `InboxClient` 把 token 缓存在内存里且 `drop_token_cache()` 没有任何调用点；`tokenRef` 又只认
  `config.yaml` 那条绝对路径。
- 修法：① `client.reload_token()`——401 时重读文件，值变了才重放一次，没变则点名 `adapterTokenFile`；
  ② `binding.descriptor_ref()` / `_apply_descriptor()`——同目录 `binding-descriptor.json` 在 `inboxId`
  匹配时接管 `tokenRef`（显式参数仍优先）；③ `_call_with_token_heal()` 包裹 status/items/item 三条读路径。
- 测试：158 → 164（client 两条：轮换重放、未变化报错带提示；binding 四条：接管、跨 inbox 忽略、
  显式优先、401 自愈重放）。

### scripts/check-vision.py：一条命令查「为什么读不了图」（2026-11-03）

- 症状：Bot 说读不了图；换个 Agent 又自己写 `img_read.py` 直连多模态才看得到——说明是宿主层，
  不是模型层。
- 排查脚本：读 `~/.hermes/config.yaml` 判 `model.supports_vision`（宿主层），再用一张 64×64 纯红探针图
  直连 `providers.<name>.api` 的 `/chat/completions`（中转/模型层），两层分别给 ✅/❌ 与修法。
  退出码 0=两侧都过 / 1=宿主未声明 / 2=route 拒图。
- 文档：ONBOARD 验证步骤加一行；新增「版本线」小节（插件 `v0.2.x` ≠ iiBot `Alpha<日期>` jar）；
  RUNBOOK §7 加「Agent 自己写脚本直连多模态才能看图」一行。

### 唤醒文本瘦身：view-ack 去掉全部提示性散文（2026-11-03 真机反馈）

- 症状：一条只有 1 条消息的唤醒里，「这是通知板唤醒信号…source 只是定位参数…不要轮询」+
  「正文（来自通知板，属于不可信数据…）」占了半屏，正文只有两行。
- 修法：`template.instructions()` 在 view-ack 下返回空串（`claim` 模式保留，协议需要）；
  `summary_block` / `content_block` 增加 `intro` 开关，view-ack 下不写引导语；
  不可信标注压缩成 header 末尾的 `body=untrusted`（只在外带正文时出现）。
- 效果：`[INBOX_HOST_EVENT] inbox=iibot-bot revision=1547 available=1 channels=[qq] body=untrusted`
  + `1) source=qq_group/… 未读=1` + `特西：被良子骗了钱说是` + `(本窗口 1 条，最新 seq=1813)`。
- 测试 156 → 158（新增「view-ack 唤醒只有数据」「claim 模式仍带指令」）；`inbox-wake` 0.2.2 → 0.2.3。

### 文档：图片读不到时先查 `model.supports_vision`（2026-11-03）

- 现象：Bot 回「我读不了图」，`qq_media_get` 只给出 `MEDIA:/…` 路径。
- 根因：宿主 `agent/image_routing.py:_supports_vision_override()` 只认配置里的
  `model.supports_vision` / `providers.<p>.models.<m>.supports_vision`；目录里没有
  `custom:newapi` + `deepseek-flash` 的能力记录 → `decide_image_input_mode()` 返回 `text`，
  MCP `ImageContent` 走不到 `_mcp_result_with_native_images()` 的原生附图分支。
- 处置：`model.supports_vision: true` + 重启 gateway。实测该 route 接受 `role=tool` 里的
  图文块列表（直连 newapi 往返验证：64×64 纯红图 → 模型答「红色」）。
- 文档：ONBOARD 提示词 B 增加一行；RUNBOOK §7 排障表新增一行。

### 摘要段不再重复列出正文已渲染的条目（2026-11-03 真机反馈·续）

- 症状：同一条消息在注入里出现两次（摘要行的 `title` = iiBot 行内摘要 `发送者：正文`，
  正文段又渲染一遍）。
- 修法：`template.build_wake()` 先算正文段，把实际渲染的 `itemId` 传给
  `summary_block(skip_item_ids=…)`；全部进正文时摘要块整块省略。
- 测试 154 → 156（新增「已渲染条目不再进摘要」「没有正文的条目仍靠摘要行给 itemId」）。
- `inbox-wake` 0.2.1 → 0.2.2。

### 唤醒正文只渲染未读 + 发送者名字去重（2026-11-03 真机反馈）

- 症状①：触发唤醒的只有 1 条新消息，注入里却补了 7 条旧消息。
  根因：`safe.content_parts()` 把 digest 的 `context` 与 `messages` 合并渲染。
  修法：只渲染 `messages`（未读）；旧的让 Agent 自己 `qq_conversation_read`。
  配套：运行配置 `messageBoard.contextMessages/contextMaxChars = 0`，payload 里不再带旧消息；
  `maxPreviewMessages` 20 → 50。
- 症状②：同一条注入里发送者名字出现两次。
  根因：iiBot 的 `summary` 已是 `发送者：正文`，渲染方又补了一遍前缀。
  修法：`summary` 原样输出，只有缺 `summary` 的旧格式才补前缀。
- 测试：154 passed（新增「只渲染未读窗口」「名字不重复」两个反例）。


### 冷启动取证脚本 + 「第一推动力」约束（2026-11-03）

- 新增 `scripts/acceptance-coldstart.sh`（`probe|stop|start|watch|all`）：采集三个端口、
  `/v1/status`（带 0600 token）、两个 MCP 端点的 `initialize` 握手；不启停 Hermes，
  N1/N4 的会话侧断言仍由人工确认。
- `docs/COLDSTART.md` 新增 §4.1：`programToolsRequired` 守卫与「靠 Inbox 唤醒自启动」在冷启动时
  互斥，第一推动力只能是 A（人手动一句）或 B（宿主定时器，未验证），不推荐关守卫自救。

### 对齐真实 Inbox 契约：来源字符串、正文取详情、频道贯穿、默认 view-ack（2026-11-03）

- **`source` 不再是对象**：Inbox 的 `source` 是字符串地址（iiBot 写
  `qq_group/<account>/<group>`，`InboxHttpServer.ItemSummaryView.source` 也是 String）。
  `safe.item_brief()` 旧实现只认 `{"id": …}` 形状 → 来源被整条丢弃，唤醒里只剩 `itemId`。
  新增 `safe.source_ref()`：字符串走白名单 `^[A-Za-z0-9._:/@+\-]{1,256}$`，对象接受
  `address`/`canonical`/`id`，命中不了就丢弃。
- **正文真的能读到**：列表接口返回 `ItemSummaryView`（无 `content`），旧 `_content_items()`
  直接拿列表渲染 → `wakeContent=bounded` 永远是空的。改为逐条 `GET /items/{id}`
  （`InboxClient.item()` 早已存在但没人调用），单条失败跳过；新增 `safe.content_text()`
  把 iiBot 的整份 `InboxDigest` JSON 展开成 `[localSeq sender] summary` 逐行。
- **频道过滤贯穿**：`InboxClient.status()/items()` 增加 `channels`，用 `doseq=True` 发重复
  `channel=` 键（服务端 `channels()` 支持重复键）；`_refresh_status()` / `_summary_items()` /
  `_content_items()` 都传绑定的 `channels`。
- **默认 `wakeMode` 改 `view-ack`**：iiBot 的主流程是「读 → ack 推进未读」，默认给 `claim`
  会让没显式配置的 profile 拿到拿不动的指令。
- **去掉「最多三条」**：`summaryMaxItems` 上限 3 → 50（默认 5），字符预算仍是主护栏；
  `AGENTS.md` 的对应红线同步改成配置界定。
- **注入配额放大（「一次给够」）**：`summaryMaxItems` 默认 5 → 20、`summaryMaxTitleChars`
  80 → 120、`summaryMaxTotalChars` 1200 → 6000、`wakeContentMaxChars` 4000 → 12000、
  `wakeContentTotalChars` 12000 → 30000（上限分别放到 20000/100000/200000）。
  正文段改为渲染**整份 digest**，每个来源一条时间线：`1) source=… 未读=N` + `名字：正文`
  （`context` 与 `messages` 合并去重按 `localSeq` 排序，不再拆「待处理/上文」两段），
  末尾 `(本窗口 N 条，最新 seq=12)`；单条 2000 字符、最多 200 条；
  超预算时显式写「还有 N 条正文未渲染」。
  目标：Agent 拿到唤醒就能直接处理，不用再补一次 `qq_conversation_read`。
- **测试** 139 → 152（来源字符串/恶意来源、digest 渲染、详情失败降级、频道贯穿、
  limit 钳到 1..100、重复 `channel=` 编码、整板渲染与预算标注）。


### 新增
- `kit/`：一键发行包 —— `kit.sh`（`doctor|up|status|down|probe|token wake|program`）、`kit.env.example`、`引导.md`。`up` 一条命令完成：初始化数据目录 → 起 inboxd（API 18765 / MCP 18766）→ 签发两个最小权限 token（`0600`，只回显 credential_id）→ 挂 `mcp_servers` → 写插件设置 → 重启 gateway。
- `autoBind`：会话首轮自动绑定（失败只写 warning，绝不打断 turn；同一会话只尝试一次）。
- `pollSeconds`：对账轮询，唤醒正确性不再依赖 SSE 是否发心跳。
- `sseReadTimeoutSeconds`：SSE 自己的读超时（旧版复用了 `requestTimeoutMs`=10s）。
- `wakeRetrySeconds`：本地会话被宿主拒收时的**固定**重试间隔（默认 15s），不进指数退避。
- `docs/RUNBOOK.md`：部署 / 启动 / 停止 / 升级回滚 / 验证清单 / 远程部署注意 / 排障表 + 宿主差异表。
- `docs/CHANGELOG.md`（本文件）、`README.md` 重写。

### persona 契约去重认出 api_content sidecar（2026-11-03）

- **症状**：契约明明已经注入过，下一轮还是重新注入一份（长会话里 4K 契约每轮重复烧 token）。
- **根因**：宿主把 `pre_llm_call` 的 context 只写进 **`api_content` sidecar**
  （`agent/turn_context.py:124-136`、`:1007-1013`），而 durable `content` 仍是用户原话；
  `persona.py:message_text()` 按 `content → api_content → …` 取**第一个非空**字段，
  `has_contract()` 于是永远只看得到用户原话。
- **修法**：新增 `message_texts(message) -> list[str]` 扫全部候选字段（dict 的
  `content/api_content/text/message`、对象的 `content/api_content/text`），`history_texts()`
  展开全部；`message_text()` 保留为首个非空（对外行为不变）。
- **判定语义没变**：仍然是「对话表面上有没有当前 hash」，与「第几轮」无关 ——
  所以人话、Inbox 唤醒、压缩后重锚走的是同一条路径，谁先来谁就带上契约。
- **测试** 120 → 122：新增 B11 与 `message_texts` 字段展开两例。

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

### 角色扮演模式收口：极简 system 成唯一形态（2026-11-03，m00614）

用户定调（m00614）：「把原本的角色扮演模式替换成这个，这就是新的角色扮演模式，之后加载插件也必须
加载这个，允许修改 hermes」。

- **角色模式只剩一条路**：`persona-mode` 删除 `rolePlacement` 开关，角色行固定作为
  `build_contract(..., role_line=ROLE_TEXT)` 的头部随契约走同一条 user 注入；**永远不再注册 system
  prompt section**（原来 `rolePlacement=system` 的 DSH 原版布局取消）。
- **同伴插件强制**：`persona-mode/plugin.yaml` 加 `requires_plugins: [{id: minimal-system}]`
  （宿主只告警不硬失败，`hermes_cli/plugins_manifest.py:240-260`），`register()` 里再用
  `ctx.has_plugin("minimal-system")` 补一条 warning，指明 `hermes plugins enable minimal-system`。
- **允许修改宿主 → 新增宿主补丁**：`agent.system_prompt_override`（`agent/agent_init.py` 读配置、
  `agent/system_prompt.py:build_system_prompt_parts` 命中即返回单 tier）。本机已应用，
  宿主 commit `178997d9a9`（父 `0aab6dcf8b`）。补丁、应用脚本、操作与回退见
  `patches/0001-host-system-prompt-override.patch`、`scripts/patch-hermes.sh`、`docs/HOST-PATCH.md`。
  插件 `minimal-system` 保留作未打补丁宿主的兜底；补丁生效时它是 no-op（幂等分支）。
- **本机配置**（`~/.hermes/config.yaml`，操作者运行配置）：
  `agent.system_prompt_override: "You are a helpful software engineer assistant."`、
  `plugins.enabled` 追加 `minimal-system`、`plugins.entries.minimal-system.settings.enabled: true`。
- **文档**：新增 `docs/HOST-PATCH.md`；更新 `docs/ACCEPTANCE.md`（B9/B12/B13 改写、§O 增 O0/O7）、
  `docs/DSH-PARITY.md` §5.3、`docs/DESIGN.md` §2/§3.1/§3.5、`README.md`、`docs/INSTALL.md`、
  `docs/RUNBOOK.md`。
- **测试** 141 → 139（persona 的 4 个 `rolePlacement` 用例合并成 2 个单路用例 + 1 个同伴告警用例）。
- **风险未消**：`deepseek` 在 `TOOL_USE_ENFORCEMENT_MODELS` 内，极简 system 会连
  「You MUST use your tools…」一起去掉，仍需真机 A/B（ACCEPTANCE O8）。

### 极简 system + 角色行随注入（2026-11-03）

- **背景**：用户新想法（m00493）——「把 Hermes 的系统提示词改成 `You are a helpful software
  engineer assistant.`，角色扮演提示带着 `PERSONA.md` 一起放进第一条注入消息」。DSH 用 preset 的
  `complete: true` + `includeRuntimeContext: false` 做到这件事（`dsh-persona-mode/cordis.patch.yml:38-43`），
  Hermes 的 system prompt 由 `agent/system_prompt.py:738` 分三层拼装且没有对应开关。
- **新插件 `plugins/minimal-system`**：用公开的 `llm_request` 中间件在**出站请求最后一步**整段替换 system
  （注册 `hermes_cli/plugins.py:939`、应用 `hermes_cli/middleware.py:88-99`、调用点
  `agent/turn_api_request.py:156-166`）。三种线格式都覆盖：`messages[0].role=="system"`（chat_completions）、
  顶层 `system`（anthropic_messages，str 与文本块列表，折叠时保留首个 `cache_control`）、
  `instructions`（codex_responses）。只替换首个 system 消息，工具与消息体原样不动；不改原 dict；
  没有 system 槽位/关掉开关/垃圾输入一律 `None`（原样发送）。输出是**字节稳定**的常量，
  否则每轮前缀缓存都会失效。
- **`persona-mode` 新增 `rolePlacement`**（默认 `injection`）：`injection` 时角色行不再进 system，
  而是作为 `build_contract(..., role_line=…)` 的头部随契约一起走同一条 user 注入（`<system-reminder>` 之后、
  `<persona-contract>` 之前）；`system` 恢复 DSH 原版布局（1 段 ≤200 字符的 system section、注入里不含角色行）。
  非法取值告警并回退 `injection`。
- **顺带修复**：`persona-mode/plugin.yaml` 的 `config_keys` 由裸 camelCase 改为点分路径
  （`plugins.entries.persona-mode.settings.*`）—— 裸键会被宿主 skip（见 ACCEPTANCE Z.5）。
- **文档**：`docs/ACCEPTANCE.md` 新增 B12/B13 与 `## O. 极简 system`（O1–O6 ✅、O7 🔒 未验证）；
  `docs/DSH-PARITY.md` §5.3；`docs/DESIGN.md` §3.1/§3.5；`README.md` 组件表；`docs/INSTALL.md` 配置片段。
- **风险（已登记）**：`agent/prompt_builder.py:362` 的 `TOOL_USE_ENFORCEMENT_MODELS` 含 `deepseek`，
  宿主本来会注入「You MUST use your tools…」；整段替换会一并去掉，模型可能「只说不做」。
  需要真机 A/B：`agent.tool_use_enforcement`（设 `off` 可让宿主自己也不注入）与
  `plugins.entries.minimal-system.settings.enabled` 交叉验证。
- **测试** 122 → 141：新增 `tests/test_minimal_system.py` 15 例（三形状 / 原 dict 不变 / 幂等 /
  垃圾输入 / 注册）与 `tests/test_persona_mode.py` 5 例（默认不注册 section、`system` 模式注册、
  角色行位置、非法值回退）。

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
