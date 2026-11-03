# Roadmap

## v0.1 alpha — implemented

- [x] Assistant CLI, file tools, soul and skill loading
- [x] Extensible model/tool/agent interfaces
- [x] SQLite and MySQL shared observations and proposals
- [x] Multi-instance task API and Compose example
- [x] Observation-driven model reflection and source proposals
- [x] Full-stack text-file evolution, including agent runtime code
- [x] Periodic validation/integration with lease and Git CAS recovery
- [x] Explicit version adoption; reproducible offline integration demo

## Next: demonstrate useful evolution

- [ ] Task replay dataset with held-out success/cost/latency evaluation
- [ ] Per-observation lifecycle, proposal evidence and acceptance scores
- [ ] Strong container/VM validation isolation and resource budgets
- [ ] Independent acceptance policy and optional review/approval queue
- [ ] Semantic or three-way merging with conflict-resolution proposals
- [ ] Persistent personal memory and conversation history

## Then: operate a fleet

- [ ] Release registry and per-instance version/health reporting
- [ ] Canary rollout, automatic rollback and dependency build pipelines
- [ ] Scheduled reflection and configurable integration batches
- [ ] Schema migrations and storage adapter contract for more databases
- [ ] Git remote publishing coordination and multi-host worker fencing
- [ ] Additional model adapters, streaming, richer tools and integrations

A release should report measured outcomes, not call every merged patch an improvement.
