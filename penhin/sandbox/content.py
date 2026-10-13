"""Capture explicitly pinned content independently of replaceable source paths.

The caller supplies a trusted file-digest manifest and its pinned identity. This
does not authenticate a publisher or replace Plugin Artifact signature checks,
and does not introduce an on-disk Artifact format.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import sys
from typing import Mapping


MAX_CONTENT_BYTES = 256 * 1024 * 1024
MAX_CONTENT_ENTRIES = 10_000


class ContentVerificationError(ValueError):
    """Content could not be prepared without weakening verification."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VerifiedFile:
    path: str
    sha256: str
    data: bytes


@dataclass(frozen=True)
class VerifiedContent:
    """Immutable captured bytes; safe to reuse without reopening source paths."""

    identity: str
    files: tuple[VerifiedFile, ...]

    def _check_integrity(self) -> None:
        if not isinstance(self.files, tuple) or len(self.files) > MAX_CONTENT_ENTRIES:
            raise ContentVerificationError("invalid_content", "Verified content requires an immutable bounded file tuple")
        manifest = {}
        total = 0
        for item in self.files:
            if not isinstance(item, VerifiedFile) or not isinstance(item.data, bytes):
                raise ContentVerificationError("invalid_content", "Verified content requires immutable file bytes")
            _validated_manifest({item.path: item.sha256})
            if item.path in manifest:
                raise ContentVerificationError("invalid_content", "Verified content contains duplicate paths")
            total += len(item.data)
            if total > MAX_CONTENT_BYTES:
                raise ContentVerificationError("content_too_large", "Content exceeds the 256 MiB preparation limit")
            if not hmac.compare_digest(hashlib.sha256(item.data).hexdigest(), item.sha256):
                raise ContentVerificationError("digest_mismatch", f"Content digest does not match: {item.path}")
            manifest[item.path] = item.sha256
        if not isinstance(self.identity, str) or not self.identity.isascii() or not hmac.compare_digest(content_identity(manifest), self.identity):
            raise ContentVerificationError("identity_mismatch", "Content manifest identity does not match")

    def seal(self) -> SealedContent:
        """Own sealed non-executable Linux data memfds until explicitly closed.

        This fixes data bytes, not a Python execution profile. No Plugin code is
        started here. Linux 6.3+ MFD_NOEXEC_SEAL is required; there is no fallback
        to a writable file or to kernel-dependent executable memfd defaults.
        """
        self._check_integrity()
        if sys.platform != "linux":
            raise ContentVerificationError("unsupported_platform", "Sealed content requires Linux; this platform only supports content preparation")
        try:
            import fcntl

            memfd_create = os.memfd_create
            add_seals, get_seals = fcntl.F_ADD_SEALS, fcntl.F_GET_SEALS
            required = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
            # Stable Linux UAPI constants, including on older Python headers:
            # include/uapi/linux/memfd.h and include/uapi/linux/fcntl.h.
            noexec_seal, seal_exec = 0x0008, 0x0020
            flags = os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING | noexec_seal
        except (ImportError, AttributeError) as error:
            raise ContentVerificationError("sealing_unavailable", "Required Linux memfd sealing APIs are unavailable") from error
        descriptors: dict[str, int] = {}
        try:
            for item in self.files:
                descriptor = memfd_create("penhin-content", flags)
                descriptors[item.path] = descriptor
                remaining = memoryview(item.data)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise ContentVerificationError("sealing_failed", "Unable to populate sealed content")
                    remaining = remaining[written:]
                os.lseek(descriptor, 0, os.SEEK_SET)
                fcntl.fcntl(descriptor, add_seals, required)
                actual = fcntl.fcntl(descriptor, get_seals)
                if actual & (required | seal_exec) != required | seal_exec or os.fstat(descriptor).st_mode & 0o111:
                    raise ContentVerificationError("sealing_failed", "Kernel did not apply the required content and non-execution seals")
                # Bind verification to the sealed inode itself, including a
                # possible same-UID writer racing us before F_ADD_SEALS.
                digest = hashlib.sha256()
                offset = 0
                while chunk := os.pread(descriptor, 1024 * 1024, offset):
                    offset += len(chunk)
                    if offset > len(item.data):
                        break
                    digest.update(chunk)
                if offset != len(item.data) or not hmac.compare_digest(digest.hexdigest(), item.sha256):
                    raise ContentVerificationError("digest_mismatch", f"Sealed content digest does not match: {item.path}")
            return SealedContent(self.identity, descriptors)
        except BaseException as error:
            for descriptor in descriptors.values():
                os.close(descriptor)
            if isinstance(error, OSError):
                raise ContentVerificationError("sealing_failed", f"Linux content sealing failed: {error}") from error
            raise


