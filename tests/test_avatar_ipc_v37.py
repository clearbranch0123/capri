"""Tests for versioned, per-user `$XDG_RUNTIME_DIR/capri/` avatar IPC contract and migration fallback."""
from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

import capri.ui.avatar_ipc as avatar_ipc
import capri.ui.client as ui_client
import capri.ui.overlay as ui_overlay


def test_avatar_ipc_paths_permissions_and_symlink_rejection(monkeypatch, tmp_path):
    run_dir = tmp_path / "xdg_run"
    run_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))
    monkeypatch.delenv("CAPRI_AVATAR_SOCKET", raising=False)
    monkeypatch.delenv("CAPRI_AVATAR_PID", raising=False)

    capri_dir = avatar_ipc.ensure_avatar_runtime_dir()
    assert capri_dir == run_dir / "capri"
    assert stat.S_IMODE(os.lstat(capri_dir).st_mode) == 0o700
    assert avatar_ipc.get_avatar_socket_path() == str(capri_dir / "avatar.sock")
    assert avatar_ipc.get_avatar_pid_path() == str(capri_dir / "avatar.pid")

    # Symlinked runtime dir must be refused (fail-closed)
    real_other = tmp_path / "other"
    real_other.mkdir()
    sym_dir = tmp_path / "sym_capri"
    sym_dir.symlink_to(real_other)
    with pytest.raises(PermissionError):
        avatar_ipc.ensure_avatar_runtime_dir(sym_dir)

    # Fallback when XDG_RUNTIME_DIR is absent uses per-user /tmp/capri-<uid>
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    fb = avatar_ipc.get_avatar_runtime_dir()
    assert fb.name == f"capri-{os.getuid()}"


def test_avatar_ipc_codec_versioning_and_validation():
    raw = avatar_ipc.encode_avatar_ipc_message({"state": "speaking", "text": "Привет"})
    assert raw.endswith(b"\n")
    decoded = avatar_ipc.decode_avatar_ipc_line(raw)
    assert decoded is not None
    assert decoded["v"] == avatar_ipc.AVATAR_IPC_SCHEMA_VERSION
    assert decoded["state"] == "speaking"

    # Legacy unversioned JSON object remains accepted during migration
    legacy = avatar_ipc.decode_avatar_ipc_line('{"state": "idle", "text": ""}')
    assert legacy == {"state": "idle", "text": ""}

    # Unsupported future/invalid protocol versions, non-dicts, malformed JSON, and oversized frames are rejected
    assert avatar_ipc.decode_avatar_ipc_line('{"v": 99, "state": "speaking"}') is None
    assert avatar_ipc.decode_avatar_ipc_line('{"v": 0, "state": "speaking"}') is None
    assert avatar_ipc.decode_avatar_ipc_line('{"v": true, "state": "speaking"}') is None
    assert avatar_ipc.decode_avatar_ipc_line('["not", "a", "dict"]') is None
    assert avatar_ipc.decode_avatar_ipc_line("{broken json") is None
    assert avatar_ipc.decode_avatar_ipc_line("x" * (avatar_ipc.AVATAR_IPC_MAX_LINE_BYTES + 10)) is None


