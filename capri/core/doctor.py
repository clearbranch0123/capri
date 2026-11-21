"""
Capri AI Companion — Self-Diagnostics (/doctor) for Closed Beta.
Inspects runtime environment, configuration hygiene (without ever printing secrets),
SQLite integrity, audio stack, optional Niri compositor, and tool policy status.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from .. import __version__
from .config import AppConfig, ROOT_DIR, USER_CONFIG_DIR, USER_CONFIG_FILE, USER_ENV_FILE
from .policy import VALID_POLICY_MODES

CheckStatus = Literal["OK", "WARN", "FAIL"]


@dataclass
class DiagnosticItem:
    name: str
    status: CheckStatus
    detail: str


def _check_python() -> DiagnosticItem:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 10)
    return DiagnosticItem(
        "Python",
        "OK" if ok else "FAIL",
        f"{v.major}.{v.minor}.{v.micro} ({platform.system()} {platform.machine()})",
    )


def _check_llm(config: AppConfig) -> DiagnosticItem:
    prov = (config.llm.provider or "unknown").upper()
    model = config.llm.model or "(не указана)"
    has_key = bool((config.llm.api_key or "").strip())
    if has_key:
        return DiagnosticItem("LLM-ядро", "OK", f"{prov} / {model} (API-ключ настроен)")
    return DiagnosticItem(
        "LLM-ядро",
        "FAIL",
        f"{prov} / {model} — API-ключ отсутствует (запусти /setup или укажи ключ в .env)",
    )


def _check_config_security() -> DiagnosticItem:
    leaks = []
    for candidate in (ROOT_DIR / "config.yaml", USER_CONFIG_FILE):
        if not candidate.exists():
            continue
        try:
            import yaml
            data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            secret_paths = [
                ("llm", "api_key"),
                ("vision", "gemini_api_key"),
                ("voice", "fish_audio_api_key"),
                ("voice", "elevenlabs_api_key"),
                ("voice", "gemini_api_key"),
            ]
            for sec, key in secret_paths:
                val = (data.get(sec) or {}).get(key)
                if isinstance(val, str) and val.strip():
                    leaks.append(f"{candidate.name}:{sec}.{key}")
        except Exception:
            pass
    if leaks:
        return DiagnosticItem(
            "Безопасность конфига",
            "WARN",
            f"Найдены непустые поля ключей в YAML ({', '.join(leaks)}). Пересохрани настройки, чтобы очистить.",
        )
    env_present = (ROOT_DIR / ".env").exists() or USER_ENV_FILE.exists()
    return DiagnosticItem(
        "Безопасность конфига",
        "OK",
        f"YAML без секретов; файл .env {'найден' if env_present else 'не создан (используются переменные окружения)'}",
    )


def _check_tool_policy(config: AppConfig) -> DiagnosticItem:
    safety = getattr(config, "safety", None)
    mode = getattr(safety, "tool_policy", "ask") if safety else "ask"
    if mode not in VALID_POLICY_MODES:
        return DiagnosticItem("Политика инструментов", "WARN", f"Неизвестный режим '{mode}', будет использован ask")
    return DiagnosticItem("Политика инструментов", "OK", f"Режим {mode.upper()} (разрушающие команды всегда защищены)")


def _check_storage_and_sqlite(sessions_db_path: Optional[Path] = None) -> DiagnosticItem:
    db_path = sessions_db_path or (USER_CONFIG_DIR / "sessions.db")
    try:
        USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return DiagnosticItem("Хранилище профиля", "FAIL", f"Нет доступа к {USER_CONFIG_DIR}: {e}")

    if not db_path.exists():
        return DiagnosticItem("Хранилище профиля", "OK", f"{USER_CONFIG_DIR} доступен (sessions.db будет создана)")

    try:
        with sqlite3.connect(str(db_path), timeout=3.0) as con:
            row = con.execute("PRAGMA integrity_check;").fetchone()
            ok = bool(row and str(row[0]).lower() == "ok")
        if ok:
            return DiagnosticItem("Хранилище профиля", "OK", f"SQLite integrity_check = ok ({db_path.name})")
        return DiagnosticItem("Хранилище профиля", "FAIL", f"SQLite integrity_check failed: {row}")
    except Exception as e:
        return DiagnosticItem("Хранилище профиля", "FAIL", f"Ошибка проверки SQLite: {e}")


def detect_linux_distro() -> tuple[str, str, bool]:
    """
    Detect Linux distribution from /etc/os-release.
    Returns (distro_id, pretty_name, is_officially_supported) for Arch / CachyOS / Fedora.
    """
    os_release = Path("/etc/os-release")
    d_id = "unknown"
    pretty = platform.system()
    id_like = ""
    if os_release.exists():
        try:
            for line in os_release.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("ID="):
                    d_id = line.split("=", 1)[1].strip().strip('"').lower()
                elif line.startswith("PRETTY_NAME="):
                    pretty = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("ID_LIKE="):
                    id_like = line.split("=", 1)[1].strip().strip('"').lower()
        except Exception:
            pass
    supported = d_id in ("arch", "cachyos", "fedora")
    if not supported and any(tok in id_like.split() for tok in ("arch", "fedora")):
        supported = d_id in ("arch", "cachyos", "fedora")
    return d_id, pretty, supported


def _distro_install_hint(distro_id: str, pkgs_arch: str, pkgs_fedora: str) -> str:
    if distro_id == "fedora":
        return f"sudo dnf install {pkgs_fedora}"
    if distro_id in ("arch", "cachyos"):
        return f"sudo pacman -S {pkgs_arch}"
    return f"Arch/CachyOS: sudo pacman -S {pkgs_arch} | Fedora: sudo dnf install {pkgs_fedora}"


def _check_display_and_niri() -> DiagnosticItem:
    from .compositor import detect_compositor

    wayland = os.environ.get("WAYLAND_DISPLAY", "")
    x11 = os.environ.get("DISPLAY", "")
    qt_plat = os.environ.get("QT_QPA_PLATFORM", "auto")
    comp = detect_compositor()
    d_id, d_pretty, d_supported = detect_linux_distro()
    distro_note = d_pretty if d_supported else f"{d_pretty} (официально: Arch/CachyOS/Fedora)"

    if comp == "niri":
        return DiagnosticItem("Окружение / WM", "OK", f"Niri Wayland ({wayland or 'active'}), {distro_note}, Qt={qt_plat}")
    if comp == "hyprland":
        return DiagnosticItem("Окружение / WM", "OK", f"Hyprland Wayland ({wayland or 'active'}), {distro_note}, Qt={qt_plat}")
    if wayland or x11:
        sess = f"Wayland ({wayland})" if wayland else f"X11 ({x11})"
        return DiagnosticItem(
            "Окружение / WM",
            "WARN",
            f"{sess}, {distro_note} — Niri/Hyprland не активен (оконные команды и локомоция мягко отключены)",
        )
    return DiagnosticItem("Окружение / WM", "WARN", f"Headless-сессия ({distro_note}, Qt={qt_plat}), Niri/Hyprland недоступен")


def _check_audio_playback() -> DiagnosticItem:
    players = [p for p in ("mpv", "paplay", "pw-play") if shutil.which(p)]
    if players:
        return DiagnosticItem("Воспроизведение звука", "OK", f"Найдены плееры: {', '.join(players)}")
    d_id, _, _ = detect_linux_distro()
    hint = _distro_install_hint(d_id, "mpv pipewire-pulse", "mpv pipewire-utils")
    return DiagnosticItem(
        "Воспроизведение звука",
        "WARN",
        f"Не найдены mpv/paplay/pw-play — озвучка TTS может не работать ({hint})",
    )


def _check_vosk_model(config: AppConfig) -> DiagnosticItem:
    wake_on = bool(getattr(config.voice, "wake_word_enabled", False))
    model_dir = ROOT_DIR / "models" / "vosk-model-small-ru-0.22"
    if model_dir.exists() and (model_dir / "am").exists():
        return DiagnosticItem("Wake-Word (Vosk)", "OK", f"Локальная модель готова ({'включён' if wake_on else 'выключен'})")
    if wake_on:
        return DiagnosticItem("Wake-Word (Vosk)", "WARN", "Wake-Word включён, но локальная модель Vosk ещё не загружена")
    return DiagnosticItem("Wake-Word (Vosk)", "OK", "Отключён в настройках")


def _check_pipewire_aec(config: AppConfig) -> DiagnosticItem:
    from ..voice.pipewire import get_pipewire_status

    st = get_pipewire_status()
    if not st.available:
        return DiagnosticItem("PipeWire / AEC", "WARN", "pactl / PipeWire не обнаружен (эхоподавление недоступно)")
    srv = st.server_name or "Audio server"
    if st.aec_active:
        return DiagnosticItem("PipeWire / AEC", "OK", f"{srv} — активен AEC-источник `{st.aec_source_name}`")
    barge_on = bool(getattr(config.voice, "hands_free_barge_in", True))
    if barge_on:
        return DiagnosticItem(
            "PipeWire / AEC",
            "OK",
            f"{srv} (источник: `{st.default_source or 'default'}`) — AEC не загружен (для колонок включи `/bargein aec on` или откалибруй микрофон в Настройках)",
        )
    return DiagnosticItem("PipeWire / AEC", "OK", f"{srv} (AEC выключен)")


def _check_wm_rules() -> DiagnosticItem:
    from .compositor import detect_compositor
    from .wm_rules import check_wm_rules_installed

    comp = detect_compositor()
    if comp not in ("niri", "hyprland"):
        return DiagnosticItem("Правила WM", "OK", "Niri/Hyprland не активен — проверка правил пропущена")
    installed, found_path = check_wm_rules_installed(comp)
    if installed and found_path:
        return DiagnosticItem("Правила WM", "OK", f"{comp.capitalize()}: правила `capri-avatar` найдены ({found_path.name})")
    return DiagnosticItem(
        "Правила WM",
        "WARN",
        f"{comp.capitalize()}: правила `capri-avatar` не найдены — выполни `/wm-rules apply` или `capri --wm-rules --apply`",
    )


def _check_mcp(config: AppConfig) -> DiagnosticItem:
    mcp_cfg = getattr(config, "mcp", None)
    if not mcp_cfg or not getattr(mcp_cfg, "enabled", False):
        return DiagnosticItem("MCP-клиент", "OK", "Выключен (включается в config.yaml → mcp.enabled: true)")
    servers = getattr(mcp_cfg, "servers", []) or []
    enabled_cnt = sum(1 for s in servers if getattr(s, "enabled", True))
    return DiagnosticItem("MCP-клиент", "OK", f"Включён: настроено серверов `{enabled_cnt}` (подключение: `/mcp connect`)")


def run_diagnostics(config: AppConfig, sessions_db_path: Optional[Path] = None) -> list[DiagnosticItem]:
    """Run all self-diagnostic checks without mutating system state or exposing secrets."""
    return [
        _check_python(),
        _check_llm(config),
        _check_config_security(),
        _check_tool_policy(config),
        _check_storage_and_sqlite(sessions_db_path=sessions_db_path),
        _check_display_and_niri(),
        _check_wm_rules(),
        _check_audio_playback(),
        _check_pipewire_aec(config),
        _check_mcp(config),
        _check_vosk_model(config),
    ]


def format_diagnostics_markdown(items: list[DiagnosticItem]) -> str:
    """Format diagnostic results as clean Markdown suitable for Cyberdeck or CLI."""
    icons = {"OK": "● OK", "WARN": "▲ WARN", "FAIL": "✕ FAIL"}
    lines = [f"**CAPRI // SELF-DIAGNOSTICS (v{__version__})**\n"]
    for item in items:
        lines.append(f"- `{icons[item.status]}` **{item.name}:** {item.detail}")
    fails = sum(1 for i in items if i.status == "FAIL")
    warns = sum(1 for i in items if i.status == "WARN")
    summary = "Все критические проверки пройдены." if fails == 0 else f"Найдено критических проблем: {fails}."
    if warns:
        summary += f" Предупреждений: {warns}."
    lines.append(f"\n{summary}")
    return "\n".join(lines)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # URL basic auth credentials: https://user:pass@host
    (re.compile(r"(https?://[^/\s:@\"'<>]+:)([^/\s@\"'<>]+)(@)", re.IGNORECASE), r"\1[REDACTED]\3"),
    # URL query string secrets (?key=..., &token=..., &api_key=...)
    (
        re.compile(
            r"([?&](?:api[_-]?key|key|token|access[_-]?token|refresh[_-]?token|secret|auth|password|passwd|sig)=)([^&\s\"'#<>]+)",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
    # Common provider/platform key prefixes
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9_\-]{8,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\bgsk_[A-Za-z0-9_\-]{8,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{12,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{12,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{12,}\b"), "[REDACTED_KEY]"),
    # JWTs (three base64url segments starting with eyJ)
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{8,}\b"), "[REDACTED_JWT]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{10,}", re.IGNORECASE), "Bearer [REDACTED_TOKEN]"),
    # Env/config style key assignments
    (
        re.compile(
            r"(\b[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|PASSWD)\s*[:=]\s*)([^\s\"'`,;]+)",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
    # User home paths (/home/<user>/... or /Users/<user>/...)
    (re.compile(r"/home/[^/\s\"'`]+"), "/home/<user>"),
    (re.compile(r"/Users/[^/\s\"'`]+"), "/Users/<user>"),
)


def _sanitize_log_line(line: str) -> str:
    """Strip ANSI escape sequences and non-printable control characters from untrusted log lines."""
    if not line:
        return ""
    cleaned = _ANSI_ESCAPE_RE.sub("", line)
    cleaned = _CTRL_CHARS_RE.sub("", cleaned)
    return cleaned


def redact_sensitive_text(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Scrub API keys, bearer tokens, URL query secrets, env secrets, and local home directory usernames."""
    if not text:
        return ""
    cleaned = text
    home_str = str(Path.home())
    if home_str and len(home_str) > 1:
        cleaned = cleaned.replace(home_str, "~")
    for secret in extra_secrets:
        s = (secret or "").strip()
        if len(s) >= 6:
            cleaned = cleaned.replace(s, "[REDACTED_KEY]")
    for pattern, repl in _SECRET_PATTERNS:
        cleaned = pattern.sub(repl, cleaned)
    return cleaned


