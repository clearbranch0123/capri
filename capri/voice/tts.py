from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Any
from .phonetics import normalize_phonetics
from ..core.resources import get_asset_path, resolve_asset_path

if TYPE_CHECKING:
    from ..core.config import VoiceConfig

ROOT_DIR = Path(__file__).resolve().parent.parent.parent

_ABBREV_END_RE = re.compile(
    r"(?:\b(?:т\.д|т\.е|т\.п|др|пр|см|стр|гг|им|ул|д|кв|рис|табл|etc|e\.g|i\.e|dr|mr|mrs|ms|vs|fig|no)\.$)",
    re.IGNORECASE,
)


class SentenceStreamSplitter:
    """
    Incremental sentence splitter for live LLM token streaming -> TTS pipeline.
    - Feeds cumulative visible text (`feed_cumulative`) without repeating already-emitted text.
    - Emits the first complete sentence as early as `min_first_chars` (~<1s time-to-first-audio).
    - Respects Markdown fenced code blocks (never splits inside an unclosed ``` block).
    - Avoids false splits on decimals, version numbers (`v2.8.0b2`), and common abbreviations.
    """

    def __init__(self, min_first_chars: int = 10, min_next_chars: int = 28):
        self.min_first_chars = max(4, int(min_first_chars))
        self.min_next_chars = max(8, int(min_next_chars))
        self._emitted_offset: int = 0
        self._emitted_count: int = 0
        self._latest_text: str = ""

    @staticmethod
    def _inside_code_fence(text: str, pos: int) -> bool:
        return (text[:pos].count("```") % 2) == 1

    def feed_cumulative(self, visible_text: str) -> list[str]:
        if not visible_text:
            return []
        self._latest_text = visible_text
        if self._emitted_offset >= len(visible_text):
            return []

        ready: list[str] = []
        while self._emitted_offset < len(visible_text):
            pending = visible_text[self._emitted_offset:]
            min_len = self.min_first_chars if self._emitted_count == 0 else self.min_next_chars

            split_rel = self._find_split_point(visible_text, self._emitted_offset, pending, min_len)
            if split_rel is None:
                break

            chunk = pending[:split_rel].strip()
            self._emitted_offset += split_rel
            # Skip leading whitespace after boundary so offset stays aligned
            while self._emitted_offset < len(visible_text) and visible_text[self._emitted_offset].isspace():
                self._emitted_offset += 1

            if chunk:
                ready.append(chunk)
                self._emitted_count += 1
        return ready

    def _find_split_point(
        self, full_text: str, base_offset: int, pending: str, min_len: int
    ) -> Optional[int]:
        # 1. Look for sentence-ending punctuation followed by whitespace (not decimal/version/abbrev)
        for m in re.finditer(r"([.!?…]+)(\s+)", pending):
            punct_end = m.end(1)
            abs_end = base_offset + punct_end
            if self._inside_code_fence(full_text, abs_end):
                continue

            # Ignore decimals / version strings like "2.8.0" or "v2.8."
            if m.group(1) == ".":
                before = pending[: m.start(1)]
                if before and before[-1].isdigit() and m.end(1) < len(pending) and pending[m.end(1)].isdigit():
                    continue
                if _ABBREV_END_RE.search(pending[:punct_end]):
                    continue

            candidate = pending[:punct_end].strip()
            if len(candidate) >= min_len:
                return m.end(2)

        # 2. Paragraph break (`\n\n`) outside code fence
        para_idx = pending.find("\n\n")
        if para_idx >= min_len and not self._inside_code_fence(full_text, base_offset + para_idx):
            return para_idx + 2

        return None

    def flush(self) -> list[str]:
        if not self._latest_text or self._emitted_offset >= len(self._latest_text):
            return []
        tail = self._latest_text[self._emitted_offset:].strip()
        self._emitted_offset = len(self._latest_text)
        if tail:
            self._emitted_count += 1
            return [tail]
        return []


