"""
CAPRI v3.2 Stage 1 — Soul & Canon deterministic acceptance tests:
1. Friendly ROAST has zero penalty (delta_trust=0, delta_irritation=0, negative_streak=0)
   on BOTH the <appraisal> path and the regex fallback path, and never auto-boosts trust.
2. Dynamic user_name everywhere (zero hardcoded 'D' leaks in prompts, emotions, context,
   initiative, greetings, milestones, or status cards when user_name='Alex').
3. Prompt structure separates confirmed CDPR canon from desktop-companion adaptation
   and keeps section token budgets lean and measurable.
"""

from __future__ import annotations

import re

from capri.core.agent import CapriAgent
from capri.core.config import AppConfig
from capri.core.context import ContextStore
from capri.core.emotions import EmotionEngine
from capri.core.initiative import InitiativeEngine
from capri.core.prompts import (
    ADAPTATION_CORE_TEMPLATE,
    CANON_CORE_TEMPLATE,
    FEW_SHOT_ANCHORS,
    get_system_prompt,
    measure_prompt_budget,
)


def _has_standalone_d(text: str) -> bool:
    return bool(re.search(r"(?<![A-Za-zА-Яа-я0-9_])D(?![A-Za-zА-Яа-я0-9_])", text or ""))


def test_roast_is_fully_neutral_on_appraisal_and_regex_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("capri.core.emotions.EMOTION_FILE", tmp_path / "emotion.json")
    engine = EmotionEngine()
    engine.reset_emotions()

    # 5 consecutive friendly roasts via appraisal must NOT drop trust, raise irritation,
    # increment negative_streak, auto-boost trust, or switch mood to 'annoyed'.
    for i in range(5):
        engine.apply_appraisal(
            {
                "category": "ROAST",
                "confidence": 1.0,
                "delta_trust": -3 if i % 2 == 0 else 2,
                "delta_irritation": 12,
                "intent": f"Дружеский подкол #{i + 1}",
                "mood": "sarcastic",
            },
            user_name="Alex",
        )
    assert engine.trust == 25
    assert engine.irritation == 0
    assert engine.negative_streak == 0
    assert engine.mood == "sarcastic"

    # 5 consecutive friendly roasts via regex fallback must ALSO keep trust/irritation/streak untouched
    for _ in range(5):
        engine.process_interaction_fallback("ну ты и тостер")
    assert engine.trust == 25
    assert engine.irritation == 0
    assert engine.negative_streak == 0
    assert engine.mood == "sarcastic"

    # Real RUDE still scales negative_streak and transitions to 'annoyed'
    for i in range(3):
        engine.apply_appraisal(
            {
                "category": "RUDE",
                "confidence": 0.95,
                "delta_trust": -5,
                "delta_irritation": 15,
                "intent": f"Реальная грубость #{i + 1}",
            },
            user_name="Alex",
        )
    assert engine.negative_streak == 3
    assert engine.trust < 25
    assert engine.irritation > 0
    assert engine.mood == "annoyed"
    assert any("Alex" in m.summary and not _has_standalone_d(m.summary) for m in engine.emotional_milestones)


