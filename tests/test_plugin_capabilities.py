from penhin.plugins.capabilities import PluginCapabilityBroker
from penhin.tools.execution import PermissionPolicy


def broker(declared, allowed):
    return PluginCapabilityBroker("sample", set(declared), PermissionPolicy(set(allowed)), {"service": "raw-secret-value"})


def test_undeclared_or_disallowed_capabilities_are_blocked() -> None:
    denied = broker({"state"}, {"state"})
    assert denied.request("network.fetch", "get", {"url": "https://example.com"}).meta["code"] == "undeclared_capability"
    disallowed = broker({"state"}, set())
    assert disallowed.request("state", "get", {"key": "x"}).meta["code"] == "capability_not_allowed"


def test_plugin_state_is_namespaced_by_identity_and_scope() -> None:
    first = broker({"state"}, {"state"})
    second = PluginCapabilityBroker("other", {"state"}, PermissionPolicy({"state"}))
    assert first.request("state", "set", {"scope": "project", "key": "theme", "value": "dark"}).ok
    assert first.request("state", "get", {"scope": "project", "key": "theme"}).data == {"value": "dark"}
    assert second.request("state", "get", {"scope": "project", "key": "theme"}).data == {"value": None}


def test_credentials_are_handles_and_never_raw_values() -> None:
    instance = broker({"credential.handle"}, {"credential.handle"})
    result = instance.request("credential.handle", "get", {"name": "service"})
    assert result.ok
    assert result.data == {"handle": "service"}
    assert "raw-secret-value" not in result.message


def test_capability_events_are_emitted_for_allowed_and_blocked_calls(monkeypatch) -> None:
    events = []
    monkeypatch.setattr("penhin.evaluation.observer.emit", lambda name, **data: events.append((name, data)))
    instance = broker({"state"}, {"state"})
    instance.request("state", "get", {"key": "a"})
    instance.request("network.fetch", "get", {"url": "https://example.test"})
    assert [event[1]["status"] for event in events] == ["allowed", "blocked"]
