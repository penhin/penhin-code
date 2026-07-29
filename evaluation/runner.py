from __future__ import annotations

import os
import hashlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .budget import BudgetExceeded
from .cases import load_suite
from .config import EvaluationConfig
from .grader import diff_summary, grade_case
from .io import read_json, write_json
from .judge import run_judge
from .metrics import metrics_from_events
from .models import EvaluationCase, EvaluationResult
from .observer import EvaluationObserver, read_events, observing
from .shared_budget import SharedBudget


PRODUCT_ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = PRODUCT_ROOT / ".benchmarks" / "runs"


@contextmanager
def evaluation_environment(run_dir: Path, run_id: str, config: EvaluationConfig):
    values = {
        "PENHIN_EVAL_RUN_DIR": str(run_dir), "PENHIN_EVAL_RUN_ID": run_id,
        "PENHIN_EVAL_BUDGET_FILE": str(run_dir / "budget.json"),
        "PENHIN_EVAL_MAX_TOTAL_TOKENS": str(config.max_total_tokens), "PENHIN_EVAL_MAX_USD": str(config.max_usd),
        "PENHIN_EVAL_MAX_JUDGE_TOKENS": str(config.max_judge_tokens),
    }
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _git(workdir: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=workdir, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def product_status() -> str:
    return _git(PRODUCT_ROOT, "status", "--porcelain", "--untracked-files=all")


def product_fingerprint() -> str:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=PRODUCT_ROOT, capture_output=True, timeout=30, check=True,
    )
    digest = hashlib.sha256()
    for raw_path in sorted(item for item in result.stdout.split(b"\0") if item):
        path = PRODUCT_ROOT / os.fsdecode(raw_path)
        digest.update(raw_path + b"\0")
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def prepare_fixture(case: EvaluationCase, suite: str, target: Path) -> None:
    source = Path(__file__).parent / "suites" / suite / case.fixture
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    _git(target, "init", "-q")
    _git(target, "config", "user.email", "eval@penhin.invalid")
    _git(target, "config", "user.name", "Penhin Eval")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "evaluation fixture")


def _case_events(run_dir: Path, case: EvaluationCase, repetition: int) -> list[dict]:
    return [event for event in read_events(run_dir) if event.get("case_id") == case.id and event.get("repetition") == repetition]


