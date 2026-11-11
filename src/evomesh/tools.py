"""Small, typed tool registry; applications can register arbitrary tools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Any]
    source_paths: tuple[str, ...] = ()
    capability: str = "tools"
    result_is_error: Callable[[str], bool] | None = None
    result_is_truncated: Callable[[str], bool] | None = None

    def __post_init__(self):
        import re

        from .evolution import safe_path

        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.capability):
            raise ValueError("Capability must be a lowercase identifier")
        for path in self.source_paths:
            safe_path(path)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def invoke(self, arguments: str) -> str:
        values = json.loads(arguments)
        Draft202012Validator(self.parameters).validate(values)
        result = self.handler(**values)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def object_schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


class FileVersionChanged(ValueError):
    """The caller's file snapshot is stale; restart chunk reading from offset zero."""


def text_page(content: str, offset: int = 0, limit: int = 12000, **metadata) -> dict:
    """Build a character page that fits the agent's tool limit including JSON escaping."""
    if type(offset) is not int or not 0 <= offset <= len(content):
        raise ValueError("Offset must be within the text")
    if type(limit) is not int or not 1 <= limit <= 12000:
        raise ValueError("Limit must be an integer in 1–12,000")
    end = min(offset + limit, len(content))
    result = {**metadata, "offset": offset, "total_chars": len(content)}
    while True:
        result.update(content=content[offset:end], next_offset=end if end < len(content) else None)
        if len(json.dumps(result, ensure_ascii=False)) <= 18000:
            return result
        if end - offset <= 1:
            raise ValueError("Text response metadata exceeds output budget")
        end = offset + (end - offset) // 2


def workspace_tools(root: Path) -> list[Tool]:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    def resolve(path: str) -> Path:
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("Path must stay inside the workspace")
        if any(
            part.startswith(".git") or part == ".env" or part.startswith(".env.")
            for part in candidate.relative_to(root).parts
        ):
            raise ValueError("Private metadata is not exposed to tools")
        return candidate

    def read_file(path: str) -> str:
        file = resolve(path)
        if file.stat().st_size > 100_000:
            raise ValueError("File exceeds 100 KB")
        return file.read_text(encoding="utf-8")

    def read_file_chunk(
        path: str, offset: int = 0, limit: int = 12000, expected_sha256: str | None = None
    ) -> dict:
        file = resolve(path)
        if not file.is_file():
            raise ValueError("Expected a regular workspace file")
        with file.open("rb") as stream:
            raw = stream.read(100_001)
        if len(raw) > 100_000:
            raise ValueError("File exceeds 100 KB")
        if b"\0" in raw:
            raise ValueError("Expected UTF-8 text, not binary content")
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and expected_sha256 != digest:
            raise FileVersionChanged(
                "File changed; restart from offset zero without the old digest"
            )
        return text_page(raw.decode("utf-8"), offset, limit, path=path, sha256=digest)

    def write_file(path: str, content: str) -> str:
        if len(content.encode()) > 100_000:
            raise ValueError("File exceeds 100 KB")
        file = resolve(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
        return "Saved " + path

    def list_files(path: str) -> list[str]:
        return sorted(
            p.name + ("/" if p.is_dir() else "")
            for p in resolve(path).iterdir()
            if not p.name.startswith(".")
        )[:200]

    def edit_file(path: str, old: str, new: str) -> str:
        content = read_file(path)
        if not old or content.count(old) != 1:
            raise ValueError("Expected exactly one match; read the file before editing")
        return write_file(path, content.replace(old, new, 1))

    def search_files(query: str, path: str = ".") -> dict:
        import os

        if not query:
            raise ValueError("Search query must not be empty")
        base = resolve(path)
        matches, scanned = [], 0
        for directory, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink()
            )
            for name in sorted(files):
                if name.startswith("."):
                    continue
                scanned += 1
                if scanned > 1000:
                    return {"matches": matches, "truncated": True}
                relative = str((Path(directory) / name).relative_to(root))
                try:
                    content = read_file(relative)
                except (ValueError, OSError, UnicodeError):
                    continue
                for number, line in enumerate(content.splitlines(), 1):
                    if query in line:
                        matches.append({"path": relative, "line": number, "text": line[:500]})
                        if len(matches) >= 100:
                            return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    path_schema = object_schema({"path": {"type": "string"}})
    return [
        Tool(
            "read_file",
            "Read a small UTF-8 file in your workspace. For longer files, use "
            "read_file_chunk to avoid output truncation.",
            path_schema,
            read_file,
        ),
        Tool(
            "read_file_chunk",
            "Read a bounded UTF-8 file page. Offsets count characters, not bytes. Follow "
            "next_offset until null, passing the returned sha256 as expected_sha256 on later "
            "pages. On FileVersionChanged, restart at offset zero without the stale digest. "
            "Supports files up to 100 KB; preserves original newlines.",
            object_schema(
                {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 12000},
                    "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
                ["path"],
            ),
            read_file_chunk,
            source_paths=("src/evomesh/tools.py",),
        ),
        Tool(
            "write_file",
            "Write a UTF-8 file in your workspace.",
            object_schema(
                {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                }
            ),
            write_file,
        ),
        Tool(
            "list_files",
            "List a workspace directory; use '.' for its root.",
            path_schema,
            list_files,
        ),
        Tool(
            "edit_file",
            "Replace exactly one literal match in a workspace text file.",
            object_schema({key: {"type": "string"} for key in ("path", "old", "new")}),
            edit_file,
            source_paths=("src/evomesh/tools.py",),
        ),
        Tool(
            "search_files",
            "Search workspace text by literal substring, returning file/line evidence.",
            object_schema(
                {"query": {"type": "string", "minLength": 1}, "path": {"type": "string"}}, ["query"]
            ),
            search_files,
            source_paths=("src/evomesh/tools.py",),
        ),
    ]


def tool(
    function=None,
    *,
    name: str | None = None,
    description: str | None = None,
    source_paths: tuple[str, ...] = (),
    capability: str = "tools",
):
    """Turn an annotated function into a Tool, without hand-writing JSON Schema.

    Supports scalar types, lists, dictionaries, Literal, and unions/Optional.
    Explicit Tool(...) remains available for more complex schemas.
    """
    import inspect
    import types
    from typing import Literal, Union, get_args, get_origin, get_type_hints

    def schema(annotation):
        scalars = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            type(None): "null",
        }
        if annotation in scalars:
            return {"type": scalars[annotation]}
        origin, args = get_origin(annotation), get_args(annotation)
        if origin is Literal:
            return {"enum": list(args)}
        if origin in (Union, types.UnionType):
            return {"anyOf": [schema(a) for a in args]}
        if origin is list and len(args) == 1:
            return {"type": "array", "items": schema(args[0])}
        if origin is dict and args[0] is str:
            return {"type": "object", "additionalProperties": schema(args[1])}
        raise TypeError(f"Unsupported tool annotation: {annotation!r}; use explicit Tool schema")

    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            raise TypeError("Tool handlers must be synchronous; use AgentApp.arun for async apps")
        properties, required = {}, []
        hints = get_type_hints(fn)
        for parameter in inspect.signature(fn).parameters.values():
            if parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
                raise TypeError("Tool handlers require named parameters")
            properties[parameter.name] = schema(hints.get(parameter.name))
            if parameter.default is parameter.empty:
                required.append(parameter.name)
        return Tool(
            name or fn.__name__,
            description or inspect.getdoc(fn) or fn.__name__,
            object_schema(properties, required),
            fn,
            source_paths=source_paths,
            capability=capability,
        )

    return decorate(function) if function is not None else decorate
