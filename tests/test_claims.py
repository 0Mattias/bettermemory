"""Unit tests for `bettermemory.claims` — the wire syntax, the
declare-time oracle, and the lenient stored-claim loader.

The detector half (`build_binding_index` / `claim_level_drift`) is NOT
re-tested here for the three measured kinds: `tests/test_bench_rot.py`
exercises those through `bench/rot/run.py`, which imports the shipped
functions — one suite, one copy, no chance of the two drifting. What
this module owns is the part the bench never had: parsing
caller-supplied claim strings, checking a claim against a live worktree
at declaration — and the ABSENT kind's tier semantics, the one detector
branch the bench corpus never contains
(the T2 absence-claim declaration).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bettermemory.claims import (
    MAX_CLAIMS,
    Claim,
    build_binding_index,
    check_claim,
    claim_level_drift,
    claim_paths,
    load_claims,
    parse_claim,
    parse_claims,
)


# ---------------------------------------------------------------------------
# parse_claim — the three shapes and their canonical forms
# ---------------------------------------------------------------------------


def test_parse_path_claim() -> None:
    claim = parse_claim("src/pkg/mod.py")
    assert claim == Claim("path", "src/pkg/mod.py", "src/pkg/mod.py", "")
    assert claim.render() == "src/pkg/mod.py"


def test_parse_symbol_claim() -> None:
    claim = parse_claim("src/pkg/mod.py::handler")
    assert claim == Claim("symbol", "src/pkg/mod.py", "handler", "")
    assert claim.render() == "src/pkg/mod.py::handler"


def test_parse_literal_claim_normalizes_value_to_repr() -> None:
    # `30` and `"30"` are different claims; repr space keeps them apart.
    numeric = parse_claim("src/pkg/mod.py::TIMEOUT=30")
    assert numeric.kind == "literal"
    assert numeric.value == "30"
    quoted = parse_claim("src/pkg/mod.py::NAME='foo'")
    assert quoted.value == "'foo'"
    # Round-trip: render() emits the normalized form, which re-parses to
    # the same claim — what gets stored is canonical.
    assert parse_claim(numeric.render()) == numeric
    assert parse_claim(quoted.render()) == quoted


def test_parse_literal_claim_is_type_sensitive() -> None:
    assert parse_claim("m.py::X=30").value != parse_claim("m.py::X=30.0").value


def test_parse_set_claim_canonicalizes_element_order() -> None:
    """A set literal is one claim however the caller orders the elements
    — and however the process's hash seed lays the set out. `{8, 16}`
    and `{16, 8}` are equal sets whose plain `repr`s differ (colliding
    small ints keep insertion order; string sets reorder per seed), so
    the stored form sorts elements by their canonical repr."""
    a = parse_claim("m.py::ALLOWED={8, 16}")
    b = parse_claim("m.py::ALLOWED={16, 8}")
    assert a == b
    assert a.value == "{16, 8}"
    # Seed-independent spelling: what one server process stores, the
    # next re-parses to the identical claim.
    spelled = parse_claim("m.py::S={'gamma', 'beta', 'alpha'}")
    assert spelled.value == "{'alpha', 'beta', 'gamma'}"
    assert parse_claim(a.render()) == a


def test_parse_dict_claim_canonicalizes_key_order() -> None:
    a = parse_claim("m.py::CFG={'b': 2, 'a': 1}")
    b = parse_claim("m.py::CFG={'a': 1, 'b': 2}")
    assert a == b
    assert a.value == "{'a': 1, 'b': 2}"
    assert parse_claim(a.render()) == a


def test_canonical_form_recurses_and_keeps_types_distinct() -> None:
    nested = parse_claim("m.py::X=[{2, 1}, {'b': {4, 3}, 'a': 0}]")
    assert nested.value == "[{1, 2}, {'a': 0, 'b': {3, 4}}]"
    assert parse_claim(nested.render()) == nested
    assert parse_claim("m.py::E=set()").value == "set()"
    # Repr space still separates `{30}` from `{30.0}` even though the
    # sets compare equal — same rule as bare `30` vs `30.0`.
    assert parse_claim("m.py::X={30}").value != parse_claim("m.py::X={30.0}").value


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "::name",
        "src\\pkg\\mod.py",
        "src/pkg/mod.py::",
        "src/pkg/mod.py::not-an-identifier",
        "src/pkg/mod.py::name.attr",
        "src/pkg/mod.py::NAME=",
        "src/pkg/mod.py::NAME=some_function()",
    ],
)
def test_parse_claim_refuses_bad_shapes(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_claim(bad)


def test_parse_claim_refusal_names_the_defect() -> None:
    """The refusal is the syntax documentation — each defect class names
    itself, because the tool description deliberately carries only the
    three-shape summary (the budget note in tests/test_server.py)."""
    with pytest.raises(ValueError, match="not a Python identifier"):
        parse_claim("m.py::bad-name")
    with pytest.raises(ValueError, match="not a Python literal"):
        parse_claim("m.py::NAME=call()")
    with pytest.raises(ValueError, match="forward slashes"):
        parse_claim("src\\mod.py")


def test_parse_claims_deduplicates_after_normalization() -> None:
    claims = parse_claims(["m.py::X=30", "m.py::X=0x1e", "m.py"])
    # 30 and 0x1e are the same literal in repr space — one claim.
    assert [c.render() for c in claims] == ["m.py::X=30", "m.py"]


def test_parse_claims_enforces_count_cap() -> None:
    with pytest.raises(ValueError, match="capped"):
        parse_claims([f"file{i}.py" for i in range(MAX_CLAIMS + 1)])


def test_load_claims_skips_invalid_entries() -> None:
    """The read side never crashes on a hand-edited frontmatter list."""
    loaded = load_claims(["good.py::f", "::broken", "also_good.py", 42])  # type: ignore[list-item]
    assert [c.render() for c in loaded] == ["good.py::f", "also_good.py"]


def test_claim_paths_deduplicates_preserving_order() -> None:
    claims = parse_claims(["b.py::f", "a.py", "b.py::G=1"])
    assert claim_paths(claims) == ["b.py", "a.py"]


# ---------------------------------------------------------------------------
# The absence shape — `!path`, the polarity mirror (T2)
# ---------------------------------------------------------------------------


def test_parse_absence_claim() -> None:
    claim = parse_claim("!src/pkg/gone.py")
    assert claim == Claim("absent", "src/pkg/gone.py", "src/pkg/gone.py", "")
    assert claim.render() == "!src/pkg/gone.py"
    assert parse_claim(claim.render()) == claim


def test_parse_absence_claim_normalizes_spacing() -> None:
    assert parse_claim("  ! src/gone.py ").render() == "!src/gone.py"


def test_parse_absence_claim_is_path_only() -> None:
    """Symbol- and literal-absence have no measured evidence base; the
    refusal teaches the boundary, as the grammar's refusals always do."""
    with pytest.raises(ValueError, match="path-only"):
        parse_claim("!src/mod.py::handler")
    with pytest.raises(ValueError, match="path-only"):
        parse_claim("!src/mod.py::NAME=1")
    with pytest.raises(ValueError, match="needs a path"):
        parse_claim("!")
    with pytest.raises(ValueError, match="forward slashes"):
        parse_claim("!src\\gone.py")


