#!/usr/bin/env python3
"""Backward-compatible launcher. Prefer `python run_mac.py`."""

from __future__ import annotations

import sys

from run_mac import main

if __name__ == "__main__":
    sys.exit(main())
