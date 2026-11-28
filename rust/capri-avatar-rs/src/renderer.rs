//! 2D Canvas Renderer (`tiny-skia` + `fontdue`) and Animation State Machine.
//!
//! Renders Capri's 300x420 logical avatar frame:
//! - Perspective holographic ground aura, Pomodoro progress ring, audio projection rings & motes
//! - Landing shockwave rings & sparks, fast-fall afterimages, chromatic glitch strips
//! - Sprite composite (`idle` + `blink`/`blink_glitch` + `mouth` lip-sync or active pose)
//! - Listening mic indicator & LIVING SIGNAL rounded speech bubble with Cyrillic font support.

use std::f32::consts::{PI, TAU};
use std::path::Path;
use std::time::Instant;

use fontdue::{Font, FontSettings};
use tiny_skia::{
    BlendMode, Color, FilterQuality, GradientStop, LineCap, Paint, PathBuilder, Pixmap,
    PixmapPaint, Point, PremultipliedColorU8, RadialGradient, Rect, SpreadMode, Stroke, Transform,
};

use crate::physics::{AvatarPhysics2D, PhysicsState};
use crate::presence::PresenceSnapshot;
use crate::protocol::IpcCommand;
use crate::sprites::{SpriteStore, DEFAULT_TARGET_H, DEFAULT_TARGET_W};

pub const CANVAS_W: u32 = 300;
pub const CANVAS_H: u32 = 420;
const IDLE_SLEEP_TICKS: u64 = 60 * 180;

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum LocomotionRequest {
    Come { target_x: Option<f64>, target_y: Option<f64> },
    Home,
}

pub struct AvatarVisualState {
    pub state: String,
    pub pose: String,
    pub mood: String,
    pub caption_text: String,
    caption_ticks: u64,
    pub follow_cursor: bool,
    pub pending_locomotion: Option<LocomotionRequest>,

    pub tick_count: u64,
    pub hover_phase: f32,
    pub idle_ticks: u64,

    pub blink_active: bool,
    pub blink_is_glitch: bool,
    pub blink_frame: usize,
    pub blink_timer: i64,

    pub glitch_active: bool,
    pub glitch_frame: usize,
    pub dance_active: bool,
    pub waking_active: bool,
    pub wake_frame: usize,

    pub mouth_envelope: Vec<u8>,
    pub mouth_env_pos: usize,
    pub current_mouth_idx: usize,
    pub audio_level_target: f32,
    pub audio_energy: f32,
    audio_level_ts: Option<Instant>,

    pub reduced_motion: bool,
    pub power_saver_active: bool,
    pub game_mode_enabled: bool,
    pub game_mode_override: Option<bool>,
    pub auto_poses_enabled: bool,
    pub reactions_enabled: bool,
    pub autonomous_life_enabled: bool,
    pub manual_pose_override: Option<String>,
    pub media_playing: bool,
    pub media_track_label: String,
    pub last_announced_track: String,

    pub focus_active: bool,
    pub focus_progress: f32,
    focus_started_at: Option<Instant>,
    focus_duration_secs: f64,

    rng_state: u64,
}

impl Default for AvatarVisualState {
    fn default() -> Self {
        Self {
            state: "idle".to_string(),
            pose: "normal".to_string(),
            mood: "neutral".to_string(),
            caption_text: String::new(),
            caption_ticks: 0,
            follow_cursor: false,
            pending_locomotion: None,
            tick_count: 0,
            hover_phase: 0.0,
            idle_ticks: 0,
            blink_active: false,
            blink_is_glitch: false,
            blink_frame: 0,
            blink_timer: 240,
            glitch_active: false,
            glitch_frame: 0,
            dance_active: false,
            waking_active: false,
            wake_frame: 0,
            mouth_envelope: Vec::new(),
            mouth_env_pos: 0,
            current_mouth_idx: 0,
            audio_level_target: 0.0,
            audio_energy: 0.0,
            audio_level_ts: None,
            reduced_motion: false,
            power_saver_active: false,
            game_mode_enabled: true,
            game_mode_override: None,
            auto_poses_enabled: true,
            reactions_enabled: true,
            autonomous_life_enabled: true,
            manual_pose_override: None,
            media_playing: false,
            media_track_label: String::new(),
            last_announced_track: String::new(),
            focus_active: false,
            focus_progress: 0.0,
            focus_started_at: None,
            focus_duration_secs: 25.0 * 60.0,
            rng_state: 0x5EED_CAFE_1234_5678,
        }
    }
}

impl AvatarVisualState {
    fn next_rand_f32(&mut self) -> f32 {
        // Lightweight deterministic xorshift64* (no `rand` crate overhead)
        let mut x = self.rng_state;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.rng_state = x;
        let v = x.wrapping_mul(0x2545_F491_4F6C_DD1D) >> 40;
        (v as f32) / ((1u32 << 24) as f32)
    }

    fn next_blink_delay(&mut self) -> i64 {
        180 + (self.next_rand_f32() * 180.0) as i64
    }

    pub fn wake_up(&mut self, sprites: &mut SpriteStore) {
        self.idle_ticks = 0;
        if self.pose == "sleep" {
            self.pose = "normal".to_string();
            if !sprites.get_slot("wake").is_empty() {
                self.waking_active = true;
                self.wake_frame = 0;
            }
        }
    }

    pub fn trigger_glitch(&mut self, sprites: &mut SpriteStore) {
        self.wake_up(sprites);
        self.glitch_active = true;
        self.glitch_frame = 0;
    }

    pub fn show_ephemeral_caption(&mut self, text: impl Into<String>, ticks: u64) {
        let raw = text.into();
        let clean = raw
            .replace('\n', " ")
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        if clean.is_empty() {
            return;
        }
        let clipped = if clean.chars().count() > 86 {
            let mut s: String = clean.chars().take(83).collect();
            s.push('…');
            s
        } else {
            clean
        };
        self.caption_text = clipped;
        self.caption_ticks = ticks.max(60);
    }

    /// Apply a fresh Living Awareness snapshot (MPRIS playback + auto Game Mode) and sync pose.
    /// Returns `true` if any visible state changed and requires an immediate redraw.
    pub fn apply_presence_snapshot(
        &mut self,
        snap: PresenceSnapshot,
        sprites: &mut SpriteStore,
        physics: &mut AvatarPhysics2D,
    ) -> bool {
        let prev_power = self.power_saver_active;
        let prev_pose = self.pose.clone();
        let prev_caption = self.caption_text.clone();

        if self.game_mode_override.is_none() {
            let effective_gm = snap.game_mode.active && self.game_mode_enabled;
            self.set_power_saver(effective_gm, sprites, physics);
        }

        self.media_playing = snap.media.is_playing;
        self.media_track_label = snap.media.track_label;
        if !self.media_playing {
            self.last_announced_track.clear();
        }

        self.sync_auto_pose(sprites, physics);

        self.power_saver_active != prev_power
            || self.pose != prev_pose
            || self.caption_text != prev_caption
    }

    /// Deterministic auto-pose resolver matching Python `capri/core/presence.py`.
    pub fn sync_auto_pose(&mut self, sprites: &mut SpriteStore, physics: &AvatarPhysics2D) {
        if self.power_saver_active {
            self.pose = "sleep".to_string();
            return;
        }
        if self.state != "idle" {
            if self.pose == "sleep" {
                self.wake_up(sprites);
            }
            if self.pose != "normal" {
                self.pose = "normal".to_string();
            }
            return;
        }
        if self.dance_active {
            return;
        }
        if let Some(manual) = self.manual_pose_override.clone() {
            if self.pose != manual {
                if self.pose == "sleep" && manual != "sleep" {
                    self.wake_up(sprites);
                }
                self.pose = manual;
            }
            return;
        }
        if self.auto_poses_enabled && self.media_playing {
            if self.pose == "sleep" {
                self.wake_up(sprites);
            }
            if self.pose != "perch_music" {
                self.pose = "perch_music".to_string();
            }
            if self.reactions_enabled
                && !self.media_track_label.is_empty()
                && self.media_track_label != self.last_announced_track
            {
                self.last_announced_track = self.media_track_label.clone();
                let label = self.media_track_label.clone();
                self.show_ephemeral_caption(format!("♪ {label}"), 210);
            }
            return;
        }
        if self.auto_poses_enabled
            && self.idle_ticks >= IDLE_SLEEP_TICKS
            && !sprites.get_slot("sleep").is_empty()
        {
            self.pose = "sleep".to_string();
            return;
        }
        let target = if physics.perched { "perch" } else { "normal" };
        if self.pose != target {
            if self.pose == "sleep" {
                self.wake_up(sprites);
            }
            self.pose = target.to_string();
        }
    }

