#!/usr/bin/env python3
"""Single-shot webhook demo: one text command to Vibe Daemon.

Requires the daemon to be running (`python run_mac.py`) so the local
Flask server is listening on http://127.0.0.1:50051/execute.

    pip install requests
    python tests/test_01_single_shot.py
"""

from __future__ import annotations

import json
import sys

import requests

EXECUTE_URL = "http://127.0.0.1:50051/execute"
COMMAND = "List the files in my Desktop directory"


def main() -> int:
    payload = {"text": COMMAND}
    print(f"POST {EXECUTE_URL}")
    print(f"payload: {payload}\n")
    try:
        response = requests.post(EXECUTE_URL, json=payload, timeout=120)
    except requests.RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        print("Is Vibe Daemon running?", file=sys.stderr)
        return 1

    print(f"HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        print(response.text)
        return 1
    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
