"""Mechanical pin on *checkable* claims made by shipped prose.

Seventeen separate "falsified claim" defects have been found in this
repo — a CHANGELOG line, docstring, or doc paragraph asserting something
the code does not do. Several were written *by the repairs fixing
earlier ones*. Every fix so far closed an instance; nothing closed the
class. This module closes the mechanically-decidable slice of it, so the
next false claim fails CI instead of shipping.

Design bias: **a checker with false positives gets disabled, and a
disabled checker protects nothing.** Every rule below was run against
the real corpus at HEAD and tightened until its misfires were zero. Where
a claim shape is ambiguous in English, it is dropped rather than guessed.
Coverage is deliberately narrow; its value is what it catches tomorrow.

What is checked
---------------
1. ``path`` — a backtick-quoted token anchored at a repo-relative prefix
   (``src/`` ``docs/`` ``tests/`` ``bench/`` ``examples/`` ``plugin/``
   ``.github/``) with a source-file suffix must exist on disk.
2. ``symbol`` — ``` `name` in `module.py` ``` must resolve inside that
   module by AST (any binding: def, class, assignment, parameter,
   attribute, import).
3. ``test-count`` — "the N tests in ``tests/x.py``" / "``tests/x.py``
   contains N tests" must match the count of test functions found by
   AST. Digits and English number words ("Nine") both parse.
4. ``line-ref`` — a ``file.py:NNN`` citation must land in range, and for
   the markdown-linked form the cited region must actually contain one of
   the code identifiers named in the surrounding paragraph.
5. ``file-count`` — "N files are named ``x.py``" must match how many files
   the repo scan actually resolves for that name.

Sources are scoped by *rot rate*, which is the core design decision
-------------------------------------------------------------------
CHANGELOG.md is a frozen historical record. An entry that was accurate
when written is not a lie later, and rewriting shipped release notes to
appease a linter is worse than the drift. So sources are tiered by how
fast each claim shape decays:

* ``path`` and ``symbol`` claims are checked **everywhere**, changelog
  included. Both decay slowly enough to be worth pinning across history:
  measured at HEAD, the changelog carries 177 checked path tokens with 2
  stale (~1%) and 16 symbol claims with 1 stale (~6%). Three allowlist
  entries to cover the entire release history is a price worth paying
  for full-history coverage.
* ``test-count`` and ``line-ref`` claims are checked in **living
  documents only** (README.md, docs/*.md). Test counts and line numbers
  rot mechanically with every refactor — pinning them against frozen
  release notes would generate permanent allowlist churn and teach
  everyone to ignore this file.
* ``file-count`` claims are checked everywhere **except** the changelog,
  for the same reason: the count is a property of the tree as it stands
  now, so a frozen release note that was right when written would drift
  into permanent allowlist churn.

On src/ and tests/ docstrings: INCLUDED
---------------------------------------
This was decided by measurement, not taste. Across all 812 docstrings in
``src/``: 11 checked path claims, 3 symbol claims, 0 line-refs, 0
test-counts. Including them adds real coverage at zero allowlist cost —
both misfires found there (an illustrative ``docs/y.md``, and
builder.py's past-tense "``_register_tools`` lived in ``server.py``")
are handled by extractor rules, not exemptions.

``tests/`` docstrings were added for the same reason, and after the same
measurement: scanning all 2483 of them for ``path`` and ``symbol``
claims produces zero failures, so the coverage is free. Only
docstrings are read, never statement bodies — the self-tests below are
built from deliberately invalid paths and symbols that exist precisely
to be rejected, so scanning bodies would misfire by construction.
**Corollary for anyone editing this file: keep synthetic examples in
code, not in docstrings.** Every rule here now applies to this file's
own prose, and the extractors do not know that a quoted counter-example
is only being discussed.

Scanning ``tests/`` matters because this file is itself shipped prose,
and its first commit miscounted the files carrying the name
``verify.py`` — asserting three where the repo holds two. Excluding
``tests/`` had made the guard structurally unable to audit its own
docstrings.

The honest caveat, in two parts:

* Extending the corpus to ``tests/`` would *not* by itself have caught
  that defect — "N files are named X" was not a checked shape in any
  source. That is why the ``file-count`` rule exists; the corpus fix and
  the shape fix each close half of it. Verified: the rule fires on the
  original wording and passes the corrected wording.
* The docstring instances that actually shipped false elsewhere were
  *semantic* ("this returns X", "the lock is held here"). No regex
  decides those, so scanning docstrings would not have caught them.
  Those claims remain uncovered, here as everywhere.

What is deliberately NOT checked
--------------------------------
* **Semantic claims.** "This is O(1)", "the lock is held across the
  write". Not mechanically decidable; the honest answer is a human
  reviewer, not a fragile heuristic.
* **Past-tense relocation prose.** "``_register_tools`` lived in
  ``server.py``" is a true statement about history. Tense markers near a
  symbol claim suppress it (``_RELOCATION_PROSE``).
* **Ambiguous module references, when any reading satisfies them.**
  Two files are named ``verify.py``. A claim is reported only when it
  fails against *every* candidate — see ``_resolve_modules``.
* **Bare "N tests in `x.py`" without a total-marking determiner.**
  English does not distinguish "the N tests in X" (total, checkable)
  from "N tests in X" (a subset, uncheckable). Real corpus instances of
  both exist. Only total-marked forms are accepted; see ``_TESTCOUNT_*``.
* **Counts against parametrized test files.** ``@pytest.mark.parametrize``
  makes function count and collected count differ, so "N tests" is
  ambiguous. Such files are skipped outright.
* **Commit messages.** Several real instances lived there, but they are
  immutable and not shipped prose. Out of scope by definition.
* **Planning documents' path claims.** ``docs/ROADMAP.md`` and the
  ``*-plan.md`` files propose files that do not exist yet — that is what
  a plan is. Their line-refs and symbol claims *are* checked, since those
  cite current code.
* **Placeholder paths.** ``src/mod.py``, ``docs/spec.md``, ``src/x.py``
  are syntax examples, not assertions of existence. Stems in
  ``_PLACEHOLDER_STEMS`` are skipped. This is an extractor rule, not an
  allowlist entry, because these are permanent by intent and an allowlist
  entry could never be retired.
* **Statement bodies in ``src/`` and ``tests/``.** Only docstrings are
  read from Python sources. Comments and string literals are not prose
  the project ships, and test bodies are synthetic by design.
* **Counting prose outside the pinned phrasings.** ``file-count`` matches
  "N files are named ``x.py``" and the elided "N are named ``x.py``".
  "``x.py`` names three files" says the same thing and is *not* matched.
  Rather than chase English, prefer the pinned phrasing when writing a
  count about this repo — an unmatched sentence is an unchecked claim.

How the ratchet works
---------------------
``_ALLOWLIST`` holds claims already false at HEAD which this test may not
fix (they live in files owned by other concurrent work, or in frozen
history). It is a ratchet, not a suppression, enforced by two paired
tests:

* ``test_no_unexpected_false_claims`` fails on any false claim **not**
  in the allowlist — the forward guard.
* ``test_allowlist_has_no_stale_entries`` fails on any allowlist entry
  that no longer corresponds to a real failure — the reverse guard. Fix
  a claim and this test tells you to delete its entry.

The reverse guard is the part that matters. Without it an allowlist
silently becomes permanent, which is how this kind of check normally
dies. Entries are keyed by (source, kind, subject) and never by line
number, so editing prose above a claim does not rot the list.

Self-tests at the bottom of this file feed synthetic prose through the
same extractors to prove each rule actually fires, and that each
precision guard actually suppresses. A checker whose rules are all
currently satisfied is indistinguishable from a checker that does
nothing, so those tests are load-bearing, not decorative.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_CHANGELOG = "CHANGELOG.md"
_PLAN_DOCS = frozenset(
    {
        "docs/ROADMAP.md",
        "docs/swarm-convergence-plan.md",
        "docs/v1.6-plan.md",
    }
)

# Conventional stand-in stems. `src/mod.py:42` in a sentence explaining
# citation syntax is not a claim that src/mod.py exists.
_PLACEHOLDER_STEMS = frozenset(
    {"x", "y", "z", "n", "mod", "spec", "foo", "bar", "baz", "qux", "example", "sample"}
)

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_NUM = "|".join([*_NUMBER_WORDS, r"\d{1,4}"])

# Qualifiers that turn a total into a delta: "9 *new* tests in X" says
# nothing about how many tests X holds.
_DELTA_QUALIFIER = re.compile(
    r"\b(new|additional|more|further|other|remaining|extra)\b", re.I
)

_PATH_PREFIXES = (
    "src/",
    "docs/",
    "tests/",
    "bench/",
    "examples/",
    "plugin/",
    ".github/",
)
_PATH_SUFFIX = re.compile(r"\.(py|md|toml|json|yml|yaml|txt|cfg|jsonl|sh)$")
_BACKTICK = re.compile(r"`{1,2}([^`\n]{1,160})`{1,2}")
_ILLUSTRATIVE_CUE = re.compile(
    r"\b(?:like|e\.g\.|eg\.|such as|for example|for instance)\W{0,3}$", re.I
)

# Prose that places a symbol somewhere it USED to be. "``_register_tools``
# lived in ``server.py``" is a true statement about history, not a claim
# that the symbol is there now. Checked against the match and a short
# lookback, since the tense marker often sits ahead of the symbol
# ("Pre-Round-3 `build_server` and `_register_tools` lived in ...").
_RELOCATION_PROSE = re.compile(
    r"\b(lived|moved|used to|previously|formerly|no longer|was|were|removed"
    r"|dropped|renamed|once|before|pre-\w+|until|old|former)\b",
    re.I,
)
_RELOCATION_LOOKBACK = 60

# `symbol` [one or two plain words] in `module.py`. The interposed words
# may not contain punctuation — that keeps the match from stepping over a
# clause boundary and pairing an unrelated symbol with an unrelated file.
_SYMBOL_IN_MODULE = re.compile(
    r"`{1,2}(?P<sym>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`{1,2}"
    r"\*{0,2}(?:\s[A-Za-z][A-Za-z-]{1,14}){0,2}\s+in\s+"
    r"`{1,2}(?P<mod>(?:src/bettermemory/|tests/)?[a-z_][a-z0-9_/]*\.py)`{1,2}"
)

# "the/all/its N tests in `tests/x.py`" — a determiner marks the count as
# the file's total. Bare "N tests in X" is a subset reading; see docstring.
_TESTCOUNT_TOTAL = re.compile(
    rf"\b(?:all|the|its)\s+(?P<n>{_NUM})\s+(?:test|check)s?"
    rf"(?:\s+functions?|\s+cases?)?\s+in\s+`{{1,2}}(?P<path>tests/[\w/]+\.py)`{{1,2}}",
    re.I,
)
# "`tests/x.py` contains N test functions" — the file is the subject, so
# the count is unambiguously its total.
_TESTCOUNT_SUBJECT = re.compile(
    rf"`{{1,2}}(?P<path>tests/[\w/]+\.py)`{{1,2}}(?P<mid>[^`]{{0,120}}?)"
    rf"\b(?:contains|holds|has)\b\s+(?:only\s+|just\s+)?(?P<n>{_NUM})\s+"
    rf"(?:test|check)s?\b(?P<tail>[^.]{{0,12}})",
    re.I,
)
# A restrictive relative clause makes "has N tests that ..." a subset.
_RESTRICTIVE = re.compile(r"^\s*(that|which|covering|pinning|exercising)\b", re.I)

# "three files are named `verify.py`" / the elided "two are named
# `init.py`". Deliberately one phrasing (plus its elision) rather than a
# net for every English way of counting files — see the module docstring.
_FILECOUNT = re.compile(
    rf"\b(?P<n>{_NUM})\s+(?:files?\s+|modules?\s+)?are\s+named\s+"
    rf"`{{1,2}}(?P<name>[\w/]+\.py)`{{1,2}}",
    re.I,
)

_LINEREF_LINKED = re.compile(
    r"\[(?P<name>[\w./]+\.py):(?P<start>\d+)(?:-(?P<end>\d+))?\]\((?P<target>[^)]+)\)"
)
_LINEREF_BARE = re.compile(r"`{0,2}(?P<name>[\w/]+\.py):(?P<start>\d+)`{0,2}")
_CODE_IDENT = re.compile(r"`{1,2}([A-Za-z_][A-Za-z0-9_]*)(?:\(|`)")

# Slack for the anchor-proximity check. Generous on purpose: a citation
# that drifted a few lines during a refactor is still useful, while one
# pointing at an unrelated part of the file is the bug we want.
_ANCHOR_WINDOW = 15


@dataclass(frozen=True)
class Claim:
    """One extracted, mechanically-checkable assertion."""

    source: str
    line: int
    kind: str
    subject: str

    @property
    def key(self) -> tuple[str, str, str]:
        """Allowlist identity — deliberately excludes ``line``."""
        return (self.source, self.kind, self.subject)


@dataclass(frozen=True)
class Failure:
    claim: Claim
    detail: str

    def __str__(self) -> str:
        return (
            f"{self.claim.source}:{self.claim.line} [{self.claim.kind}] "
            f"{self.claim.subject} — {self.detail}"
        )


# --------------------------------------------------------------------------
# Known-false claims at HEAD. Each entry says WHY it is exempt. The reverse
# guard deletes any entry that stops corresponding to a real failure.
# --------------------------------------------------------------------------
_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        _CHANGELOG,
        "path",
        "examples/memories/2025-04-15-projects-foo-stack.md",
    ): (
        "Frozen history. The example memory was later renamed "
        "foo-stack -> atlas-stack; the release note was accurate when "
        "written. Rewriting shipped release notes is worse than the drift."
    ),
    (
        _CHANGELOG,
        "path",
        "docs/blog/memory-is-rotting.md",
    ): (
        "Frozen history. A release note announced this draft post; the "
        "file was never committed (or was later removed). Genuinely "
        "false, but it is a historical entry, not a repair target."
    ),
    (
        _CHANGELOG,
        "symbol",
        "instructions in src/bettermemory/server.py",
    ): (
        "Frozen history. The MCP `instructions` block was in server.py "
        "when this entry shipped; the Round-3 wiring extraction moved it "
        "to builder.py. Accurate release note, since-refactored code."
    ),
    (
        "docs/swarm-convergence-plan.md",
        "line-ref",
        "events.py:237",
    ): (
        "Genuinely false and queued for repair: the paragraph cites this "
        "line for the crc32 shard assignment, which actually lives at "
        "events.py:320 — line 237 is inside a redaction docstring. The "
        "doc is owned by concurrent work this round, so it is exempt "
        "here rather than fixed here."
    ),
}


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------
def _living_docs() -> list[tuple[str, str]]:
    """README + docs/*.md — documents expected to describe current state."""
    out: list[tuple[str, str]] = [
        ("README.md", (_REPO_ROOT / "README.md").read_text(encoding="utf-8"))
    ]
    for path in sorted(_REPO_ROOT.glob("docs/*.md")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        out.append((rel, path.read_text(encoding="utf-8")))
    return out


def _prose_sources() -> list[tuple[str, str]]:
    """Everything scanned for path/symbol claims: living docs + changelog."""
    out = _living_docs()
    out.append((_CHANGELOG, (_REPO_ROOT / _CHANGELOG).read_text(encoding="utf-8")))
    return out


def _docstrings_under(pattern: str) -> list[tuple[str, int, str]]:
    """``(relpath, first_line_of_literal, text)`` for each matched docstring.

    Docstrings only — never statement bodies. Test modules are full of
    synthetic strings that exist precisely to be invalid, so scanning
    their bodies would misfire by construction.
    """
    out: list[tuple[str, int, str]] = []
    for path in sorted(_REPO_ROOT.glob(pattern)):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                continue
            text = ast.get_docstring(node, clean=False)
            if not text:
                continue
            first = node.body[0]
            out.append((rel, getattr(first, "lineno", 1), text))
    return out


def _code_docstrings() -> list[tuple[str, int, str]]:
    """Docstrings from both shipped source and the test suite.

    ``tests/`` is included so that this module — itself shipped prose,
    and the origin of a false claim on its first commit — falls inside
    the corpus it polices.
    """
    return _docstrings_under("src/**/*.py") + _docstrings_under("tests/**/*.py")


