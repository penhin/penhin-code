from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from penhin.result import Result


SKILLS_DIR = Path("skills")
SKILL_FILE_NAME = "SKILL.md"
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class SkillLoader:
    """Load user-provided skills from ``skills/<name>/SKILL.md`` on demand."""

    def __init__(self, skills_dir: Path = SKILLS_DIR):
        self.skills_dir = skills_dir

    def _skill_files(self) -> list[Path]:
        if not self.skills_dir.is_dir():
            return []
        return sorted(path for path in self.skills_dir.rglob(SKILL_FILE_NAME) if path.is_file())

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.DOTALL)
        if not match:
            return {}, text.strip()
        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            metadata = {}
        return metadata if isinstance(metadata, dict) else {}, match.group(2).strip()

    def _discover(self) -> dict[str, Path]:
        discovered: dict[str, Path] = {}
        for skill_file in self._skill_files():
            try:
                metadata, _ = self._parse_frontmatter(skill_file.read_text(encoding="utf-8"))
            except OSError:
                continue
            name = metadata.get("name", skill_file.parent.name)
            if isinstance(name, str) and _SKILL_NAME.fullmatch(name):
                discovered[name] = skill_file
        return discovered

    def get_descriptions(self) -> str:
        descriptions: list[str] = []
        for name, skill_file in self._discover().items():
            try:
                metadata, _ = self._parse_frontmatter(skill_file.read_text(encoding="utf-8"))
            except OSError:
                continue
            description = " ".join(str(metadata.get("description", "No description")).split())
            descriptions.append(f"- {name}: {description}")
        return "\n".join(descriptions) if descriptions else "(no skills available)"

    def get_content(self, name: str) -> Result:
        if not _SKILL_NAME.fullmatch(name):
            return Result.failure("Error: Invalid skill name", code="invalid_skill_name")
        discovered = self._discover()
        skill_file = discovered.get(name)
        if skill_file is None:
            available = list(discovered)
            return Result.failure(
                f"Error: Unknown skill '{name}'. Available: {', '.join(available) or '(none)'}",
                code="unknown_skill",
                available=available,
            )
        try:
            metadata, body = self._parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        except OSError as error:
            return Result.failure(f"Error: Could not read skill '{name}': {error}", code="skill_read_error")
        return Result.success(
            f'<skill name="{name}">\n{body}\n</skill>',
            data={"name": name, "path": str(skill_file), "meta": metadata},
        )

    def __call__(self, name: str) -> Result:
        return self.get_content(name)


load_skill = SkillLoader()
