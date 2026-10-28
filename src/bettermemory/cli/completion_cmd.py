"""`bettermemory completion bash|zsh|fish`: shell completion generated
from this build's own parser.

The script is built by walking the argparse tree `cli._build_parser()`
returns, the same tree `main()` parses with, so it names every
subcommand, nested group and flag this version has and none it lacks:
a flag added to any subparser is in the next generated script with no
second list to update. Nothing runs at tab time beyond the shell's own
code; argcomplete, which would ask the program on every tab, would be a
runtime dependency and a Python start per keystroke.

What the walk keeps, per command path (``""`` for the root, ``"memory
history"`` for a nested one): the subcommands (an alias of a parser
already listed is left out), every option string whose help is not
suppressed, whether an option takes a value and which (its ``choices``,
a file or a directory when its metavar or name says so), and the choices
of a positional (``hook``'s events, this command's shells). A value no
list can complete (a number, an id) completes to nothing.

How each script finds where the cursor is: it walks the words before the
cursor, skipping options and the value word of an option that takes one,
and extends the command path by every word that names a known path. Each
script then offers the path's subcommands, options and positional
choices, or the values of the option the cursor follows. Paths and
directories: bash leaves them to readline's filename completion (the
script is registered with ``-o default``), zsh calls ``_files``, fish
passes ``-F``.

Every name the scripts embed is checked against a conservative pattern
when the script is generated, so a choice or flag that would need shell
quoting fails the generation (and the tests that run it over the real
parser) instead of producing a script that breaks at tab time. The help
summaries zsh and fish show are quoted for each shell.

Prints only, never writes a file. The install line per shell is in the
command's help.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

SHELLS = ("bash", "zsh", "fish")
PROG = "bettermemory"

# A subcommand name or a choice value, and an option string, as the
# scripts may embed them unquoted inside a single-quoted pattern.
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\Z")
_OPTION = re.compile(r"--?[A-Za-z0-9][A-Za-z0-9_-]*\Z")

# How long a help summary may run in zsh's and fish's menus.
_SUMMARY_CHARS = 72

INSTALL_LINES = (
    'bash: add `eval "$(bettermemory completion bash)"` to ~/.bashrc. '
    "zsh: after compinit in ~/.zshrc, add "
    '`eval "$(bettermemory completion zsh)"`, or save the output as '
    "_bettermemory in a directory on $fpath. fish: "
    "`bettermemory completion fish > ~/.config/fish/completions/bettermemory.fish`."
)


@dataclass(frozen=True)
class Option:
    """One option of a command: its strings, whether it consumes the next
    word as its value, the values it offers, and whether a value is a
    ``file`` or a ``dir`` (None: neither)."""

    strings: tuple[str, ...]
    takes_value: bool
    choices: tuple[str, ...]
    value: str | None
    help: str


@dataclass(frozen=True)
class Command:
    """One command path of the tree and what completes after it."""

    path: str
    help: str
    subcommands: tuple[tuple[str, str], ...]
    options: tuple[Option, ...]
    positional_choices: tuple[str, ...]
    positional_value: str | None


def _value_kind(action: argparse.Action) -> str | None:
    """``dir`` or ``file`` when the action's value is a path, by its
    metavar (``DIR``, ``FILE``, ``PATH``) or its name (``dir``, ``*_dir``,
    and the same for path and file; not ``profile``); None otherwise."""
    metavar = action.metavar.upper() if isinstance(action.metavar, str) else ""
    words = {metavar, *action.dest.upper().split("_")[-1:]}
    if "DIR" in words or metavar.endswith("_DIR"):
        return "dir"
    if words & {"PATH", "FILE"} or metavar.endswith(("_PATH", "_FILE")):
        return "file"
    if isinstance(action.type, argparse.FileType):
        return "file"
    return None


def _choices(action: argparse.Action) -> tuple[str, ...]:
    if action.choices is None:
        return ()
    return tuple(str(choice) for choice in action.choices)


def _check(names: tuple[str, ...], pattern: re.Pattern[str], what: str) -> None:
    for name in names:
        if not pattern.match(name):
            raise ValueError(
                f"{what} {name!r} needs shell quoting; the completion scripts "
                "embed names unquoted"
            )


def _walk(
    parser: argparse.ArgumentParser, path: str, help_text: str, out: list[Command]
) -> None:
    """Append `parser`'s command at `path`, then each of its subcommands'
    in registration order."""
    subcommands: list[tuple[str, str]] = []
    children: list[tuple[str, argparse.ArgumentParser, str]] = []
    options: list[Option] = []
    positional_choices: list[str] = []
    positional_value: str | None = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {
                choice.dest: str(choice.help or "")
                for choice in action._choices_actions
            }
            seen: set[int] = set()
            for name, child in action.choices.items():
                if id(child) in seen:
                    continue
                seen.add(id(child))
                _check((name,), _WORD, "subcommand")
                subcommands.append((name, helps.get(name, "")))
                children.append((name, child, helps.get(name, "")))
            continue
        if action.help == argparse.SUPPRESS:
            continue
        choices = _choices(action)
        _check(choices, _WORD, "choice")
        if action.option_strings:
            strings = tuple(action.option_strings)
            _check(strings, _OPTION, "option")
            options.append(
                Option(
                    strings=strings,
                    takes_value=action.nargs not in (0, "?", "*"),
                    choices=choices,
                    value=_value_kind(action) if action.nargs != 0 else None,
                    help=str(action.help or ""),
                )
            )
            continue
        positional_choices.extend(choices)
        kind = _value_kind(action)
        if kind is not None and positional_value != "file":
            positional_value = kind
    out.append(
        Command(
            path=path,
            help=help_text,
            subcommands=tuple(subcommands),
            options=tuple(options),
            positional_choices=tuple(positional_choices),
            positional_value=positional_value,
        )
    )
    for name, child, child_help in children:
        _walk(child, f"{path} {name}".strip(), child_help, out)


def command_tree(parser: argparse.ArgumentParser) -> list[Command]:
    """Every command path under `parser`, the root first, depth first in
    the order `--help` lists them."""
    out: list[Command] = []
    _walk(parser, "", str(parser.description or ""), out)
    return out


def _summary(text: str) -> str:
    """A help string's first sentence on one line, cut to fit a menu."""
    text = re.sub(r"%\([^)]*\)[a-z]", "", text).replace("%%", "%")
    text = " ".join(text.split())
    end = text.find(". ")
    if end != -1:
        text = text[:end]
    text = text.rstrip(".")
    if len(text) > _SUMMARY_CHARS:
        text = text[: _SUMMARY_CHARS - 3].rstrip() + "..."
    return text


