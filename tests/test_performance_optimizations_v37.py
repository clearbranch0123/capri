"""Regression tests for Capri startup, lazy-import isolation, and avatar performance optimizations."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_py_probe(code: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["QT_QPA_PLATFORM"] = "offscreen"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_top_level_package_imports_are_lazy_and_preserve_public_exports():
    probe = r"""
import sys
import capri

assert isinstance(capri.__version__, str) and capri.__version__
for heavy in ("openai", "sounddevice", "numpy", "PySide6", "capri.core.agent", "capri.ui.overlay"):
    assert heavy not in sys.modules, f"Unexpected eager import on `import capri`: {heavy}"

# Verify public re-exports remain 100% compatible
from capri import AppConfig, load_config, save_config, CapriAgent, AgentEvent
from capri.core import get_system_prompt
from capri.voice import VoiceSynthesizer
assert AppConfig is not None
assert callable(load_config) and callable(save_config)
assert CapriAgent is not None and AgentEvent is not None
assert callable(get_system_prompt)
assert VoiceSynthesizer is not None
print("LAZY_ROOT_OK")
"""
    res = _run_py_probe(probe)
    assert res.returncode == 0, f"stderr:\n{res.stderr}\nstdout:\n{res.stdout}"
    assert "LAZY_ROOT_OK" in res.stdout


def test_avatar_overlay_import_skips_agent_voice_and_qtquick():
    probe = r"""
import sys
import capri.ui.overlay as ov

for heavy in ("openai", "sounddevice", "numpy", "PySide6.QtQuickWidgets", "capri.core.agent"):
    assert heavy not in sys.modules, f"capri.ui.overlay eagerly imported {heavy}"

