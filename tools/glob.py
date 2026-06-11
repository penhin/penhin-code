from result import Result

from .workspace import WORKDIR, is_ignored_path


def run_glob(pattern: str, path: str = ".") -> Result:
    try:
        root = (WORKDIR / path).resolve()
        if not root.is_relative_to(WORKDIR):
            return Result.failure(f"Path escapes workspace: {path}", code="path_escape")

        matches: list[str] = []
        for match_path in root.glob(pattern):
            if match_path.is_dir() or is_ignored_path(match_path):
                continue
            matches.append(str(match_path.relative_to(WORKDIR)))

        matches.sort()
        return Result.success(
            "\n".join(matches),
            data={"pattern": pattern, "path": path, "matches": matches},
            count=len(matches),
        )
    except Exception as error:
        return Result.failure(f"Error: {error}", code="glob_error")
