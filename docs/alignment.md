# Hermes / OpenMuse capability alignment

Reviewed 2026-11-03 against the primary [Hermes README](https://github.com/NousResearch/hermes-agent/blob/main/README.md) and [OpenMuse README](https://github.com/CopilotKit/OpenMuse/blob/main/README.md). The user's [OpenMuse fork](https://github.com/wangtaotaotao95/openmuse) was also inspected. This is a planning and implementation matrix, not a benchmark or a claim of complete parity.

Hermes emphasizes learning from use, personal memory, scheduled work, messaging surfaces, tool environments and delegation. OpenMuse emphasizes a personal agent with visible durable work, browser/computer access, documents and connected personal services. EvoMesh keeps both use cases available through the same assistant/framework runtime.

| Capability | EvoMesh v0.5 implementation | Remaining gap |
|---|---|---|
| Ready-to-run assistant | Assistant scaffold, web console + embedded worker, CLI chat, persisted named sessions, model configuration | Rich TUI, mobile/desktop client |
| Ready-to-use framework | Framework scaffold, `AgentApp`, `@tool`, model/tool injection, async bridge, hooks | More native model adapters and richer tracing ecosystem |
| Personal memory and recall | Owner-scoped SQL memory; resumed bounded conversations; literal history search | Semantic/FTS memory recall, model-generated compression, user modeling |
| Skills and persona | SOUL.md; skill catalog and on-demand read_skill; source-based evolution proposals | Skill installation/marketplace and richer metadata |
| Plans and knowledge | Versioned session plans; bounded cited text retrieval with source freshness; context budget/repeat controls | Executable task DAGs, checkpoint resume, vector search, rich document parsing |
| Background work | Durable SQL tasks, leases, events, cancellation, explicit retry | Pause/resume checkpoints, executable task graphs and proactive channel notifications |
| Recurring work | Interval schedules and transactional enqueueing; multi-worker deduplication | Cron/timezone expressions, backoff policies and schedule-to-channel routing |
| Agent computer | File search/exact edits; local/Docker terminal; Python/local JavaScript execution; per-run Chromium open/snapshot/click/fill/back/screenshot | Interactive PTY, cross-turn browser persistence/profile/takeover, graphical desktop |
| Web access | Allowlisted public HTTPS text retrieval and Chromium interaction | Search-provider adapters |
| Tool ecosystem | Typed decorators, trusted plugin modules, persistent MCP stdio tools | MCP remote OAuth/resources/prompts/media; packaged connector catalog |
| Delegation | Named apps, parallel `Team.run`, coordinator delegation tool | Durable subtask graphs, recursive planning and per-child budgets |
| Visible/control flow | Web chat/task/event/memory/schedule/evolution console; CLI events, application tool-policy hook | Token streaming, AG-UI/rich cards, durable human approvals UI |
| Personal services | Feishu/Lark, DingTalk, Telegram text adapters; durable inbox/reply outbox, custom ChannelAdapter and tools/MCP | Gmail/calendar, attachments, streaming cards and OAuth onboarding |
| Documents and media | Workspace files only | PDF forms, attachments, voice, image workflows and document viewers |
| Goals and monitoring | Scheduled task primitive | Goals/milestones, dedicated monitors and deduplicated notifications |
| Multi-instance operation | SQLite same-host / MySQL cross-host; sessions/tasks/schedules coordinated through SQL | Independent file replication and remote Git publication coordination |
| Whole-stack evolution | Proactive idle review with SQL leases, source findings, optional gated integration; whole-source proposals, source catalog/custom-tool provenance, observation-ID links, private run metrics, behavioral evaluation gates, detached validation | Broad held-out benchmarks, semantic merges, automatic rollout/rollback |

## Product rule

A new capability should have a callable framework API and a usable assistant entry point where applicable. Business-specific integrations should be adapters around the runtime, not embedded into the ReAct loop. Durable task/identity/storage semantics must remain identical across CLI and HTTP use.

## Verification boundary

The automated suite exercises durable runtime contracts on SQLite and MySQL, model/tool behavior using deterministic adapters, a real local MCP server, scaffold startup, API flows and concurrency/recovery. CI also runs a real restricted Docker terminal. It does not prove live LLM task quality, Gmail/calendar integration, interactive browser parity, or a mobile application—those capabilities are not shipped in this release.

## Next alignment milestones

1. Browser worker with persistent sessions and takeover; durable tool-approval workflow.
2. Token/AG-UI event adapter and richer activity views built on the shipped console.
3. Native model adapters, more personal service connectors, richer document tools.
4. Broader held-out evaluation, acceptance scoring and deployment version registry.

2026-11-09 tool review: [Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools), [browser](https://hermes-agent.nousresearch.com/docs/user-guide/features/browser), [code execution](https://hermes-agent.nousresearch.com/docs/user-guide/features/code-execution/). EvoMesh execute_code is one-shot execution, not Hermes-style tool orchestration or a persistent Python kernel. Browser sessions span tool calls within one task only.
