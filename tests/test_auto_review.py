from types import SimpleNamespace

from penhin.tools.auto_review import ALLOW, ASK_USER, review_bash


class Runtime:
    def __init__(self, text: str):
        self.text = text
        self.calls = []

    def call_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[{"type": "text", "text": self.text}])


def test_static_readonly_command_needs_no_reviewer() -> None:
    runtime = Runtime("ASK_USER")

    decision = review_bash("git status --short", runtime)

    assert decision.decision == ALLOW
    assert decision.evidence == "static_readonly"
    assert runtime.calls == []


def test_eligible_unmatched_command_uses_bounded_semantic_review() -> None:
    runtime = Runtime("ALLOW")

    decision = review_bash("npm test", runtime)

    assert decision.decision == ALLOW
    assert decision.evidence == "semantic"
    assert runtime.calls[0]["max_tokens"] == 8


def test_uncertain_or_high_risk_commands_ask_without_reviewer() -> None:
    runtime = Runtime("ALLOW")

    assert review_bash("rm -rf /", runtime).decision == ASK_USER
    assert review_bash("pytest -q && rm scratch", runtime).decision == ASK_USER
    assert review_bash("npm publish", Runtime("maybe")).decision == ASK_USER
    assert runtime.calls == []
