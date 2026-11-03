from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path

from sqlalchemy.engine import make_url

from .app import AgentApp
from .config import Config
from .evolution import Integrator, git, revision, submit
from .runtime_store import RuntimeStore
from .scaffold import scaffold
from .store import Store


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EvoMesh personal assistant and agent framework")
    p.add_argument("--config", type=Path)
    p.add_argument("--db")
    p.add_argument("--project")
    p.add_argument("--owner")
    p.add_argument("--instance")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize or add missing tables before starting replicas")
    new = commands.add_parser("new", help="Create a ready-to-run assistant or framework project")
    new.add_argument("destination", type=Path)
    new.add_argument("--template", choices=["assistant", "framework"], default="assistant")
    for name in ("run", "chat", "reflect", "serve", "task-worker"):
        sub = commands.add_parser(name)
        if name == "run":
            sub.add_argument("task")
        if name in {"run", "chat"}:
            sub.add_argument("--session", default="main" if name == "chat" else None)
            sub.add_argument("--events", action="store_true")
        if name == "serve":
            sub.add_argument("--host", default="127.0.0.1")
            sub.add_argument("--port", type=int, default=8765)
        if name == "task-worker":
            sub.add_argument("--once", action="store_true")
            sub.add_argument("--interval", type=float, default=1)
        sub.add_argument("--demo", action="store_true", default=None)
        sub.add_argument("--workspace", type=Path)
        sub.add_argument("--repo", type=Path)
        sub.add_argument("--max-steps", type=int)
        sub.add_argument("--auto-reflect", action="store_true", default=None)
    obs = commands.add_parser("observe")
    obs.add_argument("detail")
    obs.add_argument("--kind", default="feedback")
    commands.add_parser("observations")
    commands.add_parser("proposals")
    proposal = commands.add_parser("propose")
    proposal.add_argument("file", type=Path)
    proposal.add_argument("--repo", type=Path, default=Path.cwd())
    for name in ("integrate", "worker"):
        sub = commands.add_parser(name)
        sub.add_argument("--repo", type=Path, default=Path.cwd())
        sub.add_argument("--branch", default="evolution/main")
        sub.add_argument("--check", action="append", required=True)
        sub.add_argument("--timeout", type=float, default=300)
        if name == "worker":
            sub.add_argument("--interval", type=float, default=60)
    upgrade = commands.add_parser("upgrade")
    upgrade.add_argument("--repo", type=Path, default=Path.cwd())
    upgrade.add_argument("--ref", default="evolution/main")
    jobs = commands.add_parser("task").add_subparsers(dest="action", required=True)
    enqueue = jobs.add_parser("submit")
    enqueue.add_argument("prompt")
    enqueue.add_argument("--session")
    enqueue.add_argument("--key")
    jobs.add_parser("list")
    for name in ("show", "cancel", "retry", "events"):
        jobs.add_parser(name).add_argument("id")
    mem = commands.add_parser("memory").add_subparsers(dest="action", required=True)
    mem.add_parser("list").add_argument("--query", default="")
    remember = mem.add_parser("set")
    remember.add_argument("key")
    remember.add_argument("content")
    mem.add_parser("delete").add_argument("key")
    session = commands.add_parser("session").add_subparsers(dest="action", required=True)
    session.add_parser("list").add_argument("--query", default="")
    session.add_parser("show").add_argument("name")
    session.add_parser("delete").add_argument("name")
    recurring = commands.add_parser("schedule").add_subparsers(dest="action", required=True)
    add = recurring.add_parser("add")
    add.add_argument("prompt")
    add.add_argument("--every", type=float, required=True, help="Interval in seconds")
    recurring.add_parser("list")
    recurring.add_parser("disable").add_argument("id")
    return p


def configuration(args) -> Config:
    path = args.config or (Path("evomesh.toml") if Path("evomesh.toml").is_file() else None)
    cfg = Config.load(path)
    for key in (
        "project",
        "owner",
        "instance",
        "workspace",
        "repo",
        "demo",
        "max_steps",
        "auto_reflect",
    ):
        value = getattr(args, key, None)
        if value is not None:
            setattr(cfg, key, value)
    if args.db is not None:
        cfg.database_url = args.db
    cfg.validate()
    return cfg


def display(value):
    print(json.dumps(value, indent=2, ensure_ascii=False), flush=True)


