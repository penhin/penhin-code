import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from penhin.runtime.retry import CircuitBreaker, CircuitBreakerOpen
from penhin.auth import OAuthCredential, ResolvedAuth, auth_resolver, provider_auth_ids
from penhin.auth.storage import CredentialStoreUnavailable, credential_store
from penhin.auth.providers import provider_auth
from penhin.providers.models import validate_model
from penhin.infrastructure.config import get_provider_thinking_level
from penhin.providers.protocols import LLMProvider, LLMRequest, LLMResponse, StreamCallback

from .factory import build_provider
from .models import AuthenticationRequired, RuntimeStatus
from .settings import (
    build_circuit_breaker, build_compact_circuit_breaker, configured_model, configured_provider,
    load_environment, mark_setting_source, setting_source,
)


runtime = None

logger = logging.getLogger("penhin.runtime")


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[35m",
    }
    RESET = "\033[0m"
    
    def format(self, record: logging.LogRecord) -> str:
        from penhin.auth.secrets import redact_text
        color = self.COLORS.get(record.levelno, "")
        message = redact_text(super().format(record))
        return f"{color}{message}{self.RESET}"


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColorFormatter("%(message)s"))
    
    app_logger = logging.getLogger("penhin")
    app_logger.setLevel(logging.INFO)
    app_logger.handlers.clear()
    app_logger.addHandler(handler)


@dataclass
class Runtime:
    provider: LLMProvider
    model: str
    provider_id: str = ""
    thinking_level: str | None = None
    auth_expires_at: int | None = None
    max_tokens: int = 10000
    sub_max_turns: int = 30
    sub_max_tokens: int = 2000
    retry_delays: tuple[int, ...] = (1, 2, 4)
    circuit_breaker: CircuitBreaker | None = None
    compact_circuit_breaker: CircuitBreaker | None = None

    def _call_with_retry(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        breaker: CircuitBreaker | None = None,
        label: str = "messages.create",
        stream_callback: StreamCallback | None = None,
    ) -> LLMResponse:
        if self.provider_id and self.auth_expires_at is not None and self.auth_expires_at <= int(time.time()) + 60:
            resolved = resolve_runtime_auth(self.provider_id)
            if resolved is None:
                raise AuthenticationRequired(f"No credentials for {self.provider_id}. Use /login {self.provider_id} first.")
            self.provider = build_provider(self.provider_id, resolved)
            self.auth_expires_at = getattr(resolved.credential, "expires_at", None)
        retry_errors = self.provider.retry_errors
        delays = self.retry_delays

        if breaker is not None:
            try:
                breaker.before_call()
            except CircuitBreakerOpen as error:
                logger.warning(f"[circuit] {label} skipped: {error}")
                raise

        for attempt in range(len(delays) + 1):
            started = time.perf_counter()
            first_token_ms: float | None = None
            reservation_id = None
            budget = None
            try:
                request = LLMRequest(
                    model=self.model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens or self.max_tokens,
                    thinking_level=self.thinking_level,
                )
                from penhin.evaluation.observer import anonymous_id, emit
                from penhin.evaluation.shared_budget import budget_from_env, estimate_tokens, price_from_env
                budget = budget_from_env()
                if budget is not None:
                    reservation_id = budget.reserve(
                        estimate_tokens({"system": system, "messages": messages, "tools": tools}),
                        request.max_tokens,
                        price_from_env("primary"),
                        "primary",
                        os.getenv("PENHIN_EVAL_BUDGET_CASE_KEY", os.getenv("PENHIN_EVAL_CASE_ID", "")),
                        int(os.getenv("PENHIN_EVAL_CURRENT_CASE_MAX_TOKENS", "0")) or None,
                    )
                request_digest = hashlib.sha256(json.dumps(
                    {"system": system, "messages": messages, "tools": tools},
                    ensure_ascii=False, sort_keys=True, default=str,
                ).encode("utf-8")).hexdigest()[:16]
                emit("llm_call_started", label=label, provider=configured_provider(), model_id_hash=anonymous_id(self.model), attempt=attempt + 1, max_tokens=request.max_tokens, request_digest=request_digest)
                if stream_callback is None:
                    response = self.provider.create_message(request)
                else:
                    def observed_stream(text: str) -> None:
                        nonlocal first_token_ms
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - started) * 1000
                        stream_callback(text)
                    response = self.provider.stream_message(request, observed_stream)
                if budget is not None and reservation_id is not None:
                    budget.settle(reservation_id, response.usage.input_tokens, response.usage.output_tokens, price_from_env("primary"))
                    reservation_id = None
                emit(
                    "llm_call_completed", label=label, provider=configured_provider(), model_id_hash=anonymous_id(self.model),
                    attempt=attempt + 1, duration_ms=(time.perf_counter() - started) * 1000,
                    first_token_ms=first_token_ms, stop_reason=getattr(response, "stop_reason", ""),
                    usage=getattr(response, "usage", None),
                )
                if breaker is not None:
                    breaker.record_success()
                return response
            except retry_errors as error:
                if budget is not None and reservation_id is not None:
                    budget.release(reservation_id)
                from penhin.evaluation.observer import emit
                emit("llm_call_failed", label=label, attempt=attempt + 1, duration_ms=(time.perf_counter() - started) * 1000, error_type=type(error).__name__, retryable=True)
                if attempt == len(delays):
                    if breaker is not None:
                        breaker.record_failure()
                    raise

                delay = delays[attempt]
                logger.warning(
                    f"[retry] {label} failed ({error.__class__.__name__}), "
                    f"retrying in {delay}s...\n"
                    f"[retry] Reconnecting...({attempt + 1}/{len(delays)})"
                )
                emit("llm_retry", label=label, attempt=attempt + 1, delay_seconds=delay, error_type=type(error).__name__)
                time.sleep(delay)
            except Exception as error:
                if budget is not None and reservation_id is not None:
                    budget.release(reservation_id)
                from penhin.evaluation.observer import emit
                emit("llm_call_failed", label=label, attempt=attempt + 1, duration_ms=(time.perf_counter() - started) * 1000, error_type=type(error).__name__, retryable=False)
                raise

    def call_with_retry(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        stream_callback: StreamCallback | None = None,
    ) -> LLMResponse:
        return self._call_with_retry(
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            breaker=self.circuit_breaker,
            label="messages.create",
            stream_callback=stream_callback,
        )

    def call_compact_once(
        self,
        system: str,
        user_content: str,
        max_tokens: int | None = None,
    ) -> str:
        response = self._call_with_retry(
            system=system,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=max_tokens or self.max_tokens,
            breaker=self.compact_circuit_breaker,
            label="compact.messages.create",
        )

        log_usage("compact", response)

        return "\n".join(
            block.get("text", "")
            for block in response.content
            if block.get("type") == "text"
        )


