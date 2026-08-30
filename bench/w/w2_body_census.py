"""The W2 body census: the second surface of the pair corpus, priced.

The W2 body-census declaration fixes everything this script
computes and was committed before it. The edge rule and the streaming
are W3-P2's committed code path, imported (`w3p2_pairs.duplicate_edges`,
`iter_member_lines`); the counting rule is W3-P2's exclusive
substitution, imported verbatim (`w3p_pairs.need_supported`); the probe
sets are imported, not copied — the committed six from the geometry
probe, the expanded family from the W1c census's derivation, W3-P2's
frozen NEEDS for the continuity row. The ONE new rule — the
body-cleaning fixpoint of declaration §2 — lives here.

The probe-set imports live in `committed_probe_pairs`, resolved at run
time rather than at module import: the geometry modules import numpy,
which is bench-side only (`uv run --with numpy`, the W1 determinism
tests' arrangement), and the CI leg
(`tests/test_w2_body_determinism.py`) drives the census core with
hand-built probe rows and no numpy in the environment.

Two surfaces per body, both computed in the same pass: PROSE (quotes,
pre and code spans removed to a fixpoint — the asker's own phrasing,
the surface the ladder reads on) and MARKUP-TEXT (tags stripped only —
the informational variant that prices what the prose rule deletes).
Probe rows run in ENGINE STEM space; the continuity row runs in
W3-P2's surface space, unchanged.

Run: fastvenv/bin/python bench/w/w2_body_census.py
     fastvenv/bin/python bench/w/w2_body_census.py --from-derived \\
         corpus/derived/w2-bodies-<date>.tsv   # census stage alone
Artifact: w2-body-census-<date>.json in bench/w/results/.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Iterable, Sequence

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bettermemory  # noqa: E402
from bettermemory.search import _stem_token  # noqa: E402

from w3p_anatomy import NEEDS  # noqa: E402
from w3p_pairs import (  # noqa: E402
    _clean_line,
    archive_members,
    content_tokens,
    need_supported,
    provenance,
    row_post_type,
    sha256_of,
)
from w3p2_pairs import (  # noqa: E402
    REQUIRED_MEMBERS,
    _row_id,
    duplicate_edges,
    iter_member_lines,
    register_entries,
)

# Declaration §4: the ladder's constants, the family's, unchanged.
SUPPORT_MIN = 5
LICENSE_MIN_OF_SIX = 4
TWITCH_MIN_OF_SIX = 2
TECH_SITES = frozenset(
    {
        "superuser-archive",
        "apple-stackexchange-archive",
        "android-stackexchange-archive",
    }
)

_BODY_RE = re.compile(rb'\bBody="([^"]*)"')
# Declaration §2 step 2: quoted material and code fall, to a fixpoint.
_SPAN_RE = re.compile(r"<(blockquote|pre|code)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def committed_probe_pairs() -> tuple[
    tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]
]:
    """The declared probe sets, imported at run time (declaration §3).

    Function-scoped on purpose: the geometry modules import numpy,
    which the CI environment does not carry — the leg that runs there
    builds its probe rows by hand instead.
    """
    from w1b_geometry_probe import CROSS_FORM
    from w1c_geometry_census import expanded_cross_form

    return tuple(CROSS_FORM), expanded_cross_form()


def _flatten(markup: str) -> str:
    """Declaration §2 step 3: tags to spaces, entities decoded once
    more, whitespace collapsed."""
    return _clean_line(html.unescape(_TAG_RE.sub(" ", markup)))


def body_surfaces(raw_attr: bytes) -> tuple[str, str]:
    """Declaration §2: (prose, markup-text) from one Body attribute."""
    markup = html.unescape(raw_attr.decode("utf-8", errors="replace"))
    prose_markup = markup
    while True:
        stripped = _SPAN_RE.sub(" ", prose_markup)
        if stripped == prose_markup:
            break
        prose_markup = stripped
    return _flatten(prose_markup), _flatten(markup)


@dataclass
class ProbeRow:
    """One probe pair's running counts across every kept pair."""

    left: str
    right: str
    stem_left: str
    stem_right: str
    exclusive_all: int = 0
    exclusive_tech: int = 0
    exclusive_markup_all: int = 0
    exclusive_markup_tech: int = 0
    same_side_all: int = 0

    @property
    def conflated(self) -> bool:
        return self.stem_left == self.stem_right

    @property
    def supported(self) -> bool:
        return (not self.conflated) and self.exclusive_all >= SUPPORT_MIN

    def payload(self) -> dict[str, object]:
        return {
            "left": self.left,
            "right": self.right,
            "stem_conflated": self.conflated,
            "exclusive_all": self.exclusive_all,
            "exclusive_tech": self.exclusive_tech,
            "exclusive_markup_all": self.exclusive_markup_all,
            "exclusive_markup_tech": self.exclusive_markup_tech,
            "same_side_all": self.same_side_all,
            "supported": self.supported,
        }


