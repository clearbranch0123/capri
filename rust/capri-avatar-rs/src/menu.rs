//! Native `tiny-skia` + `fontdue` Crimson Graphite Quick-Control HUD (`CapriAvatarQuickPopover` parity).
//!
//! Renders a crisp, tactile Crimson Graphite card directly on the `zwlr_layer_shell_v1` stage
//! beside Capri (zero Wayland `xdg_toplevel` positioning/focus/pinning conflicts), with full
//! visual & functional parity with Python's `CapriAvatarQuickPopover`:
//! - Header: crimson status dot + `CAPRI` + live status pill + `Релик` button + `×` close
//! - `ПОЗЫ`: 1-click toggle chips (`Сидеть` / `Танец` / `Сон`)
//! - `ПОВЕДЕНИЕ`: 6 switch rows (`Физика и гравитация`, `Свободная жизнь`, `Следовать за окном`,
//!   `Авто-позы (музыка / сон)`, `Фокус-таймер (Pomodoro)`, `Game Mode`) + optional monitor row
//! - Action footer: full-width `Быстрый вопрос` CTA, 2×2 grid (`Голос` / `Область`, `На базу` / `Закрепить тут`),
//!   and `Выключить аватар`.

use fontdue::Font;
use tiny_skia::{Color, FillRule, LineCap, Paint, PathBuilder, Pixmap, Rect, Stroke, Transform};

// Exact Crimson Graphite Design Tokens (matching `capri/ui/theme.py` & `avatar_popover.py`)
const BG_SURFACE: (u8, u8, u8, u8) = (20, 23, 32, 246);          // #141720
const BG_SURFACE_ALT: (u8, u8, u8, u8) = (24, 28, 39, 246);      // #181C27
const BG_RAISED: (u8, u8, u8, u8) = (32, 37, 52, 246);           // #202534
const BG_HOVER: (u8, u8, u8, u8) = (40, 46, 65, 250);
const BORDER_CARD: (u8, u8, u8, u8) = (56, 64, 86, 235);         // border_interactive
const BORDER_SUBTLE: (u8, u8, u8, u8) = (39, 45, 62, 215);       // border_subtle
const CRIMSON: (u8, u8, u8, u8) = (225, 45, 83, 255);            // #E12D53
const CRIMSON_SOFT: (u8, u8, u8, u8) = (225, 45, 83, 46);
const CRIMSON_SOFT_HOV: (u8, u8, u8, u8) = (225, 45, 83, 76);
const CRIMSON_BORDER: (u8, u8, u8, u8) = (225, 45, 83, 150);
const CORAL_TEXT: (u8, u8, u8, u8) = (255, 118, 146, 255);
const CYAN: (u8, u8, u8, u8) = (78, 216, 232, 255);              // #4ED8E8
const CYAN_SOFT: (u8, u8, u8, u8) = (78, 216, 232, 34);
const CYAN_SOFT_HOV: (u8, u8, u8, u8) = (78, 216, 232, 58);
const CYAN_BORDER: (u8, u8, u8, u8) = (78, 216, 232, 128);
const LILAC: (u8, u8, u8, u8) = (181, 155, 255, 255);
const LILAC_SOFT: (u8, u8, u8, u8) = (181, 155, 255, 42);
const LILAC_BORDER: (u8, u8, u8, u8) = (181, 155, 255, 115);
const TEXT_PRIMARY: (u8, u8, u8) = (241, 244, 248);              // #F1F4F8
const TEXT_SECONDARY: (u8, u8, u8) = (178, 187, 204);
const TEXT_MUTED: (u8, u8, u8) = (128, 138, 158);
const TEXT_DARK: (u8, u8, u8) = (11, 14, 20);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MenuAction {
    Close,
    OpenRelic,
    TogglePose(String), // "perch" | "dance" | "sleep"
    TogglePhysics,
    ToggleAutonomous,
    ToggleFollowWorkspaces,
    ToggleAutoPoses,
    CycleFocusTimer,
    CycleGameMode,
    CycleMonitor,
    QuickAsk,
    ToggleVoice,
    LookRegion,
    ReturnHome,
    TogglePin,
    Quit,
}

impl MenuAction {
    /// Toggles keep the HUD open and refresh in place; navigation/dismiss actions close it.
    pub fn keeps_menu_open(&self) -> bool {
        matches!(
            self,
            MenuAction::TogglePose(_)
                | MenuAction::TogglePhysics
                | MenuAction::ToggleAutonomous
                | MenuAction::ToggleFollowWorkspaces
                | MenuAction::ToggleAutoPoses
                | MenuAction::CycleFocusTimer
                | MenuAction::CycleGameMode
                | MenuAction::CycleMonitor
                | MenuAction::TogglePin
        )
    }
}

#[derive(Debug, Clone)]
pub struct MenuSnapshot<'a> {
    pub avatar_state: &'a str,
    pub active_pose: &'a str,
    pub dance_active: bool,
    pub physics_enabled: bool,
    pub autonomous_enabled: bool,
    pub follow_workspaces: bool,
    pub auto_poses_enabled: bool,
    pub focus_timer_active: bool,
    pub focus_timer_label: String,
    pub game_mode_override: Option<bool>,
    pub power_saver_active: bool,
    pub pinned: bool,
    pub perched: bool,
    pub outputs: &'a [String],
    pub active_output: &'a str,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PillTone {
    Off,
    Cyan,
    Crimson,
    Lilac,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ItemStyle {
    RelicHeaderBtn,
    CloseHeaderBtn,
    PoseChip,
    SwitchRow,
    AskCta,
    VoiceBtn,
    LookBtn,
    FooterBtn,
    QuitLink,
}

