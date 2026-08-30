"""W3-D reader: definitional edge extraction and the Stage-0 census.

Implements the W3-D declaration §3 and §4 over the two admitted
register entries: the eight enwiktionary part files and the single
simplewiki multistream dump. One streaming pass, each file's pinned
sha256 re-verified over the exact bytes before its first page is read.
Every emitted edge is a directed (head, term, label, source) tuple
under the four declared rule steps — relation sections, inline synonym
templates, definition glosses, lead sentences — with head and term
single tokens under the W3-P tokenizer, carried verbatim by import.

The page parser and the extraction core are stream-agnostic so the CI
determinism leg (`tests/test_w3d_determinism.py`) can drive them with
hand-written page blocks and no corpus bytes — pinning the page gates,
the English-section bound, template stripping, the lead-sentence
finder, tuple dedup, and the floors.

Run: fastvenv/bin/python bench/w/w3d_edges.py
Artifact: w3d-census-<date>.json in bench/w/results/.
"""

from __future__ import annotations

import bz2
import html
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterable, Iterator

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from w3p_anatomy import NEEDS  # noqa: E402
from w3p_pairs import (  # noqa: E402
    REGISTER,
    content_tokens,
    provenance,
    register_overlap,
    sha256_of,
)

WIKTIONARY_NAME = "enwiktionary-20260801-pages-articles"
SIMPLEWIKI_NAME = "simplewiki-20260801-pages-articles-multistream"

# Declaration §4: the floors, fixed at the declaration commit.
FLOOR_V_EDGES = 250_000
FLOOR_C_NEEDS = 4
FLOOR_C_ATTESTATIONS = 2

_TITLE_GATE_RE = re.compile(r"^[A-Za-z][a-z0-9]{2,29}$")
_PAREN_TAIL_RE = re.compile(r"\s*\([^)]*\)\s*$")
_RELATION_HEADING_RE = re.compile(r"^(={3,5})\s*(Synonyms|Hypernyms|Hyponyms)\s*\1\s*$")
_L_TEMPLATE_RE = re.compile(r"\{\{l\|en\|([^|}\n]+)")
_SYN_INLINE_RE = re.compile(r"\{\{(?:syn|synonyms)\|en\|([^}]*)\}\}")
_LINK_TARGET_RE = re.compile(r"\[\[([^\]|#\n]+)")
_PIPED_LINK_RE = re.compile(r"\[\[[^\]|]*\|([^\]]*)\]\]")
_BARE_LINK_RE = re.compile(r"\[\[([^\]]*)\]\]")
_TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}")
_LEAD_SKIP_PREFIXES = (
    "{{",
    "|",
    "}}",
    "<",
    "==",
    "*",
    ":",
    "#",
    "[[File:",
    "[[Image:",
)

GLOSS_LINE_CAP = 3
TOKENS_PER_LINE_CAP = 12
TOKENS_PER_TARGET_CAP = 3


@dataclass
class Page:
    title: str
    ns: int
    redirect: bool
    text: str


def iter_pages(lines: Iterable[bytes]) -> Iterator[Page]:
    """Mechanical page parser over a MediaWiki XML export stream.

    Yields every page; content is accumulated only for ns-0
    non-redirect pages (others yield with empty text — the callers
    filter on ns and redirect anyway, so the bytes are never kept).
    """
    in_page = False
    in_text = False
    keep = False
    title = ""
    ns = -1
    redirect = False
    parts: list[bytes] = []
    for raw in lines:
        if not in_page:
            if b"<page>" in raw:
                in_page = True
                in_text = False
                title, ns, redirect, parts = "", -1, False, []
            continue
        if in_text:
            close = raw.find(b"</text>")
            if close < 0:
                if keep:
                    parts.append(raw)
                continue
            if keep:
                parts.append(raw[:close])
            yield Page(
                title,
                ns,
                redirect,
                html.unescape(b"".join(parts).decode("utf-8", "replace")),
            )
            in_text = False
            parts = []
            continue
        if b"<title>" in raw:
            start = raw.find(b"<title>") + 7
            end = raw.find(b"</title>", start)
            title = html.unescape(raw[start:end].decode("utf-8", "replace"))
        elif b"<ns>" in raw:
            start = raw.find(b"<ns>") + 4
            end = raw.find(b"</ns>", start)
            try:
                ns = int(raw[start:end])
            except ValueError:
                ns = -1
        elif b"<redirect" in raw:
            redirect = True
        elif b"<text" in raw:
            keep = ns == 0 and not redirect
            tag_at = raw.find(b"<text")
            tag_end = raw.find(b">", tag_at)
            if tag_end > 0 and raw[tag_end - 1 : tag_end] == b"/":
                yield Page(title, ns, redirect, "")
                continue
            body = raw[tag_end + 1 :]
            close = body.find(b"</text>")
            if close >= 0:
                yield Page(
                    title,
                    ns,
                    redirect,
                    html.unescape(body[:close].decode("utf-8", "replace"))
                    if keep
                    else "",
                )
                continue
            parts = [body] if keep else []
            in_text = True
        elif b"</page>" in raw:
            in_page = False


