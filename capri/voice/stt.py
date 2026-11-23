from __future__ import annotations

import asyncio
import io
import os
import queue
import threading
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Optional
import numpy as np


def _normalize_mic_rms(rms: float) -> float:
    """Map raw float32 microphone RMS (~0.004..0.12) cleanly to 0.0..1.0 visual energy."""
    if rms <= 0.004:
        return 0.0
    return max(0.0, min(1.0, (float(rms) - 0.004) / 0.11))


class _AsyncAudioLevelDispatcher:
    """
    Single-slot coalescing daemon sender so real-time PortAudio callbacks NEVER perform
    blocking socket I/O. Enqueueing is O(1) non-blocking and drops stale intermediate levels.
    """

    def __init__(self) -> None:
        self._queue: "queue.Queue[float]" = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            t = threading.Thread(target=self._run, name="CapriAudioLevelSender", daemon=True)
            self._thread = t
            t.start()

    def _run(self) -> None:
        while True:
            level = self._queue.get()
            try:
                from ..ui.client import notify_avatar_audio_level
                notify_avatar_audio_level(level, source="mic")
            except Exception:
                pass

    def enqueue(self, level: float) -> None:
        self._ensure_worker()
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(float(level))
        except queue.Full:
            pass


_AUDIO_LEVEL_DISPATCHER = _AsyncAudioLevelDispatcher()


def _emit_mic_audio_level(level: float) -> None:
    """Strictly non-blocking mic level push to desktop avatar (no-op in offscreen runs)."""
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        return
    _AUDIO_LEVEL_DISPATCHER.enqueue(level)
try:
    import sounddevice as sd
except Exception as _sd_exc:  # PortAudio/PulseAudio may be unavailable on headless Linux
    sd = None  # type: ignore[assignment]
    _SOUNDDEVICE_IMPORT_ERROR: Optional[Exception] = _sd_exc
else:
    _SOUNDDEVICE_IMPORT_ERROR = None
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT_DIR / ".env")

if TYPE_CHECKING:
    from ..core.config import VoiceConfig


class PushToTalkRecorder:
    """Потоковый микрофонный рекордер для зажатия и отпускания горячей клавиши."""
    def __init__(self, sample_rate: int = 16000, input_device: str = ""):
        self.sample_rate = sample_rate
        self.input_device = (input_device or "").strip()
        self.frames: list[np.ndarray] = []
        self.is_recording = False
        self.stream = None

    def _resolved_device(self) -> Optional[int]:
        if not self.input_device:
            return None
        try:
            from .pipewire import resolve_sounddevice_input
            return resolve_sounddevice_input(self.input_device)
        except Exception:
            return None

    def start(self):
        if sd is None:
            raise RuntimeError(f"Микрофон недоступен (PortAudio/sounddevice): {_SOUNDDEVICE_IMPORT_ERROR}")
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

        self.frames = []
        self.is_recording = True
        last_level_ts = [0.0]

        def callback(indata, frame_count, time_info, status):
            if self.is_recording:
                self.frames.append(indata.copy())
                now_t = time.monotonic()
                if now_t - last_level_ts[0] >= 0.08:
                    last_level_ts[0] = now_t
                    rms = float(np.sqrt(np.mean(np.square(indata, dtype=np.float32))))
                    _emit_mic_audio_level(_normalize_mic_rms(rms))

        kwargs = {
            "samplerate": self.sample_rate,
            "channels": 1,
            "dtype": "float32",
            "callback": callback,
        }
        dev = self._resolved_device()
        if dev is not None:
            kwargs["device"] = dev
        self.stream = sd.InputStream(**kwargs)
        self.stream.start()

    def stop(self) -> np.ndarray:
        self.is_recording = False
        _emit_mic_audio_level(0.0)
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

        if not self.frames:
            return np.array([], dtype=np.float32)
        return np.concatenate(self.frames, axis=0).flatten()


