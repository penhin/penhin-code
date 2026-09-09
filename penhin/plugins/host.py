"""Language-neutral contracts for Plugin hosts and brokered Attachments.

The envelopes in this module contain JSON metadata only.  Attachment bytes stay
behind :class:`AttachmentHandle`, so a transport can map ``read_bytes`` to a
pipe, socket, or platform-native handle without changing the Plugin contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from threading import Lock
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol
from uuid import uuid4

from penhin.plugins.input import AttachmentHandle, InputEnrichmentEvent

if TYPE_CHECKING:
    from penhin.tools.catalog import ToolCatalog


PROTOCOL_VERSION = 1
MAX_HOST_JSON_CHARS = 50_000


@dataclass(frozen=True)
class AttachmentDescriptor:
    """Transferable JSON metadata for one core-owned Attachment."""

    id: str
    source: str
    mime_type: str
    size: int

    @classmethod
    def from_handle(cls, handle: AttachmentHandle) -> "AttachmentDescriptor":
        return cls(handle.id, handle.source, handle.mime_type, handle.size)

    def to_message(self) -> dict[str, Any]:
        return {"id": self.id, "source": self.source, "mime_type": self.mime_type, "size": self.size}


@dataclass(frozen=True)
class Invocation:
    """Versioned invocation metadata sent to an isolated Plugin host."""

    id: str
    plugin_id: str
    operation: str
    event: InputEnrichmentEvent
    attachments: tuple[AttachmentDescriptor, ...]
    protocol_version: int = PROTOCOL_VERSION

    def to_message(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "id": self.id,
            "plugin_id": self.plugin_id,
            "operation": self.operation,
            "event": {"kind": self.event.kind, "text": self.event.text},
            "attachments": [attachment.to_message() for attachment in self.attachments],
        }


@dataclass(frozen=True)
class HostProgress:
    invocation_id: str
    message: str
    completed: int | None = None

    def to_message(self) -> dict[str, Any]:
        return {"protocol_version": PROTOCOL_VERSION, "invocation_id": self.invocation_id, "message": self.message, "completed": self.completed}


@dataclass(frozen=True)
class HostDiagnostic:
    invocation_id: str
    severity: str
    message: str

    def to_message(self) -> dict[str, Any]:
        return {"protocol_version": PROTOCOL_VERSION, "invocation_id": self.invocation_id, "severity": self.severity, "message": self.message}


@dataclass(frozen=True)
class HostResult:
    """A versioned, structured result. Media belongs in a separate Attachment."""

    invocation_id: str
    value: Mapping[str, Any]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("Host result schema_version must be positive")
        if not isinstance(self.value, Mapping):
            raise TypeError("Host result value must be an object")
        try:
            encoded = json.dumps(self.value, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Host result must be JSON-compatible metadata") from error
        if len(encoded) > MAX_HOST_JSON_CHARS:
            raise ValueError("Host result exceeds metadata limit; return media as an Attachment")

    def to_message(self) -> dict[str, Any]:
        return {"protocol_version": PROTOCOL_VERSION, "invocation_id": self.invocation_id, "schema_version": self.schema_version, "value": dict(self.value)}


class AttachmentBroker(Protocol):
    """The only protocol operation that redeems an Attachment descriptor."""

    def read_attachment(self, invocation_id: str, attachment_id: str) -> bytes: ...


class _InvocationAttachmentBroker:
    def __init__(self, invocation_id: str, handles: tuple[AttachmentHandle, ...]) -> None:
        self._invocation_id = invocation_id
        self._handles = {handle.id: handle for handle in handles}

    def read_attachment(self, invocation_id: str, attachment_id: str) -> bytes:
        if invocation_id != self._invocation_id:
            raise ValueError("Attachment handle is not valid for this invocation")
        try:
            return self._handles[attachment_id].read_bytes()
        except KeyError as error:
            raise ValueError("Attachment handle is unavailable") from error


class PluginHost(Protocol):
    """Transport-independent host boundary implemented once per SDK language."""

    def invoke(
        self,
        invocation: Invocation,
        broker: AttachmentBroker,
        progress: Callable[[HostProgress], None],
        diagnostics: Callable[[HostDiagnostic], None],
    ) -> HostResult: ...

    def cancel(self, invocation_id: str) -> None: ...
    def close(self) -> None: ...


class HostedInputEnricher:
    """Adapts a language-neutral host to the public PluginRuntime input seam."""

    input_enricher = {"event_kinds": ["*"], "mime_types": ["*"], "parallel_safe": False}

    def __init__(self, host: PluginHost, *, plugin_id: str = "") -> None:
        self._host, self._plugin_id = host, plugin_id
        self._active: set[str] = set()
        self._progress: list[HostProgress] = []
        self._diagnostics: list[HostDiagnostic] = []
        self._lock = Lock()

    def catalog(self) -> "ToolCatalog":
        from penhin.tools.catalog import ToolCatalog
        return ToolCatalog([])

    def bind_plugin_id(self, plugin_id: str) -> None:
        """Runtime-only identity binding; hosts never infer identity from paths."""
        self._plugin_id = plugin_id

    @property
    def host_identity(self) -> int:
        """Lets PluginRuntime reject accidental host sharing between Plugins."""
        return id(self._host)

    def enrich_input(self, event: InputEnrichmentEvent, attachments: tuple[AttachmentHandle, ...]) -> dict[str, Any]:
        invocation = Invocation(uuid4().hex, self._plugin_id, "input.enrich", event, tuple(AttachmentDescriptor.from_handle(item) for item in attachments))
        with self._lock:
            self._active.add(invocation.id)
        try:
            result = self._host.invoke(invocation, _InvocationAttachmentBroker(invocation.id, attachments), self._record_progress, self._record_diagnostic)
            if result.invocation_id != invocation.id:
                raise ValueError("Host result does not match invocation")
            return dict(result.value)
        finally:
            with self._lock:
                self._active.discard(invocation.id)

    def progress(self) -> tuple[HostProgress, ...]:
        with self._lock:
            return tuple(self._progress)

    def diagnostics(self) -> tuple[HostDiagnostic, ...]:
        with self._lock:
            return tuple(self._diagnostics)

    def _record_progress(self, progress: HostProgress) -> None:
        with self._lock:
            self._progress.append(progress)

    def _record_diagnostic(self, diagnostic: HostDiagnostic) -> None:
        with self._lock:
            self._diagnostics.append(diagnostic)

    def cancel(self) -> None:
        with self._lock:
            active = tuple(self._active)
        for invocation_id in active:
            self._host.cancel(invocation_id)

    def running(self) -> bool:
        with self._lock:
            return bool(self._active)

    def close(self) -> None:
        self._host.close()
