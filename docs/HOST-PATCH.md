# 宿主补丁：极简 system prompt（`agent.system_prompt_override`）

> **0.4.0 提示（2026-11-07）**：插件已三合一为 `plugins/iibot-hermes`。本文写于合并前，文中 `inbox-wake` / `minimal-system` / `persona-mode` 的目录现为 `lib/wake` / `lib/prompt` / `lib/persona`，设置键现为 `plugins.entries.iibot-hermes.settings.{inbox,prompt,persona}.*`，`allow_gateway_injection` 在 `plugins.entries.iibot-hermes.allow_gateway_injection`。映射见 `docs/CHANGELOG.md` 的 0.4.0 条目。

状态：**已应用到本机**（host `~/.hermes/hermes-agent`，commit `178997d9a9`，父 `0aab6dcf8b`）。
补丁文件：`patches/0001-host-system-prompt-override.patch`；应用脚本：`scripts/patch-hermes.sh`。

## 1. 为什么改宿主

用户要求（`m00493` / `m00614`）：角色扮演模式的 system prompt 只有一句
`You are a helpful software engineer assistant.`，角色行与 `PERSONA.md` 一起走首条 user 注入。
DSH 用 preset 的 `complete: true` + `includeRuntimeContext: false` 做到这件事
（`iiBotMosire-DSH/packages/dsh-persona-mode/cordis.patch.yml:38-43`）；Hermes 没有这个开关，
system prompt 由 `agent/system_prompt.py:738` `build_system_prompt_parts` 分三层拼装
（身份/SOUL、工具指引、项目上下文、技能索引、memory、插件段、时间戳、运行时环境锚点）。

两条实现路径，本仓库两条都提供：

| 路径 | 落点 | 优点 | 缺点 |
| --- | --- | --- | --- |
| 宿主补丁（**首选**） | `agent/agent_init.py` 读配置 → `agent._system_prompt_override`；`build_system_prompt_parts` 命中即返回单 tier | 一次性生效、不占每轮链路、`_cached_system_prompt` 与线上字节一致 | 宿主升级会重置该目录，需要重打 |
| 插件 `minimal-system`（兜底） | `llm_request` 中间件在出站最后一步整段替换 | 不改宿主，升级无忧 | 每轮都跑、且发生在 prompt-cache 装饰之后 |

两条同时存在不冲突：补丁生效时请求里本来就是那句常量，中间件 `new_request == request` 直接
返回 `None`（`plugins/minimal-system/lib/rewrite.py` 的幂等分支）。

## 2. 补丁做了什么

```diff
 agent/agent_init.py       | 3 +++
 agent/system_prompt.py    | 7 +++++++
```

- `agent/agent_init.py` `_apply_agent_section()`：`agent._system_prompt_override =
  str(_agent_section.get("system_prompt_override") or "").strip()`（与 `tool_use_enforcement` /
  `execution_guidance` 同一处，键在 `config.yaml` 的 `agent:` 段）。
- `agent/system_prompt.py` `build_system_prompt_parts()` 开头：非空 override → 直接
  `return {"stable": override, "context": "", "volatile": ""}`，跳过身份、指引、项目上下文、
  技能索引、memory、插件段、时间戳、运行时环境锚点。

副作用（可接受，已核对）：

- `reconstruct_static_prefix()` 用 `stored.startswith(static)` 判前缀；override 时 `static` 就是
  那句常量，条件恒成立。
- `agent/surface_switch.py:36` 只因 `prompt.endswith(RUNTIME_ENVIRONMENT_END)` 才拆分身份/运行时块；
  override 的 prompt 自然走「整体当身份」分支，行为正常。
- 项目上下文文件（`AGENTS.md` 等）不再进 system；需要模型知道的约束必须写进 `PERSONA.md`。

## 3. 配置

```yaml
agent:
  system_prompt_override: "You are a helpful software engineer assistant."
plugins:
  enabled: [billion-context, inbox-wake, minimal-system, persona-mode]
```

`persona-mode` 在 `plugin.yaml` 里用 `requires_plugins: [{id: minimal-system}]` 声明同伴；
宿主加载器对缺失依赖只告警不硬失败（`hermes_cli/plugins_manifest.py:240-260`），所以
`persona-mode/__init__.py:_warn_missing_companion()` 还会在 `register()` 时用 `ctx.has_plugin`
再喊一次。

## 4. 操作

```bash
./scripts/patch-hermes.sh check    # 0=已打 / 1=可打 / 3=宿主源码已变需人工看
./scripts/patch-hermes.sh apply    # 幂等
./scripts/patch-hermes.sh revert   # 幂等撤销
```

`hermes` 升级（`~/.hermes/hermes-agent` 被重置）之后重跑 `apply` 即可；若 `apply` 报冲突，
说明上游改动了 `build_system_prompt_parts`，按第 2 节的语义手工重贴，并把新 diff 回写
`patches/0001-host-system-prompt-override.patch`。

## 5. 怎么验证

1. `./scripts/patch-hermes.sh check` → `已应用`。
2. 新会话随便问一句，抓会话文件（`$HERMES_HOME/sessions/*.jsonl`）里第一条 system：
   应当**只有**那句常量，不含 "Hermes Agent" / "Tool-use enforcement" / "RUNTIME ENVIRONMENT"。
3. 同一条 user 消息的 `api_content` 里应出现 `<system-reminder>` + 角色行 + `<persona-contract>`。
4. 真机观察工具调用是否仍正常（`docs/ACCEPTANCE.md` §O 的 O8，风险项）。

## 6. 回退

```bash
./scripts/patch-hermes.sh revert
# 或者：git -C ~/.hermes/hermes-agent revert 178997d9a9
```
回退后把 `agent.system_prompt_override` 从 `config.yaml` 删掉（留着也无害：旧代码不读这个键），
system prompt 回到宿主原样；此时若想要极简提示词，只剩 `minimal-system` 中间件这一条路。

## 7. 图片必达：为什么 0.4.0-fix1 把它做成插件（而不是第二个补丁）

角色模式的 system prompt 有两条路（补丁优先、插件兜底）；图片这边只保留**插件**一条，原因是
补丁面太大、收益太小：

| 方案 | 落点 | 结论 |
| --- | --- | --- |
| 宿主补丁 `hoist_tool_result_images` | `agent/tool_dispatch_helpers.py:373`（本机 `v0.22.5+3378.g178997d` 的未提交改动，13 文件） | 只在**本机**有效；远程官方 tag 没有，升级即重置 |
| 插件 `llm_request` 中间件 | `plugins/iibot-hermes/lib/vision/hoist.py` + `__init__.py:445` | **采用**：宿主公开契约（`hermes_cli/middleware.py:88`），不改宿主、升级无忧，远程同一份代码即可生效 |

代价：多一次 `llm_request` 回调（线性扫一遍消息列表；无图时立刻返回 `None`）。因此补丁里那段
`hoist_tool_result_images` 只作为本机开发期的参考实现保留，判断依据是 `docs/DESIGN.md` 第 6 节。
