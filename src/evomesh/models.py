"""Provider-neutral protocol plus a Chat Completions-compatible HTTP adapter."""

from __future__ import annotations

from typing import Protocol

import httpx


class Model(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> dict: ...


class InvalidModelResponse(ValueError):
    """A model reply cannot safely participate in the assistant/tool protocol."""


def validate_message(message: dict) -> dict:
    """Check the entire reply before any tool executes; never echo provider data."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise InvalidModelResponse("Expected an assistant message object")
    for field in ("content", "reasoning_content"):
        if message.get(field) is not None and not isinstance(message[field], str):
            raise InvalidModelResponse("Assistant text fields must be strings or null")
    calls = message.get("tool_calls")
    if calls is None:
        return message
    if not isinstance(calls, list):
        raise InvalidModelResponse("tool_calls must be an array or null")
    identifiers = set()
    for call in calls:
        # Existing custom adapters can omit type; explicit unsupported types fail.
        if not isinstance(call, dict) or call.get("type", "function") != "function":
            raise InvalidModelResponse("Expected a function tool call object")
        identifier = call.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise InvalidModelResponse("Tool call IDs must be nonempty and unique within a reply")
        identifiers.add(identifier)
        function = call.get("function")
        if (
            not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not function["name"]
            or not isinstance(function.get("arguments"), str)
        ):
            raise InvalidModelResponse("Function calls require a name and string arguments")
    return message


class ChatModel:
    def __init__(self, model: str, base_url: str, api_key: str = "", timeout: float = 90):
        self.model, self.base_url, self.api_key = model, base_url.rstrip("/"), api_key
        self.timeout = timeout

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        body = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        # Do not retry POSTs automatically: a retry can duplicate billable inference.
        response = httpx.post(
            f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout
        )
        if response.is_error:
            # Provider bodies and request URLs can contain secrets; keep diagnostics minimal.
            raise RuntimeError(f"Model endpoint returned HTTP {response.status_code}")
        message = validate_message(response.json()["choices"][0]["message"])
        # DeepSeek thinking-mode tool calls require this provider field to be
        # round-tripped with assistant messages, including across resumed turns.
        return {
            key: message[key]
            for key in ("role", "content", "tool_calls", "reasoning_content")
            if key in message
        }


class DemoModel:
    """Deterministic offline smoke demo; deliberately not an LLM."""

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": "Workspace files: " + messages[-1]["content"]}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "demo-list",
                    "type": "function",
                    "function": {
                        "name": "list_files",
                        "arguments": '{"path":"."}',
                    },
                }
            ],
        }