    pub fn focus_remaining_mm_ss(&self) -> String {
        if !self.focus_active {
            return "OFF".to_string();
        }
        let elapsed = self
            .focus_started_at
            .map(|t| t.elapsed().as_secs_f64())
            .unwrap_or(0.0);
        let rem = (self.focus_duration_secs - elapsed).max(0.0).ceil() as u64;
        format!("{:02}:{:02}", rem / 60, rem % 60)
    }

    /// Cycle Pomodoro presets identically to Python (`OFF -> 25m -> 50m -> 15m -> OFF`).
    pub fn cycle_focus_timer(&mut self) -> Option<u32> {
        let next_mins = if !self.focus_active {
            Some(25)
        } else {
            let cur_mins = (self.focus_duration_secs / 60.0).round() as u32;
            match cur_mins {
                25 => Some(50),
                50 => Some(15),
                _ => None,
            }
        };
        if let Some(m) = next_mins {
            self.focus_active = true;
            self.focus_progress = 0.0;
            self.focus_duration_secs = (m as f64) * 60.0;
            self.focus_started_at = Some(Instant::now());
        } else {
            self.focus_active = false;
            self.focus_progress = 0.0;
            self.focus_started_at = None;
        }
        next_mins
    }

    /// Cycle Game Mode override identically to Python (`AUTO -> FORCE ON -> OFF -> AUTO`).
    pub fn cycle_game_mode(&mut self, sprites: &mut SpriteStore, physics: &mut AvatarPhysics2D) {
        match self.game_mode_override {
            None => {
                self.game_mode_override = Some(true);
                self.set_power_saver(true, sprites, physics);
            }
            Some(true) => {
                self.game_mode_override = Some(false);
                self.set_power_saver(false, sprites, physics);
            }
            Some(false) => {
                self.game_mode_override = None;
                self.set_power_saver(false, sprites, physics);
            }
        }
    }

    /// Adaptive timer interval matching Python v3.7.0 (`16ms` active / `64ms` calm idle / `120ms` sleep / `250ms` reduced motion / `None` in game mode).
    pub fn target_frame_interval_ms(&self, physics: &AvatarPhysics2D) -> Option<u64> {
        if self.power_saver_active {
            return None;
        }
        if self.reduced_motion {
            return Some(250);
        }
        let high_rate = physics.is_animating()
            || self.state != "idle"
            || self.blink_active
            || self.glitch_active
            || self.waking_active
            || self.dance_active
            || self.audio_energy > 0.01
            || self.pose == "walk";
        if high_rate {
            return Some(16);
        }
        if self.pose == "sleep" {
            return Some(120);
        }
        Some(64)
    }

    pub fn apply_ipc_command(
        &mut self,
        cmd: IpcCommand,
        sprites: &mut SpriteStore,
        physics: &mut AvatarPhysics2D,
    ) -> bool {
        match cmd {
            IpcCommand::Quit => return true,
            IpcCommand::ReloadSprites => {
                sprites.reload();
            }
            IpcCommand::AudioLevel { level, .. } => {
                self.audio_level_ts = Some(Instant::now());
                if self.reduced_motion {
                    self.audio_level_target = 0.0;
                    self.audio_energy = 0.0;
                } else {
                    self.audio_level_target = level.clamp(0.0, 1.0);
                }
            }
            IpcCommand::PowerSaver {
                active,
                override_set,
                override_val,
                ..
            } => {
                if override_set {
                    self.game_mode_override = override_val;
                }
                let effective = match self.game_mode_override {
                    Some(v) => v,
                    None => active.unwrap_or(false) && self.game_mode_enabled,
                };
                self.set_power_saver(effective, sprites, physics);
            }
            IpcCommand::ConfigSync {
                reduced_motion,
                physics_enabled,
                game_mode_enabled,
                auto_avatar_poses,
                reactions_enabled,
                autonomous_life_enabled,
                ..
            } => {
                if let Some(rm) = reduced_motion {
                    self.reduced_motion = rm;
                    physics.set_reduced_motion(rm);
                    if rm {
                        self.audio_level_target = 0.0;
                        self.audio_energy = 0.0;
                    }
                }
                if let Some(pe) = physics_enabled {
                    physics.set_physics_enabled(pe);
                }
                if let Some(ge) = game_mode_enabled {
                    self.game_mode_enabled = ge;
                    if !ge && self.game_mode_override.is_none() {
                        self.set_power_saver(false, sprites, physics);
                    }
                }
                if let Some(ap) = auto_avatar_poses {
                    self.auto_poses_enabled = ap;
                    self.sync_auto_pose(sprites, physics);
                }
                if let Some(re) = reactions_enabled {
                    self.reactions_enabled = re;
                }
                if let Some(al) = autonomous_life_enabled {
                    self.autonomous_life_enabled = al;
                }
            }
            IpcCommand::FocusTimer {
                action,
                minutes,
                seconds,
            } => match action.as_str() {
                "stop" => {
                    self.focus_active = false;
                    self.focus_progress = 0.0;
                    self.focus_started_at = None;
                }
                "start" => {
                    let dur = seconds
                        .or_else(|| minutes.map(|m| m * 60.0))
                        .unwrap_or(25.0 * 60.0)
                        .max(1.0);
                    self.focus_active = true;
                    self.focus_progress = 0.0;
                    self.focus_duration_secs = dur;
                    self.focus_started_at = Some(Instant::now());
                }
                _ => {
                    if self.focus_active {
                        self.focus_active = false;
                        self.focus_progress = 0.0;
                        self.focus_started_at = None;
                    } else {
                        self.focus_active = true;
                        self.focus_progress = 0.0;
                        self.focus_duration_secs = 25.0 * 60.0;
                        self.focus_started_at = Some(Instant::now());
                    }
                }
            },
            IpcCommand::Locomotion {
                action,
                target_x,
                target_y,
                enabled,
            } => match action.as_str() {
                "stay" => {
                    self.follow_cursor = false;
                    physics.set_pinned(enabled.unwrap_or(true));
                }
                "follow" => {
                    let on = enabled.unwrap_or(!self.follow_cursor);
                    self.follow_cursor = on;
                    if on {
                        physics.set_pinned(false);
                    }
                }
                "come" => {
                    physics.set_pinned(false);
                    self.pending_locomotion = Some(LocomotionRequest::Come { target_x, target_y });
                }
                "teleport" | "glide" => {
                    if let (Some(tx), Some(ty)) = (target_x, target_y) {
                        physics.x = tx;
                        physics.y = ty;
                        physics.vx = 0.0;
                        physics.vy = 0.0;
                    }
                }
                "home" => {
                    self.follow_cursor = false;
                    physics.set_pinned(false);
                    self.pending_locomotion = Some(LocomotionRequest::Home);
                }
                _ => {}
            },
            IpcCommand::StateUpdate {
                state,
                text,
                pose,
                mood,
                mouth_envelope,
            } => {
                if let Some(m) = mood {
                    self.mood = match m.as_str() {
                        "warm" | "playful" | "sarcastic" | "annoyed" | "neutral" => m,
                        _ => "neutral".to_string(),
                    };
                }
                if let Some(env) = mouth_envelope {
                    if env.is_empty() {
                        self.mouth_envelope.clear();
                        self.mouth_env_pos = 0;
                        self.current_mouth_idx = 0;
                    } else {
                        self.mouth_envelope = env;
                        self.mouth_env_pos = 0;
                        self.current_mouth_idx = self.mouth_envelope[0] as usize;
                        self.wake_up(sprites);
                    }
                }
                if let Some(p) = pose {
                    match p.as_str() {
                        "wake" => self.wake_up(sprites),
                        "normal" | "auto" => {
                            self.manual_pose_override = None;
                            if self.pose == "sleep" {
                                self.wake_up(sprites);
                            }
                            self.pose = "normal".to_string();
                            self.idle_ticks = 0;
                            self.sync_auto_pose(sprites, physics);
                        }
                        "perch" | "perch_music" | "lounge" | "walk" | "sleep" => {
                            if self.pose == "sleep" && p != "sleep" {
                                self.wake_up(sprites);
                            }
                            self.manual_pose_override = Some(p.clone());
                            self.pose = p;
                            self.idle_ticks = 0;
                        }
                        _ => {}
                    }
                }
                if let Some(st) = state {
                    self.state = st;
                    if self.state != "idle" {
                        self.wake_up(sprites);
                    } else {
                        self.sync_auto_pose(sprites, physics);
                    }
                }
                if let Some(txt) = text {
                    self.caption_text = txt;
                    self.caption_ticks = if self.caption_text.is_empty() { 0 } else { 360 };
                }
            }
        }
        false
    }

