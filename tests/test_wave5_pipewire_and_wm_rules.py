"""
Tests for Wave 5 (v3.0.0a2 — Living Presence 3.0 Stage 2):
- PipeWire AEC parser, idempotent enable/disable via pactl, config snippet generator
- Microphone calibration DSP (`recommend_barge_in_threshold`, `rms_to_meter_percent`) & input device resolution
- Niri KDL & Hyprland windowrulev2 rules generator, dry-run preview, timestamped backup, idempotent upsert
- CLI `--wm-rules`, `/doctor` PipeWire + WM checks, Cyberdeck `/wm-rules`, `/bargein aec`, Settings Tab 3
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
from PySide6.QtWidgets import QApplication

from capri.cli import cli_main
from capri.core.config import AppConfig
from capri.core.doctor import run_diagnostics
from capri.core.wm_rules import (
    check_wm_rules_installed,
    generate_hyprland_rules,
    generate_niri_rules,
    install_wm_rules,
    resolve_niri_config_target,
)
from capri.ui.cyberdeck import CapriCyberdeckWindow, CapriProviderSettingsDialog
from capri.voice.pipewire import (
    PIPEWIRE_AEC_SOURCE_NAME,
    CalibrationResult,
    compute_chunk_rms,
    disable_pipewire_aec,
    enable_pipewire_aec,
    generate_pipewire_aec_conf,
    parse_pactl_info,
    parse_pactl_sources,
    recommend_barge_in_threshold,
    resolve_sounddevice_input,
    rms_to_meter_percent,
)


def test_pipewire_parsers_and_calibration_dsp():
    info_raw = (
        "Server Name: PulseAudio (on PipeWire 1.6.10)\n"
        "Default Sink: alsa_output.usb-audio\n"
        "Default Source: easyeffects_source\n"
    )
    srv, sink, src, is_pw = parse_pactl_info(info_raw)
    assert is_pw is True
    assert "PipeWire" in srv
    assert sink == "alsa_output.usb-audio"
    assert src == "easyeffects_source"

    sources_raw = (
        "50\teasyeffects_sink.monitor\tPipeWire\tfloat32le 2ch 48000Hz\tSUSPENDED\n"
        "51\teasyeffects_source\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING\n"
        "93\talsa_input.usb-FIFINE\tPipeWire\ts16le 1ch 48000Hz\tSUSPENDED\n"
        f"102\t{PIPEWIRE_AEC_SOURCE_NAME}\tPipeWire\tfloat32le 1ch 48000Hz\tIDLE\n"
    )
    parsed = parse_pactl_sources(sources_raw)
    assert len(parsed) == 4
    assert parsed[0].is_monitor is True and parsed[0].is_aec is False
    assert parsed[3].is_aec is True and parsed[3].is_monitor is False

    # Calibration threshold math: sits safely above noise floor and below speech
    cal = recommend_barge_in_threshold(noise_rms=0.008, speech_rms=0.110)
    assert isinstance(cal, CalibrationResult)
    assert 0.025 <= cal.recommended_threshold <= 0.075
    assert cal.recommended_threshold > cal.noise_rms * 2.5
    assert cal.recommended_threshold < cal.speech_rms

    # Loud speaker room -> elevated protective threshold
    cal_loud = recommend_barge_in_threshold(noise_rms=0.040, speech_rms=0.042)
    assert cal_loud.recommended_threshold >= 0.080

    # VU meter & RMS helper
    silence = np.zeros(1600, dtype=np.float32)
    tone = (0.15 * np.sin(np.linspace(0, 20, 1600))).astype(np.float32)
    assert compute_chunk_rms(silence) == 0.0
    assert rms_to_meter_percent(0.0) == 0
    assert 40 <= rms_to_meter_percent(compute_chunk_rms(tone)) <= 100

    conf = generate_pipewire_aec_conf()
    assert "libpipewire-module-echo-cancel" in conf
    assert PIPEWIRE_AEC_SOURCE_NAME in conf


def test_pipewire_aec_enable_disable_and_device_resolution(monkeypatch):
    from capri.voice import pipewire as pw_mod

    state = {"loaded": False}

    def fake_run_pactl(args, timeout=1.8):
        if args == ["info"]:
            return 0, "Server Name: PulseAudio (on PipeWire 1.6.10)\nDefault Source: mic0\n", ""
        if args == ["list", "short", "sources"]:
            base = "51\tmic0\tPipeWire\ts16le 1ch 48000Hz\tIDLE\n"
            if state["loaded"]:
                base += f"99\t{PIPEWIRE_AEC_SOURCE_NAME}\tPipeWire\tfloat32le 1ch 48000Hz\tIDLE\n"
            return 0, base, ""
        if args and args[0] == "load-module" and "module-echo-cancel" in args:
            state["loaded"] = True
            return 0, "42\n", ""
        if args == ["list", "short", "modules"]:
            if state["loaded"]:
                return 0, f"42\tmodule-echo-cancel\taec_method=webrtc source_name={PIPEWIRE_AEC_SOURCE_NAME}\n", ""
            return 0, "", ""
        if args == ["unload-module", "42"]:
            state["loaded"] = False
            return 0, "", ""
        return 1, "", "unsupported"

    monkeypatch.setattr(pw_mod, "_run_pactl", fake_run_pactl)

    ok1, msg1 = enable_pipewire_aec()
    assert ok1 is True and PIPEWIRE_AEC_SOURCE_NAME in msg1

    # Second call is idempotent (reuses existing AEC source)
    ok2, msg2 = enable_pipewire_aec()
    assert ok2 is True and "уже активен" in msg2

    ok3, msg3 = disable_pipewire_aec()
    assert ok3 is True and state["loaded"] is False

    # Sounddevice resolution matches substring & falls back to None cleanly
    fake_devs = [
        {"name": "Built-in Audio", "max_input_channels": 2},
        {"name": "FIFINE USB Microphone", "max_input_channels": 1},
        {"name": "HDMI Output Only", "max_input_channels": 0},
    ]
    with patch("sounddevice.query_devices", return_value=fake_devs):
        assert resolve_sounddevice_input("") is None
        assert resolve_sounddevice_input("fifine") == 1
        assert resolve_sounddevice_input("nonexistent_mic") is None


def test_disable_pipewire_aec_never_unloads_foreign_echo_cancel_module(monkeypatch):
    """Regression guard: `disable_pipewire_aec()` must NEVER unload another app's module-echo-cancel."""
    from capri.voice import pipewire as pw_mod

    pw_mod._CAPRI_OWNED_AEC_MODULE_IDS.clear()
    unloaded_ids: list[str] = []
    modules_table = [
        "7\tmodule-echo-cancel\taec_method=webrtc source_name=other_app_aec",
        "8\tmodule-echo-cancel\t",
        f"42\tmodule-echo-cancel\taec_method=webrtc source_name={PIPEWIRE_AEC_SOURCE_NAME}",
    ]

    def fake_run_pactl(args, timeout=1.8):
        if args == ["list", "short", "modules"]:
            return 0, "\n".join(modules_table) + "\n", ""
        if args and args[0] == "unload-module":
            mid = args[1]
            unloaded_ids.append(mid)
            modules_table[:] = [ln for ln in modules_table if not ln.startswith(f"{mid}\t")]
            return 0, "", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(pw_mod, "_run_pactl", fake_run_pactl)

    # First call: unloads ONLY Capri's module 42, leaving foreign 7 and 8 untouched
    ok1, msg1 = disable_pipewire_aec()
    assert ok1 is True
    assert unloaded_ids == ["42"]
    assert any(ln.startswith("7\t") for ln in modules_table)
    assert any(ln.startswith("8\t") for ln in modules_table)

    # Second call with only foreign AEC modules present: unloads NOTHING and explains clearly
    ok2, msg2 = disable_pipewire_aec()
    assert ok2 is False
    assert unloaded_ids == ["42"]
    assert "сторонний" in msg2.lower() or "не capri" in msg2.lower()


