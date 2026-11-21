# Security Policy

## Supported Versions

During the Open Beta phase, security fixes are applied to the latest `3.2.x` release on `main`:

| Version | Supported |
| ------- | --------- |
| `3.2.x` | Yes |
| `< 3.2.0` | No (please upgrade via `./setup.sh --upgrade`) |

## Reporting a Vulnerability (Private Disclosure)

**Please do NOT open a public GitHub issue for security vulnerabilities.**

To report a suspected vulnerability privately:

1. Use **GitHub Security Advisories** ("Report a vulnerability" button in the **Security** tab of this repository), **or**
2. Contact the maintainer privately if Security Advisories are unavailable.

When reporting, please include:
- A clear description of the vulnerability and potential impact;
- Minimal reproduction steps (commands, tool calls, or crafted inputs);
- Your Capri version (`capri --version`), OS/compositor, and tool policy mode (`SAFE` / `ASK` / `TRUSTED`);
- **Never include real API keys, `.env` contents, or personal chat logs.** If diagnostic context helps, attach the redacted output of `capri --bugreport`.

We aim to acknowledge reports within **72 hours** and release a fix + regression test before public disclosure.

## Security Model Overview

Capri is a local-first Linux desktop companion with optional tool execution, isolated web browsing, and MCP (Model Context Protocol) integrations.

### 1. Centralized Tool Policy (`ToolPolicyEngine`)
All tool executions pass through `ToolPolicyEngine` (fail-closed by default when no interactive approval handler is attached):
- **`SAFE` (strictly read-only):** Only explicit read-only tools (excluding sensitive paths like `.env`, `~/.ssh`, `~/.gnupg`, `~/.config/capri`, `/etc/shadow`) and allowlisted read-only shell commands (with zero pipes, redirections, globs, command substitutions, or dangerous `git` flags) are allowed.
- **`ASK` (default):** Read-only tools and non-memory Capri-internal companion state are allowed; direct long-term memory mutations (`remember`, `update_memory_entry`, `forget_memory`), OS-mutating actions, file writes, sensitive path reads, shell commands outside the allowlist, and all external `mcp__*` tools require explicit user confirmation.
- **`TRUSTED`:** Auto-allows non-destructive desktop controls (`focus_window`, `media_control`, `set_volume`); direct memory mutations, shell commands outside the read-only allowlist, file writes, process termination, sensitive file access, and all `mcp__*` tools **still** require explicit confirmation.
- **Forbidden patterns:** Destructive/privilege-escalation commands (e.g. `rm -rf /`, `mkfs`, `dd of=/dev/sd*`, `curl ... | sh`, `shutdown`, fork bombs) are unconditionally denied in all modes.

### 2. Isolated Browser & SSRF Protection
- `capri_browser_*` and `fetch_web_page` enforce strict `http://`/`https://` scheme validation, single-DNS IP pinning (`_SSRFGuardedNetworkBackend`), per-hop redirect checking, and an ephemeral `_SSRFGuardProxy` for headless Chromium (`use_js=True`) blocking loopback, RFC1918 private networks, link-local/cloud metadata (`169.254.169.254`), CGNAT, and obfuscated IP literals.
- Untrusted web pages and MCP outputs are wrapped in explicit untrusted delimiters (`<web_page_content untrusted="true">`, `<mcp_tool_output untrusted="true">`) with closing-tag neutralization to mitigate prompt-injection tag breakout.

### 3. Secrets, Memory Gate & Local Privacy
- API keys are stored locally in `~/.config/capri/.env` (or `$XDG_CONFIG_HOME/capri/.env`) with `0600` permissions and are scrubbed from `config.yaml` on save.
- LLM-proposed memory facts are staged in an isolated `pending_memory.json` (`0600`) queue and never enter `memory.json` or system prompts without explicit user approval. A dedicated secret/credential filter blocks passwords, API keys, tokens, PINs, and private keys at proposal, edit, approval, tool, and slash-command boundaries without logging secret values.
- `/bugreport` and runtime error surfaces redact API keys, Bearer tokens, JWTs, URL query/basic-auth credentials, MCP env secrets, and local `/home/<user>` paths, and never include `sessions.db`, `memory.json`, `pending_memory.json`, `emotion.json`, or `.env`.

### 4. Cloud Provider Data Boundary
- Persistent state (`sessions.db`, `memory.json`, `pending_memory.json`, `emotion.json`, `context.json`, and `.env`) is stored locally on disk and is never uploaded as a profile or archive.
- When you configure cloud providers, only the payload required for each active capability is transmitted to your selected API endpoint:
  - **Cloud LLM** (`DeepSeek`, `Groq`, `Gemini`, `SambaNova`, `OpenRouter`, `OpenAI`): system prompt, active conversation window, and user-confirmed memory facts (never unapproved `pending_memory.json` entries or `.env`).
  - **Cloud STT** (`Groq Whisper`): recorded audio snippet for speech-to-text transcription.
  - **Cloud TTS** (`Fish Audio`, `ElevenLabs`, `Edge`, `Gemini TTS`): assistant reply text for speech synthesis.
  - **Cloud Vision** (`Gemini Vision`): desktop screenshot or user-attached image.
- Using a local OpenAI-compatible server (e.g. Ollama / LM Studio) with local STT/TTS keeps chat and voice inference on-device; optional network features such as web search/fetch, the isolated browser, and external MCP servers are independent and remain governed by configuration and `ToolPolicyEngine`.
