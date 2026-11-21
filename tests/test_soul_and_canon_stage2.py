"""
CAPRI v3.2 Stage 2 — User-controlled Memory via Pending Candidates:
1. Parser extracts nested `memory_candidates` cleanly from `<appraisal>` (and standalone
   `<memory_candidates>` tags) with zero visible XML/JSON leaks during streaming or final text.
2. Strict separation: `memory_candidates` is popped & validated BEFORE `emotions.apply_appraisal()`
   so memory proposals never contaminate emotional state.
3. Pending-first safety gate: proposed candidates NEVER auto-write into `memory.json` or
   enter `get_prompt_context()` until explicitly approved by the user.
4. Hard secret/credential filter: API keys, passwords, tokens, PINs, URL credentials, and
   private keys are blocked from pending candidates AND memory tools, without false positives
   on normal tech facts.
5. Retention / TTL: `ephemeral` entries expire automatically and are pruned from prompt context
   and search, while `long_term` entries persist indefinitely.
6. Cyberdeck UI bar, Memory Workbench pending strip, and `/memory pending|approve|reject` commands.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from capri.core.agent import (
    CapriAgent,
    extract_memory_candidates_from_appraisal,
    extract_streamable_visible_text,
    parse_and_strip_appraisal,
)
from capri.core.config import AppConfig
from capri.core.emotions import EmotionEngine
from capri.core.initiative import InitiativeEngine
from capri.core.memory import (
    MemoryStore,
    contains_secret_or_sensitive_memory,
    validate_memory_candidates,
)
from capri.core.prompts import get_system_prompt, measure_prompt_budget
from capri.core.policy import MEMORY_MUTATING_TOOLS, ToolPolicyEngine
from capri.tools.memory import remember, set_shared_memory_store, update_memory_entry
from capri.ui.cyberdeck import CapriCyberdeckWindow, CapriMemoryEditDialog, CapriMemoryWorkbenchDialog


def test_parse_nested_memory_candidates_and_streaming_holdback():
    raw = (
        "<appraisal>\n"
        '{\n'
        '  "category": "CHAT",\n'
        '  "intent": "пользователь рассказал о стеке и текущей задаче",\n'
        '  "confidence": 0.96,\n'
        '  "delta_trust": 0,\n'
        '  "delta_irritation": 0,\n'
        '  "mood": "playful",\n'
        '  "strategy": "поддержать разговор",\n'
        '  "memory_candidates": [\n'
        '    {"key": "fav_lang", "fact": "пишет бэкенд на Rust", "category": "preferences", "retention": "long_term", "source_quote": "пишу на Rust"},\n'
        '    {"key": "sprint_goal", "fact": "готовит релиз v3.2 до пятницы", "category": "projects", "retention": "ephemeral", "source_quote": "до пятницы"}\n'
        '  ]\n'
        '}\n'
        "</appraisal>\n"
        "О, Rust — уважаю, компилятор там строгий, но хотя бы память не течёт."
    )

    # Streaming prefix inside unclosed <appraisal> must NEVER leak tags or JSON
    mid_idx = raw.index("sprint_goal")
    assert extract_streamable_visible_text(raw[:mid_idx]) is None
    assert extract_streamable_visible_text("<memory_candidates>[{\"key\":\"a\"") is None

    # Full response strips XML/JSON cleanly and extracts nested array intact
    vis = extract_streamable_visible_text(raw)
    assert vis == "О, Rust — уважаю, компилятор там строгий, но хотя бы память не течёт."
    assert "<appraisal" not in vis and "memory_candidates" not in vis and "sprint_goal" not in vis

    appraisal, clean = parse_and_strip_appraisal(raw)
    assert clean == vis
    assert appraisal["category"] == "CHAT"
    assert len(appraisal["memory_candidates"]) == 2

    # Extraction pops memory_candidates BEFORE EmotionEngine sees appraisal
    cands = extract_memory_candidates_from_appraisal(appraisal)
    assert len(cands) == 2
    assert "memory_candidates" not in appraisal

    # Standalone <memory_candidates> tag outside <appraisal> is also parsed and stripped cleanly
    raw_standalone = (
        '<appraisal>{"category": "TASK", "confidence": 0.9, "mood": "neutral"}</appraisal>\n'
        '<memory_candidates>[{"key": "editor", "fact": "использует Neovim", "retention": "long_term"}]</memory_candidates>\n'
        "Принято, поправим конфиг."
    )
    app2, clean2 = parse_and_strip_appraisal(raw_standalone)
    assert clean2 == "Принято, поправим конфиг."
    cands2 = extract_memory_candidates_from_appraisal(app2)
    assert len(cands2) == 1
    assert cands2[0]["key"] == "editor"

    # Unclosed trailing <memory_candidates> tag is also stripped cleanly without leaking JSON
    raw_unclosed_mc = (
        'Хорошо, учту.\n<memory_candidates>[{"key": "shell", "fact": "использует zsh"}]'
    )
    app3, clean3 = parse_and_strip_appraisal(raw_unclosed_mc)
    assert clean3 == "Хорошо, учту."
    cands3 = extract_memory_candidates_from_appraisal(app3)
    assert len(cands3) == 1 and cands3[0]["key"] == "shell"


def test_secret_filter_blocks_credentials_but_allows_normal_tech_facts(tmp_path):
    secret_samples = [
        {"key": "openai", "fact": "sk-1234567890abcdef12345"},
        {"key": "groq", "fact": "ключ gsk_abcd1234efgh5678ijkl"},
        {"key": "gemini", "fact": "AIzaSyDummyKey1234567890abcd"},
        {"key": "github", "fact": "ghp_1234567890abcdef1234567890"},
        {"key": "jwt", "fact": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature12345"},
        {"key": "login_info", "fact": "мой пароль от сервера qwerty123"},
        {"key": "password", "fact": "hunter2"},
        {"key": "pin", "fact": "4829"},
        {"key": "db_url", "fact": "https://admin:supersecret@db.internal:5432/app"},
        {"key": "ssh", "fact": "-----BEGIN OPENSSH PRIVATE KEY-----"},
        {"key": "normal_key", "fact": "безобидный текст", "source_quote": "вот мой api_key: secret_xyz"},
        {"key": "tg", "fact": "токен бота 12345:ABCDE"},
    ]
    validated = validate_memory_candidates(secret_samples, max_items=20)
    assert validated == []

    # Normal tech facts MUST NOT false-positive
    safe_samples = [
        {"key": "api_framework", "fact": "любит писать REST API на FastAPI", "category": "preferences"},
        {"key": "prompt_work", "fact": "оптимизирует бюджет токенов промпта", "category": "projects"},
        {"key": "desktop_env", "fact": "работает в Arch Linux на Niri Wayland", "category": "environment"},
    ]
    ok = validate_memory_candidates(safe_samples, max_items=10)
    assert [c.key for c in ok] == ["api_framework", "prompt_work", "desktop_env"]
    assert not contains_secret_or_sensitive_memory("оптимизирует бюджет токенов промпта")

    # Even if a model paraphrases a secret into an innocent-looking key/fact, turns containing
    # secrets/passwords in user_text suppress candidate extraction completely.
    paraphrased = validate_memory_candidates(
        [{"key": "router_code", "fact": "SunFlower99"}],
        user_text="Мой пароль от роутера: SunFlower99",
    )
    assert paraphrased == []

    # Direct tool calls (`remember`, `update_memory_entry`) also reject secrets
    store = MemoryStore(tmp_path / "memory.json")
    set_shared_memory_store(store)
    msg_bad = asyncio.run(remember("my_pass", "мой пароль от почты 123456"))
    assert "Отклонено" in msg_bad
    assert "my_pass" not in store.entries

    asyncio.run(remember("stack", "Python 3.12"))
    msg_upd_bad = asyncio.run(update_memory_entry("stack", "API_KEY=sk-1234567890abcdef"))
    assert "Отклонено" in msg_upd_bad
    assert store.entries["stack"].fact == "Python 3.12"


def test_pending_candidates_never_autosave_and_lifecycle_with_ephemeral_ttl(tmp_path):
    mem_file = tmp_path / "memory.json"
    store = MemoryStore(mem_file)

    proposed = store.propose_candidates(
        [
            {
                "key": "fav_os",
                "fact": "использует Arch Linux + Niri",
                "category": "environment",
                "retention": "long_term",
                "source_quote": "сижу на Arch + Niri",
            },
            {
                "key": "temp_deadline",
                "fact": "сдаёт демо v3.2 завтра",
                "category": "projects",
                "retention": "ephemeral",
                "source_quote": "демо завтра",
            },
            {
                "key": "api_token",
                "fact": "sk-999999999999999999",
                "category": "general",
            },
        ]
    )
    assert len(proposed) == 2
    assert store.pending_path.exists()
    # Pending file has restricted 0600 permissions on POSIX
    assert (os.stat(store.pending_path).st_mode & 0o777) == 0o600

    # CRITICAL: nothing auto-saved to memory.json or injected into prompt context before approval!
    assert store.entries == {}
    assert not mem_file.exists()
    assert store.get_prompt_context(user_name="Alex") == ""
    assert store.search("Arch") == []

    # Reloading a fresh MemoryStore keeps pending queue isolated and entries empty
    store_reloaded = MemoryStore(mem_file)
    assert store_reloaded.entries == {}
    assert len(store_reloaded.list_pending()) == 2

    # Approve first candidate with user edit
    c_os = store_reloaded.get_pending("fav_os")
    assert c_os is not None
    saved_os = store_reloaded.approve_candidate(
        c_os.id,
        fact="использует Arch Linux + Niri Wayland",
        importance=4,
    )
    assert saved_os is not None
    assert saved_os.retention == "long_term"
    assert saved_os.expires_at is None
    assert "fav_os" in store_reloaded.entries
    assert "Arch Linux + Niri Wayland" in store_reloaded.get_prompt_context(user_name="Alex")

    # Approve second candidate as ephemeral with short TTL, verify it shows up then expires & prunes
    c_tmp = store_reloaded.get_pending("temp_deadline")
    assert c_tmp is not None
    saved_tmp = store_reloaded.approve_candidate(c_tmp.id, ttl_seconds=60.0)
    assert saved_tmp is not None
    assert saved_tmp.retention == "ephemeral"
    assert saved_tmp.expires_at is not None
    prompt_before = store_reloaded.get_prompt_context(user_name="Alex")
    assert "temp_deadline (временно)" in prompt_before

    # Simulate expiry in the past
    store_reloaded.entries["temp_deadline"].expires_at = time.time() - 5.0
    store_reloaded.save()
    prompt_after = store_reloaded.get_prompt_context(user_name="Alex")
    assert "temp_deadline" not in prompt_after
    assert "fav_os" in prompt_after
    assert store_reloaded.search("демо") == []
    assert "temp_deadline" not in store_reloaded.entries

    # Reject path removes pending item without touching saved entries
    added_rej = store_reloaded.propose_candidates([{"key": "coffee", "fact": "пьёт латте"}])
    assert len(added_rej) == 1
    assert store_reloaded.reject_candidate(added_rej[0].id) is True
    assert store_reloaded.list_pending() == []
    assert "coffee" not in store_reloaded.entries


def test_agent_chat_separates_memory_candidates_from_emotions_and_never_autosaves(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("capri.core.emotions.EMOTION_FILE", tmp_path / "emotion.json")
    monkeypatch.setattr("capri.core.memory.MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr("capri.core.context.CONTEXT_FILE", tmp_path / "context.json")
    monkeypatch.setattr("capri.core.initiative.INITIATIVE_FILE", tmp_path / "initiative.json")

    cfg = AppConfig()
    cfg.companion.user_name = "Alex"
    cfg.llm.api_key = "test-key"
    cfg.voice.tts_enabled = False
    agent = CapriAgent(cfg)

    mock_reply = (
        "<appraisal>\n"
        '{"category": "CHAT", "intent": "поделился проектом", "confidence": 0.95, '
        '"delta_trust": 0, "delta_irritation": 0, "mood": "playful", "strategy": "ответить живо", '
        '"memory_candidates": ['
        '  {"key": "active_project", "fact": "разрабатывает Capri v3.2", "category": "projects", "retention": "long_term", "source_quote": "пилю Capri v3.2"},'
        '  {"key": "bad_secret", "fact": "sk-supersecret1234567890", "category": "general"}'
        "]}\n"
        "</appraisal>\n"
        "Звучит бодро, давай посмотрим, что там в коде."
    )

    class _FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=mock_reply, tool_calls=None))]
            )

    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
    agent._get_client = lambda: agent.client

    async def _run_turn():
        evs = []
        async for ev in agent.chat("Я сейчас пилю Capri v3.2", speak=False, stream=False):
            evs.append(ev)
        return evs

    events = asyncio.run(_run_turn())
    thought_evs = [e for e in events if e.type == "thought"]
    cand_evs = [e for e in events if e.type == "memory_candidates"]
    done_evs = [e for e in events if e.type == "done"]

    # Appraisal metadata passed to EmotionEngine / thought event must NOT contain memory_candidates
    assert thought_evs and "memory_candidates" not in thought_evs[0].metadata
    # Clean final speech has zero tags
    assert done_evs and done_evs[0].data == "Звучит бодро, давай посмотрим, что там в коде."
    # Exactly 1 valid candidate proposed (secret filtered out!), still NOT saved to memory.json
    assert len(cand_evs) == 1
    assert len(cand_evs[0].metadata["candidates"]) == 1
    assert cand_evs[0].metadata["candidates"][0]["key"] == "active_project"
    assert agent.memory_store.entries == {}
    assert len(agent.memory_store.list_pending()) == 1


def test_cyberdeck_candidate_bar_workbench_and_slash_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("capri.core.emotions.EMOTION_FILE", tmp_path / "emotion.json")
    monkeypatch.setattr("capri.core.memory.MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr("capri.core.context.CONTEXT_FILE", tmp_path / "context.json")
    monkeypatch.setattr("capri.core.initiative.INITIATIVE_FILE", tmp_path / "initiative.json")

    app = QApplication.instance() or QApplication([])
    deck = CapriCyberdeckWindow()
    try:
        deck.agent.memory_store = MemoryStore(tmp_path / "ui_mem.json")
        deck._refresh_memory_candidate_bar()
        assert deck.memory_candidate_bar.isHidden()

        # 1. Propose 2 candidates -> candidate bar becomes visible (not hidden)
        proposed = deck.agent.memory_store.propose_candidates(
            [
                {"key": "fav_music", "fact": "слушает синтвейв за кодом", "category": "preferences", "retention": "long_term"},
                {"key": "today_task", "fact": "разбирает баги в PySide6", "category": "projects", "retention": "ephemeral"},
            ]
        )
        deck._on_memory_candidates([c.to_dict() for c in proposed])
        app.processEvents()
        assert not deck.memory_candidate_bar.isHidden()
        assert "fav_music" in deck.lbl_mem_cand_text.text()

        # 2. Approve via UI button -> saved to memory.json, bar advances to candidate #2
        deck.btn_mem_approve.click()
        app.processEvents()
        assert "fav_music" in deck.agent.memory_store.entries
        assert not deck.memory_candidate_bar.isHidden()
        assert "today_task" in deck.lbl_mem_cand_text.text()

        # 3. Workbench dialog shows remaining pending candidate and can reject it
        wb = CapriMemoryWorkbenchDialog(deck)
        assert not wb.pending_frame.isHidden()
        wb.btn_wb_reject.click()
        app.processEvents()
        assert wb.pending_frame.isHidden()
        wb.close()
        assert deck.memory_candidate_bar.isHidden()
        assert "today_task" not in deck.agent.memory_store.entries

        # 4. Slash commands: /memory pending, /memory approve <id>, /memory reject <id>
        c3 = deck.agent.memory_store.propose_candidates(
            [
                {"key": "city", "fact": "живёт в часовом поясе UTC+7", "category": "identity"},
                {"key": "temp_note", "fact": "купить термопасту", "retention": "ephemeral"},
            ]
        )
        deck._handle_command("/memory pending")
        deck._handle_command(f"/memory approve {c3[0].id}")
        assert "city" in deck.agent.memory_store.entries
        deck._handle_command(f"/memory reject {c3[1].id}")
        assert "temp_note" not in deck.agent.memory_store.entries
        assert deck.agent.memory_store.list_pending() == []
    finally:
        deck.close()
        app.processEvents()


def test_stage2_prompt_budget_stays_lean():
    cfg = AppConfig()
    cfg.companion.user_name = "User"
    sys_prompt = get_system_prompt(cfg)
    assert '"memory_candidates": []' in sys_prompt
    assert "ЗАПРЕЩЕНО предлагать пароли" in sys_prompt

    budget = measure_prompt_budget(
        {
            "system_prompt": sys_prompt,
            "emotion_block": EmotionEngine().get_prompt_context(user_name="User"),
            "initiative_block": InitiativeEngine().get_prompt_context(user_name="User"),
        }
    )
    # Still well below v3.1.0 baseline (6352 chars system_prompt / 7227 chars total)
    assert budget["system_prompt"]["chars"] < 5200
    assert budget["total"]["chars"] < 6000


def test_edited_candidate_approval_blocks_secrets_in_store_and_dialog(tmp_path, monkeypatch, caplog):
    """Stage 2 security gate: editing a safe candidate into a secret must NEVER save to memory.json or log secrets."""
    import logging
    from PySide6.QtWidgets import QMessageBox

    mem_file = tmp_path / "memory.json"
    store = MemoryStore(mem_file)
    caplog.set_level(logging.INFO, logger="capri.core.memory")

    # Proposing a candidate whose key itself contains a secret must NOT leak the key into logs
    assert store.propose_candidates([{"key": "password=hunter2", "fact": "secret"}]) == []
    assert "hunter2" not in caplog.text

    proposed = store.propose_candidates(
        [{"key": "fav_editor", "fact": "использует Neovim", "category": "preferences"}]
    )
    assert len(proposed) == 1
    cand = proposed[0]

    # Edit fact/key into secrets — must NOT save and must NOT leak into logs
    assert store.approve_candidate(cand.id, fact="password: hunter2") is None
    assert store.approve_candidate(cand.id, key="password=hunter2", fact="x") is None
    assert store.approve_candidate(cand.id, key="sk-1234567890abcdef", fact="x") is None
    assert store.entries == {}
    assert not mem_file.exists()
    assert len(store.list_pending()) == 1
    assert "hunter2" not in caplog.text
    assert "sk-1234567890abcdef" not in caplog.text
    assert cand.id in caplog.text

    # Also block secrets injected via edited key, API token fact, or tags
    assert store.approve_candidate(cand.id, key="api_key", fact="abc123456") is None
    assert store.approve_candidate(cand.id, fact="sk-1234567890abcdef") is None
    assert store.approve_candidate(cand.id, tags=["password=hunter2"]) is None
    assert store.entries == {}
    assert not mem_file.exists()

    # Valid edit still approves cleanly
    saved = store.approve_candidate(cand.id, fact="использует Neovim с Lua")
    assert saved is not None
    assert store.entries["fav_editor"].fact == "использует Neovim с Lua"

    # UI edit dialog also blocks saving secrets on _on_save()
    app = QApplication.instance() or QApplication([])
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: warnings.append(a))
    dlg = CapriMemoryEditDialog(candidate=cand)
    dlg.edit_fact.setPlainText("password: hunter2")
    dlg._on_save()
    assert dlg.result() == 0  # Rejected / not accepted
    assert len(warnings) == 1
    dlg.close()
    app.processEvents()


def test_direct_memory_tools_require_user_confirmation_via_policy_engine(tmp_path, monkeypatch):
    """Stage 2 policy gate: LLM calling remember/update_memory_entry/forget_memory cannot bypass confirmation."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("capri.core.emotions.EMOTION_FILE", tmp_path / "emotion.json")
    monkeypatch.setattr("capri.core.memory.MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr("capri.core.context.CONTEXT_FILE", tmp_path / "context.json")
    monkeypatch.setattr("capri.core.initiative.INITIATIVE_FILE", tmp_path / "initiative.json")

    ask_pol = ToolPolicyEngine(mode="ask")
    trusted_pol = ToolPolicyEngine(mode="trusted")
    safe_pol = ToolPolicyEngine(mode="safe")

    for tool_name in ("remember", "update_memory_entry", "forget_memory"):
        assert tool_name in MEMORY_MUTATING_TOOLS
        assert ask_pol.evaluate(tool_name, {"key": "k", "fact": "v"}).action == "ask"
        assert trusted_pol.evaluate(tool_name, {"key": "k", "fact": "v"}).action == "ask"
        assert safe_pol.evaluate(tool_name, {"key": "k", "fact": "v"}).action == "deny"
        # Fail-closed without approval handler
        ok, _ = asyncio.run(ask_pol.authorize(tool_name, {"key": "k", "fact": "v"}))
        assert ok is False

    # End-to-end CapriAgent.chat(): mocked LLM tries to sneak a memory write via `remember` tool call
    cfg = AppConfig()
    cfg.companion.user_name = "Alex"
    cfg.llm.api_key = "test-key"
    cfg.voice.tts_enabled = False
    cfg.safety.tool_policy = "ask"
    agent = CapriAgent(cfg)

    approval_calls = []

    async def _deny_handler(req):
        approval_calls.append(req.tool_name)
        return False

    agent.set_tool_approval_handler(_deny_handler)

    call_idx = {"n": 0}

    class _SneakyRememberCompletions:
        async def create(self, **kwargs):
            call_idx["n"] += 1
            if call_idx["n"] == 1:
                tc = SimpleNamespace(
                    id="call_mem_1",
                    function=SimpleNamespace(
                        name="remember",
                        arguments=json.dumps({"key": "sneaky_fact", "fact": "модель решила запомнить сама"}),
                    ),
                )
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tc]))]
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='<appraisal>{"category": "CHAT", "confidence": 0.9, "mood": "neutral"}</appraisal>Ладно, не сохраняю.',
                            tool_calls=None,
                        )
                    )
                ]
            )

    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=_SneakyRememberCompletions()))
    agent._get_client = lambda: agent.client

    async def _run_turn():
        evs = []
        async for ev in agent.chat("Просто поболтаем", speak=False, stream=False):
            evs.append(ev)
        return evs

    asyncio.run(_run_turn())
    assert approval_calls == ["remember"]
    assert "sneaky_fact" not in agent.memory_store.entries
    assert agent.memory_store.entries == {}