#[derive(Debug, Clone)]
struct MenuItem {
    label: String,
    pill_text: String,
    pill_tone: PillTone,
    action: MenuAction,
    active: bool,
    style: ItemStyle,
    x: f32,
    y: f32,
    w: f32,
    h: f32,
}

#[derive(Debug, Clone)]
struct SectionHeading {
    text: &'static str,
    y: f32,
}

pub struct ContextMenu {
    pub visible: bool,
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    status_badge: String,
    items: Vec<MenuItem>,
    sections: Vec<SectionHeading>,
    separators: Vec<f32>,
    hovered_idx: Option<usize>,
    pub dragging: bool,
    drag_offset: (f64, f64),
    cached: Option<Pixmap>,
    dirty: bool,
    last_anchor: (f64, f64, f64, f64, u32, u32),
}

impl Default for ContextMenu {
    fn default() -> Self {
        Self::new()
    }
}

impl ContextMenu {
    pub fn new() -> Self {
        Self {
            visible: false,
            x: 0,
            y: 0,
            width: 268,
            height: 440,
            status_badge: "LIVE".to_string(),
            items: Vec::new(),
            sections: Vec::new(),
            separators: Vec::new(),
            hovered_idx: None,
            dragging: false,
            drag_offset: (0.0, 0.0),
            cached: None,
            dirty: true,
            last_anchor: (0.0, 0.0, 300.0, 420.0, 1920, 1035),
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn open(
        &mut self,
        avatar_x: f64,
        avatar_y: f64,
        avatar_w: f64,
        avatar_h: f64,
        stage_w: u32,
        stage_h: u32,
        snap: &MenuSnapshot<'_>,
    ) {
        self.last_anchor = (avatar_x, avatar_y, avatar_w, avatar_h, stage_w, stage_h);
        self.rebuild_layout(snap);

        // Place cleanly beside Capri (right side if she is on left half, left side otherwise)
        let avatar_cx = avatar_x + avatar_w * 0.5;
        let preferred_x = if avatar_cx < (stage_w as f64) * 0.5 {
            avatar_x + avatar_w - 50.0
        } else {
            avatar_x - self.width as f64 + 50.0
        };
        let preferred_y = avatar_y + (avatar_h - self.height as f64) * 0.52;
        let max_x = (stage_w as i32 - self.width as i32 - 12).max(12);
        let max_y = (stage_h as i32 - self.height as i32 - 12).max(12);
        self.x = (preferred_x.round() as i32).clamp(12, max_x);
        self.y = (preferred_y.round() as i32).clamp(12, max_y);
        self.hovered_idx = None;
        self.dragging = false;
        self.visible = true;
        self.dirty = true;
    }

    /// Refresh pills & button labels in place without moving or closing an already-open HUD.
    pub fn refresh(&mut self, snap: &MenuSnapshot<'_>) {
        if !self.visible {
            return;
        }
        let (_, _, _, _, stage_w, stage_h) = self.last_anchor;
        self.rebuild_layout(snap);
        let max_x = (stage_w as i32 - self.width as i32 - 12).max(12);
        let max_y = (stage_h as i32 - self.height as i32 - 12).max(12);
        self.x = self.x.clamp(12, max_x);
        self.y = self.y.clamp(12, max_y);
        self.dirty = true;
    }

    fn rebuild_layout(&mut self, snap: &MenuSnapshot<'_>) {
        self.items.clear();
        self.sections.clear();
        self.separators.clear();

        let st_norm = snap.avatar_state.trim().to_ascii_lowercase();
        self.status_badge = if st_norm == "listening" {
            "MIC".to_string()
        } else if st_norm == "speaking" {
            "VOICE".to_string()
        } else if snap.power_saver_active {
            "GAME".to_string()
        } else if snap.focus_timer_active {
            format!("FOCUS {}", snap.focus_timer_label)
        } else if snap.pinned {
            "PINNED".to_string()
        } else if snap.perched {
            "PERCH".to_string()
        } else {
            "LIVE".to_string()
        };

        self.width = 268;
        let pad = 12.0f32;
        let inner_w = self.width as f32 - pad * 2.0;
        let gap = 6.0f32;

        // 1. Header buttons (`Релик` + `×`)
        let close_w = 22.0f32;
        let relic_w = 66.0f32;
        self.items.push(MenuItem {
            label: "Релик".to_string(),
            pill_text: String::new(),
            pill_tone: PillTone::Off,
            action: MenuAction::OpenRelic,
            active: false,
            style: ItemStyle::RelicHeaderBtn,
            x: self.width as f32 - pad - close_w - 5.0 - relic_w,
            y: 10.0,
            w: relic_w,
            h: 22.0,
        });
        self.items.push(MenuItem {
            label: String::new(),
            pill_text: String::new(),
            pill_tone: PillTone::Off,
            action: MenuAction::Close,
            active: false,
            style: ItemStyle::CloseHeaderBtn,
            x: self.width as f32 - pad - close_w,
            y: 10.0,
            w: close_w,
            h: 22.0,
        });

        // 2. Section `ПОЗЫ` — 3 equal chips (`Сидеть` / `Танец` / `Сон`)
        let mut cy = 44.0f32;
        self.sections.push(SectionHeading { text: "ПОЗЫ", y: cy });
        cy += 14.0;

        let chip_w = (inner_w - gap * 2.0) / 3.0;
        let chip_h = 28.0f32;
        let poses = [
            (
                "Сидеть",
                "perch",
                (snap.active_pose == "perch" || snap.active_pose == "perch_music")
                    && !snap.dance_active,
            ),
            ("Танец", "dance", snap.dance_active),
            ("Сон", "sleep", snap.active_pose == "sleep" && !snap.dance_active),
        ];
        for (idx, (lbl, key, is_act)) in poses.into_iter().enumerate() {
            self.items.push(MenuItem {
                label: lbl.to_string(),
                pill_text: String::new(),
                pill_tone: PillTone::Off,
                action: MenuAction::TogglePose(key.to_string()),
                active: is_act,
                style: ItemStyle::PoseChip,
                x: pad + (idx as f32) * (chip_w + gap),
                y: cy,
                w: chip_w,
                h: chip_h,
            });
        }
        cy += chip_h + 10.0;

        // Divider 1
        self.separators.push(cy);
        cy += 11.0;

        // 3. Section `ПОВЕДЕНИЕ` — 6 clean switch rows + optional multi-monitor row
        self.sections.push(SectionHeading {
            text: "ПОВЕДЕНИЕ",
            y: cy,
        });
        cy += 14.0;

        let row_h = 28.0f32;
        let row_gap = 2.0f32;

        let (gm_text, gm_tone) = match snap.game_mode_override {
            Some(true) => ("FORCE".to_string(), PillTone::Crimson),
            Some(false) => ("OFF".to_string(), PillTone::Off),
            None => {
                if snap.power_saver_active {
                    ("AUTO*".to_string(), PillTone::Cyan)
                } else {
                    ("AUTO".to_string(), PillTone::Off)
                }
            }
        };

        let (focus_text, focus_tone) = if snap.focus_timer_active {
            (
                if snap.focus_timer_label.is_empty() {
                    "ON".to_string()
                } else {
                    snap.focus_timer_label.clone()
                },
                PillTone::Lilac,
            )
        } else {
            ("OFF".to_string(), PillTone::Off)
        };

        let mut switches: Vec<(&str, String, PillTone, MenuAction)> = vec![
            (
                "Физика и гравитация",
                if snap.physics_enabled { "ON" } else { "OFF" }.to_string(),
                if snap.physics_enabled { PillTone::Cyan } else { PillTone::Off },
                MenuAction::TogglePhysics,
            ),
            (
                "Свободная жизнь",
                if snap.autonomous_enabled { "ON" } else { "OFF" }.to_string(),
                if snap.autonomous_enabled { PillTone::Cyan } else { PillTone::Off },
                MenuAction::ToggleAutonomous,
            ),
            (
                "Следовать за окном",
                if snap.follow_workspaces { "ON" } else { "OFF" }.to_string(),
                if snap.follow_workspaces { PillTone::Cyan } else { PillTone::Off },
                MenuAction::ToggleFollowWorkspaces,
            ),
            (
                "Авто-позы (музыка / сон)",
                if snap.auto_poses_enabled { "ON" } else { "OFF" }.to_string(),
                if snap.auto_poses_enabled { PillTone::Cyan } else { PillTone::Off },
                MenuAction::ToggleAutoPoses,
            ),
            (
                "Фокус-таймер (Pomodoro)",
                focus_text,
                focus_tone,
                MenuAction::CycleFocusTimer,
            ),
            ("Game Mode", gm_text, gm_tone, MenuAction::CycleGameMode),
        ];

        if snap.outputs.len() > 1 {
            let mon_lbl = if !snap.active_output.is_empty() {
                snap.active_output.to_string()
            } else {
                snap.outputs[0].clone()
            };
            switches.push(("Монитор", mon_lbl, PillTone::Cyan, MenuAction::CycleMonitor));
        }

        for (title, pill, tone, act) in switches {
            self.items.push(MenuItem {
                label: title.to_string(),
                pill_text: pill,
                pill_tone: tone,
                action: act,
                active: tone != PillTone::Off,
                style: ItemStyle::SwitchRow,
                x: pad,
                y: cy,
                w: inner_w,
                h: row_h,
            });
            cy += row_h + row_gap;
        }
        cy += 7.0;

        // Divider 2
        self.separators.push(cy);
        cy += 10.0;

        // 4. Quick Action Footer
        let ask_h = 31.0f32;
        self.items.push(MenuItem {
            label: "Быстрый вопрос".to_string(),
            pill_text: String::new(),
            pill_tone: PillTone::Off,
            action: MenuAction::QuickAsk,
            active: false,
            style: ItemStyle::AskCta,
            x: pad,
            y: cy,
            w: inner_w,
            h: ask_h,
        });
        cy += ask_h + 6.0;

        let half_w = (inner_w - gap) * 0.5;
        let btn_h = 29.0f32;
        let voice_busy = st_norm == "listening" || st_norm == "speaking";
        self.items.push(MenuItem {
            label: if voice_busy { "Стоп" } else { "Голос" }.to_string(),
            pill_text: String::new(),
            pill_tone: PillTone::Off,
            action: MenuAction::ToggleVoice,
            active: voice_busy,
            style: ItemStyle::VoiceBtn,
            x: pad,
            y: cy,
            w: half_w,
            h: btn_h,
        });
        self.items.push(MenuItem {
            label: "Область".to_string(),
            pill_text: String::new(),
            pill_tone: PillTone::Off,
            action: MenuAction::LookRegion,
            active: false,
            style: ItemStyle::LookBtn,
            x: pad + half_w + gap,
            y: cy,
            w: half_w,
            h: btn_h,
        });
        cy += btn_h + 6.0;

        self.items.push(MenuItem {
            label: "На базу".to_string(),
            pill_text: String::new(),
            pill_tone: PillTone::Off,
            action: MenuAction::ReturnHome,
            active: false,
            style: ItemStyle::FooterBtn,
            x: pad,
            y: cy,
            w: half_w,
            h: btn_h,
        });
        self.items.push(MenuItem {
            label: if snap.pinned { "Открепить" } else { "Закрепить тут" }.to_string(),
            pill_text: String::new(),
            pill_tone: PillTone::Off,
            action: MenuAction::TogglePin,
            active: snap.pinned,
            style: ItemStyle::FooterBtn,
            x: pad + half_w + gap,
            y: cy,
            w: half_w,
            h: btn_h,
        });
        cy += btn_h + 6.0;

        let quit_h = 24.0f32;
        self.items.push(MenuItem {
            label: "Выключить аватар".to_string(),
            pill_text: String::new(),
            pill_tone: PillTone::Off,
            action: MenuAction::Quit,
            active: false,
            style: ItemStyle::QuitLink,
            x: pad,
            y: cy,
            w: inner_w,
            h: quit_h,
        });
        cy += quit_h + 9.0;

        self.height = cy.ceil() as u32;
        self.dirty = true;
    }

    pub fn close(&mut self) {
        self.visible = false;
        self.dragging = false;
        self.hovered_idx = None;
    }

    /// Start dragging the HUD card when LMB presses on the header or non-button card area.
    pub fn begin_drag(&mut self, px: f64, py: f64) -> bool {
        if !self.visible || !self.contains(px, py) || self.hit_test(px, py).is_some() {
            return false;
        }
        self.dragging = true;
        self.drag_offset = (px - self.x as f64, py - self.y as f64);
        true
    }

    pub fn end_drag(&mut self) {
        self.dragging = false;
    }

    pub fn contains(&self, px: f64, py: f64) -> bool {
        if !self.visible {
            return false;
        }
        let x0 = self.x as f64;
        let y0 = self.y as f64;
        px >= x0 && px <= x0 + self.width as f64 && py >= y0 && py <= y0 + self.height as f64
    }

    fn hit_test(&self, px: f64, py: f64) -> Option<usize> {
        if !self.contains(px, py) {
            return None;
        }
        let lx = (px - self.x as f64) as f32;
        let ly = (py - self.y as f64) as f32;
        for (idx, it) in self.items.iter().enumerate() {
            if lx >= it.x && lx <= it.x + it.w && ly >= it.y && ly <= it.y + it.h {
                return Some(idx);
            }
        }
        None
    }

    pub fn on_pointer_move(&mut self, px: f64, py: f64) -> bool {
        if !self.visible {
            return false;
        }
        if self.dragging {
            let (_, _, _, _, stage_w, stage_h) = self.last_anchor;
            let max_x = (stage_w as i32 - self.width as i32 - 12).max(12);
            let max_y = (stage_h as i32 - self.height as i32 - 12).max(12);
            let nx = ((px - self.drag_offset.0).round() as i32).clamp(12, max_x);
            let ny = ((py - self.drag_offset.1).round() as i32).clamp(12, max_y);
            if nx != self.x || ny != self.y {
                self.x = nx;
                self.y = ny;
                return true;
            }
            return false;
        }
        let next = self.hit_test(px, py);
        if next != self.hovered_idx {
            self.hovered_idx = next;
            self.dirty = true;
            return true;
        }
        false
    }

    pub fn on_click(&mut self, px: f64, py: f64) -> Option<MenuAction> {
        if !self.visible {
            return None;
        }
        if !self.contains(px, py) {
            self.close();
            return None;
        }
        let hit = self.hit_test(px, py).map(|idx| self.items[idx].action.clone());
        match hit {
            Some(act) if act.keeps_menu_open() => Some(act),
            Some(MenuAction::Close) => {
                self.close();
                None
            }
            Some(act) => {
                self.close();
                Some(act)
            }
            None => None,
        }
    }

    pub fn render(&mut self, font: Option<&Font>) -> Option<(&Pixmap, i32, i32)> {
        if !self.visible {
            return None;
        }
        if self.dirty
            || self.cached.as_ref().map(|p| (p.width(), p.height()))
                != Some((self.width, self.height))
        {
            let mut pm = Pixmap::new(self.width, self.height)?;
            pm.fill(Color::TRANSPARENT);
            let w = self.width as f32;
            let h = self.height as f32;

            // 1. Crimson Graphite card shell
            fill_rounded_rect(&mut pm, 0.5, 0.5, w - 1.0, h - 1.0, 14.0, BG_SURFACE);
            stroke_rounded_rect(&mut pm, 0.5, 0.5, w - 1.0, h - 1.0, 14.0, BORDER_CARD, 1.0);

            // 2. Header: crimson dot + CAPRI + LIVE pill
            fill_circle(&mut pm, 16.0, 21.0, 4.0, CRIMSON);
            if let Some(f) = font {
                draw_text(&mut pm, f, "CAPRI", 12.0, 25.0, 25.0, TEXT_PRIMARY);
                let bw = (measure_width(f, &self.status_badge, 9.5) + 14.0).clamp(36.0, 86.0);
                let bx = 70.0f32;
                let by = 13.0f32;
                let bh = 16.0f32;
                fill_rounded_rect(&mut pm, bx, by, bw, bh, 7.0, CYAN_SOFT);
                stroke_rounded_rect(&mut pm, bx, by, bw, bh, 7.0, CYAN_BORDER, 0.9);
                let tw = measure_width(f, &self.status_badge, 9.5);
                draw_text(
                    &mut pm,
                    f,
                    &self.status_badge,
                    9.5,
                    bx + (bw - tw) * 0.5,
                    by + 11.3,
                    (CYAN.0, CYAN.1, CYAN.2),
                );
            }

            // 3. Section headings
            if let Some(f) = font {
                for sec in &self.sections {
                    draw_text(&mut pm, f, sec.text, 9.5, 13.0, sec.y + 8.5, TEXT_MUTED);
                }
            }

            // 4. Hairline dividers
            for &sy in &self.separators {
                fill_rect(&mut pm, 12.0, sy, w - 24.0, 1.0, BORDER_SUBTLE);
            }

            // 5. Interactive items
            for (idx, it) in self.items.iter().enumerate() {
                let hovered = self.hovered_idx == Some(idx);
                match it.style {
                    ItemStyle::RelicHeaderBtn => {
                        let bg = if hovered { CRIMSON } else { CRIMSON_SOFT };
                        let br = if hovered { CRIMSON } else { CRIMSON_BORDER };
                        fill_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 7.0, bg);
                        stroke_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 7.0, br, 1.0);
                        draw_diamond_icon(&mut pm, it.x + 11.0, it.y + it.h * 0.5, 3.6, if hovered { (255, 255, 255, 255) } else { CRIMSON });
                        if let Some(f) = font {
                            draw_text(&mut pm, f, &it.label, 11.0, it.x + 19.0, it.y + 14.8, TEXT_PRIMARY);
                        }
                    }
                    ItemStyle::CloseHeaderBtn => {
                        if hovered {
                            fill_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 7.0, BG_RAISED);
                            stroke_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 7.0, BORDER_SUBTLE, 0.9);
                        }
                        let col = if hovered {
                            (TEXT_PRIMARY.0, TEXT_PRIMARY.1, TEXT_PRIMARY.2, 255)
                        } else {
                            (TEXT_MUTED.0, TEXT_MUTED.1, TEXT_MUTED.2, 230)
                        };
                        draw_cross_icon(&mut pm, it.x + it.w * 0.5, it.y + it.h * 0.5, 3.8, col);
                    }
                    ItemStyle::PoseChip => {
                        let bg = if it.active {
                            CRIMSON_SOFT
                        } else if hovered {
                            BG_RAISED
                        } else {
                            BG_SURFACE_ALT
                        };
                        let br = if it.active {
                            CRIMSON
                        } else if hovered {
                            BORDER_CARD
                        } else {
                            BORDER_SUBTLE
                        };
                        fill_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 9.0, bg);
                        stroke_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 9.0, br, 1.0);
                        if let Some(f) = font {
                            let tw = measure_width(f, &it.label, 11.5);
                            let col = if it.active || hovered {
                                TEXT_PRIMARY
                            } else {
                                TEXT_SECONDARY
                            };
                            draw_text(
                                &mut pm,
                                f,
                                &it.label,
                                11.5,
                                it.x + (it.w - tw) * 0.5,
                                it.y + 18.0,
                                col,
                            );
                        }
                    }
                    ItemStyle::SwitchRow => {
                        if hovered {
                            fill_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 8.0, BG_RAISED);
                            stroke_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 8.0, BORDER_SUBTLE, 0.9);
                        }
                        if let Some(f) = font {
                            draw_text(&mut pm, f, &it.label, 11.5, it.x + 8.0, it.y + 18.0, TEXT_PRIMARY);

                            let (pbg, pbr, pfg) = match it.pill_tone {
                                PillTone::Cyan => (CYAN_SOFT, CYAN_BORDER, (CYAN.0, CYAN.1, CYAN.2)),
                                PillTone::Crimson => (CRIMSON_SOFT, CRIMSON_BORDER, (CORAL_TEXT.0, CORAL_TEXT.1, CORAL_TEXT.2)),
                                PillTone::Lilac => (LILAC_SOFT, LILAC_BORDER, (LILAC.0, LILAC.1, LILAC.2)),
                                PillTone::Off => (BG_RAISED, BORDER_SUBTLE, TEXT_MUTED),
                            };
                            let pw = (measure_width(f, &it.pill_text, 9.5) + 14.0).clamp(32.0, 62.0);
                            let ph = 17.5f32;
                            let px = it.x + it.w - pw - 5.0;
                            let py = it.y + (it.h - ph) * 0.5;
                            fill_rounded_rect(&mut pm, px, py, pw, ph, 7.5, pbg);
                            stroke_rounded_rect(&mut pm, px, py, pw, ph, 7.5, pbr, 0.9);
                            let tw = measure_width(f, &it.pill_text, 9.5);
                            draw_text(
                                &mut pm,
                                f,
                                &it.pill_text,
                                9.5,
                                px + (pw - tw) * 0.5,
                                py + 12.2,
                                pfg,
                            );
                        }
                    }
                    ItemStyle::AskCta => {
                        let bg = if hovered { CRIMSON_SOFT_HOV } else { CRIMSON_SOFT };
                        let br = if hovered { CRIMSON } else { CRIMSON_BORDER };
                        fill_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 9.0, bg);
                        stroke_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 9.0, br, 1.0);
                        if let Some(f) = font {
                            let tw = measure_width(f, &it.label, 12.0);
                            let total_w = 14.0 + tw;
                            let sx = it.x + (it.w - total_w) * 0.5;
                            draw_bolt_icon(&mut pm, sx + 4.5, it.y + it.h * 0.5, (255, 204, 88, 255));
                            draw_text(&mut pm, f, &it.label, 12.0, sx + 14.0, it.y + 19.6, TEXT_PRIMARY);
                        }
                    }
                    ItemStyle::VoiceBtn => {
                        let (bg, br, fg, icon_col) = if it.active {
                            (CRIMSON, CRIMSON, TEXT_PRIMARY, (255, 255, 255, 255))
                        } else if hovered {
                            (CYAN, CYAN, TEXT_DARK, (TEXT_DARK.0, TEXT_DARK.1, TEXT_DARK.2, 255))
                        } else {
                            (CYAN_SOFT, CYAN_BORDER, (CYAN.0, CYAN.1, CYAN.2), CYAN)
                        };
                        fill_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 9.0, bg);
                        stroke_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 9.0, br, 1.0);
                        if let Some(f) = font {
                            let tw = measure_width(f, &it.label, 11.5);
                            let total_w = 14.0 + tw;
                            let sx = it.x + (it.w - total_w) * 0.5;
                            draw_mic_icon(&mut pm, sx + 4.5, it.y + it.h * 0.5, icon_col);
                            draw_text(&mut pm, f, &it.label, 11.5, sx + 14.0, it.y + 18.6, fg);
                        }
                    }
                    ItemStyle::LookBtn => {
                        let (bg, br, fg, icon_col) = if hovered {
                            (CYAN_SOFT_HOV, CYAN_BORDER, (CYAN.0, CYAN.1, CYAN.2), CYAN)
                        } else {
                            (BG_RAISED, BORDER_CARD, TEXT_PRIMARY, (TEXT_SECONDARY.0, TEXT_SECONDARY.1, TEXT_SECONDARY.2, 255))
                        };
                        fill_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 9.0, bg);
                        stroke_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 9.0, br, 1.0);
                        if let Some(f) = font {
                            let tw = measure_width(f, &it.label, 11.5);
                            let total_w = 16.0 + tw;
                            let sx = it.x + (it.w - total_w) * 0.5;
                            draw_eye_icon(&mut pm, sx + 6.0, it.y + it.h * 0.5, icon_col);
                            draw_text(&mut pm, f, &it.label, 11.5, sx + 16.0, it.y + 18.6, fg);
                        }
                    }
                    ItemStyle::FooterBtn => {
                        let bg = if it.active {
                            CRIMSON_SOFT
                        } else if hovered {
                            BG_HOVER
                        } else {
                            BG_RAISED
                        };
                        let br = if it.active {
                            CRIMSON
                        } else if hovered {
                            CRIMSON_BORDER
                        } else {
                            BORDER_SUBTLE
                        };
                        fill_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 9.0, bg);
                        stroke_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 9.0, br, 1.0);
                        if let Some(f) = font {
                            let tw = measure_width(f, &it.label, 11.5);
                            draw_text(
                                &mut pm,
                                f,
                                &it.label,
                                11.5,
                                it.x + (it.w - tw) * 0.5,
                                it.y + 18.6,
                                TEXT_PRIMARY,
                            );
                        }
                    }
                    ItemStyle::QuitLink => {
                        if hovered {
                            fill_rounded_rect(&mut pm, it.x, it.y, it.w, it.h, 7.0, CRIMSON_SOFT);
                        }
                        if let Some(f) = font {
                            let tw = measure_width(f, &it.label, 11.0);
                            let col = if hovered {
                                (CORAL_TEXT.0, CORAL_TEXT.1, CORAL_TEXT.2)
                            } else {
                                TEXT_MUTED
                            };
                            draw_text(
                                &mut pm,
                                f,
                                &it.label,
                                11.0,
                                it.x + (it.w - tw) * 0.5,
                                it.y + 15.6,
                                col,
                            );
                        }
                    }
                }
            }

            self.cached = Some(pm);
            self.dirty = false;
        }
        self.cached.as_ref().map(|pm| (pm, self.x, self.y))
    }
}