def compute_speech_envelope(audio_path: Optional[str], phrase: str = "", fps: int = 15) -> list[int]:
    """
    Build a 0..4 mouth-openness lip-sync envelope synced to ~15 FPS:
    - For WAV audio: computes real per-frame RMS amplitude directly from PCM samples.
    - For MP3/compressed audio: generates a syllable/phoneme-timed cadence envelope
      from `phrase` (open vowels -> 3..4, soft vowels -> 2, consonants -> 1, pauses -> 0).
    """
    if audio_path and audio_path.lower().endswith(".wav") and os.path.exists(audio_path):
        try:
            import numpy as np

            with wave.open(audio_path, "rb") as wf:
                n_channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                rate = wf.getframerate()
                n_frames = wf.getnframes()
                raw = wf.readframes(n_frames)
            if sampwidth == 2 and rate > 0 and raw:
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if n_channels > 1:
                    samples = samples.reshape(-1, n_channels).mean(axis=1)
                step = max(1, int(rate / max(1, fps)))
                rms_vals: list[float] = []
                for idx in range(0, len(samples), step):
                    sl = samples[idx : idx + step]
                    if sl.size > 0:
                        rms_vals.append(float(np.sqrt(np.mean(sl * sl))))
                if rms_vals:
                    peak = max(max(rms_vals), 0.05)
                    env = [min(4, max(0, int(round((v / peak) * 4.0)))) for v in rms_vals]
                    return env[:300]
        except Exception:
            pass

    # Phoneme/cadence envelope for MP3 streams or when WAV decoding isn't available
    txt = (phrase or "").strip().lower()
    if not txt:
        return [1, 3, 2, 4, 2, 1, 0]
    wide_vowels = set("аоуяёaou")
    mid_vowels = set("еиэюыei")
    pauses = set(",.;:!?—-…\n")
    env: list[int] = []
    for ch in txt:
        if ch in pauses:
            env.extend([0, 0])
        elif ch.isspace():
            env.append(0)
        elif ch in wide_vowels:
            env.extend([4, 3])
        elif ch in mid_vowels:
            env.append(2)
        elif ch.isalpha():
            env.append(1)
    if not env:
        return [1, 2, 3, 2, 0]
    env.append(0)
    return env[:300]


def chunk_text_for_speech(text: str, min_chars: int = 30) -> list[str]:
    """Разбивает текст на логические предложения для потокового/конвейерного воспроизведения без пауз."""
    text = re.sub(r"[:;=]-?[)(/\\pPdDoO3><]+", "", text)
    raw = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text.replace("\n", " ")) if s.strip()]
    if not raw:
        return [text] if text.strip() else []

    # Первое предложение (вводное/приветствие) отдаем первым отдельным чанком,
    # чтобы синтез первого звука стартовал максимально быстро (~1.3-1.5 сек).
    chunks = [raw[0]]
    cur = ""
    for s in raw[1:]:
        if not cur:
            cur = s
        elif len(cur) < min_chars:
            cur += " " + s
        else:
            chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return chunks


