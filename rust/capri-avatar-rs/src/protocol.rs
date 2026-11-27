//! Versioned (`v: 1`) NDJSON IPC protocol parser for `capri-avatar-rs`.
//!
//! Mirrors `capri/ui/avatar_ipc.py` and `capri/ui/overlay.py::_dispatch_ipc_message`
//! so the Python Cyberdeck / CLI / agent talk to the Rust avatar seamlessly.

use serde_json::{Map, Value};

pub const AVATAR_IPC_SCHEMA_VERSION: i64 = 1;
pub const AVATAR_IPC_MAX_LINE_BYTES: usize = 65_536;

#[derive(Debug, Clone, PartialEq)]
pub enum IpcCommand {
    StateUpdate {
        state: Option<String>,
        text: Option<String>,
        pose: Option<String>,
        mood: Option<String>,
        mouth_envelope: Option<Vec<u8>>,
    },
    AudioLevel {
        level: f32,
        source: String,
    },
    Locomotion {
        action: String,
        target_x: Option<f64>,
        target_y: Option<f64>,
        enabled: Option<bool>,
    },
    PowerSaver {
        active: Option<bool>,
        override_set: bool,
        override_val: Option<bool>,
        reason: String,
    },
    ConfigSync {
        reduced_motion: Option<bool>,
        physics_enabled: Option<bool>,
        game_mode_enabled: Option<bool>,
        auto_avatar_poses: Option<bool>,
        reactions_enabled: Option<bool>,
        autonomous_life_enabled: Option<bool>,
        ui_language: Option<String>,
    },
    FocusTimer {
        action: String,
        minutes: Option<f64>,
        seconds: Option<f64>,
    },
    Quit,
    ReloadSprites,
}

/// Validate and decode one NDJSON line into a typed `IpcCommand`.
/// Accepts schema `v == 1` and legacy unversioned JSON objects (no `"v"` key);
/// rejects oversized lines, invalid JSON, non-object payloads, and unsupported versions.
pub fn decode_ipc_line(raw_line: &[u8]) -> Option<IpcCommand> {
    if raw_line.is_empty() || raw_line.len() > AVATAR_IPC_MAX_LINE_BYTES {
        return None;
    }
    let text = String::from_utf8_lossy(raw_line);
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return None;
    }
    let val: Value = serde_json::from_str(trimmed).ok()?;
    let obj = val.as_object()?;

    if let Some(v) = obj.get("v") {
        let ver = v.as_i64()?;
        if !(1..=AVATAR_IPC_SCHEMA_VERSION).contains(&ver) {
            return None;
        }
    }

    parse_command_object(obj)
}

