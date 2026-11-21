"""
Wave 3 (v2.9.0 — Living Presence 2.0) Regression Tests:
- Echo-resistant Hands-Free Barge-In DSP detector (`BargeInDetector`):
  grace period rejection, adaptive noise floor, click rejection, sustained speech trigger + pre-buffer
- Speech-amplitude lip-sync envelope (`compute_speech_envelope`): real WAV PCM amplitude + MP3 phoneme cadence
- Upgraded `AvatarV2Canvas`: all 10 sprite sheets loaded, envelope-driven mouth articulation,
  mood-aware holographic aura, micro-glitch blinks, sleep -> wake transition, perch / perch_music poses
- Cyberdeck Barge-In settings persistence, `/bargein` command, and live interrupt flow
"""

from __future__ import annotations

import math
import wave
from unittest.mock import MagicMock, patch

import numpy as np
from PySide6.QtWidgets import QApplication, QWidget

from capri.core.config import AppConfig
from capri.ui.cyberdeck import CapriCyberdeckWindow, CapriProviderSettingsDialog
from capri.ui.sprite_view import AvatarV2Canvas
from capri.voice.barge_in import BargeInDetector
from capri.voice.tts import compute_speech_envelope


def _make_voice_chunk(freq_hz: float = 280.0, amplitude: float = 0.12, n_samples: int = 1600, sr: int = 16000) -> np.ndarray:
    t = np.arange(n_samples, dtype=np.float32) / float(sr)
    return (amplitude * np.sin(2.0 * math.pi * freq_hz * t)).astype(np.float32)


def test_barge_in_detector_echo_rejection_grace_period_and_confirmation():
    det = BargeInDetector(
        base_rms_threshold=0.032,
        required_consecutive=3,
        grace_period_sec=0.45,
        pre_buffer_frames=5,
    )
    det.reset(now=100.0)

    loud_voice = _make_voice_chunk(freq_hz=260.0, amplitude=0.14)
    quiet_echo = _make_voice_chunk(freq_hz=300.0, amplitude=0.015)

    # 1. During startup grace period (t < 100.45), even loud onset must NOT trigger
    assert det.feed(loud_voice, now=100.10) is False
    assert det.feed(loud_voice, now=100.20) is False
    assert det.feed(loud_voice, now=100.30) is False
    assert det.triggered is False

    # 2. Quiet speaker echo / room noise after grace period must NOT trigger
    for idx in range(5):
        assert det.feed(quiet_echo, now=100.50 + idx * 0.1) is False
    assert det.triggered is False

    # 3. Single sharp impulsive click (1 frame only) must NOT trigger
    assert det.feed(loud_voice, now=101.10) is False
    assert det.feed(quiet_echo, now=101.20) is False
    assert det.triggered is False

    # 4. High-frequency non-voice static burst (ZCR > 0.5) must NOT trigger
    high_zcr = np.array([0.2 if i % 2 == 0 else -0.2 for i in range(1600)], dtype=np.float32)
    for idx in range(4):
        assert det.feed(high_zcr, now=101.30 + idx * 0.1) is False
    assert det.triggered is False

    # 5. Sustained voice speech over 3 consecutive frames MUST trigger & preserve pre-buffer
    assert det.feed(loud_voice, now=102.00) is False
    assert det.feed(loud_voice, now=102.10) is False
    assert det.feed(loud_voice, now=102.20) is True
    assert det.triggered is True
    pre = det.take_pre_buffer()
    assert len(pre) >= 3


def test_speech_envelope_from_wav_and_phoneme_cadence(tmp_path):
    # 1. Real 16kHz WAV file: 0.2s silence + 0.3s loud tone + 0.2s silence
    wav_path = tmp_path / "sample.wav"
    sr = 16000
    silence = np.zeros(int(sr * 0.2), dtype=np.int16)
    tone_f = _make_voice_chunk(freq_hz=320.0, amplitude=0.6, n_samples=int(sr * 0.3), sr=sr)
    tone = (tone_f * 32767.0).astype(np.int16)
    pcm = np.concatenate([silence, tone, silence])
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())

    wav_env = compute_speech_envelope(str(wav_path), phrase="", fps=15)
    assert len(wav_env) >= 8
    assert wav_env[0] == 0
    assert max(wav_env) == 4
    assert wav_env[-1] == 0

    # 2. Phoneme cadence fallback for MP3 / text phrases
    mp3_env = compute_speech_envelope(None, phrase="Привет, D! Всё работает чисто.")
    assert len(mp3_env) >= 10
    assert all(0 <= v <= 4 for v in mp3_env)
    assert 4 in mp3_env and 0 in mp3_env


