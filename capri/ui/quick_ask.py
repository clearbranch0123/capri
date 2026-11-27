"""
CAPRI // Quick Ask Bar (v3.5 — Spotlight-style Crimson Graphite Desktop Overlay).

Compact floating desktop prompt bar triggered via `capri-deck --ask` (IPC command `"ask"`)
or global Wayland hotkey (Niri / Hyprland):
- Single-line prompt input (`Enter` submits, `Esc` stops generation or dismisses)
- Direct `Ctrl+V` clipboard screenshot paste & interactive screen-region attach (`👁`)
- Quick voice input toggle (`🎙`)
- Live streaming response drawer with Copy / Collapse / `↗ В Cyberdeck` handoff
- Shares the active Cyberdeck session (`sessions.db`), memory, tools, emotions, and TTS
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QKeyEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.attachments import AttachmentItem, cleanup_temp_attachment, save_clipboard_image_bytes
from .i18n import resolve_ui_lang, tr_ui
from .theme import COLORS, TYPE, scrollbar_qss

MAX_QUICK_ASK_ATTACHMENTS = 6


class _QuickAskLineEdit(QLineEdit):
    """Single-line input with clipboard image paste (`Ctrl+V`) and `Esc` handling."""

    image_pasted = Signal(object)  # QImage
    escape_pressed = Signal()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.escape_pressed.emit()
            event.accept()
            return
        if (event.modifiers() & Qt.KeyboardModifier.ControlModifier) and event.key() == Qt.Key.Key_V:
            cb = QApplication.clipboard()
            if cb is not None:
                mime = cb.mimeData()
                if mime is not None and mime.hasImage():
                    img = cb.image()
                    if isinstance(img, QImage) and not img.isNull():
                        self.image_pasted.emit(img)
                        event.accept()
                        return
        super().keyPressEvent(event)


class CapriQuickAskBar(QWidget):
    """Floating Crimson Graphite Spotlight-style quick prompt & live answer overlay."""

    DEFAULT_BAR_WIDTH = 640
    MIN_BAR_WIDTH = 440
    MAX_BAR_WIDTH = 1120
    MIN_COMPACT_HEIGHT = 78
    MAX_COMPACT_HEIGHT = 124
    MIN_EXPANDED_HEIGHT = 210
    MAX_EXPANDED_HEIGHT = 680
    RESIZE_MARGIN = 14
    CORNER_MARGIN = 20

    submit_requested = Signal(str, list)   # (prompt_text, list[AttachmentItem])
    stop_requested = Signal()
    voice_toggle_requested = Signal()
    look_region_requested = Signal()
    open_cyberdeck_requested = Signal()
    dismissed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("capri-quick-ask")
        self.setWindowTitle("Capri Quick Ask")
        # `Tool` + bounded min/max size ensures tiling Wayland compositors (Niri / Hyprland / Sway)
        # keep this compact palette floating while allowing Windows-style edge/corner resizing.
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self._bar_width: int = self.DEFAULT_BAR_WIDTH
        self._custom_compact_height: Optional[int] = None
        self._custom_expanded_height: Optional[int] = None
        self._syncing_size: bool = False

        self._attachments: list[AttachmentItem] = []
        self._generating: bool = False
        self._voice_recording: bool = False
        self._pinned: bool = True
        self._raw_response_text: str = ""
        self._tools_seen: list[str] = []
        self._drag_offset: Optional[QPoint] = None
        self._resize_edges: Qt.Edge = Qt.Edge(0)
        self._resize_start_global: Optional[QPoint] = None
        self._resize_start_geom: Optional[QRect] = None
        self._compositor = None

        self._copy_reset_timer = QTimer(self)
        self._copy_reset_timer.setSingleShot(True)
        self._copy_reset_timer.timeout.connect(self._reset_copy_button_text)

        self._workspace_follow_timer = QTimer(self)
        self._workspace_follow_timer.setInterval(350)
        self._workspace_follow_timer.timeout.connect(self._on_workspace_follow_tick)

        self._build_ui()
        self._sync_compact_size()

    # ── UI Construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(0)

        self.card = QFrame(self)
        self.card.setObjectName("quickAskCard")
        self.card.setMouseTracking(True)
        self.card.installEventFilter(self)
        self.card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.card.setStyleSheet(f"""
            QFrame#quickAskCard {{
                background: {COLORS.nav};
                border: 1px solid {COLORS.crimson_border};
                border-radius: 16px;
            }}
        """)
        card_lay = QVBoxLayout(self.card)
        card_lay.setContentsMargins(14, 12, 14, 12)
        card_lay.setSpacing(10)

        # ── Top Input Row ──────────────────────────────────────────────────
        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(8)

        self.lbl_emblem = QLabel("◈", self.card)
        self.lbl_emblem.setFixedWidth(22)
        self.lbl_emblem.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_emblem.setStyleSheet(
            f"color: {COLORS.crimson}; font-family: {TYPE.display}; font-size: 18px; font-weight: 700; border: none;"
        )
        input_row.addWidget(self.lbl_emblem)

        self._lang = resolve_ui_lang()
        self.input_edit = _QuickAskLineEdit(self.card)
        self.input_edit.setPlaceholderText(
            tr_ui(
                "Спросить Капри…  Enter — отправить · Ctrl+V — скрин · Esc — скрыть",
                "Ask Capri…  Enter — send · Ctrl+V — screenshot · Esc — hide",
                lang=self._lang,
            )
        )
        self.input_edit.setMinimumHeight(38)
        self.input_edit.setStyleSheet(f"""
            QLineEdit {{
                background: transparent;
                color: {COLORS.text_primary};
                border: none;
                font-family: {TYPE.sans};
                font-size: 14px;
                padding: 4px 2px;
                selection-background-color: {COLORS.crimson};
            }}
        """)
        self.input_edit.returnPressed.connect(self._on_submit_clicked)
        self.input_edit.image_pasted.connect(self._on_clipboard_image_pasted)
        self.input_edit.escape_pressed.connect(self._on_escape_pressed)
        input_row.addWidget(self.input_edit, stretch=1)

        btn_icon_qss = f"""
            QPushButton {{
                background: {COLORS.surface};
                color: {COLORS.text_secondary};
                border: 1px solid {COLORS.border_subtle};
                border-radius: 9px;
                font-family: {TYPE.sans};
                font-size: 13px;
                padding: 0px;
            }}
            QPushButton:hover {{
                background: {COLORS.surface_raised};
                color: {COLORS.text_primary};
                border-color: {COLORS.border_interactive};
            }}
        """

        self.btn_region = QPushButton("👁", self.card)
        self.btn_region.setToolTip("Выделить область экрана (slurp + grim)")
        self.btn_region.setFixedSize(34, 34)
        self.btn_region.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_region.setStyleSheet(btn_icon_qss)
        self.btn_region.clicked.connect(self.look_region_requested.emit)
        input_row.addWidget(self.btn_region)

        self.btn_voice = QPushButton("🎙", self.card)
        self.btn_voice.setToolTip("Голосовой ввод")
        self.btn_voice.setFixedSize(34, 34)
        self.btn_voice.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_voice.setStyleSheet(btn_icon_qss)
        self.btn_voice.clicked.connect(self.voice_toggle_requested.emit)
        input_row.addWidget(self.btn_voice)

        self.btn_pin = QPushButton("📌", self.card)
        self.btn_pin.setFixedSize(34, 34)
        self.btn_pin.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_pin.clicked.connect(self.toggle_pinned)
        self._apply_pin_button_style()
        input_row.addWidget(self.btn_pin)

        self.btn_send = QPushButton("➤", self.card)
        self.btn_send.setToolTip("Отправить (Enter)")
        self.btn_send.setFixedSize(36, 34)
        self.btn_send.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._apply_send_button_style(generating=False)
        self.btn_send.clicked.connect(self._on_send_or_stop_clicked)
        input_row.addWidget(self.btn_send)

        self.btn_close = QPushButton("✕", self.card)
        self.btn_close.setToolTip("Скрыть (Esc)")
        self.btn_close.setFixedSize(28, 34)
        self.btn_close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_close.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {COLORS.text_muted};
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {COLORS.surface};
                color: {COLORS.text_primary};
            }}
        """)
        self.btn_close.clicked.connect(self.dismiss)
        input_row.addWidget(self.btn_close)

        card_lay.addLayout(input_row)

        # ── Attachment Chips Strip (hidden when empty) ─────────────────────
        self.attach_container = QWidget(self.card)
        self.attach_layout = QHBoxLayout(self.attach_container)
        self.attach_layout.setContentsMargins(24, 0, 0, 0)
        self.attach_layout.setSpacing(6)
        self.attach_layout.addStretch()
        self.attach_container.hide()
        card_lay.addWidget(self.attach_container)

        # ── Compact Live Response Drawer (hidden until active) ─────────────
        self.response_panel = QFrame(self.card)
        self.response_panel.setObjectName("quickAskResponse")
        self.response_panel.setStyleSheet(f"""
            QFrame#quickAskResponse {{
                background: {COLORS.surface_alt};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 11px;
            }}
        """)
        resp_lay = QVBoxLayout(self.response_panel)
        resp_lay.setContentsMargins(12, 10, 12, 10)
        resp_lay.setSpacing(8)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)

        self.lbl_status = QLabel(tr_ui("ГОТОВО", "READY", lang=self._lang), self.response_panel)
        self.lbl_status.setStyleSheet(
            f"color: {COLORS.signal_cyan}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 700; border: none;"
        )
        status_row.addWidget(self.lbl_status)

        self.lbl_tools = QLabel("", self.response_panel)
        self.lbl_tools.setStyleSheet(
            f"color: {COLORS.amber}; font-family: {TYPE.mono}; font-size: 10.5px; border: none;"
        )
        status_row.addWidget(self.lbl_tools, stretch=1)
        resp_lay.addLayout(status_row)

        self.txt_response = QTextEdit(self.response_panel)
        self.txt_response.setReadOnly(True)
        self.txt_response.setFrameShape(QFrame.Shape.NoFrame)
        self.txt_response.setMinimumHeight(64)
        self.txt_response.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.txt_response.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {COLORS.text_primary};
                border: none;
                font-family: {TYPE.sans};
                font-size: 13.5px;
                selection-background-color: {COLORS.crimson};
            }}
            {scrollbar_qss(5)}
        """)
        resp_lay.addWidget(self.txt_response, stretch=1)

        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 2, 0, 0)
        footer_row.setSpacing(8)

        footer_btn_qss = f"""
            QPushButton {{
                background: {COLORS.surface};
                color: {COLORS.text_secondary};
                border: 1px solid {COLORS.border_subtle};
                border-radius: 7px;
                font-family: {TYPE.sans};
                font-size: 11.5px;
                padding: 4px 10px;
            }}
            QPushButton:hover {{
                background: {COLORS.surface_raised};
                color: {COLORS.text_primary};
                border-color: {COLORS.border_interactive};
            }}
        """

        self.btn_copy = QPushButton(tr_ui("Копировать", "Copy", lang=self._lang), self.response_panel)
        self.btn_copy.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_copy.setStyleSheet(footer_btn_qss)
        self.btn_copy.clicked.connect(self._on_copy_clicked)
        footer_row.addWidget(self.btn_copy)

        self.btn_collapse = QPushButton(tr_ui("Свернуть", "Collapse", lang=self._lang), self.response_panel)
        self.btn_collapse.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_collapse.setStyleSheet(footer_btn_qss)
        self.btn_collapse.clicked.connect(self.collapse_response)
        footer_row.addWidget(self.btn_collapse)

        footer_row.addStretch()

        self.btn_open_deck = QPushButton(tr_ui("↗ В Cyberdeck", "↗ Cyberdeck", lang=self._lang), self.response_panel)
        self.btn_open_deck.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_open_deck.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS.signal_cyan_soft};
                color: {COLORS.signal_cyan};
                border: 1px solid {COLORS.signal_cyan_border};
                border-radius: 7px;
                font-family: {TYPE.sans};
                font-size: 11.5px;
                font-weight: 600;
                padding: 4px 11px;
            }}
            QPushButton:hover {{
                background: rgba(78, 216, 232, 0.22);
                color: {COLORS.text_primary};
            }}
        """)
        self.btn_open_deck.clicked.connect(self._on_open_cyberdeck_clicked)
        footer_row.addWidget(self.btn_open_deck)

        resp_lay.addLayout(footer_row)
        self.response_panel.hide()
        card_lay.addWidget(self.response_panel, stretch=1)

        outer.addWidget(self.card)

    def _apply_send_button_style(self, *, generating: bool) -> None:
        if generating:
            self.btn_send.setText("⏹")
            self.btn_send.setToolTip("Остановить генерацию (Esc)")
            self.btn_send.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS.coral_soft};
                    color: {COLORS.coral};
                    border: 1px solid {COLORS.coral_border};
                    border-radius: 9px;
                    font-size: 13px;
                    font-weight: 700;
                }}
                QPushButton:hover {{
                    background: rgba(240, 86, 120, 0.28);
                }}
            """)
        else:
            self.btn_send.setText("➤")
            self.btn_send.setToolTip("Отправить (Enter)")
            self.btn_send.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS.crimson};
                    color: {COLORS.text_on_accent};
                    border: none;
                    border-radius: 9px;
                    font-size: 13px;
                    font-weight: 700;
                }}
                QPushButton:hover {{
                    background: {COLORS.crimson_hover};
                }}
                QPushButton:pressed {{
                    background: {COLORS.crimson_pressed};
                }}
            """)

    def _set_emblem_color(self, color_hex: str) -> None:
        self.lbl_emblem.setStyleSheet(
            f"color: {color_hex}; font-family: {TYPE.display}; font-size: 18px; font-weight: 700; border: none;"
        )

    def _apply_pin_button_style(self) -> None:
        if self._pinned:
            self.btn_pin.setToolTip("Закреплён: следует за тобой по всем рабочим столам (клик — открепить)")
            self.btn_pin.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS.signal_cyan_soft};
                    color: {COLORS.signal_cyan};
                    border: 1px solid {COLORS.signal_cyan_border};
                    border-radius: 9px;
                    font-size: 13px;
                    padding: 0px;
                }}
                QPushButton:hover {{
                    background: rgba(78, 216, 232, 0.22);
                    border-color: {COLORS.signal_cyan};
                }}
            """)
        else:
            self.btn_pin.setToolTip("Откреплён: остаётся на текущем рабочем столе (клик — закрепить)")
            self.btn_pin.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS.surface};
                    color: {COLORS.text_muted};
                    border: 1px solid {COLORS.border_subtle};
                    border-radius: 9px;
                    font-size: 13px;
                    padding: 0px;
                }}
                QPushButton:hover {{
                    background: {COLORS.surface_raised};
                    color: {COLORS.text_primary};
                    border-color: {COLORS.border_interactive};
                }}
            """)

    @property
    def pinned(self) -> bool:
        return self._pinned

    def _get_compositor(self):
        if self._compositor is None:
            try:
                from ..core.compositor import CompositorBackend
                self._compositor = CompositorBackend()
            except Exception:
                self._compositor = False
        return self._compositor if self._compositor else None

    def _sync_native_compositor_pin(self) -> bool:
        """Sync Hyprland native `pin` state to `self._pinned` (overrides static `pin = true` WM rules)."""
        if not self.isVisible() or os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return False
        comp = self._get_compositor()
        if comp is None or getattr(comp, "kind", "unknown") != "hyprland":
            return False
        try:
            return bool(comp.set_window_pinned_sync(self.windowTitle(), self._pinned))
        except Exception:
            return False

    def _update_workspace_follow_timer(self) -> None:
        if (
            self._pinned
            and self.isVisible()
            and os.environ.get("QT_QPA_PLATFORM") != "offscreen"
        ):
            comp = self._get_compositor()
            kind = getattr(comp, "kind", "unknown") if comp is not None else "unknown"
            # Hyprland handles pinning natively via `dispatch pin`; only Niri needs timer-based workspace follow
            if kind == "niri":
                if not self._workspace_follow_timer.isActive():
                    self._workspace_follow_timer.start()
                return
        if self._workspace_follow_timer.isActive():
            self._workspace_follow_timer.stop()

    def set_pinned(self, enabled: bool) -> None:
        self._pinned = bool(enabled)
        self._apply_pin_button_style()
        self._update_workspace_follow_timer()
        if self.isVisible():
            self._sync_native_compositor_pin()
            if self._pinned:
                comp = self._get_compositor()
                if comp is not None and getattr(comp, "kind", "unknown") == "niri":
                    self._on_workspace_follow_tick()

    def toggle_pinned(self) -> None:
        self.set_pinned(not self._pinned)

    def _on_workspace_follow_tick(self) -> None:
        if not self._pinned or not self.isVisible():
            self._workspace_follow_timer.stop()
            return
        comp = self._get_compositor()
        if comp is None:
            return
        try:
            comp.follow_window_to_active_workspace_sync(self.windowTitle(), focus=False)
        except Exception:
            pass

    def is_on_other_workspace(self) -> bool:
        """Return True if Quick Ask is currently mapped on a different Niri/Hyprland workspace."""
        comp = self._get_compositor()
        if comp is None or getattr(comp, "kind", "unknown") not in ("niri", "hyprland"):
            return False
        try:
            active_ws = comp.get_focused_workspace_id_sync()
            win = comp.find_window_by_title_sync(self.windowTitle())
            if active_ws is not None and win is not None and win.get("workspace_id") is not None:
                return win["workspace_id"] != active_ws
        except Exception:
            pass
        return False

    def summon_to_active_workspace(self) -> bool:
        """Move Quick Ask onto the active Niri/Hyprland workspace and focus it."""
        comp = self._get_compositor()
        if comp is None or getattr(comp, "kind", "unknown") not in ("niri", "hyprland"):
            return False
        try:
            found, _moved = comp.follow_window_to_active_workspace_sync(self.windowTitle(), focus=True)
            return found
        except Exception:
            return False

    # ── Positioning, Compact Fixed-Height Sync & Show/Dismiss ──────────────

    def _size_bounds(self) -> tuple[int, int, int, int]:
        """Return `(min_w, max_w, min_h, max_h)` locks for the current compact/expanded state."""
        has_attach = bool(self._attachments)
        has_resp = not self.response_panel.isHidden()
        attach_extra = 34 if has_attach else 0
        if has_resp:
            min_h = self.MIN_EXPANDED_HEIGHT + attach_extra
            max_h = self.MAX_EXPANDED_HEIGHT
        else:
            min_h = self.MIN_COMPACT_HEIGHT + attach_extra
            max_h = self.MAX_COMPACT_HEIGHT + attach_extra
        return self.MIN_BAR_WIDTH, self.MAX_BAR_WIDTH, min_h, max_h

    def reset_size(self) -> None:
        """Reset Quick Ask dimensions back to default compact width & auto height."""
        self._bar_width = self.DEFAULT_BAR_WIDTH
        self._custom_compact_height = None
        self._custom_expanded_height = None
        self._sync_compact_size()

    def _unlock_resize_bounds(self) -> None:
        """Restore bounded min/max resize locks after a Wayland snap-to-size configure."""
        min_w, max_w, min_h, max_h = self._size_bounds()
        self._syncing_size = True
        try:
            self.setMinimumSize(min_w, min_h)
            self.setMaximumSize(max_w, max_h)
        finally:
            self._syncing_size = False

    def _sync_compact_size(self) -> None:
        """Apply strict min/max size locks and smoothly fit compact or expanded Quick Ask geometry."""
        has_attach = bool(self._attachments)
        has_resp = not self.response_panel.isHidden()
        min_w, max_w, min_h, max_h = self._size_bounds()

        self._bar_width = max(min_w, min(max_w, int(self._bar_width)))

        if has_resp:
            if self._custom_expanded_height is not None:
                target_h = max(min_h, min(max_h, int(self._custom_expanded_height)))
            else:
                try:
                    self.txt_response.document().setTextWidth(max(220, self._bar_width - 76))
                    doc_h = int(self.txt_response.document().size().height()) + 16
                except Exception:
                    doc_h = 84
                resp_h = max(64, min(230, doc_h))
                target_h = 82 + (34 if has_attach else 0) + resp_h + 74
                target_h = max(min_h, min(430, target_h))
        else:
            base_h = 82 + (34 if has_attach else 0)
            if self._custom_compact_height is not None:
                target_h = max(min_h, min(max_h, int(self._custom_compact_height)))
            else:
                target_h = max(min_h, min(max_h, base_h))

        self._syncing_size = True
        try:
            if self.isVisible() and os.environ.get("QT_QPA_PLATFORM") != "offscreen":
                # On Wayland (Hyprland / Niri), briefly lock min==max so the compositor applies the
                # exact programmatic expand/collapse/reset size, then re-open bounded resize limits.
                self.setFixedSize(self._bar_width, target_h)
                QTimer.singleShot(50, self._unlock_resize_bounds)
            else:
                self.setMinimumSize(min_w, min_h)
                self.setMaximumSize(max_w, max_h)
                self.resize(self._bar_width, target_h)
        finally:
            self._syncing_size = False

    def position_on_active_screen(self) -> None:
        screen = None
        try:
            screen = QApplication.screenAt(QCursor.pos())
        except Exception:
            screen = None
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.x() + max(12, (geo.width() - self.width()) // 2)
        y = geo.y() + max(42, int(geo.height() * 0.14))
        self.move(x, y)

    def open_and_focus(self) -> None:
        self._sync_compact_size()
        self.position_on_active_screen()
        self.show()
        self.raise_()
        self.activateWindow()
        self.input_edit.setFocus()
        self.input_edit.selectAll()
        self._update_workspace_follow_timer()
        QTimer.singleShot(80, self._sync_native_compositor_pin)

    def dismiss(self) -> None:
        self._workspace_follow_timer.stop()
        self.hide()
        self.dismissed.emit()

    # ── Attachments (Owned until submitted or cleaned up) ──────────────────

    def attachments(self) -> list[AttachmentItem]:
        return list(self._attachments)

    def add_attachment(self, item: AttachmentItem) -> bool:
        if len(self._attachments) >= MAX_QUICK_ASK_ATTACHMENTS:
            return False
        self._attachments.append(item)
        self._rebuild_attachment_chips()
        return True

    def remove_attachment(self, idx: int, *, cleanup_temp: bool = True) -> None:
        if 0 <= idx < len(self._attachments):
            removed = self._attachments.pop(idx)
            if cleanup_temp:
                cleanup_temp_attachment(removed)
            self._rebuild_attachment_chips()

    def clear_attachments(self, *, cleanup_temp: bool = True) -> None:
        if cleanup_temp:
            for item in self._attachments:
                cleanup_temp_attachment(item)
        self._attachments.clear()
        self._rebuild_attachment_chips()

    def take_attachments(self) -> list[AttachmentItem]:
        """Transfer ownership of all current attachments to caller without deleting temp files."""
        items = list(self._attachments)
        self._attachments.clear()
        self._rebuild_attachment_chips()
        return items

    def _rebuild_attachment_chips(self) -> None:
        while self.attach_layout.count() > 0:
            it = self.attach_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        for idx, item in enumerate(self._attachments):
            chip = QFrame(self.attach_container)
            chip.setStyleSheet(f"""
                QFrame {{
                    background: {COLORS.surface};
                    border: 1px solid {COLORS.signal_cyan_border};
                    border-radius: 8px;
                }}
            """)
            ch_lay = QHBoxLayout(chip)
            ch_lay.setContentsMargins(8, 3, 6, 3)
            ch_lay.setSpacing(6)

            raw_name = item.display_name or item.path.name
            short_name = raw_name if len(raw_name) <= 22 else f"{raw_name[:18]}…{raw_name[-4:]}"
            lbl = QLabel(f"🖼 {short_name}" if item.kind == "image" else f"📄 {short_name}", chip)
            lbl.setStyleSheet(
                f"color: {COLORS.signal_cyan}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
            )
            ch_lay.addWidget(lbl)

            btn_x = QPushButton("×", chip)
            btn_x.setFixedSize(16, 16)
            btn_x.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_x.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: {COLORS.text_muted};
                    border: none;
                    font-weight: 700;
                    font-size: 13px;
                }}
                QPushButton:hover {{
                    color: {COLORS.coral};
                }}
            """)
            btn_x.clicked.connect(lambda _=False, i=idx: self.remove_attachment(i, cleanup_temp=True))
            ch_lay.addWidget(btn_x)
            self.attach_layout.addWidget(chip)

        self.attach_layout.addStretch()
        self.attach_container.setVisible(bool(self._attachments))
        self._sync_compact_size()

    def _on_clipboard_image_pasted(self, image_obj) -> None:
        qimg = image_obj.toImage() if isinstance(image_obj, QPixmap) else image_obj
        if not isinstance(qimg, QImage) or qimg.isNull():
            return
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        ok = qimg.save(buf, "PNG")
        raw_bytes = bytes(buf.data())
        buf.close()
        if not ok or not raw_bytes:
            return
        try:
            att = save_clipboard_image_bytes(raw_bytes)
        except ValueError as err:
            self.set_error_text(f"Изображение отклонено: {err}")
            return
        if not self.add_attachment(att):
            cleanup_temp_attachment(att)
            self.set_error_text(f"Достигнут лимит вложений (максимум {MAX_QUICK_ASK_ATTACHMENTS}).")

    # ── Live State & Streaming Mirror API ──────────────────────────────────

    def set_voice_recording(self, active: bool) -> None:
        self._voice_recording = bool(active)
        if self._voice_recording:
            self.btn_voice.setText("⏹")
            self.btn_voice.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS.signal_cyan_soft};
                    color: {COLORS.signal_cyan};
                    border: 1px solid {COLORS.signal_cyan_border};
                    border-radius: 9px;
                    font-size: 13px;
                }}
            """)
            self._set_emblem_color(COLORS.signal_cyan)
            self.lbl_status.setText(tr_ui("СЛУШАЮ…", "LISTENING…", lang=self._lang))
            self.response_panel.show()
            self._sync_compact_size()
        else:
            self.btn_voice.setText("🎙")
            self.btn_voice.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS.surface};
                    color: {COLORS.text_secondary};
                    border: 1px solid {COLORS.border_subtle};
                    border-radius: 9px;
                    font-size: 13px;
                }}
                QPushButton:hover {{
                    background: {COLORS.surface_raised};
                    color: {COLORS.text_primary};
                    border-color: {COLORS.border_interactive};
                }}
            """)
            if not self._generating:
                self._set_emblem_color(COLORS.crimson)
                if self.lbl_status.text() in ("СЛУШАЮ…", "LISTENING…"):
                    if self._raw_response_text:
                        self.lbl_status.setText(tr_ui("ГОТОВО", "READY", lang=self._lang))
                        self.lbl_status.setStyleSheet(
                            f"color: {COLORS.mint}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 700; border: none;"
                        )
                    else:
                        self.response_panel.hide()
                        self._sync_compact_size()

    def begin_generation(self) -> None:
        self._generating = True
        self._raw_response_text = ""
        self._tools_seen.clear()
        self._apply_send_button_style(generating=True)
        self._set_emblem_color(COLORS.amber)
        self.lbl_status.setText(tr_ui("ДУМАЮ…", "THINKING…", lang=self._lang))
        self.lbl_status.setStyleSheet(
            f"color: {COLORS.amber}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 700; border: none;"
        )
        self.lbl_tools.setText("")
        self.txt_response.setPlainText(tr_ui("Синхронизация нейросети…", "Synchronizing neural link…", lang=self._lang))
        self.response_panel.show()
        self._sync_compact_size()

    def set_tool_activity(self, tool_info: str) -> None:
        clean = (tool_info or "").strip()
        if not clean:
            return
        name = clean.split("(")[0].strip()
        if name and name not in self._tools_seen:
            self._tools_seen.append(name)
        shown = " · ".join(self._tools_seen[-3:])
        self.lbl_tools.setText(f"⚙ {shown}")
        if self.response_panel.isHidden():
            self.response_panel.show()
        self._sync_compact_size()

    def set_streaming_text(self, text: str) -> None:
        if not text:
            return
        self._raw_response_text = text
        self.lbl_status.setText(tr_ui("ОТВЕТ…", "STREAMING…", lang=self._lang))
        self.lbl_status.setStyleSheet(
            f"color: {COLORS.signal_cyan}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 700; border: none;"
        )
        self._set_emblem_color(COLORS.signal_cyan)
        self.txt_response.setPlainText(text)
        sb = self.txt_response.verticalScrollBar()
        sb.setValue(sb.maximum())
        if self.response_panel.isHidden():
            self.response_panel.show()
        self._sync_compact_size()

    def set_final_text(self, text: str) -> None:
        self._generating = False
        self._apply_send_button_style(generating=False)
        self._set_emblem_color(COLORS.mint)
        if text:
            self._raw_response_text = text
            self.txt_response.setPlainText(text)
        self.lbl_status.setText(tr_ui("ГОТОВО", "READY", lang=self._lang))
        self.lbl_status.setStyleSheet(
            f"color: {COLORS.mint}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 700; border: none;"
        )
        if self.response_panel.isHidden():
            self.response_panel.show()
        self._sync_compact_size()

    def mark_finished(self) -> None:
        self._generating = False
        self._apply_send_button_style(generating=False)
        self._set_emblem_color(COLORS.crimson)
        if self.response_panel.isVisible() and self.lbl_status.text() in ("ДУМАЮ…", "ОТВЕТ…", "THINKING…", "STREAMING…"):
            self.lbl_status.setText(tr_ui("ГОТОВО", "READY", lang=self._lang))
            self.lbl_status.setStyleSheet(
                f"color: {COLORS.mint}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 700; border: none;"
            )

    def mark_cancelled(self) -> None:
        self._generating = False
        self._apply_send_button_style(generating=False)
        self._set_emblem_color(COLORS.crimson)
        self.lbl_status.setText(tr_ui("ПРЕРВАНО", "STOPPED", lang=self._lang))
        self.lbl_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 700; border: none;"
        )
        if not self._raw_response_text:
            self.txt_response.setPlainText(tr_ui("[Генерация остановлена]", "[Generation stopped]", lang=self._lang))

    def set_error_text(self, message: str) -> None:
        self._generating = False
        self._apply_send_button_style(generating=False)
        self._set_emblem_color(COLORS.coral)
        self._raw_response_text = message
        self.lbl_status.setText(tr_ui("ОШИБКА", "ERROR", lang=self._lang))
        self.lbl_status.setStyleSheet(
            f"color: {COLORS.coral}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 700; border: none;"
        )
        self.txt_response.setPlainText(message)
        self.response_panel.show()
        self._sync_compact_size()

    def set_language(self, lang: str) -> None:
        norm = "en" if str(lang or "").strip().lower().startswith("en") else "ru"
        self._lang = norm
        self.input_edit.setPlaceholderText(
            tr_ui(
                "Спросить Капри…  Enter — отправить · Ctrl+V — скрин · Esc — скрыть",
                "Ask Capri…  Enter — send · Ctrl+V — screenshot · Esc — hide",
                lang=norm,
            )
        )
        if not self._copy_reset_timer.isActive():
            self.btn_copy.setText(tr_ui("Копировать", "Copy", lang=norm))
        self.btn_collapse.setText(tr_ui("Свернуть", "Collapse", lang=norm))
        self.btn_open_deck.setText(tr_ui("↗ В Cyberdeck", "↗ Cyberdeck", lang=norm))
        if not self._generating and not self._voice_recording:
            self.lbl_status.setText(tr_ui("ГОТОВО", "READY", lang=norm))
        self._apply_pin_button_style()
        self._apply_send_button_style(generating=self._generating)

    def collapse_response(self) -> None:
        self._raw_response_text = ""
        self._tools_seen.clear()
        self.lbl_tools.setText("")
        self.txt_response.clear()
        self.response_panel.hide()
        self._set_emblem_color(COLORS.crimson)
        self._sync_compact_size()
        self.input_edit.setFocus()

    # ── Event Handlers ─────────────────────────────────────────────────────

    def _on_submit_clicked(self) -> None:
        text = self.input_edit.text().strip()
        if not text and not self._attachments:
            return
        atts = self.take_attachments()
        self.input_edit.clear()
        self.begin_generation()
        self.submit_requested.emit(text, atts)

    def _on_send_or_stop_clicked(self) -> None:
        if self._generating:
            self.stop_requested.emit()
        else:
            self._on_submit_clicked()

    def _on_escape_pressed(self) -> None:
        if self._generating:
            self.stop_requested.emit()
        else:
            self.dismiss()

    def _on_copy_clicked(self) -> None:
        text = self._raw_response_text or self.txt_response.toPlainText().strip()
        if not text:
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        self.btn_copy.setText(tr_ui("Скопировано ✓", "Copied ✓", lang=self._lang))
        self._copy_reset_timer.start(1400)

    def _reset_copy_button_text(self) -> None:
        self.btn_copy.setText(tr_ui("Копировать", "Copy", lang=self._lang))

    def _on_open_cyberdeck_clicked(self) -> None:
        self.hide()
        self.open_cyberdeck_requested.emit()

    # ── Edge/Corner Resizing (Windows-style) & Wayland-Native Moving ───────

    def _resize_edges_at(self, pos: QPoint) -> Qt.Edge:
        """Detect which window edges/corners `pos` (widget-local) hits for interactive resize."""
        w = self.width()
        h = self.height()
        x = pos.x()
        y = pos.y()
        m = self.RESIZE_MARGIN
        cm = self.CORNER_MARGIN

        near_left = x <= m
        near_right = x >= (w - m)
        near_top = y <= m
        near_bottom = y >= (h - m)

        # Slightly wider grab zone at the 4 corners so diagonal resize feels effortless
        if x <= cm and y <= cm:
            return Qt.Edge.LeftEdge | Qt.Edge.TopEdge
        if x >= (w - cm) and y <= cm:
            return Qt.Edge.RightEdge | Qt.Edge.TopEdge
        if x <= cm and y >= (h - cm):
            return Qt.Edge.LeftEdge | Qt.Edge.BottomEdge
        if x >= (w - cm) and y >= (h - cm):
            return Qt.Edge.RightEdge | Qt.Edge.BottomEdge

        edges = Qt.Edge(0)
        if near_left:
            edges |= Qt.Edge.LeftEdge
        elif near_right:
            edges |= Qt.Edge.RightEdge
        if near_top:
            edges |= Qt.Edge.TopEdge
        elif near_bottom:
            edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _cursor_for_edges(edges: Qt.Edge) -> Qt.CursorShape:
        has_l = bool(edges & Qt.Edge.LeftEdge)
        has_r = bool(edges & Qt.Edge.RightEdge)
        has_t = bool(edges & Qt.Edge.TopEdge)
        has_b = bool(edges & Qt.Edge.BottomEdge)
        if (has_l and has_t) or (has_r and has_b):
            return Qt.CursorShape.SizeFDiagCursor
        if (has_r and has_t) or (has_l and has_b):
            return Qt.CursorShape.SizeBDiagCursor
        if has_l or has_r:
            return Qt.CursorShape.SizeHorCursor
        if has_t or has_b:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def _update_hover_cursor(self, pos: QPoint) -> None:
        edges = self._resize_edges_at(pos)
        shape = self._cursor_for_edges(edges)
        if shape == Qt.CursorShape.ArrowCursor:
            self.unsetCursor()
        else:
            self.setCursor(QCursor(shape))

    def _apply_manual_resize(self, global_pos: QPoint) -> None:
        if not self._resize_edges or self._resize_start_global is None or self._resize_start_geom is None:
            return
        min_w, max_w, min_h, max_h = self._size_bounds()
        delta = global_pos - self._resize_start_global
        g = QRect(self._resize_start_geom)

        left = g.left()
        right = g.right()
        top = g.top()
        bottom = g.bottom()

        if self._resize_edges & Qt.Edge.RightEdge:
            new_w = max(min_w, min(max_w, g.width() + delta.x()))
            right = left + new_w - 1
        elif self._resize_edges & Qt.Edge.LeftEdge:
            new_w = max(min_w, min(max_w, g.width() - delta.x()))
            left = right - new_w + 1

        if self._resize_edges & Qt.Edge.BottomEdge:
            new_h = max(min_h, min(max_h, g.height() + delta.y()))
            bottom = top + new_h - 1
        elif self._resize_edges & Qt.Edge.TopEdge:
            new_h = max(min_h, min(max_h, g.height() - delta.y()))
            top = bottom - new_h + 1

        new_rect = QRect(left, top, right - left + 1, bottom - top + 1)
        self.setGeometry(new_rect)
        self._bar_width = max(min_w, min(max_w, new_rect.width()))
        if self.response_panel.isHidden():
            self._custom_compact_height = max(min_h, min(max_h, new_rect.height()))
        else:
            self._custom_expanded_height = max(min_h, min(max_h, new_rect.height()))

    def eventFilter(self, obj, event):
        if obj is self.card:
            et = event.type()
            if et == QEvent.Type.MouseMove and not (event.buttons() & Qt.MouseButton.LeftButton):
                pos = self.mapFromGlobal(event.globalPosition().toPoint())
                self._update_hover_cursor(pos)
            elif et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                pos = self.mapFromGlobal(event.globalPosition().toPoint())
                edges = self._resize_edges_at(pos)
                if edges != Qt.Edge(0):
                    wh = self.windowHandle()
                    if wh is not None and wh.startSystemResize(edges):
                        event.accept()
                        return True
                    self._resize_edges = edges
                    self._resize_start_global = event.globalPosition().toPoint()
                    self._resize_start_geom = self.geometry()
                    event.accept()
                    return True
            elif et == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
                pos = self.mapFromGlobal(event.globalPosition().toPoint())
                if self._resize_edges_at(pos) != Qt.Edge(0):
                    self.reset_size()
                    event.accept()
                    return True
            elif et == QEvent.Type.Leave:
                self.unsetCursor()
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            edges = self._resize_edges_at(pos)
            wh = self.windowHandle()
            if edges != Qt.Edge(0):
                if wh is not None and wh.startSystemResize(edges):
                    event.accept()
                    return
                self._resize_edges = edges
                self._resize_start_global = event.globalPosition().toPoint()
                self._resize_start_geom = self.geometry()
                event.accept()
                return
            if wh is not None and wh.startSystemMove():
                event.accept()
                return
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._resize_edges_at(event.position().toPoint()) != Qt.Edge(0):
                self.reset_size()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize_edges != Qt.Edge(0) and (event.buttons() & Qt.MouseButton.LeftButton):
            self._apply_manual_resize(event.globalPosition().toPoint())
            event.accept()
            return
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        self._update_hover_cursor(event.position().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        self._resize_edges = Qt.Edge(0)
        self._resize_start_global = None
        self._resize_start_geom = None
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._syncing_size:
            return
        min_w, max_w, min_h, max_h = self._size_bounds()
        self._bar_width = max(min_w, min(max_w, self.width()))
        if self.response_panel.isHidden():
            self._custom_compact_height = max(min_h, min(max_h, self.height()))
        else:
            self._custom_expanded_height = max(min_h, min(max_h, self.height()))
            try:
                self.txt_response.document().setTextWidth(max(220, self._bar_width - 76))
            except Exception:
                pass

    def paintEvent(self, event):
        # Soft drop shadow around card + subtle bottom-right Crimson Graphite resize grip
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.card.geometry().adjusted(-3, -1, 3, 5)
        path = QPainterPath()
        path.addRoundedRect(rect, 18, 18)
        p.fillPath(path, QColor(0, 0, 0, 125))
        p.setPen(QPen(QColor(225, 45, 83, 45), 1.0))
        p.drawPath(path)

        # Subtle corner resize indicator ticks (bottom-right rim)
        cg = self.card.geometry()
        grip_pen = QPen(QColor(225, 45, 83, 95), 1.3)
        grip_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(grip_pen)
        bx, by = cg.right() - 6, cg.bottom() - 6
        p.drawLine(bx - 7, by, bx, by - 7)
        p.drawLine(bx - 3, by, bx, by - 3)
        p.end()

    def hideEvent(self, event):
        self._workspace_follow_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._copy_reset_timer.stop()
        self._workspace_follow_timer.stop()
        self.clear_attachments(cleanup_temp=True)
        super().closeEvent(event)
