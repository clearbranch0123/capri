"""
Capri Spatial Presence / Desktop Locomotion Module (v2.2.1)
Управление физическим присутствием аватара Капри на рабочем столе Niri (Wayland).

Архитектура:
1. DesktopObserver — фоновое отслеживание геометрии окон, воркспейсов, мониторов и событий Niri IPC.
2. LocomotionController — контроллер перемещения:
   - STAY: фиксация на месте (не следует за сменой воркспейсов).
   - FOLLOW: следование за активным воркспейсом с дебаунсом 1.5 сек.
   - COME («Капри, ко мне» / /come): причаливание к активному окну.
   - GLITCH TELEPORT: при дистанции > 500px или смене воркспейса (звук ui_glitch.wav + распад + скачок + рематериализация).
   - CRAWL / CYBER-FLOAT: при дистанции <= 500px (плавная 60 FPS ease-in-out интерполяция окна).
   - FULLSCREEN AVOIDANCE: адаптивное снижение непрозрачности при полноэкранных приложениях.
   - HOME BASE («Капри, на базу» / /home): возврат в домашний угол (1590, 615).
   - MANUAL DRAG & DROP: ручное позиционирование сохраняет новый якорь.
"""

from __future__ import annotations

import json
import math
import os
import random
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Any, Callable

from PySide6.QtCore import QObject, Signal, QTimer

from ..core.compositor import (
    compositor,
    detect_compositor,
    get_hyprland_socket2_path,
    parse_hyprland_socket2_line,
)
from ..core.resources import get_assets_dir

ASSETS_DIR = get_assets_dir()
GLITCH_SOUND_PATH = ASSETS_DIR / "ui_glitch.wav"
DEFAULT_HOME_COORDS = (1590.0, 615.0)
GLITCH_DISTANCE_THRESHOLD = 500.0


def play_sound(sound_path: Path):
    """Воспроизводит системный звук через paplay или mpv в отдельном процессе."""
    if not sound_path.exists():
        return
    try:
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


