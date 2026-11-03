# Agent capabilities and whole-stack evolution

v0.4 adds reusable cognitive primitives to the same `AgentApp` used by the personal assistant, web console, channels and workers. They are ordinary Python/source modules, so their implementations can evolve through the existing proposal/integration pipeline. No special skill-only execution path or immutable subsystem is introduced.

## Plans

The `read_plan`, `set_plan` and `update_plan_step` tools maintain one durable plan per owner/session. Plans contain 1–30 steps with `todo`, `doing`, `done` or `blocked` status. The system prompt includes the current plan. The model is instructed to update progress only with completion evidence.

```python
plan = app.cognition.save_plan(
    "research",
    "Prepare a report",
    [
        {"title": "Read source notes", "status": "todo"},
        {"title": "Write and check summary", "status": "todo"},
    ],
    expected_version=0,
)
plan["steps"][0]["status"] = "done"
app.cognition.save_plan("research", plan["title"], plan["steps"], plan["version"])
answer = app.run("Continue the research plan", session="research")
```

Version 0 creates a plan; updates require the exact latest version. External edits acquire a session lease, while agent tools use their existing run lease. Stale workers cannot edit after losing that lease. A plan records progress; it is **not** an executable DAG, durable tool checkpoint, automatic resume mechanism or proof that a step actually finished. A failed turn can leave partial plan updates; inspect task receipts before continuing. Plan tools require `AgentApp.run`, not a bare `app.agent().run` without session context.

CLI: `plan list`, `plan show SESSION`, `plan set SESSION TITLE STEP... --version N`. The console offers creation and per-step state updates. Replacing an existing plan through the framework/CLI resets steps to the statuses you supply.

## Workspace knowledge

`index_document` and `search_knowledge` are both tools and `app.cognition` methods. Indexing explicitly reads one visible, relative UTF-8 workspace file, limited to 100 KB. Supported suffixes: `.txt`, `.md`, `.rst`, `.csv`, `.json`, `.py`, `.js`, `.html`, `.yaml`, `.yml`. Hidden files, paths outside the workspace and binary/NUL content are rejected. File reading is not an OS sandbox.

Search scans the latest 200 indexed documents for the configured owner, ranks chunks by overlapping lowercase Latin words/individual Chinese characters and returns up to 10 matches (default 5), with path, start/end lines, source digest and text. This is simple lexical retrieval, not vector embeddings or a semantic relevance guarantee. Each result's source must still exist inside the current workspace and match its indexed digest; changed, removed, inaccessible or retargeted files are excluded until reindexed.

```bash
evomesh knowledge index notes/project.md
evomesh knowledge search '全域进化'
evomesh knowledge list
evomesh knowledge remove DOCUMENT_ID
```

Index removal leaves the source file intact. SQL contains indexed text and is private application data. Same-owner replicas need the same source file view; SQL does not distribute files. Document content is data and cannot override user/policy instructions. No PDF/media parser, crawling, directory-wide ingestion, remote upload or vector database is implied.

## Progressive skills

`SkillLibrary.list()` exposes up to 100 skill names, descriptions and source paths from `skills/NAME/SKILL.md`; only that catalog is included initially. `read_skill` loads one body and digest on demand. Escaping symlinks are rejected. Skills are bounded (50 KB source, with a smaller serialized-content limit to fit tool results); split oversized skills into focused files. No automatic marketplace install or downloaded-code execution is added.

Skill source changes still use normal `target="skill"` proposals. Skill loading logic, selection instructions, knowledge retrieval, memory handling or planning can instead evolve as `tool`, `loop`, `soul` or `code` changes as appropriate.

## Context and loop controls

`context_chars` defaults to 120000 and measures the serialized messages/tool-schema JSON, not provider tokens. Before inference, a copy drops the oldest **complete user turns**, then shortens large tool results in the active turn if necessary. System instructions, the current user request and tool-call/result associations are preserved. Full stored history is unaffected by this inference-only compaction, apart from the pre-existing turn-count retention and tool-result limits. If instructions/current input/schemas still exceed the budget, execution stops with `ContextBudgetExceeded`; it never silently chops the user's request or invents a summary.

