## Summary

- What changed and why?

## Verification

- [ ] `QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest tests -q -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning` passes
- [ ] `git diff --check` is clean
- [ ] No secrets, `.env` values, or personal `/home/<user>` paths introduced
- [ ] Optional extras degrade gracefully when omitted (core Arch/minimal startup preserved)
- [ ] Documentation updated (`README.md`, `README.ru.md`, `docs/ARCHITECTURE.md`, or `CHANGELOG.md` if user-facing)
