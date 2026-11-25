"""Fixtures: the hostile families of the engine's memo (unit pyre-9).

Each family is a pattern of the reference, a method and a text recipe
under which plain backtracking costs time that grows faster than the
text: the 33 families of the engine's decision record of 2026-11-19
(``proto_hostile.py``) and three shapes outside the inventory. A row holds
the pattern's text and flags as the reference holds them (found anew at
each generation by the inventory's site scan, ``_pyre_sites``; a family
whose site is gone fails the generation), the method, the recipe by rule
(``head + unit * (n // div) + tail``), and ``re``'s answer at the sizes it
finishes quickly, as a count and the SHA-256 of the answer. A family's
``record`` is the record's ``file:line`` written here as a constant, not
read from the source, so an edit of the reference never moves its bytes.

The decision record names a site by file and line at 3a26051a; the
inventory names it by file and bound name. The mapping, checked against
the record's ``inventory.json`` (which holds the pattern text per id):
owner_prompt.py:99 is ``_ENVELOPES`` (its ``system-reminder`` member),
durability.py:761 ``_DRIFTING_COUNT_RE``, :766 ``_TEST_TOTAL_RE``, :685
``_ASSISTANT_SAID_RE``, :698 ``_TODO_HEADING_RE``, :741 the third
``_RELATIVE_DATE_PATTERNS``, supersession.py:253 ``_PROPER_NOUN_RE``,
service_units.py:405 the ``state`` member of ``_LAUNCHCTL_FIELDS``,
cli/completion_cmd.py:191 ``_summary.text``, search_lexicon.py:1013
``_spelling_tables.fts5``, credentials.py:238 ``_GENERIC_KEYWORD_RE``,
:262 ``_URI_USERINFO_RE``, :442 the second ``_looks_like_secret`` pattern,
verify_candidates.py:87 ``_BACKTICK_RE``, :107 ``_BARE_RE``, :184
``_DOMAIN_ROUTE_RE``, :238 ``_RELATIVE_CITATION_RE``, symbols.py:152
``_SYMBOL_IN_MODULE``, handlers/write_gates.py:50 ``_SENTENCE_SPLIT_RE``,
:56 ``_LIST_PREFIX_RE``, :59 ``_PREFERENCE_RE``, :321 ``_USER_CLAIM_RE``,
store_schema.py:1183 ``_V8_MEMORY_NAME``, machine.py:202
``_darwin_hardware.found``.

Every text is ASCII, so no answer reads past ASCII in the Unicode tables
(a folded literal matches a non-ASCII character only in a non-ASCII
text), and no answer follows the Python release: the file is not gated.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ._common import jsonl
from .gen_pyre_inventory import _sites, _spelled

#: The sizes ``re`` finishes quickly on every family.
SIZES = (250, 500, 1000, 2000)
#: The three shapes outside the inventory are exponential for ``re``.
EXTRA_SIZES = (12, 16, 20)

STATEMENT = "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("

#: (record id, inventory site, member prefix, method, head, unit, div,
#: tail, note)
FAMILIES: list[tuple[str, str, str, str, str, str, int, str, str]] = [
    (
        "owner_prompt.py:99",
        "owner_prompt.py:_ENVELOPES",
        "<system",
        "sub",
        "",
        "<system-reminder>",
        17,
        "",
        "openers, never closed",
    ),
    (
        "owner_prompt.py:99",
        "owner_prompt.py:_ENVELOPES",
        "<system",
        "sub",
        "",
        "<system-reminder ",
        17,
        "",
        "openers without '>'",
    ),
    (
        "durability.py:766",
        "durability.py:_TEST_TOTAL_RE",
        "",
        "finditer",
        "",
        "1,",
        2,
        "",
        "digit-comma run",
    ),
    (
        "durability.py:761",
        "durability.py:_DRIFTING_COUNT_RE",
        "",
        "finditer",
        "",
        "1.",
        2,
        "",
        "digit-dot run",
    ),
    (
        "service_units.py:405",
        "service_units.py:_LAUNCHCTL_FIELDS",
        "",
        "search",
        "",
        "\n",
        1,
        "",
        "blank lines",
    ),
    (
        "cli/completion_cmd.py:191",
        "cli/completion_cmd.py:_summary.text",
        "",
        "sub",
        "",
        "%(",
        2,
        "",
        "'%(' never closed",
    ),
    (
        "supersession.py:253",
        "supersession.py:_PROPER_NOUN_RE",
        "",
        "match",
        "A",
        "a",
        1,
        "!",
        "one long word, then '!'",
    ),
    (
        "search_lexicon.py:1013",
        "search_lexicon.py:_spelling_tables.fts5",
        "",
        "search",
        "",
        STATEMENT,
        59,
        "",
        "statement heads, never closed",
    ),
    (
        "credentials.py:238",
        "credentials.py:_GENERIC_KEYWORD_RE",
        "",
        "finditer",
        "",
        "a_",
        2,
        "",
        "prefix words",
    ),
    (
        "credentials.py:238",
        "credentials.py:_GENERIC_KEYWORD_RE",
        "",
        "finditer",
        "",
        "password" + " " * 40,
        48,
        "",
        "keyword then spaces",
    ),
    (
        "credentials.py:238",
        "credentials.py:_GENERIC_KEYWORD_RE",
        "",
        "finditer",
        "password is ",
        "is ",
        3,
        "",
        "keyword, then 'is' repeated",
    ),
    (
        "credentials.py:262",
        "credentials.py:_URI_USERINFO_RE",
        "",
        "finditer",
        "ab://c:",
        "x",
        1,
        "",
        "userinfo never closed",
    ),
    (
        "verify_candidates.py:107",
        "verify_candidates.py:_BARE_RE",
        "",
        "finditer",
        " /a",
        " Aa",
        3,
        "",
        "capitalised words, no separator",
    ),
    (
        "verify_candidates.py:107",
        "verify_candidates.py:_BARE_RE",
        "",
        "finditer",
        "",
        " /a" + " Aa" * 20,
        63,
        "",
        "the same, many starts",
    ),
    (
        "verify_candidates.py:238",
        "verify_candidates.py:_RELATIVE_CITATION_RE",
        "",
        "finditer",
        "",
        "a.",
        2,
        "",
        "dotted run",
    ),
    (
        "verify_candidates.py:238",
        "verify_candidates.py:_RELATIVE_CITATION_RE",
        "",
        "finditer",
        "",
        " a/",
        3,
        "aaaaa",
        "folders without a file",
    ),
    (
        "verify_candidates.py:184",
        "verify_candidates.py:_DOMAIN_ROUTE_RE",
        "",
        "finditer",
        "",
        "a.",
        2,
        "",
        "labels without a slash",
    ),
    (
        "verify_candidates.py:184",
        "verify_candidates.py:_DOMAIN_ROUTE_RE",
        "",
        "finditer",
        "",
        "a-",
        2,
        "",
        "hyphen run",
    ),
    (
        "symbols.py:152",
        "symbols.py:_SYMBOL_IN_MODULE",
        "",
        "finditer",
        "`a`",
        " ",
        1,
        "",
        "symbol then spaces",
    ),
    (
        "symbols.py:152",
        "symbols.py:_SYMBOL_IN_MODULE",
        "",
        "finditer",
        "",
        "`a` b ",
        6,
        "",
        "symbol and filler, no 'in'",
    ),
    (
        "durability.py:685",
        "durability.py:_ASSISTANT_SAID_RE",
        "",
        "finditer",
        "the assistant",
        " x",
        2,
        "",
        "opener then words",
    ),
    (
        "durability.py:685",
        "durability.py:_ASSISTANT_SAID_RE",
        "",
        "finditer",
        "",
        "the assistant ",
        14,
        "",
        "opener repeated",
    ),
    (
        "handlers/write_gates.py:59",
        "handlers/write_gates.py:_PREFERENCE_RE",
        "",
        "finditer",
        "my ",
        "a ",
        2,
        "",
        "anchored branch, words",
    ),
    (
        "handlers/write_gates.py:321",
        "handlers/write_gates.py:_USER_CLAIM_RE",
        "",
        "search",
        "the user's ",
        "a",
        1,
        "",
        "one long word",
    ),
    (
        "handlers/write_gates.py:321",
        "handlers/write_gates.py:_USER_CLAIM_RE",
        "",
        "search",
        "",
        "Aa ",
        3,
        "",
        "capitalised words",
    ),
    (
        "handlers/write_gates.py:50",
        "handlers/write_gates.py:_SENTENCE_SPLIT_RE",
        "",
        "finditer",
        "",
        ". ",
        2,
        "",
        "sentence ends",
    ),
    (
        "handlers/write_gates.py:56",
        "handlers/write_gates.py:_LIST_PREFIX_RE",
        "",
        "sub",
        "",
        "- ",
        2,
        "x",
        "list markers",
    ),
    (
        "durability.py:698",
        "durability.py:_TODO_HEADING_RE",
        "",
        "finditer",
        "",
        " " * 30 + "\n",
        31,
        "",
        "indented blank lines",
    ),
    (
        "durability.py:741",
        "durability.py:_RELATIVE_DATE_PATTERNS#3",
        "",
        "finditer",
        "1",
        " ",
        1,
        "",
        "number then spaces",
    ),
    (
        "store_schema.py:1183",
        "store_schema.py:_V8_MEMORY_NAME",
        "",
        "match",
        "",
        "-" + "A" * 25,
        26,
        "",
        "near misses of the id",
    ),
    (
        "credentials.py:442",
        "credentials.py:_looks_like_secret.-#2",
        "",
        "fullmatch",
        "",
        "aaaa/",
        5,
        "!",
        "segments then '!'",
    ),
    (
        "verify_candidates.py:87",
        "verify_candidates.py:_BACKTICK_RE",
        "",
        "finditer",
        "`",
        "a",
        1,
        "",
        "backtick never closed",
    ),
    (
        "machine.py:202",
        "machine.py:_darwin_hardware.found",
        "",
        "search",
        '"IOPlatformUUID" = "',
        "x",
        1,
        "",
        "value never closed",
    ),
]

#: (pattern, flags, method, head, unit, div, tail, note)
EXTRA: list[tuple[str, int, str, str, str, int, str, str]] = [
    (r"(?:a|aa)+b", 0, "search", "", "a", 1, "", "exponential for re"),
    (r"(?:\w+\s?)+$", 0, "search", "", "a", 1, "!", "exponential for re"),
    # After the empty match at 0 the attempt that must advance tries every
    # way to split the run of ``a``.
    (
        r"(?:a|aa)*?(?<!a)",
        0,
        "finditer",
        "",
        "a",
        1,
        "",
        "exponential after an empty match",
    ),
]


def recipe(head: str, unit: str, div: int, tail: str, n: int) -> str:
    return head + unit * (n // div) + tail


def answer(c: re.Pattern[str], method: str, text: str) -> tuple[int, str]:
    """The count and the SHA-256 of the answer: the spans of every match
    (``finditer``), the span or nothing (``match``, ``fullmatch``,
    ``search``), the text and the count of ``subn`` with ``-`` (``sub``)."""
    if method == "sub":
        out, count = c.subn("-", text)
        body = f"{out}|{count}"
    elif method == "finditer":
        spans = [m.span() for m in c.finditer(text)]
        count = len(spans)
        body = ";".join(f"{a},{b}" for a, b in spans)
    else:
        m = getattr(c, method)(text)
        count = 0 if m is None else 1
        body = "-" if m is None else "{},{}".format(*m.span())
    return count, hashlib.sha256(body.encode("utf-8")).hexdigest()


def _row(
    part: str,
    pattern: str,
    flags: int,
    method: str,
    head: str,
    unit: str,
    div: int,
    tail: str,
    note: str,
    sizes: tuple[int, ...],
) -> dict[str, Any]:
    c = re.compile(pattern, flags)
    answers = {}
    for n in sizes:
        count, sha = answer(c, method, recipe(head, unit, div, tail, n))
        answers[str(n)] = {"count": count, "sha256": sha}
    return {
        "part": part,
        "pattern": _spelled([ord(ch) for ch in pattern]),
        "flags": flags,
        "method": method,
        "recipe": {
            "head": _spelled([ord(ch) for ch in head]),
            "unit": _spelled([ord(ch) for ch in unit]),
            "div": div,
            "tail": _spelled([ord(ch) for ch in tail]),
        },
        "note": note,
        "answers": answers,
    }


def rows() -> list[dict[str, Any]]:
    sites = {s.id: s for s in _sites()}
    out = []
    for record, site_id, member, method, head, unit, div, tail, note in FAMILIES:
        site = sites.get(site_id)
        assert site is not None, f"the site of family {record} is gone: {site_id}"
        found = [
            (p, f)
            for p, f in site.instances
            if isinstance(p, str) and p.startswith(member)
        ]
        assert found, f"no member {member!r} at {site_id}"
        pattern, flags = found[0]
        row = _row(
            "memo.family", pattern, flags, method, head, unit, div, tail, note, SIZES
        )
        out.append({"record": record, "site": site_id, **row})
    for pattern, flags, method, head, unit, div, tail, note in EXTRA:
        out.append(
            _row(
                "memo.extra",
                pattern,
                flags,
                method,
                head,
                unit,
                div,
                tail,
                note,
                EXTRA_SIZES,
            )
        )
    return out


def generate() -> dict[str, bytes]:
    body = rows()
    head = {"part": "header", "fixture": "pyre_memo", "rows": len(body)}
    return {"pyre_memo.jsonl": jsonl([head, *body])}
