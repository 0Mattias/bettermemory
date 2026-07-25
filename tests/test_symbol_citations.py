"""Ratchet lint for backticked citations of symbols that do not exist.

The class: prose in this repo names internal code in backticks, and
nothing checked that the name resolves. ``tests/test_doc_claims.py``
pins the two-token form — ```` `name` in `module.py` ```` — so a bare
backticked identifier standing on its own was invisible to every gate
in the suite. Three independent passes during the 2026-07 burst proved
the class rather than an instance: a repair commit fixing false claims
minted a brand-new one in the same edit, a hand sweep over live source
found a batch more, and the verifier auditing that sweep found further
instances the sweep had walked past. Hand sweeps do not persist and
cannot be re-run; this file can.

The cost of one dangling citation is small and certain: a reader builds
a wrong mental model, an agent burns a grep. Occasionally it is larger —
one of the citations this file's first commit repaired asserted a
retrieval-side safety flag that had never existed, which is a wrong
safety assumption rather than a wasted minute.

Design bias, inherited from ``tests/test_doc_claims.py`` and
``tests/test_platform_fixture_lint.py`` along with the ratchet
mechanics: **a checker with false positives gets disabled, and a
disabled checker protects nothing.** A raw "every backticked identifier
must resolve" pass over this corpus yields hundreds of candidates of
which a handful are real, and the noise is not incidental — it is git
config keys, environment variables, SQLite meta keys, wire-format dict
keys, event kinds, stdlib and vendor names, and deliberate
counter-examples (a tool name a document promises will NOT be used, a
credential-shaped constant the secret detector must ignore). Every rule
below was run against the whole tracked-Python corpus and tightened
until its live findings were exactly the entries in ``_ALLOWLIST``.
Shapes that could not be separated from working prose were dropped and
are named as blind spots rather than guessed at.

What is checked — four shapes and one follow-up, each a mechanically
decidable slice
------------------------------------------------------------------
The corpus is every tracked ``*.py`` file; the prose read from it is
docstrings **and** ``#`` comments. Comments matter here: they are the
blind spot ``tests/test_doc_claims.py`` names in its own docstring, and
several of the citations repaired alongside this file's first commit
lived in one.

1. ``dotted-symbol`` — ```` `stem.name` ```` where ``stem`` is the
   basename of a module in ``src/bettermemory/`` and ``name`` is an
   identifier. Reported when ``name`` is bound **nowhere** in the
   tracked Python. The module half only establishes that the citation
   points at this project's code; the verdict is repo-wide on purpose,
   because attributing a real symbol to a neighbouring module is a
   routine imprecision (a re-export, a caller, an attribute of an object
   that happens to share a module's name) while a name that exists
   nowhere is a fabrication. Precision over recall, deliberately: this
   rule is blind to a real symbol filed under the wrong module.
2. ``private-symbol`` — a bare ```` `_name` ````. A leading underscore
   declares the name internal to this repo, so unlike a public
   identifier it cannot be a stdlib name, a wire key, or a config
   option, and "bound nowhere" is decidable without English. Reported
   when the name is bound nowhere in the tracked Python.
3. ``module-file`` — a bare ```` `name.py` ```` with no directory
   prefix. ``tests/test_doc_claims.py``'s ``path`` rule requires the
   prefix (``tests/x.py``), so the bare filename form was unchecked.
   Reported when no tracked file carries that basename.
4. ``inventory-bullet`` — in a **module docstring under
   ``src/bettermemory/``**, a bullet whose first token is a backticked
   identifier is that module's own inventory of what it holds. Reported
   when the identifier is bound nowhere. Restricted to multi-segment
   snake_case (at least one underscore, not SCREAMING_SNAKE): a
   single-word bullet lead in this corpus is a search mode, an outcome
   name, or a wire value, and a SCREAMING_SNAKE one is an environment
   variable. Both were measured, not assumed.
5. ``unreferenced-private`` — the follow-up rules 1 and 2 ask once the
   cited name DOES resolve. Being bound is not being reached. A dead
   delegate — an alias whose only consumer was rewritten to call the
   implementation directly — keeps a whole family of citations "valid"
   while every one of them names a code path nothing enters, and the
   binding-only test cannot tell the difference. Not hypothetical: the
   commit that added this rule retired 26 such citations across ten
   files, every one of them naming a single method on ``ToolHandlers``
   whose only surviving purpose was to hold those citations up.
   Reported when the cited name is private AND every statement binding
   it is an **undecorated** ``def``/``async def``/``class`` AND nothing
   in the tracked Python loads it, imports it, or names it in a lookup
   string. ``NameUse`` names each clause and why it is there; the
   ``What this cannot see`` section below states the two things this
   rule deliberately declines to judge. The two measurements the design
   rests on — that the undecorated clause is still load-bearing, and
   that the same scan is noise on public names — are re-derived by
   guards on every run rather than recorded here as counts.

Suppressed on purpose
---------------------
* **Historical prose, but only when the marker attaches to the
  citation.** ``_RELOCATION_PROSE`` from ``tests/test_doc_claims.py``,
  imported rather than copied so the two checkers cannot drift apart,
  supplies the vocabulary — and only the vocabulary. That regex was
  tuned for a narrower consumer over there (the two-token ```` `sym` in
  `mod.py` ```` shape), and its alternatives include ordinary English:
  ``was``, ``were``, ``before``, ``until``, ``once``, ``old``,
  ``moved``, ``dropped``, ``renamed``. Searched as a blanket over the
  whole ``_RELOCATION_LOOKBACK`` window, any of those words anywhere in
  the window exempted the citation beside it — which silently swallows
  exactly the class this file exists to catch. A marker now counts only
  when it **pre-modifies the cited name**: nothing but whitespace or a
  dash may sit between the marker and the citation (``_MARKER_ATTACHES``).
  "the pre-extraction ``_cli_serve``" is a true statement about a symbol
  that was removed; a sentence that merely said "before" one clause
  earlier is not. Attachment, not the window width, is now what bounds
  the exemption, so the imported constant is a prefilter rather than the
  guarantee. The suppression sits in the token walk, so it covers rules
  1-3; rule 4 has its own walk and no tense exemption at all.
  ``test_imported_tense_seam_holds_in_both_directions`` drives the real
  predicate over both halves of the import and both directions of each,
  so neither a lost marker, nor a widened one, nor a shrunk lookback can
  change verdicts here in silence.
* **Module references spelled without the suffix.** ``_handlers`` names
  a module, not a symbol; any private token equal to a module basename
  in ``src/bettermemory/`` is a module reference.
* **File extensions.** ``server.json`` and ``__init__.pyi`` match the
  dotted shape and are not symbol citations.
* **Placeholder segments.** ``_handle_foo`` illustrates a naming
  convention. Any ``_``-separated segment drawn from
  ``_PLACEHOLDER_WORDS`` marks the whole token as a syntax example —
  the same judgement ``tests/test_doc_claims.py`` makes for
  ``src/x.py``, applied per segment.
* **Local tails.** A private token that is the tail of a name bound in
  the same file is an elision — this corpus routinely names a sibling
  test by its distinguishing suffix. Scoped to the same file so an
  unrelated coincidence elsewhere in the tree cannot excuse a citation.

Where the reading is literal, each costing a missed finding and never a
false one: a comment's prose unit is the single comment line, so a tense
marker one line above a citation does not reach it (grouping contiguous
comment lines was measured under the old blanket suppressor and hid a
live finding whose preceding line said "before the caller takes the
lock" about lock ordering — the attachment rule would now block that
case on its own, so the one-line unit is the belt to its braces rather
than the only thing holding); a call shape is stripped only when it
trails the token; and a token containing anything but one identifier,
one dot, or a ``.py`` suffix is not a claim these rules read.

What this cannot see
--------------------
* **A bare public identifier.** ```` `broken_link` ````, ````
  `validate_proposals` ```` — the shape that costs the most and is least
  decidable. Public snake_case is indistinguishable from a wire key, an
  event kind, a config field, or a tool argument, all of which this
  corpus cites in backticks constantly. Rule 4 recovers exactly one
  slice of it (a module docstring's own inventory bullet); everywhere
  else the shape is unchecked, and the citation repaired in
  ``src/bettermemory/models.py`` alongside this file's first commit was
  of precisely this kind. If you are writing prose, prefer the
  ``module.symbol`` spelling — it is the form this file can check.
* **Wrong-module attribution.** By construction, per rule 1 above.
* **Whether a PUBLIC name is reached.** Rule 5 stops at the leading
  underscore, and the reason is a measurement rather than caution: the
  same scan run over public names flags most of them, because pytest
  collects its tests by name and an exported API is called by consumers
  outside the corpus, so "defined here and mentioned nowhere else" is
  the normal state of a public name rather than a defect. The figure is
  not written down here — it is re-derived on every run by
  ``test_the_same_scan_over_public_names_is_measured_noise``, which
  fails if the scan ever becomes precise enough on public names for
  this narrowing to be costing recall for nothing. A dead PUBLIC
  delegate cited in prose is invisible to rule 5, and that is the
  largest hole it leaves open.
* **Reachability, as opposed to being referenced.** Rule 5 decides
  "the tracked Python never mentions this name outside its own
  definition" and nothing beyond it. A private helper called only from
  inside another dead function reads as referenced and always will —
  reachability is undecidable in general, and a transitive liveness
  pass would trade a rule whose negative evidence is one unambiguous
  AST node for one that must model imports, dynamic dispatch and
  framework entry points. Rule 5 is a proper subset of dead code,
  chosen because its evidence is a single pass and its negative is
  exact. Two further channels it cannot follow, both conceded rather
  than guessed: a dispatch name assembled from fragments
  (``getattr(self, f"_handle_{kind}")`` — the literal-string channel
  in ``_lookup_strings`` covers the spelled-out form and nothing else),
  and a reference from outside the tracked Python.
* **History told in the predicative voice.** A cited name *followed* by
  "was removed" or "has since been renamed" states the same fact as the
  attributive "the removed ``_handle_foo``", but the marker trails the
  citation instead of modifying it, and reading forward from a citation
  is a second suppression channel this file does not open — every extra
  channel is another way to swallow a real finding. Such a citation is
  reported. Prefer the attributive spelling; where the sentence should
  not be reworded, an ``_ALLOWLIST`` entry is the escape hatch, and two
  of the four base entries are exactly that. It is the same judgement
  ``tests/test_doc_claims.py`` makes for its commit-pin vocabulary: an
  unmatched phrasing leaves the claim checked, which fails loudly and
  teaches the canonical form.
* **Markdown.** ``CHANGELOG.md``, ``README.md`` and ``docs/`` are not
  in this corpus. The changelog is frozen history where a removed
  symbol is a true record, and the living documents are already served
  by ``tests/test_doc_claims.py``'s ``symbol`` rule for the two-token
  form. A bare backticked identifier in markdown stays unchecked.
* **Semantics.** That a symbol exists says nothing about whether the
  sentence around it is true. The ``models.py`` repair that motivated
  this file was wrong about behaviour as well as about a name, and only
  the name half is mechanical.
* **Attribute paths deeper than one dot**, dotted forms whose left half
  is a class rather than a module, and any citation written without
  backticks.

How the ratchet works
---------------------
Two paired guards, the same shape as the two sibling lints:

* ``test_no_unexpected_symbol_citations`` fails on any finding not in
  ``_ALLOWLIST`` — the forward guard.
* ``test_allowlist_has_no_stale_entries`` fails on any entry that no
  longer corresponds to a live finding — the reverse guard, which is
  what stops an allowlist calcifying into permanent suppression.

Entries are keyed by ``(source, rule, subject)``, never by line number,
so editing prose above a citation does not rot the list.

To re-run the whole thing: ``uv run pytest tests/test_symbol_citations.py``.
To see the raw findings including the allowlisted ones, call
``collect_findings()`` — it takes no arguments and reads the tracked
corpus, so it works from a plain ``uv run python -c`` one-liner.

On the corpus's current yield, which decides how much the guards are
worth: it is **not** clean. ``_ALLOWLIST`` carries live entries, so the
reverse guard has something to check and the forward guard has a
measured baseline rather than a vacuous one. Both are still thin
evidence on their own — a rule that quietly stopped firing would keep
them green. The self-tests at the bottom are the real check: each rule
must fire on a seeded offender, stay quiet on a real symbol, and — the
part that makes the other two non-vacuous — stop firing when the rule's
own lookup is neutered, proving the finding came from the missing
binding rather than from the token's shape.

Rule 5 is the one with no live yield at all, and saying so is the point:
its first commit both introduced it and repaired everything it found, so
the corpus now holds zero ``unreferenced-private`` findings and no
``_ALLOWLIST`` entry covers one. Nothing about the live tree would notice
if the rule were deleted tomorrow. Its self-tests are therefore the whole
of its evidence, and they are built to carry that: the reachability
verdict is computed over a synthetic corpus the tests hold in hand, so
the positive and its neuter differ by exactly one added call site — not
by a flag, and not by a name injected into the world. The three quiet
cases (a decorated definition, a name reached only through a lookup
string, a public name) are pinned the same way, each against the corpus
shape it was measured from.

One consequence of the lookup-string channel, stated because it looks
like a bug and is a deliberate bound: an ``_ALLOWLIST`` entry cannot be
written for an ``unreferenced-private`` finding by naming the symbol in a
dict key — ``_lookup_strings`` reads call arguments and assigned
sequences only, so the key does not register as a reference and the
exemption behaves like any other. The alternative, counting every string
literal, would make writing the exemption retire the finding it exempts.

Corollary for anyone editing this file: these rules scan every tracked
``*.py``, this one included, and they read comments as well as
docstrings. Keep synthetic offenders inside plain string constants fed
to ``scan_source``, never in a docstring and never in a comment, or the
lint will flag itself.
"""

