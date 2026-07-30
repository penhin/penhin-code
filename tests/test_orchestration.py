import json
import os
import subprocess
import sys
import types
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestration.models import AgentJob, AgentRole, Artifact, IntegrationItem, IntegrationItemStatus, IntegrationRun, IntegrationRunStatus, JobStatus
from orchestration.repositories.postgres_repository import PostgresOrchestrationRepository
from orchestration.planning import DAG_PROTOCOL_VERSION, fallback_dag_plan, normalize_dag_plan, parse_dag_plan
from orchestration.service import create_isolated_agent_job, materialize_dag_plan
from orchestration.worker import prepare_dependency_context
from orchestration.worktrees import AgentWorktree
from orchestration.artifacts import build_handoff
from result import Result


@pytest.fixture
def repository() -> PostgresOrchestrationRepository:
    database_url = os.environ.get("PENHIN_DATABASE_URL")
    if not database_url:
        pytest.skip("PENHIN_DATABASE_URL is required for PostgreSQL integration tests")
    repository = PostgresOrchestrationRepository(database_url)
    repository.initialize()
    return repository


def test_postgres_job_lifecycle_records_attempt_artifact_and_events(repository: PostgresOrchestrationRepository) -> None:
    job = repository.create_root_job("inspect repository", "Inspect the repository read-only", AgentRole.EXPLORE)
    attempt = repository.start_attempt(job.id, model="test-model")
    artifact = Artifact(
        id=str(uuid4()),
        job_id=job.id,
        kind="subagent_result",
        content={"summary": "found the runtime", "findings": [], "changed_files": []},
    )

    completed = repository.finish_attempt(attempt.id, JobStatus.SUCCEEDED, artifact=artifact)

    assert completed.status == JobStatus.SUCCEEDED
    assert completed.result_artifact_id == artifact.id
    assert repository.get_artifact(artifact.id).content["summary"] == "found the runtime"
    assert [event.event_type for event in repository.list_events(job.id)] == [
        "job_created",
        "job_started",
        "job_succeeded",
    ]


def test_postgres_rejects_non_readonly_stage_one_job(repository: PostgresOrchestrationRepository) -> None:
    job_id = str(uuid4())
    with pytest.raises(ValueError, match="readonly"):
        repository.create_job(AgentJob(
            id=job_id,
            root_task_id=job_id,
            role=AgentRole.IMPLEMENT,
            subject="unsafe write",
            instruction="write files",
            workspace_mode="shared-write",
        ))


def test_postgres_rejects_invalid_terminal_transition(repository: PostgresOrchestrationRepository) -> None:
    job = repository.create_root_job("inspect", "inspect", AgentRole.EXPLORE)
    with pytest.raises(ValueError, match="Cannot start"):
        repository.start_attempt(job.id)
        repository.start_attempt(job.id)


def test_isolated_agent_job_gets_own_worktree_and_branch(repository: PostgresOrchestrationRepository) -> None:
    job = create_isolated_agent_job(repository, "inspect service boundaries", "explore")
    worktree = Path(job.worktree_path)
    try:
        assert job.workspace_mode == "readonly"
        assert worktree.is_dir()
        assert (worktree / "README.md").exists()
        assert job.worktree_branch.startswith("penhin/agent-")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], check=True)
        subprocess.run(["git", "branch", "-D", job.worktree_branch], check=True)


def test_runtime_builds_valid_handoff_from_plain_text_and_tool_results() -> None:
    content = build_handoff(
        "Found the relevant validation path.",
        producer={"job_id": "job-1", "role": "explore"},
        tool_results=[{"tool_name": "read", "content": '{"ok": true, "message": "read file"}'}],
    )

    assert content["protocol_valid"] is True
    assert content["summary"] == "Found the relevant validation path."
    assert content["commands_run"] == [{"command": "tool:read", "outcome": "passed", "detail": "read file"}]


def test_dag_protocol_accepts_dependencies_and_rejects_cycles() -> None:
    valid = {
        "protocol_version": DAG_PROTOCOL_VERSION,
        "goal": "Implement the feature",
        "jobs": [
            {"key": "inspect", "agent_type": "explore", "instruction": "Inspect code", "depends_on": []},
            {"key": "implement", "agent_type": "general", "instruction": "Implement change", "depends_on": ["inspect"], "priority": 2},
        ],
        "final_job_keys": ["implement"],
    }
    parsed, errors = parse_dag_plan(json.dumps(valid))
    assert parsed == valid
    assert errors == []

    valid["jobs"][0]["depends_on"] = ["implement"]
    _, errors = parse_dag_plan(json.dumps(valid))
    assert errors == ["job dependencies must be acyclic"]


def test_dag_protocol_rejects_unknown_fields_and_unbounded_timeout() -> None:
    plan = {
        "protocol_version": DAG_PROTOCOL_VERSION, "goal": "Inspect",
        "jobs": [{
            "key": "inspect", "agent_type": "explore", "instruction": "Inspect",
            "depends_on": [], "timeout_seconds": 3601, "benchmark_answer": "special-case",
        }],
        "final_job_keys": ["inspect"], "fixture_name": "known-case",
    }
    _, errors = parse_dag_plan(json.dumps(plan))
    assert "unknown top-level fields: fixture_name" in errors
    assert "jobs[0] has unknown fields: benchmark_answer" in errors
    assert "jobs[0].timeout_seconds must be an integer between 1 and 3600" in errors


