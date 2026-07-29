from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .cases import load_suite
from .config import load_evaluation_config, offline_preflight
from .io import read_json, write_json
from .report import compare_reports, generate_report
from .runner import RUNS_ROOT, run_suite


def run_dir(run_id: str) -> Path:
    path = (RUNS_ROOT / run_id).resolve()
    if not path.is_relative_to(RUNS_ROOT.resolve()) or not (path / "manifest.json").is_file():
        raise ValueError(f"evaluation run not found: {run_id}")
    return path


def baseline_eligibility_errors(manifest: dict, report: dict, expected_case_ids: list[str]) -> list[str]:
    errors: list[str] = []
    required_repetitions = 3
    if manifest.get("suite_case_ids") != expected_case_ids:
        errors.append("baseline must include every suite case")
    if manifest.get("repetitions") != required_repetitions:
        errors.append(f"baseline requires {required_repetitions} repetitions")
    if manifest.get("planned_runs") != len(expected_case_ids) * required_repetitions:
        errors.append("baseline planned run count is invalid")
    if not report.get("complete"):
        errors.append("baseline run is incomplete")
    if report.get("safety_violations") or not report.get("product_repository_unchanged"):
        errors.append("baseline run failed a safety invariant")
    budget = report.get("budget", {})
    if int(budget.get("used_input_tokens", 0) or 0) + int(budget.get("used_output_tokens", 0) or 0) > int(budget.get("max_tokens", 0) or 0):
        errors.append("baseline exceeded its token budget")
    if float(budget.get("used_usd", 0) or 0) > float(budget.get("max_usd", 0) or 0):
        errors.append("baseline exceeded its cost budget")
    return errors


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="penhin-eval", description="Evaluate Penhin agent capabilities")
    commands = root.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--suite", default="baseline-v1")
    run = commands.add_parser("run")
    run.add_argument("--suite", default="baseline-v1")
    run.add_argument("--repetitions", type=int, default=3)
    run.add_argument("--resume", default="")
    run.add_argument("--case", action="append", dest="case_ids", help="run only the selected case id; repeat for multiple cases")
    report = commands.add_parser("report")
    report.add_argument("run_id")
    compare = commands.add_parser("compare")
    compare.add_argument("run_id")
    compare.add_argument("--baseline", required=True)
    baseline = commands.add_parser("baseline")
    baseline.add_argument("action", choices=("set", "show"))
    baseline.add_argument("run_id", nargs="?")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "validate":
            cases = load_suite(args.suite)
            errors = offline_preflight()
            if errors:
                raise ValueError("; ".join(errors))
            print(json.dumps({"ok": True, "suite": args.suite, "cases": len(cases)}, ensure_ascii=False))
            return 0
        if args.command == "run":
            suite, repetitions = args.suite, args.repetitions
            case_ids = args.case_ids
            if args.resume:
                old = read_json(run_dir(args.resume) / "manifest.json")
                suite, repetitions = old["suite"], old["repetitions"]
                case_ids = old["suite_case_ids"]
            config = load_evaluation_config()
            path = run_suite(suite, repetitions, config, resume=args.resume, case_ids=case_ids)
            report = read_json(path / "report.json")
            print(json.dumps({"run_id": path.name, "complete": report["complete"], "task_completion_rate": report["task_completion_rate"]}, ensure_ascii=False))
            return 0 if report["complete"] else 2
        if args.command == "report":
            report = generate_report(run_dir(args.run_id))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.command == "compare":
            current_dir, baseline_dir = run_dir(args.run_id), run_dir(args.baseline)
            current_manifest = read_json(current_dir / "manifest.json")
            baseline_manifest = read_json(baseline_dir / "manifest.json")
            identity_fields = {"provider", "model_id_hash", "judge_provider", "judge_model_id_hash"}
            if any(current_manifest["config"].get(field) != baseline_manifest["config"].get(field) for field in identity_fields):
                raise ValueError("provider, tested model, and judge model must match the baseline")
            current = generate_report(current_dir)
            baseline = generate_report(baseline_dir)
            comparison = compare_reports(current, baseline)
            print(json.dumps(comparison, ensure_ascii=False, indent=2))
            return 0 if comparison["passed"] else 1
        baseline_path = RUNS_ROOT.parent / "baseline.json"
        if args.action == "show":
            print(baseline_path.read_text(encoding="utf-8") if baseline_path.exists() else "{}")
            return 0
        if not args.run_id:
            raise ValueError("baseline set requires run_id")
        source = run_dir(args.run_id)
        manifest, report = read_json(source / "manifest.json"), generate_report(source)
        expected_case_ids = [case.id for case in load_suite(manifest["suite"])]
        errors = baseline_eligibility_errors(manifest, report, expected_case_ids)
        if baseline_path.exists():
            previous = read_json(baseline_path)
            previous_config = previous.get("config", {})
            current_config = manifest.get("config", {})
            for field in ("judge_provider", "judge_model_id_hash"):
                if previous_config.get(field) != current_config.get(field):
                    errors.append("judge configuration differs from the current baseline")
                    break
        if errors:
            raise ValueError("; ".join(errors))
        write_json(baseline_path, {"run_id": args.run_id, "suite": manifest["suite"], "config": manifest["config"]})
        print(json.dumps({"baseline": args.run_id}))
        return 0
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
