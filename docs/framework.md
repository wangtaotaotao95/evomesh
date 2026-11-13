# A framework you can run immediately

`AgentApp` is the application runtime. The CLI and HTTP server are adapters around it. `Agent` is the lower-level model/tool loop for applications that already own persistence and orchestration.

## Start from a template

After installing EvoMesh from its repository:

```bash
evomesh new my-agent --template framework
cd my-agent
python app.py
```

The generated configuration uses an offline deterministic model. Change `demo=false` and set a tool-capable `model` and `base_url` to use real inference. API keys come from `EVOMESH_API_KEY`. The package is not yet published to PyPI.

```python
from evomesh import AgentApp, Config, tool


@tool
def count_words(text: str) -> int:
    """Count whitespace-separated words."""
    return len(text.split())


with AgentApp.from_config("evomesh.toml", tools=[count_words]) as app:
    app.state.remember("tone", "Short answers, please")
    print(app.run("Count the words in my request", session="main"))
```

`@tool` derives JSON Schema from annotated scalar, list, dictionary, union/Optional and Literal parameters, including defaults. For complex schemas, construct `Tool(name, description, parameters, handler)` directly. Duplicate names are rejected. Handlers are synchronous; `await app.arun(...)` offloads the runtime for async services.

## Replace any layer

- **Model:** pass `model=your_adapter` or `model_factory=lambda: YourAdapter(...)`. Implement `complete(messages, tools) -> assistant_message`. A shared model instance must be thread-safe if used concurrently.
- **Tools:** pass tools, call `app.register(tool)`, or configure trusted `plugin_modules`. Each module exports `register(app)`.
- **Memory/conversation/task storage:** `app.state` uses the SQLAlchemy engine from `app.store`. Choose SQLite/MySQL through `Config.database_url`, or supply an existing `Store`.
- **Runtime behavior:** use `Agent` directly or subclass `AgentApp.agent()` to install a custom loop. Both loop and factory are ordinary evolvable Python source.
- **Persona/skills:** `SOUL.md` and `skills/*/SKILL.md` are loaded at each Agent construction from `Config.repo`.
- **Policy:** pass `before_tool=lambda name, arguments: ...`. Returning false prevents invocation. This is an application policy hook, not a durable human-approval UI.
- **Events:** pass `on_event=callback` at app construction or per `run`. Events include run/model/tool transitions; raw tool arguments and outputs are not copied into progress events. Callback errors fail the run so a failed control hook cannot be silently ignored.

Example plugin:

```python
# my_tools.py
from evomesh import tool


@tool
def slug(text: str) -> str:
    """Convert words to a lowercase slug."""
    return "-".join(text.lower().split())


def register(app):
    app.register(slug)
```

Set `plugin_modules = ["my_tools"]`; make it importable via your package or `PYTHONPATH`. Plugin modules execute trusted local code.

## Durable sessions and memory

```python
app.run("My project is called Atlas", session="main")
# A new app/instance with the same project, owner and DB can continue:
app.run("What is my project called?", session="main")
app.state.list_sessions(query="Atlas")
app.state.forget_session("main")
app.state.remember("style", "Prefer concise answers")
app.state.recall("concise")
app.state.forget("style")
```

Completed turns, including tool-call messages/results, are saved. Failed/cancelled turns do not replace the previous completed history. Up to `history_turns` complete turns are retained; whole turns are removed so tool protocol pairs stay intact. This is bounded history, not semantic summarization or full archival retention. Search is literal substring matching, not vector or FTS ranking.

A per-session SQL lease prevents concurrent writers across instances. `SessionBusy` means the caller should retry later or use another session. Different sessions may run concurrently; they still need application-specific coordination for shared external resources or file paths.

`project + owner` scopes personal data. Shared evolution observations/proposals remain project-scoped. These are trusted-application namespaces, not a substitute for database authorization.

## Background tasks, schedules and cancellation

```python
task_id = app.submit("Draft the weekly notes", session="weekly", key="request-42")
# In an independently running worker using the same owner/project/DB:
app.work_once()
app.state.task(task_id)
app.state.task_events(task_id)
app.state.cancel_task(task_id)
app.state.retry_task(task_id)  # Explicit; inspect earlier effects first.
app.state.schedule("Review notes", interval_seconds=3600)
```

`work_once()` materializes due schedules and claims one task. A scheduler tick atomically advances the schedule and inserts its task. Missed intervals coalesce to one task instead of creating a backlog storm. The CLI `task-worker` repeats this method. A schedule alone does not keep a process alive or deliver an external notification.

States: `queued → running → succeeded/failed`; cancellation uses `cancelling → cancelled`; expired running leases become `interrupted`. Cancellation is cooperative between steps; an in-flight model/tool call may finish. Crash recovery deliberately does not replay side effects. Task submission keys are idempotent, but execution and external writes are not globally exactly-once.