def test_parse_absence_and_presence_are_distinct_claims() -> None:
    """An absence claim and a presence claim on the same path are
    distinct; two spellings of the same absence collapse to one
    canonical form."""
    claims = parse_claims(["!a.py", "a.py", "! a.py"])
    assert [c.render() for c in claims] == ["!a.py", "a.py"]


# ---------------------------------------------------------------------------
# check_claim — the declare-time oracle
# ---------------------------------------------------------------------------


MODULE_SOURCE = '''\
"""Module under claim."""

TIMEOUT = 30
REBOUND = "first"
REBOUND = "second"
ANNOTATED: int = 7
COMPUTED = compute_something()


def handler():
    def nested():
        pass
    return nested


class Store:
    def method(self):
        pass
'''


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(MODULE_SOURCE)
    (tmp_path / "README.md").write_text("# readme\n")
    return tmp_path


def _reason(tree: Path, raw: str) -> str | None:
    return check_claim(parse_claim(raw), tree)


def test_oracle_accepts_true_claims(tree: Path) -> None:
    assert _reason(tree, "pkg/mod.py") is None
    assert _reason(tree, "README.md") is None
    assert _reason(tree, "pkg/mod.py::handler") is None
    assert _reason(tree, "pkg/mod.py::Store") is None
    assert _reason(tree, "pkg/mod.py::TIMEOUT=30") is None