def test_wm_rules_generator_dry_run_backup_and_idempotent_install(tmp_path):
    # 1. Niri modular config.d/90-user-extra.kdl resolution + KDL comment syntax
    niri_dir = tmp_path / "niri"
    (niri_dir / "config.d").mkdir(parents=True)
    (niri_dir / "config.kdl").write_text('include "config.d/90-user-extra.kdl"\n', encoding="utf-8")
    extra_kdl = niri_dir / "config.d" / "90-user-extra.kdl"
    extra_kdl.write_text("// existing user rules\n", encoding="utf-8")

    assert resolve_niri_config_target(niri_dir) == extra_kdl

    niri_snip = generate_niri_rules(include_hotkey=True)
    assert "// >>> CAPRI-WM-RULES BEGIN >>>" in niri_snip
    assert 'match app-id=r#"^capri-avatar$"#' in niri_snip
    assert 'spawn "capri-deck" "--toggle"' in niri_snip

    # Dry-run does not modify file
    dry = install_wm_rules("niri", config_dir=niri_dir, dry_run=True)
    assert dry.applied is False
    assert extra_kdl.read_text(encoding="utf-8") == "// existing user rules\n"

    # First apply creates backup and appends fenced block
    res1 = install_wm_rules("niri", config_dir=niri_dir, dry_run=False)
    assert res1.applied is True
    assert res1.backup_path is not None and res1.backup_path.exists()
    content1 = extra_kdl.read_text(encoding="utf-8")
    assert "// existing user rules" in content1
    assert content1.count("// >>> CAPRI-WM-RULES BEGIN >>>") == 1

    installed, found = check_wm_rules_installed("niri", config_dir=niri_dir)
    assert installed is True and found == extra_kdl

    # Re-applying updates the same fenced block idempotently (no duplicate rules!)
    res2 = install_wm_rules("niri", config_dir=niri_dir, dry_run=False, include_hotkey=False)
    assert res2.updated_existing_block is True
    content2 = extra_kdl.read_text(encoding="utf-8")
    assert content2.count("// >>> CAPRI-WM-RULES BEGIN >>>") == 1
    assert "capri-deck" not in content2

    # 2. Hyprland windowrulev2 rules + idempotent apply
    hypr_dir = tmp_path / "hypr"
    hypr_dir.mkdir()
    hypr_conf = hypr_dir / "hyprland.conf"
    hypr_conf.write_text("# user hypr config\n", encoding="utf-8")

    hypr_snip = generate_hyprland_rules(include_hotkey=True)
    assert "windowrulev2 = float, class:^(capri-avatar)$" in hypr_snip
    assert "windowrulev2 = pin, class:^(capri-avatar)$" in hypr_snip
    assert "bind = SUPER SHIFT, C, exec, capri-deck --toggle" in hypr_snip

    h_res = install_wm_rules("hyprland", config_dir=hypr_dir, dry_run=False)
    assert h_res.applied is True and h_res.target_path == hypr_conf
    h_installed, h_found = check_wm_rules_installed("hyprland", config_dir=hypr_dir)
    assert h_installed is True and h_found == hypr_conf


