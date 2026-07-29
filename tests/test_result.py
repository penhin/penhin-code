import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers import Result


def test_result_json() -> None:
    data = json.loads(Result.success("hello", data={"value": 1}).to_json())
    assert data["ok"] is True
    assert data["message"] == "hello"
    assert data["data"] == {"value": 1}
    assert data["error"] == ""

    failed = Result.failure("broken", code="test_error")
    failed_data = json.loads(failed.to_json())
    assert failed.ok is False
    assert failed.error == "broken"
    assert failed_data["ok"] is False
    assert failed_data["meta"]["code"] == "test_error"


def test_result_summary_reports_sizes_without_content() -> None:
    result = Result.success("secret output", data={"value": 1}, cached=True)
    summary = result.summary()

    assert summary == {
        "message_chars": len("secret output"),
        "error_chars": 0,
        "data_type": "dict",
        "meta_keys": ["cached"],
    }
    assert "secret output" not in json.dumps(summary)


def run_all() -> None:
    test_result_json()
    test_result_summary_reports_sizes_without_content()


if __name__ == "__main__":
    run_all()
    print("ok")
