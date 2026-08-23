"""F1 stage-C weak-pair readers over the pinned Stack Exchange archives.

The F1 declaration (private register; memory-resident summary) pins the
stage-C frame — InfoNCE over source-stratified batches of weak pairs
derived from pinned bytes by committed readers — and defers the exact
reader set to the C addendum. This module is that committed reader set.
Three deterministic readers share one streaming pass per archive:

- ``tb`` title/body: each question row pairs its cleaned title with its
  own body prose — the (short, long) same-document pair.
- ``qa`` question/accepted-answer: each question row that names an
  AcceptedAnswerId pairs its cleaned title with that answer's body
  prose. Answers are created after their questions and Posts.xml rows
  are Id-ordered, so one forward pass resolves every pair the dump can
  resolve; questions whose accepted answer never arrives (deleted
  answers) are counted as unresolved.
- ``cm`` comment adjacency: consecutive comments on the same post pair
  as conversational turns. The per-site archives carry Comments.xml;
  the Stack Overflow pin is Posts-only and reads without a comment leg.
  A comment that cleans to an empty line neither pairs nor replaces the
  stored predecessor; a nonempty cleaned comment always becomes the new
  predecessor for its post, whether or not it paired.

No pair bytes are materialized. Every emitted pair line feeds a
per-reader sha256 — the stream receipt — and the artifact records
counts, drop and unresolved accounting, content-token length
histograms, and the receipts. Whatever consumes the pairs (the stage-C
run) re-runs this reader over the same pinned bytes and must reproduce
the receipts before training; CI drives the same core twice over a
synthetic fixture (tests/test_f1_pairs_determinism.py).

Cleaning imports the committed rules verbatim: the title cleaning and
content tokenizer from w3p_pairs, body prose surfaces from
w2_body_census. The pair-drop rule is the house rule: both surfaces
nonempty, case-insensitively distinct, at least two content tokens on
each side.

Run: fastvenv/bin/python bench/w/f1_pairs.py            # the 18 per-site pins
     fastvenv/bin/python bench/w/f1_pairs.py --so       # the Stack Overflow pin
     fastvenv/bin/python bench/w/f1_pairs.py --site cooking-stackexchange-archive
Artifacts: f1-pairs-sites-<date>.json / f1-pairs-so-<date>.json in
bench/w/results/.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from w2_body_census import _BODY_RE, body_surfaces  # noqa: E402
from w3p2_pairs import (  # noqa: E402
    SITE_NAMES,
    _row_id,
    iter_member_lines,
)
from w3p_pairs import (  # noqa: E402
    REGISTER,
    _clean_line,
    archive_members,
    content_tokens,
    provenance,
    row_post_type,
    sha256_of,
    strip_title_markers,
)

SO_POSTS_PIN = "stackoverflow-posts-archive"
POSTS_MEMBER = "Posts.xml"
COMMENTS_MEMBER = "Comments.xml"
READERS = ("tb", "qa", "cm")

_TITLE_RE_B = re.compile(rb'\bTitle="([^"]*)"')
_ACCEPTED_RE_B = re.compile(rb'\bAcceptedAnswerId="(\d+)"')
_POST_ID_RE_B = re.compile(rb'\bPostId="(\d+)"')
_TEXT_RE_B = re.compile(rb'\bText="([^"]*)"')

# Content-token length histogram buckets, per pair side: the label is
# the inclusive lower bound of a power-of-two range.
_HIST_BOUNDS = (2, 4, 8, 16, 32, 64, 128, 256, 512)


def _clean_title(raw: bytes) -> str:
    text = html.unescape(raw.decode("utf-8", errors="replace"))
    return _clean_line(strip_title_markers(text))


def _clean_comment(raw: bytes) -> str:
    return _clean_line(html.unescape(raw.decode("utf-8", errors="replace")))


def _bucket(tokens: int) -> str:
    lower = _HIST_BOUNDS[0]
    for bound in _HIST_BOUNDS:
        if tokens < bound:
            break
        lower = bound
    return str(lower)


@dataclass
class ReaderStats:
    """One reader's running counts and its stream receipt."""

    pairs: int = 0
    dropped_by_rule: int = 0
    unresolved: int = 0
    left_hist: dict[str, int] = field(default_factory=dict)
    right_hist: dict[str, int] = field(default_factory=dict)
    digest: Any = field(default_factory=hashlib.sha256)

    def feed(self, line: str, left_tokens: int, right_tokens: int) -> None:
        self.pairs += 1
        self.digest.update(line.encode())
        for hist, tokens in (
            (self.left_hist, left_tokens),
            (self.right_hist, right_tokens),
        ):
            key = _bucket(tokens)
            hist[key] = hist.get(key, 0) + 1

    def payload(self, with_unresolved: bool = False) -> dict[str, object]:
        out: dict[str, object] = {
            "pairs": self.pairs,
            "dropped_by_rule": self.dropped_by_rule,
            "stream_sha256": self.digest.hexdigest(),
            "left_tokens_hist": dict(
                sorted(self.left_hist.items(), key=lambda i: int(i[0]))
            ),
            "right_tokens_hist": dict(
                sorted(self.right_hist.items(), key=lambda i: int(i[0]))
            ),
        }
        if with_unresolved:
            out["unresolved"] = self.unresolved
        return out


