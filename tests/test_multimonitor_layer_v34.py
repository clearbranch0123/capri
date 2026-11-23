"""
Capri v3.4 Stage 4 — Multi-Monitor `zwlr_layer_shell_v1` Spatial 2.0 Tests.

Covers:
1. `find_adjacent_monitor()` and `map_y_between_monitors()` across standard dual displays,
   negative coordinates (left monitor at x=-1920), and mixed resolutions/offsets.
2. `AvatarPhysics2D.step()` seamless cross-monitor ballistic flight (`crossed_monitor_side = -1 | +1`)
   preserving `(vx, vy)` momentum when an adjacent monitor exists vs normal wall splat on outer edges.
3. Real Unix `socketpair()` Wayland wire-protocol verification of `WaylandLayerStage`:
   - Binding multiple `wl_output` globals & parsing `wl_output.name` (v4)
   - Live `switch_output()` destroying old surface and binding new `zwlr_layer_surface_v1` to target `wl_output`
   - Runtime hotplug disconnect (`wl_registry.global_remove`) automatically migrating to remaining output!
4. Quick-popover `Монитор` row hidden on single monitor and visible/cycling on multi-monitor,
   plus `CapriAvatarWidget._migrate_layer_to_monitor()` preserving ballistic flight velocity.
"""

from __future__ import annotations

import socket
import struct
import threading
from PySide6.QtWidgets import QApplication

from capri.core.compositor import MonitorWorkArea, find_adjacent_monitor, map_y_between_monitors
from capri.ui.avatar_physics import AvatarPhysics2D, PhysicsState
from capri.ui.avatar_popover import CapriAvatarQuickPopover
from capri.ui.overlay import CapriAvatarWidget
from capri.ui.wayland_layer import WaylandLayerStage, pack_wl_message, pack_wl_string


def test_monitor_adjacency_negative_coords_and_y_mapping():
    left_neg = MonitorWorkArea(name="HDMI-A-1", x=-1920.0, y=0.0, width=1920.0, height=1080.0, reserved_top=40.0)
    center = MonitorWorkArea(name="DP-1", x=0.0, y=0.0, width=1920.0, height=1080.0, reserved_top=45.0, is_focused=True)
    right_4k = MonitorWorkArea(name="DP-2", x=1920.0, y=-180.0, width=2560.0, height=1440.0, scale=1.5, reserved_top=0.0)
    mons = [left_neg, center, right_4k]

    # Center monitor sees left (-1920) and right (+1920) neighbors
    assert find_adjacent_monitor(center, -1, 500.0, mons) == left_neg
    assert find_adjacent_monitor(center, 1, 500.0, mons) == right_4k
    # Outer edges have NO neighbor
    assert find_adjacent_monitor(left_neg, -1, 500.0, mons) is None
    assert find_adjacent_monitor(right_4k, 1, 500.0, mons) is None
    # Single monitor never has neighbors
    assert find_adjacent_monitor(center, -1, 500.0, [center]) is None
    assert find_adjacent_monitor(center, 1, 500.0, [center]) is None

    # Vertically staggered / disjoint monitors MUST NOT bridge across empty space!
    staggered = MonitorWorkArea(name="HDMI-A-2", x=1920.0, y=1200.0, width=1920.0, height=1080.0)
    assert find_adjacent_monitor(center, 1, 500.0, [center, staggered]) is None
    # Only overlaps when y_ref actually falls inside the neighbor's vertical span
    partial = MonitorWorkArea(name="HDMI-A-3", x=1920.0, y=600.0, width=1920.0, height=1080.0)
    assert find_adjacent_monitor(center, 1, 300.0, [center, partial]) is None
    assert find_adjacent_monitor(center, 1, 750.0, [center, partial]) == partial

    # Vertical Y mapping accounts for different work_top and clamps cleanly inside destination
    mapped_y = map_y_between_monitors(300.0, center, right_4k, avatar_h=420.0)
    # global_y = 45 + 300 = 345; dst.work_top = -180 -> dst_local = 525
    assert abs(mapped_y - 525.0) < 1e-3
    # Out-of-range top/bottom clamps cleanly
    assert map_y_between_monitors(0.0, right_4k, center, avatar_h=420.0) == 0.0
    assert map_y_between_monitors(1200.0, right_4k, center, avatar_h=420.0) == center.work_height - 420.0


