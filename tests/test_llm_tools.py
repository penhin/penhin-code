import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools
from result import Result
from task import TaskStatusManager
from todo import TODO_FILE
from tools import PARENT_TOOLS, TOOL_HANDLERS

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None


SYSTEM = (
    "You are testing tool use. "
    "When the user asks for a specific tool, call that tool exactly once. "
    "Do not answer with text before calling the tool."
)

TOOL_PROMPTS = {
    "task": "Call the `task` tool with task exactly `Reply with the word done and do not use tools.`.",
    "compact": "Call the `compact` tool with an empty input object.",
    "task_start": "Call the `task_start` tool with subject exactly `llm tool test task`.",
    "task_show": "Call the `task_show` tool with id 1.",
    "task_complete": "Call the `task_complete` tool with note exactly `done`.",
    "task_block": "Call the `task_block` tool with blocked_by [1] and note exactly `waiting`.",
    "task_clear": "Call the `task_clear` tool with an empty input object.",
    "task_list": "Call the `task_list` tool with an empty input object.",
    "task_switch": "Call the `task_switch` tool with id 1.",
    "background_start": (
        "Call the `background_start` tool with task exactly "
        "`Reply with the word done and do not use tools.`."
    ),
    "background_list": "Call the `background_list` tool with an empty input object.",
    "background_show": "Call the `background_show` tool with id 2.",
    "bash": "Call the `bash` tool with command exactly `printf llm_tool_test`.",
    "read": "Call the `read` tool with path exactly `README.md` and limit 1.",
    "write": "Call the `write` tool with path exactly `.llm_tool_test.txt` and content exactly `hello`.",
    "list": "Call the `list` tool with path exactly `.` and limit 1.",
    "edit": (
        "Call the `edit` tool with path exactly `.llm_tool_test_edit.txt`, "
        "old exactly `old`, and new exactly `new`."
    ),
    "search": "Call the `search` tool with query exactly `penhin-code`, path exactly `README.md`, and limit 1.",
    "todo_set": "Call the `todo_set` tool with items exactly [`inspect`, `verify`].",
    "todo_show": "Call the `todo_show` tool with an empty input object.",
    "todo_done": "Call the `todo_done` tool with index 1.",
    "todo_clear": "Call the `todo_clear` tool with an empty input object.",
    "workspace": "Call the `workspace` tool with an empty input object.",
    "load_skill": "Call the `load_skill` tool with name exactly `code-review`.",
}

EXECUTE_HANDLER_TOOLS = {
    "bash",
    "read",
    "write",
    "list",
    "edit",
    "search",
    "todo_set",
    "todo_show",
    "todo_done",
    "todo_clear",
    "workspace",
    "load_skill",
    "task_start",
    "task_show",
    "task_complete",
    "task_block",
    "task_clear",
    "task_list",
    "task_switch",
    "background_list",
    "background_show",
}


def text_blocks(content) -> str:
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        elif getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


def require_real_llm_enabled() -> bool:
    if load_dotenv is not None:
        load_dotenv()

    if os.getenv("RUN_LLM_TOOL_TESTS") != "1":
        print("skipped: set RUN_LLM_TOOL_TESTS=1 to run real LLM tool tests")
        return False

    missing = [name for name in ("ANTHROPIC_API_KEY", "MODEL_ID") if not os.getenv(name)]
    if missing:
        print(f"skipped: missing {', '.join(missing)}")
        return False

    return True


def assert_registered_tool(expected_tool: str) -> None:
    tool_names = {tool["name"] for tool in PARENT_TOOLS}
    if expected_tool not in tool_names:
        raise AssertionError(f"{expected_tool} is not in PARENT_TOOLS")
    if expected_tool != "compact" and expected_tool not in TOOL_HANDLERS:
        raise AssertionError(f"{expected_tool} has no handler")


def tool_test_names() -> list[str]:
    requested = os.getenv("LLM_TOOL_TEST_NAME")
    if requested:
        return [requested]
    requested_many = os.getenv("LLM_TOOL_TEST_NAMES")
    if requested_many:
        return [name.strip() for name in requested_many.split(",") if name.strip()]
    return [tool["name"] for tool in PARENT_TOOLS]


