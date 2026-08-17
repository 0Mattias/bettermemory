"""G3's CI leg for W3-P: the reader reproduces itself byte-for-byte.

The determinism bar of `bench/w/W3P_DECLARATION.md` §7 has a CI half:
the extraction and census code paths run twice over a committed
synthetic fixture — hand-written XML rows below, no corpus bytes — and
must produce byte-identical pair output and an identical census
payload on every push. The fixture also pins the extraction rule
itself: which rows yield pairs, which are dropped, and how titles are
cleaned, so a drift in the declared rule fails here before it can
silently reshape a census.
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
        "w3p_pairs", _ROOT / "bench" / "w" / "w3p_pairs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["w3p_pairs"] = module
    spec.loader.exec_module(module)
    return module


def _row(post_type: str, title: str, body_html: str) -> bytes:
    escaped = (
        body_html.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    escaped_title = title.replace("&", "&amp;").replace('"', "&quot;")
    return (
        f'  <row Id="1" PostTypeId="{post_type}" '
        f'Title="{escaped_title}" Body="{escaped}" />\n'
    ).encode()


def _dup_body(target_title_html: str, marker: str = "Possible Duplicate") -> str:
    return (
        f"<blockquote><p><strong>{marker}:</strong><br>"
        f'<a href="https://stackoverflow.com/questions/42/x">'
        f"{target_title_html}</a></p></blockquote><p>My question body.</p>"
    )


FIXTURE_ROWS: list[bytes] = [
    b'<?xml version="1.0" encoding="utf-8"?>\n',
    b"<posts>\n",
    # Yields a pair; the trailing [duplicate] marker is stripped.
    _row(
        "1",
        "How do I sort a dictionary by value? [duplicate]",
        _dup_body("Sorting dictionaries by their values"),
    ),
    # Lowercase marker variant; anchor carries a tag and an entity.
    _row(
        "1",
        "Remove an element from a list by index",
        _dup_body(
            "Deleting <em>items</em> &amp; entries from arrays",
            marker="Possible duplicate",
        ),
    ),
    # The need-support row: battery on one side, charging on the other
    # (the 09d032c9 bridge need), exclusively substituted.
    _row(
        "1",
        "Android battery drain when idle",
        _dup_body("Reduce charging cycles and power usage on Android"),
    ),
    # Question without the marker: counted, no pair.
    _row("1", "What is a metaclass?", "<p>Plain question body.</p>"),
    # Answer row carrying the marker text: skipped by post type.
    _row("2", "", _dup_body("An answer quoting the closure banner")),
    # Marker but no anchor: a marker row that yields nothing.
    _row("1", "Orphaned closure banner", "<p>Possible Duplicate: gone</p>"),
    # Identical titles after cleaning: dropped.
    _row("1", "Same title twice", _dup_body("Same title twice")),
    # Too few content tokens on the left: dropped ("me" is short,
    # "help" survives alone).
    _row("1", "Help me", _dup_body("Assistance requested with a task")),
    b"</posts>\n",
]

EXPECTED_PAIRS = [
    (
        "How do I sort a dictionary by value?",
        "Sorting dictionaries by their values",
    ),
    (
        "Remove an element from a list by index",
        "Deleting items & entries from arrays",
    ),
    (
        "Android battery drain when idle",
        "Reduce charging cycles and power usage on Android",
    ),
]


def test_reader_reproduces_itself_and_pins_the_rule() -> None:
    reader = _load_reader()

    def run_once() -> tuple[bytes, str]:
        out = io.BytesIO()
        census = reader.census_from_rows(iter(FIXTURE_ROWS), out)
        payload = json.dumps(census.counts_payload(), sort_keys=True)
        return out.getvalue(), payload

    pairs_a, payload_a = run_once()
    pairs_b, payload_b = run_once()
    assert pairs_a == pairs_b, "pair output is not byte-stable"
    assert payload_a == payload_b, "census payload is not stable"

    got_pairs = [tuple(line.split("\t")) for line in pairs_a.decode().splitlines()]
    assert got_pairs == [tuple(p) for p in EXPECTED_PAIRS]

    payload = json.loads(payload_a)
    assert payload["rows_scanned"] == 8
    assert payload["rows_by_post_type"] == {"1": 7, "2": 1}
    assert payload["marker_rows"] == 6
    assert payload["pairs_total"] == 3
    assert payload["needs"]["09d032c9"]["count"] == 1
    assert payload["floors"]["V"]["holds"] is False
    assert payload["g0_verdict"] == "PARK-AT-CENSUS"


def test_tokenizer_enforces_the_declared_floors() -> None:
    reader = _load_reader()
    tokens = reader.content_tokens("I just really need the Battery-Life FIXED now ok")
    assert "just" not in tokens and "really" not in tokens, (
        "filler stems must be dropped"
    )
    assert "battery" in tokens and "life" in tokens and "fixed" in tokens
    assert all(3 <= len(t) <= 30 for t in tokens)


def test_title_marker_stripping_is_iterative() -> None:
    reader = _load_reader()
    assert (
        reader.strip_title_markers("Sort a dict [closed] [duplicate]") == "Sort a dict"
    )
    assert reader.strip_title_markers("No markers here") == "No markers here"
