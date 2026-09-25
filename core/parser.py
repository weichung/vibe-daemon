"""Parsers for agent response tags (TTS summary and speech transcript)."""

from __future__ import annotations

import re


def _extract_tts_summary(text: str) -> str:
    """Pull a short spoken summary from <tts> tags, with a markdown fallback."""
    tagged = re.search(r"<tts>(.*?)</tts>", text, flags=re.IGNORECASE | re.DOTALL)
    if tagged:
        return tagged.group(1).replace("\n", " ").strip()

    cleaned = re.sub(r"```[\s\S]*?```", " ", text)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cleaned)
    cleaned = re.sub(r"[*`]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    sentence = re.match(r"(.+?[.!?])(\s|$)", cleaned)
    first = sentence.group(1).strip() if sentence else cleaned
    return first[:200]


def _extract_transcript(text: str) -> str:
    """Return the user's recognized speech from <transcript> tags, or empty."""
    tagged = re.search(
        r"<transcript>(.*?)</transcript>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not tagged:
        return ""
    return tagged.group(1).replace("\n", " ").strip()