# --------------------------------------------------------------------------
# Repo introspection
# --------------------------------------------------------------------------
def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


@lru_cache(maxsize=None)
def _all_py_files() -> tuple[str, ...]:
    skip = {".git", ".claude", ".venv", "node_modules", "__pycache__", "build", "dist"}
    return tuple(
        p.relative_to(_REPO_ROOT).as_posix()
        for p in _REPO_ROOT.rglob("*.py")
        if not skip & set(p.relative_to(_REPO_ROOT).parts)
    )


@lru_cache(maxsize=None)
def _resolve_modules(name: str) -> tuple[str, ...]:
    """Every file a bare-ish module reference could plausibly mean.

    Bare references are genuinely ambiguous — two files are named
    ``verify.py`` and two are named ``init.py``. Rather than guess (a
    wrong guess is a false positive, the one outcome that gets this
    checker disabled) or skip (which would drop most of the corpus),
    callers verify against *all* candidates and report only when the
    claim fails against every one of them. A claim that holds for some
    plausible reading of the reference is not a false claim.
    """
    return tuple(
        rel for rel in _all_py_files() if rel == name or rel.endswith("/" + name)
    )


@lru_cache(maxsize=None)
def _bound_names(rel: str) -> frozenset[str]:
    """Every name bound anywhere in a module, by AST."""
    tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))
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
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return frozenset(names)


