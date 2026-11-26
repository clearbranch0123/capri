"""Background QThread workers and live API key probe helpers for Cyberdeck."""
from __future__ import annotations

import asyncio
import os
import threading
from typing import Optional

from PySide6.QtCore import QThread, Signal

from ... import __version__
from ...core.agent import CapriAgent
from ...core.config import is_local_llm_endpoint


_ACTIVE_QTHREADS: set[QThread] = set()


def _track_qthread(thread: Optional[QThread]) -> None:
    """Keep a strong reference to a running QThread until its `finished` signal fires."""
    if thread is None:
        return
    if thread.isFinished():
        _ACTIVE_QTHREADS.discard(thread)
        return
    if thread not in _ACTIVE_QTHREADS:
        _ACTIVE_QTHREADS.add(thread)
        thread.finished.connect(lambda t=thread: _ACTIVE_QTHREADS.discard(t))
    if thread.isFinished():
        _ACTIVE_QTHREADS.discard(thread)


def check_api_key_sync(
    service: str,
    api_key: str,
    base_url: str = "",
    timeout: float = 6.0,
) -> tuple[bool, str]:
    """
    Lightweight, secret-safe live API key verification for Settings dialog.
    Supports `llm` (OpenAI-compatible `/models`), `fish` (Fish Audio), `elevenlabs`, and `gemini` (Vision/TTS).
    Never returns raw API keys in status messages.
    """
    import httpx
    from ...core.doctor import redact_sensitive_text

    key = (api_key or "").strip()
    svc = (service or "").strip().lower()
    is_local_llm = svc == "llm" and is_local_llm_endpoint("", base_url)
    if not key and not is_local_llm:
        return False, "Ключ не введён"

    headers: dict[str, str] = {"User-Agent": f"Capri/{__version__}"}
    if svc == "llm":
        root = (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
        url = f"{root}/models"
        if key:
            headers["Authorization"] = f"Bearer {key}"
    elif svc == "fish":
        url = "https://api.fish.audio/model?page_size=1"
        headers["Authorization"] = f"Bearer {key}"
    elif svc == "elevenlabs":
        url = "https://api.elevenlabs.io/v1/user"
        headers["xi-api-key"] = key
    elif svc == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=1&key={key}"
    else:
        return False, f"Неизвестный сервис: {service}"

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code == 200:
            if is_local_llm and not key:
                return True, "Локальный сервер доступен (HTTP 200)"
            return True, "Ключ валиден (HTTP 200)"
        if resp.status_code in (401, 403):
            return False, f"Отклонён сервисом (HTTP {resp.status_code} — проверь ключ)"
        if resp.status_code == 400 and svc == "gemini":
            return False, "Неверный Gemini API-ключ (HTTP 400)"
        return False, f"Ответ API: HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "Таймаут подключения (проверь сеть/VPN)"
    except Exception as e:
        safe_msg = redact_sensitive_text(str(e), extra_secrets=(key,))
        return False, f"Ошибка сети: {safe_msg[:90]}"


class APIKeyCheckWorker(QThread):
    """Non-blocking background worker for checking an API key from Settings."""

    result_ready = Signal(str, bool, str)  # (service_id, ok, status_message)

    def __init__(self, service: str, api_key: str, base_url: str = "", parent=None):
        super().__init__(parent)
        self.service = service
        self.api_key = api_key
        self.base_url = base_url

    def run(self):
        ok, msg = check_api_key_sync(self.service, self.api_key, base_url=self.base_url)
        self.result_ready.emit(self.service, ok, msg)


class MCPConnectWorker(QThread):
    """Background worker for non-blocking, cancellable `/mcp connect` handshakes."""

    completed = Signal(object, bool)  # (summary_dict, was_cancelled)
    failed = Signal(str)

    def __init__(self, mcp_manager, mcp_config, parent=None):
        super().__init__(parent)
        self.mcp_manager = mcp_manager
        self.mcp_config = mcp_config
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        try:
            self.mcp_manager.cancel_inflight_connect()
        except Exception:
            pass

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run(self):
        try:
            res_map = self.mcp_manager.connect_from_config_sync(
                self.mcp_config,
                cancel_event=self._cancel_event,
            )
            self.completed.emit(res_map, self._cancel_event.is_set())
        except Exception as e:
            if self._cancel_event.is_set():
                self.completed.emit({}, True)
            else:
                from ...core.doctor import redact_sensitive_text
                self.failed.emit(redact_sensitive_text(str(e)))


class AgentWorker(QThread):
    """Background worker thread for CapriAgent async turns."""

    thought_signal = Signal(str, dict)
    tool_signal = Signal(str, str)
    chunk_signal = Signal(str)
    speaking_signal = Signal()
    text_ready_signal = Signal(str)
    memory_candidates_signal = Signal(list)
    finished_signal = Signal()
    error_signal = Signal(str)
    cancelled_signal = Signal()
    approval_requested = Signal(object)

    def __init__(
        self,
        agent: CapriAgent,
        message: str,
        parent=None,
        attachments: Optional[list] = None,
        display_text: Optional[str] = None,
    ):
        super().__init__(parent)
        self.agent = agent
        self.message = message
        self.attachments = list(attachments) if attachments else []
        self.display_text = display_text
        self.is_cancelled = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._pending_approval_event: Optional[threading.Event] = None

    def cancel(self):
        self.is_cancelled = True
        if self._pending_approval_event is not None:
            self._pending_approval_event.set()
        self.agent.abort()
        if hasattr(self.agent, "tts") and self.agent.tts:
            self.agent.tts.stop_sync()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._cancel_task)

    def _cancel_task(self):
        if self._task and not self._task.done():
            self._task.cancel()

    async def _request_approval(self, req) -> bool:
        if self.is_cancelled:
            return False
        ev = threading.Event()
        box = {"approved": False}
        self._pending_approval_event = ev
        try:
            self.approval_requested.emit((req, ev, box))
            while not ev.is_set() and not self.is_cancelled:
                await asyncio.sleep(0.05)
            return bool(box["approved"]) and not self.is_cancelled
        finally:
            self._pending_approval_event = None

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self.agent.set_tool_approval_handler(self._request_approval)
        try:
            self._task = self._loop.create_task(self._chat_coro())
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            self.cancelled_signal.emit()
        except Exception as e:
            if not self.is_cancelled:
                self.error_signal.emit(str(e))
        finally:
            self.agent.set_tool_approval_handler(None)
            try:
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._loop.close()
            except Exception:
                pass

    async def _chat_coro(self):
        try:
            full_text = ""
            prompt_msg = self.message
            if self.attachments:
                from ...core.attachments import build_prompt_with_attachments
                self.thought_signal.emit("Анализирую прикреплённые материалы...", {})
                try:
                    prompt_msg = await build_prompt_with_attachments(
                        self.message,
                        self.attachments,
                        config=self.agent.config,
                    )
                finally:
                    from ...core.attachments import cleanup_temp_attachment
                    for att in self.attachments:
                        cleanup_temp_attachment(att)
                if self.is_cancelled:
                    self.cancelled_signal.emit()
                    return

            async for event in self.agent.chat(
                prompt_msg,
                speak=True,
                stream=True,
                display_user_text=self.display_text,
            ):
                if self.is_cancelled:
                    self.cancelled_signal.emit()
                    return
                if event.type == "thought":
                    self.thought_signal.emit(event.data, event.metadata)
                elif event.type == "tool_start":
                    self.tool_signal.emit(event.data, "start")
                elif event.type == "tool_result":
                    self.tool_signal.emit(event.data, "result")
                elif event.type == "delta":
                    full_text = event.data
                    self.chunk_signal.emit(event.data)
                elif event.type == "speaking":
                    self.speaking_signal.emit()
                elif event.type == "memory_candidates":
                    cands = event.metadata.get("candidates") or []
                    if cands:
                        self.memory_candidates_signal.emit(list(cands))
                elif event.type == "done":
                    full_text = event.data
                    if full_text and full_text.strip():
                        self.text_ready_signal.emit(full_text)
                elif event.type == "error":
                    self.error_signal.emit(event.data)
                    return

            if self.is_cancelled:
                self.cancelled_signal.emit()
            else:
                self.finished_signal.emit()
        except asyncio.CancelledError:
            self.cancelled_signal.emit()
        except Exception as e:
            if not self.is_cancelled:
                self.error_signal.emit(str(e))


