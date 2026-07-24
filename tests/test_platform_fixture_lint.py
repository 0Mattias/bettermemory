"""Ratchet lint for platform assumptions baked into test fixtures.

The class: a fixture manipulates paths or the filesystem in a
POSIX-shaped way while the assertion it supports is platform-neutral,
so nothing fails locally or on the ubuntu legs and the defect surfaces
one windows-latest CI round-trip later. The 2026-07-19/20 burst paid
that round-trip repeatedly, and the six instances it closed are the
measuring stick for everything below:

* a hardcoded ``/tmp`` fixture path, and an ungated assertion that only
  holds where ``/opt`` exists (both repaired in 1c8b10f);
* a fixture whose hazard quietly dissolved whenever ``$TMPDIR`` landed
  under ``$HOME``, plus a sibling whose ``or os.name == "nt"`` escape
  swallowed the Windows failure outright (repaired in c1ede35);
* ``chmod(0o400)`` on a file a nested hook later unlinked — Windows
  maps the cleared owner-write bit to the READ-ONLY attribute and
  refuses the unlink (repaired in 85a5f18);
* a leading-``"/"`` string constant concatenated onto ``str(home)``,
  feeding a prefix comparison that is built on ``os.sep`` (repaired in
  a70fb69, whose commit message asked for "a lint rather than a sixth
  discovery" — this file is that lint);
* two doctor tests taking ``chmod(0o755)`` as a POSIX premise behind
  platform-neutral assertions (gated off Windows in b748c26).

Design bias, inherited from ``tests/test_doc_claims.py`` along with the
ratchet mechanics: **a checker with false positives gets disabled, and
a disabled checker protects nothing.** Every rule here was run against
the whole ``tests/`` tree at the ratchet base and tightened until the
live findings were exactly the allowlist below. Shapes that could not
be separated from working code were dropped and are listed as blind
spots rather than guessed at.

What is checked — three shapes, each the mechanically decidable slice
----------------------------------------------------------------------
1. ``posix-literal`` — a POSIX-absolute string constant (leading
   ``/``, not ``//``, longer than a bare ``/``) used as a filesystem
   path: as an argument to ``open``, to a concrete pathlib constructor,
   or to a path-taking ``os`` / ``os.path`` / ``shutil`` function; as
   the right operand of ``/`` on a path (an absolute right side
   REPLACES the left side entirely); or as the value a path-carrying
   environment variable is monkeypatched to. One hop of indirection is
   followed: a name bound exactly once, to a string constant, inside
   the same analysis unit.
2. ``slash-concat`` — ``+``-concatenation between a ``str(...)``
   conversion and a string constant that starts or ends with ``/``.
   ``str()`` of a Path renders ``os.sep``-separated on Windows, so the
   result mixes separators and anything downstream that compares
   prefixes or splits on ``os.sep`` misses. The constant may again
   arrive through a once-bound name — that indirection is exactly the
   shape a70fb69 repaired.
3. ``chmod-unlink`` — inside one top-level function (nested defs
   included, ordered by source position), a ``chmod`` to an integer
   mode whose owner-write bit is clear, then ``unlink``/``remove`` of
   the same receiver with no intervening ``chmod`` that restores the
   write bit. Windows refuses that unlink with PermissionError.

Suppressed on purpose, because they are the FIXED shapes
--------------------------------------------------------
* A read-only probe used as a gate condition — ``if
  os.path.isdir("/opt"):`` guarding the POSIX-only half of an
  assertion is the repair 1c8b10f made, not the defect.
* The probe-then-skip idiom — bind the path, test it, ``pytest.skip``
  when absent. The ``/etc/hosts`` symlink fixture does this today.
  Direction decides here exactly as it does for the platform gate, and
  so does position — a skip excuses the filesystem touches written
  after it, never one that has already run. The
  condition has to be provably true when the path is MISSING, which
  means a NEGATED presence probe (``not target.exists()``,
  ``not os.path.isfile(...)``) naming the flagged path, composed only
  through ``and``/``or``/``not``; and the skip has to sit in the branch
  that condition selects. A gate that skips when the path is PRESENT,
  a skip parked in the ``else``, a double negation that lands back on
  presence, and a bare truthiness test (``not target`` where ``target``
  is a ``Path`` — always truthy, so the skip is dead code) all leave
  the unit running on a windows-latest that has no ``/etc/hosts``, so
  none of them suppresses anything. Nor does a path named only in
  ``reason=`` prose, or a gate on a longer literal that merely contains
  the flagged one: prose gates nothing, and ``/etc/hostsX`` is a
  different file.
* Anything inside a unit (or module, via ``pytestmark``) whose skip
  provably excludes the windows-latest leg: a ``skipif`` condition, or
  the test of an in-body ``if ...: pytest.skip(...)``, that statically
  evaluates to True there. Code that never runs on Windows may be as
  POSIX as it likes — for a mark that is the whole unit, for an in-body
  skip it is what follows. Direction is the whole point — a gate that skips
  some OTHER platform (``sys.platform == "darwin"``, or an inverted
  ``!= "win32"``) leaves the unit running on exactly the leg these rules
  protect, so it exempts nothing.

What the detector cannot see — measured against the six instances
------------------------------------------------------------------
Replaying the pre-repair text of the six through these rules flags two:
the a70fb69 concatenation (caught through the once-bound-name hop) and
the 85a5f18 chmod-then-unlink (caught across its nested hook). The
self-tests below replay both shapes so that stays true by assertion,
not memory. The other four are structurally invisible here, and saying
so is the point of this section:

* Literals that reach the filesystem through the product under test.
  1c8b10f's ``/tmp`` and ``/opt`` lived inside prose bodies handed to
  the drift detector, which stats whatever it extracts; no rule here
  follows a string into product code.
* Entanglements with no literal at all. c1ede35's fixture went wrong
  whenever ``$TMPDIR`` happened to sit under ``$HOME`` — there is no
  token to flag; only the repair (pinning the two apart) is visible.
* Platform-sensitive premises behind neutral assertions. b748c26's
  tests chmod'd a directory and asserted a doctor verdict; ``chmod``
  as a premise is everywhere in legitimate, gated tests, so only the
  chmod-then-unlink slice is decidable without drowning the allowlist.
* f-string interpolation (an interpolated path followed by a ``/``
  piece) mixes separators exactly like ``slash-concat``. Measured at
  the ratchet base, the corpus instances are URL routes, rendered
  ratios, git-owned forward-slash text (gitignore patterns and their
  check-ignore matrix rows, gitdir pointer content), repo-relative
  synthetic payloads, and citation prose fed to drift detection —
  flagging working code is how a checker gets switched off, so the
  shape is excluded and pinned as a blind spot by a self-test below.

Also invisible, stated so nobody mistakes silence for coverage:
literals inside subprocess argument lists; ``/`` arriving via
``"".join``, ``+=``, ``%`` or ``.format``; a chmod whose mode is a
variable or a ``stat`` constant (unknown modes are treated as
restoring, trading recall for precision); receivers spelled
differently between the chmod and the unlink; sequences split across
helper functions; execution orders that differ from source order
(source position is a proxy); Windows-style literals breaking POSIX
legs (the mirror class); and ``PurePosixPath``, which declares its
platform explicitly and touches no filesystem. The rule set is
deliberately narrow; its value is what it catches tomorrow.

The exemption's condition parser is literal in the same way, and every
condition it cannot decide counts as NOT gated, so the unit is checked.
It reads three probes (``sys.platform``, ``os.name``,
``platform.system()``) compared against string literals — ``==``,
``!=``, ``in``/``not in`` over a literal collection,
``startswith``/``endswith``, and ``and``/``or``/``not`` over those. A
bare ``@_needs_posix_modes`` decorator resolves ONE hop through the
module's own bindings — assigned at module level, and bound exactly
once counting every binding form anywhere in the file — so the alias
spelling ``tests/test_doctor.py`` uses gates exactly like the
``pytest.mark.skipif(...)`` it was bound to, on both the platform and
the probe axis.

Where the parser stops, each costing a false positive at worst and
never a silent miss: a name INSIDE a condition (``skipif(_IS_WINDOWS,
...)``) stays unresolved, and so does a second alias hop; ``reason=``
prose is never read for direction, since it cannot prove which leg is
skipped even when it says "Windows" (the ratchet base accepted exactly
that, which is the hole this parser closes); only a decorator whose own
top-level call is ``skipif`` is honoured, so a per-case
``pytest.param(marks=pytest.mark.skipif(...))`` exempts nothing; the
skip call itself has to be spelled with a final segment of exactly
``skip``, which admits ``pytest.skip`` and a bare ``skip`` but not a
wrapper helper and not ``pytest.importorskip`` (that one skips on a
missing import, not on a platform, so crediting it would exempt units
outright); a skip counts only where the branch its gate selected
reaches it as a statement — never in an ``else``, never behind a
further ``if`` the same gate cannot also decide, and never inside a
loop, ``with``, ``try`` or nested ``def``, whose execution this parser
does not model; and an in-body skip never reaches backwards, because a
call that has already touched the filesystem cannot be un-run by a skip
written below it (a decorator or ``pytestmark`` sits ahead of the whole
body and still excuses all of it).

Both gates weigh that last one the same way, through ``_gate_excuses``,
which is also where the exception lives: only the shapes that TOUCH the
filesystem where they stand — a POSIX literal handed to a read, a
mutation or ``open``, and the ``unlink`` half of a chmod-unlink pair —
are ordered against the gate.

Where it is generous rather than strict, each taking a gate at face
value and each pinned by a self-test below, so a later tightening has
to come here and rewrite this:

* the shapes that merely BUILD a value — a ``Path`` constructor, a
  ``/`` composition, a ``setenv``, a slash-concatenation — are excused
  by a gate anywhere in the unit, including one written below them.
  None of them touches the filesystem where it stands, so a unit that
  skips at all never reaches the consequence; and ordering them would
  flag the probe-then-skip idiom's own first half, which binds the path
  a line before the gate that tests it. Whatever consumes such a value
  is judged on its own position wherever these rules read the consuming
  call at all; a writing ``Path`` method (``write_text``, ``mkdir``) is
  not one they read, gate or no gate, which is the narrowness the rule
  set declares above rather than anything this comparison adds;
* a needle matches a ``Name`` or an equal string constant anywhere in
  the gate's condition, without proving that occurrence is the same
  binding or an argument the probe actually consults.

How the ratchet works
---------------------
Same two paired guards as ``tests/test_doc_claims.py``:

* ``test_no_unexpected_platform_assumptions`` fails on any finding not
  in ``_ALLOWLIST`` — the forward guard.
* ``test_allowlist_has_no_stale_entries`` fails on any entry that no
  longer corresponds to a live finding — the reverse guard that stops
  the allowlist calcifying into permanent suppression.

Entries are keyed by (source, rule, subject) where the subject carries
the enclosing function name and the flagged shape — never a line
number, so edits elsewhere in a file do not rot the list.

What those two guards cannot currently catch: as of this commit
``_ALLOWLIST`` is empty and the corpus yields no findings at all, so
both pass vacuously and neither would notice a rule that quietly
stopped firing or an exemption that grew too broad. Until a real
finding lands, the self-tests at the bottom of this file are the ONLY
live check on the rules — which makes every suppression they assert
load-bearing. Widen an exemption and the negative direction has to
land in the same change, or nothing in the suite is watching.

Corollary for anyone editing this file: these rules scan full test
ASTs, including this module's own. Keep synthetic offender code inside
plain string constants fed to ``scan_source`` (as the self-tests do)
and never as live calls, or the lint will flag itself.
"""

