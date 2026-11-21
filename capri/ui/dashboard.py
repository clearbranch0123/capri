"""
CAPRI // Relic Construct Dashboard (v3.1 — Crimson Graphite Structural Redesign).
4-zone architectural telemetry HUD sharing Design Tokens (`theme.py`) and reusable
components (`CapriSignalMark`, `SegmentedEngramBar`, `CrimsonEdgeCard`, `RelicTelemetryTile`)
directly with Cyberdeck.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QRectF, QTimer, QPoint, QSize
from PySide6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QLinearGradient,
    QRadialGradient,
    QPixmap,
    QCursor,
    QPainterPath,
)
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QFrame,
)

from ..core.emotions import EmotionEngine
from ..core.config import load_config, save_config
from ..core.resources import get_assets_dir
from .icons import get_icon
from .theme import (
    COLORS,
    TYPE,
    apply_dark_palette,
    button_ghost_qss,
)
from .widgets import (
    CapriSignalMark,
    CrimsonEdgeCard,
    RelicTelemetryTile,
    SegmentedEngramBar,
)

ASSETS_DIR = get_assets_dir()

_MODE_SPEC = [
    ("normal", "Normal", COLORS.signal_cyan),
    ("focus", "Focus", COLORS.lilac),
    ("playful", "Playful", COLORS.crimson_hover),
    ("quiet", "Quiet", COLORS.text_muted),
]


class CapriRelicDashboard(QWidget):
    """
    Unified Crimson Graphite Relic Construct HUD:
    1. Top Command Bar (Crimson Prism Mark + live SYNCED pill + Close)
    2. Hero Engram Core + Framed Relic Biochip Card
    3. 2x2 Real Telemetry Tile Grid (Trust, Mood/Irritation, MemoryStore facts, ToolRegistry tools)
    4. Interactive 4-Segment Mode Switcher + Live Hardware/Policy Strip
    (Obsolete 'Терминал Capri' and 'Тест голоса' controls removed.)
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.emotions = EmotionEngine()
        self.config = load_config()
        self.drag_position = QPoint()
        self._mode_buttons: dict[str, QPushButton] = {}

        self._init_window()
        self._init_ui()
        self._init_timer()

    def _init_window(self):
        self.setWindowTitle("Capri // Relic Construct")
        self.setObjectName("capri-dashboard")
        self.setFixedSize(700, 466)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 16, 20, 18)
        main_layout.setSpacing(13)

        # ── 1. TOP COMMAND BAR ───────────────────────────────────────
        top_bar = QHBoxLayout()
        top_bar.setSpacing(10)

        self.brand_mark = CapriSignalMark(
            size=24,
            avatar_enabled=bool(getattr(self.config.ui, "avatar_enabled", True)),
            parent=self,
        )
        top_bar.addWidget(self.brand_mark)

        lbl_title = QLabel("CAPRI // RELIC CONSTRUCT")
        lbl_title.setStyleSheet(
            f"color: {COLORS.text_primary}; font-family: {TYPE.display}; font-size: 13px; font-weight: 700; letter-spacing: 1.0px;"
        )
        top_bar.addWidget(lbl_title)

        self.lbl_stage_pill = QLabel("● SYNCED")
        self.lbl_stage_pill.setStyleSheet(
            f"color: {COLORS.signal_cyan}; background: {COLORS.signal_cyan_soft}; "
            f"border: 1px solid {COLORS.signal_cyan_border}; border-radius: 10px; "
            f"padding: 2px 9px; font-family: {TYPE.mono}; font-size: 10px; font-weight: 600;"
        )
        top_bar.addWidget(self.lbl_stage_pill)
        top_bar.addStretch()

        btn_close = QPushButton()
        btn_close.setIcon(get_icon("close", COLORS.text_muted, 15))
        btn_close.setIconSize(QSize(15, 15))
        btn_close.setFixedSize(30, 30)
        btn_close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_close.setToolTip("Скрыть Relic Construct")
        btn_close.setStyleSheet(
            button_ghost_qss(
                text_color=COLORS.text_muted,
                hover_text=COLORS.crimson_hover,
                hover_bg=COLORS.crimson_soft,
                padding="0px",
                radius=8,
            )
        )
        btn_close.clicked.connect(self.hide)
        top_bar.addWidget(btn_close)
        main_layout.addLayout(top_bar)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {COLORS.border_hairline}; border: none;")
        main_layout.addWidget(sep)

        # ── 2. HERO ROW: ENGRAM CORE (LEFT) + BIOCHIP SHOWCASE (RIGHT) ─
        hero_row = QHBoxLayout()
        hero_row.setSpacing(14)

        hero_card = CrimsonEdgeCard(parent=self, radius=12, bg_color=COLORS.surface_alt, accent_color=COLORS.crimson)
        hc_lay = QVBoxLayout(hero_card)
        hc_lay.setContentsMargins(16, 13, 15, 13)
        hc_lay.setSpacing(9)

        sync_top = QHBoxLayout()
        sync_top.setSpacing(8)
        lbl_sync_tag = QLabel("СИНХРОНИЗАЦИЯ ЭНГРАММЫ")
        lbl_sync_tag.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10px; font-weight: 700; letter-spacing: 0.9px;"
        )
        sync_top.addWidget(lbl_sync_tag)
        sync_top.addStretch()

        self.lbl_sync_val = QLabel("100%")
        self.lbl_sync_val.setStyleSheet(
            f"color: {COLORS.signal_cyan}; font-family: {TYPE.display}; font-size: 22px; font-weight: 700;"
        )
        sync_top.addWidget(self.lbl_sync_val)
        hc_lay.addLayout(sync_top)

        self.bar_sync = SegmentedEngramBar(segments=5, parent=hero_card)
        self.bar_sync.setFixedHeight(8)
        hc_lay.addWidget(self.bar_sync)

        user_name = getattr(getattr(self.config, "companion", None), "user_name", "User") or "User"
        self.lbl_quote = QLabel(f"«На связи, {user_name}. Энграмма синхронизирована и развёрнута в системе.»")
        self.lbl_quote.setWordWrap(True)
        self.lbl_quote.setStyleSheet(
            f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12.5px; line-height: 1.42;"
        )
        hc_lay.addWidget(self.lbl_quote)

        self.lbl_stage_line = QLabel("Синхронизированная энграмма")
        self.lbl_stage_line.setStyleSheet(
            f"color: {COLORS.signal_cyan}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 600;"
        )
        hc_lay.addWidget(self.lbl_stage_line)
        hero_row.addWidget(hero_card, stretch=62)

        # Framed Relic Biochip Showcase Card
        chip_card = CrimsonEdgeCard(
            parent=self,
            radius=12,
            bg_color=COLORS.surface,
            accent_color=COLORS.crimson,
            top_accent=True,
        )
        chip_card.setFixedWidth(226)
        cc_lay = QVBoxLayout(chip_card)
        cc_lay.setContentsMargins(10, 12, 10, 10)
        cc_lay.setSpacing(6)
        cc_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.lbl_chip_img = QLabel()
        self.lbl_chip_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip_path = ASSETS_DIR / "chip-source.png"
        if not chip_path.exists():
            chip_path = ASSETS_DIR / "chip.png"
        if chip_path.exists():
            pix = QPixmap(str(chip_path)).scaled(
                200, 108, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            self.lbl_chip_img.setPixmap(pix)
        cc_lay.addWidget(self.lbl_chip_img)

        lbl_chip_name = QLabel("RELIC 2.0 // CAPRICORN")
        lbl_chip_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_chip_name.setStyleSheet(
            f"color: {COLORS.text_primary}; font-family: {TYPE.mono}; font-size: 10px; font-weight: 700;"
        )
        cc_lay.addWidget(lbl_chip_name)

        lbl_chip_status = QLabel("ENGRAM LOADED")
        lbl_chip_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_chip_status.setStyleSheet(
            f"color: {COLORS.signal_cyan}; font-family: {TYPE.mono}; font-size: 9.5px; font-weight: 600;"
        )
        cc_lay.addWidget(lbl_chip_status)
        hero_row.addWidget(chip_card, stretch=38)

        main_layout.addLayout(hero_row)

        # ── 3. 2x2 LIVE TELEMETRY TILE GRID (100% REAL DATA) ─────────
        tiles_grid = QGridLayout()
        tiles_grid.setContentsMargins(0, 0, 0, 0)
        tiles_grid.setHorizontalSpacing(10)
        tiles_grid.setVerticalSpacing(10)

        self.tile_trust = RelicTelemetryTile("heart", f"Доверие к {user_name}", "+25 / 100", COLORS.mint, self)
        self.tile_mood = RelicTelemetryTile("activity", "Настроение и шум", "Спокойное · 0%", COLORS.crimson_hover, self)
        self.tile_memory = RelicTelemetryTile("memory", "Банк памяти", "0 фактов", COLORS.signal_cyan, self)
        self.tile_tools = RelicTelemetryTile("terminal", "Инструменты", "0 активны", COLORS.amber, self)

        # Preserve compatibility attributes for callers/tests inspecting telemetry labels
        self.lbl_trust = self.tile_trust.lbl_value
        self.lbl_mood = self.tile_mood.lbl_value
        self.lbl_irrit = self.tile_mood.lbl_title
        self.lbl_memory_count = self.tile_memory.lbl_value
        self.lbl_tools_count = self.tile_tools.lbl_value

        tiles_grid.addWidget(self.tile_trust, 0, 0)
        tiles_grid.addWidget(self.tile_mood, 0, 1)
        tiles_grid.addWidget(self.tile_memory, 1, 0)
        tiles_grid.addWidget(self.tile_tools, 1, 1)
        main_layout.addLayout(tiles_grid)

        # ── 4. BOTTOM COMMAND STRIP: 4-SEGMENT MODE SWITCHER + RUNTIME ─
        footer_row = QHBoxLayout()
        footer_row.setSpacing(10)

        mode_frame = QFrame(self)
        mode_frame.setObjectName("relicModeBar")
        mode_frame.setStyleSheet(f"""
            QFrame#relicModeBar {{
                background: {COLORS.surface_alt};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 9px;
            }}
        """)
        mf_lay = QHBoxLayout(mode_frame)
        mf_lay.setContentsMargins(4, 4, 4, 4)
        mf_lay.setSpacing(4)

        for mode_id, mode_lbl, _accent in _MODE_SPEC:
            btn = QPushButton(mode_lbl)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda _checked=False, m=mode_id: self._select_mode(m))
            mf_lay.addWidget(btn)
            self._mode_buttons[mode_id] = btn

        # Compatibility button reference (`Режим: ...` verified by existing tests)
        self.btn_mode = self._mode_buttons["normal"]
        footer_row.addWidget(mode_frame, stretch=3)

        self.lbl_runtime_badge = QLabel()
        self.lbl_runtime_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_runtime_badge.setFixedHeight(36)
        self.lbl_runtime_badge.setStyleSheet(f"""
            QLabel {{
                background: {COLORS.surface_alt};
                color: {COLORS.text_secondary};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 9px;
                padding: 0px 12px;
                font-family: {TYPE.mono};
                font-size: 10.5px;
                font-weight: 600;
            }}
        """)
        footer_row.addWidget(self.lbl_runtime_badge, stretch=2)

        main_layout.addLayout(footer_row)
        apply_dark_palette(self)
        self._refresh_mode_buttons()
        self.refresh_stats()

    def _init_timer(self):
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self.refresh_stats)
        self.update_timer.start(2000)

    def _refresh_mode_buttons(self):
        cur = (self.config.companion.mode or "normal").lower()
        for mode_id, mode_lbl, accent in _MODE_SPEC:
            btn = self._mode_buttons[mode_id]
            active = mode_id == cur
            if active:
                btn.setText(f"● {mode_lbl}")
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {COLORS.surface_raised};
                        color: {accent};
                        border: 1px solid {COLORS.border_interactive};
                        border-radius: 6px;
                        padding: 0px 10px;
                        font-family: {TYPE.mono};
                        font-size: 10.5px;
                        font-weight: 700;
                    }}
                """)
            else:
                btn.setText(mode_lbl)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: transparent;
                        color: {COLORS.text_muted};
                        border: 1px solid transparent;
                        border-radius: 6px;
                        padding: 0px 10px;
                        font-family: {TYPE.mono};
                        font-size: 10.5px;
                        font-weight: 500;
                    }}
                    QPushButton:hover {{
                        background: {COLORS.surface};
                        color: {COLORS.text_primary};
                    }}
                """)

    def _select_mode(self, mode_id: str):
        self.config.companion.mode = mode_id
        self._refresh_mode_buttons()
        try:
            if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
                save_config(self.config)
        except Exception:
            pass

    def _cycle_mode(self):
        modes = [m[0] for m in _MODE_SPEC]
        cur = (self.config.companion.mode or "normal").lower()
        idx = (modes.index(cur) + 1) % len(modes) if cur in modes else 0
        self._select_mode(modes[idx])

    def refresh_stats(self):
        self.emotions.load()
        trust = self.emotions.trust
        irrit = self.emotions.irritation
        mood = self.emotions.mood

        mood_ru = {
            "playful": "Игривое",
            "warm": "Тёплое",
            "sarcastic": "Саркастичное",
            "annoyed": "Раздражённое",
            "neutral": "Спокойное",
        }.get(mood, mood)

        sync_pct = max(10, min(100, int(((trust + 100) / 200.0) * 100)))
        self.lbl_sync_val.setText(f"{sync_pct}%")
        self.bar_sync.setValue(sync_pct)
        self.lbl_stage_line.setText(self.emotions.relationship_stage_label)

        self.tile_trust.set_value(f"{trust:+d} / 100", COLORS.mint)
        mood_col = COLORS.amber if irrit > 25 else COLORS.text_primary
        self.tile_mood.set_value(f"{mood_ru} · {irrit}%", mood_col)

        try:
            from ..core.memory import MemoryStore
            mem_cnt = len(MemoryStore().get_all())
        except Exception:
            mem_cnt = 0
        try:
            from ..tools.registry import registry
            tools_cnt = len(registry.get_schemas())
        except Exception:
            tools_cnt = 0
        self.tile_memory.set_value(f"{mem_cnt} фактов", COLORS.signal_cyan)
        self.tile_tools.set_value(f"{tools_cnt} активны", COLORS.amber)

        prov = (self.config.llm.provider or "GROQ").upper()
        try:
            from ..core.compositor import detect_compositor
            comp_kind = detect_compositor()
            comp_short = {"niri": "NIRI", "hyprland": "HYPR"}.get(comp_kind, "WL")
        except Exception:
            comp_short = "WL"
        policy_lbl = (getattr(getattr(self.config, "safety", None), "tool_policy", "ask") or "ask").upper()
        self.lbl_runtime_badge.setText(f"{prov} · {comp_short} · {policy_lbl}")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), 14.0, 14.0)

        painter.save()
        painter.setClipPath(path)
        painter.fillPath(path, QBrush(QColor(COLORS.nav)))

        # Subtle atmospheric Crimson corner glow + cybernetic dot-matrix stipple
        glow = QRadialGradient(w * 0.14, 0.0, w * 0.58)
        glow.setColorAt(0.0, QColor(225, 45, 83, 30))
        glow.setColorAt(1.0, QColor(225, 45, 83, 0))
        painter.fillRect(QRectF(0.0, 0.0, w, h), QBrush(glow))

        # Restrained top-right dot matrix accent
        painter.setPen(Qt.PenStyle.NoPen)
        for gx in range(int(w) - 180, int(w) - 12, 12):
            for gy in range(14, 110, 12):
                dist = ((w - gx) ** 2 + gy ** 2) ** 0.5
                alpha = max(0, int(24 * (1.0 - dist / 210.0)))
                if alpha > 2:
                    painter.setBrush(QColor(225, 45, 83, alpha))
                    painter.drawEllipse(QPoint(gx, gy), 1, 1)

        # Crisp top crimson-to-cyan hairline
        top_grad = QLinearGradient(0.0, 0.0, w, 0.0)
        top_grad.setColorAt(0.0, QColor(225, 45, 83, 0))
        top_grad.setColorAt(0.22, QColor(225, 45, 83, 200))
        top_grad.setColorAt(0.78, QColor(78, 216, 232, 110))
        top_grad.setColorAt(1.0, QColor(78, 216, 232, 0))
        painter.setPen(QPen(QBrush(top_grad), 1.6))
        painter.drawLine(QPoint(14, 1), QPoint(int(w) - 14, 1))
        painter.restore()

        painter.setPen(QPen(QColor(COLORS.border_subtle), 1.0))
        painter.drawPath(path)
