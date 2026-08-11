"""Loader and scoring harness for the held-out instrument.

The instrument's FORMAT and the seal protocol are specified in
`FORMAT.md`. This file is the container's executable half: it validates
a set of instrument files, and — separately, and only when asked —
scores a retriever against them.

**The two commands are separate on purpose.** `--validate` reads content
solely to check structure and prints none of it, so the implementer can
confirm a delivered instrument is loadable without reading the questions
the seal forbids them from reading. Scoring is what breaks the seal, and
it is a different flag typed deliberately.

Everything here is content-free. No instrument data is committed
alongside it; the fixture under `fixtures/` is obviously-fake harness
material and is never part of an instrument.

    .venv/bin/python bench/heldout/run.py --validate
    .venv/bin/python bench/heldout/run.py --score --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bettermemory.search import search as run_search  # noqa: E402
from bettermemory.store import Store  # noqa: E402

DATA = _HERE / "data"
K_VALUES = (1, 5, 10)

# Item-level retrieval depth, collapsed afterwards to distinct sessions.
# Must exceed max(K_VALUES) by enough that k distinct sessions stay
# reachable when one session monopolises the head of the ranking. The
# envelope in FORMAT.md caps a persona well below this, so the depth is
# the whole persona rather than a window of it — the conservative
# direction, and stated rather than assumed.
RETRIEVAL_DEPTH = 200

SCOPE = ["heldout"]

_ID_RE = re.compile(r"^[a-z0-9_]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The envelope FORMAT.md section 5 publishes. Enforced as a WARNING, not
# an error: an instrument slightly outside it is still scorable, and a
# harness that refuses to load a delivered artifact over a soft target
# would be the tail wagging the dog. Structural rules below are errors.
ENVELOPE = {
    "personas": (12, 20),
    "sessions_per_persona": (8, 20),
    "turns_per_session": (4, 20),
    "questions": (100, 150),
}


class InstrumentError(ValueError):
    """A structural defect that makes an instrument unscorable."""


def file_fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InstrumentError(message)


def validate_personas(personas: Any) -> dict[str, list[dict[str, Any]]]:
    """Structural validation. Returns persona_id -> sessions.

    Raises rather than returning a report: an instrument that does not
    load is not a weaker instrument, it is not an instrument.
    """
    _require(
        isinstance(personas, list) and bool(personas),
        "personas.json must be a non-empty array",
    )
    by_persona: dict[str, list[dict[str, Any]]] = {}
    seen_sessions: set[str] = set()
    for p in personas:
        pid = p.get("persona_id")
        _require(
            isinstance(pid, str) and bool(_ID_RE.match(pid)), f"bad persona_id: {pid!r}"
        )
        _require(pid not in by_persona, f"duplicate persona_id: {pid}")
        sessions = p.get("sessions")
        _require(
            isinstance(sessions, list) and len(sessions) >= 2,
            f"{pid}: needs >= 2 sessions",
        )
        last_date = ""
        for s in sessions:
            sid = s.get("session_id")
            _require(
                isinstance(sid, str) and bool(_ID_RE.match(sid)),
                f"bad session_id: {sid!r}",
            )
            _require(sid not in seen_sessions, f"duplicate session_id: {sid}")
            seen_sessions.add(sid)
            date = s.get("date")
            _require(
                isinstance(date, str) and bool(_DATE_RE.match(date)),
                f"{sid}: bad date {date!r}",
            )
            _require(
                date >= last_date, f"{sid}: date {date} precedes the previous session"
            )
            last_date = date
            turns = s.get("turns")
            _require(isinstance(turns, list) and bool(turns), f"{sid}: needs >= 1 turn")
            for t in turns:
                _require(
                    t.get("role") in ("user", "assistant"),
                    f"{sid}: bad role {t.get('role')!r}",
                )
                text = t.get("text")
                _require(
                    isinstance(text, str) and bool(text.strip()),
                    f"{sid}: empty turn text",
                )
                _require("\n" not in text, f"{sid}: turn text contains a newline")
        by_persona[pid] = sessions

    # The label must not be inside the searchable content. A session id
    # appearing in any turn text would make the gold answer retrievable
    # as content, which measures the leak rather than the retriever.
    for pid, sessions in by_persona.items():
        for s in sessions:
            for t in s["turns"]:
                for sid in seen_sessions:
                    _require(
                        sid not in t["text"],
                        f"{s['session_id']}: turn text contains session id {sid!r}",
                    )
    return by_persona


def validate_questions(
    questions: Any, by_persona: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    _require(
        isinstance(questions, list) and bool(questions),
        "questions.json must be a non-empty array",
    )
    session_owner = {
        s["session_id"]: pid for pid, sessions in by_persona.items() for s in sessions
    }
    seen: set[str] = set()
    for q in questions:
        qid = q.get("question_id")
        _require(
            isinstance(qid, str) and bool(_ID_RE.match(qid)),
            f"bad question_id: {qid!r}",
        )
        _require(qid not in seen, f"duplicate question_id: {qid}")
        seen.add(qid)
        pid = q.get("persona_id")
        _require(pid in by_persona, f"{qid}: unknown persona_id {pid!r}")
        text = q.get("question")
        _require(isinstance(text, str) and bool(text.strip()), f"{qid}: empty question")
        answers = q.get("answer_session_ids")
        _require(
            isinstance(answers, list) and bool(answers),
            f"{qid}: needs >= 1 answer session",
        )
        _require(
            len(set(answers)) == len(answers), f"{qid}: duplicate answer session ids"
        )
        for sid in answers:
            _require(sid in session_owner, f"{qid}: unknown answer session {sid!r}")
            _require(
                session_owner[sid] == pid,
                f"{qid}: answer session {sid} belongs to {session_owner[sid]}, not {pid}",
            )
    return list(questions)


def envelope_warnings(
    by_persona: dict[str, list[dict[str, Any]]], questions: list[dict[str, Any]]
) -> list[str]:
    """Soft-target deviations from FORMAT.md section 5."""
    out: list[str] = []

    def check(label: str, value: int, key: str) -> None:
        lo, hi = ENVELOPE[key]
        if not (lo <= value <= hi):
            out.append(f"{label} is {value}, outside the {lo}-{hi} envelope")

    check("persona count", len(by_persona), "personas")
    check("question count", len(questions), "questions")
    for pid, sessions in by_persona.items():
        lo, hi = ENVELOPE["sessions_per_persona"]
        if not (lo <= len(sessions) <= hi):
            out.append(f"{pid} has {len(sessions)} sessions, outside {lo}-{hi}")
        for s in sessions:
            tlo, thi = ENVELOPE["turns_per_session"]
            if not (tlo <= len(s["turns"]) <= thi):
                out.append(
                    f"{s['session_id']} has {len(s['turns'])} turns, outside {tlo}-{thi}"
                )
    multi = sum(1 for q in questions if len(q["answer_session_ids"]) > 1)
    share = multi / len(questions) if questions else 0.0
    if not (0.15 <= share <= 0.35):
        out.append(f"multi-answer questions are {share:.0%}, outside the 15-35% target")
    return out


def load(
    data_dir: Path,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], dict[str, Any]]:
    """Load and validate an instrument directory."""
    personas_path = data_dir / "personas.json"
    questions_path = data_dir / "questions.json"
    manifest_path = data_dir / "manifest.json"
    for path in (personas_path, questions_path, manifest_path):
        _require(path.exists(), f"missing {path.name} in {data_dir}")
    by_persona = validate_personas(
        json.loads(personas_path.read_text(encoding="utf-8"))
    )
    questions = validate_questions(
        json.loads(questions_path.read_text(encoding="utf-8")), by_persona
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _require(isinstance(manifest, dict), "manifest.json must be an object")
    for field in ("instrument", "version", "authored", "license", "sealed"):
        _require(field in manifest, f"manifest.json missing {field!r}")
    return by_persona, questions, manifest


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def build_persona_store(root: Path, sessions: list[dict[str, Any]]) -> dict[str, str]:
    """Write one persona's sessions. Returns memory id -> session id.

    The session id is NEVER placed in the body or the scopes — it would
    be retrievable content if it were, and the labels would leak into
    the thing being measured. Same rule the LongMemEval harness states,
    and `validate_personas` enforces the content half of it.
    """
    store = Store(root)
    id_to_session: dict[str, str] = {}
    for s in sessions:
        date = s["date"]
        for turn in s["turns"]:
            body = f"[{date}]\n{turn['text']}"
            memory = store.write(content=body, scopes=SCOPE)
            id_to_session[memory.id] = s["session_id"]
    return id_to_session


def distinct_sessions(
    ranked_ids: list[str], id_to_session: dict[str, str]
) -> list[str]:
    """Collapse an item ranking to distinct sessions, first wins."""
    seen: list[str] = []
    known: set[str] = set()
    for mid in ranked_ids:
        sid = id_to_session.get(mid)
        if sid is not None and sid not in known:
            known.add(sid)
            seen.append(sid)
    return seen


def question_record(question: dict[str, Any], ranked: list[str]) -> dict[str, Any]:
    """One question's outcome, in the smallest replayable form.

    `evidence_ranks` carries the rank of each answer session or None, so
    every recall@k below is recomputable from the sidecar without
    re-running the engine.
    """
    answers = list(dict.fromkeys(question["answer_session_ids"]))
    ranks: list[int | None] = []
    for sid in answers:
        ranks.append(ranked.index(sid) if sid in ranked else None)
    return {
        "qid": question["question_id"],
        "persona_id": question["persona_id"],
        "n_evidence": len(answers),
        "n_ranked": len(ranked),
        "evidence_ranks": ranks,
    }


def recall_at(record: dict[str, Any], k: int) -> float:
    n = record["n_evidence"]
    if not n:
        return 0.0
    hit = [r for r in record["evidence_ranks"] if r is not None and r < k]
    return len(hit) / n


def ceiling_at(record: dict[str, Any], k: int) -> float:
    """The best recall@k this question allows, given its evidence count."""
    n = record["n_evidence"]
    return min(n, k) / n if n else 0.0


def score(
    by_persona: dict[str, list[dict[str, Any]]], questions: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run every question against the engine and aggregate."""
    records: list[dict[str, Any]] = []
    started = time.time()
    for q in questions:
        root = Path(tempfile.mkdtemp(prefix="bm-heldout-"))
        try:
            id_to_session = build_persona_store(root, by_persona[q["persona_id"]])
            hits = run_search(
                Store(root).load_all(),
                q["question"],
                max_results=RETRIEVAL_DEPTH,
                mode="hybrid",
            )
            ranked = distinct_sessions([h.id for h in hits], id_to_session)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        records.append(question_record(q, ranked))

    summary: dict[str, Any] = {
        "questions": len(records),
        "seconds": round(time.time() - started, 1),
        "macro": {
            str(k): round(sum(recall_at(r, k) for r in records) / len(records), 4)
            for k in K_VALUES
        }
        if records
        else {},
        "ceiling": {
            str(k): round(sum(ceiling_at(r, k) for r in records) / len(records), 4)
            for k in K_VALUES
        }
        if records
        else {},
    }
    return records, summary


