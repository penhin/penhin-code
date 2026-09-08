"""Public, attachment-backed input contracts for PluginRuntime."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class Attachment:
    """Opaque input data owned by the core for the lifetime of one submission."""

    id: str
    source: str
    mime_type: str
    size: int
    _content: bytes = field(repr=False, compare=False)
    session_id: str | None = None
    _session: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("Attachment id is required")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("Attachment source is required")
        if not isinstance(self.mime_type, str) or "/" not in self.mime_type:
            raise ValueError("Attachment MIME type is required")
        if not isinstance(self._content, bytes):
            raise TypeError("Attachment content must be bytes")
        if self.size != len(self._content):
            raise ValueError("Attachment size must match content")
        if self.size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"Attachment exceeds {MAX_ATTACHMENT_BYTES} byte limit")
        if self.session_id is not None and self._session is None:
            raise ValueError("Session attachment requires a session")

    @classmethod
    def create(cls, content: bytes, *, source: str, mime_type: str) -> "Attachment":
        return cls(uuid4().hex, source, mime_type, len(content), content)

    def is_live(self) -> bool:
        return self.session_id is None or self._session.is_live(self)


class AttachmentSession:
    """Core-owned attachment lifetime for one interactive session."""

    def __init__(self) -> None:
        self.id = uuid4().hex
        self._live: set[str] = set()

    def create(self, content: bytes, *, source: str, mime_type: str) -> Attachment:
        attachment = replace(Attachment.create(content, source=source, mime_type=mime_type), session_id=self.id, _session=self)
        self._live.add(attachment.id)
        return attachment

    def release(self, attachment: Attachment) -> None:
        if attachment.session_id != self.id:
            raise ValueError("Attachment does not belong to this session")
        self._live.discard(attachment.id)

    def is_live(self, attachment: Attachment) -> bool:
        return attachment.session_id == self.id and attachment.id in self._live


class AttachmentHandle:
    """The only attachment surface exposed to an Input Enricher."""

    __slots__ = ("id", "source", "mime_type", "size", "_attachment")

    def __init__(self, attachment: Attachment) -> None:
        self.id = attachment.id
        self.source = attachment.source
        self.mime_type = attachment.mime_type
        self.size = attachment.size
        self._attachment = attachment

    def read_bytes(self) -> bytes:
        if not self._attachment.is_live():
            raise ValueError("Attachment has expired")
        return bytes(self._attachment._content)


@dataclass(frozen=True)
class InputEvent:
    kind: str
    text: str
    attachments: tuple[Attachment, ...] = ()
    session: AttachmentSession | None = None

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("Input event kind is required")
        if not isinstance(self.text, str):
            raise TypeError("Input event text must be a string")
        if not self.attachments_are_live():
            raise ValueError("Input event contains expired attachments")

    def attachments_are_live(self) -> bool:
        if self.session is not None:
            return all(self.session.is_live(item) for item in self.attachments)
        return all(item.is_live() for item in self.attachments)


@dataclass(frozen=True)
class InputEnrichmentEvent:
    """The input metadata an Input Enricher may observe."""

    kind: str
    text: str


@dataclass(frozen=True)
class _InputToken:
    label: str
    text: str = ""
    attachments: tuple[Attachment, ...] = ()


class InputEditor:
    """Core-owned editor state that keeps pasted data atomic and reversible."""

    def __init__(self, *, fold_after_lines: int = 8) -> None:
        if fold_after_lines < 1:
            raise ValueError("fold_after_lines must be positive")
        self._fold_after_lines = fold_after_lines
        self._parts: list[str | _InputToken] = []

    def append_text(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("Editor text must be a string")
        if text:
            self._parts.append(text)

    def paste_text(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("Pasted text must be a string")
        lines = text.count("\n") + 1
        if lines > self._fold_after_lines:
            self._parts.append(_InputToken(f"[pasted {lines} lines]", text=text))
        else:
            self.append_text(text)

    def paste_attachments(self, attachments: tuple[Attachment, ...]) -> None:
        if not attachments:
            return
        if not all(isinstance(item, Attachment) for item in attachments):
            raise TypeError("Pasted attachments must be Attachments")
        self._parts.append(_InputToken(f"[pasted {len(attachments)} attachment(s)]", attachments=attachments))

    def display_text(self) -> str:
        return "".join(part if isinstance(part, str) else part.label for part in self._parts)

    def submit_event(self, *, kind: str = "submit") -> InputEvent:
        return InputEvent(
            kind,
            "".join(part if isinstance(part, str) else part.text for part in self._parts),
            tuple(attachment for part in self._parts if isinstance(part, _InputToken) for attachment in part.attachments),
        )

    def backspace(self) -> bool:
        if not self._parts:
            return False
        last = self._parts[-1]
        if isinstance(last, _InputToken):
            self._parts.pop()
        elif len(last) == 1:
            self._parts.pop()
        else:
            self._parts[-1] = last[:-1]
        return True


@dataclass(frozen=True)
class ContextSupplement:
    plugin_id: str
    generation: int
    content: str
    schema_version: int
    trust: str = "untrusted"
    display_mode: str = "submit"


@dataclass(frozen=True)
class InputSubmission:
    text: str
    supplements: tuple[ContextSupplement, ...]
    _generations: Mapping[str, int] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_generations", MappingProxyType(dict(self._generations)))

    def generation(self, plugin_id: str) -> int | None:
        return self._generations.get(plugin_id)


def supplement_from_result(plugin_id: str, generation: int, result: Any) -> ContextSupplement:
    """Normalize the small v1 result shape without letting plugins set trust."""
    if not isinstance(result, dict):
        raise ValueError("Input Enricher result must be an object")
    content = result.get("content")
    schema_version = result.get("schema_version")
    if not isinstance(content, str) or not content:
        raise ValueError("Input Enricher result requires non-empty content")
    if not isinstance(schema_version, int) or schema_version < 1:
        raise ValueError("Input Enricher result requires a positive schema_version")
    display_mode = result.get("display_mode", "submit")
    if display_mode not in {"submit", "display"}:
        raise ValueError("Input Enricher display_mode must be 'submit' or 'display'")
    return ContextSupplement(plugin_id, generation, content, schema_version, display_mode=display_mode)
