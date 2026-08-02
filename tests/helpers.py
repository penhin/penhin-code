import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from penhin.result import Result
from penhin.tools import TOOL_SPECS


def run_spec_tool(tool_name: str, **kwargs) -> Result:
    handler = TOOL_SPECS[tool_name].handler
    assert handler is not None
    return handler(**kwargs)


class ToolUseBlock:
    def __init__(self, block_id: str, name: str):
        self.type = "tool_use"
        self.id = block_id
        self.name = name
