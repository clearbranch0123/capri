"""
Centralized Tool Execution Policy & Approval Gate for Capri AI Companion.

Security model (Closed Beta):
- Explicit allowlist for read-only tools and strictly read-only shell commands
  (no pipes, redirections, command substitutions, chaining, or secret file reads).
- Forbidden destructive / privilege-escalation shell patterns and sensitive paths
  are ALWAYS denied regardless of policy mode.
- Mutating OS actions (non-read-only shell commands, file writes, process kills,
  desktop actions) require explicit user confirmation by default (`policy="ask"`).
- Policy modes:
  - `safe`: strictly read-only — ONLY explicit read-only tools (excluding sensitive config/secret paths)
    and strictly read-only allowlisted shell commands are allowed; ALL state-mutating tools
    (both OS-level AND Capri internal state such as memory/context/timers/initiative/avatar) are denied.
  - `ask` (default): read-only & Capri-internal companion state allowed; OS-mutating actions and
    sensitive config/secret file reads ask user confirmation.
  - `trusted`: read-only, companion state & non-destructive desktop controls (`focus_window`,
    `media_control`, `set_volume`) allowed; shell, file-write, kill, destructive actions and
    sensitive config/secret reads STILL require confirmation (no silent bypass).
"""

from __future__ import annotations

import inspect
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

RiskLevel = Literal["read_only", "companion", "mutating", "destructive", "forbidden"]
PolicyAction = Literal["allow", "ask", "deny"]
PolicyMode = Literal["safe", "ask", "trusted"]

VALID_POLICY_MODES = ("safe", "ask", "trusted")

# Explicit allowlist of strictly read-only tools (auto-allowed in all modes)
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "get_system_status",
    "list_top_processes",
    "list_windows",
    "get_volume",
    "get_weather",
    "web_search",
    "fetch_web_page",
    "capri_browser_visit",
    "capri_browser_search",
    "capri_browser_follow_link",
    "capri_browser_state",
    "list_directory",
    "read_file",
    "find_files",
    "recall_memory",
    "list_memory_categories",
    "get_memory_workbench_stats",
    "get_work_context",
    "get_initiative_status",
    "list_timers",
    "look_at_screen",
    "read_screen",
})

# Capri-internal companion state (scoped to ~/.config/capri/*, timers, notifications, avatar UI)
COMPANION_STATE_TOOLS: frozenset[str] = frozenset({
    "add_work_item",
    "resolve_work_item",
    "propose_action",
    "configure_initiative",
    "set_timer",
    "cancel_timer",
    "send_desktop_notification",
    "capri_locomotion",
    "capri_self_control",
    "capri_browser_clear",
    "take_screenshot",
})

# User-controlled long-term memory mutations: NEVER silently auto-allowed by LLM tool calls
MEMORY_MUTATING_TOOLS: frozenset[str] = frozenset({
    "remember",
    "update_memory_entry",
    "forget_memory",
})

# OS-mutating desktop tools
OS_MUTATING_TOOLS: frozenset[str] = frozenset({
    "open_app",
    "open_url",
    "close_app",
    "focus_window",
    "media_control",
    "set_volume",
    "niri_action",
    "capri_voice_call",
})

# High-impact OS tools (file writes, process termination, shell execution)
HIGH_IMPACT_TOOLS: frozenset[str] = frozenset({
    "write_file",
    "kill_process",
    "run_bash",
})

# Always forbidden shell patterns — NEVER allowed even with confirmation or in any mode
FORBIDDEN_SHELL_PATTERNS: tuple[str, ...] = (
    r"\brm\s+(-[a-zA-Z0-9\-_]+\s+)*(/|/\*|~|\$HOME)(?:\s|$)",
    r"\bmkfs(?:\.[a-z0-9]+)?\b",
    r"\bdd\s+if=.*of=/dev/(sd|nvme|vd|mmcblk)",
    r":\(\)\s*\{\s*:\|:&\s*\};:",  # Fork bomb
    r">\s*/dev/(sd|nvme|vd|mmcblk)",
    r"\bchmod\s+(-[a-zA-Z0-9\-_]+\s+)*777\s+/(?:\s|$)",
    r"\bchown\s+(-[a-zA-Z0-9\-_]+\s+)*.*?\s+/(?:\s|$)",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r"\bhalt\b",
    r"\b(curl|wget)\b[^|]*\|\s*(ba|z|da)?sh\b",
)

