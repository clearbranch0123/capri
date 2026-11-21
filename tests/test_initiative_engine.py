from __future__ import annotations

import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from PySide6.QtWidgets import QApplication
from capri.core.config import AppConfig
from capri.core.context import ContextStore
from capri.core.emotions import EmotionEngine
from capri.core.initiative import InitiativeEngine, InitiativeRecord, InitiativeSettings
from capri.tools.initiative import (
    get_initiative_status,
    propose_action,
    configure_initiative,
    get_initiative_engine,
    set_shared_initiative_engine,
)
from capri.core.agent import CapriAgent
from capri.ui.cyberdeck import CapriCyberdeckWindow


@pytest.fixture
def clean_initiative_engine(tmp_path):
    test_file = tmp_path / "test_initiative.json"
    engine = InitiativeEngine(file_path=test_file)
    engine.history = []
    engine.pending_proposals = []
    engine.last_initiative_time = 0.0
    engine.last_user_activity = time.time()
    engine.save()
    return engine


def test_initiative_engine_settings_and_persistence(tmp_path):
    test_file = tmp_path / "persist_initiative.json"
    engine1 = InitiativeEngine(file_path=test_file)
    engine1.set_enabled(True)
    engine1.set_level("proactive")
    engine1.set_cooldown(15)
    engine1.add_proposal("action_proposal", "Тестовое предложение для D")

    # Проверяем эффективные кулдауны
    assert engine1.settings.level == "proactive"
    assert engine1.get_effective_cooldown() <= 720  # 12 мин для proactive
    assert engine1.get_effective_idle_threshold() <= 420  # 7 мин для proactive

    # Создаем второй инстанс, читающий тот же файл
    engine2 = InitiativeEngine(file_path=test_file)
    assert engine2.settings.enabled is True
    assert engine2.settings.level == "proactive"
    assert len(engine2.pending_proposals) == 1
    assert engine2.pending_proposals[0]["suggestion"] == "Тестовое предложение для D"


def test_initiative_trigger_conditions(clean_initiative_engine):
    engine = clean_initiative_engine
    now = 1700000000.0  # фиксированное время

    # 1. Отключен
    engine.set_enabled(False)
    can, reason = engine.can_trigger(now=now)
    assert not can
    assert "отключен" in reason.lower()

    engine.set_enabled(True)

    # 2. Подавление режимом focus
    can, reason = engine.can_trigger(companion_mode="focus", now=now)
    assert not can
    assert "focus" in reason.lower()

    # 3. Подавление режимом quiet
    can, reason = engine.can_trigger(companion_mode="quiet", now=now)
    assert not can
    assert "quiet" in reason.lower()

    # 4. Подавление стадией estranged
    can, reason = engine.can_trigger(companion_mode="normal", relationship_stage="estranged", now=now)
    assert not can
    assert "отчуждения" in reason.lower()

    # Отключаем ночные часы для проверки порогов активности и кулдауна
    engine.settings.quiet_hours_enabled = False

    # 5. Пользователь был активен недавно (idle threshold)
    engine.last_user_activity = now - 100.0  # всего 100 секунд назад
    engine.last_initiative_time = now - 5000.0
    can, reason = engine.can_trigger(companion_mode="normal", relationship_stage="trusted_partner", now=now)
    assert not can
    assert "активен недавно" in reason.lower()

    # 6. Кулдаун между инициативами
    engine.last_user_activity = now - 2000.0  # давно
    engine.last_initiative_time = now - 200.0  # инициатива была 200с назад
    can, reason = engine.can_trigger(companion_mode="normal", relationship_stage="trusted_partner", now=now)
    assert not can
    assert "кулдаун" in reason.lower()

    # 7. Все условия выполнены -> ГОТОВ!
    engine.last_user_activity = now - 2000.0
    engine.last_initiative_time = now - 3000.0
    can, reason = engine.can_trigger(companion_mode="normal", relationship_stage="trusted_partner", now=now)
    assert can


def test_task_nudge_evaluation(clean_initiative_engine, tmp_path):
    engine = clean_initiative_engine
    now = 1700000000.0
    engine.settings.quiet_hours_enabled = False
    engine.last_user_activity = now - 2000.0
    engine.last_initiative_time = now - 3000.0

    ctx_file = tmp_path / "nudge_context.json"
    ctx = ContextStore(file_path=ctx_file)
    ctx.add_item("thread", "Проверить сетевой стек")
    ctx.add_item("next_step", "Написать юнит-тесты на InitiativeEngine")

    rec = engine.evaluate(companion_mode="normal", context_store=ctx, now=now)
    assert rec is not None
    assert rec.kind == "task_nudge"
    # Должен предпочесть next_step перед thread
    assert "Написать юнит-тесты" in rec.content
    assert rec.context_ref is not None


def test_focus_break_evaluation(clean_initiative_engine):
    engine = clean_initiative_engine
    now = 1700000000.0
    engine.settings.quiet_hours_enabled = False
    engine.last_user_activity = now - 2000.0
    engine.last_initiative_time = now - 3000.0
    # Сессия длится 3 часа (10800 секунд)
    engine.session_start_time = now - 10800.0

    rec = engine.evaluate(companion_mode="normal", now=now)
    assert rec is not None
    assert rec.kind == "focus_break"
    assert ("перерыв" in rec.content.lower() or "пауз" in rec.content.lower() or "отдых" in rec.content.lower())


