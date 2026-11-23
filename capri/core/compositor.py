"""
CAPRI // Unified Wayland Compositor Backend (strictly Niri + Hyprland).
Provides a normalized window/workspace/monitor/action/event abstraction for:
- Niri (`niri msg -j ...`, `niri msg action ...`, `niri msg -j event-stream`)
- Hyprland (`hyprctl -j ...`, `hyprctl dispatch ...`, `$XDG_RUNTIME_DIR/hypr/.../.socket2.sock`)
- Graceful `"none"` fallback when neither compositor is active.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

CompositorKind = Literal["niri", "hyprland", "none"]


@dataclass(frozen=True)
class MonitorWorkArea:
    """Normalized logical-pixel monitor geometry with panel/bar reserved insets."""

    name: str
    x: float
    y: float
    width: float
    height: float
    scale: float = 1.0
    reserved_left: float = 0.0
    reserved_top: float = 0.0
    reserved_right: float = 0.0
    reserved_bottom: float = 0.0
    is_focused: bool = False

    @property
    def work_left(self) -> float:
        return self.x + self.reserved_left

    @property
    def work_top(self) -> float:
        return self.y + self.reserved_top

    @property
    def work_right(self) -> float:
        return max(self.work_left + 1.0, self.x + self.width - self.reserved_right)

    @property
    def work_bottom(self) -> float:
        return max(self.work_top + 1.0, self.y + self.height - self.reserved_bottom)

    @property
    def work_width(self) -> float:
        return self.work_right - self.work_left

    @property
    def work_height(self) -> float:
        return self.work_bottom - self.work_top

    def contains(self, px: float, py: float) -> bool:
        return (self.x <= px < self.x + self.width) and (self.y <= py < self.y + self.height)


def find_adjacent_monitor(
    current: MonitorWorkArea,
    side: int,
    y_ref: float,
    monitors: list[MonitorWorkArea],
    edge_tolerance: float = 64.0,
) -> Optional[MonitorWorkArea]:
    """
    Find the adjacent monitor touching `current` on `side` (`-1` = left, `+1` = right)
    whose vertical span actually contains global coordinate `y_ref`.
    Rejects vertically disjoint/staggered monitors so Capri never crosses dead empty space.
    """
    if side not in (-1, 1) or not monitors:
        return None
    target_edge = current.x if side < 0 else (current.x + current.width)
    best: Optional[tuple[float, MonitorWorkArea]] = None
    for m in monitors:
        if m.name == current.name:
            continue
        m_edge = (m.x + m.width) if side < 0 else m.x
        edge_gap = abs(m_edge - target_edge)
        if edge_gap > edge_tolerance:
            continue
        # Strictly require vertical overlap at y_ref so staggered monitors never bridge empty space
        if not (m.y <= y_ref <= (m.y + m.height)):
            continue
        if best is None or edge_gap < best[0]:
            best = (edge_gap, m)
    return best[1] if best is not None else None


def map_y_between_monitors(
    local_y: float,
    src: MonitorWorkArea,
    dst: MonitorWorkArea,
    avatar_h: float = 420.0,
) -> float:
    """
    Convert avatar stage-local Y on `src` into clamped stage-local Y on `dst`,
    preserving global vertical alignment across monitors of differing height or Y offset.
    """
    global_y = src.work_top + float(local_y)
    dst_local = global_y - dst.work_top
    max_y = max(0.0, float(dst.work_height) - float(avatar_h))
    return max(0.0, min(max_y, dst_local))


def _valid_niri_win_id(win_id: Any) -> Optional[int]:
    """Validate an explicit Niri numeric window ID (rejects None/bool/zero/negative)."""
    if win_id is None or isinstance(win_id, bool):
        return None
    try:
        nid = int(win_id)
    except (TypeError, ValueError):
        return None
    return nid if nid > 0 else None


def _valid_hyprland_address(win_id: Any) -> Optional[str]:
    """Validate an explicit Hyprland window address (e.g. '0x55abc123')."""
    if win_id is None or isinstance(win_id, bool):
        return None
    addr = str(win_id).strip()
    if not addr or any(ch in addr for ch in (" ", ",", "\n", "\r", "\t", ";")):
        return None
    return addr


def normalize_niri_outputs(
    raw: Any,
    focused_name: Optional[str] = None,
) -> list[MonitorWorkArea]:
    """
    Normalize Niri `Outputs` IPC JSON into logical-pixel `MonitorWorkArea` entries.
    Skips disconnected outputs where `logical` is null/None. Preserves negative coordinates & DPI scale.
    """
    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                entries.append((str(v.get("name") or k), v))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                entries.append((str(item.get("name") or "output"), item))

    monitors: list[MonitorWorkArea] = []
    for name, info in entries:
        logical = info.get("logical")
        if not isinstance(logical, dict):
            continue
        try:
            w = float(logical.get("width", 0))
            h = float(logical.get("height", 0))
            if w <= 0.0 or h <= 0.0:
                continue
            x = float(logical.get("x", 0.0))
            y = float(logical.get("y", 0.0))
            scale = float(logical.get("scale", 1.0) or 1.0)
            if scale <= 0.0:
                scale = 1.0
        except (TypeError, ValueError):
            continue

        reserved = info.get("reserved")
        r_l = r_t = r_r = r_b = 0.0
        if isinstance(reserved, list) and len(reserved) >= 4:
            try:
                r_l, r_t, r_r, r_b = (max(0.0, float(v)) for v in reserved[:4])
            except (TypeError, ValueError):
                r_l = r_t = r_r = r_b = 0.0

        is_foc = bool(focused_name and name == focused_name)
        monitors.append(
            MonitorWorkArea(
                name=name,
                x=x,
                y=y,
                width=w,
                height=h,
                scale=scale,
                reserved_left=r_l,
                reserved_top=r_t,
                reserved_right=r_r,
                reserved_bottom=r_b,
                is_focused=is_foc,
            )
        )

    if monitors and not any(m.is_focused for m in monitors):
        first = monitors[0]
        monitors[0] = MonitorWorkArea(
            name=first.name,
            x=first.x,
            y=first.y,
            width=first.width,
            height=first.height,
            scale=first.scale,
            reserved_left=first.reserved_left,
            reserved_top=first.reserved_top,
            reserved_right=first.reserved_right,
            reserved_bottom=first.reserved_bottom,
            is_focused=True,
        )
    return monitors


def normalize_hyprland_monitors(raw: Any) -> list[MonitorWorkArea]:
    """
    Normalize Hyprland `j/monitors` JSON into logical-pixel `MonitorWorkArea` entries.
    Applies DPI `scale`, odd `transform` rotation (90/270 deg), `reserved` bar insets,
    and preserves negative multi-monitor coordinates.
    """
    if not isinstance(raw, list):
        return []

    monitors: list[MonitorWorkArea] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("disabled") is True:
            continue
        try:
            phys_w = float(item.get("width", 0))
            phys_h = float(item.get("height", 0))
            if phys_w <= 0.0 or phys_h <= 0.0:
                continue
            scale = float(item.get("scale", 1.0) or 1.0)
            if scale <= 0.0:
                scale = 1.0
            transform = int(item.get("transform", 0) or 0)
            log_w = phys_w / scale
            log_h = phys_h / scale
            if transform % 2 == 1:
                log_w, log_h = log_h, log_w
            x = float(item.get("x", 0.0))
            y = float(item.get("y", 0.0))
        except (TypeError, ValueError):
            continue

        reserved = item.get("reserved")
        r_l = r_t = r_r = r_b = 0.0
        if isinstance(reserved, list) and len(reserved) >= 4:
            try:
                r_l, r_t, r_r, r_b = (max(0.0, float(v)) for v in reserved[:4])
            except (TypeError, ValueError):
                r_l = r_t = r_r = r_b = 0.0

        monitors.append(
            MonitorWorkArea(
                name=str(item.get("name") or "monitor"),
                x=x,
                y=y,
                width=log_w,
                height=log_h,
                scale=scale,
                reserved_left=r_l,
                reserved_top=r_t,
                reserved_right=r_r,
                reserved_bottom=r_b,
                is_focused=bool(item.get("focused", False)),
            )
        )

    if monitors and not any(m.is_focused for m in monitors):
        first = monitors[0]
        monitors[0] = MonitorWorkArea(
            name=first.name,
            x=first.x,
            y=first.y,
            width=first.width,
            height=first.height,
            scale=first.scale,
            reserved_left=first.reserved_left,
            reserved_top=first.reserved_top,
            reserved_right=first.reserved_right,
            reserved_bottom=first.reserved_bottom,
            is_focused=True,
        )
    return monitors


def resolve_monitor_for_point(
    monitors: list[MonitorWorkArea],
    px: float,
    py: float,
) -> MonitorWorkArea:
    """Find the monitor containing logical point `(px, py)`, or the nearest monitor (fallback 1920x1080)."""
    if not monitors:
        return MonitorWorkArea(name="default", x=0.0, y=0.0, width=1920.0, height=1080.0, scale=1.0, is_focused=True)
    for m in monitors:
        if m.contains(px, py):
            return m

    def _dist_sq(m: MonitorWorkArea) -> float:
        cx = max(m.x, min(px, m.x + m.width))
        cy = max(m.y, min(py, m.y + m.height))
        dx = px - cx
        dy = py - cy
        return dx * dx + dy * dy

    return min(monitors, key=_dist_sq)


def get_hyprland_command_socket_path() -> Optional[Path]:
    """Locate Hyprland's synchronous command IPC socket (`.socket.sock`, NOT `.socket2.sock`)."""
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "").strip()
    if not sig:
        return None
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    candidates: list[Path] = []
    if runtime_dir:
        candidates.append(Path(runtime_dir) / "hypr" / sig / ".socket.sock")
    candidates.append(Path("/tmp/hypr") / sig / ".socket.sock")
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def _niri_socket_request(payload: Any, timeout: float = 0.25) -> Optional[dict[str, Any]]:
    """Send a single synchronous JSON request directly over `$NIRI_SOCKET` (zero subprocess overhead)."""
    sock_path = os.environ.get("NIRI_SOCKET", "").strip()
    if not sock_path or not os.path.exists(sock_path):
        return None
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(sock_path)
        wire = (json.dumps(payload) + "\n").encode("utf-8")
        sock.sendall(wire)
        buf = bytearray()
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in chunk:
                break
        line = buf.decode("utf-8", errors="replace").splitlines()[0] if buf else ""
        if not line:
            return None
        parsed = json.loads(line)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _hyprland_socket_command(command: str, timeout: float = 0.25) -> Optional[str]:
    """Send a synchronous command directly over Hyprland's `.socket.sock` (never `.socket2.sock`)."""
    sock_path = get_hyprland_command_socket_path()
    if not sock_path or not sock_path.exists():
        return None
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(sock_path))
        sock.sendall(command.encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")
    except Exception:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def is_niri_available() -> bool:
    """Return True when Niri binary and active NIRI_SOCKET are available."""
    return shutil.which("niri") is not None and bool(os.environ.get("NIRI_SOCKET"))


def is_hyprland_available() -> bool:
    """Return True when Hyprland hyprctl binary and HYPRLAND_INSTANCE_SIGNATURE are available."""
    return shutil.which("hyprctl") is not None and bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def detect_compositor() -> CompositorKind:
    """Detect active supported compositor: 'niri', 'hyprland', or 'none'."""
    if is_niri_available():
        return "niri"
    if is_hyprland_available():
        return "hyprland"
    return "none"


def get_hyprland_socket2_path() -> Optional[Path]:
    """Locate Hyprland's IPC event socket (.socket2.sock) if active."""
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "").strip()
    if not sig:
        return None
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    candidates: list[Path] = []
    if runtime_dir:
        candidates.append(Path(runtime_dir) / "hypr" / sig / ".socket2.sock")
    candidates.append(Path("/tmp/hypr") / sig / ".socket2.sock")
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def normalize_niri_window(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Niri window JSON object to Capri's unified schema."""
    layout = raw.get("layout") if isinstance(raw.get("layout"), dict) else {}
    return {
        "id": raw.get("id"),
        "app_id": raw.get("app_id") or "",
        "title": raw.get("title") or "",
        "pid": raw.get("pid"),
        "workspace_id": raw.get("workspace_id"),
        "workspace_idx": raw.get("workspace_idx"),
        "is_focused": bool(raw.get("is_focused", False)),
        "is_floating": bool(raw.get("is_floating", False)),
        "is_fullscreen": bool(raw.get("is_fullscreen", False)),
        "layout": layout,
        "compositor": "niri",
    }


def normalize_hyprland_window(
    raw: dict[str, Any],
    active_address: Optional[str] = None,
) -> dict[str, Any]:
    """Normalize a Hyprland `hyprctl -j clients` / `activewindow` object to Capri's unified schema."""
    addr = str(raw.get("address") or "")
    ws = raw.get("workspace") if isinstance(raw.get("workspace"), dict) else {}
    ws_id = ws.get("id")
    at = raw.get("at") if isinstance(raw.get("at"), list) and len(raw.get("at")) >= 2 else [0, 0]
    size = raw.get("size") if isinstance(raw.get("size"), list) and len(raw.get("size")) >= 2 else [0, 0]
    is_fs = bool(raw.get("fullscreen")) or bool(raw.get("fullscreenClient"))
    is_focused = bool(raw.get("focusHistoryID") == 0) if active_address is None else (addr == active_address)
    return {
        "id": addr,
        "app_id": raw.get("class") or raw.get("initialClass") or "",
        "title": raw.get("title") or raw.get("initialTitle") or "",
        "pid": raw.get("pid"),
        "workspace_id": ws_id,
        "workspace_idx": ws_id if isinstance(ws_id, int) else 1,
        "is_focused": is_focused,
        "is_floating": bool(raw.get("floating", False)),
        "is_fullscreen": is_fs,
        "layout": {
            "window_size": [int(size[0]), int(size[1])],
            "tile_size": [float(size[0]), float(size[1])],
            "tile_pos_in_workspace_view": [float(at[0]), float(at[1])],
        },
        "compositor": "hyprland",
    }


def normalize_hyprland_workspaces(raw_list: list[dict[str, Any]], active_ws_id: Optional[int] = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for ws in raw_list:
        ws_id = ws.get("id")
        is_act = (ws_id == active_ws_id) if active_ws_id is not None else False
        result.append(
            {
                "id": ws_id,
                "idx": ws_id if isinstance(ws_id, int) else 1,
                "name": ws.get("name") or str(ws_id),
                "is_active": is_act,
                "is_focused": is_act,
            }
        )
    return result


def parse_hyprland_socket2_line(line: str) -> Optional[tuple[str, str]]:
    """Parse a Hyprland socket2 event line formatted as `EVENT>>DATA`."""
    if ">>" not in line:
        return None
    ev, data = line.split(">>", 1)
    return ev.strip(), data.strip()


# Action mapping from common Capri/Niri action names to Hyprland dispatcher args
_NIRI_TO_HYPRLAND_ACTIONS: dict[str, list[str]] = {
    "focus-workspace-down": ["workspace", "e+1"],
    "focus-workspace-up": ["workspace", "e-1"],
    "fullscreen-window": ["fullscreen", "0"],
    "maximize-column": ["fullscreen", "1"],
    "center-column": ["centerwindow"],
    "focus-column-left": ["movefocus", "l"],
    "focus-column-right": ["movefocus", "r"],
    "move-column-left": ["movewindow", "l"],
    "move-column-right": ["movewindow", "r"],
    "close-window": ["killactive"],
}

_HYPRLAND_UNSUPPORTED_ACTIONS: frozenset[str] = frozenset({
    "toggle-overview",
})


def _run_sync(cmd: list[str], timeout: float = 2.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:
        return -1, "", str(e)


async def _run_async(cmd: list[str], timeout: float = 3.0) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except Exception as e:
        return -1, "", str(e)


class CompositorBackend:
    """Unified Niri + Hyprland IPC adapter."""

    def __init__(self, forced_kind: Optional[CompositorKind] = None):
        self._forced_kind = forced_kind

    @property
    def kind(self) -> CompositorKind:
        if self._forced_kind is not None:
            return self._forced_kind
        return detect_compositor()

    @property
    def is_available(self) -> bool:
        return self.kind in ("niri", "hyprland")

    # ── Sync IPC (for Qt threads / LocomotionController) ────────────────────
    def get_windows(self) -> list[dict[str, Any]]:
        k = self.kind
        if k == "niri":
            resp = _niri_socket_request("Windows")
            if isinstance(resp, dict) and isinstance(resp.get("Ok"), dict):
                raw_wins = resp["Ok"].get("Windows")
                if isinstance(raw_wins, list):
                    return [normalize_niri_window(w) for w in raw_wins if isinstance(w, dict)]
            code, out, _ = _run_sync(["niri", "msg", "-j", "windows"])
            if code == 0 and out.strip():
                try:
                    data = json.loads(out)
                    if isinstance(data, list):
                        return [normalize_niri_window(w) for w in data if isinstance(w, dict)]
                except json.JSONDecodeError:
                    return []
        elif k == "hyprland":
            active = self.get_focused_window()
            active_addr = str(active.get("id")) if active else None
            raw = _hyprland_socket_command("j/clients")
            if raw and raw.strip():
                try:
                    data = json.loads(raw)
                    if isinstance(data, list):
                        return [
                            normalize_hyprland_window(w, active_address=active_addr)
                            for w in data
                            if isinstance(w, dict)
                        ]
                except json.JSONDecodeError:
                    pass
            code, out, _ = _run_sync(["hyprctl", "-j", "clients"])
            if code == 0 and out.strip():
                try:
                    data = json.loads(out)
                    if isinstance(data, list):
                        return [
                            normalize_hyprland_window(w, active_address=active_addr)
                            for w in data
                            if isinstance(w, dict)
                        ]
                except json.JSONDecodeError:
                    return []
        return []

    def get_focused_window(self) -> Optional[dict[str, Any]]:
        k = self.kind
        if k == "niri":
            code, out, _ = _run_sync(["niri", "msg", "-j", "focused-window"])
            if code == 0 and out.strip():
                try:
                    data = json.loads(out)
                    if isinstance(data, dict) and "id" in data:
                        return normalize_niri_window(data)
                except json.JSONDecodeError:
                    return None
        elif k == "hyprland":
            code, out, _ = _run_sync(["hyprctl", "-j", "activewindow"])
            if code == 0 and out.strip():
                try:
                    data = json.loads(out)
                    if isinstance(data, dict) and data.get("address"):
                        return normalize_hyprland_window(data, active_address=str(data.get("address")))
                except json.JSONDecodeError:
                    return None
        return None

    def get_workspaces(self) -> list[dict[str, Any]]:
        k = self.kind
        if k == "niri":
            code, out, _ = _run_sync(["niri", "msg", "-j", "workspaces"])
            if code == 0 and out.strip():
                try:
                    data = json.loads(out)
                    return data if isinstance(data, list) else []
                except json.JSONDecodeError:
                    return []
        elif k == "hyprland":
            active_id: Optional[int] = None
            acode, aout, _ = _run_sync(["hyprctl", "-j", "activeworkspace"])
            if acode == 0 and aout.strip():
                try:
                    adata = json.loads(aout)
                    if isinstance(adata, dict) and isinstance(adata.get("id"), int):
                        active_id = adata["id"]
                except json.JSONDecodeError:
                    pass
            code, out, _ = _run_sync(["hyprctl", "-j", "workspaces"])
            if code == 0 and out.strip():
                try:
                    data = json.loads(out)
                    if isinstance(data, list):
                        return normalize_hyprland_workspaces(data, active_ws_id=active_id)
                except json.JSONDecodeError:
                    return []
        return []

    def get_monitors(self) -> list[MonitorWorkArea]:
        """Return logical-pixel MonitorWorkArea list for all active outputs/monitors."""
        k = self.kind
        if k == "niri":
            focused_name: Optional[str] = None
            f_resp = _niri_socket_request("FocusedOutput")
            if isinstance(f_resp, dict):
                f_ok = f_resp.get("Ok", {})
                f_out = f_ok.get("FocusedOutput") if isinstance(f_ok, dict) else None
                if isinstance(f_out, dict) and f_out.get("name"):
                    focused_name = str(f_out["name"])
            o_resp = _niri_socket_request("Outputs")
            if isinstance(o_resp, dict) and isinstance(o_resp.get("Ok"), dict):
                outputs = o_resp["Ok"].get("Outputs")
                mons = normalize_niri_outputs(outputs, focused_name=focused_name)
                if mons:
                    return self._apply_niri_workspace_insets(mons)
            code, out, _ = _run_sync(["niri", "msg", "-j", "outputs"])
            if code == 0 and out.strip():
                try:
                    mons = normalize_niri_outputs(json.loads(out), focused_name=focused_name)
                    return self._apply_niri_workspace_insets(mons) if mons else []
                except json.JSONDecodeError:
                    return []
        elif k == "hyprland":
            raw = _hyprland_socket_command("j/monitors")
            if raw and raw.strip():
                try:
                    mons = normalize_hyprland_monitors(json.loads(raw))
                    if mons:
                        return mons
                except json.JSONDecodeError:
                    pass
            code, out, _ = _run_sync(["hyprctl", "-j", "monitors"])
            if code == 0 and out.strip():
                try:
                    return normalize_hyprland_monitors(json.loads(out))
                except json.JSONDecodeError:
                    return []
        return []

    def _apply_niri_workspace_insets(self, mons: list[MonitorWorkArea]) -> list[MonitorWorkArea]:
        """
        Niri's `Outputs` JSON reports full logical screen dimensions, while floating window
        coordinates live inside the workspace view (excluding top/bottom layer-shell bars).
        When `reserved` is not reported, infer the bar inset from any full-width tiled window.
        """
        if not mons or any((m.reserved_top > 0 or m.reserved_bottom > 0) for m in mons):
            return mons
        resp = _niri_socket_request("Windows")
        if not isinstance(resp, dict) or not isinstance(resp.get("Ok"), dict):
            return mons
        raw_wins = resp["Ok"].get("Windows")
        if not isinstance(raw_wins, list):
            return mons

        inferred_inset = 0.0
        for w in raw_wins:
            if not isinstance(w, dict) or w.get("is_floating") or w.get("is_fullscreen"):
                continue
            layout = w.get("layout") if isinstance(w.get("layout"), dict) else {}
            ts = layout.get("tile_size") or layout.get("window_size")
            if not isinstance(ts, list) or len(ts) < 2:
                continue
            try:
                tw, th = float(ts[0]), float(ts[1])
            except (TypeError, ValueError):
                continue
            for m in mons:
                if abs(tw - m.width) <= 2.0 and 1.0 <= (m.height - th) <= 120.0:
                    inferred_inset = max(inferred_inset, m.height - th)

        if inferred_inset <= 0.0:
            return mons
        return [
            MonitorWorkArea(
                name=m.name,
                x=m.x,
                y=m.y,
                width=m.width,
                height=m.height,
                scale=m.scale,
                reserved_left=m.reserved_left,
                reserved_top=m.reserved_top,
                reserved_right=m.reserved_right,
                reserved_bottom=max(m.reserved_bottom, inferred_inset),
                is_focused=m.is_focused,
            )
            for m in mons
        ]

    def get_monitor_for_point(self, px: float, py: float) -> MonitorWorkArea:
        return resolve_monitor_for_point(self.get_monitors(), px, py)

    def get_screen_size(self) -> tuple[int, int]:
        mons = self.get_monitors()
        if mons:
            focused = next((m for m in mons if m.is_focused), mons[0])
            return int(round(focused.width)), int(round(focused.height))
        return 1920, 1080

    def ensure_window_floating_sync(self, win_id: Any) -> bool:
        """Explicitly float ONLY the window matching `win_id` (never falls back to focused window)."""
        k = self.kind
        if k == "niri":
            nid = _valid_niri_win_id(win_id)
            if nid is None:
                return False
            resp = _niri_socket_request({"Action": {"MoveWindowToFloating": {"id": nid}}})
            if isinstance(resp, dict):
                return "Ok" in resp
            code, _, _ = _run_sync(["niri", "msg", "action", "move-window-to-floating", "--id", str(nid)])
            return code == 0
        if k == "hyprland":
            addr = _valid_hyprland_address(win_id)
            if addr is None:
                return False
            resp = _hyprland_socket_command(f"dispatch setfloating address:{addr}")
            if resp is not None:
                return resp.strip().lower() == "ok"
            code, _, _ = _run_sync(["hyprctl", "dispatch", "setfloating", f"address:{addr}"])
            return code == 0
        return False

    def move_window_to_workspace_sync(self, win_id: Any, ws_idx: int) -> bool:
        k = self.kind
        if k == "niri":
            nid = _valid_niri_win_id(win_id)
            if nid is None:
                return False
            code, _, _ = _run_sync(
                ["niri", "msg", "action", "move-window-to-workspace", "--window-id", str(nid), "--focus", "false", str(ws_idx)]
            )
            return code == 0
        if k == "hyprland":
            addr = _valid_hyprland_address(win_id)
            if addr is None:
                return False
            resp = _hyprland_socket_command(f"dispatch movetoworkspacesilent {int(ws_idx)},address:{addr}")
            if resp is not None:
                return resp.strip().lower() == "ok"
            code, _, _ = _run_sync(
                ["hyprctl", "dispatch", "movetoworkspacesilent", f"{int(ws_idx)},address:{addr}"]
            )
            return code == 0
        return False

    def move_floating_window_sync(self, win_id: Any, dx: int, dy: int) -> bool:
        """Move ONLY the floating window identified by `win_id` by relative logical delta (dx, dy)."""
        k = self.kind
        if k == "niri":
            nid = _valid_niri_win_id(win_id)
            if nid is None:
                return False
            resp = _niri_socket_request(
                {
                    "Action": {
                        "MoveFloatingWindow": {
                            "id": nid,
                            "x": {"AdjustFixed": float(dx)},
                            "y": {"AdjustFixed": float(dy)},
                        }
                    }
                }
            )
            if isinstance(resp, dict):
                return "Ok" in resp
            code, _, _ = _run_sync(
                ["niri", "msg", "action", "move-floating-window", "--id", str(nid), "-x", f"{dx:+d}", "-y", f"{dy:+d}"]
            )
            return code == 0
        if k == "hyprland":
            addr = _valid_hyprland_address(win_id)
            if addr is None:
                return False
            resp = _hyprland_socket_command(f"dispatch movewindowpixel {int(dx)} {int(dy)},address:{addr}")
            if resp is not None:
                return resp.strip().lower() == "ok"
            code, _, _ = _run_sync(
                ["hyprctl", "dispatch", "movewindowpixel", f"{int(dx)} {int(dy)},address:{addr}"]
            )
            return code == 0
        return False

    def move_floating_window_exact_sync(self, win_id: Any, x: int, y: int) -> bool:
        """Place ONLY the floating window identified by `win_id` at exact logical pixel coordinates (x, y)."""
        k = self.kind
        if k == "niri":
            nid = _valid_niri_win_id(win_id)
            if nid is None:
                return False
            resp = _niri_socket_request(
                {
                    "Action": {
                        "MoveFloatingWindow": {
                            "id": nid,
                            "x": {"SetFixed": float(x)},
                            "y": {"SetFixed": float(y)},
                        }
                    }
                }
            )
            if isinstance(resp, dict):
                return "Ok" in resp
            code, _, _ = _run_sync(
                ["niri", "msg", "action", "move-floating-window", "--id", str(nid), "-x", str(int(x)), "-y", str(int(y))]
            )
            return code == 0
        if k == "hyprland":
            addr = _valid_hyprland_address(win_id)
            if addr is None:
                return False
            resp = _hyprland_socket_command(f"dispatch movewindowpixel exact {int(x)} {int(y)},address:{addr}")
            if resp is not None:
                return resp.strip().lower() == "ok"
            code, _, _ = _run_sync(
                ["hyprctl", "dispatch", "movewindowpixel", f"exact {int(x)} {int(y)},address:{addr}"]
            )
            return code == 0
        return False

    # ── Async IPC (for tools/desktop.py & agent tools) ──────────────────────
    async def async_list_windows(self) -> list[dict[str, Any]]:
        k = self.kind
        if k == "niri":
            code, out, _ = await _run_async(["niri", "msg", "-j", "windows"])
            if code == 0 and out.strip():
                try:
                    data = json.loads(out)
                    if isinstance(data, list):
                        return [normalize_niri_window(w) for w in data if isinstance(w, dict)]
                except json.JSONDecodeError:
                    return []
        elif k == "hyprland":
            acode, aout, _ = await _run_async(["hyprctl", "-j", "activewindow"])
            active_addr: Optional[str] = None
            if acode == 0 and aout.strip():
                try:
                    adata = json.loads(aout)
                    if isinstance(adata, dict) and adata.get("address"):
                        active_addr = str(adata["address"])
                except json.JSONDecodeError:
                    pass
            code, out, _ = await _run_async(["hyprctl", "-j", "clients"])
            if code == 0 and out.strip():
                try:
                    data = json.loads(out)
                    if isinstance(data, list):
                        return [
                            normalize_hyprland_window(w, active_address=active_addr)
                            for w in data
                            if isinstance(w, dict)
                        ]
                except json.JSONDecodeError:
                    return []
        return []

    async def async_focus_window(self, win_id: Any) -> tuple[bool, str]:
        k = self.kind
        if k == "niri":
            code, _, err = await _run_async(["niri", "msg", "action", "focus-window", "--id", str(win_id)])
            return code == 0, err.strip()
        if k == "hyprland":
            code, _, err = await _run_async(["hyprctl", "dispatch", "focuswindow", f"address:{win_id}"])
            return code == 0, err.strip()
        return False, "No supported compositor active"

    async def async_dispatch_action(self, action: str) -> tuple[bool, str]:
        k = self.kind
        action_clean = action.strip()
        if not action_clean:
            return False, "Пустое действие композитора."
        if k == "niri":
            code, out, err = await _run_async(["niri", "msg", "action"] + action_clean.split())
            return code == 0, (err or out).strip()
        if k == "hyprland":
            action_l = action_clean.lower()
            if action_l in _HYPRLAND_UNSUPPORTED_ACTIONS:
                return (
                    False,
                    f"Действие '{action_clean}' специфично для Niri и не поддерживается в базовом Hyprland.",
                )
            mapped = _NIRI_TO_HYPRLAND_ACTIONS.get(action_l)
            args = mapped if mapped else action_clean.split()
            code, out, err = await _run_async(["hyprctl", "dispatch"] + args)
            return code == 0, (err or out).strip()
        return False, "No supported compositor active"


compositor = CompositorBackend()