def _strip_markup(line: str) -> str:
    for _ in range(5):
        stripped = _TEMPLATE_RE.sub(" ", line)
        if stripped == line:
            break
        line = stripped
    line = _PIPED_LINK_RE.sub(r"\1", line)
    line = _BARE_LINK_RE.sub(r"\1", line)
    return line


def _target_tokens(target: str) -> list[str]:
    return content_tokens(target)[:TOKENS_PER_TARGET_CAP]


def english_section(text: str) -> list[str] | None:
    """The ==English== section's lines, up to the next level-2 heading."""
    lines = text.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "==English==":
            start = i + 1
            break
    if start is None:
        return None
    out: list[str] = []
    for line in lines[start:]:
        if line.startswith("==") and not line.startswith("==="):
            break
        out.append(line)
    return out


def wiktionary_page_edges(title: str, text: str) -> list[tuple[str, str]] | None:
    """Rule steps 1-3 for one gated page: (term, label) in emission order.

    Returns None when the page fails a gate (title shape, filler-stem
    head, no English section); the head is title.lower().
    """
    if not _TITLE_GATE_RE.match(title):
        return None
    head = title.lower()
    if content_tokens(head) != [head]:
        return None
    section = english_section(text)
    if section is None:
        return None

    edges: list[tuple[str, str]] = []
    relation_label: str | None = None
    gloss_lines = 0
    for line in section:
        heading = _RELATION_HEADING_RE.match(line)
        if heading is not None:
            relation_label = heading.group(2).lower()
            continue
        if line.startswith("="):
            relation_label = None
            continue
        if relation_label is not None and line.startswith("*"):
            for match in _L_TEMPLATE_RE.finditer(line):
                for tok in _target_tokens(match.group(1)):
                    edges.append((tok, relation_label))
            for match in _LINK_TARGET_RE.finditer(line):
                for tok in _target_tokens(match.group(1)):
                    edges.append((tok, relation_label))
        for match in _SYN_INLINE_RE.finditer(line):
            for arg in match.group(1).split("|"):
                if "=" in arg:
                    continue
                for tok in _target_tokens(arg):
                    edges.append((tok, "synonyms"))
        if line.startswith("# ") and gloss_lines < GLOSS_LINE_CAP:
            gloss_lines += 1
            for match in _LINK_TARGET_RE.finditer(line):
                for tok in _target_tokens(match.group(1)):
                    edges.append((tok, "gloss-link"))
            stripped = _strip_markup(line[2:])
            for tok in content_tokens(stripped)[:TOKENS_PER_LINE_CAP]:
                edges.append((tok, "gloss"))
    return [(term, label) for term, label in edges if term != head]


def lead_sentence(text: str) -> str | None:
    """Declaration §3.4: the mechanically found lead sentence."""
    for line in text.split("\n"):
        if not line or line.startswith(_LEAD_SKIP_PREFIXES):
            continue
        cut = line.find(". ")
        sentence = line[: cut + 1] if cut >= 0 else line
        return sentence[:400]
    return None


def simplewiki_page_edges(
    title: str, text: str
) -> tuple[str, list[tuple[str, str]]] | None:
    """Rule step 4 for one gated page: (head, [(term, label), ...])."""
    bare = _PAREN_TAIL_RE.sub("", title)
    toks = content_tokens(bare)
    if len(toks) != 1:
        return None
    head = toks[0]
    sentence = lead_sentence(text)
    if sentence is None:
        return None
    edges: list[tuple[str, str]] = []
    for match in _LINK_TARGET_RE.finditer(sentence):
        for tok in _target_tokens(match.group(1)):
            edges.append((tok, "lead-link"))
    for tok in content_tokens(_strip_markup(sentence))[:TOKENS_PER_LINE_CAP]:
        edges.append((tok, "lead"))
    return head, [(term, label) for term, label in edges if term != head]


