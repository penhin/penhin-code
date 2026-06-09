import threading
from pathlib import Path

from atomic_io import atomic_write_text
from result import Result

from .workspace import IGNORED_PATH_PARTS, WORKDIR, is_ignored_path, iter_workspace_files


FILE_LOCK = threading.RLock()


def safe_path(path: str) -> Path:
    resolved = (WORKDIR / path).resolve()

    if not resolved.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path}")

    if is_ignored_path(resolved):
        raise ValueError(f"Path is inside blocked directory: {path}")

    return resolved


def ignored_path_part(path: Path) -> str | None:
    try:
        relative_parts = path.resolve().relative_to(WORKDIR).parts
    except ValueError:
        return None

    for part in relative_parts:
        if part in IGNORED_PATH_PARTS:
            return part
    return None


def run_read(path: str, limit: int = None, line_numbers: bool = True) -> Result:
    try:
        text = safe_path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        if limit and len(lines) > limit:
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        if line_numbers:
            lines = [f"{i}: {line}" for i, line in enumerate(lines, start=1)]
        output = "\n".join(lines)[:50000]
        return Result.success(
            output,
            data={"path": path, "lines": lines, "line_numbers": line_numbers},
            truncated=len(output) >= 50000,
        )
    except Exception as error:
        return Result.failure(f"Error: {error}", code="read_error")


def run_write(path: str, content: str = None) -> Result:
    try:
        if content is None:
            return Result.failure("Error: content is required", code="missing_content")
        file_path = safe_path(path)
        with FILE_LOCK:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(file_path, content)
        return Result.success(
            f"Wrote {len(content)} bytes to {path}",
            data={"path": path, "bytes": len(content)},
        )
    except Exception as error:
        return Result.failure(f"Error: {error}", code="write_error")


def run_list(path: str = ".", limit: int = None) -> Result:
    try:
        resolved = (WORKDIR / path).resolve()
        ignored_part = ignored_path_part(resolved)
        if ignored_part:
            hint = " Use load_skill(name=...) for skill instructions." if ignored_part == "skills" else ""
            return Result.success(
                f"(ignored path: {ignored_part}.{hint})",
                data={"path": path, "ignored_part": ignored_part},
            )

        file_path = safe_path(path)
        if not file_path.is_dir():
            return Result.failure("Error: Path should be a dir", code="not_directory", data={"path": path})

        paths = []
        for child in iter_workspace_files(file_path):
            paths.append(str(child.relative_to(WORKDIR)))
            if limit and len(paths) >= limit:
                paths.append("... (limit reached)")
                break

        return Result.success(
            "\n".join(paths),
            data={"path": path, "paths": paths, "limit": limit},
            count=len(paths),
        )

    except Exception as error:
        return Result.failure(f"Error: {error}", code="list_error")


def run_edit(path: str, old: str, new: str) -> Result:
    try:
        file_path = safe_path(path)
        with FILE_LOCK:
            text = file_path.read_text(encoding="utf-8")

            count = text.count(old)
            if count == 0:
                return Result.failure("Error: old text not found", code="old_text_not_found")
            if count > 1:
                return Result.failure(f"Error: old text appears {count}", code="old_text_not_unique", count=count)

            updated = text.replace(old, new, 1)
            atomic_write_text(file_path, updated)

        return Result.success(f"Edited {path}", data={"path": path, "replacements": 1})
    except Exception as error:
        return Result.failure(f"Error: {error}", code="edit_error")


def run_search(query: str, path: str = ".", limit: int = None) -> Result:
    try:
        file_path = safe_path(path)
        results = []

        for child in iter_workspace_files(file_path):
            if limit and len(results) >= limit:
                results.append("... (limit reached)")
                break

            try:
                text = child.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    relative = child.relative_to(WORKDIR)
                    results.append(f"{relative}:{line_number}:{line}")
                    if limit and len(results) >= limit:
                        break

        return Result.success(
            "\n".join(results),
            data={"query": query, "path": path, "matches": results, "limit": limit},
            count=len(results),
        )
    except Exception as error:
        return Result.failure(f"Error: {error}", code="search_error")
