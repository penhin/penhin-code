from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .interaction import AuthInteraction
from .models import OAuthCredential
from .secrets import register_secret


OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


class OAuthError(RuntimeError):
    pass


def _disabled() -> bool:
    return os.getenv("PENHIN_DISABLE_EXPERIMENTAL_OAUTH", "").lower() in {"1", "true", "yes", "on"}


def _loopback_host(value: str) -> str:
    if value not in {"127.0.0.1", "localhost", "::1"}:
        raise OAuthError("OAuth callback host must be loopback-only")
    return value


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _state() -> str:
    return secrets.token_urlsafe(24)


def _token(response: httpx.Response, *, skew_seconds: int = 60) -> OAuthCredential:
    if not response.is_success:
        raise OAuthError(f"OAuth token request failed with HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as error:
        raise OAuthError("OAuth token endpoint returned invalid JSON") from error
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not isinstance(data.get("refresh_token"), str):
        raise OAuthError("OAuth token response is missing required fields")
    expires_in = data.get("expires_in")
    if not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool) or expires_in <= 0:
        raise OAuthError("OAuth token response has an invalid expiry")
    access, refresh = data["access_token"], data["refresh_token"]
    register_secret(access)
    register_secret(refresh)
    return OAuthCredential(
        access_token=access,
        refresh_token=refresh,
        expires_at=int(time.time() + expires_in - skew_seconds),
        account_id=_openai_account_id(access),
        scope=data.get("scope") if isinstance(data.get("scope"), str) else None,
    )


def _openai_account_id(token: str) -> str | None:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        auth = payload.get("https://api.openai.com/auth", {})
        value = auth.get("chatgpt_account_id")
        return value if isinstance(value, str) and value else None
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return None


class LoopbackCallback:
    def __init__(self, host: str, port: int, path: str, expected_state: str):
        self.result: tuple[str, str] | None = None
        self.event = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                values = parse_qs(parsed.query)
                code = values.get("code", [""])[0]
                state = values.get("state", [""])[0]
                valid = parsed.path == path and bool(code) and state == expected_state
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(("Authentication completed. You can close this window." if valid else "Authentication failed.").encode())
                if valid:
                    outer.result = (code, state)
                    outer.event.set()

            def log_message(self, _format, *_args):
                return

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def wait(self, timeout: float) -> tuple[str, str] | None:
        self.event.wait(timeout)
        return self.result

    def cancel(self) -> None:
        self.event.set()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _parse_manual(value: str, expected_state: str) -> tuple[str, str]:
    value = value.strip()
    if not value:
        raise OAuthError("missing authorization code")
    try:
        parsed = urlparse(value)
        if parsed.scheme and parsed.netloc:
            values = parse_qs(parsed.query)
            code, state = values.get("code", [""])[0], values.get("state", [""])[0]
        elif "#" in value:
            code, state = value.split("#", 1)
        else:
            code, state = value, expected_state
    except ValueError as error:
        raise OAuthError("invalid authorization response") from error
    if state != expected_state:
        raise OAuthError("OAuth state mismatch")
    if not code:
        raise OAuthError("missing authorization code")
    register_secret(code)
    return code, state


async def _race_browser_result(
    interaction: AuthInteraction,
    callback: LoopbackCallback,
    state: str,
    timeout: float,
) -> tuple[str, str]:
    prompt_async = getattr(interaction, "prompt_async")
    manual_task = asyncio.create_task(prompt_async(
        "manual_code",
        "Complete login in your browser, or paste the authorization code / redirect URL here",
    ))
    callback_task = asyncio.create_task(asyncio.to_thread(callback.wait, timeout))
    try:
        done, _pending = await asyncio.wait({manual_task, callback_task}, return_when=asyncio.FIRST_COMPLETED)
        if callback_task in done:
            result = callback_task.result()
            if result is not None:
                manual_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await manual_task
                return result
            manual_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await manual_task
            raise OAuthError("OAuth callback timed out")

        manual = await manual_task
        callback.cancel()
        callback_result = await callback_task
        if manual.strip():
            return _parse_manual(manual, state)
        if callback_result is not None:
            return callback_result
        raise OAuthError("missing authorization code")
    finally:
        callback.cancel()
        for task in (manual_task, callback_task):
            if not task.done():
                task.cancel()


