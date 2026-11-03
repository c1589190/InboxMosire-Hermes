"""Import helpers: the plugin directories are hyphenated, so they are loaded as synthetic packages."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


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


def persona_plugin() -> Any:
    return load_package("persona_mode_plugin", ROOT / "plugins" / "persona-mode")


def inbox_plugin() -> Any:
    return load_package("inbox_wake_plugin", ROOT / "plugins" / "inbox-wake")


def inbox_lib(name: str) -> Any:
    inbox_plugin()  # ensure the parent package is imported first
    return importlib.import_module(f"inbox_wake_plugin.lib.{name}")


class MemState:
    """Stand-in for ``ctx.state`` (profile-scoped JSON facade)."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class FakeCtx:
    """Just enough of ``PluginContext`` to exercise ``register()``."""

    def __init__(self, config: dict | None = None, accept_injection: bool = True) -> None:
        self.config = config or {}
        self.accept_injection = accept_injection
        self.state = MemState()
        self.tools: dict[str, dict] = {}
        self.commands: dict[str, Any] = {}
        self.hooks: dict[str, list] = {}
        self.middleware: dict[str, list] = {}
        self.sections: list[tuple[str, Any, dict]] = []
        self.injected: list[tuple[str, str]] = []
        self.unload: Any = None
        self.plugin_id = "test-plugin"

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

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
