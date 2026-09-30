#!/usr/bin/env python3
"""Two-step pipeline: shell fact, then a sarcastic Chinese summary.

Step 1 asks Vibe Daemon to fetch the macOS version via the shell route.
Step 2 takes that result and asks for a funny, sarcastic Traditional Chinese
summary — a small multi-turn pattern you can extend later.

    python tests/test_02_pipeline.py
"""

from __future__ import annotations

import json
import sys

import requests

EXECUTE_URL = "http://127.0.0.1:50051/execute"
TIMEOUT_SECONDS = 120

STEP_1 = (
    "Run a shell command to print the macOS system version "
    "(for example sw_vers or sw_vers -productVersion)."
)


def _post(text: str) -> dict:
    response = requests.post(
        EXECUTE_URL,
        json={"text": text},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    print("=== Step 1: fetch macOS version via Vibe Daemon ===")
    try:
        step1 = _post(STEP_1)
    except requests.RequestException as exc:
        print(f"Step 1 failed: {exc}", file=sys.stderr)
        print("Is Vibe Daemon running?", file=sys.stderr)
        return 1

    print(json.dumps(step1, indent=2, ensure_ascii=False))
    system_info = str(step1.get("result", "")).strip()
    if not system_info:
        print("Step 1 returned an empty result; aborting.", file=sys.stderr)
        return 1

    step2_prompt = (
        "Translate the following macOS system information into a funny, "
        "sarcastic summary in Traditional Chinese. Keep it to 1-3 sentences.\n\n"
        f"{system_info}"
    )
    print("\n=== Step 2: sarcastic Traditional Chinese summary ===")
    try:
        step2 = _post(step2_prompt)
    except requests.RequestException as exc:
        print(f"Step 2 failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(step2, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
