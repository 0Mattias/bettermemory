"""Unit tests for the scope-mismatch heuristic."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

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


def test_project_name_sentence_final_period_still_matches() -> None:
    """A name followed by `.` + whitespace (or end-of-string) is
    sentence punctuation, not a token continuation."""
    out = detect_scope_mismatch(
        body="Most of this quarter went into foo. The rest was reviews.",
        declared_scopes=["tools"],
        project_scopes={"projects:foo"},
        project_roots={},
    )
    assert out.has_mismatch is True


def test_project_name_does_not_fire_through_slug_hyphen_in_paths() -> None:
    """Production shape (`project_scopes` populated from the same store
    as `project_roots`): `projects:foo` must not claim a path under the
    sibling tree `foo-bar` via the name pass — the root pass's trailing
    guard already documents that a `-`-continued segment means a
    *different* project, and pass 1 must not shadow it."""
    out = detect_scope_mismatch(
        body="See /Users/me/projects/foo-bar/x.py for details.",
        declared_scopes=["projects:foo-bar"],
        project_scopes={"projects:foo", "projects:foo-bar"},
        project_roots={"projects:foo": "/Users/me/projects/foo"},
    )
    assert out.has_mismatch is False


def test_project_name_does_not_fire_on_sibling_slug_prose() -> None:
    """`api` must not match inside the declared sibling `payments-api`
    — hyphenated sibling repo names are the norm under the scope
    grammar."""
    out = detect_scope_mismatch(
        body="Deploy of payments-api goes through GitHub Actions, not Jenkins.",
        declared_scopes=["projects:payments-api"],
        project_scopes={"projects:api", "projects:payments-api"},
        project_roots={},
    )
    assert out.has_mismatch is False


def test_project_name_matches_snake_case_spelling() -> None:
    """The scope grammar bans `_`, so a repo literally named
    `data_pipeline` can only be tagged `projects:data-pipeline`; the
    name pass must treat `-`, `_`, and `.` as equivalent separators or
    the body's natural spelling never matches."""
    out = detect_scope_mismatch(
        body="The data_pipeline DAG retries failed loads 3 times before paging.",
        declared_scopes=["tools"],
        project_scopes={"projects:data-pipeline"},
        project_roots={},
    )
    assert out.has_mismatch is True
    assert out.suggested_scopes == ("projects:data-pipeline",)


def test_project_name_matches_dotted_spelling() -> None:
    out = detect_scope_mismatch(
        body="The next.js routing decision is documented in the ADR.",
        declared_scopes=["tools"],
        project_scopes={"projects:next-js"},
        project_roots={},
    )
    assert out.has_mismatch is True
    assert out.suggested_scopes == ("projects:next-js",)


def test_project_name_hyphen_spelling_still_matches() -> None:
    out = detect_scope_mismatch(
        body="The data-pipeline DAG retries failed loads 3 times.",
        declared_scopes=["tools"],
        project_scopes={"projects:data-pipeline"},
        project_roots={},
    )
    assert out.has_mismatch is True


def test_common_word_project_names_skipped_in_prose() -> None:
    """Repos named with everyday nouns (docs, notes, scripts) must not
    fire on ordinary prose that merely uses the word."""
    out = detect_scope_mismatch(
        body=(
            "Prefers terse inline docs over long README sections; "
            "keep review notes in the PR description."
        ),
        declared_scopes=["learning-style"],
        project_scopes={"projects:docs", "projects:notes", "projects:scripts"},
        project_roots={},
    )
    assert out.has_mismatch is False


def test_common_word_repo_still_fires_via_project_root() -> None:
    """Coverage for stoplisted repo names survives via the root pass —
    a path under the docs repo's tree is still distinctive."""
    out = detect_scope_mismatch(
        body="The style guide is /Users/me/projects/docs/style.md, keep it terse.",
        declared_scopes=["tools"],
        project_scopes={"projects:docs"},
        project_roots={"projects:docs": "/Users/me/projects/docs"},
    )
    assert out.has_mismatch is True
    assert out.matches[0].kind == "project_root"
    assert "projects:docs" in out.suggested_scopes


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


