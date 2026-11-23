"""
Capri v3.4 Stage 1 — Voice 2.0, Live Audio-Reactive Avatar Aura & Quick Voice Controls Tests.

Covers:
1. Live audio-reactive energy smoothing from TTS mouth envelope (`speaking`) and mic RMS (`listening`),
   smooth decay to 0.0 when `idle`, and strict suppression when `reduced_motion=True`.
2. Offscreen rendering of the expanded elliptical audio-reactive aura + concentric holographic rings
   and rising motes for both `speaking` (cyan) and `listening` (coral), vs suppression in `reduced_motion`.
3. Microphone RMS normalization (`_normalize_mic_rms`) silence deadzone & clamping.
4. Avatar quick-control popover `🎙 Голос` / `⏹ Остановить голос` button state sync & Cyberdeck IPC dispatch
   (`voice_toggle` when idle, `voice_stop` when listening/speaking).
5. End-to-end `QLocalServer` Cyberdeck IPC handling for `voice_toggle` and `voice_stop` commands.
"""

from __future__ import annotations

import json
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from capri.ui.avatar_popover import CapriAvatarQuickPopover
from capri.ui.client import send_cyberdeck_command
from capri.ui.overlay import AvatarState, CapriAvatarWidget
from capri.ui.sprite_view import AvatarV2Canvas
from capri.voice.stt import _normalize_mic_rms


def _count_base_aura_pixels(img: QImage) -> int:
    """Count bright non-transparent pixels in the bottom equalizer/aura band (y=360..420)."""
    cnt = 0
    for y in range(360, min(img.height(), 420), 2):
        for x in range(40, min(img.width(), 260), 2):
            c = img.pixelColor(x, y)
            if c.alpha() >= 65:
                cnt += 1
    return cnt


