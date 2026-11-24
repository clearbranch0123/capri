"""
Capri v3.6 — Optional `systemd --user` service generator & manager for `capri-avatar`.

Features:
- Generates a hardened `capri-avatar.service` unit bound to `graphical-session.target`
- Auto-reconnects on Wayland compositor restart (`Restart=on-failure`, `RestartSec=2`, `--systemd`)
- Inspects `systemctl --user show-environment` to detect missing `WAYLAND_DISPLAY` (the #1 Niri/Hyprland gotcha)
- Safe dry-run preview and atomic unit installation under `$XDG_CONFIG_HOME/systemd/user`
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .config import ROOT_DIR

SERVICE_NAME = "capri-avatar.service"
SYSTEM_SYSTEMD_USER_DIRS: tuple[Path, ...] = (
    Path("/usr/lib/systemd/user"),
    Path("/usr/local/lib/systemd/user"),
)


@dataclass(frozen=True)
class ServiceStatus:
    systemd_available: bool
    unit_path: Path
    installed: bool
    enabled: bool
    active: bool
    wayland_in_systemd_env: Optional[bool]
    detail: str
    scope: str = "none"  # "user" | "system" | "none"


@dataclass(frozen=True)
class ServiceActionResult:
    ok: bool
    unit_path: Path
    unit_content: str
    message: str
    wayland_env_warning: str = ""


def get_systemd_user_dir(unit_dir: Optional[Path] = None) -> Path:
    if unit_dir is not None:
        return Path(unit_dir)
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".config")
    return base / "systemd" / "user"


def systemd_escape_arg(arg: str) -> str:
    """Quote an ExecStart argument safely for systemd unit files when it contains whitespace or quotes."""
    raw = str(arg)
    if not raw or any(ch.isspace() or ch in ('"', "'", "\\", "$", "%") for ch in raw):
        escaped = raw.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        return f'"{escaped}"'
    return raw


def resolve_avatar_exec_command(
    *,
    python_bin: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> list[str]:
    """Resolve the cleanest executable command for `capri-avatar` (venv script, system binary, or `python -m`)."""
    py = Path(python_bin) if python_bin is not None else Path(sys.executable)
    sibling = py.parent / "capri-avatar"
    if sibling.exists() and os.access(sibling, os.X_OK):
        return [str(sibling)]
    found = shutil.which("capri-avatar")
    if found:
        return [found]
    return [str(py), "-m", "capri.ui.overlay"]


def generate_avatar_service_unit(
    *,
    mode: str = "2d",
    exec_cmd: Optional[list[str]] = None,
    working_dir: Optional[Path] = None,
    python_bin: Optional[Path] = None,
) -> str:
    """Render a clean `systemd --user` unit for `capri-avatar` with compositor-restart auto-reconnect."""
    norm_mode = "3d" if str(mode or "2d").strip().lower() == "3d" else "2d"
    cmd_parts = list(exec_cmd) if exec_cmd else resolve_avatar_exec_command(python_bin=python_bin)
    cmd_parts.extend(["--mode", norm_mode, "--systemd"])
    exec_line = " ".join(systemd_escape_arg(p) for p in cmd_parts)

    wd = working_dir if working_dir is not None else ROOT_DIR
    wd_line = f"WorkingDirectory={systemd_escape_arg(str(wd))}\n" if wd else ""

    return (
        "[Unit]\n"
        "Description=Capri AI Companion Wayland Desktop Avatar\n"
        "Documentation=https://github.com/clearbranch0123/capri\n"
        "PartOf=graphical-session.target\n"
        "After=graphical-session.target pipewire.service pipewire-pulse.service\n"
        "StartLimitIntervalSec=60\n"
        "StartLimitBurst=10\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "Environment=QT_QPA_PLATFORM=wayland\n"
        f"{wd_line}"
        f"ExecStart={exec_line}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "TimeoutStopSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=graphical-session.target\n"
    )


def _run_systemctl_user(*args: str, timeout: float = 3.0) -> tuple[int, str, str]:
    ctl = shutil.which("systemctl")
    if not ctl:
        return 127, "", "systemctl not found"
    try:
        proc = subprocess.run(
            [ctl, "--user", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as exc:
        return 1, "", str(exc)


def check_wayland_in_systemd_env() -> Optional[bool]:
    """Return True if `WAYLAND_DISPLAY` is imported into `systemd --user`, False if missing, None if unavailable."""
    if not shutil.which("systemctl"):
        return None
    rc, out, _ = _run_systemctl_user("show-environment", timeout=2.0)
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("WAYLAND_DISPLAY=") and line.split("=", 1)[1].strip():
            return True
    return False


def find_installed_avatar_unit(
    unit_dir: Optional[Path] = None,
    system_unit_dirs: Optional[Sequence[Path]] = None,
) -> tuple[Path, bool, str]:
    """Locate `capri-avatar.service` in user override dir first, then system-packaged user unit dirs."""
    user_path = get_systemd_user_dir(unit_dir) / SERVICE_NAME
    if user_path.is_file():
        return user_path, True, "user"
    sys_dirs = SYSTEM_SYSTEMD_USER_DIRS if system_unit_dirs is None else tuple(system_unit_dirs)
    for sys_dir in sys_dirs:
        candidate = Path(sys_dir) / SERVICE_NAME
        if candidate.is_file():
            return candidate, True, "system"
    return user_path, False, "none"


def get_avatar_service_status(
    unit_dir: Optional[Path] = None,
    system_unit_dirs: Optional[Sequence[Path]] = None,
) -> ServiceStatus:
    unit_path, installed, scope = find_installed_avatar_unit(
        unit_dir=unit_dir,
        system_unit_dirs=system_unit_dirs,
    )
    has_ctl = bool(shutil.which("systemctl"))
    if not has_ctl:
        return ServiceStatus(
            systemd_available=False,
            unit_path=unit_path,
            installed=installed,
            enabled=False,
            active=False,
            wayland_in_systemd_env=None,
            detail="systemd (`systemctl`) недоступен в этом окружении",
            scope=scope,
        )

    enabled = False
    active = False
    if installed:
        rc_en, out_en, _ = _run_systemctl_user("is-enabled", SERVICE_NAME)
        enabled = rc_en == 0 and out_en.strip() in ("enabled", "static", "linked")
        rc_act, out_act, _ = _run_systemctl_user("is-active", SERVICE_NAME)
        active = rc_act == 0 and out_act.strip() == "active"

    wl_env = check_wayland_in_systemd_env()
    if not installed:
        detail = "Не установлен (опционально: `capri --service install`)"
    else:
        st_bits = [
            "включён" if enabled else "отключён",
            "активен" if active else "остановлен",
        ]
        scope_tag = " (пакет)" if scope == "system" else ""
        detail = f"{SERVICE_NAME}{scope_tag}: {', '.join(st_bits)} ({unit_path})"
    return ServiceStatus(
        systemd_available=True,
        unit_path=unit_path,
        installed=installed,
        enabled=enabled,
        active=active,
        wayland_in_systemd_env=wl_env,
        detail=detail,
        scope=scope,
    )


def install_avatar_service(
    *,
    mode: str = "2d",
    enable: bool = True,
    start: bool = False,
    dry_run: bool = False,
    unit_dir: Optional[Path] = None,
    exec_cmd: Optional[list[str]] = None,
    working_dir: Optional[Path] = None,
) -> ServiceActionResult:
    """Install (or dry-run preview) `capri-avatar.service` under systemd user directory."""
    target_dir = get_systemd_user_dir(unit_dir)
    unit_path = target_dir / SERVICE_NAME
    content = generate_avatar_service_unit(mode=mode, exec_cmd=exec_cmd, working_dir=working_dir)

    if dry_run:
        return ServiceActionResult(
            ok=True,
            unit_path=unit_path,
            unit_content=content,
            message=f"Предпросмотр unit-файла ({unit_path}). Для установки: capri --service install",
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = unit_path.with_suffix(".service.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, unit_path)

    ok = True
    notes: list[str] = [f"Установлен unit: {unit_path}"]
    wl_warn = ""
    if shutil.which("systemctl"):
        rc_rel, _, err_rel = _run_systemctl_user("daemon-reload")
        if rc_rel == 0:
            if enable:
                args = ["enable", "--now", SERVICE_NAME] if start else ["enable", SERVICE_NAME]
                rc_en, _, err_en = _run_systemctl_user(*args)
                if rc_en == 0:
                    notes.append("Сервис включён в graphical-session.target" + (" и запущен" if start else ""))
                else:
                    ok = False
                    notes.append(f"Не удалось выполнить systemctl --user enable: {err_en or rc_en}")
            elif start:
                rc_st, _, err_st = _run_systemctl_user("start", SERVICE_NAME)
                if rc_st == 0:
                    notes.append("Сервис запущен")
                else:
                    ok = False
                    notes.append(f"Не удалось запустить сервис: {err_st or rc_st}")
            if check_wayland_in_systemd_env() is False:
                wl_warn = (
                    "WAYLAND_DISPLAY не найден в окружении systemd --user. "
                    "Добавь в автозапуск композитора: `systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP`"
                )
        else:
            ok = False
            notes.append(f"systemctl --user daemon-reload вернул {rc_rel}: {err_rel}")
    else:
        notes.append("systemctl не найден — unit сохранён на диск без активации")

    return ServiceActionResult(
        ok=ok,
        unit_path=unit_path,
        unit_content=content,
        message="\n".join(notes),
        wayland_env_warning=wl_warn,
    )


def uninstall_avatar_service(
    *,
    unit_dir: Optional[Path] = None,
    system_unit_dirs: Optional[Sequence[Path]] = None,
) -> ServiceActionResult:
    """Disable, stop, and remove user `capri-avatar.service` cleanly (propagating systemctl errors)."""
    target_dir = get_systemd_user_dir(unit_dir)
    user_unit_path = target_dir / SERVICE_NAME
    installed_path, is_installed, scope = find_installed_avatar_unit(
        unit_dir=unit_dir,
        system_unit_dirs=system_unit_dirs,
    )

    ok = True
    notes: list[str] = []
    has_ctl = bool(shutil.which("systemctl"))

    if has_ctl and is_installed:
        rc_dis, _, err_dis = _run_systemctl_user("disable", "--now", SERVICE_NAME)
        if rc_dis != 0:
            ok = False
            notes.append(f"Не удалось выполнить systemctl --user disable --now: {err_dis or rc_dis}")

    removed = False
    if user_unit_path.exists():
        user_unit_path.unlink()
        removed = True
        notes.append(f"Сервис удалён: {user_unit_path}")
    elif scope == "system":
        notes.append(f"Системный unit из пакета ({installed_path}) отключён для текущего пользователя")
    else:
        notes.append(f"Unit-файл не найден: {user_unit_path}")

    if has_ctl and (removed or is_installed):
        rc_rel, _, err_rel = _run_systemctl_user("daemon-reload")
        if rc_rel != 0:
            ok = False
            notes.append(f"systemctl --user daemon-reload вернул {rc_rel}: {err_rel}")

    return ServiceActionResult(
        ok=ok,
        unit_path=installed_path if is_installed else user_unit_path,
        unit_content="",
        message="\n".join(notes),
    )