    fn set_power_saver(
        &mut self,
        effective: bool,
        sprites: &mut SpriteStore,
        physics: &mut AvatarPhysics2D,
    ) {
        if effective == self.power_saver_active {
            return;
        }
        self.power_saver_active = effective;
        physics.set_game_mode(effective);
        if effective {
            self.caption_text.clear();
            self.caption_ticks = 0;
            self.pose = "sleep".to_string();
        } else {
            self.wake_up(sprites);
            self.sync_auto_pose(sprites, physics);
        }
    }

    pub fn advance(&mut self, step_ticks: u64, phys_active: bool, sprites: &mut SpriteStore) {
        let ticks = step_ticks.max(1);
        let prev_tick = self.tick_count;
        self.tick_count += ticks;

        if self.caption_ticks > 0 {
            self.caption_ticks = self.caption_ticks.saturating_sub(ticks);
            if self.caption_ticks == 0 && self.state == "idle" {
                self.caption_text.clear();
            }
        }

        if self.focus_active {
            if let Some(start) = self.focus_started_at {
                let elapsed = start.elapsed().as_secs_f64();
                self.focus_progress = (elapsed / self.focus_duration_secs).clamp(0.0, 1.0) as f32;
                if self.focus_progress >= 1.0 {
                    self.focus_active = false;
                    self.focus_started_at = None;
                }
            }
        }

        // Keep hover phase anchored at 0 while physics controls vertical position so
        // transitioning from `Landing -> Idle` resumes hovering smoothly from 0px (no pop).
        if phys_active {
            self.hover_phase = 0.0;
        } else {
            let phase_step = if self.pose == "sleep" { 0.022 } else { 0.045 };
            self.hover_phase = (self.hover_phase + phase_step * (ticks as f32)) % TAU;
        }

        if self.state != "idle" {
            self.idle_ticks = 0;
            if self.pose == "sleep" {
                self.wake_up(sprites);
            }
        } else {
            self.idle_ticks += ticks;
            if self.pose == "normal"
                && self.auto_poses_enabled
                && self.manual_pose_override.is_none()
                && !self.media_playing
                && !self.dance_active
                && self.idle_ticks >= IDLE_SLEEP_TICKS
                && !sprites.get_slot("sleep").is_empty()
            {
                self.pose = "sleep".to_string();
            }
        }

        let step_3 = (self.tick_count / 3) - (prev_tick / 3);
        let step_4 = (self.tick_count / 4) - (prev_tick / 4);

        if self.waking_active && step_3 > 0 {
            self.wake_frame += step_3 as usize;
            if self.wake_frame >= sprites.get_slot("wake").len() {
                self.waking_active = false;
                self.wake_frame = 0;
            }
        }

        if !self.blink_active {
            self.blink_timer -= ticks as i64;
            if self.blink_timer <= 0 {
                self.blink_active = true;
                self.blink_frame = 0;
                let glitch_prob = if matches!(self.mood.as_str(), "sarcastic" | "annoyed") {
                    0.55
                } else {
                    0.18
                };
                self.blink_is_glitch =
                    self.next_rand_f32() < glitch_prob && !sprites.get_slot("blink_glitch").is_empty();
            }
        } else if step_3 > 0 {
            self.blink_frame += step_3 as usize;
            let len = if self.blink_is_glitch {
                sprites.get_slot("blink_glitch").len()
            } else {
                sprites.get_slot("blink").len()
            };
            if self.blink_frame >= len.max(1) {
                self.blink_active = false;
                self.blink_is_glitch = false;
                self.blink_timer = self.next_blink_delay();
            }
        }

        if self.glitch_active && step_3 > 0 {
            self.glitch_frame += step_3 as usize;
            if self.glitch_frame >= sprites.get_slot("glitch").len().max(1) {
                self.glitch_active = false;
            }
        }

        if self.state == "speaking" && !sprites.get_slot("mouth").is_empty() {
            if step_4 > 0 {
                if !self.mouth_envelope.is_empty() {
                    if self.mouth_env_pos < self.mouth_envelope.len() {
                        self.current_mouth_idx = self.mouth_envelope[self.mouth_env_pos] as usize;
                        self.mouth_env_pos =
                            (self.mouth_env_pos + step_4 as usize).min(self.mouth_envelope.len());
                    } else {
                        self.current_mouth_idx = usize::from((self.tick_count / 4).is_multiple_of(2));
                    }
                } else {
                    let mlen = sprites.get_slot("mouth").len().max(1);
                    self.current_mouth_idx = ((self.tick_count / 4) as usize) % mlen;
                }
            }
        } else {
            self.current_mouth_idx = 0;
        }

        if self.reduced_motion {
            self.audio_level_target = 0.0;
            self.audio_energy = 0.0;
        } else {
            let target_e = if self.state == "speaking" {
                (self.current_mouth_idx as f32 / 4.0).clamp(0.0, 1.0).max(0.16)
            } else if self.state == "listening" {
                if let Some(ts) = self.audio_level_ts {
                    if ts.elapsed().as_secs_f32() > 0.45 {
                        self.audio_level_target *= 0.72;
                    }
                }
                self.audio_level_target.max(0.08)
            } else {
                self.audio_level_target = 0.0;
                0.0
            };
            let blend = if target_e > self.audio_energy { 0.42 } else { 0.18 };
            self.audio_energy += (target_e - self.audio_energy) * blend;
            if self.audio_energy < 0.005 {
                self.audio_energy = 0.0;
            }
        }
    }

    fn active_aura_rgba(&self) -> (u8, u8, u8, u8) {
        if self.focus_active && self.state == "idle" {
            return (181, 155, 255, 96);
        }
        match self.state.as_str() {
            "listening" => (242, 119, 154, 92),
            "thinking" => (233, 183, 101, 82),
            "speaking" => (105, 213, 232, 98),
            _ if self.pose == "sleep" => (109, 123, 146, 48),
            _ => match self.mood.as_str() {
                "warm" => (91, 205, 162, 82),
                "playful" => (181, 155, 255, 84),
                "sarcastic" => (242, 119, 154, 86),
                "annoyed" => (233, 98, 118, 92),
                _ => (105, 213, 232, 72),
            },
        }
    }
}

type AuraCacheKey = (u8, u8, u8, u8, u16, u16);

pub struct AvatarRenderer {
    canvas: Pixmap,
    font: Option<Font>,
    cached_aura_key: Option<AuraCacheKey>,
    cached_aura_pixmap: Option<Pixmap>,
    cached_bubble_key: Option<(String, i32)>,
    cached_bubble_pixmap: Option<Pixmap>,
}

