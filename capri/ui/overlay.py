import sys
import os
import signal
import math
import json
import ctypes
import argparse
import random
import threading
import time
from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt, QTimer, QPoint, QPointF, QRectF, QUrl, QLockFile, Signal
from PySide6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QRadialGradient,
    QPainterPath,
    QFont,
    QPixmap,
    QCursor,
    QAction,
)
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QStackedWidget,
    QVBoxLayout,
    QMenu,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtQuickWidgets import QQuickWidget
from .avatar_awareness import AvatarAwarenessEngine
from .avatar_brain import AvatarLifeBrain, BrainAction
from .avatar_physics import AvatarPhysics2D, PhysicsState, PhysicsStepResult
from .avatar_popover import CapriAvatarQuickPopover
from .sprite_view import AvatarV2Canvas
from .surface_map import LandingSurface, SurfaceKind, build_surface_map
from .dashboard import CapriRelicDashboard
from .locomotion import LocomotionController, LocomotionMode
from .wayland_layer import (
    BTN_LEFT,
    BTN_RIGHT,
    WaylandLayerStage,
    probe_layer_shell_support,
)
from ..core.compositor import (
    MonitorWorkArea,
    compositor,
    find_adjacent_monitor,
    map_y_between_monitors,
)
from ..core.config import load_config
from ..core.presence import (
    GameModeState,
    MediaPlaybackState,
    detect_game_mode,
    query_mpris_playback,
    resolve_avatar_auto_pose,
)

SOCKET_PATH = "/tmp/capri_avatar.sock"
PID_PATH = "/tmp/capri_avatar.pid"


class AvatarState:
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    PLAYFUL = "playful"


