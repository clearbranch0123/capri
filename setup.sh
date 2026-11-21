#!/usr/bin/env bash
# Capri AI Companion — Clean Bootstrap, Upgrade & Uninstall Script (Open Beta)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MODE="install"
PURGE_DATA=0
OFFLINE_MODE=0

usage() {
    cat <<'EOF'
Capri AI Companion — Setup, Upgrade & Uninstall (Open Beta)

Usage:
  ./setup.sh                  Install or refresh Capri (.venv, config templates, .desktop)
  ./setup.sh --upgrade        Safely upgrade Capri in-place with pre-upgrade profile backup
  ./setup.sh --uninstall      Remove .venv and desktop launcher (preserves user data/keys)
  ./setup.sh --uninstall --purge
                              Remove .venv, desktop launcher, AND user config/profile data
  ./setup.sh --offline        Skip network dependency resolution (uses existing venv / --no-deps)
  ./setup.sh --help           Show this help message

Environment:
  XDG_CONFIG_HOME             Overrides user profile dir (default: ~/.config/capri)
  XDG_DATA_HOME               Overrides .desktop dir (default: ~/.local/share/applications)
  PYTHON_BIN                  Override Python 3.10+ interpreter (default: python3)
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --upgrade|-u)
            MODE="upgrade"
            ;;
        --uninstall)
            MODE="uninstall"
            ;;
        --purge)
            PURGE_DATA=1
            ;;
        --offline)
            OFFLINE_MODE=1
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/capri"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_TARGET="$APP_DIR/capri-cyberdeck.desktop"

if [ "$MODE" = "uninstall" ]; then
    echo "=== CAPRI AI COMPANION // UNINSTALL ==="
    if [ -f "$DESKTOP_TARGET" ]; then
        rm -f "$DESKTOP_TARGET"
        echo "  Removed desktop launcher: $DESKTOP_TARGET"
    else
        echo "  Desktop launcher not present: $DESKTOP_TARGET"
    fi

    if [ -d "$SCRIPT_DIR/.venv" ]; then
        rm -rf "$SCRIPT_DIR/.venv"
        echo "  Removed virtual environment: $SCRIPT_DIR/.venv"
    else
        echo "  Virtual environment not present: $SCRIPT_DIR/.venv"
    fi

    if [ "$PURGE_DATA" -eq 1 ]; then
        if [ -d "$CONFIG_DIR" ]; then
            rm -rf "$CONFIG_DIR"
            echo "  Purged user profile directory: $CONFIG_DIR"
        fi
    else
        echo "  Preserved user profile & keys in: $CONFIG_DIR"
        echo "  (Pass --uninstall --purge to remove profile data as well.)"
    fi
    echo "Uninstall complete."
    exit 0
fi

echo "=== CAPRI AI COMPANION // ${MODE^^} ==="

# 1. Check Python >= 3.10
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Error: python3 not found. Please install Python 3.10+." >&2
    exit 1
fi
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), f"Python 3.10+ required, got {sys.version}"'

# 2. On --upgrade, snapshot existing user profile data BEFORE modifying .venv
BACKUP_SNAP=""
if [ "$MODE" = "upgrade" ] && [ -d "$CONFIG_DIR" ]; then
    SNAP_TS="$(date +%Y%m%d_%H%M%S)"
    BACKUP_SNAP="$CONFIG_DIR/backups/pre_upgrade_${SNAP_TS}"
    mkdir -p "$BACKUP_SNAP"
    for fname in config.yaml sessions.db memory.json emotion.json context.json initiative.json; do
        if [ -f "$CONFIG_DIR/$fname" ]; then
            cp -p "$CONFIG_DIR/$fname" "$BACKUP_SNAP/$fname"
        fi
    done
    echo "[0/4] Created pre-upgrade profile snapshot: $BACKUP_SNAP"
    echo "      (Existing .env, config.yaml, sessions.db, memory.json will NOT be overwritten.)"
fi

# 3. Create virtual environment if missing
if [ ! -d ".venv" ]; then
    echo "[1/4] Creating .venv..."
    if [ "$OFFLINE_MODE" -eq 1 ]; then
        "$PYTHON_BIN" -m venv --system-site-packages .venv
    elif command -v uv >/dev/null 2>&1; then
        uv venv .venv
    else
        "$PYTHON_BIN" -m venv .venv
    fi
else
    echo "[1/4] Using existing .venv"
fi

