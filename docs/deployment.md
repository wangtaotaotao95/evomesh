# Deployment

## Local assistant: SQLite

From the source checkout and activated virtual environment:

```bash
pip install -e '.[dev]'
evomesh init
evomesh chat
```

The default database is `.evomesh/ledger.db`; workspace files live in `workspace/`. Configure `EVOMESH_MODEL`, `EVOMESH_BASE_URL`, and optionally `EVOMESH_API_KEY` for real inference. `.env` is **not** automatically loaded by the CLI. Export variables in your process manager or shell.

To share a ledger across processes with different current directories, use the same **absolute** SQLite URL (`sqlite:////absolute/path/ledger.db`). SQLite is supported on a single host. Each instance should have its own workspace and ID. In-memory SQLite is for single-process tests.

## MySQL and two API instances

```bash
cp .env.example .env
# Edit all placeholder values, including model, endpoint, tokens and passwords.
docker compose up --build -d
```

Ports 8765 and 8766 expose instances `agent-a` and `agent-b` on localhost. MySQL is available on localhost:3307 for the host reflection/integration worker. Named volumes persist each workspace and the database. `init` runs schema setup before instances start. Docker Compose is an example deployment; configure TLS/reverse proxy, resource limits, backups, rate limits, and credentials for your own environment.

```bash
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/run \
  -H "Authorization: Bearer $EVOMESH_SERVER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task":"Create a short daily planning template in my workspace"}'
```

Export `EVOMESH_SERVER_TOKEN` in the shell first; Compose's `.env` does not export shell variables. API calls have fresh conversations and share an instance's workspace. Concurrent writes to the same file are not serialized, so applications should route independent work or provide their own workspace locks.

The runtime image contains installed Python code, soul and skills, but **no Git repository**. It executes tasks and records observations; source reflection and integration use a host checkout. This avoids pretending an immutable image can rewrite itself.

## Central reflection and integration

On the integration host, use the same project/database as the agents and a clone containing their source revisions:

```bash
source .venv/bin/activate
pip install -e '.[dev,mysql]'
export EVOMESH_DATABASE_URL='mysql+pymysql://evomesh:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:3307/evomesh?charset=utf8mb4'
export EVOMESH_PROJECT='personal'
export EVOMESH_INSTANCE_ID='integrator'
# Also export the model, endpoint and API key for reflection.
evomesh reflect --repo .
evomesh worker --repo . --interval 60 --check 'python -m pytest -q'
```

Run reflection periodically with your scheduler if desired; `worker` itself only consumes proposals and does not call a model. Source-checkout agents can use `run --auto-reflect` or `serve --auto-reflect` to submit proposals after friction. The reflector reads the latest shared project observations, so a central reflector can learn from every API instance.

`--check` may be repeated and is split into argv without a shell. The command must be available on the worker PATH; the virtual environment must contain test dependencies. Keep independent evaluation scripts outside candidate-editable source. Candidate changes do not automatically install dependencies.

For more than one integration process, share one canonical local Git repository and the DB. For separate integration hosts, designate a single publishing authority; remote distributed Git locking is not implemented. Keep the integration branch unchecked-out in all worktrees. After independently changing `main`, coordinate/merge it into the evolution branch before resuming so proposal bases remain in its ancestry.

## Rollout and rollback

For a local source installation:

```bash
git diff main..evolution/main
# Stop ongoing tasks first; working tree must be clean.
evomesh upgrade
pip install -e '.[dev,mysql]'
evomesh chat
```

For Docker, upgrade the build checkout and run `docker compose up --build -d agent-a agent-b`. Use versioned images and your orchestrator's rollout mechanism for a fleet. This alpha does not automate canary rollout or update running instances mid-request.

Record the pre-upgrade commit. To roll back a deployment, redeploy its previous image or create a separate checkout at that commit. To reverse an integrated change while preserving history, create a Git revert commit in the canonical evolution branch during a maintenance window, remove that branch's temporary worktree before restarting integration, then deploy the new revision. Database `integrated` status records historical integration, not whether a change is currently deployed or later reverted.

Back up both the Git repository and SQL database. Their histories are related but not atomically backed up. There is no automatic database schema migration/rollback in this release; any schema-changing proposal needs a separate migration plan.