def test_physics_cross_monitor_flight_vs_outer_wall_splat():
    mon = MonitorWorkArea(name="DP-1", x=0.0, y=0.0, width=1920.0, height=1035.0)

    # 1. Right seam with adjacent monitor -> crosses cleanly preserving velocity
    p1 = AvatarPhysics2D(x=1610.0, y=300.0)
    p1.state = PhysicsState.FALLING
    p1.vx = 950.0
    p1.vy = -80.0
    res_r = p1.step(0.02, mon, adjacent_right=True)
    assert res_r.crossed_monitor_side == 1
    assert res_r.wall_hit is False
    assert p1.state == PhysicsState.FALLING
    assert p1.vx > 800.0

    # 2. Left seam with adjacent monitor -> crosses cleanly preserving velocity
    p2 = AvatarPhysics2D(x=5.0, y=300.0)
    p2.state = PhysicsState.FALLING
    p2.vx = -950.0
    p2.vy = -60.0
    res_l = p2.step(0.02, mon, adjacent_left=True)
    assert res_l.crossed_monitor_side == -1
    assert res_l.wall_hit is False
    assert p2.state == PhysicsState.FALLING
    assert p2.vx < -800.0

    # 3. Outer wall (adjacent_right=False) -> normal WALL_SPLAT unchanged!
    p3 = AvatarPhysics2D(x=1610.0, y=300.0)
    p3.state = PhysicsState.FALLING
    p3.vx = 1100.0
    p3.vy = 0.0
    res_wall = p3.step(0.02, mon, adjacent_right=False)
    assert res_wall.crossed_monitor_side == 0
    assert res_wall.wall_hit is True
    assert p3.state == PhysicsState.WALL_SPLAT