from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import tokenize
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from textwrap import dedent

import pytest

from .test_doc_claims import _RELOCATION_LOOKBACK, _RELOCATION_PROSE

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Every filesystem access here resolves from `_REPO_ROOT`, never the process
# CWD, so `pytest tests/` and `pytest` from a subdirectory see one corpus.

_PACKAGE_PREFIX = "src/bettermemory/"

# Conventional stand-in words. A `_`-separated segment drawn from this set
# marks the whole token as a syntax example rather than a citation, which is
# how the `_handle_<kind>` convention is written about in this corpus.
_PLACEHOLDER_WORDS = frozenset(
    {
        "foo",
        "bar",
        "baz",
        "qux",
        "x",
        "y",
        "z",
        "n",
        "mod",
        "spec",
        "example",
        "sample",
        "module",
        "kind",
    }
)

# Suffixes that make a dotted token a filename rather than an attribute.
_FILE_EXTENSIONS = frozenset(
    {
        "py",
        "pyi",
        "md",
        "json",
        "jsonl",
        "yaml",
        "yml",
        "toml",
        "txt",
        "cfg",
        "ini",
        "sh",
        "lock",
        "gz",
        "sqlite",
        "db",
        "log",
        "csv",
        "html",
        "js",
        "css",
    }
)