def _browser_result(interaction: AuthInteraction, url: str, callback: LoopbackCallback | None, state: str) -> tuple[str, str]:
    if callback is not None:
        callback.start()
    interaction.notify("auth_url", url=url, instructions="Complete login in your browser.")
    timeout = float(os.getenv("PENHIN_OAUTH_CALLBACK_TIMEOUT_SECONDS", "180"))
    if callback is not None and callable(getattr(interaction, "prompt_async", None)):
        return asyncio.run(_race_browser_result(interaction, callback, state, timeout))

    manual = interaction.prompt("manual_code", "Paste the authorization code / redirect URL here")
    if manual.strip():
        return _parse_manual(manual, state)
    if callback is None:
        raise OAuthError("loopback callback is unavailable; paste the redirect URL instead")
    result = callback.wait(timeout)
    if result is None:
        raise OAuthError("OAuth callback timed out")
    return result


def login_openai_codex(interaction: AuthInteraction, method: str, client: httpx.Client | None = None) -> OAuthCredential:
    if _disabled():
        raise OAuthError("experimental OAuth is disabled")
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        if method == "device_code":
            return _openai_device(interaction, client)
        verifier, challenge = _pkce()
        state = _state()
        host = _loopback_host(os.getenv("PENHIN_OPENAI_OAUTH_CALLBACK_HOST", "127.0.0.1"))
        port = int(os.getenv("PENHIN_OPENAI_OAUTH_CALLBACK_PORT", "1455"))
        redirect = f"http://localhost:{port}/auth/callback"
        auth_base = os.getenv("PENHIN_OPENAI_OAUTH_AUTH_BASE_URL", "https://auth.openai.com").rstrip("/")
        params = {
            "response_type": "code", "client_id": os.getenv("PENHIN_OPENAI_OAUTH_CLIENT_ID", OPENAI_CLIENT_ID),
            "redirect_uri": redirect, "scope": "openid profile email offline_access",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
            "id_token_add_organizations": str(True).lower(),
            "codex_cli_simplified_flow": str(True).lower(),
            "originator": "pi",
        }
        try:
            callback = LoopbackCallback(host, port, "/auth/callback", state)
        except OSError:
            callback = None
        try:
            code, _ = _browser_result(interaction, f"{auth_base}/oauth/authorize?{urlencode(params)}", callback, state)
        finally:
            if callback is not None:
                callback.close()
        response = client.post(f"{auth_base}/oauth/token", data={
            "grant_type": "authorization_code", "client_id": params["client_id"], "code": code,
            "code_verifier": verifier, "redirect_uri": redirect,
        })
        credential = _token(response, skew_seconds=60)
        if not credential.account_id:
            raise OAuthError("OpenAI token is missing the ChatGPT account id")
        return credential
    finally:
        if own_client:
            client.close()


def _openai_device(interaction: AuthInteraction, client: httpx.Client) -> OAuthCredential:
    auth_base = os.getenv("PENHIN_OPENAI_OAUTH_AUTH_BASE_URL", "https://auth.openai.com").rstrip("/")
    client_id = os.getenv("PENHIN_OPENAI_OAUTH_CLIENT_ID", OPENAI_CLIENT_ID)
    response = client.post(f"{auth_base}/api/accounts/deviceauth/usercode", json={"client_id": client_id})
    if not response.is_success:
        raise OAuthError(f"device authorization failed with HTTP {response.status_code}")
    data = response.json()
    try:
        device_auth_id, user_code, interval = str(data["device_auth_id"]), str(data["user_code"]), float(data["interval"])
    except (KeyError, TypeError, ValueError) as error:
        raise OAuthError("invalid device authorization response") from error
    register_secret(device_auth_id)
    register_secret(user_code)
    interaction.notify("device_code", user_code=user_code, verification_uri=f"{auth_base}/codex/device", interval_seconds=interval)
    deadline = time.monotonic() + 15 * 60
    while time.monotonic() < deadline:
        time.sleep(max(interval, 0.1))
        poll = client.post(f"{auth_base}/api/accounts/deviceauth/token", json={"device_auth_id": device_auth_id, "user_code": user_code})
        if poll.is_success:
            result = poll.json()
            if not isinstance(result.get("authorization_code"), str) or not isinstance(result.get("code_verifier"), str):
                raise OAuthError("invalid device token response")
            token = client.post(f"{auth_base}/oauth/token", data={
                "grant_type": "authorization_code", "client_id": client_id,
                "code": result["authorization_code"], "code_verifier": result["code_verifier"],
                "redirect_uri": f"{auth_base}/deviceauth/callback",
            })
            credential = _token(token)
            if not credential.account_id:
                raise OAuthError("OpenAI token is missing the ChatGPT account id")
            return credential
        error_code = None
        try:
            error_value = poll.json().get("error")
            error_code = error_value.get("code") if isinstance(error_value, dict) else error_value
        except (ValueError, AttributeError):
            pass
        if poll.status_code in {403, 404} or error_code == "deviceauth_authorization_pending":
            continue
        if poll.status_code == 429 or error_code == "slow_down":
            interval += 5
            continue
        if error_code in {"access_denied", "authorization_declined"}:
            raise OAuthError("device authorization was denied")
        if error_code in {"expired_token", "device_code_expired"}:
            raise OAuthError("device authorization expired")
        raise OAuthError(f"device token polling failed with HTTP {poll.status_code}")
    raise OAuthError("device authorization expired")


