"""Owner-scoped plans, bounded workspace retrieval, and progressive skill loading."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from sqlalchemy import Column, Index, Integer, String, Table, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from .runtime_store import LONG, OPTIONS, identity, metadata, retry_deadlock, sessions, timestamp
from .store import LeaseLost

plans = Table(
    "runtime_plans",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("project", String(128), nullable=False),
    Column("owner", String(128), nullable=False),
    Column("session", String(128), nullable=False),
    Column("title", String(512), nullable=False),
    Column("steps", LONG, nullable=False),
    Column("version", Integer, nullable=False),
    timestamp("updated_at"),
    **OPTIONS,
)
documents = Table(
    "runtime_documents",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("project", String(128), nullable=False),
    Column("owner", String(128), nullable=False),
    Column("path", String(512), nullable=False),
    Column("digest", String(64), nullable=False),
    Column("chunks", LONG, nullable=False),
    timestamp("updated_at"),
    **OPTIONS,
)


Index("ix_plan_owner", plans.c.project, plans.c.owner, plans.c.updated_at)
Index("ix_document_owner", documents.c.project, documents.c.owner, documents.c.updated_at)


def workspace_path(root: Path, path: str) -> Path:
    if not isinstance(path, str) or not path or len(path) > 512:
        raise ValueError("Expected a workspace path of 1–512 characters")
    root = root.resolve()
    relative = Path(path)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or any(p.startswith(".") for p in relative.parts)
    ):
        raise ValueError("Use a relative, non-hidden workspace file")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root) or any(
        p.startswith(".") for p in resolved.relative_to(root).parts
    ):
        raise ValueError("File must remain in the visible workspace")
    return resolved


def terms(text):
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text.lower())
    return set(words)


class CognitionStore:
    def __init__(self, state, workspace):
        self.state, self.engine, self.workspace = state, state.engine, Path(workspace)

    def plan(self, session):
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(plans).where(
                        plans.c.id == identity(self.state.project, self.state.owner, session)
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            return None
        return {**dict(row), "steps": json.loads(row["steps"])}

    @retry_deadlock
    def save_plan(self, session, title, steps, expected_version=0, *, lease=None):
        if lease is None:
            token, _ = self.state.acquire_session(session)
            try:
                return self.save_plan(session, title, steps, expected_version, lease=token)
            finally:
                self.state.release_session(session, token)
        if not isinstance(title, str) or not title.strip() or len(title) > 512:
            raise ValueError("Plan title must contain 1–512 characters")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("Expected a nonnegative plan version")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
            raise ValueError("A plan needs 1–30 steps")
        for step in steps:
            if (
                not isinstance(step, dict)
                or set(step) != {"title", "status"}
                or not isinstance(step["title"], str)
                or not step["title"].strip()
                or len(step["title"]) > 512
                or step["status"] not in {"todo", "doing", "done", "blocked"}
            ):
                raise ValueError("Steps require a title and todo/doing/done/blocked status")
        identifier = identity(self.state.project, self.state.owner, session)
        try:
            with self.engine.begin() as conn:
                # Lock the session first. UI edits cannot race an active agent turn; stale
                # agents cannot mutate plans after losing their session lease.
                current = (
                    conn.execute(
                        select(sessions).where(sessions.c.id == identifier).with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if lease is not None:
                    if (
                        not current
                        or current["token"] != lease
                        or current["lease_until"] <= time.time()
                    ):
                        raise LeaseLost("Plan session lease lost")
                elif current and current["token"] and current["lease_until"] > time.time():
                    raise ValueError("Cannot edit a plan while its session is running")
                values = dict(
                    title=title,
                    steps=json.dumps(steps, ensure_ascii=False),
                    version=expected_version + 1,
                    updated_at=time.time(),
                )
                if expected_version == 0:
                    conn.execute(
                        insert(plans).values(
                            id=identifier,
                            project=self.state.project,
                            owner=self.state.owner,
                            session=session,
                            **values,
                        )
                    )
                else:
                    changed = conn.execute(
                        update(plans)
                        .where(plans.c.id == identifier, plans.c.version == expected_version)
                        .values(**values)
                    ).rowcount
                    if not changed:
                        raise ValueError("Plan changed; reload before editing")
        except IntegrityError:
            raise ValueError("Plan already exists; supply its current version") from None
        return {**values, "session": session, "steps": steps}

    def list_plans(self):
        with self.engine.connect() as conn:
            return [
                {**dict(row), "steps": json.loads(row["steps"])}
                for row in conn.execute(
                    select(plans)
                    .where(self.state.scope(plans))
                    .order_by(plans.c.updated_at.desc())
                    .limit(100)
                ).mappings()
            ]

    def index_document(self, path):
        file = workspace_path(self.workspace, path)
        if file.suffix.lower() not in {
            ".txt",
            ".md",
            ".rst",
            ".csv",
            ".json",
            ".py",
            ".js",
            ".html",
            ".yaml",
            ".yml",
        }:
            raise ValueError("Only supported UTF-8 text documents can be indexed")
        with file.open("rb") as stream:
            raw = stream.read(100_001)
        if len(raw) > 100_000:
            raise ValueError("Document exceeds 100 KB")
        content = raw.decode("utf-8")
        if "\x00" in content:
            raise ValueError("Expected a text document")
        normalized = file.relative_to(self.workspace.resolve()).as_posix()
        identifier = identity(
            self.state.project, self.state.owner, hashlib.sha256(normalized.encode()).hexdigest()
        )
        lines, chunks, current, start, end = content.splitlines(), [], [], 1, 1
        for number, line in enumerate(lines, 1):
            # Bound both normal paragraphs and single very long lines; preserve line citations.
            for offset in range(0, max(1, len(line)), 1800):
                piece = line[offset : offset + 1800]
                if current and sum(len(s) for s in current) + len(piece) > 2000:
                    chunks.append(
                        {"start_line": start, "end_line": end, "text": "\n".join(current)}
                    )
                    current, start = [], number
                current.append(piece)
                end = number
        if current:
            chunks.append({"start_line": start, "end_line": len(lines), "text": "\n".join(current)})
        values = dict(
            path=normalized,
            digest=hashlib.sha256(raw).hexdigest(),
            chunks=json.dumps(chunks, ensure_ascii=False),
            updated_at=time.time(),
        )
        with self.engine.begin() as conn:
            changed = conn.execute(
                update(documents).where(documents.c.id == identifier).values(**values)
            ).rowcount
        if not changed:
            try:
                with self.engine.begin() as conn:
                    conn.execute(
                        insert(documents).values(
                            id=identifier,
                            project=self.state.project,
                            owner=self.state.owner,
                            **values,
                        )
                    )
            except IntegrityError:
                with self.engine.begin() as conn:
                    conn.execute(
                        update(documents).where(documents.c.id == identifier).values(**values)
                    )
        return {"path": normalized, "chunks": len(chunks), "digest": values["digest"]}

    def list_documents(self):
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(
                        documents.c.id, documents.c.path, documents.c.digest, documents.c.updated_at
                    )
                    .where(self.state.scope(documents))
                    .order_by(documents.c.updated_at.desc())
                    .limit(200)
                ).mappings()
            ]

    def remove_document(self, identifier):
        with self.engine.begin() as conn:
            return bool(
                conn.execute(
                    delete(documents).where(
                        self.state.scope(documents), documents.c.id == identifier
                    )
                ).rowcount
            )

    def search(self, query, limit=5):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 512:
            raise ValueError("Query must contain 1–512 characters")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("limit must be 1–10")
        wanted = terms(query)
        results = []
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(documents)
                    .where(self.state.scope(documents))
                    .order_by(documents.c.updated_at.desc())
                    .limit(200)
                )
                .mappings()
                .all()
            )
        for row in rows:
            # Never return stale, deleted or newly private content from the index.
            try:
                file = workspace_path(self.workspace, row["path"])
                with file.open("rb") as stream:
                    raw = stream.read(100_001)
                if hashlib.sha256(raw).hexdigest() != row["digest"]:
                    continue
            except (OSError, ValueError):
                continue
            for chunk in json.loads(row["chunks"]):
                score = len(wanted & terms(chunk["text"]))
                if score:
                    results.append(
                        {"path": row["path"], **chunk, "score": score, "digest": row["digest"]}
                    )
        return sorted(results, key=lambda r: (-r["score"], r["path"], r["start_line"]))[:limit]


class SkillLibrary:
    def __init__(self, repo):
        self.repo = Path(repo).resolve()
        self.root = self.repo / "skills"

    def _path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise ValueError("Invalid skill name")
        path = (self.root / name / "SKILL.md").resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_relative_to(self.repo):
            raise ValueError("Skill path escaped the library")
        return path

    def list(self):
        rows = []
        for directory in sorted(self.root.glob("*"))[:100]:
            try:
                path = self._path(directory.name)
                if path.stat().st_size > 50_000:
                    continue
                text = path.read_text(encoding="utf-8")
                description = next(
                    (
                        line.strip("# ").strip()
                        for line in text.splitlines()
                        if line.strip() and not line.startswith("---")
                    ),
                    "",
                )[:180]
                rows.append(
                    {
                        "name": directory.name,
                        "description": description,
                        "path": f"skills/{directory.name}/SKILL.md",
                    }
                )
            except (OSError, ValueError):
                continue
        return rows

    def load(self, name):
        path = self._path(name)
        with path.open("rb") as stream:
            raw = stream.read(50_001)
        if len(raw) > 50_000:
            raise ValueError("Skill exceeds 50 KB")
        if len(json.dumps(raw.decode("utf-8"), ensure_ascii=False)) > 18000:
            raise ValueError("Split large skills into smaller focused skills")
        return {
            "name": name,
            "path": f"skills/{name}/SKILL.md",
            "content": raw.decode("utf-8"),
            "digest": hashlib.sha256(raw).hexdigest(),
        }
