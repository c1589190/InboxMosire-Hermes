# Hermes 宿主能力探测

本文件记录**代码级核对过**的 Hermes 侧能力，以及因此做出的降级决策。
对照物是 `InboxMosire-DSH/docs/HOST-CAPABILITIES.md`（DSH 0.2.1-rc.2 的能力清单）。

探测基线：本机 `/home/cna/.hermes/hermes-agent/` 源码树（`pyproject.toml` 里
`version = "0.0.0"`，无法用它判断版本号）。所有行号指该源码树。

## 1. 已确认可用

| 能力 | 入口 | 说明 |
|---|---|---|
| 注册工具 | `hermes_cli/plugins.py:463` `register_tool(name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False, description="", emoji="", override=False)` | 覆盖内置工具需 `plugins.entries.<id>.allow_tool_override` |
| 注册 hook | `hermes_cli/plugins.py:935` `register_hook(hook_name, callback, *, hold_stream=False)` | 合法名 = `hermes_cli.plugins.VALID_HOOKS`；回调必须吃 `**kwargs` |
| system prompt 段 | `hermes_cli/plugins.py:959` `register_system_prompt_section(id, content, *, position="after_memory", max_chars=4000)` | `id` 1–128 位小写标识符；`content` 可为 `Callable[[Mapping], str]`；**每 session 只渲染一次并冻结** |
| 段上限 | `hermes_cli/plugins_dispatch.py:98-102` | `positions={"after_memory"}`、单段 ≤4000、总 ≤8000 字符、≤32 段（超限跳过并 warning，`:671`/`:682`） |
| 注入会话消息 | `hermes_cli/plugins.py:588` `inject_message(content, role="user", *, session_key=None, origin=None) -> bool` | 实现 `hermes_cli/plugins_injection.py:11` |
| 注入授权 | `hermes_cli/plugins_injection.py` `ctx._gateway_injection_allowed()` | 读 `plugins.entries.<id>.allow_gateway_injection is True`，**默认 False** |
| 会话身份 | `hermes_cli/plugin_session.py` `session_key()` = `get_session_env("HERMES_SESSION_KEY")`、`session_cwd()`、`session_surface()` | 只在**turn 进行中**才有值；turn 外拿到空串 |
| 会话内命令 | `hermes_cli/plugins.py:649` `register_command(name, handler, description="", args_hint="", argument_mode=None)` | handler `(raw_args) -> str \| None` |
| 工具拦截 | `hermes_cli/plugins.py:2019` `_dispatch_pre_tool_call_hooks(...) -> (block_message, modified_args)` | hook 返回 `{"action": "block", "message": …}` / `"approve"` / `"modify"`；MCP 工具名带 `mcp__<server>__` 前缀 |
| 插件设置读写 | `hermes_cli/plugins.py:266` `get_config(key, default)` / `:278` `set_config` | 读 `plugins.entries.<id>.settings.<key>`，回退旧 `config` 子树 |
| 插件私有状态 | `hermes_cli/plugins.py` → `ctx.state`（`PluginState(plugin_id, manifest.data_namespace)`） | JSON facade，落 `<HERMES_HOME>/plugin-data/<id>/` |
| manifest v2 | `hermes_cli/plugins_manifest.py:36` | 含 `config_schema`（`:142-155` 只收 mapping 条目并校验 `type`）、`python_dependencies`、`api_version`、`requires_plugins` |
| MCP 挂载 | `mcp_servers.<name>.url` + `headers`（`tools/mcp_tool_transport.py:119`、`tools/mcp_tool_config.py:591`）、`tools.exclude` | 可挂 InboxMosire 的 HTTP MCP 端点 |
| 手工校验 | `hermes plugins doctor [path-or-id] [--ci]`、`hermes plugins validate <path> --install-deps` | 临时 HERMES_HOME，检查 manifest/registry 漂移 |

## 2. 缺失或受限（决定了降级设计）

| 想要的 DSH 能力 | Hermes 现状 | 本仓库的处置 |
|---|---|---|
| 用 preset **替换**整个 system prompt（`complete: true`） | 系统提示归核心所有，插件段只能**追加**到 `after_memory` | 角色位用 1 段常量；正文改走 `pre_llm_call` |
| preset 级工具集收窄（Minimal） | 无 preset 概念 | 默认不收窄；可选 `deny_tools` 经 `pre_tool_call` 屏蔽 |
| `agent/status: idle \| running` 事件 | 无 | `pre_llm_call` = running、`post_llm_call`/`on_stream_end` = idle + 补投 `pendingWake` |
| `agent.followup(msg)` 幂等键 | `inject_message` 无幂等键 | 自管水位（`lastWokenRevision`/`pendingWake`），与 DSH 同策略 |
| 插件服务注册表（`inboxWakeProfiles`） | 无 | wake profile 作为插件内置常量 |
| panel 挂载/卸载 → suspend/resume | 无 panel | 不实现；会话结束/重置/reset 时停 SSE |
| config 里的 `${ENV}` 插值 | 未发现（`hermes_cli` config 加载不做展开） | token 走**文件路径**（`adapterTokenRef`），MCP 那行必须写明文 token → config.yaml 需 0600 |
| 项目内 `./.hermes/plugins/` 自动加载 | gateway 只在进程启动时发现一次插件 | 必须装到 `$HERMES_HOME/plugins/<id>/` 并在 `config.yaml` `plugins.enabled` 里点名 |

## 3. hook 语义要点

- `pre_llm_call` 的 kwargs 至少含 `session_id`、`user_message`、`conversation_history`
  （`agent/turn_context.py:849-880`）；返回值若是 `str` 或 `{"context": …}`，会被**追加到本轮
  user message**（既保 prompt cache，又不会像 system prompt 那样冻结）。
- 返回 `{"action": "block", "message": …}` 可在推理前否决本轮。
- 单 hook 上下文默认 10000 字符上限，超出部分落
  `$HERMES_HOME/hook_outputs/<session_id>/<uuid>.txt`（所以人设正文要自己截断，不要依赖它）。
- 超 `plugins.hook_callback_timeout`（默认 30s）被放弃：`pre_tool_call` 超时按**封禁**处理，
  其它 fail-open。→ 插件里**不许**在 hook 中做网络 IO；SSE 与 HTTP 全在 worker 线程。
- 会话类 hook：`on_session_start` / `on_session_end` / `on_session_finalize` /
  `on_session_reset` / `on_session_delete` / `post_compaction`。

## 4. 复现方法

```bash
cd ~/.hermes/hermes-agent
grep -n "def register_tool\|def register_hook\|def register_system_prompt_section\|def inject_message" \
     hermes_cli/plugins.py
sed -n '98,102p' hermes_cli/plugins_dispatch.py
sed -n '2019,2026p' hermes_cli/plugins.py
cat hermes_cli/plugin_session.py
```

## 5. 环境限制（本机实测）

- 本开发者的沙箱只能写 `/tmp` 与工作区；`touch ~/.hermes/x` 返回 `Permission denied`，
  故 `hermes plugins doctor` / 真正启用插件必须在**用户自己的终端**执行。
- `hermes --version` 当前直接报错：
  `hermes: install state is not writable by this user (/home/cna/.hermes/installs/c5df6068dc9882f7/.recovery.lock); run as the install owner or grant write access`
  → 启用插件前要先解决该 install 目录的所有权/写权限。
