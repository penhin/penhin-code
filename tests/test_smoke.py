import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import (
    test_agent,
    test_atomic_io,
    test_commands,
    test_compact,
    test_prompt,
    test_result,
    test_session,
    test_task,
    test_todo,
    test_tool_runtime,
    test_tools,
    test_transcript,
)


def main() -> None:
    test_result.run_all()
    test_atomic_io.run_all()
    test_todo.run_all()
    test_tools.run_all()
    test_commands.run_all()
    test_tool_runtime.run_all()
    test_agent.run_all()
    test_task.run_all()
    test_compact.run_all()
    test_prompt.run_all()
    test_transcript.run_all()
    test_session.run_all()
    print("ok")


if __name__ == "__main__":
    main()
