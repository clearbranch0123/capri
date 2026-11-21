"""
CAPRI // Hands-Free Voice Barge-In & VAD Engine (Wave 3 — v2.9.0).

Allows the user to interrupt Capri mid-sentence purely by speaking:
- `BargeInDetector`: deterministic DSP state machine (adaptive noise floor,
  startup grace period, elevated echo-resistant RMS threshold, voice-band
  zero-crossing-rate check, and multi-frame confirmation + ring pre-buffer).
- `HandsFreeBargeInMonitor`: lightweight background mic listener active ONLY
  while Capri is speaking. On confirmed user speech, calls `on_barge_in()`
  (to interrupt streaming TTS immediately), finishes capturing the user's
  interrupting utterance via silence VAD, transcribes via `stt`, and delivers
  the recognized text via `on_text_ready(text)`.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)


class BargeInDetector:
    """
    Echo-resistant voice activity detector tuned for interrupting TTS playback.
    - Enforces a short `grace_period_sec` after speech starts (ignores speaker onset click).
    - Tracks an adaptive background `noise_floor_rms` via EMA on non-speech frames.
    - Checks voice-like zero-crossing rate (`0.01 <= zcr <= 0.35`) to reject mechanical clicks.
    - Requires `required_consecutive` consecutive speech frames before triggering barge-in.
    - Preserves the last `pre_buffer_frames` frames so the beginning of the user's interruption
      is never clipped.
    """

    def __init__(
        self,
        base_rms_threshold: float = 0.032,
        required_consecutive: int = 3,
        grace_period_sec: float = 0.45,
        pre_buffer_frames: int = 6,
        noise_multiplier: float = 2.4,
    ):
        self.base_rms_threshold = max(0.008, float(base_rms_threshold))
        self.required_consecutive = max(1, int(required_consecutive))
        self.grace_period_sec = max(0.0, float(grace_period_sec))
        self.noise_multiplier = max(1.2, float(noise_multiplier))

        self.noise_floor_rms: float = 0.005
        self._consecutive_hits: int = 0
        self._started_at: float = time.monotonic()
        self._pre_buffer: collections.deque[np.ndarray] = collections.deque(maxlen=max(1, pre_buffer_frames))
        self.triggered: bool = False

    def reset(self, now: Optional[float] = None):
        self.noise_floor_rms = 0.005
        self._consecutive_hits = 0
        self._started_at = time.monotonic() if now is None else now
        self._pre_buffer.clear()
        self.triggered = False

    @property
    def effective_threshold(self) -> float:
        return max(self.base_rms_threshold, self.noise_floor_rms * self.noise_multiplier)

    @staticmethod
    def compute_rms_and_zcr(chunk_float: np.ndarray) -> tuple[float, float]:
        if chunk_float.size == 0:
            return 0.0, 0.0
        rms = float(np.sqrt(np.mean(np.square(chunk_float, dtype=np.float32))))
        if chunk_float.size < 2:
            return rms, 0.0
        signs = chunk_float >= 0.0
        crossings = int(np.count_nonzero(signs[1:] != signs[:-1]))
        zcr = crossings / float(chunk_float.size - 1)
        return rms, zcr

    def feed(self, chunk_float: np.ndarray, now: Optional[float] = None) -> bool:
        """
        Feed one float32 mono frame (range -1..1).
        Returns True on the exact frame where sustained user barge-in is confirmed.
        """
        if self.triggered:
            return False

        cur_t = time.monotonic() if now is None else now
        self._pre_buffer.append(chunk_float)
        rms, zcr = self.compute_rms_and_zcr(chunk_float)

        # During startup grace period, only adapt the noise floor (do not trigger yet)
        if cur_t - self._started_at < self.grace_period_sec:
            self.noise_floor_rms = 0.85 * self.noise_floor_rms + 0.15 * min(rms, self.base_rms_threshold)
            self._consecutive_hits = 0
            return False

        threshold = self.effective_threshold
        voice_like_zcr = 0.008 <= zcr <= 0.38

        if rms >= threshold and voice_like_zcr:
            self._consecutive_hits += 1
            if self._consecutive_hits >= self.required_consecutive:
                self.triggered = True
                return True
        else:
            self._consecutive_hits = 0
            # Slowly adapt noise floor downward/upward toward quiet room/speaker bleed
            capped_rms = min(rms, self.base_rms_threshold * 0.9)
            self.noise_floor_rms = 0.92 * self.noise_floor_rms + 0.08 * capped_rms

        return False

    def take_pre_buffer(self) -> list[np.ndarray]:
        frames = list(self._pre_buffer)
        self._pre_buffer.clear()
        return frames


class HandsFreeBargeInMonitor:
    """
    Runs while Capri is speaking to detect live vocal interruption (Barge-In),
    immediately interrupt TTS, capture the rest of the user's sentence,
    and dispatch the transcribed text back to Cyberdeck/CLI.
    """

    def __init__(
        self,
        stt_instance,
        on_barge_in: Optional[Callable[[], None]] = None,
        on_text_ready: Optional[Callable[[str], None]] = None,
        rms_threshold: float = 0.032,
        sample_rate: int = 16000,
        input_device: str = "",
    ):
        self.stt = stt_instance
        self.on_barge_in = on_barge_in
        self.on_text_ready = on_text_ready
        self.sample_rate = sample_rate
        self.input_device = (input_device or "").strip()
        self.detector = BargeInDetector(base_rms_threshold=rms_threshold)

        self._running = False
        self._cancel_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.did_interrupt: bool = False

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        self._cancel_event.clear()
        self.did_interrupt = False
        self.detector.reset()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="CapriBargeInMonitor")
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        self._cancel_event.set()
        if self._thread and self._thread.is_alive():
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=0.8)
        self._thread = None

    def _loop(self):
        try:
            import sounddevice as sd
        except Exception:
            self._running = False
            return

        block_size = 1600  # 100ms chunks @ 16kHz
        dev_idx = None
        if self.input_device:
            try:
                from .pipewire import resolve_sounddevice_input
                dev_idx = resolve_sounddevice_input(self.input_device)
            except Exception:
                dev_idx = None
        stream_kwargs = {
            "samplerate": self.sample_rate,
            "blocksize": block_size,
            "dtype": "int16",
            "channels": 1,
        }
        if dev_idx is not None:
            stream_kwargs["device"] = dev_idx
        try:
            with sd.RawInputStream(**stream_kwargs) as stream:
                while self._running and not self._cancel_event.is_set():
                    raw_data, _ = stream.read(block_size)
                    if not raw_data:
                        continue
                    chunk_int16 = np.frombuffer(bytes(raw_data), dtype=np.int16)
                    chunk_float = chunk_int16.astype(np.float32) / 32768.0

                    if self.detector.feed(chunk_float):
                        self.did_interrupt = True
                        initial_frames = self.detector.take_pre_buffer()
                        if self.on_barge_in:
                            try:
                                self.on_barge_in()
                            except Exception as ex:
                                logger.debug(f"on_barge_in callback error: {ex}")
                        self._capture_and_transcribe(stream, block_size, initial_frames)
                        break
        except Exception as e:
            logger.debug(f"HandsFreeBargeInMonitor audio error: {e}")
        finally:
            self._running = False

    def _capture_and_transcribe(self, stream, block_size: int, initial_frames: list[np.ndarray]):
        recorded = list(initial_frames)
        silence_chunks = 0
        chunk_dur = block_size / float(self.sample_rate)
        silence_threshold = 0.010
        silence_timeout_chunks = int(1.1 / chunk_dur)
        max_chunks = int(12.0 / chunk_dur)

        for _ in range(max_chunks):
            if self._cancel_event.is_set():
                return
            try:
                raw_data, _ = stream.read(block_size)
            except Exception:
                break
            if not raw_data:
                break
            chunk_int16 = np.frombuffer(bytes(raw_data), dtype=np.int16)
            chunk_float = chunk_int16.astype(np.float32) / 32768.0
            recorded.append(chunk_float)
            rms, _ = BargeInDetector.compute_rms_and_zcr(chunk_float)
            if rms > silence_threshold:
                silence_chunks = 0
            else:
                silence_chunks += 1
                if silence_chunks >= silence_timeout_chunks:
                    break

        if self._cancel_event.is_set() or not recorded or not self.stt or not self.on_text_ready:
            return

        audio = np.concatenate(recorded, axis=0)
        if len(audio) < int(self.sample_rate * 0.35):
            return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            text = loop.run_until_complete(self.stt.transcribe_audio_array(audio))
        except Exception as e:
            logger.debug(f"Barge-in transcription error: {e}")
            text = ""
        finally:
            loop.close()

        cleaned = (text or "").strip()
        if cleaned and cleaned not in (".", "!", "?", "...", "—") and not self._cancel_event.is_set():
            try:
                self.on_text_ready(cleaned)
            except Exception as ex:
                logger.debug(f"Barge-in dispatch error: {ex}")
