import pytest
from pathlib import Path
from capri.tools.memory import (
    add_memory,
    load_memories,
    delete_memory,
    clear_all_memories,
    get_memory_summary_prompt,
    remember,
    recall_memory,
)


@pytest.fixture(autouse=True)
def clean_memory_file(tmp_path, monkeypatch):
    test_file = tmp_path / "test_memory.json"
    monkeypatch.setattr("capri.tools.memory.MEMORY_FILE", test_file)
    clear_all_memories()


def test_add_and_load_memory():
    assert add_memory("любимый_язык", "Python и Rust")
    data = load_memories()
    assert "любимый_язык" in data
    assert data["любимый_язык"]["fact"] == "Python и Rust"


def test_delete_and_clear_memory():
    add_memory("ключ1", "факт 1")
    add_memory("ключ2", "факт 2")
    assert delete_memory("ключ1")
    assert not delete_memory("несуществующий")
    data = load_memories()
    assert "ключ1" not in data
    assert "ключ2" in data

    clear_all_memories()
    assert load_memories() == {}


def test_summary_prompt_formatting():
    add_memory("тема", "киберпанк")
    summary = get_memory_summary_prompt()
    assert "ДОЛГОВРЕМЕННАЯ ПАМЯТЬ" in summary
    assert "• [тема]: киберпанк" in summary


@pytest.mark.asyncio
async def test_remember_and_recall_tools():
    msg = await remember("проект", "Capri AI Companion")
    assert "Запомнила" in msg

    all_mem = await recall_memory("")
    assert "• [проект]: Capri AI Companion" in all_mem

    filtered = await recall_memory("проект")
    assert "• [проект]: Capri AI Companion" in filtered

    empty_filtered = await recall_memory("неизвестно")
    assert "не найдено" in empty_filtered
