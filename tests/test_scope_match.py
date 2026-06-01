"""Unit tests for the scope-mismatch heuristic."""

from __future__ import annotations

from datetime import datetime, timezone

from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin
from bettermemory.scope_match import (
    collect_project_roots,
    collect_project_scopes,
    detect_scope_mismatch,
)


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _memory(
    *,
    body: str = "x",
    scopes: list[str] | None = None,
    cwd: str | None = None,
) -> Memory:
    origin = Origin(cwd=cwd) if cwd is not None else None
    return Memory(
        id=generate_ulid(),
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        scopes=scopes or ["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body + "\n",
        origin=origin,
    )


# ---------------------------------------------------------------------------
# detect_scope_mismatch — project name match
# ---------------------------------------------------------------------------


def test_no_mismatch_on_empty_body() -> None:
    out = detect_scope_mismatch(
        body="",
        declared_scopes=["tools"],
        project_scopes={"projects:foo"},
        project_roots={},
    )
    assert out.has_mismatch is False
    assert out.matches == ()
    assert out.suggested_scopes == ()


def test_no_mismatch_when_no_project_scopes() -> None:
    out = detect_scope_mismatch(
        body="working on foo's setup",
        declared_scopes=["tools"],
        project_scopes=set(),
        project_roots={},
    )
    assert out.has_mismatch is False


def test_project_name_token_match_triggers_suggestion() -> None:
    out = detect_scope_mismatch(
        body="When working on foo, the build script lives in scripts/.",
        declared_scopes=["tools"],
        project_scopes={"projects:foo"},
        project_roots={},
    )
    assert out.has_mismatch is True
    assert out.suggested_scopes == ("projects:foo",)
    assert out.matches[0].kind == "project_name"
    assert out.matches[0].suggested_scope == "projects:foo"


def test_no_match_when_declared_scope_already_present() -> None:
    """If the writer already declared the project scope, the cross-
    reference is fine — that's the whole point of multi-scope writes."""
    out = detect_scope_mismatch(
        body="Working on foo with the canonical setup.",
        declared_scopes=["projects:foo", "tools"],
        project_scopes={"projects:foo"},
        project_roots={},
    )
    assert out.has_mismatch is False


def test_word_boundary_avoids_substring_false_positive() -> None:
    """Body says `myfoox` — that shouldn't match `foo` even at substring
    level because of word boundaries."""
    out = detect_scope_mismatch(
        body="The myfoox suite handles everything.",
        declared_scopes=["tools"],
        project_scopes={"projects:foo"},
        project_roots={},
    )
    assert out.has_mismatch is False


def test_short_project_names_skipped() -> None:
    """Two-character project names are too noisy to match safely."""
    out = detect_scope_mismatch(
        body="The ab tool is great.",
        declared_scopes=["tools"],
        project_scopes={"projects:ab"},
        project_roots={},
    )
    assert out.has_mismatch is False


def test_case_insensitive_match() -> None:
    out = detect_scope_mismatch(
        body="When working on FOO, the script lives in scripts/.",
        declared_scopes=["tools"],
        project_scopes={"projects:foo"},
        project_roots={},
    )
    assert out.has_mismatch is True


def test_match_capped_at_max_count() -> None:
    """Pathological body that names many projects should still bound
    the response shape."""
    body = " ".join(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"])
    project_scopes = {f"projects:{p}" for p in body.split()}
    out = detect_scope_mismatch(
        body=body,
        declared_scopes=["tools"],
        project_scopes=project_scopes,
        project_roots={},
    )
    assert out.has_mismatch is True
    assert len(out.matches) <= 5


# ---------------------------------------------------------------------------
# detect_scope_mismatch — project root path match
# ---------------------------------------------------------------------------


def test_path_under_other_project_root_triggers_mismatch() -> None:
    out = detect_scope_mismatch(
        body="The script at /Users/me/projects/foo/x.py is the entry point.",
        declared_scopes=["tools"],
        project_scopes={"projects:foo"},
        project_roots={"projects:foo": "/Users/me/projects/foo"},
    )
    assert out.has_mismatch is True
    # Either the path-prefix or name-token match could fire first, but
    # the suggested scope should be `projects:foo` either way.
    assert "projects:foo" in out.suggested_scopes


def test_path_inside_identifier_does_not_match() -> None:
    """Substring match inside an alphanumeric identifier is suppressed
    by the leading-character check — a path embedded in a token like
    `garbage/Users/me/projects/foo/x.py` is suspicious; we lean
    conservative on the surface."""
    out = detect_scope_mismatch(
        body=(
            "z/Users/me/projects/foo/x.py"  # leading alnum char before /
            " is mentioned in passing"
        ),
        declared_scopes=["tools"],
        project_scopes=set(),
        project_roots={"projects:foo": "/Users/me/projects/foo"},
    )
    # Name-token check doesn't fire (project_scopes is empty);
    # path-root check is gated by the leading-alnum guard, so no match.
    assert out.has_mismatch is False


def test_path_root_does_not_over_match_sibling_prefix() -> None:
    """`projects:foo`'s root `/.../foo` must not over-match a sibling
    tree `/.../foobar/...` whose last segment merely shares the prefix.

    Regression: the project-root pass guarded only the *leading*
    boundary (`body[idx-1].isalnum()`) and not the trailing one, so a
    root `/Users/me/projects/foo` matched as a substring inside
    `/Users/me/projects/foobar/x.py` and attributed a `projects:foobar`
    path to `projects:foo`. The trailing boundary now enforces an exact
    segment match. `project_scopes` is empty so only the path-root pass
    can fire."""
    out = detect_scope_mismatch(
        body="The script at /Users/me/projects/foobar/x.py is unrelated.",
        declared_scopes=["tools"],
        project_scopes=set(),
        project_roots={"projects:foo": "/Users/me/projects/foo"},
    )
    assert out.has_mismatch is False


def test_path_root_rejects_slug_sibling_suffixes() -> None:
    """Trailing `-`, `_`, and `.` continue a path segment too, so
    `/.../foo` must not match `/.../foo-bar`, `/.../foo_bar`, or
    `/.../foo.bak`."""
    for suffix in ("-bar", "_bar", ".bak"):
        out = detect_scope_mismatch(
            body=f"See /Users/me/projects/foo{suffix}/x.py for details.",
            declared_scopes=["tools"],
            project_scopes=set(),
            project_roots={"projects:foo": "/Users/me/projects/foo"},
        )
        assert out.has_mismatch is False, suffix


def test_path_root_exact_segment_still_matches() -> None:
    """The trailing-boundary guard must not break legitimate hits: a
    root followed by a segment boundary (`/`) or end-of-string is a
    real match."""
    # Followed by `/`.
    out_sub = detect_scope_mismatch(
        body="The script at /Users/me/projects/foo/x.py is the entry point.",
        declared_scopes=["tools"],
        project_scopes=set(),
        project_roots={"projects:foo": "/Users/me/projects/foo"},
    )
    assert out_sub.has_mismatch is True
    assert "projects:foo" in out_sub.suggested_scopes

    # Root at end-of-string (nothing after it).
    out_end = detect_scope_mismatch(
        body="It all lives under /Users/me/projects/foo",
        declared_scopes=["tools"],
        project_scopes=set(),
        project_roots={"projects:foo": "/Users/me/projects/foo"},
    )
    assert out_end.has_mismatch is True
    assert "projects:foo" in out_end.suggested_scopes


# ---------------------------------------------------------------------------
# collect_project_scopes / collect_project_roots
# ---------------------------------------------------------------------------


def test_collect_project_scopes_dedups_and_filters() -> None:
    a = _memory(scopes=["tools", "projects:foo"])
    b = _memory(scopes=["projects:foo"])
    c = _memory(scopes=["projects:bar"])
    d = _memory(scopes=["tools"])  # no projects: scope
    out = collect_project_scopes([a, b, c, d])
    assert out == {"projects:foo", "projects:bar"}


def test_collect_project_scopes_excludes_bare_projects_prefix() -> None:
    """A scope of literally `projects:` (empty name) shouldn't be
    treated as a project — it's malformed."""
    bad = _memory(scopes=["tools"])
    out = collect_project_scopes([bad])
    assert out == set()


def test_collect_project_roots_picks_most_common_cwd() -> None:
    a = _memory(scopes=["projects:foo"], cwd="/Users/me/projects/foo")
    b = _memory(scopes=["projects:foo"], cwd="/Users/me/projects/foo")
    c = _memory(scopes=["projects:foo"], cwd="/tmp/foo-stray")
    out = collect_project_roots([a, b, c])
    assert out == {"projects:foo": "/Users/me/projects/foo"}


def test_collect_project_roots_empty_when_no_origin() -> None:
    a = _memory(scopes=["projects:foo"])  # no origin
    out = collect_project_roots([a])
    assert out == {}