from __future__ import annotations

import ast
import os
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from textwrap import dedent

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Every filesystem access here resolves from `_REPO_ROOT`, never the process
# CWD, so `pytest tests/` and `pytest` from a subdirectory see one corpus.

# ---------------------------------------------------------------------------
# What counts as "the filesystem" for the posix-literal rule
# ---------------------------------------------------------------------------
# Reads may legitimately appear as gate conditions (that is the fixed shape),
# so they are split from mutations, which are flagged wherever they appear.
_OS_READS = frozenset(
    {"stat", "lstat", "listdir", "scandir", "walk", "readlink", "access"}
)
_OS_MUTATIONS = frozenset(
    {
        "chdir",
        "mkdir",
        "makedirs",
        "rmdir",
        "removedirs",
        "remove",
        "unlink",
        "rename",
        "renames",
        "replace",
        "chmod",
        "utime",
        "link",
        "symlink",
        "truncate",
    }
)
_OSPATH_READS = frozenset(
    {
        "exists",
        "lexists",
        "isfile",
        "isdir",
        "islink",
        "ismount",
        "getsize",
        "getmtime",
        "getatime",
        "getctime",
        "samefile",
        "realpath",
    }
)
_SHUTIL_MUTATIONS = frozenset(
    {"rmtree", "copy", "copy2", "copyfile", "copytree", "move", "chown"}
)
# Concrete constructors only. PurePosixPath / PurePath are lexical and
# platform-explicit by name; constructing one is a statement, not a slip.
_PATH_CTORS = frozenset({"Path", "PosixPath", "WindowsPath"})

# Environment variables whose VALUE is a filesystem path. Monkeypatching one
# to a POSIX-absolute constant plants a foreign-shaped path under expanduser
# and friends; the portable spelling is str(tmp_path).
_PATH_ENV_VARS = frozenset(
    {
        "HOME",
        "USERPROFILE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    }
)

_OWNER_WRITE = 0o200

# What each recognised platform probe evaluates to on the windows-latest leg —
# the one leg these rules exist to protect. A skip exempts a unit only when its
# condition is provably TRUE there, so the direction of the comparison decides
# the exemption: `== "win32"` excludes Windows, `== "darwin"` does not.
_WINDOWS_PROBE_VALUES: dict[str, str] = {
    "sys.platform": "win32",
    "os.name": "nt",
    "platform.system()": "Windows",
}

# Filesystem predicates that answer False for a path which is not there. Their
# NEGATION is the only condition shape that proves "this unit skips itself when
# the path is missing" — the direction the probe-then-skip exemption needs.
_PATHLIB_PRESENCE_PROBES = frozenset(
    {
        "exists",
        "is_file",
        "is_dir",
        "is_symlink",
        "is_mount",
        "is_socket",
        "is_fifo",
        "is_block_device",
        "is_char_device",
    }
)
_OSPATH_PRESENCE_PROBES = frozenset(
    {"exists", "lexists", "isfile", "isdir", "islink", "ismount"}
)


# ---------------------------------------------------------------------------
# Findings and the allowlist
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    """One flagged platform-assumption shape in a test source file."""

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


# Shapes already present at the ratchet base which this lint may not rewrite
# (this repair touches only this new file). Each entry says WHY it is exempt;
# the reverse guard deletes any entry that stops matching a live finding.
_ALLOWLIST: dict[tuple[str, str, str], str] = {}


# ---------------------------------------------------------------------------
# Corpus — tracked test sources, with the same discipline as the doc-claim
# checker: tracked files first, so nothing vendored or virtualenv'd can
# enter; a pruned walk only where there is no git metadata at all.
# ---------------------------------------------------------------------------
_SKIP_DIR_NAMES = frozenset(
    {".git", ".claude", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}
)


def _git_tracked_test_files() -> tuple[str, ...] | None:
    """Tracked ``tests/**.py`` paths, or ``None`` outside a git checkout."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "ls-files", "-z", "--", "tests"],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        return None
    if proc.returncode != 0:  # pragma: no cover - not a checkout
        return None
    rels = [rel for rel in proc.stdout.decode("utf-8").split("\0") if rel]
    return tuple(
        sorted(
            rel for rel in rels if rel.endswith(".py") and (_REPO_ROOT / rel).is_file()
        )
    )


def _walk_test_files() -> tuple[str, ...]:
    """Fallback corpus for a tree with no git metadata, pruned as it goes."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT / "tests"):
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
def _test_files() -> tuple[str, ...]:
    tracked = _git_tracked_test_files()
    return _walk_test_files() if tracked is None else tracked


# ---------------------------------------------------------------------------
# AST plumbing
# ---------------------------------------------------------------------------
def _is_posix_absolute(value: object) -> bool:
    """Leading "/", not "//" (UNC-ish, implementation-defined), not bare "/"."""
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and len(value) > 1
    )


def _is_slash_edged(value: object) -> bool:
    return (
        isinstance(value, str)
        and value != ""
        and (value.startswith("/") or value.endswith("/"))
    )


