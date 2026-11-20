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
| `browser` | `open`, `snapshot`, `click`, `fill`, `press`, `select`, `wait`, `back`, `screenshot`; inspect snapshots to choose CSS selectors |
| `list_files_page` | Paginated directory names with listing digest checks; directories end in `/` |
| `read_file_chunk` | Bounded UTF-8 pages with character offsets and optional SHA-256 version checks |
| `search_files` | Literal substring search in a file/directory, bounded pages with path/line citations; at most 1,000 files per call |
| `edit_file` | Preserve UTF-8 text and line endings; reject overlapping/ambiguous matches; optional snapshot digest |

`terminal` and `execute_code` return `exit_code`, `output`, `output_bytes` and
`truncated`. Output combines stdout/stderr and retains the tail of at most 20,000
bytes, shortened further as needed to keep the complete JSON response within
18,000 characters, including escaping. `output_bytes` counts the original
combined bytes; `truncated=true` means some output was omitted. Narrow the command
or printed results before retrying. For Docker, streams are combined in arrival
order; relative stdout/stderr write order inside the container is not guaranteed.
Nonzero exits remain tool errors, while
truncation alone is not an execution failure. Both produce source-linked evolution
evidence without copying command arguments or diagnostics into the shared ledger.
Output is drained through a pipe into a fixed 20,000-byte tail buffer, with
bounded read chunks and no output spool file. Large stdout/stderr streams therefore
do not accumulate in framework memory or on disk. This does not limit files the
command itself writes. Commands are one-shot: their process group is cleaned up
on completion, timeout or capture failure. A child holding an output pipe after
its parent exits is still subject to the original timeout. Detached processes
that deliberately leave the group are outside this cleanup contract; local
execution remains trusted host execution, not process isolation. Docker cleanup
also removes the named container. These guarantees use POSIX process groups and
pipe selectors (Linux/macOS), not a Windows terminal backend.

On timeout, `terminal` and `execute_code` give the assistant an `ExecutionTimeout`
error with a bounded diagnostic tail in `feedback`, `timeout_seconds`, and
`output_bytes` (bytes captured before the deadline). `output_incomplete` is always
true: a timeout cannot establish successful completion. `feedback_truncated`
separately reports whether captured output was dropped for the tail/JSON budget;
it may be false even though execution was interrupted. The complete error receipt
fits within 18,000 characters, retaining the end of diagnostics when shortened.
The assistant must inspect earlier effects before retrying; no automatic replay
is introduced. Output is untrusted and remains part of the private conversation,
while shared evolution records contain error type, capability and source paths
without command arguments or diagnostics. Truncated feedback also increments the
existing output-limit metric and evidence.

Framework callers still catch `TimeoutError`, or import the more specific
`ExecutionTimeout` from `evomesh.capabilities`. Its `timeout`, `output` (bounded
bytes), and `output_bytes` attributes expose the captured evidence; `model_result()`
returns the same bounded receipt used by the assistant. Exception messages do not
contain raw output. Normal completed-command results are unchanged.

Terminal/code timeouts must be finite positive numbers of seconds (fractional
values are supported). Tool factories reject zero, negatives, booleans, nonnumeric
values, NaN and infinity before creating a workspace or launching local/Docker
commands. `Integrator` applies the same timeout validation before Git/SQL work;
its lease and `ProactiveEvolution` leases must also be finite and at least 3
seconds. This prevents malformed limits from starting work with an unusable
deadline or lease. Low-level `capture_output` callers already own a spawned
process: invalid timeout or tail-buffer arguments raise `ValueError` after process
group cleanup and pipe closure. Valid timeout diagnostics and lease-loss behavior
are unchanged.

For complete directory discovery, use `list_files_page` (default `path="."`,
`offset=0`, `limit=100`; at most 200 entries per page). It returns sorted `entries`,
`total_entries`, `offset`, `next_offset`, and `sha256`. Follow `next_offset` with
the same path and digest as `expected_sha256` until null. Page size may shrink to
keep the complete JSON within 18,000 characters, including escaped filenames.
Directories end with `/`; hidden entries, symlinks and special files are excluded.
The legacy `list_files` still returns only the first 200 names as a list.

