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
