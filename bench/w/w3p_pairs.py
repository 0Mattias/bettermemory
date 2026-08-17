"""W3-P reader: duplicate-title pair extraction and the Stage-0 census.

Implements `bench/w/W3P_DECLARATION.md` §3 and §4. One streaming pass
over the pinned StackOverflow Posts archive: question rows carrying the
legacy "Possible Duplicate" closure blockquote yield (own title, target
title) pairs; the same pass counts rows by PostTypeId (the reader
census W1b reuses) and computes the register census against the
operationalized bridge needs (`w3p_anatomy.NEEDS`, fixed at the
declaration commit).

The archive's sha256 is re-verified over the exact bytes before any
row is read; the pair file is a derived intermediate written beside the
corpus (not committed) with its sha256 recorded in the census artifact.
The aggregation core (`census_from_rows`) is stream-agnostic so the CI
determinism leg can drive it with hand-written rows and no corpus
bytes.

Run: fastvenv/bin/python bench/w/w3p_pairs.py
Artifact: w3p-census-<date>.json in bench/w/results/.
"""

from __future__ import annotations

import hashlib
import html
import json
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterable, Iterator

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bettermemory.expansion import QUERY_FILLER_WORDS  # noqa: E402
from bettermemory.search import _stem_token  # noqa: E402
from w3p_anatomy import NEEDS  # noqa: E402

REGISTER = REPO / "bench" / "w" / "corpora.json"
CORPUS_NAME = "stackoverflow-posts-archive"
LME_CORPUS = REPO / "bench/longmemeval/data/longmemeval_s_cleaned.json"

# Declaration §4: the floors, fixed at the declaration commit.
FLOOR_V_PAIRS = 50_000
FLOOR_C_NEEDS = 3
FLOOR_C_PAIRS_PER_NEED = 5

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_FILLER_STEMS = frozenset(_stem_token(w) for w in QUERY_FILLER_WORDS)
_MARKERS = ("Possible Duplicate", "Possible duplicate")
_MARKERS_B = tuple(m.encode() for m in _MARKERS)
_TITLE_RE = re.compile(r'\bTitle="([^"]*)"')
_BODY_RE = re.compile(r'\bBody="([^"]*)"')
_ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_TRAILING_MARKER_RE = re.compile(r"\s*\[(?:duplicate|closed)\]\s*$", re.I)
_POST_TYPE_B = b'PostTypeId="'


def content_tokens(text: str) -> list[str]:
    """Declaration §3 tokenizer: lowercase alnum runs, length 3-30,
    engine query-filler stems dropped."""
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        if not 3 <= len(tok) <= 30:
            continue
        if _stem_token(tok) in _FILLER_STEMS:
            continue
        out.append(tok)
    return out


def strip_title_markers(title: str) -> str:
    """Strip trailing [duplicate] / [closed] markers, repeatedly."""
    while True:
        stripped = _TRAILING_MARKER_RE.sub("", title)
        if stripped == title:
            return title.strip()
        title = stripped


def _clean_line(text: str) -> str:
    return " ".join(text.split())


def pair_from_row(line: str) -> tuple[str, str] | None:
    """The declared pair rule over one XML row, or None.

    Caller pre-filters to question rows containing a marker; this
    function re-checks both so it is safe on any row.
    """
    if 'PostTypeId="1"' not in line:
        return None
    title_m = _TITLE_RE.search(line)
    body_m = _BODY_RE.search(line)
    if title_m is None or body_m is None:
        return None
    body = html.unescape(body_m.group(1))
    marker_at = min((i for m in _MARKERS if (i := body.find(m)) >= 0), default=-1)
    if marker_at < 0:
        return None
    anchor = _ANCHOR_RE.search(body, marker_at)
    if anchor is None:
        return None
    right = _clean_line(html.unescape(_TAG_RE.sub(" ", anchor.group(1))))
    left = _clean_line(strip_title_markers(html.unescape(title_m.group(1))))
    if not left or not right or left.lower() == right.lower():
        return None
    if len(content_tokens(left)) < 2 or len(content_tokens(right)) < 2:
        return None
    return left, right


def row_post_type(line: bytes) -> bytes | None:
    at = line.find(_POST_TYPE_B)
    if at < 0:
        return None
    start = at + len(_POST_TYPE_B)
    end = line.find(b'"', start)
    return line[start:end] if end > start else None


