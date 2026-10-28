#!/usr/bin/env python3
"""A deterministic CycloneDX 1.5 SBOM from uv.lock, and a license policy check.

    python3 tools/sbom.py [--lock uv.lock] [--scope all|runtime] [--out FILE]
    python tools/sbom.py --licenses [--lock uv.lock] [--scope runtime|all]
        [--policy tools/license-policy.toml] [--format text|json]

The SBOM (the default mode) describes what the lock pins, not what one
machine installed: every registry package with its ``pkg:pypi`` purl, its
version, a ``distribution`` reference per sdist and wheel carrying the
SHA-256 the lock pins for that file (the convention ``uv export --format
cyclonedx1.5`` uses), and its scope: ``required`` for the runtime closure
(``tools/_uvlock.py`` says what that is), ``optional`` for the rest, which is
what the project's ``dev`` extra pulls in. The root component is the project
at the version its pyproject.toml declares, and the ``dependencies`` section
carries the graph. ``--scope runtime`` leaves the dev packages out.

The output is a function of the lock and the pyproject alone: components and
references are sorted, the serial number is a UUID derived from the content,
and there is no timestamp unless ``SOURCE_DATE_EPOCH`` is set, in which case
it is that instant. Two runs over one commit are byte-identical, so the SBOM
can be rebuilt and compared rather than taken on trust. Stdlib only, so the
release workflow runs it on the runner's ``python3``. Exit status 0 when
written, 2 when the lock or pyproject cannot be read.

``--licenses`` checks the license each locked package declares, read from
the installed environment's metadata (``importlib.metadata``), against
``tools/license-policy.toml``. Run it with the project's interpreter after
``uv sync --locked`` (``.venv/bin/python tools/sbom.py --licenses``); it
refuses (exit 2) an interpreter that does not have the project installed,
since every package would otherwise read "not installed" and nothing would be
checked. A package's license is its ``License-Expression`` (PEP 639), else a
recognised legacy ``License`` field, else its ``License ::`` classifiers.
Each license must be on the policy's allow list (an ``OR`` needs one allowed
branch, an ``AND`` needs every part); a GPL-family license is denied, and
anything else is unknown. The policy may record the license of a package
this environment cannot read (one only another platform installs) and may
accept a named package outright, each with a reason. Exit status 0 when every
package passes, 1 on a denied, unknown or unchecked license, 2 on a usage or
policy error. ``.github/workflows/audit.yml`` runs the check.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import tomllib
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import _uvlock

SPEC_VERSION = "1.5"


#: CycloneDX's names for the digests a lock can carry.
_HASH_ALGORITHMS = {
    "md5": "MD5",
    "sha1": "SHA-1",
    "sha256": "SHA-256",
    "sha384": "SHA-384",
    "sha512": "SHA-512",
}

#: SPDX identifiers the root component's license may be written as with
#: CycloneDX's ``license.id`` (a schema enum; an id outside it fails
#: validation). Anything else is written as a ``name``.
_KNOWN_SPDX = frozenset(
    {
        "0BSD",
        "AGPL-3.0-only",
        "AGPL-3.0-or-later",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC0-1.0",
        "GPL-2.0-only",
        "GPL-2.0-or-later",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "ISC",
        "LGPL-2.1-only",
        "LGPL-2.1-or-later",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "MIT",
        "MIT-0",
        "MPL-2.0",
        "PSF-2.0",
        "Python-2.0",
        "Unlicense",
    }
)


class UsageError(Exception):
    """A run that cannot proceed: exit status 2."""


# ---------------------------------------------------------------- the SBOM


def purl(package: _uvlock.Package) -> str | None:
    """The package URL of a registry package (``pkg:pypi/<name>@<version>``,
    with ``repository_url`` when the index is not PyPI); None for any other
    source, which no ``pkg:pypi`` purl names."""
    if not package.registry:
        return None
    name = urllib.parse.quote(package.name, safe="-._")
    version = urllib.parse.quote(package.version, safe="-._~")
    base = f"pkg:pypi/{name}@{version}"
    index = package.source[1].rstrip("/")
    if index != _uvlock.PYPI:
        base += "?repository_url=" + urllib.parse.quote(index, safe="")
    return base


def _ref(package: _uvlock.Package) -> str:
    return purl(package) or f"{package.name}@{package.version}"


def _hashes(artifact: _uvlock.Artifact) -> list[dict[str, str]]:
    if artifact.digest is None or artifact.digest[0] not in _HASH_ALGORITHMS:
        return []
    algorithm, digest = artifact.digest
    return [{"alg": _HASH_ALGORITHMS[algorithm], "content": digest}]


def _component(package: _uvlock.Package, scope: str) -> dict[str, Any]:
    component: dict[str, Any] = {
        "type": "library",
        "bom-ref": _ref(package),
        "name": package.name,
        "version": package.version,
        "scope": "required" if scope == _uvlock.RUNTIME else "optional",
    }
    found = purl(package)
    if found is not None:
        component["purl"] = found
    references = []
    for artifact in sorted(package.artifacts(), key=lambda a: a.location):
        reference: dict[str, Any] = {"type": "distribution", "url": artifact.location}
        hashes = _hashes(artifact)
        if hashes:
            reference["hashes"] = hashes
        references.append(reference)
    if references:
        component["externalReferences"] = references
    if not package.registry:
        kind, where = package.source
        component["properties"] = [{"name": "uv:source", "value": f"{kind}:{where}"}]
    return component


def _root_license(text: str | None) -> list[dict[str, Any]]:
    if not text:
        return []
    text = text.strip()
    if text in _KNOWN_SPDX:
        return [{"license": {"id": text}}]
    try:
        node = parse_expression(text)
    except LicenseSyntaxError:
        return [{"license": {"name": text}}]
    if node[0] == "id":
        return [{"license": {"name": text}}]
    return [{"expression": text}]


def _timestamp() -> str | None:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None or raw == "":
        return None
    try:
        instant = dt.datetime.fromtimestamp(int(raw), tz=dt.timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise UsageError(f"SOURCE_DATE_EPOCH={raw!r} is not a Unix timestamp") from exc
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def build(
    lock: _uvlock.Lock,
    root: _uvlock.Package,
    *,
    version: str,
    license_text: str | None,
    scope: str = "all",
) -> dict[str, Any]:
    """The SBOM as a JSON-ready mapping, in the key order it is written."""
    if scope == _uvlock.RUNTIME:
        activated = lock.runtime(root)
        root_deps = list(root.dependencies)
    else:
        activated = lock.everything(root)
        root_deps = list(root.dependencies)
        for _name, deps in (*root.optional, *root.groups):
            root_deps.extend(deps)
    sets = lock.sets(root)
    included = [
        p
        for p in lock.packages
        if p.key != root.key
        and (scope != _uvlock.RUNTIME or sets[p.key] == _uvlock.RUNTIME)
    ]
    # Every package of the chosen scope is listed, a package nothing reaches
    # included: the SBOM accounts for the whole lock, not only its graph.
    keys = {p.key for p in included}
    components = sorted(
        (_component(p, sets[p.key]) for p in included),
        key=lambda c: (c["name"], c["version"], c["bom-ref"]),
    )

    root_purl = (
        f"pkg:pypi/{urllib.parse.quote(root.name, safe='-._')}@"
        + urllib.parse.quote(version, safe="-._~")
    )
    root_component: dict[str, Any] = {
        "type": "application",
        "bom-ref": root_purl,
        "name": root.name,
        "version": version,
        "purl": root_purl,
    }
    licenses = _root_license(license_text)
    if licenses:
        root_component["licenses"] = licenses

    root_targets: dict[tuple[str, str, tuple[str, str]], _uvlock.Package] = {}
    for dep in root_deps:
        for target in lock.resolve(dep):
            if target.key in keys:
                root_targets.setdefault(target.key, target)
    graph = [
        {"ref": root_purl, "dependsOn": sorted(_ref(p) for p in root_targets.values())}
    ]
    for package in included:
        targets = [
            t
            for t in lock.edges(package, activated.get(package.key, frozenset()))
            if t.key in keys
        ]
        graph.append(
            {"ref": _ref(package), "dependsOn": sorted({_ref(t) for t in targets})}
        )
    graph.sort(key=lambda entry: (entry["ref"] != root_purl, entry["ref"]))

    metadata: dict[str, Any] = {}
    stamp = _timestamp()
    if stamp is not None:
        metadata["timestamp"] = stamp
    metadata["tools"] = {
        "components": [
            {"type": "application", "group": root.name, "name": "tools/sbom.py"}
        ]
    }
    metadata["component"] = root_component

    body: dict[str, Any] = {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        "version": 1,
        "metadata": metadata,
        "components": components,
        "dependencies": graph,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    serial = uuid.uuid5(
        uuid.NAMESPACE_URL, "urn:sha256:" + hashlib.sha256(canonical).hexdigest()
    )
    return {
        "bomFormat": body["bomFormat"],
        "specVersion": body["specVersion"],
        "serialNumber": f"urn:uuid:{serial}",
        "version": body["version"],
        "metadata": metadata,
        "components": components,
        "dependencies": graph,
    }


def render(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2) + "\n"


# ------------------------------------------------------------ the licenses

Node = tuple[Any, ...]


class LicenseSyntaxError(ValueError):
    """Text that is not an SPDX license expression."""


_TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9.+:\-]+)")
_OPERATORS = {"AND", "OR", "WITH"}


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(text):
        if text[position:].strip() == "":
            break
        match = _TOKEN.match(text, position)
        if match is None:
            raise LicenseSyntaxError(f"unexpected {text[position:].strip()[:20]!r}")
        tokens.append(match.group(1))
        position = match.end()
    return tokens


def parse_expression(text: str) -> Node:
    """An SPDX license expression as a tree: ``("id", license, exception)``,
    ``("and", [nodes])`` or ``("or", [nodes])``. AND binds tighter than OR,
    as the SPDX grammar says. Operators are read in any case, since
    lower-case ``or`` appears in the wild; anything that is not an
    expression (free text, a pasted license) is a LicenseSyntaxError."""
    tokens = _tokens(text)
    if not tokens:
        raise LicenseSyntaxError("empty")
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str:
        nonlocal position
        if position >= len(tokens):
            raise LicenseSyntaxError("ends early")
        position += 1
        return tokens[position - 1]

    def operand() -> Node:
        token = take()
        if token == "(":
            node = disjunction()
            if take() != ")":
                raise LicenseSyntaxError("unbalanced parenthesis")
            return node
        if token == ")" or token.upper() in _OPERATORS:
            raise LicenseSyntaxError(f"unexpected {token!r}")
        exception = None
        following = peek()
        if following is not None and following.upper() == "WITH":
            take()
            exception = take()
            if exception in ("(", ")") or exception.upper() in _OPERATORS:
                raise LicenseSyntaxError(f"unexpected {exception!r} after WITH")
        return ("id", token, exception)

    def conjunction() -> Node:
        parts = [operand()]
        while (token := peek()) is not None and token.upper() == "AND":
            take()
            parts.append(operand())
        return parts[0] if len(parts) == 1 else ("and", parts)

    def disjunction() -> Node:
        parts = [conjunction()]
        while (token := peek()) is not None and token.upper() == "OR":
            take()
            parts.append(conjunction())
        return parts[0] if len(parts) == 1 else ("or", parts)

    node = disjunction()
    if position != len(tokens):
        raise LicenseSyntaxError(f"unexpected {tokens[position]!r}")
    return node


ALLOWED = "allowed"
DENIED = "denied"
UNKNOWN = "unknown"
ACCEPTED = "accepted"
UNCHECKED = "unchecked"

#: How bad a verdict is, for combining the parts of an expression.
_RANK = {ALLOWED: 0, UNKNOWN: 1, DENIED: 2}


@dataclass(frozen=True)
class PackageRule:
    """A ``[packages.<name>]`` table of the policy."""

    reason: str
    #: The license to judge when this environment's metadata cannot say:
    #: the package is not installed here, or names nothing recognised.
    license: str | None = None
    #: Accept the package whatever it declares.
    accept: bool = False


@dataclass(frozen=True)
class Policy:
    allow: frozenset[str]
    deny: tuple[str, ...]
    packages: Mapping[str, PackageRule] = field(default_factory=dict)

    def leaf(self, license_id: str) -> str:
        """One identifier's verdict: on the allow list, in a denied family
        (the family itself or any ``<family>-`` id: GPL covers GPL-3.0-only
        and GPL-2.0+), or unknown. Case is ignored, and so is the deprecated
        ``+`` (or later) suffix for the allow list."""
        upper = license_id.upper()
        for family in self.deny:
            if upper == family or upper.startswith(family + "-"):
                return DENIED
        if upper.rstrip("+") in self.allow:
            return ALLOWED
        return UNKNOWN

    def judge(self, node: Node) -> str:
        """An expression's verdict. ``X WITH exception`` is judged as X: an
        exception widens a grant, it does not change the family."""
        if node[0] == "id":
            return self.leaf(str(node[1]))
        parts = [self.judge(child) for child in node[1]]
        if node[0] == "or":
            return min(parts, key=_RANK.__getitem__)
        return max(parts, key=_RANK.__getitem__)


def load_policy(path: Path) -> Policy:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UsageError(f"{path}: {exc}") from exc
    table = data.get("policy")
    if not isinstance(table, Mapping):
        raise UsageError(f"{path}: no [policy] table")
    allow = table.get("allow")
    deny = table.get("deny", [])
    if (
        not isinstance(allow, list)
        or not all(isinstance(a, str) for a in allow)
        or not allow
    ):
        raise UsageError(f"{path}: [policy] allow must be a non-empty list of strings")
    if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
        raise UsageError(f"{path}: [policy] deny must be a list of strings")
    packages: dict[str, PackageRule] = {}
    raw_packages = data.get("packages", {})
    if not isinstance(raw_packages, Mapping):
        raise UsageError(f"{path}: [packages] must be a table of tables")
    for name, rule in raw_packages.items():
        where = f"{path}: [packages.{name}]"
        if not isinstance(rule, Mapping):
            raise UsageError(f"{where} is not a table")
        unknown_keys = set(rule) - {"license", "accept", "reason"}
        if unknown_keys:
            raise UsageError(f"{where}: unknown keys {sorted(unknown_keys)}")
        reason = rule.get("reason")
        license_text = rule.get("license")
        accept = rule.get("accept", False)
        if not isinstance(reason, str) or not reason.strip():
            raise UsageError(f"{where} has no reason")
        if license_text is not None:
            if not isinstance(license_text, str):
                raise UsageError(f"{where}: license must be a string")
            try:
                parse_expression(license_text)
            except LicenseSyntaxError as exc:
                raise UsageError(f"{where}: license {license_text!r}: {exc}") from exc
        if not isinstance(accept, bool):
            raise UsageError(f"{where}: accept must be true or false")
        if license_text is None and not accept:
            raise UsageError(f"{where} sets neither license nor accept")
        packages[_uvlock.normalize(name)] = PackageRule(
            reason=reason.strip(), license=license_text, accept=accept
        )
    return Policy(
        allow=frozenset(a.upper() for a in allow),
        deny=tuple(d.upper() for d in deny),
        packages=packages,
    )


class Metadata(Protocol):
    """The one read made of a distribution's core metadata, which both what
    ``importlib.metadata`` returns and an ``email.message.Message`` answer.
    (``get`` joined ``importlib.metadata``'s protocol only in 3.12.)"""

    def get_all(self, name: str, failobj: None = None) -> list[Any] | None: ...


def _field(meta: Metadata, name: str) -> str | None:
    values = meta.get_all(name)
    return str(values[0]) if values else None


#: Legacy ``License`` field values read as an SPDX identifier, keyed by the
#: value lower-cased with its whitespace collapsed. "BSD" alone does not say
#: which clause count is meant, so it stays the family name "BSD", which the
#: policy lists on its own; an Apache license with no version is read as 2.0,
#: the only version in use on PyPI.
_LEGACY = {
    "mit": "MIT",
    "mit license": "MIT",
    "mit licence": "MIT",
    "the mit license": "MIT",
    "the mit license (mit)": "MIT",
    "expat": "MIT",
    "mit-0": "MIT-0",
    "bsd": "BSD",
    "bsd license": "BSD",
    "bsd licence": "BSD",
    "new bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "modified bsd": "BSD-3-Clause",
    "modified bsd license": "BSD-3-Clause",
    "revised bsd license": "BSD-3-Clause",
    "3-clause bsd": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "bsd-3": "BSD-3-Clause",
    "simplified bsd": "BSD-2-Clause",
    "2-clause bsd": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "bsd-2": "BSD-2-Clause",
    "freebsd": "BSD-2-Clause",
    "apache": "Apache-2.0",
    "apache 2": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "apache2": "Apache-2.0",
    "apache license": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "asl 2.0": "Apache-2.0",
    "isc": "ISC",
    "isc license": "ISC",
    "iscl": "ISC",
    "psf": "PSF-2.0",
    "psfl": "PSF-2.0",
    "psf license": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "mpl 2.0": "MPL-2.0",
    "mpl2": "MPL-2.0",
    "mpl-2": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "gpl": "GPL",
    "gnu gpl": "GPL",
    "gnu general public license": "GPL",
    "gplv2": "GPL-2.0",
    "gplv2+": "GPL-2.0-or-later",
    "gplv3": "GPL-3.0",
    "gplv3+": "GPL-3.0-or-later",
    "lgpl": "LGPL",
    "gnu lgpl": "LGPL",
    "lgplv2": "LGPL-2.0",
    "lgplv2+": "LGPL-2.0-or-later",
    "lgplv2.1": "LGPL-2.1",
    "lgplv3": "LGPL-3.0",
    "lgplv3+": "LGPL-3.0-or-later",
    "agpl": "AGPL",
    "agplv3": "AGPL-3.0",
    "agplv3+": "AGPL-3.0-or-later",
}

#: Trove license classifiers (their last ``::`` segment) read as an SPDX
#: identifier or family. An unlisted one becomes a ``LicenseRef-`` id, which
#: no policy allows, so it reads unknown rather than passing.
_CLASSIFIERS = {
    "MIT License": "MIT",
    "MIT No Attribution License (MIT-0)": "MIT-0",
    "BSD License": "BSD",
    "Zero-Clause BSD (0BSD)": "0BSD",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Mozilla Public License 1.1 (MPL 1.1)": "MPL-1.1",
    "Mozilla Public License 1.0 (MPL)": "MPL-1.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "GNU General Public License (GPL)": "GPL",
    "GNU General Public License v2 (GPLv2)": "GPL-2.0",
    "GNU General Public License v2 or later (GPLv2+)": "GPL-2.0-or-later",
    "GNU General Public License v3 (GPLv3)": "GPL-3.0",
    "GNU General Public License v3 or later (GPLv3+)": "GPL-3.0-or-later",
    "GNU Library or Lesser General Public License (LGPL)": "LGPL",
    "GNU Lesser General Public License v2 (LGPLv2)": "LGPL-2.0",
    "GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.0-or-later",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0",
    "GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0-or-later",
    "GNU Affero General Public License v3": "AGPL-3.0",
    "GNU Affero General Public License v3 or later (AGPLv3+)": "AGPL-3.0-or-later",
}


def _legacy(value: str) -> str | None:
    text = " ".join(value.split())
    if not text or text.upper() in ("UNKNOWN", "NONE") or "\n" in value.strip():
        return None
    if text.casefold() in _LEGACY:
        return _LEGACY[text.casefold()]
    if len(text) > 100:
        return None
    try:
        parse_expression(text)
    except LicenseSyntaxError:
        return None
    return text


def _classifier(value: str) -> str | None:
    parts = [p.strip() for p in value.split("::")]
    # A bare "License :: OSI Approved" names no license.
    if len(parts) < 2 or parts[0] != "License" or parts[-1] == "OSI Approved":
        return None
    last = parts[-1]
    if last in _CLASSIFIERS:
        return _CLASSIFIERS[last]
    return "LicenseRef-" + re.sub(r"[^A-Za-z0-9.]+", "-", last).strip("-")


def declared(meta: Metadata) -> tuple[str | None, str | None]:
    """``(license expression, where it came from)`` for one distribution's
    metadata: the ``License-Expression`` field, else a ``License`` field
    that names a license (a pasted license text does not), else the license
    classifiers joined with OR (two classifiers mean a choice). ``(None,
    None)`` when the metadata names no license at all."""
    expression = _field(meta, "License-Expression")
    if expression is not None and expression.strip():
        # A folded header keeps its line break; the expression is the same
        # on one line, and the report prints it as one.
        return " ".join(expression.split()), "License-Expression"
    legacy = _field(meta, "License")
    if legacy is not None:
        found = _legacy(legacy)
        if found is not None:
            return found, "License"
    named: list[str] = []
    for classifier in meta.get_all("Classifier") or ():
        found = _classifier(str(classifier))
        if found is not None and found not in named:
            named.append(found)
    if named:
        return " OR ".join(named), "Classifier"
    return None, None


@dataclass
class LicenseResult:
    name: str
    version: str
    set: str
    installed: str | None
    license: str | None
    source: str | None
    verdict: str
    note: str = ""

    @property
    def failed(self) -> bool:
        return self.verdict in (DENIED, UNKNOWN, UNCHECKED)

    def as_json(self) -> dict[str, Any]:
        return {
            "package": self.name,
            "version": self.version,
            "set": self.set,
            "installed": self.installed,
            "license": self.license,
            "source": self.source,
            "verdict": self.verdict,
            "note": self.note,
        }


def _judge_text(policy: Policy, text: str) -> str:
    try:
        return policy.judge(parse_expression(text))
    except LicenseSyntaxError:
        return UNKNOWN


def judge_package(
    policy: Policy,
    name: str,
    version: str,
    set_label: str,
    meta: Metadata | None,
) -> LicenseResult:
    """One package's license verdict. Metadata that names a license the
    policy recognises (allowed or denied) always decides, so a release that
    changes its license is caught even when the policy records an older
    one; the policy's recorded license decides only where the metadata
    cannot (not installed here, or nothing recognised). ``accept`` then
    turns a denied or unknown verdict into an accepted one."""
    rule = policy.packages.get(name)
    installed = None
    license_text: str | None = None
    source: str | None = None
    verdict = UNCHECKED
    notes: list[str] = []
    if meta is not None:
        installed = _field(meta, "Version")
        license_text, source = declared(meta)
        verdict = UNKNOWN if license_text is None else _judge_text(policy, license_text)
        if installed is not None and installed != version:
            notes.append(f"installed {installed}, the lock pins {version}")
    if (
        verdict in (UNKNOWN, UNCHECKED)
        and rule is not None
        and rule.license is not None
    ):
        license_text, source = rule.license, "policy"
        verdict = _judge_text(policy, rule.license)
        notes.append(f"recorded by the policy: {rule.reason}")
    elif verdict in (DENIED, UNKNOWN, UNCHECKED) and rule is not None and rule.accept:
        notes.append(f"accepted by the policy: {rule.reason}")
    if verdict in (DENIED, UNKNOWN, UNCHECKED) and rule is not None and rule.accept:
        verdict = ACCEPTED
    if verdict == UNCHECKED:
        notes.append(
            "not installed in this environment and the policy records no license"
        )
    return LicenseResult(
        name=name,
        version=version,
        set=set_label,
        installed=installed,
        license=license_text,
        source=source,
        verdict=verdict,
        note="; ".join(notes),
    )


def installed_metadata(name: str) -> Metadata | None:
    """This interpreter's metadata for a distribution, None when it is not
    installed. ``importlib.metadata`` normalizes the name for the lookup."""
    try:
        return importlib.metadata.distribution(name).metadata
    except importlib.metadata.PackageNotFoundError:
        return None


def check_licenses(
    lock: _uvlock.Lock,
    root: _uvlock.Package,
    policy: Policy,
    *,
    scope: str = _uvlock.RUNTIME,
    lookup: Callable[[str], Metadata | None] | None = None,
) -> list[LicenseResult]:
    """Every package of the scope judged against the policy, in the lock's
    order. `lookup` reads one distribution's metadata; by default this
    interpreter's (`installed_metadata`)."""
    if lookup is None:
        lookup = installed_metadata
    if lookup(root.name) is None:
        raise UsageError(
            f"{root.name} is not installed in this interpreter's environment ({sys.prefix}), "
            "so no locked package can be read here; run it with the project's environment "
            "after `uv sync --locked`, e.g. `.venv/bin/python tools/sbom.py --licenses`"
        )
    sets = lock.sets(root)
    results: list[LicenseResult] = []
    for package in lock.packages:
        if package.key == root.key:
            continue
        label = sets[package.key]
        if scope == _uvlock.RUNTIME and label != _uvlock.RUNTIME:
            continue
        results.append(
            judge_package(
                policy, package.name, package.version, label, lookup(package.name)
            )
        )
    return results


_LABELS = {
    ALLOWED: "ok",
    ACCEPTED: "ACCEPTED",
    DENIED: "DENIED",
    UNKNOWN: "UNKNOWN",
    UNCHECKED: "UNCHECKED",
}


def render_licenses(results: Sequence[LicenseResult], header: str) -> str:
    lines = [header]
    width = max((len(f"{r.name} {r.version}") for r in results), default=0)
    for r in results:
        label = f"{r.name} {r.version}".ljust(width)
        shown = f"{r.license} ({r.source})" if r.license else "no license declared"
        line = f"  {_LABELS[r.verdict]:<9} {label}  [{r.set}] {shown}"
        if r.note:
            line += f"  {r.note}"
        lines.append(line)
    failed = [r for r in results if r.failed]
    counts: dict[str, int] = {}
    for r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    summary = ", ".join(f"{n} {verdict}" for verdict, n in sorted(counts.items()))
    lines.append(
        f"result: {'FAIL' if failed else 'ok'}: {summary or 'nothing to check'}"
    )
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------- CLI


def _root(
    lock: _uvlock.Lock, pyproject: Path
) -> tuple[_uvlock.Package, str, str | None]:
    name, version, license_text = _uvlock.project(pyproject)
    root = lock.root(name)
    if version is None:
        version = root.version
    if not version:
        raise UsageError(f"{pyproject}: no [project] version and none in the lock")
    return root, version, license_text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A deterministic CycloneDX SBOM from uv.lock, or a license policy check.",
        epilog="Exit status: 0 written or every license passes, 1 a license fails the "
        "policy, 2 a usage, lock or policy error.",
    )
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=None,
        help="the project's pyproject.toml (default: beside the lock)",
    )
    parser.add_argument(
        "--scope",
        choices=("all", _uvlock.RUNTIME),
        default=None,
        help="the SBOM's default is all, the license check's runtime",
    )
    parser.add_argument(
        "--out", default="-", help="the SBOM's path, - for stdout (default)"
    )
    parser.add_argument(
        "--licenses",
        action="store_true",
        help="check the installed environment's licenses against the policy instead",
    )
    parser.add_argument(
        "--policy", type=Path, default=Path(__file__).with_name("license-policy.toml")
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        lock = _uvlock.load(args.lock)
        root, version, license_text = _root(
            lock, args.pyproject or args.lock.with_name("pyproject.toml")
        )
        if args.licenses:
            return _licenses(args, lock, root)
        document = build(
            lock,
            root,
            version=version,
            license_text=license_text,
            scope=args.scope or "all",
        )
    except (UsageError, _uvlock.LockError) as exc:
        print(f"sbom: {exc}", file=sys.stderr)
        return 2
    text = render(document)
    if args.out == "-":
        sys.stdout.write(text)
    else:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8", newline="\n")
        print(
            f"sbom: wrote {out} ({len(document['components'])} components)",
            file=sys.stderr,
        )
    return 0


def _licenses(
    args: argparse.Namespace, lock: _uvlock.Lock, root: _uvlock.Package
) -> int:
    scope = args.scope or _uvlock.RUNTIME
    results = check_licenses(lock, root, load_policy(args.policy), scope=scope)
    failed = any(r.failed for r in results)
    if args.format == "json":
        payload = {
            "lock": str(args.lock),
            "policy": str(args.policy),
            "scope": scope,
            "packages": [r.as_json() for r in results],
            "status": "fail" if failed else "ok",
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        header = f"licenses: {len(results)} {scope} packages from {args.lock}, policy {args.policy}"
        sys.stdout.write(render_licenses(results, header))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
