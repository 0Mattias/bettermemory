"""Read ``uv.lock``: the packages it pins and which of them run with the project.

``tools/audit_lock.py`` (the OSV audit) and ``tools/sbom.py`` (the CycloneDX
SBOM and the license check) read the lock through this module, so the two
agree on what "runtime" means. Stdlib only (``tomllib``, Python 3.11+), so
both run on a CI runner's bare ``python3`` with nothing installed. Neither is
run as a module: each is a script beside this file, and Python puts a
script's own directory first on ``sys.path``, which is how ``import _uvlock``
resolves.

The lock's shape (uv's format, ``version = 1``): one ``[[package]]`` table
per resolved distribution, with ``name``, ``version``, ``source`` (a table
with one key saying where it comes from: ``registry``, ``git``, ``url``,
``path``, ``directory``, ``editable`` or ``virtual``), ``sdist`` and
``wheels`` (each a ``url`` or ``path`` with ``hash = "sha256:<hex>"``),
``dependencies`` (each ``{name, version?, source?, extra?, marker?}``),
``optional-dependencies`` (an extra's name to such a list) and
``dev-dependencies`` (a dependency group's name to such a list). A
dependency carries ``version`` and ``source`` only when the lock holds more
than one package of that name (a resolution forked by markers), to say which
one it means.

The runtime set is the closure of the project's own ``dependencies``: every
dependency, and for one that names extras (``mcp`` asks for
``pyjwt[crypto]``), that package's ``optional-dependencies`` for those extras
as well. Markers are taken as satisfiable, so the set is the union over every
platform and Python version: ``pywin32`` is in it although only Windows
installs it. That is the safe side for an audit and an SBOM, and it is the
set ``uv export --no-dev`` writes. Everything else the lock pins (what the
project's extras pull in, here ``dev``, and its dependency groups) is the dev
set.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The lock format this reader understands. uv bumps ``version`` on a change
#: an older reader would misread (``revision`` is for compatible additions),
#: so any other value is refused rather than half-read.
LOCK_VERSION = 1

RUNTIME = "runtime"
DEV = "dev"

#: The index uv writes for a package from PyPI.
PYPI = "https://pypi.org/simple"

#: Source kinds in the order a ``source`` table is read. A registry package
#: is the only kind a vulnerability database or a ``pkg:pypi`` purl can name
#: by version; the rest are the project itself or a checkout of something.
_SOURCE_KINDS = (
    "registry",
    "git",
    "url",
    "path",
    "directory",
    "editable",
    "virtual",
)

_NORMALIZE = re.compile(r"[-_.]+")


class LockError(Exception):
    """The lock cannot be read: missing, not TOML, a format this reader
    does not know, or a dependency naming a package the lock does not hold."""


def normalize(name: str) -> str:
    """A distribution name in PEP 503's normalized form, the form uv writes
    and the form two spellings of one name (``PyYAML``, ``pyyaml``) share."""
    return _NORMALIZE.sub("-", name).lower()


@dataclass(frozen=True)
class Artifact:
    """One file of a package, an sdist or a wheel: where the lock says it is
    and the digest the lock pins for it."""

    location: str
    #: ``(algorithm, hex digest)`` as the lock writes it (``sha256``), or
    #: None for a file the lock records without a hash.
    digest: tuple[str, str] | None


@dataclass(frozen=True)
class Dependency:
    """An edge in the lock: the package it names, the extras it asks of that
    package and the marker it is guarded by (kept for display; the closure
    takes every marker as satisfiable)."""

    name: str
    #: Set only when the lock holds several packages of this name.
    version: str | None
    extras: tuple[str, ...]
    marker: str | None


@dataclass(frozen=True)
class Package:
    """One ``[[package]]`` table."""

    name: str
    version: str
    #: ``(kind, value)``, e.g. ``("registry", "https://pypi.org/simple")``
    #: or ``("editable", ".")``.
    source: tuple[str, str]
    sdist: Artifact | None
    wheels: tuple[Artifact, ...]
    dependencies: tuple[Dependency, ...]
    #: Extra name to its dependencies, as pairs so the dataclass stays
    #: hashable and its order stays the lock's.
    optional: tuple[tuple[str, tuple[Dependency, ...]], ...]
    #: Dependency group name to its dependencies (``dev-dependencies``).
    groups: tuple[tuple[str, tuple[Dependency, ...]], ...]

    @property
    def registry(self) -> bool:
        """True for a package an index serves by name and version."""
        return self.source[0] == "registry"

    @property
    def pypi(self) -> bool:
        """True for a package from PyPI itself, the index whose names a
        vulnerability database's ``PyPI`` ecosystem means."""
        return self.registry and self.source[1].rstrip("/") == PYPI

    @property
    def key(self) -> tuple[str, str, tuple[str, str]]:
        """What tells two packages of one name apart in a forked lock."""
        return (self.name, self.version, self.source)

    def artifacts(self) -> tuple[Artifact, ...]:
        """The sdist (when the lock has one) and then every wheel."""
        return ((self.sdist,) if self.sdist is not None else ()) + self.wheels

    def extra(self, name: str) -> tuple[Dependency, ...]:
        """The dependencies an extra of this package adds; none for an extra
        the lock does not record (uv writes only the extras something asks
        for)."""
        for extra, deps in self.optional:
            if extra == name:
                return deps
        return ()


