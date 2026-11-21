from .registry import registry, tool

# Import modules so their @tool decorators register them automatically
from . import desktop
from . import shell
from . import screen
from . import web
from . import browser
from . import media
from . import fs
from . import memory
from . import timers
from . import context
from . import initiative

__all__ = [
    "registry",
    "tool",
    "desktop",
    "shell",
    "screen",
    "web",
    "browser",
    "media",
    "fs",
    "memory",
    "timers",
    "context",
    "initiative",
]
