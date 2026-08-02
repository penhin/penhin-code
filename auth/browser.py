from __future__ import annotations

import subprocess
import sys

from .secrets import scrubbed_environment


def open_browser(target: str) -> None:
    """Best-effort browser launch without a shell or user-visible launcher noise."""
    if sys.platform == "darwin":
        command = ["open", target]
    elif sys.platform == "win32":
        command = ["rundll32", "url.dll,FileProtocolHandler", target]
    else:
        command = ["xdg-open", target]

    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=scrubbed_environment(),
            shell=False,
            start_new_session=True,
        )
    except OSError:
        # The URL remains visible and can be opened manually.
        return