#: A package's key mapped to the extras the walk activated on it.
Activated = dict[tuple[str, str, tuple[str, str]], frozenset[str]]


@dataclass(frozen=True)
class Lock:
    """Every package the lock pins, in the lock's order (uv sorts by name)."""

    packages: tuple[Package, ...]

    def named(self, name: str) -> list[Package]:
        """Every package of that (normalized) name."""
        wanted = normalize(name)
        return [p for p in self.packages if p.name == wanted]

    def resolve(self, dep: Dependency) -> list[Package]:
        """The package or packages an edge points at. An edge without a
        version in a forked lock points at every fork: the union, in keeping
        with the closure's superset rule."""
        found = self.named(dep.name)
        if dep.version is not None:
            found = [p for p in found if p.version == dep.version]
        if not found:
            shown = dep.name if dep.version is None else f"{dep.name} {dep.version}"
            raise LockError(f"a dependency names {shown}, which the lock does not hold")
        return found

    def root(self, name: str | None = None) -> Package:
        """The project the lock was made for: the package called `name`
        (the ``[project] name`` of its pyproject.toml), or without a name
        the one package whose source is the lock's own directory."""
        if name is not None:
            found = self.named(name)
            if len(found) != 1:
                raise LockError(
                    f"the lock holds {len(found)} packages named {name}, not 1"
                )
            return found[0]
        local = [
            p
            for p in self.packages
            if p.source[0] in ("editable", "virtual") and p.source[1] == "."
        ]
        if len(local) != 1:
            raise LockError(
                f"the lock holds {len(local)} packages sourced from its own directory, not 1"
            )
        return local[0]

    def walk(self, starts: Iterable[Dependency]) -> Activated:
        """The closure of `starts`: each package reached, with the extras
        some edge into it asked for. A package reached again with a new
        extra has that extra's dependencies followed then, so the result
        does not depend on the order edges are met in."""
        activated: dict[tuple[str, str, tuple[str, str]], set[str]] = {}
        queue = list(starts)
        while queue:
            dep = queue.pop()
            for package in self.resolve(dep):
                first = package.key not in activated
                have = activated.setdefault(package.key, set())
                new = [extra for extra in dep.extras if extra not in have]
                if not first and not new:
                    continue
                have.update(new)
                if first:
                    queue.extend(package.dependencies)
                for extra in new:
                    queue.extend(package.extra(extra))
        return {key: frozenset(extras) for key, extras in activated.items()}

    def runtime(self, root: Package) -> Activated:
        """The closure of the root's own dependencies, the root left out."""
        reached = self.walk(root.dependencies)
        reached.pop(root.key, None)
        return reached

    def everything(self, root: Package) -> Activated:
        """The closure of the root's dependencies, every extra of the root
        and every dependency group, the root left out."""
        starts: list[Dependency] = list(root.dependencies)
        for _extra, deps in root.optional:
            starts.extend(deps)
        for _group, deps in root.groups:
            starts.extend(deps)
        reached = self.walk(starts)
        reached.pop(root.key, None)
        return reached

    def sets(self, root: Package) -> dict[tuple[str, str, tuple[str, str]], str]:
        """Every package but the root, labelled ``runtime`` or ``dev``. A
        package no edge reaches (a lock edited by hand) is ``dev``: listed,
        never dropped."""
        runtime = self.runtime(root)
        return {
            p.key: RUNTIME if p.key in runtime else DEV
            for p in self.packages
            if p.key != root.key
        }

    def edges(self, package: Package, extras: Iterable[str]) -> list[Package]:
        """The packages `package` depends on with `extras` active, each
        once, in the lock's order."""
        deps: list[Dependency] = list(package.dependencies)
        for extra in extras:
            deps.extend(package.extra(extra))
        targets: dict[tuple[str, str, tuple[str, str]], Package] = {}
        for dep in deps:
            for target in self.resolve(dep):
                targets.setdefault(target.key, target)
        order = {p.key: index for index, p in enumerate(self.packages)}
        return sorted(targets.values(), key=lambda p: order[p.key])


