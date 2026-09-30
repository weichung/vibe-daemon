#!/usr/bin/env python3
"""Batch "content harvester": summarize mock headlines via the webhook.

Loops three English tech headlines through Vibe Daemon, asking for a
one-sentence YouTube Shorts title each time, then writes every response
to tests/output.txt.

    python tests/test_03_batch_agent.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

EXECUTE_URL = "http://127.0.0.1:50051/execute"
TIMEOUT_SECONDS = 120
OUTPUT_PATH = Path(__file__).resolve().parent / "output.txt"

HEADLINES = [
    "Open-source model matches frontier lab scores on a new coding benchmark",
    "Apple Silicon laptop lasts 22 hours in independent video-export test",
    "Researchers warn that silent-room voice agents hallucinate shell commands",
]


def _post(text: str) -> dict:
    response = requests.post(
        EXECUTE_URL,
        json={"text": text},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    lines: list[str] = []
    lines.append("Vibe Daemon batch harvest")
    lines.append("=" * 40)

    for index, headline in enumerate(HEADLINES, start=1):
        command = (
            "Please summarize this tech headline and give me a 1-sentence "
            f"catchy title for a YouTube short: {headline}"
        )
        print(f"[{index}/{len(HEADLINES)}] {headline}")
        try:
            body = _post(command)
        except requests.RequestException as exc:
            print(f"  failed: {exc}", file=sys.stderr)
            lines.append(f"\n## {index}. {headline}\nERROR: {exc}\n")
            continue

        result = body.get("result", "")
        print(f"  status={body.get('status')!r}")
        lines.append(f"\n## {index}. {headline}")
        lines.append(json.dumps(body, indent=2, ensure_ascii=False))
        if result:
            lines.append(str(result))

    OUTPUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
