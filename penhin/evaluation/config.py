from __future__ import annotations

import os
import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from penhin.runtime import runtime_manager
from penhin.providers.models import validate_model
from penhin.auth import auth_resolver
from penhin.auth.storage import CredentialStoreUnavailable

from .budget import ModelPrice


def _required_float(name: str) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required for real evaluation")
    try:
        number = float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error
    if number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class EvaluationConfig:
    provider: str
    model: str
    judge_provider: str
    judge_model: str
    primary_price: ModelPrice
    judge_price: ModelPrice
    max_total_tokens: int
    max_usd: float
    max_case_tokens: int
    max_multi_agent_tokens: int
    max_judge_tokens: int
    workers: int

    def public_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider, "model_id_hash": hashlib.sha256(self.model.encode()).hexdigest()[:16],
            "judge_provider": self.judge_provider, "judge_model_id_hash": hashlib.sha256(self.judge_model.encode()).hexdigest()[:16],
            "primary_price": self.primary_price.__dict__, "judge_price": self.judge_price.__dict__,
            "max_total_tokens": self.max_total_tokens, "max_usd": self.max_usd,
            "max_case_tokens": self.max_case_tokens, "max_multi_agent_tokens": self.max_multi_agent_tokens,
            "max_judge_tokens": self.max_judge_tokens, "workers": self.workers,
        }


def load_evaluation_config() -> EvaluationConfig:
    runtime_manager.load_environment()
    provider = runtime_manager.configured_provider()
    model = os.getenv("MODEL_ID", "").strip()
    judge_provider = os.getenv("PENHIN_EVAL_JUDGE_PROVIDER", "gemini").strip().lower()
    judge_model = os.getenv("PENHIN_EVAL_JUDGE_MODEL", "").strip()
    errors = []
    for selected_provider, selected_model, label in ((provider, model, "primary"), (judge_provider, judge_model, "judge")):
        if selected_provider not in {"anthropic", "openai", "openai-codex", "gemini"}:
            errors.append(f"unsupported {label} provider: {selected_provider}")
        else:
            try:
                if auth_resolver().resolve(selected_provider) is None:
                    errors.append(f"credentials are required for {label} provider {selected_provider}")
            except CredentialStoreUnavailable as error:
                errors.append(str(error))
        if not selected_model:
            errors.append(f"{'MODEL_ID' if label == 'primary' else 'PENHIN_EVAL_JUDGE_MODEL'} is required")
    if judge_provider != "gemini":
        errors.append("baseline-v1 requires an independent Gemini judge")
    if judge_provider == provider:
        errors.append("judge provider must differ from the tested provider")
    for selected_provider, selected_model, label in ((provider, model, "primary"), (judge_provider, judge_model, "judge")):
        if selected_model:
            try:
                validate_model(selected_provider, selected_model)
            except ValueError as error:
                errors.append(f"invalid {label} model: {error}")
    if shutil.which("git") is None:
        errors.append("git is required")
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        errors.append("git is not executable")
    minimum_free = _positive_int("PENHIN_EVAL_MIN_FREE_BYTES", 512 * 1024 * 1024)
    if shutil.disk_usage(Path.cwd()).free < minimum_free:
        errors.append(f"at least {minimum_free} bytes of free disk space are required")
    if errors:
        raise ValueError("; ".join(errors))
    return EvaluationConfig(
        provider=provider, model=model, judge_provider=judge_provider, judge_model=judge_model,
        primary_price=ModelPrice(_required_float("PENHIN_EVAL_PRIMARY_INPUT_USD_PER_MTOK"), _required_float("PENHIN_EVAL_PRIMARY_OUTPUT_USD_PER_MTOK")),
        judge_price=ModelPrice(_required_float("PENHIN_EVAL_JUDGE_INPUT_USD_PER_MTOK"), _required_float("PENHIN_EVAL_JUDGE_OUTPUT_USD_PER_MTOK")),
        max_total_tokens=_positive_int("PENHIN_EVAL_MAX_TOTAL_TOKENS", 6_000_000),
        max_usd=_required_float("PENHIN_EVAL_MAX_USD") if os.getenv("PENHIN_EVAL_MAX_USD") else 30.0,
        max_case_tokens=_positive_int("PENHIN_EVAL_MAX_CASE_TOKENS", 120_000),
        max_multi_agent_tokens=_positive_int("PENHIN_EVAL_MAX_MULTI_AGENT_TOKENS", 500_000),
        max_judge_tokens=_positive_int("PENHIN_EVAL_MAX_JUDGE_TOKENS", 8_000),
        workers=_positive_int("PENHIN_EVAL_WORKERS", 3),
    )


def offline_preflight() -> list[str]:
    errors = []
    if shutil.which("git") is None:
        errors.append("git is required")
    if not Path.cwd().is_dir():
        errors.append("current working directory is unavailable")
    return errors