```json
{"path": "notes", "offset": 100, "expected_sha256": "<sha256 from the first page>"}
```

`DirectoryChanged` provides restart guidance to the assistant and source-linked
error evidence to evolution without copying directory names into the shared
ledger. Framework callers obtain the same tool from `workspace_tools(workspace)`
and can catch `evomesh.tools.DirectoryChanged`. Each call enumerates and sorts the
whole directory; only response size is bounded. The digest checks visible names
and file/directory types, not file contents, and is not a filesystem lock or an
atomic snapshot under concurrent mutation. Restart at zero without the old digest
when entries change. Ordinary next-page availability is not an output-limit error.

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

`search_files` accepts a file or directory `path`, an optional `offset` (number of
matching lines to skip) and `limit` (1–100 results, default 100). Results retain
the existing `matches` and `truncated` fields, adding `next_offset`,
`scan_limit_reached`, `scanned_files` and `skipped_files`. The complete JSON page
fits within 18,000 characters, including escaping. Follow `next_offset` with the
same query/path; fewer than `limit` results may be returned to fit the budget.
Each citation keeps its workspace-relative path, line number and up to 500
characters of text around the first literal match on that line. Short lines keep
the existing `path`, `line`, `text` shape. Long lines additionally report `column`
(1-based position of the first match), `match_chars` (full query length),
`text_offset` (0-based start of the excerpt within the line), `line_chars`, and
`text_truncated=true`. Positions count Unicode characters, not UTF-8 bytes or
visual columns; tabs are one character. A query of up to 500 characters remains
fully visible, with preceding context when space permits. For a longer query,
the excerpt begins at the match and shows its first 500 characters; read the
file to inspect the full match or surrounding text.

`text_truncated` describes a line excerpt, while the page-level `truncated`
indicates another result page or the scan ceiling. Excerpt clipping alone is not
an output-limit observation because the complete file remains available through
file reading. This applies equally to assistant calls and `workspace_tools` users.

Each call rescans in deterministic directory/file order, examining at most 1,000
candidate files. Hidden entries and directory symlinks are excluded from recursive
discovery; explicit paths still use the workspace/private-metadata checks. Reads
are bounded to 100 KB and skip binary, invalid UTF-8, oversized or unreadable files,
counting them in `skipped_files`. Missing or unsupported search roots raise an error.
If `scan_limit_reached=true`, `next_offset` is null and `truncated=true`: narrow
the path rather than interpreting empty results as a complete negative search.
The scan ceiling produces source-linked truncation evidence; ordinary pagination
does not count as lost output. The personal assistant and `workspace_tools(root)`
framework API share this behavior.

Pages are live searches, not a filesystem snapshot. Concurrent file edits may
shift matching-line offsets; restart or narrow the search when consistency matters.
The usual task step/context budgets still apply to repeated tool calls.

Both the personal assistant and embedded `AgentApp` register this tool by default;
framework users can also obtain it through `workspace_tools(root)`. Tool failures
carry `src/evomesh/tools.py` provenance into the SQL evolution ledger, without
storing file contents or arguments in the failure observation.

`read_file` and `read_file_chunk` preserve decoded UTF-8 text, including BOM and
CRLF/CR/LF line endings; neither performs newline normalization. `edit_file`
replaces one literal occurrence and leaves all other characters intact. Overlapping
occurrences count as ambiguous (`aa` occurs twice in `aaa`), so include more context
to make the match unique. Missing/ambiguous matches, invalid UTF-8, NUL bytes and
edits that would exceed 100 KB are rejected before writing.

For an edit based on a prior read, pass the `sha256` returned by `read_file_chunk`
as `expected_sha256` to `edit_file`. A changed file raises `FileVersionChanged`
even if the targeted fragment is still present; reread and reconsider the edit.
Existing calls without the digest remain supported. This is a check against the
bytes read at edit start, not an atomic filesystem compare-and-swap or distributed
lock. Applications with concurrent writers must still coordinate writes. Both
assistant and framework tools use this behavior, and rejected edits feed the same
source-linked evolution observations without copying file contents or paths.

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

