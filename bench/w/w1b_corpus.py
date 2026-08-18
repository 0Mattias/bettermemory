"""Deterministic corpus slice for the W1b retrain.

The read side of `bench/w/W1B_DECLARATION.md` §3: the per-source token
budgets, the concatenation order, the Stack Exchange Posts reader, and
the derived token cache. Everything streams from the pinned register
bytes (`bench/w/corpora.json`); nothing here touches the network. The
wiki-family sources ride W1's committed article reader and wikitext
strip unchanged; the Stack Exchange sources stream `Posts.xml` from
the pinned `.7z` via ``bsdtar``, the idiom `w3p_pairs.py` established.

The cache is the declaration's derived intermediate: one streaming
pass over the slice writes tokenized documents (one per line,
space-joined) under ``bench/w/corpus/derived/``, and the trainer's
vocabulary and materialization passes read the cache instead of
re-decompressing the archives. The cache is a pure function of the
pinned bytes and this committed code; its sha256 lands in the run
meta, and each G3 retrain rebuilds it independently.

Budget semantics, per the declaration: each source contributes a
deterministic prefix of its stream up to its token cap; a source
smaller than its cap contributes its whole stream; the final document
at a cap boundary is truncated exactly at the cap; the 200M global cap
binds regardless.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from os import replace as _atomic_replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from w1_corpus import (  # noqa: E402
    CORPUS_DIR,
    REGISTER,
    iter_enwiki_articles,
    iter_gutenberg_books,
    tokenize,
)

# Declaration §3: per-source token budgets, in concatenation order.
# The casual-technical half first, then the breadth half.
SLICE: tuple[tuple[str, int], ...] = (
    ("stackoverflow-posts-archive", 80_000_000),
    ("superuser-archive", 10_000_000),
    ("apple-stackexchange-archive", 5_000_000),
    ("android-stackexchange-archive", 5_000_000),
    ("enwiki-parts-1-71", 50_000_000),
    ("simplewiki-20260801-pages-articles-multistream", 10_000_000),
    ("enwiktionary-20260801-pages-articles", 5_000_000),
    ("gutenberg-curated-2026-08-15", 9_500_000),
    ("academia-stackexchange-archive", 1_700_000),
    ("beer-stackexchange-archive", 1_700_000),
    ("coffee-stackexchange-archive", 1_700_000),
    ("cooking-stackexchange-archive", 1_700_000),
    ("diy-stackexchange-archive", 1_700_000),
    ("fitness-stackexchange-archive", 1_700_000),
    ("gardening-stackexchange-archive", 1_700_000),
    ("interpersonal-stackexchange-archive", 1_700_000),
    ("lifehacks-stackexchange-archive", 1_700_000),
    ("movies-stackexchange-archive", 1_700_000),
    ("music-stackexchange-archive", 1_700_000),
    ("outdoors-stackexchange-archive", 1_700_000),
    ("parenting-stackexchange-archive", 1_700_000),
    ("pets-stackexchange-archive", 1_700_000),
    ("travel-stackexchange-archive", 1_700_000),
)
GLOBAL_CAP = 200_000_000
CACHE_PATH = CORPUS_DIR / "derived" / "w1b-tokens.txt"

# --- Stack Exchange Posts.xml -> documents -----------------------------

_ROW_TYPE = re.compile(r'\bPostTypeId="(\d+)"')
_ROW_TITLE = re.compile(r'\bTitle="([^"]*)"')
_ROW_BODY = re.compile(r'\bBody="([^"]*)"')
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_WS = re.compile(r"\s+")


def se_row_doc(line: str) -> str | None:
    """One ``Posts.xml`` row -> the document W1b trains on, or None.

    Question rows (PostTypeId 1) contribute title plus body; answer
    rows (PostTypeId 2) contribute body; every other row type is
    skipped. The attribute value is XML-escaped HTML: one unescape
    yields markup, the tag strip removes it, and a second unescape
    resolves entities the markup itself carried.
    """
    if "<row " not in line:
        return None
    kind = _ROW_TYPE.search(line)
    if kind is None or kind.group(1) not in ("1", "2"):
        return None
    parts: list[str] = []
    if kind.group(1) == "1":
        title = _ROW_TITLE.search(line)
        if title is not None:
            parts.append(title.group(1))
    body = _ROW_BODY.search(line)
    if body is not None:
        parts.append(body.group(1))
    if not parts:
        return None
    text = html.unescape(" ".join(parts))
    text = _HTML_TAG.sub(" ", text)
    text = html.unescape(text)
    return _WS.sub(" ", text).strip()


def iter_se_docs(archive: Path) -> Iterator[str]:
    """Post documents from a pinned SE archive, document order.

    A caller that stops early (the budget rule) closes the generator;
    ``bsdtar`` is terminated without a returncode check, because the
    early stop is deliberate. A stream read to exhaustion DOES check
    the returncode — silent truncation of a source that was meant to
    be read whole would be a corpus bug, not a budget.
    """
    proc = subprocess.Popen(
        ["bsdtar", "-xOf", str(archive), "Posts.xml"],
        stdout=subprocess.PIPE,
        bufsize=1 << 22,
    )
    assert proc.stdout is not None
    exhausted = False
    try:
        for raw in proc.stdout:
            doc = se_row_doc(raw.decode("utf-8", errors="replace"))
            if doc:
                yield doc
        exhausted = True
    finally:
        proc.stdout.close()
        if exhausted:
            if proc.wait() != 0:
                raise RuntimeError(f"bsdtar exited {proc.returncode} on {archive}")
        else:
            proc.terminate()
            proc.wait()


# --- wiki part enumeration ---------------------------------------------

_FIRST_PAGE = re.compile(r"-p(\d+)p\d+\.bz2$")
_MULTISTREAM = re.compile(r"multistream(\d+)\.xml")


def _first_page(path: Path) -> int:
    match = _FIRST_PAGE.search(path.name)
    if match is None:
        raise ValueError(f"unparseable dump part name: {path.name}")
    return int(match.group(1))


def _register_entries() -> tuple[dict[str, dict[str, object]], Path]:
    register = json.loads(REGISTER.read_text())
    by_name = {c["name"]: c for c in register["corpora"]}
    return by_name, REGISTER.parent.parent.parent


def _enwiki_paths() -> list[Path]:
    """Parts 1-71 in ascending (multistream index, first page id) order."""
    by_name, root = _register_entries()
    part1 = root / str(by_name["enwiki-20260801-pages-articles-part1"]["local_path"])
    parts_dir = root / str(
        by_name["enwiki-20260801-pages-articles-parts2-71"]["local_path"]
    )

    def key(path: Path) -> tuple[int, int]:
        stream = _MULTISTREAM.search(path.name)
        if stream is None:
            raise ValueError(f"unparseable dump part name: {path.name}")
        return int(stream.group(1)), _first_page(path)

    return [part1, *sorted(parts_dir.glob("*.bz2"), key=key)]


def _iter_source_docs(name: str) -> Iterator[str]:
    """The named source's document stream, declared internal order."""
    by_name, root = _register_entries()
    if name == "enwiki-parts-1-71":
        for path in _enwiki_paths():
            yield from iter_enwiki_articles(path)
    elif name == "gutenberg-curated-2026-08-15":
        yield from iter_gutenberg_books(root / str(by_name[name]["local_path"]))
    elif name == "enwiktionary-20260801-pages-articles":
        parts_dir = root / str(by_name[name]["local_path"])
        for path in sorted(parts_dir.glob("*.bz2"), key=_first_page):
            yield from iter_enwiki_articles(path)
    elif name == "simplewiki-20260801-pages-articles-multistream":
        yield from iter_enwiki_articles(root / str(by_name[name]["local_path"]))
    elif name.endswith("-archive"):
        yield from iter_se_docs(root / str(by_name[name]["local_path"]))
    else:
        raise ValueError(f"unknown slice source: {name}")


