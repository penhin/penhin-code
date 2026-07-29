from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version


PACKAGE_NAME = "penhin-code"


def get_version() -> str:
    override = os.getenv("PENHIN_VERSION", "").strip()
    if override:
        return override
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "dev"
