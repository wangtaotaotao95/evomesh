# Task and assistant API

Start `evomesh serve` after `evomesh init`, with `EVOMESH_SERVER_TOKEN` configured. By default the server listens on `127.0.0.1:8765`. Except `/health`, all endpoints require `Authorization: Bearer <token>`. The server uses one configured project and owner; request fields cannot switch owners. Deploy separate authenticated services for different owners.

JSON request bodies are capped at 64 KB. Successful responses are JSON with `Cache-Control: no-store`. Invalid requests return 400, missing authentication 401, concurrent session use 409, and internal failures 500. This alpha server is a small integration primitive; put a production gateway in front for TLS, rate/concurrency limits, and account authorization.

| Method/path | Body or query | Result |
|---|---|---|
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

`session` and submission `key` are optional. Missing session creates a fresh UUID; supplying a session resumes its history. Reusing an idempotency key with a different prompt/session returns 400. Task execution needs a separate `task-worker`; the API does not silently start one.

```bash
curl http://127.0.0.1:8765/tasks \
  -H "Authorization: Bearer $EVOMESH_SERVER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task":"List my workspace files","key":"example-1"}'
```

Poll the returned ID and `/events` to display progress. Events are durable step receipts, not token streaming or AG-UI. Result/error transitions happen after execution finishes. Cancel prevents subsequent steps but cannot recall an external action already in flight. Retry is explicit because prior side effects may have completed before a worker failed.

Raw completed conversation/tool results and task prompts/results are persisted. Event records omit raw arguments/results by default. Protect the database and authenticate access to all content endpoints.
