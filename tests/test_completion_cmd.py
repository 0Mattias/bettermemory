"""`bettermemory completion bash|zsh|fish`: completion generated from the
parser.

The coverage tests walk the argparse tree `cli._build_parser()` returns
with a walker of their own, not the module's, so a subcommand or a flag
the generator drops fails here whichever side the drop is on. When a
shell is on PATH its own parser checks the script (`bash -n`, `zsh -n`,
`fish --no-execute`), and bash's and zsh's completion functions are run
on a few command lines (zsh's with its completion builtins stubbed, so
no compinit is needed). The shell runs are skipped on Windows, where a
`bash` on PATH may be the WSL launcher with no distribution behind it.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from bettermemory.cli import _build_parser
from bettermemory.cli.completion_cmd import (
    SHELLS,
    command_tree,
    render,
)


def _tree(
    parser: argparse.ArgumentParser, path: str = ""
) -> Iterator[tuple[str, argparse.ArgumentParser]]:
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from _tree(child, f"{path} {name}".strip())


def _root() -> argparse.ArgumentParser:
    return _build_parser()[0]


def _option_spelling(shell: str, string: str) -> str:
    """How `string` appears in the shell's script."""
    if shell != "fish":
        return string
    if string.startswith("--"):
        return f"-l {string[2:]}"
    return f"-s {string[1:]}"


def _expected(parser: argparse.ArgumentParser) -> set[str]:
    """What should complete right after `parser`'s command: its
    subcommands, its visible option strings and its positional choices."""
    words: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            words.update(action.choices)
        elif action.help != argparse.SUPPRESS:
            words.update(action.option_strings)
            if not action.option_strings and action.choices is not None:
                words.update(str(choice) for choice in action.choices)
    return words


def _offered(shell: str, script: str, path: str) -> set[str]:
    """The words `script` offers right after the command at `path`, read
    off that path's own arm of the script, so a word another command
    offers does not count for this one."""
    lines = script.splitlines()
    if shell == "bash":
        arm = f"'{path}') COMPREPLY=($(compgen -W '"
        line = next(line.strip() for line in lines if line.strip().startswith(arm))
        return set(line[len(arm) :].split("' --", 1)[0].split())
    if shell == "zsh":
        at = next(i for i, line in enumerate(lines) if line.strip() == f"('{path}')")
        words: set[str] = set()
        for line in lines[at + 1 : at + 3]:
            listed = line.strip().split("=(", 1)[1][:-1]
            words.update(entry.split(":", 1)[0] for entry in shlex.split(listed))
        return words
    words = set()
    condition = f"-n '__bettermemory_at \"{path}\"' "
    for line in lines:
        if condition not in line:
            continue
        # The description is quoted for fish, not for shlex: drop it.
        tokens = shlex.split(line.split(condition, 1)[1].split(" -d '", 1)[0])
        for flag, value in zip(tokens, tokens[1:]):
            if flag == "-a":
                words.update(value.split())
            elif flag == "-l":
                words.add(f"--{value}")
            elif flag in ("-s", "-o"):
                words.add(f"-{value}")
    return words


@pytest.mark.parametrize("shell", SHELLS)
def test_every_subcommand_and_option_of_every_parser_is_in_the_script(
    shell: str,
) -> None:
    script = render(shell, _root())
    _, registry = _build_parser()
    assert set(registry) <= _offered(shell, script, "")
    seen_paths = 0
    for path, parser in _tree(_root()):
        seen_paths += 1
        if path:
            # Every command path is one the script can reach.
            assert f"'{path}'" in script, (shell, path)
        missing = _expected(parser) - _offered(shell, script, path)
        assert not missing, f"{shell}: {path or '(root)'} lacks {sorted(missing)}"
    assert seen_paths == len(command_tree(_root()))


@pytest.mark.parametrize("shell", SHELLS)
def test_a_word_dropped_from_one_command_fails_the_coverage_check(shell: str) -> None:
    """The check above reads each command's own arm: `--json` offered by
    a dozen other commands does not hide its absence from `stats`."""
    script = render(shell, _root())
    assert "--json" in _offered(shell, script, "stats")
    assert "--json" not in _offered(shell, script, "completion")


@pytest.mark.parametrize("shell", SHELLS)
def test_choices_are_offered(shell: str) -> None:
    script = render(shell, _root())
    for client in ("claude-code", "claude-desktop", "cursor", "hermes"):
        assert client in script
    for event in ("session-start", "stop", "prompt"):
        assert event in script
    for name in SHELLS:
        assert name in script


def test_the_new_commands_are_in_the_tree() -> None:
    paths = {command.path for command in command_tree(_root())}
    assert {"stats", "completion", "memory history", "memory move"} <= paths
    history = next(c for c in command_tree(_root()) if c.path == "memory history")
    assert {s for o in history.options for s in o.strings} >= {"--diff", "--json"}


def test_value_kinds_follow_the_metavar_and_the_name() -> None:
    commands = {command.path: command for command in command_tree(_root())}
    export = {o.strings[0]: o for o in commands["export"].options}
    assert export["--output"].value == "file"
    assert export["--mirror"].value == "dir"
    assert export["--scope"].value is None and export["--scope"].takes_value
    assert export["--strict"].takes_value is False
    assert commands["membership import"].positional_value == "file"
    assert commands["completion"].positional_choices == SHELLS
    init = {o.strings[0]: o for o in commands["init"].options}
    assert "cursor" in init["--client"].choices
    assert init["--config-path"].value == "file"
    hook = {o.strings[0]: o for o in commands["hook"].options}
    assert hook["--transcript-path"].value == "file"
    assert hook["--session-id"].value is None
    # A name that only ends in the letters is not a path.
    parser = argparse.ArgumentParser(prog="bettermemory")
    parser.add_argument("--profile")
    parser.add_argument("--cache-dir")
    root = {o.strings[0]: o for o in command_tree(parser)[0].options}
    assert root["--profile"].value is None
    assert root["--cache-dir"].value == "dir"


