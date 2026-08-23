"""F1 stage-G gold prep: duplicate-graph components, splits, suppression.

The F1 declaration (private register; memory-resident summary) pins the
stage-G frame — Stack Exchange duplicate-graph positives with
false-negative-suppressed hard negatives — and defers mechanics to the
G addendum. This module derives the graph layer that addendum will pin:

- one PostLinks pass per pinned archive collects duplicate edges
  (LinkTypeId=3, document order, exact repeats dropped — the committed
  edge rule) and related edges (LinkTypeId=1, canonical undirected
  form, first occurrence kept), separately;
- duplicate edges union into components; every post inside a component
  is a transitive duplicate of every other, so negatives may never be
  drawn within a component;
- each component lands in train/dev/test by a keyed hash of its
  canonical member (the numerically smallest post id, keyed with the
  site) — no RNG, stable under re-runs, insensitive to edge order;
- related edges are the second suppression tier: linked-not-duplicate
  posts are plausible near-misses, excluded from the negative pool and
  never promoted to positives.

Artifacts: a results JSON (edge and component accounting, component
size profile, split tallies, file receipts) plus two derived TSVs under
corpus/derived/ — the component/split map (site, post id, component
representative, split) and the related-edge suppression list (site,
smaller id, larger id). Gold pair SURFACES are not re-derived here: the
Stack Overflow duplicate bodies and the per-site duplicate title pairs
already exist as census-era derived files; the G addendum names them by
sha.

Run: fastvenv/bin/python bench/w/f1_gold.py
Artifact: f1-gold-<date>.json in bench/w/results/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Iterable

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from w3p2_pairs import SITE_NAMES, iter_member_lines  # noqa: E402
from w3p_pairs import (  # noqa: E402
    REGISTER,
    archive_members,
    provenance,
    sha256_of,
)

SO_POSTLINKS_PIN = "stackoverflow-postlinks-archive"
LINKS_MEMBER = "PostLinks.xml"

_LINK_ROW_B = b"<row "
_LINK_TYPE_RE = re.compile(rb'\bLinkTypeId="(\d+)"')
_POST_ID_RE = re.compile(rb'\bPostId="(\d+)"')
_RELATED_ID_RE = re.compile(rb'\bRelatedPostId="(\d+)"')

# The split rule, fixed here: a keyed hash of the component's canonical
# member buckets the whole component. Fractions are declared, not tuned.
SPLIT_BUCKETS = 100
SPLIT_TRAIN_BELOW = 80
SPLIT_DEV_BELOW = 90

_SIZE_BOUNDS = (2, 3, 4, 5, 9, 17, 33)


def split_of(site: str, canonical_id: int) -> str:
    digest = hashlib.sha256(f"{site}\t{canonical_id}".encode()).hexdigest()
    bucket = int(digest[:8], 16) % SPLIT_BUCKETS
    if bucket < SPLIT_TRAIN_BELOW:
        return "train"
    if bucket < SPLIT_DEV_BELOW:
        return "dev"
    return "test"


def _size_bucket(size: int) -> str:
    lower = _SIZE_BOUNDS[0]
    for bound in _SIZE_BOUNDS:
        if size < bound:
            break
        lower = bound
    return str(lower)


@dataclass
class LinkStats:
    """One site's PostLinks pass: both edge families, deduped."""

    link_rows: int = 0
    duplicate_rows: int = 0
    related_rows: int = 0
    duplicate_edges: list[tuple[int, int]] = field(default_factory=list)
    related_edges: list[tuple[int, int]] = field(default_factory=list)


