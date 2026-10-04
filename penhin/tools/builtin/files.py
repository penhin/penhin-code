import concurrent.futures
import hashlib
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
READ_SNAPSHOTS: dict[str, tuple[Path, list[str], int]] = {}
READ_SNAPSHOTS_LOCK = threading.RLock()


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


def _read_snapshot(file_path: Path, snapshot_id: str | None) -> tuple[str, list[str], int]:
    if snapshot_id is not None:
        with READ_SNAPSHOTS_LOCK:
            snapshot = READ_SNAPSHOTS.get(snapshot_id)
        if snapshot is None or snapshot[0] != file_path:
            raise ValueError("Read snapshot is unavailable for this path")
        return snapshot_id, snapshot[1], snapshot[2]
    raw = file_path.read_bytes()
    identifier = hashlib.sha256(str(file_path).encode("utf-8") + b"\0" + raw).hexdigest()[:24]
    lines = raw.decode("utf-8").splitlines()
    with READ_SNAPSHOTS_LOCK:
        READ_SNAPSHOTS[identifier] = (file_path, lines, len(raw))
    return identifier, lines, len(raw)


def run_read(
    path: str, limit: int | None = None, line_numbers: bool = True,
    offset: int = 1, snapshot_id: str | None = None, *,
    query: str | None = None, pattern: str | None = None,
) -> Result:
    """Read a file snapshot, discover directory files, or search literal content."""
    try:
        if offset < 1:
            return Result.failure("Error: offset must be at least 1", code="invalid_offset")
        if limit is not None and limit < 0:
            return Result.failure("Error: limit cannot be negative", code="invalid_limit")
        file_path = safe_path(path)
        if query is not None:
            if not query or pattern is not None or snapshot_id is not None or offset != 1:
                return Result.failure("Content search requires a nonempty query; pattern, snapshot_id and offset are not supported.", code="invalid_tool_input")
            if not file_path.exists():
                return Result.failure(f"Path does not exist: {path}", code="read_error")
            return run_search(query, path, limit=limit)
        if file_path.is_dir():
            if snapshot_id is not None:
                return Result.failure("snapshot_id is only supported for file reads.", code="invalid_tool_input")
            return _read_directory(path, limit, offset, pattern)
        if pattern is not None:
            return Result.failure("A filename pattern requires a directory path.", code="invalid_tool_input")
        key = ("read", str(file_path), limit, line_numbers, offset, snapshot_id)
        cached = tool_result_cache.get(key)
        if cached is not None:
            return cached

        continuing_snapshot = snapshot_id is not None
        snapshot_id, snapshot_lines, original_bytes = _read_snapshot(file_path, snapshot_id)
        signature = file_signature(file_path) if not continuing_snapshot else None
        requested_lines = MAX_TOOL_OUTPUT_LINES if limit is None else min(max(limit, 0), MAX_TOOL_OUTPUT_LINES)
        total_lines = len(snapshot_lines)
        lines = snapshot_lines[offset - 1:offset - 1 + requested_lines]
        if line_numbers:
            lines = [f"{i}: {line}" for i, line in enumerate(lines, start=offset)]
        bounded = bound_text("\n".join(lines))
        returned_lines = len(bounded.text.splitlines())
        end_offset = offset + returned_lines - 1
        next_offset = end_offset + 1 if end_offset < total_lines else None
        omitted_ranges = []
        if offset > 1:
            omitted_ranges.append({"start": 1, "end": offset - 1})
        if next_offset is not None:
            omitted_ranges.append({"start": next_offset, "end": total_lines})
        result = Result.success(
            bounded.text,
            data={
                "path": path,
                "snapshot_id": snapshot_id,
                "evidence_ref": f"read:{snapshot_id}:{offset}-{end_offset}",
                "covered_range": {"start": offset, "end": end_offset},
                "next_offset": next_offset,
                "complete": next_offset is None,
                "omitted_ranges": omitted_ranges,
            },
            truncated=bounded.truncated or next_offset is not None,
            original_bytes=original_bytes,
            original_lines=total_lines,
        )
        return tool_result_cache.set(
            key,
            result,
            description=f"read {path}",
            is_valid=(lambda: True) if continuing_snapshot else file_validator(file_path, signature),
        )
    except Exception as error:
        return Result.failure(f"Error: {error}", code="read_error")


