"""
Capri v3.4 Stage 3 — Living Awareness Micro-Reactions & Quick Screen-Region Ask Tests.

Covers:
1. `AvatarAwarenessEngine` deterministic triggers:
   - Return from long AFK (`afk_return`), late-night check-in (`late_night`), long focus stretch (`long_session`)
   - Dynamic `user_name` interpolation (never hardcoding a personal name)
   - Strict silence gates (`game_mode_active`, `focus`/`quiet`, `speaking`/`listening`, `falling`, active caption)
   - Global & per-kind cooldown enforcement (zero spam)
2. `capture_screen_region_sync()` helper:
   - Graceful `missing_tools` when `slurp`/`grim` absent
   - Silent `cancelled` when `slurp` exits non-zero (user pressed Esc)
   - Clean `ok` capture creating a `0600` temp PNG
3. Avatar popover `👁 Область` button -> `look_region` Cyberdeck IPC dispatch
4. Cyberdeck IPC `look_region` handling & `_on_region_captured()` clean cancel / auto-attach + send + temp file cleanup
"""

from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

from capri.tools.screen import capture_screen_region_sync
from capri.ui.avatar_awareness import AvatarAwarenessEngine
from capri.ui.client import send_cyberdeck_command
from capri.ui.cyberdeck import CapriCyberdeckWindow
from capri.ui.overlay import CapriAvatarWidget


def test_avatar_awareness_triggers_gates_and_cooldowns():
    t0 = 1_700_000_000.0
    eng = AvatarAwarenessEngine(user_name="Alex", now=t0)

    # 1. Normal daytime start -> no cue immediately
    assert eng.tick(now=t0 + 60.0, local_hour=14).kind == "none"

    # 2. Long AFK (>= 45 min) -> return triggers afk_return once with user_name
    eng.note_user_activity(t0 + 3000.0)
    # Suppressed during Game Mode / focus / quiet / active voice / physics!
    assert eng.tick(now=t0 + 3001.0, local_hour=14, game_mode_active=True).kind == "none"
    assert eng.tick(now=t0 + 3002.0, local_hour=14, companion_mode="focus").kind == "none"
    assert eng.tick(now=t0 + 3003.0, local_hour=14, companion_mode="quiet").kind == "none"
    assert eng.tick(now=t0 + 3004.0, local_hour=14, avatar_state="speaking").kind == "none"
    assert eng.tick(now=t0 + 3005.0, local_hour=14, physics_state="falling").kind == "none"
    assert eng.tick(now=t0 + 3006.0, local_hour=14, has_active_caption=True).kind == "none"

    cue_afk = eng.tick(now=t0 + 3010.0, local_hour=14)
    assert cue_afk.kind == "afk_return"
    assert "Alex" in cue_afk.text

    # Global 30-min cooldown blocks immediate late-night or session cues
    assert eng.tick(now=t0 + 3500.0, local_hour=2).kind == "none"

    # 3. After global cooldown, late-night hour (02:00) emits late_night at most once per night
    eng.note_user_activity(t0 + 4800.0)  # periodic activity keeps session continuous (no AFK reset)
    cue_night = eng.tick(now=t0 + 5000.0, local_hour=2)
    assert cue_night.kind == "late_night"
    assert "Alex" in cue_night.text
    # Periodic activity keeps session continuous without AFK; repeated late-night check still suppressed by 6h cooldown
    eng.note_user_activity(t0 + 6800.0)
    assert eng.tick(now=t0 + 6900.0, local_hour=3).kind == "none"

    # 4. Long continuous focus stretch (>= 90 min since session start at t0+3000) triggers long_session
    eng.note_user_activity(t0 + 8500.0)
    cue_sess = eng.tick(now=t0 + 8600.0, local_hour=15)
    assert cue_sess.kind == "long_session"

    # Default generic "User" name omits awkward ", User" addressing
    eng_default = AvatarAwarenessEngine(user_name="User", now=t0)
    eng_default.note_user_activity(t0 + 3000.0)
    cue_def = eng_default.tick(now=t0 + 3005.0, local_hour=14)
    assert cue_def.kind == "afk_return"
    assert "User" not in cue_def.text


