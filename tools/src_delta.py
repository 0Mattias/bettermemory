#!/usr/bin/env python3
"""Measure a unit's net line delta under ``src/`` for its size prediction.

A declaration names the commit it was written against and predicts the
unit's ``src/`` delta with a MISSED-if bound. This prints the count that
prediction is graded on, so the grade is the same however it is run:

    tools/src_delta.py BASE [HEAD] [--missed-if N] [--files]

Lines are git's numstat counts (binary files are skipped), net is added
minus removed. With ``--missed-if`` the exit status is 1 when net exceeds
the bound, else 0.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def numstat(base: str, head: str) -> list[tuple[int, int, str]]:
    out = subprocess.run(
        ["git", "diff", "--numstat", f"{base}..{head}", "--", "src"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    rows = []
    for line in out.splitlines():
        added, removed, path = line.split("\t", 2)
        if added == "-":
            continue
        rows.append((int(added), int(removed), path))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Net src/ line delta of a unit.")
    parser.add_argument("base")
    parser.add_argument("head", nargs="?", default="HEAD")
    parser.add_argument("--missed-if", type=int, metavar="N")
    parser.add_argument("--files", action="store_true", help="per-file rows")
    args = parser.parse_args(argv)

    rows = numstat(args.base, args.head)
    added = sum(r[0] for r in rows)
    removed = sum(r[1] for r in rows)
    net = added - removed
    if args.files:
        for a, r, path in sorted(rows, key=lambda r: r[1] - r[0]):
            print(f"{a - r:+6d}  +{a}/-{r}  {path}")
    print(
        f"src {args.base}..{args.head}: {len(rows)} files, +{added}/-{removed}, net {net:+d}"
    )
    if args.missed_if is not None:
        missed = net > args.missed_if
        print(f"MISSED-if over {args.missed_if:+d}: {'MISSED' if missed else 'within'}")
        return 1 if missed else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
