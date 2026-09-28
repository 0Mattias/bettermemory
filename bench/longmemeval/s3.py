"""LongMemEval-S won by the memory alone: unit S3's harness.

UNIT S3 (declaration memory 01M3JFG6T3KF5R2TY4HQNCJ8NW, drafted 2026-09-27
under S plan v3, memory 01M3JEGGAYJVF3X6T7SN7B767V; the owner's go
2026-09-28 00:21 UTC, 2026-09-27 20:21 EDT). S2 read the holdout under the
S0 arm and all 500 came to 492 under the standard judge; Hindsight's
published served context, read by the same reader, came to 496 (H1). Every
lever here is a memory improvement at the same 180,000-character budget:

  (1) the budget is used: the engine's further hits serve what a full
      pool leaves unused
  (2) a dry pool is filled with whole unhit sessions nearest in time to
      the hit sessions, later ones first
  (3) the engine's rescue expansion with a personal-facts table
  (4) whole sessions in date order in LongMemEval's own history format:
      the flat-session rendering of run_generation.py at 9e0b455, checked
      byte for byte against the pinned prepare_prompt

Lever 3 is not built. The lane engages only when the fused ranking's top
hit covers under 0.60 of the query's terms, and on a1cc6108, the one dev
question the S0 arm leaves uncovered, the top hit covers two of three
("alex", "old"), so no table reaches it through the lane; the kinship
entry the plan named came from a holdout miss (8e91e7d9), which tuning on
the dev sets does not allow.

STEP S3a, THIS MODULE'S FIRST PART: no reader, no judge, no model call.
Every arm is measured by S0's answer-turn coverage on the S dev 150 and on
the M dev 150 (the same questions over LongMemEval-M's haystacks, about ten
times the sessions), at 180,000 characters. For a session arm a question
is COVERED when every has_answer turn's session is served and the turn's
content, as json.dumps writes it, is in the served history.

THE ARMS: the S0 arm (the baseline S1 and S2 read); lever 1 as the plan
wrote it, a 400-round pool, whose extra rounds join their sessions'
groups; levers 1 and 2 as the service's tiered fill (the S0 arm's rounds
first, then the engine's further hits, then whole sessions nearest in
time to a hit), and lever 2 alone as its dry fill (the third tier, only
when the S0 arm's hits and neighbors come to fewer than its pool); the
tiered fill and the S0 arm untrimmed; and lever 4, whole sessions: a pool
of 100, 200 or 400 of the engine's ranked rounds or every round it
scores, times no fill or the adjacent fill.

THE RULE, fixed before the recorded run: among the arms whose M dev
coverage is at least the S0 arm's on the same store, the one covering the
most S dev questions, then the most M dev questions, then fewer mean
served characters on the S dev, then the name. The holdout is never
served here.

GATE G1: the chosen arm covers 143 of the 143 labelled S dev questions and
its M dev coverage is at least the S0 arm's.

STEPS, each a subcommand:

  mdev      the M dev 150 out of the 2.7 GB M file, streamed, into one file
            under the unit's receipts, each question checked against S's
  ingest    the S dev 150 or the M dev 150 into a fresh store that keeps
            its session log
  coverage  every arm on both stores, the parity checks, the choice, G1

Every process that opens a store runs with BETTERMEMORY_KEYS_DIR set to
the S3 receipts' keys.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent
for _path in (_BENCH, _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import f1  # noqa: E402
import h1  # noqa: E402
import l1  # noqa: E402
import s0  # noqa: E402
import s1  # noqa: E402
from aml import run as aml_run  # noqa: E402
from aml.service import SESSION_FILLS, MemoryService, _fmt_ts, _text  # noqa: E402

RECEIPTS = l1.RECEIPTS / "s3-2026-09-28"
WORK = RECEIPTS / "work"
S_STORE = aml_run.STORES / "coverage-s3-dev"
M_STORE = aml_run.STORES / "coverage-s3-m-dev"
M_CORPUS = _HERE / "data" / "longmemeval_m_cleaned.json"
# bench/longmemeval/run.py KNOWN_CORPORA
M_CORPUS_SHA256 = "9d79e5524794a2e6900a3aa9cb7d9152c5a3e8319c9a87c25494ba1eacee495f"
M_DEV = WORK / "m-dev-150.json"
S0_ARTIFACT = s0.OUT
OUT = l1.RESULTS / "s3-coverage-2026-09-28.json"

BUDGET = s1.BUDGET
S0_ARM = s1.ARM
# The S0 arm; lever 1 as a larger pool merged into the session groups;
# levers 1 and 2 as the tiered fill; each tiered and S0 arm untrimmed too.
ROUND_ARMS = (
    S0_ARM,
    s0.Arm(order="session", top_k=400, fill="neighbors", trim="tail"),
    s0.Arm(order="session", top_k=200, fill="tiered", trim="tail"),
    s0.Arm(order="session", top_k=200, fill="dry", trim="tail"),
    s0.Arm(order="session", top_k=200, fill="tiered", trim="none"),
    s0.Arm(order="session", top_k=200, fill="neighbors", trim="none"),
)
# A pool no store reaches: every round the engine scores.
EVERY_HIT = 1_000_000
POOLS = (100, 200, 400, EVERY_HIT)
LABELLED_S_DEV = 143


@dataclass(frozen=True)
class SessionArm:
    """Whole-session serving: `pool` of the engine's ranked rounds decide
    the hit sessions, `fill` what fills a budget they leave unused, and
    `expansion` whether the engine's rescue lane is on."""

    pool: int
    fill: str
    expansion: bool = False

    def __post_init__(self) -> None:
        if self.fill not in SESSION_FILLS:
            raise ValueError(f"fill {self.fill!r}")
        if self.pool < 1:
            raise ValueError(f"pool {self.pool!r}")

    @property
    def name(self) -> str:
        pool = "all" if self.pool >= EVERY_HIT else str(self.pool)
        rescue = "-rescue" if self.expansion else ""
        return f"sessions-k{pool}-fill_{self.fill}{rescue}"


