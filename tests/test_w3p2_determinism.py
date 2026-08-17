"""G3's CI leg for W3-P2: the reader reproduces itself byte-for-byte.

The determinism bar of `bench/w/W3P2_DECLARATION.md` §7 has a CI half:
the extraction and census code paths run twice over a committed
synthetic fixture — hand-written PostLinks and Posts rows below, no
corpus bytes — and must produce byte-identical pair output and an
identical census payload on every push. The fixture also pins the
declared rule itself: the LinkTypeId=3 filter, edge dedup in document
order, unresolved-edge accounting, title cleaning, the pair-drop
conditions, the missing-member rule, and the floors — so a drift in
any of them fails here before it can silently reshape a census.
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
        "w3p2_pairs", _ROOT / "bench" / "w" / "w3p2_pairs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["w3p2_pairs"] = module
    spec.loader.exec_module(module)
    return module


def _link_row(row_id: int, post_id: int, related_id: int, link_type: int) -> bytes:
    return (
        f'  <row Id="{row_id}" CreationDate="2020-01-01T00:00:00.000" '
        f'PostId="{post_id}" RelatedPostId="{related_id}" '
        f'LinkTypeId="{link_type}" />\n'
    ).encode()


def _post_row(row_id: int, post_type: str, title: str | None, body: str) -> bytes:
    escaped_title = (
        "" if title is None else title.replace("&", "&amp;").replace('"', "&quot;")
    )
    title_attr = "" if title is None else f'Title="{escaped_title}" '
    return (
        f'  <row Id="{row_id}" PostTypeId="{post_type}" {title_attr}Body="{body}" />\n'
    ).encode()


LINK_FIXTURE: list[bytes] = [
    b'<?xml version="1.0" encoding="utf-8"?>\n',
    b"<postlinks>\n",
    # A clean duplicate edge: 10 closed as duplicate of 20.
    _link_row(1, 10, 20, 3),
    # LinkTypeId=1 ("linked"): counted as a link row, never an edge.
    _link_row(2, 10, 30, 1),
    # Exact repeat of the first edge: deduped.
    _link_row(3, 10, 20, 3),
    # The need-support edge (battery <-> charging, 09d032c9).
    _link_row(4, 30, 40, 3),
    # Edge to a post absent from the dump: unresolved.
    _link_row(5, 50, 99, 3),
    # Edge to an answer row: unresolved (only questions carry titles).
    _link_row(6, 10, 60, 3),
    # Edge whose titles clean to identical strings: dropped by rule.
    _link_row(7, 70, 80, 3),
    b"</postlinks>\n",
]

POST_FIXTURE: list[bytes] = [
    b'<?xml version="1.0" encoding="utf-8"?>\n',
    b"<posts>\n",
    # The trailing [duplicate] marker is stripped; entity unescaped.
    _post_row(10, "1", 'How do I sort a dictionary & "big" list? [duplicate]', "b"),
    _post_row(20, "1", "Sorting dictionaries by their values", "b"),
    _post_row(30, "1", "Android battery drain when idle", "b"),
    _post_row(40, "1", "Reduce charging cycles and power usage on Android", "b"),
    _post_row(50, "1", "A question whose duplicate target is gone", "b"),
    # An answer row: censused by type, contributes no title.
    _post_row(60, "2", None, "an answer body"),
    # Titles identical after marker stripping: the edge drops by rule.
    _post_row(70, "1", "Same question twice [closed]", "b"),
    _post_row(80, "1", "Same question twice", "b"),
    # A question no edge touches: censused, otherwise ignored.
    _post_row(90, "1", "What is a metaclass?", "b"),
    b"</posts>\n",
]

EXPECTED_PAIRS = [
    (
        "site-a",
        'How do I sort a dictionary & "big" list?',
        "Sorting dictionaries by their values",
    ),
    (
        "site-a",
        "Android battery drain when idle",
        "Reduce charging cycles and power usage on Android",
    ),
]


def test_reader_reproduces_itself_and_pins_the_rule() -> None:
    reader = _load_reader()

    def run_once() -> tuple[bytes, str]:
        out = io.BytesIO()
        census = reader.W3P2Census()
        census.add_site("site-a", iter(LINK_FIXTURE), iter(POST_FIXTURE), out)
        census.add_missing("site-b")
        payload = json.dumps(census.counts_payload(), sort_keys=True)
        return out.getvalue(), payload

    pairs_a, payload_a = run_once()
    pairs_b, payload_b = run_once()
    assert pairs_a == pairs_b, "pair output is not byte-stable"
    assert payload_a == payload_b, "census payload is not stable"

    got_pairs = [tuple(line.split("\t")) for line in pairs_a.decode().splitlines()]
    assert got_pairs == [tuple(p) for p in EXPECTED_PAIRS]

    payload = json.loads(payload_a)
    site_a = payload["sites"]["site-a"]
    assert site_a["status"] == "read"
    assert site_a["rows_scanned"] == 9
    assert site_a["rows_by_post_type"] == {"1": 8, "2": 1}
    assert site_a["link_rows"] == 7
    assert site_a["duplicate_edge_rows"] == 6
    assert site_a["deduped_edges"] == 5
    assert site_a["unresolved_edges"] == 2
    assert site_a["dropped_by_rule"] == 1
    assert site_a["pairs"] == 2
    assert site_a["needs"]["09d032c9"] == 1
    assert payload["sites"]["site-b"] == {"status": "missing-member", "pairs": 0}
    assert payload["pairs_total"] == 2
    assert payload["needs"]["09d032c9"]["count"] == 1
    assert payload["needs"]["09d032c9"]["supported"] is False
    assert payload["floors"]["V"]["threshold_pairs"] == 25_000
    assert payload["floors"]["C"]["threshold_needs"] == 4
    assert payload["floors"]["V"]["holds"] is False
    assert payload["w3p_floor_continuity"]["V_50000_holds"] is False
    assert payload["g0_verdict"] == "PARK-AT-CENSUS"


def test_edge_pass_orders_and_dedups_in_document_order() -> None:
    reader = _load_reader()
    stats = reader.duplicate_edges(iter(LINK_FIXTURE))
    assert stats.link_rows == 7
    assert stats.duplicate_rows == 6
    assert stats.edges == [
        (b"10", b"20"),
        (b"30", b"40"),
        (b"50", b"99"),
        (b"10", b"60"),
        (b"70", b"80"),
    ]


def test_declared_site_list_is_the_registered_eighteen() -> None:
    reader = _load_reader()
    register = json.loads((_ROOT / "bench" / "w" / "corpora.json").read_text())
    names = {e.get("name") for e in register["corpora"]}
    assert len(reader.SITE_NAMES) == 18
    missing = [n for n in reader.SITE_NAMES if n not in names]
    assert missing == [], f"declared archives missing from the register: {missing}"
