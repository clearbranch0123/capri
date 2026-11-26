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

    # 1b. Switching manually via LLM_PROVIDER=ollama over an existing cloud config.yaml resets stale cloud model,
    # while preserving explicit local Ollama tags (e.g. `deepseek-r1:8b`) when config.yaml already targets ollama.
    stale_cloud_yaml = tmp_path / "stale_cloud.yaml"
    stale_cloud_yaml.write_text("llm:\n  provider: groq\n  model: openai/gpt-oss-120b\n", encoding="utf-8")
    cfg_overridden = load_config(stale_cloud_yaml)
    assert cfg_overridden.llm.provider == "ollama"
    assert cfg_overridden.llm.model == "llama3.2"

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    stale_yaml_edit = tmp_path / "stale_yaml_edit.yaml"
    stale_yaml_edit.write_text("llm:\n  provider: ollama\n  model: deepseek-flash\n", encoding="utf-8")
    assert load_config(stale_yaml_edit).llm.model == "llama3.2"

    valid_local_yaml = tmp_path / "valid_local_tag.yaml"
    valid_local_yaml.write_text("llm:\n  provider: ollama\n  model: deepseek-r1:8b\n", encoding="utf-8")
    assert load_config(valid_local_yaml).llm.model == "deepseek-r1:8b"

    # 1c. Stale cloud LLM_MODEL in .env is rejected when LLM_PROVIDER=ollama,
    # and a valid local tag in config.yaml (`deepseek-r1:8b`) is preserved even when LLM_PROVIDER=ollama overrides YAML provider!
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")
    assert load_config(tmp_path / "env_stale_cloud.yaml").llm.model == "llama3.2"

    mixed_yaml = tmp_path / "mixed_local_tag.yaml"
    mixed_yaml.write_text("llm:\n  provider: groq\n  model: deepseek-r1:8b\n", encoding="utf-8")
    assert load_config(mixed_yaml).llm.model == "deepseek-r1:8b"

    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert load_config(mixed_yaml).llm.model == "deepseek-r1:8b"

    # Valid local LLM_MODEL in .env takes precedence cleanly
    monkeypatch.setenv("LLM_MODEL", "qwen2.5:14b")
    assert load_config(mixed_yaml).llm.model == "qwen2.5:14b"
    monkeypatch.delenv("LLM_MODEL", raising=False)

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


def test_live_http_wire_smoke_ollama_and_lmstudio_keyless_streaming(tmp_path):
    """
    Real TCP/HTTP wire-level integration test (zero HTTP/OpenAI client mocks) against a live local
    OpenAI-compatible server emulating both Ollama (400 `does not support tools` -> retry -> SSE stream)
    and LM Studio (`/v1/models` + `/v1/chat/completions` without Authorization header).
    """
    import asyncio
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from unittest.mock import patch
    from capri.core.agent import CapriAgent
    from capri.core.emotions import EmotionEngine
    from capri.core.memory import MemoryStore
    from capri.core.models import fetch_available_models
    from capri.core.sessions import SessionManager
    from capri.ui.cyberdeck import check_api_key_sync

    recorded_requests: list[dict] = []

    class _LocalOpenAIServerHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            recorded_requests.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
            if self.path == "/v1/models":
                body = json.dumps(
                    {
                        "object": "list",
                        "data": [
                            {"id": "deepseek-r1:8b", "object": "model"},
                            {"id": "qwen2.5-coder:7b", "object": "model"},
                        ],
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            recorded_requests.append(
                {
                    "method": "POST",
                    "path": self.path,
                    "model": payload.get("model"),
                    "has_tools": "tools" in payload,
                    "stream": bool(payload.get("stream")),
                }
            )
            if self.path != "/v1/chat/completions":
                self.send_response(404)
                self.end_headers()
                return

            # Exact Ollama HTTP 400 error when a reasoning/non-tool model receives `tools`
            if payload.get("model") == "deepseek-r1:8b" and "tools" in payload:
                err_body = json.dumps({"error": {"message": "registry.ollama.ai/library/deepseek-r1:8b does not support tools", "type": "api_error"}}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
                return

            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                chunks = [
                    {"id": "chatcmpl-local-1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Живой "}, "finish_reason": None}]},
                    {"id": "chatcmpl-local-1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "ответ по HTTP!"}, "finish_reason": "stop"}]},
                ]
                for ch in chunks:
                    self.wfile.write(f"data: {json.dumps(ch, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            ok_body = json.dumps(
                {
                    "id": "chatcmpl-lmstudio-1",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "LM Studio OK."}, "finish_reason": "stop"}],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(ok_body)))
            self.end_headers()
            self.wfile.write(ok_body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalOpenAIServerHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}/v1"

    try:
        # A. Live keyless server check + /models discovery
        ok, msg = check_api_key_sync("llm", "", base_url=base_url)
        assert ok is True
        assert "HTTP 200" in msg
        assert recorded_requests[-1]["auth"] is None

        ollama_cfg = AppConfig()
        ollama_cfg.llm.provider = "ollama"
        ollama_cfg.llm.base_url = base_url
        ollama_cfg.llm.model = "deepseek-r1:8b"
        ollama_cfg.llm.api_key = ""
        ollama_cfg.voice.tts_enabled = False

        discovered = asyncio.run(fetch_available_models(ollama_cfg))
        assert discovered == ["deepseek-r1:8b", "qwen2.5-coder:7b"]

        # B. Real AsyncOpenAI streaming against Ollama endpoint (triggers HTTP 400 -> toolless retry -> SSE stream)
        iso_sm = SessionManager(db_path=tmp_path / "wire_sessions.db")
        with (
            patch("capri.core.agent.EmotionEngine", side_effect=lambda: EmotionEngine(file_path=tmp_path / "wire_emo.json")),
            patch("capri.core.agent.MemoryStore", side_effect=lambda: MemoryStore(file_path=tmp_path / "wire_mem.json")),
        ):
            agent_ollama = CapriAgent(ollama_cfg, session_manager=iso_sm)

        async def _stream_ollama():
            return [ev async for ev in agent_ollama.chat("Проверка сокета", speak=False, stream=True)]

        ev_ollama = asyncio.run(_stream_ollama())
        assert any(ev.type == "done" and "Живой ответ по HTTP!" in ev.data for ev in ev_ollama)
        post_calls = [r for r in recorded_requests if r["method"] == "POST"]
        assert len(post_calls) >= 2
        assert post_calls[0]["has_tools"] is True and post_calls[1]["has_tools"] is False

        # C. Real AsyncOpenAI against LM Studio config (non-streaming, tool-capable model)
        lms_cfg = AppConfig()
        lms_cfg.llm.provider = "lmstudio"
        lms_cfg.llm.base_url = base_url
        lms_cfg.llm.model = "qwen2.5-coder:7b"
        lms_cfg.llm.api_key = ""
        lms_cfg.voice.tts_enabled = False
        with (
            patch("capri.core.agent.EmotionEngine", side_effect=lambda: EmotionEngine(file_path=tmp_path / "wire_emo2.json")),
            patch("capri.core.agent.MemoryStore", side_effect=lambda: MemoryStore(file_path=tmp_path / "wire_mem2.json")),
        ):
            agent_lms = CapriAgent(lms_cfg, session_manager=iso_sm)

        async def _call_lms():
            return [ev async for ev in agent_lms.chat("Проверка LM Studio", speak=False, stream=False)]

        ev_lms = asyncio.run(_call_lms())
        assert any(ev.type == "done" and "LM Studio OK." in ev.data for ev in ev_lms)
    finally:
        server.shutdown()
        server.server_close()