@dataclass
class BugReportResult:
    report_path: Path
    markdown: str
    log_lines_included: int = 0


def _collect_config_secrets(config: AppConfig) -> tuple[str, ...]:
    vals: list[str] = [
        getattr(config.llm, "api_key", ""),
        getattr(config.vision, "gemini_api_key", ""),
        getattr(config.voice, "fish_audio_api_key", ""),
        getattr(config.voice, "elevenlabs_api_key", ""),
        getattr(config.voice, "gemini_api_key", ""),
        getattr(config.companion, "user_name", ""),
    ]
    mcp_cfg = getattr(config, "mcp", None)
    if mcp_cfg is not None:
        for srv in getattr(mcp_cfg, "servers", []) or []:
            env_map = getattr(srv, "env", None) or {}
            if isinstance(env_map, dict):
                for _k, v in env_map.items():
                    if not isinstance(v, str):
                        continue
                    v_s = v.strip()
                    if v_s.startswith("${") and v_s.endswith("}") and len(v_s) > 3:
                        env_val = os.environ.get(v_s[2:-1], "").strip()
                        if env_val:
                            vals.append(env_val)
                    elif v_s:
                        vals.append(v_s)
    return tuple(dict.fromkeys(v.strip() for v in vals if isinstance(v, str) and len(v.strip()) >= 6))


