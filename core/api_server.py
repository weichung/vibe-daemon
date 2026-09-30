"""Local webhook API so external scripts can dispatch text to the Gemini router."""

from __future__ import annotations

import logging

from flask import Flask, jsonify, request

from core.router import VibeRouter

logger = logging.getLogger("vibe-daemon")

DEFAULT_PORT = 50051


def start_server(router: VibeRouter, port: int = DEFAULT_PORT) -> None:
    """Run the Flask webhook (blocking). Intended for a background daemon thread."""
    app = Flask("vibe-daemon-api")

    @app.post("/execute")
    def execute():
        data = request.get_json(silent=True) or {}
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return jsonify(
                {"status": "error", "result": "JSON body must include a non-empty 'text' field."}
            ), 400
        result = router.dispatch_text(data["text"])
        return jsonify({"status": "success", "result": result})

    logger.info("Local webhook listening on http://127.0.0.1:%s/execute", port)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
