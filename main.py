#!/usr/bin/env python3
"""Backwards-compatible root launcher delegating to `capri.cli`."""

from capri.cli import cli_main, main

if __name__ == "__main__":
    raise SystemExit(cli_main())
