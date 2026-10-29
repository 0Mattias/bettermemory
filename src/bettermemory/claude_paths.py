"""Where Claude Code keeps its files on this host: one answer for `init`,
`doctor`, `posture`, `sandbox_settings` and the transcripts the review
and recall samples read.

Claude Code resolves its home-directory files from one variable.
``CLAUDE_CONFIG_DIR`` moves the configuration directory (``~/.claude``
otherwise), and with it both files bettermemory reads or patches there:
the user settings file and the global config, which holds the user-scope
MCP servers and the per-project registry (``projects``, the local scope).
Claude Code 2.1.296 to 2.1.309 resolve them as follows (read from the
bundled CLI; the names are the minified ones of 2.1.309)::

    config home:   (CLAUDE_CONFIG_DIR ?? join(homedir(), ".claude")).normalize("NFC")   # Le
    userSettings:  join(<config home>, "settings.json")
    globalConfig:  getGlobalClaudeFile() -> V_():
                     <config home>/.config.json, when that legacy file is a
                       regular file and non-empty, or empty, writable and
                       with no .claude${suffix}.json beside it;
                     else join(CLAUDE_CONFIG_DIR || homedir(), `.claude${suffix}.json`)

where the suffix is ``-custom-oauth`` when ``CLAUDE_CODE_CUSTOM_OAUTH_URL``
is set and empty otherwise (a shipped build's environment is "prod"). 2.1.296
takes the legacy file whenever it exists; 2.1.309 also skips it when an
internal predicate holds (``J()``, imported from another chunk and not
identified here). So with the variable set the global config sits inside
the directory, not beside it, and the settings file is the directory's own
``settings.json``. Claude Code's environment-variable documentation says
of the variable that "all settings, session history, and plugins are
stored under this path", and its settings documentation that Claude Code
"then stores your settings, session history, and plugins there instead". A
reader or writer that assumed ``~/.claude.json`` and
``~/.claude/settings.json`` patched a file that Claude Code never reads and
checked files it ignores. The session history moves with the variable
too: Claude Code keeps its transcripts in ``projects`` in the
directory (`transcripts_dir`).

The variable's value is used as written, as Claude Code uses it (its
config home is the raw value): a ``~`` that reaches the process is not
expanded, so ``~/x`` names a directory called ``~`` under the working
directory, and a relative value is joined onto the working directory. A
shell expands an unquoted ``~`` before either program sees it. An empty
value is read as unset here; Claude Code keeps it for the config home
(``??``) and warns about it, and drops it for the global config (``||``).

The project files (``.mcp.json`` and ``.claude/settings*.json`` under the
directory Claude Code starts in) do not move with the variable. The
managed-settings file is at a system path the variable does not move
either, and stays `sandbox_settings.claude_managed_settings_path`'s.

Every function reads the environment and the home directory when called,
so a test that points ``HOME`` or ``CLAUDE_CONFIG_DIR`` at a scratch
directory moves every answer with it.

The settings files and a project's ``.mcp.json`` are read through one
bounded reader, `read_settings_text`: a project's files are whatever the
repository ships, and the audit and doctor read them in every checkout
they run in. The global config is not held to that bound: Claude Code
writes it alone, and it grows with the state it keeps per project.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path

from ._fsutil import bounded_read

#: The variable that moves Claude Code's configuration directory.
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
#: The variable whose presence renames the global config
#: (``.claude-custom-oauth.json``).
CUSTOM_OAUTH_URL_ENV = "CLAUDE_CODE_CUSTOM_OAUTH_URL"
#: The global config's name: user-scope MCP servers, the project registry.
GLOBAL_CONFIG_NAME = ".claude.json"
#: Its name under ``CLAUDE_CODE_CUSTOM_OAUTH_URL``.
CUSTOM_OAUTH_GLOBAL_CONFIG_NAME = ".claude-custom-oauth.json"
#: Every name a shipped Claude Code gives its global config in the home
#: directory or the configuration directory.
GLOBAL_CONFIG_NAMES: tuple[str, ...] = (
    GLOBAL_CONFIG_NAME,
    CUSTOM_OAUTH_GLOBAL_CONFIG_NAME,
)
#: The legacy global config an old install left in the configuration
#: directory, which Claude Code still reads first (`global_config_path`).
LEGACY_GLOBAL_CONFIG_NAME = ".config.json"
#: The settings file's name, in the configuration directory and in a
#: project's ``.claude`` directory.
SETTINGS_NAME = "settings.json"
#: A project's own settings file, kept out of git.
LOCAL_SETTINGS_NAME = "settings.local.json"
#: The directory a project keeps its settings in, and the home directory's
#: default configuration directory.
CLAUDE_DIR_NAME = ".claude"
#: The directory in `config_dir` Claude Code keeps its session
#: transcripts in, one subdirectory per project.
PROJECTS_DIR_NAME = "projects"
#: A project's MCP servers (the project scope).
PROJECT_MCP_NAME = ".mcp.json"
#: The most of a settings file or a project's ``.mcp.json`` a reader takes:
#: small enough that a file a repository ships cannot make the audit or
#: doctor hold more than this, and over a thousand times the block
#: `bettermemory security sandbox` prints (736 bytes for its four
#: directories under a scratch home). How large hand-kept settings files
#: run is not measured; a refused file is named, never misread.
SETTINGS_CAP_BYTES = 1 << 20


def _override(*, nfc: bool) -> Path | None:
    value = os.environ.get(CLAUDE_CONFIG_DIR_ENV)
    if not value:
        return None
    if nfc:
        value = unicodedata.normalize("NFC", value)
    return Path(value).absolute()


def config_dir_override() -> Path | None:
    """The directory ``CLAUDE_CONFIG_DIR`` names, as written (a ``~`` in it
    is not expanded: Claude Code keeps it literal), NFC-normalised as Claude
    Code normalises its config home, and made absolute against the working
    directory; None when it is unset or empty. Claude Code joins a relative
    value onto its own working directory each time it reads it; the
    absolute spelling names the same place for a process started there."""
    return _override(nfc=True)


def config_dir() -> Path:
    """Claude Code's configuration directory: ``$CLAUDE_CONFIG_DIR`` when
    set, ``~/.claude`` otherwise, NFC-normalised as Claude Code's own."""
    override = config_dir_override()
    if override is not None:
        return override
    return Path(unicodedata.normalize("NFC", str(Path.home() / CLAUDE_DIR_NAME)))


