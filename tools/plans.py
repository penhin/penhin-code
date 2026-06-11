from __future__ import annotations

import hashlib
from pathlib import Path

PLANS_DIR = Path.home() / ".penhin" / "plans"
PLANS_DIR.mkdir(parents=True, exist_ok=True)


def _word_slug(text: str, length: int = 3) -> str:
    """Deterministic human-readable slug from text content."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    words = []
    for i in range(0, min(length * 4, len(digest)), 4):
        segment = int(digest[i : i + 4], 16)
        word_index = segment % len(_WORDS)
        words.append(_WORDS[word_index])
    return "-".join(words)


def write_plan(content: str, slug: str | None = None) -> Path:
    slug = slug or _word_slug(content)
    path = PLANS_DIR / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


def read_plan(slug: str) -> str | None:
    path = PLANS_DIR / f"{slug}.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


def list_plans() -> list[Path]:
    return sorted(PLANS_DIR.glob("*.md"))


_WORDS = [
    "gentle", "swift", "quiet", "bold", "calm",
    "bright", "dark", "deep", "dry", "damp",
    "eager", "faint", "fierce", "fresh", "full",
    "glad", "grand", "grave", "great", "harsh",
    "holy", "keen", "kind", "lean", "light",
    "loud", "lowly", "lucky", "mild", "noble",
    "pale", "proud", "pure", "quick", "rare",
    "rough", "round", "sharp", "short", "shy",
    "slim", "small", "smart", "soft", "solid",
    "still", "sunny", "swift", "tall", "tame",
]
