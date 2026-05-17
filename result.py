from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


MAX_RESULT_CHARS = 50000


@dataclass(init=False)
class Result:
    ok: bool
    message: str
    data: Any
    error: str
    meta: dict

    def __init__(
        self,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        *,
        ok: bool | None = None,
        message: str | None = None,
        data: Any = None,
        error: str | None = None,
        meta: dict | None = None,
    ):
        self.ok = (exit_code == 0) if ok is None else ok
        self.message = stdout if message is None else message
        self.data = data
        self.error = stderr if error is None else error
        self.meta = {} if meta is None else meta

    @classmethod
    def success(cls, message: str = "", data: Any = None, **meta) -> Result:
        return cls(ok=True, message=message, data=data, meta=meta)

    @classmethod
    def failure(cls, error: str, code: str = None, data: Any = None, **meta) -> Result:
        result_meta = dict(meta)
        if code is not None:
            result_meta["code"] = code
        return cls(ok=False, error=error, data=data, meta=result_meta)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    @property
    def stdout(self) -> str:
        return self.message

    @property
    def stderr(self) -> str:
        return self.error

    def _truncate(self, text: str) -> str:
        if len(text) <= MAX_RESULT_CHARS:
            return text
        return text[:MAX_RESULT_CHARS] + f"\n... truncated {len(text) - MAX_RESULT_CHARS} chars"

    def _truncate_value(self, value):
        if isinstance(value, str):
            return self._truncate(value)
        if isinstance(value, list):
            return [self._truncate_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._truncate_value(item) for key, item in value.items()}
        return value

    def _to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "message": self._truncate(self.message),
            "data": self._truncate_value(self.data),
            "error": self._truncate(self.error),
            "meta": self._truncate_value(self.meta),
            "exit_code": self.exit_code,
            "stdout": self._truncate(self.stdout),
            "stderr": self._truncate(self.stderr),
        }

    def to_json(self) -> str:
        return json.dumps(self._to_dict(), ensure_ascii=False, indent=2)
