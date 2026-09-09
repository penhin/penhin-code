"""Official Python implementation of the language-neutral Plugin host contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from penhin.plugins.host import AttachmentBroker, AttachmentDescriptor, HostDiagnostic, HostProgress, HostResult, Invocation


@dataclass(frozen=True)
class InputEnrichment:
    """Metadata visible to a Python Input Enricher."""

    kind: str
    text: str


class AttachmentHandle:
    """SDK capability for one brokered Attachment; it has no filesystem path."""

    def __init__(self, descriptor: AttachmentDescriptor, broker: AttachmentBroker, invocation_id: str) -> None:
        self.id, self.source = descriptor.id, descriptor.source
        self.mime_type, self.size = descriptor.mime_type, descriptor.size
        self._read = lambda: broker.read_attachment(invocation_id, descriptor.id)

    def read_bytes(self) -> bytes:
        return self._read()


class PythonInputEnricher(Protocol):
    def __call__(self, event: InputEnrichment, attachments: tuple[AttachmentHandle, ...]) -> Mapping[str, Any]: ...


def input_enricher(function: PythonInputEnricher) -> PythonInputEnricher:
    """Mark a Python callable as an Input Enricher without coupling it to a host."""
    function.__penhin_input_enricher__ = True
    return function


class PythonSdkHost:
    """A Python SDK host; other language SDKs implement the same ``invoke`` API."""

    def __init__(self, handler: PythonInputEnricher) -> None:
        self._handler, self._cancelled, self._closed = handler, set(), False

    def invoke(self, invocation: Invocation, broker: AttachmentBroker, progress: Callable[[HostProgress], None], diagnostics: Callable[[HostDiagnostic], None]) -> HostResult:
        if self._closed:
            raise RuntimeError("Plugin host is closed")
        if invocation.operation != "input.enrich":
            raise ValueError(f"Unsupported Python SDK operation: {invocation.operation}")
        if invocation.id in self._cancelled:
            raise RuntimeError("Plugin invocation was cancelled")
        value = self._handler(
            InputEnrichment(invocation.event.kind, invocation.event.text),
            tuple(AttachmentHandle(item, broker, invocation.id) for item in invocation.attachments),
        )
        return HostResult(invocation.id, value)

    def cancel(self, invocation_id: str) -> None:
        self._cancelled.add(invocation_id)

    def close(self) -> None:
        self._closed = True


def tool(description: str, input_schema: dict[str, Any]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Annotate a callable declared by a plugin manifest.

    The host intentionally invokes only the manifest entrypoint in this beta; the
    annotation lets authors keep the schema and friendly description beside code
    and is checked by the loader in the next publishing phase.
    """
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        function.__penhin_tool__ = {"description": description, "input_schema": input_schema}
        return function
    return decorate
