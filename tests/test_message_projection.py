import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from message_projection import mark_message_snipped, messages_for_api

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


def test_messages_for_api_reads_legacy_collapsed_block_metadata() -> None:
    block = {
        "type": "tool_result",
        "tool_use_id": "tool-1",
        "content": "x" * 200,
        "_meta": {
            "id": "block-1",
            "collapse": {
                "id": "block-1",
                "label": "tool_result read",
                "reason": "micro_compact",
                "original_chars": 200,
            },
        },
    }
    messages = [{"role": "user", "content": [block]}]

    projected = messages_for_api(messages)

    assert block["content"] == "x" * 200
    assert "_meta" in block
    assert "_meta" not in projected[0]["content"][0]
    assert projected[0]["content"][0]["content"].startswith("[collapsed tool_result read;")


def test_messages_for_api_dynamic_collapse_is_read_only() -> None:
    block = {
        "type": "tool_result",
        "tool_use_id": "tool-1",
        "content": "x" * 200,
    }
    messages = [
        {"role": "assistant", "content": [ToolUseBlock("tool-1", "search")]},
        {"role": "user", "content": [block]},
    ]

    projected = messages_for_api(messages, collapse_keep_recent=0)

    assert block == {
        "type": "tool_result",
        "tool_use_id": "tool-1",
        "content": "x" * 200,
    }
    assert projected[1]["content"][0]["content"].startswith("[collapsed tool_result search;")