# Destructive or privilege-escalating shell patterns — ALWAYS require confirmation
DESTRUCTIVE_SHELL_PATTERNS: tuple[str, ...] = (
    r"\b(sudo|su|doas|pkexec)\b",
    r"\b(rm|shred|unlink|wipefs|fdisk|parted)\b",
    r"\b(kill|killall|pkill)\b",
    r"\b(systemctl|pacman|yay|paru|apt|dnf)\b",
    r"\b(chmod|chown|mv)\b",
)

# Shell metacharacters & glob/variable expansions forbidden in auto-allowed read-only shell commands
_SHELL_META_RE = re.compile(r"[;|&><`\n\r*?\[\]$]")

# Dangerous git flags that can execute arbitrary commands, alter repo paths, or write files
_DANGEROUS_GIT_FLAGS: frozenset[str] = frozenset({
    "-c",
    "--config-env",
    "--exec-path",
    "--upload-pack",
    "--receive-pack",
    "--ext-diff",
    "--textconv",
    "--git-dir",
    "--work-tree",
    "--output",
    "-o",
})

# Explicit allowlist of read-only binaries for auto-allowed `run_bash`
SAFE_READONLY_BINARIES: frozenset[str] = frozenset({
    "echo",
    "pwd",
    "whoami",
    "id",
    "uname",
    "uptime",
    "date",
    "hostname",
    "df",
    "du",
    "free",
    "ls",
    "lsblk",
    "lscpu",
    "lsusb",
    "lspci",
    "ps",
    "fastfetch",
    "neofetch",
    "which",
    "whereis",
    "stat",
    "file",
    "wc",
    "cat",
    "head",
    "tail",
    "nvidia-smi",
    "sensors",
    "ip",
})

SAFE_GIT_SUBCOMMANDS: frozenset[str] = frozenset({
    "status",
    "log",
    "diff",
    "branch",
    "show",
    "rev-parse",
    "describe",
    "tag",
})

SAFE_PACMAN_FLAGS: frozenset[str] = frozenset({
    "-Q",
    "-Qu",
    "-Qi",
    "-Ql",
    "-Qs",
    "-Qe",
    "-Qm",
    "-Ss",
    "-Si",
})

SENSITIVE_PATH_FRAGMENTS: tuple[str, ...] = (
    "/.env",
    "/.ssh/",
    "/.ssh",
    "/.gnupg/",
    "/.gnupg",
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/passwd",
    "/.aws/",
    "/.kube/",
    "/.netrc",
    "/.npmrc",
    "/.pypirc",
    "/.docker/config.json",
    "/.config/capri",
    "/.config/autostart",
    "/.config/systemd",
    "/.git/hooks",
    "/id_rsa",
    "/id_ed25519",
)

SENSITIVE_WRITE_PREFIXES: tuple[str, ...] = (
    "/etc",
    "/usr",
    "/boot",
    "/sys",
    "/proc",
    "/dev",
)

SENSITIVE_HOME_WRITE_SUBPATHS: tuple[str, ...] = (
    ".config/autostart",
    ".config/systemd",
    ".config/niri",
    ".config/hypr",
    ".ssh",
    ".gnupg",
)


@dataclass
class PolicyDecision:
    action: PolicyAction
    risk: RiskLevel
    reason: str
    summary: str


@dataclass
class ApprovalRequest:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    risk: RiskLevel = "mutating"
    reason: str = ""
    summary: str = ""


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[bool] | bool]


def _canonical_sensitive_files() -> set[Path]:
    """Return canonicalized paths for local & user Capri config/env files."""
    from .config import ROOT_DIR, USER_CONFIG_FILE, USER_ENV_FILE

    candidates = [
        ROOT_DIR / "config.yaml",
        ROOT_DIR / ".env",
        USER_CONFIG_FILE,
        USER_ENV_FILE,
    ]
    resolved: set[Path] = set()
    for p in candidates:
        try:
            resolved.add(p.expanduser().resolve())
        except Exception:
            resolved.add(p)
    return resolved


