"""`bettermemory ui` — run the local FastAPI web UI."""

from __future__ import annotations

import argparse
import logging
import sys

from ..config import load_config


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``ui`` subparser on the parent parser."""
    parser = sub.add_parser(
        "ui",
        help=(
            "Run the local web UI (FastAPI). Read-mostly: browse "
            "memories, run memory_verify, see memory_health rollups. "
            "Requires the `[ui]` extra: pip install bettermemory[ui]."
        ),
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help=(
            "Bind host. Default 127.0.0.1 (local only). Pass 0.0.0.0 "
            "to expose on a trusted network (the server logs a warning "
            "in that case since the UI surfaces curation data)."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Bind port. Default: 8765.",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory ui``."""
    _cli_ui(host=args.host, port=args.port)


def _cli_ui(*, host: str, port: int) -> None:
    """`bettermemory ui` — run the local web UI.

    Catches the ImportError raised when the [ui] extra is missing and
    renders a clean install hint instead of a Python traceback.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = load_config()
    try:
        from .. import web as _web

        _web.serve(config, host=host, port=port)
    except ImportError as exc:
        sys.stderr.write(
            "bettermemory ui requires the [ui] extra. Install with:\n"
            "  pip install 'bettermemory[ui]'\n"
            f"(original error: {exc})\n"
        )
        raise SystemExit(2) from exc
