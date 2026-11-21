from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import TYPE_CHECKING, Optional
from ..ui.client import notify_avatar

if TYPE_CHECKING:
    from .agent import CapriAgent
    from .emotions import EmotionEngine

logger = logging.getLogger(__name__)


async def is_screen_locked() -> bool:
    """Проверяет, заблокирован ли экран (swaylock, hyprlock или LockedHint в systemd-logind)."""
    lockers = ("swaylock", "hyprlock", "waylock", "gtklock", "wlogout")
    for locker in lockers:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-x", locker,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            if out.strip():
                return True
        except Exception:
            pass

    try:
        proc = await asyncio.create_subprocess_shell(
            "sid=$(loginctl list-sessions --no-legend | awk 'NR==1{print $1}') && loginctl show-session \"$sid\" -p LockedHint",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if b"LockedHint=yes" in out:
            return True
    except Exception:
        pass

    return False


async def is_call_active() -> bool:
    """Проверяет, идёт ли сейчас голосовой/видео звонок или активная запись микрофона другим приложением."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "list", "source-outputs",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        text = out.decode("utf-8", errors="replace").strip()
        if not text:
            return False

        # Определяем Client ID самого процесса Capri в PipeWire по текущему PID
        my_pid = str(os.getpid())
        my_clients = set()
        c_proc = await asyncio.create_subprocess_exec(
            "pactl", "list", "clients",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        c_out, _ = await c_proc.communicate()
        clients_text = c_out.decode("utf-8", errors="replace")
        for cb in clients_text.split("Client #"):
            if not cb.strip():
                continue
            cid_m = re.match(r"(\d+)", cb)
            pid_m = re.search(r'application\.process\.id\s*=\s*"([^"]+)"', cb)
            if cid_m and pid_m and pid_m.group(1) == my_pid:
                my_clients.add(cid_m.group(1))

        # Анализируем входящие аудиопотоки
        blocks = text.split("Source Output #")
        for b in blocks:
            if not b.strip():
                continue
            client_match = re.search(r"Client:\s*(\d+)", b)
            pid_match = re.search(r'application\.process\.id\s*=\s*"([^"]+)"', b)
            cid = client_match.group(1) if client_match else None
            pid = pid_match.group(1) if pid_match else None

            # Игнорируем собственные потоки Capri
            if (cid and cid in my_clients) or (pid and pid == my_pid):
                continue

            # Распознаём клиента по application.name или media.name
            if re.search(r'(application\.name|media\.name)\s*=\s*"([^"]+)"', b):
                return True

        return False
    except Exception:
        return False


class DesktopObserver:
    """
    Фоновый наблюдатель за рабочим столом Niri с режимами NORMAL, FOCUS, QUIET, OFF.
    Автоматически глушится при заблокированном экране и во время звонков/микрофона.
    """

    def __init__(self, agent: CapriAgent, emotions: EmotionEngine, mode: str = "normal"):
        self.agent = agent
        self.emotions = emotions
        self.mode = mode.lower().strip()  # 'normal', 'focus', 'quiet', 'off'
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.task: Optional[asyncio.Task] = None
        self.last_comment_time: float = 0.0
        self.last_seen_app: str = ""
        self.last_seen_title: str = ""
        self.game_mode_enabled: bool = True
        self.game_mode_active: bool = False
        self._hyprland_fullscreen: bool = False

        # Умные интервалы: в обычном режиме не чаще раза в 6-8 минут
        self.cooldown_normal: float = 380.0  # ~6.3 минуты
        self.cooldown_quiet: float = 180.0   # 3 минуты (без голоса)

    def set_mode(self, mode: str) -> str:
        mode_clean = mode.lower().strip()
        if mode_clean in ("normal", "default"):
            self.mode = "normal"
            self.start()
            return "Режим Observer: NORMAL (сбалансированные редкие комментарии)."
        elif mode_clean in ("focus", "work", "coding"):
            self.mode = "focus"
            return "Режим Observer: FOCUS (полная тишина, никаких комментариев во время работы)."
        elif mode_clean in ("quiet", "silent"):
            self.mode = "quiet"
            self.start()
            return "Режим Observer: QUIET (только визуальные реакции на аватаре, без голоса)."
        elif mode_clean in ("off", "stop", "disabled"):
            self.mode = "off"
            self.stop()
            return "Режим Observer: OFF (наблюдатель полностью выключен)."
        return f"Неизвестный режим '{mode}'. Доступны: normal, focus, quiet, off."

    def start(self):
        if self.mode == "off":
            return
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._listen_compositor_events())

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None
        if self.task:
            self.task.cancel()
            self.task = None

    async def _listen_compositor_events(self):
        from .compositor import detect_compositor, get_hyprland_socket2_path, parse_hyprland_socket2_line

        comp = detect_compositor()
        if comp == "niri":
            await self._listen_niri_events()
        elif comp == "hyprland":
            await self._listen_hyprland_events(get_hyprland_socket2_path, parse_hyprland_socket2_line)
        else:
            logger.debug("DesktopObserver: neither Niri nor Hyprland active; observer standing by.")

    async def _listen_hyprland_events(self, get_sock_fn, parse_fn):
        while self.mode != "off":
            sock_path = get_sock_fn()
            if not sock_path or not sock_path.exists():
                await asyncio.sleep(5.0)
                continue
            writer = None
            try:
                reader, writer = await asyncio.open_unix_connection(str(sock_path))
                while self.mode != "off":
                    raw_line = await reader.readline()
                    if not raw_line:
                        break
                    parsed = parse_fn(raw_line.decode("utf-8", errors="replace"))
                    if not parsed:
                        continue
                    ev_name, ev_data = parsed
                    if ev_name == "fullscreen":
                        self._hyprland_fullscreen = ev_data.strip() == "1"
                        if self.game_mode_enabled:
                            self.game_mode_active = self._hyprland_fullscreen
                    elif ev_name == "activewindow" and "," in ev_data:
                        app_id, title = ev_data.split(",", 1)
                        await self._handle_focused_window(
                            app_id.strip(),
                            title.strip(),
                            is_fullscreen=self._hyprland_fullscreen,
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Hyprland observer loop error: {e}")
                await asyncio.sleep(5.0)
            finally:
                if writer is not None:
                    try:
                        writer.close()
                    except Exception:
                        pass

    async def _listen_niri_events(self):
        while self.mode != "off":
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    "niri", "msg", "-j", "event-stream",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )

                while self.mode != "off" and self.proc and self.proc.stdout:
                    line = await self.proc.stdout.readline()
                    if not line:
                        break

                    try:
                        event = json.loads(line.decode("utf-8", errors="replace"))
                        await self._handle_event(event)
                    except json.JSONDecodeError:
                        continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Observer loop error: {e}")
                await asyncio.sleep(5.0)

    async def _handle_event(self, event: dict):
        # В режиме FOCUS наблюдатель полностью замолкает
        if self.mode == "focus":
            return

        if "WindowsChanged" not in event:
            return

        windows = event["WindowsChanged"].get("windows", [])
        focused_window = next((w for w in windows if w.get("is_focused")), None)
        if not focused_window:
            return

        app_id = focused_window.get("app_id") or ""
        title = focused_window.get("title") or ""
        is_fullscreen = bool(focused_window.get("is_fullscreen", False))
        await self._handle_focused_window(app_id, title, is_fullscreen=is_fullscreen)

    async def _handle_focused_window(self, app_id: str, title: str, is_fullscreen: bool = False):
        if self.mode == "focus":
            return

        # Пропускаем служебные окна, аватар и сам терминал агента
        if not app_id or app_id in ("capri-avatar", "kitty") and "agy" in title.lower():
            return

        # 0. Game Mode / Power Saver: полная тишина во время игр и полноэкранного режима
        if self.game_mode_enabled:
            from .presence import evaluate_game_mode

            gm = evaluate_game_mode({"app_id": app_id, "title": title, "is_fullscreen": is_fullscreen})
            self.game_mode_active = gm.active
            if gm.active:
                return
        else:
            self.game_mode_active = False

        # Если окно или заголовок не изменились — не реагируем
        if app_id == self.last_seen_app and title == self.last_seen_title:
            return

        self.last_seen_app = app_id
        self.last_seen_title = title

        # 1. Проверка блокировки экрана: если экран заблокирован, молчим
        if await is_screen_locked():
            return

        # 2. Проверка звонков: если идёт звонок в Telegram/Discord/браузере, не перебиваем
        if await is_call_active():
            return

        # 3. Проверка кулдауна
        now = time.time()
        cd = self.cooldown_quiet if self.mode == "quiet" else self.cooldown_normal
        if now - self.last_comment_time < cd:
            return

        # 4. Вероятность срабатывания (в нормальном режиме 20-25%, чтобы не надоедать)
        chance = 0.25 if self.mode == "normal" else 0.40
        if random.random() > chance:
            return

        # Фильтр только интересных приложений для реплики
        interesting_apps = (
            "firefox", "chromium", "spotify", "blender", "steam", "telegram",
            "discord", "code", "dolphin", "obs", "gimp", "vlc", "mpv", "chatgpt"
        )
        if not any(app in app_id.lower() or app in title.lower() for app in interesting_apps):
            return

        self.last_comment_time = now
        await self._trigger_reaction(app_id, title)

    async def _trigger_reaction(self, app_id: str, title: str):
        """Реакция на смену окна в зависимости от режима QUIET или NORMAL."""
        notify_avatar("playful", f"👀 {app_id}")

        # В режиме QUIET — ТОЛЬКО визуальная реакция на аватаре, без звука и спама
        if self.mode == "quiet":
            return

        # В режиме NORMAL — короткая реплика
        try:
            user_name = getattr(self.agent.config.companion, "user_name", "User") or "User"
            prompt = (
                f"Пользователь {user_name} только что открыл окно '{app_id}' (заголовок: '{title}'). "
                f"Твоё текущее настроение: {self.emotions.mood}, доверие: {self.emotions.trust}/100. "
                "Сделай одну очень короткую (1 предложение), меткую и живую реплику/подкол/наблюдение. "
                "Без дежурных фраз и без шаблонных вопросов."
            )

            messages = [
                {"role": "system", "content": self.agent.system_prompt + self.emotions.get_prompt_context(user_name=user_name)},
                {"role": "user", "content": prompt},
            ]

            client = self.agent._get_client()
            resp = await client.chat.completions.create(
                model=self.agent.config.llm.model,
                messages=messages,
                temperature=0.8,
                max_tokens=160,
            )

            from .agent import parse_and_strip_appraisal
            raw_content = resp.choices[0].message.content or ""
            _, clean_comment = parse_and_strip_appraisal(raw_content)
            comment = clean_comment.strip().strip('"«»')
            if not comment:
                return

            # Вывод в консоль
            print(f"\n\r\033[K[bold cyan]Капри (Заметила)[/bold cyan] [dim]>[/dim] {comment}\n[bold magenta]{user_name}[/bold magenta] [dim]>[/dim] ", end="", flush=True)

            # Озвучка
            if self.agent.config.voice.tts_enabled:
                await self.agent.tts.speak(comment)
            else:
                notify_avatar("idle")

        except Exception as e:
            logger.debug(f"Error in ambient reaction: {e}")
