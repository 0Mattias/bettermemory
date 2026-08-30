"""MSC (Multi-Session Chat) — loader and store construction, data not
committed.

MSC is the conversational corpus the third-instrument note names as
the right shape for a third instrument and for the store-scale
question: multi-session dialogues between recurring speaker pairs,
personal facts, casual register, with recorded time gaps between
sessions. Its DATA carries no redistribution grant (the ParlAI code is
MIT; the tarball is licensed by nobody), so nothing under
`bench/msc/data/` is committed and this module follows the exact
pattern `bench/longmemeval/data/` established: a documented fetch, a
pinned sha256, a gitignored payload, and results that are reproducible
only for someone holding the same download — a limitation stated in
any artifact rather than implied.

That non-committed path is an owner decision, recorded 2026-08-12:
the redistribution email (option 1 of THIRD_INSTRUMENT.md's unblock
list) is deferred, option 2 is authorized, and everything downstream
of the data is to be built and ready. Committing any MSC-derived
subsample or instrument still waits on the grant.

Fetch, once:

    mkdir -p bench/msc/data && cd bench/msc/data
    curl -LO https://parl.ai/downloads/msc/msc_v0.1.tar.gz
    tar -xzf msc_v0.1.tar.gz

The loader reads `session_5/<split>.txt`, whose rows carry COMPLETE
five-session episodes (`previous_dialogs` holds sessions 1-4 with the
gap after each; `dialog` is session 5), so one file yields the whole
chain without cross-file joining. Dates are synthetic: every episode's
final session is anchored at a fixed epoch and earlier sessions step
backwards through the recorded gaps, formatted exactly like the
LongMemEval runner's bracket prefix so store bodies have the same
shape. The anchor is arbitrary and shared; it claims nothing about
when MSC was collected.

    .venv/bin/python bench/msc/load.py --smoke

Smoke mode is PLUMBING VALIDATION ONLY: it loads, builds stores,
trains the store model with `bench/embed_train.py`'s own pipeline, and
scores one query mechanically. It prints counts and timings and no
verdict — any census over this corpus requires its own declared-first
document before a single cell runs, exactly like every census before
it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent
_SRC = _BENCH.parent / "src"
for _p in (str(_SRC), str(_BENCH)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bettermemory.store import Store  # noqa: E402

TARBALL_URL = "https://parl.ai/downloads/msc/msc_v0.1.tar.gz"
TARBALL_SHA256 = "e640e37cf4317cd09fc02a4cd57ef130a185f23635f4003b0cee341ffcb45e60"
DATA = _HERE / "data"
TARBALL = DATA / "msc_v0.1.tar.gz"
DIALOGUE_ROOT = DATA / "msc" / "msc_dialogue"
SCOPE = ["msc"]
SPLITS = ("test", "valid", "train")

# The synthetic-clock anchor. Every episode's final session sits here;
# earlier sessions step backwards through the recorded gaps. Fixed so
# two runs (and two machines) synthesise identical store bodies.
EPOCH = datetime(2023, 5, 20, 10, 0)

_UNIT_HOURS = {"hour": 1, "hours": 1, "day": 24, "days": 24}


def fetch_instructions() -> str:
    return (
        f"missing MSC data under {DATA}\n"
        "fetch it with:\n"
        "  mkdir -p bench/msc/data && cd bench/msc/data\n"
        f"  curl -LO {TARBALL_URL}\n"
        "  tar -xzf msc_v0.1.tar.gz\n"
        f"expected sha256: {TARBALL_SHA256}"
    )


def verify_data() -> Path:
    """The session_5 directory, after the tarball's pin is checked.

    The tarball must be present AND match the pin — an extracted tree
    without its tarball cannot be verified against anything, and an
    unpinned corpus would make every downstream number incomparable.
    Extraction is performed here when the tree is absent; that is local
    and deterministic, not a download.
    """
    if not TARBALL.exists():
        raise SystemExit(fetch_instructions())
    digest = hashlib.sha256(TARBALL.read_bytes()).hexdigest()
    if digest != TARBALL_SHA256:
        raise SystemExit(
            f"UNPINNED TARBALL — sha256 {digest[:16]}… does not match the "
            f"recorded {TARBALL_SHA256[:16]}…. Re-fetch from {TARBALL_URL}; "
            "results from an unpinned corpus are not comparable to anything."
        )
    if not DIALOGUE_ROOT.exists():
        with tarfile.open(TARBALL) as tar:
            tar.extractall(DATA, filter="data")
    session5 = DIALOGUE_ROOT / "session_5"
    if not session5.exists():
        raise SystemExit(f"tarball verified but {session5} is missing after extraction")
    return session5


def _gap_hours(entry: dict[str, Any]) -> int:
    num = entry.get("time_num")
    unit = entry.get("time_unit")
    if not isinstance(num, int) or unit not in _UNIT_HOURS:
        raise SystemExit(
            f"unrecognised session gap {num!r} {unit!r} — the loader pins "
            "the four unit spellings observed in msc_v0.1 and refuses to "
            "guess at new ones"
        )
    return num * _UNIT_HOURS[unit]


def _fmt(moment: datetime) -> str:
    return f"{moment:%Y/%m/%d} ({moment:%a}) {moment:%H:%M}"


def _rounds(texts: list[str]) -> list[str]:
    """Pair alternating turns into rounds, LongMemEval's body shape.

    `previous_dialogs` turns carry no speaker ids; MSC dialogues
    strictly alternate with Speaker 1 opening, so ids are synthesised
    positionally. A trailing unpaired turn is emitted alone rather
    than dropped, for the same reason the LongMemEval runner keeps it.
    """
    out: list[str] = []
    for i in range(0, len(texts), 2):
        parts = [f"Speaker 1: {texts[i]}"]
        if i + 1 < len(texts):
            parts.append(f"Speaker 2: {texts[i + 1]}")
        out.append("\n".join(parts))
    return out


def episode_rows(split: str = "test") -> list[dict[str, Any]]:
    if split not in SPLITS:
        raise SystemExit(f"unknown split {split!r}; choose from {SPLITS}")
    path = verify_data() / f"{split}.txt"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def corpus_fingerprint(split: str = "test") -> str:
    """sha256 of the split file an artifact was derived from.

    Every LongMemEval artifact records its corpus sha; anything derived
    from this uncommitted corpus must do the same, so a reader can tell
    whether two artifacts even saw the same bytes. The tarball pin
    guards the fetch; this fingerprints the specific file a run read.
    """
    if split not in SPLITS:
        raise SystemExit(f"unknown split {split!r}; choose from {SPLITS}")
    path = verify_data() / f"{split}.txt"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def episodes(split: str = "test") -> list[dict[str, Any]]:
    """Complete five-session episodes, in file order.

    Each: `{episode_id, personas, init_personas, sessions}` where a
    session is `{index (1-based), date, rounds}` and `date` is the
    synthetic bracket prefix computed from the recorded gaps.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in episode_rows(split):
        episode_id = str(row["metadata"]["initial_data_id"])
        if episode_id in seen:
            raise SystemExit(f"duplicate episode id {episode_id!r} in {split}")
        seen.add(episode_id)

        previous = row["previous_dialogs"]
        gaps = [_gap_hours(entry) for entry in previous]
        # sessions[k] starts at EPOCH minus every gap from k onwards;
        # the final session carries no gap and sits on the anchor.
        moments: list[datetime] = []
        for k in range(len(previous) + 1):
            back = sum(gaps[k:])
            moments.append(EPOCH - timedelta(hours=back))

        sessions: list[dict[str, Any]] = []
        for k, entry in enumerate(previous):
            texts = [str(turn["text"]) for turn in entry["dialog"]]
            sessions.append(
                {"index": k + 1, "date": _fmt(moments[k]), "rounds": _rounds(texts)}
            )
        final_texts = [str(turn["text"]) for turn in row["dialog"]]
        sessions.append(
            {
                "index": len(previous) + 1,
                "date": _fmt(moments[-1]),
                "rounds": _rounds(final_texts),
            }
        )
        out.append(
            {
                "episode_id": episode_id,
                "personas": row.get("personas"),
                "init_personas": row.get("init_personas"),
                "sessions": sessions,
            }
        )
    return out


