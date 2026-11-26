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
  - `trusted`: read-only, companion state, normal desktop controls (`focus_window`,
    `media_control`, `set_volume`, `open_app`, `open_url`, `close_app`, `niri_action`),
    normal non-sensitive file writes, and a conservative allowlist of non-destructive
    shell utilities/pipelines (`|`, `&&`, zero redirects/substitutions/interpreters/code-exec flags)
    are auto-allowed; script interpreters (`python3 -c`, `bash -c`, `node -e`, etc.),
    destructive/privileged shell commands, redirects, sensitive paths (`.env`, `~/.ssh`,
    `~/.bashrc`, `~/.config/niri`, `/etc`), `kill_process`, direct memory mutations,
    `capri_voice_call`, and external `mcp__*` tools STILL require explicit confirmation.
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

# Conservative allowlist of additional non-interpreter utilities permitted in TRUSTED mode
# (never includes script interpreters like python/bash/sh/node/perl/ruby/awk/sed/xargs/env).
TRUSTED_EXTRA_SHELL_BINARIES: frozenset[str] = frozenset({
    "grep",
    "egrep",
    "fgrep",
    "uniq",
    "cut",
    "tr",
    "diff",
    "basename",
    "dirname",
    "realpath",
    "readlink",
    "tree",
    "mkdir",
    "touch",
    "cp",
    "ss",
    "ping",
})

# Strict read-only subcommands for compositor & system query tools in TRUSTED mode
SAFE_NIRI_MSG_SUBCOMMANDS: frozenset[str] = frozenset({
    "windows",
    "workspaces",
    "outputs",
    "focused-window",
    "focused-output",
    "layers",
    "version",
    "keyboard-layouts",
    "overview-state",
})
SAFE_HYPRCTL_SUBCOMMANDS: frozenset[str] = frozenset({
    "clients",
    "activewindow",
    "workspaces",
    "activeworkspace",
    "monitors",
    "layers",
    "version",
    "devices",
    "binds",
})
SAFE_WPCTL_SUBCOMMANDS: frozenset[str] = frozenset({"status", "inspect", "get-volume", "set-volume", "set-mute"})
SAFE_PACTL_SUBCOMMANDS: frozenset[str] = frozenset({
    "info",
    "list",
    "get-default-sink",
    "get-default-source",
    "get-sink-volume",
    "get-sink-mute",
    "set-sink-volume",
    "set-sink-mute",
    "set-source-mute",
})
SAFE_PLAYERCTL_SUBCOMMANDS: frozenset[str] = frozenset({
    "status",
    "metadata",
    "play",
    "pause",
    "play-pause",
    "next",
    "previous",
    "stop",
    "volume",
    "position",
})

# Dangerous flags for `find` / `fd` / `curl` / `rg` / `sort` / `journalctl`
_DANGEROUS_RG_FLAGS: frozenset[str] = frozenset({"--pre", "--pre-glob"})
_DANGEROUS_SORT_FLAGS: frozenset[str] = frozenset({"-o", "--output", "--compress-program"})
_DANGEROUS_JOURNALCTL_FLAGS: frozenset[str] = frozenset({
    "--rotate",
    "--vacuum-size",
    "--vacuum-time",
    "--vacuum-files",
    "--flush",
    "--relinquish-var",
})
# Explicit allowlist of safe window/workspace compositor actions auto-allowed via `niri_action` in TRUSTED mode
SAFE_TRUSTED_COMPOSITOR_ACTIONS: frozenset[str] = frozenset({
    "focus-workspace-down",
    "focus-workspace-up",
    "focus-workspace-previous",
    "focus-column-left",
    "focus-column-right",
    "focus-column-first",
    "focus-column-last",
    "focus-window-down",
    "focus-window-up",
    "focus-monitor-left",
    "focus-monitor-right",
    "focus-monitor-up",
    "focus-monitor-down",
    "focus-monitor-next",
    "focus-monitor-previous",
    "toggle-overview",
    "open-overview",
    "close-overview",
    "fullscreen-window",
    "maximize-column",
    "center-column",
    "center-visible-columns",
    "toggle-window-floating",
    "switch-focus-between-floating-and-tiling",
    "move-column-left",
    "move-column-right",
    "move-window-down",
    "move-window-up",
    "move-column-to-workspace-down",
    "move-column-to-workspace-up",
})


def is_safe_trusted_compositor_action(action: str) -> bool:
    """Return True ONLY for explicitly allowlisted non-destructive Niri/Hyprland navigation/layout actions."""
    parts = (action or "").strip().lower().split()
    if not parts:
        return False
    if len(parts) == 1:
        return parts[0] in SAFE_TRUSTED_COMPOSITOR_ACTIONS
    if len(parts) == 2 and parts[0] in ("focus-workspace", "move-column-to-workspace"):
        return parts[1].isdigit() and 1 <= int(parts[1]) <= 20
    return False

