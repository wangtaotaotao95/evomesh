"""Optional MCP v2 stdio tool bridge with a persistent client lifecycle."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from concurrent.futures import TimeoutError as FutureTimeout

from .tools import Tool, ToolFeedbackError


class MCPToolError(ToolFeedbackError):
    """A remote tool returned is_error; transport/protocol failures remain exceptions."""


class MCPConnection:
    """One trusted configured server, a persistent session, and namespaced tools.

    Servers run as the current OS user. Configure only trusted executables.
    Resources, prompts, sampling, elicitation and remote OAuth are not exposed.
    """

    def __init__(self, name: str, command: list[str], timeout: float = 30):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,20}", name) or not command:
            raise ValueError("MCP needs a short namespace and command argv")
        self.name, self.command, self.timeout = name, command, timeout
        self.ready = threading.Event()
        self.error = None
        self.tools: list[Tool] = []
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout + 5):
            self.close()
            raise TimeoutError("MCP startup timed out")
        if self.error:
            self.close()
            raise RuntimeError(
                "MCP startup failed; check server command and optional mcp dependency"
            )

    def _thread(self):
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            self.error = exc
            self.ready.set()

    async def _serve(self):
        from mcp import Client, StdioServerParameters

        self.loop = asyncio.get_running_loop()
        self.main_task = asyncio.current_task()
        self.stop = asyncio.Event()
        params = StdioServerParameters(command=self.command[0], args=self.command[1:], env={})
        async with Client(params, read_timeout_seconds=self.timeout) as client:
            self.client = client
            cursor = None
            for _ in range(20):
                result = await client.list_tools(cursor=cursor)
                for remote in result.tools:
                    name = f"{self.name}__{remote.name}"
                    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
                        raise ValueError("MCP tool name is not compatible with this model adapter")

                    def handler_for(remote_name):
                        def invoke(**arguments):
                            return self.call(remote_name, arguments)

                        return invoke

                    self.tools.append(
                        Tool(
                            name,
                            remote.description or remote.name,
                            remote.input_schema,
                            handler_for(remote.name),
                            source_paths=("src/evomesh/mcp.py",),
                            capability="mcp",
                        )
                    )
                cursor = result.next_cursor
                if not cursor:
                    break
            else:
                raise ValueError("MCP tool catalog exceeds 20 pages")
            self.ready.set()
            await self.stop.wait()

    def call(self, name: str, arguments: dict):
        if not self.thread.is_alive() or self.error:
            raise RuntimeError("MCP connection is closed")

        async def invoke():
            result = await self.client.call_tool(name, arguments)
            if result.is_error:
                feedback = "\n".join(block.text for block in result.content if block.type == "text")
                if not feedback and result.structured_content is not None:
                    feedback = json.dumps(result.structured_content, ensure_ascii=False)
                raise MCPToolError(feedback or "Remote tool failed without text details")
            if result.structured_content is not None:
                return result.structured_content
            return "\n".join(block.text for block in result.content if block.type == "text")

        future = asyncio.run_coroutine_threadsafe(invoke(), self.loop)
        try:
            return future.result(self.timeout + 5)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError("MCP call timed out") from None

    def close(self):
        if hasattr(self, "loop") and self.loop.is_running():
            if self.ready.is_set():
                self.loop.call_soon_threadsafe(self.stop.set)
            else:
                self.loop.call_soon_threadsafe(self.main_task.cancel)
        self.thread.join(timeout=10)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
