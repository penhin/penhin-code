import concurrent.futures
import os
import threading
from pathlib import Path

from penhin.infrastructure.atomic_io import atomic_write_text
from penhin.result import Result
from penhin.orchestration.permissions import write_is_allowed
from penhin.tools.output_budget import MAX_TOOL_OUTPUT_LINES, bound_text

from .cache import file_signature, file_validator, tool_result_cache, tree_signature, tree_validator
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


def run_read(path: str, limit: int = None, line_numbers: bool = True, offset: int = 1) -> Result:
    try:
        if offset < 1:
            return Result.failure("Error: offset must be at least 1", code="invalid_offset")
        if limit is not None and limit < 0:
            return Result.failure("Error: limit cannot be negative", code="invalid_limit")
        file_path = safe_path(path)
        key = ("read", str(file_path), limit, line_numbers, offset)
        cached = tool_result_cache.get(key)
        if cached is not None:
            return cached

        signature = file_signature(file_path)
        requested_lines = MAX_TOOL_OUTPUT_LINES if limit is None else min(max(limit, 0), MAX_TOOL_OUTPUT_LINES)
        lines: list[str] = []
        total_lines = 0
        with file_path.open("r", encoding="utf-8") as handle:
            for total_lines, line in enumerate(handle, start=1):
                if total_lines < offset or len(lines) >= requested_lines:
                    continue
                lines.append(line.rstrip("\r\n"))
        if line_numbers:
            lines = [f"{i}: {line}" for i, line in enumerate(lines, start=offset)]
        bounded = bound_text("\n".join(lines))
        returned_lines = len(bounded.text.splitlines())
        next_offset = offset + returned_lines if offset - 1 + returned_lines < total_lines else None
        result = Result.success(
            bounded.text,
            data={"path": path, "offset": offset, "next_offset": next_offset},
            truncated=bounded.truncated or next_offset is not None,
            original_bytes=file_path.stat().st_size,
            original_lines=total_lines,
        )
        return tool_result_cache.set(
            key,
            result,
            description=f"read {path}",
            is_valid=file_validator(file_path, signature),
        )
    except Exception as error:
        return Result.failure(f"Error: {error}", code="read_error")


def run_write(path: str, content: str = None) -> Result:
    try:
        if not write_is_allowed():
            return Result.failure("Error: writes are disabled for this agent worktree", code="readonly_workspace")
        if content is None:
            return Result.failure("Error: content is required", code="missing_content")
        file_path = safe_path(path)
        with FILE_LOCK:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(file_path, content)
        tool_result_cache.clear()
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

        key = ("list", str(file_path), limit)
        cached = tool_result_cache.get(key)
        if cached is not None:
            return cached

        def signature_factory():
            return tree_signature(iter_workspace_files(file_path), WORKDIR)

        signature = signature_factory()
        paths = []
        for child in iter_workspace_files(file_path):
            paths.append(str(child.relative_to(WORKDIR)))
            if limit and len(paths) >= limit:
                paths.append("... (limit reached)")
                break

        result = Result.success(
            "\n".join(paths),
            data={"path": path, "paths": paths, "limit": limit},
            count=len(paths),
        )
        return tool_result_cache.set(
            key,
            result,
            description=f"list {path}",
            is_valid=tree_validator(signature_factory, signature),
        )

    except Exception as error:
        return Result.failure(f"Error: {error}", code="list_error")


def run_edit(path: str, old: str, new: str) -> Result:
    try:
        if not write_is_allowed():
            return Result.failure("Error: edits are disabled for this agent worktree", code="readonly_workspace")
        file_path = safe_path(path)
        with FILE_LOCK:
            text = file_path.read_text(encoding="utf-8")

            count = text.count(old)
            if count == 0:
                return Result.failure("Error: old text not found", code="old_text_not_found")
            if count > 1:
                return Result.failure(f"Error: old text appears {count} times", code="old_text_not_unique", count=count)

            updated = text.replace(old, new, 1)
            atomic_write_text(file_path, updated)

        tool_result_cache.clear()
        return Result.success(f"Edited {path}", data={"path": path, "replacements": 1})
    except Exception as error:
        return Result.failure(f"Error: {error}", code="edit_error")


def _search_file(query: str, file_path: Path, workdir: Path, limit: int) -> list[str]:
    """Search a single file. Extracted for ThreadPoolExecutor."""
    if limit <= 0:
        return []
    try:
        handle = file_path.open("r", encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    rel_path = file_path.relative_to(workdir)
    matches = []
    try:
        for line_number, line in enumerate(handle, start=1):
            if query in line:
                line_text = line.rstrip("\r\n")
                matches.append(f"{rel_path}:{line_number}:{line_text}")
                if len(matches) >= limit:
                    break
    except UnicodeDecodeError:
        return []
    finally:
        handle.close()
    return matches


def run_search(query: str, path: str = ".", limit: int = None, timeout: int = 30) -> Result:
    try:
        if limit is not None and limit < 0:
            return Result.failure("Error: limit cannot be negative", code="invalid_limit")
        file_path = safe_path(path)
        key = ("search", str(file_path), query, limit, timeout)
        cached = tool_result_cache.get(key)
        if cached is not None:
            return cached

        all_files = list(iter_workspace_files(file_path))
        if not all_files:
            return Result.success(
                "", data={"query": query, "path": path, "limit": limit}, count=0
            )

        def signature_factory():
            return tree_signature(iter_workspace_files(file_path), WORKDIR)

        signature = tree_signature(all_files, WORKDIR)
        results: list[str] = []
        result_limit = min(limit, MAX_TOOL_OUTPUT_LINES) if limit is not None else MAX_TOOL_OUTPUT_LINES
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(os.cpu_count() or 1, 8)
        ) as executor:
            futures = {executor.submit(_search_file, query, f, WORKDIR, result_limit): f for f in all_files}
            try:
                for future in concurrent.futures.as_completed(futures, timeout=timeout):
                    matches = future.result()
                    results.extend(matches)
                    if len(results) >= result_limit:
                        break
            except concurrent.futures.TimeoutError:
                pass  # return partial results

        results = results[:result_limit]
        bounded = bound_text("\n".join(results))
        returned_count = len(bounded.text.splitlines())
        limit_reached = result_limit > 0 and len(results) >= result_limit
        result = Result.success(
            bounded.text,
            data={"query": query, "path": path, "limit": limit, "narrow_query_to_continue": limit_reached},
            count=returned_count,
            collected_matches=len(results),
            truncated=bounded.truncated or limit_reached,
            limit_reached=limit_reached,
        )
        return tool_result_cache.set(
            key,
            result,
            description=f"search {path} for {query!r}",
            is_valid=tree_validator(signature_factory, signature),
        )
    except Exception as error:
        return Result.failure(f"Error: {error}", code="search_error")