def test_cli_wm_rules_doctor_checks_and_cyberdeck_voice_settings_ui(capsys):
    # CLI --wm-rules dry-run output
    rc = cli_main(["--wm-rules", "hyprland"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "windowrulev2 = float, class:^(capri-avatar)$" in out
    assert "dry-run" in out

    # /doctor includes PipeWire / AEC and Правила WM checks
    cfg = AppConfig()
    items = run_diagnostics(cfg)
    names = {i.name for i in items}
    assert "PipeWire / AEC" in names
    assert "Правила WM" in names

    # Cyberdeck Settings Tab 3 ("Голос и аудио") + calibration UI & persistence
    app = QApplication.instance() or QApplication([])
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False
    cfg.voice.pipewire_aec_enabled = False
    cfg.voice.barge_in_rms_threshold = 0.032

    with patch("capri.ui.cyberdeck.save_config"), patch("capri.ui.cyberdeck.update_env_value"):
        dlg = CapriProviderSettingsDialog(cfg)
        assert dlg.tabs.count() == 4
        assert dlg.chk_pipewire_aec.isChecked() is False
        dlg.chk_pipewire_aec.setChecked(True)
        dlg._on_calibration_finished(CalibrationResult(noise_rms=0.009, speech_rms=0.11, recommended_threshold=0.056))
        assert dlg.slider_barge_threshold.value() == 56
        assert "0.056" in dlg.lbl_barge_threshold_val.text()
        dlg._apply_and_save()
        assert cfg.voice.pipewire_aec_enabled is True
        assert cfg.voice.barge_in_rms_threshold == 0.056
        dlg.close()

    # Slash commands /wm-rules and /bargein aec
    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True), \
         patch("capri.ui.cyberdeck.save_config"), \
         patch("capri.voice.pipewire.enable_pipewire_aec", return_value=(True, "PipeWire AEC OK")):
        deck = CapriCyberdeckWindow(config=cfg)
        deck._handle_command("/wm-rules niri")
        deck._handle_command("/bargein aec on")
        assert deck.config.voice.pipewire_aec_enabled is True
        deck.close()
