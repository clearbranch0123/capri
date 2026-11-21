"""
CAPRI // LIVING SIGNAL Reusable UI Components.
Contains AudioWaveVisualizer, CapriSignalMark, CodeBlockWidget,
MarkdownMessageBrowser, ComposerTextEdit, WelcomeCanvas, and ClickableOverlay.
"""

from __future__ import annotations

import math
import random
import re
from typing import Optional

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPolygonF,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .icons import get_icon
from .theme import (
    COLORS,
    METRICS,
    TYPE,
    button_ghost_qss,
    markdown_document_css,
    scrollbar_qss,
)


_CODE_FENCE_RE = re.compile(r"```([A-Za-z0-9_+\-#.]*)\n(.*?)```", re.DOTALL)


def split_markdown_blocks(text: str) -> list[tuple[str, str, str]]:
    """
    Split markdown text into ordered segments:
    ('text', '', chunk) and ('code', lang, code_body).
    Returns a single ('text', '', text) when no fenced blocks exist.
    """
    if not text or "```" not in text:
        return [("text", "", text or "")]
    segments: list[tuple[str, str, str]] = []
    pos = 0
    for match in _CODE_FENCE_RE.finditer(text):
        start, end = match.span()
        before = text[pos:start].strip()
        if before:
            segments.append(("text", "", before))
        lang = (match.group(1) or "").strip().lower()
        code = match.group(2).rstrip("\n")
        segments.append(("code", lang, code))
        pos = end
    tail = text[pos:].strip()
    if tail:
        segments.append(("text", "", tail))
    return segments or [("text", "", text)]


class FlowLayout(QLayout):
    """
    Wrapping flow layout for compact pill/chip rows (tool calls, attachments).
    Never forces a parent container wider than a single pill — wraps gracefully onto
    multiple lines as windows shrink down to compact Wayland tile widths (480px).
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        margin: int = 0,
        h_spacing: int = 6,
        v_spacing: int = 6,
    ):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h_spacing = max(0, int(h_spacing))
        self._v_spacing = max(0, int(v_spacing))
        self.setContentsMargins(margin, margin, margin, margin)

    def __del__(self):
        while self._items:
            self.takeAt(0)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def horizontalSpacing(self) -> int:
        return self._h_spacing

    def verticalSpacing(self) -> int:
        return self._v_spacing

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, max(1, width), 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize(0, 0)
        for item in self._items:
            w_obj = item.widget()
            if w_obj is not None and w_obj.isHidden():
                continue
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_h = 0
        avail_w = max(40, effective.width()) if effective.width() > 0 else 0

        for item in self._items:
            w_obj = item.widget()
            if w_obj is not None and w_obj.isHidden():
                continue
            hint = item.sizeHint()
            item_w = min(hint.width(), avail_w) if avail_w > 0 else hint.width()
            item_h = hint.height()
            next_x = x + item_w + self._h_spacing
            if next_x - self._h_spacing > effective.right() + 1 and line_h > 0:
                x = effective.x()
                y = y + line_h + self._v_spacing
                next_x = x + item_w + self._h_spacing
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(item_w, item_h)))
            x = next_x
            line_h = max(line_h, item_h)

        return (y + line_h - rect.y()) + m.bottom()


class ElidingLabel(QLabel):
    """QLabel that smoothly elides overflowing text with '…' while keeping full `.text()` intact."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._full_text = text or ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        super().setText(self._full_text)

    def setText(self, text: str) -> None:
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._update_elided()

    def text(self) -> str:
        return self._full_text

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided()

    def _update_elided(self) -> None:
        avail = max(24, self.width() - 4)
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(self._full_text, Qt.TextElideMode.ElideRight, avail)
        super().setText(elided)


