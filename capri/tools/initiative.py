from __future__ import annotations

import asyncio
from typing import Optional
from .registry import tool
from ..core.initiative import InitiativeEngine, INITIATIVE_FILE

_shared_engine: Optional[InitiativeEngine] = None


def set_shared_initiative_engine(engine: InitiativeEngine):
    """Связывает синглтон инструментов с активным движком CapriAgent."""
    global _shared_engine
    _shared_engine = engine


def get_initiative_engine() -> InitiativeEngine:
    global _shared_engine
    if _shared_engine is None:
        _shared_engine = InitiativeEngine(INITIATIVE_FILE)
    return _shared_engine


@tool(description="Получить текущий статус автономного движка инициативы Capri (Initiative Engine), уровень активности, кулдауны и историю.")
async def get_initiative_status() -> str:
    engine = get_initiative_engine()
    card = engine.get_status_card()
    return card


@tool(description="Зафиксировать проактивное предложение или рекомендацию для D (будет озвучено или предложено в диалоге). kind: тип ('task_nudge', 'focus_break', 'action_proposal'). suggestion: текст идеи/предложения. context_ref: опциональный ID связанной задачи.")
async def propose_action(kind: str, suggestion: str, context_ref: Optional[str] = None) -> str:
    engine = get_initiative_engine()
    clean_kind = kind.lower().strip() if kind else "action_proposal"
    if clean_kind not in ("task_nudge", "focus_break", "idle_checkin", "action_proposal"):
        clean_kind = "action_proposal"

    await asyncio.to_thread(engine.add_proposal, clean_kind, suggestion, context_ref)
    return f"Проактивное предложение зарегистрировано в пуле инициативы: «{suggestion.strip()}»."


@tool(description="Настроить параметры самостоятельной инициативы Capri. enabled: включить (true) или выключить (false). level: уровень ('subtle', 'normal', 'proactive'). cooldown_minutes: кулдаун между инициативами в минутах.")
async def configure_initiative(
    enabled: Optional[bool] = None,
    level: Optional[str] = None,
    cooldown_minutes: Optional[int] = None,
) -> str:
    engine = get_initiative_engine()
    changes = []
    if enabled is not None:
        await asyncio.to_thread(engine.set_enabled, enabled)
        changes.append(f"активность -> {'ON' if enabled else 'OFF'}")
    if level is not None:
        ok = await asyncio.to_thread(engine.set_level, level)
        if ok:
            changes.append(f"уровень -> {level.upper()}")
    if cooldown_minutes is not None:
        await asyncio.to_thread(engine.set_cooldown, cooldown_minutes)
        changes.append(f"кулдаун -> {cooldown_minutes} мин")

    if not changes:
        return "Параметры инициативы не изменились. Доступно: enabled, level ('subtle'|'normal'|'proactive'), cooldown_minutes."
    return f"Параметры инициативы обновлены: {', '.join(changes)}."
