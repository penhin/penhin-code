from penhin.agent.context import RunContext
from penhin.agent.loop import call_llm, execute_tool_uses
from penhin.result import Result
from penhin.tools.catalog import ToolCatalog
from penhin.tools.execution import ApprovalFlow, PermissionPolicy, ToolExecutor, run_tool
from penhin.tools.types import ToolCategory, ToolSpec


def echo(value: str) -> Result:
    return Result.success(value)


def test_injected_catalog_limits_execution_to_its_own_specs() -> None:
    catalog = ToolCatalog([
        ToolSpec(
            name="plugin_echo",
            description="Echo a plugin value.",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            category=ToolCategory.readonly,
            handler=echo,
        ),
    ])
    policy = PermissionPolicy(allow={"plugin_echo", "read"})

    result = run_tool("plugin_echo", {"value": "hello"}, policy, catalog=catalog)
    absent_builtin = run_tool("read", {"path": "README.md"}, policy, catalog=catalog)

    assert result.result.ok is True
    assert result.result.message == "hello"
    assert absent_builtin.result.meta["code"] == "unknown_tool"
    assert ToolExecutor(catalog).execute("plugin_echo", {"value": "executor"}, policy).result.message == "executor"


def test_agent_schema_and_tool_calls_share_an_injected_catalog() -> None:
    catalog = ToolCatalog([
        ToolSpec(
            name="plugin_echo",
            description="Echo a plugin value.",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            category=ToolCategory.readonly,
            handler=echo,
        ),
    ])
    context = RunContext(
        messages=[],
        policy=PermissionPolicy(allow={"plugin_echo"}),
        approval=ApprovalFlow.preapproved(set()),
    )
    response = type("Response", (), {"content": [{"type": "tool_use", "id": "echo-1", "name": "plugin_echo", "input": {"value": "ok"}}]})()

    results, _ = execute_tool_uses(context, response, catalog)

    assert '"message": "ok"' in results[0]["content"]

    class Runtime:
        max_tokens = 50

        def call_with_retry(self, **kwargs):
            self.kwargs = kwargs
            return object()

    runtime = Runtime()
    call_llm(context, runtime, catalog)
    assert [tool["name"] for tool in runtime.kwargs["tools"]] == ["plugin_echo"]
