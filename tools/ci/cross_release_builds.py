#!/usr/bin/env python3
"""Make the two installs CI's cross-release job runs tests/test_cross_release.py
against (ENG-107), each in a venv of its own under OUT.

    tools/ci/cross_release_builds.py --previous V --exclude-newer T --build B OUT

``OUT/previous``: bettermemory V from PyPI, resolved as of T
(``uv pip install --exclude-newer``), the install a user had on V's day.

``OUT/next``: this checkout, built from a copy of its tracked files in
``OUT/source`` (so no build writes into the checkout), with pyproject.toml's
version set to B when B is not already the version there, and its
dependencies pinned by the checkout's uv.lock (``uv export --frozen``).
tools/ci/previous_release.py prints V, T and B.

Nothing installed is run: each install's version is read from its metadata.
Prints ``BETTERMEMORY_CI_PREVIOUS=...`` and ``BETTERMEMORY_CI_NEXT=...``,
the two ``bettermemory`` executables, for ``$GITHUB_ENV``.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PREVIOUS_ENV = "BETTERMEMORY_CI_PREVIOUS"
NEXT_ENV = "BETTERMEMORY_CI_NEXT"
_VERSION_LINE = re.compile(r'^version = "[^"]*"$', re.MULTILINE)


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    """Run `command`, its output on stderr: stdout holds this script's
    ``NAME=value`` lines alone (``uv export`` prints what it writes)."""
    print("+ " + " ".join(command), file=sys.stderr, flush=True)
    subprocess.run(command, check=True, cwd=cwd, stdout=sys.stderr)


def _bin(venv: Path, name: str) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin") / name


def copy_tracked(destination: Path) -> None:
    """Copy every file git tracks in the checkout, as it is on disk, to
    `destination`. A tracked file deleted from the working tree is left out."""
    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    for name in filter(None, listed.split("\0")):
        source = REPO / name
        if not source.is_file():
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def set_version(pyproject: Path, version: str) -> None:
    """Set `[project] version` in `pyproject` to `version`."""
    text = pyproject.read_text(encoding="utf-8")
    replaced, count = _VERSION_LINE.subn(f'version = "{version}"', text, count=1)
    if count != 1:
        raise SystemExit(f"no version line in {pyproject}")
    pyproject.write_text(replaced, encoding="utf-8")
    with pyproject.open("rb") as fh:
        found = tomllib.load(fh)["project"]["version"]
    if found != version:
        raise SystemExit(f"{pyproject} reads version {found!r}, not {version!r}")


def installed_version(venv: Path) -> str:
    """bettermemory's version as the venv's metadata gives it."""
    return subprocess.run(
        [
            str(_bin(venv, "python")),
            "-I",
            "-c",
            "import importlib.metadata as m; print(m.version('bettermemory'))",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_previous(out: Path, *, version: str, exclude_newer: str, python: str) -> Path:
    venv = out / "previous"
    _run(["uv", "venv", "--python", python, str(venv)])
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(_bin(venv, "python")),
            "--exclude-newer",
            exclude_newer,
            f"bettermemory=={version}",
        ]
    )
    return venv


def make_next(out: Path, *, version: str, python: str) -> Path:
    source = out / "source"
    if source.exists():
        shutil.rmtree(source)
    copy_tracked(source)
    set_version(source / "pyproject.toml", version)
    requirements = out / "requirements.txt"
    _run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
        ],
        cwd=source,
    )
    venv = out / "next"
    _run(["uv", "venv", "--python", python, str(venv)])
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(_bin(venv, "python")),
            "--requirements",
            str(requirements),
            str(source),
        ]
    )
    return venv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--previous", required=True, metavar="V")
    parser.add_argument("--exclude-newer", required=True, metavar="T")
    parser.add_argument("--build", required=True, metavar="B")
    parser.add_argument("--python", default="3.13")
    parser.add_argument("out", metavar="OUT")
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    previous = make_previous(
        out, version=args.previous, exclude_newer=args.exclude_newer, python=args.python
    )
    built = make_next(out, version=args.build, python=args.python)
    for venv, expected in ((previous, args.previous), (built, args.build)):
        found = installed_version(venv)
        if found != expected:
            raise SystemExit(f"{venv} holds bettermemory {found}, not {expected}")
    if args.previous == args.build:
        raise SystemExit(
            f"both installs are bettermemory {args.build}: nothing crosses"
        )
    print(f"{PREVIOUS_ENV}={_bin(previous, 'bettermemory')}")
    print(f"{NEXT_ENV}={_bin(built, 'bettermemory')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
