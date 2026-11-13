"""Optional computer capabilities. Configuration grants access explicitly."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .processes import capture_output
from .tools import Tool, ToolFeedbackError, object_schema, text_page


def terminal_tool(workspace: Path, backend: str = "local", timeout: float = 30) -> Tool:
    if backend not in {"local", "docker"}:
        raise ValueError("Unknown terminal backend")
    root = workspace.resolve()
    root.mkdir(parents=True, exist_ok=True)

    def run(command: list[str]) -> dict:
        if not command or any(not item or "\0" in item for item in command):
            raise ValueError("Expected non-empty command argv")
        argv, container = command, "evomesh-tool-" + uuid.uuid4().hex
        if backend == "docker":
            argv = [
                "docker",
                "run",
                "--rm",
                "--name",
                container,
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=64",
                "--memory=256m",
                "--cpus=1",
                "--user=65534:65534",
                "--tmpfs=/tmp:rw,nosuid,size=64m",
                "--workdir=/workspace",
                "--mount",
                f"type=bind,src={root},dst=/workspace,readonly",
                "python:3.12-slim",
                *command,
            ]
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "SYSTEMROOT"}}
        with tempfile.TemporaryDirectory(prefix="evomesh-terminal-") as home:
            env["HOME"] = home
            if backend == "docker":
                # Docker CLI needs the operator's selected context, not container credentials.
                for key in ("HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
                    if key in os.environ:
                        env[key] = os.environ[key]
            process = subprocess.Popen(
                argv,
                cwd=root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                code, tail, size = capture_output(process, timeout)
                content = tail.decode(errors="replace")
            finally:
                if backend == "docker":
                    subprocess.run(
                        ["docker", "rm", "-f", container],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                        check=False,
                    )
        result = {
            "exit_code": code,
            "output": content,
            "output_bytes": size,
            "truncated": size > 20000,
        }
        # Bound the serialized result, including escaping and metadata. Preserve
        # the tail (where diagnostics usually finish) and keep the JSON intact.
        while len(json.dumps(result, ensure_ascii=False)) > 18000:
            result["output"] = result["output"][len(result["output"]) // 2 :]
            result["truncated"] = True
        return result

    return Tool(
        "terminal",
        "Run one-shot command argv; its process group is cleaned up on return. "
        "No implicit shell. Returns exit_code, output tail, total "
        "output_bytes and truncated. If truncated, narrow the command output before retrying. "
        + (
            "Read-only workspace in a restricted Docker container."
            if backend == "docker"
            else "Runs locally with the host user's permissions; not a sandbox."
        ),
        object_schema(
            {
                "command": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"type": "string"},
                }
            }
        ),
        run,
        source_paths=("src/evomesh/capabilities.py", "src/evomesh/processes.py"),
        capability="terminal",
        result_is_error=lambda result: json.loads(result)["exit_code"] != 0,
        result_is_truncated=lambda result: json.loads(result)["truncated"],
    )


def execute_code_tool(workspace: Path, backend: str = "local", timeout: float = 30) -> Tool:
    """One-shot Python/JavaScript execution using the terminal's isolation contract."""
    terminal = terminal_tool(workspace, backend, timeout)

    def execute(code: str, language: str = "python") -> dict:
        if language == "python":
            argv = [sys.executable if backend == "local" else "python", "-I", "-c", code]
        elif language == "javascript" and backend == "local":
            argv = ["node", "-e", code]
        else:
            raise ValueError("Docker supports Python; local supports Python and installed Node.js")
        return terminal.handler(argv)

    return Tool(
        "execute_code",
        "Execute a one-shot Python or JavaScript program; print results. "
        "Returns exit_code, output tail, output_bytes and truncated; narrow printed output "
        "if truncated. "
        "No persistent kernel or access to agent credentials. Same permissions as terminal. "
        "Docker supports Python only; local JavaScript requires Node.js.",
        object_schema(
            {
                "code": {"type": "string", "minLength": 1, "maxLength": 50000},
                "language": {"type": "string", "enum": ["python", "javascript"]},
            },
            ["code"],
        ),
        execute,
        source_paths=("src/evomesh/capabilities.py", "src/evomesh/processes.py"),
        capability="execution",
        result_is_error=terminal.result_is_error,
        result_is_truncated=terminal.result_is_truncated,
    )


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


class WebPageChanged(ToolFeedbackError):
    """A web read no longer matches the caller's extracted-text snapshot."""


def web_tool(allowed_hosts: list[str]) -> Tool:
    allowed = {host.lower() for host in allowed_hosts}

    def read(
        url: str, offset: int = 0, limit: int = 12000, expected_sha256: str | None = None
    ) -> dict:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in allowed
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("URL must use HTTPS on an explicitly configured host")
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(
            not ipaddress.ip_address(item[4][0]).is_global for item in addresses
        ):
            raise ValueError("Private network addresses are not allowed")
        # Redirects are not followed, credentials/proxy environment are not inherited.
        # DNS preflight is defense in depth, not an egress firewall or browser sandbox.
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                media_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                if not (media_type.startswith("text/") or media_type == "application/json"):
                    raise ValueError("Only text pages are supported")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 1_000_000:
                        raise ValueError("Page exceeds 1 MB")
                    chunks.append(chunk)
                content = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        if media_type == "text/html":
            parser = PageText()
            parser.feed(content)
            content = "\n".join(parser.parts)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if expected_sha256 is not None and expected_sha256 != digest:
            raise WebPageChanged(
                "Page text changed. Restart at offset zero without expected_sha256; "
                "do not combine pages from different snapshots."
            )
        page = text_page(content, offset, limit, url=url, sha256=digest)
        # Preserve the existing text field while sharing the JSON-safe page budget.
        page["text"] = page.pop("content")
        return page

    return Tool(
        "read_web",
        "Read a text page from a configured public HTTPS host; no JS or login browser. "
        "Returns text, sha256, total_chars and next_offset. Follow next_offset with the "
        "same URL and expected_sha256 to read more; offsets count extracted characters. "
        "Each call refetches the page (1 MB maximum). Restart from zero if its text changes. "
        "HTML is extracted; plain text and JSON are preserved.",
        object_schema(
            {
                "url": {"type": "string", "minLength": 1, "maxLength": 8192},
                "offset": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12000},
                "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            },
            ["url"],
        ),
        read,
        source_paths=("src/evomesh/capabilities.py", "src/evomesh/tools.py"),
        capability="web",
    )