SESSION_ARMS = tuple(SessionArm(pool=k, fill=f) for k in POOLS for f in SESSION_FILLS)
Arm = s0.Arm | SessionArm


def service_for(root: Path) -> MemoryService:
    """C0's service, keeping the session log, as S3's stores are written."""
    service = l1.service_for(root)
    service.keep_sessions = True
    return service


def serve(
    service: MemoryService, inst: dict[str, Any], arm: Arm, budget: int
) -> list[dict[str, Any]]:
    """One arm's served items for one question. The options are read at
    search time, so one service serves every arm of a question."""
    if isinstance(arm, SessionArm):
        service.unit = "sessions"
        service.serve = arm.pool
        service.budget = budget
        service.session_fill = arm.fill
        service.rescue_expansion = arm.expansion
        pool = arm.pool
    else:
        service.unit = "rounds"
        service.session_fill = "none"
        service.rescue_expansion = False
        s0.configure(service, arm, budget)
        pool = arm.top_k
    hits: list[dict[str, Any]] = service.search(
        s0.user_id(inst), inst["question"], pool
    )
    return hits


def history(hits: list[dict[str, Any]]) -> str:
    """Served sessions as LongMemEval's history string: their blocks, in
    the served order, concatenated."""
    return "".join(h["content"] for h in hits)


def session_numbers(hits: list[dict[str, Any]]) -> list[int]:
    """Each served session's position in the store's session log."""
    return [int(str(h["id"]).split(":", 1)[1]) for h in hits]


def aligned_log(service: MemoryService, inst: dict[str, Any]) -> list[dict[str, Any]]:
    """The store's session log, refused unless it is the haystack: one
    entry per session in haystack order, its id, its turns less has_answer
    and its date as the dataset writes it."""
    log = service._sessions(service._user(s0.user_id(inst)))
    qid = inst["question_id"]
    ids = inst["haystack_session_ids"]
    if len(log) != len(ids):
        raise SystemExit(
            f"{qid}: the log holds {len(log)} sessions, the haystack {len(ids)}"
        )
    for k, (entry, sid, date, session) in enumerate(
        zip(log, ids, inst["haystack_dates"], inst["haystack_sessions"], strict=True)
    ):
        turns = [{a: b for a, b in t.items() if a != "has_answer"} for t in session]
        if entry["session_id"] != sid or entry["messages"] != turns:
            raise SystemExit(f"{qid}: log entry {k} is not session {k} {sid!r}")
        if _fmt_ts(entry["ts"]) != date:
            raise SystemExit(
                f"{qid}: session {k} dated {_fmt_ts(entry['ts'])!r}, not {date!r}"
            )
    return log


def session_coverage(
    inst: dict[str, Any], hits: list[dict[str, Any]]
) -> dict[str, Any]:
    """Every answer turn's session served, and the turn's content in the
    history as json.dumps writes it. A question with no answer turns is
    counted apart, never as covered."""
    turns = s0.answer_turns(inst)
    served = set(session_numbers(hits))
    text = history(hits)
    missing = [
        (k, j, i)
        for k, j, i in turns
        if k not in served
        or json.dumps(_text(inst["haystack_sessions"][k][i].get("content"))) not in text
    ]
    return {
        "answer_turns": len(turns),
        "missing": missing,
        "covered": bool(turns) and not missing,
    }


def session_cell(inst: dict[str, Any], hits: list[dict[str, Any]]) -> dict[str, Any]:
    got = session_coverage(inst, hits)
    return {
        "covered": got["covered"],
        "missing": got["missing"],
        "chars": len(history(hits)),
        "sessions": len(hits),
        "filled": sum(bool(h.get("filled")) for h in hits),
    }


def unique_ids(ids: list[str]) -> list[str]:
    """Session ids made unique for upstream's lookups: a haystack can hold
    one session twice under one id (13 of LongMemEval-S's do, each copy on
    its own date), and prepare_prompt keys its dates by id."""
    seen: Counter[str] = Counter()
    out = []
    for sid in ids:
        out.append(f"{sid}#{seen[sid]}" if seen[sid] else sid)
        seen[sid] += 1
    return out


