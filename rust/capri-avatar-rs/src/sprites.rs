//! Lazy PNG sprite-sheet loader & premultiplied Pixmap cache for `capri-avatar-rs`.
//!
//! Preloads only `idle` and `blink` at startup (matching `capri/ui/sprite_view.py` v3.7.0);
//! all remaining poses/animations decode lazily on first transition.

use std::collections::HashMap;
use std::env;
use std::fs::File;
use std::io::BufReader;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use tiny_skia::{Color, FilterQuality, Pixmap, PixmapPaint, PremultipliedColorU8, Transform};

pub const CELL_W: u32 = 240;
pub const CELL_H: u32 = 384;
pub const DEFAULT_TARGET_W: u32 = 210;
pub const DEFAULT_TARGET_H: u32 = 336;

#[derive(Debug, Clone, Deserialize)]
struct PoseManifestEntry {
    file: Option<String>,
    frames: Option<u32>,
    cell_w: Option<u32>,
    fps: Option<f32>,
    #[serde(skip)]
    base_dir: Option<PathBuf>,
}

const CORE_SPECS: &[(&str, &str, u32, u32)] = &[
    ("idle", "idle.png", 16, 240),
    ("blink", "blink.png", 4, 240),
    ("blink_glitch", "blink_glitch.png", 4, 240),
    ("mouth", "mouth.png", 5, 240),
    ("glitch", "glitch.png", 10, 240),
    ("dance", "dance.png", 16, 240),
    ("sleep", "sleep.png", 16, 240),
    ("wake", "wake.png", 13, 240),
    ("perch", "perch.png", 60, 196),
    ("perch_music", "perch_music.png", 60, 196),
];

const OPTIONAL_SLOTS: &[(&str, &str)] = &[
    ("drag", "drag.png"),
    ("fall", "fall.png"),
    ("splat", "splat.png"),
    ("lounge", "lounge.png"),
    ("walk", "walk.png"),
];

pub struct SpriteStore {
    bundled_dir: PathBuf,
    user_dir: PathBuf,
    target_w: u32,
    target_h: u32,
    cache: HashMap<String, Vec<Pixmap>>,
    manifest: HashMap<String, PoseManifestEntry>,
    slot_tick_divs: HashMap<String, u64>,
}

impl SpriteStore {
    pub fn new(bundled_dir: PathBuf, user_dir: Option<PathBuf>) -> Self {
        let resolved_user = user_dir.unwrap_or_else(default_user_sprites_dir);
        let mut store = Self {
            bundled_dir,
            user_dir: resolved_user,
            target_w: DEFAULT_TARGET_W,
            target_h: DEFAULT_TARGET_H,
            cache: HashMap::new(),
            manifest: HashMap::new(),
            slot_tick_divs: HashMap::new(),
        };
        store.reload();
        store
    }

    pub fn reload(&mut self) {
        self.cache.clear();
        self.slot_tick_divs.clear();
        self.manifest = self.load_manifest();
        // Preload ONLY always-visible startup sheets (`idle` + `blink`)
        let _ = self.get_slot("idle");
        let _ = self.get_slot("blink");
    }

    #[cfg(test)]
    pub fn cached_slot_count(&self) -> usize {
        self.cache.len()
    }

    pub fn slot_tick_div(&self, slot: &str, default_div: u64) -> u64 {
        self.slot_tick_divs
            .get(slot)
            .copied()
            .unwrap_or(default_div)
            .max(1)
    }

    pub fn get_slot(&mut self, slot: &str) -> &[Pixmap] {
        if !self.cache.contains_key(slot) {
            let frames = self.load_slot_uncached(slot);
            self.cache.insert(slot.to_string(), frames);
        }
        self.cache.get(slot).map(Vec::as_slice).unwrap_or(&[])
    }

    fn load_manifest(&self) -> HashMap<String, PoseManifestEntry> {
        let mut out = HashMap::new();
        for base in [&self.bundled_dir, &self.user_dir] {
            let path = base.join("poses.json");
            let Ok(raw) = std::fs::read_to_string(&path) else {
                continue;
            };
            let Ok(parsed) = serde_json::from_str::<HashMap<String, PoseManifestEntry>>(&raw) else {
                continue;
            };
            for (k, mut entry) in parsed {
                entry.base_dir = Some(base.clone());
                out.insert(k.trim().to_ascii_lowercase(), entry);
            }
        }
        out
    }

