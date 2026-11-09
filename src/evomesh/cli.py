from __future__ import annotations

import argparse
import json
import os
import secrets
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
    for name in ("run", "chat", "reflect", "evolve", "serve", "console", "task-worker"):
        sub = commands.add_parser(name)
        if name == "evolve":
            sub.add_argument(
                "--force", action="store_true", help="Ignore cooldown, never active leases"
            )
        if name == "run":
            sub.add_argument("task")
        if name in {"run", "chat"}:
            sub.add_argument("--session", default="main" if name == "chat" else None)
            sub.add_argument("--events", action="store_true")
        if name in {"serve", "console"}:
            sub.add_argument("--host", default="127.0.0.1")
            sub.add_argument("--port", type=int, default=8765)
            sub.add_argument("--with-worker", action="store_true")
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
    commands.add_parser("capabilities", help="Show capability-to-source evolution catalog")
    commands.add_parser("runs", help="List recent owner-scoped execution metrics")
    skill = commands.add_parser("skills").add_subparsers(dest="action", required=True)
    skill.add_parser("list")
    skill.add_parser("show").add_argument("name")
    knowledge = commands.add_parser("knowledge").add_subparsers(dest="action", required=True)
    knowledge.add_parser("list")
    knowledge.add_parser("index").add_argument("path")
    knowledge.add_parser("search").add_argument("query")
    knowledge.add_parser("remove").add_argument("id")
    plan = commands.add_parser("plan").add_subparsers(dest="action", required=True)
    plan.add_parser("list")
    plan.add_parser("show").add_argument("session")
    put = plan.add_parser("set")
    put.add_argument("session")
    put.add_argument("title")
    put.add_argument("steps", nargs="+")
    put.add_argument("--version", type=int, default=0)
    evaluation = commands.add_parser("evaluate", help="Run operator-supplied behavioral cases")
    evaluation.add_argument("suite", type=Path)
    evaluation.add_argument(
        "--factory", required=True, help="Trusted module:function returning a fresh AgentApp"
    )
    evaluation.add_argument("--output", type=Path)
    evaluation.add_argument("--baseline", type=Path)
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
    if args.command in {"capabilities", "runs", "skills", "knowledge", "plan"}:
        from .cognition import CognitionStore, SkillLibrary
        from .learning import LearningStore, capability_catalog

        cognition = CognitionStore(state, cfg.workspace)
        if args.command == "capabilities":
            display(capability_catalog(cfg.repo))
        elif args.command == "runs":
            display(LearningStore(state).list())
        elif args.command == "skills":
            library = SkillLibrary(cfg.repo)
            display(library.list() if args.action == "list" else library.load(args.name))
        elif args.command == "knowledge":
            display(
                {
                    "list": lambda: cognition.list_documents(),
                    "index": lambda: cognition.index_document(args.path),
                    "search": lambda: cognition.search(args.query),
                    "remove": lambda: cognition.remove_document(args.id),
                }[args.action]()
            )
        else:
            display(
                cognition.list_plans()
                if args.action == "list"
                else cognition.plan(args.session)
                if args.action == "show"
                else cognition.save_plan(
                    args.session,
                    args.title,
                    [{"title": step, "status": "todo"} for step in args.steps],
                    args.version,
                )
            )
    elif args.command == "init":
        store.initialize()
        print("Evolution and application storage initialized.")
    elif args.command in {"run", "chat", "reflect", "evolve", "serve", "console", "task-worker"}:
        app = AgentApp(
            cfg,
            store=store,
            initialize=False,
            on_event=(lambda event: print(json.dumps(event), file=sys.stderr))
            if getattr(args, "events", False)
            else None,
        )
        try:
            if args.command in {"serve", "console"}:
                from .server import create_server

                token = os.getenv("EVOMESH_SERVER_TOKEN", "")
                if (
                    not token
                    and args.command == "console"
                    and args.host in {"127.0.0.1", "localhost", "::1"}
                ):
                    token = secrets.token_urlsafe(32)
                    print(f"Console login token (this launch only): {token}", flush=True)
                server = create_server(
                    args.host,
                    args.port,
                    token,
                    app.agent,
                    app=app,
                    with_worker=args.command == "console" or args.with_worker,
                )
                print(f"EvoMesh console: http://{args.host}:{server.server_port}", flush=True)
                try:
                    server.serve_forever()
                finally:
                    server.server_close()
            elif args.command == "evolve":
                display(app.evolve_once(force=args.force))
            elif args.command == "reflect":
                if cfg.demo:
                    raise ValueError("Reflection requires a real tool-capable model")
                display(app.agent().reflect())
            elif args.command == "run":
                print(app.run(args.task, session=args.session))
            elif args.command == "task-worker":
                if args.interval <= 0:
                    raise ValueError("interval must be positive")
                from .channels import ChannelHub

                hub = ChannelHub(app)
                try:
                    while True:
                        hub.deliver_once()
                        display(app.work_once())
                        hub.deliver_once()
                        if args.once:
                            break
                        time.sleep(args.interval)
                finally:
                    hub.close()
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
        if args.command == "evaluate":
            import importlib

            from .evaluation import compare_reports, evaluate

            module, separator, name = args.factory.partition(":")
            if not separator or not module or not name:
                raise ValueError("Factory must be module:function")
            # The operator explicitly selects trusted local factory code.
            sys.path.insert(0, str(Path.cwd()))
            factory = getattr(importlib.import_module(module), name)
            report = evaluate(factory, json.loads(args.suite.read_text(encoding="utf-8")))
            if args.baseline:
                report["comparison"] = compare_reports(
                    json.loads(args.baseline.read_text(encoding="utf-8")), report
                )
            if args.output:
                args.output.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            display(report)
            if not report["passed"]:
                raise SystemExit(1)
            return
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
