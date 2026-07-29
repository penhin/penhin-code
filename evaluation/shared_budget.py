from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .budget import BudgetExceeded, ModelPrice


def estimate_tokens(value: object) -> int:
    # Deliberately conservative: JSON/token ratios vary by language and provider,
    # and API wrappers add framing tokens that are not visible here.
    return max(1, len(json.dumps(value, ensure_ascii=False, default=str)) // 3 + 1024)


class SharedBudget:
    def __init__(self, path: Path, max_tokens: int, max_usd: float):
        self.path = path
        self.max_tokens = max_tokens
        self.max_usd = max_usd
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json.dumps(self._empty()), encoding="utf-8")

    def _empty(self) -> dict[str, Any]:
        return {"max_tokens": self.max_tokens, "max_usd": self.max_usd, "used_input_tokens": 0, "used_output_tokens": 0, "used_usd": 0.0, "reservations": {}, "case_tokens": {}, "judge_tokens": {}}

    @contextmanager
    def _locked(self) -> Iterator[tuple[Any, dict[str, Any]]]:
        with self.path.open("r+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                try:
                    state = json.load(stream)
                except json.JSONDecodeError:
                    state = self._empty()
                yield stream, state
                stream.seek(0)
                json.dump(state, stream, ensure_ascii=False, sort_keys=True)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def reserve(self, estimated_input: int, max_output: int, price: ModelPrice, role: str, case_id: str = "", role_limit: int | None = None) -> str:
        reservation_id = str(uuid4())
        with self._locked() as (_stream, state):
            reserved_tokens = sum(item["input_tokens"] + item["output_tokens"] for item in state["reservations"].values())
            reserved_usd = sum(item["usd"] for item in state["reservations"].values())
            projected_tokens = state["used_input_tokens"] + state["used_output_tokens"] + reserved_tokens + estimated_input + max_output
            projected_usd = state["used_usd"] + reserved_usd + price.cost(estimated_input, max_output)
            if projected_tokens > state["max_tokens"]:
                raise BudgetExceeded(f"shared token budget would be exceeded: {projected_tokens}>{state['max_tokens']}")
            if projected_usd > state["max_usd"]:
                raise BudgetExceeded(f"shared USD budget would be exceeded: {projected_usd:.6f}>{state['max_usd']:.6f}")
            bucket_name = "judge_tokens" if role == "judge" else "case_tokens"
            bucket = state.setdefault(bucket_name, {})
            role_reserved = sum(
                item["input_tokens"] + item["output_tokens"] for item in state["reservations"].values()
                if item.get("role") == role and item.get("case_id", "") == case_id
            )
            projected_role_tokens = int(bucket.get(case_id, 0)) + role_reserved + estimated_input + max_output
            if role_limit is not None and projected_role_tokens > role_limit:
                raise BudgetExceeded(f"{role} token budget would be exceeded for {case_id}: {projected_role_tokens}>{role_limit}")
            state["reservations"][reservation_id] = {
                "pid": os.getpid(), "role": role, "case_id": case_id, "input_tokens": estimated_input,
                "output_tokens": max_output, "usd": price.cost(estimated_input, max_output),
            }
        return reservation_id

    def settle(self, reservation_id: str, input_tokens: int, output_tokens: int, price: ModelPrice) -> None:
        with self._locked() as (_stream, state):
            reservation = state["reservations"].pop(reservation_id, None) or {}
            state["used_input_tokens"] += input_tokens
            state["used_output_tokens"] += output_tokens
            state["used_usd"] += price.cost(input_tokens, output_tokens)
            role = reservation.get("role", "primary")
            case_id = reservation.get("case_id", "")
            bucket = state.setdefault("judge_tokens" if role == "judge" else "case_tokens", {})
            bucket[case_id] = int(bucket.get(case_id, 0)) + input_tokens + output_tokens

    def release(self, reservation_id: str) -> None:
        with self._locked() as (_stream, state):
            state["reservations"].pop(reservation_id, None)

    def snapshot(self) -> dict[str, Any]:
        with self._locked() as (_stream, state):
            return dict(state)

    def release_stale(self) -> int:
        removed = 0
        with self._locked() as (_stream, state):
            for reservation_id, item in list(state["reservations"].items()):
                try:
                    os.kill(int(item["pid"]), 0)
                except (ProcessLookupError, ValueError):
                    del state["reservations"][reservation_id]
                    removed += 1
                except PermissionError:
                    continue
        return removed

    def update_limits(self, max_tokens: int, max_usd: float) -> None:
        with self._locked() as (_stream, state):
            if max_tokens < int(state["max_tokens"]) or max_usd < float(state["max_usd"]):
                raise ValueError("resume budget limits cannot be reduced")
            state["max_tokens"] = max_tokens
            state["max_usd"] = max_usd
            self.max_tokens = max_tokens
            self.max_usd = max_usd


def budget_from_env() -> SharedBudget | None:
    path = os.getenv("PENHIN_EVAL_BUDGET_FILE")
    if not path:
        return None
    return SharedBudget(Path(path), int(os.getenv("PENHIN_EVAL_MAX_TOTAL_TOKENS", "6000000")), float(os.getenv("PENHIN_EVAL_MAX_USD", "30")))


def price_from_env(role: str = "primary") -> ModelPrice:
    prefix = "PENHIN_EVAL_JUDGE" if role == "judge" else "PENHIN_EVAL_PRIMARY"
    return ModelPrice(float(os.environ[f"{prefix}_INPUT_USD_PER_MTOK"]), float(os.environ[f"{prefix}_OUTPUT_USD_PER_MTOK"]))
