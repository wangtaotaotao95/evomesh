# Security model

This alpha assumes a trusted owner and trusted instances sharing a project ledger. Project IDs are queue namespaces, not tenant authorization. Database access allows contributing executable candidate source. Do not expose database credentials publicly.

The API requires a bearer token and binds to loopback by default. Use TLS and application-level access controls when exposing it beyond localhost. Built-in workspace tools restrict paths but are not designed to defend against a hostile process racing filesystem symlink changes. Registered custom tools run with the application user's permissions.

Candidate validation executes Python or other configured commands in a detached worktree. This is **not an OS sandbox**. Environment filtering and timeouts reduce accidental leakage/hangs, but do not prevent malicious filesystem or network access. Use a disposable restricted runner, no sensitive host mounts, no production credentials, and an independent evaluator for untrusted proposals.

Do not publish secrets, private conversations, or proprietary files in observations/proposals. Default runtime observations omit raw tool arguments, but explicit feedback and model-generated changes require the same care as code commits.

For a suspected vulnerability, avoid posting exploitable details or credentials in a public issue. Use GitHub private vulnerability reporting if enabled, or contact the maintainer through their public GitHub profile to arrange a private report. This document does not promise a response SLA.


## v0.2 runtime data and capabilities

`AgentApp` persists completed conversation turns, tool results, task prompts and
results. Project+owner names scope application queries but do not prevent an
administrator or a process with raw DB access from reading all records. The HTTP
service has one configured owner and one bearer token, not multi-tenant login.

Local terminal and configured Python/MCP plugins are trusted host execution.
Docker terminal is restricted and read-only; do not mount a Docker socket in an
untrusted agent container. Web reading uses an allowlist and address preflight,
but hostile DNS requires an external egress policy. Neither these primitives nor
the detached evolution worktree are a universal security sandbox.

Task cancellation is cooperative. In-flight external effects can complete; expired
worker tasks require explicit inspection/retry. Schedule execution may spend model
credits while the task worker is running. Keep instance clocks synchronized.
