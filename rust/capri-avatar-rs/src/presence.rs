//! Non-blocking Living Awareness poller for `capri-avatar-rs`:
//! - MPRIS media playback detection via `playerctl` (`perch_music` auto-pose + track caption)
//! - Automatic Game Mode / fullscreen window detection on Hyprland and Niri
//!
//! Runs in a single tiny background thread so the 60 Hz Wayland `poll()` loop never blocks.

use std::env;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::Duration;

const POLL_INTERVAL: Duration = Duration::from_millis(2500);
const INITIAL_DELAY: Duration = Duration::from_millis(300);

const CAPRI_OWN_APPS: &[&str] = &[
    "capri-cyberdeck",
    "capri-avatar",
    "capri-dashboard",
    "capri-quick-ask",
];

const GAME_APP_EXACT: &[&str] = &[
    "gamescope",
    "lutris",
    "heroic",
    "retroarch",
    "minecraft",
    "minecraft-launcher",
    "cs2",
    "dota2",
    "cyberpunk2077",
    "eldenring",
    "baldursgate3",
    "witcher3",
    "valorant",
    "osu!",
];

const GAME_APP_PREFIXES: &[&str] = &["steam_app_", "wine_", "proton_", "gamescope"];

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MediaPlaybackState {
    pub is_playing: bool,
    pub player: String,
    pub artist: String,
    pub title: String,
    pub track_label: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct GameModeState {
    pub active: bool,
    pub reason: &'static str,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PresenceSnapshot {
    pub media: MediaPlaybackState,
    pub game_mode: GameModeState,
}

/// Normalize playerctl `-a metadata --format "{{status}}\t{{playerName}}\t{{artist}}\t{{title}}"` output.
/// Prefers `Playing` over `Paused` identically to Python `capri/core/presence.py`.
pub fn parse_mpris_lines(raw: &str) -> MediaPlaybackState {
    let mut paused_candidate: Option<MediaPlaybackState> = None;

    for raw_line in raw.lines() {
        let line = raw_line.trim();
        if line.is_empty() {
            continue;
        }
        let mut parts = line.split('\t');
        let status = parts.next().unwrap_or("").trim().to_ascii_lowercase();
        let player_raw = parts.next().unwrap_or("").trim();
        let artist = parts.next().unwrap_or("").trim().to_string();
        let title = parts.next().unwrap_or("").trim().to_string();

        let player = player_raw
            .split('.')
            .next()
            .unwrap_or(player_raw)
            .trim()
            .to_string();

        let track_label = match (artist.is_empty(), title.is_empty()) {
            (false, false) => format!("{artist} — {title}"),
            (true, false) => title.clone(),
            (false, true) => artist.clone(),
            (true, true) => String::new(),
        };

        if status == "playing" {
            return MediaPlaybackState {
                is_playing: true,
                player,
                artist,
                title,
                track_label,
            };
        }
        if status == "paused" && paused_candidate.is_none() {
            paused_candidate = Some(MediaPlaybackState {
                is_playing: false,
                player,
                artist,
                title,
                track_label,
            });
        }
    }

    paused_candidate.unwrap_or_default()
}

pub fn query_mpris_playback() -> MediaPlaybackState {
    let out = Command::new("playerctl")
        .args([
            "-a",
            "metadata",
            "--format",
            "{{status}}\t{{playerName}}\t{{artist}}\t{{title}}",
        ])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output();

    match out {
        Ok(o) => {
            let stdout = String::from_utf8_lossy(&o.stdout);
            if !o.status.success() && stdout.trim().is_empty() {
                MediaPlaybackState::default()
            } else {
                parse_mpris_lines(&stdout)
            }
        }
        Err(_) => MediaPlaybackState::default(),
    }
}

pub fn is_game_window(app_id: &str) -> bool {
    let aid = app_id.trim().to_ascii_lowercase();
    if aid.is_empty() || CAPRI_OWN_APPS.contains(&aid.as_str()) {
        return false;
    }
    // Plain Steam desktop library/store in windowed mode is NOT a running game
    if aid == "steam" {
        return false;
    }
    if GAME_APP_EXACT.contains(&aid.as_str()) {
        return true;
    }
    if GAME_APP_PREFIXES.iter().any(|p| aid.starts_with(p)) {
        return true;
    }
    aid.ends_with(".exe")
}

pub fn evaluate_active_window_json(raw: &str) -> GameModeState {
    let Ok(val) = serde_json::from_str::<serde_json::Value>(raw) else {
        return GameModeState::default();
    };
    let Some(obj) = val.as_object() else {
        return GameModeState::default();
    };
    let app_id = obj
        .get("class")
        .or_else(|| obj.get("app_id"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim();

    let aid_low = app_id.to_ascii_lowercase();
    if CAPRI_OWN_APPS.contains(&aid_low.as_str()) {
        return GameModeState::default();
    }

    let is_fullscreen = obj
        .get("is_fullscreen")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
        || obj
            .get("fullscreen")
            .map(|v| v.as_bool().unwrap_or(false) || v.as_i64().unwrap_or(0) != 0)
            .unwrap_or(false)
        || obj
            .get("fullscreenClient")
            .map(|v| v.as_bool().unwrap_or(false) || v.as_i64().unwrap_or(0) != 0)
            .unwrap_or(false);

    let game_hit = is_game_window(app_id);
    if is_fullscreen {
        return GameModeState {
            active: true,
            reason: if game_hit {
                "fullscreen_game"
            } else {
                "fullscreen_app"
            },
        };
    }
    if game_hit {
        return GameModeState {
            active: true,
            reason: "windowed_game",
        };
    }
    GameModeState::default()
}

pub fn detect_game_mode_auto() -> GameModeState {
    if env::var_os("HYPRLAND_INSTANCE_SIGNATURE").is_some() {
        if let Ok(out) = Command::new("hyprctl")
            .args(["activewindow", "-j"])
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
        {
            if out.status.success() {
                return evaluate_active_window_json(&String::from_utf8_lossy(&out.stdout));
            }
        }
    } else if env::var_os("NIRI_SOCKET").is_some() {
        if let Ok(out) = Command::new("niri")
            .args(["msg", "-j", "focused-window"])
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
        {
            if out.status.success() {
                return evaluate_active_window_json(&String::from_utf8_lossy(&out.stdout));
            }
        }
    }
    GameModeState::default()
}

pub struct PresencePoller {
    rx: mpsc::Receiver<PresenceSnapshot>,
    stop: Arc<AtomicBool>,
    auto_poses_enabled: Arc<AtomicBool>,
    game_mode_poll_enabled: Arc<AtomicBool>,
}

impl PresencePoller {
    pub fn spawn(auto_poses_enabled: bool, game_mode_poll_enabled: bool) -> Self {
        let (tx, rx) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        let auto_poses = Arc::new(AtomicBool::new(auto_poses_enabled));
        let game_poll = Arc::new(AtomicBool::new(game_mode_poll_enabled));

        let stop_bg = Arc::clone(&stop);
        let auto_bg = Arc::clone(&auto_poses);
        let game_bg = Arc::clone(&game_poll);

        let _ = thread::Builder::new()
            .name("capri-presence".to_string())
            .spawn(move || {
                thread::sleep(INITIAL_DELAY);
                while !stop_bg.load(Ordering::Relaxed) {
                    let media = if auto_bg.load(Ordering::Relaxed) {
                        query_mpris_playback()
                    } else {
                        MediaPlaybackState::default()
                    };
                    let game_mode = if game_bg.load(Ordering::Relaxed) {
                        detect_game_mode_auto()
                    } else {
                        GameModeState::default()
                    };
                    if tx.send(PresenceSnapshot { media, game_mode }).is_err() {
                        break;
                    }
                    // Sleep in small slices so shutdown is responsive
                    for _ in 0..25 {
                        if stop_bg.load(Ordering::Relaxed) {
                            return;
                        }
                        thread::sleep(POLL_INTERVAL / 25);
                    }
                }
            });

        Self {
            rx,
            stop,
            auto_poses_enabled: auto_poses,
            game_mode_poll_enabled: game_poll,
        }
    }

    pub fn set_flags(&self, auto_poses_enabled: bool, game_mode_poll_enabled: bool) {
        self.auto_poses_enabled
            .store(auto_poses_enabled, Ordering::Relaxed);
        self.game_mode_poll_enabled
            .store(game_mode_poll_enabled, Ordering::Relaxed);
    }

    /// Drain all queued snapshots and return the latest one, if any.
    pub fn poll_latest(&self) -> Option<PresenceSnapshot> {
        let mut latest = None;
        while let Ok(snap) = self.rx.try_recv() {
            latest = Some(snap);
        }
        latest
    }
}

impl Drop for PresencePoller {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_mpris_lines_and_prefers_playing_over_paused() {
        let raw = "Paused\tchromium.instance123\tYouTube\tLo-Fi\n\
                   Playing\tspotify\tsekairotten\t46с\n";
        let st = parse_mpris_lines(raw);
        assert!(st.is_playing);
        assert_eq!(st.player, "spotify");
        assert_eq!(st.artist, "sekairotten");
        assert_eq!(st.title, "46с");
        assert_eq!(st.track_label, "sekairotten — 46с");

        let paused = parse_mpris_lines("Paused\tspotify\tJilt\tClarion\n");
        assert!(!paused.is_playing);
        assert_eq!(paused.track_label, "Jilt — Clarion");
    }

    #[test]
    fn detects_game_windows_and_ignores_capri_and_steam_library() {
        assert!(is_game_window("steam_app_1091500"));
        assert!(is_game_window("Cyberpunk2077.exe"));
        assert!(is_game_window("gamescope"));
        assert!(!is_game_window("steam"));
        assert!(!is_game_window("capri-cyberdeck"));
        assert!(!is_game_window("kitty"));
    }

    #[test]
    fn evaluates_hyprland_and_niri_active_window_fullscreen_and_games() {
        let hypr_fs = r#"{"class": "mpv", "title": "movie", "fullscreen": 2, "fullscreenClient": 2}"#;
        let gm_fs = evaluate_active_window_json(hypr_fs);
        assert!(gm_fs.active);
        assert_eq!(gm_fs.reason, "fullscreen_app");

        let hypr_game = r#"{"class": "steam_app_1091500", "title": "Cyberpunk 2077", "fullscreen": 1}"#;
        let gm_game = evaluate_active_window_json(hypr_game);
        assert!(gm_game.active);
        assert_eq!(gm_game.reason, "fullscreen_game");

        let hypr_normal = r#"{"class": "kitty", "title": "zsh", "fullscreen": 0}"#;
        assert!(!evaluate_active_window_json(hypr_normal).active);

        let capri_own = r#"{"class": "capri-cyberdeck", "fullscreen": 1}"#;
        assert!(!evaluate_active_window_json(capri_own).active);
    }
}