def oauth_suffix() -> str:
    """The suffix Claude Code puts in its global config's name:
    ``-custom-oauth`` when ``CLAUDE_CODE_CUSTOM_OAUTH_URL`` is set, empty
    otherwise."""
    return "-custom-oauth" if os.environ.get(CUSTOM_OAUTH_URL_ENV) else ""


def legacy_config_path() -> Path:
    """The legacy global config, ``.config.json`` in `config_dir`."""
    return config_dir() / LEGACY_GLOBAL_CONFIG_NAME


def global_config_path() -> Path:
    """The global config Claude Code reads (``getGlobalClaudeFile``): the
    legacy ``.config.json`` in `config_dir` when it is a regular file and
    either non-empty, or empty, writable by someone and with no
    ``.claude`` json beside it; otherwise ``.claude.json``
    (``.claude-custom-oauth.json`` under ``CLAUDE_CODE_CUSTOM_OAUTH_URL``)
    in ``$CLAUDE_CONFIG_DIR`` when the variable is set, in the home
    directory otherwise. Its ``mcpServers`` are the user scope and
    ``projects[<dir>].mcpServers`` the local scope."""
    override = _override(nfc=False)
    base = override if override is not None else Path.home()
    current = base / f".claude{oauth_suffix()}.json"
    legacy = legacy_config_path()
    try:
        found = os.stat(legacy)
    except (OSError, ValueError):
        return current
    if stat.S_ISREG(found.st_mode) and (
        found.st_size > 0 or (found.st_mode & 0o222 and not os.path.exists(current))
    ):
        return legacy
    return current


def is_global_config(path: Path) -> bool:
    """Whether `path` is a Claude Code global config, the file Claude Code
    locks for every write: one named as a global config is named
    (`GLOBAL_CONFIG_NAMES`, in this configuration directory or another),
    or the file `global_config_path` resolves to here, the legacy
    ``.config.json`` among them."""
    if path.name in GLOBAL_CONFIG_NAMES:
        return True
    return _same_path(path, global_config_path())


