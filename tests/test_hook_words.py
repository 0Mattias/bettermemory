"""The hook words have one implementation, the hook client.

`hook` and its three 8.x aliases (`session-start`, `audit-turn` and
`prompt-recall`: `_daemon_client.HOOK_WORDS`) go to
`_daemon_client.hook_main` from the console entry (`_entry.main`, which
the `bettermemory` script and `python -m bettermemory` run), and from
`cli.main` and the historical `bettermemory.server:main` the same way
(`_entry.route_hook_word`), before any parser runs. The CLI parser
registers `hook` for `--help` alone, and no `bettermemory.cli` module
serves one of the aliases: the in-process bodies they once had are gone.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

from bettermemory import _daemon_client, _entry, cli, server
from bettermemory._daemon_client import HOOK_WORDS

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_bettermemory_session_start_goes_to_the_hook_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"][
        "scripts"
    ]
    assert scripts["bettermemory"] == "bettermemory._entry:main"

    _, subparsers = cli._build_parser()
    assert set(subparsers) & HOOK_WORDS == {"hook"}
    for word in HOOK_WORDS - {"hook"}:
        name = f"bettermemory.cli.{word.replace('-', '_')}_cmd"
        assert importlib.util.find_spec(name) is None, name

    calls: list[list[str]] = []

    def hook_main(argv: list[str]) -> int:
        calls.append(list(argv))
        return 0

    def no_parser() -> None:
        raise AssertionError("the CLI parser ran for a hook word")

    monkeypatch.setattr(_daemon_client, "hook_main", hook_main)
    monkeypatch.setattr(cli, "_build_parser", no_parser)
    for entry in (_entry.main, cli.main, server.main):
        for word in sorted(HOOK_WORDS):
            argv = [word, "--session-id", "s1"]
            monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
            with pytest.raises(SystemExit) as exited:
                entry()
            assert exited.value.code == 0
            assert calls == [argv], (entry, word)
            calls.clear()
