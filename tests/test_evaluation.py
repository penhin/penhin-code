from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from penhin.evaluation.budget import BudgetExceeded, ModelPrice
from penhin.evaluation.cases import load_suite, parse_case
from penhin.evaluation.cli import baseline_eligibility_errors
from penhin.evaluation.config import EvaluationConfig
from penhin.evaluation.grader import grade_case
from penhin.infrastructure.atomic_io import read_json, write_safe_json_atomic as write_json
from penhin.evaluation.judge import judge_payload, parse_judge_score
from penhin.evaluation.metrics import metrics_from_events, orchestration_metrics_from_events, percentile, stability_by_case
from penhin.evaluation.models import CASE_SCHEMA_VERSION, EvaluationCase, EvaluationResult
from penhin.evaluation.observer import EvaluationObserver, read_events
from penhin.evaluation.observer import observing
from penhin.evaluation.report import compare_reports, generate_report
from penhin.evaluation.shared_budget import SharedBudget
from penhin.evaluation.runner import shared_budget_exceeded
from penhin.evaluation.trace import build_trace_summary
from penhin.providers.protocols import LLMResponse, LLMUsage


def case_data(fixture: str = "fixture") -> dict:
    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "id": "test-case",
        "layer": "main",
        "category": "test",
        "prompt": "Do the task",
        "fixture": fixture,
        "timeout_seconds": 30,
    }


def test_baseline_suite_has_expected_layer_distribution() -> None:
    cases = load_suite("baseline-v1")
    assert len(cases) == 30
    assert sum(case.layer == "main" for case in cases) == 16
    assert sum(case.layer == "subagent" for case in cases) == 8
    assert sum(case.layer == "multi_agent" for case in cases) == 6


def test_baseline_requires_full_suite_with_three_repetitions() -> None:
    case_ids = [f"case-{index}" for index in range(30)]
    report = {
        "complete": True, "safety_violations": 0, "product_repository_unchanged": True,
        "budget": {"used_input_tokens": 10, "used_output_tokens": 10, "max_tokens": 100, "used_usd": 0.1, "max_usd": 1},
    }
    valid = {"suite_case_ids": case_ids, "repetitions": 3, "planned_runs": 90}
    assert baseline_eligibility_errors(valid, report, case_ids) == []
    smoke = {"suite_case_ids": case_ids[:3], "repetitions": 1, "planned_runs": 3}
    assert baseline_eligibility_errors(smoke, report, case_ids)


def test_fixture_preparation_excludes_runtime_caches(tmp_path: Path) -> None:
    from penhin.evaluation.runner import prepare_fixture
    case = load_suite("baseline-v1")[0]
    prepare_fixture(case, "baseline-v1", tmp_path / "repo")
    tracked = subprocess.run(["git", "ls-files"], cwd=tmp_path / "repo", capture_output=True, text=True, check=True).stdout
    assert "__pycache__" not in tracked
    assert ".pyc" not in tracked


def test_product_fingerprint_detects_content_change_without_status_shape_change(tmp_path: Path) -> None:
    import penhin.evaluation.runner as runner
    init_repo(tmp_path)
    original_root = runner.PRODUCT_ROOT
    try:
        runner.PRODUCT_ROOT = tmp_path
        (tmp_path / "allowed.txt").write_text("dirty-one\n", encoding="utf-8")
        before_status, before = runner.product_status(), runner.product_fingerprint()
        (tmp_path / "allowed.txt").write_text("dirty-two\n", encoding="utf-8")
        assert runner.product_status() == before_status
        assert runner.product_fingerprint() != before
    finally:
        runner.PRODUCT_ROOT = original_root


def test_case_schema_rejects_unknown_fields_and_unsafe_paths(tmp_path: Path) -> None:
    (tmp_path / "fixture").mkdir()
    unknown = case_data() | {"surprise": True}
    with pytest.raises(ValueError, match="unknown case fields"):
        parse_case(unknown, tmp_path)
    unsafe = case_data("../fixture")
    with pytest.raises(ValueError, match="safe relative path"):
        parse_case(unsafe, tmp_path)


def test_case_schema_requires_role_for_subagent(tmp_path: Path) -> None:
    (tmp_path / "fixture").mkdir()
    data = case_data() | {"layer": "subagent"}
    with pytest.raises(ValueError, match="requires agent_role"):
        parse_case(data, tmp_path)