    fn resolve_sprite_path(&self, filename: &str, base_dir: Option<&Path>) -> Option<PathBuf> {
        if let Some(base) = base_dir {
            let cand = base.join(filename);
            if cand.is_file() {
                return Some(cand);
            }
        }
        let user_cand = self.user_dir.join(filename);
        if user_cand.is_file() {
            return Some(user_cand);
        }
        let bundled_cand = self.bundled_dir.join(filename);
        if bundled_cand.is_file() {
            return Some(bundled_cand);
        }
        None
    }

    fn load_slot_uncached(&mut self, slot: &str) -> Vec<Pixmap> {
        if let Some(&(_, fname, count, cw)) = CORE_SPECS.iter().find(|&&(k, _, _, _)| k == slot) {
            let path = self.bundled_dir.join(fname);
            return slice_fixed_sheet(&path, count, cw, self.target_w, self.target_h);
        }
        if let Some(&(_, default_file)) = OPTIONAL_SLOTS.iter().find(|&&(k, _)| k == slot) {
            let entry = self.manifest.get(slot).cloned();
            let fname = entry
                .as_ref()
                .and_then(|e| e.file.as_deref())
                .unwrap_or(default_file);
            let base = entry.as_ref().and_then(|e| e.base_dir.as_deref());
            let Some(path) = self.resolve_sprite_path(fname, base) else {
                return Vec::new();
            };
            if let Some(fps) = entry.as_ref().and_then(|e| e.fps) {
                let clamped = fps.clamp(1.0, 60.0);
                let div = (60.0 / clamped).round().max(1.0) as u64;
                self.slot_tick_divs.insert(slot.to_string(), div);
            }
            let frames_hint = entry.as_ref().and_then(|e| e.frames).filter(|&v| v > 0);
            let cw_hint = entry.as_ref().and_then(|e| e.cell_w).filter(|&v| v > 0);
            return slice_auto_sheet(&path, frames_hint, cw_hint, self.target_w, self.target_h);
        }
        Vec::new()
    }
}

pub fn default_user_sprites_dir() -> PathBuf {
    if let Ok(xdg) = env::var("XDG_CONFIG_HOME") {
        let trimmed = xdg.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed).join("capri").join("sprites");
        }
    }
    let home = env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
    PathBuf::from(home).join(".config").join("capri").join("sprites")
}

pub fn resolve_bundled_sprites_dir(explicit: Option<&Path>) -> PathBuf {
    if let Some(dir) = explicit {
        return dir.to_path_buf();
    }
    if let Ok(env_dir) = env::var("CAPRI_SPRITES_DIR") {
        let trimmed = env_dir.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    // Check relative to executable (`rust/capri-avatar-rs/target/release/capri-avatar-rs`)
    if let Ok(exe) = env::current_exe() {
        for ancestor in exe.ancestors().skip(1).take(6) {
            let cand = ancestor.join("capri").join("assets").join("sprites_v2");
            if cand.is_dir() {
                return cand;
            }
        }
    }
    // Check current working directory
    if let Ok(cwd) = env::current_dir() {
        let cand = cwd.join("capri").join("assets").join("sprites_v2");
        if cand.is_dir() {
            return cand;
        }
    }
    PathBuf::from("/usr/share/capri-companion/sprites_v2")
}

fn decode_png_to_premul_pixmap(path: &Path) -> Option<Pixmap> {
    let file = File::open(path).ok()?;
    let mut decoder = png::Decoder::new(BufReader::new(file));
    decoder.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut reader = decoder.read_info().ok()?;
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).ok()?;
    let bytes = &buf[..info.buffer_size()];

    let mut pix = Pixmap::new(info.width, info.height)?;
    let dst = pix.pixels_mut();
    match info.color_type {
        png::ColorType::Rgba => {
            let (chunks, _) = bytes.as_chunks::<4>();
            for (px, chunk) in dst.iter_mut().zip(chunks) {
                *px = PremultipliedColorU8::from_rgba(
                    premul(chunk[0], chunk[3]),
                    premul(chunk[1], chunk[3]),
                    premul(chunk[2], chunk[3]),
                    chunk[3],
                )
                .unwrap_or(PremultipliedColorU8::TRANSPARENT);
            }
        }
        png::ColorType::Rgb => {
            let (chunks, _) = bytes.as_chunks::<3>();
            for (px, chunk) in dst.iter_mut().zip(chunks) {
                *px = PremultipliedColorU8::from_rgba(chunk[0], chunk[1], chunk[2], 255)
                    .unwrap_or(PremultipliedColorU8::TRANSPARENT);
            }
        }
        png::ColorType::GrayscaleAlpha => {
            let (chunks, _) = bytes.as_chunks::<2>();
            for (px, chunk) in dst.iter_mut().zip(chunks) {
                let v = premul(chunk[0], chunk[1]);
                *px = PremultipliedColorU8::from_rgba(v, v, v, chunk[1])
                    .unwrap_or(PremultipliedColorU8::TRANSPARENT);
            }
        }
        png::ColorType::Grayscale => {
            for (px, &g) in dst.iter_mut().zip(bytes.iter()) {
                *px = PremultipliedColorU8::from_rgba(g, g, g, 255)
                    .unwrap_or(PremultipliedColorU8::TRANSPARENT);
            }
        }
        png::ColorType::Indexed => return None,
    }
    Some(pix)
}

