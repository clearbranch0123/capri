from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QShortcut,
)
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSystemTrayIcon,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import __codename__, __version__
from ..core.agent import AgentEvent, CapriAgent
from ..core.config import (
    PROVIDER_DEFAULTS,
    USER_CONFIG_DIR,
    AppConfig,
    delete_env_value,
    get_user_config_dir,
    load_config,
    save_config,
    update_env_value,
)
from ..core.emotions import EmotionEngine
from ..core.memory import (
    CATEGORIES,
    CATEGORY_LABELS,
    RETENTION_MODES,
    MemoryCandidate,
    MemoryEntry,
    MemoryStore,
    memory_payload_contains_secret,
)
from ..core.models import fetch_available_models, format_models_list, resolve_model_name
from ..core.prompts import get_system_prompt
from ..core.sessions import SessionRecord, TurnRecord
from ..tools.desktop import capri_self_control, focus_window, get_system_status, list_windows
from ..tools.memory import add_memory, clear_all_memories, delete_memory, load_memories
from ..tools.registry import registry
from .client import notify_avatar, notify_avatar_locomotion, send_avatar_control
from .icons import get_icon, render_svg_pixmap
from .motion import MotionController, MotionTokens
from .theme import (
    COLORS,
    METRICS,
    TYPE,
    apply_dark_palette,
    button_danger_qss,
    button_ghost_qss,
    button_primary_qss,
    button_secondary_qss,
    crimson_cta_qss,
    dialog_base_qss,
    input_qss,
    markdown_document_css,
    menu_qss,
    message_box_qss,
    scrollbar_qss,
    table_qss,
)
from .widgets import (
    AttachmentStripWidget,
    AudioWaveVisualizer,
    CapriSignalMark,
    ClickableOverlay,
    CodeBlockWidget,
    ComposerCapsuleFrame,
    ComposerTextEdit,
    CrimsonEdgeCard,
    ElidingLabel,
    FlowLayout,
    MarkdownMessageBrowser,
    SegmentedEngramBar,
    UserMonogramBadge,
    WelcomeCanvas,
    split_markdown_blocks,
)

logger = logging.getLogger(__name__)
from ..core.resources import get_assets_dir

ASSETS_DIR = get_assets_dir()


def get_cyberdeck_ipc_socket_path() -> str:
    """Return per-user Unix socket path for single-instance & Wayland `--toggle` IPC."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir and os.path.isdir(runtime_dir):
        return str(Path(runtime_dir) / "capri-cyberdeck.sock")
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return f"/tmp/capri-cyberdeck-{uid}.sock"


def send_cyberdeck_ipc(cmd: str, socket_path: Optional[str] = None, timeout: float = 0.4) -> bool:
    """Send a lightweight JSON command ('toggle'|'show'|'hide'|'quit') to a running Cyberdeck."""
    target = socket_path or get_cyberdeck_ipc_socket_path()
    if not os.path.exists(target):
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(target)
        payload = json.dumps({"cmd": cmd}).encode("utf-8") + b"\n"
        sock.sendall(payload)
        try:
            ack = sock.recv(64)
            return bool(ack)
        except Exception:
            return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def play_ui_sound(sound_name: str):
    """Non-blocking UI sound playback."""
    try:
        sound_path = ASSETS_DIR / f"{sound_name}.wav"
        if not sound_path.exists():
            return
        if shutil.which("paplay"):
            subprocess.Popen(["paplay", str(sound_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("mpv"):
            subprocess.Popen(
                ["mpv", "--no-config", "--no-video", "--really-quiet", str(sound_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
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


def _format_rounds_ru(count: int) -> str:
    n = abs(int(count))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} раунд"
    if 2 <= (n % 10) <= 4 and not (12 <= (n % 100) <= 14):
        return f"{n} раунда"
    return f"{n} раундов"


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

            lbl_name = QLabel("Капри")
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
                lbl_pro = QLabel("инициатива")
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

            lbl_name = QLabel("Система")
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

        self.btn_thought_toggle = QPushButton("Рассуждение")
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


_ACTIVE_QTHREADS: set[QThread] = set()


def _track_qthread(thread: Optional[QThread]) -> None:
    """Keep a strong reference to a running QThread until its `finished` signal fires."""
    if thread is None:
        return
    if thread.isFinished():
        _ACTIVE_QTHREADS.discard(thread)
        return
    if thread not in _ACTIVE_QTHREADS:
        _ACTIVE_QTHREADS.add(thread)
        thread.finished.connect(lambda t=thread: _ACTIVE_QTHREADS.discard(t))
    if thread.isFinished():
        _ACTIVE_QTHREADS.discard(thread)


def check_api_key_sync(
    service: str,
    api_key: str,
    base_url: str = "",
    timeout: float = 6.0,
) -> tuple[bool, str]:
    """
    Lightweight, secret-safe live API key verification for Settings dialog.
    Supports `llm` (OpenAI-compatible `/models`), `fish` (Fish Audio), `elevenlabs`, and `gemini` (Vision/TTS).
    Never returns raw API keys in status messages.
    """
    import httpx
    from ..core.doctor import redact_sensitive_text

    key = (api_key or "").strip()
    svc = (service or "").strip().lower()
    if not key:
        return False, "Ключ не введён"

    headers: dict[str, str] = {"User-Agent": f"Capri/{__version__}"}
    if svc == "llm":
        root = (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
        url = f"{root}/models"
        headers["Authorization"] = f"Bearer {key}"
    elif svc == "fish":
        url = "https://api.fish.audio/model?page_size=1"
        headers["Authorization"] = f"Bearer {key}"
    elif svc == "elevenlabs":
        url = "https://api.elevenlabs.io/v1/user"
        headers["xi-api-key"] = key
    elif svc == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=1&key={key}"
    else:
        return False, f"Неизвестный сервис: {service}"

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code == 200:
            return True, "Ключ валиден (HTTP 200)"
        if resp.status_code in (401, 403):
            return False, f"Отклонён сервисом (HTTP {resp.status_code} — проверь ключ)"
        if resp.status_code == 400 and svc == "gemini":
            return False, "Неверный Gemini API-ключ (HTTP 400)"
        return False, f"Ответ API: HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "Таймаут подключения (проверь сеть/VPN)"
    except Exception as e:
        safe_msg = redact_sensitive_text(str(e), extra_secrets=(key,))
        return False, f"Ошибка сети: {safe_msg[:90]}"


class APIKeyCheckWorker(QThread):
    """Non-blocking background worker for checking an API key from Settings."""

    result_ready = Signal(str, bool, str)  # (service_id, ok, status_message)

    def __init__(self, service: str, api_key: str, base_url: str = "", parent=None):
        super().__init__(parent)
        self.service = service
        self.api_key = api_key
        self.base_url = base_url

    def run(self):
        ok, msg = check_api_key_sync(self.service, self.api_key, base_url=self.base_url)
        self.result_ready.emit(self.service, ok, msg)


class MCPConnectWorker(QThread):
    """Background worker for non-blocking, cancellable `/mcp connect` handshakes."""

    completed = Signal(object, bool)  # (summary_dict, was_cancelled)
    failed = Signal(str)

    def __init__(self, mcp_manager, mcp_config, parent=None):
        super().__init__(parent)
        self.mcp_manager = mcp_manager
        self.mcp_config = mcp_config
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        try:
            self.mcp_manager.cancel_inflight_connect()
        except Exception:
            pass

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run(self):
        try:
            res_map = self.mcp_manager.connect_from_config_sync(
                self.mcp_config,
                cancel_event=self._cancel_event,
            )
            self.completed.emit(res_map, self._cancel_event.is_set())
        except Exception as e:
            if self._cancel_event.is_set():
                self.completed.emit({}, True)
            else:
                from ..core.doctor import redact_sensitive_text
                self.failed.emit(redact_sensitive_text(str(e)))


class AgentWorker(QThread):
    """Background worker thread for CapriAgent async turns."""

    thought_signal = Signal(str, dict)
    tool_signal = Signal(str, str)
    chunk_signal = Signal(str)
    speaking_signal = Signal()
    text_ready_signal = Signal(str)
    memory_candidates_signal = Signal(list)
    finished_signal = Signal()
    error_signal = Signal(str)
    cancelled_signal = Signal()
    approval_requested = Signal(object)

    def __init__(
        self,
        agent: CapriAgent,
        message: str,
        parent=None,
        attachments: Optional[list] = None,
        display_text: Optional[str] = None,
    ):
        super().__init__(parent)
        self.agent = agent
        self.message = message
        self.attachments = list(attachments) if attachments else []
        self.display_text = display_text
        self.is_cancelled = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._pending_approval_event: Optional[threading.Event] = None

    def cancel(self):
        self.is_cancelled = True
        if self._pending_approval_event is not None:
            self._pending_approval_event.set()
        self.agent.abort()
        if hasattr(self.agent, "tts") and self.agent.tts:
            self.agent.tts.stop_sync()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._cancel_task)

    def _cancel_task(self):
        if self._task and not self._task.done():
            self._task.cancel()

    async def _request_approval(self, req) -> bool:
        if self.is_cancelled:
            return False
        ev = threading.Event()
        box = {"approved": False}
        self._pending_approval_event = ev
        try:
            self.approval_requested.emit((req, ev, box))
            while not ev.is_set() and not self.is_cancelled:
                await asyncio.sleep(0.05)
            return bool(box["approved"]) and not self.is_cancelled
        finally:
            self._pending_approval_event = None

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self.agent.set_tool_approval_handler(self._request_approval)
        try:
            self._task = self._loop.create_task(self._chat_coro())
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            self.cancelled_signal.emit()
        except Exception as e:
            if not self.is_cancelled:
                self.error_signal.emit(str(e))
        finally:
            self.agent.set_tool_approval_handler(None)
            try:
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._loop.close()
            except Exception:
                pass

    async def _chat_coro(self):
        try:
            full_text = ""
            prompt_msg = self.message
            if self.attachments:
                from ..core.attachments import build_prompt_with_attachments
                self.thought_signal.emit("Анализирую прикреплённые материалы...", {})
                try:
                    prompt_msg = await build_prompt_with_attachments(
                        self.message,
                        self.attachments,
                        config=self.agent.config,
                    )
                finally:
                    from ..core.attachments import cleanup_temp_attachment
                    for att in self.attachments:
                        cleanup_temp_attachment(att)
                if self.is_cancelled:
                    self.cancelled_signal.emit()
                    return

            async for event in self.agent.chat(
                prompt_msg,
                speak=True,
                stream=True,
                display_user_text=self.display_text,
            ):
                if self.is_cancelled:
                    self.cancelled_signal.emit()
                    return
                if event.type == "thought":
                    self.thought_signal.emit(event.data, event.metadata)
                elif event.type == "tool_start":
                    self.tool_signal.emit(event.data, "start")
                elif event.type == "tool_result":
                    self.tool_signal.emit(event.data, "result")
                elif event.type == "delta":
                    full_text = event.data
                    self.chunk_signal.emit(event.data)
                elif event.type == "speaking":
                    self.speaking_signal.emit()
                elif event.type == "memory_candidates":
                    cands = event.metadata.get("candidates") or []
                    if cands:
                        self.memory_candidates_signal.emit(list(cands))
                elif event.type == "done":
                    full_text = event.data
                    if full_text and full_text.strip():
                        self.text_ready_signal.emit(full_text)
                elif event.type == "error":
                    self.error_signal.emit(event.data)
                    return

            if self.is_cancelled:
                self.cancelled_signal.emit()
            else:
                self.finished_signal.emit()
        except asyncio.CancelledError:
            self.cancelled_signal.emit()
        except Exception as e:
            if not self.is_cancelled:
                self.error_signal.emit(str(e))


class VoiceRecordWorker(QThread):
    """Background voice capture & STT transcription worker with cancellable async STT task."""

    text_ready = Signal(str)
    failed = Signal(str)

    def __init__(self, voice_config):
        super().__init__()
        self.config = voice_config
        self.stop_event = threading.Event()
        self.is_discarded = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None

    def stop_recording(self):
        self.stop_event.set()

    def _cancel_task(self):
        if self._task and not self._task.done():
            self._task.cancel()

    def cancel_recording(self):
        self.is_discarded = True
        self.stop_event.set()
        loop = self._loop
        if loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._cancel_task)
            except RuntimeError:
                pass

    def run(self):
        try:
            from ..voice.stt import SpeechRecognizer
            stt = SpeechRecognizer(self.config)
            audio = stt.record_audio(max_duration=6.0, silence_timeout=1.3, stop_event=self.stop_event)
            if self.is_discarded:
                self.failed.emit("Запись отменена.")
                return

            if len(audio) == 0:
                self.failed.emit("Аудиозапись пуста.")
                return

            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                self._task = loop.create_task(stt.transcribe_audio_array(audio))
                if self.is_discarded:
                    self._task.cancel()
                text = loop.run_until_complete(self._task)
            except asyncio.CancelledError:
                self.failed.emit("Запись отменена.")
                return
            finally:
                try:
                    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                self._loop = None
                self._task = None
                loop.close()

            if self.is_discarded:
                self.failed.emit("Запись отменена.")
                return

            if text and text.strip():
                self.text_ready.emit(text.strip())
            else:
                self.failed.emit("Речь не распознана.")
        except Exception as e:
            if not self.is_discarded:
                self.failed.emit(str(e))


def _make_dialog_header(title: str, subtitle: str, on_close) -> QHBoxLayout:
    """Shared calm dialog header layout across all secondary dialogs."""
    header = QHBoxLayout()
    header.setSpacing(12)

    title_box = QVBoxLayout()
    title_box.setSpacing(2)

    lbl_sub = QLabel(subtitle)
    lbl_sub.setStyleSheet(
        f"color: {COLORS.signal_cyan}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 600; letter-spacing: 0.8px;"
    )
    title_box.addWidget(lbl_sub)

    lbl_title = QLabel(title)
    lbl_title.setStyleSheet(
        f"color: {COLORS.text_primary}; font-family: {TYPE.display}; font-size: 16px; font-weight: 600;"
    )
    title_box.addWidget(lbl_title)

    header.addLayout(title_box)
    header.addStretch()

    btn_close = QPushButton()
    btn_close.setIcon(get_icon("close", COLORS.text_muted, 15))
    btn_close.setIconSize(QSize(15, 15))
    btn_close.setFixedSize(30, 30)
    btn_close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    btn_close.setToolTip("Закрыть")
    btn_close.setStyleSheet(
        button_ghost_qss(
            text_color=COLORS.text_muted,
            hover_text=COLORS.coral,
            hover_bg=COLORS.coral_soft,
            padding="0px",
            radius=8,
        )
    )
    btn_close.clicked.connect(on_close)
    header.addWidget(btn_close)
    return header


class RegionCaptureWorker(QThread):
    """Non-blocking, cancellable Wayland region screenshot worker (`slurp` + `grim`)."""

    captured = Signal(str, str)  # (file_path_or_empty, status)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cancel_event = threading.Event()
        self._path_lock = threading.Lock()
        self._pending_path: Optional[str] = None

    def cancel(self) -> None:
        self.cancel_event.set()

    def claim_pending_path(self) -> Optional[str]:
        """Atomically transfer ownership of the captured temp file to the UI handler."""
        with self._path_lock:
            p = self._pending_path
            self._pending_path = None
            return p

    def cleanup_unclaimed_path(self) -> None:
        """Delete any captured temp screenshot that was never claimed by the UI handler."""
        p = self.claim_pending_path()
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    def run(self):
        path = ""
        try:
            from ..tools.screen import capture_screen_region_sync
            cap_path, status = capture_screen_region_sync(cancel_event=self.cancel_event)
            path = cap_path or ""
            if path:
                with self._path_lock:
                    self._pending_path = path
            if self.cancel_event.is_set():
                self.cleanup_unclaimed_path()
                return
            self.captured.emit(path, status)
        except Exception:
            self.cleanup_unclaimed_path()
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            if not self.cancel_event.is_set():
                self.captured.emit("", "error")


class MicCalibrationWorker(QThread):
    """Guided 3-second microphone noise-floor + speech RMS calibration worker for Barge-In."""

    level_changed = Signal(int)      # 0..100 VU meter percent
    phase_changed = Signal(str)      # Russian phase hint
    calibration_ready = Signal(object)  # CalibrationResult
    failed = Signal(str)

    def __init__(self, input_device: str = "", sample_rate: int = 16000, parent=None):
        super().__init__(parent)
        self.input_device = (input_device or "").strip()
        self.sample_rate = sample_rate
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        from ..voice.pipewire import (
            compute_chunk_rms,
            recommend_barge_in_threshold,
            resolve_sounddevice_input,
            rms_to_meter_percent,
        )
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as e:
            self.failed.emit(f"Модуль захвата звука недоступен: {e}")
            return

        block_size = int(self.sample_rate * 0.08)  # 80ms frames
        dev_idx = resolve_sounddevice_input(self.input_device)
        stream_kwargs = {
            "samplerate": self.sample_rate,
            "blocksize": block_size,
            "channels": 1,
            "dtype": "float32",
        }
        if dev_idx is not None:
            stream_kwargs["device"] = dev_idx

        noise_samples: list[float] = []
        speech_samples: list[float] = []

        try:
            with sd.InputStream(**stream_kwargs) as stream:
                # Phase 1: 0.9s ambient room / speaker noise floor
                self.phase_changed.emit("Шаг 1/2: 1 сек тишины — замеряю фоновый шум...")
                for _ in range(11):
                    if self._stop_event.is_set():
                        return
                    chunk, _ = stream.read(block_size)
                    rms = compute_chunk_rms(chunk.flatten())
                    noise_samples.append(rms)
                    self.level_changed.emit(rms_to_meter_percent(rms))

                # Phase 2: 2.1s user normal speaking voice
                self.phase_changed.emit("Шаг 2/2: Скажи короткую фразу обычным голосом...")
                for _ in range(26):
                    if self._stop_event.is_set():
                        return
                    chunk, _ = stream.read(block_size)
                    rms = compute_chunk_rms(chunk.flatten())
                    speech_samples.append(rms)
                    self.level_changed.emit(rms_to_meter_percent(rms))
        except Exception as e:
            if not self._stop_event.is_set():
                self.failed.emit(f"Ошибка микрофона: {e}")
            return

        if self._stop_event.is_set():
            return

        noise_rms = float(np.percentile(noise_samples, 75)) if noise_samples else 0.006
        speech_rms = float(np.percentile(speech_samples, 85)) if speech_samples else noise_rms
        res = recommend_barge_in_threshold(noise_rms, speech_rms)
        self.level_changed.emit(0)
        self.calibration_ready.emit(res)


class CapriFirstRunWizard(QDialog):
    """Minimal Closed Beta onboarding wizard (LIVING SIGNAL): name, provider/API key (.env), tool policy, avatar."""

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Быстрая настройка — Capri")
        self.setFixedSize(560, 490)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        layout.addLayout(
            _make_dialog_header(
                "Добро пожаловать в Capri",
                "CAPRI // FIRST-RUN SETUP",
                self.reject,
            )
        )

        lbl_intro = QLabel("Настрой имя, провайдера нейросети и политику безопасности. API-ключ сохраняется только в .env.")
        lbl_intro.setWordWrap(True)
        lbl_intro.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12.5px;")
        layout.addWidget(lbl_intro)

        # User name
        lbl_name = QLabel("Как к тебе обращаться?")
        lbl_name.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600;")
        layout.addWidget(lbl_name)
        self.edit_user_name = QLineEdit(getattr(self.config.companion, "user_name", "User") or "User")
        self.edit_user_name.setMinimumHeight(36)
        self.edit_user_name.setPlaceholderText("Например: D, Alex, Kai")
        layout.addWidget(self.edit_user_name)

        # Provider
        lbl_prov = QLabel("Провайдер нейросети")
        lbl_prov.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600;")
        layout.addWidget(lbl_prov)
        self.combo_provider = QComboBox()
        self.combo_provider.setMinimumHeight(36)
        self._provider_ids = ["deepseek", "groq", "gemini", "sambanova", "openrouter", "openai"]
        labels = {
            "deepseek": "DeepSeek (V3 / R1)",
            "groq": "Groq (Быстрый inference)",
            "gemini": "Google Gemini",
            "sambanova": "SambaNova Cloud",
            "openrouter": "OpenRouter",
            "openai": "OpenAI",
        }
        cur_p = (self.config.llm.provider or "deepseek").lower()
        for pid in self._provider_ids:
            self.combo_provider.addItem(labels[pid], pid)
        if cur_p in self._provider_ids:
            self.combo_provider.setCurrentIndex(self._provider_ids.index(cur_p))
        layout.addWidget(self.combo_provider)

        # API key
        lbl_key = QLabel("API-ключ провайдера (сохраняется в .env, не в YAML)")
        lbl_key.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600;")
        layout.addWidget(lbl_key)
        self.edit_api_key = QLineEdit(self.config.llm.api_key or "")
        self.edit_api_key.setMinimumHeight(36)
        self.edit_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_api_key.setPlaceholderText("Вставь API-ключ...")
        layout.addWidget(self.edit_api_key)

        # Tool policy
        lbl_pol = QLabel("Политика безопасности инструментов")
        lbl_pol.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600;")
        layout.addWidget(lbl_pol)
        self.combo_policy = QComboBox()
        self.combo_policy.setMinimumHeight(36)
        self.combo_policy.addItem("Ask — подтверждать изменяющие действия (рекомендуется)", "ask")
        self.combo_policy.addItem("Safe — строго только чтение (без изменений)", "safe")
        self.combo_policy.addItem("Trusted — десктоп авто, shell/файлы/kill с подтверждением", "trusted")
        cur_pol = getattr(getattr(self.config, "safety", None), "tool_policy", "ask")
        for idx in range(self.combo_policy.count()):
            if self.combo_policy.itemData(idx) == cur_pol:
                self.combo_policy.setCurrentIndex(idx)
                break
        layout.addWidget(self.combo_policy)

        # Avatar toggle
        self.chk_avatar = QCheckBox("Включить плавающий desktop-аватар (при наличии композитора)")
        self.chk_avatar.setChecked(bool(getattr(self.config.ui, "avatar_enabled", True)))
        self.chk_avatar.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px;")
        layout.addWidget(self.chk_avatar)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        self.btn_skip = QPushButton("Позже")
        self.btn_skip.setMinimumHeight(36)
        self.btn_skip.setStyleSheet(button_secondary_qss("8px 16px"))
        self.btn_skip.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_skip)

        self.btn_save = QPushButton("Сохранить и продолжить")
        self.btn_save.setIcon(get_icon("check", COLORS.text_on_accent, 14))
        self.btn_save.setMinimumHeight(36)
        self.btn_save.setStyleSheet(button_primary_qss("8px 16px"))
        self.btn_save.clicked.connect(self._apply)
        btn_row.addWidget(self.btn_save)
        layout.addLayout(btn_row)
        apply_dark_palette(self)

    def _apply(self):
        name = self.edit_user_name.text().strip() or "User"
        pid = self.combo_provider.currentData() or "deepseek"
        api_key = self.edit_api_key.text().strip()
        policy = self.combo_policy.currentData() or "ask"

        self.config.companion.user_name = name
        self.config.llm.provider = pid
        if pid in PROVIDER_DEFAULTS:
            self.config.llm.base_url = PROVIDER_DEFAULTS[pid]["base_url"]
            self.config.llm.model = PROVIDER_DEFAULTS[pid]["default_model"]
            update_env_value("LLM_PROVIDER", pid)
            update_env_value("LLM_MODEL", self.config.llm.model)
            if api_key:
                self.config.llm.api_key = api_key
                update_env_value(PROVIDER_DEFAULTS[pid]["env_key"], api_key)
        self.config.safety.tool_policy = policy
        self.config.ui.avatar_enabled = bool(self.chk_avatar.isChecked())
        save_config(self.config)
        self.accept()


class CapriToolApprovalDialog(QDialog):
    """Explicit confirmation dialog for mutating/high-impact tool calls (LIVING SIGNAL)."""

    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request = request
        self.setWindowTitle("Подтверждение действия — Capri")
        self.setMinimumWidth(520)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        is_destructive = getattr(request, "risk", "mutating") in ("destructive", "forbidden")
        tag_color = COLORS.coral if is_destructive else COLORS.amber
        risk_label = "ВЫСОКИЙ РИСК" if is_destructive else "ИЗМЕНЯЮЩЕЕ ДЕЙСТВИЕ"

        layout.addLayout(
            _make_dialog_header(
                f"Разрешить инструмент «{request.tool_name}»?",
                f"CAPRI // TOOL POLICY · {risk_label}",
                self.reject,
            )
        )

        lbl_reason = QLabel(getattr(request, "reason", "Действие изменяет состояние системы."))
        lbl_reason.setWordWrap(True)
        lbl_reason.setStyleSheet(
            f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 13px;"
        )
        layout.addWidget(lbl_reason)

        details_frame = QFrame(self)
        details_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface_code};
                border: 1px solid {tag_color};
                border-radius: 9px;
            }}
        """)
        df_layout = QVBoxLayout(details_frame)
        df_layout.setContentsMargins(12, 10, 12, 10)
        df_layout.setSpacing(4)

        lbl_summary = QLabel(getattr(request, "summary", request.tool_name))
        lbl_summary.setWordWrap(True)
        lbl_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl_summary.setStyleSheet(
            f"color: {COLORS.text_primary}; font-family: {TYPE.mono}; font-size: 12px; border: none;"
        )
        df_layout.addWidget(lbl_summary)
        layout.addWidget(details_frame)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        self.btn_deny = QPushButton("Отклонить")
        self.btn_deny.setMinimumHeight(36)
        self.btn_deny.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_deny.setStyleSheet(button_secondary_qss("8px 16px"))
        self.btn_deny.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_deny)

        self.btn_allow = QPushButton("Разрешить один раз")
        self.btn_allow.setIcon(get_icon("check", "#16070D" if is_destructive else COLORS.text_on_accent, 14))
        self.btn_allow.setMinimumHeight(36)
        self.btn_allow.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_allow.setStyleSheet(
            button_danger_qss(filled=True, padding="8px 16px")
            if is_destructive
            else button_primary_qss("8px 16px")
        )
        self.btn_allow.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_allow)

        layout.addLayout(btn_row)