impl AvatarRenderer {
    pub fn new() -> Self {
        let canvas = Pixmap::new(CANVAS_W, CANVAS_H).expect("allocate 300x420 avatar canvas");
        let font = load_system_ui_font();
        Self {
            canvas,
            font,
            cached_aura_key: None,
            cached_aura_pixmap: None,
            cached_bubble_key: None,
            cached_bubble_pixmap: None,
        }
    }

    pub fn ui_font(&self) -> Option<&Font> {
        self.font.as_ref()
    }

    pub fn render(
        &mut self,
        vis: &AvatarVisualState,
        phys: &AvatarPhysics2D,
        sprites: &mut SpriteStore,
    ) -> &Pixmap {
        self.canvas.fill(Color::TRANSPARENT);

        let cw = CANVAS_W as f32;
        let ch = CANVAS_H as f32;
        let target_w = DEFAULT_TARGET_W as f32;
        let target_h = DEFAULT_TARGET_H as f32;

        let char_x = (cw - target_w) * 0.5;
        let phys_active = matches!(
            phys.state,
            PhysicsState::Grabbed
                | PhysicsState::Falling
                | PhysicsState::WallSplat
                | PhysicsState::Landing
        );
        let hover_y = if vis.reduced_motion || phys_active {
            0.0
        } else {
            let amp = if matches!(vis.pose.as_str(), "sleep" | "perch" | "perch_music") {
                2.0
            } else {
                5.0
            };
            vis.hover_phase.sin() * amp
        };
        let base_char_y = (ch - target_h - 14.0).max(72.0);
        let char_y = (base_char_y
            + hover_y
            + if vis.reduced_motion {
                0.0
            } else {
                phys.canvas_offset_y as f32
            })
        .max(48.0);

        let master_opacity = if vis.power_saver_active { 0.15f32 } else { 1.0f32 };

        // 1. Perspective holographic ground aura (cached per quantized color & radius)
        let (ar, ag, ab, aa) = vis.active_aura_rgba();
        let audio_e = if vis.reduced_motion {
            0.0
        } else {
            vis.audio_energy.clamp(0.0, 1.0)
        };
        let foot_dx = if vis.pose == "normal" && !vis.dance_active {
            18.0
        } else {
            0.0
        };
        let cx = cw * 0.5 + foot_dx;
        let aura_rx = 66.0 + audio_e * 22.0;
        let aura_ry = 15.0 + audio_e * 5.0;
        let aura_cy = (base_char_y + target_h - 20.0).min(ch - 24.0);
        let boosted_a = ((aa as f32 + audio_e * 72.0).min(195.0) * master_opacity) as u8;
        let aura_key: AuraCacheKey = (
            ar,
            ag,
            ab,
            boosted_a & !3,
            (aura_rx.round() as u16) & !1,
            aura_ry.round() as u16,
        );
        if self.cached_aura_key != Some(aura_key) {
            let aw = (aura_key.4 as u32 * 2 + 4).min(CANVAS_W);
            let ah = (aura_key.5 as u32 * 2 + 4).min(80);
            if let Some(mut apm) = Pixmap::new(aw.max(4), ah.max(4)) {
                apm.fill(Color::TRANSPARENT);
                draw_radial_aura(
                    &mut apm,
                    (aw as f32 * 0.5, ah as f32 * 0.5),
                    (aura_key.4 as f32, aura_key.5 as f32),
                    (ar, ag, ab, aura_key.3),
                );
                self.cached_aura_pixmap = Some(apm);
                self.cached_aura_key = Some(aura_key);
            }
        }
        if let Some(ref apm) = self.cached_aura_pixmap {
            let ax = (cx - apm.width() as f32 * 0.5).round() as i32;
            let ay = (aura_cy - apm.height() as f32 * 0.5).round() as i32;
            let aura_paint = PixmapPaint {
                opacity: 1.0,
                blend_mode: BlendMode::SourceOver,
                quality: FilterQuality::Nearest,
            };
            self.canvas
                .draw_pixmap(ax, ay, apm.as_ref(), &aura_paint, Transform::identity(), None);
        }

        // 1a-focus. Pomodoro focus ring
        if vis.focus_active {
            let rem_frac = (1.0 - vis.focus_progress).clamp(0.02, 1.0);
            stroke_ellipse(&mut self.canvas, cx, aura_cy, 34.0, 8.0, (181, 155, 255, 42), 1.4);
            let arc_a = if vis.reduced_motion {
                165
            } else {
                (145.0 + 40.0 * (vis.hover_phase * 0.7).sin().abs()) as u8
            };
            stroke_ellipse_arc(
                &mut self.canvas,
                (cx, aura_cy),
                (34.0, 8.0),
                (-PI * 0.5, rem_frac * TAU),
                (181, 155, 255, arc_a),
                2.1,
            );
        }

        // 1a. Voice projection rings & rising motes
        if !vis.reduced_motion && matches!(vis.state.as_str(), "speaking" | "listening") && audio_e > 0.02 {
            let (rc, gc, bc) = if vis.state == "listening" {
                (242, 119, 154)
            } else {
                (105, 213, 232)
            };
            let inner_rx = 28.0 + audio_e * 18.0;
            let inner_ry = 6.0 + audio_e * 4.0;
            stroke_ellipse(
                &mut self.canvas,
                cx,
                aura_cy,
                inner_rx,
                inner_ry,
                (rc, gc, bc, (45.0 + audio_e * 115.0) as u8),
                1.4,
            );
            let wave_p = (vis.hover_phase * 0.85) % 1.0;
            let outer_rx = inner_rx + wave_p * (20.0 + audio_e * 14.0);
            let outer_ry = (inner_ry + wave_p * (4.0 + audio_e * 3.0)).min(16.0);
            let outer_a = ((1.0 - wave_p) * (35.0 + audio_e * 95.0)) as u8;
            if outer_a > 4 {
                stroke_ellipse(
                    &mut self.canvas,
                    cx,
                    aura_cy,
                    outer_rx,
                    outer_ry,
                    (rc, gc, bc, outer_a),
                    1.0,
                );
            }
            for idx in 0..6 {
                let ang = (TAU / 6.0) * (idx as f32) + vis.hover_phase * 0.9;
                let lift = (vis.hover_phase * 0.7 + (idx as f32) * 0.17) % 1.0;
                let mx = cx + ang.cos() * (20.0 + audio_e * 20.0);
                let my = aura_cy + ang.sin() * 5.5 - lift * (18.0 + audio_e * 26.0);
                let ma = ((1.0 - lift) * (50.0 + audio_e * 135.0)).min(220.0) as u8;
                if ma > 6 {
                    let rad = 1.3 + (1.0 - lift) * 1.1 * audio_e;
                    fill_circle(&mut self.canvas, mx, my, rad, (rc, gc, bc, ma));
                }
            }
        }

        // 1b. Landing shockwave ripple
        if !vis.reduced_motion && phys.impact_ripple > 0.01 {
            let rip = phys.impact_ripple as f32;
            let prog = 1.0 - rip;
            let rcx = cw * 0.5;
            let rcy = base_char_y + target_h - 10.0;
            let a1 = (205.0 * rip) as u8;
            stroke_ellipse(
                &mut self.canvas,
                rcx,
                rcy,
                30.0 + prog * 66.0,
                7.0 + prog * 13.0,
                (105, 213, 232, a1),
                2.0,
            );
            let a2 = (125.0 * rip) as u8;
            stroke_ellipse(
                &mut self.canvas,
                rcx,
                rcy,
                42.0 + prog * 82.0,
                10.0 + prog * 17.0,
                (242, 119, 154, a2),
                1.3,
            );
            for s_idx in 0..6 {
                let ang = (PI / 3.0) * (s_idx as f32) + 0.35;
                let sx = rcx + ang.cos() * (24.0 + prog * 72.0);
                let sy = rcy + ang.sin() * (5.0 + prog * 11.0) - prog * 8.0;
                let col = if s_idx % 2 == 0 {
                    (105, 213, 232, a1)
                } else {
                    (242, 119, 154, a1)
                };
                fill_circle(&mut self.canvas, sx, sy, 2.0, col);
            }
        }

        // 2. Select primary sprite slot & frame index
        let selection = select_primary_slot(vis, phys, sprites);

        // 2a. Fast-fall afterimages
        if !vis.reduced_motion && phys.state == PhysicsState::Falling {
            if let Some((slot, frame_idx, _)) = selection {
                let speed = phys.vx.hypot(phys.vy);
                if speed >= 480.0 {
                    let nx = (-phys.vx / speed) as f32;
                    let ny = (-phys.vy / speed) as f32;
                    let frames = sprites.get_slot(slot);
                    if let Some(pm) = frames.get(frame_idx) {
                        for (dist_px, alpha_val) in [(22.0f32, 0.11f32), (11.0f32, 0.24f32)] {
                            let ox = (nx * dist_px).clamp(-36.0, 36.0);
                            let oy = (ny * dist_px).clamp(-28.0, 28.0);
                            let dx = (cw - pm.width() as f32) * 0.5 + ox;
                            let dy = char_y + oy;
                            let paint = PixmapPaint {
                                opacity: alpha_val * master_opacity,
                                blend_mode: BlendMode::SourceOver,
                                quality: FilterQuality::Bilinear,
                            };
                            self.canvas.draw_pixmap(
                                0,
                                0,
                                pm.as_ref(),
                                &paint,
                                Transform::from_translate(dx, dy),
                                None,
                            );
                        }
                    }
                }
            }
        }

        // 2b. Primary sprite composite with physics transform
        let flip_walk_x = vis.pose == "walk" && phys.facing < 0;
        let pivot_x = cw * 0.5;
        let pivot_y = if phys.state == PhysicsState::Landing {
            char_y + target_h - 8.0
        } else {
            char_y + target_h * 0.5
        };
        let total_tilt = if vis.reduced_motion || phys.state == PhysicsState::Grabbed {
            0.0
        } else {
            phys.tilt_deg as f32
        };
        let sx = (if vis.reduced_motion {
            1.0
        } else {
            phys.scale_x as f32
        }) * (if flip_walk_x { -1.0 } else { 1.0 });
        let sy = if vis.reduced_motion {
            1.0
        } else {
            phys.scale_y as f32
        };
        let is_identity_transform = !(total_tilt.abs() > 0.05
            || (sx - 1.0).abs() > 0.002
            || (sy - 1.0).abs() > 0.002);
        let body_transform = if !is_identity_transform {
            Transform::from_translate(pivot_x, pivot_y)
                .pre_rotate(total_tilt)
                .pre_scale(sx, sy)
                .pre_translate(-pivot_x, -pivot_y)
        } else {
            Transform::identity()
        };

        let sprite_paint = PixmapPaint {
            opacity: master_opacity,
            blend_mode: BlendMode::SourceOver,
            quality: if is_identity_transform {
                FilterQuality::Nearest
            } else {
                FilterQuality::Bilinear
            },
        };

        if let Some((slot, frame_idx, is_idle_comp)) = selection {
            if let Some(pm) = sprites.get_slot(slot).get(frame_idx) {
                let draw_x = ((cw - pm.width() as f32) * 0.5).round();
                let draw_y = char_y.round();
                if is_identity_transform {
                    self.canvas.draw_pixmap(
                        draw_x as i32,
                        draw_y as i32,
                        pm.as_ref(),
                        &sprite_paint,
                        Transform::identity(),
                        None,
                    );
                } else {
                    let tr = body_transform.pre_translate(draw_x, char_y);
                    self.canvas
                        .draw_pixmap(0, 0, pm.as_ref(), &sprite_paint, tr, None);
                }
            }

            if is_idle_comp {
                if vis.blink_active {
                    let b_slot = if vis.blink_is_glitch {
                        "blink_glitch"
                    } else {
                        "blink"
                    };
                    let b_frames = sprites.get_slot(b_slot);
                    if !b_frames.is_empty() {
                        let b_idx = vis.blink_frame.min(b_frames.len() - 1);
                        let pm = &b_frames[b_idx];
                        let draw_x = ((cw - pm.width() as f32) * 0.5).round();
                        if is_identity_transform {
                            self.canvas.draw_pixmap(
                                draw_x as i32,
                                char_y.round() as i32,
                                pm.as_ref(),
                                &sprite_paint,
                                Transform::identity(),
                                None,
                            );
                        } else {
                            let tr = body_transform.pre_translate(draw_x, char_y);
                            self.canvas
                                .draw_pixmap(0, 0, pm.as_ref(), &sprite_paint, tr, None);
                        }
                    }
                }
                if vis.state == "speaking" {
                    let m_frames = sprites.get_slot("mouth");
                    if !m_frames.is_empty() {
                        let m_idx = vis.current_mouth_idx.min(m_frames.len() - 1);
                        let pm = &m_frames[m_idx];
                        let draw_x = ((cw - pm.width() as f32) * 0.5).round();
                        if is_identity_transform {
                            self.canvas.draw_pixmap(
                                draw_x as i32,
                                char_y.round() as i32,
                                pm.as_ref(),
                                &sprite_paint,
                                Transform::identity(),
                                None,
                            );
                        } else {
                            let tr = body_transform.pre_translate(draw_x, char_y);
                            self.canvas
                                .draw_pixmap(0, 0, pm.as_ref(), &sprite_paint, tr, None);
                        }
                    }
                }
            }

            // 2c. Chromatic glitch scanline accents on wall_splat / glitch_active
            if !vis.reduced_motion && (phys.state == PhysicsState::WallSplat || vis.glitch_active) {
                let phase = vis.tick_count % 7;
                let bands = [
                    (0.24f32, if phase.is_multiple_of(2) { 6.0f32 } else { -6.0 }, (242, 119, 154, 125)),
                    (0.54f32, if phase.is_multiple_of(2) { -5.0f32 } else { 5.0 }, (105, 213, 232, 120)),
                    (0.76f32, if phase.is_multiple_of(3) { 4.0f32 } else { -4.0 }, (242, 119, 154, 105)),
                ];
                for (yf, shift_x, col) in bands {
                    let y = char_y + target_h * yf;
                    fill_rect(&mut self.canvas, char_x + shift_x, y, target_w, 2.5, col);
                }
            }
        }

        // 3. Listening mic pulse badge
        if vis.state == "listening" {
            let mic_x = char_x + target_w - 18.0;
            let mic_y = char_y + 45.0;
            let pulse = (vis.hover_phase * 2.0).sin().abs() * 2.0;
            fill_circle(&mut self.canvas, mic_x, mic_y, 10.0 + pulse, (91, 205, 162, 230));
            stroke_ellipse(&mut self.canvas, mic_x, mic_y, 10.0 + pulse, 10.0 + pulse, (241, 244, 248, 240), 1.5);
            fill_rect(&mut self.canvas, mic_x - 3.0, mic_y - 5.0, 6.0, 7.0, (9, 12, 18, 255));
            fill_rect(&mut self.canvas, mic_x - 1.0, mic_y + 2.0, 2.0, 4.0, (9, 12, 18, 255));
        }

        // 4. LIVING SIGNAL rounded speech bubble with Cyrillic text (cached per text + anchor_y)
        let caption = vis.caption_text.trim();
        if !caption.is_empty() && !vis.power_saver_active {
            let anchor_y_bucket = (base_char_y.round()) as i32;
            let needs_rebuild = match &self.cached_bubble_key {
                Some((prev_txt, prev_y)) => prev_txt != caption || *prev_y != anchor_y_bucket,
                None => true,
            };
            if needs_rebuild {
                let mut bubble_pm = Pixmap::new(CANVAS_W, 120).expect("allocate bubble cache");
                bubble_pm.fill(Color::TRANSPARENT);
                draw_speech_bubble(
                    &mut bubble_pm,
                    self.font.as_ref(),
                    caption,
                    char_x + target_w * 0.5,
                    anchor_y_bucket as f32,
                );
                self.cached_bubble_pixmap = Some(bubble_pm);
                self.cached_bubble_key = Some((caption.to_string(), anchor_y_bucket));
            }
            if let Some(ref bpm) = self.cached_bubble_pixmap {
                let bubble_paint = PixmapPaint {
                    opacity: 1.0,
                    blend_mode: BlendMode::SourceOver,
                    quality: FilterQuality::Nearest,
                };
                let bubble_dy = if phys.y < 0.0 {
                    (-phys.y as f32 + 4.0).min(96.0)
                } else {
                    0.0
                };
                let bubble_dx = if phys.x < 0.0 {
                    (-phys.x as f32).min(48.0)
                } else {
                    0.0
                };
                self.canvas.draw_pixmap(
                    0,
                    0,
                    bpm.as_ref(),
                    &bubble_paint,
                    Transform::from_translate(bubble_dx, bubble_dy),
                    None,
                );
            }
        } else {
            self.cached_bubble_key = None;
            self.cached_bubble_pixmap = None;
        }

        &self.canvas
    }
}

