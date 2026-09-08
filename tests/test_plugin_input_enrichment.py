from dataclasses import dataclass

import pytest

from penhin.plugins import Attachment, AttachmentSession, InputEditor, InputEvent
from penhin.plugins.runtime import PluginRegistration, PluginRuntime
from penhin.result import Result
from penhin.tools.catalog import ToolCatalog


@dataclass
class FakeEnricher:
    name: str = "ocr"

    def catalog(self) -> ToolCatalog:
        return ToolCatalog([])

    def enrich_input(self, event: InputEvent, attachments):
        assert event.kind == "paste"
        assert not hasattr(event, "attachments")
        handle = attachments[0]
        assert not hasattr(handle, "path")
        assert handle.mime_type == "image/png"
        return {"content": handle.read_bytes().decode("utf-8"), "schema_version": 1}

    def close(self) -> None:
        pass


class FakeLoader:
    def __init__(self) -> None:
        self.plugin = FakeEnricher()

    def validate(self, _registration):
        return Result.success()

    def load(self, _registration):
        return self.plugin


class MemoryManager:
    def __init__(self, entries):
        self.entries = entries

    def effective(self):
        return self.entries


class ReloadingEnricher:
    def __init__(self, label, loader) -> None:
        self.label = label
        self.loader = loader
        self.closed = False

    def catalog(self) -> ToolCatalog:
        return ToolCatalog([])

    def enrich_input(self, _event, _attachments):
        if self.label == "first":
            self.loader.manager.entries["ocr"]["source"] = "second"
            self.loader.runtime.reload()
        return {"content": self.label, "schema_version": 1}

    def close(self) -> None:
        self.closed = True


class ReloadingLoader:
    def __init__(self, manager) -> None:
        self.manager = manager
        self.runtime = None

    def validate(self, registration):
        return Result.success(data={"identity": "official", "digest": registration.source, "capabilities": []})

    def load(self, registration):
        return ReloadingEnricher(registration.source, self)


class RetainingEnricher:
    def catalog(self) -> ToolCatalog:
        return ToolCatalog([])

    def enrich_input(self, _event, attachments):
        self.handle = attachments[0]
        return {"content": "accepted", "schema_version": 1}

    def close(self) -> None:
        pass


class RetainingLoader:
    def __init__(self) -> None:
        self.plugin = RetainingEnricher()

    def validate(self, _registration):
        return Result.success()

    def load(self, _registration):
        return self.plugin


def test_runtime_enriches_attachment_through_a_brokered_handle() -> None:
    runtime = PluginRuntime(ToolCatalog([]), [PluginRegistration("ocr", "fixture")], FakeLoader())
    runtime.discover()
    runtime.activate("ocr")
    attachment = Attachment.create(b"receipt total: 42", source="paste", mime_type="image/png")

    result = runtime.process_input(InputEvent("paste", "read this", (attachment,)))

    assert result.ok
    submission = result.data
    assert submission.text == "read this"
    assert submission.generation("ocr") == 1
    assert len(submission.supplements) == 1
    supplement = submission.supplements[0]
    assert supplement.plugin_id == "ocr"
    assert supplement.content == "receipt total: 42"
    assert supplement.trust == "untrusted"


def test_editor_folds_long_paste_expands_it_for_submission_and_backspace_removes_it() -> None:
    editor = InputEditor(fold_after_lines=2)
    editor.append_text("Summarize: ")
    editor.paste_text("one\ntwo\nthree")

    assert editor.display_text() == "Summarize: [pasted 3 lines]"
    assert editor.submit_event().text == "Summarize: one\ntwo\nthree"

    assert editor.backspace() is True
    assert editor.display_text() == "Summarize: "
    assert editor.submit_event().text == "Summarize: "


def test_runtime_rejects_an_attachment_after_its_session_releases_it() -> None:
    runtime = PluginRuntime(ToolCatalog([]), [], FakeLoader())
    session = AttachmentSession()
    attachment = session.create(b"secret", source="paste", mime_type="text/plain")
    event = InputEvent("paste", "inspect", (attachment,), session=session)
    session.release(attachment)

    result = runtime.process_input(event)

    assert not result.ok
    assert result.meta["code"] == "attachment_expired"


def test_editor_cannot_submit_a_released_session_attachment() -> None:
    session = AttachmentSession()
    attachment = session.create(b"secret", source="paste", mime_type="text/plain")
    editor = InputEditor()
    editor.paste_attachments((attachment,))
    session.release(attachment)

    with pytest.raises(ValueError, match="expired"):
        editor.submit_event()


def test_handle_cannot_read_an_attachment_after_its_session_releases_it() -> None:
    loader = RetainingLoader()
    runtime = PluginRuntime(ToolCatalog([]), [PluginRegistration("ocr", "fixture")], loader)
    runtime.discover()
    runtime.activate("ocr")
    session = AttachmentSession()
    attachment = session.create(b"secret", source="paste", mime_type="text/plain")
    assert runtime.process_input(InputEvent("paste", "inspect", (attachment,))).ok
    session.release(attachment)

    with pytest.raises(ValueError, match="expired"):
        loader.plugin.handle.read_bytes()


def test_editor_keeps_pasted_attachments_atomic_and_backspace_removes_their_backing_data() -> None:
    editor = InputEditor()
    attachment = Attachment.create(b"image", source="paste", mime_type="image/png")
    editor.paste_attachments((attachment,))

    assert editor.display_text() == "[pasted 1 attachment(s)]"
    assert editor.submit_event().attachments == (attachment,)

    assert editor.backspace() is True
    assert editor.submit_event().attachments == ()


def test_submitted_input_keeps_its_plugin_generation_when_reload_occurs_mid_enrichment() -> None:
    manager = MemoryManager({"ocr": {"source": "first", "config": {"authorized": True}}})
    loader = ReloadingLoader(manager)
    runtime = PluginRuntime.from_manager(ToolCatalog([]), manager, loader)
    loader.runtime = runtime
    runtime.discover()
    runtime.activate("ocr")

    first = runtime.process_input(InputEvent("paste", "one"))
    second = runtime.process_input(InputEvent("paste", "two"))

    assert first.data.supplements[0].content == "first"
    assert first.data.generation("ocr") == 1
    assert second.data.supplements[0].content == "second"
    assert second.data.generation("ocr") == 2


def test_attachment_metadata_is_observable_and_invalid_mime_or_size_is_rejected() -> None:
    attachment = Attachment.create(b"abc", source="paste", mime_type="text/plain")

    assert (attachment.source, attachment.mime_type, attachment.size) == ("paste", "text/plain", 3)
    with pytest.raises(ValueError, match="MIME"):
        Attachment.create(b"abc", source="paste", mime_type="plain")
    with pytest.raises(ValueError, match="exceeds"):
        Attachment.create(b"x" * (10 * 1024 * 1024 + 1), source="paste", mime_type="text/plain")
    with pytest.raises(ValueError, match="source"):
        Attachment("id", "", "text/plain", 1, b"x")
