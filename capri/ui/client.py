"""
IPC-клиент для отправки событий из терминального агента Capri в плавающий UI-виджет аватара.
Если виджет запущен, он мгновенно меняет спрайт и анимацию (idle, listening, thinking, speaking, playful).
Если виджет не запущен, вызовы безопасно игнорируются без задержек.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Optional

SOCKET_PATH = "/tmp/capri_avatar.sock"


def notify_avatar(state: str, text: str = "", metadata: Optional[dict] = None) -> bool:
    """
    Отправляет команду аватару через локальный UNIX-сокет.
    state: "idle" | "listening" | "thinking" | "speaking" | "playful"
    text: подпись/субтитры или название выполняемого инструмента
    """
    if not Path(SOCKET_PATH).exists():
        return False

    payload = {
        "state": state,
        "text": text,
        "metadata": metadata or {},
    }
    raw_data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.05)  # Не блокируем основной цикл
            sock.connect(SOCKET_PATH)
            sock.sendall(raw_data)
            return True
    except Exception:
        return False


def set_avatar_mode(mode: str) -> bool:
    """Переключает режим аватара ('2d' или '3d')."""
    if not Path(SOCKET_PATH).exists():
        return False

    payload = {"mode": mode}
    raw_data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.05)
            sock.connect(SOCKET_PATH)
            sock.sendall(raw_data)
            return True
    except (ConnectionRefusedError, FileNotFoundError):
        try:
            Path(SOCKET_PATH).unlink(missing_ok=True)
        except Exception:
            pass
        return False
    except Exception:
        return False


def notify_avatar_locomotion(action: str, retries: int = 4, **kwargs) -> bool:
    """
    Отправляет команду пространственного перемещения аватару (Capri Spatial Presence):
    action: "come" | "stay" | "follow" | "home" | "glide" | "teleport"
    kwargs: target_x, target_y, enabled, etc.
    """
    import time
    for _ in range(max(1, retries)):
        if Path(SOCKET_PATH).exists():
            break
        time.sleep(0.06)

    if not Path(SOCKET_PATH).exists():
        return False

    payload = {
        "type": "locomotion",
        "action": action,
        **kwargs,
    }
    raw_data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.12)
            sock.connect(SOCKET_PATH)
            sock.sendall(raw_data)
            return True
    except (ConnectionRefusedError, FileNotFoundError):
        try:
            Path(SOCKET_PATH).unlink(missing_ok=True)
        except Exception:
            pass
        return False
    except Exception:
        return False


def send_avatar_control(payload: dict) -> bool:
    """Send arbitrary control payload (power_saver, config_sync, pose, etc.) to running avatar."""
    if not Path(SOCKET_PATH).exists():
        return False
    raw_data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.08)
            sock.connect(SOCKET_PATH)
            sock.sendall(raw_data)
            return True
    except Exception:
        return False


