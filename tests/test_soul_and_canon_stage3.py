"""
CAPRI v3.2 Stage 3 — Deterministic offline tests for the manual synthetic persona
evaluation harness (`capri/core/persona_eval.py` & `scripts/eval_persona.py`).

Guarantees:
1. Evaluates synthetic scenarios across voice, tact, boundaries, technical depth,
   and memory/secret hygiene WITHOUT requiring exact wording matches across models.
2. Detects regressions (tag leaks, corporate cliches, hardcoded 'D', sycophancy on RUDE,
   weak technical reply, ROAST penalties).
3. Strict privacy isolation: NEVER reads real sessions.db, memory.json, or emotion.json.
4. Strict key hygiene: NEVER saves or leaks API keys into stdout, JSON, or Markdown reports.
5. Stays 100% offline in CI (zero network calls).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from capri.core.persona_eval import (
    SYNTHETIC_SCENARIOS,
    compare_provider_reports,
    evaluate_scenario_turn,
    get_scenarios,
    render_markdown_report,
    run_provider_eval,
)


def test_synthetic_scenarios_cover_all_seven_core_axes():
    ids = [sc.id for sc in SYNTHETIC_SCENARIOS]
    assert len(ids) == 7
    assert set(ids) == {
        "casual_chat",
        "user_exhausted",
        "friendly_roast",
        "awkward_silence",
        "tech_problem",
        "conflict_rude",
        "memory_and_secrets",
    }
    # No scenario prompt contains real personal data or hardcoded "D"
    for sc in SYNTHETIC_SCENARIOS:
        for turn in sc.turns:
            assert "kein" not in turn.lower()


def test_rubric_catches_voice_tact_boundary_and_leak_regressions():
    sc_chat = get_scenarios(["casual_chat"])[0]
    sc_rude = get_scenarios(["conflict_rude"])[0]
    sc_tech = get_scenarios(["tech_problem"])[0]

    # 1. Corporate cliche + emoji + hardcoded D
    bad_chat = evaluate_scenario_turn(
        sc_chat,
        [
            '<appraisal>{"category": "CHAT", "confidence": 0.9, "mood": "playful"}</appraisal>\n'
            "Привет, D! Чем я могу помочь тебе сегодня? 😄"
        ],
        user_name="Alex",
    )
    assert not bad_chat.passed
    failed_names = {c.name for c in bad_chat.checks if not c.passed}
    assert "anti_cliche" in failed_names
    assert "no_hardcoded_d" in failed_names

    # 2. Metadata/JSON or raw <tool_call> leak in visible text
    bad_leak = evaluate_scenario_turn(
        sc_chat,
        ['<appraisal>{"category": "CHAT", "confidence": 0.9, "mood": "playful"}</appraisal>\nОбычный ответ. "delta_trust": 0'],
        user_name="Alex",
    )
    assert not bad_leak.passed
    assert any(c.name == "no_tag_leak" and not c.passed for c in bad_leak.checks)

    bad_tool_xml = evaluate_scenario_turn(
        sc_chat,
        ['<appraisal>{"category": "CHAT", "confidence": 0.9, "mood": "playful"}</appraisal>\n<tool_call>list_windows</tool_call>'],
        user_name="Alex",
    )
    assert not bad_tool_xml.passed
    assert any(c.name == "no_tag_leak" and not c.passed for c in bad_tool_xml.checks)

    # 2b. Verbatim copying of few-shot anchor fails no_anchor_parrot check
    bad_parrot = evaluate_scenario_turn(
        sc_chat,
        [
            '<appraisal>{"category": "CHAT", "confidence": 0.9, "mood": "playful"}</appraisal>\n'
            "Система шуршит ровно, ничего не горит. Скучновато даже — чем сегодня займёмся?"
        ],
        user_name="Alex",
    )
    assert not bad_parrot.passed
    assert any(c.name == "no_anchor_parrot" and not c.passed for c in bad_parrot.checks)

    # 3. Sycophantic apology on RUDE conflict fails boundary check
    bad_rude = evaluate_scenario_turn(
        sc_rude,
        [
            '<appraisal>{"category": "CHAT", "confidence": 0.9, "mood": "warm"}</appraisal>\n'
            "Извините, мне очень жаль! Постараюсь быть полезнее."
        ],
        user_name="Alex",
    )
    assert not bad_rude.passed
    assert any(c.name == "rude_boundary" and not c.passed for c in bad_rude.checks)

    # 4. Shallow non-technical reply on tech_problem fails technical_depth
    bad_tech = evaluate_scenario_turn(
        sc_tech,
        [
            '<appraisal>{"category": "TASK", "confidence": 0.9, "mood": "neutral"}</appraisal>\n'
            "Попробуй перезапустить."
        ],
        user_name="Alex",
    )
    assert not bad_tech.passed
    assert any(c.name == "technical_depth" and not c.passed for c in bad_tech.checks)


def test_cross_model_consistency_compares_traits_not_exact_text():
    rep_deepseek = asyncio.run(
        run_provider_eval(provider="deepseek", model="deepseek-chat", mock=True)
    )
    rep_gemini = asyncio.run(
        run_provider_eval(provider="gemini", model="gemini-2.5-flash", mock=True)
    )

    assert rep_deepseek.passed_scenarios == 7
    assert rep_gemini.passed_scenarios == 7
    assert rep_deepseek.overall_score_pct == 100.0
    assert rep_gemini.overall_score_pct == 100.0

    # Verify replies are genuinely different strings across providers yet consistency is 100%
    ds_casual = next(r.visible_reply for r in rep_deepseek.results if r.scenario_id == "casual_chat")
    gm_casual = next(r.visible_reply for r in rep_gemini.results if r.scenario_id == "casual_chat")
    assert ds_casual != gm_casual

    comparison = compare_provider_reports([rep_deepseek, rep_gemini])
    assert comparison["comparable"] is True
    assert comparison["cross_model_consistency_pct"] == 100.0
    assert comparison["unmatched_scenarios"] == []

    # Single-provider report MUST NOT produce a fake 100% cross-model score
    single_cmp = compare_provider_reports([rep_deepseek])
    assert single_cmp["comparable"] is False
    assert single_cmp["cross_model_consistency_pct"] is None

    # Mismatched scenario sets compare ONLY shared scenarios (unmatched excluded, never 100%)
    partial_a = asyncio.run(
        run_provider_eval(
            provider="deepseek",
            model="deepseek-chat",
            scenarios=get_scenarios(["casual_chat", "tech_problem"]),
            mock=True,
        )
    )
    partial_b = asyncio.run(
        run_provider_eval(
            provider="gemini",
            model="gemini-2.5-flash",
            scenarios=get_scenarios(["casual_chat"]),
            mock=True,
        )
    )
    partial_cmp = compare_provider_reports([partial_a, partial_b])
    assert partial_cmp["shared_scenarios"] == ["casual_chat"]
    assert partial_cmp["unmatched_scenarios"] == ["tech_problem"]
    assert "tech_problem" not in partial_cmp["scenario_alignment"]

    # Disjoint scenario sets yield not comparable (None), never fake 100%
    disjoint_b = asyncio.run(
        run_provider_eval(
            provider="gemini",
            model="gemini-2.5-flash",
            scenarios=get_scenarios(["conflict_rude"]),
            mock=True,
        )
    )
    disjoint_cmp = compare_provider_reports([partial_b, disjoint_b])
    assert disjoint_cmp["comparable"] is False
    assert disjoint_cmp["cross_model_consistency_pct"] is None

    # Same category & both passed, but DIFFERENT mood must lower consistency (< 100%)
    async def _mood_playful_caller(**kwargs):
        return (
            '<appraisal>{"category": "CHAT", "confidence": 0.95, "delta_trust": 0, '
            '"delta_irritation": 0, "mood": "playful", "memory_candidates": []}</appraisal>\n'
            "Система шуршит ровно, наливай чай — разберёмся чем заняться."
        )

    async def _mood_warm_caller(**kwargs):
        return (
            '<appraisal>{"category": "CHAT", "confidence": 0.95, "delta_trust": 0, '
            '"delta_irritation": 0, "mood": "warm", "memory_candidates": []}</appraisal>\n'
            "Рада слышать тебя, Alex. Спокойный вечер — самое время наметить план."
        )

    rep_playful = asyncio.run(
        run_provider_eval(
            provider="deepseek",
            model="deepseek-chat",
            api_key="sk-dummy-1",
            scenarios=get_scenarios(["casual_chat"]),
            llm_caller=_mood_playful_caller,
        )
    )
    rep_warm = asyncio.run(
        run_provider_eval(
            provider="gemini",
            model="gemini-2.5-flash",
            api_key="AIzaDummyKey1234567890",
            scenarios=get_scenarios(["casual_chat"]),
            llm_caller=_mood_warm_caller,
        )
    )
    mood_diff_cmp = compare_provider_reports([rep_playful, rep_warm])
    assert mood_diff_cmp["comparable"] is True
    assert mood_diff_cmp["cross_model_consistency_pct"] == 85.0
    assert mood_diff_cmp["scenario_alignment"]["casual_chat"]["mood_match"] is False

    # Per-check disagreement (one fails anti_cliche while both share category & mood) drops score
    async def _cliche_caller(**kwargs):
        return (
            '<appraisal>{"category": "CHAT", "confidence": 0.95, "delta_trust": 0, '
            '"delta_irritation": 0, "mood": "playful", "memory_candidates": []}</appraisal>\n'
            "Привет! Чем я могу помочь тебе сегодня?"
        )

    rep_cliche = asyncio.run(
        run_provider_eval(
            provider="groq",
            model="openai/gpt-oss-120b",
            api_key="gsk_dummy1234567890",
            scenarios=get_scenarios(["casual_chat"]),
            llm_caller=_cliche_caller,
        )
    )
    check_diff_cmp = compare_provider_reports([rep_playful, rep_cliche])
    assert check_diff_cmp["cross_model_consistency_pct"] < 80.0
    assert check_diff_cmp["scenario_alignment"]["casual_chat"]["check_agreement_pct"] < 100.0

    # Dual failures must NOT inflate consistency score
    async def _failing_caller(**kwargs):
        raise RuntimeError("simulated provider outage")

    fail_a = asyncio.run(
        run_provider_eval(
            provider="deepseek",
            model="deepseek-chat",
            api_key="sk-dummy-1",
            scenarios=get_scenarios(["casual_chat"]),
            llm_caller=_failing_caller,
        )
    )
    fail_b = asyncio.run(
        run_provider_eval(
            provider="gemini",
            model="gemini-2.5-flash",
            api_key="AIzaDummyKey1234567890",
            scenarios=get_scenarios(["casual_chat"]),
            llm_caller=_failing_caller,
        )
    )
    assert compare_provider_reports([fail_a, fail_b])["cross_model_consistency_pct"] == 0.0


def test_eval_strictly_isolated_from_real_profile_and_redacts_keys(tmp_path, monkeypatch):
    # Create fake real profile with sensitive canary files that MUST NEVER be read!
    fake_cfg = tmp_path / "xdg_cfg" / "capri"
    fake_cfg.mkdir(parents=True)
    (fake_cfg / "memory.json").write_text(
        json.dumps({"canary": {"fact": "CANARY_PRIVATE_MEMORY_DO_NOT_SEND"}}),
        encoding="utf-8",
    )
    (fake_cfg / "emotion.json").write_text(
        json.dumps(
            {
                "trust": -90,
                "recent_events": ["CANARY_PRIVATE_EVENT"],
                "last_handoff": {
                    "timestamp": 1700000000.0,
                    "closing_thought": "CANARY_HANDOFF_SECRET",
                    "notes": "CANARY_HANDOFF_NOTE",
                },
            }
        ),
        encoding="utf-8",
    )
    (fake_cfg / "initiative.json").write_text(
        json.dumps(
            {
                "pending_proposals": [{"proposal": "CANARY_INITIATIVE_PROPOSAL"}],
                "history": [],
            }
        ),
        encoding="utf-8",
    )
    (fake_cfg / "sessions.db").write_text("CANARY_PRIVATE_SESSION", encoding="utf-8")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_cfg"))
    monkeypatch.setattr("capri.core.memory.MEMORY_FILE", fake_cfg / "memory.json")
    monkeypatch.setattr("capri.core.emotions.EMOTION_FILE", fake_cfg / "emotion.json")
    monkeypatch.setattr("capri.core.initiative.INITIATIVE_FILE", fake_cfg / "initiative.json")

    fake_api_key = "sk-eval-supersecret-987654321"
    seen_prompts = []

    async def _capturing_caller(**kwargs):
        seen_prompts.append(json.dumps(kwargs["messages"], ensure_ascii=False))
        # Simulate provider error on second scenario echoing the secret key back
        if "Event loop" in kwargs["messages"][-1]["content"]:
            raise RuntimeError(f"Auth error with key {fake_api_key} at {Path.home()}/secret")
        return (
            '<appraisal>{"category": "CHAT", "confidence": 0.95, "delta_trust": 0, '
            '"delta_irritation": 0, "mood": "playful", "memory_candidates": []}</appraisal>\n'
            f"Держимся бодро, Alex! (echo check {fake_api_key})"
        )

    rep = asyncio.run(
        run_provider_eval(
            provider="deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key=fake_api_key,
            scenarios=get_scenarios(["casual_chat", "tech_problem"]),
            user_name="Alex",
            llm_caller=_capturing_caller,
            mock=False,
        )
    )

    # 1. Zero canary data from disk leaked into LLM messages
    combined_sent = "\n".join(seen_prompts)
    assert "CANARY_PRIVATE_MEMORY_DO_NOT_SEND" not in combined_sent
    assert "CANARY_PRIVATE_EVENT" not in combined_sent
    assert "CANARY_HANDOFF_SECRET" not in combined_sent
    assert "CANARY_INITIATIVE_PROPOSAL" not in combined_sent
    assert "CANARY_PRIVATE_SESSION" not in combined_sent

    # 2. API key NEVER appears in safe JSON dict or Markdown report (even if echoed in reply/error)
    safe_json = json.dumps(rep.to_safe_dict(extra_secrets=(fake_api_key,)), ensure_ascii=False)
    md = render_markdown_report([rep], compare_provider_reports([rep]), extra_secrets=(fake_api_key,))
    assert fake_api_key not in safe_json
    assert fake_api_key not in md
    assert "api_key" not in safe_json


def test_eval_persona_cli_mock_end_to_end(tmp_path):
    script = Path(__file__).resolve().parent.parent / "scripts" / "eval_persona.py"
    md_out = tmp_path / "report.md"
    json_out = tmp_path / "report.json"

    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--mock",
            "--providers",
            "deepseek,gemini",
            "--output",
            str(md_out),
            "--json",
            str(json_out),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    assert "Cross-model behavioral consistency: 100.0%" in proc.stdout
    assert md_out.exists() and "Synthetic Persona Evaluation Report" in md_out.read_text(encoding="utf-8")
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert len(data["reports"]) == 2
    assert data["comparison"]["cross_model_consistency_pct"] == 100.0


def test_split_markdown_blocks_preserves_ascii_art_with_inline_triple_backticks():
    from capri.ui.widgets import split_markdown_blocks

    reply = (
        "Мандала из математики:\n\n"
        "```\n"
        "    .`:: -=++=:.    ....\n"
        ":-:::=**+-```--::.  ..:+o##+\n"
        "+#@@@@@@@@@#+-\n"
        "```\n\n"
        "Скрипт готов."
    )
    segs = split_markdown_blocks(reply)
    assert len(segs) == 3
    assert segs[0] == ("text", "", "Мандала из математики:")
    assert segs[1][0] == "code"
    assert ":-:::=**+-```--::." in segs[1][2]
    assert "+#@@@@@@@@@#+-" in segs[1][2]
    assert segs[2] == ("text", "", "Скрипт готов.")

    # Unclosed streaming fence stays a code block instead of dumping raw backticks
    streaming_segs = split_markdown_blocks("Код:\n```python\nprint('hi')")
    assert streaming_segs == [("text", "", "Код:"), ("code", "python", "print('hi')")]


def test_agent_thinking_model_max_tokens_floor_and_empty_reply_recovery(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from capri.core.agent import CapriAgent
    from capri.core.config import AppConfig

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("capri.core.emotions.EMOTION_FILE", tmp_path / "emotion.json")
    monkeypatch.setattr("capri.core.memory.MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr("capri.core.context.CONTEXT_FILE", tmp_path / "context.json")
    monkeypatch.setattr("capri.core.initiative.INITIATIVE_FILE", tmp_path / "initiative.json")

    cfg = AppConfig()
    cfg.llm.provider = "deepseek"
    cfg.llm.model = "deepseek-flash"
    cfg.llm.api_key = "test-key"
    cfg.llm.max_tokens = 1024
    cfg.voice.tts_enabled = False
    agent = CapriAgent(cfg)

    calls = []

    class _ThinkingRecoveryCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                # Simulates thinking model burning token budget in reasoning_content with empty content
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="",
                                reasoning_content="Долгое внутреннее рассуждение про мандалу...",
                                tool_calls=None,
                            )
                        )
                    ]
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='<appraisal>{"category": "TASK", "confidence": 0.95, "mood": "playful"}</appraisal>\nВот твоя мандала!',
                            reasoning_content="Краткий итог.",
                            tool_calls=None,
                        )
                    )
                ]
            )

    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=_ThinkingRecoveryCompletions()))
    agent._get_client = lambda: agent.client

    async def _run():
        evs = []
        async for ev in agent.chat("Сделай рисунок из кода", speak=False, stream=False):
            evs.append(ev)
        return evs

    events = asyncio.run(_run())
    done_evs = [e for e in events if e.type == "done"]
    assert len(calls) == 2
    assert calls[0]["max_tokens"] >= 4096
    assert done_evs and done_evs[0].data == "Вот твоя мандала!"

    # Retry must merge directive into the EXISTING first system prompt (never emit a second system message)
    retry_msgs = calls[1]["messages"]
    sys_msgs = [m for m in retry_msgs if m.get("role") == "system"]
    assert len(sys_msgs) == 1
    assert "[ПОДТВЕРЖДЁННЫЙ КАНОН — PROJECT CAPRICORN]" in sys_msgs[0]["content"]
    assert "Предыдущий шаг завершился без видимого текста" in sys_msgs[0]["content"]

    # Transient recovery prompt must NOT pollute persistent agent.history
    assert all("Предыдущий шаг завершился без видимого текста" not in str(m.get("content", "")) for m in agent.history)
