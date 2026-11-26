"""
CAPRI // Cyberpunk AI Companion for Linux (Niri Wayland / Hyprland / Arch / CachyOS / Fedora)
Construct Relic Engram — VOICE 3.0, FOCUS & ARCH ECOSYSTEM (v3.7.0)
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "3.7.0"
__app_name__ = "Capri"
__codename__ = "Project Capricorn // Relic Engram"
__author__ = "D"

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AppConfig": (".core.config", "AppConfig"),
    "load_config": (".core.config", "load_config"),
    "save_config": (".core.config", "save_config"),
    "CapriAgent": (".core.agent", "CapriAgent"),
    "AgentEvent": (".core.agent", "AgentEvent"),
}

__all__ = [
    "__version__",
    "__app_name__",
    "__codename__",
    "__author__",
    "AppConfig",
    "load_config",
    "save_config",
    "CapriAgent",
    "AgentEvent",
]


def __getattr__(name: str) -> Any:
    spec = _LAZY_EXPORTS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = spec
    value = getattr(importlib.import_module(mod_name, __name__), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
