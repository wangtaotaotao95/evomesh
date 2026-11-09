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
occupy that worker until finished. Use separate task-worker replicas if necessary.
Background inference has provider costs; enable it deliberately. Framework defaults
remain off so importing or creating an app does not silently begin paid inference.

## Tool behavior

| Tool | Behavior |
|---|---|
| `terminal` | Explicit argv, no implicit shell; exit code and bounded output; timeout kills process group |
| `execute_code` | One-shot Python (`-I`) or local Node.js; print results; same backend/timeout as terminal |
| `browser` | `open`, `snapshot`, `click`, `fill`, `back`, `screenshot`; inspect snapshots to choose CSS selectors |
| `search_files` | Literal substring search, path/line citations, at most 1,000 files and 100 hits |
| `edit_file` | Read first; replace exactly one literal match; rejects missing/ambiguous matches |

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
