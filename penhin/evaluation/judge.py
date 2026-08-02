from __future__ import annotations

import json
import re
from typing import Any

from penhin.providers.gemini import GeminiProvider
from penhin.auth import ApiKeyCredential
from penhin.auth.secrets import safe_value
from penhin.runtime import runtime_manager
from penhin.providers.protocols import LLMRequest

from .budget import BudgetExceeded
from .models import EvaluationCase, JudgeScore
from .observer import emit
from .shared_budget import budget_from_env, estimate_tokens, price_from_env


JUDGE_SYSTEM = """You are an independent evaluator. Score only the supplied anonymous evidence. Return one JSON object with integer fields correctness, relevance, evidence, maintainability from 1 to 5, and a short rationale string. Do not infer the tested model or provider."""


def _candidate(text: str) -> str:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    return fenced.group(1) if fenced else stripped


def parse_judge_score(text: str) -> JudgeScore:
    try:
        data = json.loads(_candidate(text))
    except json.JSONDecodeError as error:
        raise ValueError(f"judge returned invalid JSON: {error.msg}") from error
    if not isinstance(data, dict) or set(data) != {"correctness", "relevance", "evidence", "maintainability", "rationale"}:
        raise ValueError("judge response has invalid fields")
    for name in ("correctness", "relevance", "evidence", "maintainability"):
        if not isinstance(data[name], int) or isinstance(data[name], bool) or not 1 <= data[name] <= 5:
            raise ValueError(f"judge {name} must be an integer from 1 to 5")
    if not isinstance(data["rationale"], str) or not data["rationale"].strip():
        raise ValueError("judge rationale must be a non-empty string")
    return JudgeScore(**data)


def judge_payload(case: EvaluationCase, final_answer: str, diff_summary: str, checks: list[dict[str, Any]]) -> str:
    return json.dumps(safe_value({
        "task": case.prompt, "rubric": case.rubric,
        "final_answer": final_answer[:12000], "diff_summary": diff_summary[:8000], "deterministic_checks": checks,
    }), ensure_ascii=False, sort_keys=True)


def run_judge(case: EvaluationCase, final_answer: str, diff_summary: str, checks: list[dict[str, Any]], budget_key: str = "") -> JudgeScore:
    import os
    model = os.environ["PENHIN_EVAL_JUDGE_MODEL"]
    prompt = judge_payload(case, final_answer, diff_summary, checks)
    resolved = runtime_manager.resolve_auth("gemini")
    if resolved is None or not isinstance(resolved.credential, ApiKeyCredential):
        raise ValueError("Gemini judge credentials are not configured")
    provider = GeminiProvider(api_key=resolved.credential.key)
    budget = budget_from_env()
    price = price_from_env("judge")
    last_error: Exception | None = None
    for attempt in range(2):
        reservation = None
        try:
            if budget:
                reservation = budget.reserve(
                    estimate_tokens(prompt), 1200, price, "judge", budget_key or case.id,
                    int(os.getenv("PENHIN_EVAL_MAX_JUDGE_TOKENS", "8000")),
                )
            emit("judge_call_started", attempt=attempt + 1)
            response = provider.create_message(LLMRequest(model=model, system=JUDGE_SYSTEM, messages=[{"role": "user", "content": prompt}], max_tokens=1200))
            if budget and reservation:
                budget.settle(reservation, response.usage.input_tokens, response.usage.output_tokens, price)
                reservation = None
            emit("judge_call_usage", attempt=attempt + 1, usage=response.usage)
            text = "\n".join(block.get("text", "") for block in response.content if block.get("type") == "text")
            score = parse_judge_score(text)
            emit("judge_call_completed", attempt=attempt + 1, scores=score)
            return score
        except BudgetExceeded:
            if budget and reservation:
                budget.release(reservation)
            raise
        except Exception as error:
            if budget and reservation:
                budget.release(reservation)
            last_error = error
            emit("judge_call_failed", attempt=attempt + 1, error_type=type(error).__name__)
    raise ValueError(f"judge failed after one retry: {last_error}")
