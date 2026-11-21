"""
CAPRI // Package-safe runtime asset resolver.
Works identically in source checkouts (`capri/assets`), editable installs,
and standalone wheel installs (`site-packages/capri/assets`), with graceful
fallback to a legacy top-level `assets/` directory if present.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = PACKAGE_DIR.parent


def get_assets_dir() -> Path:
    """Return the active runtime assets directory (preferring packaged `capri/assets`)."""
    pkg_assets = PACKAGE_DIR / "assets"
    if pkg_assets.exists():
        return pkg_assets
    legacy_assets = ROOT_DIR / "assets"
    if legacy_assets.exists():
        return legacy_assets
    return pkg_assets


def get_asset_path(*parts: str) -> Path:
    """Resolve a relative asset path inside the runtime assets directory."""
    return get_assets_dir().joinpath(*parts)


def resolve_asset_path(path_like: str | Path) -> Path:
    """
    Resolve a configured asset path (e.g. legacy `'assets/capri_voice_clean.wav'`,
    bare `'capri_voice_clean.wav'`, or an absolute path).
    """
    raw = Path(path_like).expanduser()
    if raw.is_absolute() and raw.exists():
        return raw
    if raw.exists():
        return raw
    parts = raw.parts
    if parts and parts[0] == "assets":
        candidate = get_assets_dir().joinpath(*parts[1:])
        if candidate.exists():
            return candidate
    candidate = get_assets_dir() / raw
    if candidate.exists():
        return candidate
    return raw