@dataclass
class SitePairCensus:
    """One archive's read: row accounting plus the three readers."""

    status: str = "read"
    rows: int = 0
    row_counts: dict[str, int] = field(default_factory=dict)
    comment_rows: int | None = None
    tb: ReaderStats = field(default_factory=ReaderStats)
    qa: ReaderStats = field(default_factory=ReaderStats)
    cm: ReaderStats = field(default_factory=ReaderStats)

    def payload(self) -> dict[str, object]:
        if self.status != "read":
            return {"status": self.status}
        readers: dict[str, object] = {
            "tb": self.tb.payload(),
            "qa": self.qa.payload(with_unresolved=True),
        }
        if self.comment_rows is None:
            readers["cm"] = {"status": "no-member"}
        else:
            cm = self.cm.payload()
            cm["comment_rows"] = self.comment_rows
            readers["cm"] = cm
        receipt = hashlib.sha256()
        for name in READERS:
            receipt.update(getattr(self, name).digest.hexdigest().encode())
        return {
            "status": self.status,
            "rows_scanned": self.rows,
            "rows_by_post_type": dict(sorted(self.row_counts.items())),
            "readers": readers,
            "site_receipt_sha256": receipt.hexdigest(),
        }


def _kept_tokens(left: str, right: str) -> tuple[int, int] | None:
    """The house pair-drop rule; token counts when the pair is kept."""
    if not left or not right or left.lower() == right.lower():
        return None
    left_tokens = len(content_tokens(left))
    right_tokens = len(content_tokens(right))
    if left_tokens < 2 or right_tokens < 2:
        return None
    return left_tokens, right_tokens


def read_site(
    site: str,
    post_lines: Iterable[bytes],
    comment_lines: Iterable[bytes] | None = None,
) -> SitePairCensus:
    """The declared per-archive read: one Posts pass, one Comments pass."""
    census = SitePairCensus()
    pending: dict[bytes, tuple[str, str]] = {}

    for raw in post_lines:
        post_type = row_post_type(raw)
        if post_type is None:
            continue
        census.rows += 1
        key = post_type.decode("ascii", errors="replace")
        census.row_counts[key] = census.row_counts.get(key, 0) + 1
        if post_type == b"1":
            row_id = _row_id(raw)
            title_m = _TITLE_RE_B.search(raw)
            body_m = _BODY_RE.search(raw)
            if row_id is None or title_m is None:
                continue
            title = _clean_title(title_m.group(1))
            if body_m is not None:
                prose, _ = body_surfaces(body_m.group(1))
                kept = _kept_tokens(title, prose)
                if kept is None:
                    census.tb.dropped_by_rule += 1
                else:
                    line = f"{site}\ttb\t{row_id.decode('ascii')}\t{title}\t{prose}\n"
                    census.tb.feed(line, *kept)
            accepted_m = _ACCEPTED_RE_B.search(raw)
            if accepted_m is not None and title:
                pending[accepted_m.group(1)] = (row_id.decode("ascii"), title)
        elif post_type == b"2":
            row_id = _row_id(raw)
            if row_id is None or row_id not in pending:
                continue
            question_id, title = pending.pop(row_id)
            body_m = _BODY_RE.search(raw)
            if body_m is None:
                census.qa.dropped_by_rule += 1
                continue
            prose, _ = body_surfaces(body_m.group(1))
            kept = _kept_tokens(title, prose)
            if kept is None:
                census.qa.dropped_by_rule += 1
            else:
                line = f"{site}\tqa\t{question_id}\t{title}\t{prose}\n"
                census.qa.feed(line, *kept)
    census.qa.unresolved = len(pending)
    pending.clear()

    if comment_lines is not None:
        census.comment_rows = 0
        last: dict[bytes, str] = {}
        for raw in comment_lines:
            post_m = _POST_ID_RE_B.search(raw)
            text_m = _TEXT_RE_B.search(raw)
            if post_m is None or text_m is None:
                continue
            census.comment_rows += 1
            text = _clean_comment(text_m.group(1))
            if not text:
                continue
            post_id = post_m.group(1)
            previous = last.get(post_id)
            if previous is not None:
                kept = _kept_tokens(previous, text)
                if kept is None:
                    census.cm.dropped_by_rule += 1
                else:
                    line = (
                        f"{site}\tcm\t{post_id.decode('ascii')}\t{previous}\t{text}\n"
                    )
                    census.cm.feed(line, *kept)
            last[post_id] = text
    return census