# A backtick-quoted span, single or double fence. Bounded so a stray backtick
# cannot swallow a paragraph.
_BACKTICK = re.compile(r"``([^`\n]{1,160})``|`([^`\n]{1,160})`")
# A trailing call shape: `build_server(state=...)` cites `build_server`.
_TRAILING_CALL = re.compile(r"\([^()]*\)$")

_DOTTED = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$")
_PRIVATE = re.compile(r"^_[A-Za-z][A-Za-z0-9_]*$")
_MODULE_FILE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.py$")
# A docstring bullet whose first token is a backticked identifier, optionally
# with a call shape: the module-inventory convention.
_INVENTORY_BULLET = re.compile(
    r"^[ \t]*[-*][ \t]+`{1,2}([A-Za-z_][A-Za-z0-9_]*)(?:\([^`]*\))?`{1,2}"
)

# Everything permitted between a tense marker and the citation it modifies:
# whitespace and dashes, nothing else. A content word, a comma or any other
# punctuation between the two means the marker belongs to a different phrase
# and says nothing about the cited name. This is the whole difference between
# "the pre-extraction <name>" and "flushed before the writer returns; <name>",
# and it is what stops the imported vocabulary's ordinary-English
# alternatives from exempting a live citation.
_MARKER_ATTACHES = re.compile(r"[\s–—-]*")


# ---------------------------------------------------------------------------
# Findings and the allowlist
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    """One backticked citation that resolves to nothing."""

    source: str
    line: int
    rule: str
    subject: str
    detail: str

    @property
    def key(self) -> tuple[str, str, str]:
        """Allowlist identity — deliberately excludes ``line``."""
        return (self.source, self.rule, self.subject)

    def __str__(self) -> str:
        return f"{self.source}:{self.line} [{self.rule}] {self.subject} — {self.detail}"


# Citations live at the ratchet base which are correct as written and which
# no extractor rule separates from a real defect. Each says WHY; the reverse
# guard deletes any entry that stops matching a live finding.
_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "src/bettermemory/credentials.py",
        "dotted-symbol",
        "config.SECRET_KEY_V2",
    ): (
        "Synthetic example inside the credential detector's own explanation "
        "of the structured references it must NOT flag as literal secrets. "
        "A real module attribute here would defeat the illustration, so this "
        "citation is permanently unresolvable by intent."
    ),
    (
        "tests/test_health.py",
        "private-symbol",
        "_handle_remove",
    ): (
        "Hypothetical method in the scenario the test guards against — 'a "
        "contributor adds `_handle_remove` ... and forgets the table entry'. "
        "The name must NOT exist for the illustration to work; the sibling "
        "`_handle_foo` in the same block is dodged by the placeholder-segment "
        "rule only because 'foo' is a stand-in word and 'remove' is not."
    ),
    (
        "tests/test_hook.py",
        "private-symbol",
        "_parse_iso_ts",
    ): (
        "Names the bespoke timestamp helper that the fix this test pins "
        "REMOVED in favour of the shared parser. Accurate history stated "
        "without a tense marker the imported vocabulary recognises; "
        "rewording it is a prose call for whoever next edits that docstring, "
        "not something this ratchet should force."
    ),
    (
        "tests/test_server_negative_outcomes.py",
        "private-symbol",
        "_parse_iso_ts",
    ): (
        "Same removed helper as the tests/test_hook.py entry, cited twice in "
        "one docstring describing the pre-fix parse. History, not a claim "
        "that the symbol is there now."
    ),
}


# ---------------------------------------------------------------------------
# Corpus — tracked Python only, so nothing vendored or virtualenv'd enters.
# Same discipline as the two sibling lints, and for the same reason: a
# dependency's docstring must never be able to fail this repo's CI.
# ---------------------------------------------------------------------------
_SKIP_DIR_NAMES = frozenset(
    {".git", ".claude", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}
)


def _git_tracked_py_files() -> tuple[str, ...] | None:
    """Tracked ``*.py`` paths, or ``None`` outside a git checkout."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "ls-files", "-z", "--", "*.py"],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        return None
    if proc.returncode != 0:  # pragma: no cover - not a checkout
        return None
    rels = [rel for rel in proc.stdout.decode("utf-8").split("\0") if rel]
    return tuple(sorted(rel for rel in rels if (_REPO_ROOT / rel).is_file()))


def _walk_py_files() -> tuple[str, ...]:
    """Fallback corpus for a tree with no git metadata, pruned as it goes.

    A directory holding ``pyvenv.cfg`` is a virtualenv root per PEP 405
    whatever it is named, so that marker catches environments a name list
    would miss.
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        here = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _SKIP_DIR_NAMES and not (here / d / "pyvenv.cfg").is_file()
        ]
        out.extend(
            (here / name).relative_to(_REPO_ROOT).as_posix()
            for name in filenames
            if name.endswith(".py")
        )
    return tuple(sorted(out))


@lru_cache(maxsize=None)
def _all_py_files() -> tuple[str, ...]:
    tracked = _git_tracked_py_files()
    return _walk_py_files() if tracked is None else tracked


def _bound_names_in(text: str) -> frozenset[str]:
    """Every name bound anywhere in one Python source, by AST.

    Deliberately generous — a citation is satisfied by any binding form,
    including a parameter or an attribute, because prose names all of them
    and the question this rule asks is "does this name exist at all".
    """
    names: set[str] = set()
    tree = ast.parse(text)
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
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return frozenset(names)


@lru_cache(maxsize=None)
def _bound_names(rel: str) -> frozenset[str]:
    return _bound_names_in((_REPO_ROOT / rel).read_text(encoding="utf-8"))


