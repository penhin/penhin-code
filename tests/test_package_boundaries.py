import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "penhin"


def test_legacy_top_level_python_modules_are_removed() -> None:
    legacy = {
        "agent.py", "commands.py", "compact.py", "main.py", "runtime.py",
        "subagent.py", "task.py", "tool_runtime.py", "ui.py",
    }
    assert not {path.name for path in ROOT.glob("*.py")} & legacy


def test_stable_package_boundaries_exist() -> None:
    required = {
        "cli/main.py", "cli/commands/router.py", "agent/service.py",
        "agent/subagents/profiles.py", "runtime/manager.py", "runtime/factory.py",
        "auth/service.py", "auth/oauth/callback.py", "providers/registry.py",
        "providers/protocols.py", "tools/execution/approval.py",
        "tools/execution/validation.py", "tools/execution/observability.py",
        "orchestration/jobs.py", "orchestration/dags.py",
        "infrastructure/atomic_io.py", "evaluation/runner.py",
    }
    assert not {name for name in required if not (PACKAGE / name).is_file()}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_low_level_packages_do_not_depend_on_cli_or_orchestration() -> None:
    for area in ("providers", "infrastructure"):
        for path in (PACKAGE / area).rglob("*.py"):
            forbidden = {
                name for name in _imports(path)
                if name.startswith("penhin.cli") or name.startswith("penhin.orchestration")
            }
            assert not forbidden, f"{path.relative_to(ROOT)} imports {sorted(forbidden)}"


def test_public_packages_export_explicit_facades() -> None:
    from penhin.agent import AgentService
    from penhin.auth import AuthService
    from penhin.cli.commands import CommandRouter
    from penhin.orchestration import OrchestrationService
    from penhin.runtime import RuntimeManager
    from penhin.tools.execution import ToolExecutor

    assert all((AgentService, AuthService, CommandRouter, OrchestrationService, RuntimeManager, ToolExecutor))