fn parse_command_object(obj: &Map<String, Value>) -> Option<IpcCommand> {
    let msg_type = obj
        .get("type")
        .and_then(Value::as_str)
        .map(|s| s.trim().to_ascii_lowercase());
    let action_str = obj
        .get("action")
        .and_then(Value::as_str)
        .map(|s| s.trim().to_ascii_lowercase());

    if matches!(msg_type.as_deref(), Some("control")) {
        if let Some(cmd) = obj
            .get("command")
            .or_else(|| obj.get("cmd"))
            .and_then(Value::as_str)
        {
            match cmd.trim().to_ascii_lowercase().as_str() {
                "quit" | "exit" | "shutdown" => return Some(IpcCommand::Quit),
                "reload_sprites" => return Some(IpcCommand::ReloadSprites),
                _ => {}
            }
        }
    }
    if matches!(action_str.as_deref(), Some("quit")) {
        return Some(IpcCommand::Quit);
    }

    if msg_type.as_deref() == Some("locomotion")
        || matches!(
            action_str.as_deref(),
            Some("come" | "stay" | "follow" | "home" | "glide" | "teleport")
        )
    {
        return Some(IpcCommand::Locomotion {
            action: action_str.unwrap_or_else(|| "home".to_string()),
            target_x: obj.get("target_x").and_then(Value::as_f64),
            target_y: obj.get("target_y").and_then(Value::as_f64),
            enabled: obj.get("enabled").and_then(Value::as_bool),
        });
    }

    if msg_type.as_deref() == Some("audio_level") {
        let level = obj
            .get("level")
            .and_then(Value::as_f64)
            .unwrap_or(0.0)
            .clamp(0.0, 1.0) as f32;
        let source = obj
            .get("source")
            .and_then(Value::as_str)
            .unwrap_or("mic")
            .to_string();
        return Some(IpcCommand::AudioLevel { level, source });
    }

    if msg_type.as_deref() == Some("power_saver") {
        let override_set = obj.contains_key("override");
        let override_val = obj.get("override").and_then(Value::as_bool);
        let active = obj.get("active").and_then(Value::as_bool);
        let reason = obj
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("ipc")
            .to_string();
        return Some(IpcCommand::PowerSaver {
            active,
            override_set,
            override_val,
            reason,
        });
    }

    if msg_type.as_deref() == Some("config_sync") {
        return Some(IpcCommand::ConfigSync {
            reduced_motion: obj.get("reduced_motion").and_then(Value::as_bool),
            physics_enabled: obj
                .get("physics_enabled")
                .or_else(|| obj.get("avatar_physics_enabled"))
                .and_then(Value::as_bool),
            game_mode_enabled: obj.get("game_mode_enabled").and_then(Value::as_bool),
            auto_avatar_poses: obj.get("auto_avatar_poses").and_then(Value::as_bool),
            reactions_enabled: obj.get("avatar_reactions_enabled").and_then(Value::as_bool),
            autonomous_life_enabled: obj
                .get("autonomous_life_enabled")
                .or_else(|| obj.get("avatar_autonomous_life"))
                .and_then(Value::as_bool),
            ui_language: obj
                .get("ui_language")
                .and_then(Value::as_str)
                .map(|s| s.trim().to_ascii_lowercase()),
        });
    }

    if msg_type.as_deref() == Some("focus_timer") {
        let action = action_str.unwrap_or_else(|| "cycle".to_string());
        return Some(IpcCommand::FocusTimer {
            action,
            minutes: obj.get("minutes").and_then(Value::as_f64),
            seconds: obj.get("seconds").and_then(Value::as_f64),
        });
    }

    let meta = obj.get("metadata").and_then(Value::as_object);
    let state = obj
        .get("state")
        .and_then(Value::as_str)
        .map(|s| s.trim().to_ascii_lowercase());
    let text = obj
        .get("text")
        .and_then(Value::as_str)
        .map(ToString::to_string);
    let pose = obj
        .get("pose")
        .or_else(|| meta.and_then(|m| m.get("pose")))
        .and_then(Value::as_str)
        .map(|s| s.trim().to_ascii_lowercase());
    let mood = meta
        .and_then(|m| m.get("mood"))
        .and_then(Value::as_str)
        .map(|s| s.trim().to_ascii_lowercase());
    let mouth_envelope = meta
        .and_then(|m| m.get("mouth_envelope"))
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_i64().map(|n| n.clamp(0, 4) as u8))
                .collect::<Vec<u8>>()
        });

    Some(IpcCommand::StateUpdate {
        state,
        text,
        pose,
        mood,
        mouth_envelope,
    })
}

/// Per-connection NDJSON framing buffer with strict per-line byte limits.
#[derive(Debug)]
pub struct IpcStreamBuffer {
    max_line_bytes: usize,
    buf: Vec<u8>,
    discarding_oversized: bool,
}

impl Default for IpcStreamBuffer {
    fn default() -> Self {
        Self::new(AVATAR_IPC_MAX_LINE_BYTES)
    }
}

impl IpcStreamBuffer {
    pub fn new(max_line_bytes: usize) -> Self {
        Self {
            max_line_bytes: max_line_bytes.max(256),
            buf: Vec::new(),
            discarding_oversized: false,
        }
    }

