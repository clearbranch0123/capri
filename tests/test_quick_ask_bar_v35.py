"""
Tests for v3.5 Block 1 — Quick Ask Bar (`capri-deck --ask`):
- Floating Crimson Graphite overlay lifecycle (open/focus/toggle/dismiss/Esc stop vs dismiss)
- Clipboard image paste (`Ctrl+V`) + ownership transfer on submit + zero `/tmp` leak on remove/close
- Live streaming response drawer (begin_generation, tool activity, streaming, final, copy, collapse, open Cyberdeck)
- Cyberdeck IPC `"ask"` roundtrip + cold-start `launch_cyberdeck(["--ask"])` CLI dispatch
- Screen-region capture routed back to Quick Ask Bar without popping full Cyberdeck
- Niri & Hyprland WM floating rules + `--ask` hotkey generation
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

from capri.core.attachments import AttachmentItem, cleanup_temp_attachment
from capri.core.wm_rules import generate_hyprland_rules, generate_niri_rules
from capri.ui.client import send_cyberdeck_command
from capri.ui.cyberdeck import CapriCyberdeckWindow, launch_cyberdeck
from capri.ui.quick_ask import CapriQuickAskBar


def test_quick_ask_bar_submit_attachments_streaming_and_cleanup(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])

    bar = CapriQuickAskBar(None)
    submitted: list[tuple[str, list]] = []
    stops: list[bool] = []
    opened_deck: list[bool] = []
    bar.submit_requested.connect(lambda txt, atts: submitted.append((txt, atts)))
    bar.stop_requested.connect(lambda: stops.append(True))
    bar.open_cyberdeck_requested.connect(lambda: opened_deck.append(True))

    try:
        # 1. Empty submit does nothing; compact bar stays locked to minimal floating height (no tiling stretch!)
        assert bool(bar.windowFlags() & Qt.WindowType.Tool) is True
        assert bar.minimumWidth() == bar.maximumWidth() == 640
        assert bar.minimumHeight() == bar.maximumHeight()
        compact_h = bar.height()
        assert 76 <= compact_h <= 100
        bar._on_submit_clicked()
        assert submitted == []

        # 2. Paste clipboard image -> attaches chip & removes cleanly with temp PNG deletion
        im = QImage(32, 32, QImage.Format.Format_ARGB32)
        im.fill(QColor(225, 45, 83))
        bar._on_clipboard_image_pasted(im)
        assert len(bar.attachments()) == 1
        pasted_path = bar.attachments()[0].path
        assert os.path.exists(pasted_path)
        bar.remove_attachment(0, cleanup_temp=True)
        assert bar.attachments() == []
        assert not os.path.exists(pasted_path)

        # 3. Attach image + text -> submit transfers ownership without deleting temp PNG
        bar._on_clipboard_image_pasted(im)
        att_path = bar.attachments()[0].path
        bar.input_edit.setText("Что на этом графике?")
        bar._on_submit_clicked()
        assert len(submitted) == 1
        prompt_text, atts = submitted[0]
        assert prompt_text == "Что на этом графике?"
        assert len(atts) == 1 and os.path.exists(att_path)
        assert bar.attachments() == []
        assert bar.input_edit.text() == ""
        assert bar._generating is True

        # Clean up transferred temp attachment
        for a in atts:
            cleanup_temp_attachment(a)
        assert not os.path.exists(att_path)

        # 4. Esc while generating emits stop_requested instead of hiding; streaming & final update drawer
        bar.set_tool_activity("web_search(query='niri')")
        assert "web_search" in bar.lbl_tools.text()
        bar.set_streaming_text("Вижу график загрузки CPU...")
        assert "Вижу график" in bar.txt_response.toPlainText()

        bar._on_escape_pressed()
        assert stops == [True]

        bar.set_final_text("Загрузка CPU стабильна на 12%.")
        assert bar._generating is False
        assert bar.lbl_status.text() == "ГОТОВО"

        # Copy & Collapse & Open in Cyberdeck
        bar._on_copy_clicked()
        assert QApplication.clipboard().text() == "Загрузка CPU стабильна на 12%."
        bar.collapse_response()
        assert bar.response_panel.isHidden() is True
        assert bar.height() == compact_h
        bar._on_open_cyberdeck_clicked()
        assert opened_deck == [True]

        # 5. Closing bar cleans up any owned temp attachments (zero /tmp leak)
        tmp_owned = tmp_path / "owned_temp.png"
        tmp_owned.write_bytes(b"\x89PNG\r\n\x1a\ndata")
        bar.add_attachment(
            AttachmentItem(path=tmp_owned, kind="image", display_name="owned_temp.png", size_bytes=12, is_temp=True)
        )
        assert tmp_owned.exists()
    finally:
        bar.close()
        app.processEvents()

    assert not tmp_owned.exists()


def test_cyberdeck_ipc_ask_command_and_region_capture_routing(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])

    sock_path = str(tmp_path / "capri-ask-ipc.sock")
    events: list[str] = []

    class _FakeDeck(QWidget):
        def __init__(self):
            super().__init__()
            self.ipc_server = None
            self._ipc_socket_path = None
            self._region_capture_worker = None
            self._region_capture_target = "cyberdeck"
            self._quick_ask_bar = None

        _stop_ipc_server = CapriCyberdeckWindow._stop_ipc_server
        start_ipc_server = CapriCyberdeckWindow.start_ipc_server
        _on_ipc_connection = CapriCyberdeckWindow._on_ipc_connection
        _active_quick_ask = CapriCyberdeckWindow._active_quick_ask
        _ensure_quick_ask_bar = CapriCyberdeckWindow._ensure_quick_ask_bar
        _toggle_quick_ask = CapriCyberdeckWindow._toggle_quick_ask
        _on_quick_ask_submit = CapriCyberdeckWindow._on_quick_ask_submit
        _on_quick_ask_look_region = CapriCyberdeckWindow._on_quick_ask_look_region
        _on_region_captured = CapriCyberdeckWindow._on_region_captured

        def _send_message(self, _checked=False, *, override_text=None, override_attachments=None):
            events.append(f"send:{override_text}:{len(override_attachments or [])}")

        def _stop_generation(self, silent=False):
            events.append(f"stop:{silent}")

        def _toggle_voice_input(self):
            events.append("voice")

        def _trigger_look_region(self):
            events.append("look_region")

        def _show_and_raise(self):
            events.append("show_deck")

        def _append_message(self, role, text):
            events.append(f"msg:{role}:{text}")

    deck = _FakeDeck()
    try:
        assert deck.start_ipc_server(sock_path) is True
        res: list[bool] = []
        t = threading.Thread(target=lambda: res.append(send_cyberdeck_command("ask", socket_path=sock_path, timeout=1.0)))
        t.start()
        for _ in range(40):
            app.processEvents()
            if not t.is_alive():
                break
            time.sleep(0.01)
        t.join(timeout=1.0)
        assert res == [True]
        assert deck._quick_ask_bar is not None
        assert deck._quick_ask_bar.isVisible() is True

        # Submitting from Quick Ask Bar passes override_text directly without touching Cyberdeck composer
        deck._quick_ask_bar.input_edit.setText("Сколько свободной RAM?")
        deck._quick_ask_bar._on_submit_clicked()
        assert events[-1] == "send:Сколько свободной RAM?:0"

        # Region capture triggered from Quick Ask routes PNG back to Quick Ask (NOT full Cyberdeck!)
        events.clear()
        deck._on_quick_ask_look_region()
        assert deck._region_capture_target == "quick_ask"
        assert events == ["look_region"]

        tmp_png = tmp_path / "region_qa.png"
        im = QImage(28, 28, QImage.Format.Format_ARGB32)
        im.fill(QColor(78, 216, 232))
        assert im.save(str(tmp_png), "PNG") is True

        deck._on_region_captured(str(tmp_png), "ok")
        assert not tmp_png.exists()
        assert deck._region_capture_target == "cyberdeck"
        assert len(deck._quick_ask_bar.attachments()) == 1
        assert "show_deck" not in events

        # Regression 1: Cancelling region capture via Esc restores Quick Ask cleanly with zero error
        deck._quick_ask_bar.collapse_response()
        deck._on_quick_ask_look_region()
        assert deck._quick_ask_bar.isVisible() is False
        deck._on_region_captured("", "cancelled")
        assert deck._region_capture_target == "cyberdeck"
        assert deck._quick_ask_bar.isVisible() is True
        assert deck._quick_ask_bar.response_panel.isHidden() is True

        # Regression 2: Hiding Quick Ask mid-generation MUST still receive completion/cancel/error updates
        # so `_generating` never gets stuck True when reopened later!
        qa = deck._quick_ask_bar
        qa.begin_generation()
        assert qa._generating is True
        qa.hide()
        assert qa.isVisible() is False
        assert deck._active_quick_ask() is qa

        qa_target = deck._active_quick_ask()
        qa_target.set_streaming_text("Промежуточный ответ…")
        qa_target.set_final_text("Финальный ответ готов.")
        assert qa._generating is False
        assert qa.isVisible() is False  # stayed hidden, did not pop unexpectedly
        assert qa.lbl_status.text() == "ГОТОВО"
        assert "Финальный ответ готов." in qa.txt_response.toPlainText()
        assert deck._active_quick_ask() is None  # hidden & idle -> no longer mirrored
    finally:
        if deck._quick_ask_bar is not None:
            deck._quick_ask_bar.close()
        deck._stop_ipc_server()
        deck.close()


def test_cli_ask_flag_and_wm_rules_include_quick_ask(monkeypatch, tmp_path):
    # CLI `--ask` delegates to IPC `"ask"` and exits 0 when Cyberdeck is running
    ipc_cmds: list[str] = []
    monkeypatch.setattr("capri.ui.cyberdeck.send_cyberdeck_ipc", lambda cmd, **kw: (ipc_cmds.append(cmd) or True))
    assert launch_cyberdeck(["--ask"]) == 0
    assert ipc_cmds == ["ask"]

    # Niri & Hyprland WM rules float "Capri Quick Ask", support custom hotkeys end-to-end through install_wm_rules,
    # AND preserve user-customized hotkeys on idempotent re-apply!
    from capri.core.wm_rules import generate_wm_rules, install_wm_rules

    niri = generate_wm_rules("niri", include_hotkey=True, toggle_hotkey="Mod+Alt+C", ask_hotkey="Mod+Alt+Space")
    assert 'match title=r#"^Capri Quick Ask$"#' in niri
    assert 'Mod+Alt+C { spawn "capri-deck" "--toggle"; }' in niri
    assert 'Mod+Alt+Space { spawn "capri-deck" "--ask"; }' in niri
    niri_no_hk = generate_wm_rules("niri", include_hotkey=False)
    assert 'match title=r#"^Capri Quick Ask$"#' in niri_no_hk
    assert "capri-deck" not in niri_no_hk

    # Install custom hotkeys to isolated Niri config -> re-apply without overrides preserves custom binds!
    niri_dir = tmp_path / "niri"
    niri_dir.mkdir()
    res_init = install_wm_rules(
        "niri",
        config_dir=niri_dir,
        dry_run=False,
        include_hotkey=True,
        toggle_hotkey="Mod+Alt+C",
        ask_hotkey="Mod+Alt+Space",
    )
    assert res_init.applied is True
    res_reapply = install_wm_rules("niri", config_dir=niri_dir, dry_run=False, include_hotkey=True)
    assert res_reapply.updated_existing_block is True
    final_kdl = (niri_dir / "config.kdl").read_text(encoding="utf-8")
    assert 'Mod+Alt+C { spawn "capri-deck" "--toggle"; }' in final_kdl
    assert 'Mod+Alt+Space { spawn "capri-deck" "--ask"; }' in final_kdl

    hypr = generate_wm_rules("hyprland", include_hotkey=True, ask_hotkey="SUPER ALT, SPACE")
    assert "windowrulev2 = float, title:^(Capri Quick Ask)$" in hypr
    assert "bind = SUPER ALT, SPACE, exec, capri-deck --ask" in hypr

    # Avatar quick popover "⚡ Быстрый вопрос" button sends IPC "ask" without requiring any WM hotkey
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CAPRI_DISABLE_LAYER_SHELL", "1")
    QApplication.instance() or QApplication([])
    from capri.ui.overlay import CapriAvatarWidget
    sent_from_avatar: list[str] = []
    monkeypatch.setattr("capri.ui.client.send_cyberdeck_command", lambda cmd, **kw: (sent_from_avatar.append(cmd) or True))
    av = CapriAvatarWidget(mode="2d")
    try:
        assert "Быстрый вопрос" in av.quick_popover.btn_quick_ask.text()
        av.quick_popover.btn_quick_ask.click()
        assert sent_from_avatar == ["ask"]
    finally:
        av.close()


def test_quick_ask_sticky_pin_workspace_follow_and_voice_status_reset(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    # 1. CompositorBackend.follow_window_to_active_workspace_sync moves ONLY Quick Ask across Niri workspaces
    from capri.core.compositor import CompositorBackend

    niri_calls: list[object] = []

    def _fake_niri_req(payload, timeout=0.35):
        niri_calls.append(payload)
        if payload == "Workspaces":
            return {"Ok": {"Workspaces": [{"id": 10, "idx": 1, "is_focused": False}, {"id": 22, "idx": 3, "is_focused": True}]}}
        if payload == "Windows":
            return {"Ok": {"Windows": [{"id": 77, "title": "Capri Quick Ask", "workspace_id": 10, "is_floating": True, "is_focused": False}]}}
        if isinstance(payload, dict) and "Action" in payload:
            return {"Ok": "Handled"}
        return None

    monkeypatch.setattr("capri.core.compositor._niri_socket_request", _fake_niri_req)
    comp = CompositorBackend(forced_kind="niri")
    found, moved = comp.follow_window_to_active_workspace_sync("Capri Quick Ask", focus=False)
    assert found is True and moved is True

    # 1b. Hyprland native `set_window_pinned_sync` toggles `dispatch pin` only when state differs
    hypr_cmds: list[str] = []
    hypr_pinned_state = {"pinned": True}

    def _fake_hypr_cmd(cmd, timeout=0.25):
        hypr_cmds.append(cmd)
        if cmd == "j/clients":
            return (
                '[{"address":"0xabc123","title":"Capri Quick Ask","workspace":{"id":4},'
                f'"floating":true,"focusHistoryID":0,"pinned":{str(hypr_pinned_state["pinned"]).lower()}}}]'
            )
        if cmd == "dispatch pin address:0xabc123":
            hypr_pinned_state["pinned"] = not hypr_pinned_state["pinned"]
            return "ok"
        return "ok"

    monkeypatch.setattr("capri.core.compositor._hyprland_socket_command", _fake_hypr_cmd)
    hcomp = CompositorBackend(forced_kind="hyprland")
    # Already pinned -> requesting True is a no-op (no double-toggle!)
    assert hcomp.set_window_pinned_sync("Capri Quick Ask", True) is True
    assert "dispatch pin address:0xabc123" not in hypr_cmds
    # Requesting False -> unpins natively even if static WM rule had `pin = true`
    assert hcomp.set_window_pinned_sync("Capri Quick Ask", False) is True
    assert hypr_pinned_state["pinned"] is False
    assert hypr_cmds.count("dispatch pin address:0xabc123") == 1
    # Requesting True again -> re-pins natively
    assert hcomp.set_window_pinned_sync("Capri Quick Ask", True) is True
    assert hypr_pinned_state["pinned"] is True
    assert hypr_cmds.count("dispatch pin address:0xabc123") == 2

    # 1c. Hyprland 0.56+ Lua config mode falls back cleanly to `hl.dsp.window.pin({ window = "address:..." })`
    lua_cmds: list[str] = []
    lua_pinned_state = {"pinned": True}

    def _fake_hypr_lua_cmd(cmd, timeout=0.25):
        lua_cmds.append(cmd)
        if cmd == "j/clients":
            return (
                '[{"address":"0xdef456","title":"Capri Quick Ask","workspace":{"id":4},'
                f'"floating":true,"focusHistoryID":0,"pinned":{str(lua_pinned_state["pinned"]).lower()}}}]'
            )
        if cmd == "dispatch pin address:0xdef456":
            return 'error: 3 [string "return hl.dispatch(pin address:0xdef456)"]:1: \')\' expected'
        if cmd == 'dispatch hl.dsp.window.pin({ window = "address:0xdef456" })':
            lua_pinned_state["pinned"] = not lua_pinned_state["pinned"]
            return "ok"
        return "error"

    monkeypatch.setattr("capri.core.compositor._hyprland_socket_command", _fake_hypr_lua_cmd)
    assert hcomp.set_window_pinned_sync("Capri Quick Ask", False) is True
    assert lua_pinned_state["pinned"] is False
    assert 'dispatch hl.dsp.window.pin({ window = "address:0xdef456" })' in lua_cmds
    assert any(
        isinstance(c, dict)
        and c.get("Action", {}).get("MoveWindowToWorkspace") == {"window_id": 77, "reference": {"Id": 22}, "focus": False}
        for c in niri_calls
    )
    # Silent follow (focus=False) MUST NOT steal focus!
    assert not any(isinstance(c, dict) and "FocusWindow" in c.get("Action", {}) for c in niri_calls)

    # Summon with focus=True DOES focus the moved window
    niri_calls.clear()
    found2, moved2 = comp.follow_window_to_active_workspace_sync("Capri Quick Ask", focus=True)
    assert found2 is True and moved2 is True
    assert any(isinstance(c, dict) and c.get("Action", {}).get("FocusWindow") == {"id": 77} for c in niri_calls)

    # 2. Quick Ask resolves CompositorBackend cleanly, sticky pin defaults ON, toggles, and voice-stop clears "СЛУШАЮ…"
    bar = CapriQuickAskBar(None)
    try:
        assert isinstance(bar._get_compositor(), CompositorBackend)
        assert bar.pinned is True
        bar.btn_pin.click()
        assert bar.pinned is False
        bar.btn_pin.click()
        assert bar.pinned is True

        # Voice status regression: ending voice recording resets "СЛУШАЮ…" cleanly
        bar.set_final_text("Готовый ответ.")
        bar.set_voice_recording(True)
        assert bar.lbl_status.text() == "СЛУШАЮ…"
        bar.set_voice_recording(False)
        assert bar.lbl_status.text() == "ГОТОВО"
    finally:
        bar.close()