def _read_directory(path: str, limit: int | None, offset: int, pattern: str | None) -> Result:
    root = safe_path(path)
    if pattern is not None:
        from .glob import glob_workspace_files
        if not pattern:
            return Result.failure("Filename pattern cannot be empty.", code="invalid_tool_input")
        files = glob_workspace_files(root, pattern)
    else:
        files = iter_workspace_files(root)
    paths = sorted(str(file.relative_to(WORKDIR)) for file in files)
    page_size = MAX_TOOL_OUTPUT_LINES if limit is None else min(limit, MAX_TOOL_OUTPUT_LINES)
    page = paths[offset - 1:offset - 1 + page_size]
    bounded = bound_text("\n".join(page))
    page = page[:len(bounded.text.splitlines())]
    next_offset = offset + len(page) if offset - 1 + len(page) < len(paths) else None
    return Result.success(
        bounded.text,
        data={"kind": "directory", "path": path, "pattern": pattern,
              "paths": page, "total_count": len(paths), "next_offset": next_offset,
              "complete": next_offset is None},
        count=len(page), truncated=next_offset is not None or bounded.truncated,
    )


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


def run_edit_batch(edits: list[dict[str, str]]) -> Result:
    """Apply exact replacements only when every cited read snapshot is current."""
    if not write_is_allowed():
        return Result.failure("Error: writes are disabled for this agent worktree", code="readonly_workspace")
    if not edits:
        return Result.failure("Error: edits are required", code="invalid_edit_batch")
    try:
        prepared: dict[Path, tuple[str, str, str]] = {}
        bases: list[dict[str, str]] = []
        with FILE_LOCK:
            for edit in edits:
                if not isinstance(edit, dict) or not all(isinstance(edit.get(key), str) for key in ("path", "snapshot_id", "old", "new")):
                    raise ValueError("Each edit requires string path, snapshot_id, old, and new")
                file_path = safe_path(edit["path"])
                snapshot_id, snapshot_lines, _size = _read_snapshot(file_path, edit["snapshot_id"])
                original, current, _snapshot = prepared.get(file_path, (file_path.read_text(encoding="utf-8"), file_path.read_text(encoding="utf-8"), snapshot_id))
                if current.splitlines() != snapshot_lines:
                    raise ValueError(f"Stale read snapshot for {edit['path']}")
                count = current.count(edit["old"])
                if count != 1:
                    raise ValueError(f"Conflicting edit for {edit['path']}: expected one matching hunk, found {count}")
                prepared[file_path] = (original, current.replace(edit["old"], edit["new"], 1), snapshot_id)
                bases.append({"path": edit["path"], "snapshot_id": snapshot_id})
            written: list[Path] = []
            try:
                for file_path, (_original, updated, _snapshot_id) in prepared.items():
                    atomic_write_text(file_path, updated)
                    written.append(file_path)
            except Exception:
                for file_path in written:
                    atomic_write_text(file_path, prepared[file_path][0])
                raise
        tool_result_cache.clear()
        return Result.success(
            f"Applied {len(edits)} edits across {len(prepared)} files",
            data={
                "base_snapshots": bases,
                "change_set": [{"path": str(path.relative_to(WORKDIR)), "replacements": sum(1 for edit in edits if safe_path(edit["path"]) == path)} for path in prepared],
            },
        )
    except Exception as error:
        return Result.failure(
            f"Error: {error}", code="edit_batch_conflict", conflict={"reason": str(error)},
        )


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


def run_search(query: str, path: str = ".", limit: int | None = None, timeout: int = 30) -> Result:
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
