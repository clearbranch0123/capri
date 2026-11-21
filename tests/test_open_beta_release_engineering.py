"""
Open Beta Release Engineering, Installation, Upgrade/Recovery & Packaging Tests (Waves OB-2 & OB-3).

Verifies:
1. `XDG_CONFIG_HOME` resolution across Capri config & profile paths
2. Clean install in isolated `HOME` & `XDG_CONFIG_HOME` without Niri/Hyprland
3. Idempotent re-run of `setup.sh` preserving `.env` (0600) & `config.yaml`
4. `setup.sh --upgrade` creating pre-upgrade backup and preserving old profile
5. Interrupted/failed upgrade (`CAPRI_SIMULATE_UPGRADE_FAIL=1`) failing cleanly with 100% user data intact
6. `setup.sh --uninstall` preserving user profile vs `--uninstall --purge` removing it
7. Running `capri --version`, `capri --doctor`, `capri --bugreport` from `/tmp` (different cwd)
8. Full profile export -> wipe -> restore roundtrip
9. Fedora `dnf` diagnostic hint coverage
10. Required Open Beta repo files (`LICENSE`, `ASSETS_NOTICE.md`, `SECURITY.md`, GitHub CI, Dependabot, Bug Report form)
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from capri.core.backup import export_profile_backup, restore_profile_backup
from capri.core.config import ROOT_DIR, get_user_config_dir
from capri.core.doctor import _distro_install_hint
from capri.core.sessions import SessionManager


def _make_isolated_app_copy(tmp_path: Path) -> Path:
    """Create a lightweight clean checkout copy containing setup.sh, templates, and desktop file."""
    app_copy = tmp_path / "capri_app"
    app_copy.mkdir()
    for fname in ("setup.sh", ".env.example", "config.example.yaml", "pyproject.toml", "README.md"):
        src = ROOT_DIR / fname
        if src.exists():
            shutil.copy2(src, app_copy / fname)
    assets_dst = app_copy / "capri" / "assets"
    assets_dst.mkdir(parents=True)
    shutil.copy2(ROOT_DIR / "capri" / "assets" / "capri-cyberdeck.desktop", assets_dst / "capri-cyberdeck.desktop")
    return app_copy


def test_xdg_config_home_resolution(tmp_path, monkeypatch):
    custom_xdg = tmp_path / "custom_xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(custom_xdg))
    assert get_user_config_dir() == custom_xdg / "capri"


def test_setup_sh_clean_install_idempotency_upgrade_interrupted_and_uninstall(tmp_path):
    app_dir = _make_isolated_app_copy(tmp_path)
    iso_home = tmp_path / "iso_home"
    iso_cfg = tmp_path / "iso_xdg_config"
    iso_data = tmp_path / "iso_xdg_data"
    iso_home.mkdir()

    base_env = dict(os.environ)
    base_env.pop("NIRI_SOCKET", None)
    base_env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
    base_env["HOME"] = str(iso_home)
    base_env["XDG_CONFIG_HOME"] = str(iso_cfg)
    base_env["XDG_DATA_HOME"] = str(iso_data)
    base_env["PYTHON_BIN"] = sys.executable

    # 1. Help flag works cleanly
    r_help = subprocess.run(["bash", str(app_dir / "setup.sh"), "--help"], env=base_env, capture_output=True, text=True)
    assert r_help.returncode == 0
    assert "--upgrade" in r_help.stdout
    assert "--uninstall" in r_help.stdout

    # 2. Clean install without Niri into isolated HOME & XDG_CONFIG_HOME
    r1 = subprocess.run(["bash", str(app_dir / "setup.sh"), "--offline"], env=base_env, capture_output=True, text=True)
    assert r1.returncode == 0, f"setup.sh failed:\n{r1.stdout}\n{r1.stderr}"

    cfg_dir = iso_cfg / "capri"
    env_file = cfg_dir / ".env"
    cfg_file = cfg_dir / "config.yaml"
    desktop_file = iso_data / "applications" / "capri-cyberdeck.desktop"

    assert env_file.exists()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert cfg_file.exists()
    assert desktop_file.exists()
    assert str(app_dir) in desktop_file.read_text(encoding="utf-8")
    assert (app_dir / ".venv").is_dir()

    # Populate user profile data (simulating existing user on previous version)
    env_file.write_text("GROQ_API_KEY=gsk_user_keep_me_99999\n", encoding="utf-8")
    cfg_file.write_text("companion:\n  user_name: TesterD\nsafety:\n  tool_policy: ask\n", encoding="utf-8")
    (cfg_dir / "memory.json").write_text('{"entries": {"fav": {"fact": "arch"}}}', encoding="utf-8")

    # 3. Idempotent re-run must NOT overwrite user .env / config.yaml / memory.json
    r2 = subprocess.run(["bash", str(app_dir / "setup.sh"), "--offline"], env=base_env, capture_output=True, text=True)
    assert r2.returncode == 0, r2.stderr
    assert "gsk_user_keep_me_99999" in env_file.read_text(encoding="utf-8")
    assert "TesterD" in cfg_file.read_text(encoding="utf-8")

    # 4. Safe --upgrade creates pre-upgrade snapshot and preserves user files
    r_up = subprocess.run(
        ["bash", str(app_dir / "setup.sh"), "--upgrade", "--offline"],
        env=base_env,
        capture_output=True,
        text=True,
    )
    assert r_up.returncode == 0, r_up.stderr
    snaps = list((cfg_dir / "backups").glob("pre_upgrade_*"))
    assert len(snaps) >= 1
    assert (snaps[0] / "config.yaml").exists()
    assert (snaps[0] / "memory.json").exists()
    assert "gsk_user_keep_me_99999" in env_file.read_text(encoding="utf-8")

    # 5. Simulated interrupted/failed upgrade exits non-zero AND preserves 100% of user profile
    fail_env = dict(base_env)
    fail_env["CAPRI_SIMULATE_UPGRADE_FAIL"] = "1"
    r_fail = subprocess.run(
        ["bash", str(app_dir / "setup.sh"), "--upgrade", "--offline"],
        env=fail_env,
        capture_output=True,
        text=True,
    )
    assert r_fail.returncode != 0
    assert "NOT modified" in r_fail.stderr
    assert "gsk_user_keep_me_99999" in env_file.read_text(encoding="utf-8")
    assert "TesterD" in cfg_file.read_text(encoding="utf-8")
    assert '"arch"' in (cfg_dir / "memory.json").read_text(encoding="utf-8")

    # 6. --uninstall removes .venv & .desktop while PRESERVING user config/keys
    r_un = subprocess.run(["bash", str(app_dir / "setup.sh"), "--uninstall"], env=base_env, capture_output=True, text=True)
    assert r_un.returncode == 0
    assert not desktop_file.exists()
    assert not (app_dir / ".venv").exists()
    assert env_file.exists()
    assert cfg_file.exists()

    # 7. --uninstall --purge also removes user profile directory
    r_purge = subprocess.run(
        ["bash", str(app_dir / "setup.sh"), "--uninstall", "--purge"],
        env=base_env,
        capture_output=True,
        text=True,
    )
    assert r_purge.returncode == 0
    assert not cfg_dir.exists()


def test_cli_runs_cleanly_from_tmp_with_isolated_xdg_without_niri(tmp_path):
    iso_home = tmp_path / "cli_home"
    iso_xdg = tmp_path / "cli_xdg"
    iso_home.mkdir()
    iso_xdg.mkdir()

    env = dict(os.environ)
    env.pop("NIRI_SOCKET", None)
    env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
    env["HOME"] = str(iso_home)
    env["XDG_CONFIG_HOME"] = str(iso_xdg)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(ROOT_DIR)

    # Execute from /tmp to prove zero cwd assumptions
    res_ver = subprocess.run(
        [sys.executable, "-m", "capri.cli", "--version"],
        cwd="/tmp",
        env=env,
        capture_output=True,
        text=True,
    )
    from capri import __version__ as capri_version
    assert res_ver.returncode == 0
    assert capri_version in res_ver.stdout

    res_doc = subprocess.run(
        [sys.executable, "-m", "capri.cli", "--doctor"],
        cwd="/tmp",
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_doc.returncode == 0
    assert "SELF-DIAGNOSTICS" in res_doc.stdout

    bug_out = tmp_path / "tmp_bugreport.md"
    res_bug = subprocess.run(
        [sys.executable, "-m", "capri.cli", "--bugreport", str(bug_out)],
        cwd="/tmp",
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_bug.returncode == 0
    assert bug_out.exists()
    assert "CAPRI // BUG REPORT" in bug_out.read_text(encoding="utf-8")


def test_profile_export_wipe_restore_roundtrip_and_fedora_hints(tmp_path):
    profile_dir = tmp_path / "user_profile"
    profile_dir.mkdir()

    # Seed realistic profile state (SQLite session + memory + config + .env)
    sm = SessionManager(db_path=profile_dir / "sessions.db")
    sess = sm.create_session("Open Beta Test Chat")
    sm.add_turn(sess.id, "Привет Капри", "Привет, D! Все системы в норме.")
    (profile_dir / "memory.json").write_text('{"entries": {"os": {"fact": "Fedora"}}}', encoding="utf-8")
    (profile_dir / "config.yaml").write_text("companion:\n  user_name: D\n", encoding="utf-8")
    (profile_dir / ".env").write_text("GROQ_API_KEY=gsk_never_exported_12345\n", encoding="utf-8")

    # Export (must exclude .env secrets)
    zip_out = tmp_path / "capri_backup.zip"
    exp = export_profile_backup(output_path=zip_out, profile_dir=profile_dir)
    assert exp.archive_path.exists()
    assert ".env" not in exp.included_files

    # Wipe profile completely
    shutil.rmtree(profile_dir)
    profile_dir.mkdir()
    assert not (profile_dir / "sessions.db").exists()

    # Restore and verify full recovery
    rest = restore_profile_backup(zip_out, profile_dir=profile_dir)
    assert "sessions.db" in rest.restored_files
    assert "memory.json" in rest.restored_files
    sm2 = SessionManager(db_path=profile_dir / "sessions.db")
    sessions = sm2.list_sessions()
    assert any(s.title == "Open Beta Test Chat" for s in sessions)
    assert "Fedora" in (profile_dir / "memory.json").read_text(encoding="utf-8")

    # Fedora dnf hint coverage
    hint_fed = _distro_install_hint("fedora", "mpv pipewire-pulse", "mpv pipewire-utils")
    assert "sudo dnf install mpv pipewire-utils" == hint_fed


def test_open_beta_governance_and_github_files_present():
    required_files = (
        ROOT_DIR / "LICENSE",
        ROOT_DIR / "ASSETS_NOTICE.md",
        ROOT_DIR / "SECURITY.md",
        ROOT_DIR / "README.md",
        ROOT_DIR / "docs" / "screenshots" / "cyberdeck.png",
        ROOT_DIR / "docs" / "screenshots" / "relic.png",
        ROOT_DIR / ".github" / "workflows" / "ci.yml",
        ROOT_DIR / ".github" / "dependabot.yml",
        ROOT_DIR / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml",
    )
    for f in required_files:
        assert f.exists(), f"Missing required Open Beta file: {f}"

    assert "MIT License" in (ROOT_DIR / "LICENSE").read_text(encoding="utf-8")
    assert "PolyForm Noncommercial" in (ROOT_DIR / "ASSETS_NOTICE.md").read_text(encoding="utf-8")
    assert "Security Advisories" in (ROOT_DIR / "SECURITY.md").read_text(encoding="utf-8")
