//! Deterministic 2D desktop avatar physics engine (`AvatarPhysics2D`).
//!
//! Mirrors `capri/ui/avatar_physics.py` constants, rolling drag velocity history,
//! ballistic throws, wall-splat recoil, soft single floor bounce, `Shift` mid-air pin,
//! and optional Niri/Hyprland window-shelf magnetic perching.

use std::collections::VecDeque;
use std::f64::consts::{PI, TAU};
use std::process::Command;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PhysicsState {
    Idle,
    Grabbed,
    Falling,
    WallSplat,
    Landing,
    Pinned,
}

#[derive(Debug, Clone, Copy)]
pub struct MonitorWorkArea {
    pub left: f64,
    pub top: f64,
    pub width: f64,
    pub height: f64,
}

impl MonitorWorkArea {
    pub fn from_size(width: u32, height: u32) -> Self {
        Self {
            left: 0.0,
            top: 0.0,
            width: width.max(300) as f64,
            height: height.max(420) as f64,
        }
    }

    pub fn right(&self) -> f64 {
        self.left + self.width
    }

    pub fn bottom(&self) -> f64 {
        self.top + self.height
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SurfaceKind {
    Floor,
    WindowTop,
}

#[derive(Debug, Clone)]
pub struct LandingSurface {
    pub kind: SurfaceKind,
    pub x_min: f64,
    pub x_max: f64,
    pub y: f64,
    pub priority: i32,
}

impl LandingSurface {
    pub fn contains_foot_x(&self, foot_x: f64, margin: f64) -> bool {
        foot_x >= (self.x_min - margin) && foot_x <= (self.x_max + margin)
    }

    pub fn clamp_avatar_x(&self, x: f64, avatar_w: f64) -> f64 {
        let lo = self.x_min - avatar_w * 0.25;
        let hi = self.x_max - avatar_w * 0.75;
        if lo <= hi {
            x.clamp(lo, hi)
        } else {
            ((self.x_min + self.x_max) * 0.5) - avatar_w * 0.5
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct PhysicsConfig {
    pub gravity: f64,
    pub air_drag: f64,
    pub restitution: f64,
    pub wall_restitution: f64,
    pub min_bounce_vy: f64,
    pub min_throw_speed: f64,
    pub max_throw_speed: f64,
    pub throw_scale: f64,
    pub pause_zero_window: f64,
    pub wall_splat_speed: f64,
    pub wall_splat_duration: f64,
    pub landing_duration: f64,
    pub perch_seat_offset: f64,
    pub shelf_snap_margin: f64,
    pub pendulum_stiffness: f64,
    pub pendulum_damping: f64,
    pub max_tilt_deg: f64,
    pub squash_stiffness: f64,
    pub squash_damping: f64,
}

impl Default for PhysicsConfig {
    fn default() -> Self {
        Self {
            gravity: 1650.0,
            air_drag: 0.992,
            restitution: 0.28,
            wall_restitution: 0.35,
            min_bounce_vy: 210.0,
            min_throw_speed: 180.0,
            max_throw_speed: 1450.0,
            throw_scale: 0.85,
            pause_zero_window: 0.09,
            wall_splat_speed: 720.0,
            wall_splat_duration: 0.14,
            landing_duration: 0.22,
            perch_seat_offset: 38.0,
            shelf_snap_margin: 24.0,
            pendulum_stiffness: 36.0,
            pendulum_damping: 10.0,
            max_tilt_deg: 8.0,
            squash_stiffness: 120.0,
            squash_damping: 16.0,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct PhysicsStepEvents {
    pub landed: bool,
    pub wall_hit: bool,
    pub perched: bool,
    pub crossed_monitor_side: i8,
}

pub struct AvatarPhysics2D {
    pub cfg: PhysicsConfig,
    pub avatar_w: f64,
    pub avatar_h: f64,
    pub x: f64,
    pub y: f64,
    pub vx: f64,
    pub vy: f64,
    pub state: PhysicsState,
    pub pinned: bool,
    pub reduced_motion: bool,
    pub physics_enabled: bool,
    pub game_mode: bool,

    pub facing: i8,
    pub tilt_deg: f64,
    tilt_vel: f64,
    pub flail_phase: f64,
    pub scale_x: f64,
    pub scale_y: f64,
    scale_vx: f64,
    scale_vy: f64,
    pub canvas_offset_y: f64,
    pub impact_ripple: f64,
    wall_splat_side: i8,

    state_timer: f64,
    bounce_count: u8,
    drag_clock: f64,
    drag_history: VecDeque<(f64, f64, f64)>,
    pub active_surface: Option<LandingSurface>,
    pub perched: bool,
}

impl AvatarPhysics2D {
    pub fn new(avatar_w: f64, avatar_h: f64, x: f64, y: f64) -> Self {
        Self {
            cfg: PhysicsConfig::default(),
            avatar_w,
            avatar_h,
            x,
            y,
            vx: 0.0,
            vy: 0.0,
            state: PhysicsState::Idle,
            pinned: false,
            reduced_motion: false,
            physics_enabled: true,
            game_mode: false,
            facing: 1,
            tilt_deg: 0.0,
            tilt_vel: 0.0,
            flail_phase: 0.0,
            scale_x: 1.0,
            scale_y: 1.0,
            scale_vx: 0.0,
            scale_vy: 0.0,
            canvas_offset_y: 0.0,
            impact_ripple: 0.0,
            wall_splat_side: 0,
            state_timer: 0.0,
            bounce_count: 0,
            drag_clock: 0.0,
            drag_history: VecDeque::with_capacity(32),
            active_surface: None,
            perched: false,
        }
    }

    pub fn is_animating(&self) -> bool {
        matches!(
            self.state,
            PhysicsState::Grabbed
                | PhysicsState::Falling
                | PhysicsState::WallSplat
                | PhysicsState::Landing
        ) || self.impact_ripple > 0.01
            || self.canvas_offset_y.abs() > 0.2
            || self.tilt_deg.abs() > 0.08
            || (self.scale_x - 1.0).abs() > 0.004
            || (self.scale_y - 1.0).abs() > 0.004
    }

    pub fn clamp_bounds(&self, monitor: MonitorWorkArea) -> (f64, f64, f64, f64) {
        let min_x = monitor.left;
        let max_x = (monitor.right() - self.avatar_w).max(min_x);
        let min_y = monitor.top;
        let max_y = (monitor.bottom() - self.avatar_h).max(min_y);
        (min_x, max_x, min_y, max_y)
    }

    pub fn clamp_to_monitor(&mut self, monitor: MonitorWorkArea) {
        let (min_x, max_x, min_y, max_y) = self.clamp_bounds(monitor);
        self.x = self.x.clamp(min_x, max_x);
        self.y = self.y.clamp(min_y, max_y);
    }

    pub fn set_reduced_motion(&mut self, enabled: bool) {
        self.reduced_motion = enabled;
        if enabled {
            self.reset_visual_springs();
        }
    }

    pub fn set_physics_enabled(&mut self, enabled: bool) {
        self.physics_enabled = enabled;
        if !enabled {
            self.vx = 0.0;
            self.vy = 0.0;
            self.reset_visual_springs();
            if matches!(
                self.state,
                PhysicsState::Falling | PhysicsState::WallSplat | PhysicsState::Landing
            ) {
                self.state = if self.pinned {
                    PhysicsState::Pinned
                } else {
                    PhysicsState::Idle
                };
            }
        }
    }

    pub fn set_pinned(&mut self, pinned: bool) {
        self.pinned = pinned;
        if pinned {
            self.vx = 0.0;
            self.vy = 0.0;
            self.reset_visual_springs();
            self.state = PhysicsState::Pinned;
        } else if self.state == PhysicsState::Pinned {
            self.state = PhysicsState::Idle;
        }
    }

    pub fn set_game_mode(&mut self, active: bool) {
        self.game_mode = active;
        if active {
            self.vx = 0.0;
            self.vy = 0.0;
            self.reset_visual_springs();
            if matches!(
                self.state,
                PhysicsState::Falling | PhysicsState::WallSplat | PhysicsState::Landing
            ) {
                self.state = PhysicsState::Idle;
            }
        }
    }

    pub fn reset_visual_springs(&mut self) {
        self.tilt_deg = 0.0;
        self.tilt_vel = 0.0;
        self.scale_x = 1.0;
        self.scale_y = 1.0;
        self.scale_vx = 0.0;
        self.scale_vy = 0.0;
        self.canvas_offset_y = 0.0;
        self.impact_ripple = 0.0;
        self.wall_splat_side = 0;
    }

    fn landing_y_for_surface(&self, surface: &LandingSurface, monitor: MonitorWorkArea) -> f64 {
        let (_, _, min_y, max_y) = self.clamp_bounds(monitor);
        if surface.kind == SurfaceKind::Floor {
            (surface.y - self.avatar_h).clamp(min_y, max_y)
        } else {
            (surface.y - self.avatar_h + self.cfg.perch_seat_offset).clamp(min_y, max_y)
        }
    }

    fn find_shelf_below<'a>(
        &self,
        surfaces: &'a [LandingSurface],
        monitor: MonitorWorkArea,
        prev_y: f64,
        curr_y: f64,
        snap_margin: f64,
    ) -> Option<&'a LandingSurface> {
        let foot_x = self.x + self.avatar_w * 0.5;
        let mut best: Option<(f64, i32, &'a LandingSurface)> = None;
        for s in surfaces {
            if s.kind == SurfaceKind::Floor || !s.contains_foot_x(foot_x, -28.0) {
                continue;
            }
            let sy = self.landing_y_for_surface(s, monitor);
            if prev_y <= sy + snap_margin && curr_y >= sy - 1.0 {
                let replace = match best {
                    None => true,
                    Some((by, bp, _)) => (sy, -s.priority) < (by, bp),
                };
                if replace {
                    best = Some((sy, -s.priority, s));
                }
            }
        }
        best.map(|(_, _, s)| s)
    }

    fn find_drop_snap_shelf<'a>(
        &self,
        surfaces: &'a [LandingSurface],
        monitor: MonitorWorkArea,
        snap_radius: f64,
    ) -> Option<&'a LandingSurface> {
        let foot_x = self.x + self.avatar_w * 0.5;
        let mut best: Option<(f64, i32, &'a LandingSurface)> = None;
        for s in surfaces {
            if s.kind == SurfaceKind::Floor || !s.contains_foot_x(foot_x, -28.0) {
                continue;
            }
            let sy = self.landing_y_for_surface(s, monitor);
            let dist = (self.y - sy).abs();
            if dist <= snap_radius {
                let replace = match best {
                    None => true,
                    Some((bd, bp, _)) => (dist, -s.priority) < (bd, bp),
                };
                if replace {
                    best = Some((dist, -s.priority, s));
                }
            }
        }
        best.map(|(_, _, s)| s)
    }

    pub fn begin_grab(&mut self, timestamp: f64) {
        self.state = PhysicsState::Grabbed;
        self.pinned = false;
        self.vx = 0.0;
        self.vy = 0.0;
        self.bounce_count = 0;
        self.wall_splat_side = 0;
        self.canvas_offset_y = 0.0;
        self.tilt_deg = 0.0;
        self.tilt_vel = 0.0;
        self.scale_x = 1.0;
        self.scale_y = 1.0;
        self.drag_clock = timestamp;
        self.drag_history.clear();
        self.drag_history.push_back((timestamp, self.x, self.y));
        self.active_surface = None;
        self.perched = false;
    }

    /// Cleanly exit `Grabbed` if a monitor hotplug or surface loss interrupts an active drag.
    pub fn cancel_grab(&mut self) {
        if self.state != PhysicsState::Grabbed {
            return;
        }
        self.vx = 0.0;
        self.vy = 0.0;
        self.drag_history.clear();
        self.reset_visual_springs();
        self.state = if self.pinned {
            PhysicsState::Pinned
        } else {
            PhysicsState::Idle
        };
    }

    pub fn observe_drag(&mut self, new_x: f64, new_y: f64, timestamp: f64, monitor: MonitorWorkArea) {
        if self.state != PhysicsState::Grabbed {
            self.begin_grab(timestamp);
        }
        let (min_x, max_x, min_y, max_y) = self.clamp_bounds(monitor);
        let dt = (timestamp - self.drag_clock).clamp(1e-4, 0.15);
        self.drag_clock = self.drag_clock.max(timestamp);
        self.x = new_x.clamp(min_x, max_x);
        self.y = new_y.clamp(min_y, max_y);

        if self.drag_history.len() >= 2
            && (self.drag_clock - self.drag_history[self.drag_history.len() - 2].0) < 0.008
        {
            if let Some(last) = self.drag_history.back_mut() {
                *last = (self.drag_clock, self.x, self.y);
            }
        } else {
            self.drag_history.push_back((self.drag_clock, self.x, self.y));
        }

        let cutoff = self.drag_clock - 0.20;
        while self.drag_history.len() > 2 && self.drag_history[0].0 < cutoff {
            self.drag_history.pop_front();
        }

        let (vx, vy) = self.compute_history_velocity(self.drag_clock);
        self.vx = vx;
        self.vy = vy;
        if self.vx.abs() > 25.0 {
            self.facing = if self.vx > 0.0 { 1 } else { -1 };
        }
        if !self.reduced_motion {
            self.flail_phase = (self.flail_phase + dt * 12.0) % TAU;
        }
    }

    fn compute_history_velocity(&self, now: f64) -> (f64, f64) {
        if self.drag_history.len() < 2 {
            return (0.0, 0.0);
        }
        let mut last_move_idx = None;
        for i in (1..self.drag_history.len()).rev() {
            let (_, x1, y1) = self.drag_history[i];
            let (_, x0, y0) = self.drag_history[i - 1];
            if (x1 - x0).hypot(y1 - y0) >= 0.5 {
                last_move_idx = Some(i);
                break;
            }
        }
        let Some(last_idx) = last_move_idx else {
            return (0.0, 0.0);
        };
        let (t1, x1, y1) = self.drag_history[last_idx];
        if (now - t1) >= self.cfg.pause_zero_window {
            return (0.0, 0.0);
        }
        let mut start_idx = last_idx.saturating_sub(1);
        for j in (0..last_idx).rev() {
            let (tj, _, _) = self.drag_history[j];
            start_idx = j;
            if (t1 - tj) >= 0.09 {
                break;
            }
        }
        let (t0, x0, y0) = self.drag_history[start_idx];
        if (x1 - x0).hypot(y1 - y0) < 4.0 {
            return (0.0, 0.0);
        }
        let span = (t1 - t0).max(0.035);
        let vx = ((x1 - x0) / span) * self.cfg.throw_scale;
        let vy = ((y1 - y0) / span) * self.cfg.throw_scale;
        if vx.hypot(vy) < self.cfg.min_throw_speed {
            return (0.0, 0.0);
        }
        (vx, vy)
    }

    pub fn end_grab(
        &mut self,
        shift_held: bool,
        now: f64,
        monitor: MonitorWorkArea,
        surfaces: &[LandingSurface],
    ) -> PhysicsStepEvents {
        let (min_x, max_x, min_y, max_y) = self.clamp_bounds(monitor);
        self.x = self.x.clamp(min_x, max_x);
        self.y = self.y.clamp(min_y, max_y);

        // Re-evaluate velocity at release timestamp so a brief pause before release zeroes throw!
        let (vx, vy) = self.compute_history_velocity(now);
        self.vx = vx;
        self.vy = vy;

        if shift_held || !self.physics_enabled {
            self.pinned = shift_held;
            self.vx = 0.0;
            self.vy = 0.0;
            self.active_surface = None;
            self.perched = false;
            self.state = if self.pinned {
                PhysicsState::Pinned
            } else {
                PhysicsState::Idle
            };
            self.reset_visual_springs();
            return PhysicsStepEvents::default();
        }

        if self.reduced_motion {
            self.pinned = false;
            self.vx = 0.0;
            self.vy = 0.0;
            if let Some(shelf) = self.find_shelf_below(surfaces, monitor, self.y, max_y, 36.0) {
                self.y = self.landing_y_for_surface(shelf, monitor);
                self.x = shelf.clamp_avatar_x(self.x, self.avatar_w).clamp(min_x, max_x);
                self.active_surface = Some(shelf.clone());
                self.perched = true;
            } else {
                self.y = max_y;
                self.active_surface = None;
                self.perched = false;
            }
            self.state = PhysicsState::Idle;
            self.reset_visual_springs();
            return PhysicsStepEvents {
                landed: true,
                wall_hit: false,
                perched: self.perched,
                crossed_monitor_side: 0,
            };
        }

        let speed = self.vx.hypot(self.vy);
        if speed > self.cfg.max_throw_speed && speed > 0.0 {
            let s = self.cfg.max_throw_speed / speed;
            self.vx *= s;
            self.vy *= s;
        }
        self.pinned = false;
        self.bounce_count = 0;

        if self.vy >= -120.0 && self.vx.abs() < 260.0 {
            if let Some(shelf) = self.find_drop_snap_shelf(surfaces, monitor, 36.0) {
                self.y = self.landing_y_for_surface(shelf, monitor);
                self.x = shelf.clamp_avatar_x(self.x, self.avatar_w).clamp(min_x, max_x);
                self.vx = 0.0;
                self.vy = 0.0;
                self.active_surface = Some(shelf.clone());
                self.perched = true;
                self.state = PhysicsState::Landing;
                self.state_timer = self.cfg.landing_duration;
                self.scale_x = 1.07;
                self.scale_y = 0.93;
                self.impact_ripple = 0.45;
                return PhysicsStepEvents {
                    landed: true,
                    wall_hit: false,
                    perched: true,
                    crossed_monitor_side: 0,
                };
            }
        }

        if self.y >= max_y - 1.0 && self.vy >= 0.0 {
            self.y = max_y;
            self.vx = 0.0;
            self.vy = 0.0;
            self.active_surface = None;
            self.perched = false;
            self.state = PhysicsState::Landing;
            self.state_timer = self.cfg.landing_duration;
            self.scale_x = 1.08;
            self.scale_y = 0.92;
            self.impact_ripple = 0.5;
            return PhysicsStepEvents {
                landed: true,
                wall_hit: false,
                perched: false,
                crossed_monitor_side: 0,
            };
        }

        self.active_surface = None;
        self.perched = false;
        self.state = PhysicsState::Falling;
        PhysicsStepEvents::default()
    }

    pub fn step(
        &mut self,
        dt: f64,
        monitor: MonitorWorkArea,
        surfaces: &[LandingSurface],
        adjacent_left: bool,
        adjacent_right: bool,
    ) -> PhysicsStepEvents {
        let step_dt = dt.clamp(1e-4, 0.05);
        let (min_x, max_x, min_y, max_y) = self.clamp_bounds(monitor);

        if self.impact_ripple > 0.0 {
            self.impact_ripple = (self.impact_ripple - step_dt * 3.2).max(0.0);
        }

        if self.reduced_motion {
            self.reset_visual_springs();
            if matches!(
                self.state,
                PhysicsState::Falling | PhysicsState::WallSplat | PhysicsState::Landing
            ) {
                self.vx = 0.0;
                self.vy = 0.0;
                self.y = max_y;
                self.x = self.x.clamp(min_x, max_x);
                self.state = PhysicsState::Idle;
                return PhysicsStepEvents {
                    landed: true,
                    ..Default::default()
                };
            }
            return PhysicsStepEvents::default();
        }

        match self.state {
            PhysicsState::Grabbed => {
                self.step_pendulum(step_dt, 0.0);
                self.flail_phase = (self.flail_phase + step_dt * 12.0) % TAU;
                self.step_squash(step_dt, 1.0, 1.0);
                return PhysicsStepEvents::default();
            }
            PhysicsState::Idle | PhysicsState::Pinned => {
                self.step_pendulum(step_dt, 0.0);
                self.step_squash(step_dt, 1.0, 1.0);
                self.canvas_offset_y *= 0.82;
                if self.canvas_offset_y.abs() < 0.2 {
                    self.canvas_offset_y = 0.0;
                }
                return PhysicsStepEvents::default();
            }
            PhysicsState::WallSplat => {
                self.state_timer -= step_dt;
                self.step_squash(step_dt, 0.84, 1.14);
                self.step_pendulum(step_dt, 0.0);
                if self.state_timer <= 0.0 {
                    let rebound_dir = if self.wall_splat_side < 0 { 1.0 } else { -1.0 };
                    self.vx = rebound_dir * 220.0;
                    self.vy = self.vy.max(40.0);
                    self.wall_splat_side = 0;
                    self.state = PhysicsState::Falling;
                }
                return PhysicsStepEvents::default();
            }
            PhysicsState::Landing => {
                self.state_timer -= step_dt;
                self.step_pendulum(step_dt, 0.0);
                self.step_squash(step_dt, 1.0, 1.0);
                let t_norm = (self.state_timer / self.cfg.landing_duration.max(1e-3)).max(0.0);
                self.canvas_offset_y = -(t_norm * PI).sin() * 6.0 * t_norm;
                // Don't finish the landing while a spring is still moving. Checking
                // only scale position let a zero-crossing end the state mid-oscillation,
                // then the reset below snapped the tilt/scale to neutral in one frame.
                let squash_settled = (self.scale_x - 1.0).abs() < 0.01
                    && (self.scale_y - 1.0).abs() < 0.01
                    && self.scale_vx.abs() < 0.04
                    && self.scale_vy.abs() < 0.04;
                let tilt_settled = self.tilt_deg.abs() < 0.08 && self.tilt_vel.abs() < 0.25;
                if (self.state_timer <= 0.0 && squash_settled && tilt_settled)
                    || self.state_timer <= -0.6
                {
                    self.scale_x = 1.0;
                    self.scale_y = 1.0;
                    self.scale_vx = 0.0;
                    self.scale_vy = 0.0;
                    self.tilt_deg = 0.0;
                    self.tilt_vel = 0.0;
                    self.canvas_offset_y = 0.0;
                    self.state = PhysicsState::Idle;
                }
                return PhysicsStepEvents::default();
            }
            PhysicsState::Falling => {}
        }

        if self.game_mode || !self.physics_enabled {
            self.vx = 0.0;
            self.vy = 0.0;
            self.state = PhysicsState::Idle;
            return PhysicsStepEvents::default();
        }

        self.vy += self.cfg.gravity * step_dt;
        self.vx *= self.cfg.air_drag.powf(step_dt * 60.0);

        let prev_y = self.y;
        self.x += self.vx * step_dt;
        self.y += self.vy * step_dt;

        if self.vx.abs() > 20.0 {
            self.facing = if self.vx > 0.0 { 1 } else { -1 };
        }

        let flight_tilt = (self.vx * 0.008).clamp(-self.cfg.max_tilt_deg, self.cfg.max_tilt_deg);
        self.step_pendulum(step_dt, flight_tilt);
        let fall_stretch = (self.vy.abs() / 9000.0).min(0.05);
        self.step_squash(step_dt, 1.0 - fall_stretch * 0.5, 1.0 + fall_stretch);

        let mut events = PhysicsStepEvents::default();

        if self.y < min_y {
            self.y = min_y;
            if self.vy < 0.0 {
                self.vy = -self.vy * 0.25;
            }
        }

        if self.x <= min_x {
            if adjacent_left && self.vx < -140.0 {
                self.x = min_x;
                events.crossed_monitor_side = -1;
                return events;
            }
            self.x = min_x;
            let impact_vx = self.vx.abs();
            if impact_vx >= self.cfg.wall_splat_speed {
                self.vx = 0.0;
                self.vy *= 0.25;
                self.wall_splat_side = -1;
                self.scale_x = 0.72;
                self.scale_y = 1.24;
                self.state = PhysicsState::WallSplat;
                self.state_timer = self.cfg.wall_splat_duration;
                events.wall_hit = true;
                return events;
            } else if self.vx < 0.0 {
                self.vx = -self.vx * self.cfg.wall_restitution;
                events.wall_hit = impact_vx > 120.0;
            }
        } else if self.x >= max_x {
            if adjacent_right && self.vx > 140.0 {
                self.x = max_x;
                events.crossed_monitor_side = 1;
                return events;
            }
            self.x = max_x;
            let impact_vx = self.vx.abs();
            if impact_vx >= self.cfg.wall_splat_speed {
                self.vx = 0.0;
                self.vy *= 0.25;
                self.wall_splat_side = 1;
                self.scale_x = 0.72;
                self.scale_y = 1.24;
                self.state = PhysicsState::WallSplat;
                self.state_timer = self.cfg.wall_splat_duration;
                events.wall_hit = true;
                return events;
            } else if self.vx > 0.0 {
                self.vx = -self.vx * self.cfg.wall_restitution;
                events.wall_hit = impact_vx > 120.0;
            }
        }

        if self.vy >= 0.0 && !surfaces.is_empty() {
            if let Some(shelf) =
                self.find_shelf_below(surfaces, monitor, prev_y, self.y, self.cfg.shelf_snap_margin)
            {
                let impact = (self.vy.abs() / 1100.0).min(1.0);
                self.y = self.landing_y_for_surface(shelf, monitor);
                self.x = shelf.clamp_avatar_x(self.x, self.avatar_w).clamp(min_x, max_x);
                self.vx = 0.0;
                self.vy = 0.0;
                self.active_surface = Some(shelf.clone());
                self.perched = true;
                self.state = PhysicsState::Landing;
                self.state_timer = self.cfg.landing_duration;
                self.scale_x = 1.0 + 0.08 * impact.max(0.35);
                self.scale_y = 1.0 - 0.07 * impact.max(0.35);
                self.impact_ripple = self.impact_ripple.max((0.35 + impact * 0.45).min(1.0));
                events.landed = true;
                events.perched = true;
                return events;
            }
        }

        if self.y >= max_y {
            self.y = max_y;
            let impact_vy = self.vy.abs();
            let impact = (impact_vy / 1100.0).min(1.0);
            if impact_vy >= self.cfg.min_bounce_vy && self.bounce_count < 1 {
                self.bounce_count += 1;
                self.vy = -impact_vy * self.cfg.restitution;
                self.vx *= 0.65;
                self.scale_x = 1.0 + 0.10 * impact;
                self.scale_y = 1.0 - 0.09 * impact;
                self.impact_ripple = self.impact_ripple.max((0.35 + impact * 0.5).min(1.0));
            } else {
                self.vx = 0.0;
                self.vy = 0.0;
                self.active_surface = None;
                self.perched = false;
                self.state = PhysicsState::Landing;
                self.state_timer = self.cfg.landing_duration;
                self.scale_x = 1.0 + 0.09 * impact.max(0.3);
                self.scale_y = 1.0 - 0.08 * impact.max(0.3);
                self.impact_ripple = self.impact_ripple.max((0.30 + impact * 0.5).min(1.0));
                events.landed = true;
            }
        }

        events
    }

    fn step_pendulum(&mut self, dt: f64, target_deg: f64) {
        let accel = (target_deg - self.tilt_deg) * self.cfg.pendulum_stiffness
            - self.tilt_vel * self.cfg.pendulum_damping;
        self.tilt_vel += accel * dt;
        self.tilt_deg = (self.tilt_deg + self.tilt_vel * dt)
            .clamp(-self.cfg.max_tilt_deg, self.cfg.max_tilt_deg);
    }

    fn step_squash(&mut self, dt: f64, target_sx: f64, target_sy: f64) {
        let ax = (target_sx - self.scale_x) * self.cfg.squash_stiffness
            - self.scale_vx * self.cfg.squash_damping;
        let ay = (target_sy - self.scale_y) * self.cfg.squash_stiffness
            - self.scale_vy * self.cfg.squash_damping;
        self.scale_vx += ax * dt;
        self.scale_vy += ay * dt;
        self.scale_x = (self.scale_x + self.scale_vx * dt).clamp(0.7, 1.3);
        self.scale_y = (self.scale_y + self.scale_vy * dt).clamp(0.7, 1.3);
    }
}

/// Best-effort query for visible Hyprland/Niri floating window tops to perch on.
/// Called only on drag-release so idle CPU remains unaffected.
pub fn query_window_shelves(monitor: MonitorWorkArea) -> Vec<LandingSurface> {
    let mut shelves = Vec::new();
    if std::env::var_os("NIRI_SOCKET").is_some() {
        if let Ok(out) = Command::new("niri").args(["msg", "-j", "windows"]).output() {
            if out.status.success() {
                if let Ok(val) = serde_json::from_slice::<serde_json::Value>(&out.stdout) {
                    if let Some(arr) = val.as_array() {
                        for win in arr {
                            let is_floating = win
                                .get("is_floating")
                                .and_then(serde_json::Value::as_bool)
                                .unwrap_or(false);
                            if !is_floating {
                                continue;
                            }
                            let Some(layout) = win.get("layout") else {
                                continue;
                            };
                            let Some(pos) = layout
                                .get("tile_pos_in_workspace_view")
                                .and_then(serde_json::Value::as_array)
                            else {
                                continue;
                            };
                            let Some(size) = layout
                                .get("window_size")
                                .and_then(serde_json::Value::as_array)
                            else {
                                continue;
                            };
                            if pos.len() < 2 || size.len() < 2 {
                                continue;
                            }
                            let x = pos[0].as_f64().unwrap_or(0.0);
                            let y = pos[1].as_f64().unwrap_or(0.0);
                            let w = size[0].as_f64().unwrap_or(0.0);
                            if w >= 180.0 && y > monitor.top + 80.0 && y < monitor.bottom() - 80.0 {
                                shelves.push(LandingSurface {
                                    kind: SurfaceKind::WindowTop,
                                    x_min: x,
                                    x_max: x + w,
                                    y,
                                    priority: 10,
                                });
                            }
                        }
                    }
                }
            }
        }
    } else if std::env::var_os("HYPRLAND_INSTANCE_SIGNATURE").is_some() {
        if let Ok(out) = Command::new("hyprctl").args(["-j", "clients"]).output() {
            if out.status.success() {
                if let Ok(val) = serde_json::from_slice::<serde_json::Value>(&out.stdout) {
                    if let Some(arr) = val.as_array() {
                        for win in arr {
                            let mapped = win
                                .get("mapped")
                                .and_then(serde_json::Value::as_bool)
                                .unwrap_or(false);
                            if !mapped {
                                continue;
                            }
                            let Some(at) = win.get("at").and_then(serde_json::Value::as_array) else {
                                continue;
                            };
                            let Some(sz) = win.get("size").and_then(serde_json::Value::as_array) else {
                                continue;
                            };
                            if at.len() < 2 || sz.len() < 2 {
                                continue;
                            }
                            let x = at[0].as_f64().unwrap_or(0.0);
                            let y = at[1].as_f64().unwrap_or(0.0);
                            let w = sz[0].as_f64().unwrap_or(0.0);
                            if w >= 180.0 && y > monitor.top + 80.0 && y < monitor.bottom() - 80.0 {
                                shelves.push(LandingSurface {
                                    kind: SurfaceKind::WindowTop,
                                    x_min: x,
                                    x_max: x + w,
                                    y,
                                    priority: 10,
                                });
                            }
                        }
                    }
                }
            }
        }
    }
    shelves
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gravity_fall_and_single_bounce_settle_on_floor() {
        let mon = MonitorWorkArea::from_size(1920, 1080);
        let mut phys = AvatarPhysics2D::new(300.0, 420.0, 800.0, 100.0);
        phys.begin_grab(0.0);
        phys.observe_drag(800.0, 100.0, 0.1, mon);
        phys.end_grab(false, 0.2, mon, &[]);
        assert_eq!(phys.state, PhysicsState::Falling);

        let mut landed = false;
        for _ in 0..240 {
            let ev = phys.step(1.0 / 60.0, mon, &[], false, false);
            if ev.landed {
                landed = true;
            }
            if phys.state == PhysicsState::Idle {
                break;
            }
        }
        assert!(landed);
        assert_eq!(phys.state, PhysicsState::Idle);
        assert!((phys.y - (1080.0 - 420.0)).abs() < 1e-3);
    }

    #[test]
    fn shift_release_pins_avatar_mid_air() {
        let mon = MonitorWorkArea::from_size(1920, 1080);
        let mut phys = AvatarPhysics2D::new(300.0, 420.0, 500.0, 200.0);
        phys.begin_grab(0.0);
        phys.end_grab(true, 0.05, mon, &[]);
        assert_eq!(phys.state, PhysicsState::Pinned);
        assert!(phys.pinned);
        phys.step(1.0 / 60.0, mon, &[], false, false);
        assert_eq!(phys.y, 200.0);
    }

    #[test]
    fn fast_horizontal_throw_crosses_into_adjacent_monitor() {
        let mon = MonitorWorkArea::from_size(1920, 1080);
        let mut phys = AvatarPhysics2D::new(300.0, 420.0, 1600.0, 200.0);
        phys.state = PhysicsState::Falling;
        phys.vx = 900.0;
        phys.vy = 0.0;
        let ev = phys.step(0.05, mon, &[], false, true);
        assert_eq!(ev.crossed_monitor_side, 1);
        assert_eq!(phys.state, PhysicsState::Falling);
    }

    #[test]
    fn cancel_grab_recovers_from_hotplug_mid_drag() {
        let mon = MonitorWorkArea::from_size(1920, 1080);
        let mut phys = AvatarPhysics2D::new(300.0, 420.0, 600.0, 300.0);
        phys.begin_grab(0.0);
        phys.observe_drag(720.0, 260.0, 0.05, mon);
        assert_eq!(phys.state, PhysicsState::Grabbed);
        phys.cancel_grab();
        assert_eq!(phys.state, PhysicsState::Idle);
        assert_eq!(phys.vx, 0.0);
        assert_eq!(phys.vy, 0.0);
    }
}
