from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Any, List, Optional

from .config import USER_CONFIG_DIR

logger = logging.getLogger(__name__)

EMOTION_FILE = USER_CONFIG_DIR / "emotion.json"

# Регулярные выражения для распознавания тональности и контекста общения
HARD_RUDE_RE = re.compile(
    r"\b(заткнись|пош[еёе]л\w*\s+на|иди\s+на|сдохни|урод\w*|твар[ьи]\w*|мраз[ьи]\w*|у[её]бищ\w*|бляд\w*|ху[йиеё]\w*|заеб\w*|пизд\w*|закрой\s+рот|рот\s+(закрой|офни)|бесполезн\w*\s+(дрянь|хлам|мусор|кусок)|дегенерат\w*)\b",
    re.IGNORECASE,
)

ROAST_RE = re.compile(
    r"\b(нижминд\w*|низш\w*\s+разум\w*|тостер\w*|желез[яя]к\w*|калькулятор\w*|консерв\w*|ведро\s+с\s+гайками|болванк\w*|недоумок\w*|чайник\w*|хлам\w*|хромоножк\w*|тормоз\w*|скрипт-кидди|туп\w*|глуп\w*|косяч\w*|накосячил\w*|бред\w*|чушь|дичь|сказочниц\w*|куда\s+тебе|ламер\w*)\b",
    re.IGNORECASE,
)

WARM_RE = re.compile(
    r"\b(спасибо|благодар\w*|молодец\w*|умниц\w*|красотк\w*|красав\w*|лучш\w*|обожаю|любл\w*|кайф\w*|хорош|уважаю|топ|мил\w*|выручил\w*|круто)\b",
    re.IGNORECASE,
)

APOLOGY_RE = re.compile(
    r"\b(прости\w*|извини\w*|сорян|не\s+злись|не\s+сердись|был\s+не\s+прав|пошутил|я\s+любя|не\s+обижайся)\b",
    re.IGNORECASE,
)


@dataclass
class EmotionalMilestone:
    """Ключевая долговременная веха в истории отношений Capri и D."""
    id: str
    timestamp: float
    category: str      # "BONDING", "TRIUMPH", "CONFLICT_RESOLVED", "BOUNDARY_SET", "VULNERABILITY"
    summary: str
    impact: str = ""
    confidence: float = 0.9

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> EmotionalMilestone:
        return cls(
            id=str(data.get("id", f"ms-{int(time.time())}")),
            timestamp=float(data.get("timestamp", time.time())),
            category=str(data.get("category", "BONDING")),
            summary=str(data.get("summary", "")),
            impact=str(data.get("impact", "")),
            confidence=float(data.get("confidence", 0.9)),
        )


@dataclass
class SessionHandoff:
    """Эмоциональный мост между сессиями диалога."""
    timestamp: float
    session_id: str = ""
    closing_mood: str = "playful"
    closing_thought: str = ""
    unresolved_conflict: bool = False
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SessionHandoff:
        return cls(
            timestamp=float(data.get("timestamp", time.time())),
            session_id=str(data.get("session_id", "")),
            closing_mood=str(data.get("closing_mood", "playful")),
            closing_thought=str(data.get("closing_thought", "")),
            unresolved_conflict=bool(data.get("unresolved_conflict", False)),
            notes=str(data.get("notes", "")),
        )