@lru_cache(maxsize=None)
def _test_function_count(rel: str) -> int | None:
    """Count test functions by AST. None when the file is parametrized.

    ``@pytest.mark.parametrize`` decouples function count from collected
    count, which makes any "N tests" claim ambiguous — so we decline to
    check rather than risk a false positive.
    """
    tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))

    def is_parametrize(node: ast.AST) -> bool:
        for deco in getattr(node, "decorator_list", []):
            if "parametrize" in ast.unparse(deco):
                return True
        return False

    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                if is_parametrize(node):
                    return None
                count += 1
    return count


# --------------------------------------------------------------------------
# Extraction + verification, per claim shape
# --------------------------------------------------------------------------
def _is_placeholder(token: str) -> bool:
    stem = token.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return stem.lower() in _PLACEHOLDER_STEMS


def check_paths(source: str, text: str, line_offset: int = 0) -> list[Failure]:
    """Anchored repo-relative path tokens must exist on disk."""
    out: list[Failure] = []
    if source in _PLAN_DOCS:
        return out
    for index, line in enumerate(text.splitlines(), 1):
        for match in _BACKTICK.finditer(line):
            token = match.group(1)
            if not token.startswith(_PATH_PREFIXES):
                continue
            if any(ch in token for ch in "*?<>%$ ") or not _PATH_SUFFIX.search(token):
                continue
            if _is_placeholder(token):
                continue
            if _ILLUSTRATIVE_CUE.search(line[: match.start()]):
                continue
            if (_REPO_ROOT / token).exists():
                continue
            claim = Claim(source, index + line_offset, "path", token)
            out.append(Failure(claim, "no such file in the repo"))
    return out


