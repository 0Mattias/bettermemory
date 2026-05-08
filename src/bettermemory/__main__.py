"""Entry point for `python -m bettermemory`.

The installed `bettermemory` script (registered under `[project.scripts]`)
calls `bettermemory.server:main` directly. This shim makes the same
behaviour available without depending on the script being on PATH.
"""

from .server import main


if __name__ == "__main__":
    main()
