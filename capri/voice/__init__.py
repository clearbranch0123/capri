from __future__ import annotations

import importlib
from typing import Any

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "VoiceSynthesizer": (".tts", "VoiceSynthesizer"),
    "SpeechRecognizer": (".stt", "SpeechRecognizer"),
    "WakeWordListener": (".wake", "WakeWordListener"),
}

__all__ = ["VoiceSynthesizer", "SpeechRecognizer", "WakeWordListener"]


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
