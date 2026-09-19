"""Immutable, inspectable capability boundaries for an Agent run.

The envelope describes the runtime that already exists today.  It deliberately
does not add enforcement: later Sandbox and credential work can consume this
single receipt instead of rediscovering configuration at each boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from penhin.tools.catalog import ToolCatalog
from penhin.tools.execution.approval import PermissionPolicy


_current: ContextVar["RuntimeEnvelope | None"] = ContextVar("penhin_runtime_envelope", default=None)


@dataclass(frozen=True)
class RuntimeBudget:
    max_tokens: int | None = None
    max_turns: int | None = None
    max_tool_calls: int | None = None

    def narrowed(self, requested: "RuntimeBudget") -> "RuntimeBudget":
        def limit(parent: int | None, child: int | None, field: str) -> int | None:
            if parent is not None and (child is None or child > parent):
                raise _widening_error(f"Runtime envelope cannot widen {field}")
            return child if child is not None else parent
        return RuntimeBudget(
            max_tokens=limit(self.max_tokens, requested.max_tokens, "max_tokens"),
            max_turns=limit(self.max_turns, requested.max_turns, "max_turns"),
            max_tool_calls=limit(self.max_tool_calls, requested.max_tool_calls, "max_tool_calls"),
        )


@dataclass(frozen=True)
class RuntimeEnvelope:
    """One resolved capability receipt, suitable for status and durable evidence."""

    provider: str
    model: str
    tools: frozenset[str]
    permission_mode: str
    sandbox: bool
    writable_roots: tuple[str, ...]
    network_destinations: tuple[str, ...]
    credential_capabilities: frozenset[str]
    budget: RuntimeBudget
    lifecycle: str

    @classmethod
    def root(cls, runtime, policy: PermissionPolicy, catalog: ToolCatalog, *, cwd: Path | None = None) -> "RuntimeEnvelope":
        provider = str(getattr(runtime, "provider_id", "") or "not configured")
        return cls(
            provider=provider,
            model=str(getattr(runtime, "model", "not configured")),
            tools=frozenset(policy.allow & catalog.names("parent")),
            permission_mode=_permission_mode(policy),
            # #47 will replace this receipt value with the enforced Sandbox state.
            sandbox=False,
            writable_roots=(str((cwd or Path.cwd()).resolve()),),
            # Existing behavior permits network access; make that visible before it is governed.
            network_destinations=("*",),
            credential_capabilities=frozenset({provider} if provider != "not configured" else ()),
            budget=RuntimeBudget(max_tokens=getattr(runtime, "max_tokens", None)),
            lifecycle="root-session",
        )

    def narrow(
        self,
        *,
        tools: set[str] | frozenset[str] | None = None,
        writable_roots: tuple[str, ...] | None = None,
        network_destinations: tuple[str, ...] | None = None,
        credential_capabilities: set[str] | frozenset[str] | None = None,
        budget: RuntimeBudget | None = None,
        lifecycle: str = "child-run",
    ) -> "RuntimeEnvelope":
        child_tools = self.tools if tools is None else frozenset(tools)
        child_credentials = self.credential_capabilities if credential_capabilities is None else frozenset(credential_capabilities)
        child_roots = self.writable_roots if writable_roots is None else tuple(writable_roots)
        child_network = self.network_destinations if network_destinations is None else tuple(network_destinations)
        if not child_tools <= self.tools:
            raise _widening_error("Runtime envelope cannot widen tools")
        if not child_credentials <= self.credential_capabilities:
            raise _widening_error("Runtime envelope cannot widen credential capabilities")
        if not set(child_roots) <= set(self.writable_roots):
            raise _widening_error("Runtime envelope cannot widen writable roots")
        if self.network_destinations != ("*",) and not set(child_network) <= set(self.network_destinations):
            raise _widening_error("Runtime envelope cannot widen network destinations")
        return RuntimeEnvelope(
            provider=self.provider,
            model=self.model,
            tools=child_tools,
            permission_mode=self.permission_mode,
            sandbox=self.sandbox,
            writable_roots=child_roots,
            network_destinations=child_network,
            credential_capabilities=child_credentials,
            budget=self.budget.narrowed(budget or RuntimeBudget()),
            lifecycle=lifecycle,
        )

    def receipt(self) -> dict[str, object]:
        receipt = asdict(self)
        receipt["tools"] = sorted(self.tools)
        receipt["credential_capabilities"] = sorted(self.credential_capabilities)
        receipt["writable_roots"] = list(self.writable_roots)
        receipt["network_destinations"] = list(self.network_destinations)
        return {"version": 1, **receipt}


def _permission_mode(policy: PermissionPolicy) -> str:
    from penhin.infrastructure.config import get_permission_mode
    return get_permission_mode() if policy.deny == set() else "restricted"


def _widening_error(message: str) -> ValueError:
    from penhin.evaluation.observer import emit
    emit("runtime_envelope_widening_rejected", reason=message)
    return ValueError(message)


def current_envelope() -> RuntimeEnvelope | None:
    return _current.get()


def resolve_envelope(context, runtime, catalog: ToolCatalog) -> RuntimeEnvelope:
    """Attach and durably record a root receipt exactly once for a run context."""
    if context.runtime_envelope is None:
        context.runtime_envelope = RuntimeEnvelope.root(runtime, context.policy, catalog)
    context.record_runtime_envelope()
    return context.runtime_envelope


@contextmanager
def using_envelope(envelope: RuntimeEnvelope) -> Iterator[None]:
    token = _current.set(envelope)
    try:
        yield
    finally:
        _current.reset(token)
