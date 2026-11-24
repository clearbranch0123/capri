from __future__ import annotations

import asyncio
import logging
import select
import threading
import time
from typing import Callable, Optional
try:
    import evdev
    from evdev import ecodes
except Exception as _evdev_exc:  # Optional global PTT hotkey dependency
    evdev = None  # type: ignore[assignment]
    ecodes = None  # type: ignore[assignment]
    _EVDEV_IMPORT_ERROR: Optional[Exception] = _evdev_exc
else:
    _EVDEV_IMPORT_ERROR = None
from ..ui.client import notify_avatar

logger = logging.getLogger(__name__)

DEFAULT_PTT_KEY = "KEY_RIGHTALT"


class PushToTalkListener:
    """Глобальный слушатель Push-to-Talk (зажал клавишу — говоришь, отпустил — ответ)."""
    def __init__(
        self,
        key_name: str = DEFAULT_PTT_KEY,
        on_speech_ready: Optional[Callable[[str], None]] = None,
        on_press: Optional[Callable[[], None]] = None,
        stt_instance=None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        wake_listener=None,
    ):
        self.key_name = self._normalize_key_name(key_name)
        self.key_code = getattr(ecodes, self.key_name, ecodes.KEY_RIGHTALT) if ecodes is not None else None
        self.on_speech_ready = on_speech_ready
        self.on_press = on_press
        self.stt = stt_instance
        self.loop = loop
        self.wake_listener = wake_listener

        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.devices: list = []
        self.is_key_pressed = False

    def _normalize_key_name(self, name: str) -> str:
        s = name.upper().strip()
        if not s.startswith("KEY_"):
            s = f"KEY_{s}"
        aliases = {
            "KEY_RIGHT_ALT": "KEY_RIGHTALT",
            "KEY_ALTGR": "KEY_RIGHTALT",
            "KEY_RIGHT_CTRL": "KEY_RIGHTCTRL",
            "KEY_LEFT_ALT": "KEY_LEFTALT",
            "KEY_LEFT_CTRL": "KEY_LEFTCTRL",
        }
        return aliases.get(s, s)

    def _find_devices(self) -> list:
        if evdev is None or ecodes is None or self.key_code is None:
            return []
        devs = []
        for path in evdev.list_devices():
            try:
                d = evdev.InputDevice(path)
                caps = d.capabilities()
                if ecodes.EV_KEY in caps:
                    keys = caps[ecodes.EV_KEY]
                    if self.key_code in keys:
                        devs.append(d)
            except Exception:
                pass
        return devs

    def start(self) -> bool:
        if evdev is None or ecodes is None:
            logger.warning(f"Push-to-Talk отключён: пакет `evdev` не установлен ({_EVDEV_IMPORT_ERROR})")
            return False
        self.devices = self._find_devices()
        if not self.devices:
            return False

        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="CapriPTT")
        self.thread.start()
        return True

    def stop(self):
        self.running = False

    def _run_loop(self):
        while self.running:
            if not self.devices:
                self.devices = self._find_devices()
                time.sleep(1.0)
                continue

            try:
                r, _, _ = select.select(self.devices, [], [], 0.5)
                for dev in r:
                    for event in dev.read():
                        if event.type == ecodes.EV_KEY and event.code == self.key_code:
                            if event.value == 1 and not self.is_key_pressed:
                                self.is_key_pressed = True
                                self._handle_press()
                            elif event.value == 0 and self.is_key_pressed:
                                self.is_key_pressed = False
                                self._handle_release()
            except (OSError, IOError):
                # Клавиатура была отключена или переподключена
                self.devices = self._find_devices()
                time.sleep(0.5)
            except Exception:
                time.sleep(0.1)

    def _handle_press(self):
        if self.wake_listener:
            try:
                self.wake_listener.pause()
            except Exception:
                pass
        if self.on_press:
            try:
                self.on_press()
            except Exception:
                pass
        if self.stt:
            self.stt.recorder.start()
        notify_avatar("listening", "● Запись микрофона...")

    def _handle_release(self):
        if self.wake_listener:
            try:
                self.wake_listener.resume()
            except Exception:
                pass
        notify_avatar("thinking", "Распознаю...")
        if not self.stt:
            notify_avatar("idle")
            return

        audio = self.stt.recorder.stop()
        # Игнорируем случайные микро-нажатия короче 0.35 секунды (350мс)
        if len(audio) < int(self.stt.sample_rate * 0.35):
            notify_avatar("idle")
            return

        if self.loop and self.on_speech_ready:
            asyncio.run_coroutine_threadsafe(self._process_speech(audio), self.loop)
        else:
            notify_avatar("idle")

    async def _process_speech(self, audio):
        try:
            text = await self.stt.transcribe_audio_array(audio)
            text = text.strip() if text else ""
            if text and self.on_speech_ready:
                if asyncio.iscoroutinefunction(self.on_speech_ready):
                    await self.on_speech_ready(text)
                else:
                    self.on_speech_ready(text)
            else:
                notify_avatar("idle")
        except Exception:
            notify_avatar("idle")
