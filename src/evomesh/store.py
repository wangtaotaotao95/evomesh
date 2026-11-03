"""Portable SQL ledger with compare-and-swap claims and expiring worker leases."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

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
    create_engine,
    event,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

metadata = MetaData()
proposals = Table(
    "proposals",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("project", String(128), nullable=False),
    Column("instance", String(128), nullable=False),
    Column("fingerprint", String(64), nullable=False, unique=True),
    Column("target", String(32), nullable=False),
    Column("base_commit", String(64), nullable=False),
    Column("rationale", Text, nullable=False),
    Column("changes", Text().with_variant(LONGTEXT(), "mysql"), nullable=False),
    Column("status", String(24), nullable=False),
    Column("token", String(36)),
    Column("lease_until", Float(precision=53)),
    Column("attempts", Integer, nullable=False, default=0),
    Column("result_commit", String(64)),
    Column("report", Text),
    Column("created_at", Float(precision=53), nullable=False),
    Column("updated_at", Float(precision=53), nullable=False),
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_bin",
)
Index("ix_proposal_queue", proposals.c.project, proposals.c.status, proposals.c.created_at)
observations = Table(
    "observations",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("project", String(128), nullable=False),
    Column("instance", String(128), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("detail", Text, nullable=False),
    Column("created_at", Float(precision=53), nullable=False),
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_bin",
)
Index("ix_observation_project", observations.c.project, observations.c.created_at)


class LeaseLost(RuntimeError):
    pass


class Store:
    """One schema for SQLite (same host) and MySQL (multiple hosts).

    Schema initialization is explicit so deployments can bootstrap once before
    starting replicas. A project scopes the queue; it is not an auth boundary.
    """

    def __init__(self, url: str = "sqlite:///.evomesh/ledger.db"):
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if url in ("sqlite://", "sqlite:///:memory:"):
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **kwargs)
        if self.engine.dialect.name not in {"sqlite", "mysql"}:
            raise ValueError("Supported backends: sqlite and mysql+pymysql")
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def configure(dbapi_connection, _):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()

    def initialize(self) -> None:
        metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    def observe(self, project: str, instance: str, kind: str, detail: str) -> str:
        identifier = str(uuid.uuid4())
        with self.engine.begin() as conn:
            conn.execute(
                insert(observations).values(
                    id=identifier,
                    project=project,
                    instance=instance,
                    kind=kind,
                    detail=detail[:16000],
                    created_at=time.time(),
                )
            )
        return identifier

    def observations(self, project: str, limit: int = 20) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(observations)
                .where(
                    observations.c.project == project,
                )
                .order_by(observations.c.created_at.desc())
                .limit(limit)
            )
            return [dict(row) for row in rows.mappings()]

    def submit(
        self,
        *,
        project: str,
        instance: str,
        target: str,
        base_commit: str,
        rationale: str,
        changes: list[dict],
    ) -> str:
        if target not in {"skill", "tool", "soul", "loop", "code"}:
            raise ValueError("Unknown evolution target")
        if not project or len(project) > 128 or not instance or len(instance) > 128:
            raise ValueError("project and instance must contain 1–128 characters")
        if not rationale.strip() or not changes or len(changes) > 32:
            raise ValueError("A rationale and 1–32 file changes are required")
        if len(rationale) > 10000:
            raise ValueError("Rationale exceeds 10,000 characters")
        payload = json.dumps(changes, sort_keys=True, ensure_ascii=False)
        if len(payload.encode()) > 500_000:
            raise ValueError("Proposal exceeds 500 KB")
        fingerprint = hashlib.sha256(f"{project}\0{base_commit}\0{payload}".encode()).hexdigest()
        identifier, now = str(uuid.uuid4()), time.time()
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(proposals).values(
                        id=identifier,
                        project=project,
                        instance=instance,
                        target=target,
                        fingerprint=fingerprint,
                        base_commit=base_commit,
                        rationale=rationale,
                        changes=payload,
                        status="pending",
                        created_at=now,
                        updated_at=now,
                    )
                )
        except IntegrityError:
            with self.engine.connect() as conn:
                existing = conn.execute(
                    select(proposals.c.id).where(
                        proposals.c.fingerprint == fingerprint,
                    )
                ).scalar_one_or_none()
            if existing is None:
                raise
            return existing
        return identifier

    def list(self, project: str, limit: int = 100) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(proposals)
                .where(
                    proposals.c.project == project,
                )
                .order_by(proposals.c.created_at.desc())
                .limit(limit)
            )
            return [dict(row) for row in rows.mappings()]

    def claim(self, project: str, lease_seconds: float = 120) -> dict | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = time.time()
        eligible = and_(
            proposals.c.project == project,
            or_(
                proposals.c.status == "pending",
                and_(proposals.c.status == "validating", proposals.c.lease_until < now),
            ),
        )
        # Read IDs then CAS in a fresh transaction. Avoid read->write lock upgrades
        # on SQLite and do not rely on vendor-specific SELECT FOR UPDATE behavior.
        with self.engine.connect() as conn:
            ids = list(
                conn.execute(
                    select(proposals.c.id)
                    .where(eligible)
                    .order_by(proposals.c.created_at)
                    .limit(32)
                ).scalars()
            )
        for identifier in ids:
            token = str(uuid.uuid4())
            with self.engine.begin() as conn:
                result = conn.execute(
                    update(proposals)
                    .where(
                        proposals.c.id == identifier,
                        eligible,
                    )
                    .values(
                        status="validating",
                        token=token,
                        lease_until=now + lease_seconds,
                        attempts=proposals.c.attempts + 1,
                        updated_at=now,
                    )
                )
                if result.rowcount == 1:
                    return dict(
                        conn.execute(
                            select(proposals).where(
                                proposals.c.id == identifier,
                            )
                        )
                        .mappings()
                        .one()
                    )
        return None

    def renew(self, identifier: str, token: str, lease_seconds: float = 120) -> None:
        now = time.time()
        with self.engine.begin() as conn:
            result = conn.execute(
                update(proposals)
                .where(
                    proposals.c.id == identifier,
                    proposals.c.token == token,
                    proposals.c.status == "validating",
                    proposals.c.lease_until > now,
                )
                .values(lease_until=now + lease_seconds, updated_at=now)
            )
            if result.rowcount != 1:
                raise LeaseLost(identifier)

    def finish(
        self,
        identifier: str,
        token: str,
        status: str,
        report: str = "",
        result_commit: str | None = None,
    ) -> None:
        if status not in {"integrated", "conflict", "rejected", "error", "pending"}:
            raise ValueError("Invalid terminal status")
        with self.engine.begin() as conn:
            result = conn.execute(
                update(proposals)
                .where(
                    proposals.c.id == identifier,
                    proposals.c.token == token,
                    proposals.c.status == "validating",
                    proposals.c.lease_until > time.time(),
                )
                .values(
                    status=status,
                    token=None,
                    lease_until=None,
                    report=report[-16000:],
                    result_commit=result_commit,
                    updated_at=time.time(),
                )
            )
            if result.rowcount != 1:
                raise LeaseLost(identifier)
