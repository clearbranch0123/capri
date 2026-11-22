"""
Capri v3.3 Stage 2 — Spatial Window Surface Map (Niri & Hyprland).

Builds deterministic logical-pixel landing surfaces ("shelves") that Capri can land on,
perch/lounge on, ride along with, and fall off when the underlying window closes:
- Always includes the monitor floor (`SurfaceKind.FLOOR`).
- Hyprland: floating window top edges + tiled window top edges with enough headroom.
- Niri: floating window top edges + horizontal split seams between vertically stacked
  tiled windows in the same column (`pos_in_scrolling_layout = [col, row >= 2]`), while
  keeping top-row tiled columns unobstructed so Capri never blocks code/text headers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..core.compositor import MonitorWorkArea


class SurfaceKind:
    FLOOR = "floor"
    FLOATING_TOP = "floating_top"
    TILED_TOP = "tiled_top"
    NIRI_SPLIT_SEAM = "niri_split_seam"


@dataclass(frozen=True)
class LandingSurface:
    """A horizontal logical-pixel ledge Capri can stand or perch on."""

    surface_id: str
    kind: str
    x_min: float
    x_max: float
    y: float
    window_id: Any = None
    app_id: str = ""
    preferred_pose: str = "normal"  # "normal" (floor) | "perch" (window shelf)
    priority: int = 0

    @property
    def width(self) -> float:
        return max(0.0, self.x_max - self.x_min)

    def contains_foot_x(self, foot_x: float, margin: float = 0.0) -> bool:
        lo = self.x_min - margin
        hi = self.x_max + margin
        if lo > hi:
            mid = (self.x_min + self.x_max) * 0.5
            return abs(foot_x - mid) <= 24.0
        return lo <= foot_x <= hi

    def clamp_avatar_x(self, x: float, avatar_w: float) -> float:
        half = avatar_w * 0.5
        lo = self.x_min - half + 28.0
        hi = self.x_max - half - 28.0
        if lo > hi:
            return (self.x_min + self.x_max) * 0.5 - half
        return max(lo, min(hi, x))


def _extract_rect(win: dict[str, Any]) -> Optional[tuple[float, float, float, float]]:
    """Extract `(x, y, w, h)` in logical pixels from a normalized window dict, if available."""
    layout = win.get("layout") if isinstance(win.get("layout"), dict) else {}
    pos = layout.get("tile_pos_in_workspace_view")
    size = layout.get("tile_size") or layout.get("window_size")
    if not (isinstance(pos, (list, tuple)) and len(pos) >= 2):
        return None
    if not (isinstance(size, (list, tuple)) and len(size) >= 2):
        return None
    try:
        x, y = float(pos[0]), float(pos[1])
        w, h = float(size[0]), float(size[1])
    except (TypeError, ValueError):
        return None
    if w <= 0.0 or h <= 0.0:
        return None
    return x, y, w, h


def _extract_size(win: dict[str, Any]) -> Optional[tuple[float, float]]:
    layout = win.get("layout") if isinstance(win.get("layout"), dict) else {}
    size = layout.get("tile_size") or layout.get("window_size")
    if not (isinstance(size, (list, tuple)) and len(size) >= 2):
        return None
    try:
        w, h = float(size[0]), float(size[1])
    except (TypeError, ValueError):
        return None
    if w <= 0.0 or h <= 0.0:
        return None
    return w, h


def build_surface_map(
    windows: list[dict[str, Any]],
    monitor: MonitorWorkArea,
    *,
    compositor_kind: str = "niri",
    active_workspace_id: Any = None,
    avatar_pid: Optional[int] = None,
    min_shelf_width: float = 180.0,
    min_headroom: float = 110.0,
    niri_stack_gap: float = 16.0,
    stage_origin_offset: tuple[float, float] = (0.0, 0.0),
) -> list[LandingSurface]:
    """
    Build the ordered list of landing surfaces for `monitor` in logical stage pixels.

    - Always contains the monitor floor (`SurfaceKind.FLOOR`).
    - Filters out Capri's own window, fullscreen windows, and windows on inactive workspaces.
    - Adapts cleanly to both Hyprland (floating + tiled top edges) and Niri (floating top edges
      + vertical column split seams without cluttering top-row tiled headers).
    """
    ox, oy = float(stage_origin_offset[0]), float(stage_origin_offset[1])
    floor = LandingSurface(
        surface_id=f"floor:{monitor.name}",
        kind=SurfaceKind.FLOOR,
        x_min=monitor.work_left,
        x_max=monitor.work_right,
        y=monitor.work_bottom,
        window_id=None,
        app_id="",
        preferred_pose="normal",
        priority=0,
    )
    surfaces: list[LandingSurface] = [floor]

    if not windows:
        return surfaces

    visible: list[dict[str, Any]] = []
    for w in windows:
        if not isinstance(w, dict):
            continue
        if w.get("app_id") == "capri-avatar":
            continue
        if avatar_pid is not None and w.get("pid") == avatar_pid:
            continue
        if w.get("is_fullscreen"):
            continue
        if active_workspace_id is not None:
            w_ws = w.get("workspace_id")
            if w_ws is not None and w_ws != active_workspace_id:
                continue
        visible.append(w)

    min_valid_y = monitor.work_top + float(min_headroom)
    max_valid_y = monitor.work_bottom - 24.0

    def _maybe_add_shelf(
        *,
        surf_id: str,
        kind: str,
        x0: float,
        x1: float,
        y: float,
        win: dict[str, Any],
        priority: int,
    ) -> None:
        clamped_x0 = max(monitor.work_left, x0)
        clamped_x1 = min(monitor.work_right, x1)
        if (clamped_x1 - clamped_x0) < min_shelf_width:
            return
        if not (min_valid_y <= y <= max_valid_y):
            return
        surfaces.append(
            LandingSurface(
                surface_id=surf_id,
                kind=kind,
                x_min=clamped_x0,
                x_max=clamped_x1,
                y=y,
                window_id=win.get("id"),
                app_id=str(win.get("app_id") or ""),
                preferred_pose="perch",
                priority=priority,
            )
        )

    if compositor_kind == "hyprland":
        for w in visible:
            rect = _extract_rect(w)
            if rect is None:
                continue
            wx, wy, ww, _wh = rect
            sx0 = wx - ox
            sx1 = wx + ww - ox
            sy = wy - oy
            is_flt = bool(w.get("is_floating"))
            _maybe_add_shelf(
                surf_id=f"win:{w.get('id')}:top",
                kind=SurfaceKind.FLOATING_TOP if is_flt else SurfaceKind.TILED_TOP,
                x0=sx0,
                x1=sx1,
                y=sy,
                win=w,
                priority=3 if is_flt else 2,
            )
    else:
        # Niri (or generic Wayland fallback):
        # 1. Floating windows always expose exact workspace-view coordinates
        niri_columns: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        for w in visible:
            if w.get("is_floating"):
                rect = _extract_rect(w)
                if rect is None:
                    continue
                wx, wy, ww, _wh = rect
                _maybe_add_shelf(
                    surf_id=f"win:{w.get('id')}:top",
                    kind=SurfaceKind.FLOATING_TOP,
                    x0=wx - ox,
                    x1=wx + ww - ox,
                    y=wy - oy,
                    win=w,
                    priority=3,
                )
            else:
                layout = w.get("layout") if isinstance(w.get("layout"), dict) else {}
                scroll_pos = layout.get("pos_in_scrolling_layout")
                if isinstance(scroll_pos, (list, tuple)) and len(scroll_pos) >= 2:
                    try:
                        col = int(scroll_pos[0])
                        row = int(scroll_pos[1])
                    except (TypeError, ValueError):
                        continue
                    niri_columns.setdefault(col, []).append((row, w))

        # 2. Vertically stacked Niri windows (row >= 2 in same column) expose horizontal split seams.
        # Safety invariant: when Niri omits `tile_pos_in_workspace_view` on a multi-column workspace,
        # scrolled columns have unknown viewport X — skip unverified multi-column seams rather than
        # inventing phantom off-screen shelves! Single-column workspaces have deterministic viewport X.
        sorted_cols = sorted(niri_columns.keys())
        allow_estimated_x = (len(sorted_cols) == 1)
        for col_idx in sorted_cols:
            stack = sorted(niri_columns[col_idx], key=lambda pair: pair[0])
            first_size = _extract_size(stack[0][1])
            col_w = first_size[0] if first_size else 0.0
            if allow_estimated_x and 0.0 < col_w < (monitor.work_width - 40.0):
                col_x_default = monitor.work_left + (monitor.work_width - col_w) * 0.5
            else:
                col_x_default = monitor.work_left + 16.0

            y_cursor = monitor.work_top + 16.0
            for idx, (row_num, w) in enumerate(stack):
                rect = _extract_rect(w)
                size = _extract_size(w)
                if idx > 0 and row_num >= 2:
                    if rect is not None:
                        wx, wy, ww, _wh = rect
                        seam_x0, seam_x1, seam_y = wx - ox, wx + ww - ox, wy - oy
                    elif allow_estimated_x and size is not None:
                        ww = size[0]
                        seam_x0 = col_x_default
                        seam_x1 = col_x_default + ww
                        seam_y = y_cursor
                    else:
                        seam_x0 = seam_x1 = seam_y = 0.0
                    if seam_x1 > seam_x0:
                        _maybe_add_shelf(
                            surf_id=f"niri_seam:{w.get('id')}",
                            kind=SurfaceKind.NIRI_SPLIT_SEAM,
                            x0=seam_x0,
                            x1=seam_x1,
                            y=seam_y,
                            win=w,
                            priority=2,
                        )
                if size is not None:
                    y_cursor += size[1] + niri_stack_gap

    # Sort descending by priority, then ascending by y (highest shelf first)
    surfaces.sort(key=lambda s: (-s.priority, s.y))
    return surfaces


def find_surface_by_window_id(
    surfaces: list[LandingSurface],
    window_id: Any,
) -> Optional[LandingSurface]:
    """Find the updated shelf corresponding to a previously perched `window_id`."""
    if window_id is None:
        return None
    for s in surfaces:
        if s.window_id == window_id:
            return s
    return None
