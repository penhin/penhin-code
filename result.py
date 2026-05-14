import json

from dataclasses import dataclass

MAX_RESULT_CHARS = 50000

@dataclass
class Result:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""

    def _to_dict(self) -> dict:
        result = {}
        for key, value in self.__dict__.items():
            if key.startswith("_"):
                continue
            result[key] = self._truncate(value) if isinstance(value, str) else value
        return result

    def _truncate(self, text: str) -> str:
        if len(text) <= MAX_RESULT_CHARS:
            return text
        return text[:MAX_RESULT_CHARS] + f"\n... truncated {len(text) - MAX_RESULT_CHARS} chars"

    def to_json(self) -> str:
        return json.dumps(self._to_dict(), ensure_ascii=False, indent=2)