@dataclass
class SiteBodyCensus:
    """One site's rows in the artifact (declaration §1-§2)."""

    status: str = "read"
    rows: int = 0
    row_counts: dict[str, int] = field(default_factory=dict)
    link_rows: int = 0
    duplicate_rows: int = 0
    deduped_edges: int = 0
    unresolved_edges: int = 0
    dropped_by_rule: int = 0
    pairs: int = 0

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
        }


class ProbeCensus:
    """The census stage: probe counts streamed pair by pair.

    Fed either by the extraction pass or by a re-read of the derived
    file (`--from-derived`); the counts block is identical either way,
    which is the declared determinism check.
    """

    def __init__(
        self,
        six: Sequence[tuple[str, str]],
        expanded: Sequence[tuple[str, str]],
    ) -> None:
        self.six = [ProbeRow(a, b, _stem_token(a), _stem_token(b)) for a, b in six]
        self.expanded = [
            ProbeRow(a, b, _stem_token(a), _stem_token(b)) for a, b in expanded
        ]
        stems = {r.stem_left for r in self.six + self.expanded}
        stems |= {r.stem_right for r in self.six + self.expanded}
        self.presence: dict[str, int] = {stem: 0 for stem in stems}
        self.need_counts: dict[str, int] = {}
        self.pairs_total = 0
        self.tech_pairs = 0

    def add_pair(
        self, site: str, prose_l: str, prose_r: str, full_l: str, full_r: str
    ) -> None:
        pt_l = frozenset(content_tokens(prose_l))
        pt_r = frozenset(content_tokens(prose_r))
        ps_l = frozenset(_stem_token(t) for t in pt_l)
        ps_r = frozenset(_stem_token(t) for t in pt_r)
        fs_l = frozenset(_stem_token(t) for t in content_tokens(full_l))
        fs_r = frozenset(_stem_token(t) for t in content_tokens(full_r))
        tech = site in TECH_SITES
        self.pairs_total += 1
        if tech:
            self.tech_pairs += 1
        for stem in self.presence:
            self.presence[stem] += (stem in ps_l) + (stem in ps_r)
        for row in self.six + self.expanded:
            if row.conflated:
                continue
            sa = frozenset({row.stem_left})
            sb = frozenset({row.stem_right})
            if need_supported(ps_l, ps_r, sa, sb):
                row.exclusive_all += 1
                if tech:
                    row.exclusive_tech += 1
            if need_supported(fs_l, fs_r, sa, sb):
                row.exclusive_markup_all += 1
                if tech:
                    row.exclusive_markup_tech += 1
            a, b = row.stem_left, row.stem_right
            if (a in ps_l and b in ps_l) or (a in ps_r and b in ps_r):
                row.same_side_all += 1
        for qid, (left_set, right_set, _register, _note) in NEEDS.items():
            if need_supported(pt_l, pt_r, left_set, right_set):
                self.need_counts[qid] = self.need_counts.get(qid, 0) + 1

    def counts_payload(self) -> dict[str, Any]:
        six_supported = sum(1 for r in self.six if r.supported)
        if six_supported >= LICENSE_MIN_OF_SIX:
            outcome = "license"
        elif six_supported >= TWITCH_MIN_OF_SIX:
            outcome = "twitch"
        else:
            outcome = "park"
        continuity = {
            qid: {"register": NEEDS[qid][2], "count": self.need_counts.get(qid, 0)}
            for qid in sorted(NEEDS)
        }
        return {
            "pairs_total": self.pairs_total,
            "tech_register_pairs": self.tech_pairs,
            "support_min": SUPPORT_MIN,
            "committed_six": [r.payload() for r in self.six],
            "expanded_family": [r.payload() for r in self.expanded],
            "expanded_supported": sum(1 for r in self.expanded if r.supported),
            "expanded_total": len(self.expanded),
            "presence_bodies_prose": dict(sorted(self.presence.items())),
            "needs_continuity": continuity,
            "readiness": {
                "outcome": outcome,
                "six_supported": six_supported,
                "license_min_of_six": LICENSE_MIN_OF_SIX,
                "twitch_min_of_six": TWITCH_MIN_OF_SIX,
            },
        }


