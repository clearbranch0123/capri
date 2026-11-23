"""
Capri v3.3 — Pure-Python Wayland `zwlr_layer_shell_v1` Transparent Click-Through Overlay Stage.

Architecture:
- Speaks native Wayland wire protocol directly over `$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY`
  using ONLY Python stdlib (`socket`, `struct`, `mmap`, `os.memfd_create`) + PySide6 `QImage`/`QPainter`.
- Zero C++ ABI dependencies (no `layer-shell-qt`, no `gtk-layer-shell`, no `pywayland`).
- Works natively on Niri, Hyprland, Sway, River, and any `wlr-layer-shell` compositor.
- Renders Capri at 60 FPS directly into double-buffered `wl_shm` ARGB8888 buffers using
  minimal dirty-rect updates (0 compositor IPC window-move commands during drag/physics!).
- Uses `wl_surface.set_input_region` so 100% of the screen outside Capri's 300x420 sprite box
  is click-through to applications underneath without stealing focus.
"""

from __future__ import annotations

import array
import mmap
import os
import select
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRect, QSocketNotifier, Signal
from PySide6.QtGui import QColor, QImage, QPainter

# Linux input-event-codes.h constants
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
KEY_LEFTSHIFT = 42
KEY_RIGHTSHIFT = 54

# Wayland core opcodes
_WL_DISPLAY_ID = 1
_WL_DISPLAY_SYNC = 0
_WL_DISPLAY_GET_REGISTRY = 1
_WL_REGISTRY_BIND = 0
_WL_COMPOSITOR_CREATE_SURFACE = 0
_WL_COMPOSITOR_CREATE_REGION = 1
_WL_SHM_CREATE_POOL = 0
_WL_SHM_POOL_CREATE_BUFFER = 0
_WL_SHM_POOL_DESTROY = 1
_WL_BUFFER_DESTROY = 0
_WL_SURFACE_DESTROY = 0
_WL_SURFACE_ATTACH = 1
_WL_SURFACE_DAMAGE = 2
_WL_SURFACE_SET_INPUT_REGION = 5
_WL_SURFACE_COMMIT = 6
_WL_SURFACE_DAMAGE_BUFFER = 9
_WL_REGION_DESTROY = 0
_WL_REGION_ADD = 1
_WL_SEAT_GET_POINTER = 0
_WL_SEAT_GET_KEYBOARD = 1
_WL_POINTER_RELEASE = 1
_WL_KEYBOARD_RELEASE = 0
_WL_OUTPUT_RELEASE = 0

# zwlr_layer_shell_v1 / zwlr_layer_surface_v1 opcodes & enums
_LAYER_SHELL_GET_LAYER_SURFACE = 0
_LAYER_SURFACE_SET_SIZE = 0
_LAYER_SURFACE_SET_ANCHOR = 1
_LAYER_SURFACE_SET_EXCLUSIVE_ZONE = 2
_LAYER_SURFACE_SET_MARGIN = 3
_LAYER_SURFACE_SET_KEYBOARD_INTERACTIVITY = 4
_LAYER_SURFACE_ACK_CONFIGURE = 6
_LAYER_SURFACE_DESTROY = 7

LAYER_BACKGROUND = 0
LAYER_BOTTOM = 1
LAYER_TOP = 2
LAYER_OVERLAY = 3

ANCHOR_TOP = 1
ANCHOR_BOTTOM = 2
ANCHOR_LEFT = 4
ANCHOR_RIGHT = 8
ANCHOR_ALL = ANCHOR_TOP | ANCHOR_BOTTOM | ANCHOR_LEFT | ANCHOR_RIGHT

KB_INTERACTIVITY_NONE = 0
KB_INTERACTIVITY_EXCLUSIVE = 1
KB_INTERACTIVITY_ON_DEMAND = 2

WL_SHM_FORMAT_ARGB8888 = 0


def pack_wl_string(text: str) -> bytes:
    """Encode a Wayland protocol NUL-terminated string padded to 4-byte alignment."""
    raw = text.encode("utf-8") + b"\x00"
    pad = (-len(raw)) & 3
    return struct.pack("<I", len(raw)) + raw + (b"\x00" * pad)


def pack_wl_message(obj_id: int, opcode: int, payload: bytes = b"") -> bytes:
    """Pack a Wayland wire message: header (obj_id, (size << 16) | opcode) + payload."""
    size = 8 + len(payload)
    return struct.pack("<II", obj_id, (size << 16) | (opcode & 0xFFFF)) + payload


def unpack_wl_fixed(raw_int: int) -> float:
    """Convert Wayland 24.8 signed fixed-point integer to float logical pixels."""
    return float(raw_int) / 256.0


def get_wayland_socket_path() -> Optional[str]:
    """Return the active Wayland display Unix socket path, or None if unavailable."""
    wd = os.environ.get("WAYLAND_DISPLAY", "").strip()
    if not wd:
        return None
    if os.path.isabs(wd):
        return wd if os.path.exists(wd) else None
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if not xdg:
        return None
    path = os.path.join(xdg, wd)
    return path if os.path.exists(path) else None