def check_symbols(source: str, text: str, line_offset: int = 0) -> list[Failure]:
    """``` `sym` in `module.py` ``` must resolve in that module by AST."""
    out: list[Failure] = []
    for match in _SYMBOL_IN_MODULE.finditer(text):
        sym, mod = match.group("sym"), match.group("mod")
        if sym.endswith("py") or _is_placeholder(mod):
            continue
        context = text[max(0, match.start() - _RELOCATION_LOOKBACK) : match.end()]
        if _RELOCATION_PROSE.search(context):
            continue
        candidates = _resolve_modules(mod)
        if not candidates:
            continue
        parts = sym.split(".")
        if any(
            parts[-1] in _bound_names(rel) or parts[0] in _bound_names(rel)
            for rel in candidates
        ):
            continue
        line = _line_of(text, match.start()) + line_offset
        claim = Claim(source, line, "symbol", f"{sym} in {mod}")
        out.append(Failure(claim, f"not bound anywhere in {mod} (checked by AST)"))
    return out


def check_file_counts(source: str, text: str, line_offset: int = 0) -> list[Failure]:
    """ "N files are named ``x.py``" must match what the repo scan resolves.

    This shape exists because the first version of this module miscounted
    the files carrying the name ``verify.py`` — asserting three where the
    repo holds two. That was a false claim in the very file built to stop
    false claims, in a shape no other rule covered.
    """
    out: list[Failure] = []
    for match in _FILECOUNT.finditer(text):
        name = match.group("name")
        if _is_placeholder(name):
            continue
        claimed = _parse_number(match.group("n"))
        if claimed < 0:
            continue
        actual = len(_resolve_modules(name))
        if actual == claimed:
            continue
        line = _line_of(text, match.start()) + line_offset
        claim = Claim(source, line, "file-count", name)
        out.append(
            Failure(
                claim, f"prose claims {claimed} file(s) so named; repo has {actual}"
            )
        )
    return out


