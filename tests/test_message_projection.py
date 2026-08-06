import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from penhin.agent.projection import mark_message_snipped, messages_for_api

from tests.helpers import ToolUseBlock


def test_messages_for_api_filters_snipped_messages() -> None:
    snipped = {"role": "user", "content": "old"}
    mark_message_snipped(snipped)
    messages = [
        snipped,
        {"role": "user", "content": "current"},
    ]

    assert messages_for_api(messages) == [{"role": "user", "content": "current"}]
    assert snipped["content"] == "old"


def test_messages_for_api_is_stable_and_strips_internal_metadata() -> None:
    block = {
        "type": "tool_result",
        "tool_use_id": "tool-1",
        "content": "x" * 200,
        "_meta": {"id": "internal"},
    }
    messages = [
        {"role": "assistant", "content": [ToolUseBlock("tool-1", "search")]},
        {"role": "user", "content": [block]},
    ]

    projected = messages_for_api(messages)

    assert block["_meta"] == {"id": "internal"}
    assert projected == messages_for_api(messages)
    assert projected[1]["content"][0] == {
        "type": "tool_result", "tool_use_id": "tool-1", "content": "x" * 200,
    }