def _resolve_candidate_paths(raw_path: str) -> list[Path]:
    """Resolve raw_path against home, cwd, and Capri ROOT_DIR to prevent relative/symlink bypasses."""
    from .config import ROOT_DIR

    cleaned = (raw_path or "").strip()
    if not cleaned:
        return []
    expanded = os.path.expanduser(cleaned)
    p = Path(expanded)
    candidates: list[Path] = []
    if p.is_absolute():
        candidates.append(p)
    else:
        for base in (Path.home(), Path.cwd(), ROOT_DIR):
            candidates.append(base / p)

    out: list[Path] = []
    for c in candidates:
        try:
            out.append(c.resolve())
        except Exception:
            out.append(c)
    return out


def _resolve_target_path(raw_path: str) -> str:
    try:
        expanded = os.path.expanduser((raw_path or "").strip())
        p = Path(expanded)
        if not p.is_absolute():
            p = Path.home() / p
        return str(p.resolve())
    except Exception:
        return (raw_path or "").strip()


def _touches_sensitive_path(raw: str) -> bool:
    norm = raw.replace("\\", "/").strip()
    if not norm:
        return False
    low = norm.lower()
    base_name = os.path.basename(low)
    if base_name in (".env", ".env.local", "config.yaml"):
        return True
    if low.endswith("/.env") or low.endswith("/.env.local") or low.endswith("/config.yaml"):
        return True

    sensitive_canonical = _canonical_sensitive_files()
    for cand in _resolve_candidate_paths(norm):
        if cand in sensitive_canonical:
            return True
        cand_low = str(cand).lower()
        if cand.name.lower() in (".env", ".env.local", "config.yaml"):
            return True
        for frag in SENSITIVE_PATH_FRAGMENTS:
            if frag in cand_low:
                return True

    for frag in SENSITIVE_PATH_FRAGMENTS:
        if frag in low:
            return True
    return False


def _is_sensitive_write_target(raw_path: str) -> bool:
    if _touches_sensitive_path(raw_path):
        return True
    resolved = _resolve_target_path(raw_path)
    for pref in SENSITIVE_WRITE_PREFIXES:
        if resolved == pref or resolved.startswith(pref + "/"):
            return True
    home = str(Path.home().resolve())
    for dotfile in (".bashrc", ".zshrc", ".profile", ".bash_profile", ".xprofile"):
        if resolved == os.path.join(home, dotfile):
            return True
    for sub in SENSITIVE_HOME_WRITE_SUBPATHS:
        full_sub = os.path.join(home, sub)
        if resolved == full_sub or resolved.startswith(full_sub + "/"):
            return True
    if "/.git/hooks/" in resolved or resolved.endswith("/.git/hooks"):
        return True
    return False


def is_forbidden_shell_command(command: str) -> bool:
    cmd_clean = (command or "").strip()
    if not cmd_clean:
        return False
    for pattern in FORBIDDEN_SHELL_PATTERNS:
        if re.search(pattern, cmd_clean, re.IGNORECASE):
            return True
    return False


def is_safe_readonly_shell_command(command: str) -> bool:
    """
    Return True ONLY for an explicit allowlist of strictly read-only shell commands
    with zero shell metacharacters, piping, redirection, or sensitive file access.
    """
    cmd_clean = (command or "").strip()
    if not cmd_clean:
        return False
    if _SHELL_META_RE.search(cmd_clean):
        return False
    if is_forbidden_shell_command(cmd_clean):
        return False
    try:
        tokens = shlex.split(cmd_clean)
    except ValueError:
        return False
    if not tokens:
        return False

    exe = os.path.basename(tokens[0]).lower()
    args = tokens[1:]

    for arg in args:
        if _touches_sensitive_path(arg):
            return False

    if exe == "git":
        if not args:
            return False
        # Block dangerous flags anywhere (e.g. `git -c core.pager=...`, `--ext-diff`, `--output=...`)
        for a in args:
            flag_head = a.split("=", 1)[0]
            if flag_head in _DANGEROUS_GIT_FLAGS:
                return False
        if args[0] == "-C":
            if len(args) < 3:
                return False
            sub = args[2]
            sub_args = args[3:]
        else:
            if args[0].startswith("-"):
                return False
            sub = args[0]
            sub_args = args[1:]
        if sub not in SAFE_GIT_SUBCOMMANDS:
            return False
        if sub == "branch" and any(a in ("-d", "-D", "-m", "-M", "--delete", "--move", "-c", "-C", "--copy") for a in sub_args):
            return False
        if sub == "tag" and any(not a.startswith("-l") and a != "--list" for a in sub_args):
            return False
        return True

    if exe == "pacman":
        return bool(args) and args[0] in SAFE_PACMAN_FLAGS

    if exe == "ip":
        return bool(args) and args[0] in ("addr", "a", "route", "r", "link", "l") and (
            len(args) == 1 or args[1] in ("show", "list")
        )

    return exe in SAFE_READONLY_BINARIES


