# Task and assistant API

Start `evomesh serve` after `evomesh init`, with `EVOMESH_SERVER_TOKEN` configured. By default the server listens on `127.0.0.1:8765`. The public routes are `/health`, the console shell `/`, `/app.js`, `/style.css`, and platform-authenticated `/webhooks/{account}`. All data/control endpoints require `Authorization: Bearer <token>`. The server uses one configured project and owner; request fields cannot switch owners. Deploy separate authenticated services for different owners.

JSON request bodies are capped at 64 KB. Successful responses are JSON with `Cache-Control: no-store`. Invalid requests return 400, missing bearer authentication 401, rejected channel authentication/allowlist 403, concurrent session use 409, and internal failures 500. This alpha server is a small integration primitive; put a production gateway in front for TLS, rate/concurrency limits, and account authorization.

| Method/path | Body or query | Result |
|---|---|---|
| `GET /capabilities` | none | Whole-stack capability/source catalog; custom tool provenance |
| `GET /runs` | none | Latest 100 owner-scoped execution metrics; no raw prompts/answers |
| `GET /skills`, `GET /skills/{name}` | none | Skill catalog or selected body/digest |
| `GET /plans`, `GET /plans/{session}` | none | Recent plans or one session plan |
| `POST /plans` | `{"session":"main","title":"Research","steps":[{"title":"Read","status":"todo"}],"version":0}` | Create/update with expected version; active session returns 409 |
| `GET /knowledge?query=...` | optional query | Matching chunks with file/line citations, or latest 200 indexed document metadata |
| `POST /knowledge/index` | `{"path":"notes.md"}` | Index/reindex one visible workspace text file |
| `POST /knowledge/remove` | `{"id":"..."}` | Remove index only; source file remains |
| `GET /status` | none | Version, project/owner, model, storage kind, task totals, local embedded-worker status; no credentials |
| `GET /channels` | none | Configured channel status, allowlists, missing env names and webhook paths |
| `GET /deliveries` | none | Latest 100 reply states; excludes callback URLs, tokens and private targets |
| `POST /deliveries/{id}/retry` | `{"acknowledge_duplicate_risk":true}` | Manually retry a failed/uncertain reply, without rerunning the task |
| `POST /webhooks/{account}` | Platform-signed JSON; no bearer | Authenticate, deduplicate and atomically enqueue; 200 acknowledges persistence |
| `GET /evolution` | none | Latest 100 project observations and proposals; claim tokens omitted |
| `POST /observations` | `{"detail":"..."}` | Add a project-scoped feedback record |
| `GET /health` | none | Process liveness |
| `POST /run` | `{"task":"...","session":"main"}` | Synchronous answer, session, instance |
| `POST /tasks` | `{"task":"...","session":"main","key":"request-1"}` | 202 with durable task ID |
| `GET /tasks` | none | Latest 100 owner tasks |
| `GET /tasks/{id}` | none | Status, result, error type and timestamps |
| `GET /tasks/{id}/events` | none | Up to 1,000 persisted progress events |
| `POST /tasks/{id}/cancel` | `{}` | Whether a state transition occurred |
| `POST /tasks/{id}/retry` | `{}` | Explicit retry of failed/interrupted/cancelled work |
| `GET /sessions?query=literal` | optional search | Session names and timestamps |
| `GET /sessions/{name}` | none | Retained completed conversation history |
| `POST /sessions/{name}/delete` | `{}` | Delete idle conversation; active leases block deletion |
| `GET /memory?query=literal` | optional search | Up to 50 matching memories |
| `POST /memory` | `{"key":"tone","content":"concise"}` | Create/replace memory |
| `POST /memory/delete` | `{"key":"tone"}` | Delete memory |
| `GET /schedules` | none | Owner's interval schedules |
| `POST /schedules` | `{"task":"...","interval_seconds":3600}` | New schedule ID |
| `POST /schedules/{id}/disable` | `{}` | Disable future scheduling |

`session` and submission `key` are optional. Missing session creates a fresh UUID; supplying a session resumes its history. Reusing an idempotency key with a different prompt/session returns 400. Task execution needs `task-worker`, `serve --with-worker`, or `console` (which starts one by default).

```bash
curl http://127.0.0.1:8765/tasks \
  -H "Authorization: Bearer $EVOMESH_SERVER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task":"List my workspace files","key":"example-1"}'
```

Poll the returned ID and `/events` to display progress. Events are durable step receipts, not token streaming or AG-UI. Result/error transitions happen after execution finishes. Cancel prevents subsequent steps but cannot recall an external action already in flight. Retry is explicit because prior side effects may have completed before a worker failed.

Raw completed conversation/tool results and task prompts/results are persisted. Event records omit raw arguments/results by default. Protect the database and authenticate access to all content endpoints.