class CapriSignalMark(QWidget):
    """
    Signature geometric Capri mark: faceted Crimson Red obsidian diamond with inner living signal trace.
    Shared across Cyberdeck, WelcomeCanvas, ChatMessageCard, and Relic Dashboard.
    Respects `avatar_enabled` by displaying a restrained Cyan/Mint active presence dot.
    """

    def __init__(self, size: int = 36, avatar_enabled: bool = True, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._size = size
        self._avatar_enabled = bool(avatar_enabled)
        self.setFixedSize(size, size)

    def set_avatar_enabled(self, enabled: bool) -> None:
        self._avatar_enabled = bool(enabled)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        s = float(self._size)
        cx, cy = s / 2.0, s / 2.0
        r = s * 0.44

        diamond = QPolygonF([
            QPointF(cx, cy - r),
            QPointF(cx + r, cy),
            QPointF(cx, cy + r),
            QPointF(cx - r, cy),
        ])

        # Deep faceted Crimson prism fill
        grad = QLinearGradient(cx - r, cy - r, cx + r, cy + r)
        grad.setColorAt(0.0, QColor("#F23D63"))
        grad.setColorAt(0.55, QColor("#CF2046"))
        grad.setColorAt(1.0, QColor("#7D1029"))
        painter.setBrush(QBrush(grad))
        painter.setPen(QPen(QColor("#FF6B8A"), max(0.9, s * 0.032)))
        painter.drawPolygon(diamond)

        # Subtle upper-left glass facet for cyberware depth
        top_facet = QPolygonF([
            QPointF(cx, cy - r),
            QPointF(cx + r * 0.55, cy - r * 0.45),
            QPointF(cx, cy),
            QPointF(cx - r, cy),
        ])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 28))
        painter.drawPolygon(top_facet)

        # Crisp dark-ruby inner signal trace
        trace = QPainterPath()
        trace.moveTo(cx - r * 0.54, cy)
        trace.lineTo(cx - r * 0.22, cy)
        trace.lineTo(cx - r * 0.06, cy - r * 0.34)
        trace.lineTo(cx + r * 0.12, cy + r * 0.34)
        trace.lineTo(cx + r * 0.26, cy)
        trace.lineTo(cx + r * 0.54, cy)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(
            QPen(
                QColor("#FFE8EE"),
                max(1.25, s * 0.055),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.drawPath(trace)

        # Subtle presence indicator in bottom-right corner (only for larger marks >= 22px)
        if s >= 22:
            dot_r = max(2.2, s * 0.09)
            dot_center = QPointF(s - dot_r - 1.5, s - dot_r - 1.5)
            painter.setPen(QPen(QColor(COLORS.nav), 1.5))
            painter.setBrush(QColor(COLORS.signal_cyan if self._avatar_enabled else COLORS.text_muted))
            painter.drawEllipse(dot_center, dot_r, dot_r)


class SegmentedEngramBar(QWidget):
    """
    Reusable 5-segment Cyan engram synchronization meter shared by Cyberdeck & Relic Dashboard.
    Exposes QProgressBar-compatible `.setValue(int)` / `.value()` / `.setRange(min, max)`.
    """

    def __init__(self, segments: int = 5, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._segments = max(3, int(segments))
        self._min = 0
        self._max = 100
        self._value = 70
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def setRange(self, minimum: int, maximum: int) -> None:
        self._min = int(minimum)
        self._max = max(self._min + 1, int(maximum))
        self.update()

    def value(self) -> int:
        return self._value

    def setValue(self, val: int) -> None:
        clamped = max(self._min, min(self._max, int(val)))
        if clamped != self._value:
            self._value = clamped
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        gap = 4.0
        seg_w = max(4.0, (w - gap * (self._segments - 1)) / float(self._segments))
        radius = min(3.0, h / 2.0)

        ratio = (self._value - self._min) / float(self._max - self._min)
        filled_exact = ratio * self._segments

        track_col = QColor("#1D2230")
        cyan_start = QColor(COLORS.signal_cyan)
        cyan_end = QColor(COLORS.mint)

        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(self._segments):
            x = i * (seg_w + gap)
            seg_rect = QRectF(x, 0.0, seg_w, h)
            painter.setBrush(track_col)
            painter.drawRoundedRect(seg_rect, radius, radius)

            seg_fill = max(0.0, min(1.0, filled_exact - i))
            if seg_fill > 0.01:
                fill_w = max(h, seg_w * seg_fill)
                fill_rect = QRectF(x, 0.0, min(seg_w, fill_w), h)
                grad = QLinearGradient(x, 0.0, x + seg_w, 0.0)
                grad.setColorAt(0.0, cyan_start)
                grad.setColorAt(1.0, cyan_end)
                painter.setBrush(QBrush(grad))
                painter.drawRoundedRect(fill_rect, radius, radius)


class UserMonogramBadge(QWidget):
    """Compact circular Cyan-accented user monogram badge for right-aligned user messages."""

    def __init__(self, letter: str = "D", size: int = 20, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._letter = (letter or "D").strip()[:1].upper() or "D"
        self._size = size
        self.setFixedSize(size, size)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        s = float(self._size)
        rect = QRectF(0.75, 0.75, s - 1.5, s - 1.5)
        painter.setBrush(QColor(COLORS.signal_cyan_soft))
        painter.setPen(QPen(QColor(COLORS.signal_cyan_border), 1.0))
        painter.drawEllipse(rect)

        font = QFont("Inter", max(8, int(s * 0.46)))
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(QColor(COLORS.signal_cyan))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._letter)


class AudioWaveVisualizer(QWidget):
    """
    State-driven Living Signal waveform aligned to the composer column.
    - Completely hidden and timer-stopped in `idle` state (zero idle CPU or decorative noise).
    - Animates smoothly only in `listening`, `speaking`, and `thinking` states.
    - Respects `reduced_motion`: shows a calm static indicator without running a timer.
    """

    _STATE_LABELS = {
        "listening": "Слушаю",
        "speaking": "Говорю",
        "thinking": "Думаю",
        "idle": "",
    }

    def __init__(self, parent: Optional[QWidget] = None, reduced_motion: bool = False):
        super().__init__(parent)
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.state = "idle"
        self._reduced_motion = bool(reduced_motion)
        self.phase = 0.0
        self.bars = [0.22] * 18

        self.timer = QTimer(self)
        self.timer.setInterval(40)  # 25 FPS only when active
        self.timer.timeout.connect(self._update_wave)

        # Hidden by default while idle
        self.setVisible(False)

    def set_reduced_motion(self, enabled: bool) -> None:
        self._reduced_motion = bool(enabled)
        if self._reduced_motion:
            self.timer.stop()
            self._set_static_profile()
        elif self.state != "idle" and self.isVisible():
            self.timer.start()
        self.update()

    def set_state(self, state: str) -> None:
        self.state = state or "idle"
        if self.state == "idle":
            self.timer.stop()
            self.bars = [0.15] * len(self.bars)
            self.setVisible(False)
            self.update()
            return

        self.setVisible(True)
        self._set_static_profile()
        if not self._reduced_motion:
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def _set_static_profile(self) -> None:
        n = len(self.bars)
        if self.state == "listening":
            self.bars = [0.28 + 0.42 * math.sin((i / max(1, n - 1)) * math.pi) for i in range(n)]
        elif self.state == "speaking":
            self.bars = [0.32 + 0.50 * abs(math.sin(i * 0.55 + 0.4)) for i in range(n)]
        elif self.state == "thinking":
            self.bars = [0.22 + 0.25 * math.sin((i / max(1, n - 1)) * math.pi) for i in range(n)]
        else:
            self.bars = [0.15] * n

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)

    def _update_wave(self) -> None:
        if self.state == "idle" or self._reduced_motion or not self.isVisible():
            self.timer.stop()
            return
        self.phase = (self.phase + 0.14) % math.tau
        n = len(self.bars)
        if self.state == "speaking":
            for i in range(n):
                env = math.sin((i / max(1, n - 1)) * math.pi)
                h = (0.25 + 0.65 * abs(math.sin(self.phase * 2.6 + i * 0.45))) * (0.45 + 0.55 * env)
                self.bars[i] = min(1.0, h + random.uniform(0.0, 0.08))
        elif self.state == "listening":
            for i in range(n):
                env = math.sin((i / max(1, n - 1)) * math.pi)
                h = (0.20 + 0.55 * abs(math.sin(self.phase * 2.0 + i * 0.52))) * (0.5 + 0.5 * env)
                self.bars[i] = min(1.0, h)
        elif self.state == "thinking":
            for i in range(n):
                wave = 0.5 + 0.5 * math.sin(self.phase * 2.8 - i * 0.42)
                self.bars[i] = 0.18 + 0.38 * wave
        self.update()

    def paintEvent(self, event):
        if self.state == "idle":
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())

        if self.state == "speaking":
            c1, c2 = QColor(COLORS.signal_cyan), QColor(COLORS.crimson)
        elif self.state == "listening":
            c1, c2 = QColor(COLORS.signal_cyan), QColor(COLORS.mint)
        else:
            c1, c2 = QColor(COLORS.amber), QColor(COLORS.crimson)

        # Compact centered signal capsule
        label = self._STATE_LABELS.get(self.state, "")
        font = QFont("Inter", 9)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text_w = fm.horizontalAdvance(label) + 12 if label else 0

        bars_w = min(168.0, max(72.0, w - text_w - 44.0))
        pill_w = min(w - 4.0, bars_w + text_w + 26.0)
        pill_h = h - 2.0
        px = (w - pill_w) / 2.0
        py = 1.0

        pill_rect = QRectF(px, py, pill_w, pill_h)
        painter.setBrush(QColor(COLORS.surface))
        painter.setPen(QPen(QColor(COLORS.border_subtle), 1.0))
        painter.drawRoundedRect(pill_rect, pill_h / 2.0, pill_h / 2.0)

        # Status dot
        dot_x = px + 11.0
        dot_y = py + pill_h / 2.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(c1)
        painter.drawEllipse(QPointF(dot_x, dot_y), 3.0, 3.0)

        # State label
        cursor_x = dot_x + 8.0
        if label:
            painter.setPen(QColor(COLORS.text_secondary))
            painter.drawText(
                QRectF(cursor_x, py, text_w, pill_h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                label,
            )
            cursor_x += text_w

        # Bars
        bar_count = len(self.bars)
        avail = max(24.0, (px + pill_w - 10.0) - cursor_x)
        spacing = 3.0
        bar_w = max(2.0, min(3.5, (avail - spacing * (bar_count - 1)) / float(bar_count)))
        total_bars_w = bar_count * bar_w + (bar_count - 1) * spacing
        start_x = cursor_x + max(0.0, (avail - total_bars_w) / 2.0)
        max_bh = pill_h - 8.0

        for i, val in enumerate(self.bars):
            bx = start_x + i * (bar_w + spacing)
            bh = max(3.0, val * max_bh)
            by = py + (pill_h - bh) / 2.0
            grad = QLinearGradient(bx, by, bx, by + bh)
            grad.setColorAt(0.0, c1)
            grad.setColorAt(1.0, c2)
            painter.setBrush(QBrush(grad))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(bx, by, bar_w, bh), bar_w / 2.0, bar_w / 2.0)


class CodeBlockWidget(QFrame):
    """Dedicated fenced code block with language badge, one-click Copy, and local horizontal scroll."""

    def __init__(self, code: str, language: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._code = code
        self.setObjectName("codeBlock")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(f"""
            QFrame#codeBlock {{
                background: {COLORS.surface_code};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame(self)
        header.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface_alt};
                border: none;
                border-bottom: 1px solid {COLORS.border_hairline};
                border-top-left-radius: 9px;
                border-top-right-radius: 9px;
            }}
        """)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 5, 8, 5)
        hl.setSpacing(8)

        lbl_lang = QLabel(language or "code")
        lbl_lang.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; font-weight: 600;"
        )
        hl.addWidget(lbl_lang)
        hl.addStretch()

        self.btn_copy = QPushButton("Копировать")
        self.btn_copy.setIcon(get_icon("copy", COLORS.text_muted, 13))
        self.btn_copy.setIconSize(QSize(13, 13))
        self.btn_copy.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_copy.setStyleSheet(
            button_ghost_qss(
                text_color=COLORS.text_muted,
                hover_text=COLORS.text_primary,
                hover_bg=COLORS.surface_raised,
                padding="3px 8px",
                radius=6,
                font_size="11px",
            )
        )
        self.btn_copy.clicked.connect(self._copy_code)
        hl.addWidget(self.btn_copy)
        layout.addWidget(header)

        self.editor = QPlainTextEdit(self)
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setFrameShape(QFrame.Shape.NoFrame)
        self.editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.editor.setPlainText(code)
        self.editor.setStyleSheet(f"""
            QPlainTextEdit {{
                background: transparent;
                color: {COLORS.text_primary};
                border: none;
                padding: 10px 12px;
                font-family: {TYPE.mono};
                font-size: 12.5px;
                selection-background-color: {COLORS.surface_active};
            }}
            {scrollbar_qss(6)}
        """)

        lines = max(1, code.count("\n") + 1)
        fm = QFontMetrics(self.editor.font())
        line_h = max(19, fm.lineSpacing() + 2)
        content_h = min(360, lines * line_h + 44)
        self.editor.setFixedHeight(content_h)
        layout.addWidget(self.editor)

    def _copy_code(self) -> None:
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(self._code)
        self.btn_copy.setIcon(get_icon("check", COLORS.mint, 13))
        self.btn_copy.setText("Скопировано")
        QTimer.singleShot(1400, self._reset_copy_btn)

    def _reset_copy_btn(self) -> None:
        try:
            self.btn_copy.setIcon(get_icon("copy", COLORS.text_muted, 13))
            self.btn_copy.setText("Копировать")
        except RuntimeError:
            pass


