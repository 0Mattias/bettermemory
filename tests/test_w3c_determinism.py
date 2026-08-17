"""G3's CI leg for W3-C: the builder reproduces itself byte-for-byte.

The determinism bar of `bench/w/W3C_DECLARATION.md` §7 has a CI half:
both table builds, the composition, and the emission run twice over
hand-written pair and edge fixtures — no derived-file bytes — and must
produce byte-identical table source and identical build payloads on
every push. The fixture also pins the declared rules themselves: the
exclusive-substitution count and PPMI floors, the label weights and
the score floor, the SYMMETRIZATION deviation (an edge stored
`gibson→guitar` must surface under the `guitar` head), the D-first
2+2 interleave with dedup and backfill, the caps, and the B0 rows —
so a drift in any of them fails here before it can silently reshape
a read.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location(
        "w3c_bridge", _ROOT / "bench" / "w" / "w3c_bridge.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["w3c_bridge"] = module
    spec.loader.exec_module(module)
    return module


PAIR_LINES = [
    "site-a\tbattery life short\tcharging pad problem\n",
    "site-a\tbattery life short\tcharging pad problem\n",
    "site-b\tbattery life short\tcharging pad problem\n",
    # Below the fixture count floor: contributes nothing.
    "site-b\tunique alpha row\tbeta gamma row\n",
    # Malformed line: skipped.
    "just-two\tfields\n",
]

EDGE_LINES = [
    # Stored brand->instrument; must surface under the instrument head.
    "gibson\tguitar\tgloss-link\twiktionary\t1\n",
    "gibson\tguitar\tgloss\twiktionary\t1\n",
    # A synonyms edge carries weight 6.
    "cocktail\tdrink\tsynonyms\twiktionary\t1\n",
    # A lone weight-1 gloss token dies at the score floor.
    "lonely\tword\tgloss\twiktionary\t1\n",
    # Unknown label: ignored.
    "odd\tball\tmystery\twiktionary\t1\n",
]


def _build_once(builder: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    table_p, stats_p = builder.build_table_p(
        iter(PAIR_LINES), count_floor=2, ppmi_floor=0.5
    )
    table_d, stats_d = builder.build_table_d(iter(EDGE_LINES))
    composed = builder.compose(table_d, table_p)
    source = builder.emit_table_source(composed, "fixture build")
    b0 = builder.b0_rows(composed)
    return source, {"p": stats_p, "d": stats_d, "b0": b0}, composed


def test_builder_reproduces_itself_and_pins_the_rules() -> None:
    builder = _load_builder()
    source_a, payload_a, composed_a = _build_once(builder)
    source_b, payload_b, _ = _build_once(builder)
    assert source_a == source_b, "table source is not byte-stable"
    assert payload_a == payload_b, "build payload is not stable"

    # Table P: nine substitution types at count 3, one row below floor.
    assert payload_a["p"]["substitution_pair_types"] == 13
    assert payload_a["p"]["types_at_count_floor"] == 9
    assert composed_a["battery"] == ["charging", "pad", "problem"]

    # Table D symmetrization: the stored head was gibson, the ask-side
    # lookup is guitar — both directions must exist.
    assert composed_a["guitar"] == ["gibson"]
    assert composed_a["gibson"] == ["guitar"]
    assert composed_a["cocktail"] == ["drink"]
    # The weight-1 singleton and the unknown label leave no heads.
    assert "lonely" not in composed_a and "odd" not in composed_a

    # B0: battery, guitar, and cocktail needs carry bridges; the
    # situational needs do not.
    b0 = payload_a["b0"]
    assert b0["survived"] == 3
    assert b0["needs"]["09d032c9"]["survived"] is True
    assert ["guitar", "gibson"] in b0["needs"]["95228167"]["bridges"]
    assert b0["needs"]["1a1907b4"]["survived"] is True
    assert b0["needs"]["d6233ab6"]["survived"] is False

    # The emitted module is exec-able and carries the composed table
    # under the harness's expected name.
    namespace: dict[str, Any] = {}
    exec(compile(source_a, "<w3c_table>", "exec"), namespace)
    surface = namespace["SURFACE_NEIGHBORS"]
    assert surface["guitar"] == ("gibson",)
    assert surface["battery"] == ("charging", "pad", "problem")


def test_composition_interleaves_d_first_with_dedup_and_backfill() -> None:
    builder = _load_builder()
    table_d = builder.RankedTable(
        heads=["head"], terms={"head": ["d1", "shared", "d3", "d4"]}
    )
    table_p = builder.RankedTable(
        heads=["head"], terms={"head": ["shared", "p2", "p3", "p4"]}
    )
    composed = builder.compose(table_d, table_p)
    # D[:2] = d1, shared; P[:2] = shared (dup, kept first), p2; backfill
    # D[2:] = d3 — cap at four.
    assert composed["head"] == ["d1", "shared", "p2", "d3"]


def test_declared_default_floors_are_the_declaration_values() -> None:
    builder = _load_builder()
    assert builder.P_COUNT_FLOOR == 10
    assert builder.P_PPMI_FLOOR == 2.0
    assert builder.P_MUTUAL_RANK == 8
    assert builder.D_SCORE_FLOOR == 2
    assert builder.D_LABEL_WEIGHTS["synonyms"] == 6
    assert builder.D_LABEL_WEIGHTS["hypernyms"] == 4
    assert builder.D_LABEL_WEIGHTS["gloss"] == 1
    assert builder.COMPOSE_D_SLOTS == 2 and builder.COMPOSE_P_SLOTS == 2
    assert builder.COMPOSE_TERMS_PER_HEAD == 4
    assert builder.TABLE_SOURCE_CAP_BYTES == 300 * 1024
