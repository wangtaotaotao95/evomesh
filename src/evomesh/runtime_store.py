"""Durable conversations, owner-scoped memory, tasks and interval schedules."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from functools import wraps

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    case,
    delete,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.exc import IntegrityError, OperationalError

from .store import LeaseLost, Store

metadata = MetaData()
LONG = Text().with_variant(LONGTEXT(), "mysql")
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_bin"}


def timestamp(name):
    return Column(name, Float(precision=53), nullable=False)


sessions = Table(
    "runtime_sessions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("project", String(128), nullable=False),
    Column("owner", String(128), nullable=False),
    Column("name", String(128), nullable=False),
    Column("history", LONG, nullable=False),
    Column("token", String(36)),
    Column("lease_until", Float(precision=53)),
    timestamp("updated_at"),
    **OPTIONS,
)
memories = Table(
    "runtime_memories",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("project", String(128), nullable=False),
    Column("owner", String(128), nullable=False),
    Column("key", String(128), nullable=False),
    Column("content", Text, nullable=False),
    timestamp("updated_at"),
    **OPTIONS,
)
tasks = Table(
    "runtime_tasks",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("dedupe", String(64), nullable=False, unique=True),
    Column("project", String(128), nullable=False),
    Column("owner", String(128), nullable=False),
    Column("session", String(128), nullable=False),
    Column("prompt", Text, nullable=False),
    Column("status", String(24), nullable=False),
    Column("result", LONG),
    Column("error", String(128)),
    Column("token", String(36)),
    Column("lease_until", Float(precision=53)),
    Column("attempts", Integer, nullable=False),
    timestamp("created_at"),
    timestamp("updated_at"),
    **OPTIONS,
)
Index("ix_runtime_tasks_queue", tasks.c.project, tasks.c.owner, tasks.c.status)
events = Table(
    "runtime_events",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("task_id", String(36), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("detail", Text, nullable=False),
    timestamp("created_at"),
    **OPTIONS,
)
Index("ix_runtime_events_task", events.c.task_id, events.c.created_at)
schedules = Table(
    "runtime_schedules",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("project", String(128), nullable=False),
    Column("owner", String(128), nullable=False),
    Column("prompt", Text, nullable=False),
    Column("interval_seconds", Float(precision=53), nullable=False),
    Column("enabled", Integer, nullable=False),
    timestamp("next_run"),
    **OPTIONS,
)


def identity(*parts: str) -> str:
    if any(not isinstance(p, str) or not p or len(p) > 128 for p in parts):
        raise ValueError("Identifiers must contain 1–128 characters")
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def rowdict(row):
    return dict(row) if row else None


def retry_deadlock(function):
    """Retry only known rolled-back SQL transactions, never model/tool execution."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        for attempt in range(5):
            try:
                return function(*args, **kwargs)
            except OperationalError as exc:
                # MySQL 1213 rolls back the entire transaction. Other errors may
                # have ambiguous outcomes and must not be automatically replayed.
                if not exc.orig.args or exc.orig.args[0] != 1213 or attempt == 4:
                    raise
                time.sleep(0.01 * (2**attempt))

    return wrapped


class SessionBusy(RuntimeError):
    pass


