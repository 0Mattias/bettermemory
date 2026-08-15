"""Structured claims — declared at write time, checked at declaration,
watched by the commit-drift leg.

The extraction problem this module refuses to solve
---------------------------------------------------
Every earlier attempt to know WHAT a memory asserts read the prose:
`symbols.py` parses a two-token docstring convention and stays advisory
because widening it would multiply guesses; the path leg stats files a
regex scraped out of sentences; the commit leg counts commits touching
those files, never knowing which line the memory cares about. On the
30-repository corpus that file-level signal costs **3.4 alerts per
genuine catch** (`bench/rot/results/multirepo-anchored-2026-07-30.json`,
`pooled.file_level_incumbent`). The claim-level `weak` tier measured on
the same corpus costs **1.1 at 94% precision** — but it needs a claim,
and a real-prose claim extractor is an open problem.

It is only an open problem post-hoc. The AUTHOR of a memory knows what
it is claiming at the moment of writing. This module gives that
knowledge a wire shape: the caller passes `claims=[...]` to
`memory_write` / `memory_verify`, each entry naming one checkable
assertion. No extractor, no guessing — the firewall that kept the
bench's detector honest (`build_binding_index` cannot see a claim;
the claim side entered only as rendered prose) inverts into a product
guarantee: the structure the detector consumes is structure the author
supplied.

The wire syntax
---------------
One string per claim, four shapes. The first three are the kinds
`bench/rot`'s corpus measured; the fourth is their polarity mirror,
scoped by T1's live-store census
(`bench/rot/T2_ABSENCE_CLAIM_DECLARATION.md`):

- ``src/pkg/mod.py`` — a PATH claim: the file exists.
- ``src/pkg/mod.py::name`` — a SYMBOL claim: `name` is a top-level
  `def`/`class` in that module.
- ``src/pkg/mod.py::NAME=value`` — a LITERAL claim: module-level
  constant `NAME` is assigned that literal (compared in canonical
  `repr` space after `ast.literal_eval`: `30` and `30.0` stay
  distinct, while `{8, 16}` and `{16, 8}` are one claim — see
  `_canonical_repr`).
- ``!src/pkg/mod.py`` — an ABSENCE claim: nothing exists at that
  path. Declaration refuses while anything — file or directory —
  occupies it, and the drift polarity inverts: reappearance is the
  drift. Path-only; ``!path::x`` is refused, because symbol- and
  literal-absence have no measured evidence base (T-P4's cohort is
  paths) and loosening an oracle without re-running the bench ships
  an unmeasured detector wearing measured numbers.

Paths are stored repo-relative with forward slashes. `::` was chosen
because coding agents already read it as file-scoped addressing
(pytest node ids); `=` binds looser than `::` so a value may contain
`::` but a symbol name may not — names are identifiers. `!` was chosen
for absence because it cannot begin a Python identifier (no collision
with the symbol/literal shapes) and the old grammar would only ever
have admitted a `!`-prefixed path if a file literally so named passed
the existence gate — the live store held 0 such claims in 595 when the
marker was declared.

Checked at declaration, not trusted
-----------------------------------
`check_claim` re-implements the bench ORACLE (`label_claim`): path
existence, a top-level AST lookup, a literal comparison — the dullest
possible checks, promoted verbatim rather than re-derived so the
shipped gate stays the measured one. A claim that fails RIGHT NOW is
refused at declaration; `memory_verify` re-runs the same oracle over
STORED claims and refuses to stamp `last_verified_at` over a false
one. The declare-time gate is what makes the read-side signal cheap:
drift detection never has to wonder whether the claim was ever true.

Two deliberate narrownesses, inherited from the measured oracle: a
symbol claim is about `ast.Module.body` — a method, a nested def, or a
name bound under `if TYPE_CHECKING:` is NOT a top-level binding (claim
the class, or the module path, instead); a literal claim reads plain
`ast.Assign` with one `ast.Name` target — an annotated assignment
(`X: Final = 3`) is invisible to it, and the first binding wins when a
name is rebound. Loosening either half without re-running the bench
would ship an unmeasured detector wearing measured numbers.

The read side
-------------
`build_binding_index` / `claim_level_drift` are the bench detector,
moved here so `bench/rot/run.py` imports the SHIPPED functions (its
stated ethos — "the shipped function, not a reimplementation").
`verify.resolve_commit_drift_count` composes them: for a memory
carrying claims, commits that touched a claim-governed file escalate
the staleness verdict only when the `weak` tier says the touched lines
implicate a claimed binding. Files the memory cites but never claims
keep the incumbent any-touch rule — declaring a claim narrows the
alarm for that file, silence keeps the conservative default.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_CLAIMS",
    "MAX_CLAIM_LEN",
    "COMMIT_MARK",
    "Claim",
    "parse_claim",
    "parse_claims",
    "load_claims",
    "check_claim",
    "claim_paths",
    "build_binding_index",
    "claim_level_drift",
    "string_fragment",
    "anchors_from_value",
]

# Caps mirror the `verified_*` attestation caps at the memory_verify
# handler boundary (`handlers/verify._MAX_VERIFIED_ENTRIES` /
# `_MAX_VERIFIED_ITEM_LEN`): a memory claims a handful of facts, not a
# manifest, and a claim is a short address, not prose.
MAX_CLAIMS = 64
MAX_CLAIM_LEN = 1024

# Record separator for `git log` patch streams. A control character
# rather than a text marker so it can never collide with source content
# — note this makes the stream binary to `grep` and friends when
# debugging. Same constant the bench used; exported so the git plumbing
# in `origin.py` and the bench emit the exact stream this parser reads.
COMMIT_MARK = "\x01"

# Column-0 binding shapes. No `lstrip`: an indented `def` is a method or
# nested function, which is not what a top-level-symbol claim asserts —
# matching indented lines would flag every method edit in a class as
# drift on the class's own claim. The assign pattern tolerates an
# annotation and augmented operators so a `NAME: int = 3` or `NAME += 1`
# edit still reads as touching the binding.
_DEF_RE = re.compile(r"^(?:async[ \t]+)?(?:def|class)[ \t]+([A-Za-z_]\w*)[ \t]*[(\[:]")
_ASSIGN_RE = re.compile(
    r"^([A-Za-z_]\w*)[ \t]*(?::[^=\n]+)?"
    r"(?:\+|-|\*|/|//|%|\*\*|>>|<<|&|\^|\|)?=(?!=)"
)
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# An anchor line must carry this many non-whitespace characters to be
# treated as a content address. Short interior lines (`}`, `],`,
# `"name",`) recur all over a file and would attribute unrelated edits
# to the literal.
_MIN_ANCHOR_CHARS = 12

_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")

# Same ceiling `symbols.py` uses before refusing to parse a cited
# module: a multi-megabyte file is not where module-level claims live,
# and `ast.parse` on one inside a write handler is a latency cliff.
_MAX_MODULE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Claim:
    """One declared assertion. What the drift detector is allowed to know.

    Field-compatible with the bench's `Citation` on purpose — the bench
    now constructs THIS class, so the detector the corpus measured and
    the detector production runs cannot drift apart structurally.
    `value` is `_canonical_repr`-normalized for literal claims and `""`
    otherwise.
    """

    kind: str
    rel_path: str
    name: str
    value: str

    def render(self) -> str:
        """The canonical wire string — what gets stored and displayed."""
        if self.kind == "absent":
            return f"!{self.rel_path}"
        if self.kind == "path":
            return self.rel_path
        if self.kind == "symbol":
            return f"{self.rel_path}::{self.name}"
        return f"{self.rel_path}::{self.name}={self.value}"


def _canonical_repr(value: Any) -> str:
    """`repr`, with unordered containers rendered in one fixed order.

    Plain `repr` is not canonical for sets (iteration order follows the
    per-process hash seed and insertion/collision layout) or dicts (key
    insertion order), so the same value can render two ways — refusing a
    true claim at declaration and flapping a stored claim at verify once
    a new server process rolls a new seed. Sets render their elements
    sorted by canonical repr, dicts sort items the same way, sequences
    keep their order (it is semantic) and recurse. Scalars stay bare
    `repr`, so `30` and `30.0` remain distinct claims.
    """
    if isinstance(value, set):
        if not value:
            return "set()"
        return "{" + ", ".join(sorted(_canonical_repr(v) for v in value)) + "}"
    if isinstance(value, dict):
        items = sorted(
            (_canonical_repr(k), _canonical_repr(v)) for k, v in value.items()
        )
        return "{" + ", ".join(f"{k}: {v}" for k, v in items) + "}"
    if isinstance(value, tuple):
        if len(value) == 1:
            return "(" + _canonical_repr(value[0]) + ",)"
        return "(" + ", ".join(_canonical_repr(v) for v in value) + ")"
    if isinstance(value, list):
        return "[" + ", ".join(_canonical_repr(v) for v in value) + "]"
    return repr(value)


def parse_claim(raw: str) -> Claim:
    """Parse one wire string into a `Claim`, or raise ValueError.

    The error messages name the exact defect — these surface verbatim in
    the memory_write / memory_verify refusal, which is the only place a
    caller learns the syntax beyond the one-line tool description.

    `raw` is already type-validated at the handler boundary (the same
    explicit isinstance loop the `verified_*` lists go through); this
    function owns syntax, not typing.
    """
    text = raw.strip()
    if not text:
        raise ValueError("a claim cannot be empty")
    if len(text) > MAX_CLAIM_LEN:
        raise ValueError(
            f"claim is {len(text)} chars — cap is {MAX_CLAIM_LEN}. Claims "
            "are short path/symbol addresses, not prose."
        )
    absent = text.startswith("!")
    if absent:
        text = text[1:].lstrip()
        if not text:
            raise ValueError(
                "an absence claim needs a path after '!' (`!src/gone.py` "
                "asserts nothing exists at that path)"
            )
    path_part, sep, rest = text.partition("::")
    path_part = path_part.strip()
    if not path_part:
        raise ValueError(f"claim {text!r} has no path before '::'")
    if "\\" in path_part:
        raise ValueError(f"claim path {path_part!r} must use forward slashes")
    if absent:
        if sep:
            raise ValueError(
                f"claim {raw.strip()!r}: absence claims are path-only — "
                "`!path` asserts nothing exists at that path; symbol and "
                "literal absence are not claimable kinds"
            )
        return Claim("absent", path_part, path_part, "")
    if not sep:
        return Claim("path", path_part, path_part, "")
    rest = rest.strip()
    if not rest:
        raise ValueError(
            f"claim {text!r} ends at '::' — name a symbol "
            "(`path::name`) or a constant (`path::NAME=value`)"
        )
    name, eq, value = rest.partition("=")
    name = name.strip()
    if not _IDENT_RE.match(name):
        raise ValueError(f"claim symbol {name!r} is not a Python identifier")
    if not eq:
        return Claim("symbol", path_part, name, "")
    value = value.strip()
    if not value:
        raise ValueError(
            f"claim {text!r} ends at '=' — write the literal value, "
            "quoting strings (`path::NAME='foo'`)"
        )
    try:
        normalized = _canonical_repr(ast.literal_eval(value))
    except Exception:
        raise ValueError(
            f"claim value {value!r} is not a Python literal — quote "
            "strings (`path::NAME='foo'`), and claim the symbol form "
            "(`path::NAME`) for values that aren't literals"
        ) from None
    return Claim("literal", path_part, name, normalized)


def parse_claims(raw: list[str]) -> list[Claim]:
    """Parse a wire list, enforcing the count cap and per-entry syntax.

    Duplicates (after normalization) collapse silently — attesting the
    same fact twice is redundancy, not an error.
    """
    if len(raw) > MAX_CLAIMS:
        raise ValueError(
            f"claims capped at {MAX_CLAIMS} entries (got {len(raw)}); "
            "a memory claims a handful of facts, not a manifest"
        )
    out: list[Claim] = []
    seen: set[str] = set()
    for entry in raw:
        claim = parse_claim(entry)
        rendered = claim.render()
        if rendered in seen:
            continue
        seen.add(rendered)
        out.append(claim)
    return out


def load_claims(raw: list[str] | tuple[str, ...]) -> list[Claim]:
    """Parse STORED claim strings, skipping entries that no longer parse.

    The lenient mirror of `parse_claims`: declaration refuses a bad
    entry, but the read side never crashes on a hand-edited frontmatter
    list — an invalid entry contributes nothing to drift detection
    rather than poisoning retrieval for the whole record.
    """
    out: list[Claim] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        try:
            out.append(parse_claim(entry))
        except ValueError:
            continue
    return out


def claim_paths(claims: list[Claim]) -> list[str]:
    """Distinct rel_paths across `claims`, declaration order preserved."""
    seen: dict[str, None] = {}
    for claim in claims:
        seen.setdefault(claim.rel_path, None)
    return list(seen)


def _resolve_claim_path(root: Path, rel_path: str) -> Path | None:
    """Resolve a claimed path under `root`, or None when it escapes.

    Same discipline as `symbols._resolve_module`: plain relative paths
    only, no `..`/`~`/drive-letter escapes, containment checked AFTER
    symlink resolution so a link pointing out of the tree is caught.
    Unlike that function, a bare basename is allowed — `README.md` at
    the repo root is a legitimate claim — because here the path is
    resolved against a DECLARED root, not searched for.
    """
    if rel_path.startswith(("/", "~")):
        return None
    if len(rel_path) > 1 and rel_path[1] == ":":
        return None
    parts = rel_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    try:
        resolved_root = root.resolve(strict=False)
        target = (resolved_root / rel_path).resolve(strict=False)
        if not target.is_relative_to(resolved_root):
            return None
        return target
    except (OSError, ValueError):
        return None


def check_claim(claim: Claim, root: Path) -> str | None:
    """The declare-time oracle: None when the claim holds, else why not.

    Promoted from `bench/rot`'s `label_claim` — existence, a top-level
    AST lookup, a literal comparison, and never an inference. The one
    addition is the reason string: the bench needed a label, a refused
    caller needs to know what the tree actually says.

    The `absent` kind is the same oracle with the existence check
    inverted: it holds when NOTHING occupies the resolved path — a
    directory defeats an absence claim exactly as it fails a path
    claim's `is_file()`. Resolution and containment are unchanged, so
    escapes refuse identically for both polarities. Deliberately no
    git-history requirement at the gate: a never-existed path is a
    weaker but not a false absence claim, and history enters on the
    read side, where it already lives.
    """
    target = _resolve_claim_path(root, claim.rel_path)
    if target is None:
        return (
            f"path {claim.rel_path!r} does not resolve inside the "
            "worktree — claims are anchored to the memory's origin "
            "worktree; use verified_paths for out-of-tree attestations"
        )
    if claim.kind == "absent":
        if target.exists():
            return (
                f"path {claim.rel_path!r} exists in the worktree — an "
                "absence claim (`!path`) asserts it stays deleted; "
                "remove it, or drop the claim if it is meant to be back"
            )
        return None
    if not target.is_file():
        return f"path {claim.rel_path!r} does not exist in the worktree"
    if claim.kind == "path":
        return None
    try:
        if target.stat().st_size > _MAX_MODULE_BYTES:
            return (
                f"{claim.rel_path!r} is too large to AST-check "
                f"(> {_MAX_MODULE_BYTES} bytes)"
            )
        source = target.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return f"{claim.rel_path!r} could not be read"
    try:
        parsed = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        # Not just SyntaxError: a NUL byte raised ValueError before
        # 3.12, and deeply nested literals raise RecursionError. A file
        # that does not parse cannot support a claim about its bindings.
        return f"{claim.rel_path!r} does not parse as Python"
    if claim.kind == "symbol":
        for node in parsed.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == claim.name:
                    return None
        return (
            f"`{claim.name}` is not a top-level def/class in "
            f"{claim.rel_path!r} — methods and nested defs are not "
            "top-level bindings; claim the enclosing class or the path"
        )
    # Literal claim. First binding wins on rebinding (`setdefault`), the
    # same rule the bench oracle pinned — a name rebound at module level
    # must keep resolving to its first binding.
    for node in parsed.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            targ = node.targets[0]
            if isinstance(targ, ast.Name) and targ.id == claim.name:
                try:
                    current = _canonical_repr(ast.literal_eval(node.value))
                except Exception:
                    return (
                        f"`{claim.name}` in {claim.rel_path!r} is not a "
                        "literal assignment — claim the symbol form "
                        f"(`{claim.rel_path}::{claim.name}`) instead"
                    )
                if current == claim.value:
                    return None
                return (
                    f"`{claim.name}` in {claim.rel_path!r} is currently "
                    f"`{current}`, not `{claim.value}` — update the "
                    "claim (and the body, if it states the old value)"
                )
    return (
        f"`{claim.name}` is not assigned at module level in "
        f"{claim.rel_path!r} (annotated assignments like "
        f"`{claim.name}: T = ...` are not readable as literal claims; "
        "claim the symbol or path form instead)"
    )


def _binding_token(content: str) -> tuple[str, str] | None:
    """Map a changed line to at most one column-0 binding, or None.

    No `lstrip` — see `_DEF_RE`'s note. This discipline (column-0
    matching on the changed lines themselves) is the entire difference
    between the measured detector and a name-grep; it is what excludes
    methods, nested defs, keyword arguments (`foo(TIMEOUT=30)`) and
    dict entries (`"TIMEOUT": 30`).
    """
    match = _DEF_RE.match(content)
    if match:
        return ("def", match.group(1))
    match = _ASSIGN_RE.match(content)
    if match:
        return ("assign", match.group(1))
    return None


def _rhs_repr(content: str) -> str | None:
    """The assignment's right-hand side, normalised to canonical repr.

    Matching `Claim.value`, which is itself
    `_canonical_repr(ast.literal_eval(...))`, is what makes the
    comparison type-sensitive: `30` and `30.0` must not compare equal —
    the bench treats that change as genuine drift.
    """
    _, sep, rhs = content.partition("=")
    if not sep:
        return None
    try:
        return _canonical_repr(ast.literal_eval(rhs.strip()))
    except Exception:
        return None


def string_fragment(content: str) -> str | None:
    """The decoded text of a source line that is a bare string literal.

    Python implicit concatenation means a long constant's LOGICAL lines
    and the file's PHYSICAL lines are different objects — a logical line
    routinely spans several physical ones. Measured on the bench corpus,
    whole-line anchors missed 12 of the 20 literal claims that actually
    went false, every one a multi-line tool description. So invert it:
    decode each changed physical line back to the text it contributes,
    and ask whether that text appears ANYWHERE in the claimed value.
    `ast.literal_eval` rather than string surgery because the source
    carries escapes (`\\"`, `\\n`) the value does not.

    Returns None for a line that is not a self-contained string literal.
    """
    stripped = content.strip().rstrip(",")
    # A trailing `)` closes the enclosing parenthesised concatenation,
    # not the string; leading `(` opens it. Neither belongs to the
    # literal.
    stripped = stripped.removesuffix(")").removeprefix("(").strip()
    if not stripped or stripped[0] not in "\"'":
        return None
    try:
        decoded = ast.literal_eval(stripped)
    except Exception:
        return None
    return decoded if isinstance(decoded, str) else None


def anchors_from_value(value: str) -> tuple[str, ...]:
    """Whole-line content addresses for a multi-line literal.

    Kept alongside `string_fragment` because it catches the case that
    one misses: a value whose physical and logical lines DO coincide (a
    triple-quoted block), where the changed line is not a self-contained
    string literal and so decodes to nothing. Derived from the claimed
    VALUE, which is in the memory record, so drift detection has the
    same material the bench's firewall allowed.
    """
    try:
        literal = ast.literal_eval(value)
    except Exception:
        return ()
    if not isinstance(literal, str) or "\n" not in literal:
        return ()
    seen: dict[str, None] = {}
    for line in literal.split("\n"):
        stripped = line.strip()
        if len(stripped) >= _MIN_ANCHOR_CHARS:
            seen[stripped] = None
    return tuple(seen)


def build_binding_index(diff_text: str) -> dict[str, Any]:
    """Parse one `git log -p -U0` stream into a claim-agnostic index.

    THE SIGNATURE IS THE GUARANTEE: one argument, the diff text. This
    function cannot see a claim, so it cannot be accused of looking one
    up. Every claim-specific decision happens later, against this index.

    At `-U0` a hunk contains exactly `b` removed and `d` added lines and
    no context, so consumption is exact rather than greedy, and the
    `minus == b and plus == d` check turns a malformed parse into a loud
    failure instead of a quietly under-counting detector. That matters
    because file content can itself contain `diff --git` and `@@` lines.
    """
    bindings: dict[tuple[str, str, str], dict[str, Any]] = {}
    changed_text: dict[str, dict[str, set[str]]] = {}
    changed_fragments: dict[str, dict[str, set[str]]] = {}
    deleted: set[str] = set()
    files: set[str] = set()
    commits: set[str] = set()
    hunks = 0
    mismatches = 0

    sha = ""
    path = ""
    lines = diff_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith(COMMIT_MARK):
            sha = line[1:].strip()
            commits.add(sha)
            path = ""
            continue
        if line.startswith("diff --git "):
            path = ""
            continue
        if line.startswith("--- ") and not line.startswith("--- a/"):
            continue
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                # Deletion: the authoritative path is the a/ side, which
                # we already recorded when we saw it.
                if path:
                    deleted.add(path)
            else:
                path = target[2:] if target.startswith("b/") else target
                files.add(path)
            continue
        if line.startswith("--- a/"):
            path = line[6:].strip()
            files.add(path)
            continue
        if not line.startswith("@@"):
            continue
        match = _HUNK_RE.match(line)
        if not match or not path:
            continue
        hunks += 1
        removed = int(match.group(2) or 1)
        added = int(match.group(4) or 1)
        minus = plus = 0
        for _ in range(removed + added):
            if i >= len(lines):
                break
            body_line = lines[i]
            i += 1
            if body_line.startswith("\\"):
                continue
            if body_line.startswith("-"):
                minus += 1
                side = "removed"
            elif body_line.startswith("+"):
                plus += 1
                side = "added"
            else:
                # Not a hunk body line at -U0 — the stream is not shaped
                # the way this parser assumes. Back up and let the outer
                # loop resynchronise on the next header.
                i -= 1
                break
            content = body_line[1:]
            stripped = content.strip()
            if len(stripped) >= _MIN_ANCHOR_CHARS:
                changed_text.setdefault(path, {}).setdefault(stripped, set()).add(sha)
            fragment = string_fragment(content)
            if fragment is not None and len(fragment.strip()) >= _MIN_ANCHOR_CHARS:
                changed_fragments.setdefault(path, {}).setdefault(fragment, set()).add(
                    sha
                )
            token = _binding_token(content)
            if token is None:
                continue
            key = (path, token[0], token[1])
            entry = bindings.setdefault(
                key,
                {
                    "commits": set(),
                    "adds": 0,
                    "removes": 0,
                    "edit_lines": 0,
                    "rhs_added": set(),
                    "rhs_removed": set(),
                },
            )
            entry["commits"].add(sha)
            entry["edit_lines"] += 1
            if side == "added":
                entry["adds"] += 1
            else:
                entry["removes"] += 1
            if token[0] == "assign":
                rhs = _rhs_repr(content)
                if rhs is not None:
                    entry[f"rhs_{side}"].add(rhs)
        if minus != removed or plus != added:
            mismatches += 1

    return {
        "bindings": bindings,
        "changed_text": changed_text,
        "changed_fragments": changed_fragments,
        "deleted": deleted,
        "files": files,
        "commits": len(commits),
        "hunks": hunks,
        "parse_mismatches": mismatches,
    }


def claim_level_drift(cite: Claim, index: dict[str, Any]) -> dict[str, Any]:
    """Score one claim against the index. Two tiers, both reported.

    STRICT is the verdict channel: the binding NET-DISAPPEARED (more
    removals than re-additions), the asserted value was removed and not
    put back, a content anchor moved, or the file is gone. Every route by
    which the bench oracle can return "false" — rename, delete,
    de-top-level by indentation, move-and-re-export — puts the
    `def`/`class` line itself into a hunk, so narrowing this far costs
    no recall.

    WEAK is "the binding was touched at all". On the 30-repository
    corpus it costs 1.1 alerts per catch at 94% precision — the tier
    `verify.resolve_commit_drift_count` escalates on, because a touched
    binding is exactly the spot-check-this signal, while STRICT alone
    would go quiet on in-place edits that changed what the line says.

    The ABSENT kind inverts both tiers' polarity: weak = the claimed
    path was touched at all in the window, strict = touched and NOT
    net-deleted — the window re-created a path the claim asserts stays
    gone. Additive branch on a kind the measured corpus never contains;
    the three measured kinds' code paths are untouched.

    A BODY-ONLY EDIT IS DELIBERATELY NOT DRIFT. The oracle matches a
    definition by `.name` and never inspects its contents, so a body
    edit leaves the label `still_true` BY CONSTRUCTION — pinned by
    `test_pure_reformat_is_not_drift` in the bench. Counting body churn
    could therefore only manufacture false positives, never a catch.
    """
    path_gone = cite.rel_path in index["deleted"]
    empty: dict[str, Any] = {
        "commits": set(),
        "adds": 0,
        "removes": 0,
        "edit_lines": 0,
        "rhs_added": set(),
        "rhs_removed": set(),
    }
    anchor_commits = 0
    value_gone = False
    anchor_shas: set[str] = set()

    if cite.kind == "path":
        entry = empty
        strict = path_gone
        weak = path_gone
    elif cite.kind == "absent":
        # Polarity mirror of the path kind. The window STARTS absent
        # (the declare-time oracle gated on absence), so a window
        # showing both a touch and a deletion can only be
        # add-then-delete — it ends absent, and weak-only is correct.
        # The set-based index cannot order events, but the invariant
        # closes the gap; the one degradation (add-delete-add in a
        # single window reads weak-only) is caught by the verify gate
        # on the next stamp attempt.
        entry = empty
        touched = cite.rel_path in index["files"]
        strict = touched and not path_gone
        weak = touched
    elif cite.kind == "symbol":
        entry = index["bindings"].get((cite.rel_path, "def", cite.name), empty)
        strict = path_gone or (entry["removes"] - entry["adds"]) > 0
        weak = path_gone or bool(entry["commits"])
    else:
        entry = index["bindings"].get((cite.rel_path, "assign", cite.name), empty)
        value_gone = (
            cite.value in entry["rhs_removed"] and cite.value not in entry["rhs_added"]
        )
        per_file = index["changed_text"].get(cite.rel_path, {})
        hit = anchor_shas
        for anchor in anchors_from_value(cite.value):
            hit |= per_file.get(anchor, set())
        # Substring containment, the direction that survives implicit
        # concatenation — see `string_fragment`.
        try:
            claimed = ast.literal_eval(cite.value)
        except Exception:
            claimed = None
        if isinstance(claimed, str):
            for fragment, shas in (
                index["changed_fragments"].get(cite.rel_path, {}).items()
            ):
                if fragment in claimed:
                    hit |= shas
        anchor_commits = len(hit)
        strict = path_gone or value_gone or anchor_commits > 0
        weak = path_gone or bool(entry["commits"]) or anchor_commits > 0

    # `binding_shas` / `anchor_shas` are additive keys the product's
    # commit counting reads (`verify.resolve_commit_drift_count` unions
    # them into an exact distinct-commit count); the bench scores only
    # the original keys. A path claim carries neither — the caller
    # attributes a gone path to the post-since commits touching it,
    # since the deletion commit's identity never entered this index.
    return {
        "cite_commits": len(entry["commits"]) + anchor_commits,
        "cite_edit_lines": entry["edit_lines"],
        "anchor_commits": anchor_commits,
        "value_gone": value_gone,
        "strict": strict,
        "weak": weak,
        "binding_shas": set(entry["commits"]),
        "anchor_shas": anchor_shas,
    }