class CapriResetDialog(QDialog):
    """Engram reset confirmation dialog styled in LIVING SIGNAL."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Сброс энграммы — Capri")
        self.setFixedSize(560, 274)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)

        face_frame = QFrame()
        face_frame.setFixedSize(156, 226)
        face_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.canvas};
                border: 1px solid {COLORS.border_subtle};
                border-radius: 10px;
            }}
        """)
        ff_layout = QVBoxLayout(face_frame)
        ff_layout.setContentsMargins(6, 6, 6, 8)
        ff_layout.setSpacing(6)

        lbl_face = QLabel()
        lbl_face.setAlignment(Qt.AlignmentFlag.AlignCenter)
        face_path = ASSETS_DIR / "capri_face_dialog.png"
        if not face_path.exists():
            face_path = ASSETS_DIR / "avatar.png"

        if face_path.exists():
            pix = QPixmap(str(face_path)).scaled(
                144,
                182,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            lbl_face.setPixmap(pix)
        ff_layout.addWidget(lbl_face)

        lbl_id = QLabel("CONSTRUCT // CAPRI")
        lbl_id.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_id.setStyleSheet(
            f"color: {COLORS.coral}; font-family: {TYPE.mono}; font-size: 9px; font-weight: 600; letter-spacing: 0.6px; border: none;"
        )
        ff_layout.addWidget(lbl_id)
        layout.addWidget(face_frame)

        right_layout = QVBoxLayout()
        right_layout.setSpacing(10)

        lbl_sys_tag = QLabel("ПРОТОКОЛ ПЕРЕЗАГРУЗКИ RELIC 2.0")
        lbl_sys_tag.setStyleSheet(
            f"color: {COLORS.coral}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 600; letter-spacing: 0.8px;"
        )
        right_layout.addWidget(lbl_sys_tag)

        lbl_title = QLabel("Сбросить энграмму?")
        lbl_title.setStyleSheet(
            f"color: {COLORS.text_primary}; font-family: {TYPE.display}; font-size: 19px; font-weight: 600;"
        )
        right_layout.addWidget(lbl_title)

        user_name = getattr(getattr(getattr(parent, "config", None), "companion", None), "user_name", "User") or "User"
        lbl_quote = QLabel(f"«Думаешь, я так просто забуду все твои грешки, {user_name}?»")
        lbl_quote.setWordWrap(True)
        lbl_quote.setStyleSheet(
            f"color: {COLORS.signal_cyan}; font-family: {TYPE.sans}; font-style: italic; font-size: 13px;"
        )
        right_layout.addWidget(lbl_quote)

        lbl_desc = QLabel(
            "Сброс вернёт уровень доверия к базовым +25, обнулит раздражение (0%) "
            "и очистит историю эмоциональных событий и конфликтов."
        )
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(
            f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; line-height: 1.4;"
        )
        right_layout.addWidget(lbl_desc)
        right_layout.addStretch()

        btn_box = QHBoxLayout()
        btn_box.setSpacing(10)
        btn_box.addStretch()

        btn_cancel = QPushButton("Пусть живёт")
        btn_cancel.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_cancel.setMinimumHeight(36)
        btn_cancel.setStyleSheet(button_secondary_qss("8px 16px"))
        btn_cancel.clicked.connect(self.reject)
        btn_box.addWidget(btn_cancel)

        btn_confirm = QPushButton("Сбросить энграмму")
        btn_confirm.setIcon(get_icon("reset", "#16070D", 14))
        btn_confirm.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_confirm.setMinimumHeight(36)
        btn_confirm.setStyleSheet(button_danger_qss(filled=True, padding="8px 16px"))
        btn_confirm.clicked.connect(self.accept)
        btn_box.addWidget(btn_confirm)

        right_layout.addLayout(btn_box)
        layout.addLayout(right_layout)


class CapriChangelogDialog(QDialog):
    """Release notes dialog styled in LIVING SIGNAL."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Capri — Хроника обновлений v{__version__}")
        self.resize(740, 540)
        self.setMinimumSize(520, 380)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        layout.addLayout(
            _make_dialog_header(
                f"История обновлений (v{__version__})",
                "CAPRI // SYSTEM UPDATE CHRONICLES",
                self.close,
            )
        )

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.document().setDefaultStyleSheet(markdown_document_css())
        self.browser.setStyleSheet(f"""
            QTextBrowser {{
                background: {COLORS.canvas};
                color: {COLORS.text_primary};
                border: 1px solid {COLORS.border_subtle};
                border-radius: 10px;
                padding: 14px;
                font-family: {TYPE.sans};
                font-size: 13px;
            }}
            {scrollbar_qss(6)}
        """)

        candidate_paths = [
            Path(__file__).resolve().parent.parent.parent / "CHANGELOG.md",
            Path(__file__).resolve().parent.parent / "CHANGELOG.md",
            Path(sys.prefix) / "share" / "capri" / "CHANGELOG.md",
        ]
        content = ""
        for p in candidate_paths:
            if p.exists():
                try:
                    loaded = p.read_text(encoding="utf-8")
                    if loaded.strip():
                        content = loaded
                        break
                except Exception:
                    pass

        if not content:
            try:
                import importlib.resources as pkg_resources
                content = pkg_resources.files("capri").joinpath("CHANGELOG.md").read_text(encoding="utf-8")
            except Exception:
                pass

        if content:
            self.browser.setMarkdown(content)
        else:
            self.browser.setText("Хроника обновлений (CHANGELOG.md) не найдена.")

        layout.addWidget(self.browser, stretch=1)

        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()

        btn_done = QPushButton("Закрыть")
        btn_done.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_done.setMinimumHeight(36)
        btn_done.setStyleSheet(button_secondary_qss("8px 20px"))
        btn_done.clicked.connect(self.accept)
        bottom_layout.addWidget(btn_done)
        layout.addLayout(bottom_layout)


class CapriProviderSettingsDialog(QDialog):
    """Tabbed LLM core, voice/TTS/STT engines & APIs, Vision AI, and UI/safety settings dialog."""

    def __init__(self, current_config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = current_config
        self._initial_provider = (self.config.llm.provider or "groq").strip().lower()
        self._initial_base_url = (self.config.llm.base_url or "").strip()
        self._cleared_env_keys: set[str] = set()
        self._calib_worker: Optional[MicCalibrationWorker] = None
        self._key_check_workers: dict[str, APIKeyCheckWorker] = {}
        self.setWindowTitle("Настройки нейроядра, голоса и зрения — Capri")
        self.setMinimumSize(620, 620)
        self.resize(680, 660)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        layout.addLayout(
            _make_dialog_header(
                "Нейроядро, голос, зрение и API",
                "CAPRI // CORE, VOICE, VISION & KEYS",
                self.reject,
            )
        )

        def _field_label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600; border: none;"
            )
            return lbl

        def _make_secret_row(
            placeholder: str,
            initial_val: str,
            on_clear=None,
        ) -> tuple[QHBoxLayout, QLineEdit, QPushButton, QPushButton, QPushButton]:
            row = QHBoxLayout()
            row.setSpacing(6)
            edit = QLineEdit()
            edit.setMinimumHeight(36)
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText(placeholder)
            edit.setText(initial_val)
            row.addWidget(edit, stretch=1)

            btn_eye = QPushButton()
            btn_eye.setIcon(get_icon("eye", COLORS.text_secondary, 15))
            btn_eye.setIconSize(QSize(15, 15))
            btn_eye.setFixedSize(36, 36)
            btn_eye.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_eye.setToolTip("Показать / скрыть ключ")
            btn_eye.setStyleSheet(button_secondary_qss("0px"))
            btn_eye.clicked.connect(lambda _=False, e=edit, b=btn_eye: self._toggle_secret_field(e, b))
            row.addWidget(btn_eye)

            btn_paste = QPushButton("Вставить")
            btn_paste.setIcon(get_icon("paste", COLORS.text_secondary, 14))
            btn_paste.setIconSize(QSize(14, 14))
            btn_paste.setFixedHeight(36)
            btn_paste.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_paste.setToolTip("Вставить ключ из буфера обмена")
            btn_paste.setStyleSheet(button_secondary_qss("0px 10px"))
            btn_paste.clicked.connect(lambda _=False, e=edit: self._paste_into_field(e))
            row.addWidget(btn_paste)

            btn_clear = QPushButton()
            btn_clear.setIcon(get_icon("trash", COLORS.coral, 14))
            btn_clear.setIconSize(QSize(14, 14))
            btn_clear.setFixedSize(36, 36)
            btn_clear.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_clear.setToolTip("Удалить сохранённый ключ из .env")
            btn_clear.setStyleSheet(button_danger_qss(filled=False, padding="0px"))
            if on_clear is not None:
                btn_clear.clicked.connect(on_clear)
            else:
                btn_clear.clicked.connect(edit.clear)
            row.addWidget(btn_clear)
            return row, edit, btn_eye, btn_paste, btn_clear

        self.tabs = QTabWidget()
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
                background: {COLORS.surface_alt};
                top: -1px;
            }}
            QTabBar {{
                background: transparent;
                border: none;
            }}
            QTabBar::tab {{
                background: {COLORS.surface};
                color: {COLORS.text_secondary};
                border: 1px solid {COLORS.border_hairline};
                border-bottom: none;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                padding: 8px 14px;
                margin-right: 4px;
                font-family: {TYPE.sans};
                font-size: 12px;
                font-weight: 600;
            }}
            QTabBar::tab:selected {{
                background: {COLORS.surface_alt};
                color: {COLORS.signal_cyan};
                border-color: {COLORS.signal_cyan_border};
            }}
            QTabBar::tab:hover:!selected {{
                color: {COLORS.text_primary};
            }}
        """)

        # =====================================================================
        # Tab 1: LLM Core
        # =====================================================================
        tab_llm = QWidget()
        tab_llm.setStyleSheet("background: transparent;")
        llm_layout = QVBoxLayout(tab_llm)
        llm_layout.setContentsMargins(16, 14, 16, 14)
        llm_layout.setSpacing(10)

        llm_layout.addWidget(_field_label("Провайдер нейросети"))

        self.combo_provider = QComboBox()
        self.combo_provider.setMinimumHeight(36)
        self.providers_map = {
            "DeepSeek (Официальный API / V3 & R1)": "deepseek",
            "Groq (Сверхбыстрый / Рекомендуется)": "groq",
            "SambaNova (Llama 3.3 70B / gpt-oss-120b)": "sambanova",
            "Google Gemini (Flash / Pro)": "gemini",
            "OpenRouter (DeepSeek / Claude / GPT)": "openrouter",
            "OpenAI (Официальный API)": "openai",
        }
        for label in self.providers_map:
            self.combo_provider.addItem(label)

        curr_p = (self.config.llm.provider or "groq").lower()
        for idx, (_label, p_id) in enumerate(self.providers_map.items()):
            if p_id == curr_p:
                self.combo_provider.setCurrentIndex(idx)
                break
        self.combo_provider.currentIndexChanged.connect(self._on_provider_changed)
        llm_layout.addWidget(self.combo_provider)

        llm_layout.addWidget(_field_label("API-ключ LLM (хранится только в .env, права 0600)"))
        key_box, self.edit_key, self.btn_show_key, self.btn_paste_key, self.btn_clear_key = _make_secret_row(
            "Вставь API-ключ выбранного провайдера...",
            self._get_current_key_for_provider(curr_p),
            on_clear=self._on_clear_llm_key,
        )
        llm_layout.addLayout(key_box)

        llm_check_row = QHBoxLayout()
        llm_check_row.setSpacing(8)
        self.btn_check_llm_key = QPushButton("Проверить LLM API")
        self.btn_check_llm_key.setIcon(get_icon("check", COLORS.signal_cyan, 13))
        self.btn_check_llm_key.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_check_llm_key.setFixedHeight(30)
        self.btn_check_llm_key.setStyleSheet(button_secondary_qss("4px 12px", radius=7))
        self.btn_check_llm_key.clicked.connect(self._on_check_llm_key)
        llm_check_row.addWidget(self.btn_check_llm_key)

        self.lbl_llm_check_status = QLabel("Нажми, чтобы проверить доступность ключа и Base URL")
        self.lbl_llm_check_status.setWordWrap(True)
        self.lbl_llm_check_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        llm_check_row.addWidget(self.lbl_llm_check_status, stretch=1)
        llm_layout.addLayout(llm_check_row)

        llm_layout.addWidget(_field_label("Модель (выбери или введи свою)"))
        self.combo_model = QComboBox()
        self.combo_model.setMinimumHeight(36)
        self.combo_model.setEditable(True)
        self._populate_models_for_provider(curr_p)
        llm_layout.addWidget(self.combo_model)

        llm_layout.addWidget(_field_label("API Base URL"))
        self.edit_base_url = QLineEdit()
        self.edit_base_url.setMinimumHeight(36)
        known_defaults = {v.get("base_url") for v in PROVIDER_DEFAULTS.values() if v.get("base_url")}
        prov_default_url = PROVIDER_DEFAULTS.get(curr_p, {}).get("base_url", "")
        cur_url = (self.config.llm.base_url or "").strip()
        if not cur_url or (cur_url in known_defaults and cur_url != prov_default_url):
            cur_url = prov_default_url
        self.edit_base_url.setText(cur_url)
        self._initial_base_url = cur_url
        llm_layout.addWidget(self.edit_base_url)
        llm_layout.addStretch()

        # =====================================================================
        # Tab 2: Interface, Profile & Safety
        # =====================================================================
        tab_pref = QWidget()
        tab_pref.setStyleSheet("background: transparent;")
        pref_layout = QVBoxLayout(tab_pref)
        pref_layout.setContentsMargins(16, 14, 16, 14)
        pref_layout.setSpacing(10)

        pref_layout.addWidget(_field_label("Как к тебе обращаться (имя в диалоге и промпте)"))
        self.edit_user_name = QLineEdit()
        self.edit_user_name.setMinimumHeight(36)
        self.edit_user_name.setPlaceholderText("Например: D, Alex, Kai...")
        self.edit_user_name.setText(getattr(self.config.companion, "user_name", "User") or "User")
        pref_layout.addWidget(self.edit_user_name)

        pref_layout.addWidget(_field_label("Политика безопасности инструментов (ToolPolicyEngine)"))
        self.combo_policy = QComboBox()
        self.combo_policy.setMinimumHeight(36)
        self.combo_policy.addItem("Ask — запрашивать подтверждение на изменения (Рекомендуется)", "ask")
        self.combo_policy.addItem("Safe — строгий read-only режим (запрет любых изменений)", "safe")
        self.combo_policy.addItem("Trusted — десктоп авто, shell/файлы/kill с подтверждением", "trusted")
        curr_pol = (getattr(self.config.safety, "tool_policy", "ask") or "ask").lower()
        pol_idx = self.combo_policy.findData(curr_pol)
        if pol_idx >= 0:
            self.combo_policy.setCurrentIndex(pol_idx)
        pref_layout.addWidget(self.combo_policy)

        toggles_frame = QFrame()
        toggles_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        tl = QVBoxLayout(toggles_frame)
        tl.setContentsMargins(14, 12, 14, 12)
        tl.setSpacing(10)

        self.chk_avatar_enabled = QCheckBox("Включить рабочий desktop-аватар Capri")
        self.chk_avatar_enabled.setChecked(bool(getattr(self.config.ui, "avatar_enabled", True)))
        tl.addWidget(self.chk_avatar_enabled)

        self.chk_auto_avatar_poses = QCheckBox("Авто-позы аватара (музыка MPRIS, сон и живые реакции)")
        self.chk_auto_avatar_poses.setChecked(bool(getattr(self.config.ui, "auto_avatar_poses", True)))
        tl.addWidget(self.chk_auto_avatar_poses)

        self.chk_game_mode = QCheckBox("Игровой режим / Power Saver (автопауза в fullscreen-играх)")
        self.chk_game_mode.setChecked(bool(getattr(self.config.ui, "game_mode_enabled", True)))
        tl.addWidget(self.chk_game_mode)

        self.chk_reduced_motion = QCheckBox("Уменьшить анимации интерфейса (Reduced Motion)")
        self.chk_reduced_motion.setChecked(bool(getattr(self.config.ui, "reduced_motion", False)))
        tl.addWidget(self.chk_reduced_motion)

        self.chk_minimize_to_tray = QCheckBox("Сворачивать в системный трей при закрытии окна")
        self.chk_minimize_to_tray.setChecked(bool(getattr(self.config.ui, "minimize_to_tray", True)))
        tl.addWidget(self.chk_minimize_to_tray)

        pref_layout.addWidget(toggles_frame)
        pref_layout.addStretch()

        # =====================================================================
        # Tab 3: Voice, TTS/STT Engines, Fish Audio / ElevenLabs APIs & PipeWire AEC
        # =====================================================================
        tab_voice_scroll = QScrollArea()
        tab_voice_scroll.setWidgetResizable(True)
        tab_voice_scroll.setFrameShape(QFrame.Shape.NoFrame)
        tab_voice_scroll.setStyleSheet(f"QScrollArea {{ background: transparent; border: none; }} {scrollbar_qss(6)}")

        tab_voice = QWidget()
        tab_voice.setStyleSheet("background: transparent;")
        voice_layout = QVBoxLayout(tab_voice)
        voice_layout.setContentsMargins(16, 14, 16, 14)
        voice_layout.setSpacing(10)

        # TTS Engine & STT Mode selectors
        engines_grid = QGridLayout()
        engines_grid.setSpacing(10)

        engines_grid.addWidget(_field_label("Движок озвучки (TTS)"), 0, 0)
        engines_grid.addWidget(_field_label("Распознавание речи (STT)"), 0, 1)

        self.combo_tts_engine = QComboBox()
        self.combo_tts_engine.setMinimumHeight(36)
        self.combo_tts_engine.addItem("Fish Audio (Нейронный голос)", "fish")
        self.combo_tts_engine.addItem("Edge TTS (Бесплатный облачный)", "edge")
        self.combo_tts_engine.addItem("ElevenLabs (Облачный)", "elevenlabs")
        self.combo_tts_engine.addItem("Gemini TTS (Google)", "gemini")
        self.combo_tts_engine.addItem("Silero TTS (Локальный оффлайн)", "silero")
        cur_tts = (getattr(self.config.voice, "tts_engine", "fish") or "fish").strip().lower()
        tts_idx = self.combo_tts_engine.findData(cur_tts)
        self.combo_tts_engine.setCurrentIndex(tts_idx if tts_idx >= 0 else 0)
        self.combo_tts_engine.currentIndexChanged.connect(self._sync_tts_provider_panels)
        engines_grid.addWidget(self.combo_tts_engine, 1, 0)

        self.combo_stt_mode = QComboBox()
        self.combo_stt_mode.setMinimumHeight(36)
        self.combo_stt_mode.addItem("Groq Whisper Turbo (Облачный)", "groq")
        self.combo_stt_mode.addItem("Local Whisper (Оффлайн)", "local")
        cur_stt = (getattr(self.config.voice, "stt_mode", "groq") or "groq").strip().lower()
        stt_idx = self.combo_stt_mode.findData(cur_stt)
        self.combo_stt_mode.setCurrentIndex(stt_idx if stt_idx >= 0 else 0)
        engines_grid.addWidget(self.combo_stt_mode, 1, 1)
        voice_layout.addLayout(engines_grid)

        # Fish Audio API & Voice ID panel
        self.fish_panel = QFrame()
        self.fish_panel.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        fp_l = QVBoxLayout(self.fish_panel)
        fp_l.setContentsMargins(14, 11, 14, 11)
        fp_l.setSpacing(8)
        fp_l.addWidget(_field_label("Fish Audio API-ключ (FISH_AUDIO_API_KEY)"))
        fish_key_val = (
            getattr(self.config.voice, "fish_audio_api_key", "")
            or os.getenv("FISH_AUDIO_API_KEY", "")
        ).strip()
        fish_row, self.edit_fish_key, self.btn_show_fish_key, self.btn_paste_fish_key, self.btn_clear_fish_key = _make_secret_row(
            "Вставь Fish Audio API-ключ...",
            fish_key_val,
            on_clear=lambda: self._mark_env_key_cleared("FISH_AUDIO_API_KEY", self.edit_fish_key, self.lbl_fish_check_status),
        )
        fp_l.addLayout(fish_row)

        fp_l.addWidget(_field_label("Fish Audio Voice / Reference ID (FISH_AUDIO_VOICE_ID)"))
        self.edit_fish_voice_id = QLineEdit()
        self.edit_fish_voice_id.setMinimumHeight(34)
        self.edit_fish_voice_id.setPlaceholderText("ID модели голоса на fish.audio (опционально)")
        self.edit_fish_voice_id.setText(
            (getattr(self.config.voice, "fish_audio_voice_id", "") or os.getenv("FISH_AUDIO_VOICE_ID", "")).strip()
        )
        fp_l.addWidget(self.edit_fish_voice_id)

        fish_chk_row = QHBoxLayout()
        fish_chk_row.setSpacing(8)
        self.btn_check_fish_key = QPushButton("Проверить Fish API")
        self.btn_check_fish_key.setIcon(get_icon("check", COLORS.signal_cyan, 13))
        self.btn_check_fish_key.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_check_fish_key.setFixedHeight(30)
        self.btn_check_fish_key.setStyleSheet(button_secondary_qss("4px 12px", radius=7))
        self.btn_check_fish_key.clicked.connect(self._on_check_fish_key)
        fish_chk_row.addWidget(self.btn_check_fish_key)

        self.lbl_fish_check_status = QLabel("Ключ сохраняется в .env (0600)")
        self.lbl_fish_check_status.setWordWrap(True)
        self.lbl_fish_check_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        fish_chk_row.addWidget(self.lbl_fish_check_status, stretch=1)
        fp_l.addLayout(fish_chk_row)
        voice_layout.addWidget(self.fish_panel)

        # ElevenLabs API & Voice ID panel (shown when ElevenLabs is selected)
        self.eleven_panel = QFrame()
        self.eleven_panel.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        el_l = QVBoxLayout(self.eleven_panel)
        el_l.setContentsMargins(14, 11, 14, 11)
        el_l.setSpacing(8)
        el_l.addWidget(_field_label("ElevenLabs API-ключ (ELEVENLABS_API_KEY)"))
        el_key_val = (
            getattr(self.config.voice, "elevenlabs_api_key", "")
            or os.getenv("ELEVENLABS_API_KEY", "")
        ).strip()
        el_row, self.edit_eleven_key, self.btn_show_eleven_key, self.btn_paste_eleven_key, self.btn_clear_eleven_key = _make_secret_row(
            "Вставь ElevenLabs API-ключ...",
            el_key_val,
            on_clear=lambda: self._mark_env_key_cleared("ELEVENLABS_API_KEY", self.edit_eleven_key, self.lbl_eleven_check_status),
        )
        el_l.addLayout(el_row)

        el_l.addWidget(_field_label("ElevenLabs Voice ID (ELEVENLABS_VOICE_ID)"))
        self.edit_eleven_voice_id = QLineEdit()
        self.edit_eleven_voice_id.setMinimumHeight(34)
        self.edit_eleven_voice_id.setPlaceholderText("ID голоса ElevenLabs")
        self.edit_eleven_voice_id.setText(
            (getattr(self.config.voice, "elevenlabs_voice_id", "") or os.getenv("ELEVENLABS_VOICE_ID", "")).strip()
        )
        el_l.addWidget(self.edit_eleven_voice_id)

        el_chk_row = QHBoxLayout()
        el_chk_row.setSpacing(8)
        self.btn_check_eleven_key = QPushButton("Проверить ElevenLabs")
        self.btn_check_eleven_key.setIcon(get_icon("check", COLORS.signal_cyan, 13))
        self.btn_check_eleven_key.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_check_eleven_key.setFixedHeight(30)
        self.btn_check_eleven_key.setStyleSheet(button_secondary_qss("4px 12px", radius=7))
        self.btn_check_eleven_key.clicked.connect(self._on_check_eleven_key)
        el_chk_row.addWidget(self.btn_check_eleven_key)

        self.lbl_eleven_check_status = QLabel("Ключ сохраняется в .env (0600)")
        self.lbl_eleven_check_status.setWordWrap(True)
        self.lbl_eleven_check_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        el_chk_row.addWidget(self.lbl_eleven_check_status, stretch=1)
        el_l.addLayout(el_chk_row)
        voice_layout.addWidget(self.eleven_panel)

        self.lbl_tts_engine_hint = QLabel("")
        self.lbl_tts_engine_hint.setWordWrap(True)
        self.lbl_tts_engine_hint.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        voice_layout.addWidget(self.lbl_tts_engine_hint)

        voice_toggles = QFrame()
        voice_toggles.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        vtl = QVBoxLayout(voice_toggles)
        vtl.setContentsMargins(14, 12, 14, 12)
        vtl.setSpacing(9)

        self.chk_tts_enabled = QCheckBox("Озвучивать ответы Капри (TTS)")
        self.chk_tts_enabled.setChecked(bool(getattr(self.config.voice, "tts_enabled", True)))
        vtl.addWidget(self.chk_tts_enabled)

        self.chk_wake_word_enabled = QCheckBox("Активация голосом Wake-Word («Капри», оффлайн Vosk)")
        self.chk_wake_word_enabled.setChecked(bool(getattr(self.config.voice, "wake_word_enabled", False)))
        vtl.addWidget(self.chk_wake_word_enabled)

        self.chk_hands_free_barge_in = QCheckBox("Перебивать речь голосом (Hands-Free Barge-In)")
        self.chk_hands_free_barge_in.setChecked(bool(getattr(self.config.voice, "hands_free_barge_in", True)))
        vtl.addWidget(self.chk_hands_free_barge_in)

        self.chk_pipewire_aec = QCheckBox("Эхоподавление PipeWire WebRTC AEC (для колонок)")
        self.chk_pipewire_aec.setChecked(bool(getattr(self.config.voice, "pipewire_aec_enabled", False)))
        vtl.addWidget(self.chk_pipewire_aec)

        aec_row = QHBoxLayout()
        aec_row.setSpacing(8)
        self.lbl_aec_status = QLabel("PipeWire AEC: проверка...")
        self.lbl_aec_status.setWordWrap(True)
        self.lbl_aec_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        aec_row.addWidget(self.lbl_aec_status, stretch=1)

        self.btn_toggle_aec = QPushButton("Включить AEC")
        self.btn_toggle_aec.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_toggle_aec.setFixedHeight(30)
        self.btn_toggle_aec.setStyleSheet(button_secondary_qss("4px 12px", radius=7))
        self.btn_toggle_aec.clicked.connect(self._on_toggle_aec_clicked)
        aec_row.addWidget(self.btn_toggle_aec)
        vtl.addLayout(aec_row)
        voice_layout.addWidget(voice_toggles)

        voice_layout.addWidget(_field_label("Микрофон (источник ввода PipeWire / sounddevice)"))
        self.combo_input_device = QComboBox()
        self.combo_input_device.setMinimumHeight(34)
        self._populate_input_devices()
        voice_layout.addWidget(self.combo_input_device)

        calib_frame = QFrame()
        calib_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        cl = QVBoxLayout(calib_frame)
        cl.setContentsMargins(14, 11, 14, 11)
        cl.setSpacing(8)

        thr_hdr = QHBoxLayout()
        lbl_thr_title = QLabel("Порог срабатывания Barge-In (RMS)")
        lbl_thr_title.setStyleSheet(
            f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600; border: none;"
        )
        thr_hdr.addWidget(lbl_thr_title)
        thr_hdr.addStretch()

        init_thr = max(0.015, min(0.200, float(getattr(self.config.voice, "barge_in_rms_threshold", 0.032))))
        self.lbl_barge_threshold_val = QLabel(f"{init_thr:.3f}")
        self.lbl_barge_threshold_val.setStyleSheet(
            f"color: {COLORS.signal_cyan}; font-family: {TYPE.mono}; font-size: 12px; font-weight: 700; border: none;"
        )
        thr_hdr.addWidget(self.lbl_barge_threshold_val)
        cl.addLayout(thr_hdr)

        self.slider_barge_threshold = QSlider(Qt.Orientation.Horizontal)
        self.slider_barge_threshold.setRange(15, 200)
        self.slider_barge_threshold.setValue(int(round(init_thr * 1000)))
        self.slider_barge_threshold.valueChanged.connect(
            lambda v: self.lbl_barge_threshold_val.setText(f"{v / 1000.0:.3f}")
        )
        cl.addWidget(self.slider_barge_threshold)

        self.bar_mic_level = QProgressBar()
        self.bar_mic_level.setRange(0, 100)
        self.bar_mic_level.setValue(0)
        self.bar_mic_level.setFixedHeight(8)
        self.bar_mic_level.setTextVisible(False)
        self.bar_mic_level.setStyleSheet(f"""
            QProgressBar {{
                background: {COLORS.canvas};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background: {COLORS.mint};
                border-radius: 3px;
            }}
        """)
        cl.addWidget(self.bar_mic_level)

        calib_row = QHBoxLayout()
        calib_row.setSpacing(8)
        self.btn_calibrate_mic = QPushButton("Калибровать микрофон (3 сек)")
        self.btn_calibrate_mic.setIcon(get_icon("mic", COLORS.signal_cyan, 14))
        self.btn_calibrate_mic.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_calibrate_mic.setFixedHeight(32)
        self.btn_calibrate_mic.setStyleSheet(button_secondary_qss("4px 12px", radius=7))
        self.btn_calibrate_mic.clicked.connect(self._on_calibrate_mic_clicked)
        calib_row.addWidget(self.btn_calibrate_mic)

        self.lbl_calibration_status = QLabel("Наушники: ~0.032 · Колонки: ~0.065 или AEC")
        self.lbl_calibration_status.setWordWrap(True)
        self.lbl_calibration_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10.5px; border: none;"
        )
        calib_row.addWidget(self.lbl_calibration_status, stretch=1)
        cl.addLayout(calib_row)

        voice_layout.addWidget(calib_frame)
        voice_layout.addStretch()
        tab_voice_scroll.setWidget(tab_voice)

        # =====================================================================
        # Tab 4: Vision AI ("Просмотр экрана") & Gemini API + Keys Status
        # =====================================================================
        tab_vision = QWidget()
        tab_vision.setStyleSheet("background: transparent;")
        vision_layout = QVBoxLayout(tab_vision)
        vision_layout.setContentsMargins(16, 14, 16, 14)
        vision_layout.setSpacing(10)

        self.chk_vision_enabled = QCheckBox("Включить просмотр экрана и анализ изображений (/look, Ctrl+V)")
        self.chk_vision_enabled.setChecked(bool(getattr(self.config.vision, "enabled", True)))
        vision_layout.addWidget(self.chk_vision_enabled)

        vision_layout.addWidget(_field_label("Метод анализа экрана (Vision Provider)"))
        self.combo_vision_provider = QComboBox()
        self.combo_vision_provider.setMinimumHeight(36)
        self.combo_vision_provider.addItem("Google Gemini Vision AI (Рекомендуется)", "gemini")
        self.combo_vision_provider.addItem("Локальный OCR (Tesseract, оффлайн)", "ocr")
        cur_vp = (getattr(self.config.vision, "provider", "gemini") or "gemini").strip().lower()
        vp_idx = self.combo_vision_provider.findData(cur_vp)
        self.combo_vision_provider.setCurrentIndex(vp_idx if vp_idx >= 0 else 0)
        vision_layout.addWidget(self.combo_vision_provider)

        vision_layout.addWidget(_field_label("Модель Gemini Vision"))
        self.combo_vision_model = QComboBox()
        self.combo_vision_model.setMinimumHeight(36)
        self.combo_vision_model.setEditable(True)
        for vm in ("gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"):
            self.combo_vision_model.addItem(vm)
        cur_vm = (getattr(self.config.vision, "model", "gemini-3.1-flash-lite") or "gemini-3.1-flash-lite").strip()
        vm_idx = self.combo_vision_model.findText(cur_vm)
        if vm_idx >= 0:
            self.combo_vision_model.setCurrentIndex(vm_idx)
        else:
            self.combo_vision_model.setCurrentText(cur_vm)
        vision_layout.addWidget(self.combo_vision_model)

        vision_layout.addWidget(_field_label("Gemini API-ключ (GEMINI_API_KEY — для просмотра экрана и Gemini TTS)"))
        gem_key_val = (
            getattr(self.config.vision, "gemini_api_key", "")
            or getattr(self.config.voice, "gemini_api_key", "")
            or os.getenv("GEMINI_API_KEY", "")
        ).strip()
        gem_row, self.edit_gemini_key, self.btn_show_gemini_key, self.btn_paste_gemini_key, self.btn_clear_gemini_key = _make_secret_row(
            "Вставь Google Gemini API-ключ (AIza...)...",
            gem_key_val,
            on_clear=lambda: self._mark_env_key_cleared("GEMINI_API_KEY", self.edit_gemini_key, self.lbl_gemini_check_status),
        )
        vision_layout.addLayout(gem_row)

        gem_chk_row = QHBoxLayout()
        gem_chk_row.setSpacing(8)
        self.btn_check_gemini_key = QPushButton("Проверить Gemini API")
        self.btn_check_gemini_key.setIcon(get_icon("check", COLORS.signal_cyan, 13))
        self.btn_check_gemini_key.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_check_gemini_key.setFixedHeight(30)
        self.btn_check_gemini_key.setStyleSheet(button_secondary_qss("4px 12px", radius=7))
        self.btn_check_gemini_key.clicked.connect(self._on_check_gemini_key)
        gem_chk_row.addWidget(self.btn_check_gemini_key)

        self.lbl_gemini_check_status = QLabel("Используется для /look, скриншотов Ctrl+V и Gemini TTS")
        self.lbl_gemini_check_status.setWordWrap(True)
        self.lbl_gemini_check_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        gem_chk_row.addWidget(self.lbl_gemini_check_status, stretch=1)
        vision_layout.addLayout(gem_chk_row)

        # Compact live API keys summary box
        keys_summary_frame = QFrame()
        keys_summary_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        ks_l = QVBoxLayout(keys_summary_frame)
        ks_l.setContentsMargins(14, 11, 14, 11)
        ks_l.setSpacing(6)
        ks_l.addWidget(_field_label("Статус настроенных API-ключей (.env)"))
        self.lbl_keys_overview = QLabel("")
        self.lbl_keys_overview.setWordWrap(True)
        self.lbl_keys_overview.setStyleSheet(
            f"color: {COLORS.text_secondary}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        ks_l.addWidget(self.lbl_keys_overview)
        vision_layout.addWidget(keys_summary_frame)
        vision_layout.addStretch()

        self._sync_tts_provider_panels()
        self._refresh_aec_status_ui()
        self._refresh_keys_overview()

        for ed in (self.edit_key, self.edit_fish_key, self.edit_eleven_key, self.edit_gemini_key):
            ed.textChanged.connect(lambda _t: self._refresh_keys_overview())

        self.tabs.addTab(tab_llm, "Нейроядро")
        self.tabs.addTab(tab_voice_scroll, "Голос и аудио")
        self.tabs.addTab(tab_vision, "Зрение и API")
        self.tabs.addTab(tab_pref, "Интерфейс и безопасность")
        layout.addWidget(self.tabs, stretch=1)

        btn_box = QHBoxLayout()
        btn_box.setSpacing(10)

        btn_cancel = QPushButton("Отмена")
        btn_cancel.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_cancel.setMinimumHeight(36)
        btn_cancel.setStyleSheet(button_secondary_qss("8px 16px"))
        btn_cancel.clicked.connect(self.reject)
        btn_box.addWidget(btn_cancel)

        btn_box.addStretch()

        self.btn_save = QPushButton("Применить и сохранить")
        self.btn_save.setIcon(get_icon("check", COLORS.text_on_accent, 14))
        self.btn_save.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_save.setMinimumHeight(36)
        self.btn_save.setStyleSheet(button_primary_qss("8px 18px"))
        self.btn_save.clicked.connect(self._apply_and_save)
        btn_box.addWidget(self.btn_save)

        layout.addLayout(btn_box)
        apply_dark_palette(self)

    def _mark_env_key_cleared(self, env_var: str, field: QLineEdit, status_lbl: Optional[QLabel] = None):
        self._cleared_env_keys.add(env_var)
        field.clear()
        if status_lbl is not None:
            status_lbl.setText(f"Ключ {env_var} будет удалён из .env после сохранения")
            status_lbl.setStyleSheet(
                f"color: {COLORS.amber}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
            )
        if hasattr(self, "lbl_keys_overview"):
            self._refresh_keys_overview()

    def _on_clear_llm_key(self):
        p_id = self._get_selected_provider_id()
        env_var = PROVIDER_DEFAULTS.get(p_id, {}).get("env_key", f"{p_id.upper()}_API_KEY")
        self._mark_env_key_cleared(env_var, self.edit_key, self.lbl_llm_check_status)

    def _toggle_secret_field(self, field: QLineEdit, btn: QPushButton):
        if field.echoMode() == QLineEdit.EchoMode.Password:
            field.setEchoMode(QLineEdit.EchoMode.Normal)
            btn.setIcon(get_icon("eye-off", COLORS.signal_cyan, 15))
        else:
            field.setEchoMode(QLineEdit.EchoMode.Password)
            btn.setIcon(get_icon("eye", COLORS.text_secondary, 15))

    def _paste_into_field(self, field: QLineEdit):
        clipboard = QApplication.clipboard()
        text = clipboard.text().strip()
        if text:
            field.setText(text)

    def _sync_tts_provider_panels(self):
        eng = (self.combo_tts_engine.currentData() or "fish").lower()
        self.fish_panel.setVisible(eng == "fish")
        self.eleven_panel.setVisible(eng == "elevenlabs")
        hints = {
            "fish": "",
            "edge": "Edge TTS работает бесплатно без API-ключа (требуется интернет).",
            "elevenlabs": "",
            "gemini": "Gemini TTS использует Gemini API-ключ со вкладки «Зрение и API».",
            "silero": "Silero TTS синтезирует речь полностью локально на CPU/GPU без API-ключей.",
        }
        txt = hints.get(eng, "")
        self.lbl_tts_engine_hint.setText(txt)
        self.lbl_tts_engine_hint.setVisible(bool(txt))

    def _refresh_keys_overview(self):
        p_id = self._get_selected_provider_id()
        cur_llm_k = self.edit_key.text().strip()
        groq_ok = bool(cur_llm_k if p_id == "groq" else os.getenv("GROQ_API_KEY", "").strip())
        ds_ok = bool(cur_llm_k if p_id == "deepseek" else os.getenv("DEEPSEEK_API_KEY", "").strip())
        gem_ok = bool(self.edit_gemini_key.text().strip() or (cur_llm_k if p_id == "gemini" else ""))
        fish_ok = bool(self.edit_fish_key.text().strip())
        el_ok = bool(self.edit_eleven_key.text().strip())

        def _badge(name: str, ok: bool) -> str:
            return f"● {name}: OK" if ok else f"○ {name}: —"

        self.lbl_keys_overview.setText(
            f"{_badge('Groq', groq_ok)}   ·   {_badge('DeepSeek', ds_ok)}   ·   {_badge('Gemini Vision', gem_ok)}\n"
            f"{_badge('Fish Audio', fish_ok)}   ·   {_badge('ElevenLabs', el_ok)}"
        )

    def _start_api_key_check(
        self,
        service: str,
        api_key: str,
        btn: QPushButton,
        status_lbl: QLabel,
        base_url: str = "",
    ):
        prev = self._key_check_workers.get(service)
        if prev is not None and prev.isRunning():
            return
        if not (api_key or "").strip():
            status_lbl.setText("✕ Сначала введи API-ключ")
            status_lbl.setStyleSheet(
                f"color: {COLORS.coral}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
            )
            return
        btn.setEnabled(False)
        status_lbl.setText("⏳ Проверка ключа...")
        status_lbl.setStyleSheet(
            f"color: {COLORS.amber}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        worker = APIKeyCheckWorker(service=service, api_key=api_key, base_url=base_url, parent=self)
        self._key_check_workers[service] = worker
        _track_qthread(worker)
        worker.result_ready.connect(
            lambda svc, ok, msg, b=btn, lbl=status_lbl: self._on_api_key_check_done(svc, ok, msg, b, lbl)
        )
        worker.start()

    def _on_api_key_check_done(
        self,
        service: str,
        ok: bool,
        msg: str,
        btn: QPushButton,
        status_lbl: QLabel,
    ):
        btn.setEnabled(True)
        color = COLORS.mint if ok else COLORS.coral
        prefix = "● " if ok else "✕ "
        status_lbl.setText(f"{prefix}{msg}")
        status_lbl.setStyleSheet(
            f"color: {color}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )

    def _on_check_llm_key(self):
        self._start_api_key_check(
            "llm",
            self.edit_key.text().strip(),
            self.btn_check_llm_key,
            self.lbl_llm_check_status,
            base_url=self.edit_base_url.text().strip(),
        )

    def _on_check_fish_key(self):
        self._start_api_key_check(
            "fish",
            self.edit_fish_key.text().strip(),
            self.btn_check_fish_key,
            self.lbl_fish_check_status,
        )

    def _on_check_eleven_key(self):
        self._start_api_key_check(
            "elevenlabs",
            self.edit_eleven_key.text().strip(),
            self.btn_check_eleven_key,
            self.lbl_eleven_check_status,
        )

    def _on_check_gemini_key(self):
        self._start_api_key_check(
            "gemini",
            self.edit_gemini_key.text().strip(),
            self.btn_check_gemini_key,
            self.lbl_gemini_check_status,
        )

    def _stop_key_check_workers(self):
        for w in list(self._key_check_workers.values()):
            try:
                w.result_ready.disconnect()
            except Exception:
                pass
            if w.isRunning():
                w.wait(1000)
        self._key_check_workers.clear()

    def _get_selected_provider_id(self) -> str:
        current_text = self.combo_provider.currentText()
        return self.providers_map.get(current_text, "groq")

    def _get_current_key_for_provider(self, provider_id: str) -> str:
        if (self.config.llm.provider or "").lower() == provider_id and self.config.llm.api_key:
            return self.config.llm.api_key
        env_var = PROVIDER_DEFAULTS.get(provider_id, {}).get("env_key", "")
        return os.getenv(env_var, "")

    def _on_provider_changed(self):
        p_id = self._get_selected_provider_id()
        default_url = PROVIDER_DEFAULTS.get(p_id, {}).get("base_url", "")
        self.edit_base_url.setText(default_url)
        key = self._get_current_key_for_provider(p_id)
        self.edit_key.setText(key)
        self._populate_models_for_provider(p_id)
        if hasattr(self, "lbl_keys_overview"):
            self._refresh_keys_overview()

    def _populate_models_for_provider(self, provider_id: str):
        self.combo_model.clear()
        models_by_provider = {
            "deepseek": ["deepseek-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
            "groq": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
            "sambanova": ["Meta-Llama-3.3-70B-Instruct", "gpt-oss-120b", "MiniMax-M3.1-Flash", "gemma-4-31B-it"],
            "gemini": ["gemini-2.5-flash", "gemini-2.5-pro"],
            "openrouter": ["deepseek/deepseek-chat", "openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"],
            "openai": ["gpt-4o-mini", "gpt-4o"],
        }
        models = models_by_provider.get(provider_id, ["openai/gpt-oss-120b"])
        for m in models:
            self.combo_model.addItem(m)

        curr_m = self.config.llm.model
        if curr_m:
            idx = self.combo_model.findText(curr_m)
            if idx >= 0:
                self.combo_model.setCurrentIndex(idx)
            elif self.combo_model.count() > 0:
                self.combo_model.setCurrentIndex(0)

    def _toggle_key_visibility(self):
        self._toggle_secret_field(self.edit_key, self.btn_show_key)

    def _paste_clipboard_key(self):
        self._paste_into_field(self.edit_key)

    def _populate_input_devices(self):
        self.combo_input_device.clear()
        current_dev = (getattr(self.config.voice, "input_device", "") or "").strip()
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            devices = [("", "Системный микрофон по умолчанию (Auto)")]
        else:
            from ..voice.pipewire import list_input_devices
            devices = list_input_devices()
        for key, label in devices:
            self.combo_input_device.addItem(label, key)
        if current_dev and self.combo_input_device.findData(current_dev) < 0:
            self.combo_input_device.addItem(current_dev, current_dev)
        idx = self.combo_input_device.findData(current_dev)
        self.combo_input_device.setCurrentIndex(idx if idx >= 0 else 0)

    def _refresh_aec_status_ui(self):
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            self.lbl_aec_status.setText("PipeWire AEC: готов к настройке")
            self.btn_toggle_aec.setText("Включить AEC")
            return
        from ..voice.pipewire import get_pipewire_status
        st = get_pipewire_status()
        if not st.available:
            self.lbl_aec_status.setText("PipeWire / pactl не найден")
            self.btn_toggle_aec.setEnabled(False)
            return
        self.btn_toggle_aec.setEnabled(True)
        if st.aec_active:
            self.lbl_aec_status.setText(f"AEC активен: {st.aec_source_name}")
            self.lbl_aec_status.setStyleSheet(
                f"color: {COLORS.mint}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
            )
            self.btn_toggle_aec.setText("Выключить AEC")
        else:
            self.lbl_aec_status.setText(f"{st.server_name}: AEC выключен")
            self.lbl_aec_status.setStyleSheet(
                f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
            )
            self.btn_toggle_aec.setText("Включить AEC")

    def _on_toggle_aec_clicked(self):
        from ..voice.pipewire import disable_pipewire_aec, enable_pipewire_aec, get_pipewire_status
        st = get_pipewire_status()
        if st.aec_active:
            ok, msg = disable_pipewire_aec()
            if ok:
                self.chk_pipewire_aec.setChecked(False)
        else:
            ok, msg = enable_pipewire_aec()
            if ok:
                self.chk_pipewire_aec.setChecked(True)
        if ok:
            self._refresh_aec_status_ui()
            self._populate_input_devices()
        else:
            self.lbl_aec_status.setText(msg)
            self.lbl_aec_status.setStyleSheet(
                f"color: {COLORS.amber}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
            )

    def _on_calibrate_mic_clicked(self):
        if self._calib_worker and self._calib_worker.isRunning():
            self._stop_calibration_worker()
            self.btn_calibrate_mic.setText("Калибровать микрофон (3 сек)")
            self.lbl_calibration_status.setText("Калибровка остановлена.")
            return
        dev_key = self.combo_input_device.currentData() or ""
        self.btn_calibrate_mic.setText("Стоп...")
        self.bar_mic_level.setValue(0)
        worker = MicCalibrationWorker(input_device=str(dev_key), parent=self)
        worker.level_changed.connect(self.bar_mic_level.setValue)
        worker.phase_changed.connect(self.lbl_calibration_status.setText)
        worker.calibration_ready.connect(self._on_calibration_finished)
        worker.failed.connect(self._on_calibration_failed)
        self._calib_worker = worker
        worker.start()

    def _on_calibration_finished(self, result):
        self.btn_calibrate_mic.setText("Калибровать микрофон (3 сек)")
        val_int = max(15, min(200, int(round(result.recommended_threshold * 1000))))
        self.slider_barge_threshold.setValue(val_int)
        self.lbl_calibration_status.setText(
            f"Готово: фон {result.noise_rms:.3f} · голос {result.speech_rms:.3f} → порог {result.recommended_threshold:.3f}"
        )

    def _on_calibration_failed(self, err: str):
        self.btn_calibrate_mic.setText("Калибровать микрофон (3 сек)")
        self.bar_mic_level.setValue(0)
        self.lbl_calibration_status.setText(err)

    def _stop_calibration_worker(self):
        w = getattr(self, "_calib_worker", None)
        if w is not None:
            w.stop()
            if w.isRunning():
                w.wait(900)
            self._calib_worker = None

    def reject(self):
        self._stop_calibration_worker()
        self._stop_key_check_workers()
        super().reject()

    def closeEvent(self, event):
        self._stop_calibration_worker()
        self._stop_key_check_workers()
        super().closeEvent(event)

    def _apply_and_save(self):
        self._stop_calibration_worker()
        self._stop_key_check_workers()
        p_id = self._get_selected_provider_id()
        key = self.edit_key.text().strip()
        model = self.combo_model.currentText().strip()
        base_url = self.edit_base_url.text().strip()

        fish_key = self.edit_fish_key.text().strip()
        fish_voice = self.edit_fish_voice_id.text().strip()
        eleven_key = self.edit_eleven_key.text().strip()
        eleven_voice = self.edit_eleven_voice_id.text().strip()
        gemini_key = self.edit_gemini_key.text().strip()

        prev_provider = (self._initial_provider or "").strip().lower()
        prev_base_url = (self._initial_base_url or "").strip()
        env_var = PROVIDER_DEFAULTS.get(p_id, {}).get("env_key", f"{p_id.upper()}_API_KEY")

        self.config.llm.provider = p_id
        if key:
            self._cleared_env_keys.discard(env_var)
            self.config.llm.api_key = key
        elif env_var in self._cleared_env_keys or p_id != prev_provider or base_url != prev_base_url:
            # Never carry a previous provider's API key over to a newly selected provider or changed/cleared Base URL,
            # and honor explicit key deletion. When Base URL changes/clears on the same provider with an empty key,
            # also purge the provider's env key so `load_config()` does not resurrect it for the new endpoint.
            self.config.llm.api_key = ""
            if p_id == prev_provider and base_url != prev_base_url:
                self._cleared_env_keys.add(env_var)
        self.config.llm.model = model
        self.config.llm.base_url = base_url
        self.config.companion.user_name = self.edit_user_name.text().strip() or "User"
        self.config.safety.tool_policy = self.combo_policy.currentData() or "ask"

        # Voice / TTS / STT
        self.config.voice.tts_enabled = self.chk_tts_enabled.isChecked()
        self.config.voice.tts_engine = str(self.combo_tts_engine.currentData() or "fish")
        self.config.voice.stt_mode = str(self.combo_stt_mode.currentData() or "groq")
        self.config.voice.wake_word_enabled = self.chk_wake_word_enabled.isChecked()
        self.config.voice.hands_free_barge_in = self.chk_hands_free_barge_in.isChecked()
        self.config.voice.pipewire_aec_enabled = self.chk_pipewire_aec.isChecked()
        self.config.voice.input_device = str(self.combo_input_device.currentData() or "").strip()
        self.config.voice.barge_in_rms_threshold = round(self.slider_barge_threshold.value() / 1000.0, 3)
        if fish_key:
            self._cleared_env_keys.discard("FISH_AUDIO_API_KEY")
            self.config.voice.fish_audio_api_key = fish_key
        elif "FISH_AUDIO_API_KEY" in self._cleared_env_keys:
            self.config.voice.fish_audio_api_key = ""
        self.config.voice.fish_audio_voice_id = fish_voice
        if eleven_key:
            self._cleared_env_keys.discard("ELEVENLABS_API_KEY")
            self.config.voice.elevenlabs_api_key = eleven_key
        elif "ELEVENLABS_API_KEY" in self._cleared_env_keys:
            self.config.voice.elevenlabs_api_key = ""
        self.config.voice.elevenlabs_voice_id = eleven_voice

        # Vision / Screen Analysis & Gemini key sync
        self.config.vision.enabled = self.chk_vision_enabled.isChecked()
        self.config.vision.provider = str(self.combo_vision_provider.currentData() or "gemini")
        self.config.vision.model = self.combo_vision_model.currentText().strip() or "gemini-3.1-flash-lite"
        if gemini_key:
            self._cleared_env_keys.discard("GEMINI_API_KEY")
            self.config.vision.gemini_api_key = gemini_key
            self.config.voice.gemini_api_key = gemini_key
        elif "GEMINI_API_KEY" in self._cleared_env_keys:
            self.config.vision.gemini_api_key = ""
            self.config.voice.gemini_api_key = ""

        # UI
        self.config.ui.avatar_enabled = self.chk_avatar_enabled.isChecked()
        self.config.ui.auto_avatar_poses = self.chk_auto_avatar_poses.isChecked()
        self.config.ui.game_mode_enabled = self.chk_game_mode.isChecked()
        self.config.ui.reduced_motion = self.chk_reduced_motion.isChecked()
        self.config.ui.minimize_to_tray = self.chk_minimize_to_tray.isChecked()

        save_config(self.config)

        for del_var in sorted(self._cleared_env_keys):
            delete_env_value(del_var)
        if key:
            update_env_value(env_var, key)
        if fish_key:
            update_env_value("FISH_AUDIO_API_KEY", fish_key)
        if fish_voice:
            update_env_value("FISH_AUDIO_VOICE_ID", fish_voice)
        if eleven_key:
            update_env_value("ELEVENLABS_API_KEY", eleven_key)
        if eleven_voice:
            update_env_value("ELEVENLABS_VOICE_ID", eleven_voice)
        if gemini_key:
            update_env_value("GEMINI_API_KEY", gemini_key)
        update_env_value("LLM_PROVIDER", p_id)
        update_env_value("LLM_MODEL", model)

        self.accept()


class CapriSessionsDialog(QDialog):
    """Sessions archive, preview, and backup manager styled in LIVING SIGNAL."""

    def __init__(self, parent: "CapriCyberdeckWindow"):
        super().__init__(parent)
        self.cyberdeck = parent
        self.agent = parent.agent
        self.session_manager = parent.agent.session_manager
        self.selected_session_id: Optional[str] = None
        self.selected_session: Optional[SessionRecord] = None
        self.sessions: list[SessionRecord] = []

        self.setWindowTitle("Архив сессий — Capri")
        self.resize(860, 560)
        self.setMinimumSize(640, 420)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        layout.addLayout(
            _make_dialog_header(
                "Архив диалогов и резервные копии",
                "CAPRI // SESSION LOGISTICS",
                self.reject,
            )
        )

        body_layout = QHBoxLayout()
        body_layout.setSpacing(14)

        left_box = QVBoxLayout()
        left_box.setSpacing(8)

        self.edit_search = QLineEdit()
        self.edit_search.setMinimumHeight(34)
        self.edit_search.setPlaceholderText("Поиск по названию или тексту...")
        self.edit_search.addAction(get_icon("search", COLORS.text_muted, 14), QLineEdit.ActionPosition.LeadingPosition)
        self.edit_search.setClearButtonEnabled(True)
        self.edit_search.textChanged.connect(self._on_search_changed)
        left_box.addWidget(self.edit_search)

        self.list_sessions = QListWidget()
        self.list_sessions.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list_sessions.setStyleSheet(f"""
            QListWidget {{
                background: {COLORS.canvas};
                border: 1px solid {COLORS.border_subtle};
                border-radius: 10px;
                padding: 4px;
                color: {COLORS.text_primary};
                font-family: {TYPE.sans};
                font-size: 12.5px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 9px 10px;
                border-radius: 7px;
                margin-bottom: 2px;
                border-left: 2px solid transparent;
            }}
            QListWidget::item:hover {{
                background: {COLORS.surface};
            }}
            QListWidget::item:selected {{
                background: {COLORS.surface_active};
                border-left: 2px solid {COLORS.signal_cyan};
                color: {COLORS.text_primary};
            }}
            {scrollbar_qss(6)}
        """)
        self.list_sessions.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_sessions.itemDoubleClicked.connect(lambda _: self._resume_selected())
        left_box.addWidget(self.list_sessions, stretch=1)

        self.lbl_count = QLabel("Сессий: 0")
        self.lbl_count.setStyleSheet(f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px;")
        left_box.addWidget(self.lbl_count)

        body_layout.addLayout(left_box, stretch=4)

        right_panel = QFrame()
        right_panel.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.canvas};
                border: 1px solid {COLORS.border_subtle};
                border-radius: 10px;
            }}
        """)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(16, 14, 16, 14)
        right_layout.setSpacing(10)

        self.lbl_session_title = QLabel("Выберите сессию из списка слева")
        self.lbl_session_title.setStyleSheet(
            f"color: {COLORS.text_primary}; font-family: {TYPE.display}; font-size: 14px; font-weight: 600; border: none;"
        )
        self.lbl_session_title.setWordWrap(True)
        right_layout.addWidget(self.lbl_session_title)

        self.lbl_session_meta = QLabel("UUID: -\nСоздана: -\nОбновлена: -")
        self.lbl_session_meta.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10.5px; line-height: 1.4; border: none;"
        )
        right_layout.addWidget(self.lbl_session_meta)

        lbl_preview_title = QLabel("ПРЕДПРОСМОТР ДИАЛОГА")
        lbl_preview_title.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10px; font-weight: 600; letter-spacing: 0.6px; border: none;"
        )
        right_layout.addWidget(lbl_preview_title)

        self.txt_preview = QTextEdit()
        self.txt_preview.setReadOnly(True)
        self.txt_preview.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS.surface_alt};
                color: {COLORS.text_primary};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 8px;
                padding: 10px;
                font-family: {TYPE.sans};
                font-size: 12.5px;
            }}
            {scrollbar_qss(6)}
        """)
        right_layout.addWidget(self.txt_preview, stretch=1)

        actions_box = QHBoxLayout()
        actions_box.setSpacing(8)

        self.btn_resume = QPushButton("Восстановить")
        self.btn_resume.setIcon(get_icon("sessions", COLORS.text_on_accent, 14))
        self.btn_resume.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_resume.setMinimumHeight(36)
        self.btn_resume.setEnabled(False)
        self.btn_resume.setStyleSheet(button_primary_qss("7px 14px"))
        self.btn_resume.clicked.connect(self._resume_selected)
        actions_box.addWidget(self.btn_resume)

        self.btn_delete = QPushButton("Удалить")
        self.btn_delete.setIcon(get_icon("trash", COLORS.coral, 14))
        self.btn_delete.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_delete.setMinimumHeight(36)
        self.btn_delete.setEnabled(False)
        self.btn_delete.setStyleSheet(button_danger_qss(filled=False, padding="7px 14px"))
        self.btn_delete.clicked.connect(self._delete_selected)
        actions_box.addWidget(self.btn_delete)
        actions_box.addStretch()

        right_layout.addLayout(actions_box)
        body_layout.addWidget(right_panel, stretch=6)
        layout.addLayout(body_layout, stretch=1)

        footer_layout = QHBoxLayout()
        footer_layout.setSpacing(10)

        btn_new = QPushButton("Новая сессия")
        btn_new.setIcon(get_icon("plus", COLORS.signal_cyan, 14))
        btn_new.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_new.setMinimumHeight(36)
        btn_new.setStyleSheet(button_secondary_qss("8px 14px"))
        btn_new.clicked.connect(self._create_new_session)
        footer_layout.addWidget(btn_new)

        btn_backup = QPushButton("Бэкап базы")
        btn_backup.setIcon(get_icon("download", COLORS.text_secondary, 14))
        btn_backup.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_backup.setMinimumHeight(36)
        btn_backup.setStyleSheet(button_secondary_qss("8px 14px"))
        btn_backup.clicked.connect(self._create_backup)
        footer_layout.addWidget(btn_backup)

        footer_layout.addStretch()

        btn_close = QPushButton("Закрыть")
        btn_close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_close.setMinimumHeight(36)
        btn_close.setStyleSheet(button_secondary_qss("8px 18px"))
        btn_close.clicked.connect(self.reject)
        footer_layout.addWidget(btn_close)

        layout.addLayout(footer_layout)
        self._refresh_session_list()

    def _refresh_session_list(self):
        raw_query = self.edit_search.text().strip()
        filter_text = raw_query.casefold()
        self.list_sessions.clear()
        if raw_query and hasattr(self.session_manager, "search_sessions"):
            self.sessions = self.session_manager.search_sessions(raw_query, limit=100)
        else:
            self.sessions = self.session_manager.list_sessions(limit=100)

        active_id = self.agent.current_session_id
        count = 0
        target_row = 0
        for s in self.sessions:
            if filter_text and filter_text not in (s.title or "").lower() and filter_text not in (s.preview or "").lower():
                continue

            count += 1
            is_active = s.id == active_id
            status_tag = " · активна" if is_active else ""
            date_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(s.updated_at))
            rounds_str = _format_rounds_ru(s.turn_count) if s.turn_count else "пустая"

            item = QListWidgetItem()
            item.setText(f"{s.title}{status_tag}\n{date_str}  ·  {rounds_str}")
            item.setData(Qt.ItemDataRole.UserRole, s.id)
            if is_active:
                item.setForeground(QColor(COLORS.signal_cyan))
                target_row = self.list_sessions.count()
            self.list_sessions.addItem(item)

        self.lbl_count.setText(f"Сессий: {count}")

        if self.list_sessions.count() > 0:
            self.list_sessions.setCurrentRow(target_row)
        else:
            self._clear_preview()

    def _on_search_changed(self, text: str):
        self._refresh_session_list()

    def _on_selection_changed(self):
        items = self.list_sessions.selectedItems()
        if not items:
            self._clear_preview()
            return

        session_id = items[0].data(Qt.ItemDataRole.UserRole)
        self.selected_session_id = session_id
        sess = self.session_manager.get_session(session_id)
        if not sess:
            self._clear_preview()
            return

        self.selected_session = sess
        is_active = sess.id == self.agent.current_session_id
        active_badge = f" <span style='color: {COLORS.mint}; font-size: 11px;'>● активна</span>" if is_active else ""
        escaped_title = html.escape(sess.title)
        self.lbl_session_title.setText(f"{escaped_title}{active_badge}")

        created_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sess.created_at))
        updated_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sess.updated_at))
        self.lbl_session_meta.setText(
            f"UUID: {sess.id}\nСоздана: {created_str}  ·  Обновлена: {updated_str}\nРаундов: {sess.turn_count}"
        )

        u_name = (
            getattr(getattr(getattr(self.parent(), "config", None), "companion", None), "user_name", "") or ""
        ).strip() or "User"
        turns = self.session_manager.get_recent_turns(session_id, limit=30)
        if turns:
            lines = []
            for t in turns:
                is_pro = getattr(t, "status", "") == "proactive" or (t.user_text or "").startswith("[INITIATIVE //")
                if t.user_text and not is_pro:
                    lines.append(f"<b style='color: {COLORS.coral};'>{html.escape(u_name)}:</b> {html.escape(t.user_text)}")
                if t.assistant_text:
                    clean_resp = html.escape(t.assistant_text).replace("\n", "<br>")
                    lines.append(f"<b style='color: {COLORS.signal_cyan};'>Капри:</b> {clean_resp}")
                lines.append(f"<hr style='border: 0; border-top: 1px solid {COLORS.border_hairline}; margin: 8px 0;'>")
            self.txt_preview.setHtml("".join(lines))
        else:
            self.txt_preview.setHtml(f"<i style='color: {COLORS.text_muted};'>В этой сессии пока нет сообщений.</i>")

        self.btn_resume.setEnabled(True)
        self.btn_delete.setEnabled(True)

    def _clear_preview(self):
        self.selected_session_id = None
        self.selected_session = None
        self.lbl_session_title.setText("Выберите сессию из списка слева")
        self.lbl_session_meta.setText("UUID: -\nСоздана: -\nОбновлена: -")
        self.txt_preview.clear()
        self.btn_resume.setEnabled(False)
        self.btn_delete.setEnabled(False)

    def _resume_selected(self):
        if not self.selected_session_id:
            return
        play_ui_sound("ui_click")
        self.cyberdeck._load_session_into_ui(self.selected_session_id)
        self.accept()

    def _delete_selected(self):
        if not self.selected_session_id:
            return
        play_ui_sound("ui_glitch")
        target_id = self.selected_session_id
        is_active = target_id == self.agent.current_session_id

        self.session_manager.soft_delete_session(target_id)
        if is_active:
            self.cyberdeck._start_new_session(auto=True)

        self._refresh_session_list()

    def _create_new_session(self):
        self.cyberdeck._start_new_session()
        self.accept()

    def _create_backup(self):
        play_ui_sound("ui_click")
        backup_dir = get_user_config_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"sessions_backup_{ts_str}.db"
        if self.session_manager.backup_to(target):
            self.cyberdeck._append_message("Система", f"Резервная копия создана:\n{target}")
            self.txt_preview.setHtml(f"<b style='color: {COLORS.mint};'>Резервная копия создана:</b><br>{target}")
        else:
            self.txt_preview.setHtml(f"<b style='color: {COLORS.coral};'>Ошибка создания резервной копии базы.</b>")


