"""Deterministic corpus readers for the W1 trainer.

The read side of the W1 declaration §2: every function here is
a pure, order-stable stream over the pinned register bytes
(`bench/w/corpora.json`), so a retrain sees byte-identical input in
byte-identical order. Nothing here touches the network; the register's
local paths are the only sources. Nothing here touches the engine
either — the tokenizer below is the TRAINER'S, deliberately separate
from `search.py`'s stemmer contract.

Register read order is the declaration's "deterministic prefix in
register order": the enwiki part-file first, then the Gutenberg books
in ascending id order. The 50 M-token cap therefore lands wherever it
lands — measured on the pinned bytes, the enwiki part alone exceeds
it, so the Gutenberg books contribute no training tokens in W1. That
is the declared rule executing, not a choice made at train time; the
run artifact records the actual composition.

The wikitext strip is deliberately modest: templates, tables, refs,
tags, link markup, and headings are removed with bounded, order-fixed
rewrites; what survives is prose with a tolerable residue of markup
junk. Rare junk tokens fall below the trainer's min-count floor, so
the strip needs to be deterministic and decent, not perfect. The
Project Gutenberg strip cuts at the standard START/END boilerplate
markers, honoring the register's no-trademark-carried note.
"""

from __future__ import annotations

import bz2
import html
import json
import re
from collections.abc import Iterator
from pathlib import Path

W_DIR = Path(__file__).parent
CORPUS_DIR = W_DIR / "corpus"
REGISTER = W_DIR / "corpora.json"

# --- tokenizer (the trainer's own; declaration §2) ---------------------

