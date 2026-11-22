"""
Capri v3.3 Stage 2 — Deterministic Spatial Surface Map & Perching Tests (Niri & Hyprland).

Covers:
- Hyprland surface extraction (floating + tiled windows with headroom, ignoring top-hugging/narrow/fullscreen/own windows)
- Niri surface extraction (floating top edges + stacked vertical column split seams without cluttering top-row tiled headers)
- Multi-monitor & negative coordinates with `stage_origin_offset` translation
- Magnetic landing while falling onto window ledges & direct gentle drop-snap into `perch` pose
- Riding along smoothly when the perched window moves
- Losing footing (`lost_footing=True`) with startled drop when the perched window closes (and clean snap in `reduced_motion`)
- Offscreen `CapriAvatarWidget` integration preserving perched pose against MPRIS background polling
"""

from __future__ import annotations

import os
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from capri.core.compositor import MonitorWorkArea
from capri.ui.avatar_physics import AvatarPhysics2D, PhysicsState
from capri.ui.overlay import CapriAvatarWidget
from capri.ui.surface_map import SurfaceKind, build_surface_map


def test_build_surface_map_hyprland_floating_and_tiled_with_filters():
    mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1080.0, reserved_top=36.0)
    windows = [
        # Own avatar window -> must be excluded
        {
            "id": "0xown",
            "app_id": "capri-avatar",
            "pid": 1001,
            "workspace_id": 1,
            "is_floating": True,
            "is_fullscreen": False,
            "layout": {"tile_pos_in_workspace_view": [1500.0, 600.0], "tile_size": [300.0, 420.0]},
        },
        # Floating window with generous headroom -> FLOATING_TOP priority 3
        {
            "id": "0xflt",
            "app_id": "kitty",
            "pid": 2002,
            "workspace_id": 1,
            "is_floating": True,
            "is_fullscreen": False,
            "layout": {"tile_pos_in_workspace_view": [400.0, 460.0], "tile_size": [700.0, 420.0]},
        },
        # Bottom-half tiled window with headroom -> TILED_TOP priority 2
        {
            "id": "0xtile_bot",
            "app_id": "code",
            "pid": 2003,
            "workspace_id": 1,
            "is_floating": False,
            "is_fullscreen": False,
            "layout": {"tile_pos_in_workspace_view": [980.0, 560.0], "tile_size": [900.0, 480.0]},
        },
        # Top-row tiled window hugging top bar (wy=44 < work_top+110) -> skipped!
        {
            "id": "0xtile_top",
            "app_id": "firefox",
            "pid": 2004,
            "workspace_id": 1,
            "is_floating": False,
            "is_fullscreen": False,
            "layout": {"tile_pos_in_workspace_view": [20.0, 44.0], "tile_size": [940.0, 1000.0]},
        },
        # Too-narrow tooltip floating window -> skipped!
        {
            "id": "0xnarrow",
            "app_id": "popup",
            "pid": 2005,
            "workspace_id": 1,
            "is_floating": True,
            "is_fullscreen": False,
            "layout": {"tile_pos_in_workspace_view": [200.0, 500.0], "tile_size": [120.0, 80.0]},
        },
        # Window on inactive workspace -> skipped!
        {
            "id": "0xotherws",
            "app_id": "spotify",
            "pid": 2006,
            "workspace_id": 2,
            "is_floating": True,
            "is_fullscreen": False,
            "layout": {"tile_pos_in_workspace_view": [300.0, 500.0], "tile_size": [600.0, 400.0]},
        },
    ]

    surfaces = build_surface_map(
        windows,
        mon,
        compositor_kind="hyprland",
        active_workspace_id=1,
        avatar_pid=1001,
    )
    ids = [s.surface_id for s in surfaces]
    assert "win:0xflt:top" in ids
    assert "win:0xtile_bot:top" in ids
    assert "floor:DP-1" in ids
    assert "win:0xown:top" not in ids
    assert "win:0xtile_top:top" not in ids
    assert "win:0xnarrow:top" not in ids
    assert "win:0xotherws:top" not in ids
    # Priority ordering: floating top > tiled top > floor
    assert surfaces[0].kind == SurfaceKind.FLOATING_TOP
    assert surfaces[1].kind == SurfaceKind.TILED_TOP
    assert surfaces[-1].kind == SurfaceKind.FLOOR