class CapriMemoryEditDialog(QDialog):
    """Add/edit dialog for Memory Workbench entries and pending candidates."""

    def __init__(
        self,
        parent=None,
        entry: Optional[MemoryEntry] = None,
        default_category: str = "general",
        candidate: Optional[MemoryCandidate] = None,
    ):
        super().__init__(parent)
        self.entry = entry
        self.candidate = candidate
        is_edit = entry is not None and candidate is None
        is_candidate = candidate is not None

        if is_candidate:
            self.setWindowTitle("Подтверждение кандидата памяти — Capri")
        else:
            self.setWindowTitle("Редактор памяти — Capri" if is_edit else "Новый факт памяти — Capri")
        self.resize(540, 440)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        user_name = getattr(getattr(getattr(parent, "cyberdeck", None), "config", None), "companion", None)
        if user_name is None:
            user_name = getattr(getattr(parent, "config", None), "companion", None)
        u_label = getattr(user_name, "user_name", "User") if user_name else "User"
        if is_candidate:
            hdr_title = "Проверка кандидата памяти"
        else:
            hdr_title = "Редактирование факта" if is_edit else f"Новый факт о {u_label}"
        layout.addLayout(
            _make_dialog_header(
                hdr_title,
                "MEMORY WORKBENCH",
                self.reject,
            )
        )

        init_key = candidate.key if is_candidate else (entry.key if entry else "")
        init_cat = candidate.category if is_candidate else (entry.category if entry else default_category)
        init_imp = candidate.importance if is_candidate else (entry.importance if entry else 3)
        init_ret = candidate.retention if is_candidate else (getattr(entry, "retention", "long_term") if entry else "long_term")
        init_fact = candidate.fact if is_candidate else (entry.fact if entry else "")

        layout.addWidget(QLabel("Ключ (идентификатор факта)"))
        self.edit_key = QLineEdit(init_key)
        self.edit_key.setMinimumHeight(34)
        self.edit_key.setPlaceholderText("например: favorite_editor, stack, habit...")
        if is_edit:
            self.edit_key.setEnabled(False)
        layout.addWidget(self.edit_key)

        row = QHBoxLayout()
        row.setSpacing(10)

        cat_box = QVBoxLayout()
        cat_box.addWidget(QLabel("Категория"))
        self.combo_cat = QComboBox()
        self.combo_cat.setMinimumHeight(34)
        for cat in CATEGORIES:
            lbl = CATEGORY_LABELS.get(cat, cat)
            self.combo_cat.addItem(f"{lbl} ({cat})", cat)
        idx = self.combo_cat.findData(init_cat)
        if idx >= 0:
            self.combo_cat.setCurrentIndex(idx)
        cat_box.addWidget(self.combo_cat)
        row.addLayout(cat_box)

        imp_box = QVBoxLayout()
        imp_box.addWidget(QLabel("Приоритет"))
        self.combo_imp = QComboBox()
        self.combo_imp.setMinimumHeight(34)
        self.combo_imp.addItem("★☆☆☆☆ (1 — Низкая)", 1)
        self.combo_imp.addItem("★★☆☆☆ (2 — Ниже среднего)", 2)
        self.combo_imp.addItem("★★★☆☆ (3 — Стандартная)", 3)
        self.combo_imp.addItem("★★★★☆ (4 — Высокая)", 4)
        self.combo_imp.addItem("★★★★★ (5 — Критическая)", 5)
        idx_imp = self.combo_imp.findData(init_imp)
        if idx_imp >= 0:
            self.combo_imp.setCurrentIndex(idx_imp)
        imp_box.addWidget(self.combo_imp)
        row.addLayout(imp_box)

        ret_box = QVBoxLayout()
        ret_box.addWidget(QLabel("Срок хранения"))
        self.combo_retention = QComboBox()
        self.combo_retention.setMinimumHeight(34)
        self.combo_retention.addItem("Постоянно", "long_term")
        self.combo_retention.addItem("Временно (72ч)", "ephemeral")
        idx_ret = self.combo_retention.findData(init_ret if init_ret in RETENTION_MODES else "long_term")
        if idx_ret >= 0:
            self.combo_retention.setCurrentIndex(idx_ret)
        ret_box.addWidget(self.combo_retention)
        row.addLayout(ret_box)

        layout.addLayout(row)

        layout.addWidget(QLabel("Текст факта"))
        self.edit_fact = QTextEdit()
        self.edit_fact.setPlaceholderText("Опиши факт или знание...")
        self.edit_fact.setPlainText(init_fact)
        self.edit_fact.setMaximumHeight(96)
        layout.addWidget(self.edit_fact)

        if is_candidate and candidate.source_quote:
            lbl_quote = QLabel(f"Источник из диалога: «{candidate.source_quote}»")
            lbl_quote.setWordWrap(True)
            lbl_quote.setStyleSheet(f"color: {COLORS.text_muted}; font-size: 11px;")
            layout.addWidget(lbl_quote)

        layout.addWidget(QLabel("Теги (через запятую)"))
        self.edit_tags = QLineEdit(", ".join(entry.tags) if entry and entry.tags else "")
        self.edit_tags.setMinimumHeight(34)
        self.edit_tags.setPlaceholderText("arch, linux, dev, habit...")
        layout.addWidget(self.edit_tags)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()

        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setMinimumHeight(36)
        self.btn_cancel.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_cancel.setStyleSheet(button_secondary_qss("7px 16px"))
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)

        self.btn_save = QPushButton("Запомнить" if is_candidate else "Сохранить")
        self.btn_save.setIcon(get_icon("check", COLORS.text_on_accent, 14))
        self.btn_save.setMinimumHeight(36)
        self.btn_save.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_save.setStyleSheet(button_primary_qss("7px 18px"))
        self.btn_save.clicked.connect(self._on_save)
        btn_layout.addWidget(self.btn_save)

        layout.addLayout(btn_layout)
        apply_dark_palette(self)

    def _on_save(self):
        k = self.edit_key.text().strip()
        f = self.edit_fact.toPlainText().strip()
        if not k or not f:
            QMessageBox.warning(self, "Ошибка", "Ключ и текст факта не могут быть пустыми.")
            return
        tags = [t.strip().lower() for t in self.edit_tags.text().split(",") if t.strip()]
        if memory_payload_contains_secret(k, f, tags=tags):
            QMessageBox.warning(
                self,
                "Секреты запрещены",
                "Пароли, API-ключи, токены, PIN-коды и другие секреты не сохраняются в память.",
            )
            return
        self.accept()

    def get_data(self) -> dict:
        tags_raw = self.edit_tags.text().split(",")
        tags = [t.strip().lower() for t in tags_raw if t.strip()]
        return {
            "key": self.edit_key.text().strip().lower(),
            "fact": self.edit_fact.toPlainText().strip(),
            "category": self.combo_cat.currentData(),
            "importance": self.combo_imp.currentData(),
            "retention": self.combo_retention.currentData() or "long_term",
            "tags": tags,
        }


