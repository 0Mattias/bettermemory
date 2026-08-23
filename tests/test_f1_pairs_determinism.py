"""CI leg for the F1 stage-C/G readers: byte-identical on every drive.

The F1 unit runs its C and G stages under F-strict determinism; the
pair readers and the gold-graph pass are the derivation's first links,
so their rules get the same CI treatment the W3-P2 reader established:
each core runs twice over a committed synthetic fixture — hand-written
Posts, Comments, and PostLinks rows, no corpus bytes — and must produce
identical payloads and stream receipts. The fixture also pins the rules
themselves: title/body pairing, single-pass accepted-answer resolution
with unresolved accounting, comment-adjacency emission and the
predecessor-update rule, the duplicate-edge dedup and related-edge
canonicalization, transitive component merging, and the keyed-hash
split's insensitivity to edge order.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "bench" / "w" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _post_row(
    row_id: int,
    post_type: str,
    title: str | None = None,
    body: str | None = None,
    accepted: int | None = None,
) -> bytes:
    parts = [f'  <row Id="{row_id}" PostTypeId="{post_type}"']
    if accepted is not None:
        parts.append(f'AcceptedAnswerId="{accepted}"')
    if title is not None:
        escaped = title.replace("&", "&amp;").replace('"', "&quot;")
        parts.append(f'Title="{escaped}"')
    if body is not None:
        escaped = (
            body.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        parts.append(f'Body="{escaped}"')
    return (" ".join(parts) + " />\n").encode()


def _comment_row(row_id: int, post_id: int, text: str) -> bytes:
    escaped = text.replace("&", "&amp;").replace('"', "&quot;")
    return (f'  <row Id="{row_id}" PostId="{post_id}" Text="{escaped}" />\n').encode()


def _link_row(row_id: int, post_id: int, related_id: int, link_type: int) -> bytes:
    return (
        f'  <row Id="{row_id}" PostId="{post_id}" RelatedPostId="{related_id}" '
        f'LinkTypeId="{link_type}" />\n'
    ).encode()


_POSTS = [
    # Question 1: valid title and body -> tb pair; accepted answer 4 -> qa.
    _post_row(
        1,
        "1",
        title="Sourdough starter smells like acetone",
        body="<p>Feeding schedule slipped and the starter jar smells sharp.</p>",
        accepted=4,
    ),
    # Question 2: body prose under two content tokens -> tb dropped;
    # accepted answer 9 never appears -> qa unresolved.
    _post_row(
        2, "1", title="Kettle descaling cadence question", body="<p>ok</p>", accepted=9
    ),
    # Question 3: no accepted answer; title equals body -> tb dropped.
    _post_row(3, "1", title="Bicycle chain waxing", body="<p>Bicycle chain waxing</p>"),
    # Answer 4: resolves question 1.
    _post_row(
        4,
        "2",
        body="<p>Acetone smell means the starter is hungry; feed twice daily.</p>",
    ),
    # Answer 5: nobody's accepted answer -> ignored.
    _post_row(5, "2", body="<p>Unclaimed answer body with plenty of prose tokens.</p>"),
]

_COMMENTS = [
    _comment_row(11, 1, "Try feeding rye flour instead tonight"),
    _comment_row(12, 7, "Different post entirely, no pairing here"),
    # Pairs with comment 11 on post 1.
    _comment_row(13, 1, "Rye flour worked, smell gone after two feedings"),
    # Raw newline entity: cleans to empty -> neither pairs nor replaces
    # the predecessor (handcrafted; the builder would double-escape it).
    b'  <row Id="14" PostId="1" Text="&#xA;" />\n',
    # Short side -> dropped by rule, but still becomes the predecessor.
    _comment_row(15, 1, "ok"),
    # Pairs against "ok"? No: left side has under two content tokens ->
    # dropped, and this comment becomes the new predecessor.
    _comment_row(16, 1, "Starter doubled overnight after the rye switch"),
]

_LINKS = [
    _link_row(21, 101, 102, 3),
    _link_row(22, 101, 102, 3),  # exact repeat -> deduped
    _link_row(23, 102, 103, 3),  # merges into the 101 component
    _link_row(24, 205, 206, 3),  # separate component
    _link_row(25, 301, 302, 1),  # related edge
    _link_row(26, 302, 301, 1),  # same edge, reversed -> canonical dedup
    _link_row(27, 401, 402, 4),  # unrelated link type -> ignored
]


def _pairs_payload(module: Any) -> Any:
    census = module.read_site("fixture-site", iter(_POSTS), iter(_COMMENTS))
    return census.payload()


def test_pair_readers_pin_rules_and_reproduce() -> None:
    module = _load("f1_pairs")
    first = _pairs_payload(module)
    second = _pairs_payload(module)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    readers = first["readers"]
    assert readers["tb"]["pairs"] == 1
    assert readers["tb"]["dropped_by_rule"] == 2
    assert readers["qa"]["pairs"] == 1
    assert readers["qa"]["unresolved"] == 1
    assert readers["cm"]["pairs"] == 1
    assert readers["cm"]["dropped_by_rule"] == 2
    assert readers["cm"]["comment_rows"] == 6
    assert first["rows_by_post_type"] == {"1": 3, "2": 2}
    for name in ("tb", "qa", "cm"):
        assert len(readers[name]["stream_sha256"]) == 64


def test_pair_readers_without_comments_member() -> None:
    module = _load("f1_pairs")
    census = module.read_site("fixture-site", iter(_POSTS), None)
    payload = census.payload()
    assert payload["readers"]["cm"] == {"status": "no-member"}


def _gold_payload(module: Any, links: list[bytes]) -> tuple[Any, bytes, bytes]:
    components_out = io.BytesIO()
    related_out = io.BytesIO()
    gold = module.read_site_gold(
        "fixture-site", iter(links), components_out, related_out
    )
    return gold.payload(), components_out.getvalue(), related_out.getvalue()


def test_gold_graph_pins_rules_and_reproduces() -> None:
    module = _load("f1_gold")
    first, components_first, related_first = _gold_payload(module, _LINKS)
    second, components_second, related_second = _gold_payload(module, _LINKS)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert components_first == components_second
    assert related_first == related_second

    assert first["duplicate_edge_rows"] == 4
    assert first["duplicate_edges_deduped"] == 3
    assert first["related_edge_rows"] == 2
    assert first["related_edges_deduped"] == 1
    assert first["components"] == 2
    assert first["posts_in_graph"] == 5
    assert first["component_size_hist"] == {"2": 1, "3": 1}
    # 101-102-103 is one transitive component: three posts, one canonical
    # representative, one split for all of them.
    rows = [line.split(b"\t") for line in components_first.splitlines()]
    big = [row for row in rows if row[2] == b"101"]
    assert sorted(row[1] for row in big) == [b"101", b"102", b"103"]
    assert len({row[3] for row in big}) == 1
    assert related_first == b"fixture-site\t301\t302\n"


def test_gold_split_ignores_edge_order() -> None:
    module = _load("f1_gold")
    reordered = [_LINKS[3], _LINKS[2], _LINKS[0], _LINKS[1], _LINKS[6], _LINKS[4]]
    first, components_first, _ = _gold_payload(module, _LINKS)
    second, components_second, _ = _gold_payload(module, reordered)
    for key in (
        "components",
        "posts_in_graph",
        "component_size_hist",
        "split_components",
        "split_posts",
        "split_duplicate_edges",
        "split_pairs_possible",
    ):
        assert first[key] == second[key]
    assert components_first == components_second
