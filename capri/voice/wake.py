from __future__ import annotations

import asyncio
import collections
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional, List

import numpy as np
try:
    import sounddevice as sd
except Exception as _sd_exc:  # PortAudio/PulseAudio may be unavailable on headless Linux
    sd = None  # type: ignore[assignment]
    _SOUNDDEVICE_IMPORT_ERROR: Optional[Exception] = _sd_exc
else:
    _SOUNDDEVICE_IMPORT_ERROR = None
import vosk

from ..ui.client import notify_avatar

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_MODEL_DIR = ROOT_DIR / "models" / "vosk-model-small-ru-0.22"
from ..core.resources import get_asset_path

CHIRP_SOUND = get_asset_path("wake_chirp.wav")


def ensure_vosk_model(target_dir: Path = DEFAULT_MODEL_DIR) -> bool:
    """Проверяет наличие оффлайн-модели Vosk и автоматически загружает её при отсутствии."""
    if target_dir.exists() and (target_dir / "am").exists():
        return True

    try:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        url = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
        logger.info("Загрузка оффлайн-модели Vosk Small RU (45MB)...")
        with urllib.request.urlopen(url, timeout=40) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(target_dir.parent)
        return target_dir.exists() and (target_dir / "am").exists()
    except Exception as e:
        logger.warning(f"Не удалось автоматически загрузить модель Vosk: {e}")
        return False


def ensure_pipewire_audio_route():
    """Проверяет и восстанавливает связь микрофона в PipeWire/EasyEffects, если виртуальный сорс отвязан."""
    try:
        if not shutil.which("pw-link"):
            return
        res = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, timeout=1.5)
        text = res.stdout
        # Если EasyEffects активен, проверяем подключение микрофона
        if "easyeffects_source" in text:
            target_mic = None
            def_res = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True, timeout=1.0)
            default_src = def_res.stdout.strip()
            if default_src and "easyeffects" not in default_src.lower() and "monitor" not in default_src.lower():
                target_mic = default_src
            else:
                p_res = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=1.5)
                mic_names = [line.split()[1] for line in p_res.stdout.splitlines() if ("usb" in line.lower() or "mic" in line.lower() or "input" in line.lower()) and "monitor" not in line.lower() and "easyeffects" not in line.lower()]
                fifine = next((m for m in mic_names if "fifine" in m.lower()), None)
                target_mic = fifine or (mic_names[0] if mic_names else None)

            # Проверяем, привязан ли микрофон именно к EasyEffects входам (FL и FR)
            fl_linked = False
            fr_linked = False
            lines = text.splitlines()
            for idx, line in enumerate(lines):
                if "easyeffects_source:input_FL" in line:
                    for sub in lines[idx + 1 : idx + 8]:
                        if "|<-" in sub:
                            if target_mic and target_mic in sub:
                                fl_linked = True
                                break
                        else:
                            break
                elif "easyeffects_source:input_FR" in line:
                    for sub in lines[idx + 1 : idx + 8]:
                        if "|<-" in sub:
                            if target_mic and target_mic in sub:
                                fr_linked = True
                                break
                        else:
                            break

            ee_linked = fl_linked and fr_linked

            if target_mic and not ee_linked:
                out_ports_res = subprocess.run(["pw-link", "-o"], capture_output=True, text=True, timeout=1.5)
                mic_ports = [p.strip() for p in out_ports_res.stdout.splitlines() if p.strip().startswith(target_mic)]
                mono_port = next((p for p in mic_ports if "mono" in p.lower()), None)
                fl_port = next((p for p in mic_ports if "fl" in p.lower() or "left" in p.lower()), None)
                fr_port = next((p for p in mic_ports if "fr" in p.lower() or "right" in p.lower()), None)

                if mono_port:
                    subprocess.run(["pw-link", mono_port, "easyeffects_source:input_FL"], capture_output=True, timeout=1.0)
                    subprocess.run(["pw-link", mono_port, "easyeffects_source:input_FR"], capture_output=True, timeout=1.0)
                elif fl_port and fr_port:
                    subprocess.run(["pw-link", fl_port, "easyeffects_source:input_FL"], capture_output=True, timeout=1.0)
                    subprocess.run(["pw-link", fr_port, "easyeffects_source:input_FR"], capture_output=True, timeout=1.0)
                elif mic_ports:
                    subprocess.run(["pw-link", mic_ports[0], "easyeffects_source:input_FL"], capture_output=True, timeout=1.0)
                    subprocess.run(["pw-link", mic_ports[0], "easyeffects_source:input_FR"], capture_output=True, timeout=1.0)
    except Exception:
        pass


