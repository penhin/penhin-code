from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import os

from result import Result


TOOL_CACHE_PLACEHOLDER_MIN_CHARS = 4000
CacheKey = tuple
Validator = Callable[[], bool]
SignatureFactory = Callable[[], tuple]


@dataclass
class CacheEntry:
    result: Result
    message_chars: int
    description: str
    is_valid: Validator


class ToolResultCache:
    def __init__(self):
        self.entries: dict[CacheKey, CacheEntry] = {}

    def get(self, key: CacheKey) -> Result | None:
        entry: CacheEntry = self.entries.get(key)
        if entry is None:
            return None

        if not entry.is_valid():
            self.entries.pop(key, None)
            return None

        if entry.message_chars >= TOOL_CACHE_PLACEHOLDER_MIN_CHARS:
            return Result.success(
                f"[cached {entry.description}: unchanged; previous full result is already in context]",
                data={
                    "cached": True,
                    "cache_hit": True,
                    "description": entry.description,
                    "message_chars": entry.message_chars,
                },
                cached=True,
                cache_hit=True,
            )

        return Result(
            ok=entry.result.ok,
            message=entry.result.message,
            data=entry.result.data,
            error=entry.result.error,
            meta={**entry.result.meta, "cached": True, "cache_hit": True},
        )

    def set(self, key: CacheKey, result: Result, description: str, is_valid: Validator) -> Result:
        if not result.ok:
            return result

        self.entries[key] = CacheEntry(
            result=result,
            message_chars=len(result.message),
            description=description,
            is_valid=is_valid,
        )
        return result
    
    def clear(self) -> None:
        self.entries.clear()


def file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def file_validator(path: Path, signature: tuple[int, int]) -> Validator:
    def is_valid() -> bool:
        try:
            return file_signature(path) == signature
        except OSError:
            return False

    return is_valid


def tree_signature(paths, workdir: Path) -> tuple:
    entries = []
    for path in paths:
        try:
            stat = path.stat()
            rel_path = str(path.relative_to(workdir))
        except (OSError, ValueError):
            continue
        entries.append((rel_path, stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(entries))


def directory_tree_signature(
    root: Path,
    workdir: Path,
    is_ignored,
    recursive: bool = True,
) -> tuple:
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        if is_ignored(current):
            dirnames[:] = []
            continue
        dirnames[:] = [
            dirname for dirname in dirnames
            if not is_ignored(current / dirname)
        ]
        try:
            stat = current.stat()
            rel_path = str(current.relative_to(workdir))
        except (OSError, ValueError):
            continue
        visible_filenames = tuple(sorted(filenames))
        entries.append((rel_path, stat.st_mtime_ns, visible_filenames))
        if not recursive:
            dirnames[:] = []
    return tuple(sorted(entries))


def tree_validator(signature_factory: SignatureFactory, signature: tuple) -> Validator:
    def is_valid() -> bool:
        try:
            return signature_factory() == signature
        except OSError:
            return False

    return is_valid


tool_result_cache = ToolResultCache()
