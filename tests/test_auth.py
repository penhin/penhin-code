from __future__ import annotations

import json
import multiprocessing
import stat
import threading
import time
from pathlib import Path

import httpx
import pytest

from penhin.auth.models import ApiKeyCredential, OAuthCredential, credential_from_dict
from penhin.auth.oauth import LoopbackCallback, OAuthError, login_anthropic, login_openai_codex, refresh_oauth
from penhin.auth.oauth._flows import _browser_result, _pkce
from penhin.auth.resolver import AuthResolver
from penhin.auth.providers import provider_auth, provider_key_name
from penhin.auth.secrets import redact_text, register_secret, safe_value, scrubbed_environment
from penhin.auth.storage import CredentialStoreUnavailable, FileCredentialStore, InMemoryCredentialStore, KeyringCredentialStore


def _increment_file_credential(path: str, lock_path: str) -> None:
    store = FileCredentialStore(Path(path), Path(lock_path))
    store.modify("openai", lambda current: ApiKeyCredential(key=str(int(current.key) + 1)))


def test_credential_schema_rejects_unknown_and_mixed_fields() -> None:
    with pytest.raises(ValueError):
        credential_from_dict({"type": "api_key", "key": "secret", "access_token": "mixed"})
    with pytest.raises(ValueError):
        credential_from_dict({"type": "future", "key": "secret"})


def test_provider_auth_capabilities_match_runtime_protocols() -> None:
    assert provider_auth("anthropic").methods() == ("api_key", "oauth")
    assert provider_auth("openai").methods() == ("api_key",)
    assert provider_auth("openai-codex").methods() == ("oauth",)
    assert provider_auth("gemini").methods() == ("api_key",)
    assert provider_auth("deepseek").methods() == ("api_key",)
    assert provider_key_name("anthropic") == "ANTHROPIC_API_KEY"
    assert provider_key_name("openai") == "OPENAI_API_KEY"
    assert provider_key_name("openai-codex") is None
    assert provider_key_name("gemini") == "GEMINI_API_KEY"
    assert provider_key_name("deepseek") == "DEEPSEEK_API_KEY"
    with pytest.raises(ValueError):
        provider_auth("openai-codex").resolve(ApiKeyCredential(key="wrong-kind"))


def test_file_store_permissions_round_trip_and_delete(tmp_path: Path) -> None:
    path, lock = tmp_path / "penhin.auth.json", tmp_path / "penhin.auth.lock"
    store = FileCredentialStore(path, lock)
    credential = ApiKeyCredential(key="sentinel-secret")
    assert store.modify("anthropic", lambda _current: credential) == credential
    assert store.read("anthropic") == credential
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == "penhin.auth/v1"
    store.delete("anthropic")
    assert store.read("anthropic") is None


def test_file_store_hardens_existing_permissions_and_rejects_symlink(tmp_path: Path) -> None:
    path, lock = tmp_path / "penhin.auth.json", tmp_path / "penhin.auth.lock"
    payload = {"schema_version": "penhin.auth/v1", "providers": {"openai": {"type": "api_key", "key": "existing-secret"}}}
    path.write_text(json.dumps(payload))
    path.chmod(0o644)
    assert FileCredentialStore(path, lock).read("openai") == ApiKeyCredential(key="existing-secret")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    symlink = tmp_path / "linked.json"
    symlink.symlink_to(path)
    with pytest.raises(CredentialStoreUnavailable):
        FileCredentialStore(symlink, tmp_path / "linked.lock").read("openai")


def test_corrupt_file_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "penhin.auth.json"
    path.write_text("not-json")
    store = FileCredentialStore(path, tmp_path / "penhin.auth.lock")
    with pytest.raises(json.JSONDecodeError):
        store.modify("openai", lambda _current: ApiKeyCredential(key="new-secret"))
    assert path.read_text() == "not-json"


