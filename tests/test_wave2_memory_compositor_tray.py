"""
Wave 2 (v2.8.0b3) Regression Tests:
- SQLite schema v1 -> v2 migration, FTS5 full-text search across dialogue turns, and hybrid search_sessions
- Rolling auto-compression (`compact_session_if_needed`), prompt injection in CapriAgent, rollback invalidation
- Hybrid ranked MemoryStore search (Russian inflections/stems, multi-word queries, fuzzy typo resilience)
- Unified CompositorBackend for Niri & Hyprland (detection, window/workspace normalization, desktop tools, DesktopObserver)
- Distro detection (Arch / CachyOS / Fedora) and Hyprland support in /doctor
- System tray toggle preference (`minimize_to_tray`) and native Wayland `capri-deck --toggle` Unix IPC
"""

from __future__ import annotations

import json
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import pytest
from PySide6.QtWidgets import QApplication

from capri.core.agent import CapriAgent
from capri.core.compositor import (
    CompositorBackend,
    detect_compositor,
    normalize_hyprland_window,
    parse_hyprland_socket2_line,
)
from capri.core.config import AppConfig
from capri.core.doctor import run_diagnostics
from capri.core.memory import MemoryStore
from capri.core.sessions import CURRENT_SCHEMA_VERSION, SessionManager
from capri.tools.desktop import focus_window, list_windows, niri_action
from capri.ui.cyberdeck import (
    CapriCyberdeckWindow,
    CapriProviderSettingsDialog,
    send_cyberdeck_ipc,
)
from capri.ui.locomotion import DesktopObserver