def test_session_start_resets_after_long_app_downtime(tmp_path):
    test_file = tmp_path / "downtime_initiative.json"
    engine1 = InitiativeEngine(file_path=test_file)
    # Имитируем старую сессию 5 часов назад
    engine1.session_start_time = time.time() - 18000
    engine1.last_user_activity = time.time() - 14400  # 4 часа назад
    engine1.save()

    # Новый запуск приложения
    engine2 = InitiativeEngine(file_path=test_file)
    # Время сессии должно быть сброшено к текущему, а не унаследовано старое
    assert time.time() - engine2.session_start_time < 5.0


def test_idle_checkin_evaluation(clean_initiative_engine):
    engine = clean_initiative_engine
    now = 1700000000.0
    engine.settings.quiet_hours_enabled = False
    engine.last_user_activity = now - 3000.0
    engine.last_initiative_time = now - 4000.0
    engine.session_start_time = now - 600.0  # свежая сессия

    rec = engine.evaluate(companion_mode="normal", now=now)
    assert rec is not None
    assert rec.kind == "idle_checkin"
    assert len(rec.content) > 10


def test_proposals_queue(clean_initiative_engine):
    engine = clean_initiative_engine
    now = 1700000000.0
    engine.settings.quiet_hours_enabled = False
    engine.last_user_activity = now - 2000.0
    engine.last_initiative_time = now - 3000.0

    engine.add_proposal("action_proposal", "Сделать оптимизацию шейдеров Cyberdeck")
    assert len(engine.pending_proposals) == 1

    rec = engine.evaluate(companion_mode="normal", now=now)
    assert rec is not None
    assert rec.kind == "action_proposal"
    assert "Сделать оптимизацию шейдеров Cyberdeck" in rec.content
    # Очередь предложений должна стать пустой
    assert len(engine.pending_proposals) == 0


def test_agent_initiative_integration(tmp_path, monkeypatch):
    test_init = tmp_path / "agent_initiative.json"
    test_ctx = tmp_path / "agent_context.json"
    monkeypatch.setattr("capri.core.initiative.INITIATIVE_FILE", test_init)
    monkeypatch.setattr("capri.core.context.CONTEXT_FILE", test_ctx)

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    agent = CapriAgent(cfg)

    # Проверяем, что движок инициативы инициализирован и связан
    assert hasattr(agent, "initiative_engine")
    assert agent.initiative_engine is get_initiative_engine()

    # Проверяем системный промпт
    full_prompt = agent._build_full_prompt()
    assert "INITIATIVE ENGINE" in full_prompt
    assert "Капри обладает собственной инициативой" in full_prompt

    # Проверяем уважение настройки config.companion.proactive_enabled = False
    cfg_disabled = AppConfig()
    cfg_disabled.voice.wake_word_enabled = False
    cfg_disabled.companion.proactive_enabled = False
    agent_disabled = CapriAgent(cfg_disabled)
    assert agent_disabled.initiative_engine.settings.enabled is False

    # Проверяем сброс времени сессии при /new
    old_start = agent.initiative_engine.session_start_time
    time.sleep(0.01)
    agent.start_new_session(title="Чистая сессия")
    assert agent.initiative_engine.session_start_time >= old_start


@pytest.mark.asyncio
async def test_initiative_tools(tmp_path, monkeypatch):
    test_file = tmp_path / "tools_initiative.json"
    engine = InitiativeEngine(file_path=test_file)
    monkeypatch.setattr("capri.tools.initiative._shared_engine", engine)

    # 1. propose_action
    p_res = await propose_action("action_proposal", "Проверить latency TTS")
    assert "Проактивное предложение зарегистрировано" in p_res
    assert len(engine.pending_proposals) == 1

    # 2. configure_initiative
    cfg_res = await configure_initiative(enabled=True, level="proactive", cooldown_minutes=10)
    assert "Параметры инициативы обновлены" in cfg_res
    assert engine.settings.level == "proactive"
    assert engine.settings.cooldown_seconds == 600

    # 3. get_initiative_status
    status_card = await get_initiative_status()
    assert "ДВИЖОК ИНИЦИАТИВЫ CAPRI" in status_card
    assert "PROACTIVE" in status_card


def test_cyberdeck_initiative_commands_and_tick(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    test_file = tmp_path / "deck_initiative.json"
    monkeypatch.setattr("capri.core.initiative.INITIATIVE_FILE", test_file)

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.voice.tts_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True), patch("capri.ui.cyberdeck.save_config", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.initiative_engine = InitiativeEngine(test_file)

        # 1. /initiative (статус)
        deck._handle_command("/initiative")

        # 2. /initiative level subtle
        deck._handle_command("/initiative level subtle")
        assert deck.agent.initiative_engine.settings.level == "subtle"

        # 3. /initiative cooldown 15
        deck._handle_command("/initiative cooldown 15")
        assert deck.agent.initiative_engine.settings.cooldown_seconds == 900

        # 4. /initiative off
        deck._handle_command("/initiative off")
        assert deck.agent.initiative_engine.settings.enabled is False

        # 5. /initiative on
        deck._handle_command("/initiative on")
        assert deck.agent.initiative_engine.settings.enabled is True

        # 6. /initiative trigger (принудительное срабатывание)
        deck._handle_command("/initiative trigger")
        # Проверяем, что в истории появилась инициатива
        assert len(deck.agent.initiative_engine.history) >= 1

        deck.close()
