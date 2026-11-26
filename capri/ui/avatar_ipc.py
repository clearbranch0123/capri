"""
Versioned, per-user Unix-socket IPC contract for `capri-avatar`.

Security & compatibility guarantees:
- Primary runtime directory: `$XDG_RUNTIME_DIR/capri` (mode `0700`), falling back to
  `/tmp/capri-<uid>` (`0700`) when `XDG_RUNTIME_DIR` is unavailable.
- Socket file (`avatar.sock`) and PID file (`avatar.pid`) are restricted to owner-only (`0600`).
- Rejects symlinked runtime directories or directories owned by another UID.
- Wire format: newline-delimited JSON objects (NDJSON, max 64 KiB/line) carrying `"v": 1`.
  - Server accepts `v == 1` and legacy unversioned payloads (no `"v"` key) during migration.
  - Server rejects malformed payloads, oversized lines, and unknown/future protocol versions.
- Client tries the primary per-user socket first, then falls back to legacy `/tmp/capri_avatar.sock`
  during transition so an already-running older avatar process remains reachable.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Optional

AVATAR_IPC_SCHEMA_VERSION = 1
AVATAR_IPC_MAX_LINE_BYTES = 65536
LEGACY_AVATAR_SOCKET_PATH = "/tmp/capri_avatar.sock"
LEGACY_AVATAR_PID_PATH = "/tmp/capri_avatar.pid"


def _current_uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def get_avatar_runtime_dir() -> Path:
    """Return the per-user runtime directory for Capri avatar IPC sockets and PID files."""
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg and os.path.isdir(xdg):
        return Path(xdg) / "capri"
    return Path(f"/tmp/capri-{_current_uid()}")


def ensure_avatar_runtime_dir(target_dir: Optional[Path] = None) -> Path:
    """
    Create and validate the per-user runtime directory with strict `0700` permissions.
    Refuses symlinked directories or directories owned by another user.
    """
    target = Path(target_dir) if target_dir is not None else get_avatar_runtime_dir()
    if target.is_symlink():
        raise PermissionError(f"Refusing symlinked IPC runtime directory: {target}")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(target)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PermissionError(f"Invalid IPC runtime directory path: {target}")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise PermissionError(f"IPC runtime directory owned by uid {st.st_uid}, expected {os.getuid()}: {target}")
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass
    return target


def get_avatar_socket_path() -> str:
    """Return the active per-user Unix socket path for `capri-avatar`."""
    override = os.environ.get("CAPRI_AVATAR_SOCKET", "").strip()
    if override:
        return override
    return str(get_avatar_runtime_dir() / "avatar.sock")


def get_avatar_pid_path() -> str:
    """Return the active per-user PID file path for `capri-avatar`."""
    override = os.environ.get("CAPRI_AVATAR_PID", "").strip()
    if override:
        return override
    return str(get_avatar_runtime_dir() / "avatar.pid")


def get_avatar_lock_path(pid_path: Optional[str | Path] = None) -> str:
    """Return the per-user singleton lock file path alongside the avatar PID file."""
    base = Path(pid_path).parent if pid_path is not None else get_avatar_runtime_dir()
    return str(base / "avatar.lock")


class AvatarSingletonLock:
    """
    Non-blocking POSIX `flock` singleton lock for `capri-avatar`.

    Prevents concurrent-launch races between startup checks, PID-file writes, and
    IPC socket binding. The kernel automatically releases the lock if the holder crashes.
    The lock file itself is intentionally never unlinked so contenders always lock the same inode.
    """

    def __init__(self, lock_path: str | Path):
        self.lock_path = Path(lock_path)
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        import fcntl

        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd: Optional[int] = None
        try:
            fd = os.open(str(self.lock_path), flags, 0o600)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                os.close(fd)
                return False
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                os.close(fd)
                return False
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd = fd
            return True
        except OSError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return False

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def get_avatar_socket_candidates(*, include_legacy: bool = True) -> list[str]:
    """Return ordered socket paths to probe: primary per-user path first, then legacy fallback."""
    primary = get_avatar_socket_path()
    candidates = [primary]
    if include_legacy and LEGACY_AVATAR_SOCKET_PATH not in candidates:
        candidates.append(LEGACY_AVATAR_SOCKET_PATH)
    return candidates


def enforce_owner_only_file_mode(path: str | Path, mode: int = 0o600) -> None:
    """Enforce owner-only permissions (`0600`) on a local socket or PID file."""
    p = Path(path)
    try:
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            return
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return
        os.chmod(p, mode)
    except OSError:
        pass


def is_owned_unix_socket(path: str | Path) -> bool:
    """
    Return True ONLY if `path` exists, is NOT a symlink, is a Unix domain socket,
    and is owned by the current user's UID (protects `/tmp` legacy fallback from hijacking).
    """
    p = Path(path)
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISSOCK(st.st_mode):
        return False
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return False
    return True


def peer_socket_matches_current_uid(sock: Any) -> bool:
    """Verify via Linux SO_PEERCRED that the connected Unix socket peer runs as the current UID."""
    if not hasattr(os, "getuid"):
        return True
    import socket as _socket
    import struct as _struct

    so_peercred = getattr(_socket, "SO_PEERCRED", None)
    if so_peercred is None:
        return True
    try:
        creds = sock.getsockopt(_socket.SOL_SOCKET, so_peercred, _struct.calcsize("3i"))
        _pid, peer_uid, _gid = _struct.unpack("3i", creds)
        return peer_uid == os.getuid()
    except OSError:
        return False


def read_owned_pid_file(path: str | Path) -> Optional[int]:
    """
    Safely read a positive integer PID from `path` without following symlinks.
    Opens with `O_NOFOLLOW` and validates via `fstat` that the descriptor refers
    to a small regular file owned by the current UID.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd: Optional[int] = None
    try:
        fd = os.open(str(path), flags)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return None
        if st.st_size <= 0 or st.st_size > 64:
            return None
        raw = os.read(fd, 64).decode("ascii", errors="ignore").strip()
        pid = int(raw)
        return pid if pid > 1 else None
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def write_owned_pid_file(path: str | Path, pid: int) -> bool:
    """Atomically write `pid` to a `0600` regular file without following symlinks."""
    safe_unlink_owned_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd: Optional[int] = None
    try:
        fd = os.open(str(path), flags, 0o600)
        os.write(fd, f"{int(pid)}\n".encode("ascii"))
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def safe_unlink_owned_path(path: str | Path) -> bool:
    """Unlink a socket/PID file only if it exists, is not a symlink, and is owned by current UID."""
    p = Path(path)
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return False
    try:
        os.unlink(p)
        return True
    except OSError:
        return False