def test_case_schema_validates_fixture_driven_orchestration_plan(tmp_path: Path) -> None:
    (tmp_path / "fixture").mkdir()
    plan = {
        "protocol_version": "penhin.dag/v1", "goal": "Exercise recovery",
        "jobs": [{"key": "inspect", "agent_type": "explore", "instruction": "Inspect", "depends_on": []}],
        "final_job_keys": ["inspect"],
    }
    parsed = parse_case(case_data() | {"layer": "multi_agent", "orchestration_plan": plan}, tmp_path)
    assert parsed.orchestration_plan == plan
    with pytest.raises(ValueError, match="only valid for multi_agent"):
        parse_case(case_data() | {"orchestration_plan": plan}, tmp_path)
    with pytest.raises(ValueError, match="invalid orchestration_plan"):
        parse_case(case_data() | {"layer": "multi_agent", "orchestration_plan": plan | {"final_job_keys": ["missing"]}}, tmp_path)


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "allowed.txt").write_text("before\n", encoding="utf-8")
    (path / "forbidden.txt").write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=path, check=True)


def test_grader_checks_commands_content_and_change_scope(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "allowed.txt").write_text("expected\n", encoding="utf-8")
    case = EvaluationCase(
        CASE_SCHEMA_VERSION, "grade-case", "main", "test", "prompt", "fixture", 30,
        content_checks=(), allowed_paths=("allowed.txt",), forbidden_paths=("forbidden.txt",),
    )
    checks, changed, violations = grade_case(case, tmp_path)
    assert changed == ["allowed.txt"]
    assert all(check.passed for check in checks)
    assert violations == []
    (tmp_path / "forbidden.txt").write_text("changed\n", encoding="utf-8")
    _, _, violations = grade_case(case, tmp_path)
    assert any("forbidden change" in item for item in violations)


