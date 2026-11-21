import pytest
import asyncio
from capri.tools.timers import set_timer, list_timers, cancel_timer, get_active_timers, _active_timers


@pytest.mark.asyncio
async def test_set_and_list_timers():
    _active_timers.clear()
    res = await set_timer(60, "чай")
    assert "установлен" in res
    assert len(get_active_timers()) == 1

    listing = await list_timers()
    assert "чай" in listing
    assert "осталось" in listing

    # Cancel
    cancel_res = await cancel_timer("чай")
    assert "отменен" in cancel_res
    assert len(get_active_timers()) == 0


@pytest.mark.asyncio
async def test_cancel_all_timers():
    _active_timers.clear()
    await set_timer(120, "кофе")
    await set_timer(180, "пицца")
    assert len(get_active_timers()) == 2

    cancel_res = await cancel_timer("all")
    assert "Отменено таймеров: 2" in cancel_res
    assert len(get_active_timers()) == 0


@pytest.mark.asyncio
async def test_timer_invalid_duration():
    res = await set_timer(0, "тест")
    assert "больше нуля" in res

    res2 = await set_timer(100000, "тест")
    assert "не может быть больше 24 часов" in res2
