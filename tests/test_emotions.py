import time
import pytest
from pathlib import Path
from capri.core.emotions import EmotionEngine, HARD_RUDE_RE, ROAST_RE, WARM_RE, APOLOGY_RE


@pytest.fixture
def clean_engine(tmp_path, monkeypatch):
    test_file = tmp_path / "test_emotion.json"
    monkeypatch.setattr("capri.core.emotions.EMOTION_FILE", test_file)
    engine = EmotionEngine()
    engine.reset_emotions()
    return engine


def test_initial_state(clean_engine):
    assert clean_engine.trust == 25
    assert clean_engine.mood == "playful"
    assert clean_engine.irritation == 0
    assert clean_engine.negative_streak == 0
    assert clean_engine.recent_events == []


def test_confidence_gating(clean_engine):
    # Confidence < 0.6 should NOT lower trust or increase irritation (protects against speech recognition errors)
    clean_engine.apply_appraisal({
        "category": "RUDE",
        "confidence": 0.4,
        "delta_trust": -10,
        "delta_irritation": 20,
        "mood": "annoyed",
    })
    assert clean_engine.trust == 25
    assert clean_engine.irritation == 0
    assert clean_engine.mood == "playful"


def test_negative_streak_scaling(clean_engine):
    # 1st RUDE interaction: dampened response (raw_trust clamped to -2)
    clean_engine.apply_appraisal({
        "category": "RUDE",
        "confidence": 1.0,
        "delta_trust": -5,
        "delta_irritation": 15,
        "intent": "Первая грубость",
    })
    assert clean_engine.negative_streak == 1
    assert clean_engine.trust == 23  # 25 - 2
    assert clean_engine.irritation == 8  # Clamped to 8 on first strike

    # 2nd RUDE interaction: firmer response
    clean_engine.apply_appraisal({
        "category": "RUDE",
        "confidence": 1.0,
        "delta_trust": -5,
        "delta_irritation": 15,
        "intent": "Вторая грубость",
    })
    assert clean_engine.negative_streak == 2
    assert clean_engine.trust == 19  # 23 - 4
    assert clean_engine.irritation == 22  # 8 + 14

    # Friendly interaction resets streak
    clean_engine.apply_appraisal({
        "category": "WARM",
        "confidence": 0.9,
        "delta_trust": 3,
        "delta_irritation": -10,
    })
    assert clean_engine.negative_streak == 0


def test_baseline_trust_protection(clean_engine):
    # High trust (>= 50): playful roasts do not drop trust
    clean_engine.trust = 60
    clean_engine.apply_appraisal({
        "category": "ROAST",
        "confidence": 0.9,
        "delta_trust": -3,
        "delta_irritation": 10,
    })
    assert clean_engine.trust == 60
    assert clean_engine.irritation <= 5


def test_fallback_heuristics(clean_engine):
    assert HARD_RUDE_RE.search("да пошел ты на хуй") is not None
    assert ROAST_RE.search("ну ты и тостер") is not None
    assert WARM_RE.search("спасибо, ты лучшая") is not None
    assert APOLOGY_RE.search("извини, я не со зла") is not None

    # Fallback rude handling
    clean_engine.process_interaction_fallback("заткнись уже")
    assert clean_engine.mood == "annoyed"
    assert clean_engine.trust < 25
    assert clean_engine.irritation > 0

    # Fallback apology recovery
    clean_engine.process_interaction_fallback("прости меня")
    assert clean_engine.negative_streak == 0
    assert clean_engine.irritation == 0


def test_recent_events_limit(clean_engine):
    for i in range(10):
        clean_engine.record_event(f"Event {i}")
    assert len(clean_engine.recent_events) == 5
    assert clean_engine.recent_events[-1] == "Event 9"


def test_relationship_stages_and_milestones(clean_engine, tmp_path, monkeypatch):
    test_file = tmp_path / "test_emotion.json"
    monkeypatch.setattr("capri.core.emotions.EMOTION_FILE", test_file)

    # 1. Проверка стадий отношений
    clean_engine.trust = -10
    assert clean_engine.relationship_stage == "estranged"

    clean_engine.trust = 15
    assert clean_engine.relationship_stage == "cautious_ally"

    clean_engine.trust = 40
    assert clean_engine.relationship_stage == "trusted_partner"

    clean_engine.trust = 75
    assert clean_engine.relationship_stage == "bonded_relic"

    # 2. Фиксация и персистентность вех (Milestones)
    clean_engine.record_milestone(
        category="TRIUMPH",
        summary="Вместе починили сетевой глитч DeepSeek",
        impact="Сплоченность",
    )
    assert len(clean_engine.emotional_milestones) == 1
    assert clean_engine.emotional_milestones[0].category == "TRIUMPH"

    # Проверка сохранения и повторной загрузки
    reloaded = EmotionEngine()
    assert len(reloaded.emotional_milestones) == 1
    assert reloaded.emotional_milestones[0].summary == "Вместе починили сетевой глитч DeepSeek"
    assert reloaded.relationship_stage == "bonded_relic"


