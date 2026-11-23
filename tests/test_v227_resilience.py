import asyncio
import time
from unittest.mock import AsyncMock, patch, MagicMock
import pytest

from capri.core.config import AppConfig, LLMConfig
from capri.core.agent import CapriAgent
from capri.tools.desktop import set_volume, focus_window
from capri.tools.shell import run_bash


@pytest.mark.asyncio
async def test_set_volume_conversational_and_safe_scaling():
    """Проверяем безопасное масштабирование громкости и разговорные команды."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc_mock = AsyncMock()
        proc_mock.communicate.return_value = (b"", b"")
        mock_exec.return_value = proc_mock

        # 1. "тише"
        res = await set_volume("тише")
        assert "Громкость изменена на -10%" in res
        mock_exec.assert_called_with("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.10-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        # 2. "громче"
        res = await set_volume("громче")
        assert "Громкость изменена на +10%" in res
        mock_exec.assert_called_with("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.10+", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        # 3. "50" без знака % -> должно безопасно смасштабироваться в 0.50, а не 50.0 (5000%)
        res = await set_volume("50")
        assert "50%" in res
        mock_exec.assert_called_with("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.50", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        # 4. "100"
        res = await set_volume("100")
        assert "100%" in res
        mock_exec.assert_called_with("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "1.0", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)


@pytest.mark.asyncio
async def test_run_bash_stderr_truncation():
    """Проверяем, что гигантский stderr обрезается до max_chars, предотвращая переполнение LLM-контекста."""
    huge_err = "ERROR_LINE\n" * 500  # > 5000 chars
    with patch("asyncio.create_subprocess_shell") as mock_shell:
        proc_mock = AsyncMock()
        proc_mock.returncode = 1
        proc_mock.communicate.return_value = (b"", huge_err.encode("utf-8"))
        mock_shell.return_value = proc_mock

        res = await run_bash("faulty_command")
        assert "STDERR (код 1):" in res
        assert "stderr обрезан, всего" in res
        assert len(res) < 3500


def test_agent_single_key_client_lifecycle():
    """Проверяем создание клиента AsyncOpenAI с единым первичным ключом."""
    cfg = AppConfig(
        llm=LLMConfig(
            api_key="primary-gsk-key",
            model="openai/gpt-oss-120b",
        )
    )
    agent = CapriAgent(cfg)

    client = agent._get_client()
    assert client.api_key == "primary-gsk-key"
    assert str(client.base_url).rstrip("/") == "https://api.groq.com/openai/v1"


@pytest.mark.asyncio
async def test_agent_sambanova_parameter_compatibility():
    """Проверяем, что для SambaNova не передаются presence_penalty и frequency_penalty (защита от 400 Bad Request)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    cfg = AppConfig(
        llm=LLMConfig(
            provider="sambanova",
            api_key="samba-key-123",
            model="Meta-Llama-3.3-70B-Instruct",
        )
    )
    agent = CapriAgent(cfg)

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="Привет, D!", tool_calls=None))]
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

    with patch.object(agent, "_get_client", return_value=mock_client):
        async for _ in agent.chat("Тест", speak=False):
            pass

        assert mock_client.chat.completions.create.call_count == 1
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert "presence_penalty" not in call_kwargs
        assert "frequency_penalty" not in call_kwargs


@pytest.mark.asyncio
async def test_agent_deepseek_compatibility_and_reasoning_retention():
    """Проверяем совместимость с DeepSeek: отсутствие штрафов, сохранение reasoning_content."""
    cfg = AppConfig(
        llm=LLMConfig(
            provider="deepseek",
            api_key="ds-key-456",
            model="deepseek-flash",
            presence_penalty=0.5,
            frequency_penalty=0.6,
        )
    )
    agent = CapriAgent(cfg)

    mock_client = MagicMock()
    mock_msg = MagicMock(content="Привет из Найт-Сити!", tool_calls=None)
    mock_msg.reasoning_content = "Рассуждаю о текущем контексте пользователя D..."
    mock_resp = MagicMock(choices=[MagicMock(message=mock_msg)])
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

    with patch.object(agent, "_get_client", return_value=mock_client):
        events = []
        async for ev in agent.chat("Привет", speak=False):
            events.append(ev)

        assert mock_client.chat.completions.create.call_count == 1
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert "presence_penalty" not in call_kwargs
        assert "frequency_penalty" not in call_kwargs

        # Проверяем эмит события мысли из CoT DeepSeek
        thought_events = [e for e in events if e.type == "thought"]
        assert len(thought_events) > 0
        assert "DEEPSEEK" in thought_events[0].data

        # Проверяем сохранение reasoning_content в истории для последующих tool turns
        assert agent.history[-1]["role"] == "assistant"
        assert agent.history[-1]["reasoning_content"] == "Рассуждаю о текущем контексте пользователя D..."


