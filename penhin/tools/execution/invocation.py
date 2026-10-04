"""The single governed execution seam for model-requested tools."""
from __future__ import annotations
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock
import itertools
import time
from typing import Any
from penhin.result import Result
from penhin.tools.catalog import ToolCatalog
from penhin.tools.registry import DEFAULT_TOOL_CATALOG
from penhin.tools.types import ToolCategory, ToolEffect, ToolInput, ToolOutcome
from .approval import ApprovalFlow, PermissionPolicy, default_approval_flow
from .service import ToolRun, check_tool_access
from .validation import validate_tool_input
from .validation import unknown_tool_input_fields
from .observability import log_tool_blocked, log_tool_done, log_tool_start

_CALL_IDS = itertools.count(1)

EffectExecutor = Callable[[dict[str, Any], object], Result]
EffectDefinition = tuple[dict[str, Any], EffectExecutor]
ApprovalResolver = Callable[[str, dict[str, Any], PermissionPolicy, ApprovalFlow], ToolRun]
DELEGATION_TOOLS = {"task", "verify"}

def _compact(_payload: dict[str, Any], context: object) -> Result:
    if context is not None: context.request_force_compact()
    return Result.success()

def _snip(payload: dict[str, Any], context: object) -> Result:
    if context is None: return Result.failure("No active session to snip.", code="missing_context")
    value = payload["selectors"]
    selector_texts = value.split() if isinstance(value, str) else [str(item) for item in value]
    try:
        from penhin.agent.context import parse_snip_selectors
        selectors = parse_snip_selectors(selector_texts)
    except ValueError:
        return Result.failure("Invalid snip selector. Use turn numbers or ranges like 2 or 2-4.", code="invalid_tool_input")
    snipped = context.force_snip_turns(selectors)
    return Result.success(f"Marked {snipped} messages as snipped.", snipped=snipped)

def _enter_plan(_payload: dict[str, Any], context: object) -> Result:
    from penhin.tools.builtin.plan_mode import run_enter_plan
    return run_enter_plan(context)

def _save_plan_and_exit(payload: dict[str, Any], context: object) -> Result:
    from penhin.tools.builtin.plan_mode import run_exit_plan
    return run_exit_plan(payload["plan_content"], context)

def _present_plan(payload, context) -> Result:
    from penhin.agent.planning import present_plan
    return present_plan(context, payload["alternatives"])

DEFAULT_EFFECTS: dict[str, EffectDefinition] = {
    "present_plan": ({"required": {"alternatives": (list, type(None))}, "optional": set()}, _present_plan),
    "compact_context": ({"required": {}, "optional": set()}, _compact),
    "snip_turns": ({"required": {"selectors": (str, list)}, "optional": set()}, _snip),
    "enter_plan_mode": ({"required": {}, "optional": set()}, _enter_plan),
    "save_plan_and_exit": ({"required": {"plan_content": str}, "optional": set()}, _save_plan_and_exit),
}

@dataclass(frozen=True)
class ToolCall:
    index: int
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str

@dataclass
class ToolExecutionContext:
    policy: PermissionPolicy
    approval: ApprovalFlow
    approval_resolver: ApprovalResolver | None = None
    run_context: object | None = None
    max_tool_calls: int | None = None
    tool_calls_used: int = 0
    catalog: ToolCatalog = DEFAULT_TOOL_CATALOG
    budget_lock: Lock = field(default_factory=Lock)

def collect_tool_calls(content: Any) -> list[ToolCall]:
    if not isinstance(content, list): return []
    def value(block: Any, key: str, default: Any = None) -> Any:
        return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)
    return [ToolCall(index, str(value(block, "name", "")), value(block, "input", {}) or {}, str(value(block, "id", ""))) for index, block in enumerate(content) if value(block, "type") == "tool_use"]

def tool_result_block(call: ToolCall, run: ToolRun) -> dict[str, Any]:
    return {"type": "tool_result", "tool_name": call.tool_name, "tool_use_id": call.tool_use_id, "content": run.result.to_json()}

