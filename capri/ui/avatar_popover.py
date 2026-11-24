"""
Capri v3.3 Stage 4 — Minimalist Crimson Graphite Quick-Control Avatar Popover.

Replaces the dry right-click QMenu on the desktop avatar with a compact, tactile card:
- Header: Capri status indicator + live state chip + Relic HUD button + Close avatar button
- 1-click pose chips (2x2): Сидеть / Лежать / Танец / Сон
- Clean pill switches: Физика, Свободная жизнь, Следовать, Авто-позы, Game Mode
- Quick footer: На базу | Закрепить тут / Открепить
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .i18n import resolve_ui_lang, tr_ui
from .theme import COLORS as P, TYPE as T, apply_dark_palette


def _popover_qss() -> str:
    return f"""
        QFrame#avatarPopoverCard {{
            background-color: {P.surface};
            border: 1px solid {P.border_interactive};
            border-radius: 14px;
        }}
        QLabel#popoverBrand {{
            color: {P.text_primary};
            font-family: {T.display};
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 1.2px;
        }}
        QLabel#popoverDot {{
            background-color: {P.crimson};
            border-radius: 4px;
            min-width: 8px;
            max-width: 8px;
            min-height: 8px;
            max-height: 8px;
        }}
        QLabel#popoverStateBadge {{
            color: {P.signal_cyan};
            background-color: {P.signal_cyan_soft};
            border: 1px solid {P.signal_cyan_border};
            border-radius: 7px;
            padding: 1px 7px;
            font-family: {T.mono};
            font-size: 10px;
            font-weight: 600;
        }}
        QLabel#popoverSection {{
            color: {P.text_muted};
            font-family: {T.sans};
            font-size: 10px;
            font-weight: 600;
            letter-spacing: 0.8px;
            padding-top: 2px;
        }}
        QFrame#popoverDivider {{
            background-color: {P.border_subtle};
            min-height: 1px;
            max-height: 1px;
        }}
        QPushButton#popoverRelicBtn {{
            background-color: {P.crimson_soft};
            color: {P.text_primary};
            border: 1px solid {P.crimson_border};
            border-radius: 7px;
            padding: 3px 9px;
            font-family: {T.sans};
            font-size: 11px;
            font-weight: 600;
        }}
        QPushButton#popoverRelicBtn:hover {{
            background-color: {P.crimson};
            color: {P.text_on_accent};
            border-color: {P.crimson_hover};
        }}
        QPushButton#popoverVoiceBtn {{
            background-color: {P.signal_cyan_soft};
            color: {P.signal_cyan};
            border: 1px solid {P.signal_cyan_border};
            border-radius: 9px;
            padding: 6px 10px;
            font-family: {T.sans};
            font-size: 12px;
            font-weight: 600;
        }}
        QPushButton#popoverVoiceBtn:hover {{
            background-color: {P.signal_cyan};
            color: {P.canvas};
            border-color: {P.signal_cyan};
        }}
        QPushButton#popoverVoiceBtn[active="true"] {{
            background-color: {P.crimson};
            color: {P.text_on_accent};
            border-color: {P.crimson_hover};
        }}
        QPushButton#popoverLookBtn {{
            background-color: {P.surface_raised};
            color: {P.text_primary};
            border: 1px solid {P.border_interactive};
            border-radius: 9px;
            padding: 6px 10px;
            font-family: {T.sans};
            font-size: 12px;
            font-weight: 600;
        }}
        QPushButton#popoverLookBtn:hover {{
            background-color: {P.signal_cyan_soft};
            color: {P.signal_cyan};
            border-color: {P.signal_cyan_border};
        }}
        QPushButton#popoverCloseBtn {{
            background-color: transparent;
            color: {P.text_muted};
            border: 1px solid transparent;
            border-radius: 7px;
            padding: 2px 6px;
            font-size: 12px;
            font-weight: 700;
        }}
        QPushButton#popoverCloseBtn:hover {{
            background-color: {P.surface_raised};
            color: {P.text_primary};
            border-color: {P.border_subtle};
        }}
        QPushButton#popoverQuitBtn {{
            background-color: transparent;
            color: {P.text_muted};
            border: none;
            border-radius: 7px;
            padding: 4px 8px;
            font-family: {T.sans};
            font-size: 11px;
            font-weight: 500;
        }}
        QPushButton#popoverQuitBtn:hover {{
            background-color: {P.crimson_soft};
            color: {P.coral_hover};
        }}
        QPushButton[role="poseChip"] {{
            background-color: {P.surface_alt};
            color: {P.text_secondary};
            border: 1px solid {P.border_subtle};
            border-radius: 9px;
            padding: 6px 10px;
            font-family: {T.sans};
            font-size: 12px;
            font-weight: 500;
        }}
        QPushButton[role="poseChip"]:hover {{
            background-color: {P.surface_raised};
            color: {P.text_primary};
            border-color: {P.border_interactive};
        }}
        QPushButton[role="poseChip"][active="true"] {{
            background-color: {P.crimson_soft};
            color: {P.text_primary};
            border: 1px solid {P.crimson};
            font-weight: 600;
        }}
        QPushButton[role="toggleRow"] {{
            background-color: transparent;
            color: {P.text_primary};
            border: 1px solid transparent;
            border-radius: 8px;
            padding: 6px 8px;
            text-align: left;
            font-family: {T.sans};
            font-size: 12px;
        }}
        QPushButton[role="toggleRow"]:hover {{
            background-color: {P.surface_raised};
            border-color: {P.border_subtle};
        }}
        QLabel[role="pillState"] {{
            background-color: {P.surface_raised};
            color: {P.text_muted};
            border: 1px solid {P.border_subtle};
            border-radius: 8px;
            padding: 1px 8px;
            font-family: {T.mono};
            font-size: 10px;
            font-weight: 600;
        }}
        QLabel[role="pillState"][state="on"] {{
            background-color: {P.signal_cyan_soft};
            color: {P.signal_cyan};
            border-color: {P.signal_cyan_border};
        }}
        QLabel[role="pillState"][state="crimson"] {{
            background-color: {P.crimson_soft};
            color: {P.coral_hover};
            border-color: {P.crimson_border};
        }}
        QPushButton#popoverAskBtn {{
            background-color: {P.crimson_soft};
            color: {P.text_primary};
            border: 1px solid {P.crimson_border};
            border-radius: 9px;
            padding: 7px 10px;
            font-family: {T.sans};
            font-size: 12px;
            font-weight: 700;
        }}
        QPushButton#popoverAskBtn:hover {{
            background-color: rgba(225, 45, 83, 0.28);
            border-color: {P.crimson};
            color: {P.text_on_accent};
        }}
        QPushButton#popoverFooterBtn {{
            background-color: {P.surface_raised};
            color: {P.text_primary};
            border: 1px solid {P.border_subtle};
            border-radius: 9px;
            padding: 7px 10px;
            font-family: {T.sans};
            font-size: 12px;
            font-weight: 600;
        }}
        QPushButton#popoverFooterBtn:hover {{
            background-color: {P.surface_active};
            border-color: {P.crimson_border};
        }}
        QPushButton#popoverFooterBtn[pinned="true"] {{
            background-color: {P.crimson_soft};
            border-color: {P.crimson};
            color: {P.text_primary};
        }}
    """


class _SwitchRowButton(QPushButton):
    """Minimalist full-width clickable row with a right-aligned status pill."""

    def __init__(self, label_text: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setProperty("role", "toggleRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(30)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 6, 0)
        lay.setSpacing(8)
        self.lbl_title = QLabel(label_text, self)
        self.lbl_title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.lbl_title.setStyleSheet(f"color: {P.text_primary}; font-size: 12px; background: transparent;")
        self.lbl_pill = QLabel("OFF", self)
        self.lbl_pill.setProperty("role", "pillState")
        self.lbl_pill.setProperty("state", "off")
        self.lbl_pill.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.lbl_title, 1)
        lay.addWidget(self.lbl_pill, 0)

    def set_pill(self, text: str, state: str = "off") -> None:
        self.lbl_pill.setText(text)
        self.lbl_pill.setProperty("state", state)
        self.lbl_pill.style().unpolish(self.lbl_pill)
        self.lbl_pill.style().polish(self.lbl_pill)


class CapriAvatarQuickPopover(QWidget):
    """Minimalist Crimson Graphite quick-control popover opened on avatar right-click."""

    open_dashboard_requested = Signal()
    quick_ask_requested = Signal()
    voice_action_requested = Signal()
    look_region_requested = Signal()
    pose_toggled = Signal(str)           # "perch" | "lounge" | "dance" | "sleep"
    physics_toggled = Signal()
    autonomous_toggled = Signal()
    follow_toggled = Signal()
    auto_poses_toggled = Signal()
    game_mode_cycled = Signal()
    monitor_cycle_requested = Signal()
    home_requested = Signal()
    pin_toggled = Signal()
    close_avatar_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(
            parent,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setObjectName("capri-avatar-popover")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(268)
        apply_dark_palette(self)
        self.setStyleSheet(_popover_qss())

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self.card = QFrame(self)
        self.card.setObjectName("avatarPopoverCard")
        root.addWidget(self.card)

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(12, 11, 12, 12)
        lay.setSpacing(8)

        # ── 1. Compact Header ───────────────────────────────────────────────
        lang = resolve_ui_lang()
        self._lang = lang
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        dot = QLabel(self.card)
        dot.setObjectName("popoverDot")
        brand = QLabel("CAPRI", self.card)
        brand.setObjectName("popoverBrand")
        self.lbl_state = QLabel("IDLE", self.card)
        self.lbl_state.setObjectName("popoverStateBadge")
        self.btn_hud = QPushButton(tr_ui("◈ Релик", "◈ Relic", lang=lang), self.card)
        self.btn_hud.setObjectName("popoverRelicBtn")
        self.btn_hud.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hud.setToolTip(tr_ui("Открыть окно Capri Relic Construct", "Open Capri Relic Construct", lang=lang))
        self.btn_close = QPushButton("✕", self.card)
        self.btn_close.setObjectName("popoverCloseBtn")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setToolTip(tr_ui("Закрыть меню", "Close menu", lang=lang))

        hdr.addWidget(dot)
        hdr.addWidget(brand)
        hdr.addWidget(self.lbl_state)
        hdr.addStretch(1)
        hdr.addWidget(self.btn_hud)
        hdr.addWidget(self.btn_close)
        lay.addLayout(hdr)

        # ── 2. 1-Click Pose Chips (only real available animations; 'Лежать' auto-shows if lounge.png exists)
        lbl_poses = QLabel(tr_ui("ПОЗЫ", "POSES", lang=lang), self.card)
        lbl_poses.setObjectName("popoverSection")
        lay.addWidget(lbl_poses)

        self.pose_grid = QGridLayout()
        self.pose_grid.setSpacing(6)
        self.pose_buttons: dict[str, QPushButton] = {}
        pose_specs = [
            ("perch", tr_ui("Сидеть", "Perch", lang=lang), 0, 0),
            ("dance", tr_ui("Танец", "Dance", lang=lang), 0, 1),
            ("sleep", tr_ui("Сон", "Sleep", lang=lang), 0, 2),
            ("lounge", tr_ui("Лежать", "Lounge", lang=lang), 1, 0),
        ]
        for key, title, r, c in pose_specs:
            btn = QPushButton(title, self.card)
            btn.setProperty("role", "poseChip")
            btn.setProperty("active", "false")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _chk=False, k=key: self._emit_and_refresh(lambda: self.pose_toggled.emit(k)))
            self.pose_grid.addWidget(btn, r, c)
            self.pose_buttons[key] = btn
        self.pose_buttons["lounge"].setVisible(False)
        lay.addLayout(self.pose_grid)

        div1 = QFrame(self.card)
        div1.setObjectName("popoverDivider")
        lay.addWidget(div1)

        # ── 3. Clean Behavior Switch Rows ───────────────────────────────────
        lbl_beh = QLabel(tr_ui("ПОВЕДЕНИЕ", "BEHAVIOR", lang=lang), self.card)
        lbl_beh.setObjectName("popoverSection")
        lay.addWidget(lbl_beh)

        switches_box = QVBoxLayout()
        switches_box.setSpacing(2)
        self.row_physics = _SwitchRowButton(tr_ui("Физика и гравитация", "Physics & gravity", lang=lang), self.card)
        self.row_autonomous = _SwitchRowButton(tr_ui("Свободная жизнь", "Autonomous life", lang=lang), self.card)
        self.row_follow = _SwitchRowButton(tr_ui("Следовать за окном", "Follow active window", lang=lang), self.card)
        self.row_auto_poses = _SwitchRowButton(tr_ui("Авто-позы (музыка / сон)", "Auto-poses (music / sleep)", lang=lang), self.card)
        self.row_game_mode = _SwitchRowButton("Game Mode", self.card)
        self.row_monitor = _SwitchRowButton(tr_ui("Монитор", "Monitor", lang=lang), self.card)
        self.row_monitor.setVisible(False)
        for row in (
            self.row_physics,
            self.row_autonomous,
            self.row_follow,
            self.row_auto_poses,
            self.row_game_mode,
            self.row_monitor,
        ):
            switches_box.addWidget(row)
        lay.addLayout(switches_box)

        div2 = QFrame(self.card)
        div2.setObjectName("popoverDivider")
        lay.addWidget(div2)

        # ── 4. Quick Action Footer ──────────────────────────────────────────
        self.btn_quick_ask = QPushButton(tr_ui("⚡ Быстрый вопрос", "⚡ Quick Ask", lang=lang), self.card)
        self.btn_quick_ask.setObjectName("popoverAskBtn")
        self.btn_quick_ask.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_quick_ask.setToolTip(tr_ui("Открыть компактную строку быстрого вопроса (capri-deck --ask)", "Open compact Quick Ask bar (capri-deck --ask)", lang=lang))
        lay.addWidget(self.btn_quick_ask)

        quick_row = QHBoxLayout()
        quick_row.setSpacing(6)
        self.btn_voice = QPushButton(tr_ui("🎙 Голос", "🎙 Voice", lang=lang), self.card)
        self.btn_voice.setObjectName("popoverVoiceBtn")
        self.btn_voice.setProperty("active", "false")
        self.btn_voice.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_voice.setToolTip(tr_ui("Начать голосовой ввод или остановить речь Капри", "Start voice input or stop Capri speech", lang=lang))

        self.btn_look_region = QPushButton(tr_ui("👁 Область", "👁 Region", lang=lang), self.card)
        self.btn_look_region.setObjectName("popoverLookBtn")
        self.btn_look_region.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_look_region.setToolTip(tr_ui("Выделить область экрана и спросить Капри (slurp + grim)", "Capture screen region (slurp + grim)", lang=lang))

        quick_row.addWidget(self.btn_voice, 1)
        quick_row.addWidget(self.btn_look_region, 1)
        lay.addLayout(quick_row)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.btn_home = QPushButton(tr_ui("На базу", "Go Home", lang=lang), self.card)
        self.btn_home.setObjectName("popoverFooterBtn")
        self.btn_home.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pin = QPushButton(tr_ui("Закрепить тут", "Pin Here", lang=lang), self.card)
        self.btn_pin.setObjectName("popoverFooterBtn")
        self.btn_pin.setCursor(Qt.CursorShape.PointingHandCursor)
        footer.addWidget(self.btn_home, 1)
        footer.addWidget(self.btn_pin, 1)
        lay.addLayout(footer)

        self.btn_quit_avatar = QPushButton(tr_ui("Выключить аватар", "Close Avatar", lang=lang), self.card)
        self.btn_quit_avatar.setObjectName("popoverQuitBtn")
        self.btn_quit_avatar.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self.btn_quit_avatar)

        # Wire signals: open Relic BEFORE hiding popover so Wayland hands focus cleanly
        self.btn_hud.clicked.connect(lambda: (self.open_dashboard_requested.emit(), self.hide()))
        self.btn_quick_ask.clicked.connect(lambda: (self.quick_ask_requested.emit(), self.hide()))
        self.btn_voice.clicked.connect(lambda: (self.voice_action_requested.emit(), self.hide()))
        self.btn_look_region.clicked.connect(lambda: (self.look_region_requested.emit(), self.hide()))
        self.btn_close.clicked.connect(self.hide)
        self.btn_quit_avatar.clicked.connect(lambda: (self.hide(), self.close_avatar_requested.emit()))
        self.row_physics.clicked.connect(self.physics_toggled.emit)
        self.row_autonomous.clicked.connect(self.autonomous_toggled.emit)
        self.row_follow.clicked.connect(self.follow_toggled.emit)
        self.row_auto_poses.clicked.connect(self.auto_poses_toggled.emit)
        self.row_game_mode.clicked.connect(self.game_mode_cycled.emit)
        self.row_monitor.clicked.connect(self.monitor_cycle_requested.emit)
        self.btn_home.clicked.connect(lambda: (self.hide(), self.home_requested.emit()))
        self.btn_pin.clicked.connect(self.pin_toggled.emit)

    def _emit_and_refresh(self, fn) -> None:
        fn()

    def sync_from_state(
        self,
        *,
        active_pose: str,
        dance_active: bool,
        physics_enabled: bool,
        autonomous_enabled: bool,
        follow_enabled: bool,
        auto_poses_enabled: bool,
        game_mode_override: Optional[bool],
        power_saver_active: bool,
        pinned: bool,
        perched: bool,
        has_lounge_sprite: bool = False,
        avatar_state: str = "idle",
        available_monitors: Optional[list[str]] = None,
        active_monitor: str = "",
    ) -> None:
        st_norm = (avatar_state or "idle").strip().lower()
        if st_norm == "listening":
            self.lbl_state.setText("MIC")
        elif st_norm == "speaking":
            self.lbl_state.setText("VOICE")
        elif power_saver_active:
            self.lbl_state.setText("GAME")
        elif pinned:
            self.lbl_state.setText("PINNED")
        elif perched:
            self.lbl_state.setText("PERCH")
        else:
            self.lbl_state.setText("LIVE")

        voice_busy = st_norm in ("listening", "speaking")
        lang = getattr(self, "_lang", resolve_ui_lang())
        self.btn_voice.setText(tr_ui("⏹ Стоп", "⏹ Stop", lang=lang) if voice_busy else tr_ui("🎙 Голос", "🎙 Voice", lang=lang))
        self.btn_voice.setProperty("active", "true" if voice_busy else "false")
        self.btn_voice.style().unpolish(self.btn_voice)
        self.btn_voice.style().polish(self.btn_voice)

        # Show "Лежать" only when a real custom lounge.png sprite is actually loaded!
        self.pose_buttons["lounge"].setVisible(bool(has_lounge_sprite))

        active_map = {
            "perch": active_pose in ("perch", "perch_music") and not dance_active,
            "lounge": active_pose == "lounge" and not dance_active,
            "dance": bool(dance_active),
            "sleep": active_pose == "sleep" and not dance_active,
        }
        for key, btn in self.pose_buttons.items():
            btn.setProperty("active", "true" if active_map.get(key) else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        self.row_physics.set_pill("ON" if physics_enabled else "OFF", "on" if physics_enabled else "off")
        self.row_autonomous.set_pill("ON" if autonomous_enabled else "OFF", "on" if autonomous_enabled else "off")
        self.row_follow.set_pill("ON" if follow_enabled else "OFF", "on" if follow_enabled else "off")
        self.row_auto_poses.set_pill("ON" if auto_poses_enabled else "OFF", "on" if auto_poses_enabled else "off")

        if game_mode_override is True:
            self.row_game_mode.set_pill("FORCE", "crimson")
        elif game_mode_override is False:
            self.row_game_mode.set_pill("OFF", "off")
        else:
            self.row_game_mode.set_pill("AUTO*" if power_saver_active else "AUTO", "on" if power_saver_active else "off")

        mons = [m for m in (available_monitors or []) if m]
        if len(mons) >= 2:
            self.row_monitor.setVisible(True)
            self.row_monitor.set_pill(active_monitor or mons[0], "on")
        else:
            self.row_monitor.setVisible(False)

        self.btn_pin.setText(tr_ui("Открепить", "Unpin", lang=lang) if pinned else tr_ui("Закрепить тут", "Pin Here", lang=lang))
        self.btn_pin.setProperty("pinned", "true" if pinned else "false")
        self.btn_pin.style().unpolish(self.btn_pin)
        self.btn_pin.style().polish(self.btn_pin)

    def show_near(self, pos: QPoint) -> None:
        self.adjustSize()
        tx = max(12, pos.x() - self.width() // 2)
        ty = max(12, pos.y() - self.height() // 2)
        self.move(tx, ty)
        self.show()
        self.raise_()
        self.activateWindow()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self.hide()
        super().changeEvent(event)
