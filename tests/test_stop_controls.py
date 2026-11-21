import pytest
import threading
import numpy as np
from unittest.mock import MagicMock
from capri.core.config import AppConfig
from capri.core.agent import CapriAgent
from capri.voice.tts import VoiceSynthesizer


def test_agent_abort_flag():
    cfg = AppConfig()
    agent = CapriAgent(cfg)
    assert not agent._abort_requested
    agent.abort()
    assert agent._abort_requested is True


def test_voice_synthesizer_stop_sync():
    cfg = AppConfig()
    synth = VoiceSynthesizer(cfg.voice)
    # Mock current process
    mock_proc = MagicMock()
    mock_proc.returncode = None
    synth._current_process = mock_proc

    synth.stop_sync()
    assert mock_proc.terminate.called or mock_proc.kill.called
    assert synth._is_stopped is True
    assert synth._current_process is None


def test_speech_recognizer_stop_event():
    from capri.voice.stt import SpeechRecognizer
    cfg = AppConfig()
    rec = SpeechRecognizer(cfg.voice)

    stop_ev = threading.Event()
    stop_ev.set()  # set immediately to stop loop on first iteration

    # Should exit cleanly without waiting
    audio = rec.record_audio(max_duration=6.0, silence_timeout=1.3, stop_event=stop_ev)
    assert isinstance(audio, np.ndarray)
    assert len(audio) == 0


def test_agent_client_loop_switch():
    import asyncio
    cfg = AppConfig()
    agent = CapriAgent(cfg)

    async def get_c():
        return agent._get_client()

    # First loop running
    loop1 = asyncio.new_event_loop()
    client1 = loop1.run_until_complete(get_c())
    assert client1 is not None
    assert agent._client_loop is loop1
    loop1.close()

    # Second loop running after first was closed
    loop2 = asyncio.new_event_loop()
    client2 = loop2.run_until_complete(get_c())
    assert client2 is not client1
    assert agent._client_loop is loop2
    loop2.close()



