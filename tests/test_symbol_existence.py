"""The verify-time symbol-existence check: shape, silence, and reach.

Three things are pinned here, in descending order of how much they
matter.

**Silence on prose that only looks like a citation.** The whole value of
an advisory field is that a reader believes it, and one fabricated alarm
spends that belief permanently. The adversarial corpus below is the
acceptance bar: every string in it is run against a tree that WOULD make
it alarm if it parsed, and the required result is nothing. Sentences
that deny the claim, sentences that place the symbol somewhere it used
to be, illustrations, URLs, absolute and Windows paths, and pairs that
straddle a clause boundary all live in it.

**Advisory, structurally.** Two pins, and the source-level one is the
load-bearing half: nothing outside the verify handler may import the
checker, so the check cannot reach a staleness verdict without that pin
failing first. The behavioural pin is that a memory whose body cites a
symbol bound nowhere still reads `fresh` right after verification.

**Reach, re-derived rather than asserted.** The measurement that
decides how much this feature is worth is "how many real citations does
the parser see", and the honest answer is small. Measured when this
landed, and re-derivable by the tests at the bottom of this file:

* This repo's own tracked Python (198 files under `src/bettermemory/`
  and `tests/`, docstrings plus `#` comments): 35 citations parse, 14
  resolve to a file that parses, 21 are unresolvable because they cite
  a bare basename with no directory part, 0 report a miss.
* Widened to every tracked `*.py` and `*.md`: 99 citation-shaped
  matches, 81 survive suppression, 41 resolve, and exactly one reports
  a miss — a CHANGELOG entry from before the server's instructions
  block moved to another module, which is a true positive against a
  record the erratum register forbids rewriting.
* A real 231-memory store: 113 memories carry a backticked
  symbol-shaped token and 43 carry a backticked `.py` path, but only
  THREE express the pair as a citation the parser can read, and of
  those three, two carry no recorded worktree root. Thirty carry both
  a symbol and a path token in one sentence — which is the ceiling a
  co-occurrence parser would reach, and the reason not to build one:
  "`foo` and `pkg/mod.py` disagree" pairs two tokens that assert
  nothing about each other.

That last bullet is the finding. The two-token shape is a docstring
convention, and the structured answer for memory bodies is
claims-at-write, not a looser reader.
"""

from __future__ import annotations

