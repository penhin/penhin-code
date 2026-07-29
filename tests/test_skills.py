import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skills import SkillLoader


def test_skill_loader_discovers_user_skill_created_after_initialization(tmp_path: Path) -> None:
    loader = SkillLoader(tmp_path / "skills")
    skill_file = tmp_path / "skills" / "release-check" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: release-check\ndescription: Verify a release.\n---\n\n# Release check\n",
        encoding="utf-8",
    )

    assert "release-check: Verify a release." in loader.get_descriptions()
    result = loader.get_content("release-check")
    assert result.ok is True
    assert "# Release check" in result.message


def test_skill_loader_rejects_invalid_names(tmp_path: Path) -> None:
    result = SkillLoader(tmp_path).get_content("../secret")

    assert result.ok is False
    assert result.meta["code"] == "invalid_skill_name"