def _write_sessions(
    store: Store, episode: dict[str, Any], session_key: Any
) -> tuple[dict[str, str], int]:
    id_to_session: dict[str, str] = {}
    n = 0
    for session in episode["sessions"]:
        key = session_key(episode, session)
        for body in session["rounds"]:
            memory = store.write(content=f"[{session['date']}]\n{body}", scopes=SCOPE)
            id_to_session[memory.id] = key
            n += 1
    return id_to_session, n


def build_episode_store(
    root: Path, episode: dict[str, Any]
) -> tuple[dict[str, str], int]:
    """One episode's five sessions as one store. Session keys are
    `s<index>`; like the LongMemEval runner, the key is never placed in
    a body or a scope, where it would be retrievable content."""
    return _write_sessions(Store(root), episode, lambda _e, s: f"s{s['index']}")


def build_aggregate_store(
    root: Path, episode_list: list[dict[str, Any]]
) -> tuple[dict[str, str], int]:
    """Many episodes in one store — the store-scale shape. Session keys
    are `<episode_id>/s<index>`. Mixing speaker pairs in one collection
    is a disclosed property of the aggregate, not an accident: the
    scale question is about corpus mass, and any census reading this
    store states the mixture."""
    store = Store(root)
    id_to_session: dict[str, str] = {}
    total = 0
    for episode in episode_list:
        mapping, n = _write_sessions(
            store, episode, lambda e, s: f"{e['episode_id']}/s{s['index']}"
        )
        id_to_session.update(mapping)
        total += n
    return id_to_session, total


