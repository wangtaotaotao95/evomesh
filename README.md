<div align="center">

# EvoMesh

**One instance learns. The whole agent evolves.**

A personal assistant and extensible agent framework for **multi-instance, whole-stack self-evolution**.

[![CI](https://github.com/wangtaotaotao95/evomesh/actions/workflows/ci.yml/badge.svg)](https://github.com/wangtaotaotao95/evomesh/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![MIT](https://img.shields.io/badge/license-MIT-green)
![Alpha](https://img.shields.io/badge/status-alpha-orange)

[中文](README.zh-CN.md) · [Architecture](docs/architecture.md) · [Deployment](docs/deployment.md) · [Roadmap](docs/roadmap.md)

</div>

Agent friction can come from anywhere: a weak skill, a missing tool, an unhelpful soul, a brittle ReAct loop, or a single line of implementation code. EvoMesh treats all of those as evolvable source artifacts.

Each instance records observations and proposes changes to a shared SQL ledger. An integration worker periodically claims proposals, checks conflicts, validates them in a detached Git worktree, and advances a versioned evolution branch. Instances adopt a validated revision between runs.

**Alpha, not a production autonomy claim.** The execution, proposal, validation, integration, and explicit upgrade paths work. Real-world improvement quality depends on your model and validation suite. Automated fleet rollout, semantic conflict resolution, strong execution sandboxing, and durable conversation memory are future work.

## What works today

| Capability | Implementation |
|---|---|
| Personal assistant | CLI chat, file tools, `SOUL.md`, skill loading, compatible model endpoint |
| Framework | `Agent`, `Tool`, `Model` protocol, SQL ledger, integration API |
| Multi-instance operation | Authenticated task API, per-instance IDs, shared project ledger |
| Storage | SQLite with WAL for one host; MySQL with PyMySQL for multiple hosts |
| Observe → propose | Tool failures, step limits, explicit feedback; model-driven source inspection and proposals |
| Whole-stack changes | Skill, tool, soul, loop, or other tracked UTF-8 code; add/update/delete |
| Periodic integration | Worker interval, duplicate suppression, leases, heartbeats, conflict detection |
| Validation and recovery | Operator-supplied checks, timeout, atomic Git ref update, commit recovery |
| Version adoption | Explicit clean-checkout fast-forward and restart |

## Start in a minute

Requires Python 3.11+ and Git. Run from a source checkout for self-evolution.

```bash
git clone https://github.com/wangtaotaotao95/evomesh.git
cd evomesh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
evomesh init
evomesh run --demo 'List my workspace'
python examples/evolution_demo.py
```

The offline demo uses no API key and no paid inference. `--demo` is a deterministic file-listing smoke test. `evolution_demo.py` proves two instances can contribute a soul change and a tool change, validate both, and upgrade a disposable agent repository.

For an actual assistant, configure a tool-capable Chat Completions-compatible endpoint:

```bash
export EVOMESH_BASE_URL='https://your-provider.example/v1'
export EVOMESH_MODEL='your-tool-capable-model'
export EVOMESH_API_KEY='your-key'
evomesh chat
# Or one task; extra reflection calls happen only after observed friction:
evomesh run --auto-reflect 'Organize my notes in the workspace'
```

Model name and base URL are deliberately explicit. Compatibility varies by provider/model; no hosted-model compatibility or quality benchmark is claimed. `ChatModel` uses the [Chat Completions tool-call format](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create). You can replace it with any implementation of the `Model` protocol.

## The evolution loop

```mermaid
flowchart LR
    A[Instance A] --> O[Shared observations]
    B[Instance B] --> O
    O --> R[Inspect source and reflect]
    R --> P[(SQL proposal ledger)]
    P --> W[Periodic integration worker]
    W --> V[Conflict check and validation]
    V -->|pass| G[Versioned evolution branch]
    V -->|fail| F[Rejected or conflict report]
    G --> U[Explicit upgrade and restart]
    U --> A
    U --> B
```

```bash
# Record feedback and let a model inspect source and generate a proposal.
evomesh observe 'The assistant repeatedly forgets to summarize saved files'
evomesh reflect --repo .
evomesh proposals

# Validate and integrate one proposal. The checker runs inside the candidate tree.
evomesh integrate --repo . --check 'python -m pytest -q'

# Or keep processing one proposal per interval.
evomesh worker --repo . --interval 60 --check 'python -m pytest -q'

# Inspect the result, then adopt it between runs.
git diff main..evolution/main
evomesh upgrade --repo .
# Restart the agent; reinstall dependencies if they changed.
```

The integration branch defaults to `evolution/main` and must not be checked out. The currently running checkout stays unchanged until `upgrade`. Checks run with candidate `src/` first on `PYTHONPATH`, so tests exercise proposed Python changes, not the previously installed source.

Changes to different files can accumulate from an older base. A second change to an already-modified file is marked `conflict`; the alpha does not guess a semantic merge. See [architecture](docs/architecture.md) for recovery and consistency boundaries.

## Deploy multiple instances

```bash
pip install -e '.[dev,mysql]'
export EVOMESH_DATABASE_URL='mysql+pymysql://user:password@host/evomesh?charset=utf8mb4'
export EVOMESH_PROJECT='my-agent'
evomesh init  # Once, before replicas start.
export EVOMESH_SERVER_TOKEN='a-long-random-secret'
EVOMESH_INSTANCE_ID=laptop evomesh serve --port 8765
# On another machine with the same DB/project and its own workspace:
# EVOMESH_INSTANCE_ID=server evomesh serve --port 8765
```

`POST /run` accepts `{"task":"..."}` with `Authorization: Bearer <token>`. Each request starts a fresh conversation; CLI chat retains history only in process. The API is a small deployment primitive, not a complete web product.

[Docker Compose](compose.yaml) includes MySQL and two task API instances. Read [deployment instructions](docs/deployment.md) for storage, reflection, a central integration worker, rollout, and rollback.

## Build on EvoMesh

```python
from evomesh import Agent, Store, Tool
from evomesh.models import ChatModel
from evomesh.tools import object_schema

store = Store("sqlite:///agent.db")
store.initialize()
tool = Tool(
    "word_count",
    "Count words.",
    object_schema({"text": {"type": "string"}}),
    lambda text: {"words": len(text.split())},
)
# Supply your provider's model ID and base URL:
model = ChatModel(model="your-model", base_url="https://your-provider.example/v1")
agent = Agent(model, store, [tool], project="my-agent", instance="worker-1")
# answer = agent.run('Count the words in: learn from experience')
```

See [custom_agent.py](examples/custom_agent.py) for a complete environment-configured example. Tools and model adapters are normal Python objects; you can subclass the runtime loop, customize the soul, or submit proposals from your own evaluation pipeline.

## Scope and trust

- SQLite is for processes on the **same host**, not a shared database over NFS. Use MySQL for cross-host instances.
- Instances share observations/proposals through SQL; workspaces and personal files are not replicated.
- Integration workers share **one canonical Git repository**. Independent clones must not publish the same integration branch concurrently.
- A detached worktree is **not an OS security sandbox**. Candidate tests execute code. Run untrusted proposals in a disposable, restricted environment and use an external evaluator before deployment.
- No generic shell tool is exposed to the assistant. Custom tools determine its additional capabilities.
- Raw conversations are not persisted by default. Explicit observations and model-written proposals can still contain sensitive data; connect only trusted instances and configure database access appropriately.

## Related work

Inspired by the learning loop in [Hermes Agent](https://github.com/NousResearch/hermes-agent). Its [self-evolution project](https://github.com/NousResearch/hermes-agent-self-evolution) also describes expansion from skills to prompts, tools, and code. EvoMesh focuses on **shared SQL evolution records, multi-instance contributions, and versioned integration** rather than claiming that other projects can only evolve skills.

This repository is an independent project and is not affiliated with other projects named EvoMesh.

## Contribute

```bash
pip install -e '.[dev,mysql]'
ruff check .
ruff format --check .
pytest -q
# Optional: exercise the same suite against an empty, disposable MySQL database.
EVOMESH_TEST_DATABASE_URL='mysql+pymysql://user:password@localhost/evomesh_test' pytest -q
```

CI runs SQLite on Python 3.11–3.13, the test suite against MySQL 8.4, package builds, and a Docker build. See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and the [roadmap](docs/roadmap.md).

MIT licensed. Source distribution name: `evomesh-agent`; import and command: `evomesh`. No PyPI publication is implied.
