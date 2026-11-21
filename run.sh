#!/usr/bin/env bash
# Capri AI Companion Launcher
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"

if [ -f ".venv/bin/python" ]; then
    if [ "$1" = "--avatar" ]; then
        exec .venv/bin/python -m capri.ui.overlay
    elif [ "$1" = "--gui" ] || [ "$1" = "--deck" ] || [ "$1" = "--cyberdeck" ]; then
        shift
        exec .venv/bin/python -m capri.ui.cyberdeck "$@"
    elif [ "$1" = "--test" ] || [ "$1" = "--tests" ]; then
        shift
        exec .venv/bin/pytest "$@"
    elif [ "$1" = "--version" ] || [ "$1" = "-v" ] || [ "$1" = "-V" ]; then
        exec .venv/bin/python main.py --version
    fi
    exec .venv/bin/python main.py "$@"
else
    echo "Virtual environment not found! Run setup first."
    exit 1
fi
