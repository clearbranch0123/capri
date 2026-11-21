"""
CAPRI // Vector SVG Icon Registry.
Renders crisp, consistent monochrome & dual-tone inline SVG icons using PySide6.QtSvg
with zero network dependencies and clean HiDPI scaling.
"""

from __future__ import annotations

from functools import lru_cache
from PySide6.QtCore import Qt, QByteArray, QRectF
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .theme import COLORS


_SVG_PATHS: dict[str, str] = {
    "sidebar": (
        '<rect x="3" y="4" width="18" height="16" rx="2.5"/>'
        '<line x1="9" y1="4" x2="9" y2="20"/>'
    ),
    "plus": (
        '<line x1="12" y1="5" x2="12" y2="19"/>'
        '<line x1="5" y1="12" x2="19" y2="12"/>'
    ),
    "search": (
        '<circle cx="11" cy="11" r="7"/>'
        '<line x1="20" y1="20" x2="16.2" y2="16.2"/>'
    ),
    "sessions": (
        '<path d="M4 7h16M4 12h11M4 17h14"/>'
        '<circle cx="18.5" cy="12" r="1.5" fill="currentColor" stroke="none"/>'
    ),
    "memory": (
        '<ellipse cx="12" cy="6" rx="7" ry="3"/>'
        '<path d="M5 6v6c0 1.66 3.13 3 7 3s7-1.34 7-3V6"/>'
        '<path d="M5 12v6c0 1.66 3.13 3 7 3s7-1.34 7-3v-6"/>'
    ),
    "changelog": (
        '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
        '<polyline points="14 3 14 8 19 8"/>'
        '<line x1="9" y1="13" x2="15" y2="13"/>'
        '<line x1="9" y1="17" x2="13" y2="17"/>'
    ),
    "clear": (
        '<path d="M18 13L11 20H5l-2-2 9-9 6 4z"/>'
        '<path d="M12 9l5-5 4 4-5 5"/>'
        '<line x1="11" y1="20" x2="21" y2="20"/>'
    ),
    "reset": (
        '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/>'
        '<polyline points="3 3 3 8 8 8"/>'
    ),
    "settings": (
        '<line x1="4" y1="7" x2="20" y2="7"/>'
        '<circle cx="9" cy="7" r="2" fill="#0A0B0F"/>'
        '<line x1="4" y1="17" x2="20" y2="17"/>'
        '<circle cx="15" cy="17" r="2" fill="#0A0B0F"/>'
    ),
    "cpu": (
        '<rect x="6" y="6" width="12" height="12" rx="2"/>'
        '<rect x="9.5" y="9.5" width="5" height="5" rx="0.8"/>'
        '<line x1="9" y1="3" x2="9" y2="6"/><line x1="15" y1="3" x2="15" y2="6"/>'
        '<line x1="9" y1="18" x2="9" y2="21"/><line x1="15" y1="18" x2="15" y2="21"/>'
        '<line x1="3" y1="9" x2="6" y2="9"/><line x1="3" y1="15" x2="6" y2="15"/>'
        '<line x1="18" y1="9" x2="21" y2="9"/><line x1="18" y1="15" x2="21" y2="15"/>'
    ),
    "eye": (
        '<path d="M2.5 12S6 6 12 6s9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z"/>'
        '<circle cx="12" cy="12" r="2.7"/>'
    ),
    "eye-off": (
        '<path d="M17.94 17.94A10.1 10.1 0 0 1 12 19c-6 0-9.5-7-9.5-7a18.5 18.5 0 0 1 5.06-5.94"/>'
        '<path d="M9.9 5.24A9.2 9.2 0 0 1 12 5c6 0 9.5 7 9.5 7a18.5 18.5 0 0 1-2.16 3.19"/>'
        '<line x1="3" y1="3" x2="21" y2="21"/>'
    ),
    "mic": (
        '<rect x="9" y="3" width="6" height="11" rx="3"/>'
        '<path d="M5.5 11a6.5 6.5 0 0 0 13 0"/>'
        '<line x1="12" y1="17.5" x2="12" y2="21"/>'
        '<line x1="9" y1="21" x2="15" y2="21"/>'
    ),
    "send": (
        '<line x1="21" y1="3" x2="10" y2="14"/>'
        '<polygon points="21 3 14.5 21 10 14 3 9.5 21 3"/>'
    ),
    "chat": (
        '<path d="M21 14a3 3 0 0 1-3 3H8l-5 4V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3z"/>'
    ),
    "shield": (
        '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3z"/>'
    ),
    "code": (
        '<polyline points="8 7 3 12 8 17"/>'
        '<polyline points="16 7 21 12 16 17"/>'
        '<line x1="14" y1="5" x2="10" y2="19"/>'
    ),
    "heart": (
        '<path d="M12 20s-7-4.4-9-8.5C1.5 8.2 3.8 5 7.2 5c2 0 3.6 1.1 4.8 2.7C13.2 6.1 14.8 5 16.8 5 20.2 5 22.5 8.2 21 11.5 19 15.6 12 20 12 20z"/>'
    ),
    "stop": (
        '<rect x="6" y="6" width="12" height="12" rx="2.5" fill="currentColor" stroke="none"/>'
    ),
    "edit": (
        '<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>'
    ),
    "trash": (
        '<polyline points="3 6 5 6 21 6"/>'
        '<path d="M8 6V4a1.5 1.5 0 0 1 1.5-1.5h5A1.5 1.5 0 0 1 16 4v2"/>'
        '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'
    ),
    "chevron-right": '<polyline points="9 6 15 12 9 18"/>',
    "chevron-down": '<polyline points="6 9 12 15 18 9"/>',
    "more": (
        '<circle cx="5.5" cy="12" r="1.4" fill="currentColor" stroke="none"/>'
        '<circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/>'
        '<circle cx="18.5" cy="12" r="1.4" fill="currentColor" stroke="none"/>'
    ),
    "close": (
        '<line x1="6" y1="6" x2="18" y2="18"/>'
        '<line x1="18" y1="6" x2="6" y2="18"/>'
    ),
    "copy": (
        '<rect x="9" y="9" width="11" height="11" rx="2"/>'
        '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
    ),
    "check": '<polyline points="5 12.5 10 17.5 19 7"/>',
    "paperclip": (
        '<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>'
    ),
    "image": (
        '<rect x="3" y="4" width="18" height="16" rx="2"/>'
        '<circle cx="8.5" cy="9.5" r="1.5"/>'
        '<polyline points="21 16 16 11 5 20"/>'
    ),
    "file": (
        '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
        '<polyline points="14 2 14 8 20 8"/>'
    ),
    "terminal": (
        '<polyline points="5 8 10 12 5 16"/>'
        '<line x1="12" y1="16" x2="19" y2="16"/>'
    ),
    "sparkles": (
        '<path d="M12 3l1.7 5.3L19 10l-5.3 1.7L12 17l-1.7-5.3L5 10l5.3-1.7z"/>'
    ),
    "activity": (
        '<polyline points="2 12 6 12 9 5 15 19 18 12 22 12"/>'
    ),
    "download": (
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
        '<polyline points="7 10 12 15 17 10"/>'
        '<line x1="12" y1="15" x2="12" y2="3"/>'
    ),
    "upload": (
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
        '<polyline points="17 8 12 3 7 8"/>'
        '<line x1="12" y1="3" x2="12" y2="15"/>'
    ),
    "paste": (
        '<rect x="8" y="3" width="8" height="4" rx="1"/>'
        '<path d="M8 5H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/>'
    ),
    "layers": (
        '<polygon points="12 3 21 8 12 13 3 8 12 3"/>'
        '<polyline points="3 13 12 18 21 13"/>'
    ),
    "power": (
        '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/>'
        '<line x1="12" y1="2.5" x2="12" y2="12"/>'
    ),
    "capri-signal": (
        '<polygon points="12 2.5 21 12 12 21.5 3 12 12 2.5" stroke-width="1.6"/>'
        '<polyline points="7 12 10 12 11.6 8.5 13.4 15.5 15 12 17 12" stroke-width="1.7"/>'
    ),
}


@lru_cache(maxsize=256)
def render_svg_pixmap(
    name: str,
    color: str = COLORS.text_secondary,
    size: int = 16,
    stroke_width: float = 1.75,
    scale: int = 2,
) -> QPixmap:
    """Render a named inline SVG icon into a crisp HiDPI QPixmap."""
    body = _SVG_PATHS.get(name, _SVG_PATHS["capri-signal"])
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 24 24" fill="none" stroke="{color}" color="{color}" '
        f'stroke-width="{stroke_width}" stroke-linecap="round" stroke-linejoin="round">'
        f"{body}</svg>"
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    px_size = max(1, int(size * scale))
    pixmap = QPixmap(px_size, px_size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px_size, px_size))
    painter.end()
    pixmap.setDevicePixelRatio(float(scale))
    return pixmap


def get_icon(
    name: str,
    color: str = COLORS.text_secondary,
    size: int = 16,
    stroke_width: float = 1.75,
) -> QIcon:
    """Return a QIcon backed by the cached HiDPI SVG pixmap."""
    return QIcon(render_svg_pixmap(name, color=color, size=size, stroke_width=stroke_width))