# ---------------------------------------------------------------------------
# Smoke — plumbing validation only, no verdict
# ---------------------------------------------------------------------------


def smoke(split: str, n_episodes: int) -> int:
    import embed_train  # noqa: PLC0415

    started = time.time()
    eps = episodes(split)
    n_sessions = sum(len(e["sessions"]) for e in eps)
    n_rounds = sum(len(s["rounds"]) for e in eps for s in e["sessions"])
    n_chars = sum(len(r) for e in eps for s in e["sessions"] for r in s["rounds"])
    print(
        f"split={split}: {len(eps)} episodes, {n_sessions} sessions, "
        f"{n_rounds} rounds, {n_chars / 1e6:.1f}M chars "
        f"(loaded in {time.time() - started:.1f}s)"
    )

    root = Path(tempfile.mkdtemp(prefix="bm-msc-smoke-"))
    try:
        _mapping, n = build_episode_store(root / "one", eps[0])
        print(f"episode store: {n} items ({eps[0]['episode_id']})")

        t0 = time.time()
        id_to_session, n_agg = build_aggregate_store(root / "agg", eps[:n_episodes])
        memories = Store(root / "agg").load_all()
        print(
            f"aggregate store: {n_agg} items over {n_episodes} episodes "
            f"({time.time() - t0:.1f}s)"
        )

        t0 = time.time()
        units = [m.body for m in memories]
        streams = embed_train.token_streams(units)
        vocab, index = embed_train.build_vocab(streams)
        counts = embed_train.cooccurrence(streams, index)
        counts = {k: v for k, v in counts.items() if v >= embed_train.MIN_COOC}
        vectors, losses = embed_train.train(counts, len(vocab))
        print(
            f"trained: {sum(len(s) for s in streams)} tokens, vocab "
            f"{len(vocab)}, {len(counts)} cells, final loss "
            f"{losses[-1]:.4f} ({time.time() - t0:.1f}s)"
        )

        vec = {t: vectors[i] for i, t in enumerate(vocab)}
        probe = "what kind of car does my friend drive"
        from bettermemory.search import _expand_kebab, _strip_stopwords, tokenize  # noqa: PLC0415

        tokens = [
            t
            for t in sorted(set(_strip_stopwords(_expand_kebab(tokenize(probe)))))
            if t in vec
        ]
        if tokens:
            dim = len(vectors[0])
            pooled = [sum(vec[t][d] for t in tokens) / len(tokens) for d in range(dim)]
            best_key, best_score = "", float("-inf")
            for memory in memories:
                doc_tokens = [
                    t
                    for t in set(_strip_stopwords(_expand_kebab(tokenize(memory.body))))
                    if t in vec
                ]
                if not doc_tokens:
                    continue
                doc = [
                    sum(vec[t][d] for t in doc_tokens) / len(doc_tokens)
                    for d in range(dim)
                ]
                score = sum(a * b for a, b in zip(pooled, doc))
                if score > best_score:
                    best_score = score
                    best_key = id_to_session[memory.id]
        else:
            best_key = "(no probe token in vocab)"
        print(f"probe scored end-to-end; top session key: {best_key}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(
        "PLUMBING ONLY — no verdict, no artifact. A census over this "
        "corpus requires its own declaration first."
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="MSC loader. Data is not committed; see module docstring."
    )
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--split", default="test", choices=SPLITS)
    p.add_argument("--episodes", type=int, default=40, metavar="N")
    args = p.parse_args()
    if args.smoke:
        return smoke(args.split, args.episodes)
    verify_data()
    eps = episodes(args.split)
    print(f"{len(eps)} episodes in {args.split}; use --smoke for the full check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
