"""Mechanical pin on *measurement* claims made by resident surfaces.

The companion module tests/test_doc_claims.py closes the mechanically-
decidable slice of "prose asserts something the code does not do": paths,
symbols, test counts, line refs, file counts. Numbers were its one
structural blind spot, and every surviving false claim had collected
there — a recall figure no committed artifact ever measured, told to every
model on every turn inside a tool description; a schema-cost figure on the
front page that understated the real serialization and split it half-and-half
when the real split is nearer three-quarters. This module closes that slice,
so the next unbacked number fails CI instead of shipping.

The rule it enforces, in one sentence: **a number presented as a
measurement on a resident surface must be derivable from an artifact
committed to this repo.**

Design bias, inherited deliberately: a checker with false positives gets
disabled, and a disabled checker protects nothing. Both extraction rules
below were run against the whole real corpus before being written down and
tightened until their misfires were zero. Where a claim's shape is
ambiguous, it is dropped rather than guessed, and the drop is named in the
not-checked list.

Surfaces
--------
1. **Every tool description.** The DESC constants re-exported by
   `src/bettermemory/_handlers.py` and defined next to their handlers under
   `src/bettermemory/handlers/`. These are the highest-stakes prose in the
   repo: resident in the model's context on every single turn.
2. **The server `instructions` block**, built in
   `src/bettermemory/builder.py`. Same residency, system-prompt level.
3. **Tracked markdown**, derived from ``git ls-files`` rather than listed —
   README.md and docs/internals.md, where this project's own cost is
   summarised and then stated in full, plus every other document that
   scans clean today. A document that still carries an unrepaired backlog
   is named in ``_DOC_SURFACE_EXCLUSIONS`` with its reason, and the two
   guards beside that constant keep the split honest in both directions:
   nothing may sit outside both sets, and an entry whose backlog is
   repaired is forced back into the corpus.
4. **`src/bettermemory/doctor.py`, whole-file prose** (docstrings,
   full-line comment blocks, string literals). Not arbitrary: doctor exists
   for one purpose, telling an operator the truth about their store, and it
   is the one module whose strings are printed verbatim to a human and
   quoted back by a model. Its threshold comments are the provenance record
   for the numbers those strings print, so provenance is held to the same
   standard as output. This is the strictest surface in the repo by intent.

Artifacts
---------
The pinnable set is the committed bench results — the JSON under each
bench's results directory, plus docs/eval/comparative-live-2026-07-03.json.
Bench *logs* are not tracked, so a claim can only cite a result file. A
citation resolves either directly (a claim naming a .json artifact) or by
directory (a claim naming a bench, e.g. its README, pins against every
result file that bench has committed).

What is checked
---------------
Two rules, both scoped to a chunk — a markdown list item, a prose
paragraph, or one Python string literal / comment block. Chunk scope is
what makes a citation local: a number is pinned against the artifact its
own claim names, not against one named elsewhere in the file.

1. ``measured`` — a chunk carrying a measurement cue (the word "measured"
   and its family) *and* a measurement-shaped number must cite an artifact,
   and every measurement-shaped number in the chunk must be derivable from
   it. This is the discriminator that works on this corpus, and it was
   chosen over the obvious one for a specific reason: a bare percent-or-size
   pattern over the descriptions false-positives on almost every hit,
   because those numbers are contract constants enforced by adjacent code —
   the episode frontmatter ceilings, the note and excerpt caps, the
   contradicted-outcome weighting, the groundedness threshold. None of them
   is a measurement. The cue separates the two populations cleanly.

2. ``size`` — on the markdown surfaces only, a byte-size or ratio token
   must pin whether or not the chunk carries a cue. A serialized-byte
   figure or an "N times bigger" ratio about this project's own footprint
   is a measurement by construction; there is no reading of it that is a
   contract constant. This rule exists because the cue rule has a hole a
   real defect shipped through: the README's cost claim carried no cue at
   all, so nothing anchored on the word "measured" would have seen it. It
   stops short of the descriptions and the instructions block precisely
   because those are where the enforced byte ceilings live.

Numbers are recognised by shape, not by any list: a percentage, a ratio
with an ``x``, a value with a byte or character unit, a comma-grouped
integer, or a bare fraction below one. A number is derivable from an
artifact when the artifact holds a value it rounds or truncates to, after
the obvious unit conversions. When a chunk names a metric the artifact
also names, the pool narrows to that metric — a claim that says recall@1
is pinned against the recall@1 rows and not against every number in the
file.

What is deliberately NOT checked
--------------------------------
* **Bare counts.** "The 18 default tools", "27 MCP tools", "180 synthetic
  documents" — an integer with no unit is not distinguishable from an
  enumeration, and the tool counts already have their own triple guard
  (see tests/test_tool_surface.py and the prose scan in tests/test_eval.py).
  Adding a shape here that swallowed them would double-govern.
* **Dates and version strings.** Masked before extraction. They are
  provenance, not measurement, and they are checked implicitly: a claim's
  cited artifact records its own date and version, and a re-run that moves
  them moves the numbers too.
* **Percentages outside a cued chunk.** Unlike bytes, a percentage in prose
  is often a threshold or a share rather than a measurement, so the size
  rule stops at bytes and ratios. A percentage claim needs its cue.
* **Every other module under src/.** Measured at HEAD, a whole-tree sweep
  finds a large population of ad-hoc performance comments — microsecond
  timings, speedup factors, contention figures — that were measured once on
  one machine and were never artifacts. Sweeping them would need a
  permanent allowlist covering most of the findings, which is the exact
  shape of a guard that gets ignored. They are not resident surfaces and
  they are not marketing; doctor is included because it is both.
* **Semantic correctness of a pinned number.** Membership in the cited
  artifact is a necessary condition, not a sufficient one: this rule cannot
  tell that a rate quoted for one probe was measured for another. The
  metric-narrowing step above buys back part of it and no more. When such a
  claim is found, the repair belongs in the prose; do not expect the guard
  to find the next one.
* **Numbers inside URLs, HTML attributes and fenced code blocks.** Masked.
  A version badge's query string is not prose.
* **Anything the extraction misses.** A claim phrased without a cue and
  without a recognised number shape is invisible here. That is the honest
  cost of zero misfires, and it is the reason the size rule exists at all —
  it was added after the cue rule was shown to have let a real defect
  through.

How the ratchet works
---------------------
``_ALLOWLIST`` is empty at HEAD, and that is a deliberate property rather
than an accident. The truth-sync pass ahead of this module repaired most of
what the rules find; the two it left standing — a threshold comment's
invented spread across "real scopes", and a fix hint's invented perfect
rate on rare-term queries, both in operator-facing prose — were repaired in
the same commit as this module rather than exempted, because a guard born
with its findings exempted protects nothing. Should an entry ever be needed, the
same two paired tests as the doc-claims module apply — a forward guard that
fails on any unexpected finding, and a reverse guard that fails on any
entry no longer matching a real one, with the two causes of a stale entry
spelled out because they need opposite responses. Both are proved to fire
against synthetic input, since an empty allowlist makes the live assertions
vacuous.

Self-tests at the bottom feed three kinds of input through the real
extractor: a fabricated number injected into a copy of a real description,
a copy of a currently-passing surface with one digit changed, and verbatim
copies of the three claims this repo shipped before the truth-sync. The
last group is what demonstrates teeth on real defects rather than on
invented ones. They are held in module constants and comments rather than
in docstrings, because a docstring in this file enters the doc-claims
corpus and a synthetic example there would be read as an assertion.
"""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import tokenize
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from _pytest.outcomes import Failed

from bettermemory.builder import build_server
from bettermemory.config import Config, StorageConfig
from bettermemory.session import SessionState
from bettermemory.store import Store

from .test_doc_claims import _git_tracked_files, _tracked_among, _walk_files

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Every filesystem read below resolves from `_REPO_ROOT`, never from the
# process CWD, so the corpus is the same from any invocation directory.
_HANDLERS_MODULE = "src/bettermemory/_handlers.py"
_BUILDER_MODULE = "src/bettermemory/builder.py"
_DOCTOR_MODULE = "src/bettermemory/doctor.py"

