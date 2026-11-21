"""
Closed Beta Stage 2 Stability Regression Tests:
- /doctor self-diagnostics (never leaks secrets, detects missing API keys, SQLite integrity)
- Versioned profile backup & restore (.capri-backup.zip): SQLite online snapshot, secret-free YAML,
  SHA-256 tamper rejection, zip-slip path traversal rejection, schema mismatch, pre-restore snapshot
- First-run setup wizard (saves user_name, policy, provider; API key only to .env)
- Dynamic user_name propagation in WelcomeCanvas & MemoryWorkbenchDialog
- Graceful non-Niri degradation in desktop tools and LocomotionController observer
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from PySide6.QtWidgets import QApplication

from capri.core.backup import (
    BACKUP_SCHEMA_VERSION,
    export_profile_backup,
    restore_profile_backup,
)
from capri.core.config import AppConfig
from capri.core.doctor import format_diagnostics_markdown, generate_bug_report, run_diagnostics
from capri.core.prompts import get_system_prompt
from capri.core.sessions import SessionManager
from capri.tools.desktop import focus_window, list_windows, niri_action
from capri.ui.cyberdeck import (
    CapriCyberdeckWindow,
    CapriFirstRunWizard,
    CapriMemoryWorkbenchDialog,
    CapriProviderSettingsDialog,
    ChatMessageCard,
)
from capri.ui.locomotion import DesktopObserver


def test_doctor_diagnostics_report_and_secret_redaction(tmp_path):
    db_path = tmp_path / "sessions.db"
    sm = SessionManager(db_path=db_path)
    sm.create_session("Diag session")

    cfg = AppConfig()
    cfg.llm.provider = "deepseek"
    cfg.llm.model = "deepseek-flash"
    cfg.llm.api_key = "sk-ultra-secret-must-never-appear-in-doctor"

    items = run_diagnostics(cfg, sessions_db_path=db_path)
    md = format_diagnostics_markdown(items)

    assert "CAPRI // SELF-DIAGNOSTICS" in md
    assert "sk-ultra-secret-must-never-appear-in-doctor" not in md
    by_name = {i.name: i for i in items}
    assert by_name["Python"].status == "OK"
    assert by_name["LLM-ядро"].status == "OK"
    assert by_name["Хранилище профиля"].status == "OK"
    assert by_name["Политика инструментов"].status == "OK"

    # Missing key produces FAIL
    cfg_no_key = AppConfig()
    cfg_no_key.llm.api_key = ""
    items_no_key = {i.name: i for i in run_diagnostics(cfg_no_key, sessions_db_path=db_path)}
    assert items_no_key["LLM-ядро"].status == "FAIL"


def test_profile_backup_and_restore_roundtrip_and_security(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    # Populate realistic profile state: SQLite DB + JSON state + config with secret key
    sm = SessionManager(db_path=profile_dir / "sessions.db")
    s1 = sm.create_session("Бета-сессия 1")
    sm.add_turn(s1.id, user_text="Привет", assistant_text="На связи!")
    (profile_dir / "memory.json").write_text(json.dumps({"entries": {"fav": {"fact": "Arch Linux"}}}), encoding="utf-8")
    (profile_dir / "emotion.json").write_text(json.dumps({"trust": 55, "irritation": 0}), encoding="utf-8")
    (profile_dir / "context.json").write_text(json.dumps({"active_project": "capri"}), encoding="utf-8")
    (profile_dir / "config.yaml").write_text(
        yaml.safe_dump({"llm": {"provider": "deepseek", "api_key": "sk-leaked-in-yaml"}}),
        encoding="utf-8",
    )

    archive = tmp_path / "backup.capri-backup.zip"
    exp = export_profile_backup(output_path=archive, profile_dir=profile_dir)
    assert exp.schema_version == BACKUP_SCHEMA_VERSION
    assert archive.exists()
    assert "sessions.db" in exp.included_files
    assert "memory.json" in exp.included_files
    assert "config.yaml" in exp.included_files

    # Verify secret was scrubbed in exported archive
    with zipfile.ZipFile(archive, "r") as zf:
        exported_cfg = yaml.safe_load(zf.read("config.yaml").decode("utf-8"))
        assert exported_cfg["llm"]["api_key"] == ""
        assert ".env" not in zf.namelist()

    # Restore into a second directory with existing data -> creates pre-restore safety snapshot
    restore_dir = tmp_path / "restored"
    restore_dir.mkdir()
    (restore_dir / "memory.json").write_text(json.dumps({"entries": {"old": {"fact": "Old"}}}), encoding="utf-8")

    res = restore_profile_backup(archive, profile_dir=restore_dir, create_pre_restore_snapshot=True)
    assert res.schema_version == BACKUP_SCHEMA_VERSION
    assert res.pre_restore_snapshot is not None and res.pre_restore_snapshot.exists()
    assert "sessions.db" in res.restored_files

    restored_sm = SessionManager(db_path=restore_dir / "sessions.db")
    assert any(s.title == "Бета-сессия 1" for s in restored_sm.list_sessions())
    assert json.loads((restore_dir / "memory.json").read_text(encoding="utf-8"))["entries"]["fav"]["fact"] == "Arch Linux"

    # Tampered SHA-256 archive MUST be rejected
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive, "r") as zin, zipfile.ZipFile(tampered, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "memory.json":
                data = b'{"entries": {"hacked": true}}'
            zout.writestr(item, data)
    with pytest.raises(ValueError, match="SHA-256"):
        restore_profile_backup(tampered, profile_dir=restore_dir)

    # Zip-slip path traversal archive MUST be rejected
    zipslip = tmp_path / "zipslip.zip"
    with zipfile.ZipFile(zipslip, "w") as zout:
        zout.writestr("../evil.txt", b"pwn")
    with pytest.raises(ValueError, match="Небезопасный путь"):
        restore_profile_backup(zipslip, profile_dir=restore_dir)


def test_first_run_wizard_and_dynamic_user_name(tmp_path):
    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    cfg.companion.user_name = "Alex"
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.save_config") as mock_save, \
         patch("capri.ui.cyberdeck.update_env_value") as mock_env:
        wiz = CapriFirstRunWizard(cfg)
        wiz.edit_user_name.setText("V")
        wiz.edit_api_key.setText("sk-wizard-key")
        wiz.combo_policy.setCurrentIndex(1)  # safe
        wiz._apply()

        assert cfg.companion.user_name == "V"
        assert cfg.safety.tool_policy == "safe"
        mock_save.assert_called_once()
        mock_env.assert_any_call("LLM_PROVIDER", cfg.llm.provider)
        wiz.close()

        # Provider & model must be written to .env even when API key field is left empty
        mock_env.reset_mock()
        wiz_no_key = CapriFirstRunWizard(cfg)
        wiz_no_key.combo_provider.setCurrentIndex(1)  # groq
        wiz_no_key.edit_api_key.setText("")
        wiz_no_key._apply()
        mock_env.assert_any_call("LLM_PROVIDER", "groq")
        mock_env.assert_any_call("LLM_MODEL", cfg.llm.model)
        wiz_no_key.close()

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck._refresh_welcome_canvas(context="startup")
        mem_btn = next(b for b in deck.welcome_canvas.suggestion_buttons if b.objectName() == "welcomeSuggestion_memory")
        assert "о V" in mem_btn.text()

        wb = CapriMemoryWorkbenchDialog(deck)
        assert wb.table.horizontalHeaderItem(1).text() == "Факт о V"
        wb.close()

        # /doctor command runs cleanly inside Cyberdeck
        deck._handle_command("/doctor")
        # /profile export + /profile restore reloads initiative_engine too
        out_zip = tmp_path / "cmd_backup.zip"
        deck._handle_command(f"/profile export {out_zip}")
        assert out_zip.exists()
        with patch.object(deck.agent.initiative_engine, "load") as mock_init_load, \
             patch("capri.core.backup.restore_profile_backup") as mock_restore:
            from capri.core.backup import RestoreResult
            mock_restore.return_value = RestoreResult(restored_files=["initiative.json"], pre_restore_snapshot=None, schema_version=1)
            deck._handle_command(f"/profile restore {out_zip}")
            mock_init_load.assert_called_once()
        deck.close()


@pytest.mark.asyncio
async def test_graceful_non_niri_degradation(monkeypatch):
    monkeypatch.delenv("NIRI_SOCKET", raising=False)
    with patch("shutil.which", return_value=None):
        res_list = await list_windows()
        assert "Niri не обнаружен" in res_list

        res_focus = await focus_window("firefox")
        assert "Niri не обнаружен" in res_focus

        res_act = await niri_action("toggle-overview")
        assert "Niri не обнаружен" in res_act

        # DesktopObserver exits cleanly without spawning subprocesses when Niri is unavailable
        obs = DesktopObserver()
        obs._event_stream_loop()
        assert obs._running is False


def test_tabbed_settings_dialog_and_dynamic_prompts():
    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    cfg.llm.provider = "deepseek"
    cfg.llm.model = "deepseek-flash"
    cfg.llm.api_key = "sk-keep-existing-key"
    cfg.companion.user_name = "Mira"
    cfg.safety.tool_policy = "ask"
    cfg.voice.tts_enabled = True
    cfg.ui.avatar_enabled = True
    cfg.ui.reduced_motion = False

    # Dynamic prompt uses configured user_name instead of hardcoded "D"
    prompt = get_system_prompt(cfg)
    assert "Mira" in prompt
    assert "которого называешь D" not in prompt

    with patch("capri.ui.cyberdeck.save_config") as mock_save, \
         patch("capri.ui.cyberdeck.update_env_value") as mock_env:
        dlg = CapriProviderSettingsDialog(cfg)
        assert dlg.tabs.count() == 4
        assert dlg.edit_user_name.text() == "Mira"
        idx_trusted = dlg.combo_policy.findData("trusted")
        assert idx_trusted >= 0
        trusted_label = dlg.combo_policy.itemText(idx_trusted).lower()
        assert "десктоп" in trusted_label and "подтвержден" in trusted_label

        # Leave API key blank -> existing key must not be overwritten in config or .env
        dlg.edit_key.setText("")
        dlg.edit_user_name.setText("Nova")
        idx_safe = dlg.combo_policy.findData("safe")
        assert idx_safe >= 0
        dlg.combo_policy.setCurrentIndex(idx_safe)
        dlg.chk_tts_enabled.setChecked(False)
        dlg.chk_avatar_enabled.setChecked(False)
        dlg.chk_reduced_motion.setChecked(True)
        dlg._apply_and_save()

        assert cfg.llm.api_key == "sk-keep-existing-key"
        assert cfg.companion.user_name == "Nova"
        assert cfg.safety.tool_policy == "safe"
        assert cfg.voice.tts_enabled is False
        assert cfg.ui.avatar_enabled is False
        assert cfg.ui.reduced_motion is True
        mock_save.assert_called_once()
        mock_env.assert_any_call("LLM_PROVIDER", "deepseek")
        mock_env.assert_any_call("LLM_MODEL", "deepseek-flash")
        for call in mock_env.call_args_list:
            assert call.args[0] != "DEEPSEEK_API_KEY"
        dlg.close()

    updated_prompt = get_system_prompt(cfg)
    assert "Nova" in updated_prompt
    assert "Mira" not in updated_prompt

    # Custom user names must be classified as user bubbles (not system cards)
    user_card = ChatMessageCard("Nova", "Проверка пользовательской карточки")
    sys_card = ChatMessageCard("Система", "Системное уведомление")
    assert user_card.is_user is True
    assert sys_card.is_user is False
    user_card.close()
    sys_card.close()


def test_bugreport_redacts_secrets_and_paths_and_excludes_chat_history(tmp_path, monkeypatch):
    # Create a real sessions DB with private chat content
    db_path = tmp_path / "sessions.db"
    sm = SessionManager(db_path=db_path)
    s1 = sm.create_session("Личный диалог")
    sm.add_turn(s1.id, user_text="СУПЕР_ПРИВАТНОЕ_СООБЩЕНИЕ_ЮЗЕРА", assistant_text="СЕКРЕТНЫЙ_ОТВЕТ")

    # Create a runtime log containing leaked API keys and local home path
    fake_log = tmp_path / "capri.log"
    fake_log.write_text(
        f"2026-11-20 INFO Startup from {Path.home()}/Projects/capri\n"
        "2026-11-20 ERROR auth failed header=Bearer eyJhbGciOiJIUzI1NiJ9.secret\n"
        "2026-11-20 WARN DEEPSEEK_API_KEY=sk-topsecretvalue1234567890\n"
        "2026-11-20 WARN groq=gsk_supersecretgroq987654321 gemini=AIzaSyFakeKey1234567890123456\n",
        encoding="utf-8",
    )

    cfg = AppConfig()
    cfg.llm.provider = "deepseek"
    cfg.llm.model = "deepseek-flash"
    cfg.llm.api_key = "sk-my-personal-key-9999999"
    cfg.companion.user_name = "SecretTesterName"
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    out_report = tmp_path / "report.md"
    res = generate_bug_report(
        cfg,
        output_path=out_report,
        sessions_db_path=db_path,
        log_paths=[fake_log],
        max_log_lines=50,
    )
    assert res.report_path == out_report
    assert out_report.exists()
    assert res.log_lines_included == 4

    text = out_report.read_text(encoding="utf-8")
    assert "CAPRI // BUG REPORT" in text
    assert "CAPRI // SELF-DIAGNOSTICS" in text
    assert "sk-my-personal-key-9999999" not in text
    assert "sk-topsecretvalue1234567890" not in text
    assert "gsk_supersecretgroq987654321" not in text
    assert "AIzaSyFakeKey1234567890123456" not in text
    assert "eyJhbGciOiJIUzI1NiJ9.secret" not in text
    assert "SecretTesterName" not in text
    assert str(Path.home()) not in text
    assert "СУПЕР_ПРИВАТНОЕ_СООБЩЕНИЕ_ЮЗЕРА" not in text
    assert "СЕКРЕТНЫЙ_ОТВЕТ" not in text

    # Verify local key storage defaults to ~/.config/capri/.env when no project-local .env exists
    import capri.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "ROOT_DIR", tmp_path / "empty_install")
    monkeypatch.setattr(cfg_mod, "USER_CONFIG_DIR", tmp_path / "user_cfg")
    monkeypatch.setattr(cfg_mod, "USER_ENV_FILE", tmp_path / "user_cfg" / ".env")
    assert cfg_mod._resolve_writable_env_path() == tmp_path / "user_cfg" / ".env"

    # Also verify /bugreport command inside Cyberdeck
    app = QApplication.instance() or QApplication([])
    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        cmd_report = tmp_path / "deck_bugreport.md"
        deck._handle_command(f"/bugreport {cmd_report}")
        assert cmd_report.exists()
        assert "sk-my-personal-key-9999999" not in cmd_report.read_text(encoding="utf-8")
        deck.close()