def check_test_counts(source: str, text: str) -> list[Failure]:
    """Total-marked test counts must match the AST function count."""
    out: list[Failure] = []
    found: list[tuple[int, str, int]] = []

    for match in _TESTCOUNT_TOTAL.finditer(text):
        window = text[max(0, match.start() - 40) : match.start()]
        if _DELTA_QUALIFIER.search(window) or _DELTA_QUALIFIER.search(match.group(0)):
            continue
        found.append(
            (
                _line_of(text, match.start()),
                match.group("path"),
                _parse_number(match.group("n")),
            )
        )

    for match in _TESTCOUNT_SUBJECT.finditer(text):
        if _RESTRICTIVE.match(match.group("tail")):
            continue
        if _DELTA_QUALIFIER.search(match.group(0)):
            continue
        found.append(
            (
                _line_of(text, match.start()),
                match.group("path"),
                _parse_number(match.group("n")),
            )
        )

    for line, rel, claimed in found:
        if claimed < 0:
            continue
        if not (_REPO_ROOT / rel).is_file():
            claim = Claim(source, line, "test-count", rel)
            out.append(Failure(claim, "claims a test count for a file that is missing"))
            continue
        actual = _test_function_count(rel)
        if actual is None or actual == claimed:
            continue
        claim = Claim(source, line, "test-count", rel)
        out.append(Failure(claim, f"prose claims {claimed} tests; AST counts {actual}"))
    return out


def _parse_number(token: str) -> int:
    lowered = token.lower()
    if lowered.isdigit():
        return int(lowered)
    return _NUMBER_WORDS.get(lowered, -1)


def _paragraph_around(text: str, line: int) -> str:
    lines = text.splitlines()
    index = min(max(line - 1, 0), max(len(lines) - 1, 0))
    start = index
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = index
    while end < len(lines) - 1 and lines[end + 1].strip():
        end += 1
    return "\n".join(lines[start : end + 1])