class VoiceRecordWorker(QThread):
    """Background voice capture & STT transcription worker with cancellable async STT task."""

    text_ready = Signal(str)
    failed = Signal(str)

    def __init__(self, voice_config):
        super().__init__()
        self.config = voice_config
        self.stop_event = threading.Event()
        self.is_discarded = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None

    def stop_recording(self):
        self.stop_event.set()

    def _cancel_task(self):
        if self._task and not self._task.done():
            self._task.cancel()

    def cancel_recording(self):
        self.is_discarded = True
        self.stop_event.set()
        loop = self._loop
        if loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._cancel_task)
            except RuntimeError:
                pass

    def run(self):
        try:
            from ...voice.stt import SpeechRecognizer
            stt = SpeechRecognizer(self.config)
            audio = stt.record_audio(max_duration=6.0, silence_timeout=1.3, stop_event=self.stop_event)
            if self.is_discarded:
                self.failed.emit("Запись отменена.")
                return

            if len(audio) == 0:
                self.failed.emit("Аудиозапись пуста.")
                return

            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                self._task = loop.create_task(stt.transcribe_audio_array(audio))
                if self.is_discarded:
                    self._task.cancel()
                text = loop.run_until_complete(self._task)
            except asyncio.CancelledError:
                self.failed.emit("Запись отменена.")
                return
            finally:
                try:
                    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                self._loop = None
                self._task = None
                loop.close()

            if self.is_discarded:
                self.failed.emit("Запись отменена.")
                return

            if text and text.strip():
                self.text_ready.emit(text.strip())
            else:
                self.failed.emit("Речь не распознана.")
        except Exception as e:
            if not self.is_discarded:
                self.failed.emit(str(e))