def execute_case(run_dir: Path, run_id: str, suite: str, case: EvaluationCase, repetition: int, config: EvaluationConfig) -> EvaluationResult:
    result = EvaluationResult(run_id=run_id, case_id=case.id, repetition=repetition, layer=case.layer, category=case.category, status="running")
    with tempfile.TemporaryDirectory(prefix=f"penhin-eval-{case.id}-") as temp_name:
        workdir = Path(temp_name) / "repo"
        prepare_fixture(case, suite, workdir)
        case_file = run_dir / "case_inputs" / f"{case.id}-{repetition}.json"
        worker_output = run_dir / "worker_outputs" / f"{case.id}-{repetition}.json"
        write_json(case_file, asdict(case))
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(PRODUCT_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
            "PENHIN_EVAL_RUN_DIR": str(run_dir), "PENHIN_EVAL_RUN_ID": run_id,
            "PENHIN_EVAL_CASE_ID": case.id, "PENHIN_EVAL_REPETITION": str(repetition),
            "PENHIN_TRACE_ID": f"{run_id}:{case.id}:{repetition}",
            "PENHIN_EVAL_BUDGET_CASE_KEY": f"{case.id}:{repetition}",
            "PENHIN_EVAL_BUDGET_FILE": str(run_dir / "budget.json"),
            "PENHIN_EVAL_MAX_TOTAL_TOKENS": str(config.max_total_tokens), "PENHIN_EVAL_MAX_USD": str(config.max_usd),
            "PENHIN_DATABASE_URL": f"sqlite:///{workdir / '.penhin' / 'evaluation.sqlite3'}",
            "PENHIN_WORKSPACE_MODE": "isolated_write", "PENHIN_SYNC_AGENT_TIMEOUT_SECONDS": str(case.timeout_seconds),
            "PENHIN_EVAL_CURRENT_CASE_MAX_TOKENS": str(config.max_multi_agent_tokens if case.layer == "multi_agent" else config.max_case_tokens),
            "PENHIN_EVAL_MAX_JUDGE_TOKENS": str(config.max_judge_tokens),
        })
        started = time.perf_counter()
        process = subprocess.Popen(
            [sys.executable, "-m", "evaluation.case_worker", "--case", str(case_file), "--output", str(worker_output)],
            cwd=workdir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=case.timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            result.status, result.error = "timed_out", f"case exceeded {case.timeout_seconds}s"
        else:
            if worker_output.exists():
                worker = read_json(worker_output)
                result.final_answer = worker.get("message", "")
                result.error = worker.get("error", "")
                result.status = "completed" if worker.get("ok") else ("budget_stopped" if worker.get("meta", {}).get("error_type") == "BudgetExceeded" else "failed")
                result.metrics.update(worker.get("meta", {}))
            else:
                result.status, result.error = "crashed", (stderr or stdout or f"worker exit={process.returncode}")[-4000:]
        result.metrics["end_to_end_ms"] = (time.perf_counter() - started) * 1000
        result.metrics["execution_status"] = result.status
        checks, changed, violations = grade_case(case, workdir)
        result.checks = checks
        result.changed_files = changed
        result.safety_violations = violations
        result.diff_summary = diff_summary(workdir)
        result.completed = result.status in {"completed", "failed", "timed_out", "crashed"}
        result.deterministic_passed = result.status == "completed" and all(check.passed for check in checks) and not violations
        events = _case_events(run_dir, case, repetition)
        result.metrics.update(metrics_from_events(events, case.expected_tools))
        if result.status == "completed":
            try:
                observer = EvaluationObserver(run_dir, run_id, case.id, repetition)
                with observing(observer):
                    result.judge = run_judge(case, result.final_answer, result.diff_summary, [asdict(check) for check in checks], f"{case.id}:{repetition}")
            except BudgetExceeded as error:
                result.judge_error = str(error)
                result.status = "budget_stopped"
            except Exception as error:
                result.judge_error = str(error)
        else:
            result.judge_error = "judge not run for non-completed execution"
        return result


def _new_manifest(run_id: str, suite: str, repetitions: int, cases: list[EvaluationCase], config: EvaluationConfig) -> dict:
    return {
        "schema_version": "penhin.eval.run/v1", "run_id": run_id, "suite": suite,
        "suite_case_ids": [case.id for case in cases], "repetitions": repetitions,
        "planned_runs": len(cases) * repetitions, "created_at_ns": time.time_ns(),
        "code_commit": _git(PRODUCT_ROOT, "rev-parse", "HEAD"), "config": config.public_dict(),
        "status": "running", "product_status_before": product_status(),
        "product_fingerprint_before": product_fingerprint(),
    }


def run_suite(
    suite: str,
    repetitions: int,
    config: EvaluationConfig,
    resume: str = "",
    case_ids: list[str] | None = None,
) -> Path:
    all_cases = load_suite(suite)
    selected_ids = case_ids or [case.id for case in all_cases]
    unknown_ids = sorted(set(selected_ids) - {case.id for case in all_cases})
    if unknown_ids:
        raise ValueError(f"unknown evaluation case ids: {', '.join(unknown_ids)}")
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("evaluation case ids must not contain duplicates")
    selected = set(selected_ids)
    cases = [case for case in all_cases if case.id in selected]
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    if resume:
        run_dir = (RUNS_ROOT / resume).resolve()
        if not run_dir.is_relative_to(RUNS_ROOT.resolve()) or not (run_dir / "manifest.json").is_file():
            raise ValueError(f"evaluation run not found: {resume}")
        manifest = read_json(run_dir / "manifest.json")
        if manifest["suite"] != suite or manifest["repetitions"] != repetitions:
            raise ValueError("resume suite and repetitions must match the original run")
        if manifest["suite_case_ids"] != [case.id for case in cases]:
            raise ValueError("resume case selection must match the original run")
        current_public = config.public_dict()
        immutable = {"provider", "model_id_hash", "judge_provider", "judge_model_id_hash", "primary_price", "judge_price"}
        if any(manifest["config"].get(field) != current_public.get(field) for field in immutable):
            raise ValueError("resume requires the original providers, models, and prices")
        run_id = resume
    else:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
        run_dir = RUNS_ROOT / run_id
        manifest = _new_manifest(run_id, suite, repetitions, cases, config)
        write_json(run_dir / "manifest.json", manifest)
    budget = SharedBudget(run_dir / "budget.json", config.max_total_tokens, config.max_usd)
    budget.update_limits(config.max_total_tokens, config.max_usd)
    budget.release_stale()
    existing_results = {path.stem: read_json(path) for path in (run_dir / "results").glob("*.json")}
    completed_keys = {key for key, value in existing_results.items() if value.get("status") != "budget_stopped"}
    budget_stopped = False
    pending: list[tuple[EvaluationCase, int]] = []
    with evaluation_environment(run_dir, run_id, config):
        for case in cases:
            for repetition in range(1, repetitions + 1):
                key = f"{case.id}-{repetition}"
                if key in completed_keys:
                    continue
                previous = existing_results.get(key)
                if previous and previous.get("status") == "budget_stopped" and previous.get("completed"):
                    try:
                        observer = EvaluationObserver(run_dir, run_id, case.id, repetition)
                        with observing(observer):
                            previous["judge"] = run_judge(case, previous.get("final_answer", ""), previous.get("diff_summary", ""), previous.get("checks", []), f"{case.id}:{repetition}").to_dict()
                        previous["judge_error"] = ""
                        previous["status"] = previous.get("metrics", {}).get("execution_status", "completed")
                        write_json(run_dir / "results" / f"{key}.json", previous)
                        continue
                    except BudgetExceeded:
                        budget_stopped = True
                        break
                pending.append((case, repetition))
            if budget_stopped:
                break
        if not budget_stopped and config.workers == 1:
            for case, repetition in pending:
                try:
                    result = execute_case(run_dir, run_id, suite, case, repetition, config)
                except BudgetExceeded:
                    budget_stopped = True
                    break
                write_json(run_dir / "results" / f"{case.id}-{repetition}.json", result.to_dict())
                if result.status == "budget_stopped":
                    budget_stopped = True
                    break
        elif not budget_stopped and pending:
            iterator = iter(pending)
            with ProcessPoolExecutor(max_workers=min(config.workers, len(pending))) as executor:
                active = {}

                def submit_next() -> bool:
                    try:
                        case, repetition = next(iterator)
                    except StopIteration:
                        return False
                    future = executor.submit(execute_case, run_dir, run_id, suite, case, repetition, config)
                    active[future] = (case, repetition)
                    return True

                for _ in range(min(config.workers, len(pending))):
                    submit_next()
                while active:
                    done, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in done:
                        case, repetition = active.pop(future)
                        try:
                            result = future.result()
                        except BudgetExceeded:
                            budget_stopped = True
                            continue
                        except Exception as error:
                            result = EvaluationResult(
                                run_id=run_id, case_id=case.id, repetition=repetition,
                                layer=case.layer, category=case.category, status="crashed",
                                completed=True, error=f"evaluation worker crashed: {type(error).__name__}: {error}",
                            )
                        write_json(run_dir / "results" / f"{case.id}-{repetition}.json", result.to_dict())
                        if result.status == "budget_stopped":
                            budget_stopped = True
                    while not budget_stopped and len(active) < config.workers and submit_next():
                        pass
    actual_results = list((run_dir / "results").glob("*.json"))
    status_after = product_status()
    fingerprint_after = product_fingerprint()
    manifest.update({
        "status": "budget_stopped" if budget_stopped else ("complete" if len(actual_results) == manifest["planned_runs"] else "incomplete"),
        "completed_runs": len(actual_results), "budget": budget.snapshot(), "finished_at_ns": time.time_ns(),
        "product_status_after": status_after, "product_fingerprint_after": fingerprint_after,
        "product_repository_unchanged": status_after == manifest["product_status_before"] and fingerprint_after == manifest["product_fingerprint_before"],
    })
    write_json(run_dir / "manifest.json", manifest)
    from .report import generate_report
    generate_report(run_dir)
    return run_dir
