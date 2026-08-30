"""W3-P2 reader: labeled duplicate-pair extraction and the Stage-0 census.

Implements the W3-P2 declaration §3 and §4 over the eighteen
per-site Stack Exchange archives the declaration admits. Per archive,
in ascending order of the archive filename: the pinned sha256 is
re-verified over the exact bytes, then `PostLinks.xml` is streamed for
LinkTypeId=3 duplicate edges (document order, exact repeats dropped),
then `Posts.xml` is streamed for the per-site PostTypeId census and the
titles of question rows on either side of an edge. Each resolved edge
yields a (left title, right title) pair under W3-P's cleaning and
tokenizer, imported verbatim from `w3p_pairs`; the census counts need
support against the frozen `w3p_anatomy.NEEDS` sets.

The aggregation core (`W3P2Census.add_site`) is stream-agnostic so the
CI determinism leg (`tests/test_w3p2_determinism.py`) can drive it with
hand-written rows and no corpus bytes — including the missing-member
rule, edge dedup, and unresolved-edge accounting the declaration names.

Run: fastvenv/bin/python bench/w/w3p2_pairs.py
Artifact: w3p2-census-<date>.json in bench/w/results/.
"""

from __future__ import annotations

import html
import json
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

from w3p_anatomy import NEEDS  # noqa: E402
from w3p_pairs import (  # noqa: E402
    REGISTER,
    _clean_line,
    archive_members,
    content_tokens,
    need_supported,
    provenance,
    register_overlap,
    row_post_type,
    sha256_of,
    strip_title_markers,
)

# Declaration §3: the eighteen admitted archives, by register name.
SITE_NAMES = (
    "academia-stackexchange-archive",
    "android-stackexchange-archive",
    "apple-stackexchange-archive",
    "beer-stackexchange-archive",
    "coffee-stackexchange-archive",
    "cooking-stackexchange-archive",
    "diy-stackexchange-archive",
    "fitness-stackexchange-archive",
    "gardening-stackexchange-archive",
    "interpersonal-stackexchange-archive",
    "lifehacks-stackexchange-archive",
    "movies-stackexchange-archive",
    "music-stackexchange-archive",
    "outdoors-stackexchange-archive",
    "parenting-stackexchange-archive",
    "pets-stackexchange-archive",
    "superuser-archive",
    "travel-stackexchange-archive",
)
REQUIRED_MEMBERS = ("PostLinks.xml", "Posts.xml")

# Declaration §4: the floors, fixed at the declaration commit, and the
# W3-P floors the continuity row reports informationally.
FLOOR_V_PAIRS = 25_000
FLOOR_C_NEEDS = 4
FLOOR_C_PAIRS_PER_NEED = 5
W3P_FLOOR_V_PAIRS = 50_000
W3P_FLOOR_C_NEEDS = 3

_ID_B = b' Id="'
_LINK_TYPE_DUP_B = b'LinkTypeId="3"'
_LINK_ROW_B = b"<row "
_POST_ID_RE = re.compile(rb'\bPostId="(\d+)"')
_RELATED_ID_RE = re.compile(rb'\bRelatedPostId="(\d+)"')
_TITLE_RE = re.compile(rb'\bTitle="([^"]*)"')


def _row_id(line: bytes) -> bytes | None:
    at = line.find(_ID_B)
    if at < 0:
        return None
    start = at + len(_ID_B)
    end = line.find(b'"', start)
    return line[start:end] if end > start else None


@dataclass
class EdgeStats:
    """The edge pass over one site's PostLinks.xml (declaration §3.2)."""

    link_rows: int = 0
    duplicate_rows: int = 0
    edges: list[tuple[bytes, bytes]] = field(default_factory=list)

    @property
    def deduped(self) -> int:
        return len(self.edges)


def duplicate_edges(lines: Iterable[bytes]) -> EdgeStats:
    """LinkTypeId=3 edges in document order, exact repeats dropped."""
    stats = EdgeStats()
    seen: set[tuple[bytes, bytes]] = set()
    for raw in lines:
        if _LINK_ROW_B not in raw:
            continue
        stats.link_rows += 1
        if _LINK_TYPE_DUP_B not in raw:
            continue
        post_m = _POST_ID_RE.search(raw)
        related_m = _RELATED_ID_RE.search(raw)
        if post_m is None or related_m is None:
            continue
        stats.duplicate_rows += 1
        edge = (post_m.group(1), related_m.group(1))
        if edge in seen:
            continue
        seen.add(edge)
        stats.edges.append(edge)
    return stats


