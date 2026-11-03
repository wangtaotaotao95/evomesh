# Roadmap

## v0.1 — shared whole-stack evolution

- [x] Assistant CLI, soul/skill loading and extensible tool loop
- [x] SQLite/MySQL observations and proposals
- [x] Source reflection; skill/tool/soul/loop/code changes
- [x] Periodic integration, validation, conflict detection and crash recovery
- [x] Explicit version adoption and offline evolution demo

## v0.2 — assistant and framework use one runtime

- [x] `AgentApp`, typed tool decorators, plugins and ready-to-run project templates
- [x] Owner-scoped memory and bounded persistent conversations/search
- [x] Durable task queue, progress events, cancellation and explicit retry
- [x] Multi-worker interval scheduler with atomic task enqueueing
- [x] Task/session/memory/schedule HTTP APIs and CLI commands
- [x] Named delegation, bounded team parallelism, async bridge and policy hooks
- [x] Optional local/Docker terminal, allowlisted web text retrieval
- [x] Persistent MCP v2 stdio tool discovery/calls
- [x] Real database/MCP/Docker verification in CI

## v0.3 — channels and web console

- [x] Feishu/Lark encrypted events, DingTalk signed callbacks, Telegram secret webhooks
- [x] Text replies, explicit sender/group allowlists and environment-only credentials
- [x] Atomic channel receipt/task enqueue; SQL reply claims and manual uncertainty recovery
- [x] Bundled responsive console: overview/chat/tasks/memory/schedules/channels/evolution
- [x] One-command console with embedded worker; reusable server/custom channel adapter APIs
- [x] Protocol fixtures, HTTP end-to-end tests and browser flow verification

## Next: richer assistant surfaces without splitting the framework

- [ ] Persistent interactive browser worker and takeover
- [ ] Durable review/approval flow, pause/resume checkpoints and task plans
- [ ] Token streaming and AG-UI adapter, richer personal activity UI
- [ ] More provider/channel connectors and OAuth onboarding
- [ ] Document/PDF/media workflows and personal service adapters
- [ ] Rich skill management, semantic recall and context compression

## Next: prove and deploy useful evolution

- [ ] Task replay and held-out success/cost/latency evaluation
- [ ] Observation lifecycle, proposal evidence and acceptance scoring
- [ ] Strong isolated candidate validation and independent acceptance policy
- [ ] Semantic/three-way conflict resolution
- [ ] Release registry, canary rollout and rollback
- [ ] General schema migrations, remote Git publication coordination

Use [the capability alignment matrix](alignment.md) to distinguish implemented primitives from full product parity. A merged patch is not automatically a measured improvement.