def upstream_flat_session(
    prepare: Callable[..., str], inst: dict[str, Any], hits: list[dict[str, Any]]
) -> str:
    """Upstream's own prompt for the served sessions: the flat-session
    retriever given them as its ranked items, json, useronly false, con,
    no truncation. A copy, since prepare_prompt drops has_answer in place."""
    entry = copy.deepcopy(inst)
    ids = unique_ids(inst["haystack_session_ids"])
    entry["haystack_session_ids"] = ids
    numbers = session_numbers(hits)
    entry["retrieval_results"] = {
        "ranked_items": [{"corpus_id": ids[n]} for n in numbers]
    }
    return prepare(
        entry,
        "flat-session",
        len(numbers),
        False,
        "json",
        True,
        tokenizer=f1._Whole(),
        tokenizer_backend="openai",
        max_retrieval_length=10**12,
        merge_key_expansion_into_value="none",
    )


def arms_named(names: list[str]) -> list[Arm]:
    every: dict[str, Arm] = {a.name: a for a in (*ROUND_ARMS, *SESSION_ARMS)}
    return [every[n] for n in names]


def measure(
    root: Path, inst: dict[str, Any], names: list[str], upstream: str | None
) -> dict[str, Any]:
    """Every arm for one question, from a store checked against the
    haystack, each session arm's prompt checked against upstream's. Its own
    service, so questions run in parallel processes."""
    service = service_for(root)
    positions = s0.round_positions(service, inst)
    aligned_log(service, inst)
    prepare = (
        f1.upstream_prepare(Path(upstream), l1.UPSTREAM_GENERATION_SHA256)
        if upstream
        else None
    )
    cells: dict[str, Any] = {}
    for arm in arms_named(names):
        hits = serve(service, inst, arm, BUDGET)
        if isinstance(arm, SessionArm):
            cell = session_cell(inst, hits)
            if prepare is not None and hits:
                prompt = l1.reading_prompt(
                    history(hits), inst["question_date"], inst["question"]
                )
                cell["upstream_equal"] = prompt == upstream_flat_session(
                    prepare, inst, hits
                )
        else:
            cell = s0._cell(inst, hits, positions)
        cells[arm.name] = cell
    return {
        "question_id": inst["question_id"],
        "question_type": inst["question_type"],
        "answer_turns": len(s0.answer_turns(inst)),
        "rounds": len(positions),
        "sessions": len(inst["haystack_sessions"]),
        "arms": cells,
    }


def _measure_job(
    job: tuple[str, dict[str, Any], list[str], str | None],
) -> dict[str, Any]:
    root, inst, names, upstream = job
    return measure(Path(root), inst, names, upstream)


# ---------------------------------------------------------------- the M dev


def iter_json_array(path: Path, chunk: int = 1 << 24) -> Iterator[Any]:
    """The elements of a top-level JSON array, one at a time, so a 2.7 GB
    file is never held whole. An element that runs past the buffer is
    retried with more of the file; an object cut short never parses."""
    decoder = json.JSONDecoder()
    with path.open(encoding="utf-8") as fh:
        buf = fh.read(chunk)
        pos = buf.index("[") + 1
        while True:
            while True:
                while pos < len(buf) and buf[pos] in " \t\r\n,":
                    pos += 1
                if pos < len(buf):
                    break
                more = fh.read(chunk)
                if not more:
                    raise SystemExit(f"{path}: the array never closes")
                buf, pos = buf[pos:] + more, 0
            if buf[pos] == "]":
                return
            while True:
                try:
                    item, end = decoder.raw_decode(buf, pos)
                    break
                except json.JSONDecodeError:
                    more = fh.read(chunk)
                    if not more:
                        raise
                    buf, pos = buf[pos:] + more, 0
            yield item
            pos = end


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 24), b""):
            h.update(block)
    return h.hexdigest()


def same_question(s: dict[str, Any], m: dict[str, Any]) -> bool:
    """The same question and gold. Not the question date: M sets its own,
    after its longer haystack (497 of the 500 differ from S's)."""
    keys = ("question_id", "question_type", "question", "answer")
    return all(str(s[k]) == str(m[k]) for k in keys)


def cmd_mdev(args: argparse.Namespace) -> None:
    corpus = Path(args.m_corpus)
    got = file_sha256(corpus)
    if got != M_CORPUS_SHA256:
        raise SystemExit(f"{corpus} hashes to {got}, not the pinned {M_CORPUS_SHA256}")
    dev = s0.dev_instances(l1._corpus())
    by_id = {inst["question_id"]: inst for inst in dev}
    found: dict[str, dict[str, Any]] = {}
    seen = 0
    for item in iter_json_array(corpus):
        seen += 1
        qid = item["question_id"]
        if qid in by_id:
            if not same_question(by_id[qid], item):
                raise SystemExit(f"{qid}: M's question differs from S's")
            found[qid] = item
    if seen != 500 or set(found) != set(by_id):
        raise SystemExit(f"M holds {seen} questions and {len(found)} of the dev 150")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps([found[i["question_id"]] for i in dev], ensure_ascii=False)
    out.write_bytes(data.encode("utf-8"))
    sessions = [len(found[q]["haystack_sessions"]) for q in by_id]
    print(
        json.dumps(
            {
                "m_corpus_sha256": got,
                "questions": len(found),
                "out": str(out),
                "out_sha256": l1._sha(data),
                "sessions_median": statistics.median(sessions),
                "sessions_max": max(sessions),
            }
        )
    )


def m_dev_instances(path: Path) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(path.read_bytes().decode("utf-8"))
    return data


# ---------------------------------------------------------------- the stores


