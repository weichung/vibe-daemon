"""Intent router: classify spoken audio and dispatch to IDE, shell, or chat."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger("vibe-daemon")

ROUTER_MODEL = "gemini-3.8-flash"
ROUTER_SYSTEM_INSTRUCTION = (
    "You are a macOS system router. Listen to the audio and call the "
    "appropriate tool. For coding, extract the instruction. For OS tasks, "
    "write the correct bash/zsh command. For general chat, provide a concise "
    "response. Always reply to conversational queries in the exact same "
    "language that the user spoke (e.g., reply in Traditional Chinese if the "
    "user spoke Chinese, and English if the user spoke English)."
)

OnRouteToIde = Callable[[bytes], None]
OnText = Callable[[str], None]


class VibeRouter:
    """Gemini function-calling router over captured microphone audio."""

    def __init__(self) -> None:
        self.client = None
        self.on_route_to_ide: Optional[OnRouteToIde] = None
        self.on_shell_executed: Optional[OnText] = None
        self.on_conversation: Optional[OnText] = None
        self._pending_wav_bytes: bytes = b""
        if os.environ.get("GEMINI_API_KEY"):
            try:
                self.client = genai.Client()
            except Exception as e:
                logger.error("Failed to initialize Gemini client: %s", e)

    def reload_client(self) -> None:
        """Rebuild the Gemini client after GEMINI_API_KEY changes."""
        self.client = None
        if os.environ.get("GEMINI_API_KEY"):
            try:
                self.client = genai.Client()
                logger.info("Gemini client reloaded")
            except Exception as e:
                logger.error("Failed to initialize Gemini client: %s", e)
        else:
            logger.warning("GEMINI_API_KEY missing; Gemini client not initialized")

    def _tool_route_to_ide(self, instruction: str) -> str:
        """Route a coding or file-editing request to the Antigravity IDE.

        Use this when the user wants to write code, modify files, or use the IDE.

        Args:
            instruction: The user's coding instruction extracted from speech.

        Returns:
            A short confirmation that the request was forwarded to the IDE.
        """
        logger.info("Route to IDE: %s", instruction)
        if self.on_route_to_ide is not None:
            self.on_route_to_ide(self._pending_wav_bytes)
        return f"Routed to IDE: {instruction}"

    def _tool_execute_shell(self, command: str) -> str:
        """Run an OS-level bash/zsh command on this Mac.

        Use this for OS-level tasks such as opening apps, listing files,
        checking system status, or running shell commands.

        Args:
            command: A complete bash/zsh command to execute.

        Returns:
            Combined stdout and stderr from the command.
        """
        logger.info("Execute shell: %s", command)
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
        )
        result = ((completed.stdout or "") + (completed.stderr or "")).strip()
        if not result:
            result = f"Command exited with status {completed.returncode}."
        if self.on_shell_executed is not None:
            self.on_shell_executed(result)
        return result

    def _tool_conversation(self, response_text: str) -> str:
        """Answer a general question that does not need the IDE or the shell.

        Use this for general questions, explanations, and casual chat.

        Args:
            response_text: A concise spoken reply for the user.

        Returns:
            The conversational reply that should be spoken back.
        """
        logger.info("Conversation: %s", response_text)
        if self.on_conversation is not None:
            self.on_conversation(response_text)
        return response_text

    def _routing_config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=ROUTER_SYSTEM_INSTRUCTION,
            tools=[
                self._tool_route_to_ide,
                self._tool_execute_shell,
                self._tool_conversation,
            ],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")
            ),
        )

    def _execute_function_calls(self, response) -> str:
        calls = response.function_calls or []
        if not calls:
            fallback = (response.text or "").strip()
            logger.warning("Router returned no function call; text=%s", fallback)
            if fallback and self.on_conversation is not None:
                self.on_conversation(fallback)
            return fallback or "No route selected."

        tools = {
            "_tool_route_to_ide": self._tool_route_to_ide,
            "_tool_execute_shell": self._tool_execute_shell,
            "_tool_conversation": self._tool_conversation,
        }
        summaries: list[str] = []
        for call in calls:
            name = call.name or ""
            args = dict(call.args or {})
            logger.info("Router function call: %s(%s)", name, args)
            tool = tools.get(name)
            if tool is None:
                summaries.append(f"Unknown tool: {name}")
                continue
            try:
                result = tool(**args)
            except Exception:
                logger.exception("Tool %s failed", name)
                summaries.append(f"{name} failed")
                continue
            summaries.append(str(result).strip() or name)
        return "; ".join(summaries)

    def dispatch_audio(self, wav_bytes: bytes) -> str:
        """Classify spoken audio and execute the matching tool. Returns a summary."""
        if not self.client:
            return "API key is missing. Please set it in Preferences."
        self._pending_wav_bytes = wav_bytes
        audio_part = types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")
        try:
            response = self.client.models.generate_content(
                model=ROUTER_MODEL,
                contents=[
                    "Listen to this audio and call the appropriate tool.",
                    audio_part,
                ],
                config=self._routing_config(),
            )
        except Exception:
            logger.exception("Gemini routing request failed")
            return "Routing failed."
        return self._execute_function_calls(response)

    def dispatch_text(self, text: str) -> str:
        """Classify a text command and execute the matching tool. Returns a summary."""
        if not self.client:
            return "API key is missing. Please set it in Preferences."
        try:
            response = self.client.models.generate_content(
                model=ROUTER_MODEL,
                contents=[text],
                config=self._routing_config(),
            )
        except Exception:
            logger.exception("Gemini text routing request failed")
            return "Routing failed."
        return self._execute_function_calls(response)