def test_avatar_ipc_live_xdg_socket_permissions_and_version_enforcement(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CAPRI_DISABLE_LAYER_SHELL", "1")
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))
    monkeypatch.delenv("CAPRI_AVATAR_SOCKET", raising=False)
    monkeypatch.delenv("CAPRI_AVATAR_PID", raising=False)

    # Restore default sentinels so both server and client use dynamic $XDG_RUNTIME_DIR resolution
    monkeypatch.setattr(ui_client, "SOCKET_PATH", ui_client._DEFAULT_SOCKET_SENTINEL)
    monkeypatch.setattr(ui_overlay, "SOCKET_PATH", ui_overlay._DEFAULT_SOCKET_SENTINEL)
    monkeypatch.setattr(ui_overlay, "PID_PATH", ui_overlay._DEFAULT_PID_SENTINEL)
    monkeypatch.setattr(avatar_ipc, "LEGACY_AVATAR_SOCKET_PATH", str(tmp_path / "unused_legacy.sock"))

    app = QApplication.instance() or QApplication([])
    widget = ui_overlay.CapriAvatarWidget(mode="2d")
    try:
        expected_dir = run_dir / "capri"
        expected_sock = expected_dir / "avatar.sock"
        assert expected_dir.is_dir()
        assert expected_sock.exists()
        assert stat.S_IMODE(os.lstat(expected_dir).st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(expected_sock).st_mode) == 0o600

        # 1. Versioned v1 client call delivers cleanly
        assert ui_client.notify_avatar("speaking", "Связь по XDG v1") is True
        for _ in range(30):
            app.processEvents()
            if widget.state == "speaking":
                break
            time.sleep(0.01)
        assert widget.state == "speaking"
        assert "XDG v1" in widget.caption_text

        # 2. Unsupported protocol version (v: 99) is ignored without changing state
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(expected_sock))
            s.sendall((json.dumps({"v": 99, "state": "thinking", "text": "bad"}) + "\n").encode("utf-8"))
        for _ in range(10):
            app.processEvents()
            time.sleep(0.01)
        assert widget.state == "speaking"

        # 3. Legacy unversioned client payload (no "v") still works during migration
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(expected_sock))
            s.sendall((json.dumps({"state": "listening", "text": "legacy ok"}) + "\n").encode("utf-8"))
        for _ in range(30):
            app.processEvents()
            if widget.state == "listening":
                break
            time.sleep(0.01)
        assert widget.state == "listening"
    finally:
        widget.anim_timer.stop()
        widget.close()
        app.processEvents()
        assert not (run_dir / "capri" / "avatar.sock").exists()


def test_avatar_ipc_client_falls_back_to_legacy_socket_during_transition(monkeypatch, tmp_path):
    run_dir = tmp_path / "empty_run"
    run_dir.mkdir(mode=0o700)
    legacy_sock = tmp_path / "legacy_avatar.sock"

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))
    monkeypatch.delenv("CAPRI_AVATAR_SOCKET", raising=False)
    monkeypatch.setattr(ui_client, "SOCKET_PATH", ui_client._DEFAULT_SOCKET_SENTINEL)
    monkeypatch.setattr(avatar_ipc, "LEGACY_AVATAR_SOCKET_PATH", str(legacy_sock))

    received: list[dict] = []
    ready = threading.Event()

    def _legacy_server():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(str(legacy_sock))
            srv.listen(1)
            srv.settimeout(2.0)
            ready.set()
            conn, _ = srv.accept()
            with conn:
                raw = conn.recv(4096)
                for line in raw.decode("utf-8").splitlines():
                    if line.strip():
                        received.append(json.loads(line))

    t = threading.Thread(target=_legacy_server, daemon=True)
    t.start()
    assert ready.wait(timeout=2.0)

    # Primary `$XDG_RUNTIME_DIR/capri/avatar.sock` does not exist -> client transparently falls back to legacy
    assert ui_client.notify_avatar("playful", "legacy fallback") is True
    t.join(timeout=2.0)
    assert len(received) == 1
    assert received[0]["v"] == avatar_ipc.AVATAR_IPC_SCHEMA_VERSION
    assert received[0]["state"] == "playful"
    assert received[0]["text"] == "legacy fallback"


