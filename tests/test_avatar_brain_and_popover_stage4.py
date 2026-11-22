"""
Capri v3.3 Stage 4 — Autonomous Desktop Life Brain & Crimson Graphite Quick-Popover Tests.

Covers:
- AvatarLifeBrain hard safety gates (autonomous_enabled, game_mode, reduced_motion, pinned / /stay,
  active TTS speaking/listening/thinking, manual_pose_override, media_playing, cooldown)
- Floor strolls, window shelf hops, lounging, and dropping back to floor (100% deterministic via seeded RNG)
- Minimalist Crimson Graphite CapriAvatarQuickPopover UI sync, pose chip toggles, behavior switches,
  pin/unpin, home, HUD, and close signals
- Offscreen CapriAvatarWidget integration for autonomous walking & popover control actions
"""

from __future__ import annotations

import random

from PySide6.QtWidgets import QApplication

from capri.core.compositor import MonitorWorkArea
from capri.ui.avatar_brain import AvatarLifeBrain, BrainAction
from capri.ui.avatar_physics import PhysicsState
from capri.ui.avatar_popover import CapriAvatarQuickPopover
from capri.ui.overlay import CapriAvatarWidget
from capri.ui.surface_map import LandingSurface, SurfaceKind, build_surface_map


def test_avatar_brain_safety_gates_block_unsolicited_motion():
    mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1035.0)
    surfaces = build_surface_map([], mon, compositor_kind="niri")
    brain = AvatarLifeBrain(min_interval_s=5.0, max_interval_s=5.0, interaction_cooldown_s=10.0, rng=random.Random(1))
    brain._next_action_at = 50.0

    common = dict(
        now=100.0,
        last_interaction_ts=10.0,
        avatar_state="idle",
        physics_state=PhysicsState.IDLE,
        x=1200.0,
        y=615.0,
        current_pose="normal",
        active_surface=None,
        surfaces=surfaces,
        monitor=mon,
    )

    # Every single gate MUST suppress autonomous motion cleanly
    assert brain.tick(**common, game_mode_active=True).action == BrainAction.NONE
    assert brain.tick(**common, reduced_motion=True).action == BrainAction.NONE
    assert brain.tick(**common, pinned=True).action == BrainAction.NONE
    assert brain.tick(**{**common, "avatar_state": "speaking"}).action == BrainAction.NONE
    assert brain.tick(**{**common, "avatar_state": "listening"}).action == BrainAction.NONE
    assert brain.tick(**{**common, "avatar_state": "thinking"}).action == BrainAction.NONE
    assert brain.tick(**{**common, "physics_state": PhysicsState.FALLING}).action == BrainAction.NONE
    assert brain.tick(**common, manual_pose_override="sleep").action == BrainAction.NONE
    assert brain.tick(**common, media_playing=True).action == BrainAction.NONE
    assert brain.tick(**{**common, "last_interaction_ts": 95.0}).action == BrainAction.NONE  # within 10s cooldown

    brain.set_enabled(False)
    assert brain.tick(**common).action == BrainAction.NONE