def _lookup_strings(tree: ast.AST) -> set[str]:
    """String literals sitting where something could look a name up.

    Two positions, both measured against this corpus rather than imagined:
    a call argument (``getattr(obj, "_x")``, ``monkeypatch.setattr(m, "_x",
    v)``, ``pytest.mark.parametrize``) and an element of a list/tuple/set
    that is the direct right-hand side of an assignment (the ``__all__``
    re-export lists that carry private names in ``_handlers.py`` and
    ``handlers/_shared.py``). A name written in either place is registered
    somewhere no AST edge records, so it must not read as unreferenced.

    Both positions exclude a string nested in a dict key, which is what
    ``_ALLOWLIST`` entries are. That is load-bearing rather than incidental:
    were the subject of an exemption to count as a reference, writing the
    entry would retire the very finding it exempts and the reverse guard
    would then delete it, leaving no way to exempt one of these at all.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        values: list[ast.expr] = []
        if isinstance(node, ast.Call):
            values = [*node.args, *(kw.value for kw in node.keywords)]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
            node.value, (ast.List, ast.Tuple, ast.Set)
        ):
            values = list(node.value.elts)
        for value in values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                out.add(value.value)
    return out


@dataclass(frozen=True)
class NameUse:
    """How a corpus uses every identifier in it, partitioned four ways.

    Kept separate from the verdict so the two measurements the rule's design
    rests on — that the undecorated clause is load-bearing, and that the same
    scan is useless on public names — are computable by a test instead of
    written into prose as counts that rot as the tree grows.
    """

    plain_defs: frozenset[str]
    """Names with at least one UNDECORATED ``def``/``async def``/``class``."""

    decorated_defs: frozenset[str]
    """Names with at least one decorated definition — a registration channel
    with no AST edge back to a caller. ``@field_validator``,
    ``@pytest.fixture`` and ``@app.tool`` all produce callables that a
    framework calls correctly and nothing else calls at all."""

    rebound: frozenset[str]
    """Names bound by something other than a definition — an assignment, a
    parameter, an attribute store, an import alias, a ``global``. Something
    besides the definition owns the name and this scan cannot reason about
    it."""

    used: frozenset[str]
    """Names appearing as a load — bare name, attribute access, imported
    name, decorator expression — or in a lookup string."""


def name_use(sources: dict[str, str]) -> NameUse:
    """Partition every identifier in ``{path: text}`` by how it is used.

    Takes the corpus as a mapping rather than reading the tree, so a
    self-test can hand it a two-file world it controls and watch the verdict
    move when one call site is added. That is the only honest way to show
    the rule below turns on the reference scan rather than on the shape of
    the citation.
    """
    plain: set[str] = set()
    decorated: set[str] = set()
    rebound: set[str] = set()
    used: set[str] = set()
    for text in sources.values():
        tree = ast.parse(text)
        used |= _lookup_strings(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                (decorated if node.decorator_list else plain).add(node.name)
            elif isinstance(node, ast.Name):
                (rebound if isinstance(node.ctx, ast.Store) else used).add(node.id)
            elif isinstance(node, ast.Attribute):
                (rebound if isinstance(node.ctx, ast.Store) else used).add(node.attr)
            elif isinstance(node, ast.arg):
                rebound.add(node.arg)
            elif isinstance(node, ast.keyword) and node.arg is not None:
                rebound.add(node.arg)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                rebound.update(node.names)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    rebound.add((alias.asname or alias.name).split(".")[0])
                    used.add(alias.name.split(".")[-1])
    return NameUse(
        plain_defs=frozenset(plain),
        decorated_defs=frozenset(decorated),
        rebound=frozenset(rebound),
        used=frozenset(used),
    )


def unreferenced_names(
    use: NameUse, *, allow_decorated: bool = False
) -> frozenset[str]:
    """Names a corpus defines and then never mentions again.

    A name qualifies when every statement binding it is a definition, none
    of those definitions is decorated, and nothing loads it, imports it or
    names it in a lookup string. ``allow_decorated`` drops the second clause
    and exists for one caller: the guard that shows the clause is still
    doing work on the live tree.

    NOT filtered to private names — that filter belongs to the rule, not to
    the scan, so the guard below can run the identical scan over public
    names and measure what it would cost there.

    What this decides is exactly "the corpus never mentions this name
    outside its own definition". What it does **not** decide is
    reachability, which is undecidable in general: a name called only from
    inside another dead function reads as referenced here and always will.
    The rule is a subset of dead code, chosen because its evidence is a
    single AST pass and its negative — one reference anywhere — is
    unambiguous.
    """
    defined = use.plain_defs | use.decorated_defs if allow_decorated else use.plain_defs
    barred = use.rebound | use.used
    if not allow_decorated:
        barred = barred | use.decorated_defs
    return frozenset(defined - barred)


def unreferenced_private_names(sources: dict[str, str]) -> frozenset[str]:
    """The rule's verdict: ``unreferenced_names`` narrowed to private names.

    The leading underscore is the whole of the narrowing, and it is what
    keeps the rule usable —
    ``test_the_same_scan_over_public_names_is_measured_noise`` runs the
    identical scan without it.
    """
    return frozenset(
        name for name in unreferenced_names(name_use(sources)) if _PRIVATE.match(name)
    )


@dataclass(frozen=True)
class World:
    """The name universe a scan resolves citations against.

    Injectable so the self-tests can neuter one lookup at a time and show
    that a finding disappears — which is what proves the positive self-tests
    are testing the rule rather than the token's shape.

    ``symbols`` answers "is this name bound anywhere"; ``unreferenced``
    answers the narrower follow-up "and does anything reach it". The two are
    separate fields, with no default between them, so a construction site
    that forgets the second cannot silently disable the reachability verdict.
    """

    symbols: frozenset[str]
    module_stems: frozenset[str]
    basenames: frozenset[str]
    local: frozenset[str]
    unreferenced: frozenset[str]


@lru_cache(maxsize=None)
def _repo_symbols() -> frozenset[str]:
    names: set[str] = set()
    for rel in _all_py_files():
        names |= _bound_names(rel)
    return frozenset(names)


@lru_cache(maxsize=None)
def _repo_unreferenced_privates() -> frozenset[str]:
    return unreferenced_private_names(
        {rel: (_REPO_ROOT / rel).read_text(encoding="utf-8") for rel in _all_py_files()}
    )


@lru_cache(maxsize=None)
def _package_module_stems() -> frozenset[str]:
    return frozenset(
        rel.rsplit("/", 1)[-1][: -len(".py")]
        for rel in _all_py_files()
        if rel.startswith(_PACKAGE_PREFIX)
    )


@lru_cache(maxsize=None)
def _tracked_basenames() -> frozenset[str]:
    return frozenset(rel.rsplit("/", 1)[-1] for rel in _all_py_files())


def world_for(source: str) -> World:
    """The real repo world, with ``local`` bound to ``source`` when tracked."""
    local = _bound_names(source) if source in _all_py_files() else frozenset()
    return World(
        symbols=_repo_symbols(),
        module_stems=_package_module_stems(),
        basenames=_tracked_basenames(),
        local=local,
        unreferenced=_repo_unreferenced_privates(),
    )


# ---------------------------------------------------------------------------
# Prose extraction
# ---------------------------------------------------------------------------
def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _prose_units(text: str) -> list[tuple[str, int, str]]:
    """``(kind, first_line, prose)`` for every docstring and comment line.

    A comment's unit is ONE line. Contiguous comment lines were measured as
    a single unit first and the lookback then reached across them, letting a
    tense marker in a neighbouring sentence excuse a live citation.
    """
    out: list[tuple[str, int, str]] = []
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        doc = ast.get_docstring(node, clean=False)
        if not doc:
            continue
        first = node.body[0]
        out.append(("docstring", getattr(first, "lineno", 1), doc))
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            out.append(("comment", token.start[0], token.string.lstrip("#")))
    return out


def _module_docstring(text: str) -> tuple[int, str] | None:
    tree = ast.parse(text)
    doc = ast.get_docstring(tree, clean=False)
    if not doc or not tree.body:
        return None
    return getattr(tree.body[0], "lineno", 1), doc


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def _is_historical(text: str, start: int, lookback: int | None = None) -> bool:
    """A tense marker ATTACHED to the citation makes it a claim about the past.

    Attached, not merely nearby. The imported vocabulary is a word list tuned
    for a different consumer and it includes ordinary English, so searching
    the whole lookback for it exempted any citation that happened to share a
    sentence with `before`, `once`, `was` or `old`. The marker must instead
    pre-modify the cited name, with only whitespace or a dash between them.

    ``lookback`` defaults to the imported ``_RELOCATION_LOOKBACK``, read here
    rather than captured as a default argument so the seam test sees the same
    value this module actually runs on. Overriding it is how that test proves
    the guarantee is grammatical rather than numeric: widening the window
    does not widen the exemption, because attachment is what bounds it.
    """
    reach = _RELOCATION_LOOKBACK if lookback is None else lookback
    window = text[max(0, start - reach) : start]
    return any(
        _MARKER_ATTACHES.fullmatch(window[match.end() :])
        for match in _RELOCATION_PROSE.finditer(window)
    )


def _has_placeholder_segment(token: str) -> bool:
    return any(seg in _PLACEHOLDER_WORDS for seg in token.strip("_").split("_") if seg)


def _is_local_tail(token: str, world: World) -> bool:
    """The token is the tail of a name bound in the same file — an elision."""
    return any(name.endswith(token) and name != token for name in world.local)


def _citation_tokens(text: str) -> list[tuple[str, int]]:
    """``(token, offset)`` for each backticked span, trailing call shape stripped."""
    out: list[tuple[str, int]] = []
    for match in _BACKTICK.finditer(text):
        raw = (match.group(1) or match.group(2) or "").strip()
        out.append((_TRAILING_CALL.sub("", raw).strip(), match.start()))
    return out


def scan_source(source: str, text: str, world: World | None = None) -> list[Finding]:
    """Every dangling citation in one Python source's docstrings and comments."""
    w = world_for(source) if world is None else world
    findings: list[Finding] = []
    for kind, base_line, prose in _prose_units(text):
        for token, offset in _citation_tokens(prose):
            line = base_line + _line_of(prose, offset) - 1
            if _is_historical(prose, offset):
                continue
            findings.extend(_judge(source, line, kind, token, w))
    findings.extend(_scan_inventory_bullets(source, text, w))
    return sorted(findings, key=lambda f: (f.line, f.rule, f.subject))