def test_file_store_serializes_refresh_style_check_then_write(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path / "penhin.auth.json", tmp_path / "penhin.auth.lock")
    expired = OAuthCredential("old-access", "old-refresh", 1)
    refreshed = OAuthCredential("new-access", "new-refresh", int(time.time()) + 3600)
    store.modify("anthropic", lambda _current: expired)
    refreshes = 0
    refresh_lock = threading.Lock()

    def refresh() -> None:
        def update(current):
            nonlocal refreshes
            if isinstance(current, OAuthCredential) and current.expires_at <= 1:
                with refresh_lock:
                    refreshes += 1
                time.sleep(0.02)
                return refreshed
            return None
        store.modify("anthropic", update)

    threads = [threading.Thread(target=refresh) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert refreshes == 1
    assert store.read("anthropic") == refreshed


def test_file_store_serializes_multiprocess_updates(tmp_path: Path) -> None:
    path, lock = tmp_path / "penhin.auth.json", tmp_path / "penhin.auth.lock"
    store = FileCredentialStore(path, lock)
    store.modify("openai", lambda _current: ApiKeyCredential(key="0"))
    processes = [multiprocessing.Process(target=_increment_file_credential, args=(str(path), str(lock))) for _ in range(6)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    assert store.read("openai") == ApiKeyCredential(key="6")


def test_in_memory_modify_serializes_concurrent_updates() -> None:
    store = InMemoryCredentialStore({"openai": ApiKeyCredential(key="0")})

    def update(index: int) -> None:
        store.modify("openai", lambda _current: ApiKeyCredential(key=str(index)))

    threads = [threading.Thread(target=update, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.read("openai") is not None
    assert len(store.list()) == 1


def test_keyring_store_round_trip_with_fake_backend(tmp_path: Path) -> None:
    class FakeKeyring:
        def __init__(self):
            self.values = {}

        def get_password(self, service, provider):
            return self.values.get((service, provider))

        def set_password(self, service, provider, value):
            self.values[(service, provider)] = value

        def delete_password(self, service, provider):
            del self.values[(service, provider)]

    store = object.__new__(KeyringCredentialStore)
    store.lock_path = tmp_path / "penhin.auth.lock"
    store.keyring = FakeKeyring()
    store.errors = (RuntimeError,)
    credential = ApiKeyCredential(key="keyring-secret")
    assert store.modify("gemini", lambda _current: credential) == credential
    assert store.read("gemini") == credential
    store.delete("gemini")
    assert store.read("gemini") is None


def test_keyring_unavailable_is_not_silently_downgraded(monkeypatch) -> None:
    import keyring

    monkeypatch.setattr(keyring, "get_keyring", lambda: type("Backend", (), {"priority": 0})())
    with pytest.raises(CredentialStoreUnavailable):
        KeyringCredentialStore()


def test_plaintext_keyring_backend_is_rejected(monkeypatch) -> None:
    import keyring

    backend_type = type("PlaintextKeyring", (), {"priority": 1})
    backend_type.__module__ = "keyrings.alt.file"
    monkeypatch.setattr(keyring, "get_keyring", backend_type)
    with pytest.raises(CredentialStoreUnavailable):
        KeyringCredentialStore()


def test_resolver_prefers_process_then_store_then_env(monkeypatch, tmp_path: Path) -> None:
    store = InMemoryCredentialStore({"openai": ApiKeyCredential(key="stored")})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "process")
    assert AuthResolver(store).resolve("openai").credential == ApiKeyCredential(key="process")
    monkeypatch.delenv("OPENAI_API_KEY")
    assert AuthResolver(store).resolve("openai").credential == ApiKeyCredential(key="stored")


def test_secret_redaction_and_subprocess_scrubbing(monkeypatch) -> None:
    register_secret("registered-secret-value")
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret-value")
    monkeypatch.setenv("PENHIN_DATABASE_URL", "postgres://user:password@host/db")
    assert redact_text("registered-secret-value environment-secret-value") == "<redacted> <redacted>"
    environment = scrubbed_environment()
    assert "OPENAI_API_KEY" not in environment
    assert "PENHIN_DATABASE_URL" not in environment
    assert safe_value({"auth_url": "https://provider/authorize?state=secret", "error_code": "timeout"}) == {
        "auth_url": "<redacted>", "error_code": "timeout",
    }


def test_pkce_generates_distinct_verifier_and_s256_challenge() -> None:
    first = _pkce()
    second = _pkce()
    assert first != second
    assert len(first[0]) >= 43 and len(first[1]) >= 43
    assert first[0] != first[1]


def test_loopback_rejects_wrong_state_and_accepts_expected_state() -> None:
    callback = LoopbackCallback("127.0.0.1", 0, "/callback", "expected")
    callback.start()
    port = callback.server.server_port
    try:
        wrong = httpx.get(f"http://127.0.0.1:{port}/callback?code=secret-code&state=wrong")
        assert wrong.status_code == 400
        good = httpx.get(f"http://127.0.0.1:{port}/callback?code=secret-code&state=expected")
        assert good.status_code == 200
        assert callback.wait(1) == ("secret-code", "expected")
    finally:
        callback.close()


class AsyncBrowserInteraction:
    def __init__(self) -> None:
        self.notified_url = ""
        self.prompt_started = threading.Event()
        self.prompt_cancelled = False

    def notify(self, kind: str, **payload: object) -> None:
        if kind == "auth_url":
            self.notified_url = str(payload["url"])

    def prompt(self, kind: str, message: str, options=()) -> str:
        raise AssertionError("the synchronous prompt must not be used")

    async def prompt_async(self, kind: str, message: str, options=()) -> str:
        import asyncio

        self.prompt_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.prompt_cancelled = True
            raise
        return ""


class ImmediateCallback:
    def __init__(self, result: tuple[str, str]) -> None:
        self.result = result
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def wait(self, timeout: float) -> tuple[str, str]:
        assert timeout > 0
        return self.result

    def cancel(self) -> None:
        self.cancelled = True


def test_browser_callback_completes_without_pressing_enter() -> None:
    interaction = AsyncBrowserInteraction()
    callback = ImmediateCallback(("authorization-code", "expected-state"))

    result = _browser_result(interaction, "https://provider.example/authorize", callback, "expected-state")

    assert result == ("authorization-code", "expected-state")
    assert callback.started is True
    assert callback.cancelled is True
    assert interaction.prompt_started.is_set()
    assert interaction.prompt_cancelled is True
    assert interaction.notified_url == "https://provider.example/authorize"


def test_refresh_rotates_both_openai_tokens() -> None:
    payload = "eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjdCJ9fQ"
    access = f"x.{payload}.x"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "access_token": access, "refresh_token": "new-refresh", "expires_in": 3600,
    }))
    current = OAuthCredential("old-access", "old-refresh", int(time.time()) - 1, account_id="old-account")
    with httpx.Client(transport=transport) as client:
        updated = refresh_oauth("openai-codex", current, client)
    assert updated.access_token == access
    assert updated.refresh_token == "new-refresh"
    assert updated.account_id == "acct"