def _ingest_job(job: tuple[str, dict[str, Any]]) -> int:
    root, inst = job
    service = service_for(Path(root))
    user, adds = aml_run.adds_for(inst)
    return sum(service.add(user_id=user, **add) for add in adds)


def _instances(which: str, m_dev: Path) -> list[dict[str, Any]]:
    if which == "s":
        return s0.dev_instances(l1._corpus())
    return m_dev_instances(m_dev)


def cmd_ingest(args: argparse.Namespace) -> None:
    if not os.environ.get("BETTERMEMORY_KEYS_DIR"):
        raise SystemExit("set BETTERMEMORY_KEYS_DIR to the S3 receipts' keys")
    root = Path(args.store)
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"{root} is not empty; S3 builds its stores fresh")
    instances = _instances(args.corpus, Path(args.m_dev))
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        rounds = list(pool.map(_ingest_job, [(str(root), i) for i in instances]))
    print(
        json.dumps(
            {
                "corpus": args.corpus,
                "questions": len(instances),
                "rounds": sum(rounds),
                "seconds": round(time.time() - t0, 1),
                "store": str(root),
            }
        )
    )


# ---------------------------------------------------------------- coverage


def summarize(rows: list[dict[str, Any]], name: str, base: str) -> dict[str, Any]:
    """One arm over the labelled questions: covered, served size, and the
    questions it gains or loses against `base`."""
    scored = [r for r in rows if r["answer_turns"]]
    covered = {r["question_id"] for r in scored if r["arms"][name]["covered"]}
    base_covered = {r["question_id"] for r in scored if r["arms"][base]["covered"]}
    chars = [r["arms"][name]["chars"] for r in scored]
    every = [r["arms"][name]["chars"] for r in rows]
    return {
        "n": len(scored),
        "covered": len(covered),
        "rate": round(len(covered) / len(scored), 4) if scored else None,
        "mean_chars": round(statistics.fmean(chars), 1) if chars else 0.0,
        "mean_chars_all": round(statistics.fmean(every), 1) if every else 0.0,
        "p50_chars": statistics.median(chars) if chars else 0,
        "max_chars": max(every) if every else 0,
        "missing": sorted({r["question_id"] for r in scored} - covered),
        "recovered_vs_s0_arm": sorted(covered - base_covered),
        "regressed_vs_s0_arm": sorted(base_covered - covered),
    }


def choose(
    s_stats: dict[str, dict[str, Any]],
    m_stats: dict[str, dict[str, Any]],
    candidates: list[str],
    s0_m_covered: int,
) -> str | None:
    """The declared rule: of the candidates whose M dev coverage is at
    least the S0 arm's, most S dev covered, then most M dev covered, then
    fewer mean S dev characters, then the name."""
    eligible = [n for n in candidates if m_stats[n]["covered"] >= s0_m_covered]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda n: (
            -s_stats[n]["covered"],
            -m_stats[n]["covered"],
            s_stats[n]["mean_chars"],
            n,
        ),
    )


def s0_parity(rows: list[dict[str, Any]], s0_rows: dict[str, Any]) -> list[str]:
    """S dev questions whose S0-arm cell from S3's fresh store differs from
    S0's recorded cell at 180,000 characters, in characters or coverage."""
    key = str(BUDGET)
    return [
        r["question_id"]
        for r in rows
        if s0_rows[r["question_id"]]["best_curve"][key]["chars"]
        != r["arms"][S0_ARM.name]["chars"]
        or s0_rows[r["question_id"]]["best_curve"][key]["covered"]
        != r["arms"][S0_ARM.name]["covered"]
    ]


def _run_rows(
    root: Path,
    instances: list[dict[str, Any]],
    names: list[str],
    upstream: str,
    workers: int,
) -> list[dict[str, Any]]:
    jobs = [(str(root), inst, names, upstream) for inst in instances]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_measure_job, jobs))


def predictions(n: dict[str, Any]) -> list[dict[str, Any]]:
    """The declaration's three S3a predictions graded on the run."""
    v = l1._verdict
    s_cov, m_cov, s0_m, mean = (
        n["s_covered"],
        n["m_covered"],
        n["s0_m_covered"],
        n["s_mean"],
    )
    return [
        {
            "id": "S3-P1",
            "claim": "dev coverage 143 of 143 at 180,000 characters under some "
            "variant; MISSED under 142",
            "got": s_cov,
            "verdict": v(s_cov == LABELLED_S_DEV, s_cov < 142),
        },
        {
            "id": "S3-P2",
            "claim": "M dev coverage above the S0 arm's on the same store; MISSED "
            "if not above",
            "got": {"arm": m_cov, "s0_arm": s0_m},
            "verdict": v(m_cov > s0_m, m_cov <= s0_m),
        },
        {
            "id": "S3-P3",
            "claim": "served mean on dev between 120,000 and 165,000 characters; "
            "MISSED above 175,000",
            "got": mean,
            "verdict": v(120_000 <= mean <= 165_000, mean > 175_000),
        },
    ]


