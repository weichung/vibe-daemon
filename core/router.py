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
WORKSPACE_DIR = os.path.expanduser("~/vibe_workspace")
HOME_DIR = os.path.expanduser("~")
SHELL_TIMEOUT_SECONDS = 15
_SHELL_BLOCKLIST = (
    "sudo ",
    "rm -rf /",
    "mkfs",
    "> ~/.bashrc",
)
_SHELL_BLOCKED_MESSAGE = (
    "Command blocked for safety reasons. Do not use sudo or destructive commands."
)

ROUTER_MODEL = "gemini-3.8-flash"
MAX_TURNS = 3
ROUTER_SYSTEM_INSTRUCTION = (
    "You are a macOS system router with a two-tier execution policy. "
    "Listen to the audio (or read the text). On each turn, call one appropriate "
    "tool, or reply in natural language with no function call once the task is "
    "finished. After a tool result, you may call a tool again to correct an error. "
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
    "'drive' (v3), 'docs' (v1), 'sheets' (v4), 'youtube' (v3). "
    "GOOGLE API PARAMETER CHEAT SHEET: When calling "
    "`_tool_google_workspace_api`, you MUST put these service-specific keys "
    "in `kwargs`: "
    "Calendar — always include \"calendarId\": \"primary\". When listing "
    "events, also include \"timeMin\" and \"timeMax\" in RFC3339 format "
    "(example: \"2026-10-02T00:00:00Z\"). "
    "Gmail — always include \"userId\": \"me\". "
    "YouTube — for list and search methods, always include \"part\" "
    "(example: \"part\": \"snippet,contentDetails\"). "
    "Drive — for files.list, specify \"spaces\": \"drive\". "
    "Sheets — always include \"spreadsheetId\". "
    "Docs — always include \"documentId\". "
    "If the tool returns {\"status\": \"error\", ...}, read \"message\", "
    "fix the missing parameter or invalid format in kwargs, and call "
    "`_tool_google_workspace_api` again. "
    "When using `_tool_execute_shell`, you are running in a macOS sandbox. "
    "You can read any file and execute applications, but you are ONLY allowed "
    "to write/modify files inside `~/vibe_workspace`. Any attempt to write "
    "outside this directory will be blocked by the OS. Keep commands under "
    "15 seconds."
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
        os.makedirs(WORKSPACE_DIR, exist_ok=True)
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
        """Run a bash command under a macOS Seatbelt profile.

        Use this for OS-level tasks such as listing files or running shell
        commands. You can read any file and execute applications, but writes
        are allowed only inside ~/vibe_workspace. Commands time out after 15
        seconds. Do not use sudo or destructive system commands.

        Args:
            command: A complete bash command. It runs via sandbox-exec.

        Returns:
            Combined stdout and stderr, or a JSON error if the command is
            blocked or times out.
        """
        logger.info("Execute shell in %s: %s", WORKSPACE_DIR, command)
        if _shell_command_blocked(command):
            logger.warning("Blocked shell command: %s", command)
            return _shell_tool_error(
                _SHELL_BLOCKED_MESSAGE,
                "Choose a non-destructive command. Writes must stay inside ~/vibe_workspace.",
            )
        profile = _seatbelt_profile()
        try:
            completed = subprocess.run(
                ["sandbox-exec", "-p", profile, "/bin/bash", "-c", command],
                capture_output=True,
                text=True,
                cwd=WORKSPACE_DIR,
                timeout=SHELL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "Shell command timed out after %ss: %s",
                SHELL_TIMEOUT_SECONDS,
                command,
            )
            return _shell_tool_error(
                f"Command timed out after {SHELL_TIMEOUT_SECONDS} seconds.",
                "Run a faster command, or split the work into a smaller step.",
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
            kwargs: Keyword arguments for the final method. Required keys:
                Calendar always needs calendarId='primary' plus timeMin and
                timeMax (RFC3339) when listing events; Gmail always needs
                userId='me'; YouTube list/search needs part (e.g.
                'snippet,contentDetails'); Drive files.list needs
                spaces='drive'; Sheets needs spreadsheetId; Docs needs
                documentId.

        Returns:
            JSON text of the API response, or a JSON error object the caller
            can correct and retry.
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
            try:
                result = _execute_method_path(service, method_path, payload)
            except TypeError as exc:
                logger.warning("Google API missing or invalid parameter: %s", exc)
                return _google_tool_error(exc)
            except HttpError as exc:
                logger.warning("Google API HTTP error: %s", exc)
                return _google_tool_error(exc)
            return json.dumps(result, default=str, ensure_ascii=False)
        except (TypeError, HttpError) as exc:
            logger.warning("Google Workspace API error: %s", exc)
            return _google_tool_error(exc)
        except Exception as exc:
            logger.exception("Google Workspace API call failed")
            return _google_tool_error(exc)

    def _routing_config(self) -> types.GenerateContentConfig:
        # AUTO lets a later turn answer in text. ANY would force a tool call
        # on every turn, so the model could never finish the loop.
        return types.GenerateContentConfig(
            system_instruction=ROUTER_SYSTEM_INSTRUCTION,
            tools=self.tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO")
            ),
        )

    def _execute_function_calls(self, response) -> tuple[list[types.Part], str, bool]:
        """Run each function call and build user-role function-response parts.

        Returns the parts to append, a joined summary, and whether
        ``_tool_conversation`` already spoke to the user.
        """
        calls = response.function_calls or []
        tools = {tool.__name__: tool for tool in self.tools}
        parts: list[types.Part] = []
        summaries: list[str] = []
        conversation_spoken = False
        for call in calls:
            name = call.name or ""
            args = dict(call.args or {})
            logger.info("Router function call: %s(%s)", name, args)
            tool = tools.get(name)
            if tool is None:
                result = f"Unknown tool: {name}"
            else:
                try:
                    result = str(tool(**args))
                except Exception as exc:
                    logger.exception("Tool %s failed", name)
                    result = json.dumps(
                        {
                            "status": "error",
                            "message": str(exc),
                            "suggestion": (
                                "Analyze the error, adjust the arguments, "
                                "and call the tool again."
                            ),
                        }
                    )
            if name == "_tool_conversation":
                conversation_spoken = True
            summaries.append(result.strip() or name)
            part = types.Part.from_function_response(
                name=name,
                response=_function_response_body(result),
            )
            call_id = getattr(call, "id", None)
            if call_id and part.function_response is not None:
                part.function_response.id = call_id
            parts.append(part)
        return parts, "; ".join(summaries), conversation_spoken

    def _agent_loop(self, contents: list[types.Content]) -> str:
        """Send history to Gemini until it answers in text or the turn cap hits."""
        last_summary = ""
        conversation_spoken = False
        for turn in range(1, MAX_TURNS + 1):
            logger.info("Router turn %d/%d", turn, MAX_TURNS)
            try:
                response = self.client.models.generate_content(
                    model=ROUTER_MODEL,
                    contents=contents,
                    config=self._routing_config(),
                )
            except Exception:
                logger.exception("Gemini routing request failed")
                return last_summary or "Routing failed."

            calls = response.function_calls or []
            if not calls:
                text = (response.text or "").strip()
                if not text:
                    return last_summary or "No route selected."
                if not conversation_spoken and self.on_conversation is not None:
                    self.on_conversation(text)
                return text

            model_content = _model_content(response)
            if model_content is None:
                logger.warning("Function call missing model content; stopping loop")
                _parts, summary, _spoken = self._execute_function_calls(response)
                return summary or last_summary or "No route selected."

            contents.append(model_content)
            parts, summary, spoke = self._execute_function_calls(response)
            last_summary = summary or last_summary
            conversation_spoken = conversation_spoken or spoke
            if not parts:
                return last_summary or "No route selected."
            contents.append(types.Content(role="user", parts=parts))

        logger.warning("Router reached MAX_TURNS=%d", MAX_TURNS)
        return last_summary or "No route selected."

    def dispatch_audio(self, wav_bytes: bytes) -> str:
        """Classify spoken audio, run tools, and let the model repair errors."""
        if not self.client:
            return "API key is missing. Please set it in Preferences."
        self._pending_wav_bytes = wav_bytes
        audio_part = types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")
        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text="Listen to this audio and call the appropriate tool."
                    ),
                    audio_part,
                ],
            )
        ]
        return self._agent_loop(contents)

    def dispatch_text(self, text: str) -> str:
        """Classify a text command, run tools, and let the model repair errors."""
        if not self.client:
            return "API key is missing. Please set it in Preferences."
        contents = [
            types.Content(role="user", parts=[types.Part.from_text(text=text)])
        ]
        return self._agent_loop(contents)