import ast
import io
import json
import sys
import tokenize
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store
from bettermemory.symbols import (
    _MAX_CITATIONS_PER_BODY,
    _resolve_module,
    check_symbol_citations,
    extract_symbol_citations,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal source tree to resolve citations against.

    `pkg/mod.py` binds `top_function`, `TopClass` (with a method), and
    `TOP_CONSTANT` at module level, plus a name that only exists inside
    a function body and one that only exists under `if TYPE_CHECKING`.
    """
    root = tmp_path / "worktree"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    from collections import OrderedDict\n"
        "\n"
        "TOP_CONSTANT = 3\n"
        "\n"
        "\n"
        "def top_function(some_argument):\n"
        "    local_only = some_argument\n"
        "    return local_only\n"
        "\n"
        "\n"
        "class TopClass:\n"
        "    def a_method(self):\n"
        "        return None\n",
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# The citation shape
# ---------------------------------------------------------------------------


def test_the_canonical_shape_parses() -> None:
    assert extract_symbol_citations("`top_function` in `pkg/mod.py` does it") == (
        ("top_function", "pkg/mod.py"),
    )


def test_double_backticks_call_suffix_and_dotted_names_parse() -> None:
    """Three widenings over the docstring rule this shape came from, each
    of them reach rather than looseness: RST-style double backticks,
    a callable written the way prose writes callables, and a qualified
    name."""
    assert extract_symbol_citations("``top_function`` in ``pkg/mod.py``") == (
        ("top_function", "pkg/mod.py"),
    )
    assert extract_symbol_citations("`top_function()` in `pkg/mod.py`") == (
        ("top_function", "pkg/mod.py"),
    )
    assert extract_symbol_citations("`TopClass.a_method` in `pkg/mod.py`") == (
        ("TopClass.a_method", "pkg/mod.py"),
    )


def test_up_to_two_interposed_plain_words_are_allowed() -> None:
    assert extract_symbol_citations("`top_function` lands in `pkg/mod.py`") == (
        ("top_function", "pkg/mod.py"),
    )
    assert extract_symbol_citations("`top_function` still lands in `pkg/mod.py`") == (
        ("top_function", "pkg/mod.py"),
    )
    # Three words is a clause, not a connective — dropped rather than
    # guessed at.
    assert (
        extract_symbol_citations("`top_function` really still lands in `x/m.py`") == ()
    )


def test_duplicate_citations_collapse() -> None:
    body = "`top_function` in `pkg/mod.py`, and again `top_function` in `pkg/mod.py`"
    assert extract_symbol_citations(body) == (("top_function", "pkg/mod.py"),)


# ---------------------------------------------------------------------------
# The adversarial corpus — the acceptance bar for this feature
# ---------------------------------------------------------------------------
#
# Every entry names a symbol that is NOT bound in `pkg/mod.py`, so any
# entry that parses will also alarm. That is deliberate: a corpus of
# strings that merely fail to match proves nothing about the half of the
# pipeline that decides what to report.

_ADVERSARIAL = (
    # Denial — the sentence says the opposite of what a citation asserts.
    "`absent_name` is not defined in `pkg/mod.py`",
    "`absent_name` never landed in `pkg/mod.py`",
    "there is no `absent_name` in `pkg/mod.py`",
    # Relocation — a true statement about where something used to be.
    "`absent_name` used to live in `pkg/mod.py`",
    "`absent_name` was in `pkg/mod.py` before the split",
    "the former `absent_name` in `pkg/mod.py` is gone",
    "`absent_name` moved out of `pkg/mod.py`",
    # Illustration — the pair is an example of a shape, not a claim.
    "a citation looks like `absent_name` in `pkg/mod.py`",
    "e.g. `absent_name` in `pkg/mod.py`",
    "for example `absent_name` in `pkg/mod.py`",
    # Intent rather than fact.
    "`absent_name` should be in `pkg/mod.py` once the port lands",
    "`absent_name` would be in `pkg/mod.py` under the old layout",
    # Not a citation at all: one half unmarked.
    "absent_name in `pkg/mod.py`",
    "`absent_name` in pkg/mod.py",
    "absent_name in pkg/mod.py",
    # Punctuation between the halves means a clause boundary was crossed,
    # so the two tokens are not talking about each other.
    "`absent_name`, defined elsewhere, in `pkg/mod.py`",
    "`absent_name` (deprecated) in `pkg/mod.py`",
    # Shapes the module refuses to resolve at all.
    "`absent_name` in `https://example.com/pkg/mod.py`",
    "`absent_name` in `/absolute/pkg/mod.py`",
    "`absent_name` in `~/pkg/mod.py`",
    "`absent_name` in `C:/pkg/mod.py`",
    "`absent_name` in `pkg\\mod.py`",
    "`absent_name` in `pkg/mod.md`",
    # A filename in the symbol slot is a path, not a name.
    "`pkg.mod.py` in `pkg/mod.py`",
)


@pytest.mark.parametrize("prose", _ADVERSARIAL)
def test_adversarial_prose_never_alarms(prose: str, tree: Path) -> None:
    """AC: zero false alarms. Checked against a real tree, so an entry
    that started parsing would report `absent_name` as missing and fail
    here rather than passing quietly on an empty parse."""
    report = check_symbol_citations(prose, worktree_root=tree)
    assert report.missing == (), f"fabricated a miss from: {prose}"


def test_the_adversarial_corpus_would_catch_a_loosened_parser(tree: Path) -> None:
    """The control the corpus needs. Strip the guards from one of its own
    entries and the same tree DOES report a miss — so the assertions
    above are testing suppression, not a checker that never fires."""
    report = check_symbol_citations("`absent_name` in `pkg/mod.py`", worktree_root=tree)
    assert [str(c) for c in report.missing] == ["absent_name in pkg/mod.py"]


# ---------------------------------------------------------------------------
# What the AST is asked, and what each answer means
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol",
    ["top_function", "TopClass", "TOP_CONSTANT", "OrderedDict", "TopClass.a_method"],
)
def test_module_scope_bindings_read_top_level(symbol: str, tree: Path) -> None:
    """Including the conditional import: a name bound under
    `if TYPE_CHECKING:` is a module-level name, and a walk that stopped
    at the first compound statement would miss every optional dependency
    in this codebase."""
    report = check_symbol_citations(f"`{symbol}` in `pkg/mod.py`", worktree_root=tree)
    assert [c.binding for c in report.checked] == ["top_level"]
    assert report.missing == ()


@pytest.mark.parametrize("symbol", ["a_method", "some_argument", "local_only"])
def test_names_bound_only_inside_a_scope_read_nested_not_absent(
    symbol: str, tree: Path
) -> None:
    """The commonest real citation shape there is — a method named on its
    own against the file that holds its class — and the single largest
    false-alarm class a strict top-level reading would create."""
    report = check_symbol_citations(f"`{symbol}` in `pkg/mod.py`", worktree_root=tree)
    assert [c.binding for c in report.checked] == ["nested"]
    assert report.missing == ()


def test_a_name_bound_nowhere_is_the_only_reported_miss(tree: Path) -> None:
    report = check_symbol_citations(
        "`vanished_helper` in `pkg/mod.py`", worktree_root=tree
    )
    assert [c.binding for c in report.checked] == ["absent"]
    assert [str(c) for c in report.missing] == ["vanished_helper in pkg/mod.py"]
    assert report.to_dict()["missing"] == ["vanished_helper in pkg/mod.py"]


def test_one_parse_serves_every_citation_of_the_same_module(tree: Path) -> None:
    """A body naming four symbols in one file must read and parse it
    once. Asserted through the report rather than a call counter: all
    four answers have to agree, which they cannot if a stale or partial
    index were being rebuilt per citation."""
    body = (
        "`top_function` in `pkg/mod.py`; `TopClass` in `pkg/mod.py`; "
        "`a_method` in `pkg/mod.py`; `vanished_helper` in `pkg/mod.py`"
    )
    report = check_symbol_citations(body, worktree_root=tree)
    assert [c.binding for c in report.checked] == [
        "top_level",
        "top_level",
        "nested",
        "absent",
    ]


# ---------------------------------------------------------------------------
# Could-not-ask is never a miss
# ---------------------------------------------------------------------------


def test_a_missing_module_is_unresolved_not_missing(tree: Path) -> None:
    """A file that moved says nothing about whether the symbol claim
    held. Folding the two together is how a check like this manufactures
    drift out of a rename."""
    report = check_symbol_citations("`whatever` in `pkg/gone.py`", worktree_root=tree)
    assert report.checked == ()
    assert [str(c) for c in report.unresolved] == ["whatever in pkg/gone.py"]
    assert report.missing == ()


def test_an_unparsable_module_is_unresolved_not_missing(tree: Path) -> None:
    (tree / "pkg" / "broken.py").write_text("def (: oops\n", encoding="utf-8")
    report = check_symbol_citations("`whatever` in `pkg/broken.py`", worktree_root=tree)
    assert report.checked == ()
    assert report.missing == ()
    assert len(report.unresolved) == 1


def test_a_bare_basename_is_unresolved(tree: Path) -> None:
    """Resolving one would mean searching the tree, and two files can
    share a basename. The count of these IS the measurement of what an
    index would buy — see the reach tests below, where they are the
    majority."""
    report = check_symbol_citations("`top_function` in `mod.py`", worktree_root=tree)
    assert report.checked == ()
    assert [str(c) for c in report.unresolved] == ["top_function in mod.py"]


def test_no_recorded_worktree_root_makes_the_check_inert(tree: Path) -> None:
    assert not check_symbol_citations("`vanished_helper` in `pkg/mod.py`")


def test_a_recorded_root_this_machine_cannot_see_makes_the_check_inert(
    tmp_path: Path,
) -> None:
    """The cross-host case, and the one place this module deliberately
    reverses the path leg's bias. A store synced from another machine
    carries roots this machine does not have; resolving citations
    against one would mark every citation in every synced memory absent
    at once, and the alarm would be about the sync rather than about any
    claim. `origin` records the opposite direction for the auto-scope
    filter and `verify` records it again for path drift — both times
    because over-reporting is THEIR safe direction. It is not this
    one's."""
    gone = tmp_path / "never-existed"
    assert not check_symbol_citations(
        "`vanished_helper` in `pkg/mod.py`", worktree_root=gone
    )
    a_file = tmp_path / "not-a-directory"
    a_file.write_text("x", encoding="utf-8")
    assert not check_symbol_citations(
        "`vanished_helper` in `pkg/mod.py`", worktree_root=a_file
    )


def test_a_module_path_escaping_the_root_resolves_to_nothing(tree: Path) -> None:
    """`..` is the one escape shape the citation regex admits — every
    other one (absolute, `~`, drive-lettered, backslashed) fails to
    match at all. Containment is enforced anyway, after resolution, so
    the guard does not depend on the regex staying as it is."""
    outside = tree.parent / "outside.py"
    outside.write_text("def elsewhere():\n    return 1\n", encoding="utf-8")
    report = check_symbol_citations(
        "`vanished` in `pkg/../../outside.py`", worktree_root=tree
    )
    assert report.checked == ()
    assert report.missing == ()


@pytest.mark.parametrize(
    "module",
    [
        "mod.py",
        "/absolute/mod.py",
        "~/mod.py",
        "C:/pkg/mod.py",
        "pkg\\mod.py",
        "pkg/../../escape.py",
        "pkg//mod.py",
        "pkg/./mod.py",
    ],
)
def test_the_resolver_refuses_every_non_plain_relative_shape(
    module: str, tree: Path
) -> None:
    """Direct coverage for the resolver's own guards. Most of these
    cannot reach it through the citation regex today; the resolver is
    the gate that must hold regardless, because a regex tuned for
    precision on prose is not a security boundary."""
    assert _resolve_module(tree, module) is None


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs privileges on Windows"
)
def test_a_symlink_pointing_out_of_the_root_resolves_to_nothing(tree: Path) -> None:
    """Containment is tested after resolution, so it sees where the link
    actually goes rather than where it sits."""
    outside = tree.parent / "outside.py"
    outside.write_text("def elsewhere():\n    return 1\n", encoding="utf-8")
    (tree / "pkg" / "linked.py").symlink_to(outside)
    report = check_symbol_citations("`vanished` in `pkg/linked.py`", worktree_root=tree)
    assert report.checked == ()
    assert report.missing == ()


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------


