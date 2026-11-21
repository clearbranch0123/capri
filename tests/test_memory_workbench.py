from __future__ import annotations

import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from PySide6.QtWidgets import QApplication
from capri.core.config import AppConfig
from capri.core.memory import MemoryStore, MemoryEntry, CATEGORIES, CATEGORY_LABELS
from capri.tools.memory import (
    remember,
    recall_memory,
    forget_memory,
    update_memory_entry,
    list_memory_categories,
    get_memory_workbench_stats,
    get_memory_store,
    set_shared_memory_store,
)
from capri.core.agent import CapriAgent
from capri.ui.cyberdeck import CapriCyberdeckWindow, CapriMemoryWorkbenchDialog, CapriMemoryEditDialog


@pytest.fixture
def clean_memory_store(tmp_path):
    test_file = tmp_path / "test_wb_memory.json"
    store = MemoryStore(file_path=test_file)
    store.clear()
    return store


def test_memory_entry_and_store_crud(clean_memory_store):
    store = clean_memory_store
    assert store.entries == {}

    # 1. Добавление фактов в разные категории
    e1 = store.add_or_update("name", "D", category="identity", importance=5, tags=["user", "callsign"])
    e2 = store.add_or_update("editor", "Neovim с Lua-конфигом", category="preferences", importance=4)
    e3 = store.add_or_update("distro", "CachyOS / Arch Linux", category="environment", importance=3)

    assert len(store.entries) == 3
    assert e1.category == "identity"
    assert e1.importance == 5
    assert "callsign" in e1.tags
    assert e2.fact == "Neovim с Lua-конфигом"

    # 2. Обновление факта
    e2_upd = store.add_or_update("editor", "Neovim v0.11", category="preferences", importance=5)
    assert len(store.entries) == 3
    assert e2_upd.fact == "Neovim v0.11"
    assert e2_upd.importance == 5

    # 3. Удаление
    assert store.delete("distro")
    assert not store.delete("non_existent")
    assert len(store.entries) == 2

    # 4. Очистка по категории
    store.add_or_update("wm", "Niri Wayland", category="environment")
    assert len(store.entries) == 3
    cleared = store.clear(category="preferences")
    assert cleared == 1
    assert "editor" not in store.entries
    assert "name" in store.entries
    assert "wm" in store.entries

    # 5. Полная очистка
    total_cleared = store.clear()
    assert total_cleared == 2
    assert len(store.entries) == 0


def test_memory_backward_compatibility(tmp_path):
    test_file = tmp_path / "legacy_memory.json"
    legacy_data = {
        "city": {"fact": "Night City", "updated_at": "2026-11-01 12:00:00"},
        "alias": "D",
        "a" * 100: {"fact": "Oversized key fact"},
    }
    with open(test_file, "w", encoding="utf-8") as f:
        json.dump(legacy_data, f)

    # Загружаем в новый MemoryStore
    store = MemoryStore(file_path=test_file)
    assert len(store.entries) == 3
    assert "city" in store.entries
    assert ("a" * 64) in store.entries
    assert store.entries["a" * 64].key == "a" * 64
    assert store.delete("a" * 100)  # delete with original oversized key correctly normalizes to 64 chars
    assert len(store.entries) == 2
    assert "city" in store.entries
    assert store.entries["city"].fact == "Night City"
    assert store.entries["city"].category == "general"
    # Метка времени из строки должна быть корректно распарсена, а не заменена на time.time()
    expected_ts = time.mktime(time.strptime("2026-11-01 12:00:00", "%Y-%m-%d %H:%M:%S"))
    assert store.entries["city"].updated_at == expected_ts

    assert "alias" in store.entries
    assert store.entries["alias"].fact == "D"
    assert store.entries["alias"].importance == 3

    # Проверяем семантику полной замены для save_memories
    from capri.tools.memory import save_memories, load_memories, set_shared_memory_store
    set_shared_memory_store(store)
    save_memories({"single": {"fact": "One fact"}})
    assert len(store.entries) == 1
    assert "single" in store.entries
    assert "city" not in store.entries
    save_memories({})
    assert load_memories() == {}


def test_memory_search_and_filtering(clean_memory_store):
    store = clean_memory_store
    store.add_or_update("lang_py", "Python 3.12 для бэкенда", category="projects", importance=4, tags=["code", "python"])
    store.add_or_update("lang_rs", "Rust для низкоуровневых тулов", category="projects", importance=5, tags=["code", "rust"])
    store.add_or_update("coffee", "Эспрессо без сахара", category="preferences", importance=2, tags=["diet"])
    store.add_or_update("rule_git", "Никогда не пушить без тестов", category="constraints", importance=5, tags=["rules"])

    # 1. Поиск по тексту
    res_py = store.search(query="python")
    assert len(res_py) == 1
    assert res_py[0].key == "lang_py"

    # 2. Поиск по категории
    res_proj = store.search(category="projects")
    assert len(res_proj) == 2
    # Первым должен идти более важный факт (importance=5)
    assert res_proj[0].key == "lang_rs"

    # 3. Поиск по тегу
    res_tag = store.search(tag="diet")
    assert len(res_tag) == 1
    assert res_tag[0].key == "coffee"


