# Architecture and consistency contract

## The two loops

The task loop uses a model and registered tools to complete a task. `SOUL.md` and `skills/*/SKILL.md` are loaded at agent construction. The CLI bounds iterations; tools validate JSON Schema arguments. Errors become concise observations without raw task arguments.

The evolution loop reads recent shared observations, inspects a pinned Git revision, and submits file replacements. A proposal does not execute on submission. Reflection is explicit (`reflect`, `/reflect`) or enabled after task friction (`--auto-reflect`). The current reflector uses the latest 20 project observations; it does not yet track observation consumption or score competing proposals.

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

There is no distributed transaction across Git and SQL. This protocol provides exclusive live claims, atomic branch publication, and idempotent recovery in a trusted canonical repository. It does not claim global exactly-once execution; validation can run again after a crash. Avoid validators with external side effects.

Terminal states are `integrated`, `conflict`, `rejected`, and `error`. A `pending` retry is used for ref races. Conflict/rejected/error records stay available for inspection; generate a revised proposal to retry them. Errors cannot be silently treated as accepted changes.

## Source and deployment topology

Many agent instances can run from separate immutable checkouts with the same source ancestry and project DB. A central integrator must possess their base commits; fetch released revisions before processing remote-instance proposals.

Multiple integrator processes may share **the same Git object database and refs on one host**, but independent clones are not one authority. The alpha does not sync Git over the ledger. Use one central integrator repository; do not put SQLite or Git worktrees on an unreliable network filesystem.

Integration updates an unchecked-out `evolution/main` branch. `upgrade` requires a clean checkout and fast-forwards it; it does not hot-reload Python modules. Restart between tasks. Container fleets rebuild from a chosen revision and roll out via their normal deployment system.

## Evaluation boundary

Tests establish configured properties, not universal improvement. Add task replay, success-rate, latency/cost budgets, and soul-behavior regression checks before autonomous promotion. If candidate code may edit its own tests, run independent held-out evaluation outside that tree. Operator credentials, deployment permissions, and acceptance policy stay outside candidate source.

A worktree isolates file changes, not processes or network access. The checker environment cleanup is not a security sandbox: candidate code can access the host under the runner user's permissions. Treat all instances with proposal access as code contributors; use disposable restricted execution for untrusted code.