class SpeechRecognizer:
    def __init__(self, config: VoiceConfig):
        self.config = config
        self._model = None
        self.sample_rate = 16000
        self.recorder = PushToTalkRecorder(
            sample_rate=self.sample_rate,
            input_device=getattr(config, "input_device", "") or "",
        )

    def _stream_kwargs(self) -> dict:
        kwargs: dict = {"samplerate": self.sample_rate, "channels": 1, "dtype": "float32"}
        pref = (getattr(self.config, "input_device", "") or "").strip()
        if pref:
            try:
                from .pipewire import resolve_sounddevice_input
                dev = resolve_sounddevice_input(pref)
                if dev is not None:
                    kwargs["device"] = dev
            except Exception:
                pass
        return kwargs

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            try:
                self._model = WhisperModel(
                    self.config.stt_model_size,
                    device=self.config.stt_device,
                    compute_type=self.config.stt_compute_type,
                )
            except Exception as e:
                print(f"[STT Warning] Ошибка инициализации на {self.config.stt_device}: {e}. Переключаюсь на CPU.")
                self._model = WhisperModel(
                    self.config.stt_model_size,
                    device="cpu",
                    compute_type="int8",
                )
        return self._model

    def record_audio(
        self,
        max_duration: float = 6.0,
        silence_timeout: float = 1.3,
        stop_event: Optional[threading.Event] = None,
    ) -> np.ndarray:
        """
        Запись с автоматической детекцией тишины (VAD) и поддержкой принудительной остановки:
        останавливается, когда пользователь закончил говорить или нажал кнопку остановки.
        """
        chunk_duration = 0.1
        chunk_samples = int(self.sample_rate * chunk_duration)
        max_chunks = int(max_duration / chunk_duration)
        silence_chunks_needed = int(silence_timeout / chunk_duration)

        recorded = []
        speech_started = False
        consecutive_silence = 0
        silence_threshold = 0.008

        if stop_event and stop_event.is_set():
            return np.array([], dtype=np.float32)

        if sd is None:
            raise RuntimeError(f"Микрофон недоступен (PortAudio/sounddevice): {_SOUNDDEVICE_IMPORT_ERROR}")
        try:
            with sd.InputStream(**self._stream_kwargs()) as stream:
                for _ in range(max_chunks):
                    if stop_event and stop_event.is_set():
                        break
                    chunk, _ = stream.read(chunk_samples)
                    chunk_flat = chunk.flatten()
                    recorded.append(chunk_flat)

                    rms = float(np.sqrt(np.mean(chunk_flat ** 2)))
                    _emit_mic_audio_level(_normalize_mic_rms(rms))
                    if rms > silence_threshold:
                        speech_started = True
                        consecutive_silence = 0
                    elif speech_started:
                        consecutive_silence += 1
                        if consecutive_silence >= silence_chunks_needed:
                            break
        finally:
            _emit_mic_audio_level(0.0)

        if not recorded:
            return np.array([], dtype=np.float32)
        return np.concatenate(recorded, axis=0)

    def record_seconds(self, duration: float = 4.0) -> np.ndarray:
        """Запись фиксированного числа секунд без VAD-отсечки."""
        chunk_samples = int(self.sample_rate * 0.1)
        total_chunks = max(1, int(round(duration / 0.1)))
        recorded = []
        if sd is None:
            raise RuntimeError(f"Микрофон недоступен (PortAudio/sounddevice): {_SOUNDDEVICE_IMPORT_ERROR}")
        with sd.InputStream(**self._stream_kwargs()) as stream:
            for _ in range(total_chunks):
                chunk, _ = stream.read(chunk_samples)
                recorded.append(chunk.flatten())
        if not recorded:
            return np.array([], dtype=np.float32)
        return np.concatenate(recorded, axis=0)

    async def transcribe_audio_array(self, audio: np.ndarray) -> str:
        """
        Распознавание речи:
        1. Groq Whisper Large v3 Turbo (контекстный промпт, 0.15–0.3 сек).
        2. Резервный локальный faster-whisper.
        """
        if len(audio) == 0:
            return ""

        # Проверка на тишину и случайный микрофонный шум
        max_amp = float(np.abs(audio).max()) if len(audio) > 0 else 0.0
        if max_amp < 0.008:
            return ""

        groq_api_key = os.getenv("GROQ_API_KEY", "")
        if not groq_api_key and hasattr(self.config, "groq_api_key"):
            groq_api_key = getattr(self.config, "groq_api_key", "")

        if groq_api_key:
            try:
                from openai import AsyncOpenAI

                pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
                buf = io.BytesIO()
                with wave.open(buf, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(self.sample_rate)
                    wf.writeframes(pcm.tobytes())
                wav_bytes = buf.getvalue()

                client = AsyncOpenAI(api_key=groq_api_key, base_url="https://api.groq.com/openai/v1", timeout=8.0)
                try:
                    resp = await client.audio.transcriptions.create(
                        model="whisper-large-v3-turbo",
                        file=("audio.wav", wav_bytes, "audio/wav"),
                        language=self.config.stt_language,
                        # Контекстный промпт: имя Капри и термины
                        prompt="Привет, Капри! Разговор с киберпанк-напарницей Капри (Capri). Arch Linux, Niri, Kitty, Dolphin, PipeWire, RTX 3050.",
                        timeout=8.0,
                    )
                finally:
                    await client.close()
                text = resp.text.strip()
                # Фильтрация типичных галлюцинаций Whisper на тишине
                bad_phrases = (
                    "продолжение следует", "субтитры", "спасибо за просмотр",
                    "редактор субтитров", "до свидания", "переводчик",
                    "подписывайтесь", "ставьте лайки"
                )
                low_text = text.lower()
                if any(bad in low_text for bad in bad_phrases) and len(text) < 45:
                    return ""
                if text:
                    return text
            except Exception:
                pass

        # Резервный локальный faster-whisper
        try:
            model = self._get_model()
            segments, _ = await asyncio.to_thread(
                model.transcribe,
                audio,
                language=self.config.stt_language,
                beam_size=5,
                vad_filter=True,
                initial_prompt="Капри"
            )
            text = " ".join([seg.text.strip() for seg in segments]).strip()
            return text
        except Exception:
            return ""

    async def transcribe_file(self, file_path: str) -> str:
        with open(file_path, "rb") as f:
            wav_bytes = f.read()

        groq_key = os.getenv("GROQ_API_KEY", "")
        if groq_key:
            try:
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1", timeout=8.0)
                resp = await client.audio.transcriptions.create(
                    model="whisper-large-v3-turbo",
                    file=("audio.wav", wav_bytes, "audio/wav"),
                    language=self.config.stt_language,
                    prompt="Привет, Капри! Разговор с киберпанк-напарницей Капри (Capri). Arch Linux, Niri, Kitty, Dolphin, PipeWire, RTX 3050.",
                    timeout=8.0,
                )
                return resp.text.strip()
            except Exception:
                pass

        model = self._get_model()
        segments, _ = await asyncio.to_thread(
            model.transcribe,
            file_path,
            language=self.config.stt_language,
            beam_size=5,
            vad_filter=True,
            initial_prompt="Капри"
        )
        return " ".join([seg.text.strip() for seg in segments]).strip()