def link_edges(lines: Iterable[bytes]) -> LinkStats:
    """LinkTypeId=3 edges in document order (exact repeats dropped) and
    LinkTypeId=1 edges in canonical undirected form (first kept)."""
    stats = LinkStats()
    seen_dup: set[tuple[int, int]] = set()
    seen_rel: set[tuple[int, int]] = set()
    for raw in lines:
        if _LINK_ROW_B not in raw:
            continue
        stats.link_rows += 1
        type_m = _LINK_TYPE_RE.search(raw)
        if type_m is None or type_m.group(1) not in (b"1", b"3"):
            continue
        post_m = _POST_ID_RE.search(raw)
        related_m = _RELATED_ID_RE.search(raw)
        if post_m is None or related_m is None:
            continue
        post_id = int(post_m.group(1))
        related_id = int(related_m.group(1))
        if type_m.group(1) == b"3":
            stats.duplicate_rows += 1
            edge = (post_id, related_id)
            if edge not in seen_dup:
                seen_dup.add(edge)
                stats.duplicate_edges.append(edge)
        else:
            stats.related_rows += 1
            edge = (min(post_id, related_id), max(post_id, related_id))
            if edge not in seen_rel:
                seen_rel.add(edge)
                stats.related_edges.append(edge)
    return stats


class DuplicateGraph:
    """Union-find over one site's duplicate edges; canonical member is
    the numerically smallest post id in each component."""

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def _find(self, node: int) -> int:
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != root:
            self.parent[node], node = root, self.parent[node]
        return root

    def add_edge(self, a: int, b: int) -> None:
        for node in (a, b):
            if node not in self.parent:
                self.parent[node] = node
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            keep, drop = (ra, rb) if ra < rb else (rb, ra)
            self.parent[drop] = keep

    def components(self) -> dict[int, list[int]]:
        """Canonical member -> sorted members, for every component."""
        out: dict[int, list[int]] = {}
        for node in self.parent:
            out.setdefault(self._find(node), []).append(node)
        return {root: sorted(members) for root, members in out.items()}


@dataclass
class SiteGold:
    """One site's rows in the artifact."""

    status: str = "read"
    stats: LinkStats = field(default_factory=LinkStats)
    components: int = 0
    posts_in_graph: int = 0
    size_hist: dict[str, int] = field(default_factory=dict)
    split_components: dict[str, int] = field(default_factory=dict)
    split_posts: dict[str, int] = field(default_factory=dict)
    split_edges: dict[str, int] = field(default_factory=dict)
    _split_members: dict[str, list[list[int]]] = field(default_factory=dict)

    def payload(self) -> dict[str, object]:
        if self.status != "read":
            return {"status": self.status}
        pairs_possible = {
            split: sum(
                len(members) * (len(members) - 1) // 2
                for members in self._split_members.get(split, [])
            )
            for split in ("train", "dev", "test")
        }
        return {
            "status": self.status,
            "link_rows": self.stats.link_rows,
            "duplicate_edge_rows": self.stats.duplicate_rows,
            "duplicate_edges_deduped": len(self.stats.duplicate_edges),
            "related_edge_rows": self.stats.related_rows,
            "related_edges_deduped": len(self.stats.related_edges),
            "components": self.components,
            "posts_in_graph": self.posts_in_graph,
            "component_size_hist": dict(
                sorted(self.size_hist.items(), key=lambda i: int(i[0]))
            ),
            "split_components": dict(self.split_components),
            "split_posts": dict(self.split_posts),
            "split_duplicate_edges": dict(self.split_edges),
            "split_pairs_possible": pairs_possible,
        }


def read_site_gold(
    site: str,
    link_lines: Iterable[bytes],
    components_out: IO[bytes] | None = None,
    related_out: IO[bytes] | None = None,
) -> SiteGold:
    """The declared per-site gold pass: edges, components, splits."""
    gold = SiteGold()
    gold.stats = link_edges(link_lines)

    graph = DuplicateGraph()
    for a, b in gold.stats.duplicate_edges:
        graph.add_edge(a, b)
    components = graph.components()
    gold.components = len(components)
    gold.posts_in_graph = sum(len(m) for m in components.values())

    split_by_root: dict[int, str] = {}
    gold._split_members = {"train": [], "dev": [], "test": []}
    for root in sorted(components):
        members = components[root]
        split = split_of(site, root)
        split_by_root[root] = split
        gold._split_members[split].append(members)
        gold.split_components[split] = gold.split_components.get(split, 0) + 1
        gold.split_posts[split] = gold.split_posts.get(split, 0) + len(members)
        key = _size_bucket(len(members))
        gold.size_hist[key] = gold.size_hist.get(key, 0) + 1
        if components_out is not None:
            for member in members:
                components_out.write(f"{site}\t{member}\t{root}\t{split}\n".encode())
    for a, b in gold.stats.duplicate_edges:
        split = split_by_root[graph._find(a)]
        gold.split_edges[split] = gold.split_edges.get(split, 0) + 1
    if related_out is not None:
        for a, b in gold.stats.related_edges:
            related_out.write(f"{site}\t{a}\t{b}\n".encode())
    return gold


