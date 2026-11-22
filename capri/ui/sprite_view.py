from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QRect, QRectF, QPointF
from PySide6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QFont,
    QFontMetrics,
    QPixmap,
    QRadialGradient,
    QPolygonF,
)
from PySide6.QtWidgets import QWidget

from ..core.resources import get_asset_path

SPRITES_DIR = get_asset_path("sprites_v2")
USER_SPRITES_DIR = Path.home() / ".config" / "capri" / "sprites"


# Subtle mood-driven holographic ground aura colors (RGB, alpha)
_MOOD_AURA_RGB: dict[str, tuple[int, int, int, int]] = {
    "warm": (91, 205, 162, 82),       # soft mint-cyan
    "playful": (181, 155, 255, 84),   # lilac signal
    "sarcastic": (242, 119, 154, 86), # neon coral
    "annoyed": (233, 98, 118, 92),    # sharp crimson
    "neutral": (105, 213, 232, 72),   # obsidian cyan
}


class AvatarV2Canvas(QWidget):
    """
    Анимированная прозрачная фигурка Капри на рабочем столе (v2.9.0 — Living Presence 2.0).
    Поддерживает все спрайт-листы `capri/assets/sprites_v2/`:
    - 16 кадров плавного idle / breathing (`idle.png`)
    - 4 кадра естественного моргания (`blink.png`) и киберпанк-глитч моргания (`blink_glitch.png`)
    - 5 кадров артикуляции рта (`mouth.png`) с синхронизацией по реальной огибающей речи (`mouth_envelope`)
    - 10 кадров глитча (`glitch.png`)
    - 16 кадров танца (`dance.png`)
    - 16 кадров сна (`sleep.png`) и 13 кадров пробуждения (`wake.png`)
    - 60 кадров присеста (`perch.png`) и музыкального вайба (`perch_music.png`)
    - LIVING SIGNAL комикс-бабл реплик и эмоциональная голографическая аура
    """

    CELL_W = 240
    CELL_H = 384
    IDLE_SLEEP_TICKS = 60 * 180  # ~3 минуты полной тишины до мягкого перехода в sleep

    def __init__(
        self,
        parent_widget,
        target_w: int = 210,
        target_h: int = 336,
        user_sprites_dir: Optional[Path] = None,
    ):
        super().__init__(parent_widget)
        self.host = parent_widget
        self.target_w = target_w
        self.target_h = target_h
        self._user_sprites_dir: Path = Path(user_sprites_dir) if user_sprites_dir is not None else USER_SPRITES_DIR

        # Базовые спрайт-листы
        self.idle_frames: list[QPixmap] = []
        self.blink_frames: list[QPixmap] = []
        self.blink_glitch_frames: list[QPixmap] = []
        self.mouth_frames: list[QPixmap] = []
        self.glitch_frames: list[QPixmap] = []
        self.dance_frames: list[QPixmap] = []
        self.sleep_frames: list[QPixmap] = []
        self.wake_frames: list[QPixmap] = []
        self.perch_frames: list[QPixmap] = []
        self.perch_music_frames: list[QPixmap] = []

        # Опциональные drop-in слоты v3.3 (автонарезка или poses.json, с фоллбэком на базовые)
        self.drag_frames: list[QPixmap] = []
        self.fall_frames: list[QPixmap] = []
        self.splat_frames: list[QPixmap] = []
        self.lounge_frames: list[QPixmap] = []
        self.walk_frames: list[QPixmap] = []
        self._slot_tick_divs: dict[str, int] = {}

        self._load_all_sprites()

        # Анимационные счетчики
        self.tick_count: int = 0
        self.hover_phase: float = 0.0
        self.idle_ticks: int = 0

        # Состояния моргания (обычное и микро-глитч)
        self.blink_active: bool = False
        self.blink_is_glitch: bool = False
        self.blink_frame: int = 0
        self.blink_timer: int = random.randint(180, 360)

        # Состояния глитча и поз
        self.glitch_active: bool = False
        self.glitch_frame: int = 0
        self.dance_active: bool = False
        self.pose: str = "normal"  # "normal" | "perch" | "perch_music" | "lounge" | "walk" | "sleep"
        self.waking_active: bool = False
        self.wake_frame: int = 0

        # Эмоция и синхронизированный липсинк (0..4 envelope @ 15 FPS)
        self.mood: str = "neutral"
        self.mouth_envelope: list[int] = []
        self.mouth_env_pos: int = 0
        self.current_mouth_idx: int = 0

        # 60 Hz in-canvas physics visual transform state (zero compositor IPC cost)
        self.reduced_motion: bool = False
        self.physics_state: str = "idle"
        self.physics_tilt_deg: float = 0.0
        self.physics_scale_x: float = 1.0
        self.physics_scale_y: float = 1.0
        self.physics_flail_phase: float = 0.0
        self.physics_facing: int = 1
        self.physics_offset_y: float = 0.0
        self.physics_impact_ripple: float = 0.0
        self.physics_vx: float = 0.0
        self.physics_vy: float = 0.0

    def _load_pose_manifest(self) -> dict[str, dict]:
        """Load optional `poses.json` manifest (user override dir first, then bundled sprites_v2)."""
        manifest: dict[str, dict] = {}
        for base in (SPRITES_DIR, self._user_sprites_dir):
            mp = base / "poses.json"
            if not mp.exists():
                continue
            try:
                raw = json.loads(mp.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key, val in raw.items():
                        if isinstance(val, dict):
                            entry = dict(val)
                            entry["_base_dir"] = base
                            manifest[str(key).strip().lower()] = entry
            except Exception:
                continue
        return manifest

    def _resolve_sprite_path(self, filename: str, base_dir: Optional[Path] = None) -> Optional[Path]:
        if base_dir is not None:
            cand = base_dir / filename
            if cand.exists():
                return cand
        user_cand = self._user_sprites_dir / filename
        if user_cand.exists():
            return user_cand
        bundled_cand = SPRITES_DIR / filename
        if bundled_cand.exists():
            return bundled_cand
        return None

    def _slice_auto_sheet(
        self,
        path: Optional[Path],
        *,
        frames_hint: Optional[int] = None,
        cell_w_hint: Optional[int] = None,
    ) -> list[QPixmap]:
        """Auto-slice a drop-in horizontal sprite strip (supports manifest hints or zero-config PNGs)."""
        if path is None or not path.exists():
            return []
        src = QPixmap(str(path))
        if src.isNull() or src.width() <= 0 or src.height() <= 0:
            return []
        img_w, img_h = src.width(), src.height()
        if frames_hint and frames_hint > 0:
            count = int(frames_hint)
            cw = int(cell_w_hint) if (cell_w_hint and cell_w_hint > 0) else max(1, img_w // count)
        elif cell_w_hint and cell_w_hint > 0:
            cw = int(cell_w_hint)
            count = max(1, img_w // cw)
        elif img_w >= self.CELL_W and img_w % self.CELL_W == 0:
            cw = self.CELL_W
            count = img_w // cw
        elif img_w > img_h * 1.4:
            est_cw = max(1, int(round(img_h * (self.CELL_W / self.CELL_H))))
            count = max(1, int(round(img_w / est_cw)))
            cw = max(1, img_w // count)
        else:
            count = 1
            cw = img_w
        ch = img_h
        frames: list[QPixmap] = []
        for i in range(count):
            if i * cw >= img_w:
                break
            crop = src.copy(i * cw, 0, min(cw, img_w - i * cw), ch)
            scaled = crop.scaled(
                self.target_w,
                self.target_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            frames.append(scaled)
        return frames

    @staticmethod
    def _parse_positive_int(val) -> Optional[int]:
        if val is None or isinstance(val, bool):
            return None
        try:
            parsed = int(val)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _load_optional_slot(self, slot_name: str, default_file: str, manifest: dict[str, dict]) -> list[QPixmap]:
        entry = manifest.get(slot_name)
        fname = str(entry.get("file") or default_file) if isinstance(entry, dict) else default_file
        base_dir = entry.get("_base_dir") if isinstance(entry, dict) else None
        path = self._resolve_sprite_path(fname, base_dir=base_dir)
        if path is None:
            return []
        f_hint = self._parse_positive_int(entry.get("frames")) if isinstance(entry, dict) else None
        cw_hint = self._parse_positive_int(entry.get("cell_w")) if isinstance(entry, dict) else None
        if isinstance(entry, dict) and entry.get("fps") is not None and not isinstance(entry.get("fps"), bool):
            try:
                fps = max(1.0, min(60.0, float(entry["fps"])))
                self._slot_tick_divs[slot_name] = max(1, int(round(60.0 / fps)))
            except (TypeError, ValueError):
                pass
        return self._slice_auto_sheet(path, frames_hint=f_hint, cell_w_hint=cw_hint)

    def _load_all_sprites(self):
        self._slot_tick_divs.clear()
        self.idle_frames = self._slice_sheet(SPRITES_DIR / "idle.png", 16, cell_w=240)
        self.blink_frames = self._slice_sheet(SPRITES_DIR / "blink.png", 4, cell_w=240)
        self.blink_glitch_frames = self._slice_sheet(SPRITES_DIR / "blink_glitch.png", 4, cell_w=240)
        self.mouth_frames = self._slice_sheet(SPRITES_DIR / "mouth.png", 5, cell_w=240)
        self.glitch_frames = self._slice_sheet(SPRITES_DIR / "glitch.png", 10, cell_w=240)
        self.dance_frames = self._slice_sheet(SPRITES_DIR / "dance.png", 16, cell_w=240)
        self.sleep_frames = self._slice_sheet(SPRITES_DIR / "sleep.png", 16, cell_w=240)
        self.wake_frames = self._slice_sheet(SPRITES_DIR / "wake.png", 13, cell_w=240)
        # perch & perch_music sheets are 11760x384 = 60 frames @ 196px wide
        self.perch_frames = self._slice_sheet(SPRITES_DIR / "perch.png", 60, cell_w=196)
        self.perch_music_frames = self._slice_sheet(SPRITES_DIR / "perch_music.png", 60, cell_w=196)

        # Optional drop-in custom sheets (seamlessly fall back to bundled sprites when absent)
        manifest = self._load_pose_manifest()
        self.drag_frames = self._load_optional_slot("drag", "drag.png", manifest)
        self.fall_frames = self._load_optional_slot("fall", "fall.png", manifest)
        self.splat_frames = self._load_optional_slot("splat", "splat.png", manifest)
        self.lounge_frames = self._load_optional_slot("lounge", "lounge.png", manifest)
        self.walk_frames = self._load_optional_slot("walk", "walk.png", manifest)

    def reload_sprites(self, user_sprites_dir: Optional[Path] = None) -> dict[str, int]:
        if user_sprites_dir is not None:
            self._user_sprites_dir = Path(user_sprites_dir)
        self._load_all_sprites()
        self.update()
        return self.get_loaded_sprite_slots()

    def get_loaded_sprite_slots(self) -> dict[str, int]:
        return {
            "idle": len(self.idle_frames),
            "dance": len(self.dance_frames),
            "perch": len(self.perch_frames),
            "perch_music": len(self.perch_music_frames),
            "sleep": len(self.sleep_frames),
            "drag": len(self.drag_frames),
            "fall": len(self.fall_frames),
            "splat": len(self.splat_frames),
            "lounge": len(self.lounge_frames),
            "walk": len(self.walk_frames),
        }

    def _slice_sheet(self, path: Path, count: int, cell_w: Optional[int] = None) -> list[QPixmap]:
        if not path.exists():
            return []
        src = QPixmap(str(path))
        if src.isNull():
            return []
        cw = cell_w or self.CELL_W
        ch = min(self.CELL_H, src.height())
        frames = []
        for i in range(count):
            crop = src.copy(i * cw, 0, cw, ch)
            scaled = crop.scaled(
                self.target_w,
                self.target_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            frames.append(scaled)
        return frames

    def set_mood(self, mood: str):
        clean = (mood or "neutral").strip().lower()
        if clean in _MOOD_AURA_RGB:
            self.mood = clean
        else:
            self.mood = "neutral"

    def set_lip_sync_envelope(self, envelope: Optional[list[int]]):
        if not envelope:
            self.mouth_envelope = []
            self.mouth_env_pos = 0
            self.current_mouth_idx = 0
            return
        max_idx = max(0, len(self.mouth_frames) - 1) if self.mouth_frames else 4
        self.mouth_envelope = [max(0, min(max_idx, int(v))) for v in envelope]
        self.mouth_env_pos = 0
        self.current_mouth_idx = self.mouth_envelope[0]
        self.wake_up()

    def set_pose(self, pose: str):
        p = (pose or "normal").strip().lower()
        if p == "wake":
            self.wake_up()
            return
        if p in ("normal", "perch", "perch_music", "lounge", "walk", "sleep"):
            if self.pose == "sleep" and p != "sleep":
                self.wake_up()
            self.pose = p
            self.idle_ticks = 0
            self.update()

    def wake_up(self):
        """Smoothly transition out of sleeping pose using the 13-frame wake animation."""
        self.idle_ticks = 0
        if self.pose == "sleep":
            self.pose = "normal"
            if self.wake_frames:
                self.waking_active = True
                self.wake_frame = 0

    def set_reduced_motion(self, enabled: bool):
        self.reduced_motion = bool(enabled)
        if self.reduced_motion:
            self.physics_tilt_deg = 0.0
            self.physics_scale_x = 1.0
            self.physics_scale_y = 1.0
            self.physics_offset_y = 0.0
            self.physics_impact_ripple = 0.0
            self.physics_vx = 0.0
            self.physics_vy = 0.0
            self.update()

    def set_physics_visual(
        self,
        *,
        state: str,
        tilt_deg: float = 0.0,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        flail_phase: float = 0.0,
        facing: int = 1,
        canvas_offset_y: float = 0.0,
        impact_ripple: float = 0.0,
        vx: float = 0.0,
        vy: float = 0.0,
    ):
        self.physics_state = state or "idle"
        self.physics_facing = -1 if facing < 0 else 1
        if self.reduced_motion:
            self.physics_tilt_deg = 0.0
            self.physics_scale_x = 1.0
            self.physics_scale_y = 1.0
            self.physics_flail_phase = 0.0
            self.physics_offset_y = 0.0
            self.physics_impact_ripple = 0.0
            self.physics_vx = 0.0
            self.physics_vy = 0.0
        else:
            self.physics_tilt_deg = float(tilt_deg)
            self.physics_scale_x = float(scale_x)
            self.physics_scale_y = float(scale_y)
            self.physics_flail_phase = float(flail_phase)
            self.physics_offset_y = float(canvas_offset_y)
            self.physics_impact_ripple = max(0.0, min(1.0, float(impact_ripple)))
            self.physics_vx = float(vx)
            self.physics_vy = float(vy)

    def trigger_glitch(self):
        self.wake_up()
        self.glitch_active = True
        self.glitch_frame = 0

    def trigger_dance(self, active: bool | None = None):
        self.wake_up()
        if active is None:
            self.dance_active = not self.dance_active
        else:
            self.dance_active = active

    def advance_animation(self):
        self.tick_count += 1
        state = getattr(self.host, "state", "idle")

        # Hover speed slows gently when sleeping
        phase_step = 0.022 if self.pose == "sleep" else 0.045
        self.hover_phase += phase_step
        if self.hover_phase > math.tau:
            self.hover_phase -= math.tau

        # Wake immediately on any active state
        if state != "idle":
            self.idle_ticks = 0
            if self.pose == "sleep":
                self.wake_up()
        else:
            self.idle_ticks += 1
            if (
                self.pose == "normal"
                and not self.dance_active
                and self.sleep_frames
                and self.idle_ticks >= self.IDLE_SLEEP_TICKS
            ):
                self.pose = "sleep"

        # Wake-up transition animation
        if self.waking_active:
            if self.tick_count % 3 == 0:
                self.wake_frame += 1
                if self.wake_frame >= len(self.wake_frames):
                    self.waking_active = False
                    self.wake_frame = 0

        # 1. Логика моргания (обычное или киберпанк-глитч в зависимости от настроения)
        if not self.blink_active:
            self.blink_timer -= 1
            if self.blink_timer <= 0:
                self.blink_active = True
                self.blink_frame = 0
                glitch_prob = 0.55 if self.mood in ("sarcastic", "annoyed") else 0.18
                self.blink_is_glitch = bool(self.blink_glitch_frames) and (random.random() < glitch_prob)
        else:
            if self.tick_count % 3 == 0:
                self.blink_frame += 1
                frames_ref = self.blink_glitch_frames if self.blink_is_glitch else self.blink_frames
                if self.blink_frame >= len(frames_ref):
                    self.blink_active = False
                    self.blink_is_glitch = False
                    self.blink_timer = random.randint(180, 360)

        # 2. Логика глитча
        if self.glitch_active:
            if self.tick_count % 3 == 0:
                self.glitch_frame += 1
                if self.glitch_frame >= len(self.glitch_frames):
                    self.glitch_active = False

        # 3. Синхронизация липсинка (~15 FPS при 60 Гц таймере = шаг каждые 4 тика)
        if state == "speaking" and self.mouth_frames:
            if self.tick_count % 4 == 0:
                if self.mouth_envelope:
                    if self.mouth_env_pos < len(self.mouth_envelope):
                        self.current_mouth_idx = self.mouth_envelope[self.mouth_env_pos]
                        self.mouth_env_pos += 1
                    else:
                        # Subtle tail cadence if audio slightly outlasts estimated envelope
                        self.current_mouth_idx = 1 if (self.tick_count // 4) % 2 == 0 else 0
                else:
                    self.current_mouth_idx = (self.tick_count // 4) % len(self.mouth_frames)
        else:
            self.current_mouth_idx = 0

        self.update()

    def _active_aura_color(self, state: str) -> QColor:
        if state == "listening":
            return QColor(242, 119, 154, 92)  # living coral mic active
        if state == "thinking":
            return QColor(233, 183, 101, 82)  # amber neural compute
        if state == "speaking":
            return QColor(105, 213, 232, 98)  # bright signal cyan
        if self.pose == "sleep":
            return QColor(109, 123, 146, 48)  # calm muted slate
        r, g, b, a = _MOOD_AURA_RGB.get(self.mood, _MOOD_AURA_RGB["neutral"])
        return QColor(r, g, b, a)

    def paintEvent(self, event):
        painter = QPainter(self)
        self.render_to_painter(painter, self.width(), self.height())

    def render_to_painter(self, painter: QPainter, canvas_w: int, canvas_h: int):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        state = getattr(self.host, "state", "idle")
        char_x = (canvas_w - self.target_w) / 2.0
        phys_active = self.physics_state in ("grabbed", "falling", "wall_splat", "landing")
        if self.reduced_motion or phys_active:
            hover_y = 0.0
        else:
            amp = 2.0 if self.pose in ("sleep", "perch", "perch_music") else 5.0
            hover_y = math.sin(self.hover_phase) * amp
        base_char_y = max(72.0, float(canvas_h - self.target_h - 6))
        char_y = max(48.0, base_char_y + hover_y + (0.0 if self.reduced_motion else self.physics_offset_y))

        # 1. Голографическая аура с учётом состояния и настроения
        glow_col = self._active_aura_color(state)
        grad = QRadialGradient(canvas_w / 2.0, char_y + self.target_h - 15.0, 78.0)
        grad.setColorAt(0.0, glow_col)
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(QRectF(0, char_y + self.target_h - 45.0, canvas_w, 60), QBrush(grad))

        # 1b. Двойное голографическое кольцо приземления + микро-искры (60 Hz in-canvas, zero IPC)
        if not self.reduced_motion and self.physics_impact_ripple > 0.01:
            painter.save()
            progress = 1.0 - self.physics_impact_ripple
            cx = canvas_w / 2.0
            cy = base_char_y + self.target_h - 10.0
            # Inner crisp signal-cyan shockwave ring
            rx1 = 30.0 + progress * 66.0
            ry1 = 7.0 + progress * 13.0
            a1 = int(205 * self.physics_impact_ripple)
            painter.setPen(QPen(QColor(105, 213, 232, a1), 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(cx, cy), rx1, ry1)
            # Outer soft crimson-coral echo ring
            rx2 = 42.0 + progress * 82.0
            ry2 = 10.0 + progress * 17.0
            a2 = int(125 * self.physics_impact_ripple)
            painter.setPen(QPen(QColor(242, 119, 154, a2), 1.3))
            painter.drawEllipse(QPointF(cx, cy), rx2, ry2)
            # 6 subtle expanding holo-spark particles along the landing plane
            painter.setPen(Qt.PenStyle.NoPen)
            for s_idx in range(6):
                ang = (math.pi / 3.0) * s_idx + 0.35
                sx = cx + math.cos(ang) * (24.0 + progress * 72.0)
                sy = cy + math.sin(ang) * (5.0 + progress * 11.0) - progress * 8.0
                col = QColor(105, 213, 232, a1) if (s_idx % 2 == 0) else QColor(242, 119, 154, a1)
                painter.setBrush(QBrush(col))
                painter.drawEllipse(QPointF(sx, sy), 2.0, 2.0)
            painter.restore()

        def _select_primary_sprite() -> tuple[Optional[QPixmap], bool]:
            """Return `(pixmap, is_idle_composite)` respecting drop-in custom slots & clean fallbacks."""
            if self.physics_state == "wall_splat":
                if self.splat_frames:
                    div = self._slot_tick_divs.get("splat", 4)
                    return self.splat_frames[(self.tick_count // div) % len(self.splat_frames)], False
                if self.glitch_frames:
                    return self.glitch_frames[(self.tick_count // 2) % len(self.glitch_frames)], False
            if self.physics_state == "falling":
                if self.fall_frames:
                    div = self._slot_tick_divs.get("fall", 3)
                    return self.fall_frames[(self.tick_count // div) % len(self.fall_frames)], False
                if self.drag_frames:
                    div = self._slot_tick_divs.get("drag", 3)
                    return self.drag_frames[(self.tick_count // div) % len(self.drag_frames)], False
                if self.dance_frames:
                    return self.dance_frames[(self.tick_count // 3) % len(self.dance_frames)], False
            if self.physics_state == "grabbed":
                if self.drag_frames:
                    div = self._slot_tick_divs.get("drag", 3)
                    return self.drag_frames[(self.tick_count // div) % len(self.drag_frames)], False
                if self.dance_frames:
                    return self.dance_frames[(self.tick_count // 3) % len(self.dance_frames)], False
            if (self.glitch_active or state == "thinking") and self.glitch_frames:
                return self.glitch_frames[self.glitch_frame % len(self.glitch_frames)], False
            if self.waking_active and self.wake_frames:
                return self.wake_frames[min(self.wake_frame, len(self.wake_frames) - 1)], False
            if self.dance_active and self.dance_frames:
                return self.dance_frames[(self.tick_count // 3) % len(self.dance_frames)], False
            if self.pose == "sleep" and self.sleep_frames:
                return self.sleep_frames[(self.tick_count // 8) % len(self.sleep_frames)], False
            if self.pose == "lounge":
                if self.lounge_frames:
                    div = self._slot_tick_divs.get("lounge", 6)
                    return self.lounge_frames[(self.tick_count // div) % len(self.lounge_frames)], False
                if self.perch_frames:
                    return self.perch_frames[(self.tick_count // 6) % len(self.perch_frames)], False
            if self.pose == "walk" and self.walk_frames:
                div = self._slot_tick_divs.get("walk", 4)
                return self.walk_frames[(self.tick_count // div) % len(self.walk_frames)], False
            if self.pose == "perch_music" and self.perch_music_frames:
                return self.perch_music_frames[(self.tick_count // 4) % len(self.perch_music_frames)], False
            if self.pose == "perch" and self.perch_frames:
                return self.perch_frames[(self.tick_count // 5) % len(self.perch_frames)], False
            if self.idle_frames:
                return self.idle_frames[(self.tick_count // 7) % len(self.idle_frames)], True
            return None, False

        def _draw_centered(pm: QPixmap, dx: float = 0.0, dy: float = 0.0):
            draw_x = int((canvas_w - pm.width()) / 2.0 + dx)
            painter.drawPixmap(draw_x, int(char_y + dy), pm)

        primary_pm, is_idle_comp = _select_primary_sprite()

        # 2a. Голографический шлейф (motion-trail afterimages) при быстром броске/падении
        if (
            not self.reduced_motion
            and primary_pm is not None
            and self.physics_state == "falling"
        ):
            speed = math.hypot(self.physics_vx, self.physics_vy)
            if speed >= 480.0:
                nx = -self.physics_vx / speed
                ny = -self.physics_vy / speed
                for step_i, (dist_px, alpha_val) in enumerate(((22.0, 0.11), (11.0, 0.24))):
                    _ = step_i
                    painter.save()
                    painter.setOpacity(painter.opacity() * alpha_val)
                    ox = max(-36.0, min(36.0, nx * dist_px))
                    oy = max(-28.0, min(28.0, ny * dist_px))
                    _draw_centered(primary_pm, dx=ox, dy=oy)
                    painter.restore()

        # 2b. Отрисовка спрайта персонажа по приоритету анимации + плавная физика без дёргания
        painter.save()
        if not self.reduced_motion:
            pivot_x = canvas_w / 2.0
            if self.physics_state == "landing":
                pivot_y = char_y + self.target_h - 8.0
            else:
                pivot_y = char_y + self.target_h * 0.5

            total_tilt = 0.0 if self.physics_state == "grabbed" else self.physics_tilt_deg
            sx = self.physics_scale_x
            sy = self.physics_scale_y
            if abs(total_tilt) > 0.05 or abs(sx - 1.0) > 0.002 or abs(sy - 1.0) > 0.002:
                painter.translate(pivot_x, pivot_y)
                if abs(total_tilt) > 0.05:
                    painter.rotate(total_tilt)
                if abs(sx - 1.0) > 0.002 or abs(sy - 1.0) > 0.002:
                    painter.scale(sx, sy)
                painter.translate(-pivot_x, -pivot_y)

        if primary_pm is not None:
            _draw_centered(primary_pm)
            if is_idle_comp:
                if self.blink_active:
                    b_list = self.blink_glitch_frames if (self.blink_is_glitch and self.blink_glitch_frames) else self.blink_frames
                    if b_list:
                        b_idx = min(self.blink_frame, len(b_list) - 1)
                        _draw_centered(b_list[b_idx])

                if state == "speaking" and self.mouth_frames:
                    m_idx = max(0, min(self.current_mouth_idx, len(self.mouth_frames) - 1))
                    _draw_centered(self.mouth_frames[m_idx])

            # 2c. Тонкие хроматические RGB глитч-полосы при ударе о стену или активном глитче
            if not self.reduced_motion and (self.physics_state == "wall_splat" or self.glitch_active):
                painter.save()
                draw_x = int((canvas_w - primary_pm.width()) / 2.0)
                pm_w, pm_h = primary_pm.width(), primary_pm.height()
                phase = self.tick_count % 7
                bands = (
                    (0.24, 0.06, 6 if (phase % 2 == 0) else -6, QColor(242, 119, 154, 120)),
                    (0.54, 0.05, -5 if (phase % 2 == 0) else 5, QColor(105, 213, 232, 115)),
                    (0.76, 0.04, 4 if (phase % 3 == 0) else -4, QColor(242, 119, 154, 100)),
                )
                for y_frac, h_frac, shift_x, tint_col in bands:
                    sy = int(pm_h * y_frac)
                    sh = max(4, int(pm_h * h_frac))
                    painter.drawPixmap(
                        draw_x + shift_x,
                        int(char_y) + sy,
                        primary_pm,
                        0,
                        sy,
                        pm_w,
                        sh,
                    )
                    painter.fillRect(QRectF(draw_x + shift_x, char_y + sy, pm_w, 2.0), tint_col)
                painter.restore()
        painter.restore()

        # 3. Индикатор активного микрофона / Hands-Free Barge-In
        if state == "listening":
            painter.save()
            mic_x = char_x + self.target_w - 28.0
            mic_y = char_y + 35.0
            pulse = abs(math.sin(self.hover_phase * 2.0)) * 4.0
            painter.setBrush(QBrush(QColor(91, 205, 162, 230)))
            painter.setPen(QPen(QColor(241, 244, 248), 1.5))
            painter.drawEllipse(QRectF(mic_x - pulse / 2.0, mic_y - pulse / 2.0, 20 + pulse, 20 + pulse))
            painter.setPen(QPen(QColor(9, 12, 18), 2.0))
            painter.drawRoundedRect(QRectF(mic_x + 6, mic_y + 4, 8, 8), 3, 3)
            painter.drawLine(int(mic_x + 10), int(mic_y + 12), int(mic_x + 10), int(mic_y + 16))
            painter.restore()

        # 4. LIVING SIGNAL комикс-бабл реплики
        caption = getattr(self.host, "caption_text", "").strip()
        if caption:
            self._draw_speech_bubble(painter, caption, char_x + self.target_w / 2.0, char_y, canvas_w=canvas_w)

    def _compute_speech_bubble_rect(self, text: str, font: QFont, anchor_y: float, canvas_w: Optional[int] = None) -> QRectF:
        """Compute exact multi-line bubble geometry via QFontMetrics so 1-4 lines never clip."""
        w = canvas_w if canvas_w is not None else self.width()
        fm = QFontMetrics(font)
        max_bubble_w = max(140.0, float(w - 16))
        max_text_w = max(100, int(max_bubble_w - 28))
        bounds = fm.boundingRect(
            QRect(0, 0, max_text_w, 240),
            Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap,
            text,
        )
        bubble_w = min(max_bubble_w, max(144.0, float(bounds.width()) + 30.0))
        # Re-measure at final text width to ensure exact wrapped height
        final_text_w = max(80, int(bubble_w - 26))
        final_bounds = fm.boundingRect(
            QRect(0, 0, final_text_w, 240),
            Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap,
            text,
        )
        max_bubble_h = max(46.0, min(84.0, anchor_y + 10.0))
        bubble_h = max(34.0, min(max_bubble_h, float(final_bounds.height()) + 16.0))
        bubble_x = (w - bubble_w) / 2.0
        sky_bottom = max(bubble_h + 4.0, anchor_y + 6.0)
        bubble_y = max(4.0, min(14.0, (sky_bottom - bubble_h) * 0.45))
        return QRectF(bubble_x, bubble_y, bubble_w, bubble_h)

    def _draw_speech_bubble(
        self,
        painter: QPainter,
        text: str,
        anchor_x: float,
        anchor_y: float,
        canvas_w: Optional[int] = None,
    ):
        painter.save()
        font = QFont("Inter", 9, QFont.Weight.Medium)
        painter.setFont(font)

        rect = self._compute_speech_bubble_rect(text, font, anchor_y, canvas_w=canvas_w)
        bubble_x, bubble_y, bubble_w, bubble_h = rect.x(), rect.y(), rect.width(), rect.height()
        bg_col = QColor(13, 19, 30, 242)
        border_col = QColor(105, 213, 232, 210)

        painter.setBrush(QBrush(bg_col))
        painter.setPen(QPen(border_col, 1.2))
        painter.drawRoundedRect(rect, 9, 9)

        tip_y = min(bubble_y + bubble_h + 10.0, max(bubble_y + bubble_h + 5.0, anchor_y + 10.0))
        arrow = QPolygonF([
            QPointF(anchor_x - 6.0, bubble_y + bubble_h - 0.5),
            QPointF(anchor_x + 6.0, bubble_y + bubble_h - 0.5),
            QPointF(anchor_x, tip_y),
        ])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg_col))
        painter.drawPolygon(arrow)

        painter.setPen(QPen(QColor(242, 119, 154, 220), 2.5))
        painter.drawLine(int(bubble_x + 4), int(bubble_y + 7), int(bubble_x + 4), int(bubble_y + bubble_h - 7))

        painter.setPen(QColor(241, 244, 248))
        text_rect = QRectF(bubble_x + 12, bubble_y + 6, bubble_w - 20, bubble_h - 12)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)

        painter.restore()
