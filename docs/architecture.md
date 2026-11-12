# Architecture and consistency contract

## The two loops

The lower-level `Agent` task loop uses a model and registered tools to complete a task. `SOUL.md` and `skills/*/SKILL.md` are loaded at agent construction. The CLI bounds iterations; tools validate JSON Schema arguments. Errors become concise observations without raw task arguments.

The evolution loop reads recent shared observations, inspects a pinned Git revision, and submits file replacements. A proposal does not execute on submission. Reflection is explicit (`reflect`, `/reflect`), enabled after task friction (`--auto-reflect`), or proactively triggered by an idle worker (`proactive_evolution=true`). Active reviews inspect pinned sources even with no failures, record source-review evidence and optionally invoke the gated integrator. Project-wide SQL leases and cooldowns coordinate replicas; manual `evolve --force` skips cooldown, never a live lease. The current reflector uses the latest 20 project observations; it does not yet track observation consumption or score competing proposals.

## Storage

`observations`: ID, project, instance, kind, detail, timestamp.

`proposals`: ID, project, instance, target category, base commit, rationale, full replacement files, fingerprint, status, lease token/expiry, attempt count, validation report, result commit, timestamps.

SQLAlchemy Core implements the same schema for SQLite and MySQL. The backend-specific settings are SQLite WAL/busy timeout and MySQL LONGTEXT/UTF-8 case-sensitive collation. Timestamps use double precision to preserve subsecond lease comparisons. Initialize schema once before replicas; migrations are not yet implemented.

A fingerprint includes project, base SHA and canonical JSON changes. Identical content from two instances deduplicates; it does not count independent endorsements. Submission ordering within identical timestamps is unspecified. The target category is descriptive and does not restrict source paths.

## Proposal format

```json
{
  "target": "soul",
  "rationale": "Observation IDs and expected improvement",
  "base_commit": "FULL_GIT_COMMIT_SHA",
  "changes": [
    {
      "path": "SOUL.md",
      "before_sha256": "SHA256_OF_FILE_AT_BASE",
      "content": "Complete replacement text\n"
    }
  ]
}
```

Use `make_change(repo, base, path, content)` to derive the hash. A null hash means a new file; null content means deletion. `submit(...)` validates against the exact base revision. A proposal has 1–32 changes and a serialized change payload up to 500 KB. Binary files, symlink edits, submodules, executable-mode changes, and Git metadata are outside the alpha format.

## Claim → validate → publish

1. Find pending proposals or expired validating proposals for this project.
2. CAS the status and lease into a unique worker token. Only one contender wins.
3. Renew the lease in a heartbeat thread. A stale token cannot complete another worker's claim.
4. Read the canonical integration branch. Recover an existing commit carrying this proposal's exact trailer if a prior worker published before losing DB connectivity.
5. Require the base commit to be an ancestor of the integration head. Verify before-hashes against both base and head; changed files become conflicts.
6. Create a detached worktree at that head and apply replacements. Path traversal, private metadata paths, and symlinks are rejected.
7. Run configured argv checks with a timeout. Environment variables are minimized, and candidate `src/` and repository root precede installed code on `PYTHONPATH`. Tests may use dependencies installed in the worker environment. Changed dependencies require operator environment preparation.
8. Reject validation that modifies tracked candidate content. Commit the validated index using `commit-tree`, without hooks or signing.
9. CAS the integration ref from the validated head to the candidate commit. If another worker advanced it, return the proposal to pending for fresh validation.
10. Finish the SQL record with the commit and report. On a Git/SQL crash gap, recovery finds the commit trailer.

Validation drains combined stdout/stderr through the same bounded POSIX pipe
capture used by terminal tools, retaining an 8,000-byte tail per check instead of
spooling full output to disk. Truncated reports include the captured byte count;
the SQL ledger retains its existing final 16,000-character report limit. Successful
checks and nonzero exits still pass through the same publication gates.

Once the renewal thread detects lease loss or a renewal error, the active check
is interrupted and its process group is cleaned up. The capture loop checks that
signal between reads and waits of at most 100 ms, even if the child is quiet or
has closed stdout early. Renewal detection itself follows the existing heartbeat
interval and database response time; this is not a new SQL query every 100 ms.
The signal is also checked before launching each command. Candidate worktrees are removed, and
`LeaseLost` propagates without finishing another owner's claim or publishing a
commit. Normal lease expiry/reclaim remains the recovery path.

Process cleanup also runs on success, timeout and capture errors. It covers the
invocation's process group, not deliberately detached processes or files written
by the candidate. Validation still requires a trusted or independently isolated
runner; bounded capture does not make arbitrary candidate code a sandbox.

There is no distributed transaction across Git and SQL. This protocol provides exclusive live claims, atomic branch publication, and idempotent recovery in a trusted canonical repository. It does not claim global exactly-once execution; validation can run again after a crash. Avoid validators with external side effects.

Terminal states are `integrated`, `conflict`, `rejected`, and `error`. A `pending` retry is used for ref races. Conflict/rejected/error records stay available for inspection; generate a revised proposal to retry them. Errors cannot be silently treated as accepted changes.

## Source and deployment topology

Many agent instances can run from separate immutable checkouts with the same source ancestry and project DB. A central integrator must possess their base commits; fetch released revisions before processing remote-instance proposals.

Multiple integrator processes may share **the same Git object database and refs on one host**, but independent clones are not one authority. The alpha does not sync Git over the ledger. Use one central integrator repository; do not put SQLite or Git worktrees on an unreliable network filesystem.

Integration updates an unchecked-out `evolution/main` branch. `upgrade` requires a clean checkout and fast-forwards it; it does not hot-reload Python modules. Restart between tasks. Container fleets rebuild from a chosen revision and roll out via their normal deployment system.

## Evaluation boundary

Tests establish configured properties, not universal improvement. Add task replay, success-rate, latency/cost budgets, and soul-behavior regression checks before autonomous promotion. If candidate code may edit its own tests, run independent held-out evaluation outside that tree. Operator credentials, deployment permissions, and acceptance policy stay outside candidate source.

A worktree isolates file changes, not processes or network access. The checker environment cleanup is not a security sandbox: candidate code can access the host under the runner user's permissions. Treat all instances with proposal access as code contributors; use disposable restricted execution for untrusted code.


## v0.2 application runtime

`AgentApp` adds configuration, persistent owner-scoped conversation and memory,
background tasks, interval schedules, events and tool extensions around `Agent`.
The CLI and authenticated HTTP API use this same runtime. See [framework guide](framework.md).

New `runtime_*` tables are additive; `evomesh init` creates missing tables without
altering v0.1 evolution records. Completed conversations retain a bounded number of
whole turns, including tool results. Session leases prevent concurrent writers.
Personal data uses project+owner scope; shared evolution observations use project
scope. The DB itself remains a trusted administrative boundary.

Task claims use CAS and heartbeat leases. Expired execution becomes `interrupted`
rather than being silently replayed. User retries are explicit. Schedule advancement
and enqueueing are atomic in one SQL transaction. Task completion and conversation
save are separate, so crash recovery may require inspecting the last saved turn.

Tools may be local functions, configured plugins or MCP stdio tools. Terminal/web
capabilities are opt-in. Named delegation uses separate app configurations and a
bounded thread pool; it is not process isolation. Tool policy and event callbacks
allow applications to integrate their own control and observability layers.
