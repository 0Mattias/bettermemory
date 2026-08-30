"""Run an instrument with a W1 table swapped into the expansion leg.

The measurement harness the W1 declaration §4 constrains: the
ranking code path must be byte-identical to shipped
``search.search(rescue_expansion=True)`` except for the table
contents. The seam is the one the instruments themselves already use —
`bench/retrieval/run.py`'s own ``main()`` pokes engine module
attributes for its mechanism arms — and `search._EXPANSION_TABLES` is
read as a module global at each call, so replacing it before invoking
the runner swaps vocabulary and nothing else. `QUERY_FILLER_WORDS`
stays the live hand list in every arm; ``morph_variants`` is untouched.

Arms, per §5 of the declaration:

- ``full``: the learned table replaces all three hand lookup tables —
  empty ``irregular`` and ``clippings``, learned ``synonyms``.
- ``syn``: the learned table replaces ``SYNONYM_GROUPS`` only; the two
  high-precision hand tables ride along unchanged.
- ``static``: no swap at all — the shipped hand tables, for the paired
  integrity read in the same process and provenance.

The learned ``synonyms`` mapping is built through the same stemmer the
live ``build_tables`` uses: head and neighbors stem into ranker token
space, identity-after-stem pairs drop, and colliding heads union. The
emitted JSON wraps the runner's own artifact with the table-source
sha256 and the arm so every W1 number names the exact table it ranked
with.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType

_W_DIR = Path(__file__).parent
_ROOT = _W_DIR.parent.parent

sys.path.insert(0, str(_ROOT / "src"))

from bettermemory import search as _engine  # noqa: E402
from bettermemory.expansion import ExpansionTables, build_tables  # noqa: E402


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def learned_tables(table_path: Path, arm: str) -> tuple[ExpansionTables, str]:
    """(the arm's ExpansionTables, the table source's sha256)."""
    table_sha = hashlib.sha256(table_path.read_bytes()).hexdigest()
    table_module = _load_module("w1_learned_table", table_path)
    surface: dict[str, tuple[str, ...]] = table_module.SURFACE_NEIGHBORS
    live = build_tables(_engine._stem_token)
    synonyms: dict[str, set[str]] = {}
    for head, neighbors in surface.items():
        head_stem = _engine._stem_token(head)
        mates = {_engine._stem_token(n) for n in neighbors} - {head_stem}
        if mates:
            synonyms.setdefault(head_stem, set()).update(mates)
    learned = {k: frozenset(v) for k, v in synonyms.items()}
    if arm == "full":
        tables = ExpansionTables(
            filler_stems=live.filler_stems,
            irregular={},
            clippings={},
            synonyms=learned,
        )
    elif arm == "syn":
        tables = ExpansionTables(
            filler_stems=live.filler_stems,
            irregular=live.irregular,
            clippings=live.clippings,
            synonyms=learned,
        )
    else:
        raise ValueError(f"unknown arm {arm!r}")
    return tables, table_sha


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an instrument with a W1 table in the expansion leg."
    )
    parser.add_argument("--table", required=True, help="generated table module")
    parser.add_argument("--arm", required=True, choices=("full", "syn", "static"))
    parser.add_argument(
        "--instrument",
        default="dev",
        choices=("dev", "longmemeval"),
    )
    parser.add_argument("--out", required=True, help="wrapped-artifact JSON path")
    parser.add_argument(
        "--runner-args",
        default="",
        help="extra args passed through to the instrument runner",
    )
    args = parser.parse_args()

    table_sha = None
    if args.arm != "static":
        tables, table_sha = learned_tables(Path(args.table), args.arm)
        _engine._EXPANSION_TABLES = tables

    if args.instrument == "dev":
        runner_path = _ROOT / "bench" / "retrieval" / "run.py"
    else:
        runner_path = _ROOT / "bench" / "longmemeval" / "run.py"

    argv = [str(runner_path), "--rescue-expansion", "on", "--json"]
    if args.runner_args:
        argv.extend(args.runner_args.split())

    captured = io.StringIO()
    old_argv = sys.argv
    sys.argv = argv
    try:
        runner = _load_module("w1_instrument_runner", runner_path)
        with contextlib.redirect_stdout(captured):
            code = runner.main()
    finally:
        sys.argv = old_argv
    if code != 0:
        print(captured.getvalue(), file=sys.stderr)
        return code

    wrapped = {
        "w1": {
            "arm": args.arm,
            "table": args.table if args.arm != "static" else None,
            "table_sha256": table_sha,
            "instrument": args.instrument,
            "runner_args": args.runner_args,
        },
        "runner": json.loads(captured.getvalue()),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(wrapped, indent=1, sort_keys=True) + "\n")
    summary = {"arm": args.arm, "out": str(out)}
    results = wrapped["runner"].get("results")
    if isinstance(results, list):
        for row in results:
            if isinstance(row, dict) and "recall_at_1" in row:
                summary[f"{row.get('arm')}/{row.get('probe')}"] = (
                    f"r@1={row['recall_at_1']} r@5={row['recall_at_5']}"
                )
            elif isinstance(row, dict) and "macro" in row:
                summary[f"{row.get('arm')}/macro"] = row["macro"]
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