def _source(table: Any, where: str) -> tuple[str, str]:
    if not isinstance(table, Mapping):
        raise LockError(f"{where}: `source` is not a table")
    for kind in _SOURCE_KINDS:
        if kind in table:
            return (kind, str(table[kind]))
    raise LockError(
        f"{where}: `source` names no kind this reader knows ({sorted(table)})"
    )


def _artifact(table: Any, where: str) -> Artifact:
    if not isinstance(table, Mapping):
        raise LockError(f"{where}: an sdist or wheel entry is not a table")
    location = table.get("url") or table.get("path") or table.get("filename")
    if not isinstance(location, str):
        raise LockError(f"{where}: an sdist or wheel entry has no url or path")
    digest: tuple[str, str] | None = None
    raw = table.get("hash")
    if isinstance(raw, str) and ":" in raw:
        algorithm, _, value = raw.partition(":")
        digest = (algorithm.lower(), value.lower())
    return Artifact(location=location, digest=digest)


def _dependencies(items: Any, where: str) -> tuple[Dependency, ...]:
    if items is None:
        return ()
    if not isinstance(items, list):
        raise LockError(f"{where}: a dependency list is not an array")
    out: list[Dependency] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            raise LockError(f"{where}: a dependency has no name")
        version = item.get("version")
        extras = item.get("extra") or ()
        marker = item.get("marker")
        out.append(
            Dependency(
                name=normalize(item["name"]),
                version=version if isinstance(version, str) else None,
                extras=tuple(str(e) for e in extras),
                marker=marker if isinstance(marker, str) else None,
            )
        )
    return tuple(out)


def _named_lists(
    table: Any, where: str
) -> tuple[tuple[str, tuple[Dependency, ...]], ...]:
    if table is None:
        return ()
    if not isinstance(table, Mapping):
        raise LockError(f"{where}: an extras or groups entry is not a table")
    return tuple(
        (str(name), _dependencies(items, f"{where} [{name}]"))
        for name, items in table.items()
    )


def _package(table: Any, index: int) -> Package:
    if not isinstance(table, Mapping) or not isinstance(table.get("name"), str):
        raise LockError(f"package #{index + 1} has no name")
    name = normalize(table["name"])
    where = f"package {name}"
    source = _source(table.get("source"), where)
    version = table.get("version")
    if not isinstance(version, str):
        # uv omits the version of a local project whose version is dynamic;
        # an index always serves one.
        if source[0] == "registry":
            raise LockError(f"{where}: a registry package has no version")
        version = ""
    sdist = table.get("sdist")
    wheels = table.get("wheels") or []
    if not isinstance(wheels, list):
        raise LockError(f"{where}: `wheels` is not an array")
    return Package(
        name=name,
        version=version,
        source=source,
        sdist=_artifact(sdist, where) if sdist is not None else None,
        wheels=tuple(_artifact(w, where) for w in wheels),
        dependencies=_dependencies(table.get("dependencies"), where),
        optional=_named_lists(table.get("optional-dependencies"), f"{where} extras"),
        groups=_named_lists(table.get("dev-dependencies"), f"{where} groups"),
    )


def parse(text: str) -> Lock:
    """The lock in `text`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LockError(f"not TOML: {exc}") from exc
    if data.get("version") != LOCK_VERSION:
        raise LockError(
            f"lock format version {data.get('version')!r}; this reader knows {LOCK_VERSION}"
        )
    tables = data.get("package") or []
    if not isinstance(tables, list):
        raise LockError("`package` is not an array of tables")
    return Lock(packages=tuple(_package(t, i) for i, t in enumerate(tables)))


def load(path: Path) -> Lock:
    """The lock at `path`; a missing or unreadable file is a LockError."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LockError(f"{path}: {exc.strerror or exc}") from exc
    try:
        return parse(text)
    except LockError as exc:
        raise LockError(f"{path}: {exc}") from exc


def project(pyproject: Path) -> tuple[str, str | None, str | None]:
    """``(name, version, license)`` from a pyproject.toml's ``[project]``
    table. The version is None when it is dynamic; the license is the SPDX
    string form (PEP 639) or the legacy ``{ text = ... }``, None when absent."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LockError(f"{pyproject}: {exc}") from exc
    table = data.get("project")
    if not isinstance(table, Mapping) or not isinstance(table.get("name"), str):
        raise LockError(f"{pyproject}: no [project] name")
    version = table.get("version")
    license_value = table.get("license")
    license_text: str | None = None
    if isinstance(license_value, str):
        license_text = license_value
    elif isinstance(license_value, Mapping) and isinstance(
        license_value.get("text"), str
    ):
        license_text = license_value["text"]
    return (
        normalize(table["name"]),
        version if isinstance(version, str) else None,
        license_text,
    )
