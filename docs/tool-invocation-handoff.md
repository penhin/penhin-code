# Tool Invocation Refactor Handoff

## Goal

Complete the Tool Invocation refactor so one deep module owns model-requested tool execution: budget checks, delegation limits, schema validation, permission and approval, ordered batching, Tool Effects, result blocks, and observation. The agent message module should only project model blocks and pass them to Tool Invocation.

## Confirmed decisions

- Put the deep module at `penhin/tools/execution/invocation.py`.
- Every registered `ToolSpec.handler` returns a `ToolOutcome`; do not retain a raw-`Result` execution path.
- A handler must not receive or mutate `RunContext`.
- `ToolOutcome` carries an open `ToolEffect(kind, payload)` list.
- Tool Invocation owns an effect-kind registry. Each entry owns payload schema validation and the effect implementation.
- Effects are serial. Effect failure fails that Tool Invocation and prevents later effects.
- Record applied or failed effects in `ToolRun` metadata and the `tool_call_completed` observation event.
- First migrated tools: `compact`, `snip`, `enter_plan`, `exit_plan`.
- `compact` must not also be specially requested by `agent/loop.py`; a single effect path owns it.

## Domain and decision records

- `CONTEXT.md` defines **Tool Invocation**, **Tool Outcome**, and **Tool Effect**.
- `docs/adr/0002-govern-tool-effects-through-invocation.md` records the architectural decision.

## Current commits

Start review and work from commit `c20edcb` on `main`.

```text
acfdba7 refactor: govern tool effects through invocation
c20edcb fix: require tool outcomes at execution seam
```

The initial baseline for reviewing this work is `27cdac3`.

## What exists now

- `ToolEffect` and `ToolOutcome` exist in `penhin/tools/types.py`.
- `ToolInvocation` exists in `penhin/tools/execution/invocation.py`.
- The four selected tools declare effects in `penhin/tools/registry.py`.
- `ToolRun` and `tool_call_completed` now carry effect records.
- `ToolSpec.__post_init__` currently coerces handler results to `ToolOutcome`.
- Focused tests exist in `tests/test_tool_invocation.py`.

## Known blockers: do not treat current code as complete

1. `ToolInvocation.invoke()` delegates to `service.run_tool()`. It is not the real execution seam.
2. `penhin/agent/messages.py` still owns tool budgets, delegation limits, parallel batching, approval resolution, ordering, and result-block construction.
3. Effect handling currently maps only `kind -> callback`; payload validation is ad hoc inside callbacks. Replace it with a registry entry that contains a payload schema and executor.
4. `ToolSpec.__post_init__` is an implicit coercion adapter. Replace it with an explicit uniform handler definition/factory or migrate registry handlers directly, so the `ToolSpec` interface honestly requires `ToolOutcome`.
5. `agent/loop.py` still responds to `manual_compact`; remove the duplicate effect-specific path once Tool Invocation applies the compact effect.
6. The existing effect executor delegates plan effects to `penhin.tools.builtin.plan_mode`. That is acceptable only while the effect executor remains the sole caller; handlers must remain context-free.

## Required implementation sequence

1. Move `ToolCall`, `ToolExecutionContext`, budget/delegation checks, batching, ordered result creation, and approval-resolver use from `penhin/agent/messages.py` into `ToolInvocation` (or a private collaborator owned by it).
2. Keep `agent/messages.py` as a thin projection module: parse model blocks, invoke Tool Invocation, attach cache control.
3. Make `ToolInvocation` receive a real effect registry. Define one registry entry per effect with payload schema and executor; reject unknown fields, missing fields, and wrong types before invoking an executor.
4. Remove the raw-`Result` branch from `penhin/tools/execution/service.py`. Do not leave two handler protocols.
5. Replace `ToolSpec.__post_init__` coercion with an explicit, uniform `ToolOutcome` handler contract. Update direct handler tests to inspect `.result` where they intentionally test the handler seam.
6. Make the compact effect the only source of compact scheduling; ensure it does not run twice.
7. Add tests through the Tool Invocation seam for: serial effect ordering, schema rejection before mutation, effect failure stopping later effects, effects in observation metadata, no duplicate compact scheduling, parallel result ordering for effect-free tools, and approval/budget/delegation behavior.

## Test-state isolation

`tests/test_tools.py` shares persistent task state through the default task-state location. When a prior failure leaves a current task, later task tests see `Current task` and fail. Fix this with an autouse fixture or dependency injection that assigns each test a temporary `TaskStatusManager`; do not rely on manually clearing local `.tasks` state.

## Verification

Run focused tests frequently:

```powershell
python -m pytest tests/test_tool_invocation.py tests/test_message_flow.py tests/test_tool_runtime.py tests/test_plan.py tests/test_tools.py -q
```

Then run the full suite:

```powershell
python -m pytest -q
```

On this Windows environment, unrelated existing failures may occur because `penhin/evaluation/shared_budget.py` imports Unix-only `fcntl` and auth tests assert Unix `0600` mode. Report them separately; do not weaken those tests as part of this refactor.

## Review requirement

Before final delivery, review `git diff 27cdac3...HEAD` on two axes:

- Standards: repo guidance plus code smells.
- Spec: every confirmed decision above and ADR-0002.

Do not claim completion until Tool Invocation is the actual single execution seam.