def _judge(
    source: str, line: int, kind: str, token: str, world: World
) -> list[Finding]:
    file_match = _MODULE_FILE.match(token)
    if file_match is not None:
        if file_match.group(1) in _PLACEHOLDER_WORDS or token in world.basenames:
            return []
        return [
            Finding(
                source,
                line,
                "module-file",
                token,
                f"no tracked file is named {token} (cited in a {kind})",
            )
        ]
    dotted = _DOTTED.match(token)
    if dotted is not None and dotted.group(1) in world.module_stems:
        name = dotted.group(2)
        if name in _FILE_EXTENSIONS or _has_placeholder_segment(name):
            return []
        if name in world.symbols:
            return _unreferenced_findings(source, line, kind, token, name, world)
        return [
            Finding(
                source,
                line,
                "dotted-symbol",
                token,
                f"{name} is bound nowhere in the tracked Python (cited in a {kind})",
            )
        ]
    if _PRIVATE.match(token):
        if token in world.module_stems:
            return []
        if _has_placeholder_segment(token) or _is_local_tail(token, world):
            return []
        if token in world.symbols:
            return _unreferenced_findings(source, line, kind, token, token, world)
        return [
            Finding(
                source,
                line,
                "private-symbol",
                token,
                f"{token} is bound nowhere in the tracked Python (cited in a {kind})",
            )
        ]
    return []


def _unreferenced_detail(name: str, world: World) -> str | None:
    """Why a name that IS bound still resolves to nothing, or ``None``.

    The one seam between "bound" and "reachable". Every rule that used to
    stop at ``name in world.symbols`` routes its yes-branch through here, so
    there is no rule for which the two questions can drift apart.
    """
    if name not in world.unreferenced:
        return None
    return (
        f"{name} is defined but nothing in the tracked Python references it — "
        f"the binding survives, the code path does not"
    )


def _unreferenced_findings(
    source: str, line: int, kind: str, subject: str, name: str, world: World
) -> list[Finding]:
    detail = _unreferenced_detail(name, world)
    if detail is None:
        return []
    return [
        Finding(
            source, line, "unreferenced-private", subject, f"{detail} (in a {kind})"
        )
    ]


def _scan_inventory_bullets(source: str, text: str, world: World) -> list[Finding]:
    """Module-docstring bullets that lead with a backticked identifier.

    Package sources only: the convention is a module announcing its own
    surface. Elsewhere a leading backticked token is as likely to be a wire
    key or a CLI flag, which is the noise this rule exists to stay out of.
    """
    if not source.startswith(_PACKAGE_PREFIX):
        return []
    doc = _module_docstring(text)
    if doc is None:
        return []
    base_line, prose = doc
    findings: list[Finding] = []
    for index, raw_line in enumerate(prose.splitlines()):
        match = _INVENTORY_BULLET.match(raw_line)
        if match is None:
            continue
        name = match.group(1)
        if "_" not in name or name == name.upper():
            continue
        # No reachability verdict here, and none is missing: a bullet lead
        # that is private is already a backticked token in this same module
        # docstring, so the token walk above judges it and adding a second
        # judgement would only mint a duplicate under one allowlist key. The
        # leads this rule uniquely owns are the public ones, where the
        # reference scan is measured useless — see
        # `test_the_same_scan_over_public_names_is_measured_noise`.
        if name in world.symbols or _has_placeholder_segment(name):
            continue
        findings.append(
            Finding(
                source,
                base_line + index,
                "inventory-bullet",
                name,
                f"{name} leads a module-inventory bullet but is bound nowhere",
            )
        )
    return findings


def collect_findings() -> list[Finding]:
    out: list[Finding] = []
    for rel in _all_py_files():
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        out.extend(scan_source(rel, text))
    return out


# ---------------------------------------------------------------------------
# The two paired ratchet tests
# ---------------------------------------------------------------------------
def test_no_unexpected_symbol_citations() -> None:
    """Forward guard: no new dangling backticked citation may land.

    Prefer fixing the citation — ``git grep`` the name you meant and write
    that one. An ``_ALLOWLIST`` entry is for a citation that is CORRECT as
    written (a removed symbol named as history, a synthetic example that
    must not resolve); say which, and expect the reverse guard to force the
    entry out if the prose is ever reworded.
    """
    unexpected = [f for f in collect_findings() if f.key not in _ALLOWLIST]
    if unexpected:
        rendered = "\n".join(f"  - {finding}" for finding in unexpected)
        pytest.fail(
            f"{len(unexpected)} backticked citation(s) resolve to nothing:\n"
            f"{rendered}\n\n"
            f"Each costs a reader a wrong mental model and an agent a wasted "
            f"grep. Verify the real name with `git grep` and write it, rather "
            f"than widening the checker."
        )


def test_allowlist_has_no_stale_entries() -> None:
    """Reverse guard: the allowlist may not outlive the findings it covers.

    Two causes, opposite responses: (1) the citation was repaired or the
    prose reworded — delete the entry, that is the ratchet; (2) an extractor
    rule narrowed while the dangling citation survives — deleting then hides
    it, so check the source before deleting.
    """
    live = {finding.key for finding in collect_findings()}
    stale = sorted(key for key in _ALLOWLIST if key not in live)
    if stale:
        rendered = "\n".join(
            f"  - {key} (exempt because: {_ALLOWLIST[key]})" for key in stale
        )
        pytest.fail(
            f"{len(stale)} _ALLOWLIST entr(y/ies) no longer correspond to a "
            f"live finding:\n{rendered}\n\nIf the citation was repaired, delete "
            f"the entry. If the code merely stopped matching the extractor, "
            f"deleting hides a live citation — verify against the source first."
        )


def test_allowlist_entries_carry_a_reason() -> None:
    """Every exemption must say why, so review can judge it."""
    for key, reason in _ALLOWLIST.items():
        assert len(reason.strip()) >= 40, f"{key} needs a substantive reason"


def test_this_module_is_inside_the_scanned_corpus() -> None:
    """The lint scans itself, so its own prose obeys its own rules."""
    corpus = _all_py_files()
    assert corpus, "corpus is empty — Python-file discovery broke"
    assert "tests/test_symbol_citations.py" in corpus
    assert not [rel for rel in corpus if "site-packages" in rel]


def test_walk_fallback_admits_nothing_the_git_listing_excludes() -> None:
    """The no-git fallback may miss tracked files, never admit untracked ones."""
    tracked = _git_tracked_py_files()
    if tracked is None:  # pragma: no cover - only outside a git checkout
        pytest.skip("not a git checkout")
    walked = _walk_py_files()
    assert not set(walked) - set(tracked), (
        f"the walk admits untracked files the git listing excludes: "
        f"{sorted(set(walked) - set(tracked))[:10]}"
    )


