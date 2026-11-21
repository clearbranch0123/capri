from __future__ import annotations

import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from PySide6.QtWidgets import QApplication
from capri.core.config import AppConfig
from capri.core.context import ContextStore, ContextItem
from capri.tools.context import get_work_context, add_work_item, resolve_work_item, get_context_store
from capri.core.agent import CapriAgent
from capri.ui.cyberdeck import CapriCyberdeckWindow


@pytest.fixture
def clean_context_store(tmp_path):
    test_file = tmp_path / "test_context.json"
    store = ContextStore(file_path=test_file)
    store.clear_context()
    return store


def test_context_store_crud(clean_context_store):
    store = clean_context_store
    assert store.items == []

    # 1. Добавление элементов
    t1 = store.add_item("thread", "Отладка сетевого шлюза", project="capri")
    d1 = store.add_item("decision", "Использовать SQLite для сессий", project="capri")
    ns1 = store.add_item("next_step", "Прогнать pytest на всех модулях", project="capri")
    ns2 = store.add_item("step", "Алиас команды step превращается в next_step", project="capri")

    assert len(store.items) == 4
    assert t1.kind == "thread"
    assert t1.status == "active"
    assert d1.kind == "decision"
    assert ns1.kind == "next_step"
    assert ns2.kind == "next_step"

    # 2. Выборка активных элементов
    active_threads = store.get_active_items(kind="thread")
    assert len(active_threads) == 1
    assert active_threads[0].id == t1.id

    # 3. Закрытие элемента (resolve)
    assert store.resolve_item(t1.id)
    assert t1.status == "resolved"
    assert len(store.get_active_items(kind="thread")) == 0

    # 4. Замена элемента (supersede)
    new_d = store.supersede_item(d1.id, "Использовать SQLite с WAL-режимом для сессий")
    assert new_d is not None
    assert d1.status == "superseded"
    assert new_d.content == "Использовать SQLite с WAL-режимом для сессий"

    # 5. Удаление элемента
    assert store.delete_item(ns1.id)
    assert len([it for it in store.items if it.id == ns1.id]) == 0


def test_context_store_persistence(tmp_path):
    test_file = tmp_path / "test_persist.json"
    store1 = ContextStore(file_path=test_file)
    store1.set_active_project("cyberdeck_v2")
    store1.set_session_summary("Завершена настройка звука PipeWire")
    store1.add_item("thread", "Проверить задержку TTS", project="cyberdeck_v2")
    store1.add_item("decision", "Выбран Fish Audio как основной голос", project="cyberdeck_v2")

    # Создаём новый экземпляр, читающий тот же файл
    store2 = ContextStore(file_path=test_file)
    assert store2.active_project == "cyberdeck_v2"
    assert store2.last_session_summary == "Завершена настройка звука PipeWire"
    assert len(store2.items) == 2
    assert store2.items[0].content == "Проверить задержку TTS"


def test_bounded_prompt_context(clean_context_store):
    store = clean_context_store
    store.set_active_project("niri_shell")
    store.set_session_summary("Обсуждали Wayland IPC")

    # Добавляем много элементов, чтобы проверить ограничение вывода
    for i in range(10):
        store.add_item("thread", f"Задача #{i}")
        store.add_item("decision", f"Решение #{i}")
        store.add_item("next_step", f"Шаг #{i}")

    prompt = store.get_prompt_context()
    assert "[РАБОЧИЙ КОНТЕКСТ И НЕПРЕРЫВНОСТЬ ДИАЛОГА]" in prompt
    assert "niri_shell" in prompt
    assert "Обсуждали Wayland IPC" in prompt

    # Проверяем, что вывод ограничен последними элементами и не раздувается
    assert "Задача #9" in prompt
    assert "Решение #9" in prompt
    assert "Шаг #9" in prompt
    # Элемент #0 не должен присутствовать в bounded-списке
    assert "Задача #0" not in prompt

    # Проверяем ограничение длины слишком длинной записи (clipping)
    long_task = store.add_item("thread", "ОЧЕНЬ_ДЛИННЫЙ_ТЕКСТ_" * 30)
    prompt_clipped = store.get_prompt_context()
    assert "..." in prompt_clipped
    assert len(prompt_clipped) < 4000


def test_unique_ids_on_burst_exceeding_capacity(clean_context_store):
    store = clean_context_store
    added_ids = []
    # Добавляем 60 элементов подряд (превышая лимит в 50)
    for i in range(60):
        item = store.add_item("thread", f"Потоковая задача {i}")
        added_ids.append(item.id)

    # Все сгенерированные ID должны быть строго уникальными
    assert len(added_ids) == 60
    assert len(set(added_ids)) == 60
    assert len(store.items) == 50


def test_agent_working_context_continuity(tmp_path, monkeypatch):
    test_file = tmp_path / "agent_context.json"
    monkeypatch.setattr("capri.core.context.CONTEXT_FILE", test_file)

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    agent = CapriAgent(cfg)
    assert get_context_store() is agent.context_store

    # Добавляем задачу в рабочий контекст
    agent.context_store.add_item("thread", "Починить кликабельность кнопок", project="capri")
    agent.context_store.add_item("decision", "Принято решение о разделении сессий и памяти")

    # Проверяем, что контекст попадает в системный промпт
    full_prompt = agent._build_full_prompt()
    assert "РАБОЧИЙ КОНТЕКСТ" in full_prompt
    assert "Починить кликабельность кнопок" in full_prompt

    # Начинаем НОВУЮ сессию (/new)
    new_sess = agent.start_new_session(title="Чистая сессия")
    assert agent.current_session_id == new_sess.id

    # Рабочий контекст ДОЛЖЕН СОХРАНИТЬСЯ в новой сессии!
    new_prompt = agent._build_full_prompt()
    assert "Починить кликабельность кнопок" in new_prompt
    assert "Принято решение о разделении сессий и памяти" in new_prompt


@pytest.mark.asyncio
async def test_context_tools(tmp_path, monkeypatch):
    test_file = tmp_path / "tools_context.json"
    monkeypatch.setattr("capri.core.context.CONTEXT_FILE", test_file)
    monkeypatch.setattr("capri.tools.context._shared_store", ContextStore(test_file))

    # 1. Добавление через инструмент
    add_res = await add_work_item("thread", "Тестовая задача из инструмента", project="testing")
    assert "Задача зафиксирована" in add_res

    # 2. Просмотр через инструмент
    view_res = await get_work_context()
    assert "Тестовая задача из инструмента" in view_res

    # 3. Закрытие через инструмент
    item_id = add_res.split("[#")[1].split("]")[0]
    resolve_res = await resolve_work_item(item_id)
    assert "успешно отмечен как выполненный" in resolve_res


def test_cyberdeck_context_commands(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    test_file = tmp_path / "deck_context.json"
    monkeypatch.setattr("capri.core.context.CONTEXT_FILE", test_file)

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.context_store = ContextStore(test_file)

        # 1. /context (просмотр)
        deck._handle_command("/context")

        # 2. /context add thread
        deck._handle_command("/context add thread Разработка Vision модуля")
        assert len(deck.agent.context_store.get_active_items()) == 1
        assert deck.agent.context_store.items[0].content == "Разработка Vision модуля"

        # 3. /context resolve
        t_id = deck.agent.context_store.items[0].id
        deck._handle_command(f"/context resolve {t_id}")
        assert len(deck.agent.context_store.get_active_items()) == 0

        # 4. /context clear
        deck._handle_command("/context clear")
        assert len(deck.agent.context_store.items) == 0

        deck.close()