def cmd_coverage(args: argparse.Namespace) -> None:
    if not os.environ.get("BETTERMEMORY_KEYS_DIR"):
        raise SystemExit("set BETTERMEMORY_KEYS_DIR to the S3 receipts' keys")
    names = [a.name for a in (*ROUND_ARMS, *SESSION_ARMS)]
    session_names = [a.name for a in SESSION_ARMS]
    s0_art = json.loads(Path(args.s0_artifact).read_text(encoding="utf-8"))
    s0_rows = {r["question_id"]: r for r in s0_art["rows"]}
    s_dev = s0.dev_instances(l1._corpus())
    m_dev = m_dev_instances(Path(args.m_dev))
    if [i["question_id"] for i in m_dev] != [i["question_id"] for i in s_dev]:
        raise SystemExit("the M dev is not the S dev's questions in its order")

    t0 = time.time()
    s_rows = _run_rows(Path(args.s_store), s_dev, names, args.upstream, args.workers)
    s_seconds = round(time.time() - t0, 1)
    differ = s0_parity(s_rows, s0_rows)
    if differ:
        raise SystemExit(f"S0 parity: {len(differ)} cells differ from S0's: {differ}")
    t1 = time.time()
    m_rows = _run_rows(Path(args.m_store), m_dev, names, args.upstream, args.workers)
    m_seconds = round(time.time() - t1, 1)

    upstream_checked = [
        c["upstream_equal"]
        for rows in (s_rows, m_rows)
        for r in rows
        for n in session_names
        for c in [r["arms"][n]]
        if "upstream_equal" in c
    ]
    if not all(upstream_checked):
        raise SystemExit(
            f"upstream parity: {upstream_checked.count(False)} session prompts differ"
        )
    s_stats = {n: summarize(s_rows, n, S0_ARM.name) for n in names}
    m_stats = {n: summarize(m_rows, n, S0_ARM.name) for n in names}
    s0_m = m_stats[S0_ARM.name]["covered"]
    chosen = choose(s_stats, m_stats, names, s0_m)
    g1 = (
        chosen is not None
        and s_stats[chosen]["covered"] == LABELLED_S_DEV
        and m_stats[chosen]["covered"] >= s0_m
    )
    graded = predictions(
        {
            "s_covered": s_stats[chosen]["covered"] if chosen else 0,
            "m_covered": m_stats[chosen]["covered"] if chosen else 0,
            "s0_m_covered": s0_m,
            "s_mean": s_stats[chosen]["mean_chars"] if chosen else 0.0,
        }
    )
    src_dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--", "src"],
            capture_output=True,
            text=True,
            cwd=str(_HERE),
            timeout=10,
        ).stdout.strip()
    )
    l1._write_json(
        Path(args.out),
        {
            "unit": "S3",
            "step": "S3a",
            "declaration": "memory 01M3JFG6T3KF5R2TY4HQNCJ8NW",
            "plan": "memory 01M3JEGGAYJVF3X6T7SN7B767V",
            "provenance": l1._provenance(),
            "protocol": {
                "dataset": {
                    "s": {
                        "file": str(l1.CORPUS.relative_to(_BENCH.parent)),
                        "sha256": l1.CORPUS_SHA256,
                    },
                    "m": {
                        "file": str(M_CORPUS.relative_to(_BENCH.parent)),
                        "sha256": M_CORPUS_SHA256,
                        "dev_extract_sha256": l1._sha(Path(args.m_dev).read_bytes()),
                    },
                },
                "split": "the dev 150 of aml_run.split on S and the same questions "
                "on M; the holdout is not served",
                "covered": "rounds: every has_answer turn in a served round, its "
                "text verbatim; sessions: every has_answer turn's session served "
                "and the turn's content, as json.dumps writes it, in the history",
                "stores": {
                    "s": "bench/aml/.stores/coverage-s3-dev, built fresh with the "
                    "session log; its S0-arm cells equal S0's recorded ones",
                    "m": "bench/aml/.stores/coverage-s3-m-dev, built fresh with the "
                    "session log from the M dev extract",
                },
                "budget_chars": BUDGET,
                "session_prompt": "run_generation.py prepare_prompt flat-session at "
                + l1.UPSTREAM
                + ": json, useronly false, con, no truncation; every served "
                "session prompt checked byte for byte against the pinned function",
                "rule": "among arms with M dev coverage at least the S0 arm's: "
                "most S dev covered, then most M dev covered, then fewer mean S "
                "dev characters, then the name",
                "no_model": "no reader, no judge, no model call, no API spend",
            },
            "seconds": {"s_dev": s_seconds, "m_dev": m_seconds},
            "parity": {
                "s0_cells": {"n": len(s_rows), "equal": len(s_rows) - len(differ)},
                "upstream_prompts": {
                    "checked": len(upstream_checked),
                    "equal": sum(upstream_checked),
                },
            },
            "s_dev": s_stats,
            "m_dev": m_stats,
            "chosen": chosen,
            "g1": {
                "passed": g1,
                "s_dev_covered": s_stats[chosen]["covered"] if chosen else None,
                "m_dev_covered": m_stats[chosen]["covered"] if chosen else None,
                "s0_arm_m_dev_covered": s0_m,
            },
            "src_dirty": src_dirty,
            "predictions": graded,
            "rows": {"s_dev": s_rows, "m_dev": m_rows},
        },
    )
    print(
        json.dumps(
            {
                "chosen": chosen,
                "g1": g1,
                "s_dev": {
                    n: (s_stats[n]["covered"], s_stats[n]["mean_chars"]) for n in names
                },
                "m_dev": {
                    n: (m_stats[n]["covered"], m_stats[n]["mean_chars"]) for n in names
                },
                "predictions": [(p["id"], p["got"], p["verdict"]) for p in graded],
            },
            indent=1,
        )
    )