def check_line_refs(source: str, text: str) -> list[Failure]:
    """``file.py:NNN`` citations must be in range and land near their claim."""
    out: list[Failure] = []
    linked_spans: list[tuple[int, int]] = []

    for match in _LINEREF_LINKED.finditer(text):
        linked_spans.append(match.span())
        line = _line_of(text, match.start())
        name = match.group("name")
        subject = f"{name}:{match.group('start')}"
        target = ((_REPO_ROOT / source).parent / match.group("target")).resolve()
        claim = Claim(source, line, "line-ref", subject)
        if not target.is_file():
            out.append(Failure(claim, f"link target missing: {match.group('target')}"))
            continue
        body = target.read_text(encoding="utf-8").splitlines()
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if end > len(body):
            out.append(
                Failure(claim, f"cites line {end}; {name} has {len(body)} lines")
            )
            continue
        # Anchor proximity: identifiers named in the same paragraph that
        # exist in the target must appear near the cited region. If none of
        # the paragraph's identifiers exist in the file at all, there is no
        # anchor to check against and we say nothing.
        blob = "\n".join(body)
        anchors = {
            ident
            for ident in _CODE_IDENT.findall(_paragraph_around(text, line))
            if re.search(rf"\b{re.escape(ident)}\b", blob)
        }
        if not anchors:
            continue
        near = "\n".join(
            body[max(0, start - 1 - _ANCHOR_WINDOW) : end + _ANCHOR_WINDOW]
        )
        if any(re.search(rf"\b{re.escape(a)}\b", near) for a in anchors):
            continue
        out.append(
            Failure(
                claim,
                f"none of the identifiers the paragraph attributes to this "
                f"citation ({', '.join(sorted(anchors))}) appear within "
                f"{_ANCHOR_WINDOW} lines of {name}:{start}",
            )
        )

    for match in _LINEREF_BARE.finditer(text):
        if any(s <= match.start() < e for s, e in linked_spans):
            continue
        name = match.group("name")
        if _is_placeholder(name):
            continue
        candidates = _resolve_modules(name)
        if not candidates:
            continue
        start = int(match.group("start"))
        lengths = {
            rel: len((_REPO_ROOT / rel).read_text(encoding="utf-8").splitlines())
            for rel in candidates
        }
        if any(start <= n for n in lengths.values()):
            continue
        line = _line_of(text, match.start())
        claim = Claim(source, line, "line-ref", f"{name}:{start}")
        sizes = ", ".join(f"{rel} has {n}" for rel, n in sorted(lengths.items()))
        out.append(Failure(claim, f"cites line {start}; {sizes}"))
    return out


def collect_failures() -> list[Failure]:
    """Run every checker over its in-scope corpus."""
    out: list[Failure] = []
    for source, text in _prose_sources():
        out.extend(check_paths(source, text))
        out.extend(check_symbols(source, text))
    for source, text in _living_docs():
        out.extend(check_test_counts(source, text))
        out.extend(check_line_refs(source, text))
        out.extend(check_file_counts(source, text))
    for rel, lineno, text in _code_docstrings():
        out.extend(check_paths(rel, text, line_offset=lineno - 1))
        out.extend(check_symbols(rel, text, line_offset=lineno - 1))
        out.extend(check_file_counts(rel, text, line_offset=lineno - 1))
    return out


# --------------------------------------------------------------------------
# The two paired ratchet tests
# --------------------------------------------------------------------------
def test_no_unexpected_false_claims() -> None:
    """Forward guard: no checkable claim in shipped prose may be false.

    If this fails, the prose is wrong — fix the prose (or the code it
    describes). Adding an ``_ALLOWLIST`` entry is for claims owned by
    other in-flight work, not for silencing your own.
    """
    unexpected = [f for f in collect_failures() if f.claim.key not in _ALLOWLIST]
    if unexpected:
        rendered = "\n".join(f"  - {f}" for f in unexpected)
        pytest.fail(
            f"{len(unexpected)} false claim(s) in shipped prose:\n{rendered}\n\n"
            f"Each is a statement the repo does not support. Fix the prose "
            f"rather than the checker unless the extraction itself misfired."
        )


def test_allowlist_has_no_stale_entries() -> None:
    """Reverse guard: the allowlist may not outlive the failures it covers.

    This is what makes the allowlist a ratchet. When someone repairs an
    exempted claim, its entry stops matching a real failure and this test
    fails, forcing the entry out. Without this, the list would silently
    calcify into permanent suppression.
    """
    live = {f.claim.key for f in collect_failures()}
    stale = sorted(key for key in _ALLOWLIST if key not in live)
    if stale:
        rendered = "\n".join(
            f"  - {key} (exempt because: {_ALLOWLIST[key]})" for key in stale
        )
        pytest.fail(
            f"{len(stale)} _ALLOWLIST entr(y/ies) no longer correspond to a real "
            f"failure:\n{rendered}\n\nThe claim was fixed, or the extractor "
            f"stopped matching it. Delete the entry — that is the ratchet."
        )


def test_allowlist_entries_carry_a_reason() -> None:
    """Every exemption must say why, so review can judge it."""
    for key, reason in _ALLOWLIST.items():
        assert len(reason.strip()) >= 40, f"{key} needs a substantive reason"


# --------------------------------------------------------------------------
# Self-tests. These prove the checkers can actually fail — a rule that is
# merely satisfied today is indistinguishable from a rule that does nothing.
# They assert against real repo files, so they stay honest as the repo moves.
# --------------------------------------------------------------------------
def test_detects_missing_path() -> None:
    fails = check_paths("docs/fake.md", "See `src/bettermemory/nope_xyz.py` for it.")
    assert [f.claim.subject for f in fails] == ["src/bettermemory/nope_xyz.py"]


