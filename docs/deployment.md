# Deployment and v0.2 upgrade

## Existing v0.1 installations

Back up the Git repository and SQL database, update the package, and run `evomesh init` **once before restarting replicas**. v0.2 adds `runtime_sessions`, `runtime_memories`, `runtime_tasks`, `runtime_events` and `runtime_schedules`; it does not alter the v0.1 evolution tables. General schema migrations are still not provided.

CLI/API now use `AgentApp` and persist completed conversations, tool results, task prompts and results. `Agent` as a low-level Python class remains in-memory. Choose a stable `EVOMESH_OWNER` and protect the database. Named CLI sessions resume with `--session`; `/run` returns a generated session ID when one is not supplied.

The Compose example now shares a `personal-data` volume between API replicas and the task worker. v0.1 used separate `agent-a-data`/`agent-b-data` volumes. Preserve those volumes and explicitly migrate selected workspace files before adopting the new Compose layout; they are not silently merged or deleted.

## One host: SQLite

```bash
evomesh new my-assistant
cd my-assistant
evomesh init
evomesh chat --session main
# In another terminal, same config/project/owner:
evomesh task-worker
```

Configure a real model by setting `demo=false`, model, base URL and `EVOMESH_API_KEY`. `.env` is not automatically loaded by the CLI; TOML and exported environment variables are supported. Env overrides TOML; explicit CLI flags override both.

SQLite supports processes on one host, not a database file over NFS. For processes with different working directories, use `--config` with an absolute path or a shared absolute SQLite URL. Runtime state is additive to the evolution ledger. Same-owner instances must also share the relevant workspace; SQL does not transfer files.

## MySQL and replicated API

```bash
cp .env.example .env
# Replace model, endpoint, key, token and database password placeholders.
docker compose up --build -d
```

The example starts MySQL 8.4, one schema-init job, two API replicas at localhost:8765/8766, and one independent task worker. A persistent volume gives those processes the same owner workspace. MySQL is exposed on localhost:3307 for the host-side evolution integrator. Production operators should add their own TLS gateway, resource/concurrency limits, backup and access-control policy.

For additional task workers: `docker compose up -d --scale task-worker=2`. SQL leases prevent concurrent claim of one task; session leases prevent overlapping turns in one session. Different sessions can still write the same files, so applications must coordinate operations on shared paths/resources.

The runtime image contains installed code, soul and skills, but no Git history. It can run user tasks and record friction. Source reflection and integration run against a separate source checkout. Do not turn on source reflection in an image without a configured repository.

For cross-host deployments, use MySQL and a deliberately shared filesystem or routed workspace service for replicas of one owner. Separate owners can have separate workspaces and task workers. The bearer-token API represents a single configured owner; it is not a multi-tenant account service.

## API and background work

```bash
export EVOMESH_SERVER_TOKEN='a-long-random-secret'
evomesh serve --port 8765
evomesh task-worker # Separate process; must stay running for tasks/schedules.
```

`serve` accepts requests, `task-worker` executes durable jobs. `/run` executes synchronously; `/tasks` only queues. Schedules are materialized by the task worker and coalesce missed intervals. No external notifications or implicit daemon are installed.

Task cancellation stops subsequent steps, not actions already in flight. A worker crash or lost lease leaves the task `interrupted` when another worker checks the queue; inspect receipts/effects, then explicitly retry. The task runner never promises exactly-once external side effects. Keep worker clocks synchronized for lease expiry.

## Terminal and MCP

Use `terminal="local"` only for trusted host execution. Docker terminal requires a running Docker engine and a pre-pulled `python:3.12-slim` image. It runs with a read-only workspace, no network, nonroot user and CPU/memory/PID limits. Docker daemon access is a privileged operator capability; the default application container does not mount the Docker socket.

MCP stdio servers are optional and configured as trusted command arrays. Install `.[mcp]`; the packaged runtime image includes this extra. Server processes keep their connections for the app lifecycle. Configure server-specific credentials through a trusted launcher rather than sharing EvoMesh's model key.

## Central source reflection/integration

```bash
source .venv/bin/activate
pip install -e '.[dev,mysql,mcp]'
export EVOMESH_DATABASE_URL='mysql+pymysql://evomesh:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:3307/evomesh?charset=utf8mb4'
export EVOMESH_PROJECT='personal'
export EVOMESH_INSTANCE_ID='integrator'
# Export EVOMESH_MODEL, EVOMESH_BASE_URL and EVOMESH_API_KEY as well.
evomesh reflect --repo .
evomesh worker --repo . --interval 60 --check 'python -m pytest -q'
```

`worker` consumes source proposals, not user tasks; `task-worker` consumes user jobs. Schedule reflection with your process manager if desired, or use `auto_reflect=true` on a source-checkout runtime to reflect after friction. Do not enable it in a demo configuration.

All integration workers must use one canonical Git object store/ref authority. The canonical repo must contain every submitted base revision. Independent clones are not distributed Git locks. Keep the integration branch unchecked-out. Coordinate changes to main with the evolution branch during maintenance so bases remain in its ancestry.

`--check` accepts argv strings without a shell. Candidate `src/` precedes installed source on `PYTHONPATH`; dependencies must already exist in the checker environment. The worktree is not an OS sandbox. Use disposable restricted runners and external held-out evaluation for untrusted proposals.

## Rollout and rollback

Inspect `git diff main..evolution/main`, stop tasks, run `evomesh upgrade`, reinstall changed dependencies and restart. For Docker, rebuild from the chosen commit and restart API/task-worker containers. Automatic canary rollout and hot reload are not implemented.

Record the previous deployed revision/image. Roll back by redeploying it or creating a separate checkout at that commit. Preserve evolution history with a Git revert commit when needed. An SQL `integrated` record is historical; it does not assert that a change is currently deployed or has not been reverted.

Back up both SQL and Git; they are not one distributed transaction. Application task/session state and personal files also require a coordinated backup strategy for your deployment.
