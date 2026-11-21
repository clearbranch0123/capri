"""
Tests for Wave 4 (v3.0.0a1 — Living Presence 3.0 Stage 1):
- MPRIS playback parser & preferred music player selection
- Game Mode / Power Saver detection (fullscreen, games, false-positive guards)
- Deterministic avatar auto-pose resolver (perch_music, sleep, normal, manual override)
- InitiativeEngine & DesktopObserver suppression during Game Mode
- CapriAvatarWidget Power Saver (0 FPS timer stop), MPRIS auto-perch, poke/shake/drop reactions
- Cyberdeck `/gamemode`, `/avatar auto`, status chip, and Settings dialog persistence
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication

from capri.core.config import AppConfig
from capri.core.initiative import InitiativeEngine
from capri.core.observer import DesktopObserver
from capri.core.presence import (
    GameModeState,
    MediaPlaybackState,
    detect_game_mode,
    evaluate_game_mode,
    is_game_window,
    parse_mpris_lines,
    resolve_avatar_auto_pose,
)
from capri.ui.cyberdeck import CapriCyberdeckWindow, CapriProviderSettingsDialog


def test_parse_mpris_lines_prefers_music_players_and_handles_paused_stopped():
    raw_multi = (
        "Playing\tfirefox.instance_1_2\tYouTube\tLo-Fi Mix\n"
        "Playing\tspotify\tSamurai\tNever Fade Away\n"
    )
    st = parse_mpris_lines(raw_multi)
    assert st.is_playing is True
    assert st.player == "spotify"
    assert st.track_label == "Samurai — Never Fade Away"

    raw_browser = "Playing\tchromium.instance99\tKerry Eurodyne\tChippin' In\n"
    st_b = parse_mpris_lines(raw_browser)
    assert st_b.is_playing is True
    assert st_b.player == "chromium.instance99"
    assert st_b.track_label == "Kerry Eurodyne — Chippin' In"

    raw_paused = "Paused\tspotify\tSamurai\tBlack Dog\n"
    st_p = parse_mpris_lines(raw_paused)
    assert st_p.is_playing is False
    assert st_p.status == "Paused"

    assert parse_mpris_lines("").is_playing is False


def test_evaluate_game_mode_fullscreen_games_and_false_positive_guards():
    # Fullscreen Steam game
    gm1 = evaluate_game_mode({"app_id": "steam_app_1091500", "title": "Cyberpunk 2077", "is_fullscreen": True})
    assert gm1.active is True
    assert gm1.reason == "fullscreen_game"

    # Fullscreen video / browser
    gm2 = evaluate_game_mode({"app_id": "firefox", "title": "Edgerunners S2", "is_fullscreen": True})
    assert gm2.active is True
    assert gm2.reason == "fullscreen_app"

    # Windowed game runtimes
    assert is_game_window("gamescope") is True
    assert is_game_window("steam_app_730") is True
    assert is_game_window("Cyberpunk2077.exe") is True
    gm3 = evaluate_game_mode({"app_id": "gamescope", "title": "Game", "is_fullscreen": False})
    assert gm3.active is True
    assert gm3.reason == "windowed_game"

    # False-positive guards: plain Steam store in windowed mode & Capri's own windows
    assert is_game_window("steam") is False
    gm_steam = evaluate_game_mode({"app_id": "steam", "title": "Steam", "is_fullscreen": False})
    assert gm_steam.active is False

    gm_own = evaluate_game_mode({"app_id": "capri-cyberdeck", "title": "CAPRI", "is_fullscreen": True})
    assert gm_own.active is False

    # Manual overrides win unconditionally
    assert evaluate_game_mode(None, manual_override=True).active is True
    assert evaluate_game_mode({"app_id": "gamescope", "is_fullscreen": True}, manual_override=False).active is False


def test_detect_game_mode_integrates_with_compositor_backend_niri_and_hyprland(monkeypatch):
    """
    Integration test verifying `detect_game_mode()` calls the real `CompositorBackend.get_focused_window()`
    (and Niri `get_screen_size()` geometry fullscreen check) for both Hyprland and Niri.
    """
    import json
    from capri.core import compositor as comp_mod

    # 1. Hyprland fullscreen CS2 via real CompositorBackend
    hypr_backend = comp_mod.CompositorBackend(forced_kind="hyprland")

    def fake_hypr_run(cmd, timeout=1.5):
        if cmd == ["hyprctl", "-j", "activewindow"]:
            payload = {
                "address": "0x55a1",
                "class": "cs2",
                "title": "Counter-Strike 2",
                "pid": 4242,
                "workspace": {"id": 3, "name": "3"},
                "at": [0, 0],
                "size": [1920, 1080],
                "floating": False,
                "fullscreen": 2,
                "fullscreenClient": 2,
            }
            return 0, json.dumps(payload), ""
        return 1, "", "unexpected"

    monkeypatch.setattr(comp_mod, "_run_sync", fake_hypr_run)
    gm_hypr = detect_game_mode(backend=hypr_backend)
    assert gm_hypr.active is True
    assert gm_hypr.reason == "fullscreen_game"
    assert gm_hypr.app_id == "cs2"
    assert gm_hypr.is_fullscreen is True

    # 2. Niri real JSON (no `is_fullscreen` key!) — fullscreen detected via full-output window_size
    niri_backend = comp_mod.CompositorBackend(forced_kind="niri")

    def fake_niri_fs_run(cmd, timeout=1.5):
        if cmd == ["niri", "msg", "-j", "focused-window"]:
            payload = {
                "id": 99,
                "title": "YouTube — Fullscreen",
                "app_id": "firefox",
                "pid": 8080,
                "workspace_id": 2,
                "is_focused": True,
                "is_floating": False,
                "layout": {"window_size": [1920, 1080], "tile_size": [1920.0, 1080.0]},
            }
            return 0, json.dumps(payload), ""
        if cmd == ["niri", "msg", "-j", "outputs"]:
            return 0, json.dumps({"DP-1": {"logical": {"width": 1920, "height": 1080}}}), ""
        return 1, "", "unexpected"

    monkeypatch.setattr(comp_mod, "_run_sync", fake_niri_fs_run)
    gm_niri_fs = detect_game_mode(backend=niri_backend)
    assert gm_niri_fs.active is True
    assert gm_niri_fs.reason == "fullscreen_app"
    assert gm_niri_fs.is_fullscreen is True

    # 3. Niri normal tiled window (height < 1080 due to bar/gaps) -> NOT Game Mode
    def fake_niri_tiled_run(cmd, timeout=1.5):
        if cmd == ["niri", "msg", "-j", "focused-window"]:
            payload = {
                "id": 55,
                "title": "kitty",
                "app_id": "kitty",
                "pid": 61167,
                "workspace_id": 10,
                "is_focused": True,
                "is_floating": False,
                "layout": {"window_size": [922, 985], "tile_size": [922.0, 985.0]},
            }
            return 0, json.dumps(payload), ""
        if cmd == ["niri", "msg", "-j", "outputs"]:
            return 0, json.dumps({"DP-1": {"logical": {"width": 1920, "height": 1080}}}), ""
        return 1, "", "unexpected"

    monkeypatch.setattr(comp_mod, "_run_sync", fake_niri_tiled_run)
    gm_niri_tiled = detect_game_mode(backend=niri_backend)
    assert gm_niri_tiled.active is False
    assert gm_niri_tiled.is_fullscreen is False

    # 4. Also verify default module-level `compositor` path works directly without passing `backend`
    monkeypatch.setattr(comp_mod, "detect_compositor", lambda: "hyprland")
    monkeypatch.setattr(comp_mod, "_run_sync", fake_hypr_run)
    gm_default = detect_game_mode()
    assert gm_default.active is True
    assert gm_default.reason == "fullscreen_game"


def test_resolve_avatar_auto_pose_priority_machine():
    # Game mode always puts avatar to power-saver sleep
    assert resolve_avatar_auto_pose(
        avatar_state="idle", media_playing=True, game_mode_active=True
    ) == ("sleep", "power_saver")

    # Active speaking/listening overrides music or manual sleep
    assert resolve_avatar_auto_pose(
        avatar_state="speaking", media_playing=True, manual_pose_override="sleep"
    ) == ("normal", "active_interaction")

    # Manual pose override respected during idle
    assert resolve_avatar_auto_pose(
        avatar_state="idle", media_playing=True, manual_pose_override="perch"
    ) == ("perch", "manual_override")

    # MPRIS playing switches to perch_music automatically
    assert resolve_avatar_auto_pose(
        avatar_state="idle", media_playing=True, auto_poses_enabled=True
    ) == ("perch_music", "mpris_playing")

    # Idle timeout puts avatar to sleep
    assert resolve_avatar_auto_pose(
        avatar_state="idle", media_playing=False, idle_seconds=200.0, idle_sleep_threshold=180.0
    ) == ("sleep", "idle_timeout")

    # Disabled auto-poses returns normal
    assert resolve_avatar_auto_pose(
        avatar_state="idle", media_playing=True, auto_poses_enabled=False
    ) == ("normal", "auto_disabled")


def test_initiative_and_observer_suppressed_in_game_mode(tmp_path):
    ie = InitiativeEngine(file_path=tmp_path / "init_gm.json")
    ie.settings.enabled = True
    ie.settings.quiet_hours_enabled = False
    ie.last_initiative_time = 0.0
    ie.last_user_activity = time.time() - 3600

    can_norm, _ = ie.can_trigger(companion_mode="normal", game_mode_active=False)
    assert can_norm is True

    can_gm, reason_gm = ie.can_trigger(companion_mode="normal", game_mode_active=True)
    assert can_gm is False
    assert "Game Mode" in reason_gm or "Игровой" in reason_gm
    assert ie.evaluate(companion_mode="normal", game_mode_active=True) is None

    # DesktopObserver must suppress reactions while a fullscreen game is focused
    dummy_agent = MagicMock()
    dummy_emotions = MagicMock()
    obs = DesktopObserver(dummy_agent, dummy_emotions, mode="normal")
    obs._trigger_reaction = AsyncMock()

    asyncio.run(obs._handle_focused_window("steam_app_1091500", "Cyberpunk 2077", is_fullscreen=True))
    assert obs.game_mode_active is True
    obs._trigger_reaction.assert_not_awaited()


def test_avatar_widget_mpris_gamemode_power_saver_and_reactions(tmp_path, monkeypatch):
    from capri.ui import overlay as ov_mod

    monkeypatch.setattr(ov_mod, "SOCKET_PATH", str(tmp_path / "test_avatar.sock"))
    monkeypatch.setattr(ov_mod, "PID_PATH", str(tmp_path / "test_avatar.pid"))

    app = QApplication.instance() or QApplication([])
    widget = ov_mod.CapriAvatarWidget(mode="2d")
    widget.auto_poses_enabled = True
    widget.game_mode_enabled = True
    widget.reactions_enabled = True
    QApplication.processEvents()

    # 1. MPRIS playing -> automatic perch_music + track caption
    playing = MediaPlaybackState(
        is_playing=True,
        status="Playing",
        player="spotify",
        artist="Samurai",
        title="Never Fade Away",
    )
    widget._apply_presence_snapshot(playing, GameModeState(active=False))
    assert widget.canvas_v2.pose == "perch_music"
    assert "Samurai" in widget.caption_text

    # 2. Entering Game Mode / Power Saver -> stops 60 FPS timer, dims opacity, sleeps
    widget._apply_presence_snapshot(
        MediaPlaybackState(is_playing=False),
        GameModeState(active=True, reason="fullscreen_game", app_id="steam_app_1091500", is_fullscreen=True),
    )
    assert widget.power_saver_active is True
    assert widget.anim_timer.isActive() is False
    assert widget.windowOpacity() <= 0.2
    assert widget.canvas_v2.pose == "sleep"

    # Reactions are suppressed while Power Saver is active
    widget.caption_text = ""
    widget.trigger_poke_reaction()
    assert widget.caption_text == ""

    # 3. Exiting Game Mode -> restores 60 FPS timer, full opacity, wake animation
    widget._apply_presence_snapshot(MediaPlaybackState(is_playing=False), GameModeState(active=False))
    assert widget.power_saver_active is False
    assert widget.anim_timer.isActive() is True
    assert widget.windowOpacity() == pytest.approx(1.0)
    assert widget.canvas_v2.waking_active is True

    # 4. Interactive poke (single & rapid 3x) and shake reactions
    widget._last_reaction_ts = 0.0
    widget.trigger_poke_reaction()
    assert widget.caption_text in ov_mod.CapriAvatarWidget._POKE_QUOTES

    widget._last_reaction_ts = 0.0
    widget._poke_count = 2
    widget._last_poke_ts = time.time()
    widget.trigger_poke_reaction()
    assert widget.caption_text in ov_mod.CapriAvatarWidget._RAPID_POKE_QUOTES
    assert widget.canvas_v2.glitch_active is True

    widget.canvas_v2.glitch_active = False
    widget.trigger_shake_reaction()
    assert widget.caption_text in ov_mod.CapriAvatarWidget._SHAKE_QUOTES
    assert widget.canvas_v2.glitch_active is True

    widget._last_reaction_ts = 0.0
    widget.trigger_drag_drop_reaction()
    assert widget.caption_text in ov_mod.CapriAvatarWidget._DROP_QUOTES

    widget.close()


def test_cyberdeck_gamemode_avatar_auto_commands_and_settings_persistence():
    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False
    cfg.ui.auto_avatar_poses = True
    cfg.ui.game_mode_enabled = True

    with patch("capri.ui.cyberdeck.save_config"), patch("capri.ui.cyberdeck.update_env_value"):
        dlg = CapriProviderSettingsDialog(cfg)
        assert dlg.chk_auto_avatar_poses.isChecked() is True
        assert dlg.chk_game_mode.isChecked() is True
        dlg.chk_auto_avatar_poses.setChecked(False)
        dlg.chk_game_mode.setChecked(False)
        dlg._apply_and_save()
        assert cfg.ui.auto_avatar_poses is False
        assert cfg.ui.game_mode_enabled is False
        dlg.close()

    sent_payloads: list[dict] = []
    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True), \
         patch("capri.ui.cyberdeck.send_avatar_control", side_effect=lambda p: sent_payloads.append(p) or True), \
         patch("capri.ui.cyberdeck.save_config"):
        deck = CapriCyberdeckWindow(config=cfg)

        deck._handle_command("/gamemode on")
        assert deck._game_mode_active is True
        assert "Game Mode" in deck.lbl_status_chip.text()
        assert any(p.get("type") == "power_saver" and p.get("active") is True for p in sent_payloads)

        deck._handle_command("/gamemode off")
        assert deck._game_mode_active is False
        assert "Online" in deck.lbl_status_chip.text()

        deck._handle_command("/gamemode auto")
        assert deck._game_mode_override is None
        assert deck.config.ui.game_mode_enabled is True

        deck._handle_command("/avatar auto on")
        assert deck.config.ui.auto_avatar_poses is True
        assert any(p.get("type") == "config_sync" and p.get("auto_avatar_poses") is True for p in sent_payloads)

        deck._handle_command("/avatar auto off")
        assert deck.config.ui.auto_avatar_poses is False

        deck.close()