fn select_primary_slot(
    vis: &AvatarVisualState,
    phys: &AvatarPhysics2D,
    sprites: &mut SpriteStore,
) -> Option<(&'static str, usize, bool)> {
    let tc = vis.tick_count;
    if phys.state == PhysicsState::WallSplat {
        let len = sprites.get_slot("splat").len();
        if len > 0 {
            let div = sprites.slot_tick_div("splat", 4);
            return Some(("splat", ((tc / div) as usize) % len, false));
        }
        let glen = sprites.get_slot("glitch").len();
        if glen > 0 {
            return Some(("glitch", ((tc / 2) as usize) % glen, false));
        }
    }
    if phys.state == PhysicsState::Falling {
        for (slot, def_div) in [("fall", 3), ("drag", 3), ("dance", 3)] {
            let len = sprites.get_slot(slot).len();
            if len > 0 {
                let div = sprites.slot_tick_div(slot, def_div);
                return Some((slot, ((tc / div) as usize) % len, false));
            }
        }
    }
    if phys.state == PhysicsState::Grabbed {
        for (slot, def_div) in [("drag", 3), ("dance", 3)] {
            let len = sprites.get_slot(slot).len();
            if len > 0 {
                let div = sprites.slot_tick_div(slot, def_div);
                return Some((slot, ((tc / div) as usize) % len, false));
            }
        }
    }
    if vis.glitch_active || vis.state == "thinking" {
        let len = sprites.get_slot("glitch").len();
        if len > 0 {
            return Some(("glitch", vis.glitch_frame % len, false));
        }
    }
    if vis.waking_active {
        let len = sprites.get_slot("wake").len();
        if len > 0 {
            return Some(("wake", vis.wake_frame.min(len - 1), false));
        }
    }
    if vis.dance_active {
        let len = sprites.get_slot("dance").len();
        if len > 0 {
            return Some(("dance", ((tc / 3) as usize) % len, false));
        }
    }
    match vis.pose.as_str() {
        "sleep" => {
            let len = sprites.get_slot("sleep").len();
            if len > 0 {
                return Some(("sleep", ((tc / 8) as usize) % len, false));
            }
        }
        "lounge" => {
            let len = sprites.get_slot("lounge").len();
            if len > 0 {
                let div = sprites.slot_tick_div("lounge", 6);
                return Some(("lounge", ((tc / div) as usize) % len, false));
            }
            let plen = sprites.get_slot("perch").len();
            if plen > 0 {
                return Some(("perch", ((tc / 6) as usize) % plen, false));
            }
        }
        "walk" => {
            let len = sprites.get_slot("walk").len();
            if len > 0 {
                let div = sprites.slot_tick_div("walk", 4);
                return Some(("walk", ((tc / div) as usize) % len, false));
            }
        }
        "perch_music" => {
            let len = sprites.get_slot("perch_music").len();
            if len > 0 {
                return Some(("perch_music", ((tc / 4) as usize) % len, false));
            }
        }
        "perch" => {
            let len = sprites.get_slot("perch").len();
            if len > 0 {
                return Some(("perch", ((tc / 5) as usize) % len, false));
            }
        }
        _ => {}
    }

    let ilen = sprites.get_slot("idle").len();
    if ilen > 0 {
        return Some(("idle", ((tc / 7) as usize) % ilen, true));
    }
    None
}

