"""
CAPRI // Pytest root configuration.
1. Forces deterministic offscreen Qt platform for all unit and GUI tests,
   preventing Wayland/X11 compositor interference and ensuring reproducible CI/local test runs.
2. Hermetically isolates user profile storage (`sessions.db`, `memory.json`, `emotion.json`,
   `context.json`, `initiative.json`, `config.yaml`, `.env`, backups, bugreports) into a
   per-test temporary directory so running tests NEVER pollutes the user's real chats or config.
"""

import os
from pathlib import Path
import pytest

# Force offscreen QPA platform before any PySide6 modules are imported
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.pop("QT_STYLE_OVERRIDE", None)


@pytest.fixture(autouse=True)
def _isolate_user_profile_and_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect all persistent Capri profile/config writes away from the real user profile."""
    profile_dir = tmp_path / "capri_test_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    import capri.core.attachments as attachments_mod
    import capri.core.backup as backup_mod
    import capri.core.config as config_mod
    import capri.core.context as context_mod
    import capri.core.doctor as doctor_mod
    import capri.core.emotions as emotions_mod
    import capri.core.initiative as initiative_mod
    import capri.core.memory as memory_mod
    import capri.core.sessions as sessions_mod
    import capri.tools.memory as memory_tools_mod

    test_cfg = profile_dir / "config.yaml"
    test_env = profile_dir / ".env"

    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", profile_dir / "sessions.db")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", profile_dir / "memory.json")
    monkeypatch.setattr(memory_tools_mod, "MEMORY_FILE", profile_dir / "memory.json")
    monkeypatch.setattr(emotions_mod, "EMOTION_FILE", profile_dir / "emotion.json")
    monkeypatch.setattr(context_mod, "CONTEXT_FILE", profile_dir / "context.json")
    monkeypatch.setattr(initiative_mod, "INITIATIVE_FILE", profile_dir / "initiative.json")

    monkeypatch.setattr(config_mod, "USER_CONFIG_DIR", profile_dir)
    monkeypatch.setattr(config_mod, "USER_CONFIG_FILE", test_cfg)
    monkeypatch.setattr(config_mod, "USER_ENV_FILE", test_env)
    monkeypatch.setattr(config_mod, "_resolve_writable_config_path", lambda: test_cfg)

    orig_update_env = config_mod.update_env_value
    orig_delete_env = config_mod.delete_env_value

    def _isolated_update_env(key: str, value: str, env_path=None):
        return orig_update_env(key, value, env_path=env_path or test_env)

    def _isolated_delete_env(key: str, env_path=None):
        return orig_delete_env(key, env_path=env_path or test_env)

    monkeypatch.setattr(config_mod, "update_env_value", _isolated_update_env)
    monkeypatch.setattr(config_mod, "delete_env_value", _isolated_delete_env)

    monkeypatch.setattr(attachments_mod, "DEFAULT_CLIPBOARD_CACHE_DIR", profile_dir / "clipboard_tmp")
    monkeypatch.setattr(backup_mod, "USER_CONFIG_DIR", profile_dir)
    monkeypatch.setattr(doctor_mod, "USER_CONFIG_DIR", profile_dir)
    monkeypatch.setattr(doctor_mod, "USER_CONFIG_FILE", test_cfg)
    monkeypatch.setattr(doctor_mod, "USER_ENV_FILE", test_env)

    # Isolate avatar IPC socket/pid and isolated browser profile dir
    try:
        import capri.ui.client as ui_client_mod
        import capri.ui.overlay as ui_overlay_mod
        monkeypatch.setattr(ui_client_mod, "SOCKET_PATH", str(profile_dir / "capri_avatar.sock"))
        monkeypatch.setattr(ui_overlay_mod, "SOCKET_PATH", str(profile_dir / "capri_avatar.sock"))
        monkeypatch.setattr(ui_overlay_mod, "PID_PATH", str(profile_dir / "capri_avatar.pid"))
    except Exception:
        pass
    try:
        import capri.tools.browser as browser_mod
        b_dir = profile_dir / "browser_profile"
        monkeypatch.setattr(browser_mod, "CAPRI_BROWSER_PROFILE_DIR", b_dir)
        browser_mod._shared_browser.profile_dir = b_dir
        browser_mod._shared_browser.current_url = ""
        browser_mod._shared_browser.current_title = ""
        browser_mod._shared_browser.current_links.clear()
        browser_mod._shared_browser.history.clear()
        browser_mod._shared_browser.cookies.clear()
    except Exception:
        pass
