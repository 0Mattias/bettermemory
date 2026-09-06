"""Unit tests for the scope-mismatch heuristic."""

from __future__ import annotations

import ntpath
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin
from bettermemory.scope_match import (
    _home_alias,
    collect_project_roots,
    collect_project_scopes,
    detect_scope_mismatch,
)
from bettermemory.verify import _home_ignores_case


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
# _home_alias — separator folding (Windows spellings)
#
# Sibling of the gap `verify._is_under_home` shed: `_home_alias` used to
# compare raw strings against `home + os.sep`, so a forward-slash or
# mixed Windows spelling of a home-rooted project root read as NOT
# home-rooted and the tilde-alias search was silently skipped. Closed by
# folding both comparands through `verify._fold_altsep` before the
# prefix check. Windows semantics are exercised from any platform via
# explicit `ntpath` values (`_simulate_windows_home`); on a real Windows
# runner the same monkeypatches are identity writes.
# ---------------------------------------------------------------------------


def _simulate_windows_home(monkeypatch: pytest.MonkeyPatch, home: str) -> None:
    """Pin `_home_alias`'s inputs to explicit ntpath semantics.

    Same shape as the helper of this name in `tests/test_verify.py`,
    kept local because test modules here do not import from each other:
    `os.sep` / `os.altsep` become the `ntpath` constants — identity
    writes on a real Windows runner, the simulation everywhere else —
    and the home env vars point `Path.home()` at `home` in its Windows
    spelling: HOME for the POSIX `expanduser`, USERPROFILE for the
    Windows one, with HOMEDRIVE/HOMEPATH cleared so USERPROFILE wins.
    """
    monkeypatch.setattr(os, "sep", ntpath.sep)
    monkeypatch.setattr(os, "altsep", ntpath.altsep)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)


def test_home_alias_recognises_forward_slash_windows_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deferred sibling itself: `C:/Users/me/work` is a spelling
    Windows accepts for a home-rooted root, and the raw `home + os.sep`
    prefix check misread it as not home-rooted — returning None where
    the backslash twin contracted. All spellings of one path must
    contract alike; the tail keeps the root's own spelling."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-user")
    # Backslash-canonical root — the pre-existing contract.
    assert _home_alias(r"C:\Users\bm-user\work\bm-server") == r"~\work\bm-server"
    # Forward-slash spelling of the SAME root: the closed gap.
    assert _home_alias("C:/Users/bm-user/work/bm-server") == "~/work/bm-server"
    # Mixed spelling — USERPROFILE's backslashes + a forward-slash tail.
    assert _home_alias(r"C:\Users\bm-user/work") == "~/work"
    # Home itself has no tail to contract — None, as before the fold.
    assert _home_alias("C:/Users/bm-user") is None
    # NOT home-rooted under any separator spelling: the fold must not
    # widen the alias to merely drive-lettered paths or sibling homes.
    assert _home_alias("D:/data/project") is None
    assert _home_alias("C:/Users/bm-other/project") is None


def test_home_alias_folds_the_home_spelling_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both comparands fold: some Windows setups carry a forward-slash
    USERPROFILE, and a backslash-spelled root under it is the same
    path. (On a real Windows runner `Path.home()` renders
    backslash-canonical whatever the env says; the fold is what makes
    the POSIX simulation and the live leg agree.)"""
    _simulate_windows_home(monkeypatch, "C:/Users/bm-user")
    assert _home_alias(r"C:\Users\bm-user\project") == r"~\project"
    assert _home_alias("C:/Users/bm-user/project") == "~/project"
    assert _home_alias(r"D:\data\project") is None


def test_home_alias_posix_backslash_stays_a_filename_character(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On POSIX `os.altsep` is None and the fold must be the identity:
    `home` + backslash + `child` names a FILE whose name contains a
    backslash, not a subdirectory, so it must NOT contract. Gated on
    the live `os.altsep` because under ntpath semantics the same
    spelling really is a separator run (pinned above). Guards against
    an over-eager fold that rewrites both characters unconditionally."""
    if os.altsep is not None:
        pytest.skip("this platform has an altsep; the fold is MEANT to fire here")
    home = tmp_path / "bm-posix-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert _home_alias(str(home) + os.sep + "child") == "~" + os.sep + "child"
    assert _home_alias(str(home) + "\\child") is None


