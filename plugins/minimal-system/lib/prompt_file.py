"""Operator-authored system-prompt file (markdown or plain text).

The file is sent **verbatim** as the outbound system prompt, so it is just text: headings,
lists and code fences keep working because nothing is parsed or rendered. Two consequences
drive this module:

* the bytes must be identical on every turn (a changing system prompt invalidates the
  provider's prefix cache), so the body is cached by ``(mtime, size)`` and only re-read when
  the file actually changes;
* a missing, unreadable or blank file must never break a turn, so every I/O problem returns
  ``None`` and the caller falls back to the inline ``systemPrompt`` setting.

Mirrors ``plugins/persona-mode/lib/persona.py``'s ``PersonaReader``/``resolve_path`` so both
plugins behave the same way when the operator points them at a file.
"""
from __future__ import annotations

import os
from typing import Optional

#: The operator's own prompt file lives in Hermes' home, next to ``SOUL.md`` / ``config.yaml``.
DEFAULT_FILE_NAME = "SYSTEM.md"


def hermes_home() -> str:
    """The running Hermes home, in the same order the host itself resolves it.

    ``HERMES_HOME`` wins (the gateway binds it per profile), then the host's own
    ``get_hermes_home()`` when we happen to run in-process, then ``~/.hermes``.
    """
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return os.path.expanduser(home)
    try:  # in-process with the host: its resolver also knows about profiles
        from hermes_cli.config import get_hermes_home  # type: ignore

        return str(get_hermes_home())
    except Exception:
        return os.path.expanduser(os.path.join("~", ".hermes"))


def default_prompt_path() -> str:
    """``$HERMES_HOME/SYSTEM.md`` — the default when ``systemPromptFile`` is left empty."""
    return os.path.join(hermes_home(), DEFAULT_FILE_NAME)


def resolve_path(path: str, base: str = "") -> str:
    """Absolute config path wins; otherwise resolve against *base* (default: process cwd).

    A relative path is ambiguous in a gateway whose cwd is the install root and is shared by
    every session, so absolute paths are what ``docs/INSTALL.md`` recommends; the relative
    form only exists to keep hand-written configs readable.
    """
    path = os.path.expanduser(str(path or "").strip())
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base or os.getcwd(), path))


class PromptReader:
    """Read + cache one prompt file by ``(mtime, size)``; never raises on I/O errors."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._key: Optional[tuple[float, int]] = None
        self._text: Optional[str] = None

    def read(self) -> Optional[str]:
        """Return the file body, or ``None`` when it is missing / unreadable / empty.

        ``utf-8-sig`` so a BOM-prefixed file does not leak ``\ufeff`` into the system prompt;
        ``strip()`` because leading/trailing blank lines would only churn the prefix cache.
        """
        if not self.path:
            return None
        try:
            stat = os.stat(self.path)
        except OSError:
            self._key, self._text = None, None
            return None
        key = (stat.st_mtime, stat.st_size)
        if key == self._key:
            return self._text
        try:
            with open(self.path, encoding="utf-8-sig") as handle:
                body = handle.read()
        except (OSError, UnicodeDecodeError):
            self._key, self._text = key, None
            return None
        self._key = key
        self._text = body.strip() or None
        return self._text