def need_supported(
    t1: frozenset[str] | set[str],
    t2: frozenset[str] | set[str],
    left: frozenset[str],
    right: frozenset[str],
) -> bool:
    """Exclusive substitution, either direction (declaration §4)."""

    def one_way(
        a_side: set[str] | frozenset[str], b_side: set[str] | frozenset[str]
    ) -> bool:
        return any(a in a_side and a not in b_side for a in left) and any(
            b in b_side and b not in a_side for b in right
        )

    return one_way(t1, t2) or one_way(t2, t1)


@dataclass
class Census:
    """The Stage-0 counts. Everything here is deterministic in the
    row stream; wall-clock lives outside, in the artifact only."""

    rows: int = 0
    row_counts: dict[str, int] = field(default_factory=dict)
    marker_rows: int = 0
    pairs_total: int = 0
    need_counts: dict[str, int] = field(default_factory=dict)
    pair_vocab: set[str] = field(default_factory=set)

    def counts_payload(self) -> dict[str, object]:
        need_rows = {
            qid: {
                "count": self.need_counts.get(qid, 0),
                "gloss": NEEDS[qid][3],
                "register": NEEDS[qid][2],
                "supported": self.need_counts.get(qid, 0) >= FLOOR_C_PAIRS_PER_NEED,
            }
            for qid in sorted(NEEDS)
        }
        supported = sum(1 for row in need_rows.values() if row["supported"])
        floors = {
            "V": {
                "threshold_pairs": FLOOR_V_PAIRS,
                "pairs_total": self.pairs_total,
                "holds": self.pairs_total >= FLOOR_V_PAIRS,
            },
            "C": {
                "threshold_needs": FLOOR_C_NEEDS,
                "pairs_per_need": FLOOR_C_PAIRS_PER_NEED,
                "needs_supported": supported,
                "holds": supported >= FLOOR_C_NEEDS,
            },
        }
        v_holds = bool(floors["V"]["holds"])
        c_holds = bool(floors["C"]["holds"])
        return {
            "rows_scanned": self.rows,
            "rows_by_post_type": dict(sorted(self.row_counts.items())),
            "marker_rows": self.marker_rows,
            "pairs_total": self.pairs_total,
            "pair_vocabulary_terms": len(self.pair_vocab),
            "needs": need_rows,
            "floors": floors,
            "g0_verdict": "PASS" if (v_holds and c_holds) else "PARK-AT-CENSUS",
        }


def census_from_rows(
    lines: Iterable[bytes], pairs_out: IO[bytes] | None = None
) -> Census:
    """The aggregation core: raw XML row lines in, census out.

    Stream-agnostic by design — the CI determinism leg drives this with
    hand-written rows; the real pass drives it with the bsdtar stream.
    """
    census = Census()
    needs = {qid: (spec[0], spec[1]) for qid, spec in NEEDS.items()}
    for raw in lines:
        post_type = row_post_type(raw)
        if post_type is None:
            continue
        census.rows += 1
        key = post_type.decode("ascii", errors="replace")
        census.row_counts[key] = census.row_counts.get(key, 0) + 1
        if post_type != b"1":
            continue
        if not any(m in raw for m in _MARKERS_B):
            continue
        census.marker_rows += 1
        pair = pair_from_row(raw.decode("utf-8", errors="replace"))
        if pair is None:
            continue
        left_title, right_title = pair
        census.pairs_total += 1
        if pairs_out is not None:
            pairs_out.write(f"{left_title}\t{right_title}\n".encode())
        t1 = frozenset(content_tokens(left_title))
        t2 = frozenset(content_tokens(right_title))
        census.pair_vocab.update(t1, t2)
        for qid, (left_set, right_set) in needs.items():
            if need_supported(t1, t2, left_set, right_set):
                census.need_counts[qid] = census.need_counts.get(qid, 0) + 1
    return census