def test_forward_slash_root_matches_tilde_citation_under_ntpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end reach of the fold through `detect_scope_mismatch`: a
    stored root in forward-slash Windows spelling never appears
    verbatim in a body that cites the path in `~` form, so the alias is
    the ONLY route to the match — pre-fold, the root was misread as not
    home-rooted and the citation sailed through unflagged. The
    D:-rooted control pins that folding widens nothing."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-user")
    out = detect_scope_mismatch(
        body="The audit loop lives under ~/work/bm-server these days.",
        declared_scopes=["tools"],
        project_scopes=set(),
        project_roots={
            "projects:bettermemory": "C:/Users/bm-user/work/bm-server",
            "projects:other": "D:/work/bm-server",
        },
    )
    assert out.has_mismatch is True
    assert out.suggested_scopes == ("projects:bettermemory",)
    assert out.matches[0].kind == "project_root"


# ---------------------------------------------------------------------------
# Separator folding in the NOISE direction — the raw-comparison siblings of
# the `_home_alias` fold above. `_home_alias`'s raw comparison failed toward
# silent misses; these failed toward false positives: the shared-root
# identity check (`root in declared_roots`) and `_declared_root_covers`'s
# prefix check + body probe all compared spellings byte-for-byte, so a store
# mixing separator families for one directory lost the monorepo and
# nested-root suppressions and bounced correctly-tagged writes. Windows
# semantics exercised from any platform via `_simulate_windows_home`
# (identity writes on a real Windows runner).
# ---------------------------------------------------------------------------


def test_shared_root_other_separator_family_quiet_when_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The monorepo/shared-root suppression must survive the two scopes
    recording the SAME directory in different separator families (a
    hand-edited or cross-machine-synced store; `origin.capture` records
    cwd as ``str(Path.cwd().resolve())``, i.e. OS-native). Byte-equality
    membership missed the collision and bounced the correctly-tagged
    write demanding the foreign scope."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-user")
    out = detect_scope_mismatch(
        body=(
            "The webapp dev server config is "
            "C:/Users/bm-user/code/webapp/vite.config.ts; HMR needs port 5174."
        ),
        declared_scopes=["projects:webapp"],
        project_scopes={"projects:homelab", "projects:webapp"},
        project_roots={
            "projects:homelab": "C:/Users/bm-user/code/webapp",
            "projects:webapp": r"C:\Users\bm-user\code\webapp",
        },
    )
    assert out.has_mismatch is False


def test_nested_roots_other_separator_family_child_declared_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root-pass shape in isolation, mixed spellings: the declared child
    root (forward-slash family) extends the parent root (backslash
    family) at the same match index, so the parent hit is suppressed.
    Raw `startswith` on both the stored-roots prefix check and the body
    probe read the child as unrelated and flagged the parent on a
    correctly-tagged child write."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-user")
    out = detect_scope_mismatch(
        body=(
            r"Auth middleware lives at C:\Users\bm-user\work\mono\services\api\auth.go"
            " now."
        ),
        declared_scopes=["projects:mono-api"],
        project_scopes=set(),
        project_roots={
            "projects:mono": r"C:\Users\bm-user\work\mono",
            "projects:mono-api": "C:/Users/bm-user/work/mono/services/api",
        },
    )
    assert out.has_mismatch is False


def test_nested_roots_tilde_citation_other_family_child_declared_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contracted leg of the covers probe: the body cites the child
    path via the parent root's tilde alias (which keeps the parent's
    backslash tail spelling), while the declared child root is stored
    in the forward-slash family. The folded probe recognises the
    citation as the declared child and stays quiet."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-user")
    out = detect_scope_mismatch(
        body=r"Auth middleware lives at ~\work\mono\services\api\auth.go now.",
        declared_scopes=["projects:mono-api"],
        project_scopes=set(),
        project_roots={
            "projects:mono": r"C:\Users\bm-user\work\mono",
            "projects:mono-api": "C:/Users/bm-user/work/mono/services/api",
        },
    )
    assert out.has_mismatch is False


def test_nested_roots_other_family_parent_path_outside_child_still_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: folding must not over-suppress. A path under the parent
    but *outside* the declared child's subtree is genuinely about the
    parent project, whatever the separator families involved."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-user")
    out = detect_scope_mismatch(
        body=r"The CI config lives at C:\Users\bm-user\work\mono\ci\pipeline.yml today.",
        declared_scopes=["projects:mono-api"],
        project_scopes=set(),
        project_roots={
            "projects:mono": r"C:\Users\bm-user\work\mono",
            "projects:mono-api": "C:/Users/bm-user/work/mono/services/api",
        },
    )
    assert out.has_mismatch is True
    assert out.matches[0].kind == "project_root"
    assert "projects:mono" in out.suggested_scopes