def encode_avatar_ipc_message(payload: dict[str, Any]) -> bytes:
    """Encode a versioned (`v: 1`) NDJSON frame for `capri-avatar` IPC."""
    if not isinstance(payload, dict):
        raise TypeError("Avatar IPC payload must be a dict")
    envelope = dict(payload)
    if "v" not in envelope:
        envelope["v"] = AVATAR_IPC_SCHEMA_VERSION
    raw = (json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8")
    if len(raw) > AVATAR_IPC_MAX_LINE_BYTES:
        raise ValueError(f"Avatar IPC payload exceeds max line size ({len(raw)} > {AVATAR_IPC_MAX_LINE_BYTES} bytes)")
    return raw


def decode_avatar_ipc_line(raw_line: str | bytes) -> Optional[dict[str, Any]]:
    """
    Validate and decode one NDJSON line from the `capri-avatar` Unix socket.
    Accepts schema `v == 1` and legacy unversioned dicts (no `"v"` key); rejects
    oversized lines, malformed JSON, non-dict payloads, and unsupported protocol versions.
    """
    if isinstance(raw_line, bytes):
        if len(raw_line) > AVATAR_IPC_MAX_LINE_BYTES:
            return None
        text = raw_line.decode("utf-8", errors="ignore").strip()
    else:
        if len(raw_line.encode("utf-8", errors="ignore")) > AVATAR_IPC_MAX_LINE_BYTES:
            return None
        text = raw_line.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if "v" in data:
        ver = data.get("v")
        if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1 or ver > AVATAR_IPC_SCHEMA_VERSION:
            return None
    return data


class AvatarIPCStreamBuffer:
    """
    Per-connection NDJSON framing buffer for Unix stream sockets.

    Reassembles partial chunks across multiple `readyRead` callbacks up to `\n`,
    enforces `AVATAR_IPC_MAX_LINE_BYTES` per line, and drops oversized frames cleanly
    until the next newline delimiter so subsequent frames remain intact.
    """

    def __init__(self, max_line_bytes: int = AVATAR_IPC_MAX_LINE_BYTES):
        self.max_line_bytes = max(256, int(max_line_bytes))
        self._buf = bytearray()
        self._discarding_oversized = False

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        if not chunk:
            return []
        messages: list[dict[str, Any]] = []
        view = memoryview(chunk)
        pos = 0
        total = len(view)

        while pos < total:
            nl_idx = chunk.find(b"\n", pos)
            if self._discarding_oversized:
                if nl_idx == -1:
                    return messages
                self._discarding_oversized = False
                pos = nl_idx + 1
                continue

            if nl_idx == -1:
                rem = total - pos
                if len(self._buf) + rem > self.max_line_bytes:
                    self._buf.clear()
                    self._discarding_oversized = True
                    return messages
                self._buf.extend(view[pos:total])
                break

            seg_len = nl_idx - pos
            if len(self._buf) + seg_len > self.max_line_bytes:
                self._buf.clear()
                pos = nl_idx + 1
                continue

            if self._buf:
                self._buf.extend(view[pos:nl_idx])
                raw_line = bytes(self._buf)
                self._buf.clear()
            else:
                raw_line = bytes(view[pos:nl_idx])
            pos = nl_idx + 1

            decoded = decode_avatar_ipc_line(raw_line)
            if decoded is not None:
                messages.append(decoded)

        return messages

    def flush_eof(self) -> list[dict[str, Any]]:
        """Flush a trailing unterminated frame when a peer closes the stream cleanly."""
        if self._discarding_oversized or not self._buf:
            self._buf.clear()
            self._discarding_oversized = False
            return []
        raw_line = bytes(self._buf)
        self._buf.clear()
        decoded = decode_avatar_ipc_line(raw_line)
        return [decoded] if decoded is not None else []