def niri_cmd(*args: str) -> tuple[int, str, str]:
    """Выполняет команду niri и возвращает (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["niri"] + list(args),
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:
        return -1, "", str(e)


def niri_json(*args: str) -> Any:
    """Выполняет niri msg -j <args> и возвращает разобранный JSON."""
    code, out, _ = niri_cmd("msg", "-j", *args)
    if code == 0 and out.strip():
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None
    return None


class LocomotionMode:
    STAY = "stay"
    FOLLOW = "follow"


class DesktopObserver(QObject):
    """
    Отслеживает состояние Niri:
    - активный воркспейс
    - активное (focused) окно и его геометрию
    - полноэкранный режим
    - поток событий niri msg -j event-stream
    """
    workspace_changed = Signal(int, int)  # (ws_idx, ws_id)
    focus_changed = Signal(dict)          # focused_window
    fullscreen_changed = Signal(bool)     # is_fullscreen

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._running = True
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self.active_workspace_id: Optional[int] = None
        self.active_workspace_idx: Optional[int] = None
        self.is_fullscreen: bool = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._event_stream_loop, daemon=True, name="NiriObserverThread")
        self._thread.start()

    def stop(self):
        self._running = False
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _event_stream_loop(self):
        """Непрерывное чтение событий из Niri IPC или Hyprland socket2 с авто-переподключением."""
        comp = detect_compositor()
        if comp == "none":
            self._running = False
            return
        if comp == "hyprland":
            self._hyprland_event_loop()
            return
        while self._running:
            proc = None
            try:
                proc = subprocess.Popen(
                    ["niri", "msg", "-j", "event-stream"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                self._proc = proc
                if proc.stdout is None:
                    break
                for line in iter(proc.stdout.readline, ""):
                    if not self._running:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        self._process_event(data)
                    except RuntimeError:
                        # QObject already deleted by Qt/GC
                        self._running = False
                        break
                    except Exception:
                        pass
            except RuntimeError:
                self._running = False
                break
            except Exception:
                pass
            finally:
                self._proc = None
                if proc:
                    try:
                        proc.terminate()
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass
            if self._running:
                time.sleep(1.5)

    def _hyprland_event_loop(self):
        """Чтение потока событий Hyprland (.socket2.sock) для Spatial Presence."""
        while self._running:
            sock_path = get_hyprland_socket2_path()
            if not sock_path or not sock_path.exists():
                time.sleep(1.5)
                continue
            sock = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect(str(sock_path))
                buf = ""
                while self._running:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        parsed = parse_hyprland_socket2_line(line)
                        if not parsed:
                            continue
                        ev_name, ev_data = parsed
                        try:
                            self._process_hyprland_event(ev_name, ev_data)
                        except RuntimeError:
                            self._running = False
                            return
            except Exception:
                pass
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
            if self._running:
                time.sleep(1.5)

    def _process_hyprland_event(self, ev_name: str, ev_data: str):
        if ev_name in ("workspace", "workspacev2"):
            raw_ws = ev_data.split(",")[0].strip()
            try:
                ws_id = int(raw_ws)
            except ValueError:
                return
            if ws_id != self.active_workspace_id:
                self.active_workspace_id = ws_id
                self.active_workspace_idx = ws_id
                self.workspace_changed.emit(ws_id, ws_id)
        elif ev_name in ("activewindow", "activewindowv2"):
            fw = self.get_focused_window()
            if fw:
                self.focus_changed.emit(fw)
                self._check_fullscreen(fw)
        elif ev_name == "fullscreen":
            is_fs = ev_data.strip() == "1"
            if is_fs != self.is_fullscreen:
                self.is_fullscreen = is_fs
                self.fullscreen_changed.emit(is_fs)

    def _process_event(self, event: dict):
        # 1. Смена активного воркспейса
        if "WorkspaceActivated" in event:
            ws_info = event["WorkspaceActivated"]
            ws_id = ws_info.get("id")
            if ws_id is not None and ws_id != self.active_workspace_id:
                self.active_workspace_id = ws_id
                workspaces = self.get_workspaces() or []
                ws_idx = next((w.get("idx") for w in workspaces if w.get("id") == ws_id), 1)
                self.active_workspace_idx = ws_idx
                self.workspace_changed.emit(ws_idx, ws_id)

        elif "WorkspacesChanged" in event:
            workspaces = event["WorkspacesChanged"].get("workspaces", [])
            for ws in workspaces:
                if ws.get("is_active") or ws.get("is_focused"):
                    ws_id = ws.get("id")
                    ws_idx = ws.get("idx", 1)
                    if ws_id != self.active_workspace_id:
                        self.active_workspace_id = ws_id
                        self.active_workspace_idx = ws_idx
                        self.workspace_changed.emit(ws_idx, ws_id)
                    break

        # 2. Смена фокуса окна
        if "WindowFocusChanged" in event:
            fw = self.get_focused_window()
            if fw:
                self.focus_changed.emit(fw)
                self._check_fullscreen(fw)

        elif "WindowsChanged" in event:
            fw = self.get_focused_window()
            if fw:
                self._check_fullscreen(fw)

    def _check_fullscreen(self, fw: dict):
        layout = fw.get("layout", {})
        win_size = layout.get("window_size")
        tile_size = layout.get("tile_size")
        screen_w, screen_h = self.get_screen_size()
        # Полноэкранное окно в Niri занимает экран целиком без зазоров
        is_fs = False
        if win_size and win_size[0] >= screen_w and win_size[1] >= screen_h:
            is_fs = True
        elif tile_size and tile_size[0] >= screen_w and tile_size[1] >= screen_h:
            is_fs = True

        if is_fs != self.is_fullscreen:
            self.is_fullscreen = is_fs
            self.fullscreen_changed.emit(is_fs)


    def get_windows(self) -> list[dict]:
        if detect_compositor() == "hyprland":
            return compositor.get_windows()
        res = niri_json("windows")
        return res if isinstance(res, list) else []

    def get_workspaces(self) -> list[dict]:
        if detect_compositor() == "hyprland":
            return compositor.get_workspaces()
        res = niri_json("workspaces")
        return res if isinstance(res, list) else []

    def get_focused_window(self) -> Optional[dict]:
        if detect_compositor() == "hyprland":
            return compositor.get_focused_window()
        res = niri_json("focused-window")
        return res if isinstance(res, dict) and "id" in res else None

    def get_avatar_window(self, avatar_pid: Optional[int] = None) -> Optional[dict]:
        windows = self.get_windows()
        # 1. Поиск по PID процесса аватара
        if avatar_pid:
            for w in windows:
                if w.get("pid") == avatar_pid:
                    return w
        # 2. Поиск по app_id
        for w in windows:
            if w.get("app_id") == "capri-avatar":
                return w
        return None

    def get_active_workspace(self) -> Optional[dict]:
        workspaces = self.get_workspaces()
        for ws in workspaces:
            if ws.get("is_active") or ws.get("is_focused"):
                return ws
        return workspaces[0] if workspaces else None

    def get_screen_size(self) -> tuple[int, int]:
        if detect_compositor() == "hyprland":
            return compositor.get_screen_size()
        outputs = niri_json("outputs")
        if isinstance(outputs, dict):
            for name, out in outputs.items():
                logical = out.get("logical")
                if logical and "width" in logical and "height" in logical:
                    return int(logical["width"]), int(logical["height"])
        return 1920, 1080


class LocomotionController(QObject):
    """
    Машина состояний пространственного перемещения аватара Капри.
    """
    locomotion_started = Signal(str, float, float)  # (action, target_x, target_y)
    locomotion_finished = Signal(str, float, float)
    caption_requested = Signal(str)
    glitch_requested = Signal()
    opacity_requested = Signal(float)

    def __init__(self, avatar_widget=None, start_observer: Optional[bool] = None):
        super().__init__(avatar_widget)
        self.avatar_widget = avatar_widget
        self.mode: str = LocomotionMode.STAY
        self.home_coords: tuple[float, float] = DEFAULT_HOME_COORDS
        self.anchor_coords: tuple[float, float] = DEFAULT_HOME_COORDS
        self.avatar_size: tuple[int, int] = (300, 420)

        self.observer = DesktopObserver(self)
        self.observer.workspace_changed.connect(self._on_workspace_changed_event)
        self.observer.fullscreen_changed.connect(self._on_fullscreen_changed_event)
        should_start = (
            start_observer
            if start_observer is not None
            else (os.environ.get("QT_QPA_PLATFORM") != "offscreen")
        )
        if should_start:
            self.observer.start()
        self.destroyed.connect(lambda *_: self.observer.stop())

        # Флаги выполнения
        self.is_moving: bool = False

        # Таймер дебаунса следования за воркспейсом (1.5 сек)
        self.follow_debounce_timer = QTimer(self)
        self.follow_debounce_timer.setSingleShot(True)
        self.follow_debounce_timer.timeout.connect(self._do_debounced_workspace_follow)
        self.pending_target_ws_idx: Optional[int] = None

        # Таймер плавной интерполяции Crawl/Glide
        self.glide_timer = QTimer(self)
        self.glide_timer.timeout.connect(self._on_glide_tick)
        self.glide_data: dict = {}
        self._teleport_generation: int = 0

        # Реплики с характером при подзыве («Капри, ко мне»)
        self.come_quotes = [
            "Чего хотел?",
            "Уже здесь, шеф. Чо стряслось?",
            "Телепортация завершена. Внимаю.",
            "Приползла. Выкладывай, шеф.",
            "Я рядом. Не теряй меня из виду.",
            "Кибер-конструкт прибыл по координатам.",
            "Звал? Я здесь.",
        ]

    # ──────────────────────────────────────────────────────────────────────────
    # Инспекция текущей позиции
    # ──────────────────────────────────────────────────────────────────────────
    def get_avatar_id(self) -> Optional[int]:
        w = self.observer.get_avatar_window(os.getpid())
        return w.get("id") if w else None

    def get_current_coords(self) -> tuple[float, float]:
        w = self.observer.get_avatar_window(os.getpid())
        if w:
            pos = w.get("layout", {}).get("tile_pos_in_workspace_view")
            if pos and len(pos) == 2:
                return float(pos[0]), float(pos[1])
        return self.anchor_coords

    def get_current_workspace_idx(self) -> int:
        w = self.observer.get_avatar_window(os.getpid())
        if w:
            ws_id = w.get("workspace_id")
            for ws in self.observer.get_workspaces():
                if ws.get("id") == ws_id:
                    return ws.get("idx", 1)
        return 1

    # ──────────────────────────────────────────────────────────────────────────
    # Основные команды перемещения
    # ──────────────────────────────────────────────────────────────────────────
    def come(self) -> tuple[bool, str]:
        """
        «Капри, ко мне» / /come:
        Определяет геометрию активного окна и причаливает к нему.
        Выбирает Glitch Teleport (>500px / другой воркспейс) или Crawl (<=500px).
        """
        fw = self.observer.get_focused_window()
        screen_w, screen_h = self.observer.get_screen_size()
        av_w, av_h = self.avatar_size

        target_ws_idx = None
        target_x, target_y = self.home_coords

        if fw and fw.get("app_id") != "capri-avatar":
            # Воркспейс целевого окна
            fw_ws_id = fw.get("workspace_id")
            for ws in self.observer.get_workspaces():
                if ws.get("id") == fw_ws_id:
                    target_ws_idx = ws.get("idx", 1)
                    break

            layout = fw.get("layout", {})
            tile_size = layout.get("tile_size") or [900.0, 900.0]
            w_width, w_height = float(tile_size[0]), float(tile_size[1])

            # Окно плавающее или тайлинговое?
            if fw.get("is_floating"):
                pos = layout.get("tile_pos_in_workspace_view")
                win_x = float(pos[0]) if pos else 300.0
                win_y = float(pos[1]) if pos else 150.0

                # Причаливаем справа от плавающего окна
                target_x = win_x + w_width + 16.0
                if target_x + av_w > screen_w - 20:
                    # Если справа не влезает — встаем слева
                    target_x = win_x - av_w - 16.0
                target_y = min(max(50.0, win_y + 20.0), screen_h - av_h - 45.0)
            else:
                # Тайлинговое окно Niri
                if w_width >= screen_w - 70:
                    # Окно на всю ширину (Firefox, ChatGPT, Code)
                    # Садимся в правый верхний угол как оверлей-ассистент
                    target_x = screen_w - av_w - 30.0
                    target_y = 75.0
                else:
                    # Колонки Niri обычно отцентрированы или прижаты с зазором gaps 25
                    win_x = max(25.0, (screen_w - w_width) / 2.0)
                    target_x = win_x + w_width + 16.0
                    if target_x + av_w > screen_w - 20:
                        target_x = win_x - av_w - 16.0
                    target_y = min(max(50.0, 50.0 + (w_height - av_h) / 2.0), screen_h - av_h - 45.0)
        else:
            # Нет активного окна — встаем по центру справа
            active_ws = self.observer.get_active_workspace()
            if active_ws:
                target_ws_idx = active_ws.get("idx", 1)
            target_x = screen_w - av_w - 30.0
            target_y = max(60.0, (screen_h - av_h) / 2.0)

        # Ограничиваем координаты границами экрана
        target_x = max(20.0, min(target_x, screen_w - av_w - 20.0))
        target_y = max(20.0, min(target_y, screen_h - av_h - 20.0))

        cur_x, cur_y = self.get_current_coords()
        cur_ws_idx = self.get_current_workspace_idx()
        dist = math.hypot(target_x - cur_x, target_y - cur_y)

        quote = random.choice(self.come_quotes)
        self.caption_requested.emit(quote)

        # Если на другом воркспейсе или дистанция больше порога — Glitch Teleport
        if (target_ws_idx is not None and target_ws_idx != cur_ws_idx) or dist > GLITCH_DISTANCE_THRESHOLD:
            self.teleport(target_x, target_y, target_ws_idx=target_ws_idx)
            return True, f"Glitch teleport к окну ({target_x:.0f}, {target_y:.0f})"
        else:
            self.glide(target_x, target_y)
            return True, f"Crawl glide к окну ({target_x:.0f}, {target_y:.0f})"

    def stay(self) -> tuple[bool, str]:
        """«Капри, стой тут» / /stay: фиксация на месте, отключение следования."""
        self.mode = LocomotionMode.STAY
        self.anchor_coords = self.get_current_coords()
        msg = "Остаюсь тут. Свистнешь, если понадоблюсь."
        self.caption_requested.emit(msg)
        return True, "Stay mode active"

    def follow(self, enabled: bool = True) -> tuple[bool, str]:
        """Включение / выключение следования за активным воркспейсом."""
        if enabled:
            self.mode = LocomotionMode.FOLLOW
            msg = "Режим следования активирован. Иду за тобой."
            # Если текущий активный воркспейс уже отличается — синхронизируемся сразу
            active_ws = self.observer.get_active_workspace()
            if active_ws:
                cur_ws_idx = self.get_current_workspace_idx()
                act_ws_idx = active_ws.get("idx", 1)
                if cur_ws_idx != act_ws_idx:
                    self._on_workspace_changed_event(act_ws_idx, active_ws.get("id", 1))
        else:
            self.mode = LocomotionMode.STAY
            msg = "Следование отключено. Зафиксирована."
        self.caption_requested.emit(msg)
        return True, f"Follow mode: {self.mode}"


    def home(self) -> tuple[bool, str]:
        """«Капри, на базу» / /home: возврат в исходный угол."""
        screen_w, screen_h = self.observer.get_screen_size()
        av_w, av_h = self.avatar_size
        target_x = max(20.0, min(self.home_coords[0], screen_w - av_w - 30.0))
        target_y = max(20.0, min(self.home_coords[1], screen_h - av_h - 45.0))

        cur_x, cur_y = self.get_current_coords()
        dist = math.hypot(target_x - cur_x, target_y - cur_y)

        self.caption_requested.emit("Возвращаюсь на базу.")
        if dist > GLITCH_DISTANCE_THRESHOLD:
            self.teleport(target_x, target_y)
        else:
            self.glide(target_x, target_y)
        return True, "Returning home"

    # ──────────────────────────────────────────────────────────────────────────
    # Механики перемещения: Glitch Teleport и Crawl/Glide
    # ──────────────────────────────────────────────────────────────────────────
    def teleport(self, target_x: float, target_y: float, target_ws_idx: Optional[int] = None):
        """Быстрое перемещение с глитч-эффектом и звуком ui_glitch.wav."""
        if self.is_moving:
            self.glide_timer.stop()

        self._teleport_generation += 1
        gen = self._teleport_generation

        self.is_moving = True
        self.locomotion_started.emit("teleport", target_x, target_y)

        play_sound(GLITCH_SOUND_PATH)
        self.glitch_requested.emit()
        self.opacity_requested.emit(0.2)

        # Фаза 1: Распад в глитч (140мс), затем прыжок координат
        QTimer.singleShot(140, lambda: self._execute_teleport_jump(gen, target_x, target_y, target_ws_idx))

    def _execute_teleport_jump(self, gen: int, target_x: float, target_y: float, target_ws_idx: Optional[int]):
        # Отменяем устаревший колбэк при поступлении нового перемещения
        if gen != self._teleport_generation:
            return

        avatar_id = self.get_avatar_id()
        if avatar_id:
            comp = detect_compositor()
            # Перемещение между воркспейсами (Niri / Hyprland)
            if target_ws_idx is not None:
                if comp == "hyprland":
                    compositor.move_window_to_workspace_sync(avatar_id, int(target_ws_idx))
                else:
                    niri_cmd("msg", "action", "move-window-to-workspace", "--window-id", str(avatar_id), "--focus", "false", str(target_ws_idx))

            # Перемещение в целевые координаты
            cur_x, cur_y = self.get_current_coords()
            dx = int(round(target_x - cur_x))
            dy = int(round(target_y - cur_y))
            if dx != 0 or dy != 0:
                if comp == "hyprland":
                    compositor.move_floating_window_sync(avatar_id, dx, dy)
                else:
                    niri_cmd("msg", "action", "move-floating-window", "--id", str(avatar_id), "-x", f"{dx:+d}", "-y", f"{dy:+d}")

        # Фаза 2: Рематериализация (140мс)
        QTimer.singleShot(140, lambda: self._finish_teleport(gen, target_x, target_y))

    def _finish_teleport(self, gen: int, target_x: float, target_y: float):
        if gen != self._teleport_generation:
            return

        # Восстанавливаем непрозрачность с учётом текущего полноэкранного режима
        target_opacity = 0.18 if getattr(self.observer, "is_fullscreen", False) else 1.0
        self.opacity_requested.emit(target_opacity)
        self.glitch_requested.emit()
        self.is_moving = False
        self.anchor_coords = (target_x, target_y)
        self.locomotion_finished.emit("teleport", target_x, target_y)


    def glide(self, target_x: float, target_y: float, duration_ms: int = 320, steps: int = 18):
        """Плавное переползание (Crawl/Cyber-Float) с ease-in-out интерполяцией."""
        if self.is_moving:
            self.glide_timer.stop()

        self._teleport_generation += 1

        avatar_id = self.get_avatar_id()
        if not avatar_id:
            return

        cur_x, cur_y = self.get_current_coords()
        if round(cur_x) == round(target_x) and round(cur_y) == round(target_y):
            return

        self.is_moving = True
        self.locomotion_started.emit("glide", target_x, target_y)

        interval = max(14, duration_ms // steps)
        self.glide_data = {
            "avatar_id": avatar_id,
            "x0": cur_x,
            "y0": cur_y,
            "x1": target_x,
            "y1": target_y,
            "prev_x": cur_x,
            "prev_y": cur_y,
            "current_step": 1,
            "total_steps": steps,
        }
        self.glide_timer.start(interval)

    def _on_glide_tick(self):
        data = self.glide_data
        step = data["current_step"]
        total = data["total_steps"]
        avatar_id = data["avatar_id"]

        t = step / total
        # Плавная функция сглаживания ease-in-out
        ease = 0.5 * (1.0 - math.cos(math.pi * t))

        cur_x = data["x0"] + (data["x1"] - data["x0"]) * ease
        cur_y = data["y0"] + (data["y1"] - data["y0"]) * ease

        dx = int(round(cur_x) - round(data["prev_x"]))
        dy = int(round(cur_y) - round(data["prev_y"]))

        if dx != 0 or dy != 0:
            if detect_compositor() == "hyprland":
                compositor.move_floating_window_sync(avatar_id, dx, dy)
            else:
                niri_cmd("msg", "action", "move-floating-window", "--id", str(avatar_id), "-x", f"{dx:+d}", "-y", f"{dy:+d}")
            data["prev_x"] = cur_x
            data["prev_y"] = cur_y

        data["current_step"] += 1
        if step >= total:
            self.glide_timer.stop()
            self.is_moving = False
            self.anchor_coords = (data["x1"], data["y1"])
            self.locomotion_finished.emit("glide", data["x1"], data["y1"])

    # ──────────────────────────────────────────────────────────────────────────
    # События: воркспейсы, полноэкранный режим, ручной drag & drop
    # ──────────────────────────────────────────────────────────────────────────
    def _on_workspace_changed_event(self, ws_idx: int, ws_id: int):
        """Событие смены рабочего стола Niri."""
        if self.mode != LocomotionMode.FOLLOW:
            return

        # Запускаем дебаунс на 1.5 сек (как в ТЗ D), чтобы Капри не металась при быстром скролле
        self.pending_target_ws_idx = ws_idx
        self.follow_debounce_timer.start(1500)

    def _do_debounced_workspace_follow(self):
        if self.mode != LocomotionMode.FOLLOW or self.pending_target_ws_idx is None:
            return

        target_ws_idx = self.pending_target_ws_idx
        cur_ws_idx = self.get_current_workspace_idx()
        if cur_ws_idx == target_ws_idx:
            return

        avatar_id = self.get_avatar_id()
        if avatar_id:
            play_sound(GLITCH_SOUND_PATH)
            self.glitch_requested.emit()
            if detect_compositor() == "hyprland":
                compositor.move_window_to_workspace_sync(avatar_id, int(target_ws_idx))
            else:
                niri_cmd("msg", "action", "move-window-to-workspace", "--window-id", str(avatar_id), "--focus", "false", str(target_ws_idx))

    def _on_fullscreen_changed_event(self, is_fs: bool):
        """Полноэкранное приложение: аккуратно снижаем видимость или возвращаем."""
        if is_fs:
            # Призрачная полупрозрачность чтобы не перекрывать видео / игры
            self.opacity_requested.emit(0.18)
        else:
            self.opacity_requested.emit(1.0)

    def on_manual_drag_end(self, new_x: float, new_y: float):
        """Вызывается когда пользователь сам перетащил аватар мышью."""
        # Запоминаем новые координаты как текущий якорь
        self.anchor_coords = (new_x, new_y)

    def handle_command(self, payload: dict) -> tuple[bool, str]:
        """Обработка IPC-команд локомоции от Cyberdeck, CLI или инструментов."""
        action = payload.get("action", "").lower().strip()
        if action in ("come", "ко мне"):
            return self.come()
        elif action in ("stay", "стой"):
            return self.stay()
        elif action in ("follow", "следуй"):
            enabled = payload.get("enabled", True)
            return self.follow(enabled=enabled)
        elif action in ("home", "на базу"):
            return self.home()
        elif action == "glide":
            tx = payload.get("x", self.home_coords[0])
            ty = payload.get("y", self.home_coords[1])
            self.glide(float(tx), float(ty))
            return True, f"Gliding to ({tx}, {ty})"
        elif action == "teleport":
            tx = payload.get("x", self.home_coords[0])
            ty = payload.get("y", self.home_coords[1])
            tws = payload.get("workspace_idx")
            self.teleport(float(tx), float(ty), target_ws_idx=tws)
            return True, f"Teleporting to ({tx}, {ty})"
        return False, f"Unknown action: {action}"
