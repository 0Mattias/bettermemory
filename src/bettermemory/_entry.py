"""The process entry point behind the ``bettermemory`` script and
``python -m bettermemory``.

The hook words are dispatched before the CLI package is imported, so a
hook pays the interpreter and the standard library and nothing else
(`_daemon_client` says why that matters). Everything else goes to
`cli.main`.
"""

from __future__ import annotations

import sys


def main() -> None:
    argv = sys.argv[1:]
    if argv:
        from ._daemon_client import HOOK_WORDS

        if argv[0] in HOOK_WORDS:
            from ._daemon_client import hook_main

            sys.exit(hook_main(argv))
    from .cli import main as cli_main

    cli_main()


__all__ = ["main"]