# Markdown surfaces are derived from the tree, never listed. At `3ea0dfd`
# the list held two entries against forty-three tracked documents, and
# nothing anywhere noticed the other forty-one: a fabricated rate in a
# bench README or in the model-facing skill passed CI silently.
#
# Deriving them does not mean scanning them all today. Run over the whole
# tree at that same commit, the rules report 126 findings across the
# seventeen unrepaired documents below — overwhelmingly citation-shape,
# prose that states a real figure without naming the artifact it came
# from. That is a repair backlog, and a guard that lands with its whole
# finding set exempted protects nothing. So each unscanned document is
# named here with its reason, and the two guards below this constant hold
# the line in both directions: the coverage ratchet fails on a document
# that is in neither set, and the drain guard fails once an entry stops
# registering a finding, forcing it back into the corpus. The list
# drains; it cannot calcify.
_DOC_SURFACE_EXCLUSIONS: dict[str, str] = {
    "CHANGELOG.md": (
        "Frozen release record. Every published figure landed here on the "
        "day it was measured; a re-run moves the live number and would turn "
        "the whole history red, and rewriting shipped release notes to "
        "satisfy a linter is worse than the drift. tests/test_doc_claims.py "
        "tiers it out of its living-document rules for the same reason."
    ),
    "bench/toolcost/README.md": (
        "Deliberately an artifact-reference target rather than a scanned "
        "surface: `_ARTIFACT_REF` resolves a claim naming this bench "
        "against its committed results. Its own convention is the whole "
        "tools array serialized to UTF-8 bytes, while the footprint prose "
        "this module governs counts per-tool raw characters — "
        "tests/test_resident_footprint.py records that neither figure is "
        "derivable from the other, so scanning it would judge its numbers "
        "against the wrong convention."
    ),
    "bench/dedup/README.md": (
        "The dedup bench's own report. It publishes its run's thresholds "
        "and rates rather than citing them from somewhere else, so no "
        "chunk carries an artifact reference. The repair is a citation per "
        "claim, or a directory-implicit rule letting a document inside "
        "bench/X pin against bench/X's results — a rule change, not this."
    ),
    "bench/longmemeval/README.md": (
        "The longmemeval bench's own report, same shape as the dedup "
        "entry: its figures are that run's, published here rather than "
        "cited here."
    ),
    "bench/longmemeval/PREREGISTRATION.md": (
        "Pre-registration for that bench. It fixes corpus properties and a "
        "cost estimate before the run, and the corpus count it states is "
        "carried by no committed result file — a citation cannot be added "
        "until the run that would hold it is committed."
    ),
    "bench/longmemeval/CLAUDE-MEM-ADAPTER.md": (
        "The competitor-adapter note for that bench. Its figures describe "
        "a third-party corpus and an arm that has not been run, so nothing "
        "committed here can pin them; the repair is in the prose."
    ),
    "bench/retrieval/README.md": (
        "The retrieval bench's own report, same shape as the other bench "
        "reports: published rates stated without a per-chunk citation."
    ),
    "bench/rot/README.md": (
        "The rot bench's own report — a scorecard of its own run, and the "
        "largest block of uncited figures outside the changelog. Same "
        "repair as the other bench reports."
    ),
    "bench/rot/PREREGISTRATION.md": (
        "Pre-registration for the rot bench. Its percentages are stratum "
        "properties of a corpus drawn before a single detector number "
        "existed, which is exactly why no result file carries them."
    ),
    "docs/ROADMAP.md": (
        "A plan. Its figures quote past runs and set targets for work not "
        "yet done. tests/test_doc_claims.py exempts it from path claims on "
        "the same grounds: a plan describes a tree that does not exist."
    ),
    "docs/swarm-convergence-plan.md": (
        "A plan document, exempt from path claims in the doc-claims module "
        "for the same reason. It quotes a survey's figures and proposes "
        "work that was never all done."
    ),
    "docs/api.md": (
        "States schema and payload sizes throughout the reference prose. "
        "They are size-shaped, so the size rule reports them whether or "
        "not the sentence carries a cue, and none is cited. Repairing them "
        "means measuring or rewording — prose work this pass does not own."
    ),
    "docs/eval-results.md": (
        "A results write-up whose figures come from eval runs that were "
        "never committed as artifacts. Every number in it is uncitable by "
        "construction until such a run is committed."
    ),
    "docs/eval.md": (
        "Documents the eval harness and quotes its comparative run. One "
        "figure is additionally not derivable from the committed "
        "comparative JSON, so this entry covers a citation gap and a "
        "derivation gap at once."
    ),
    "docs/eval/widening-labeling-2026-07-29.md": (
        "A dated labeling analysis. Its rates were computed in the session "
        "that wrote it and the run behind them is not a committed "
        "artifact. Its two older siblings scan clean and are in the "
        "corpus, so the exclusion is about this document, not the series."
    ),
    "docs/incidents/2026-07-26-staleness-verdict-constant-function.md": (
        "A postmortem quoting the world before the fix. The figures it "
        "reports are the broken behaviour's, which the current rot results "
        "deliberately no longer contain — closer in kind to a changelog "
        "entry than to a living document."
    ),
    "docs/installation.md": (
        "Carries the plugin-skill size figure, uncited, in the same words "
        "as the system prompt and the skill itself. One measurement "
        "repairs all three; the sentence is not owned by this pass."
    ),
    "docs/system_prompt.md": (
        "The same uncited plugin-skill size figure as docs/installation.md."
    ),
    "plugin/skills/bettermemory/SKILL.md": (
        "The model-facing skill states its own size, uncited, in the same "
        "words as docs/installation.md. It is the most load-bearing of the "
        "three copies and the right one to repair first."
    ),
}

# The two exclusions that are decisions about *conventions* rather than a
# queue of unrepaired prose. Repairing a document cannot promote these, so
# the drain guard leaves them alone.
_CATEGORICAL_EXCLUSIONS = frozenset({"CHANGELOG.md", "bench/toolcost/README.md"})


@lru_cache(maxsize=None)
def _doc_surfaces() -> tuple[str, ...]:
    """Tracked markdown this module scans, minus the named exclusions.

    Shares tests/test_doc_claims.py's tracked-files helper rather than
    shelling out to git a second time: the two corpora drifted apart once
    already, and one routine is one place to keep honest.
    """
    tracked = _git_tracked_files("*.md")
    rels = _walk_files(".md") if tracked is None else tracked
    return tuple(rel for rel in rels if rel not in _DOC_SURFACE_EXCLUSIONS)


# Source labels are stable strings so allowlist keys survive prose edits.
_INSTRUCTIONS_SOURCE = "server.instructions"
_DESC_SOURCE_PREFIX = "desc:"


# --------------------------------------------------------------------------
# Claim shapes
# --------------------------------------------------------------------------
# The measurement cue. "measures" is in the family on purpose: the README
# says `bettermemory eval` measures whether memory helped, and if that
# sentence ever grows a rate, the rate is a measurement.
_MEASURE_CUE = re.compile(
    r"\b(?:measured|measures|measuring|measurement|measurements"
    r"|re-measure|re-measured|remeasure|benchmarked)\b",
    re.I,
)

# Provenance tokens, masked before number extraction: an ISO date and a
# dotted version are not measurements.
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_VERSION = re.compile(r"\b\d+\.\d+\.\d+[\w.-]*")
# Non-prose regions of a markdown document.
_URL = re.compile(r"\bhttps?://\S+")
# A markup tag, not any angle-bracket pair: the character after `<` must
# start a tag name or a closing slash. Without that bound, a description
# reading "<30% overlap … >500 words" on one line would be masked from the
# first bracket to the last, hiding both numbers.
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>\n]{0,400}>")
_FENCE = re.compile(r"^```.*?^```", re.M | re.S)

_UNITS = r"%|x|B|KB|MB|GB|bytes?|chars?|characters?"
_NUMBER = re.compile(
    # `(?<![\w@$.,])` keeps the pattern off the tail of an identifier, off a
    # metric index (`recall@1` names a metric, it does not measure one), off
    # a currency amount, and off the second half of an already-masked token.
    r"(?<![\w@$.,])(?:~|approx\.?\s*)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
    rf"\s*(?P<unit>{_UNITS})?(?![\w%])"
)

_SIZE_UNITS = frozenset({"x", "B", "KB", "MB", "GB", "byte", "bytes"})