def _version() -> str:
    from .. import __version__

    return str(__version__)


def _header(shell: str) -> list[str]:
    return [
        f"# {shell} completion for {PROG} {_version()}, generated by "
        f"`{PROG} completion {shell}` from the parser of that build.",
        "# Regenerate after an upgrade so new subcommands and flags complete.",
    ]


def _value_patterns(commands: list[Command]) -> list[str]:
    """``path|option`` for every option that consumes the next word."""
    return [
        f"{command.path}|{string}"
        for command in commands
        for option in command.options
        if option.takes_value
        for string in option.strings
    ]


def _paths(commands: list[Command]) -> list[str]:
    return [command.path for command in commands if command.path]


def _candidates(command: Command) -> list[str]:
    words = [name for name, _ in command.subcommands]
    words += list(command.positional_choices)
    words += [string for option in command.options for string in option.strings]
    return words


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------


def _bash(commands: list[Command]) -> str:
    lines = _header("bash")
    lines += [
        "",
        "_bettermemory() {",
        "    local cur prev word cmdpath next skip i",
        "    COMPREPLY=()",
        '    cur="${COMP_WORDS[COMP_CWORD]}"',
        '    prev="${COMP_WORDS[COMP_CWORD-1]}"',
        '    if [ "$prev" = "=" ] && [ "$COMP_CWORD" -gt 1 ]; then',
        '        prev="${COMP_WORDS[COMP_CWORD-2]}"',
        "    fi",
        '    cmdpath=""',
        "    skip=0",
        "    for ((i = 1; i < COMP_CWORD; i++)); do",
        '        word="${COMP_WORDS[i]}"',
        '        if [ "$skip" = 1 ]; then',
        "            skip=0",
        "            continue",
        "        fi",
        '        case "$cmdpath|$word" in',
    ]
    lines += [
        f"            '{pattern}') skip=1; continue ;;"
        for pattern in _value_patterns(commands)
    ]
    lines += [
        "        esac",
        '        case "$word" in',
        "            -*|=) continue ;;",
        "        esac",
        '        if [ -n "$cmdpath" ]; then next="$cmdpath $word"; else next="$word"; fi',
        '        case "$next" in',
    ]
    lines += [
        f"            '{path}') cmdpath=\"$next\" ;;" for path in _paths(commands)
    ]
    lines += [
        "        esac",
        "    done",
        '    case "$cmdpath|$prev" in',
    ]
    for command in commands:
        for option in command.options:
            if not option.takes_value:
                continue
            for string in option.strings:
                pattern = f"'{command.path}|{string}')"
                if option.choices:
                    words = " ".join(option.choices)
                    lines.append(
                        f"            {pattern} "
                        f"COMPREPLY=($(compgen -W '{words}' -- \"$cur\")); return 0 ;;"
                    )
                else:
                    # A path, or a value no list completes: an empty
                    # reply, so `-o default` offers file names.
                    lines.append(f"            {pattern} return 0 ;;")
    lines += [
        "    esac",
        '    case "$cmdpath" in',
    ]
    for command in commands:
        words = " ".join(_candidates(command))
        lines.append(
            f"            '{command.path}') "
            f"COMPREPLY=($(compgen -W '{words}' -- \"$cur\")) ;;"
        )
    lines += [
        "    esac",
        "    return 0",
        "}",
        "",
        f"complete -o default -F _bettermemory {PROG}",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# zsh
# ---------------------------------------------------------------------------


def _zsh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _zsh_entry(name: str, help_text: str) -> str:
    summary = _summary(help_text)
    return _zsh_quote(f"{name}:{summary}" if summary else name)


def _zsh(commands: list[Command]) -> str:
    lines = [f"#compdef {PROG}", *_header("zsh")]
    lines += [
        "",
        "_bettermemory() {",
        "    local cur prev word cmdpath next",
        "    local -i i skip",
        # Not `options`: zsh keeps its shell options in a special array of
        # that name.
        "    local -a subs opts",
        '    cur="${words[CURRENT]}"',
        '    prev="${words[CURRENT-1]}"',
        '    cmdpath=""',
        "    skip=0",
        "    for ((i = 2; i < CURRENT; i++)); do",
        '        word="${words[i]}"',
        "        if (( skip )); then",
        "            skip=0",
        "            continue",
        "        fi",
        '        case "$cmdpath|$word" in',
    ]
    lines += [
        f"            ('{pattern}') skip=1; continue ;;"
        for pattern in _value_patterns(commands)
    ]
    lines += [
        "        esac",
        '        case "$word" in',
        "            (-*) continue ;;",
        "        esac",
        '        if [[ -n "$cmdpath" ]]; then next="$cmdpath $word"; else next="$word"; fi',
        '        case "$next" in',
    ]
    lines += [
        f"            ('{path}') cmdpath=\"$next\" ;;" for path in _paths(commands)
    ]
    lines += [
        "        esac",
        "    done",
        '    case "$cmdpath|$prev" in',
    ]
    for command in commands:
        for option in command.options:
            if not option.takes_value:
                continue
            for string in option.strings:
                pattern = f"('{command.path}|{string}')"
                if option.choices:
                    action = f"compadd -- {' '.join(option.choices)}"
                elif option.value == "dir":
                    action = "_files -/"
                elif option.value == "file":
                    action = "_files"
                else:
                    action = "_message 'value'"
                lines.append(f"            {pattern} {action}; return 0 ;;")
    lines += [
        "    esac",
        '    case "$cmdpath" in',
    ]
    for command in commands:
        subs = [_zsh_entry(name, text) for name, text in command.subcommands]
        subs += [_zsh_quote(choice) for choice in command.positional_choices]
        opts = [
            _zsh_entry(string, option.help)
            for option in command.options
            for string in option.strings
        ]
        lines.append(f"        ('{command.path}')")
        lines.append(f"            subs=({' '.join(subs)})")
        lines.append(f"            opts=({' '.join(opts)})")
        if command.positional_value == "dir":
            lines.append("            _files -/")
        elif command.positional_value == "file":
            lines.append("            _files")
        lines.append("            ;;")
    lines += [
        "    esac",
        "    (( ${#subs} )) && _describe -t commands 'command' subs",
        "    _describe -t options 'option' opts",
        "}",
        "",
        "# Autoloaded from $fpath, the file's body runs as the function: define",
        "# it and complete. Sourced (eval), register it with compinit's compdef.",
        'if [[ "${funcstack[1]}" == "_bettermemory" ]]; then',
        '    _bettermemory "$@"',
        "else",
        f"    compdef _bettermemory {PROG}",
        "fi",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# fish
# ---------------------------------------------------------------------------


def _fish_quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _fish_flags(strings: tuple[str, ...]) -> str:
    out: list[str] = []
    for string in strings:
        if string.startswith("--"):
            out.append(f"-l {string[2:]}")
        elif len(string) == 2:
            out.append(f"-s {string[1:]}")
        else:
            out.append(f"-o {string[1:]}")
    return " ".join(out)


def _fish(commands: list[Command]) -> str:
    lines = _header("fish")
    value_patterns = " ".join(_fish_quote(p) for p in _value_patterns(commands))
    paths = " ".join(_fish_quote(p) for p in _paths(commands))
    lines += [
        "",
        "function __bettermemory_cmdpath",
        f"    set -l value_options {value_patterns}",
        f"    set -l paths {paths}",
        "    set -l tokens (commandline -opc)",
        "    set -e tokens[1]",
        "    set -l cmdpath ''",
        "    set -l skip 0",
        "    for word in $tokens",
        "        if test $skip -eq 1",
        "            set skip 0",
        "            continue",
        "        end",
        '        if contains -- "$cmdpath|$word" $value_options',
        "            set skip 1",
        "            continue",
        "        end",
        "        if string match -q -- '-*' $word",
        "            continue",
        "        end",
        "        set -l next $word",
        '        if test -n "$cmdpath"',
        '            set next "$cmdpath $word"',
        "        end",
        "        if contains -- $next $paths",
        "            set cmdpath $next",
        "        end",
        "    end",
        "    echo $cmdpath",
        "end",
        "",
        "function __bettermemory_at",
        "    set -l here (__bettermemory_cmdpath)",
        '    test "$here" = "$argv[1]"',
        "end",
        "",
        f"complete -c {PROG} -f",
    ]
    for command in commands:
        condition = _fish_quote(f'__bettermemory_at "{command.path}"')
        prefix = f"complete -c {PROG} -n {condition}"
        for name, text in command.subcommands:
            line = f"{prefix} -a {name}"
            summary = _summary(text)
            if summary:
                line += f" -d {_fish_quote(summary)}"
            lines.append(line)
        if command.positional_choices:
            lines.append(
                f"{prefix} -a {_fish_quote(' '.join(command.positional_choices))}"
            )
        if command.positional_value is not None:
            lines.append(f"{prefix} -F")
        for option in command.options:
            line = f"{prefix} {_fish_flags(option.strings)}"
            if option.takes_value:
                if option.choices:
                    line += f" -x -a {_fish_quote(' '.join(option.choices))}"
                elif option.value is not None:
                    line += " -r -F"
                else:
                    line += " -x"
            summary = _summary(option.help)
            if summary:
                line += f" -d {_fish_quote(summary)}"
            lines.append(line)
    return "\n".join(lines) + "\n"


_RENDERERS = {"bash": _bash, "zsh": _zsh, "fish": _fish}


def render(shell: str, parser: argparse.ArgumentParser) -> str:
    """The completion script for `shell` over the tree under `parser`."""
    if shell not in _RENDERERS:
        raise ValueError(f"no completion for shell {shell!r}; one of {SHELLS}")
    return _RENDERERS[shell](command_tree(parser))


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``completion`` subparser."""
    help_text = (
        "Print a completion script for bash, zsh or fish, generated from this "
        "build's own parser, so it completes every subcommand and flag this "
        "version has. Prints only; regenerate it after an upgrade. " + INSTALL_LINES
    )
    parser = sub.add_parser("completion", help=help_text, description=help_text)
    parser.add_argument("shell", choices=SHELLS, help="The shell to complete for.")
    return parser


def run(args: argparse.Namespace, *, root_parser: argparse.ArgumentParser) -> None:
    """Dispatch handler for ``bettermemory completion <shell>``."""
    sys.stdout.write(render(args.shell, root_parser))


__all__ = [
    "INSTALL_LINES",
    "PROG",
    "SHELLS",
    "Command",
    "Option",
    "add_subparser",
    "command_tree",
    "render",
    "run",
]
