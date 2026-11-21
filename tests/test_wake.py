import pytest
import re
from capri.voice.wake import WakeWordListener, ensure_vosk_model, DEFAULT_MODEL_DIR


def test_wake_model_installed(tmp_path):
    # Tests that ensure_vosk_model detects an existing local model without downloading
    fake_model = tmp_path / "vosk-fake-model"
    (fake_model / "am").mkdir(parents=True)
    assert ensure_vosk_model(fake_model) is True


def test_wake_pattern_word_boundaries():
    wake_words = ["капри", "эй капри", "привет капри"]
    escaped = [re.escape(w.lower()) for w in sorted(wake_words, key=len, reverse=True)]
    pattern = re.compile(r"\b(" + "|".join(escaped) + r")\b", re.IGNORECASE)

    # Should match full words
    assert pattern.search("капри") is not None
    assert pattern.search("эй капри") is not None
    assert pattern.search("привет капри, как дела?") is not None
    assert pattern.search("давай, капри, открой терминал") is not None

    # Should NOT match partial syllables
    assert pattern.search("капр") is None
    assert pattern.search("каприз") is None
    assert pattern.search("капризная") is None


def test_wake_listener_pause_resume():
    listener = WakeWordListener(wake_words=["капри"])
    assert not listener.running
    assert listener._paused is False

    # Pause sets _paused
    listener.pause()
    assert listener._paused is True

    # Resume resets _paused
    listener.resume()
    assert listener._paused is False


def test_wake_listener_cancel_recording():
    listener = WakeWordListener(wake_words=["капри"])
    assert not listener._cancel_wake_event.is_set()
    listener.cancel_active_recording()
    assert listener._cancel_wake_event.is_set()


def test_wake_energy_thresholds():
    listener = WakeWordListener(wake_words=["капри"])
    assert listener.min_speech_rms >= 0.005
    assert listener.partial_min_rms >= listener.min_speech_rms


def test_handle_wake_event_silence_no_phantom_text():
    from unittest.mock import MagicMock
    import numpy as np

    callback_called = []
    listener = WakeWordListener(
        wake_words=["капри"],
        on_text_ready=lambda t: callback_called.append(t),
    )

    mock_stream = MagicMock()
    # Возвращаем тишину (нули) при чтении потока
    silent_block = np.zeros(2000, dtype=np.int16).tobytes()
    mock_stream.read.return_value = (silent_block, None)

    # Имитируем initial_frames с нулевой энергией
    initial_frames = [np.zeros(2000, dtype=np.float32)]
    listener._handle_wake_event(mock_stream, block_size=2000, initial_frames=initial_frames)

    # При тишине фантомный вызов "Капри" НЕ должен быть отправлен
    assert callback_called == []

