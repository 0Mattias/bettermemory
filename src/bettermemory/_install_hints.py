"""Install-command spellings — the one module that owns them.

Every surface that tells a user how to add or repair an optional extra
composes its command from these atoms. The spelling used to live in
hand-written per-surface copies (``doctor``'s fix hints plus two
surfaces in the pre-4.0 semantic lane), and per-surface copies of one
command string are exactly the drift that shipped an unquoted,
zsh-refused install command in the first place — each surface got fixed
on its own schedule. ``tests/test_install_hints.py`` greps shipped
source and pins each spelling to this module, so a new hand-written
copy fails CI instead of drifting.

Two arguments carry every spelling choice below, hoisted here from
``doctor`` where they were first won:

- **The tool form leads.** ``docs/installation.md`` documents ``uv tool
  install 'bettermemory[<extra>]'`` as the install path, and ``uv pip``
  writes to the ACTIVE virtualenv rather than to a tool environment —
  so a ``uv pip`` spelling repairs nothing for the ``uv tool install``
  or ``pipx`` user, which is most of the population these messages talk
  to. The virtualenv and development-clone forms follow as parenthetical
  variants, never lead.
- **The extras spec is quoted.** ``[`` is a glob character, and zsh —
  macOS's default shell — refuses an unquoted ``bettermemory[ui]``
  outright ("no matches found"), so an unquoted spelling is a repair
  instruction that does not run.

Import-free on purpose (pinned by test): ``doctor``, ``web``, ``llm``,
and ``cli.ui`` all import this at module level, and a module with no
imports of its own keeps every placement unconditionally cheap.

The atoms are bare of backticks — the caller owns the prose around a
command, and the surfaces deliberately compose different prose (the
``ui`` CLI stacks the command on its own line; the web banner inlines
it backticked mid-sentence). The two ``*_command`` forms at the bottom
are the fully-backticked compositions ``doctor`` binds.
"""


def extras_spec(extra: str) -> str:
    """The quoted package-plus-extra spec, e.g. ``'bettermemory[ui]'``.

    Pre-quoted because ``[`` globs — see the module docstring; every
    command atom below embeds the spec through here so no caller can
    reintroduce the unquoted form.
    """
    return f"'bettermemory[{extra}]'"


def tool_reinstall(extra: str) -> str:
    """The leading form: a ``--reinstall`` of the ``uv tool`` environment.

    A tool environment is repaired by reinstalling the tool, so this one
    spelling serves both the absent extra and the installed-but-broken
    one — which is why it leads everywhere and the variants below only
    follow (the environment argument in the module docstring).
    """
    return f"uv tool install --reinstall {extras_spec(extra)}"


def pipx_force(extra: str) -> str:
    """`tool_reinstall` for the pipx-managed install: ``--force`` is
    pipx's respelling of the same repair."""
    return f"pipx install --force {extras_spec(extra)}"


def pip_force_reinstall(module: str) -> str:
    """Force-reinstall of the damaged ``module`` in the ACTIVE virtualenv.

    Reaches the module only when the virtualenv that runs bettermemory
    is also the active one — true for a self-managed venv install, false
    for the tool environments above — so it follows `tool_reinstall` as
    a variant rather than leading.
    """
    return f"uv pip install --force-reinstall {module}"


def dev_clone_editable(extra: str) -> str:
    """Editable install of the extra from a development clone.

    The spec is quoted here too — ``".[<extra>]"`` — for the reason
    `extras_spec` documents: an unquoted ``.[ui]`` is not pasteable
    into zsh.
    """
    return f'uv pip install -e ".[{extra}]"'


def install_extra_command(extra: str) -> str:
    """The full ADD-an-extra instruction, backticked for prose.

    Tool form leading, pipx and development-clone variants in the
    parenthetical. ``doctor`` binds this as ``_install_extra_command``
    and ships it verbatim in fix hints.
    """
    return (
        f"`{tool_reinstall(extra)}` "
        f"(pipx: `{pipx_force(extra)}`; from a "
        f"development clone: `{dev_clone_editable(extra)}`)"
    )


def reinstall_extra_command(module: str, extra: str) -> str:
    """The full REPAIR-a-broken-extra instruction, backticked for prose.

    Same shape as `install_extra_command`, but the second parenthetical
    variant repairs the damaged ``module`` itself: the broken thing is a
    dependency of the extra, not the package. ``doctor`` binds this as
    ``_reinstall_extra_command``.
    """
    return (
        f"`{tool_reinstall(extra)}` "
        f"(pipx: `{pipx_force(extra)}`; inside "
        f"the virtualenv that runs bettermemory: "
        f"`{pip_force_reinstall(module)}`)"
    )
