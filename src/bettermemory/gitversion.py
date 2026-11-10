"""The oldest git bettermemory supports, and the version of the one on PATH
(ENG-227).

Every read of a repository runs the git on PATH (`_git.run_git`) or reads
what it would, and some of those reads turn on its release: whether it
takes a ``hasconfig:remote.*.url:`` include (2.36), the words
``branch.autoSetupMerge`` takes (2.35, 2.37), the format-1 extensions it
knows (2.44, 2.48, 2.54), ``config.worktree`` under a format below -1
(2.42), a tab or a CR inside a value (2.45), ``git var GIT_CONFIG_SYSTEM``
(2.42), ``GIT_NO_LAZY_FETCH`` (2.44). `gitsystem` asks the binary its
version with its other questions (`gitsystem.Sources.version`, and the
hasconfig include of its own), and a read whose rule turns on the release
declines where the git is older or has named none. `MINIMUM` is the oldest
release the git-reading suites were run against, built from its source,
where every read stood: right, or a decline; on an older one some reads
are not known to. It was chosen by running those suites, not by reading
git's source (docs/operations.md, "Supported git").

`installed` asks the git on PATH its version once (``git version``), for
`bettermemory doctor`'s git check.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass

from . import _git as _git_runner

#: The oldest git version bettermemory supports, (major, minor).
MINIMUM: tuple[int, int] = (2, 34)
#: How long `installed` waits for ``git version``: a first run of Apple's
#: ``/usr/bin/git`` shim resolves the developer directory, which a warm
#: one does not; 13 ms is a warm run's median (ENG-207).
VERSION_TIMEOUT_SECONDS = 5.0

_VERSION = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")


def text(version: tuple[int, ...]) -> str:
    """`version` spelled as git spells it: ``2.34``, ``2.39.5``."""
    return ".".join(str(part) for part in version)


def parse(output: str) -> tuple[int, int, int] | None:
    """The (major, minor, patch) ``git version`` prints first, patch 0
    where it names none (``2.56.GIT``, a build between releases): a
    vendor's suffix (``(Apple Git-157)``, ``.windows.1``) is read past.
    None for output that names no version."""
    found = _VERSION.match(output.strip())
    if found is None:
        return None
    major, minor, patch = found.groups()
    return int(major), int(minor), int(patch or 0)


@dataclass(frozen=True)
class Installed:
    """The git on PATH: where it is (`path`, None where there is none),
    what ``git version`` printed (`output`, None where it did not run or
    exited non-zero) and the version read from it (`version`)."""

    path: str | None
    output: str | None
    version: tuple[int, int, int] | None

    @property
    def supported(self) -> bool | None:
        """Whether `version` is `MINIMUM` or later; None where it is not
        known."""
        if self.version is None:
            return None
        return self.version[:2] >= MINIMUM


def installed() -> Installed:
    """The git on PATH and its version, asked once (``git version``)."""
    path = shutil.which("git")
    if path is None:
        return Installed(None, None, None)
    done = _git_runner.run_git(["version"], timeout=VERSION_TIMEOUT_SECONDS)
    if done is None or done.returncode != 0:
        return Installed(path, None, None)
    output = done.stdout.strip()
    return Installed(path, output, parse(output))
