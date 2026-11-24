"""
Capri v3.6 Stage 2 — Dev & Focus Companion Tests:
1. `FocusTimerController` preset cycle (`OFF -> 25m -> 50m -> 15m -> OFF`), progress ratio, `mm:ss`
   formatting, silence gate (`is_silent_gate_active`), and exact one-shot completion tick.
2. `CapriAvatarWidget` + `CapriAvatarQuickPopover` Focus Timer UI integration:
   - Popover row cycles presets (`FOCUS 25:00`),
   - `AvatarV2Canvas` renders soft holographic Focus progress ring under Capri's heels,
   - `_tick_autonomous_brain` suppresses unsolicited awareness chatter and autonomous walks while
     the Focus timer is active,
   - Completion tick clears the aura, lifts the silence gate, and displays completion caption.
3. Cyberdeck `/pomodoro` (`/focus-timer`) slash command + `focus_gate` IPC silencing `InitiativeEngine`.
4. `capri --watch` (`watch_runner` + `cli_main`):
   - CLI parsing (`--watch "cmd"`, `--watch -- cmd args`, `--no-voice`, `--no-notify`),
   - Exit-code propagation (`0` on success, non-zero on failure),
   - Bilingual avatar + TTS completion/failure alert generation,
   - `SIGINT` (`KeyboardInterrupt`) clean forwarding without unwanted TTS speech.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from capri.cli import cli_main
from capri.core.config import AppConfig
from capri.core.focus_timer import FocusTimerController
from capri.core.memory import MemoryStore
from capri.core.sessions import SessionManager
from capri.core.watch_runner import (
    WatchResult,
    build_watch_alert_messages,
    format_duration_human,
    parse_watch_cli_args,
    run_watched_command,
)
from capri.ui.client import send_cyberdeck_command
from capri.ui.cyberdeck import CapriCyberdeckWindow
from capri.ui.overlay import CapriAvatarWidget
from capri.ui.sprite_view import AvatarV2Canvas


def _ensure_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("CAPRI_DISABLE_LAYER_SHELL", "1")
    return QApplication.instance() or QApplication([])


def test_focus_timer_controller_preset_cycle_progress_and_completion():
    ft = FocusTimerController()
    assert ft.active is False
    assert ft.is_silent_gate_active is False
    assert ft.snapshot(now=100.0).mm_ss == "OFF"

    # Cycle 1 -> 25m
    s1 = ft.cycle_preset(now=100.0)
    assert s1.active is True
    assert s1.preset_minutes == 25
    assert s1.mm_ss == "25:00"
    assert ft.is_silent_gate_active is True

    # Halfway through 25m (750s elapsed)
    mid = ft.snapshot(now=850.0)
    assert abs(mid.progress - 0.5) < 1e-3
    assert mid.mm_ss == "12:30"
    assert ft.tick(now=850.0) is False

    # Cycle 2 -> 50m, Cycle 3 -> 15m, Cycle 4 -> OFF
    assert ft.cycle_preset(now=850.0).preset_minutes == 50
    assert ft.cycle_preset(now=850.0).preset_minutes == 15
    assert ft.cycle_preset(now=850.0).active is False

    # Short custom seconds -> exact one-shot completion
    ft.start_seconds(10.0, now=200.0)
    assert ft.tick(now=209.5) is False
    assert ft.tick(now=210.0) is True
    assert ft.active is False
    assert ft.tick(now=211.0) is False


def test_avatar_focus_aura_silence_gate_and_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CAPRI_DISABLE_LAYER_SHELL", "1")
    _ensure_app()

    # 1. Verify Focus progress ring renders additional lilac pixels in base aura zone
    host = QWidget()
    host.state = "idle"
    host.caption_text = ""
    canvas = AvatarV2Canvas(host, user_sprites_dir=tmp_path)
    canvas.resize(300, 420)

    def _render_lilac_score() -> int:
        img = QImage(300, 420, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        canvas.render_to_painter(p, 300, 420)
        p.end()
        score = 0
        for y in range(355, 415, 2):
            for x in range(85, 235, 2):
                c = img.pixelColor(x, y)
                if c.alpha() >= 55 and c.blue() > c.green():
                    score += 1
        return score

    base_score = _render_lilac_score()
    canvas.set_focus_visual(True, progress=0.25)
    focus_score = _render_lilac_score()
    assert focus_score > base_score
    host.close()

    # 2. Full CapriAvatarWidget popover cycle + autonomous brain silence gate + completion bubble
    sent_deck: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "capri.ui.client.send_cyberdeck_command",
        lambda cmd, **kw: (sent_deck.append((cmd, kw.get("extra") or {})) or True),
    )

    widget = CapriAvatarWidget(mode="2d")
    try:
        widget.quick_popover.row_focus_timer.click()
        assert widget.focus_timer.active is True
        assert widget.quick_popover.lbl_state.text().startswith("FOCUS ")
        assert widget.canvas_v2.focus_active is True
        assert sent_deck and sent_deck[-1][0] == "focus_gate"
        assert sent_deck[-1][1]["seconds"] > 1400

        # Force awareness return-greeting precondition & brain walk precondition: both must stay silent!
        widget.awareness._away_since = time.time() - 3600.0
        widget.caption_text = ""
        widget._auto_walk_target = None
        widget._tick_autonomous_brain()
        assert widget.caption_text == ""
        assert widget._auto_walk_target is None

        # Simulate countdown reaching 0 -> completion caption & focus_active cleared
        widget._start_focus_timer_seconds(1.0)
        widget.focus_timer._end_mono = time.monotonic() - 0.1
        widget._on_focus_timer_tick()
        assert widget.focus_timer.active is False
        assert widget.canvas_v2.focus_active is False
        assert "⏱" in widget.caption_text
    finally:
        widget.close()


def test_cyberdeck_pomodoro_command_and_focus_gate_ipc(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = _ensure_app()

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False
    cfg.companion.proactive_enabled = True

    avatar_payloads: list[dict] = []
    with (
        patch("capri.ui.cyberdeck.notify_avatar", return_value=True),
        patch("capri.ui.cyberdeck.send_avatar_control", side_effect=lambda p: (avatar_payloads.append(p) or True)),
    ):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = SessionManager(db_path=tmp_path / "pomo.db")
        deck.agent.memory_store = MemoryStore(file_path=tmp_path / "pomo.json")

        # Start 25m focus via slash command
        deck._handle_command("/pomodoro 25")
        assert deck._focus_gate_until > time.monotonic() + 1400
        assert "Focus" in deck.lbl_status_chip.text()
        assert avatar_payloads[-1] == {"type": "focus_timer", "action": "start", "minutes": 25.0}

        # Proactive initiative evaluation must NOT be called while focus gate is active
        eval_mock = MagicMock()
        deck.agent.initiative_engine.evaluate = eval_mock
        deck._check_initiative_tick()
        eval_mock.assert_not_called()

        # Stop via IPC focus_gate seconds=0
        sock_path = str(tmp_path / "deck-focus.sock")
        assert deck.start_ipc_server(sock_path) is True
        t = threading.Thread(
            target=lambda: send_cyberdeck_command("focus_gate", socket_path=sock_path, extra={"seconds": 0.0})
        )
        t.start()
        for _ in range(30):
            app.processEvents()
            if not t.is_alive():
                break
            time.sleep(0.01)
        t.join(timeout=1.0)
        assert deck._focus_gate_until == 0.0
        deck.close()


def test_pomodoro_retries_until_cold_starting_avatar_socket_is_ready(monkeypatch, tmp_path):
    """`/pomodoro` right after spawning the avatar must not lose the timer if the IPC socket isn't up yet."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = _ensure_app()

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    attempts: list[dict] = []
    socket_ready_after = 3  # first 3 sends fail (socket not created yet), 4th succeeds

    def _flaky_send(payload):
        attempts.append(dict(payload))
        return len(attempts) > socket_ready_after

    with (
        patch("capri.ui.cyberdeck.notify_avatar", return_value=False),
        patch("capri.ui.cyberdeck.send_avatar_control", side_effect=_flaky_send),
    ):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = SessionManager(db_path=tmp_path / "retry.db")
        deck.agent.memory_store = MemoryStore(file_path=tmp_path / "retry.json")
        deck._AVATAR_CONTROL_RETRY_MS = 5
        deck._ensure_avatar_running = lambda: None  # do not actually spawn a process in tests

        deck._handle_command("/pomodoro 25")
        for _ in range(80):
            app.processEvents()
            if len(attempts) > socket_ready_after:
                break
            time.sleep(0.01)

        assert len(attempts) == socket_ready_after + 1
        assert all(p == {"type": "focus_timer", "action": "start", "minutes": 25.0} for p in attempts)

        # A newer command supersedes pending retries: `/pomodoro off` must cancel a still-retrying `start`
        attempts.clear()
        socket_ready_after = 10_000  # socket never ready during this phase
        deck._handle_command("/pomodoro 50")
        deck._handle_command("/pomodoro off")
        for _ in range(30):
            app.processEvents()
            time.sleep(0.005)
        started = [p for p in attempts if p.get("action") == "start"]
        stopped = [p for p in attempts if p.get("action") == "stop"]
        # `start` was attempted once immediately, then superseded — never retried after `off`
        assert len(started) == 1
        assert len(stopped) >= 1
        deck.close()