# A byte figure governed by one of these words is a contract constant, not
# a measurement: caps are enforced by adjacent code, so no bench result can
# pin them and no rewrite can make them honest. The size rule is cue-free by
# design, which would otherwise make an accurate sentence like "takeaways
# are capped at 4 KB; frontmatter has a 64 KB ceiling" a finding whose only
# cheap repair is deleting the sentence — the path by which a checker with
# false positives gets switched off.
#
# Adjacency is the whole design. A chunk-level test looks equivalent and is
# not: the pre-truth-sync README bullet read "cost ~35 KB of schema per turn
# … the description half of that is capped in CI", where `capped` governs a
# different quantity one clause away. Exempting on mere presence would have
# blinded this guard to the exact claim it was built to catch, which is why
# `test_guard_would_have_flagged_the_pre_truth_sync_readme_bullet` pins it.
_CAP_CUE = re.compile(
    r"\b(?:cap|caps|capped|ceiling|limit|limits|limited|maximum|max"
    r"|at most|no larger than|refuses? above)\b",
    re.I,
)
# How far from the number a cap word may sit and still govern it. One short
# clause: "capped at 4 KB", "a 64 KB ceiling". Clause boundaries end the
# window regardless, so a semicolon cuts the reach.
_CAP_REACH = 24
_CLAUSE_BREAK = re.compile(r"[;.—]|\s-\s")


def _is_contract_constant(chunk: str, number: Number) -> bool:
    """Whether a cap word governs this number, not merely shares its chunk."""
    before = chunk[max(0, number.start - _CAP_REACH) : number.start]
    after = chunk[number.end : number.end + _CAP_REACH]
    # Keep the window inside the number's own clause.
    breaks = list(_CLAUSE_BREAK.finditer(before))
    if breaks:
        before = before[breaks[-1].end() :]
    tail = _CLAUSE_BREAK.search(after)
    if tail:
        after = after[: tail.start()]
    return bool(_CAP_CUE.search(before) or _CAP_CUE.search(after))


# A repo-relative reference to a committed artifact, or to the bench that
# owns one. `bench/toolcost/README.md` and the bare `bench/toolcost` of a
# markdown reference-link both resolve to that bench's result files.
_ARTIFACT_REF = re.compile(r"\b(?:bench|docs)/[\w][\w./-]*")


@dataclass(frozen=True)
class Claim:
    """One extracted number presented as a measurement."""

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
# Known-unpinnable claims. EMPTY AT HEAD, and the module docstring explains
# why that is the point. An entry needs a reason of substance; the reverse
# guard deletes any entry that stops matching a real finding.
# --------------------------------------------------------------------------
_ALLOWLIST: dict[tuple[str, str, str], str] = {}


# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------
def _git_tracked(pattern: str) -> tuple[str, ...] | None:
    """Tracked paths matching ``pattern``, or None when git is unavailable.

    Tracked-file discovery matters here: an artifact has to be *committed*
    to back a published claim, and a local bench run leaves untracked JSON
    in the same directories. Degrades to a filesystem glob so the module
    still works in an export without a git dir.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--", pattern],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - env
        return None
    if out.returncode != 0:  # pragma: no cover - env
        return None
    return tuple(sorted(p for p in out.stdout.split("\0") if p))


@lru_cache(maxsize=1)
def _artifacts() -> tuple[str, ...]:
    """Committed bench results, repo-relative."""
    patterns = ("bench/*/results/*.json", "docs/eval/*.json")
    found: list[str] = []
    for pattern in patterns:
        tracked = _git_tracked(pattern)
        if tracked is None:  # pragma: no cover - env
            tracked = tuple(
                sorted(
                    p.relative_to(_REPO_ROOT).as_posix()
                    for p in _REPO_ROOT.glob(pattern)
                )
            )
        found.extend(tracked)
    return tuple(sorted(found))


def _walk_json(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            out.extend(_walk_json(value, str(key)))
    elif isinstance(node, list):
        for value in node:
            out.extend(_walk_json(value, prefix))
    else:
        out.append((prefix, node))
    return out


@lru_cache(maxsize=64)
def _artifact_values(rel: str) -> tuple[tuple[str, float], ...]:
    """``(leaf_key, numeric_value)`` for every number in one artifact.

    Booleans are excluded: `True` is not the number one here, and letting it
    through would pin a claim of "1" against any flag in the file.
    """
    raw = json.loads((_REPO_ROOT / rel).read_text(encoding="utf-8"))
    out: list[tuple[str, float]] = []
    for key, value in _walk_json(raw):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out.append((key, float(value)))
    return tuple(out)


def _metric_spellings(key: str) -> tuple[str, ...]:
    """How a document might name an artifact key.

    Only whole-key spellings, so `bytes` in prose does not match a key
    called `full_bytes`. The ``@`` form is the one that matters in practice:
    a result file records `recall_at_1`, and prose writes recall@1.
    """
    spellings = {key}
    if "_at_" in key:
        spellings.add(key.replace("_at_", "@"))
    return tuple(sorted(spellings))


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------
def _mask(text: str, pattern: re.Pattern[str]) -> str:
    """Blank a region out, preserving every offset and newline."""

    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in match.group(0))

    return pattern.sub(blank, text)


def _masked_prose(text: str) -> str:
    # Tags before URLs: a URL run is `\S+`, so masking the href first eats
    # the tag's own closing bracket and leaves the rest of the tag exposed.
    for pattern in (_FENCE, _HTML_TAG, _URL, _DATE, _VERSION):
        text = _mask(text, pattern)
    return text


_BULLET = re.compile(r"^[ \t]*(?:[-*+]|\d{1,2}[.)])\s")


def _chunks(text: str) -> list[tuple[int, str]]:
    """``(offset, chunk)`` — blank-line blocks, split again at list items.

    Splitting list items apart is what keeps a citation local. A markdown
    bullet list is one blank-line block, so without this step a bullet
    carrying an artifact reference would vouch for numbers in every sibling
    bullet.
    """
    out: list[tuple[int, str]] = []
    for offset, block in _blocks(text):
        lines = block.split("\n")
        if not any(_BULLET.match(line) for line in lines):
            out.append((offset, block))
            continue
        start = 0
        cursor = offset
        for index, line in enumerate(lines):
            if index and _BULLET.match(line):
                piece = "\n".join(lines[start:index])
                out.append((cursor, piece))
                cursor += len(piece) + 1
                start = index
        out.append((cursor, "\n".join(lines[start:])))
    return out


def _blocks(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    pos = 0
    for match in re.finditer(r"\n[ \t]*\n", text):
        out.append((pos, text[pos : match.start()]))
        pos = match.end()
    out.append((pos, text[pos:]))
    return out


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# --------------------------------------------------------------------------
# Number extraction and pinning
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Number:
    raw: str
    value: float
    unit: str
    decimals: int
    start: int = 0
    end: int = 0


def numbers_in(chunk: str) -> list[Number]:
    """Measurement-shaped numbers in an already-masked chunk.

    A number qualifies on shape alone: it carries a unit, or it is
    comma-grouped, or it is a bare fraction below one. A bare integer does
    not — see the not-checked list.
    """
    out: list[Number] = []
    for match in _NUMBER.finditer(chunk):
        digits = match.group("num")
        unit = match.group("unit") or ""
        plain = digits.replace(",", "")
        qualifies = bool(unit) or "," in digits or plain.startswith("0.")
        if not qualifies:
            continue
        decimals = len(plain.split(".")[1]) if "." in plain else 0
        # `raw` is normalised rather than sliced from the text: an
        # approximation marker and the spacing before a unit are not part of
        # the claim, and the allowlist keys on this string.
        if not unit:
            raw = digits
        elif unit in ("%", "x"):
            raw = f"{digits}{unit}"
        else:
            raw = f"{digits} {unit}"
        out.append(
            Number(
                raw=raw,
                value=float(plain),
                unit=unit,
                decimals=decimals,
                start=match.start(),
                end=match.end(),
            )
        )
    return out


def _candidates(number: Number) -> list[tuple[float, float]]:
    """``(value, tolerance)`` pairs an artifact could legitimately hold.

    The tolerance is one unit in the token's last written place — exactly
    the freedom a writer has when quoting a measurement at reduced
    precision, so a ratio recorded as 4.845 may be published as 4.84.

    It is scaled with the value, and that is the load-bearing part. A
    percentage written to one decimal is a thousandth as a fraction, so a
    fixed tolerance would let a fabricated rate of 88.8% pin against a
    measured 0.9. The conversions are: a percentage may be stored as either
    the percent or the fraction, and a kilo-or-larger unit may be stored in
    bytes under either the binary or the decimal convention.
    """
    step = 10.0**-number.decimals
    out = [(number.value, step)]
    if number.unit == "%":
        out.append((number.value / 100.0, step / 100.0))
    scale = {"KB": 1, "MB": 2, "GB": 3}.get(number.unit)
    if scale is not None:
        for base in (1024, 1000):
            out.append((number.value * base**scale, step * base**scale))
    return out


def _matches(number: Number, pool: list[tuple[str, float]]) -> bool:
    """True when some pooled value rounds or truncates to this token."""
    return any(
        abs(value - candidate) < tolerance
        for _key, value in pool
        for candidate, tolerance in _candidates(number)
    )


def cited_artifacts(chunk: str) -> tuple[list[str], list[str]]:
    """``(resolved, dangling)`` artifact references found in a chunk."""
    resolved: list[str] = []
    dangling: list[str] = []
    known = _artifacts()
    for match in _ARTIFACT_REF.finditer(chunk):
        ref = match.group(0).rstrip(".,;:)")
        if ref.endswith(".json"):
            if ref in known:
                resolved.append(ref)
            else:
                dangling.append(ref)
            continue
        parts = ref.split("/")
        if len(parts) < 2:
            continue
        bench = "/".join(parts[:2])
        owned = [a for a in known if a.startswith(bench + "/")]
        if owned:
            resolved.extend(owned)
    return sorted(set(resolved)), sorted(set(dangling))


def _pool_for(chunk: str, artifacts: list[str]) -> list[tuple[str, float]]:
    """Values a chunk's numbers may pin against, narrowed by named metric."""
    pool: list[tuple[str, float]] = []
    for rel in artifacts:
        pool.extend(_artifact_values(rel))
    named = {
        key
        for key, _value in pool
        for spelling in _metric_spellings(key)
        if re.search(rf"(?<![\w@]){re.escape(spelling)}(?![\w])", chunk)
    }
    if not named:
        return pool
    return [(key, value) for key, value in pool if key in named]