def test_oracle_refuses_missing_path(tree: Path) -> None:
    assert "does not exist" in str(_reason(tree, "pkg/gone.py"))


def test_oracle_refuses_escaping_paths(tree: Path) -> None:
    for escape in ("../outside.py", "/etc/passwd", "~/x.py", "a/../../b.py"):
        reason = check_claim(Claim("path", escape, escape, ""), tree)
        assert reason is not None and "worktree" in reason


def test_oracle_refuses_method_as_top_level_symbol(tree: Path) -> None:
    """The measured oracle's strictness is the shipped strictness: a
    method is not `ast.Module.body`, and the refusal points at the
    class/path forms rather than silently accepting an unwatchable
    claim (the column-0 detector could never fire for it)."""
    reason = _reason(tree, "pkg/mod.py::method")
    assert reason is not None and "not a top-level def/class" in reason
    assert _reason(tree, "pkg/mod.py::nested") is not None


def test_oracle_refuses_wrong_literal_and_reports_current(tree: Path) -> None:
    reason = _reason(tree, "pkg/mod.py::TIMEOUT=60")
    assert reason is not None
    assert "`30`" in reason and "`60`" in reason


def test_oracle_accepts_unordered_literals_in_any_spelling(tree: Path) -> None:
    """A true set/dict claim must not be refused over element or key
    order: the source's layout and the claim's spelling both pass
    through the same canonicalization, so equal values compare equal
    in any process."""
    (tree / "pkg" / "unordered.py").write_text(
        'ALLOWED = {16, 8}\nLABELS = {"b": 2, "a": 1}\n'
    )
    assert _reason(tree, "pkg/unordered.py::ALLOWED={8, 16}") is None
    assert _reason(tree, "pkg/unordered.py::LABELS={'a': 1, 'b': 2}") is None


def test_oracle_first_binding_wins_on_rebound_name(tree: Path) -> None:
    assert _reason(tree, "pkg/mod.py::REBOUND='first'") is None
    assert _reason(tree, "pkg/mod.py::REBOUND='second'") is not None


def test_oracle_refuses_annotated_assignment_as_literal(tree: Path) -> None:
    """`X: int = 7` is invisible to the bench oracle (plain ast.Assign
    only) — the shipped gate matches the measured configuration and the
    refusal routes the caller to the symbol/path forms instead."""
    reason = _reason(tree, "pkg/mod.py::ANNOTATED=7")
    assert reason is not None and "not assigned at module level" in reason


def test_oracle_refuses_non_literal_assignment(tree: Path) -> None:
    reason = _reason(tree, "pkg/mod.py::COMPUTED='x'")
    assert reason is not None and "not a literal assignment" in reason


def test_oracle_refuses_symbol_claim_on_unparsable_file(tree: Path) -> None:
    (tree / "pkg" / "broken.py").write_text("def broken(:\n")
    assert _reason(tree, "pkg/broken.py") is None  # path claim: exists
    assert "does not parse" in str(_reason(tree, "pkg/broken.py::broken"))


def test_oracle_symbol_claim_on_non_python_file(tree: Path) -> None:
    """Markdown prose is a SyntaxError to `ast.parse`, so a symbol claim
    against it refuses on parse — except the degenerate case of a file
    that happens to be valid Python (a comment-only README), which
    refuses on the symbol lookup instead. Either way: refused."""
    (tree / "PROSE.md").write_text("A plain sentence, not Python.\n")
    reason = _reason(tree, "PROSE.md::heading")
    assert reason is not None and "does not parse" in reason
    # Comment-only markdown parses as an empty module — still refused,
    # via the not-top-level branch.
    assert _reason(tree, "README.md::heading") is not None


