"""
Capri v3.3 Stage 1 — Deterministic Avatar Physics & Compositor Geometry Tests.

Covers:
- Logical multi-monitor / DPI / transform / panel reserved work-area normalization (Niri & Hyprland)
- Strict explicit target-ID and floating-window guards (never moving foreign/focused windows)
- Hyprland command socket (.socket.sock) vs event socket (.socket2.sock) separation
- 2D rigid-body + visual-spring physics lifecycle (grab pendulum/flail, toss, wall splat, floor bounce & settle)
- Compositor IPC throttling (<= 20 Hz + final exact settle, never flooding at 60 Hz)
- Switch semantics: Shift-pin, /stay, reduced_motion, Game Mode, and clean IPC-loss handling
- Offscreen Qt AvatarV2Canvas & CapriAvatarWidget integration without a live compositor
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication

from capri.core.compositor import (
    CompositorBackend,
    MonitorWorkArea,
    get_hyprland_command_socket_path,
    get_hyprland_socket2_path,
    normalize_hyprland_monitors,
    normalize_niri_outputs,
    resolve_monitor_for_point,
)
from capri.ui.avatar_physics import AvatarPhysics2D, PhysicsConfig, PhysicsState
from capri.ui.overlay import CapriAvatarWidget


def test_normalize_niri_outputs_skips_null_and_preserves_negative_dpi():
    raw_outputs = {
        "HDMI-A-1": {
            "name": "HDMI-A-1",
            "make": "Unknown",
            "logical": None,  # Disconnected output must be skipped!
        },
        "DP-2": {
            "name": "DP-2",
            "logical": {"x": -1920, "y": -120, "width": 1920, "height": 1080, "scale": 1.25, "transform": "Normal"},
            "reserved": [0, 32, 0, 40],
        },
        "DP-1": {
            "name": "DP-1",
            "logical": {"x": 0, "y": 0, "width": 2560, "height": 1440, "scale": 1.5, "transform": "Normal"},
        },
    }
    mons = normalize_niri_outputs(raw_outputs, focused_name="DP-1")
    assert len(mons) == 2
    by_name = {m.name: m for m in mons}
    assert "HDMI-A-1" not in by_name
    assert by_name["DP-2"].x == -1920.0
    assert by_name["DP-2"].y == -120.0
    assert by_name["DP-2"].scale == 1.25
    assert by_name["DP-2"].work_top == -88.0
    assert by_name["DP-2"].work_bottom == 920.0
    assert by_name["DP-1"].is_focused is True
    assert by_name["DP-2"].is_focused is False


def test_normalize_hyprland_monitors_scale_transform_reserved_negative():
    raw_mons = [
        {
            "name": "DP-1",
            "x": 0,
            "y": 0,
            "width": 3840,
            "height": 2160,
            "scale": 2.0,
            "transform": 0,
            "reserved": [0, 36, 0, 0],
            "focused": True,
        },
        {
            "name": "HDMI-A-1",
            "x": -1080,
            "y": -200,
            "width": 1920,
            "height": 1080,
            "scale": 1.0,
            "transform": 1,  # 90 deg portrait -> logical 1080x1920
            "reserved": [0, 0, 0, 48],
            "focused": False,
        },
        {
            "name": "eDP-1",
            "disabled": True,
            "width": 1920,
            "height": 1080,
            "scale": 1.0,
        },
    ]
    mons = normalize_hyprland_monitors(raw_mons)
    assert len(mons) == 2
    dp1, hdmi = mons[0], mons[1]
    assert (dp1.width, dp1.height) == (1920.0, 1080.0)
    assert dp1.work_top == 36.0
    assert dp1.work_bottom == 1080.0
    assert (hdmi.x, hdmi.y) == (-1080.0, -200.0)
    assert (hdmi.width, hdmi.height) == (1080.0, 1920.0)
    assert hdmi.work_bottom == -200.0 + 1920.0 - 48.0


def test_resolve_monitor_for_point_multi_monitor():
    left = MonitorWorkArea("DP-2", x=-1920.0, y=0.0, width=1920.0, height=1080.0)
    main = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=2560.0, height=1440.0, is_focused=True)

    assert resolve_monitor_for_point([left, main], -500.0, 400.0).name == "DP-2"
    assert resolve_monitor_for_point([left, main], 500.0, 400.0).name == "DP-1"
    # Outside both -> nearest monitor
    assert resolve_monitor_for_point([left, main], -2500.0, 200.0).name == "DP-2"
    # Empty list -> deterministic 1920x1080 default
    fallback = resolve_monitor_for_point([], 100.0, 100.0)
    assert (fallback.width, fallback.height) == (1920.0, 1080.0)


def test_compositor_strict_target_id_and_niri_wire_format():
    backend = CompositorBackend(forced_kind="niri")

    # Invalid / empty IDs must ALWAYS be rejected before touching IPC
    for bad_id in (None, "", 0, -5, False, "not-an-int"):
        assert backend.move_floating_window_sync(bad_id, 10, 10) is False
        assert backend.move_floating_window_exact_sync(bad_id, 100, 100) is False
        assert backend.ensure_window_floating_sync(bad_id) is False

    sent_payloads = []

    def fake_niri_sock(payload, timeout=0.25):
        sent_payloads.append(payload)
        return {"Ok": "Handled"}

    with patch("capri.core.compositor._niri_socket_request", side_effect=fake_niri_sock):
        assert backend.ensure_window_floating_sync(42) is True
        assert backend.move_floating_window_sync(42, 15, -8) is True
        assert backend.move_floating_window_exact_sync(42, 800, 620) is True

    assert sent_payloads[0] == {"Action": {"MoveWindowToFloating": {"id": 42}}}
    assert sent_payloads[1] == {
        "Action": {
            "MoveFloatingWindow": {
                "id": 42,
                "x": {"AdjustFixed": 15.0},
                "y": {"AdjustFixed": -8.0},
            }
        }
    }
    assert sent_payloads[2] == {
        "Action": {
            "MoveFloatingWindow": {
                "id": 42,
                "x": {"SetFixed": 800.0},
                "y": {"SetFixed": 620.0},
            }
        }
    }


def test_compositor_hyprland_command_socket_vs_socket2_and_explicit_address(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "sig-test")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    cmd_sock = get_hyprland_command_socket_path()
    ev_sock = get_hyprland_socket2_path()
    assert cmd_sock is not None and cmd_sock.name == ".socket.sock"
    assert ev_sock is not None and ev_sock.name == ".socket2.sock"
    assert cmd_sock != ev_sock

    backend = CompositorBackend(forced_kind="hyprland")
    # Malformed / empty addresses must be rejected
    for bad_addr in (None, "", "   ", "0x123,extra", "0x123;evil"):
        assert backend.move_floating_window_sync(bad_addr, 5, 5) is False
        assert backend.move_floating_window_exact_sync(bad_addr, 50, 50) is False
        assert backend.ensure_window_floating_sync(bad_addr) is False

    commands = []

    def fake_hypr_cmd(cmd, timeout=0.25):
        commands.append(cmd)
        return "ok"

    with patch("capri.core.compositor._hyprland_socket_command", side_effect=fake_hypr_cmd):
        assert backend.ensure_window_floating_sync("0x55abc123") is True
        assert backend.move_floating_window_sync("0x55abc123", 12, -4) is True
        assert backend.move_floating_window_exact_sync("0x55abc123", 400, 610) is True

    assert commands == [
        "dispatch setfloating address:0x55abc123",
        "dispatch movewindowpixel 12 -4,address:0x55abc123",
        "dispatch movewindowpixel exact 400 610,address:0x55abc123",
    ]


def test_physics_grab_pendulum_throw_wall_splat_and_floor_bounce_settle():
    mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1080.0, reserved_top=30.0, reserved_bottom=40.0)
    phys = AvatarPhysics2D(avatar_w=300.0, avatar_h=420.0, x=800.0, y=200.0)

    # 1. Native compositor drag observation -> tracks velocity cleanly with zero jitter/tilt and zero IPC writes
    phys.begin_grab(800.0, 200.0)
    assert phys.state == PhysicsState.GRABBED
    cur_x = 800.0
    for _ in range(10):
        cur_x += 22.0
        r = phys.observe_external_drag(cur_x, 190.0, dt=1 / 60.0)
        assert r.ipc_move_dx == 0 and r.ipc_move_dy == 0
        assert r.tilt_deg == 0.0
    assert r.facing == 1
    assert phys.vx > 200.0

    # 2. Toss toward right wall -> hits wall, triggers WALL_SPLAT, then falls & bounces to IDLE
    phys.x = 1320.0
    phys.vx = 920.0
    phys.vy = -120.0
    rel = phys.end_grab(shift_held=False, monitor=mon)
    assert rel.state == PhysicsState.FALLING

    saw_wall_splat = False
    saw_bounce = False
    saw_land = False
    for _ in range(360):  # up to 6 simulated seconds
        res = phys.step(1 / 60.0, mon)
        assert abs(res.tilt_deg) <= phys.cfg.max_tilt_deg + 1e-6
        if res.wall_hit and res.state == PhysicsState.WALL_SPLAT:
            saw_wall_splat = True
        if res.bounced:
            saw_bounce = True
        if res.landed:
            saw_land = True
        if saw_land and phys.state == PhysicsState.IDLE:
            break

    assert saw_wall_splat is True
    assert saw_bounce is True
    assert saw_land is True
    assert phys.state == PhysicsState.IDLE
    expected_floor_y = mon.work_bottom - 420.0  # 1080 - 40 - 420 = 620
    assert abs(phys.y - expected_floor_y) < 1e-6


def test_niri_workspace_bar_inset_inference():
    backend = CompositorBackend(forced_kind="niri")
    raw_outputs = {
        "Ok": {
            "Outputs": {
                "DP-1": {
                    "name": "DP-1",
                    "logical": {"x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0},
                }
            }
        }
    }
    raw_windows = {
        "Ok": {
            "Windows": [
                {
                    "id": 3,
                    "app_id": "firefox",
                    "is_floating": False,
                    "is_fullscreen": False,
                    "layout": {"tile_size": [1920.0, 1035.0], "window_size": [1920, 1035]},
                }
            ]
        }
    }

    def fake_niri(req, timeout=0.25):
        if req == "FocusedOutput":
            return {"Ok": {"FocusedOutput": {"name": "DP-1"}}}
        if req == "Outputs":
            return raw_outputs
        if req == "Windows":
            return raw_windows
        return None

    with patch("capri.core.compositor._niri_socket_request", side_effect=fake_niri):
        mons = backend.get_monitors()
    assert len(mons) == 1
    assert mons[0].work_bottom == 1035.0
    assert mons[0].work_bottom - 420.0 == 615.0


def test_ballistic_fling_arc_and_long_hold_stability():
    mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1080.0, reserved_bottom=45.0)
    phys = AvatarPhysics2D(avatar_w=300.0, avatar_h=420.0, x=300.0, y=500.0)

    # 1. Holding stationary in mid-air for 2.0s (120 60Hz ticks) NEVER drops GRABBED prematurely
    t = 10.0
    phys.begin_grab(300.0, 200.0, timestamp=t)
    for _ in range(120):
        t += 1 / 60.0
        st = phys.step(1 / 60.0, mon)
        assert st.state == PhysicsState.GRABBED

    # 2. Drag LEFT first, then flick RIGHT & UP right before release -> MUST fly RIGHT (never inverted!)
    x, y = 1200.0, 480.0
    phys.begin_grab(x, y, timestamp=t)
    # Earlier leftward movement (should age out of recent throw window)
    for _ in range(8):
        t += 0.05
        x -= 90.0
        phys.observe_external_drag(x, y, dt=0.05, timestamp=t)
    # Now flick briskly RIGHT and UP over the last 100ms
    for _ in range(3):
        t += 0.05
        x += 70.0   # ~1400 px/s right
        y -= 40.0   # ~-800 px/s up
        phys.observe_external_drag(x, y, dt=0.05, timestamp=t)
        # Interleaved 60Hz step during GRABBED must NOT destroy throw velocity
        phys.step(1 / 60.0, mon)

    rel = phys.end_grab(shift_held=False, monitor=mon)
    assert rel.state == PhysicsState.FALLING
    assert phys.vx > 800.0    # Flies RIGHT, matching the final flick!
    assert phys.vy < -400.0

    # Verify she genuinely rises upward AND travels right before landing
    min_y_reached = phys.y
    max_x_reached = phys.x
    for _ in range(360):
        phys.step(1 / 60.0, mon)
        min_y_reached = min(min_y_reached, phys.y)
        max_x_reached = max(max_x_reached, phys.x)
        if phys.state == PhysicsState.IDLE:
            break
    assert min_y_reached < y - 60.0
    assert max_x_reached > x + 350.0
    assert phys.state == PhysicsState.IDLE
    assert abs(phys.y - 615.0) < 1e-6

    # 3. Gentle slow placement (< min_throw_speed) -> calm vertical drop, NOT flung!
    t += 1.0
    phys.begin_grab(700.0, 150.0, timestamp=t)
    for _ in range(4):
        t += 0.05
        phys.observe_external_drag(phys.x + 6.0, 150.0, dt=0.05, timestamp=t)  # 120 px/s gentle move
    phys.end_grab(shift_held=False, monitor=mon)
    assert phys.state == PhysicsState.FALLING
    assert abs(phys.vx) < 1e-6 and abs(phys.vy) < 1e-6

    # 4. Fast drag followed by brief pause (>= 90ms) before releasing -> clean vertical drop!
    t += 1.0
    phys.begin_grab(700.0, 150.0, timestamp=t)
    t += 0.05
    phys.observe_external_drag(820.0, 150.0, dt=0.05, timestamp=t)
    for _ in range(3):
        t += 0.05  # 150ms held still
        phys.observe_external_drag(820.0, 150.0, dt=0.05, timestamp=t)
    phys.end_grab(shift_held=False, monitor=mon)
    assert phys.state == PhysicsState.FALLING
    assert abs(phys.vx) < 1e-6 and abs(phys.vy) < 1e-6


def test_physics_shift_release_pins_in_air_and_negative_monitor_floor():
    left_mon = MonitorWorkArea(
        "DP-2", x=-1920.0, y=-100.0, width=1920.0, height=1080.0, reserved_bottom=48.0
    )
    phys = AvatarPhysics2D(avatar_w=300.0, avatar_h=420.0, x=-1200.0, y=50.0)

    # Shift + release pins in mid-air (no gravity fall)
    phys.begin_grab(-1200.0, 50.0)
    res_pin = phys.end_grab(shift_held=True, monitor=left_mon)
    assert res_pin.state == PhysicsState.PINNED
    assert res_pin.ipc_set_exact == (-1200, 50)
    for _ in range(60):
        st = phys.step(1 / 60.0, left_mon)
        assert st.state == PhysicsState.PINNED
        assert st.y == 50.0
        assert st.ipc_move_dx == 0 and st.ipc_move_dy == 0

    # Normal release on negative monitor falls onto that monitor's work_bottom
    phys.begin_grab(-1200.0, 50.0)
    phys.end_grab(shift_held=False, monitor=left_mon)
    for _ in range(300):
        phys.step(1 / 60.0, left_mon)
        if phys.state == PhysicsState.IDLE:
            break
    expected_floor_y = left_mon.work_bottom - 420.0  # (-100 + 1080 - 48) - 420 = 512.0
    assert phys.state == PhysicsState.IDLE
    assert -1920.0 <= phys.x <= -300.0
    assert abs(phys.y - expected_floor_y) < 1e-6


def test_physics_ipc_throttling_never_floods_at_60hz():
    mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1080.0)
    cfg = PhysicsConfig(ipc_sync_interval=0.05, ipc_min_delta_px=4.0)
    phys = AvatarPhysics2D(avatar_w=300.0, avatar_h=420.0, x=500.0, y=20.0, config=cfg)

    phys.begin_grab(500.0, 20.0)
    phys.end_grab(shift_held=False, monitor=mon)

    ipc_calls_first_sec = 0
    exact_calls = 0
    for idx in range(180):  # up to 3.0 simulated seconds @ 60 Hz
        res = phys.step(1 / 60.0, mon)
        if res.ipc_move_dx != 0 or res.ipc_move_dy != 0:
            if idx < 60:
                ipc_calls_first_sec += 1
        if res.ipc_set_exact is not None:
            exact_calls += 1
        if phys.state == PhysicsState.IDLE:
            break

    # Strictly <= 20 Hz throttled relative moves during the first 60 frames (1.0s)!
    assert ipc_calls_first_sec <= 20
    # Final settle emits exact landing placement
    assert exact_calls == 1
    assert phys.state == PhysicsState.IDLE
    assert abs(phys.y - (mon.work_bottom - 420.0)) < 1e-6


def test_physics_switches_reduced_motion_game_mode_and_ipc_loss():
    mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1080.0)

    # 1. reduced_motion=True -> snaps straight to floor with zero pendulum/squash/bounce
    phys_rm = AvatarPhysics2D(avatar_w=300.0, avatar_h=420.0, x=600.0, y=100.0, reduced_motion=True)
    phys_rm.begin_grab(600.0, 100.0)
    drag_r = phys_rm.update_grab(40.0, 0.0, dt=1 / 60.0, monitor=mon)
    assert drag_r.tilt_deg == 0.0
    assert drag_r.scale_x == 1.0 and drag_r.scale_y == 1.0
    rel_r = phys_rm.end_grab(shift_held=False, monitor=mon)
    assert rel_r.state == PhysicsState.IDLE
    assert rel_r.landed is True
    assert rel_r.y == mon.work_bottom - 420.0
    assert rel_r.impact_ripple == 0.0

    # 2. Game Mode stops active falling cleanly
    phys_gm = AvatarPhysics2D(avatar_w=300.0, avatar_h=420.0, x=600.0, y=100.0)
    phys_gm.begin_grab(600.0, 100.0)
    phys_gm.end_grab(shift_held=False, monitor=mon)
    assert phys_gm.state == PhysicsState.FALLING
    phys_gm.set_game_mode(True)
    assert phys_gm.state == PhysicsState.IDLE
    st_gm = phys_gm.step(1 / 60.0, mon)
    assert st_gm.ipc_move_dx == 0 and st_gm.ipc_move_dy == 0

    # 3. IPC loss disables compositor move requests without raising
    phys_no_ipc = AvatarPhysics2D(avatar_w=300.0, avatar_h=420.0, x=600.0, y=100.0)
    phys_no_ipc.set_ipc_available(False)
    phys_no_ipc.begin_grab(600.0, 100.0)
    phys_no_ipc.end_grab(shift_held=False, monitor=mon)
    for _ in range(180):
        r = phys_no_ipc.step(1 / 60.0, mon)
        assert r.ipc_move_dx == 0 and r.ipc_move_dy == 0
        assert r.ipc_set_exact is None


def test_overlay_verified_floating_guard_ipc_loss_and_canvas_paint(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])

    widget = CapriAvatarWidget(mode="2d")
    try:
        # 1. Foreign window (wrong pid & wrong app_id) MUST NEVER be moved
        foreign_win = {"id": 999, "pid": os.getpid() + 10000, "app_id": "firefox", "is_floating": True}
        with patch.object(widget.locomotion.observer, "get_avatar_window", return_value=foreign_win):
            assert widget._resolve_verified_floating_avatar_id() is None
            assert widget.physics.ipc_available is False

        # 2. Own avatar window that is tiled -> must be floated first before accepting its ID
        tiled_own = {"id": 77, "pid": os.getpid(), "app_id": "capri-avatar", "is_floating": False}
        with patch.object(widget.locomotion.observer, "get_avatar_window", return_value=tiled_own), \
             patch("capri.ui.overlay.compositor.ensure_window_floating_sync", return_value=True) as mock_float:
            assert widget._resolve_verified_floating_avatar_id() == 77
            mock_float.assert_called_once_with(77)
            assert widget.physics.ipc_available is True

        # 3. If IPC move fails (compositor lost), physics marks IPC unavailable cleanly
        widget._verified_avatar_id = 77
        mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1080.0)
        widget.physics.begin_grab(500.0, 200.0)
        pin_res = widget.physics.end_grab(shift_held=True, monitor=mon)
        with patch("capri.ui.overlay.compositor.move_floating_window_exact_sync", return_value=False):
            assert widget._apply_physics_ipc_move(pin_res) is False
            assert widget.physics.ipc_available is False
            assert widget._verified_avatar_id is None

        # 4. /stay pins physics cleanly
        widget.physics.set_ipc_available(True)
        with patch.object(widget.locomotion, "get_current_coords", return_value=(700.0, 300.0)):
            widget.locomotion.stay()
            assert widget.physics.pinned is True
            assert widget.physics.state == PhysicsState.PINNED

        # 5. Offscreen QPainter render with active tilt/squash/flail/ripple AND reduced_motion
        widget.canvas_v2.resize(300, 420)
        widget.canvas_v2.set_physics_visual(
            state=PhysicsState.GRABBED,
            tilt_deg=18.5,
            scale_x=0.92,
            scale_y=1.08,
            flail_phase=1.2,
            facing=-1,
            canvas_offset_y=-4.0,
            impact_ripple=0.8,
        )
        img = QImage(300, 420, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        widget.canvas_v2.render(img)
        assert not img.isNull()

        widget.canvas_v2.set_reduced_motion(True)
        assert widget.canvas_v2.physics_tilt_deg == 0.0
        assert widget.canvas_v2.physics_impact_ripple == 0.0
        widget.canvas_v2.render(img)
        # 6. Echo-free drag step & stationary release -> ZERO runaway and ZERO phantom throw
        widget._verified_avatar_id = 77
        widget.physics.set_ipc_available(True)
        widget._active_monitor = mon
        widget._drag_active = True
        widget._drag_press_local = QPointF(150.0, 200.0)
        widget._drag_start_coords = (600.0, 200.0)
        widget._drag_last_ts = 100.0
        widget._drag_ignore_until = 0.0
        widget.physics.begin_grab(600.0, 200.0, timestamp=100.0)

        moves = []
        with patch(
            "capri.ui.overlay.compositor.move_floating_window_exact_sync",
            side_effect=lambda wid, x, y: (moves.append((wid, x, y)) or True),
        ):
            # Step 1: user moves +60px right -> commits once, clears latest, sets 22ms echo guard
            widget._drag_latest_local = QPointF(210.0, 200.0)
            widget._commit_drag_step(now_mono=100.05, local_pos=widget._drag_latest_local)
            assert moves == [(77, 660, 200)]
            assert widget._drag_latest_local is None
            assert widget._drag_ignore_until > 100.07

            # Step 2 & 3: user stops moving for 100ms -> stationary samples zero velocity cleanly
            widget._commit_drag_step(now_mono=100.10, local_pos=None)
            widget._commit_drag_step(now_mono=100.15, local_pos=None)
            assert moves == [(77, 660, 200)]  # ZERO runaway extra moves!
            assert abs(widget.physics.vx) < 1e-6 and abs(widget.physics.vy) < 1e-6
        widget._drag_active = False
        app.processEvents()
    finally:
        widget.close()


def test_wayland_layer_stage_protocol_and_pointer_drag_physics():
    import struct
    from capri.ui.wayland_layer import (
        BTN_LEFT,
        KEY_LEFTSHIFT,
        WaylandLayerStage,
        pack_wl_message,
        pack_wl_string,
        unpack_wl_fixed,
    )

    # 1. Wire protocol encoding helpers
    s_bytes = pack_wl_string("capri")
    assert len(s_bytes) == 12  # 4 (len=6) + 6 ("capri\0") + 2 pad
    assert struct.unpack_from("<I", s_bytes, 0)[0] == 6
    msg = pack_wl_message(7, 3, struct.pack("<I", 42))
    assert struct.unpack("<III", msg) == (7, (12 << 16) | 3, 42)
    assert unpack_wl_fixed(25600) == 100.0
    assert unpack_wl_fixed(-1280) == -5.0

    # 2. Simulate compositor events directly into WaylandLayerStage (deterministic, zero live WM!)
    app = QApplication.instance() or QApplication([])
    stage = WaylandLayerStage()
    stage._layer_surface_id = 10
    stage._pointer_id = 11
    stage._keyboard_id = 12
    stage._compositor_id = 5
    stage._shm_id = 6
    stage._surface_id = 9

    presses = []
    moves = []
    releases = []
    stage.pointer_press.connect(lambda x, y, b, sh: presses.append((x, y, b, sh)))
    stage.pointer_move.connect(lambda x, y, t: moves.append((x, y, t)))
    stage.pointer_release.connect(lambda x, y, b, sh: releases.append((x, y, b, sh)))

    # Pointer enter -> move -> Left Shift key press -> LMB press -> drag -> LMB release
    stage._dispatch_event(11, 0, struct.pack("<IIii", 1, 9, 500 * 256, 300 * 256))
    stage._dispatch_event(12, 3, struct.pack("<IIII", 2, 100, KEY_LEFTSHIFT, 1))
    assert stage.shift_held is True
    stage._dispatch_event(11, 3, struct.pack("<IIII", 3, 101, BTN_LEFT, 1))
    stage._dispatch_event(11, 2, struct.pack("<Iii", 102, 620 * 256, 260 * 256))
    stage._dispatch_event(11, 3, struct.pack("<IIII", 4, 103, BTN_LEFT, 0))
    app.processEvents()

    assert presses == [(500.0, 300.0, BTN_LEFT, True)]
    assert len(moves) == 1 and moves[0][:2] == (620.0, 260.0)
    assert releases == [(620.0, 260.0, BTN_LEFT, True)]

    # Shift release clears shift_held cleanly
    stage._dispatch_event(12, 3, struct.pack("<IIII", 5, 104, KEY_LEFTSHIFT, 0))
    assert stage.shift_held is False
    stage.close()


def test_1000hz_gaming_mouse_coalescing_preserves_throw_and_stop():
    mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1035.0)
    phys = AvatarPhysics2D(avatar_w=300.0, avatar_h=420.0, x=400.0, y=300.0)

    # Simulate 1000 Hz mouse moving right at 900 px/s (0.9 px per 1ms event) for 100ms
    t = 50.0
    phys.begin_grab(400.0, 300.0, timestamp=t)
    x = 400.0
    for _ in range(100):
        t += 0.001
        x += 0.9
        phys.observe_external_drag(x, 300.0, dt=0.001, timestamp=t)
    rel = phys.end_grab(shift_held=False, monitor=mon)
    assert rel.state == PhysicsState.FALLING
    assert 650.0 < phys.vx < 900.0  # ~900 * 0.85 = 765 px/s, NOT zeroed by 1ms steps!

    # Same 1000 Hz mouse pausing for 110ms before release -> drops strictly vertically
    t += 1.0
    phys.begin_grab(400.0, 300.0, timestamp=t)
    x = 400.0
    for _ in range(60):
        t += 0.001
        x += 1.2
        phys.observe_external_drag(x, 300.0, dt=0.001, timestamp=t)
    for _ in range(110):
        t += 0.001
        phys.observe_external_drag(x, 300.0, dt=0.001, timestamp=t)
    phys.end_grab(shift_held=False, monitor=mon)
    assert abs(phys.vx) < 1e-6 and abs(phys.vy) < 1e-6


def test_wayland_layer_stage_never_reuses_busy_buffer_and_returns_kb_focus():
    import array
    import socket
    import struct
    from PySide6.QtGui import QColor
    from capri.ui.wayland_layer import (
        BTN_LEFT,
        KB_INTERACTIVITY_NONE,
        KB_INTERACTIVITY_ON_DEMAND,
        WaylandLayerStage,
        pack_wl_message,
    )

    app = QApplication.instance() or QApplication([])
    client_sock, peer_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client_sock.setblocking(False)
    peer_sock.settimeout(0.5)

    stage = WaylandLayerStage()
    stage._sock = client_sock
    stage._compositor_id = 5
    stage._compositor_ver = 4
    stage._shm_id = 6
    stage._layer_shell_id = 7
    stage._layer_shell_ver = 4
    stage._surface_id = 9
    stage._layer_surface_id = 10
    stage._pointer_id = 11
    stage._keyboard_id = 12
    stage._active = True

    def _drain_peer_messages() -> list[tuple[int, int, bytes]]:
        msgs: list[tuple[int, int, bytes]] = []
        raw = bytearray()
        peer_sock.setblocking(False)
        try:
            while True:
                data, ancdata, _, _ = peer_sock.recvmsg(65536, socket.CMSG_SPACE(128))
                for lvl, typ, cdata in ancdata:
                    if lvl == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                        fds = array.array("i")
                        fds.frombytes(cdata[: len(cdata) - (len(cdata) % fds.itemsize)])
                        for fd in fds:
                            os.close(fd)
                if not data:
                    break
                raw.extend(data)
        except BlockingIOError:
            pass
        finally:
            peer_sock.setblocking(True)
        idx = 0
        while idx + 8 <= len(raw):
            oid, size_op = struct.unpack_from("<II", raw, idx)
            sz = size_op >> 16
            op = size_op & 0xFFFF
            if sz < 8 or idx + sz > len(raw):
                break
            msgs.append((oid, op, bytes(raw[idx + 8 : idx + sz])))
            idx += sz
        return msgs

    try:
        # 1. Configure 320x440 logical stage -> creates 2 wl_shm buffers & sends SCM_RIGHTS fds
        stage._dispatch_event(10, 0, struct.pack("<III", 1, 320, 440))
        assert stage.is_active is True
        assert len(stage._buffers) == 2
        _drain_peer_messages()

        paint_calls = []

        def _paint_marker(painter, w, h, tag):
            paint_calls.append(tag)
            painter.fillRect(0, 0, 8, 8, QColor(255, 0, 0, 255))

        # 2. Render 2 frames -> both buffers become busy
        assert stage.render_avatar(lambda p, w, h: _paint_marker(p, w, h, "f1"), x=0, y=0, w=300, h=420) is True
        assert stage.render_avatar(lambda p, w, h: _paint_marker(p, w, h, "f2"), x=4, y=4, w=300, h=420) is True
        assert all(b.busy for b in stage._buffers)
        snap0 = bytes(stage._buffers[0].mm[:64])
        snap1 = bytes(stage._buffers[1].mm[:64])
        _drain_peer_messages()

        # 3. Attempt a 3rd frame while BOTH buffers are busy -> MUST NOT touch busy buffer memory!
        assert stage.render_avatar(lambda p, w, h: _paint_marker(p, w, h, "f3"), x=10, y=10, w=300, h=420) is False
        assert paint_calls == ["f1", "f2"]
        assert bytes(stage._buffers[0].mm[:64]) == snap0
        assert bytes(stage._buffers[1].mm[:64]) == snap1
        assert stage._pending_frame is not None

        # 4. Compositor sends wl_buffer.release on buffer 0 -> deferred frame "f3" flushes cleanly!
        buf0_id = stage._buffers[0].buffer_id
        peer_sock.sendall(pack_wl_message(buf0_id, 0))
        stage._read_and_dispatch()
        assert paint_calls == ["f1", "f2", "f3"]
        assert stage._pending_frame is None
        assert stage._buffers[0].busy is True

        # 5. clear_frame while both buffers busy -> commits empty input region immediately & defers clear
        stage._buffers[0].busy = True
        stage._buffers[1].busy = True
        stage.clear_frame()
        assert stage._pending_clear is True
        assert stage._last_input_mode == "empty"
        peer_sock.sendall(pack_wl_message(buf0_id, 0))
        stage._read_and_dispatch()
        assert stage._pending_clear is False

        # 6. Keyboard interactivity starts at NONE, switches to ON_DEMAND only while hovered,
        #    and returns to NONE immediately upon button release / pointer leave!
        assert stage._kb_interactivity == KB_INTERACTIVITY_NONE
        stage._dispatch_event(11, 0, struct.pack("<IIii", 10, 9, 100 * 256, 100 * 256))  # pointer.enter
        assert stage._kb_interactivity == KB_INTERACTIVITY_ON_DEMAND
        stage._dispatch_event(11, 3, struct.pack("<IIII", 11, 200, BTN_LEFT, 1))         # button press
        stage._dispatch_event(11, 3, struct.pack("<IIII", 12, 220, BTN_LEFT, 0))         # button release
        assert stage._kb_interactivity == KB_INTERACTIVITY_NONE
        kb_msgs = [
            struct.unpack("<I", pl)[0]
            for oid, op, pl in _drain_peer_messages()
            if oid == 10 and op == 4 and len(pl) == 4
        ]
        assert KB_INTERACTIVITY_ON_DEMAND in kb_msgs
        assert kb_msgs[-1] == KB_INTERACTIVITY_NONE
        app.processEvents()
    finally:
        stage.close()
        try:
            peer_sock.close()
        except Exception:
            pass
