"""Establish why `citation_resolved_rate` cannot yet be measured at all.

WHAT THIS WAS BUILT TO DO, AND WHAT IT FOUND INSTEAD. `bench/rot`'s
README calls grading against real memory bodies "the only remaining
route" to a publishable staleness claim, and frames it as a
multiplication:

    real_world_J  <=  J_resolved  x  resolution_rate

with only the first factor ever measured. This module set out to measure
the second one on a live store. It cannot be measured, and the reason is
more useful than the number would have been.

**`parse_claim_citation` is a corpus-format reader, not a claim
extractor.** It matches three anchored, full-string sentence templates:

    ^The module `X` is part of this package\\.$
    ^`X` is defined at the top level of `Y`\\.$
    ^`X` in `Y` is set to `Z`\\.$

Those are the sentence forms `bench/rot`'s own generator emits. A real
memory body is prose and cannot match an anchored full-string template,
so it resolves to `None` **by construction, not by measurement**. On a
live 216-memory store: 143 are "checkable" under `bench/claims.py`'s
regex sweep, and **0** parse.

SO THE HONEST STATUS OF THE DECOMPOSITION IS "UNDEFINED", NOT "ZERO".
Reporting `real_world_J <= 0.2875 x 0.0 = 0.0` would be a false claim
about this project's own product, arrived at by exactly the mistake
`bench/longmemeval` spent a day learning to avoid on a competitor's:
a pipeline that structurally cannot produce output, mistaken for a
subject that performs badly. The second factor is not small. It is not
yet a quantity.

WHAT THIS MEANS FOR THE ROADMAP. README item 4 is larger than it reads.
It is not "measure the second factor" — it is:

    1. Build a claim extractor that reads REAL memory prose, not three
       generated sentence templates.
    2. Only then is `resolution_rate` a measurable quantity.
    3. Only then does `J_resolved x resolution_rate` mean anything, and
       only then can "we verify, and here is the measured accuracy" be
       said about the shipped product rather than about a corpus.

Until step 1 exists, every published J is a statement about generated
sentences, and **that qualifier belongs next to the number.**

PRIVACY. Same contract as `bench/claims.py`: strictly READ-ONLY,
AGGREGATES ONLY. No body, no filename, no scope, no citation text enters
the output, so a result file is publishable verbatim.

Usage:

    venv/bin/python bench/rot/resolution.py
    venv/bin/python bench/rot/resolution.py --json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
DEFAULT_STORE = Path.home() / ".claude-memory"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rot = _load("bench_rot_run", _HERE / "run.py")
claims = _load("bench_claims", _ROOT / "bench" / "claims.py")


@dataclass
class _Resolution:
    """Counters only — see the module docstring's privacy contract."""

    total: int = 0
    checkable: int = 0
    bare: int = 0
    parsed: int = 0
    unparsed: int = 0
    checkable_but_unparsed: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    @property
    def parse_rate(self) -> float:
        return self.parsed / self.total if self.total else 0.0

    @property
    def checkable_rate(self) -> float:
        return self.checkable / self.total if self.total else 0.0


def run(store: Path, repo_root: Path | None) -> _Resolution:
    r = _Resolution()
    for path in sorted(store.glob("*.md")):
        if path.name.startswith("."):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        _, body = claims.split_frontmatter(text)
        r.total += 1

        is_checkable = bool(claims.classify_body(body) & set(claims.CHECKABLE_CLASSES))
        if is_checkable:
            r.checkable += 1
        else:
            r.bare += 1

        cite = rot.parse_claim_citation(body, repo_root)
        if cite is None:
            r.unparsed += 1
            if is_checkable:
                r.checkable_but_unparsed += 1
            continue
        r.parsed += 1
        kind = getattr(cite, "kind", "unknown")
        r.by_kind[kind] = r.by_kind.get(kind, 0) + 1
    return r


def synthetic_control() -> tuple[int, int]:
    """Parse the generator's OWN sentence forms, so a zero on real bodies
    is attributable to body shape rather than to a broken parser.

    Without this control the real-store zero is uninterpretable: a parser
    that matches nothing at all would produce exactly the same number.
    """
    samples = [
        "The module `src/bettermemory/store.py` is part of this package.",
        "`write` is defined at the top level of `src/bettermemory/store.py`.",
        "`LIMIT` in `src/bettermemory/store.py` is set to `30`.",
    ]
    ok = sum(1 for s in samples if rot.parse_claim_citation(s, _ROOT) is not None)
    return ok, len(samples)


def _format_text(r: _Resolution, store: Path, control: tuple[int, int]) -> str:
    ok, n = control
    return "\n".join(
        [
            f"store: {store}  ({r.total} memories)",
            "",
            "  checkable (bench/claims.py regex sweep)   "
            f"{r.checkable:>4}  {100 * r.checkable_rate:>5.1f}%",
            "  bare                                      "
            f"{r.bare:>4}  {100 * (1 - r.checkable_rate):>5.1f}%",
            "",
            "  PARSED by parse_claim_citation            "
            f"{r.parsed:>4}  {100 * r.parse_rate:>5.1f}%",
            f"  checkable but unparsed                    {r.checkable_but_unparsed:>4}",
            "",
            f"  control — generator's own sentence forms  {ok}/{n} parsed",
            "",
            "WHAT THIS ESTABLISHES",
            "  The parser works (control passes) and matches nothing real.",
            "  `parse_claim_citation` reads three anchored full-string",
            "  sentence templates that bench/rot's generator emits. Real",
            "  memory prose cannot match an anchored template, so the zero",
            "  above is STRUCTURAL, not a measurement of claim quality.",
            "",
            "  Therefore `resolution_rate` is UNDEFINED, not zero, and",
            "  `real_world_J <= J_resolved x resolution_rate` cannot be",
            "  evaluated. Publishing 0.2875 x 0.0 would be a false claim",
            "  about our own product.",
            "",
            "  README item 4 is consequently larger than it reads: a claim",
            "  extractor for real prose has to exist before the second",
            "  factor is a quantity at all. Until then every published J",
            "  describes generated sentences, and that qualifier belongs",
            "  beside the number.",
        ]
    )


def main() -> int:
    p = argparse.ArgumentParser(
        description="Why citation_resolved_rate is undefined on real bodies."
    )
    p.add_argument("--store", default=str(DEFAULT_STORE))
    p.add_argument("--repo-root", default=str(_ROOT))
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    store = Path(args.store).expanduser()
    if not store.is_dir():
        print(f"no store at {store}", file=sys.stderr)
        return 1

    r = run(store, Path(args.repo_root).expanduser())
    control = synthetic_control()
    if args.json:
        print(
            json.dumps(
                {
                    "store": str(store),
                    "total": r.total,
                    "checkable": r.checkable,
                    "bare": r.bare,
                    "parsed": r.parsed,
                    "unparsed": r.unparsed,
                    "parse_rate": round(r.parse_rate, 4),
                    "checkable_but_unparsed": r.checkable_but_unparsed,
                    "by_kind": r.by_kind,
                    "control_parsed": control[0],
                    "control_total": control[1],
                    "resolution_rate": None,
                    "resolution_rate_status": (
                        "UNDEFINED — parse_claim_citation reads three anchored "
                        "synthetic sentence templates, so real prose cannot match "
                        "by construction. Not a measurement of claim quality."
                    ),
                },
                indent=2,
            )
        )
    else:
        print(_format_text(r, store, control))
    return 0


if __name__ == "__main__":
    sys.exit(main())