def dispatch(args, store: Store, cfg: Config) -> None:
    state = RuntimeStore(store, cfg.project, cfg.owner)
    if args.command == "init":
        store.initialize()
        print("Evolution and application storage initialized.")
    elif args.command in {"run", "chat", "reflect", "serve", "task-worker"}:
        app = AgentApp(
            cfg,
            store=store,
            initialize=False,
            on_event=(lambda event: print(json.dumps(event), file=sys.stderr))
            if getattr(args, "events", False)
            else None,
        )
        try:
            if args.command == "serve":
                from .server import create_server

                server = create_server(
                    args.host, args.port, os.getenv("EVOMESH_SERVER_TOKEN", ""), app.agent, app=app
                )
                print(f"EvoMesh listening on {args.host}:{args.port}", flush=True)
                try:
                    server.serve_forever()
                finally:
                    server.server_close()
            elif args.command == "reflect":
                if cfg.demo:
                    raise ValueError("Reflection requires a real tool-capable model")
                display(app.agent().reflect())
            elif args.command == "run":
                print(app.run(args.task, session=args.session))
            elif args.command == "task-worker":
                if args.interval <= 0:
                    raise ValueError("interval must be positive")
                while True:
                    display(app.work_once())
                    if args.once:
                        break
                    time.sleep(args.interval)
            else:
                print(f"EvoMesh — session {args.session}; /quit, /reflect, /memory")
                while True:
                    try:
                        task = input("you> ").strip()
                    except EOFError:
                        break
                    if task == "/quit":
                        break
                    try:
                        if task == "/reflect":
                            display(app.agent().reflect())
                        elif task == "/memory":
                            display(state.recall())
                        elif task:
                            print(app.run(task, session=args.session))
                    except Exception as exc:
                        print(
                            f"Task failed: {type(exc).__name__}; conversation preserved",
                            file=sys.stderr,
                        )
        finally:
            app.close()
    elif args.command == "task":
        actions = {
            "submit": lambda: state.submit(args.prompt, args.session, args.key),
            "list": state.list_tasks,
            "show": lambda: state.task(args.id),
            "events": lambda: state.task_events(args.id),
            "cancel": lambda: state.cancel_task(args.id),
            "retry": lambda: state.retry_task(args.id),
        }
        display(actions[args.action]())
    elif args.command == "memory":
        display(
            state.recall(args.query)
            if args.action == "list"
            else state.remember(args.key, args.content)
            if args.action == "set"
            else state.forget(args.key)
        )
    elif args.command == "session":
        display(
            state.list_sessions(args.query)
            if args.action == "list"
            else state.session(args.name)
            if args.action == "show"
            else state.forget_session(args.name)
        )
    elif args.command == "schedule":
        display(
            state.list_schedules()
            if args.action == "list"
            else state.schedule(args.prompt, args.every)
            if args.action == "add"
            else state.disable_schedule(args.id)
        )
    elif args.command == "observe":
        print(store.observe(cfg.project, cfg.instance, args.kind, args.detail))
    elif args.command in {"observations", "proposals"}:
        display(
            store.observations(cfg.project)
            if args.command == "observations"
            else store.list(cfg.project)
        )
    elif args.command == "propose":
        data = json.loads(args.file.read_text())
        print(
            submit(
                store,
                args.repo,
                project=cfg.project,
                instance=cfg.instance,
                target=data["target"],
                rationale=data["rationale"],
                changes=data["changes"],
                base=data.get("base_commit"),
            )
        )
    elif args.command in {"integrate", "worker"}:
        if args.command == "worker" and args.interval <= 0:
            raise ValueError("interval must be positive")
        worker = Integrator(
            store,
            args.repo,
            cfg.project,
            [shlex.split(c) for c in args.check],
            branch=args.branch,
            timeout=args.timeout,
        )
        while True:
            display(worker.once())
            if args.command == "integrate":
                break
            time.sleep(args.interval)
    elif args.command == "upgrade":
        if git(args.repo, "status", "--porcelain"):
            raise ValueError("Checkout must be clean before upgrade")
        target = revision(args.repo, args.ref)
        git(args.repo, "merge", "--ff-only", target)
        print(f"Upgraded to {target}. Reinstall changed dependencies and restart.")


def main() -> None:
    args, store = parser().parse_args(), None
    try:
        if args.command == "new":
            scaffold(args.destination, args.template)
            print(f"Created {args.template} project at {args.destination.resolve()}")
            return
        cfg = configuration(args)
        url = make_url(cfg.database_url)
        if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        store = Store(cfg.database_url)
        # Schema creation is explicit for deployed workers; generated project startup is simple.
        if args.command not in {"serve", "task-worker", "worker", "integrate"}:
            store.initialize()
        dispatch(args, store, cfg)
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
    except Exception as exc:
        print(
            f"{type(exc).__name__}: "
            + (
                str(exc)
                if isinstance(exc, (ValueError, RuntimeError))
                else "Operation failed; check configuration and inputs."
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    main()
