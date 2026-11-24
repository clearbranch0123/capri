"""
Capri v3.6 — Focus / Pomodoro Timer State Machine.

Provides a deterministic monotonic countdown controller for the desktop avatar and Cyberdeck:
- Preset cycle: OFF -> 25m -> 50m -> 15m -> OFF (plus arbitrary custom minutes/seconds)
- Progress ratio (`0.0..1.0`) for Capri's holographic Focus progress ring
- Automatic silence gate (`is_silent_gate_active`) suppressing proactive chatter and autonomous walks
  until the focus block completes
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional


FOCUS_PRESET_MINUTES: tuple[int, ...] = (25, 50, 15)


@dataclass(frozen=True)
class FocusSnapshot:
    active: bool
    preset_minutes: int
    total_seconds: float
    remaining_seconds: float
    progress: float  # 0.0 at start -> 1.0 at completion
    mm_ss: str


class FocusTimerController:
    """Monotonic countdown timer with preset cycling and exact one-shot completion detection."""

    def __init__(self) -> None:
        self._active: bool = False
        self._preset_minutes: int = 0
        self._total_seconds: float = 0.0
        self._start_mono: float = 0.0
        self._end_mono: float = 0.0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def preset_minutes(self) -> int:
        return self._preset_minutes

    @property
    def is_silent_gate_active(self) -> bool:
        """True while a focus block is running — suppresses unsolicited chatter and random walks."""
        return self._active

    def start(self, minutes: float = 25.0, *, now: Optional[float] = None) -> FocusSnapshot:
        dur = max(1.0, float(minutes) * 60.0)
        t0 = time.monotonic() if now is None else float(now)
        self._active = True
        self._preset_minutes = max(1, int(round(float(minutes))))
        self._total_seconds = dur
        self._start_mono = t0
        self._end_mono = t0 + dur
        return self.snapshot(now=t0)

    def start_seconds(self, seconds: float, *, now: Optional[float] = None) -> FocusSnapshot:
        dur = max(1.0, float(seconds))
        t0 = time.monotonic() if now is None else float(now)
        self._active = True
        self._preset_minutes = max(1, int(math.ceil(dur / 60.0)))
        self._total_seconds = dur
        self._start_mono = t0
        self._end_mono = t0 + dur
        return self.snapshot(now=t0)

    def stop(self, *, now: Optional[float] = None) -> FocusSnapshot:
        self._active = False
        self._preset_minutes = 0
        self._total_seconds = 0.0
        self._start_mono = 0.0
        self._end_mono = 0.0
        return self.snapshot(now=now)

    def cycle_preset(self, *, now: Optional[float] = None) -> FocusSnapshot:
        """Cycle OFF -> 25m -> 50m -> 15m -> OFF."""
        t0 = time.monotonic() if now is None else float(now)
        if not self._active:
            return self.start(FOCUS_PRESET_MINUTES[0], now=t0)
        if self._preset_minutes in FOCUS_PRESET_MINUTES:
            idx = FOCUS_PRESET_MINUTES.index(self._preset_minutes)
            if idx + 1 < len(FOCUS_PRESET_MINUTES):
                return self.start(FOCUS_PRESET_MINUTES[idx + 1], now=t0)
        return self.stop(now=t0)

    def remaining_seconds(self, *, now: Optional[float] = None) -> float:
        if not self._active:
            return 0.0
        t = time.monotonic() if now is None else float(now)
        return max(0.0, self._end_mono - t)

    def tick(self, *, now: Optional[float] = None) -> bool:
        """Advance timer and return True exactly once when an active focus session finishes."""
        if not self._active:
            return False
        t = time.monotonic() if now is None else float(now)
        if t >= self._end_mono:
            self.stop(now=t)
            return True
        return False

    @staticmethod
    def format_mm_ss(seconds: float) -> str:
        rem = max(0, int(math.ceil(float(seconds))))
        mins, secs = divmod(rem, 60)
        if mins >= 100:
            hrs, mins = divmod(mins, 60)
            return f"{hrs}:{mins:02d}:{secs:02d}"
        return f"{mins:02d}:{secs:02d}"

    def snapshot(self, *, now: Optional[float] = None) -> FocusSnapshot:
        if not self._active or self._total_seconds <= 0.0:
            return FocusSnapshot(
                active=False,
                preset_minutes=0,
                total_seconds=0.0,
                remaining_seconds=0.0,
                progress=0.0,
                mm_ss="OFF",
            )
        rem = self.remaining_seconds(now=now)
        prog = max(0.0, min(1.0, 1.0 - (rem / self._total_seconds)))
        return FocusSnapshot(
            active=True,
            preset_minutes=self._preset_minutes,
            total_seconds=self._total_seconds,
            remaining_seconds=rem,
            progress=prog,
            mm_ss=self.format_mm_ss(rem),
        )