def _seatbelt_profile() -> str:
    """Allow reads and exec, but deny home-directory writes outside the workspace."""
    home = HOME_DIR.replace("\\", "\\\\").replace('"', '\\"')
    workspace = WORKSPACE_DIR.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "(version 1)\n"
        "(allow default)\n"
        f'(deny file-write* (subpath "{home}"))\n'
        f'(allow file-write* (subpath "{workspace}"))\n'
    )


def _shell_command_blocked(command: str) -> bool:
    lowered = command.lower()
    return any(keyword in lowered for keyword in _SHELL_BLOCKLIST)


def _shell_tool_error(message: str, suggestion: str) -> str:
    return json.dumps(
        {"status": "error", "message": message, "suggestion": suggestion},
        ensure_ascii=False,
    )


def _model_content(response) -> types.Content | None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates or candidates[0].content is None:
        return None
    content = candidates[0].content
    if not getattr(content, "role", None):
        content.role = "model"
    return content


def _function_response_body(result: str) -> dict[str, Any]:
    """Turn a tool string into the JSON object FunctionResponse requires."""
    text = "" if result is None else str(result)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"output": text}
    if isinstance(parsed, dict):
        return parsed
    return {"output": parsed}


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


_GOOGLE_RETRY_SUGGESTION = (
    "Analyze the missing parameter or invalid format, update your kwargs, "
    "and call this tool again."
)


def _google_tool_error(exc: BaseException) -> str:
    """Return a non-fatal JSON error the model can correct on the next call."""
    return json.dumps(
        {
            "status": "error",
            "message": str(exc),
            "suggestion": _GOOGLE_RETRY_SUGGESTION,
        },
        ensure_ascii=False,
    )
