"""
Capri v3.6 Stage 3 — Arch / CachyOS Ecosystem Tests:
1. `systemd --user` avatar service generator, path escaping, dry-run/install/uninstall lifecycle,
   and `WAYLAND_DISPLAY` environment check.
2. `wait_for_wayland_socket()` auto-discovery + `CapriAvatarWidget(systemd_managed=True)` clean
   non-zero exit (`Restart=on-failure`) when the Wayland compositor closes/restarts.
3. Ready-to-build `packaging/arch/PKGBUILD` static validation (version sync with `capri.__version__`,
   valid bash syntax via `bash -n`, dual code+assets license, `ASSETS_NOTICE.md`, and systemd unit).
4. Enhanced `/doctor` diagnostics (`_check_systemd_avatar_service`, `_check_vision_and_clipboard`,
   distro-aware Arch/CachyOS/Fedora install hints) and `capri --service` CLI routing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

import capri
from capri.cli import cli_main
from capri.core.config import AppConfig
from capri.core.doctor import run_diagnostics
from capri.core.systemd_service import (
    SERVICE_NAME,
    generate_avatar_service_unit,
    get_avatar_service_status,
    install_avatar_service,
    systemd_escape_arg,
    uninstall_avatar_service,
)
from capri.ui.overlay import CapriAvatarWidget, wait_for_wayland_socket

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_systemd_unit_generation_escaping_and_lifecycle(tmp_path):
    assert systemd_escape_arg("/usr/bin/capri-avatar") == "/usr/bin/capri-avatar"
    assert systemd_escape_arg("/opt/my capri/bin/capri-avatar") == '"/opt/my capri/bin/capri-avatar"'

    unit = generate_avatar_service_unit(
        mode="2d",
        exec_cmd=["/opt/my capri/bin/capri-avatar"],
        working_dir=tmp_path,
    )
    assert "PartOf=graphical-session.target" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=2" in unit
    assert '"/opt/my capri/bin/capri-avatar" --mode 2d --systemd' in unit
    assert "WantedBy=graphical-session.target" in unit

    # Dry-run writes nothing
    dry = install_avatar_service(dry_run=True, unit_dir=tmp_path)
    assert dry.ok is True
    assert not (tmp_path / SERVICE_NAME).exists()

    # Install + status + uninstall with mocked systemctl
    with (
        patch("capri.core.systemd_service.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "capri.core.systemd_service._run_systemctl_user",
            side_effect=lambda *args, **kw: (
                (0, "PATH=/usr/bin\n", "") if args and args[0] == "show-environment"
                else (0, "enabled", "") if args and args[0] == "is-enabled"
                else (0, "active", "") if args and args[0] == "is-active"
                else (0, "", "")
            ),
        ),
    ):
        res = install_avatar_service(enable=True, start=False, unit_dir=tmp_path)
        assert res.ok is True
        assert (tmp_path / SERVICE_NAME).is_file()
        assert "WAYLAND_DISPLAY" in res.wayland_env_warning

        st = get_avatar_service_status(unit_dir=tmp_path, system_unit_dirs=())
        assert st.installed is True
        assert st.scope == "user"
        assert st.enabled is True
        assert st.active is True
        assert st.wayland_in_systemd_env is False

        rem = uninstall_avatar_service(unit_dir=tmp_path, system_unit_dirs=())
        assert rem.ok is True
        assert not (tmp_path / SERVICE_NAME).exists()


def test_systemd_status_detects_package_installed_unit_and_safe_uninstall(tmp_path):
    user_dir = tmp_path / "user_systemd"
    sys_dir = tmp_path / "usr_lib_systemd_user"
    sys_dir.mkdir(parents=True)
    pkg_unit = sys_dir / SERVICE_NAME
    pkg_unit.write_text("[Unit]\nDescription=Packaged Capri\n", encoding="utf-8")

    with (
        patch("capri.core.systemd_service.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "capri.core.systemd_service._run_systemctl_user",
            side_effect=lambda *args, **kw: (
                (0, "WAYLAND_DISPLAY=wayland-1\n", "") if args and args[0] == "show-environment"
                else (0, "enabled", "") if args and args[0] == "is-enabled"
                else (0, "active", "") if args and args[0] == "is-active"
                else (0, "", "")
            ),
        ),
    ):
        st = get_avatar_service_status(unit_dir=user_dir, system_unit_dirs=(sys_dir,))
        assert st.installed is True
        assert st.scope == "system"
        assert st.unit_path == pkg_unit
        assert st.enabled is True
        assert st.active is True
        assert st.wayland_in_systemd_env is True
        assert "(пакет)" in st.detail

        # `/doctor` sees the package-installed systemd service cleanly
        cfg = AppConfig()
        cfg.llm.api_key = "sk-test-pkg-unit"
        with patch("capri.core.systemd_service.SYSTEM_SYSTEMD_USER_DIRS", (sys_dir,)):
            with patch("capri.core.systemd_service.get_systemd_user_dir", return_value=user_dir):
                diag = {i.name: i for i in run_diagnostics(cfg)}
        assert "(пакет)" in diag["Systemd-сервис аватара"].detail
        assert "активен" in diag["Systemd-сервис аватара"].detail

        # Uninstalling disables the user enablement without trying to delete `/usr/lib/systemd/user/...`
        rem = uninstall_avatar_service(unit_dir=user_dir, system_unit_dirs=(sys_dir,))
        assert rem.ok is True
        assert pkg_unit.is_file()


def test_systemd_cli_propagates_systemctl_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    # 1. daemon-reload failure on install -> ok=False & CLI exit 1
    with (
        patch("capri.core.systemd_service.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "capri.core.systemd_service._run_systemctl_user",
            return_value=(1, "", "Failed to connect to bus"),
        ),
    ):
        res = install_avatar_service(enable=True, start=False, unit_dir=tmp_path / "u1")
        assert res.ok is False
        assert cli_main(["--service", "install"]) == 1

    # 2. enable failure after successful daemon-reload -> ok=False & CLI exit 1
    with (
        patch("capri.core.systemd_service.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "capri.core.systemd_service._run_systemctl_user",
            side_effect=lambda *args, **kw: (
                (0, "", "") if args and args[0] == "daemon-reload"
                else (1, "", "Unit file error") if args and args[0] == "enable"
                else (0, "WAYLAND_DISPLAY=wayland-1\n", "")
            ),
        ),
    ):
        res = install_avatar_service(enable=True, start=False, unit_dir=tmp_path / "u2")
        assert res.ok is False
        assert cli_main(["--service", "install"]) == 1

    # 3. start failure (--no-enable --start) -> ok=False
    with (
        patch("capri.core.systemd_service.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "capri.core.systemd_service._run_systemctl_user",
            side_effect=lambda *args, **kw: (
                (0, "", "") if args and args[0] == "daemon-reload"
                else (1, "", "start failed") if args and args[0] == "start"
                else (0, "WAYLAND_DISPLAY=wayland-1\n", "")
            ),
        ),
    ):
        res = install_avatar_service(enable=False, start=True, unit_dir=tmp_path / "u3")
        assert res.ok is False

    # 4. uninstall failure on disable --now -> ok=False & CLI exit 1
    u4 = tmp_path / "xdg" / "systemd" / "user"
    u4.mkdir(parents=True, exist_ok=True)
    (u4 / SERVICE_NAME).write_text("[Unit]\nDescription=Test\n", encoding="utf-8")
    with (
        patch("capri.core.systemd_service.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "capri.core.systemd_service._run_systemctl_user",
            side_effect=lambda *args, **kw: (
                (1, "", "Failed to connect to bus") if args and args[0] == "disable"
                else (0, "", "")
            ),
        ),
    ):
        assert cli_main(["--service", "uninstall"]) == 1

    # 5. uninstall failure on daemon-reload -> ok=False
    u5 = tmp_path / "u5"
    u5.mkdir(parents=True, exist_ok=True)
    (u5 / SERVICE_NAME).write_text("[Unit]\nDescription=Test\n", encoding="utf-8")
    with (
        patch("capri.core.systemd_service.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "capri.core.systemd_service._run_systemctl_user",
            side_effect=lambda *args, **kw: (
                (1, "", "reload failed") if args and args[0] == "daemon-reload"
                else (0, "", "")
            ),
        ),
    ):
        res = uninstall_avatar_service(unit_dir=u5, system_unit_dirs=())
        assert res.ok is False


def test_clean_arch_package_env_imports_and_cli_startup():
    """Verify all entrypoints (`capri`, `capri-deck`, `capri-avatar`) import and run cleanly
    when ONLY official Arch `PKGBUILD` dependencies are available (optional pip-only extras absent)."""
    probe = r"""