def test_dag_protocol_recovers_embedded_json_and_has_safe_fallback() -> None:
    valid = {
        "protocol_version": DAG_PROTOCOL_VERSION,
        "goal": "Inspect in parallel",
        "jobs": [{"key": "inspect", "agent_type": "explore", "instruction": "Inspect", "depends_on": []}],
        "final_job_keys": ["inspect"],
    }
    parsed, errors = parse_dag_plan("Here is the plan:\n" + json.dumps(valid) + "\nDone.")
    assert parsed == valid
    assert errors == []
    fallback = fallback_dag_plan("Fix subtract and verify it")
    assert [job["agent_type"] for job in fallback["jobs"]] == ["explore", "verification"]
    assert fallback["final_job_keys"] == ["verify"]
    assert all(job["agent_type"] != "general" for job in fallback["jobs"])
    implementation_plan = {
        "protocol_version": DAG_PROTOCOL_VERSION, "goal": "Fix it",
        "jobs": [{"key": "implement", "agent_type": "general", "instruction": "Implement", "depends_on": []}],
        "final_job_keys": ["implement"],
    }
    normalized, changes = normalize_dag_plan(implementation_plan, "任意语言和措辞")
    assert [job["agent_type"] for job in normalized["jobs"]] == ["general", "verification"]
    assert normalized["jobs"][-1]["depends_on"] == ["implement"]
    assert changes


def test_plan_normalization_does_not_infer_permissions_from_prompt_keywords() -> None:
    plan = {
        "protocol_version": DAG_PROTOCOL_VERSION, "goal": "Review",
        "jobs": [{"key": "inspect", "agent_type": "explore", "instruction": "Inspect", "depends_on": []}],
        "final_job_keys": ["inspect"],
    }
    for wording in ("Fix this", "修改这个问题", "réparer ce défaut", "plan an implementation"):
        normalized, changes = normalize_dag_plan(plan, wording)
        assert normalized == plan
        assert changes == []


