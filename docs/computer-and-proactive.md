# Computer tools and proactive whole-stack evolution

The assistant, channels, console and embedded applications register the same tools
through `AgentApp`. All new tools declare their implementation paths, so failures
point to evolvable source rather than being converted into skills by default.

## Install and configure

```bash
pip install -e '.[browser,dev]'
python -m playwright install chromium
```

Add these keys inside the existing `[evomesh]` section:

```toml
terminal = "local" # disabled (default), local or docker
browser = true
browser_allowed_hosts = ["example.com"] # Or ["*"] for public HTTPS hosts
proactive_evolution = true
evolution_interval_seconds = 3600
evolution_max_steps = 32 # Independent of user-task max_steps; range 1–100
evolution_checks = [["python", "-m", "pytest", "-q"]]
evolution_branch = "evolution/main"
```

Use a real tool-capable model (`demo=false`), a committed Git source repository,
and `evomesh console` or `evomesh task-worker`. The worker checks for proactive work
when the user-task queue is empty. A busy queue delays reviews; reviews themselves
occupy that worker until finished. Use the dedicated worker below to separate reviews.
Background inference has provider costs; enable it deliberately. Framework defaults
remain off so importing or creating an app does not silently begin paid inference.

For predictable reviews under a sustained task backlog, run a dedicated process:

```bash
evomesh --config evomesh.toml init
evomesh --config evomesh.toml evolution-worker --interval 60
# Or check for one due review, then exit:
evomesh --config evomesh.toml evolution-worker --once
```

Set `proactive_evolution=false` on task/console replicas when the dedicated worker
should own all review execution. That setting controls only their idle reviews;
explicitly starting `evolution-worker` enables reviews independently. The dedicated
worker never claims user tasks, ticks their schedules or delivers channel replies.
`--interval` is the SQL polling interval, not the inference interval: project-wide
`evolution_interval_seconds` and leases still govern when a review can run. Multiple
dedicated/idle workers share these same constraints. The daemon prints reports only
for executed cycles; `--once` prints null when nothing is due. There is no daemon
`--force` option to bypass cooldowns.

The daemon handles SIGINT/SIGTERM by finishing the current review/validation/report,
then exiting; an idle wait wakes immediately. Allow enough shutdown grace for the
bounded model and validation calls. A hard kill still relies on lease expiry and
the existing recovery contracts. Initialize the schema before starting deployed
workers, and point integration-enabled workers at the same canonical Git object store.

Framework applications can run `ProactiveEvolution(app).run(stop_event,
poll_interval=60, on_cycle=callback)` in their managed worker thread or process.
Set the `threading.Event` to stop after the current cycle. The caller owns app/store
cleanup. An optional callback receives completed/failed cycle reports; cooldown
polls are silent. Unhandled infrastructure or callback errors exit the worker so a
supervisor can report/restart it. `app.evolve_once()` remains the one-cycle API.

## Tool behavior

| Tool | Behavior |
|---|---|
| `terminal` | Explicit argv, no implicit shell; exit code and bounded output; timeout kills process group |
| `execute_code` | One-shot Python (`-I`) or local Node.js; print results; same backend/timeout as terminal |
| `browser` | `open`, `snapshot`, `click`, `fill`, `back`, `screenshot`; inspect snapshots to choose CSS selectors |
| `read_file_chunk` | Bounded UTF-8 pages with character offsets and optional SHA-256 version checks |
| `search_files` | Literal substring search, path/line citations, at most 1,000 files and 100 hits |
| `edit_file` | Read first; replace exactly one literal match; rejects missing/ambiguous matches |

`terminal` and `execute_code` return `exit_code`, `output`, `output_bytes` and
`truncated`. Output combines stdout/stderr and retains the tail of at most 20,000
bytes, shortened further as needed to keep the complete JSON response within
18,000 characters, including escaping. `output_bytes` counts the original
combined bytes; `truncated=true` means some output was omitted. Narrow the command
or printed results before retrying. Nonzero exits remain tool errors, while
truncation alone is not an execution failure. Both produce source-linked evolution
evidence without copying command arguments or diagnostics into the shared ledger.
This bounds the returned response, not temporary capture-file disk usage.

For long workspace files, call `read_file_chunk` with `path` and optionally `limit`
(1–12,000 characters). Follow the returned `next_offset` until null, supplying the
returned `sha256` as `expected_sha256` on each subsequent call. The digest covers
the original file bytes. If they change, `FileVersionChanged` requires restarting
at offset zero without the old digest; this detects version changes between calls
when the digest is supplied, but does not lock the file or retain a snapshot.
Pages preserve Unicode and original newlines, and include metadata and JSON
escaping within an 18,000-character output budget. UTF-8 files remain limited to
100 KB; binary content, workspace escapes and private metadata paths are rejected.
The existing `read_file` still returns plain text for small files.

Both the personal assistant and embedded `AgentApp` register this tool by default;
framework users can also obtain it through `workspace_tools(root)`. Tool failures
carry `src/evomesh/tools.py` provenance into the SQL evolution ledger, without
storing file contents or arguments in the failure observation.

Local terminal/code execution has host-user permissions. Environment scrubbing is
not a sandbox: local code can access host files and the network. Docker is nonroot,
network-disabled and resource-limited, with a read-only workspace and writable `/tmp`;
the bundled Docker image supports Python only. Local JavaScript requires `node` on
PATH. There is no persistent kernel or programmatic dispatch into other agent tools.