def play_wake_sound():
    """Воспроизведение короткого футуристичного сигнала активации."""
    try:
        sound_path = str(CHIRP_SOUND)
        if CHIRP_SOUND.exists():
            if shutil.which("paplay"):
                subprocess.Popen(
                    ["paplay", sound_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            elif shutil.which("mpv"):
                subprocess.Popen(
                    ["mpv", "--no-config", "--no-video", "--really-quiet", sound_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
    except Exception:
        pass

    # Резервный системный звуковой сигнал в терминал
    try:
        import sys
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass


class WakeWordListener:
    """
    Фоновый непрерывный слушатель ключевого слова («Капри» / «Эй, Капри»).
    Работает на легковесном оффлайн-движке Vosk (Kaldi ASR) с грамматическим фильтром.
    Потребляет минимум CPU (<1.5%) и ОЗУ (~30MB).
    """

    def __init__(
        self,
        stt_instance=None,
        on_text_ready: Optional[Callable[[str], None]] = None,
        on_wake: Optional[Callable[[], None]] = None,
        model_path: Optional[str | Path] = None,
        sample_rate: int = 16000,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        wake_words: Optional[List[str]] = None,
    ):
        self.stt = stt_instance
        self.on_text_ready = on_text_ready
        self.on_wake = on_wake
        self.sample_rate = sample_rate
        self.loop = loop
        self.wake_words = wake_words or ["капри", "эй капри", "привет капри"]

        # Динамический regex с учетом пользовательских ключевых фраз по границам слов
        sorted_phrases = sorted((w.strip().lower() for w in self.wake_words if w.strip()), key=len, reverse=True)
        escaped_words = [re.escape(w) for w in sorted_phrases]
        pattern_str = r"\b(" + "|".join(escaped_words) + r")\b" if escaped_words else r"\bкапри\b"
        self.wake_pattern = re.compile(pattern_str, re.IGNORECASE)

        self.model_path = Path(model_path) if model_path else DEFAULT_MODEL_DIR
        self.model: Optional[vosk.Model] = None
        self.recognizer: Optional[vosk.KaldiRecognizer] = None

        self.min_speech_rms: float = 0.008
        self.partial_min_rms: float = 0.012

        self.running = False
        self.thread: Optional[threading.Thread] = None
        self._paused = False
        self._stream_closed_event = threading.Event()
        self._stream_closed_event.set()
        self._cancel_wake_event = threading.Event()
        self.is_recording_command = False

    def cancel_active_recording(self):
        """Немедленно отменяет активную запись команды после срабатывания wake-word."""
        self._cancel_wake_event.set()

    def is_available(self) -> bool:
        """Проверяет доступность модели Vosk или возможность её автозагрузки."""
        return ensure_vosk_model(self.model_path)

    def pause(self):
        """Временная пауза (когда активен PTT, /listen или говорит Капри) с ожиданием закрытия потока микрофона."""
        self._paused = True
        # Ожидаем завершения текущего 125мс чанка и закрытия потока без конфликтов PortAudio
        self._stream_closed_event.wait(timeout=0.5)

    def resume(self):
        """Возобновление фонового прослушивания."""
        self._paused = False

    def start(self) -> bool:
        if not self.is_available():
            logger.warning(f"Vosk model not available at {self.model_path}")
            return False

        if self.running:
            return True

        vosk.SetLogLevel(-1)
        ensure_pipewire_audio_route()
        try:
            self.model = vosk.Model(str(self.model_path))
            grammar_words = []
            for phrase in self.wake_words:
                cleaned = phrase.strip().lower()
                if cleaned:
                    grammar_words.append(cleaned)
                    grammar_words.extend(cleaned.split())
            grammar_words.extend([
                "капри", "эй капри", "привет капри", "слушай капри",
                "каприз", "эй", "привет", "слушай", "да", "нет",
                "что", "это", "как", "так", "раз", "два", "три", "стоп",
                "[unk]"
            ])
            grammar_list = list(dict.fromkeys(grammar_words))
            grammar_json = json.dumps(grammar_list, ensure_ascii=False)
            self.recognizer = vosk.KaldiRecognizer(self.model, self.sample_rate, grammar_json)
        except Exception as e:
            logger.error(f"Failed to load Vosk model: {e}")
            return False

        self.running = True
        self._paused = False
        self.thread = threading.Thread(target=self._listen_loop, daemon=True, name="CapriWakeWord")
        self.thread.start()
        return True

    def stop(self):
        self.running = False
        self._paused = True
        self._cancel_wake_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)
        self.thread = None
        self._stream_closed_event.set()

    def _listen_loop(self):
        """Главный цикл захвата микрофона с VAD-фильтрацией и подтверждением ключевого слова."""
        if sd is None:
            logger.warning(f"Wake-Word отключён: PortAudio/sounddevice недоступен ({_SOUNDDEVICE_IMPORT_ERROR})")
            self.running = False
            self._stream_closed_event.set()
            return
        block_size = 2000  # 125 мс чанки при 16 кГц для стабильной синхронизации
        # Кольцевой буфер последних 6 чанков (~750 мс), чтобы сохранять слитную речь после «Капри»
        pre_buffer = collections.deque(maxlen=6)
        consecutive_partials = 0
        silence_frames = 0

        while self.running:
            if self._paused:
                time.sleep(0.08)
                continue

            self._stream_closed_event.clear()
            try:
                with sd.RawInputStream(
                    samplerate=self.sample_rate,
                    blocksize=block_size,
                    dtype="int16",
                    channels=1,
                ) as stream:
                    while self.running and not self._paused:
                        data, _ = stream.read(block_size)
                        if not data:
                            continue

                        # Vosk CFFI в Python 3.12 требует явный bytes, а не buffer
                        bytes_data = bytes(data)

                        # Конвертируем чанк в float32 для буфера сохранения
                        chunk_int16 = np.frombuffer(bytes_data, dtype=np.int16)
                        chunk_float = chunk_int16.astype(np.float32) / 32768.0
                        pre_buffer.append(chunk_float)

                        # Вычисляем среднеквадратичную амплитуду энергии (RMS)
                        rms = float(np.sqrt(np.mean(chunk_float ** 2)))

                        # VAD-гейт: если громкость ниже порога речи (тишина, выключенный микрофон, тихий фон)
                        if rms < self.min_speech_rms:
                            silence_frames += 1
                            consecutive_partials = 0
                            if silence_frames >= 8:  # ~1 сек непрерывной тишины
                                self.recognizer.Reset()
                            continue

                        silence_frames = 0
                        wake_detected = False

                        # Проверка ключевого слова
                        if self.recognizer.AcceptWaveform(bytes_data):
                            res = json.loads(self.recognizer.Result())
                            text = res.get("text", "")
                            if self.wake_pattern.search(text):
                                wake_detected = True
                            consecutive_partials = 0
                        else:
                            partial = json.loads(self.recognizer.PartialResult())
                            p_text = partial.get("partial", "")
                            # Для промежуточных результатов требуем достаточной энергии речи и подтверждения в 2 смежных чанках (~250 мс)
                            if self.wake_pattern.search(p_text) and rms >= self.partial_min_rms:
                                consecutive_partials += 1
                                if consecutive_partials >= 2:
                                    wake_detected = True
                                    consecutive_partials = 0
                            else:
                                consecutive_partials = 0

                        if wake_detected:
                            # Проверяем, что в буфере действительно присутствовала речевая энергия
                            max_pre_rms = max((float(np.sqrt(np.mean(f ** 2))) for f in pre_buffer), default=0.0)
                            if max_pre_rms < self.min_speech_rms:
                                self.recognizer.Reset()
                                continue

                            # Передаем пре-буфер, чтобы не срезать начало команды
                            initial_frames = list(pre_buffer)
                            pre_buffer.clear()
                            consecutive_partials = 0
                            self._handle_wake_event(stream, block_size, initial_frames)

            except Exception as e:
                logger.debug(f"Audio stream glitch: {e}")
                time.sleep(0.2)
            finally:
                self._stream_closed_event.set()

    def _handle_wake_event(self, stream: sd.RawInputStream, block_size: int, initial_frames: list[np.ndarray]):
        """Обработка срабатывания ключевого слова и запись фразы пользователя без смены размера блока."""
        if self.recognizer:
            self.recognizer.Reset()
        self._cancel_wake_event.clear()
        self.is_recording_command = True

        try:
            # 1. Сигнал активации и реакция
            play_wake_sound()
            notify_avatar("listening", "● Слушаю тебя...")

            if self.on_wake:
                try:
                    self.on_wake()
                except Exception:
                    pass

            # 2. Фаза записи команды (VAD по паузе речи) с ИДЕНТИЧНЫМ block_size
            recorded_chunks = list(initial_frames)
            speech_started = False
            silence_chunks = 0
            chunk_duration = block_size / float(self.sample_rate)  # 125 мс
            # Порог речи VAD для конденсаторного микрофона (выше комнатного шума ~0.003, но ловит нормальную речь)
            silence_threshold = 0.008
            silence_timeout_chunks = int(1.2 / chunk_duration)  # ~1.2 сек тишины для финализации
            max_chunks = int(12.0 / chunk_duration)  # максимум 12 сек на команду
            start_wait_chunks = int(2.5 / chunk_duration)  # 2.5 сек ожидания продолжения фразы

            chunks_read = 0
            while chunks_read < max_chunks and self.running and not self._paused:
                if self._cancel_wake_event.is_set():
                    notify_avatar("idle")
                    return

                raw_data, _ = stream.read(block_size)
                if not raw_data:
                    break
                chunks_read += 1

                raw_bytes = bytes(raw_data)
                chunk_int16 = np.frombuffer(raw_bytes, dtype=np.int16)
                chunk_float = chunk_int16.astype(np.float32) / 32768.0
                recorded_chunks.append(chunk_float)

                rms = float(np.sqrt(np.mean(chunk_float ** 2)))
                if rms > silence_threshold:
                    speech_started = True
                    silence_chunks = 0
                elif speech_started:
                    silence_chunks += 1
                    if silence_chunks >= silence_timeout_chunks:
                        break
                else:
                    if chunks_read >= start_wait_chunks:
                        break

            if self._paused or self._cancel_wake_event.is_set():
                notify_avatar("idle")
                return

            # Если после активации речи пользователя не последовало
            if not speech_started:
                notify_avatar("idle")
                # Не шлём фантомный вызов «Капри» в чат LLM, если пользователь ничего не сказал
                return

            audio = np.concatenate(recorded_chunks, axis=0)

            # Если запись слишком короткая (< 0.4 сек)
            if len(audio) < int(self.sample_rate * 0.4):
                notify_avatar("idle")
                return

            notify_avatar("thinking", "Распознаю...")

            # 3. Отправка в Groq Whisper (STT)
            if not self.stt or not self.on_text_ready or self._cancel_wake_event.is_set():
                notify_avatar("idle")
                return

            if self.loop:
                asyncio.run_coroutine_threadsafe(self._transcribe_and_dispatch(audio), self.loop)
            else:
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        text = loop.run_until_complete(self.stt.transcribe_audio_array(audio))
                    finally:
                        loop.close()

                    text = text.strip() if text else ""
                    if text and text not in (".", "!", "?", "...", "—") and not self._cancel_wake_event.is_set():
                        self.on_text_ready(text)
                    else:
                        notify_avatar("idle")
                except Exception as e:
                    logger.error(f"Wake STT dispatch error (standalone thread): {e}")
        finally:
            self.is_recording_command = False
            notify_avatar("idle")

    async def _transcribe_and_dispatch(self, audio: np.ndarray):
        """Асинхронная транскрипция аудио и отправка в агент."""
        try:
            text = await self.stt.transcribe_audio_array(audio)
            text = text.strip() if text else ""
            if text and text not in (".", "!", "?", "...", "—"):
                if self.on_text_ready:
                    if asyncio.iscoroutinefunction(self.on_text_ready):
                        await self.on_text_ready(text)
                    else:
                        self.on_text_ready(text)
            else:
                notify_avatar("idle")
        except Exception as e:
            logger.error(f"Wake STT dispatch error: {e}")
        finally:
            notify_avatar("idle")