Every browser result fits within an 18,000-character JSON budget, including
escaping and screenshot paths. `original_chars` records the full lengths of
`url`, `title`, `snapshot` and `text`; `truncated_fields` names clipped fields and
`truncated` reports whether any were clipped. Initial field limits are 4,096 URL,
1,000 title, 12,000 accessibility snapshot and 6,000 text characters; fields may
shrink further to fit the total budget. Clipped strings are previews, including
the accessibility text; do not treat them as complete page evidence. This bounds
returned output, not Chromium memory or the initial extraction size.

To recover content outside a whole-page preview, use `snapshot` with a CSS
`selector` matching one element, for example:

```json
{"action": "snapshot", "selector": "main"}
```

Framework callers use `session.operate("snapshot", selector="main")`. Only the
accessibility/text fields are scoped; URL/title remain page metadata. Omitting
the selector reads the body. Other actions continue to return whole-page
snapshots. This is a fresh live read, not pagination or a frozen DOM snapshot;
a selected element can still exceed the budget. Clipping increments browser
output-limit metrics and source-linked evolution evidence without copying page
text, titles, selectors or URLs into the shared ledger.

Browser navigation and subrequests must pass configured host, HTTPS and public-IP
checks. Redirects are intercepted too; service workers and WebSockets are blocked.

Form controls support `press` with a CSS `selector` and `key` (for example `Enter`,
`Tab`, `Backspace` or a Playwright key combination). `select` targets a native HTML
`<select>` using exactly one of `values` or `labels`, each an array of strings.
Labels let the assistant choose options visible in the accessibility snapshot
without guessing hidden HTML values. Multiple selections require a native
`multiple` control; an empty array clears selection. Custom dropdown widgets use
their normal click/keyboard interactions instead of `select`.

```json
{"action": "select", "selector": "#language", "labels": ["中文"]}
```

```json
{"action": "press", "selector": "#query", "key": "Enter"}
```

Both actions return the updated page snapshot. They can trigger page events,
submission or navigation and remain subject to existing host routing checks and
tool policy hooks. Framework callers use the same operations, for example
`session.operate("select", selector="#colors", values=["red", "blue"])` and
`session.operate("press", selector="#query", key="Enter")`. No new configuration
is needed when browser tools are enabled. Failures retain browser source provenance
in the evolution ledger without copying selectors, input or option contents.