def test_build_surface_map_niri_floating_and_vertical_split_seam():
    mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1035.0)
    windows = [
        # Top-row tiled window in column 1 -> no shelf above it (keeps top-row unobstructed)
        {
            "id": 10,
            "app_id": "kitty",
            "workspace_id": 1,
            "is_floating": False,
            "is_fullscreen": False,
            "layout": {
                "pos_in_scrolling_layout": [1, 1],
                "tile_size": [920.0, 480.0],
                "tile_pos_in_workspace_view": None,
            },
        },
        # Stacked lower tiled window in column 1 ([1, 2]) -> creates NIRI_SPLIT_SEAM!
        {
            "id": 11,
            "app_id": "telegram",
            "workspace_id": 1,
            "is_floating": False,
            "is_fullscreen": False,
            "layout": {
                "pos_in_scrolling_layout": [1, 2],
                "tile_size": [920.0, 480.0],
                "tile_pos_in_workspace_view": None,
            },
        },
        # Floating Picture-in-Picture / dialog window -> FLOATING_TOP!
        {
            "id": 12,
            "app_id": "mpv",
            "workspace_id": 1,
            "is_floating": True,
            "is_fullscreen": False,
            "layout": {
                "tile_size": [520.0, 320.0],
                "tile_pos_in_workspace_view": [1120.0, 480.0],
            },
        },
    ]

    surfaces = build_surface_map(windows, mon, compositor_kind="niri", active_workspace_id=1)
    by_id = {s.surface_id: s for s in surfaces}
    assert "win:12:top" in by_id
    assert by_id["win:12:top"].kind == SurfaceKind.FLOATING_TOP
    assert by_id["win:12:top"].y == 480.0
    assert "niri_seam:11" in by_id
    assert by_id["niri_seam:11"].kind == SurfaceKind.NIRI_SPLIT_SEAM
    assert by_id["niri_seam:11"].y == 16.0 + 480.0 + 16.0  # top margin + row1 height + gap = 512.0
    assert "niri_seam:10" not in by_id
    assert "floor:DP-1" in by_id

    # Multi-column Niri workspace WITHOUT tile_pos_in_workspace_view must NOT invent phantom seams,
    # while multi-column windows WITH verified tile_pos_in_workspace_view ARE preserved!
    multi_col_windows = [
        {
            "id": 20,
            "app_id": "kitty",
            "workspace_id": 1,
            "is_floating": False,
            "is_fullscreen": False,
            "layout": {"pos_in_scrolling_layout": [1, 1], "tile_size": [900.0, 480.0], "tile_pos_in_workspace_view": None},
        },
        {
            "id": 21,
            "app_id": "telegram",
            "workspace_id": 1,
            "is_floating": False,
            "is_fullscreen": False,
            "layout": {"pos_in_scrolling_layout": [1, 2], "tile_size": [900.0, 480.0], "tile_pos_in_workspace_view": None},
        },
        {
            "id": 22,
            "app_id": "firefox",
            "workspace_id": 1,
            "is_floating": False,
            "is_fullscreen": False,
            "layout": {"pos_in_scrolling_layout": [2, 1], "tile_size": [900.0, 480.0], "tile_pos_in_workspace_view": None},
        },
        {
            "id": 23,
            "app_id": "code",
            "workspace_id": 1,
            "is_floating": False,
            "is_fullscreen": False,
            "layout": {"pos_in_scrolling_layout": [2, 2], "tile_size": [900.0, 480.0], "tile_pos_in_workspace_view": [960.0, 520.0]},
        },
    ]
    mc_surfaces = build_surface_map(multi_col_windows, mon, compositor_kind="niri", active_workspace_id=1)
    mc_ids = {s.surface_id for s in mc_surfaces}
    assert "niri_seam:21" not in mc_ids  # unverified X on multi-column -> skipped safely!
    assert "niri_seam:23" in mc_ids      # verified tile_pos_in_workspace_view -> kept!
    assert "floor:DP-1" in mc_ids


