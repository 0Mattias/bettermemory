"""The process entry point behind the ``bettermemory`` script and
``python -m bettermemory``.

The hook words are dispatched before the CLI package is imported, so a
hook pays the interpreter and the standard library and nothing else
(`_daemon_client` says why that matters). Everything else goes to
`cli.main`, which routes the hook words the same way
(`route_hook_word`), so `bettermemory.server:main` and an in-process
`cli.main` reach the one implementation, `_daemon_client.hook_main`.
"""

from __future__ import annotations

import sys


def route_hook_word(argv: list[str]) -> None:
    """Exit through `_daemon_client.hook_main` when `argv` starts with a
    hook word (`_daemon_client.HOOK_WORDS`: ``hook`` and its three 8.x
    aliases); return otherwise. The one place the words are routed."""
    if not argv:
        return
    from ._daemon_client import HOOK_WORDS

    if argv[0] in HOOK_WORDS:
        from ._daemon_client import hook_main

        sys.exit(hook_main(argv))


def main() -> None:
    route_hook_word(sys.argv[1:])
    from .cli import main as cli_main

    cli_main()


__all__ = ["main", "route_hook_word"]
