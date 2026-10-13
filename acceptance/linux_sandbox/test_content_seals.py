"""Real Linux content contracts; run explicitly, never substitute unit mocks.

    python -m pytest acceptance/linux_sandbox/test_content_seals.py -q

Non-Linux hosts and kernels missing required sealing fail this suite. Passing
these contracts alone does not establish the complete Sandbox baseline.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import sys

import pytest

from penhin.sandbox.content import ContentVerificationError, capture_verified_content, content_identity


HELLO_SHA256 = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


@pytest.fixture(autouse=True)
def require_real_linux() -> None:
    assert sys.platform == "linux", "Linux content-sealing acceptance requires a real Linux kernel; unsupported platforms fail, never skip"


def prepare(root: Path, *, second_file: bool = False):
    root.mkdir()
    (root / "plugin.py").write_bytes(b"hello")
    digests = {"plugin.py": HELLO_SHA256}
    if second_file:
        (root / "dependency.py").write_bytes(b"hello")
        digests["dependency.py"] = HELLO_SHA256
    return capture_verified_content(root, digests=digests, expected_identity=content_identity(digests))


def test_memfds_retain_verified_bytes_and_reject_all_content_changes(tmp_path: Path) -> None:
    import fcntl

    root = tmp_path / "artifact"
    content = prepare(root)
    (root / "plugin.py").unlink()
    (root / "plugin.py").write_bytes(b"replacement")

    with content.seal() as sealed:
        descriptor = sealed.open_file("plugin.py")
        writable = os.open(f"/proc/self/fd/{descriptor}", os.O_RDWR | os.O_CLOEXEC)
        try:
            assert os.read(descriptor, 100) == b"hello"
            required = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL | 0x0020
            assert fcntl.fcntl(writable, fcntl.F_GET_SEALS) & required == required
            assert os.fstat(writable).st_mode & 0o111 == 0
            assert not os.get_inheritable(descriptor)
            for mutate in (
                lambda: os.pwrite(writable, b"x", 0),
                lambda: os.ftruncate(writable, 0),
                lambda: os.ftruncate(writable, 6),
                lambda: os.fchmod(writable, 0o700),
            ):
                with pytest.raises(OSError) as error:
                    mutate()
                assert error.value.errno == errno.EPERM
            assert os.pread(writable, 100, 0) == b"hello"
        finally:
            os.close(writable)
            os.close(descriptor)


def test_repeated_consumers_share_fixed_bytes_but_have_independent_offsets(tmp_path: Path) -> None:
    content = prepare(tmp_path / "artifact")
    with content.seal() as sealed:
        first = sealed.open_file("plugin.py")
        second = sealed.open_file("plugin.py")
        try:
            assert os.read(first, 2) == b"he"
            assert os.read(second, 100) == b"hello"
            assert os.read(first, 100) == b"llo"
        finally:
            os.close(first)
            os.close(second)
        third = sealed.open_file("plugin.py")
        try:
            assert os.read(third, 100) == b"hello"
        finally:
            os.close(third)


def test_context_manager_releases_all_owner_descriptors(tmp_path: Path) -> None:
    content = prepare(tmp_path / "artifact", second_file=True)
    before = set(os.listdir("/proc/self/fd"))
    with content.seal() as sealed:
        assert sealed.paths == ("dependency.py", "plugin.py")
    assert set(os.listdir("/proc/self/fd")) == before
    sealed.close()
    with pytest.raises(ContentVerificationError) as error:
        sealed.open_file("plugin.py")
    assert error.value.code == "content_closed"


def test_partial_sealing_failure_releases_previously_created_descriptors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    content = prepare(tmp_path / "artifact", second_file=True)
    created: list[int] = []
    memfd_create, fcntl_call = os.memfd_create, fcntl.fcntl

    def record_memfd(name: str, flags: int) -> int:
        descriptor = memfd_create(name, flags)
        created.append(descriptor)
        return descriptor

    def fail_second_seal(descriptor: int, command: int, argument: int = 0):
        if command == fcntl.F_ADD_SEALS and len(created) == 2:
            raise OSError(errno.EPERM, "injected kernel sealing failure")
        return fcntl_call(descriptor, command, argument)

    monkeypatch.setattr(os, "memfd_create", record_memfd)
    monkeypatch.setattr(fcntl, "fcntl", fail_second_seal)
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(ContentVerificationError) as error:
        content.seal()
    assert error.value.code == "sealing_failed"
    assert len(created) == 2
    for descriptor in created:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
    assert set(os.listdir("/proc/self/fd")) == before


def test_memfd_changes_before_sealing_are_detected_after_the_bytes_become_immutable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    content = prepare(tmp_path / "artifact")
    fcntl_call = fcntl.fcntl

    def modify_before_sealing(descriptor: int, command: int, argument: int = 0):
        if command == fcntl.F_ADD_SEALS:
            os.pwrite(descriptor, b"x", 0)
        return fcntl_call(descriptor, command, argument)

    monkeypatch.setattr(fcntl, "fcntl", modify_before_sealing)
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(ContentVerificationError) as error:
        content.seal()
    assert error.value.code == "digest_mismatch"
    assert set(os.listdir("/proc/self/fd")) == before


def test_special_files_are_rejected_without_opening_a_fifo(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    digests = {"pipe": HELLO_SHA256}

    with pytest.raises(ContentVerificationError) as error:
        capture_verified_content(tmp_path, digests=digests, expected_identity=content_identity(digests))

    assert error.value.code == "unsafe_content"


def test_directory_replacement_during_capture_cannot_follow_an_outside_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "artifact"
    nested = root / "dependencies"
    nested.mkdir(parents=True)
    (nested / "module.py").write_bytes(b"hello")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "module.py").write_bytes(b"unverified outside content")
    digests = {"dependencies/module.py": HELLO_SHA256}
    open_file = os.open
    replaced = False

    def replace_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if path == "dependencies" and dir_fd is not None and not replaced:
            replaced = True
            nested.rename(root / "retired")
            nested.symlink_to(outside, target_is_directory=True)
        return open_file(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises(ContentVerificationError) as error:
        capture_verified_content(root, digests=digests, expected_identity=content_identity(digests))
    assert replaced
    assert error.value.code == "unsafe_content"