class CapriMemoryWorkbenchDialog(QDialog):
    """Long-term Memory Workbench styled in LIVING SIGNAL with dark viewport/headers."""

    def __init__(self, parent: "CapriCyberdeckWindow"):
        super().__init__(parent)
        self.cyberdeck = parent
        self.store = getattr(parent.agent, "memory_store", None) or MemoryStore()

        self.setWindowTitle("Memory Workbench — Capri")
        self.resize(920, 580)
        self.setMinimumSize(680, 440)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        layout.addLayout(
            _make_dialog_header(
                "Memory Workbench — Долговременная память",
                "CAPRI // COGNITIVE REPOSITORY",
                self.accept,
            )
        )

        bar = QHBoxLayout()
        bar.setSpacing(10)

        self.edit_search = QLineEdit()
        self.edit_search.setMinimumHeight(36)
        self.edit_search.setPlaceholderText("Поиск по ключу, факту или тегу...")
        self.edit_search.addAction(get_icon("search", COLORS.text_muted, 14), QLineEdit.ActionPosition.LeadingPosition)
        self.edit_search.setClearButtonEnabled(True)
        self.edit_search.textChanged.connect(self._refresh_table)
        bar.addWidget(self.edit_search, stretch=3)

        self.combo_cat_filter = QComboBox()
        self.combo_cat_filter.setMinimumHeight(36)
        self.combo_cat_filter.addItem("Все категории", "")
        for cat in CATEGORIES:
            lbl = CATEGORY_LABELS.get(cat, cat)
            self.combo_cat_filter.addItem(f"{lbl} ({cat})", cat)
        self.combo_cat_filter.currentIndexChanged.connect(self._refresh_table)
        bar.addWidget(self.combo_cat_filter, stretch=2)

        self.btn_add = QPushButton("Добавить факт")
        self.btn_add.setIcon(get_icon("plus", COLORS.text_on_accent, 14))
        self.btn_add.setMinimumHeight(36)
        self.btn_add.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_add.setStyleSheet(button_primary_qss("7px 14px"))
        self.btn_add.clicked.connect(self._on_add)
        bar.addWidget(self.btn_add)

        layout.addLayout(bar)

        # Pending memory candidates review strip (hidden when queue is empty)
        self.pending_frame = QFrame(self)
        self.pending_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface_raised};
                border: 1px solid {COLORS.crimson};
                border-radius: 10px;
            }}
        """)
        pf_layout = QHBoxLayout(self.pending_frame)
        pf_layout.setContentsMargins(12, 8, 12, 8)
        pf_layout.setSpacing(10)

        pf_text_col = QVBoxLayout()
        pf_text_col.setSpacing(2)
        self.lbl_wb_pending_title = QLabel("")
        self.lbl_wb_pending_title.setStyleSheet(
            f"color: {COLORS.crimson}; font-family: {TYPE.mono}; font-size: 10px; font-weight: 700; border: none;"
        )
        self.lbl_wb_pending_body = QLabel("")
        self.lbl_wb_pending_body.setWordWrap(True)
        self.lbl_wb_pending_body.setStyleSheet(f"color: {COLORS.text_primary}; font-size: 12px; border: none;")
        pf_text_col.addWidget(self.lbl_wb_pending_title)
        pf_text_col.addWidget(self.lbl_wb_pending_body)
        pf_layout.addLayout(pf_text_col, stretch=1)

        self.btn_wb_approve = QPushButton("Запомнить")
        self.btn_wb_approve.setMinimumHeight(30)
        self.btn_wb_approve.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_wb_approve.setStyleSheet(button_primary_qss("4px 12px"))
        self.btn_wb_approve.clicked.connect(self._on_approve_top_pending)
        pf_layout.addWidget(self.btn_wb_approve)

        self.btn_wb_edit_pending = QPushButton("Править")
        self.btn_wb_edit_pending.setMinimumHeight(30)
        self.btn_wb_edit_pending.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_wb_edit_pending.setStyleSheet(button_secondary_qss("4px 10px"))
        self.btn_wb_edit_pending.clicked.connect(self._on_edit_top_pending)
        pf_layout.addWidget(self.btn_wb_edit_pending)

        self.btn_wb_reject = QPushButton("Отклонить")
        self.btn_wb_reject.setMinimumHeight(30)
        self.btn_wb_reject.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_wb_reject.setStyleSheet(button_danger_qss(filled=False, padding="4px 10px"))
        self.btn_wb_reject.clicked.connect(self._on_reject_top_pending)
        pf_layout.addWidget(self.btn_wb_reject)

        self.pending_frame.setVisible(False)
        layout.addWidget(self.pending_frame)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        user_name = getattr(self.cyberdeck.config.companion, "user_name", "User") or "User"
        self.table.setHorizontalHeaderLabels(["Ключ", f"Факт о {user_name}", "Категория", "Важность", "Обновлено"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(self._on_edit)
        self.table.viewport().setStyleSheet(f"background: {COLORS.canvas}; background-color: {COLORS.canvas};")
        self.table.setStyleSheet(table_qss() + scrollbar_qss(6))
        layout.addWidget(self.table, stretch=1)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px;")
        layout.addWidget(self.lbl_status)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)

        self.btn_edit = QPushButton("Редактировать")
        self.btn_edit.setIcon(get_icon("edit", COLORS.text_secondary, 14))
        self.btn_edit.setMinimumHeight(36)
        self.btn_edit.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_edit.setStyleSheet(button_secondary_qss("7px 12px"))
        self.btn_edit.clicked.connect(self._on_edit)
        bottom.addWidget(self.btn_edit)

        self.btn_delete = QPushButton("Удалить")
        self.btn_delete.setIcon(get_icon("trash", COLORS.coral, 14))
        self.btn_delete.setMinimumHeight(36)
        self.btn_delete.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_delete.setStyleSheet(button_danger_qss(filled=False, padding="7px 12px"))
        self.btn_delete.clicked.connect(self._on_delete)
        bottom.addWidget(self.btn_delete)

        self.btn_clear_cat = QPushButton("Очистить категорию")
        self.btn_clear_cat.setIcon(get_icon("clear", COLORS.coral, 14))
        self.btn_clear_cat.setMinimumHeight(36)
        self.btn_clear_cat.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_clear_cat.setStyleSheet(button_danger_qss(filled=False, padding="7px 12px"))
        self.btn_clear_cat.clicked.connect(self._on_clear_category)
        bottom.addWidget(self.btn_clear_cat)

        bottom.addStretch()

        self.btn_export = QPushButton("Экспорт")
        self.btn_export.setIcon(get_icon("download", COLORS.text_secondary, 14))
        self.btn_export.setMinimumHeight(36)
        self.btn_export.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_export.setStyleSheet(button_secondary_qss("7px 12px"))
        self.btn_export.clicked.connect(self._on_export)
        bottom.addWidget(self.btn_export)

        self.btn_import = QPushButton("Импорт")
        self.btn_import.setIcon(get_icon("upload", COLORS.text_secondary, 14))
        self.btn_import.setMinimumHeight(36)
        self.btn_import.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_import.setStyleSheet(button_secondary_qss("7px 12px"))
        self.btn_import.clicked.connect(self._on_import)
        bottom.addWidget(self.btn_import)

        self.btn_close = QPushButton("Закрыть")
        self.btn_close.setMinimumHeight(36)
        self.btn_close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_close.setStyleSheet(button_secondary_qss("7px 16px"))
        self.btn_close.clicked.connect(self.accept)
        bottom.addWidget(self.btn_close)

        layout.addLayout(bottom)
        apply_dark_palette(self)
        self._refresh_table()

    def _refresh_table(self):
        q = self.edit_search.text().strip()
        cat = self.combo_cat_filter.currentData() or None
        results = self.store.search(query=q, category=cat)

        self.table.setRowCount(len(results))
        for row, entry in enumerate(results):
            item_k = QTableWidgetItem(entry.key)
            item_k.setForeground(QColor(COLORS.signal_cyan))

            item_f = QTableWidgetItem(entry.fact)
            cat_label = CATEGORY_LABELS.get(entry.category, entry.category)
            item_c = QTableWidgetItem(cat_label)
            item_c.setForeground(QColor(COLORS.text_secondary))

            item_imp = QTableWidgetItem("★" * entry.importance)
            item_imp.setForeground(QColor(COLORS.amber))

            upd_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.updated_at))
            if getattr(entry, "retention", "long_term") == "ephemeral":
                upd_str += " · врем."
            item_u = QTableWidgetItem(upd_str)
            item_u.setForeground(QColor(COLORS.text_muted))

            self.table.setItem(row, 0, item_k)
            self.table.setItem(row, 1, item_f)
            self.table.setItem(row, 2, item_c)
            self.table.setItem(row, 3, item_imp)
            self.table.setItem(row, 4, item_u)

        total = len(self.store.entries)
        visible = len(results)
        pending = self.store.list_pending()
        pend_note = f"  ·  На проверке: {len(pending)}" if pending else ""
        self.lbl_status.setText(f"Отображается записей: {visible} из {total}  ·  Категория: {cat or 'все'}{pend_note}")
        self._refresh_pending_banner()

    def _refresh_pending_banner(self):
        pending = self.store.list_pending()
        if not pending:
            self.pending_frame.setVisible(False)
            return
        top = pending[0]
        ret_lbl = "ВРЕМЕННО 72Ч" if top.retention == "ephemeral" else "ПОСТОЯННО"
        self.lbl_wb_pending_title.setText(
            f"КАНДИДАТ ПАМЯТИ (1/{len(pending)}) · {ret_lbl} · #{top.id}"
        )
        self.lbl_wb_pending_body.setText(f"[{top.key}]: {top.fact}")
        if top.source_quote:
            self.lbl_wb_pending_body.setToolTip(f"Источник: «{top.source_quote}»")
        else:
            self.lbl_wb_pending_body.setToolTip("")
        self.pending_frame.setVisible(True)

    def _notify_cyberdeck_memory_changed(self):
        if hasattr(self.cyberdeck, "_refresh_memory_candidate_bar"):
            self.cyberdeck._refresh_memory_candidate_bar()
        if hasattr(self.cyberdeck, "refresh_stats"):
            self.cyberdeck.refresh_stats()

    def _on_approve_top_pending(self):
        pending = self.store.list_pending()
        if not pending:
            return
        self.store.approve_candidate(pending[0].id)
        self._refresh_table()
        self._notify_cyberdeck_memory_changed()

    def _on_edit_top_pending(self):
        pending = self.store.list_pending()
        if not pending:
            return
        cand = pending[0]
        dlg = CapriMemoryEditDialog(self, candidate=cand)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            self.store.approve_candidate(
                cand.id,
                key=data["key"],
                fact=data["fact"],
                category=data["category"],
                importance=data["importance"],
                retention=data.get("retention", cand.retention),
                tags=data["tags"],
            )
            self._refresh_table()
            self._notify_cyberdeck_memory_changed()

    def _on_reject_top_pending(self):
        pending = self.store.list_pending()
        if not pending:
            return
        self.store.reject_candidate(pending[0].id)
        self._refresh_table()
        self._notify_cyberdeck_memory_changed()

    def _get_selected_key(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.text() if item else None

    def _on_add(self):
        cat_filter = self.combo_cat_filter.currentData() or "general"
        dlg = CapriMemoryEditDialog(self, default_category=cat_filter)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            self.store.add_or_update(
                key=data["key"],
                fact=data["fact"],
                category=data["category"],
                importance=data["importance"],
                retention=data.get("retention", "long_term"),
                tags=data["tags"],
                source="workbench",
            )
            self._refresh_table()
            self._notify_cyberdeck_memory_changed()

    def _on_edit(self):
        k = self._get_selected_key()
        if not k or k not in self.store.entries:
            QMessageBox.information(self, "Внимание", "Выберите запись для редактирования.")
            return
        entry = self.store.entries[k]
        dlg = CapriMemoryEditDialog(self, entry=entry)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            self.store.add_or_update(
                key=k,
                fact=data["fact"],
                category=data["category"],
                importance=data["importance"],
                retention=data.get("retention", getattr(entry, "retention", "long_term")),
                tags=data["tags"],
                source="workbench",
            )
            self._refresh_table()
            self._notify_cyberdeck_memory_changed()

    def _on_delete(self):
        k = self._get_selected_key()
        if not k:
            QMessageBox.information(self, "Внимание", "Выберите запись для удаления.")
            return
        reply = QMessageBox.question(
            self,
            "Подтверждение",
            f"Удалить факт с ключом [{k}] из долговременной памяти?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.store.delete(k)
            self._refresh_table()

    def _on_clear_category(self):
        cat = self.combo_cat_filter.currentData()
        target_name = CATEGORY_LABELS.get(cat, cat) if cat else "ВСЮ ПАМЯТЬ"
        reply = QMessageBox.question(
            self,
            "Очистка памяти",
            f"Вы уверены, что хотите полностью очистить [{target_name}]?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.store.clear(category=cat or None)
            self._refresh_table()

    def _on_export(self):
        backup_dir = get_user_config_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        default_path = str(backup_dir / f"memory_export_{int(time.time())}.json")
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт долговременной памяти", default_path, "JSON Files (*.json)"
        )
        if file_path:
            try:
                data = self.store.export_dict()
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                QMessageBox.information(self, "Успех", f"Память успешно экспортирована в:\n{file_path}")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось экспортировать файл:\n{e}")

    def _on_import(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Импорт долговременной памяти", str(Path.home()), "JSON Files (*.json)"
        )
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                cnt = self.store.import_dict(raw_data)
                self._refresh_table()
                QMessageBox.information(self, "Успех", f"Успешно импортировано записей: {cnt}")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось импортировать файл:\n{e}")


class CapriCyberdeckWindow(QWidget):
    """
    Main Cyberdeck desktop companion window — LIVING SIGNAL v2 redesign.
    """

    wake_text_signal = Signal(str)
    wake_triggered_signal = Signal()
    barge_in_triggered_signal = Signal()
    barge_in_text_signal = Signal(str)

    def __init__(self, config: AppConfig | None = None):
        super().__init__()
        self.config = config or load_config()
        self.agent = CapriAgent(self.config)
        self.emotions = self.agent.emotions
        self.session_manager = self.agent.session_manager
        self.motion = MotionController(
            parent=self,
            reduced_motion=bool(getattr(self.config.ui, "reduced_motion", False)),
        )
        self.current_user_card: Optional[ChatMessageCard] = None
        self.current_response_card: Optional[ChatMessageCard] = None
        self._response_has_content: bool = False
        self.drag_position = QPoint()

        self.thread: Optional[QThread] = None
        self.worker: Optional[AgentWorker] = None
        self.mcp_worker: Optional[MCPConnectWorker] = None
        self.wake_listener = None
        self.barge_in_monitor = None
        self.avatar_proc: Optional[subprocess.Popen] = None
        self._force_quit: bool = False
        self.tray_icon: Optional[QSystemTrayIcon] = None
        self.ipc_server: Optional[QLocalServer] = None
        self._ipc_socket_path: Optional[str] = None

        self.current_tools_log: list[str] = []
        self.current_thought_text = ""
        self._composer_focused = False
        self._composer_recording = False
        self._stream_pending_text: Optional[str] = None
        self._stream_flush_timer = QTimer(self)
        self._stream_flush_timer.setSingleShot(True)
        self._stream_flush_timer.timeout.connect(self._flush_stream_chunk)

        self.wake_text_signal.connect(self._handle_wake_text)
        self.wake_triggered_signal.connect(self._handle_wake_triggered)
        self.barge_in_triggered_signal.connect(self._on_barge_in_triggered)
        self.barge_in_text_signal.connect(self._handle_barge_in_text)

        self._init_window()
        self._init_ui()
        self._init_timers()

        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            self._init_tray_icon()
            self.start_ipc_server()

        if getattr(self.config.ui, "avatar_enabled", True):
            self._ensure_avatar_running()

        if getattr(self.config.voice, "wake_word_enabled", False):
            self._start_wake_listener()

        # Restore active session turns or show intentional WelcomeCanvas when empty
        active_sess_id = self.agent.current_session_id
        turns = self.agent.session_manager.get_turns(active_sess_id, limit=50)
        sess = self.agent.session_manager.get_session(active_sess_id)
        sess_title = sess.title if sess else "Новый диалог"
        self.lbl_active_session_title.setText(sess_title)
        self.lbl_turn_counter.setText(_format_rounds_ru(len(turns)))

        if turns:
            self._populate_turns_ui(turns)
        else:
            self._refresh_welcome_canvas(context="startup")
        self._update_welcome_visibility()
        self._refresh_sidebar_sessions()

        # Offer first-run setup wizard on real desktop launch when no API key is configured
        if (
            not (self.config.llm.api_key or "").strip()
            and os.environ.get("QT_QPA_PLATFORM") != "offscreen"
        ):
            QTimer.singleShot(250, self._show_first_run_wizard)

    def _init_window(self):
        self.setWindowTitle(f"CAPRI // LIVING SIGNAL v{__version__}")
        self.setObjectName("capri-cyberdeck")
        QApplication.setApplicationName("capri-cyberdeck")
        QApplication.setDesktopFileName("capri-cyberdeck")

        self.setMinimumSize(480, 360)
        self.resize(1120, 720)

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAcceptDrops(True)

    def _init_ui(self):
        # Full-bleed split inside 1px painted outer hairline — zero double boxes
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(1, 1, 1, 1)
        main_layout.setSpacing(0)

        self.central_layout = QHBoxLayout()
        self.central_layout.setContentsMargins(0, 0, 0, 0)
        self.central_layout.setSpacing(0)

        # ── LEFT NAVIGATION RAIL / DRAWER ─────────────────────────────
        self.left_panel = QFrame(self)
        self.left_panel.setObjectName("leftPanel")
        self.left_panel.setMinimumWidth(0)
        self.left_panel.setMinimumHeight(0)
        self.left_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        self.left_panel.setMaximumWidth(METRICS.sidebar_default)
        self.left_panel.setStyleSheet(f"""
            QFrame#leftPanel {{
                background: {COLORS.nav};
                border: none;
                border-right: 1px solid {COLORS.border_hairline};
                border-top-left-radius: 10px;
                border-bottom-left-radius: 10px;
            }}
        """)
        lp_layout = QVBoxLayout(self.left_panel)
        lp_layout.setContentsMargins(12, 12, 12, 12)
        lp_layout.setSpacing(8)
        self.lp_layout = lp_layout

        # Brand micro-header (hidden at compact heights < 520px to maximize session list room)
        self.sidebar_brand_row = QWidget(self.left_panel)
        brand_layout = QHBoxLayout(self.sidebar_brand_row)
        brand_layout.setContentsMargins(2, 0, 2, 4)
        brand_layout.setSpacing(8)

        self.brand_mark = CapriSignalMark(
            size=26,
            avatar_enabled=bool(getattr(self.config.ui, "avatar_enabled", True)),
            parent=self.sidebar_brand_row,
        )
        brand_layout.addWidget(self.brand_mark)

        lbl_brand = QLabel("CAPRI")
        lbl_brand.setStyleSheet(
            f"color: {COLORS.text_primary}; font-family: {TYPE.display}; font-size: 14px; font-weight: 700; letter-spacing: 1.6px;"
        )
        brand_layout.addWidget(lbl_brand)
        brand_layout.addStretch()

        self.lbl_logo = QLabel(f"v{__version__}")
        self.lbl_logo.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 600;"
        )
        brand_layout.addWidget(self.lbl_logo)

        self.btn_rail_settings = QPushButton()
        self.btn_rail_settings.setIcon(get_icon("settings", COLORS.text_muted, 14))
        self.btn_rail_settings.setIconSize(QSize(14, 14))
        self.btn_rail_settings.setFixedSize(26, 26)
        self.btn_rail_settings.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_rail_settings.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_rail_settings.setToolTip("Настройки нейроядра и интерфейса")
        self.btn_rail_settings.setStyleSheet(
            button_ghost_qss(
                text_color=COLORS.text_muted,
                hover_text=COLORS.text_primary,
                hover_bg=COLORS.surface_raised,
                padding="0px",
                radius=7,
            )
        )
        self.btn_rail_settings.clicked.connect(self._show_provider_settings)
        brand_layout.addWidget(self.btn_rail_settings)
        lp_layout.addWidget(self.sidebar_brand_row)

        # Primary CTA: New Chat (Signature Crimson Red accent + inline right-aligned Ctrl N keycap)
        self.btn_new_session = QPushButton("Новый чат")
        self.btn_new_session.setIcon(get_icon("plus", "#FFFFFF", 15, stroke_width=2.1))
        self.btn_new_session.setIconSize(QSize(15, 15))
        self.btn_new_session.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_new_session.setToolTip("Начать новую чистую сессию диалога (/new, Ctrl+N)")
        self.btn_new_session.setFixedHeight(38)
        self.btn_new_session.setStyleSheet(crimson_cta_qss("0px 12px", radius=11))
        cta_badge_lay = QHBoxLayout(self.btn_new_session)
        cta_badge_lay.setContentsMargins(0, 0, 10, 0)
        cta_badge_lay.addStretch()
        lbl_ctrl_n = QLabel("Ctrl N", self.btn_new_session)
        lbl_ctrl_n.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lbl_ctrl_n.setStyleSheet(
            f"color: rgba(255, 255, 255, 0.72); font-family: {TYPE.mono}; font-size: 10px; font-weight: 600; background: transparent; border: none;"
        )
        cta_badge_lay.addWidget(lbl_ctrl_n)
        self.btn_new_session.clicked.connect(self._start_new_session)
        lp_layout.addWidget(self.btn_new_session)

        # Full-history SQLite search input with right-aligned Ctrl K keycap
        self.search_session_input = QLineEdit()
        self.search_session_input.setPlaceholderText("Поиск диалогов...")
        self.search_session_input.addAction(
            get_icon("search", COLORS.text_muted, 14),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self.search_session_input.setClearButtonEnabled(True)
        self.search_session_input.setFixedHeight(34)
        self.search_session_input.setStyleSheet(input_qss("4px 46px 4px 8px", radius=10))
        search_badge_lay = QHBoxLayout(self.search_session_input)
        search_badge_lay.setContentsMargins(0, 0, 10, 0)
        search_badge_lay.addStretch()
        self._lbl_ctrl_k = QLabel("Ctrl K", self.search_session_input)
        self._lbl_ctrl_k.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._lbl_ctrl_k.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10px; font-weight: 600; background: transparent; border: none;"
        )
        search_badge_lay.addWidget(self._lbl_ctrl_k)
        self.search_session_input.textChanged.connect(
            lambda txt: self._lbl_ctrl_k.setVisible(not bool(txt))
        )
        self.search_session_input.textChanged.connect(self._on_search_session_text_changed)
        lp_layout.addWidget(self.search_session_input)

        # Global keyboard shortcuts for quick navigation
        self._shortcut_new_chat = QShortcut(QKeySequence("Ctrl+N"), self)
        self._shortcut_new_chat.activated.connect(self._start_new_session)
        self._shortcut_search = QShortcut(QKeySequence("Ctrl+K"), self)
        self._shortcut_search.activated.connect(self._focus_session_search)

        # Session history list (chat icons + Crimson Red active pill)
        self.session_list_widget = QListWidget()
        self.session_list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.session_list_widget.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.session_list_widget.setIconSize(QSize(14, 14))
        self.session_list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.session_list_widget.customContextMenuRequested.connect(self._on_session_context_menu)
        self.session_list_widget.installEventFilter(self)
        self.session_list_widget.setStyleSheet(f"""
            QListWidget {{
                background: transparent;
                border: none;
                color: {COLORS.text_secondary};
                font-family: {TYPE.sans};
                font-size: 12.5px;
                outline: none;
                padding: 2px 0px;
            }}
            QListWidget::item {{
                padding: 7px 10px;
                border-radius: 8px;
                margin-bottom: 2px;
                border-left: 2px solid transparent;
            }}
            QListWidget::item:hover {{
                background: {COLORS.surface};
                color: {COLORS.text_primary};
            }}
            QListWidget::item:selected {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(225, 45, 83, 0.24),
                    stop:0.70 rgba(26, 29, 40, 0.88),
                    stop:1 rgba(20, 23, 32, 0.55)
                );
                color: {COLORS.text_primary};
                border-left: 2px solid {COLORS.crimson};
                font-weight: 600;
            }}
            {scrollbar_qss(4)}
        """)
        self.session_list_widget.itemClicked.connect(self._on_sidebar_session_clicked)
        self.session_list_widget.setMinimumHeight(60)
        lp_layout.addWidget(self.session_list_widget, stretch=1)

        # Hidden compatibility telemetry label
        self.lbl_sys_info = QLabel()
        self.lbl_sys_info.setVisible(False)
        self._update_sys_info()
        lp_layout.addWidget(self.lbl_sys_info)

        # Unified Relic Engram telemetry card (icon-labeled live metrics, collapses at height < 520px)
        hud_box = QFrame(self.left_panel)
        self.hud_box = hud_box
        hud_box.setObjectName("relicHud")
        hud_box.setStyleSheet(f"""
            QFrame#relicHud {{
                background: {COLORS.surface_alt};
                border: 1px solid {COLORS.border_subtle};
                border-radius: 12px;
            }}
        """)
        hb_layout = QVBoxLayout(hud_box)
        hb_layout.setContentsMargins(12, 11, 12, 11)
        hb_layout.setSpacing(7)

        sync_row = QHBoxLayout()
        sync_row.setSpacing(6)
        lbl_syn_icon = QLabel()
        lbl_syn_icon.setPixmap(get_icon("layers", COLORS.text_primary, 14).pixmap(14, 14))
        sync_row.addWidget(lbl_syn_icon)

        lbl_syn_header = QLabel("Энграмма")
        lbl_syn_header.setStyleSheet(
            f"color: {COLORS.text_primary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 700;"
        )
        sync_row.addWidget(lbl_syn_header)
        sync_row.addStretch()

        self.lbl_engram_pct = QLabel("70%")
        self.lbl_engram_pct.setStyleSheet(
            f"color: {COLORS.signal_cyan}; font-family: {TYPE.mono}; font-size: 12px; font-weight: 700;"
        )
        sync_row.addWidget(self.lbl_engram_pct)
        hb_layout.addLayout(sync_row)

        self.bar_engram = SegmentedEngramBar(segments=5, parent=hud_box)
        self.bar_engram.setFixedHeight(7)
        hb_layout.addWidget(self.bar_engram)

        stage_row = QHBoxLayout()
        stage_row.setSpacing(6)
        dot_stage = QLabel()
        dot_stage.setFixedSize(7, 7)
        dot_stage.setStyleSheet(f"background: {COLORS.signal_cyan}; border-radius: 3px; border: none;")
        stage_row.addWidget(dot_stage)

        self.lbl_stage = ElidingLabel(self.emotions.relationship_stage_label, hud_box)
        self.lbl_stage.setStyleSheet(
            f"color: {COLORS.text_primary}; font-family: {TYPE.sans}; font-size: 11px; font-weight: 600;"
        )
        stage_row.addWidget(self.lbl_stage, stretch=1)
        hb_layout.addLayout(stage_row)

        # Compact real telemetry rows with icons
        live_grid = QGridLayout()
        live_grid.setContentsMargins(0, 2, 0, 0)
        live_grid.setHorizontalSpacing(6)
        live_grid.setVerticalSpacing(4)

        def _mk_icon_key(icon_name: str, txt: str) -> QWidget:
            row_w = QWidget(hud_box)
            row_w.setStyleSheet("background: transparent; border: none;")
            rl = QHBoxLayout(row_w)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(6)
            ic = QLabel(row_w)
            ic.setPixmap(get_icon(icon_name, COLORS.text_muted, 12).pixmap(12, 12))
            rl.addWidget(ic)
            lb = QLabel(txt, row_w)
            lb.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 11px;")
            rl.addWidget(lb)
            rl.addStretch()
            return row_w

        def _mk_metric_val(txt: str, color: str = COLORS.text_primary) -> QLabel:
            lbl = QLabel(txt)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lbl.setStyleSheet(
                f"color: {color}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 600;"
            )
            return lbl

        self.lbl_metric_context = _mk_metric_val("0")
        self.lbl_metric_memory = _mk_metric_val("0 фактов")
        self.lbl_metric_tools = _mk_metric_val("0 активны", COLORS.amber)

        live_grid.addWidget(_mk_icon_key("cpu", "Контекст"), 0, 0)
        live_grid.addWidget(self.lbl_metric_context, 0, 1)
        live_grid.addWidget(_mk_icon_key("memory", "Память"), 1, 0)
        live_grid.addWidget(self.lbl_metric_memory, 1, 1)
        live_grid.addWidget(_mk_icon_key("terminal", "Инструменты"), 2, 0)
        live_grid.addWidget(self.lbl_metric_tools, 2, 1)
        hb_layout.addLayout(live_grid)

        # Compact emotional micro-footer inside Engram card (preserves lbl_mood, lbl_trust, lbl_irrit)
        hud_sep = QFrame(hud_box)
        hud_sep.setFixedHeight(1)
        hud_sep.setStyleSheet(f"background: {COLORS.border_hairline}; border: none;")
        hb_layout.addWidget(hud_sep)

        self.lbl_mood = QLabel("Настроение: Игривое")
        self.lbl_mood.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.sans}; font-size: 10px;"
        )
        hb_layout.addWidget(self.lbl_mood)

        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(6)
        self.lbl_trust = QLabel("Доверие +25")
        self.lbl_trust.setStyleSheet(
            f"color: {COLORS.mint}; font-family: {TYPE.mono}; font-size: 10px;"
        )
        self.lbl_irrit = QLabel("Шум 0%")
        self.lbl_irrit.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10px;"
        )
        metrics_row.addWidget(self.lbl_trust)
        metrics_row.addStretch()
        metrics_row.addWidget(self.lbl_irrit)
        hb_layout.addLayout(metrics_row)

        lp_layout.addWidget(hud_box)

        # Streamlined bottom utility dock (clean pill row + icon dock — compact & reachable at 360px)
        nav_sep = QFrame()
        nav_sep.setFixedHeight(1)
        nav_sep.setStyleSheet(f"background: {COLORS.border_hairline}; border: none;")
        lp_layout.addWidget(nav_sep)

        primary_nav = QHBoxLayout()
        primary_nav.setSpacing(6)
        primary_nav.setContentsMargins(0, 0, 0, 0)

        pill_nav_qss = f"""
            QPushButton {{
                background: {COLORS.surface_alt};
                color: {COLORS.text_secondary};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 8px;
                padding: 5px 10px;
                font-family: {TYPE.sans};
                font-size: 11.5px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background: {COLORS.surface_raised};
                border-color: {COLORS.border_interactive};
                color: {COLORS.text_primary};
            }}
        """

        self.btn_sessions = QPushButton("Сессии")
        self.btn_sessions.setIcon(get_icon("sessions", COLORS.text_secondary, 13))
        self.btn_sessions.setIconSize(QSize(13, 13))
        self.btn_sessions.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_sessions.setToolTip("Архив и резервные копии диалогов (/sessions)")
        self.btn_sessions.setStyleSheet(pill_nav_qss)
        self.btn_sessions.clicked.connect(self._show_sessions_dialog)
        primary_nav.addWidget(self.btn_sessions, stretch=1)

        self.btn_memory_wb = QPushButton("Память")
        self.btn_memory_wb.setIcon(get_icon("memory", COLORS.text_secondary, 13))
        self.btn_memory_wb.setIconSize(QSize(13, 13))
        self.btn_memory_wb.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_memory_wb.setToolTip("Memory Workbench — банк долговременной памяти (/memory)")
        self.btn_memory_wb.setStyleSheet(pill_nav_qss)
        self.btn_memory_wb.clicked.connect(self._show_memory_workbench)
        primary_nav.addWidget(self.btn_memory_wb, stretch=1)
        lp_layout.addLayout(primary_nav)

        # Compact horizontal icon-utility dock at the very bottom
        icon_dock = QHBoxLayout()
        icon_dock.setContentsMargins(2, 2, 2, 0)
        icon_dock.setSpacing(6)

        dock_icon_qss = button_ghost_qss(
            text_color=COLORS.text_muted,
            hover_text=COLORS.text_primary,
            hover_bg=COLORS.surface_raised,
            padding="0px",
            radius=8,
        )

        self.btn_changelog = QPushButton()
        self.btn_changelog.setIcon(get_icon("changelog", COLORS.text_muted, 15))
        self.btn_changelog.setIconSize(QSize(15, 15))
        self.btn_changelog.setFixedSize(32, 30)
        self.btn_changelog.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_changelog.setToolTip("Хроника обновлений (CHANGELOG.md)")
        self.btn_changelog.setStyleSheet(dock_icon_qss)
        self.btn_changelog.clicked.connect(self._show_changelog)
        icon_dock.addWidget(self.btn_changelog)

        btn_dock_relic = QPushButton()
        btn_dock_relic.setIcon(get_icon("layers", COLORS.text_muted, 15))
        btn_dock_relic.setIconSize(QSize(15, 15))
        btn_dock_relic.setFixedSize(32, 30)
        btn_dock_relic.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_dock_relic.setToolTip("Открыть панель Relic Construct")
        btn_dock_relic.setStyleSheet(dock_icon_qss)
        btn_dock_relic.clicked.connect(self._open_relic_dashboard)
        icon_dock.addWidget(btn_dock_relic)

        icon_dock.addStretch()

        self.btn_reset = QPushButton()
        self.btn_reset.setIcon(get_icon("reset", COLORS.text_muted, 14))
        self.btn_reset.setIconSize(QSize(14, 14))
        self.btn_reset.setFixedSize(32, 30)
        self.btn_reset.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_reset.setToolTip("Сбросить энграмму и эмоциональное состояние (/reset)")
        self.btn_reset.setStyleSheet(button_danger_qss(filled=False, padding="0px", radius=8))
        self.btn_reset.clicked.connect(self._prompt_reset_capri)
        icon_dock.addWidget(self.btn_reset)

        self.btn_quit = QPushButton()
        self.btn_quit.setIcon(get_icon("power", COLORS.crimson, 15))
        self.btn_quit.setIconSize(QSize(15, 15))
        self.btn_quit.setFixedSize(32, 30)
        self.btn_quit.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_quit.setToolTip("Полностью выключить Капри (закрыть окно, трей и аватар)")
        self.btn_quit.setStyleSheet(button_danger_qss(filled=False, padding="0px", radius=8))
        self.btn_quit.clicked.connect(self._quit_application)
        icon_dock.addWidget(self.btn_quit)

        lp_layout.addLayout(icon_dock)

        self.central_layout.addWidget(self.left_panel)

        self._sidebar_expanded = True
        self._drawer_open = False

        # ── RIGHT MAIN WORKSPACE ──────────────────────────────────────
        self.workspace_panel = QWidget(self)
        self.workspace_panel.setObjectName("workspacePanel")
        self.workspace_panel.setStyleSheet("QWidget#workspacePanel { background: transparent; }")
        right_panel = QVBoxLayout(self.workspace_panel)
        right_panel.setContentsMargins(0, 0, 0, 0)
        right_panel.setSpacing(0)

        # Calm header row (works cleanly with Niri frameless/tiled behavior)
        self.header_bar = QFrame(self.workspace_panel)
        self.header_bar.setObjectName("headerBar")
        self.header_bar.setFixedHeight(METRICS.header_height)
        self.header_bar.setStyleSheet(f"""
            QFrame#headerBar {{
                background: transparent;
                border: none;
                border-bottom: 1px solid {COLORS.border_hairline};
            }}
        """)
        top_bar = QHBoxLayout(self.header_bar)
        top_bar.setContentsMargins(12, 0, 12, 0)
        top_bar.setSpacing(8)

        self.btn_toggle_sidebar = QPushButton()
        self.btn_toggle_sidebar.setIcon(get_icon("sidebar", COLORS.text_secondary, 16))
        self.btn_toggle_sidebar.setIconSize(QSize(16, 16))
        self.btn_toggle_sidebar.setFixedSize(34, 34)
        self.btn_toggle_sidebar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_toggle_sidebar.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_toggle_sidebar.setToolTip("Показать / скрыть навигацию")
        self.btn_toggle_sidebar.setStyleSheet(
            button_ghost_qss(
                text_color=COLORS.text_secondary,
                hover_text=COLORS.text_primary,
                hover_bg=COLORS.surface,
                padding="0px",
                radius=8,
            )
        )
        self.btn_toggle_sidebar.clicked.connect(self._toggle_sidebar)
        top_bar.addWidget(self.btn_toggle_sidebar)

        self.lbl_active_session_title = ElidingLabel("Новый диалог")
        self.lbl_active_session_title.setStyleSheet(f"""
            color: {COLORS.text_primary};
            font-family: {TYPE.sans};
            font-size: 13.5px;
            font-weight: 600;
        """)
        top_bar.addWidget(self.lbl_active_session_title, stretch=1)

        self.lbl_turn_counter = QLabel("0 раундов")
        self.lbl_turn_counter.setVisible(False)

        self.btn_mode = QPushButton()
        self.btn_mode.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_mode.setToolTip("Переключить режим компаньона (Normal / Focus / Playful / Quiet)")
        self.btn_mode.setFixedHeight(30)
        self.btn_mode.clicked.connect(self._cycle_mode)
        self._refresh_mode_button()
        top_bar.addWidget(self.btn_mode)

        self.lbl_status_chip = QLabel("● Online")
        self.lbl_status_chip.setStyleSheet(
            f"color: {COLORS.signal_cyan}; font-family: {TYPE.mono}; font-size: 11px; font-weight: 600; padding: 0px 4px;"
        )
        top_bar.addWidget(self.lbl_status_chip)

        # Clean separator before right-hand icon action cluster
        hdr_sep = QFrame(self.header_bar)
        hdr_sep.setFixedSize(1, 18)
        hdr_sep.setStyleSheet(f"background: {COLORS.border_subtle}; border: none;")
        top_bar.addWidget(hdr_sep)

        prov_name = (self.config.llm.provider or "GROQ").upper()
        self.btn_provider = QPushButton(prov_name)
        self.btn_provider.setIcon(get_icon("cpu", COLORS.text_secondary, 13))
        self.btn_provider.setIconSize(QSize(13, 13))
        self.btn_provider.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_provider.setToolTip("Настройки нейроядра, модели и Reduced Motion")
        self.btn_provider.setFixedHeight(30)
        self.btn_provider.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS.surface_alt};
                color: {COLORS.text_secondary};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 9px;
                padding: 0px 10px;
                font-family: {TYPE.mono};
                font-size: 10.5px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {COLORS.surface_raised};
                border-color: {COLORS.border_interactive};
                color: {COLORS.text_primary};
            }}
            QPushButton:focus {{
                border-color: {COLORS.focus_ring};
            }}
        """)
        self.btn_provider.clicked.connect(self._show_provider_settings)
        top_bar.addWidget(self.btn_provider)

        self.btn_relic_hud = QPushButton()
        self.btn_relic_hud.setIcon(get_icon("layers", COLORS.text_secondary, 15))
        self.btn_relic_hud.setIconSize(QSize(15, 15))
        self.btn_relic_hud.setFixedSize(32, 32)
        self.btn_relic_hud.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_relic_hud.setToolTip("Открыть панель Relic Construct")
        self.btn_relic_hud.setStyleSheet(
            button_ghost_qss(
                text_color=COLORS.text_secondary,
                hover_text=COLORS.signal_cyan,
                hover_bg=COLORS.surface_raised,
                padding="0px",
                radius=8,
            )
        )
        self.btn_relic_hud.clicked.connect(self._open_relic_dashboard)
        top_bar.addWidget(self.btn_relic_hud)

        # Compatibility hidden version button (accessible in sidebar & overflow menu)
        self.btn_ver = QPushButton(f"v{__version__}")
        self.btn_ver.setVisible(False)
        self.btn_ver.clicked.connect(self._show_changelog)

        self.btn_more = QPushButton()
        self.btn_more.setIcon(get_icon("more", COLORS.text_secondary, 16))
        self.btn_more.setIconSize(QSize(16, 16))
        self.btn_more.setFixedSize(32, 32)
        self.btn_more.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_more.setToolTip("Статус, анимации и дополнительные действия")
        self.btn_more.setStyleSheet(
            button_ghost_qss(
                text_color=COLORS.text_secondary,
                hover_text=COLORS.text_primary,
                hover_bg=COLORS.surface,
                padding="0px",
                radius=8,
            )
        )
        self.btn_more.clicked.connect(self._open_overflow_menu)
        top_bar.addWidget(self.btn_more)

        self.btn_close = QPushButton()
        self.btn_close.setIcon(get_icon("close", COLORS.text_muted, 15))
        self.btn_close.setIconSize(QSize(15, 15))
        self.btn_close.setFixedSize(32, 32)
        self.btn_close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_close.setToolTip("Закрыть окно")
        self.btn_close.setStyleSheet(
            button_ghost_qss(
                text_color=COLORS.text_muted,
                hover_text=COLORS.coral,
                hover_bg=COLORS.coral_soft,
                padding="0px",
                radius=8,
            )
        )
        self.btn_close.clicked.connect(self.close)
        top_bar.addWidget(self.btn_close)

        right_panel.addWidget(self.header_bar)

        # Conversation scroll surface (borderless canvas — dominant visual weight)
        self.scroll_area = QScrollArea(self.workspace_panel)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet(f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            {scrollbar_qss(6)}
        """)

        scroll_content = QWidget()
        scroll_content.setMinimumWidth(0)
        scroll_content.setStyleSheet("background: transparent;")
        sc_layout = QHBoxLayout(scroll_content)
        sc_layout.setContentsMargins(28, 0, 28, 0)
        sc_layout.addStretch(1)
        self._sc_layout = sc_layout

        self.chat_container = QWidget(scroll_content)
        self.chat_container.setMinimumWidth(0)
        self.chat_container.setMaximumWidth(METRICS.column_max_width)
        self.chat_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.chat_container.setStyleSheet("background: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(6, 14, 6, 12)
        self.chat_layout.setSpacing(6)

        self.welcome_canvas = WelcomeCanvas(
            avatar_enabled=bool(getattr(self.config.ui, "avatar_enabled", True)),
            parent=self.chat_container,
        )
        self.welcome_canvas.suggestion_triggered.connect(self._on_welcome_suggestion)
        self.chat_layout.addWidget(self.welcome_canvas)
        self.chat_layout.addStretch(1)

        sc_layout.addWidget(self.chat_container, 100)
        sc_layout.addStretch(1)

        self.scroll_area.setWidget(scroll_content)
        right_panel.addWidget(self.scroll_area, stretch=1)

        # ── COMPOSER AREA (aligned to shared fluid conversation column) ─────
        composer_outer = QHBoxLayout()
        composer_outer.setContentsMargins(28, 4, 28, 16)
        composer_outer.setSpacing(0)
        composer_outer.addStretch(1)
        self._composer_outer = composer_outer

        composer_col = QWidget(self.workspace_panel)
        composer_col.setMinimumWidth(0)
        composer_col.setMaximumWidth(METRICS.column_max_width)
        composer_col.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.composer_col = composer_col
        composer_col_layout = QVBoxLayout(composer_col)
        composer_col_layout.setContentsMargins(0, 0, 0, 0)
        composer_col_layout.setSpacing(8)

        # State-driven AudioWaveVisualizer (aligned to composer, hidden in idle!)
        self.visualizer = AudioWaveVisualizer(
            parent=composer_col,
            reduced_motion=self.motion.reduced_motion,
        )
        composer_col_layout.addWidget(self.visualizer)

        # User-controlled Pending Memory Candidate approval bar (hidden when queue is empty)
        self.memory_candidate_bar = QFrame(composer_col)
        self.memory_candidate_bar.setObjectName("MemoryCandidateBar")
        self.memory_candidate_bar.setStyleSheet(f"""
            QFrame#MemoryCandidateBar {{
                background: {COLORS.surface_raised};
                border: 1px solid {COLORS.crimson};
                border-radius: 12px;
            }}
        """)
        mem_bar_layout = QHBoxLayout(self.memory_candidate_bar)
        mem_bar_layout.setContentsMargins(12, 8, 10, 8)
        mem_bar_layout.setSpacing(8)

        mem_text_col = QVBoxLayout()
        mem_text_col.setContentsMargins(0, 0, 0, 0)
        mem_text_col.setSpacing(2)

        self.lbl_mem_cand_badge = QLabel("")
        self.lbl_mem_cand_badge.setStyleSheet(
            f"color: {COLORS.crimson}; font-family: {TYPE.mono}; font-size: 10px; font-weight: 700; border: none;"
        )
        self.lbl_mem_cand_text = QLabel("")
        self.lbl_mem_cand_text.setWordWrap(True)
        self.lbl_mem_cand_text.setStyleSheet(f"color: {COLORS.text_primary}; font-size: 12px; border: none;")
        mem_text_col.addWidget(self.lbl_mem_cand_badge)
        mem_text_col.addWidget(self.lbl_mem_cand_text)
        mem_bar_layout.addLayout(mem_text_col, stretch=1)

        self.btn_mem_approve = QPushButton("Запомнить")
        self.btn_mem_approve.setMinimumHeight(28)
        self.btn_mem_approve.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_mem_approve.setStyleSheet(button_primary_qss("4px 11px"))
        self.btn_mem_approve.clicked.connect(self._on_approve_pending_memory)
        mem_bar_layout.addWidget(self.btn_mem_approve)

        self.btn_mem_edit = QPushButton("Править")
        self.btn_mem_edit.setMinimumHeight(28)
        self.btn_mem_edit.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_mem_edit.setStyleSheet(button_secondary_qss("4px 10px"))
        self.btn_mem_edit.clicked.connect(self._on_edit_pending_memory)
        mem_bar_layout.addWidget(self.btn_mem_edit)

        self.btn_mem_reject = QPushButton("Отклонить")
        self.btn_mem_reject.setMinimumHeight(28)
        self.btn_mem_reject.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_mem_reject.setStyleSheet(button_danger_qss(filled=False, padding="4px 10px"))
        self.btn_mem_reject.clicked.connect(self._on_reject_pending_memory)
        mem_bar_layout.addWidget(self.btn_mem_reject)

        self.memory_candidate_bar.setVisible(False)
        composer_col_layout.addWidget(self.memory_candidate_bar)

        # Sleek Composer Capsule with custom Crimson corner glow
        self.composer_capsule = ComposerCapsuleFrame(composer_col)
        capsule_layout = QVBoxLayout(self.composer_capsule)
        capsule_layout.setContentsMargins(14, 10, 12, 10)
        capsule_layout.setSpacing(6)
        self.composer_capsule_layout = capsule_layout

        self.attachment_strip = AttachmentStripWidget(self.composer_capsule)
        capsule_layout.addWidget(self.attachment_strip)

        self.input_field = ComposerTextEdit(self.composer_capsule, min_height=34, max_height=136)
        self.input_field.setPlaceholderText("Написать Капри...   / для команд")
        self.input_field.returnPressed.connect(self._send_message)
        self.input_field.focusChanged.connect(self._on_composer_focus_changed)
        self.input_field.voiceShortcutPressed.connect(self._toggle_voice_input)
        self.input_field.filesDropped.connect(self._attach_file_paths)
        self.input_field.imagePasted.connect(self._attach_pasted_image)
        capsule_layout.addWidget(self.input_field)

        toolbar_row = QHBoxLayout()
        toolbar_row.setContentsMargins(0, 2, 0, 0)
        toolbar_row.setSpacing(6)

        composer_icon_qss = button_ghost_qss(
            text_color=COLORS.text_secondary,
            hover_text=COLORS.text_primary,
            hover_bg=COLORS.surface_raised,
            padding="0px",
            radius=9,
        )

        self.btn_attach = QPushButton()
        self.btn_attach.setIcon(get_icon("paperclip", COLORS.text_secondary, 16))
        self.btn_attach.setIconSize(QSize(16, 16))
        self.btn_attach.setFixedSize(34, 34)
        self.btn_attach.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_attach.setToolTip("Прикрепить файл или изображение (Drag & Drop / Ctrl+V)")
        self.btn_attach.setStyleSheet(composer_icon_qss)
        self.btn_attach.clicked.connect(self._pick_attachments_dialog)
        toolbar_row.addWidget(self.btn_attach)

        self.btn_vision = QPushButton()
        self.btn_vision.setIcon(get_icon("eye", COLORS.text_secondary, 16))
        self.btn_vision.setIconSize(QSize(16, 16))
        self.btn_vision.setFixedSize(34, 34)
        self.btn_vision.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_vision.setToolTip("Посмотреть на экран через Vision AI (/look)")
        self.btn_vision.setStyleSheet(composer_icon_qss)
        self.btn_vision.clicked.connect(self._trigger_vision)
        toolbar_row.addWidget(self.btn_vision)

        self.btn_commands = QPushButton()
        self.btn_commands.setIcon(get_icon("code", COLORS.text_secondary, 15))
        self.btn_commands.setIconSize(QSize(15, 15))
        self.btn_commands.setFixedSize(34, 34)
        self.btn_commands.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_commands.setToolTip("Быстрые слэш-команды (/status, /browser, /call, /mcp, /doctor)")
        self.btn_commands.setStyleSheet(composer_icon_qss)
        self.btn_commands.clicked.connect(self._open_quick_commands_menu)
        toolbar_row.addWidget(self.btn_commands)

        hint_slot = QWidget(self.composer_capsule)
        hint_slot.setMinimumWidth(0)
        hint_slot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        hint_slot.setStyleSheet("background: transparent; border: none;")
        hint_slot_layout = QHBoxLayout(hint_slot)
        hint_slot_layout.setContentsMargins(4, 0, 4, 0)
        hint_slot_layout.setSpacing(0)

        self.lbl_composer_hint = ElidingLabel("Enter — отправить · Shift+Enter — перенос · Ctrl+M — голос")
        self.lbl_composer_hint.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10px;"
        )
        hint_slot_layout.addWidget(self.lbl_composer_hint, stretch=1)
        toolbar_row.addWidget(hint_slot, stretch=1)

        self.btn_mic = QPushButton()
        self.btn_mic.setFixedSize(34, 34)
        self.btn_mic.setIconSize(QSize(16, 16))
        self.btn_mic.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_mic.clicked.connect(self._toggle_voice_input)
        toolbar_row.addWidget(self.btn_mic)

        self.btn_send = QPushButton()
        self.btn_send.setFixedSize(44, 34)
        self.btn_send.setIconSize(QSize(16, 16))
        self.btn_send.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_send.clicked.connect(self._on_send_button_clicked)
        toolbar_row.addWidget(self.btn_send)

        capsule_layout.addLayout(toolbar_row)
        composer_col_layout.addWidget(self.composer_capsule)

        composer_outer.addWidget(composer_col, 100)
        composer_outer.addStretch(1)
        right_panel.addLayout(composer_outer)

        self.central_layout.addWidget(self.workspace_panel, stretch=1)
        main_layout.addLayout(self.central_layout)

        # Drawer backdrop for compact widths (< 1000px)
        self.drawer_backdrop = ClickableOverlay(self)
        self.drawer_backdrop.clicked.connect(self._close_drawer)

        self._reset_mic_button()
        self._reset_send_button()
        self._apply_composer_state()
        self._refresh_memory_candidate_bar()
        self.refresh_stats()

    def _refresh_memory_candidate_bar(self):
        if not hasattr(self, "memory_candidate_bar"):
            return
        mem_store = getattr(self.agent, "memory_store", None)
        pending = mem_store.list_pending() if mem_store is not None else []
        if not pending:
            self.memory_candidate_bar.setVisible(False)
            return
        cand = pending[0]
        ret_lbl = "ВРЕМЕННО 72Ч" if cand.retention == "ephemeral" else "ПОСТОЯННО"
        cat_lbl = CATEGORY_LABELS.get(cand.category, cand.category)
        self.lbl_mem_cand_badge.setText(
            f"ЗАПОМНИТЬ? ({1}/{len(pending)}) · {ret_lbl} · {cat_lbl.upper()}"
        )
        self.lbl_mem_cand_text.setText(f"[{cand.key}]: {cand.fact}")
        tooltip = f"ID: {cand.id}"
        if cand.source_quote:
            tooltip += f"\nИсточник: «{cand.source_quote}»"
        self.memory_candidate_bar.setToolTip(tooltip)
        self.memory_candidate_bar.setVisible(True)

    def _on_memory_candidates(self, _candidates: list):
        self._refresh_memory_candidate_bar()
        self.refresh_stats()

    def _on_approve_pending_memory(self):
        mem_store = getattr(self.agent, "memory_store", None)
        if mem_store is None:
            return
        pending = mem_store.list_pending()
        if not pending:
            self._refresh_memory_candidate_bar()
            return
        cand = pending[0]
        entry = mem_store.approve_candidate(cand.id)
        if entry is not None:
            play_ui_sound("ui_click")
        self._refresh_memory_candidate_bar()
        self.refresh_stats()

    def _on_edit_pending_memory(self):
        mem_store = getattr(self.agent, "memory_store", None)
        if mem_store is None:
            return
        pending = mem_store.list_pending()
        if not pending:
            self._refresh_memory_candidate_bar()
            return
        cand = pending[0]
        dlg = CapriMemoryEditDialog(self, candidate=cand)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            mem_store.approve_candidate(
                cand.id,
                key=data["key"],
                fact=data["fact"],
                category=data["category"],
                importance=data["importance"],
                retention=data.get("retention", cand.retention),
                tags=data["tags"],
            )
            play_ui_sound("ui_click")
        self._refresh_memory_candidate_bar()
        self.refresh_stats()

    def _on_reject_pending_memory(self):
        mem_store = getattr(self.agent, "memory_store", None)
        if mem_store is None:
            return
        pending = mem_store.list_pending()
        if not pending:
            self._refresh_memory_candidate_bar()
            return
        mem_store.reject_candidate(pending[0].id)
        self._refresh_memory_candidate_bar()
        self.refresh_stats()

    def _on_composer_focus_changed(self, focused: bool):
        self._composer_focused = focused
        self._apply_composer_state()

    def _focus_session_search(self):
        if self.width() < METRICS.drawer_breakpoint:
            if not self._drawer_open:
                self._open_drawer()
        elif not self._sidebar_expanded:
            self._toggle_sidebar()
        self.search_session_input.setFocus()
        self.search_session_input.selectAll()

    def _workspace_width(self) -> int:
        w = self.width()
        docked = (w >= METRICS.drawer_breakpoint) and getattr(self, "_sidebar_expanded", True)
        return max(320, w - (METRICS.sidebar_default if docked else 0))

    def _composer_hint_for_width(self, ws_w: int) -> str:
        if ws_w >= 760:
            return "Enter — отправить · Shift+Enter — перенос · Ctrl+V — скрин · Ctrl+M — голос"
        return "Enter — отправить · Shift+Enter — перенос"

    def _apply_composer_state(self):
        ws_w = self._workspace_width()
        if self._composer_recording:
            self.lbl_composer_hint.setText("Идёт запись речи · клик или Esc для отмены")
            self.lbl_composer_hint.setStyleSheet(
                f"color: {COLORS.coral}; font-family: {TYPE.mono}; font-size: 10px; font-weight: 600;"
            )
            self.lbl_composer_hint.setVisible(True)
        else:
            self.lbl_composer_hint.setText(self._composer_hint_for_width(ws_w))
            self.lbl_composer_hint.setStyleSheet(
                f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10px;"
            )
            self.lbl_composer_hint.setVisible(ws_w >= 540)

        if hasattr(self.composer_capsule, "set_capsule_state"):
            self.composer_capsule.set_capsule_state(
                focused=bool(self._composer_focused),
                recording=bool(self._composer_recording),
            )

    def _open_relic_dashboard(self):
        play_ui_sound("ui_click")
        if not hasattr(self, "_relic_dashboard") or self._relic_dashboard is None:
            from .dashboard import CapriRelicDashboard
            self._relic_dashboard = CapriRelicDashboard()
        self._relic_dashboard.refresh_stats()
        self._relic_dashboard.show()
        self._relic_dashboard.raise_()
        self._relic_dashboard.activateWindow()

    def _open_quick_commands_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(menu_qss())
        apply_dark_palette(menu)
        cmds = [
            ("/status", "Статус системы (CPU / GPU / RAM)"),
            ("/look", "Посмотреть на экран (Vision AI)"),
            ("/browser status", "Изолированный браузер Capri"),
            ("/call status", "Join Voice (PipeWire мост)"),
            ("/mcp status", "MCP-серверы и инструменты"),
            ("/doctor", "Самодиагностика (/doctor)"),
            ("/wm-rules", "Правила Niri / Hyprland"),
        ]
        for cmd_str, desc in cmds:
            act = menu.addAction(f"{cmd_str}  —  {desc}")
            act.triggered.connect(lambda _c=False, c=cmd_str: self._handle_command(c))
        pos = self.btn_commands.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 6))
        menu.exec(pos)

    def _refresh_mode_button(self):
        mode = (self.config.companion.mode or "normal").lower()
        mode_meta = {
            "normal": ("Normal", COLORS.signal_cyan),
            "focus": ("Focus", COLORS.lilac),
            "playful": ("Playful", COLORS.crimson_hover),
            "quiet": ("Quiet", COLORS.text_muted),
        }
        label, accent = mode_meta.get(mode, (mode.capitalize(), COLORS.signal_cyan))
        self.btn_mode.setText(f"{label.upper()}  ▾")
        self.btn_mode.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS.surface};
                color: {accent};
                border: 1px solid {COLORS.border_subtle};
                border-radius: 15px;
                padding: 0px 13px;
                font-family: {TYPE.mono};
                font-size: 10.5px;
                font-weight: 700;
                letter-spacing: 0.4px;
            }}
            QPushButton:hover {{
                background: {COLORS.surface_raised};
                border-color: {COLORS.border_interactive};
            }}
            QPushButton:focus {{
                border-color: {COLORS.focus_ring};
            }}
        """)

    def _set_reduced_motion(self, enabled: bool, persist: bool = True):
        flag = bool(enabled)
        self.config.ui.reduced_motion = flag
        self.motion.set_reduced_motion(flag)
        if hasattr(self, "visualizer") and self.visualizer:
            self.visualizer.set_reduced_motion(flag)
        if persist:
            save_config(self.config)

    def _open_overflow_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(menu_qss())

        cur_sess = self.agent.session_manager.get_session(self.agent.current_session_id)
        rounds_text = _format_rounds_ru(cur_sess.turn_count if cur_sess else 0)
        act_status = menu.addAction(f"● Online  ·  {rounds_text}  ·  v{__version__}")
        act_status.setEnabled(False)

        act_model = menu.addAction(f"Модель: {self.config.llm.model}")
        act_model.setEnabled(False)
        menu.addSeparator()

        act_reduced = QAction("Уменьшить анимации (Reduced Motion)", menu)
        act_reduced.setCheckable(True)
        act_reduced.setChecked(bool(self.motion.reduced_motion))
        act_reduced.toggled.connect(lambda checked: self._set_reduced_motion(checked, persist=True))
        menu.addAction(act_reduced)

        menu.addSeparator()
        act_settings = menu.addAction(get_icon("settings", COLORS.text_secondary, 14), "Настройки нейроядра...")
        act_settings.triggered.connect(self._show_provider_settings)

        act_memory = menu.addAction(get_icon("memory", COLORS.text_secondary, 14), "Memory Workbench")
        act_memory.triggered.connect(self._show_memory_workbench)

        act_sessions = menu.addAction(get_icon("sessions", COLORS.text_secondary, 14), "Архив сессий")
        act_sessions.triggered.connect(self._show_sessions_dialog)

        act_changelog = menu.addAction(get_icon("changelog", COLORS.text_secondary, 14), f"Обновления (v{__version__})")
        act_changelog.triggered.connect(self._show_changelog)

        menu.addSeparator()
        act_doctor = menu.addAction(get_icon("activity", COLORS.signal_cyan, 14), "Диагностика (/doctor)")
        act_doctor.triggered.connect(lambda: self._handle_command("/doctor"))

        act_bugreport = menu.addAction(get_icon("file", COLORS.text_secondary, 14), "Собрать баг-репорт (/bugreport)")
        act_bugreport.triggered.connect(lambda: self._handle_command("/bugreport"))

        act_setup = menu.addAction(get_icon("settings", COLORS.text_secondary, 14), "Мастер настройки (/setup)")
        act_setup.triggered.connect(self._show_first_run_wizard)

        act_prof_exp = menu.addAction(get_icon("download", COLORS.text_secondary, 14), "Экспорт профиля (.zip)...")
        act_prof_exp.triggered.connect(self._export_profile_dialog)

        act_prof_imp = menu.addAction(get_icon("upload", COLORS.text_secondary, 14), "Восстановить профиль (.zip)...")
        act_prof_imp.triggered.connect(self._restore_profile_dialog)

        pos = self.btn_more.mapToGlobal(QPoint(0, self.btn_more.height() + 4))
        menu.exec(pos)

    def _refresh_welcome_canvas(self, context: str = "startup"):
        user_name = getattr(self.config.companion, "user_name", "User") or "User"
        greeting = self.emotions.generate_greeting(user_name=user_name, context=context)
        prov = (self.config.llm.provider or "GROQ").upper()
        subtitle = f"{self.emotions.relationship_stage_label}  ·  {prov} / {self.config.llm.model}"
        self.welcome_canvas.update_state(
            greeting=greeting,
            subtitle=subtitle,
            avatar_enabled=bool(getattr(self.config.ui, "avatar_enabled", True)),
            user_name=user_name,
        )

    def _show_first_run_wizard(self):
        dlg = CapriFirstRunWizard(self.config, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.agent.reload_config(self.config)
            self._barge_in_stt = None
            self._update_sys_info()
            self.btn_provider.setText((self.config.llm.provider or "GROQ").upper())
            self._refresh_welcome_canvas(context="startup")
            self._append_message(
                "Система",
                f"Настройка сохранена: пользователь **{self.config.companion.user_name}**, "
                f"провайдер **{(self.config.llm.provider or '').upper()}**, политика **{self.config.safety.tool_policy.upper()}**.",
            )

    def _export_profile_dialog(self):
        from ..core.backup import export_profile_backup
        from ..core.config import USER_CONFIG_DIR
        default_dir = USER_CONFIG_DIR / "backups"
        default_dir.mkdir(parents=True, exist_ok=True)
        default_name = str(default_dir / f"capri_profile_v{__version__}_{time.strftime('%Y%m%d_%H%M%S')}.capri-backup.zip")
        out_path, _ = QFileDialog.getSaveFileName(self, "Экспорт профиля Capri", default_name, "Capri Backup (*.zip)")
        if not out_path:
            return
        try:
            res = export_profile_backup(output_path=Path(out_path))
            self._append_message(
                "Система",
                f"Профиль сохранён: `{res.archive_path}` ({len(res.included_files)} файлов, схема v{res.schema_version}, без API-ключей).",
            )
        except Exception as e:
            self._append_message("Система", f"Ошибка экспорта профиля: {e}")

    def _reload_after_profile_restore(self):
        """Reload all stateful subsystems consistently after profile backup restoration."""
        self.emotions.load()
        self.agent.memory_store.load()
        self.agent.context_store.load()
        if hasattr(self.agent, "initiative_engine") and self.agent.initiative_engine:
            self.agent.initiative_engine.load()
        if hasattr(self.agent, "session_manager") and self.agent.session_manager:
            active_sess = self.agent.session_manager.get_or_create_active_session()
            self.agent.current_session_id = active_sess.id
            self.agent.reload_history(active_sess.id)
        self._refresh_sidebar_sessions()
        self.refresh_stats()

    def _restore_profile_dialog(self):
        from ..core.backup import restore_profile_backup
        from ..core.config import USER_CONFIG_DIR
        in_path, _ = QFileDialog.getOpenFileName(
            self, "Восстановить профиль Capri", str(USER_CONFIG_DIR), "Capri Backup (*.zip)"
        )
        if not in_path:
            return
        reply = QMessageBox.question(
            self,
            "Восстановление профиля",
            "Восстановить память, эмоции, контекст и историю сессий из архива?\n"
            "Перед заменой автоматически создастся резервная копия текущего состояния.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            res = restore_profile_backup(Path(in_path))
            self._reload_after_profile_restore()
            self._append_message(
                "Система",
                f"Профиль восстановлен ({', '.join(res.restored_files)}). "
                f"Снимок до восстановления: `{res.pre_restore_snapshot}`.",
            )
        except Exception as e:
            self._append_message("Система", f"Ошибка восстановления профиля: {e}")

    def _chat_card_count(self) -> int:
        count = 0
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and isinstance(item.widget(), ChatMessageCard):
                count += 1
        return count

    def _update_welcome_visibility(self):
        if hasattr(self, "welcome_canvas") and self.welcome_canvas:
            is_empty = self._chat_card_count() == 0
            self.welcome_canvas.setVisible(is_empty)
            if is_empty and hasattr(self, "scroll_area") and self.scroll_area:
                self.welcome_canvas.sync_viewport_height(self.scroll_area.viewport().height())

    def _clear_chat_cards(self):
        for i in reversed(range(self.chat_layout.count())):
            item = self.chat_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, ChatMessageCard):
                self.chat_layout.removeWidget(w)
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _on_welcome_suggestion(self, action_id: str):
        play_ui_sound("ui_click")
        if action_id == "vision":
            self._trigger_vision()
        elif action_id == "status":
            self._handle_command("/status")
        elif action_id == "memory":
            self._show_memory_workbench()
        elif action_id == "context":
            self._handle_command("/context")

    def eventFilter(self, obj, event):
        if obj is getattr(self, "session_list_widget", None) and event.type() == QEvent.Type.KeyPress:
            item = self.session_list_widget.currentItem()
            if item:
                sess_id = item.data(Qt.ItemDataRole.UserRole)
                sess_title = item.data(Qt.ItemDataRole.UserRole + 1) or item.text().strip()
                if sess_id:
                    if event.key() == Qt.Key.Key_F2:
                        self._rename_session_by_id(sess_id, sess_title)
                        return True
                    if event.key() == Qt.Key.Key_Delete:
                        self._delete_session_by_id(sess_id, sess_title)
                        return True
        return super().eventFilter(obj, event)

    def _apply_sidebar_density(self, is_compact_h: bool):
        if hasattr(self, "hud_box") and self.hud_box:
            self.hud_box.setVisible(not is_compact_h)
        if hasattr(self, "sidebar_brand_row") and self.sidebar_brand_row:
            self.sidebar_brand_row.setVisible(not is_compact_h)
        if hasattr(self, "btn_new_session") and self.btn_new_session:
            self.btn_new_session.setFixedHeight(METRICS.compact_control_height if is_compact_h else METRICS.control_height)
        if hasattr(self, "search_session_input") and self.search_session_input:
            self.search_session_input.setFixedHeight(28 if is_compact_h else 32)
        if hasattr(self, "lp_layout") and self.lp_layout:
            if is_compact_h:
                self.lp_layout.setContentsMargins(8, 8, 8, 8)
                self.lp_layout.setSpacing(4)
            else:
                self.lp_layout.setContentsMargins(12, 12, 12, 12)
                self.lp_layout.setSpacing(8)

    def _toggle_sidebar(self):
        """Toggle docked sidebar (wide) or animated overlay drawer (compact)."""
        play_ui_sound("ui_click")
        if self.width() < METRICS.drawer_breakpoint:
            if self._drawer_open:
                self._close_drawer()
            else:
                self._open_drawer()
        else:
            self._sidebar_expanded = not self._sidebar_expanded
            if self._sidebar_expanded:
                self.left_panel.show()
                self.left_panel.setMaximumWidth(METRICS.sidebar_default)
                self.left_panel.setMinimumWidth(METRICS.sidebar_min)
            else:
                self.left_panel.hide()
            self._update_responsive_chrome()

    def _current_column_width(self) -> int:
        ws_w = self._workspace_width()
        if ws_w >= 1400:
            side = 32
            col_max = METRICS.column_max_width
        elif ws_w >= 960:
            side = 24
            col_max = METRICS.column_max_width
        elif ws_w >= 680:
            side = 16
            col_max = max(320, ws_w - 2 * side)
        else:
            side = 8
            col_max = max(280, ws_w - 2 * side)
        return min(col_max, max(280, ws_w - 2 * side - 12))

    def _update_responsive_chrome(self):
        """Adapt header chips, fluid chat/composer column, card widths, and hint to workspace width."""
        ws_w = self._workspace_width()
        self.lbl_status_chip.setVisible(ws_w >= 580)
        if hasattr(self, "btn_relic_hud") and self.btn_relic_hud:
            self.btn_relic_hud.setVisible(ws_w >= 540)

        if ws_w >= 1400:
            side, col_max = 32, METRICS.column_max_width
        elif ws_w >= 960:
            side, col_max = 24, METRICS.column_max_width
        elif ws_w >= 680:
            side, col_max = 16, max(320, ws_w - 32)
        else:
            side, col_max = 8, max(280, ws_w - 16)

        if hasattr(self, "_sc_layout") and self._sc_layout:
            self._sc_layout.setContentsMargins(side, 0, side, 0)
        if hasattr(self, "_composer_outer") and self._composer_outer:
            bot_m = 10 if self.height() < METRICS.compact_height_breakpoint else 16
            self._composer_outer.setContentsMargins(side, 4, side, bot_m)
        if hasattr(self, "chat_container") and self.chat_container:
            self.chat_container.setMaximumWidth(col_max)
        if hasattr(self, "composer_col") and self.composer_col:
            self.composer_col.setMaximumWidth(col_max)

        eff_col = min(col_max, max(280, ws_w - 2 * side - 12))
        if hasattr(self, "chat_layout") and self.chat_layout:
            for i in range(self.chat_layout.count()):
                item = self.chat_layout.itemAt(i)
                w_obj = item.widget() if item else None
                if isinstance(w_obj, ChatMessageCard):
                    w_obj.sync_column_width(eff_col)

        self._apply_composer_state()

    def _open_drawer(self):
        self._drawer_open = True
        w = self.width()
        drawer_w = min(280, max(240, w - 40))
        if self.central_layout.indexOf(self.left_panel) != -1:
            self.central_layout.removeWidget(self.left_panel)
            self.left_panel.setParent(self)

        self._apply_sidebar_density(self.height() < METRICS.compact_height_breakpoint)
        self.left_panel.setMaximumWidth(drawer_w)
        self.left_panel.setMinimumWidth(drawer_w)
        self.motion.animate_drawer_open(self.left_panel, self.drawer_backdrop, drawer_w, self.height())

    def _close_drawer(self):
        self._drawer_open = False
        if self.width() < METRICS.drawer_breakpoint:
            self.motion.animate_drawer_close(self.left_panel, self.drawer_backdrop)
        else:
            self.motion.stop("drawer")
            self.drawer_backdrop.hide()

    def _update_backdrop_geometry(self):
        if hasattr(self, "drawer_backdrop") and self.drawer_backdrop:
            self.drawer_backdrop.setGeometry(0, 0, self.width(), self.height())
            self.drawer_backdrop.raise_()
            if hasattr(self, "left_panel") and self.left_panel and self.left_panel.isVisible():
                self.left_panel.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.width()
        h = self.height()
        is_narrow = w < 660
        is_compact_h = h < METRICS.compact_height_breakpoint

        # Header & composer adaptation based on true workspace width (works cleanly at 480px..1920px)
        self._update_responsive_chrome()

        # Tighten composer slightly on very short tiles (360px height)
        self.input_field.setPlaceholderText(
            "Написать Капри..."
            if is_narrow
            else "Написать Капри...   / для команд"
        )
        if is_compact_h:
            self.input_field.set_height_bounds(32, 72)
            self.composer_capsule_layout.setContentsMargins(10, 6, 8, 6)
            for b in (self.btn_attach, self.btn_vision, self.btn_commands, self.btn_mic):
                b.setFixedSize(30, 30)
            self.btn_send.setFixedSize(38, 30)
        else:
            self.input_field.set_height_bounds(34, 136)
            self.composer_capsule_layout.setContentsMargins(14, 10, 12, 10)
            for b in (self.btn_attach, self.btn_vision, self.btn_commands, self.btn_mic):
                b.setFixedSize(34, 34)
            self.btn_send.setFixedSize(44, 34)
        if self._is_generating():
            self._set_send_button_stop_state()
        else:
            self._reset_send_button()

        if hasattr(self, "welcome_canvas") and self.welcome_canvas:
            self.welcome_canvas.set_compact_mode(compact_height=is_compact_h, narrow_width=is_narrow)
            if self.welcome_canvas.isVisible() and hasattr(self, "scroll_area") and self.scroll_area:
                vp_h = self.scroll_area.viewport().height() or max(200, h - METRICS.header_height - 110)
                self.welcome_canvas.sync_viewport_height(vp_h)

        self._apply_sidebar_density(is_compact_h)

        # Docked vs Overlay Drawer breakpoint (1000 px)
        if w >= METRICS.drawer_breakpoint:
            self.motion.stop("drawer")
            self._drawer_open = False
            self.drawer_backdrop.hide()
            if self.central_layout.indexOf(self.left_panel) == -1:
                self.central_layout.insertWidget(0, self.left_panel)
            self.left_panel.setMaximumWidth(METRICS.sidebar_default)
            self.left_panel.setMinimumWidth(METRICS.sidebar_min)
            if self._sidebar_expanded:
                self.left_panel.show()
            else:
                self.left_panel.hide()
        else:
            if self.central_layout.indexOf(self.left_panel) != -1:
                self.central_layout.removeWidget(self.left_panel)
                self.left_panel.setParent(self)
            if self._drawer_open:
                self.motion.stop("drawer")
                drawer_w = min(280, max(240, w - 40))
                self.left_panel.setGeometry(0, 0, drawer_w, self.height())
                self.left_panel.setMaximumWidth(drawer_w)
                self.left_panel.setMinimumWidth(drawer_w)
                self._update_backdrop_geometry()
                self.left_panel.show()
                self.left_panel.raise_()
            else:
                self.left_panel.hide()
                self.drawer_backdrop.hide()

    def _on_search_session_text_changed(self, text: str):
        self._refresh_sidebar_sessions(search_query=text)

    def _refresh_sidebar_sessions(self, search_query: Optional[str] = None):
        """Refresh grouped sidebar session list with full SQLite search."""
        if not hasattr(self, "session_list_widget") or not self.session_list_widget:
            return
        if not hasattr(self, "agent") or not self.agent or not hasattr(self.agent, "session_manager"):
            return
        self.session_list_widget.blockSignals(True)
        self.session_list_widget.clear()
        try:
            if search_query is None:
                search_query = self.search_session_input.text() if self.search_session_input else ""
            query = search_query.strip()

            if query:
                if hasattr(self.agent.session_manager, "search_sessions"):
                    sessions = self.agent.session_manager.search_sessions(query, limit=60)
                else:
                    sessions = [
                        s
                        for s in self.agent.session_manager.list_sessions(limit=500)
                        if query.lower() in (s.title or "").lower() or query.lower() in (s.preview or "").lower()
                    ]
            else:
                sessions = self.agent.session_manager.list_sessions(limit=60)

            current_id = getattr(self.agent, "current_session_id", None)

            if not sessions:
                empty_item = QListWidgetItem("Ничего не найдено" if query else "Нет сохранённых диалогов")
                empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
                empty_item.setForeground(QColor(COLORS.text_muted))
                self.session_list_widget.addItem(empty_item)
                return

            now = time.time()
            local_now = time.localtime(now)
            today_start = time.mktime((local_now.tm_year, local_now.tm_mon, local_now.tm_mday, 0, 0, 0, 0, 0, -1))
            seven_days_ago = today_start - (6 * 86400)

            groups = {
                "СЕГОДНЯ": [],
                "ПОСЛЕДНИЕ 7 ДНЕЙ": [],
                "РАНЕЕ": [],
            }

            for s in sessions:
                ts = s.updated_at or s.created_at or 0
                if ts >= today_start:
                    groups["СЕГОДНЯ"].append(s)
                elif ts >= seven_days_ago:
                    groups["ПОСЛЕДНИЕ 7 ДНЕЙ"].append(s)
                else:
                    groups["РАНЕЕ"].append(s)

            active_item = None
            for group_name, group_sessions in groups.items():
                if not group_sessions:
                    continue

                header_item = QListWidgetItem(group_name)
                header_item.setFlags(Qt.ItemFlag.NoItemFlags)
                header_item.setForeground(QColor(COLORS.text_muted))
                hdr_font = header_item.font()
                hdr_font.setBold(True)
                hdr_font.setPointSize(8)
                header_item.setFont(hdr_font)
                header_item.setData(Qt.ItemDataRole.UserRole, None)
                self.session_list_widget.addItem(header_item)

                for s in group_sessions:
                    title = s.title if s.title else "Диалог"
                    is_active = (s.id == current_id)
                    item = QListWidgetItem(
                        get_icon("chat", COLORS.text_primary if is_active else COLORS.text_muted, 13),
                        title,
                    )
                    item.setData(Qt.ItemDataRole.UserRole, s.id)
                    item.setData(Qt.ItemDataRole.UserRole + 1, s.title)
                    try:
                        created_str = (
                            time.strftime("%d.%m %H:%M", time.localtime(s.created_at))
                            if isinstance(s.created_at, (int, float))
                            else str(s.created_at)[:16]
                        )
                    except Exception:
                        created_str = str(s.created_at)
                    item.setToolTip(
                        f"{s.title} ({_format_rounds_ru(s.turn_count)})\n"
                        f"Создано: {created_str}\nПКМ или F2/Del: Переименовать / Удалить"
                    )
                    self.session_list_widget.addItem(item)
                    if s.id == current_id:
                        active_item = item

            if active_item:
                self.session_list_widget.setCurrentItem(active_item)
        except Exception as e:
            logger.warning(f"Failed to refresh sidebar sessions: {e}")
        finally:
            self.session_list_widget.blockSignals(False)

    def _on_sidebar_session_clicked(self, item: QListWidgetItem):
        if not item:
            return
        sess_id = item.data(Qt.ItemDataRole.UserRole)
        if not sess_id:
            return
        if self.agent and sess_id != self.agent.current_session_id:
            play_ui_sound("ui_click")
            self._load_session_into_ui(sess_id)
        if self._drawer_open or self.width() < METRICS.drawer_breakpoint:
            self._close_drawer()

    def _rename_session_by_id(self, sess_id: str, current_title: str):
        new_title, ok = QInputDialog.getText(
            self,
            "Переименовать диалог",
            "Новое название диалога:",
            QLineEdit.EchoMode.Normal,
            current_title,
        )
        if ok and new_title.strip():
            clean_title = new_title.strip()
            self.agent.session_manager.rename_session(sess_id, clean_title)
            if sess_id == getattr(self.agent, "current_session_id", None):
                self.lbl_active_session_title.setText(clean_title)
            self._refresh_sidebar_sessions()

    def _delete_session_by_id(self, sess_id: str, sess_title: str):
        box = QMessageBox(
            QMessageBox.Icon.Question,
            "Удаление диалога",
            f"Удалить диалог «{sess_title}» из списка?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setStyleSheet(message_box_qss())
        if box.exec() == QMessageBox.StandardButton.Yes:
            self.agent.session_manager.soft_delete_session(sess_id)
            current_id = getattr(self.agent, "current_session_id", None)
            if sess_id == current_id:
                remaining = self.agent.session_manager.list_sessions(limit=1)
                if remaining:
                    self._load_session_into_ui(remaining[0].id)
                else:
                    self._start_new_session(auto=True)
            else:
                self._refresh_sidebar_sessions()

    def _on_session_context_menu(self, pos: QPoint):
        item = self.session_list_widget.itemAt(pos)
        if not item:
            return
        sess_id = item.data(Qt.ItemDataRole.UserRole)
        if not sess_id:
            return
        sess_title = item.data(Qt.ItemDataRole.UserRole + 1) or item.text().strip()

        menu = QMenu(self)
        menu.setStyleSheet(menu_qss())
        act_open = menu.addAction(get_icon("sessions", COLORS.text_secondary, 14), "Открыть")
        act_rename = menu.addAction(get_icon("edit", COLORS.text_secondary, 14), "Переименовать")
        menu.addSeparator()
        act_delete = menu.addAction(get_icon("trash", COLORS.coral, 14), "Удалить")

        chosen = menu.exec(self.session_list_widget.viewport().mapToGlobal(pos))
        if not chosen:
            return

        if chosen == act_open:
            if sess_id != self.agent.current_session_id:
                self._load_session_into_ui(sess_id)
            if self._drawer_open or self.width() < METRICS.drawer_breakpoint:
                self._close_drawer()
        elif chosen == act_rename:
            self._rename_session_by_id(sess_id, sess_title)
        elif chosen == act_delete:
            self._delete_session_by_id(sess_id, sess_title)

    def _show_sessions_dialog(self):
        play_ui_sound("ui_click")
        dlg = CapriSessionsDialog(self)
        dlg.exec()

    def _show_memory_workbench(self):
        play_ui_sound("ui_click")
        dlg = CapriMemoryWorkbenchDialog(self)
        dlg.exec()

    def _start_new_session(self, title: Optional[str] = None, auto: bool = False):
        if not auto:
            play_ui_sound("ui_click")
        sess = self.agent.start_new_session(title)
        self._clear_chat_cards()

        sess_title = sess.title if sess else "Новый диалог"
        self.lbl_active_session_title.setText(sess_title)
        self.lbl_turn_counter.setText(_format_rounds_ru(0))

        self._refresh_welcome_canvas(context="new_session")
        self._update_welcome_visibility()
        self.refresh_stats()
        self._refresh_sidebar_sessions()
        if self._drawer_open or self.width() < METRICS.drawer_breakpoint:
            self._close_drawer()

    def _load_session_into_ui(self, session_id: str):
        sess = self.agent.session_manager.get_session(session_id)
        if not sess:
            self._append_message("Система", "Сессия не найдена в базе.")
            return

        success = self.agent.resume_session(session_id)
        if not success:
            self._append_message("Система", f"Не удалось восстановить сессию {session_id[:8]}.")
            return

        self._clear_chat_cards()
        turns = self.agent.session_manager.get_turns(session_id, limit=100)
        self._populate_turns_ui(turns)

        self.lbl_active_session_title.setText(sess.title)
        self.lbl_turn_counter.setText(_format_rounds_ru(len(turns)))

        if not turns:
            self._refresh_welcome_canvas(context="new_session")
        self._update_welcome_visibility()
        self.refresh_stats()
        self._refresh_sidebar_sessions()
        if self._drawer_open or self.width() < METRICS.drawer_breakpoint:
            self._close_drawer()

    def _populate_turns_ui(self, turns: list):
        """Render stored session turns immediately without animating restored history."""
        user_name = getattr(self.config.companion, "user_name", "User") or "User"
        for turn in turns:
            try:
                t_ts = (
                    time.strftime("%H:%M", time.localtime(turn.created_at))
                    if isinstance(getattr(turn, "created_at", None), (int, float)) and turn.created_at > 0
                    else ""
                )
            except Exception:
                t_ts = ""
            is_proactive_turn = (
                getattr(turn, "status", "") == "proactive"
                or (turn.user_text or "").strip().startswith("[INITIATIVE //")
            )
            if turn.user_text and not is_proactive_turn:
                user_card = ChatMessageCard(
                    user_name,
                    turn.user_text,
                    turn_id=turn.id,
                    sequence=turn.sequence,
                    parent=self.chat_container,
                    motion=self.motion,
                    timestamp=t_ts,
                )
                user_card.sync_column_width(self._current_column_width())
                user_card.edit_requested.connect(self._on_card_edit_clicked)
                user_card.delete_requested.connect(self._on_card_delete_clicked)
                self.chat_layout.insertWidget(self.chat_layout.count() - 1, user_card)
            if turn.assistant_text or turn.thought or turn.tools:
                asst_card = ChatMessageCard(
                    "Капри",
                    turn.assistant_text or "",
                    thought=turn.thought or "",
                    tools_info=None,
                    turn_id=turn.id,
                    sequence=turn.sequence,
                    parent=self.chat_container,
                    is_proactive=is_proactive_turn,
                    motion=self.motion,
                    timestamp=t_ts,
                )
                asst_card.sync_column_width(self._current_column_width())
                if turn.tools:
                    for t in turn.tools:
                        if isinstance(t, dict):
                            fn_name = t.get("name", "tool")
                            asst_card.add_tool_info(f"Запуск: {fn_name}", kind="start")
                            res_val = t.get("result")
                            if res_val is not None and str(res_val).strip():
                                asst_card.add_tool_info(str(res_val), kind="result")
                        elif isinstance(t, str):
                            asst_card.add_tool_info(t, kind="start")
                asst_card.delete_requested.connect(self._on_card_delete_clicked)
                self.chat_layout.insertWidget(self.chat_layout.count() - 1, asst_card)
        self._update_welcome_visibility()
        self._scroll_to_bottom()

    def _loco_come(self):
        play_ui_sound("ui_click")
        self._ensure_avatar_running()
        notify_avatar_locomotion("come")
        self._append_message("Система", "Spatial Presence: аватар вызван к активному окну.")

    def _loco_home(self):
        play_ui_sound("ui_click")
        self._ensure_avatar_running()
        notify_avatar_locomotion("home")
        self._append_message("Система", "Spatial Presence: аватар возвращается на базу.")

    def _show_changelog(self):
        play_ui_sound("ui_click")
        dlg = CapriChangelogDialog(self)
        dlg.exec()

    def _show_provider_settings(self):
        play_ui_sound("ui_click")
        dlg = CapriProviderSettingsDialog(self.config, self)
        if dlg.exec():
            self._stop_generation()
            self._set_reduced_motion(bool(self.config.ui.reduced_motion), persist=False)
            avatar_on = bool(getattr(self.config.ui, "avatar_enabled", True))
            if hasattr(self, "brand_mark") and self.brand_mark:
                self.brand_mark.set_avatar_enabled(avatar_on)
            if avatar_on:
                self._ensure_avatar_running()
                send_avatar_control({
                    "type": "config_sync",
                    "auto_avatar_poses": bool(getattr(self.config.ui, "auto_avatar_poses", True)),
                    "game_mode_enabled": bool(getattr(self.config.ui, "game_mode_enabled", True)),
                    "avatar_reactions_enabled": bool(getattr(self.config.ui, "avatar_reactions_enabled", True)),
                    "companion_mode": str(getattr(self.config.companion, "mode", "normal") or "normal"),
                    "user_name": str(getattr(self.config.companion, "user_name", "User") or "User"),
                })
            elif self.avatar_proc and self.avatar_proc.poll() is None:
                try:
                    self.avatar_proc.terminate()
                except Exception:
                    pass
                self.avatar_proc = None
            self.agent.reload_config(self.config)
            self._barge_in_stt = None
            # Apply Wake-Word toggle live without restart
            want_wake = bool(getattr(self.config.voice, "wake_word_enabled", False))
            if want_wake and not (self.wake_listener and self.wake_listener.running):
                self._start_wake_listener()
            elif not want_wake and self.wake_listener:
                try:
                    self.wake_listener.stop()
                except Exception:
                    pass
                self.wake_listener = None
            self._update_sys_info()
            prov_name = (self.config.llm.provider or "GROQ").upper()
            tts_eng = (getattr(self.config.voice, "tts_engine", "fish") or "fish").upper()
            stt_eng = (getattr(self.config.voice, "stt_mode", "groq") or "groq").upper()
            vis_eng = (getattr(self.config.vision, "provider", "gemini") or "gemini").upper() if getattr(self.config.vision, "enabled", True) else "OFF"
            u_name = getattr(self.config.companion, "user_name", "User") or "User"
            self.btn_provider.setText(prov_name)
            self._refresh_welcome_canvas(context="startup")
            play_ui_sound("ui_click")
            self._append_message(
                "Капри",
                f"Настройки применены. Ядро: **{prov_name}** (`{self.config.llm.model}`) · "
                f"TTS: `{tts_eng}` · STT: `{stt_eng}` · Vision: `{vis_eng}`. Готова к работе, {u_name}.",
                thought="Обновление параметров LLM, голоса, зрения и API-ключей",
            )

    def _init_timers(self):
        self.stats_timer = QTimer(self)
        self.stats_timer.timeout.connect(self.refresh_stats)
        self.stats_timer.start(2500)

        self.initiative_timer = QTimer(self)
        self.initiative_timer.timeout.connect(self._check_initiative_tick)
        self.initiative_timer.start(15000)

    def _sync_game_mode_state(self) -> bool:
        """Evaluate Game Mode / Power Saver and update header status indicator."""
        from ..core.presence import detect_game_mode

        override = getattr(self, "_game_mode_override", None)
        enabled = bool(getattr(self.config.ui, "game_mode_enabled", True))
        if override is not None:
            gm = detect_game_mode(manual_override=override)
            active = gm.active
        elif enabled and os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            gm = detect_game_mode(manual_override=None)
            active = gm.active
        else:
            active = False
        self._game_mode_active = active

        if hasattr(self, "lbl_status_chip") and self.lbl_status_chip:
            if active:
                self.lbl_status_chip.setText("● Game Mode")
                self.lbl_status_chip.setStyleSheet(
                    f"color: {COLORS.amber}; font-family: {TYPE.mono}; font-size: 11px; font-weight: 600;"
                )
            else:
                self.lbl_status_chip.setText("● Online")
                self.lbl_status_chip.setStyleSheet(
                    f"color: {COLORS.signal_cyan}; font-family: {TYPE.mono}; font-size: 11px; font-weight: 600; padding: 0px 4px;"
                )
        return active

    def _check_initiative_tick(self):
        if not hasattr(self, "agent") or not self.agent:
            return
        gm_active = self._sync_game_mode_state()
        if not getattr(self.config.companion, "proactive_enabled", True):
            return
        if self._is_generating() or self._is_recording_voice():
            return
        init_engine = getattr(self.agent, "initiative_engine", None)
        if not init_engine:
            return
        ctx_store = getattr(self.agent, "context_store", None)
        emotions = getattr(self.agent, "emotions", None)
        mode = getattr(self.config.companion, "mode", "normal")
        u_name = (getattr(self.config.companion, "user_name", "") or "").strip() or "User"
        rec = init_engine.evaluate(
            companion_mode=mode,
            context_store=ctx_store,
            emotions=emotions,
            game_mode_active=gm_active,
            user_name=u_name,
        )
        if rec:
            self._handle_initiative_event(rec)

    def _handle_initiative_event(self, rec):
        thought = f"[INITIATIVE // {rec.kind.upper()}]"
        card = self._append_message("Капри", rec.content, thought=thought, is_proactive=True)
        play_ui_sound("agent_ready")

        if rec.kind == "focus_break":
            notify_avatar("concerned")
        elif rec.kind == "task_nudge":
            notify_avatar("smug")
        else:
            notify_avatar("idle")

        try:
            turn = self.agent.session_manager.add_turn(
                session_id=self.agent.current_session_id,
                user_text=f"[INITIATIVE // {rec.kind}]",
                assistant_text=rec.content,
                thought=thought,
                tools=[],
                status="proactive",
            )
            if card and turn:
                card.set_turn_metadata(turn.id, turn.sequence)
                card.delete_requested.connect(self._on_card_delete_clicked)
        except Exception as ex:
            logger.warning(f"Failed to record initiative turn in session: {ex}")

        if (
            hasattr(self.config, "voice")
            and self.config.voice.tts_enabled
            and getattr(self.config.companion, "mode", "normal") not in ("quiet", "focus")
        ):
            def _speak_async(text: str):
                try:
                    if hasattr(self.agent, "tts") and self.agent.tts:
                        asyncio.run(self.agent.tts.speak(text))
                except Exception as e:
                    logger.warning(f"Failed to speak proactive initiative: {e}")

            threading.Thread(target=_speak_async, args=(rec.content,), daemon=True).start()

    def refresh_stats(self):
        self.emotions.load()
        trust = self.emotions.trust
        irrit = self.emotions.irritation
        mood = self.emotions.mood

        mood_ru = {
            "playful": "Игривое / Озорное",
            "warm": "Тёплое / Лояльное",
            "sarcastic": "Саркастичное",
            "annoyed": "Раздражённое",
            "neutral": "Спокойное",
        }.get(mood, mood)

        sync_pct = max(10, min(100, int(((trust + 100) / 200.0) * 100)))
        self.bar_engram.setValue(sync_pct)
        self.lbl_engram_pct.setText(f"{sync_pct}%")

        self.lbl_stage.setText(self.emotions.relationship_stage_label)
        self.lbl_mood.setText(f"Настроение: {mood_ru}")
        self.lbl_trust.setText(f"Доверие {trust:+d}")
        self.lbl_irrit.setText(f"Раздражение {irrit}%")
        self.lbl_irrit.setStyleSheet(
            f"color: {COLORS.amber if irrit > 25 else COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10px;"
        )

        if hasattr(self, "agent") and self.agent and hasattr(self.agent, "session_manager"):
            try:
                cur_sess = self.agent.session_manager.get_session(self.agent.current_session_id)
                if cur_sess:
                    self.lbl_active_session_title.setText(cur_sess.title)
                    self.lbl_turn_counter.setText(_format_rounds_ru(cur_sess.turn_count))
                    if hasattr(self, "lbl_metric_context"):
                        self.lbl_metric_context.setText(str(cur_sess.turn_count))
            except Exception:
                pass
            try:
                mem_store = getattr(self.agent, "memory_store", None)
                if mem_store is not None and hasattr(self, "lbl_metric_memory"):
                    saved_cnt = len(mem_store.entries)
                    pend_cnt = len(mem_store.list_pending())
                    label = f"{saved_cnt} фактов (+{pend_cnt})" if pend_cnt else f"{saved_cnt} фактов"
                    self.lbl_metric_memory.setText(label)
            except Exception:
                pass
        if hasattr(self, "lbl_metric_tools"):
            try:
                self.lbl_metric_tools.setText(f"{len(registry.get_schemas())} активны")
            except Exception:
                pass

    def _update_sys_info(self):
        provider = (self.config.llm.provider or "GROQ").upper()
        tts_st = "ON" if self.config.voice.tts_enabled else "OFF"
        tts_eng = (self.config.voice.tts_engine or "Fish Audio").capitalize()
        if "fish" in tts_eng.lower():
            tts_eng = "Fish Audio"
        try:
            from ..core.compositor import detect_compositor
            comp_kind = detect_compositor()
            comp_label = {"niri": "Niri Wayland", "hyprland": "Hyprland Wayland"}.get(comp_kind, "Wayland")
        except Exception:
            comp_label = "Wayland"
        tools_cnt = len(registry.get_schemas())
        self.lbl_sys_info.setText(
            f"ЯДРО: {provider}\n"
            f"МОДЕЛЬ: {self.config.llm.model}\n"
            f"ГОЛОС: {tts_eng} [{tts_st}]\n"
            f"ОБСЕРВЕР: {comp_label}\n"
            f"ТУЛЫ: {tools_cnt} активных"
        )

    def _scroll_to_bottom(self):
        QTimer.singleShot(
            30,
            lambda: self.scroll_area.verticalScrollBar().setValue(
                self.scroll_area.verticalScrollBar().maximum()
            ),
        )

    def _append_message(
        self,
        sender: str,
        text: str,
        thought: str = "",
        tools: Optional[list[str]] = None,
        turn_id: str = "",
        sequence: int = 0,
        animate: bool = True,
        is_proactive: bool = False,
    ) -> Optional[ChatMessageCard]:
        if not (text or "").strip() and not (thought or "").strip() and not tools:
            return None
        card = ChatMessageCard(
            sender,
            text,
            thought=thought,
            tools_info=tools,
            turn_id=turn_id,
            sequence=sequence,
            parent=self.chat_container,
            is_proactive=is_proactive,
            motion=self.motion,
        )
        card.sync_column_width(self._current_column_width())
        if turn_id:
            card.edit_requested.connect(self._on_card_edit_clicked)
            card.delete_requested.connect(self._on_card_delete_clicked)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, card)
        self._update_welcome_visibility()
        if animate:
            self.motion.fade_in(card, duration=MotionTokens.MESSAGE_MS)
        self._scroll_to_bottom()
        return card

    def _is_generating(self) -> bool:
        return bool(hasattr(self, "worker") and self.worker and self.worker.isRunning())

    def _is_recording_voice(self) -> bool:
        v_active = bool(hasattr(self, "voice_thread") and self.voice_thread and self.voice_thread.isRunning())
        w_active = bool(
            hasattr(self, "wake_listener")
            and self.wake_listener
            and getattr(self.wake_listener, "is_recording_command", False)
        )
        return v_active or w_active

    def _on_send_button_clicked(self):
        if self._is_generating():
            self._stop_generation()
        else:
            self._send_message()

    def _set_send_button_stop_state(self):
        if hasattr(self, "btn_send"):
            self.btn_send.setText("")
            self.btn_send.setIcon(get_icon("stop", "#FFFFFF", 14))
            self.btn_send.setEnabled(True)
            self.btn_send.setToolTip("Остановить генерацию и прервать речь Капри (Esc)")
            self.btn_send.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS.coral};
                    color: #FFFFFF;
                    border: 1px solid {COLORS.coral_hover};
                    border-radius: 10px;
                    padding: 0px;
                }}
                QPushButton:hover {{
                    background: {COLORS.coral_hover};
                }}
            """)

    def _reset_send_button(self):
        if hasattr(self, "btn_send"):
            self.btn_send.setText("")
            self.btn_send.setIcon(get_icon("send", "#FFFFFF", 15, stroke_width=2.0))
            self.btn_send.setEnabled(True)
            self.btn_send.setToolTip("Отправить сообщение (Enter)")
            self.btn_send.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(
                        x1:0, y1:0, x2:0, y2:1,
                        stop:0 #EF385E,
                        stop:1 #CE2247
                    );
                    color: #FFFFFF;
                    border: 1px solid {COLORS.crimson};
                    border-radius: 10px;
                    padding: 0px;
                }}
                QPushButton:hover {{
                    background: {COLORS.crimson_hover};
                    border-color: {COLORS.crimson_hover};
                }}
                QPushButton:pressed {{
                    background: {COLORS.crimson_pressed};
                }}
                QPushButton:disabled {{
                    background: {COLORS.surface_raised};
                    color: {COLORS.text_muted};
                    border-color: {COLORS.border_hairline};
                }}
            """)

    def _stop_generation(self, silent: bool = False):
        self._stream_flush_timer.stop()
        self._stream_pending_text = None
        play_ui_sound("ui_glitch")
        if hasattr(self, "voice_thread") and self.voice_thread and self.voice_thread.isRunning():
            self.voice_thread.cancel_recording()
        if hasattr(self, "wake_listener") and self.wake_listener and getattr(self.wake_listener, "is_recording_command", False):
            self.wake_listener.cancel_active_recording()
        self._reset_mic_button()

        if hasattr(self, "worker") and self.worker:
            try:
                self.worker.thought_signal.disconnect()
                self.worker.tool_signal.disconnect()
                self.worker.chunk_signal.disconnect()
                self.worker.speaking_signal.disconnect()
                self.worker.text_ready_signal.disconnect()
                self.worker.memory_candidates_signal.disconnect()
                self.worker.finished_signal.disconnect()
                self.worker.cancelled_signal.disconnect()
                self.worker.error_signal.disconnect()
            except Exception:
                pass
            self.worker.cancel()
            self.worker.wait(350)
        if hasattr(self, "agent") and self.agent:
            self.agent.abort()
        self._reset_send_button()
        self.visualizer.set_state("idle")
        notify_avatar("idle")
        try:
            subprocess.run(["pkill", "-9", "-f", "mpv.*tmp.*"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "-f", "mpv.*--really-quiet.*"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "-f", "mpv.*capri"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "-f", "paplay.*capri"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "-f", "pw-play.*tmp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if hasattr(self, "wake_listener") and self.wake_listener:
            self.wake_listener.resume()
        if hasattr(self, "current_response_card") and self.current_response_card:
            if not silent:
                self.current_response_card.set_text("[Генерация ответа и речь Капри остановлены пользователем]")
            else:
                self.current_response_card.set_text("[Прервано новым сообщением]")
        elif not silent:
            self._append_message("Система", "Генерация ответа и речь Капри остановлены пользователем.")
        self.current_response_card = None

    def _set_mic_button_recording_state(self):
        self._composer_recording = True
        self._apply_composer_state()
        if hasattr(self, "btn_mic"):
            self.btn_mic.setText("")
            self.btn_mic.setIcon(get_icon("stop", "#16070D", 14))
            self.btn_mic.setToolTip("Идёт запись речи. Нажми для завершения (Esc для отмены)")
            self.btn_mic.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS.coral};
                    color: #16070D;
                    border: none;
                    border-radius: 10px;
                    padding: 0px;
                }}
                QPushButton:hover {{
                    background: {COLORS.coral_hover};
                }}
            """)

    def _stop_voice_input(self):
        play_ui_sound("ui_click")
        if hasattr(self, "voice_thread") and self.voice_thread and self.voice_thread.isRunning():
            self._composer_recording = False
            self._apply_composer_state()
            self.visualizer.set_state("thinking")
            notify_avatar("thinking", "Распознаю...")
            self.voice_thread.stop_recording()
        elif hasattr(self, "wake_listener") and self.wake_listener and getattr(self.wake_listener, "is_recording_command", False):
            self.wake_listener.cancel_active_recording()
            self._reset_mic_button()
            self.visualizer.set_state("idle")
            notify_avatar("idle")
        else:
            self._reset_mic_button()
            self.visualizer.set_state("idle")
            notify_avatar("idle")

    def _cancel_voice_input(self):
        play_ui_sound("ui_glitch")
        if hasattr(self, "voice_thread") and self.voice_thread and self.voice_thread.isRunning():
            self.voice_thread.cancel_recording()
        if hasattr(self, "wake_listener") and self.wake_listener and getattr(self.wake_listener, "is_recording_command", False):
            self.wake_listener.cancel_active_recording()
        self._reset_mic_button()
        self.visualizer.set_state("idle")
        notify_avatar("idle")
        self._append_message("Система", "Голосовой ввод отменён.")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_M and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._toggle_voice_input()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            if getattr(self, "_drawer_open", False):
                self._close_drawer()
                event.accept()
                return
            if self._is_recording_voice():
                self._cancel_voice_input()
                event.accept()
                return
            if self._is_generating():
                self._stop_generation()
                event.accept()
                return
        super().keyPressEvent(event)

    def _send_message(self):
        if self._is_generating():
            self._stop_generation(silent=True)

        text = self.input_field.text().strip()
        pending_attachments = self.attachment_strip.attachments() if hasattr(self, "attachment_strip") else []
        if not text and not pending_attachments:
            if not self._is_generating():
                self.visualizer.set_state("idle")
                notify_avatar("idle")
            return

        self.input_field.clear()
        if hasattr(self, "attachment_strip") and self.attachment_strip:
            self.attachment_strip.clear()
        play_ui_sound("ui_click")
        if hasattr(self, "agent") and hasattr(self.agent, "initiative_engine") and self.agent.initiative_engine:
            self.agent.initiative_engine.record_user_activity()
        user_name = getattr(self.config.companion, "user_name", "User") or "User"
        from ..core.attachments import format_user_display_with_attachments
        display_text = format_user_display_with_attachments(text, pending_attachments)
        self.current_user_card = self._append_message(user_name, display_text)

        if text.startswith("/") and not pending_attachments:
            if not self._is_generating():
                self.visualizer.set_state("idle")
                notify_avatar("idle")
            self._handle_command(text)
            return

        # Spatial Presence quick phrases
        clean_prompt = text.lower().strip().rstrip(".!?,")
        come_phrases = (
            "капри, ко мне", "капри ко мне", "ко мне", "подойди", "иди сюда",
            "капри, иди сюда", "капри иди сюда", "капри подойди", "капри, подойди",
            "сюда", "подлети", "приползи", "к окну", "капри сюда", "быстро сюда",
        )
        stay_phrases = (
            "капри, стой тут", "капри стой тут", "стой тут", "стой здесь",
            "капри, стой здесь", "останься", "зафиксируйся", "капри стой", "стой на месте", "стой",
        )
        home_phrases = (
            "капри, на базу", "капри на базу", "на базу", "домой",
            "вернись в угол", "капри домой", "капри, домой", "в угол", "на место",
        )
        follow_phrases = ("следуй за мной", "иди за мной", "режим следования", "следуй")

        if clean_prompt in come_phrases:
            self._ensure_avatar_running()
            notify_avatar_locomotion("come")
        elif clean_prompt in stay_phrases:
            self._ensure_avatar_running()
            notify_avatar_locomotion("stay")
        elif clean_prompt in home_phrases:
            self._ensure_avatar_running()
            notify_avatar_locomotion("home")
        elif clean_prompt in follow_phrases:
            self._ensure_avatar_running()
            notify_avatar_locomotion("follow", enabled=True)

        self.visualizer.set_state("thinking")
        notify_avatar("thinking", "Обработка...")

        self.current_tools_log = []
        self.current_thought_text = ""
        self._response_has_content = False

        self.current_response_card = ChatMessageCard(
            "Капри",
            "Синхронизация нейросети...",
            thought="Анализирую входящий запрос...",
            parent=self.chat_container,
            motion=self.motion,
        )
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, self.current_response_card)
        self._update_welcome_visibility()
        self.motion.fade_in(self.current_response_card, duration=MotionTokens.MESSAGE_MS)
        self._scroll_to_bottom()

        self._set_send_button_stop_state()

        if hasattr(self, "wake_listener") and self.wake_listener:
            self.wake_listener.pause()

        if hasattr(self, "worker") and self.worker and self.worker.isRunning():
            try:
                self.worker.thought_signal.disconnect()
                self.worker.tool_signal.disconnect()
                self.worker.chunk_signal.disconnect()
                self.worker.speaking_signal.disconnect()
                self.worker.text_ready_signal.disconnect()
                self.worker.memory_candidates_signal.disconnect()
                self.worker.finished_signal.disconnect()
                self.worker.cancelled_signal.disconnect()
                self.worker.error_signal.disconnect()
            except Exception:
                pass
            self.worker.cancel()
            self.worker.wait(200)

        self.worker = AgentWorker(
            self.agent,
            text,
            attachments=pending_attachments,
            display_text=display_text,
        )
        _track_qthread(self.worker)
        self.worker.thought_signal.connect(self._on_thought)
        self.worker.tool_signal.connect(self._on_tool)
        self.worker.chunk_signal.connect(self._on_stream_chunk)
        self.worker.speaking_signal.connect(self._on_speaking_started)
        self.worker.text_ready_signal.connect(self._on_text_ready)
        self.worker.memory_candidates_signal.connect(self._on_memory_candidates)
        self.worker.finished_signal.connect(self._on_speech_finished)
        self.worker.cancelled_signal.connect(self._on_cancelled)
        self.worker.error_signal.connect(self._on_error)
        self.worker.approval_requested.connect(self._on_tool_approval_requested)
        self.worker.start()

    def _attach_file_paths(self, paths: list):
        from ..core.attachments import classify_attachment
        added = 0
        rejected = []
        for raw_p in paths:
            item = classify_attachment(raw_p)
            if item is not None:
                if self.attachment_strip.add_attachment(item):
                    added += 1
            else:
                rejected.append(Path(raw_p).name)
        if added:
            play_ui_sound("ui_click")
            self.input_field.setFocus()
        if rejected:
            self._append_message(
                "Система",
                f"Пропущены неподдерживаемые или слишком большие файлы: {', '.join(rejected)}.",
            )

    def _attach_pasted_image(self, image_obj):
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage, QPixmap
        from ..core.attachments import save_clipboard_image_bytes

        qimg = image_obj
        if isinstance(image_obj, QPixmap):
            qimg = image_obj.toImage()
        if not isinstance(qimg, QImage) or qimg.isNull():
            return

        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        ok = qimg.save(buf, "PNG")
        raw_bytes = bytes(buf.data())
        buf.close()
        if not ok or not raw_bytes:
            return

        from ..core.attachments import cleanup_temp_attachment
        try:
            att = save_clipboard_image_bytes(raw_bytes)
        except ValueError as e:
            self._append_message("Система", f"Изображение из буфера отклонено: {e}")
            return
        if self.attachment_strip.add_attachment(att):
            play_ui_sound("ui_click")
            self.input_field.setFocus()
        else:
            cleanup_temp_attachment(att)
            self._append_message("Система", "Достигнут лимит вложений (максимум 6 на сообщение).")

    def _pick_attachments_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Прикрепить файлы или изображения",
            str(Path.home()),
            "Изображения и код (*.png *.jpg *.jpeg *.webp *.py *.md *.txt *.json *.yaml *.yml *.toml *.sh *.log *.js *.ts *.kdl);;Все файлы (*)",
        )
        if files:
            self._attach_file_paths(files)

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
                    self._attach_file_paths(local_paths)
                    event.acceptProposedAction()
                    return
            if md.hasImage():
                img = md.imageData()
                if img is not None:
                    self._attach_pasted_image(img)
                    event.acceptProposedAction()
                    return
        super().dropEvent(event)

    def _on_stream_chunk(self, full_visible_text: str):
        if not full_visible_text or not full_visible_text.strip():
            return
        self._stream_pending_text = full_visible_text
        if not self._stream_flush_timer.isActive():
            self._stream_flush_timer.start(45)

    def _flush_stream_chunk(self):
        text = self._stream_pending_text
        self._stream_pending_text = None
        if not text or not hasattr(self, "current_response_card") or not self.current_response_card:
            return
        sb = self.scroll_area.verticalScrollBar()
        near_bottom = (sb.maximum() - sb.value()) <= 80
        self._response_has_content = True
        self.current_response_card.set_text(text)
        if near_bottom:
            self._scroll_to_bottom()

    def _on_tool_approval_requested(self, payload):
        req, ev, box = payload
        try:
            dlg = CapriToolApprovalDialog(req, self)
            box["approved"] = dlg.exec() == QDialog.DialogCode.Accepted
        except Exception:
            box["approved"] = False
        finally:
            ev.set()

    def _handle_command(self, text: str):
        parts = text.split()
        cmd = parts[0].lower()

        if cmd in ("/exit", "/quit"):
            self._quit_application()
            return

        if cmd in ("/clear", "/cls"):
            self._clear_dialog()
            return

        if cmd in ("/new", "/новый"):
            custom_title = " ".join(parts[1:]) if len(parts) > 1 else None
            self._start_new_session(custom_title)
            return

        if cmd in ("/resume", "/load", "/сессия", "/сессии", "/sessions", "/history"):
            if len(parts) > 1:
                target = parts[1].strip()
                if target == "last":
                    sessions = self.agent.session_manager.list_sessions(limit=5)
                    cands = [s for s in sessions if s.id != self.agent.current_session_id]
                    if cands:
                        self._load_session_into_ui(cands[0].id)
                        return
                    if sessions:
                        self._load_session_into_ui(sessions[0].id)
                        return
                    self._append_message("Система", "Нет доступных сохранённых сессий.")
                    return
                sessions = self.agent.session_manager.list_sessions(limit=100)
                matched = [s for s in sessions if s.id.startswith(target) or target.lower() in s.title.lower()]
                if matched:
                    self._load_session_into_ui(matched[0].id)
                    return
                self._append_message("Система", f"Сессия '{target}' не найдена в базе.")
                return
            self._show_sessions_dialog()
            return

        if cmd in ("/backup", "/бэкап"):
            backup_dir = get_user_config_dir() / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts_str = time.strftime("%Y%m%d_%H%M%S")
            target = backup_dir / f"sessions_backup_{ts_str}.db"
            if self.agent.session_manager.backup_to(target):
                self._append_message("Система", f"Резервная копия базы создана:\n{target}")
            else:
                self._append_message("Система", "Ошибка: сбой создания резервной копии базы.")
            return

        if cmd in ("/stop", "/abort", "/shh", "/тихо", "/стоп"):
            self._stop_generation()
            if self._is_recording_voice():
                self._cancel_voice_input()
            return

        if cmd in ("/changelog", "/updates", "/шейнджлог", "/ченджлог", "/версия"):
            self._show_changelog()
            return

        if cmd in ("/reset", "/wipe"):
            self._prompt_reset_capri()
            return

        if cmd == "/help":
            help_text = (
                "ДОСТУПНЫЕ КОМАНДЫ КИБЕРДЕКА:\n\n"
                "• /doctor — самодиагностика системы, конфига, SQLite и аудио\n"
                "• /bugreport [путь.md] — безопасный баг-репорт (без ключей и переписки)\n"
                "• /setup — мастер быстрой настройки (имя, провайдер, ключ, политика)\n"
                "• /policy [safe|ask|trusted] — политика безопасности инструментов\n"
                "• /profile [export|restore] — версионированный бэкап и восстановление профиля (.zip)\n"
                "• /new [название] — начать новую сессию диалога\n"
                "• /resume [id|last] — возобновить сохранённую сессию\n"
                "• /sessions — открыть архив сессий\n"
                "• /backup — создать резервную копию базы сессий\n"
                "• /mode [focus|normal|quiet|playful] — сменить режим поведения Капри\n"
                "• /model [120b|qwen|20b|deepseek] — переключить модель или показать список\n"
                "• /key [api_key] — показать или обновить API-ключ (.env)\n"
                "• /look [вопрос] — посмотреть на экран через Vision AI (или Ctrl+V скрин / Drag & Drop)\n"
                "• /reset [capri] — перезагрузка биочипа и сброс отношений\n"
                "• /np [next|prev|pause|play] — управление медиаплеером\n"
                "• /vol [громкость|mute] — громкость системы\n"
                "• /timer [5m|30s] [название] — быстрый таймер (/timers — список)\n"
                "• /top [cpu|mem] — топ ресурсоёмких процессов\n"
                "• /mood (/rel) — настроение, стадия связи и доверие\n"
                "• /context (/work) — рабочий контекст, задачи и решения\n"
                "• /initiative (/proactive) — настройки самостоятельной инициативы\n"
                "• /memory (/workbench) — Memory Workbench: память, /memory pending | approve <id> | reject <id>\n"
                "• /forget <ключ> — удалить факт из памяти\n"
                "• /focus [окно] / /windows — управление окнами Niri\n"
                "• /avatar [dance|glitch|playful|perch|music|sleep|wake|idle|toggle] — управление аватаром\n"
                "• /come, /stay, /follow [on|off], /home — Spatial Presence аватара\n"
                "• /status — телеметрия железа\n"
                "• /stop (Esc) — остановить генерацию и речь\n"
                "• /wake [on|off] — Wake-Word («Капри»)\n"
                "• /bargein [on|off] — Hands-Free перебивание речи Капри голосом\n"
                "• Горячие клавиши: Enter (отправить), Shift+Enter (перенос), Ctrl+V (скриншот), Ctrl+M (голос), Esc (стоп)\n"
                "• /tools, /clear, /privacy, /changelog, /exit"
            )
            self._append_message("Система", help_text)
            return

        if cmd in ("/mood", "/rel", "/relationship"):
            self.refresh_stats()
            trust = self.emotions.trust
            irrit = self.emotions.irritation
            mood = self.emotions.mood
            u_name = getattr(self.config.companion, "user_name", "User") or "User"
            mood_ru = {
                "playful": "Игривое / Озорное",
                "warm": "Тёплое / Лояльное",
                "sarcastic": "Саркастичное",
                "annoyed": "Раздражённое",
                "neutral": "Спокойное",
            }.get(mood, mood)
            events_str = ""
            if self.emotions.recent_events:
                events_str = "\nНедавние события:\n" + "\n".join([f"  • {e}" for e in self.emotions.recent_events[-3:]])
            milestones_str = ""
            if self.emotions.emotional_milestones:
                milestones_str = "\nКлючевые вехи:\n" + "\n".join(
                    [f"  ★ [{m.category}] {m.summary}" for m in self.emotions.emotional_milestones[-3:]]
                )
            absence = self.emotions.get_absence_context()
            card = (
                f"ЭМОЦИОНАЛЬНЫЙ СТАТУС И СВЯЗЬ КАПРИ:\n"
                f"  Стадия связи:    {self.emotions.relationship_stage_label}\n"
                f"  Настроение:      {mood_ru}\n"
                f"  Доверие к {u_name}: {trust:+d} / 100\n"
                f"  Раздражение:     {irrit}%\n"
                f"  Активность:      {absence['description']}{events_str}{milestones_str}"
            )
            self._append_message("Капри", card)
            return

        if cmd in ("/mode", "/focus_mode", "/chill"):
            sub = ""
            if cmd == "/chill":
                sub = "playful"
            elif len(parts) > 1:
                sub = parts[1].lower()

            if sub in ("focus", "normal", "quiet", "playful"):
                self.config.companion.mode = sub
                self.agent.system_prompt = get_system_prompt(self.config)
                if self.agent.history:
                    self.agent.history[0] = {"role": "system", "content": self.agent._build_full_prompt()}
                self._refresh_mode_button()
                save_config(self.config)
                send_avatar_control({"type": "config_sync", "companion_mode": sub})
                desc = {
                    "focus": "FOCUS: Полная тишина, лаконичные ответы без шуток и болтовни.",
                    "normal": "NORMAL: Сбалансированный режим напарника — помощь и умеренный юмор.",
                    "quiet": "QUIET: Тихий режим — реакции только визуальные, минимум шума.",
                    "playful": "PLAYFUL: Озорной и дерзкий режим — больше шуток, живые реакции.",
                }[sub]
                self._append_message("Система", f"Режим переключён: [{sub.upper()}]\n{desc}")
            else:
                cur = self.config.companion.mode.upper()
                self._append_message("Система", f"Текущий режим: [{cur}]. Доступно: /mode normal | focus | quiet | playful")
            return

        if cmd in ("/memory", "/mem", "/workbench", "/memwb"):
            mem_store = getattr(self.agent, "memory_store", None) or MemoryStore()
            sub = parts[1].lower() if len(parts) > 1 else ""

            if cmd in ("/workbench", "/memwb") or sub in ("workbench", "ui", "gui", "wb"):
                dlg = CapriMemoryWorkbenchDialog(self)
                dlg.exec()
                self._refresh_memory_candidate_bar()
                self.refresh_stats()
                return

            if sub in ("pending", "candidates", "queue"):
                pending = mem_store.list_pending()
                if not pending:
                    self._append_message("Система", "Очередь кандидатов памяти пуста.")
                else:
                    lines = [f"Кандидаты памяти на подтверждение ({len(pending)}):"]
                    for cand in pending:
                        ret_lbl = "временно 72ч" if cand.retention == "ephemeral" else "постоянно"
                        cat_lbl = CATEGORY_LABELS.get(cand.category, cand.category)
                        quote_part = f" · «{cand.source_quote}»" if cand.source_quote else ""
                        lines.append(f"  • {cand.id} [{cand.key}] ({cat_lbl}, {ret_lbl}): {cand.fact}{quote_part}")
                    lines.append("\nКоманды: /memory approve <id> | /memory reject <id|all>")
                    self._append_message("Система", "\n".join(lines))
                self._refresh_memory_candidate_bar()
            elif sub in ("approve", "accept", "confirm"):
                if len(parts) < 3:
                    self._append_message("Система", "Использование: /memory approve <id_или_ключ>")
                else:
                    target_id = parts[2]
                    entry = mem_store.approve_candidate(target_id)
                    if entry is not None:
                        ret_tag = " (временно 72ч)" if entry.retention == "ephemeral" else ""
                        self._append_message("Система", f"Подтверждено и сохранено: [{entry.key}]{ret_tag} -> '{entry.fact}'.")
                    else:
                        self._append_message("Система", f"Кандидат [{target_id}] не найден. Проверь /memory pending.")
                self._refresh_memory_candidate_bar()
                self.refresh_stats()
            elif sub in ("reject", "dismiss", "decline"):
                if len(parts) < 3:
                    self._append_message("Система", "Использование: /memory reject <id_или_ключ|all>")
                elif parts[2].lower() == "all":
                    cnt = mem_store.clear_pending()
                    self._append_message("Система", f"Отклонены все кандидаты памяти ({cnt}).")
                else:
                    target_id = parts[2]
                    if mem_store.reject_candidate(target_id):
                        self._append_message("Система", f"Кандидат [{target_id}] отклонён.")
                    else:
                        self._append_message("Система", f"Кандидат [{target_id}] не найден. Проверь /memory pending.")
                self._refresh_memory_candidate_bar()
                self.refresh_stats()
            elif sub == "clear":
                target_cat = parts[2].lower() if len(parts) > 2 else None
                deleted_cnt = mem_store.clear(category=target_cat)
                if target_cat:
                    self._append_message("Система", f"Память очищена: категория [{target_cat}] ({deleted_cnt} записей).")
                else:
                    self.emotions.clear_events()
                    self.agent.clear_history()
                    self._append_message("Система", f"Вся долговременная память очищена ({deleted_cnt} записей).")
                self.refresh_stats()
            elif sub == "reset":
                mem_store.clear()
                self.emotions.reset_emotions()
                self.agent.clear_history()
                self._append_message("Система", "Полный сброс памяти и эмоционального статуса Капри.")
                self.refresh_stats()
            elif sub == "add" and len(parts) > 3:
                key = parts[2]
                fact = " ".join(parts[3:])
                if memory_payload_contains_secret(key, fact):
                    self._append_message(
                        "Система",
                        "Отклонено: пароли, API-ключи, токены и другие секреты не сохраняются в память.",
                    )
                else:
                    entry = mem_store.add_or_update(key, fact, category="general", importance=3, source="user")
                    self._append_message("Система", f"Запомнила: [{entry.key}] -> '{entry.fact}' ({entry.category}).")
                    self.refresh_stats()
            elif sub == "edit" and len(parts) > 3:
                key = parts[2]
                new_fact = " ".join(parts[3:])
                if memory_payload_contains_secret(key, new_fact):
                    self._append_message(
                        "Система",
                        "Отклонено: пароли, API-ключи, токены и другие секреты не сохраняются в память.",
                    )
                    return
                clean_k = key.lower().strip()
                mem_store._sync_disk()
                if clean_k in mem_store.entries:
                    old_e = mem_store.entries[clean_k]
                    entry = mem_store.add_or_update(
                        clean_k,
                        new_fact,
                        category=old_e.category,
                        importance=old_e.importance,
                        confidence=old_e.confidence,
                        tags=old_e.tags,
                        source="user",
                    )
                    self._append_message(
                        "Система",
                        f"Обновлено: [{entry.key}] -> '{entry.fact}' (категория: {entry.category}, важность: {'★' * entry.importance}).",
                    )
                else:
                    self._append_message("Система", f"Ключ [{key}] не найден в памяти. Используй /memory add.")
            elif sub == "search" and len(parts) > 2:
                q = " ".join(parts[2:])
                results = mem_store.search(query=q)
                if results:
                    lines = [f"Результаты поиска памяти по '{q}' ({len(results)}):"]
                    for it in results:
                        stars = "★" * it.importance
                        lbl = CATEGORY_LABELS.get(it.category, it.category)
                        lines.append(f"  • [{it.key}] ({lbl}, {stars}): {it.fact}")
                    self._append_message("Система", "\n".join(lines))
                else:
                    self._append_message("Система", f"В памяти ничего не найдено по запросу '{q}'.")
            elif sub == "cat" and len(parts) > 2:
                cat_target = parts[2].lower().strip()
                results = mem_store.search(category=cat_target)
                if results:
                    lbl = CATEGORY_LABELS.get(cat_target, cat_target)
                    lines = [f"Память — категория [{lbl.upper()}] ({len(results)}):"]
                    for it in results:
                        stars = "★" * it.importance
                        lines.append(f"  • [{it.key}] ({stars}): {it.fact}")
                    self._append_message("Система", "\n".join(lines))
                else:
                    self._append_message("Система", f"Нет записей в категории [{cat_target}]. Доступно: {', '.join(CATEGORIES)}")
            elif sub == "export":
                backup_dir = get_user_config_dir() / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                target = backup_dir / f"memory_export_{int(time.time())}.json"
                try:
                    with open(target, "w", encoding="utf-8") as f:
                        json.dump(mem_store.export_dict(), f, ensure_ascii=False, indent=2)
                    self._append_message("Система", f"Память экспортирована: {target}")
                except Exception as ex:
                    self._append_message("Система", f"Ошибка экспорта памяти: {ex}")
            else:
                u_name = (getattr(self.config.companion, "user_name", "") or "").strip() or "User"
                card = mem_store.get_stats_card(user_name=u_name)
                self._append_message("Капри", card)
            return

        if cmd == "/forget":
            mem_store = getattr(self.agent, "memory_store", None) or MemoryStore()
            if len(parts) > 1:
                key = parts[1]
                if mem_store.delete(key):
                    self._append_message("Система", f"Факт [{key}] успешно удалён из памяти.")
                else:
                    self._append_message("Система", f"Факт [{key}] не найден в памяти.")
            else:
                self._append_message("Система", "Использование: /forget <ключ>")
            return

        if cmd in ("/context", "/work", "/threads"):
            ctx_store = getattr(self.agent, "context_store", None)
            if not ctx_store:
                self._append_message("Система", "Модуль рабочего контекста не инициализирован.")
                return

            sub = parts[1].lower() if len(parts) > 1 else ""
            if sub == "clear":
                ctx_store.clear_context()
                self._append_message("Система", "Рабочий контекст очищен.")
            elif sub == "add" and len(parts) > 3:
                kind = parts[2].lower()
                content = " ".join(parts[3:])
                item = ctx_store.add_item(kind=kind, content=content, source="user")
                self._append_message("Система", f"Контекст обновлён: [#{item.id}] ({item.kind.upper()}) '{item.content}'.")
            elif sub == "resolve" and len(parts) > 2:
                item_id = parts[2]
                if ctx_store.resolve_item(item_id):
                    self._append_message("Система", f"Задача [#{item_id}] закрыта.")
                else:
                    self._append_message("Система", f"Элемент с ID '{item_id}' не найден.")
            elif sub == "project" and len(parts) > 2:
                proj_name = " ".join(parts[2:])
                ctx_store.set_active_project(proj_name)
                self._append_message("Система", f"Активный проект установлен: [{proj_name}].")
            else:
                card = ctx_store.get_status_card()
                self._append_message("Капри", card)
            return

        if cmd in ("/initiative", "/proactive"):
            init_engine = getattr(self.agent, "initiative_engine", None)
            if not init_engine:
                self._append_message("Система", "Модуль инициативы не инициализирован.")
                return

            sub = parts[1].lower() if len(parts) > 1 else ""
            mode = getattr(self.config.companion, "mode", "normal")
            stage = self.agent.emotions.relationship_stage if hasattr(self.agent, "emotions") else "trusted_partner"

            if sub in ("on", "enable", "start"):
                init_engine.set_enabled(True)
                self.config.companion.proactive_enabled = True
                save_config(self.config)
                self._append_message("Система", "Инициатива активирована.")
            elif sub in ("off", "disable", "stop"):
                init_engine.set_enabled(False)
                self.config.companion.proactive_enabled = False
                save_config(self.config)
                self._append_message("Система", "Инициатива отключена.")
            elif sub == "level" and len(parts) > 2:
                target_lvl = parts[2].lower()
                if init_engine.set_level(target_lvl):
                    self._append_message("Система", f"Уровень инициативы: [{target_lvl.upper()}].")
                else:
                    self._append_message("Система", "Доступные уровни: subtle | normal | proactive")
            elif sub in ("cooldown", "cd") and len(parts) > 2:
                try:
                    cd_mins = int(parts[2])
                    init_engine.set_cooldown(cd_mins)
                    self._append_message("Система", f"Кулдаун инициативы: {cd_mins} мин.")
                except ValueError:
                    self._append_message("Система", "Использование: /initiative cooldown <минуты>")
            elif sub in ("trigger", "now", "force"):
                ctx_store = getattr(self.agent, "context_store", None)
                emotions = getattr(self.agent, "emotions", None)
                u_name = (getattr(self.config.companion, "user_name", "") or "").strip() or "User"
                rec = init_engine.force_trigger(context_store=ctx_store, emotions=emotions, user_name=u_name)
                self._handle_initiative_event(rec)
            else:
                u_name = (getattr(self.config.companion, "user_name", "") or "").strip() or "User"
                card = init_engine.get_status_card(companion_mode=mode, stage=stage, user_name=u_name)
                self._append_message("Капри", card)
            return

        if cmd == "/focus":
            target = parts[1] if len(parts) > 1 else "capri"
            res = asyncio.run(focus_window(target))
            self._append_message("Система", res)
            return

        if cmd in ("/windows", "/wins"):
            res = asyncio.run(list_windows())
            self._append_message("Система", res)
            return

        if cmd == "/avatar":
            act = parts[1].lower() if len(parts) > 1 else "toggle"
            if act == "auto":
                sub = parts[2].lower() if len(parts) > 2 else ""
                if sub in ("on", "1", "true", "вкл"):
                    self.config.ui.auto_avatar_poses = True
                elif sub in ("off", "0", "false", "выкл"):
                    self.config.ui.auto_avatar_poses = False
                else:
                    self.config.ui.auto_avatar_poses = not bool(getattr(self.config.ui, "auto_avatar_poses", True))
                save_config(self.config)
                st_val = bool(self.config.ui.auto_avatar_poses)
                send_avatar_control({"type": "config_sync", "auto_avatar_poses": st_val})
                st_lbl = "ВКЛЮЧЕНЫ" if st_val else "ВЫКЛЮЧЕНЫ"
                self._append_message("Система", f"Авто-позы аватара (MPRIS / сон): **{st_lbl}**.")
                return
            pose_aliases = {
                "perch": "perch",
                "присесть": "perch",
                "music": "perch_music",
                "perch_music": "perch_music",
                "вайб": "perch_music",
                "sleep": "sleep",
                "спать": "sleep",
                "wake": "wake",
                "проснуться": "wake",
                "normal": "normal",
            }
            if act in pose_aliases:
                pose = pose_aliases[act]
                self._ensure_avatar_running()
                notify_avatar("idle", "", metadata={"pose": pose, "mood": self.emotions.mood})
                self._append_message("Система", f"Поза аватара установлена: **{pose}**.")
                return
            res = asyncio.run(capri_self_control("avatar", act))
            self._append_message("Система", res)
            return

        if cmd in ("/gamemode", "/powersaver"):
            sub = parts[1].lower() if len(parts) > 1 else "status"
            if sub in ("on", "1", "true", "вкл"):
                self._game_mode_override = True
                self._sync_game_mode_state()
                send_avatar_control({"type": "power_saver", "active": True, "override": True, "reason": "slash_on"})
                self._append_message(
                    "Система",
                    "Игровой режим / Power Saver **ВКЛЮЧЁН вручную**: 60 FPS анимация аватара и фоновые инициативы поставлены на паузу.",
                )
            elif sub in ("off", "0", "false", "выкл"):
                self._game_mode_override = False
                self._sync_game_mode_state()
                send_avatar_control({"type": "power_saver", "active": False, "override": False, "reason": "slash_off"})
                self._append_message("Система", "Игровой режим / Power Saver **ВЫКЛЮЧЕН**.")
            elif sub in ("auto", "авто"):
                self._game_mode_override = None
                self.config.ui.game_mode_enabled = True
                save_config(self.config)
                self._sync_game_mode_state()
                send_avatar_control({
                    "type": "config_sync",
                    "game_mode_enabled": True,
                })
                send_avatar_control({"type": "power_saver", "override": None})
                self._append_message(
                    "Система",
                    "Игровой режим переведён в **AUTO**: автоматическая пауза при полноэкранных играх и видео в Niri / Hyprland.",
                )
            else:
                active = self._sync_game_mode_state()
                ov = getattr(self, "_game_mode_override", None)
                mode_str = "FORCE ON" if ov is True else ("FORCE OFF" if ov is False else "AUTO")
                state_str = "АКТИВЕН (Power Saver)" if active else "Обычный режим"
                self._append_message(
                    "Система",
                    f"Игровой режим: **{state_str}** (политика: `{mode_str}`).\n"
                    "Команды: `/gamemode auto | on | off`.",
                )
            return

        if cmd in ("/come", "/ко_мне", "/подойди"):
            self._ensure_avatar_running()
            notify_avatar_locomotion("come")
            play_ui_sound("ui_click")
            self._append_message("Система", "Spatial Presence: Капри направляется к активному окну.")
            return

        if cmd in ("/stay", "/стой", "/останься"):
            self._ensure_avatar_running()
            notify_avatar_locomotion("stay")
            play_ui_sound("ui_click")
            self._append_message("Система", "Spatial Presence: режим STAY активирован.")
            return

        if cmd in ("/follow", "/следуй"):
            sub = parts[1].lower() if len(parts) > 1 else ""
            enable = sub not in ("off", "0", "false", "нет", "отключи")
            self._ensure_avatar_running()
            notify_avatar_locomotion("follow", enabled=enable)
            play_ui_sound("ui_click")
            st = "ВКЛЮЧЕНО" if enable else "ВЫКЛЮЧЕНО"
            self._append_message("Система", f"Spatial Presence: следование за воркспейсами [{st}].")
            return

        if cmd in ("/home", "/база", "/домой"):
            self._ensure_avatar_running()
            notify_avatar_locomotion("home")
            play_ui_sound("ui_click")
            self._append_message("Система", "Spatial Presence: аватар возвращается на базу.")
            return

        if cmd == "/status":
            res = asyncio.run(get_system_status())
            self._append_message("Система", res)
            return

        if cmd in ("/media", "/np", "/track", "/music"):
            from ..tools.media import media_control
            sub = parts[1].lower() if len(parts) > 1 else "status"
            res = asyncio.run(media_control(sub))
            self._append_message("Система", res)
            return

        if cmd in ("/vol", "/volume"):
            from ..tools.desktop import get_volume, set_volume
            if len(parts) > 1:
                arg = parts[1].strip()
                res = asyncio.run(set_volume(arg))
            else:
                res = asyncio.run(get_volume())
            self._append_message("Система", res)
            return

        if cmd in ("/top", "/procs", "/ps"):
            from ..tools.shell import list_top_processes
            sort_by = parts[1].lower() if len(parts) > 1 and parts[1].lower() in ("cpu", "mem") else "cpu"
            res = asyncio.run(list_top_processes(sort_by=sort_by, limit=6))
            self._append_message("Система", f"Топ процессов ({sort_by.upper()}):\n{res}")
            return

        if cmd in ("/timer", "/timers", "/alarm"):
            from ..tools.timers import cancel_timer, list_timers, set_timer
            play_ui_sound("click")
            if cmd == "/timers" or (len(parts) > 1 and parts[1].lower() in ("list", "список")):
                res = asyncio.run(list_timers())
                self._append_message("Система", res)
                return

            if len(parts) > 1 and parts[1].lower() in ("cancel", "stop", "отмена", "сброс"):
                target = " ".join(parts[2:]) if len(parts) > 2 else "all"
                res = asyncio.run(cancel_timer(target))
                self._append_message("Система", res)
                return

            if len(parts) < 2:
                self._append_message(
                    "Система",
                    "Использование: /timer <время> [название] (например: /timer 5m кофе) · /timers · /timer cancel [название|all]",
                )
                return

            raw_time = parts[1].lower().strip()
            label = " ".join(parts[2:]).strip() if len(parts) > 2 else "таймер"
            try:
                if raw_time.endswith("m") or raw_time.endswith("мин"):
                    seconds = int(float(raw_time.rstrip("mмин")) * 60)
                elif raw_time.endswith("s") or raw_time.endswith("сек") or raw_time.endswith("с"):
                    seconds = int(float(raw_time.rstrip("sсекс")))
                elif raw_time.endswith("h") or raw_time.endswith("ч"):
                    seconds = int(float(raw_time.rstrip("hч")) * 3600)
                else:
                    val = float(raw_time)
                    seconds = int(val * 60) if val <= 60 else int(val)
            except ValueError:
                self._append_message("Система", f"Не удалось разобрать время '{raw_time}'. Укажи, например: 5m, 30s, 1h.")
                return

            res = asyncio.run(set_timer(seconds, label))
            self._append_message("Система", res)
            return

        if cmd in ("/model", "/models"):
            if len(parts) > 1 and parts[1].lower() == "key":
                new_key = parts[2].strip() if len(parts) > 2 else ""
                if not new_key:
                    masked = (
                        (self.config.llm.api_key[:6] + "..." + self.config.llm.api_key[-4:])
                        if self.config.llm.api_key
                        else "НЕ ЗАДАН"
                    )
                    self._append_message(
                        "Система",
                        f"Текущий API-ключ ({self.config.llm.provider or 'GROQ'}): {masked}\nДля смены: /key <новый_ключ>",
                    )
                else:
                    self.config.llm.api_key = new_key
                    self.agent.reload_config(self.config)
                    save_config(self.config)
                    masked = new_key[:6] + "..." + new_key[-4:] if len(new_key) > 10 else "***"
                    self._append_message("Система", f"API-ключ обновлён [{masked}] и сохранён.")
                return

            if len(parts) > 1:
                resolved = resolve_model_name(parts[1].strip())
                self.config.llm.model = resolved
                self.agent.reload_config(self.config)
                save_config(self.config)
                self._update_sys_info()
                self._append_message("Система", f"Модель переключена на [{resolved}].")
            else:
                models = asyncio.run(fetch_available_models(self.config))
                card_text = format_models_list(
                    self.config.llm.model,
                    models,
                    provider=(self.config.llm.provider or "GROQ"),
                )
                clean_card = re.sub(r"\[/?[a-z]+(?: [a-z]+)?\]", "", card_text)
                self._append_message("Система", clean_card)
            return

        if cmd in ("/provider", "/core", "/llm", "/провайдер", "/ядро"):
            self._show_provider_settings()
            return

        if cmd in ("/key", "/apikey"):
            new_key = parts[1].strip() if len(parts) > 1 else ""
            if not new_key:
                self._show_provider_settings()
                return
            self.config.llm.api_key = new_key
            self.agent.reload_config(self.config)
            save_config(self.config)
            masked = new_key[:6] + "..." + new_key[-4:] if len(new_key) > 10 else "***"
            self._append_message("Система", f"API-ключ обновлён [{masked}] и сохранён.")
            return

        if cmd in ("/look", "/vision", "/screen"):
            sub = parts[1].strip() if len(parts) > 1 else ""
            if sub == "select":
                msg = "Капри, посмотри на выделенную область экрана."
            elif sub:
                msg = f"Капри, посмотри на экран: {' '.join(parts[1:])}."
            else:
                msg = "Капри, глянь на экран и опиши, что ты видишь."
            self.input_field.setText(msg)
            self._send_message()
            return

        if cmd == "/tools":
            from ..tools import registry as tools_registry
            tools_list = list(tools_registry._tools.keys())
            res = f"Активные системные инструменты ({len(tools_list)}):\n" + ", ".join(sorted(tools_list))
            self._append_message("Система", res)
            return

        if cmd in ("/wake", "/wakeword"):
            sub = parts[1].lower() if len(parts) > 1 else ""
            if sub in ("on", "start", "1", "вкл"):
                ok = True if (self.wake_listener and self.wake_listener.running) else self._start_wake_listener()
                if ok:
                    self.config.voice.wake_word_enabled = True
                    save_config(self.config)
                    self._append_message("Система", "Wake-Word («Капри») активирован.")
                else:
                    self.config.voice.wake_word_enabled = False
                    self._append_message("Система", "Ошибка: не удалось запустить модель Vosk для Wake-Word.")
            elif sub in ("off", "stop", "0", "выкл"):
                if self.wake_listener:
                    self.wake_listener.stop()
                    self.wake_listener = None
                self.config.voice.wake_word_enabled = False
                save_config(self.config)
                self._append_message("Система", "Wake-Word отключён. Используй кнопку микрофона или Ctrl+M.")
            else:
                st = "ВКЛЮЧЁН" if (self.wake_listener and self.wake_listener.running) else "ВЫКЛЮЧЕН"
                self._append_message("Система", f"Статус Wake-Word: [{st}]. Переключение: /wake on | /wake off")
            return

        if cmd == "/bargein":
            sub = parts[1].lower() if len(parts) > 1 else ""
            if sub in ("on", "1", "true", "вкл"):
                self.config.voice.hands_free_barge_in = True
                save_config(self.config)
                self._append_message("Система", "Hands-Free Barge-In **ВКЛЮЧЁН**: можно перебивать речь Капри голосом.")
            elif sub in ("off", "0", "false", "выкл"):
                self.config.voice.hands_free_barge_in = False
                self._stop_barge_in_monitor()
                save_config(self.config)
                self._append_message("Система", "Hands-Free Barge-In **ВЫКЛЮЧЕН**.")
            elif sub in ("threshold", "rms", "порог") and len(parts) > 2:
                try:
                    val = float(parts[2].replace(",", "."))
                    val = max(0.010, min(0.250, val))
                    self.config.voice.barge_in_rms_threshold = val
                    save_config(self.config)
                    self._append_message(
                        "Система",
                        f"Порог Barge-In установлен: **`{val:.3f}`** "
                        "(рекомендация: `0.032` для наушников, `0.060–0.090` для громких колонок).",
                    )
                except ValueError:
                    self._append_message("Система", "Укажи число от 0.01 до 0.25, например: `/bargein threshold 0.065`")
            elif sub == "aec":
                from ..voice.pipewire import (
                    disable_pipewire_aec,
                    enable_pipewire_aec,
                    generate_pipewire_aec_conf,
                    get_pipewire_status,
                )
                aec_act = parts[2].lower() if len(parts) > 2 else "status"
                if aec_act in ("on", "enable", "1", "вкл"):
                    ok, msg = enable_pipewire_aec()
                    if ok:
                        self.config.voice.pipewire_aec_enabled = True
                        save_config(self.config)
                    self._append_message("Система", msg)
                elif aec_act in ("off", "disable", "0", "выкл"):
                    ok, msg = disable_pipewire_aec()
                    if ok:
                        self.config.voice.pipewire_aec_enabled = False
                        save_config(self.config)
                    self._append_message("Система", msg)
                elif aec_act in ("conf", "config", "snippet"):
                    snippet = generate_pipewire_aec_conf()
                    self._append_message(
                        "Система",
                        "Конфиг постоянного WebRTC AEC (`~/.config/pipewire/pipewire.conf.d/99-capri-aec.conf`):\n\n"
                        f"```text\n{snippet.rstrip()}\n```",
                    )
                else:
                    pw = get_pipewire_status()
                    if not pw.available:
                        self._append_message("Система", "PipeWire / `pactl` недоступен.")
                    else:
                        aec_st = f"АКТИВЕН (`{pw.aec_source_name}`)" if pw.aec_active else "выключен"
                        self._append_message(
                            "Система",
                            f"**{pw.server_name}** · PipeWire AEC: {aec_st}\n"
                            "Команды: `/bargein aec on | off | conf` · визуальная калибровка в Настройках → «Голос и аудио».",
                        )
            else:
                st = "ВКЛЮЧЁН" if getattr(self.config.voice, "hands_free_barge_in", True) else "ВЫКЛЮЧЕН"
                thr = float(getattr(self.config.voice, "barge_in_rms_threshold", 0.032))
                aec_cfg = "ON" if getattr(self.config.voice, "pipewire_aec_enabled", False) else "OFF"
                self._append_message(
                    "Система",
                    f"Hands-Free Barge-In: **{st}** (порог RMS: `{thr:.3f}`, PipeWire AEC: `{aec_cfg}`).\n"
                    "Команды: `/bargein on | off` · `/bargein threshold 0.032` · `/bargein aec on | off | conf`.",
                )
            return

        if cmd in ("/wm-rules", "/wmrules", "/wm"):
            from ..core.wm_rules import install_wm_rules

            target_wm = None
            apply_now = False
            include_hotkey = True
            for tok in parts[1:]:
                tl = tok.lower()
                if tl in ("niri", "hyprland"):
                    target_wm = tl
                elif tl in ("apply", "--apply", "install"):
                    apply_now = True
                elif tl in ("--no-hotkey", "no-hotkey"):
                    include_hotkey = False
            res = install_wm_rules(target_wm, dry_run=not apply_now, include_hotkey=include_hotkey)
            lang = "kdl" if res.wm == "niri" else "ini"
            if apply_now:
                self._append_message("Система", res.message)
            else:
                self._append_message(
                    "Система",
                    f"{res.message}\n\n```{lang}\n{res.snippet.rstrip()}\n```\n"
                    f"Чтобы записать правила автоматически с бэкапом, выполни: `/wm-rules {res.wm} apply`",
                )
            return

        if cmd == "/browser":
            from ..tools.browser import (
                capri_browser_clear,
                capri_browser_follow_link,
                capri_browser_search,
                capri_browser_state,
                capri_browser_visit,
            )
            sub = parts[1].strip() if len(parts) > 1 else "status"
            sub_l = sub.lower()
            if sub_l in ("status", "state", "info"):
                res = asyncio.run(capri_browser_state())
            elif sub_l in ("clear", "reset", "очистить"):
                res = asyncio.run(capri_browser_clear())
            elif sub_l == "search" and len(parts) > 2:
                res = asyncio.run(capri_browser_search(" ".join(parts[2:])))
            elif sub_l in ("link", "go", "follow") and len(parts) > 2 and parts[2].isdigit():
                res = asyncio.run(capri_browser_follow_link(int(parts[2])))
            elif sub.startswith(("http://", "https://")):
                use_js = "--js" in parts[2:]
                res = asyncio.run(capri_browser_visit(sub, use_js=use_js))
            else:
                res = (
                    "Изолированный браузер Capri («Her Own Browser»):\n"
                    "• `/browser status` — состояние и профиль\n"
                    "• `/browser https://example.com [--js]` — открыть страницу изолированно\n"
                    "• `/browser search <запрос>` — поиск с нумерованными ссылками\n"
                    "• `/browser link <N>` — перейти по ссылке №N\n"
                    "• `/browser clear` — очистить куки, историю и профиль Capri"
                )
            self._append_message("Система", res)
            return

        if cmd in ("/call", "/joinvoice", "/voicecall"):
            from ..voice.join_voice import get_join_voice_status, join_voice, leave_voice

            sub = parts[1].lower() if len(parts) > 1 else "status"
            if sub in ("join", "on", "start", "войти"):
                mix_mic = "--no-mic" not in parts[2:]
                _ok, msg = join_voice(
                    mix_user_mic=mix_mic,
                    user_mic_source=getattr(self.config.voice, "input_device", "") or "",
                )
                self._append_message("Система", msg)
            elif sub in ("leave", "off", "stop", "выйти"):
                _ok, msg = leave_voice()
                self._append_message("Система", msg)
            else:
                st = get_join_voice_status()
                if not st.pipewire_available:
                    self._append_message("Система", "Join Voice недоступен: PipeWire (`pactl`) не обнаружен.")
                elif st.active:
                    mix_lbl = "твой микрофон + Капри" if st.mic_mixed else "только голос Капри"
                    self._append_message(
                        "Система",
                        f"Join Voice **АКТИВЕН** ({mix_lbl}).\n"
                        f"Микрофон для Discord/Telegram: **`Capri_Virtual_Mic`** (`{st.virtual_mic_name}`).\n"
                        "Для отключения введи `/call leave`.",
                    )
                else:
                    self._append_message(
                        "Система",
                        "Join Voice не активен.\n"
                        "Команды: `/call join` (подключить виртуальный микрофон + твой мик) · `/call join --no-mic` (только Капри) · `/call leave`.",
                    )
            return

        if cmd == "/mcp":
            mcp_mgr = getattr(self.agent, "mcp_manager", None)
            if not mcp_mgr:
                self._append_message("Система", "MCP-менеджер не инициализирован.")
                return
            sub = parts[1].lower() if len(parts) > 1 else "status"
            if sub in ("connect", "reload", "start"):
                self._start_mcp_connect_async()
            elif sub in ("disconnect", "stop", "off"):
                self._cancel_mcp_connect_worker(wait_ms=2500)
                mcp_mgr.shutdown_sync()
                self._append_message("Система", "Все MCP-серверы отключены, внешние инструменты выгружены.")
            elif sub == "tools":
                specs = mcp_mgr.list_registered_tools()
                if not specs:
                    self._append_message("Система", "Активных MCP-инструментов нет. Выполни `/mcp connect`.")
                else:
                    lines = [f"- `{s.qualified_name}` — {s.description}" for s in specs]
                    self._append_message("Система", f"Подключённые MCP-инструменты ({len(specs)}):\n" + "\n".join(lines))
            else:
                self._append_message("Система", mcp_mgr.format_status_markdown(self.config.mcp))
            return

        if cmd == "/doctor":
            from ..core.doctor import run_diagnostics, format_diagnostics_markdown
            db_path = getattr(self.agent.session_manager, "db_path", None)
            items = run_diagnostics(self.config, sessions_db_path=db_path)
            self._append_message("Система", format_diagnostics_markdown(items))
            return

        if cmd in ("/bugreport", "/bug"):
            from ..core.doctor import generate_bug_report
            db_path = getattr(self.agent.session_manager, "db_path", None)
            custom_out = Path(parts[1]).expanduser() if len(parts) > 1 else None
            try:
                res = generate_bug_report(
                    self.config,
                    output_path=custom_out,
                    sessions_db_path=db_path,
                )
                self._append_message(
                    "Система",
                    f"Безопасный баг-репорт сохранён: `{res.report_path}`\n"
                    f"Включено строк лога: `{res.log_lines_included}` (ключи, пути, память и переписка исключены).\n\n"
                    f"{res.markdown}",
                )
            except Exception as e:
                self._append_message("Система", f"Ошибка сборки баг-репорта: {e}")
            return

        if cmd == "/setup":
            self._show_first_run_wizard()
            return

        if cmd == "/profile":
            from ..core.backup import export_profile_backup, restore_profile_backup
            sub = parts[1].lower() if len(parts) > 1 else ""
            if sub in ("export", "backup"):
                custom_out = Path(parts[2]).expanduser() if len(parts) > 2 else None
                try:
                    res = export_profile_backup(output_path=custom_out)
                    self._append_message(
                        "Система",
                        f"Резервная копия профиля создана: `{res.archive_path}` "
                        f"({', '.join(res.included_files)}; без API-ключей).",
                    )
                except Exception as e:
                    self._append_message("Система", f"Ошибка экспорта профиля: {e}")
            elif sub in ("restore", "import") and len(parts) > 2:
                try:
                    res = restore_profile_backup(Path(parts[2]).expanduser())
                    self._reload_after_profile_restore()
                    self._append_message(
                        "Система",
                        f"Профиль восстановлен ({', '.join(res.restored_files)}).",
                    )
                except Exception as e:
                    self._append_message("Система", f"Ошибка восстановления профиля: {e}")
            else:
                self._append_message(
                    "Система",
                    "Управление профилем: `/profile export [путь.zip]` · `/profile restore <путь.zip>`",
                )
            return

        if cmd == "/policy":
            from ..core.policy import VALID_POLICY_MODES
            sub = parts[1].lower() if len(parts) > 1 else ""
            if sub in VALID_POLICY_MODES:
                self.config.safety.tool_policy = sub
                self.agent.policy_engine.set_mode(sub)
                save_config(self.config)
                self._append_message(
                    "Система",
                    f"Политика безопасности инструментов установлена: **{sub.upper()}** "
                    "(разрушающие/shell/запись файлов всегда защищены).",
                )
            else:
                cur_pol = self.agent.policy_engine.mode.upper()
                self._append_message(
                    "Система",
                    f"Текущая политика инструментов: **{cur_pol}**.\n"
                    "Доступные режимы: `/policy safe` (только чтение), `/policy ask` (по умолчанию: подтверждение изменяющих действий), `/policy trusted` (базовый десктоп авто; shell/файлы/kill с подтверждением).",
                )
            return

        if cmd == "/privacy":
            p_text = (
                "Приватность и локальное хранение Capri:\n"
                "• Локальное хранилище: ~/.config/capri/\n"
                "• API-ключи пользователя: только локально в ~/.config/capri/.env (права 0600, не попадают в config.yaml)\n"
                "• Долговременная память: ~/.config/capri/memory.json\n"
                "• Шкала отношений: ~/.config/capri/emotion.json\n"
                "• Персональные логи и переписка не передаются наружу (исключены даже из /bugreport)."
            )
            self._append_message("Система", p_text)
            return

        self._append_message("Система", f"Неизвестная команда '{cmd}'. Введи /help для списка команд.")

    def _cancel_mcp_connect_worker(self, wait_ms: int = 2500) -> None:
        wk = getattr(self, "mcp_worker", None)
        if wk is None:
            return
        self.mcp_worker = None
        if not wk.isFinished():
            _track_qthread(wk)
        for sig_name in ("completed", "failed"):
            try:
                getattr(wk, sig_name).disconnect()
            except Exception:
                pass
        if wk.isRunning():
            try:
                wk.cancel()
                wk.wait(wait_ms)
            except Exception:
                pass
        if wk.isFinished():
            _ACTIVE_QTHREADS.discard(wk)

    def _start_mcp_connect_async(self) -> None:
        mcp_mgr = getattr(self.agent, "mcp_manager", None)
        if not mcp_mgr:
            self._append_message("Система", "MCP-менеджер не инициализирован.")
            return
        if not self.config.mcp or not getattr(self.config.mcp, "enabled", False):
            self._cancel_mcp_connect_worker(wait_ms=2000)
            mcp_mgr.shutdown_sync()
            self._append_message("Система", mcp_mgr.format_status_markdown(self.config.mcp))
            return
        # Cleanly cancel any previous in-flight connect attempt so repeated `/mcp connect` never leaks processes/threads
        self._cancel_mcp_connect_worker(wait_ms=2500)
        self._append_message("Система", "Подключаю MCP-серверы в фоне…")
        wk = MCPConnectWorker(mcp_mgr, self.config.mcp, parent=self)
        self.mcp_worker = wk
        _track_qthread(wk)
        wk.completed.connect(lambda res_map, cancelled, w=wk: self._on_mcp_connect_completed(w, res_map, cancelled))
        wk.failed.connect(lambda err, w=wk: self._on_mcp_connect_failed(w, err))
        wk.start()

    def _on_mcp_connect_completed(self, worker: MCPConnectWorker, res_map: dict, was_cancelled: bool) -> None:
        if self.mcp_worker is worker:
            self.mcp_worker = None
        _ACTIVE_QTHREADS.discard(worker)
        if was_cancelled:
            return
        mcp_mgr = getattr(self.agent, "mcp_manager", None)
        if not res_map:
            if mcp_mgr:
                self._append_message("Система", mcp_mgr.format_status_markdown(self.config.mcp))
            return
        summary = "\n".join(f"- **{k}:** {v}" for k, v in res_map.items())
        self._append_message("Система", f"Подключение MCP завершено:\n{summary}")

    def _on_mcp_connect_failed(self, worker: MCPConnectWorker, err: str) -> None:
        if self.mcp_worker is worker:
            self.mcp_worker = None
        _ACTIVE_QTHREADS.discard(worker)
        self._append_message("Система", f"Ошибка подключения MCP: {err}")

    def _on_thought(self, summary: str, meta: dict):
        self.current_thought_text = summary
        if hasattr(self, "current_response_card") and self.current_response_card:
            self.current_response_card.set_thought(summary)
            self._scroll_to_bottom()

    def _on_tool(self, info: str, kind: str):
        kind_l = (kind or "start").strip().lower()
        if kind_l != "result":
            self.current_tools_log.append(info)
        if hasattr(self, "current_response_card") and self.current_response_card:
            self.current_response_card.add_tool_info(info, kind=kind_l)
            self._scroll_to_bottom()

    def _start_barge_in_monitor(self):
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return
        if not getattr(self.config.voice, "hands_free_barge_in", True):
            return
        if not getattr(self.config.voice, "tts_enabled", True):
            return
        if self.barge_in_monitor is not None:
            return
        try:
            from ..voice.barge_in import HandsFreeBargeInMonitor
            from ..voice.stt import SpeechRecognizer

            stt = getattr(self, "_barge_in_stt", None)
            if stt is None:
                stt = SpeechRecognizer(self.config.voice)
                self._barge_in_stt = stt

            in_dev = (getattr(self.config.voice, "input_device", "") or "").strip()
            if not in_dev and getattr(self.config.voice, "pipewire_aec_enabled", False):
                try:
                    from ..voice.pipewire import get_pipewire_status
                    pw = get_pipewire_status()
                    if pw.aec_active and pw.aec_source_name:
                        in_dev = pw.aec_source_name
                except Exception:
                    pass

            monitor = HandsFreeBargeInMonitor(
                stt_instance=stt,
                on_barge_in=lambda: self.barge_in_triggered_signal.emit(),
                on_text_ready=lambda txt: self.barge_in_text_signal.emit(txt),
                rms_threshold=float(getattr(self.config.voice, "barge_in_rms_threshold", 0.032)),
                input_device=in_dev,
            )
            monitor.start()
            self.barge_in_monitor = monitor
        except Exception as e:
            logger.debug(f"Could not start HandsFreeBargeInMonitor: {e}")

    def _stop_barge_in_monitor(self):
        mon = getattr(self, "barge_in_monitor", None)
        self.barge_in_monitor = None
        if mon is not None:
            try:
                mon.stop()
            except Exception:
                pass

    def _on_speaking_started(self):
        self.visualizer.set_state("speaking")
        notify_avatar("speaking", "", metadata={"mood": getattr(self.emotions, "mood", "neutral")})
        self._start_barge_in_monitor()

    def _on_barge_in_triggered(self):
        """User spoke over Capri's TTS: immediately interrupt playback and listen."""
        if hasattr(self, "agent") and self.agent and hasattr(self.agent, "tts") and self.agent.tts:
            self.agent.tts.interrupt()
        if hasattr(self, "worker") and self.worker and self.worker.isRunning():
            self.worker.cancel()
        self.visualizer.set_state("listening")
        notify_avatar("listening", "Слушаю (перебивание)...", metadata={"mood": getattr(self.emotions, "mood", "neutral")})

    def _handle_barge_in_text(self, text: str):
        self._stop_barge_in_monitor()
        clean = (text or "").strip()
        if clean:
            self.input_field.setText(clean)
            self._send_message()
        else:
            self.visualizer.set_state("idle")
            notify_avatar("idle")
            if hasattr(self, "wake_listener") and self.wake_listener:
                self.wake_listener.resume()

    def _on_text_ready(self, final_text: str):
        self._stream_flush_timer.stop()
        self._stream_pending_text = None
        if not final_text or not final_text.strip():
            return

        self.visualizer.set_state("speaking")
        if getattr(self.config.voice, "tts_enabled", True):
            self._start_barge_in_monitor()
        self._response_has_content = True
        if hasattr(self, "current_response_card") and self.current_response_card:
            self.current_response_card.set_text(final_text)
            self.current_response_card.set_thought(self.current_thought_text)
            self._scroll_to_bottom()
        else:
            self._append_message("Капри", final_text, thought=self.current_thought_text, tools=self.current_tools_log)
        self.refresh_stats()
        self._attach_turn_metadata_to_live_cards()

    def _on_cancelled(self):
        self._stream_flush_timer.stop()
        self._stream_pending_text = None
        if not (self.barge_in_monitor and getattr(self.barge_in_monitor, "did_interrupt", False)):
            self._stop_barge_in_monitor()
            self.visualizer.set_state("idle")
            notify_avatar("idle")
            if hasattr(self, "wake_listener") and self.wake_listener:
                self.wake_listener.resume()
        self._reset_send_button()
        if hasattr(self, "current_response_card") and self.current_response_card:
            self.current_response_card.set_text("[Генерация ответа прервана пользователем]")
        self._attach_turn_metadata_to_live_cards()
        self.current_response_card = None
        self._response_has_content = False
        self.current_user_card = None

    def _on_speech_finished(self):
        self._stop_barge_in_monitor()
        self._reset_send_button()
        self.visualizer.set_state("idle")
        notify_avatar("idle", "", metadata={"mood": getattr(self.emotions, "mood", "neutral")})
        if hasattr(self, "wake_listener") and self.wake_listener:
            self.wake_listener.resume()
        if hasattr(self, "current_response_card") and self.current_response_card:
            card_lbl = getattr(self.current_response_card, "lbl_text", None)
            if (
                not getattr(self, "_response_has_content", False)
                and card_lbl
                and card_lbl.text().strip() == "Синхронизация нейросети..."
            ):
                if getattr(self, "current_tools_log", None):
                    self.current_response_card.set_text("[Команда выполнена]")
                else:
                    self.current_response_card.set_text("[Ответ завершён]")
        self._attach_turn_metadata_to_live_cards()
        self.current_response_card = None
        self._response_has_content = False
        self.current_user_card = None

    def _attach_turn_metadata_to_live_cards(self):
        try:
            recent = self.agent.session_manager.get_recent_turns(self.agent.current_session_id, limit=1)
            if recent:
                last_turn = recent[0]
                if hasattr(self, "current_user_card") and self.current_user_card:
                    self.current_user_card.set_turn_metadata(last_turn.id, last_turn.sequence)
                    if not getattr(self.current_user_card, "_signals_connected", False):
                        self.current_user_card.edit_requested.connect(self._on_card_edit_clicked)
                        self.current_user_card.delete_requested.connect(self._on_card_delete_clicked)
                        self.current_user_card._signals_connected = True

                if hasattr(self, "current_response_card") and self.current_response_card:
                    self.current_response_card.set_turn_metadata(last_turn.id, last_turn.sequence)
                    if not getattr(self.current_response_card, "_signals_connected", False):
                        self.current_response_card.delete_requested.connect(self._on_card_delete_clicked)
                        self.current_response_card._signals_connected = True

            cur_sess = self.agent.session_manager.get_session(self.agent.current_session_id)
            if cur_sess:
                self.lbl_active_session_title.setText(cur_sess.title)
                self.lbl_turn_counter.setText(_format_rounds_ru(cur_sess.turn_count))
            self._refresh_sidebar_sessions()
        except Exception as ex:
            logger.warning(f"Metadata attach error: {ex}")

    def _on_card_edit_clicked(self, turn_id: str):
        logger.info(f"Card edit clicked: turn_id={turn_id}")
        if self._is_generating():
            self._append_message("Система", "Дождись завершения генерации или нажми Стоп.")
            return
        turn = self.agent.session_manager.get_turn(turn_id)
        if not turn:
            logger.warning(f"Turn not found for edit: turn_id={turn_id}")
            return

        play_ui_sound("ui_click")
        self.agent.rollback_to_turn(turn_id)
        self._remove_cards_from_sequence(turn.sequence)
        self.current_user_card = None
        self.current_response_card = None

        if hasattr(self, "input_field") and self.input_field:
            self.input_field.setText(turn.user_text)
            self.input_field.setFocus()
            self.input_field.selectAll()

        self._append_message(
            "Система",
            f"Диалог откатан к раунду #{turn.sequence}. Отредактируй текст и нажми Enter.",
        )
        self.refresh_stats()

    def _on_card_delete_clicked(self, turn_id: str):
        logger.info(f"Card delete clicked: turn_id={turn_id}")
        if self._is_generating():
            self._append_message("Система", "Дождись завершения генерации или нажми Стоп.")
            return
        turn = self.agent.session_manager.get_turn(turn_id)
        if not turn:
            logger.warning(f"Turn not found for delete: turn_id={turn_id}")
            return

        reply = QMessageBox.question(
            self,
            "Удаление раунда",
            f"Удалить сообщение #{turn.sequence} и откатить диалог к этой точке?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        play_ui_sound("ui_glitch")
        self.agent.rollback_to_turn(turn_id)
        self._remove_cards_from_sequence(turn.sequence)
        self.current_user_card = None
        self.current_response_card = None
        self._update_welcome_visibility()
        self.refresh_stats()

    def _remove_cards_from_sequence(self, min_sequence: int):
        for i in reversed(range(self.chat_layout.count())):
            item = self.chat_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, ChatMessageCard) and getattr(w, "sequence", 0) >= min_sequence:
                self.chat_layout.removeWidget(w)
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _on_error(self, err: str):
        self._stream_flush_timer.stop()
        self._stream_pending_text = None
        self._reset_send_button()
        self.visualizer.set_state("idle")
        notify_avatar("idle")
        if hasattr(self, "current_response_card") and self.current_response_card:
            self.current_response_card.set_text(f"Ошибка: {err}")
        else:
            self._append_message("Система", f"Ошибка: {err}")
        self.current_response_card = None
        if hasattr(self, "wake_listener") and self.wake_listener:
            self.wake_listener.resume()

    def _trigger_vision(self):
        self.input_field.setText("Капри, глянь на экран и опиши, что ты видишь.")
        self._send_message()

    def _trigger_look_region(self):
        """Interactive Wayland screen-region capture (`slurp` + `grim`) -> attach to composer for user prompt."""
        existing = getattr(self, "_region_capture_worker", None)
        if existing is not None and existing.isRunning():
            return
        worker = RegionCaptureWorker(self)
        self._region_capture_worker = worker
        _track_qthread(worker)
        worker.captured.connect(self._on_region_captured)
        worker.start()

    def _cancel_region_capture_worker(self, wait_ms: int = 1000) -> None:
        rw = getattr(self, "_region_capture_worker", None)
        self._region_capture_worker = None
        if rw is None:
            return
        if not rw.isFinished():
            _track_qthread(rw)
        try:
            rw.captured.disconnect()
        except Exception:
            pass
        try:
            rw.cancel()
        except Exception:
            pass
        if rw.isRunning():
            try:
                rw.wait(max(100, int(wait_ms)))
            except Exception:
                pass
        rw.cleanup_unclaimed_path()
        if rw.isFinished():
            _ACTIVE_QTHREADS.discard(rw)

    def _on_region_captured(self, tmp_path: str, status: str):
        rw = getattr(self, "_region_capture_worker", None)
        if rw is not None:
            rw.claim_pending_path()
        self._region_capture_worker = None
        if status == "cancelled":
            return
        if status == "missing_tools":
            notify_avatar("playful", "Установи slurp и grim для захвата области.")
            self._append_message("Система", "Для захвата области экрана нужны утилиты Wayland: `slurp` и `grim`.")
            return
        if status != "ok" or not tmp_path or not os.path.exists(tmp_path):
            self._append_message("Система", "Не удалось захватить выбранную область экрана.")
            return

        from ..core.attachments import cleanup_temp_attachment, save_clipboard_image_bytes

        raw_bytes = b""
        try:
            raw_bytes = Path(tmp_path).read_bytes()
        except OSError:
            raw_bytes = b""
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        if not raw_bytes:
            self._append_message("Система", "Захваченное изображение оказалось пустым.")
            return

        try:
            att = save_clipboard_image_bytes(raw_bytes)
        except ValueError as e:
            self._append_message("Система", f"Скриншот области отклонён: {e}")
            return

        if not self.attachment_strip.add_attachment(att):
            cleanup_temp_attachment(att)
            self._append_message("Система", "Достигнут лимит вложений (максимум 6 на сообщение).")
            return

        play_ui_sound("ui_click")
        self._show_and_raise()
        if hasattr(self, "input_field") and self.input_field is not None:
            self.input_field.setFocus()

    def _toggle_voice_input(self):
        if self._is_recording_voice():
            self._stop_voice_input()
            return

        self.visualizer.set_state("listening")
        notify_avatar("listening", "Слушаю...")
        self._set_mic_button_recording_state()

        if hasattr(self, "wake_listener") and self.wake_listener:
            self.wake_listener.pause()

        self.voice_thread = VoiceRecordWorker(self.config.voice)
        _track_qthread(self.voice_thread)
        self.voice_thread.text_ready.connect(self._on_voice_input_ready)
        self.voice_thread.failed.connect(self._on_voice_input_failed)
        self.voice_thread.start()

    def _on_voice_input_ready(self, text: str):
        self._reset_mic_button()
        clean = text.strip() if text else ""
        if not clean:
            self.visualizer.set_state("idle")
            notify_avatar("idle")
            return
        self.input_field.setText(clean)
        self._send_message()

    def _on_voice_input_failed(self, reason: str = ""):
        self._reset_mic_button()
        self.visualizer.set_state("idle")
        notify_avatar("idle")

    def _reset_mic_button(self):
        self._composer_recording = False
        self._apply_composer_state()
        if hasattr(self, "wake_listener") and self.wake_listener:
            self.wake_listener.resume()
        if hasattr(self, "btn_mic"):
            self.btn_mic.setText("")
            self.btn_mic.setIcon(get_icon("mic", COLORS.signal_cyan, 16))
            self.btn_mic.setToolTip("Голосовой ввод (клик или Ctrl+M)")
            self.btn_mic.setStyleSheet(
                button_ghost_qss(
                    text_color=COLORS.signal_cyan,
                    hover_text=COLORS.signal_cyan_hover,
                    hover_bg=COLORS.signal_cyan_soft,
                    padding="0px",
                    radius=10,
                )
            )
        if not self._is_generating():
            self.visualizer.set_state("idle")
            notify_avatar("idle")

    def _start_wake_listener(self) -> bool:
        try:
            from ..voice.stt import SpeechRecognizer
            from ..voice.wake import WakeWordListener
            stt = SpeechRecognizer(self.config.voice)
            self.wake_listener = WakeWordListener(
                stt_instance=stt,
                on_text_ready=lambda t: self.wake_text_signal.emit(t),
                on_wake=lambda: self.wake_triggered_signal.emit(),
                wake_words=getattr(self.config.voice, "wake_words", ["капри", "эй капри", "привет капри"]),
            )
            ok = self.wake_listener.start()
            print(f"[CYBERDECK] WakeWordListener start result: {ok}", flush=True)
            return ok
        except Exception as e:
            print(f"[CYBERDECK] WakeWordListener error: {e}", flush=True)
            return False

    def _handle_wake_triggered(self):
        self.visualizer.set_state("listening")
        self._set_mic_button_recording_state()

    def _handle_wake_text(self, text: str):
        self._reset_mic_button()
        if text and text.strip():
            self.input_field.setText(text.strip())
            self._send_message()
        else:
            self.visualizer.set_state("idle")
            notify_avatar("idle")

    def _ensure_avatar_running(self):
        if notify_avatar("idle"):
            return

        proj_root = Path(__file__).resolve().parent.parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = str(proj_root)
        try:
            mode = getattr(self.config.ui, "avatar_mode", "2d")
            self.avatar_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "capri.ui.overlay",
                    "--mode",
                    mode,
                    "--parent-pid",
                    str(os.getpid()),
                ],
                cwd=str(proj_root),
                env=env,
            )
        except Exception:
            pass

    def hideEvent(self, event):
        if hasattr(self, "motion") and self.motion:
            self.motion.stop_all()
        if hasattr(self, "visualizer") and self.visualizer:
            self.visualizer.timer.stop()
        super().hideEvent(event)

    def _build_tray_icon(self) -> QIcon:
        mark = CapriSignalMark(size=32, avatar_enabled=bool(getattr(self.config.ui, "avatar_enabled", True)))
        pm = mark.grab()
        return QIcon(pm)

    def _init_tray_icon(self) -> bool:
        """Create system tray icon and menu when desktop tray is available."""
        if self.tray_icon is not None:
            return True
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        tray = QSystemTrayIcon(self._build_tray_icon(), self)
        tray.setToolTip(f"Capri Cyberdeck v{__version__}")

        menu = QMenu()
        menu.setStyleSheet(menu_qss())

        act_toggle = menu.addAction("Показать / скрыть Cyberdeck")
        act_toggle.triggered.connect(self._toggle_window_visibility)

        act_new = menu.addAction("Новый чат")
        act_new.triggered.connect(lambda: (self._show_and_raise(), self._start_new_session()))

        act_doc = menu.addAction("Диагностика (/doctor)")
        act_doc.triggered.connect(lambda: (self._show_and_raise(), self._handle_command("/doctor")))

        menu.addSeparator()
        act_quit = menu.addAction("Выход (завершить Capri)")
        act_quit.triggered.connect(self._quit_application)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self.tray_icon = tray
        return True

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._toggle_window_visibility()

    def _show_and_raise(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()
        if hasattr(self, "input_field") and self.input_field:
            self.input_field.setFocus()

    def _toggle_window_visibility(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self._show_and_raise()

    def _quit_application(self):
        self._force_quit = True
        if hasattr(self, "agent") and self.agent and getattr(self.agent, "tts", None):
            try:
                self.agent.tts.interrupt()
            except Exception:
                pass
        self.close()

    def start_ipc_server(self, socket_path: Optional[str] = None) -> bool:
        """Start local Unix IPC server (`QLocalServer`) for native Wayland `capri-deck --toggle`."""
        self._stop_ipc_server()
        target = socket_path or get_cyberdeck_ipc_socket_path()
        try:
            if os.path.exists(target):
                os.unlink(target)
        except OSError:
            pass
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(target):
            return False
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        server.newConnection.connect(self._on_ipc_connection)
        self.ipc_server = server
        self._ipc_socket_path = target
        return True

    def _stop_ipc_server(self):
        if self.ipc_server is not None:
            try:
                self.ipc_server.close()
            except Exception:
                pass
            self.ipc_server = None
        if self._ipc_socket_path and os.path.exists(self._ipc_socket_path):
            try:
                os.unlink(self._ipc_socket_path)
            except OSError:
                pass
        self._ipc_socket_path = None

    def _on_ipc_connection(self):
        if self.ipc_server is None:
            return
        while self.ipc_server.hasPendingConnections():
            conn = self.ipc_server.nextPendingConnection()
            if conn is None:
                continue
            if not conn.bytesAvailable():
                conn.waitForReadyRead(200)
            raw = bytes(conn.readAll()).decode("utf-8", errors="replace").strip()
            cmd = ""
            if raw:
                try:
                    data = json.loads(raw.splitlines()[0])
                    cmd = str(data.get("cmd", "")).strip().lower()
                except Exception:
                    cmd = raw.lower()
            if cmd == "toggle":
                self._toggle_window_visibility()
            elif cmd == "show":
                self._show_and_raise()
            elif cmd == "hide":
                self.hide()
            elif cmd == "voice_toggle":
                self._toggle_voice_input()
            elif cmd == "voice_stop":
                if self._is_recording_voice():
                    self._stop_voice_input()
                self._stop_generation(silent=True)
            elif cmd == "look_region":
                self._trigger_look_region()
            elif cmd == "quit":
                QTimer.singleShot(0, self._quit_application)
            try:
                conn.write(b"ok\n")
                conn.flush()
                conn.waitForBytesWritten(100)
                conn.disconnectFromServer()
            except Exception:
                pass

    def closeEvent(self, event):
        minimize_tray = bool(getattr(self.config.ui, "minimize_to_tray", True))
        if (
            minimize_tray
            and not self._force_quit
            and os.environ.get("QT_QPA_PLATFORM") != "offscreen"
            and self.tray_icon is not None
            and self.tray_icon.isVisible()
        ):
            event.ignore()
            self.hide()
            return

        self._stop_ipc_server()
        self._stop_barge_in_monitor()
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            try:
                from ..voice.join_voice import is_join_voice_active, leave_voice
                if is_join_voice_active():
                    leave_voice()
            except Exception:
                pass
        self._cancel_mcp_connect_worker(wait_ms=2500)
        self._cancel_region_capture_worker(wait_ms=1000)
        if hasattr(self, "agent") and self.agent and getattr(self.agent, "mcp_manager", None):
            try:
                self.agent.mcp_manager.shutdown_sync()
            except Exception:
                pass
        if self.tray_icon is not None:
            try:
                self.tray_icon.hide()
            except Exception:
                pass
        if hasattr(self, "motion") and self.motion:
            self.motion.stop_all()
        if hasattr(self, "visualizer") and self.visualizer:
            self.visualizer.timer.stop()
        if hasattr(self, "stats_timer") and self.stats_timer:
            self.stats_timer.stop()
        if hasattr(self, "initiative_timer") and self.initiative_timer:
            self.initiative_timer.stop()
        if hasattr(self, "_stream_flush_timer") and self._stream_flush_timer:
            self._stream_flush_timer.stop()
            self._stream_pending_text = None
        if hasattr(self, "attachment_strip") and self.attachment_strip:
            self.attachment_strip.clear(cleanup_temp=True)
        if hasattr(self, "voice_thread") and self.voice_thread:
            vt = self.voice_thread
            if not vt.isFinished():
                _track_qthread(vt)
            try:
                vt.text_ready.disconnect()
            except Exception:
                pass
            try:
                vt.failed.disconnect()
            except Exception:
                pass
            if vt.isRunning():
                try:
                    vt.cancel_recording()
                    vt.wait()
                except Exception:
                    pass
            if vt.isFinished():
                _ACTIVE_QTHREADS.discard(vt)
        if hasattr(self, "worker") and self.worker:
            wk = self.worker
            if not wk.isFinished():
                _track_qthread(wk)
            for sig_name in (
                "thought_signal",
                "tool_signal",
                "chunk_signal",
                "speaking_signal",
                "text_ready_signal",
                "finished_signal",
                "cancelled_signal",
                "error_signal",
                "approval_requested",
            ):
                try:
                    getattr(wk, sig_name).disconnect()
                except Exception:
                    pass
            if wk.isRunning():
                try:
                    wk.cancel()
                    wk.wait()
                except Exception:
                    pass
            if wk.isFinished():
                _ACTIVE_QTHREADS.discard(wk)
        if self.avatar_proc:
            try:
                self.avatar_proc.terminate()
                self.avatar_proc.wait(timeout=1.0)
            except Exception:
                try:
                    self.avatar_proc.kill()
                except Exception:
                    pass
            self.avatar_proc = None
        if hasattr(self, "wake_listener") and self.wake_listener:
            try:
                self.wake_listener.stop()
            except Exception:
                pass
        super().closeEvent(event)
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            app_inst = QApplication.instance()
            if app_inst is not None:
                app_inst.quit()

    def _cycle_mode(self):
        modes = ["normal", "focus", "playful", "quiet"]
        cur = self.config.companion.mode.lower()
        idx = (modes.index(cur) + 1) % len(modes) if cur in modes else 0
        new_mode = modes[idx]
        self.config.companion.mode = new_mode
        self.agent.system_prompt = get_system_prompt(self.config)
        if self.agent.history:
            self.agent.history[0] = {"role": "system", "content": self.agent._build_full_prompt()}
        save_config(self.config)
        send_avatar_control({"type": "config_sync", "companion_mode": new_mode})
        self._refresh_mode_button()
        self._append_message("Система", f"Режим работы переключён: {new_mode.upper()}")

    def _clear_dialog(self):
        sess = self.agent.start_new_session()
        self.emotions.clear_events()
        self._clear_chat_cards()
        sess_title = sess.title if sess else "Новый диалог"
        self.lbl_active_session_title.setText(sess_title)
        self.lbl_turn_counter.setText(_format_rounds_ru(0))
        self._refresh_welcome_canvas(context="new_session")
        self._update_welcome_visibility()
        self.refresh_stats()
        self._refresh_sidebar_sessions()
        if self._drawer_open or self.width() < METRICS.drawer_breakpoint:
            self._close_drawer()

    def _prompt_reset_capri(self):
        dlg = CapriResetDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            play_ui_sound("ui_glitch")
            self.emotions.reset_emotions()
            sess = self.agent.start_new_session()
            self._clear_chat_cards()
            sess_title = sess.title if sess else "Новый диалог"
            self.lbl_active_session_title.setText(sess_title)
            self.lbl_turn_counter.setText(_format_rounds_ru(0))
            self.refresh_stats()
            self._refresh_sidebar_sessions()
            notify_avatar("glitch", "Перезагрузка энграммы...")
            self._append_message(
                "Система",
                "Энграмма Капри сброшена к базовым параметрам (доверие +25, раздражение 0%).",
            )
            QTimer.singleShot(1500, lambda: notify_avatar("idle"))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if event.position().y() <= METRICS.header_height:
                wh = self.windowHandle()
                if wh:
                    wh.startSystemMove()
                    event.accept()
                    return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        radius = 10.0

        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), radius, radius)

        painter.save()
        painter.setClipPath(path)
        painter.fillPath(path, QBrush(QColor(COLORS.canvas)))

        # Restrained atmospheric Crimson Graphite light-streaks in workspace corners
        reduced = bool(getattr(getattr(self, "motion", None), "reduced_motion", False))
        if w >= 600.0 and not reduced:
            ws_left = float(METRICS.sidebar_default) if (w >= METRICS.drawer_breakpoint and getattr(self, "_sidebar_expanded", True)) else 0.0
            # Top-left subtle crimson ambient haze + delicate signal hairline
            top_glow = QRadialGradient(ws_left + (w - ws_left) * 0.14, 48.0, max(220.0, (w - ws_left) * 0.42))
            top_glow.setColorAt(0.0, QColor(225, 45, 83, 30))
            top_glow.setColorAt(0.55, QColor(225, 45, 83, 8))
            top_glow.setColorAt(1.0, QColor(225, 45, 83, 0))
            painter.fillRect(QRectF(ws_left, 0.0, w - ws_left, h), QBrush(top_glow))

            # Bottom-right subtle crimson counterbalance
            bot_glow = QRadialGradient(w - 40.0, h - 70.0, max(180.0, (w - ws_left) * 0.30))
            bot_glow.setColorAt(0.0, QColor(225, 45, 83, 24))
            bot_glow.setColorAt(0.6, QColor(225, 45, 83, 6))
            bot_glow.setColorAt(1.0, QColor(225, 45, 83, 0))
            painter.fillRect(QRectF(ws_left, 0.0, w - ws_left, h), QBrush(bot_glow))

            # Crisp 1px diagonal crimson signal hairline + cybernetic dot-matrix fade in top-left & bottom-right
            line_grad = QLinearGradient(ws_left, 230.0, ws_left + 275.0, 48.0)
            line_grad.setColorAt(0.0, QColor(225, 45, 83, 0))
            line_grad.setColorAt(0.45, QColor(225, 45, 83, 145))
            line_grad.setColorAt(1.0, QColor(225, 45, 83, 0))
            painter.setPen(QPen(QBrush(line_grad), 1.3))
            painter.drawLine(QPoint(int(ws_left), 230), QPoint(int(ws_left) + 275, 48))

            br_line = QLinearGradient(w - 220.0, h - 18.0, w - 12.0, h - 190.0)
            br_line.setColorAt(0.0, QColor(225, 45, 83, 0))
            br_line.setColorAt(0.5, QColor(225, 45, 83, 115))
            br_line.setColorAt(1.0, QColor(225, 45, 83, 0))
            painter.setPen(QPen(QBrush(br_line), 1.2))
            painter.drawLine(QPoint(int(w) - 220, int(h) - 18), QPoint(int(w) - 12, int(h) - 190))

            # Subtle dot-matrix stipple in upper-left & lower-right workspace corners
            painter.setPen(Qt.PenStyle.NoPen)
            for gx in range(int(ws_left) + 14, int(ws_left) + 220, 12):
                for gy in range(58, 220, 12):
                    d = ((gx - (ws_left + 30.0)) ** 2 + (gy - 70.0) ** 2) ** 0.5
                    a = max(0, int(26 * (1.0 - d / 195.0)))
                    if a > 2:
                        painter.setBrush(QColor(225, 45, 83, a))
                        painter.drawEllipse(QPoint(gx, gy), 1, 1)
            for gx in range(max(int(ws_left) + 14, int(w) - 210), int(w) - 14, 12):
                for gy in range(max(60, int(h) - 190), int(h) - 20, 12):
                    d = ((w - 26.0 - gx) ** 2 + (h - 36.0 - gy) ** 2) ** 0.5
                    a = max(0, int(22 * (1.0 - d / 190.0)))
                    if a > 2:
                        painter.setBrush(QColor(225, 45, 83, a))
                        painter.drawEllipse(QPoint(gx, gy), 1, 1)

        painter.restore()
        painter.setPen(QPen(QColor(COLORS.border_subtle), 1.0))
        painter.drawPath(path)