def test_capture_screen_region_sync_missing_cancel_and_success(monkeypatch):
    # 1. Missing tools -> graceful status
    monkeypatch.setattr("capri.tools.screen.shutil.which", lambda name: None)
    path, status = capture_screen_region_sync()
    assert path is None and status == "missing_tools"

    # 2. User presses Esc in slurp -> silent cancel
    monkeypatch.setattr("capri.tools.screen.shutil.which", lambda name: f"/usr/bin/{name}")

    class _FakePopen:
        def __init__(self, rc: int, out: str = "", on_run=None):
            self.returncode = rc
            self._out = out
            self._on_run = on_run
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.terminated = True
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self, timeout=None):
            if self._on_run is not None:
                self._on_run()
            return self._out, ""

    monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: _FakePopen(1, ""))
    path, status = capture_screen_region_sync()
    assert path is None and status == "cancelled"

    # 3. Successful slurp + grim capture -> creates 0600 PNG file
    def _fake_ok_popen(cmd, **kw):
        if cmd[0] == "slurp":
            return _FakePopen(0, "100,200 320x240\n")
        if cmd[0] == "grim":
            out_file = cmd[-1]

            def _write_png():
                img = QImage(32, 32, QImage.Format.Format_ARGB32)
                img.fill(QColor(225, 45, 83))
                assert img.save(out_file, "PNG") is True

            return _FakePopen(0, "", on_run=_write_png)
        raise AssertionError(f"Unexpected cmd: {cmd}")

    monkeypatch.setattr("subprocess.Popen", _fake_ok_popen)
    QApplication.instance() or QApplication([])
    path, status = capture_screen_region_sync()
    assert status == "ok" and path is not None and os.path.exists(path)
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        os.remove(path)

    # 4. Cancellation while slurp is waiting terminates subprocess promptly and leaves zero temp files
    hang_proc_holder: list[object] = []

    class _HangingPopen(_FakePopen):
        def __init__(self):
            super().__init__(None, "")
            self._alive = True

        def poll(self):
            return None if self._alive else self.returncode

        def terminate(self):
            self._alive = False
            super().terminate()

        def communicate(self, timeout=None):
            if self._alive:
                time.sleep(min(0.03, timeout or 0.03))
                raise subprocess.TimeoutExpired(cmd=["slurp"], timeout=timeout or 0.03)
            return "", ""

    def _spawn_hang(cmd, **kw):
        p = _HangingPopen()
        hang_proc_holder.append(p)
        return p

    monkeypatch.setattr("subprocess.Popen", _spawn_hang)
    cancel_ev = threading.Event()
    result_box: list[tuple] = []
    t = threading.Thread(target=lambda: result_box.append(capture_screen_region_sync(timeout=30.0, cancel_event=cancel_ev)))
    t0 = time.monotonic()
    t.start()
    time.sleep(0.08)
    cancel_ev.set()
    t.join(timeout=1.5)
    assert not t.is_alive()
    assert (time.monotonic() - t0) < 1.0
    assert result_box == [(None, "cancelled")]
    assert hang_proc_holder and hang_proc_holder[0].terminated is True

    # 5. Cancellation right as grim completes deletes the temp screenshot so /tmp never leaks
    leaked_paths: list[str] = []
    cancel_after_grim = threading.Event()

    def _fake_cancel_race(cmd, **kw):
        if cmd[0] == "slurp":
            return _FakePopen(0, "10,10 100x100\n")
        if cmd[0] == "grim":
            out_file = cmd[-1]
            leaked_paths.append(out_file)

            def _write_and_cancel():
                Path(out_file).write_bytes(b"\x89PNG\r\n\x1a\nfakedata")
                cancel_after_grim.set()

            return _FakePopen(0, "", on_run=_write_and_cancel)
        raise AssertionError(f"Unexpected cmd: {cmd}")

    monkeypatch.setattr("subprocess.Popen", _fake_cancel_race)
    path_r, status_r = capture_screen_region_sync(cancel_event=cancel_after_grim)
    assert path_r is None and status_r == "cancelled"
    assert leaked_paths and not os.path.exists(leaked_paths[0])


