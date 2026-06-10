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
    meta: dict[str, Any]

    def __init__(
        self,
        ok: bool = True,
        message: str = "",
        data: Any = None,
        error: str = "",
        meta: dict[str, Any] | None = None,
    ):
        self.ok = ok
        self.message = message
        self.data = data
        self.error = error
        self.meta = {} if meta is None else meta

    @classmethod
    def success(cls, message: str = "", data: Any = None, **meta: Any) -> Result:
        return cls(ok=True, message=message, data=data, meta=meta)

    @classmethod
    def failure(cls, error: str, code: str = None, data: Any = None, **meta: Any) -> Result:
        result_meta = dict(meta)
        if code is not None:
            result_meta["code"] = code
        return cls(ok=False, error=error, data=data, meta=result_meta)

    def _truncate(self, text: str) -> str:
        if len(text) <= MAX_RESULT_CHARS:
            return text
        return text[:MAX_RESULT_CHARS] + f"\n... truncated {len(text) - MAX_RESULT_CHARS} chars"

    def _truncate_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._truncate(value)
        if isinstance(value, list):
            return [self._truncate_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._truncate_value(item) for key, item in value.items()}
        return value

    def _to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self._truncate(self.message),
            "data": self._truncate_value(self.data),
            "error": self._truncate(self.error),
            "meta": self._truncate_value(self.meta),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "message_chars": len(self.message),
            "error_chars": len(self.error),
            "data_type": type(self.data).__name__ if self.data is not None else "none",
            "meta_keys": sorted(self.meta),
        }

    def to_json(self) -> str:
        return json.dumps(self._to_dict(), ensure_ascii=False, indent=2)