def _small_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    child = sub.add_parser("alpha", aliases=["al"], help="The alpha command.")
    child.add_argument("--visible", help="Shown.")
    child.add_argument("--secret", help=argparse.SUPPRESS)
    child.add_argument("--path-file", metavar="FILE")
    return parser


@pytest.mark.parametrize("shell", SHELLS)
def test_hidden_options_and_aliases_are_left_out(shell: str) -> None:
    script = render(shell, _small_parser())
    assert "alpha" in script
    assert _option_spelling(shell, "--visible") in script
    assert "secret" not in script
    assert "'al'" not in script and " al " not in script


@pytest.mark.parametrize("shell", SHELLS)
def test_a_flag_added_to_the_parser_is_in_the_next_script(shell: str) -> None:
    parser = _small_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    sub.choices["alpha"].add_argument("--brand-new-flag", action="store_true")
    assert _option_spelling(shell, "--brand-new-flag") in render(shell, parser)


def test_a_name_that_needs_quoting_fails_the_generation() -> None:
    parser = argparse.ArgumentParser(prog="bettermemory")
    parser.add_argument("--mode", choices=["a b"])
    with pytest.raises(ValueError, match="needs shell quoting"):
        render("bash", parser)
    with pytest.raises(ValueError, match="no completion for shell"):
        render("powershell", parser)


def test_summaries_are_quoted_for_zsh_and_fish() -> None:
    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("go", help="It's a 100%% 'quoted' \\ help. Second sentence.")
    zsh = render("zsh", parser)
    assert "'go:It'\\''s a 100% '\\''quoted'\\'' \\ help'" in zsh
    fish = render("fish", parser)
    assert "-d 'It\\'s a 100% \\'quoted\\' \\\\ help'" in fish
    assert "Second sentence" not in zsh + fish


def _run(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


@pytest.mark.parametrize("shell", SHELLS)
def test_cli_prints_the_script(
    shell: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(["completion", shell], monkeypatch) == 0
    out = capsys.readouterr().out
    assert f"{shell} completion for bettermemory" in out
    assert out == render(shell, _root())


def test_cli_refuses_an_unknown_shell(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(["completion", "powershell"], monkeypatch) == 2
    assert "invalid choice" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The shells themselves
# ---------------------------------------------------------------------------


def _shell(name: str) -> str:
    found = shutil.which(name)
    if found is None or sys.platform == "win32":
        pytest.skip(f"{name} is not available here")
    return found


def _write(tmp_path: Path, shell: str) -> Path:
    path = tmp_path / f"bettermemory.{shell}"
    path.write_text(render(shell, _root()), encoding="utf-8")
    return path


_BASH_DRIVER = """
source "$1"
t() {
    COMP_WORDS=("$@")
    COMP_CWORD=$((${#COMP_WORDS[@]} - 1))
    _bettermemory
    echo "${COMPREPLY[*]}"
}
t bettermemory me
t bettermemory memory ''
t bettermemory memory history --
t bettermemory init --client cl
t bettermemory export --scope memory --st
t bettermemory export --output ''
t bettermemory capture grade x --rule 1
"""


def test_bash_accepts_and_completes(tmp_path: Path) -> None:
    bash = _shell("bash")
    script = _write(tmp_path, "bash")
    subprocess.run([bash, "-n", str(script)], check=True)
    driver = tmp_path / "driver.bash"
    driver.write_text(_BASH_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [bash, str(driver), str(script)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == [
        "memory membership",
        "move history -h --help",
        "--help --diff --json",
        "claude-code claude-desktop cline",
        # `memory` after --scope is its value, not the memory command.
        "--strict --store",
        # A path: an empty reply, and readline's file names (-o default).
        "",
        "1 10",
    ]


_ZSH_DRIVER = """
compdef() { :; }
_describe() { print -r -- "$4: ${(P)4}"; }
compadd() { shift; print -r -- "compadd: $*"; }
_files() { print -r -- "files $*"; }
_message() { print -r -- "message $*"; }
source "$1"
t() { words=("$@"); CURRENT=${#words}; _bettermemory; }
t bettermemory memory ''
t bettermemory init --client ''
t bettermemory export --mirror ''
t bettermemory membership import ''
t bettermemory health --days ''
"""


def test_zsh_accepts_and_completes(tmp_path: Path) -> None:
    zsh = _shell("zsh")
    script = _write(tmp_path, "zsh")
    subprocess.run([zsh, "-n", str(script)], check=True)
    driver = tmp_path / "driver.zsh"
    driver.write_text(_ZSH_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [zsh, "-f", str(driver), str(script)],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = result.stdout.splitlines()
    assert lines[0].startswith("subs: move:Move one memory or episode")
    assert "history:Every log row" in lines[0]
    assert (
        lines[1].startswith("opts: -h:show this help message") and "--help:" in lines[1]
    )
    assert lines[2] == (
        "compadd: claude-code claude-desktop cursor continue cline hermes"
    )
    assert lines[3] == "files -/"
    assert "files " in lines and any(line.startswith("opts: ") for line in lines[4:])
    assert lines[-1] == "message value"


def test_fish_accepts(tmp_path: Path) -> None:
    fish = _shell("fish")
    script = _write(tmp_path, "fish")
    subprocess.run([fish, "--no-execute", str(script)], check=True)