_TOKEN = re.compile(r"[a-z0-9]+")
_MIN_LEN = 2
_MAX_LEN = 30


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric runs, keep length 2-30."""
    return [t for t in _TOKEN.findall(text.lower()) if _MIN_LEN <= len(t) <= _MAX_LEN]


# --- wikitext -> prose --------------------------------------------------

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_REF_PAIR = re.compile(r"<ref[^>/]*>.*?</ref>", re.S | re.I)
_REF_SELF = re.compile(r"<ref[^>]*/>", re.I)
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_TEMPLATE_INNER = re.compile(r"\{\{[^{}]*\}\}")
_TABLE = re.compile(r"\{\|.*?\|\}", re.S)
_LINK_INNER = re.compile(r"\[\[([^\[\]]*)\]\]")
_EXTERNAL_LINK = re.compile(r"\[https?://[^\s\]]+\s+([^\]]*)\]")
_BARE_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^=+\s*(.*?)\s*=+\s*$", re.M)
_LIST_MARK = re.compile(r"^[*#:;]+\s*", re.M)
_QUOTES = re.compile(r"'{2,}")
# Namespaced [[...]] targets that are markup, not prose. Lowercased
# prefix match; the common namespaces in article wikitext.
_LINK_DROP_PREFIXES = ("file:", "image:", "category:", "wikipedia:", "template:")
# Bounded innermost-first rewriting: real articles nest templates and
# links a handful of levels deep; the bound only exists so malformed
# markup cannot loop the stripper.
_MAX_NEST_PASSES = 12


def _replace_link(match: re.Match[str]) -> str:
    inner = match.group(1)
    if inner.lower().startswith(_LINK_DROP_PREFIXES):
        return " "
    return inner.rsplit("|", 1)[-1]


def strip_wikitext(raw: str) -> str:
    """Markup-escaped dump text for one article -> plain-ish prose."""
    text = html.unescape(raw)
    text = _COMMENT.sub(" ", text)
    text = _REF_PAIR.sub(" ", text)
    text = _REF_SELF.sub(" ", text)
    text = _TAG.sub(" ", text)
    for _ in range(_MAX_NEST_PASSES):
        text, n = _TEMPLATE_INNER.subn(" ", text)
        if not n:
            break
    text = _TABLE.sub(" ", text)
    for _ in range(_MAX_NEST_PASSES):
        text, n = _LINK_INNER.subn(_replace_link, text)
        if not n:
            break
    text = _EXTERNAL_LINK.sub(r"\1", text)
    text = _BARE_URL.sub(" ", text)
    text = _HEADING.sub(r"\1", text)
    text = _LIST_MARK.sub("", text)
    text = _QUOTES.sub("", text)
    return text


def iter_enwiki_articles(path: Path) -> Iterator[str]:
    """Stripped prose of each main-namespace, non-redirect article.

    Line-oriented scan of the dump XML: safe because the dump escapes
    every ``<`` inside article text as ``&lt;``, so literal
    ``<text``/``</text>`` markers only ever occur as real structure.
    Dump order is the stream order — the determinism the declaration
    needs — and multistream bz2 is handled transparently by
    ``bz2.open`` reading members back to back.
    """
    in_page = False
    in_text = False
    ns_zero = False
    redirect = False
    buf: list[str] = []
    with bz2.open(path, "rt", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if in_text:
                end = line.find("</text>")
                if end < 0:
                    buf.append(line)
                    continue
                buf.append(line[:end])
                in_text = False
                if ns_zero and not redirect:
                    yield strip_wikitext("".join(buf))
                buf.clear()
                continue
            stripped = line.lstrip()
            if stripped.startswith("<page>"):
                in_page, ns_zero, redirect = True, False, False
            elif not in_page:
                continue
            elif stripped.startswith("<ns>"):
                ns_zero = stripped.startswith("<ns>0</ns>")
            elif stripped.startswith("<redirect"):
                redirect = True
            elif stripped.startswith("<text"):
                head = line.split(">", 1)
                if len(head) != 2:
                    continue
                body = head[1]
                end = body.find("</text>")
                if end >= 0:
                    if ns_zero and not redirect:
                        yield strip_wikitext(body[:end])
                else:
                    in_text = True
                    buf.append(body)
            elif stripped.startswith("</page>"):
                in_page = False


# --- Project Gutenberg -> prose ----------------------------------------

_PG_START = re.compile(r"\*\*\* ?START OF.*$", re.M)
_PG_END = re.compile(r"\*\*\* ?END OF.*$", re.M)


def strip_gutenberg(raw: str) -> str:
    """Text between the standard PG START/END boilerplate markers."""
    start = _PG_START.search(raw)
    end = _PG_END.search(raw)
    lo = start.end() if start else 0
    hi = end.start() if end else len(raw)
    return raw[lo:hi]


def iter_gutenberg_books(directory: Path) -> Iterator[str]:
    """Stripped prose of each admitted book, ascending id order."""
    paths = sorted(
        directory.glob("pg*.txt"), key=lambda p: int(p.stem.removeprefix("pg"))
    )
    for path in paths:
        yield strip_gutenberg(path.read_text(encoding="utf-8", errors="replace"))


# --- the register stream ------------------------------------------------


def register_paths() -> tuple[Path, Path]:
    """(enwiki part-file, gutenberg directory) from the pinned register."""
    register = json.loads(REGISTER.read_text())
    by_name = {c["name"]: c for c in register["corpora"]}
    enwiki = by_name["enwiki-20260801-pages-articles-part1"]
    gutenberg = by_name["gutenberg-curated-2026-08-15"]
    root = REGISTER.parent.parent.parent
    return root / enwiki["local_path"], root / gutenberg["local_path"]


def iter_register_tokens(cap: int) -> Iterator[list[str]]:
    """Token lists per document, register order, stopping at ``cap``.

    The cap counts tokens actually emitted; the final document is
    truncated exactly at the cap so a retrain replays the identical
    prefix. Documents are the windowing boundary downstream — training
    windows never cross a document edge.
    """
    remaining = cap
    enwiki_path, gutenberg_dir = register_paths()
    streams: tuple[Iterator[str], ...] = (
        iter_enwiki_articles(enwiki_path),
        iter_gutenberg_books(gutenberg_dir),
    )
    for stream in streams:
        for document in stream:
            tokens = tokenize(document)
            if not tokens:
                continue
            if len(tokens) >= remaining:
                yield tokens[:remaining]
                return
            remaining -= len(tokens)
            yield tokens
