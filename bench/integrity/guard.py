"""The guard on the test splits, shared by bench/integrity/run.py
(`collect`, a table-2 `score`), bench/decisions/decisions_integrity.py
and bench/decisions/review_integrity.py.

There are two test splits, each read once and each with its own seals
file (TEST_SEALS): v1's ("v1-test", bench/integrity/v1/SEALS.json) and
v1b's ("v1b-test", bench/integrity/v1b/SEALS.json), a fresh held-out split
authored later with v1's machinery (bench/integrity/v1b/gen.py). v1b has
no dev split: its seal names v1's dev split as dev_sha256 and v1's
operating points as operating_points_sha256, which it reuses, and the
review's seals below serve both. Everything said of "the test split"
below holds for each, against its own seals file; `is_test_version`
names them. A caller that passes no `seals` gets the corpus version's
own file.

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

A corpus whose `version` is a test version is refused unless (a) its file's
sha256 equals `test_sha256` and (b) --operating-points names a file whose
sha256 equals `operating_points_sha256`. A run that writes a result is
also refused when its output directory already holds a result for the
same arm (or instrument) on the same corpus sha, unless --rerun-reason
gives a reason: the rerun, its reason and the earlier files are then
recorded in the new result, and the earlier files are kept (the new
result may not overwrite one of them). Every other corpus passes
untouched.

The write review (unit D1) adds a second seal file beside SEALS.json,
bench/integrity/v1/SEALS-review.json, written by
bench/decisions/review_integrity.py seal. Its fields:

  operating_points_sha256  sha256 of operating-points-review.json
  questions_sha256         sha256 of the canonical JSON of the review's
                           questions (bettermemory.review.QUESTIONS)
  prompt_sha256            sha256 of bench/decisions/prompts/review-judge.md
  constants_sha256         sha256 of the canonical JSON of the related_for
                           constants

A test run whose --operating-points names operating-points-review.json
has that file's sha checked against SEALS-review.json, while the split's
own sha is still checked against SEALS.json's test_sha256. The chat-review
instrument runs on the test split only so, and only when its question,
prompt and constants shas equal SEALS-review.json's.

The chat family. The chat instrument (claude-opus-5.5-in-session), the
chat-review instrument, its rescore (chat-review-rescore, unit D1b) and an
export count as one family of reads: the judges read the test split at the
export, and a rescore reads their answers again. An earlier result or export
stamp of any of them on the test sha makes a later one need
--rerun-reason, and a reason already recorded by an earlier one does not
count again. An export (`export=True`) is held to the rerun rule although
it writes no result; its stamp is what a later run finds.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]

SEALS = _HERE / "v1" / "SEALS.json"
SEALS_V1B = _HERE / "v1b" / "SEALS.json"
# v1's test version, kept for the callers that name it.
TEST_VERSION = "v1-test"
# Every read-once test split, by corpus version, with its seals file.
TEST_SEALS: dict[str, Path] = {
    TEST_VERSION: SEALS,
    "v1b-test": SEALS_V1B,
}
SEAL_FIELDS = {
    "v1-dev": "dev_sha256",
    "v1-test": "test_sha256",
    "v1b-test": "test_sha256",
}
OPERATING_POINTS_FIELD = "operating_points_sha256"
# The fields a test run needs, in the order a missing one is reported.
SEALED_FIELDS = ("dev_sha256", "test_sha256", OPERATING_POINTS_FIELD)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# The write review's seals (unit D1).
SEALS_REVIEW = _HERE / "v1" / "SEALS-review.json"
REVIEW_POINTS_NAME = "operating-points-review.json"
REVIEW_SHA_FIELDS = ("questions_sha256", "prompt_sha256", "constants_sha256")
REVIEW_SEAL_FIELDS = (OPERATING_POINTS_FIELD, *REVIEW_SHA_FIELDS)

# The chat family of reads: the chat instrument, the chat-review instrument,
# its rescore and an export (decisions_integrity.py --instrument export).
CHAT_INSTRUMENT = "claude-opus-5.5-in-session"
REVIEW_INSTRUMENT = "chat-review"
RESCORE_INSTRUMENT = "chat-review-rescore"
EXPORT_ARM = "export"
CHAT_FAMILY = frozenset(
    {CHAT_INSTRUMENT, REVIEW_INSTRUMENT, RESCORE_INSTRUMENT, EXPORT_ARM}
)
EXPORT_STAMP_KIND = "export_stamp"


class Refused(Exception):
    """The guard refuses the run; the message says why."""


def is_test_version(version: Any) -> bool:
    """Whether a corpus `version` names a read-once test split."""
    return isinstance(version, str) and version in TEST_SEALS


def seals_for(version: Any) -> Path:
    """The seals file of a corpus version: a test split's own, v1's for
    every other version (v1 dev's sha is sealed there)."""
    return TEST_SEALS.get(version, SEALS) if isinstance(version, str) else SEALS


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _rel(path: Path) -> str:
    """Relative to the repo when inside it, forward slashes everywhere."""
    resolved = Path(path).resolve()
    if resolved.is_relative_to(_ROOT):
        return resolved.relative_to(_ROOT).as_posix()
    return resolved.as_posix()


def read_seals(
    seals: Path = SEALS, fields: tuple[str, ...] | None = None
) -> dict[str, str]:
    """The seal fields, each checked to be a sha256; raises Refused when
    the file is missing, unreadable or lacks one. `fields` narrows the
    fields required (by default all three: a test run needs them all)."""
    if not Path(seals).is_file():
        raise Refused(f"no seals file at {_rel(seals)}: the test split is not sealed")
    try:
        doc = json.loads(Path(seals).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"seals file {_rel(seals)} is unreadable: {exc}") from exc
    if not isinstance(doc, dict):
        raise Refused(f"seals file {_rel(seals)} is not a JSON object")
    out: dict[str, str] = {}
    for field in fields or SEALED_FIELDS:
        value = doc.get(field)
        if not isinstance(value, str) or not _SHA256.match(value):
            raise Refused(f"seals file {_rel(seals)} has no valid {field}")
        out[field] = value
    return out


def read_review_seals(seals: Path = SEALS_REVIEW) -> dict[str, str]:
    """SEALS-review.json's four fields, each checked to be a sha256;
    raises Refused when the file is missing, unreadable or lacks one."""
    if not Path(seals).is_file():
        raise Refused(
            f"no review seals file at {_rel(seals)}: the review points are not sealed"
        )
    return read_seals(seals, fields=REVIEW_SEAL_FIELDS)


def is_review_points(path: Path | None) -> bool:
    """Whether --operating-points names the review's points file."""
    return path is not None and Path(path).name == REVIEW_POINTS_NAME


def check_review_shas(
    shas: Mapping[str, str] | None, sealed: Mapping[str, str]
) -> dict[str, str]:
    """The run's question, prompt and constants shas, refused unless each
    equals the one SEALS-review.json holds."""
    if not shas:
        raise Refused(
            "a chat-review run on the test split must carry its question, prompt "
            "and constants shas"
        )
    out: dict[str, str] = {}
    for field in REVIEW_SHA_FIELDS:
        value = shas.get(field)
        if value != sealed[field]:
            raise Refused(
                f"the run's {field} {value} is not the sealed {field} {sealed[field]}"
            )
        out[field] = str(value)
    return out


def seal_status(
    corpus: dict[str, Any], sha: str, seals: Path | None = None
) -> list[str]:
    """For `check`: the problem when a v1 or v1b split's sha differs from
    its seal (in `seals`, by default the version's own file). Nothing
    before the seals file exists, and nothing for v0. Only the split's own
    field is required: the corpus is sealed before any arm runs, and the
    operating points' sha joins v1's seals after the dev runs."""
    version = corpus.get("version")
    field = SEAL_FIELDS.get(str(version))
    seals = Path(seals) if seals is not None else seals_for(version)
    if field is None or not seals.is_file():
        return []
    try:
        sealed = read_seals(seals, fields=(field,))[field]
    except Refused as exc:
        return [str(exc)]
    if sealed != sha:
        return [
            f"{corpus['version']} sha {sha} differs from the sealed {field} {sealed}"
        ]
    return []


def _same_read(doc: dict[str, Any], arm: str, arm_key: str) -> bool:
    """Whether `doc` is a read by `arm`: the same arm, or, for the chat
    family, any member of it or an export stamp."""
    if doc.get(arm_key) == arm:
        return True
    if arm not in CHAT_FAMILY:
        return False
    return (
        doc.get(arm_key) in CHAT_FAMILY
        or doc.get("instrument") in CHAT_FAMILY
        or doc.get("kind") == EXPORT_STAMP_KIND
    )


def earlier_results(directory: Path, arm: str, arm_key: str, sha: str) -> list[Path]:
    """The JSON files in `directory` (not below it) that are results of
    `arm` (read from the `arm_key` field) on corpus sha `sha`. For an arm of
    the chat family, a result of any member of the family and an export
    stamp count too."""
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
            and doc.get("corpus_sha256") == sha
            and _same_read(doc, arm, arm_key)
        ):
            found.append(path)
    return found