def _clean_title(raw: bytes) -> str:
    text = html.unescape(raw.decode("utf-8", errors="replace"))
    return _clean_line(strip_title_markers(text))


@dataclass
class SiteCensus:
    """One site's rows in the artifact (declaration §4)."""

    status: str = "read"
    rows: int = 0
    row_counts: dict[str, int] = field(default_factory=dict)
    link_rows: int = 0
    duplicate_rows: int = 0
    deduped_edges: int = 0
    unresolved_edges: int = 0
    dropped_by_rule: int = 0
    pairs: int = 0
    need_counts: dict[str, int] = field(default_factory=dict)

    def payload(self) -> dict[str, object]:
        if self.status != "read":
            return {"status": self.status, "pairs": 0}
        return {
            "status": self.status,
            "rows_scanned": self.rows,
            "rows_by_post_type": dict(sorted(self.row_counts.items())),
            "link_rows": self.link_rows,
            "duplicate_edge_rows": self.duplicate_rows,
            "deduped_edges": self.deduped_edges,
            "unresolved_edges": self.unresolved_edges,
            "dropped_by_rule": self.dropped_by_rule,
            "pairs": self.pairs,
            "needs": {qid: self.need_counts.get(qid, 0) for qid in sorted(NEEDS)},
        }


@dataclass
class W3P2Census:
    """The Stage-0 aggregate. Deterministic in the streams; wall-clock
    lives outside, in the artifact only."""

    sites: dict[str, SiteCensus] = field(default_factory=dict)
    pairs_total: int = 0
    need_counts: dict[str, int] = field(default_factory=dict)
    pair_vocab: set[str] = field(default_factory=set)

    def add_missing(self, site: str) -> SiteCensus:
        census = SiteCensus(status="missing-member")
        self.sites[site] = census
        return census

    def add_site(
        self,
        site: str,
        link_lines: Iterable[bytes],
        post_lines: Iterable[bytes],
        pairs_out: IO[bytes] | None = None,
    ) -> SiteCensus:
        """The declared per-site read: edge pass, title pass, pair rule."""
        census = SiteCensus()
        self.sites[site] = census

        edge_stats = duplicate_edges(link_lines)
        census.link_rows = edge_stats.link_rows
        census.duplicate_rows = edge_stats.duplicate_rows
        census.deduped_edges = edge_stats.deduped
        wanted: set[bytes] = set()
        for post_id, related_id in edge_stats.edges:
            wanted.add(post_id)
            wanted.add(related_id)

        titles: dict[bytes, bytes] = {}
        for raw in post_lines:
            post_type = row_post_type(raw)
            if post_type is None:
                continue
            census.rows += 1
            key = post_type.decode("ascii", errors="replace")
            census.row_counts[key] = census.row_counts.get(key, 0) + 1
            if post_type != b"1":
                continue
            row_id = _row_id(raw)
            if row_id is None or row_id not in wanted:
                continue
            title_m = _TITLE_RE.search(raw)
            if title_m is not None:
                titles[row_id] = title_m.group(1)

        needs = {qid: (spec[0], spec[1]) for qid, spec in NEEDS.items()}
        for post_id, related_id in edge_stats.edges:
            left_raw = titles.get(post_id)
            right_raw = titles.get(related_id)
            if left_raw is None or right_raw is None:
                census.unresolved_edges += 1
                continue
            left = _clean_title(left_raw)
            right = _clean_title(right_raw)
            if not left or not right or left.lower() == right.lower():
                census.dropped_by_rule += 1
                continue
            t1 = frozenset(content_tokens(left))
            t2 = frozenset(content_tokens(right))
            if len(t1) < 2 or len(t2) < 2:
                census.dropped_by_rule += 1
                continue
            census.pairs += 1
            self.pairs_total += 1
            if pairs_out is not None:
                pairs_out.write(f"{site}\t{left}\t{right}\n".encode())
            self.pair_vocab.update(t1, t2)
            for qid, (left_set, right_set) in needs.items():
                if need_supported(t1, t2, left_set, right_set):
                    census.need_counts[qid] = census.need_counts.get(qid, 0) + 1
                    self.need_counts[qid] = self.need_counts.get(qid, 0) + 1
        return census

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
        w3p_supported = sum(
            1 for row in need_rows.values() if row["count"] >= FLOOR_C_PAIRS_PER_NEED
        )
        continuity = {
            "_": "informational: the W3-P floors applied to these counts",
            "V_50000_holds": self.pairs_total >= W3P_FLOOR_V_PAIRS,
            "C_3_of_8_holds": w3p_supported >= W3P_FLOOR_C_NEEDS,
        }
        return {
            "rows_scanned": sum(
                s.rows for s in self.sites.values() if s.status == "read"
            ),
            "sites": {name: self.sites[name].payload() for name in self.sites},
            "pairs_total": self.pairs_total,
            "pair_vocabulary_terms": len(self.pair_vocab),
            "needs": need_rows,
            "floors": floors,
            "w3p_floor_continuity": continuity,
            "g0_verdict": "PASS" if (v_holds and c_holds) else "PARK-AT-CENSUS",
        }