class SealedContent:
    """Own immutable memfds, with a separate read offset for each consumer.

    Keep this object alive across activations. Each open_file() result belongs
    to its caller and must be closed by that caller. The owner is not safe to
    close concurrently with open_file(). Closing it never closes borrowed FDs.
    """

    def __init__(self, identity: str, descriptors: dict[str, int]) -> None:
        self.identity = identity
        self._descriptors = descriptors
        self._closed = False

    @property
    def paths(self) -> tuple[str, ...]:
        self._require_open()
        return tuple(self._descriptors)

    def _require_open(self) -> None:
        if self._closed:
            raise ContentVerificationError("content_closed", "Sealed content is closed")

    def open_file(self, relative: str) -> int:
        """Return an owned read-only FD at offset zero for one captured file."""
        self._require_open()
        descriptor = self._descriptors[relative]
        # dup() shares an offset and breaks concurrent/repeated consumers.
        # /proc/self/fd reopens this retained immutable inode, never its source.
        return os.open(f"/proc/self/fd/{descriptor}", os.O_RDONLY | os.O_CLOEXEC)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            descriptors, self._descriptors = self._descriptors, {}
            for descriptor in descriptors.values():
                os.close(descriptor)

    def __enter__(self) -> SealedContent:
        self._require_open()
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()


