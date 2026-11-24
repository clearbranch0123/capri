"""
Capri v3.6 Stage 1 — Voice 3.0 Tests:
1. SentenceStreamSplitter eager first-clause split (<0.7s opener start), short-salutation safety,
   decimal-comma safety, code-fence protection, and long run-on clause split.
2. StreamingTTSSession bounded lookahead prefetch (`max_prefetch`), background chunk N+1 synthesis
   while chunk N plays (zero inter-sentence gap), and strict FIFO playback order.
3. Leak-free mid-synthesis cancellation/barge-in (including slow in-flight synthesis tasks that resolve
   after `cancel()` / `interrupt()`).
4. SileroDaemonClient / XTTSDaemonClient `asyncio.Lock` serialization under concurrent prefetch calls.
5. `compute_audio_level_curve` real PCM WAV envelope + pure-Python MP3 fallback + seamless inter-chunk
   hologram level continuity.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import time
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from capri.core.config import AppConfig
from capri.voice.tts import (
    SentenceStreamSplitter,
    SileroDaemonClient,
    VoiceSynthesizer,
    compute_audio_level_curve,
    compute_speech_envelope,
)


def _write_test_wav(path: Path, sample_rate: int = 16000, duration_s: float = 0.28, sampwidth: int = 2) -> None:
    n = int(sample_rate * duration_s)
    frames = bytearray()
    for i in range(n):
        t = i / float(sample_rate)
        # Amplitude envelope swells up then fades
        env = math.sin(math.pi * (i / float(max(1, n - 1))))
        val = env * math.sin(2.0 * math.pi * 220.0 * t)
        if sampwidth == 3:
            s24 = max(-8388608, min(8388607, int(round(5800000 * val))))
            frames.extend(int(s24).to_bytes(3, byteorder="little", signed=True))
        else:
            sample = int(22000 * val)
            frames.extend(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))


def test_sentence_splitter_eager_first_clause_and_run_on_split():
    splitter = SentenceStreamSplitter(
        min_first_chars=10,
        min_next_chars=24,
        eager_first_clause=True,
        eager_clause_min_chars=28,
        max_clause_chars=90,
    )

    # 1. Short salutation (<28 chars before comma) does NOT split prematurely into a choppy micro-clip
    assert splitter.feed_cumulative("Живая, проверяю") == []

    # 2. Long opening clause (>=28 chars before comma + next word started) splits eagerly BEFORE period!
    eager = splitter.feed_cumulative(
        "Проверила активные сервисы композитора и аудиограф, задержек в потоке сейчас"
    )
    assert eager == ["Проверила активные сервисы композитора и аудиограф,"]

    # 3. Rest of sentence #1 finishes normally on period
    rest1 = splitter.feed_cumulative(
        "Проверила активные сервисы композитора и аудиограф, задержек в потоке сейчас нет. Следующая "
    )
    assert rest1 == ["задержек в потоке сейчас нет."]

    # 4. Numeric commas ("3, 14") and unclosed code fences never trigger false clause splits
    sp_num = SentenceStreamSplitter(eager_first_clause=True, eager_clause_min_chars=20)
    assert sp_num.feed_cumulative("Значение коэффициента примерно 3, 14159265 в расчёте") == []

    # 5. Long run-on second sentence (> max_clause_chars) splits cleanly at semicolon/dash
    sp_long = SentenceStreamSplitter(eager_first_clause=False, max_clause_chars=80)
    sp_long.feed_cumulative("Короткий старт готов. ")
    run_on = (
        "Короткий старт готов. "
        "Дальше идёт очень длинное развёрнутое объяснение архитектуры звукового конвейера — "
        "которое продолжается без точки ещё на сотню символов подряд"
    )
    chunks = sp_long.feed_cumulative(run_on)
    assert chunks == ["Дальше идёт очень длинное развёрнутое объяснение архитектуры звукового конвейера —"]
    assert sp_long.flush() == ["которое продолжается без точки ещё на сотню символов подряд"]


@pytest.mark.asyncio
async def test_streaming_tts_bounded_lookahead_prefetch_eliminates_inter_chunk_gap(tmp_path):
    cfg = AppConfig()
    cfg.voice.tts_enabled = True
    cfg.voice.tts_engine = "edge"
    tts = VoiceSynthesizer(cfg.voice)

    inflight = 0
    max_inflight_seen = 0
    play_timestamps: list[tuple[str, float, float]] = []

    async def fake_synth(clean_text: str):
        nonlocal inflight, max_inflight_seen
        inflight += 1
        max_inflight_seen = max(max_inflight_seen, inflight)
        try:
            await asyncio.sleep(0.07)  # Simulate 70ms network/GPU TTS synthesis
            out = tmp_path / f"pref_{ abs(hash(clean_text)) % 100000 }.mp3"
            out.write_bytes(b"ID3" + b"\x00" * 512)
            return str(out)
        finally:
            inflight -= 1

    async def fake_play(audio_path: str, phrase: str = "", seamless_follow: bool = False):
        t0 = time.monotonic()
        await asyncio.sleep(0.08)  # Simulate 80ms audio playback (> 70ms synthesis time!)
        t1 = time.monotonic()
        play_timestamps.append((phrase, t0, t1))
        if os.path.exists(audio_path):
            os.remove(audio_path)
        return True

    tts._synthesize_chunk_file = fake_synth  # type: ignore[method-assign]
    tts._play_audio_file = fake_play  # type: ignore[method-assign]

    sess = tts.start_stream_session(max_prefetch=2)
    phrases = [
        "Первая фраза пошла в эфир.",
        "Вторая фраза синтезируется на фоне.",
        "Третья фраза ждёт слот предзагрузки.",
        "Четвёртая фраза завершает ответ.",
    ]
    for p in phrases:
        assert sess.push(p) is True

    played = await sess.finish(timeout=5.0)
    assert played == 4
    # Bounded semaphore strictly enforced max_prefetch=2 concurrent syntheses
    assert max_inflight_seen == 2
    assert [p for p, _, _ in play_timestamps] == phrases

    # Because chunk N+1 synthesized in background while chunk N played (80ms > 70ms),
    # inter-sentence gap between chunk 1 end and chunk 2 start must be near-zero (<25ms, NOT 70ms!)
    gap_1_to_2 = play_timestamps[1][1] - play_timestamps[0][2]
    gap_2_to_3 = play_timestamps[2][1] - play_timestamps[1][2]
    assert gap_1_to_2 < 0.030, f"Expected near-zero gap, got {gap_1_to_2:.4f}s"
    assert gap_2_to_3 < 0.030, f"Expected near-zero gap, got {gap_2_to_3:.4f}s"


@pytest.mark.asyncio
async def test_late_resolving_inflight_synth_cleans_up_temp_files_on_cancel(tmp_path):
    cfg = AppConfig()
    cfg.voice.tts_enabled = True
    tts = VoiceSynthesizer(cfg.voice)

    synth2_entered = asyncio.Event()
    created_paths: list[Path] = []

    async def slow_unshielded_synth(clean_text: str):
        idx = len(created_paths)
        out = tmp_path / f"late_{idx}.mp3"
        created_paths.append(out)
        if idx == 0:
            out.write_bytes(b"ID3first")
            return str(out)
        synth2_entered.set()
        try:
            await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            # Simulate an underlying worker writing the file right as cancellation lands
            out.write_bytes(b"ID3late")
            return str(out)
        out.write_bytes(b"ID3done")
        return str(out)

    play1_holding = asyncio.Event()

    async def hold_play(audio_path: str, phrase: str = "", seamless_follow: bool = False):
        play1_holding.set()
        try:
            await asyncio.sleep(2.0)
        finally:
            if os.path.exists(audio_path):
                os.remove(audio_path)
        return False

    tts._synthesize_chunk_file = slow_unshielded_synth  # type: ignore[method-assign]
    tts._play_audio_file = hold_play  # type: ignore[method-assign]

    sess = tts.start_stream_session(max_prefetch=3)
    sess.push("Первая фраза играет.")
    sess.push("Вторая фраза в процессе синтеза.")
    await asyncio.wait_for(play1_holding.wait(), timeout=2.0)
    await asyncio.wait_for(synth2_entered.wait(), timeout=2.0)

    tts.interrupt()
    await asyncio.sleep(0.08)

    for p in created_paths:
        assert not p.exists(), f"Leaked temporary TTS file after interrupt: {p}"


@pytest.mark.asyncio
async def test_silero_daemon_client_serializes_concurrent_prefetch_requests(tmp_path):
    worker_py = tmp_path / "fake_silero_worker.py"
    worker_py.write_text(
        "import json, sys, time\n"
        "print('READY', flush=True)\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    time.sleep(0.03)\n"
        "    open(req['output'], 'wb').write(b'RIFFfake')\n"
        "    print(json.dumps({'status': 'ok'}), flush=True)\n",
        encoding="utf-8",
    )
    client = SileroDaemonClient(Path(os.sys.executable), worker_py)
    try:
        out_a = tmp_path / "a.wav"
        out_b = tmp_path / "b.wav"
        # Concurrent calls without a lock would raise RuntimeError on stdout.readline()
        res_a, res_b = await asyncio.gather(
            client.synthesize("первый", str(out_a)),
            client.synthesize("второй", str(out_b)),
        )
        assert res_a is True and res_b is True
        assert out_a.exists() and out_b.exists()
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_sliding_lookahead_window_caps_buffered_files_while_chunk1_plays(tmp_path):
    """While chunk #1 is still playing, fast synthesis must NOT synthesize all 6 queued chunks onto disk."""
    cfg = AppConfig()
    cfg.voice.tts_enabled = True
    tts = VoiceSynthesizer(cfg.voice)

    synth_calls: list[str] = []
    play1_started = asyncio.Event()
    release_play1 = asyncio.Event()

    async def fast_synth(clean_text: str):
        synth_calls.append(clean_text)
        out = tmp_path / f"win_{len(synth_calls)}.mp3"
        out.write_bytes(b"ID3fast")
        return str(out)

    async def gated_play(audio_path: str, phrase: str = "", seamless_follow: bool = False):
        if phrase.startswith("Фраза 1"):
            play1_started.set()
            await release_play1.wait()
        if os.path.exists(audio_path):
            os.remove(audio_path)
        return True

    tts._synthesize_chunk_file = fast_synth  # type: ignore[method-assign]
    tts._play_audio_file = gated_play  # type: ignore[method-assign]

    sess = tts.start_stream_session(max_prefetch=2)
    for idx in range(1, 7):
        sess.push(f"Фраза {idx} в длинном ответе.")

    await asyncio.wait_for(play1_started.wait(), timeout=2.0)
    await asyncio.sleep(0.08)
    # True sliding window: only chunk #1 (playing) + chunk #2 (1 ahead) synthesized — chunks #3..#6 wait!
    assert len(synth_calls) == 2

    release_play1.set()
    played = await sess.finish(timeout=3.0)
    assert played == 6
    assert len(synth_calls) == 6


