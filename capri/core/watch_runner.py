"""
Capri v3.6 — `capri --watch <cmd>` Build & Task Companion Runner.

Runs a developer command with native TTY passthrough (preserving compiler/test colors & progress bars),
signals the Wayland desktop avatar on start and completion, sends an optional desktop notification,
and speaks a concise completion/failure summary via VoiceSynthesizer (unless `--no-voice` or Ctrl+C).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

from .config import AppConfig, load_config
from ..ui.client import notify_avatar
from ..ui.i18n import is_english_ui


@dataclass(frozen=True)
class WatchResult:
    command_display: str
    returncode: int
    elapsed_seconds: float
    interrupted: bool


def format_duration_human(seconds: float, *, english: bool = False) -> str:
    sec = max(0.0, float(seconds))
    if sec < 60.0:
        return f"{sec:.1f}s" if english else f"{sec:.1f}с"
    mins, rem = divmod(int(round(sec)), 60)
    if mins < 60:
        return f"{mins}m {rem}s" if english else f"{mins}м {rem}с"
    hrs, mins = divmod(mins, 60)
    return f"{hrs}h {mins}m" if english else f"{hrs}ч {mins}м"


def shorten_command_label(cmd_display: str, max_len: int = 38) -> str:
    clean = " ".join((cmd_display or "").strip().split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1].rstrip() + "…"


def parse_watch_cli_args(args: list[str]) -> tuple[list[str], bool, bool, bool]:
    """
    Extract `(cmd_tokens, use_shell, no_voice, no_notify)` from CLI `args` containing `--watch` / `watch`.
    Supports both:
      - `capri --watch "cargo build --release"`  -> `use_shell=True` (single shell command string)
      - `capri --watch -- pytest -q`             -> `use_shell=False` (direct exec argv)
      - `capri --watch pytest -q`                -> `use_shell=False` (direct exec argv)
    """
    no_voice = False
    no_notify = False
    watch_idx = -1
    for i, tok in enumerate(args):
        if tok in ("--watch", "watch"):
            watch_idx = i
            break
        if tok == "--no-voice":
            no_voice = True
        elif tok == "--no-notify":
            no_notify = True

    if watch_idx < 0:
        return [], False, no_voice, no_notify

    tail = list(args[watch_idx + 1 :])
    cleaned_tail: list[str] = []
    after_double_dash = False
    for tok in tail:
        if after_double_dash:
            cleaned_tail.append(tok)
            continue
        if tok == "--":
            after_double_dash = True
            continue
        if tok == "--no-voice":
            no_voice = True
            continue
        if tok == "--no-notify":
            no_notify = True
            continue
        cleaned_tail.append(tok)

    if not cleaned_tail:
        return [], False, no_voice, no_notify

    # Single string containing shell operators/spaces runs via user's $SHELL -c; multi-token runs direct exec
    if len(cleaned_tail) == 1 and not after_double_dash:
        return cleaned_tail, True, no_voice, no_notify
    return cleaned_tail, False, no_voice, no_notify


def _send_desktop_notification(title: str, body: str, *, critical: bool = False) -> None:
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    notify_bin = shutil.which("notify-send")
    if not notify_bin:
        return
    urgency = "normal" if not critical else "critical"
    try:
        subprocess.run(
            [notify_bin, "-a", "Capri", "-u", urgency, "-t", "6000", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
    except Exception:
        pass


def build_watch_alert_messages(
    result: WatchResult,
    *,
    user_name: str = "User",
    english: bool = False,
) -> tuple[str, str, str, str]:
    """Return `(avatar_state, avatar_caption, notify_title, tts_phrase)` for a finished watch run."""
    short_cmd = shorten_command_label(result.command_display, max_len=34)
    spoken_cmd = shorten_command_label(result.command_display.split()[0] if result.command_display.strip() else "task", max_len=24)
    dur_str = format_duration_human(result.elapsed_seconds, english=english)
    uname = (user_name or "User").strip() or "User"

    if result.interrupted:
        caption = f"⏹ Stopped ({dur_str}): {short_cmd}" if english else f"⏹ Прервано ({dur_str}): {short_cmd}"
        title = "Capri // Watch Stopped" if english else "Capri // Задача остановлена"
        return "idle", caption, title, ""

    if result.returncode == 0:
        caption = f"✅ Done in {dur_str}: {short_cmd}" if english else f"✅ Готово за {dur_str}: {short_cmd}"
        title = "Capri // Task Complete" if english else "Capri // Задача завершена"
        speech = (
            f"All done, {uname}. {spoken_cmd} finished cleanly in {dur_str}."
            if english
            else f"Готово, {uname}. Команда {spoken_cmd} завершилась успешно за {dur_str}."
        )
        return "playful", caption, title, speech

    rc = result.returncode
    caption = (
        f"⚠ Exit {rc} ({dur_str}): {short_cmd}"
        if english
        else f"⚠ Ошибка {rc} ({dur_str}): {short_cmd}"
    )
    title = f"Capri // Task Failed (exit {rc})" if english else f"Capri // Сбой задачи (код {rc})"
    speech = (
        f"Heads up, {uname}. {spoken_cmd} failed with exit code {rc} after {dur_str}."
        if english
        else f"Эй, {uname}, команда {spoken_cmd} завершилась с ошибкой {rc} через {dur_str}."
    )
    return "thinking", caption, title, speech


def run_watched_command(
    cmd_tokens: list[str],
    *,
    use_shell: bool = False,
    no_voice: bool = False,
    no_notify: bool = False,
    config: Optional[AppConfig] = None,
) -> WatchResult:
    """Execute `cmd_tokens`, forward signals cleanly, and trigger Capri's completion alert."""
    cfg = config or load_config()
    english = is_english_ui(cfg)
    user_name = getattr(cfg.companion, "user_name", "User") or "User"
    cmd_display = cmd_tokens[0] if use_shell else " ".join(cmd_tokens)
    short_cmd = shorten_command_label(cmd_display, max_len=36)

    start_caption = f"⚙ Watching: {short_cmd}" if english else f"⚙ Слежу: {short_cmd}"
    notify_avatar("idle", start_caption)

    if use_shell:
        shell_bin = os.environ.get("SHELL") or "/bin/sh"
        popen_cmd = [shell_bin, "-c", cmd_tokens[0]]
    else:
        popen_cmd = list(cmd_tokens)

    t0 = time.monotonic()
    interrupted = False
    try:
        proc = subprocess.Popen(popen_cmd)
    except OSError as exc:
        print(f"[Capri --watch] {exc}", file=sys.stderr)
        res_err = WatchResult(
            command_display=cmd_display,
            returncode=127,
            elapsed_seconds=0.0,
            interrupted=False,
        )
        av_st, av_cap, _, _ = build_watch_alert_messages(res_err, user_name=user_name, english=english)
        notify_avatar(av_st, av_cap)
        return res_err
    prev_sigint = signal.getsignal(signal.SIGINT)

    def _forward_sigint(signum, frame):
        nonlocal interrupted
        interrupted = True
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass

    try:
        signal.signal(signal.SIGINT, _forward_sigint)
        rc = proc.wait()
    except KeyboardInterrupt:
        interrupted = True
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                rc = proc.wait(timeout=2.0)
            except Exception:
                proc.kill()
                rc = proc.wait()
        else:
            rc = proc.returncode or 130
    finally:
        try:
            signal.signal(signal.SIGINT, prev_sigint)
        except Exception:
            pass

    if interrupted and rc < 0:
        rc = 128 + abs(rc)
    elif interrupted and rc == 0:
        rc = 130

    elapsed = max(0.0, time.monotonic() - t0)
    res = WatchResult(
        command_display=cmd_display,
        returncode=int(rc),
        elapsed_seconds=elapsed,
        interrupted=interrupted,
    )

    av_state, av_caption, notif_title, speech_text = build_watch_alert_messages(
        res,
        user_name=user_name,
        english=english,
    )
    notify_avatar(av_state, av_caption)

    if not no_notify and not res.interrupted:
        _send_desktop_notification(notif_title, av_caption, critical=False)

    if (
        speech_text
        and not no_voice
        and not res.interrupted
        and bool(getattr(cfg.voice, "tts_enabled", True))
    ):
        try:
            from ..voice.tts import VoiceSynthesizer

            tts = VoiceSynthesizer(cfg.voice)
            asyncio.run(asyncio.wait_for(tts.speak(speech_text), timeout=25.0))
        except Exception:
            pass

    return res