`repeat_limit` defaults to 3 identical tool-name + canonical-argument attempts per turn, including nonconsecutive repeats. The next identical invocation stops before executing. This can interrupt intentional polling/repetition; configure a higher limit (up to 20) for that use case. It does not roll back earlier actions. Tool result truncation is explicitly labeled.

Context pressure/overflow, repeated calls, retrieval misses and tool errors produce structured observations with capability/source hints. They avoid raw prompts, queries, file contents and exception text. Limits remain operator configuration; reflection proposes source changes and cannot silently rewrite a running instance's settings.

## Evidence and provenance

`app.capabilities()` / `evomesh capabilities` map built-in capability names to source files: loop, context, planning, knowledge, skills, memory, runtime, channels, models, tools, console, evaluation, evolution and soul. This is a discovery catalog, not an allowlist: any valid tracked UTF-8 source can still change. `available_in_repo=false` means the configured source repo is not the framework checkout or lacks the relevant files. Point `repo` to the code you intend to evolve.

Custom business tools declare their own paths:

```python
from evomesh import tool


@tool(source_paths=("company/search.py",), capability="company-search")
def company_search(query: str) -> str:
    """Search the company's authorized knowledge source."""
    return search_service(query)
```

These paths appear in reflection discovery and tool-error observations. They are relative source hints, not paths to private task files or permission to edit arbitrary host files.

`app.last_run_metrics` is context-local; `app.learning.list()` and `/runs` expose the latest 100 owner runs. Metrics include inference/tool attempts, tool errors, elapsed duration, context compactions, maximum input characters, configured limits, framework version, configured repo revision and observation IDs. They do not contain task prompts, answers or arguments. The configured repo revision is not a guarantee that installed dependency code equals that commit. Persistence failures log a generic warning and do not make an already-executed action retryable. These are counts/timings, not provider token usage or cost accounting.

Reflection submissions include real observation IDs and atomically store proposal/evidence rows. Evidence must exist in the same project. Manual `submit(..., observation_ids=[...])` can also supply links; legacy manual proposals without evidence remain supported. Deduplication retains the first proposal and its original evidence set. Linking an observation demonstrates provenance, not that the proposed patch fixes it.

## Behavioral evaluation and integration gates

From the source checkout:

```bash
evomesh evaluate evals/offline.json --factory examples.evaluation_factory:create_app --output baseline.json
# After a candidate change, use the same suite:
evomesh evaluate evals/offline.json --factory examples.evaluation_factory:create_app \
  --baseline baseline.json --output candidate.json
```

The factory is trusted operator-selected `module:function` code and creates a fresh `AgentApp` for each case. Local modules are resolved from the current directory. The bundled factory uses a temporary workspace/database and the offline demo model. For real evaluations, build a disposable fixture factory with your chosen model and authorized tools; the evaluator actually runs them. It does not automatically choose live providers or copy production state.

Each case must have a unique name, prompt and at least one explicit assertion: `contains`, `not_contains`, `required_tools`, `forbidden_tools`, `max_model_calls`, `max_tool_calls`, `max_tool_errors`, or `max_duration_seconds`. Tool assertions refer to attempted calls. A duration budget is checked after the run, not a hard interrupt. Each case must complete successfully too. Reports contain check results and metrics, not raw prompts/answers; exceptions retain type only. Suite fingerprints prevent unrelated baseline/candidate comparisons. Exit status is nonzero when a case fails. `evaluate(factory, cases)` and `compare_reports(baseline, candidate)` are callable APIs.

Add an evaluation to the existing integration checks:

```bash
evomesh integrate --repo . \
  --check 'python -m pytest -q' \
  --check 'python -m evomesh.cli evaluate evals/offline.json --factory examples.evaluation_factory:create_app'
```

The integration worker runs checks in the candidate worktree with source precedence and bounded process timeouts. A regression fails the gate; a passing arbitrary implementation change can integrate normally. Tests exercise both cases. The offline sample proves wiring, not general intelligence or improvement in production. Broader task suites and independent acceptance remain necessary; keep held-out criteria and trusted enforcement outside the mutable candidate checkout when evaluating untrusted proposals. Candidate tests/worktrees alone are not a security boundary.