# 4. Install or upgrade Capri package
echo "[2/4] Installing capri package (editable)..."
install_pkg() {
    if [ "${CAPRI_SIMULATE_UPGRADE_FAIL:-0}" = "1" ]; then
        echo "Error: Simulated interrupted upgrade failure (CAPRI_SIMULATE_UPGRADE_FAIL=1)." >&2
        return 1
    fi
    if [ "$OFFLINE_MODE" -eq 1 ]; then
        if [ -x ".venv/bin/pip" ]; then
            .venv/bin/pip install --no-build-isolation --no-deps -e .
        else
            # Minimal fallback when venv has no pip in offline test harness
            SITE_PKG="$(.venv/bin/python -c 'import site; print(site.getsitepackages()[0])')"
            printf '%s\n' "$SCRIPT_DIR" > "$SITE_PKG/capri_editable.pth"
        fi
        return 0
    fi
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python .venv/bin/python -e ".[test]"
    else
        .venv/bin/pip install --upgrade pip
        .venv/bin/pip install -e ".[test]"
    fi
}

if ! install_pkg; then
    echo "" >&2
    echo "ERROR: Package ${MODE} failed or was interrupted." >&2
    echo "Your user data and API keys in '$CONFIG_DIR' were NOT modified." >&2
    if [ -n "$BACKUP_SNAP" ]; then
        echo "Pre-upgrade backup snapshot is preserved at: $BACKUP_SNAP" >&2
    fi
    exit 1
fi

# 5. Initialize user config & .env templates (secret-free; NEVER overwrite existing user files)
echo "[3/4] Checking local config and .env templates ($CONFIG_DIR)..."
mkdir -p "$CONFIG_DIR"

if [ ! -f ".env" ] && [ ! -f "$CONFIG_DIR/.env" ] && [ -f ".env.example" ]; then
    cp ".env.example" "$CONFIG_DIR/.env"
    chmod 600 "$CONFIG_DIR/.env"
    echo "  Created $CONFIG_DIR/.env (0600) from .env.example — store your local API keys there."
elif [ -f "$CONFIG_DIR/.env" ]; then
    chmod 600 "$CONFIG_DIR/.env" 2>/dev/null || true
    echo "  Preserved existing $CONFIG_DIR/.env"
fi

if [ ! -f "$CONFIG_DIR/config.yaml" ] && [ -f "config.example.yaml" ]; then
    cp "config.example.yaml" "$CONFIG_DIR/config.yaml"
    echo "  Created $CONFIG_DIR/config.yaml from config.example.yaml."
elif [ -f "$CONFIG_DIR/config.yaml" ]; then
    echo "  Preserved existing $CONFIG_DIR/config.yaml"
fi

# 6. Install/refresh desktop entry for Wayland app_id registration
echo "[4/4] Installing desktop entry..."
mkdir -p "$APP_DIR"
DESKTOP_SRC=""
if [ -f "capri/assets/capri-cyberdeck.desktop" ]; then
    DESKTOP_SRC="capri/assets/capri-cyberdeck.desktop"
elif [ -f "assets/capri-cyberdeck.desktop" ]; then
    DESKTOP_SRC="assets/capri-cyberdeck.desktop"
fi
if [ -n "$DESKTOP_SRC" ]; then
    sed "s|@CAPRI_ROOT@|$SCRIPT_DIR|g" "$DESKTOP_SRC" > "$DESKTOP_TARGET"
    echo "  Installed $DESKTOP_TARGET"
fi

# 7. Target distro & Wayland helper checks (Arch / CachyOS / Fedora + Niri / Hyprland)
DISTRO_ID="unknown"
if [ -f /etc/os-release ]; then
    DISTRO_ID="$(grep -E '^ID=' /etc/os-release | head -n1 | cut -d= -f2 | tr -d '"' | tr '[:upper:]' '[:lower:]')"
fi
MISSING_TOOLS=()
for bin_name in mpv grim slurp wl-paste; do
    if ! command -v "$bin_name" >/dev/null 2>&1; then
        MISSING_TOOLS+=("$bin_name")
    fi
done

echo ""
echo "${MODE^} complete! Launch Cyberdeck with: ./run.sh --gui"
echo "Run diagnostics with: ./run.sh --doctor"
echo "Run tests with:       ./run.sh --test"
if [ "${#MISSING_TOOLS[@]}" -gt 0 ]; then
    echo ""
    echo "Optional Wayland/audio helpers missing: ${MISSING_TOOLS[*]}"
    if [ "$DISTRO_ID" = "fedora" ]; then
        echo "  Fedora install hint: sudo dnf install mpv grim slurp wl-clipboard pipewire-utils portaudio"
    else
        echo "  Arch/CachyOS install hint: sudo pacman -S mpv grim slurp wl-clipboard pipewire-pulse portaudio"
    fi
fi
echo ""
echo "Tip — Global Wayland hotkey for Cyberdeck toggle:"
echo "  Niri (~/.config/niri/config.kdl):      Mod+Shift+C { spawn \"capri-deck\" \"--toggle\"; }"
echo "  Hyprland (~/.config/hypr/hyprland.conf): bind = SUPER SHIFT, C, exec, capri-deck --toggle"