def test_avatar_ipc_client_rejects_symlink_and_foreign_uid_legacy_sockets(monkeypatch, tmp_path):
    run_dir = tmp_path / "empty_run2"
    run_dir.mkdir(mode=0o700)
    real_sock = tmp_path / "real_target.sock"
    symlink_legacy = tmp_path / "legacy_symlink.sock"
    regular_file = tmp_path / "not_a_socket.sock"
    regular_file.write_text("hi", encoding="utf-8")

    # Bind real listening socket, then point legacy symlink to it
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(real_sock))
    srv.listen(1)
    srv.settimeout(0.2)
    try:
        symlink_legacy.symlink_to(real_sock)
        assert avatar_ipc.is_owned_unix_socket(real_sock) is True
        assert avatar_ipc.is_owned_unix_socket(symlink_legacy) is False
        assert avatar_ipc.is_owned_unix_socket(regular_file) is False

        # Client must refuse symlinked legacy socket
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))
        monkeypatch.delenv("CAPRI_AVATAR_SOCKET", raising=False)
        monkeypatch.setattr(ui_client, "SOCKET_PATH", ui_client._DEFAULT_SOCKET_SENTINEL)
        monkeypatch.setattr(avatar_ipc, "LEGACY_AVATAR_SOCKET_PATH", str(symlink_legacy))
        assert ui_client.notify_avatar("speaking", "should not reach symlink") is False

        # Client must refuse a socket owned by a different UID
        real_lstat = os.lstat

        class _FakeStat:
            def __init__(self, orig):
                self.st_mode = orig.st_mode
                self.st_uid = (os.getuid() + 1) if hasattr(os, "getuid") else 99999

        monkeypatch.setattr(avatar_ipc, "LEGACY_AVATAR_SOCKET_PATH", str(real_sock))
        monkeypatch.setattr(
            avatar_ipc.os,
            "lstat",
            lambda p: _FakeStat(real_lstat(p)) if str(p) == str(real_sock) else real_lstat(p),
        )
        assert avatar_ipc.is_owned_unix_socket(real_sock) is False
        assert ui_client.notify_avatar("speaking", "should not reach foreign uid") is False
    finally:
        srv.close()


def test_avatar_ipc_stream_buffer_reassembles_fragmented_ndjson_and_drops_oversized(monkeypatch, tmp_path):
    # 1. Unit test stream framing across arbitrary chunk splits and oversized lines
    buf = avatar_ipc.AvatarIPCStreamBuffer(max_line_bytes=512)
    full_msg = b'{"v": 1, "state": "speaking", "text": "Split across 3 chunks"}\n'
    assert buf.feed(full_msg[:10]) == []
    assert buf.feed(full_msg[10:28]) == []
    out = buf.feed(full_msg[28:])
    assert len(out) == 1 and out[0]["text"] == "Split across 3 chunks"

    # Oversized frame split across two feeds is dropped, while following valid frame succeeds
    assert buf.feed(b'{"v": 1, "text": "' + b"A" * 400) == []
    after_big = buf.feed(b"B" * 300 + b'"}\n{"v": 1, "state": "idle", "text": "recovered"}\n')
    assert len(after_big) == 1 and after_big[0]["text"] == "recovered"

    # 2. Live server end-to-end test: one NDJSON message sent in 3 separate Unix stream writes
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CAPRI_DISABLE_LAYER_SHELL", "1")
    run_dir = tmp_path / "run_frag"
    run_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))
    monkeypatch.delenv("CAPRI_AVATAR_SOCKET", raising=False)
    monkeypatch.delenv("CAPRI_AVATAR_PID", raising=False)
    monkeypatch.setattr(ui_overlay, "SOCKET_PATH", ui_overlay._DEFAULT_SOCKET_SENTINEL)
    monkeypatch.setattr(ui_overlay, "PID_PATH", ui_overlay._DEFAULT_PID_SENTINEL)

    app = QApplication.instance() or QApplication([])
    widget = ui_overlay.CapriAvatarWidget(mode="2d")
    try:
        sock_path = run_dir / "capri" / "avatar.sock"
        payload = b'{"v": 1, "state": "speaking", "text": "Fragmented stream OK"}\n'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(str(sock_path))
            s.sendall(payload[:14])
            app.processEvents()
            time.sleep(0.02)
            s.sendall(payload[14:35])
            app.processEvents()
            time.sleep(0.02)
            s.sendall(payload[35:])
            for _ in range(40):
                app.processEvents()
                if widget.state == "speaking":
                    break
                time.sleep(0.01)
        assert widget.state == "speaking"
        assert "Fragmented stream OK" in widget.caption_text
    finally:
        widget.anim_timer.stop()
        widget.close()
        app.processEvents()