def test_falling_lands_on_window_shelf_perches_rides_along_and_falls_on_close():
    mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1035.0)
    win_initial = [
        {
            "id": 42,
            "app_id": "kitty",
            "workspace_id": 1,
            "is_floating": True,
            "is_fullscreen": False,
            "layout": {"tile_pos_in_workspace_view": [600.0, 560.0], "tile_size": [600.0, 320.0]},
        }
    ]
    surfaces = build_surface_map(win_initial, mon, compositor_kind="niri", active_workspace_id=1)

    phys = AvatarPhysics2D(avatar_w=300.0, avatar_h=420.0, x=750.0, y=40.0)
    phys.begin_grab(750.0, 40.0)
    rel = phys.end_grab(shift_held=False, monitor=mon, surfaces=surfaces)
    assert rel.state == PhysicsState.FALLING

    landed_res = None
    for _ in range(240):
        res = phys.step(1 / 60.0, mon, surfaces=surfaces)
        if res.landed:
            landed_res = res
        if phys.state == PhysicsState.IDLE:
            break

    assert landed_res is not None
    assert landed_res.surface_id == "win:42:top"
    assert landed_res.preferred_pose == "perch"
    assert phys.perched is True
    expected_perch_y = 560.0 - 420.0 + phys.cfg.perch_seat_offset
    assert abs(phys.y - expected_perch_y) < 1e-6

    # 2. Move the underlying window gently (+50px right, +20px down) -> Capri rides along!
    win_moved = [
        {
            "id": 42,
            "app_id": "kitty",
            "workspace_id": 1,
            "is_floating": True,
            "is_fullscreen": False,
            "layout": {"tile_pos_in_workspace_view": [650.0, 580.0], "tile_size": [600.0, 320.0]},
        }
    ]
    moved_surfaces = build_surface_map(win_moved, mon, compositor_kind="niri", active_workspace_id=1)
    ride_res = phys.sync_surfaces(moved_surfaces, mon)
    assert ride_res.lost_footing is False
    assert abs(phys.x - 800.0) < 1e-6
    assert abs(phys.y - (expected_perch_y + 20.0)) < 1e-6
    assert phys.perched is True

    # 3. Close the underlying window -> loses footing, startled hop, falls onto monitor floor in IDLE!
    empty_surfaces = build_surface_map([], mon, compositor_kind="niri", active_workspace_id=1)
    drop_res = phys.sync_surfaces(empty_surfaces, mon)
    assert drop_res.lost_footing is True
    assert phys.state == PhysicsState.FALLING
    assert phys.perched is False
    assert phys.vy < 0.0  # startled micro-hop

    for _ in range(240):
        res = phys.step(1 / 60.0, mon, surfaces=empty_surfaces)
        if phys.state == PhysicsState.IDLE:
            break
    assert phys.state == PhysicsState.IDLE
    assert phys.perched is False
    assert abs(phys.y - (1035.0 - 420.0)) < 1e-6


def test_direct_drop_snap_and_reduced_motion_lost_footing():
    mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1035.0)
    win = [
        {
            "id": 99,
            "app_id": "firefox",
            "workspace_id": 1,
            "is_floating": True,
            "is_fullscreen": False,
            "layout": {"tile_pos_in_workspace_view": [500.0, 540.0], "tile_size": [640.0, 340.0]},
        }
    ]
    surfaces = build_surface_map(win, mon, compositor_kind="niri", active_workspace_id=1)

    # 1. Gentle drop right near window top edge snaps magnetically into perch!
    phys = AvatarPhysics2D(avatar_w=300.0, avatar_h=420.0, x=660.0, y=145.0)
    phys.begin_grab(660.0, 145.0)
    snap_res = phys.end_grab(shift_held=False, monitor=mon, surfaces=surfaces)
    assert snap_res.landed is True
    assert snap_res.preferred_pose == "perch"
    assert phys.perched is True

    # 2. With reduced_motion=True, closing window snaps straight to floor with zero hop/bounce
    phys.set_reduced_motion(True)
    phys.state = PhysicsState.IDLE
    floor_only = build_surface_map([], mon, compositor_kind="niri", active_workspace_id=1)
    rm_res = phys.sync_surfaces(floor_only, mon)
    assert rm_res.lost_footing is True
    assert rm_res.landed is True
    assert phys.state == PhysicsState.IDLE
    assert abs(phys.y - (1035.0 - 420.0)) < 1e-6


def test_overlay_stage2_perch_pose_preservation_and_lost_footing_reaction(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])

    widget = CapriAvatarWidget(mode="2d")
    try:
        mon = MonitorWorkArea("DP-1", x=0.0, y=0.0, width=1920.0, height=1035.0)
        win = [
            {
                "id": 55,
                "app_id": "kitty",
                "workspace_id": 1,
                "is_floating": True,
                "is_fullscreen": False,
                "layout": {"tile_pos_in_workspace_view": [500.0, 540.0], "tile_size": [640.0, 340.0]},
            }
        ]
        surfaces = build_surface_map(win, mon, compositor_kind="niri", active_workspace_id=1)
        widget.physics.begin_grab(650.0, 145.0)
        res = widget.physics.end_grab(shift_held=False, monitor=mon, surfaces=surfaces)
        widget._apply_landing_pose(res)
        assert widget.canvas_v2.pose == "perch"

        # Background auto-pose sync must NOT overwrite active window perch back to "normal"!
        widget.physics.state = PhysicsState.IDLE
        widget._sync_auto_pose()
        assert widget.canvas_v2.pose == "perch"

        # When underlying window closes, _poll_perched_surfaces drops Capri and switches pose to normal
        with patch.object(widget, "_stage_monitor", return_value=mon), \
             patch.object(widget, "_refresh_surface_map", return_value=build_surface_map([], mon)):
            widget._poll_perched_surfaces()
        assert widget.physics.state == PhysicsState.FALLING
        assert widget.canvas_v2.pose == "normal"
        assert widget.caption_text != ""
        app.processEvents()
    finally:
        widget.close()