class Avatar2DCanvas(QWidget):
    """2D-холст с киберпанк HUD-кольцами и 5 эмоциональными спрайтами."""
    def __init__(self, parent_widget, size: int = 240):
        super().__init__(parent_widget)
        self.host = parent_widget
        self.avatar_size = size
        from ..core.resources import get_asset_path
        self.sprites_dir = get_asset_path("sprites")
        self.sprites: dict[str, QPixmap] = {}
        self._load_sprites()

    def _load_sprites(self):
        state_map = {
            AvatarState.IDLE: "idle.png",
            AvatarState.LISTENING: "listening.png",
            AvatarState.THINKING: "thinking.png",
            AvatarState.SPEAKING: "speaking.png",
            AvatarState.PLAYFUL: "playful.png",
        }
        circle_diameter = self.avatar_size - 44
        for st, fname in state_map.items():
            fpath = self.sprites_dir / fname
            if fpath.exists():
                pix = QPixmap(str(fpath))
                if not pix.isNull():
                    scaled = pix.scaled(
                        circle_diameter,
                        circle_diameter,
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    self.sprites[st] = scaled

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        center_x = self.width() / 2.0
        center_y = (self.avatar_size / 2.0) + 38.0
        radius = (self.avatar_size - 48) / 2.0
        state = self.host.state

        if state == AvatarState.IDLE:
            primary_col = QColor(0, 229, 255)
            accent_col = QColor(138, 43, 226, 160)
            pulse_mod = math.sin(self.host.pulse_phase) * 3.0
        elif state == AvatarState.LISTENING:
            primary_col = QColor(255, 0, 127)
            accent_col = QColor(255, 60, 0, 180)
            pulse_mod = math.sin(self.host.pulse_phase * 2.0) * 6.0
        elif state == AvatarState.THINKING:
            primary_col = QColor(255, 215, 0)
            accent_col = QColor(0, 229, 255, 180)
            pulse_mod = math.sin(self.host.pulse_phase * 3.5) * 4.0
        elif state == AvatarState.SPEAKING:
            primary_col = QColor(57, 255, 20)
            accent_col = QColor(0, 229, 255, 180)
            pulse_mod = abs(math.sin(self.host.pulse_phase * 4.0)) * 8.0
        else:
            primary_col = QColor(217, 70, 239)
            accent_col = QColor(57, 255, 20, 180)
            pulse_mod = math.sin(self.host.pulse_phase * 1.5) * 4.5

        # 1. Верхний HUD-бабл
        if self.host.caption_text:
            painter.save()
            caption_w = min(self.width() - 10, len(self.host.caption_text) * 7.5 + 24)
            caption_h = 24
            caption_x = (self.width() - caption_w) / 2.0
            caption_y = 6.0
            bg_col = QColor(10, 12, 18, 220)
            painter.setBrush(QBrush(bg_col))
            painter.setPen(QPen(primary_col, 1.2))
            rect = QRectF(caption_x, caption_y, caption_w, caption_h)
            painter.drawRoundedRect(rect, 12, 12)
            painter.setPen(QColor(240, 245, 255))
            font = QFont("monospace", 8, QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.host.caption_text)
            painter.restore()

        # 2. Свечение
        glow_rad = radius + 18.0 + pulse_mod
        grad = QRadialGradient(center_x, center_y, glow_rad)
        glow_start = QColor(primary_col)
        glow_start.setAlpha(70)
        grad.setColorAt(0.0, glow_start)
        grad.setColorAt(0.8, accent_col)
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPoint(int(center_x), int(center_y)), int(glow_rad), int(glow_rad))

        # 3. Вращающиеся кольца
        painter.save()
        painter.translate(center_x, center_y)
        painter.rotate(self.host.rotation_angle)
        pen_outer = QPen(primary_col, 2.0, Qt.PenStyle.DashLine)
        pen_outer.setDashPattern([8, 6, 16, 6])
        painter.setPen(pen_outer)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(-radius - 8, -radius - 8, (radius + 8) * 2, (radius + 8) * 2))

        tick_pen = QPen(primary_col, 2.0)
        painter.setPen(tick_pen)
        for angle in range(0, 360, 45):
            rad_ang = math.radians(angle)
            x1 = (radius + 5) * math.cos(rad_ang)
            y1 = (radius + 5) * math.sin(rad_ang)
            x2 = (radius + 12) * math.cos(rad_ang)
            y2 = (radius + 12) * math.sin(rad_ang)
            painter.drawLine(QPoint(int(x1), int(y1)), QPoint(int(x2), int(y2)))

        painter.rotate(-self.host.rotation_angle * 2.2)
        pen_inner = QPen(accent_col, 1.2, Qt.PenStyle.DotLine)
        painter.setPen(pen_inner)
        painter.drawEllipse(QRectF(-radius - 3, -radius - 3, (radius + 3) * 2, (radius + 3) * 2))
        painter.restore()

        # 4. Эквалайзер речи
        if state == AvatarState.SPEAKING:
            painter.save()
            painter.translate(center_x, center_y)
            num_bars = 16
            eq_pen = QPen(QColor(57, 255, 20, 220), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            painter.setPen(eq_pen)
            for b in range(num_bars):
                ang = (360.0 / num_bars) * b
                rad_b = math.radians(ang)
                bar_len = 4.0 + math.sin(self.host.pulse_phase * 5.0 + b) * 8.0 + abs(math.cos(b * 1.5)) * 6.0
                x_start = (radius + 14) * math.cos(rad_b)
                y_start = (radius + 14) * math.sin(rad_b)
                x_end = (radius + 14 + bar_len) * math.cos(rad_b)
                y_end = (radius + 14 + bar_len) * math.sin(rad_b)
                painter.drawLine(QPoint(int(x_start), int(y_start)), QPoint(int(x_end), int(y_end)))
            painter.restore()

        # 5. Спрайт персонажа
        active_pixmap = self.sprites.get(state) or self.sprites.get(AvatarState.IDLE)
        painter.setBrush(QBrush(QColor(14, 16, 24)))
        painter.setPen(QPen(primary_col, 2.5))
        painter.drawEllipse(QPoint(int(center_x), int(center_y)), int(radius), int(radius))

        if active_pixmap and not active_pixmap.isNull():
            path = QPainterPath()
            path.addEllipse(center_x - radius, center_y - radius, radius * 2, radius * 2)
            painter.setClipPath(path)

            # Эффект живого дыхания / легкого покачивания
            bob_y = math.sin(self.host.pulse_phase) * 2.5
            glitch_x = (math.sin(self.host.pulse_phase * 11.0) * 2.0) if state == AvatarState.THINKING else 0.0

            pix_x = center_x - active_pixmap.width() / 2.0 + glitch_x
            pix_y = center_y - active_pixmap.height() / 2.0 + bob_y
            painter.drawPixmap(int(pix_x), int(pix_y), active_pixmap)
            painter.setClipping(False)

        # 6. Статус
        painter.save()
        status_bg = QColor(10, 12, 18, 200)
        painter.setBrush(QBrush(status_bg))
        painter.setPen(QPen(primary_col, 1.0))
        status_w = 64
        status_h = 16
        status_rect = QRectF(center_x - status_w / 2.0, center_y + radius - 12.0, status_w, status_h)
        painter.drawRoundedRect(status_rect, 6, 6)
        painter.setPen(QColor(primary_col))
        st_font = QFont("monospace", 7, QFont.Weight.Bold)
        painter.setFont(st_font)
        painter.drawText(status_rect, Qt.AlignmentFlag.AlignCenter, state.upper())
        painter.restore()


class CapriAvatarWidget(QWidget):
    presence_snapshot_ready = Signal(object, object)  # (MediaPlaybackState, GameModeState)

    _POKE_QUOTES = (
        "Эй, руки прочь от голограммы!",
        "Да-да, я здесь. Что стряслось?",
        "Щекотно же... ну, почти.",
        "Сигнал стабильный. Проверяешь, жива ли я?",
        "Хе-хе, скучно стало?",
    )
    _RAPID_POKE_QUOTES = (
        "Эй, хватит тыкать в конструкт — я тебе не тачскрин!",
        "Ещё раз тыкнешь — переименую все твои вкладки.",
        "Спокойно, шеф, биочип не кнопка турбо!",
    )
    _SHAKE_QUOTES = (
        "Стоп-стоп, у меня гироскоп сейчас вылетит!",
        "Не тряси конструкт, энграмма рассыплется!",
        "Эй! Я тебе не снежный шар!",
    )
    _DROP_QUOTES = (
        "О, новое местечко. Тут обзор даже лучше.",
        "Переезжаем? Ладно, располагаюсь.",
        "Якорь закреплён. Нормальная точка.",
    )
    _WALL_SPLAT_QUOTES = (
        "Ауч! Предупреждать надо про стену!",
        "Эй, аккуратнее с голограммой — экран не резиновый!",
        "Почти пробила границу монитора...",
    )
    _PIN_QUOTES = (
        "Закрепилась в воздухе. Гравитация на паузе.",
        "Зависла тут. Отпустишь потом, шеф.",
    )
    _LOST_FOOTING_QUOTES = (
        "Эй! Кто выдернул окно из-под голограммы?!",
        "Окно пропало — ладно, полетели!",
        "Предупреждать надо, прежде чем пол убирать!",
    )

    def __init__(self, mode: str = "2d", parent_pid: int | None = None, size: int = 300):
        super().__init__()
        self.mode = mode  # "2d" (v2 sprite), "classic" (circle), "3d"
        self.parent_pid = parent_pid
        self.avatar_size = size
        self.state = AvatarState.IDLE
        self.pulse_phase = 0.0
        self.rotation_angle = 0.0
        self.caption_text = ""
        self.caption_timer = 0
        self.drag_position = QPoint()

        try:
            cfg = load_config()
            self.auto_poses_enabled: bool = bool(getattr(cfg.ui, "auto_avatar_poses", True))
            self.game_mode_enabled: bool = bool(getattr(cfg.ui, "game_mode_enabled", True))
            self.reactions_enabled: bool = bool(getattr(cfg.ui, "avatar_reactions_enabled", True))
            self.reduced_motion: bool = bool(getattr(cfg.ui, "reduced_motion", False))
            self.physics_enabled: bool = bool(getattr(cfg.ui, "avatar_physics_enabled", True))
            self.autonomous_life_enabled: bool = bool(getattr(cfg.ui, "avatar_autonomous_life", True))
        except Exception:
            self.auto_poses_enabled = True
            self.game_mode_enabled = True
            self.reactions_enabled = True
            self.reduced_motion = False
            self.physics_enabled = True
            self.autonomous_life_enabled = True

        self.physics = AvatarPhysics2D(
            avatar_w=300.0,
            avatar_h=420.0,
            reduced_motion=self.reduced_motion,
            physics_enabled=self.physics_enabled,
        )
        self.brain = AvatarLifeBrain(autonomous_enabled=self.autonomous_life_enabled)
        try:
            _cfg_aw = load_config()
            _aw_user = str(getattr(getattr(_cfg_aw, "companion", None), "user_name", "User") or "User")
            _aw_mode = str(getattr(getattr(_cfg_aw, "companion", None), "mode", "normal") or "normal")
        except Exception:
            _aw_user = "User"
            _aw_mode = "normal"
        self.companion_mode: str = _aw_mode
        self.awareness = AvatarAwarenessEngine(user_name=_aw_user, now=time.time())
        self._auto_walk_target: Optional[tuple[float, float]] = None
        self._auto_walk_surface: Optional[LandingSurface] = None
        self._auto_walk_ipc_accum: float = 0.0
        self._autonomous_pose: Optional[str] = None
        self._drag_active: bool = False
        self._drag_press_local: Optional[QPointF] = None
        self._drag_latest_local: Optional[QPointF] = None
        self._drag_ignore_until: float = 0.0
        self._drag_start_coords: tuple[float, float] = (0.0, 0.0)
        self._drag_last_ts: float = 0.0
        self._press_shift_held: bool = False
        self._verified_avatar_id = None
        self._last_anim_ts: float = time.monotonic()
        self._active_monitor: Optional[MonitorWorkArea] = None
        self._known_monitors: list[MonitorWorkArea] = []
        self._cached_surfaces: list[LandingSurface] = []

        # Pure-Wayland zwlr_layer_shell_v1 60/165 FPS transparent click-through stage
        self.layer_stage: Optional[WaylandLayerStage] = None
        self.using_layer_stage: bool = False
        self._layer_opacity: float = 1.0
        self._layer_dragging: bool = False
        self._layer_grab_offset: tuple[float, float] = (150.0, 200.0)
        self._layer_press_pos: tuple[float, float] = (0.0, 0.0)
        self._layer_last_click_ts: float = 0.0
        self._layer_last_click_pos: tuple[float, float] = (0.0, 0.0)
        self._layer_stay_ws_id: Optional[int] = None
        self._layer_hidden_for_ws: bool = False

        # Fallback timer for offscreen / non-layer-shell testing
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(50)
        self._drag_timer.timeout.connect(self._on_drag_tick)

        self.power_saver_active: bool = False
        self.game_mode_override: Optional[bool] = None  # None=auto, True=forced ON, False=forced OFF
        self.manual_pose_override: Optional[str] = None
        self.media_state: MediaPlaybackState = MediaPlaybackState()
        self.game_state: GameModeState = GameModeState()
        self._last_announced_track: str = ""
        self._last_interaction_ts: float = time.time()
        self._presence_poll_inflight: bool = False

        # Interactive poke / drag / shake state
        self._press_global_pos: Optional[QPoint] = None
        self._press_ts: float = 0.0
        self._drag_distance: float = 0.0
        self._drag_last_x: Optional[int] = None
        self._shake_last_dir: int = 0
        self._shake_reversals: int = 0
        self._shake_window_start: float = 0.0
        self._shake_triggered_this_drag: bool = False
        self._poke_count: int = 0
        self._last_poke_ts: float = 0.0
        self._last_reaction_ts: float = 0.0

        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(230)
        self._click_timer.timeout.connect(self.trigger_poke_reaction)

        # Киберпанк Relic Dashboard (v0.2.0) & Crimson Graphite Quick Popover (v3.3)
        self.dashboard = CapriRelicDashboard()
        self.quick_popover = CapriAvatarQuickPopover(None)
        self._wire_quick_popover()

        self._init_window()
        self._init_views()
        self._init_ipc_server()
        self._init_timers()
        self._init_locomotion()
        self._init_layer_stage()
        self.presence_snapshot_ready.connect(self._apply_presence_snapshot)

    def _init_locomotion(self):
        self.locomotion = LocomotionController(self)
        self.locomotion.caption_requested.connect(lambda txt: self.set_state("playful", txt))
        self.locomotion.glitch_requested.connect(self._trigger_teleport_glitch)
        self.locomotion.opacity_requested.connect(self.setWindowOpacity)
        if hasattr(self.locomotion, "observer") and self.locomotion.observer:
            self.locomotion.observer.fullscreen_changed.connect(self._on_compositor_fullscreen)
            self.locomotion.observer.focus_changed.connect(self._on_compositor_focus_changed)
            self.locomotion.observer.workspace_changed.connect(self._on_layer_workspace_changed)

    def _init_layer_stage(self):
        """
        Prefer native `zwlr_layer_shell_v1` transparent click-through overlay on Wayland
        compositors (Niri, Hyprland, Sway) for 60/165 FPS cursor-locked drag & ballistics
        with ZERO compositor window-move IPC calls.
        """
        if self.mode != "2d":
            return
        if os.environ.get("CAPRI_DISABLE_LAYER_SHELL", "").strip().lower() in ("1", "true", "yes"):
            return
        if not probe_layer_shell_support():
            return
        stage = WaylandLayerStage(self, namespace="capri-avatar")
        stage.configured.connect(self._on_layer_configured)
        stage.pointer_press.connect(self._on_layer_pointer_press)
        stage.pointer_move.connect(self._on_layer_pointer_move)
        stage.pointer_release.connect(self._on_layer_pointer_release)
        stage.outputs_changed.connect(self._on_layer_outputs_changed)
        stage.output_switched.connect(self._on_layer_output_switched)
        stage.closed.connect(self._on_layer_closed)
        preferred = ""
        try:
            self._known_monitors = compositor.get_monitors()
            focused_m = next((m for m in self._known_monitors if m.is_focused), None)
            if focused_m is not None:
                preferred = focused_m.name
        except Exception:
            self._known_monitors = []
        if not stage.start(preferred_output=preferred):
            return

        self.layer_stage = stage
        self.using_layer_stage = True
        app_inst = QApplication.instance()
        if app_inst is not None:
            app_inst.setQuitOnLastWindowClosed(False)
        # Zero MoveFloatingWindow IPC needed — all motion renders directly inside the layer stage!
        self.physics.set_ipc_available(False)

        init_x = max(0.0, float(stage.width - 330))
        init_y = max(0.0, float(stage.height - 420))
        self.physics.sync_external_position(init_x, init_y)
        self.locomotion.home_coords = (init_x, init_y)
        self.locomotion.anchor_coords = (init_x, init_y)
        try:
            act_ws = self.locomotion.observer.get_active_workspace()
            if isinstance(act_ws, dict) and act_ws.get("id") is not None:
                self._layer_stay_ws_id = int(act_ws["id"])
        except Exception:
            pass
        self._render_layer_frame()

    def _stage_monitor(self) -> MonitorWorkArea:
        """
        Return logical stage bounds for physics clamping.
        Note: In `zwlr_layer_shell_v1` mode, the surface is currently anchored to a single
        focused output in surface-local `[0..width, 0..height]` logical coordinates; multi-monitor
        window migration is handled separately from the floating-window fallback.
        """
        if self.layer_stage is not None and self.layer_stage.is_active:
            return MonitorWorkArea(
                name="layer-stage",
                x=0.0,
                y=0.0,
                width=float(self.layer_stage.width),
                height=float(self.layer_stage.height),
                is_focused=True,
            )
        return compositor.get_monitor_for_point(
            self.physics.x + self.width() / 2.0,
            self.physics.y + self.height() / 2.0,
        )

    def set_layer_position(self, x: float, y: float) -> None:
        """Directly place Capri inside the Wayland layer stage (0 compositor IPC calls)."""
        mon = self._stage_monitor()
        min_x, max_x, min_y, max_y = self.physics._clamp_bounds(mon)
        cx = max(min_x, min(max_x, float(x)))
        cy = max(min_y, min(max_y, float(y)))
        self.physics.sync_external_position(cx, cy)
        self._layer_hidden_for_ws = False
        self._render_layer_frame()

    def _on_layer_configured(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            return
        mon = self._stage_monitor()
        min_x, max_x, min_y, max_y = self.physics._clamp_bounds(mon)
        self.physics.sync_external_position(
            max(min_x, min(max_x, self.physics.x)),
            max(min_y, min(max_y, self.physics.y)),
        )
        self.locomotion.home_coords = (max(0.0, float(width - 330)), max(0.0, float(height - 420)))
        self._render_layer_frame()

    def _on_layer_workspace_changed(self, ws_idx: int, ws_id: int) -> None:
        if not self.using_layer_stage or self.layer_stage is None:
            return
        if self.locomotion.mode == LocomotionMode.FOLLOW:
            self._layer_stay_ws_id = ws_id
            if self._layer_hidden_for_ws:
                self._layer_hidden_for_ws = False
                self._render_layer_frame()
            return
        # STAY mode: keep Capri anchored to her current workspace
        if self._layer_stay_ws_id is None:
            self._layer_stay_ws_id = ws_id
            return
        should_hide = (ws_id != self._layer_stay_ws_id)
        if should_hide != self._layer_hidden_for_ws:
            self._layer_hidden_for_ws = should_hide
            if should_hide:
                self.layer_stage.clear_frame()
            else:
                self._render_layer_frame()

    def _on_layer_closed(self) -> None:
        self.using_layer_stage = False
        self.layer_stage = None
        if not self.isVisible():
            self.show()

    def _on_layer_outputs_changed(self, _names: list) -> None:
        try:
            self._known_monitors = compositor.get_monitors()
        except Exception:
            pass
        self._sync_quick_popover()

    def _on_layer_output_switched(self, _output_name: str) -> None:
        try:
            self._known_monitors = compositor.get_monitors()
        except Exception:
            pass
        self._cached_surfaces = []
        self._sync_quick_popover()

    def _current_compositor_monitor(self) -> Optional[MonitorWorkArea]:
        if not self._known_monitors:
            return None
        active_name = self.layer_stage.active_output_name if self.layer_stage is not None else ""
        if active_name:
            for m in self._known_monitors:
                if m.name == active_name:
                    return m
        return next((m for m in self._known_monitors if m.is_focused), self._known_monitors[0])

    def _adjacent_monitor_flags(self) -> tuple[bool, bool]:
        if not self.using_layer_stage or self.layer_stage is None or len(self._known_monitors) < 2:
            return False, False
        cur = self._current_compositor_monitor()
        if cur is None:
            return False, False
        y_ref = cur.work_top + self.physics.y + 210.0
        has_left = find_adjacent_monitor(cur, -1, y_ref, self._known_monitors) is not None
        has_right = find_adjacent_monitor(cur, 1, y_ref, self._known_monitors) is not None
        return has_left, has_right

    def _migrate_layer_to_monitor(self, target_mon: MonitorWorkArea, *, side: int = 0) -> bool:
        """
        Migrate active Wayland layer stage to `target_mon`:
        - `side in (-1, 1)`: seamless cross-monitor ballistic flight preserving `(vx, vy)` momentum!
        - `side == 0`: deliberate hop via UI, landing calmly near home corner on `target_mon`.
        """
        if not self.using_layer_stage or self.layer_stage is None:
            return False
        prev_mon = self._current_compositor_monitor()
        saved_vx = self.physics.vx
        saved_vy = self.physics.vy
        saved_y = self.physics.y

        if not self.layer_stage.switch_output(target_mon.name):
            return False

        stage_w = float(self.layer_stage.width or target_mon.work_width)
        stage_h = float(self.layer_stage.height or target_mon.work_height)
        self.physics.active_surface = None
        self.physics.perched = False
        self._cached_surfaces = []

        if side in (-1, 1):
            if prev_mon is not None:
                new_y = map_y_between_monitors(saved_y, prev_mon, target_mon, avatar_h=420.0)
            else:
                new_y = max(0.0, min(stage_h - 420.0, saved_y))
            new_x = max(0.0, stage_w - 304.0) if side < 0 else 4.0
            self.physics.x = new_x
            self.physics.y = max(0.0, min(max(0.0, stage_h - 420.0), new_y))
            self.physics.vx = saved_vx
            self.physics.vy = saved_vy
            self.physics.state = PhysicsState.FALLING
        else:
            nx = max(0.0, stage_w - 330.0)
            ny = max(0.0, stage_h - 420.0)
            self.physics.sync_external_position(nx, ny)
            self.locomotion.home_coords = (nx, ny)
            self.locomotion.anchor_coords = (nx, ny)
            if hasattr(self, "canvas_v2"):
                self.canvas_v2.trigger_glitch()
        self._render_layer_frame()
        return True

    def _compute_avatar_hitbox(self, draw_x: int, draw_y: int) -> tuple[int, int, int, int]:
        """
        Compute a tight Wayland click/drag hitbox matching Capri's visible sprite silhouette
        inside the 300x420 canvas (leaves speech-bubble sky above her head & side margins
        100% click-through so you never grab empty air!).
        """
        pose = self.canvas_v2.pose if hasattr(self, "canvas_v2") else "normal"
        if pose in ("perch", "perch_music", "lounge") and self.physics.state == PhysicsState.IDLE:
            return (draw_x + 70, draw_y + 114, 160, 280)
        return (draw_x + 64, draw_y + 96, 172, 316)

    def _render_layer_frame(self) -> None:
        if not self.using_layer_stage or self.layer_stage is None or not self.layer_stage.is_active:
            return
        if self._layer_hidden_for_ws:
            return
        interactive = not self.power_saver_active
        dx = max(0, min(self.layer_stage.width - 300, int(round(self.physics.x))))
        dy = max(0, min(self.layer_stage.height - 420, int(round(self.physics.y))))
        hitbox = self._compute_avatar_hitbox(dx, dy)
        self.layer_stage.render_avatar(
            self.canvas_v2.render_to_painter,
            x=dx,
            y=dy,
            w=300,
            h=420,
            opacity=self._layer_opacity,
            interactive=interactive,
            full_input=self._layer_dragging,
            input_rect=hitbox,
        )

    def _on_layer_pointer_press(self, px: float, py: float, button: int, shift_held: bool) -> None:
        if button == BTN_RIGHT:
            self._show_context_menu(QPoint(int(px), int(py)))
            return
        if button != BTN_LEFT:
            return

        now = time.time()
        now_mono = time.monotonic()
        self._cancel_autonomous_walk()
        self._autonomous_pose = None
        self._press_ts = now
        self._last_interaction_ts = now
        self._drag_distance = 0.0
        self._shake_triggered_this_drag = False
        self._layer_press_pos = (px, py)
        self._layer_grab_offset = (px - self.physics.x, py - self.physics.y)
        self._layer_dragging = True
        self._press_shift_held = bool(shift_held)
        self._drag_last_ts = now_mono
        self._active_monitor = self._stage_monitor()

        self.physics.begin_grab(self.physics.x, self.physics.y, timestamp=now_mono)
        self._push_physics_visual(self.physics._make_result())
        if hasattr(self, "canvas_v2"):
            self.canvas_v2.wake_up()
        if self.layer_stage is not None:
            self.layer_stage.set_full_input()
        self._render_layer_frame()

    def _on_layer_pointer_move(self, px: float, py: float, ts: float) -> None:
        if not self._layer_dragging:
            return
        mon = self._active_monitor or self._stage_monitor()
        min_x, max_x, min_y, max_y = self.physics._clamp_bounds(mon)
        new_x = max(min_x, min(max_x, px - self._layer_grab_offset[0]))
        new_y = max(min_y, min(max_y, py - self._layer_grab_offset[1]))

        step_dist = math.hypot(new_x - self.physics.x, new_y - self.physics.y)
        self._drag_distance += step_dist
        if self._drag_distance >= 8.0 and hasattr(self, "canvas_v2") and self.canvas_v2.pose != "normal":
            self.manual_pose_override = None
            self.canvas_v2.set_pose("normal")

        dt = max(0.0005, min(0.1, ts - self._drag_last_ts))
        self._drag_last_ts = ts
        res = self.physics.observe_external_drag(new_x, new_y, dt, timestamp=ts)
        self._push_physics_visual(res)
        # Render immediately if compositor released a buffer (capped to ~125 FPS on 1000Hz gaming mice)
        if ts - getattr(self, "_last_drag_render_ts", 0.0) >= 0.008:
            self._last_drag_render_ts = ts
            self._render_layer_frame()

    def _on_layer_pointer_release(self, px: float, py: float, button: int, shift_held: bool) -> None:
        if button != BTN_LEFT or not self._layer_dragging:
            return
        self._layer_dragging = False
        now_mono = time.monotonic()
        dt = max(0.002, min(0.1, now_mono - self._drag_last_ts))
        self._drag_last_ts = now_mono
        # Record exact release moment so pausing >= 90ms zeroes velocity while brisk flicks keep true momentum
        self.physics.observe_external_drag(self.physics.x, self.physics.y, dt, timestamp=now_mono)

        elapsed = time.time() - self._press_ts
        moved = max(
            self._drag_distance,
            math.hypot(px - self._layer_press_pos[0], py - self._layer_press_pos[1]),
        )
        effective_shift = bool(shift_held or self._press_shift_held)
        self._press_shift_held = False

        if moved < 6.0 and elapsed < 0.38:
            self._layer_last_click_ts = time.time()
            self._layer_last_click_pos = (px, py)
            self.physics.vx = 0.0
            self.physics.vy = 0.0
            self.physics.state = PhysicsState.PINNED if self.physics.pinned else PhysicsState.IDLE
            self.physics._reset_visual_springs()
            self._push_physics_visual(self.physics._make_result())
            self._click_timer.start()
            self._render_layer_frame()
            return

        mon = self._stage_monitor()
        self._active_monitor = mon
        if not effective_shift and hasattr(self, "canvas_v2") and self.canvas_v2.pose != "normal":
            self.manual_pose_override = None
            self.canvas_v2.set_pose("normal")

        surfaces = self._refresh_surface_map(mon) if not effective_shift else []
        res = self.physics.end_grab(shift_held=effective_shift, monitor=mon, surfaces=surfaces)
        self._push_physics_visual(res)
        if effective_shift:
            self.locomotion.on_manual_drag_end(res.x, res.y)
            if self.reactions_enabled and not self.power_saver_active:
                self._show_ephemeral_caption(random.choice(self._PIN_QUOTES), ticks=170)
        elif res.landed:
            self._apply_landing_pose(res)
            self.locomotion.on_manual_drag_end(res.x, res.y)
            if moved >= 40.0 and not self._shake_triggered_this_drag:
                self.trigger_drag_drop_reaction()
        self._render_layer_frame()

    def _refresh_surface_map(self, monitor: Optional[MonitorWorkArea] = None) -> list[LandingSurface]:
        """Build fresh logical-pixel landing surfaces for the active monitor and workspace."""
        mon = monitor or self._stage_monitor()
        comp_kind = compositor.kind
        windows = self.locomotion.observer.get_windows() if comp_kind in ("niri", "hyprland") else []
        active_ws_id = None
        try:
            act_ws = self.locomotion.observer.get_active_workspace()
            if isinstance(act_ws, dict):
                active_ws_id = act_ws.get("id")
        except Exception:
            active_ws_id = None

        origin_offset = (0.0, 0.0)
        if self.using_layer_stage and comp_kind == "hyprland":
            mons = compositor.get_monitors()
            focused_mon = next((m for m in mons if m.is_focused), mons[0] if mons else None)
            if focused_mon is not None:
                origin_offset = (focused_mon.work_left, focused_mon.work_top)

        self._cached_surfaces = build_surface_map(
            windows,
            mon,
            compositor_kind=comp_kind,
            active_workspace_id=active_ws_id,
            avatar_pid=os.getpid(),
            stage_origin_offset=origin_offset,
        )
        return self._cached_surfaces

    def _apply_landing_pose(self, res: PhysicsStepResult) -> None:
        if not hasattr(self, "canvas_v2"):
            return
        if res.preferred_pose == "perch":
            pose = "perch_music" if self.media_state.is_playing else "perch"
            self.canvas_v2.set_pose(pose)
        elif self.manual_pose_override is None and self.canvas_v2.pose in ("perch", "perch_music"):
            self.canvas_v2.set_pose("normal")

    def _poll_perched_surfaces(self) -> None:
        """
        Lightweight 350ms check ONLY while Capri is perched on a window shelf.
        Zero IPC calls while sitting on the monitor floor, pinned, or in Game Mode.
        """
        if self.power_saver_active:
            return
        surf = self.physics.active_surface
        if surf is None or surf.window_id is None:
            return
        if self.physics.state not in (PhysicsState.IDLE, PhysicsState.LANDING):
            return
        mon = self._stage_monitor()
        surfaces = self._refresh_surface_map(mon)
        res = self.physics.sync_surfaces(surfaces, mon)
        if res.lost_footing:
            if hasattr(self, "canvas_v2"):
                self.canvas_v2.set_pose("normal")
            self._push_physics_visual(res)
            if not self.using_layer_stage:
                self._apply_physics_ipc_move(res)
            now = time.time()
            if self.reactions_enabled and (now - self._last_reaction_ts >= 2.5):
                self._last_reaction_ts = now
                self._show_ephemeral_caption(random.choice(self._LOST_FOOTING_QUOTES), ticks=190)
            if self.using_layer_stage:
                self._render_layer_frame()
        elif res.ipc_set_exact is not None or self.using_layer_stage:
            self.locomotion.on_manual_drag_end(res.x, res.y)
            if not self.using_layer_stage:
                self._apply_physics_ipc_move(res)
            else:
                self._render_layer_frame()

    def setWindowOpacity(self, level: float):
        self._layer_opacity = max(0.0, min(1.0, float(level)))
        super().setWindowOpacity(level)
        if self.using_layer_stage:
            self._render_layer_frame()

    def _trigger_teleport_glitch(self):
        if hasattr(self, "canvas_v2"):
            self.canvas_v2.trigger_glitch()

    def _init_window(self):
        self.setWindowTitle("capri-avatar")
        self.setObjectName("capri-avatar")
        QApplication.setApplicationName("capri-avatar")
        QApplication.setDesktopFileName("capri-avatar")

        # Размер окна под полноростовую фигурку и комикс-бабл реплики
        self.setFixedSize(300, 420)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def _init_views(self):
        self.stack = QStackedWidget(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

        # 1. Основной живой спрайтовый конструкт v0.2.0 (без рамок и кругов)
        self.canvas_v2 = AvatarV2Canvas(self, target_w=210, target_h=336)
        self.canvas_v2.set_reduced_motion(self.reduced_motion)
        self.stack.addWidget(self.canvas_v2)

        # 2. Классический круглый HUD-радар v0.1.0 (для любителей ретро)
        self.canvas_classic = Avatar2DCanvas(self, size=240)
        self.stack.addWidget(self.canvas_classic)

        # 3. 3D QML View
        self.quick_3d = QQuickWidget(self)
        self.quick_3d.setClearColor(QColor(0, 0, 0, 0))
        self.quick_3d.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        from ..core.resources import get_asset_path
        qml_path = get_asset_path("3d", "qml", "Avatar3D.qml")
        if qml_path.exists():
            self.quick_3d.setSource(QUrl.fromLocalFile(str(qml_path)))
        self.stack.addWidget(self.quick_3d)

        self.set_mode(self.mode)

    def set_mode(self, mode: str):
        self.mode = mode
        if mode == "3d":
            self.stack.setCurrentWidget(self.quick_3d)
            self._sync_3d_properties()
        elif mode in ("classic", "circle"):
            self.stack.setCurrentWidget(self.canvas_classic)
            self.canvas_classic.update()
        else:
            self.stack.setCurrentWidget(self.canvas_v2)
            self.canvas_v2.update()

    def _sync_3d_properties(self):
        root_obj = self.quick_3d.rootObject()
        if root_obj:
            root_obj.setProperty("avatarState", self.state)
            root_obj.setProperty("captionText", self.caption_text)

    def _init_ipc_server(self):
        if os.path.exists(SOCKET_PATH):
            try:
                os.unlink(SOCKET_PATH)
            except OSError:
                pass
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._handle_new_connection)
        self.server.listen(SOCKET_PATH)

    def _handle_new_connection(self):
        client = self.server.nextPendingConnection()
        if not client:
            return
        client.readyRead.connect(lambda: self._read_client_data(client))

    def _read_client_data(self, client: QLocalSocket):
        raw = client.readAll().data().decode("utf-8", errors="ignore")
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("type") == "locomotion" or data.get("action") in ("come", "stay", "follow", "home", "glide", "teleport"):
                    self.locomotion.handle_command(data)
                    continue
                if data.get("type") == "audio_level":
                    if hasattr(self, "canvas_v2"):
                        self.canvas_v2.set_audio_level(
                            float(data.get("level", 0.0)),
                            source=str(data.get("source", "mic")),
                        )
                    continue
                if data.get("type") == "power_saver":
                    if "override" in data:
                        self.game_mode_override = data["override"]
                    if "active" in data:
                        self.apply_game_mode(bool(data["active"]), reason=str(data.get("reason", "ipc")))
                    elif self.game_mode_override is None:
                        self.apply_game_mode(False, reason="auto_reset")
                    continue
                if data.get("type") == "config_sync":
                    if "auto_avatar_poses" in data:
                        self.auto_poses_enabled = bool(data["auto_avatar_poses"])
                    if "game_mode_enabled" in data:
                        self.game_mode_enabled = bool(data["game_mode_enabled"])
                        if not self.game_mode_enabled and self.game_mode_override is None:
                            self.apply_game_mode(False, reason="config_disabled")
                    if "avatar_reactions_enabled" in data:
                        self.reactions_enabled = bool(data["avatar_reactions_enabled"])
                    if "reduced_motion" in data:
                        self.reduced_motion = bool(data["reduced_motion"])
                        self.physics.set_reduced_motion(self.reduced_motion)
                        if self.reduced_motion:
                            self._cancel_autonomous_walk()
                        if hasattr(self, "canvas_v2"):
                            self.canvas_v2.set_reduced_motion(self.reduced_motion)
                    if "physics_enabled" in data or "avatar_physics_enabled" in data:
                        val = data.get("physics_enabled", data.get("avatar_physics_enabled", True))
                        self.physics_enabled = bool(val)
                        self.physics.set_physics_enabled(self.physics_enabled)
                    if "autonomous_life_enabled" in data or "avatar_autonomous_life" in data:
                        val = data.get("autonomous_life_enabled", data.get("avatar_autonomous_life", True))
                        self.autonomous_life_enabled = bool(val)
                        self.brain.set_enabled(self.autonomous_life_enabled)
                        if not self.autonomous_life_enabled:
                            self._cancel_autonomous_walk()
                    if "companion_mode" in data:
                        self.companion_mode = str(data.get("companion_mode") or "normal").strip().lower()
                    if "user_name" in data and str(data.get("user_name") or "").strip():
                        self.awareness.user_name = str(data["user_name"]).strip()
                    if "ui_language" in data:
                        ui_lang = "en" if str(data.get("ui_language") or "").strip().lower().startswith("en") else "ru"
                        os.environ["CAPRI_UI_LANG"] = ui_lang
                        if getattr(self, "_popover", None) is not None:
                            self._popover.set_language(ui_lang)
                    self._sync_auto_pose()
                    continue
                new_state = data.get("state", self.state)
                text = data.get("text", "")
                meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
                if "mode" in data:
                    self.set_mode(data["mode"])
                req_pose = data.get("pose") or meta.get("pose")
                if req_pose and hasattr(self, "canvas_v2"):
                    p_str = str(req_pose).strip().lower()
                    if p_str in ("normal", "wake", "auto"):
                        self.manual_pose_override = None
                        self.canvas_v2.set_pose("wake" if p_str == "wake" else "normal")
                    elif p_str in ("perch", "perch_music", "lounge", "walk", "sleep"):
                        self.manual_pose_override = p_str
                        self.canvas_v2.set_pose(p_str)
                if meta and hasattr(self, "canvas_v2"):
                    if "mood" in meta:
                        self.canvas_v2.set_mood(str(meta["mood"]))
                    if "mouth_envelope" in meta and isinstance(meta["mouth_envelope"], list):
                        self.canvas_v2.set_lip_sync_envelope(meta["mouth_envelope"])
                self.set_state(new_state, text)
            except Exception:
                pass

    def _init_timers(self):
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self._update_animation)
        self.anim_timer.start(16)

        # MPRIS + Game Mode / Power Saver фоновый опрос (отключён в offscreen-тестах)
        self.presence_timer = QTimer(self)
        self.presence_timer.setInterval(2500)
        self.presence_timer.timeout.connect(self._poll_presence_async)
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            self.presence_timer.start()

        # Отслеживание окна под Капри (выполняет IPC-запрос ТОЛЬКО когда она сидит на окне)
        self.surface_timer = QTimer(self)
        self.surface_timer.setInterval(350)
        self.surface_timer.timeout.connect(self._poll_perched_surfaces)
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            self.surface_timer.start()

        # Автономная жизнь Капри (мягкие прогулки и прыжки на окна с соблюдением всех блокировок)
        self.brain_timer = QTimer(self)
        self.brain_timer.setInterval(2000)
        self.brain_timer.timeout.connect(self._tick_autonomous_brain)
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            self.brain_timer.start()

        # Сторожевой таймер: проверка жизни родительского процесса
        if self.parent_pid:
            self.watchdog_timer = QTimer(self)
            self.watchdog_timer.timeout.connect(self._check_parent_alive)
            self.watchdog_timer.start(800)

    def _on_compositor_fullscreen(self, is_fs: bool):
        """Instant Power Saver hook directly from compositor fullscreen events."""
        if self.game_mode_override is not None:
            return
        if not self.game_mode_enabled:
            return
        self.apply_game_mode(bool(is_fs), reason="fullscreen_app" if is_fs else "")

    def _on_compositor_focus_changed(self, fw: dict):
        """No-op hook reserved for future focus-aware avatar behaviors."""
        return

    def _poll_presence_async(self):
        if self._presence_poll_inflight:
            return
        self._presence_poll_inflight = True

        def _worker():
            try:
                media = query_mpris_playback() if self.auto_poses_enabled else MediaPlaybackState()
                if self.game_mode_enabled or self.game_mode_override is not None:
                    gm = detect_game_mode(manual_override=self.game_mode_override)
                else:
                    gm = GameModeState(active=False)
                self.presence_snapshot_ready.emit(media, gm)
            except Exception:
                pass
            finally:
                self._presence_poll_inflight = False

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_presence_snapshot(self, media: MediaPlaybackState, gm: GameModeState):
        self.media_state = media
        self.game_state = gm
        if media.is_playing or gm.active:
            self._cancel_autonomous_walk()
            self._autonomous_pose = None
        self.apply_game_mode(gm.active, reason=gm.reason)
        self._sync_auto_pose()

    def apply_game_mode(self, active: bool, reason: str = ""):
        """
        Game Mode / Power Saver:
        - When active: stops 60 FPS timer while idle (0 wasted CPU/GPU frames in games),
          lowers opacity to subtle ghost, clears idle captions, rests avatar in sleep pose.
        - When exiting: restores 60 FPS animation, full opacity, and smooth wake animation.
        """
        if self.game_mode_override is True:
            effective = True
        elif self.game_mode_override is False:
            effective = False
        else:
            effective = bool(active) and bool(self.game_mode_enabled)

        if effective == self.power_saver_active:
            return
        self.power_saver_active = effective
        self.physics.set_game_mode(effective)
        if effective:
            self._cancel_autonomous_walk()

        if effective:
            if self.state == AvatarState.IDLE:
                self.anim_timer.stop()
            else:
                self.anim_timer.start(250)
            self.caption_text = ""
            self.caption_timer = 0
            self.setWindowOpacity(0.15)
            if hasattr(self, "canvas_v2"):
                self.canvas_v2.set_pose("sleep")
                self.canvas_v2.update()
            if self.using_layer_stage:
                self._render_layer_frame()
        else:
            self.anim_timer.start(16)
            self.setWindowOpacity(1.0)
            if hasattr(self, "canvas_v2"):
                self.canvas_v2.wake_up()
            self._sync_auto_pose()
            if self.using_layer_stage:
                self._render_layer_frame()

    def _sync_auto_pose(self):
        if not hasattr(self, "canvas_v2"):
            return
        idle_sec = max(0.0, time.time() - self._last_interaction_ts)
        pose, reason = resolve_avatar_auto_pose(
            avatar_state=self.state,
            media_playing=bool(self.media_state.is_playing),
            game_mode_active=bool(self.power_saver_active),
            manual_pose_override=self.manual_pose_override,
            idle_seconds=idle_sec,
            idle_sleep_threshold=180.0,
            auto_poses_enabled=self.auto_poses_enabled,
        )
        if reason in ("mpris_playing", "power_saver", "idle_timeout", "active_interaction"):
            self._cancel_autonomous_walk()
            self._autonomous_pose = None
        # Preserve active autonomous walk / autonomous lounge / window perch while calmly idle
        if (
            self.state == AvatarState.IDLE
            and not self.power_saver_active
            and self.manual_pose_override is None
            and not self.media_state.is_playing
            and pose == "normal"
            and reason in ("default", "auto_disabled")
        ):
            if self._auto_walk_target is not None:
                pose = "walk"
            elif self._autonomous_pose in ("lounge", "perch"):
                pose = self._autonomous_pose
            elif self.physics.perched:
                pose = "perch"
        elif (
            self.physics.perched
            and self.state == AvatarState.IDLE
            and not self.power_saver_active
            and self.manual_pose_override is None
            and pose == "normal"
        ):
            pose = "perch_music" if self.media_state.is_playing else "perch"
        prev_pose = self.canvas_v2.pose
        if pose != prev_pose:
            self.canvas_v2.set_pose(pose)
            if (
                pose == "perch_music"
                and reason == "mpris_playing"
                and self.reactions_enabled
                and not self.power_saver_active
                and self.media_state.track_label
                and self.media_state.track_label != self._last_announced_track
            ):
                self._last_announced_track = self.media_state.track_label
                self._show_ephemeral_caption(f"🎵 {self.media_state.track_label}", ticks=210)
        elif (
            pose == "perch_music"
            and reason == "mpris_playing"
            and self.reactions_enabled
            and not self.power_saver_active
            and self.media_state.track_label
            and self.media_state.track_label != self._last_announced_track
        ):
            self._last_announced_track = self.media_state.track_label
            self._show_ephemeral_caption(f"🎵 {self.media_state.track_label}", ticks=210)

    def _show_ephemeral_caption(self, text: str, ticks: int = 200):
        clean_txt = " ".join((text or "").replace("\n", " ").split()).strip()
        if not clean_txt:
            return
        if len(clean_txt) > 92:
            clipped = clean_txt[:89].rsplit(" ", 1)[0].rstrip(" ,;:-")
            clean_txt = (clipped if len(clipped) >= 40 else clean_txt[:89].rstrip()) + "…"
        self.caption_text = clean_txt
        self.caption_timer = max(60, int(ticks))
        if hasattr(self, "canvas_v2"):
            self.canvas_v2.update()

    def _check_parent_alive(self):
        # Если родительский терминал Kitty или Cyberdeck завершился - мгновенно закрываемся
        if self.parent_pid:
            try:
                os.kill(self.parent_pid, 0)
            except OSError:
                self.close()
                QApplication.quit()

    def set_state(self, state: str, text: str = ""):
        self.state = state
        if state != AvatarState.IDLE:
            self._cancel_autonomous_walk()
            self._autonomous_pose = None
        if state != AvatarState.IDLE or text:
            now_act = time.time()
            self._last_interaction_ts = now_act
            if hasattr(self, "awareness") and self.awareness is not None:
                self.awareness.note_user_activity(now_act)
        if self.power_saver_active:
            if state == AvatarState.IDLE:
                self.anim_timer.stop()
            else:
                self.anim_timer.start(250)
        if text:
            clean_txt = " ".join(text.replace("\n", " ").split()).strip()
            if len(clean_txt) > 92:
                clipped = clean_txt[:89].rsplit(" ", 1)[0].rstrip(" ,;:-")
                clean_txt = (clipped if len(clipped) >= 40 else clean_txt[:89].rstrip()) + "…"
            self.caption_text = clean_txt
            self.caption_timer = 280
        elif state == AvatarState.IDLE:
            self.caption_timer = 40

        # Триггер глитча при раздумьях
        if state == AvatarState.THINKING:
            self.canvas_v2.trigger_glitch()
        elif state == AvatarState.IDLE:
            self._sync_auto_pose()

        if self.mode == "3d":
            self._sync_3d_properties()
        elif self.mode in ("classic", "circle"):
            self.canvas_classic.update()
        else:
            self.canvas_v2.update()

    def _resolve_verified_floating_avatar_id(self):
        """
        Resolve ONLY Capri's own avatar window ID and guarantee it is floating.
        Never returns a foreign window ID; marks physics IPC unavailable on failure.
        """
        win = self.locomotion.observer.get_avatar_window(os.getpid())
        if not isinstance(win, dict):
            self.physics.set_ipc_available(False)
            self._verified_avatar_id = None
            return None
        is_own = (win.get("pid") == os.getpid()) or (win.get("app_id") == "capri-avatar")
        wid = win.get("id")
        if not is_own or not wid:
            self.physics.set_ipc_available(False)
            self._verified_avatar_id = None
            return None
        if not win.get("is_floating"):
            if not compositor.ensure_window_floating_sync(wid):
                self.physics.set_ipc_available(False)
                self._verified_avatar_id = None
                return None
        self.physics.set_ipc_available(True)
        self._verified_avatar_id = wid
        return wid

    def _apply_physics_ipc_move(self, res: PhysicsStepResult) -> bool:
        """Apply a throttled compositor move for the verified floating avatar window only."""
        if res.ipc_set_exact is None and res.ipc_move_dx == 0 and res.ipc_move_dy == 0:
            return True
        if not self.physics.ipc_available:
            return False
        wid = self._verified_avatar_id or self._resolve_verified_floating_avatar_id()
        if not wid:
            self.physics.set_ipc_available(False)
            return False
        if res.ipc_set_exact is not None:
            ok = compositor.move_floating_window_exact_sync(wid, res.ipc_set_exact[0], res.ipc_set_exact[1])
        else:
            ok = compositor.move_floating_window_sync(wid, res.ipc_move_dx, res.ipc_move_dy)
        if not ok:
            self.physics.set_ipc_available(False)
            self._verified_avatar_id = None
        return ok

    def _push_physics_visual(self, res: PhysicsStepResult) -> None:
        if hasattr(self, "canvas_v2"):
            self.canvas_v2.set_physics_visual(
                state=res.state,
                tilt_deg=res.tilt_deg,
                scale_x=res.scale_x,
                scale_y=res.scale_y,
                flail_phase=res.flail_phase,
                facing=res.facing,
                canvas_offset_y=res.canvas_offset_y,
                impact_ripple=res.impact_ripple,
                vx=res.vx,
                vy=res.vy,
            )

    def _update_animation(self):
        now_mono = time.monotonic()
        dt = max(0.001, min(0.05, now_mono - self._last_anim_ts))
        self._last_anim_ts = now_mono

        self.pulse_phase = (self.pulse_phase + 0.05) % (2 * math.pi)

        # 60 Hz visual + physics tick (0 IPC calls in Wayland layer-stage mode!)
        if not self.power_saver_active:
            phys_busy = (
                self.physics.state != PhysicsState.IDLE
                or abs(self.physics.tilt_deg) > 0.05
                or abs(self.physics.scale_x - 1.0) > 0.005
                or abs(self.physics.scale_y - 1.0) > 0.005
                or abs(self.physics.canvas_offset_y) > 0.1
                or self.physics.impact_ripple > 0.005
            )
            if phys_busy:
                if self.using_layer_stage:
                    mon = self._stage_monitor()
                else:
                    mon = self._active_monitor or compositor.get_monitor_for_point(
                        self.physics.x + self.width() / 2.0,
                        self.physics.y + self.height() / 2.0,
                    )
                self._active_monitor = mon
                adj_left, adj_right = self._adjacent_monitor_flags()
                res = self.physics.step(
                    dt,
                    mon,
                    surfaces=self._cached_surfaces,
                    adjacent_left=adj_left,
                    adjacent_right=adj_right,
                )
                if res.crossed_monitor_side != 0 and self.using_layer_stage:
                    cur_m = self._current_compositor_monitor()
                    if cur_m is not None:
                        y_ref = cur_m.work_top + self.physics.y + 210.0
                        nxt_m = find_adjacent_monitor(cur_m, res.crossed_monitor_side, y_ref, self._known_monitors)
                        if nxt_m is not None:
                            self._migrate_layer_to_monitor(nxt_m, side=res.crossed_monitor_side)
                if not self.using_layer_stage:
                    self._apply_physics_ipc_move(res)
                self._push_physics_visual(res)
                if res.wall_hit and res.impact_strength >= 0.35:
                    self.trigger_wall_splat_reaction()
                if res.landed:
                    self._apply_landing_pose(res)
                    self.locomotion.on_manual_drag_end(res.x, res.y)
                if self.physics.state in (PhysicsState.IDLE, PhysicsState.PINNED) and res.impact_ripple <= 0.005:
                    self._active_monitor = None
            elif self._auto_walk_target is not None:
                self._step_autonomous_walk(dt)

        # Продвижение спрайтовой анимации v0.2.0
        self.canvas_v2.advance_animation()

        if self.caption_timer > 0:
            self.caption_timer -= 1
            if self.caption_timer == 0:
                self.caption_text = ""
                if self.state == AvatarState.PLAYFUL:
                    self.state = AvatarState.IDLE
                if self.mode == "3d":
                    self._sync_3d_properties()

        if self.using_layer_stage:
            self._render_layer_frame()
        elif self.mode in ("classic", "circle"):
            self.canvas_classic.update()

    def trigger_wall_splat_reaction(self):
        """Playful glitch reaction when Capri hits a monitor boundary at high speed."""
        if not self.reactions_enabled or self.power_saver_active:
            return
        now = time.time()
        if now - self._last_reaction_ts < 2.2:
            return
        self._last_interaction_ts = now
        self._last_reaction_ts = now
        if hasattr(self, "canvas_v2"):
            self.canvas_v2.wake_up()
            self.canvas_v2.trigger_glitch()
        self._show_ephemeral_caption(random.choice(self._WALL_SPLAT_QUOTES), ticks=210)

    def trigger_poke_reaction(self):
        """Playful Capri reaction when the user single-clicks/pokes the desktop hologram."""
        if not self.reactions_enabled or self.power_saver_active:
            return
        now = time.time()
        self._last_interaction_ts = now
        if now - self._last_poke_ts <= 5.0:
            self._poke_count += 1
        else:
            self._poke_count = 1
        self._last_poke_ts = now
        if now - self._last_reaction_ts < 1.2 and self._poke_count < 3:
            return
        self._last_reaction_ts = now
        if hasattr(self, "canvas_v2"):
            self.canvas_v2.wake_up()
            if self._poke_count >= 3:
                self.canvas_v2.trigger_glitch()
        pool = self._RAPID_POKE_QUOTES if self._poke_count >= 3 else self._POKE_QUOTES
        self._show_ephemeral_caption(random.choice(pool), ticks=210)

    def trigger_shake_reaction(self):
        """Indignant glitch reaction when the user rapidly shakes the avatar window."""
        if not self.reactions_enabled or self.power_saver_active:
            return
        now = time.time()
        self._last_interaction_ts = now
        self._last_reaction_ts = now
        if hasattr(self, "canvas_v2"):
            self.canvas_v2.wake_up()
            self.canvas_v2.trigger_glitch()
        self._show_ephemeral_caption(random.choice(self._SHAKE_QUOTES), ticks=230)

    def trigger_drag_drop_reaction(self):
        """Light remark when the user gently relocates Capri across the desktop."""
        if not self.reactions_enabled or self.power_saver_active:
            return
        now = time.time()
        self._last_interaction_ts = now
        if now - self._last_reaction_ts < 4.5:
            return
        self._last_reaction_ts = now
        if hasattr(self, "canvas_v2"):
            self.canvas_v2.wake_up()
        self._show_ephemeral_caption(random.choice(self._DROP_QUOTES), ticks=190)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._cancel_autonomous_walk()
            gpos = event.globalPosition().toPoint()
            self.drag_position = gpos - self.frameGeometry().topLeft()
            self._press_global_pos = gpos
            self._press_ts = time.time()
            self._drag_distance = 0.0
            self._drag_last_x = gpos.x()
            self._shake_last_dir = 0
            self._shake_reversals = 0
            self._shake_window_start = self._press_ts
            self._shake_triggered_this_drag = False

            use_physics_drag = (
                self.physics_enabled
                and not self.power_saver_active
                and compositor.is_available
            )
            aid = self._resolve_verified_floating_avatar_id() if use_physics_drag else None
            if aid is not None:
                cur_x, cur_y = self.locomotion.get_current_coords()
                now_mono = time.monotonic()
                self._active_monitor = compositor.get_monitor_for_point(
                    cur_x + self.width() / 2.0,
                    cur_y + self.height() / 2.0,
                )
                self._drag_active = True
                self._drag_press_local = event.position()
                self._drag_latest_local = None
                self._drag_ignore_until = 0.0
                self._press_shift_held = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self._drag_start_coords = (cur_x, cur_y)
                self._drag_last_ts = now_mono
                self.physics.begin_grab(cur_x, cur_y, timestamp=now_mono)
                self._push_physics_visual(self.physics._make_result())
                if hasattr(self, "canvas_v2"):
                    self.canvas_v2.wake_up()
                    self.canvas_v2.update()
                self._drag_timer.start()
            else:
                self._drag_active = False
                # Graceful native move fallback when IPC is unavailable or physics disabled
                wh = self.windowHandle()
                if wh and hasattr(wh, "startSystemMove"):
                    wh.startSystemMove()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())

    def _commit_drag_step(self, *, now_mono: float, local_pos: Optional[QPointF]) -> None:
        """
        Move the verified floating avatar window by a single echo-free delta (<= 20 Hz).
        Clears `_drag_latest_local` and suppresses stale pre-repaint motion events for 22ms
        so Wayland pointer coordinates NEVER create a runaway positive-feedback loop.
        """
        dt = max(0.01, min(0.15, now_mono - self._drag_last_ts))
        self._drag_last_ts = now_mono

        dx = 0.0
        dy = 0.0
        if local_pos is not None and self._drag_press_local is not None:
            dx = float(local_pos.x() - self._drag_press_local.x())
            dy = float(local_pos.y() - self._drag_press_local.y())

        new_x = self.physics.x + dx
        new_y = self.physics.y + dy
        mon = self._active_monitor
        if mon is not None:
            min_x, max_x, min_y, max_y = self.physics._clamp_bounds(mon)
            new_x = max(min_x, min(max_x, new_x))
            new_y = max(min_y, min(max_y, new_y))

        actual_dx = new_x - self.physics.x
        actual_dy = new_y - self.physics.y
        step_dist = math.hypot(actual_dx, actual_dy)

        if step_dist >= 2.0 and self._verified_avatar_id:
            self._drag_distance += step_dist
            if self._drag_distance >= 8.0 and hasattr(self, "canvas_v2") and self.canvas_v2.pose != "normal":
                self.manual_pose_override = None
                self.canvas_v2.set_pose("normal")
            ok = compositor.move_floating_window_exact_sync(
                self._verified_avatar_id,
                int(round(new_x)),
                int(round(new_y)),
            )
            if not ok:
                self.physics.set_ipc_available(False)
                self._verified_avatar_id = None
                return
            # Clear pending sample and wait one compositor frame (>16.7ms) for fresh surface coords
            self._drag_latest_local = None
            self._drag_ignore_until = now_mono + 0.022
            res = self.physics.observe_external_drag(new_x, new_y, dt, timestamp=now_mono)
        else:
            # Stationary sample -> cleanly zeroes velocity if user pauses >= 90ms before release!
            res = self.physics.observe_external_drag(self.physics.x, self.physics.y, dt, timestamp=now_mono)

        self._push_physics_visual(res)

    def _on_drag_tick(self):
        if not self._drag_active:
            self._drag_timer.stop()
            return
        self._commit_drag_step(now_mono=time.monotonic(), local_pos=self._drag_latest_local)

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton:
            if self._drag_active:
                now_mono = time.monotonic()
                if now_mono >= self._drag_ignore_until:
                    self._drag_latest_local = event.position()
                event.accept()
                return

            gpos = event.globalPosition().toPoint()
            self.move(gpos - self.drag_position)
            if self._drag_last_x is not None:
                dx = gpos.x() - self._drag_last_x
                self._drag_distance += abs(dx)
                if abs(dx) >= 14:
                    now = time.time()
                    if now - self._shake_window_start > 1.0:
                        self._shake_window_start = now
                        self._shake_reversals = 0
                    cur_dir = 1 if dx > 0 else -1
                    if self._shake_last_dir != 0 and cur_dir != self._shake_last_dir:
                        self._shake_reversals += 1
                    self._shake_last_dir = cur_dir
                    self._drag_last_x = gpos.x()
                    if self._shake_reversals >= 3 and not self._shake_triggered_this_drag:
                        self._shake_triggered_this_drag = True
                        self.trigger_shake_reaction()
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._drag_active:
                self._drag_timer.stop()
                self._drag_active = False
                now_mono = time.monotonic()
                # Only consume a fresh post-guard mouseMoveEvent (never cached stale release coords!)
                final_local = self._drag_latest_local if now_mono >= self._drag_ignore_until else None
                self._commit_drag_step(now_mono=now_mono, local_pos=final_local)
                self._drag_press_local = None
                self._drag_latest_local = None

                shift_held = bool(
                    (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                    or self._press_shift_held
                )
                self._press_shift_held = False

                elapsed = time.time() - self._press_ts
                start_x, start_y = self._drag_start_coords
                moved = max(
                    self._drag_distance,
                    math.hypot(self.physics.x - start_x, self.physics.y - start_y),
                )

                if moved < 6.0 and elapsed < 0.38:
                    self.physics.vx = 0.0
                    self.physics.vy = 0.0
                    self.physics.state = PhysicsState.PINNED if self.physics.pinned else PhysicsState.IDLE
                    self.physics._reset_visual_springs()
                    self._push_physics_visual(self.physics._make_result())
                    self._active_monitor = None
                    self._click_timer.start()
                    event.accept()
                    return

                mon = self._active_monitor or compositor.get_monitor_for_point(
                    self.physics.x + self.width() / 2.0,
                    self.physics.y + self.height() / 2.0,
                )
                self._active_monitor = mon
                if not shift_held and hasattr(self, "canvas_v2") and self.canvas_v2.pose != "normal":
                    self.manual_pose_override = None
                    self.canvas_v2.set_pose("normal")

                surfaces = self._refresh_surface_map(mon) if not shift_held else []
                res = self.physics.end_grab(shift_held=shift_held, monitor=mon, surfaces=surfaces)
                self._apply_physics_ipc_move(res)
                self._push_physics_visual(res)

                if shift_held:
                    self.locomotion.on_manual_drag_end(res.x, res.y)
                    self._active_monitor = None
                    if self.reactions_enabled and not self.power_saver_active:
                        self._show_ephemeral_caption(random.choice(self._PIN_QUOTES), ticks=170)
                elif res.landed:
                    self._apply_landing_pose(res)
                    self.locomotion.on_manual_drag_end(res.x, res.y)
                    if moved >= 40.0 and not self._shake_triggered_this_drag:
                        self.trigger_drag_drop_reaction()
                event.accept()
                return

            gpos = event.globalPosition().toPoint()
            elapsed = time.time() - self._press_ts
            delta_px = 0.0
            if self._press_global_pos is not None:
                dx = gpos.x() - self._press_global_pos.x()
                dy = gpos.y() - self._press_global_pos.y()
                delta_px = math.hypot(dx, dy)
            moved = max(self._drag_distance, delta_px)

            if moved < 6.0 and elapsed < 0.38:
                # Defer poke slightly so double-click can cancel it cleanly
                self._click_timer.start()
            elif moved >= 40.0 and not self._shake_triggered_this_drag:
                self.trigger_drag_drop_reaction()
            # Ручной перенос пользователем завершен: фиксируем новые координаты как якорь
            QTimer.singleShot(150, self._sync_manual_drag_position)
            event.accept()

    def _sync_manual_drag_position(self):
        prev_anchor = self.locomotion.anchor_coords
        cur_pos = self.locomotion.get_current_coords()
        if cur_pos:
            dist = math.hypot(cur_pos[0] - prev_anchor[0], cur_pos[1] - prev_anchor[1])
            self.locomotion.on_manual_drag_end(cur_pos[0], cur_pos[1])
            self.physics.sync_external_position(cur_pos[0], cur_pos[1])
            if dist >= 40.0 and not self._shake_triggered_this_drag:
                self.trigger_drag_drop_reaction()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Left clicks stay strictly for avatar interaction (poking/dragging);
            # Relic Construct opens intentionally from the right-click menu's "◈ Релик" button.
            self.trigger_poke_reaction()
            event.accept()

    def _build_context_menu(self) -> tuple[QMenu, dict[str, object]]:
        from .theme import apply_dark_palette, menu_qss

        menu = QMenu(None if self.using_layer_stage else self)
        menu.setStyleSheet(menu_qss())
        apply_dark_palette(menu)

        act_dash = menu.addAction("Нейроинтерфейс (Relic HUD)")
        menu.addSeparator()

        poses_menu = QMenu("Позы и анимации", menu)
        poses_menu.setStyleSheet(menu_qss())
        apply_dark_palette(poses_menu)
        act_perch = poses_menu.addAction("Присесть (Perch)")
        act_perch_music = poses_menu.addAction("Вайб под музыку")
        act_dance = poses_menu.addAction("Танец")
        act_sleep = poses_menu.addAction("Сон / Пробуждение")
        menu.addMenu(poses_menu)

        auto_lbl = "Авто-позы (MPRIS / сон): [ON]" if self.auto_poses_enabled else "Авто-позы (MPRIS / сон): [OFF]"
        act_auto_poses = menu.addAction(auto_lbl)
        if self.game_mode_override is True:
            gm_lbl = "Game Mode / Power Saver: [FORCE ON]"
        elif self.game_mode_override is False:
            gm_lbl = "Game Mode / Power Saver: [OFF]"
        else:
            gm_lbl = "Game Mode / Power Saver: [AUTO — ON]" if self.power_saver_active else "Game Mode / Power Saver: [AUTO]"
        act_game_mode = menu.addAction(gm_lbl)
        menu.addSeparator()

        follow_label = "Следование за окном: [ON]" if self.locomotion.mode == LocomotionMode.FOLLOW else "Следование за окном: [OFF]"
        act_follow = menu.addAction(follow_label)
        act_home = menu.addAction("Вернуть на базу")
        menu.addSeparator()

        act_close = menu.addAction("Закрыть аватар")

        actions = {
            "dash": act_dash,
            "perch": act_perch,
            "perch_music": act_perch_music,
            "dance": act_dance,
            "sleep": act_sleep,
            "auto_poses": act_auto_poses,
            "game_mode": act_game_mode,
            "follow": act_follow,
            "home": act_home,
            "close": act_close,
        }
        return menu, actions

    def _wire_quick_popover(self) -> None:
        p = self.quick_popover
        p.open_dashboard_requested.connect(self._open_relic_dashboard)
        p.quick_ask_requested.connect(self._trigger_quick_ask_from_ui)
        p.voice_action_requested.connect(self._trigger_voice_from_ui)
        p.look_region_requested.connect(self._trigger_look_region_from_ui)
        p.pose_toggled.connect(self._toggle_quick_pose)
        p.physics_toggled.connect(self._toggle_physics_enabled)
        p.autonomous_toggled.connect(self._toggle_autonomous_life)
        p.follow_toggled.connect(self._toggle_follow_mode)
        p.auto_poses_toggled.connect(self._toggle_auto_poses)
        p.game_mode_cycled.connect(self._cycle_game_mode)
        p.monitor_cycle_requested.connect(self._cycle_avatar_monitor)
        p.home_requested.connect(self._return_home_from_ui)
        p.pin_toggled.connect(self._toggle_pin_in_place)
        p.close_avatar_requested.connect(self._close_avatar_from_ui)

    def _sync_quick_popover(self) -> None:
        if not hasattr(self, "quick_popover") or self.quick_popover is None:
            return
        self.quick_popover.sync_from_state(
            active_pose=self.canvas_v2.pose if hasattr(self, "canvas_v2") else "normal",
            dance_active=bool(self.canvas_v2.dance_active) if hasattr(self, "canvas_v2") else False,
            physics_enabled=self.physics_enabled,
            autonomous_enabled=self.autonomous_life_enabled,
            follow_enabled=(self.locomotion.mode == LocomotionMode.FOLLOW),
            auto_poses_enabled=self.auto_poses_enabled,
            game_mode_override=self.game_mode_override,
            power_saver_active=self.power_saver_active,
            pinned=self.physics.pinned,
            perched=self.physics.perched,
            has_lounge_sprite=bool(getattr(self.canvas_v2, "lounge_frames", [])) if hasattr(self, "canvas_v2") else False,
            avatar_state=self.state,
            available_monitors=self.layer_stage.available_outputs() if self.layer_stage is not None else [m.name for m in self._known_monitors],
            active_monitor=self.layer_stage.active_output_name if self.layer_stage is not None else "",
        )

    def _open_relic_dashboard(self) -> None:
        self.dashboard.show()
        self.dashboard.raise_()
        self.dashboard.activateWindow()

    def _trigger_quick_ask_from_ui(self) -> None:
        from .client import send_cyberdeck_command

        now = time.time()
        self._last_interaction_ts = now
        self.awareness.note_user_activity(now)
        ok = send_cyberdeck_command("ask")
        if not ok and self.reactions_enabled and not self.power_saver_active:
            self._show_ephemeral_caption("Запусти Cyberdeck для быстрого вопроса.", ticks=160)

    def _trigger_voice_from_ui(self) -> None:
        from .client import send_cyberdeck_command

        now = time.time()
        self._last_interaction_ts = now
        self.awareness.note_user_activity(now)
        cmd = "voice_stop" if self.state in (AvatarState.LISTENING, AvatarState.SPEAKING) else "voice_toggle"
        ok = send_cyberdeck_command(cmd)
        if not ok and self.reactions_enabled and not self.power_saver_active:
            self._show_ephemeral_caption("Запусти Cyberdeck для голосового ввода.", ticks=160)

    def _trigger_look_region_from_ui(self) -> None:
        from .client import send_cyberdeck_command

        now = time.time()
        self._last_interaction_ts = now
        self.awareness.note_user_activity(now)
        ok = send_cyberdeck_command("look_region")
        if not ok and self.reactions_enabled and not self.power_saver_active:
            self._show_ephemeral_caption("Запусти Cyberdeck для анализа области экрана.", ticks=160)

    def _toggle_quick_pose(self, pose_key: str) -> None:
        self._last_interaction_ts = time.time()
        self._cancel_autonomous_walk()
        self._autonomous_pose = None
        if not hasattr(self, "canvas_v2"):
            return
        if pose_key == "dance":
            self.canvas_v2.trigger_dance()
        elif pose_key == "sleep":
            if self.canvas_v2.pose == "sleep":
                self.manual_pose_override = None
                self.canvas_v2.set_pose("wake")
            else:
                # Reset transient playful state so advance_animation() never cancels sleep with wake.png flash!
                if self.state == AvatarState.PLAYFUL:
                    self.state = AvatarState.IDLE
                self.canvas_v2.waking_active = False
                self.canvas_v2.glitch_active = False
                self.canvas_v2.dance_active = False
                self.manual_pose_override = "sleep"
                self.canvas_v2.set_pose("sleep")
        elif pose_key in ("perch", "lounge"):
            self.canvas_v2.dance_active = False
            nxt = "normal" if self.canvas_v2.pose == pose_key else pose_key
            self.manual_pose_override = None if nxt == "normal" else nxt
            self.canvas_v2.set_pose(nxt)
        self._sync_quick_popover()
        if self.using_layer_stage:
            self._render_layer_frame()

    def _toggle_physics_enabled(self) -> None:
        self.physics_enabled = not self.physics_enabled
        self.physics.set_physics_enabled(self.physics_enabled)
        if not self.physics_enabled:
            self._cancel_autonomous_walk()
        self._sync_quick_popover()

    def _toggle_autonomous_life(self) -> None:
        self.autonomous_life_enabled = not self.autonomous_life_enabled
        self.brain.set_enabled(self.autonomous_life_enabled)
        if not self.autonomous_life_enabled:
            self._cancel_autonomous_walk()
        self._sync_quick_popover()

    def _toggle_follow_mode(self) -> None:
        new_follow = (self.locomotion.mode != LocomotionMode.FOLLOW)
        self.locomotion.follow(new_follow)
        self._sync_quick_popover()

    def _toggle_auto_poses(self) -> None:
        self.auto_poses_enabled = not self.auto_poses_enabled
        self.manual_pose_override = None
        self._sync_auto_pose()
        self._sync_quick_popover()

    def _cycle_game_mode(self) -> None:
        if self.game_mode_override is None:
            self.game_mode_override = True
            self.apply_game_mode(True, reason="manual_menu")
        elif self.game_mode_override is True:
            self.game_mode_override = False
            self.apply_game_mode(False, reason="manual_menu_off")
        else:
            self.game_mode_override = None
            self.apply_game_mode(False, reason="manual_menu_auto")
        self._sync_quick_popover()

    def _cycle_avatar_monitor(self) -> None:
        if not self.using_layer_stage or self.layer_stage is None:
            return
        names = self.layer_stage.available_outputs()
        if len(names) < 2:
            return
        cur = self.layer_stage.active_output_name
        idx = (names.index(cur) + 1) % len(names) if cur in names else 0
        target_name = names[idx]
        target_mon = next((m for m in self._known_monitors if m.name == target_name), None)
        if target_mon is not None:
            self._migrate_layer_to_monitor(target_mon, side=0)
        else:
            self.layer_stage.switch_output(target_name)
        self._sync_quick_popover()

    def _return_home_from_ui(self) -> None:
        self._last_interaction_ts = time.time()
        self._cancel_autonomous_walk()
        self.physics.set_pinned(False)
        self.physics.active_surface = None
        self.physics.perched = False
        self.locomotion.home()
        self._sync_quick_popover()

    def _toggle_pin_in_place(self) -> None:
        self._last_interaction_ts = time.time()
        self._cancel_autonomous_walk()
        new_pin = not self.physics.pinned
        self.physics.set_pinned(new_pin)
        if not new_pin and self.physics_enabled:
            mon = self._stage_monitor()
            surfaces = self._refresh_surface_map(mon)
            res = self.physics.end_grab(shift_held=False, monitor=mon, surfaces=surfaces)
            self._push_physics_visual(res)
            if not self.using_layer_stage:
                self._apply_physics_ipc_move(res)
        elif new_pin and self.reactions_enabled and not self.power_saver_active:
            self._show_ephemeral_caption(random.choice(self._PIN_QUOTES), ticks=160)
        self._sync_quick_popover()
        if self.using_layer_stage:
            self._render_layer_frame()

    def _close_avatar_from_ui(self) -> None:
        self.close()
        if self.using_layer_stage or getattr(self, "layer_stage", None) is None:
            app_inst = QApplication.instance()
            if app_inst is not None and not self.isVisible():
                app_inst.quit()

    # ── Stage 4: Autonomous Desktop Life Execution ──────────────────────────

    def _start_autonomous_walk(
        self,
        target_x: float,
        target_y: float,
        surface: Optional[LandingSurface] = None,
    ) -> None:
        self._auto_walk_target = (float(target_x), float(target_y))
        self._auto_walk_surface = surface
        self._auto_walk_ipc_accum = 0.0
        if hasattr(self, "canvas_v2"):
            self.canvas_v2.set_pose("walk")

    def _cancel_autonomous_walk(self) -> None:
        if self._auto_walk_target is None:
            return
        self._auto_walk_target = None
        self._auto_walk_surface = None
        if hasattr(self, "canvas_v2") and self.canvas_v2.pose == "walk":
            self.canvas_v2.set_pose("perch" if self.physics.perched else "normal")

    def _step_autonomous_walk(self, dt: float) -> None:
        if self._auto_walk_target is None:
            return
        if (
            not self.autonomous_life_enabled
            or self.power_saver_active
            or self.reduced_motion
            or self.physics.pinned
            or bool(self.media_state.is_playing)
            or self.manual_pose_override is not None
            or self.state != AvatarState.IDLE
            or self.physics.state != PhysicsState.IDLE
        ):
            self._cancel_autonomous_walk()
            return
        tx, ty = self._auto_walk_target
        dx = tx - self.physics.x
        walk_speed = 115.0  # calm desktop stroll (logical px/s)
        step = walk_speed * dt
        if abs(dx) <= max(2.0, step):
            self.physics.sync_external_position(tx, ty)
            self.locomotion.on_manual_drag_end(tx, ty)
            if not self.using_layer_stage and self.physics.ipc_available:
                wid = self._verified_avatar_id or self._resolve_verified_floating_avatar_id()
                if wid:
                    compositor.move_floating_window_exact_sync(wid, int(round(tx)), int(round(ty)))
            self._cancel_autonomous_walk()
            return

        direction = 1.0 if dx > 0.0 else -1.0
        self.physics.facing = 1 if direction > 0.0 else -1
        if hasattr(self, "canvas_v2"):
            self.canvas_v2.physics_facing = self.physics.facing
        new_x = self.physics.x + direction * step
        self.physics.x = new_x
        self.physics.y = ty
        if not self.using_layer_stage and self.physics.ipc_available:
            self._auto_walk_ipc_accum += dt
            if self._auto_walk_ipc_accum >= self.physics.cfg.ipc_sync_interval:
                self._auto_walk_ipc_accum = 0.0
                wid = self._verified_avatar_id or self._resolve_verified_floating_avatar_id()
                if wid:
                    compositor.move_floating_window_exact_sync(wid, int(round(new_x)), int(round(ty)))

    def _tick_autonomous_brain(self) -> None:
        # 0. Calm Living Awareness micro-reactions (strictly rate-limited, silent in Game/Focus/Quiet)
        cue = self.awareness.tick(
            now=time.time(),
            reactions_enabled=self.reactions_enabled,
            game_mode_active=self.power_saver_active,
            companion_mode=self.companion_mode,
            avatar_state=self.state,
            physics_state=self.physics.state,
            has_active_caption=bool((self.caption_text or "").strip()),
        )
        if cue.kind != "none" and cue.text:
            self._show_ephemeral_caption(cue.text, ticks=220)

        if (
            not self.autonomous_life_enabled
            or self.power_saver_active
            or self.reduced_motion
            or self._auto_walk_target is not None
            or self._layer_dragging
            or self._drag_active
            or self.locomotion.is_moving
        ):
            return
        mon = self._stage_monitor()
        surfaces = self._refresh_surface_map(mon)
        dec = self.brain.tick(
            now=time.time(),
            last_interaction_ts=self._last_interaction_ts,
            avatar_state=self.state,
            physics_state=self.physics.state,
            x=self.physics.x,
            y=self.physics.y,
            current_pose=self.canvas_v2.pose if hasattr(self, "canvas_v2") else "normal",
            active_surface=self.physics.active_surface,
            surfaces=surfaces,
            monitor=mon,
            mood=self.canvas_v2.mood if hasattr(self, "canvas_v2") else "neutral",
            pinned=self.physics.pinned,
            game_mode_active=self.power_saver_active,
            reduced_motion=self.reduced_motion,
            manual_pose_override=self.manual_pose_override,
            media_playing=bool(self.media_state.is_playing),
        )
        if dec.action == BrainAction.NONE:
            return
        if dec.action == BrainAction.WALK and dec.target_x is not None and dec.target_y is not None:
            self._autonomous_pose = None
            self._start_autonomous_walk(dec.target_x, dec.target_y, surface=dec.target_surface)
        elif dec.action == BrainAction.HOP_TO_SHELF and dec.target_x is not None and dec.target_y is not None:
            self._autonomous_pose = None
            self.physics.active_surface = dec.target_surface
            self.physics.perched = True
            self.locomotion.teleport(dec.target_x, dec.target_y)
            if hasattr(self, "canvas_v2"):
                self.canvas_v2.set_pose("perch_music" if self.media_state.is_playing else "perch")
        elif dec.action == BrainAction.DROP_TO_FLOOR:
            self._autonomous_pose = None
            self.physics.active_surface = None
            self.physics.perched = False
            self.physics.vx = max(-180.0, min(180.0, ((dec.target_x or self.physics.x) - self.physics.x) * 0.9))
            self.physics.vy = -95.0
            self.physics.state = PhysicsState.FALLING
            if hasattr(self, "canvas_v2"):
                self.canvas_v2.set_pose("normal")
        elif dec.action == BrainAction.LOUNGE and dec.pose:
            self._autonomous_pose = dec.pose
            if hasattr(self, "canvas_v2"):
                self.canvas_v2.set_pose(dec.pose)
        elif dec.action == BrainAction.IDLE_STAND and dec.pose:
            self._autonomous_pose = None
            if hasattr(self, "canvas_v2"):
                self.canvas_v2.set_pose(dec.pose)

    def _show_context_menu(self, pos: QPoint):
        self._sync_quick_popover()
        self.quick_popover.show_near(pos)

    def closeEvent(self, event):
        if getattr(self, "layer_stage", None) is not None:
            self.layer_stage.close()
            self.layer_stage = None
            self.using_layer_stage = False
        if hasattr(self, "_drag_timer") and self._drag_timer:
            self._drag_timer.stop()
        if hasattr(self, "_click_timer") and self._click_timer:
            self._click_timer.stop()
        if hasattr(self, "brain_timer") and self.brain_timer:
            self.brain_timer.stop()
        if hasattr(self, "quick_popover") and self.quick_popover:
            self.quick_popover.close()
        if hasattr(self, "surface_timer") and self.surface_timer:
            self.surface_timer.stop()
        if hasattr(self, "presence_timer") and self.presence_timer:
            self.presence_timer.stop()
        if hasattr(self, "anim_timer") and self.anim_timer:
            self.anim_timer.stop()
        if hasattr(self, "locomotion") and self.locomotion and getattr(self.locomotion, "observer", None):
            self.locomotion.observer.stop()
        if os.path.exists(SOCKET_PATH):
            try:
                os.unlink(SOCKET_PATH)
            except OSError:
                pass
        if os.path.exists(PID_PATH):
            try:
                os.unlink(PID_PATH)
            except OSError:
                pass
        event.accept()


def launch_avatar():
    from .. import __version__
    parser = argparse.ArgumentParser(prog="capri-avatar", description="Capri Desktop Avatar Overlay")
    parser.add_argument("--version", "-v", action="version", version=f"Capri Avatar Overlay v{__version__}")
    parser.add_argument("--mode", default="2d", choices=["2d", "3d"])
    parser.add_argument("--parent-pid", type=int, default=None)
    args, _ = parser.parse_known_args()

    # 1. Защита от дублей: если уже запущен активный PID, проверяем его жизнь
    if os.path.exists(PID_PATH):
        try:
            with open(PID_PATH, "r") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            # Старый процесс жив. Проверяем, существует ли активный сокет.
            # Если сокет удален, значит прошлый процесс подвис в фоне — принудительно гасим его.
            if not os.path.exists(SOCKET_PATH):
                try:
                    os.kill(old_pid, 15)
                except OSError:
                    pass
            else:
                sys.exit(0)
        except (OSError, ValueError):
            # Старый процесс мертв, перезаписываем PID
            pass

    try:
        with open(PID_PATH, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    # 2. Привязка к смерти родителя на уровне ядра Linux (только если родитель явно задан)
    if args.parent_pid:
        try:
            libc = ctypes.CDLL("libc.so.6")
            libc.prctl(1, 15)  # PR_SET_PDEATHSIG = 1, SIGTERM = 15
        except Exception:
            pass

    def _sig_handler(signum, frame):
        if os.path.exists(PID_PATH):
            try:
                os.unlink(PID_PATH)
            except OSError:
                pass
        if os.path.exists(SOCKET_PATH):
            try:
                os.unlink(SOCKET_PATH)
            except OSError:
                pass
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGINT, _sig_handler)
    except Exception:
        pass

    app = QApplication(sys.argv)
    from .theme import apply_dark_palette
    apply_dark_palette(app)
    widget = CapriAvatarWidget(mode=args.mode, parent_pid=args.parent_pid)
    if not widget.using_layer_stage:
        widget.show()

    ret = app.exec()
    if os.path.exists(PID_PATH):
        try:
            os.unlink(PID_PATH)
        except OSError:
            pass
    if os.path.exists(SOCKET_PATH):
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
    if os.path.exists(SOCKET_PATH):
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
    # Принудительный выход для гарантированного завершения фоновых потоков QQuickWidget/OpenGL
    os._exit(ret)


main = launch_avatar


if __name__ == "__main__":
    launch_avatar()
