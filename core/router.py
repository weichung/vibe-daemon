"""Intent router: classify spoken audio and dispatch to IDE, shell, or chat."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from typing import Any, Optional

from google import genai
from google.genai import types

from core.google_auth import get_google_credentials

logger = logging.getLogger("vibe-daemon")

_ALLOWED_GOOGLE_SERVICES = frozenset(
    {"calendar", "gmail", "drive", "docs", "sheets", "youtube"}
)
_SAFE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_MAX_METHOD_DEPTH = 8

ROUTER_MODEL = "gemini-3.8-flash"
ROUTER_SYSTEM_INSTRUCTION = (
    "You are a macOS system router with a two-tier execution policy. "
    "Listen to the audio (or read the text) and call exactly one appropriate tool. "
    "For coding, extract the instruction. For OS tasks, write the correct "
    "bash/zsh command. For general chat, provide a concise response. Always "
    "reply to conversational queries in the exact same language that the user "
    "spoke (e.g., reply in Traditional Chinese if the user spoke Chinese, and "
    "English if the user spoke English). "
    "TWO-TIER ROUTING: "
    "Rule X (Fast API Path): Always attempt to fulfill the user's request "
    "using standard tools first — especially `_tool_execute_shell` for OS, "
    "filesystem, app-launch, and CLI/API tasks, `_tool_route_to_ide` for "
    "coding/file edits, and `_tool_conversation` for questions. "
    "Rule Y (Slow Computer Use Path): If the user asks to control a specific "
    "GUI application, interact with a website that lacks a CLI/API, or "
    "perform an action that requires visual or mouse control (clicking "
    "buttons, filling GUI forms, dragging windows, navigating a browser "
    "UI), you MUST fall back to `_tool_computer_use` and pass the user's "
    "exact instruction. Do not invent a shell command that cannot actually "
    "complete a visual/GUI task. "
    "GOOGLE WORKSPACE: For Google Calendar, Gmail, Drive, Docs, Sheets, or "
    "YouTube, call the single universal tool `_tool_google_workspace_api`. "
    "Do not invent a separate tool and do not use the shell or Computer Use "
    "for these services. Arguments: `service_name` (e.g. 'calendar'), "
    "`version` (e.g. 'v3'), `method_path` (dot or call notation, e.g. "
    "'events.list' or 'events().insert'), and `kwargs` (the keyword arguments "
    "for that method). Example — list upcoming primary-calendar events: "
    "service_name='calendar', version='v3', method_path='events.list', "
    "kwargs={'calendarId': 'primary', 'maxResults': 10, 'singleEvents': True, "
    "'orderBy': 'startTime'}. Other service_name values: 'gmail' (v1), "
    "'drive' (v3), 'docs' (v1), 'sheets' (v4), 'youtube' (v3)."
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
        self.tools = [
            self._tool_route_to_ide,
            self._tool_execute_shell,
            self._tool_conversation,
            self._tool_computer_use,
            self._tool_google_workspace_api,
        ]
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

    def _tool_computer_use(self, instruction: str) -> str:
        """Fall back to visual/GUI computer use when shell and APIs cannot help.

        Use this Slow Computer Use Path only when standard tools are insufficient:
        controlling a specific GUI application, interacting with a website that
        has no CLI/API, or any action that needs mouse, keyboard, or on-screen
        visual control (clicking buttons, filling GUI forms, dragging windows).

        Args:
            instruction: The user's exact original instruction for GUI automation.

        Returns:
            A status string from the computer-use backend.
        """
        logger.warning(f"Computer Use fallback triggered for: {instruction}")
        return (
            "ERROR: Computer Use API is currently a placeholder and not yet implemented."
        )

    def _tool_google_workspace_api(
        self,
        service_name: str,
        version: str,
        method_path: str,
        kwargs: dict | None = None,
    ) -> str:
        """Call Google Calendar, Gmail, Drive, Docs, Sheets, or YouTube.

        Use this universal tool for any Google Workspace request. Do not use
        the shell or computer-use tools for these services.

        Args:
            service_name: Discovery name, e.g. 'calendar', 'gmail', 'drive',
                'docs', 'sheets', or 'youtube'.
            version: API version, e.g. 'v3' for calendar, 'v1' for gmail/docs,
                'v3' for drive/youtube, 'v4' for sheets.
            method_path: Resource chain in dot or call notation, e.g.
                'events.list' or 'events().insert'.
            kwargs: Keyword arguments for the final method, e.g.
                {'calendarId': 'primary', 'maxResults': 10}.

        Returns:
            JSON text of the API response, or a JSON error object.
        """
        logger.info(
            "Google Workspace API: %s %s %s",
            service_name,
            version,
            method_path,
        )
        try:
            from googleapiclient.discovery import build
            from googleapiclient.errors import HttpError
        except ImportError as exc:
            return json.dumps(
                {"error": f"Google API libraries are not installed: {exc}"}
            )
        try:
            payload = _normalize_kwargs(kwargs)
            _validate_google_target(service_name, version, method_path)
            creds = get_google_credentials()
            service = build(service_name, version, credentials=creds)
            result = _execute_method_path(service, method_path, payload)
            return json.dumps(result, default=str, ensure_ascii=False)
        except HttpError as exc:
            logger.exception("Google API HTTP error")
            return json.dumps({"error": _http_error_detail(exc)})
        except Exception as exc:
            logger.exception("Google Workspace API call failed")
            return json.dumps({"error": str(exc)})

    def _routing_config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=ROUTER_SYSTEM_INSTRUCTION,
            tools=self.tools,
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

        tools = {tool.__name__: tool for tool in self.tools}
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


def _normalize_kwargs(kwargs: object) -> dict[str, Any]:
    if kwargs is None:
        return {}
    if isinstance(kwargs, str):
        parsed = json.loads(kwargs)
        if not isinstance(parsed, dict):
            raise TypeError("kwargs JSON must be an object")
        return parsed
    if isinstance(kwargs, Mapping):
        return dict(kwargs)
    raise TypeError("kwargs must be a dict")


def _validate_google_target(service_name: str, version: str, method_path: str) -> None:
    if service_name not in _ALLOWED_GOOGLE_SERVICES:
        allowed = ", ".join(sorted(_ALLOWED_GOOGLE_SERVICES))
        raise ValueError(f"Unsupported Google service '{service_name}'. Allowed: {allowed}")
    if not _SAFE_VERSION.fullmatch(version or ""):
        raise ValueError(f"Invalid API version: {version!r}")
    parts = _method_parts(method_path)
    if not parts:
        raise ValueError("method_path is empty")
    if len(parts) > _MAX_METHOD_DEPTH:
        raise ValueError("method_path is too deep")
    for part in parts:
        if part.startswith("_") or not _SAFE_TOKEN.fullmatch(part):
            raise ValueError(f"Invalid method_path segment: {part!r}")


def _method_parts(method_path: str) -> list[str]:
    """Split 'events().insert' or 'events.insert' into ['events', 'insert']."""
    normalized = (method_path or "").replace("()", "")
    return [part.strip() for part in normalized.split(".") if part.strip()]


def _execute_method_path(service: object, method_path: str, kwargs: dict[str, Any]) -> Any:
    """Walk a discovery resource chain and execute the final request.

    Intermediate segments are called with no arguments (``events()``).
    Only the last segment receives ``kwargs``, then ``execute()``.
    """
    target = service
    parts = _method_parts(method_path)
    for index, part in enumerate(parts):
        if not hasattr(target, part):
            raise AttributeError(f"Google API has no attribute '{part}' on {type(target).__name__}")
        attr = getattr(target, part)
        if not callable(attr):
            raise TypeError(f"'{part}' is not callable")
        if index < len(parts) - 1:
            target = attr()
            continue
        request = attr(**kwargs)
        execute = getattr(request, "execute", None)
        if not callable(execute):
            raise TypeError(f"'{part}' did not return an executable Google API request")
        return execute()
    raise ValueError("method_path is empty")


def _http_error_detail(exc: Exception) -> str:
    content = getattr(exc, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    if content:
        return str(content)
    return str(exc)
