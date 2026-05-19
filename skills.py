import re
from typing import Any
from pathlib import Path

import yaml

from result import Result


SKILLS_DIR = Path("skills")


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills: dict[str, dict[str, Any]] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not self.skills_dir.exists():
            return

        for skill_file in sorted(self.skills_dir.rglob("SKILL.md")):
            text = skill_file.read_text(encoding="utf-8")
            meta, body = self._parse_frontmatter(text)
            name = meta.get("name", skill_file.parent.name)
            self.skills[name] = {
                "meta": meta,
                "body": body,
                "path": str(skill_file),
            }

    def _parse_frontmatter(self, text: str) -> tuple[dict[str, Any], str]:
        match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.DOTALL)
        if not match:
            return {}, text

        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}

        return meta, match.group(2).strip()

    def get_descriptions(self) -> str:
        if not self.skills:
            return "(no skills available)"

        lines = []
        for name, skill in self.skills.items():
            description = skill["meta"].get("description", "No description")
            description = " ".join(str(description).split())
            lines.append(f"- {name}: {description}")
        return "\n".join(lines)

    def get_content(self, name: str) -> Result:
        skill = self.skills.get(name)
        if not skill:
            available = ", ".join(self.skills.keys()) or "(none)"
            return Result.failure(
                f"Error: Unknown skill '{name}'. Available: {available}",
                code="unknown_skill",
                available=list(self.skills.keys()),
            )

        return Result.success(
            f"<skill name=\"{name}\">\n{skill['body']}\n</skill>",
            data={"name": name, "path": skill["path"], "meta": skill["meta"]},
        )

    def __call__(self, name: str) -> Result:
        return self.get_content(name)


load_skill = SkillLoader(SKILLS_DIR)