# ---------------------------------------------------------------- the reads
#
# S3b reads the dev 150 under the chosen arm, S3c Hindsight's published
# context for all 500, S3d the holdout 350 under the chosen arm, each one
# question to a reader under one briefing (lever 5) and graded by both
# official judges through L1's `price` and `judge` steps.

READ_BATCH = 1
S2_STORE = aml_run.STORES / "coverage-s2-holdout"
S2_ARTIFACT = l1.RESULTS / "s2-holdout-2026-09-27.json"
H1_RUN_FILE = l1.RECEIPTS / "h1-2026-09-27" / "hindsight-longmemeval-s.json.gz"
F1_ARTIFACT = f1.OUT
WORK_DEV = RECEIPTS / "work-dev"
WORK_HINDSIGHT = RECEIPTS / "work-hindsight"
WORK_HOLDOUT = RECEIPTS / "work-holdout"
JUDGES = {"record": l1.JUDGE_OF_RECORD, "standard": l1.STANDARD_JUDGE}
# The two dev questions no reader has answered right under the standard
# judge with its evidence in front of it (memory 01M3JCJRSNXCSH0DDGYKJEAGR0).
DEV_OUT_OF_REACH = "gpt4_2ba83207"

# L1's second wording with three spans changed: one prompt a reader, the
# measured file size, and lever 5, the answer first. The reading prompt in
# the file is LongMemEval's own and unchanged; this is what the reader is
# told about the form of its reply, the same for every arm compared.
_L1_BATCH = "it lists up to 6 prompt file paths, one per line."
_L1_SIZE = (
    "(each is up to about 90,000 characters and 2,000 lines; if a read stops "
    "early, continue with offset until the end, because the question is on "
    "the last lines)"
)
_L1_FORM = (
    "in the form its instructions ask for. Keep each answer under about 600 words."
)
_S3_BATCH = "it lists one prompt file path."
_S3_SIZE = (
    "(it is up to about {chars:,} characters and {lines:,} lines, more than "
    "one Read returns; continue with offset until the end, because the "
    "question is on the last lines)"
)
_S3_FORM = (
    "in the form its instructions ask for, with the answer first: open with "
    "the answer itself in one plain sentence and put everything else, any "
    "caveat included, after it; when the history holds nothing that answers "
    "the question, that first sentence says so. Keep each answer under about "
    "600 words."
)
for _span in (_L1_BATCH, _L1_SIZE, _L1_FORM):
    assert l1.READER_INSTRUCTIONS.count(_span) == 1, _span
READER_TEMPLATE = (
    l1.READER_INSTRUCTIONS.replace(_L1_BATCH, _S3_BATCH)
    .replace(_L1_SIZE, _S3_SIZE)
    .replace(_L1_FORM, _S3_FORM)
)


def reader_instructions(
    batch_file: Path, answers_file: Path, chars: int, lines: int
) -> str:
    return READER_TEMPLATE.format(
        batch=batch_file, answers=answers_file, chars=chars, lines=lines
    )


def arm_named(name: str) -> s0.Arm:
    got = [a for a in ROUND_ARMS if a.name == name]
    if not got:
        raise SystemExit(f"{name} is not one of S3's round arms")
    return got[0]


def served_prompt(
    service: MemoryService, inst: dict[str, Any], arm: s0.Arm, split: str
) -> tuple[dict[str, Any], str]:
    """One question served under a round arm from a store that holds its
    haystack, its coverage and its LongMemEval reading prompt."""
    positions = s0.round_positions(service, inst)
    hits = serve(service, inst, arm, BUDGET)
    context = l1.served_context(hits)
    got = s0.coverage(inst, hits, positions)
    prompt = l1.reading_prompt(context, inst["question_date"], inst["question"])
    qid = inst["question_id"]
    return {
        "question_id": qid,
        "question_type": inst["question_type"],
        "question": inst["question"],
        "gold": str(inst["answer"]),
        "abstention": "_abs" in qid,
        "split": split,
        "question_date": inst["question_date"],
        "n_hits": len(hits),
        "context_chars": len(context),
        "prompt_chars": len(prompt),
        "context_sha256": l1._sha(context),
        "prompt_sha256": l1._sha(prompt),
        "answer_turns": got["answer_turns"],
        "covered": got["covered"],
    }, prompt


def _served_job(
    job: tuple[str, dict[str, Any], str, str],
) -> tuple[dict[str, Any], str]:
    root, inst, name, split = job
    return served_prompt(service_for(Path(root)), inst, arm_named(name), split)


def _s0_arm_chars(job: tuple[str, dict[str, Any]]) -> int:
    root, inst = job
    hits = serve(service_for(Path(root)), inst, S0_ARM, BUDGET)
    return len(l1.served_context(hits))


