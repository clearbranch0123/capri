"""
Capri v3.3 Stage 1 — Deterministic 2D Avatar Physics Engine (pure logical pixels, zero Qt dependency).

Design invariants:
- Operates strictly in compositor logical pixels using `MonitorWorkArea` (supporting DPI scale,
  panel reserved insets, multi-monitor layouts, and negative monitor coordinates).
- High-frequency visual dynamics (pendulum swing, flail/kick oscillation, squash-and-stretch,
  micro landing bounce, wall-splat recoil, impact ripple) run at 60 Hz inside the Qt canvas
  with ZERO compositor IPC calls.
- Compositor floating-window moves are strictly throttled (`<= 20 Hz` and `>= 4px` threshold)
  plus one exact settle move when motion finishes, preventing compositor socket congestion.
- Clean switch semantics:
  * `reduced_motion=True`: disables continuous bounce/pendulum/squash/ripple; snaps cleanly to floor.
  * `Shift` release or `pinned=True` (`/stay`): holds position in air without gravity fall.
  * `game_mode=True`: suspends unsolicited motion cleanly.
  * `ipc_available=False`: disables compositor move requests without throwing or touching other windows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from ..core.compositor import MonitorWorkArea
from .surface_map import LandingSurface, SurfaceKind, find_surface_by_window_id


class PhysicsState:
    IDLE = "idle"
    GRABBED = "grabbed"
    FALLING = "falling"
    WALL_SPLAT = "wall_splat"
    LANDING = "landing"
    PINNED = "pinned"


@dataclass(frozen=True)
class PhysicsConfig:
    gravity: float = 1650.0             # logical px / s^2 (crisp desktop gravity)
    air_drag: float = 0.992             # per-60Hz-frame drag (natural parabolic arc)
    restitution: float = 0.28           # soft single elastic floor bounce
    wall_restitution: float = 0.35      # controlled horizontal wall rebound
    min_bounce_vy: float = 210.0        # below this landing speed -> settle cleanly
    min_throw_speed: float = 180.0      # below this flick speed -> calm vertical drop
    max_throw_speed: float = 1450.0     # natural max fling speed across 1920x1080
    throw_scale: float = 0.85           # weighty, controllable feel (never hyper-sensitive)
    pause_zero_window: float = 0.09     # >= 90ms still before release -> zero velocity
    wall_splat_speed: float = 720.0     # high-speed horizontal impact triggers brief splat
    wall_splat_duration: float = 0.14   # seconds sticking briefly on hard wall hit
    landing_duration: float = 0.22      # seconds for gentle landing settle
    perch_seat_offset: float = 38.0     # legs hang naturally over window bezel when perched
    shelf_snap_margin: float = 24.0     # vertical magnetic capture window when passing a shelf
    ipc_sync_interval: float = 0.05     # <= 20 Hz compositor window moves
    ipc_min_delta_px: float = 4.0       # skip sub-threshold IPC moves
    # Subtle, non-jittery visual spring constants
    pendulum_stiffness: float = 36.0
    pendulum_damping: float = 10.0      # critically damped -> zero shaky oscillation
    max_tilt_deg: float = 8.0           # subtle aerodynamic lean only, never crooked
    squash_stiffness: float = 120.0
    squash_damping: float = 16.0


@dataclass
class PhysicsStepResult:
    state: str
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    # Compositor move request (throttled! None / (0, 0) on most frames)
    ipc_move_dx: int = 0
    ipc_move_dy: int = 0
    ipc_set_exact: Optional[tuple[int, int]] = None
    # One-shot event flags for captions / sound / glitch triggers
    landed: bool = False
    bounced: bool = False
    wall_hit: bool = False
    lost_footing: bool = False
    impact_strength: float = 0.0
    # Active landing surface & pose hint (Stage 2)
    surface_id: str = ""
    surface_kind: str = SurfaceKind.FLOOR
    preferred_pose: str = "normal"      # "normal" | "perch"
    # 60 Hz in-canvas visual transform state
    tilt_deg: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    flail_phase: float = 0.0
    facing: int = 1                     # +1 right, -1 left
    canvas_offset_y: float = 0.0
    impact_ripple: float = 0.0          # 0..1 fading shockwave on landing


class AvatarPhysics2D:
    """Deterministic 2D rigid-body + visual-spring simulation for Capri's desktop avatar."""

    def __init__(
        self,
        *,
        avatar_w: float = 300.0,
        avatar_h: float = 420.0,
        x: float = 1590.0,
        y: float = 615.0,
        config: Optional[PhysicsConfig] = None,
        reduced_motion: bool = False,
        physics_enabled: bool = True,
    ):
        self.cfg = config or PhysicsConfig()
        self.avatar_w = float(avatar_w)
        self.avatar_h = float(avatar_h)

        # Logical top-left coordinates of avatar window
        self.x: float = float(x)
        self.y: float = float(y)
        self.vx: float = 0.0
        self.vy: float = 0.0

        # Last coordinates actually flushed to the Wayland compositor
        self._ipc_x: float = float(x)
        self._ipc_y: float = float(y)
        self._ipc_accum_dt: float = 0.0

        # Mode / switch flags
        self.state: str = PhysicsState.IDLE
        self.pinned: bool = False
        self.reduced_motion: bool = bool(reduced_motion)
        self.physics_enabled: bool = bool(physics_enabled)
        self.game_mode: bool = False
        self.ipc_available: bool = True

        # Visual-only 60 Hz spring parameters (never require IPC!)
        self.facing: int = 1
        self.tilt_deg: float = 0.0
        self.tilt_vel: float = 0.0
        self.flail_phase: float = 0.0
        self.scale_x: float = 1.0
        self.scale_y: float = 1.0
        self.scale_vx: float = 0.0
        self.scale_vy: float = 0.0
        self.canvas_offset_y: float = 0.0
        self.impact_ripple: float = 0.0
        self.wall_splat_side: int = 0  # -1 left wall, +1 right wall, 0 none

        self._state_timer: float = 0.0
        self._bounce_count: int = 0
        self._grab_prev_vx: float = 0.0
        self._drag_clock: float = 0.0
        self._drag_history: list[tuple[float, float, float]] = []
        self._used_external_drag: bool = False
        self.active_surface: Optional[LandingSurface] = None
        self.perched: bool = False

    # ── Switches & Synchronization ──────────────────────────────────────────

    def sync_external_position(self, x: float, y: float) -> None:
        """Align simulation & IPC tracker with an externally reported compositor position."""
        self.x = float(x)
        self.y = float(y)
        self._ipc_x = float(x)
        self._ipc_y = float(y)

    def set_reduced_motion(self, enabled: bool) -> None:
        self.reduced_motion = bool(enabled)
        if self.reduced_motion:
            self._reset_visual_springs()

    def set_physics_enabled(self, enabled: bool) -> None:
        self.physics_enabled = bool(enabled)
        if not self.physics_enabled:
            self.vx = 0.0
            self.vy = 0.0
            self._reset_visual_springs()
            if self.state in (PhysicsState.FALLING, PhysicsState.WALL_SPLAT, PhysicsState.LANDING):
                self.state = PhysicsState.PINNED if self.pinned else PhysicsState.IDLE

    def set_pinned(self, pinned: bool) -> None:
        self.pinned = bool(pinned)
        if self.pinned:
            self.vx = 0.0
            self.vy = 0.0
            self._reset_visual_springs()
            self.state = PhysicsState.PINNED
        elif self.state == PhysicsState.PINNED:
            self.state = PhysicsState.IDLE

    def set_game_mode(self, active: bool) -> None:
        self.game_mode = bool(active)
        if self.game_mode:
            self.vx = 0.0
            self.vy = 0.0
            self._reset_visual_springs()
            if self.state in (PhysicsState.FALLING, PhysicsState.WALL_SPLAT, PhysicsState.LANDING):
                self.state = PhysicsState.IDLE

    def set_ipc_available(self, available: bool) -> None:
        self.ipc_available = bool(available)

    def _reset_visual_springs(self) -> None:
        self.tilt_deg = 0.0
        self.tilt_vel = 0.0
        self.scale_x = 1.0
        self.scale_y = 1.0
        self.scale_vx = 0.0
        self.scale_vy = 0.0
        self.canvas_offset_y = 0.0
        self.impact_ripple = 0.0
        self.wall_splat_side = 0

    # ── Bounds Helpers (Logical Pixels + Negative Monitor Coords) ───────────

    def _clamp_bounds(self, monitor: MonitorWorkArea) -> tuple[float, float, float, float]:
        min_x = monitor.work_left
        max_x = max(min_x, monitor.work_right - self.avatar_w)
        min_y = monitor.work_top
        max_y = max(min_y, monitor.work_bottom - self.avatar_h)
        return min_x, max_x, min_y, max_y

    def floor_y_for(self, monitor: MonitorWorkArea) -> float:
        _, _, _, max_y = self._clamp_bounds(monitor)
        return max_y

    def landing_y_for_surface(self, surface: LandingSurface, monitor: MonitorWorkArea) -> float:
        min_x, max_x, min_y, max_y = self._clamp_bounds(monitor)
        _ = (min_x, max_x)
        if surface.kind == SurfaceKind.FLOOR:
            return max(min_y, min(max_y, surface.y - self.avatar_h))
        return max(min_y, min(max_y, surface.y - self.avatar_h + self.cfg.perch_seat_offset))

    def _find_shelf_below(
        self,
        surfaces: Optional[list[LandingSurface]],
        monitor: MonitorWorkArea,
        *,
        prev_y: float,
        curr_y: float,
        snap_margin: Optional[float] = None,
    ) -> Optional[LandingSurface]:
        if not surfaces:
            return None
        margin = self.cfg.shelf_snap_margin if snap_margin is None else float(snap_margin)
        foot_x = self.x + self.avatar_w * 0.5
        best: Optional[tuple[float, int, LandingSurface]] = None
        for s in surfaces:
            if s.kind == SurfaceKind.FLOOR:
                continue
            if not s.contains_foot_x(foot_x, margin=-28.0):
                continue
            sy = self.landing_y_for_surface(s, monitor)
            if prev_y <= sy + margin and curr_y >= sy - 1.0:
                key = (sy, -s.priority)
                if best is None or key < (best[0], best[1]):
                    best = (sy, -s.priority, s)
        return best[2] if best is not None else None

    def _find_drop_snap_shelf(
        self,
        surfaces: Optional[list[LandingSurface]],
        monitor: MonitorWorkArea,
        *,
        snap_radius: float = 36.0,
    ) -> Optional[LandingSurface]:
        if not surfaces:
            return None
        foot_x = self.x + self.avatar_w * 0.5
        best: Optional[tuple[float, int, LandingSurface]] = None
        for s in surfaces:
            if s.kind == SurfaceKind.FLOOR:
                continue
            if not s.contains_foot_x(foot_x, margin=-28.0):
                continue
            sy = self.landing_y_for_surface(s, monitor)
            dist = abs(self.y - sy)
            if dist <= snap_radius:
                key = (dist, -s.priority)
                if best is None or key < (best[0], best[1]):
                    best = (dist, -s.priority, s)
        return best[2] if best is not None else None

    def sync_surfaces(
        self,
        surfaces: list[LandingSurface],
        monitor: MonitorWorkArea,
    ) -> PhysicsStepResult:
        """
        Keep a perched avatar synchronized with its underlying window:
        - Rides along smoothly if the window moves gently (<= 140px).
        - Drops off (`lost_footing=True`) into `FALLING` if the window closes, shrinks,
          switches workspace, or jumps far away.
        """
        if (
            self.active_surface is None
            or self.active_surface.window_id is None
            or self.state not in (PhysicsState.IDLE, PhysicsState.LANDING)
            or self.pinned
            or self.game_mode
            or not self.physics_enabled
        ):
            return self._make_result()

        updated = find_surface_by_window_id(surfaces, self.active_surface.window_id)
        min_x, max_x, min_y, max_y = self._clamp_bounds(monitor)
        if updated is not None and updated.width >= 180.0:
            old_y = self.landing_y_for_surface(self.active_surface, monitor)
            new_y = self.landing_y_for_surface(updated, monitor)
            old_cx = (self.active_surface.x_min + self.active_surface.x_max) * 0.5
            new_cx = (updated.x_min + updated.x_max) * 0.5
            shift = math.hypot(new_cx - old_cx, new_y - old_y)
            if shift <= 140.0:
                if shift >= 1.0:
                    nx = updated.clamp_avatar_x(self.x + (new_cx - old_cx), self.avatar_w)
                    self.x = max(min_x, min(max_x, nx))
                    self.y = max(min_y, min(max_y, new_y))
                    self.active_surface = updated
                    return self._make_result(ipc_set_exact=self._flush_exact_ipc())
                self.active_surface = updated
                return self._make_result()

        # Underlying window disappeared or jumped away -> lose footing!
        self.active_surface = None
        self.perched = False
        if self.reduced_motion:
            self.vx = 0.0
            self.vy = 0.0
            self.y = max_y
            self.state = PhysicsState.IDLE
            self._reset_visual_springs()
            return self._make_result(
                lost_footing=True,
                landed=True,
                ipc_set_exact=self._flush_exact_ipc(),
            )
        self.vx = 0.0
        self.vy = -85.0  # tiny startled hop off the vanished ledge
        self._bounce_count = 0
        self.state = PhysicsState.FALLING
        return self._make_result(lost_footing=True)

    # ── Grab / Drag / Release Lifecycle ─────────────────────────────────────

    def begin_grab(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        if x is not None and y is not None:
            self.sync_external_position(x, y)
        self.state = PhysicsState.GRABBED
        self.pinned = False
        self.vx = 0.0
        self.vy = 0.0
        self._grab_prev_vx = 0.0
        self._bounce_count = 0
        self._ipc_accum_dt = 0.0
        self.wall_splat_side = 0
        self.canvas_offset_y = 0.0
        self.tilt_deg = 0.0
        self.tilt_vel = 0.0
        self.scale_x = 1.0
        self.scale_y = 1.0
        self._drag_clock = float(timestamp) if timestamp is not None else 0.0
        self._drag_history = [(self._drag_clock, self.x, self.y)]
        self._used_external_drag = False
        self.active_surface = None
        self.perched = False

    def _compute_history_velocity(self, now: Optional[float] = None) -> tuple[float, float]:
        """
        Compute clean ballistic throw velocity from recent real drag samples.
        - If the user paused for >= `pause_zero_window` (90ms) before release -> (0.0, 0.0).
        - If speed < `min_throw_speed` (180 px/s) -> (0.0, 0.0) so gentle placement drops straight down.
        - Otherwise returns weighted velocity scaled by `throw_scale` for natural desktop weight.
        """
        if len(self._drag_history) < 2:
            return 0.0, 0.0
        ref_t = self._drag_clock if now is None else float(now)

        # Find the most recent sample where the pointer actually moved
        last_move_idx = -1
        for i in range(len(self._drag_history) - 1, 0, -1):
            _, x1, y1 = self._drag_history[i]
            _, x0, y0 = self._drag_history[i - 1]
            if math.hypot(x1 - x0, y1 - y0) >= 0.5:
                last_move_idx = i
                break

        if last_move_idx <= 0:
            return 0.0, 0.0

        t1, x1, y1 = self._drag_history[last_move_idx]
        # User stopped their hand before releasing -> guaranteed vertical drop!
        if (ref_t - t1) >= self.cfg.pause_zero_window:
            return 0.0, 0.0

        # Use recent ~80-130ms window strictly BEFORE last_move_idx (ignores earlier opposite movement!)
        start_idx = max(0, last_move_idx - 1)
        for j in range(last_move_idx - 1, -1, -1):
            tj, _, _ = self._drag_history[j]
            start_idx = j
            if (t1 - tj) >= 0.09:
                break

        t0, x0, y0 = self._drag_history[start_idx]
        if math.hypot(x1 - x0, y1 - y0) < 4.0:
            return 0.0, 0.0
        span = max(0.035, t1 - t0)
        vx = ((x1 - x0) / span) * self.cfg.throw_scale
        vy = ((y1 - y0) / span) * self.cfg.throw_scale
        speed = math.hypot(vx, vy)
        if speed < self.cfg.min_throw_speed:
            return 0.0, 0.0
        return vx, vy

    def observe_external_drag(
        self,
        new_x: float,
        new_y: float,
        dt: float,
        timestamp: Optional[float] = None,
    ) -> PhysicsStepResult:
        """
        Record a committed drag position `(new_x, new_y)` at `timestamp` into the rolling
        velocity history without triggering extra IPC writes.
        Coalesces sub-8ms samples cleanly so 1000 Hz gaming mice track velocity accurately.
        """
        step_dt = max(1e-4, min(float(dt), 0.15))
        if self.state != PhysicsState.GRABBED:
            self.begin_grab(new_x, new_y, timestamp=timestamp)
            return self._make_result()

        self._used_external_drag = True
        if timestamp is not None:
            self._drag_clock = max(self._drag_clock, float(timestamp))
        else:
            self._drag_clock += step_dt

        self.x = float(new_x)
        self.y = float(new_y)
        self._ipc_x = self.x
        self._ipc_y = self.y

        if (
            len(self._drag_history) >= 2
            and (self._drag_clock - self._drag_history[-2][0]) < 0.008
        ):
            # Coalesce ultra-high-frequency (e.g. 1000 Hz) motion into ~125 Hz buckets
            self._drag_history[-1] = (self._drag_clock, self.x, self.y)
        else:
            self._drag_history.append((self._drag_clock, self.x, self.y))

        cutoff = self._drag_clock - 0.20
        while len(self._drag_history) > 2 and self._drag_history[0][0] < cutoff:
            self._drag_history.pop(0)

        self.vx, self.vy = self._compute_history_velocity(self._drag_clock)
        if abs(self.vx) > 25.0:
            self.facing = 1 if self.vx > 0 else -1

        # Keep visual sprite clean during drag (dance.png handles the dangling motion!)
        self.tilt_deg = 0.0
        self.tilt_vel = 0.0
        self.scale_x = 1.0
        self.scale_y = 1.0
        if not self.reduced_motion:
            self.flail_phase = (self.flail_phase + step_dt * 12.0) % math.tau
        return self._make_result()

    def update_grab(
        self,
        dx: float,
        dy: float,
        dt: float,
        monitor: Optional[MonitorWorkArea] = None,
    ) -> PhysicsStepResult:
        """Apply pointer drag delta (logical px), compute smooth lean & throttled IPC delta."""
        step_dt = max(1e-3, min(float(dt), 0.1))
        if self.state != PhysicsState.GRABBED:
            self.begin_grab()

        self.x += float(dx)
        self.y += float(dy)
        if monitor is not None:
            min_x, max_x, min_y, max_y = self._clamp_bounds(monitor)
            self.x = max(min_x, min(max_x, self.x))
            self.y = max(min_y, min(max_y, self.y))

        inst_vx = float(dx) / step_dt
        inst_vy = float(dy) / step_dt
        alpha = 0.42
        self.vx = (1.0 - alpha) * self.vx + alpha * inst_vx
        self.vy = (1.0 - alpha) * self.vy + alpha * inst_vy
        if abs(self.vx) > 25.0:
            self.facing = 1 if self.vx > 0 else -1

        if not self.reduced_motion:
            # Gentle critically-damped lean (never shaky!)
            target_tilt = max(
                -self.cfg.max_tilt_deg,
                min(self.cfg.max_tilt_deg, -self.vx * 0.008),
            )
            self._step_pendulum(step_dt, target_tilt)
            self.flail_phase = (self.flail_phase + step_dt * 12.0) % math.tau
            self._step_squash(step_dt, target_sx=1.0, target_sy=1.0)
        else:
            self._reset_visual_springs()

        ipc_dx, ipc_dy = self._consume_throttled_ipc_delta(step_dt)
        return self._make_result(ipc_move_dx=ipc_dx, ipc_move_dy=ipc_dy)

    def end_grab(
        self,
        *,
        shift_held: bool = False,
        monitor: MonitorWorkArea,
        surfaces: Optional[list[LandingSurface]] = None,
    ) -> PhysicsStepResult:
        """
        Release Capri from cursor grip:
        - `shift_held` or `physics_enabled=False` -> pin cleanly at current coordinates.
        - `reduced_motion=True` -> snap cleanly to nearest shelf below or floor with zero bounce.
        - Dropped directly over a window shelf -> snap magnetically onto that ledge (`perch`).
        - Otherwise -> launch into `FALLING` with clamped throw momentum `(vx, vy)`.
        """
        min_x, max_x, min_y, max_y = self._clamp_bounds(monitor)
        self.x = max(min_x, min(max_x, self.x))
        self.y = max(min_y, min(max_y, self.y))

        if shift_held or not self.physics_enabled:
            self.pinned = bool(shift_held)
            self.vx = 0.0
            self.vy = 0.0
            self.active_surface = None
            self.perched = False
            self.state = PhysicsState.PINNED if self.pinned else PhysicsState.IDLE
            self._reset_visual_springs()
            return self._make_result(ipc_set_exact=self._flush_exact_ipc())

        if self.reduced_motion:
            self.pinned = False
            self.vx = 0.0
            self.vy = 0.0
            shelf = self._find_shelf_below(surfaces, monitor, prev_y=self.y, curr_y=max_y, snap_margin=36.0)
            if shelf is not None:
                self.y = self.landing_y_for_surface(shelf, monitor)
                self.x = max(min_x, min(max_x, shelf.clamp_avatar_x(self.x, self.avatar_w)))
                self.active_surface = shelf
                self.perched = (shelf.preferred_pose == "perch")
            else:
                self.y = max_y
                self.active_surface = None
                self.perched = False
            self.state = PhysicsState.IDLE
            self._reset_visual_springs()
            return self._make_result(landed=True, ipc_set_exact=self._flush_exact_ipc())

        # Clamp throw velocity vector
        speed = math.hypot(self.vx, self.vy)
        if speed > self.cfg.max_throw_speed and speed > 0.0:
            scale = self.cfg.max_throw_speed / speed
            self.vx *= scale
            self.vy *= scale

        self.pinned = False
        self._bounce_count = 0
        self._ipc_accum_dt = 0.0

        # Direct gentle drop right onto a window shelf -> magnetic perch snap
        if self.vy >= -120.0 and abs(self.vx) < 260.0:
            snap_shelf = self._find_drop_snap_shelf(surfaces, monitor)
            if snap_shelf is not None:
                self.y = self.landing_y_for_surface(snap_shelf, monitor)
                self.x = max(min_x, min(max_x, snap_shelf.clamp_avatar_x(self.x, self.avatar_w)))
                self.vx = 0.0
                self.vy = 0.0
                self.active_surface = snap_shelf
                self.perched = (snap_shelf.preferred_pose == "perch")
                self.state = PhysicsState.LANDING
                self._state_timer = self.cfg.landing_duration
                self.scale_x = 1.07
                self.scale_y = 0.93
                self.impact_ripple = 0.45
                return self._make_result(landed=True, impact_strength=0.35, ipc_set_exact=self._flush_exact_ipc())

        # Already resting on floor with no upward toss?
        if self.y >= max_y - 1.0 and self.vy >= 0.0:
            self.y = max_y
            self.vx = 0.0
            self.vy = 0.0
            self.active_surface = None
            self.perched = False
            self.state = PhysicsState.LANDING
            self._state_timer = self.cfg.landing_duration
            self.scale_x = 1.08
            self.scale_y = 0.92
            self.impact_ripple = 0.5
            return self._make_result(landed=True, impact_strength=0.45, ipc_set_exact=self._flush_exact_ipc())

        self.active_surface = None
        self.perched = False
        self.state = PhysicsState.FALLING
        return self._make_result()

    # ── Fixed-Timestep 60 Hz Simulation Tick ────────────────────────────────

    def step(
        self,
        dt: float,
        monitor: MonitorWorkArea,
        surfaces: Optional[list[LandingSurface]] = None,
    ) -> PhysicsStepResult:
        """Advance physics & visual springs by `dt` seconds."""
        step_dt = max(1e-4, min(float(dt), 0.05))
        min_x, max_x, min_y, max_y = self._clamp_bounds(monitor)

        # Fade landing ripple smoothly
        if self.impact_ripple > 0.0:
            self.impact_ripple = max(0.0, self.impact_ripple - step_dt * 3.2)

        if self.reduced_motion:
            self._reset_visual_springs()
            if self.state in (PhysicsState.FALLING, PhysicsState.WALL_SPLAT, PhysicsState.LANDING):
                self.vx = 0.0
                self.vy = 0.0
                self.y = max_y
                self.x = max(min_x, min(max_x, self.x))
                self.state = PhysicsState.IDLE
                return self._make_result(landed=True, ipc_set_exact=self._flush_exact_ipc())
            return self._make_result()

        if self.state == PhysicsState.GRABBED:
            # Keep sprite calm & upright while dance.png plays continuously during hold.
            # Never decay throw velocity here — rolling drag history handles pause vs flick cleanly!
            self._step_pendulum(step_dt, 0.0)
            self.flail_phase = (self.flail_phase + step_dt * 12.0) % math.tau
            self._step_squash(step_dt, target_sx=1.0, target_sy=1.0)
            return self._make_result()

        if self.state in (PhysicsState.IDLE, PhysicsState.PINNED):
            self._step_pendulum(step_dt, 0.0)
            self._step_squash(step_dt, target_sx=1.0, target_sy=1.0)
            self.canvas_offset_y *= 0.82
            if abs(self.canvas_offset_y) < 0.2:
                self.canvas_offset_y = 0.0
            return self._make_result()

        if self.state == PhysicsState.WALL_SPLAT:
            self._state_timer -= step_dt
            self._step_squash(step_dt, target_sx=0.84, target_sy=1.14)
            self._step_pendulum(step_dt, 0.0)
            if self._state_timer <= 0.0:
                rebound_dir = 1.0 if self.wall_splat_side < 0 else -1.0
                self.vx = rebound_dir * 220.0
                self.vy = max(self.vy, 40.0)
                self.wall_splat_side = 0
                self.state = PhysicsState.FALLING
            return self._make_result()

        if self.state == PhysicsState.LANDING:
            self._state_timer -= step_dt
            self._step_pendulum(step_dt, 0.0)
            self._step_squash(step_dt, target_sx=1.0, target_sy=1.0)
            # Soft damped micro-hop strictly inside canvas (zero IPC cost!)
            t_norm = max(0.0, self._state_timer / max(1e-3, self.cfg.landing_duration))
            self.canvas_offset_y = -math.sin(t_norm * math.pi) * 6.0 * t_norm
            if self._state_timer <= 0.0 and abs(self.scale_x - 1.0) < 0.02 and abs(self.scale_y - 1.0) < 0.02:
                self.scale_x = 1.0
                self.scale_y = 1.0
                self.tilt_deg = 0.0
                self.canvas_offset_y = 0.0
                self.state = PhysicsState.IDLE
            return self._make_result()

        # ── PhysicsState.FALLING ────────────────────────────────────────────
        if self.game_mode or not self.physics_enabled:
            self.vx = 0.0
            self.vy = 0.0
            self.state = PhysicsState.IDLE
            return self._make_result()

        self.vy += self.cfg.gravity * step_dt
        drag_factor = self.cfg.air_drag ** (step_dt * 60.0)
        self.vx *= drag_factor

        prev_y = self.y
        self.x += self.vx * step_dt
        self.y += self.vy * step_dt

        if abs(self.vx) > 20.0:
            self.facing = 1 if self.vx > 0 else -1

        # Subtle lean into horizontal flight arc (capped at max_tilt_deg, zero oscillation)
        flight_tilt = max(-self.cfg.max_tilt_deg, min(self.cfg.max_tilt_deg, self.vx * 0.008))
        self._step_pendulum(step_dt, flight_tilt)
        # Very subtle vertical stretch on fast drop
        fall_stretch = min(0.05, abs(self.vy) / 9000.0)
        self._step_squash(step_dt, target_sx=1.0 - fall_stretch * 0.5, target_sy=1.0 + fall_stretch)

        wall_hit = False
        bounced = False
        landed = False
        impact_strength = 0.0

        # Ceiling clamp (e.g. tossed straight up under top bar)
        if self.y < min_y:
            self.y = min_y
            if self.vy < 0.0:
                self.vy = -self.vy * 0.25

        # Left / right monitor wall collision
        if self.x <= min_x:
            self.x = min_x
            impact_vx = abs(self.vx)
            if impact_vx >= self.cfg.wall_splat_speed:
                wall_hit = True
                impact_strength = min(1.0, impact_vx / self.cfg.max_throw_speed)
                self.vx = 0.0
                self.vy *= 0.25
                self.wall_splat_side = -1
                self.scale_x = 0.72
                self.scale_y = 1.24
                self.state = PhysicsState.WALL_SPLAT
                self._state_timer = self.cfg.wall_splat_duration
                return self._make_result(
                    wall_hit=True,
                    impact_strength=impact_strength,
                    ipc_set_exact=self._flush_exact_ipc(),
                )
            elif self.vx < 0.0:
                self.vx = -self.vx * self.cfg.wall_restitution
                wall_hit = impact_vx > 120.0
        elif self.x >= max_x:
            self.x = max_x
            impact_vx = abs(self.vx)
            if impact_vx >= self.cfg.wall_splat_speed:
                wall_hit = True
                impact_strength = min(1.0, impact_vx / self.cfg.max_throw_speed)
                self.vx = 0.0
                self.vy *= 0.25
                self.wall_splat_side = 1
                self.scale_x = 0.72
                self.scale_y = 1.24
                self.state = PhysicsState.WALL_SPLAT
                self._state_timer = self.cfg.wall_splat_duration
                return self._make_result(
                    wall_hit=True,
                    impact_strength=impact_strength,
                    ipc_set_exact=self._flush_exact_ipc(),
                )
            elif self.vx > 0.0:
                self.vx = -self.vx * self.cfg.wall_restitution
                wall_hit = impact_vx > 120.0

        # Window shelf collision while descending (Stage 2 magnetic landing & perch!)
        if self.vy >= 0.0 and surfaces:
            shelf = self._find_shelf_below(surfaces, monitor, prev_y=prev_y, curr_y=self.y)
            if shelf is not None:
                impact_vy = abs(self.vy)
                impact_strength = min(1.0, impact_vy / 1100.0)
                self.y = self.landing_y_for_surface(shelf, monitor)
                self.x = max(min_x, min(max_x, shelf.clamp_avatar_x(self.x, self.avatar_w)))
                self.vx = 0.0
                self.vy = 0.0
                self.active_surface = shelf
                self.perched = (shelf.preferred_pose == "perch")
                self.state = PhysicsState.LANDING
                self._state_timer = self.cfg.landing_duration
                self.scale_x = 1.0 + 0.08 * max(0.35, impact_strength)
                self.scale_y = 1.0 - 0.07 * max(0.35, impact_strength)
                self.impact_ripple = max(self.impact_ripple, min(1.0, 0.35 + impact_strength * 0.45))
                return self._make_result(
                    landed=True,
                    wall_hit=wall_hit,
                    impact_strength=impact_strength,
                    ipc_set_exact=self._flush_exact_ipc(),
                )

        # Floor collision (1 soft elastic hop, then gentle squash-and-stretch settle to IDLE)
        if self.y >= max_y:
            self.y = max_y
            impact_vy = abs(self.vy)
            impact_strength = min(1.0, impact_vy / 1100.0)
            if impact_vy >= self.cfg.min_bounce_vy and self._bounce_count < 1:
                self._bounce_count += 1
                self.vy = -impact_vy * self.cfg.restitution
                self.vx *= 0.65
                self.scale_x = 1.0 + 0.10 * impact_strength
                self.scale_y = 1.0 - 0.09 * impact_strength
                self.impact_ripple = max(self.impact_ripple, min(1.0, 0.35 + impact_strength * 0.5))
                bounced = True
            else:
                self.vx = 0.0
                self.vy = 0.0
                self.active_surface = None
                self.perched = False
                self.state = PhysicsState.LANDING
                self._state_timer = self.cfg.landing_duration
                self.scale_x = 1.0 + 0.08 * max(0.35, impact_strength)
                self.scale_y = 1.0 - 0.07 * max(0.35, impact_strength)
                self.impact_ripple = max(self.impact_ripple, min(1.0, 0.4 + impact_strength * 0.45))
                landed = True
                return self._make_result(
                    landed=True,
                    wall_hit=wall_hit,
                    impact_strength=impact_strength,
                    ipc_set_exact=self._flush_exact_ipc(),
                )

        ipc_dx, ipc_dy = self._consume_throttled_ipc_delta(step_dt)
        return self._make_result(
            landed=landed,
            bounced=bounced,
            wall_hit=wall_hit,
            impact_strength=impact_strength,
            ipc_move_dx=ipc_dx,
            ipc_move_dy=ipc_dy,
        )

    # ── Internal Visual Springs & IPC Throttling ────────────────────────────

    def _step_pendulum(self, dt: float, target_deg: float) -> None:
        acc = (target_deg - self.tilt_deg) * self.cfg.pendulum_stiffness - self.tilt_vel * self.cfg.pendulum_damping
        self.tilt_vel += acc * dt
        self.tilt_deg += self.tilt_vel * dt
        self.tilt_deg = max(-self.cfg.max_tilt_deg, min(self.cfg.max_tilt_deg, self.tilt_deg))

    def _step_squash(self, dt: float, *, target_sx: float, target_sy: float) -> None:
        ax = (target_sx - self.scale_x) * self.cfg.squash_stiffness - self.scale_vx * self.cfg.squash_damping
        ay = (target_sy - self.scale_y) * self.cfg.squash_stiffness - self.scale_vy * self.cfg.squash_damping
        self.scale_vx += ax * dt
        self.scale_vy += ay * dt
        self.scale_x = max(0.65, min(1.35, self.scale_x + self.scale_vx * dt))
        self.scale_y = max(0.65, min(1.35, self.scale_y + self.scale_vy * dt))

    def _consume_throttled_ipc_delta(self, dt: float) -> tuple[int, int]:
        if not self.ipc_available:
            return 0, 0
        self._ipc_accum_dt += dt
        if self._ipc_accum_dt < self.cfg.ipc_sync_interval:
            return 0, 0
        raw_dx = self.x - self._ipc_x
        raw_dy = self.y - self._ipc_y
        if math.hypot(raw_dx, raw_dy) < self.cfg.ipc_min_delta_px:
            return 0, 0
        idx = int(round(raw_dx))
        idy = int(round(raw_dy))
        if idx == 0 and idy == 0:
            return 0, 0
        self._ipc_accum_dt = 0.0
        self._ipc_x += idx
        self._ipc_y += idy
        return idx, idy

    def _flush_exact_ipc(self) -> Optional[tuple[int, int]]:
        if not self.ipc_available:
            return None
        tx = int(round(self.x))
        ty = int(round(self.y))
        self._ipc_accum_dt = 0.0
        self._ipc_x = float(tx)
        self._ipc_y = float(ty)
        return tx, ty

    def _make_result(
        self,
        *,
        ipc_move_dx: int = 0,
        ipc_move_dy: int = 0,
        ipc_set_exact: Optional[tuple[int, int]] = None,
        landed: bool = False,
        bounced: bool = False,
        wall_hit: bool = False,
        lost_footing: bool = False,
        impact_strength: float = 0.0,
    ) -> PhysicsStepResult:
        surf = self.active_surface
        return PhysicsStepResult(
            state=self.state,
            x=self.x,
            y=self.y,
            vx=self.vx,
            vy=self.vy,
            ipc_move_dx=ipc_move_dx,
            ipc_move_dy=ipc_move_dy,
            ipc_set_exact=ipc_set_exact,
            landed=landed,
            bounced=bounced,
            wall_hit=wall_hit,
            lost_footing=lost_footing,
            impact_strength=impact_strength,
            surface_id=surf.surface_id if surf is not None else "",
            surface_kind=surf.kind if surf is not None else SurfaceKind.FLOOR,
            preferred_pose=surf.preferred_pose if surf is not None else "normal",
            tilt_deg=self.tilt_deg,
            scale_x=self.scale_x,
            scale_y=self.scale_y,
            flail_phase=self.flail_phase,
            facing=self.facing,
            canvas_offset_y=self.canvas_offset_y,
            impact_ripple=self.impact_ripple,
        )
