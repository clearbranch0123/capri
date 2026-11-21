import pytest
from capri.tools.media import media_control
from capri.tools.desktop import get_volume


@pytest.mark.asyncio
async def test_media_control_invalid():
    res = await media_control("invalid_action_xyz")
    assert "Неизвестное действие" in res or "playerctl" in res


@pytest.mark.asyncio
async def test_media_control_status():
    res = await media_control("status")
    assert isinstance(res, str)
    assert len(res) > 0


@pytest.mark.asyncio
async def test_get_volume():
    res = await get_volume()
    assert isinstance(res, str)
    assert any(term in res.lower() for term in ("volume", "звук", "не удалось", "ошибка", "wpctl"))


def test_stt_record_methods(monkeypatch):
    import numpy as np
    from capri.voice.stt import SpeechRecognizer
    from capri.core.config import VoiceConfig

    cfg = VoiceConfig()
    stt = SpeechRecognizer(cfg)
    assert hasattr(stt, "record_audio")
    assert hasattr(stt, "record_seconds")

    class MockStream:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, n):
            return np.zeros((n, 1), dtype=np.float32), False

    from types import SimpleNamespace
    from capri.voice import stt as stt_mod

    monkeypatch.setattr(stt_mod, "sd", SimpleNamespace(InputStream=MockStream))
    audio = stt.record_seconds(0.1)
    assert isinstance(audio, np.ndarray)


@pytest.mark.asyncio
async def test_agent_tools_omitted_on_second_turn():
    from unittest.mock import AsyncMock, MagicMock, patch
    from capri.core.agent import CapriAgent
    from capri.core.config import AppConfig

    cfg = AppConfig()
    cfg.llm.api_key = "dummy-test-key"
    agent = CapriAgent(cfg)

    mock_tc = MagicMock()
    mock_tc.id = "call_xyz"
    mock_tc.function.name = "media_control"
    mock_tc.function.arguments = '{"action": "status"}'

    resp_turn1 = MagicMock()
    resp_turn1.choices = [MagicMock(message=MagicMock(content=None, tool_calls=[mock_tc]))]

    resp_turn2 = MagicMock()
    resp_turn2.choices = [MagicMock(message=MagicMock(content="Играет музыка", tool_calls=None))]

    mock_create = AsyncMock(side_effect=[resp_turn1, resp_turn2])

    with patch.object(agent, "_get_client") as mock_get_client, \
         patch("capri.tools.registry.registry.execute", new_callable=AsyncMock) as mock_exec:
        mock_client = MagicMock()
        mock_client.chat.completions.create = mock_create
        mock_get_client.return_value = mock_client
        mock_exec.return_value = "Playing: Yakui The Maid"

        events = []
        async for ev in agent.chat("что играет?"):
            events.append(ev)

        assert mock_create.call_count == 2
        call_1_kwargs = mock_create.call_args_list[0].kwargs
        call_2_kwargs = mock_create.call_args_list[1].kwargs

        # Turn 1 MUST pass tools schema
        assert "tools" in call_1_kwargs
        assert len(call_1_kwargs["tools"]) > 0

        # Turn 2 MUST NOT pass tools schema (saving ~2300-3000 tokens and avoiding Groq TPM limits)
        assert "tools" not in call_2_kwargs


@pytest.mark.asyncio
async def test_agent_single_key_error_handling_and_rollback():
    from unittest.mock import AsyncMock, MagicMock, patch
    from capri.core.agent import CapriAgent
    from capri.core.config import AppConfig

    cfg = AppConfig()
    cfg.llm.api_key = "primary-key"
    agent = CapriAgent(cfg)

    # Primary client throws RateLimitError (e.g. 429)
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=Exception("Error 429: Rate limit reached"))

    with patch.object(agent, "_get_client", return_value=mock_client):
        events = []
        async for ev in agent.chat("Привет"):
            events.append(ev)

        assert mock_client.chat.completions.create.call_count == 1
        error_events = [ev for ev in events if ev.type == "error"]
        assert len(error_events) == 1
        assert "429" in error_events[0].data
        # User message was rolled back to prevent corrupting history
        assert agent.history[-1].get("role") != "user"


