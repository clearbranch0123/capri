"""
CAPRI // Desktop Presence Engine (v3.0 — Living Presence 3.0).
Zero-Qt, pure & unit-testable detection layer for:
- MPRIS media playback (Spotify, browsers, mpv, vlc, etc. via `playerctl`)
- Game Mode / Power Saver (Niri & Hyprland fullscreen + active game runtime detection)
- Deterministic automatic avatar pose state machine (`perch_music`, `perch`, `sleep`, `normal`)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from .compositor import compositor, detect_compositor

_PREFERRED_MUSIC_PLAYERS = frozenset({
    "spotify",
    "spotifyd",
    "ncspot",
    "cider",
    "rhythmbox",
    "amberol",
    "elisa",
    "lollypop",
    "strawberry",
    "audacious",
    "cmus",
    "deadbeef",
    "mpv",
    "vlc",
    "harmonoid",
    "youtube-music",
    "pear-desktop",
})

_CAPRI_OWN_APPS = frozenset({
    "capri-avatar",
    "capri-cyberdeck",
    "capri-dashboard",
    "capri-terminal",
})

_GAME_APP_PREFIXES = (
    "steam_app_",
    "gamescope",
    "retroarch",
    "rpcs3",
    "pcsx2",
    "dolphin-emu",
    "ryujinx",
    "yuzu",
    "cemu",
    "ppsspp",
    "osu!",
    "minecraft",
)

_GAME_APP_EXACT = frozenset({
    "gamescope",
    "retroarch",
    "cs2",
    "dota2",
    "hl2_linux",
    "portal2_linux",
    "factorio",
    "terraria",
    "stardewvalley",
    "balatro",
    "celeste",
    "hollow_knight",
    "cyberpunk2077",
    "eldenring",
    "witcher3",
    "lutris-wrapper",
})


@dataclass(frozen=True)
class MediaPlaybackState:
    """Normalized snapshot of active desktop media playback (MPRIS)."""

    is_playing: bool = False
    status: str = "Stopped"
    player: str = ""
    artist: str = ""
    title: str = ""

    @property
    def track_label(self) -> str:
        a = self.artist.strip()
        t = self.title.strip()
        if a and t:
            return f"{a} — {t}"
        return t or a


@dataclass(frozen=True)
class GameModeState:
    """Normalized snapshot of fullscreen / Game Mode Power Saver state."""

    active: bool = False
    reason: str = ""
    app_id: str = ""
    title: str = ""
    is_fullscreen: bool = False


def parse_mpris_lines(raw: str) -> MediaPlaybackState:
    """
    Parse lines emitted by:
      playerctl -a metadata --format "{{status}}\\t{{playerName}}\\t{{artist}}\\t{{title}}"
    Prefers dedicated music players over generic browsers if multiple streams are playing.
    """
    if not raw or not raw.strip():
        return MediaPlaybackState()

    playing_candidates: list[MediaPlaybackState] = []
    paused_candidate: Optional[MediaPlaybackState] = None

    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0].strip() if len(parts) > 0 else ""
        player = parts[1].strip() if len(parts) > 1 else ""
        artist = parts[2].strip() if len(parts) > 2 else ""
        title = parts[3].strip() if len(parts) > 3 else ""
        state = MediaPlaybackState(
            is_playing=(status.lower() == "playing"),
            status=status or "Stopped",
            player=player,
            artist=artist,
            title=title,
        )
        if state.is_playing:
            playing_candidates.append(state)
        elif paused_candidate is None and status.lower() == "paused":
            paused_candidate = state

    if playing_candidates:
        for cand in playing_candidates:
            p_base = cand.player.lower().split(".")[0]
            if p_base in _PREFERRED_MUSIC_PLAYERS:
                return cand
        return playing_candidates[0]

    if paused_candidate is not None:
        return paused_candidate
    return MediaPlaybackState()


def query_mpris_playback(timeout: float = 0.8) -> MediaPlaybackState:
    """Query active MPRIS players safely via `playerctl`. Never raises."""
    if not shutil.which("playerctl"):
        return MediaPlaybackState()
    try:
        proc = subprocess.run(
            [
                "playerctl",
                "-a",
                "metadata",
                "--format",
                "{{status}}\t{{playerName}}\t{{artist}}\t{{title}}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0 and not proc.stdout.strip():
            return MediaPlaybackState()
        return parse_mpris_lines(proc.stdout)
    except Exception:
        return MediaPlaybackState()


def is_game_window(app_id: str, title: str = "") -> bool:
    """Return True if a focused Wayland window clearly represents a running game."""
    aid = (app_id or "").strip().lower()
    if not aid or aid in _CAPRI_OWN_APPS:
        return False
    # Plain Steam desktop library/store in windowed mode is NOT a running game
    if aid == "steam":
        return False
    if aid in _GAME_APP_EXACT:
        return True
    if any(aid.startswith(pref) for pref in _GAME_APP_PREFIXES):
        return True
    if aid.endswith(".exe"):
        return True
    # Proton/Wine windows sometimes report empty/generic class with wine prefix
    if aid.startswith(("wine_", "proton_")):
        return True
    return False


def _is_window_fullscreen(
    focused_window: dict,
    screen_size: Optional[tuple[int, int]] = None,
) -> bool:
    """
    Determine whether a normalized window is fullscreen across both Hyprland and Niri.
    - Hyprland sets `is_fullscreen=True` directly from `fullscreen`/`fullscreenClient`.
    - Niri's `focused-window` JSON does not expose an `is_fullscreen` flag, so we also
      detect fullscreen when `layout.window_size` or `layout.tile_size` covers the full output.
    """
    if bool(focused_window.get("is_fullscreen", False)):
        return True
    if not screen_size:
        return False
    sw, sh = int(screen_size[0]), int(screen_size[1])
    if sw <= 0 or sh <= 0:
        return False
    layout = focused_window.get("layout")
    if not isinstance(layout, dict):
        return False
    for key in ("window_size", "tile_size"):
        dims = layout.get(key)
        if isinstance(dims, (list, tuple)) and len(dims) >= 2:
            try:
                if float(dims[0]) >= sw and float(dims[1]) >= sh:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def evaluate_game_mode(
    focused_window: Optional[dict],
    manual_override: Optional[bool] = None,
    screen_size: Optional[tuple[int, int]] = None,
) -> GameModeState:
    """
    Determine whether Game Mode / Power Saver should be active.
    - `manual_override=True`  -> forced ON
    - `manual_override=False` -> forced OFF
    - `manual_override=None`  -> automatic based on compositor fullscreen or game window
    """
    if manual_override is True:
        return GameModeState(active=True, reason="manual_override")
    if manual_override is False:
        return GameModeState(active=False, reason="manual_off")

    if not focused_window or not isinstance(focused_window, dict):
        return GameModeState(active=False)

    app_id = str(focused_window.get("app_id") or "").strip()
    title = str(focused_window.get("title") or "").strip()
    is_fs = _is_window_fullscreen(focused_window, screen_size=screen_size)

    if app_id.lower() in _CAPRI_OWN_APPS:
        return GameModeState(active=False, app_id=app_id, title=title, is_fullscreen=is_fs)

    game_hit = is_game_window(app_id, title)
    if is_fs:
        return GameModeState(
            active=True,
            reason="fullscreen_game" if game_hit else "fullscreen_app",
            app_id=app_id,
            title=title,
            is_fullscreen=True,
        )
    if game_hit:
        return GameModeState(
            active=True,
            reason="windowed_game",
            app_id=app_id,
            title=title,
            is_fullscreen=False,
        )
    return GameModeState(active=False, app_id=app_id, title=title, is_fullscreen=False)


def detect_game_mode(
    manual_override: Optional[bool] = None,
    backend=None,
) -> GameModeState:
    """Query active compositor (Niri or Hyprland) for focused window and evaluate Game Mode."""
    if manual_override is not None:
        return evaluate_game_mode(None, manual_override=manual_override)
    comp = backend if backend is not None else compositor
    if not comp.is_available:
        return GameModeState(active=False)
    fw = comp.get_focused_window()
    if not fw:
        return GameModeState(active=False)
    screen_size: Optional[tuple[int, int]] = None
    if not fw.get("is_fullscreen") and comp.kind == "niri":
        try:
            screen_size = comp.get_screen_size()
        except Exception:
            screen_size = None
    return evaluate_game_mode(fw, manual_override=None, screen_size=screen_size)


def resolve_avatar_auto_pose(
    *,
    avatar_state: str = "idle",
    media_playing: bool = False,
    game_mode_active: bool = False,
    manual_pose_override: Optional[str] = None,
    idle_seconds: float = 0.0,
    idle_sleep_threshold: float = 180.0,
    auto_poses_enabled: bool = True,
) -> tuple[str, str]:
    """
    Deterministic avatar pose resolver returning `(pose, reason)`.
    Valid poses: 'normal' | 'perch' | 'perch_music' | 'lounge' | 'walk' | 'sleep'.
    """
    st = (avatar_state or "idle").strip().lower()
    if game_mode_active:
        return "sleep", "power_saver"
    if st != "idle":
        return "normal", "active_interaction"
    if manual_pose_override in ("normal", "perch", "perch_music", "lounge", "walk", "sleep"):
        return manual_pose_override, "manual_override"
    if not auto_poses_enabled:
        return "normal", "auto_disabled"
    if media_playing:
        return "perch_music", "mpris_playing"
    if idle_seconds >= max(10.0, float(idle_sleep_threshold)):
        return "sleep", "idle_timeout"
    return "normal", "default"