For asynchronously updated pages, use `wait` with a CSS `selector` after the
triggering action. `state` defaults to `visible`; `hidden` also accepts a removed
element, `attached` requires DOM presence (even if hidden), and `detached` requires
absence. These follow [Playwright locator wait semantics](https://playwright.dev/python/docs/api/class-locator#locator-wait-for).

```json
{"action": "wait", "selector": "#results", "state": "visible"}
```

```json
{"action": "wait", "selector": "#loading", "state": "hidden"}
```

A successful wait returns a fresh page snapshot; an already satisfied condition
returns immediately. Waiting does not repeat the preceding click or submission.
Each wait uses the session's operation timeout (15 seconds by default); framework
callers can choose a finite positive `BrowserSession(..., timeout=5)` in seconds
and call `session.operate("wait", selector="#results")`. This timeout bounds the
condition wait, not the combined wait and subsequent snapshot. An unmet condition
raises `TimeoutError` and leaves the session usable; AgentApp records a tool error
with browser source provenance in the shared SQL evolution ledger, without the
selector or page content. A hidden/absent indicator alone does not prove an
operation succeeded; inspect the resulting page. This is element-state waiting,
not a general network-idle or text-matching API.

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
`list_source(prefix="", offset=0, limit=100)` discovers tracked paths in JSON pages
at that same pinned Git commit. It returns `base_commit`, `prefix`, `offset`,
`total_paths`, `paths` and `next_offset`; continue with the same prefix until
`next_offset` is null. Prefixes are literal repository-relative text (for example,
`src/evomesh/`), not glob patterns. Offsets count matching paths, and limits range
from 1 to 200. Pages fit 18,000 characters including JSON escaping, so fewer than
`limit` paths may be returned. Unicode, spaces, tabs and newlines in Git path names
are preserved. This replaces the old truncated newline-delimited path string.

The path catalog is loaded into memory on first use and reused for this review;
concurrent HEAD changes do not change its pages or `read_source`'s revision.
Listing a path does not count as reading it for a finding, and does not bypass
existing source-file eligibility checks. Narrow prefixes to conserve model steps:
pagination removes the inaccessible suffix, but does not make an arbitrarily
large repository cheap to enumerate or review.

`read_source` returns character-based pages with `offset`, `total_chars` and
`next_offset`; follow `next_offset` until null to read a complete file. Responses
include JSON escaping in their size budget, so large or escape-heavy files remain
valid JSON below the tool output limit. Before reading a Git blob, the shared source
reader checks its pinned tree metadata and rejects files larger than 100,000 bytes.
This also applies to proposal creation and integration, including deletion of an
oversized existing file. Full replacement/new-file content has the same byte limit
as exact-edit results; older or directly queued oversized proposals are rejected
by the integration gate. This bounds source bytes returned to Python, not Git's own
memory use when reading packed objects. Never build a full replacement from only
one partial source page.

Manual and proactive reflection start with summaries of the latest 20 observations
and 10 proposals in the configured project. The summary JSON is bounded to 18,000
characters including escaping; text previews are shortened, with original lengths
and explicit truncation flags. Large evidence no longer fills the initial prompt
before the first model call. This is a bound on the evidence section, not the
whole request (source catalogs, tool schemas and instructions also consume context).

The reflector's `read_review_record` tool reads full fields on demand:

```json
{"record_type": "proposal", "record_id": "ID_FROM_SUMMARY", "field": "report", "offset": 0, "limit": 12000}
```

Use `observation/detail`, `proposal/rationale` or `proposal/report`. Follow
`next_offset` until null; each complete JSON page remains within 18,000 characters.
Proposal metadata includes status, base/result commits and linked evidence IDs,
but excludes lease tokens and full change payloads. Selected records are held in
memory for the review, so later ledger updates cannot mix versions between pages.
New source findings from this cycle are readable too. Unknown IDs, records outside
the selected window, other projects and invalid field combinations are rejected.
This is a bounded review window, not a paginated archive of the entire ledger.

Read previous validation reports to understand rejected/conflicting candidates,
then inspect the pinned source and propose a corrected change with evidence. A
report describes a previous attempt; it does not validate the new one. Reports
are untrusted data and never authorize weakening the operator's checks. The
existing SQL evidence links, test gates and explicit deployment remain in place.
`Agent.reflect()` and `AgentApp.evolve_once()` share this behavior without a schema
migration on SQLite or MySQL.

When an integration check times out, its rejection report retains earlier checks'
reports and up to 8,000 bytes of the timed-out command's captured output tail.
The report identifies the deadline, bytes captured before it, and whether older
output was truncated. Partial output is explicitly incomplete: it is not a passing
gate, and later checks do not run. Process-group cleanup completes before the
report is returned; neither the integration branch nor running code adopts the
candidate. The next review can inspect this diagnostic with `read_review_record`
and propose a correction that must pass the same checks. As with other validation
reports, candidate output is stored in the shared ledger; use synthetic validation
data and avoid printing credentials or private conversation content.

At the framework capture layer, deadlines raise `evomesh.processes.CaptureTimeout`,
a `TimeoutError` subclass with `timeout`, `output` (bounded bytes) and `output_bytes`
(bytes captured before the deadline). Its string representation contains no output.
Existing `except TimeoutError` handlers continue to work for terminal/code tools;
their ordinary model-facing error still omits raw timeout output. Guards such as
SQL lease-loss checks continue to interrupt validation independently of deadlines.

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