def generate_bug_report(
    config: AppConfig,
    output_path: Optional[Path] = None,
    sessions_db_path: Optional[Path] = None,
    log_paths: Optional[list[Path]] = None,
    max_log_lines: int = 50,
) -> BugReportResult:
    """
    Build a safe, redacted bug-report bundle for Closed Beta testers.
    Includes app version, OS/WM flags, sanitized config summary, /doctor checks,
    and optional redacted tail of runtime logs.
    NEVER includes chat history, memory.json, emotion.json, .env, or API keys.
    """
    extra_secrets = _collect_config_secrets(config)
    now_utc = datetime.now(timezone.utc)
    stamp = now_utc.strftime("%Y%m%d-%H%M%S")

    if output_path is None:
        out_dir = USER_CONFIG_DIR / "bugreports"
        out_dir.mkdir(parents=True, exist_ok=True)
        target_path = out_dir / f"capri-bugreport-{stamp}.md"
    else:
        target_path = Path(output_path).expanduser()
        target_path.parent.mkdir(parents=True, exist_ok=True)

    items = run_diagnostics(config, sessions_db_path=sessions_db_path)
    diag_md = redact_sensitive_text(format_diagnostics_markdown(items), extra_secrets=extra_secrets)

    desktop = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "unknown"
    wayland_on = bool(os.environ.get("WAYLAND_DISPLAY"))
    niri_on = bool(os.environ.get("NIRI_SOCKET"))
    qt_plat = os.environ.get("QT_QPA_PLATFORM", "auto")

    safety_cfg = getattr(config, "safety", None)
    policy_mode = getattr(safety_cfg, "tool_policy", "ask") if safety_cfg else "ask"

    sections: list[str] = [
        f"# CAPRI // BUG REPORT (v{__version__})",
        f"- **Generated (UTC):** `{now_utc.isoformat()}`",
        f"- **OS / Arch:** `{platform.system()} {platform.release()} ({platform.machine()})`",
        f"- **Python:** `{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}`",
        f"- **Desktop / Display:** `{desktop}` · Wayland=`{wayland_on}` · Niri=`{niri_on}` · Qt=`{qt_plat}`",
        "",
        "## Sanitized Configuration (no keys, no user content)",
        f"- **LLM:** `{(config.llm.provider or 'unknown').upper()}` / `{config.llm.model or '(unset)'}` (key_set=`{bool((config.llm.api_key or '').strip())}`)",
        f"- **Tool Policy:** `{policy_mode}`",
        f"- **Companion Mode:** `{getattr(config.companion, 'mode', 'normal')}` · proactive=`{getattr(config.companion, 'proactive_enabled', True)}`",
        f"- **UI:** avatar=`{getattr(config.ui, 'avatar_enabled', True)}` ({getattr(config.ui, 'avatar_mode', '2d')}) · reduced_motion=`{getattr(config.ui, 'reduced_motion', False)}`",
        f"- **Voice:** tts=`{getattr(config.voice, 'tts_enabled', True)}` ({getattr(config.voice, 'tts_engine', 'edge')}) · stt=`{getattr(config.voice, 'stt_mode', 'groq')}` · wake_word=`{getattr(config.voice, 'wake_word_enabled', False)}`",
        "",
        "## Diagnostics (/doctor)",
        diag_md,
    ]

    candidate_logs = log_paths if log_paths is not None else [USER_CONFIG_DIR / "capri.log"]
    log_tail_lines: list[str] = []
    for lp in candidate_logs:
        p = Path(lp)
        if not p.exists() or not p.is_file():
            continue
        try:
            raw_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = raw_lines[-max_log_lines:] if max_log_lines > 0 else []
            for ln in tail:
                log_tail_lines.append(redact_sensitive_text(_sanitize_log_line(ln), extra_secrets=extra_secrets))
        except Exception as e:
            log_tail_lines.append(f"[Unable to read {p.name}: {e}]")

    sections.append("")
    sections.append("## Recent Runtime Log Tail (redacted)")
    if log_tail_lines:
        sections.append("```text")
        sections.extend(log_tail_lines)
        sections.append("```")
    else:
        sections.append("_No runtime log file found (chat history and memory are intentionally excluded)._")

    sections.append("")
    sections.append("_Privacy note: Chat history (`sessions.db`), `memory.json`, `emotion.json`, `.env`, and API keys are never included._")

    report_md = redact_sensitive_text("\n".join(sections) + "\n", extra_secrets=extra_secrets)
    target_path.write_text(report_md, encoding="utf-8")
    try:
        os.chmod(target_path, 0o600)
    except OSError:
        pass

    return BugReportResult(
        report_path=target_path,
        markdown=report_md,
        log_lines_included=len(log_tail_lines),
    )