def probe_layer_shell_support(timeout: float = 0.5) -> bool:
    """Fast read-only Wayland registry probe verifying `zwlr_layer_shell_v1` is advertised."""
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        return False
    sock_path = get_wayland_socket_path()
    if not sock_path:
        return False
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(sock_path)
        sock.sendall(
            pack_wl_message(_WL_DISPLAY_ID, _WL_DISPLAY_GET_REGISTRY, struct.pack("<I", 2))
            + pack_wl_message(_WL_DISPLAY_ID, _WL_DISPLAY_SYNC, struct.pack("<I", 3))
        )
        buf = b""
        needed = {"wl_compositor", "wl_shm", "wl_seat", "zwlr_layer_shell_v1"}
        found: set[str] = set()
        done = False
        while not done:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 8:
                obj, size_op = struct.unpack_from("<II", buf, 0)
                size = size_op >> 16
                op = size_op & 0xFFFF
                if size < 8 or len(buf) < size:
                    break
                payload = buf[8:size]
                buf = buf[size:]
                if obj == 2 and op == 0 and len(payload) >= 8:
                    _, str_len = struct.unpack_from("<II", payload, 0)
                    if str_len > 1 and 8 + str_len <= len(payload):
                        iface = payload[8 : 8 + str_len - 1].decode("utf-8", errors="replace")
                        if iface in needed:
                            found.add(iface)
                elif obj == 3 and op == 0:
                    done = True
                    break
        return needed.issubset(found)
    except Exception:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


@dataclass
class _WaylandOutput:
    global_name: int
    obj_id: int
    version: int
    name: str = ""
    x: int = 0
    y: int = 0


@dataclass
class _ShmBuffer:
    buffer_id: int
    pool_id: int
    fd: int
    mm: mmap.mmap
    width: int
    height: int
    stride: int
    size: int
    busy: bool = False
    prev_rect: Optional[QRect] = None