def claimed_numbers(chunk: str, *, size_rule: bool) -> list[Number]:
    """Which of a chunk's numbers this module treats as measurement claims.

    Split out from the pinning step so the coverage floor below can assert
    that the surfaces known to carry claims still register as carrying them.
    A guard whose extractor quietly stops matching reports no findings and
    looks exactly like a guard with nothing to find.
    """
    numbers = numbers_in(chunk)
    if not numbers:
        return []
    if _MEASURE_CUE.search(chunk):
        return numbers
    if size_rule:
        return [
            n
            for n in numbers
            if n.unit in _SIZE_UNITS and not _is_contract_constant(chunk, n)
        ]
    return []


def check_chunk(
    source: str,
    chunk: str,
    line: int,
    *,
    size_rule: bool,
    raw_chunk: str | None = None,
) -> list[Failure]:
    """Run both rules over one chunk.

    ``chunk`` is masked, which is what number extraction needs. Citations
    are read from ``raw_chunk`` instead, because masking an ISO date would
    cut a dated artifact filename in half and turn an exact citation into a
    whole-directory one.
    """
    citation_text = chunk if raw_chunk is None else raw_chunk
    claimed = claimed_numbers(chunk, size_rule=size_rule)
    if not claimed:
        return []

    resolved, dangling = cited_artifacts(citation_text)
    out: list[Failure] = []
    for ref in dangling:
        out.append(
            Failure(
                Claim(source, line, "missing-artifact", ref),
                "cited as the artifact behind a measurement, but no such "
                "committed bench result exists",
            )
        )
    if not resolved:
        for number in claimed:
            out.append(
                Failure(
                    Claim(source, line, "uncited", number.raw),
                    "presented as a measurement but the claim cites no "
                    "committed artifact. Cite a bench result file (or the "
                    "bench that owns one), or rewrite the claim so it no "
                    "longer asserts a measured number",
                )
            )
        return out
    pool = _pool_for(chunk, resolved)
    for number in claimed:
        if _matches(number, pool):
            continue
        out.append(
            Failure(
                Claim(source, line, "unpinned", number.raw),
                f"not derivable from {', '.join(resolved)} "
                f"(pool of {len(pool)} value(s))",
            )
        )
    return out


def check_text(source: str, text: str, *, size_rule: bool = False) -> list[Failure]:
    """Extract and pin every measurement claim in one surface's text."""
    masked = _masked_prose(text)
    # Masking replaces characters one-for-one, so an offset into `masked` is
    # the same offset into `text` and the two chunk streams stay aligned.
    assert len(masked) == len(text)
    out: list[Failure] = []
    for offset, chunk in _chunks(masked):
        out.extend(
            check_chunk(
                source,
                chunk,
                _line_of(masked, offset),
                size_rule=size_rule,
                raw_chunk=text[offset : offset + len(chunk)],
            )
        )
    return out


# --------------------------------------------------------------------------
# Surfaces
# --------------------------------------------------------------------------
@lru_cache(maxsize=1)
def desc_constants() -> tuple[tuple[str, str], ...]:
    """``(name, value)`` for every DESC constant the server registers."""
    import bettermemory._handlers as handlers

    return tuple(
        (name, getattr(handlers, name))
        for name in sorted(dir(handlers))
        if name.startswith("DESC_")
    )


@lru_cache(maxsize=1)
def instructions_text() -> str:
    """The server `instructions` block, read from the builder by AST.

    Read statically rather than by building a server so the corpus needs no
    fixture and no store. The reading is verified against a live server
    below, which is what keeps a silent divergence from turning this surface
    into an empty sweep.

    Matched on the `instructions=` KEYWORD, not on the server class's name.
    It used to require a call to `FastMCP`, which meant the mcp 2.x port —
    a rename of that class and nothing else about this block — tripped the
    assertion below for no reason a reader of the failure could act on. The
    keyword is the thing this corpus is actually about, it is unique in
    `_BUILDER_MODULE`, and it does not move when the SDK reorganises. The
    loud failure is the property worth keeping and it is unchanged: sweeping
    nothing still raises rather than passing an empty corpus.
    """
    tree = ast.parse((_REPO_ROOT / _BUILDER_MODULE).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "instructions":
                continue
            value = ast.literal_eval(keyword.value)
            assert isinstance(value, str)
            return value
    raise AssertionError(
        f"no `instructions=...` string literal found at any call site in "
        f"{_BUILDER_MODULE}; the instructions surface would silently sweep "
        f"nothing. If the block moved to a module constant, read it from there "
        f"instead"
    )


def _python_prose(rel: str) -> list[tuple[int, str]]:
    """``(line, text)`` for docstrings, string literals and comment blocks.

    Runs of full-line comments coalesce into one chunk. Without that, a cue
    on one line and its numbers on the next would land in separate chunks
    and neither would be a claim — which is how a threshold comment nearly
    escaped this guard while being read as its provenance record.
    """
    src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
    out: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))

    lines = src.split("\n")
    run: list[str] = []
    run_start = 0
    for token in tokenize.generate_tokens(io.StringIO(src).readline):
        if token.type != tokenize.COMMENT:
            continue
        row = token.start[0]
        full_line = not lines[row - 1][: token.start[1]].strip()
        if not full_line:
            out.append((row, token.string))
            continue
        if run and row == run_start + len(run):
            run.append(token.string)
            continue
        if run:
            out.append((run_start, "\n".join(run)))
        run, run_start = [token.string], row
    if run:
        out.append((run_start, "\n".join(run)))
    return sorted(out)


def collect_failures() -> list[Failure]:
    """Run both rules over every surface."""
    out: list[Failure] = []
    for name, value in desc_constants():
        out.extend(check_text(f"{_DESC_SOURCE_PREFIX}{name}", value))
    out.extend(check_text(_INSTRUCTIONS_SOURCE, instructions_text()))
    for rel in _doc_surfaces():
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        out.extend(check_text(rel, text, size_rule=True))
    for line, text in _python_prose(_DOCTOR_MODULE):
        for failure in check_text(_DOCTOR_MODULE, text):
            out.append(
                Failure(
                    Claim(
                        failure.claim.source,
                        line + failure.claim.line - 1,
                        failure.claim.kind,
                        failure.claim.subject,
                    ),
                    failure.detail,
                )
            )
    return out


