"""
Zero-Qt cross-language end-to-end IPC integration test:
Python `capri.ui.client` -> live `capri-avatar-rs --headless` Rust daemon.

Because `capri.ui` uses lazy PEP 562 imports, this test requires ZERO PySide6/Qt
dependencies and runs directly inside the `rust-avatar-ci` GitHub Actions job with
`CAPRI_REQUIRE_RUST_AVATAR_E2E=1`.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from capri.ui import avatar_ipc
from capri.ui import client as ui_client

REPO_ROOT = Path(__file__).resolve().parent.parent


def _find_rust_avatar_binary() -> Path | None:
    override = os.environ.get("CAPRI_AVATAR_RS_BIN", "").strip()
    if override:
        cand = Path(override).expanduser()
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    for rel in (
        Path("rust") / "capri-avatar-rs" / "target" / "release" / "capri-avatar-rs",
        Path("rust") / "capri-avatar-rs" / "target" / "debug" / "capri-avatar-rs",
    ):
        cand = REPO_ROOT / rel
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    found = shutil.which("capri-avatar-rs")
    return Path(found) if found else None


def test_python_to_rust_avatar_e2e_ipc_roundtrip_without_qt(monkeypatch, tmp_path):
    """Verify full v:1 NDJSON IPC roundtrip from Python client to live Rust `capri-avatar-rs`."""
    rust_bin = _find_rust_avatar_binary()
    if rust_bin is None:
        if os.environ.get("CAPRI_REQUIRE_RUST_AVATAR_E2E") == "1":
            pytest.fail(
                "CAPRI_REQUIRE_RUST_AVATAR_E2E=1 is set, but `capri-avatar-rs` binary was not found. "
                "Build it before running this E2E test."
            )
        pytest.skip("capri-avatar-rs binary not built in this environment")

    run_dir = tmp_path / "run_rs_e2e"
    run_dir.mkdir(mode=0o700)
    sock_path = run_dir / "capri" / "avatar.sock"
    pid_path = run_dir / "capri" / "avatar.pid"

    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = str(run_dir)
    env.pop("CAPRI_AVATAR_SOCKET", None)
    env.pop("CAPRI_AVATAR_PID", None)

    proc = subprocess.Popen(
        [str(rust_bin), "--headless", "--bench-first-frame"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not sock_path.exists():
            assert proc.poll() is None, f"capri-avatar-rs exited early: {proc.stderr.read()}"
            time.sleep(0.02)

        assert sock_path.exists(), "capri-avatar-rs did not create avatar.sock"
        assert pid_path.exists(), "capri-avatar-rs did not create avatar.pid"
        assert stat.S_IMODE(os.lstat(sock_path).st_mode) == 0o600
        assert stat.S_IMODE(os.lstat(pid_path).st_mode) == 0o600
        assert avatar_ipc.read_owned_pid_file(pid_path) == proc.pid

        monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))
        monkeypatch.delenv("CAPRI_AVATAR_SOCKET", raising=False)
        monkeypatch.setattr(ui_client, "SOCKET_PATH", ui_client._DEFAULT_SOCKET_SENTINEL)

        assert (
            ui_client.notify_avatar(
                "speaking",
                "Привет от Python E2E!",
                {"mood": "playful", "mouth_envelope": [0, 3, 2, 1]},
            )
            is True
        )
        assert ui_client.notify_avatar_audio_level(0.65, source="tts") is True
        assert ui_client.notify_avatar_locomotion("come", target_x=400.0, target_y=250.0) is True
        assert ui_client.notify_avatar_locomotion("home") is True
        assert ui_client.send_avatar_control({"type": "focus_timer", "action": "start", "minutes": 25}) is True
        assert ui_client.send_avatar_control({"type": "config_sync", "reduced_motion": True}) is True
        assert ui_client.send_avatar_control({"type": "control", "command": "quit"}) is True

        stdout, stderr = proc.communicate(timeout=5.0)
        assert proc.returncode == 0, f"unexpected exit {proc.returncode}: {stderr}"
        assert "FIRST_FRAME_MS=" in stdout
        assert not sock_path.exists(), "capri-avatar-rs must unlink avatar.sock on clean quit"
        assert not pid_path.exists(), "capri-avatar-rs must unlink avatar.pid on clean quit"
        if os.environ.get("CAPRI_ASSERT_NO_QT") == "1":
            assert "PySide6" not in sys.modules
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
