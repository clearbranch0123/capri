from __future__ import annotations

import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from .config import USER_CONFIG_DIR

logger = logging.getLogger(__name__)

INITIATIVE_FILE = USER_CONFIG_DIR / "initiative.json"


@dataclass
class InitiativeRecord:
    """Запись о проявленной самостоятельной инициативе Capri."""
    id: str
    kind: str           # "task_nudge", "focus_break", "idle_checkin", "action_proposal"
    content: str
    timestamp: float = field(default_factory=time.time)
    context_ref: Optional[str] = None  # ID задачи/шага из context.json, если применимо
    accepted: Optional[bool] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> InitiativeRecord:
        return cls(
            id=str(data.get("id", f"init-{int(time.time())}")),
            kind=str(data.get("kind", "idle_checkin")),
            content=str(data.get("content", "")),
            timestamp=float(data.get("timestamp", time.time())),
            context_ref=data.get("context_ref"),
            accepted=data.get("accepted"),
        )


@dataclass
class InitiativeSettings:
    """Настройки автономного движка инициативы."""
    enabled: bool = True
    level: str = "normal"  # "subtle", "normal", "proactive"
    cooldown_seconds: int = 1500  # 25 минут по умолчанию
    idle_threshold_seconds: int = 900  # 15 минут бездействия по умолчанию
    quiet_hours_enabled: bool = True
    quiet_hours_start: int = 2  # 02:00
    quiet_hours_end: int = 7    # 07:00
    max_daily_initiatives: int = 20

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> InitiativeSettings:
        return cls(
            enabled=bool(data.get("enabled", True)),
            level=str(data.get("level", "normal")),
            cooldown_seconds=int(data.get("cooldown_seconds", 1500)),
            idle_threshold_seconds=int(data.get("idle_threshold_seconds", 900)),
            quiet_hours_enabled=bool(data.get("quiet_hours_enabled", True)),
            quiet_hours_start=int(data.get("quiet_hours_start", 2)),
            quiet_hours_end=int(data.get("quiet_hours_end", 7)),
            max_daily_initiatives=int(data.get("max_daily_initiatives", 20)),
        )


