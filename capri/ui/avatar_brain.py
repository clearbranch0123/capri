"""
Capri v3.3 Stage 4 — Deterministic Autonomous Desktop Life Brain (pure Python, zero Qt dependency).

Gives Capri calm, believable desktop autonomy while strictly honoring every safety switch:
- Pacing: acts only every ~22–48 seconds after >= 18s of user quiet so she never feels frantic.
- Respects ALL gates unconditionally:
  * `autonomous_enabled=False` -> NONE
  * `game_mode_active=True` -> NONE
  * `reduced_motion=True` -> NONE
  * `pinned=True` (Shift-pin or `/stay`) -> NONE
  * `physics_state != IDLE` -> NONE (never interrupts drag/throw/fall/landing)
  * `avatar_state != "idle"` -> NONE (pauses cleanly during speaking/TTS, listening, thinking)
  * `manual_pose_override is not None` -> NONE (never overrides user's selected pose)
  * `media_playing=True` -> NONE (stays vibing peacefully to music)
- Niri & Hyprland aware:
  * Favors bottom monitor floor strolls (`WALK`) so she never blocks tiled code/text.
  * Occasionally hops onto an available window shelf (`HOP_TO_SHELF`) and perches/lounges,
    or drops back down to the floor (`DROP_TO_FLOOR`).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from ..core.compositor import MonitorWorkArea
from .avatar_physics import PhysicsState
from .surface_map import LandingSurface, SurfaceKind


class BrainAction:
    NONE = "none"
    WALK = "walk"
    HOP_TO_SHELF = "hop_to_shelf"
    DROP_TO_FLOOR = "drop_to_floor"
    LOUNGE = "lounge"
    IDLE_STAND = "idle_stand"


@dataclass(frozen=True)
class BrainDecision:
    action: str = BrainAction.NONE
    target_x: Optional[float] = None
    target_y: Optional[float] = None
    target_surface: Optional[LandingSurface] = None
    pose: Optional[str] = None
    use_teleport: bool = False
    caption: str = ""


class AvatarLifeBrain:
    """Calm, mood-aware autonomous desktop companion planner."""

    def __init__(
        self,
        *,
        autonomous_enabled: bool = True,
        min_interval_s: float = 22.0,
        max_interval_s: float = 46.0,
        interaction_cooldown_s: float = 18.0,
        avatar_w: float = 300.0,
        avatar_h: float = 420.0,
        perch_seat_offset: float = 38.0,
        rng: Optional[random.Random] = None,
    ):
        self.autonomous_enabled: bool = bool(autonomous_enabled)
        self.min_interval_s: float = float(min_interval_s)
        self.max_interval_s: float = float(max_interval_s)
        self.interaction_cooldown_s: float = float(interaction_cooldown_s)
        self.avatar_w: float = float(avatar_w)
        self.avatar_h: float = float(avatar_h)
        self.perch_seat_offset: float = float(perch_seat_offset)
        self._rng: random.Random = rng if rng is not None else random.Random()
        self._next_action_at: float = 0.0

    def set_enabled(self, enabled: bool) -> None:
        self.autonomous_enabled = bool(enabled)

    def reset_schedule(self, now: float) -> None:
        delay = self._rng.uniform(self.min_interval_s, self.max_interval_s)
        self._next_action_at = float(now) + delay

    def tick(
        self,
        *,
        now: float,
        last_interaction_ts: float,
        avatar_state: str,
        physics_state: str,
        x: float,
        y: float,
        current_pose: str,
        active_surface: Optional[LandingSurface],
        surfaces: list[LandingSurface],
        monitor: MonitorWorkArea,
        mood: str = "neutral",
        pinned: bool = False,
        game_mode_active: bool = False,
        reduced_motion: bool = False,
        manual_pose_override: Optional[str] = None,
        media_playing: bool = False,
    ) -> BrainDecision:
        """Evaluate whether Capri should take a calm autonomous desktop action at timestamp `now`."""
        # Hard safety gates — NEVER act when any gate is active
        if (
            not self.autonomous_enabled
            or game_mode_active
            or reduced_motion
            or pinned
            or physics_state != PhysicsState.IDLE
            or avatar_state != "idle"
            or manual_pose_override is not None
            or media_playing
            or current_pose == "sleep"
        ):
            return BrainDecision()

        if (now - last_interaction_ts) < self.interaction_cooldown_s:
            return BrainDecision()

        if self._next_action_at <= 0.0:
            self.reset_schedule(now)
            return BrainDecision()

        if now < self._next_action_at:
            return BrainDecision()

        self.reset_schedule(now)

        min_x = monitor.work_left + 16.0
        max_x = max(min_x, monitor.work_right - self.avatar_w - 16.0)
        floor_y = max(monitor.work_top, monitor.work_bottom - self.avatar_h)

        # Mood influences liveliness vs chilling
        mood_l = (mood or "neutral").strip().lower()
        is_lively = mood_l in ("playful", "warm")
        is_chill = mood_l in ("sarcastic", "annoyed")

        window_shelves = [s for s in surfaces if s.kind != SurfaceKind.FLOOR and s.width >= 200.0]

        # ── Case A: Currently perched on a window shelf ─────────────────────
        if active_surface is not None and active_surface.kind != SurfaceKind.FLOOR:
            roll = self._rng.random()
            # Occasionally hop smoothly down to the floor
            drop_chance = 0.40 if is_lively else 0.28
            if roll < drop_chance:
                tx = max(min_x, min(max_x, x + self._rng.uniform(-160.0, 160.0)))
                return BrainDecision(
                    action=BrainAction.DROP_TO_FLOOR,
                    target_x=tx,
                    target_y=floor_y,
                    pose="normal",
                )
            # Toggle calmly between perched and lounging on the shelf
            if roll < 0.72:
                next_pose = "lounge" if current_pose == "perch" else "perch"
                return BrainDecision(action=BrainAction.LOUNGE, pose=next_pose)
            # Small stroll along a wide shelf
            if active_surface.width >= self.avatar_w + 90.0:
                tx = active_surface.clamp_avatar_x(x + self._rng.uniform(-150.0, 150.0), self.avatar_w)
                if abs(tx - x) >= 48.0:
                    return BrainDecision(
                        action=BrainAction.WALK,
                        target_x=tx,
                        target_y=y,
                        target_surface=active_surface,
                        pose="walk",
                    )
            return BrainDecision()

        # ── Case B: Standing or lounging on the monitor floor ───────────────
        if current_pose in ("lounge", "perch"):
            # Stand back up before strolling
            if self._rng.random() < 0.65:
                return BrainDecision(action=BrainAction.IDLE_STAND, pose="normal")
            return BrainDecision()

        roll = self._rng.random()
        shelf_chance = 0.32 if (window_shelves and is_lively) else (0.22 if window_shelves else 0.0)
        lounge_chance = 0.28 if is_chill else 0.16

        if window_shelves and roll < shelf_chance:
            shelf = self._rng.choice(window_shelves)
            tx = shelf.clamp_avatar_x(
                self._rng.uniform(shelf.x_min, shelf.x_max) - self.avatar_w * 0.5,
                self.avatar_w,
            )
            tx = max(min_x, min(max_x, tx))
            ty = max(monitor.work_top, min(floor_y, shelf.y - self.avatar_h + self.perch_seat_offset))
            return BrainDecision(
                action=BrainAction.HOP_TO_SHELF,
                target_x=tx,
                target_y=ty,
                target_surface=shelf,
                pose="perch",
                use_teleport=True,
            )

        if roll < shelf_chance + lounge_chance:
            return BrainDecision(action=BrainAction.LOUNGE, pose="lounge")

        # Calm horizontal stroll along the bottom floor (140..420 px away from current X)
        span = max_x - min_x
        if span < 90.0:
            return BrainDecision()
        direction = 1.0 if self._rng.random() < 0.5 else -1.0
        dist = self._rng.uniform(140.0, min(420.0, span * 0.65))
        tx = x + direction * dist
        if tx < min_x or tx > max_x:
            tx = x - direction * dist
        tx = max(min_x, min(max_x, tx))
        if abs(tx - x) < 55.0:
            return BrainDecision()

        return BrainDecision(
            action=BrainAction.WALK,
            target_x=tx,
            target_y=floor_y,
            pose="walk",
        )