# The seam fixtures, held as data so the guard below and the rule self-tests
# further down cannot drift apart. `{}` marks where the citation sits.
#
# Constructions that MUST read as historical: the marker pre-modifies the
# cited name. Only the `pre-\w+` alternative is load-bearing for the corpus as
# it stands — dropping each alternative in turn and rescanning reopens two
# citations, both spelled "the pre-extraction ...", and nothing else. The rest
# are the canonical spellings this module teaches, pinned so that narrowing
# the shared vocabulary is a loud failure here rather than a silent one.
_ATTACHED_TENSE_PROSE = (
    "Identical behavior to the pre-extraction {} entry point.",
    "Superseded by the former {} helper.",
    "The now-removed {} took the same argument.",
    "History only: the renamed {} became the shared parser.",
    "The old {} spelling is gone from the tree.",
)

# Constructions that must NOT read as historical: an ordinary sentence that
# happens to contain one of the imported vocabulary's common-English
# alternatives, with a live citation beside it. Every one of these was
# silently exempt while the vocabulary was searched as a blanket over the
# lookback, which is the overreach the attachment rule closed.
_INNOCENT_TENSE_WORD_PROSE = (
    "Flushed before the writer returns; {} guards against recursion.",
    "Once the queue drains, {} takes over.",
    "The verdict was surprising, so {} logs it.",
    "Blocks until the lock clears, then {} runs.",
    "Rows dropped by the filter never reach {} at all.",
    "The cursor moved forward, so {} re-reads the row.",
    "Renamed scopes are rewritten in place by {}.",
    "The old-style rows still land in {} today.",
)


def _reads_as_historical(template: str, lookback: int | None = None) -> bool:
    """Run the real suppression predicate over one ``{}`` template."""
    prose = template.format("`_cli_serve`")
    return _is_historical(prose, prose.index("`"), lookback)


def test_imported_tense_seam_holds_in_both_directions() -> None:
    """Both halves of the import, and both directions of each.

    ``tests/test_doc_claims.py`` owns the vocabulary and the window; importing
    rather than copying keeps one definition, but it also means an edit over
    there changes verdicts here. Asserting that a list of phrases still
    *matches* the regex covers exactly one of the four ways this seam can
    break, and it is the least dangerous one: it says nothing about a
    vocabulary that grew until it matches everything, nothing about the
    constant, and nothing about the suppression this module actually applies.
    So the guard drives the real predicate over measured constructions
    instead.

    * a marker lost from the vocabulary breaks the attached fixtures;
    * a marker widened until it matches ordinary English breaks the innocent
      ones — the fail-open direction, which is the one that quietly reopens
      the class this file exists to catch;
    * a lookback shrunk below the fixtures' reach breaks the attached ones,
      and is asserted separately so the failure names the constant.

    The remaining direction — a lookback *widened* — is bounded by the
    attachment rule rather than by the number, and
    ``test_attachment_not_the_window_width_bounds_the_suppression`` pins that.
    """
    for template in _ATTACHED_TENSE_PROSE:
        assert _reads_as_historical(template), (
            f"the shared tense vocabulary no longer suppresses {template!r}; "
            f"tests/test_symbol_citations.py relies on that construction"
        )
    for template in _INNOCENT_TENSE_WORD_PROSE:
        assert not _reads_as_historical(template), (
            f"{template!r} is an ordinary sentence, not a claim about the "
            f"past, yet it now exempts the citation beside it — the shared "
            f"vocabulary or the attachment rule has widened"
        )
    reach = max(template.index("{}") for template in _ATTACHED_TENSE_PROSE)
    assert _RELOCATION_LOOKBACK >= reach, (
        f"_RELOCATION_LOOKBACK is {_RELOCATION_LOOKBACK}, too short to reach "
        f"the marker in a {reach}-character construction this module relies "
        f"on; the historical exemption dies silently when it shrinks"
    )


def test_attachment_not_the_window_width_bounds_the_suppression() -> None:
    """The fail-open direction of the imported constant.

    ``_RELOCATION_LOOKBACK`` used to be the only thing keeping the exemption
    from going paragraph-wide, and raising it failed nothing. Attachment
    replaces the number with a grammatical bound, so a marker a whole
    paragraph away cannot reach the citation however wide the window is.
    """
    far = "The old contract is gone. " + "Filler prose that says nothing. " * 8
    assert not _reads_as_historical(far + "{} decides the verdict.")
    assert not _reads_as_historical(far + "{} decides the verdict.", lookback=10_000), (
        "a marker a paragraph away exempts the citation once the window is "
        "wide enough — the bound is back to being the constant, not the "
        "attachment rule"
    )
    # ...while the attributive form is still reached at any width.
    assert _reads_as_historical(_ATTACHED_TENSE_PROSE[0], lookback=10_000)


# ---------------------------------------------------------------------------
# Self-tests: each rule must demonstrably fire, stay quiet on a real symbol,
# and — the part that makes the first two mean something — stop firing when
# its own lookup is neutered. A rule that is merely satisfied today is
# indistinguishable from a rule that does nothing. Offender code lives in
# string constants precisely so the lint's own scan cannot mistake it for
# live prose (see the module docstring's corollary).
# ---------------------------------------------------------------------------
_FAKE_SRC = "src/bettermemory/fake.py"
_FAKE_TEST = "tests/test_fake.py"


def _rules(source: str, text: str, world: World | None = None) -> list[tuple[str, str]]:
    return [(f.rule, f.subject) for f in scan_source(source, dedent(text), world)]


def _real_world(local: frozenset[str] = frozenset()) -> World:
    return World(
        symbols=_repo_symbols(),
        module_stems=_package_module_stems(),
        basenames=_tracked_basenames(),
        local=local,
        unreferenced=_repo_unreferenced_privates(),
    )


def _world_knowing(*names: str) -> World:
    """The real world plus ``names`` — the neutering used below."""
    base = _real_world()
    return World(
        symbols=base.symbols | frozenset(names),
        module_stems=base.module_stems,
        basenames=base.basenames,
        local=base.local,
        unreferenced=base.unreferenced,
    )


def _world_over(sources: dict[str, str]) -> World:
    """A world whose binding AND reference verdicts come from ``sources``.

    The other helpers vary one axis against the real tree. This one replaces
    the tree, because the reachability rule's verdict is a property of the
    whole corpus: the only way to show a finding turns on a call site is to
    hold a corpus in hand and add one.
    """
    symbols: set[str] = set()
    for text in sources.values():
        symbols |= _bound_names_in(text)
    return World(
        symbols=frozenset(symbols),
        module_stems=_package_module_stems(),
        basenames=_tracked_basenames(),
        local=frozenset(),
        unreferenced=unreferenced_private_names(sources),
    )


# The shape the 2026-07 repair commit invented while fixing other false
# claims: a plausible dotted helper that never existed. This is the seeded
# regression the forward guard exists for.
_SEEDED_DOTTED = '''
"""Rows are flattened through `store.list_row_to_dict` before display."""
'''

_SEEDED_DOTTED_REAL = '''
"""Rows are flattened through `store.summary_to_dict` before display."""
'''


def test_flags_a_dotted_citation_that_resolves_to_nothing() -> None:
    assert _rules(_FAKE_SRC, _SEEDED_DOTTED) == [
        ("dotted-symbol", "store.list_row_to_dict")
    ]


def test_stays_quiet_on_the_real_symbol_the_seed_should_have_named() -> None:
    assert _rules(_FAKE_SRC, _SEEDED_DOTTED_REAL) == []