class MarkdownMessageBrowser(QTextBrowser):
    """
    Clean Markdown prose renderer using Inter & JetBrains Mono with automatic height adjustment,
    zero internal scrollbars, and full `.text()` compatibility for tests.
    """

    def __init__(self, text: str = "", parent: Optional[QWidget] = None, raw_text_override: Optional[str] = None):
        super().__init__(parent)
        self.setOpenExternalLinks(True)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.document().setDocumentMargin(0)
        self.document().setDefaultStyleSheet(markdown_document_css())
        self._ideal_width = 120

        self.setStyleSheet(f"""
            QTextBrowser {{
                background: transparent;
                border: none;
                padding: 0px;
                margin: 0px;
                color: {COLORS.text_primary};
                selection-background-color: {COLORS.surface_active};
            }}
        """)

        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Text, QColor(COLORS.text_primary))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(COLORS.text_primary))
        pal.setColor(QPalette.ColorRole.Base, QColor("transparent"))
        self.setPalette(pal)

        self.document().documentLayout().documentSizeChanged.connect(self._adjust_height)
        self._raw_text = ""
        self._raw_override = raw_text_override
        self.setText(text)

    def setText(self, text: str, raw_override: Optional[str] = None):
        self._raw_text = text or ""
        if raw_override is not None:
            self._raw_override = raw_override
        self.setMarkdown(self._raw_text)
        raw_html = self.toHtml()
        # Enforce high-contrast text color against Linux Qt dark-theme palette overrides
        styled_html = (
            raw_html.replace('<body style="', f'<body style="color: {COLORS.text_primary}; ')
            .replace('<p style="', f'<p style="color: {COLORS.text_primary}; margin-top: 0px; margin-bottom: 6px; ')
            .replace('<li style="', f'<li style="color: {COLORS.text_primary}; ')
            .replace(
                '<pre style="',
                f'<pre style="color: {COLORS.text_primary}; background-color: {COLORS.surface_code}; '
                f'border: 1px solid {COLORS.border_hairline}; border-radius: 6px; padding: 8px 10px; '
                f'margin: 6px 0px; white-space: pre-wrap; word-break: break-word; ',
            )
        )
        self.setHtml(styled_html)
        self.document().setTextWidth(-1)
        ideal = int(math.ceil(self.document().idealWidth())) + 12
        self._ideal_width = max(64, min(860, ideal))
        if self.isVisible() and self.viewport().width() > 120:
            self.document().setTextWidth(float(self.viewport().width()))
        else:
            self.document().setTextWidth(float(self._ideal_width))
        self.updateGeometry()
        self._adjust_height()

    def minimumSizeHint(self) -> QSize:
        return QSize(0, max(22, self.minimumHeight()))

    def sizeHint(self) -> QSize:
        return QSize(self._ideal_width, max(22, self.minimumHeight()))

    def text(self) -> str:
        """Preserve full compatibility with card.lbl_text.text() callers and tests."""
        return self._raw_override if self._raw_override is not None else self._raw_text

    def sync_width(self) -> None:
        w = self.viewport().width() or self.width()
        if w > 40:
            self.document().setTextWidth(float(w))
            self._adjust_height()

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = max(40, self.viewport().width() or event.size().width())
        self.document().setTextWidth(float(w))
        self._adjust_height()

    def _adjust_height(self):
        doc = self.document()
        h = max(24, int(math.ceil(doc.size().height())) + 10)
        if self.minimumHeight() != h or self.maximumHeight() != h:
            self.setFixedHeight(h)
            self.updateGeometry()
            p = self.parentWidget()
            if p is not None:
                p.updateGeometry()


