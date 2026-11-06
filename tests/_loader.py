"""Import helpers: the plugin directory is hyphenated, so it is loaded as a synthetic package.

1.0.0 merged ``inbox-wake`` + ``minimal-system`` + ``persona-mode`` into the single
``plugins/iibot-hermes`` package; it is imported once as ``iibot_hermes_plugin`` and the
old helper names (``inbox_plugin`` / ``persona_plugin`` / ``inbox_lib``) are thin aliases
so a test only has to change *where* it asks, not how.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "iibot-hermes"
PACKAGE = "iibot_hermes_plugin"
PLUGIN_DIR = ROOT / "plugins" / PLUGIN_ID


def load_package(module_name: str, directory: Path) -> Any:
    init = directory / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        module_name, init, submodule_search_locations=[str(directory)]
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def plugin() -> Any:
    """The merged plugin package (``plugins/iibot-hermes``)."""
    existing = sys.modules.get(PACKAGE)
    if existing is not None:
        return existing
    return load_package(PACKAGE, PLUGIN_DIR)


#: Old call sites keep working: there is one plugin now.
def persona_plugin() -> Any:
    return plugin()


def inbox_plugin() -> Any:
    return plugin()


def prompt_plugin() -> Any:
    return plugin()


def inbox_lib(name: str) -> Any:
    """``inbox_wake_plugin.lib.<name>`` -> ``iibot_hermes_plugin.lib.wake.<name>``."""
    plugin()  # ensure the parent package is imported first
    return importlib.import_module(f"{PACKAGE}.lib.wake.{name}")


def prompt_lib(name: str) -> Any:
    plugin()
    return importlib.import_module(f"{PACKAGE}.lib.prompt.{name}")


def persona_lib(name: str) -> Any:
    plugin()
    return importlib.import_module(f"{PACKAGE}.lib.persona.{name}")


def settings_lib() -> Any:
    plugin()
    return importlib.import_module(f"{PACKAGE}.lib.settings")


class MemState:
    """Stand-in for ``ctx.state`` (profile-scoped JSON facade)."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class FakeCtx:
    """Just enough of ``PluginContext`` to exercise ``register()``.

    ``settings`` is the plugin's own subtree — ``plugins.entries.iibot-hermes.settings`` —
    and ``get_config`` resolves dotted keys through it exactly like the host does
    (``hermes_cli/plugins.py:266``). ``legacy`` holds the 0.x entries
    (``{"inbox-wake": {...}, "minimal-system": {...}, "persona-mode": {...}}``) and is
    exposed through ``load_host_config()``, the surface ``lib/settings.py`` uses for the
    read-only legacy fallback. ``entries`` sets non-settings keys on this plugin's entry
    (``allow_gateway_injection``) or whole legacy entries verbatim.
    """

    def __init__(
        self,
        settings: dict | None = None,
        accept_injection: bool = True,
        legacy: dict[str, dict] | None = None,
        entries: dict[str, dict] | None = None,
    ) -> None:
        self.settings: dict[str, Any] = dict(settings or {})
        self.legacy: dict[str, dict] = dict(legacy or {})
        self.entries: dict[str, dict] = dict(entries or {})
        self.accept_injection = accept_injection
        self.state = MemState()
        self.tools: dict[str, dict] = {}
        self.commands: dict[str, Any] = {}
        self.hooks: dict[str, list] = {}
        self.middleware: dict[str, list] = {}
        self.sections: list[tuple[str, Any, dict]] = []
        self.injected: list[tuple[str, str]] = []
        self.unload: Any = None
        self.plugin_id = PLUGIN_ID

    # -- host surfaces -------------------------------------------------------------
    def get_config(self, key: str, default: Any = None) -> Any:
        node: Any = self.settings
        for part in str(key).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def load_host_config(self) -> dict:
        entries: dict[str, Any] = {
            legacy_id: {"settings": dict(values)}
            for legacy_id, values in self.legacy.items()
        }
        own = dict(self.entries.get(PLUGIN_ID) or {})
        settings = dict(own.pop("settings", {}))
        settings.update(self.settings)
        own["settings"] = settings
        entries[PLUGIN_ID] = own
        for legacy_id, entry in self.entries.items():
            if legacy_id != PLUGIN_ID:
                entries[legacy_id] = dict(entry)
        return {"plugins": {"entries": entries}}

    # -- registration surfaces -----------------------------------------------------
    def register_tool(self, **kwargs: Any) -> None:
        self.tools[kwargs["name"]] = kwargs

    def register_command(self, name: str, handler=None, **kwargs: Any) -> None:
        self.commands[name] = handler

    def register_hook(self, name: str, callback) -> None:
        self.hooks.setdefault(name, []).append(callback)

    def register_middleware(self, kind: str, callback) -> None:
        self.middleware.setdefault(kind, []).append(callback)

    def register_system_prompt_section(self, section_id: str, content: Any, **kwargs: Any) -> None:
        self.sections.append((section_id, content, kwargs))

    def inject_message(self, content: str, session_key: str | None = None, origin=None) -> bool:
        if not self.accept_injection:
            return False
        self.injected.append((content, session_key or ""))
        return True

    def on_unload(self, callback) -> None:
        self.unload = callback