def test_bounded_prompt_context(clean_memory_store):
    store = clean_memory_store
    store.add_or_update("user_id", "D", category="identity", importance=5)
    store.add_or_update("os", "Arch Linux", category="environment", importance=4)
    # Добавляем слишком длинный факт
    long_fact = "ОЧЕНЬ_ДЛИННЫЙ_ТЕКСТ_" * 20
    store.add_or_update("notes", long_fact, category="general", importance=2)

    # Добавляем факт со сверхдлинным ключом (edge case обрезки и эллипсиса)
    long_key = "a" * 120
    store.add_or_update(long_key, "Факт со сверхдлинным ключом", category="projects", importance=3)

    prompt = store.get_prompt_context()
    assert "ДОЛГОВРЕМЕННАЯ ПАМЯТЬ — СОХРАНЁННЫЕ ФАКТЫ О D" in prompt
    assert "Личность D" in prompt
    assert "• [user_id]: D" in prompt
    assert "Окружение / Софт" in prompt
    # Длинный факт должен быть обрезан с троеточием
    assert "..." in prompt
    # Сверхдлинный ключ должен быть усечен
    assert len(prompt) < 1500

    # Проверка строгого лимита при малом бюджете токенов
    small_prompt = store.get_prompt_context(max_tokens_budget=30)
    assert len(small_prompt) <= 300


@pytest.mark.asyncio
async def test_memory_workbench_tools(tmp_path, monkeypatch):
    test_file = tmp_path / "tools_wb_memory.json"
    store = MemoryStore(file_path=test_file)
    monkeypatch.setattr("capri.tools.memory.MEMORY_FILE", test_file)
    monkeypatch.setattr("capri.tools.memory._shared_store", store)

    # 1. remember с категорией и важностью
    msg_add = await remember("audio", "Fish Audio Neural TTS", category="environment", importance=5)
    assert "Запомнила" in msg_add
    assert "Окружение / Софт" in msg_add

    # 2. recall_memory
    recalled = await recall_memory("audio")
    assert "• [audio]: Fish Audio Neural TTS" in recalled

    # 3. update_memory_entry
    msg_upd = await update_memory_entry("audio", "Fish Audio S1 v2.0", importance=5)
    assert "обновлен" in msg_upd
    assert store.entries["audio"].fact == "Fish Audio S1 v2.0"

    # 4. list_memory_categories
    cats = await list_memory_categories()
    assert "environment" in cats
    assert "identity" in cats

    # 5. get_memory_workbench_stats
    stats = await get_memory_workbench_stats()
    assert "MEMORY WORKBENCH" in stats
    assert "audio" in stats

    # 6. forget_memory
    f_res = await forget_memory("audio")
    assert "удалён" in f_res
    assert len(store.entries) == 0


def test_agent_memory_store_integration(tmp_path, monkeypatch):
    test_file = tmp_path / "agent_wb_memory.json"
    monkeypatch.setattr("capri.core.memory.MEMORY_FILE", test_file)

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    agent = CapriAgent(cfg)

    assert hasattr(agent, "memory_store")
    assert agent.memory_store is get_memory_store()

    # Добавляем факт
    agent.memory_store.add_or_update("codename", "Relic Engram", category="projects", importance=5)

    full_prompt = agent._build_full_prompt()
    assert "ДОЛГОВРЕМЕННАЯ ПАМЯТЬ" in full_prompt
    assert "Relic Engram" in full_prompt


def test_cyberdeck_memory_workbench_commands(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    test_file = tmp_path / "deck_wb_memory.json"
    monkeypatch.setattr("capri.core.memory.MEMORY_FILE", test_file)

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True), patch("capri.ui.cyberdeck.save_config", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.memory_store = MemoryStore(test_file)

        # 1. /memory (карточка дашборда)
        deck._handle_command("/memory")

        # 2. /memory add
        deck.agent.memory_store.add_or_update("stack", "Python, PySide6, SQLite", category="preferences", importance=5, tags=["dev"])
        assert "stack" in deck.agent.memory_store.entries
        assert deck.agent.memory_store.entries["stack"].fact == "Python, PySide6, SQLite"

        # 3. /memory edit - должен сохранить категорию, приоритет и теги!
        deck._handle_command("/memory edit stack Python 3.12, PySide6, SQLite")
        updated_e = deck.agent.memory_store.entries["stack"]
        assert updated_e.fact == "Python 3.12, PySide6, SQLite"
        assert updated_e.category == "preferences"
        assert updated_e.importance == 5
        assert updated_e.tags == ["dev"]

        # 4. /memory search
        deck._handle_command("/memory search sqlite")

        # 5. /memory cat
        deck._handle_command("/memory cat general")

        # 6. /forget
        deck._handle_command("/forget stack")
        assert "stack" not in deck.agent.memory_store.entries

        # 7. /memory export (изолируем в tmp_path и XDG_CONFIG_HOME)
        import os
        with patch("pathlib.Path.home", return_value=tmp_path), patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(tmp_path / ".config")}
        ):
            deck._handle_command("/memory export")
            export_files = list((tmp_path / ".config" / "capri" / "backups").glob("memory_export_*.json"))
            assert len(export_files) == 1

        # 8. /memory clear
        deck._handle_command("/memory clear")
        assert len(deck.agent.memory_store.entries) == 0

        deck.close()