// ── Crisp Vector Primitives & Icons ─────────────────────────────────────────

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

fn fill_rounded_rect(pm: &mut Pixmap, x: f32, y: f32, w: f32, h: f32, r: f32, rgba: (u8, u8, u8, u8)) {
    let Some(path) = rounded_rect_path(x, y, w, h, r) else {
        return;
    };
    let mut paint = Paint::default();
    paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
    paint.anti_alias = true;
    pm.fill_path(&path, &paint, FillRule::Winding, Transform::identity(), None);
}

#[allow(clippy::too_many_arguments)]
fn stroke_rounded_rect(
    pm: &mut Pixmap,
    x: f32,
    y: f32,
    w: f32,
    h: f32,
    r: f32,
    rgba: (u8, u8, u8, u8),
    width: f32,
) {
    let Some(path) = rounded_rect_path(x, y, w, h, r) else {
        return;
    };
    let mut paint = Paint::default();
    paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
    paint.anti_alias = true;
    let stroke = Stroke {
        width,
        ..Stroke::default()
    };
    pm.stroke_path(&path, &paint, &stroke, Transform::identity(), None);
}

fn fill_rect(pm: &mut Pixmap, x: f32, y: f32, w: f32, h: f32, rgba: (u8, u8, u8, u8)) {
    let Some(rect) = Rect::from_xywh(x, y, w, h) else {
        return;
    };
    let mut paint = Paint::default();
    paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
    pm.fill_rect(rect, &paint, Transform::identity(), None);
}