def log_usage(label: str, response) -> None:
    usage = response.usage
    logger.info(
        f"[usage:{label}] "
        f"input={usage.input_tokens} "
        f"output={usage.output_tokens} "
        f"total={usage.input_tokens + usage.output_tokens} "
    )


def init_runtime(required: bool = True) -> None:
    setup_logging()

    global runtime
    load_environment()

    provider = configured_provider()
    if provider not in provider_auth_ids():
        message = f"Unsupported LLM_PROVIDER={provider!r}; choose anthropic, openai, openai-codex, gemini, or deepseek"
        if required:
            logger.error(message)
            raise SystemExit(1)
        runtime = None
        return
    model = configured_model(provider)
    try:
        resolved = resolve_runtime_auth(provider)
    except CredentialStoreUnavailable as error:
        if required:
            logger.error(str(error))
            raise SystemExit(1)
        runtime = None
        return
    if resolved is None or not model:
        runtime = None
        if required:
            missing = "credentials" if resolved is None else "a selected model"
            logger.error(f"Missing {missing}. Start interactive Penhin and use /login.")
            raise SystemExit(1)
        return
    try:
        validate_model(provider, model)
    except ValueError as error:
        logger.error(str(error))
        runtime = None
        if required:
            raise SystemExit(1)
        return
    
    runtime = Runtime(
        provider=build_provider(provider, resolved),
        model=model,
        provider_id=provider,
        thinking_level=get_provider_thinking_level(provider) or None,
        auth_expires_at=getattr(resolved.credential, "expires_at", None),
        circuit_breaker=build_circuit_breaker(),
        compact_circuit_breaker=build_compact_circuit_breaker(),
    )


