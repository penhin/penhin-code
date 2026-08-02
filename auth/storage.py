from __future__ import annotations

import json
import os
import stat
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from filelock import FileLock

from config import CONFIG_DIR, get_credential_backend
from .models import SUPPORTED_AUTH_PROVIDERS, Credential, credential_from_dict, credential_to_dict
from .secrets import register_secret


AUTH_FILE = CONFIG_DIR / "auth.json"
AUTH_LOCK_FILE = CONFIG_DIR / "auth.lock"
SCHEMA_VERSION = "penhin.auth/v1"
KEYRING_SERVICE = "penhin-code"
T = TypeVar("T")


class CredentialStoreUnavailable(RuntimeError):
    pass


def _secure_directory() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def _secure_atomic_write(path: Path, content: str) -> None:
    _secure_directory()
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _harden_regular_file(path: Path) -> None:
    if path.is_symlink():
        raise CredentialStoreUnavailable(f"refusing symlink credential path: {path}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CredentialStoreUnavailable(f"credential path is not a private regular file: {path}")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o600)


def _register(credential: Credential | None) -> Credential | None:
    if credential is not None:
        register_secret(getattr(credential, "key", None))
        register_secret(getattr(credential, "access_token", None))
        register_secret(getattr(credential, "refresh_token", None))
    return credential


class CredentialStore(ABC):
    backend_name: str

    @abstractmethod
    def read(self, provider: str) -> Credential | None: ...

    @abstractmethod
    def list(self) -> dict[str, Credential]: ...

    @abstractmethod
    def modify(self, provider: str, fn: Callable[[Credential | None], Credential | None]) -> Credential | None: ...

    @abstractmethod
    def delete(self, provider: str) -> None: ...


class InMemoryCredentialStore(CredentialStore):
    backend_name = "memory"

    def __init__(self, values: dict[str, Credential] | None = None):
        self.values = dict(values or {})
        self.lock = threading.RLock()

    def read(self, provider: str) -> Credential | None:
        with self.lock:
            return _register(self.values.get(provider))

    def list(self) -> dict[str, Credential]:
        with self.lock:
            return dict(self.values)

    def modify(self, provider: str, fn: Callable[[Credential | None], Credential | None]) -> Credential | None:
        with self.lock:
            next_value = fn(self.values.get(provider))
            if next_value is not None:
                self.values[provider] = next_value
            return _register(self.values.get(provider))

    def delete(self, provider: str) -> None:
        with self.lock:
            self.values.pop(provider, None)


class FileCredentialStore(CredentialStore):
    backend_name = "file"

    def __init__(self, path: Path = AUTH_FILE, lock_path: Path = AUTH_LOCK_FILE):
        self.path = path
        self.lock_path = lock_path

    def _load_unlocked(self) -> dict[str, Credential]:
        if not self.path.exists():
            return {}
        _harden_regular_file(self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or set(data) != {"schema_version", "providers"}:
            raise ValueError("unsupported or invalid credential file")
        providers = data["providers"]
        if not isinstance(providers, dict) or any(provider not in SUPPORTED_AUTH_PROVIDERS for provider in providers):
            raise ValueError("invalid credential provider")
        return {provider: credential_from_dict(value) for provider, value in providers.items()}

    def _save_unlocked(self, values: dict[str, Credential]) -> None:
        payload = {"schema_version": SCHEMA_VERSION, "providers": {key: credential_to_dict(value) for key, value in sorted(values.items())}}
        _secure_atomic_write(self.path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def _lock(self) -> FileLock:
        _secure_directory()
        if self.lock_path.is_symlink():
            raise CredentialStoreUnavailable(f"refusing symlink credential lock: {self.lock_path}")
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        os.chmod(self.lock_path, 0o600)
        return FileLock(str(self.lock_path))

    def read(self, provider: str) -> Credential | None:
        with self._lock():
            return _register(self._load_unlocked().get(provider))

    def list(self) -> dict[str, Credential]:
        with self._lock():
            return {key: _register(value) for key, value in self._load_unlocked().items()}  # type: ignore[misc]

    def modify(self, provider: str, fn: Callable[[Credential | None], Credential | None]) -> Credential | None:
        if provider not in SUPPORTED_AUTH_PROVIDERS:
            raise ValueError(f"unsupported auth provider: {provider}")
        with self._lock():
            values = self._load_unlocked()
            next_value = fn(values.get(provider))
            if next_value is None:
                return _register(values.get(provider))
            _register(next_value)
            values[provider] = next_value
            self._save_unlocked(values)
            return _register(next_value)

    def delete(self, provider: str) -> None:
        with self._lock():
            values = self._load_unlocked()
            if provider in values:
                del values[provider]
                self._save_unlocked(values)


class KeyringCredentialStore(CredentialStore):
    backend_name = "keyring"

    def __init__(self, lock_path: Path = AUTH_LOCK_FILE):
        self.lock_path = lock_path
        try:
            import keyring
            from keyring.errors import KeyringError, NoKeyringError
        except ImportError as error:
            raise CredentialStoreUnavailable("system keyring support is not installed") from error
        self.keyring = keyring
        self.errors = (KeyringError, NoKeyringError)
        backend = keyring.get_keyring()
        backend_identity = f"{backend.__class__.__module__}.{backend.__class__.__name__}".lower()
        nested = [f"{item.__class__.__module__}.{item.__class__.__name__}".lower() for item in getattr(backend, "backends", ())]
        insecure = backend_identity.startswith("keyrings.alt.") or "plaintext" in backend_identity or any(
            item.startswith("keyrings.alt.") or "plaintext" in item for item in nested
        )
        if getattr(backend, "priority", 0) <= 0 or insecure:
            raise CredentialStoreUnavailable("the system keyring is unavailable in this session")

    def _lock(self) -> FileLock:
        _secure_directory()
        if self.lock_path.is_symlink():
            raise CredentialStoreUnavailable(f"refusing symlink credential lock: {self.lock_path}")
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        os.chmod(self.lock_path, 0o600)
        return FileLock(str(self.lock_path))

    def _read_unlocked(self, provider: str) -> Credential | None:
        try:
            value = self.keyring.get_password(KEYRING_SERVICE, provider)
        except self.errors as error:
            raise CredentialStoreUnavailable("system keyring could not be read") from error
        if value is None:
            return None
        return _register(credential_from_dict(json.loads(value)))

    def read(self, provider: str) -> Credential | None:
        with self._lock():
            return self._read_unlocked(provider)

    def list(self) -> dict[str, Credential]:
        return {provider: credential for provider in sorted(SUPPORTED_AUTH_PROVIDERS) if (credential := self.read(provider)) is not None}

    def modify(self, provider: str, fn: Callable[[Credential | None], Credential | None]) -> Credential | None:
        if provider not in SUPPORTED_AUTH_PROVIDERS:
            raise ValueError(f"unsupported auth provider: {provider}")
        with self._lock():
            current = self._read_unlocked(provider)
            next_value = fn(current)
            if next_value is None:
                return current
            _register(next_value)
            try:
                self.keyring.set_password(KEYRING_SERVICE, provider, json.dumps(credential_to_dict(next_value)))
                verified = self._read_unlocked(provider)
            except self.errors as error:
                raise CredentialStoreUnavailable("system keyring could not be written") from error
            if verified != next_value:
                raise CredentialStoreUnavailable("system keyring verification failed")
            return _register(verified)

    def delete(self, provider: str) -> None:
        with self._lock():
            if self._read_unlocked(provider) is None:
                return
            try:
                self.keyring.delete_password(KEYRING_SERVICE, provider)
            except self.errors as error:
                raise CredentialStoreUnavailable("system keyring could not delete the credential") from error


def credential_store(backend: str | None = None) -> CredentialStore:
    selected = backend or get_credential_backend()
    if selected == "file":
        return FileCredentialStore()
    if selected not in {"", "keyring"}:
        raise ValueError(f"unsupported credential backend: {selected}")
    return KeyringCredentialStore()
