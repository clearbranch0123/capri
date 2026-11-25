import pytest
from pathlib import Path
from capri.core.config import load_config, save_config, AppConfig
from capri.core.models import resolve_model_name, MODEL_ALIASES


def test_load_config(tmp_path):
    # Isolated default config check — never depends on personal local config.yaml
    config = load_config(tmp_path / "nonexistent_config.yaml")
    assert config.companion.name == "Капри"
    assert config.companion.user_name == "User"
    assert config.voice.wake_word_enabled is True
    assert "капри" in config.voice.wake_words

    # Also verify shipped config.example.yaml loads cleanly with empty secrets
    example_yaml = Path(__file__).resolve().parent.parent / "config.example.yaml"
    ex_cfg = load_config(example_yaml)
    assert ex_cfg.companion.name == "Капри"
    assert ex_cfg.safety.tool_policy == "ask"


def test_save_and_reload_config(tmp_path):
    test_yaml = tmp_path / "test_config.yaml"
    cfg = AppConfig()
    cfg.companion.mode = "focus"
    cfg.voice.ptt_key = "KEY_RIGHTALT"

    assert save_config(cfg, test_yaml) is True
    loaded = load_config(test_yaml)
    assert loaded.companion.mode == "focus"
    assert loaded.voice.ptt_key == "KEY_RIGHTALT"


def test_model_aliases():
    assert resolve_model_name("120b") == "openai/gpt-oss-120b"
    assert resolve_model_name("gpt") == "openai/gpt-oss-120b"
    assert resolve_model_name("qwen") == "qwen/qwen3.8-27b"
    assert resolve_model_name("27b") == "qwen/qwen3.8-27b"
    assert resolve_model_name("llama") == "Meta-Llama-3.3-70B-Instruct"
    assert resolve_model_name("deepseek") == "deepseek-flash"
    assert resolve_model_name("ds") == "deepseek-flash"
    assert resolve_model_name("v4") == "deepseek-flash"
    assert resolve_model_name("v4.1") == "deepseek-flash"
    assert resolve_model_name("v4-pro") == "deepseek-v4-pro"
    assert resolve_model_name("v3") == "deepseek-chat"
    assert resolve_model_name("r1") == "deepseek-reasoner"
    assert resolve_model_name("unknown-model") == "unknown-model"


