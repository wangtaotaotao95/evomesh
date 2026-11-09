"""SQL-fenced, project-wide proactive review intervals shared by all replicas."""

import json
import threading
import time
import uuid

from sqlalchemy import Column, String, Table, insert, select, update
from sqlalchemy.exc import IntegrityError

from .runtime_store import LONG, OPTIONS, metadata, timestamp
from .store import LeaseLost

cycles = Table(
    "evolution_cycles",
    metadata,
    Column("project", String(128), primary_key=True),
    Column("token", String(36)),
    timestamp("lease_until"),
    timestamp("next_run"),
    Column("status", String(24), nullable=False),
    Column("report", LONG, nullable=False),
    **OPTIONS,
)


class ProactiveEvolution:
    def __init__(self, app, lease_seconds=120):
        self.app = app
        self.engine = app.store.engine
        self.project = app.config.project
        self.lease_seconds = lease_seconds

    def status(self):
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(cycles).where(cycles.c.project == self.project))
                .mappings()
                .first()
            )
        if row is None:
            return None
        return {k: json.loads(v) if k == "report" else v for k, v in row.items() if k != "token"}

    def _claim(self, force=False):
        current = self.status()
        if current and (
            current["lease_until"] > time.time()
            or (not force and current["next_run"] > time.time())
        ):
            return None
        try:
            with self.engine.begin() as conn:
                if current is None:
                    conn.execute(
                        insert(cycles).values(
                            project=self.project,
                            token=None,
                            lease_until=0,
                            next_run=0,
                            status="ready",
                            report="{}",
                        )
                    )
        except IntegrityError:
            pass
        now, token = time.time(), str(uuid.uuid4())
        with self.engine.begin() as conn:
            query = update(cycles).where(
                cycles.c.project == self.project, cycles.c.lease_until <= now
            )
            if not force:
                query = query.where(cycles.c.next_run <= now)
            result = conn.execute(
                query.values(token=token, lease_until=now + self.lease_seconds, status="running")
            )
        return token if result.rowcount == 1 else None

    def _renew(self, token):
        now = time.time()
        with self.engine.begin() as conn:
            result = conn.execute(
                update(cycles)
                .where(
                    cycles.c.project == self.project,
                    cycles.c.token == token,
                    cycles.c.lease_until > now,
                )
                .values(lease_until=now + self.lease_seconds)
            )
        if result.rowcount != 1:
            raise LeaseLost(self.project)

    def once(self, *, force=False):
        cfg = self.app.config
        if cfg.demo:
            raise ValueError("Proactive evolution requires a real model adapter")
        token = self._claim(force)
        if token is None:
            return None
        stop, lost = threading.Event(), threading.Event()

        def renew():
            while not stop.wait(self.lease_seconds / 3):
                try:
                    self._renew(token)
                except Exception:
                    lost.set()
                    return

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        report, status = {}, "succeeded"
        try:
            agent = self.app.agent(cancelled=lost.is_set)
            agent.max_steps = cfg.evolution_max_steps
            report["proposals"] = agent.reflect(proactive=True, guard=lambda: self._renew(token))
            self._renew(token)
            if cfg.evolution_checks:
                from .evolution import Integrator

                report["integration"] = Integrator(
                    self.app.store,
                    cfg.repo,
                    cfg.project,
                    cfg.evolution_checks,
                    branch=cfg.evolution_branch,
                ).once()
        except Exception as exc:
            # Shared state never records task text, provider responses or credentials.
            status, report = "failed", {"error": type(exc).__name__}
        finally:
            stop.set()
            thread.join()
        with self.engine.begin() as conn:
            result = conn.execute(
                update(cycles)
                .where(
                    cycles.c.project == self.project,
                    cycles.c.token == token,
                    cycles.c.lease_until > time.time(),
                )
                .values(
                    token=None,
                    lease_until=0,
                    status=status,
                    next_run=time.time() + cfg.evolution_interval_seconds,
                    report=json.dumps(report),
                )
            )
        if result.rowcount != 1:
            raise LeaseLost(self.project)
        return {"status": status, **report}