@dataclass
class W3DCensus:
    """The Stage-0 aggregate. Deterministic in the streams."""

    distinct_pairs: set[tuple[str, str]] = field(default_factory=set)
    distinct_by_source: dict[str, int] = field(default_factory=dict)
    distinct_by_label: dict[str, int] = field(default_factory=dict)
    attestations_total: int = 0
    pages_read: dict[str, int] = field(default_factory=dict)
    heads: dict[str, int] = field(default_factory=dict)
    vocab: set[str] = field(default_factory=set)
    need_attestations: dict[str, list[tuple[str, str, str, str]]] = field(
        default_factory=dict
    )

    def add_page(
        self,
        head: str,
        term_labels: list[tuple[str, str]],
        source: str,
        edges_out: IO[bytes] | None = None,
    ) -> None:
        if not term_labels:
            return
        head = sys.intern(head)
        counts: dict[tuple[str, str], int] = {}
        for term, label in term_labels:
            key = (sys.intern(term), label)
            counts[key] = counts.get(key, 0) + 1
        self.pages_read[source] = self.pages_read.get(source, 0) + 1
        self.heads[source] = self.heads.get(source, 0) + 1
        self.vocab.add(head)
        seen_pair_labels: set[tuple[str, str]] = set()
        seen_pairs: set[str] = set()
        for (term, label), count in counts.items():
            if edges_out is not None:
                edges_out.write(
                    f"{head}\t{term}\t{label}\t{source}\t{count}\n".encode()
                )
            self.vocab.add(term)
            self.attestations_total += 1
            if term not in seen_pairs:
                seen_pairs.add(term)
                self.distinct_pairs.add((head, term))
                self.distinct_by_source[source] = (
                    self.distinct_by_source.get(source, 0) + 1
                )
            if (term, label) not in seen_pair_labels:
                seen_pair_labels.add((term, label))
                self.distinct_by_label[label] = self.distinct_by_label.get(label, 0) + 1
            self._record_need(head, term, label, source)

    def _record_need(self, head: str, term: str, label: str, source: str) -> None:
        for qid, (left, right, _register, _gloss) in NEEDS.items():
            if (head in left and term in right) or (head in right and term in left):
                rows = self.need_attestations.setdefault(qid, [])
                tup = (head, term, label, source)
                if tup not in rows:
                    rows.append(tup)

    def counts_payload(self) -> dict[str, object]:
        need_rows = {
            qid: {
                "count": len(self.need_attestations.get(qid, [])),
                "attestations": [list(t) for t in self.need_attestations.get(qid, [])],
                "gloss": NEEDS[qid][3],
                "register": NEEDS[qid][2],
                "supported": len(self.need_attestations.get(qid, []))
                >= FLOOR_C_ATTESTATIONS,
            }
            for qid in sorted(NEEDS)
        }
        supported = sum(1 for row in need_rows.values() if row["supported"])
        floors = {
            "V": {
                "threshold_edges": FLOOR_V_EDGES,
                "distinct_edges": len(self.distinct_pairs),
                "holds": len(self.distinct_pairs) >= FLOOR_V_EDGES,
            },
            "C": {
                "threshold_needs": FLOOR_C_NEEDS,
                "attestations_per_need": FLOOR_C_ATTESTATIONS,
                "needs_supported": supported,
                "holds": supported >= FLOOR_C_NEEDS,
            },
        }
        v_holds = bool(floors["V"]["holds"])
        c_holds = bool(floors["C"]["holds"])
        return {
            "pages_with_edges": dict(sorted(self.pages_read.items())),
            "edge_volume": {
                "distinct_total": len(self.distinct_pairs),
                "distinct_by_source": dict(sorted(self.distinct_by_source.items())),
                "distinct_by_label": dict(sorted(self.distinct_by_label.items())),
                "attestations_total": self.attestations_total,
            },
            "needs": need_rows,
            "floors": floors,
            "g0_verdict": "PASS" if (v_holds and c_holds) else "PARK-AT-CENSUS",
        }


def _register_entry(name: str) -> dict[str, object]:
    register = json.loads(REGISTER.read_text())
    for entry in register["corpora"]:
        if entry.get("name") == name:
            return dict(entry)
    raise SystemExit(f"{name} not found in {REGISTER}")