def test_avatar_brain_deterministic_floor_walk_shelf_hop_and_drop():
    mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1035.0)
    win = [
        {
            "id": 77,
            "app_id": "kitty",
            "workspace_id": 1,
            "is_floating": True,
            "is_fullscreen": False,
            "layout": {"tile_pos_in_workspace_view": [500.0, 540.0], "tile_size": [680.0, 340.0]},
        }
    ]
    surfaces = build_surface_map(win, mon, compositor_kind="niri", active_workspace_id=1)
    shelf = next(s for s in surfaces if s.kind == SurfaceKind.FLOATING_TOP)

    brain = AvatarLifeBrain(
        min_interval_s=10.0,
        max_interval_s=10.0,
        interaction_cooldown_s=5.0,
        rng=random.Random(42),
    )

    seen_floor_actions: set[str] = set()
    now = 100.0
    brain._next_action_at = now
    for _ in range(25):
        dec = brain.tick(
            now=now,
            last_interaction_ts=0.0,
            avatar_state="idle",
            physics_state=PhysicsState.IDLE,
            x=900.0,
            y=615.0,
            current_pose="normal",
            active_surface=None,
            surfaces=surfaces,
            monitor=mon,
            mood="playful",
        )
        seen_floor_actions.add(dec.action)
        if dec.action == BrainAction.WALK:
            assert dec.target_x is not None and 16.0 <= dec.target_x <= 1920.0 - 300.0 - 16.0
            assert dec.target_y == 615.0
        elif dec.action == BrainAction.HOP_TO_SHELF:
            assert dec.target_surface == shelf
            assert dec.pose == "perch"
            assert dec.use_teleport is True
        now += 15.0

    assert BrainAction.WALK in seen_floor_actions
    assert BrainAction.HOP_TO_SHELF in seen_floor_actions

    # When perched on a shelf, brain can lounge, stroll wide shelf, or drop back to floor
    seen_shelf_actions: set[str] = set()
    for _ in range(20):
        dec = brain.tick(
            now=now,
            last_interaction_ts=0.0,
            avatar_state="idle",
            physics_state=PhysicsState.IDLE,
            x=680.0,
            y=158.0,
            current_pose="perch",
            active_surface=shelf,
            surfaces=surfaces,
            monitor=mon,
            mood="playful",
        )
        seen_shelf_actions.add(dec.action)
        now += 15.0
    assert BrainAction.DROP_TO_FLOOR in seen_shelf_actions