@pytest.mark.asyncio
async def test_deepseek_reasoning_sanitization_and_resilience():
    """Проверяем санитизацию reasoning_content для DeepSeek и защиту от 500 ошибок."""
    cfg = AppConfig(llm=LLMConfig(provider="deepseek", model="deepseek-flash", api_key="sk-test"))
    agent = CapriAgent(cfg)
    client = agent._create_openai_client("sk-test")
    assert client.timeout == 60.0
    assert client.max_retries == 3

    # 1. История с пропущенным reasoning_content в одном из ответов
    raw_history = [
        {"role": "system", "content": "Prompt"},
        {"role": "user", "content": "Вопрос 1"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "run_bash"}}], "reasoning_content": "Мысль 1"},
        {"role": "tool", "content": "Результат"},
        {"role": "assistant", "content": "Ответ без мысли"},  # missing reasoning_content
        {"role": "user", "content": "Вопрос 2"},
        {"role": "assistant", "content": "Финал", "reasoning_content": "Мысль 2"},
    ]

    # Для DeepSeek: недостающий reasoning_content должен быть безопасно синтезирован
    sanitized_ds = agent._prepare_messages_for_llm(raw_history, provider="deepseek")
    for m in sanitized_ds:
        if m.get("role") == "assistant":
            assert "reasoning_content" in m
            assert len(m["reasoning_content"].strip()) > 0

    # Для Groq: поле reasoning_content должно быть удалено во избежание 400
    sanitized_groq = agent._prepare_messages_for_llm(raw_history, provider="groq")
    for m in sanitized_groq:
        if m.get("role") == "assistant":
            assert "reasoning_content" not in m

    # 2. Перехват псевдо-инструмента appraisal
    from capri.tools.registry import registry
    res = await registry.execute("appraisal", {})
    assert "appraisal" in res and "внутренний XML-тег" in res


def test_bracket_thought_stripping_and_tts_defense():
    """Проверяем парсинг bracket-appraisal, санитизацию текста диалога и защиту TTS от утечки мыслей."""
    from capri.core.agent import parse_and_strip_appraisal
    from capri.voice.tts import clean_text_for_tts

    # 1. Парсинг краткой формы appraisal в скобках [ROAST | conf: 90%] intent → strategy
    raw_roast = (
        "[ROAST | conf: 90%] снова подколол «глупой ИИшкой» и обвинил в закомментированных конфигах → парировать с ухмылкой, предъявить факты\n\n"
        "Проверила, раз уж на то пошло. В конфигах чисто."
    )
    appraisal, clean_roast = parse_and_strip_appraisal(raw_roast)
    assert appraisal.get("category") == "ROAST"
    assert appraisal.get("confidence") == 0.9
    assert "снова подколол" in appraisal.get("intent", "")
    assert "парировать с ухмылкой" in appraisal.get("strategy", "")
    assert "[ROAST" not in clean_roast
    assert "парировать" not in clean_roast
    assert clean_roast == "Проверила, раз уж на то пошло. В конфигах чисто."

    # 2. Парсинг [CHAT] без указания conf
    raw_chat = "[CHAT] поздоровался, ждёт ответа → ответить живо\n\nПривет, D!"
    app_chat, clean_chat = parse_and_strip_appraisal(raw_chat)
    assert app_chat.get("category") == "CHAT"
    assert clean_chat == "Привет, D!"

    # 3. Вычищение неформатных мыслей (Мысли: ...) и [DEEPSEEK]
    raw_cot = "(Мысли: пользователь хочет проверить статус)\n[DEEPSEEK] Анализирую процессы\nВсё работает стабильно."
    _, clean_cot = parse_and_strip_appraisal(raw_cot)
    assert "Мысли:" not in clean_cot
    assert "DEEPSEEK" not in clean_cot
    assert clean_cot == "Всё работает стабильно."

    # 4. Защита TTS (clean_text_for_tts) как последний барьер безопасности
    tts_input = (
        "[ROAST | conf: 90%] снова подколол «глупой ИИшкой» → парировать с ухмылкой\n\n"
        "*(мысли: проверила)* [DEEPSEEK] Думаю...\n"
        "Ты зря смеёшься, D. Система под полным контролем."
    )
    tts_cleaned = clean_text_for_tts(tts_input, engine="fish")
    assert "ROAST" not in tts_cleaned
    assert "conf:" not in tts_cleaned
    assert "парировать" not in tts_cleaned
    assert "DEEPSEEK" not in tts_cleaned
    assert "мысли" not in tts_cleaned
    assert "[" not in tts_cleaned and "]" not in tts_cleaned
    assert "Ди" in tts_cleaned  # Проверка адаптации имени D
    assert "Ты зря смеёшься, Ди. Система под полным контролем." in tts_cleaned



