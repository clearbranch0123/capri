"""
CAPRI // Cyberpunk AI Companion for Linux (Niri Wayland / Hyprland / Arch / CachyOS / Fedora)
Construct Relic Engram — VOICE 3.0, FOCUS & ARCH ECOSYSTEM (v3.6.0)
"""

__version__ = "3.6.0"
__app_name__ = "Capri"
__codename__ = "Project Capricorn // Relic Engram"
__author__ = "D"

from .core.config import AppConfig, load_config, save_config
from .core.agent import CapriAgent, AgentEvent

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