#[inline]
fn premul(c: u8, a: u8) -> u8 {
    ((c as u16 * a as u16 + 127) / 255) as u8
}

fn crop_and_scale_frame(
    src: &Pixmap,
    src_x: u32,
    crop_w: u32,
    crop_h: u32,
    target_w: u32,
    target_h: u32,
) -> Option<Pixmap> {
    if crop_w == 0 || crop_h == 0 {
        return None;
    }
    let scale = (target_w as f32 / crop_w as f32).min(target_h as f32 / crop_h as f32);
    let out_w = ((crop_w as f32 * scale).round() as u32).max(1);
    let out_h = ((crop_h as f32 * scale).round() as u32).max(1);

    let mut dst = Pixmap::new(out_w, out_h)?;
    dst.fill(Color::TRANSPARENT);
    let paint = PixmapPaint {
        opacity: 1.0,
        blend_mode: tiny_skia::BlendMode::Source,
        quality: FilterQuality::Bilinear,
    };
    let transform = Transform::from_scale(scale, scale)
        .pre_translate(-(src_x as f32), 0.0);
    dst.draw_pixmap(0, 0, src.as_ref(), &paint, transform, None);
    Some(dst)
}

fn slice_fixed_sheet(
    path: &Path,
    count: u32,
    cell_w: u32,
    target_w: u32,
    target_h: u32,
) -> Vec<Pixmap> {
    let Some(src) = decode_png_to_premul_pixmap(path) else {
        return Vec::new();
    };
    let ch = CELL_H.min(src.height());
    let mut frames = Vec::with_capacity(count as usize);
    for i in 0..count {
        let sx = i * cell_w;
        if sx >= src.width() {
            break;
        }
        let cw = cell_w.min(src.width() - sx);
        if let Some(frame) = crop_and_scale_frame(&src, sx, cw, ch, target_w, target_h) {
            frames.push(frame);
        }
    }
    frames
}

fn slice_auto_sheet(
    path: &Path,
    frames_hint: Option<u32>,
    cell_w_hint: Option<u32>,
    target_w: u32,
    target_h: u32,
) -> Vec<Pixmap> {
    let Some(src) = decode_png_to_premul_pixmap(path) else {
        return Vec::new();
    };
    let img_w = src.width();
    let img_h = src.height();
    let (count, cw) = if let Some(fh) = frames_hint {
        let cw = cell_w_hint.unwrap_or_else(|| (img_w / fh).max(1));
        (fh, cw)
    } else if let Some(cwh) = cell_w_hint {
        ((img_w / cwh).max(1), cwh)
    } else if img_w >= CELL_W && img_w % CELL_W == 0 {
        (img_w / CELL_W, CELL_W)
    } else if (img_w as f32) > (img_h as f32) * 1.4 {
        let est_cw = ((img_h as f32) * (CELL_W as f32 / CELL_H as f32))
            .round()
            .max(1.0);
        let cnt = ((img_w as f32) / est_cw).round().max(1.0) as u32;
        (cnt, (img_w / cnt).max(1))
    } else {
        (1, img_w)
    };

    let mut frames = Vec::with_capacity(count as usize);
    for i in 0..count {
        let sx = i * cw;
        if sx >= img_w {
            break;
        }
        let w = cw.min(img_w - sx);
        if let Some(frame) = crop_and_scale_frame(&src, sx, w, img_h, target_w, target_h) {
            frames.push(frame);
        }
    }
    frames
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preloads_only_idle_and_blink_from_bundled_sprites() {
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
        assert_eq!(store.cached_slot_count(), 2);
        assert_eq!(store.get_slot("idle").len(), 16);
        assert_eq!(store.get_slot("blink").len(), 4);
        assert_eq!(store.get_slot("mouth").len(), 5);
        assert_eq!(store.cached_slot_count(), 3);
    }
}