# ---------------------------------------------------------------------------
# check_claim — the absent kind's inverted oracle
# ---------------------------------------------------------------------------


def test_oracle_absence_holds_when_nothing_occupies_the_path(tree: Path) -> None:
    assert _reason(tree, "!pkg/gone.py") is None


def test_oracle_absence_refuses_existing_file(tree: Path) -> None:
    reason = _reason(tree, "!pkg/mod.py")
    assert reason is not None and "exists in the worktree" in reason


def test_oracle_absence_refuses_directory(tree: Path) -> None:
    """A directory occupying the path defeats absence exactly as it
    fails a path claim's `is_file()` — the two polarities refuse the
    same in-between state rather than each calling it their own way."""
    reason = _reason(tree, "!pkg")
    assert reason is not None and "exists in the worktree" in reason


def test_oracle_absence_refuses_escapes_like_presence(tree: Path) -> None:
    """Same containment walk for both polarities: an absence claim on an
    out-of-tree path is refused, not vacuously true."""
    for escape in ("../outside.py", "/etc/passwd", "~/x.py", "a/../../b.py"):
        reason = check_claim(Claim("absent", escape, escape, ""), tree)
        assert reason is not None and "worktree" in reason


# ---------------------------------------------------------------------------
# claim_level_drift — the absent branch only
#
# The three measured kinds stay tested through `tests/test_bench_rot.py`
# (one suite, one copy — see the module docstring). The absent kind is
# the one detector branch the bench corpus never contains, so its tier
# semantics are owned here, on handcrafted -U0 streams.
# ---------------------------------------------------------------------------

_MARK = "\x01"


def _readd_stream(sha: str = "sha1") -> str:
    return (
        f"{_MARK}{sha}\n"
        "diff --git a/pkg/gone.py b/pkg/gone.py\n"
        "--- /dev/null\n"
        "+++ b/pkg/gone.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+GONE_TIMEOUT_SECONDS = 30\n"
        "+GONE_RETRY_LIMIT = 3\n"
    )


def _delete_stream(sha: str = "sha2") -> str:
    return (
        f"{_MARK}{sha}\n"
        "diff --git a/pkg/gone.py b/pkg/gone.py\n"
        "--- a/pkg/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-GONE_TIMEOUT_SECONDS = 30\n"
        "-GONE_RETRY_LIMIT = 3\n"
    )


def test_absent_claim_fires_both_tiers_on_net_reappearance() -> None:
    index = build_binding_index(_readd_stream())
    result = claim_level_drift(parse_claim("!pkg/gone.py"), index)
    assert result["weak"] is True
    assert result["strict"] is True


def test_absent_claim_reads_weak_only_when_window_ends_absent() -> None:
    """Add-then-delete inside one window: the set-based index shows both
    a touch and a deletion; under the declare-time invariant (the window
    STARTS absent) that sequence can only end absent — weak says
    spot-check, strict stays quiet."""
    index = build_binding_index(_readd_stream("sha1") + _delete_stream("sha2"))
    result = claim_level_drift(parse_claim("!pkg/gone.py"), index)
    assert result["weak"] is True
    assert result["strict"] is False


def test_absent_claim_stays_quiet_on_unrelated_churn() -> None:
    stream = (
        f"{_MARK}sha9\n"
        "diff --git a/pkg/other.py b/pkg/other.py\n"
        "--- a/pkg/other.py\n"
        "+++ b/pkg/other.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-OTHER_CONSTANT_VALUE = 1\n"
        "+OTHER_CONSTANT_VALUE = 2\n"
    )
    index = build_binding_index(stream)
    result = claim_level_drift(parse_claim("!pkg/gone.py"), index)
    assert result["weak"] is False
    assert result["strict"] is False


def test_presence_path_claim_unmoved_by_the_absent_branch() -> None:
    """Guard on A-P5's additive-only promise: the same re-add stream
    that fires the absent kind leaves the presence path kind exactly
    where the bench measured it — quiet (nothing was deleted)."""
    index = build_binding_index(_readd_stream())
    result = claim_level_drift(parse_claim("pkg/gone.py"), index)
    assert result["weak"] is False
    assert result["strict"] is False
