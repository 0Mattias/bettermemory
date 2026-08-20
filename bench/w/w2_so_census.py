"""The W2 SO census: the programming register's duplicate graph, priced.

`bench/w/W2_SO_CENSUS_DECLARATION.md` fixes everything this script
computes and was committed before it. Every rule that counts is
imported: the edge rule (`w3p2_pairs.duplicate_edges`), the body
surfaces and the census core (`w2_body_census.body_surfaces`,
`ProbeCensus`, `committed_probe_pairs`), exclusive substitution
(`w3p_pairs.need_supported`), the tokenizer and keep rule
(`w3p_pairs.content_tokens`). This module owns exactly what the
declaration says it owns: the two-archive input layout (the publisher
ships the site per table), the eager resolution arrangement of §4 —
CI-pinned equivalent to the imported reference — and the two
informational rows, direction and unique targets.

Stage 0 reads the W3-P legacy title-pair file already in hand, before
any fetch, under the site label the declaration fixes. Stage 2 reads
the two Stack Overflow pins. The census stage re-run from the derived
file must reproduce the counts block identically; that repeat is the
declared determinism check.

Run: fastvenv/bin/python bench/w/w2_so_census.py --stage0
     fastvenv/bin/python bench/w/w2_so_census.py
     fastvenv/bin/python bench/w/w2_so_census.py --from-derived \\
         corpus/derived/w2-so-bodies-<date>.tsv
Artifacts: w2-so-stage0-<date>.json / w2-so-census-<date>.json in
bench/w/results/.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import IO, Any, Iterable, Sequence

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bettermemory  # noqa: E402
from bettermemory.search import _stem_token  # noqa: E402

from w2_body_census import (  # noqa: E402
    _BODY_RE,
    ProbeCensus,
    SiteBodyCensus,
    W2BodyCensus,
    body_surfaces,
    committed_probe_pairs,
)
from w3p_pairs import (  # noqa: E402
    archive_members,
    content_tokens,
    need_supported,
    provenance,
    row_post_type,
    sha256_of,
)
from w3p2_pairs import _row_id, duplicate_edges, iter_member_lines  # noqa: E402

# Declaration §2: the stage 0 input and its recorded sha's location.
STAGE0_SITE = "stackoverflow-legacy-titles"
STAGE0_CENSUS_JSON = REPO / "bench" / "w" / "results" / "w3p-census-2026-08-17.json"

# Declaration §4: the two pins stage 2 reads, by register name.
POSTS_PIN = "stackoverflow-posts-archive"
POSTLINKS_PIN = "stackoverflow-postlinks-archive"
SO_SITE = "stackoverflow"
REGISTER = REPO / "bench" / "w" / "corpora.json"


class SOProbeCensus(ProbeCensus):
    """The imported census plus the two declared informational rows.

    Direction is which SIDE of a kept pair carries which stem of an
    exclusive crossing — the left column is the PostId (closed) side by
    construction of the derived file. Unique targets need the edge's
    RelatedPostId, so pairs enter through `add_pair_ids`; the imported
    counting runs unchanged underneath, and the directional split is
    asserted against it on every crossing (declaration §4).
    """

    def __init__(
        self,
        six: Sequence[tuple[str, str]],
        expanded: Sequence[tuple[str, str]],
    ) -> None:
        super().__init__(six, expanded)
        rows = self.six + self.expanded
        self.a_on_post_side = [0] * len(rows)
        self.a_on_related_side = [0] * len(rows)
        self.unique_targets: list[set[bytes]] = [set() for _ in rows]

    def add_pair_ids(
        self,
        site: str,
        prose_l: str,
        prose_r: str,
        full_l: str,
        full_r: str,
        related_id: bytes | None = None,
    ) -> None:
        super().add_pair(site, prose_l, prose_r, full_l, full_r)
        ps_l = frozenset(_stem_token(t) for t in content_tokens(prose_l))
        ps_r = frozenset(_stem_token(t) for t in content_tokens(prose_r))
        for index, row in enumerate(self.six + self.expanded):
            if row.conflated:
                continue
            sa, sb = row.stem_left, row.stem_right
            a_left = sa in ps_l and sa not in ps_r and sb in ps_r and sb not in ps_l
            a_right = sa in ps_r and sa not in ps_l and sb in ps_l and sb not in ps_r
            crossed = a_left or a_right
            if crossed != need_supported(ps_l, ps_r, frozenset({sa}), frozenset({sb})):
                raise AssertionError(
                    f"directional split disagrees with the imported rule on"
                    f" {row.left}/{row.right}"
                )
            if a_left:
                self.a_on_post_side[index] += 1
            if a_right:
                self.a_on_related_side[index] += 1
            if crossed and related_id is not None:
                self.unique_targets[index].add(related_id)

    def counts_payload(self) -> dict[str, Any]:
        payload = super().counts_payload()
        rows = self.six + self.expanded
        blocks = list(payload["committed_six"]) + list(payload["expanded_family"])
        for index, (row, block) in enumerate(zip(rows, blocks)):
            if block["left"] != row.left or block["right"] != row.right:
                raise AssertionError("payload rows out of declared probe order")
            split = self.a_on_post_side[index] + self.a_on_related_side[index]
            if not row.conflated and split != row.exclusive_all:
                raise AssertionError(
                    f"directional rows do not sum to the imported count on"
                    f" {row.left}/{row.right}"
                )
            block["a_on_post_side"] = self.a_on_post_side[index]
            block["a_on_related_side"] = self.a_on_related_side[index]
            block["unique_targets"] = len(self.unique_targets[index])
        return payload


def eager_site_read(
    site: str,
    link_lines: Iterable[bytes],
    post_lines: Iterable[bytes],
    probes: SOProbeCensus,
    bodies_out: IO[bytes] | None = None,
) -> SiteBodyCensus:
    """Declaration §4's arrangement: the reference read, resolved eagerly.

    Same rules, same counts — the CI leg pins this function against
    `W2BodyCensus.add_site` on the committed fixture. Bodies are cached
    only until every edge touching them has resolved; a pair is emitted
    the moment its second member arrives, in edge order among edges the
    same row resolves.
    """
    census = SiteBodyCensus()
    edge_stats = duplicate_edges(link_lines)
    census.link_rows = edge_stats.link_rows
    census.duplicate_rows = edge_stats.duplicate_rows
    census.deduped_edges = edge_stats.deduped
    edges = edge_stats.edges

    refcount: dict[bytes, int] = {}
    pending: dict[bytes, list[int]] = {}
    for index, (post_id, related_id) in enumerate(edges):
        refcount[post_id] = refcount.get(post_id, 0) + 1
        refcount[related_id] = refcount.get(related_id, 0) + 1
        pending.setdefault(post_id, []).append(index)
        if related_id != post_id:
            pending.setdefault(related_id, []).append(index)

    resolved = bytearray(len(edges))
    cache: dict[bytes, bytes] = {}

    def release(member: bytes) -> None:
        refcount[member] -= 1
        if refcount[member] == 0:
            cache.pop(member, None)

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
        if row_id is None or row_id not in refcount:
            continue
        body_m = _BODY_RE.search(raw)
        if body_m is None:
            continue
        cache[row_id] = body_m.group(1)
        for index in pending.get(row_id, ()):
            if resolved[index]:
                continue
            post_id, related_id = edges[index]
            left_raw = cache.get(post_id)
            right_raw = cache.get(related_id)
            if left_raw is None or right_raw is None:
                continue
            resolved[index] = 1
            prose_l, full_l = body_surfaces(left_raw)
            prose_r, full_r = body_surfaces(right_raw)
            if not prose_l or not prose_r or prose_l.lower() == prose_r.lower():
                census.dropped_by_rule += 1
            elif len(content_tokens(prose_l)) < 2 or len(content_tokens(prose_r)) < 2:
                census.dropped_by_rule += 1
            else:
                census.pairs += 1
                if bodies_out is not None:
                    line = (
                        f"{site}\t{post_id.decode('ascii')}"
                        f"\t{related_id.decode('ascii')}"
                        f"\t{prose_l}\t{prose_r}\t{full_l}\t{full_r}\n"
                    )
                    bodies_out.write(line.encode())
                probes.add_pair_ids(
                    site, prose_l, prose_r, full_l, full_r, related_id=related_id
                )
            release(post_id)
            release(related_id)
    census.unresolved_edges = len(edges) - int(sum(resolved))
    return census


def stage0_read(lines: Iterable[str], probes: SOProbeCensus) -> dict[str, int]:
    """Declaration §2: the legacy title pairs, fed as pinned.

    Two columns, closed question's own title then the canonical
    target's; the title surface is both surfaces (the markup variant is
    degenerate by declaration), and there is no RelatedPostId, so the
    unique-target row does not accumulate here.
    """
    read = kept = 0
    for line in lines:
        read += 1
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 2:
            continue
        kept += 1
        left, right = parts
        probes.add_pair_ids(STAGE0_SITE, left, right, left, right, related_id=None)
    return {"lines_read": read, "pairs_fed": kept}


def census_from_derived(path: Path, probes: SOProbeCensus) -> SOProbeCensus:
    """The census stage alone, re-read from the seven-column derived
    file — the declared determinism repeat, directional rows included."""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 7:
                continue
            probes.add_pair_ids(
                parts[0],
                parts[3],
                parts[4],
                parts[5],
                parts[6],
                related_id=parts[2].encode("ascii"),
            )
    return probes


def _register_entry(name: str) -> dict[str, Any]:
    register = json.loads(REGISTER.read_text())
    for entry in register["corpora"]:
        if entry.get("name") == name:
            return dict(entry)
    raise SystemExit(f"{name} not found in {REGISTER}")


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


def _print_headline(counts: dict[str, Any]) -> None:
    for row in counts["committed_six"]:
        print(
            f"  {row['left']}/{row['right']}: excl={row['exclusive_all']}"
            f" (post-side {row['a_on_post_side']},"
            f" related-side {row['a_on_related_side']},"
            f" targets {row['unique_targets']}),"
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


def _write_artifact(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    shown = path.relative_to(REPO) if path.is_relative_to(REPO) else path
    print(f"\nartifact: {shown}")


def run_stage0(out: str | None, date: str) -> int:
    recorded = json.loads(STAGE0_CENSUS_JSON.read_text())
    pair_file = REPO / str(recorded["pair_file"]["path"])
    recorded_sha = str(recorded["pair_file"]["sha256"])
    print(f"{pair_file.name}: re-verifying recorded sha256 ...", file=sys.stderr)
    actual_sha = sha256_of(pair_file)
    if actual_sha != recorded_sha:
        raise SystemExit(
            f"{pair_file.name}: sha mismatch (recorded {recorded_sha}, actual"
            f" {actual_sha}); the record is the authority — nothing is read"
        )
    probes = SOProbeCensus(*committed_probe_pairs())
    with pair_file.open("r", encoding="utf-8") as handle:
        input_counts = stage0_read(handle, probes)
    counts = probes.counts_payload()
    payload: dict[str, object] = {
        "provenance": provenance(),
        "declaration": "bench/w/W2_SO_CENSUS_DECLARATION.md",
        "bettermemory_version": bettermemory.__version__,
        "stage": "stage0-legacy-titles",
        "input": {
            "path": str(pair_file.relative_to(REPO)),
            "sha256": actual_sha,
            "sha256_recorded_in": str(STAGE0_CENSUS_JSON.relative_to(REPO)),
            **input_counts,
        },
        "census": counts,
    }
    out_path = (
        Path(out)
        if out
        else REPO / "bench" / "w" / "results" / f"w2-so-stage0-{date}.json"
    )
    _write_artifact(out_path, payload)
    _print_headline(counts)
    return 0


def run_from_derived(derived_arg: str, out: str | None) -> int:
    derived = Path(derived_arg).expanduser()
    if not derived.is_absolute():
        derived = (Path(__file__).resolve().parent / derived).resolve()
    probes = census_from_derived(derived, SOProbeCensus(*committed_probe_pairs()))
    counts = probes.counts_payload()
    payload: dict[str, object] = {
        "provenance": provenance(),
        "declaration": "bench/w/W2_SO_CENSUS_DECLARATION.md",
        "stage": "census-only",
        "derived_file": {"path": str(derived), "sha256": sha256_of(derived)},
        "census": counts,
    }
    if out:
        _write_artifact(Path(out).expanduser(), payload)
    else:
        print(json.dumps(payload, indent=1, sort_keys=True))
    _print_headline(counts)
    return 0


def run_stage2(out: str | None, date: str) -> int:
    posts_entry = _register_entry(POSTS_PIN)
    links_entry = _register_entry(POSTLINKS_PIN)
    started_all = time.time()
    archives_block = [_reverify(links_entry), _reverify(posts_entry)]
    for block, member in zip(archives_block, ("PostLinks.xml", "Posts.xml")):
        if member not in block["members"]:
            raise SystemExit(
                f"{block['archive']}: expected member {member} missing — the"
                f" declaration's one-site rule stops the unit here"
            )

    derived_dir = REPO / "bench" / "w" / "corpus" / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    bodies_path = derived_dir / f"w2-so-bodies-{date}.tsv"
    probes = SOProbeCensus(*committed_probe_pairs())
    links_archive = REPO / str(links_entry["local_path"])
    posts_archive = REPO / str(posts_entry["local_path"])
    started = time.time()
    with bodies_path.open("wb") as bodies_out:
        site = eager_site_read(
            SO_SITE,
            iter_member_lines(links_archive, "PostLinks.xml"),
            iter_member_lines(posts_archive, "Posts.xml"),
            probes,
            bodies_out,
        )
    print(
        f"{SO_SITE}: {site.pairs} pairs from {site.deduped_edges} edges in"
        f" {round(time.time() - started, 1)}s",
        file=sys.stderr,
    )
    pass_seconds = round(time.time() - started_all, 1)

    counts = probes.counts_payload()
    payload: dict[str, object] = {
        "provenance": provenance(),
        "declaration": "bench/w/W2_SO_CENSUS_DECLARATION.md",
        "bettermemory_version": bettermemory.__version__,
        "archives": archives_block,
        "derived_file": {
            "path": str(bodies_path.relative_to(REPO)),
            "sha256": sha256_of(bodies_path),
            "bytes": bodies_path.stat().st_size,
        },
        "sites": {SO_SITE: site.payload()},
        "census": counts,
        "pass_seconds": pass_seconds,
    }
    out_path = (
        Path(out)
        if out
        else REPO / "bench" / "w" / "results" / f"w2-so-census-{date}.json"
    )
    _write_artifact(out_path, payload)
    print(f"derived file: {bodies_path}")
    _print_headline(counts)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage0", action="store_true")
    parser.add_argument("--from-derived", default=None, metavar="TSV")
    parser.add_argument("--out", default=None, metavar="PATH")
    args = parser.parse_args()
    date = time.strftime("%Y-%m-%d")
    if args.stage0 and args.from_derived:
        raise SystemExit("--stage0 and --from-derived are separate stages")
    if args.stage0:
        return run_stage0(args.out, date)
    if args.from_derived:
        return run_from_derived(args.from_derived, args.out)
    return run_stage2(args.out, date)


__all__ = [
    "SOProbeCensus",
    "W2BodyCensus",
    "body_surfaces",
    "census_from_derived",
    "eager_site_read",
    "stage0_read",
]

if __name__ == "__main__":
    raise SystemExit(main())
