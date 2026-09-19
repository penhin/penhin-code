"""Fail-closed semantic review for auto-review approval misses."""
from __future__ import annotations

from dataclasses import dataclass

from penhin.approval_rules import bash_command_operations
from penhin.tools.builtin.shell import command_is_dangerous, command_references_ignored_path
from penhin.tools.execution.observability import input_summary
from penhin.orchestration.permissions import readonly_command_is_allowed


ALLOW = "ALLOW"
ASK_USER = "ASK_USER"
REVIEW_SYSTEM = (
    "You review a single developer shell command for automatic execution. "
    "Reply with exactly ALLOW only when it is clearly routine and low risk. "
    "Reply ASK_USER for uncertainty, writes, network access, credentials, destructive actions, "
    "or anything outside routine local development checks."
)


@dataclass(frozen=True)
class ReviewDecision:
    decision: str
    evidence: str


def review_bash(command: object, runtime=None) -> ReviewDecision:
    """Return an ephemeral decision; never persist or grant a session approval."""
    if not isinstance(command, str) or not command.strip() or len(command) > 1_000:
        return ReviewDecision(ASK_USER, "invalid_or_oversized")
    operations = bash_command_operations(command)
    if operations is None or len(operations) != 1:
        return ReviewDecision(ASK_USER, "compound_or_unsupported")
    if command_is_dangerous(command) or command_references_ignored_path(command):
        return ReviewDecision(ASK_USER, "high_risk_static")
    if readonly_command_is_allowed(command):
        return ReviewDecision(ALLOW, "static_readonly")
    try:
        if runtime is None:
            from penhin.runtime import runtime_manager
            runtime = runtime_manager.current()
        response = runtime.call_with_retry(
            system=REVIEW_SYSTEM,
            messages=[{"role": "user", "content": f"Command summary: {input_summary({'command': command})}"}],
            max_tokens=8,
        )
        text = "".join(
            str(block.get("text", ""))
            for block in getattr(response, "content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
    except Exception:
        return ReviewDecision(ASK_USER, "reviewer_unavailable")
    return ReviewDecision(ALLOW if text == ALLOW else ASK_USER, "semantic" if text in {ALLOW, ASK_USER} else "reviewer_uncertain")