def register_entry() -> dict[str, object]:
    register = json.loads(REGISTER.read_text())
    for entry in register["corpora"]:
        if entry.get("name") == CORPUS_NAME:
            return dict(entry)
    raise SystemExit(f"{CORPUS_NAME} not found in {REGISTER}")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_members(archive: Path) -> list[str]:
    out = subprocess.run(
        ["bsdtar", "-tf", str(archive)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def iter_archive_lines(archive: Path) -> Iterator[bytes]:
    proc = subprocess.Popen(
        ["bsdtar", "-xOf", str(archive)],
        stdout=subprocess.PIPE,
        bufsize=1 << 22,
    )
    assert proc.stdout is not None
    try:
        progress = 0
        for line in proc.stdout:
            progress += 1
            if progress % 5_000_000 == 0:
                print(f"  ... {progress:,} lines", file=sys.stderr)
            yield line
    finally:
        proc.stdout.close()
        returncode = proc.wait()
        if returncode != 0:
            raise RuntimeError(f"bsdtar exited {returncode}")


def register_overlap(pair_vocab: set[str]) -> dict[str, object]:
    """Informational: how much of the preference asks' and miss golds'
    vocabulary the pair corpus knows at all (declaration §4)."""
    corpus = json.loads(LME_CORPUS.read_text())
    ask_tokens: set[str] = set()
    gold_tokens: set[str] = set()
    for inst in corpus:
        if inst.get("question_type") != "single-session-preference":
            continue
        ask_tokens.update(content_tokens(inst["question"]))
        if inst.get("question_id") not in NEEDS:
            continue
        answer_sessions = set(inst["answer_session_ids"])
        for sid, session in zip(
            inst["haystack_session_ids"], inst["haystack_sessions"]
        ):
            if sid not in answer_sessions:
                continue
            for turn in session:
                gold_tokens.update(content_tokens(turn.get("content", "")))

    def fraction(tokens: set[str]) -> float:
        if not tokens:
            return 0.0
        return round(len(tokens & pair_vocab) / len(tokens), 4)

    return {
        "preference_ask_tokens": len(ask_tokens),
        "preference_ask_fraction_in_pair_vocab": fraction(ask_tokens),
        "miss_gold_tokens": len(gold_tokens),
        "miss_gold_fraction_in_pair_vocab": fraction(gold_tokens),
    }


def provenance() -> dict[str, object]:
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    ).stdout.strip()
    return {
        "commit": commit or "unknown",
        "date": time.strftime("%Y-%m-%d"),
        "machine": {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }


def main() -> int:
    entry = register_entry()
    archive = REPO / str(entry["local_path"])
    pinned_sha = str(entry["sha256"])
    date = time.strftime("%Y-%m-%d")
    derived_dir = REPO / "bench" / "w" / "corpus" / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    pairs_path = derived_dir / f"w3p-pairs-{date}.tsv"
    census_path = REPO / "bench" / "w" / "results" / f"w3p-census-{date}.json"

    print(f"re-verifying pinned sha256 over {archive} ...", file=sys.stderr)
    started = time.time()
    actual_sha = sha256_of(archive)
    if actual_sha != pinned_sha:
        raise SystemExit(
            f"archive sha mismatch: pinned {pinned_sha}, actual {actual_sha};"
            " the pin is the authority — nothing is read"
        )
    sha_seconds = round(time.time() - started, 1)
    print(f"sha256 verified in {sha_seconds}s", file=sys.stderr)

    members = archive_members(archive)
    print(f"members: {members}", file=sys.stderr)

    started = time.time()
    with pairs_path.open("wb") as pairs_out:
        census = census_from_rows(iter_archive_lines(archive), pairs_out)
    pass_seconds = round(time.time() - started, 1)
    pairs_sha = sha256_of(pairs_path)

    artifact: dict[str, object] = {
        "provenance": provenance(),
        "declaration": "bench/w/W3P_DECLARATION.md",
        "corpus": {
            "name": CORPUS_NAME,
            "archive": str(entry["local_path"]),
            "sha256_pinned": pinned_sha,
            "sha256_reverified": actual_sha,
            "sha_seconds": sha_seconds,
            "members": members,
        },
        "pair_file": {
            "path": str(pairs_path.relative_to(REPO)),
            "sha256": pairs_sha,
            "bytes": pairs_path.stat().st_size,
        },
        "census": census.counts_payload(),
        "register_overlap": register_overlap(census.pair_vocab),
        "pass_seconds": pass_seconds,
    }
    census_path.write_text(json.dumps(artifact, indent=1) + "\n")

    payload = census.counts_payload()
    print(json.dumps(payload, indent=1))
    print(f"\ncensus artifact: {census_path.relative_to(REPO)}")
    print(f"pair file: {pairs_path} (sha256 {pairs_sha[:16]}...)")
    print(f"G0 verdict: {payload['g0_verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