class WaylandLayerStage(QObject):
    """
    Full-workarea transparent `zwlr_layer_shell_v1` overlay stage integrated into Qt's event loop.
    Handles direct 165 Hz pointer events, dynamic click-through `input_region`, Shift modifier
    tracking, and fast dirty-rect double-buffered `wl_shm` rendering.
    """

    configured = Signal(int, int)                    # (logical_width, logical_height)
    pointer_press = Signal(float, float, int, bool)  # (x, y, button, shift_held)
    pointer_move = Signal(float, float, float)       # (x, y, monotonic_ts)
    pointer_release = Signal(float, float, int, bool)  # (x, y, button, shift_held)
    outputs_changed = Signal(list)                   # list[str] of active wl_output connector names
    output_switched = Signal(str)                    # active wl_output connector name
    closed = Signal()

    def __init__(self, parent: Optional[QObject] = None, namespace: str = "capri-avatar"):
        super().__init__(parent)
        self.namespace = namespace
        self._sock: Optional[socket.socket] = None
        self._notifier: Optional[QSocketNotifier] = None
        self._recv_buf = bytearray()
        self._next_id: int = 2
        self._active: bool = False

        # Wayland bound object IDs
        self._registry_id: int = 0
        self._globals: dict[str, tuple[int, int]] = {}
        self._outputs: dict[int, _WaylandOutput] = {}   # global_name -> _WaylandOutput
        self._output_by_oid: dict[int, int] = {}        # obj_id -> global_name
        self.active_output_name: str = ""
        self._active_output_global: int = 0
        self._compositor_id: int = 0
        self._compositor_ver: int = 1
        self._shm_id: int = 0
        self._seat_id: int = 0
        self._layer_shell_id: int = 0
        self._layer_shell_ver: int = 1
        self._surface_id: int = 0
        self._layer_surface_id: int = 0
        self._pending_switch_surface_id: int = 0
        self._pending_switch_layer_id: int = 0
        self._pending_switch_target_global: int = 0
        self._pending_switch_size: tuple[int, int] = (0, 0)
        self._pending_switch_configured: bool = False
        self._pending_switch_aborted: bool = False
        self._switching_output: bool = False
        self._deferred_hotplug_fallback: bool = False
        self._pointer_id: int = 0
        self._keyboard_id: int = 0

        # Surface & double-buffered shm state
        self.width: int = 0
        self.height: int = 0
        self._configured_once: bool = False
        self._buffers: list[_ShmBuffer] = []
        self._last_committed_rect: Optional[QRect] = None
        self._last_input_mode: str = ""
        self._last_input_rect: Optional[tuple[int, int, int, int]] = None
        self._kb_interactivity: int = KB_INTERACTIVITY_NONE
        # Deferred frame when all wl_shm buffers are still owned by the compositor
        self._pending_frame: Optional[tuple] = None
        self._pending_clear: bool = False

        # Live pointer & Shift modifier state
        self._pointer_x: float = 0.0
        self._pointer_y: float = 0.0
        self._pointer_inside: bool = False
        self._mods_depressed: int = 0
        self._shift_keys_down: set[int] = set()
        self._sync_callbacks: set[int] = set()

    @property
    def is_active(self) -> bool:
        return self._active and self._configured_once and self.width > 0 and self.height > 0

    @property
    def shift_held(self) -> bool:
        return bool((self._mods_depressed & 0x1) or self._shift_keys_down)

    def _alloc_id(self) -> int:
        oid = self._next_id
        self._next_id += 1
        return oid

    def _send(self, data: bytes) -> bool:
        if self._sock is None:
            return False
        try:
            self._sock.sendall(data)
            return True
        except Exception:
            self._handle_disconnect()
            return False

    def _send_with_fd(self, data: bytes, fd: int) -> bool:
        if self._sock is None:
            return False
        try:
            fds = array.array("i", [fd])
            self._sock.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])
            return True
        except Exception:
            self._handle_disconnect()
            return False

    def _bind_global(self, iface: str, max_ver: int) -> tuple[int, int]:
        info = self._globals.get(iface)
        if not info:
            return 0, 0
        name, avail_ver = info
        ver = min(avail_ver, max_ver)
        oid = self._alloc_id()
        payload = struct.pack("<I", name) + pack_wl_string(iface) + struct.pack("<II", ver, oid)
        self._send(pack_wl_message(_WL_REGISTRY_BIND if False else self._registry_id, _WL_REGISTRY_BIND, payload))
        return oid, ver

    def _bind_output(self, global_name: int, avail_ver: int) -> _WaylandOutput:
        if global_name in self._outputs:
            return self._outputs[global_name]
        ver = min(int(avail_ver), 4)
        oid = self._alloc_id()
        payload = struct.pack("<I", global_name) + pack_wl_string("wl_output") + struct.pack("<II", ver, oid)
        self._send(pack_wl_message(self._registry_id, _WL_REGISTRY_BIND, payload))
        out = _WaylandOutput(global_name=global_name, obj_id=oid, version=ver)
        self._outputs[global_name] = out
        self._output_by_oid[oid] = global_name
        return out

    def available_outputs(self) -> list[str]:
        """Return connector names of currently connected `wl_output` displays."""
        return [o.name for o in self._outputs.values() if o.name]

    def _find_output_by_name(self, output_name: str) -> Optional[_WaylandOutput]:
        target = (output_name or "").strip()
        if not target:
            return None
        for out in self._outputs.values():
            if out.name == target:
                return out
        return None

    def _create_layer_surface_pair(self, output_obj_id: int = 0) -> tuple[int, int]:
        """Allocate and send initial config for a (wl_surface, zwlr_layer_surface_v1) pair."""
        surf_id = self._alloc_id()
        ls_id = self._alloc_id()
        init_msgs = b"".join([
            pack_wl_message(self._compositor_id, _WL_COMPOSITOR_CREATE_SURFACE, struct.pack("<I", surf_id)),
            pack_wl_message(
                self._layer_shell_id,
                _LAYER_SHELL_GET_LAYER_SURFACE,
                struct.pack("<IIII", ls_id, surf_id, int(output_obj_id), LAYER_TOP)
                + pack_wl_string(self.namespace),
            ),
            pack_wl_message(ls_id, _LAYER_SURFACE_SET_SIZE, struct.pack("<II", 0, 0)),
            pack_wl_message(ls_id, _LAYER_SURFACE_SET_ANCHOR, struct.pack("<I", ANCHOR_ALL)),
            pack_wl_message(ls_id, _LAYER_SURFACE_SET_EXCLUSIVE_ZONE, struct.pack("<i", 0)),
            pack_wl_message(ls_id, _LAYER_SURFACE_SET_MARGIN, struct.pack("<iiii", 0, 0, 0, 0)),
            pack_wl_message(
                ls_id,
                _LAYER_SURFACE_SET_KEYBOARD_INTERACTIVITY,
                struct.pack("<I", KB_INTERACTIVITY_NONE),
            ),
            pack_wl_message(surf_id, _WL_SURFACE_COMMIT),
        ])
        if not self._send(init_msgs):
            return 0, 0
        return surf_id, ls_id

    def _create_layer_surface_on(self, output_obj_id: int = 0) -> bool:
        surf_id, ls_id = self._create_layer_surface_pair(output_obj_id)
        if not surf_id or not ls_id:
            return False
        self._surface_id = surf_id
        self._layer_surface_id = ls_id
        self._kb_interactivity = KB_INTERACTIVITY_NONE
        self._last_input_mode = ""
        self._last_input_rect = None
        self._last_committed_rect = None
        return True

    def _run_deferred_hotplug_fallback(self, failed_target: str = "") -> None:
        """If the active output disappeared during a switch transaction, migrate cleanly now."""
        if not self._deferred_hotplug_fallback:
            return
        self._deferred_hotplug_fallback = False
        if not self._active or self._sock is None or self._switching_output:
            return
        if self._active_output_global in self._outputs and self._layer_surface_id:
            return
        candidates = self.available_outputs()
        if not candidates:
            return
        ordered = [c for c in candidates if c != failed_target] + [
            c for c in candidates if c == failed_target
        ]
        for name in ordered:
            if self.switch_output(name):
                return

    def switch_output(self, output_name: str, timeout: float = 0.45) -> bool:
        """
        Atomically migrate the active `zwlr_layer_surface_v1` onto a different connected `wl_output`
        (by connector name, e.g. 'DP-1' / 'HDMI-A-1'). Keeps the previous surface alive until the
        candidate surface receives `configure`; if switching times out, destroys only the candidate
        and leaves the original stage intact and active. Non-reentrant: hotplug removals arriving
        mid-transaction are deferred until the active transaction finishes.
        """
        if not self._active or self._sock is None or self._switching_output:
            return False
        target = self._find_output_by_name(output_name)
        if target is None:
            return False
        if target.global_name == self._active_output_global and self._layer_surface_id:
            return True

        promoted = False
        self._switching_output = True
        try:
            # Create candidate surface while keeping the current active surface alive for safe rollback
            cand_surf, cand_ls = self._create_layer_surface_pair(target.obj_id)
            if cand_surf and cand_ls:
                self._pending_switch_surface_id = cand_surf
                self._pending_switch_layer_id = cand_ls
                self._pending_switch_target_global = target.global_name
                self._pending_switch_size = (0, 0)
                self._pending_switch_configured = False
                self._pending_switch_aborted = False

                was_blocking = self._sock.gettimeout()
                try:
                    self._sock.settimeout(timeout)
                    pumped = self._pump_until(
                        lambda: self._pending_switch_configured or self._pending_switch_aborted,
                        timeout=timeout,
                    )
                finally:
                    if self._sock is not None:
                        if was_blocking is None or was_blocking == 0.0:
                            self._sock.setblocking(False)
                        else:
                            self._sock.settimeout(was_blocking)

                ok = bool(
                    pumped
                    and self._pending_switch_configured
                    and not self._pending_switch_aborted
                    and (target.global_name in self._outputs)
                    and self._sock is not None
                )
                if not ok:
                    # Roll back cleanly: destroy ONLY the failed candidate; keep original surface & buffers active!
                    if self._sock is not None:
                        self._send(
                            pack_wl_message(cand_ls, _LAYER_SURFACE_DESTROY)
                            + pack_wl_message(cand_surf, _WL_SURFACE_DESTROY)
                        )
                else:
                    # Candidate configured cleanly -> atomically promote & tear down previous surface + old buffers
                    old_ls = self._layer_surface_id
                    old_surf = self._surface_id
                    new_w, new_h = self._pending_switch_size

                    self._surface_id = cand_surf
                    self._layer_surface_id = cand_ls
                    self._active_output_global = target.global_name
                    self.active_output_name = target.name
                    self._kb_interactivity = KB_INTERACTIVITY_NONE
                    self._last_input_mode = ""
                    self._last_input_rect = None
                    self._last_committed_rect = None
                    self._pending_frame = None
                    self._pending_clear = False

                    destroy_msgs = b""
                    if old_ls:
                        destroy_msgs += pack_wl_message(old_ls, _LAYER_SURFACE_DESTROY)
                    if old_surf:
                        destroy_msgs += pack_wl_message(old_surf, _WL_SURFACE_DESTROY)
                    if destroy_msgs:
                        self._send(destroy_msgs)

                    self.width = int(new_w)
                    self.height = int(new_h)
                    self._recreate_buffers(self.width, self.height)
                    self._configured_once = True
                    promoted = True
        finally:
            self._pending_switch_surface_id = 0
            self._pending_switch_layer_id = 0
            self._pending_switch_target_global = 0
            self._pending_switch_size = (0, 0)
            self._pending_switch_configured = False
            self._pending_switch_aborted = False
            self._switching_output = False

        if promoted:
            self.configured.emit(self.width, self.height)
            self.output_switched.emit(self.active_output_name)
        if self._deferred_hotplug_fallback:
            self._run_deferred_hotplug_fallback(failed_target="" if promoted else target.name)
        return promoted

    def start(self, timeout: float = 1.0, preferred_output: str = "") -> bool:
        """Connect to Wayland, discover `wl_output` displays, create `TOP` layer surface, and wait for configure."""
        sock_path = get_wayland_socket_path()
        if not sock_path:
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(sock_path)
            self._sock = sock
        except Exception:
            self.close()
            return False

        # 1. Discover globals via registry + roundtrip sync
        self._registry_id = self._alloc_id()
        sync_id = self._alloc_id()
        self._sync_callbacks.add(sync_id)
        if not self._send(
            pack_wl_message(_WL_DISPLAY_ID, _WL_DISPLAY_GET_REGISTRY, struct.pack("<I", self._registry_id))
            + pack_wl_message(_WL_DISPLAY_ID, _WL_DISPLAY_SYNC, struct.pack("<I", sync_id))
        ):
            self.close()
            return False

        if not self._pump_until(lambda: sync_id not in self._sync_callbacks, timeout=timeout):
            self.close()
            return False

        needed = ("wl_compositor", "wl_shm", "wl_seat", "zwlr_layer_shell_v1")
        if not all(k in self._globals for k in needed):
            self.close()
            return False

        # 2. Bind required Wayland globals (+ sync once if outputs were bound so wl_output.name arrives)
        self._compositor_id, self._compositor_ver = self._bind_global("wl_compositor", 4)
        self._shm_id, _ = self._bind_global("wl_shm", 1)
        self._seat_id, _ = self._bind_global("wl_seat", 5)
        self._layer_shell_id, self._layer_shell_ver = self._bind_global("zwlr_layer_shell_v1", 4)

        if self._outputs:
            out_sync = self._alloc_id()
            self._sync_callbacks.add(out_sync)
            self._send(pack_wl_message(_WL_DISPLAY_ID, _WL_DISPLAY_SYNC, struct.pack("<I", out_sync)))
            self._pump_until(lambda: out_sync not in self._sync_callbacks, timeout=min(0.4, timeout))

        target_oid = 0
        target_out = self._find_output_by_name(preferred_output) if preferred_output else None
        if target_out is not None:
            target_oid = target_out.obj_id
            self._active_output_global = target_out.global_name
            self.active_output_name = target_out.name
        elif len(self._outputs) == 1:
            only = next(iter(self._outputs.values()))
            self._active_output_global = only.global_name
            self.active_output_name = only.name

        # 3. Create wl_surface + zwlr_layer_surface_v1 anchored to all 4 edges (respects panels via zone=0).
        if not self._create_layer_surface_on(target_oid):
            self.close()
            return False

        # Wait for initial zwlr_layer_surface_v1.configure(serial, w, h)
        if not self._pump_until(lambda: self._configured_once, timeout=timeout):
            self.close()
            return False

        # Switch socket to non-blocking & hook into Qt's event loop via QSocketNotifier
        self._sock.setblocking(False)
        self._active = True
        self._notifier = QSocketNotifier(self._sock.fileno(), QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._on_socket_readable)
        return True

    def _pump_until(self, predicate: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not predicate():
            rem = deadline - time.monotonic()
            if rem <= 0 or self._sock is None:
                return False
            r, _, _ = select.select([self._sock], [], [], min(0.05, rem))
            if r:
                if not self._read_and_dispatch():
                    return False
        return True

    def _on_socket_readable(self, _fd: int = 0) -> None:
        if self._sock is None:
            return
        self._read_and_dispatch()

    def _read_and_dispatch(self) -> bool:
        if self._sock is None:
            return False
        try:
            data, ancdata, _, _ = self._sock.recvmsg(65536, socket.CMSG_SPACE(128))
        except BlockingIOError:
            return True
        except Exception:
            self._handle_disconnect()
            return False

        # Always close any compositor-sent file descriptors (e.g. wl_keyboard.keymap) to avoid leaks
        for cmsg_level, cmsg_type, cmsg_data in ancdata:
            if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
                fds = array.array("i")
                usable = len(cmsg_data) - (len(cmsg_data) % fds.itemsize)
                fds.frombytes(cmsg_data[:usable])
                for fd in fds:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

        if not data:
            self._handle_disconnect()
            return False

        self._recv_buf.extend(data)
        while len(self._recv_buf) >= 8:
            obj_id, size_op = struct.unpack_from("<II", self._recv_buf, 0)
            size = size_op >> 16
            op = size_op & 0xFFFF
            if size < 8:
                self._handle_disconnect()
                return False
            if len(self._recv_buf) < size:
                break
            payload = bytes(self._recv_buf[8:size])
            del self._recv_buf[:size]
            self._dispatch_event(obj_id, op, payload)
        return True

    def _dispatch_event(self, obj_id: int, op: int, payload: bytes) -> None:
        if obj_id == _WL_DISPLAY_ID:
            if op == 0:  # wl_display.error
                self._handle_disconnect()
            return

        if obj_id == self._registry_id:
            if op == 0 and len(payload) >= 8:  # wl_registry.global
                name, str_len = struct.unpack_from("<II", payload, 0)
                if str_len > 1 and 8 + str_len <= len(payload):
                    iface = payload[8 : 8 + str_len - 1].decode("utf-8", errors="replace")
                    padded = (str_len + 3) & ~3
                    if 8 + padded + 4 <= len(payload):
                        ver = struct.unpack_from("<I", payload, 8 + padded)[0]
                        self._globals[iface] = (name, ver)
                        if iface == "wl_output":
                            self._bind_output(name, ver)
            elif op == 1 and len(payload) >= 4:  # wl_registry.global_remove (hotplug disconnect!)
                removed_name = struct.unpack_from("<I", payload, 0)[0]
                out = self._outputs.pop(removed_name, None)
                if out is not None:
                    self._output_by_oid.pop(out.obj_id, None)
                    if out.version >= 3 and self._sock is not None:
                        self._send(pack_wl_message(out.obj_id, _WL_OUTPUT_RELEASE))
                    names = self.available_outputs()
                    if self._active:
                        self.outputs_changed.emit(names)
                        if self._switching_output:
                            # Defer hotplug migration until the in-flight switch_output() transaction finishes
                            if removed_name == self._pending_switch_target_global:
                                self._pending_switch_aborted = True
                            if removed_name == self._active_output_global:
                                self._deferred_hotplug_fallback = True
                        elif removed_name == self._active_output_global and names:
                            # Active output was unplugged while idle -> migrate cleanly to a remaining output!
                            self.switch_output(names[0])
            return

        if obj_id in self._output_by_oid:
            gname = self._output_by_oid[obj_id]
            out = self._outputs.get(gname)
            if out is not None:
                if op == 0 and len(payload) >= 8:  # wl_output.geometry(x, y, ...)
                    ox, oy = struct.unpack_from("<ii", payload, 0)
                    out.x = int(ox)
                    out.y = int(oy)
                elif op == 4 and len(payload) >= 4:  # wl_output.name(str) (wl_output v4)
                    str_len = struct.unpack_from("<I", payload, 0)[0]
                    if str_len > 1 and 4 + str_len <= len(payload):
                        out.name = payload[4 : 4 + str_len - 1].decode("utf-8", errors="replace")
                        if gname == self._active_output_global and not self.active_output_name:
                            self.active_output_name = out.name
                        if self._active:
                            self.outputs_changed.emit(self.available_outputs())
            return

        if obj_id in self._sync_callbacks and op == 0:  # wl_callback.done
            self._sync_callbacks.discard(obj_id)
            return

        if obj_id == self._seat_id and op == 0 and len(payload) >= 4:  # wl_seat.capabilities
            caps = struct.unpack_from("<I", payload, 0)[0]
            if (caps & 1) and not self._pointer_id:
                self._pointer_id = self._alloc_id()
                self._send(pack_wl_message(self._seat_id, _WL_SEAT_GET_POINTER, struct.pack("<I", self._pointer_id)))
            if (caps & 2) and not self._keyboard_id:
                self._keyboard_id = self._alloc_id()
                self._send(pack_wl_message(self._seat_id, _WL_SEAT_GET_KEYBOARD, struct.pack("<I", self._keyboard_id)))
            return

        if obj_id == self._pending_switch_layer_id and self._pending_switch_layer_id != 0:
            if op == 0 and len(payload) >= 12:  # zwlr_layer_surface_v1.configure on candidate surface
                serial, w, h = struct.unpack_from("<III", payload, 0)
                self._send(pack_wl_message(obj_id, _LAYER_SURFACE_ACK_CONFIGURE, struct.pack("<I", serial)))
                if w > 0 and h > 0:
                    self._pending_switch_size = (int(w), int(h))
                    self._pending_switch_configured = True
            elif op == 1:  # zwlr_layer_surface_v1.closed on candidate surface
                self._pending_switch_aborted = True
            return

        if obj_id == self._layer_surface_id:
            if op == 0 and len(payload) >= 12:  # zwlr_layer_surface_v1.configure
                serial, w, h = struct.unpack_from("<III", payload, 0)
                self._send(pack_wl_message(self._layer_surface_id, _LAYER_SURFACE_ACK_CONFIGURE, struct.pack("<I", serial)))
                if w > 0 and h > 0:
                    resized = (w != self.width or h != self.height)
                    self.width = int(w)
                    self.height = int(h)
                    if resized or not self._buffers:
                        self._recreate_buffers(self.width, self.height)
                    self._configured_once = True
                    self.configured.emit(self.width, self.height)
            elif op == 1:  # zwlr_layer_surface_v1.closed
                if self._switching_output:
                    self._layer_surface_id = 0
                    self._deferred_hotplug_fallback = True
                else:
                    self._handle_disconnect()
            return

        if obj_id == self._pointer_id:
            if op == 0 and len(payload) >= 16:  # wl_pointer.enter(serial, surface, sx, sy)
                _, _, fx, fy = struct.unpack_from("<IIii", payload, 0)
                self._pointer_inside = True
                self._pointer_x = unpack_wl_fixed(fx)
                self._pointer_y = unpack_wl_fixed(fy)
                # Enable ON_DEMAND only while pointer is over Capri so Shift+release works during drag,
                # then drop back to NONE immediately on leave/release so keyboard focus always returns!
                if self._layer_shell_ver >= 4:
                    self.set_keyboard_interactivity(KB_INTERACTIVITY_ON_DEMAND, commit=True)
            elif op == 1:  # wl_pointer.leave
                self._pointer_inside = False
                self.set_keyboard_interactivity(KB_INTERACTIVITY_NONE, commit=True)
            elif op == 2 and len(payload) >= 12:  # wl_pointer.motion(time, sx, sy)
                _, fx, fy = struct.unpack_from("<Iii", payload, 0)
                self._pointer_x = unpack_wl_fixed(fx)
                self._pointer_y = unpack_wl_fixed(fy)
                self.pointer_move.emit(self._pointer_x, self._pointer_y, time.monotonic())
            elif op == 3 and len(payload) >= 16:  # wl_pointer.button(serial, time, btn, state)
                _, _, btn, state = struct.unpack_from("<IIII", payload, 0)
                if state == 1:
                    self.pointer_press.emit(self._pointer_x, self._pointer_y, int(btn), self.shift_held)
                else:
                    sh = self.shift_held
                    # Immediately relinquish keyboard focus back to the user's active window/input field
                    self.set_keyboard_interactivity(KB_INTERACTIVITY_NONE, commit=True)
                    self.pointer_release.emit(self._pointer_x, self._pointer_y, int(btn), sh)
            return

        if obj_id == self._keyboard_id:
            if op == 3 and len(payload) >= 16:  # wl_keyboard.key(serial, time, key, state)
                _, _, key, state = struct.unpack_from("<IIII", payload, 0)
                if key in (KEY_LEFTSHIFT, KEY_RIGHTSHIFT):
                    if state == 1:
                        self._shift_keys_down.add(key)
                    else:
                        self._shift_keys_down.discard(key)
            elif op == 4 and len(payload) >= 20:  # wl_keyboard.modifiers
                _, depressed, _, _, _ = struct.unpack_from("<IIIII", payload, 0)
                self._mods_depressed = int(depressed)
            elif op == 2:  # wl_keyboard.leave
                self._mods_depressed = 0
                self._shift_keys_down.clear()
            return

        # wl_buffer.release (op == 0) — strictly required by Wayland spec before buffer reuse
        for buf in self._buffers:
            if obj_id == buf.buffer_id and op == 0:
                buf.busy = False
                self._flush_pending_frame()
                return

    # ── Shared-Memory Double Buffers ────────────────────────────────────────

    def _destroy_buffers(self) -> None:
        for buf in self._buffers:
            if self._sock is not None:
                self._send(
                    pack_wl_message(buf.buffer_id, _WL_BUFFER_DESTROY)
                    + pack_wl_message(buf.pool_id, _WL_SHM_POOL_DESTROY)
                )
            try:
                buf.mm.close()
            except Exception:
                pass
            try:
                os.close(buf.fd)
            except OSError:
                pass
        self._buffers.clear()

    def _recreate_buffers(self, width: int, height: int) -> None:
        self._destroy_buffers()
        stride = width * 4
        size = stride * height
        for idx in range(2):
            fd = os.memfd_create(f"capri_wl_shm_{idx}", os.MFD_CLOEXEC)
            os.ftruncate(fd, size)
            mm = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            pool_id = self._alloc_id()
            buf_id = self._alloc_id()
            self._send_with_fd(
                pack_wl_message(self._shm_id, _WL_SHM_CREATE_POOL, struct.pack("<Ii", pool_id, size)),
                fd,
            )
            self._send(
                pack_wl_message(
                    pool_id,
                    _WL_SHM_POOL_CREATE_BUFFER,
                    struct.pack("<IiiiiI", buf_id, 0, width, height, stride, WL_SHM_FORMAT_ARGB8888),
                )
            )
            self._buffers.append(
                _ShmBuffer(
                    buffer_id=buf_id,
                    pool_id=pool_id,
                    fd=fd,
                    mm=mm,
                    width=width,
                    height=height,
                    stride=stride,
                    size=size,
                )
            )

    # ── Dynamic Click-Through Input Region ──────────────────────────────────

    def _apply_input_region(self, rects: list[tuple[int, int, int, int]]) -> None:
        if not self._surface_id or not self._compositor_id:
            return
        reg_id = self._alloc_id()
        msgs = [pack_wl_message(self._compositor_id, _WL_COMPOSITOR_CREATE_REGION, struct.pack("<I", reg_id))]
        for rx, ry, rw, rh in rects:
            if rw > 0 and rh > 0:
                msgs.append(pack_wl_message(reg_id, _WL_REGION_ADD, struct.pack("<iiii", rx, ry, rw, rh)))
        msgs.append(pack_wl_message(self._surface_id, _WL_SURFACE_SET_INPUT_REGION, struct.pack("<I", reg_id)))
        msgs.append(pack_wl_message(reg_id, _WL_REGION_DESTROY))
        self._send(b"".join(msgs))

    def set_input_rect(self, x: int, y: int, w: int, h: int, *, force: bool = False) -> None:
        """Restrict clickable area strictly to Capri's bounding box; rest of screen is click-through."""
        rect = (int(x), int(y), max(0, int(w)), max(0, int(h)))
        if (
            not force
            and self._last_input_mode == "rect"
            and self._last_input_rect is not None
            and abs(rect[0] - self._last_input_rect[0]) < 2
            and abs(rect[1] - self._last_input_rect[1]) < 2
            and rect[2] == self._last_input_rect[2]
            and rect[3] == self._last_input_rect[3]
        ):
            return
        self._last_input_mode = "rect"
        self._last_input_rect = rect
        self._apply_input_region([rect])

    def set_full_input(self) -> None:
        """Temporarily cover entire stage during active LMB drag so fast flings never drop pointer."""
        if self._last_input_mode == "full":
            return
        self._last_input_mode = "full"
        self._last_input_rect = (0, 0, self.width, self.height)
        self._apply_input_region([(0, 0, self.width, self.height)])
        self._send(pack_wl_message(self._surface_id, _WL_SURFACE_COMMIT))

    def clear_input(self) -> None:
        """Make entire overlay 100% click-through (e.g. Game Mode / fullscreen app)."""
        if self._last_input_mode == "empty":
            return
        self._last_input_mode = "empty"
        self._last_input_rect = None
        self._apply_input_region([])
        self._send(pack_wl_message(self._surface_id, _WL_SURFACE_COMMIT))

    def set_keyboard_interactivity(self, mode: int, *, commit: bool = False) -> None:
        """Update `zwlr_layer_surface_v1` keyboard interactivity (defaults to NONE when idle)."""
        target = int(mode)
        if target == self._kb_interactivity or not self._layer_surface_id:
            return
        self._kb_interactivity = target
        msgs = pack_wl_message(
            self._layer_surface_id,
            _LAYER_SURFACE_SET_KEYBOARD_INTERACTIVITY,
            struct.pack("<I", target),
        )
        if commit and self._surface_id:
            msgs += pack_wl_message(self._surface_id, _WL_SURFACE_COMMIT)
        self._send(msgs)

    # ── Fast Dirty-Rect Frame Rendering (Strict wl_buffer.release Protocol) ─

    def _pick_free_buffer(self) -> Optional[_ShmBuffer]:
        """Return an idle buffer strictly NOT owned by the compositor (never reuses busy buffers)."""
        for buf in self._buffers:
            if not buf.busy:
                return buf
        return None

    def _flush_pending_frame(self) -> None:
        if not self.is_active:
            return
        if self._pending_clear:
            self._pending_clear = False
            self.clear_frame()
            return
        if self._pending_frame is not None:
            args = self._pending_frame
            self._pending_frame = None
            draw_fn, x, y, w, h, opacity, interactive, full_input, input_rect = args
            self.render_avatar(
                draw_fn,
                x=x,
                y=y,
                w=w,
                h=h,
                opacity=opacity,
                interactive=interactive,
                full_input=full_input,
                input_rect=input_rect,
            )

    def render_avatar(
        self,
        draw_fn: Callable[[QPainter, int, int], None],
        *,
        x: int,
        y: int,
        w: int,
        h: int,
        opacity: float = 1.0,
        interactive: bool = True,
        full_input: bool = False,
        input_rect: Optional[tuple[int, int, int, int]] = None,
    ) -> bool:
        """
        Paint Capri at `(x, y, w, h)` into an idle shared-memory Wayland buffer using minimal dirty rects.
        Strictly respects Wayland `wl_buffer.release`: if all buffers are currently busy, defers the
        latest frame until `wl_buffer.release` arrives instead of overwriting compositor-owned memory.
        """
        if not self.is_active:
            return False
        buf = self._pick_free_buffer()
        if buf is None:
            # Never touch a busy buffer — defer until wl_buffer.release arrives!
            self._pending_clear = False
            self._pending_frame = (draw_fn, x, y, w, h, opacity, interactive, full_input, input_rect)
            return False
        self._pending_frame = None
        self._pending_clear = False

        # Clamp target rect safely inside buffer bounds
        draw_x = max(0, min(self.width - w, int(round(x))))
        draw_y = max(0, min(self.height - h, int(round(y))))
        curr_rect = QRect(draw_x, draw_y, w, h)
        dirty = QRect(curr_rect)
        if buf.prev_rect is not None:
            dirty = dirty.united(buf.prev_rect)
        if self._last_committed_rect is not None:
            dirty = dirty.united(self._last_committed_rect)
        dirty = dirty.intersected(QRect(0, 0, buf.width, buf.height))

        img = QImage(buf.mm, buf.width, buf.height, buf.stride, QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(img)
        try:
            # Clear ONLY previous dirty region on this specific buffer (0.05ms instead of full-screen clear!)
            if buf.prev_rect is not None:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
                painter.fillRect(buf.prev_rect, QColor(0, 0, 0, 0))
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            if opacity > 0.005:
                painter.save()
                painter.setClipRect(curr_rect)
                painter.translate(float(draw_x), float(draw_y))
                painter.setOpacity(max(0.0, min(1.0, float(opacity))))
                draw_fn(painter, w, h)
                painter.restore()
        finally:
            painter.end()
            # Ensure QImage wrapper releases buffer view cleanly before any later mmap close
            del img

        buf.prev_rect = curr_rect
        self._last_committed_rect = curr_rect
        buf.busy = True

        if full_input:
            if self._last_input_mode != "full":
                self._last_input_mode = "full"
                self._last_input_rect = (0, 0, self.width, self.height)
                self._apply_input_region([(0, 0, self.width, self.height)])
        elif interactive:
            if input_rect is not None:
                hx, hy, hw, hh = input_rect
                hx = max(0, min(self.width - 1, int(hx)))
                hy = max(0, min(self.height - 1, int(hy)))
                hw = max(1, min(self.width - hx, int(hw)))
                hh = max(1, min(self.height - hy, int(hh)))
                self.set_input_rect(hx, hy, hw, hh)
            else:
                self.set_input_rect(draw_x, draw_y, w, h)
        else:
            if self._last_input_mode != "empty":
                self._last_input_mode = "empty"
                self._last_input_rect = None
                self._apply_input_region([])

        dmg_op = _WL_SURFACE_DAMAGE_BUFFER if self._compositor_ver >= 4 else _WL_SURFACE_DAMAGE
        msgs = b"".join([
            pack_wl_message(self._surface_id, _WL_SURFACE_ATTACH, struct.pack("<Iii", buf.buffer_id, 0, 0)),
            pack_wl_message(
                self._surface_id,
                dmg_op,
                struct.pack("<iiii", dirty.x(), dirty.y(), dirty.width(), dirty.height()),
            ),
            pack_wl_message(self._surface_id, _WL_SURFACE_COMMIT),
        ])
        return self._send(msgs)

    def clear_frame(self) -> None:
        """Hide Capri visually & make entire overlay click-through (defers pixel clear if all buffers busy)."""
        if not self.is_active:
            return
        self._last_input_mode = "empty"
        self._last_input_rect = None
        self._apply_input_region([])
        buf = self._pick_free_buffer()
        if buf is None:
            # Still commit empty input region immediately, and defer pixel clear until wl_buffer.release
            self._pending_frame = None
            self._pending_clear = True
            self._send(pack_wl_message(self._surface_id, _WL_SURFACE_COMMIT))
            return
        self._pending_clear = False
        self._pending_frame = None
        dirty = buf.prev_rect or self._last_committed_rect or QRect(0, 0, 1, 1)
        if buf.prev_rect is not None and self._last_committed_rect is not None:
            dirty = buf.prev_rect.united(self._last_committed_rect)
        if buf.prev_rect is not None:
            img = QImage(buf.mm, buf.width, buf.height, buf.stride, QImage.Format.Format_ARGB32_Premultiplied)
            p = QPainter(img)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(buf.prev_rect, QColor(0, 0, 0, 0))
            p.end()
            del img
            buf.prev_rect = None
        self._last_committed_rect = None
        buf.busy = True
        dmg_op = _WL_SURFACE_DAMAGE_BUFFER if self._compositor_ver >= 4 else _WL_SURFACE_DAMAGE
        self._send(
            b"".join([
                pack_wl_message(self._surface_id, _WL_SURFACE_ATTACH, struct.pack("<Iii", buf.buffer_id, 0, 0)),
                pack_wl_message(
                    self._surface_id,
                    dmg_op,
                    struct.pack("<iiii", dirty.x(), dirty.y(), dirty.width(), dirty.height()),
                ),
                pack_wl_message(self._surface_id, _WL_SURFACE_COMMIT),
            ])
        )

    def _handle_disconnect(self) -> None:
        was_active = self._active
        self.close()
        if was_active:
            self.closed.emit()

    def close(self) -> None:
        self._active = False
        if self._notifier is not None:
            try:
                self._notifier.setEnabled(False)
                self._notifier.deleteLater()
            except Exception:
                pass
            self._notifier = None
        self._destroy_buffers()
        if self._sock is not None:
            try:
                if self._pointer_id:
                    self._send(pack_wl_message(self._pointer_id, _WL_POINTER_RELEASE))
                if self._keyboard_id:
                    self._send(pack_wl_message(self._keyboard_id, _WL_KEYBOARD_RELEASE))
                if self._layer_surface_id:
                    self._send(pack_wl_message(self._layer_surface_id, _LAYER_SURFACE_DESTROY))
                if self._surface_id:
                    self._send(pack_wl_message(self._surface_id, _WL_SURFACE_DESTROY))
                for out in list(self._outputs.values()):
                    if out.version >= 3:
                        self._send(pack_wl_message(out.obj_id, _WL_OUTPUT_RELEASE))
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._outputs.clear()
        self._output_by_oid.clear()
        self._active_output_global = 0
        self.active_output_name = ""
        self._pointer_id = 0
        self._keyboard_id = 0
        self._layer_surface_id = 0
        self._surface_id = 0
        self._pending_switch_surface_id = 0
        self._pending_switch_layer_id = 0
        self._pending_switch_target_global = 0
        self._pending_switch_size = (0, 0)
        self._pending_switch_configured = False
        self._pending_switch_aborted = False
        self._switching_output = False
        self._deferred_hotplug_fallback = False