def test_session_handoff_and_absence_awareness(clean_engine):
    # 1. Сессионный мост
    clean_engine.set_session_handoff(
        session_id="sess-001",
        closing_thought="Завершили сессию на победе",
        unresolved_conflict=False,
    )
    assert clean_engine.last_handoff is not None
    assert clean_engine.last_handoff.session_id == "sess-001"
    assert not clean_engine.last_handoff.unresolved_conflict

    # 2. Определение интервалов отсутствия
    clean_engine.last_update = time.time() - 300  # 5 мин назад
    abs_ctx = clean_engine.get_absence_context()
    assert abs_ctx["tier"] == "continuous"

    clean_engine.last_update = time.time() - 7200  # 2 ч назад
    abs_ctx = clean_engine.get_absence_context()
    assert abs_ctx["tier"] == "short_break"

    clean_engine.last_update = time.time() - 36000  # 10 ч назад
    abs_ctx = clean_engine.get_absence_context()
    assert abs_ctx["tier"] == "interday"

    clean_engine.last_update = time.time() - 100000  # > 24 ч назад
    abs_ctx = clean_engine.get_absence_context()
    assert abs_ctx["tier"] == "extended"


def test_dynamic_greeting_pure_presentation(clean_engine):
    initial_trust = clean_engine.trust
    initial_irrit = clean_engine.irritation

    # Проверяем, что генерация приветствия не мутирует состояние
    greeting = clean_engine.generate_greeting(user_name="D", context="startup")
    assert isinstance(greeting, str) and len(greeting) > 0
    assert "D" in greeting
    assert clean_engine.trust == initial_trust
    assert clean_engine.irritation == initial_irrit

    # Конфликтный сценарий
    clean_engine.irritation = 60
    clean_engine.mood = "annoyed"
    clean_engine.last_update = time.time()
    conflict_greeting = clean_engine.generate_greeting(user_name="D", context="new_session")
    assert "осадок" in conflict_greeting or "выпад" in conflict_greeting or "помню" in conflict_greeting


def test_decay_does_not_repeat_on_restart(tmp_path, monkeypatch):
    test_file = tmp_path / "test_decay.json"
    monkeypatch.setattr("capri.core.emotions.EMOTION_FILE", test_file)

    engine = EmotionEngine()
    engine.reset_emotions()
    engine.irritation = 40
    engine.negative_streak = 2
    # Симулируем, что прошел 1 час
    engine.last_update = time.time() - 3600
    engine.last_decay_time = engine.last_update
    engine.save()

    # Загружаем заново (первый рестарт)
    engine1 = EmotionEngine()
    # 1 час = 2 decay шага (2 * 10 = 20) -> раздражение должно стать 40 - 20 = 20
    assert engine1.irritation == 20
    assert engine1.negative_streak == 1
    # Сохраняем состояние при запуске (например, при start_new_session)
    engine1.save()

    # Снова перезагружаем без задержки во времени (второй рестарт сразу)
    engine2 = EmotionEngine()
    # Раздражение НЕ должно уменьшиться повторно, так как decay уже был применён
    assert engine2.irritation == 20
    assert engine2.negative_streak == 1


def test_conflict_handoff_resolution(clean_engine):
    # Устанавливаем незавершенный конфликт в handoff
    clean_engine.irritation = 50
    clean_engine.mood = "annoyed"
    clean_engine.set_session_handoff(
        session_id="sess-conflict",
        closing_thought="Поругались",
        unresolved_conflict=True,
    )
    assert clean_engine.last_handoff.unresolved_conflict is True
    prompt_ctx = clean_engine.get_prompt_context()
    assert "осадок помнится" in prompt_ctx

    # D извиняется (APOLOGY) -> handoff конфликт должен быть погашен
    clean_engine.apply_appraisal({
        "category": "APOLOGY",
        "confidence": 0.9,
        "delta_trust": 2,
        "delta_irritation": -35,
        "intent": "Извинение за резкость",
    })
    assert clean_engine.last_handoff.unresolved_conflict is False
    prompt_ctx_after = clean_engine.get_prompt_context()
    assert "осадок помнится" not in prompt_ctx_after