def test_dependency_commit_propagation_is_repository_agnostic(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "unrelated_module.rs").write_text("pub fn value() -> i32 { 1 }\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "unrelated_module.rs").write_text("pub fn value() -> i32 { 2 }\n", encoding="utf-8")
    git("commit", "-q", "-am", "dependency")
    dependency_commit = git("rev-parse", "HEAD")
    git("reset", "--hard", base)

    dependency = SimpleNamespace(
        id="dependency", status=JobStatus.SUCCEEDED, depends_on=[], result_artifact_id="artifact",
        role=AgentRole.GENERAL,
    )
    artifact = Artifact(
        id="artifact", job_id="dependency", kind="agent_handoff.v1", schema_valid=True,
        content={
            "summary": "Updated an arbitrary Rust module", "findings": [], "risks": [],
            "changed_files": [{"path": "unrelated_module.rs"}],
            "change_set": {"commits": [dependency_commit]},
        },
    )
    current = SimpleNamespace(
        id="current", root_task_id="root", depends_on=["dependency"], worktree_path=str(tmp_path),
    )

    class Repository:
        def get_job(self, job_id: str):
            return dependency if job_id == "dependency" else None

        def get_artifact(self, artifact_id: str):
            return artifact if artifact_id == "artifact" else None

    context, commits = prepare_dependency_context(Repository(), current)
    assert commits == [dependency_commit]
    assert context[0]["summary"] == "Updated an arbitrary Rust module"
    assert "{ 2 }" in (tmp_path / "unrelated_module.rs").read_text(encoding="utf-8")


def test_materialized_dag_uses_persistent_dependency_ids(
    repository: PostgresOrchestrationRepository, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "orchestration.service.provision_worktree",
        lambda job_id: AgentWorktree(path=f"/tmp/penhin-test-{job_id}", branch=f"penhin/test-{job_id[:8]}"),
    )
    planner = repository.create_root_job("plan", "plan", AgentRole.PLANNER)
    plan = {
        "protocol_version": DAG_PROTOCOL_VERSION,
        "goal": "Implement the feature",
        "jobs": [
            {"key": "inspect", "agent_type": "explore", "instruction": "Inspect code", "depends_on": []},
            {"key": "implement", "agent_type": "general", "instruction": "Implement change", "depends_on": ["inspect"]},
        ],
        "final_job_keys": ["implement"],
    }

    materialized = materialize_dag_plan(repository, planner.id, plan)
    inspect = repository.get_job(materialized["job_ids"]["inspect"])
    implement = repository.get_job(materialized["job_ids"]["implement"])

    assert inspect.root_task_id == planner.id
    assert implement.root_task_id == planner.id
    assert implement.depends_on == [inspect.id]
    assert implement.workspace_mode == "isolated_write"
    repository.request_cancel(inspect.id)
    repository.request_cancel(implement.id)


def test_postgres_claim_only_releases_dag_node_after_all_dependencies_succeed(
    repository: PostgresOrchestrationRepository,
) -> None:
    parent_id = str(uuid4())
    parent = repository.create_job(AgentJob(
        id=parent_id, root_task_id=parent_id, role=AgentRole.EXPLORE, subject="parent", instruction="parent",
        priority=100000, worktree_path="/tmp/parent", worktree_branch="penhin/test-parent",
    ))
    child = repository.create_job(AgentJob(
        id=str(uuid4()), root_task_id=parent.id, parent_id=parent.id, role=AgentRole.GENERAL,
        subject="child", instruction="child", depends_on=[parent.id], workspace_mode="isolated_write",
        priority=100000, worktree_path="/tmp/child", worktree_branch="penhin/test-child",
    ))

    parent_claim = repository.claim_next_job()
    assert parent_claim is not None
    assert parent_claim[0].id == parent.id
    # The only claimable job is the parent; completing it releases its dependent child.
    repository.finish_attempt(
        parent_claim[1].id,
        JobStatus.SUCCEEDED,
        artifact=Artifact(id=str(uuid4()), job_id=parent.id, kind="test", content={}),
    )
    claimed = repository.claim_next_job()
    assert claimed is not None
    assert claimed[0].id == child.id
    child_attempt = claimed[1]
    repository.finish_attempt(child_attempt.id, JobStatus.FAILED, error="test cleanup")


def test_postgres_records_integration_run_and_ordered_items(repository: PostgresOrchestrationRepository) -> None:
    root = repository.create_root_task("integration root", "integration root")
    source = repository.create_job(AgentJob(
        id=str(uuid4()), root_task_id=root.id, parent_id=root.id, role=AgentRole.GENERAL,
        subject="source", instruction="source", workspace_mode="isolated_write",
        worktree_path="/tmp/source", worktree_branch="penhin/source",
    ))
    run = IntegrationRun(
        id=str(uuid4()), root_task_id=root.id, base_commit="a" * 40,
        worktree_path="/tmp/integration", worktree_branch="penhin/integration-test",
    )
    item = IntegrationItem(
        id=str(uuid4()), run_id=run.id, job_id=source.id, ordinal=0,
        source_branch=source.worktree_branch, commits=["b" * 40],
    )

    stored = repository.create_integration_run(run, [item])
    repository.transition_integration_item(item.id, IntegrationItemStatus.APPLYING)
    repository.transition_integration_item(item.id, IntegrationItemStatus.APPLIED)
    repository.transition_integration_run(run.id, IntegrationRunStatus.APPLYING)
    repository.transition_integration_run(run.id, IntegrationRunStatus.INTEGRATED, result_commit="c" * 40)

    assert stored.status == IntegrationRunStatus.CREATED
    assert repository.get_integration_run(run.id).result_commit == "c" * 40
    assert repository.list_integration_items(run.id)[0].commits == ["b" * 40]
    assert repository.list_integration_items(run.id)[0].status == IntegrationItemStatus.APPLIED


def test_postgres_rejects_invalid_integration_transitions(repository: PostgresOrchestrationRepository) -> None:
    root = repository.create_root_task("integration transitions", "integration transitions")
    run = repository.create_integration_run(IntegrationRun(
        id=str(uuid4()), root_task_id=root.id, base_commit="a" * 40,
        worktree_path="/tmp/integration", worktree_branch="penhin/integration-transitions",
    ), [])

    with pytest.raises(ValueError, match="Cannot transition integration run"):
        repository.transition_integration_run(run.id, IntegrationRunStatus.VERIFIED)


def test_worker_wraps_plain_text_result_in_runtime_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestration import worker

    job = AgentJob(
        id=str(uuid4()), root_task_id=str(uuid4()), role=AgentRole.EXPLORE, subject="invalid", instruction="invalid",
        worktree_path=str(Path.cwd()), worktree_branch="test",
    )

    class Repository:
        def initialize(self):
            pass

        def register_worker_pid(self, *args):
            pass

        def get_job(self, _job_id):
            return job

        def finish_attempt(self, *args, **kwargs):
            self.finished = (args, kwargs)

    repository = Repository()
    monkeypatch.setattr(worker, "repository_from_database_url", lambda _url: repository)
    monkeypatch.setattr(worker, "parse_args", lambda: Namespace(database_url="postgresql://test", job_id=job.id, attempt_id="attempt", worker_token="token"))
    monkeypatch.setattr(worker, "init_runtime", lambda: None)
    monkeypatch.setitem(sys.modules, "subagent", types.SimpleNamespace(run_subagent=lambda *_args, **_kwargs: Result.success('{"summary":"invalid"}')))

    assert worker.main() == 0
    args, kwargs = repository.finished
    assert args[1] == JobStatus.SUCCEEDED
    assert kwargs["terminal_reason"] == "completed"
    assert kwargs["artifact"].schema_valid is True
    assert kwargs["artifact"].content["summary"] == '{"summary":"invalid"}'
