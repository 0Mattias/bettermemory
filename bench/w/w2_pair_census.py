"""The W2 pair-class census: does the labeled-pair corpus carry the prize?

The W2 pair-census declaration fixes everything this script
computes and was committed before it. The counting rule is W3-P2's
exclusive substitution, imported verbatim (`w3p_pairs.need_supported`);
the probe sets are imported, not copied — the committed six from the
geometry probe, the expanded family from the W1c census's derivation,
and W3-P2's frozen NEEDS for the continuity row.

Two token spaces, each matching its own declaration: the probe rows
run in ENGINE STEM space (W2 declaration §2 — the space every table
and probe in this program matches in), the continuity row in W3-P2's
SURFACE space (its §4, unchanged, so the two censuses read side by
side without renegotiation).

Run: .venv/bin/python bench/w/w2_pair_census.py \\
        --out results/w2-pair-census-<date>.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
for _p in (str(_REPO / "src"), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bettermemory  # noqa: E402
from bettermemory.search import _stem_token  # noqa: E402

from w1b_geometry_probe import CROSS_FORM  # noqa: E402
from w1c_geometry_census import expanded_cross_form  # noqa: E402
from w3p_anatomy import NEEDS  # noqa: E402
from w3p_pairs import content_tokens, need_supported  # noqa: E402

PAIRS_TSV = _HERE / "corpus" / "derived" / "w3p2-pairs-2026-08-17.tsv"
TECH_SITES = frozenset(
    {
        "superuser-archive",
        "apple-stackexchange-archive",
        "android-stackexchange-archive",
    }
)
SUPPORT_MIN = 5
LICENSE_MIN_OF_SIX = 4
TWITCH_MIN_OF_SIX = 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, metavar="PATH")
    args = parser.parse_args()

    raw = PAIRS_TSV.read_bytes()
    rows: list[
        tuple[str, frozenset[str], frozenset[str], frozenset[str], frozenset[str]]
    ] = []
    site_counts: dict[str, int] = {}
    for line in raw.decode("utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        site, left, right = parts
        lt = content_tokens(left)
        rt = content_tokens(right)
        rows.append(
            (
                site,
                frozenset(lt),
                frozenset(rt),
                frozenset(_stem_token(t) for t in lt),
                frozenset(_stem_token(t) for t in rt),
            )
        )
        site_counts[site] = site_counts.get(site, 0) + 1

    def probe_counts(pair: tuple[str, str]) -> dict[str, object]:
        sa = frozenset({_stem_token(pair[0])})
        sb = frozenset({_stem_token(pair[1])})
        conflated = sa == sb
        excl_all = excl_tech = same_all = 0
        if not conflated:
            for site, _lt, _rt, ls, rs in rows:
                if need_supported(ls, rs, sa, sb):
                    excl_all += 1
                    if site in TECH_SITES:
                        excl_tech += 1
                a = next(iter(sa))
                b = next(iter(sb))
                if (a in ls and b in ls) or (a in rs and b in rs):
                    same_all += 1
        return {
            "left": pair[0],
            "right": pair[1],
            "stem_conflated": conflated,
            "exclusive_all": excl_all,
            "exclusive_tech": excl_tech,
            "same_side_all": same_all,
            "supported": (not conflated) and excl_all >= SUPPORT_MIN,
        }

    six = [probe_counts(p) for p in CROSS_FORM]
    expanded = [probe_counts(p) for p in expanded_cross_form()]

    continuity = {}
    for need_id, (left_set, right_set, register, note) in sorted(NEEDS.items()):
        n = sum(
            1
            for _s, lt, rt, _ls, _rs in rows
            if need_supported(lt, rt, left_set, right_set)
        )
        continuity[need_id] = {"register": register, "count": n, "note": note}

    six_supported = sum(1 for r in six if r["supported"])
    if six_supported >= LICENSE_MIN_OF_SIX:
        outcome = "license"
    elif six_supported >= TWITCH_MIN_OF_SIX:
        outcome = "twitch"
    else:
        outcome = "park"

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_HERE,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    payload = {
        "provenance": {
            "generated": date.today().isoformat(),
            "git_commit": commit,
            "bettermemory_version": bettermemory.__version__,
            "declaration": "bench/w/W2_PAIR_CENSUS_DECLARATION.md",
            "pairs_tsv": str(PAIRS_TSV.relative_to(_REPO)),
            "pairs_sha256": hashlib.sha256(raw).hexdigest(),
        },
        "pairs_total": len(rows),
        "pairs_by_site": dict(sorted(site_counts.items())),
        "tech_register_sites": sorted(TECH_SITES),
        "tech_register_pairs": sum(site_counts.get(s, 0) for s in TECH_SITES),
        "support_min": SUPPORT_MIN,
        "committed_six": six,
        "expanded_family": expanded,
        "expanded_supported": sum(1 for r in expanded if r["supported"]),
        "expanded_total": len(expanded),
        "needs_continuity": continuity,
        "readiness": {
            "outcome": outcome,
            "six_supported": six_supported,
            "license_min_of_six": LICENSE_MIN_OF_SIX,
            "twitch_min_of_six": TWITCH_MIN_OF_SIX,
        },
    }
    text = json.dumps(payload, indent=1, sort_keys=True)
    if args.out:
        out = Path(args.out).expanduser()
        if not out.is_absolute():
            out = (_HERE / out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(text)
    for r in six:
        print(
            f"  {r['left']}/{r['right']}: excl={r['exclusive_all']} "
            f"(tech {r['exclusive_tech']}), same-side={r['same_side_all']}, "
            f"supported={r['supported']}",
            file=sys.stderr,
        )
    print(
        f"six supported: {six_supported}/6 -> {outcome}; expanded "
        f"{payload['expanded_supported']}/{payload['expanded_total']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