def get_runtime() -> Runtime:
    if runtime is None:
        raise AuthenticationRequired("No authenticated runtime is available. Use /login first.")
    return runtime


def runtime_available() -> bool:
    return runtime is not None


def set_runtime_model(model: str) -> None:
    validate_model(configured_provider(), model)
    if runtime is not None:
        runtime.model = model


def set_runtime_thinking_level(level: str | None) -> None:
    if runtime is not None:
        runtime.thinking_level = level


def set_runtime_provider(provider: str, model: str) -> None:
    global runtime
    if provider not in provider_auth_ids():
        raise ValueError(f"Unsupported provider: {provider}")
    validate_model(provider, model)
    resolved = resolve_runtime_auth(provider)
    if resolved is None:
        raise AuthenticationRequired(f"No credentials for {provider}. Use /login {provider} first.")
    runtime = Runtime(
        provider=build_provider(provider, resolved), model=model,
        provider_id=provider,
        thinking_level=get_provider_thinking_level(provider) or None,
        auth_expires_at=getattr(resolved.credential, "expires_at", None),
        circuit_breaker=build_circuit_breaker(),
        compact_circuit_breaker=build_compact_circuit_breaker(),
    )


def _refresh_if_needed(provider: str, credential):
    if not isinstance(credential, OAuthCredential) or credential.expires_at > int(time.time()) + 60:
        return credential
    from penhin.evaluation.observer import emit
    started = time.perf_counter()
    emit("auth_refresh_started", provider=provider, auth_type="oauth")
    store = credential_store()
    try:
        updated = store.modify(
            provider,
            lambda current: provider_auth(provider).refresh(current)
            if isinstance(current, OAuthCredential) and current.expires_at <= int(time.time()) + 60
            else None,
        )
    except Exception as error:
        emit("auth_refresh_failed", provider=provider, error_type=type(error).__name__, duration_ms=(time.perf_counter() - started) * 1000)
        raise
    if not isinstance(updated, OAuthCredential):
        raise AuthenticationRequired(f"OAuth credential for {provider} could not be refreshed")
    emit("auth_refresh_completed", provider=provider, duration_ms=(time.perf_counter() - started) * 1000)
    return updated


def resolve_runtime_auth(provider: str) -> ResolvedAuth | None:
    resolved = auth_resolver().resolve(provider)
    if resolved is None:
        return None
    credential = _refresh_if_needed(provider, resolved.credential)
    return ResolvedAuth(provider, credential, resolved.source, resolved.backend)


class RuntimeManager:
    """Stable lifecycle boundary for the process-wide model runtime."""

    def initialize(self, required: bool = True) -> None:
        init_runtime(required)

    def current(self) -> Runtime:
        return get_runtime()

    def available(self) -> bool:
        return runtime_available()

    def set_model(self, model: str) -> None:
        set_runtime_model(model)

    def set_thinking_level(self, level: str | None) -> None:
        set_runtime_thinking_level(level)

    def switch_provider(self, provider: str, model: str) -> None:
        set_runtime_provider(provider, model)

    def resolve_auth(self, provider: str) -> ResolvedAuth | None:
        return resolve_runtime_auth(provider)

    def status(self) -> RuntimeStatus:
        current = runtime
        return RuntimeStatus(
            available=current is not None,
            provider=configured_provider(),
            model=current.model if current is not None else configured_model(configured_provider()),
            auth_expires_at=current.auth_expires_at if current is not None else None,
        )

    @staticmethod
    def configured_provider() -> str:
        return configured_provider()

    @staticmethod
    def setting_source(name: str) -> str:
        return setting_source(name)

    @staticmethod
    def mark_setting_source(name: str, source: str) -> None:
        mark_setting_source(name, source)

    @staticmethod
    def load_environment() -> None:
        load_environment()


runtime_manager = RuntimeManager()


__all__ = ["AuthenticationRequired", "Runtime", "RuntimeManager", "RuntimeStatus", "runtime_manager"]