# ---------------------------------------------------------------------------
# Pass-1 span suppression — separator folding of the body search itself.
#
# The comparison-boundary folds above cannot reach `_declared_root_spans`:
# it SEARCHES the body, so a raw `body.find` of each declared root's
# verbatim spelling (plus its single-spelling tilde alias) yielded no span
# when the body cited the root in the other separator family — and the
# parent project's name token, a segment of every child path, was flagged
# on a correctly-tagged child write even though pass 2 stayed quiet. The
# span search now runs over the folded body with folded needles; the fold
# is length-preserving, so spans stay in original body coordinates.
# Windows semantics exercised from any platform via `_simulate_windows_home`
# (identity writes on a real Windows runner).
# ---------------------------------------------------------------------------


def test_pass1_token_suppressed_when_declared_child_root_other_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass-1 twin of the covers fold: the declared child root is
    stored in the forward-slash family while the body cites the child
    path in backslashes. Raw `body.find` yielded no span, so the
    parent's name token was flagged on a correctly-tagged child write
    even though the pass-2 root hit was already suppressed. The folded
    span search recognises the citation."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-user")
    out = detect_scope_mismatch(
        body=(
            r"Auth middleware lives at C:\Users\bm-user\work\mono\services\api\auth.go"
            " now."
        ),
        declared_scopes=["projects:mono-api"],
        project_scopes={"projects:mono", "projects:mono-api"},
        project_roots={
            "projects:mono": r"C:\Users\bm-user\work\mono",
            "projects:mono-api": "C:/Users/bm-user/work/mono/services/api",
        },
    )
    assert out.has_mismatch is False


def test_pass1_token_suppressed_via_tilde_alias_other_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alias needle of the folded span search: the body cites the child
    tree through the home alias in backslashes, while the alias built
    from the forward-slash declared root keeps the forward tail — only
    the FOLDED search can place the span that suppresses the parent's
    name token."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-user")
    out = detect_scope_mismatch(
        body=r"Auth middleware lives at ~\work\mono\services\api\auth.go now.",
        declared_scopes=["projects:mono-api"],
        project_scopes={"projects:mono", "projects:mono-api"},
        project_roots={
            "projects:mono": r"C:\Users\bm-user\work\mono",
            "projects:mono-api": "C:/Users/bm-user/work/mono/services/api",
        },
    )
    assert out.has_mismatch is False


def test_pass1_token_still_fires_outside_child_tree_other_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the folded span search must not over-suppress. A path
    under the parent but outside the declared child's subtree produces
    no child-root span, so the parent's name token legitimately
    flags — whatever the separator families involved."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-user")
    out = detect_scope_mismatch(
        body=r"The CI config lives at C:\Users\bm-user\work\mono\ci\pipeline.yml.",
        declared_scopes=["projects:mono-api"],
        project_scopes={"projects:mono", "projects:mono-api"},
        project_roots={
            "projects:mono": r"C:\Users\bm-user\work\mono",
            "projects:mono-api": "C:/Users/bm-user/work/mono/services/api",
        },
    )
    assert out.has_mismatch is True
    assert out.matches[0].kind == "project_name"
    assert "projects:mono" in out.suggested_scopes


# ---------------------------------------------------------------------------
# CASE axis — byte-first comparisons with a filesystem-probe retry.
#
# Every root comparison in the module used to settle on bytes alone (after
# the separator fold), so on a case-folding volume — Windows NTFS, default
# macOS APFS — a case-variant spelling of one directory (the lowercase
# drive letter some shells record into a synced store, a re-cased segment
# in a hand-edited one) slipped the degenerate-home guard and lost the
# shared/nested-root suppressions. Closed by mirroring the byte-then-probe
# pairing of `verify._is_under_home`: bytes first, and only a case-modulo
# match asks the filesystem (`verify._home_ignores_case`) whether the
# anchoring directory's volume folds case. Like verify's own case test,
# the follows-the-filesystem tests below assert BOTH directions against
# the live volume — case sensitivity is a per-volume property, so neither
# branch can be pinned by platform. The probe fails closed: a directory
# absent from the local filesystem keeps byte semantics (the disclosed
# residue for cross-machine stores), pinned by the unprobeable-store test.
# ---------------------------------------------------------------------------