def content_identity(digests: Mapping[str, str]) -> str:
    """Return the identity to pin alongside a separately trusted manifest."""
    encoded = json.dumps(_validated_manifest(digests), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validated_manifest(digests: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(digests, Mapping) or len(digests) > MAX_CONTENT_ENTRIES:
        raise ContentVerificationError("invalid_manifest", "Content manifest must be a bounded file-digest mapping")
    manifest = dict(digests)
    for relative, digest in manifest.items():
        if (
            not isinstance(relative, str) or not relative or len(relative) > 4096
            or any(char in relative for char in "\\:\x00")
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ContentVerificationError("invalid_manifest", "Content manifest requires canonical relative paths and lowercase SHA-256 digests")
    return manifest


def _check_kind(info: os.stat_result) -> None:
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
        or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
    ):
        raise ContentVerificationError("unsafe_content", "Content contains a symlink, hardlink, reparse point, or special file")


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    # Windows lstat/fstat can expose different ctime semantics. Birth time is
    # consistent there; Linux retains ctime's change detection. The Windows
    # branch is preparation only and never enables sealed Linux execution.
    change_time = getattr(info, "st_birthtime_ns", info.st_ctime_ns) if os.name == "nt" else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, change_time)


def _entry_stat(directory: Path | int, name: str) -> os.stat_result:
    if isinstance(directory, int):
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    return (directory / name).lstat()


def _open_root(root: Path) -> Path | int:
    if ".." in root.parts:
        raise ContentVerificationError("unsafe_content", "Content root must not contain parent traversal")
    root = root.absolute()
    if sys.platform == "linux":
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(root.anchor, flags)
        try:
            for part in root.parts[1:]:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    # This path prepares bytes only; Windows path checks are not Linux's
    # descriptor-relative enforcement and cannot enable a sandbox launch.
    for parent in (*reversed(root.parents), root):
        info = parent.lstat()
        _check_kind(info)
        if not stat.S_ISDIR(info.st_mode):
            raise ContentVerificationError("unsafe_content", "Content root ancestors must be directories")
    return root


def _read_file(directory: Path | int, name: str, before: os.stat_result, remaining: int) -> bytes:
    if before.st_size > remaining:
        raise ContentVerificationError("content_too_large", "Content exceeds the 256 MiB preparation limit")
    if isinstance(directory, int):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    else:
        descriptor = os.open(directory / name, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        opened = os.fstat(descriptor)
        _check_kind(opened)
        if _stamp(before) != _stamp(opened):
            raise ContentVerificationError("content_changed", "Content file changed before capture")
        chunks = []
        total = 0
        while chunk := os.read(descriptor, min(1024 * 1024, remaining - total + 1)):
            total += len(chunk)
            if total > remaining:
                raise ContentVerificationError("content_too_large", "Content exceeds the 256 MiB preparation limit")
            chunks.append(chunk)
        if _stamp(opened) != _stamp(os.fstat(descriptor)) or _stamp(opened) != _stamp(_entry_stat(directory, name)):
            raise ContentVerificationError("content_changed", "Content file changed during capture")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _snapshot(root: Path, *, read_bytes: bool) -> tuple[dict[str, bytes], dict[str, tuple[int, ...]]]:
    files: dict[str, bytes] = {}
    inventory: dict[str, tuple[int, ...]] = {}
    total_bytes = 0

    def walk(directory: Path | int, prefix: str, depth: int) -> None:
        nonlocal total_bytes
        if depth > 64:
            raise ContentVerificationError("content_too_large", "Content nesting exceeds 64 directories")
        before = os.fstat(directory) if isinstance(directory, int) else directory.lstat()
        _check_kind(before)
        inventory[prefix] = _stamp(before)
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) + len(inventory) > MAX_CONTENT_ENTRIES:
                    raise ContentVerificationError("content_too_large", "Content exceeds the 10,000-entry preparation limit")
        for name in sorted(names):
            relative = f"{prefix}/{name}" if prefix else name
            info = _entry_stat(directory, name)
            _check_kind(info)
            inventory[relative] = _stamp(info)
            if len(inventory) > MAX_CONTENT_ENTRIES:
                raise ContentVerificationError("content_too_large", "Content exceeds the 10,000-entry preparation limit")
            if stat.S_ISDIR(info.st_mode):
                child = (
                    os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    if isinstance(directory, int) else directory / name
                )
                try:
                    opened = os.fstat(child) if isinstance(child, int) else child.lstat()
                    if _stamp(info) != _stamp(opened):
                        raise ContentVerificationError("content_changed", "Content directory changed before capture")
                    walk(child, relative, depth + 1)
                finally:
                    if isinstance(child, int):
                        os.close(child)
            else:
                data = _read_file(directory, name, info, MAX_CONTENT_BYTES - total_bytes) if read_bytes else b""
                total_bytes += len(data)
                files[relative] = data
        after = os.fstat(directory) if isinstance(directory, int) else directory.lstat()
        if _stamp(before) != _stamp(after):
            raise ContentVerificationError("content_changed", "Content directory changed during capture")

    directory = _open_root(root)
    try:
        walk(directory, "", 0)
    finally:
        if isinstance(directory, int):
            os.close(directory)
    return files, inventory


def capture_verified_content(
    root: Path,
    *,
    digests: Mapping[str, str],
    expected_identity: str,
) -> VerifiedContent:
    """Verify all files and retain at most 256 MiB / 10,000 filesystem entries.

    Linux walks use directory descriptors and O_NOFOLLOW. Other platforms only
    prepare and validate content; they do not establish Linux launch support.
    The returned bytes remain immutable even after the source tree is replaced.
    """
    manifest = _validated_manifest(digests)
    identity = content_identity(manifest)
    if not isinstance(expected_identity, str) or not expected_identity.isascii() or not hmac.compare_digest(identity, expected_identity):
        raise ContentVerificationError("identity_mismatch", "Content manifest identity does not match")
    try:
        captured, before = _snapshot(Path(root), read_bytes=True)
        _, after = _snapshot(Path(root), read_bytes=False)
    except OSError as error:
        raise ContentVerificationError("unsafe_content", f"Unable to capture content safely: {error}") from error
    if before != after:
        raise ContentVerificationError("content_changed", "Content changed between capture and verification")
    if captured.keys() != manifest.keys():
        raise ContentVerificationError("manifest_mismatch", "Content files do not exactly match the trusted manifest")
    files = []
    for relative, expected in sorted(manifest.items()):
        data = captured[relative]
        actual = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise ContentVerificationError("digest_mismatch", f"Content digest does not match: {relative}")
        files.append(VerifiedFile(relative, actual, data))
    return VerifiedContent(identity, tuple(files))
