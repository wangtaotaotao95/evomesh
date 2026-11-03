# Web console

EvoMesh ships its console inside the Python wheel and Docker image. No Node build or separate hosted application is needed. It uses the same `AgentApp`, SQL state, tools, and event API as your own framework application.

## Personal assistant

```bash
evomesh new my-assistant
cd my-assistant
evomesh console
```

Open `http://127.0.0.1:8765`. When running on loopback without `EVOMESH_SERVER_TOKEN`, `console` generates a temporary token and prints it in your terminal. Paste it into the login form. The token remains in this browser tab's session storage; logout clears it. To retain a token across service restarts, export `EVOMESH_SERVER_TOKEN` yourself.

The generated assistant uses an offline demo model that lists workspace files. For real conversations, set `demo=false`, model and base URL in the config, and `EVOMESH_API_KEY` in the service environment.

## Pages and behavior

| Page | Available actions |
|---|---|
| Overview | Real task totals, recent tasks, channel readiness, recent evolution distribution, local instance/worker information |
| Chat | New/named sessions, resumed history, submit durable tasks, poll step events, cancel |
| Tasks | Create jobs, inspect results/events, cancel, explicitly rerun failed/interrupted/cancelled jobs |
| Plans | Create session plans and update step states with version checks; active sessions cannot be edited concurrently |
| Knowledge & skills | Index workspace text files, search cited passages, remove indexes and inspect on-demand skill bodies |
| Memory | Add/replace/delete owner-scoped long-term memories |
| Schedules | Create interval schedules and disable future runs |
| Channels | Show credential readiness and allowlists, setup snippets, reply status and explicit reply retry |
| Evolution | Record feedback, view private run metrics and capability/source mappings, inspect project-wide observations and evidence-linked proposals |

Evolution checks, merging and instance upgrades remain in the existing CLI integration workflow. The UI does not run arbitrary validation commands or silently adopt proposed code. Channel credentials/config are managed through TOML/environment and a restart; the page does not save secrets or edit the deployment config. Channel readiness means local configuration is present, not proof of platform connectivity.

Chat uses task/event polling, not token streaming. Refreshing during a run does not stop the durable task; find it under Tasks, then reopen its session after completion. Closing the browser does not stop the server. Failed/cancelled turns are available in task receipts, while only completed turns are committed to conversation history. Tool cancellation takes effect between steps; it cannot recall an external action already in flight.

Overview task counts cover all owner tasks; lists show recent records (100 tasks, deliveries, observations and proposals; 50 memories). Evolution's namespace is `project`, so owners sharing a project can see the same evolution history. API deployment is one trusted owner, not a multi-user login system.

## Separate deployment or framework embedding

```bash
evomesh init
export EVOMESH_SERVER_TOKEN='replace-with-a-long-random-secret'
evomesh serve --host 127.0.0.1 --port 8765
# In another terminal with identical config:
evomesh task-worker
```

Or use `serve --with-worker`. A remote bind requires an explicitly configured token. For internet exposure, use an HTTPS gateway with body/rate/concurrency limits. The console has no third-party scripts/fonts and renders untrusted content as text. The public shell contains no private state; all data/control APIs require a bearer token. Webhooks use separate platform verification.

```python
import os
from evomesh import AgentApp
from evomesh.server import create_server

with AgentApp.from_config("evomesh.toml") as app:
    server = create_server(
        "127.0.0.1",
        8765,
        os.environ["EVOMESH_SERVER_TOKEN"],
        app.agent,
        app=app,
        with_worker=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
```

Local embedded-worker status is shown in Overview; an external worker is not falsely reported as offline. Its actual progress is visible through task states/events. SQL outages and local worker exceptions appear as errors. Stopping the server waits for its current embedded-worker operation to finish before closing resources.