def call_expected_tool(runtime, expected_tool: str):
    from runtime import print_usage

    assert_registered_tool(expected_tool)
    messages = [
        {
            "role": "user",
            "content": TOOL_PROMPTS.get(
                expected_tool,
                f"Call the `{expected_tool}` tool now. Use valid minimal input. Do not call any other tool.",
            ),
        }
    ]
    response = runtime.call_with_retry(
        system=SYSTEM,
        messages=messages,
        tools=PARENT_TOOLS,
        max_tokens=256,
    )
    print_usage("llm-tool-request", response)

    tool_uses = [block for block in response.content if getattr(block, "type", None) == "tool_use"]
    if len(tool_uses) != 1:
        raise AssertionError(f"expected one tool_use, got {len(tool_uses)}: {text_blocks(response.content)}")

    tool_use = tool_uses[0]
    if tool_use.name != expected_tool:
        raise AssertionError(f"expected {expected_tool}, got {tool_use.name}")

    return tool_use, response, messages


def prepare_handler_state() -> tuple[TaskStatusManager, str | None]:
    manager = TaskStatusManager(Path(tempfile.mkdtemp(prefix="penhin-llm-tools-")))
    main_task = manager.start("existing main task")
    background = manager.start_background("existing background task")
    manager.finish_background(background.id, "completed", result="background result")
    assert main_task.id == 1
    assert background.id == 2

    original_todo = TODO_FILE.read_text(encoding="utf-8") if TODO_FILE.exists() else None
    return manager, original_todo


def restore_handler_state(original_todo: str | None) -> None:
    for path in (Path(".llm_tool_test.txt"), Path(".llm_tool_test_edit.txt")):
        path.unlink(missing_ok=True)

    if original_todo is None:
        TODO_FILE.unlink(missing_ok=True)
    else:
        TODO_FILE.write_text(original_todo, encoding="utf-8")


def execute_handler(tool_use) -> Result:
    if tool_use.name == "compact":
        return Result(stdout="Compacted conversation history now")

    if tool_use.name == "edit":
        Path(".llm_tool_test_edit.txt").write_text("old", encoding="utf-8")
    if tool_use.name in {"todo_show", "todo_done"}:
        TOOL_HANDLERS["todo_set"](items=["inspect", "verify"])

    return TOOL_HANDLERS[tool_use.name](**tool_use.input)


def test_llm_calls_requested_tool() -> None:
    if not require_real_llm_enabled():
        return

    from runtime import get_runtime, init_runtime

    init_runtime()
    runtime = get_runtime()
    manager, original_todo = prepare_handler_state()
    original_task_status = tools.task_status

    try:
        tools.task_status = manager
        for expected_tool in tool_test_names():
            print(f"[llm-tool-test] requesting {expected_tool}")
            tool_use, _, _ = call_expected_tool(runtime, expected_tool)
            if tool_use.name in EXECUTE_HANDLER_TOOLS or tool_use.name == "compact":
                handler_result = execute_handler(tool_use)
                if handler_result.exit_code != 0:
                    raise AssertionError(f"{tool_use.name} handler failed: {handler_result.to_json()}")
    finally:
        tools.task_status = original_task_status
        restore_handler_state(original_todo)


def test_llm_calls_requested_tool_and_finishes() -> None:
    if not require_real_llm_enabled():
        return
    if os.getenv("RUN_LLM_TOOL_FINAL_TEST") != "1":
        return

    from runtime import get_runtime, init_runtime, print_usage

    init_runtime()
    runtime = get_runtime()
    expected_tool = os.getenv("LLM_TOOL_TEST_NAME", "workspace")
    tool_use, response, messages = call_expected_tool(runtime, expected_tool)
    handler_result = TOOL_HANDLERS[tool_use.name](**tool_use.input)
    messages.append({"role": "assistant", "content": response.content})
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": handler_result.to_json(),
                }
            ],
        }
    )

    final_response = runtime.call_with_retry(
        system=SYSTEM,
        messages=messages,
        tools=PARENT_TOOLS,
        max_tokens=256,
    )
    print_usage("llm-tool-final", final_response)

    if final_response.stop_reason == "tool_use":
        raise AssertionError("expected final text after tool_result, got another tool_use")
    if not text_blocks(final_response.content).strip():
        raise AssertionError("expected non-empty final text")


def main() -> None:
    test_llm_calls_requested_tool()
    print("ok")


if __name__ == "__main__":
    main()