fn fill_circle(pm: &mut Pixmap, cx: f32, cy: f32, r: f32, rgba: (u8, u8, u8, u8)) {
    let Some(path) = PathBuilder::from_circle(cx, cy, r) else {
        return;
    };
    let mut paint = Paint::default();
    paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
    paint.anti_alias = true;
    pm.fill_path(&path, &paint, FillRule::Winding, Transform::identity(), None);
}

fn draw_diamond_icon(pm: &mut Pixmap, cx: f32, cy: f32, r: f32, rgba: (u8, u8, u8, u8)) {
    let mut pb = PathBuilder::new();
    pb.move_to(cx, cy - r);
    pb.line_to(cx + r, cy);
    pb.line_to(cx, cy + r);
    pb.line_to(cx - r, cy);
    pb.close();
    if let Some(path) = pb.finish() {
        let mut paint = Paint::default();
        paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
        paint.anti_alias = true;
        pm.fill_path(&path, &paint, FillRule::Winding, Transform::identity(), None);
    }
}

fn draw_cross_icon(pm: &mut Pixmap, cx: f32, cy: f32, r: f32, rgba: (u8, u8, u8, u8)) {
    let mut pb = PathBuilder::new();
    pb.move_to(cx - r, cy - r);
    pb.line_to(cx + r, cy + r);
    pb.move_to(cx + r, cy - r);
    pb.line_to(cx - r, cy + r);
    if let Some(path) = pb.finish() {
        let mut paint = Paint::default();
        paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
        paint.anti_alias = true;
        let stroke = Stroke {
            width: 1.5,
            line_cap: LineCap::Round,
            ..Stroke::default()
        };
        pm.stroke_path(&path, &paint, &stroke, Transform::identity(), None);
    }
}