Browser tools use a fresh headless Chromium context per `AgentApp.run`. Page/cookie
state persists across tool calls within that run, and is closed on success, failure
or cancellation. No personal profile, cross-turn login persistence, uploads,
downloads, arbitrary evaluate tool or desktop takeover is provided. Screenshots are
saved as `browser-artifacts/<id>.png` inside the workspace and returned as paths.
The current text model adapter receives accessibility/text observations, not images.

Browser navigation and subrequests must pass configured host, HTTPS and public-IP
checks. Redirects are intercepted too; service workers and WebSockets are blocked.
Pages can execute JavaScript. DNS preflight is not an egress firewall; deploy inside
an appropriate network/container boundary when browsing untrusted pages. Browser
dependencies are optional and loaded lazily; install Chromium on each browser worker.

```python
from pathlib import Path
from evomesh.browser import BrowserSession
from evomesh.capabilities import execute_code_tool

with BrowserSession(Path("workspace"), ["example.com"]) as browser:
    print(browser.operate("open", url="https://example.com"))

execute = execute_code_tool(Path("workspace"), backend="local")
print(execute.invoke('{"code":"print(sum(range(10)))"}'))
```

## Proactive review and integration

`app.evolve_once()` / `evomesh evolve` claims a project-wide SQL lease, even when
there are no error observations. The reflector reads a pinned source revision,
identifies a concrete improvement and calls `record_finding` with previously read
paths. These source-review observations are distinct from measured runtime errors.
`read_source` returns character-based pages with `offset`, `total_chars` and
`next_offset`; follow `next_offset` until null to read a complete file. Responses
include JSON escaping in their size budget, so large or escape-heavy files remain
valid JSON below the tool output limit. Files remain bounded to 100 KB. Never build
a full replacement from only one partial source page.

For a localized improvement, `propose_evolution` accepts `edits` instead of `content`
for an existing file. Read the relevant source page first, then provide unique
literal matches (including surrounding context when necessary):

```json
{
  "target": "tool",
  "rationale": "Correct the result; add regression coverage in the same proposal",
  "observation_ids": ["an-observation-id-returned-by-record_finding"],
  "files": [
    {"path": "tool.py", "edits": [{"old": "return 1", "new": "return 2"}]},
    {"path": "tests/test_answer.py", "content": "from tool import answer\n\ndef test_answer():\n    assert answer() == 2\n"}
  ]
}
```

Each file must specify exactly one of `content` or `edits`. An edit contains `old`
(nonempty text) and `new` (text; empty removes the matched fragment). Up to 64 edits
apply sequentially to the pinned Git blob, never to uncommitted working files.
Each match must be unique, including overlapping matches. Missing/ambiguous matches,
missing files, binary sources, no-op results or results exceeding 100 KB are rejected.
If any file fails, no proposal is saved. New files and whole-file deletion still use
`content` (text or null). The reflector must read a file before proposing its edits.

The framework API is `evomesh.evolution.make_edit_change(repo, base, path, edits)`.
It produces the same full-content/before-hash change object as `make_change`, usable
with `submit`. SQL records, evidence links, stale-base detection and integration
checks remain unchanged on SQLite and MySQL. Exact editing reduces model output;
it is not fuzzy matching, a three-way merge or a shortcut around validation.

Active review uses `evolution_max_steps` (default 32), independently of the assistant's
`max_steps` (default 12). Override one CLI run with
`evomesh evolve --evolution-max-steps 40`. The reflector is told to reserve steps for
evidence and a proposal; hitting the bound reports `StepLimitExceeded` rather than
an undifferentiated `RuntimeError`. A larger budget allows more model calls and costs;
it does not guarantee that a review will discover or complete a useful change.

The proposal links evidence IDs and can change skills, tools, soul, loop, tests,
configuration or any other permitted repository text file. One cycle submits at
most one proposal; no worthwhile finding means no proposal. Recent proposal summaries
help the model avoid repeated work; this is not semantic duplicate detection.

SQLite and MySQL use the same claim, lease renewal, expiry recovery and cooldown
logic. `--force` ignores cooldown but cannot steal an active lease. Shared records
contain source findings and safe status metadata; private conversations and API
credentials must never be copied into a proposal. `/evolution-status` (authenticated)
exposes the current cycle, next due time and report without its lease token.

If reflection saves a proposal and then hits its step limit, is cancelled or loses
the model connection, the cycle remains `failed` but its report retains the saved
`proposals` IDs and a safe `error` type. Raw provider errors are not persisted.
Integration does not run after incomplete reflection. An integration-stage error
also preserves the successful reflection's proposal IDs. Inspect those records
before retrying; ordinary cooldowns still apply, and pending proposals can use the
existing integration worker. A saved proposal is not a validated or deployed change.

Framework calls to `Agent.reflect()` raise `evomesh.agent.ReflectionFailed` when
reflection fails after saving proposals. Its `proposal_ids` and `error_type`
attributes allow callers to recover the receipt. Failures before any proposal keep
their original exception types. This covers handled exceptions, not a hard process
kill; after a crash, inspect the durable proposal ledger.

An empty `evolution_checks` records proposals only. Non-empty command arrays invoke
the existing `Integrator` for at most one pending proposal after each review. Run
all desired regression/behavioral gates here; candidate code executes in a detached
worktree using an environment without model/database credentials. Checks have host
permissions, not OS sandboxing: use disposable workers for untrusted candidate code.
Failed checks do not publish the candidate. All integration replicas need one
canonical Git object store, and the integration branch must not be checked out.

Validated commits land on `evolution/main`; this is not automatic GitHub push or
deployment. Inspect the diff, adopt through `evomesh upgrade`, rebuild dependencies
and restart between tasks. The separate Codex recurring development task can build,
test and push project improvements to GitHub; it is not a replacement for the
runtime's SQL-coordinated evolution engine.
