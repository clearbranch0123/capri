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

from PySide6.QtCore import QBuffer, QIODevice, QPoint, Qt, QTimer, Signal
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
        # `Tool` + locked `setFixedSize(w, h)` ensures tiling Wayland compositors (Niri / Hyprland / Sway)
        # automatically float this compact palette without stretching it to a full-height tiled column.
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._bar_width: int = 640

        self._attachments: list[AttachmentItem] = []
        self._generating: bool = False
        self._voice_recording: bool = False
        self._pinned: bool = True
        self._raw_response_text: str = ""
        self._tools_seen: list[str] = []
        self._drag_offset: Optional[QPoint] = None
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
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.card = QFrame(self)
        self.card.setObjectName("quickAskCard")
        self.card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
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
        self.txt_response.setMaximumHeight(250)
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
        resp_lay.addWidget(self.txt_response)

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
        card_lay.addWidget(self.response_panel)

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

    def _update_workspace_follow_timer(self) -> None:
        if (
            self._pinned
            and self.isVisible()
            and os.environ.get("QT_QPA_PLATFORM") != "offscreen"
        ):
            comp = self._get_compositor()
            if comp is not None and getattr(comp, "kind", "unknown") in ("niri", "hyprland"):
                if not self._workspace_follow_timer.isActive():
                    self._workspace_follow_timer.start()
                return
        if self._workspace_follow_timer.isActive():
            self._workspace_follow_timer.stop()

    def set_pinned(self, enabled: bool) -> None:
        self._pinned = bool(enabled)
        self._apply_pin_button_style()
        self._update_workspace_follow_timer()
        if self._pinned and self.isVisible():
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

    def _sync_compact_size(self) -> None:
        """Lock exact (width, height) so Wayland tiling WMs always keep Quick Ask floating & minimal."""
        has_attach = bool(self._attachments)
        has_resp = not self.response_panel.isHidden()

        # Fit QTextEdit height smoothly to document content without sprawling across the screen
        if has_resp:
            try:
                self.txt_response.document().setTextWidth(self._bar_width - 76)
                doc_h = int(self.txt_response.document().size().height()) + 14
            except Exception:
                doc_h = 84
            resp_h = max(64, min(230, doc_h))
            self.txt_response.setFixedHeight(resp_h)

        # Base compact height: top bar (62) + outer margins (20) = 82px
        target_h = 82
        if has_attach:
            target_h += 34
        if has_resp:
            target_h += self.txt_response.height() + 74

        target_h = max(78, min(430, target_h))
        self.setFixedSize(self._bar_width, target_h)

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

    # Allow subtle dragging by card margin / non-input header area
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        # Soft drop shadow strictly around the compact card geometry (never stretches vertically)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.card.geometry().adjusted(-3, -1, 3, 5)
        path = QPainterPath()
        path.addRoundedRect(rect, 18, 18)
        p.fillPath(path, QColor(0, 0, 0, 125))
        p.setPen(QPen(QColor(225, 45, 83, 45), 1.0))
        p.drawPath(path)
        p.end()

    def hideEvent(self, event):
        self._workspace_follow_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._copy_reset_timer.stop()
        self._workspace_follow_timer.stop()
        self.clear_attachments(cleanup_temp=True)
        super().closeEvent(event)
