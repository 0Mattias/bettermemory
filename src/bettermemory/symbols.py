"""Advisory symbol-existence checking for prose that cites code.

Memory bodies name symbols constantly, and until this module nothing in
production ever resolved one against a file. The path leg stats files;
the commit leg counts commits; a memory asserting that some function is
defined in a module it names could go on saying so for a year after the
function was deleted, and every freshness signal the server emits would
keep agreeing with it. The claim-level machinery that scores well on the
rot benchmark is bench-only, and its parser reads a corpus template, not
prose.

What this module does, in one sentence: parse the two-token citation
shape out of a body, resolve the cited module underneath the memory's
own recorded worktree root, and ask the AST whether the name is bound
there.

**Read the reach measurement before believing this covers anything.**
`tests/test_symbol_existence.py` re-derives it on every run over this
repo's own tracked prose and over a synthetic body set, and the
headline is small: the two-token shape is a DOCSTRING convention.
Bodies in a real store overwhelmingly name a symbol in one clause and a
file in another, which is co-occurrence, not a citation — "`foo` and
`pkg/mod.py` disagree" asserts nothing about whether `foo` is in that
file. Widening the parser to pair any backticked name with any
backticked path in the same sentence would multiply the count by an
order of magnitude and every extra pair would be a guess, so the parser
stays narrow and the coverage stays honestly small. The structured
answer to this is claims-at-write, not a looser reader.

**Advisory, structurally.** Nothing here feeds `verdict_from_signals`,
`compute_staleness_verdict`, or any other staleness input, and it is
wired into exactly one caller — the `memory_verify` handler, between
loading the snapshot and stamping the attestation. A miss is reported to
the caller as evidence and is otherwise inert. That is not a temporary
state of the code: the precision of this check on real prose has never
been measured, and until a benchmark measures it, a signal that cannot
escalate anything is the only honest shape for it.

Where the conservatism is spent
-------------------------------
Every judgement call below is resolved toward silence, because the cost
asymmetry here is the opposite of the path leg's. A missed citation
costs one unchecked claim. A false alarm on prose that merely *looks*
like a citation trains the reader to ignore the field, and an ignored
advisory protects nothing.

So a citation is only ever reported as a miss when all of these hold:

* Both halves are backticked. The author marked them as code.
* The module resolves, relative and inside the memory's recorded
  worktree root, to a file that exists.
* That file parses as Python.
* The name is bound NOWHERE in it — not at module level, not as a
  method, an argument, an attribute, or an import.

Anything else — an unstattable root, a file that moved, a file that no
longer parses, a name bound only inside a class body — is reported as
evidence or not at all, never as a miss. In particular the last one:
"bound at top level" is the question this check asks, but a bare method
name cited against the module that holds its class ("`mark_verified` in
`store.py`") is how people actually write, and answering "absent" there
would be a fabrication. The binding tier travels with each checked
citation instead, so a consumer that later wants the strict reading has
it without this module having to guess which reading was meant.

Deliberately not checked, and why
---------------------------------
* **Absolute module citations.** Resolving them means READING a file
  named by memory content from anywhere on the disk, which is a wider
  capability than the path leg's stat and is not needed: a memory
  making claims about a repo has that repo's root recorded on it.
* **Bare basenames** (a module cited with no directory part). Resolving
  one means searching the tree for that name, which is a cost and an
  ambiguity — two files can share a basename — that the honest answer
  does not require. They land in `unresolved`, and the count of them is
  the measurement of how much reach a future index would buy.
* **Non-Python modules.** There is no AST to ask.
* **Attribute chains deeper than the module's own AST can settle.**
  A dotted name resolves if either end of it is bound; the middle is
  not walked.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "SymbolCitation",
    "SymbolDriftReport",
    "check_symbol_citations",
    "extract_symbol_citations",
]


# ---------------------------------------------------------------------------
# Caps — bound the work one body can cause
# ---------------------------------------------------------------------------
#
# This runs once per `memory_verify` call, not per search hit, so the budget
# is looser than the path leg's. It is still bounded: a body pasted full of
# citations must not turn one attestation into hundreds of file reads and
# AST parses.

_MAX_CITATIONS_PER_BODY = 8
_MAX_MODULE_PATH_LEN = 256
_MAX_SYMBOL_LEN = 96

# Hard cap on how much of a body is scanned, mirroring the path leg's. The
# cut can be a plain slice here where the path leg needs a whitespace-aligned
# one: bisecting a citation destroys either its closing backtick or its
# module path, and both failures drop the citation rather than inventing a
# truncated one that resolves to something else.
_MAX_BODY_SCAN_CHARS = 32 * 1024

# A source file above this size is not a module somebody wrote a memory
# about; it is generated data with a .py suffix. Skipping it costs a
# citation nobody made and bounds the parse.
_MAX_MODULE_BYTES = 2 * 1024 * 1024

# Extensions that make a dotted token a path rather than a name, so a
# citation whose first backtick pair held a filename is dropped instead of
# being looked up as a symbol.
_FILE_SUFFIXES = frozenset(
    {"py", "md", "txt", "json", "toml", "yml", "yaml", "cfg", "ini", "sh", "jsonl"}
)


# ---------------------------------------------------------------------------
# The citation shape
# ---------------------------------------------------------------------------
#
# Inherited from the two-token rule in tests/test_doc_claims.py, which was
# tuned against this repo's whole prose corpus: a backticked identifier, at
# most two interposed plain words, `in`, a backticked module path. The
# interposed words may not carry punctuation, which is what keeps the match
# from stepping over a clause boundary and pairing an unrelated symbol with
# an unrelated file.
#
# Two additions over that rule, both reach rather than looseness: a trailing
# `()` on the symbol (prose writes callables that way), and a module path
# that is not restricted to this repo's own directory layout.

_SYMBOL_IN_MODULE = re.compile(
    r"`{1,2}(?P<sym>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,3})"
    r"(?:\(\))?`{1,2}"
    r"\*{0,2}(?:\s[A-Za-z][A-Za-z-]{1,14}){0,2}\s+in\s+"
    r"`{1,2}(?P<mod>[A-Za-z0-9_.][\w./-]{0,255}\.py)`{1,2}"
)

# Prose that says the citation does NOT hold right now, in any of the three
# ways this shape gets written: the symbol used to be there (relocation),
# the sentence denies the claim (negation), or the pair is an illustration
# of a shape rather than an assertion about a file.
#
# Generous on purpose, and the generosity is free in one direction only:
# every alternative here can cost a check that would have been legitimate,
# and none of them can cause an alarm. The relocation half is the marker
# list from tests/test_doc_claims.py, which is what those alternatives were
# tuned for; the other two halves are this module's, because a memory body
# is written in looser prose than a docstring.
_SUPPRESSING_PROSE = re.compile(
    r"\b(?:lived|moved|used\s+to|previously|formerly|no\s+longer|was|were"
    r"|removed|dropped|renamed|once|before|pre-\w+|until|old|former"
    r"|not|never|no|nowhere|isn't|wasn't|doesn't|didn't|instead\s+of"
    r"|rather\s+than|would\s+be|should\s+be|will\s+be|planned|proposed"
    r"|such\s+as|for\s+example|for\s+instance|like|say|imagine|suppose"
    r"|e\.?g\.?|i\.?e\.?)\b",
    re.I,
)
_SUPPRESSION_LOOKBACK = 60


@dataclass(frozen=True)
class SymbolCitation:
    """One parsed citation, and what the AST said about it.

    `binding` is the evidence, not a verdict:

    * `top_level` — the name is bound in the module's own top-level
      scope (including inside a top-level `if` or `try`, which is where
      conditional imports and optional dependencies live).
    * `nested` — bound somewhere in the file but not at top level: a
      method, a parameter, an attribute, a local. Not drift. A bare
      method name cited against its module is ordinary prose.
    * `absent` — bound nowhere the AST can see. This is the only tier
      that becomes a reported miss.
    * `unresolved` — the module was never opened, so nothing was asked.
      Carried on the citations in the report's own `unresolved` bucket,
      never in `checked`.
    """

    symbol: str
    module: str
    binding: str

    def __str__(self) -> str:
        return f"{self.symbol} in {self.module}"


@dataclass(frozen=True)
class SymbolDriftReport:
    """Evidence about a body's symbol citations. Never a verdict.

    `checked` holds every citation whose module resolved to a file that
    parsed; `unresolved` holds the ones that got as far as being parsed
    out of the prose and no further — a bare basename, a file that is
    not under the recorded root, a file that has moved or stopped
    parsing. `unresolved` is deliberately NOT a drift signal: a moved
    file says nothing about whether the symbol claim held, and folding
    the two together is how a cross-host store would light up every
    memory it synced.

    An empty report is the normal case and emits nothing. That includes
    every memory with no recorded worktree root, and every memory whose
    recorded root this machine cannot confirm is a directory — see
    `check_symbol_citations` for why that fail-open is the defensible
    line here even though the path leg makes the opposite call.
    """

    checked: tuple[SymbolCitation, ...] = ()
    unresolved: tuple[SymbolCitation, ...] = ()

    @property
    def missing(self) -> tuple[SymbolCitation, ...]:
        return tuple(c for c in self.checked if c.binding == "absent")

    def __bool__(self) -> bool:
        return bool(self.checked or self.unresolved)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "checked": [str(c) for c in self.checked],
            "missing": [str(c) for c in self.missing],
            "unresolved": [str(c) for c in self.unresolved],
        }


def extract_symbol_citations(body: str) -> tuple[tuple[str, str], ...]:
    """Parse `(symbol, module)` pairs out of a body. No disk access.

    Deduplicated, source order preserved, capped. Split out from the
    checking so the parser's reach over a corpus of real prose can be
    measured without a filesystem in the loop — the number that decides
    whether this shape is worth anything is "how many real citations
    does it see", and that has to be answerable cheaply.
    """
    text = body[:_MAX_BODY_SCAN_CHARS] if body else ""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for match in _SYMBOL_IN_MODULE.finditer(text):
        symbol, module = match.group("sym"), match.group("mod")
        if len(symbol) > _MAX_SYMBOL_LEN or len(module) > _MAX_MODULE_PATH_LEN:
            continue
        # A dotted "symbol" that ends in a file extension is a path the
        # first backtick pair happened to hold, not a name.
        if symbol.rsplit(".", 1)[-1] in _FILE_SUFFIXES:
            continue
        window = text[max(0, match.start() - _SUPPRESSION_LOOKBACK) : match.end()]
        if _SUPPRESSING_PROSE.search(window):
            continue
        pair = (symbol, module)
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
        if len(out) >= _MAX_CITATIONS_PER_BODY:
            break
    return tuple(out)


def check_symbol_citations(
    body: str,
    *,
    worktree_root: str | Path | None = None,
) -> SymbolDriftReport:
    """AST-check a body's symbol citations against the recorded worktree.

    Returns an empty report — which every caller treats as "nothing to
    say" — when there is no root, when the root is not a directory this
    process can confirm, or when the body cites nothing parseable.

    **The fail-open on an unconfirmable root is a decision, not an
    oversight.** `origin._worktree_root_is_gone` records the opposite
    bias for the auto-scope filter, and `verify._path_exists` records it
    again for the path leg: there, an indeterminate stat folds toward
    MORE signal, because over-reporting drift is that surface's safe
    direction. It is not this one's. A store synced from another machine
    carries roots that machine has and this one does not; resolving
    citations against a root that is not there would mark every citation
    in every synced memory absent at once, and the resulting alarm would
    be about the sync, not about any claim. A machine that has never
    seen the checkout has no evidence either way, and "no evidence" is
    an empty report.

    The residual that fail-open does NOT close: a synced memory whose
    recorded root happens to name a live directory on this machine
    holding a different tree. Citations then resolve against the wrong
    files. It is the same exposure the anchored-attestation check
    carries, it is bounded here by being advisory, and closing it needs
    a machine identity on the origin rather than a heuristic.
    """
    citations = extract_symbol_citations(body)
    if not citations:
        return SymbolDriftReport()
    root = _live_root(worktree_root)
    if root is None:
        return SymbolDriftReport()

    checked: list[SymbolCitation] = []
    unresolved: list[SymbolCitation] = []
    # One parse per FILE, not per citation: a body naming four symbols in
    # one module must not read and parse it four times.
    cache: dict[str, tuple[frozenset[str], frozenset[str]] | None] = {}
    for symbol, module in citations:
        if module not in cache:
            cache[module] = _module_bindings(root, module)
        index = cache[module]
        if index is None:
            unresolved.append(SymbolCitation(symbol, module, "unresolved"))
            continue
        tier = _tier(symbol, index[0], index[1])
        checked.append(SymbolCitation(symbol, module, tier))
    return SymbolDriftReport(checked=tuple(checked), unresolved=tuple(unresolved))


def _tier(symbol: str, top_level: frozenset[str], anywhere: frozenset[str]) -> str:
    """Classify one name against a module's two binding sets.

    A dotted name resolves off either end — `Store.mark_verified` cited
    against the module that defines `Store`, and a fully-qualified
    `bettermemory.verify.compute_staleness_verdict` cited against the
    module that defines the last segment, are both true statements and
    neither is checkable segment-by-segment from one file's AST. Taking
    either end is the same call `tests/test_doc_claims.py` made on this
    repo's own prose, and for the same reason: attributing a real symbol
    to a neighbouring module is a routine imprecision, while a name that
    exists nowhere is a fabrication.
    """
    parts = symbol.split(".")
    ends = (parts[0], parts[-1])
    if any(part in top_level for part in ends):
        return "top_level"
    if any(part in anywhere for part in ends):
        return "nested"
    return "absent"


def _live_root(worktree_root: str | Path | None) -> Path | None:
    """The recorded root, only when this process can confirm it is a
    directory. Any failure to confirm returns None (see
    `check_symbol_citations` for why that direction)."""
    if worktree_root is None:
        return None
    try:
        root = Path(worktree_root).resolve(strict=False)
        return root if root.is_dir() else None
    except (OSError, ValueError):
        return None


def _resolve_module(root: Path, module: str) -> Path | None:
    """Resolve a cited module path under `root`, or None.

    Rejects everything that is not a plain relative path landing inside
    the root: absolute paths, drive letters, `~`, `..` escapes, bare
    basenames with no directory part, and symlinks pointing out of the
    tree (the containment test runs after resolution, so it sees where
    the link actually goes).
    """
    if "/" not in module.strip("/"):
        # No directory part. Resolving it would mean searching the tree;
        # see the module docstring's not-checked list.
        return None
    if module.startswith(("/", "~", "\\")) or "\\" in module:
        return None
    if len(module) > 1 and module[1] == ":":
        return None
    parts = module.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    try:
        target = (root / module).resolve(strict=False)
        if not target.is_relative_to(root):
            return None
        return target if target.is_file() else None
    except (OSError, ValueError):
        return None


def _module_bindings(
    root: Path, module: str
) -> tuple[frozenset[str], frozenset[str]] | None:
    """`(top_level, anywhere)` name sets for a cited module, or None.

    None means "could not ask" — the file is not resolvable under the
    root, is too large, cannot be read, or no longer parses. A file that
    stopped parsing is a real event, but it is not evidence about a
    symbol claim, and the direction that manufactures no alarm is the
    one this module takes everywhere.
    """
    path = _resolve_module(root, module)
    if path is None:
        return None
    try:
        if path.stat().st_size > _MAX_MODULE_BYTES:
            return None
        source = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        # Not just SyntaxError: a NUL byte raises ValueError on some
        # versions and deeply nested literals raise RecursionError. A
        # verify call must not die because one cited file is odd.
        return None
    return frozenset(_top_level_names(tree)), frozenset(_all_names(tree))


def _top_level_names(tree: ast.Module) -> set[str]:
    """Names bound in the module's own scope.

    Walks INTO `if`/`try`/`for`/`with` bodies — a name bound under
    `if TYPE_CHECKING:` or in an import fallback is a module-level name
    — and stops AT `def`/`class`, whose bodies open a new scope. The
    walk is an explicit stack rather than recursion so a pathological
    nesting depth cannot raise where `ast.parse` did not.
    """
    names: set[str] = set()
    stack: list[ast.AST] = list(ast.iter_child_nodes(tree))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        stack.extend(ast.iter_child_nodes(node))
    return names


def _all_names(tree: ast.Module) -> set[str]:
    """Every name bound anywhere in the module, by AST.

    Method names, parameters, attributes, keyword arguments at call
    sites, exception aliases and imports included. This is the lenient
    set, and it is what keeps the check from alarming on the commonest
    real citation shape there is — a method named on its own, cited
    against the file that holds its class. The keyword-argument clause
    is not decoration either: prose names a setting by the keyword the
    call site passes it under (`MCPServer(instructions=...)`), and without
    that clause such a citation reads as absent.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names
