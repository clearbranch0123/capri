"""
IPC-клиент для отправки событий из терминального агента Capri в плавающий UI-виджет аватара.
Если виджет запущен, он мгновенно меняет спрайт и анимацию (idle, listening, thinking, speaking, playful).
Если виджет не запущен, вызовы безопасно игнорируются без задержек.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Optional

from .avatar_ipc import (
    encode_avatar_ipc_message,
    get_avatar_socket_candidates,
    get_avatar_socket_path,
    is_owned_unix_socket,
    peer_socket_matches_current_uid,
    safe_unlink_owned_path,
)

# Public module-level attribute preserved for backward compatibility and test isolation.
# When left untouched, client calls dynamically resolve `$XDG_RUNTIME_DIR/capri/avatar.sock`
# with legacy `/tmp/capri_avatar.sock` fallback during transition.
_DEFAULT_SOCKET_SENTINEL = get_avatar_socket_path()
SOCKET_PATH = _DEFAULT_SOCKET_SENTINEL


def _resolve_avatar_socket_candidates() -> list[str]:
    if SOCKET_PATH != _DEFAULT_SOCKET_SENTINEL:
        return [SOCKET_PATH]
    return get_avatar_socket_candidates(include_legacy=True)


def _send_avatar_ipc(
    payload: dict,
    *,
    timeout: float = 0.05,
    retries: int = 1,
    cleanup_stale: bool = False,
) -> bool:
    try:
        raw_data = encode_avatar_ipc_message(payload)
    except Exception:
        return False

    candidates = _resolve_avatar_socket_candidates()
    for _ in range(max(1, int(retries))):
        if any(is_owned_unix_socket(c) for c in candidates):
            break
        if retries > 1:
            time.sleep(0.06)

    for target in candidates:
        if not is_owned_unix_socket(target):
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(target)
                if not peer_socket_matches_current_uid(sock):
                    continue
                sock.sendall(raw_data)
                return True
        except (ConnectionRefusedError, FileNotFoundError):
            if cleanup_stale:
                safe_unlink_owned_path(target)
            continue
        except Exception:
            continue
    return False


def get_cyberdeck_ipc_socket_path() -> str:
    """Return per-user Unix socket path for running Capri Cyberdeck IPC."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir and os.path.isdir(runtime_dir):
        return str(Path(runtime_dir) / "capri-cyberdeck.sock")
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return f"/tmp/capri-cyberdeck-{uid}.sock"


def send_cyberdeck_command(
    cmd: str,
    socket_path: Optional[str] = None,
    timeout: float = 0.25,
    extra: Optional[dict] = None,
) -> bool:
    """Send a lightweight JSON command ('toggle'|'show'|'hide'|'quit'|'voice_toggle'|'voice_stop'|'focus_gate') to Cyberdeck."""
    target = socket_path or get_cyberdeck_ipc_socket_path()
    if not os.path.exists(target):
        return False
    body: dict = {"cmd": str(cmd).strip().lower()}
    if isinstance(extra, dict):
        body.update(extra)
    raw = (json.dumps(body) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(target)
            sock.sendall(raw)
            try:
                return bool(sock.recv(64))
            except Exception:
                return True
    except OSError:
        return False


def notify_avatar(state: str, text: str = "", metadata: Optional[dict] = None) -> bool:
    """
    Отправляет команду аватару через локальный UNIX-сокет (`v: 1` NDJSON).
    state: "idle" | "listening" | "thinking" | "speaking" | "playful"
    text: подпись/субтитры или название выполняемого инструмента
    """
    payload = {
        "state": state,
        "text": text,
        "metadata": metadata or {},
    }
    return _send_avatar_ipc(payload, timeout=0.05)


def set_avatar_mode(mode: str) -> bool:
    """Переключает режим аватара ('2d' или '3d')."""
    return _send_avatar_ipc({"mode": mode}, timeout=0.05, cleanup_stale=True)


def notify_avatar_locomotion(action: str, retries: int = 4, **kwargs) -> bool:
    """
    Отправляет команду пространственного перемещения аватару (Capri Spatial Presence):
    action: "come" | "stay" | "follow" | "home" | "glide" | "teleport"
    kwargs: target_x, target_y, enabled, etc.
    """
    payload = {
        "type": "locomotion",
        "action": action,
        **kwargs,
    }
    return _send_avatar_ipc(payload, timeout=0.12, retries=retries, cleanup_stale=True)


def send_avatar_control(payload: dict) -> bool:
    """Send arbitrary control payload (power_saver, config_sync, pose, etc.) to running avatar."""
    return _send_avatar_ipc(payload, timeout=0.08)


def notify_avatar_audio_level(level: float, source: str = "mic") -> bool:
    """
    Send lightweight normalized audio energy (0.0..1.0) to the desktop avatar for
    real-time voice-reactive aura and equalizer rendering.
    """
    clamped = max(0.0, min(1.0, float(level)))
    payload = {"type": "audio_level", "level": round(clamped, 3), "source": str(source or "mic")}
    return _send_avatar_ipc(payload, timeout=0.025)