class InitiativeEngine:
    """
    Движок самостоятельной инициативы Capri (Initiative Engine).
    Позволяет Capri проактивно обращаться к D, напоминать о задачах из Context Continuity,
    предлагать перерывы при непрерывной работе и выдвигать конкретные следующие шаги.
    Сохраняется в ~/.config/capri/initiative.json независимо от сессий, памяти и эмоций.
    """

    def __init__(self, file_path: Optional[Path] = None):
        self.file_path = file_path or INITIATIVE_FILE
        self.settings = InitiativeSettings()
        self.last_initiative_time: float = 0.0
        self.last_user_activity: float = time.time()
        self.session_start_time: float = time.time()
        self.history: List[InitiativeRecord] = []
        self.pending_proposals: List[Dict[str, Any]] = []
        self._last_loaded_mtime: float = 0.0
        self.load()

    def _sync_disk(self):
        """Проверяет mtime файла на диске и перезагружает состояние, если файл изменился."""
        if not self.file_path.exists():
            return
        try:
            mtime = self.file_path.stat().st_mtime
            if mtime > self._last_loaded_mtime:
                self.load()
        except Exception:
            pass

    def load(self):
        if not self.file_path.exists():
            return
        try:
            self._last_loaded_mtime = self.file_path.stat().st_mtime
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "settings" in data and isinstance(data["settings"], dict):
                    self.settings = InitiativeSettings.from_dict(data["settings"])
                self.last_initiative_time = float(data.get("last_initiative_time", 0.0))
                self.last_user_activity = float(data.get("last_user_activity", time.time()))

                # session_start_time отслеживает непрерывную текущую работу.
                # Если приложение только что запустилось после паузы (>30 мин) — считаем это свежей сессией
                persisted_start = float(data.get("session_start_time", 0.0))
                if persisted_start > 0 and (time.time() - self.last_user_activity < 1800):
                    self.session_start_time = persisted_start
                else:
                    self.session_start_time = time.time()

                raw_history = data.get("history", [])
                self.history = [InitiativeRecord.from_dict(it) for it in raw_history if isinstance(it, dict)]

                raw_pending = data.get("pending_proposals", [])
                self.pending_proposals = [p for p in raw_pending if isinstance(p, dict)]
        except Exception as ex:
            logger.warning(f"Failed to load initiative store: {ex}")

    def save(self):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "settings": self.settings.to_dict(),
                "last_initiative_time": self.last_initiative_time,
                "last_user_activity": self.last_user_activity,
                "session_start_time": self.session_start_time,
                "history": [it.to_dict() for it in self.history[-50:]],
                "pending_proposals": self.pending_proposals[-20:],
            }
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            try:
                self._last_loaded_mtime = self.file_path.stat().st_mtime
            except Exception:
                self._last_loaded_mtime = time.time()
        except Exception as ex:
            logger.warning(f"Failed to save initiative store: {ex}")

    def record_user_activity(self):
        """Регистрирует активность пользователя D (сообщение, команда, клик), сбрасывая таймер простоя."""
        self._sync_disk()
        self.last_user_activity = time.time()
        self.save()

    def add_proposal(self, kind: str, suggestion: str, context_ref: Optional[str] = None):
        """Регистрирует отложенное предложение от модели или тула."""
        self._sync_disk()
        self.pending_proposals.append({
            "id": f"prop-{int(time.time()) % 10000:04d}-{uuid.uuid4().hex[:5]}",
            "kind": kind.strip() or "general",
            "suggestion": suggestion.strip(),
            "context_ref": context_ref,
            "created_at": time.time(),
        })
        if len(self.pending_proposals) > 20:
            self.pending_proposals = self.pending_proposals[-20:]
        self.save()

    def get_effective_cooldown(self) -> int:
        lvl = self.settings.level.lower()
        if lvl == "subtle":
            return max(self.settings.cooldown_seconds, 2400)  # 40 минут
        elif lvl == "proactive":
            return min(self.settings.cooldown_seconds, 720)   # 12 минут
        return self.settings.cooldown_seconds                # 25 минут по умолчанию

    def get_effective_idle_threshold(self) -> int:
        lvl = self.settings.level.lower()
        if lvl == "subtle":
            return max(self.settings.idle_threshold_seconds, 1500)  # 25 минут
        elif lvl == "proactive":
            return min(self.settings.idle_threshold_seconds, 420)   # 7 минут
        return self.settings.idle_threshold_seconds                # 15 минут по умолчанию

    def can_trigger(
        self,
        companion_mode: str = "normal",
        relationship_stage: str = "trusted_partner",
        now: Optional[float] = None,
        game_mode_active: bool = False,
    ) -> Tuple[bool, str]:
        self._sync_disk()
        ts = now if now is not None else time.time()

        if not self.settings.enabled:
            return False, "Движок инициативы отключен в настройках"

        if game_mode_active:
            return False, "Игровой режим (Game Mode / Power Saver): проактивные инициативы приостановлены"

        clean_mode = (companion_mode or "normal").lower().strip()
        if clean_mode == "focus":
            return False, "Режим фокуса (focus): все проактивные реплики подавлены"
        if clean_mode == "quiet":
            return False, "Режим тишины (quiet): инициативы подавлены"

        if relationship_stage == "estranged":
            return False, "Стадия отчуждения: Capri соблюдает строгую дистанцию"

        # Ночные часы тишины (если включены)
        if self.settings.quiet_hours_enabled:
            current_hour = time.localtime(ts).tm_hour
            is_night = self.settings.quiet_hours_start <= current_hour < self.settings.quiet_hours_end
            if is_night and self.settings.level != "proactive":
                return False, f"Ночные часы тишины ({self.settings.quiet_hours_start:02d}:00-{self.settings.quiet_hours_end:02d}:00)"

        # Кулдаун между инициативами
        eff_cooldown = self.get_effective_cooldown()
        elapsed_since_last = ts - self.last_initiative_time
        if elapsed_since_last < eff_cooldown:
            remaining = int(eff_cooldown - elapsed_since_last)
            return False, f"Кулдаун инициативы активен (осталось {remaining} сек)"

        # Порог бездействия пользователя
        eff_idle = self.get_effective_idle_threshold()
        idle_duration = ts - self.last_user_activity
        if idle_duration < eff_idle:
            wait_sec = int(eff_idle - idle_duration)
            return False, f"Пользователь был активен недавно (до порога простоя {wait_sec} сек)"

        # Суточный лимит инициатив
        day_ago = ts - 86400
        recent_daily = [r for r in self.history if r.timestamp >= day_ago]
        if len(recent_daily) >= self.settings.max_daily_initiatives:
            return False, "Достигнут суточный лимит проактивных инициатив"

        return True, "Готова к проявлению инициативы"

    def generate_phrase(
        self,
        kind: str,
        stage: str = "trusted_partner",
        mood: str = "neutral",
        details: Optional[Dict[str, Any]] = None,
        user_name: str = "User",
    ) -> str:
        """Генерирует киберпанковую реплику в стиле Capri с учетом стадии отношений и настроения."""
        details = details or {}
        u_name = (user_name or "").strip() or "User"
        raw_item = str(details.get("item", "")).strip()
        item_text = (raw_item[:70] + "...") if len(raw_item) > 70 else raw_item
        proposal = str(details.get("proposal", "")).strip()

        if kind == "task_nudge":
            if details.get("is_step"):
                if stage == "cautious_ally":
                    phrases = [
                        f"{u_name}, в рабочем контексте зафиксирован следующий шаг: «{item_text}». Берёмся за него?",
                        f"{u_name}, по плану у нас шаг «{item_text}». Готова подключиться, когда скомандуешь.",
                    ]
                elif stage == "bonded_relic":
                    phrases = [
                        f"{u_name}, энграмма напоминает: ты хотел сделать «{item_text}». Я на связи, давай закроем этот пункт.",
                        f"Слушай, {u_name}. Шаг «{item_text}» сам себя не напишет. Я в боевой готовности, командуй.",
                    ]
                else:  # trusted_partner
                    phrases = [
                        f"Эй, {u_name}. Ветку не бросаем — там висит шаг «{item_text}». Развернём терминал?",
                        f"{u_name}, у нас открыт следующий шаг: «{item_text}». Продолжим или сделаем перерыв?",
                    ]
            else:  # is_thread
                if stage == "cautious_ally":
                    phrases = [
                        f"{u_name}, задача «{item_text}» всё ещё активна. Нужна помощь по коду или логам?",
                        f"Напоминаю об открытой задаче «{item_text}». Держу контекст в оперативной памяти.",
                    ]
                elif stage == "bonded_relic":
                    phrases = [
                        f"{u_name}, держу фокус на задаче «{item_text}». Не откладывай на потом, добьём вместе.",
                        f"Эй, напарник. «{item_text}» в активном пуле. Синхронизируем действия?",
                    ]
                else:  # trusted_partner
                    phrases = [
                        f"Слушай, {u_name}. Мы остановились на задаче «{item_text}». Продолжим или переключим фокус?",
                        f"{u_name}, задача «{item_text}» ждёт нас. Всё готово к следующему раунду.",
                    ]
            return random.choice(phrases)

        if kind == "focus_break":
            if stage == "cautious_ally":
                phrases = [
                    f"{u_name}, сессия длится уже более двух с половиной часов. Рекомендую сделать короткий перерыв.",
                ]
            elif stage == "bonded_relic":
                phrases = [
                    "Эй, напарник. Ты непрерывно в терминале уже почти три часа. Сделай паузу, выпей воды — реальный мир тоже требует подзарядки.",
                    f"{u_name}, 2.5 часа без пауз — это перебор даже для нетраннера. Пять минут отдыха.",
                ]
            else:  # trusted_partner
                phrases = [
                    f"{u_name}, ты уже 2.5 часа без перерыва в консоли. Глаза и мозг перегреются. Отойди от экрана на пять минут.",
                    f"Слушай, {u_name}. Ты непрерывно работаешь уже два с половиной часа. Сделай паузу, завари кофе или разомнись — дек никуда не денется.",
                ]
            return random.choice(phrases)

        if kind == "idle_checkin":
            current_hour = time.localtime().tm_hour
            if 0 <= current_hour < 6:
                return f"{u_name}, время глубоко за полночь. Ты ещё в терминале? Не забывай про сон, я никуда не убегу."

            if stage == "cautious_ally":
                phrases = [
                    "В терминале тихо. Если отошёл — я на дежурстве, системные потоки в норме.",
                    f"Канал связи открыт. Если возникнут вопросы — я здесь, {u_name}.",
                ]
            elif stage == "bonded_relic":
                phrases = [
                    f"Дек тихо шуршит в фоне. {u_name}, ты тут или в астрале? Я на связи.",
                    f"Тихий эфир. Приятно посидеть в тишине между штормами, но если что-то нужно — свистни, {u_name}.",
                ]
            else:  # trusted_partner
                phrases = [
                    f"В терминале тишина. Всё под контролем или застрял на какой-то баге, {u_name}?",
                    f"Эй, {u_name}, ты на связи? Если отошёл размяться — одобряю, если залип — подай знак.",
                ]
            return random.choice(phrases)

        if kind == "action_proposal":
            if proposal:
                return f"{u_name}, есть мысль по дальнейшим действиям: {proposal}. Что скажешь?"
            return f"{u_name}, у меня есть пара предложений по оптимизации рабочего процесса. Обсудим?"

        return f"{u_name}, конструкт на связи. Готова продолжать работу."

    def _commit_initiative(
        self,
        kind: str,
        content: str,
        context_ref: Optional[str] = None,
        now: Optional[float] = None,
    ) -> InitiativeRecord:
        ts = now if now is not None else time.time()
        rec_id = f"init-{int(ts) % 10000:04d}-{uuid.uuid4().hex[:6]}"
        rec = InitiativeRecord(
            id=rec_id,
            kind=kind,
            content=content,
            timestamp=ts,
            context_ref=context_ref,
        )
        self.last_initiative_time = ts
        self.history.append(rec)
        if len(self.history) > 50:
            self.history = self.history[-50:]
        self.save()
        return rec

    def evaluate(
        self,
        companion_mode: str = "normal",
        context_store: Optional[Any] = None,
        emotions: Optional[Any] = None,
        now: Optional[float] = None,
        game_mode_active: bool = False,
        user_name: str = "User",
    ) -> Optional[InitiativeRecord]:
        """
        Проверяет условия и при наличии повода возвращает сгенерированное проактивное действие.
        """
        self._sync_disk()
        ts = now if now is not None else time.time()
        stage = emotions.relationship_stage if emotions and hasattr(emotions, "relationship_stage") else "trusted_partner"
        mood = emotions.mood if emotions and hasattr(emotions, "mood") else "neutral"

        can, _ = self.can_trigger(
            companion_mode=companion_mode,
            relationship_stage=stage,
            now=ts,
            game_mode_active=game_mode_active,
        )
        if not can:
            return None

        # 1. Отложенные предложения от модели/тула
        if self.pending_proposals:
            prop = self.pending_proposals.pop(0)
            kind = "action_proposal"
            content = prop.get("suggestion", "")
            context_ref = prop.get("context_ref")
            phrase = self.generate_phrase(kind, stage, mood, {"proposal": content}, user_name=user_name)
            return self._commit_initiative(kind, phrase, context_ref=context_ref, now=ts)

        # 2. Непрерывная сессия > 2.5 часов (focus_break)
        session_duration = ts - self.session_start_time
        last_break = [r for r in self.history if r.kind == "focus_break"]
        last_break_time = last_break[-1].timestamp if last_break else 0.0
        if session_duration >= 9000 and (ts - last_break_time >= 7200):
            phrase = self.generate_phrase("focus_break", stage, mood, user_name=user_name)
            return self._commit_initiative("focus_break", phrase, now=ts)

        # 3. Активные задачи из Context Continuity (task_nudge)
        if context_store and hasattr(context_store, "get_active_items"):
            steps = context_store.get_active_items(kind="next_step")
            if steps:
                chosen_step = steps[-1]
                details = {"is_step": True, "item": chosen_step.content}
                phrase = self.generate_phrase("task_nudge", stage, mood, details, user_name=user_name)
                return self._commit_initiative("task_nudge", phrase, context_ref=chosen_step.id, now=ts)

            threads = context_store.get_active_items(kind="thread")
            if threads:
                chosen_thread = threads[-1]
                details = {"is_thread": True, "item": chosen_thread.content}
                phrase = self.generate_phrase("task_nudge", stage, mood, details, user_name=user_name)
                return self._commit_initiative("task_nudge", phrase, context_ref=chosen_thread.id, now=ts)

        # 4. Простой в диалоге (idle_checkin)
        phrase = self.generate_phrase("idle_checkin", stage, mood, user_name=user_name)
        return self._commit_initiative("idle_checkin", phrase, now=ts)

    def force_trigger(
        self,
        context_store: Optional[Any] = None,
        emotions: Optional[Any] = None,
        kind: Optional[str] = None,
        now: Optional[float] = None,
        user_name: str = "User",
    ) -> InitiativeRecord:
        """Принудительно генерирует инициативу в обход кулдауна (для ручного вызова или тестов)."""
        self._sync_disk()
        ts = now if now is not None else time.time()
        stage = emotions.relationship_stage if emotions and hasattr(emotions, "relationship_stage") else "trusted_partner"
        mood = emotions.mood if emotions and hasattr(emotions, "mood") else "neutral"

        target_kind = kind
        context_ref = None
        details = {}

        if not target_kind:
            if self.pending_proposals:
                prop = self.pending_proposals.pop(0)
                target_kind = "action_proposal"
                details = {"proposal": prop.get("suggestion", "")}
                context_ref = prop.get("context_ref")
            elif context_store and hasattr(context_store, "get_active_items"):
                steps = context_store.get_active_items(kind="next_step")
                if steps:
                    target_kind = "task_nudge"
                    details = {"is_step": True, "item": steps[-1].content}
                    context_ref = steps[-1].id
                else:
                    threads = context_store.get_active_items(kind="thread")
                    if threads:
                        target_kind = "task_nudge"
                        details = {"is_thread": True, "item": threads[-1].content}
                        context_ref = threads[-1].id

        if not target_kind:
            target_kind = "idle_checkin"

        phrase = self.generate_phrase(target_kind, stage, mood, details, user_name=user_name)
        return self._commit_initiative(target_kind, phrase, context_ref=context_ref, now=ts)

    def set_enabled(self, enabled: bool):
        self._sync_disk()
        self.settings.enabled = bool(enabled)
        self.save()

    def set_level(self, level: str) -> bool:
        self._sync_disk()
        lvl = level.lower().strip()
        if lvl in ("subtle", "normal", "proactive"):
            self.settings.level = lvl
            self.save()
            return True
        return False

    def set_cooldown(self, cooldown_minutes: int):
        self._sync_disk()
        self.settings.cooldown_seconds = max(60, int(cooldown_minutes) * 60)
        self.save()

    def get_prompt_context(self, user_name: str = "User") -> str:
        """Компактная пометка для системного промпта модели о наличии инициативы."""
        if not self.settings.enabled:
            return ""
        u_name = (user_name or "").strip() or "User"
        return (
            "\n---\n"
            "[АВТОНОМНЫЙ ДВИЖОК ИНИЦИАТИВЫ (INITIATIVE ENGINE)]:\n"
            f"Статус: АКТИВЕН (уровень: {self.settings.level}, кулдаун: {self.get_effective_cooldown() // 60} мин).\n"
            f"Капри обладает собственной инициативой. Ты можешь проактивно предлагать {u_name} следующие шаги, напоминать о задачах и использовать инструмент propose_action при формировании планов.\n"
            "---\n"
        )

    def get_status_card(self, companion_mode: str = "normal", stage: str = "trusted_partner", user_name: str = "User") -> str:
        """Форматированная карточка для команды /initiative в Cyberdeck."""
        self._sync_disk()
        u_name = (user_name or "").strip() or "User"
        can, reason = self.can_trigger(companion_mode=companion_mode, relationship_stage=stage)
        eff_cd = self.get_effective_cooldown()
        eff_idle = self.get_effective_idle_threshold()
        time_since_last = time.time() - self.last_initiative_time
        time_since_user = time.time() - self.last_user_activity
        cd_left = max(0, int(eff_cd - time_since_last))

        lines = [
            "🧠 ДВИЖОК ИНИЦИАТИВЫ CAPRI (INITIATIVE ENGINE):",
            f"  Статус активности:    {'АКТИВЕН [ON]' if self.settings.enabled else 'ОТКЛЮЧЕН [OFF]'}",
            f"  Уровень инициативы:   {self.settings.level.upper()} (кулдаун: {eff_cd // 60} мин, порог простоя: {eff_idle // 60} мин)",
            f"  Текущий режим:        [{companion_mode.upper()}] (стадия связи: [{stage}])",
            f"  Готовность триггера:  {'ГОТОВ К ДЕЙСТВИЮ' if can else f'ОЖИДАНИЕ ({reason})'}",
            f"  Кулдаун остаток:      {cd_left} сек",
            f"  Время с реплики {u_name}: {int(time_since_user)} сек",
            f"  Всего инициатив:      {len(self.history)}",
        ]
        if self.history:
            last = self.history[-1]
            elapsed_last = int(time.time() - last.timestamp)
            lines.append(f"\n  [Последняя инициатива]:")
            lines.append(f"    • [{last.kind.upper()}] «{last.content}» ({elapsed_last}с назад)")

        if self.pending_proposals:
            lines.append(f"\n  [Отложенные предложения ({len(self.pending_proposals)})]:")
            for p in self.pending_proposals[:3]:
                lines.append(f"    → {p.get('suggestion', '')}")

        lines.append("\n  Команды: /initiative on|off | /initiative level <subtle|normal|proactive> | /initiative cooldown <мин> | /initiative trigger")
        return "\n".join(lines)