def test_wayland_layer_multi_output_switch_and_hotplug_protocol(monkeypatch, tmp_path):
    """Real Unix AF_UNIX Wayland wire-protocol test for wl_output binding, switch_output(), and global_remove hotplug."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    sock_path = str(tmp_path / "wayland-test-0")
    listen_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listen_sock.bind(sock_path)
    listen_sock.listen(1)
    listen_sock.settimeout(3.0)

    bound_outputs: dict[int, int] = {}      # global_name -> client obj_id
    layer_surface_outputs: list[int] = []   # output obj_ids passed to get_layer_surface
    released_outputs: list[int] = []
    registry_id_box: list[int] = [0]
    layer_shell_id_box: list[int] = [0]
    server_sock_box: list[socket.socket] = []
    done_ev = threading.Event()

    def _fake_compositor():
        buf = bytearray()
        serial = 100
        try:
            conn, _ = listen_sock.accept()
            server_sock_box.append(conn)
            conn.settimeout(0.2)
            while not done_ev.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= 8:
                    oid, size_op = struct.unpack_from("<II", buf, 0)
                    sz = size_op >> 16
                    op = size_op & 0xFFFF
                    if sz < 8 or len(buf) < sz:
                        break
                    payload = bytes(buf[8:sz])
                    del buf[:sz]

                    # wl_display.get_registry -> announce compositor, shm, seat, layer_shell + 2 wl_outputs!
                    if oid == 1 and op == 1 and len(payload) >= 4:
                        reg_id = struct.unpack_from("<I", payload, 0)[0]
                        registry_id_box[0] = reg_id
                        ann = b"".join([
                            pack_wl_message(reg_id, 0, struct.pack("<I", 1) + pack_wl_string("wl_compositor") + struct.pack("<I", 4)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 2) + pack_wl_string("wl_shm") + struct.pack("<I", 1)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 3) + pack_wl_string("wl_seat") + struct.pack("<I", 5)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 4) + pack_wl_string("zwlr_layer_shell_v1") + struct.pack("<I", 4)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 10) + pack_wl_string("wl_output") + struct.pack("<I", 4)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 11) + pack_wl_string("wl_output") + struct.pack("<I", 4)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 12) + pack_wl_string("wl_output") + struct.pack("<I", 4)),
                        ])
                        conn.sendall(ann)
                    elif oid == 1 and op == 0 and len(payload) >= 4:  # wl_display.sync
                        cb_id = struct.unpack_from("<I", payload, 0)[0]
                        conn.sendall(pack_wl_message(cb_id, 0, struct.pack("<I", 0)))
                    elif oid == registry_id_box[0] and op == 0 and len(payload) >= 8:  # wl_registry.bind
                        gname, slen = struct.unpack_from("<II", payload, 0)
                        iface = payload[8 : 8 + slen - 1].decode("utf-8", errors="replace")
                        padded = (slen + 3) & ~3
                        _ver, new_oid = struct.unpack_from("<II", payload, 8 + padded)
                        if iface == "zwlr_layer_shell_v1":
                            layer_shell_id_box[0] = new_oid
                        elif iface == "wl_output":
                            bound_outputs[gname] = new_oid
                            cname = {10: "DP-1", 11: "HDMI-A-1", 12: "STALLED-1"}.get(gname, f"OUT-{gname}")
                            # Send wl_output.name (op == 4)
                            conn.sendall(pack_wl_message(new_oid, 4, pack_wl_string(cname)))
                    elif oid == layer_shell_id_box[0] and op == 0 and len(payload) >= 16:  # get_layer_surface
                        ls_id, _surf_id, out_oid, _layer = struct.unpack_from("<IIII", payload, 0)
                        layer_surface_outputs.append(out_oid)
                        # Simulate unresponsive output 12 (never sends configure!) to verify safe rollback
                        if out_oid == bound_outputs.get(12):
                            continue
                        serial += 1
                        w, h = (2560, 1440) if out_oid == bound_outputs.get(11) else (1920, 1035)
                        conn.sendall(pack_wl_message(ls_id, 0, struct.pack("<III", serial, w, h)))
                    elif oid in bound_outputs.values() and op == 0:  # wl_output.release
                        released_outputs.append(oid)
        except Exception:
            pass

    srv_thread = threading.Thread(target=_fake_compositor, daemon=True)
    srv_thread.start()

    monkeypatch.setattr("capri.ui.wayland_layer.get_wayland_socket_path", lambda: sock_path)
    stage = WaylandLayerStage(None)
    monkeypatch.setattr(stage, "_recreate_buffers", lambda w, h: None)
    switched_events: list[str] = []
    stage.output_switched.connect(lambda name: switched_events.append(name))

    try:
        # 1. Start targeting DP-1 -> discovers DP-1, HDMI-A-1 & STALLED-1 and configures to 1920x1035
        assert stage.start(timeout=1.5, preferred_output="DP-1") is True
        assert sorted(stage.available_outputs()) == ["DP-1", "HDMI-A-1", "STALLED-1"]
        assert stage.active_output_name == "DP-1"
        assert (stage.width, stage.height) == (1920, 1035)
        assert layer_surface_outputs[-1] == bound_outputs[10]

        # 2. Timed-out switch to unresponsive STALLED-1 MUST return False AND keep DP-1 active & intact!
        prev_ls_id = stage._layer_surface_id
        prev_surf_id = stage._surface_id
        assert stage.switch_output("STALLED-1", timeout=0.15) is False
        assert stage.is_active is True
        assert stage.active_output_name == "DP-1"
        assert (stage.width, stage.height) == (1920, 1035)
        assert stage._layer_surface_id == prev_ls_id
        assert stage._surface_id == prev_surf_id

        # 3. Switch to HDMI-A-1 -> binds new layer surface to output 11 and resizes to 2560x1440!
        assert stage.switch_output("HDMI-A-1", timeout=1.5) is True
        assert stage.active_output_name == "HDMI-A-1"
        assert (stage.width, stage.height) == (2560, 1440)
        assert layer_surface_outputs[-1] == bound_outputs[11]
        assert switched_events[-1] == "HDMI-A-1"

        # 4. Hotplug disconnect of active HDMI-A-1 (`wl_registry.global_remove` for global 11)
        #    -> automatically migrates back to remaining DP-1 without disconnecting!
        server_sock_box[0].sendall(pack_wl_message(registry_id_box[0], 1, struct.pack("<I", 11)))
        assert stage._pump_until(lambda: stage.active_output_name == "DP-1", timeout=1.5) is True
        assert sorted(stage.available_outputs()) == ["DP-1", "STALLED-1"]
        assert (stage.width, stage.height) == (1920, 1035)
    finally:
        done_ev.set()
        stage.close()
        for s in server_sock_box:
            try:
                s.close()
            except Exception:
                pass
        try:
            listen_sock.close()
        except Exception:
            pass
        srv_thread.join(timeout=1.0)


def test_wayland_layer_stage_hotplug_during_in_flight_switch(monkeypatch, tmp_path):
    """
    Regression: if `wl_registry.global_remove` unplugs the currently active output WHILE
    `switch_output()` is mid-transaction waiting for candidate `configure`:
      1. When candidate configures normally -> no re-entrant switch clobbers `_pending_switch_*`,
         outer `switch_output()` returns True and lands cleanly on the target output.
      2. When candidate stalls/times out -> outer `switch_output()` returns False AND deferred
         hotplug recovery migrates cleanly onto a surviving output instead of stranding the stage.
    """
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    sock_path = str(tmp_path / "wl-hotplug-race.sock")
    listen_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listen_sock.bind(sock_path)
    listen_sock.listen(1)
    listen_sock.settimeout(3.0)

    done_ev = threading.Event()
    server_sock_box: list[socket.socket] = []
    bound_outputs: dict[int, int] = {}
    layer_surface_outputs: list[int] = []
    registry_id_box = [0]
    layer_shell_id_box = [0]

    def _fake_compositor():
        try:
            conn, _ = listen_sock.accept()
            conn.settimeout(0.05)
            server_sock_box.append(conn)
            buf = bytearray()
            serial = 1
            while not done_ev.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= 8:
                    oid, size_op = struct.unpack_from("<II", buf, 0)
                    sz = size_op >> 16
                    op = size_op & 0xFFFF
                    if len(buf) < sz:
                        break
                    payload = bytes(buf[8:sz])
                    del buf[:sz]

                    if oid == 1 and op == 1 and len(payload) >= 4:
                        reg_id = struct.unpack_from("<I", payload, 0)[0]
                        registry_id_box[0] = reg_id
                        ann = b"".join([
                            pack_wl_message(reg_id, 0, struct.pack("<I", 1) + pack_wl_string("wl_compositor") + struct.pack("<I", 4)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 2) + pack_wl_string("wl_shm") + struct.pack("<I", 1)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 3) + pack_wl_string("wl_seat") + struct.pack("<I", 5)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 4) + pack_wl_string("zwlr_layer_shell_v1") + struct.pack("<I", 4)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 10) + pack_wl_string("wl_output") + struct.pack("<I", 4)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 11) + pack_wl_string("wl_output") + struct.pack("<I", 4)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 12) + pack_wl_string("wl_output") + struct.pack("<I", 4)),
                            pack_wl_message(reg_id, 0, struct.pack("<I", 13) + pack_wl_string("wl_output") + struct.pack("<I", 4)),
                        ])
                        conn.sendall(ann)
                    elif oid == 1 and op == 0 and len(payload) >= 4:
                        cb_id = struct.unpack_from("<I", payload, 0)[0]
                        conn.sendall(pack_wl_message(cb_id, 0, struct.pack("<I", 0)))
                    elif oid == registry_id_box[0] and op == 0 and len(payload) >= 8:
                        gname, slen = struct.unpack_from("<II", payload, 0)
                        iface = payload[8 : 8 + slen - 1].decode("utf-8", errors="replace")
                        padded = (slen + 3) & ~3
                        _ver, new_oid = struct.unpack_from("<II", payload, 8 + padded)
                        if iface == "zwlr_layer_shell_v1":
                            layer_shell_id_box[0] = new_oid
                        elif iface == "wl_output":
                            bound_outputs[gname] = new_oid
                            cname = {10: "DP-1", 11: "HDMI-A-1", 12: "DP-2", 13: "STALLED-1"}[gname]
                            conn.sendall(pack_wl_message(new_oid, 4, pack_wl_string(cname)))
                    elif oid == layer_shell_id_box[0] and op == 0 and len(payload) >= 16:
                        ls_id, _surf_id, out_oid, _layer = struct.unpack_from("<IIII", payload, 0)
                        layer_surface_outputs.append(out_oid)
                        if out_oid == bound_outputs.get(11):
                            # Scenario 1: unplug active DP-1 (10) BEFORE configuring candidate HDMI-A-1 (11)!
                            serial += 1
                            conn.sendall(
                                pack_wl_message(registry_id_box[0], 1, struct.pack("<I", 10))
                                + pack_wl_message(ls_id, 0, struct.pack("<III", serial, 2560, 1440))
                            )
                        elif out_oid == bound_outputs.get(13):
                            # Scenario 2: unplug active HDMI-A-1 (11) mid-switch, and NEVER configure STALLED-1 (13)!
                            conn.sendall(pack_wl_message(registry_id_box[0], 1, struct.pack("<I", 11)))
                        elif out_oid == bound_outputs.get(12):
                            serial += 1
                            conn.sendall(pack_wl_message(ls_id, 0, struct.pack("<III", serial, 3840, 2160)))
                        else:
                            serial += 1
                            conn.sendall(pack_wl_message(ls_id, 0, struct.pack("<III", serial, 1920, 1035)))
        except Exception:
            pass

    srv_thread = threading.Thread(target=_fake_compositor, daemon=True)
    srv_thread.start()

    monkeypatch.setattr("capri.ui.wayland_layer.get_wayland_socket_path", lambda: sock_path)
    stage = WaylandLayerStage(None)
    monkeypatch.setattr(stage, "_recreate_buffers", lambda w, h: None)
    switched_events: list[str] = []
    stage.output_switched.connect(lambda name: switched_events.append(name))

    try:
        assert stage.start(timeout=1.5, preferred_output="DP-1") is True
        assert stage.active_output_name == "DP-1"

        # 1. Switch DP-1 -> HDMI-A-1 while DP-1 is unplugged mid-flight -> MUST succeed cleanly on HDMI-A-1!
        assert stage.switch_output("HDMI-A-1", timeout=1.5) is True
        assert stage.is_active is True
        assert stage.active_output_name == "HDMI-A-1"
        assert (stage.width, stage.height) == (2560, 1440)
        assert switched_events == ["HDMI-A-1"]
        assert "DP-1" not in stage.available_outputs()

        # 2. Switch HDMI-A-1 -> STALLED-1 while HDMI-A-1 is unplugged mid-flight -> returns False for STALLED-1,
        #    then deferred hotplug recovery automatically migrates stage onto surviving DP-2!
        assert stage.switch_output("STALLED-1", timeout=0.15) is False
        assert stage.is_active is True
        assert stage.active_output_name == "DP-2"
        assert (stage.width, stage.height) == (3840, 2160)
        assert switched_events == ["HDMI-A-1", "DP-2"]
        assert "HDMI-A-1" not in stage.available_outputs()
    finally:
        done_ev.set()
        stage.close()
        for s in server_sock_box:
            try:
                s.close()
            except Exception:
                pass
        try:
            listen_sock.close()
        except Exception:
            pass
        srv_thread.join(timeout=1.0)


def test_popover_monitor_row_visibility_and_widget_migration(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CAPRI_DISABLE_LAYER_SHELL", "1")
    QApplication.instance() or QApplication([])

    # 1. Single monitor -> Монитор row stays hidden; 2+ monitors -> visible with active badge
    pop = CapriAvatarQuickPopover(None)
    common = dict(
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
    )
    pop.sync_from_state(**common, available_monitors=["DP-1"], active_monitor="DP-1")
    assert pop.row_monitor.isHidden() is True
    pop.sync_from_state(**common, available_monitors=["DP-1", "HDMI-A-1"], active_monitor="HDMI-A-1")
    assert pop.row_monitor.isHidden() is False
    assert pop.row_monitor.lbl_pill.text() == "HDMI-A-1"
    pop.close()

    # 2. Widget ballistic cross-monitor migration preserves (vx, vy) & places on opposite edge
    m1 = MonitorWorkArea(name="DP-1", x=0.0, y=0.0, width=1920.0, height=1080.0, reserved_top=45.0, is_focused=True)
    m2 = MonitorWorkArea(name="HDMI-A-1", x=1920.0, y=0.0, width=2560.0, height=1440.0, reserved_top=0.0)

    class _FakeStage:
        def __init__(self):
            self.width = 1920
            self.height = 1035
            self.active_output_name = "DP-1"
            self.is_active = True
            self.switched_to: list[str] = []

        def available_outputs(self):
            return ["DP-1", "HDMI-A-1"]

        def switch_output(self, name: str) -> bool:
            self.switched_to.append(name)
            self.active_output_name = name
            if name == "HDMI-A-1":
                self.width, self.height = 2560, 1440
            else:
                self.width, self.height = 1920, 1035
            return True

        def render_avatar(self, *args, **kwargs):
            return True

        def close(self):
            pass

    widget = CapriAvatarWidget(mode="2d")
    try:
        stage = _FakeStage()
        widget.layer_stage = stage
        widget.using_layer_stage = True
        widget._known_monitors = [m1, m2]
        widget.physics.x = 1620.0
        widget.physics.y = 300.0
        widget.physics.vx = 920.0
        widget.physics.vy = -120.0
        widget.physics.state = PhysicsState.FALLING

        assert widget._migrate_layer_to_monitor(m2, side=1) is True
        assert stage.switched_to == ["HDMI-A-1"]
        assert widget.physics.x == 4.0
        assert widget.physics.vx == 920.0
        assert widget.physics.vy == -120.0
        assert widget.physics.state == PhysicsState.FALLING
    finally:
        widget.close()
