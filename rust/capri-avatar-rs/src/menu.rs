//! Compact native Crimson Graphite RMB Quick Menu rendered with `tiny-skia` + `fontdue`.
//!
//! Zero Qt dependency: composites directly onto the `zwlr_layer_shell_v1` stage when open,
//! supports hover highlighting, click-outside dismissal, poses, physics toggle,
//! multi-monitor switching, and delegates Quick Ask / Cyberdeck / Voice over IPC.

use fontdue::Font;
use tiny_skia::{
    Color, FillRule, Paint, PathBuilder, Pixmap, PremultipliedColorU8, Rect, Stroke, Transform,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MenuAction {
    SetPose(String),
    ToggleDance,
    TogglePhysics,
    SwitchOutput(String),
    QuickAsk,
    ToggleCyberdeck,
    ToggleVoice,
    Quit,
}

#[derive(Debug, Clone)]
pub struct MenuItem {
    pub label: String,
    pub action: MenuAction,
    pub active: bool,
    pub danger: bool,
    pub separator_before: bool,
    pub y: f32,
    pub h: f32,
}

pub struct QuickMenu {
    pub visible: bool,
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    items: Vec<MenuItem>,
    hovered_idx: Option<usize>,
    cached_pixmap: Option<Pixmap>,
    dirty: bool,
}

impl Default for QuickMenu {
    fn default() -> Self {
        Self::new()
    }
}

impl QuickMenu {
    pub fn new() -> Self {
        Self {
            visible: false,
            x: 0,
            y: 0,
            width: 224,
            height: 0,
            items: Vec::new(),
            hovered_idx: None,
            cached_pixmap: None,
            dirty: true,
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn open(
        &mut self,
        click_x: f64,
        click_y: f64,
        stage_w: u32,
        stage_h: u32,
        current_pose: &str,
        dance_active: bool,
        physics_enabled: bool,
        outputs: &[String],
        active_output: &str,
    ) {
        self.items.clear();
        let mut cursor_y = 32.0f32;
        let row_h = 26.0f32;

        let mut push_item = |label: String, action: MenuAction, active: bool, danger: bool, sep: bool| {
            if sep {
                cursor_y += 7.0;
            }
            self.items.push(MenuItem {
                label,
                action,
                active,
                danger,
                separator_before: sep,
                y: cursor_y,
                h: row_h,
            });
            cursor_y += row_h + 2.0;
        };

        push_item(
            "Поза: Обычная".to_string(),
            MenuAction::SetPose("normal".to_string()),
            current_pose == "normal" && !dance_active,
            false,
            false,
        );
        push_item(
            "Поза: Присест".to_string(),
            MenuAction::SetPose("perch".to_string()),
            current_pose == "perch",
            false,
            false,
        );
        push_item(
            "Поза: Сон".to_string(),
            MenuAction::SetPose("sleep".to_string()),
            current_pose == "sleep",
            false,
            false,
        );
        push_item(
            "Танец".to_string(),
            MenuAction::ToggleDance,
            dance_active,
            false,
            false,
        );
        push_item(
            if physics_enabled {
                "Физика: ВКЛ".to_string()
            } else {
                "Физика: ВЫКЛ".to_string()
            },
            MenuAction::TogglePhysics,
            physics_enabled,
            false,
            true,
        );

        if outputs.len() > 1 {
            for (idx, name) in outputs.iter().enumerate() {
                let is_cur = name == active_output;
                push_item(
                    format!("Экран: {name}"),
                    MenuAction::SwitchOutput(name.clone()),
                    is_cur,
                    false,
                    idx == 0,
                );
            }
        }

        push_item("Быстрый вопрос".to_string(), MenuAction::QuickAsk, false, false, true);
        push_item("Cyberdeck".to_string(), MenuAction::ToggleCyberdeck, false, false, false);
        push_item("Голос".to_string(), MenuAction::ToggleVoice, false, false, false);
        push_item("Выход".to_string(), MenuAction::Quit, false, true, true);

        self.width = 224;
        self.height = (cursor_y + 8.0).ceil() as u32;
        let max_x = (stage_w as i32 - self.width as i32 - 8).max(8);
        let max_y = (stage_h as i32 - self.height as i32 - 8).max(8);
        self.x = (click_x.round() as i32).clamp(8, max_x);
        self.y = (click_y.round() as i32).clamp(8, max_y);
        self.hovered_idx = None;
        self.visible = true;
        self.dirty = true;
    }

    pub fn close(&mut self) {
        self.visible = false;
        self.hovered_idx = None;
        self.dirty = true;
    }

    pub fn contains(&self, px: f64, py: f64) -> bool {
        if !self.visible {
            return false;
        }
        px >= self.x as f64
            && px <= (self.x + self.width as i32) as f64
            && py >= self.y as f64
            && py <= (self.y + self.height as i32) as f64
    }

    /// Update hovered row index; returns `true` if hover state changed and menu needs repaint.
    pub fn on_pointer_move(&mut self, px: f64, py: f64) -> bool {
        if !self.visible {
            return false;
        }
        let next = self.hit_index(px, py);
        if next != self.hovered_idx {
            self.hovered_idx = next;
            self.dirty = true;
            return true;
        }
        false
    }

    /// Handle left-click while menu is visible; returns clicked `MenuAction` or `None` if clicked outside.
    pub fn on_click(&mut self, px: f64, py: f64) -> Option<MenuAction> {
        if !self.visible {
            return None;
        }
        let idx = self.hit_index(px, py);
        self.close();
        idx.and_then(|i| self.items.get(i).map(|it| it.action.clone()))
    }

    fn hit_index(&self, px: f64, py: f64) -> Option<usize> {
        if !self.contains(px, py) {
            return None;
        }
        let ly = (py - self.y as f64) as f32;
        for (idx, item) in self.items.iter().enumerate() {
            if ly >= item.y && ly <= item.y + item.h {
                return Some(idx);
            }
        }
        None
    }

    pub fn render(&mut self, font: Option<&Font>) -> Option<(&Pixmap, i32, i32)> {
        if !self.visible || self.width == 0 || self.height == 0 {
            return None;
        }
        if self.dirty || self.cached_pixmap.is_none() {
            let mut pm = Pixmap::new(self.width, self.height)?;
            pm.fill(Color::TRANSPARENT);
            draw_menu_card(&mut pm, font, &self.items, self.hovered_idx);
            self.cached_pixmap = Some(pm);
            self.dirty = false;
        }
        self.cached_pixmap.as_ref().map(|pm| (pm, self.x, self.y))
    }
}

fn draw_menu_card(pm: &mut Pixmap, font: Option<&Font>, items: &[MenuItem], hovered: Option<usize>) {
    let w = pm.width() as f32;
    let h = pm.height() as f32;

    if let Some(rr) = rounded_rect(0.5, 0.5, w - 1.0, h - 1.0, 10.0) {
        let mut fill = Paint::default();
        fill.set_color_rgba8(13, 19, 30, 245);
        fill.anti_alias = true;
        pm.fill_path(&rr, &fill, FillRule::Winding, Transform::identity(), None);

        let mut border = Paint::default();
        border.set_color_rgba8(105, 213, 232, 165);
        border.anti_alias = true;
        let stroke = Stroke {
            width: 1.2,
            ..Stroke::default()
        };
        pm.stroke_path(&rr, &border, &stroke, Transform::identity(), None);
    }

    // Crimson header bar + title
    fill_rect(pm, 10.0, 10.0, 3.0, 14.0, (242, 119, 154, 235));
    if let Some(f) = font {
        draw_text(pm, f, "CAPRI // AVATAR", 11.5, 20.0, 21.5, (215, 224, 238));
    }
    fill_rect(pm, 10.0, 28.0, w - 20.0, 1.0, (105, 213, 232, 55));

    for (idx, item) in items.iter().enumerate() {
        if item.separator_before {
            fill_rect(pm, 12.0, item.y - 4.0, w - 24.0, 1.0, (105, 213, 232, 42));
        }
        let is_hovered = hovered == Some(idx);
        if is_hovered || item.active {
            if let Some(rr) = rounded_rect(8.0, item.y, w - 16.0, item.h, 6.0) {
                let mut bg = Paint::default();
                if is_hovered {
                    if item.danger {
                        bg.set_color_rgba8(120, 32, 52, 215);
                    } else {
                        bg.set_color_rgba8(32, 46, 70, 225);
                    }
                } else {
                    bg.set_color_rgba8(24, 34, 52, 165);
                }
                bg.anti_alias = true;
                pm.fill_path(&rr, &bg, FillRule::Winding, Transform::identity(), None);
            }
        }

        // Status dot
        let dot_col = if item.danger {
            (242, 119, 154, 230)
        } else if item.active {
            (91, 205, 162, 240)
        } else {
            (105, 213, 232, 120)
        };
        if let Some(dot) = PathBuilder::from_circle(19.0, item.y + item.h * 0.5, if item.active { 3.5 } else { 2.2 }) {
            let mut p = Paint::default();
            p.set_color_rgba8(dot_col.0, dot_col.1, dot_col.2, dot_col.3);
            p.anti_alias = true;
            pm.fill_path(&dot, &p, FillRule::Winding, Transform::identity(), None);
        }

        if let Some(f) = font {
            let text_rgb = if item.danger {
                (252, 165, 184)
            } else if item.active || is_hovered {
                (245, 248, 252)
            } else {
                (198, 210, 226)
            };
            draw_text(pm, f, &item.label, 12.5, 30.0, item.y + 17.5, text_rgb);
        }
    }
}

fn fill_rect(pm: &mut Pixmap, x: f32, y: f32, w: f32, h: f32, rgba: (u8, u8, u8, u8)) {
    let Some(rect) = Rect::from_xywh(x, y, w, h) else {
        return;
    };
    let mut paint = Paint::default();
    paint.set_color_rgba8(rgba.0, rgba.1, rgba.2, rgba.3);
    pm.fill_rect(rect, &paint, Transform::identity(), None);
}

fn rounded_rect(x: f32, y: f32, w: f32, h: f32, r: f32) -> Option<tiny_skia::Path> {
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

fn draw_text(
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
                let sa = cov as u16;
                let inv_sa = 255u16 - sa;
                let sr = (rgb.0 as u16 * sa + 127) / 255;
                let sg = (rgb.1 as u16 * sa + 127) / 255;
                let sb = (rgb.2 as u16 * sa + 127) / 255;
                let out_r = (sr + (dst.red() as u16 * inv_sa + 127) / 255).min(255) as u8;
                let out_g = (sg + (dst.green() as u16 * inv_sa + 127) / 255).min(255) as u8;
                let out_b = (sb + (dst.blue() as u16 * inv_sa + 127) / 255).min(255) as u8;
                let out_a = (sa + (dst.alpha() as u16 * inv_sa + 127) / 255).min(255) as u8;
                if let Some(c) = PremultipliedColorU8::from_rgba(out_r, out_g, out_b, out_a) {
                    pixels[idx] = c;
                }
            }
        }
        pen_x += metrics.advance_width;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn menu_opens_clamps_to_screen_and_hits_actions() {
        let mut menu = QuickMenu::new();
        let outs = vec!["DP-1".to_string(), "HDMI-A-1".to_string()];
        menu.open(1900.0, 1050.0, 1920, 1080, "normal", false, true, &outs, "DP-1");
        assert!(menu.visible);
        assert!(menu.x + menu.width as i32 <= 1912);
        assert!(menu.y + menu.height as i32 <= 1072);

        // Click first item (`SetPose("normal")`)
        let first_y = menu.y as f64 + 38.0;
        let act = menu.on_click(menu.x as f64 + 50.0, first_y);
        assert_eq!(act, Some(MenuAction::SetPose("normal".to_string())));
        assert!(!menu.visible);
    }

    #[test]
    fn menu_click_outside_dismisses_without_action() {
        let mut menu = QuickMenu::new();
        menu.open(400.0, 400.0, 1920, 1080, "perch", false, true, &["DP-1".into()], "DP-1");
        assert!(menu.visible);
        assert_eq!(menu.on_click(10.0, 10.0), None);
        assert!(!menu.visible);
    }
}