@dataclass
class W2BodyCensus:
    """The extraction aggregate. Deterministic in the streams;
    wall-clock lives outside, in the artifact only."""

    probes: ProbeCensus
    sites: dict[str, SiteBodyCensus] = field(default_factory=dict)

    def add_missing(self, site: str) -> SiteBodyCensus:
        census = SiteBodyCensus(status="missing-member")
        self.sites[site] = census
        return census

    def add_site(
        self,
        site: str,
        link_lines: Iterable[bytes],
        post_lines: Iterable[bytes],
        bodies_out: IO[bytes] | None = None,
    ) -> SiteBodyCensus:
        """The declared per-site read: edge pass, body pass, pair rule."""
        census = SiteBodyCensus()
        self.sites[site] = census

        edge_stats = duplicate_edges(link_lines)
        census.link_rows = edge_stats.link_rows
        census.duplicate_rows = edge_stats.duplicate_rows
        census.deduped_edges = edge_stats.deduped
        wanted: set[bytes] = set()
        for post_id, related_id in edge_stats.edges:
            wanted.add(post_id)
            wanted.add(related_id)

        bodies: dict[bytes, bytes] = {}
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
            body_m = _BODY_RE.search(raw)
            if body_m is not None:
                bodies[row_id] = body_m.group(1)

        for post_id, related_id in edge_stats.edges:
            left_raw = bodies.get(post_id)
            right_raw = bodies.get(related_id)
            if left_raw is None or right_raw is None:
                census.unresolved_edges += 1
                continue
            prose_l, full_l = body_surfaces(left_raw)
            prose_r, full_r = body_surfaces(right_raw)
            if not prose_l or not prose_r or prose_l.lower() == prose_r.lower():
                census.dropped_by_rule += 1
                continue
            if len(content_tokens(prose_l)) < 2 or len(content_tokens(prose_r)) < 2:
                census.dropped_by_rule += 1
                continue
            census.pairs += 1
            if bodies_out is not None:
                line = f"{site}\t{prose_l}\t{prose_r}\t{full_l}\t{full_r}\n"
                bodies_out.write(line.encode())
            self.probes.add_pair(site, prose_l, prose_r, full_l, full_r)
        return census

    def sites_payload(self) -> dict[str, object]:
        return {name: self.sites[name].payload() for name in self.sites}


def census_from_derived(path: Path, probes: ProbeCensus) -> ProbeCensus:
    """The census stage alone, re-read from the derived file — the
    declared determinism repeat."""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 5:
                continue
            probes.add_pair(parts[0], parts[1], parts[2], parts[3], parts[4])
    return probes


def _print_headline(counts: dict[str, Any]) -> None:
    for row in counts["committed_six"]:
        print(
            f"  {row['left']}/{row['right']}: excl={row['exclusive_all']}"
            f" (tech {row['exclusive_tech']},"
            f" markup {row['exclusive_markup_all']}),"
            f" same-side={row['same_side_all']},"
            f" supported={row['supported']}",
            file=sys.stderr,
        )
    readiness = counts["readiness"]
    print(
        f"six supported: {readiness['six_supported']}/6 ->"
        f" {readiness['outcome']}; expanded"
        f" {counts['expanded_supported']}/{counts['expanded_total']}",
        file=sys.stderr,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-derived", default=None, metavar="TSV")
    parser.add_argument("--out", default=None, metavar="PATH")
    args = parser.parse_args()
    date = time.strftime("%Y-%m-%d")

    if args.from_derived:
        derived = Path(args.from_derived).expanduser()
        if not derived.is_absolute():
            derived = (Path(__file__).resolve().parent / derived).resolve()
        probes = census_from_derived(derived, ProbeCensus(*committed_probe_pairs()))
        counts = probes.counts_payload()
        payload: dict[str, object] = {
            "provenance": provenance(),
            "declaration": "bench/w/W2_BODY_CENSUS_DECLARATION.md",
            "stage": "census-only",
            "derived_file": {
                "path": str(derived),
                "sha256": sha256_of(derived),
            },
            "census": counts,
        }
        text = json.dumps(payload, indent=1, sort_keys=True)
        if args.out:
            out = Path(args.out).expanduser()
            out.write_text(text + "\n", encoding="utf-8")
            print(f"wrote {out}", file=sys.stderr)
        else:
            print(text)
        _print_headline(counts)
        return 0

    entries = register_entries()
    derived_dir = REPO / "bench" / "w" / "corpus" / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    bodies_path = derived_dir / f"w2-bodies-{date}.tsv"
    out_path = (
        Path(args.out)
        if args.out
        else REPO / "bench" / "w" / "results" / f"w2-body-census-{date}.json"
    )

    census = W2BodyCensus(probes=ProbeCensus(*committed_probe_pairs()))
    archives_block: list[dict[str, object]] = []
    started_all = time.time()
    with bodies_path.open("wb") as bodies_out:
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
                bodies_out,
            )
            print(
                f"{archive.name}: {site.pairs} pairs from {site.deduped_edges}"
                f" edges in {round(time.time() - started, 1)}s",
                file=sys.stderr,
            )
    pass_seconds = round(time.time() - started_all, 1)
    bodies_sha = sha256_of(bodies_path)

    counts = census.probes.counts_payload()
    artifact: dict[str, object] = {
        "provenance": provenance(),
        "declaration": "bench/w/W2_BODY_CENSUS_DECLARATION.md",
        "bettermemory_version": bettermemory.__version__,
        "archives": archives_block,
        "derived_file": {
            "path": str(bodies_path.relative_to(REPO)),
            "sha256": bodies_sha,
            "bytes": bodies_path.stat().st_size,
        },
        "sites": census.sites_payload(),
        "census": counts,
        "pass_seconds": pass_seconds,
    }
    out_path.write_text(json.dumps(artifact, indent=1, sort_keys=True) + "\n")
    print(f"\ncensus artifact: {out_path.relative_to(REPO)}")
    print(f"derived file: {bodies_path} (sha256 {bodies_sha[:16]}...)")
    _print_headline(counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
