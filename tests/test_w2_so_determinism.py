"""The CI leg for the W2 SO census: the arrangement equals the reference.

The determinism clause of the W2 SO-census declaration §6 has
a CI half, and its center of gravity is an equivalence: the eager
resolution arrangement the declaration owns must produce, on a
committed synthetic fixture, exactly what the imported reference
(`w2_body_census.W2BodyCensus.add_site`) produces — an identical counts
block, an identical site payload, and the same derived rows as sets —
with the fixture exercising a self-edge, a shared-target fan, an
unresolved edge, a body-less member, both crossing directions, the
dedup rule and both drop rules. The leg also pins the two rows the
declaration owns (the directional split summing to the imported
exclusive count, the unique-target set collapsing on a fan), the
stage 0 title path with its degenerate markup variant, and the
from-derived repeat reproducing the counts block — no corpus bytes, no
numpy.

The probe rows are hand-built (`SOProbeCensus` takes the pair lists as
arguments) because the committed probe modules import numpy, which is
bench-side only; the real run resolves them through
`committed_probe_pairs` and records them in the artifact.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load_reader() -> Any:
    spec = importlib.util.spec_from_file_location(
        "w2_so_census", _ROOT / "bench" / "w" / "w2_so_census.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["w2_so_census"] = module
    spec.loader.exec_module(module)
    return module


def _link_row(row_id: int, post_id: int, related_id: int, link_type: int) -> bytes:
    return (
        f'  <row Id="{row_id}" CreationDate="2020-01-01T00:00:00.000" '
        f'PostId="{post_id}" RelatedPostId="{related_id}" '
        f'LinkTypeId="{link_type}" />\n'
    ).encode()


def _escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _post_row(row_id: int, post_type: str, body_html: str) -> bytes:
    return (
        f'  <row Id="{row_id}" PostTypeId="{post_type}" '
        f'Body="{_escape_attr(body_html)}" />\n'
    ).encode()


SIX_FIXTURE = [("toggle", "flag"), ("undo", "rollback"), ("timeout", "expiry")]
EXPANDED_FIXTURE = [("flag", "boolean")]

LINK_FIXTURE: list[bytes] = [
    b'<?xml version="1.0" encoding="utf-8"?>\n',
    b"<postlinks>\n",
    # A clean duplicate edge: toggle on the closed side, flag on the
    # target side.
    _link_row(1, 10, 20, 3),
    # LinkTypeId=1 ("linked"): counted as a link row, never an edge.
    _link_row(2, 10, 30, 1),
    # Exact repeat of the first edge: deduped.
    _link_row(3, 10, 20, 3),
    # undo/rollback, with the closure-notice blockquote on the closed
    # side.
    _link_row(4, 30, 40, 3),
    # The shared-target fan: a second closed question into target 40,
    # so undo/rollback mints two crossings from one target.
    _link_row(5, 35, 40, 3),
    # The reverse direction: flag on the closed side, toggle on the
    # target side.
    _link_row(6, 45, 25, 3),
    # A self-edge: resolves against itself and drops by the identical
    # prose rule.
    _link_row(7, 50, 50, 3),
    # Edge to a post absent from the dump: unresolved.
    _link_row(8, 60, 99, 3),
    # Edge to a question row that carries no Body attribute: unresolved.
    _link_row(9, 70, 75, 3),
    # A body that cleans to fewer than two content tokens: dropped.
    _link_row(10, 80, 20, 3),
    # The NEEDS continuity pair (battery <-> charging).
    _link_row(11, 100, 110, 3),
    b"</postlinks>\n",
]

POST_FIXTURE: list[bytes] = [
    b'<?xml version="1.0" encoding="utf-8"?>\n',
    b"<posts>\n",
    # toggle in prose; flag ONLY inside a code span, so the prose
    # surface keeps the exclusive substitution and the markup-text
    # variant destroys it.
    _post_row(
        10,
        "1",
        "<p>How can I toggle the dark setting permanently?</p><code>--flag</code>",
    ),
    _post_row(20, "1", "<p>You can flip the flag value in the panel.</p>"),
    _post_row(25, "1", "<p>Where is the dark mode toggle located exactly?</p>"),
    # The closure notice quotes the mate's vocabulary in a blockquote:
    # the prose surface drops it in one cleaning fixpoint.
    _post_row(
        30,
        "1",
        "<blockquote>Possible Duplicate: <code>rollback</code> my"
        " changes</blockquote><p>I need to undo my last edit quickly.</p>",
    ),
    _post_row(35, "1", "<p>Please undo the broken deploy for me now.</p>"),
    # rollback in prose; timeout and expiry co-occur on one side, so the
    # same-side row reads once per kept pair that touches this body.
    _post_row(
        40,
        "1",
        "<p>Use the rollback feature after the timeout expiry passes.</p>",
    ),
    _post_row(45, "1", "<p>Set the flag in the configuration file itself.</p>"),
    _post_row(50, "1", "<p>The self edge resolves and drops by rule.</p>"),
    _post_row(60, "1", "<p>A question whose duplicate target is gone.</p>"),
    _post_row(70, "1", "<p>A question whose duplicate target has no body.</p>"),
    b'  <row Id="75" PostTypeId="1" Title="No body attribute here" />\n',
    # Cleans to a single sub-length token: dropped by the token rule.
    _post_row(80, "1", "<p><code>ls -la</code> ok</p>"),
    _post_row(100, "1", "<p>Android battery drain when idle</p>"),
    _post_row(110, "1", "<p>Reduce charging cycles and power usage on Android</p>"),
    # A question no edge touches: censused, otherwise ignored.
    _post_row(120, "1", "<p>What is a metaclass?</p>"),
    # An answer row: censused by type, contributes no body.
    _post_row(200, "2", "<p>An answer body.</p>"),
    b"</posts>\n",
]


def _run_eager(reader: Any) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    out = io.BytesIO()
    probes = reader.SOProbeCensus(SIX_FIXTURE, EXPANDED_FIXTURE)
    site = reader.eager_site_read(
        "so", iter(LINK_FIXTURE), iter(POST_FIXTURE), probes, out
    )
    return out.getvalue(), site.payload(), probes.counts_payload()


def _run_reference(reader: Any) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    out = io.BytesIO()
    census = reader.W2BodyCensus(
        probes=reader.ProbeCensus(SIX_FIXTURE, EXPANDED_FIXTURE)
    )
    site = census.add_site("so", iter(LINK_FIXTURE), iter(POST_FIXTURE), out)
    return out.getvalue(), site.payload(), census.probes.counts_payload()


def _strip_owned_rows(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop the two declaration-owned rows so the remainder must equal
    the imported reference's block exactly."""
    clone = json.loads(json.dumps(payload))
    for block in ("committed_six", "expanded_family"):
        for row in clone[block]:
            row.pop("a_on_post_side", None)
            row.pop("a_on_related_side", None)
            row.pop("unique_targets", None)
    return clone