def _verify(path: Path, pinned: str) -> tuple[str, float]:
    started = time.time()
    actual = sha256_of(path)
    if actual != pinned:
        raise SystemExit(
            f"{path.name}: sha mismatch (pinned {pinned}, actual {actual});"
            " the pin is the authority — nothing is read"
        )
    return actual, round(time.time() - started, 1)


def iter_bz2_lines(path: Path) -> Iterator[bytes]:
    with bz2.open(path, "rb") as handle:
        progress = 0
        for line in handle:
            progress += 1
            if progress % 20_000_000 == 0:
                print(f"  ... {progress:,} lines", file=sys.stderr)
            yield line


def main() -> int:
    wikt = _register_entry(WIKTIONARY_NAME)
    simple = _register_entry(SIMPLEWIKI_NAME)
    date = time.strftime("%Y-%m-%d")
    derived_dir = REPO / "bench" / "w" / "corpus" / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    edges_path = derived_dir / f"w3d-edges-{date}.tsv"
    census_path = REPO / "bench" / "w" / "results" / f"w3d-census-{date}.json"

    census = W3DCensus()
    files_block: list[dict[str, object]] = []
    started_all = time.time()
    with edges_path.open("wb") as edges_out:
        for item in wikt["items"]:  # type: ignore[union-attr]
            path = REPO / str(wikt["local_path"]) / str(item["file"])
            print(f"{path.name}: re-verifying pinned sha256 ...", file=sys.stderr)
            actual, sha_seconds = _verify(path, str(item["sha256"]))
            files_block.append(
                {
                    "file": str(item["file"]),
                    "source": "wiktionary",
                    "sha256_pinned": str(item["sha256"]),
                    "sha256_reverified": actual,
                    "sha_seconds": sha_seconds,
                }
            )
            started = time.time()
            pages = 0
            for page in iter_pages(iter_bz2_lines(path)):
                if page.ns != 0 or page.redirect:
                    continue
                pages += 1
                term_labels = wiktionary_page_edges(page.title, page.text)
                if term_labels:
                    census.add_page(
                        page.title.lower(), term_labels, "wiktionary", edges_out
                    )
            print(
                f"{path.name}: {pages:,} ns0 pages,"
                f" {len(census.distinct_pairs):,} distinct edges so far"
                f" in {round(time.time() - started, 1)}s",
                file=sys.stderr,
            )
        path = REPO / str(simple["local_path"])
        print(f"{path.name}: re-verifying pinned sha256 ...", file=sys.stderr)
        actual, sha_seconds = _verify(path, str(simple["sha256"]))
        files_block.append(
            {
                "file": path.name,
                "source": "simplewiki",
                "sha256_pinned": str(simple["sha256"]),
                "sha256_reverified": actual,
                "sha_seconds": sha_seconds,
            }
        )
        started = time.time()
        pages = 0
        for page in iter_pages(iter_bz2_lines(path)):
            if page.ns != 0 or page.redirect:
                continue
            pages += 1
            result = simplewiki_page_edges(page.title, page.text)
            if result is not None:
                head, term_labels = result
                census.add_page(head, term_labels, "simplewiki", edges_out)
        print(
            f"{path.name}: {pages:,} ns0 pages in {round(time.time() - started, 1)}s",
            file=sys.stderr,
        )
    pass_seconds = round(time.time() - started_all, 1)
    edges_sha = sha256_of(edges_path)

    artifact: dict[str, object] = {
        "provenance": provenance(),
        "declaration": "bench/w/W3D_DECLARATION.md",
        "files": files_block,
        "edge_file": {
            "path": str(edges_path.relative_to(REPO)),
            "sha256": edges_sha,
            "bytes": edges_path.stat().st_size,
        },
        "census": census.counts_payload(),
        "register_overlap": register_overlap(census.vocab),
        "pass_seconds": pass_seconds,
    }
    census_path.write_text(json.dumps(artifact, indent=1) + "\n")

    payload = census.counts_payload()
    headline = ("edge_volume", "needs", "floors", "g0_verdict")
    print(json.dumps({k: payload[k] for k in headline}, indent=1))
    print(f"\ncensus artifact: {census_path.relative_to(REPO)}")
    print(f"edge file: {edges_path} (sha256 {edges_sha[:16]}...)")
    print(f"G0 verdict: {payload['g0_verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