def login_anthropic(interaction: AuthInteraction, client: httpx.Client | None = None) -> OAuthCredential:
    if _disabled():
        raise OAuthError("experimental OAuth is disabled")
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    verifier, challenge = _pkce()
    state = verifier
    host = _loopback_host(os.getenv("PENHIN_ANTHROPIC_OAUTH_CALLBACK_HOST", "127.0.0.1"))
    port = int(os.getenv("PENHIN_ANTHROPIC_OAUTH_CALLBACK_PORT", "53692"))
    redirect = f"http://localhost:{port}/callback"
    authorize = os.getenv("PENHIN_ANTHROPIC_OAUTH_AUTHORIZE_URL", "https://claude.ai/oauth/authorize")
    token_url = os.getenv("PENHIN_ANTHROPIC_OAUTH_TOKEN_URL", "https://platform.claude.com/v1/oauth/token")
    client_id = os.getenv("PENHIN_ANTHROPIC_OAUTH_CLIENT_ID", ANTHROPIC_CLIENT_ID)
    scopes = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
    params = {"code": "true", "client_id": client_id, "response_type": "code", "redirect_uri": redirect,
              "scope": scopes, "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
    try:
        callback = LoopbackCallback(host, port, "/callback", state)
    except OSError:
        callback = None
    try:
        code, returned_state = _browser_result(interaction, f"{authorize}?{urlencode(params)}", callback, state)
    finally:
        if callback is not None:
            callback.close()
    try:
        response = client.post(token_url, json={"grant_type": "authorization_code", "client_id": client_id, "code": code,
                                                "state": returned_state, "redirect_uri": redirect, "code_verifier": verifier})
        return _token(response, skew_seconds=300)
    finally:
        if own_client:
            client.close()


def refresh_oauth(provider: str, current: OAuthCredential, client: httpx.Client | None = None) -> OAuthCredential:
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        if provider == "openai-codex":
            base = os.getenv("PENHIN_OPENAI_OAUTH_AUTH_BASE_URL", "https://auth.openai.com").rstrip("/")
            response = client.post(f"{base}/oauth/token", data={"grant_type": "refresh_token", "refresh_token": current.refresh_token,
                                                                  "client_id": os.getenv("PENHIN_OPENAI_OAUTH_CLIENT_ID", OPENAI_CLIENT_ID)})
            updated = _token(response)
            return OAuthCredential(updated.access_token, updated.refresh_token, updated.expires_at,
                                   updated.account_id or current.account_id, updated.scope or current.scope)
        if provider == "anthropic":
            url = os.getenv("PENHIN_ANTHROPIC_OAUTH_TOKEN_URL", "https://platform.claude.com/v1/oauth/token")
            response = client.post(url, json={"grant_type": "refresh_token", "refresh_token": current.refresh_token,
                                              "client_id": os.getenv("PENHIN_ANTHROPIC_OAUTH_CLIENT_ID", ANTHROPIC_CLIENT_ID)})
            return _token(response, skew_seconds=300)
        raise OAuthError(f"provider {provider} does not support OAuth refresh")
    finally:
        if own_client:
            client.close()
