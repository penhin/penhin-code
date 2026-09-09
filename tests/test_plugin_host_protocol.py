from __future__ import annotations

from dataclasses import dataclass, field

from penhin.plugins import Attachment, InputEvent
import pytest

from penhin.plugins.host import HostDiagnostic, HostProgress, HostResult, HostedInputEnricher, Invocation, MAX_HOST_JSON_CHARS, PROTOCOL_VERSION
from penhin.plugins.runtime import PluginRegistration, PluginRuntime
from penhin.plugins.sdk import PythonSdkHost, input_enricher
from penhin.result import Result
from penhin.tools.catalog import ToolCatalog


@dataclass
class FakeHost:
    payloads: list[dict] = field(default_factory=list)
    progress: list[HostProgress] = field(default_factory=list)
    diagnostics: list[HostDiagnostic] = field(default_factory=list)
    cancelled: bool = False
    closed: bool = False

    def invoke(self, invocation: Invocation, broker, progress, diagnostics):
        self.payloads.append(invocation.to_message())
        progress(HostProgress(invocation.id, "reading attachment", 1))
        attachment = invocation.attachments[0]
        assert not hasattr(attachment, "path")
        return HostResult(invocation.id, {"content": broker.read_attachment(invocation.id, attachment.id).decode("utf-8"), "schema_version": 1})

    def cancel(self, _invocation_id: str) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True


class Loader:
    def __init__(self, plugins):
        self.plugins = plugins

    def validate(self, _registration):
        return Result.success()

    def load(self, registration):
        return self.plugins[registration.name]


def test_hosted_input_enricher_uses_metadata_only_invocation_and_brokered_attachments() -> None:
    host = FakeHost()
    plugin = HostedInputEnricher(host)
    runtime = PluginRuntime(ToolCatalog([]), [PluginRegistration("ocr", "fixture")], Loader({"ocr": plugin}))
    runtime.discover(); runtime.activate("ocr")

    result = runtime.process_input(InputEvent("paste", "inspect", (Attachment.create(b"receipt", source="paste", mime_type="image/png"),)))

    assert result.ok
    assert result.data.supplements[0].content == "receipt"
    payload = host.payloads[0]
    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["event"] == {"kind": "paste", "text": "inspect"}
    assert payload["attachments"] == [{"id": payload["attachments"][0]["id"], "source": "paste", "mime_type": "image/png", "size": 7}]
    assert "content" not in str(payload).lower()
    assert plugin.progress() == (HostProgress(payload["id"], "reading attachment", 1),)


def test_hosted_input_enricher_reports_host_diagnostics_and_stops_only_its_host() -> None:
    class FailingHost(FakeHost):
        def invoke(self, invocation, _broker, _progress, diagnostics):
            diagnostics(HostDiagnostic(invocation.id, "warning", "partial result"))
            raise RuntimeError("host failed")

    failing, healthy = FailingHost(), FakeHost()
    runtime = PluginRuntime(
        ToolCatalog([]),
        [PluginRegistration("broken", "fixture"), PluginRegistration("ocr", "fixture")],
        Loader({"broken": HostedInputEnricher(failing), "ocr": HostedInputEnricher(healthy)}),
    )
    runtime.discover(); runtime.activate("broken"); runtime.activate("ocr")

    result = runtime.process_input(InputEvent("paste", "inspect", (Attachment.create(b"ok", source="paste", mime_type="text/plain"),)))

    assert result.ok and result.data.supplements[0].plugin_id == "ocr"
    assert runtime.deactivate("broken").ok
    assert failing.closed
    assert not healthy.closed


def test_python_sdk_implements_the_same_public_host_contract() -> None:
    @input_enricher
    def inspect(event, attachments):
        assert event.kind == "paste"
        return {"content": attachments[0].read_bytes().decode("utf-8"), "schema_version": 1}

    runtime = PluginRuntime(
        ToolCatalog([]), [PluginRegistration("python", "fixture")],
        Loader({"python": HostedInputEnricher(PythonSdkHost(inspect))}),
    )
    runtime.discover(); runtime.activate("python")

    result = runtime.process_input(InputEvent("paste", "inspect", (Attachment.create(b"sdk", source="paste", mime_type="text/plain"),)))

    assert result.ok and result.data.supplements[0].content == "sdk"


def test_host_results_bound_json_metadata_instead_of_embedded_media() -> None:
    with pytest.raises(ValueError, match="Attachment"):
        HostResult("call", {"base64": "x" * MAX_HOST_JSON_CHARS})