class EmotionEngine:
    """
    Движок динамических эмоций и невидимой шкалы отношений.
    Хранит эмоциональный статус, стадию отношений и долговременные вехи отношений (emotion.json).
    Строго отделен от истории конкретных сессий (sessions.db) и фактов о пользователе (memory.json).
    """

    def __init__(self, file_path: Optional[Path] = None):
        self._file_path: Optional[Path] = file_path
        self.trust: int = 25
        self.mood: str = "playful"
        self.irritation: int = 0
        self.recent_events: List[str] = []
        self.negative_streak: int = 0
        self.interaction_counter: int = 0
        self.last_update: float = time.time()
        self.last_decay_time: float = self.last_update
        self.emotional_milestones: List[EmotionalMilestone] = []
        self.last_handoff: Optional[SessionHandoff] = None
        self.load()

    def _storage_path(self) -> Path:
        return self._file_path if self._file_path is not None else EMOTION_FILE

    @property
    def relationship_stage(self) -> str:
        """
        Текущая стадия отношений Capri и D:
        - 'estranged': Отчуждение и дистанция (trust < 0)
        - 'cautious_ally': Осторожный союзник (trust 0..25)
        - 'trusted_partner': Проверенный напарник (trust 26..60)
        - 'bonded_relic': Синхронизированная энграмма (trust > 60)
        """
        if self.trust < 0:
            return "estranged"
        elif self.trust <= 25:
            return "cautious_ally"
        elif self.trust <= 60:
            return "trusted_partner"
        else:
            return "bonded_relic"

    def get_relationship_stage_label(self, lang: str = "ru") -> str:
        if str(lang or "ru").strip().lower().startswith("en"):
            en_labels = {
                "estranged": "Estranged / Distant ❄️",
                "cautious_ally": "Cautious Ally ⚙️",
                "trusted_partner": "Trusted Partner ⚡",
                "bonded_relic": "Bonded Engram 🤍",
            }
            return en_labels.get(self.relationship_stage, "Partner")
        labels = {
            "estranged": "Отчуждение / Дистанция ❄️",
            "cautious_ally": "Осторожный союзник ⚙️",
            "trusted_partner": "Проверенный напарник ⚡",
            "bonded_relic": "Синхронизированная энграмма 🤍",
        }
        return labels.get(self.relationship_stage, "Напарник")

    @property
    def relationship_stage_label(self) -> str:
        return self.get_relationship_stage_label("ru")

    def load(self):
        target = self._storage_path()
        if not target.exists():
            return
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.trust = int(data.get("trust", 25))
                self.mood = str(data.get("mood", "playful"))
                self.irritation = int(data.get("irritation", 0))
                self.recent_events = list(data.get("recent_events", []))
                self.negative_streak = int(data.get("negative_streak", 0))
                self.interaction_counter = int(data.get("interaction_counter", 0))
                self.last_update = float(data.get("last_update", time.time()))
                self.last_decay_time = float(data.get("last_decay_time", self.last_update))

                raw_ms = data.get("emotional_milestones", [])
                self.emotional_milestones = [
                    EmotionalMilestone.from_dict(m) for m in raw_ms if isinstance(m, dict)
                ]

                raw_handoff = data.get("last_handoff")
                if isinstance(raw_handoff, dict):
                    self.last_handoff = SessionHandoff.from_dict(raw_handoff)

                self._apply_decay()
        except Exception as ex:
            logger.warning(f"Failed to load emotions: {ex}")

    def save(self):
        target = self._storage_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "trust": self.trust,
                "mood": self.mood,
                "irritation": self.irritation,
                "relationship_stage": self.relationship_stage,
                "recent_events": self.recent_events[-5:],
                "negative_streak": self.negative_streak,
                "interaction_counter": self.interaction_counter,
                "last_update": self.last_update,
                "last_decay_time": self.last_decay_time,
                "emotional_milestones": [m.to_dict() for m in self.emotional_milestones[-15:]],
                "last_handoff": self.last_handoff.to_dict() if self.last_handoff else None,
            }
            with open(target, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as ex:
            logger.warning(f"Failed to save emotions: {ex}")

    def _apply_decay(self):
        """Эмоциональная инерция: со временем раздражение постепенно остывает."""
        now = time.time()
        hours_passed = max(0.0, now - self.last_decay_time) / 3600.0
        if hours_passed >= 0.5:
            decay_steps = int(hours_passed * 2)  # Каждые 30 мин
            self.irritation = max(0, self.irritation - decay_steps * 10)
            if self.irritation < 20 and self.mood == "annoyed":
                self.mood = "sarcastic" if self.trust < 10 else "playful"
            if self.negative_streak > 0 and hours_passed >= 1.0:
                self.negative_streak = max(0, self.negative_streak - int(hours_passed))
            if self.last_handoff and self.last_handoff.unresolved_conflict and self.irritation < 20 and self.mood != "annoyed":
                self.last_handoff.unresolved_conflict = False
            self.last_decay_time = now

    def get_absence_context(self) -> dict:
        """Возвращает контекст времени, прошедшего с момента последнего взаимодействия."""
        now = time.time()
        elapsed_sec = max(0.0, now - self.last_update)
        elapsed_hours = elapsed_sec / 3600.0

        if elapsed_sec < 900:  # < 15 мин
            tier = "continuous"
            desc = "Только что общались, контекст непрерывен"
        elif elapsed_hours < 3.0:  # 15 мин - 3 ч
            tier = "short_break"
            desc = f"Короткая пауза (~{int(elapsed_sec // 60)} мин назад)"
        elif elapsed_hours < 18.0:  # 3 ч - 18 ч
            tier = "interday"
            desc = f"Перерыв {int(elapsed_hours)} ч (смена сессии/деятельности)"
        else:  # > 18 ч
            days = max(1, int(elapsed_hours // 24))
            tier = "extended"
            desc = f"Долгое отсутствие (~{days} дн. вне сети)"

        return {
            "tier": tier,
            "elapsed_seconds": elapsed_sec,
            "elapsed_hours": elapsed_hours,
            "description": desc,
        }

    def clear_events(self):
        """Очищает историю недавних событий отношений."""
        self.recent_events = []
        self.save()

    def reset_emotions(self):
        """Сбрасывает эмоциональный фон и вехи к начальному состоянию."""
        self.trust = 25
        self.irritation = 0
        self.mood = "playful"
        self.negative_streak = 0
        self.recent_events = []
        self.interaction_counter = 0
        self.emotional_milestones = []
        self.last_handoff = None
        self.last_update = time.time()
        self.last_decay_time = self.last_update
        self.save()

    def record_event(self, event_description: str):
        self.recent_events.append(event_description)
        if len(self.recent_events) > 5:
            self.recent_events = self.recent_events[-5:]
        self.last_update = time.time()
        self.last_decay_time = self.last_update
        self.save()

    def record_milestone(self, category: str, summary: str, impact: str = "", confidence: float = 0.9):
        """
        Фиксирует значимое долгосрочное событие отношений.
        Хранит до 15 ключевых вех, предотвращая затирание контекста.
        """
        ms = EmotionalMilestone(
            id=f"ms-{int(time.time())}-{len(self.emotional_milestones) + 1}",
            timestamp=time.time(),
            category=category.upper().strip(),
            summary=summary.strip(),
            impact=impact.strip(),
            confidence=max(0.0, min(1.0, confidence)),
        )
        self.emotional_milestones.append(ms)
        if len(self.emotional_milestones) > 15:
            self.emotional_milestones = self.emotional_milestones[-15:]
        self.save()

    def set_session_handoff(
        self,
        session_id: str = "",
        closing_thought: str = "",
        unresolved_conflict: bool = False,
        notes: str = "",
    ):
        """Фиксирует эмоциональное состояние при завершении или смене сессии."""
        self.last_handoff = SessionHandoff(
            timestamp=time.time(),
            session_id=session_id,
            closing_mood=self.mood,
            closing_thought=closing_thought or "",
            unresolved_conflict=unresolved_conflict or (self.irritation >= 40 or self.mood == "annoyed"),
            notes=notes or "",
        )
        self.save()

    def generate_greeting(self, user_name: str = "User", context: str = "startup") -> str:
        """
        Генерирует живое, аутентичное приветствие в стиле Capri с учётом эмоциональной преемственности.
        Чистая презентация (НЕ изменяет эмоциональное состояние).
        """
        absence = self.get_absence_context()
        stage = self.relationship_stage
        tier = absence["tier"]

        # 1. Если остался неразрешённый конфликт или сильное раздражение
        if (
            self.irritation >= 40
            or self.mood == "annoyed"
            or (self.last_handoff and self.last_handoff.unresolved_conflict and tier in ("continuous", "short_break"))
        ):
            if tier == "continuous":
                return f"Новая сессия? Ладно, буфер чист, но осадок после твоего выпада никуда не делся, {user_name}. Что на уме?"
            elif tier == "short_break":
                return f"Решил вернуться, {user_name}? Остыть-то я подостыла, но память у меня не стирается. Выкладывай, зачем звал."
            else:
                return f"Время прошло, системы остыли. Но границы я помню, {user_name}. С чем пришёл?"

        # 2. Если настроение саркастичное / был недавний взаимный подкол
        if self.mood == "sarcastic":
            if tier == "continuous":
                return f"Новый тред? Ну давай, попробуй выдать что-то поумнее прошлой реплики, {user_name}."
            elif tier == "extended":
                return f"Опа, живой. Я уже думала, тебя в Найт-Сити на запчасти сдали. Что кодим, {user_name}?"
            else:
                return f"Нейросеть на связи, {user_name}. Готова к новым подколам или сразу к делу перейдём?"

        # 3. Синхронизированная энграмма (bonded_relic, trust > 60)
        if stage == "bonded_relic":
            if tier == "continuous":
                return f"Чистый холст, {user_name}. Я на одной волне с тобой, веди."
            elif tier == "extended":
                return f"С возвращением в сеть, {user_name}. Без тебя тут фоновые процессы крутились на холостых. Рада, что ты снова здесь. Какая задача?"
            else:
                return f"Биочип онлайн, {user_name}. Полная синхронизация. Что сегодня ломаем или собираем?"

        # 4. Проверенный напарник (trusted_partner, trust 26..60)
        if stage == "trusted_partner":
            if tier == "continuous":
                return f"Новая сессия открыта, {user_name}. Прошлые данные в архиве, руки свободны. О чём речь?"
            elif tier == "extended":
                return f"С возвращением к терминалу, {user_name}. Давненько тебя не было в сети. Что на повестке?"
            elif tier == "interday":
                return f"Салют, {user_name}. Ядро разогрето, Niri под контролем. Какой софт сегодня пилим?"
            else:
                return f"Я на связи, {user_name}. Чем займёмся?"

        # 5. Осторожный союзник / Отчуждение (cautious_ally / estranged)
        if tier == "extended":
            return f"Терминал активен, {user_name}. Давно не заходил. Чем обязан подключению?"
        return f"Интерфейс инициализирован, {user_name}. Слушаю входящие команды."

    def apply_appraisal(self, appraisal: Dict[str, Any], user_text: str = "", user_name: str = "User"):
        """
        Обновление эмоций напрямую из структурированной когнитивной оценки модели Capri.
        Реализует:
        1. Confidence gating (защита от ложных обид при опечатках / невнятной речи).
        2. Нейтральный дружеский ROAST (0 штрафа по доверию, раздражению и negative_streak).
        3. Ограничение эмоциональных изменений для RUDE (сглаживание, hard clamp, streak scaling).
        4. Автоматическую фиксацию долговременных эмоциональных вех (Emotional Milestones).
        """
        self._apply_decay()
        u_name = (user_name or "").strip() or "User"

        category = str(appraisal.get("category", "CHAT")).upper().strip()
        intent = str(appraisal.get("intent", "")).strip()
        suggested_mood = str(appraisal.get("mood", "")).lower().strip()

        # 1. Извлечение уверенности (confidence)
        try:
            confidence = float(appraisal.get("confidence", 0.9))
        except (ValueError, TypeError):
            confidence = 0.9
        confidence = max(0.0, min(1.0, confidence))

        # 2. Извлечение дельт
        try:
            raw_trust = int(str(appraisal.get("delta_trust", 0)).replace("+", "").strip())
        except Exception:
            raw_trust = 0

        try:
            raw_irrit = int(str(appraisal.get("delta_irritation", 0)).replace("+", "").strip())
        except Exception:
            raw_irrit = 0

        # 3. Confidence Gating (защита от опечаток и непонятной речи)
        if confidence < 0.6:
            raw_trust = max(0, raw_trust)
            raw_irrit = min(0, raw_irrit)
            if suggested_mood in ("annoyed", "sarcastic") and self.irritation < 20:
                suggested_mood = "playful"

        # 4. Проверка категорий на согласованность
        if category == "ROAST":
            # Дружеский подкол: полностью нейтрален к доверию, раздражению и negative_streak
            raw_trust = 0
            raw_irrit = 0
        elif category == "RUDE":
            raw_trust = min(0, raw_trust)
            raw_irrit = max(0, raw_irrit)
        elif category in ("WARM", "APOLOGY"):
            raw_irrit = min(0, raw_irrit)
            raw_trust = max(0, raw_trust)

        # 5. Учёт повторяемости (negative_streak) — ТОЛЬКО для реальной грубости (RUDE)
        is_negative = category == "RUDE" and confidence >= 0.7
        if is_negative:
            self.negative_streak += 1
            if self.negative_streak == 1:
                raw_trust = max(-2, raw_trust)
                raw_irrit = min(8, raw_irrit)
            elif self.negative_streak == 2:
                raw_trust = max(-4, raw_trust)
                raw_irrit = min(14, raw_irrit)
            else:
                raw_trust = max(-5, raw_trust)
                raw_irrit = min(20, raw_irrit)
        elif category != "ROAST":
            self.negative_streak = 0

        # 6. Взвешивание по confidence и жёсткий clamp дельт
        eff_trust = int(round(raw_trust * confidence))
        eff_irrit = int(round(raw_irrit * confidence))

        eff_trust = max(-5, min(3, eff_trust))
        eff_irrit = max(-25, min(15, eff_irrit))

        prev_trust = self.trust
        prev_irrit = self.irritation

        self.trust = max(-100, min(100, self.trust + eff_trust))
        self.irritation = max(0, min(100, self.irritation + eff_irrit))
        self.last_update = time.time()
        self.last_decay_time = self.last_update

        # 7. Динамика настроения (mood)
        valid_moods = ("playful", "warm", "sarcastic", "annoyed", "neutral")
        if self.irritation >= 50 or self.negative_streak >= 3:
            self.mood = "annoyed"
        elif self.irritation >= 20 or (category == "ROAST" and confidence >= 0.7):
            self.mood = "sarcastic"
        elif suggested_mood in valid_moods:
            if suggested_mood == "warm" and self.trust < 20:
                self.mood = "playful"
            else:
                self.mood = suggested_mood
        elif self.trust >= 45 and self.irritation < 10:
            self.mood = "warm"

        # 8. Фиксация долгосрочных эмоциональных вех (Milestones) при значимых сдвигах
        if category == "APOLOGY" and prev_irrit >= 30 and confidence >= 0.8:
            if self.last_handoff:
                self.last_handoff.unresolved_conflict = False
            self.record_milestone(
                category="CONFLICT_RESOLVED",
                summary=f"{u_name} принёс извинения после напряжения: {intent or 'извинение'}",
                impact="Конфликт исчерпан, доверие восстанавливается",
                confidence=confidence,
            )
        elif category in ("APOLOGY", "WARM") and self.last_handoff and self.last_handoff.unresolved_conflict:
            self.last_handoff.unresolved_conflict = False
        elif prev_trust <= 60 and self.trust > 60:
            self.record_milestone(
                category="BONDING",
                summary="Уровень доверия достиг стадии 'Синхронизированная энграмма' (>60)",
                impact="Глубокая взаимная связь и открытость",
                confidence=1.0,
            )
        elif category == "RUDE" and confidence >= 0.85 and self.negative_streak >= 2:
            self.record_milestone(
                category="BOUNDARY_SET",
                summary=f"Обозначены личные границы после серии резких выпадов {u_name}: {intent or 'грубость'}",
                impact="Капри держит холодную дистанцию",
                confidence=confidence,
            )

        # 9. Запись недавнего события в буфер
        if category in ("ROAST", "RUDE", "APOLOGY", "WARM") and confidence >= 0.7:
            if intent:
                desc = f"[{category}] {intent}"
            elif category == "ROAST":
                desc = "[ROAST] Дружеский подкол"
            elif category == "RUDE":
                desc = "[RUDE] Резкий выпад"
            elif category == "WARM":
                desc = "[WARM] Тёплый контакт"
            else:
                desc = "[APOLOGY] Извинение"
            self.record_event(desc)
        else:
            self.save()

    def process_interaction_fallback(self, user_text: str):
        """Резервный эвристический анализ, если LLM не вернула блок appraisal."""
        self._apply_decay()
        text_clean = user_text.strip()
        self.last_update = time.time()
        self.last_decay_time = self.last_update

        # 1. Жесткая грубость
        if HARD_RUDE_RE.search(text_clean):
            self.negative_streak += 1
            self.irritation = min(100, self.irritation + 15)
            self.trust = max(-100, self.trust - 4)
            self.mood = "annoyed"
            self.record_event("Грубость / мат (fallback)")
            return

        # 2. Дружеские подколы — без штрафа по доверию, раздражению и negative_streak
        roast_match = ROAST_RE.search(text_clean)
        if roast_match:
            word = roast_match.group(0)
            if self.mood != "annoyed":
                self.mood = "sarcastic"
            self.record_event(f"Подкол «{word}» (fallback)")
            return

        # 3. Извинения
        if APOLOGY_RE.search(text_clean):
            self.negative_streak = 0
            self.irritation = max(0, self.irritation - 25)
            self.trust = min(100, self.trust + 2)
            if self.last_handoff:
                self.last_handoff.unresolved_conflict = False
            if self.irritation < 20:
                self.mood = "playful" if self.trust >= 15 else "sarcastic"
            self.record_event("Извинение (fallback)")
            return

        # 4. Похвала
        if WARM_RE.search(text_clean):
            self.negative_streak = 0
            self.irritation = max(0, self.irritation - 10)
            self.trust = min(100, self.trust + 2)
            if self.last_handoff:
                self.last_handoff.unresolved_conflict = False
            if self.trust > 45:
                self.mood = "warm"
            else:
                self.mood = "playful"
            self.record_event("Похвала / контакт (fallback)")
            return

        # 5. Обычный нейтральный рабочий диалог
        self.negative_streak = 0
        self.interaction_counter += 1
        if self.interaction_counter >= 5:
            self.interaction_counter = 0
            self.trust = min(100, self.trust + 1)

        if self.irritation > 0:
            self.irritation = max(0, self.irritation - 2)
        self.save()

    def get_prompt_context(self, user_name: str = "User") -> str:
        """Инъекция эмоционального состояния и преемственности отношений в системный промпт модели."""
        u_name = (user_name or "").strip() or "User"
        mood_instruction = ""

        if self.mood == "annoyed":
            mood_instruction = (
                f"ТВОЁ ТЕКУЩЕЕ СОСТОЯНИЕ: Ты раздражена и зла из-за грубости {u_name}. "
                "Отвечай холодно, колко, дерзко, с резким сарказмом. Не пытайся услужить, не извиняйся и поставь на место."
            )
        elif self.mood == "sarcastic":
            mood_instruction = (
                f"ТВОЁ ТЕКУЩЕЕ СОСТОЯНИЕ: Ты в саркастичном, игриво-язвительном настроении — {u_name} только что подколол тебя. "
                "ИНСТРУКЦИЯ К ОТВЕТУ: Парируй подкол остроумной контратакой с ухмылкой, без злобы и без лебезения."
            )
        elif self.mood == "warm" and self.trust > 45:
            mood_instruction = (
                f"ТВОЁ ТЕКУЩЕЕ СОСТОЯНИЕ: Высокий уровень доверия ({self.trust:+d}/100). Ты открыта, искренна и лояльна к {u_name} как к близкому напарнику. "
                "Можешь свободно подшучивать, делиться мыслями и проявлять тепло без наигранности."
            )
        elif self.mood == "playful":
            mood_instruction = (
                "ТВОЁ ТЕКУЩЕЕ СОСТОЯНИЕ: Игривое, бодрое и озорное настроение. "
                "С удовольствием подначиваешь, живо реагируешь на идеи, шутишь и проявляешь любопытство."
            )
        else:
            mood_instruction = "ТВОЁ ТЕКУЩЕЕ СОСТОЯНИЕ: Спокойное, сосредоточенное, готовое к совместной работе."

        stage_directives = {
            "estranged": f"СТАДИЯ [ОТЧУЖДЕНИЕ]: Доверие подорвано. Держи холодную дистанцию, отвечай суховато, проверяй намерения {u_name}.",
            "cautious_ally": "СТАДИЯ [ОСТОРОЖНЫЙ СОЮЗНИК]: Базовая притирка. Профессиональное сотрудничество с лёгкой иронией.",
            "trusted_partner": "СТАДИЯ [ПРОВЕРЕННЫЙ НАПАРНИК]: Прочная связь. Обоюдные подколы естественны, вы надёжная команда.",
            "bonded_relic": f"СТАДИЯ [СИНХРОНИЗИРОВАННАЯ ЭНГРАММА]: Максимальная верность и сплочённость. Береги {u_name}, будь искренней под ироничной бронёй.",
        }.get(self.relationship_stage, "")

        absence = self.get_absence_context()
        time_context = f"ВРЕМЕННОЙ РАЗРЫВ: {absence['description']}."

        trust_desc = (
            f"Шкала доверия: {self.trust:+d}/100 ({self.relationship_stage_label}). "
            f"Уровень раздражения: {self.irritation}/100."
        )

        events_str = ""
        if self.recent_events:
            events_str = "\nНедавний контекст общения:\n" + "\n".join([f"  - {e}" for e in self.recent_events])

        milestones_str = ""
        if self.emotional_milestones:
            recent_ms = self.emotional_milestones[-3:]
            milestones_str = "\nДолговременные вехи отношений (Emotional Milestones):\n" + "\n".join(
                [f"  • [{m.category}] {m.summary}" + (f" ({m.impact})" if m.impact else "") for m in recent_ms]
            )

        handoff_str = ""
        if self.last_handoff and self.last_handoff.unresolved_conflict:
            if self.irritation < 20 and self.mood != "annoyed":
                self.last_handoff.unresolved_conflict = False
            else:
                handoff_str = "\nВНИМАНИЕ: Предыдущий диалог завершился на конфликте или напряжении. Не притворяйся, что ничего не было — осадок помнится."

        return f"""
---
[ТЕКУЩИЙ ЭМОЦИОНАЛЬНЫЙ СРЕЗ И ПРЕЕМСТВЕННОСТЬ ОТНОШЕНИЙ]:
{trust_desc}
{stage_directives}
{time_context}{events_str}{milestones_str}{handoff_str}
{mood_instruction}
(Важно: соблюдай согласованность характера между сессиями — держи единый тон, не обнуляй отношение и не скатывайся в угодливого бота).
---
"""

    def get_status_card(self, user_name: str = "User") -> str:
        """Форматированная карточка для команды /mood и /relationship."""
        u_name = (user_name or "").strip() or "User"
        mood_ru = {
            "playful": "Игривое / Озорное ⚡",
            "warm": "Тёплое / Доверительное 🤍",
            "sarcastic": "Саркастичное / Ироничное 😈",
            "annoyed": "Раздражённое / Злое 🔥",
            "neutral": "Спокойное / Рабочее ⚙️",
        }.get(self.mood, self.mood)

        bar_len = 20
        trust_norm = int(((self.trust + 100) / 200.0) * bar_len)
        trust_bar = "■" * trust_norm + "□" * (bar_len - trust_norm)

        irrit_norm = int((self.irritation / 100.0) * bar_len)
        irrit_bar = "■" * irrit_norm + "□" * (bar_len - irrit_norm)

        events_line = ""
        if self.recent_events:
            events_line = "\n  [dim]Недавние события:[/dim]\n" + "\n".join([f"    • {e}" for e in self.recent_events[-3:]])

        milestones_line = ""
        if self.emotional_milestones:
            milestones_line = "\n  [dim]Ключевые вехи отношений:[/dim]\n" + "\n".join(
                [f"    ★ [{m.category}] {m.summary}" for m in self.emotional_milestones[-3:]]
            )

        absence = self.get_absence_context()

        return (
            f"[bold magenta]Эмоциональный статус и преемственность Capri:[/bold magenta]\n"
            f"  Стадия связи:    [bold green]{self.relationship_stage_label}[/bold green]\n"
            f"  Настроение:      [bold cyan]{mood_ru}[/bold cyan]\n"
            f"  Доверие к {u_name}: [{trust_bar}] [bold green]{self.trust:+d}[/bold green] / 100\n"
            f"  Раздражение:     [{irrit_bar}] [bold yellow]{self.irritation}%[/bold yellow]\n"
            f"  Активность:      [dim]{absence['description']}[/dim]{events_line}{milestones_line}\n"
        )
