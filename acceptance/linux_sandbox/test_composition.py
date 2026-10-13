"""Explicit native Linux acceptance entry; never convert missing enforcement to skip.

Run separately: python -m pytest acceptance/linux_sandbox/test_composition.py -q
This trusted fixture experiment does not certify a Python/Plugin runtime baseline.
"""

import json
from pathlib import Path
import subprocess
import sys


def test_rootless_composition_with_real_enforcement(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    evidence = tmp_path / "composition.json"
    result = subprocess.run(
        [sys.executable, str(repository / "tools/linux_sandbox/composition.py"),
         "--output", str(evidence)],
        cwd=repository, text=True, capture_output=True, timeout=240, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(evidence.read_text(encoding="utf-8"))
    assert report["result"] == "composition_passed"
    assert report["supported_runtime_baseline"] is False
    assert report["production_ready"] is False
    assert report["checks"] == {
        "real_byte_and_inode_exhaustion": True,
        "extra_broker_pipe_handoff": True,
        "pre_exec_placement": True,
        "concurrent_hosts": True,
        "supervisor_sigkill_cleanup": True,
        "core_sigkill_cleanup": True,
    }
    assert len(report["hosts"]) == 2
    assert not report.get("cleanup_errors")