def test_dotted_rule_is_the_binding_lookup_not_the_token_shape() -> None:
    """Neuter the lookup and the seeded finding must vanish.

    Without this, ``test_flags_a_dotted_citation_that_resolves_to_nothing``
    would pass just as well against a rule that flagged every dotted token.
    """
    assert _rules(_FAKE_SRC, _SEEDED_DOTTED, _world_knowing("list_row_to_dict")) == []


def test_dotted_rule_needs_a_real_package_module_on_the_left() -> None:
    """``sqlite3.Row`` is not a citation of this project's code."""
    assert (
        _rules(_FAKE_SRC, '\n"""Handed back as `sqlite3.OperationalError`."""\n') == []
    )


def test_dotted_rule_ignores_file_suffixes() -> None:
    text = '\n"""Written to `server.json` and read back from `config.yaml`."""\n'
    assert _rules(_FAKE_SRC, text) == []


def test_flags_a_private_citation_that_resolves_to_nothing() -> None:
    text = "\nx = 1  # the `_count_recent_searches` shield decides the verdict\n"
    assert _rules(_FAKE_TEST, text) == [("private-symbol", "_count_recent_searches")]


def test_private_rule_stays_quiet_on_the_real_symbol() -> None:
    text = "\nx = 1  # the `_count_recent_retrievals` shield decides the verdict\n"
    assert _rules(_FAKE_TEST, text) == []


def test_private_rule_is_the_binding_lookup_not_the_underscore() -> None:
    text = "\nx = 1  # the `_count_recent_searches` shield decides the verdict\n"
    assert _rules(_FAKE_TEST, text, _world_knowing("_count_recent_searches")) == []


def test_private_rule_reads_docstrings_as_well_as_comments() -> None:
    assert _rules(_FAKE_TEST, '\n"""Recorded by the `_list_active` handler."""\n') == [
        ("private-symbol", "_list_active")
    ]


def test_private_module_reference_without_the_suffix_is_not_a_symbol() -> None:
    """``_handlers`` names a module; ``_handlers.py`` is a real file."""
    assert (
        _rules(_FAKE_TEST, '\n"""Threaded through `_handlers` on the way in."""\n')
        == []
    )


def test_placeholder_segments_mark_a_convention_not_a_citation() -> None:
    text = '\n"""Key `"foo"` maps to method `_handle_foo` with no renaming."""\n'
    assert _rules(_FAKE_TEST, text) == []


def test_a_local_tail_is_an_elision_not_a_citation() -> None:
    """Prose naming a sibling in the same file by its distinguishing tail."""
    text = '\n"""The sibling `_end_fence` case only exercises the END pair."""\n'
    assert _rules(_FAKE_TEST, text, _real_world()) == [("private-symbol", "_end_fence")]
    local = _real_world(local=frozenset({"test_rejects_body_with_matching_end_fence"}))
    assert _rules(_FAKE_TEST, text, local) == []


# The bound-but-unreached shape, held as synthetic corpora so the reference
# scan can be driven over a tree whose call sites are under this file's
# control. Sources live in string constants for the same reason the prose
# fixtures do: the lint scans itself, and a real `def` here would enter the
# live corpus.
_DEAD_DELEGATE_SRC = '''
def load_rows(store):
    """Module-level implementation; the one caller reaches it directly."""
    return store.rows()


class Bundle:
    def _load_rows(self):
        """Bound alias over this bundle's store."""
        return load_rows(self.store)
'''

_DEAD_DELEGATE_CORPUS = {"src/bettermemory/fake.py": _DEAD_DELEGATE_SRC}

# The same tree with one call site added — the whole difference between the
# two verdicts below.
_LIVE_DELEGATE_CORPUS = {
    "src/bettermemory/fake.py": _DEAD_DELEGATE_SRC,
    "src/bettermemory/caller.py": "def go(bundle):\n    return bundle._load_rows()\n",
}

# Registered by a decorator, called by a framework, reachable from no AST
# edge: the pydantic/pytest shape that made the undecorated clause necessary.
_DECORATED_CORPUS = {
    "src/bettermemory/fake.py": '''
class Model:
    @field_validator("scopes")
    @classmethod
    def _load_rows(cls, v):
        """A validator pydantic calls and nothing else does."""
        return v
'''
}

# Reached only through a string: getattr dispatch and an export list, the two
# lookup positions `_lookup_strings` reads.
_GETATTR_CORPUS = {
    "src/bettermemory/fake.py": """
class Bundle:
    def _load_rows(self):
        return ()


def dispatch(bundle):
    return getattr(bundle, "_load_rows")()
"""
}

_EXPORT_CORPUS = {
    "src/bettermemory/fake.py": '__all__ = ["_load_rows"]\n\n\ndef _load_rows():\n    return ()\n'
}

# A public delegate with the identical dead shape, to pin that the rule
# declines to judge it.
_PUBLIC_DEAD_CORPUS = {
    "src/bettermemory/fake.py": "def orphan_helper():\n    return ()\n"
}

_UNREACHED_PROSE = '\n"""Candidates come from `_load_rows` before ranking."""\n'


def test_flags_a_private_citation_that_nothing_in_the_tree_reaches() -> None:
    """The demonstrated shape: the binding survives, the code path does not."""
    assert _rules(_FAKE_TEST, _UNREACHED_PROSE, _world_over(_DEAD_DELEGATE_CORPUS)) == [
        ("unreferenced-private", "_load_rows")
    ]


def test_reachability_is_the_reference_scan_not_the_delegation_shape() -> None:
    """Neuter the rule's own lookup by adding one call site, nothing else.

    Both corpora define the same method with the same body and the same
    delegating shape. Only the second is reached. Without this the positive
    above would pass just as well against a rule that flagged every private
    one-line delegate.
    """
    assert (
        _rules(_FAKE_TEST, _UNREACHED_PROSE, _world_over(_LIVE_DELEGATE_CORPUS)) == []
    )


def test_a_decorated_private_is_registered_rather_than_dead() -> None:
    """``@field_validator`` and ``@pytest.fixture`` have no direct caller.

    Dropping this clause is what takes the live yield from 1 name to 8: six
    pydantic validators in ``models.py`` and an autouse fixture spread over
    three semantic test modules, every one of them correctly called.
    """
    assert _rules(_FAKE_TEST, _UNREACHED_PROSE, _world_over(_DECORATED_CORPUS)) == []


def test_a_private_reached_only_through_a_lookup_string_stays_quiet() -> None:
    assert _rules(_FAKE_TEST, _UNREACHED_PROSE, _world_over(_GETATTR_CORPUS)) == []
    assert _rules(_FAKE_TEST, _UNREACHED_PROSE, _world_over(_EXPORT_CORPUS)) == []


def test_the_dotted_spelling_carries_the_same_reachability_verdict() -> None:
    """The module-qualified spelling and the bare one are one claim.

    Named without backticks on purpose — this lint scans itself, and the
    synthetic symbol below exists only inside a string constant.
    """
    text = '\n"""Routed through `_handlers._load_rows` on the way in."""\n'
    assert _rules(_FAKE_SRC, text, _world_over(_DEAD_DELEGATE_CORPUS)) == [
        ("unreferenced-private", "_handlers._load_rows")
    ]


def test_the_reachability_rule_declines_to_judge_public_names() -> None:
    """A public delegate with the identical dead shape draws no verdict."""
    text = '\n"""Rows are flattened through `store.orphan_helper` first."""\n'
    assert _rules(_FAKE_SRC, text, _world_over(_PUBLIC_DEAD_CORPUS)) == []