def test_deepseek_provider_config(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds_key_abc123")
    config = load_config(tmp_path / "isolated_ds.yaml")
    assert config.llm.provider == "deepseek"
    assert config.llm.api_key == "ds_key_abc123"
    assert config.llm.base_url == "https://api.deepseek.com"
    assert config.llm.model == "deepseek-flash"


def test_sambanova_provider_config(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "sambanova")
    monkeypatch.setenv("SAMBANOVA_API_KEY", "samba_key_789")
    config = load_config(tmp_path / "isolated_samba.yaml")
    assert config.llm.provider == "sambanova"
    assert config.llm.api_key == "samba_key_789"
    assert config.llm.base_url == "https://api.sambanova.ai/v1"
    assert config.llm.model == "Meta-Llama-3.3-70B-Instruct"


def test_update_env_value(tmp_path):
    from capri.core.config import update_env_value
    test_env = tmp_path / ".test_env"
    assert update_env_value("TEST_VAR", "hello_world", test_env) is True
    content = test_env.read_text()
    assert "TEST_VAR=hello_world" in content
    # Update existing
    assert update_env_value("TEST_VAR", "new_value", test_env) is True
    content = test_env.read_text()
    assert "TEST_VAR=new_value" in content


def test_ollama_and_lmstudio_keyless_provider_flow(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch
    from PySide6.QtWidgets import QApplication
    from capri.core.agent import CapriAgent
    from capri.core.config import is_local_llm_endpoint, provider_env_key_name
    from capri.core.doctor import run_diagnostics
    from capri.core.models import fetch_available_models
    from capri.ui.cyberdeck import CapriFirstRunWizard, CapriProviderSettingsDialog, check_api_key_sync

    for k in ("GROQ_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "SAMBANOVA_API_KEY", "CAPRI_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)

    assert is_local_llm_endpoint("ollama", "http://localhost:11434/v1") is True
    assert is_local_llm_endpoint("lmstudio", "http://localhost:1234/v1") is True
    assert is_local_llm_endpoint("openai", "http://127.0.0.1:8080/v1") is True
    assert is_local_llm_endpoint("groq", "https://api.groq.com/openai/v1") is False
    assert provider_env_key_name("deepseek") == "DEEPSEEK_API_KEY"
    assert provider_env_key_name("ollama") == "OLLAMA_API_KEY"

    # 1. load_config for ollama works cleanly without any API key
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    cfg = load_config(tmp_path / "ollama_cfg.yaml")
    assert cfg.llm.provider == "ollama"
    assert cfg.llm.base_url == "http://localhost:11434/v1"
    assert cfg.llm.model == "llama3.2"
    assert cfg.llm.api_key == ""

    # 2. /doctor reports OK for local keyless Ollama (and FAIL for keyless cloud Groq)
    diag = {i.name: i for i in run_diagnostics(cfg, sessions_db_path=tmp_path / "s.db")}
    assert diag["LLM-ядро"].status == "OK"
    assert "локальный сервер" in diag["LLM-ядро"].detail

    cloud_cfg = AppConfig()
    cloud_cfg.llm.provider = "groq"
    cloud_cfg.llm.base_url = "https://api.groq.com/openai/v1"
    cloud_cfg.llm.api_key = ""
    diag_cloud = {i.name: i for i in run_diagnostics(cloud_cfg, sessions_db_path=tmp_path / "s2.db")}
    assert diag_cloud["LLM-ядро"].status == "FAIL"

    # 3. check_api_key_sync & fetch_available_models work without API key on localhost
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"data": [{"id": "qwen2.5:14b"}, {"id": "deepseek-r1:8b"}]}
    with patch("httpx.Client.get", return_value=fake_resp):
        ok, msg = check_api_key_sync("llm", "", base_url="http://localhost:11434/v1")
        assert ok is True
        assert "Локальный сервер доступен" in msg

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=fake_resp):
        models = asyncio.run(fetch_available_models(cfg))
        assert models == ["qwen2.5:14b", "deepseek-r1:8b"]

    # 4. CapriAgent.chat succeeds without API key AND retries without tools if local model doesn't support tools
    from capri.core.emotions import EmotionEngine
    from capri.core.memory import MemoryStore
    from capri.core.sessions import SessionManager

    cfg.voice.tts_enabled = False
    iso_sm = SessionManager(db_path=tmp_path / "iso_sessions.db")
    with (
        patch("capri.core.agent.EmotionEngine", side_effect=lambda: EmotionEngine(file_path=tmp_path / "iso_emo.json")),
        patch("capri.core.agent.MemoryStore", side_effect=lambda: MemoryStore(file_path=tmp_path / "iso_mem.json")),
    ):
        agent = CapriAgent(cfg, session_manager=iso_sm)
    calls = []

    async def fake_create(**kwargs):
        calls.append(dict(kwargs))
        if "tools" in kwargs:
            raise RuntimeError("Error code: 400 - {'error': {'message': 'deepseek-r1:8b does not support tools'}}")
        msg_obj = SimpleNamespace(content="Локальный ответ готов.", tool_calls=None, reasoning_content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg_obj)])

    mock_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    agent._get_client = lambda: mock_client

    async def _run_turn():
        return [ev async for ev in agent.chat("Привет", speak=False, stream=False)]

    events = asyncio.run(_run_turn())
    assert len(calls) == 2
    assert "tools" in calls[0] and "tools" not in calls[1]
    assert any(ev.type == "done" and "Локальный ответ готов." in ev.data for ev in events)

    # 5. Settings & Setup Wizard expose ollama and lmstudio
    QApplication.instance() or QApplication([])
    wiz = CapriFirstRunWizard(cfg)
    assert "ollama" in wiz._provider_ids
    assert "lmstudio" in wiz._provider_ids
    wiz.close()

    dlg = CapriProviderSettingsDialog(cfg)
    assert "ollama" in dlg.providers_map.values()
    assert "lmstudio" in dlg.providers_map.values()
    dlg.close()
