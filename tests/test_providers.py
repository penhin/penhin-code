import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from types import SimpleNamespace

from penhin.providers.anthropic import normalize_content_block, normalize_response


class FakeThinkingBlock:
    def model_dump(self, mode: str = "json", exclude_none: bool = True):
        return {
            "type": "thinking",
            "thinking": "reasoning summary",
            "signature": "sig",
        }


def test_anthropic_normalize_preserves_thinking_block_fields() -> None:
    assert normalize_content_block(FakeThinkingBlock()) == {
        "type": "thinking",
        "thinking": "reasoning summary",
        "signature": "sig",
    }


def test_anthropic_context_usage_includes_cache_tokens() -> None:
    response = SimpleNamespace(
        content=[],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=4,
            cache_read_input_tokens=20,
            cache_creation_input_tokens=5,
        ),
    )

    assert normalize_response(response).usage.context_tokens == 39
