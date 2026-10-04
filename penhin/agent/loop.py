import json
import logging
from collections.abc import Callable
from typing import Any

from penhin.cli import ui

from penhin.agent.state import AgentDeps, AgentState, is_terminal, step_agent
from penhin.approval_rules import suggest_bash_prefix
from penhin.runtime.retry import CircuitBreakerOpen
from penhin.agent.compaction import CompactionError
from penhin.agent.context import RunContext
from penhin.agent.messages import ToolResults, build_tool_execution_context, execute_tool_blocks
from penhin.agent.projection import messages_for_api
from penhin.agent.prompts import build_main_system, ensure_project_instructions_message
from penhin.runtime import runtime_manager
from penhin.runtime.manager import log_usage
from penhin.tools.execution import ApprovalFlow, PermissionPolicy, approval_key, run_tool
from penhin.tools.execution.invocation import collect_tool_calls
from penhin.tools.catalog import ToolCatalog
from penhin.tools.registry import DEFAULT_TOOL_CATALOG, MODEL_TOOL_CATALOG


API_UNAVAILABLE_MESSAGE = "API is temporarily unavailable because the circuit breaker is open. Please try again later."

logger = logging.getLogger("penhin.agent")


def format_tool_input(tool_input: dict[str, Any]) -> str:
    if not tool_input:
        return "{}"
    return json.dumps(tool_input, ensure_ascii=False, indent=2)


def run_with_one_time_approval(
    tool_name: str,
    tool_input: dict[str, Any],
    policy: PermissionPolicy,
    approval: ApprovalFlow,
    catalog: ToolCatalog = DEFAULT_TOOL_CATALOG, context: RunContext | None = None,
):
    one_time_approval = approval.copy()
    one_time_approval.approve(tool_name, tool_input, catalog)
    return run_tool(
        tool_name,
        tool_input,
        policy,
        one_time_approval, context=context, catalog=catalog,
    )


def run_with_one_time_rejection(
    tool_name: str,
    tool_input: dict[str, Any],
    policy: PermissionPolicy,
    approval: ApprovalFlow,
    catalog: ToolCatalog = DEFAULT_TOOL_CATALOG, context: RunContext | None = None,
):
    one_time_rejection = approval.copy()
    one_time_rejection.reject(tool_name, tool_input, catalog)
    return run_tool(
        tool_name,
        tool_input,
        policy,
        one_time_rejection, context=context, catalog=catalog,
    )


def resolve_approval(
    tool_name: str,
    tool_input: dict[str, Any],
    policy: PermissionPolicy,
    approval: ApprovalFlow,
    catalog: ToolCatalog = DEFAULT_TOOL_CATALOG, context: RunContext | None = None,
):
    if policy.mode == "auto-review" and tool_name == "bash":
        from penhin.evaluation.observer import emit
        from penhin.tools.auto_review import ALLOW, review_bash
        decision = review_bash(tool_input.get("command"))
        emit(
            "auto_review_decision",
            tool_name=tool_name,
            decision=decision.decision,
            evidence=decision.evidence,
        )
        if decision.decision == ALLOW:
            return run_with_one_time_approval(tool_name, tool_input, policy, approval, catalog, context)
    logger.info(f"[approval] tool: {tool_name}")
    logger.info(f"[approval] key: {approval_key(tool_name, tool_input, catalog)}")
    logger.info(format_tool_input(tool_input))
    suggested_prefix = None
    if tool_name == "bash":
        command = str(tool_input.get("command", ""))
        suggested_prefix = suggest_bash_prefix(command)
        from penhin.auth.secrets import redact_text
        print("[approval] bash")
        print(redact_text(command))
        print()
        print("1. allow once")
        print("2. allow exact command this session")
        if suggested_prefix:
            print(f"3. allow command prefix this session: {suggested_prefix}")
        else:
            print("3. allow command prefix this session: unavailable")
        print("4. reject")

    try:
        reply = input("[approval] choose 1-4 [4] ").strip().lower()
    except EOFError:
        logger.info("[approval] no input available; rejecting")
        reply = ""
    if reply in {"1", "y"}:
        return run_with_one_time_approval(tool_name, tool_input, policy, approval, catalog, context)

    if reply in {"2", "ys"}:
        approval.approve(tool_name, tool_input, catalog)
        return run_tool(
            tool_name,
            tool_input,
            policy,
            approval, context=context, catalog=catalog,
        )

    if reply in {"3", "yp"} and suggested_prefix:
        approval.approve_rule(tool_name, suggested_prefix)
        return run_tool(
            tool_name,
            tool_input,
            policy,
            approval, context=context, catalog=catalog,
        )

    return run_with_one_time_rejection(tool_name, tool_input, policy, approval, catalog, context)


def compact_context_for_llm(context: RunContext, runtime) -> None:
    force_compact_hint = context.consume_force_compact_hint()
    if force_compact_hint is not None:
        try:
            context.force_auto_compact(hint=force_compact_hint)
        except CompactionError as error:
            context.request_force_compact(force_compact_hint)
            logger.warning(f"[compact] {error}")
        return

    context.auto_compact_if_needed(runtime.context_window, runtime.compaction_reserve_tokens)


