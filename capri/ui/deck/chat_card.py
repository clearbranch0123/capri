"""Chat message card widget and subtle UI feedback helpers for Cyberdeck."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from typing import Optional

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...core.resources import get_assets_dir
from ..i18n import format_rounds, tr_ui
from ..icons import get_icon
from ..motion import MotionController, MotionTokens
from ..theme import COLORS, TYPE, button_ghost_qss
from ..widgets import (
    CapriSignalMark,
    CodeBlockWidget,
    CrimsonEdgeCard,
    FlowLayout,
    MarkdownMessageBrowser,
    UserMonogramBadge,
    split_markdown_blocks,
)

logger = logging.getLogger(__name__)
ASSETS_DIR = get_assets_dir()


def play_ui_sound(sound_name: str):
    """Non-blocking UI sound playback (skipped in headless offscreen test runs)."""
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        sound_path = ASSETS_DIR / f"{sound_name}.wav"
        if not sound_path.exists():
            return
        proc = None
        if shutil.which("paplay"):
            proc = subprocess.Popen(["paplay", str(sound_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("mpv"):
            proc = subprocess.Popen(
                ["mpv", "--no-config", "--no-video", "--really-quiet", str(sound_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if proc is not None:
            threading.Thread(target=proc.wait, daemon=True).start()
    except Exception:
        pass


def fade_in_card(widget: QWidget, duration: int = MotionTokens.MESSAGE_MS, motion: Optional[MotionController] = None):
    """Restrained entrance transition for newly arriving live message cards."""
    if motion is not None:
        motion.fade_in(widget, duration=duration)
        return
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        return
    ctrl = MotionController(parent=widget)
    ctrl.fade_in(widget, duration=duration)


def _format_rounds_ru(count: int, lang: Optional[str] = None) -> str:
    return format_rounds(count, lang=lang)


class ChatMessageCard(QFrame):
    """
    LIVING SIGNAL conversation turn view:
    - D (user): right-aligned restrained surface pill with hover/focus actions.
    - Capri (assistant): clean editorial typography directly on canvas, grouped tool status row,
      collapsible thought disclosure, and locally scrollable fenced code blocks.
    - System: quiet centered event divider pill.
    Preserves all tested public attributes/signals: `lbl_text.text()`, `btn_edit`, `btn_delete`,
    `lbl_thought`, `thought_container`, `tools_info`, `edit_requested`, `delete_requested`.
    """

    edit_requested = Signal(str)    # emits turn_id
    delete_requested = Signal(str)  # emits turn_id

    def __init__(
        self,
        sender: str,
        text: str,
        thought: str = "",
        tools_info: Optional[list[str]] = None,
        turn_id: str = "",
        sequence: int = 0,
        parent: Optional[QWidget] = None,
        is_proactive: bool = False,
        motion: Optional[MotionController] = None,
        timestamp: str = "",
    ):
        super().__init__(parent)
        self.sender = sender
        sender_l = (sender or "").strip().lower()
        self.is_assistant = sender_l in ("капри", "capri", "assistant")
        self.is_system = sender_l in ("система", "system", "ошибка", "error")
        self.is_user = not self.is_assistant and not self.is_system
        self.is_proactive = bool(is_proactive)
        self.timestamp = timestamp or time.strftime("%H:%M")
        self.thought = thought
        self.tools_info: list[str] = []
        self._tool_detail_entries: list[tuple[str, str]] = []
        if tools_info:
            for item in tools_info:
                if self._is_tool_result_payload(item, "start"):
                    self._tool_detail_entries.append(("result", str(item)))
                else:
                    self.tools_info.append(str(item))
                    self._tool_detail_entries.append(("start", str(item)))
        self.turn_id = turn_id
        self.sequence = sequence
        self._motion = motion
        self._actions_revealed = False
        self._tools_expanded = False
        self._extra_body_widgets: list[QWidget] = []

        self._init_ui(text)

    def _init_ui(self, text: str):
        self.setObjectName("chatCard")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setStyleSheet("QFrame#chatCard { background: transparent; border: none; padding: 0px; margin: 0px; }")

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(4, 7, 4, 7)
        outer_layout.setSpacing(6)

        row_layout = QHBoxLayout()
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(0)
        self._row_layout = row_layout

        if self.is_assistant:
            self.bubble = CrimsonEdgeCard(parent=self, radius=14.0, bg_color=COLORS.surface_alt, accent_color=COLORS.crimson)
        else:
            self.bubble = QFrame(self)
        self.bubble.setObjectName("chatBubble")
        self.bubble.setMinimumWidth(0)
        b_layout = QVBoxLayout(self.bubble)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(7)

        if self.is_user:
            b_layout.setContentsMargins(14, 10, 14, 11)
            b_layout.setSpacing(6)

            sender_label = (self.sender or "User").strip() or "User"
            badge = UserMonogramBadge(letter=sender_label, size=20, parent=self.bubble)
            header_layout.addWidget(badge)

            # Avoid duplicating a single-character nickname (e.g. `(D) D`) next to its monogram badge
            if sender_label.casefold() != badge._letter.casefold():
                lbl_name = QLabel(sender_label)
                lbl_name.setStyleSheet(f"""
                    color: {COLORS.text_primary};
                    font-family: {TYPE.sans};
                    font-size: 11.5px;
                    font-weight: 600;
                """)
                header_layout.addWidget(lbl_name)

            lbl_ts = QLabel(self.timestamp)
            lbl_ts.setStyleSheet(f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10px;")
            header_layout.addWidget(lbl_ts)
            header_layout.addStretch()

            self.btn_edit = QPushButton()
            self.btn_edit.setToolTip("Редактировать запрос (откатить диалог к этой точке)")
            self.btn_edit.setFixedSize(24, 24)
            self.btn_edit.setIconSize(QSize(13, 13))
            self.btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_edit.clicked.connect(self._on_edit_click)
            self.btn_edit.setVisible(bool(self.turn_id))
            self.btn_edit.installEventFilter(self)
            header_layout.addWidget(self.btn_edit)

            self.btn_delete = QPushButton()
            self.btn_delete.setToolTip("Удалить раунд из истории")
            self.btn_delete.setFixedSize(24, 24)
            self.btn_delete.setIconSize(QSize(13, 13))
            self.btn_delete.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_delete.clicked.connect(self._on_delete_click)
            self.btn_delete.setVisible(bool(self.turn_id))
            self.btn_delete.installEventFilter(self)
            header_layout.addWidget(self.btn_delete)

            b_layout.addLayout(header_layout)

            self.bubble.setMaximumWidth(620)
            self.bubble.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
            self.bubble.setStyleSheet(f"""
                QFrame#chatBubble {{
                    background: #161A24;
                    border: 1px solid {COLORS.border_subtle};
                    border-radius: 14px;
                    border-bottom-right-radius: 5px;
                }}
            """)
            row_layout.addStretch(1)
            row_layout.addWidget(self.bubble, 0)

        elif self.is_assistant:
            # 3-part hierarchy inside a left-aligned fluid container (never floating in a narrow center void):
            # (1) Header above card (Crimson Prism + Капри + timestamp)
            # (2) Smooth QPainter CrimsonEdgeCard bubble
            # (3) Floating action pills below card
            b_layout.setContentsMargins(18, 14, 18, 14)
            b_layout.setSpacing(9)

            self._asst_wrap = QWidget(self)
            self._asst_wrap.setMinimumWidth(0)
            self._asst_wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            self._asst_wrap.setStyleSheet("background: transparent; border: none;")

            asst_stack = QVBoxLayout(self._asst_wrap)
            asst_stack.setContentsMargins(0, 0, 0, 0)
            asst_stack.setSpacing(6)

            header_layout.setContentsMargins(2, 0, 2, 0)
            self.avatar_mark = CapriSignalMark(size=20, avatar_enabled=True, parent=self._asst_wrap)
            header_layout.addWidget(self.avatar_mark)

            lbl_name = QLabel(tr_ui("Капри", "Capri"))
            lbl_name.setStyleSheet(f"""
                color: {COLORS.text_primary};
                font-family: {TYPE.sans};
                font-size: 13px;
                font-weight: 700;
            """)
            header_layout.addWidget(lbl_name)

            lbl_ts = QLabel(self.timestamp)
            lbl_ts.setStyleSheet(f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10.5px;")
            header_layout.addWidget(lbl_ts)

            if self.is_proactive:
                lbl_pro = QLabel(tr_ui("инициатива", "initiative"))
                lbl_pro.setStyleSheet(f"""
                    color: {COLORS.signal_cyan};
                    background: {COLORS.signal_cyan_soft};
                    border: 1px solid {COLORS.signal_cyan_border};
                    border-radius: 8px;
                    padding: 1px 8px;
                    font-family: {TYPE.mono};
                    font-size: 10px;
                    font-weight: 600;
                """)
                header_layout.addWidget(lbl_pro)

            header_layout.addStretch()
            asst_stack.addLayout(header_layout)

            self.bubble.setMaximumWidth(16777215)
            self.bubble.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            asst_stack.addWidget(self.bubble)

            # Floating action pill strip neatly under the assistant card
            self.btn_edit = QPushButton()
            self.btn_edit.setVisible(False)

            footer_actions = QHBoxLayout()
            footer_actions.setContentsMargins(2, 0, 2, 0)
            footer_actions.setSpacing(6)

            self.btn_copy_msg = QPushButton()
            self.btn_copy_msg.setToolTip("Копировать ответ")
            self.btn_copy_msg.setFixedSize(28, 26)
            self.btn_copy_msg.setIconSize(QSize(13, 13))
            self.btn_copy_msg.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_copy_msg.clicked.connect(self._on_copy_click)
            self.btn_copy_msg.installEventFilter(self)
            footer_actions.addWidget(self.btn_copy_msg)

            self.btn_delete = QPushButton()
            self.btn_delete.setToolTip("Удалить раунд из истории")
            self.btn_delete.setFixedSize(28, 26)
            self.btn_delete.setIconSize(QSize(13, 13))
            self.btn_delete.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_delete.clicked.connect(self._on_delete_click)
            self.btn_delete.setVisible(bool(self.turn_id))
            self.btn_delete.installEventFilter(self)
            footer_actions.addWidget(self.btn_delete)
            footer_actions.addStretch()

            asst_stack.addLayout(footer_actions)
            self._asst_wrap.setMaximumWidth(16777215)
            row_layout.addWidget(self._asst_wrap, 1)

        else:
            # Quiet system event divider pill
            b_layout.setContentsMargins(14, 7, 14, 7)
            b_layout.setSpacing(4)

            dot = QLabel()
            dot.setFixedSize(6, 6)
            dot.setStyleSheet(f"background: {COLORS.crimson}; border-radius: 3px; border: none;")
            header_layout.addWidget(dot)

            lbl_name = QLabel(tr_ui("Система", "System"))
            lbl_name.setStyleSheet(f"""
                color: {COLORS.text_muted};
                font-family: {TYPE.mono};
                font-size: 10.5px;
                font-weight: 600;
            """)
            header_layout.addWidget(lbl_name)
            header_layout.addStretch()

            self.btn_edit = QPushButton()
            self.btn_edit.setVisible(False)
            self.btn_delete = QPushButton()
            self.btn_delete.setVisible(False)

            b_layout.addLayout(header_layout)

            self.bubble.setMaximumWidth(640)
            self.bubble.setStyleSheet(f"""
                QFrame#chatBubble {{
                    background: {COLORS.surface_alt};
                    border: 1px solid {COLORS.border_hairline};
                    border-radius: 10px;
                }}
            """)
            row_layout.addStretch(1)
            row_layout.addWidget(self.bubble, 0)
            row_layout.addStretch(1)

        # 1. Thought Disclosure (Cyan system pill + animated reveal)
        self.thought_container = QWidget(self.bubble)
        tc_layout = QVBoxLayout(self.thought_container)
        tc_layout.setContentsMargins(0, 0, 0, 0)
        tc_layout.setSpacing(6)

        self.btn_thought_toggle = QPushButton(tr_ui("Рассуждение", "Reasoning"))
        self.btn_thought_toggle.setIcon(get_icon("chevron-right", COLORS.signal_cyan, 13))
        self.btn_thought_toggle.setIconSize(QSize(13, 13))
        self.btn_thought_toggle.setCheckable(True)
        self.btn_thought_toggle.setChecked(False)
        self.btn_thought_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_thought_toggle.setStyleSheet(f"""
            QPushButton {{
                color: {COLORS.signal_cyan};
                font-family: {TYPE.sans};
                font-size: 11.5px;
                font-weight: 600;
                background: {COLORS.signal_cyan_soft};
                border: 1px solid {COLORS.signal_cyan_border};
                border-radius: 8px;
                padding: 4px 10px;
                text-align: left;
            }}
            QPushButton:hover {{
                color: {COLORS.signal_cyan_hover};
                border-color: {COLORS.signal_cyan};
                background: rgba(78, 216, 232, 0.18);
            }}
            QPushButton:checked {{
                color: {COLORS.signal_cyan_hover};
                border-color: {COLORS.signal_cyan};
                background: rgba(78, 216, 232, 0.18);
            }}
            QPushButton:focus {{
                border-color: {COLORS.focus_ring};
            }}
        """)
        self.btn_thought_toggle.toggled.connect(self._toggle_thought)
        tc_layout.addWidget(self.btn_thought_toggle, alignment=Qt.AlignmentFlag.AlignLeft)

        self.lbl_thought = QLabel(self._soft_break_long_tokens(self.thought or ""))
        self.lbl_thought.setWordWrap(True)
        self.lbl_thought.setMinimumWidth(0)
        self.lbl_thought.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_thought.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_thought.setStyleSheet(f"""
            color: {COLORS.text_secondary};
            background: {COLORS.surface_code};
            border: 1px solid {COLORS.border_hairline};
            border-left: 2px solid {COLORS.signal_cyan};
            border-radius: 6px;
            padding: 8px 12px;
            font-family: {TYPE.mono};
            font-size: 11.5px;
            line-height: 1.45;
        """)
        self.lbl_thought.setVisible(False)
        tc_layout.addWidget(self.lbl_thought)

        self.thought_container.setVisible(bool(self.thought))
        b_layout.addWidget(self.thought_container)

        # 2. Grouped Tool Activity Row (wrapping FlowLayout pills + expandable call/output log)
        self.tools_box = QFrame(self.bubble)
        self.tools_box.setMinimumWidth(0)
        self.tools_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.tools_box.setStyleSheet("background: transparent; border: none;")
        self.tb_outer = QVBoxLayout(self.tools_box)
        self.tb_outer.setContentsMargins(0, 0, 0, 0)
        self.tb_outer.setSpacing(6)

        self.tb_layout = FlowLayout(margin=0, h_spacing=6, v_spacing=6)
        self.tb_outer.addLayout(self.tb_layout)

        self.tools_detail_box = QLabel(self.tools_box)
        self.tools_detail_box.setWordWrap(True)
        self.tools_detail_box.setMinimumWidth(0)
        self.tools_detail_box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.tools_detail_box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tools_detail_box.setStyleSheet(f"""
            color: {COLORS.text_secondary};
            background: {COLORS.surface_alt};
            border: 1px solid {COLORS.border_hairline};
            border-left: 2px solid {COLORS.amber};
            border-radius: 6px;
            padding: 8px 11px;
            font-family: {TYPE.mono};
            font-size: 11px;
        """)
        self.tools_detail_box.setVisible(False)
        self.tb_outer.addWidget(self.tools_detail_box)

        self._rebuild_tools_ui()
        b_layout.addWidget(self.tools_box)

        # 3. Body content (Markdown prose + dedicated locally scrollable CodeBlockWidgets)
        self.body_container = QWidget(self.bubble)
        self.body_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.body_layout = QVBoxLayout(self.body_container)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(8)

        self.lbl_text = MarkdownMessageBrowser("", self.body_container)
        self.body_layout.addWidget(self.lbl_text)
        b_layout.addWidget(self.body_container)

        self._render_body(text)

        outer_layout.addLayout(row_layout)
        self._apply_action_reveal(False)

    def _apply_action_reveal(self, revealed: bool):
        self._actions_revealed = revealed
        icon_col = COLORS.text_primary if revealed else COLORS.text_muted
        del_col = COLORS.text_secondary if revealed else COLORS.text_muted
        pill_bg = COLORS.surface if self.is_assistant else "transparent"
        pill_border = COLORS.border_subtle if self.is_assistant else "transparent"
        asst_btn_qss = f"""
            QPushButton {{
                background: {pill_bg};
                color: {icon_col};
                border: 1px solid {pill_border};
                border-radius: 7px;
                padding: 0px;
            }}
            QPushButton:hover {{
                background: {COLORS.surface_raised};
                border-color: {COLORS.border_interactive};
                color: {COLORS.signal_cyan};
            }}
        """
        asst_del_qss = f"""
            QPushButton {{
                background: {pill_bg};
                color: {del_col};
                border: 1px solid {pill_border};
                border-radius: 7px;
                padding: 0px;
            }}
            QPushButton:hover {{
                background: {COLORS.coral_soft};
                border-color: {COLORS.coral_border};
                color: {COLORS.coral};
            }}
        """
        if hasattr(self, "btn_edit") and self.btn_edit:
            self.btn_edit.setIcon(get_icon("edit", icon_col, 13))
            self.btn_edit.setStyleSheet(
                button_ghost_qss(
                    text_color=icon_col,
                    hover_text=COLORS.signal_cyan,
                    hover_bg=COLORS.surface_raised,
                    padding="0px",
                    radius=6,
                )
            )
        if hasattr(self, "btn_delete") and self.btn_delete:
            self.btn_delete.setIcon(get_icon("trash", del_col, 13))
            self.btn_delete.setStyleSheet(
                asst_del_qss
                if self.is_assistant
                else button_ghost_qss(
                    text_color=del_col,
                    hover_text=COLORS.coral,
                    hover_bg=COLORS.coral_soft,
                    padding="0px",
                    radius=6,
                )
            )
        if hasattr(self, "btn_copy_msg") and self.btn_copy_msg:
            self.btn_copy_msg.setIcon(get_icon("copy", icon_col, 13))
            self.btn_copy_msg.setStyleSheet(asst_btn_qss)

    def enterEvent(self, event):
        self._apply_action_reveal(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        focused = any(
            getattr(self, name, None) is not None and getattr(self, name).hasFocus()
            for name in ("btn_edit", "btn_delete", "btn_copy_msg")
        )
        if not focused:
            self._apply_action_reveal(False)
        super().leaveEvent(event)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.FocusIn:
            self._apply_action_reveal(True)
        elif event.type() == QEvent.Type.FocusOut:
            if not self.underMouse():
                self._apply_action_reveal(False)
        return super().eventFilter(obj, event)

    def _clear_extra_body_widgets(self):
        for w in self._extra_body_widgets:
            self.body_layout.removeWidget(w)
            w.hide()
            w.setParent(None)
            w.deleteLater()
        self._extra_body_widgets.clear()

    def _render_body(self, text: str):
        raw = text or ""
        self._clear_extra_body_widgets()

        segments = split_markdown_blocks(raw)
        has_code_blocks = any(kind == "code" for kind, _, _ in segments)

        if not has_code_blocks:
            self.lbl_text.setVisible(True)
            self.lbl_text.setText(raw, raw_override=raw)
            return

        # Keep lbl_text holding the full raw text for `.text()` compatibility,
        # while rendering fenced code blocks in dedicated CodeBlockWidgets with local scroll.
        first_prose_used = False
        for kind, lang, content in segments:
            if kind == "text":
                if not first_prose_used:
                    self.lbl_text.setVisible(True)
                    self.lbl_text.setText(content, raw_override=raw)
                    first_prose_used = True
                else:
                    prose = MarkdownMessageBrowser(content, self.body_container)
                    self.body_layout.addWidget(prose)
                    self._extra_body_widgets.append(prose)
            else:
                cb = CodeBlockWidget(content, language=lang, parent=self.body_container)
                self.body_layout.addWidget(cb)
                self._extra_body_widgets.append(cb)

        if not first_prose_used:
            self.lbl_text.setText("", raw_override=raw)
            self.lbl_text.setVisible(False)

    @staticmethod
    def _is_tool_result_payload(raw: str, kind: str = "start") -> bool:
        if (kind or "start").strip().lower() == "result":
            return True
        text = (raw or "").strip()
        if not text:
            return True
        if "\n" in text or "\r" in text:
            return True
        has_call_prefix = text.startswith(("Запуск:", "Вызов:", "Tool:", "tool:"))
        if not has_call_prefix and len(text) > 72:
            return True
        return False

    @staticmethod
    def _soft_break_long_tokens(text: str, chunk: int = 44) -> str:
        """Insert zero-width break opportunities into very long unbroken paths/URLs."""
        if not text:
            return ""
        out_lines: list[str] = []
        for line in text.splitlines():
            parts = line.split(" ")
            fixed_parts: list[str] = []
            for p in parts:
                if len(p) > chunk:
                    p = "\u200b".join(p[i : i + chunk] for i in range(0, len(p), chunk))
                fixed_parts.append(p)
            out_lines.append(" ".join(fixed_parts))
        return "\n".join(out_lines)

    @staticmethod
    def _format_tool_result_preview(raw: str, max_lines: int = 6, max_chars: int = 460) -> str:
        clean = (raw or "").strip()
        if not clean:
            return ""
        lines = [ln.rstrip() for ln in clean.splitlines() if ln.strip()]
        clipped_lines = False
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            clipped_lines = True
        formatted: list[str] = []
        total = 0
        for ln in lines:
            if len(ln) > 130:
                ln = ln[:127].rstrip() + "…"
            if total + len(ln) > max_chars:
                rem = max(0, max_chars - total)
                if rem > 12:
                    formatted.append("  │ " + ln[: rem - 1].rstrip() + "…")
                clipped_lines = True
                break
            formatted.append("  │ " + ln)
            total += len(ln)
        if clipped_lines:
            formatted.append("  │ …")
        return "\n".join(formatted)

    def _normalize_tool_label(self, raw: str) -> str:
        first_line = ""
        for ln in (raw or "").splitlines():
            s = ln.strip()
            if s:
                first_line = s
                break
        clean = first_line
        for prefix in ("Запуск:", "Вызов:", "Tool:", "tool:"):
            if clean.startswith(prefix):
                clean = clean[len(prefix):].strip()
        clean = " ".join(clean.split())
        if len(clean) > 26:
            clean = clean[:25].rstrip() + "…"
        return clean or "tool"

    def _rebuild_tools_ui(self):
        while self.tb_layout.count():
            item = self.tb_layout.takeAt(0)
            if item and item.widget():
                w = item.widget()
                w.hide()
                w.setParent(None)
                w.deleteLater()

        if not self.tools_info and not self._tool_detail_entries:
            self.tools_box.setVisible(False)
            self.tools_detail_box.setVisible(False)
            return

        grouped: OrderedDict[str, int] = OrderedDict()
        for line in self.tools_info:
            name = self._normalize_tool_label(line)
            grouped[name] = grouped.get(name, 0) + 1

        if not grouped and self._tool_detail_entries:
            grouped["tool"] = len(self._tool_detail_entries)

        for tool_name, cnt in grouped.items():
            label = f"{tool_name} ×{cnt}" if cnt > 1 else tool_name
            pill = QPushButton(label)
            pill.setIcon(get_icon("terminal", COLORS.amber, 12))
            pill.setIconSize(QSize(12, 12))
            pill.setCursor(Qt.CursorShape.PointingHandCursor)
            pill.setMaximumWidth(210)
            pill.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            pill.setToolTip("Показать или скрыть детали вызовов инструментов")
            pill.setStyleSheet(f"""
                QPushButton {{
                    color: {COLORS.amber};
                    background: {COLORS.amber_soft};
                    border: 1px solid {COLORS.amber_border};
                    border-radius: 7px;
                    padding: 3px 9px;
                    font-family: {TYPE.mono};
                    font-size: 10.5px;
                    font-weight: 600;
                }}
                QPushButton:hover {{
                    background: rgba(233, 183, 101, 0.20);
                }}
            """)
            pill.clicked.connect(self._toggle_tools_detail)
            self.tb_layout.addWidget(pill)

        detail_blocks: list[str] = []
        entries = self._tool_detail_entries or [("start", ln) for ln in self.tools_info]
        for entry_kind, entry_text in entries:
            if entry_kind == "result":
                preview = self._format_tool_result_preview(entry_text)
                if preview:
                    detail_blocks.append(preview)
            else:
                detail_blocks.append(f"▸ {entry_text.strip()}")

        detail_text = self._soft_break_long_tokens("\n".join(detail_blocks))
        self.tools_detail_box.setText(detail_text)
        self.tools_detail_box.setVisible(self._tools_expanded)
        self.tools_box.setVisible(True)
        self.tools_box.updateGeometry()

    def _toggle_tools_detail(self):
        self._tools_expanded = not self._tools_expanded
        if self._motion is not None:
            self._motion.animate_disclosure(self.tools_detail_box, self._tools_expanded)
        else:
            self.tools_detail_box.setVisible(self._tools_expanded)

    def _toggle_thought(self, checked: bool):
        if hasattr(self, "lbl_thought"):
            if self._motion is not None:
                self._motion.animate_disclosure(self.lbl_thought, checked)
            else:
                self.lbl_thought.setVisible(checked)
        if hasattr(self, "btn_thought_toggle"):
            icon_name = "chevron-down" if checked else "chevron-right"
            icon_color = COLORS.signal_cyan_hover if checked else COLORS.signal_cyan
            self.btn_thought_toggle.setIcon(get_icon(icon_name, icon_color, 13))

    def set_thought(self, thought: str):
        self.thought = thought
        if hasattr(self, "lbl_thought") and self.lbl_thought:
            self.lbl_thought.setText(self._soft_break_long_tokens(thought or ""))
            if self.btn_thought_toggle.isChecked():
                self.lbl_thought.setVisible(bool(thought))
        if hasattr(self, "thought_container") and self.thought_container:
            self.thought_container.setVisible(bool(thought))

    def add_tool_info(self, t_line: str, kind: str = "start"):
        text = (t_line or "").strip()
        if not text:
            return
        if self._is_tool_result_payload(text, kind):
            self._tool_detail_entries.append(("result", text))
        else:
            self.tools_info.append(text)
            self._tool_detail_entries.append(("start", text))
        self._rebuild_tools_ui()

    def set_text(self, text: str):
        self._render_body(text)

    def _on_copy_click(self):
        cb = QApplication.clipboard()
        if cb is not None and hasattr(self, "lbl_text"):
            cb.setText(self.lbl_text.text())
        if hasattr(self, "btn_copy_msg"):
            self.btn_copy_msg.setIcon(get_icon("check", COLORS.mint, 13))
            QTimer.singleShot(1200, lambda: self._apply_action_reveal(self._actions_revealed))

    def _on_edit_click(self):
        logger.info(f"ChatMessageCard: edit clicked for turn_id={self.turn_id}")
        if self.turn_id:
            self.edit_requested.emit(self.turn_id)

    def _on_delete_click(self):
        logger.info(f"ChatMessageCard: delete clicked for turn_id={self.turn_id}")
        if self.turn_id:
            self.delete_requested.emit(self.turn_id)

    def sync_column_width(self, col_width: int) -> None:
        """Adapt card width & right breathing gutter cleanly across fullscreen, windowed, and narrow tiles."""
        cw = max(280, int(col_width))
        if self.is_assistant:
            if cw >= 1120:
                right_gutter = int(cw * 0.16)
            elif cw >= 760:
                right_gutter = int(cw * 0.10)
            else:
                right_gutter = 0
            if hasattr(self, "_row_layout") and self._row_layout is not None:
                self._row_layout.setContentsMargins(0, 0, right_gutter, 0)
            if hasattr(self, "lbl_text") and hasattr(self.lbl_text, "sync_width"):
                self.lbl_text.sync_width()
        elif self.is_user:
            target = min(740, int(cw * 0.64)) if cw >= 720 else int(cw * 0.88)
            self.bubble.setMaximumWidth(max(220, target))
            if hasattr(self, "lbl_text") and hasattr(self.lbl_text, "sync_width"):
                self.lbl_text.sync_width()
        else:
            self.bubble.setMaximumWidth(min(780, cw))

    def set_turn_metadata(self, turn_id: str, sequence: int):
        self.turn_id = turn_id
        self.sequence = sequence
        if hasattr(self, "btn_edit"):
            self.btn_edit.setVisible(bool(turn_id and self.is_user))
        if hasattr(self, "btn_delete"):
            self.btn_delete.setVisible(bool(turn_id))
