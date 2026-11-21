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
import subprocess
from pathlib import Path
from typing import Any, Literal, Optional

CompositorKind = Literal["niri", "hyprland", "none"]


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

    def get_screen_size(self) -> tuple[int, int]:
        k = self.kind
        if k == "niri":
            code, out, _ = _run_sync(["niri", "msg", "-j", "outputs"])
            if code == 0 and out.strip():
                try:
                    outputs = json.loads(out)
                    if isinstance(outputs, dict):
                        for _name, out_info in outputs.items():
                            logical = out_info.get("logical") if isinstance(out_info, dict) else None
                            if logical and "width" in logical and "height" in logical:
                                return int(logical["width"]), int(logical["height"])
                except json.JSONDecodeError:
                    pass
        elif k == "hyprland":
            code, out, _ = _run_sync(["hyprctl", "-j", "monitors"])
            if code == 0 and out.strip():
                try:
                    monitors = json.loads(out)
                    if isinstance(monitors, list) and monitors:
                        focused = next((m for m in monitors if isinstance(m, dict) and m.get("focused")), monitors[0])
                        if isinstance(focused, dict) and "width" in focused and "height" in focused:
                            scale = float(focused.get("scale") or 1.0)
                            scale = scale if scale > 0 else 1.0
                            return int(round(focused["width"] / scale)), int(round(focused["height"] / scale))
                except json.JSONDecodeError:
                    pass
        return 1920, 1080

    def move_window_to_workspace_sync(self, win_id: Any, ws_idx: int) -> bool:
        k = self.kind
        if k == "niri":
            code, _, _ = _run_sync(
                ["niri", "msg", "action", "move-window-to-workspace", "--window-id", str(win_id), "--focus", "false", str(ws_idx)]
            )
            return code == 0
        if k == "hyprland":
            code, _, _ = _run_sync(
                ["hyprctl", "dispatch", "movetoworkspacesilent", f"{ws_idx},address:{win_id}"]
            )
            return code == 0
        return False

    def move_floating_window_sync(self, win_id: Any, dx: int, dy: int) -> bool:
        k = self.kind
        if k == "niri":
            code, _, _ = _run_sync(
                ["niri", "msg", "action", "move-floating-window", "--id", str(win_id), "-x", f"{dx:+d}", "-y", f"{dy:+d}"]
            )
            return code == 0
        if k == "hyprland":
            code, _, _ = _run_sync(
                ["hyprctl", "dispatch", "movewindowpixel", f"{dx} {dy},address:{win_id}"]
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