class ComposerTextEdit(QPlainTextEdit):
    """
    Auto-growing multiline composer editor with Enter-to-send and Shift+Enter newline.
    Exposes a full QLineEdit-compatible API (`text`, `setText`, `clear`, `selectAll`, `returnPressed`)
    so existing send/edit/Vision/voice callers and tests work seamlessly.
    """

    returnPressed = Signal()
    focusChanged = Signal(bool)
    voiceShortcutPressed = Signal()
    filesDropped = Signal(list)
    imagePasted = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None, min_height: int = 34, max_height: int = 136):
        super().__init__(parent)
        self._min_height = min_height
        self._max_height = max_height
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setTabChangesFocus(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(2)
        self.setStyleSheet(f"""
            QPlainTextEdit {{
                background: transparent;
                color: {COLORS.text_primary};
                border: none;
                padding: 4px 6px;
                font-family: {TYPE.sans};
                font-size: 14px;
                selection-background-color: {COLORS.surface_active};
            }}
            {scrollbar_qss(5)}
        """)
        self.textChanged.connect(self._update_height)
        self.setFixedHeight(self._min_height)

    def set_height_bounds(self, min_height: int, max_height: int) -> None:
        self._min_height = min_height
        self._max_height = max_height
        self._update_height()

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:
        self.setPlainText(text or "")
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.setTextCursor(cursor)
        self._update_height()

    def clear(self) -> None:
        super().clear()
        self._update_height()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_M and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            event.accept()
            self.voiceShortcutPressed.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            mods = event.modifiers()
            if mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
                super().keyPressEvent(event)
                self._update_height()
                return
            event.accept()
            self.returnPressed.emit()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.focusChanged.emit(True)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.focusChanged.emit(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_height()

    def canInsertFromMimeData(self, source) -> bool:
        if source is not None and (source.hasImage() or source.hasUrls()):
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        if source is not None:
            if source.hasImage():
                img = source.imageData()
                if img is not None:
                    self.imagePasted.emit(img)
                    return
            if source.hasUrls():
                local_paths = [u.toLocalFile() for u in source.urls() if u.isLocalFile() and u.toLocalFile()]
                if local_paths:
                    self.filesDropped.emit(local_paths)
                    return
        super().insertFromMimeData(source)

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md is not None and (md.hasUrls() or md.hasImage()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        md = event.mimeData()
        if md is not None and (md.hasUrls() or md.hasImage()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        md = event.mimeData()
        if md is not None:
            if md.hasUrls():
                local_paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile() and u.toLocalFile()]
                if local_paths:
                    self.filesDropped.emit(local_paths)
                    event.acceptProposedAction()
                    return
            if md.hasImage():
                img = md.imageData()
                if img is not None:
                    self.imagePasted.emit(img)
                    event.acceptProposedAction()
                    return
        super().dropEvent(event)

    def _update_height(self) -> None:
        doc = self.document()
        vw = max(120, self.viewport().width())
        doc.setTextWidth(float(vw))
        fm = QFontMetrics(self.font())
        line_h = max(20, fm.lineSpacing() + 2)
        blocks = max(1, int(doc.size().height()))
        # QPlainTextEdit documentLayout returns height in lines
        desired = blocks * line_h + 12
        clamped = max(self._min_height, min(self._max_height, desired))
        if self.height() != clamped:
            self.setFixedHeight(clamped)


class AttachmentStripWidget(QWidget):
    """Compact horizontal attachment chip strip inside the composer capsule (hidden when empty)."""

    changed = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._items: list = []
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._layout = FlowLayout(self, margin=2, h_spacing=6, v_spacing=6)
        self.setVisible(False)

    def attachments(self) -> list:
        return list(self._items)

    def add_attachment(self, item) -> bool:
        from ..core.attachments import MAX_ATTACHMENTS
        if len(self._items) >= MAX_ATTACHMENTS:
            return False
        if any(str(existing.path) == str(item.path) for existing in self._items):
            return False
        self._items.append(item)
        self._rebuild()
        return True

    def remove_attachment(self, path_str: str, cleanup_temp: bool = True) -> None:
        from ..core.attachments import cleanup_temp_attachment
        kept = []
        for it in self._items:
            if str(it.path) == path_str:
                if cleanup_temp:
                    cleanup_temp_attachment(it)
            else:
                kept.append(it)
        self._items = kept
        self._rebuild()

    def clear(self, cleanup_temp: bool = False) -> None:
        if cleanup_temp:
            from ..core.attachments import cleanup_temp_attachment
            for it in self._items:
                cleanup_temp_attachment(it)
        self._items.clear()
        self._rebuild()

    def _rebuild(self) -> None:
        while self._layout.count():
            child = self._layout.takeAt(0)
            w = child.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        for item in self._items:
            chip = QFrame(self)
            chip.setObjectName("attachmentChip")
            chip.setStyleSheet(f"""
                QFrame#attachmentChip {{
                    background: {COLORS.surface_raised};
                    border: 1px solid {COLORS.border_interactive};
                    border-radius: 8px;
                }}
            """)
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(6, 3, 5, 3)
            cl.setSpacing(6)

            lbl_icon = QLabel(chip)
            if item.kind == "image" and item.path.exists():
                from PySide6.QtGui import QPixmap
                pix = QPixmap(str(item.path))
                if not pix.isNull():
                    lbl_icon.setPixmap(
                        pix.scaled(
                            20,
                            20,
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
                else:
                    lbl_icon.setPixmap(get_icon("image", COLORS.signal_cyan, 14).pixmap(14, 14))
            else:
                lbl_icon.setPixmap(get_icon("file", COLORS.signal_cyan, 14).pixmap(14, 14))
            cl.addWidget(lbl_icon)

            name_short = item.display_name if len(item.display_name) <= 22 else item.display_name[:19] + "..."
            lbl_name = QLabel(name_short, chip)
            lbl_name.setToolTip(str(item.path))
            lbl_name.setStyleSheet(
                f"color: {COLORS.text_primary}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
            )
            cl.addWidget(lbl_name)

            btn_rm = QPushButton(chip)
            btn_rm.setIcon(get_icon("close", COLORS.text_muted, 11))
            btn_rm.setIconSize(QSize(11, 11))
            btn_rm.setFixedSize(18, 18)
            btn_rm.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_rm.setToolTip("Убрать вложение")
            btn_rm.setStyleSheet(
                button_ghost_qss(
                    text_color=COLORS.text_muted,
                    hover_text=COLORS.coral,
                    hover_bg=COLORS.coral_soft,
                    padding="0px",
                    radius=5,
                )
            )
            p_key = str(item.path)
            btn_rm.clicked.connect(lambda _checked=False, k=p_key: self.remove_attachment(k))
            cl.addWidget(btn_rm)

            self._layout.addWidget(chip)

        self.setVisible(bool(self._items))
        self.updateGeometry()
        self.changed.emit(len(self._items))


class WelcomeCanvas(QWidget):
    """
    Intentional new-chat / empty-session Living Signal welcome composition.
    Quietly frames Capri's greeting, geometric signal mark (respecting avatar_enabled),
    and 4 real actionable suggestions without competing with live conversations.
    """

    suggestion_triggered = Signal(str)

    def __init__(
        self,
        greeting: str = "",
        subtitle: str = "",
        avatar_enabled: bool = True,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._compact_height = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 48, 20, 28)
        outer.setSpacing(0)
        self._outer_layout = outer

        center = QWidget(self)
        center.setMaximumWidth(640)
        self._center_widget = center
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(14)
        cl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.signal_mark = CapriSignalMark(size=46, avatar_enabled=avatar_enabled, parent=center)
        cl.addWidget(self.signal_mark, alignment=Qt.AlignmentFlag.AlignCenter)

        self.lbl_greeting = QLabel(greeting or "На связи, D. С чего начнём?")
        self.lbl_greeting.setWordWrap(True)
        self.lbl_greeting.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_greeting.setStyleSheet(f"""
            color: {COLORS.text_primary};
            font-family: {TYPE.display};
            font-size: 20px;
            font-weight: 600;
            letter-spacing: -0.2px;
        """)
        cl.addWidget(self.lbl_greeting)

        self.lbl_subtitle = QLabel(subtitle)
        self.lbl_subtitle.setWordWrap(True)
        self.lbl_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_subtitle.setStyleSheet(f"""
            color: {COLORS.text_muted};
            font-family: {TYPE.mono};
            font-size: 11px;
        """)
        cl.addWidget(self.lbl_subtitle)

        # 4 real action suggestions
        self.suggestions_box = QWidget(center)
        self.suggestions_grid = QGridLayout(self.suggestions_box)
        self.suggestions_grid.setContentsMargins(0, 10, 0, 4)
        self.suggestions_grid.setSpacing(10)

        try:
            from ..core.compositor import detect_compositor
            comp_kind = detect_compositor()
            comp_short = {"niri": "Niri", "hyprland": "Hyprland"}.get(comp_kind, "Wayland")
        except Exception:
            comp_short = "Wayland"
        self._suggestions = [
            ("vision", "eye", "Посмотреть на экран", "Vision-анализ активного окна"),
            ("status", "activity", "Статус системы", f"CPU, GPU, память и {comp_short}"),
            ("memory", "memory", "Банк памяти", "Просмотр и правка фактов о D"),
            ("context", "layers", "Рабочий контекст", "Текущие задачи и решения"),
        ]
        self.suggestion_buttons: list[QPushButton] = []
        for idx, (action_id, icon_name, title, desc) in enumerate(self._suggestions):
            btn = QPushButton(f"{title}\n{desc}")
            btn.setObjectName(f"welcomeSuggestion_{action_id}")
            btn.setIcon(get_icon(icon_name, COLORS.signal_cyan, 16))
            btn.setIconSize(QSize(16, 16))
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.setMinimumHeight(58)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS.surface_alt};
                    color: {COLORS.text_primary};
                    border: 1px solid {COLORS.border_hairline};
                    border-radius: 10px;
                    padding: 10px 14px;
                    text-align: left;
                    font-family: {TYPE.sans};
                    font-size: 12px;
                    font-weight: 500;
                }}
                QPushButton:hover {{
                    background: {COLORS.surface_raised};
                    border-color: {COLORS.crimson_border};
                    color: {COLORS.text_primary};
                }}
                QPushButton:focus {{
                    border-color: {COLORS.focus_ring};
                }}
            """)
            btn.clicked.connect(lambda _checked=False, aid=action_id: self.suggestion_triggered.emit(aid))
            self.suggestions_grid.addWidget(btn, idx // 2, idx % 2)
            self.suggestion_buttons.append(btn)

        cl.addWidget(self.suggestions_box)
        outer.addWidget(center, alignment=Qt.AlignmentFlag.AlignCenter)

    def update_state(self, greeting: str, subtitle: str, avatar_enabled: bool, user_name: str = "D") -> None:
        self.lbl_greeting.setText(greeting)
        self.lbl_subtitle.setText(subtitle)
        self.signal_mark.set_avatar_enabled(avatar_enabled)
        u = (user_name or "D").strip() or "D"
        for btn in self.suggestion_buttons:
            if btn.objectName() == "welcomeSuggestion_memory":
                btn.setText(f"Банк памяти\nПросмотр и правка фактов о {u}")
                break

    def set_compact_mode(self, compact_height: bool, narrow_width: bool) -> None:
        self._compact_height = bool(compact_height)
        self.lbl_subtitle.setVisible(not compact_height)
        self.suggestions_box.setVisible(not compact_height)
        self.signal_mark.setVisible(not compact_height)
        for idx, btn in enumerate(self.suggestion_buttons):
            self.suggestions_grid.removeWidget(btn)
            if narrow_width:
                self.suggestions_grid.addWidget(btn, idx, 0)
            else:
                self.suggestions_grid.addWidget(btn, idx // 2, idx % 2)
        self.suggestions_box.setMinimumHeight(0 if compact_height else (270 if narrow_width else 142))
        self._center_widget.adjustSize()
        self.updateGeometry()

    def sync_viewport_height(self, viewport_h: int) -> None:
        """Vertically balance the welcome block within the available scroll viewport without clipping."""
        if self._compact_height or viewport_h <= 0:
            self._outer_layout.setContentsMargins(16, 18, 16, 14)
            self.setMinimumHeight(0)
            self.updateGeometry()
            return
        self._center_widget.adjustSize()
        content_h = max(310, self._center_widget.sizeHint().height())
        free_h = max(0, viewport_h - content_h - 32)
        top_pad = max(24, min(140, int(free_h * 0.40)))
        bot_pad = 24
        self._outer_layout.setContentsMargins(20, top_pad, 20, bot_pad)
        self.setMinimumHeight(content_h + top_pad + bot_pad)
        self.updateGeometry()


class ClickableOverlay(QFrame):
    """Semi-transparent drawer dismissal backdrop for compact windows."""

    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {COLORS.backdrop}; border: none;")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setVisible(False)

    def mousePressEvent(self, event):
        self.clicked.emit()
        if event:
            event.accept()



class CrimsonEdgeCard(QFrame):
    """
    Architectural Dark Graphite card with a smooth QPainter-rendered Crimson Red
    left-edge luminous highlight (eliminates CSS border-left corner hooks on rounded rects).
    Shared across Capri assistant messages and Relic Construct hero surfaces.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        radius: float = 14.0,
        bg_color: str = COLORS.surface_alt,
        accent_color: str = COLORS.crimson,
        top_accent: bool = False,
    ):
        super().__init__(parent)
        self._radius = float(radius)
        self._bg_color = bg_color
        self._accent_color = accent_color
        self._top_accent = bool(top_accent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent; border: none;")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        if w <= 2 or h <= 2:
            return
        r = min(self._radius, w / 2.0, h / 2.0)

        rect = QRectF(0.75, 0.75, w - 1.5, h - 1.5)
        path = QPainterPath()
        path.addRoundedRect(rect, r, r)

        painter.save()
        painter.setClipPath(path)

        # Deep Dark Graphite surface fill with subtle top-left depth
        bg_grad = QLinearGradient(0.0, 0.0, w, h)
        bg_grad.setColorAt(0.0, QColor("#151923"))
        bg_grad.setColorAt(1.0, QColor(self._bg_color))
        painter.fillPath(path, QBrush(bg_grad))

        # Restrained inner crimson sheen along the accented edge
        acc = QColor(self._accent_color)
        if self._top_accent:
            sheen = QLinearGradient(0.0, 0.0, 0.0, min(56.0, h * 0.45))
            sheen.setColorAt(0.0, QColor(acc.red(), acc.green(), acc.blue(), 26))
            sheen.setColorAt(1.0, QColor(acc.red(), acc.green(), acc.blue(), 0))
            painter.fillRect(QRectF(0.0, 0.0, w, min(56.0, h * 0.45)), QBrush(sheen))
        else:
            sheen = QLinearGradient(0.0, 0.0, min(42.0, w * 0.16), 0.0)
            sheen.setColorAt(0.0, QColor(acc.red(), acc.green(), acc.blue(), 28))
            sheen.setColorAt(1.0, QColor(acc.red(), acc.green(), acc.blue(), 0))
            painter.fillRect(QRectF(0.0, 0.0, min(42.0, w * 0.16), h), QBrush(sheen))
        painter.restore()

        # Subtle outer graphite hairline
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(COLORS.border_subtle), 1.0))
        painter.drawPath(path)

        # Smooth rounded luminous accent arc along left (or top) edge
        if self._top_accent:
            edge_grad = QLinearGradient(0.0, 0.0, w, 0.0)
            edge_grad.setColorAt(0.0, QColor(acc.red(), acc.green(), acc.blue(), 210))
            edge_grad.setColorAt(0.65, QColor(acc.red(), acc.green(), acc.blue(), 120))
            edge_grad.setColorAt(1.0, QColor(acc.red(), acc.green(), acc.blue(), 0))
            painter.setPen(QPen(QBrush(edge_grad), 1.6))
            painter.drawLine(QPointF(r * 0.8, 1.1), QPointF(w - r * 0.8, 1.1))
        else:
            edge_grad = QLinearGradient(0.0, 0.0, 0.0, h)
            edge_grad.setColorAt(0.0, QColor(acc.red(), acc.green(), acc.blue(), 220))
            edge_grad.setColorAt(0.55, QColor(acc.red(), acc.green(), acc.blue(), 195))
            edge_grad.setColorAt(1.0, QColor(acc.red(), acc.green(), acc.blue(), 150))
            painter.save()
            painter.setClipRect(QRectF(0.0, 0.0, r + 4.0, h))
            painter.setPen(QPen(QBrush(edge_grad), 2.0))
            painter.drawPath(path)
            painter.restore()