import sys
import asyncio

BLOCKED = {
    "vosk",
    "sounddevice",
    "faster_whisper",
    "edge_tts",
    "fishaudio",
    "elevenlabs",
    "google",
    "duckduckgo_search",
    "ddgs",
    "av",
    "torch",
    "torchaudio",
    "TTS",
    "evdev",
}

class _Blocker:
    def find_spec(self, fullname, path, target=None):
        top = fullname.split(".")[0]
        if top in BLOCKED:
            raise ModuleNotFoundError(f"Blocked optional package in clean Arch env: {fullname}", name=top)
        return None

sys.meta_path.insert(0, _Blocker())

import capri.cli
import capri.ui.cyberdeck
import capri.ui.overlay
import capri.tools
from capri.voice.wake import WakeWordListener
from capri.voice.ptt import PushToTalkListener
from capri.voice.tts import VoiceSynthesizer
from capri.core.config import VoiceConfig

assert WakeWordListener().is_available() is False
assert WakeWordListener().start() is False
assert PushToTalkListener().start() is False
tts = VoiceSynthesizer(VoiceConfig(tts_provider="edge-tts"))
assert asyncio.run(tts._synthesize_edge_file("hello")) is None
assert capri.cli.cli_main(["--version"]) == 0
assert capri.cli.cli_main(["--doctor"]) == 0
assert capri.cli.cli_main(["--service", "install", "--dry-run"]) == 0
print("CLEAN_ARCH_ENV_OK")
"""
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["CAPRI_DISABLE_LAYER_SHELL"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"Clean Arch env startup failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "CLEAN_ARCH_ENV_OK" in proc.stdout


def test_wayland_socket_wait_and_systemd_compositor_restart_exit(monkeypatch, tmp_path):
    import socket

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CAPRI_DISABLE_LAYER_SHELL", "1")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    QApplication.instance() or QApplication([])

    # Stale non-socket / dead socket files are ignored and time out cleanly
    (tmp_path / "wayland-0.lock").write_text("", encoding="utf-8")
    (tmp_path / "wayland-0").write_text("", encoding="utf-8")
    assert wait_for_wayland_socket(timeout_s=0.05, poll_interval=0.01, runtime_dir=tmp_path) is None

    # Discovers a live listening Unix socket `wayland-1`
    live_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        live_sock.bind(str(tmp_path / "wayland-1"))
        live_sock.listen(2)
        discovered = wait_for_wayland_socket(timeout_s=0.2, poll_interval=0.01, runtime_dir=tmp_path)
        assert discovered == "wayland-1"
        assert os.environ.get("WAYLAND_DISPLAY") == "wayland-1"
    finally:
        live_sock.close()

    # Under systemd_managed=True, compositor disconnect (_on_layer_closed) marks reconnect exit
    widget = CapriAvatarWidget(mode="2d", systemd_managed=True)
    try:
        widget.using_layer_stage = True
        with patch.object(QApplication.instance(), "exit") as exit_mock:
            widget._on_layer_closed()
            assert widget._compositor_reconnect_exit is True
            exit_mock.assert_called_once_with(1)
    finally:
        widget.close()


def test_arch_pkgbuild_validity_and_version_sync():
    pkgbuild = REPO_ROOT / "packaging" / "arch" / "PKGBUILD"
    assert pkgbuild.is_file(), "packaging/arch/PKGBUILD is missing"
    content = pkgbuild.read_text(encoding="utf-8")

    # Version must stay synchronized with capri.__version__
    assert f"pkgver={capri.__version__}" in content
    # Must honestly declare both MIT (code) and PolyForm Noncommercial (assets)
    assert "'MIT'" in content
    assert "PolyForm-Noncommercial-1.0.0" in content
    assert "ASSETS_NOTICE.md" in content
    assert "/usr/lib/systemd/user/capri-avatar.service" in content
    assert "capri-cyberdeck.desktop" in content

    # Valid bash syntax check (`bash -n`)
    res = subprocess.run(["bash", "-n", str(pkgbuild)], capture_output=True, text=True)
    assert res.returncode == 0, f"PKGBUILD syntax error: {res.stderr}"


def test_doctor_enhanced_arch_hints_and_service_cli(monkeypatch, capsys):
    cfg = AppConfig()
    cfg.llm.api_key = "sk-test-doctor-ok"
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")

    with (
        patch("capri.core.doctor.detect_linux_distro", return_value=("cachyos", "CachyOS Linux", True)),
        patch("capri.core.doctor.shutil.which", side_effect=lambda cmd: None if cmd in ("grim", "slurp") else f"/usr/bin/{cmd}"),
        patch(
            "capri.core.systemd_service.get_avatar_service_status",
            return_value=get_avatar_service_status(
                unit_dir=Path("/nonexistent_capri_systemd_dir"),
                system_unit_dirs=(),
            ),
        ),
    ):
        items = run_diagnostics(cfg)
    by_name = {i.name: i for i in items}
    assert "Wayland Vision / Буфер" in by_name
    assert by_name["Wayland Vision / Буфер"].status == "WARN"
    assert "sudo pacman -S grim slurp wl-clipboard" in by_name["Wayland Vision / Буфер"].detail
    assert "Systemd-сервис аватара" in by_name

    # CLI `--service install --dry-run` renders unit cleanly and returns 0
    rc = cli_main(["--service", "install", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "capri-avatar.service" in out
    assert "Restart=on-failure" in out