fn load_system_ui_font() -> Option<Font> {
    let candidates = [
        "/usr/share/fonts/TTF/Inter-Medium.ttf",
        "/usr/share/fonts/inter/Inter-Medium.ttf",
        "/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/TTF/JetBrainsMonoNLNerdFont-Regular.ttf",
        "/usr/share/fonts/gnu-free/FreeSans.otf",
    ];
    for p in candidates {
        if Path::new(p).is_file() {
            if let Ok(bytes) = std::fs::read(p) {
                if let Ok(font) = Font::from_bytes(bytes, FontSettings::default()) {
                    return Some(font);
                }
            }
        }
    }
    None
}

fn draw_radial_aura(
    canvas: &mut Pixmap,
    center: (f32, f32),
    radii: (f32, f32),
    rgba: (u8, u8, u8, u8),
) {
    let (cx, cy) = center;
    let (rx, ry) = radii;
    let (r, g, b, a) = rgba;
    let Some(shader) = RadialGradient::new(
        Point::from_xy(0.0, 0.0),
        Point::from_xy(0.0, 0.0),
        rx.max(1.0),
        vec![
            GradientStop::new(0.0, Color::from_rgba8(r, g, b, a)),
            GradientStop::new(0.55, Color::from_rgba8(r, g, b, ((a as f32) * 0.38) as u8)),
            GradientStop::new(1.0, Color::from_rgba8(r, g, b, 0)),
        ],
        SpreadMode::Pad,
        Transform::identity(),
    ) else {
        return;
    };
    let Some(path) = PathBuilder::from_circle(0.0, 0.0, rx.max(1.0)) else {
        return;
    };
    let paint = Paint {
        shader,
        anti_alias: true,
        ..Paint::default()
    };
    let tr = Transform::from_translate(cx, cy).pre_scale(1.0, ry / rx.max(1.0));
    canvas.fill_path(&path, &paint, tiny_skia::FillRule::Winding, tr, None);
}

fn stroke_ellipse(
    canvas: &mut Pixmap,
    cx: f32,
    cy: f32,
    rx: f32,
    ry: f32,
    rgba: (u8, u8, u8, u8),
    width: f32,
) {
    let Some(rect) = Rect::from_xywh(cx - rx, cy - ry, rx * 2.0, ry * 2.0) else {
        return;
    };
    let Some(path) = PathBuilder::from_oval(rect) else {
        return;
    };
    let mut paint = Paint::default();
    paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
    paint.anti_alias = true;
    let stroke = Stroke {
        width,
        ..Stroke::default()
    };
    canvas.stroke_path(&path, &paint, &stroke, Transform::identity(), None);
}

fn stroke_ellipse_arc(
    canvas: &mut Pixmap,
    center: (f32, f32),
    radii: (f32, f32),
    angles: (f32, f32),
    rgba: (u8, u8, u8, u8),
    width: f32,
) {
    let (cx, cy) = center;
    let (rx, ry) = radii;
    let (start_rad, sweep_rad) = angles;
    let steps = 48usize;
    let mut pb = PathBuilder::new();
    for i in 0..=steps {
        let t = (i as f32) / (steps as f32);
        let ang = start_rad + sweep_rad * t;
        let x = cx + ang.cos() * rx;
        let y = cy + ang.sin() * ry;
        if i == 0 {
            pb.move_to(x, y);
        } else {
            pb.line_to(x, y);
        }
    }
    let Some(path) = pb.finish() else { return };
    let mut paint = Paint::default();
    paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
    paint.anti_alias = true;
    let stroke = Stroke {
        width,
        line_cap: LineCap::Round,
        ..Stroke::default()
    };
    canvas.stroke_path(&path, &paint, &stroke, Transform::identity(), None);
}

