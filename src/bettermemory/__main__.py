"""Entry point for `python -m bettermemory`, the same as the installed
``bettermemory`` script: `_entry.main` routes the hook words before the
CLI package loads and everything else to `cli.main`."""

from ._entry import main


if __name__ == "__main__":
    main()