def _eager_rows_as_reference(derived: bytes) -> list[str]:
    """Project the seven-column derived rows onto the reference's five."""
    rows = []
    for line in derived.decode().splitlines():
        parts = line.split("\t")
        assert len(parts) == 7
        rows.append("\t".join([parts[0], *parts[3:]]))
    return rows


def test_eager_arrangement_equals_the_imported_reference() -> None:
    reader = _load_reader()
    eager_tsv, eager_site, eager_counts = _run_eager(reader)
    ref_tsv, ref_site, ref_counts = _run_reference(reader)

    assert eager_site == ref_site, "site payload diverges from the reference"
    assert json.dumps(_strip_owned_rows(eager_counts), sort_keys=True) == json.dumps(
        ref_counts, sort_keys=True
    ), "counts block diverges from the reference"
    assert sorted(_eager_rows_as_reference(eager_tsv)) == sorted(
        ref_tsv.decode().splitlines()
    ), "derived rows diverge from the reference as sets"

    # The fixture's accounting, pinned once on the shared numbers.
    assert eager_site["rows_scanned"] == 16
    assert eager_site["rows_by_post_type"] == {"1": 15, "2": 1}
    assert eager_site["link_rows"] == 11
    assert eager_site["duplicate_edge_rows"] == 10
    assert eager_site["deduped_edges"] == 9
    assert eager_site["unresolved_edges"] == 2
    assert eager_site["dropped_by_rule"] == 2
    assert eager_site["pairs"] == 5