assert ov.CapriAvatarWidget is not None
print("LAZY_OVERLAY_OK")
"""
    res = _run_py_probe(probe)
    assert res.returncode == 0, f"stderr:\n{res.stderr}\nstdout:\n{res.stdout}"
    assert "LAZY_OVERLAY_OK" in res.stdout


def test_avatar_2d_does_not_instantiate_qtquick3d_until_3d_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CAPRI_DISABLE_LAYER_SHELL", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    import capri.ui.overlay as ov

    monkeypatch.setattr(ov, "SOCKET_PATH", str(tmp_path / "avatar_lazy_3d.sock"))
    app = QApplication.instance() or QApplication([])
    widget = ov.CapriAvatarWidget(mode="2d")
    try:
        assert widget.quick_3d is None
        widget._sync_3d_properties()  # safe no-op when 3D view not loaded
    finally:
        widget.anim_timer.stop()
        widget.close()
        app.processEvents()


def test_avatar_sprites_load_lazily_by_state_and_classic_canvas_is_deferred(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CAPRI_DISABLE_LAYER_SHELL", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    import capri.ui.overlay as ov

    monkeypatch.setattr(ov, "SOCKET_PATH", str(tmp_path / "avatar_lazy_sprites.sock"))
    app = QApplication.instance() or QApplication([])
    widget = ov.CapriAvatarWidget(mode="2d")
    try:
        assert widget.canvas_classic is None
        cached_at_start = widget.canvas_v2.get_cached_sprite_slots()
        assert cached_at_start == {"idle", "blink"}, f"Unexpected eager sprite slots: {cached_at_start}"

        # Stepping idle animation must not eagerly decode sleep/perch/dance/glitch sheets
        for _ in range(10):
            widget.canvas_v2.advance_animation()
        assert "perch" not in widget.canvas_v2.get_cached_sprite_slots()
        assert "perch_music" not in widget.canvas_v2.get_cached_sprite_slots()
        assert "sleep" not in widget.canvas_v2.get_cached_sprite_slots()
        assert "dance" not in widget.canvas_v2.get_cached_sprite_slots()

        # On-demand access decodes requested sheet cleanly
        assert len(widget.canvas_v2.perch_frames) == 60
        assert "perch" in widget.canvas_v2.get_cached_sprite_slots()

        # Classic mode lazily instantiates Avatar2DCanvas
        widget.set_mode("classic")
        assert widget.canvas_classic is not None
    finally:
        widget.anim_timer.stop()
        widget.close()
        app.processEvents()


def test_avatar_adaptive_anim_timer_throttles_in_idle_and_boosts_on_activity(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CAPRI_DISABLE_LAYER_SHELL", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    import capri.ui.overlay as ov

    monkeypatch.setattr(ov, "SOCKET_PATH", str(tmp_path / "avatar_adaptive_timer.sock"))
    app = QApplication.instance() or QApplication([])
    widget = ov.CapriAvatarWidget(mode="2d")
    try:
        # Calm IDLE uses relaxed ~15 Hz cadence (64 ms) instead of burning 60 Hz redraws
        widget.caption_timer = 0
        widget.canvas_v2.blink_active = False
        widget._sync_anim_timer_interval()
        assert widget.anim_timer.isActive() is True
        assert widget.anim_timer.interval() == 64

        # Sleep pose relaxes even further (120 ms)
        widget.canvas_v2.set_pose("sleep")
        widget._sync_anim_timer_interval()
        assert widget.anim_timer.interval() == 120

        # Active speaking immediately restores 60 Hz (16 ms)
        widget.set_state(ov.AvatarState.SPEAKING, "Проверка 60 Гц!")
        assert widget.anim_timer.interval() == 16

        # Returning to calm idle with no caption settles back to 64 ms
        widget.set_state(ov.AvatarState.IDLE, "")
        widget.caption_timer = 0
        widget.canvas_v2.waking_active = False
        widget.canvas_v2.blink_active = False
        widget.canvas_v2.audio_energy = 0.0
        widget._sync_anim_timer_interval()
        assert widget.anim_timer.interval() == 64

        # Physics motion immediately boosts back to 60 Hz (16 ms)
        widget.physics.state = ov.PhysicsState.FALLING
        widget._sync_anim_timer_interval()
        assert widget.anim_timer.interval() == 16
    finally:
        widget.anim_timer.stop()
        widget.close()
        app.processEvents()


def test_cyberdeck_public_reexports_match_modular_deck_package():
    import capri.ui.cyberdeck as cd
    from capri.ui.deck.chat_card import ChatMessageCard, fade_in_card, play_ui_sound
    from capri.ui.deck.dialogs import (
        CapriChangelogDialog,
        CapriFirstRunWizard,
        CapriMemoryEditDialog,
        CapriMemoryWorkbenchDialog,
        CapriResetDialog,
        CapriSessionsDialog,
        CapriToolApprovalDialog,
    )
    from capri.ui.deck.settings_dialog import CapriProviderSettingsDialog
    from capri.ui.deck.workers import (
        APIKeyCheckWorker,
        AgentWorker,
        MCPConnectWorker,
        MicCalibrationWorker,
        RegionCaptureWorker,
        VoiceRecordWorker,
        check_api_key_sync,
    )

    assert cd.ChatMessageCard is ChatMessageCard
    assert cd.fade_in_card is fade_in_card
    assert cd.play_ui_sound is play_ui_sound
    assert cd.CapriFirstRunWizard is CapriFirstRunWizard
    assert cd.CapriToolApprovalDialog is CapriToolApprovalDialog
    assert cd.CapriResetDialog is CapriResetDialog
    assert cd.CapriChangelogDialog is CapriChangelogDialog
    assert cd.CapriSessionsDialog is CapriSessionsDialog
    assert cd.CapriMemoryEditDialog is CapriMemoryEditDialog
    assert cd.CapriMemoryWorkbenchDialog is CapriMemoryWorkbenchDialog
    assert cd.CapriProviderSettingsDialog is CapriProviderSettingsDialog
    assert cd.APIKeyCheckWorker is APIKeyCheckWorker
    assert cd.MCPConnectWorker is MCPConnectWorker
    assert cd.AgentWorker is AgentWorker
    assert cd.VoiceRecordWorker is VoiceRecordWorker
    assert cd.RegionCaptureWorker is RegionCaptureWorker
    assert cd.MicCalibrationWorker is MicCalibrationWorker
    assert cd.check_api_key_sync is check_api_key_sync