def test_avatar_v2_canvas_all_sprites_lip_sync_mood_aura_and_sleep_wake():
    app = QApplication.instance() or QApplication([])

    class _DummyHost(QWidget):
        def __init__(self):
            super().__init__()
            self.state = "idle"
            self.caption_text = "Живое присутствие 2.0"
            self.resize(300, 420)

    host = _DummyHost()
    canvas = AvatarV2Canvas(host, target_w=220, target_h=352)
    canvas.resize(300, 420)

    # All 10 packaged sprite sheets must load cleanly
    assert len(canvas.idle_frames) == 16
    assert len(canvas.blink_frames) == 4
    assert len(canvas.blink_glitch_frames) == 4
    assert len(canvas.mouth_frames) == 5
    assert len(canvas.glitch_frames) == 10
    assert len(canvas.dance_frames) == 16
    assert len(canvas.sleep_frames) == 16
    assert len(canvas.wake_frames) == 13
    assert len(canvas.perch_frames) == 60
    assert len(canvas.perch_music_frames) == 60

    # Mood aura shifts cleanly
    canvas.set_mood("sarcastic")
    assert canvas.mood == "sarcastic"

    # Envelope-driven lip-sync advances during speaking
    host.state = "speaking"
    canvas.set_lip_sync_envelope([0, 4, 2, 0])
    seen_mouth = []
    for _ in range(16):
        canvas.advance_animation()
        seen_mouth.append(canvas.current_mouth_idx)
    assert 4 in seen_mouth and 2 in seen_mouth

    # Poses & sleep -> wake smooth transition
    host.state = "idle"
    canvas.set_pose("perch_music")
    assert canvas.pose == "perch_music"
    canvas.set_pose("sleep")
    assert canvas.pose == "sleep"
    canvas.wake_up()
    assert canvas.pose == "normal"
    assert canvas.waking_active is True

    # Renders cleanly offscreen without error
    pm = canvas.grab()
    assert not pm.isNull()
    assert pm.width() == 300 and pm.height() == 420
    host.close()


def test_cyberdeck_barge_in_setting_command_and_interrupt_flow():
    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.voice.hands_free_barge_in = True
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.save_config"), patch("capri.ui.cyberdeck.update_env_value"):
        dlg = CapriProviderSettingsDialog(cfg)
        assert dlg.chk_hands_free_barge_in.isChecked() is True
        dlg.chk_hands_free_barge_in.setChecked(False)
        dlg._apply_and_save()
        assert cfg.voice.hands_free_barge_in is False
        dlg.close()

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True), \
         patch("capri.ui.cyberdeck.save_config"):
        deck = CapriCyberdeckWindow(config=cfg)
        deck._handle_command("/bargein on")
        assert deck.config.voice.hands_free_barge_in is True

        deck.agent.tts.interrupt = MagicMock()
        deck._on_barge_in_triggered()
        deck.agent.tts.interrupt.assert_called_once()
        assert deck.visualizer.state == "listening"

        deck._handle_command("/bargein off")
        assert deck.config.voice.hands_free_barge_in is False
        deck.close()


def test_avatar_v2_dynamic_multiline_speech_bubble_height():
    """AvatarV2Canvas speech bubble must dynamically grow for 3-line captions without clipping."""
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QWidget
    from capri.ui.sprite_view import AvatarV2Canvas

    app = QApplication.instance() or QApplication([])

    class _Host(QWidget):
        def __init__(self):
            super().__init__()
            self.state = "speaking"
            self.caption_text = ""
            self.resize(300, 420)

    host = _Host()
    canvas = AvatarV2Canvas(host)
    canvas.resize(300, 420)
    font = QFont("Inter", 9, QFont.Weight.Medium)

    short_rect = canvas._compute_speech_bubble_rect("Привет, D!", font, anchor_y=78.0)
    long_caption = (
        "А вот у тебя в песочнице разброс: Spotify на втором воркспейсе, "
        "TrueSlash с каким-то слэшем…"
    )
    long_rect = canvas._compute_speech_bubble_rect(long_caption, font, anchor_y=78.0)

    assert long_rect.height() > short_rect.height()
    assert long_rect.height() >= 54.0
    assert long_rect.width() <= canvas.width() - 14.0
    assert long_rect.top() >= 4.0

    host.caption_text = long_caption
    pm = canvas.grab()
    assert not pm.isNull()
    assert pm.width() == 300 and pm.height() == 420
    host.close()
