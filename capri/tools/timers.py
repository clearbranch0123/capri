from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from .registry import tool
from ..ui.client import notify_avatar

_active_timers: list[dict] = []


async def _timer_worker(seconds: int, label: str):
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return

    # 1. Desktop notification
    if shutil.which("notify-send"):
        try:
            await asyncio.create_subprocess_exec(
                "notify-send",
                "Капри: Таймер!",
                f"Время вышло: {label} ({seconds} сек.)",
                "-u", "critical",
                "-t", "10000",
            )
        except Exception:
            pass

    # 2. Avatar reaction
    notify_avatar("playful", f"⏰ Таймер: {label}!")

    # 3. Audio beep or notification sound
    try:
        candidate_sounds = (
            "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
            "/usr/share/sounds/freedesktop/stereo/complete.oga",
            "/usr/share/sounds/freedesktop/stereo/bell.oga",
        )
        for sound in candidate_sounds:
            if Path(sound).exists():
                if shutil.which("paplay"):
                    await asyncio.create_subprocess_exec(
                        "paplay", sound,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    break
                elif shutil.which("mpv"):
                    await asyncio.create_subprocess_exec(
                        "mpv", "--no-video", sound,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    break
    except Exception:
        pass


def get_active_timers() -> list[dict]:
    """Возвращает список активных не завершенных таймеров с подсчетом оставшегося времени."""
    now = time.time()
    active = []
    # очищаем завершенные
    for item in list(_active_timers):
        if not item["task"].done():
            remaining = max(0, int(item["seconds"] - (now - item["start"])))
            active.append({
                "label": item["label"],
                "seconds": item["seconds"],
                "remaining": remaining,
                "task": item["task"],
                "start": item["start"],
            })
    return active


@tool(description="Поставить таймер в секундах. seconds: секунды (300 = 5 мин), label: событие ('разминка').")
async def set_timer(seconds: int, label: str = "таймер") -> str:
    try:
        seconds = int(float(str(seconds)))
    except Exception:
        return "Укажи количество секунд числом (например, 300 для 5 минут)."

    if seconds <= 0:
        return "Время таймера должно быть больше нуля секунд."
    if seconds > 86400:
        return "Таймер не может быть больше 24 часов."

    task = asyncio.create_task(_timer_worker(seconds, label))
    _active_timers.append({"task": task, "label": label, "seconds": seconds, "start": time.time()})

    minutes = seconds // 60
    rem_secs = seconds % 60
    time_str = f"{minutes} мин {rem_secs} сек" if minutes > 0 else f"{seconds} сек"
    return f"Таймер на {time_str} ('{label}') установлен и тикает в фоне."


@tool(description="Список активных таймеров и оставшееся время.")
async def list_timers() -> str:
    active = get_active_timers()
    if not active:
        return "Активных таймеров сейчас нет."
    lines = ["Активные таймеры:"]
    for idx, t in enumerate(active, 1):
        rem_m, rem_s = divmod(t["remaining"], 60)
        lines.append(f"{idx}. «{t['label']}» — осталось {rem_m} мин {rem_s} сек")
    return "\n".join(lines)


@tool(description="Отменить таймер по названию/номеру или 'all' для всех.")
async def cancel_timer(query: str = "") -> str:
    active = get_active_timers()
    if not active:
        return "Нет активных таймеров для отмены."

    query_clean = query.strip().lower()
    if not query_clean or query_clean in ("all", "все", "всё"):
        count = len(active)
        for t in active:
            t["task"].cancel()
        _active_timers.clear()
        return f"Отменено таймеров: {count}."

    cancelled = []
    for idx, t in enumerate(active, 1):
        if str(idx) == query_clean or query_clean in t["label"].lower():
            t["task"].cancel()
            cancelled.append(t["label"])
            # удаляем из глобального списка
            for item in list(_active_timers):
                if item["task"] is t["task"]:
                    _active_timers.remove(item)

    if cancelled:
        return f"Таймер(ы) отменен(ы): {', '.join(cancelled)}."
    return f"Таймер с описанием '{query}' не найден среди активных."
