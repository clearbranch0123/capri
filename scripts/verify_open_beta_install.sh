#!/usr/bin/env bash
# Capri Open Beta — Isolated Install, Upgrade, Interruption & Uninstall Verifier
# Safely runs inside temporary HOME & XDG_CONFIG_HOME without ever touching user data.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TARBALL="${1:-}"

if [ -z "$TARBALL" ]; then
    TARBALL="$(ls -1 "${ROOT_DIR}/dist"/capri-*-open-beta.tar.gz 2>/dev/null | head -n1 || true)"
fi
if [ -z "$TARBALL" ] || [ ! -f "$TARBALL" ]; then
    echo "Release tarball not found; building first..."
    "$ROOT_DIR/scripts/build_beta_release.sh"
    TARBALL="$(ls -1 "${ROOT_DIR}/dist"/capri-*-open-beta.tar.gz | head -n1)"
fi

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

ISO_HOME="$SANDBOX/home"
ISO_XDG_CONFIG="$SANDBOX/xdg_config"
ISO_XDG_DATA="$SANDBOX/xdg_data"
INSTALL_ROOT="$SANDBOX/extract"
mkdir -p "$ISO_HOME" "$ISO_XDG_CONFIG" "$ISO_XDG_DATA" "$INSTALL_ROOT"

tar -xzf "$TARBALL" -C "$INSTALL_ROOT"
APP_DIR="$(find "$INSTALL_ROOT" -mindepth 1 -maxdepth 1 -type d | head -n1)"

echo "=== VERIFYING CLEAN INSTALL FROM TARBALL (NO NIRI, ISOLATED XDG) ==="
env -u NIRI_SOCKET -u HYPRLAND_INSTANCE_SIGNATURE \
    HOME="$ISO_HOME" \
    XDG_CONFIG_HOME="$ISO_XDG_CONFIG" \
    XDG_DATA_HOME="$ISO_XDG_DATA" \
    QT_QPA_PLATFORM="offscreen" \
    bash "$APP_DIR/setup.sh" --offline

test -f "$ISO_XDG_CONFIG/capri/.env"
test -f "$ISO_XDG_CONFIG/capri/config.yaml"
test -f "$ISO_XDG_DATA/applications/capri-cyberdeck.desktop"

echo "=== VERIFYING IDEMPOTENT RE-RUN ==="
printf 'CAPRI_TEST_KEY=capri_preserved_placeholder_value\n' > "$ISO_XDG_CONFIG/capri/.env"
printf 'companion:\n  user_name: TesterD\n' > "$ISO_XDG_CONFIG/capri/config.yaml"

env -u NIRI_SOCKET -u HYPRLAND_INSTANCE_SIGNATURE \
    HOME="$ISO_HOME" \
    XDG_CONFIG_HOME="$ISO_XDG_CONFIG" \
    XDG_DATA_HOME="$ISO_XDG_DATA" \
    bash "$APP_DIR/setup.sh" --offline

grep -q "capri_preserved_placeholder_value" "$ISO_XDG_CONFIG/capri/.env"
grep -q "TesterD" "$ISO_XDG_CONFIG/capri/config.yaml"

echo "=== VERIFYING CLI EXECUTION FROM /tmp ==="
(
    cd /tmp
    env -u NIRI_SOCKET -u HYPRLAND_INSTANCE_SIGNATURE \
        HOME="$ISO_HOME" \
        XDG_CONFIG_HOME="$ISO_XDG_CONFIG" \
        XDG_DATA_HOME="$ISO_XDG_DATA" \
        QT_QPA_PLATFORM="offscreen" \
        PYTHONPATH="$APP_DIR" \
        "$ROOT_DIR/.venv/bin/python" -m capri.cli --doctor >/dev/null
)

echo "=== VERIFYING --upgrade & SIMULATED INTERRUPTED UPGRADE ==="
env HOME="$ISO_HOME" XDG_CONFIG_HOME="$ISO_XDG_CONFIG" XDG_DATA_HOME="$ISO_XDG_DATA" \
    bash "$APP_DIR/setup.sh" --upgrade --offline

ls -d "$ISO_XDG_CONFIG/capri/backups/pre_upgrade_"* >/dev/null

if env HOME="$ISO_HOME" XDG_CONFIG_HOME="$ISO_XDG_CONFIG" XDG_DATA_HOME="$ISO_XDG_DATA" \
    CAPRI_SIMULATE_UPGRADE_FAIL=1 bash "$APP_DIR/setup.sh" --upgrade --offline 2>/dev/null; then
    echo "ERROR: Simulated failed upgrade should have returned non-zero!" >&2
    exit 1
fi
grep -q "capri_preserved_placeholder_value" "$ISO_XDG_CONFIG/capri/.env"
grep -q "TesterD" "$ISO_XDG_CONFIG/capri/config.yaml"

echo "=== VERIFYING --uninstall (PRESERVES DATA) & --uninstall --purge ==="
env HOME="$ISO_HOME" XDG_CONFIG_HOME="$ISO_XDG_CONFIG" XDG_DATA_HOME="$ISO_XDG_DATA" \
    bash "$APP_DIR/setup.sh" --uninstall

test ! -e "$ISO_XDG_DATA/applications/capri-cyberdeck.desktop"
test ! -d "$APP_DIR/.venv"
test -f "$ISO_XDG_CONFIG/capri/.env"

env HOME="$ISO_HOME" XDG_CONFIG_HOME="$ISO_XDG_CONFIG" XDG_DATA_HOME="$ISO_XDG_DATA" \
    bash "$APP_DIR/setup.sh" --uninstall --purge

test ! -d "$ISO_XDG_CONFIG/capri"

echo "ALL OPEN BETA RELEASE ENGINEERING CHECKS PASSED!"
