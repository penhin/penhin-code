import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from penhin.providers.anthropic import normalize_content_block


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