def test_collect_project_roots_case_variant_home_follows_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home cwd recorded in a case-variant spelling names the SAME
    directory on a folding volume, so byte equality let it through as a
    store-wide prefix-matching root — the cascade the guard exists to
    stop. On a case-sensitive volume the spelling is a genuinely
    different directory and must stay a legitimate root."""
    home = tmp_path / "BmCaseHome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    reskinned = str(home).swapcase()
    assert reskinned != str(home), "fixture assumption: home must have cased chars"
    kept_root = str(home) + os.sep + "work" + os.sep + "bm-server"
    dotfiles = _memory(scopes=["projects:dotfiles"], cwd=reskinned)
    kept = _memory(scopes=["projects:bettermemory"], cwd=kept_root)
    out = collect_project_roots([dotfiles, kept])
    if _home_ignores_case(str(home)):
        assert out == {"projects:bettermemory": kept_root}
    else:
        assert out == {
            "projects:dotfiles": reskinned,
            "projects:bettermemory": kept_root,
        }


def test_home_alias_case_variant_home_prefix_follows_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root whose leading portion spells home in a different case is
    home-rooted on a folding volume — the byte prefix check silently
    skipped the alias search for it, the same false-negative class the
    separator fold closed. On a case-sensitive volume it is NOT
    home-rooted and must not contract. The tail keeps the root's own
    spelling either way."""
    home = tmp_path / "BmCaseHome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    tail = os.sep + os.sep.join(("work", "bm-server"))
    reskinned = str(home).swapcase() + tail
    if _home_ignores_case(str(home)):
        assert _home_alias(reskinned) == "~" + tail
    else:
        assert _home_alias(reskinned) is None


def test_shared_root_case_variant_follows_the_filesystem(tmp_path: Path) -> None:
    """The shared-root suppression across a case-variant pair: two
    scopes recorded one directory in different casings. On a folding
    volume the collision is real and the correctly-tagged write must
    not bounce demanding the foreign scope; on a case-sensitive volume
    the re-cased spelling is a different directory and flagging it is
    the honest verdict."""
    shared = tmp_path / "BmWebapp"
    shared.mkdir()
    exact = str(shared)
    reskinned = exact.swapcase()
    assert reskinned != exact, "fixture assumption: cased path"
    body = (
        "The webapp dev server config is "
        + reskinned
        + os.sep
        + "vite.config.ts; HMR needs port 5174."
    )
    out = detect_scope_mismatch(
        body=body,
        declared_scopes=["projects:bm-app"],
        project_scopes=set(),
        project_roots={
            "projects:bm-lab": reskinned,
            "projects:bm-app": exact,
        },
    )
    if _home_ignores_case(exact):
        assert out.has_mismatch is False
    else:
        assert out.has_mismatch is True
        assert out.suggested_scopes == ("projects:bm-lab",)


def test_nested_roots_case_variant_child_follows_the_filesystem(
    tmp_path: Path,
) -> None:
    """The covers checks across a case-variant pair: the declared child
    root is recorded re-cased relative to the candidate parent and the
    body's citation. On a folding volume the child still extends the
    parent at the match and the parent hit stays suppressed; on a
    case-sensitive volume the stored child spelling is a different
    directory and the parent is honestly flagged."""
    parent = tmp_path / "BmMono"
    child = parent / "services" / "api"
    child.mkdir(parents=True)
    parent_exact = str(parent)
    child_exact = str(child)
    child_reskinned = child_exact.swapcase()
    assert child_reskinned != child_exact, "fixture assumption: cased path"
    body = "Auth middleware lives at " + child_exact + os.sep + "auth.go now."
    out = detect_scope_mismatch(
        body=body,
        declared_scopes=["projects:bm-child"],
        project_scopes=set(),
        project_roots={
            "projects:bm-parent": parent_exact,
            "projects:bm-child": child_reskinned,
        },
    )
    if _home_ignores_case(child_exact):
        assert out.has_mismatch is False
    else:
        assert out.has_mismatch is True
        assert "projects:bm-parent" in out.suggested_scopes


