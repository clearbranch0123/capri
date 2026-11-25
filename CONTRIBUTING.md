# Contributing to Capri

Thanks for your interest in improving **Capri AI Companion for Linux**!

## Development Setup

```bash
git clone https://github.com/clearbranch0123/capri.git
cd capri
./setup.sh
```

Run the full automated test suite (offscreen Qt, strict warning checks):

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest tests -q \
  -W error::ResourceWarning \
  -W error::pytest.PytestUnraisableExceptionWarning
```

Verify formatting and release hygiene before opening a pull request:

```bash
git diff --check
```

## Guidelines

1. **Architecture & boundaries:** Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) before making structural changes to IPC, `ToolPolicyEngine`, or MCP subprocess handling.
2. **Security & privacy first:**
   - Never log or persist API keys outside `$XDG_CONFIG_HOME/capri/.env` (`0600`).
   - Keep `ToolPolicyEngine` fail-closed (`SAFE / ASK / TRUSTED`) and add regression tests for any path, shell, browser, or MCP boundary change.
   - Never include personal `.env`, `config.yaml`, chat logs, or `/home/<user>` paths in commits or tests.
3. **Optional dependencies:** Core entrypoints (`capri`, `capri-deck`, `capri-avatar`) must import cleanly even when optional extras (`voice`, `local-stt`, `vision`, `web`) are not installed.
4. **Licensing:** Source code contributions are licensed under the **MIT License** ([`LICENSE`](LICENSE)). Visual and audio assets under `capri/assets/` are governed separately — see [`ASSETS_NOTICE.md`](ASSETS_NOTICE.md).

## Reporting Bugs & Security Issues

- Use the **Bug Report** issue template with redacted diagnostics from `./run.sh --bugreport`.
- Report security vulnerabilities privately following [`SECURITY.md`](SECURITY.md).