def test_read_owned_pid_file_and_kill_avatar_reject_symlinks_and_foreign_uid(monkeypatch, tmp_path):
    from capri import cli as capri_cli

    # 1. Valid owned regular PID file succeeds with 0600 mode
    good_pid = tmp_path / "avatar.pid"
    assert avatar_ipc.write_owned_pid_file(good_pid, 424242) is True
    assert stat.S_IMODE(os.lstat(good_pid).st_mode) == 0o600
    assert avatar_ipc.read_owned_pid_file(good_pid) == 424242

    # 2. Symlink PID file is rejected atomically via O_NOFOLLOW
    sym_pid = tmp_path / "legacy_sym.pid"
    sym_pid.symlink_to(good_pid)
    assert avatar_ipc.read_owned_pid_file(sym_pid) is None

    # 3. Foreign UID PID file is rejected via fstat
    real_fstat = os.fstat

    class _ForeignStat:
        def __init__(self, orig):
            self.st_mode = orig.st_mode
            self.st_size = orig.st_size
            self.st_uid = (os.getuid() + 1) if hasattr(os, "getuid") else 99999

    monkeypatch.setattr(avatar_ipc.os, "fstat", lambda fd: _ForeignStat(real_fstat(fd)))
    assert avatar_ipc.read_owned_pid_file(good_pid) is None
    monkeypatch.setattr(avatar_ipc.os, "fstat", real_fstat)

    # 4. cli.kill_avatar() must NOT send SIGTERM when legacy PID file is a symlink
    killed: list[tuple[int, int]] = []
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run_kill"))
    (tmp_path / "run_kill").mkdir(mode=0o700)
    monkeypatch.setattr(avatar_ipc, "LEGACY_AVATAR_PID_PATH", str(sym_pid))
    monkeypatch.setattr(avatar_ipc, "LEGACY_AVATAR_SOCKET_PATH", str(tmp_path / "no_legacy.sock"))
    monkeypatch.setattr(capri_cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    capri_cli.kill_avatar()
    assert killed == []
    assert sym_pid.is_symlink()  # symlink was neither read nor blindly unlinked


def test_avatar_singleton_flock_prevents_concurrent_launch_and_handles_pid_write_failure(monkeypatch, tmp_path):
    import subprocess
    import sys

    lock_path = tmp_path / "avatar.lock"
    lock1 = avatar_ipc.AvatarSingletonLock(lock_path)
    assert lock1.acquire() is True
    assert stat.S_IMODE(os.lstat(lock_path).st_mode) == 0o600

    try:
        # Cross-process kernel flock contention must immediately fail non-blockingly
        repo_root = str(Path(__file__).resolve().parent.parent)
        probe = (
            f"import sys; sys.path.insert(0, {repo_root!r}); "
            "from capri.ui.avatar_ipc import AvatarSingletonLock; "
            f"lk = AvatarSingletonLock({str(lock_path)!r}); "
            "print('ACQUIRED' if lk.acquire() else 'BUSY')"
        )
        res = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=10)
        assert res.returncode == 0
        assert res.stdout.strip() == "BUSY"
    finally:
        lock1.release()

    # Once released, another process acquires immediately
    res2 = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=10)
    assert res2.returncode == 0
    assert res2.stdout.strip() == "ACQUIRED"

    # Symlink at lock path is refused (O_NOFOLLOW)
    sym_lock = tmp_path / "sym.lock"
    sym_lock.symlink_to(lock_path)
    assert avatar_ipc.AvatarSingletonLock(sym_lock).acquire() is False

    # launch_avatar() must abort cleanly with exit code 1 if write_owned_pid_file fails
    run_dir = tmp_path / "run_launch"
    run_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(ui_overlay, "SOCKET_PATH", ui_overlay._DEFAULT_SOCKET_SENTINEL)
    monkeypatch.setattr(ui_overlay, "PID_PATH", ui_overlay._DEFAULT_PID_SENTINEL)
    monkeypatch.setattr(ui_overlay, "write_owned_pid_file", lambda p, pid: False)
    monkeypatch.setattr(sys, "argv", ["capri-avatar", "--mode", "2d"])
    with pytest.raises(SystemExit) as exc:
        ui_overlay.launch_avatar()
    assert exc.value.code == 1
    # Lock must be released cleanly after the failed PID write
    lk_after = avatar_ipc.AvatarSingletonLock(run_dir / "capri" / "avatar.lock")
    assert lk_after.acquire() is True
    lk_after.release()