def _register_by_name() -> dict[str, dict[str, Any]]:
    register = json.loads(REGISTER.read_text())
    return {str(e.get("name")): dict(e) for e in register["corpora"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, metavar="PATH")
    args = parser.parse_args()
    date = time.strftime("%Y-%m-%d")

    by_name = _register_by_name()
    names = [SO_POSTLINKS_PIN, *SITE_NAMES]
    missing = [n for n in names if n not in by_name]
    if missing:
        raise SystemExit(f"admitted archives missing from the register: {missing}")
    entries = [by_name[n] for n in names]
    entries.sort(key=lambda e: Path(str(e["local_path"])).name)

    derived_dir = REPO / "bench" / "w" / "corpus" / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    components_path = derived_dir / f"f1-gold-components-{date}.tsv"
    related_path = derived_dir / f"f1-gold-related-{date}.tsv"

    started_all = time.time()
    archives_block: list[dict[str, Any]] = []
    sites: dict[str, dict[str, object]] = {}
    with (
        components_path.open("wb") as components_out,
        related_path.open("wb") as related_out,
    ):
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
            members = archive_members(archive)
            archives_block.append(
                {
                    "site": name,
                    "archive": str(entry["local_path"]),
                    "sha256_pinned": pinned_sha,
                    "sha256_reverified": actual_sha,
                    "sha_seconds": round(time.time() - started, 1),
                    "members": members,
                }
            )
            if LINKS_MEMBER not in members:
                print(f"{archive.name}: missing {LINKS_MEMBER} — recorded")
                sites[name] = SiteGold(status="missing-member").payload()
                continue
            started = time.time()
            gold = read_site_gold(
                name,
                iter_member_lines(archive, LINKS_MEMBER),
                components_out,
                related_out,
            )
            sites[name] = gold.payload()
            print(
                f"{archive.name}: {len(gold.stats.duplicate_edges)} dup edges,"
                f" {gold.components} components,"
                f" {len(gold.stats.related_edges)} related edges in"
                f" {round(time.time() - started, 1)}s",
                file=sys.stderr,
            )

    payload: dict[str, object] = {
        "provenance": provenance(),
        "reader": "bench/w/f1_gold.py",
        "split_rule": {
            "buckets": SPLIT_BUCKETS,
            "train_below": SPLIT_TRAIN_BELOW,
            "dev_below": SPLIT_DEV_BELOW,
            "key": "sha256(site TAB canonical-id) first 8 hex digits",
        },
        "archives": archives_block,
        "sites": sites,
        "derived_files": {
            "components": {
                "path": str(components_path.relative_to(REPO)),
                "sha256": sha256_of(components_path),
                "bytes": components_path.stat().st_size,
            },
            "related": {
                "path": str(related_path.relative_to(REPO)),
                "sha256": sha256_of(related_path),
                "bytes": related_path.stat().st_size,
            },
        },
        "pass_seconds": round(time.time() - started_all, 1),
    }
    out_path = (
        Path(args.out).expanduser()
        if args.out
        else REPO / "bench" / "w" / "results" / f"f1-gold-{date}.json"
    )
    out_path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    shown = out_path.relative_to(REPO) if out_path.is_relative_to(REPO) else out_path
    print(f"\nartifact: {shown}")
    return 0


__all__ = [
    "DuplicateGraph",
    "LinkStats",
    "SiteGold",
    "link_edges",
    "read_site_gold",
    "split_of",
]

if __name__ == "__main__":
    raise SystemExit(main())
