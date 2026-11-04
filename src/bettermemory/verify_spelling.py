"""Which spelling of a path claim drift reads (ENG-220's review, round C).

A tree stores a name as the git that committed it read it: decomposed where
a git on macOS did not precompose, composed elsewhere. A claim or a cited
path is read under its own spelling wherever the tree holds it, in the
head's tree or among the paths the window changed (a file deleted there);
only where the tree holds neither is it read under every other spelling of
its name (`claims.stored_spellings`). A tree that stores both spellings as
two files (twins) then reads each spec on its own file, and a single file
stored in the other form still matches. Past the spellings bound
(`claims.spellings_capped`), a path the tree holds in none of its whole
forms is not looked for in the mixed ones: the read declines, and the hit
keeps the conservative count, never not-applicable.

`literal_pathspecs` spells the chosen names for git (``:(literal)``).

This module lives apart from `verify.py` to keep it under the package's
2,000-line bound. A leaf of the verify family: from the package it imports
`claims`, `origin` and `origin_history`, and `verify` imports it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import origin, origin_history
from .claims import spellings_capped, stored_spellings


def git_bool(value: str | None) -> bool:
    """A boolean environment variable as git reads it: unset or empty is
    false, ``true`` / ``yes`` / ``on`` and a nonzero integer are true."""
    if value is None:
        return False
    word = value.strip().lower()
    if word in ("true", "yes", "on"):
        return True
    try:
        return int(word) != 0
    except ValueError:
        return False


def literal_pathspecs(names: Sequence[str]) -> list[str]:
    """`names` spelled so git reads each as the path it names, never as a
    pattern: the ``:(literal)`` magic on each (ENG-183).

    A name is a repository path (`origin.resolve_repo_pathspecs`, a
    claimed path), and git reads a plain one as a pattern: ``src/foo[1].py``
    also matched ``src/foo1.py``, and a file named ``:(glob)**`` matched
    every commit, so the author-date logs over-counted where the reachable
    walk (`ReachableWalk.shas_touching`, which compares paths) did not,
    and the two bases disagreed about one memory. Every log, phantom check
    and patch stream claim drift runs takes its names through here, the
    spellings `Spelled.names` chose; the walk compares the same names.

    Where the environment already makes git read every pathspec literally
    (``GIT_LITERAL_PATHSPECS``), git reads no magic either and would take
    the prefix as part of the name, so the names pass as they stand."""
    if git_bool(os.environ.get("GIT_LITERAL_PATHSPECS")):
        return list(names)
    return [f":(literal){name}" for name in names]


@dataclass(frozen=True)
class Spelled:
    """The spellings each spec is read under (`spelled`): a spec not in
    `chosen` has the one, its own; and whether a spec past the spellings
    bound was found in none of its forms, so the read `declined`."""

    chosen: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    declined: bool = False

    def of(self, spec: str) -> tuple[str, ...]:
        """The spellings `spec` is read under, its own first."""
        return self.chosen.get(spec, (spec,))

    def names(self, specs: Sequence[str]) -> list[str]:
        """Every spelling `specs` are read under, in order, once each."""
        return list(dict.fromkeys(name for spec in specs for name in self.of(spec)))


def spelled(
    cwd: Path,
    specs: Sequence[str],
    *,
    toplevel: Path | None,
    head: str,
    touched: Mapping[str, object] | None = None,
    since: datetime | None = None,
) -> Spelled:
    """The spellings to read `specs` under, for the window that ends at
    `head` (a hash, or ``HEAD`` where the logs read it): each spec its own
    where `head`'s tree holds it or the window changed it, and otherwise
    every spelling of its name. The window's changed paths are the walk's
    (`touched`) on the reachable basis; on dates, a spec the tree does not
    hold is changed in the window where a commit authored after `since`
    touched it. A spec with one spelling (ASCII, or a name normalisation
    leaves as it is) costs nothing; the others cost one ``cat-file`` for
    them all, and on dates one log for each the tree does not hold.

    Where git does not answer, a spec is read under every spelling, as
    before the exact spelling was preferred: twins may over-count there,
    never under. A spec past the spellings bound that neither the tree nor
    the window holds in any whole form declines the read."""
    variant = [
        spec
        for spec in dict.fromkeys(specs)
        if len(stored_spellings(spec)) > 1 or spellings_capped(spec)
    ]
    if not variant:
        return Spelled()
    if toplevel is None:
        toplevel = origin.repo_toplevel(cwd)
    looked = {
        name
        for spec in variant
        for name in (stored_spellings(spec) if spellings_capped(spec) else (spec,))
    }
    held = None if toplevel is None else _held(toplevel, head, looked)
    chosen: dict[str, tuple[str, ...]] = {}
    declined = False
    for spec in variant:
        if (held is not None and spec in held) or _changed(
            cwd, spec, toplevel, touched, since
        ):
            chosen[spec] = (spec,)
        elif not spellings_capped(spec):
            chosen[spec] = stored_spellings(spec)
        else:
            present = tuple(n for n in stored_spellings(spec) if held and n in held)
            chosen[spec] = present
            declined = declined or not present
    return Spelled(chosen, declined)


def _held(toplevel: Path, head: str, names: set[str]) -> set[str] | None:
    """The names among `names` that `head`'s tree holds, a file or a
    folder, from one ``cat-file --batch-check`` (each ``<head>:<name>``
    on its standard input, so no argument is precomposed); None where git
    did not answer. A name holding a line break cannot be asked and is
    not held."""
    asked = sorted(name for name in names if "\n" not in name and "\r" not in name)
    if not asked:
        return set()
    raw = origin._git(
        toplevel,
        *origin_history.AS_STORED_PATHS,
        "cat-file",
        "--batch-check",
        timeout=5.0,
        empty_ok=True,
        stdin="".join(f"{head}:{name}\n" for name in asked).encode("utf-8"),
    )
    if raw is None:
        return None
    lines = raw.split("\n")
    if len(lines) != len(asked):
        return None
    return {
        name
        for name, line in zip(asked, lines)
        if len(line.split()) == 3 and origin.is_full_commit_sha(line.split()[0])
    }


def _changed(
    cwd: Path,
    spec: str,
    toplevel: Path | None,
    touched: Mapping[str, object] | None,
    since: datetime | None,
) -> bool:
    """Whether the window changed `spec` under its own spelling: a path the
    walk lists, or one below it for a folder; on dates, a commit authored
    after `since` that touched it."""
    if touched is not None:
        prefix = spec.rstrip("/") + "/"
        return spec in touched or any(path.startswith(prefix) for path in touched)
    if since is None:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    pairs = origin.commit_author_sha_pairs_touching_pathspecs(
        cwd, literal_pathspecs([spec]), toplevel=toplevel
    )
    return pairs is not None and bool(pairs) and pairs[-1][0] > since


__all__ = ["Spelled", "git_bool", "literal_pathspecs", "spelled"]