def _register_by_name() -> dict[str, dict[str, Any]]:
    register = json.loads(REGISTER.read_text())
    return {str(e.get("name")): dict(e) for e in register["corpora"]}


def _reverify(entry: dict[str, Any]) -> dict[str, Any]:
    archive = REPO / str(entry["local_path"])
    pinned_sha = str(entry["sha256"])
    print(f"{archive.name}: re-verifying pinned sha256 ...", file=sys.stderr)
    started = time.time()
    actual_sha = sha256_of(archive)
    if actual_sha != pinned_sha:
        raise SystemExit(
            f"{archive.name}: sha mismatch (pinned {pinned_sha}, actual"
            f" {actual_sha}); the pin is the authority — nothing is read"
        )
    return {
        "site": str(entry["name"]),
        "archive": str(entry["local_path"]),
        "sha256_pinned": pinned_sha,
        "sha256_reverified": actual_sha,
        "sha_seconds": round(time.time() - started, 1),
        "members": archive_members(archive),
    }


def _read_archive(entry: dict[str, Any], block: dict[str, Any]) -> SitePairCensus:
    name = str(entry["name"])
    archive = REPO / str(entry["local_path"])
    if POSTS_MEMBER not in block["members"]:
        print(f"{archive.name}: missing {POSTS_MEMBER} — recorded", file=sys.stderr)
        return SitePairCensus(status="missing-member")
    comments = (
        iter_member_lines(archive, COMMENTS_MEMBER)
        if COMMENTS_MEMBER in block["members"]
        else None
    )
    started = time.time()
    census = read_site(name, iter_member_lines(archive, POSTS_MEMBER), comments)
    print(
        f"{archive.name}: tb {census.tb.pairs} / qa {census.qa.pairs} /"
        f" cm {census.cm.pairs} pairs in {round(time.time() - started, 1)}s",
        file=sys.stderr,
    )
    return census


def _run(names: Sequence[str], out_path: Path) -> int:
    by_name = _register_by_name()
    missing = [n for n in names if n not in by_name]
    if missing:
        raise SystemExit(f"admitted archives missing from the register: {missing}")
    entries = [by_name[n] for n in names]
    entries.sort(key=lambda e: Path(str(e["local_path"])).name)

    started_all = time.time()
    archives_block: list[dict[str, Any]] = []
    sites: dict[str, dict[str, object]] = {}
    totals = {name: 0 for name in READERS}
    for entry in entries:
        block = _reverify(entry)
        archives_block.append(block)
        census = _read_archive(entry, block)
        sites[str(entry["name"])] = census.payload()
        if census.status == "read":
            for name in READERS:
                totals[name] += getattr(census, name).pairs

    payload: dict[str, object] = {
        "provenance": provenance(),
        "reader": "bench/w/f1_pairs.py",
        "archives": archives_block,
        "sites": sites,
        "pairs_total": totals,
        "pass_seconds": round(time.time() - started_all, 1),
    }
    out_path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    shown = out_path.relative_to(REPO) if out_path.is_relative_to(REPO) else out_path
    print(f"\nartifact: {shown}")
    print(f"pairs_total: {totals}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--so", action="store_true")
    parser.add_argument("--site", default=None, metavar="REGISTER_NAME")
    parser.add_argument("--out", default=None, metavar="PATH")
    args = parser.parse_args()
    date = time.strftime("%Y-%m-%d")
    if args.so and args.site:
        raise SystemExit("--so and --site are separate runs")
    if args.so:
        names = [SO_POSTS_PIN]
        default_out = REPO / "bench" / "w" / "results" / f"f1-pairs-so-{date}.json"
    elif args.site:
        names = [args.site]
        default_out = (
            REPO / "bench" / "w" / "results" / f"f1-pairs-{args.site}-{date}.json"
        )
    else:
        names = list(SITE_NAMES)
        default_out = REPO / "bench" / "w" / "results" / f"f1-pairs-sites-{date}.json"
    out_path = Path(args.out).expanduser() if args.out else default_out
    return _run(names, out_path)


__all__ = [
    "ReaderStats",
    "SitePairCensus",
    "read_site",
]

if __name__ == "__main__":
    raise SystemExit(main())