def _recorded_reasons(paths: Iterable[Path]) -> dict[str, Path]:
    """The rerun reasons the earlier files recorded, each with its file."""
    reasons: dict[str, Path] = {}
    for path in paths:
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rerun = (doc.get("test_guard") or {}).get("rerun") or {}
        reason = rerun.get("reason") if isinstance(rerun, dict) else None
        if isinstance(reason, str) and reason.strip():
            reasons.setdefault(reason.strip(), Path(path))
    return reasons


def guard_run(
    corpus: dict[str, Any],
    corpus_sha: str,
    *,
    arm: str,
    arm_key: str,
    out: Path | None,
    operating_points: Path | None,
    rerun_reason: str | None,
    seals: Path | None = None,
    review_seals: Path = SEALS_REVIEW,
    review_shas: Mapping[str, str] | None = None,
    export: bool = False,
    results_dirs: Iterable[Path] = (),
) -> dict[str, Any] | None:
    """Pass or refuse a run. None for any corpus but a test split (v1's
    or v1b's); for one, the record the run's output carries (its
    `test_guard` field), or Refused. `seals` defaults to the split's own
    seals file (TEST_SEALS); the rerun rule is per corpus sha, so a read
    of one split never counts against the other.

    `out` is the file the run will write, None for a run that writes none,
    which skips the rerun rule unless `export` is set: an export is a read
    of the test split, so it is held to the rule although it writes no
    result. The earlier results are looked for beside `out` and in each of
    `results_dirs`. `review_shas` are the run's question, prompt and
    constants shas, checked against `review_seals` when --operating-points
    names the review points or the arm is chat-review."""
    version = corpus.get("version")
    if not is_test_version(version):
        return None
    seals = Path(seals) if seals is not None else seals_for(version)
    rerun_reason = (rerun_reason or "").strip() or None
    sealed = read_seals(seals)
    if corpus_sha != sealed["test_sha256"]:
        raise Refused(
            f"the test split's sha {corpus_sha} is not the sealed test_sha256 "
            f"{sealed['test_sha256']}"
        )
    review = arm == REVIEW_INSTRUMENT or is_review_points(operating_points)
    if operating_points is None:
        if review:
            raise Refused(
                "the chat-review instrument runs on the test split only with "
                f"--operating-points naming the sealed {REVIEW_POINTS_NAME}"
            )
        raise Refused(
            "the test split runs only with --operating-points naming the sealed "
            "operating-points file"
        )
    if not Path(operating_points).is_file():
        raise Refused(f"--operating-points {operating_points} is not a file")
    points_sha = file_sha256(operating_points)
    record: dict[str, Any] = {
        "seals": {"path": _rel(seals), "sha256": file_sha256(seals)},
    }
    if review:
        if not is_review_points(operating_points):
            raise Refused(
                "the chat-review instrument runs on the test split only with "
                f"--operating-points naming the sealed {REVIEW_POINTS_NAME}, not "
                f"{_rel(operating_points)}"
            )
        review_sealed = read_review_seals(review_seals)
        if points_sha != review_sealed[OPERATING_POINTS_FIELD]:
            raise Refused(
                f"--operating-points sha {points_sha} is not the sealed "
                f"{OPERATING_POINTS_FIELD} {review_sealed[OPERATING_POINTS_FIELD]} "
                f"of {_rel(review_seals)}"
            )
        record["review_seals"] = {
            "path": _rel(review_seals),
            "sha256": file_sha256(review_seals),
        }
        record["review_shas"] = check_review_shas(review_shas, review_sealed)
    elif points_sha != sealed[OPERATING_POINTS_FIELD]:
        raise Refused(
            f"--operating-points sha {points_sha} is not the sealed "
            f"{OPERATING_POINTS_FIELD} {sealed[OPERATING_POINTS_FIELD]}"
        )
    record["operating_points"] = {"path": _rel(operating_points), "sha256": points_sha}
    if out is None and not export:
        return record
    directories = [Path(out).parent] if out is not None else []
    directories += [Path(d) for d in results_dirs]
    earlier: list[Path] = []
    seen: set[Path] = set()
    for directory in directories:
        for path in earlier_results(directory, arm, arm_key, corpus_sha):
            if path.resolve() not in seen:
                seen.add(path.resolve())
                earlier.append(path)
    if earlier and not rerun_reason:
        raise Refused(
            f"{arm} already has a test result on this sha: "
            f"{', '.join(_rel(p) for p in earlier)}; a rerun needs --rerun-reason"
        )
    if out is not None:
        resolved_out = Path(out).resolve()
        if any(p.resolve() == resolved_out for p in earlier):
            raise Refused(
                f"--out {_rel(out)} is an earlier test result, which is kept; write "
                "the rerun to a new path"
            )
    if rerun_reason:
        recorded = _recorded_reasons(earlier)
        if rerun_reason in recorded:
            raise Refused(
                f"--rerun-reason is already recorded by {_rel(recorded[rerun_reason])}; "
                "a new read needs a new reason"
            )
        record["rerun"] = {
            "reason": rerun_reason,
            "earlier": [_rel(p) for p in earlier],
        }
    if export:
        record["export"] = True
    return record
