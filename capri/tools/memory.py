from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional, List
from .registry import tool
from ..core.memory import MemoryStore, MemoryEntry, MEMORY_FILE, CATEGORIES, CATEGORY_LABELS

_shared_store: Optional[MemoryStore] = None


def set_shared_memory_store(store: MemoryStore):
    """Связывает синглтон инструментов с активным хранилищем CapriAgent."""
    global _shared_store
    _shared_store = store


def get_memory_store() -> MemoryStore:
    global _shared_store
    if _shared_store is None:
        _shared_store = MemoryStore(MEMORY_FILE)
    return _shared_store


# ── СОВМЕСТИМОСТЬ С ПРЕЖНИМ API ───────────────────────────────────────────

def load_memories() -> Dict[str, Any]:
    """Обратная совместимость: возвращает словарь в формате прежнего memory.json."""
    store = get_memory_store()
    return {k: {"fact": v.fact, "updated_at": v.updated_at, "category": v.category, "importance": v.importance} for k, v in store.entries.items()}


def save_memories(data: Dict[str, Any]):
    """Обратная совместимость: заменяет хранилище памяти переданным словарем."""
    store = get_memory_store()
    store.replace_all(data)


def add_memory(key: str, fact: str, category: str = "general", importance: int = 3) -> bool:
    key_clean = key.lower().strip()
    fact_clean = fact.strip()
    if not key_clean or not fact_clean:
        return False
    store = get_memory_store()
    store.add_or_update(key_clean, fact_clean, category=category, importance=importance, source="user")
    return True


def delete_memory(key: str) -> bool:
    key_clean = key.lower().strip()
    store = get_memory_store()
    return store.delete(key_clean)


def clear_all_memories(category: Optional[str] = None) -> bool:
    store = get_memory_store()
    store.clear(category=category)
    return True


def get_memory_summary_prompt() -> str:
    """Формирует строгий структурированный блок фактов о D для системного промпта."""
    store = get_memory_store()
    return store.get_prompt_context()


# ── ИНСТРУМЕНТЫ АГЕНТА (LLM TOOLS) ─────────────────────────────────────────

@tool(description="Запомнить факт о пользователе D в долговременную память. key: тема/ключ, fact: текст факта, category: категория ('identity', 'preferences', 'environment', 'projects', 'constraints', 'general'), importance: важность от 1 до 5 (по умолчанию 3).")
async def remember(
    key: str,
    fact: str = "",
    value: str = "",
    text: str = "",
    category: str = "general",
    importance: int = 3,
) -> str:
    actual_fact = (fact or value or text).strip()
    key_clean = key.lower().strip()
    if not key_clean or not actual_fact:
        return "Укажи ключ и текст факта для сохранения."

    store = get_memory_store()
    clean_cat = category.lower().strip() if category else "general"
    if clean_cat not in CATEGORIES:
        clean_cat = "general"

    entry = await asyncio.to_thread(
        store.add_or_update,
        key_clean,
        actual_fact,
        category=clean_cat,
        importance=max(1, min(5, int(importance))),
        source="agent",
    )
    cat_ru = CATEGORY_LABELS.get(entry.category, entry.category)
    return f"Запомнила: [{entry.key}] -> '{entry.fact}' (категория: {cat_ru}, важность: {'★' * entry.importance})."


@tool(description="Вспомнить факты из долговременной памяти. query: поисковое слово (пусто = всё), category: опциональный фильтр по категории ('identity', 'preferences', 'environment', 'projects', 'constraints', 'general').")
async def recall_memory(query: str = "", category: str = "") -> str:
    store = get_memory_store()
    cat_filter = category.lower().strip() if category else None
    results = await asyncio.to_thread(store.search, query=query, category=cat_filter)
    if not results:
        if query or category:
            return f"В памяти ничего не найдено по запросу '{query}'{f' в категории [{category}]' if category else ''}."
        return "В долговременной памяти пока ничего не сохранено."

    matches = []
    for it in results:
        cat_suffix = f" [{CATEGORY_LABELS.get(it.category, it.category)}]" if it.category != "general" else ""
        matches.append(f"• [{it.key}]: {it.fact}{cat_suffix}")

    return "Сохранённые воспоминания:\n" + "\n".join(matches)


@tool(description="Удалить конкретный факт из долговременной памяти по ключу key.")
async def forget_memory(key: str) -> str:
    store = get_memory_store()
    key_clean = key.lower().strip()
    deleted = await asyncio.to_thread(store.delete, key_clean)
    if deleted:
        return f"Факт [{key_clean}] удалён из памяти."
    return f"Ключ [{key_clean}] не найден в памяти."


@tool(description="Обновить или изменить существующий факт в долговременной памяти. key: ключ факта, new_fact: обновленный текст, category: опциональная категория, importance: опциональная важность 1-5.")
async def update_memory_entry(
    key: str,
    new_fact: str,
    category: Optional[str] = None,
    importance: Optional[int] = None,
) -> str:
    store = get_memory_store()
    store._sync_disk()
    key_clean = key.lower().strip()
    if key_clean not in store.entries:
        return f"Факт с ключом [{key_clean}] не существует в памяти. Используй remember для добавления."

    old = store.entries[key_clean]
    cat = category if category is not None else old.category
    imp = importance if importance is not None else old.importance
    entry = await asyncio.to_thread(store.add_or_update, key_clean, new_fact, category=cat, importance=imp, source="agent")
    return f"Факт [{entry.key}] обновлен: '{entry.fact}' (категория: {entry.category}, важность: {'★' * entry.importance})."


@tool(description="Получить список всех доступных категорий памяти и описание их назначения.")
async def list_memory_categories() -> str:
    store = get_memory_store()
    lines = ["Категории долговременной памяти Capri (Memory Workbench):"]
    for cat in CATEGORIES:
        lbl = CATEGORY_LABELS.get(cat, cat)
        cnt = sum(1 for e in store.entries.values() if e.category == cat)
        lines.append(f"• {cat} ({lbl}) — {cnt} записей")
    return "\n".join(lines)


@tool(description="Получить общую статистику и дашборд хранилища памяти Memory Workbench.")
async def get_memory_workbench_stats() -> str:
    store = get_memory_store()
    card = store.get_stats_card()
    return card