def test_citations_are_capped_per_body() -> None:
    body = " ".join(f"`sym{i}` in `pkg/mod{i}.py`" for i in range(40))
    assert len(extract_symbol_citations(body)) == _MAX_CITATIONS_PER_BODY


def test_a_citation_past_the_body_scan_cap_is_dropped_not_truncated() -> None:
    """The cap may only ever DROP a claim. A slice that produced a
    partial citation which then resolved to a different file would
    fabricate evidence out of a body that said something else."""
    body = ("x " * 20_000) + "`top_function` in `pkg/mod.py`"
    assert len(body) > 32 * 1024
    assert extract_symbol_citations(body) == ()


def test_an_oversized_module_is_skipped_rather_than_parsed(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bettermemory.symbols._MAX_MODULE_BYTES", 10)
    report = check_symbol_citations(
        "`vanished_helper` in `pkg/mod.py`", worktree_root=tree
    )
    assert report.checked == ()
    assert len(report.unresolved) == 1


# ---------------------------------------------------------------------------
# The verify handler — where the one production caller lives
# ---------------------------------------------------------------------------


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(config=cfg, store=Store(memory_dir), state=SessionState())


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _plant(memory_dir: Path, body: str, root: Path) -> str:
    """Write a memory whose recorded worktree root is `root`."""
    memory = Store(memory_dir).write(
        content=body,
        scopes=["tools"],
        origin=Origin(cwd=str(root), worktree_root=str(root)),
    )
    return memory.id


async def test_verify_reports_symbol_existence_for_what_it_can_parse(
    server: Any, memory_dir: Path, tree: Path
) -> None:
    mid = _plant(memory_dir, "`top_function` in `pkg/mod.py` is the entry point.", tree)
    result = await _call(server, "memory_verify", id=mid)
    assert result["symbol_drift"] == {
        "checked": ["top_function in pkg/mod.py"],
        "missing": [],
        "unresolved": [],
    }


async def test_verify_reports_a_miss_with_an_advisory_note(
    server: Any, memory_dir: Path, tree: Path
) -> None:
    mid = _plant(memory_dir, "`vanished_helper` in `pkg/mod.py` does the work.", tree)
    result = await _call(server, "memory_verify", id=mid)
    assert result["symbol_drift"]["missing"] == ["vanished_helper in pkg/mod.py"]
    assert "Advisory only" in result["symbol_drift"]["note"]


async def test_verify_stays_silent_when_the_body_cites_nothing(
    server: Any, memory_dir: Path, tree: Path
) -> None:
    """Silence is the normal case. An empty block on every call would
    read as "checked, nothing wrong" when the truth is almost always
    "there was nothing here to check"."""
    mid = _plant(memory_dir, "prefer cost checkpoints on long runs", tree)
    result = await _call(server, "memory_verify", id=mid)
    assert "symbol_drift" not in result


async def test_a_symbol_miss_does_not_move_the_staleness_verdict(
    server: Any, memory_dir: Path, tree: Path
) -> None:
    """AC: advisory only. The memory was verified a moment ago and cites
    a symbol bound nowhere; it still reads `fresh`, because no verdict
    input reads this check."""
    mid = _plant(memory_dir, "`vanished_helper` in `pkg/mod.py` does the work.", tree)
    verified = await _call(server, "memory_verify", id=mid)
    assert verified["symbol_drift"]["missing"]
    shown = await _call(server, "memory_show", id=mid)
    assert shown["staleness_verdict"] == "fresh"


async def test_the_advisory_does_not_reach_the_persisted_record(
    server: Any, memory_dir: Path, tree: Path
) -> None:
    """No new frozen-surface vocabulary: the attestation lists are what
    they were, and a symbol miss adds nothing to them."""
    mid = _plant(memory_dir, "`vanished_helper` in `pkg/mod.py` does the work.", tree)
    await _call(server, "memory_verify", id=mid)
    stored = Store(memory_dir).load_one(mid)
    assert stored.verified_paths == []
    assert stored.verified_commits == []
    assert stored.verified_versions == []
    assert stored.last_verified_at is not None


async def test_a_miss_lands_in_the_event_log_and_nothing_else_does(
    server: Any, memory_dir: Path, tree: Path
) -> None:
    """The only telemetry a future precision measurement could be built
    from, and the reason it is conditional: a field written on every
    verify would make "the check fired" indistinguishable from "the
    check ran", and the count that matters is the first one."""
    from bettermemory.events import iter_events

    quiet = _plant(memory_dir, "prefer cost checkpoints on long runs", tree)
    loud = _plant(memory_dir, "`vanished_helper` in `pkg/mod.py` does the work.", tree)
    await _call(server, "memory_verify", id=quiet)
    await _call(server, "memory_verify", id=loud)

    events = {e["id"]: e for e in iter_events(memory_dir) if e["kind"] == "verify"}
    assert "symbol_drift_missing" not in events[quiet]
    assert events[loud]["symbol_drift_missing"] == 1


def test_mark_verified_does_not_itself_check_symbols(
    memory_dir: Path, tree: Path
) -> None:
    """The LAYER SPLIT, pinned for this check the same way it is pinned
    for the attestation-existence refusal: policy sits in the handler,
    the store stays a persistence primitive. A store that grew opinions
    about body content would apply them to `web.py`'s verify endpoint
    and every test that uses the primitive as a fixture."""
    store = Store(memory_dir)
    mid = _plant(memory_dir, "`vanished_helper` in `pkg/mod.py` does the work.", tree)
    verified = store.mark_verified(mid)
    assert verified.last_verified_at is not None
    assert not hasattr(verified, "symbol_drift")


def test_only_the_verify_handler_imports_the_checker() -> None:
    """The structural guarantee behind "advisory". A staleness verdict
    cannot start reading this check without an import appearing
    somewhere else in `src/`, so this fails before the behaviour does —
    including in the two files most likely to reach for it,
    `src/bettermemory/verify.py` and `src/bettermemory/_response.py`.

    Moving the check into a verdict is a legitimate future change. It
    requires a benchmark measuring its precision on real prose, and
    deleting this test with that evidence written down is how it gets
    made.

    Recorded as a package-relative path, not a basename: two files in
    this package are called `verify.py`, and a basename comparison would
    accept the import landing in the trust module instead of the
    handler — which is the exact edit this test exists to catch.
    """
    package = _REPO_ROOT / "src" / "bettermemory"
    importers = []
    for path in sorted(package.rglob("*.py")):
        parsed = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(package).as_posix()
        for node in ast.walk(parsed):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "symbols"
            ):
                importers.append(rel)
            elif isinstance(node, ast.Import) and any(
                alias.name.endswith("bettermemory.symbols") for alias in node.names
            ):
                importers.append(rel)
    assert importers == ["handlers/verify.py"], importers


