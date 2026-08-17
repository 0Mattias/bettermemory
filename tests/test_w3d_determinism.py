"""G3's CI leg for W3-D: the reader reproduces itself byte-for-byte.

The determinism bar of `bench/w/W3D_DECLARATION.md` §7 has a CI half:
the page parser, the four extraction rule steps, and the census run
twice over committed synthetic page blocks — hand-written XML below,
no corpus bytes — and must produce byte-identical edge output and an
identical census payload on every push. The fixture also pins the
declared rule itself: the ns and redirect gates, both title gates, the
English-section bound, relation-section levels, inline synonym
templates, the three-gloss cap, template stripping, piped links, the
lead-sentence finder and its skip prefixes, self-edge dropping, tuple
dedup, and the floors — so a drift in any of them fails here before
it can silently reshape a census.
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
        "w3d_edges", _ROOT / "bench" / "w" / "w3d_edges.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["w3d_edges"] = module
    spec.loader.exec_module(module)
    return module


def _page(title: str, ns: int, text: str, redirect: bool = False) -> bytes:
    redirect_line = '    <redirect title="elsewhere" />\n' if redirect else ""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped_title = title.replace("&", "&amp;")
    return (
        "  <page>\n"
        f"    <title>{escaped_title}</title>\n"
        f"    <ns>{ns}</ns>\n"
        f"{redirect_line}"
        "    <revision>\n"
        f'      <text bytes="{len(text)}">{escaped}</text>\n'
        "    </revision>\n"
        "  </page>\n"
    ).encode()


BATTERY_TEXT = """==English==

===Noun===
# {{lb|en|electronics}} A [[device]] that stores energy for [[charging|charging things]], supplying [[power]].
# A [[battery]] of artillery guns.
#: An example line that is not a gloss.
## A sub-sense line that is not a gloss.
# A third gloss about [[assault]] charges.
# A fourth gloss carrying [[zebrafish]] beyond the cap.
{{syn|en|powercell|q=informal}}

====Synonyms====
* {{l|en|cell}}, [[accumulator]]

==French==

===Noun===
# A french-only line with [[frenchonly]].
"""

GIBSON_WIKT_TEXT = """==English==