def _analysis_units(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Top-level functions, class methods, and one unit for module-level code.

    A unit is the region within which the once-bound-name resolution and the
    chmod/unlink sequencing run. Nested defs stay inside their top-level
    function's unit on purpose: the 85a5f18 instance chmod'd in the test body
    and unlinked inside a nested monkeypatch hook, so splitting scopes would
    have hidden exactly the defect this rule exists for.
    """
    units: list[tuple[str, ast.AST]] = []
    leftover: list[ast.stmt] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            units.append((stmt.name, stmt))
        elif isinstance(stmt, ast.ClassDef):
            for inner in stmt.body:
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    units.append((f"{stmt.name}.{inner.name}", inner))
                else:
                    leftover.append(inner)
        else:
            leftover.append(stmt)
    if leftover:
        units.append(("<module>", ast.Module(body=leftover, type_ignores=[])))
    return units


def _parent_map(unit: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(unit)
        for child in ast.iter_child_nodes(parent)
    }


def _binding_counts(unit: ast.AST) -> Counter[str]:
    """How often each name is bound in the subtree, over every binding form.

    Exactly-once is the precision guard shared by both one-hop resolutions
    below: a rebound name (loop targets, with-as, walrus and augmented
    bindings all count) may hold anything by the time it is used, so it
    resolves to nothing rather than to a guess.
    """

    def _names_in(target: ast.AST | None) -> list[str]:
        if target is None:
            return []
        return [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]

    binds: Counter[str] = Counter()
    for node in ast.walk(unit):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                binds.update(_names_in(target))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            binds.update(_names_in(node.target))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            binds.update(_names_in(node.target))
        elif isinstance(node, ast.withitem):
            binds.update(_names_in(node.optional_vars))
        elif isinstance(node, ast.comprehension):
            binds.update(_names_in(node.target))
        elif isinstance(node, ast.NamedExpr):
            binds.update(_names_in(node.target))
    return binds


def _once_bound_strings(unit: ast.AST) -> dict[str, str]:
    """Names bound exactly once in the unit, to a string constant."""
    binds = _binding_counts(unit)
    values: dict[str, str] = {}
    for node in ast.walk(unit):
        if isinstance(node, ast.Assign):
            if (
                len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                values[node.targets[0].id] = node.value.value
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                values[node.target.id] = node.value.value
    return {name: text for name, text in values.items() if binds[name] == 1}


def _module_bindings(tree: ast.Module) -> dict[str, ast.expr]:
    """Names assigned at module level, mapped to the expression bound.

    The exactly-once count is taken over the WHOLE file, not just module
    level, so a name a function also rebinds drops out rather than resolving
    to whichever value happened to be written at the top.

    ``_needs_posix_modes = pytest.mark.skipif(sys.platform == "win32", ...)``
    applied as a bare ``@_needs_posix_modes`` is the shape the doctor tests
    use; without this hop the condition parser sees an ``ast.Name``, decides
    it is not a ``skipif`` call, and checks a unit windows-latest never runs.

    The map is deliberately NOT filtered to marks — ``_skipif_condition`` is
    what insists on a resolved top-level ``skipif`` call, so a name bound to
    anything else resolves to nothing that gates. Same discipline as the
    string resolution above: module level, exactly once, and ONE hop, so an
    alias bound to another alias stays unresolved rather than chased.
    """
    binds = _binding_counts(tree)
    bindings: dict[str, ast.expr] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if isinstance(target, ast.Name) and binds[target.id] == 1:
            bindings[target.id] = stmt.value
    return bindings


def _resolve_str(node: ast.AST, bound: dict[str, str]) -> str | None:
    """The string a node statically resolves to: a constant, or one name hop."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bound.get(node.id)
    return None


def _inside_gate_condition(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Is the node inside an if/while/ternary TEST, or a skipif decorator?

    A probe consulted to DECIDE is the platform-neutral fix shape; a probe
    relied on unconditionally is the defect. An ``assert`` is not a gate —
    asserting POSIX filesystem shape is the class itself.
    """
    prev: ast.AST = node
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.If, ast.While, ast.IfExp)) and cur.test is prev:
            return True
        if isinstance(cur, ast.Call) and ast.unparse(cur.func).endswith("skipif"):
            return True
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        prev, cur = cur, parents.get(cur)
    return False


def _string_constant(node: ast.expr) -> str | None:
    value = node.value if isinstance(node, ast.Constant) else None
    return value if isinstance(value, str) else None


def _string_collection(node: ast.expr) -> tuple[str, ...] | None:
    """A literal tuple/list/set of string constants — never a bare string.

    Keeping a lone constant out matters for ``in``: ``sys.platform in "win32"``
    is a substring test, not membership, and the two must not be conflated.
    """
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return None
    members: list[str] = []
    for elt in node.elts:
        text = _string_constant(elt)
        if text is None:
            return None
        members.append(text)
    return tuple(members)


def _windows_probe_value(node: ast.expr) -> str | None:
    """The windows-latest value of a recognised platform probe, else ``None``."""
    return _WINDOWS_PROBE_VALUES.get(ast.unparse(node))


def _comparison_on_windows(node: ast.Compare) -> bool | None:
    """Tri-state value of a single-operator comparison on the Windows leg."""
    if len(node.ops) != 1:
        return None
    op, left, right = node.ops[0], node.left, node.comparators[0]
    actual = _windows_probe_value(left)
    if actual is not None:
        if isinstance(op, (ast.Eq, ast.NotEq)):
            other = _string_constant(right)
            if other is None:
                return None
            return actual == other if isinstance(op, ast.Eq) else actual != other
        if isinstance(op, (ast.In, ast.NotIn)):
            members = _string_collection(right)
            if members is None:
                return None
            return (
                actual in members if isinstance(op, ast.In) else actual not in members
            )
        return None
    # The mirrored spelling: `"win32" == sys.platform`, `"win" in sys.platform`.
    actual = _windows_probe_value(right)
    other = _string_constant(left)
    if actual is None or other is None:
        return None
    if isinstance(op, ast.Eq):
        return actual == other
    if isinstance(op, ast.NotEq):
        return actual != other
    if isinstance(op, ast.In):
        return other in actual
    if isinstance(op, ast.NotIn):
        return other not in actual
    return None


def _str_method_on_windows(node: ast.Call) -> bool | None:
    """``<probe>.startswith(...)`` / ``.endswith(...)`` on the Windows leg."""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in {
        "startswith",
        "endswith",
    }:
        return None
    actual = _windows_probe_value(func.value)
    if actual is None or len(node.args) != 1 or node.keywords:
        return None
    needle = _string_constant(node.args[0])
    needles = (needle,) if needle is not None else _string_collection(node.args[0])
    if needles is None:
        return None
    return (
        actual.startswith(needles)
        if func.attr == "startswith"
        else actual.endswith(needles)
    )


def _truth_on_windows(node: ast.expr) -> bool | None:
    """Statically evaluate a skip condition on the windows-latest leg.

    ``True``/``False`` mean provably so; ``None`` means the condition is not
    one of the recognised shapes. Only ``True`` exempts a unit — an unknown
    condition falls through to being checked, which is the safe direction for
    a lint whose whole job is the Windows leg.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _truth_on_windows(node.operand)
        return None if inner is None else not inner
    if isinstance(node, ast.BoolOp):
        values = [_truth_on_windows(value) for value in node.values]
        if isinstance(node.op, ast.And):
            if any(value is False for value in values):
                return False
            return True if all(value is True for value in values) else None
        if any(value is True for value in values):
            return True
        return False if all(value is False for value in values) else None
    if isinstance(node, ast.Call):
        return _str_method_on_windows(node)
    if isinstance(node, ast.Compare):
        return _comparison_on_windows(node)
    return None


def _skipif_condition(node: ast.expr, bindings: dict[str, ast.expr]) -> ast.expr | None:
    """The condition of a ``skipif`` call, or ``None`` if this is not one.

    A bare ``ast.Name`` is resolved one hop through the module's bindings
    first, so ``@_needs_posix_modes`` reads exactly like the
    ``pytest.mark.skipif(...)`` it was bound to. A name that is unbound,
    rebound, or bound to another name stays unresolved and gates nothing.

    Only the node's OWN top-level call counts: a ``skipif`` nested inside a
    ``pytest.param(marks=...)`` skips one parametrised case, not the unit.
    """
    if isinstance(node, ast.Name):
        alias = bindings.get(node.id)
        if alias is None or isinstance(alias, ast.Name):
            return None
        node = alias
    if not isinstance(node, ast.Call) or not ast.unparse(node.func).endswith("skipif"):
        return None
    condition = next(
        (kw.value for kw in node.keywords if kw.arg == "condition"),
        node.args[0] if node.args else None,
    )
    if condition is None:
        return None
    # pytest also accepts a string condition, which it evals at collection.
    text = _string_constant(condition)
    if text is None:
        return condition
    try:
        return ast.parse(text, mode="eval").body
    except SyntaxError:  # pragma: no cover - malformed condition string
        return None


def _excludes_windows(marks: list[ast.expr], bindings: dict[str, ast.expr]) -> bool:
    """Does any of these marks provably skip the unit on windows-latest?

    Several ``skipif`` marks skip when ANY of their conditions holds, so one
    Windows-true condition is enough.
    """
    for mark in marks:
        condition = _skipif_condition(mark, bindings)
        if condition is not None and _truth_on_windows(condition) is True:
            return True
    return False


def _is_skip_call(node: ast.expr) -> bool:
    """A call whose final name segment is exactly ``skip``.

    ``pytest.skip(...)`` and a bare ``skip(...)`` count. Two near-misses do
    not, and both matter: ``skipif`` is a mark, not something a body calls;
    and ``pytest.importorskip(...)`` skips only when an import fails, so on a
    windows-latest that HAS the module it skips nothing — reading it as an
    unconditional skip would exempt units this file exists to check.
    """
    if not isinstance(node, ast.Call):
        return False
    return ast.unparse(node.func).rsplit(".", 1)[-1] == "skip"


def _source_position(node: ast.stmt | ast.expr) -> tuple[int, int]:
    """``(lineno, col_offset)`` — the order gates and shapes are compared in."""
    return (node.lineno, node.col_offset)


# Where a decorator or a module-level mark sits: ahead of every statement in
# the body, so it excuses the whole unit no matter how the body is ordered.
_BEFORE_BODY = (-1, -1)


def _reached_skip_position(
    body: list[ast.stmt], proven: Callable[[ast.expr], bool]
) -> tuple[int, int] | None:
    """Where entering this branch reaches a ``skip`` call, if it reaches one.

    Only the statement chain counts, plus a nested ``if`` whose own test
    ``proven`` also decides — two conditions that hold together do reach the
    skip beneath them. Everything else is execution this file does not model:
    an ``else`` branch (the condition selected the other one), a loop, a
    ``with``, a ``try``, or a nested ``def``, none of which is guaranteed to
    run the skip.

    The position reported is the skip statement's own rather than the gate's,
    because that is where the unit stops: a filesystem touch written between
    the gate and the skip it selects still runs.
    """
    for stmt in body:
        if isinstance(stmt, ast.Expr) and _is_skip_call(stmt.value):
            return _source_position(stmt)
        if isinstance(stmt, ast.If) and proven(stmt.test):
            position = _reached_skip_position(stmt.body, proven)
            if position is not None:
                return position
    return None


def _gated_skip_position(
    unit: ast.AST, proven: Callable[[ast.expr], bool]
) -> tuple[int, int] | None:
    """Where an ``if <proven>:`` in this unit skips it, if one does.

    The skip must sit under a gate ``proven`` decides — a bare ``skip(...)``
    in the unit body is not read as an exemption here, and the search starts
    at the unit's own statement chain rather than walking every ``if`` in the
    subtree, so a gate buried inside a loop, a ``with``, or a nested ``def``
    proves nothing about what runs. Shared by both gates so the two read one
    shape. Statements come in source order, so the first gate found is the
    earliest, which is the one that excuses the most.
    """
    body: list[ast.stmt] = getattr(unit, "body", [])
    for stmt in body:
        if isinstance(stmt, ast.If) and proven(stmt.test):
            position = _reached_skip_position(stmt.body, proven)
            if position is not None:
                return position
    return None


def _gate_excuses(
    gate: tuple[int, int] | None,
    node: ast.stmt | ast.expr,
    *,
    touches_here: bool,
) -> bool:
    """Does a gate at ``gate`` excuse the shape flagged at ``node``?

    ``None`` is no gate at all and excuses nothing. Otherwise a skip stops
    only what has not run yet, so the comparison depends on where the shape's
    hazard lands. A shape that TOUCHES the filesystem where it stands — a
    POSIX literal handed to a read, a mutation or ``open``, or the ``unlink``
    half of a chmod-unlink pair — has already raised (or already landed
    somewhere foreign) by the time a skip below it runs, so only a gate ahead
    of it excuses it. Shapes that merely BUILD a value touch nothing where
    they stand, and any unit that skips at all never reaches their
    consequence; ordering those against the gate would flag the
    probe-then-skip idiom's own first half, whose whole shape is to bind the
    path before testing it.
    """
    if gate is None:
        return False
    return not touches_here or gate < _source_position(node)


def _platform_gate(
    unit: ast.AST, bindings: dict[str, ast.expr]
) -> tuple[int, int] | None:
    """Where this unit becomes provably skipped on windows-latest, if it does.

    Direction-aware on purpose: a ``skipif`` that skips some other platform
    still runs here, so it exempts nothing (see the module docstring). A mark
    covers the unit from ``_BEFORE_BODY``; an in-body skip covers from where
    it stands.
    """
    if _excludes_windows(list(getattr(unit, "decorator_list", [])), bindings):
        return _BEFORE_BODY
    return _gated_skip_position(unit, lambda test: _truth_on_windows(test) is True)


def _module_platform_gated(tree: ast.Module, bindings: dict[str, ast.expr]) -> bool:
    """A module-level ``pytestmark`` skipif that excludes Windows exempts the file."""
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
        ):
            continue
        value = stmt.value
        marks = (
            list(value.elts) if isinstance(value, (ast.List, ast.Tuple)) else [value]
        )
        if _excludes_windows(marks, bindings):
            return True
    return False


def _names_a_needle(node: ast.AST, needles: set[str]) -> bool:
    """Does this expression name one of the needles?

    Matching is on AST identity, never on unparsed text: an identifier needle
    has to appear as a ``Name``, a literal needle as an EQUAL string constant.
    Substring matching would let a gate on ``"/etc/hostsX"`` excuse a use of
    ``"/etc/hosts"``, and a text search would find either inside ``reason=``
    prose, which gates nothing.
    """
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and inner.id in needles:
            return True
        if (
            isinstance(inner, ast.Constant)
            and isinstance(inner.value, str)
            and inner.value in needles
        ):
            return True
    return False


def _is_presence_probe(node: ast.expr, needles: set[str]) -> bool:
    """A call that is FALSE when the probed path is absent, naming a needle.

    ``target.exists()``, ``os.path.isfile("/etc/hosts")``, ``os.access(...)``:
    each answers False for a path that is not there, so its negation is what a
    skip has to be gated on. A bare name is not one of these — ``not target``
    where ``target`` is a ``Path`` never fires, because a ``Path`` is always
    truthy whether or not the file exists.
    """
    if not isinstance(node, ast.Call):
        return False
    callee = ast.unparse(node.func)
    base = callee.rsplit(".", 1)[-1]
    if callee.startswith("os.path."):
        recognised = base in _OSPATH_PRESENCE_PROBES
    elif callee.startswith("os."):
        recognised = base == "access"
    else:
        recognised = base in _PATHLIB_PRESENCE_PROBES
    return recognised and _names_a_needle(node, needles)


def _true_when_absent(node: ast.expr, needles: set[str]) -> bool:
    """Is this condition provably TRUE when the probed path is MISSING?

    Direction carries the whole content of the probe-then-skip idiom, just as
    it does for the platform gate: the un-negated probe skips where the path
    is THERE and leaves the POSIX-shaped body running on the leg it was never
    portable on. ``or`` needs one absence-proving piece, ``and`` needs all of
    them, and ``not`` hands off to the companion below — which is what makes
    ``not (not target.exists())`` land back on presence, and ``not (A or B)``
    read exactly like the ``not A and not B`` it is equivalent to.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _false_when_absent(node.operand, needles)
    if isinstance(node, ast.BoolOp):
        parts = [_true_when_absent(value, needles) for value in node.values]
        return any(parts) if isinstance(node.op, ast.Or) else all(parts)
    return False


def _false_when_absent(node: ast.expr, needles: set[str]) -> bool:
    """The companion question: provably FALSE when the probed path is MISSING.

    A presence probe is the base case; the boolean operators mirror
    ``_true_when_absent`` under De Morgan, so the two spellings of one
    condition cannot disagree about direction.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _true_when_absent(node.operand, needles)
    if isinstance(node, ast.BoolOp):
        parts = [_false_when_absent(value, needles) for value in node.values]
        return all(parts) if isinstance(node.op, ast.Or) else any(parts)
    return _is_presence_probe(node, needles)


def _probe_skip_gate(
    unit: ast.AST, needles: set[str], bindings: dict[str, ast.expr]
) -> tuple[int, int] | None:
    """Where this unit skips itself when the probed path is absent, if it does.

    The probe-then-skip idiom: bind the path, test it, skip when it is not
    there. Four narrowings, each the same shape as the platform gate's: only
    a real ``skipif`` CONDITION counts (``reason=`` prose can name a path
    without gating on it), only an absence-proving condition counts, only a
    skip the selected branch actually reaches counts, and — once
    ``_gate_excuses`` weighs the position returned here — only shapes the skip
    runs ahead of are excused.
    """
    for deco in getattr(unit, "decorator_list", []):
        condition = _skipif_condition(deco, bindings)
        if condition is not None and _true_when_absent(condition, needles):
            return _BEFORE_BODY
    return _gated_skip_position(unit, lambda test: _true_when_absent(test, needles))


# ---------------------------------------------------------------------------
# The three rules
# ---------------------------------------------------------------------------
def _fs_category(callee: str) -> str | None:
    """None, or how the callable touches the filesystem: read/mutate/ctor.

    Reads and constructors are gate-suppressible (consulting the filesystem
    to decide is the fix shape); mutations are flagged wherever they appear.
    ``open`` counts as a mutation: append/write modes create, and even a
    read-mode open of a POSIX literal fails loudly off-POSIX.
    """
    base = callee.rsplit(".", 1)[-1]
    if callee == "open":
        return "mutate"
    if base in _PATH_CTORS and (callee == base or callee.endswith(f"pathlib.{base}")):
        return "ctor"
    if callee.startswith("os.path."):
        return "read" if base in _OSPATH_READS else None
    if callee.startswith("os."):
        if base in _OS_READS:
            return "read"
        if base in _OS_MUTATIONS:
            return "mutate"
        return None
    if callee.startswith("shutil."):
        if base == "disk_usage":
            return "read"
        return "mutate" if base in _SHUTIL_MUTATIONS else None
    return None


def _scan_posix_literals(
    source: str,
    unit_name: str,
    unit: ast.AST,
    bindings: dict[str, ast.expr],
    platform_gate: tuple[int, int] | None,
) -> list[Finding]:
    """Rule ``posix-literal``: absolute POSIX constants used as paths."""
    findings: list[Finding] = []
    parents = _parent_map(unit)
    bound = _once_bound_strings(unit)

    for node in ast.walk(unit):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            # `p / "/abs"`: a string operand of `/` is Path.__truediv__ (real
            # division never takes str), and an absolute right side REPLACES
            # the left — silently on POSIX, drive-relatively on Windows.
            lit = _resolve_str(node.right, bound)
            # Composing the path touches nothing where it stands, so a gate
            # anywhere in the unit excuses it.
            if _is_posix_absolute(lit) and not _gate_excuses(
                platform_gate, node, touches_here=False
            ):
                findings.append(
                    Finding(
                        source,
                        node.lineno,
                        "posix-literal",
                        f"{unit_name}: {ast.unparse(node)}",
                        "an absolute right side of `/` replaces the left side; "
                        "build subpaths from relative segments",
                    )
                )
            continue
        if not isinstance(node, ast.Call):
            continue
        callee = ast.unparse(node.func)
        base = callee.rsplit(".", 1)[-1]

        if base == "setenv" and len(node.args) >= 2:
            var_node, value_node = node.args[0], node.args[1]
            var = (
                var_node.value
                if isinstance(var_node, ast.Constant)
                and isinstance(var_node.value, str)
                else None
            )
            lit = _resolve_str(value_node, bound)
            # Planting the variable touches nothing where it stands; what
            # reads it later is judged on its own position.
            if (
                var in _PATH_ENV_VARS
                and _is_posix_absolute(lit)
                and not _gate_excuses(platform_gate, node, touches_here=False)
            ):
                findings.append(
                    Finding(
                        source,
                        node.lineno,
                        "posix-literal",
                        f"{unit_name}: setenv({var}, {lit})",
                        "a path env var pinned to a POSIX constant; "
                        "use str(tmp_path) so the shape is native everywhere",
                    )
                )
            continue

        category = _fs_category(callee)
        if category is None:
            continue
        lit = None
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            value = _resolve_str(arg, bound)
            if _is_posix_absolute(value):
                lit = value
                break
        if lit is None:
            continue
        # A constructor builds a path object and touches nothing; a read or a
        # mutation reaches the filesystem where it stands.
        touches_here = category != "ctor"
        if _gate_excuses(platform_gate, node, touches_here=touches_here):
            continue
        if category in ("read", "ctor"):
            if _inside_gate_condition(node, parents):
                continue
            needles = {lit}
            holder = parents.get(node)
            if (
                isinstance(holder, ast.Assign)
                and len(holder.targets) == 1
                and isinstance(holder.targets[0], ast.Name)
            ):
                needles.add(holder.targets[0].id)
            if _gate_excuses(
                _probe_skip_gate(unit, needles, bindings),
                node,
                touches_here=touches_here,
            ):
                continue
        findings.append(
            Finding(
                source,
                node.lineno,
                "posix-literal",
                f"{unit_name}: {callee}({lit})",
                "POSIX-absolute literal fed to the filesystem; build from "
                "tmp_path, or gate on the root existing / a platform skip",
            )
        )
    return findings


def _scan_slash_concat(
    source: str, unit_name: str, unit: ast.AST, platform_gate: tuple[int, int] | None
) -> list[Finding]:
    """Rule ``slash-concat``: a slash-edged constant glued onto ``str(...)``."""
    findings: list[Finding] = []
    bound = _once_bound_strings(unit)
    for node in ast.walk(unit):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
            continue
        sides = (node.left, node.right)
        str_calls = [
            side
            for side in sides
            if isinstance(side, ast.Call)
            and isinstance(side.func, ast.Name)
            and side.func.id == "str"
            and side.args
        ]
        if len(str_calls) != 1:
            continue
        other = sides[1] if sides[0] is str_calls[0] else sides[0]
        value = _resolve_str(other, bound)
        if not _is_slash_edged(value):
            continue
        # Gluing the string touches nothing; whatever consumes the mixed
        # separators later is what fails, and a unit that skips never gets
        # there.
        if _gate_excuses(platform_gate, node, touches_here=False):
            continue
        findings.append(
            Finding(
                source,
                node.lineno,
                "slash-concat",
                f"{unit_name}: {ast.unparse(node)}",
                "str(...) renders os.sep-separated on Windows; concatenate "
                "os.sep (or stay in pathlib) instead of a literal '/'",
            )
        )
    return findings


def _scan_chmod_unlink(
    source: str, unit_name: str, unit: ast.AST, platform_gate: tuple[int, int] | None
) -> list[Finding]:
    """Rule ``chmod-unlink``: write bit cleared, then the same receiver unlinked.

    Sequencing is by source position across the whole unit, nested defs
    included — a proxy for execution order that holds for the fixture shape
    this rule exists for (set up read-only, hook unlinks later). Receivers
    match by unparsed text; an unknown (non-literal) mode conservatively
    counts as restoring the write bit, trading recall for precision.

    The unlink is what raises on Windows, so it is the call a platform gate is
    weighed against: a skip between the chmod and the unlink means the unlink
    never runs there, and a skip below both means it already has.
    """
    findings: list[Finding] = []
    calls = sorted(
        (node for node in ast.walk(unit) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    restrictive: dict[str, tuple[int, int]] = {}  # receiver -> (mode, line)
    for call in calls:
        func = call.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr == "chmod" and call.args:
            receiver = ast.unparse(func.value)
            mode_node: ast.AST = call.args[0]
            if receiver == "os" and len(call.args) >= 2:
                receiver = ast.unparse(call.args[0])
                mode_node = call.args[1]
            mode = (
                mode_node.value
                if isinstance(mode_node, ast.Constant)
                and isinstance(mode_node.value, int)
                else None
            )
            if mode is not None and (mode & _OWNER_WRITE) == 0:
                restrictive[receiver] = (mode, call.lineno)
            else:
                restrictive.pop(receiver, None)
        elif func.attr in {"unlink", "remove"}:
            receiver = ast.unparse(func.value)
            if receiver == "os" and call.args:
                receiver = ast.unparse(call.args[0])
            if receiver not in restrictive:
                continue
            if _gate_excuses(platform_gate, call, touches_here=True):
                continue
            mode, chmod_line = restrictive.pop(receiver)
            findings.append(
                Finding(
                    source,
                    call.lineno,
                    "chmod-unlink",
                    f"{unit_name}: {receiver} chmod({oct(mode)}) then {func.attr}",
                    f"chmod at source line {chmod_line} clears the owner-write "
                    "bit (READ-ONLY on Windows) and nothing restores it before "
                    "the unlink; chmod the receiver writable (e.g. 0o600) first",
                )
            )
    return findings


def scan_source(source: str, text: str) -> list[Finding]:
    """Run all three rules over one test source. Self-tests feed this."""
    tree = ast.parse(text)
    bindings = _module_bindings(tree)
    if _module_platform_gated(tree, bindings):
        return []
    findings: list[Finding] = []
    for unit_name, unit in _analysis_units(tree):
        platform_gate = _platform_gate(unit, bindings)
        if platform_gate == _BEFORE_BODY:
            # A mark excludes the whole unit, so there is nothing to weigh.
            continue
        findings.extend(
            _scan_posix_literals(source, unit_name, unit, bindings, platform_gate)
        )
        findings.extend(_scan_slash_concat(source, unit_name, unit, platform_gate))
        findings.extend(_scan_chmod_unlink(source, unit_name, unit, platform_gate))
    return findings


@lru_cache(maxsize=None)
def collect_findings() -> tuple[Finding, ...]:
    """Every finding across the tracked test corpus."""
    out: list[Finding] = []
    for rel in _test_files():
        out.extend(scan_source(rel, (_REPO_ROOT / rel).read_text(encoding="utf-8")))
    return tuple(out)


# ---------------------------------------------------------------------------
# The two paired ratchet tests
# ---------------------------------------------------------------------------
def test_no_unexpected_platform_assumptions() -> None:
    """Forward guard: no new POSIX-shaped fixture manipulation may land.

    If this fails, prefer repairing the fixture (tmp_path, os.sep, restore
    the write bit, or an explicit existence/platform gate). An ``_ALLOWLIST``
    entry is for a shape you verified benign on the windows-latest leg and
    cannot rewrite in your change — say why, and expect the reverse guard to
    force the entry out when the shape is repaired.
    """
    unexpected = [f for f in collect_findings() if f.key not in _ALLOWLIST]
    if unexpected:
        rendered = "\n".join(f"  - {finding}" for finding in unexpected)
        pytest.fail(
            f"{len(unexpected)} platform-assumption shape(s) in test fixtures:\n"
            f"{rendered}\n\n"
            f"Each is invisible locally and on ubuntu and historically cost "
            f"one windows-latest round-trip to discover. Fix the fixture "
            f"rather than the checker unless the extraction itself misfired."
        )


def test_allowlist_has_no_stale_entries() -> None:
    """Reverse guard: the allowlist may not outlive the findings it covers.

    Two causes, opposite responses: (1) the shape was repaired — delete the
    entry, that is the ratchet; (2) an extractor rule narrowed or the code
    was reworded past the rule while the hazard survives — deleting then
    hides a live shape, so check the source before deleting.
    """
    live = {finding.key for finding in collect_findings()}
    stale = sorted(key for key in _ALLOWLIST if key not in live)
    if stale:
        rendered = "\n".join(
            f"  - {key} (exempt because: {_ALLOWLIST[key]})" for key in stale
        )
        pytest.fail(
            f"{len(stale)} _ALLOWLIST entr(y/ies) no longer correspond to a "
            f"live finding:\n{rendered}\n\nIf the shape was repaired, delete "
            f"the entry. If the code merely stopped matching the extractor, "
            f"deleting hides a live hazard — verify against the source first."
        )


def test_allowlist_entries_carry_a_reason() -> None:
    """Every exemption must say why, so review can judge it."""
    for key, reason in _ALLOWLIST.items():
        assert len(reason.strip()) >= 40, f"{key} needs a substantive reason"


def test_this_module_is_inside_the_scanned_corpus() -> None:
    """The lint scans itself, so its own live code obeys its own rules."""
    corpus = _test_files()
    assert corpus, "corpus is empty — test-file discovery broke"
    assert "tests/test_platform_fixture_lint.py" in corpus
    assert all(rel.startswith("tests/") for rel in corpus)
    assert not [rel for rel in corpus if "site-packages" in rel]


def test_walk_fallback_admits_nothing_the_git_listing_excludes() -> None:
    """The no-git fallback may miss tracked files, never admit untracked ones."""
    tracked = _git_tracked_test_files()
    if tracked is None:  # pragma: no cover - only outside a git checkout
        pytest.skip("not a git checkout")
    walked = _walk_test_files()
    assert not set(walked) - set(tracked), (
        f"the walk admits untracked files the git listing excludes: "
        f"{sorted(set(walked) - set(tracked))[:10]}"
    )


# ---------------------------------------------------------------------------
# Self-tests: each rule must demonstrably fire, and each suppression must
# demonstrably suppress. A rule that is merely satisfied today is
# indistinguishable from a rule that does nothing. Offender code lives in
# string constants precisely so the lint's own scan cannot mistake it for
# live fixture code (see the module docstring's corollary).
# ---------------------------------------------------------------------------
def _rules_fired(text: str) -> list[tuple[str, str]]:
    return [(f.rule, f.subject) for f in scan_source("tests/fake.py", dedent(text))]


def test_flags_posix_literal_fed_to_open() -> None:
    fired = _rules_fired(
        """
        def test_reads_the_host_file():
            with open("/etc/passwd") as fh:
                assert fh.read()
        """
    )
    assert fired == [("posix-literal", "test_reads_the_host_file: open(/etc/passwd)")]


def test_flags_posix_literal_reaching_the_filesystem_through_a_name() -> None:
    """One hop of indirection: bound once, to a constant, then used."""
    fired = _rules_fired(
        """
        def test_stats_a_fixed_path():
            probe = "/var/log/syslog"
            assert os.stat(probe)
        """
    )
    assert [rule for rule, _ in fired] == ["posix-literal"]


def test_flags_path_constructor_and_truediv_literals() -> None:
    fired = _rules_fired(
        """
        def test_builds_absolute_paths(tmp_path):
            seed = Path("/tmp/seed")
            replaced = tmp_path / "/etc/hosts"
            return seed, replaced
        """
    )
    assert [rule for rule, _ in fired] == ["posix-literal", "posix-literal"]
    # A relative segment on the right of `/` is the portable shape.
    assert (
        _rules_fired(
            """
            def test_builds_relative_paths(tmp_path):
                return tmp_path / "sub" / "file.txt"
            """
        )
        == []
    )


def test_read_probe_inside_a_gate_condition_is_the_fixed_shape() -> None:
    """The 1c8b10f repair: assert the POSIX-only half behind an existence gate."""
    fired = _rules_fired(
        """
        def test_remote_root_behaviour():
            if os.path.isdir("/opt"):
                assert "/opt/gophish" in missing
        """
    )
    assert fired == []
    # The same probe relied on unconditionally is the defect.
    assert (
        _rules_fired(
            """
            def test_remote_root_behaviour():
                assert os.path.isdir("/opt")
            """
        )
        != []
    )


def test_probe_then_skip_idiom_is_not_flagged() -> None:
    """The live ``/etc/hosts`` symlink fixture's shape: bind, gate, skip."""
    fired = _rules_fired(
        """
        def test_symlink_to_host_file(source_root):
            target = Path("/etc/hosts")
            if not target.exists():
                pytest.skip("requires /etc/hosts")
            os.symlink(target, source_root / "bad.md")
        """
    )
    assert fired == []


def test_absence_shaped_probe_gates_still_suppress() -> None:
    """The directions that DO prove the unit skips when the path is gone.

    An ``or`` of negations skips if any piece is missing, an ``and`` of them
    skips only when every piece is (both hold once the path is absent), and
    the same idiom spelled as a ``skipif`` condition gates the whole unit.
    All three are the fixed shape; if they stopped being quiet the narrowing
    below would just be a way to turn the rule off.
    """
    or_composed = """
        def test_symlink_to_host_file(source_root):
            target = Path("/etc/hosts")
            if not target.exists() or not target.is_file():
                pytest.skip("requires a real /etc/hosts")
            os.symlink(target, source_root / "bad.md")
        """
    and_composed = """
        def test_symlink_to_host_file(source_root):
            target = Path("/etc/hosts")
            if not target.exists() and not target.is_symlink():
                pytest.skip("requires a real /etc/hosts")
            os.symlink(target, source_root / "bad.md")
        """
    decorator_probe = """
        @pytest.mark.skipif(
            not os.path.exists("/etc/hosts"), reason="host file absent"
        )
        def test_reads_the_host_file():
            assert os.path.isfile("/etc/hosts")
        """
    for label, snippet in (
        ("or of negations", or_composed),
        ("and of negations", and_composed),
        ("skipif decorator probe", decorator_probe),
    ):
        assert _rules_fired(snippet) == [], label


def test_probe_skips_pointed_away_from_absence_do_not_suppress() -> None:
    """The probe suppression is direction-aware for the same reason the
    platform gate is: every snippet here names the path in something that
    looks like a gate, and every one of them still RUNS on a windows-latest
    that has no ``/etc/hosts``, so the literal has to fire.

    ``inverted probe`` skips where the path is PRESENT; the ``skip in else``
    pair puts the skip on the branch its condition did not select, once per
    direction of the test; ``double negation`` spells presence the long way
    round and must not survive one nesting level; ``path truthiness`` tests a
    ``Path``, which is truthy whether or not the file exists, so the skip is
    unreachable; ``not (A or B)`` and ``not A and not B`` are the same
    condition and both need the second piece to hold, which absence alone
    does not prove; and ``reason prose only`` names the path in prose a
    ``skipif`` never evaluates.
    """
    inverted_probe = """
        def test_symlink_to_host_file(source_root):
            target = Path("/etc/hosts")
            if target.exists():
                pytest.skip("requires /etc/hosts")
            os.symlink(target, source_root / "bad.md")
        """
    presence_test_skip_in_else = """
        def test_symlink_to_host_file(source_root):
            target = Path("/etc/hosts")
            if target.exists():
                pass
            else:
                pytest.skip("requires /etc/hosts")
            os.symlink(target, source_root / "bad.md")
        """
    absence_test_skip_in_else = """
        def test_symlink_to_host_file(source_root):
            target = Path("/etc/hosts")
            if not target.exists():
                pass
            else:
                pytest.skip("requires /etc/hosts")
            os.symlink(target, source_root / "bad.md")
        """
    double_negation = """
        def test_symlink_to_host_file(source_root):
            target = Path("/etc/hosts")
            if not (not target.exists()):
                pytest.skip("requires /etc/hosts")
            os.symlink(target, source_root / "bad.md")
        """
    path_truthiness = """
        def test_symlink_to_host_file(source_root):
            target = Path("/etc/hosts")
            if not target:
                pytest.skip("requires /etc/hosts")
            os.symlink(target, source_root / "bad.md")
        """
    negated_or = """
        def test_reads_the_host_file():
            if not (os.path.exists("/etc/hosts") or shutil.which("git")):
                pytest.skip("requires /etc/hosts")
            assert os.path.isfile("/etc/hosts")
        """
    equivalent_and = """
        def test_reads_the_host_file():
            if not os.path.exists("/etc/hosts") and not shutil.which("git"):
                pytest.skip("requires /etc/hosts")
            assert os.path.isfile("/etc/hosts")
        """
    reason_prose_only = """
        @pytest.mark.skipif(sys.platform == "darwin", reason="needs /etc/hosts")
        def test_reads_the_host_file():
            assert os.path.isfile("/etc/hosts")
        """
    for label, snippet in (
        ("inverted probe", inverted_probe),
        ("presence test, skip in else", presence_test_skip_in_else),
        ("absence test, skip in else", absence_test_skip_in_else),
        ("double negation", double_negation),
        ("path truthiness", path_truthiness),
        ("not (A or B)", negated_or),
        ("not A and not B", equivalent_and),
        ("reason prose only", reason_prose_only),
    ):
        assert [rule for rule, _ in _rules_fired(snippet)] == ["posix-literal"], label


def test_equivalent_condition_spellings_get_the_same_verdict() -> None:
    """``not (A or B)`` and ``not A and not B`` are one condition.

    The two are De Morgan twins, so a parser that reads one as
    absence-proving and flags the other is reading spelling, not direction.
    Both suppress when every piece is an absence probe, and neither does when
    one piece is something absence cannot decide.
    """
    both_probes_or = """
        def test_reads_the_host_file():
            if not (os.path.exists("/etc/hosts") or os.path.isfile("/etc/hosts")):
                pytest.skip("requires /etc/hosts")
            assert os.path.isdir("/etc/hosts")
        """
    both_probes_and = """
        def test_reads_the_host_file():
            if not os.path.exists("/etc/hosts") and not os.path.isfile("/etc/hosts"):
                pytest.skip("requires /etc/hosts")
            assert os.path.isdir("/etc/hosts")
        """
    assert _rules_fired(both_probes_or) == []
    assert _rules_fired(both_probes_and) == []


def test_needle_matching_is_exact_not_a_substring_of_the_gate() -> None:
    """A gate on a DIFFERENT path may not excuse this one.

    ``/etc/hostsX`` contains ``/etc/hosts`` as a substring and is a different
    file; a text-matching needle check took the longer literal as a gate on
    the shorter one. The exact spelling still suppresses, so this is a
    tightening rather than a switch-off.
    """
    different_literal = """
        def test_reads_the_host_file():
            if not os.path.exists("/etc/hostsX"):
                pytest.skip("requires /etc/hostsX")
            assert os.path.isfile("/etc/hosts")
        """
    same_literal = """
        def test_reads_the_host_file():
            if not os.path.exists("/etc/hosts"):
                pytest.skip("requires /etc/hosts")
            assert os.path.isfile("/etc/hosts")
        """
    assert [rule for rule, _ in _rules_fired(different_literal)] == ["posix-literal"]
    assert _rules_fired(same_literal) == []


def test_setenv_posix_constant_flagged_portable_spelling_not() -> None:
    fired = _rules_fired(
        """
        def test_fake_home(monkeypatch, tmp_path):
            monkeypatch.setenv("HOME", "/home/nobody")
            monkeypatch.setenv("USERPROFILE", str(tmp_path))
        """
    )
    assert fired == [("posix-literal", "test_fake_home: setenv(HOME, /home/nobody)")]


def test_platform_gated_units_and_modules_are_exempt() -> None:
    gated_function = """
        @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
        def test_posix_only():
            open("/etc/passwd").close()
        """
    inline_gate = """
        def test_posix_only():
            if sys.platform == "win32":
                pytest.skip("POSIX-only")
            open("/etc/passwd").close()
        """
    module_mark = """
        pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX-only file")

        def test_posix_only():
            open("/etc/passwd").close()
        """
    assert _rules_fired(gated_function) == []
    assert _rules_fired(inline_gate) == []
    assert _rules_fired(module_mark) == []


def test_gates_pointed_at_another_platform_do_not_exempt() -> None:
    """The direction the skip points is what decides the exemption.

    Every snippet here mentions a platform, and every one of them still RUNS
    on windows-latest — the leg these rules protect — so the POSIX literal in
    the body has to fire. Treating "mentions a platform" as an exemption is
    what let a single mac-direction ``skipif`` reopen the whole class.
    """
    darwin_skipif = """
        @pytest.mark.skipif(sys.platform == "darwin", reason="flaky on macOS")
        def test_runs_on_windows():
            open("/etc/passwd").close()
        """
    inverted_skipif = """
        @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
        def test_runs_on_windows():
            open("/etc/passwd").close()
        """
    inverted_os_name = """
        @pytest.mark.skipif(os.name != "nt", reason="Windows-only")
        def test_runs_on_windows():
            open("/etc/passwd").close()
        """
    negated_prefix = """
        @pytest.mark.skipif(
            not sys.platform.startswith("win"), reason="Windows-only"
        )
        def test_runs_on_windows():
            open("/etc/passwd").close()
        """
    inline_darwin_skip = """
        def test_runs_on_windows():
            if sys.platform == "darwin":
                pytest.skip("not on macOS")
            open("/etc/passwd").close()
        """
    darwin_module_mark = """
        pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="macOS")

        def test_runs_on_windows():
            open("/etc/passwd").close()
        """
    for label, snippet in (
        ("darwin skipif", darwin_skipif),
        ("inverted sys.platform", inverted_skipif),
        ("inverted os.name", inverted_os_name),
        ("negated prefix", negated_prefix),
        ("inline darwin skip", inline_darwin_skip),
        ("darwin pytestmark", darwin_module_mark),
    ):
        assert [rule for rule, _ in _rules_fired(snippet)] == ["posix-literal"], label


def test_windows_excluding_conditions_the_parser_recognises() -> None:
    """The spellings that DO prove the windows-latest leg never runs it."""
    spellings = (
        'sys.platform.startswith("win")',
        'sys.platform in ("win32", "cygwin")',
        'sys.platform in {"win32"}',
        '"win32" == sys.platform',
        '"win" in sys.platform',
        'os.name == "nt"',
        'platform.system() == "Windows"',
        'sys.platform == "win32" or os.name == "nt"',
        'os.name == "nt" and sys.platform == "win32"',
        'not (sys.platform != "win32")',
        'sys.platform not in ("linux", "darwin")',
    )
    for condition in spellings:
        snippet = f"""
        @pytest.mark.skipif({condition}, reason="POSIX-only")
        def test_posix_only():
            open("/etc/passwd").close()
        """
        assert _rules_fired(snippet) == [], condition
        inline = f"""
        def test_posix_only():
            if {condition}:
                pytest.skip("POSIX-only")
            open("/etc/passwd").close()
        """
        assert _rules_fired(inline) == [], condition


def test_string_form_skipif_condition_is_parsed_not_taken_as_truthy() -> None:
    """pytest evaluates a string condition at collection; so does this parser.

    A non-empty string is truthy to Python but says nothing about direction —
    reading it as a condition keeps both legs of the ratchet honest.
    """
    excluded = """
        @pytest.mark.skipif('sys.platform == "win32"', reason="POSIX-only")
        def test_posix_only():
            open("/etc/passwd").close()
        """
    not_excluded = """
        @pytest.mark.skipif('sys.platform == "darwin"', reason="macOS")
        def test_runs_on_windows():
            open("/etc/passwd").close()
        """
    assert _rules_fired(excluded) == []
    assert [rule for rule, _ in _rules_fired(not_excluded)] == ["posix-literal"]


def test_mark_alias_decorators_resolve_one_hop() -> None:
    """The ``tests/test_doctor.py`` spelling: a module-level alias bound to a
    ``skipif``, then applied bare. The decorator is an ``ast.Name``, not a
    call, so without the hop the parser gives up and checks a unit
    windows-latest never runs. Both axes read the alias: the per-unit
    decorator and a ``pytestmark`` that names it."""
    aliased_decorator = """
        _needs_posix_modes = pytest.mark.skipif(
            sys.platform == "win32", reason="POSIX mode bits are not meaningful"
        )

        @_needs_posix_modes
        def test_posix_only():
            open("/etc/passwd").close()
        """
    aliased_module_mark = """
        _needs_posix_modes = pytest.mark.skipif(os.name == "nt", reason="POSIX")
        pytestmark = _needs_posix_modes

        def test_posix_only():
            open("/etc/passwd").close()
        """
    aliased_probe_gate = """
        _needs_host_file = pytest.mark.skipif(
            not os.path.exists("/etc/hosts"), reason="host file absent"
        )

        @_needs_host_file
        def test_reads_the_host_file():
            assert os.path.isfile("/etc/hosts")
        """
    for label, snippet in (
        ("aliased decorator", aliased_decorator),
        ("aliased pytestmark", aliased_module_mark),
        ("aliased probe gate", aliased_probe_gate),
    ):
        assert _rules_fired(snippet) == [], label


def test_mark_alias_resolution_stops_where_it_cannot_prove_direction() -> None:
    """One hop, bound exactly once, and still pointed at Windows.

    Resolving the alias buys nothing if it then exempts on the strength of a
    name: a mac-directed alias, a rebound one (which may hold either mark by
    the time the decorator applies), a hop through a second alias, and a name
    that arrives from another module all leave the unit running here.
    """
    darwin_alias = """
        _flaky_on_mac = pytest.mark.skipif(sys.platform == "darwin", reason="mac")

        @_flaky_on_mac
        def test_runs_on_windows():
            open("/etc/passwd").close()
        """
    rebound_alias = """
        _gate = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
        _gate = pytest.mark.skipif(shutil.which("git") is None, reason="git")

        @_gate
        def test_runs_on_windows():
            open("/etc/passwd").close()
        """
    second_hop = """
        _base = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
        _alias = _base

        @_alias
        def test_runs_on_windows():
            open("/etc/passwd").close()
        """
    imported_alias = """
        @_needs_posix_modes
        def test_runs_on_windows():
            open("/etc/passwd").close()
        """
    for label, snippet in (
        ("darwin alias", darwin_alias),
        ("rebound alias", rebound_alias),
        ("second hop", second_hop),
        ("alias bound in another module", imported_alias),
    ):
        assert [rule for rule, _ in _rules_fired(snippet)] == ["posix-literal"], label


def test_a_skip_the_gate_does_not_reach_is_not_an_exemption() -> None:
    """A gate exempts only what its own condition decides.

    ``skip behind a flag`` and ``proven behind a flag`` are the two nesting
    orders of one hole: windows-latest skips only when the OTHER condition
    also holds, so the unit runs there some of the time and the literal has
    to fire. ``importorskip`` skips on a missing module, never on a platform
    — reading it as a skip would exempt the unit outright. ``skip in a loop``
    sits in execution this parser does not model.
    """
    skip_behind_a_flag = """
        def test_maybe_skipped(some_flag):
            if sys.platform == "win32":
                if some_flag:
                    pytest.skip("sometimes")
            open("/etc/passwd").close()
        """
    proven_behind_a_flag = """
        def test_maybe_skipped(some_flag):
            if some_flag:
                if sys.platform == "win32":
                    pytest.skip("sometimes")
            open("/etc/passwd").close()
        """
    import_or_skip = """
        def test_needs_a_module():
            pytest.importorskip("pwd")
            open("/etc/passwd").close()
        """
    skip_in_a_loop = """
        def test_looped(cases):
            for case in cases:
                if sys.platform == "win32":
                    pytest.skip("POSIX-only")
            open("/etc/passwd").close()
        """
    for label, snippet in (
        ("skip behind a flag", skip_behind_a_flag),
        ("proven behind a flag", proven_behind_a_flag),
        ("importorskip", import_or_skip),
        ("skip in a loop", skip_in_a_loop),
    ):
        assert [rule for rule, _ in _rules_fired(snippet)] == ["posix-literal"], label
    # The other direction: when BOTH nested conditions are provably true on
    # windows-latest, the skip really is reached and the unit is exempt.
    both_proven = """
        def test_posix_only():
            if sys.platform == "win32":
                if os.name == "nt":
                    pytest.skip("POSIX-only")
            open("/etc/passwd").close()
        """
    assert _rules_fired(both_proven) == []


def test_documented_generosities_of_the_exemption() -> None:
    """Pins the two spots the module docstring calls generous rather than
    strict, so the prose cannot drift from the code: a shape that only BUILDS
    a value is credited to a gate wherever the gate sits, including below it,
    and a needle matched anywhere in the condition counts even where the probe
    does not consult it. If either is tightened later, this test fails and the
    docstring has to be rewritten in the same change."""
    builders_below_the_gate = """
        def test_builds_paths(monkeypatch, tmp_path, home):
            seed = Path("/tmp/seed")
            replaced = tmp_path / "/etc/hosts"
            exact = str(home) + "/bm-audit-case-fold"
            monkeypatch.setenv("HOME", "/home/nobody")
            if sys.platform == "win32":
                pytest.skip("POSIX-only")
            return seed, replaced, exact
        """
    needle_in_an_unconsulted_position = """
        def test_reads_the_host_file():
            if not os.path.exists(os.environ.get("HOSTS", "/etc/hosts")):
                pytest.skip("requires a host file")
            assert os.path.isfile("/etc/hosts")
        """
    assert _rules_fired(builders_below_the_gate) == []
    assert _rules_fired(needle_in_an_unconsulted_position) == []


def test_a_gate_below_a_filesystem_touch_does_not_reach_back_to_excuse_it() -> None:
    """A skip stops what has not run yet, and nothing else.

    Every snippet writes its gate BELOW a call that reaches the filesystem
    where it stands, so on windows-latest that call has already raised (or
    already landed somewhere foreign) by the time the skip would run. Both
    gates appear, because the search they share used to scan the unit's whole
    statement chain without comparing source position — the third generation
    of one family, after the platform gate and then the probe gate were made
    direction-aware. ``touch inside the gate`` is why the comparison is
    against the SKIP and not the ``if`` above it: that call runs on Windows
    before the skip beneath it does.
    """
    platform_gate_below_a_read = (
        """
        def test_reads_the_host_file():
            os.stat("/etc/hosts")
            if sys.platform == "win32":
                pytest.skip("POSIX-only")
        """,
        ["posix-literal"],
    )
    platform_gate_below_an_open = (
        """
        def test_reads_the_host_file():
            open("/etc/passwd").close()
            if os.name == "nt":
                pytest.skip("POSIX-only")
        """,
        ["posix-literal"],
    )
    platform_gate_below_a_chmod_unlink = (
        """
        def test_cleanup(victim):
            victim.chmod(0o400)
            victim.unlink()
            if sys.platform == "win32":
                pytest.skip("POSIX permission semantics")
        """,
        ["chmod-unlink"],
    )
    probe_gate_below_a_read = (
        """
        def test_reads_the_host_file():
            assert os.path.isfile("/etc/hosts")
            if not os.path.exists("/etc/hosts"):
                pytest.skip("requires /etc/hosts")
        """,
        ["posix-literal"],
    )
    probe_gate_below_a_read_bound_to_the_needle = (
        """
        def test_stats_the_host_file():
            probe = os.stat("/etc/hosts")
            if not os.path.exists(probe):
                pytest.skip("requires /etc/hosts")
        """,
        ["posix-literal"],
    )
    touch_inside_the_gate_above_its_skip = (
        """
        def test_reads_the_host_file():
            if sys.platform == "win32":
                os.stat("/etc/hosts")
                pytest.skip("POSIX-only")
        """,
        ["posix-literal"],
    )
    for label, (snippet, expected) in (
        ("platform gate below a read", platform_gate_below_a_read),
        ("platform gate below an open", platform_gate_below_an_open),
        ("platform gate below a chmod/unlink", platform_gate_below_a_chmod_unlink),
        ("probe gate below a read", probe_gate_below_a_read),
        ("probe gate below a bound read", probe_gate_below_a_read_bound_to_the_needle),
        ("touch inside the gate", touch_inside_the_gate_above_its_skip),
    ):
        assert [rule for rule, _ in _rules_fired(snippet)] == expected, label


def test_a_gate_above_the_shape_still_excuses_it() -> None:
    """The direction the position comparison must not break.

    Each snippet is one from
    ``test_a_gate_below_a_filesystem_touch_does_not_reach_back_to_excuse_it``
    with the gate moved above the shape, plus the two mark spellings — a
    decorator sits ahead of the whole body by construction, so it excuses
    every shape in it and is never ordered against one. Without these the
    tightening above would just be a way to switch the exemptions off.
    """
    platform_gate_above_a_read = """
        def test_reads_the_host_file():
            if sys.platform == "win32":
                pytest.skip("POSIX-only")
            os.stat("/etc/hosts")
        """
    platform_mark_above_a_read = """
        @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
        def test_reads_the_host_file():
            os.stat("/etc/hosts")
        """
    platform_gate_above_a_chmod_unlink = """
        def test_cleanup(victim):
            if sys.platform == "win32":
                pytest.skip("POSIX permission semantics")
            victim.chmod(0o400)
            victim.unlink()
        """
    probe_gate_above_a_read = """
        def test_reads_the_host_file():
            if not os.path.exists("/etc/hosts"):
                pytest.skip("requires /etc/hosts")
            assert os.path.isfile("/etc/hosts")
        """
    probe_gate_above_a_read_bound_to_the_needle = """
        def test_stats_the_host_file():
            if not os.path.exists("/etc/hosts"):
                pytest.skip("requires /etc/hosts")
            probe = os.stat("/etc/hosts")
            return probe
        """
    probe_mark_above_a_read = """
        @pytest.mark.skipif(
            not os.path.exists("/etc/hosts"), reason="host file absent"
        )
        def test_reads_the_host_file():
            os.stat("/etc/hosts")
        """
    for label, snippet in (
        ("platform gate above a read", platform_gate_above_a_read),
        ("platform mark above a read", platform_mark_above_a_read),
        ("platform gate above a chmod/unlink", platform_gate_above_a_chmod_unlink),
        ("probe gate above a read", probe_gate_above_a_read),
        ("probe gate above a bound read", probe_gate_above_a_read_bound_to_the_needle),
        ("probe mark above a read", probe_mark_above_a_read),
    ):
        assert _rules_fired(snippet) == [], label


def test_undecidable_gates_fall_through_to_being_checked() -> None:
    """The disclosed narrowing: what the condition parser cannot read is not
    an exemption. A name INSIDE a condition — the alias hop reaches a bare
    decorator, never a name the condition itself is built from — a ``reason``
    that merely says "Windows", a ``skipif`` that gates one parametrised case,
    and a skip in an ``else`` branch all cost a false positive rather than a
    silent miss."""
    opaque_condition_name = """
        _IS_WINDOWS = sys.platform == "win32"

        @pytest.mark.skipif(_IS_WINDOWS, reason="POSIX-only")
        def test_maybe_gated():
            open("/etc/passwd").close()
        """
    reason_prose_only = """
        @pytest.mark.skipif(
            shutil.which("git") is None, reason="POSIX mode bits, not Windows"
        )
        def test_maybe_gated():
            open("/etc/passwd").close()
        """
    per_case_mark = """
        @pytest.mark.parametrize(
            "n",
            [pytest.param(1, marks=pytest.mark.skipif(os.name == "nt", reason="x"))],
        )
        def test_one_case_gated(n):
            open("/etc/passwd").close()
        """
    skip_in_else_branch = """
        def test_gated_the_long_way_round():
            if sys.platform != "win32":
                pass
            else:
                pytest.skip("POSIX-only")
            open("/etc/passwd").close()
        """
    for label, snippet in (
        ("opaque condition name", opaque_condition_name),
        ("reason prose only", reason_prose_only),
        ("per-case mark", per_case_mark),
        ("skip in else", skip_in_else_branch),
    ):
        assert [rule for rule, _ in _rules_fired(snippet)] == ["posix-literal"], label


def test_root_and_double_slash_literals_are_not_claims() -> None:
    """Bare "/" and "//"-prefixed strings fall outside the rule on purpose."""
    fired = _rules_fired(
        """
        def test_edges():
            a = Path("/")
            b = Path("//server/share")
            return a, b
        """
    )
    assert fired == []


def test_flags_slash_constant_concatenated_onto_str_of_a_path() -> None:
    """The shape behind the allowlist's founding entry — since repaired
    (the origin fixture appends os.sep now), so no live instance remains."""
    fired = _rules_fired(
        """
        def test_trailing_slash_spelling(tmp_path):
            spellings = [str(tmp_path), str(tmp_path) + "/"]
            return spellings
        """
    )
    assert fired == [
        ("slash-concat", "test_trailing_slash_spelling: str(tmp_path) + '/'")
    ]


def test_flags_the_repaired_concatenation_shape_through_its_name_hop() -> None:
    """The pre-repair text of the a70fb69 fixture, condensed.

    The slash constant was parked in ``tail`` first, so a rule reading only
    direct literals would have missed the one instance that motivated this
    file. The once-bound-name hop is what makes this fire.
    """
    fired = _rules_fired(
        """
        def test_home_exemption_follows_the_filesystem_on_case(home):
            tail = "/bm-audit-case-fold/src/handlers"
            exact = str(home) + tail
            return exact
        """
    )
    assert fired == [
        (
            "slash-concat",
            "test_home_exemption_follows_the_filesystem_on_case: str(home) + tail",
        )
    ]


def test_os_sep_construction_is_the_fixed_shape_and_stays_quiet() -> None:
    """The a70fb69 repair itself must not be flagged."""
    fired = _rules_fired(
        """
        def test_home_exemption_follows_the_filesystem_on_case(home):
            tail = os.sep + os.sep.join(("bm-audit-case-fold", "src", "handlers"))
            exact = str(home) + tail
            return exact
        """
    )
    assert fired == []


def test_slash_between_plain_strings_is_not_a_path_claim() -> None:
    """Real corpus shapes that must stay quiet: a repo-relative suffix match
    (git output is always forward-slashed), a URL, and synthetic length-cap
    prose. None involves a ``str(...)`` conversion, which is the tell that a
    native-rendered path is on one side."""
    fired = _rules_fired(
        """
        def test_various_string_glue(name, overshoot):
            a = rel.endswith("/" + name)
            b = "https://example.com/" + "r" * 400
            c = "/tmp/" + "a" * 600
            return a, b, c
        """
    )
    assert fired == []


def test_fstring_slash_interpolation_is_a_documented_blind_spot() -> None:
    """Pins the docstring's honesty: the f-string twin of slash-concat is
    deliberately not covered. If a rule for it lands, this test and the
    blind-spot paragraph must change together."""
    fired = _rules_fired(
        """
        def test_fstring_shape(tmp_path):
            body = f"installed at {tmp_path}/App.app on this machine"
            return body
        """
    )
    assert fired == []


def test_flags_chmod_readonly_then_unlink_across_a_nested_hook() -> None:
    """The pre-repair text of the 85a5f18 fixture, condensed: chmod in the
    test body, unlink inside a nested monkeypatch hook, nothing restoring
    the write bit between them."""
    fired = _rules_fired(
        """
        def test_fix_survives_a_segment_vanishing(tmp_path, monkeypatch):
            ghost = tmp_path / ".events.00.jsonl"
            ghost.write_text("", encoding="utf-8")
            ghost.chmod(0o400)

            def _vanish_after_the_glob(directory):
                if ghost.exists():
                    ghost.unlink()
                return []

            monkeypatch.setattr(mod, "_files", _vanish_after_the_glob)
        """
    )
    assert fired == [
        (
            "chmod-unlink",
            "test_fix_survives_a_segment_vanishing: ghost chmod(0o400) then unlink",
        )
    ]


def test_restoring_the_write_bit_before_unlink_stays_quiet() -> None:
    """The 85a5f18 repair itself: chmod(0o600) lands between, in source order."""
    fired = _rules_fired(
        """
        def test_fix_survives_a_segment_vanishing(tmp_path, monkeypatch):
            ghost = tmp_path / ".events.00.jsonl"
            ghost.chmod(0o400)

            def _vanish_after_the_glob(directory):
                if ghost.exists():
                    ghost.chmod(0o600)
                    ghost.unlink()
                return []

            monkeypatch.setattr(mod, "_files", _vanish_after_the_glob)
        """
    )
    assert fired == []


def test_writable_mode_then_unlink_is_not_the_class() -> None:
    fired = _rules_fired(
        """
        def test_cleanup(tmp_path):
            victim = tmp_path / "x"
            victim.chmod(0o644)
            victim.unlink()
        """
    )
    assert fired == []


def test_os_chmod_and_os_remove_spellings_are_covered() -> None:
    fired = _rules_fired(
        """
        def test_module_level_spellings(victim):
            os.chmod(victim, 0o444)
            os.remove(victim)
        """
    )
    assert fired == [
        ("chmod-unlink", "test_module_level_spellings: victim chmod(0o444) then remove")
    ]


def test_unknown_chmod_mode_is_not_guessed() -> None:
    """A variable or stat-constant mode resolves to nothing; the rule treats
    it as restoring rather than guessing — precision over recall, as the
    module docstring discloses."""
    fired = _rules_fired(
        """
        def test_variable_mode(victim, mode):
            victim.chmod(mode)
            victim.unlink()
        """
    )
    assert fired == []


def test_chmod_unlink_under_a_platform_gate_is_exempt() -> None:
    fired = _rules_fired(
        """
        def test_posix_only_permissions(victim):
            if sys.platform == "win32":
                pytest.skip("POSIX permission semantics")
            victim.chmod(0o400)
            victim.unlink()
        """
    )
    assert fired == []
