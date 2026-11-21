from __future__ import annotations

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

    def __init__(self, parent_widget, target_w: int = 210, target_h: int = 336):
        super().__init__(parent_widget)
        self.host = parent_widget
        self.target_w = target_w
        self.target_h = target_h

        # Спрайт-листы
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
        self.pose: str = "normal"  # "normal" | "perch" | "perch_music" | "sleep"
        self.waking_active: bool = False
        self.wake_frame: int = 0

        # Эмоция и синхронизированный липсинк (0..4 envelope @ 15 FPS)
        self.mood: str = "neutral"
        self.mouth_envelope: list[int] = []
        self.mouth_env_pos: int = 0
        self.current_mouth_idx: int = 0

    def _load_all_sprites(self):
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
        if p in ("normal", "perch", "perch_music", "sleep"):
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
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        state = getattr(self.host, "state", "idle")
        char_x = (self.width() - self.target_w) / 2.0
        amp = 2.0 if self.pose in ("sleep", "perch", "perch_music") else 5.0
        hover_y = math.sin(self.hover_phase) * amp
        char_y = max(72.0, float(self.height() - self.target_h - 6)) + hover_y

        # 1. Голографическая аура с учётом состояния и настроения
        glow_col = self._active_aura_color(state)
        grad = QRadialGradient(self.width() / 2.0, char_y + self.target_h - 15.0, 78.0)
        grad.setColorAt(0.0, glow_col)
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(QRectF(0, char_y + self.target_h - 45.0, self.width(), 60), QBrush(grad))

        def _draw_centered(pm: QPixmap):
            draw_x = int((self.width() - pm.width()) / 2.0)
            painter.drawPixmap(draw_x, int(char_y), pm)

        # 2. Отрисовка спрайта персонажа по приоритету анимации
        if (self.glitch_active or state == "thinking") and self.glitch_frames:
            g_idx = self.glitch_frame % len(self.glitch_frames)
            _draw_centered(self.glitch_frames[g_idx])
        elif self.waking_active and self.wake_frames:
            w_idx = min(self.wake_frame, len(self.wake_frames) - 1)
            _draw_centered(self.wake_frames[w_idx])
        elif self.dance_active and self.dance_frames:
            d_idx = (self.tick_count // 3) % len(self.dance_frames)
            _draw_centered(self.dance_frames[d_idx])
        elif self.pose == "sleep" and self.sleep_frames:
            s_idx = (self.tick_count // 8) % len(self.sleep_frames)
            _draw_centered(self.sleep_frames[s_idx])
        elif self.pose == "perch_music" and self.perch_music_frames:
            pm_idx = (self.tick_count // 4) % len(self.perch_music_frames)
            _draw_centered(self.perch_music_frames[pm_idx])
        elif self.pose == "perch" and self.perch_frames:
            p_idx = (self.tick_count // 5) % len(self.perch_frames)
            _draw_centered(self.perch_frames[p_idx])
        elif self.idle_frames:
            i_idx = (self.tick_count // 7) % len(self.idle_frames)
            _draw_centered(self.idle_frames[i_idx])

            if self.blink_active:
                b_list = self.blink_glitch_frames if (self.blink_is_glitch and self.blink_glitch_frames) else self.blink_frames
                if b_list:
                    b_idx = min(self.blink_frame, len(b_list) - 1)
                    _draw_centered(b_list[b_idx])

            if state == "speaking" and self.mouth_frames:
                m_idx = max(0, min(self.current_mouth_idx, len(self.mouth_frames) - 1))
                _draw_centered(self.mouth_frames[m_idx])

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
            self._draw_speech_bubble(painter, caption, char_x + self.target_w / 2.0, char_y)

    def _compute_speech_bubble_rect(self, text: str, font: QFont, anchor_y: float) -> QRectF:
        """Compute exact multi-line bubble geometry via QFontMetrics so 1-4 lines never clip."""
        fm = QFontMetrics(font)
        max_bubble_w = max(140.0, float(self.width() - 16))
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
        bubble_x = (self.width() - bubble_w) / 2.0
        sky_bottom = max(bubble_h + 4.0, anchor_y + 6.0)
        bubble_y = max(4.0, min(14.0, (sky_bottom - bubble_h) * 0.45))
        return QRectF(bubble_x, bubble_y, bubble_w, bubble_h)

    def _draw_speech_bubble(self, painter: QPainter, text: str, anchor_x: float, anchor_y: float):
        painter.save()
        font = QFont("Inter", 9, QFont.Weight.Medium)
        painter.setFont(font)

        rect = self._compute_speech_bubble_rect(text, font, anchor_y)
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