def test_optional_rust_avatar_backend_ipc_roundtrip(monkeypatch, tmp_path):
    """If the experimental `capri-avatar-rs` binary is built, verify live Python->Rust IPC parity."""
    import subprocess

    rust_bin = ui_overlay._resolve_rust_avatar_binary()
    if rust_bin is None:
        pytest.skip("capri-avatar-rs binary not built in this environment")

    run_dir = tmp_path / "run_rs"
    run_dir.mkdir(mode=0o700)
    sock_path = run_dir / "capri" / "avatar.sock"
    pid_path = run_dir / "capri" / "avatar.pid"

    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = str(run_dir)
    env.pop("CAPRI_AVATAR_SOCKET", None)
    env.pop("CAPRI_AVATAR_PID", None)

    proc = subprocess.Popen(
        [str(rust_bin), "--headless", "--bench-first-frame"],
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

        assert ui_client.notify_avatar("speaking", "Привет от Python!", {"mood": "playful", "mouth_envelope": [0, 3, 2]}) is True
        assert ui_client.notify_avatar_audio_level(0.65, source="tts") is True
        assert ui_client.send_avatar_control({"type": "focus_timer", "action": "start", "minutes": 25}) is True
        assert ui_client.send_avatar_control({"type": "control", "command": "quit"}) is True

        rc = proc.wait(timeout=5.0)
        assert rc == 0
        assert not sock_path.exists(), "capri-avatar-rs must unlink avatar.sock on clean quit"
        assert not pid_path.exists(), "capri-avatar-rs must unlink avatar.pid on clean quit"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_avatar_backend_config_fallback_and_systemd_unit(monkeypatch, tmp_path, capsys):
    import sys
    from capri.core.config import UIConfig
    from capri.core.systemd_service import generate_avatar_service_unit

    assert UIConfig().avatar_backend == "python"
    assert UIConfig(avatar_backend="rust").avatar_backend == "rust"

    monkeypatch.delenv("CAPRI_AVATAR_BACKEND", raising=False)
    assert ui_overlay._resolve_requested_avatar_backend("rust") == "rust"
    assert ui_overlay._resolve_requested_avatar_backend("python") == "python"
    monkeypatch.setenv("CAPRI_AVATAR_BACKEND", "rust")
    assert ui_overlay._resolve_requested_avatar_backend(None) == "rust"
    monkeypatch.delenv("CAPRI_AVATAR_BACKEND", raising=False)

    # When `--backend rust` is requested without a built binary, launch_avatar falls back to Python
    # (reaches PID write check instead of crashing early with exit code 2).
    run_dir = tmp_path / "run_fallback"
    run_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(ui_overlay, "SOCKET_PATH", ui_overlay._DEFAULT_SOCKET_SENTINEL)
    monkeypatch.setattr(ui_overlay, "PID_PATH", ui_overlay._DEFAULT_PID_SENTINEL)
    monkeypatch.setattr(ui_overlay, "_resolve_rust_avatar_binary", lambda: None)
    monkeypatch.setattr(ui_overlay, "write_owned_pid_file", lambda p, pid: False)
    monkeypatch.setattr(sys, "argv", ["capri-avatar", "--mode", "2d", "--backend", "rust"])

    with pytest.raises(SystemExit) as exc:
        ui_overlay.launch_avatar()
    assert exc.value.code == 1
    assert "falling back to python" in capsys.readouterr().err

    unit_py = generate_avatar_service_unit(mode="2d", backend="python", exec_cmd=["/usr/bin/capri-avatar"])
    assert "--backend" not in unit_py
    unit_rs = generate_avatar_service_unit(mode="2d", backend="rust", exec_cmd=["/usr/bin/capri-avatar"])
    assert "--mode 2d --backend rust --systemd" in unit_rs
