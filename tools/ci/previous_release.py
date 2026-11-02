#!/usr/bin/env python3
"""The published release CI's cross-release job runs against this checkout
(ENG-107), and the version the checkout is built as beside it.

    tools/ci/previous_release.py [--pypi-json FILE] [--version V] [--skip V]...

The previous release is the newest release on PyPI, not yanked and with a
wheel, in the checkout's major and at or below the checkout's version
(pyproject.toml's ``[project] version``), other than a version a tag on
HEAD names: on a release's own tag the job meets the release before it.
PyPI is the source, not the tags, because some tags never reached it
(v9.0.0rc2 to rc4 and rc8 were tagged and not published).

The checkout is built as its own version, or as the candidate after it
(`next_candidate`) when the release found carries the same version: the
daemon and its clients take two equal versions for one build
(`_daemon_client._keeps`), so between releases nothing would cross.

Versions are ordered by the package's own `_daemon_client.version_key`,
which imports nothing outside the standard library and is read from the
checkout's ``src`` without installing it.

Prints ``key=value`` lines for ``$GITHUB_OUTPUT``: ``version``, the
release; ``uploaded``, its newest wheel's upload time; ``exclude_newer``,
a moment just after that, for ``uv pip install --exclude-newer``, so the
release installs with the dependencies it had on its day; and ``build``,
the version the checkout is built as. Prints nothing, and exits 0, when
the major has no earlier release.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

REPO = Path(__file__).resolve().parents[2]
PYPI_JSON = "https://pypi.org/pypi/bettermemory/json"
#: How far past the wheel's upload `exclude_newer` reaches.
MARGIN = timedelta(minutes=1)
_FETCH_TIMEOUT_SECONDS = 30.0

VersionKey = Callable[[object], tuple[int, ...] | None]


class Release(NamedTuple):
    """A published release: its version and its newest wheel's upload time."""

    version: str
    uploaded: datetime


def version_key() -> VersionKey:
    """The package's `_daemon_client.version_key`, from the checkout's src."""
    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from bettermemory._daemon_client import version_key as key

    return key


def _uploaded(file: Mapping[str, Any]) -> datetime:
    text = str(file["upload_time_iso_8601"]).replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def previous_release(
    document: Mapping[str, Any], *, current: str, skip: Iterable[str] = ()
) -> Release | None:
    """The newest release in `document` (PyPI's JSON for the project) that
    has a wheel not yanked, is in `current`'s major, is at or below
    `current`, and is none of `skip`; None when there is none. Raises
    ValueError for a `current` the version key cannot place. A release
    whose version the key cannot place is passed over."""
    key = version_key()
    ours = key(current)
    if ours is None:
        raise ValueError(f"cannot place the checkout's version {current!r}")
    skipped = {placed for placed in map(key, skip) if placed is not None}
    best: tuple[tuple[int, ...], Release] | None = None
    for version, files in dict(document.get("releases") or {}).items():
        theirs = key(version)
        if theirs is None or theirs in skipped:
            continue
        if theirs[0] != ours[0] or theirs > ours:
            continue
        wheels = [
            file
            for file in files or ()
            if file.get("packagetype") == "bdist_wheel" and not file.get("yanked")
        ]
        if not wheels:
            continue
        if best is None or theirs > best[0]:
            best = (theirs, Release(version, max(map(_uploaded, wheels))))
    return None if best is None else best[1]


def next_candidate(version: str) -> str:
    """The candidate after `version`, as tests/test_shim_versions.py's
    `next_candidate` gives it: `X.Y.ZrcN` gives `X.Y.Zrc(N+1)`, a final
    `X.Y.Z` gives `X.Y.(Z+1)`, a dev release the release it leads to.
    Raises ValueError for a version this cannot place."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?(?:\.dev(\d+))?", version)
    if match is None:
        raise ValueError(f"no candidate follows {version!r}")
    major, minor, patch, rc, dev = match.groups()
    base = f"{major}.{minor}.{patch}"
    if dev is not None:
        return f"{base}rc{rc if rc is not None else 1}"
    if rc is not None:
        return f"{base}rc{int(rc) + 1}"
    return f"{major}.{minor}.{int(patch) + 1}"


def build_version(current: str, release: Release) -> str:
    """The version the checkout is built as beside `release`: its own, or
    the candidate after it when the two are one version."""
    key = version_key()
    return next_candidate(current) if key(current) == key(release.version) else current


def exclude_newer(release: Release) -> str:
    """A moment just after `release`'s wheel was uploaded, in the form
    ``uv pip install --exclude-newer`` reads."""
    moment = release.uploaded.replace(microsecond=0) + timedelta(seconds=1) + MARGIN
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def output_lines(current: str, release: Release | None) -> list[str]:
    """What the script prints for `release`: nothing for None."""
    if release is None:
        return []
    return [
        f"version={release.version}",
        f"uploaded={release.uploaded.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"exclude_newer={exclude_newer(release)}",
        f"build={build_version(current, release)}",
    ]


def checkout_version() -> str:
    with (REPO / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def head_tags() -> list[str]:
    """The versions the tags on HEAD name, each without its leading v."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "tag", "--points-at", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [tag.removeprefix("v") for tag in out.split()]


def fetch(url: str = PYPI_JSON) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        document: dict[str, Any] = json.load(response)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--pypi-json", metavar="FILE", help="Read PyPI's JSON from FILE, not PyPI."
    )
    parser.add_argument(
        "--version", help="The checkout's version (default: pyproject.toml's)."
    )
    parser.add_argument(
        "--skip",
        action="append",
        metavar="V",
        help="A version to pass over (default: the versions HEAD's tags name).",
    )
    args = parser.parse_args(argv)
    current = args.version or checkout_version()
    skip = args.skip if args.skip is not None else head_tags()
    if args.pypi_json:
        document = json.loads(Path(args.pypi_json).read_text(encoding="utf-8"))
    else:
        document = fetch()
    try:
        lines = output_lines(
            current, previous_release(document, current=current, skip=skip)
        )
    except ValueError as exc:
        print(f"previous_release: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