fn fill_circle(canvas: &mut Pixmap, cx: f32, cy: f32, r: f32, rgba: (u8, u8, u8, u8)) {
    let Some(path) = PathBuilder::from_circle(cx, cy, r) else {
        return;
    };
    let mut paint = Paint::default();
    paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
    paint.anti_alias = true;
    canvas.fill_path(&path, &paint, tiny_skia::FillRule::Winding, Transform::identity(), None);
}

fn fill_rect(canvas: &mut Pixmap, x: f32, y: f32, w: f32, h: f32, rgba: (u8, u8, u8, u8)) {
    let Some(rect) = Rect::from_xywh(x, y, w, h) else {
        return;
    };
    let mut paint = Paint::default();
    paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
    paint.anti_alias = true;
    canvas.fill_rect(rect, &paint, Transform::identity(), None);
}

fn rounded_rect_path(x: f32, y: f32, w: f32, h: f32, r: f32) -> Option<tiny_skia::Path> {
    let r = r.min(w * 0.5).min(h * 0.5);
    let mut pb = PathBuilder::new();
    pb.move_to(x + r, y);
    pb.line_to(x + w - r, y);
    pb.quad_to(x + w, y, x + w, y + r);
    pb.line_to(x + w, y + h - r);
    pb.quad_to(x + w, y + h, x + w - r, y + h);
    pb.line_to(x + r, y + h);
    pb.quad_to(x, y + h, x, y + h - r);
    pb.line_to(x, y + r);
    pb.quad_to(x, y, x + r, y);
    pb.close();
    pb.finish()
}

fn draw_speech_bubble(
    canvas: &mut Pixmap,
    font: Option<&Font>,
    text: &str,
    anchor_x: f32,
    anchor_y: f32,
) {
    let cw = canvas.width() as f32;
    let font_px = 12.5f32;
    let max_bubble_w = (cw - 16.0).max(140.0);
    let max_text_w = (max_bubble_w - 28.0).max(100.0);

    let lines = wrap_text(font, text, font_px, max_text_w, 4);
    let line_h = 15.5f32;
    let widest = lines
        .iter()
        .map(|l| measure_line_width(font, l, font_px))
        .fold(0.0f32, f32::max);

    let bubble_w = (widest + 30.0).clamp(144.0, max_bubble_w);
    let max_bubble_h = (anchor_y + 10.0).clamp(46.0, 84.0);
    let bubble_h = ((lines.len() as f32) * line_h + 16.0).clamp(34.0, max_bubble_h);
    let bubble_x = (cw - bubble_w) * 0.5;
    let sky_bottom = (anchor_y + 6.0).max(bubble_h + 4.0);
    let bubble_y = ((sky_bottom - bubble_h) * 0.45).clamp(4.0, 14.0);

    // Bubble fill + border
    if let Some(rr) = rounded_rect_path(bubble_x, bubble_y, bubble_w, bubble_h, 9.0) {
        let mut fill = Paint::default();
        fill.set_color_rgba8(13, 19, 30, 242);
        fill.anti_alias = true;
        canvas.fill_path(&rr, &fill, tiny_skia::FillRule::Winding, Transform::identity(), None);

        let mut border = Paint::default();
        border.set_color_rgba8(105, 213, 232, 210);
        border.anti_alias = true;
        let stroke = Stroke {
            width: 1.2,
            ..Stroke::default()
        };
        canvas.stroke_path(&rr, &border, &stroke, Transform::identity(), None);
    }

    // Bottom pointer triangle
    let tip_y = (anchor_y + 10.0).max(bubble_y + bubble_h + 5.0).min(bubble_y + bubble_h + 10.0);
    let mut tri = PathBuilder::new();
    tri.move_to(anchor_x - 6.0, bubble_y + bubble_h - 0.5);
    tri.line_to(anchor_x + 6.0, bubble_y + bubble_h - 0.5);
    tri.line_to(anchor_x, tip_y);
    tri.close();
    if let Some(tpath) = tri.finish() {
        let mut fill = Paint::default();
        fill.set_color_rgba8(13, 19, 30, 242);
        fill.anti_alias = true;
        canvas.fill_path(&tpath, &fill, tiny_skia::FillRule::Winding, Transform::identity(), None);
    }

    // Left crimson signal bar
    fill_rect(
        canvas,
        bubble_x + 3.5,
        bubble_y + 7.0,
        2.5,
        (bubble_h - 14.0).max(4.0),
        (242, 119, 154, 220),
    );

    // Rasterize crisp UTF-8 / Cyrillic text centered inside bubble
    if let Some(f) = font {
        let total_text_h = (lines.len() as f32) * line_h;
        let mut y_cursor = bubble_y + (bubble_h - total_text_h) * 0.5 + font_px * 0.85;
        for line in &lines {
            let lw = measure_line_width(Some(f), line, font_px);
            let x_start = bubble_x + 8.0 + ((bubble_w - 14.0) - lw).max(0.0) * 0.5;
            draw_text_line(canvas, f, line, font_px, x_start, y_cursor, (241, 244, 248));
            y_cursor += line_h;
        }
    }
}

fn measure_line_width(font: Option<&Font>, text: &str, px: f32) -> f32 {
    let Some(f) = font else {
        return (text.chars().count() as f32) * (px * 0.58);
    };
    text.chars().map(|ch| f.metrics(ch, px).advance_width).sum()
}

fn wrap_text(font: Option<&Font>, text: &str, px: f32, max_w: f32, max_lines: usize) -> Vec<String> {
    let mut lines = Vec::new();
    let mut current = String::new();
    for word in text.split_whitespace() {
        let candidate = if current.is_empty() {
            word.to_string()
        } else {
            format!("{current} {word}")
        };
        if !current.is_empty() && measure_line_width(font, &candidate, px) > max_w {
            lines.push(current);
            if lines.len() >= max_lines {
                return lines;
            }
            current = word.to_string();
        } else {
            current = candidate;
        }
    }
    if !current.is_empty() && lines.len() < max_lines {
        lines.push(current);
    }
    if lines.is_empty() {
        lines.push(text.to_string());
    }
    lines
}

fn draw_text_line(
    canvas: &mut Pixmap,
    font: &Font,
    line: &str,
    px: f32,
    start_x: f32,
    baseline_y: f32,
    rgb: (u8, u8, u8),
) {
    let cw = canvas.width() as i32;
    let ch = canvas.height() as i32;
    let pixels = canvas.pixels_mut();
    let mut pen_x = start_x;

    for ch_val in line.chars() {
        let (metrics, bitmap) = font.rasterize(ch_val, px);
        let gx = (pen_x + metrics.xmin as f32).round() as i32;
        let gy = (baseline_y - metrics.height as f32 - metrics.ymin as f32).round() as i32;

        for row in 0..metrics.height {
            let py = gy + row as i32;
            if py < 0 || py >= ch {
                continue;
            }
            let row_off = (py as usize) * (cw as usize);
            let bm_off = row * metrics.width;
            for col in 0..metrics.width {
                let px_x = gx + col as i32;
                if px_x < 0 || px_x >= cw {
                    continue;
                }
                let cov = bitmap[bm_off + col];
                if cov == 0 {
                    continue;
                }
                let idx = row_off + px_x as usize;
                let dst = pixels[idx];
                pixels[idx] = blend_premul_over(dst, rgb.0, rgb.1, rgb.2, cov);
            }
        }
        pen_x += metrics.advance_width;
    }
}

#[inline]
fn blend_premul_over(dst: PremultipliedColorU8, r: u8, g: u8, b: u8, a: u8) -> PremultipliedColorU8 {
    let sa = a as u16;
    let inv_sa = 255u16 - sa;
    let sr = (r as u16 * sa + 127) / 255;
    let sg = (g as u16 * sa + 127) / 255;
    let sb = (b as u16 * sa + 127) / 255;
    let out_r = (sr + (dst.red() as u16 * inv_sa + 127) / 255).min(255) as u8;
    let out_g = (sg + (dst.green() as u16 * inv_sa + 127) / 255).min(255) as u8;
    let out_b = (sb + (dst.blue() as u16 * inv_sa + 127) / 255).min(255) as u8;
    let out_a = (sa + (dst.alpha() as u16 * inv_sa + 127) / 255).min(255) as u8;
    PremultipliedColorU8::from_rgba(out_r, out_g, out_b, out_a).unwrap_or(dst)
}