fn draw_bolt_icon(pm: &mut Pixmap, cx: f32, cy: f32, rgba: (u8, u8, u8, u8)) {
    let mut pb = PathBuilder::new();
    pb.move_to(cx + 1.0, cy - 5.5);
    pb.line_to(cx - 3.5, cy + 0.5);
    pb.line_to(cx + 0.2, cy + 0.5);
    pb.line_to(cx - 1.2, cy + 5.5);
    pb.line_to(cx + 3.5, cy - 0.8);
    pb.line_to(cx - 0.1, cy - 0.8);
    pb.close();
    if let Some(path) = pb.finish() {
        let mut paint = Paint::default();
        paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
        paint.anti_alias = true;
        pm.fill_path(&path, &paint, FillRule::Winding, Transform::identity(), None);
    }
}

fn draw_mic_icon(pm: &mut Pixmap, cx: f32, cy: f32, rgba: (u8, u8, u8, u8)) {
    fill_rounded_rect(pm, cx - 2.3, cy - 5.0, 4.6, 6.8, 2.3, rgba);
    let mut pb = PathBuilder::new();
    pb.move_to(cx - 4.2, cy - 0.2);
    pb.quad_to(cx - 4.2, cy + 3.6, cx, cy + 3.6);
    pb.quad_to(cx + 4.2, cy + 3.6, cx + 4.2, cy - 0.2);
    pb.move_to(cx, cy + 3.6);
    pb.line_to(cx, cy + 5.6);
    if let Some(path) = pb.finish() {
        let mut paint = Paint::default();
        paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
        paint.anti_alias = true;
        let stroke = Stroke {
            width: 1.2,
            line_cap: LineCap::Round,
            ..Stroke::default()
        };
        pm.stroke_path(&path, &paint, &stroke, Transform::identity(), None);
    }
}