class ToolInvocation:
    """Owns governed tool execution, ordered result blocks, and Tool Effects."""
    def __init__(self, effect_handlers: dict[str, EffectDefinition | EffectExecutor] | None = None) -> None:
        self._effect_handlers = DEFAULT_EFFECTS if effect_handlers is None else effect_handlers

    def invoke(self, tool_name: str, tool_input: ToolInput, policy: PermissionPolicy, approval: ApprovalFlow | None = None, context: object = None, catalog: ToolCatalog = DEFAULT_TOOL_CATALOG) -> ToolRun:
        approval = approval or default_approval_flow(policy, catalog)
        call_id, start = f"tool-{next(_CALL_IDS)}", time.perf_counter()
        spec = catalog.get(tool_name)
        planning = getattr(context, "planning", None)
        if planning is not None and planning.active and spec is not None and tool_name != "plan" and spec.category != ToolCategory.readonly:
            return ToolRun(Result.failure("Implementation waits for user plan selection. Use read to inspect and plan to present three alternatives.", code="plan_selection_required"))
        access = check_tool_access(tool_name, tool_input, policy, approval, catalog)
        if access is not None:
            log_tool_blocked(call_id, tool_name, tool_input, access.result, (time.perf_counter() - start) * 1000, "approval_required" if access.approval_required else "blocked")
            return access
        invalid = validate_tool_input(tool_name, tool_input, catalog)
        if invalid: return ToolRun(invalid)
        unknown_fields = unknown_tool_input_fields(tool_name, tool_input, catalog)
        if unknown_fields:
            import logging
            import json
            logging.getLogger("penhin.tool_runtime").warning(f"[tool] unknown_input call_id={call_id} name={tool_name} fields={json.dumps(unknown_fields)}")
        log_tool_start(call_id, tool_name, tool_input)
        spec = catalog.get(tool_name)
        if spec is None or spec.handler is None: return ToolRun(Result.failure(f"Unknown tool handler: {tool_name}", code="unknown_tool_handler"))
        try: outcome = spec.handler(**tool_input)
        except TypeError as error: return ToolRun(Result.failure(f"Invalid input for {tool_name}: {error}", code="invalid_tool_input"))
        except Exception as error: return ToolRun(Result.failure(f"Tool {tool_name} failed: {error}", code="tool_error"))
        if not isinstance(outcome, ToolOutcome): return ToolRun(Result.failure(f"Tool {tool_name} returned no ToolOutcome", code="invalid_tool_outcome"))
        run = self.apply_outcome(tool_name, outcome, context)
        log_tool_done(call_id, tool_name, run, (time.perf_counter() - start) * 1000)
        self._observe_completion(tool_name, tool_input, run)
        return run

    @staticmethod
    def _observe_completion(tool_name: str, tool_input: ToolInput, run: ToolRun) -> None:
        """Keep effect records on the same completion event as the tool result."""
        from penhin.evaluation.observer import emit
        from .observability import short_hash
        emit(
            "tool_call_completed",
            tool_name=tool_name,
            input_digest=short_hash(tool_input),
            status="ok" if run.result.ok else "error",
            code=run.result.meta.get("code"),
            effects=run.effects,
        )

    def apply_outcome(self, tool_name: str, outcome: ToolOutcome, context: object) -> ToolRun:
        if not outcome.result.ok: return ToolRun(outcome.result)
        effects: list[dict[str, str]] = []
        for effect in outcome.effects:
            definition = self._effect_handlers.get(effect.kind)
            if definition is None:
                effects.append({"kind": effect.kind, "status": "failed", "code": "unknown_tool_effect"})
                return ToolRun(Result.failure(f"Unknown Tool Effect: {effect.kind}", code="unknown_tool_effect"), effects=effects)
            schema, executor = ({"required": {}, "optional": set()}, definition) if callable(definition) else definition
            error = self._validate_effect(effect, schema)
            if error:
                effects.append({"kind": effect.kind, "status": "failed", "code": "invalid_tool_effect"})
                return ToolRun(Result.failure(error, code="invalid_tool_effect"), effects=effects)
            result = executor(effect.payload, context)
            if not result.ok:
                effects.append({"kind": effect.kind, "status": "failed", "code": result.meta.get("code", "tool_effect_failed")})
                return ToolRun(result, effects=effects)
            outcome.result.message = result.message or outcome.result.message
            outcome.result.data = result.data if result.data is not None else outcome.result.data
            outcome.result.meta.update(result.meta)
            effects.append({"kind": effect.kind, "status": "applied"})
        return ToolRun(
            outcome.result,
            manual_compact=any(effect["kind"] == "compact_context" and effect["status"] == "applied" for effect in effects),
            effects=effects,
        )

    @staticmethod
    def _validate_effect(effect: ToolEffect, schema: dict[str, Any]) -> str | None:
        if not isinstance(effect.payload, dict): return f"Tool Effect {effect.kind}.payload must be an object"
        required = schema["required"]
        unknown = set(effect.payload) - set(required) - set(schema.get("optional", set()))
        if unknown: return f"Tool Effect {effect.kind} has unknown fields: {', '.join(sorted(unknown))}"
        for key, expected in required.items():
            if key not in effect.payload: return f"Tool Effect {effect.kind} is missing required field: {key}"
            if not isinstance(effect.payload[key], expected): return f"Tool Effect {effect.kind}.{key} has the wrong type"
        return None

    def execute_blocks(self, content: Any, execution: ToolExecutionContext) -> tuple[list[dict[str, Any]], bool]:
        calls, results, batch, manual_compact = collect_tool_calls(content), [], [], False
        def flush() -> None:
            nonlocal batch, manual_compact
            if not batch: return
            with ThreadPoolExecutor(max_workers=5) as executor: completed = list(executor.map(lambda call: self._run_call(call, execution), batch))
            results.extend(block for _index, block, _compact in sorted(completed)); manual_compact = manual_compact or any(compact for _index, _block, compact in completed); batch = []
        for call in calls:
            spec = execution.catalog.get(call.tool_name)
            if spec is not None and spec.parallel_safe and not spec.approval.requires_approval: batch.append(call)
            else:
                flush(); _index, block, compact = self._run_call(call, execution); results.append(block); manual_compact = manual_compact or compact
        flush()
        return results, manual_compact

    def _run_call(self, call: ToolCall, execution: ToolExecutionContext) -> tuple[int, dict[str, Any], bool]:
        with execution.budget_lock:
            exhausted = execution.max_tool_calls is not None and execution.tool_calls_used >= execution.max_tool_calls
            if not exhausted:
                execution.tool_calls_used += 1
        if exhausted:
            run = ToolRun(Result.failure(f"Tool budget exhausted before {call.tool_name}.", code="tool_budget_exhausted"))
        else:
            blocked = execution.run_context.post_delegation_tool_block(call.tool_name) if execution.run_context is not None else None
            run = ToolRun(blocked) if blocked else self.invoke(call.tool_name, call.tool_input, execution.policy, execution.approval, execution.run_context, execution.catalog)
        if run.approval_required and execution.approval_resolver is not None: run = execution.approval_resolver(call.tool_name, call.tool_input, execution.policy, execution.approval)
        if execution.run_context is not None and call.tool_name in DELEGATION_TOOLS and run.result.ok: execution.run_context.activate_post_delegation_guard(call.tool_name)
        return call.index, tool_result_block(call, run), any(item["kind"] == "compact_context" and item["status"] == "applied" for item in run.effects)
