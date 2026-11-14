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

## v0.4 — plans, knowledge and evidence-driven evolution

- [x] Owner/session plans with versions and session-lease fencing
- [x] Bounded workspace text indexing and cited lexical retrieval with freshness checks
- [x] Skill catalog and on-demand skill body loading
- [x] Character context budgeting, whole-turn preservation and duplicate-call limits
- [x] Private run metrics and whole-stack capability/source discovery
- [x] Custom tool source provenance and atomic proposal-observation evidence links
- [x] Behavioral evaluation, suite fingerprint/baseline comparison and integration gate tests
- [x] Console plans/knowledge/skills, runtime metrics and source catalog

## Next: richer assistant surfaces without splitting the framework

- [ ] Persistent interactive browser worker and takeover
- [ ] Durable review/approval flow, pause/resume checkpoints and executable task graphs
- [ ] Token streaming and AG-UI adapter, richer personal activity UI
- [ ] More provider/channel connectors and OAuth onboarding
- [ ] Document/PDF/media workflows and personal service adapters
- [ ] Skill marketplace/installation, semantic recall and model-generated context compression

## Next: prove and deploy useful evolution

- [ ] Task replay and held-out success/cost/latency evaluation
- [ ] Observation lifecycle, proposal evidence and acceptance scoring
- [ ] Strong isolated candidate validation and independent acceptance policy
- [ ] Semantic/three-way conflict resolution
- [ ] Release registry, canary rollout and rollback
- [ ] General schema migrations, remote Git publication coordination

Use [the capability alignment matrix](alignment.md) to distinguish implemented primitives from full product parity. A merged patch is not automatically a measured improvement.

## v0.5 — computer tools and proactive evolution

- [x] Per-task Chromium: open, snapshot, click, fill, back, screenshot
- [x] Native browser select controls by label/value and element-scoped keyboard actions
- [x] Python/local JavaScript execute_code with terminal isolation and timeout
- [x] JSON-safe execution output tails and source-linked truncation evidence/metrics
- [x] Bounded pipe capture without output spool files; deadline and descendant cleanup
- [x] Literal file search with line evidence and exact-match editing
- [x] Bounded search pagination, single-file scopes and explicit scan-limit evidence
- [x] Bounded workspace file pages with SHA-256 consistency checks and evolution provenance
- [x] Idle, project-wide proactive source review without requiring runtime errors
- [x] Source findings, bounded proposals, SQL lease/cooldown, optional gated integration
- [x] Real Chromium fixture test and multi-instance review regression tests
- [x] Paginated reflection source reads and independent bounded evolution step budget
- [x] Exact source-edit proposals compiled against pinned Git blobs, preserving untouched content
- [ ] Cross-turn browser profiles and interactive takeover
- [ ] Persistent code kernels and programmatic tool composition
- [x] Dedicated review worker to avoid idle-worker scheduling delays under sustained load
- [x] Preserve saved proposal receipts when reflection or integration subsequently fails
- [x] Bounded review summaries, frozen evidence pages and previous validation feedback
- [x] Bounded validation output and active check interruption after detected SQL lease loss
- [x] Bounded model-facing MCP failure feedback and bridge provenance; reusable framework tool errors
- [x] JSON-safe web text pagination, extracted-text snapshot checks and source-linked read failures
- [x] Pinned source catalog pagination and literal prefix filtering for whole-repository review
