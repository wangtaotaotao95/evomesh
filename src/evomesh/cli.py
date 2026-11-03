from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import sys
import time
from pathlib import Path

from .agent import DEFAULT_SOUL, Agent
from .evolution import Integrator, git, revision, submit
from .models import ChatModel, DemoModel
from .store import Store
from .tools import workspace_tools


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EvoMesh personal agent and evolution framework")
    p.add_argument(
        "--db", default=os.getenv("EVOMESH_DATABASE_URL", "sqlite:///.evomesh/ledger.db")
    )
    p.add_argument("--project", default=os.getenv("EVOMESH_PROJECT", "default"))
    p.add_argument("--instance", default=os.getenv("EVOMESH_INSTANCE_ID", socket.gethostname()))
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize the shared database (run once before replicas)")
    for name in ("run", "chat", "reflect", "serve"):
        sub = commands.add_parser(name)
        if name == "run":
            sub.add_argument("task")
        if name == "serve":
            sub.add_argument("--host", default="127.0.0.1")
            sub.add_argument("--port", type=int, default=8765)
        sub.add_argument("--demo", action="store_true", help="Offline deterministic tool demo")
        sub.add_argument("--workspace", type=Path, default=Path("workspace"))
        sub.add_argument("--repo", type=Path, default=Path.cwd())
        sub.add_argument("--max-steps", type=int, default=12)
        sub.add_argument(
            "--auto-reflect",
            action="store_true",
            help="Reflect after a task records friction; uses extra model calls",
        )
    obs = commands.add_parser("observe", help="Record explicit feedback without storing a chat")
    obs.add_argument("detail")
    obs.add_argument("--kind", default="feedback")
    commands.add_parser("observations")
    commands.add_parser("proposals")
    proposal = commands.add_parser("propose", help="Submit a JSON proposal")
    proposal.add_argument("file", type=Path)
    proposal.add_argument("--repo", type=Path, default=Path.cwd())
    for name in ("integrate", "worker"):
        sub = commands.add_parser(name)
        sub.add_argument("--repo", type=Path, default=Path.cwd())
        sub.add_argument("--branch", default="evolution/main")
        sub.add_argument(
            "--check",
            action="append",
            required=True,
            help="Trusted argv string (repeatable); runs in candidate worktree",
        )
        sub.add_argument("--timeout", type=float, default=300)
        if name == "worker":
            sub.add_argument("--interval", type=float, default=60)
    upgrade = commands.add_parser(
        "upgrade", help="Fast-forward a clean checkout; restart afterward"
    )
    upgrade.add_argument("--repo", type=Path, default=Path.cwd())
    upgrade.add_argument("--ref", default="evolution/main")
    return p


def build_agent(args, store: Store) -> Agent:
    if args.demo:
        model = DemoModel()
    else:
        model_name = os.getenv("EVOMESH_MODEL")
        base_url = os.getenv("EVOMESH_BASE_URL")
        if not model_name or not base_url:
            raise ValueError("Set EVOMESH_MODEL and EVOMESH_BASE_URL, or use --demo")
        model = ChatModel(model_name, base_url, os.getenv("EVOMESH_API_KEY", ""))
    soul_file = args.repo / "SOUL.md"
    soul = soul_file.read_text() if soul_file.is_file() else DEFAULT_SOUL
    skill_dir = args.repo / "skills"
    for skill in sorted(skill_dir.glob("*/SKILL.md"))[:20]:
        soul += "\n\n" + skill.read_text()[:10000]
    return Agent(
        model,
        store,
        workspace_tools(args.workspace),
        project=args.project,
        instance=args.instance,
        soul=soul,
        max_steps=args.max_steps,
        repo=args.repo,
    )


def dispatch(args, store: Store) -> None:
    if args.command == "serve":
        from .server import create_server

        # Validate model configuration before accepting requests.
        build_agent(args, store)
        server = create_server(
            args.host,
            args.port,
            os.getenv("EVOMESH_SERVER_TOKEN", ""),
            lambda: build_agent(args, store),
            args.auto_reflect,
        )
        print(f"EvoMesh listening on {args.host}:{args.port}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    elif args.command == "init":
        store.initialize()
        print("Evolution ledger initialized.")
    elif args.command in {"run", "chat", "reflect"}:
        agent = build_agent(args, store)
        if args.command == "reflect":
            if args.demo:
                raise ValueError("Reflection needs a tool-capable model; demo only lists files")
            print(json.dumps(agent.reflect()))
            return

        def run_task(task):
            try:
                print(agent.run(task))
            finally:
                if args.auto_reflect and agent.last_observations and not args.demo:
                    try:
                        print("Evolution proposals:", json.dumps(agent.reflect()), file=sys.stderr)
                    except Exception as exc:
                        print(f"Reflection failed: {type(exc).__name__}", file=sys.stderr)

        if args.command == "run":
            run_task(args.task)
        else:
            print("EvoMesh — /quit to exit, /reflect to propose improvements")
            while True:
                try:
                    task = input("you> ").strip()
                except EOFError:
                    break
                if task == "/quit":
                    break
                if task == "/reflect":
                    print(json.dumps(agent.reflect()))
                elif task:
                    run_task(task)
    elif args.command == "observe":
        print(store.observe(args.project, args.instance, args.kind, args.detail))
    elif args.command in {"observations", "proposals"}:
        rows = (
            store.observations(args.project)
            if args.command == "observations"
            else store.list(
                args.project,
            )
        )
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif args.command == "propose":
        data = json.loads(args.file.read_text())
        print(
            submit(
                store,
                args.repo,
                project=args.project,
                instance=args.instance,
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
            args.project,
            [shlex.split(c) for c in args.check],
            branch=args.branch,
            timeout=args.timeout,
        )
        while True:
            result = worker.once()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.command == "integrate":
                break
            time.sleep(args.interval)
    elif args.command == "upgrade":
        if git(args.repo, "status", "--porcelain"):
            raise ValueError("Checkout must be clean before upgrade")
        target = revision(args.repo, args.ref)
        git(args.repo, "merge", "--ff-only", target)
        print(f"Upgraded to {target}. Reinstall dependencies if changed, then restart the agent.")


def main() -> None:
    args = parser().parse_args()
    Path(".evomesh").mkdir(exist_ok=True)
    store = None
    try:
        store = Store(args.db)
        dispatch(args, store)
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
    except Exception as exc:
        # SQLAlchemy exceptions can contain credentials or proposal contents.
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
