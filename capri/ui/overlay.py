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
from PySide6.QtCore import Qt, QTimer, QPoint, QRectF, QUrl, QLockFile, Signal
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
from .sprite_view import AvatarV2Canvas
from .dashboard import CapriRelicDashboard
from .locomotion import LocomotionController, LocomotionMode
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
        "Да-да, я здесь, D. Что стряслось?",
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
        except Exception:
            self.auto_poses_enabled = True
            self.game_mode_enabled = True
            self.reactions_enabled = True

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

        # Киберпанк Relic Dashboard (v0.2.0)
        self.dashboard = CapriRelicDashboard()

        self._init_window()
        self._init_views()
        self._init_ipc_server()
        self._init_timers()
        self._init_locomotion()
        self.presence_snapshot_ready.connect(self._apply_presence_snapshot)

    def _init_locomotion(self):
        self.locomotion = LocomotionController(self)
        self.locomotion.caption_requested.connect(lambda txt: self.set_state("playful", txt))
        self.locomotion.glitch_requested.connect(self._trigger_teleport_glitch)
        self.locomotion.opacity_requested.connect(self.setWindowOpacity)
        if hasattr(self.locomotion, "observer") and self.locomotion.observer:
            self.locomotion.observer.fullscreen_changed.connect(self._on_compositor_fullscreen)

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
                    elif p_str in ("perch", "perch_music", "sleep"):
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
        else:
            self.anim_timer.start(16)
            self.setWindowOpacity(1.0)
            if hasattr(self, "canvas_v2"):
                self.canvas_v2.wake_up()
            self._sync_auto_pose()

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
        if state != AvatarState.IDLE or text:
            self._last_interaction_ts = time.time()
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

    def _update_animation(self):
        self.pulse_phase = (self.pulse_phase + 0.05) % (2 * math.pi)

        # Продвижение спрайтовой анимации v0.2.0
        self.canvas_v2.advance_animation()

        if self.caption_timer > 0:
            self.caption_timer -= 1
            if self.caption_timer == 0:
                self.caption_text = ""
                if self.mode == "3d":
                    self._sync_3d_properties()

        if self.mode in ("classic", "circle"):
            self.canvas_classic.update()

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
            # Нативный системный перенос плавающего окна в Wayland / Niri
            wh = self.windowHandle()
            if wh and hasattr(wh, "startSystemMove"):
                wh.startSystemMove()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
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
            if dist >= 40.0 and not self._shake_triggered_this_drag:
                self.trigger_drag_drop_reaction()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._click_timer.stop()
            # Двойной клик открывает стильный киберпанк Relic Dashboard
            self.dashboard.show()
            self.dashboard.raise_()
            self.dashboard.activateWindow()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton:
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

    def _build_context_menu(self) -> tuple[QMenu, dict[str, object]]:
        from .theme import apply_dark_palette, menu_qss

        menu = QMenu(self)
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

    def _show_context_menu(self, pos: QPoint):
        menu, actions = self._build_context_menu()
        action = menu.exec(pos)
        if action is None:
            return
        if action == actions["dash"]:
            self.dashboard.show()
            self.dashboard.raise_()
            self.dashboard.activateWindow()
        elif action == actions["follow"]:
            new_follow = (self.locomotion.mode != LocomotionMode.FOLLOW)
            self.locomotion.follow(new_follow)
        elif action == actions["home"]:
            self.locomotion.home()
        elif action == actions["dance"]:
            self.canvas_v2.trigger_dance()
        elif action == actions["perch"]:
            nxt = "normal" if self.canvas_v2.pose == "perch" else "perch"
            self.manual_pose_override = None if nxt == "normal" else nxt
            self.canvas_v2.set_pose(nxt)
        elif action == actions["perch_music"]:
            nxt = "normal" if self.canvas_v2.pose == "perch_music" else "perch_music"
            self.manual_pose_override = None if nxt == "normal" else nxt
            self.canvas_v2.set_pose(nxt)
        elif action == actions["sleep"]:
            if self.canvas_v2.pose == "sleep":
                self.manual_pose_override = None
                self.canvas_v2.set_pose("wake")
            else:
                self.manual_pose_override = "sleep"
                self.canvas_v2.set_pose("sleep")
        elif action == actions["auto_poses"]:
            self.auto_poses_enabled = not self.auto_poses_enabled
            self.manual_pose_override = None
            self._sync_auto_pose()
        elif action == actions["game_mode"]:
            if self.game_mode_override is None:
                self.game_mode_override = True
                self.apply_game_mode(True, reason="manual_menu")
            elif self.game_mode_override is True:
                self.game_mode_override = False
                self.apply_game_mode(False, reason="manual_menu_off")
            else:
                self.game_mode_override = None
                self.apply_game_mode(False, reason="manual_menu_auto")
        elif action == actions["close"]:
            self.close()

    def closeEvent(self, event):
        if hasattr(self, "_click_timer") and self._click_timer:
            self._click_timer.stop()
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
