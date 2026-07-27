import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from orchestration.models import AgentJob, AgentRole, Artifact, JobStatus
from orchestration.repository import PostgresOrchestrationRepository
from orchestration.planning import DAG_PROTOCOL_VERSION, parse_dag_plan
from orchestration.service import create_isolated_agent_job, materialize_dag_plan
from orchestration.worktrees import AgentWorktree
from orchestration.artifacts import HANDOFF_PROTOCOL_VERSION, normalize_subagent_result


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


def test_handoff_protocol_accepts_complete_structured_payload() -> None:
    text = json.dumps({
        "protocol_version": HANDOFF_PROTOCOL_VERSION,
        "summary": "Repository entrypoint identified.",
        "findings": [{
            "title": "CLI entrypoint", "detail": "main.py owns the interactive loop.", "severity": "info",
            "evidence": [{"path": "main.py", "location": "main", "detail": "defines the CLI loop."}],
        }],
        "commands_run": [{"command": "pytest -q", "outcome": "passed", "detail": "197 tests passed."}],
        "changed_files": [],
        "risks": [],
        "handoff": {"recommended_next_action": "Schedule implementation.", "suggested_roles": ["implement"], "blocking_questions": []},
    })

    content, valid = normalize_subagent_result(text, producer={"job_id": "job-1", "role": "explore"})

    assert valid is True
    assert content["protocol_valid"] is True
    assert content["producer"]["job_id"] == "job-1"


def test_handoff_protocol_rejects_incomplete_payload_without_losing_raw_text() -> None:
    content, valid = normalize_subagent_result('{"summary": "not enough"}')

    assert valid is False
    assert content["protocol_valid"] is False
    assert content["raw_text"] == '{"summary": "not enough"}'


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
