"""
Wave 1 (v2.8.0b2) Regression Tests:
- Incremental SentenceStreamSplitter (fast first sentence, code-fence protection, abbreviations/versions)
- StreamingTTSSession (parallel prefetch, strict FIFO playback, instant interrupt() + temp file cleanup)
- CapriAgent live sentence-streaming TTS starting BEFORE LLM generation finishes
- Silent interception of hallucinated `appraisal` / `think` tool calls (no visible `>_ appraisal` pills)
- Packaged runtime assets inside `capri/assets` via `capri.core.resources`
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from capri.core.agent import CapriAgent
from capri.core.config import AppConfig
from capri.core.resources import get_asset_path, get_assets_dir, resolve_asset_path
from capri.core.sessions import SessionManager
from capri.voice.tts import SentenceStreamSplitter, VoiceSynthesizer


def test_sentence_stream_splitter_incremental_code_fences_and_abbreviations():
    splitter = SentenceStreamSplitter(min_first_chars=10, min_next_chars=24)

    # Short partial buffer — not complete yet
    assert splitter.feed_cumulative("Живая,") == []
    assert splitter.feed_cumulative("Живая, в хорошей форме.") == []  # waiting for whitespace/end

    # First sentence completes as soon as space follows punctuation
    first = splitter.feed_cumulative("Живая, в хорошей форме. Версия v2.8.0b2 и т.д. ")
    assert first == ["Живая, в хорошей форме."]

    # Should NOT split inside `v2.8.0b2` or `т.д.` — waits until real sentence end
    assert splitter.feed_cumulative(
        "Живая, в хорошей форме. Версия v2.8.0b2 и т.д. работает стабильно! "
    ) == ["Версия v2.8.0b2 и т.д. работает стабильно!"]

    # Code fence: must NOT split sentences inside an unclosed ``` block
    base = "Живая, в хорошей форме. Версия v2.8.0b2 и т.д. работает стабильно! "
    inside_code = base + "Вот пример:\n```python\nprint('Hello! World. Done!')\n"
    assert splitter.feed_cumulative(inside_code) == []

    # After closing code fence + trailing sentence, flush emits the rest cleanly
    full = inside_code + "```\nВсё готово к запуску."
    after_close = splitter.feed_cumulative(full)
    flushed = splitter.flush()
    combined = " ".join(after_close + flushed)
    assert "Всё готово к запуску." in combined
    # Re-flushing emits nothing
    assert splitter.flush() == []


@pytest.mark.asyncio
async def test_streaming_tts_session_parallel_prefetch_ordered_playback_and_interrupt(tmp_path):
    cfg = AppConfig()
    cfg.voice.tts_enabled = True
    cfg.voice.tts_engine = "edge"
    tts = VoiceSynthesizer(cfg.voice)

    synth_started: list[str] = []
    played_order: list[str] = []
    play1_started = asyncio.Event()
    release_play1 = asyncio.Event()

    async def fake_synth(clean_text: str):
        synth_started.append(clean_text)
        p = tmp_path / f"chunk_{len(synth_started)}.mp3"
        p.write_bytes(b"ID3fakeaudio")
        return str(p)

    async def fake_play(audio_path: str, phrase: str = ""):
        played_order.append(phrase)
        if len(played_order) == 1:
            play1_started.set()
            await release_play1.wait()
        import os
        if os.path.exists(audio_path):
            os.remove(audio_path)
        return not tts._is_stopped

    tts._synthesize_chunk_file = fake_synth  # type: ignore[method-assign]
    tts._play_audio_file = fake_play  # type: ignore[method-assign]

    sess = tts.start_stream_session()
    assert sess.push("Первая фраза готова.") is True
    assert sess.push("Вторая фраза грузится заранее.") is True
    assert sess.push("Третья фраза в очереди.") is True

    await asyncio.wait_for(play1_started.wait(), timeout=2.0)
    # Give event loop a tick so parallel prefetch tasks run while chunk 1 is still playing
    await asyncio.sleep(0.05)
    assert len(synth_started) == 3  # Parallel prefetch started all 3 while #1 is still playing!

    release_play1.set()
    played_cnt = await sess.finish(timeout=3.0)
    assert played_cnt == 3
    assert played_order == [
        "Первая фраза готова.",
        "Вторая фраза грузится заранее.",
        "Третья фраза в очереди.",
    ]

    # Now test instant interrupt() (barge-in): unplayed prefetched temp files must be cleaned up!
    played_order.clear()
    play1_started.clear()
    release_play1.clear()
    created_files: list[str] = []

    async def fake_synth_track(clean_text: str):
        p = tmp_path / f"barge_{len(created_files)}.mp3"
        p.write_bytes(b"ID3barge")
        created_files.append(str(p))
        return str(p)

    tts._synthesize_chunk_file = fake_synth_track  # type: ignore[method-assign]
    sess2 = tts.start_stream_session()
    sess2.push("Эту фразу сейчас перебьют.")
    sess2.push("А эта не должна прозвучать вообще.")
    await asyncio.wait_for(play1_started.wait(), timeout=2.0)
    await asyncio.sleep(0.05)

    tts.interrupt()
    await sess2.finish(timeout=2.0)
    assert len(played_order) == 1
    assert tts._is_stopped is True
    # Every prefetched unplayed temp file must have been deleted on interrupt
    import os
    for fpath in created_files:
        assert not os.path.exists(fpath)


@pytest.mark.asyncio
async def test_agent_streaming_tts_starts_before_llm_stream_ends(tmp_path):
    cfg = AppConfig()
    cfg.llm.provider = "deepseek"
    cfg.llm.model = "deepseek-flash"
    cfg.llm.api_key = "sk-test-stream-tts"
    cfg.voice.tts_enabled = True
    cfg.voice.tts_engine = "edge"

    sm = SessionManager(db_path=tmp_path / "stream_tts.db")
    agent = CapriAgent(cfg, session_manager=sm)

    pushed_phrases: list[str] = []
    first_phrase_pushed = asyncio.Event()
    allow_second_sentence = asyncio.Event()

    class _FakeStreamSession:
        def push(self, raw_sentence: str) -> bool:
            pushed_phrases.append(raw_sentence)
            first_phrase_pushed.set()
            return True

        async def finish(self, timeout: float = 90.0) -> int:
            return len(pushed_phrases)

        def cancel(self):
            pass

    agent.tts.start_stream_session = MagicMock(return_value=_FakeStreamSession())  # type: ignore[method-assign]

    async def _gated_stream():
        for piece in (
            '<appraisal>{"category":"CHAT","intent":"тест","confidence":0.9,"delta_trust":0,"delta_irritation":0,"mood":"neutral","strategy":"ответить"}</appraisal>\n',
            "Первая фраза готова сразу! ",
        ):
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=piece, reasoning_content=None, tool_calls=None))]
            )
        # Wait until TTS has ALREADY received sentence #1 before streaming sentence #2!
        await asyncio.wait_for(first_phrase_pushed.wait(), timeout=2.0)
        allow_second_sentence.set()
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="А вторая приходит позже.", reasoning_content=None, tool_calls=None))]
        )

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_gated_stream())
    agent._get_client = MagicMock(return_value=mock_client)

    events = []
    async for ev in agent.chat("Проверь потоковый голос", speak=True, stream=True):
        events.append(ev)

    assert allow_second_sentence.is_set()
    assert any(ev.type == "speaking" for ev in events)
    assert len(pushed_phrases) == 2
    assert pushed_phrases[0] == "Первая фраза готова сразу!"
    assert pushed_phrases[1] == "А вторая приходит позже."


@pytest.mark.asyncio
async def test_agent_silently_intercepts_appraisal_tool_call_without_visible_tool_pill(tmp_path):
    cfg = AppConfig()
    cfg.llm.provider = "deepseek"
    cfg.llm.model = "deepseek-flash"
    cfg.llm.api_key = "sk-test-appraisal-intercept"
    cfg.voice.tts_enabled = False

    sm = SessionManager(db_path=tmp_path / "appraisal_intercept.db")
    agent = CapriAgent(cfg, session_manager=sm)
    trust_before = agent.emotions.trust

    # Iteration 1: model mistakenly emits a tool_call named "appraisal"
    appraisal_payload = json.dumps(
        {
            "category": "WARM",
            "intent": "похвалил интерфейс",
            "confidence": 0.95,
            "delta_trust": 2,
            "delta_irritation": 0,
            "mood": "warm",
            "strategy": "ответить спокойно",
        },
        ensure_ascii=False,
    )
    tc = SimpleNamespace(
        id="call_appraisal_1",
        function=SimpleNamespace(name="appraisal", arguments=appraisal_payload),
    )
    resp1 = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, reasoning_content=None, tool_calls=[tc]))]
    )
    # Iteration 2: clean natural response without repeating <appraisal>
    resp2 = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="Живая, в хорошей форме.",
                    reasoning_content=None,
                    tool_calls=None,
                )
            )
        ]
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=[resp1, resp2])
    agent._get_client = MagicMock(return_value=mock_client)

    events = []
    async for ev in agent.chat("Как дела, Капри?", speak=False, stream=True):
        events.append(ev)

    # Zero visible tool_start / tool_result pills!
    assert not any(ev.type in ("tool_start", "tool_result") for ev in events)
    # Appraisal thought WAS still captured and applied!
    thought_events = [ev for ev in events if ev.type == "thought"]
    assert any("WARM" in ev.data and "95%" in ev.data for ev in thought_events)
    assert agent.emotions.trust >= trust_before
    # Final answer is clean
    done_ev = next(ev for ev in events if ev.type == "done")
    assert done_ev.data == "Живая, в хорошей форме."
    # Persisted turn in SQLite has zero fake tool pills
    turns = sm.get_turns(agent.current_session_id)
    assert len(turns) == 1
    assert turns[0].tools == []


def test_packaged_assets_resolver_and_all_runtime_assets_present():
    assets_dir = get_assets_dir()
    assert assets_dir.exists()
    assert assets_dir.name == "assets"
    assert assets_dir.parent.name == "capri"

    required_rel_paths = [
        ("ui_click.wav",),
        ("ui_glitch.wav",),
        ("wake_chirp.wav",),
        ("avatar.png",),
        ("chip.png",),
        ("capri_face_dialog.png",),
        ("capri_voice_clean.wav",),
        ("capri-cyberdeck.desktop",),
        ("sprites_v2", "idle.png"),
        ("sprites_v2", "mouth.png"),
        ("sprites", "idle.png"),
        ("3d", "qml", "Avatar3D.qml"),
    ]
    for parts in required_rel_paths:
        p = get_asset_path(*parts)
        assert p.exists(), f"Missing packaged runtime asset: {p}"

    # Legacy relative "assets/..." config strings still resolve cleanly
    resolved = resolve_asset_path("assets/capri_voice_clean.wav")
    assert resolved.exists()
    assert resolved.parent == assets_dir