# Dangerous flags for `find` / `fd` / `curl` that can execute code or write/upload files
_DANGEROUS_FIND_FLAGS: frozenset[str] = frozenset({
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
    "-delete",
    "-fls",
    "-fprint",
    "-fprint0",
    "-fprintf",
})
_DANGEROUS_FD_FLAGS: frozenset[str] = frozenset({"-x", "-X", "--exec", "--exec-batch"})
_DANGEROUS_CURL_FLAGS: frozenset[str] = frozenset({
    "-o",
    "-O",
    "--output",
    "--remote-name",
    "--remote-name-all",
    "-K",
    "--config",
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "-F",
    "--form",
    "-T",
    "--upload-file",
    "-X",
    "--request",
})
# Metacharacters forbidden even in TRUSTED auto-allowed shell commands
# (blocks redirects, subshells, command/variable substitution, semicolons, backgrounding, newlines)
_TRUSTED_FORBIDDEN_META_RE = re.compile(r"[;><`$\n\r()]")

SENSITIVE_HOME_DOTFILES: tuple[str, ...] = (
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".xprofile",
)

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
    "/.config/niri",
    "/.config/hypr",
    "/.git/hooks",
    "/.bashrc",
    "/.zshrc",
    "/.profile",
    "/.bash_profile",
    "/.xprofile",
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
    sensitive_names = (".env", ".env.local", "config.yaml") + SENSITIVE_HOME_DOTFILES
    if base_name in sensitive_names:
        return True
    if any(low.endswith("/" + name) for name in sensitive_names):
        return True

    sensitive_canonical = _canonical_sensitive_files()
    home = str(Path.home().resolve()).lower()
    for cand in _resolve_candidate_paths(norm):
        if cand in sensitive_canonical:
            return True
        cand_low = str(cand).lower()
        if cand.name.lower() in sensitive_names:
            return True
        for sub in SENSITIVE_HOME_WRITE_SUBPATHS:
            full_sub = os.path.join(home, sub).lower()
            if cand_low == full_sub or cand_low.startswith(full_sub + "/"):
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
    for dotfile in SENSITIVE_HOME_DOTFILES:
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


def _command_touches_sensitive_path(command: str) -> bool:
    """Return True if any shell token or raw path fragment references a sensitive secret/system path."""
    cmd_clean = (command or "").strip()
    if not cmd_clean:
        return False
    low_cmd = cmd_clean.lower()
    if _touches_sensitive_path(cmd_clean):
        return True
    for dotfile in (".bashrc", ".zshrc", ".profile", ".bash_profile", ".xprofile"):
        if dotfile in low_cmd:
            return True
    for sub in SENSITIVE_HOME_WRITE_SUBPATHS:
        if sub in low_cmd:
            return True
    try:
        tokens = shlex.split(cmd_clean)
    except ValueError:
        tokens = cmd_clean.split()
    for tok in tokens:
        cleaned = tok.strip("\"'`()[]{};,&|<>")
        if not cleaned:
            continue
        if _touches_sensitive_path(cleaned) or _is_sensitive_write_target(cleaned):
            return True
        # Handle flag=path forms (e.g. --env-file=.env, -f=/etc/shadow)
        if "=" in cleaned:
            rhs = cleaned.split("=", 1)[1].strip("\"'")
            if rhs and (_touches_sensitive_path(rhs) or _is_sensitive_write_target(rhs)):
                return True
    return False


def _is_trusted_shell_segment(tokens: list[str]) -> bool:
    """Validate one pipeline/chain segment against the conservative TRUSTED allowlist."""
    if not tokens:
        return False
    exe = os.path.basename(tokens[0]).lower()
    args = tokens[1:]

    for arg in args:
        if _touches_sensitive_path(arg) or _is_sensitive_write_target(arg):
            return False
        if "=" in arg:
            rhs = arg.split("=", 1)[1].strip("\"'")
            if rhs and (_touches_sensitive_path(rhs) or _is_sensitive_write_target(rhs)):
                return False

    if exe == "git":
        if not args:
            return False
        for a in args:
            if a.split("=", 1)[0] in _DANGEROUS_GIT_FLAGS:
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

    if exe == "niri":
        # Allow ONLY read-only `niri msg [--json] <subcmd>` queries (blocks `action quit`, `action spawn`, etc.)
        if len(args) < 2 or args[0] != "msg":
            return False
        rest = args[1:]
        if rest and rest[0] in ("--json", "-j"):
            rest = rest[1:]
        return bool(rest) and rest[0] in SAFE_NIRI_MSG_SUBCOMMANDS

    if exe == "hyprctl":
        # Allow ONLY read-only `hyprctl [-j] <subcmd>` queries (blocks `dispatch exit`, `dispatch exec`, `keyword`, etc.)
        rest = args[1:] if (args and args[0] in ("-j", "--json")) else args
        return bool(rest) and rest[0] in SAFE_HYPRCTL_SUBCOMMANDS

    if exe == "wpctl":
        return bool(args) and args[0] in SAFE_WPCTL_SUBCOMMANDS

    if exe == "pactl":
        return bool(args) and args[0] in SAFE_PACTL_SUBCOMMANDS

    if exe == "playerctl":
        rest = [a for a in args if not a.startswith("-")]
        return bool(rest) and rest[0] in SAFE_PLAYERCTL_SUBCOMMANDS

    if exe in ("timedatectl", "hostnamectl"):
        return not args or args == ["status"]

    if exe == "journalctl":
        return not any(a.split("=", 1)[0] in _DANGEROUS_JOURNALCTL_FLAGS for a in args)

    if exe == "rg":
        return not any(a.split("=", 1)[0] in _DANGEROUS_RG_FLAGS for a in args)

    if exe == "sort":
        return not any(a.split("=", 1)[0] in _DANGEROUS_SORT_FLAGS for a in args)

    if exe == "find":
        return not any(a in _DANGEROUS_FIND_FLAGS for a in args)

    if exe == "fd":
        return not any(a.split("=", 1)[0] in _DANGEROUS_FD_FLAGS for a in args)

    if exe == "curl":
        return not any(a.split("=", 1)[0] in _DANGEROUS_CURL_FLAGS for a in args)

    return exe in SAFE_READONLY_BINARIES or exe in TRUSTED_EXTRA_SHELL_BINARIES


