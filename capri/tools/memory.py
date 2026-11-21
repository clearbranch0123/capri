from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional, List
from .registry import tool
from ..core.memory import (
    MemoryStore,
    MemoryEntry,
    MEMORY_FILE,
    CATEGORIES,
    CATEGORY_LABELS,
    RETENTION_MODES,
    contains_secret_or_sensitive_memory,
)

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
    """Формирует строгий структурированный блок фактов о пользователе для системного промпта."""
    store = get_memory_store()
    return store.get_prompt_context()


# ── ИНСТРУМЕНТЫ АГЕНТА (LLM TOOLS) ─────────────────────────────────────────

@tool(description="Явно сохранить факт в память по прямой просьбе пользователя (требует подтверждения политикой безопасности; для фоновых наблюдений используй memory_candidates). key: ключ, fact: текст факта, category: 'identity'|'preferences'|'environment'|'projects'|'constraints'|'general', importance: 1..5, retention: 'long_term'|'ephemeral'.")
async def remember(
    key: str,
    fact: str = "",
    value: str = "",
    text: str = "",
    category: str = "general",
    importance: int = 3,
    retention: str = "long_term",
) -> str:
    actual_fact = (fact or value or text).strip()
    key_clean = key.lower().strip()
    if not key_clean or not actual_fact:
        return "Укажи ключ и текст факта для сохранения."

    if (
        contains_secret_or_sensitive_memory(key_clean)
        or contains_secret_or_sensitive_memory(actual_fact)
        or contains_secret_or_sensitive_memory(f"{key_clean}={actual_fact}")
    ):
        return "Отклонено: пароли, API-ключи, токены и другие секреты не сохраняются в память."

    store = get_memory_store()
    clean_cat = category.lower().strip() if category else "general"
    if clean_cat not in CATEGORIES:
        clean_cat = "general"
    clean_ret = (retention or "long_term").lower().strip()
    if clean_ret not in RETENTION_MODES:
        clean_ret = "long_term"

    entry = await asyncio.to_thread(
        store.add_or_update,
        key_clean,
        actual_fact,
        category=clean_cat,
        importance=max(1, min(5, int(importance))),
        source="agent",
        retention=clean_ret,
    )
    cat_ru = CATEGORY_LABELS.get(entry.category, entry.category)
    ret_ru = ", временно 72ч" if entry.retention == "ephemeral" else ""
    return f"Запомнила: [{entry.key}] -> '{entry.fact}' (категория: {cat_ru}, важность: {'★' * entry.importance}{ret_ru})."


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


@tool(description="Явно обновить существующий факт в долговременной памяти по просьбе пользователя (требует подтверждения политикой безопасности). key: ключ факта, new_fact: обновленный текст, category: опциональная категория, importance: опциональная важность 1-5.")
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

    if (
        contains_secret_or_sensitive_memory(key_clean)
        or contains_secret_or_sensitive_memory(new_fact)
        or contains_secret_or_sensitive_memory(f"{key_clean}={new_fact}")
    ):
        return "Отклонено: пароли, API-ключи, токены и другие секреты не сохраняются в память."

    old = store.entries[key_clean]
    cat = category if category is not None else old.category
    imp = importance if importance is not None else old.importance
    entry = await asyncio.to_thread(
        store.add_or_update,
        key_clean,
        new_fact,
        category=cat,
        importance=imp,
        source="agent",
        retention=old.retention,
    )
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
    from ..core.config import load_config
    store = get_memory_store()
    u_name = (getattr(getattr(load_config(), "companion", None), "user_name", "") or "").strip() or "User"
    card = store.get_stats_card(user_name=u_name)
    return card