def register_entries() -> list[dict[str, object]]:
    register = json.loads(REGISTER.read_text())
    by_name = {e.get("name"): dict(e) for e in register["corpora"]}
    missing = [name for name in SITE_NAMES if name not in by_name]
    if missing:
        raise SystemExit(f"admitted archives missing from the register: {missing}")
    entries = [by_name[name] for name in SITE_NAMES]
    entries.sort(key=lambda e: Path(str(e["local_path"])).name)
    return entries


def iter_member_lines(archive: Path, member: str) -> Iterator[bytes]:
    proc = subprocess.Popen(
        ["bsdtar", "-xOf", str(archive), member],
        stdout=subprocess.PIPE,
        bufsize=1 << 22,
    )
    assert proc.stdout is not None
    try:
        yield from proc.stdout
    finally:
        proc.stdout.close()
        returncode = proc.wait()
        if returncode != 0:
            raise RuntimeError(f"bsdtar exited {returncode} on {archive}:{member}")


def main() -> int:
    entries = register_entries()
    date = time.strftime("%Y-%m-%d")
    derived_dir = REPO / "bench" / "w" / "corpus" / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    pairs_path = derived_dir / f"w3p2-pairs-{date}.tsv"
    census_path = REPO / "bench" / "w" / "results" / f"w3p2-census-{date}.json"

    census = W3P2Census()
    archives_block: list[dict[str, object]] = []
    started_all = time.time()
    with pairs_path.open("wb") as pairs_out:
        for entry in entries:
            name = str(entry["name"])
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
            sha_seconds = round(time.time() - started, 1)
            members = archive_members(archive)
            archives_block.append(
                {
                    "site": name,
                    "archive": str(entry["local_path"]),
                    "sha256_pinned": pinned_sha,
                    "sha256_reverified": actual_sha,
                    "sha_seconds": sha_seconds,
                    "members": members,
                }
            )
            if any(m not in members for m in REQUIRED_MEMBERS):
                print(f"{archive.name}: missing member — recorded", file=sys.stderr)
                census.add_missing(name)
                continue
            started = time.time()
            site = census.add_site(
                name,
                iter_member_lines(archive, "PostLinks.xml"),
                iter_member_lines(archive, "Posts.xml"),
                pairs_out,
            )
            print(
                f"{archive.name}: {site.pairs} pairs from {site.deduped_edges}"
                f" edges in {round(time.time() - started, 1)}s",
                file=sys.stderr,
            )
    pass_seconds = round(time.time() - started_all, 1)
    pairs_sha = sha256_of(pairs_path)

    artifact: dict[str, object] = {
        "provenance": provenance(),
        "declaration": "bench/w/W3P2_DECLARATION.md",
        "archives": archives_block,
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
    headline = (
        "pairs_total",
        "needs",
        "floors",
        "w3p_floor_continuity",
        "g0_verdict",
    )
    print(json.dumps({k: payload[k] for k in headline}, indent=1))
    print(f"\ncensus artifact: {census_path.relative_to(REPO)}")
    print(f"pair file: {pairs_path} (sha256 {pairs_sha[:16]}...)")
    print(f"G0 verdict: {payload['g0_verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
