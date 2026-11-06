"""Implementation modules for the iibot-hermes plugin (no import-time side effects).

``wake/`` is the InboxMosire wake adapter (was ``plugins/inbox-wake/lib``), ``prompt/`` the
system-prompt rewrite (was ``plugins/minimal-system/lib``), ``persona/`` the role-play
injection and guard (was ``plugins/persona-mode/lib``). ``settings.py`` resolves the three
sections of ``plugins.entries.iibot-hermes.settings`` with the 0.x namespaces as a read-only
fallback.
"""
