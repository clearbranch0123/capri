"""
Closed Beta Stage 3 Feature Regression Tests:
- True token streaming (`stream=True`) in CapriAgent with zero flash of unclosed <appraisal>/<think>
  or bracket appraisal headers, streamed tool_calls accumulation, and mid-stream abort support
- Debounced streaming UI updates in CapriCyberdeckWindow (_on_stream_chunk -> _flush_stream_chunk)
- Drag & Drop files and Ctrl+V QImage screenshot paste in ComposerTextEdit & AttachmentStripWidget
- Async attachment prompt enrichment (text files + Vision analysis for images)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from capri.core.agent import CapriAgent, extract_streamable_visible_text
from capri.core.attachments import (
    build_prompt_with_attachments,
    classify_attachment,
    format_user_display_with_attachments,
    save_clipboard_image_bytes,
)
from capri.core.config import AppConfig, LLMConfig
from capri.ui.cyberdeck import CapriCyberdeckWindow, ChatMessageCard


def test_extract_streamable_visible_text_hides_incomplete_internal_tags():
    # Unclosed <appraisal> or <think> must NEVER flash during streaming
    assert extract_streamable_visible_text("<ap") is None
    assert extract_streamable_visible_text("<appraisal>{\"category\": \"CHAT\"") is None
    assert extract_streamable_visible_text("<think>размышляю") is None
    assert extract_streamable_visible_text("[ROAST | conf: 90%] подкол → парировать") is None

    # Once closed and visible answer begins, clean text streams through
    buf = "<appraisal>{\"category\": \"WARM\", \"confidence\": 0.9}</appraisal>\nПривет, "
    assert extract_streamable_visible_text(buf) == "Привет,"

    buf2 = "[CHAT] поздоровался\nПривет, D! Всё работает."
    assert extract_streamable_visible_text(buf2) == "Привет, D! Всё работает."


@pytest.mark.asyncio
async def test_agent_true_streaming_and_appraisal_shielding():
    cfg = AppConfig(
        llm=LLMConfig(provider="deepseek", api_key="sk-stream-test", model="deepseek-flash")
    )
    agent = CapriAgent(cfg)

    streamed_chunks = [
        "<appraisal>",
        '{"category": "CHAT", "confidence": 0.95}',
        "</appraisal>\n",
        "Привет, ",
        "D! ",
        "Стриминг работает.",
    ]

    async def fake_stream():
        for piece in streamed_chunks:
            delta = SimpleNamespace(content=piece, reasoning_content=None, tool_calls=None)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())

    deltas: list[str] = []
    done_events: list[str] = []
    with patch.object(agent, "_get_client", return_value=mock_client):
        async for ev in agent.chat("Тест стрима", speak=False, stream=True):
            if ev.type == "delta":
                deltas.append(ev.data)
            elif ev.type == "done":
                done_events.append(ev.data)

    # Confirm stream=True was requested
    assert mock_client.chat.completions.create.call_args.kwargs.get("stream") is True
    # Multiple progressive deltas emitted, and NONE of them ever contained <appraisal>
    assert len(deltas) >= 3
    assert all("<appraisal>" not in d and "category" not in d for d in deltas)
    assert deltas[-1] == "Привет, D! Стриминг работает."
    assert done_events == ["Привет, D! Стриминг работает."]


@pytest.mark.asyncio
async def test_agent_streamed_tool_calls_accumulation():
    cfg = AppConfig(
        llm=LLMConfig(provider="deepseek", api_key="sk-stream-tc", model="deepseek-flash")
    )
    agent = CapriAgent(cfg)

    async def stream_turn_1():
        # Tool call streamed across 3 chunks
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None, reasoning_content=None,
            tool_calls=[SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(name="run_b", arguments='{"comm'))],
        ))])
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None, reasoning_content=None,
            tool_calls=[SimpleNamespace(index=0, id=None, function=SimpleNamespace(name="ash", arguments='and": "echo '))],
        ))])
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None, reasoning_content=None,
            tool_calls=[SimpleNamespace(index=0, id=None, function=SimpleNamespace(name="", arguments='stream-ok"}'))],
        ))])

    async def stream_turn_2():
        for piece in ("Команда ", "выполнена: ", "stream-ok"):
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content=piece, reasoning_content=None, tool_calls=None,
            ))])

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=[stream_turn_1(), stream_turn_2()])

    tool_results = []
    done_texts = []
    with patch.object(agent, "_get_client", return_value=mock_client):
        async for ev in agent.chat("Проверь echo", speak=False, stream=True):
            if ev.type == "tool_result":
                tool_results.append(ev.data)
            elif ev.type == "done":
                done_texts.append(ev.data)

    assert tool_results == ["stream-ok"]
    assert done_texts == ["Команда выполнена: stream-ok"]


@pytest.mark.asyncio
async def test_attachments_classification_and_enrichment(tmp_path):
    from PIL import Image
    from capri.core.attachments import validate_image_limits
    from capri.tools.screen import analyze_image_file

    code_file = tmp_path / "sample.py"
    code_file.write_text("def hello():\n    return 42\n", encoding="utf-8")

    # Create a real valid small PNG via Pillow
    img_file = tmp_path / "screen.png"
    Image.new("RGB", (64, 48), color=(105, 213, 232)).save(img_file, format="PNG")

    att_code = classify_attachment(code_file)
    att_img = classify_attachment(img_file)
    assert att_code is not None and att_code.kind == "text"
    assert att_img is not None and att_img.kind == "image"

    # Oversized byte limit rejected
    ok_sz, msg_sz = validate_image_limits(img_file, max_bytes=10)
    assert ok_sz is False and "лимит размера" in msg_sz

    # Excessive pixel count (decompression bomb guard) rejected
    ok_px, msg_px = validate_image_limits(img_file, max_pixels=100)
    assert ok_px is False and "лимит пикселей" in msg_px

    # Corrupt PNG rejected by classify_attachment and analyze_image_file
    corrupt_img = tmp_path / "corrupt.png"
    corrupt_img.write_bytes(b"not-a-real-png-header")
    assert classify_attachment(corrupt_img) is None
    res_corrupt = await analyze_image_file(corrupt_img)
    assert "отклонено лимитами безопасности" in res_corrupt

    disp = format_user_display_with_attachments("Глянь ошибку", [att_img, att_code])
    assert "[IMG: screen.png]" in disp
    assert "[FILE: sample.py]" in disp

    with patch("capri.tools.screen.analyze_image_file", new=AsyncMock(return_value="[Vision]: На скрине Traceback")):
        enriched = await build_prompt_with_attachments("Разбери баг", [att_img, att_code], config=AppConfig())
        assert "Разбери баг" in enriched
        assert "[Прикреплённое изображение: screen.png]" in enriched
        assert "На скрине Traceback" in enriched
        assert "[Прикреплённый файл: sample.py]" in enriched
        assert "def hello():" in enriched


def test_cyberdeck_streaming_flush_and_clipboard_drag_drop(tmp_path):
    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    sample_file = tmp_path / "notes.md"
    sample_file.write_text("# Заметка\nТест drag and drop", encoding="utf-8")

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.show()
        QApplication.processEvents()

        # 1. Attachment strip hidden by default
        assert deck.attachment_strip.isHidden() is True

        # 2. Paste QImage screenshot into ComposerTextEdit (Ctrl+V path)
        qimg = QImage(64, 48, QImage.Format.Format_RGBA8888)
        qimg.fill(QColor("#69D5E8"))
        mime_img = QMimeData()
        mime_img.setImageData(qimg)
        assert deck.input_field.canInsertFromMimeData(mime_img) is True
        deck.input_field.insertFromMimeData(mime_img)
        QApplication.processEvents()

        assert deck.attachment_strip.isHidden() is False
        assert len(deck.attachment_strip.attachments()) == 1
        assert deck.attachment_strip.attachments()[0].kind == "image"

        # 3. Drop/paste file URL into ComposerTextEdit
        mime_url = QMimeData()
        mime_url.setUrls([QUrl.fromLocalFile(str(sample_file))])
        deck.input_field.insertFromMimeData(mime_url)
        QApplication.processEvents()

        assert len(deck.attachment_strip.attachments()) == 2
        assert deck.attachment_strip.attachments()[1].display_name == "notes.md"

        # 4. Removing temp pasted attachment deletes temp PNG on disk; removing user file preserves it!
        temp_img_path = deck.attachment_strip.attachments()[0].path
        assert temp_img_path.exists() is True
        deck.attachment_strip.remove_attachment(str(temp_img_path))
        assert temp_img_path.exists() is False
        assert len(deck.attachment_strip.attachments()) == 1

        # Removing normal user file must NEVER delete user's file on disk
        deck.attachment_strip.remove_attachment(str(sample_file))
        assert sample_file.exists() is True
        assert deck.attachment_strip.isHidden() is True

        # 5. Exceeding MAX_ATTACHMENTS cleans up rejected temp clipboard PNG immediately
        from capri.core.attachments import MAX_ATTACHMENTS
        for idx in range(MAX_ATTACHMENTS):
            f_i = tmp_path / f"f_{idx}.txt"
            f_i.write_text(f"file {idx}", encoding="utf-8")
            deck._attach_file_paths([str(f_i)])
        assert len(deck.attachment_strip.attachments()) == MAX_ATTACHMENTS

        created_temps: list[Path] = []
        orig_save = save_clipboard_image_bytes

        def spy_save(raw_bytes, cache_dir=None):
            item = orig_save(raw_bytes, cache_dir=cache_dir)
            created_temps.append(item.path)
            return item

        with patch("capri.core.attachments.save_clipboard_image_bytes", side_effect=spy_save):
            deck._attach_pasted_image(qimg)
        assert len(created_temps) == 1
        assert created_temps[0].exists() is False

        # 6. Closing window cleans up remaining unsent temp screenshots, while keeping user files intact
        deck.attachment_strip.clear(cleanup_temp=True)
        deck._attach_file_paths([str(sample_file)])
        deck._attach_pasted_image(qimg)
        unsent_temp_path = [a.path for a in deck.attachment_strip.attachments() if a.is_temp][0]
        assert unsent_temp_path.exists() is True

        # 7. Debounced streaming chunk flush updates live response card cleanly
        card = ChatMessageCard("Капри", "Синхронизация нейросети...", parent=deck.chat_container)
        deck.current_response_card = card
        deck.chat_layout.insertWidget(deck.chat_layout.count() - 1, card)
        deck._on_stream_chunk("Живой токенный стриминг...")
        deck._flush_stream_chunk()
        assert card.lbl_text.text() == "Живой токенный стриминг..."

        deck.close()
        assert unsent_temp_path.exists() is False
        assert sample_file.exists() is True


def test_speech_finished_preserves_response_quoting_placeholder():
    """
    Regression: when Capri describes her own screen (look_at_screen) and quotes
    'Синхронизация нейросети...' inside her real answer, _on_speech_finished()
    must NEVER overwrite the answer with '[Команда выполнена]'.
    """
    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.voice.tts_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        try:
            # Case A: Real response containing the placeholder phrase
            card_a = ChatMessageCard("Капри", "Синхронизация нейросети...", parent=deck.chat_container)
            deck.current_response_card = card_a
            deck._response_has_content = False
            deck.current_tools_log = ["look_at_screen"]
            deck.chat_layout.insertWidget(deck.chat_layout.count() - 1, card_a)

            real_reply = (
                'Вижу рабочий стол и окно со мной: в чате видна надпись '
                '"Синхронизация нейросети...". Всё работает штатно.'
            )
            deck._on_text_ready(real_reply)
            deck._on_speech_finished()
            assert card_a.lbl_text.text() == real_reply
            assert "[Команда выполнена]" not in card_a.lbl_text.text()

            # Case B: True tool-only turn with no model text still collapses placeholder
            card_b = ChatMessageCard("Капри", "Синхронизация нейросети...", parent=deck.chat_container)
            deck.current_response_card = card_b
            deck._response_has_content = False
            deck.current_tools_log = ["look_at_screen"]
            deck.chat_layout.insertWidget(deck.chat_layout.count() - 1, card_b)

            deck._on_speech_finished()
            assert card_b.lbl_text.text() == "[Команда выполнена]"
        finally:
            deck.close()