===Proper noun===
# A brand of [[guitar]].
"""

GIBSON_SIMPLE_TEXT = """{{Infobox company
| name = Gibson
}}
'''Gibson''' is an [[United States|American]] company that makes [[guitar]]s. It is based in [[Nashville]].
"""

WIKTIONARY_STREAM: list[bytes] = [
    b"<mediawiki>\n",
    *_page("battery", 0, BATTERY_TEXT).splitlines(keepends=True),
    *_page("Gibson", 0, GIBSON_WIKT_TEXT).splitlines(keepends=True),
    # Multi-word title: fails the single-word gate.
    *_page("United States", 0, "==English==\n# A [[country]].\n").splitlines(
        keepends=True
    ),
    # Filler-stem head: gated out by the tokenizer.
    *_page("guess", 0, "==English==\n# A [[conjecture]].\n").splitlines(
        keepends=True
    ),
    # Talk namespace: filtered before parsing.
    *_page("battery", 1, "==English==\n# A [[talkpage]] note.\n").splitlines(
        keepends=True
    ),
    # Redirect: filtered.
    *_page("cell", 0, "#REDIRECT [[battery]]", redirect=True).splitlines(keepends=True),
    # No English section: gated out.
    *_page("gato", 0, "==Spanish==\n# A [[cat]].\n").splitlines(keepends=True),
    b"</mediawiki>\n",
]

SIMPLEWIKI_STREAM: list[bytes] = [
    b"<mediawiki>\n",
    *_page("Gibson (guitar company)", 0, GIBSON_SIMPLE_TEXT).splitlines(keepends=True),
    # Two-token title after parenthetical stripping: gated out.
    *_page("United States", 0, "The [[United States]] is a country.\n").splitlines(
        keepends=True
    ),
    *_page("Fender", 0, "'''Fender''' makes [[guitar]]s.\n").splitlines(keepends=True),
    b"</mediawiki>\n",
]


def _run_once(reader: Any) -> tuple[bytes, str]:
    out = io.BytesIO()
    census = reader.W3DCensus()
    for page in reader.iter_pages(iter(WIKTIONARY_STREAM)):
        if page.ns != 0 or page.redirect:
            continue
        term_labels = reader.wiktionary_page_edges(page.title, page.text)
        if term_labels:
            census.add_page(page.title.lower(), term_labels, "wiktionary", out)
    for page in reader.iter_pages(iter(SIMPLEWIKI_STREAM)):
        if page.ns != 0 or page.redirect:
            continue
        result = reader.simplewiki_page_edges(page.title, page.text)
        if result is not None:
            head, term_labels = result
            if term_labels:
                census.add_page(head, term_labels, "simplewiki", out)
    return out.getvalue(), json.dumps(census.counts_payload(), sort_keys=True)


def test_reader_reproduces_itself_and_pins_the_rule() -> None:
    reader = _load_reader()
    edges_a, payload_a = _run_once(reader)
    edges_b, payload_b = _run_once(reader)
    assert edges_a == edges_b, "edge output is not byte-stable"
    assert payload_a == payload_b, "census payload is not stable"

    rows = [line.split("\t") for line in edges_a.decode().splitlines()]
    by_head_term_label = {(r[0], r[1], r[2]): (r[3], int(r[4])) for r in rows}
    heads = [r[0] for r in rows]

    # Rule step 3: gloss links and gloss tokens from the battery page.
    assert by_head_term_label[("battery", "charging", "gloss-link")][0] == "wiktionary"
    assert ("battery", "power", "gloss-link") in by_head_term_label
    assert ("battery", "charging", "gloss") in by_head_term_label
    assert ("battery", "power", "gloss") in by_head_term_label
    # The {{lb|...}} template is stripped before tokens are taken.
    assert ("battery", "electronics", "gloss") not in by_head_term_label
    # Rule steps 1-2: relation section and inline synonyms.
    assert ("battery", "cell", "synonyms") in by_head_term_label
    assert ("battery", "accumulator", "synonyms") in by_head_term_label
    assert ("battery", "powercell", "synonyms") in by_head_term_label
    # The three-gloss cap, the sub-sense and example exclusions, the
    # English-section bound, and the self-edge drop.
    assert ("battery", "zebrafish", "gloss-link") not in by_head_term_label
    assert ("battery", "frenchonly", "gloss-link") not in by_head_term_label
    assert ("battery", "battery", "gloss-link") not in by_head_term_label
    assert ("battery", "assault", "gloss-link") in by_head_term_label
    # Gated pages contribute nothing.
    assert "united" not in heads and "guess" not in heads and "gato" not in heads
    # Rule step 4: the lead sentence, bounded at the first ". ".
    assert ("gibson", "guitar", "lead-link") in by_head_term_label
    assert by_head_term_label[("gibson", "guitar", "lead-link")][0] == "simplewiki"
    assert ("gibson", "american", "lead") in by_head_term_label
    assert not any(r[1] == "nashville" for r in rows)

    payload = json.loads(payload_a)
    assert payload["pages_with_edges"] == {"simplewiki": 2, "wiktionary": 2}
    volume = payload["edge_volume"]
    assert volume["distinct_total"] == len({(r[0], r[1]) for r in rows})
    assert set(volume["distinct_by_source"]) == {"wiktionary", "simplewiki"}
    assert "synonyms" in volume["distinct_by_label"]
    assert "lead-link" in volume["distinct_by_label"]

    # Need support: battery<->charging/power and guitar<->gibson.
    battery_need = payload["needs"]["09d032c9"]
    assert battery_need["count"] == 4
    assert battery_need["supported"] is True
    guitar_need = payload["needs"]["95228167"]
    attestations = {tuple(a) for a in guitar_need["attestations"]}
    assert ("gibson", "guitar", "gloss-link", "wiktionary") in attestations
    assert ("gibson", "guitar", "gloss", "wiktionary") in attestations
    assert ("gibson", "guitar", "lead-link", "simplewiki") in attestations
    assert ("fender", "guitar", "lead-link", "simplewiki") in attestations
    assert guitar_need["count"] == 4
    assert guitar_need["supported"] is True

    # The floors as declared; the fixture parks on V.
    assert payload["floors"]["V"]["threshold_edges"] == 250_000
    assert payload["floors"]["C"]["threshold_needs"] == 4
    assert payload["floors"]["C"]["attestations_per_need"] == 2
    assert payload["floors"]["C"]["holds"] is False
    assert payload["g0_verdict"] == "PARK-AT-CENSUS"


def test_page_parser_gates_and_self_closed_text() -> None:
    reader = _load_reader()
    stream = [
        b"<mediawiki>\n",
        b"  <page>\n",
        b"    <title>empty</title>\n",
        b"    <ns>0</ns>\n",
        b'    <text bytes="0" />\n',
        b"  </page>\n",
        *_page("battery", 0, "==English==\n# A [[device]].\n").splitlines(
            keepends=True
        ),
        *_page("cell", 0, "#REDIRECT [[battery]]", redirect=True).splitlines(
            keepends=True
        ),
        b"</mediawiki>\n",
    ]
    pages = list(reader.iter_pages(iter(stream)))
    assert [(p.title, p.ns, p.redirect) for p in pages] == [
        ("empty", 0, False),
        ("battery", 0, False),
        ("cell", 0, True),
    ]
    assert pages[0].text == ""
    assert "[[device]]" in pages[1].text


def test_lead_sentence_finder_skips_scaffolding() -> None:
    reader = _load_reader()
    assert (
        reader.lead_sentence(GIBSON_SIMPLE_TEXT)
        == "'''Gibson''' is an [[United States|American]] company that makes"
        " [[guitar]]s."
    )
    assert reader.lead_sentence("{{stub}}\n== Heading ==\n") is None
