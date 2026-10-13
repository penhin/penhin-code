from __future__ import annotations

from pathlib import Path
import os
import sys

import pytest

from penhin.sandbox.content import MAX_CONTENT_BYTES, ContentVerificationError, VerifiedContent, VerifiedFile, capture_verified_content, content_identity


HELLO_SHA256 = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_verified_content_retains_exact_bytes_after_source_replacement(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    root.mkdir()
    source = root / "plugin.py"
    source.write_bytes(b"hello")
    digests = {"plugin.py": HELLO_SHA256}

    content = capture_verified_content(root, digests=digests, expected_identity=content_identity(digests))
    source.unlink()
    source.write_bytes(b"replaced")

    assert content.files[0].path == "plugin.py"
    assert content.files[0].data == b"hello"
    assert content.files[0].sha256 == HELLO_SHA256
    assert content.identity == content_identity(digests)


@pytest.mark.parametrize("relative", ["", ".", "../outside", "/outside", "C:/outside", "C:outside", "a\\b", "a//b", "a/./b", "a/../b", "a\x00b"])
def test_manifest_rejects_ambiguous_or_outside_paths_before_reading(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ContentVerificationError) as error:
        content_identity({relative: HELLO_SHA256})
    assert error.value.code == "invalid_manifest"


def test_verified_content_requires_the_manifest_to_cover_every_file(tmp_path: Path) -> None:
    (tmp_path / "plugin.py").write_bytes(b"hello")
    (tmp_path / "unexpected.py").write_bytes(b"not verified")
    digests = {"plugin.py": HELLO_SHA256}

    with pytest.raises(ContentVerificationError) as error:
        capture_verified_content(tmp_path, digests=digests, expected_identity=content_identity(digests))

    assert error.value.code == "manifest_mismatch"


@pytest.mark.parametrize("arrangement", ["file_symlink", "root_symlink", "ancestor_symlink", "hardlink"])
def test_verified_content_rejects_source_aliases(tmp_path: Path, arrangement: str) -> None:
    root = tmp_path / "source"
    root.mkdir()
    source = root / "plugin.py"
    source.write_bytes(b"hello")
    if arrangement == "file_symlink":
        source.rename(tmp_path / "outside.py")
        source.symlink_to(tmp_path / "outside.py")
    elif arrangement == "root_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    elif arrangement == "ancestor_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        root = alias / "source"
    else:
        os.link(source, tmp_path / "outside.py")
    digests = {"plugin.py": HELLO_SHA256}

    with pytest.raises(ContentVerificationError) as error:
        capture_verified_content(root, digests=digests, expected_identity=content_identity(digests))

    assert error.value.code == "unsafe_content"


def test_sealing_fails_explicitly_outside_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "plugin.py").write_bytes(b"hello")
    digests = {"plugin.py": HELLO_SHA256}
    content = capture_verified_content(tmp_path, digests=digests, expected_identity=content_identity(digests))
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(ContentVerificationError) as error:
        content.seal()

    assert error.value.code == "unsupported_platform"


@pytest.mark.parametrize("failure", ["identity", "digest", "missing_file"])
def test_unverified_content_never_produces_a_snapshot(tmp_path: Path, failure: str) -> None:
    (tmp_path / "plugin.py").write_bytes(b"hello")
    digests = {"plugin.py": "0" * 64 if failure == "digest" else HELLO_SHA256}
    identity = "0" * 64 if failure == "identity" else content_identity(digests)
    if failure == "missing_file":
        (tmp_path / "plugin.py").unlink()

    with pytest.raises(ContentVerificationError) as error:
        capture_verified_content(tmp_path, digests=digests, expected_identity=identity)

    assert error.value.code == {"identity": "identity_mismatch", "digest": "digest_mismatch", "missing_file": "manifest_mismatch"}[failure]


def test_capture_rejects_a_file_changed_during_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "plugin.py"
    source.write_bytes(b"hello")
    digests = {"plugin.py": HELLO_SHA256}
    read = os.read
    modified = False

    def read_and_modify(descriptor: int, length: int) -> bytes:
        nonlocal modified
        result = read(descriptor, length)
        if result and not modified:
            modified = True
            source.write_bytes(b"changed after reading")
        return result

    monkeypatch.setattr(os, "read", read_and_modify)
    with pytest.raises(ContentVerificationError) as error:
        capture_verified_content(tmp_path, digests=digests, expected_identity=content_identity(digests))

    assert modified
    assert error.value.code == "content_changed"


def test_preparation_rejects_oversized_content_before_reading_it(tmp_path: Path) -> None:
    source = tmp_path / "plugin.py"
    with source.open("wb") as stream:
        stream.truncate(MAX_CONTENT_BYTES + 1)
    digests = {"plugin.py": HELLO_SHA256}

    with pytest.raises(ContentVerificationError) as error:
        capture_verified_content(tmp_path, digests=digests, expected_identity=content_identity(digests))

    assert error.value.code == "content_too_large"


@pytest.mark.parametrize("failure", ["bytes", "identity", "mutable_data", "duplicate_path"])
def test_sealing_rechecks_content_integrity_before_using_the_kernel(failure: str) -> None:
    digests = {"plugin.py": HELLO_SHA256}
    item = VerifiedFile("plugin.py", HELLO_SHA256, b"hello")
    if failure == "bytes":
        item = VerifiedFile("plugin.py", HELLO_SHA256, b"forged")
    elif failure == "mutable_data":
        item = VerifiedFile("plugin.py", HELLO_SHA256, bytearray(b"hello"))
    content = VerifiedContent(
        "0" * 64 if failure == "identity" else content_identity(digests),
        (item, item) if failure == "duplicate_path" else (item,),
    )

    with pytest.raises(ContentVerificationError) as error:
        content.seal()

    assert error.value.code == {
        "bytes": "digest_mismatch", "identity": "identity_mismatch",
        "mutable_data": "invalid_content", "duplicate_path": "invalid_content",
    }[failure]
