"""Modular subcomponents extracted from `capri.ui.cyberdeck`."""
from __future__ import annotations

import sys
from typing import Any


def cyberdeck_hook(attr: str, default: Any) -> Any:
    """Resolve a module-level hook on `capri.ui.cyberdeck` when patched in tests."""
    mod = sys.modules.get("capri.ui.cyberdeck")
    if mod is not None and hasattr(mod, attr):
        return getattr(mod, attr)
    return default