def write_reads(
    work: Path,
    built: list[tuple[dict[str, Any], str]],
    unit: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """The prompts, their wrap for the Read tool, one batch a question, the
    reader instructions at the measured file size, and meta.json."""
    for sub in ("prompts", "prompts_wrapped", "batches", "readers", "answers"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for row, prompt in built:
        qid = row["question_id"]
        wrapped, inserted = l1.wrap_for_read(prompt)
        (work / "prompts" / f"{qid}.txt").write_bytes(prompt.encode("utf-8"))
        (work / "prompts_wrapped" / f"{qid}.txt").write_bytes(wrapped.encode("utf-8"))
        row.update(
            wrap_inserted=inserted,
            wrapped_lines=l1.line_count(wrapped),
            longest_line=max(len(x) for x in wrapped.split("\n")),
        )
        rows.append(row)
    chars = s1.round_up(max(r["prompt_chars"] for r in rows), 1_000)
    lines = s1.round_up(max(r["wrapped_lines"] for r in rows), 100)
    batches = [[r["question_id"]] for r in rows]
    instructions: list[str] = []
    for i, batch in enumerate(batches):
        # L1's names: batch_00 to batch_99, then batch_100 and on
        batch_file = work / "batches" / f"batch_{i:02d}.txt"
        answers_file = work / "answers" / f"batch_{i:02d}.json"
        listing = "".join(f"{work / 'prompts_wrapped' / q}.txt\n" for q in batch)
        batch_file.write_text(listing, encoding="utf-8")
        text = reader_instructions(batch_file, answers_file, chars, lines)
        (work / "readers" / f"reader_{i:02d}.txt").write_text(text, encoding="utf-8")
        instructions.append(text)
    meta = {
        "unit": unit,
        "batch_size": READ_BATCH,
        "stated_chars": chars,
        "stated_lines": lines,
        "reader_template_sha256": l1._sha(READER_TEMPLATE),
        **extra,
        "batches": batches,
        "instructions": instructions,
        "rows": rows,
    }
    l1._write_json(work / "meta.json", meta)
    return meta


def cmd_read_prompts(args: argparse.Namespace) -> None:
    """S3b (dev), S3c (hindsight) or S3d (holdout): the prompts to read."""
    work = Path(args.work).resolve()
    if (work / "meta.json").exists():
        raise SystemExit(f"{work} already holds a read; each read is built once")
    corpus = l1._corpus()
    extra: dict[str, Any] = {}
    if args.which == "hindsight":
        by_id = h1.check_run(h1.load_run(Path(args.run_file)), corpus)
        split = h1.splits(corpus)
        built = [
            h1.build(
                inst, by_id[inst["question_id"]]["context"], split[inst["question_id"]]
            )
            for inst in corpus
        ]
        unit = "S3c"
    else:
        if not os.environ.get("BETTERMEMORY_KEYS_DIR"):
            raise SystemExit("set BETTERMEMORY_KEYS_DIR to the store's receipts' keys")
        arm = arm_named(args.arm)
        cells: dict[str, Any] = {}
        if args.which == "dev":
            instances, root, unit = s0.dev_instances(corpus), Path(args.store), "S3b"
            cells = {
                r["question_id"]: r["arms"][arm.name]
                for r in l1._load_json(Path(args.coverage))["rows"]["s_dev"]
            }
        else:
            _, holdout = aml_run.split(corpus)
            keep = set(holdout)
            instances = [i for i in corpus if i["question_id"] in keep]
            root, unit = Path(args.store), "S3d"
            recorded = {
                r["question_id"]: r["context_chars"]
                for r in l1._load_json(Path(args.s2_artifact))["rows"]
            }
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                chars = list(
                    pool.map(_s0_arm_chars, [(str(root), i) for i in instances])
                )
            differ = [
                i["question_id"]
                for i, c in zip(instances, chars, strict=True)
                if recorded[i["question_id"]] != c
            ]
            if differ:
                raise SystemExit(f"S2 parity: {len(differ)} contexts differ: {differ}")
            extra["s2_parity"] = {"n": len(instances), "equal": len(instances)}
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            built = list(
                pool.map(
                    _served_job,
                    [(str(root), i, arm.name, unit_split(unit)) for i in instances],
                )
            )
        if args.which == "dev":
            differ = [
                row["question_id"]
                for row, _ in built
                if cells[row["question_id"]]["chars"] != row["context_chars"]
                or cells[row["question_id"]]["covered"] != row["covered"]
            ]
            if differ:
                raise SystemExit(f"G1 parity: {len(differ)} contexts differ: {differ}")
            extra["g1_parity"] = {"n": len(built), "equal": len(built)}
        extra["arm"] = arm.name
        extra["budget"] = BUDGET
    meta = write_reads(work, built, unit, extra)
    rows = meta["rows"]
    labelled = [r for r in rows if r["answer_turns"]]
    print(
        json.dumps(
            {
                "unit": unit,
                "questions": len(rows),
                "readers": len(meta["batches"]),
                "labelled": len(labelled),
                "covered": sum(r["covered"] for r in labelled),
                "stated_chars": meta["stated_chars"],
                "stated_lines": meta["stated_lines"],
                "prompt_chars_mean": round(
                    statistics.fmean(r["prompt_chars"] for r in rows)
                ),
                "context_chars_mean": round(
                    statistics.fmean(r["context_chars"] for r in rows)
                ),
            }
        )
    )


def unit_split(unit: str) -> str:
    return {"S3b": "dev", "S3d": "holdout"}[unit]


def verdicts_of(work: Path) -> dict[str, dict[str, bool | None]]:
    """Each judge's verdict per question from a work directory's
    judgments.json (L1's `judge` step)."""
    judged = l1._load_json(work / "judgments.json")
    return {
        name: {q: r["verdict"] for q, r in judged[name]["rows"].items()}
        for name in JUDGES
    }


def gate2(
    rows: list[dict[str, Any]],
    verdicts: dict[str, dict[str, bool | None]],
    s1_verdicts: dict[str, dict[str, bool | None]],
) -> dict[str, Any]:
    """G2 under each judge: 150 of 150, or 149 with the out-of-reach
    question the only miss; no question S1 answered right lost; every
    abstention right. A missing verdict is a wrong answer."""
    out: dict[str, Any] = {}
    for name in JUDGES:
        ok = {
            r["question_id"]: verdicts[name].get(r["question_id"]) is True for r in rows
        }
        misses = sorted(q for q, v in ok.items() if not v)
        lost = sorted(q for q in ok if s1_verdicts[name].get(q) is True and not ok[q])
        abst = [ok[r["question_id"]] for r in rows if r["abstention"]]
        out[name] = {
            "correct": sum(ok.values()),
            "misses": misses,
            "regressed_vs_s1": lost,
            "abstentions": {"n": len(abst), "correct": sum(abst)},
            "passed": misses in ([], [DEV_OUT_OF_REACH]) and not lost and all(abst),
        }
    out["passed"] = all(out[name]["passed"] for name in JUDGES)
    return out


def s1_dev_verdicts(s2_artifact: dict[str, Any]) -> dict[str, dict[str, bool | None]]:
    """S1's dev answers under each official judge, as S2 graded them."""
    return {
        name: {
            r["question_id"]: r["verdicts"]["s1"][model]
            for r in s2_artifact["dev_rows"]
        }
        for name, model in JUDGES.items()
    }


def cmd_gate2(args: argparse.Namespace) -> None:
    work = Path(args.work).resolve()
    rows = l1._load_json(work / "meta.json")["rows"]
    got = gate2(
        rows,
        verdicts_of(work),
        s1_dev_verdicts(l1._load_json(Path(args.s2_artifact))),
    )
    l1._write_json(work / "gate2.json", got)
    print(json.dumps(got, indent=1))


def gate3(
    rows: list[dict[str, Any]], verdicts: dict[str, dict[str, bool | None]]
) -> dict[str, Any]:
    """The target T under each judge: Hindsight's context under the
    briefing, all 500, a missing verdict wrong."""
    return {
        name: {
            "correct": sum(verdicts[name].get(r["question_id"]) is True for r in rows),
            "n": len(rows),
            "misses": sorted(
                r["question_id"]
                for r in rows
                if verdicts[name].get(r["question_id"]) is not True
            ),
        }
        for name in JUDGES
    }


def cmd_gate3(args: argparse.Namespace) -> None:
    work = Path(args.work).resolve()
    rows = l1._load_json(work / "meta.json")["rows"]
    got = gate3(rows, verdicts_of(work))
    l1._write_json(work / "gate3.json", got)
    print(json.dumps(got, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("mdev")
    s.add_argument("--m-corpus", default=str(M_CORPUS))
    s.add_argument("--out", default=str(M_DEV))
    s.set_defaults(fn=cmd_mdev)
    s = sub.add_parser("ingest")
    s.add_argument("--corpus", choices=("s", "m"), required=True)
    s.add_argument("--store", required=True)
    s.add_argument("--m-dev", default=str(M_DEV))
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(fn=cmd_ingest)
    s = sub.add_parser("coverage")
    s.add_argument("--s-store", default=str(S_STORE))
    s.add_argument("--m-store", default=str(M_STORE))
    s.add_argument("--m-dev", default=str(M_DEV))
    s.add_argument("--s0-artifact", default=str(S0_ARTIFACT))
    s.add_argument("--upstream", default=str(l1.UPSTREAM_GENERATION))
    s.add_argument("--out", default=str(OUT))
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(fn=cmd_coverage)
    s = sub.add_parser("read-prompts")
    s.add_argument("--which", choices=("dev", "hindsight", "holdout"), required=True)
    s.add_argument("--work", required=True)
    s.add_argument("--arm", default=None)
    s.add_argument("--store", default=None)
    s.add_argument("--coverage", default=str(OUT))
    s.add_argument("--s2-artifact", default=str(S2_ARTIFACT))
    s.add_argument("--run-file", default=str(H1_RUN_FILE))
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(fn=cmd_read_prompts)
    s = sub.add_parser("collect")
    s.add_argument("--work", required=True)
    s.set_defaults(fn=l1.cmd_collect)
    s = sub.add_parser("audit")
    s.add_argument("--work", required=True)
    s.add_argument("--transcripts", required=True)
    s.set_defaults(fn=s1.cmd_audit, role="readers")
    s = sub.add_parser("gate2")
    s.add_argument("--work", default=str(WORK_DEV))
    s.add_argument("--s2-artifact", default=str(S2_ARTIFACT))
    s.set_defaults(fn=cmd_gate2)
    s = sub.add_parser("gate3")
    s.add_argument("--work", default=str(WORK_HINDSIGHT))
    s.set_defaults(fn=cmd_gate3)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