# --- the token cache ----------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 26), b""):
            digest.update(block)
    return digest.hexdigest()


def build_cache(
    cache_path: Path = CACHE_PATH,
    slice_spec: tuple[tuple[str, int], ...] = SLICE,
    global_cap: int = GLOBAL_CAP,
) -> dict[str, object]:
    """Stream the declared slice once; write the token cache; return meta.

    ``slice_spec``/``global_cap`` are parameters only so the smoke path
    can exercise every reader cheaply; the trainer always passes the
    declared defaults, and the run meta records what was used.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_name(cache_path.name + ".tmp")
    per_source: dict[str, int] = {}
    total = 0
    with tmp.open("w", encoding="utf-8") as out:
        for name, budget in slice_spec:
            remaining = min(budget, global_cap - total)
            got = 0
            if remaining > 0:
                for doc in _iter_source_docs(name):
                    tokens = tokenize(doc)
                    if not tokens:
                        continue
                    if len(tokens) >= remaining:
                        out.write(" ".join(tokens[:remaining]) + "\n")
                        got += remaining
                        break
                    out.write(" ".join(tokens) + "\n")
                    got += len(tokens)
                    remaining -= len(tokens)
            per_source[name] = got
            total += got
            print(
                f"  cache: {name} +{got} tokens ({total} total)",
                file=sys.stderr,
                flush=True,
            )
    _atomic_replace(tmp, cache_path)
    return {
        "path": str(cache_path),
        "per_source": per_source,
        "total_tokens": total,
        "bytes": cache_path.stat().st_size,
        "sha256": _sha256_file(cache_path),
    }


def iter_cache_docs(cache_path: Path = CACHE_PATH) -> Iterator[list[str]]:
    """Token lists per document from the cache, cache order."""
    with cache_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            tokens = line.split()
            if tokens:
                yield tokens