def test_zero_hardcoded_d_across_prompts_emotions_context_initiative(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("capri.core.emotions.EMOTION_FILE", tmp_path / "emotion.json")
    monkeypatch.setattr("capri.core.context.CONTEXT_FILE", tmp_path / "context.json")
    monkeypatch.setattr("capri.core.initiative.INITIATIVE_FILE", tmp_path / "initiative.json")

    cfg = AppConfig()
    cfg.companion.user_name = "Alex"

    sys_prompt = get_system_prompt(cfg)
    assert "Alex" in sys_prompt
    assert not _has_standalone_d(sys_prompt)

    emotions = EmotionEngine()
    emotions.reset_emotions()
    for mood in ("playful", "sarcastic", "annoyed", "warm", "neutral"):
        emotions.mood = mood
        emotions.trust = 70 if mood == "warm" else (-20 if mood == "annoyed" else 25)
        ctx = emotions.get_prompt_context(user_name="Alex")
        assert not _has_standalone_d(ctx), f"Hardcoded D leaked in emotion prompt for mood={mood}"
    card = emotions.get_status_card(user_name="Alex")
    assert "Alex" in card and not _has_standalone_d(card)

    greeting = emotions.generate_greeting(user_name="Alex", context="startup")
    assert "Alex" in greeting and not _has_standalone_d(greeting)

    cstore = ContextStore()
    cstore.add_item("thread", "Тестовая ветка")
    c_prompt = cstore.get_prompt_context(user_name="Alex")
    assert "Alex" in c_prompt and not _has_standalone_d(c_prompt)

    init_eng = InitiativeEngine()
    for kind in ("task_nudge", "focus_break", "idle_checkin", "action_proposal"):
        for stage in ("cautious_ally", "trusted_partner", "bonded_relic"):
            phrase = init_eng.generate_phrase(
                kind, stage=stage, details={"is_step": True, "item": "проверить билд", "proposal": "запустить тесты"}, user_name="Alex"
            )
            assert not _has_standalone_d(phrase), f"Hardcoded D leaked in initiative phrase ({kind}/{stage}): {phrase}"
    assert "Alex" in init_eng.get_prompt_context(user_name="Alex")
    assert not _has_standalone_d(init_eng.get_prompt_context(user_name="Alex"))
    assert "Alex" in init_eng.get_status_card(user_name="Alex")
    assert not _has_standalone_d(init_eng.get_status_card(user_name="Alex"))

    agent = CapriAgent(cfg)
    agent.memory_store.add_or_update("stack", "Python 3.12, PySide6", category="preferences", importance=4)
    mem_prompt = agent.memory_store.get_prompt_context(user_name="Alex")
    mem_card = agent.memory_store.get_stats_card(user_name="Alex")
    assert "Alex" in mem_prompt and not _has_standalone_d(mem_prompt)
    assert "Alex" in mem_card and not _has_standalone_d(mem_card)

    full_prompt = agent._build_full_prompt("проверим контекст и стек")
    assert "Alex" in full_prompt
    assert "ДОЛГОВРЕМЕННАЯ ПАМЯТЬ" in full_prompt
    assert not _has_standalone_d(full_prompt)

    # Default config user_name must be neutral ("User"), never hardcoded "D"
    assert AppConfig().companion.user_name == "User"

    # Desktop status tool also uses configured user_name instead of hardcoded "D"
    import asyncio
    from capri.core.config import save_config
    from capri.tools.desktop import capri_self_control
    save_config(cfg)
    status_out = asyncio.run(capri_self_control("status"))
    assert "Alex" in status_out and not _has_standalone_d(status_out)


def test_observer_and_ui_fallbacks_have_no_hardcoded_d(tmp_path, monkeypatch):
    """Stage 1 regression: observer.py passes user_name; widgets.py fallbacks are neutral."""
    import asyncio
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from capri.core.observer import DesktopObserver
    from capri.ui.widgets import UserMonogramBadge, WelcomeCanvas

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("capri.core.emotions.EMOTION_FILE", tmp_path / "emotion.json")

    app = QApplication.instance() or QApplication([])
    badge = UserMonogramBadge()
    assert badge._letter == "U"
    welcome = WelcomeCanvas(greeting="")
    assert not _has_standalone_d(welcome.lbl_greeting.text())
    badge.deleteLater()
    welcome.deleteLater()
    app.processEvents()

    # Observer must pass configured user_name into emotions.get_prompt_context()
    captured = {}

    class _DummyCompletions:
        async def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Всё ровно, работаем."))]
            )

    cfg = AppConfig()
    cfg.companion.user_name = "Alex"
    cfg.llm.api_key = "test-key"
    cfg.voice.tts_enabled = False
    agent = CapriAgent(cfg)
    agent.emotions.mood = "annoyed"
    agent.emotions.trust = -20
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=_DummyCompletions()))
    agent._get_client = lambda: agent.client
    monkeypatch.setattr("capri.core.observer.notify_avatar", lambda *a, **kw: None)

    obs = DesktopObserver(agent, agent.emotions, mode="normal")
    asyncio.run(obs._trigger_reaction("kitty", "Terminal"))
    assert "messages" in captured
    sys_msg = captured["messages"][0]["content"]
    assert "Alex" in sys_msg
    assert not _has_standalone_d(sys_msg)
    assert "грубости Alex" in sys_msg
    assert "грубости User" not in sys_msg


def test_canon_vs_adaptation_separation_and_prompt_budget():
    cfg = AppConfig()
    cfg.companion.user_name = "User"
    sys_prompt = get_system_prompt(cfg)

    # Explicit structural separation between confirmed CDPR canon and adaptation
    assert "[ПОДТВЕРЖДЁННЫЙ КАНОН — PROJECT CAPRICORN]" in sys_prompt
    assert "[АДАПТАЦИЯ ПЕРСОНАЖА ДЛЯ DESKTOP-КОМПАНЬОНА]" in sys_prompt
    assert "[ОРИГИНАЛЬНЫЕ ЯКОРЯ ГОЛОСА]" in sys_prompt
    assert "Soulkiller" in CANON_CORE_TEMPLATE
    assert "Kyoko" in CANON_CORE_TEMPLATE
    # Interpretive persona framing belongs strictly in ADAPTATION, not CANON
    assert "ОДНА цельная личность" not in CANON_CORE_TEMPLATE
    assert "чужая ассоциация" not in CANON_CORE_TEMPLATE
    assert "ОДНА цельная личность" in ADAPTATION_CORE_TEMPLATE
    assert "Blackwall" not in sys_prompt
    assert "оружейный конструкт" not in sys_prompt
    assert len(FEW_SHOT_ANCHORS) < 900

    # Leaner than previous v3.1.0 baseline (6352 chars system_prompt / 7227 total)
    budget = measure_prompt_budget(
        {
            "system_prompt": sys_prompt,
            "emotion_block": EmotionEngine().get_prompt_context(user_name="User"),
            "initiative_block": InitiativeEngine().get_prompt_context(user_name="User"),
        }
    )
    assert budget["system_prompt"]["chars"] < 5500
    assert budget["total"]["chars"] < 6500
    assert budget["total"]["approx_tokens"] > 0
