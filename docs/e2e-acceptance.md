# Multi-agent E2E acceptance

E2E tests run in a temporary Git repository, with a dedicated PostgreSQL namespace and real Provider credentials from `.env`. They must never use the product repository as the Agent target.

## Deterministic oracle

The test harness owns pass/fail assertions: Job and Integration state transitions, Artifact schema validity, Git commit ancestry, changed-file allowlists, command exit codes, and cleanup. An LLM is never the sole oracle.

## LLM-assisted cases

Use the real Planner only to produce a `penhin.dag/v1` plan constrained by a scenario contract. Validate the returned plan before execution: expected role graph, allowed paths, maximum node count, and required final node. Use a separate verifier call only to classify human-readable evidence; compare its structured verdict against deterministic checks and record disagreement as a failure.

## Required scenarios

1. Happy path: explore → implement → verify, committed change set, integration, quality gate, and cleanup.
2. Invalid Artifact: malformed handoff fails its Job and prevents dependent dispatch.
3. Restart: stop/restart Scheduler while a Worker is running; no duplicate attempt is created.
4. Conflict: two valid change sets overlap; Integration reaches `needs_resolution` and target branch remains unchanged.
5. Cancellation and timeout: Worker process group is terminated and the database terminal state matches the event trail.

Every scenario must create and remove its temporary repository, Agent worktrees, branches, integration worktrees, and database records in `finally` cleanup. Store the LLM prompt, structured response, model identifier, and run IDs as test evidence, but never store credentials.
