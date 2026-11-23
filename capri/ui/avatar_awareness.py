"""
Capri v3.4 — Living Awareness Engine (calm, zero-spam desktop micro-reactions).

Pure-Python deterministic evaluator that produces occasional, tasteful avatar speech-bubble
micro-reactions for meaningful moments (returning from long AFK, late-night care, long focus
stretches) while strictly respecting Game Mode, `focus`/`quiet` companion modes, active voice
states, and generous per-kind + global cooldowns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class AwarenessCue:
    kind: str  # "none" | "afk_return" | "late_night" | "long_session"
    text: str = ""


class AvatarAwarenessEngine:
    """Rate-limited, distraction-free desktop awareness micro-reaction planner."""

    def __init__(
        self,
        *,
        user_name: str = "User",
        afk_threshold_sec: float = 2700.0,         # 45 minutes
        long_session_threshold_sec: float = 5400.0,  # 90 minutes
        global_cooldown_sec: float = 1800.0,       # 30 minutes between any two cues
        late_night_cooldown_sec: float = 21600.0,  # 6 hours (at most once per night)
        afk_cooldown_sec: float = 7200.0,          # 2 hours
        session_cooldown_sec: float = 7200.0,      # 2 hours
        now: Optional[float] = None,
    ) -> None:
        self.user_name = (user_name or "User").strip() or "User"
        self.afk_threshold_sec = max(60.0, float(afk_threshold_sec))
        self.long_session_threshold_sec = max(300.0, float(long_session_threshold_sec))
        self.global_cooldown_sec = max(60.0, float(global_cooldown_sec))
        self.late_night_cooldown_sec = max(600.0, float(late_night_cooldown_sec))
        self.afk_cooldown_sec = max(300.0, float(afk_cooldown_sec))
        self.session_cooldown_sec = max(300.0, float(session_cooldown_sec))

        init_ts = float(now) if now is not None else 0.0
        self.session_start_ts: float = init_ts
        self.last_active_ts: float = init_ts
        self.last_any_cue_ts: float = -1e9
        self.last_kind_ts: dict[str, float] = {}
        self._pending_afk_return: bool = False

    def note_user_activity(self, now: float) -> None:
        """Record active user interaction; flags an AFK return if away >= afk_threshold_sec."""
        ts = float(now)
        if self.last_active_ts <= 0.0:
            self.session_start_ts = ts
            self.last_active_ts = ts
            return
        away_sec = ts - self.last_active_ts
        if away_sec >= self.afk_threshold_sec:
            self._pending_afk_return = True
            self.session_start_ts = ts
        self.last_active_ts = ts

    def _can_emit(self, kind: str, now: float, kind_cooldown: float) -> bool:
        if (now - self.last_any_cue_ts) < self.global_cooldown_sec:
            return False
        prev = self.last_kind_ts.get(kind, -1e9)
        return (now - prev) >= kind_cooldown

    def _record_emit(self, kind: str, now: float) -> None:
        self.last_any_cue_ts = now
        self.last_kind_ts[kind] = now

    def tick(
        self,
        *,
        now: float,
        local_hour: Optional[int] = None,
        reactions_enabled: bool = True,
        game_mode_active: bool = False,
        companion_mode: str = "normal",
        avatar_state: str = "idle",
        physics_state: str = "idle",
        has_active_caption: bool = False,
    ) -> AwarenessCue:
        """Evaluate whether a calm micro-reaction cue should appear right now."""
        ts = float(now)
        if self.session_start_ts <= 0.0:
            self.session_start_ts = ts
            self.last_active_ts = ts

        # Strict silence gates — never interrupt games, focus/quiet modes, voice, or physics
        mode = (companion_mode or "normal").strip().lower()
        if (
            not reactions_enabled
            or game_mode_active
            or mode in ("focus", "quiet")
            or (avatar_state or "idle").strip().lower() != "idle"
            or (physics_state or "idle").strip().lower() != "idle"
            or has_active_caption
        ):
            return AwarenessCue("none")

        uname = self.user_name
        addr = f", {uname}" if uname and uname.lower() != "user" else ""

        # 1. Return from long AFK
        if self._pending_afk_return:
            self._pending_afk_return = False
            if self._can_emit("afk_return", ts, self.afk_cooldown_sec):
                self._record_emit("afk_return", ts)
                return AwarenessCue(
                    "afk_return",
                    f"С возвращением{addr}. Системы держала в тонусе.",
                )

        # 2. Quiet late-night check-in (01:00..04:59 local time, at most once per night)
        hour = datetime.fromtimestamp(ts).hour if local_hour is None else int(local_hour)
        if 1 <= hour <= 4 and self._can_emit("late_night", ts, self.late_night_cooldown_sec):
            if (ts - self.session_start_ts) >= 300.0:
                self._record_emit("late_night", ts)
                return AwarenessCue(
                    "late_night",
                    f"Уже глубокая ночь{addr} — не загоняй себя, сон важнее.",
                )

        # 3. Gentle long focus stretch reminder (>= 90 min continuous session)
        active_span = ts - self.session_start_ts
        recently_active = (ts - self.last_active_ts) <= 600.0
        if (
            recently_active
            and active_span >= self.long_session_threshold_sec
            and self._can_emit("long_session", ts, self.session_cooldown_sec)
        ):
            self._record_emit("long_session", ts)
            return AwarenessCue(
                "long_session",
                f"Уже полтора часа без паузы{addr} — глоток воды и минутка для глаз не помешают.",
            )

        return AwarenessCue("none")
