"""Provider-neutral protocol plus a Chat Completions-compatible HTTP adapter."""

from __future__ import annotations

from typing import Protocol

import httpx


class Model(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> dict: ...


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
        message = response.json()["choices"][0]["message"]
        return {key: message[key] for key in ("role", "content", "tool_calls") if key in message}


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
