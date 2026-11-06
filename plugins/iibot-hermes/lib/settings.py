"""Where ``iibot-hermes`` reads its three settings sections (and the legacy fallback).

The plugin owns exactly one namespace now::

    plugins.entries.iibot-hermes.settings.inbox    # wake adapter
    plugins.entries.iibot-hermes.settings.prompt   # system-prompt rewrite
    plugins.entries.iibot-hermes.settings.persona  # role-play injection / guard

Before 1.0.0 those sections were three plugins with their own entries
(``plugins.entries.inbox-wake.settings`` / ``minimal-system`` / ``persona-mode``).
An operator who upgrades the code but not ``config.yaml`` must keep working, so a
missing section falls back to the old entry and logs one WARNING naming both paths.
The legacy values are **read only**: this module never writes anything back.

Reading uses two host surfaces, in this order:

* ``plugins.entries.<id>.settings`` from the whole host config
  (``hermes_cli/config.py:1985 load_config_readonly``; a ``PluginContext`` cannot
  address *another* plugin's entry, so the raw config mapping is the only route to
  the legacy namespaces). A context that exposes ``load_host_config()`` (tests) is
  preferred over the host import.
* ``ctx.get_config("<section>", None)`` — the public plugin API
  (``hermes_cli/plugins.py:266``) — when the config file is not readable at all.

No token is read, formatted or logged here: the sections only carry paths/ids.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

#: Registry id of this plugin (== ``plugin.yaml`` ``name`` and the directory).
PLUGIN_ID = "iibot-hermes"

#: Section name -> entry id it used to live under.
LEGACY_IDS: dict[str, str] = {
    "inbox": "inbox-wake",
    "prompt": "minimal-system",
    "persona": "persona-mode",
}

SECTIONS: tuple[str, ...] = tuple(LEGACY_IDS)


def load_host_config(ctx: Any) -> Mapping[str, Any]:
    """The whole host config mapping, or ``{}`` when it cannot be read.

    Never raises: a missing/unreadable ``config.yaml`` must degrade to defaults, not
    abort plugin registration (the host would then silently drop every tool).
    """
    getter = getattr(ctx, "load_host_config", None)
    if callable(getter):
        try:
            raw = getter()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("iibot-hermes: load_host_config() failed: %s", exc)
            raw = None
        if isinstance(raw, Mapping):
            return raw
    try:
        from hermes_cli.config import load_config_readonly

        raw = load_config_readonly()
    except Exception as exc:  # tests / non-host processes have no hermes_cli
        logger.debug("iibot-hermes: host config unavailable: %s", exc)
        return {}
    return raw if isinstance(raw, Mapping) else {}


def entry(ctx: Any, plugin_id: str, host: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
    """``plugins.entries.<plugin_id>`` as a mapping (empty when absent)."""
    config = host if host is not None else load_host_config(ctx)
    try:
        plugins = config.get("plugins") or {}
        entries = plugins.get("entries") or {}
        value = entries.get(plugin_id)
    except AttributeError:
        return {}
    return value if isinstance(value, Mapping) else {}


def _entry_settings(ctx: Any, plugin_id: str, host: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    raw = entry(ctx, plugin_id, host).get("settings")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _assign(target: dict[str, Any], path: list[str], value: Any) -> None:
    node = target
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = value


def _section_view(settings: Mapping[str, Any], section: str) -> tuple[dict[str, Any], bool]:
    """``settings.inbox`` plus literal ``"inbox.apiUrl"`` keys, and whether it exists at all.

    ``hermes config set plugins.entries.<id>.settings.inbox.apiUrl …`` writes a nested
    mapping, but a hand-edited file (or an older ``config set``) may carry the dotted
    key verbatim. Both spellings mean the same thing, so accept both.
    """
    view: dict[str, Any] = {}
    present = False
    direct = settings.get(section)
    if isinstance(direct, Mapping):
        view.update(direct)
        present = True
    prefix = section + "."
    for key, value in settings.items():
        if isinstance(key, str) and key.startswith(prefix):
            rest = key[len(prefix):]
            if rest:
                _assign(view, rest.split("."), value)
                present = True
    return view, present


def load_section(ctx: Any, section: str) -> dict[str, Any]:
    """Settings for one section, new namespace first, legacy entry as read-only fallback."""
    if section not in LEGACY_IDS:
        raise KeyError(f"unknown settings section {section!r}")
    legacy_id = LEGACY_IDS[section]
    host = load_host_config(ctx)

    own = _entry_settings(ctx, PLUGIN_ID, host)
    new, present = _section_view(own, section)
    if not present:
        # The config file may be unreadable in this process; the public ctx API still works.
        try:
            value = ctx.get_config(section, None)
        except Exception:  # pragma: no cover - defensive
            value = None
        if isinstance(value, Mapping):
            new = dict(value)
            present = True

    legacy = _entry_settings(ctx, legacy_id, host)
    if legacy:
        if present:
            logger.warning(
                "iibot-hermes: plugins.entries.%s.settings 仍存在但已被忽略——"
                "设置现在从 plugins.entries.%s.settings.%s 读取；确认无误后请删除旧段",
                legacy_id, PLUGIN_ID, section,
            )
        else:
            logger.warning(
                "iibot-hermes: 设置 %s 已迁移到 plugins.entries.%s.settings.%s；"
                "本次回落读取旧 plugins.entries.%s.settings（只读，不会写回；迁移见 docs/INSTALL.md）",
                section, PLUGIN_ID, section, legacy_id,
            )
            return legacy
    return new


def legacy_gateway_flag(ctx: Any) -> bool:
    """``entries.inbox-wake.allow_gateway_injection`` still set (host now reads the new entry)."""
    return entry(ctx, LEGACY_IDS["inbox"]).get("allow_gateway_injection") is True


def own_gateway_flag(ctx: Any) -> bool:
    """``entries.iibot-hermes.allow_gateway_injection`` (a first-level entry key, not a setting)."""
    return entry(ctx, PLUGIN_ID).get("allow_gateway_injection") is True