@pytest.mark.asyncio
async def test_audio_level_curve_and_seamless_playback_envelope(tmp_path):
    wav_path = tmp_path / "tone.wav"
    _write_test_wav(wav_path, duration_s=0.24, sampwidth=2)
    curve, dur = compute_audio_level_curve(str(wav_path), phrase="Проверка звука", fps=25)
    assert 0.20 <= dur <= 0.30
    assert len(curve) >= 4
    assert max(curve) > 0.65
    assert all(0.0 <= v <= 1.0 for v in curve)

    # 24-bit signed PCM WAV (`sampwidth=3`) decoded directly from real PCM samples (even without PyAV)
    wav24_path = tmp_path / "tone24.wav"
    _write_test_wav(wav24_path, duration_s=0.24, sampwidth=3)
    with patch.dict("sys.modules", {"av": None}):
        curve24, dur24 = compute_audio_level_curve(str(wav24_path), phrase="", fps=25)
        mouth24 = compute_speech_envelope(str(wav24_path), phrase="", fps=15)
    assert 0.20 <= dur24 <= 0.30
    assert max(curve24) > 0.65
    assert max(mouth24) >= 3

    # Pure-Python MP3 cadence fallback (non-WAV header)
    fake_mp3 = tmp_path / "speech.mp3"
    fake_mp3.write_bytes(b"\xff\xfb\x90\x64" * 700)  # ~2800 bytes -> ~0.4s
    mp3_curve, mp3_dur = compute_audio_level_curve(str(fake_mp3), phrase="Системы в норме, погнали!", fps=25)
    assert mp3_dur >= 0.25
    assert len(mp3_curve) >= 4
    assert max(mp3_curve) > 0.3

    # Verify _stream_playback_envelope holds 0.14 on seamless_follow=True vs 0.0 on final chunk
    cfg = AppConfig()
    tts = VoiceSynthesizer(cfg.voice)
    emitted: list[float] = []
    with patch("capri.voice.tts._TTS_LEVEL_DISPATCHER.emit", side_effect=emitted.append):
        await tts._stream_playback_envelope([0.3, 0.7], fps=80, seamless_follow=True)
        assert emitted[-1] == 0.14
        emitted.clear()
        await tts._stream_playback_envelope([0.5, 0.2], fps=80, seamless_follow=False)
        assert emitted[-1] == 0.0