def _provenance() -> dict[str, Any]:
    """Version + commit + platform stamp for the emitted artifact.

    `tree_dirty` counts tracked modifications only, so a run's own
    freshly written result files (untracked) don't mark it dirty.
    """
    import platform
    import subprocess
    from datetime import date

    import bettermemory

    commit: str | None = None
    tree_dirty: bool | None = None
    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(_HERE),
                timeout=10,
            ).stdout.strip()
            or None
        )
        tree_dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                capture_output=True,
                text=True,
                cwd=str(_HERE),
                timeout=10,
            ).stdout.strip()
        )
    except OSError:
        pass
    return {
        "bettermemory_version": bettermemory.__version__,
        "commit": commit,
        "tree_dirty": tree_dirty,
        "date": date.today().isoformat(),
        "machine": {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Held-out instrument loader and scorer.")
    p.add_argument("--data", default=str(DATA), metavar="DIR")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--validate",
        action="store_true",
        help=(
            "Structure only. Reads content to check it and prints none of it, "
            "so a sealed instrument can be confirmed loadable without reading "
            "the questions."
        ),
    )
    mode.add_argument(
        "--score",
        action="store_true",
        help="Run the engine against the instrument. Breaks the seal by design.",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--per-question", default=None, metavar="PATH")
    p.add_argument("--out", default=None, metavar="PATH")
    args = p.parse_args()

    data_dir = Path(args.data).expanduser()
    if not data_dir.is_absolute():
        data_dir = (_HERE / data_dir).resolve()
    if not data_dir.exists():
        print(f"missing instrument directory: {data_dir}", file=sys.stderr)
        return 1

    try:
        by_persona, questions, manifest = load(data_dir)
    except InstrumentError as exc:
        print(f"instrument is not loadable: {exc}", file=sys.stderr)
        return 2

    warnings = envelope_warnings(by_persona, questions)
    if args.validate:
        print(
            f"loadable: {len(by_persona)} personas, "
            f"{sum(len(s) for s in by_persona.values())} sessions, "
            f"{len(questions)} questions "
            f"(sealed={manifest.get('sealed')}, license={manifest.get('license')})",
            file=sys.stderr,
        )
        for w in warnings:
            print(f"  envelope: {w}", file=sys.stderr)
        return 0

    records, summary = score(by_persona, questions)
    payload: dict[str, Any] = {
        "provenance": _provenance(),
        "instrument": {
            "manifest": manifest,
            "personas_sha256": file_fingerprint(data_dir / "personas.json"),
            "questions_sha256": file_fingerprint(data_dir / "questions.json"),
        },
        "retrieval_depth": RETRIEVAL_DEPTH,
        "k_values": list(K_VALUES),
        "envelope_warnings": warnings,
        "summary": summary,
    }
    if args.per_question:
        pq = Path(args.per_question).expanduser()
        if not pq.is_absolute():
            pq = (_HERE / pq).resolve()
        pq.parent.mkdir(parents=True, exist_ok=True)
        pq.write_text(
            json.dumps({**payload, "records": records}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"per-question records written to {pq}", file=sys.stderr)

    text = json.dumps(payload, indent=2)
    if args.out:
        out = Path(args.out).expanduser()
        if not out.is_absolute():
            out = (_HERE / out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    elif args.json:
        print(text)
    else:
        print(f"macro recall: {summary['macro']}")
        print(f"ceilings:     {summary['ceiling']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