fn draw_eye_icon(pm: &mut Pixmap, cx: f32, cy: f32, rgba: (u8, u8, u8, u8)) {
    let mut pb = PathBuilder::new();
    pb.move_to(cx - 5.5, cy);
    pb.quad_to(cx, cy - 4.8, cx + 5.5, cy);
    pb.quad_to(cx, cy + 4.8, cx - 5.5, cy);
    pb.close();
    if let Some(path) = pb.finish() {
        let mut paint = Paint::default();
        paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
        paint.anti_alias = true;
        let stroke = Stroke {
            width: 1.2,
            ..Stroke::default()
        };
        pm.stroke_path(&path, &paint, &stroke, Transform::identity(), None);
    }
    fill_circle(pm, cx, cy, 1.8, rgba);
}

fn measure_width(font: &Font, text: &str, px: f32) -> f32 {
    text.chars().map(|ch| font.metrics(ch, px).advance_width).sum()
}

fn draw_text(pm: &mut Pixmap, font: &Font, text: &str, px: f32, start_x: f32, baseline_y: f32, rgb: (u8, u8, u8)) {
    let cw = pm.width() as i32;
    let ch = pm.height() as i32;
    let pixels = pm.pixels_mut();
    let mut pen_x = start_x;
    for ch_glyph in text.chars() {
        let (metrics, bitmap) = font.rasterize(ch_glyph, px);
        let gx0 = (pen_x + metrics.xmin as f32).round() as i32;
        let gy0 = (baseline_y - metrics.height as f32 - metrics.ymin as f32).round() as i32;
        for row in 0..metrics.height {
            let dy = gy0 + row as i32;
            if dy < 0 || dy >= ch {
                continue;
            }
            let row_off = (dy as usize) * (cw as usize);
            let b_off = row * metrics.width;
            for col in 0..metrics.width {
                let dx = gx0 + col as i32;
                if dx < 0 || dx >= cw {
                    continue;
                }
                let a = bitmap[b_off + col];
                if a == 0 {
                    continue;
                }
                let idx = row_off + (dx as usize);
                let dst = pixels[idx];
                let sa = a as u16;
                let inv = 255u16 - sa;
                let sr = (rgb.0 as u16 * sa + 127) / 255;
                let sg = (rgb.1 as u16 * sa + 127) / 255;
                let sb = (rgb.2 as u16 * sa + 127) / 255;
                let out_r = (sr + (dst.red() as u16 * inv + 127) / 255).min(255) as u8;
                let out_g = (sg + (dst.green() as u16 * inv + 127) / 255).min(255) as u8;
                let out_b = (sb + (dst.blue() as u16 * inv + 127) / 255).min(255) as u8;
                let out_a = (sa + (dst.alpha() as u16 * inv + 127) / 255).min(255) as u8;
                if let Some(p) = tiny_skia::PremultipliedColorU8::from_rgba(out_r, out_g, out_b, out_a) {
                    pixels[idx] = p;
                }
            }
        }
        pen_x += metrics.advance_width;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_snapshot<'a>(outs: &'a [String]) -> MenuSnapshot<'a> {
        MenuSnapshot {
            avatar_state: "idle",
            active_pose: "normal",
            dance_active: false,
            physics_enabled: true,
            autonomous_enabled: true,
            follow_workspaces: false,
            auto_poses_enabled: true,
            focus_timer_active: false,
            focus_timer_label: "OFF".to_string(),
            game_mode_override: None,
            power_saver_active: false,
            pinned: false,
            perched: false,
            outputs: outs,
            active_output: "DP-1",
        }
    }

    #[test]
    fn menu_opens_beside_avatar_clamps_to_screen_and_hits_actions() {
        let mut menu = ContextMenu::new();
        let outs = vec!["DP-1".to_string(), "HDMI-A-1".to_string()];
        let snap = sample_snapshot(&outs);
        menu.open(1620.0, 615.0, 300.0, 420.0, 1920, 1035, &snap);
        assert!(menu.visible);
        assert!(menu.x >= 12 && menu.x + menu.width as i32 <= 1920 - 12);
        assert!(menu.y >= 12 && menu.y + menu.height as i32 <= 1035 - 12);
        assert!(menu.render(None).is_some());

        // Toggle physics stays open
        let phys_it = menu
            .items
            .iter()
            .find(|i| i.action == MenuAction::TogglePhysics)
            .expect("physics row")
            .clone();
        let act = menu.on_click(
            menu.x as f64 + phys_it.x as f64 + 10.0,
            menu.y as f64 + phys_it.y as f64 + 10.0,
        );
        assert_eq!(act, Some(MenuAction::TogglePhysics));
        assert!(menu.visible, "toggle rows should keep HUD open for quick adjustments");

        // LookRegion closes HUD after dispatch
        let look_it = menu
            .items
            .iter()
            .find(|i| i.action == MenuAction::LookRegion)
            .expect("look region button")
            .clone();
        let act2 = menu.on_click(
            menu.x as f64 + look_it.x as f64 + 10.0,
            menu.y as f64 + look_it.y as f64 + 10.0,
        );
        assert_eq!(act2, Some(MenuAction::LookRegion));
        assert!(!menu.visible);
    }

    #[test]
    fn menu_click_outside_dismisses_without_action() {
        let mut menu = ContextMenu::new();
        let outs = vec!["DP-1".to_string()];
        let snap = sample_snapshot(&outs);
        menu.open(20.0, 600.0, 300.0, 420.0, 1920, 1035, &snap);
        assert!(menu.visible);
        let act = menu.on_click(1850.0, 100.0);
        assert!(act.is_none());
        assert!(!menu.visible);
    }

    #[test]
    fn menu_can_be_dragged_by_header_and_clamped_to_stage() {
        let mut menu = ContextMenu::new();
        let outs = vec!["DP-1".to_string()];
        let snap = sample_snapshot(&outs);
        menu.open(1500.0, 500.0, 300.0, 420.0, 1920, 1035, &snap);
        let start_x = menu.x;
        let start_y = menu.y;
        // Grab the CAPRI header (non-button region)
        assert!(menu.begin_drag(start_x as f64 + 30.0, start_y as f64 + 20.0));
        assert!(menu.dragging);
        assert!(menu.on_pointer_move(start_x as f64 - 170.0, start_y as f64 - 80.0));
        assert_eq!(menu.x, start_x - 200);
        assert_eq!(menu.y, start_y - 100);
        // Clamps safely to top-left margin
        assert!(menu.on_pointer_move(-500.0, -500.0));
        assert_eq!(menu.x, 12);
        assert_eq!(menu.y, 12);
        menu.end_drag();
        assert!(!menu.dragging);
    }
}