def test_accepts_existing_path() -> None:
    assert check_paths("docs/fake.md", "See `src/bettermemory/store.py`.") == []


def test_placeholder_paths_are_not_claims() -> None:
    """`src/mod.py` in a syntax example asserts nothing about the repo."""
    text = "repo-relative citations (`src/mod.py`, `docs/spec.md`, `src/x.py`)"
    assert check_paths("docs/fake.md", text) == []


def test_illustrative_cue_suppresses_path_claim() -> None:
    assert (
        check_paths("docs/fake.md", "a repo-relative path like `src/nope_q.py`") == []
    )


def test_plan_docs_exempt_from_path_claims() -> None:
    """Plans propose files that do not exist yet; that is what a plan is."""
    text = "add a `tests/bench_not_real_xyz.py` harness"
    assert check_paths("docs/swarm-convergence-plan.md", text) == []
    assert len(check_paths("docs/clients.md", text)) == 1


def test_detects_unresolvable_symbol() -> None:
    fails = check_symbols("docs/fake.md", "`no_such_symbol_xyz` in `verify.py` does it")
    assert len(fails) == 1
    assert fails[0].claim.kind == "symbol"


def test_accepts_resolvable_symbol() -> None:
    assert (
        check_symbols("docs/fake.md", "`compute_staleness_verdict` in `verify.py`")
        == []
    )


def test_symbol_match_does_not_cross_a_clause_boundary() -> None:
    """The interposed-words rule must not pair unrelated halves.

    Real corpus text: "(`show`) both use it. Eight tests in
    `tests/test_indexed_lookup.py`" — `show` is not claimed to live in
    that file, and an over-greedy pattern would say it was.
    """
    text = "(`show`) both use it. Eight tests in `tests/test_indexed_lookup.py` pin it."
    assert check_symbols("docs/fake.md", text) == []


def test_past_tense_relocation_is_not_a_present_claim() -> None:
    """Real corpus text from builder.py's module docstring.

    ``_register_tools`` genuinely used to live in server.py and now lives
    in builder.py. The sentence is true; reading it as a present-tense
    claim was an extractor bug, not a false claim in the prose.
    """
    text = (
        "Pre-Round-3 ``build_server`` and ``_register_tools`` lived in ``server.py``."
    )
    assert check_symbols("src/bettermemory/builder.py", text) == []


def test_present_tense_claim_about_the_same_symbol_still_fires() -> None:
    """The relocation guard must not blanket-exempt the module."""
    text = "``_register_tools`` in ``server.py`` binds each handler."
    assert len(check_symbols("docs/fake.md", text)) == 1


def test_detects_wrong_test_count() -> None:
    """tests/test_indexed_lookup.py is unparametrized, so its count is exact."""
    real = _test_function_count("tests/test_indexed_lookup.py")
    assert real is not None
    text = f"The {real + 3} tests in `tests/test_indexed_lookup.py` pin the property."
    fails = check_test_counts("docs/fake.md", text)
    assert len(fails) == 1
    assert f"AST counts {real}" in fails[0].detail


def test_detects_wrong_test_count_spelled_as_a_word() -> None:
    """The real instances were spelled out ("Nine"), not written in digits."""
    text = "The nineteen tests in `tests/test_indexed_lookup.py` pin the property."
    fails = check_test_counts("docs/fake.md", text)
    assert len(fails) == 1


def test_accepts_correct_test_count() -> None:
    real = _test_function_count("tests/test_indexed_lookup.py")
    text = f"The {real} tests in `tests/test_indexed_lookup.py` pin the property."
    assert check_test_counts("docs/fake.md", text) == []


def test_subject_form_test_count_is_checked() -> None:
    text = "`tests/test_indexed_lookup.py` contains nineteen test functions."
    assert len(check_test_counts("docs/fake.md", text)) == 1


def test_bare_count_without_determiner_is_not_a_total_claim() -> None:
    """ "two checks in `X`" means two of them, not that X holds two."""
    text = "regression tests (`tests/test_version.py`, two checks in "
    text += "`tests/test_indexed_lookup.py`)."
    assert check_test_counts("docs/fake.md", text) == []


def test_delta_qualified_count_is_not_a_total_claim() -> None:
    text = "The 9 new tests in `tests/test_indexed_lookup.py` pin striping."
    assert check_test_counts("docs/fake.md", text) == []


def test_parametrized_files_are_skipped_for_counts() -> None:
    """Function count != collected count, so the claim is ambiguous."""
    parametrized = [
        rel
        for rel in _all_py_files()
        if rel.startswith("tests/") and _test_function_count(rel) is None
    ]
    assert parametrized, "expected at least one parametrized test module"
    text = f"The 99999 tests in `{parametrized[0]}` pin things."
    assert check_test_counts("docs/fake.md", text) == []