def transcripts_dir() -> Path:
    """Where Claude Code keeps its session transcripts: ``projects`` in
    `config_dir`, so under ``$CLAUDE_CONFIG_DIR`` when it is set."""
    return config_dir() / PROJECTS_DIR_NAME


def user_settings_path() -> Path:
    """The user settings file, ``settings.json`` in `config_dir`."""
    return config_dir() / SETTINGS_NAME


def project_settings_paths(project: Path) -> tuple[Path, Path]:
    """A project's shared and local settings files, in the order Claude
    Code layers them over the user's: ``.claude/settings.json``, then
    ``.claude/settings.local.json``."""
    base = project / CLAUDE_DIR_NAME
    return base / SETTINGS_NAME, base / LOCAL_SETTINGS_NAME


def project_mcp_path(project: Path) -> Path:
    """A project's MCP servers file, ``.mcp.json`` at its root."""
    return project / PROJECT_MCP_NAME


def read_settings_text(path: Path, cap: int = SETTINGS_CAP_BYTES) -> str:
    """The text of a settings file or a project's ``.mcp.json``, at most
    `cap` bytes (`SETTINGS_CAP_BYTES`), decoded as UTF-8. Raises
    FileNotFoundError when the file is not there, ValueError when it is
    over the cap, not a regular file (a FIFO, a link to a terminal) or not
    UTF-8 (UnicodeDecodeError), OSError when it cannot be read. A file over
    the cap is refused from its size, and one that is not a regular file
    from its type, never read (`_fsutil.bounded_read`)."""
    return bounded_read(path, cap).decode("utf-8")


def _same_path(a: Path, b: Path | None) -> bool:
    if b is None:
        return False
    if a == b:
        return True
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(
            os.path.abspath(b)
        )


def settings_for_mcp_config(config_path: Path) -> Path:
    """The settings file whose permission rules apply where the MCP config
    at `config_path` registers a server.

    The global config this host resolves (`global_config_path`) pairs
    with the user settings (`user_settings_path`). Another global config
    pairs with the user settings of its own layout: ``~/.claude.json`` with
    ``~/.claude/settings.json``, and ``<dir>/.claude.json`` with
    ``<dir>/settings.json`` when ``<dir>`` is a configuration directory.
    Claude Code writes its ``.claude`` json in the home directory or in the
    directory ``CLAUDE_CONFIG_DIR`` names and nowhere else, so one found
    anywhere but the home directory belongs to a configuration directory,
    named by this shell's variable or by another's; a legacy
    ``.config.json`` is only ever in one. Any other file (a project's
    ``.mcp.json``, a file passed as ``--config-path``) pairs with the
    ``.claude/settings.json`` beside it. The settings file is never derived
    from the MCP file's parent alone: under ``CLAUDE_CONFIG_DIR`` that names
    ``<dir>/.claude/settings.json``, which Claude Code does not read."""
    parent = config_path.parent
    if _same_path(config_path, global_config_path()):
        return user_settings_path()
    if config_path.name in GLOBAL_CONFIG_NAMES:
        in_home = _same_path(parent, Path.home())
        if in_home and not _same_path(parent, config_dir_override()):
            return parent / CLAUDE_DIR_NAME / SETTINGS_NAME
        return parent / SETTINGS_NAME
    if config_path.name == LEGACY_GLOBAL_CONFIG_NAME:
        return parent / SETTINGS_NAME
    return parent / CLAUDE_DIR_NAME / SETTINGS_NAME


__all__ = [
    "CLAUDE_CONFIG_DIR_ENV",
    "CLAUDE_DIR_NAME",
    "CUSTOM_OAUTH_GLOBAL_CONFIG_NAME",
    "CUSTOM_OAUTH_URL_ENV",
    "GLOBAL_CONFIG_NAME",
    "GLOBAL_CONFIG_NAMES",
    "LEGACY_GLOBAL_CONFIG_NAME",
    "LOCAL_SETTINGS_NAME",
    "PROJECTS_DIR_NAME",
    "PROJECT_MCP_NAME",
    "SETTINGS_CAP_BYTES",
    "SETTINGS_NAME",
    "config_dir",
    "config_dir_override",
    "global_config_path",
    "is_global_config",
    "legacy_config_path",
    "oauth_suffix",
    "project_mcp_path",
    "project_settings_paths",
    "read_settings_text",
    "settings_for_mcp_config",
    "transcripts_dir",
    "user_settings_path",
]