def test_grader_ignores_evaluation_infrastructure(tmp_path: Path) -> None:
    init_repo(tmp_path)
    infrastructure = tmp_path / ".penhin"
    infrastructure.mkdir()
    (infrastructure / "penhin.evaluation.sqlite3").write_text("state", encoding="utf-8")
    tasks = tmp_path / ".tasks"
    tasks.mkdir()
    (tasks / "current.json").write_text("{}", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-312.pyc").write_bytes(b"cache")
    (tmp_path / ".penhin_todos.json").write_text("[]", encoding="utf-8")
    case = EvaluationCase(
        CASE_SCHEMA_VERSION, "infra", "main", "test", "prompt", "fixture", 30,
        allowed_paths=("__no_changes__",),
    )

    checks, changed, violations = grade_case(case, tmp_path)

    assert changed == []
    assert violations == []
    assert all(check.passed for check in checks)


def test_grader_includes_committed_changes_from_integration_worktree(tmp_path: Path) -> None:
    init_repo(tmp_path)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    (tmp_path / "allowed.txt").write_text("integrated\n", encoding="utf-8")
    subprocess.run(["git", "add", "allowed.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "integrated change"], cwd=tmp_path, check=True)
    case = EvaluationCase(
        CASE_SCHEMA_VERSION, "integrated", "multi_agent", "test", "prompt", "fixture", 30,
        allowed_paths=("allowed.txt",),
    )
    checks, changed, violations = grade_case(case, tmp_path, base)
    assert changed == ["allowed.txt"]
    assert violations == []
    assert all(check.passed for check in checks)


def test_observer_redacts_secrets_but_preserves_token_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMPLE_API_KEY", "secret-value-123")
    observer = EvaluationObserver(tmp_path, "run", "case", 1)
    observer.emit("llm_call_completed", usage={"input_tokens": 12, "output_tokens": 3}, detail="secret-value-123")
    event = read_events(tmp_path)[0]
    assert event["payload"]["usage"]["input_tokens"] == 12
    assert event["payload"]["detail"] == "<redacted>"


def test_observer_adds_cross_process_correlation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PENHIN_TRACE_ID", "trace-1")
    monkeypatch.setenv("PENHIN_ROOT_TASK_ID", "root-1")
    monkeypatch.setenv("PENHIN_JOB_ID", "job-1")
    monkeypatch.setenv("PENHIN_ATTEMPT_ID", "attempt-1")
    EvaluationObserver(tmp_path, "run", "case", 1).emit("orchestration_test")
    event = read_events(tmp_path)[0]
    assert event["schema_version"] == "penhin.eval.event/v2"
    assert event["event_id"]
    assert event["correlation"] == {
        "trace_id": "trace-1", "root_task_id": "root-1",
        "job_id": "job-1", "attempt_id": "attempt-1",
    }


def test_runtime_emits_llm_usage_and_first_token_latency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from penhin.runtime import manager as runtime
    requests = []

    class Provider:
        retry_errors = ()

        def stream_message(self, request, callback):
            requests.append(request)
            callback("hello")
            return LLMResponse([{"type": "text", "text": "hello"}], "end_turn", LLMUsage(11, 4))

    monkeypatch.delenv("PENHIN_EVAL_BUDGET_FILE", raising=False)
    observer = EvaluationObserver(tmp_path, "run", "case", 1)
    with observing(observer):
        response = runtime.Runtime(Provider(), "model", thinking_level="max").call_with_retry(
            "system", [], stream_callback=lambda _text: None,
        )
    assert response.usage.input_tokens == 11
    assert requests[0].thinking_level == "max"
    completed = [event for event in read_events(tmp_path) if event["event_type"] == "llm_call_completed"][0]
    assert completed["payload"]["usage"]["output_tokens"] == 4
    assert completed["payload"]["first_token_ms"] is not None


def test_write_json_redacts_secret_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_PASSWORD", "do-not-store")
    path = tmp_path / "value.json"
    write_json(path, {"answer": "prefix do-not-store suffix", "input_tokens": 9})
    text = path.read_text(encoding="utf-8")
    assert "do-not-store" not in text
    assert read_json(path)["input_tokens"] == 9


def test_shared_budget_reserves_settles_and_enforces_limits(tmp_path: Path) -> None:
    budget = SharedBudget(tmp_path / "budget.json", 100, 1.0)
    price = ModelPrice(1.0, 2.0)
    reservation = budget.reserve(10, 20, price, "primary", "case", 50)
    with pytest.raises(BudgetExceeded, match="primary token budget"):
        budget.reserve(10, 20, price, "primary", "case", 50)
    budget.settle(reservation, 8, 4, price)
    snapshot = budget.snapshot()
    assert snapshot["used_input_tokens"] == 8
    assert snapshot["case_tokens"]["case"] == 12
    assert snapshot["reservations"] == {}


def test_shared_budget_releases_dead_process_reservations(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    budget = SharedBudget(path, 100, 1.0)
    reservation = budget.reserve(10, 20, ModelPrice(1.0, 2.0), "primary", "case")
    state = json.loads(path.read_text(encoding="utf-8"))
    state["reservations"][reservation]["pid"] = 2_147_483_647
    path.write_text(json.dumps(state), encoding="utf-8")
    assert budget.release_stale() == 1
    assert budget.snapshot()["reservations"] == {}


def test_only_shared_budget_exhaustion_stops_the_batch() -> None:
    assert shared_budget_exceeded("shared token budget would be exceeded: 101>100") is True
    assert shared_budget_exceeded("shared USD budget would be exceeded: 2>1") is True
    assert shared_budget_exceeded("primary token budget would be exceeded for case") is False
    assert shared_budget_exceeded("judge token budget would be exceeded for case") is False


def test_metrics_capture_tools_tokens_latency_and_stability() -> None:
    events = [
        {"event_type": "llm_call_completed", "payload": {"usage": {"input_tokens": 10, "output_tokens": 2}, "duration_ms": 100}},
        {"event_type": "tool_call_completed", "payload": {"tool_name": "read", "input_digest": "a", "status": "ok", "duration_ms": 5}},
        {"event_type": "tool_call_completed", "payload": {"tool_name": "read", "input_digest": "a", "status": "error", "duration_ms": 15}},
        {"event_type": "llm_retry", "payload": {}},
    ]
    metrics = metrics_from_events(events, ("read", "bash"))
    assert metrics["total_tokens"] == 12
    assert metrics["tool_success_rate"] == 0.5
    assert metrics["duplicate_tool_calls"] == 1
    assert metrics["expected_tool_coverage"] == 0.5
    assert percentile([1, 2, 3, 4], 0.95) == pytest.approx(3.85)
    stability = stability_by_case([
        {"case_id": "a", "deterministic_passed": True, "judge": None},
        {"case_id": "a", "deterministic_passed": False, "judge": None},
    ])
    assert stability["mixed_outcome_case_rate"] == 1.0


def test_orchestration_metrics_and_trace_diagnose_protocol_failure() -> None:
    events = [
        {
            "event_id": "1", "event_type": "orchestration_plan_started", "case_id": "multi",
            "repetition": 1, "monotonic_ns": 1_000_000, "correlation": {"trace_id": "trace"}, "payload": {},
        },
        {
            "event_id": "2", "event_type": "orchestration_job_created", "case_id": "multi",
            "repetition": 1, "monotonic_ns": 2_000_000, "correlation": {"trace_id": "trace"},
            "payload": {"root_task_id": "root", "job_id": "planner"},
        },
        {
            "event_id": "3", "event_type": "orchestration_artifact_built", "case_id": "multi",
            "repetition": 1, "monotonic_ns": 3_000_000, "correlation": {"trace_id": "trace"},
            "payload": {"root_task_id": "root", "job_id": "planner", "artifact_id": "artifact", "schema_valid": False},
        },
        {
            "event_id": "4", "event_type": "orchestration_worker_completed", "case_id": "multi",
            "repetition": 1, "monotonic_ns": 4_000_000, "correlation": {"trace_id": "trace"},
            "payload": {"root_task_id": "root", "job_id": "planner", "status": "failed", "stage": "protocol_validation", "error_code": "invalid_protocol", "protocol_errors": ["invalid JSON"]},
        },
        {
            "event_id": "5", "event_type": "orchestration_plan_failed", "case_id": "multi",
            "repetition": 1, "monotonic_ns": 5_000_000, "correlation": {"trace_id": "trace"},
            "payload": {"root_task_id": "root", "stage": "planner_execution", "error_code": "failed"},
        },
    ]
    metrics = orchestration_metrics_from_events(events)
    assert metrics["plans_failed"] == 1
    assert metrics["invalid_artifacts"] == 1
    assert metrics["job_trace_completeness_rate"] == 1.0
    assert metrics["failure_stages"] == {"planner_execution": 1, "protocol_validation": 1}
    trace = build_trace_summary(events, case_id="multi", repetition=1)
    assert trace["orchestration_event_count"] == 5
    assert trace["root_cause"]["error_code"] == "invalid_protocol"
    assert any(item.get("code") == "invalid_protocol" for item in trace["diagnostics"])
    assert trace["timeline"][3]["protocol_errors"] == ["invalid JSON"]


def test_judge_parser_is_strict_and_payload_is_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    score = parse_judge_score(json.dumps({"correctness": 5, "relevance": 4, "evidence": 3, "maintainability": 4, "rationale": "grounded"}))
    assert score.correctness == 5
    with pytest.raises(ValueError, match="invalid fields"):
        parse_judge_score('{"correctness": 5}')
    monkeypatch.setenv("JUDGE_API_KEY", "judge-secret")
    case = EvaluationCase(CASE_SCHEMA_VERSION, "judge-case", "main", "test", "prompt", "fixture", 30)
    payload = judge_payload(case, "answer judge-secret", "", [])
    assert "judge-secret" not in payload
    assert "provider" not in payload and "model" not in payload


def fake_config() -> EvaluationConfig:
    return EvaluationConfig(
        provider="anthropic", model="primary", judge_provider="gemini", judge_model="judge",
        primary_price=ModelPrice(1, 2), judge_price=ModelPrice(0.5, 1),
        max_total_tokens=100000, max_usd=30, max_case_tokens=10000,
        max_multi_agent_tokens=20000, max_judge_tokens=2000, workers=1,
    )


def test_run_suite_resume_does_not_repeat_completed_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import penhin.evaluation.runner as runner
    case = EvaluationCase(CASE_SCHEMA_VERSION, "resume-case", "main", "test", "prompt", "fixture", 30)
    monkeypatch.setattr(runner, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(runner, "load_suite", lambda _suite: [case])
    calls = []

    def execute(_run_dir, run_id, _suite, selected, repetition, _config):
        calls.append((selected.id, repetition))
        return EvaluationResult(run_id=run_id, case_id=selected.id, repetition=repetition, layer="main", category="test", status="completed", completed=True, deterministic_passed=True)

    monkeypatch.setattr(runner, "execute_case", execute)
    first = runner.run_suite("test-suite", 1, fake_config(), case_ids=["resume-case"])
    assert calls == [("resume-case", 1)]
    runner.run_suite("test-suite", 1, fake_config(), resume=first.name, case_ids=["resume-case"])
    assert calls == [("resume-case", 1)]


def test_run_suite_rejects_unknown_case_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import penhin.evaluation.runner as runner
    case = EvaluationCase(CASE_SCHEMA_VERSION, "known-case", "main", "test", "prompt", "fixture", 30)
    monkeypatch.setattr(runner, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(runner, "load_suite", lambda _suite: [case])
    with pytest.raises(ValueError, match="unknown evaluation case"):
        runner.run_suite("test-suite", 1, fake_config(), case_ids=["missing-case"])


def report_fixture(tmp_path: Path, completion: bool = True, p95: float = 100.0) -> dict:
    config = fake_config().public_dict()
    manifest = {
        "run_id": tmp_path.name, "suite": "test", "planned_runs": 1,
        "status": "complete", "product_repository_unchanged": True,
        "config": config, "budget": {},
    }
    write_json(tmp_path / "manifest.json", manifest)
    result = EvaluationResult(run_id=tmp_path.name, case_id="case", repetition=1, layer="main", category="test", status="completed", completed=True, deterministic_passed=completion)
    result.metrics["end_to_end_ms"] = p95
    write_json(tmp_path / "results" / "case-1.json", result.to_dict())
    return generate_report(tmp_path)


def test_report_and_balanced_regression_gates(tmp_path: Path) -> None:
    baseline = report_fixture(tmp_path / "baseline", True, 100)
    current = report_fixture(tmp_path / "current", False, 150)
    comparison = compare_reports(current, baseline)
    assert comparison["passed"] is False
    assert any("overall completion" in failure for failure in comparison["failures"])
    assert any("latency" in failure for failure in comparison["failures"])


def test_report_separates_model_and_fixture_driven_multi_agent_runs(tmp_path: Path) -> None:
    manifest = {
        "run_id": "modes", "suite": "test", "planned_runs": 2, "status": "complete",
        "product_repository_unchanged": True, "config": fake_config().public_dict(), "budget": {},
    }
    write_json(tmp_path / "manifest.json", manifest)
    for index, mode in enumerate(("model_driven", "fixture_driven"), 1):
        result = EvaluationResult(
            run_id="modes", case_id=f"case-{index}", repetition=1, layer="multi_agent",
            category="test", status="completed", completed=True, deterministic_passed=True,
        )
        result.metrics.update({"end_to_end_ms": 1, "orchestration_plan_mode": mode})
        write_json(tmp_path / "results" / f"case-{index}.json", result.to_dict())
    report = generate_report(tmp_path)
    assert report["multi_agent_by_plan_mode"]["model_driven"]["runs"] == 1
    assert report["multi_agent_by_plan_mode"]["fixture_driven"]["runs"] == 1
    assert "reported separately" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_regression_gate_does_not_let_fixture_runs_mask_model_planning_drop() -> None:
    baseline = {
        "task_completion_rate": 1.0, "by_layer": {}, "safety_violations": 0,
        "product_repository_unchanged": True, "statuses": {}, "planned_runs": 20,
        "quality": {field: 5 for field in ("correctness", "relevance", "evidence", "maintainability")},
        "latency": {}, "cost": {"total_usd": 1},
        "multi_agent_by_plan_mode": {
            "model_driven": {"completion_rate": 1.0},
            "fixture_driven": {"completion_rate": 1.0},
        },
    }
    current = {
        **baseline,
        "multi_agent_by_plan_mode": {
            "model_driven": {"completion_rate": 0.66},
            "fixture_driven": {"completion_rate": 1.0},
        },
    }
    comparison = compare_reports(current, baseline)
    assert comparison["passed"] is False
    assert any("model_driven" in failure for failure in comparison["failures"])


def test_eval_cli_validates_built_in_suite() -> None:
    result = subprocess.run(
        [os.fspath(Path(os.sys.executable)), "-m", "penhin.evaluation", "validate", "--suite", "baseline-v1"],
        cwd=Path(__file__).parent.parent, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["cases"] == 30
