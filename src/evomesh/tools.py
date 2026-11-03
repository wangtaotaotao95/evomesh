"""Small, typed tool registry; applications can register arbitrary tools."""

from __future__ import annotations

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

    path_schema = object_schema({"path": {"type": "string"}})
    return [
        Tool("read_file", "Read a UTF-8 file in your workspace.", path_schema, read_file),
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
