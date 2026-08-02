from penhin.result import Result

from .cache import directory_tree_signature, tool_result_cache, tree_validator
from .workspace import WORKDIR, is_ignored_path


def run_glob(pattern: str, path: str = ".") -> Result:
    try:
        root = (WORKDIR / path).resolve()
        if not root.is_relative_to(WORKDIR):
            return Result.failure(f"Path escapes workspace: {path}", code="path_escape")

        key = ("glob", str(root), pattern)
        cached = tool_result_cache.get(key)
        if cached is not None:
            return cached

        def matching_files() -> list:
            return [
                match_path
                for match_path in root.glob(pattern)
                if not match_path.is_dir() and not is_ignored_path(match_path)
            ]

        recursive_signature = "/" in pattern or "**" in pattern
        matched_files = matching_files()
        signature = directory_tree_signature(
            root,
            WORKDIR,
            is_ignored_path,
            recursive=recursive_signature,
        )
        matches: list[str] = []
        for match_path in matched_files:
            matches.append(str(match_path.relative_to(WORKDIR)))

        matches.sort()
        result = Result.success(
            "\n".join(matches),
            data={"pattern": pattern, "path": path, "matches": matches},
            count=len(matches),
        )
        return tool_result_cache.set(
            key,
            result,
            description=f"glob {pattern} in {path}",
            is_valid=tree_validator(
                lambda: directory_tree_signature(
                    root,
                    WORKDIR,
                    is_ignored_path,
                    recursive=recursive_signature,
                ),
                signature,
            ),
        )
    except Exception as error:
        return Result.failure(f"Error: {error}", code="glob_error")
