"""
CAPRI // Unified Design System v3.1 — Crimson Graphite.
Centralized color tokens, typography, spacing, radius, elevation, dark QPalette guards,
and shared QSS factories for Cyberdeck, Relic Dashboard, Avatar menus, and all dialogs.

Color Architecture:
- Dark Graphite: deep obsidian/graphite layered surfaces without muddy blue casts
- Crimson Red: primary Capri brand accent (emblem, primary CTA, active selection, assistant edge, send)
- Cyan: secondary system accent (engram sync, online/presence, voice waveform, thought disclosure)
- Amber: tools, warnings, and ToolPolicyEngine approval requests
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    # Dark Graphite layered surfaces
    canvas: str = "#0A0B0F"
    nav: str = "#0E1016"
    surface: str = "#141720"
    surface_alt: str = "#11141C"
    surface_raised: str = "#1B1F2B"
    surface_active: str = "#222736"
    surface_code: str = "#0C0E14"

    # Typography hierarchy
    text_primary: str = "#F2F4F8"
    text_secondary: str = "#A2ABB9"
    text_muted: str = "#6B7384"
    text_on_accent: str = "#FFFFFF"
    text_on_cyan: str = "#07131A"

    # Structural hairlines & focus
    border_hairline: str = "#1B1F2A"
    border_subtle: str = "#252A38"
    border_interactive: str = "#343B4E"
    focus_ring: str = "#E12D53"

    # Primary brand accent — Crimson Red (Capri signature)
    crimson: str = "#E12D53"
    crimson_hover: str = "#F23D63"
    crimson_pressed: str = "#C42245"
    crimson_soft: str = "rgba(225, 45, 83, 0.14)"
    crimson_wash: str = "rgba(225, 45, 83, 0.08)"
    crimson_border: str = "rgba(225, 45, 83, 0.42)"
    crimson_glow: str = "rgba(225, 45, 83, 0.22)"

    # Secondary system accent — Cyan (presence, engram sync, voice, thoughts)
    signal_cyan: str = "#4ED8E8"
    signal_cyan_hover: str = "#70E4F2"
    signal_cyan_soft: str = "rgba(78, 216, 232, 0.12)"
    signal_cyan_border: str = "rgba(78, 216, 232, 0.34)"

    # Supporting accents
    lilac: str = "#A992F7"
    lilac_soft: str = "rgba(169, 146, 247, 0.12)"
    lilac_border: str = "rgba(169, 146, 247, 0.30)"

    coral: str = "#F05678"
    coral_hover: str = "#FA6E8C"
    coral_soft: str = "rgba(240, 86, 120, 0.14)"
    coral_border: str = "rgba(240, 86, 120, 0.38)"

    # Tools, warnings & approvals — Amber
    amber: str = "#E8AD52"
    amber_soft: str = "rgba(232, 173, 82, 0.13)"
    amber_border: str = "rgba(232, 173, 82, 0.34)"

    mint: str = "#4DD4A8"
    mint_soft: str = "rgba(77, 212, 168, 0.14)"
    mint_border: str = "rgba(77, 212, 168, 0.34)"

    backdrop: str = "rgba(5, 6, 10, 0.72)"


@dataclass(frozen=True)
class Typography:
    sans: str = '"Inter", "Inter Display", "Segoe UI", "Noto Sans", sans-serif'
    display: str = '"Inter Display", "Inter", "Segoe UI", sans-serif'
    mono: str = '"JetBrainsMono Nerd Font", "JetBrains Mono", "Fira Code", monospace'

    size_xs: int = 11
    size_sm: int = 12
    size_base: int = 14
    size_body: int = 14
    size_md: int = 15
    size_lg: int = 18
    size_xl: int = 22


@dataclass(frozen=True)
class Metrics:
    sp_1: int = 4
    sp_2: int = 8
    sp_3: int = 12
    sp_4: int = 16
    sp_6: int = 24
    sp_8: int = 32

    radius_sm: int = 8
    radius_md: int = 12
    radius_lg: int = 16
    radius_pill: int = 999

    sidebar_min: int = 252
    sidebar_default: int = 266
    sidebar_max: int = 280
    column_max_width: int = 1600
    header_height: int = 48
    control_height: int = 36
    compact_control_height: int = 30
    drawer_breakpoint: int = 1000
    compact_height_breakpoint: int = 520


COLORS = Palette()
TYPE = Typography()
METRICS = Metrics()


def scrollbar_qss(width: int = 6) -> str:
    return f"""
        QScrollBar:vertical {{
            background: transparent;
            width: {width}px;
            margin: 4px 0px;
            border: none;
        }}
        QScrollBar::handle:vertical {{
            background: {COLORS.border_subtle};
            min-height: 28px;
            border-radius: {width // 2}px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {COLORS.border_interactive};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            height: 0px;
            background: transparent;
        }}
        QScrollBar:horizontal {{
            background: transparent;
            height: {width}px;
            margin: 0px 4px;
            border: none;
        }}
        QScrollBar::handle:horizontal {{
            background: {COLORS.border_subtle};
            min-width: 28px;
            border-radius: {width // 2}px;
        }}
        QScrollBar::handle:horizontal:hover {{
            background: {COLORS.border_interactive};
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
            width: 0px;
            background: transparent;
        }}
    """


def card_frame_qss(
    object_name: str,
    bg: str = COLORS.surface_alt,
    border: str = COLORS.border_hairline,
    radius: int = 12,
    left_accent: str = "",
) -> str:
    left_rule = f"border-left: 2px solid {left_accent};" if left_accent else ""
    return f"""
        QFrame#{object_name} {{
            background: {bg};
            border: 1px solid {border};
            {left_rule}
            border-radius: {radius}px;
        }}
    """


def menu_qss() -> str:
    return f"""
        QMenu {{
            background-color: {COLORS.surface};
            color: {COLORS.text_primary};
            border: 1px solid {COLORS.border_subtle};
            border-radius: {METRICS.radius_sm}px;
            padding: 5px;
            font-family: {TYPE.sans};
            font-size: 12.5px;
        }}
        QMenu::item {{
            padding: 7px 14px;
            border-radius: 6px;
            color: {COLORS.text_primary};
        }}
        QMenu::item:selected {{
            background-color: {COLORS.crimson_soft};
            color: {COLORS.text_primary};
        }}
        QMenu::item:disabled {{
            color: {COLORS.text_muted};
            background: transparent;
        }}
        QMenu::separator {{
            height: 1px;
            background: {COLORS.border_hairline};
            margin: 5px 8px;
        }}
        QMenu::indicator {{
            width: 14px;
            height: 14px;
            margin-left: 6px;
        }}
    """


def crimson_cta_qss(padding: str = "0px 12px", radius: int = 10) -> str:
    """Signature Capri Crimson Red primary sidebar CTA with restrained horizontal depth."""
    return f"""
        QPushButton {{
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #DC264E,
                stop:0.55 #8B1B35,
                stop:1 #2A141D
            );
            color: {COLORS.text_on_accent};
            border: 1px solid rgba(225, 45, 83, 0.56);
            border-radius: {radius}px;
            padding: {padding};
            font-family: {TYPE.sans};
            font-size: 12.5px;
            font-weight: 600;
            text-align: left;
        }}
        QPushButton:hover {{
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #ED355D,
                stop:0.58 #9E203E,
                stop:1 #361724
            );
            border-color: {COLORS.crimson_hover};
            color: #FFFFFF;
        }}
        QPushButton:pressed {{
            background: {COLORS.crimson_pressed};
            border-color: {COLORS.crimson};
        }}
        QPushButton:focus {{
            border: 1px solid {COLORS.crimson_hover};
        }}
    """


def button_primary_qss(padding: str = "8px 16px", radius: int = 8) -> str:
    return f"""
        QPushButton {{
            background: qlineargradient(
                x1:0, y1:0, x2:0, y2:1,
                stop:0 #EA355B,
                stop:1 #CF2348
            );
            color: {COLORS.text_on_accent};
            border: 1px solid {COLORS.crimson};
            border-radius: {radius}px;
            padding: {padding};
            font-family: {TYPE.sans};
            font-size: 12.5px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            background: {COLORS.crimson_hover};
            border-color: {COLORS.crimson_hover};
        }}
        QPushButton:pressed {{
            background: {COLORS.crimson_pressed};
        }}
        QPushButton:focus {{
            border: 1px solid {COLORS.text_primary};
        }}
        QPushButton:disabled {{
            background: {COLORS.surface_raised};
            color: {COLORS.text_muted};
            border-color: {COLORS.border_hairline};
        }}
    """


def button_secondary_qss(padding: str = "7px 14px", radius: int = 8) -> str:
    return f"""
        QPushButton {{
            background: {COLORS.surface};
            color: {COLORS.text_primary};
            border: 1px solid {COLORS.border_subtle};
            border-radius: {radius}px;
            padding: {padding};
            font-family: {TYPE.sans};
            font-size: 12.5px;
            font-weight: 500;
        }}
        QPushButton:hover {{
            background: {COLORS.surface_raised};
            border-color: {COLORS.border_interactive};
        }}
        QPushButton:pressed {{
            background: {COLORS.surface_active};
        }}
        QPushButton:focus {{
            border-color: {COLORS.focus_ring};
        }}
        QPushButton:disabled {{
            background: {COLORS.nav};
            color: {COLORS.text_muted};
            border-color: {COLORS.border_hairline};
        }}
    """


def button_ghost_qss(
    text_color: str = COLORS.text_secondary,
    hover_text: str = COLORS.text_primary,
    hover_bg: str = COLORS.surface_raised,
    padding: str = "6px 10px",
    radius: int = 8,
    font_size: str = "12px",
    align_left: bool = False,
) -> str:
    align_rule = "text-align: left;" if align_left else ""
    return f"""
        QPushButton {{
            background: transparent;
            color: {text_color};
            border: 1px solid transparent;
            border-radius: {radius}px;
            padding: {padding};
            font-family: {TYPE.sans};
            font-size: {font_size};
            font-weight: 500;
            {align_rule}
        }}
        QPushButton:hover {{
            background: {hover_bg};
            color: {hover_text};
        }}
        QPushButton:pressed {{
            background: {COLORS.surface_active};
        }}
        QPushButton:focus {{
            border: 1px solid {COLORS.focus_ring};
        }}
        QPushButton:disabled {{
            color: {COLORS.text_muted};
        }}
    """


def button_danger_qss(filled: bool = False, padding: str = "7px 14px", radius: int = 8) -> str:
    if filled:
        return f"""
            QPushButton {{
                background: {COLORS.coral};
                color: #FFFFFF;
                border: 1px solid {COLORS.coral};
                border-radius: {radius}px;
                padding: {padding};
                font-family: {TYPE.sans};
                font-size: 12.5px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {COLORS.coral_hover};
                border-color: {COLORS.coral_hover};
            }}
            QPushButton:focus {{
                border: 1px solid {COLORS.text_primary};
            }}
            QPushButton:disabled {{
                background: {COLORS.surface_raised};
                color: {COLORS.text_muted};
                border-color: {COLORS.border_hairline};
            }}
        """
    return f"""
        QPushButton {{
            background: transparent;
            color: {COLORS.text_muted};
            border: 1px solid transparent;
            border-radius: {radius}px;
            padding: {padding};
            font-family: {TYPE.sans};
            font-size: 12px;
            font-weight: 500;
        }}
        QPushButton:hover {{
            background: {COLORS.coral_soft};
            color: {COLORS.coral};
            border-color: {COLORS.coral_border};
        }}
        QPushButton:focus {{
            border-color: {COLORS.coral};
            color: {COLORS.coral};
        }}
        QPushButton:disabled {{
            color: {COLORS.text_muted};
        }}
    """


def input_qss(padding: str = "7px 11px", radius: int = 8, mono: bool = False) -> str:
    font_family = TYPE.mono if mono else TYPE.sans
    return f"""
        QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox {{
            background: {COLORS.canvas};
            color: {COLORS.text_primary};
            border: 1px solid {COLORS.border_subtle};
            border-radius: {radius}px;
            padding: {padding};
            font-family: {font_family};
            font-size: 12.5px;
            selection-background-color: {COLORS.crimson_soft};
            selection-color: {COLORS.text_primary};
        }}
        QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover, QComboBox:hover {{
            border-color: {COLORS.border_interactive};
        }}
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{
            border-color: {COLORS.focus_ring};
            background: {COLORS.surface_alt};
        }}
        QLineEdit:disabled {{
            background: {COLORS.nav};
            color: {COLORS.text_muted};
        }}
        QComboBox::drop-down {{
            border: none;
            width: 24px;
            background: transparent;
        }}
        QComboBox QAbstractItemView {{
            background-color: {COLORS.surface};
            color: {COLORS.text_primary};
            border: 1px solid {COLORS.border_subtle};
            border-radius: 6px;
            selection-background-color: {COLORS.surface_active};
            selection-color: {COLORS.signal_cyan};
            outline: none;
            padding: 4px;
        }}
        QComboBox QAbstractItemView::item {{
            background-color: {COLORS.surface};
            color: {COLORS.text_primary};
            padding: 6px 10px;
            border-radius: 4px;
            min-height: 24px;
        }}
        QComboBox QAbstractItemView::item:hover {{
            background-color: {COLORS.surface_raised};
            color: {COLORS.text_primary};
        }}
        QComboBox QAbstractItemView::item:selected {{
            background-color: {COLORS.surface_active};
            color: {COLORS.signal_cyan};
        }}
    """


def dialog_base_qss() -> str:
    return f"""
        QDialog {{
            background: {COLORS.nav};
            color: {COLORS.text_primary};
            border: 1px solid {COLORS.border_subtle};
            border-radius: {METRICS.radius_md}px;
            font-family: {TYPE.sans};
        }}
        QLabel {{
            color: {COLORS.text_secondary};
            font-family: {TYPE.sans};
            background: transparent;
        }}
        QCheckBox {{
            color: {COLORS.text_primary};
            font-family: {TYPE.sans};
            font-size: 12.5px;
            spacing: 8px;
        }}
        QCheckBox::indicator {{
            width: 16px;
            height: 16px;
            border-radius: 4px;
            border: 1px solid {COLORS.border_interactive};
            background: {COLORS.canvas};
        }}
        QCheckBox::indicator:checked {{
            background: {COLORS.crimson};
            border-color: {COLORS.crimson};
        }}
        QCheckBox::indicator:focus {{
            border-color: {COLORS.focus_ring};
        }}
        {input_qss()}
        {scrollbar_qss(6)}
    """


def message_box_qss() -> str:
    return f"""
        QMessageBox {{
            background-color: {COLORS.nav};
            color: {COLORS.text_primary};
            font-family: {TYPE.sans};
        }}
        QMessageBox QLabel {{
            color: {COLORS.text_primary};
            font-size: 13px;
            font-family: {TYPE.sans};
        }}
        QMessageBox QPushButton {{
            background: {COLORS.surface};
            color: {COLORS.text_primary};
            border: 1px solid {COLORS.border_subtle};
            border-radius: 8px;
            padding: 6px 14px;
            min-width: 72px;
            font-family: {TYPE.sans};
            font-size: 12px;
            font-weight: 500;
        }}
        QMessageBox QPushButton:hover {{
            background: {COLORS.surface_raised};
            border-color: {COLORS.crimson};
            color: {COLORS.text_primary};
        }}
    """


def markdown_document_css() -> str:
    return f"""
        body {{
            color: {COLORS.text_primary};
            font-family: {TYPE.sans};
            font-size: 14.5px;
            line-height: 1.54;
        }}
        p {{
            margin: 0px 0px 8px 0px;
        }}
        p:last-child {{
            margin-bottom: 0px;
        }}
        code {{
            font-family: {TYPE.mono};
            font-size: 12.5px;
            background-color: {COLORS.surface_raised};
            color: {COLORS.signal_cyan};
            padding: 1px 5px;
            border-radius: 4px;
        }}
        pre {{
            font-family: {TYPE.mono};
            font-size: 12px;
            background-color: {COLORS.surface_code};
            color: {COLORS.text_primary};
            padding: 10px 12px;
            border-radius: 8px;
            border: 1px solid {COLORS.border_hairline};
            margin: 8px 0px;
            white-space: pre-wrap;
            word-break: break-word;
        }}
        blockquote {{
            border-left: 2px solid {COLORS.crimson};
            padding-left: 12px;
            margin: 6px 0px;
            color: {COLORS.text_secondary};
            font-style: italic;
        }}
        a {{
            color: {COLORS.signal_cyan};
            text-decoration: none;
        }}
        ul, ol {{
            margin-top: 4px;
            margin-bottom: 8px;
            padding-left: 20px;
        }}
        li {{
            margin-bottom: 3px;
        }}
        h1 {{
            color: {COLORS.text_primary};
            font-size: 18px;
            font-weight: 700;
            margin: 10px 0px 6px 0px;
        }}
        h2 {{
            color: {COLORS.text_primary};
            font-size: 16px;
            font-weight: 600;
            margin: 8px 0px 4px 0px;
        }}
        h3, h4 {{
            color: {COLORS.signal_cyan};
            font-size: 14.5px;
            font-weight: 600;
            margin: 6px 0px 4px 0px;
        }}
        strong {{
            color: #FFFFFF;
            font-weight: 600;
        }}
        em {{
            color: {COLORS.text_secondary};
        }}
        hr {{
            border: none;
            border-top: 1px solid {COLORS.border_hairline};
            margin: 10px 0px;
        }}
    """


def table_qss() -> str:
    return f"""
        QTableWidget {{
            background: {COLORS.canvas};
            background-color: {COLORS.canvas};
            color: {COLORS.text_primary};
            gridline-color: {COLORS.border_hairline};
            border: 1px solid {COLORS.border_subtle};
            border-radius: {METRICS.radius_sm}px;
            font-size: 12.5px;
            font-family: {TYPE.sans};
            outline: none;
        }}
        QTableWidget::item {{
            padding: 6px 8px;
        }}
        QTableWidget::item:selected {{
            background: {COLORS.crimson_soft};
            color: {COLORS.text_primary};
        }}
        QHeaderView {{
            background: {COLORS.canvas};
            background-color: {COLORS.canvas};
            border: none;
        }}
        QHeaderView::section {{
            background: {COLORS.surface};
            background-color: {COLORS.surface};
            color: {COLORS.text_secondary};
            font-family: {TYPE.mono};
            font-size: 11px;
            font-weight: 600;
            border: none;
            border-bottom: 1px solid {COLORS.border_subtle};
            border-right: 1px solid {COLORS.border_hairline};
            padding: 7px 10px;
        }}
        QHeaderView::section:vertical {{
            background: {COLORS.canvas};
            background-color: {COLORS.canvas};
            border: none;
        }}
        QTableCornerButton::section {{
            background: {COLORS.surface};
            background-color: {COLORS.surface};
            border: none;
            border-bottom: 1px solid {COLORS.border_subtle};
        }}
    """


def build_dark_palette():
    """Build a complete Crimson Graphite dark QPalette immune to light desktop Qt themes."""
    from PySide6.QtGui import QColor, QPalette

    pal = QPalette()
    c_canvas = QColor(COLORS.canvas)
    c_nav = QColor(COLORS.nav)
    c_surface = QColor(COLORS.surface)
    c_raised = QColor(COLORS.surface_raised)
    c_active = QColor(COLORS.surface_active)
    c_text = QColor(COLORS.text_primary)
    c_muted = QColor(COLORS.text_muted)
    c_cyan = QColor(COLORS.signal_cyan)

    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        txt = c_muted if group == QPalette.ColorGroup.Disabled else c_text
        pal.setColor(group, QPalette.ColorRole.Window, c_nav)
        pal.setColor(group, QPalette.ColorRole.WindowText, txt)
        pal.setColor(group, QPalette.ColorRole.Base, c_surface)
        pal.setColor(group, QPalette.ColorRole.AlternateBase, c_raised)
        pal.setColor(group, QPalette.ColorRole.ToolTipBase, c_surface)
        pal.setColor(group, QPalette.ColorRole.ToolTipText, c_text)
        pal.setColor(group, QPalette.ColorRole.Text, txt)
        pal.setColor(group, QPalette.ColorRole.Button, c_surface)
        pal.setColor(group, QPalette.ColorRole.ButtonText, txt)
        pal.setColor(group, QPalette.ColorRole.BrightText, c_cyan)
        pal.setColor(group, QPalette.ColorRole.Highlight, c_active)
        pal.setColor(group, QPalette.ColorRole.HighlightedText, c_cyan)
        pal.setColor(group, QPalette.ColorRole.Link, c_cyan)
        if hasattr(QPalette.ColorRole, "PlaceholderText"):
            pal.setColor(group, QPalette.ColorRole.PlaceholderText, c_muted)
        pal.setColor(group, QPalette.ColorRole.Dark, c_canvas)
    return pal


def apply_dark_combobox_popup(combo) -> None:
    """
    Force dark Crimson Graphite background & readable light text on QComboBox dropdown popups
    under all Linux Qt 6 styles (Fusion/Breeze/qt6ct/Wayland), preventing white-on-white popups.
    """
    from PySide6.QtWidgets import QListView

    pal = build_dark_palette()
    combo.setPalette(pal)

    view = combo.view()
    if view is None:
        view = QListView(combo)
        combo.setView(view)

    view.setPalette(pal)
    view.setAutoFillBackground(True)
    view.setStyleSheet(
        f"""
        QAbstractItemView {{
            background-color: {COLORS.surface};
            color: {COLORS.text_primary};
            border: 1px solid {COLORS.border_subtle};
            selection-background-color: {COLORS.surface_active};
            selection-color: {COLORS.signal_cyan};
            outline: none;
            padding: 4px;
        }}
        QAbstractItemView::item {{
            background-color: {COLORS.surface};
            color: {COLORS.text_primary};
            padding: 6px 10px;
            border-radius: 4px;
            min-height: 24px;
        }}
        QAbstractItemView::item:hover {{
            background-color: {COLORS.surface_raised};
            color: {COLORS.text_primary};
        }}
        QAbstractItemView::item:selected {{
            background-color: {COLORS.surface_active};
            color: {COLORS.signal_cyan};
        }}
        """
    )
    vp = view.viewport()
    if vp is not None:
        vp.setPalette(pal)
        vp.setAutoFillBackground(True)
    container = view.parentWidget()
    if container is not None and container is not combo:
        container.setPalette(pal)
        container.setAutoFillBackground(True)
        container.setStyleSheet(
            f"background-color: {COLORS.surface}; border: 1px solid {COLORS.border_subtle}; border-radius: 6px;"
        )


def apply_dark_palette(target) -> None:
    """Apply Crimson Graphite dark QPalette to a QApplication or QWidget and all nested QComboBox popups."""
    from PySide6.QtWidgets import QComboBox

    pal = build_dark_palette()
    target.setPalette(pal)
    if hasattr(target, "findChildren"):
        for cb in target.findChildren(QComboBox):
            apply_dark_combobox_popup(cb)