# --------------------------------------------------------------------------
# The paired ratchet tests
# --------------------------------------------------------------------------
def test_no_unpinned_measurement_claims() -> None:
    """Forward guard: no number on a resident surface may be unbacked.

    If this fails, the surface is making a claim this repo cannot support.
    The fix is one of three, in order of preference: cite the artifact that
    already measures it, rewrite the sentence so it stops asserting a
    measured number, or run the bench and commit the result. An
    ``_ALLOWLIST`` entry is the last resort and it is currently empty on
    purpose.
    """
    unexpected = [f for f in collect_failures() if f.claim.key not in _ALLOWLIST]
    if unexpected:
        rendered = "\n".join(f"  - {f}" for f in unexpected)
        pytest.fail(
            f"{len(unexpected)} unbacked number(s) on resident surfaces:\n"
            f"{rendered}\n\nEach is presented to a model or a reader as a "
            f"measurement. Fix the prose or cite the artifact rather than "
            f"loosening the checker, unless the extraction itself misfired."
        )


def test_allowlist_has_no_stale_entries() -> None:
    """Reverse guard: the allowlist may not outlive the findings it covers."""
    live = {f.claim.key for f in collect_failures()}
    stale = stale_entries(_ALLOWLIST, live)
    if stale:
        pytest.fail(_stale_report(_ALLOWLIST, stale))


