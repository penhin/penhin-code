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


def test_build_main_system_includes_project_instructions() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        instructions_path = Path(tmpdir) / "AGENTS.md"
        instructions_path.write_text("Always run smoke tests.", encoding="utf-8")

        with patch.object(prompt, "USER_PROMPT_PATH", instructions_path):
            system = prompt.build_main_system()

    assert system.startswith(prompt.MAIN_SYSTEM)
    assert "# Project Instructions" in system
    assert "Always run smoke tests." in system


def test_build_subagent_system_includes_project_instructions() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        instructions_path = Path(tmpdir) / "AGENTS.md"
        instructions_path.write_text("Keep changes small.", encoding="utf-8")

        with patch.object(prompt, "USER_PROMPT_PATH", instructions_path):
            system = prompt.build_subagent_system()

    assert system.startswith(prompt.SUBAGENT_SYSTEM)
    assert "# Project Instructions" in system
    assert "Keep changes small." in system
    assert "keep the assigned task narrow" in system


def test_build_subagent_final_system_keeps_final_instruction() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        instructions_path = Path(tmpdir) / "AGENTS.md"
        instructions_path.write_text("Keep changes small.", encoding="utf-8")

        with patch.object(prompt, "USER_PROMPT_PATH", instructions_path):
            system = prompt.build_subagent_final_system()

    assert "# Project Instructions" in system
    assert "Keep changes small." in system
    assert "The tool budget is exhausted." in system


def run_all() -> None:
    test_build_main_system_without_project_instructions()
    test_build_main_system_includes_project_instructions()
    test_build_subagent_system_includes_project_instructions()
    test_build_subagent_final_system_keeps_final_instruction()


if __name__ == "__main__":
    run_all()
    print("ok")
