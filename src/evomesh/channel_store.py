"""Atomic channel receipt + task insertion, and fenced, at-most-once reply attempts."""

import hashlib
import json
import time
import uuid

from sqlalchemy import Column, Index, String, Table, insert, select, update
from sqlalchemy.exc import IntegrityError

from .runtime_store import LONG, OPTIONS, identity, metadata, retry_deadlock, tasks, timestamp
from .store import LeaseLost

deliveries = Table(
    "runtime_channel_deliveries",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("project", String(128), nullable=False),
    Column("owner", String(128), nullable=False),
    Column("channel", String(40), nullable=False),
    Column("task_id", String(36), nullable=False),
    Column("reply", LONG, nullable=False),
    Column("status", String(24), nullable=False),
    Column("token", String(36)),
    Column("error", String(128)),
    timestamp("lease_until"),
    timestamp("created_at"),
    **OPTIONS,
)
Index("ix_channel_outbox", deliveries.c.project, deliveries.c.owner, deliveries.c.status)


class ChannelStore:
    def __init__(self, runtime):
        self.runtime, self.engine = runtime, runtime.engine

    @retry_deadlock
    def accept(self, channel, message):
        for value in (message.event_id, message.sender, message.conversation):
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError("Invalid channel identifier")
        event_key = hashlib.sha256(message.event_id.encode()).hexdigest()
        identifier = identity(self.runtime.project, self.runtime.owner, channel, event_key)
        session = (
            "channel-"
            + hashlib.sha256(
                json.dumps([channel, message.conversation, message.sender]).encode()
            ).hexdigest()
        )
        values = self.runtime._task_values(message.text, session, identifier)
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(tasks).values(**values))
                conn.execute(
                    insert(deliveries).values(
                        id=identifier,
                        project=self.runtime.project,
                        owner=self.runtime.owner,
                        channel=channel,
                        task_id=values["id"],
                        reply=json.dumps(message.reply),
                        status="waiting",
                        lease_until=0,
                        created_at=time.time(),
                    )
                )
        except IntegrityError:
            with self.engine.connect() as conn:
                existing = conn.execute(
                    select(deliveries.c.task_id).where(deliveries.c.id == identifier)
                ).scalar_one_or_none()
            if existing is None:
                raise
            return existing
        return values["id"]

    def list(self):
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(
                        deliveries.c.id,
                        deliveries.c.channel,
                        deliveries.c.task_id,
                        deliveries.c.status,
                        deliveries.c.error,
                        deliveries.c.created_at,
                    )
                    .where(self.runtime.scope(deliveries))
                    .order_by(deliveries.c.created_at.desc())
                    .limit(100)
                ).mappings()
            ]

    def claim_reply(self):
        now = time.time()
        with self.engine.connect() as conn:
            expired = (
                conn.execute(
                    select(deliveries.c.id)
                    .where(
                        self.runtime.scope(deliveries),
                        deliveries.c.status == "sending",
                        deliveries.c.lease_until < now,
                    )
                    .limit(100)
                )
                .scalars()
                .all()
            )
        for identifier in expired:
            self._expire(identifier, now)
        with self.engine.connect() as conn:
            candidates = (
                conn.execute(
                    select(deliveries.c.id)
                    .join(tasks, tasks.c.id == deliveries.c.task_id)
                    .where(
                        self.runtime.scope(deliveries),
                        deliveries.c.status == "waiting",
                        tasks.c.status.in_(["succeeded", "failed", "cancelled", "interrupted"]),
                    )
                    .order_by(deliveries.c.created_at)
                    .limit(20)
                )
                .scalars()
                .all()
            )
        for identifier in candidates:
            row = self._claim(identifier, now)
            if row:
                return row
        return None

    @retry_deadlock
    def _expire(self, identifier, now):
        with self.engine.begin() as conn:
            conn.execute(
                update(deliveries)
                .where(
                    deliveries.c.id == identifier,
                    deliveries.c.status == "sending",
                    deliveries.c.lease_until < now,
                )
                .values(status="uncertain", token=None, error="Reply worker lease expired")
            )

    @retry_deadlock
    def _claim(self, identifier, now):
        with self.engine.begin() as conn:
            changed = conn.execute(
                update(deliveries)
                .where(
                    deliveries.c.id == identifier,
                    deliveries.c.status == "waiting",
                )
                .values(status="sending", token=str(uuid.uuid4()), lease_until=now + 120)
            ).rowcount
            if changed:
                return dict(
                    conn.execute(select(deliveries).where(deliveries.c.id == identifier))
                    .mappings()
                    .one()
                )
        return None

    @retry_deadlock
    def finish_reply(self, identifier, token, status, error=None):
        if status not in {"sent", "failed", "uncertain"}:
            raise ValueError("Invalid delivery status")
        with self.engine.begin() as conn:
            changed = conn.execute(
                update(deliveries)
                .where(
                    self.runtime.scope(deliveries),
                    deliveries.c.id == identifier,
                    deliveries.c.token == token,
                    deliveries.c.status == "sending",
                    deliveries.c.lease_until > time.time(),
                )
                .values(status=status, error=error, token=None)
            ).rowcount
            if not changed:
                raise LeaseLost("Reply lease lost")

    @retry_deadlock
    def retry_reply(self, identifier):
        # Explicit operator action; retrying uncertain deliveries can duplicate messages.
        with self.engine.begin() as conn:
            return bool(
                conn.execute(
                    update(deliveries)
                    .where(
                        self.runtime.scope(deliveries),
                        deliveries.c.id == identifier,
                        deliveries.c.status.in_(["failed", "uncertain"]),
                    )
                    .values(status="waiting", error=None, token=None, lease_until=0)
                ).rowcount
            )