@lru_cache(maxsize=None)
def _repo_name_use() -> NameUse:
    return name_use(
        {rel: (_REPO_ROOT / rel).read_text(encoding="utf-8") for rel in _all_py_files()}
    )


def test_the_same_scan_over_public_names_is_measured_noise() -> None:
    """Why the rule stops at the leading underscore, measured on this tree.

    pytest collects its tests by name and an exported API is called by
    consumers outside the corpus, so "defined here, mentioned nowhere else"
    is the NORMAL state of a public name rather than a defect. Asserted as a
    ratio rather than a count so growth in the tree cannot rot it: at the
    time of writing the scan flags roughly four public names in five.
    """
    use = _repo_name_use()
    public = {
        name for name in use.plain_defs | use.decorated_defs if not name.startswith("_")
    }
    flagged = {name for name in unreferenced_names(use) if not name.startswith("_")}
    assert len(flagged) * 2 > len(public), (
        f"the same scan now flags only {len(flagged)} of {len(public)} public "
        f"definitions — if it has become precise on public names, the rule's "
        f"private-only narrowing is costing recall for no reason and should "
        f"be revisited rather than left as documented"
    )


def test_the_undecorated_clause_still_earns_its_place() -> None:
    """Dropping it widens the live yield — the measurement the rule rests on.

    Every name it lets back in is one with a decorated definition: a pydantic
    ``@field_validator``, a pytest fixture, a tool registered on a server.
    Each is correctly called by a framework and by nothing this scan can see,
    so each would be a false positive. Pinned as "the difference is
    non-empty" rather than as a list, because the list is exactly the sort of
    thing that rots.
    """
    use = _repo_name_use()
    strict = {n for n in unreferenced_names(use) if _PRIVATE.match(n)}
    loose = {
        n for n in unreferenced_names(use, allow_decorated=True) if _PRIVATE.match(n)
    }
    assert loose - strict, (
        "dropping the undecorated clause no longer changes the live yield, so "
        "nothing here demonstrates why the clause exists — re-measure before "
        "trusting the docstring's account of it"
    )
    assert not strict - loose, "the strict scan must be a subset of the loose one"


def test_historical_prose_is_not_a_present_tense_claim() -> None:
    text = (
        '\n"""Identical behavior to the pre-extraction `_cli_serve` entry point."""\n'
    )
    assert _rules(_FAKE_TEST, text) == []


def test_the_same_citation_without_a_tense_marker_still_fires() -> None:
    text = '\n"""Identical behavior to the `_cli_serve` entry point."""\n'
    assert _rules(_FAKE_TEST, text) == [("private-symbol", "_cli_serve")]


def test_an_unattached_tense_word_does_not_exempt_the_citation_beside_it() -> None:
    """The overreach the attachment rule closed, end to end through the lint.

    Every sentence in the innocent set holds one of the imported vocabulary's
    common-English alternatives next to a dangling citation, and every one of
    them was silently exempt before. The two halves share a word on purpose —
    "The old <name> spelling" suppresses and "The old-style rows still land
    in <name>" does not — which is what shows the verdict turns on the
    construction rather than on the vocabulary entry.
    """
    for template in _INNOCENT_TENSE_WORD_PROSE:
        text = '\n"""' + template.format("`_cli_serve`") + '"""\n'
        assert _rules(_FAKE_TEST, text) == [("private-symbol", "_cli_serve")], template
    for template in _ATTACHED_TENSE_PROSE:
        text = '\n"""' + template.format("`_cli_serve`") + '"""\n'
        assert _rules(_FAKE_TEST, text) == [], template


def test_the_tense_exemption_covers_every_token_rule_it_sits_above() -> None:
    """The suppression is in the token walk, so rules 1-3 share its verdict.

    Both shapes below were exempt under the blanket reading — a fabricated
    dotted symbol and a filename no tracked file carries, each beside an
    ordinary past-tense clause.
    """
    dotted = '\n"""Rows dropped by the filter reach `store.list_row_to_dict` raw."""\n'
    assert _rules(_FAKE_SRC, dotted) == [("dotted-symbol", "store.list_row_to_dict")]
    named = '\n"""The cursor moved forward, so `test_health_commit_drift.py` runs."""\n'
    assert _rules(_FAKE_TEST, named) == [("module-file", "test_health_commit_drift.py")]


def test_a_tense_marker_on_the_line_above_a_comment_does_not_reach_it() -> None:
    """Comment units are one line, so lookback cannot cross a line break."""
    text = (
        "\nx = 1\n"
        "# then tombstone the memory before the caller takes the lock.\n"
        "# `_cli_serve` guards against recursion here.\n"
    )
    assert _rules(_FAKE_TEST, text) == [("private-symbol", "_cli_serve")]


def test_flags_a_bare_module_filename_that_does_not_exist() -> None:
    text = '\n"""Unit-tested in `test_health_commit_drift.py` end to end."""\n'
    assert _rules(_FAKE_TEST, text) == [("module-file", "test_health_commit_drift.py")]


def test_module_file_rule_stays_quiet_on_a_real_basename() -> None:
    assert (
        _rules(_FAKE_TEST, '\n"""Unit-tested in `test_verify.py` end to end."""\n')
        == []
    )


def test_module_file_rule_is_the_basename_lookup_not_the_suffix() -> None:
    text = '\n"""Unit-tested in `test_health_commit_drift.py` end to end."""\n'
    neutered = World(
        symbols=_repo_symbols(),
        module_stems=_package_module_stems(),
        basenames=_tracked_basenames() | frozenset({"test_health_commit_drift.py"}),
        local=frozenset(),
        unreferenced=_repo_unreferenced_privates(),
    )
    assert _rules(_FAKE_TEST, text, neutered) == []


def test_module_file_rule_skips_placeholder_stems() -> None:
    assert (
        _rules(_FAKE_TEST, '\n"""A citation like `x.py` is syntax, not a claim."""\n')
        == []
    )


_SEEDED_BULLET = '''
"""Module shape.

- `Cluster` is the input the provider reasons over.
- `validate_proposals` rejects hallucinated memory IDs before the
  diff renderer sees them.
"""
'''


def test_flags_an_inventory_bullet_naming_a_symbol_that_is_gone() -> None:
    assert _rules(_FAKE_SRC, _SEEDED_BULLET) == [
        ("inventory-bullet", "validate_proposals")
    ]


def test_inventory_bullet_rule_stays_quiet_on_the_real_symbol() -> None:
    assert (
        _rules(
            _FAKE_SRC,
            _SEEDED_BULLET.replace("validate_proposals", "parse_and_validate"),
        )
        == []
    )


def test_inventory_bullet_rule_is_the_binding_lookup_not_the_bullet() -> None:
    assert _rules(_FAKE_SRC, _SEEDED_BULLET, _world_knowing("validate_proposals")) == []


def test_inventory_bullet_rule_declines_single_word_and_screaming_leads() -> None:
    """Measured noise: bullet leads that are search modes or env vars."""
    text = '''
"""Module shape.

- `hybrid` is the default mode.
- `BETTERMEMORY_DIR` overrides the store root.
"""
'''
    assert _rules(_FAKE_SRC, text) == []


def test_inventory_bullet_rule_does_not_run_outside_the_package() -> None:
    """A bullet-led backticked token in a test module is not an inventory."""
    assert _rules(_FAKE_TEST, _SEEDED_BULLET) == []


def test_a_trailing_call_shape_is_stripped_before_lookup() -> None:
    text = '\n"""The entry point is `builder.build_server(state=None)` today."""\n'
    assert _rules(_FAKE_SRC, text) == []