/// Blit premultiplied RGBA8888 canvas into a Wayland `wl_shm` ARGB8888 (little-endian BGRA) buffer
/// at `(offset_x, offset_y)`, clearing `prev_rect` first so only dirty rects are touched.
pub fn blit_canvas_to_wayland_argb8888(
    buf: &mut [u8],
    buf_w: u32,
    buf_h: u32,
    canvas: &Pixmap,
    offset_x: i32,
    offset_y: i32,
    prev_rect: Option<(i32, i32, i32, i32)>,
) -> (i32, i32, i32, i32) {
    let bw = buf_w as i32;
    let bh = buf_h as i32;
    let stride = (buf_w as usize) * 4;

    if let Some((px, py, pw, ph)) = prev_rect {
        let x0 = px.clamp(0, bw) as usize;
        let x1 = (px + pw).clamp(0, bw) as usize;
        let y0 = py.clamp(0, bh) as usize;
        let y1 = (py + ph).clamp(0, bh) as usize;
        if x1 > x0 {
            for y in y0..y1 {
                let row = y * stride;
                buf[row + x0 * 4..row + x1 * 4].fill(0);
            }
        }
    }

    let cw = canvas.width() as i32;
    let ch = canvas.height() as i32;
    let src_pixels = canvas.pixels();

    let dst_x0 = offset_x.clamp(0, bw);
    let dst_y0 = offset_y.clamp(0, bh);
    let dst_x1 = (offset_x + cw).clamp(0, bw);
    let dst_y1 = (offset_y + ch).clamp(0, bh);

    if dst_x1 > dst_x0 && dst_y1 > dst_y0 {
        let copy_w = (dst_x1 - dst_x0) as usize;
        let src_x0 = (dst_x0 - offset_x) as usize;
        let dst_x0_bytes = (dst_x0 as usize) * 4;
        for dy in dst_y0..dst_y1 {
            let sy = (dy - offset_y) as usize;
            let dst_row = (dy as usize) * stride + dst_x0_bytes;
            let src_row = sy * (cw as usize) + src_x0;
            let dst_slice = &mut buf[dst_row..dst_row + copy_w * 4];
            let src_slice = &src_pixels[src_row..src_row + copy_w];
            let (dst_chunks, _) = dst_slice.as_chunks_mut::<4>();
            for (d, p) in dst_chunks.iter_mut().zip(src_slice.iter()) {
                let a = p.alpha();
                if a == 0 {
                    *d = [0, 0, 0, 0];
                } else {
                    // Little-endian Wayland WL_SHM_FORMAT_ARGB8888: [B, G, R, A]
                    *d = [p.blue(), p.green(), p.red(), a];
                }
            }
        }
    }

    (
        dst_x0,
        dst_y0,
        (dst_x1 - dst_x0).max(0),
        (dst_y1 - dst_y0).max(0),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn renders_idle_and_cyrillic_speech_bubble_without_panicking() {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("capri")
            .join("assets")
            .join("sprites_v2");
        if !dir.is_dir() {
            return;
        }
        let mut store = SpriteStore::new(dir, Some(PathBuf::from("/nonexistent-capri-sprites")));
        let vis = AvatarVisualState {
            caption_text: "Привет, D! Проверка кириллицы в бабле.".to_string(),
            ..AvatarVisualState::default()
        };
        let phys = AvatarPhysics2D::new(300.0, 420.0, 100.0, 100.0);
        let mut renderer = AvatarRenderer::new();
        let pm = renderer.render(&vis, &phys, &mut store);
        assert_eq!(pm.width(), CANVAS_W);
        assert_eq!(pm.height(), CANVAS_H);
        let non_empty = pm.pixels().iter().filter(|p| p.alpha() > 0).count();
        assert!(non_empty > 5000, "rendered sprite + bubble should have opaque pixels");
    }

    #[test]
    fn blit_clears_previous_rect_with_zero_ghost_pixels_left_behind() {
        let mut canvas = Pixmap::new(16, 16).unwrap();
        canvas.fill(Color::from_rgba8(105, 213, 232, 255));
        let w = 100u32;
        let h = 100u32;
        let mut fb = vec![0u8; (w * h * 4) as usize];

        let r1 = blit_canvas_to_wayland_argb8888(&mut fb, w, h, &canvas, 10, 10, None);
        assert_eq!(r1, (10, 10, 16, 16));
        let r2 = blit_canvas_to_wayland_argb8888(&mut fb, w, h, &canvas, 60, 50, Some(r1));
        assert_eq!(r2, (60, 50, 16, 16));

        // Every single pixel inside old rect (10..26, 10..26) must now be 0x00000000
        for y in 10..26 {
            for x in 10..26 {
                let off = ((y * w + x) * 4) as usize;
                assert_eq!(&fb[off..off + 4], &[0, 0, 0, 0], "ghost pixel at ({x},{y})");
            }
        }
        // New rect must contain BGRA = [232, 213, 105, 255]
        let off = ((50 * w + 60) * 4) as usize;
        assert_eq!(&fb[off..off + 4], &[232, 213, 105, 255]);
    }

    #[test]
    fn presence_snapshot_switches_perch_music_caption_and_game_mode() {
        use crate::presence::{GameModeState, MediaPlaybackState, PresenceSnapshot};

        let mut store = SpriteStore::new(
            PathBuf::from("/nonexistent-bundled"),
            Some(PathBuf::from("/nonexistent-user")),
        );
        let mut vis = AvatarVisualState::default();
        let mut phys = AvatarPhysics2D::new(300.0, 420.0, 100.0, 100.0);

        // 1. Music starts playing -> auto-switches to perch_music + track bubble
        let playing = PresenceSnapshot {
            media: MediaPlaybackState {
                is_playing: true,
                player: "spotify".to_string(),
                artist: "sekairotten".to_string(),
                title: "46с".to_string(),
                track_label: "sekairotten — 46с".to_string(),
            },
            game_mode: GameModeState::default(),
        };
        assert!(vis.apply_presence_snapshot(playing.clone(), &mut store, &mut phys));
        assert_eq!(vis.pose, "perch_music");
        assert!(vis.caption_text.contains("sekairotten — 46с"));

        // 2. Same track continues -> does not spam caption again
        vis.caption_text.clear();
        assert!(!vis.apply_presence_snapshot(playing, &mut store, &mut phys));
        assert!(vis.caption_text.is_empty());

        // 3. Fullscreen game starts -> enters Game Mode sleep; exits -> resumes perch_music
        let in_game = PresenceSnapshot {
            media: MediaPlaybackState {
                is_playing: true,
                track_label: "sekairotten — 46с".to_string(),
                ..MediaPlaybackState::default()
            },
            game_mode: GameModeState {
                active: true,
                reason: "fullscreen_game",
            },
        };
        assert!(vis.apply_presence_snapshot(in_game, &mut store, &mut phys));
        assert!(vis.power_saver_active);
        assert_eq!(vis.pose, "sleep");

        let back_to_desktop = PresenceSnapshot {
            media: MediaPlaybackState {
                is_playing: true,
                track_label: "sekairotten — 46с".to_string(),
                ..MediaPlaybackState::default()
            },
            game_mode: GameModeState::default(),
        };
        assert!(vis.apply_presence_snapshot(back_to_desktop, &mut store, &mut phys));
        assert!(!vis.power_saver_active);
        assert_eq!(vis.pose, "perch_music");

        // 4. Music pauses -> returns to normal pose
        let paused = PresenceSnapshot::default();
        assert!(vis.apply_presence_snapshot(paused, &mut store, &mut phys));
        assert_eq!(vis.pose, "normal");
    }
}