def is_trusted_shell_command(command: str) -> bool:
    """
    Conservative allowlist evaluator for `run_bash` in `TRUSTED` mode.

    Allows simple commands and `|` / `&&` pipelines composed solely of allowlisted
    non-interpreter utilities with zero redirects, command/variable substitutions,
    semicolons, subshells, code-exec flags, destructive patterns, or sensitive paths.
    """
    cmd_clean = (command or "").strip()
    if not cmd_clean:
        return False
    if is_forbidden_shell_command(cmd_clean):
        return False
    if any(re.search(p, cmd_clean, re.IGNORECASE) for p in DESTRUCTIVE_SHELL_PATTERNS):
        return False
    if _TRUSTED_FORBIDDEN_META_RE.search(cmd_clean):
        return False
    if _command_touches_sensitive_path(cmd_clean):
        return False

    try:
        lex = shlex.shlex(cmd_clean, posix=True, punctuation_chars="|&")
        lex.whitespace_split = True
        raw_tokens = list(lex)
    except ValueError:
        return False
    if not raw_tokens:
        return False

    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in raw_tokens:
        if tok in ("|", "&&"):
            if not cur:
                return False
            segments.append(cur)
            cur = []
        elif tok in ("&", "||") or any(ch in tok for ch in ("|", "&")):
            # Reject backgrounding `&`, `||`, or unexpected operator clusters
            return False
        else:
            cur.append(tok)
    if not cur:
        return False
    segments.append(cur)

    return all(_is_trusted_shell_segment(seg) for seg in segments)


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
            touches_sensitive = _command_touches_sensitive_path(cmd)
            risk: RiskLevel = "destructive" if (is_destructive or touches_sensitive) else "mutating"
            if self.mode == "safe":
                return PolicyDecision(
                    "deny",
                    risk,
                    "Режим политики SAFE запрещает произвольные shell-команды вне read-only allowlist.",
                    summary,
                )
            if self.mode == "trusted" and is_trusted_shell_command(cmd):
                return PolicyDecision(
                    "allow",
                    "mutating",
                    "Разрешённая shell-команда из консервативного allowlist (режим TRUSTED).",
                    summary,
                )
            return PolicyDecision(
                "ask",
                risk,
                (
                    "Разрушающая/привилегированная команда или обращение к чувствительным путям требует подтверждения."
                    if (is_destructive or touches_sensitive)
                    else "Команда shell не входит в read-only allowlist и требует подтверждения пользователя."
                ),
                summary,
            )

        if tool_name in ("read_file", "list_directory", "find_files"):
            raw_path = str(args.get("path") or args.get("search_path") or "")
            raw_pattern = str(args.get("pattern") or "") if tool_name == "find_files" else ""
            if _touches_sensitive_path(raw_path) or (raw_pattern and _touches_sensitive_path(raw_pattern)):
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
                    f"Доступ к чувствительному пути (.env / SSH / shell dotfiles / конфиг композитора) через {tool_name} требует подтверждения.",
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
            if self.mode == "trusted":
                return PolicyDecision(
                    "allow",
                    "mutating",
                    "Запись в обычный пользовательский файл (режим TRUSTED).",
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
            if self.mode == "trusted" and tool_name != "capri_voice_call":
                if tool_name == "niri_action":
                    raw_act = str(args.get("action", "") or "").strip()
                    if not is_safe_trusted_compositor_action(raw_act):
                        return PolicyDecision(
                            "ask",
                            "mutating",
                            f"Действие композитора '{raw_act}' не входит в безопасный allowlist и требует подтверждения.",
                            summary,
                        )
                return PolicyDecision(
                    "allow",
                    "mutating",
                    "Управление рабочим столом (режим TRUSTED).",
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
