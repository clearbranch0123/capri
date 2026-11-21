from __future__ import annotations

import asyncio
from typing import Optional
from .registry import tool
from ..core.context import ContextStore, CONTEXT_FILE

_shared_store: Optional[ContextStore] = None


def set_shared_context_store(store: ContextStore):
    """Связывает синглтон инструментов с активным хранилищем CapriAgent."""
    global _shared_store
    _shared_store = store


def get_context_store() -> ContextStore:
    global _shared_store
    if _shared_store is None:
        _shared_store = ContextStore(CONTEXT_FILE)
    return _shared_store


@tool(description="Просмотреть текущий рабочий контекст, открытые задачи, принятые решения и следующие шаги.")
async def get_work_context() -> str:
    store = get_context_store()
    card = store.get_status_card()
    return card


@tool(description="Добавить элемент в рабочий контекст для сохранения непрерывности работы между сессиями. kind: 'thread' (задача/ветка), 'decision' (решение), 'next_step' (следующий шаг), 'note' (заметка). content: описание. project: опциональное имя проекта (по умолчанию активный проект).")
async def add_work_item(kind: str, content: str, project: Optional[str] = None) -> str:
    store = get_context_store()
    clean_kind = kind.lower().strip()
    if clean_kind in ("step", "next_step", "nextstep"):
        clean_kind = "next_step"
    elif clean_kind in ("task", "thread"):
        clean_kind = "thread"
    elif clean_kind not in ("decision", "note"):
        clean_kind = "thread"
    clean_proj = project.strip() if project and project.strip() else None
    item = await asyncio.to_thread(store.add_item, clean_kind, content, clean_proj, "agent")
    kind_ru = {
        "thread": "Задача",
        "decision": "Решение",
        "next_step": "Следующий шаг",
        "note": "Заметка",
    }.get(clean_kind, "Элемент")
    return f"{kind_ru} зафиксирована в рабочем контексте: [#{item.id}] '{item.content}' (проект: {item.project})."


@tool(description="Отметить задачу или следующий шаг в рабочем контексте как выполненные. item_id: ID элемента (например: 'ctx-12345-1').")
async def resolve_work_item(item_id: str) -> str:
    store = get_context_store()
    ok = await asyncio.to_thread(store.resolve_item, item_id)
    if ok:
        return f"Элемент контекста [#{item_id}] успешно отмечен как выполненный."
    return f"Элемент с ID '{item_id}' не найден в активном контексте."