def test_popover_look_region_button_and_cyberdeck_ipc(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CAPRI_DISABLE_LAYER_SHELL", "1")
    QApplication.instance() or QApplication([])

    # 1. Avatar popover "👁 Область" sends "look_region" to Cyberdeck IPC
    sent: list[str] = []
    monkeypatch.setattr("capri.ui.client.send_cyberdeck_command", lambda cmd, **kw: (sent.append(cmd) or True))
    widget = CapriAvatarWidget(mode="2d")
    try:
        assert widget.quick_popover.btn_look_region.text() == "👁 Область"
        widget.quick_popover.btn_look_region.click()
        assert sent[-1] == "look_region"
    finally:
        widget.close()

    # 2. Cyberdeck IPC receives "look_region" and _on_region_captured attaches + sends + cleans temp PNG
    sock_path = str(tmp_path / "capri-look-test.sock")
    events: list[str] = []

    class _FakeStrip:
        def __init__(self):
            self.items = []

        def add_attachment(self, item):
            self.items.append(item)
            return True

    class _FakeInput:
        def __init__(self):
            self.txt = ""
            self.focused = False

        def toPlainText(self):
            return self.txt

        def setText(self, t):
            self.txt = t

        def setFocus(self):
            self.focused = True
            events.append("focused")

    class _MinimalDeck(QWidget):
        def __init__(self):
            super().__init__()
            self.ipc_server = None
            self._ipc_socket_path = None
            self._region_capture_worker = None
            self.attachment_strip = _FakeStrip()
            self.input_field = _FakeInput()

        _stop_ipc_server = CapriCyberdeckWindow._stop_ipc_server
        start_ipc_server = CapriCyberdeckWindow.start_ipc_server
        _on_ipc_connection = CapriCyberdeckWindow._on_ipc_connection
        _on_region_captured = CapriCyberdeckWindow._on_region_captured

        def _trigger_look_region(self):
            events.append("triggered_look_region")

        def _append_message(self, role, text):
            events.append(f"msg:{role}:{text}")

        def _show_and_raise(self):
            events.append("raised")

        def _send_message(self):
            events.append("sent")

    deck = _MinimalDeck()
    try:
        assert deck.start_ipc_server(sock_path) is True
        res: list[bool] = []
        t = threading.Thread(target=lambda: res.append(send_cyberdeck_command("look_region", socket_path=sock_path, timeout=1.0)))
        t.start()
        app = QApplication.instance()
        for _ in range(40):
            app.processEvents()
            if not t.is_alive():
                break
            time.sleep(0.01)
        t.join(timeout=1.0)
        assert res and res[0] is True
        assert "triggered_look_region" in events

        # Cancelled capture must be completely silent (zero system messages, zero sends)
        events.clear()
        deck._on_region_captured("", "cancelled")
        assert events == []

        # Valid captured PNG attaches cleanly, removes raw tmp file, raises & focuses composer WITHOUT auto-sending
        tmp_png = tmp_path / "captured.png"
        im = QImage(40, 40, QImage.Format.Format_ARGB32)
        im.fill(QColor(78, 216, 232))
        assert im.save(str(tmp_png), "PNG") is True
        deck._on_region_captured(str(tmp_png), "ok")
        assert not tmp_png.exists()  # raw temp screenshot cleaned up immediately!
        assert len(deck.attachment_strip.items) == 1
        assert "raised" in events and "focused" in events
        assert "sent" not in events
        assert deck.input_field.txt == ""
        # Clean up saved attachment temp file created by save_clipboard_image_bytes
        from capri.core.attachments import cleanup_temp_attachment
        for att in deck.attachment_strip.items:
            cleanup_temp_attachment(att)

        # 3. Unclaimed queued signal race on close: worker owns temp PNG and deletes it on cancel cleanup
        from capri.ui.cyberdeck import RegionCaptureWorker
        race_png = tmp_path / "unclaimed_race.png"
        race_png.write_bytes(b"\x89PNG\r\n\x1a\nracedata")
        monkeypatch.setattr("capri.tools.screen.capture_screen_region_sync", lambda cancel_event=None: (str(race_png), "ok"))
        rw = RegionCaptureWorker()
        # Do NOT connect signal (simulating closeEvent disconnecting before queued delivery!)
        rw.run()
        assert race_png.exists()
        rw.cleanup_unclaimed_path()
        assert not race_png.exists()
    finally:
        deck._stop_ipc_server()
        deck.close()
