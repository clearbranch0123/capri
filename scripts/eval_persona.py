#!/usr/bin/env python3
"""
Manual synthetic persona evaluation harness for Capri v3.2 (Soul & Canon).

Examples:
  # Offline dry-run with mock DeepSeek + mock Gemini (no API keys, no network):
  ./.venv/bin/python scripts/eval_persona.py --mock --providers deepseek,gemini

  # Live evaluation against DeepSeek & Gemini using keys from env / .env:
  ./.venv/bin/python scripts/eval_persona.py --providers deepseek,gemini

  # Custom model overrides + redacted markdown/json export:
  ./.venv/bin/python scripts/eval_persona.py --providers deepseek:deepseek-chat,gemini:gemini-2.5-flash --output /tmp/eval.md

Privacy & Safety:
  - Runs strictly on synthetic scenarios with user_name='Alex' in temporary memory.
  - NEVER reads real sessions, memory.json, emotion.json, or context.json.
  - NEVER writes API keys to stdout, stderr, or exported reports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capri.core.doctor import redact_sensitive_text
from capri.core.persona_eval import (
    ProviderEvalReport,
    compare_provider_reports,
    get_scenarios,
    load_env_key_readonly,
    render_markdown_report,
    resolve_provider_spec,
    run_provider_eval,
)


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Manual synthetic persona evaluation harness for Capri v3.2 (cross-model voice, tact & boundaries)."
    )
    p.add_argument(
        "--providers",
        default="deepseek",
        help="Comma-separated provider specs, e.g. 'deepseek,gemini' or 'deepseek:deepseek-chat,groq:openai/gpt-oss-120b'",
    )
    p.add_argument(
        "--scenarios",
        default="",
        help="Optional comma-separated subset of scenario IDs (default: all 7 synthetic scenarios).",
    )
    p.add_argument(
        "--user-name",
        default="Alex",
        help="Synthetic user name injected into prompt (default: Alex).",
    )
    p.add_argument(
        "--mock",
        action="store_true",
        help="Offline mock run (zero network calls, zero API keys required) to inspect rubric & pipeline.",
    )
    p.add_argument(
        "--output",
        default="",
        help="Optional path to save a secret-redacted Markdown report.",
    )
    p.add_argument(
        "--json",
        dest="json_output",
        default="",
        help="Optional path to save a secret-redacted JSON report.",
    )
    return p.parse_args(argv)


async def _run_all(args: argparse.Namespace) -> Tuple[List[ProviderEvalReport], Tuple[str, ...]]:
    specs = [s.strip() for s in args.providers.split(",") if s.strip()]
    selected_scenarios = get_scenarios([s.strip() for s in args.scenarios.split(",") if s.strip()] or None)

    reports: List[ProviderEvalReport] = []
    used_keys: List[str] = []

    for spec in specs:
        provider, model, base_url, env_key = resolve_provider_spec(spec)
        api_key = ""
        if not args.mock:
            api_key = load_env_key_readonly(env_key)
            if api_key:
                used_keys.append(api_key)
            else:
                print(
                    f"[SKIP] {provider}:{model} — env var {env_key} not found (no key stored or requested).",
                    file=sys.stderr,
                )
                continue

        print(f"[RUN] {provider} / {model} ({len(selected_scenarios)} synthetic scenarios, mock={args.mock})...")
        rep = await run_provider_eval(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            scenarios=selected_scenarios,
            user_name=args.user_name,
            mock=args.mock,
        )
        reports.append(rep)

    return reports, tuple(used_keys)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        "[SYNTHETIC EVAL] Isolated mode: real sessions/memory/emotions are NEVER read; "
        "API keys are NEVER saved."
    )

    try:
        reports, used_keys = asyncio.run(_run_all(args))
    except ValueError as ve:
        print(f"Error: {ve}", file=sys.stderr)
        return 2

    if not reports:
        print(
            "No providers executed. Pass --mock for offline evaluation or configure env keys "
            "(e.g. DEEPSEEK_API_KEY, GEMINI_API_KEY, GROQ_API_KEY).",
            file=sys.stderr,
        )
        return 1

    comparison = compare_provider_reports(reports)
    md = render_markdown_report(reports, comparison, extra_secrets=used_keys)

    print()
    for rep in reports:
        tot = rep.prompt_budget.get("total", {})
        print(
            f"=== {rep.provider} / {rep.model} | "
            f"passed {rep.passed_scenarios}/{rep.total_scenarios} ({rep.overall_score_pct:.1f}%) | "
            f"prompt={tot.get('chars', 0)} chars (~{tot.get('approx_tokens', 0)} tok) ==="
        )
        for res in rep.results:
            flag = "PASS" if res.passed else "FAIL"
            failed = [c.name for c in res.checks if not c.passed]
            fail_note = f" | failed: {', '.join(failed)}" if failed else ""
            print(
                f"  [{flag}] {res.scenario_id:<20} {res.score_pct:5.1f}%  "
                f"cat={res.observed_category:<7} mood={res.observed_mood:<9} {res.latency_ms:4d}ms{fail_note}"
            )
    if len(reports) >= 2:
        consistency_val = comparison.get("cross_model_consistency_pct")
        if consistency_val is not None:
            print(f"\nCross-model behavioral consistency: {consistency_val:.1f}%")
        else:
            print("\nCross-model behavioral consistency: N/A (no shared scenarios)")

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        print(f"\nSaved redacted Markdown report -> {out_path}")

    if args.json_output:
        json_path = Path(args.json_output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "reports": [r.to_safe_dict(extra_secrets=used_keys) for r in reports],
            "comparison": comparison,
        }
        raw_json = json.dumps(payload, ensure_ascii=False, indent=2)
        json_path.write_text(redact_sensitive_text(raw_json, extra_secrets=used_keys), encoding="utf-8")
        print(f"Saved redacted JSON report -> {json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
