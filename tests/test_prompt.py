import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import prompt


def test_build_main_system_without_project_instructions() -> None:
    missing_path = Path(tempfile.mkdtemp()) / "AGENTS.md"

    with patch.object(prompt, "USER_PROMPT_PATH", missing_path):
        system = prompt.build_main_system()

    assert system == prompt.MAIN_SYSTEM
    assert "# Project Instructions" not in system


def test_main_system_sections_use_xml_tags() -> None:
    system = prompt.build_main_system()

    assert "<identity>" in system
    assert "</identity>" in system
    assert "<tool_usage>" in system
    assert "</tool_usage>" in system
    assert "<file_scope>" in system
    assert "</file_scope>" in system
    assert "<task_workflow>" in system
    assert "</task_workflow>" in system
    assert "<planning_workflow>" not in system
    assert "agent_plan_create" in system
    assert "agent_dag_show" in system
    assert "task_start" in system
    assert "verify" in system
    assert "task_complete" in system
    assert "<available_tools>" in system
    assert "</available_tools>" in system
    assert "<available_skills>" in system
    assert "</available_skills>" in system


def test_build_main_system_does_not_include_project_instructions() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        instructions_path = Path(tmpdir) / "AGENTS.md"
        instructions_path.write_text("Always run smoke tests.", encoding="utf-8")

        with patch.object(prompt, "USER_PROMPT_PATH", instructions_path):
            system = prompt.build_main_system()

    assert system == prompt.MAIN_SYSTEM
    assert "Always run smoke tests." not in system


def test_project_instructions_are_first_user_message() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        instructions_path = Path(tmpdir) / "AGENTS.md"
        instructions_path.write_text("Always run smoke tests.", encoding="utf-8")
        messages = [{"role": "user", "content": "hello"}]

        with patch.object(prompt, "USER_PROMPT_PATH", instructions_path):
            prompt.ensure_project_instructions_message(messages)

    assert messages == [
        {
            "role": "user",
            "content": "<project_instructions>\nAlways run smoke tests.\n</project_instructions>",
        },
        {"role": "user", "content": "hello"},
    ]


def test_build_subagent_system_does_not_include_project_instructions() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        instructions_path = Path(tmpdir) / "AGENTS.md"
        instructions_path.write_text("Keep changes small.", encoding="utf-8")

        with patch.object(prompt, "USER_PROMPT_PATH", instructions_path):
            system = prompt.build_subagent_system()

    assert system == prompt.SUBAGENT_SYSTEM
    assert "Keep changes small." not in system
    assert "keep the assigned task narrow" in system


def test_build_subagent_final_system_keeps_final_instruction() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        instructions_path = Path(tmpdir) / "AGENTS.md"
        instructions_path.write_text("Keep changes small.", encoding="utf-8")

        with patch.object(prompt, "USER_PROMPT_PATH", instructions_path):
            system = prompt.build_subagent_final_system()

    assert "Keep changes small." not in system
    assert "The tool budget is exhausted." in system
    assert "Do not call tools" in system


def test_build_verification_system_defines_boundary() -> None:
    system = prompt.build_verification_system()

    assert system == prompt.VERIFICATION_SYSTEM
    assert "verification agent" in system
    assert "Do not create, edit, or delete files." in system
    assert "Run focused tests or checks" in system
    assert "try to break the implementation" in system
    assert "Backend/API" in system
    assert "Frontend/UI" in system
    assert "CLI/tools" in system
    assert "Database/migrations" in system
    assert "Concurrency/background work" in system
    assert "Boundary values" in system
    assert "Idempotency" in system
    assert "Orphan operations" in system
    assert "Reading code is not verification" in system
    assert "Command run:" in system
    assert "Verdict: PASS | FAIL | BLOCKED" in system
    assert "Did I merely inspect code and call it verified?" in system
    assert "concise verdict" in system


def run_all() -> None:
    test_build_main_system_without_project_instructions()
    test_main_system_sections_use_xml_tags()
    test_build_main_system_does_not_include_project_instructions()
    test_project_instructions_are_first_user_message()
    test_build_subagent_system_does_not_include_project_instructions()
    test_build_subagent_final_system_keeps_final_instruction()
    test_build_verification_system_defines_boundary()


if __name__ == "__main__":
    run_all()
    print("ok")