def test_pass1_span_case_variant_citation_follows_the_filesystem(
    tmp_path: Path,
) -> None:
    """CASE leg of the folded span search: the body cites the declared
    child tree in a re-cased spelling. On a folding volume the span
    still lands and the parent's name token stays suppressed; on a
    case-sensitive volume the citation names a different tree and the
    token honestly flags the parent scope."""
    parent = tmp_path / "quasar"
    child = parent / "services" / "api"
    child.mkdir(parents=True)
    child_exact = str(child)
    reskinned = child_exact.swapcase()
    assert reskinned != child_exact, "fixture assumption: cased path"
    body = "Auth middleware lives at " + reskinned + os.sep + "auth.go now."
    out = detect_scope_mismatch(
        body=body,
        declared_scopes=["projects:quasar-api"],
        project_scopes={"projects:quasar", "projects:quasar-api"},
        project_roots={
            "projects:quasar": str(parent),
            "projects:quasar-api": child_exact,
        },
    )
    if _home_ignores_case(child_exact):
        assert out.has_mismatch is False
    else:
        assert out.has_mismatch is True
        assert out.matches[0].kind == "project_name"
        assert out.suggested_scopes == ("projects:quasar",)


def test_case_variant_windows_store_unprobeable_keeps_byte_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disclosed residue of probe-don't-assume: a case-variant pair
    in a store synced from another machine cannot be probed (the
    directories exist on no CI leg — `bm-user` is nobody's runner
    account), so the case leg fails closed and byte semantics stay.
    The candidate is flagged exactly as before the case fix —
    conservative, and pinned so replacing the probe with a platform
    guess has to face this test."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-user")
    out = detect_scope_mismatch(
        body=(
            r"The webapp dev server config is c:\users\bm-user\code\webapp"
            r"\vite.config.ts; HMR needs port 5174."
        ),
        declared_scopes=["projects:webapp"],
        project_scopes=set(),
        project_roots={
            "projects:homelab": r"c:\users\bm-user\code\webapp",
            "projects:webapp": r"C:\Users\bm-user\code\webapp",
        },
    )
    assert out.has_mismatch is True
    assert out.suggested_scopes == ("projects:homelab",)


def test_case_variant_drive_letter_suppressed_when_volume_folds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive twin of the unprobeable test: the same synced-store
    shape — a lowercase drive letter recorded by another shell — with
    the volume verdict pinned to case-folding, because no CI leg can
    conjure a folding volume for a path that must NOT exist locally.
    The probe itself runs against live volumes in the
    follows-the-filesystem tests above and in verify.py's own suite.
    With the verdict pinned, the re-cased spelling reads as the
    declared root and the correctly-tagged write stays quiet."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-user")

    def _volume_folds(path: str) -> bool:
        return True

    monkeypatch.setattr("bettermemory.scope_match._home_ignores_case", _volume_folds)
    out = detect_scope_mismatch(
        body=(
            r"The webapp dev server config is c:\users\bm-user\code\webapp"
            r"\vite.config.ts; HMR needs port 5174."
        ),
        declared_scopes=["projects:webapp"],
        project_scopes=set(),
        project_roots={
            "projects:homelab": r"c:\users\bm-user\code\webapp",
            "projects:webapp": r"C:\Users\bm-user\code\webapp",
        },
    )
    assert out.has_mismatch is False


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


def test_collect_project_roots_drops_altsep_spelled_windows_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The degenerate-root guard is the fail-open sibling: byte equality
    against the backslash-canonical `Path.home()` let a forward-slash or
    mixed Windows spelling of the home cwd through as a regular root —
    which then prefix-matches essentially every path the user cites,
    the store-wide cascade the guard exists to stop. Both comparands
    now fold; kept roots keep the store's original spelling."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-user")
    fwd_home = _memory(scopes=["projects:dotfiles"], cwd="C:/Users/bm-user")
    mixed_home = _memory(scopes=["projects:dotfiles-mixed"], cwd="C:\\Users/bm-user")
    canonical_home = _memory(
        scopes=["projects:dotfiles-canonical"], cwd=r"C:\Users\bm-user"
    )
    posix_marker = _memory(scopes=["projects:rootfs"], cwd="/")
    sep_marker = _memory(scopes=["projects:driveless"], cwd="\\")
    kept = _memory(
        scopes=["projects:bettermemory"], cwd="C:/Users/bm-user/work/bm-server"
    )
    out = collect_project_roots(
        [fwd_home, mixed_home, canonical_home, posix_marker, sep_marker, kept]
    )
    # Every home/root spelling is dropped; the real project root
    # survives IN ITS ORIGINAL forward-slash spelling.
    assert out == {"projects:bettermemory": "C:/Users/bm-user/work/bm-server"}