    pub fn feed(&mut self, chunk: &[u8]) -> Vec<IpcCommand> {
        let mut out = Vec::new();
        let mut pos = 0usize;
        while pos < chunk.len() {
            let nl_rel = chunk[pos..].iter().position(|&b| b == b'\n');
            if self.discarding_oversized {
                match nl_rel {
                    None => return out,
                    Some(rel) => {
                        self.discarding_oversized = false;
                        pos += rel + 1;
                        continue;
                    }
                }
            }

            match nl_rel {
                None => {
                    let rem = chunk.len() - pos;
                    if self.buf.len() + rem > self.max_line_bytes {
                        self.buf.clear();
                        self.discarding_oversized = true;
                        return out;
                    }
                    self.buf.extend_from_slice(&chunk[pos..]);
                    break;
                }
                Some(rel) => {
                    let end = pos + rel;
                    if self.buf.len() + rel > self.max_line_bytes {
                        self.buf.clear();
                        pos = end + 1;
                        continue;
                    }
                    let decoded = if self.buf.is_empty() {
                        decode_ipc_line(&chunk[pos..end])
                    } else {
                        self.buf.extend_from_slice(&chunk[pos..end]);
                        let res = decode_ipc_line(&self.buf);
                        self.buf.clear();
                        res
                    };
                    if let Some(cmd) = decoded {
                        out.push(cmd);
                    }
                    pos = end + 1;
                }
            }
        }
        out
    }

    pub fn flush_eof(&mut self) -> Option<IpcCommand> {
        if self.discarding_oversized || self.buf.is_empty() {
            self.buf.clear();
            self.discarding_oversized = false;
            return None;
        }
        let res = decode_ipc_line(&self.buf);
        self.buf.clear();
        res
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_unsupported_versions_and_non_objects() {
        assert!(decode_ipc_line(br#"{"v": 2, "state": "idle"}"#).is_none());
        assert!(decode_ipc_line(br#"{"v": 0, "state": "idle"}"#).is_none());
        assert!(decode_ipc_line(br#"{"v": true, "state": "idle"}"#).is_none());
        assert!(decode_ipc_line(br#"[1, 2, 3]"#).is_none());
        assert!(decode_ipc_line(br#"not json"#).is_none());
    }

    #[test]
    fn parses_v1_and_legacy_payloads() {
        let cmd = decode_ipc_line(
            r#"{"v":1,"state":"speaking","text":"Привет, D!","metadata":{"mood":"playful","mouth_envelope":[0,2,4,1]}}"#.as_bytes(),
        )
        .expect("valid v1 state update");
        assert_eq!(
            cmd,
            IpcCommand::StateUpdate {
                state: Some("speaking".into()),
                text: Some("Привет, D!".into()),
                pose: None,
                mood: Some("playful".into()),
                mouth_envelope: Some(vec![0, 2, 4, 1]),
            }
        );

        let legacy = decode_ipc_line(br#"{"type":"audio_level","level":0.75,"source":"tts"}"#)
            .expect("valid legacy audio_level");
        assert_eq!(
            legacy,
            IpcCommand::AudioLevel {
                level: 0.75,
                source: "tts".into(),
            }
        );
    }

    #[test]
    fn stream_buffer_reassembles_chunks_and_recovers_from_oversized_line() {
        let mut buf = IpcStreamBuffer::new(256);
        assert!(buf.feed(br#"{"v":1,"state":"#).is_empty());
        let msgs = buf.feed(br#""thinking","text":"ok"}"#);
        assert!(msgs.is_empty());
        let flushed = buf.feed(b"\n");
        assert_eq!(flushed.len(), 1);

        // Oversized line is discarded cleanly without breaking the next frame
        let huge = vec![b'x'; 400];
        assert!(buf.feed(&huge).is_empty());
        let after = buf.feed(b"\n{\"v\":1,\"type\":\"focus_timer\",\"action\":\"start\",\"minutes\":25}\n");
        assert_eq!(
            after,
            vec![IpcCommand::FocusTimer {
                action: "start".into(),
                minutes: Some(25.0),
                seconds: None,
            }]
        );
    }
}