class ComposerCapsuleFrame(QFrame):
    """
    Signature Capri composer capsule with subtle Crimson Red corner-accent border glow
    (top-left and bottom-right) on deep Dark Graphite surface.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._focused = False
        self._recording = False
        self.setObjectName("composerCapsule")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("QFrame#composerCapsule { background: transparent; border: none; }")

    def set_capsule_state(self, focused: bool, recording: bool) -> None:
        if self._focused != bool(focused) or self._recording != bool(recording):
            self._focused = bool(focused)
            self._recording = bool(recording)
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        if w <= 2 or h <= 2:
            return
        r = 16.0
        rect = QRectF(0.75, 0.75, w - 1.5, h - 1.5)
        path = QPainterPath()
        path.addRoundedRect(rect, r, r)

        # Dark Graphite fill
        bg = "#151822" if (self._focused or self._recording) else COLORS.surface_alt
        painter.save()
        painter.setClipPath(path)
        painter.fillPath(path, QBrush(QColor(bg)))

        # Subtle crimson inner corner sheen
        alpha_sheen = 34 if self._recording else (22 if self._focused else 14)
        tl_glow = QLinearGradient(0.0, 0.0, 140.0, 60.0)
        tl_glow.setColorAt(0.0, QColor(225, 45, 83, alpha_sheen))
        tl_glow.setColorAt(1.0, QColor(225, 45, 83, 0))
        painter.fillRect(QRectF(0.0, 0.0, 160.0, h), QBrush(tl_glow))

        br_glow = QLinearGradient(w, h, w - 140.0, h - 60.0)
        br_glow.setColorAt(0.0, QColor(225, 45, 83, alpha_sheen))
        br_glow.setColorAt(1.0, QColor(225, 45, 83, 0))
        painter.fillRect(QRectF(max(0.0, w - 160.0), 0.0, 160.0, h), QBrush(br_glow))
        painter.restore()

        # Perimeter border with smooth Crimson diagonal corner highlight
        border_grad = QLinearGradient(0.0, 0.0, w, h)
        if self._recording:
            border_grad.setColorAt(0.0, QColor(240, 86, 120, 240))
            border_grad.setColorAt(0.5, QColor(225, 45, 83, 180))
            border_grad.setColorAt(1.0, QColor(240, 86, 120, 240))
            pen_w = 1.5
        elif self._focused:
            border_grad.setColorAt(0.0, QColor(225, 45, 83, 225))
            border_grad.setColorAt(0.28, QColor(52, 59, 78, 220))
            border_grad.setColorAt(0.72, QColor(52, 59, 78, 220))
            border_grad.setColorAt(1.0, QColor(225, 45, 83, 210))
            pen_w = 1.35
        else:
            border_grad.setColorAt(0.0, QColor(225, 45, 83, 165))
            border_grad.setColorAt(0.22, QColor(37, 42, 56, 230))
            border_grad.setColorAt(0.78, QColor(37, 42, 56, 230))
            border_grad.setColorAt(1.0, QColor(225, 45, 83, 155))
            pen_w = 1.15

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QBrush(border_grad), pen_w))
        painter.drawPath(path)


class RelicTelemetryTile(QFrame):
    """Compact Dark Graphite telemetry tile with icon, muted caption, and crisp monospace value."""

    def __init__(
        self,
        icon_name: str,
        title: str,
        value: str = "—",
        accent: str = COLORS.signal_cyan,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setObjectName("relicTile")
        self.setStyleSheet(f"""
            QFrame#relicTile {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(11, 8, 11, 9)
        lay.setSpacing(4)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self.lbl_icon = QLabel(self)
        self.lbl_icon.setPixmap(get_icon(icon_name, accent, 13).pixmap(13, 13))
        self.lbl_icon.setStyleSheet("border: none; background: transparent;")
        top.addWidget(self.lbl_icon)

        self.lbl_title = QLabel(title, self)
        self.lbl_title.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.sans}; font-size: 10.5px; font-weight: 600; border: none;"
        )
        top.addWidget(self.lbl_title)
        top.addStretch()
        lay.addLayout(top)

        self.lbl_value = QLabel(value, self)
        self.lbl_value.setStyleSheet(
            f"color: {COLORS.text_primary}; font-family: {TYPE.mono}; font-size: 12.5px; font-weight: 700; border: none;"
        )
        lay.addWidget(self.lbl_value)

    def set_value(self, value: str, color: Optional[str] = None) -> None:
        self.lbl_value.setText(value)
        if color:
            self.lbl_value.setStyleSheet(
                f"color: {color}; font-family: {TYPE.mono}; font-size: 12.5px; font-weight: 700; border: none;"
            )
