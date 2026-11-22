"""
Capri v3.3 Stage 3 — Procedural Cyber-Hologram Dynamics & Drop-In Custom Sprite-Sheet Tests.

Covers:
- Auto-sliced drop-in PNG sprite slots (`drag.png`, `fall.png`, `splat.png`, `lounge.png`, `walk.png`)
  without requiring any manifest file
- Optional `poses.json` manifest overriding frame count, `cell_w`, filename, and FPS divisor
- Graceful fallback to existing bundled `sprites_v2/` when custom drop-in files are absent
- Offscreen rendering of motion-trail afterimages on fast throws, chromatic RGB glitch slices
  on wall splat, and dual-ring holographic landing shockwave + sparks
- Strict `reduced_motion=True` suppression of all procedural effects
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from capri.ui.avatar_physics import PhysicsState
from capri.ui.sprite_view import AvatarV2Canvas


def _make_strip_png(path: Path, frames: int, cell_w: int = 240, cell_h: int = 384, rgb=(240, 60, 120)) -> None:
    img = QImage(frames * cell_w, cell_h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    for i in range(frames):
        p.fillRect(i * cell_w + 40, 40, cell_w - 80, cell_h - 80, QColor(rgb[0], rgb[1], rgb[2], 255))
    p.end()
    assert img.save(str(path), "PNG") is True


def _count_non_transparent_pixels(img: QImage) -> int:
    non_zero = 0
    for y in range(0, img.height(), 4):
        for x in range(0, img.width(), 4):
            if img.pixelColor(x, y).alpha() > 0:
                non_zero += 1
    return non_zero


def test_drop_in_custom_sprite_slots_auto_slicing_and_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    host = QWidget()
    host.state = "idle"
    host.caption_text = ""

    # 1. Empty user override dir -> custom slots empty, bundled sprites loaded cleanly
    empty_dir = tmp_path / "empty_sprites"
    empty_dir.mkdir()
    canvas = AvatarV2Canvas(host, user_sprites_dir=empty_dir)
    slots = canvas.get_loaded_sprite_slots()
    assert slots["idle"] == 16
    assert slots["dance"] == 16
    assert slots["perch"] == 60
    assert slots["drag"] == 0
    assert slots["lounge"] == 0

    # 2. Drop in zero-config PNG strips (no poses.json) -> auto-sliced accurately!
    custom_dir = tmp_path / "custom_sprites"
    custom_dir.mkdir()
    _make_strip_png(custom_dir / "drag.png", frames=6, cell_w=240, cell_h=384)
    _make_strip_png(custom_dir / "fall.png", frames=4, cell_w=240, cell_h=384)
    _make_strip_png(custom_dir / "splat.png", frames=1, cell_w=240, cell_h=384)
    _make_strip_png(custom_dir / "lounge.png", frames=5, cell_w=240, cell_h=384)
    _make_strip_png(custom_dir / "walk.png", frames=8, cell_w=240, cell_h=384)

    slots2 = canvas.reload_sprites(user_sprites_dir=custom_dir)
    assert slots2["drag"] == 6
    assert slots2["fall"] == 4
    assert slots2["splat"] == 1
    assert slots2["lounge"] == 5
    assert slots2["walk"] == 8

    # 3. Custom poses.json manifest overrides filenames, cell_w, frame counts & FPS divisor
    _make_strip_png(custom_dir / "my_lounge_sheet.png", frames=3, cell_w=200, cell_h=384)
    (custom_dir / "poses.json").write_text(
        json.dumps({"lounge": {"file": "my_lounge_sheet.png", "frames": 3, "cell_w": 200, "fps": 12}}),
        encoding="utf-8",
    )
    slots3 = canvas.reload_sprites(user_sprites_dir=custom_dir)
    assert slots3["lounge"] == 3
    assert canvas._slot_tick_divs["lounge"] == 5  # round(60 / 12)

    # 4. Malformed poses.json values (strings/booleans/negatives) must NOT crash startup — fallback to auto-slice!
    (custom_dir / "poses.json").write_text(
        json.dumps(
            {
                "drag": {"file": "drag.png", "frames": "bad", "cell_w": -10, "fps": "oops"},
                "fall": {"file": "fall.png", "frames": True, "cell_w": "nan"},
            }
        ),
        encoding="utf-8",
    )
    slots4 = canvas.reload_sprites(user_sprites_dir=custom_dir)
    assert slots4["drag"] == 6  # cleanly fell back to auto-slicing!
    assert slots4["fall"] == 4
    host.close()


def test_procedural_effects_render_and_reduced_motion_suppression(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    host = QWidget()
    host.state = "idle"
    host.caption_text = ""
    canvas = AvatarV2Canvas(host, user_sprites_dir=tmp_path)
    canvas.resize(300, 420)

    def _render_snapshot() -> QImage:
        img = QImage(300, 420, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        canvas.render_to_painter(p, 300, 420)
        p.end()
        return img

    # 1. Fast throw -> motion-trail afterimages widen non-transparent silhouette vs stationary fall
    canvas.set_reduced_motion(False)
    canvas.set_physics_visual(state=PhysicsState.FALLING, vx=0.0, vy=100.0)
    img_slow = _render_snapshot()
    canvas.set_physics_visual(state=PhysicsState.FALLING, vx=1250.0, vy=-350.0)
    img_fast = _render_snapshot()
    assert _count_non_transparent_pixels(img_fast) > _count_non_transparent_pixels(img_slow)

    # 2. Wall splat -> chromatic RGB glitch slices render cleanly without errors
    canvas.set_physics_visual(state=PhysicsState.WALL_SPLAT, scale_x=0.84, scale_y=1.14)
    img_splat = _render_snapshot()
    assert _count_non_transparent_pixels(img_splat) > 50

    # 3. Landing -> dual-ring holographic shockwave + sparks render cleanly
    canvas.set_physics_visual(state=PhysicsState.LANDING, impact_ripple=0.85, scale_x=1.08, scale_y=0.92)
    img_land = _render_snapshot()
    assert _count_non_transparent_pixels(img_land) > 50

    # 4. reduced_motion=True suppresses vx/vy motion trail, ripple, tilt, and squash cleanly
    canvas.set_reduced_motion(True)
    canvas.set_physics_visual(state=PhysicsState.FALLING, vx=1250.0, vy=-350.0, impact_ripple=0.9, tilt_deg=8.0)
    assert canvas.physics_vx == 0.0 and canvas.physics_vy == 0.0
    assert canvas.physics_impact_ripple == 0.0
    assert canvas.physics_tilt_deg == 0.0
    img_rm = _render_snapshot()
    assert _count_non_transparent_pixels(img_rm) > 50

    # 5. Lounge & walk poses accept cleanly with bundled fallbacks
    canvas.set_pose("lounge")
    assert canvas.pose == "lounge"
    assert _count_non_transparent_pixels(_render_snapshot()) > 50
    canvas.set_pose("walk")
    assert canvas.pose == "walk"
    assert _count_non_transparent_pixels(_render_snapshot()) > 50
    host.close()
