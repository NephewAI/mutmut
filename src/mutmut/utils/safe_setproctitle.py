"""Safe wrapper for setproctitle that handles fork-unsafe behavior on macOS.

setproctitle uses CoreFoundation APIs on macOS which aren't fork-safe.
Calling setproctitle after fork() causes segfaults in the child process.

This module provides a safe_setproctitle() function that:
- Works normally on Linux
- Is a no-op on macOS to avoid crashes after fork()

Related: https://github.com/boxed/mutmut/pull/450#issuecomment-4002571055
"""

from mutmut.configuration import config


def safe_setproctitle(title: str) -> None:
    """Set the process title, unless the config turns that off (the default on macOS).

    Reads the config on call rather than import, so importing mutmut needs no project config.
    """
    if config().use_setproctitle:
        from setproctitle import setproctitle

        setproctitle(title)