class RuntimeStore:
    def __init__(self, store: Store, project: str, owner: str = "personal"):
        identity(project, owner)
        self.store, self.project, self.owner = store, project, owner
        self.engine = store.engine

    def initialize(self):
        metadata.create_all(self.engine)

    def scope(self, table):
        return and_(table.c.project == self.project, table.c.owner == self.owner)

    def acquire_session(self, name: str, ttl: float = 120) -> tuple[str, list[dict]]:
        identifier, now, token = (
            identity(self.project, self.owner, name),
            time.time(),
            str(uuid.uuid4()),
        )
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(sessions).values(
                        id=identifier,
                        project=self.project,
                        owner=self.owner,
                        name=name,
                        history="[]",
                        updated_at=now,
                    )
                )
        except IntegrityError:
            pass
        with self.engine.begin() as conn:
            result = conn.execute(
                update(sessions)
                .where(
                    sessions.c.id == identifier,
                    or_(sessions.c.token.is_(None), sessions.c.lease_until < now),
                )
                .values(token=token, lease_until=now + ttl)
            )
            if result.rowcount != 1:
                raise SessionBusy(
                    "This session is already running; use another session or retry later"
                )
            history = conn.execute(
                select(sessions.c.history).where(
                    sessions.c.id == identifier,
                )
            ).scalar_one()
        return token, json.loads(history)

    @retry_deadlock
    def renew_session(self, name: str, token: str, ttl: float = 120):
        now = time.time()
        with self.engine.begin() as conn:
            result = conn.execute(
                update(sessions)
                .where(
                    sessions.c.id == identity(self.project, self.owner, name),
                    sessions.c.token == token,
                    sessions.c.lease_until > now,
                )
                .values(lease_until=now + ttl)
            )
            if result.rowcount != 1:
                raise LeaseLost("Session lease lost")

    @retry_deadlock
    def release_session(self, name: str, token: str, history: list[dict] | None = None):
        values = {"token": None, "lease_until": None}
        if history is not None:
            values.update(history=json.dumps(history, ensure_ascii=False), updated_at=time.time())
        with self.engine.begin() as conn:
            result = conn.execute(
                update(sessions)
                .where(
                    sessions.c.id == identity(self.project, self.owner, name),
                    sessions.c.token == token,
                    sessions.c.lease_until > time.time(),
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise LeaseLost("Session lease lost")

    def session(self, name: str) -> list[dict]:
        with self.engine.connect() as conn:
            history = conn.execute(
                select(sessions.c.history).where(
                    sessions.c.id == identity(self.project, self.owner, name),
                )
            ).scalar_one_or_none()
            return json.loads(history) if history else []

    def list_sessions(self, query: str = "") -> list[dict]:
        statement = select(sessions.c.name, sessions.c.updated_at).where(self.scope(sessions))
        if query:
            statement = statement.where(sessions.c.history.contains(query, autoescape=True))
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    statement.order_by(
                        sessions.c.updated_at.desc(),
                    ).limit(100)
                ).mappings()
            ]

    def forget_session(self, name: str):
        with self.engine.begin() as conn:
            result = conn.execute(
                delete(sessions).where(
                    sessions.c.id == identity(self.project, self.owner, name),
                    or_(sessions.c.token.is_(None), sessions.c.lease_until < time.time()),
                )
            )
            return bool(result.rowcount)

    def remember(self, key: str, content: str):
        identifier = identity(self.project, self.owner, key)
        if not isinstance(content, str) or not content.strip() or len(content) > 8000:
            raise ValueError("Memory must contain 1–8,000 characters")
        values = dict(content=content, updated_at=time.time())
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(memories).values(
                        id=identifier, project=self.project, owner=self.owner, key=key, **values
                    )
                )
        except IntegrityError:
            with self.engine.begin() as conn:
                conn.execute(update(memories).where(memories.c.id == identifier).values(**values))
        return key

    def recall(self, query: str = "") -> list[dict]:
        statement = select(memories.c.key, memories.c.content).where(self.scope(memories))
        if query:
            statement = statement.where(
                or_(
                    memories.c.content.contains(query, autoescape=True),
                    memories.c.key.contains(query, autoescape=True),
                )
            )
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    statement.order_by(
                        memories.c.updated_at.desc(),
                    ).limit(50)
                ).mappings()
            ]

    def forget(self, key: str) -> bool:
        with self.engine.begin() as conn:
            return bool(
                conn.execute(
                    delete(memories).where(
                        memories.c.id == identity(self.project, self.owner, key),
                    )
                ).rowcount
            )

    def _task_values(self, prompt: str, session: str, key: str):
        identity(self.project, self.owner, session, key)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            raise ValueError("Task must contain 1–12,000 characters")
        now = time.time()
        return dict(
            id=str(uuid.uuid4()),
            dedupe=identity(self.project, self.owner, key),
            project=self.project,
            owner=self.owner,
            session=session,
            prompt=prompt,
            status="queued",
            attempts=0,
            created_at=now,
            updated_at=now,
        )

    def submit(self, prompt: str, session: str | None = None, key: str | None = None) -> str:
        values = self._task_values(prompt, session or str(uuid.uuid4()), key or str(uuid.uuid4()))
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(tasks).values(**values))
        except IntegrityError:
            with self.engine.connect() as conn:
                old = conn.execute(select(tasks).where(tasks.c.dedupe == values["dedupe"]))
                row = old.mappings().one()
                if row["prompt"] != prompt or (session is not None and row["session"] != session):
                    raise ValueError(
                        "Idempotency key already used for a different request"
                    ) from None
                return row["id"]
        return values["id"]

    def list_tasks(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    select(tasks)
                    .where(self.scope(tasks))
                    .order_by(tasks.c.created_at.desc())
                    .limit(100)
                ).mappings()
            ]

    def task(self, identifier: str) -> dict | None:
        with self.engine.connect() as conn:
            return rowdict(
                conn.execute(
                    select(tasks).where(
                        self.scope(tasks),
                        tasks.c.id == identifier,
                    )
                )
                .mappings()
                .first()
            )

    def claim_task(self, ttl: float = 120) -> dict | None:
        now = time.time()
        # Discover without locks, then CAS by primary key. A broad UPDATE through
        # the status index can deadlock with a completion moving that index entry.
        with self.engine.connect() as conn:
            expired = list(
                conn.execute(
                    select(tasks.c.id)
                    .where(
                        self.scope(tasks),
                        tasks.c.status.in_(["running", "cancelling"]),
                        tasks.c.lease_until < now,
                    )
                    .limit(100)
                ).scalars()
            )
        for identifier in expired:
            self._interrupt_expired(identifier, now)
        with self.engine.connect() as conn:
            ids = list(
                conn.execute(
                    select(tasks.c.id)
                    .where(
                        self.scope(tasks),
                        tasks.c.status == "queued",
                    )
                    .order_by(tasks.c.created_at)
                    .limit(32)
                ).scalars()
            )
        for identifier in ids:
            row = self._claim_id(identifier, ttl)
            if row is not None:
                return row
        return None

    @retry_deadlock
    def _interrupt_expired(self, identifier: str, now: float):
        with self.engine.begin() as conn:
            conn.execute(
                update(tasks)
                .where(
                    tasks.c.id == identifier,
                    self.scope(tasks),
                    tasks.c.status.in_(["running", "cancelling"]),
                    tasks.c.lease_until < now,
                )
                .values(
                    status="interrupted",
                    token=None,
                    lease_until=None,
                    error="Worker lease expired; inspect effects before retry",
                    updated_at=now,
                )
            )

    @retry_deadlock
    def _claim_id(self, identifier: str, ttl: float):
        with self.engine.begin() as conn:
            result = conn.execute(
                update(tasks)
                .where(tasks.c.id == identifier, self.scope(tasks), tasks.c.status == "queued")
                .values(
                    status="running",
                    token=str(uuid.uuid4()),
                    lease_until=time.time() + ttl,
                    attempts=tasks.c.attempts + 1,
                    updated_at=time.time(),
                )
            )
            if result.rowcount == 1:
                return dict(
                    conn.execute(
                        select(tasks).where(
                            tasks.c.id == identifier,
                        )
                    )
                    .mappings()
                    .one()
                )
        return None

    @retry_deadlock
    def renew_task(self, identifier: str, token: str, ttl: float = 120):
        with self.engine.begin() as conn:
            result = conn.execute(
                update(tasks)
                .where(
                    self.scope(tasks),
                    tasks.c.id == identifier,
                    tasks.c.token == token,
                    tasks.c.status.in_(["running", "cancelling"]),
                    tasks.c.lease_until > time.time(),
                )
                .values(lease_until=time.time() + ttl)
            )
            if result.rowcount != 1:
                raise LeaseLost("Task lease lost")

    @retry_deadlock
    def finish_task(self, identifier: str, token: str, status: str, result=None, error=None):
        if status not in {"succeeded", "failed", "cancelled", "queued"}:
            raise ValueError("Invalid task status")
        with self.engine.begin() as conn:
            owned = and_(
                self.scope(tasks),
                tasks.c.id == identifier,
                tasks.c.token == token,
                tasks.c.lease_until > time.time(),
                tasks.c.status.in_(["running", "cancelling"]),
            )
            # One conditional write serializes cancellation with completion.
            changed = conn.execute(
                update(tasks)
                .where(owned)
                .values(
                    status=case((tasks.c.status == "cancelling", "cancelled"), else_=status),
                    result=result,
                    error=error,
                    token=None,
                    lease_until=None,
                    updated_at=time.time(),
                )
            )
            if changed.rowcount != 1:
                raise LeaseLost("Task lease lost")
            return conn.execute(
                select(tasks.c.status).where(
                    tasks.c.id == identifier,
                )
            ).scalar_one()

    @retry_deadlock
    def cancel_task(self, identifier: str) -> bool:
        with self.engine.begin() as conn:
            for old, new in (("queued", "cancelled"), ("running", "cancelling")):
                if conn.execute(
                    update(tasks)
                    .where(self.scope(tasks), tasks.c.id == identifier, tasks.c.status == old)
                    .values(status=new, updated_at=time.time())
                ).rowcount:
                    return True
        return False

    @retry_deadlock
    def retry_task(self, identifier: str) -> bool:
        with self.engine.begin() as conn:
            return bool(
                conn.execute(
                    update(tasks)
                    .where(
                        self.scope(tasks),
                        tasks.c.id == identifier,
                        tasks.c.status.in_(["failed", "interrupted", "cancelled"]),
                    )
                    .values(status="queued", result=None, error=None, updated_at=time.time())
                ).rowcount
            )

    def add_event(self, identifier: str, token: str, kind: str, detail: dict):
        with self.engine.begin() as conn:
            # Fence events from expired workers, including after retry.
            owned = conn.execute(
                select(tasks.c.id).where(
                    self.scope(tasks),
                    tasks.c.id == identifier,
                    tasks.c.token == token,
                    tasks.c.lease_until > time.time(),
                )
            ).scalar_one_or_none()
            if owned is None:
                raise LeaseLost("Task lease lost")
            conn.execute(
                insert(events).values(
                    id=str(uuid.uuid4()),
                    task_id=identifier,
                    kind=kind[:32],
                    detail=json.dumps(detail, ensure_ascii=False)[:16000],
                    created_at=time.time(),
                )
            )

    def task_events(self, identifier: str) -> list[dict]:
        if self.task(identifier) is None:
            return []
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    select(events)
                    .where(events.c.task_id == identifier)
                    .order_by(events.c.created_at, events.c.id)
                    .limit(1000)
                ).mappings()
            ]

    def schedule(self, prompt: str, interval_seconds: float, start_at: float | None = None) -> str:
        import math

        if not math.isfinite(interval_seconds) or interval_seconds < 1:
            raise ValueError("Schedule interval must be at least one second")
        self._task_values(prompt, "scheduled", "validate")
        identifier = str(uuid.uuid4())
        next_run = time.time() if start_at is None else start_at
        if not math.isfinite(next_run):
            raise ValueError("Invalid start time")
        with self.engine.begin() as conn:
            conn.execute(
                insert(schedules).values(
                    id=identifier,
                    project=self.project,
                    owner=self.owner,
                    prompt=prompt,
                    interval_seconds=interval_seconds,
                    enabled=1,
                    next_run=next_run,
                )
            )
        return identifier

    def list_schedules(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    select(schedules).where(
                        self.scope(schedules),
                    )
                ).mappings()
            ]

    def disable_schedule(self, identifier: str) -> bool:
        with self.engine.begin() as conn:
            return bool(
                conn.execute(
                    update(schedules)
                    .where(self.scope(schedules), schedules.c.id == identifier)
                    .values(enabled=0)
                ).rowcount
            )

    def tick(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        with self.engine.connect() as conn:
            due = [
                dict(r)
                for r in conn.execute(
                    select(schedules)
                    .where(
                        self.scope(schedules), schedules.c.enabled == 1, schedules.c.next_run <= now
                    )
                    .limit(100)
                ).mappings()
            ]
        generated = []
        for schedule in due:
            with self.engine.begin() as conn:
                result = conn.execute(
                    update(schedules)
                    .where(
                        schedules.c.id == schedule["id"],
                        schedules.c.enabled == 1,
                        schedules.c.next_run == schedule["next_run"],
                    )
                    .values(next_run=now + schedule["interval_seconds"])
                )
                if result.rowcount != 1:
                    continue
                # Advancing the schedule and enqueueing its task are one SQL transaction.
                key = f"{schedule['id']}:{schedule['next_run']}"
                values = self._task_values(schedule["prompt"], str(uuid.uuid4()), key)
                conn.execute(insert(tasks).values(**values))
                generated.append(values["id"])
        return generated