class RegionCaptureWorker(QThread):
    """Non-blocking, cancellable Wayland region screenshot worker (`slurp` + `grim`)."""

    captured = Signal(str, str)  # (file_path_or_empty, status)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cancel_event = threading.Event()
        self._path_lock = threading.Lock()
        self._pending_path: Optional[str] = None

    def cancel(self) -> None:
        self.cancel_event.set()

    def claim_pending_path(self) -> Optional[str]:
        """Atomically transfer ownership of the captured temp file to the UI handler."""
        with self._path_lock:
            p = self._pending_path
            self._pending_path = None
            return p

    def cleanup_unclaimed_path(self) -> None:
        """Delete any captured temp screenshot that was never claimed by the UI handler."""
        p = self.claim_pending_path()
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    def run(self):
        path = ""
        try:
            from ...tools.screen import capture_screen_region_sync
            cap_path, status = capture_screen_region_sync(cancel_event=self.cancel_event)
            path = cap_path or ""
            if path:
                with self._path_lock:
                    self._pending_path = path
            if self.cancel_event.is_set():
                self.cleanup_unclaimed_path()
                return
            self.captured.emit(path, status)
        except Exception:
            self.cleanup_unclaimed_path()
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            if not self.cancel_event.is_set():
                self.captured.emit("", "error")


class MicCalibrationWorker(QThread):
    """Guided 3-second microphone noise-floor + speech RMS calibration worker for Barge-In."""

    level_changed = Signal(int)      # 0..100 VU meter percent
    phase_changed = Signal(str)      # Russian phase hint
    calibration_ready = Signal(object)  # CalibrationResult
    failed = Signal(str)

    def __init__(self, input_device: str = "", sample_rate: int = 16000, parent=None):
        super().__init__(parent)
        self.input_device = (input_device or "").strip()
        self.sample_rate = sample_rate
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        from ...voice.pipewire import (
            compute_chunk_rms,
            recommend_barge_in_threshold,
            resolve_sounddevice_input,
            rms_to_meter_percent,
        )
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as e:
            self.failed.emit(f"Модуль захвата звука недоступен: {e}")
            return

        block_size = int(self.sample_rate * 0.08)  # 80ms frames
        dev_idx = resolve_sounddevice_input(self.input_device)
        stream_kwargs = {
            "samplerate": self.sample_rate,
            "blocksize": block_size,
            "channels": 1,
            "dtype": "float32",
        }
        if dev_idx is not None:
            stream_kwargs["device"] = dev_idx

        noise_samples: list[float] = []
        speech_samples: list[float] = []

        try:
            with sd.InputStream(**stream_kwargs) as stream:
                # Phase 1: 0.9s ambient room / speaker noise floor
                self.phase_changed.emit("Шаг 1/2: 1 сек тишины — замеряю фоновый шум...")
                for _ in range(11):
                    if self._stop_event.is_set():
                        return
                    chunk, _ = stream.read(block_size)
                    rms = compute_chunk_rms(chunk.flatten())
                    noise_samples.append(rms)
                    self.level_changed.emit(rms_to_meter_percent(rms))

                # Phase 2: 2.1s user normal speaking voice
                self.phase_changed.emit("Шаг 2/2: Скажи короткую фразу обычным голосом...")
                for _ in range(26):
                    if self._stop_event.is_set():
                        return
                    chunk, _ = stream.read(block_size)
                    rms = compute_chunk_rms(chunk.flatten())
                    speech_samples.append(rms)
                    self.level_changed.emit(rms_to_meter_percent(rms))
        except Exception as e:
            if not self._stop_event.is_set():
                self.failed.emit(f"Ошибка микрофона: {e}")
            return

        if self._stop_event.is_set():
            return

        noise_rms = float(np.percentile(noise_samples, 75)) if noise_samples else 0.006
        speech_rms = float(np.percentile(speech_samples, 85)) if speech_samples else noise_rms
        res = recommend_barge_in_threshold(noise_rms, speech_rms)
        self.level_changed.emit(0)
        self.calibration_ready.emit(res)