def test_path_root_followed_by_sentence_period_matches() -> None:
    """A `.` after the root that is followed by whitespace (or ends the
    body) is sentence punctuation, not a path-segment continuation —
    per the trailing guard's own stated intent."""
    # Root + ". " mid-body.
    out_mid = detect_scope_mismatch(
        body=(
            "Everything for that tool lives in /Users/me/projects/foo. "
            "The venv is at the root."
        ),
        declared_scopes=["tools"],
        project_scopes=set(),
        project_roots={"projects:foo": "/Users/me/projects/foo"},
    )
    assert out_mid.has_mismatch is True
    assert "projects:foo" in out_mid.suggested_scopes

    # Root + "." at end-of-string.
    out_end = detect_scope_mismatch(
        body="It all lives under /Users/me/projects/foo.",
        declared_scopes=["tools"],
        project_scopes=set(),
        project_roots={"projects:foo": "/Users/me/projects/foo"},
    )
    assert out_end.has_mismatch is True


def test_path_root_second_occurrence_not_masked_by_first() -> None:
    """A guard-rejected first occurrence (`foo.bak`) must not mask a
    clean later occurrence of the same root."""
    out = detect_scope_mismatch(
        body=(
            "Backup sits at /Users/me/projects/foo.bak while the live "
            "tree is /Users/me/projects/foo/x.py as usual."
        ),
        declared_scopes=["tools"],
        project_scopes=set(),
        project_roots={"projects:foo": "/Users/me/projects/foo"},
    )
    assert out.has_mismatch is True
    assert "projects:foo" in out.suggested_scopes


def test_path_root_matches_tilde_contracted_spelling() -> None:
    """Bodies overwhelmingly cite home paths in `~` form while inferred
    roots are absolute; the root pass must match the contracted
    spelling too. The root's basename diverges from the scope name so
    the name pass cannot rescue the miss."""
    home = str(Path.home())
    root = home + os.sep + "work" + os.sep + "bm-server"
    alias = "~" + root[len(home) :]
    out = detect_scope_mismatch(
        body=f"The audit loop lives under {alias} these days.",
        declared_scopes=["tools"],
        project_scopes=set(),
        project_roots={"projects:bettermemory": root},
    )
    assert out.has_mismatch is True
    assert "projects:bettermemory" in out.suggested_scopes


def test_path_root_tilde_spelling_rejects_slug_sibling() -> None:
    """The tilde alias gets the same boundary guards: `~/work/bm-server`
    must not match inside `~/work/bm-server-old`."""
    home = str(Path.home())
    root = home + os.sep + "work" + os.sep + "bm-server"
    alias = "~" + root[len(home) :]
    out = detect_scope_mismatch(
        body=f"The old checkout at {alias}-old is abandoned.",
        declared_scopes=["tools"],
        project_scopes=set(),
        project_roots={"projects:bettermemory": root},
    )
    assert out.has_mismatch is False


def test_shared_root_explained_by_declared_scope_not_flagged() -> None:
    """origin.cwd attribution can give a foreign scope the declared
    project's directory as its inferred root (facts about homelab
    recorded mid-webapp-session). The declared tag already explains the
    cited path, so the colliding scope must stay quiet."""
    out = detect_scope_mismatch(
        body=(
            "The webapp dev server config is "
            "/Users/me/code/webapp/vite.config.ts; HMR needs port 5174."
        ),
        declared_scopes=["projects:webapp"],
        project_scopes={"projects:homelab", "projects:webapp"},
        project_roots={
            "projects:homelab": "/Users/me/code/webapp",
            "projects:webapp": "/Users/me/code/webapp",
        },
    )
    assert out.has_mismatch is False


