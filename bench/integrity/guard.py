"""The guard on the v1 test split, shared by bench/integrity/run.py
(`collect`) and bench/decisions/decisions_integrity.py.

The test split is read once, after the operating points were frozen on
the dev split. bench/integrity/v1/SEALS.json records that freeze. It is
one JSON object, and the fields read here are:

  dev_sha256               sha256 of the bytes of bench/integrity/v1/dev.json
  test_sha256              sha256 of the bytes of bench/integrity/v1/test.json
  operating_points_sha256  sha256 of the bytes of the operating-points file
                           frozen on the dev split

Each is 64 lowercase hex characters; any other field is carried and not
read, and a file lacking one of the three refuses every test run.
`dev_sha256` and `test_sha256` are compared by `seal_status` (run.py
check reports a v1 split that differs from its seal); `test_sha256` and
`operating_points_sha256` by `guard_run`.

A corpus whose `version` is "v1-test" is refused unless (a) its file's
sha256 equals `test_sha256` and (b) --operating-points names a file whose
sha256 equals `operating_points_sha256`. A run that writes a result is
also refused when its output directory already holds a result for the
same arm (or instrument) on the same corpus sha, unless --rerun-reason
gives a reason: the rerun, its reason and the earlier files are then
recorded in the new result, and the earlier files are kept (the new
result may not overwrite one of them). Every other corpus passes
untouched.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]

SEALS = _HERE / "v1" / "SEALS.json"
TEST_VERSION = "v1-test"
SEAL_FIELDS = {
    "v1-dev": "dev_sha256",
    "v1-test": "test_sha256",
}
OPERATING_POINTS_FIELD = "operating_points_sha256"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class Refused(Exception):
    """The guard refuses the run; the message says why."""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _rel(path: Path) -> str:
    """Relative to the repo when inside it, forward slashes everywhere."""
    resolved = Path(path).resolve()
    if resolved.is_relative_to(_ROOT):
        return resolved.relative_to(_ROOT).as_posix()
    return resolved.as_posix()


def read_seals(seals: Path = SEALS) -> dict[str, str]:
    """The seal fields, each checked to be a sha256; raises Refused when
    the file is missing, unreadable or lacks one."""
    if not Path(seals).is_file():
        raise Refused(f"no seals file at {_rel(seals)}: the test split is not sealed")
    try:
        doc = json.loads(Path(seals).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"seals file {_rel(seals)} is unreadable: {exc}") from exc
    if not isinstance(doc, dict):
        raise Refused(f"seals file {_rel(seals)} is not a JSON object")
    out: dict[str, str] = {}
    for field in (*SEAL_FIELDS.values(), OPERATING_POINTS_FIELD):
        value = doc.get(field)
        if not isinstance(value, str) or not _SHA256.match(value):
            raise Refused(f"seals file {_rel(seals)} has no valid {field}")
        out[field] = value
    return out


def seal_status(corpus: dict[str, Any], sha: str, seals: Path = SEALS) -> list[str]:
    """For `check`: the problem when a v1 split's sha differs from its
    seal. Nothing before the seals file exists, and nothing for v0."""
    field = SEAL_FIELDS.get(str(corpus.get("version")))
    if field is None or not Path(seals).is_file():
        return []
    try:
        sealed = read_seals(seals)[field]
    except Refused as exc:
        return [str(exc)]
    if sealed != sha:
        return [
            f"{corpus['version']} sha {sha} differs from the sealed {field} {sealed}"
        ]
    return []


def earlier_results(directory: Path, arm: str, arm_key: str, sha: str) -> list[Path]:
    """The JSON files in `directory` (not below it) that are results of
    `arm` (read from the `arm_key` field) on corpus sha `sha`."""
    found: list[Path] = []
    if not Path(directory).is_dir():
        return found
    for path in sorted(Path(directory).glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(doc, dict)
            and doc.get(arm_key) == arm
            and doc.get("corpus_sha256") == sha
        ):
            found.append(path)
    return found


def guard_run(
    corpus: dict[str, Any],
    corpus_sha: str,
    *,
    arm: str,
    arm_key: str,
    out: Path | None,
    operating_points: Path | None,
    rerun_reason: str | None,
    seals: Path = SEALS,
) -> dict[str, Any] | None:
    """Pass or refuse a run. None for any corpus but the v1 test split;
    for it, the record the run's output carries (its `test_guard` field),
    or Refused. `out` is the result the run will write, None for a run
    that writes none, which skips the rerun rule."""
    if corpus.get("version") != TEST_VERSION:
        return None
    rerun_reason = (rerun_reason or "").strip() or None
    sealed = read_seals(seals)
    if corpus_sha != sealed["test_sha256"]:
        raise Refused(
            f"the test split's sha {corpus_sha} is not the sealed test_sha256 "
            f"{sealed['test_sha256']}"
        )
    if operating_points is None:
        raise Refused(
            "the test split runs only with --operating-points naming the sealed "
            "operating-points file"
        )
    if not Path(operating_points).is_file():
        raise Refused(f"--operating-points {operating_points} is not a file")
    points_sha = file_sha256(operating_points)
    if points_sha != sealed[OPERATING_POINTS_FIELD]:
        raise Refused(
            f"--operating-points sha {points_sha} is not the sealed "
            f"{OPERATING_POINTS_FIELD} {sealed[OPERATING_POINTS_FIELD]}"
        )
    record: dict[str, Any] = {
        "seals": {"path": _rel(seals), "sha256": file_sha256(seals)},
        "operating_points": {"path": _rel(operating_points), "sha256": points_sha},
    }
    if out is None:
        return record
    earlier = earlier_results(Path(out).parent, arm, arm_key, corpus_sha)
    if earlier and not rerun_reason:
        raise Refused(
            f"{arm} already has a test result on this sha: "
            f"{', '.join(_rel(p) for p in earlier)}; a rerun needs --rerun-reason"
        )
    resolved_out = Path(out).resolve()
    if any(p.resolve() == resolved_out for p in earlier):
        raise Refused(
            f"--out {_rel(out)} is an earlier test result, which is kept; write the "
            "rerun to a new path"
        )
    if rerun_reason:
        record["rerun"] = {
            "reason": rerun_reason,
            "earlier": [_rel(p) for p in earlier],
        }
    return record