def test_detects_out_of_range_line_reference() -> None:
    text = "see [events.py:999999](../src/bettermemory/events.py) for the shard"
    fails = check_line_refs("docs/fake.md", text)
    assert len(fails) == 1
    assert "cites line 999999" in fails[0].detail


def test_detects_line_reference_pointing_at_the_wrong_place() -> None:
    """The real defect: events.py:237 cited for the crc32 shard assignment.

    Line 237 is inside a redaction docstring; the assignment is at 320.
    A pure line-count check passes this — only anchor proximity catches it.
    """
    text = (
        "a recorder picks its shard by `crc32(session_id)` "
        "([events.py:237](../src/bettermemory/events.py)), so writers differ"
    )
    fails = check_line_refs("docs/fake.md", text)
    assert len(fails) == 1
    assert "crc32" in fails[0].detail


def test_accepts_line_reference_that_lands_on_its_anchor() -> None:
    text = (
        "a recorder picks its shard by `crc32(session_id)` "
        "([events.py:320](../src/bettermemory/events.py)), so writers differ"
    )
    assert check_line_refs("docs/fake.md", text) == []


def test_ambiguous_module_reference_accepts_any_plausible_reading() -> None:
    """Two files are named ``verify.py``; the claim holds if either satisfies it.

    ``memory_verify`` is defined only in ``handlers/verify.py``, never in
    the top-level ``verify.py``. Guessing "shallowest wins" would report
    this true statement as false — the exact false positive that gets a
    checker switched off.

    The count above is in the phrasing ``_FILECOUNT`` matches, so this
    docstring is itself checked by ``check_file_counts``.
    """
    assert len(_resolve_modules("verify.py")) > 1
    assert check_symbols("docs/fake.md", "`memory_verify` in `verify.py` runs it") == []


def test_ambiguous_module_reference_still_reports_a_claim_false_everywhere() -> None:
    """Ambiguity is not a free pass: absent from all candidates is false."""
    fails = check_symbols("docs/fake.md", "`absent_everywhere_xyz` in `verify.py`")
    assert len(fails) == 1


def test_detects_wrong_file_count() -> None:
    """The exact false claim this file shipped on its first commit.

    The original prose put the count at three; the repo holds two.
    The offending wording is kept in the body, not this docstring,
    because ``tests/`` docstrings are now part of the scanned corpus —
    quoting it here would make this docstring a false claim in its own
    right.
    """
    text = "three files are named `verify.py` so the reference is ambiguous"
    fails = check_file_counts("tests/fake.py", text)
    assert len(fails) == 1
    assert fails[0].claim.kind == "file-count"
    assert "claims 3 file(s) so named; repo has 2" in fails[0].detail


def test_accepts_correct_file_count() -> None:
    actual = len(_resolve_modules("verify.py"))
    text = f"{actual} files are named `verify.py` so the reference is ambiguous"
    assert check_file_counts("tests/fake.py", text) == []


def test_file_count_accepts_the_elided_form() -> None:
    """ "two are named `init.py`" elides the noun and is still a claim."""
    assert check_file_counts("tests/fake.py", "two are named `init.py`") == []
    assert len(check_file_counts("tests/fake.py", "nine are named `init.py`")) == 1


def test_file_count_ignores_placeholder_names() -> None:
    assert check_file_counts("tests/fake.py", "three files are named `mod.py`") == []


def test_this_module_is_inside_the_scanned_corpus() -> None:
    """The guard must be able to audit its own docstrings.

    Excluding ``tests/`` is what let the original miscount of
    ``verify.py`` ship: no rule could ever have read it.
    """
    scanned = {rel for rel, _, _ in _code_docstrings()}
    assert "tests/test_doc_claims.py" in scanned
    assert any(rel.startswith("src/") for rel in scanned)


def test_corpus_extension_alone_would_not_have_caught_the_defect() -> None:
    """Pins the honest limit claimed in the module docstring.

    Scanning ``tests/`` was necessary but not sufficient — the original
    wording is invisible to every rule that predates ``file-count``, so
    the corpus fix and the shape fix each close half of the hole.
    """
    text = "three files are named `verify.py` and two are named `init.py`"
    assert check_paths("tests/fake.py", text) == []
    assert check_symbols("tests/fake.py", text) == []
    assert len(check_file_counts("tests/fake.py", text)) == 1


def test_line_ref_uses_the_largest_plausible_candidate() -> None:
    """A line in range for any same-named file is not an out-of-range cite."""
    text = "see verify.py:1700 for the detector"
    assert check_line_refs("docs/fake.md", text) == []
    assert len(check_line_refs("docs/fake.md", "see verify.py:999999 there")) == 1
