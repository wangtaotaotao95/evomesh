# Security model

This alpha assumes a trusted owner and trusted instances sharing a project ledger. Project IDs are queue namespaces, not tenant authorization. Database access allows contributing executable candidate source. Do not expose database credentials publicly.

The API requires a bearer token and binds to loopback by default. Use TLS and application-level access controls when exposing it beyond localhost. Built-in workspace tools restrict paths but are not designed to defend against a hostile process racing filesystem symlink changes. Registered custom tools run with the application user's permissions.

Candidate validation executes Python or other configured commands in a detached worktree. This is **not an OS sandbox**. Environment filtering and timeouts reduce accidental leakage/hangs, but do not prevent malicious filesystem or network access. Use a disposable restricted runner, no sensitive host mounts, no production credentials, and an independent evaluator for untrusted proposals.

Do not publish secrets, private conversations, or proprietary files in observations/proposals. Default runtime observations omit raw tool arguments, but explicit feedback and model-generated changes require the same care as code commits.

For a suspected vulnerability, avoid posting exploitable details or credentials in a public issue. Use GitHub private vulnerability reporting if enabled, or contact the maintainer through their public GitHub profile to arrange a private report. This document does not promise a response SLA.
