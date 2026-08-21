"""The CI leg for the W2 body census: the reader reproduces itself.

The determinism clause of the W2 body-census declaration §5
has a CI half: the extraction and census code paths run twice over a
committed synthetic fixture — hand-written PostLinks and Posts rows
below, no corpus bytes, no numpy — and must produce byte-identical
derived output and an identical counts block on every push. The
fixture also pins the declared rules themselves: the edge pass through
the imported W3-P2 code path, body resolution, the cleaning fixpoint
(blockquote / pre / code spans fall from the prose surface, tags-only
for the markup-text variant), the two-stage entity unescape, the
pair-drop conditions, the missing-member rule, exclusive substitution
on both surfaces, same-side co-occurrence, the presence rows, the
NEEDS continuity row, and the ladder constants — so a drift in any of
them fails here before it can silently reshape a census.

The probe rows are hand-built (`ProbeCensus` takes the pair lists as
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
        "w2_body_census", _ROOT / "bench" / "w" / "w2_body_census.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["w2_body_census"] = module
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


# The fixture probe sets: three prize-shaped rows and one expanded row,
# hand-built. The ladder constants still read 4-of-six / 2-of-six, so
# the fixture verdict is park by construction.
SIX_FIXTURE = [("toggle", "flag"), ("undo", "rollback"), ("timeout", "expiry")]
EXPANDED_FIXTURE = [("flag", "boolean")]

LINK_FIXTURE: list[bytes] = [
    b'<?xml version="1.0" encoding="utf-8"?>\n',
    b"<postlinks>\n",
    # A clean duplicate edge: the toggle/flag pair.
    _link_row(1, 10, 20, 3),
    # LinkTypeId=1 ("linked"): counted as a link row, never an edge.
    _link_row(2, 10, 30, 1),
    # Exact repeat of the first edge: deduped.
    _link_row(3, 10, 20, 3),
    # The undo/rollback pair, with the closure-notice blockquote.
    _link_row(4, 30, 40, 3),
    # Edge to a post absent from the dump: unresolved.
    _link_row(5, 50, 99, 3),
    # Edge to an answer row: unresolved (only questions resolve).
    _link_row(6, 10, 60, 3),
    # Bodies identical after cleaning: dropped by rule.
    _link_row(7, 70, 80, 3),
    # A body that cleans to fewer than two content tokens: dropped.
    _link_row(8, 90, 20, 3),
    # The NEEDS continuity pair (battery <-> charging, 09d032c9).
    _link_row(9, 100, 110, 3),
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
    # The platform's closure notice quotes the mate's vocabulary in a
    # blockquote (with a nested code span): the prose surface drops it,
    # the markup-text variant keeps it and loses exclusivity.
    _post_row(
        30,
        "1",
        "<blockquote>Possible Duplicate: <code>rollback</code> my"
        " changes</blockquote><p>I need to undo my last edit quickly.</p>",
    ),
    # rollback in prose; timeout and expiry co-occur on one side.
    _post_row(
        40,
        "1",
        "<p>Use the rollback feature after the timeout expiry passes.</p>",
    ),
    _post_row(50, "1", "<p>A question whose duplicate target is gone.</p>"),
    # An answer row: censused by type, contributes no body.
    _post_row(60, "2", "<p>An answer body.</p>"),
    # Identical bodies: the edge drops by rule.
    _post_row(70, "1", "<p>The same body twice for the drop rule.</p>"),
    _post_row(80, "1", "<p>The same body twice for the drop rule.</p>"),
    # Cleans to a single sub-length token: dropped by the token rule.
    _post_row(90, "1", "<p><code>ls -la</code> ok</p>"),
    _post_row(100, "1", "<p>Android battery drain when idle</p>"),
    _post_row(110, "1", "<p>Reduce charging cycles and power usage on Android</p>"),
    # A question no edge touches: censused, otherwise ignored.
    _post_row(120, "1", "<p>What is a metaclass?</p>"),
    b"</posts>\n",
]


def _run_once(reader: Any) -> tuple[bytes, str, dict[str, Any]]:
    out = io.BytesIO()
    census = reader.W2BodyCensus(
        probes=reader.ProbeCensus(SIX_FIXTURE, EXPANDED_FIXTURE)
    )
    census.add_site("site-a", iter(LINK_FIXTURE), iter(POST_FIXTURE), out)
    census.add_missing("site-b")
    sites = json.dumps(census.sites_payload(), sort_keys=True)
    return out.getvalue(), sites, census.probes.counts_payload()


def test_reader_reproduces_itself_and_pins_the_rule() -> None:
    reader = _load_reader()

    bodies_a, sites_a, counts_a = _run_once(reader)
    bodies_b, sites_b, counts_b = _run_once(reader)
    assert bodies_a == bodies_b, "derived output is not byte-stable"
    assert sites_a == sites_b, "site payload is not stable"
    assert json.dumps(counts_a, sort_keys=True) == json.dumps(
        counts_b, sort_keys=True
    ), "counts block is not stable"

    site = json.loads(sites_a)["site-a"]
    assert site["status"] == "read"
    assert site["rows_scanned"] == 12
    assert site["rows_by_post_type"] == {"1": 11, "2": 1}
    assert site["link_rows"] == 9
    assert site["duplicate_edge_rows"] == 8
    assert site["deduped_edges"] == 7
    assert site["unresolved_edges"] == 2
    assert site["dropped_by_rule"] == 2
    assert site["pairs"] == 3
    assert json.loads(sites_a)["site-b"] == {"status": "missing-member", "pairs": 0}

    lines = bodies_a.decode().splitlines()
    assert len(lines) == 3
    first = lines[0].split("\t")
    assert len(first) == 5
    assert first[0] == "site-a"
    # The prose surface lost the code span; the markup-text kept it.
    assert first[1] == "How can I toggle the dark setting permanently?"
    assert first[3] == "How can I toggle the dark setting permanently? --flag"
    # The blockquote (with its nested code span) fell from the prose
    # surface of the closed question in one cleaning fixpoint.
    second = lines[1].split("\t")
    assert second[1] == "I need to undo my last edit quickly."
    assert "rollback" in second[3]

    by_pair = {(r["left"], r["right"]): r for r in counts_a["committed_six"]}
    toggle = by_pair[("toggle", "flag")]
    assert toggle["exclusive_all"] == 1
    assert toggle["exclusive_markup_all"] == 0
    assert toggle["same_side_all"] == 0
    assert toggle["supported"] is False
    undo = by_pair[("undo", "rollback")]
    assert undo["exclusive_all"] == 1
    assert undo["exclusive_markup_all"] == 0
    timeout = by_pair[("timeout", "expiry")]
    assert timeout["exclusive_all"] == 0
    assert timeout["same_side_all"] == 1

    expanded = counts_a["expanded_family"]
    assert [(r["left"], r["right"]) for r in expanded] == [("flag", "boolean")]
    assert expanded[0]["exclusive_all"] == 0

    presence = counts_a["presence_bodies_prose"]
    stem = reader._stem_token
    assert presence[stem("toggle")] == 1
    assert presence[stem("flag")] == 1  # body 10's flag lives in code only
    assert presence[stem("rollback")] == 1  # body 30's is quoted, stripped
    assert presence[stem("undo")] == 1
    assert presence[stem("timeout")] == 1
    assert presence[stem("expiry")] == 1
    assert presence[stem("boolean")] == 0

    assert counts_a["pairs_total"] == 3
    assert counts_a["tech_register_pairs"] == 0
    assert counts_a["needs_continuity"]["09d032c9"]["count"] == 1
    readiness = counts_a["readiness"]
    assert readiness["six_supported"] == 0
    assert readiness["outcome"] == "park"
    assert readiness["license_min_of_six"] == 4
    assert readiness["twitch_min_of_six"] == 2
    assert counts_a["support_min"] == 5


def test_census_stage_reproduces_from_the_derived_file(tmp_path: Path) -> None:
    """The declared determinism repeat: the census stage re-read from
    the derived file returns the identical counts block."""
    reader = _load_reader()
    bodies, _sites, counts = _run_once(reader)

    derived = tmp_path / "w2-bodies-fixture.tsv"
    derived.write_bytes(bodies)
    reread = reader.census_from_derived(
        derived, reader.ProbeCensus(SIX_FIXTURE, EXPANDED_FIXTURE)
    )
    assert json.dumps(reread.counts_payload(), sort_keys=True) == json.dumps(
        counts, sort_keys=True
    )


def test_body_surfaces_unescape_in_two_stages() -> None:
    """Attribute entities decode to HTML; text entities decode after
    tag stripping — the declaration's step 1 and step 3 exactly."""
    reader = _load_reader()
    prose, full = reader.body_surfaces(b"&lt;p&gt;a &amp;amp; b&lt;/p&gt;")
    assert prose == "a & b"
    assert full == "a & b"
    prose, full = reader.body_surfaces(
        b"&lt;p&gt;kept&lt;/p&gt;&lt;pre&gt;&lt;code&gt;dropped"
        b"&lt;/code&gt;&lt;/pre&gt;"
    )
    assert prose == "kept"
    assert full == "kept dropped"
