"""The hook words have one implementation, the hook client.

`hook` and its three 8.x aliases (`session-start`, `audit-turn` and
`prompt-recall`: `_daemon_client.HOOK_WORDS`) go to
`_daemon_client.hook_main` from the console entry (`_entry.main`, which
the `bettermemory` script and `python -m bettermemory` run), and from
`cli.main` and the historical `bettermemory.server:main` the same way
(`_entry.route_hook_word`), before any parser runs. The CLI parser
registers `hook` for `--help` alone, and no `bettermemory.cli` module
serves one of the aliases: the in-process bodies they once had are gone.
Any other first word goes to the parser, the client's return is the
process's exit code, and `bettermemory hook --help` is the client's usage
line, not the parser's help.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from bettermemory import _daemon_client, _entry, cli, server
from bettermemory._daemon_client import HOOK_WORDS

from .conftest import shielded_child_env

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


class _ReachedTheParser(Exception):
    pass


_ENTRIES = (_entry.main, cli.main, server.main)


def test_any_other_first_word_goes_to_the_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A word that is not a hook word, a hook word spelled another way, or
    a hook word after another, never reaches the client."""

    def hook_main(argv: list[str]) -> int:
        raise AssertionError(f"the hook client ran for {argv}")

    def parser() -> None:
        raise _ReachedTheParser

    monkeypatch.setattr(_daemon_client, "hook_main", hook_main)
    monkeypatch.setattr(cli, "_build_parser", parser)
    for entry in _ENTRIES:
        for argv in (["status"], ["session_start"], ["hooks"], ["status", "hook"]):
            monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
            with pytest.raises(_ReachedTheParser):
                entry()


def test_the_hook_clients_return_is_the_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every entry exits with what `hook_main` returned. It returns 0 by its
    contract; a code it ever returned otherwise must not be swallowed."""
    monkeypatch.setattr(_daemon_client, "hook_main", lambda argv: 3)
    for entry in _ENTRIES:
        monkeypatch.setattr(sys, "argv", ["bettermemory", "hook", "stop"])
        with pytest.raises(SystemExit) as exited:
            entry()
        assert exited.value.code == 3, entry


@pytest.mark.parametrize(
    "argv", [["hook", "--help"], ["hook", "session-start", "--help"], ["hook"]]
)
def test_hook_help_is_the_clients_usage_line(argv: list[str], tmp_path: Path) -> None:
    """`bettermemory hook --help` goes to the client like every hook word:
    its one usage line on stderr and exit 0, nothing on stdout where the
    parser's help would go, and no store or daemon touched."""
    env = shielded_child_env()
    env["BETTERMEMORY_DIR"] = str(tmp_path / "store")
    done = subprocess.run(
        [sys.executable, "-m", "bettermemory", *argv],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    assert done.stderr.startswith(
        "usage: bettermemory hook {session-start|stop|prompt} [--quiet]"
    )
    assert len(done.stderr.splitlines()) == 1, done.stderr
    assert not (tmp_path / "store").exists()