def call_llm(context: RunContext, runtime, catalog: ToolCatalog = MODEL_TOOL_CATALOG):
    ensure_project_instructions_message(context.messages)
    streamed = False
    stream = None

    def on_stream_text(text: str) -> None:
        nonlocal streamed, stream
        if context.planning.active:
            return  # Planning responses are rendered as host-owned questions or proposals.
        if not streamed:
            stream = ui.start_assistant_message()
        streamed = True
        stream.write(text)

    response = None
    try:
        response = runtime.call_with_retry(
            system=build_main_system(catalog, context.planning),
            messages=messages_for_api(context.messages),
            tools=catalog.schemas("parent"),
            max_tokens=runtime.max_tokens,
            stream_callback=on_stream_text,
        )
        return response
    finally:
        if streamed:
            usage = getattr(response, "usage", None)
            tokens = getattr(usage, "total_tokens", None)
            if tokens is None and usage is not None:
                tokens = sum(getattr(usage, field, 0) or 0 for field in ("input_tokens", "output_tokens")) or None
            ui.finish_stream(stream, tokens=tokens)

def record_llm_response(context: RunContext, response) -> None:
    context.add_assistant_message(response.content, response.usage)
    log_usage("main", response)


def should_continue_with_tools(response) -> bool:
    return response.stop_reason == "tool_use"


def execute_tool_uses(
    context: RunContext,
    response,
    catalog: ToolCatalog = MODEL_TOOL_CATALOG,
) -> tuple[ToolResults, bool]:
    calls = collect_tool_calls(response.content)
    cards = [ui.start_tool_call(call.tool_name, call.tool_input) for call in calls]
    try:
        results = execute_tool_blocks(
            response.content,
            build_tool_execution_context(
                context.policy,
                context.approval,
                approval_resolver=lambda name, tool_input, policy, approval: resolve_approval(
                    name, tool_input, policy, approval, catalog, context,
                ),
                context=context,
                catalog=catalog,
            ),
        )
    except Exception:
        for card in cards:
            ui.finish_tool_call(card, ok=False)
        raise
    tool_results, manual_compact = results
    for card, block in zip(cards, tool_results):
        try:
            ok = bool(json.loads(str(block.get("content", "{}"))).get("ok"))
        except (TypeError, ValueError, json.JSONDecodeError):
            ok = False
        ui.finish_tool_call(card, ok=ok)
    return tool_results, manual_compact


def record_tool_results(context: RunContext, tool_results: ToolResults, manual_compact: bool) -> None:
    context.add_tool_results(tool_results)


def handle_circuit_open(context: RunContext, error: CircuitBreakerOpen) -> None:
    logger.warning(f"[circuit] {API_UNAVAILABLE_MESSAGE} ({error})")
    context.add_assistant_message([
        {"type": "text", "text": API_UNAVAILABLE_MESSAGE}
    ])


def build_agent_deps(
    runtime,
    catalog: ToolCatalog = MODEL_TOOL_CATALOG,
    catalog_provider: Callable[[], ToolCatalog] | None = None,
) -> AgentDeps:
    current_catalog = catalog_provider or (lambda: catalog)
    return AgentDeps(
        compact_context=lambda context: compact_context_for_llm(context, runtime),
        call_llm=lambda context: call_llm(context, runtime, current_catalog()),
        record_llm_response=record_llm_response,
        should_continue_with_tools=should_continue_with_tools,
        execute_tool_uses=lambda context, response: execute_tool_uses(context, response, current_catalog()),
        record_tool_results=record_tool_results,
        handle_circuit_open=handle_circuit_open,
    )


def run_agent_state_machine(
    context: RunContext,
    deps: AgentDeps,
    initial_state: AgentState | None = None,
) -> AgentState:
    state = initial_state or AgentState()
    while not is_terminal(state):
        state = step_agent(context, state, deps)
    return state


def agent_loop(context: RunContext, catalog: ToolCatalog = MODEL_TOOL_CATALOG) -> AgentState:
    runtime = runtime_manager.current()
    plugin_runtime = context.plugin_runtime
    catalog_provider = plugin_runtime.catalog if plugin_runtime is not None else None
    effective_catalog = catalog_provider() if catalog_provider is not None else catalog
    from penhin.runtime.envelope import resolve_envelope, using_envelope
    with using_envelope(resolve_envelope(context, runtime, effective_catalog)):
        return run_agent_state_machine(context, build_agent_deps(runtime, catalog, catalog_provider))


def run_once(context: RunContext) -> AgentState:
    return agent_loop(context)


def run_once_prompt(query: str, session_manager) -> AgentState:
    from penhin.infrastructure.config import get_permission_mode
    from penhin.tools.execution import runtime_permission_setup

    policy, approval = runtime_permission_setup(get_permission_mode())
    context = RunContext(
        messages=session_manager.build_context(),
        policy=policy,
        approval=approval,
        session_path=session_manager.path,
        session_manager=session_manager,
    )
    context.add_user_message(query)
    return agent_loop(context)