def test_refresh_error_does_not_expose_response_body() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(400, text="server-secret-token"))
    current = OAuthCredential("old-access", "old-refresh", int(time.time()) - 1)
    with httpx.Client(transport=transport) as client, pytest.raises(OAuthError) as caught:
        refresh_oauth("anthropic", current, client)
    assert "server-secret-token" not in str(caught.value)


class ManualInteraction:
    def __init__(self):
        self.url = ""
        self.events: list[tuple[str, dict]] = []

    def notify(self, kind: str, **payload: object) -> None:
        self.events.append((kind, payload))
        if kind == "auth_url":
            self.url = str(payload["url"])

    def prompt(self, kind: str, message: str, options=()) -> str:
        state = parse_qs(urlparse(self.url).query)["state"][0]
        return f"http://localhost/callback?code=manual-code&state={state}"


from urllib.parse import parse_qs, urlparse


def test_anthropic_manual_oauth_uses_pkce_and_never_logs_token_body(monkeypatch) -> None:
    monkeypatch.setenv("PENHIN_ANTHROPIC_OAUTH_CALLBACK_PORT", "0")
    interaction = ManualInteraction()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["code"] == "manual-code"
        assert body["code_verifier"] == body["state"]
        return httpx.Response(200, json={"access_token": "anthropic-access", "refresh_token": "anthropic-refresh", "expires_in": 3600})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        credential = login_anthropic(interaction, client)
    assert credential.access_token == "anthropic-access"
    query = parse_qs(urlparse(interaction.url).query)
    assert query["code_challenge_method"] == ["S256"]


def test_openai_browser_oauth_extracts_account(monkeypatch) -> None:
    monkeypatch.setenv("PENHIN_OPENAI_OAUTH_CALLBACK_PORT", "0")
    interaction = ManualInteraction()
    payload = "eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjdCJ9fQ"

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"code_verifier=" in request.content
        return httpx.Response(200, json={"access_token": f"x.{payload}.x", "refresh_token": "openai-refresh", "expires_in": 3600})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        credential = login_openai_codex(interaction, "browser", client)
    assert credential.account_id == "acct"


def test_openai_device_flow_polls_then_exchanges(monkeypatch) -> None:
    monkeypatch.setattr("penhin.auth.oauth._flows.time.sleep", lambda _seconds: None)
    interaction = ManualInteraction()
    payload = "eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjdCJ9fQ"
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.url.path.endswith("/usercode"):
            return httpx.Response(200, json={"device_auth_id": "device-secret", "user_code": "ABCD-EFGH", "interval": 0})
        if request.url.path.endswith("/deviceauth/token"):
            polls += 1
            if polls == 1:
                return httpx.Response(400, json={"error": "slow_down"})
            return httpx.Response(200, json={"authorization_code": "code", "code_verifier": "verifier"})
        return httpx.Response(200, json={"access_token": f"x.{payload}.x", "refresh_token": "refresh", "expires_in": 3600})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        credential = login_openai_codex(interaction, "device_code", client)
    assert credential.account_id == "acct"
    assert polls == 2
    assert interaction.events[0][0] == "device_code"