def test_crimson_graphite_quick_popover_controls_and_overlay_integration(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])

    pop = CapriAvatarQuickPopover()
    events = []
    pop.pose_toggled.connect(lambda p: events.append(("pose", p)))
    pop.physics_toggled.connect(lambda: events.append(("physics",)))
    pop.autonomous_toggled.connect(lambda: events.append(("autonomous",)))
    pop.follow_toggled.connect(lambda: events.append(("follow",)))
    pop.auto_poses_toggled.connect(lambda: events.append(("auto_poses",)))
    pop.game_mode_cycled.connect(lambda: events.append(("game_mode",)))
    pop.pin_toggled.connect(lambda: events.append(("pin",)))
    pop.home_requested.connect(lambda: events.append(("home",)))
    pop.open_dashboard_requested.connect(lambda: events.append(("hud",)))
    pop.close_avatar_requested.connect(lambda: events.append(("close",)))

    pop.sync_from_state(
        active_pose="perch",
        dance_active=False,
        physics_enabled=True,
        autonomous_enabled=True,
        follow_enabled=False,
        auto_poses_enabled=True,
        game_mode_override=None,
        power_saver_active=False,
        pinned=True,
        perched=False,
        has_lounge_sprite=False,
    )
    assert pop.lbl_state.text() == "PINNED"
    # "Лежать" stays hidden until a real custom lounge.png sprite exists
    assert pop.pose_buttons["lounge"].isHidden() is True
    assert pop.pose_buttons["perch"].property("active") == "true"
    assert pop.row_physics.lbl_pill.text() == "ON"
    assert pop.row_autonomous.lbl_pill.text() == "ON"
    assert pop.btn_pin.text() == "Открепить"

    # When custom lounge.png is present, "Лежать" appears cleanly!
    pop.sync_from_state(
        active_pose="lounge",
        dance_active=False,
        physics_enabled=True,
        autonomous_enabled=True,
        follow_enabled=False,
        auto_poses_enabled=True,
        game_mode_override=None,
        power_saver_active=False,
        pinned=True,
        perched=False,
        has_lounge_sprite=True,
    )
    assert pop.pose_buttons["lounge"].isHidden() is False
    assert pop.pose_buttons["lounge"].property("active") == "true"

    pop.pose_buttons["lounge"].click()
    pop.row_physics.click()
    pop.row_autonomous.click()
    pop.btn_pin.click()
    # Top-right ✕ ONLY closes the popover — NEVER shuts down the avatar!
    pop.btn_close.click()
    assert ("close",) not in events
    # Explicit "Выключить аватар" button emits close_avatar_requested, and "◈ Релик" opens Relic Construct
    assert "Релик" in pop.btn_hud.text()
    pop.btn_hud.click()
    pop.btn_quit_avatar.click()
    assert ("pose", "lounge") in events
    assert ("physics",) in events
    assert ("autonomous",) in events
    assert ("pin",) in events
    assert ("hud",) in events
    assert ("close",) in events
    pop.close()

    # Full widget wiring check: tight hitbox excludes empty sky above Capri's head
    widget = CapriAvatarWidget(mode="2d")
    try:
        hx, hy, hw, hh = widget._compute_avatar_hitbox(1000, 500)
        assert hx > 1000 and hy >= 596  # excludes empty speech-bubble sky above head & side margins
        assert hw < 200 and hh < 340
        widget.canvas_v2.set_pose("perch")
        px, py, pw, ph = widget._compute_avatar_hitbox(1000, 500)
        assert py > hy and ph < hh      # even tighter seated hitbox when perched!
        widget.canvas_v2.set_pose("normal")
        assert widget.brain.autonomous_enabled is True
        widget._toggle_autonomous_life()
        assert widget.brain.autonomous_enabled is False
        widget._toggle_physics_enabled()
        assert widget.physics_enabled is False
        assert widget.physics.physics_enabled is False
        widget._toggle_pin_in_place()
        assert widget.physics.pinned is True
        assert widget.physics.state == PhysicsState.PINNED
        widget._toggle_quick_pose("lounge")
        assert widget.canvas_v2.pose == "lounge"
        widget._toggle_quick_pose("lounge")
        assert widget.canvas_v2.pose == "normal"

        # Clicking "Сон" while state == "playful" MUST stay in sleep.png (never flash wake.png!)
        widget.set_state("playful", "Возвращаюсь на базу.")
        widget._toggle_quick_pose("sleep")
        assert widget.state == "idle"
        assert widget.canvas_v2.pose == "sleep"
        assert widget.canvas_v2.waking_active is False
        for _ in range(10):
            widget.canvas_v2.advance_animation()
        assert widget.canvas_v2.pose == "sleep"
        assert widget.canvas_v2.waking_active is False
        widget._toggle_quick_pose("sleep")  # wake cleanly
        assert widget.canvas_v2.pose == "normal"

        # Clicking "◈ Релик" opens CapriRelicDashboard cleanly
        widget.quick_popover.btn_hud.click()
        assert widget.dashboard.isVisible() is True
        widget.dashboard.hide()

        # Autonomous walk stops cleanly on TTS speaking state
        widget._start_autonomous_walk(800.0, 615.0)
        assert widget._auto_walk_target is not None
        widget.set_state("speaking", "Привет, D!")
        assert widget._auto_walk_target is None

        # Manual "lounge" pose MUST survive background _sync_auto_pose() calls
        widget.set_state("idle", "")
        widget._toggle_quick_pose("lounge")
        assert widget.manual_pose_override == "lounge"
        widget._sync_auto_pose()
        assert widget.canvas_v2.pose == "lounge"
        widget._toggle_quick_pose("lounge")  # reset manual override
        assert widget.manual_pose_override is None

        # Active autonomous walk & autonomous lounge survive idle _sync_auto_pose(),
        # and IMMEDIATELY stop when MPRIS music starts playing!
        from capri.core.presence import GameModeState, MediaPlaybackState, resolve_avatar_auto_pose

        assert resolve_avatar_auto_pose(manual_pose_override="lounge") == ("lounge", "manual_override")
        widget.physics.set_pinned(False)
        widget.physics.set_physics_enabled(True)
        widget.brain.set_enabled(True)
        widget.autonomous_life_enabled = True
        widget._start_autonomous_walk(900.0, 615.0)
        widget._sync_auto_pose()
        assert widget.canvas_v2.pose == "walk"
        assert widget._auto_walk_target is not None

        # MPRIS music starts -> cancels active walk immediately and transitions to perch_music!
        widget._apply_presence_snapshot(
            MediaPlaybackState(is_playing=True, title="Track", artist="Artist"),
            GameModeState(active=False),
        )
        assert widget._auto_walk_target is None
        assert widget.canvas_v2.pose == "perch_music"

        # Autonomous lounge also survives quiet _sync_auto_pose() when music stops
        widget._apply_presence_snapshot(MediaPlaybackState(is_playing=False), GameModeState(active=False))
        widget._autonomous_pose = "lounge"
        widget.canvas_v2.set_pose("lounge")
        widget._sync_auto_pose()
        assert widget.canvas_v2.pose == "lounge"
        app.processEvents()
    finally:
        widget.close()