def test_reader_reproduces_itself() -> None:
    reader = _load_reader()
    first = _run_eager(reader)
    second = _run_eager(reader)
    assert first[0] == second[0], "derived output is not byte-stable"
    assert json.dumps(first[1], sort_keys=True) == json.dumps(
        second[1], sort_keys=True
    ), "site payload is not stable"
    assert json.dumps(first[2], sort_keys=True) == json.dumps(
        second[2], sort_keys=True
    ), "counts block is not stable"


def test_directional_and_unique_target_rows() -> None:
    reader = _load_reader()
    _, _, counts = _run_eager(reader)
    by_pair = {(r["left"], r["right"]): r for r in counts["committed_six"]}

    toggle = by_pair[("toggle", "flag")]
    assert toggle["exclusive_all"] == 2
    assert toggle["a_on_post_side"] == 1
    assert toggle["a_on_related_side"] == 1
    assert toggle["unique_targets"] == 2
    # The code-span flag fell to the markup variant on one crossing only.
    assert toggle["exclusive_markup_all"] == 1

    undo = by_pair[("undo", "rollback")]
    assert undo["exclusive_all"] == 2
    assert undo["a_on_post_side"] == 2
    assert undo["a_on_related_side"] == 0
    # The fan: two crossings minted by one canonical target.
    assert undo["unique_targets"] == 1

    timeout = by_pair[("timeout", "expiry")]
    assert timeout["exclusive_all"] == 0
    assert timeout["same_side_all"] == 2

    for row in counts["committed_six"] + counts["expanded_family"]:
        assert (
            row["a_on_post_side"] + row["a_on_related_side"] == row["exclusive_all"]
        ), f"directional rows do not sum on {row['left']}/{row['right']}"


def test_stage0_title_path_is_degenerate_and_counted() -> None:
    reader = _load_reader()
    probes = reader.SOProbeCensus(SIX_FIXTURE, EXPANDED_FIXTURE)
    lines = [
        "How do I toggle dark mode\tSet the flag for the dark theme\n",
        "a malformed line without a second column\n",
        "Undo the last commit\tHow to rollback a commit safely\n",
    ]
    fed = reader.stage0_read(iter(lines), probes)
    assert fed == {"lines_read": 3, "pairs_fed": 2}
    counts = probes.counts_payload()
    by_pair = {(r["left"], r["right"]): r for r in counts["committed_six"]}
    toggle = by_pair[("toggle", "flag")]
    assert toggle["exclusive_all"] == 1
    assert toggle["a_on_post_side"] == 1
    # No RelatedPostId on the title surface: the unique-target row does
    # not accumulate at stage 0.
    assert toggle["unique_targets"] == 0
    for row in counts["committed_six"]:
        assert row["exclusive_markup_all"] == row["exclusive_all"], (
            "the markup variant must be degenerate on a title surface"
        )


def test_from_derived_repeat_reproduces_the_counts(tmp_path: Path) -> None:
    reader = _load_reader()
    eager_tsv, _, eager_counts = _run_eager(reader)
    derived = tmp_path / "w2-so-bodies-fixture.tsv"
    derived.write_bytes(eager_tsv)
    probes = reader.census_from_derived(
        derived, reader.SOProbeCensus(SIX_FIXTURE, EXPANDED_FIXTURE)
    )
    assert json.dumps(probes.counts_payload(), sort_keys=True) == json.dumps(
        eager_counts, sort_keys=True
    ), "the from-derived repeat does not reproduce the counts block"