def launch_cyberdeck(argv: Optional[list[str]] = None):
    args = sys.argv[1:] if argv is None else argv
    if any(a in ("--version", "-v", "-V") for a in args):
        print(f"Capri Cyberdeck v{__version__}")
        return 0
    if any(a in ("--help", "-h") for a in args):
        print(
            f"Capri Cyberdeck v{__version__} — PySide6 Desktop Companion GUI\n"
            "Usage: capri-deck [--toggle | --show | --hide | --look-region | --quit] [--version] [--help]"
        )
        return 0

    # Native Wayland global-hotkey / single-instance IPC (Niri & Hyprland)
    auto_look_region = False
    if "--toggle" in args:
        if send_cyberdeck_ipc("toggle"):
            return 0
    elif "--show" in args:
        if send_cyberdeck_ipc("show"):
            return 0
    elif "--hide" in args:
        send_cyberdeck_ipc("hide")
        return 0
    elif "--look-region" in args:
        if send_cyberdeck_ipc("look_region"):
            return 0
        auto_look_region = True
    elif "--quit" in args:
        send_cyberdeck_ipc("quit")
        return 0
    else:
        # Plain `capri-deck`: raise existing window if already running
        if send_cyberdeck_ipc("show"):
            return 0

    app = QApplication(sys.argv)
    apply_dark_palette(app)
    app.setQuitOnLastWindowClosed(False)
    window = CapriCyberdeckWindow()
    window.show()
    if auto_look_region:
        QTimer.singleShot(350, window._trigger_look_region)
    sys.exit(app.exec())


main = launch_cyberdeck


if __name__ == "__main__":
    launch_cyberdeck()