def stale_entries(
    allowlist: dict[tuple[str, str, str], str],
    live: set[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    return sorted(key for key in allowlist if key not in live)


def _stale_report(
    allowlist: dict[tuple[str, str, str], str],
    stale: list[tuple[str, str, str]],
) -> str:
    rendered = "\n".join(
        f"  - {key} (exempt because: {allowlist[key]})" for key in stale
    )
    return (
        f"{len(stale)} _ALLOWLIST entr(y/ies) no longer correspond to a real "
        f"finding:\n{rendered}\n\nTwo different things cause this and they "
        f"need opposite responses:\n"
        f"  (1) the claim was repaired — the number now pins, or the "
        f"sentence no longer asserts a measurement. Delete the entry; that "
        f"is the ratchet working.\n"
        f"  (2) the extractor stopped matching a claim that is still "
        f"unbacked — the prose was reworded past the cue, the number was "
        f"rewritten into a shape this module does not recognise, or a rule "
        f"narrowed. Deleting the entry then hides a live false claim.\n"
        f"Read the claim on the surface before deleting. Recording (1) when "
        f"it was really (2) is a documented failure of the sibling guard in "
        f"tests/test_doc_claims.py, which is why this message spells both out."
    )


def test_allowlist_entries_carry_a_reason() -> None:
    """Every exemption must say why, so review can judge it."""
    for key, reason in _ALLOWLIST.items():
        assert len(reason.strip()) >= 40, f"{key} needs a substantive reason"


def test_every_markdown_on_disk_is_a_number_surface_excluded_or_untracked() -> None:
    """The coverage ratchet: two enumerations of this repo's markdown agree.

    This is the guard the module went without. The surface list was two
    entries long, so a fabricated rate in any other document — a bench
    report, the model-facing skill, an incident postmortem — was checked
    by nothing and shipped green. A document added tomorrow under a new
    directory is that same defect arriving again.

    The population is a filesystem walk, not the ``git ls-files`` listing
    ``_doc_surfaces`` is derived from, and the reasoning is the sibling
    ratchet's in tests/test_doc_claims.py verbatim: subtracting a
    derivation from its own input computes the empty set whatever the
    derivation does, so the assertion would pass over a corpus narrowed
    to one document. A walk asks a different question and therefore has
    an answer that can be wrong. Untracked and gitignored markdown — the
    vendored competitor package under ``bench/``, a postmortem not yet
    staged — is what a walk sees and git does not; it is subtracted by
    handing git the walk's own paths, never by re-reading the glob.
    """
    walked = _walk_files(".md")
    tracked = _tracked_among(walked)
    if tracked is None:  # pragma: no cover - only outside a git checkout
        pytest.skip("not a git checkout")
    surfaces = set(_doc_surfaces())
    assert surfaces, "the markdown corpus is empty — file discovery broke"
    undecided = (set(walked) & tracked) - surfaces - set(_DOC_SURFACE_EXCLUSIONS)
    assert undecided == set(), (
        f"{len(undecided)} tracked markdown file(s) are neither scanned for "
        f"measurement claims nor excluded: {sorted(undecided)}\nA file is in "
        f"the corpus by default; it only lands here if it is in "
        f"_DOC_SURFACE_EXCLUSIONS, so this failing means the derivation "
        f"broke rather than that a decision is missing."
    )
    unwalked = surfaces - set(walked)
    assert unwalked == set(), (
        f"{len(unwalked)} scanned document(s) are invisible to the filesystem "
        f"walk: {sorted(unwalked)}\nThe walk's prune list now covers tracked "
        f"prose, so this ratchet's population no longer covers the corpus."
    )


def test_excluded_surface_entries_are_tracked_and_carry_a_reason() -> None:
    """An exclusion must name a real document and say why it is out."""
    tracked = _git_tracked_files("*.md")
    if tracked is None:  # pragma: no cover - only outside a git checkout
        pytest.skip("not a git checkout")
    unknown = set(_DOC_SURFACE_EXCLUSIONS) - set(tracked)
    assert unknown == set(), (
        f"_DOC_SURFACE_EXCLUSIONS names {sorted(unknown)}, which the repo "
        f"does not track. A renamed or deleted document leaves an entry "
        f"that excludes nothing while reading as a standing decision."
    )
    assert _CATEGORICAL_EXCLUSIONS <= set(_DOC_SURFACE_EXCLUSIONS)
    for rel, reason in _DOC_SURFACE_EXCLUSIONS.items():
        assert len(reason.strip()) >= 40, f"{rel} needs a substantive reason"


def test_excluded_surfaces_still_have_findings_to_repair() -> None:
    """The drain guard: an exclusion may not outlive the backlog it names.

    Each entry outside ``_CATEGORICAL_EXCLUSIONS`` is a queue of prose
    repairs, not a standing decision. When the last finding in a document
    is repaired, the entry stops describing anything and the document
    belongs back in the corpus — otherwise this list is a suppression that
    happens to look like a plan, and the next unbacked number added to a
    drained document would be invisible again.

    The categorical two are exempt because repairing their prose cannot
    make them scannable: one is frozen history, the other counts bytes by
    a different convention.
    """
    drained = []
    for rel in sorted(set(_DOC_SURFACE_EXCLUSIONS) - _CATEGORICAL_EXCLUSIONS):
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        if not check_text(rel, text, size_rule=True):
            drained.append(rel)
    assert drained == [], (
        f"{len(drained)} excluded document(s) now register no finding: "
        f"{drained}\nThe repair landed — delete each entry from "
        f"_DOC_SURFACE_EXCLUSIONS so the document is scanned from now on. "
        f"That promotion is the whole point of the list; leaving the entry "
        f"in place turns a finished repair into a permanent blind spot."
    )


# --------------------------------------------------------------------------
# Structural guards. A sweep that silently stops finding its own surface is
# indistinguishable from a green build, so each surface is proved present.
# --------------------------------------------------------------------------
def test_description_surface_is_the_whole_registered_set() -> None:
    """The DESC sweep must see every description, not a stale subset."""
    names = [name for name, _ in desc_constants()]
    assert len(names) == len(set(names))
    # 27 tool descriptions plus the shared links fragment. Moves only when a
    # tool is added or removed, which the tool-count guards catch first.
    assert len(names) == 28, (
        f"{_HANDLERS_MODULE} now re-exports {len(names)} DESC constants, not "
        f"28. If a tool was added or removed this is expected — update the "
        f"count. If the re-export list was pruned, the sweep just lost a "
        f"resident surface."
    )
    for name, value in desc_constants():
        assert isinstance(value, str) and value.strip(), name


def test_instructions_surface_matches_the_served_block(memory_dir: Path) -> None:
    """The static read must equal what a real server advertises."""
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=Store(memory_dir),
        state=SessionState(),
    )
    assert (server.instructions or "") == instructions_text()
    assert len(instructions_text()) > 800


def test_doc_surfaces_are_present_and_substantial() -> None:
    """The corpus must be non-empty and must still hold both cost documents.

    A derived corpus can empty itself — a bad pattern, a missing checkout —
    and every rule below would then pass on nothing. The two documents
    named here are the ones that state this project's footprint, so they
    are asserted by name and by size: they are what the `size` rule was
    built for, and a rename that dropped either would otherwise be silent.
    """
    surfaces = _doc_surfaces()
    assert len(surfaces) > 10, f"markdown corpus collapsed to {surfaces}"
    for rel in ("README.md", "docs/internals.md"):
        assert rel in surfaces, f"{rel} left the number-claim corpus"
        assert len((_REPO_ROOT / rel).read_text(encoding="utf-8")) > 2000, rel
    for rel in surfaces:
        assert (_REPO_ROOT / rel).is_file(), rel


def test_doctor_prose_surface_is_populated() -> None:
    """A parse regression here would empty the strictest surface silently."""
    prose = _python_prose(_DOCTOR_MODULE)
    assert len(prose) > 100
    joined = "\n".join(text for _line, text in prose)
    # Both halves must be present: the operator-facing strings and the
    # comment blocks that record where a threshold came from.
    assert "retrieval_discrimination" in joined
    assert "#: Topical-query recall@1" in joined


def surface_claim_counts() -> dict[str, int]:
    """How many measurement claims each surface currently registers."""
    counts: dict[str, int] = {}

    def add(source: str, text: str, *, size_rule: bool = False) -> None:
        masked = _masked_prose(text)
        total = sum(
            len(claimed_numbers(chunk, size_rule=size_rule))
            for _offset, chunk in _chunks(masked)
        )
        counts[source] = counts.get(source, 0) + total

    for name, value in desc_constants():
        add(f"{_DESC_SOURCE_PREFIX}{name}", value)
    add(_INSTRUCTIONS_SOURCE, instructions_text())
    for rel in _doc_surfaces():
        add(rel, (_REPO_ROOT / rel).read_text(encoding="utf-8"), size_rule=True)
    for _line, text in _python_prose(_DOCTOR_MODULE):
        add(_DOCTOR_MODULE, text)
    return counts


# Per-surface claim counts measured at the truth-sync pass. A count floor
# rather than a token pin: re-running a bench and re-publishing its numbers
# is sanctioned, so pinning tokens would churn. But `>= 1` was too weak to
# be a floor at all — the README bullet states four figures, and dropping
# the cue word from it left three of them silently un-governed while the
# fourth kept the surface above a floor of one.
#
# Lowered for the footprint phase's toolcost re-publish. Both doc surfaces
# dropped the same two figures — the `4.84x` head-to-head ratio and
# claude-mem's own byte total — because only bettermemory's arm was re-run.
# Restating a ratio whose numerator moved and whose denominator is a
# 2026-07-26 probe of someone else's package would have been derivable from
# the committed pair and still false, which is the one failure mode this
# module cannot see: it checks that numbers trace to an artifact, never that
# the artifact still describes the thing the sentence is about. So the
# ratio left the prose rather than being recomputed, and these floors follow
# the claims down. README additionally folded its input-schema figure into
# the internals paragraph, which is why the two surfaces now differ.
_CLAIM_FLOORS = {
    "docs/internals.md": 3,
}


def test_coverage_floor_the_known_claim_bearing_surfaces_still_match() -> None:
    """Each surface known to state measurements must still register them.

    Deliberately a floor and not an inventory: re-running a bench and
    re-publishing its numbers is a sanctioned outcome, so pinning the exact
    tokens would generate churn the forward guard already covers. What
    cannot be allowed to change silently is a surface dropping out of the
    extractor's reach — that turns a green run into no run.

    Recorded at the time of writing, for a reader chasing a change here:
    the internals paragraph states the serialized tool-surface cost in
    four figures. (The doctor fix hint dropped out with the 4.0.0 purist
    strip; README.md dropped out at 5.0.0 by owner directive — the README
    carries NO measurements at all now, because dated numbers in a
    front page rot faster than anyone re-measures them. Evidence lives in
    bench/ and docs, where it sits beside its caveats and dates.) The
    descriptions and the instructions block state no measurement at all,
    by design — a rate is only honest beside its caveat and an
    always-resident string should not spend characters on one.
    """
    counts = surface_claim_counts()
    for source, floor in _CLAIM_FLOORS.items():
        assert counts.get(source, 0) >= floor, (
            f"{source} registers {counts.get(source, 0)} measurement claims, "
            f"below the recorded floor of {floor}. Either claims were removed "
            f"(fine — update this floor) or the extractor stopped reaching "
            f"them (not fine — the guard is now blind there)."
        )


def test_readme_carries_no_measurement_claims() -> None:
    """The front page carries ZERO measurements — a 5.0.0 owner decision.

    Dated numbers on a front page rot faster than anyone re-measures
    them: the pre-5.0 README quoted a tool-surface byte count measured
    three minor versions earlier as if current, on a project whose
    thesis is that stale claims get flagged. Evidence lives in bench/
    and the docs, beside its dates and caveats; the README states what
    the product does in timeless terms. A measurement claim appearing
    in README.md is a regression, not an addition — move it to the doc
    that can carry its date.
    """
    counts = surface_claim_counts()
    assert counts.get("README.md", 0) == 0, (
        f"README.md now states {counts['README.md']} measurement "
        "claim(s). The front page carries no numbers by design — put "
        "the figure in bench/ or docs/ next to its date and artifact, "
        "and link it."
    )


def test_resident_descriptions_state_no_measurement() -> None:
    """The always-resident surfaces carry no rate, and that is the design.

    Asserted rather than assumed, because it is the premise of the
    negative self-test: the injection below is the only measurement claim
    in the description corpus, so the failure it produces cannot be
    confused with a pre-existing one.
    """
    counts = surface_claim_counts()
    offenders = {
        source: n
        for source, n in counts.items()
        if n
        and (source.startswith(_DESC_SOURCE_PREFIX) or source == _INSTRUCTIONS_SOURCE)
    }
    assert not offenders, (
        f"a resident surface now states a measurement: {offenders}. That is "
        f"allowed if it cites an artifact — the forward guard decides — but "
        f"it costs per-turn characters on every client, and a rate without "
        f"its caveat reads as a promise. Prefer the module docstring."
    )


def test_artifact_inventory_is_committed_and_parses() -> None:
    """The pinnable set must be non-empty, tracked, and readable.

    Values are not pinned here on purpose: re-running a bench is a
    sanctioned outcome, and the coupling that matters is enforced by the
    forward guard — a re-run that moves a number fails every surface still
    quoting the old one. Recorded at the time of writing, for a reader:
    the toolcost result holds 38009 serialized bytes of which 28604 are
    names and descriptions and 7096 input schemas over 18 tools, and the
    canonical retrieval result holds lexical recall@1 of 0.35 as asked and
    0.80 re-queried against 0.60 and 0.90 for the semantic arm.
    """
    artifacts = _artifacts()
    assert artifacts, "no committed bench results found; the pool is empty"
    for rel in artifacts:
        assert (_REPO_ROOT / rel).exists(), rel
        assert _artifact_values(rel), f"{rel} holds no numbers"


def test_canonical_artifacts_are_still_where_the_surfaces_point() -> None:
    """Named because live prose cites them; a rename must fail loudly."""
    for rel in (
        "bench/toolcost/results/bettermemory-vs-claude-mem-2026-07-26.json",
        "bench/retrieval/results/v2-unpadded-2026-07-26.json",
    ):
        assert rel in _artifacts(), rel


# --------------------------------------------------------------------------
# Self-tests. A rule whose findings are all currently zero is
# indistinguishable from a rule that does nothing, so these are
# load-bearing. Every fixture below is a code constant or a comment: a
# docstring in this file enters the doc-claims corpus, where a synthetic
# example reads as an assertion.
# --------------------------------------------------------------------------

# A fabricated rate. It appears in no committed artifact, and it is written
# with the artifact citation present so that the failure proves the pinning
# step fires rather than the citation step.
_FABRICATED_DESC_SENTENCE = (
    " Query wording: measured 88.8% recall@1 "
    "(bench/retrieval/results/v2-unpadded-2026-07-26.json)."
)


def test_negative_self_test_fabricated_number_in_a_real_description() -> None:
    name, real = next((n, v) for n, v in desc_constants() if n == "DESC_MEMORY_SEARCH")
    source = f"{_DESC_SOURCE_PREFIX}{name}"
    # Positive control first: the shipped description is clean, so the
    # failure below is caused by the injection and not by the surface.
    assert check_text(source, real) == []
    failures = check_text(source, real + _FABRICATED_DESC_SENTENCE)
    assert [(f.claim.kind, f.claim.subject) for f in failures] == [
        ("unpinned", "88.8%")
    ]


def test_negative_self_test_reaches_the_real_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injected claim must fail the actual forward guard, not a copy.

    `collect_failures` resolves the description surface through the module
    global, so swapping that global exercises the real collector, the real
    allowlist filter and the real failure rendering — everything the CI
    guard runs, with one surface poisoned.
    """
    patched = tuple(
        (n, (v + _FABRICATED_DESC_SENTENCE) if n == "DESC_MEMORY_SEARCH" else v)
        for n, v in desc_constants()
    )
    monkeypatch.setitem(globals(), "desc_constants", lambda: patched)
    unexpected = [f for f in collect_failures() if f.claim.key not in _ALLOWLIST]
    assert [f.claim.key for f in unexpected] == [
        ("desc:DESC_MEMORY_SEARCH", "unpinned", "88.8%")
    ]
    with pytest.raises(Failed):
        test_no_unpinned_measurement_claims()


def test_a_changed_digit_on_a_passing_surface_fails() -> None:
    """The surfaces that pass today must be the reason, not luck.

    The tampered literal has to be one docs/internals.md currently
    carries, which makes it a lockstep dependency of every re-publish:
    when the footprint phase retired the 3.29.0 figure this test had
    pinned since it was written, `tampered == real` and the assert below
    was the only thing between that and a self-test quietly grading a
    no-op edit. Hence the explicit inequality — it fires on a stale
    literal before the interesting assertion can pass vacuously.
    """
    real = (_REPO_ROOT / "docs/internals.md").read_text(encoding="utf-8")
    assert check_text("docs/internals.md", real, size_rule=True) == []
    tampered = real.replace("33,960 bytes", "33,999 bytes")
    assert tampered != real, (
        "docs/internals.md no longer carries the literal this self-test "
        "tampers with; re-point it at a byte figure the file currently "
        "claims, or the tamper is a no-op and proves nothing."
    )
    failures = check_text("docs/internals.md", tampered, size_rule=True)
    assert [(f.claim.kind, f.claim.subject) for f in failures] == [
        ("unpinned", "33,999 bytes")
    ]


def test_a_dangling_artifact_citation_fails() -> None:
    text = (
        "The surface measured 38,009 bytes (bench/toolcost/results/no-such-run.json)."
    )
    failures = check_text("docs/internals.md", text, size_rule=True)
    kinds = {f.claim.kind for f in failures}
    assert "missing-artifact" in kinds


# ---- The three claims this repo shipped before the truth-sync pass -------
#
# Verbatim, so the guard's teeth are demonstrated on real defects rather
# than invented ones. Read back with `git show <sha>:<path>` against the
# commit before the pass; each fixture is the whole string or bullet that
# carried the claim. Each is unbacked in the same way: no committed
# artifact ever measured it, and none of the three cites one.
#
# Held as fixtures rather than read from git on purpose. CI clones shallow,
# so a test that reached for a historical blob would either skip there — a
# guard that silently does nothing on the machine that matters — or need a
# fetch. The cost is that these strings are frozen by hand; that is fine,
# because history is frozen too.
#
# Run against the whole surface set, the pre-pass tree yields eleven
# findings: two on the description, five on the doctor hint, three on the
# threshold comment, one on the README bullet. The internals page yields
# none, because before the pass it stated the cost with no number at all —
# it said the per-turn tool context "stays small".

# The always-resident search description. The pair it quoted was an
# original live-store measurement that never became an artifact; the
# committed replication measures the same probe pair materially higher.
_SEARCH_DESC_BEFORE = (
    "- `query`: nouns a memory would contain (tool, file, error names), "
    "not question phrasing — measured 10%→65% recall@1. Weak hits: "
    "re-query, different nouns.\n"
)

# The README's cost bullet. No cue at all, which is why the size rule
# exists: the byte figure was the claim, and it was several kilobytes low
# while also mis-stating the description share as half.
_README_BULLET_BEFORE = (
    "- Nothing is auto-injected; retrieval is a deliberate tool call. "
    "The 18\n  default tools do cost ~35 KB of schema per turn either "
    "way; the\n  description half of that is capped in CI.\n"
)

# The doctor fix hint, printed verbatim to an operator deciding whether to
# install a several-hundred-megabyte extra on the strength of it. Two of its
# rates ("100%", and the store size behind the pair) had no artifact; the
# store size itself is invisible here, being a bare count.
_DOCTOR_HINT_BEFORE = (
    "Install an embeddings extra — that is now the whole fix. "
    "`bettermemory[embeddings-fast]` is the lighter ONNX path; "
    "`bettermemory[embeddings]` is the torch one, and the choice is "
    "install weight, not capability. The default "
    '`search_mode = "hybrid"` picks the model up on its own and '
    "fuses it as a third leg beside the two lexical ones, which keep "
    "scoring 100% on rare-term queries. No config flag is required: "
    "pairing this with `semantic_dedup = true` used to be needed and "
    "is not, and that flag only ever controlled WRITE-time dedup "
    "(Jaccard vs cosine) — leave it alone unless you want that. "
    "Measured on a 190-memory store: recall@1 10% -> 30% on "
    "questions as asked, 65% -> 80% on re-queried ones. Weigh the "
    "install size; this is reported, never auto-applied."
)

# The same hint AFTER the truth-sync pass and BEFORE this module's repair.
# The pass fixed the rates and added the citation; the perfect rate in the
# sentence above it survived, because a reader checking the citation finds
# the four numbers next to it and stops. This fixture is the sharpest teeth
# demonstration in the file: a claim that survived a deliberate hand audit
# of exactly this surface, caught by the metric-narrowing step.
_DOCTOR_HINT_AFTER_SYNC_BEFORE_REPAIR = (
    "Install an embeddings extra — that is now the whole fix. "
    "`bettermemory[embeddings-fast]` is the lighter ONNX path; "
    "`bettermemory[embeddings]` is the torch one, and the choice is "
    "install weight, not capability. The default "
    '`search_mode = "hybrid"` picks the model up on its own and '
    "fuses it as a third leg beside the two lexical ones, which keep "
    "scoring 100% on rare-term queries. No config flag is required: "
    "pairing this with `semantic_dedup = true` used to be needed and "
    "is not, and that flag only ever controlled WRITE-time dedup "
    "(Jaccard vs cosine) — leave it alone unless you want that. "
    "Measured: recall@1 35% -> 60% on questions as asked, "
    "80% -> 90% on re-queried ones "
    "(bench/retrieval/results/v2-unpadded-2026-07-26.json — 180 "
    "synthetic documents, easier than a real store, so the deltas "
    "carry and the absolute rates do not). Weigh the install "
    "size; this is reported, never auto-applied."
)

# The threshold comment that recorded where a doctor warning level came
# from. The second finding the truth-sync pass left standing, repaired in
# the same commit as this module: a spread quoted to two decimals across
# "real scopes" that no committed run ever recorded.
_THRESHOLD_COMMENT_BEFORE = (
    "#: Topical-query recall@1 at or below this warns. Set from the measured\n"
    "#: spread across real scopes rather than picked: a heterogeneous scope\n"
    "#: measured ~0.71-0.81 and a highly coherent one ~0.31, so the threshold\n"
    '#: sits below the former and above the latter. It is a floor for "this\n'
    '#: scope has a retrieval problem you cannot see", not a quality target.'
)


# Both halves of the contract-constant exemption, in one place because the
# distinction between them is the design. Neither sentence is hypothetical:
# the first is `docs/api.md`'s wording for the episode caps, the second is
# the README bullet the truth-sync pass replaced.
_CAP_SENTENCE = (
    "Episode takeaways are capped at 4 KB; the YAML frontmatter has a 64 KB ceiling."
)
_CAP_WORD_ONE_CLAUSE_AWAY = (
    "The 18 default tools do cost ~35 KB of schema per turn either way; "
    "the description half of that is capped in CI."
)


def test_a_cap_word_governing_a_byte_figure_exempts_it() -> None:
    assert check_text("README.md", _CAP_SENTENCE, size_rule=True) == []


def test_a_cap_word_in_another_clause_does_not_exempt_a_measurement() -> None:
    failures = check_text("README.md", _CAP_WORD_ONE_CLAUSE_AWAY, size_rule=True)
    assert [(f.claim.kind, f.claim.subject) for f in failures] == [("uncited", "35 KB")]


def test_guard_would_have_flagged_the_pre_truth_sync_description() -> None:
    failures = check_text(
        f"{_DESC_SOURCE_PREFIX}DESC_MEMORY_SEARCH", _SEARCH_DESC_BEFORE
    )
    assert {f.claim.kind for f in failures} == {"uncited"}
    assert {f.claim.subject for f in failures} == {"10%", "65%"}


def test_guard_would_have_flagged_the_pre_truth_sync_readme_bullet() -> None:
    failures = check_text("README.md", _README_BULLET_BEFORE, size_rule=True)
    assert [(f.claim.kind, f.claim.subject) for f in failures] == [("uncited", "35 KB")]
    # The cue rule alone would have missed it — that hole is the size
    # rule's whole reason for existing, so it is asserted, not assumed.
    assert check_text("README.md", _README_BULLET_BEFORE) == []


def test_guard_would_have_flagged_the_pre_truth_sync_doctor_hint() -> None:
    failures = check_text(_DOCTOR_MODULE, _DOCTOR_HINT_BEFORE)
    assert {f.claim.kind for f in failures} == {"uncited"}
    assert {f.claim.subject for f in failures} == {"100%", "10%", "30%", "65%", "80%"}
    # The store size is a bare count and stays invisible, which is the
    # not-checked list holding: "190" could as easily be an enumeration.
    assert "190" not in {f.claim.subject for f in failures}


def test_guard_flags_the_rate_that_survived_the_truth_sync_pass() -> None:
    """The finding a hand audit of this exact surface missed.

    Note the kind: ``unpinned``, not ``uncited``. The claim sits in a chunk
    that does cite the right artifact and whose four other rates all pin,
    so a citation check alone would have passed it. Only narrowing the pool
    to the metric the chunk names — recall@1 — rejects it, because the 1.0
    it would otherwise have pinned against is a recall@5 row.
    """
    failures = check_text(_DOCTOR_MODULE, _DOCTOR_HINT_AFTER_SYNC_BEFORE_REPAIR)
    assert [(f.claim.kind, f.claim.subject) for f in failures] == [("unpinned", "100%")]
    # And the repaired string, live at HEAD, is clean — the repair is real
    # and not a re-wording that merely dodged the extractor.
    live = "\n".join(text for _line, text in _python_prose(_DOCTOR_MODULE))
    assert "scoring 100% on rare-term queries" not in live


def test_guard_flags_the_threshold_comment_this_module_repaired() -> None:
    """Coalescing comment lines is what makes this one visible.

    The cue sits on the first line of the block and the numbers on the
    third. Line-at-a-time reading would find a cue with no number and a
    number with no cue, and report nothing.
    """
    failures = check_text(_DOCTOR_MODULE, _THRESHOLD_COMMENT_BEFORE)
    assert {f.claim.kind for f in failures} == {"uncited"}
    assert {f.claim.subject for f in failures} == {"0.71", "0.81", "0.31"}
    first_line_only = _THRESHOLD_COMMENT_BEFORE.split("\n")[0]
    assert check_text(_DOCTOR_MODULE, first_line_only) == []


# ---- Precision guards: the populations that must NOT be flagged ---------
#
# These are the misfires a naive percent-or-size pattern produces on this
# corpus. Each is a contract constant enforced by adjacent code, quoted
# from a shipped description.


def test_contract_constants_in_descriptions_are_not_measurements() -> None:
    for text in (
        "Capped by `max_takeaway_bytes` (default 4 KB) — the takeaway lives "
        "in YAML frontmatter (64 KB ceiling).",
        "- `note` (optional, ≤500 chars): free-form context",
        "demotes 2x under the same flag",
        "Sentences with <30% token overlap to the transcript return "
        "{status:'ungrounded'}",
    ):
        assert check_text(f"{_DESC_SOURCE_PREFIX}DESC_SYNTHETIC", text) == []


def test_bare_counts_are_not_measurements() -> None:
    """Tool counts have their own guards; this one must not double-govern."""
    text = (
        "27 MCP tools; 18 register by default. Nine curation tools sit behind a flag."
    )
    assert check_text("docs/internals.md", text, size_rule=True) == []


def test_metric_narrowing_rejects_a_value_measured_for_another_metric() -> None:
    """A rate quoted as recall@1 may not pin against a recall@5 row.

    The canonical retrieval artifact records a re-queried recall@5 of 1.0,
    so without narrowing a claim of a perfect recall@1 would pin against
    it. This is the step that let a real hint's rate be caught.
    """
    text = (
        "measured recall@1 of 100% "
        "(bench/retrieval/results/v2-unpadded-2026-07-26.json)"
    )
    failures = check_text(_DOCTOR_MODULE, text)
    assert [(f.claim.kind, f.claim.subject) for f in failures] == [("unpinned", "100%")]
    # Same number, honestly labelled, pins — narrowing is not a blanket ban.
    honest = (
        "measured recall@5 of 100% "
        "(bench/retrieval/results/v2-unpadded-2026-07-26.json)"
    )
    assert check_text(_DOCTOR_MODULE, honest) == []


def test_masked_regions_are_not_scanned() -> None:
    """Each mask is proved to be a mask rather than a no-op.

    At HEAD none of the three changes a finding — verified by re-running the
    whole corpus with each disabled. They are here so the rules survive
    prose that grows a badge, a raw link or a config sample, and a mask
    nobody can see working is a mask that quietly stops working.
    """
    for text in (
        # An HTML attribute, badge-shaped, carrying its own alt text.
        '<img alt="measured 12,345 bytes" src="https://example.invalid/b.svg">',
        # A bare link in prose, which no tag mask would catch.
        "measured — the raw run is at https://example.invalid/r/12,345-bytes",
        # A fenced sample, where the digits are code and not a claim.
        "```\n# measured 12,345 bytes\n```",
    ):
        assert check_text("README.md", text, size_rule=True) == [], text
    # The same digits in plain prose are a finding, so what differs above is
    # the masking and not the shape of the number.
    plain = check_text("README.md", "measured 12,345 bytes", size_rule=True)
    assert [(f.claim.kind, f.claim.subject) for f in plain] == [
        ("uncited", "12,345 bytes")
    ]


def test_a_citation_does_not_vouch_across_list_items() -> None:
    """Chunking is what keeps a citation local to its own claim."""
    text = (
        "- measured 38,009 bytes "
        "(bench/toolcost/results/bettermemory-vs-claude-mem-2026-07-26.json)\n"
        "- measured 12,345 bytes of something else entirely\n"
    )
    failures = check_text("README.md", text, size_rule=True)
    assert [(f.claim.kind, f.claim.subject) for f in failures] == [
        ("uncited", "12,345 bytes")
    ]


def test_reverse_guard_fires_on_a_synthetic_stale_entry() -> None:
    """The live allowlist is empty, so the ratchet is proved on a fixture."""
    key = ("desc:DESC_SYNTHETIC", "uncited", "42%")
    reason = "a synthetic entry used only to prove the reverse guard fires"
    assert len(reason) >= 40
    stale = stale_entries({key: reason}, set())
    assert stale == [key]
    report = _stale_report({key: reason}, stale)
    assert "the claim was repaired" in report
    assert "the extractor stopped matching" in report
    assert stale_entries({key: reason}, {key}) == []