def clean_text_for_tts(text: str, engine: str = "fish") -> str:
    """Очищает текст от markdown, кода, ссылок, эмодзи, и КАТЕГОРИЧЕСКИ от любых служебных мыслей/appraisal."""
    # 1. Безусловное удаление блоков когнитивной оценки и скрытых мыслей модели
    text = re.sub(r"<appraisal\b[\s\S]*?(?:</appraisal>|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<think\b[\s\S]*?(?:</think>|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<thought\b[\s\S]*?(?:</thought>|$)", "", text, flags=re.IGNORECASE)

    # 2. Удаление кратких заголовков мыслей и категорий в скобках [ROAST ...], [CHAT ...] и CoT
    text = re.sub(
        r"^\s*\[(?:TASK|CHAT|WARM|ROAST|RUDE|APOLOGY)[^\]]*\][^\n]*\n*",
        "",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    text = re.sub(
        r"\[(?:TASK|CHAT|WARM|ROAST|RUDE|APOLOGY)[^\]]*\]",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"[\*\(\[]+\s*(?:мысли|мысль|thought|внутренний монолог)[^\*\]\)]*[\*\)\]]+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\[(?:DEEPSEEK|THOUGHT|CONF|МЫСЛИ)[^\]]*\][^\n]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^\s*\(?(?:Мысли|Мысль|Thought|CoT|Внутренний монолог):\s*[^\n\)]+\)?\n*",
        "",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )

    # 3. Удаление JSON-объектов appraisal, если они каким-то образом утекли в чистый текст
    text = re.sub(
        r"\{[^{}]*\"(?:category|intent|delta_trust|delta_irritation|confidence|mood|strategy)\"[^{}]*\}",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # 3. Удаление строк с служебными параметрами appraisal (tone:, felt:, delta_trust:, etc.)
    text = re.sub(
        r"^(?:tone|felt|intent|category|confidence|delta_trust|delta_irritation|mood|strategy)\s*:.*$",
        "",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )

    # 4. Удаление блоков кода, ссылок, тегов
    text = re.sub(r"```[\s\S]*?```", "код скрыт", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"<[^>]+>", "", text)

    # 5. Маркеры списков
    text = re.sub(r"^[ \t]*[•\*\-][ \t]+", "", text, flags=re.MULTILINE)

    # Для корректного звучания имени D в русской речи
    text = re.sub(r"\bD\b", "Ди", text)

    # Для Silero (чисто русский токенайзер) нужна адаптация под кириллицу и ударения
    if engine in ("silero", "baya"):
        text = normalize_phonetics(text)
        text = re.sub(r"\bДи\b", "Д+и", text)

    # Очистка оставшихся спецсимволов markdown и кавычек
    text = re.sub(r"[*#_~>«»\"]", " ", text)
    text = re.sub(r"[–—]", "-", text)

    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"
        "\U0001FA70-\U0001FAFF"
        "]+",
        flags=re.UNICODE,
    )
    text = emoji_pattern.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class XTTSDaemonClient:
    def __init__(self, venv_python: Path, worker_script: Path):
        self.venv_python = venv_python
        self.worker_script = worker_script
        self.proc: Optional[asyncio.subprocess.Process] = None

    async def ensure_running(self):
        if self.proc is None or self.proc.returncode is not None:
            if not self.venv_python.exists():
                raise FileNotFoundError(f"Python 3.10 environment not found at {self.venv_python}")

            print("[XTTS] Запуск локального воркера на GPU...", flush=True)
            self.proc = await asyncio.create_subprocess_exec(
                str(self.venv_python),
                str(self.worker_script),
                "--daemon",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Ждем готовности модели
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode().strip()
                if "READY" in decoded:
                    print("[XTTS] Воркер готов к синтезу!", flush=True)
                    break

    async def synthesize(self, text: str, output_path: str, speaker_path: str) -> bool:
        await self.ensure_running()
        if not self.proc or not self.proc.stdin or not self.proc.stdout:
            return False

        req = json.dumps({"text": text, "output": output_path, "speaker": speaker_path, "language": "ru"})
        self.proc.stdin.write((req + "\n").encode())
        await self.proc.stdin.drain()

        while True:
            res_line = await self.proc.stdout.readline()
            if not res_line:
                return False
            decoded = res_line.decode().strip()
            if not decoded:
                continue
            try:
                res = json.loads(decoded)
                if res.get("status") == "ok":
                    dur = res.get("duration", 0)
                    print(f"[XTTS] Сгенерировано за {dur} сек. Воспроизведение...", flush=True)
                    return True
                else:
                    print(f"[XTTS Worker Error]: {res.get('message')}", flush=True)
                    return False
            except json.JSONDecodeError:
                continue

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await self.proc.wait()
            except Exception:
                pass
            self.proc = None


class SileroDaemonClient:
    def __init__(self, venv_python: Path, worker_script: Path):
        self.venv_python = venv_python
        self.worker_script = worker_script
        self.proc: Optional[asyncio.subprocess.Process] = None

    async def ensure_running(self):
        if self.proc is None or self.proc.returncode is not None:
            if not self.venv_python.exists():
                raise FileNotFoundError(f"Python environment not found at {self.venv_python}")

            self.proc = await asyncio.create_subprocess_exec(
                str(self.venv_python),
                str(self.worker_script),
                "--daemon",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode().strip()
                if "READY" in decoded:
                    break

    async def synthesize(self, text: str, output_path: str, speaker: str = "baya") -> bool:
        await self.ensure_running()
        if not self.proc or not self.proc.stdin or not self.proc.stdout:
            return False

        req = json.dumps({"text": text, "output": output_path, "speaker": speaker, "sample_rate": 24000})
        self.proc.stdin.write((req + "\n").encode())
        await self.proc.stdin.drain()

        while True:
            res_line = await self.proc.stdout.readline()
            if not res_line:
                return False
            decoded = res_line.decode().strip()
            if not decoded:
                continue
            try:
                res = json.loads(decoded)
                if res.get("status") == "ok":
                    return True
                else:
                    return False
            except json.JSONDecodeError:
                continue

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await self.proc.wait()
            except Exception:
                pass
            self.proc = None


class StreamingTTSSession:
    """
    Sentence-level streaming TTS pipeline with parallel synthesis prefetch
    and immediate cancellation/barge-in support.
    """

    def __init__(self, tts: "VoiceSynthesizer"):
        self.tts = tts
        self.engine = (tts.config.tts_engine or "edge").lower()
        self._queue: asyncio.Queue[Optional[tuple[str, asyncio.Task[Optional[str]]]]] = asyncio.Queue()
        self._consumer_task: Optional[asyncio.Task] = None
        self._synth_tasks: list[asyncio.Task] = []
        self._cancelled = False
        self._pushed_count = 0
        self._played_count = 0

    @property
    def pushed_count(self) -> int:
        return self._pushed_count

    @property
    def played_count(self) -> int:
        return self._played_count

    def push(self, raw_sentence: str) -> bool:
        if self._cancelled or self.tts._is_stopped or not self.tts.config.tts_enabled:
            return False
        cleaned = clean_text_for_tts(raw_sentence, engine=self.engine)
        if not cleaned:
            return False
        if self._consumer_task is None:
            self._consumer_task = asyncio.create_task(self._consume())
        synth_task = asyncio.create_task(self.tts._synthesize_chunk_file(cleaned))
        self._synth_tasks.append(synth_task)
        self._queue.put_nowait((cleaned, synth_task))
        self._pushed_count += 1
        return True

    async def _consume(self):
        try:
            while not self._cancelled and not self.tts._is_stopped:
                item = await self._queue.get()
                if item is None:
                    break
                phrase, synth_task = item
                audio_path: Optional[str] = None
                try:
                    audio_path = await synth_task
                except asyncio.CancelledError:
                    break
                except Exception as ex:
                    print(f"[Streaming TTS] Ошибка синтеза фрагмента: {ex}")
                    continue

                if self._cancelled or self.tts._is_stopped:
                    if audio_path and os.path.exists(audio_path):
                        try:
                            os.remove(audio_path)
                        except OSError:
                            pass
                    break

                if not audio_path or not os.path.exists(audio_path):
                    continue

                played = await self.tts._play_audio_file(audio_path, phrase=phrase)
                if played:
                    self._played_count += 1
        except asyncio.CancelledError:
            pass
        finally:
            self._cleanup_pending_tasks()
            if self.tts._active_stream_session is self:
                self.tts._active_stream_session = None
            try:
                from ..ui.client import notify_avatar
                notify_avatar("idle")
            except Exception:
                pass

    def _cleanup_pending_tasks(self):
        for t in self._synth_tasks:
            if not t.done():
                t.cancel()
            else:
                try:
                    res = t.result()
                    if isinstance(res, str) and os.path.exists(res):
                        os.remove(res)
                except Exception:
                    pass
        self._synth_tasks.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except Exception:
                break

    async def finish(self, timeout: float = 90.0) -> int:
        if self._cancelled or self._consumer_task is None:
            return self._played_count
        await self._queue.put(None)
        try:
            await asyncio.wait_for(self._consumer_task, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.cancel()
        return self._played_count

    def cancel(self):
        if self._cancelled:
            return
        self._cancelled = True
        if self._consumer_task and not self._consumer_task.done():
            try:
                self._consumer_task.cancel()
            except Exception:
                pass
        self._cleanup_pending_tasks()


class VoiceSynthesizer:
    def __init__(self, config: VoiceConfig):
        self.config = config
        self._current_process: Optional[asyncio.subprocess.Process] = None
        self._mirror_process: Optional[asyncio.subprocess.Process] = None
        self._silero_model = None
        self.reference_wav = get_asset_path("capri_ru_dub_clean_20s.wav")
        if not self.reference_wav.exists():
            self.reference_wav = get_asset_path("capri_voice_clean.wav")
        if not self.reference_wav.exists() and getattr(config, "xtts_speaker_wav", ""):
            self.reference_wav = resolve_asset_path(config.xtts_speaker_wav)

        # Worker daemons setup
        xtts_python = ROOT_DIR / ".venv-xtts" / "bin" / "python"
        xtts_worker = Path(__file__).resolve().parent / "xtts_worker.py"
        silero_worker = Path(__file__).resolve().parent / "silero_worker.py"
        self._xtts_client = XTTSDaemonClient(xtts_python, xtts_worker)
        self._silero_client = SileroDaemonClient(xtts_python, silero_worker)
        self._active_task: Optional[asyncio.Task] = None
        self._active_stream_session: Optional[StreamingTTSSession] = None
        self._is_stopped = False
        self._stop_event = threading.Event()
        self._fish_ref_bytes_cache: Optional[bytes] = None

    def start_stream_session(self) -> StreamingTTSSession:
        """Start a sentence-streaming TTS session (prefetch + ordered playback + instant interrupt)."""
        if self._active_stream_session is not None:
            self._active_stream_session.cancel()
        self._is_stopped = False
        self._stop_event.clear()
        session = StreamingTTSSession(self)
        self._active_stream_session = session
        return session

    def interrupt(self):
        """Immediate barge-in / playback interruption hook."""
        self.stop_sync()

    async def stop(self):
        """Остановить текущее воспроизведение голоса."""
        self.stop_sync()

    def stop_sync(self):
        """Синхронно прерывает любое активное воспроизведение звука и принудительно убивает дочерние процессы."""
        self._is_stopped = True
        self._stop_event.set()

        if self._active_stream_session is not None:
            sess = self._active_stream_session
            self._active_stream_session = None
            sess.cancel()

        if self._active_task and not self._active_task.done():
            try:
                self._active_task.cancel()
            except Exception:
                pass
            self._active_task = None

        for proc_attr in ("_current_process", "_mirror_process"):
            proc = getattr(self, proc_attr, None)
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    proc.terminate()
                except Exception:
                    pass
            setattr(self, proc_attr, None)

        # Гарантированно глушим любые дочерние mpv процессы синтеза речи
        try:
            subprocess.run(["pkill", "-9", "-f", "mpv.*tmp.*"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "-f", "mpv.*--really-quiet.*"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "-f", "mpv.*capri"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "-f", "paplay.*capri"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "-f", "pw-play.*tmp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        try:
            from ..ui.client import notify_avatar
            notify_avatar("idle")
        except Exception:
            pass

    def _get_fish_ref_bytes(self) -> Optional[bytes]:
        if self._fish_ref_bytes_cache is not None:
            return self._fish_ref_bytes_cache
        ref_file = get_asset_path("capri_ru_dub_clean_20s.wav")
        if not ref_file.exists():
            ref_file = self.reference_wav
        if ref_file.exists():
            try:
                self._fish_ref_bytes_cache = ref_file.read_bytes()
            except OSError:
                self._fish_ref_bytes_cache = None
        return self._fish_ref_bytes_cache

    async def _synthesize_edge_file(self, text: str) -> Optional[str]:
        if self._is_stopped:
            return None
        import edge_tts

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            communicate = edge_tts.Communicate(
                text=text,
                voice=self.config.tts_voice,
                rate=self.config.tts_rate,
                pitch=self.config.tts_pitch,
            )
            await asyncio.wait_for(communicate.save(tmp_path), timeout=20.0)
            if self._is_stopped or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return None
            return tmp_path
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            if not self._is_stopped:
                print(f"[Edge TTS Error]: {e}")
            return None

    async def _synthesize_silero_file(self, text: str) -> Optional[str]:
        if self._is_stopped:
            return None
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            success = await self._silero_client.synthesize(text, tmp_path, speaker="baya")
            if self._is_stopped or not success or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return None
            return tmp_path
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return None

    async def _synthesize_xtts_file(self, text: str) -> Optional[str]:
        if self._is_stopped:
            return None
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            success = await self._xtts_client.synthesize(text, tmp_path, str(self.reference_wav))
            if self._is_stopped or not success or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return None
            return tmp_path
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return None

    async def _synthesize_fish_file(self, text: str) -> Optional[str]:
        if self._is_stopped:
            return None
        api_key = self.config.fish_audio_api_key or os.getenv("FISH_AUDIO_API_KEY", "")
        if not api_key:
            return None
        try:
            from fishaudio import AsyncFishAudio
            from fishaudio.types import ReferenceAudio

            client = AsyncFishAudio(api_key=api_key)
            voice_id = self.config.fish_audio_voice_id or os.getenv("FISH_AUDIO_VOICE_ID", "")
            kwargs: dict[str, Any] = {
                "text": text,
                "format": "mp3",
                "model": "s2.1-pro-free",
                "latency": "balanced",
            }
            if voice_id:
                kwargs["reference_id"] = voice_id
            else:
                ref_bytes = self._get_fish_ref_bytes()
                if ref_bytes:
                    kwargs["references"] = [
                        ReferenceAudio(
                            audio=ref_bytes,
                            text="Оцифрованные человеческие души, много душ собранных в одно",
                        )
                    ]
            audio_data = await asyncio.wait_for(client.tts.convert(**kwargs), timeout=15.0)
            if self._is_stopped or not audio_data:
                return None
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp.write(audio_data)
                return tmp.name
        except Exception as ex:
            if not self._is_stopped:
                print(f"[Fish Audio] Ошибка синтеза фрагмента '{text[:30]}...': {ex}")
            return None

    async def _synthesize_elevenlabs_file(self, text: str) -> Optional[str]:
        if self._is_stopped:
            return None
        api_key = self.config.elevenlabs_api_key or os.getenv("ELEVENLABS_API_KEY", "")
        voice_id = self.config.elevenlabs_voice_id or os.getenv("ELEVENLABS_VOICE_ID", "")
        if not api_key or not voice_id:
            return None
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            from elevenlabs.client import AsyncElevenLabs
            client = AsyncElevenLabs(api_key=api_key)
            audio_stream = await client.text_to_speech.convert(
                voice_id=voice_id,
                text=text,
                model_id="eleven_multilingual_v2",
            )
            with open(tmp_path, "wb") as f:
                async for chunk in audio_stream:
                    if self._is_stopped:
                        break
                    f.write(chunk)
            if self._is_stopped or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return None
            return tmp_path
        except Exception as ex:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            if not self._is_stopped:
                print(f"[ElevenLabs Error]: {ex}")
            return None

    async def _synthesize_gemini_file(self, text: str) -> Optional[str]:
        if self._is_stopped:
            return None
        api_key = self.config.gemini_api_key or os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            return None
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            voice_name = getattr(self.config, "gemini_voice", "Puck")
            res = await client.aio.models.generate_content(
                model="gemini-2.0-flash",
                contents=f"Say in character as a sharp cyberpunk companion: {text}",
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                        )
                    ),
                ),
            )
            written = False
            if res.candidates:
                for cand in res.candidates:
                    if cand.content and cand.content.parts:
                        for part in cand.content.parts:
                            if part.inline_data and part.inline_data.data:
                                with open(tmp_path, "wb") as f:
                                    f.write(part.inline_data.data)
                                written = True
                                break
            if self._is_stopped or not written or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return None
            return tmp_path
        except Exception as ex:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            if not self._is_stopped:
                print(f"[Gemini Audio Error]: {ex}")
            return None

    async def _synthesize_chunk_file(self, clean_text: str) -> Optional[str]:
        """Synthesize a single cleaned sentence with automatic fallback chain."""
        if self._is_stopped or not clean_text:
            return None
        engine = (self.config.tts_engine or "edge").lower()
        path: Optional[str] = None
        if engine in ("fish", "fish_audio", "fishaudio"):
            path = await self._synthesize_fish_file(clean_text)
        elif engine in ("eleven", "elevenlabs", "11labs"):
            path = await self._synthesize_elevenlabs_file(clean_text)
        elif engine in ("gemini", "google"):
            path = await self._synthesize_gemini_file(clean_text)
        elif engine in ("xtts", "clone"):
            path = await self._synthesize_xtts_file(clean_text)
        elif engine in ("silero", "baya"):
            path = await self._synthesize_silero_file(clean_text)
        else:
            path = await self._synthesize_edge_file(clean_text)

        if path is None and not self._is_stopped and engine not in ("silero", "baya", "edge"):
            path = await self._synthesize_silero_file(clean_text)
        if path is None and not self._is_stopped and engine != "edge":
            path = await self._synthesize_edge_file(clean_text)
        return path

    async def _play_audio_file(self, audio_path: str, phrase: str = "") -> bool:
        """Play a temporary audio file via mpv (with pw-play/paplay fallback) and always delete it."""
        if self._is_stopped or not audio_path or not os.path.exists(audio_path):
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
            return False

        try:
            from ..ui.client import notify_avatar
            envelope = compute_speech_envelope(audio_path, phrase=phrase, fps=15)
            notify_avatar("speaking", phrase, metadata={"mouth_envelope": envelope})
        except Exception:
            pass

        cmd: Optional[list[str]] = None
        if shutil.which("mpv"):
            cmd = [
                "mpv",
                "--no-config",
                "--keep-open=no",
                "--terminal=no",
                "--no-video",
                "--really-quiet",
                audio_path,
            ]
        elif shutil.which("pw-play"):
            cmd = ["pw-play", audio_path]
        elif shutil.which("paplay"):
            cmd = ["paplay", audio_path]

        if cmd is None:
            try:
                os.remove(audio_path)
            except OSError:
                pass
            return False

        mirror_cmd: Optional[list[str]] = None
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            try:
                from .join_voice import CAPRI_VOICE_BUS_SINK, is_join_voice_active
                if is_join_voice_active():
                    if shutil.which("mpv"):
                        mirror_cmd = [
                            "mpv",
                            "--no-config",
                            "--keep-open=no",
                            "--terminal=no",
                            "--no-video",
                            "--really-quiet",
                            f"--audio-device=pulse/{CAPRI_VOICE_BUS_SINK}",
                            audio_path,
                        ]
                    elif shutil.which("paplay"):
                        mirror_cmd = ["paplay", "-d", CAPRI_VOICE_BUS_SINK, audio_path]
            except Exception:
                mirror_cmd = None

        try:
            self._current_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
            if mirror_cmd is not None:
                self._mirror_process = await asyncio.create_subprocess_exec(
                    *mirror_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    stdin=asyncio.subprocess.DEVNULL,
                )
            if self._mirror_process is not None:
                await asyncio.gather(
                    self._current_process.wait(),
                    self._mirror_process.wait(),
                    return_exceptions=True,
                )
            else:
                await self._current_process.wait()
            return not self._is_stopped
        except asyncio.CancelledError:
            for p in (self._current_process, self._mirror_process):
                if p and p.returncode is None:
                    try:
                        p.kill()
                    except Exception:
                        pass
            raise
        except Exception:
            return False
        finally:
            self._current_process = None
            self._mirror_process = None
            if os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

    async def speak(self, text: str):
        """Озвучивает текст в зависимости от выбранного движка."""
        if not self.config.tts_enabled:
            return

        self._is_stopped = False
        self._stop_event.clear()

        engine = self.config.tts_engine.lower()
        clean_text = clean_text_for_tts(text, engine=engine)
        if not clean_text or self._is_stopped:
            return

        self._active_task = asyncio.current_task()
        try:
            if engine in ("fish", "fish_audio", "fishaudio"):
                await self._speak_fish(clean_text)
            elif engine in ("eleven", "elevenlabs", "11labs"):
                await self._speak_elevenlabs(clean_text)
            elif engine in ("gemini", "google"):
                await self._speak_gemini(clean_text)
            elif engine in ("silero", "baya"):
                await self._speak_silero(clean_text)
            elif engine in ("xtts", "clone"):
                await self._speak_xtts(clean_text)
            else:
                await self._speak_edge(clean_text)
        except asyncio.CancelledError:
            self.stop_sync()
        finally:
            self._active_task = None

    async def _speak_silero(self, text: str):
        """Синтез через Silero (Baya) на GPU через постоянный воркер."""
        if self._is_stopped:
            return
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            success = await self._silero_client.synthesize(text, tmp_path, speaker="baya")
            if self._is_stopped:
                return
            if not success or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                if not self._is_stopped:
                    print("[Silero] Ошибка синтеза, переключаюсь на Edge-TTS...")
                    await self._speak_edge(text)
                return

            self._current_process = await asyncio.create_subprocess_exec(
                "mpv",
                "--no-config",
                "--keep-open=no",
                "--terminal=no",
                "--no-video",
                "--really-quiet",
                tmp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
            await self._current_process.wait()
        except asyncio.CancelledError:
            if self._current_process:
                try: self._current_process.kill()
                except Exception: pass
            raise
        except Exception as e:
            if not self._is_stopped:
                print(f"[Silero Error]: {e}, переключаюсь на Edge-TTS...")
                await self._speak_edge(text)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            self._current_process = None

    async def _speak_xtts(self, text: str):
        """Локальное клонирование голоса Капри через XTTS-v2 на RTX 3050."""
        if self._is_stopped:
            return
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            speaker = str(self.reference_wav)
            success = await self._xtts_client.synthesize(text, tmp_path, speaker)
            if self._is_stopped:
                return
            if not success or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                if not self._is_stopped:
                    print("[XTTS] Ошибка синтеза, переключаюсь на Silero...")
                    await self._speak_silero(text)
                return

            self._current_process = await asyncio.create_subprocess_exec(
                "mpv",
                "--no-config",
                "--keep-open=no",
                "--terminal=no",
                "--no-video",
                "--really-quiet",
                tmp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
            await self._current_process.wait()
        except asyncio.CancelledError:
            if self._current_process:
                try: self._current_process.kill()
                except Exception: pass
            raise
        except Exception as e:
            if not self._is_stopped:
                print(f"[XTTS Error]: {e}, переключаюсь на Silero...")
                await self._speak_silero(text)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            self._current_process = None

    async def _speak_fish(self, text: str):
        """Синтез через Fish Audio API с конвейерным воспроизведением (Sentence Pipeline)."""
        if self._is_stopped:
            return

        api_key = self.config.fish_audio_api_key or os.getenv("FISH_AUDIO_API_KEY", "")
        if not api_key:
            if not self._is_stopped:
                print("[Fish Audio] FISH_AUDIO_API_KEY не найден в .env, переключаюсь на резервный звук...")
                await self._speak_silero(text)
            return

        chunks = chunk_text_for_speech(text)
        if not chunks or self._is_stopped:
            return

        fetch_tasks = []
        try:
            from fishaudio import AsyncFishAudio
            from fishaudio.types import ReferenceAudio

            client = AsyncFishAudio(api_key=api_key)
            voice_id = self.config.fish_audio_voice_id or os.getenv("FISH_AUDIO_VOICE_ID", "")

            ref_bytes = self._get_fish_ref_bytes() if not voice_id else None

            async def fetch_chunk(chunk_text: str) -> Optional[str]:
                if self._is_stopped:
                    return None
                kwargs = {
                    "text": chunk_text,
                    "format": "mp3",
                    "model": "s2.1-pro-free",
                    "latency": "balanced",
                }
                if voice_id:
                    kwargs["reference_id"] = voice_id
                elif ref_bytes:
                    kwargs["references"] = [
                        ReferenceAudio(audio=ref_bytes, text="Оцифрованные человеческие души, много душ собранных в одно")
                    ]

                try:
                    # Даем до 15 секунд на генерацию одного чанка API
                    audio_data = await asyncio.wait_for(client.tts.convert(**kwargs), timeout=15.0)
                except Exception as ex:
                    print(f"[Fish Audio] Ошибка конвертации чанка '{chunk_text[:30]}...': {ex}")
                    return None

                if self._is_stopped:
                    return None

                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    tmp.write(audio_data)
                    return tmp.name

            # Запускаем параллельную предзагрузку ВСЕХ чанков ответа сразу,
            # чтобы последующие предложения были уже готовы на диске к моменту окончания первого.
            fetch_tasks = [asyncio.create_task(fetch_chunk(c)) for c in chunks]
            played_count = 0

            for i in range(len(chunks)):
                if self._is_stopped:
                    break
                chunk_file = await fetch_tasks[i]
                if self._is_stopped:
                    if chunk_file and os.path.exists(chunk_file):
                        try: os.remove(chunk_file)
                        except Exception: pass
                    break

                if not chunk_file or not os.path.exists(chunk_file):
                    continue

                # Оповещаем UI-аватар о текущей фразе
                try:
                    from ..ui.client import notify_avatar
                    notify_avatar("speaking", chunks[i])
                except Exception:
                    pass

                self._current_process = await asyncio.create_subprocess_exec(
                    "mpv",
                    "--no-config",
                    "--keep-open=no",
                    "--terminal=no",
                    "--no-video",
                    "--really-quiet",
                    chunk_file,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    stdin=asyncio.subprocess.DEVNULL,
                )
                try:
                    await self._current_process.wait()
                    played_count += 1
                except asyncio.CancelledError:
                    if self._current_process:
                        try: self._current_process.kill()
                        except Exception: pass
                    raise
                finally:
                    self._current_process = None
                    try:
                        os.remove(chunk_file)
                    except Exception:
                        pass

                if self._is_stopped:
                    break

            # Если Fish Audio не вернул ни одного аудио-чанка (тихий сбой API), автоматически каскадируем в Silero
            if played_count == 0 and not self._is_stopped:
                print("[Fish Audio] Аудио-чанки не получены от API, переключаюсь на Silero fallback...")
                await self._speak_silero(text)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self._is_stopped:
                print(f"[Fish Audio Error]: {e}, переключаюсь на резервный звук...")
                await self._speak_silero(text)
        finally:
            for t in fetch_tasks:
                if not t.done():
                    t.cancel()
            self._current_process = None
            try:
                from ..ui.client import notify_avatar
                notify_avatar("idle")
            except Exception:
                pass

    async def _speak_elevenlabs(self, text: str):
        """Синтез через ElevenLabs API (студийный клон 1-в-1)."""
        api_key = self.config.elevenlabs_api_key or os.getenv("ELEVENLABS_API_KEY", "")
        voice_id = self.config.elevenlabs_voice_id or os.getenv("ELEVENLABS_VOICE_ID", "")
        if not api_key or not voice_id:
            print("[ElevenLabs] ELEVENLABS_API_KEY или ELEVENLABS_VOICE_ID не найдены в .env, переключаюсь...")
            await self._speak_silero(text)
            return

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            from elevenlabs.client import AsyncElevenLabs
            client = AsyncElevenLabs(api_key=api_key)
            audio_stream = await client.text_to_speech.convert(
                voice_id=voice_id,
                text=text,
                model_id="eleven_multilingual_v2",
            )
            with open(tmp_path, "wb") as f:
                async for chunk in audio_stream:
                    f.write(chunk)

            self._current_process = await asyncio.create_subprocess_exec(
                "mpv",
                "--no-config",
                "--keep-open=no",
                "--terminal=no",
                "--no-video",
                "--really-quiet",
                tmp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
            await self._current_process.wait()
        except Exception as e:
            print(f"[ElevenLabs Error]: {e}, переключаюсь на резервный звук...")
            await self._speak_silero(text)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            self._current_process = None

    async def _speak_gemini(self, text: str):
        """Синтез через Google Gemini 2.0 Audio (Google AI Studio)."""
        api_key = self.config.gemini_api_key or os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            print("[Gemini Audio] GEMINI_API_KEY не найден в .env, переключаюсь...")
            await self._speak_silero(text)
            return

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            voice_name = getattr(self.config, "gemini_voice", "Puck")
            res = await client.aio.models.generate_content(
                model="gemini-2.0-flash",
                contents=f"Say in character as a sharp cyberpunk companion: {text}",
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice_name
                            )
                        )
                    ),
                ),
            )
            written = False
            if res.candidates:
                for cand in res.candidates:
                    if cand.content and cand.content.parts:
                        for part in cand.content.parts:
                            if part.inline_data and part.inline_data.data:
                                with open(tmp_path, "wb") as f:
                                    f.write(part.inline_data.data)
                                written = True
                                break
            if not written or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                print("[Gemini Audio] Нет аудио в ответе, переключаюсь на резервный звук...")
                await self._speak_silero(text)
                return

            self._current_process = await asyncio.create_subprocess_exec(
                "mpv",
                "--no-config",
                "--keep-open=no",
                "--terminal=no",
                "--no-video",
                "--really-quiet",
                tmp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
            await self._current_process.wait()
        except asyncio.CancelledError:
            if self._current_process:
                try: self._current_process.kill()
                except Exception: pass
            raise
        except Exception as e:
            if not self._is_stopped:
                print(f"[Gemini Audio Error]: {e}, переключаюсь на резервный звук...")
                await self._speak_silero(text)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            self._current_process = None

    async def _speak_edge(self, text: str):
        """Облачный синтез через Edge-TTS."""
        if self._is_stopped:
            return
        import edge_tts

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            communicate = edge_tts.Communicate(
                text=text,
                voice=self.config.tts_voice,
                rate=self.config.tts_rate,
                pitch=self.config.tts_pitch,
            )
            await communicate.save(tmp_path)
            if self._is_stopped:
                return

            self._current_process = await asyncio.create_subprocess_exec(
                "mpv",
                "--no-config",
                "--keep-open=no",
                "--terminal=no",
                "--no-video",
                "--really-quiet",
                tmp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
            await self._current_process.wait()
        except asyncio.CancelledError:
            if self._current_process:
                try: self._current_process.kill()
                except Exception: pass
            raise
        except Exception as e:
            if not self._is_stopped:
                print(f"[Edge TTS Error]: {e}")
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            self._current_process = None
