from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import fields
from pathlib import Path
from uuid import uuid4

from agent import agent_loop
from context import RunContext
from evaluation.io import write_json
from evaluation.models import CommandCheck, ContentCheck, EvaluationCase
from evaluation.observer import EvaluationObserver, emit, observing
from result import Result
from runtime import init_runtime
from tool_runtime import runtime_permission_setup


def case_from_dict(data: dict) -> EvaluationCase:
    values = {item.name: data[item.name] for item in fields(EvaluationCase) if item.name in data}
    values["commands"] = tuple(CommandCheck(**item) for item in values.get("commands", []))
    values["content_checks"] = tuple(ContentCheck(**item) for item in values.get("content_checks", []))
    for name in ("allowed_paths", "forbidden_paths", "expected_tools"):
        values[name] = tuple(values.get(name, []))
    return EvaluationCase(**values)


def _assistant_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text").strip()
    return ""


def run_main(case: EvaluationCase) -> Result:
    policy, approval = runtime_permission_setup("full-access")
    context = RunContext(messages=[{"role": "user", "content": case.prompt}], policy=policy, approval=approval)
    state = agent_loop(context)
    return Result.success(_assistant_text(context.messages), turns=state.turn, terminal_reason=str(state.terminal_reason or ""))


def run_child(case: EvaluationCase) -> Result:
    from subagent import run_subagent
    return run_subagent(case.prompt, agent_type=case.agent_role)


def run_multi_agent(case: EvaluationCase) -> Result:
    from orchestration.models import TERMINAL_JOB_STATUSES, AgentRole, Artifact, JobStatus
    from orchestration.planning import validate_dag_plan
    from orchestration.service import create_dag_plan, finalize_dag, materialize_dag_plan, repository_from_env, scheduler_from_env, wait_for_job
    if case.orchestration_plan is None:
        planned = create_dag_plan(case.prompt)
    else:
        errors = validate_dag_plan(case.orchestration_plan)
        if errors:
            return Result.failure("Invalid fixture orchestration plan", code="invalid_fixture_plan", errors=errors)
        repository = repository_from_env()
        planner = repository.create_root_job("Evaluation fixture plan", case.prompt, AgentRole.PLANNER)
        attempt = repository.start_attempt(planner.id, model="evaluation-fixture")
        artifact = Artifact(
            id=str(uuid4()), job_id=planner.id, kind="agent_dag_plan.v1",
            content={"protocol_valid": True, "plan_source": "evaluation_fixture", **case.orchestration_plan},
        )
        repository.finish_attempt(attempt.id, JobStatus.SUCCEEDED, artifact=artifact, terminal_reason="fixture_materialized")
        root_task_id = planner.id
        emit("orchestration_plan_started", root_task_id=root_task_id, planner_job_id=planner.id, plan_source="evaluation_fixture")
        data = materialize_dag_plan(repository, planner.id, case.orchestration_plan)
        scheduler_from_env().dispatch()
        emit(
            "orchestration_plan_validated", root_task_id=root_task_id,
            job_count=len(case.orchestration_plan["jobs"]), plan_source="evaluation_fixture",
        )
        planned = Result.success(json.dumps(data, ensure_ascii=False), data=data)
    if not planned.ok:
        return planned
    repository = repository_from_env()
    if case.scenario in {"invalid_artifact", "timeout_cancel"}:
        deadline = time.monotonic() + min(case.timeout_seconds, 120)
        while time.monotonic() < deadline:
            jobs = repository.list_jobs(planned.data["root_task_id"])
            expected = (
                [job for job in jobs if job.status == JobStatus.FAILED]
                if case.scenario == "invalid_artifact"
                else [job for job in jobs if job.status == JobStatus.TIMED_OUT]
            )
            if expected:
                scheduler = scheduler_from_env()
                for job in jobs:
                    if job.status not in TERMINAL_JOB_STATUSES:
                        scheduler.request_cancel(job.id)
                terminal = [repository.get_job(job.id).to_dict() for job in jobs]
                return Result.success(
                    json.dumps({"fault": case.scenario, "terminal_jobs": terminal}, ensure_ascii=False),
                    final_job_ids=planned.data["final_job_ids"], root_task_id=planned.data["root_task_id"],
                    evaluation_worktree=str(Path.cwd()), recovery_outcome=f"{case.scenario}_handled",
                )
            time.sleep(0.05)
        return Result.failure(f"Timed out waiting for deterministic {case.scenario} fault", code="fault_injection_timeout")
    summaries = []
    final_jobs = []
    final_artifact_ids = []
    for job_id in planned.data["final_job_ids"]:
        outcome = wait_for_job(repository, job_id, case.timeout_seconds)
        if not outcome.ok:
            if case.scenario == "integration_conflict":
                jobs = repository.list_jobs(planned.data["root_task_id"])
                conflict = [job for job in jobs if job.status == JobStatus.FAILED and "cherry-pick" in job.error.lower()]
                if conflict:
                    return Result.success(
                        json.dumps({"conflict_detected": True, "terminal_jobs": [job.to_dict() for job in jobs]}, ensure_ascii=False),
                        final_job_ids=planned.data["final_job_ids"], root_task_id=planned.data["root_task_id"],
                        evaluation_worktree=str(Path.cwd()), recovery_outcome="integration_conflict_detected",
                    )
            return outcome
        summaries.append(outcome.data["artifact"].content.get("summary", ""))
        final_jobs.append(outcome.data["job"])
        final_artifact_ids.append(outcome.data["artifact"].id)
    verification_command = list(case.commands[0].command) if case.commands else None
    integration = finalize_dag(
        repository, planned.data["root_task_id"], planned.data["final_job_ids"], verification_command,
    )
    if not integration.ok:
        return integration
    evaluation_worktree = integration.data["worktree_path"]
    return Result.success(
        "\n\n".join(summaries), final_job_ids=planned.data["final_job_ids"],
        final_artifact_ids=final_artifact_ids, root_task_id=planned.data["root_task_id"],
        evaluation_worktree=evaluation_worktree,
        integration_id=integration.data.get("integration_id"),
        integration_status=integration.data.get("integration_status"),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    case = case_from_dict(json.loads(Path(args.case).read_text(encoding="utf-8")))
    observer = EvaluationObserver(Path(os.environ["PENHIN_EVAL_RUN_DIR"]), os.environ["PENHIN_EVAL_RUN_ID"], case.id, int(os.environ["PENHIN_EVAL_REPETITION"]))
    started = time.perf_counter()
    result: Result
    with observing(observer):
        emit("agent_run_started", layer=case.layer, role=case.agent_role or None, scenario=case.scenario or None)
        try:
            init_runtime()
            if case.layer == "main":
                result = run_main(case)
            elif case.layer == "subagent":
                result = run_child(case)
            else:
                result = run_multi_agent(case)
        except Exception as error:
            result = Result.failure(str(error), code="evaluation_worker_error", error_type=type(error).__name__)
        duration_ms = (time.perf_counter() - started) * 1000
        emit("agent_run_completed", status="ok" if result.ok else "error", duration_ms=duration_ms, code=result.meta.get("code"), **{key: value for key, value in result.meta.items() if key in {"turns", "terminal_reason"}})
    write_json(Path(args.output), result._to_dict() | {"duration_ms": duration_ms})
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