def test_audio_reactive_energy_smoothing_rendering_and_reduced_motion(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    host = QWidget()
    host.state = "idle"
    host.caption_text = ""
    canvas = AvatarV2Canvas(host, user_sprites_dir=tmp_path)
    canvas.resize(300, 420)

    def _render() -> QImage:
        img = QImage(300, 420, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        canvas.render_to_painter(p, 300, 420)
        p.end()
        return img

    # 1. Idle state starts at 0.0 audio energy
    for _ in range(5):
        canvas.advance_animation()
    assert canvas.audio_energy == 0.0
    idle_px = _count_base_aura_pixels(_render())

    # 2. Speaking with loud lip-sync envelope boosts audio_energy & widens reactive base visualizer
    host.state = "speaking"
    canvas.set_lip_sync_envelope([4, 4, 4, 4, 4, 4])
    for _ in range(12):
        canvas.advance_animation()
    assert canvas.audio_energy > 0.75
    speak_px = _count_base_aura_pixels(_render())
    assert speak_px > idle_px

    # 3. Listening with live mic RMS boosts audio_energy in coral equalizer mode
    host.state = "listening"
    canvas.set_audio_level(0.92, source="mic")
    for _ in range(12):
        canvas.advance_animation()
    assert canvas.audio_energy > 0.75
    listen_px = _count_base_aura_pixels(_render())
    assert listen_px > idle_px

    # 4. Returning to idle decays energy cleanly back to 0.0
    host.state = "idle"
    for _ in range(45):
        canvas.advance_animation()
    assert canvas.audio_energy == 0.0

    # 5. reduced_motion=True strictly suppresses audio_energy & reactive visualizer
    host.state = "listening"
    canvas.set_reduced_motion(True)
    canvas.set_audio_level(1.0, source="mic")
    for _ in range(10):
        canvas.advance_animation()
    assert canvas.audio_energy == 0.0
    rm_px = _count_base_aura_pixels(_render())
    assert rm_px < listen_px
    host.close()


def test_mic_rms_normalization():
    assert _normalize_mic_rms(0.0) == 0.0
    assert _normalize_mic_rms(0.003) == 0.0
    mid = _normalize_mic_rms(0.059)
    assert 0.45 <= mid <= 0.55
    assert _normalize_mic_rms(0.5) == 1.0


def test_popover_voice_button_and_overlay_audio_level_ipc(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CAPRI_DISABLE_LAYER_SHELL", "1")
    QApplication.instance() or QApplication([])

    pop = CapriAvatarQuickPopover(None)
    pop.sync_from_state(
        active_pose="normal",
        dance_active=False,
        physics_enabled=True,
        autonomous_enabled=True,
        follow_enabled=False,
        auto_poses_enabled=True,
        game_mode_override=None,
        power_saver_active=False,
        pinned=False,
        perched=False,
        avatar_state="idle",
    )
    assert pop.btn_voice.text() == "🎙 Голос"
    assert pop.btn_voice.property("active") == "false"

    pop.sync_from_state(
        active_pose="normal",
        dance_active=False,
        physics_enabled=True,
        autonomous_enabled=True,
        follow_enabled=False,
        auto_poses_enabled=True,
        game_mode_override=None,
        power_saver_active=False,
        pinned=False,
        perched=False,
        avatar_state="listening",
    )
    assert pop.lbl_state.text() == "MIC"
    assert pop.btn_voice.text() == "⏹ Стоп"
    assert pop.btn_voice.property("active") == "true"
    pop.close()

    # Overlay IPC handling + voice button command dispatch
    sent_cmds: list[str] = []
    monkeypatch.setattr("capri.ui.client.send_cyberdeck_command", lambda cmd, **kw: (sent_cmds.append(cmd) or True))

    widget = CapriAvatarWidget(mode="2d")
    try:
        # Idle -> clicking quick voice triggers voice_toggle
        widget.state = AvatarState.IDLE
        widget._sync_quick_popover()
        widget.quick_popover.btn_voice.click()
        assert sent_cmds[-1] == "voice_toggle"

        # Speaking -> clicking quick voice triggers voice_stop
        widget.state = AvatarState.SPEAKING
        widget._sync_quick_popover()
        assert widget.quick_popover.lbl_state.text() == "VOICE"
        widget.quick_popover.btn_voice.click()
        assert sent_cmds[-1] == "voice_stop"

        # Direct audio_level IPC packet updates canvas_v2 target cleanly
        class _FakeClient:
            def __init__(self, payload: dict):
                self._raw = (json.dumps(payload) + "\n").encode("utf-8")

            def readAll(self):
                class _BA:
                    def __init__(self, b: bytes):
                        self._b = b

                    def data(self) -> bytes:
                        return self._b

                return _BA(self._raw)

        widget.canvas_v2.set_reduced_motion(False)
        widget._read_client_data(_FakeClient({"type": "audio_level", "level": 0.77, "source": "mic"}))
        assert abs(widget.canvas_v2.audio_level_target - 0.77) < 1e-3
    finally:
        widget.close()


def test_cyberdeck_ipc_voice_toggle_and_voice_stop(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    from capri.ui.cyberdeck import CapriCyberdeckWindow

    # Minimal stub reusing real CapriCyberdeckWindow._on_ipc_connection + start_ipc_server
    sock_path = str(tmp_path / "capri-cyberdeck-test.sock")
    calls: list[str] = []

    class _MinimalDeck(QWidget):
        def __init__(self):
            super().__init__()
            self.ipc_server = None
            self._ipc_socket_path = None
            self._recording = False

        _stop_ipc_server = CapriCyberdeckWindow._stop_ipc_server
        start_ipc_server = CapriCyberdeckWindow.start_ipc_server
        _on_ipc_connection = CapriCyberdeckWindow._on_ipc_connection

        def _is_recording_voice(self):
            return self._recording

        def _toggle_voice_input(self):
            self._recording = not self._recording
            calls.append("voice_toggle")

        def _stop_voice_input(self):
            self._recording = False
            calls.append("voice_stop_mic")

        def _stop_generation(self, silent: bool = False):
            calls.append(f"stop_gen:{silent}")

    deck = _MinimalDeck()
    try:
        assert deck.start_ipc_server(sock_path) is True
        import threading

        def _send_and_pump(cmd: str):
            res: list[bool] = []
            t = threading.Thread(target=lambda: res.append(send_cyberdeck_command(cmd, socket_path=sock_path, timeout=1.0)))
            t.start()
            app = QApplication.instance()
            for _ in range(40):
                app.processEvents()
                if not t.is_alive():
                    break
                import time
                time.sleep(0.01)
            t.join(timeout=1.0)
            return res[0] if res else False

        assert _send_and_pump("voice_toggle") is True
        assert calls[-1] == "voice_toggle"
        assert deck._recording is True

        assert _send_and_pump("voice_stop") is True
        assert "voice_stop_mic" in calls
        assert "stop_gen:True" in calls
        assert deck._recording is False
    finally:
        deck._stop_ipc_server()
        deck.close()


def test_audio_level_dispatcher_never_blocks_audio_callback(monkeypatch):
    """PortAudio callback enqueue must return immediately even if socket I/O stalls."""
    import time
    from capri.voice.stt import _AsyncAudioLevelDispatcher

    delivered: list[float] = []
    started = __import__("threading").Event()
    release = __import__("threading").Event()

    def _slow_notify(level: float, source: str = "mic") -> bool:
        delivered.append(level)
        started.set()
        release.wait(timeout=0.5)
        return True

    monkeypatch.setattr("capri.ui.client.notify_avatar_audio_level", _slow_notify)
    disp = _AsyncAudioLevelDispatcher()

    t0 = time.monotonic()
    disp.enqueue(0.2)
    assert started.wait(timeout=0.5)
    # Burst 10 updates while worker is blocked in socket I/O -> must NOT block caller & must coalesce!
    for v in (0.3, 0.4, 0.5, 0.6, 0.9):
        disp.enqueue(v)
    elapsed = time.monotonic() - t0
    release.set()
    assert elapsed < 0.15

    # Wait briefly for coalesced latest value (0.9) to flush
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline and (not delivered or delivered[-1] != 0.9):
        time.sleep(0.01)
    assert delivered[-1] == 0.9
    assert len(delivered) <= 3  # stale intermediates coalesced cleanly!


def test_silero_daemon_closes_subprocess_transport_on_cancel_and_eof(tmp_path):
    """Direct regression test proving SileroDaemonClient leaves zero unclosed transports."""
    import asyncio
    import gc
    import sys
    import warnings
    from capri.voice.tts import SileroDaemonClient

    # 1. Worker that hangs before printing READY -> cancelled during ensure_running()
    hang_worker = tmp_path / "hang_worker.py"
    hang_worker.write_text("import time; time.sleep(10)\n", encoding="utf-8")

    # 2. Worker that exits immediately without printing READY -> EOF during ensure_running()
    eof_worker = tmp_path / "eof_worker.py"
    eof_worker.write_text("import sys; sys.exit(0)\n", encoding="utf-8")

    async def _run_cases():
        c1 = SileroDaemonClient(venv_python=tmp_path / "py", worker_script=hang_worker)
        c1.venv_python = __import__("pathlib").Path(sys.executable)
        task = asyncio.create_task(c1.ensure_running())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert c1.proc is None

        c2 = SileroDaemonClient(venv_python=__import__("pathlib").Path(sys.executable), worker_script=eof_worker)
        await c2.ensure_running()
        assert c2.proc is None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(_run_cases())
        gc.collect()
    unraisable = [w for w in caught if "Event loop is closed" in str(w.message) or "unclosed" in str(w.message).lower()]
    assert unraisable == []