def test_sessions_v1_migration_fts5_search_and_rolling_compaction(tmp_path):
    db_path = tmp_path / "legacy_v1.db"

    # 1. Seed a legacy schema v1 SQLite database (no summary/compacted_up_to_seq/turns_fts)
    with sqlite3.connect(str(db_path)) as con:
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
        con.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                is_archived INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0,
                turn_count INTEGER DEFAULT 0,
                preview TEXT DEFAULT ''
            );
        """)
        con.execute("""
            CREATE TABLE turns (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                thought TEXT DEFAULT '',
                tools_json TEXT DEFAULT '[]',
                model_messages_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'completed',
                UNIQUE(session_id, sequence)
            );
        """)
        now = time.time()
        con.execute(
            "INSERT INTO sessions VALUES ('s-old', 'Старый разговор', ?, ?, 0, 0, 1, 'короткое превью')",
            (now - 100, now - 100),
        )
        con.execute(
            "INSERT INTO turns VALUES ('t-old-1', 's-old', 1, ?, 'Настраивали гипрланд и pipewire конфиг', 'Готово, звуковой стек поднят.', '', '[]', '[]', 'completed')",
            (now - 100,),
        )
        con.execute("PRAGMA user_version = 1;")
        con.commit()

    # 2. Opening SessionManager upgrades v1 -> v2 and backfills turns_fts
    sm = SessionManager(db_path=db_path)
    with sqlite3.connect(str(db_path)) as con:
        ver = con.execute("PRAGMA user_version;").fetchone()[0]
        assert ver == CURRENT_SCHEMA_VERSION

    # Search finds session by text INSIDE turn body (not present in title or preview!)
    found_sessions = sm.search_sessions("pipewire")
    assert any(s.id == "s-old" for s in found_sessions)

    # Cyrillic stem/prefix FTS5 search across turns
    turn_hits = sm.search_turns_hybrid("гипрланд")
    assert len(turn_hits) >= 1
    assert turn_hits[0]["session_id"] == "s-old"

    # 3. Rolling auto-compression on a long session (12 turns -> folds older 6 into summary)
    long_sess = sm.create_session("Длинный рабочий поток")
    for i in range(1, 13):
        sm.add_turn(
            long_sess.id,
            user_text=f"Шаг {i}: настройка модуля #{i} под Wayland",
            assistant_text=f"Выполнена проверка и сборка шага {i}.",
        )
    summary = sm.compact_session_if_needed(long_sess.id, threshold=10, keep_recent=6)
    assert summary
    assert "[#1]" in summary
    assert "[#6]" in summary
    assert "[#12]" not in summary  # Recent 6 turns stay verbatim, not folded

    rec = sm.get_session(long_sess.id)
    assert rec is not None
    assert rec.compacted_up_to_seq == 6

    # 4. CapriAgent injects compressed summary into system prompt
    cfg = AppConfig()
    cfg.voice.tts_enabled = False
    agent = CapriAgent(cfg, session_manager=sm)
    assert agent.resume_session(long_sess.id) is True
    full_prompt = agent._build_full_prompt(user_query="как там дела?")
    assert '<historical_context untrusted="true">' in full_prompt
    assert "НЕДОВЕРЕННЫЕ ДАННЫЕ" in full_prompt
    assert "[#1]" in full_prompt

    # 5. Rolling back into compacted turns cleanly invalidates stale summary
    sm.rollback_session(long_sess.id, from_sequence=4)
    rec_after = sm.get_session(long_sess.id)
    assert rec_after is not None
    assert rec_after.summary == ""
    assert rec_after.compacted_up_to_seq == 0


def test_memory_store_hybrid_ranked_search(tmp_path):
    store = MemoryStore(file_path=tmp_path / "hybrid_mem.json")
    store.add_or_update("wm_setup", "Использую Hyprland и Niri на CachyOS и Fedora", category="environment", importance=5, tags=["wayland", "linux"])
    store.add_or_update("coffee", "Любит крепкий эспрессо без сахара", category="preferences", importance=3, tags=["drinks"])
    store.add_or_update("gpu", "Видеокарта NVIDIA RTX 3050 для локальных задач", category="environment", importance=4, tags=["hardware"])

    # Inflected Russian query + multi-word match ("видеокарты локальные")
    hits_gpu = store.search_hybrid("видеокарты локальные")
    assert hits_gpu and hits_gpu[0].key == "gpu"

    # Multi-word query across key/fact/tags
    hits_wm = store.search_hybrid("настройка hyprland на федора")
    assert hits_wm and hits_wm[0].key == "wm_setup"

    # Typo tolerance via character trigrams ("эспресо" vs "эспрессо")
    hits_typo = store.search_hybrid("эспресо")
    assert hits_typo and hits_typo[0].key == "coffee"


@pytest.mark.asyncio
async def test_hyprland_compositor_backend_tools_observer_and_doctor(monkeypatch, tmp_path):
    # Test Hyprland normalization & socket2 event parsing
    raw_hypr_win = {
        "address": "0x55abc123",
        "class": "firefox",
        "title": "Arch Wiki — Hyprland",
        "pid": 4242,
        "workspace": {"id": 2, "name": "2"},
        "at": [100, 80],
        "size": [1400, 900],
        "floating": True,
        "fullscreen": 0,
        "focusHistoryID": 0,
    }
    norm = normalize_hyprland_window(raw_hypr_win, active_address="0x55abc123")
    assert norm["id"] == "0x55abc123"
    assert norm["app_id"] == "firefox"
    assert norm["workspace_id"] == 2
    assert norm["is_focused"] is True
    assert norm["is_floating"] is True
    assert norm["layout"]["window_size"] == [1400, 900]
    assert parse_hyprland_socket2_line("activewindow>>kitty,nvim main.py") == ("activewindow", "kitty,nvim main.py")

    # Simulate Hyprland active environment (no Niri)
    monkeypatch.delenv("NIRI_SOCKET", raising=False)
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test-hypr-sig")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")

    def fake_which(name: str):
        return "/usr/bin/hyprctl" if name == "hyprctl" else None

    with patch("shutil.which", side_effect=fake_which):
        assert detect_compositor() == "hyprland"

        # Mock async hyprctl responses for list_windows, focus_window, niri_action
        async def fake_run_async(cmd, timeout=3.0):
            if cmd[:3] == ["hyprctl", "-j", "activewindow"]:
                return 0, json.dumps(raw_hypr_win), ""
            if cmd[:3] == ["hyprctl", "-j", "clients"]:
                return 0, json.dumps([raw_hypr_win]), ""
            if cmd[:2] == ["hyprctl", "dispatch"]:
                return 0, "ok", ""
            return 1, "", "unexpected"

        with patch("capri.core.compositor._run_async", side_effect=fake_run_async):
            lw = await list_windows()
            assert "firefox" in lw and "0x55abc123" in lw

            fw = await focus_window("браузер")
            assert "Фокус успешно переведён" in fw

            act = await niri_action("focus-workspace-down")
            assert "Hyprland" in act and "успешно выполнено" in act

            # Niri-only action `toggle-overview` must NOT silently map to togglespecialworkspace on Hyprland
            ov = await niri_action("toggle-overview")
            assert "не поддерживается" in ov.lower()

        # /doctor recognizes Hyprland as OK
        cfg = AppConfig()
        cfg.llm.api_key = "sk-hypr-test"
        items = {i.name: i for i in run_diagnostics(cfg, sessions_db_path=tmp_path / "doc.db")}
        assert items["Окружение / WM"].status == "OK"
        assert "Hyprland" in items["Окружение / WM"].detail

        # DesktopObserver handles Hyprland workspace/fullscreen events cleanly
        obs = DesktopObserver()
        ws_events = []
        fs_events = []
        obs.workspace_changed.connect(lambda idx, wid: ws_events.append((idx, wid)))
        obs.fullscreen_changed.connect(lambda flag: fs_events.append(flag))
        obs._process_hyprland_event("workspace", "3")
        obs._process_hyprland_event("fullscreen", "1")
        assert ws_events == [(3, 3)]
        assert fs_events == [True]


def test_cyberdeck_tray_setting_and_native_wayland_toggle_ipc(tmp_path):
    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False
    cfg.ui.minimize_to_tray = True

    # Verify minimize_to_tray checkbox persists in CapriProviderSettingsDialog
    with patch("capri.ui.cyberdeck.save_config"), patch("capri.ui.cyberdeck.update_env_value"):
        dlg = CapriProviderSettingsDialog(cfg)
        assert dlg.chk_minimize_to_tray.isChecked() is True
        dlg.chk_minimize_to_tray.setChecked(False)
        dlg._apply_and_save()
        assert cfg.ui.minimize_to_tray is False
        dlg.close()

    # Verify native Wayland `capri-deck --toggle` Unix socket IPC roundtrip
    sock_path = str(tmp_path / "cyberdeck_test.sock")
    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.show()
        app.processEvents()
        assert deck.isHidden() is False

        assert deck.start_ipc_server(socket_path=sock_path) is True

        # Send toggle from a background thread while processing Qt events so QLocalServer handles it live
        import threading
        ack_box = {}

        def _send(cmd: str):
            ack_box[cmd] = send_cyberdeck_ipc(cmd, socket_path=sock_path, timeout=1.0)

        t1 = threading.Thread(target=_send, args=("toggle",))
        t1.start()
        deadline = time.time() + 2.0
        while t1.is_alive() and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)
        t1.join(timeout=1.0)
        app.processEvents()
        assert ack_box.get("toggle") is True
        assert deck.isHidden() is True

        # Second toggle restores window visibility
        t2 = threading.Thread(target=_send, args=("toggle",))
        t2.start()
        deadline = time.time() + 2.0
        while t2.is_alive() and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)
        t2.join(timeout=1.0)
        app.processEvents()
        assert ack_box.get("toggle") is True
        assert deck.isHidden() is False

        deck.close()