def test_monorepo_identical_roots_sibling_quiet_when_declared() -> None:
    """Monorepo sub-projects legitimately share one checkout root; a
    write correctly tagged with one sub-project must not bounce
    demanding the sibling."""
    out = detect_scope_mismatch(
        body=(
            "API auth middleware lives in "
            "/Users/me/work/monorepo/services/api/auth.py now."
        ),
        declared_scopes=["projects:api"],
        project_scopes={"projects:api", "projects:webapp"},
        project_roots={
            "projects:api": "/Users/me/work/monorepo",
            "projects:webapp": "/Users/me/work/monorepo",
        },
    )
    assert out.has_mismatch is False


def test_monorepo_identical_roots_still_flag_when_neither_declared() -> None:
    """Honest ambiguity: with no declared scope explaining the shared
    root, both siblings are suggested."""
    out = detect_scope_mismatch(
        body="Auth middleware lives in /Users/me/work/monorepo/services/auth.py now.",
        declared_scopes=["tools"],
        project_scopes=set(),
        project_roots={
            "projects:api": "/Users/me/work/monorepo",
            "projects:webapp": "/Users/me/work/monorepo",
        },
    )
    assert out.has_mismatch is True
    assert out.suggested_scopes == ("projects:api", "projects:webapp")


def test_nested_roots_child_declared_not_gated_for_parent() -> None:
    """Production shape: a write correctly tagged with the child scope
    citing a path under its own root must not bounce demanding the
    parent — via either pass (the parent's *name* is a segment of every
    child path, and the parent's *root* is a prefix of it)."""
    out = detect_scope_mismatch(
        body=(
            "Auth middleware lives at /Users/me/work/mono/services/api/auth.go; "
            "JWTs are validated there, not in the gateway."
        ),
        declared_scopes=["projects:mono-api"],
        project_scopes={"projects:mono", "projects:mono-api"},
        project_roots={
            "projects:mono": "/Users/me/work/mono",
            "projects:mono-api": "/Users/me/work/mono/services/api",
        },
    )
    assert out.has_mismatch is False


def test_nested_roots_pass2_child_declared_quiet() -> None:
    """Root-pass shape in isolation: the declared child root extends
    the parent root at the same match index, so the parent hit is
    suppressed."""
    out = detect_scope_mismatch(
        body="Auth middleware lives at /Users/me/work/mono/services/api/auth.go now.",
        declared_scopes=["projects:mono-api"],
        project_scopes=set(),
        project_roots={
            "projects:mono": "/Users/me/work/mono",
            "projects:mono-api": "/Users/me/work/mono/services/api",
        },
    )
    assert out.has_mismatch is False


def test_nested_roots_parent_path_outside_child_still_flags() -> None:
    """Control: a path under the parent but *outside* the declared
    child's subtree is genuinely about the parent project."""
    out = detect_scope_mismatch(
        body="The CI config lives at /Users/me/work/mono/.github/workflows/ci.yml.",
        declared_scopes=["projects:mono-api"],
        project_scopes={"projects:mono", "projects:mono-api"},
        project_roots={
            "projects:mono": "/Users/me/work/mono",
            "projects:mono-api": "/Users/me/work/mono/services/api",
        },
    )
    assert out.has_mismatch is True
    assert "projects:mono" in out.suggested_scopes


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


def test_collect_project_roots_skips_degenerate_roots() -> None:
    """A dotfiles-style project worked from `$HOME` (or a stray session
    at `/`) would make its root prefix-match essentially every path the
    user ever cites — such scopes are omitted rather than poisoning the
    gate store-wide."""
    home = str(Path.home())
    a = _memory(scopes=["projects:dotfiles"], cwd=home)
    b = _memory(scopes=["projects:dotfiles"], cwd=home)
    c = _memory(scopes=["projects:rootfs"], cwd="/")
    d = _memory(scopes=["projects:foo"], cwd="/Users/me/projects/foo")
    out = collect_project_roots([a, b, c, d])
    assert out == {"projects:foo": "/Users/me/projects/foo"}


def test_collect_project_roots_empty_when_no_origin() -> None:
    a = _memory(scopes=["projects:foo"])  # no origin
    out = collect_project_roots([a])
    assert out == {}