def test_watch_runner_cli_exit_codes_alerts_and_sigint():
    # 1. Argument parser supports both single shell string (with leading or trailing --no-voice) and `--` exec
    toks, sh, nv, nn = parse_watch_cli_args(["--watch", "make -j4", "--no-voice"])
    assert (toks, sh, nv, nn) == (["make -j4"], True, True, False)

    toks2, sh2, nv2, _ = parse_watch_cli_args(["--watch", "--", sys.executable, "-c", "print(1)", "--no-voice"])
    assert (toks2, sh2, nv2) == ([sys.executable, "-c", "print(1)", "--no-voice"], False, False)

    assert format_duration_human(12.34, english=True) == "12.3s"
    assert format_duration_human(125.0, english=False) == "2м 5с"

    # 2. Alert builder produces playful on 0, thinking on non-zero, and silent idle on Ctrl+C
    ok_state, ok_cap, _, ok_tts = build_watch_alert_messages(
        WatchResult("cargo build", 0, 8.4, False), user_name="D", english=True
    )
    assert ok_state == "playful" and "✅" in ok_cap and "cargo" in ok_tts

    err_state, err_cap, _, err_tts = build_watch_alert_messages(
        WatchResult("pytest -q", 1, 3.2, False), user_name="D", english=False
    )
    assert err_state == "thinking" and "⚠" in err_cap and "ошибкой 1" in err_tts

    int_state, _, _, int_tts = build_watch_alert_messages(
        WatchResult("sleep 99", 130, 1.0, True), user_name="D", english=True
    )
    assert int_state == "idle" and int_tts == ""

    # 3. End-to-end cli_main(["--watch", ...]) propagates exact exit code & invokes TTS when enabled
    cfg = AppConfig()
    cfg.voice.tts_enabled = True
    speak_mock = AsyncMock()
    av_calls: list[tuple[str, str]] = []

    with (
        patch("capri.core.watch_runner.load_config", return_value=cfg),
        patch("capri.core.watch_runner.notify_avatar", side_effect=lambda st, tx="": (av_calls.append((st, tx)) or True)),
        patch("capri.voice.tts.VoiceSynthesizer.speak", speak_mock),
    ):
        rc_ok = cli_main(["--watch", "--no-notify", "--", sys.executable, "-c", "raise SystemExit(0)"])
        assert rc_ok == 0
        assert av_calls[-1][0] == "playful"
        assert speak_mock.await_count == 1

        rc_fail = cli_main(["--watch", "--no-voice", "--no-notify", "--", sys.executable, "-c", "raise SystemExit(7)"])
        assert rc_fail == 7
        assert av_calls[-1][0] == "thinking"
        # --no-voice respected: speak_mock count unchanged
        assert speak_mock.await_count == 1

        # Missing command returns usage code 2; missing binary returns POSIX 127 cleanly
        assert cli_main(["--watch"]) == 2
        assert cli_main(["--watch", "--no-voice", "--no-notify", "--", "/nonexistent_capri_bin_xyz"]) == 127
