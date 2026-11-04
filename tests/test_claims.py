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

import shutil
import subprocess
from pathlib import Path

import pytest

from bettermemory.claims import (
    MAX_CLAIMS,
    Claim,
    build_binding_index,
    check_claim,
    claim_is_addressable,
    governed_claim_paths,
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


@pytest.mark.parametrize("control", ["\n", "\r", "\t", "\x01", "\x1f", "\x7f"])
@pytest.mark.parametrize("shape", ["{}", "{}::handler", "{}::TIMEOUT=30", "!{}"])
def test_a_declared_path_holding_a_control_character_is_refused(
    control: str, shape: str
) -> None:
    """git quotes a name holding a C0 control or DEL in every patch it
    prints, so claim drift cannot read a claim on it (ENG-220's re-review):
    declaration refuses it, naming the character. A claim stored before is
    still loaded, so the drift readers see it and decline."""
    raw = shape.format(f"src/a{control}b.py")
    with pytest.raises(
        ValueError, match=f"control character \\(U\\+{ord(control):04X}"
    ):
        parse_claims([raw])
    assert [claim.rel_path for claim in load_claims([raw])] == [f"src/a{control}b.py"]


@pytest.mark.parametrize("surrogate", ["\ud800", "\udbff", "\udc80", "\udfff"])
@pytest.mark.parametrize("shape", ["{}", "{}::handler", "!{}"])
def test_a_declared_path_holding_a_lone_surrogate_is_refused(
    surrogate: str, shape: str
) -> None:
    """A lone surrogate spells no character, and no file name git stores
    as UTF-8 holds one, so claim drift cannot ask git for it (ENG-220's
    review, round D): declaration refuses it, naming the code point. A
    claim stored before is still loaded, and the drift readers decline."""
    raw = shape.format(f"src/a{surrogate}b.py")
    with pytest.raises(ValueError, match=f"lone surrogate \\(U\\+{ord(surrogate):04X}"):
        parse_claims([raw])
    assert [claim.rel_path for claim in load_claims([raw])] == [f"src/a{surrogate}b.py"]


def test_a_path_with_no_control_character_is_declared() -> None:
    """A space, a quote, a non-ASCII letter and a C1 control (which git
    prints as it is under ``core.quotePath=false``) are not refused."""
    for raw in ("src/a b.py", 'src/we"ird.py', "src/café.py", "src/a\x85b.py"):
        assert parse_claims([raw])[0].rel_path == raw


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
#: The full hashes the streams' marks carry, as ``%H`` prints them: the
#: parser reads no other mark.
_H1, _H2, _H3, _H9 = ("1" * 40, "2" * 40, "3" * 40, "9" * 40)


def _readd_stream(sha: str = _H1) -> str:
    return (
        f"{_MARK}{sha}\n"
        "diff --git a/pkg/gone.py b/pkg/gone.py\n"
        "--- /dev/null\n"
        "+++ b/pkg/gone.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+GONE_TIMEOUT_SECONDS = 30\n"
        "+GONE_RETRY_LIMIT = 3\n"
    )


def _delete_stream(sha: str = _H2) -> str:
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
    index = build_binding_index(_readd_stream(_H1) + _delete_stream(_H2))
    result = claim_level_drift(parse_claim("!pkg/gone.py"), index)
    assert result["weak"] is True
    assert result["strict"] is False


def test_absent_claim_stays_quiet_on_unrelated_churn() -> None:
    stream = (
        f"{_MARK}{_H9}\n"
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


def test_a_no_newline_marker_mid_hunk_is_not_a_hunk_line() -> None:
    """An edit of a last line without a newline: git prints ``\\ No
    newline at end of file`` after the removed line, inside the hunk, and
    again after the added one. Counted as a hunk line, the first ended the
    hunk before the added line, a mismatch that sent the whole window to
    the per-file count, and the added binding was never indexed."""
    stream = (
        f"{_MARK}{_H1}\n"
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "--- a/pkg/mod.py\n"
        "+++ b/pkg/mod.py\n"
        "@@ -2 +2 @@\n"
        "-TIMEOUT_SECONDS = 30\n"
        "\\ No newline at end of file\n"
        "+TIMEOUT_SECONDS = 45\n"
        "\\ No newline at end of file\n"
    )
    index = build_binding_index(stream)
    assert (index["hunks"], index["parse_mismatches"]) == (1, 0)
    entry = index["bindings"][("pkg/mod.py", "assign", "TIMEOUT_SECONDS")]
    assert (entry["removes"], entry["adds"]) == (1, 1)
    assert entry["rhs_added"] == {"45"}
    result = claim_level_drift(parse_claim("pkg/mod.py::TIMEOUT_SECONDS=30"), index)
    assert (result["weak"], result["strict"]) == (True, True)


@pytest.mark.parametrize(
    "mark", ["sha1", "f" * 39, "F" * 40, "f" * 41, "f" * 40 + " x", ""]
)
def test_a_mark_without_a_full_hash_is_a_mismatch(mark: str) -> None:
    """A record mark carries the full hash ``%H`` prints; any other is
    counted as a mismatch, which sends the window to the per-file count,
    and no hunk after it is given to the commit it names (ENG-220's
    re-review)."""
    stream = (
        f"{_MARK}{_H1}\n"
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "--- a/pkg/mod.py\n"
        "+++ b/pkg/mod.py\n"
        "@@ -1 +1 @@\n"
        "-TIMEOUT_SECONDS = 30\n"
        "+TIMEOUT_SECONDS = 45\n"
        f"{_MARK}{mark}\n"
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "--- a/pkg/mod.py\n"
        "+++ b/pkg/mod.py\n"
        "@@ -1 +1 @@\n"
        "-TIMEOUT_SECONDS = 45\n"
        "+TIMEOUT_SECONDS = 60\n"
    )
    index = build_binding_index(stream)
    assert index["parse_mismatches"] == 1
    assert index["commits"] == 1
    entry = index["bindings"][("pkg/mod.py", "assign", "TIMEOUT_SECONDS")]
    assert entry["commits"] == {_H1}


def test_a_sha256_mark_is_read() -> None:
    stream = f"{_MARK}{'a' * 64}\n" + _EDIT_BESIDE.split("\n", 1)[1]
    index = build_binding_index(stream)
    assert (index["parse_mismatches"], index["commits"]) == (0, 1)


def _header_only(sha: str, path: str, *extended: str) -> str:
    """One commit's section whose header alone says `path` changed: no
    ``---``/``+++`` line and no hunk, as git prints an empty file's
    deletion or creation, or a mode change."""
    return f"{_MARK}{sha}\ndiff --git a/{path} b/{path}\n" + "".join(
        f"{line}\n" for line in extended
    )


_EDIT_BESIDE = (
    f"{_MARK}{_H2}\n"
    "diff --git a/pkg/other.py b/pkg/other.py\n"
    "--- a/pkg/other.py\n"
    "+++ b/pkg/other.py\n"
    "@@ -1 +1 @@\n"
    "-OTHER_CONSTANT_VALUE = 1\n"
    "+OTHER_CONSTANT_VALUE = 2\n"
)


def test_an_empty_files_deletion_fires_its_path_claim() -> None:
    """Git prints an empty file's deletion as its header alone, no
    ``---``/``+++`` line and no hunk. Beside another file's edit, the
    parser indexed the other file and not the deletion, so the path claim
    read untouched: a changed claim reading fresh. The header's path is
    deleted now, and the commit named for it."""
    stream = (
        _header_only(
            _H1,
            "pkg/empty.py",
            "deleted file mode 100644",
            "index e69de29..0000000",
        )
        + _EDIT_BESIDE
    )
    index = build_binding_index(stream)
    assert "pkg/empty.py" in index["deleted"] and "pkg/empty.py" in index["files"]
    assert index["header_only"] == {"pkg/empty.py": {_H1}}
    assert index["parse_mismatches"] == 0
    result = claim_level_drift(parse_claim("pkg/empty.py"), index)
    assert (result["weak"], result["strict"]) == (True, True)


def test_an_empty_files_creation_fires_its_absence_claim() -> None:
    stream = (
        _header_only(
            _H1, "pkg/gone.py", "new file mode 100644", "index 0000000..e69de29"
        )
        + _EDIT_BESIDE
    )
    index = build_binding_index(stream)
    assert "pkg/gone.py" in index["files"] and "pkg/gone.py" not in index["deleted"]
    assert index["header_only"] == {"pkg/gone.py": {_H1}}
    result = claim_level_drift(parse_claim("!pkg/gone.py"), index)
    assert (result["weak"], result["strict"]) == (True, True)


def test_a_mode_change_touches_the_file_and_fires_no_claim() -> None:
    """A mode change alone is printed as its header: the file is touched,
    so the stream is no longer read as naming no file, and no claim kind
    reads a mode, so none fires."""
    index = build_binding_index(
        _header_only(_H1, "pkg/mod.py", "old mode 100644", "new mode 100755")
    )
    assert index["files"] == {"pkg/mod.py"} and not index["deleted"]
    assert index["header_only"] == {"pkg/mod.py": {_H1}}
    for spec in ("pkg/mod.py", "pkg/mod.py::handler", "pkg/mod.py::TIMEOUT=30"):
        result = claim_level_drift(parse_claim(spec), index)
        assert (result["weak"], result["strict"]) == (False, False), spec


def test_a_whole_rename_deletes_its_source_and_touches_its_target() -> None:
    """The streams claim drift reads print no renames; one that does is
    read all the same: a rename's source is gone, its target touched."""
    stream = (
        f"{_MARK}{_H1}\n"
        "diff --git a/pkg/old.py b/pkg/new.py\n"
        "similarity index 100%\n"
        "rename from pkg/old.py\n"
        "rename to pkg/new.py\n"
    )
    index = build_binding_index(stream)
    assert index["deleted"] == {"pkg/old.py"}
    assert index["files"] == {"pkg/old.py", "pkg/new.py"}
    assert index["header_only"] == {"pkg/old.py": {_H1}, "pkg/new.py": {_H1}}


def test_a_section_with_lines_is_not_header_only() -> None:
    """A deletion that prints its lines names its commit through them, as
    before: its header touches the path, and `header_only` stays empty."""
    stream = (
        f"{_MARK}{_H1}\n"
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "deleted file mode 100644\n"
        "index 1234567..0000000\n"
        "--- a/pkg/mod.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-TIMEOUT_SECONDS_VALUE = 30\n"
    )
    index = build_binding_index(stream)
    assert index["deleted"] == {"pkg/mod.py"}
    assert index["header_only"] == {}
    assert index["changed_text"]["pkg/mod.py"] == {"TIMEOUT_SECONDS_VALUE = 30": {_H1}}


def test_a_quoted_header_alone_is_read_unquoted() -> None:
    """Git quotes a path holding a double quote even with quotePath off,
    on the ``diff --git`` line as on ``---``/``+++``; the producer unquotes
    both, so an empty file's deletion there is read under its own path."""
    from bettermemory.origin import _dequote_patch_headers

    raw = (
        f"{_MARK}{_H1}\n"
        'diff --git "a/pkg/we\\"ird.py" "b/pkg/we\\"ird.py"\n'
        "deleted file mode 100644\n"
        "index e69de29..0000000\n"
        f"{_MARK}{_H3}\n"
        'diff --git "a/pkg/a\\"b.py" b/pkg/c.py\n'
        "similarity index 100%\n"
        'rename from "pkg/a\\"b.py"\n'
        "rename to pkg/c.py\n"
    ) + _EDIT_BESIDE
    index = build_binding_index(_dequote_patch_headers(raw))
    assert index["deleted"] == {'pkg/we"ird.py', 'pkg/a"b.py'}
    assert claim_level_drift(parse_claim('pkg/we"ird.py'), index)["weak"] is True


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_a_patch_of_a_last_line_without_a_newline_parses_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same, from git's own patch stream."""
    from bettermemory import origin

    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = A\n\temail = a@a\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        return done.stdout.decode().strip()

    git("init", "-q")
    for value in (30, 45):
        (repo / "mod.py").write_bytes(f"import os\nTIMEOUT_SECONDS = {value}".encode())
        git("add", "mod.py")
        git("commit", "-q", "-m", f"c{value}")
    stream = origin.commit_patch_stream(repo, [git("rev-parse", "HEAD")], ["mod.py"])
    assert stream is not None and "\\ No newline at end of file" in stream
    index = build_binding_index(stream)
    assert (index["hunks"], index["parse_mismatches"]) == (1, 0)
    assert index["bindings"][("mod.py", "assign", "TIMEOUT_SECONDS")]["adds"] == 1


def test_presence_path_claim_unmoved_by_the_absent_branch() -> None:
    """Guard on A-P5's additive-only promise: the same re-add stream
    that fires the absent kind leaves the presence path kind exactly
    where the bench measured it — quiet (nothing was deleted)."""
    index = build_binding_index(_readd_stream())
    result = claim_level_drift(parse_claim("pkg/gone.py"), index)
    assert result["weak"] is False
    assert result["strict"] is False


# ---------------------------------------------------------------------------
# Addressability — which claims may narrow their file
# ---------------------------------------------------------------------------


def test_container_literals_are_not_addressable() -> None:
    """A container's source may span physical lines, and every literal
    route in `claim_level_drift` is blind to an interior one. Declaring
    such a claim must therefore not take its file out of the any-touch
    leg — the predicate that decides is asserted directly so the reason
    survives a refactor of its caller."""
    for value in ('{"a": 1}', "[1, 2]", "{1, 2}", "(1, 2)"):
        claim = parse_claim(f"pkg/mod.py::NAME={value}")
        assert claim_is_addressable(claim) is False, value


def test_scalars_and_strings_stay_addressable() -> None:
    """The narrowing survives for every value that cannot hide from the
    binding line. A scalar has nowhere to live but the assignment
    itself; a string — even a multi-line one — is what
    `anchors_from_value` and `string_fragment` were built to address."""
    for value in ("30", "30.0", "True", "None", '"hi"', '"""a\nb"""'):
        claim = parse_claim(f"pkg/mod.py::NAME={value}")
        assert claim_is_addressable(claim) is True, value


def test_non_literal_kinds_are_addressable() -> None:
    """Path, symbol and absence claims are untouched by the split —
    their bindings are column-0 addressable or path-level."""
    for spec in ("pkg/mod.py", "pkg/mod.py::handler", "!pkg/gone.py"):
        assert claim_is_addressable(parse_claim(spec)) is True, spec


def test_one_unaddressable_claim_ungoverns_the_whole_path() -> None:
    """Governance is per-FILE, because the narrowing promise is
    per-file: "commits escalate only when a claimed binding is
    implicated." That promise cannot be kept for a file holding a
    binding nothing can implicate, so one unaddressable claim
    disqualifies the path even when an addressable claim sits beside
    it. Other files in the same declaration keep their narrowing."""
    claims = [
        parse_claim("pkg/mod.py::handler"),
        parse_claim('pkg/mod.py::LIMITS={"a": 1}'),
        parse_claim("pkg/other.py::TIMEOUT=30"),
    ]
    assert claim_paths(claims) == ["pkg/mod.py", "pkg/other.py"]
    assert governed_claim_paths(claims) == ["pkg/other.py"]


# ---------------------------------------------------------------------------
# A stat that did not answer is not evidence
# ---------------------------------------------------------------------------


def _unreadable_dir_is_enforceable() -> bool:
    """True when this process can actually be locked out of a directory."""
    import os
    import sys

    if sys.platform == "win32":
        return False
    getuid = getattr(os, "geteuid", None)
    return getuid is not None and getuid() != 0


@pytest.mark.skipif(
    not _unreadable_dir_is_enforceable(),
    reason="needs POSIX mode bits and a non-root euid",
)
def test_absence_claim_is_not_affirmed_by_a_stat_that_failed(tmp_path: Path) -> None:
    """The absence branch AFFIRMS its claim on the negative arm, so a
    stat this process could not complete must never reach it.

    `Path.exists()` cannot express "could not tell" and gets it wrong in
    both directions by interpreter: it re-raises EACCES on 3.11-3.13 and
    folds it into the same False as "nothing is there" on 3.14. On 3.14
    that made a deleted-then-restored file under a directory that later
    became unreadable keep its `!path` claim affirmed while the file sat
    on disk — a false clean on the trust path, not a missed alarm."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "legacy").mkdir()
    target = tmp_path / "src" / "legacy" / "old_api.py"
    claim = parse_claim("!src/legacy/old_api.py")

    # Genuinely absent: the claim holds, and must keep holding.
    assert check_claim(claim, tmp_path) is None

    # The file comes back — a revert, a cherry-pick, a restored copy.
    target.write_text("def old_api(): pass\n")
    assert "exists in the worktree" in (check_claim(claim, tmp_path) or "")

    # ...and its directory then becomes unreadable. Still not affirmed.
    (tmp_path / "src" / "legacy").chmod(0o000)
    try:
        reason = check_claim(claim, tmp_path)
    finally:
        (tmp_path / "src" / "legacy").chmod(0o755)
    assert reason is not None
    assert "could not be read" in reason


@pytest.mark.skipif(
    not _unreadable_dir_is_enforceable(),
    reason="needs POSIX mode bits and a non-root euid",
)
def test_positive_claims_report_unreadable_rather_than_absent(tmp_path: Path) -> None:
    """The other polarity's two failure modes, both wrong, both closed.

    On 3.11-3.13 `is_file()` re-raised straight out of `check_claim`,
    through `_refuse_stale_stored_claims` and `_validate_declared_claims`
    — so a claims-carrying `memory_verify` died. On 3.14 it answered
    False and the caller published "does not exist in the worktree",
    which is an accusation about a tree nothing had read. Neither is an
    answer; both now report what actually happened."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def handler(): pass\n")

    for spec in ("src/mod.py", "src/mod.py::handler"):
        claim = parse_claim(spec)
        assert check_claim(claim, tmp_path) is None, spec
        (tmp_path / "src").chmod(0o000)
        try:
            reason = check_claim(claim, tmp_path)
        finally:
            (tmp_path / "src").chmod(0o755)
        assert reason is not None, spec
        assert "could not be read" in reason, spec
        assert "does not exist" not in reason, spec


def test_a_claim_follows_symlinks_because_resolution_does(tmp_path: Path) -> None:
    """Pins the semantics `_resolve_claim_path` has always had, now that
    `_occupancy` sits behind it: the claim addresses the path the tree
    RESOLVES to, not the link that points at it. So a dangling link
    inside the worktree reads as absent on both polarities — the
    absence claim holds, and the path claim fails for the same reason.
    Asserted because the three-valued probe would otherwise look like a
    place where lstat could change this, and it cannot: resolution has
    already followed the link before the probe runs."""
    import os
    import sys

    if sys.platform == "win32":
        pytest.skip("symlink semantics differ on Windows; POSIX-only test")

    (tmp_path / "src").mkdir()
    os.symlink(tmp_path / "src" / "nowhere.py", tmp_path / "src" / "link.py")

    assert check_claim(parse_claim("!src/link.py"), tmp_path) is None
    assert "does not exist" in (check_claim(parse_claim("src/link.py"), tmp_path) or "")


# ---------------------------------------------------------------------------
# The three answers a claim check gives are two, to a caller that only
# sees "non-None": every could-not-read reason carries the marker a
# consumer branches on, and no contradiction does.
# ---------------------------------------------------------------------------


def test_a_contradiction_is_never_reported_as_indeterminate(tmp_path: Path) -> None:
    from bettermemory.claims import claim_reason_is_indeterminate

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("TIMEOUT = 30\n")
    for spec in (
        "src/missing.py",
        "src/mod.py::absent_symbol",
        "src/mod.py::TIMEOUT=60",
    ):
        reason = check_claim(parse_claim(spec), tmp_path)
        assert reason is not None, spec
        assert not claim_reason_is_indeterminate(reason), (spec, reason)
    assert check_claim(parse_claim("!src/mod.py"), tmp_path) is not None
    assert not claim_reason_is_indeterminate(
        check_claim(parse_claim("!src/mod.py"), tmp_path) or ""
    )


@pytest.mark.skipif(
    not _unreadable_dir_is_enforceable(),
    reason="needs POSIX mode bits and a non-root euid",
)
def test_every_could_not_read_reason_carries_the_marker(tmp_path: Path) -> None:
    """Both polarities of the stat that did not answer, and the read
    that failed after the stat did: each is the marker, by name, so the
    verify refusal and the restore strip can tell "the tree contradicts
    this" from "the tree could not be read for this"."""
    from bettermemory.claims import claim_reason_is_indeterminate

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def handler(): pass\n")
    (tmp_path / "src").chmod(0o000)
    try:
        for spec in ("!src/mod.py", "src/mod.py", "src/mod.py::handler"):
            reason = check_claim(parse_claim(spec), tmp_path)
            assert reason is not None, spec
            assert claim_reason_is_indeterminate(reason), (spec, reason)
    finally:
        (tmp_path / "src").chmod(0o755)
    # The third site: the stat answered, the read did not.
    unreadable = tmp_path / "src" / "sealed.py"
    unreadable.write_text("def handler(): pass\n")
    unreadable.chmod(0o000)
    try:
        reason = check_claim(parse_claim("src/sealed.py::handler"), tmp_path)
        assert reason is not None
        assert claim_reason_is_indeterminate(reason), reason
    finally:
        unreadable.chmod(0o644)