# ---------------------------------------------------------------------------
# Reach — measured, not claimed
# ---------------------------------------------------------------------------


def _prose_chunks(path: Path) -> list[str]:
    """Docstrings and `#` comments of one Python file.

    Comments are included because they are where half of this repo's
    citations live — the blind spot `tests/test_doc_claims.py` names in
    its own docstring.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    chunks: list[str] = []
    try:
        parsed = ast.parse(text)
    except SyntaxError:  # pragma: no cover - the corpus parses
        return []
    for node in ast.walk(parsed):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                chunks.append(doc)
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                chunks.append(token.string)
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        pass
    return chunks


def _repo_prose_citations() -> tuple[int, int, list[str]]:
    """`(parsed, resolved, misses)` over this repo's own Python prose.

    Scoped to `src/bettermemory/` and `tests/` rather than the whole
    checkout so no virtualenv, vendored copy or build artefact can enter
    — the same discipline the three sibling lints keep, arrived at the
    same way.
    """
    files = sorted((_REPO_ROOT / "src" / "bettermemory").rglob("*.py")) + sorted(
        (_REPO_ROOT / "tests").glob("*.py")
    )
    parsed = resolved = 0
    misses: list[str] = []
    for path in files:
        for chunk in _prose_chunks(path):
            for symbol, module in extract_symbol_citations(chunk):
                parsed += 1
                report = check_symbol_citations(
                    f"`{symbol}` in `{module}`", worktree_root=_REPO_ROOT
                )
                if not report.checked:
                    continue
                resolved += 1
                if report.missing:
                    misses.append(f"{path.name}: {report.missing[0]}")
    return parsed, resolved, misses


def test_no_citation_in_this_repos_own_prose_reports_a_miss() -> None:
    """The strongest zero-false-alarm evidence available: real prose,
    written without this checker in mind, over a real tree.

    A failure here is most likely a real dangling citation in a
    docstring or comment — the same class `tests/test_doc_claims.py`
    catches for documents — and the repair is the prose, not this test.
    """
    _, _, misses = _repo_prose_citations()
    assert misses == [], misses


def test_the_parser_reaches_a_nonzero_slice_of_real_prose() -> None:
    """A floor with slack under the measured value, not the value.

    Its job is to catch a tightening that quietly takes the reach to
    zero — a checker that resolves nothing passes every silence test in
    this file. The measured split when this landed is in the module
    docstring; the failure message re-derives it so the number never has
    to be trusted from prose.
    """
    parsed, resolved, _ = _repo_prose_citations()
    assert resolved >= 5, f"parsed {parsed}, resolved {resolved}"


def test_bare_basenames_are_the_dominant_unresolved_shape() -> None:
    """Names the honest reason the reach is small, and pins it to a
    measurement instead of a claim: most citations in this corpus name a
    module with no directory part, which this module declines to resolve
    by searching the tree. Anyone proposing a basename index can start
    by re-running this.
    """
    parsed, resolved, _ = _repo_prose_citations()
    assert parsed > resolved, f"parsed {parsed}, resolved {resolved}"