def _format_summary(tool_name: str, arguments: dict[str, Any]) -> str:
    if tool_name == "run_bash":
        return f"run_bash: {arguments.get('command', '')}"
    if tool_name == "write_file":
        path = arguments.get("path", "")
        size = len(str(arguments.get("content", "")))
        return f"write_file: {path} ({size} симв.)"
    if tool_name == "kill_process":
        return f"kill_process: {arguments.get('target', '')}"
    if not arguments:
        return tool_name
    pairs = ", ".join(f"{k}={str(v)[:60]}" for k, v in arguments.items())
    return f"{tool_name}({pairs})"


class ToolPolicyEngine:
    """Centralized, testable execution policy gate for Capri tool calls."""

    def __init__(
        self,
        mode: str = "ask",
        allow_shell_read_only: bool = True,
        approval_handler: Optional[ApprovalHandler] = None,
    ):
        self.mode: PolicyMode = mode if mode in VALID_POLICY_MODES else "ask"  # type: ignore[assignment]
        self.allow_shell_read_only: bool = bool(allow_shell_read_only)
        self._approval_handler: Optional[ApprovalHandler] = approval_handler

    def set_mode(self, mode: str) -> str:
        norm = (mode or "ask").strip().lower()
        if norm not in VALID_POLICY_MODES:
            raise ValueError(f"Unsupported tool policy mode: {mode}. Use one of: {', '.join(VALID_POLICY_MODES)}")
        self.mode = norm  # type: ignore[assignment]
        return self.mode

    def set_approval_handler(self, handler: Optional[ApprovalHandler]) -> None:
        self._approval_handler = handler

    def evaluate(self, tool_name: str, arguments: Optional[dict[str, Any]] = None) -> PolicyDecision:
        args = arguments or {}
        summary = _format_summary(tool_name, args)

        if tool_name == "run_bash":
            cmd = str(args.get("command", "") or "").strip()
            if not cmd:
                return PolicyDecision("allow", "read_only", "Пустая команда.", summary)
            if is_forbidden_shell_command(cmd):
                return PolicyDecision(
                    "deny",
                    "forbidden",
                    "Команда расценена как потенциально опасная или разрушительная и заблокирована политикой безопасности.",
                    summary,
                )
            if self.allow_shell_read_only and is_safe_readonly_shell_command(cmd):
                return PolicyDecision("allow", "read_only", "Безопасная read-only команда из allowlist.", summary)

            is_destructive = any(re.search(p, cmd, re.IGNORECASE) for p in DESTRUCTIVE_SHELL_PATTERNS)
            risk: RiskLevel = "destructive" if is_destructive else "mutating"
            if self.mode == "safe":
                return PolicyDecision(
                    "deny",
                    risk,
                    "Режим политики SAFE запрещает произвольные shell-команды вне read-only allowlist.",
                    summary,
                )
            # Even in 'trusted', non-readonly shell commands still require explicit confirmation
            return PolicyDecision(
                "ask",
                risk,
                "Команда shell не входит в read-only allowlist и требует подтверждения пользователя.",
                summary,
            )

        if tool_name in ("read_file", "list_directory", "find_files"):
            raw_path = str(args.get("path") or args.get("search_path") or "")
            if _touches_sensitive_path(raw_path):
                if self.mode == "safe":
                    return PolicyDecision(
                        "deny",
                        "destructive",
                        f"Доступ к чувствительному пути ({tool_name}) запрещён в режиме SAFE.",
                        summary,
                    )
                return PolicyDecision(
                    "ask",
                    "destructive",
                    f"Доступ к чувствительному пути (.env / SSH / конфиг секретов) через {tool_name} требует подтверждения.",
                    summary,
                )
            return PolicyDecision("allow", "read_only", "Чтение файловой системы.", summary)

        if tool_name == "write_file":
            raw_path = str(args.get("path", "") or "")
            if _is_sensitive_write_target(raw_path):
                if self.mode == "safe":
                    return PolicyDecision(
                        "deny",
                        "forbidden",
                        "Запись в системный или чувствительный файл запрещена в режиме SAFE.",
                        summary,
                    )
                return PolicyDecision(
                    "ask",
                    "destructive",
                    "Запись в чувствительный или системный файл требует явного подтверждения.",
                    summary,
                )
            if self.mode == "safe":
                return PolicyDecision(
                    "deny",
                    "mutating",
                    "Запись файлов запрещена в режиме политики SAFE.",
                    summary,
                )
            return PolicyDecision(
                "ask",
                "mutating",
                "Запись в файл изменяет данные на диске и требует подтверждения.",
                summary,
            )

        if tool_name == "kill_process":
            if self.mode == "safe":
                return PolicyDecision(
                    "deny",
                    "destructive",
                    "Завершение процессов запрещено в режиме политики SAFE.",
                    summary,
                )
            return PolicyDecision(
                "ask",
                "destructive",
                "Завершение процесса требует подтверждения пользователя.",
                summary,
            )

        if tool_name in READ_ONLY_TOOLS:
            return PolicyDecision("allow", "read_only", "Инструмент только для чтения.", summary)

        if tool_name in MEMORY_MUTATING_TOOLS:
            if self.mode == "safe":
                return PolicyDecision(
                    "deny",
                    "memory",
                    f"Режим SAFE строго read-only: изменение памяти '{tool_name}' запрещено.",
                    summary,
                )
            return PolicyDecision(
                "ask",
                "memory",
                f"Изменение долговременной памяти ('{tool_name}') требует явного подтверждения пользователя.",
                summary,
            )

        if tool_name in COMPANION_STATE_TOOLS:
            if self.mode == "safe":
                return PolicyDecision(
                    "deny",
                    "companion",
                    f"Режим SAFE строго read-only: изменяющий состояние Capri инструмент '{tool_name}' запрещён.",
                    summary,
                )
            return PolicyDecision("allow", "companion", "Внутреннее состояние компаньона Capri.", summary)

        if tool_name in OS_MUTATING_TOOLS:
            if self.mode == "safe":
                return PolicyDecision(
                    "deny",
                    "mutating",
                    f"Изменяющее действие ОС '{tool_name}' запрещено в режиме SAFE.",
                    summary,
                )
            if self.mode == "trusted" and tool_name in ("focus_window", "media_control", "set_volume"):
                return PolicyDecision(
                    "allow",
                    "mutating",
                    "Неразрушающее управление рабочим столом (режим TRUSTED).",
                    summary,
                )
            return PolicyDecision(
                "ask",
                "mutating",
                f"Действие ОС '{tool_name}' требует подтверждения пользователя.",
                summary,
            )

        if tool_name.startswith("mcp__"):
            if self.mode == "safe":
                return PolicyDecision(
                    "deny",
                    "mutating",
                    f"Внешний MCP-инструмент '{tool_name}' запрещён в режиме политики SAFE.",
                    summary,
                )
            return PolicyDecision(
                "ask",
                "mutating",
                f"Внешний MCP-инструмент '{tool_name}' требует явного подтверждения пользователя.",
                summary,
            )

        if self.mode == "safe":
            return PolicyDecision(
                "deny",
                "mutating",
                f"Неизвестный инструмент '{tool_name}' запрещён в режиме SAFE.",
                summary,
            )
        return PolicyDecision(
            "ask",
            "mutating",
            f"Инструмент '{tool_name}' не входит в read-only allowlist и требует подтверждения.",
            summary,
        )

    async def authorize(self, tool_name: str, arguments: Optional[dict[str, Any]] = None) -> tuple[bool, PolicyDecision]:
        """Evaluate policy and invoke approval_handler when action == 'ask'. Fails closed without a handler."""
        decision = self.evaluate(tool_name, arguments)
        if decision.action == "allow":
            return True, decision
        if decision.action == "deny":
            return False, decision

        if self._approval_handler is None:
            return False, PolicyDecision(
                "deny",
                decision.risk,
                f"{decision.reason} (Нет интерактивного обработчика подтверждения — отклонено fail-closed).",
                decision.summary,
            )

        req = ApprovalRequest(
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            risk=decision.risk,
            reason=decision.reason,
            summary=decision.summary,
        )
        res = self._approval_handler(req)
        if inspect.isawaitable(res):
            approved = bool(await res)
        else:
            approved = bool(res)
        if approved:
            return True, decision
        return False, PolicyDecision(
            "deny",
            decision.risk,
            "Пользователь отклонил выполнение действия.",
            decision.summary,
        )