def test_collect_project_roots_posix_backslash_stays_a_filename_character() -> None:
    """On POSIX `os.altsep` is None and the fold must be the identity: a
    root of literally ``\\`` is an (odd) relative name, NOT a spelling
    of the filesystem root, so the degenerate guard must keep it.
    Gated on the live `os.altsep` because under ntpath semantics the
    same spelling really is the root marker (pinned above)."""
    if os.altsep is not None:
        pytest.skip("this platform has an altsep; the fold is MEANT to fire here")
    a = _memory(scopes=["projects:oddly-named"], cwd="\\")
    out = collect_project_roots([a])
    assert out == {"projects:oddly-named": "\\"}


def test_collect_project_roots_empty_when_no_origin() -> None:
    a = _memory(scopes=["projects:foo"])  # no origin
    out = collect_project_roots([a])
    assert out == {}


# ---------------------------------------------------------------------------
# Degenerate roots at intermediate depths — `_drop_ancestor_roots`
# ---------------------------------------------------------------------------


def test_collect_project_roots_drops_a_root_that_contains_another() -> None:
    """The `$HOME` / `/` guard only names the degenerate root at its two
    best-known spellings; the pathology lives at every level between. A
    scope whose memories were all written from `~/Documents` inherits that
    directory as its root and then prefix-matches every sibling project's
    paths — the store this was measured on had two such scopes, and 31 of
    its 86 recorded `scope_mismatch` events suggested those two and
    nothing else."""
    a = _memory(scopes=["projects:cml"], cwd="/Users/me/Documents")
    b = _memory(scopes=["projects:homelab"], cwd="/Users/me/Documents")
    c = _memory(scopes=["projects:foo"], cwd="/Users/me/Documents/projects/foo")
    out = collect_project_roots([a, b, c])
    assert out == {"projects:foo": "/Users/me/Documents/projects/foo"}


def test_collect_project_roots_keeps_a_shallow_root_nothing_sits_under() -> None:
    """Ancestry is decided against the roots the store actually carries,
    not against a depth threshold: a shallow root that no sibling sits
    under still discriminates and is kept."""
    a = _memory(scopes=["projects:shallow"], cwd="/srv/app")
    b = _memory(scopes=["projects:deep"], cwd="/Users/me/a/b/c/d/deep")
    out = collect_project_roots([a, b])
    assert out == {
        "projects:shallow": "/srv/app",
        "projects:deep": "/Users/me/a/b/c/d/deep",
    }


def test_collect_project_roots_ancestry_respects_segment_boundaries() -> None:
    """`/a/foo` is not an ancestor of `/a/foobar` — the same boundary rule
    `_find_root_occurrence` applies in prose, so a sibling whose last
    segment merely shares a prefix must not evict a legitimate root."""
    a = _memory(scopes=["projects:foo"], cwd="/Users/me/projects/foo")
    b = _memory(scopes=["projects:foobar"], cwd="/Users/me/projects/foobar")
    out = collect_project_roots([a, b])
    assert out == {
        "projects:foo": "/Users/me/projects/foo",
        "projects:foobar": "/Users/me/projects/foobar",
    }


def test_collect_project_roots_keeps_identical_roots() -> None:
    """Two scopes sharing ONE checkout (monorepo sub-projects) are not an
    ancestry pair — the containment test is strict, and the identical-root
    case already has its own suppression at the comparison site."""
    a = _memory(scopes=["projects:api"], cwd="/Users/me/projects/mono")
    b = _memory(scopes=["projects:web"], cwd="/Users/me/projects/mono")
    out = collect_project_roots([a, b])
    assert out == {
        "projects:api": "/Users/me/projects/mono",
        "projects:web": "/Users/me/projects/mono",
    }


def test_generic_home_subdirectory_no_longer_suggests_sibling_scopes() -> None:
    """End to end, the false positive this fixes: a ruling memory that
    cites `~/Documents` as a location, tagged with no project scope at
    all, used to be blocked with `projects:cml` and `projects:homelab`
    suggested — two projects that merely live under that directory."""
    mems = [
        _memory(scopes=["projects:cml"], cwd="/Users/me/Documents"),
        _memory(scopes=["projects:homelab"], cwd="/Users/me/Documents"),
        _memory(scopes=["projects:foo"], cwd="/Users/me/Documents/projects/foo"),
    ]
    body = (
        "RULING. The audit covered the store and the global config under "
        "/Users/me/Documents, and every proposed fix was approved."
    )
    report = detect_scope_mismatch(
        body=body,
        declared_scopes=["workflow", "learning-style"],
        project_scopes=collect_project_scopes(mems),
        project_roots=collect_project_roots(mems),
    )
    assert not report.has_mismatch
    assert report.suggested_scopes == ()