The session save and task completion are separate transactions. After a crash, inspect saved history and task events before explicitly retrying an interrupted task. `asyncio` coroutine cancellation does not forcibly terminate the thread used by `arun`; use the task cancellation API for durable work.

## Independent proactive reviews

For independent active source reviews, use `ProactiveEvolution.run` in your managed
worker thread/process. Unlike `app.work_once()`, it does not consume user tasks or
wait for their queue to empty:

```python
from threading import Event, Thread
from evomesh.proactive import ProactiveEvolution

stop = Event()
reviewer = Thread(
    target=ProactiveEvolution(app).run,
    args=(stop,),
    kwargs={"poll_interval": 60, "on_cycle": print},
)
reviewer.start()
try:
    answer = app.run("Help with my task")
finally:
    stop.set()
    reviewer.join()  # Finish the active cycle before closing app/store.
```

This is explicit opt-in even when `config.proactive_evolution` is false. The latter
controls idle reviews inside `work_once()`. Dedicated and idle reviews share SQL
leases/cooldowns, and optional `evolution_checks` still gate integration. Use a
thread-safe model or `model_factory` for concurrent applications. See
[worker configuration and shutdown](computer-and-proactive.md).

## Named delegation

```python
from evomesh import Team

team = Team({"research": research_app, "writing": writing_app}, max_workers=2)
results = team.run({"research": "Inspect sources", "writing": "Draft an outline"})
coordinator.register(team.as_tool())
```

Each member is an `AgentApp` with its own selected model, tools, owner scope and workspace. Delegation targets are explicit names. The team bounds simultaneous jobs; avoid recursive registration or cycles. This is not a recursive autonomous planner, remote worker scheduler, or process sandbox.

## MCP

Install `.[mcp]`, then configure:

```toml
[evomesh.mcp_servers]
math = ["python", "/absolute/path/to/server.py"]
```

A persistent stdio connection exposes `math__tool_name` tools. Closing the app closes its MCP clients. Optional dependency: official MCP Python SDK v2. Tools are discovered with pagination; the model calls the advertised schemas through normal `Tool` validation. MCP resources, prompts, sampling, elicitation, remote OAuth and media presentation are not yet implemented.

MCP tool results marked `is_error` become `MCPToolError`, a `RuntimeError` subclass
with explicit model-facing feedback. Text blocks are preferred; structured error
content is serialized when no text is available. The agent receives bounded JSON
with `error`, `feedback`, `feedback_chars` and `feedback_truncated`, allowing a
subsequent model decision to correct arguments. An error result is not retried
automatically by the bridge; earlier external effects may still need inspection.
Successful result formats are unchanged, even if their business data contains
fields named `error` or `is_error`. Transport/protocol exceptions retain the
ordinary sanitized error handling.

Framework tools can opt into the same feedback contract:

```python
from evomesh import ToolFeedbackError, tool


@tool(source_paths=("company/catalog.py",), capability="catalog")
def lookup(key: str) -> str:
    """Look up an authorized catalog key."""
    if key not in {"red", "blue"}:
        raise ToolFeedbackError("Choose a catalog key: red or blue.")
    return key
```

Feedback is untrusted tool data, not a new instruction source. Only include text
intended for the model: it can appear in owner-scoped saved conversations/tool
receipts. Ordinary exception strings remain hidden from the model, and the shared
evolution ledger records only type, capability and source paths. Error and output
truncation metrics still apply. Feedback keeps at most 12,000 characters and is
shortened further so the complete JSON response fits 18,000 characters including
escaping. Calling an MCP tool directly still raises a `RuntimeError` subclass;
its `.feedback` attribute exposes the original server feedback to the caller,
while its ordinary exception string stays generic.

Discovered MCP tools declare `mcp` capability and `src/evomesh/mcp.py` provenance.
This points evolution at the bridge implementation, not the external server's
source code, and does not copy raw server diagnostics into shared observations.

Server commands run under the host user. The bridge does not pass EvoMesh API keys into the subprocess; configure server-specific credentials with your own trusted launcher. Treat server output as untrusted data.

## Optional computer tools

`terminal="local"` enables bounded argv execution in the workspace with filtered environment variables. It is full host-user execution, not a sandbox. `terminal="docker"` runs in `python:3.12-slim` with no network, nonroot identity, resource limits and a read-only workspace mount. Pre-pull the image; provide a running Docker engine. The container can read mounted files but cannot modify them; the normal file tools remain the writing interface.

`allowed_hosts=["example.com"]` enables `read_web` for exact configured HTTPS hosts. It limits response size, rejects redirects and private-address preflight checks, and extracts text. It is not a search engine or an interactive browser. Use a network egress policy for hostile URLs or DNS; no strong SSRF sandbox is claimed.
