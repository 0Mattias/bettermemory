"""Tests for verify.py — path-drift detection on memory bodies.

Detection is the load-bearing piece. Existence checks against the real
filesystem use temp paths fixture-style (paths created/deleted in the
test) to keep behaviour deterministic across machines.

Also exercises `compute_verification_status` — the structural staleness
verdict the retrieval surface attaches to every response. The point of
the test class is to lock in that "never" / "stale" both populate the
`recommendation` field so a model receiving the payload can't miss the
ask to spot-check.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bettermemory.origin import Origin
from bettermemory.verify import (
    DEFAULT_VERIFICATION_STALE_DAYS,
    CommitDriftStatus,
    PathDriftReport,
    VerificationStatus,
    _MAX_ANCHOR_CITATIONS,
    _MAX_ANCHORED_CITATION_STATS,
    _MAX_BODY_SCAN_BYTES,
    _MAX_PATH_LENGTH,
    _PLACEHOLDER_PATHS,
    _PLACEHOLDER_PREFIXES,
    _RELATIVE_CITATION_RE,
    _extract_candidates,
    _fold_altsep,
    _home_ignores_case,
    _is_multi_segment_routelike,
    _is_under_home,
    _normalize_candidate,
    _normalize_for_compare,
    commit_drift_anchor_paths,
    compute_commit_drift,
    compute_staleness_verdict,
    compute_verification_status,
    detect_path_drift,
    resolve_commit_drift_count,
    verdict_from_signals,
)

from .conftest import set_git_discovery_ceiling


_GIT_AVAILABLE = shutil.which("git") is not None


def _init_repo_with_remote(path: Path, *, remote: str) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", remote],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _commit_at(path: Path, message: str, *, when: datetime) -> None:
    iso = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = iso
    env["GIT_COMMITTER_DATE"] = iso
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", message],
        cwd=path,
        check=True,
        capture_output=True,
        env=env,
    )


def _commit_touching(
    path: Path, message: str, *, when: datetime, filename: str = "notes.md"
) -> None:
    """Commit that TOUCHES a file — required by the claim-anchored drift
    policy: only commits touching a memory's cited/attested paths count,
    so drift-expecting fixtures must move the cited file, not just HEAD
    (`_commit_at`'s --allow-empty commits are invisible to the filter)."""
    target = path / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as fh:
        fh.write(f"{message}\n")
    subprocess.run(["git", "add", filename], cwd=path, check=True, capture_output=True)
    iso = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = iso
    env["GIT_COMMITTER_DATE"] = iso
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=path,
        check=True,
        capture_output=True,
        env=env,
    )


def _commit_touching_split(
    path: Path,
    message: str,
    *,
    author_when: datetime,
    committer_when: datetime,
    filename: str = "notes.md",
) -> None:
    """Commit that TOUCHES a file with DIFFERENT author/committer dates —
    the on-disk shape a rebase leaves (author date preserved, committer
    date rewritten). Distinct from `_commit_touching` (author == committer)
    and `test_server_commit_drift._commit_split` (empty, touches no file, so
    invisible to the claim-anchored path filter)."""
    target = path / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as fh:
        fh.write(f"{message}\n")
    subprocess.run(["git", "add", filename], cwd=path, check=True, capture_output=True)
    author_iso = author_when.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    committer_iso = committer_when.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = author_iso
    env["GIT_COMMITTER_DATE"] = committer_iso
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=path,
        check=True,
        capture_output=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Empty / no-paths cases
# ---------------------------------------------------------------------------


def test_empty_body_returns_empty_report() -> None:
    report = detect_path_drift("")
    assert report.checked == ()
    assert report.missing == ()
    assert report.has_drift is False


def test_body_with_no_path_tokens_returns_empty_report() -> None:
    report = detect_path_drift("Just some prose with no paths in it.")
    assert report.checked == ()
    assert report.missing == ()


def test_relative_paths_are_ignored() -> None:
    """Relative paths can't be checked without an anchor — too many false
    positives in prose ("docs/installation.md" in a sentence)."""
    report = detect_path_drift("see docs/installation.md and src/foo.py")
    assert report.checked == ()


# ---------------------------------------------------------------------------
# Anchored attestations — relative verified_paths + origin.worktree_root
# ---------------------------------------------------------------------------


def test_relative_attestation_is_checked_against_the_worktree(tmp_path: Path) -> None:
    """The gap P2 measured: a relative citation gets no deletion check.

    The body form stays unchecked (prose is a false-positive swamp), but
    an ATTESTED relative path is the caller's explicit claim, and the
    memory's own `origin.worktree_root` is the anchor the exclusion said
    was missing. Measured on a real 206-memory store before this existed:
    72 memories received no path check of any kind.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "alive.py").write_text("x")
    body = "Defined in `src/alive.py`, removed from `src/gone.py`."

    unanchored = detect_path_drift(body, verified_paths=["src/alive.py", "src/gone.py"])
    assert unanchored.checked == (), "relative body citations stay unchecked"
    assert unanchored.missing == ()

    anchored = detect_path_drift(
        body,
        verified_paths=["src/alive.py", "src/gone.py"],
        worktree_root=tmp_path,
    )
    assert [Path(p).name for p in anchored.missing] == ["gone.py"]
    assert [Path(p).name for p in anchored.verified] == ["alive.py"]


def test_relative_attestation_checked_even_when_body_cites_nothing(
    tmp_path: Path,
) -> None:
    """An attested-only memory must still get a report.

    `detect_path_drift` returns early when the body yields no candidates;
    with an anchor and attestations there is real work to do, so that
    early exit would silence exactly the memories this covers.
    """
    report = detect_path_drift(
        "A prose-only note with no path citation whatsoever.",
        verified_paths=["src/gone.py"],
        worktree_root=tmp_path,
    )
    assert [Path(p).name for p in report.missing] == ["gone.py"]


def test_anchored_absent_attestation_is_expected_not_missing(tmp_path: Path) -> None:
    """`verified_absent_paths` keeps its escape-hatch meaning when anchored —
    an intentionally-absent path must not become a perpetual drift signal."""
    report = detect_path_drift(
        "Deployed from `deploy/remote.yml` on the other host.",
        absent_paths=["deploy/remote.yml"],
        worktree_root=tmp_path,
    )
    assert report.missing == ()
    assert [Path(p).name for p in report.expected_absent] == ["remote.yml"]


def test_anchoring_does_not_duplicate_an_absolute_citation(tmp_path: Path) -> None:
    """A path cited absolutely AND attested is one claim, not two.

    The body pass already checked it; the anchored pass must not append a
    second entry for the same file.
    """
    real = tmp_path / "dup.py"
    real.write_text("x")
    report = detect_path_drift(
        f"The file `{real}` is the entry point.",
        verified_paths=[str(real)],
        worktree_root=tmp_path,
    )
    assert len(report.checked) == 1


def test_anchoring_is_inert_without_a_worktree_root(tmp_path: Path) -> None:
    """Memories written outside a git checkout carry no worktree_root, and
    must behave exactly as before rather than resolving against a cwd."""
    report = detect_path_drift(
        "Defined in `src/gone.py`.", verified_paths=["src/gone.py"]
    )
    assert report.checked == () and report.missing == ()


# ---------------------------------------------------------------------------
# Anchored CITATIONS — relative paths in body prose, resolved against the
# memory's recorded worktree.
#
# The measured gap: the rot benchmark's relative-citation arm produced
# EXACTLY ZERO path-drift flags, so the citation style developers actually
# write got no path protection while the same claims written absolutely
# were checked. The anchor closes it; the filter layer below is what keeps
# it from fabricating drift, because `_RELATIVE_CITATION_RE` is
# deliberately over-matchy — safe for commit anchors (a phantom touches no
# commit) and NOT safe for a stat.
# ---------------------------------------------------------------------------


def test_relative_body_citation_is_checked_when_anchored(tmp_path: Path) -> None:
    """The feature: a cited-but-unattested relative path gets a real check.

    Without it, a relative citation nobody attested — just the way the
    author happened to write the path — could be deleted and every
    retrieval of the memory would still read clean.
    """
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "alive.py").write_text("x")
    body = "Handled in src/pkg/alive.py; the old src/pkg/gone.py is history."

    report = detect_path_drift(body, worktree_root=tmp_path)
    assert [Path(p).name for p in report.missing] == ["gone.py"]
    assert [Path(p).name for p in report.checked] == ["alive.py", "gone.py"]
    # Never `verified` — that bucket means "attested AND present", and a
    # citation is nobody's reviewed claim.
    assert report.verified == ()


def test_relative_body_citation_stays_unchecked_without_an_anchor(
    tmp_path: Path,
) -> None:
    """The gate the frozen pre-registration rests on.

    P2 of the rot pre-registration is graded from arms that call
    `detect_path_drift(body)` with no worktree_root. If the citation pass
    ever fires unanchored, a published prediction is retroactively
    falsified by a code change — so the gate is pinned, not assumed.
    """
    (tmp_path / "src").mkdir()
    report = detect_path_drift("The old src/gone.py is history.")
    assert report.checked == () and report.missing == ()


def test_a_worktree_this_machine_never_had_skips_every_anchored_check(
    tmp_path: Path,
) -> None:
    """Cross-host fail-open, for citations AND attestations.

    A store synced from another host carries that host's worktree_root.
    Joining relative claims to it marks EVERY one missing — a constant
    function, not a detector, firing on every memory from that host at
    once. `_worktree_root_is_live` refuses to answer instead: a machine
    that never saw the checkout has no evidence either way.
    """
    body = "Handled in src/pkg/mod.py."
    gone = tmp_path / "checkout-from-another-host"

    report = detect_path_drift(
        body, verified_paths=["src/pkg/mod.py"], worktree_root=gone
    )
    assert report.checked == () and report.missing == ()

    # A recorded root that resolves to a FILE is equally unusable.
    not_a_dir = tmp_path / "root.txt"
    not_a_dir.write_text("x")
    shadowed = detect_path_drift(
        body, verified_paths=["src/pkg/mod.py"], worktree_root=not_a_dir
    )
    assert shadowed.checked == () and shadowed.missing == ()


def test_bare_domains_and_schemeless_urls_are_never_stat_checked(
    tmp_path: Path,
) -> None:
    """The over-match the commit-drift regex tolerates on purpose.

    `pypi.org` is a zero-directory match the regex admits knowingly, and
    `www.example.com/a/b.md` matches whole because the domain-with-route
    lookahead only rejects extensionless tails. Anchored and stat'd, both
    would report a missing FILE. The real directories are created here so
    the parent-existence rule cannot be what saves the test — the
    host-shape and directory-segment rules have to.
    """
    (tmp_path / "www.example.com" / "a").mkdir(parents=True)
    (tmp_path / "docs.rs" / "serde" / "latest").mkdir(parents=True)
    body = (
        "Published on pypi.org; mirrored at www.example.com/a/b.md and "
        "documented at docs.rs/serde/latest/index.html."
    )
    report = detect_path_drift(body, worktree_root=tmp_path)
    assert report.checked == () and report.missing == ()


def test_a_filename_without_a_directory_is_not_checked(tmp_path: Path) -> None:
    """The single largest false-positive class.

    Prose names a file without its directory constantly ("the `_MODES`
    tuple in run.py", "bump CHANGELOG.md"), and joined to the worktree
    ROOT almost none of those exist. The root itself obviously exists, so
    the parent-existence rule would happily let both through — the
    directory-segment rule is the one being pinned.
    """
    report = detect_path_drift(
        "Bump CHANGELOG.md, then the _MODES tuple in run.py.",
        worktree_root=tmp_path,
    )
    assert report.checked == () and report.missing == ()


def test_citation_whose_parent_directory_is_gone_is_dropped_not_missing(
    tmp_path: Path,
) -> None:
    """A citation written relative to somewhere other than the repo root.

    An author standing in `bench/` writes `rot/run.py`; anchored at the
    worktree root that resolves to a path whose whole neighbourhood is
    absent. Reporting it missing would be drift manufactured by our own
    anchoring guess, so an absent parent means SKIP. The known cost —
    a whole-directory delete takes its citations down with it — is the
    same bound `_is_multi_segment_routelike` already documents.
    """
    (tmp_path / "src").mkdir()
    report = detect_path_drift(
        "Entry point is rot/run.py these days.", worktree_root=tmp_path
    )
    assert report.checked == () and report.missing == ()

    # Same shape, live neighbourhood: that one IS reported.
    real = detect_path_drift("Entry point is src/run.py.", worktree_root=tmp_path)
    assert [Path(p).name for p in real.missing] == ["run.py"]


def test_unlisted_extension_is_not_checked(tmp_path: Path) -> None:
    """The regex accepts any 2-8 letter-first run as an "extension", so
    slash-and-dot shaped prose ("the src/dst.mapping split") validates as
    a path. The parent directory is created so only the extension
    allowlist can be doing the work."""
    (tmp_path / "src").mkdir()
    report = detect_path_drift(
        "Documented under src/dst.mapping for now.", worktree_root=tmp_path
    )
    assert report.checked == () and report.missing == ()


def test_placeholder_relative_citation_is_not_checked(tmp_path: Path) -> None:
    """`path/to/...` is the universal documentation placeholder. Anchored,
    the prefix test would no longer recognise it (the worktree root sits
    in front), which is why the check runs on the root-slashed form."""
    (tmp_path / "path" / "to").mkdir(parents=True)
    report = detect_path_drift(
        "Pass `--config path/to/config.yaml` to override.", worktree_root=tmp_path
    )
    assert report.checked == () and report.missing == ()


def test_anchored_citation_does_not_duplicate_an_attested_path(
    tmp_path: Path,
) -> None:
    """One file cited AND attested is one claim. The attestation must be
    the entry that lands — it carries the `verified` / `expected_absent`
    semantics a bare citation cannot express."""
    (tmp_path / "src").mkdir()
    report = detect_path_drift(
        "Deployed from src/remote.yml on the other host.",
        absent_paths=["src/remote.yml"],
        worktree_root=tmp_path,
    )
    assert len(report.checked) == 1
    assert report.missing == ()
    assert [Path(p).name for p in report.expected_absent] == ["remote.yml"]


def test_anchored_citations_respect_the_stat_budget(tmp_path: Path) -> None:
    """A body citing hundreds of files must not turn one retrieval into
    hundreds of stats. The budget is the STAT cap (8), not the commit
    anchor cap (24) — an anchor is one pathspec string in a single `git
    log`, a citation is a syscall on the hottest read path."""
    (tmp_path / "src").mkdir()
    body = " ".join(f"src/mod{i}.py" for i in range(60))
    report = detect_path_drift(body, worktree_root=tmp_path)
    assert len(report.missing) == _MAX_ANCHORED_CITATION_STATS
    assert _MAX_ANCHORED_CITATION_STATS < _MAX_ANCHOR_CITATIONS


def test_filtered_noise_does_not_exhaust_the_stat_budget(tmp_path: Path) -> None:
    """The budget is counted at the stat, not at the regex match. Counting
    matches would let a body full of bare domains and root filenames burn
    the whole allowance before the one real citation at the end of the
    body was ever looked at."""
    (tmp_path / "src").mkdir()
    noise = " ".join(["pypi.org", "CHANGELOG.md", "run.py"] * 20)
    body = f"{noise} and finally src/real.py."
    assert detect_path_drift(body).checked == ()
    anchored = detect_path_drift(body, worktree_root=tmp_path)
    assert [Path(p).name for p in anchored.missing] == ["real.py"]


def test_an_existing_citation_is_evidence_not_an_alarm(tmp_path: Path) -> None:
    """A citation that checks out lands in `checked` and nowhere else.

    Every surface gates its `path_drift` block on missing / verified /
    expected_absent, so a healthy body adds evidence a caller can read
    off the report without adding noise to the wire or moving the
    staleness verdict.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "here.py").write_text("x")
    report = detect_path_drift("Lives in src/here.py.", worktree_root=tmp_path)
    assert len(report.checked) == 1
    assert report.missing == () and report.verified == ()
    assert report.has_drift is False


def test_adversarial_prose_produces_no_anchored_citation_flags(
    tmp_path: Path,
) -> None:
    """The zero-false-positive suite, extended to the anchored path.

    Every shape here reached `_RELATIVE_CITATION_RE` as a match or a near
    match at some point in its history. With a live anchor in play, one
    escape is one fabricated `path_drift_missing` on a real memory — and
    the verdict escalation that follows it.
    """
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "path" / "to").mkdir(parents=True)
    (tmp_path / "www.example.com").mkdir()
    body = (
        "CI/CD and TCP/IP pipelines, e.g. the U.S. case, i.e. at 3.16.0 or "
        "v3.16.0rc1. Docs at docs.python.org/3/library/re.html, package on "
        "pypi.org/simple/pkg and pypi.org, mirror www.example.com/index.html. "
        "Bump CHANGELOG.md and run.py; config at path/to/settings.toml; "
        "notes in src/pkg/notes.thing; the read/write.access split; "
        "and -leading-dash.md."
    )
    report = detect_path_drift(body, worktree_root=tmp_path)
    assert report.missing == (), report.missing
    assert report.checked == (), report.checked


# ---------------------------------------------------------------------------
# Backtick-wrapped paths
# ---------------------------------------------------------------------------


def test_backtick_path_existing(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("x")
    body = f"The script is at `{real}` for now."
    report = detect_path_drift(body)
    assert (
        real.as_posix() in [str(p) for p in report.checked]
        or str(real) in report.checked
    )
    assert report.missing == ()


def test_backtick_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.txt"
    body = f"The old script lived at `{missing}`."
    report = detect_path_drift(body)
    assert str(missing) in report.checked
    assert str(missing) in report.missing
    assert report.has_drift is True


def test_backtick_path_with_trailing_slash_normalised(tmp_path: Path) -> None:
    """`/tmp/foo/` and `/tmp/foo` should dedup to the same candidate."""
    d = tmp_path / "dir"
    d.mkdir()
    body = f"Lives in `{d}/` and also at `{d}` again."
    report = detect_path_drift(body)
    # One canonical candidate, found once.
    assert len(report.checked) == 1
    assert report.missing == ()


# ---------------------------------------------------------------------------
# Bare paths
# ---------------------------------------------------------------------------


def test_bare_absolute_path_existing(tmp_path: Path) -> None:
    real = tmp_path / "bare.txt"
    real.write_text("x")
    body = f"Open {real} to see the config."
    report = detect_path_drift(body)
    assert str(real) in report.checked
    assert report.missing == ()


def test_bare_absolute_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "ghost.txt"
    body = f"Used to live at {missing} on the homelab."
    report = detect_path_drift(body)
    assert str(missing) in report.missing


def test_bare_home_path() -> None:
    """`~/...` paths are expanded before stat."""
    body = "Config at ~/.does-not-exist-bettermemory-test on the box."
    report = detect_path_drift(body)
    # The path candidate is recorded literally (with `~`); existence
    # check expands. The path probably doesn't exist on the test box.
    assert "~/.does-not-exist-bettermemory-test" in report.checked
    assert "~/.does-not-exist-bettermemory-test" in report.missing


# ---------------------------------------------------------------------------
# Trailing punctuation
# ---------------------------------------------------------------------------


def test_trailing_period_stripped(tmp_path: Path) -> None:
    real = tmp_path / "foo.txt"
    real.write_text("x")
    body = f"It is at {real}."
    report = detect_path_drift(body)
    # The literal candidate should be the path *without* the trailing period.
    assert str(real) in report.checked


def test_trailing_comma_stripped(tmp_path: Path) -> None:
    missing = tmp_path / "missing-comma"
    body = f"See {missing}, and elsewhere."
    report = detect_path_drift(body)
    assert str(missing) in report.checked


def test_trailing_close_paren_stripped(tmp_path: Path) -> None:
    real = tmp_path / "in-parens"
    real.mkdir()
    body = f"(check {real})"
    report = detect_path_drift(body)
    assert str(real) in report.checked


# ---------------------------------------------------------------------------
# False positives we want to avoid
# ---------------------------------------------------------------------------


def test_url_not_treated_as_path() -> None:
    body = "See https://github.com/0Mattias/bettermemory for details."
    report = detect_path_drift(body)
    assert report.checked == ()


def test_git_ssh_remote_not_treated_as_path() -> None:
    body = "Clone via git@github.com:owner/repo.git for the remote."
    report = detect_path_drift(body)
    assert report.checked == ()


def test_user_at_host_path_not_treated_as_path() -> None:
    """`user@host:/path` is an SSH command target, not a local path."""
    body = "scp from mattias@homelab:/etc/foo to local."
    report = detect_path_drift(body)
    # The `:/etc/foo` shouldn't slip through. The bare regex's boundary
    # check rules out preceding word chars, and the `@` filter in the
    # candidate validator catches it if it does.
    assert all("@" not in c for c in report.checked)


def test_claude_code_slash_command_not_treated_as_path() -> None:
    """`/plugin marketplace add owner/repo` is a Claude Code slash command,
    not a path. It would otherwise get extracted from backticks and end
    up in `missing` because no such file exists on disk — a false
    positive on any memory that quotes the plugin install command."""
    body = (
        "Install path: `/plugin marketplace add 0Mattias/bettermemory` "
        "then `/plugin install bettermemory@bettermemory`."
    )
    report = detect_path_drift(body)
    assert report.checked == ()
    assert report.missing == ()


def test_shell_invocation_not_treated_as_path() -> None:
    """A backtick-wrapped shell invocation starting with an absolute path
    to a binary still has command shape — slash + command name + space-
    separated arguments. The disk would say "missing" for the whole
    string, which is the false positive we want to avoid."""
    body = "Run `/usr/bin/env python -m bettermemory` to start the server."
    report = detect_path_drift(body)
    assert report.checked == ()
    assert report.missing == ()


def test_path_with_internal_spaces_still_detected(tmp_path: Path) -> None:
    """A real path with internal whitespace crosses directory boundaries,
    so its first whitespace-separated chunk contains multiple slashes —
    distinguishing it from a CLI invocation. Make sure the command-shape
    filter doesn't reject these."""
    target = tmp_path / "Some User" / "file.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x")
    body = f"Stored at `{target}`."
    report = detect_path_drift(body)
    assert str(target) in report.checked
    assert str(target) not in report.missing


def test_two_token_slash_command_not_treated_as_path() -> None:
    """The minimal command shape `/cmd arg` — two whitespace-separated
    tokens, the first a single-slash command name. We catch it via the
    same heuristic as longer commands."""
    body = "Just run `/plugin install` and you're done."
    report = detect_path_drift(body)
    assert "/plugin install" not in report.checked
    assert "/plugin install" not in report.missing


def test_short_paths_excluded() -> None:
    """`/x` is too short to be a meaningful claim — too many false positives
    in prose like "see the / divider"."""
    body = "Use /x or /y for the divider."
    report = detect_path_drift(body)
    assert "/x" not in report.checked
    assert "/y" not in report.checked


def test_root_alone_excluded() -> None:
    body = "From / down through the tree."
    report = detect_path_drift(body)
    assert "/" not in report.checked


# ---------------------------------------------------------------------------
# Documentation-placeholder paths
#
# Memories that document a path-typed API ("a memory verified for
# `/etc/foo`") use illustrative placeholder paths in prose. The extractor
# would otherwise drag those into `path_drift_missing` because no such
# file exists on disk — a phantom drift signal on every retrieval, with
# the same `staleness_verdict: "spot_check_recommended"` payload as a
# real broken-path drift. Filter them out at extraction time.
# ---------------------------------------------------------------------------


# Hardcoded so a deletion from `_PLACEHOLDER_PATHS` causes the
# corresponding test case to fail (parametrising off the frozenset
# itself would just drop the case, silently). The membership guard
# below ensures additions still require touching this list.
_EXPECTED_PLACEHOLDER_PATHS: tuple[str, ...] = (
    "/etc/bar",
    "/etc/baz",
    "/etc/foo",
    "/foo",
    "/foo/bar",
    "/foo/bar/baz",
    "/foo/baz",
    "/path/to",
)


def test_placeholder_paths_list_matches_frozenset() -> None:
    """Guard so additions to `_PLACEHOLDER_PATHS` are mirrored in the
    parametrise list — otherwise a new member could ship without
    regression coverage."""
    assert set(_EXPECTED_PLACEHOLDER_PATHS) == set(_PLACEHOLDER_PATHS)


# Sibling pin for the prefix-form placeholder whitelist consumed by
# the same `_is_placeholder` filter (`verify.py:473` and `:481`). The
# `_PLACEHOLDER_PREFIXES` tuple (`verify.py:153`, `("/path/to/",
# "~/path/to/")` — note: a tuple, not a frozenset, because the
# `str.startswith` API accepts a tuple-of-prefixes directly) carries
# the same hazard surface as `_PLACEHOLDER_PATHS`: a deletion turns
# legitimate documentation placeholders into phantom `path_drift_
# missing` entries (a memory verifying `/path/to/file` as an
# illustrative example would start surfacing as broken-path drift),
# and an addition could over-filter (a real path under one of the
# prefixes silently dropped from drift coverage). The existing
# `test_path_to_prefix_placeholder_skipped` and
# `test_home_path_to_prefix_placeholder_skipped` cover the two
# current members per-prefix (deletion side) but neither imports
# `_PLACEHOLDER_PREFIXES`, so an addition couldn't fail any test.
#
# The hardcoded tuple is NOT derived from the source — sibling
# pattern to `_EXPECTED_PLACEHOLDER_PATHS` above. Mirrors the
# `_EXPECTED_USE_OUTCOMES` shape (db81630) on the prefix-filter
# surface.
#
# Negative-control: adding `"/bogus/"` to `_PLACEHOLDER_PREFIXES`
# fails `test_placeholder_prefixes_match_tuple` (set inequality).
# Revert restores green.
_EXPECTED_PLACEHOLDER_PREFIXES: tuple[str, ...] = (
    "/path/to/",
    "~/path/to/",
)


def test_placeholder_prefixes_match_tuple() -> None:
    """Guard so additions to ``_PLACEHOLDER_PREFIXES`` (the closed-protocol
    prefix-form whitelist consumed by ``_is_placeholder`` via
    ``str.startswith``) are mirrored in the hardcoded
    ``_EXPECTED_PLACEHOLDER_PREFIXES`` tuple — otherwise a new prefix
    could ship without regression coverage and over-filter real paths
    out of drift detection. Sibling guard to
    ``test_placeholder_paths_list_matches_frozenset`` above."""
    assert set(_EXPECTED_PLACEHOLDER_PREFIXES) == set(_PLACEHOLDER_PREFIXES)


@pytest.mark.parametrize("placeholder", _EXPECTED_PLACEHOLDER_PATHS)
def test_placeholder_path_skipped(placeholder: str) -> None:
    """Every member of `_PLACEHOLDER_PATHS` must be filtered out of the
    drift report when it appears backtick-wrapped in a memory body. The
    canonical bug — a memory verifying a path-typed API ("a memory
    verified for `/etc/foo` reads as clean…") generating a phantom
    `path_drift_missing` entry on every retrieval — recurs the moment
    any of these members silently drops out of the frozenset. Pin the
    whole set so a deletion fails CI loudly rather than producing
    low-grade telemetry noise."""
    body = (
        f"A memory verified for `{placeholder}` reads as `clean` even when "
        f"the surrounding project moved, as long as `{placeholder}` itself "
        f"didn't."
    )
    report = detect_path_drift(body)
    assert placeholder not in report.checked
    assert placeholder not in report.missing


def test_path_to_prefix_placeholder_skipped() -> None:
    """`/path/to/X` is the universal documentation placeholder; treat
    anything under it as an example, not a citation."""
    body = "Pass `/path/to/file` to the `--config` flag."
    report = detect_path_drift(body)
    assert all("/path/to/" not in c for c in report.checked)
    assert all("/path/to/" not in m for m in report.missing)


def test_home_path_to_prefix_placeholder_skipped() -> None:
    """`~/path/to/...` is the home-relative form of the same convention."""
    body = "Drop the file at `~/path/to/somewhere` for the loader to pick it up."
    report = detect_path_drift(body)
    assert all("path/to" not in c for c in report.checked)


def test_placeholder_with_extension_skipped() -> None:
    """`/etc/foo.conf` is a placeholder too — strip the final extension
    before matching against the placeholder set."""
    body = "Rename `/etc/foo.conf` to your real config name before running."
    report = detect_path_drift(body)
    assert "/etc/foo.conf" not in report.checked


def test_dot_prefixed_real_path_not_misclassified_as_placeholder(
    tmp_path: Path,
) -> None:
    """`~/.claude-memory` and similar dot-prefixed home-relative paths
    must NOT trip the extension-stripping placeholder branch — stripping
    `.claude-memory` would leave the stem `~/`, which is not a
    placeholder, but the regression risk is real enough to pin."""
    target = tmp_path / ".claude-memory"
    target.mkdir()
    body = f"Memories live at `{target}`."
    report = detect_path_drift(body)
    assert str(target) in report.checked
    assert str(target) not in report.missing


def test_tmp_foo_test_fixture_still_valid_path(tmp_path: Path) -> None:
    """`/tmp/foo`-shaped tmp-path fixtures are widely used in real test
    suites and frequently exist briefly during a test run. The
    placeholder filter is deliberately narrow enough that they pass
    through — only `/foo` / `/foo/bar` etc. and the `/etc/foo` family
    are skipped, NOT every terminal-component `foo`."""
    target = tmp_path / "foo"
    target.write_text("real")
    body = f"Created at `{target}`."
    report = detect_path_drift(body)
    assert str(target) in report.checked
    assert str(target) not in report.missing


# ---------------------------------------------------------------------------
# Single-segment routes / identifiers (URL routes mistaken for fs paths)
#
# The canonical bite: a memory body documenting `/verify` (a POST
# route of the since-removed web UI, NOT a filesystem path) was being
# extracted as a path
# candidate, stat'd, and surfaced as `path_drift_missing=1` on every
# retrieval. The class is broader than `/verify` — any single-segment
# absolute path without an extension is almost always a URL route or
# identifier in prose, not a filesystem citation. Filter at extraction
# time so the drift signal stays trustworthy.
# ---------------------------------------------------------------------------


def test_verify_route_in_prose_not_path() -> None:
    """The exact body shape that bit production: backtick-wrapped
    `/verify` followed by an HTTP verb in prose. Must not surface in
    `missing` (or `checked`) — it's a route, not a path."""
    body = "Web UI `/verify` POST: CSRF Origin check and length cap."
    report = detect_path_drift(body)
    assert "/verify" not in report.checked
    assert "/verify" not in report.missing


def test_single_segment_extensionless_routes_skipped() -> None:
    """Broader class of the same bite: route-like single-segment paths
    that pepper API documentation prose."""
    body = (
        "Endpoints documented inline: `/healthz`, `/ready`, `/login`, "
        "`/api`, `/dashboard`. None of these are filesystem citations."
    )
    report = detect_path_drift(body)
    for route in ("/healthz", "/ready", "/login", "/api", "/dashboard"):
        assert route not in report.checked, f"{route} leaked into checked"
        assert route not in report.missing, f"{route} leaked into missing"


def test_single_segment_with_extension_still_extracted() -> None:
    """`/foo.txt` IS a plausible filesystem citation — the extension is
    the distinguishing feature. Should still hit the stat path and
    surface as missing when the file doesn't exist."""
    body = "Touched `/this-file-should-not-exist-xyz123.flag` as a marker."
    report = detect_path_drift(body)
    assert "/this-file-should-not-exist-xyz123.flag" in report.checked
    assert "/this-file-should-not-exist-xyz123.flag" in report.missing


def test_multi_segment_extensionless_still_extracted(tmp_path: Path) -> None:
    """The narrowing applies ONLY to single-segment paths. A
    multi-segment extensionless path like `/usr/local/bin/foo` is a
    legitimate filesystem claim and must still be checked."""
    nested = tmp_path / "subdir" / "binary-name"
    nested.parent.mkdir()
    nested.write_text("#!/bin/sh\n")
    body = f"Installed binary at `{nested}`."
    report = detect_path_drift(body)
    assert str(nested) in report.checked
    assert str(nested) not in report.missing


def test_home_relative_single_segment_still_extracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~/.zshrc`-shaped single-segment home-relative paths are real
    filesystem citations — they go through a different branch
    (`~/` prefix) and must not be affected by the narrowing."""
    home_file = tmp_path / ".some-rc"
    home_file.write_text("real")
    # Cross-platform `~` redirect: POSIX reads `HOME`; Windows reads
    # `USERPROFILE` first, then falls back to `HOMEDRIVE + HOMEPATH`.
    # Setting only `HOME` works on Linux and macOS but is a no-op on
    # Windows — `~` still expands to the runner's real home and the
    # `.some-rc` stat fails. Same pattern v1.4.1 used for the three
    # tests under tests/test_init.py.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    body = "Config lives at `~/.some-rc`."
    report = detect_path_drift(body)
    assert "~/.some-rc" in report.checked
    assert "~/.some-rc" not in report.missing


# ---------------------------------------------------------------------------
# Caps on per-body work
# ---------------------------------------------------------------------------


def test_cap_at_max_paths(tmp_path: Path) -> None:
    """A pathological body with many paths is capped; we don't stat all of them."""
    paths = [tmp_path / f"p{i}" for i in range(20)]
    body = " ".join(f"`{p}`" for p in paths)
    report = detect_path_drift(body)
    assert len(report.checked) <= 8


def test_extremely_long_path_skipped(tmp_path: Path) -> None:
    """Catches DOS-shaped pasted content — a single 600-char "path"."""
    huge = "/tmp/" + ("a" * 600)
    body = f"Lives at `{huge}`."
    report = detect_path_drift(body)
    assert huge not in report.checked


def test_body_scan_capped_at_max_bytes(tmp_path: Path) -> None:
    """The scan input is truncated at `_MAX_BODY_SCAN_BYTES` before any
    regex touches it, so a path claim living past the cap in an adversarial
    multi-KB body is never extracted — bounding the per-hit work regardless
    of body length. A path BEFORE the cap is still processed normally, which
    keeps this mutation-sound: reverting the truncation would surface the
    beyond-cap path in `missing`."""
    near = tmp_path / "within-cap.flag"  # does not exist -> missing
    far = tmp_path / "beyond-cap.flag"  # does not exist, but past the cap
    # Filler with no path shape, long enough to push `far` past the cap.
    filler = "x" * (_MAX_BODY_SCAN_BYTES + 4096)
    body = f"early `{near}` {filler} late `{far}`"
    report = detect_path_drift(body)
    # Within-cap claim is checked and flagged (the scan ran).
    assert str(near) in report.missing
    # Beyond-cap claim was truncated away entirely — not checked, not missing.
    assert str(far) not in report.checked
    assert str(far) not in report.missing


def test_normalize_candidate_length_gate_precedes_trim_loops() -> None:
    """The length gate sits at the very top of `_normalize_candidate`, before
    the char-at-a-time trailing-trim loops. Constructed so the ONLY thing
    that rejects the input is that early gate: the raw candidate exceeds
    `_MAX_PATH_LENGTH + 64`, but its trailing `.` run — which the trim loops
    would strip — brings it under `_MAX_PATH_LENGTH`. If the gate were left
    after the loops (the reverted state), the tail would be stripped and the
    candidate would validate to a non-None path. `is None` therefore fails
    the moment the gate moves back below the loops."""
    trimmable_tail = "." * 80
    core = "/tmp/" + "a" * 500  # 505 chars, well under _MAX_PATH_LENGTH
    raw = core + trimmable_tail
    assert len(raw) > _MAX_PATH_LENGTH + 64
    assert len(core) <= _MAX_PATH_LENGTH
    assert _normalize_candidate(raw) is None


# ---------------------------------------------------------------------------
# Dedup: same path appearing multiple ways
# ---------------------------------------------------------------------------


def test_duplicate_path_collapsed(tmp_path: Path) -> None:
    """Same path mentioned three times only checked once."""
    real = tmp_path / "dup"
    real.write_text("x")
    body = f"At `{real}`, again at `{real}`, and bare at {real} too."
    report = detect_path_drift(body)
    occurrences = [c for c in report.checked if c == str(real)]
    assert len(occurrences) == 1


def test_backtick_takes_precedence_over_bare(tmp_path: Path) -> None:
    """Same path in both backtick and bare form: one canonical entry."""
    real = tmp_path / "both"
    real.write_text("x")
    body = f"In code: `{real}`. Bare: {real}."
    report = detect_path_drift(body)
    matching = [c for c in report.checked if c == str(real)]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# Existence check robustness
# ---------------------------------------------------------------------------


def test_oserror_during_exists_treated_as_missing(tmp_path: Path) -> None:
    """If `Path.exists()` raises (permission denied, ELOOP), we don't crash.

    The cited path is built from `tmp_path` rather than a hardcoded
    `/tmp/...` so its PARENT genuinely exists on every platform. Since
    3.25.2 an extensionless leading-slash candidate whose parent is
    absent reads as an application route
    (`_is_multi_segment_routelike`), and `/tmp` does not exist on
    Windows — the old fixture passed on POSIX and silently changed what
    this test exercised on the windows-latest leg.
    """
    target = tmp_path / "some-real-looking-path"
    body = f"See `{target}` for the thing."

    class _Boom:
        def expanduser(self) -> "_Boom":
            return self

        def exists(self) -> bool:
            raise PermissionError("nope")

    with patch("bettermemory.verify.Path", lambda _x: _Boom()):
        report = detect_path_drift(body)
    # The candidate was checked; PermissionError -> missing bucket.
    assert str(target) in report.checked
    assert str(target) in report.missing


# ---------------------------------------------------------------------------
# PathDriftReport
# ---------------------------------------------------------------------------


def test_report_to_dict_round_trips() -> None:
    r = PathDriftReport(checked=("/a", "/b"), missing=("/b",))
    d = r.to_dict()
    assert d == {
        "checked": ["/a", "/b"],
        "missing": ["/b"],
        "verified": [],
        "expected_absent": [],
        "dropped_as_route": [],
        "claim_anchored_missing": [],
    }


def test_report_to_dict_includes_verified_paths() -> None:
    r = PathDriftReport(checked=("/a", "/b"), missing=("/b",), verified=("/a",))
    d = r.to_dict()
    assert d == {
        "checked": ["/a", "/b"],
        "missing": ["/b"],
        "verified": ["/a"],
        "expected_absent": [],
        "dropped_as_route": [],
        "claim_anchored_missing": [],
    }


def test_report_to_dict_includes_expected_absent() -> None:
    r = PathDriftReport(checked=("/a", "/b"), missing=(), expected_absent=("/b",))
    d = r.to_dict()
    assert d == {
        "checked": ["/a", "/b"],
        "missing": [],
        "verified": [],
        "expected_absent": ["/b"],
        "dropped_as_route": [],
        "claim_anchored_missing": [],
    }


def test_report_to_dict_includes_dropped_as_route() -> None:
    """The suppressed set has to survive serialisation. Both handler
    gates emit `to_dict()` wholesale, so this key is what makes widening
    those gates a one-line change rather than a new plumbing job."""
    r = PathDriftReport(checked=(), missing=(), dropped_as_route=("/admin/macros",))
    d = r.to_dict()
    assert d == {
        "checked": [],
        "missing": [],
        "verified": [],
        "expected_absent": [],
        "dropped_as_route": ["/admin/macros"],
        "claim_anchored_missing": [],
    }


def test_has_drift_only_when_missing_nonempty() -> None:
    healthy = PathDriftReport(checked=("/a",), missing=())
    drifted = PathDriftReport(checked=("/a",), missing=("/a",))
    assert healthy.has_drift is False
    assert drifted.has_drift is True


def test_has_drift_stays_missing_only_when_routes_were_dropped() -> None:
    """A suppressed route is explicitly NOT drift.

    `has_drift` no longer feeds the staleness verdict directly — the
    escalation term is `has_claim_anchored_drift` since the provenance
    split — but it is still the term that decides whether the caller
    SEES a `path_drift` block at all, so folding the suppressed bucket
    into it would resurrect the URL-route noise on the visibility side
    instead of the verdict side. Pinned so a future "make it visible"
    patch can't take the lazy route of widening `has_drift`."""
    routes_only = PathDriftReport(
        checked=(), missing=(), dropped_as_route=("/api/v1/events/presence",)
    )
    assert routes_only.has_drift is False


def test_dropped_as_route_ships_whenever_the_surface_gate_fires() -> None:
    """The half of the observability that DOES reach a tool caller.

    `memory_show` and `memory_search`'s expanded top hit gate the
    `path_drift` block on `has_drift or verified or expected_absent` and
    then emit `to_dict()` wholesale (`handlers/show.py`,
    `handlers/search.py`). So a memory that has any OTHER reason to emit
    the block carries the suppressed set along with it for free — the
    gap is confined to reports whose ONLY non-empty bucket is
    `dropped_as_route`, which no surface emits today.

    Pinned because the free half is exactly what a later gate-widening
    patch could break: rebuild the block field-by-field instead of from
    `to_dict()` and this silently drops back to zero reach.
    """
    mixed = PathDriftReport(
        checked=("/a",),
        missing=("/a",),
        dropped_as_route=("/admin/macros",),
    )
    # Verbatim the gate expression both handlers use.
    assert bool(mixed.has_drift or mixed.verified or mixed.expected_absent) is True
    assert mixed.to_dict()["dropped_as_route"] == ["/admin/macros"]

    # ...and the confined gap, stated as the gate sees it. This asserts
    # the GATE's arithmetic, not that the gap is desirable: it is the
    # defect the reach note on `PathDriftReport` documents.
    routes_only = PathDriftReport(
        checked=(), missing=(), dropped_as_route=("/admin/macros",)
    )
    assert (
        bool(
            routes_only.has_drift or routes_only.verified or routes_only.expected_absent
        )
        is False
    )
    assert routes_only.to_dict()["dropped_as_route"] == ["/admin/macros"]


def test_verified_paths_match_after_extractor_normalises_body_candidate(
    tmp_path: Path,
) -> None:
    """The audit caught an asymmetry: body candidates pass through
    `_normalize_candidate` (which trims trailing punctuation) before
    the `_normalize_for_compare` set-membership check, but
    `verified_paths` only went through the latter. So a perfectly
    valid attestation like `verified_paths=["/path/to/foo,"]` (with a
    trailing comma copied from prose) failed to match a body
    candidate `/path/to/foo` (already trimmed). After the fix both
    sides go through the same trim/validate pipeline."""
    real = tmp_path / "config"
    real.mkdir()
    body = f"see `{real}` for the layout"
    # Caller attests with a trailing comma — what naturally happens
    # when copying a path out of prose.
    report = detect_path_drift(
        body,
        verified_paths=[f"{real},"],
    )
    assert tuple(report.verified) == (str(real),)
    # And the inverse — without the fix, the verified set would have
    # been empty and `verified` would be `()`.
    assert report.has_drift is False


# ---------------------------------------------------------------------------
# Backtick-extraction edge cases — the validator runs against any string
# inside backticks, so it has to defend against shapes the bare regex
# already filters out (URLs, SSH remotes).
# ---------------------------------------------------------------------------


def test_url_inside_backticks_is_rejected() -> None:
    """A URL that the author wrote in backticks (`https://...`) shouldn't
    be treated as a filesystem path."""
    body = "Docs at `https://example.com/docs/foo` for reference."
    report = detect_path_drift(body)
    assert report.checked == ()


def test_ssh_remote_inside_backticks_is_rejected() -> None:
    body = "Clone via `git@github.com:owner/repo.git` then build."
    report = detect_path_drift(body)
    assert report.checked == ()


def test_user_at_host_path_inside_backticks_is_rejected() -> None:
    body = "scp from `mattias@homelab:/etc/foo` to local."
    report = detect_path_drift(body)
    assert report.checked == ()


def test_arbitrary_text_inside_backticks_is_rejected() -> None:
    """Backticks frequently wrap code identifiers, not paths. Anything
    that isn't path-shaped (no leading /, ~/, or DRIVE:) drops out."""
    body = "Use `memory_search` to retrieve and `tokenize()` to split."
    report = detect_path_drift(body)
    assert report.checked == ()


# ---------------------------------------------------------------------------
# Windows paths — drive-letter prefix
# ---------------------------------------------------------------------------


def test_windows_path_in_backticks_detected() -> None:
    """The drive-prefix branch of the validator covers Windows paths even
    when the test runs on macOS — Path() handles them as POSIX-style on
    non-Windows, so existence will be False (we're checking shape, not
    going to find the path)."""
    body = "Lives at `C:\\Users\\me\\config.txt` on the laptop."
    report = detect_path_drift(body)
    assert any("C:" in c for c in report.checked)
    # Won't exist on a non-Windows test box, so it's flagged as missing.
    assert report.missing  # non-empty


def test_windows_forward_slash_path_in_backticks_detected() -> None:
    body = "See `D:/data/foo` on the dev box."
    report = detect_path_drift(body)
    assert any("D:" in c for c in report.checked)


# ---------------------------------------------------------------------------
# Bare path cap — the bare-loop break path
# ---------------------------------------------------------------------------


def test_bare_path_cap_reached(tmp_path: Path) -> None:
    """Many bare paths in a single body still cap at the limit."""
    paths = [tmp_path / f"q{i}" for i in range(20)]
    body = "see " + ", ".join(str(p) for p in paths) + " for details"
    report = detect_path_drift(body)
    assert len(report.checked) <= 8


# ---------------------------------------------------------------------------
# compute_verification_status — structural staleness verdict
# ---------------------------------------------------------------------------
#
# The "never" and "stale" branches are the load-bearing ones — they each
# populate `recommendation`, which is the field a retrieving model is
# expected to act on. The "fresh" branch carries `recommendation=None`
# so a consumer can branch on truthiness without timestamp arithmetic.


_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


def test_verification_never_when_last_verified_is_none() -> None:
    """A memory that has never been verified produces status='never'
    with a non-empty recommendation. This is the case that motivated
    the structural change — a `last_verified_at: null` timestamp was
    too easy for the consuming model to skim past."""
    status = compute_verification_status(None, now=_NOW)
    assert status.status == "never"
    assert status.last_verified_at is None
    assert status.age_days is None
    assert status.recommendation is not None
    assert "spot-check" in status.recommendation.lower()
    assert "memory_verify" in status.recommendation


def test_verification_fresh_within_window() -> None:
    """A recently-verified memory produces status='fresh' with no
    recommendation — the absence of a recommendation is the signal
    that no spot-check is needed."""
    last_verified = _NOW - timedelta(days=5)
    status = compute_verification_status(last_verified, now=_NOW)
    assert status.status == "fresh"
    assert status.age_days == 5
    assert status.recommendation is None


def test_verification_stale_past_default_window() -> None:
    """Past the default 30-day window, a verified memory flips to
    stale and gets a re-spot-check recommendation that names the
    age in days — making the staleness concrete in the response."""
    last_verified = _NOW - timedelta(days=45)
    status = compute_verification_status(last_verified, now=_NOW)
    assert status.status == "stale"
    assert status.age_days == 45
    assert status.recommendation is not None
    assert "45" in status.recommendation
    assert "memory_verify" in status.recommendation


def test_verification_boundary_at_threshold_is_fresh() -> None:
    """Exactly at the threshold boundary the memory is still fresh —
    the stale window is strictly greater-than, so a memory with
    `last_verified_at == now - 30 days` reads as fresh and only flips
    to stale once it crosses the threshold by any measurable amount.
    The audit reframed this from the previous "stale at the boundary"
    semantic because the calendar reading of "fresh for 30 days, then
    stale" naturally means "stale starts on day 31", and the previous
    behaviour produced a midnight-UTC verdict flip on day 30 instead.
    Pin the contract so a future tweak can't quietly invert the sign."""
    last_verified = _NOW - timedelta(days=DEFAULT_VERIFICATION_STALE_DAYS)
    status = compute_verification_status(last_verified, now=_NOW)
    assert status.status == "fresh"


def test_verification_just_over_threshold_is_stale() -> None:
    """One second past the threshold flips to stale — pairs with
    `boundary_at_threshold_is_fresh` to lock the strict-greater
    boundary on the seconds-resolution comparison."""
    threshold = DEFAULT_VERIFICATION_STALE_DAYS
    last_verified = _NOW - timedelta(days=threshold) - timedelta(seconds=1)
    status = compute_verification_status(last_verified, now=_NOW)
    assert status.status == "stale"


def test_verification_just_under_threshold_is_fresh() -> None:
    """One second under the threshold is still fresh."""
    threshold = DEFAULT_VERIFICATION_STALE_DAYS
    last_verified = _NOW - timedelta(days=threshold) + timedelta(seconds=1)
    status = compute_verification_status(last_verified, now=_NOW)
    assert status.status == "fresh"


def test_verification_naive_datetime_treated_as_utc() -> None:
    """A timezone-naive last_verified_at (legacy frontmatter, hand
    edits) doesn't blow up — it's treated as UTC for the comparison.
    Mirrors the convention search._recency_factor uses for the same
    reason."""
    naive = (_NOW - timedelta(days=10)).replace(tzinfo=None)
    status = compute_verification_status(naive, now=_NOW)
    assert status.status == "fresh"
    assert status.age_days == 10


def test_verification_zero_threshold_marks_everything_stale() -> None:
    """`stale_after_days=0` collapses the fresh window — every
    verified memory becomes stale immediately. Useful in tests
    that want the stale branch without sleeping."""
    last_verified = _NOW - timedelta(seconds=1)
    status = compute_verification_status(last_verified, now=_NOW, stale_after_days=0)
    assert status.status == "stale"


def test_verification_negative_threshold_clamped_to_zero() -> None:
    """A negative threshold is clamped to 0 rather than producing an
    inverted comparison. Cheap defensive guard so a config typo can't
    flip every memory to fresh."""
    last_verified = _NOW - timedelta(days=10)
    status = compute_verification_status(last_verified, now=_NOW, stale_after_days=-5)
    assert status.status == "stale"
    # The reported threshold is the clamped value, not the raw input —
    # consumers should see the actual cutoff used.
    assert status.stale_after_days == 0


def test_verification_future_timestamp_does_not_crash() -> None:
    """A clock skew that puts last_verified_at in the future shouldn't
    raise — age clamps at 0 and the memory reads as fresh."""
    last_verified = _NOW + timedelta(days=1)
    status = compute_verification_status(last_verified, now=_NOW)
    assert status.status == "fresh"
    assert status.age_days == 0


def test_verification_to_dict_shape_fresh() -> None:
    """Fresh memories serialise with `recommendation: None` so the
    consumer can branch on truthiness — null is the explicit
    "nothing to do" signal."""
    last_verified = _NOW - timedelta(days=2)
    status = compute_verification_status(last_verified, now=_NOW)
    payload = status.to_dict()
    assert payload["status"] == "fresh"
    assert payload["recommendation"] is None
    assert payload["last_verified_at"].endswith("Z")
    assert payload["age_days"] == 2
    assert payload["stale_after_days"] == DEFAULT_VERIFICATION_STALE_DAYS


def test_verification_to_dict_shape_never() -> None:
    """Never-verified serialises last_verified_at and age_days as
    null, with a populated recommendation. Same key set as the
    other branches — uniform shape lets consumers branch on the
    `status` field alone."""
    payload = compute_verification_status(None, now=_NOW).to_dict()
    assert payload["status"] == "never"
    assert payload["last_verified_at"] is None
    assert payload["age_days"] is None
    assert payload["recommendation"] is not None
    assert "stale_after_days" in payload


def test_verification_status_is_immutable_dataclass() -> None:
    """Frozen dataclass — accidental mutation by a consumer would
    silently corrupt the verdict on subsequent reads if it weren't."""
    status = compute_verification_status(None, now=_NOW)
    assert isinstance(status, VerificationStatus)
    import dataclasses as _dc

    try:
        status.status = "fresh"  # type: ignore[misc]
    except _dc.FrozenInstanceError:
        return
    raise AssertionError("VerificationStatus should be frozen")


# ---------------------------------------------------------------------------
# verdict_from_signals — the rollup ladder, and the stale-demotion carve-out
# ---------------------------------------------------------------------------
#
# Until 3.30.0 a `never`/`stale` verification status pre-empted both
# drift inputs outright, which made the verdict a CONSTANT FUNCTION at
# the shipped default: every memory past the 30-day window read
# `spot_check_required` regardless of what the drift legs found.
# `bench/rot` measured it as arithmetically identical to `always_flag`
# (flag rate 100%, Youden's J = 0.000 in every class and both windows).
#
# The ladder now lets a MEASUREMENT override the calendar PROXY, and
# these tests pin both halves of that — the demotion, and every guard
# that stops it from becoming a false green. The guards are the
# load-bearing part: a demotion that fired on absent evidence would be
# the exact defect class the verdict exists to expose.
#
# Negative control: deleting the `commit_drift_count == 0` condition
# from the final branch (i.e. demoting on any clean stale memory) flips
# `test_verdict_stale_does_not_demote_when_commit_leg_silent` and
# `test_verdict_stale_does_not_demote_on_path_evidence_alone` from
# passing to failing; deleting the `status == "never"` early return
# flips `test_verdict_never_never_demotes`.


@pytest.mark.parametrize(
    ("status", "path_missing", "commit_count", "expected"),
    [
        # Calendar-fresh: unchanged in every combination.
        ("fresh", 0, 0, "fresh"),
        ("fresh", 0, None, "fresh"),
        ("fresh", 0, 3, "spot_check_recommended"),
        ("fresh", 2, None, "spot_check_recommended"),
        # `never`: no anchor exists, so nothing can stand the calendar down.
        ("never", 0, None, "spot_check_required"),
        ("never", 0, 0, "spot_check_required"),
        ("never", 2, 0, "spot_check_required"),
        # `stale`: demotes ONLY on a measured-zero commit leg.
        ("stale", 0, 0, "fresh"),
        ("stale", 0, None, "spot_check_required"),
        ("stale", 0, 3, "spot_check_required"),
        ("stale", 2, 0, "spot_check_required"),
    ],
)
def test_verdict_ladder(
    status: str, path_missing: int, commit_count: int | None, expected: str
) -> None:
    """Pin the full cross-product of the three signals.

    Table-driven rather than one test per branch because the ladder's
    correctness IS the interaction — reading the eleven rows together
    is what shows that the only cell which moved is
    `stale`/no-path-drift/commit-zero."""
    assert (
        verdict_from_signals(
            status=status,
            path_drift_missing=path_missing,
            commit_drift_count=commit_count,
        )
        == expected
    )


def test_verdict_stale_demotes_on_measured_zero_commit_drift() -> None:
    """The fix itself: a memory well past its freshness window whose
    anchored paths saw ZERO commits since its own last verification
    reads `fresh`.

    This is the branch that stops the shipped default from being a
    constant function. `commit_drift_count == 0` is not "we didn't
    look" — `compute_commit_drift` returns None for that — it is
    "we counted commits touching this memory's own claim anchors since
    its `last_verified_at`, and there were none"."""
    now = datetime.now(timezone.utc)
    stale = compute_verification_status(now - timedelta(days=400), now=now)
    assert stale.status == "stale"
    assert (
        compute_staleness_verdict(
            verification=stale, path_drift_missing=0, commit_drift_count=0
        )
        == "fresh"
    )


def test_verdict_stale_does_not_demote_when_commit_leg_silent() -> None:
    """`None` means the leg could not ask — no origin repo, caller in a
    different repo, git unreachable, or no claim anchor landing here.

    Absence of evidence is not evidence of freshness. This is the
    branch that keeps preference/lesson/reflection memories — the ~36%
    of real bodies `bench/claims.py` grades as judgement rather than
    checkable claim — pinned at `spot_check_required`, which is the
    class `compute_commit_drift` explicitly exempts and hands to the
    calendar backstop."""
    now = datetime.now(timezone.utc)
    stale = compute_verification_status(now - timedelta(days=400), now=now)
    assert (
        compute_staleness_verdict(
            verification=stale, path_drift_missing=0, commit_drift_count=None
        )
        == "spot_check_required"
    )


def test_verdict_stale_does_not_demote_on_path_evidence_alone() -> None:
    """Path existence must not lower the verdict on its own.

    "The cited file still exists" answers a weaker question than
    "nothing touched it since you checked", and the 2026-07-26 store
    sweep put a number on how much weaker: of 15 missing-path alerts
    raised from paths scraped out of body prose, ~0 were real drift,
    against 3 of 3 for anchored attestations. So a clean path leg with
    a silent commit leg stays `spot_check_required`."""
    now = datetime.now(timezone.utc)
    stale = compute_verification_status(now - timedelta(days=400), now=now)
    # Clean path leg (nothing missing), commit leg silent.
    assert (
        verdict_from_signals(
            status=stale.status, path_drift_missing=0, commit_drift_count=None
        )
        == "spot_check_required"
    )


def test_verdict_never_never_demotes() -> None:
    """`never` is unconditional, belt-and-braces against a future
    `compute_commit_drift` that learns to emit a count without a
    `last_verified_at` anchor. Without an anchor there is no "since
    when", so a zero count would be meaningless rather than
    reassuring."""
    now = datetime.now(timezone.utc)
    never = compute_verification_status(None, now=now)
    assert never.status == "never"
    assert (
        compute_staleness_verdict(
            verification=never, path_drift_missing=0, commit_drift_count=0
        )
        == "spot_check_required"
    )


def test_verdict_drift_still_raises_a_stale_memory() -> None:
    """The demotion must not weaken the raise path: a stale memory with
    real drift on either leg stays at `spot_check_required`, not the
    milder `spot_check_recommended` a calendar-fresh memory would get."""
    now = datetime.now(timezone.utc)
    stale = compute_verification_status(now - timedelta(days=400), now=now)
    for missing, count in ((1, 0), (0, 5), (3, 9)):
        assert (
            compute_staleness_verdict(
                verification=stale,
                path_drift_missing=missing,
                commit_drift_count=count,
            )
            == "spot_check_required"
        )


def test_compute_staleness_verdict_delegates_to_primitive() -> None:
    """`compute_staleness_verdict` must stay a thin wrapper.

    The two emission sites (`verify.compute_staleness_verdict` and
    `_response.attach_commit_drift_counts`) previously restated the
    ladder against shared constants, guarded only by "mirror the gate
    above" comments — the arrangement that lets a semantic change reach
    one surface and not the other. Sharing the primitive is what makes
    them structurally incapable of diverging; this pins that they still
    agree cell-for-cell."""
    now = datetime.now(timezone.utc)
    for stamp in (None, now - timedelta(days=1), now - timedelta(days=400)):
        verification = compute_verification_status(stamp, now=now)
        for missing in (0, 2):
            for count in (None, 0, 4):
                assert compute_staleness_verdict(
                    verification=verification,
                    path_drift_missing=missing,
                    commit_drift_count=count,
                ) == verdict_from_signals(
                    status=verification.status,
                    path_drift_missing=missing,
                    commit_drift_count=count,
                )


# ---------------------------------------------------------------------------
# compute_commit_drift — repo-aware staleness
# ---------------------------------------------------------------------------
#
# The function only emits a verdict when every precondition holds: the
# memory has been verified, has an origin.repo, the caller is in a repo,
# the repos match, and git was reachable. Each test below pins one
# precondition false so the silence-on-no-signal contract is locked in;
# the "happy path" tests then check the two non-null branches (`clean`
# and `drift`) against a real fixture repo.

_REMOTE = "git@github.com:example/foo.git"


def test_commit_drift_returns_none_when_never_verified(tmp_path: Path) -> None:
    """No anchor to count from — the verification.status="never" branch
    already maxes the alarm; emitting commit_drift here would be noise."""
    result = compute_commit_drift(
        last_verified_at=None,
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
    )
    assert result is None


def test_commit_drift_returns_none_when_memory_has_no_origin_repo() -> None:
    """A memory written outside any repo has no project identity to anchor
    against — silence rather than guess."""
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        memory_origin_repo=None,
        caller_origin=Origin(cwd="/projects/foo", repo=_REMOTE, branch="main"),
    )
    assert result is None


def test_commit_drift_returns_none_when_caller_origin_is_none() -> None:
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=None,
    )
    assert result is None


def test_commit_drift_returns_none_when_caller_not_in_a_repo() -> None:
    """Origin with no repo means the caller is outside any project — the
    auto-scope filter already drops cross-project memories on this branch,
    and commit_drift has nothing to count against."""
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd="/projects/foo", repo=None, branch=None),
    )
    assert result is None


def test_commit_drift_returns_none_when_repos_dont_match(tmp_path: Path) -> None:
    """Memory written from repo A, caller in repo B — the auto-scope
    filter already keeps that memory out of search results, but
    memory_show is unrestricted by id and could still surface it.
    Stay silent rather than count commits in the wrong repo."""
    other_remote = "git@github.com:example/other.git"
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=other_remote, branch="main"),
    )
    assert result is None


def test_commit_drift_returns_none_when_git_unreachable(tmp_path: Path) -> None:
    """Caller cwd isn't a git repo — no .git, no log to count. The
    function bails to None rather than reporting a misleading clean/drift."""
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
    )
    assert result is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_status_clean_when_no_commits_after_verify(
    tmp_path: Path,
) -> None:
    """Repo exists and matches, the verify anchor is after the last commit:
    status is 'clean', count is 0, recommendation is None. The clean
    branch is the positive evidence the consumer needs to trust the
    calendar verification. The fixture commit TOUCHES the cited file
    (`_commit_touching`, not the old `--allow-empty` `_commit_at`):
    since the quiescent branch classifies applicability, a cited path
    no commit ever touched is a PHANTOM and reads None — clean/0 is
    reserved for a memory whose anchor is real."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_touching(tmp_path, "older", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="claims about notes.md hold",
    )
    assert result is not None
    assert result.status == "clean"
    assert result.commits_since_verify == 0
    assert result.recommendation is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_status_drift_when_commits_after_verify(
    tmp_path: Path,
) -> None:
    """The load-bearing case: commits touching the memory's cited path
    landed since the last verify, so the calendar may say 'fresh' but the
    claims' ground truth has moved. Status is 'drift', count matches,
    recommendation includes the count and actionable next steps
    (memory_verify / memory_update)."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "anchor", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _commit_touching(
        tmp_path, "after-1", when=datetime(2026, 2, 1, tzinfo=timezone.utc)
    )
    _commit_touching(
        tmp_path, "after-2", when=datetime(2026, 2, 2, tzinfo=timezone.utc)
    )
    _commit_touching(
        tmp_path, "after-3", when=datetime(2026, 2, 3, tzinfo=timezone.utc)
    )
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="claims about notes.md hold",
    )
    assert result is not None
    assert result.status == "drift"
    assert result.commits_since_verify == 3
    assert result.recommendation is not None
    assert "3 commits" in result.recommendation
    assert "memory_verify" in result.recommendation
    assert "memory_update" in result.recommendation


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_recommendation_singular_for_one_commit(
    tmp_path: Path,
) -> None:
    """Off-by-one cosmetic: pluralisation should be correct so the
    rendered recommendation reads as English, not template-debug
    output ('1 commits' would be a tell)."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "anchor", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _commit_touching(tmp_path, "after", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="claims about notes.md hold",
    )
    assert result is not None
    assert result.commits_since_verify == 1
    assert result.recommendation is not None
    assert "1 commit touching" in result.recommendation
    assert "1 commits" not in result.recommendation


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_to_dict_shape_clean(tmp_path: Path) -> None:
    """JSON shape is uniform across status values so consumers can branch
    on `status` alone without an existence check on every field. The
    fixture commit touches the cited `notes.md` so the quiescent
    applicability classification reads the anchor as real, not phantom."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_touching(tmp_path, "older", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="claims about notes.md hold",
    )
    assert result is not None
    payload = result.to_dict()
    assert payload == {
        "status": "clean",
        "commits_since_verify": 0,
        "recommendation": None,
        # No `verified_head` on the call: the author-date axis, named.
        "basis": "author-date",
    }


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_normalised_repo_url_still_matches(tmp_path: Path) -> None:
    """Memory's origin.repo is the SSH form; caller's is HTTPS. They
    describe the same project — repos_match should normalise away the
    surface form and commit_drift should fire."""
    _init_repo_with_remote(tmp_path, remote="https://github.com/example/foo.git")
    _commit_at(tmp_path, "anchor", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _commit_touching(tmp_path, "after", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo="git@github.com:example/foo.git",
        caller_origin=Origin(
            cwd=str(tmp_path),
            repo="https://github.com/example/foo.git",
            branch="main",
        ),
        body="claims about notes.md hold",
    )
    assert result is not None
    assert result.status == "drift"
    assert result.commits_since_verify == 1


# ---------------------------------------------------------------------------
# Claim-anchored commit drift — the anchor derivation and the exemption
# policy (a memory citing no paths cannot commit-drift; measured 100%
# false-positive on the dogfood store before the gate: 12/12 at 3.13.0,
# 24/24 at 3.16.0)
# ---------------------------------------------------------------------------


def test_anchor_paths_empty_for_claimless_body() -> None:
    """A preference/lesson body citing nothing path-shaped has no claim
    anchors — the claim-kind signal that exempts it from commit drift."""
    assert (
        commit_drift_anchor_paths(
            "prefer cost checkpoints on long autonomous runs; ask before "
            "burning tokens",
        )
        == ()
    )


def test_anchor_paths_union_attested_cited_and_relative() -> None:
    """Anchors = verified_paths + absolute/~ citations + repo-relative
    citations, deduplicated, attestations first."""
    anchors = commit_drift_anchor_paths(
        "see src/pkg/mod.py:42 and ~/.config/app.toml for the flag",
        verified_paths=["docs/spec.md", "src/pkg/mod.py"],
    )
    assert anchors[0] == "docs/spec.md"
    assert set(anchors) == {"docs/spec.md", "src/pkg/mod.py", "~/.config/app.toml"}


def test_relative_citation_regex_rejects_prose_and_urls() -> None:
    """The conservative shape rules: acronym pairs, abbreviations,
    version strings, and URL tokens must not become anchors — a URL
    anchoring a memory would re-open the exact false-positive class the
    policy exists to close."""
    noise = (
        "CI/CD and TCP/IP pipelines, e.g. the U.S. case, i.e. at 3.16.0 "
        "or v3.16.0rc1, docs.python.org/3/library/re.html and "
        "pypi.org/simple/pkg routes"
    )
    assert [m.group(1) for m in _RELATIVE_CITATION_RE.finditer(noise)] == []


def test_relative_citation_regex_accepts_real_citations() -> None:
    got = [
        m.group(1)
        for m in _RELATIVE_CITATION_RE.finditer(
            "pinned in `tests/test_server.py`, bump CHANGELOG.md and "
            "plugin/.claude-plugin/plugin.json; details in "
            "src/pkg/eval.py:1228."
        )
    ]
    assert got == [
        "tests/test_server.py",
        "CHANGELOG.md",
        "plugin/.claude-plugin/plugin.json",
        "src/pkg/eval.py",
    ]


def test_relative_citation_regex_ignores_dash_leading_filename() -> None:
    """Fix (c): the lookbehind bars a match starting right after a dash, so
    a leading-dash token spawns no phantom relative anchor, and a dash
    INSIDE an absolute path spawns no bonus relative anchor. Pre-fix the
    lookbehind omitted `-`: `-leading-dash.md` truncated to the anchor
    `leading-dash.md`, and `/opt/claude-code/src/cli.ts` spawned the phantom
    `code/src/cli.ts`.
    """
    assert [
        m.group(1)
        for m in _RELATIVE_CITATION_RE.finditer("prefix -leading-dash.md tail")
    ] == []
    assert [
        m.group(1)
        for m in _RELATIVE_CITATION_RE.finditer("/opt/claude-code/src/cli.ts")
    ] == []


def test_relative_citation_regex_still_matches_internal_dash_filename() -> None:
    """The dash lookbehind must not break a hyphenated filename cited from a
    clean opener — the dash lives INSIDE the match, only the START position
    is guarded. `my-mod.py` still anchors whole."""
    got = [
        m.group(1)
        for m in _RELATIVE_CITATION_RE.finditer("pinned in src/my-mod.py today")
    ]
    assert got == ["src/my-mod.py"]


def test_anchor_paths_caps_relative_citations() -> None:
    """A pathological body citing hundreds of files stays bounded — the
    cap only DROPS anchors (never invents), and the anchor set staying
    non-empty preserves the memory's anchored classification."""
    body = " ".join(f"dir{i}/file{i}.py" for i in range(200))
    anchors = commit_drift_anchor_paths(body)
    assert len(anchors) == _MAX_ANCHOR_CITATIONS


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_none_for_untethered_memory_despite_commits(
    tmp_path: Path,
) -> None:
    """THE policy regression test: commits landed since verify, but the
    memory cites no paths — the bare repo-wide count must NOT surface as
    drift (it says nothing about a claim-less memory). Reverting the
    claim-anchored gate in compute_commit_drift makes this fail with a
    'drift'/2 result."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "anchor", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _commit_at(tmp_path, "after-1", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    _commit_at(tmp_path, "after-2", when=datetime(2026, 2, 2, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="a workflow preference that merely originated in this repo",
    )
    assert result is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_weak_tier_keeps_zero_for_an_empty_patch_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An EMPTY stream is the legitimate no-touch window: the commits
    diffed nothing under the governed specs, so no claim fires and the
    governed half contributes an honest zero — `([], set())`, never a
    demotion to the incumbent count."""
    from bettermemory import verify as verify_mod
    from bettermemory.claims import load_claims

    monkeypatch.setattr(verify_mod, "commit_patch_stream", lambda *a, **k: "")
    drifted, implicated = verify_mod._weak_tier_evaluation(
        tmp_path, ["deadbeef"], ["src/x.py"], load_claims(["src/x.py::foo"]), None
    )
    assert drifted == []
    assert implicated == set()


def test_weak_tier_demotes_when_stream_indexes_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NON-empty stream whose diff headers never parsed (mojibake, an
    unrecognised format, or a merge-commit window that lists commits
    without diff bodies) indexes no files at all. Pre-fix every claim
    then read "untouched" and the governed half contributed a silent
    zero. The evaluation must demote to the incumbent per-file count
    instead — `None` for the implicated set, the caller's fallback."""
    from bettermemory import verify as verify_mod
    from bettermemory.claims import COMMIT_MARK, load_claims

    claims = load_claims(["src/x.py::foo"])
    for stream in (
        "garbage that is not a diff\nmore \ufffd bytes\n",
        f"{COMMIT_MARK}deadbeef\n{COMMIT_MARK}cafebabe\n",
    ):
        monkeypatch.setattr(
            verify_mod, "commit_patch_stream", lambda *a, _s=stream, **k: _s
        )
        drifted, implicated = verify_mod._weak_tier_evaluation(
            tmp_path, ["deadbeef", "cafebabe"], ["src/x.py"], claims, None
        )
        assert drifted == []
        assert implicated is None, stream


def test_weak_tier_demotes_on_a_hunk_count_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`build_binding_index` counts a hunk whose line tally disagrees with
    its header instead of raising; a window that produced one cannot be
    trusted to have indexed the claim's binding, so the weak tier steps
    aside for the incumbent count rather than passing on a partial read."""
    from bettermemory import verify as verify_mod
    from bettermemory.claims import COMMIT_MARK, load_claims

    stream = (
        f"{COMMIT_MARK}abc1234\n"
        "diff --git a/src/x.py b/src/x.py\n"
        "--- a/src/x.py\n"
        "+++ b/src/x.py\n"
        "@@ -1,2 +1,1 @@\n"
        "-def foo():\n"
        "+def foo(): pass\n"
    )
    monkeypatch.setattr(verify_mod, "commit_patch_stream", lambda *a, **k: stream)
    drifted, implicated = verify_mod._weak_tier_evaluation(
        tmp_path, ["abc1234"], ["src/x.py"], load_claims(["src/x.py::foo"]), None
    )
    assert drifted == []
    assert implicated is None


def test_commit_drift_none_when_anchors_all_escape_repo(tmp_path: Path) -> None:
    """Anchors exist but none resolve inside the caller's repo (remote
    host paths, home-dir configs): the claims live elsewhere, so this
    repo's commits still can't invalidate them — None, not the
    unfiltered-fallback the legacy composition applied."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "anchor", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _commit_at(tmp_path, "after", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="the router config lives at /data/compose/.env on the board",
        verified_paths=["~/.claude.json"],
    )
    assert result is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_clean_when_cited_path_untouched(tmp_path: Path) -> None:
    """The claim-anchored discriminator: commits landed, but none touched
    the cited file — the world the memory checked hasn't moved, so the
    verdict is clean (0), not drift-by-association."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "anchor", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _commit_touching(
        tmp_path,
        "cited-file-baseline",
        when=datetime(2026, 1, 2, tzinfo=timezone.utc),
        filename="notes.md",
    )
    _commit_touching(
        tmp_path,
        "unrelated-churn",
        when=datetime(2026, 2, 1, tzinfo=timezone.utc),
        filename="other.md",
    )
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="claims about notes.md hold",
    )
    assert result is not None
    assert result.status == "clean"
    assert result.commits_since_verify == 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_none_when_anchors_escape_and_repo_quiescent(
    tmp_path: Path,
) -> None:
    """The QUIESCENT half of the all-escape cross product: same memory as
    `test_commit_drift_none_when_anchors_all_escape_repo`, but the repo
    has seen NO commits since the verify. Pre-fix the `count > 0` gate
    skipped anchor resolution entirely and minted clean/0 — which the
    stale-plus-zero demotion consumed as a measurement, so a
    calendar-stale memory citing only remote-host paths read `fresh`
    exactly as long as the repo sat still, then flipped to
    `spot_check_required` on the first unrelated commit. Applicability
    must not be keyed to unrelated repo activity: None on both sides."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "older", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="the router config lives at /data/compose/.env on the board",
        verified_paths=["~/.claude.json"],
    )
    assert result is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_none_for_phantom_citation_when_repo_quiescent(
    tmp_path: Path,
) -> None:
    """Quiescent parity for the phantom rule: a cited path no commit in
    history ever touched is not-applicable on the positive branch
    (`test_commit_drift_none_for_phantom_subroot_citation`), and a zero
    repo-wide count must classify it the same way rather than minting
    clean/0 off an anchor that was never resolved."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "older", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="claims about notes.md hold",
    )
    assert result is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_clean_when_quiescent_and_governed_claim_real(
    tmp_path: Path,
) -> None:
    """The governed half of the quiescent classification: a memory whose
    ONLY anchor is a declared claim (untethered body) is fully governed
    — `claim_paths` joins the spec set exactly as in
    `_resolve_with_claims` — so a real, untouched claim path keeps the
    affirmative clean/0 in a quiescent repo."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_touching(tmp_path, "older", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="a workflow note that cites nothing path-shaped",
        claims=["notes.md"],
    )
    assert result is not None
    assert result.status == "clean"
    assert result.commits_since_verify == 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_quiescent_deleted_but_real_anchor_stays_clean(
    tmp_path: Path,
) -> None:
    """Guard on the quiescent classification: a since-DELETED cited file
    is REAL, not phantom — its add + delete commits keep it in the
    touching log — so it stays clean/0, never None. Mirrors
    `test_resolve_commit_drift_count_zero_for_deleted_but_real_anchor`
    for the zero-count branch: the probe must read history, not the
    current tree, or every memory about a deliberately removed file
    would lose its measurement the moment the repo goes quiet."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_touching(
        tmp_path,
        "add gone.py",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
        filename="gone.py",
    )
    (tmp_path / "gone.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    _commit_at(
        tmp_path, "delete gone.py", when=datetime(2026, 1, 10, tzinfo=timezone.utc)
    )
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="gone.py was removed on purpose; see its final revision",
    )
    assert result is not None
    assert result.status == "clean"
    assert result.commits_since_verify == 0


def test_resolve_commit_drift_count_falls_back_on_git_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git can't answer (cwd is not a repo): keep the conservative
    unfiltered count rather than silently exempting a possibly-drifted
    memory — infrastructure failure must never widen the exemption.
    The discovery ceiling is what MAKES cwd not-a-repo when basetemp
    itself sits under a checkout — without it, git resolves the
    enclosing repo, `notes.md` reads as a confirmed phantom ([]), and
    the fallback collapses into a None exemption."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    assert (
        resolve_commit_drift_count(
            cwd=tmp_path,
            since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            unfiltered=7,
            anchors=("notes.md",),
        )
        == 7
    )


def test_resolve_commit_drift_count_none_for_empty_anchors(tmp_path: Path) -> None:
    assert (
        resolve_commit_drift_count(
            cwd=tmp_path,
            since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            unfiltered=7,
            anchors=(),
        )
        is None
    )


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_resolve_commit_drift_count_none_for_root_only_anchors(
    tmp_path: Path,
) -> None:
    """A memory whose only path-shaped claim is the repo root ("the
    project lives at X") anchors nothing discriminating — the root
    pathspec would match every commit, i.e. the unfiltered count in
    disguise. Policy: not-applicable, same as anchors that all escape
    the repo. Regression shape: a live location memory read 149 commits
    of "drift" — exactly the unfiltered count — through its root cite
    while its discriminating anchors read 0."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "anchor", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _commit_at(tmp_path, "after", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert (
        resolve_commit_drift_count(
            cwd=tmp_path,
            since=datetime(2026, 1, 15, tzinfo=timezone.utc),
            unfiltered=7,
            anchors=(str(tmp_path),),
        )
        is None
    )


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_root_citation_does_not_drag_in_unrelated_churn(
    tmp_path: Path,
) -> None:
    """Body cites the repo root AND a specific file, and unrelated churn
    landed after the verify. The root cite must not widen the anchor set
    to the whole repo — only commits touching the discriminating anchor
    count, so the verdict stays clean."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "anchor", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _commit_touching(
        tmp_path,
        "cited-file-baseline",
        when=datetime(2026, 1, 2, tzinfo=timezone.utc),
        filename="notes.md",
    )
    _commit_touching(
        tmp_path,
        "unrelated-churn",
        when=datetime(2026, 2, 1, tzinfo=timezone.utc),
        filename="other.md",
    )
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body=f"the project lives at {tmp_path} and claims about notes.md hold",
    )
    assert result is not None
    assert result.status == "clean"
    assert result.commits_since_verify == 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_resolve_commit_drift_count_author_date_ignores_committer_inflation(
    tmp_path: Path,
) -> None:
    """Rebase-inflation with a POSITIVE unfiltered count, now counted in
    AUTHOR-date space. Two rebased commits touch the cited file with author
    date BEFORE `since` and committer date rewritten AFTER it — the shape
    `git pull --rebase` leaves on disk. The old implementation counted them
    on COMMITTER date via `git rev-list --since` (getting 2) and leaned on a
    `min(unfiltered, filtered)` clamp to bound the inflation (to 1 here). The
    author-date filter ignores the committer rewrite entirely and reports the
    exact truth: NONE of the anchor's commits were authored after `since`, so
    0. This supersedes the former clamp behavior — the clamp is gone because
    an author-date subset can never exceed the author-date unfiltered count.
    Pre-fix this returned the clamped 1; the author-date count returns 0.
    """
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_touching(
        tmp_path, "baseline", when=datetime(2020, 1, 1, tzinfo=timezone.utc)
    )
    # Two rebased commits touching the cited file: authored in 2020 (before
    # `since`), committer date rewritten to 2026 (after it) — the exact shape
    # `git pull --rebase` leaves on disk.
    _commit_touching_split(
        tmp_path,
        "rebased-1",
        author_when=datetime(2020, 2, 1, tzinfo=timezone.utc),
        committer_when=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    _commit_touching_split(
        tmp_path,
        "rebased-2",
        author_when=datetime(2020, 3, 1, tzinfo=timezone.utc),
        committer_when=datetime(2026, 6, 2, tzinfo=timezone.utc),
    )
    # The committer-date path filter over the two rebased commits returned 2
    # (min-clamped to unfiltered=1 by the old code); the author-date count is
    # 0 — no notes.md commit was AUTHORED after `since`.
    result = resolve_commit_drift_count(
        cwd=tmp_path,
        since=datetime(2025, 1, 1, tzinfo=timezone.utc),
        unfiltered=1,
        anchors=("notes.md",),
    )
    assert result == 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_resolve_commit_drift_count_author_date_exact_under_unrelated_churn(
    tmp_path: Path,
) -> None:
    """The residual the min() clamp could NOT fix, pinned. Post-verify churn
    on OTHER files raises the author-date `unfiltered` count, while a rebase
    inflates the ANCHOR file's committer dates past `since` (author dates
    preserved before it). The old committer-date `--since` filter counted the
    two rebased anchor commits as 2; because `unfiltered` (3) exceeded that,
    the `min(unfiltered, filtered)` clamp did NOT bind and 2 was reported —
    yet the true author-date anchored count is 0 (no anchor commit was
    authored after `since`). The author-date filter reports the exact 0.
    Must FAIL against the min-clamped source, which returns 2.
    """
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    # Anchor-file baseline, authored well before `since`, committer preserved
    # — makes `notes.md` a REAL anchor (present in history, not a phantom).
    _commit_touching(
        tmp_path, "baseline", when=datetime(2020, 1, 1, tzinfo=timezone.utc)
    )
    # Two REBASED commits touching the anchor: authored before `since`,
    # committer date rewritten far after it (the `git pull --rebase` shape).
    _commit_touching_split(
        tmp_path,
        "rebased-1",
        author_when=datetime(2020, 2, 1, tzinfo=timezone.utc),
        committer_when=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    _commit_touching_split(
        tmp_path,
        "rebased-2",
        author_when=datetime(2020, 3, 1, tzinfo=timezone.utc),
        committer_when=datetime(2026, 6, 2, tzinfo=timezone.utc),
    )
    # Post-verify churn on an UNRELATED file — genuinely authored after
    # `since`. These lift the author-date `unfiltered` count to 3 without
    # touching the anchor, so the clamp ceiling sits ABOVE the inflated
    # committer-date filtered count (2) and cannot bind.
    _commit_touching(
        tmp_path,
        "other-1",
        when=datetime(2025, 6, 1, tzinfo=timezone.utc),
        filename="other.md",
    )
    _commit_touching(
        tmp_path,
        "other-2",
        when=datetime(2025, 7, 1, tzinfo=timezone.utc),
        filename="other.md",
    )
    _commit_touching(
        tmp_path,
        "other-3",
        when=datetime(2025, 8, 1, tzinfo=timezone.utc),
        filename="other.md",
    )
    result = resolve_commit_drift_count(
        cwd=tmp_path,
        since=datetime(2025, 1, 1, tzinfo=timezone.utc),
        # Author-date unfiltered truth: the 3 other.md commits authored after
        # `since` (the anchor's rebased commits were authored in 2020).
        unfiltered=3,
        anchors=("notes.md",),
    )
    assert result == 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_none_for_phantom_subroot_citation(tmp_path: Path) -> None:
    """Fix (b): a body citing an existing file by a SUB-ROOT path
    (`pkg/search.py`, dropping the real `src/` prefix) anchors a
    repo-relative pathspec no commit ever touched — `resolve_repo_pathspecs`
    resolves it lexically, never checking existence. Commits landed after
    verify (positive unfiltered) but the phantom anchor matches none of
    them: pre-fix this minted an affirmative clean/0; the phantom probe
    returns None (not-applicable). Live repro: `handlers/search.py` read
    clean while `src/bettermemory/handlers/search.py` read drift.
    """
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_touching(
        tmp_path,
        "add real file",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
        filename="src/pkg/search.py",
    )
    _commit_touching(
        tmp_path,
        "churn real file",
        when=datetime(2026, 2, 1, tzinfo=timezone.utc),
        filename="src/pkg/search.py",
    )
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="the handler logic in pkg/search.py is the hot path",
    )
    assert result is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_none_for_phantom_spaced_filename_citation(
    tmp_path: Path,
) -> None:
    """Fix (b): a spaced filename (`docs/My Notes.md`) can't be matched
    across the space, so the relative-citation regex anchors only the tail
    `Notes.md` — a repo-relative pathspec no commit ever touched. With
    commits landed after verify (positive unfiltered), pre-fix minted a
    false clean/0 off that phantom tail; the phantom probe returns None.
    """
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_touching(
        tmp_path,
        "add spaced file",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
        filename="docs/My Notes.md",
    )
    _commit_touching(
        tmp_path,
        "churn spaced file",
        when=datetime(2026, 2, 1, tzinfo=timezone.utc),
        filename="docs/My Notes.md",
    )
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="see docs/My Notes.md for the rationale",
    )
    assert result is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_resolve_commit_drift_count_zero_for_deleted_but_real_anchor(
    tmp_path: Path,
) -> None:
    """Guard on fix (b): the phantom probe must NOT swallow a since-DELETED
    cited file. A file added then removed BEFORE the verify is untouched
    afterward (filtered 0) yet is REAL — its add + delete commits keep it in
    history — so it stays clean/0, never None. This is why the probe uses
    `rev-list ... HEAD` (history) not `git ls-files` (current tree only),
    which would have mis-flagged the deleted file as phantom.
    """
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_touching(
        tmp_path,
        "add gone.py",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
        filename="gone.py",
    )
    # Delete the cited file BEFORE the verify instant.
    (tmp_path / "gone.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    _commit_at(
        tmp_path, "delete gone.py", when=datetime(2026, 1, 10, tzinfo=timezone.utc)
    )
    # Post-verify churn on an unrelated file → unfiltered > 0.
    _commit_touching(
        tmp_path,
        "unrelated churn",
        when=datetime(2026, 3, 1, tzinfo=timezone.utc),
        filename="other.py",
    )
    result = resolve_commit_drift_count(
        cwd=tmp_path,
        since=datetime(2026, 2, 1, tzinfo=timezone.utc),
        unfiltered=1,
        anchors=("gone.py",),
    )
    assert result == 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_resolves_repo_toplevel_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Perf pin on the memory_show hot path: one `compute_commit_drift`
    call that reaches the claim-anchored narrowing forks
    ``git rev-parse --show-toplevel`` exactly ONCE. The repo root is
    resolved up front and threaded into `resolve_commit_drift_count`
    (mirroring the batch surfaces — `health._compute_commit_drift_debt`
    and `_response.attach_commit_drift_counts`); pre-fix the call left
    ``toplevel`` unset, so `resolve_repo_pathspecs` and
    `commit_author_timestamps_touching_pathspecs` EACH re-derived it —
    two rev-parse forks per retrieval. Counted at the ``origin._git``
    seam (the module git runner): every git helper resolves ``_git``
    from origin's module globals at call time, so the count survives
    verify.py's from-imports and covers all resolution paths."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "anchor", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _commit_touching(tmp_path, "after", when=datetime(2026, 2, 1, tzinfo=timezone.utc))

    from bettermemory import origin as origin_module

    real_git = origin_module._git
    toplevel_forks = 0

    def counting_git(cwd: Path, *args: str, **kwargs: Any) -> str | None:
        nonlocal toplevel_forks
        if args == ("rev-parse", "--show-toplevel"):
            toplevel_forks += 1
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(origin_module, "_git", counting_git)

    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
        body="claims about notes.md hold",
    )
    # The narrowing path must have actually run (drift on the cited file),
    # otherwise a skipped narrowing would trivially satisfy the fork count.
    assert result is not None
    assert result.status == "drift"
    assert result.commits_since_verify == 1
    assert toplevel_forks == 1


def test_commit_drift_status_is_immutable_dataclass(tmp_path: Path) -> None:
    """Frozen — same rationale as VerificationStatus: a consumer that
    mutates the verdict could silently corrupt later reads when the
    object is reused (which the server doesn't do today, but the
    contract should not depend on that)."""
    status = CommitDriftStatus(
        status="clean", commits_since_verify=0, recommendation=None
    )
    import dataclasses as _dc

    try:
        status.status = "drift"  # type: ignore[misc]
    except _dc.FrozenInstanceError:
        return
    raise AssertionError("CommitDriftStatus should be frozen")


# ---------------------------------------------------------------------------
# Spaced-path extraction, URL-route suppression, absent attestations
# (the 3.8.x false-signal fixes — each test pins one live incident class)
# ---------------------------------------------------------------------------


def test_bare_spaced_segment_path_extracted_whole(tmp_path: Path) -> None:
    """Class A incident: bare `~/Library/Application Support/...` used to
    truncate at the space, and the prefix (`~/Library/Application`)
    false-flagged as missing on every retrieval."""
    spaced = tmp_path / "Application Support" / "Claude"
    spaced.mkdir(parents=True)
    target = spaced / "claude_desktop_config.json"
    target.write_text("{}")
    body = f"the desktop config {target} contains only UI preferences"
    report = detect_path_drift(body)
    assert str(target) in report.checked
    assert report.missing == ()


def test_bare_multiword_spaced_segment_with_continuation(tmp_path: Path) -> None:
    """Two consecutive spaced words resume with a slash — the
    `.../Visual Studio Code.app/Contents` shape."""
    app = tmp_path / "Visual Studio Code.app" / "Contents"
    app.mkdir(parents=True)
    body = f"binaries live under {app} on this machine"
    report = detect_path_drift(body)
    assert str(app) in report.checked
    assert report.missing == ()


def test_bare_truncation_before_capitalized_word_dropped(tmp_path: Path) -> None:
    """A bare match that stops right before ` Capitalized…` and fails the
    disk check is an ambiguous truncation (terminal spaced component) —
    dropped entirely, never flagged missing."""
    body = f"installed at {tmp_path}/Visual Studio Code.app on this machine"
    # `{tmp_path}/Visual Studio Code.app` does NOT exist; the truncated
    # candidate `{tmp_path}/Visual` doesn't either. The ` Studio` tail
    # marks the extraction ambiguous -> no candidate, no phantom flag.
    report = detect_path_drift(body)
    assert report.missing == ()
    assert all("Visual" not in c for c in report.checked)


def test_bare_missing_path_before_lowercase_prose_still_flags(tmp_path: Path) -> None:
    """The ambiguity drop must not swallow real drift: ordinary prose
    continues lowercase after a path citation."""
    gone = tmp_path / "definitely-gone.conf"
    body = f"the config at {gone} was moved last week"
    report = detect_path_drift(body)
    assert str(gone) in report.missing


def test_bare_existing_path_before_capitalized_word_kept(tmp_path: Path) -> None:
    """Existence proves the prefix is a real path regardless of what the
    prose does next — only MISSING ambiguous candidates are dropped."""
    body = f"see {tmp_path} Files are kept there"
    report = detect_path_drift(body)
    assert str(tmp_path) in report.checked
    assert report.missing == ()


def test_windows_drive_spaced_path_in_backticks_extracted() -> None:
    """`C:\\Program Files\\…` failed the internal-space multi-slash rule
    because the drive prefix wasn't credited as a boundary — the single
    most common spaced path on Windows was silently dropped."""
    body = r"installed under `C:\Program Files\bettermemory-test-noexist` there"
    report = detect_path_drift(body)
    assert "C:\\Program Files\\bettermemory-test-noexist" in report.checked


def test_url_route_suppressed_by_domain_cross_reference() -> None:
    """Class B incident: a body citing `pypi.org/pypi/bettermemory/<ver>/json`
    in one sentence and the bare index route `/pypi/bettermemory/json` in
    another permanently false-flagged the route as a missing file."""
    body = (
        "hit `pypi.org/pypi/bettermemory/<ver>/json` (200 = live) — the "
        "top-level `/pypi/bettermemory/json` index lags ~1min"
    )
    report = detect_path_drift(body)
    assert report.checked == ()
    assert report.missing == ()


def test_route_suppression_requires_first_segment_match(tmp_path: Path) -> None:
    """A domain citation elsewhere in the body must not suppress unrelated
    filesystem candidates."""
    real = tmp_path / "config.toml"
    real.write_text("x")
    body = f"docs at example.com/docs/setup — config lives in {real}"
    report = detect_path_drift(body)
    assert str(real) in report.checked
    assert report.missing == ()


def test_absolute_absent_attestation_under_worktree_routes_to_expected_absent(
    tmp_path: Path,
) -> None:
    """An absence attested in ABSOLUTE form for a file inside the recorded
    worktree must route the body's relative citation of that file to
    `expected_absent`, exactly as the relative spelling of the same
    attestation does. Pre-fix the anchored-attestation pass skipped every
    absolute form, so the citation pass stat'd the relative citation with
    no absent set in hand and escalated a reviewed, intentional absence to
    `claim_anchored_missing` — the altitude memory's five purged bench
    documents read `spot_check_recommended` on every retrieval for it."""
    (tmp_path / "bench").mkdir()
    gone = tmp_path / "bench" / "DOOR_C_DECISION_BRIEF.md"
    body = (
        "Decision recorded in bench/DOOR_C_DECISION_BRIEF.md (withdrawn from "
        "the tree; the archive copy lives elsewhere)."
    )
    absolute = detect_path_drift(body, absent_paths=[str(gone)], worktree_root=tmp_path)
    relative = detect_path_drift(
        body, absent_paths=["bench/DOOR_C_DECISION_BRIEF.md"], worktree_root=tmp_path
    )
    for report in (absolute, relative):
        assert [Path(p).name for p in report.expected_absent] == [
            "DOOR_C_DECISION_BRIEF.md"
        ]
        assert report.missing == ()
        assert report.claim_anchored_missing == ()
    assert absolute.to_dict() == relative.to_dict()


def test_absolute_verified_attestation_under_worktree_lands_in_verified(
    tmp_path: Path,
) -> None:
    """The present-file twin: an absolute `verified_paths` entry inside the
    worktree is checked by the anchored pass and lands in `verified`, so
    the body's relative citation of the same file is not re-stat'd as an
    unattested citation."""
    (tmp_path / "docs").mkdir()
    present = tmp_path / "docs" / "ROADMAP.md"
    present.write_text("# roadmap\n", encoding="utf-8")
    report = detect_path_drift(
        "the flip bars are declared in docs/ROADMAP.md",
        verified_paths=[str(present)],
        worktree_root=tmp_path,
    )
    assert [Path(p).name for p in report.verified] == ["ROADMAP.md"]
    assert [Path(p).name for p in report.checked] == ["ROADMAP.md"]
    assert report.missing == ()


def test_absolute_attestation_outside_worktree_stays_out_of_anchored_pass(
    tmp_path: Path,
) -> None:
    """An absolute attestation that resolves OUTSIDE the worktree is not a
    worktree claim: the anchored pass leaves it alone (no phantom entry in
    `checked`), and it remains reachable only through the main loop's
    set-membership when the body cites it."""
    root = tmp_path / "repo"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere" / "conf.ini"
    report = detect_path_drift(
        "nothing here cites a path", absent_paths=[str(elsewhere)], worktree_root=root
    )
    assert report.checked == ()
    assert report.expected_absent == ()


def test_absent_attestation_moves_missing_to_expected_absent(tmp_path: Path) -> None:
    """Classes C+D: remote-host / platform-conditional / cited-as-NOT-here
    paths are attestable via `verified_absent_paths` — excluded from
    `missing`, surfaced under `expected_absent`."""
    gone = tmp_path / "remote-only"
    body = f"the stacks live in {gone} on the zimaboard, not on this Mac"
    flagged = detect_path_drift(body)
    assert str(gone) in flagged.missing
    attested = detect_path_drift(body, absent_paths=[str(gone)])
    assert attested.missing == ()
    assert str(gone) in attested.expected_absent
    assert str(gone) in attested.checked
    assert attested.has_drift is False


def test_absent_attestation_with_tilde_expansion(tmp_path: Path, monkeypatch) -> None:
    """Attestation and body candidate match through the same `~`-expansion
    pipeline as `verified_paths`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    body = "the project is NOT in ~/Projects anymore"
    attested = detect_path_drift(body, absent_paths=["~/Projects"])
    assert attested.missing == ()
    assert attested.expected_absent == ("~/Projects",)


def test_absent_attestation_does_not_mask_other_drift(tmp_path: Path) -> None:
    other = tmp_path / "other-gone.conf"
    attested_path = tmp_path / "expected-gone"
    body = f"configs were at {other} and remote {attested_path} respectively"
    report = detect_path_drift(body, absent_paths=[str(attested_path)])
    assert str(other) in report.missing
    assert str(attested_path) in report.expected_absent


def test_absent_attested_path_that_exists_is_normal_candidate(tmp_path: Path) -> None:
    """Presence never raises a flag: an attested-absent path that has
    (re)appeared is just a healthy candidate."""
    back = tmp_path / "reappeared"
    back.mkdir()
    body = f"data lives in {back} again"
    report = detect_path_drift(body, absent_paths=[str(back)])
    assert str(back) in report.checked
    assert report.missing == ()
    assert report.expected_absent == ()


# ---------------------------------------------------------------------------
# Extractor false-signal hunt fixes (2026-06-09 multi-agent audit)
# ---------------------------------------------------------------------------


def test_backticked_line_suffix_stripped_existing(tmp_path: Path) -> None:
    """`path/file.py:407` cites a code location; the file is the claim."""
    f = tmp_path / "mod.py"
    f.write_text("x")
    for suffix in (":407", ":445-461", ":12:5"):
        report = detect_path_drift(f"the filter lives in `{f}{suffix}`.")
        assert str(f) in report.checked, suffix
        assert report.missing == (), suffix


def test_backticked_line_suffix_stripped_still_flags_missing(
    tmp_path: Path,
) -> None:
    gone = tmp_path / "gone.py"
    report = detect_path_drift(f"the old hook was `{gone}:42`.")
    assert str(gone) in report.missing


def test_bare_at_sign_path_extracted_whole(tmp_path: Path) -> None:
    """Homebrew kegs (`python@3.12`), systemd templates (`foo@1.service`)
    carry `@` — the bare scan used to truncate at it."""
    keg = tmp_path / "python@3.12" / "bin"
    keg.mkdir(parents=True)
    report = detect_path_drift(f"interpreter pinned at {keg} for the project")
    assert str(keg) in report.checked
    assert report.missing == ()


def test_balanced_paren_directory_name_kept(tmp_path: Path) -> None:
    arch = tmp_path / "bettermemory (archived)"
    arch.mkdir()
    report = detect_path_drift(f"old tree moved to `{arch}` for reference.")
    assert str(arch) in report.checked
    assert report.missing == ()


def test_unbalanced_trailing_paren_still_stripped(tmp_path: Path) -> None:
    gone = tmp_path / "gone-paren"
    report = detect_path_drift(f"(check `{gone})`)")
    assert str(gone) in report.missing


def test_tilde_and_absolute_spellings_dedup_to_one_claim() -> None:
    home = str(Path.home())
    body = f"old launcher at ~/.bm-test-dd.sh (absolute: {home}/.bm-test-dd.sh)"
    report = detect_path_drift(body)
    assert len(report.missing) == 1


def test_windows_spaced_path_with_continuation_extracted() -> None:
    body = r"runtime at `C:\Program Files\bm-noexist\node.exe` per IT policy"
    report = detect_path_drift(body)
    assert "C:\\Program Files\\bm-noexist\\node.exe" in report.checked


def test_three_word_terminal_spaced_dir_in_backticks(tmp_path: Path) -> None:
    d = tmp_path / "My Cool Project"
    d.mkdir()
    report = detect_path_drift(f"notes in `{d}`.")
    assert str(d) in report.checked
    assert report.missing == ()


def test_home_anchored_spaced_dir_accepted() -> None:
    """`~/Calibre Library/...` — the home anchor crosses the home root, so
    its single-slash first chunk passes the boundary rule like `C:\\…`."""
    report = detect_path_drift("calibre db at `~/Calibre Library/metadata.db` x")
    target = "~/Calibre Library/metadata.db"
    assert target in report.checked or target in report.missing


def test_paren_continuation_after_bare_path_not_flagged(tmp_path: Path) -> None:
    """`report (2).pdf` duplicate-download names: the ` (`-continuation is
    an ambiguous truncation, not drift."""
    f = tmp_path / "report (2).pdf"
    f.write_text("x")
    report = detect_path_drift(
        f"invoice saved as {tmp_path}/report (2).pdf after download."
    )
    assert report.missing == ()


def test_sentence_final_bare_path_still_flags(tmp_path: Path) -> None:
    """A trimmed sentence period is a prose delimiter — the next sentence's
    capital must not mark the citation ambiguous."""
    gone = tmp_path / "old.conf"
    report = detect_path_drift(f"Old config was at {gone}. The new one works fine.")
    assert str(gone) in report.missing


def test_single_argument_backticked_command_excluded() -> None:
    report = detect_path_drift("cron runs `/opt/homebrew/bin/brew upgrade` nightly.")
    assert report.checked == ()


def test_env_assignment_boundary_extracts_path(tmp_path: Path) -> None:
    gone = tmp_path / "store.db"
    report = detect_path_drift(f"launchd sets BM_DB={gone} at login")
    assert str(gone) in report.missing


def test_flag_equals_boundary_extracts_path(tmp_path: Path) -> None:
    gone = tmp_path / "store2.db"
    report = detect_path_drift(f"start with --db={gone} for the test store")
    assert str(gone) in report.missing


def test_wellknown_route_filenames_skipped() -> None:
    body = "nginx overrides /robots.txt and serves /openapi.json from the app"
    report = detect_path_drift(body)
    assert report.checked == ()


def test_dollar_home_prefix_canonicalized_to_tilde() -> None:
    report = detect_path_drift(
        "config read from $HOME/bm-missing-xyz/c.toml at startup"
    )
    assert "~/bm-missing-xyz/c.toml" in report.missing


def test_glob_pattern_citation_not_statted(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    report = detect_path_drift(f"logs rotate under `{tmp_path}/logs/*.log` nightly")
    assert report.checked == ()


def test_template_placeholder_paths_rejected() -> None:
    body = "settings at `~/.config/<app>/settings.toml`, `/opt/stacks/{service}/data`"
    report = detect_path_drift(body)
    assert report.checked == ()


def test_shell_escaped_spaces_unescaped(tmp_path: Path) -> None:
    """Candidates keep the author's separator spelling, so compare via
    Path equality, not raw strings — on Windows `str(tmp_path)` is the
    backslash form while the citation uses `/` (both resolve)."""
    f = tmp_path / "My Drive" / "notes.txt"
    f.parent.mkdir()
    f.write_text("x")
    cited = f"{tmp_path.as_posix()}/My\\ Drive/notes.txt"
    backtick = detect_path_drift(f"vault at `{cited}` synced")
    assert backtick.missing == ()
    assert [Path(c) for c in backtick.checked] == [f]
    bare = detect_path_drift(f"vault lives at {cited} synced via Drive")
    assert bare.missing == ()
    assert [Path(c) for c in bare.checked] == [f]


def test_markdown_table_pipe_is_bare_boundary(tmp_path: Path) -> None:
    gone = tmp_path / "table.conf"
    report = detect_path_drift(f"| nginx |{gone}| row")
    assert str(gone) in report.missing


def test_acronym_pair_glue_falls_back_to_existing_prefix(tmp_path: Path) -> None:
    """`<existing-dir> TCP/IP keepalive` — the continuation rule glues the
    acronym pair on; the disk arbitrates back to the real path. Uses a
    tmp dir rather than `/etc/hosts` so the existing-prefix arm also
    holds on Windows runners."""
    cited = tmp_path.as_posix()
    report = detect_path_drift(f"tuned {cited} TCP/IP keepalive overrides today")
    assert cited in report.checked
    assert report.missing == ()


def test_attested_ambiguous_candidate_still_flags_when_deleted(
    tmp_path: Path,
) -> None:
    """verified-then-deleted is a real drift signal — attestation resolves
    extraction ambiguity, so the ambiguous-truncation drop must not eat it."""
    gone = tmp_path / "run.sh"
    body = f"backup script at {gone} Runs nightly via launchd."
    unattested = detect_path_drift(body)
    assert unattested.missing == ()  # ambiguous, dropped
    attested = detect_path_drift(body, verified_paths=[str(gone)])
    assert str(gone) in attested.missing


def test_later_clean_occurrence_downgrades_ambiguity(tmp_path: Path) -> None:
    """Sentence order must not decide whether real drift is reported."""
    gone = tmp_path / "run2.sh"
    body = f"backup at {gone} Runs nightly. Cron invokes {gone} at 02:00 daily."
    report = detect_path_drift(body)
    assert str(gone) in report.missing


def test_smb_share_spec_rejected() -> None:
    report = detect_path_drift("photos live on //nas/photos (SMB share).")
    assert report.checked == ()


def test_domain_route_regex_bounded_against_redos() -> None:
    """ReDoS guard: `_DOMAIN_ROUTE_RE` is finditer'd over the whole raw
    body per search hit. An unbounded label repeat backtracks
    catastrophically on a long domain-shaped run with NO trailing slash
    (the `/segment` tail never matches). The `{1,20}` bound keeps the scan
    linear. Assert both that the pathological body is handled correctly
    (no phantom candidates) and that it returns well under a generous
    wall-clock guard — the current unbounded regex takes multiple seconds
    on this input."""
    import time

    # A ~20k-label dotted run with no slash — the pathological shape that
    # forces the engine to retry every partition at every start offset.
    poison = "a" + ".a" * 20000
    body = f"see /etc/hosts and then {poison} in the notes"
    start = time.monotonic()
    report = detect_path_drift(body)
    elapsed = time.monotonic() - start
    # Generous bound: fixed code runs in ~0.01s; unbounded takes ~3.7-4.7s.
    assert elapsed < 2.0, f"path-drift scan took {elapsed:.2f}s — regex not bounded"
    # The poison run is not a filesystem path and must not leak into the
    # report; the real path in the same body is still handled normally.
    assert poison not in report.checked
    assert poison not in report.missing
    assert all("a.a.a" not in c for c in report.checked)


def test_domain_route_bound_preserves_route_suppression() -> None:
    """The `{1,20}` label bound must not change route-suppression behavior
    for realistic domains: a body citing a domain-attached route in one
    place and the same first-segment bare route in another still has the
    bare route suppressed (empty `missing`), exactly as before the bound.
    Correctness anchor — passes both before and after the fix; guards
    against a future over-tightening of the bound."""
    body = (
        "hit `pypi.org/pypi/bettermemory/<ver>/json` (200 = live) — the "
        "top-level `/pypi/bettermemory/json` index lags ~1min"
    )
    report = detect_path_drift(body)
    assert report.checked == ()
    assert report.missing == ()


def test_scan_cap_cannot_fabricate_drift_from_straddling_path(
    tmp_path: Path,
) -> None:
    """The input bound sliced at a hard byte offset, so a legitimate citation
    straddling the 32 KiB boundary was cut MID-TOKEN: the surviving prefix
    validated as a path, failed the disk check (the end-of-string tail
    lookahead can't fire), and FABRICATED a `path_drift_missing` entry — a
    false non-fresh staleness verdict — from a body whose real path exists.
    Legal bodies run to 1 MB, so >32 KiB is reachable without hostility. The
    cut must land on the last whitespace inside the cap: the straddling claim
    is DROPPED, never bisected. Reverting to the hard slice resurrects the
    phantom prefix and fails the no-missing assertion."""
    real = tmp_path / "straddle" / "README.md"
    real.parent.mkdir()
    real.write_text("present\n", encoding="utf-8")
    cited = str(real)

    # Place the citation so the cap lands 6 chars before its end — inside
    # "README.md" — leaving a phantom prefix that exists nowhere on disk.
    start = _MAX_BODY_SCAN_BYTES - (len(cited) - 6)
    phantom = cited[: len(cited) - 6]
    assert not Path(phantom).exists()  # fixture sanity: bisection makes a ghost
    body = "f" * (start - 1) + " " + cited + " and trailing prose"
    assert start + len(cited) > _MAX_BODY_SCAN_BYTES  # fixture straddles the cap

    report = detect_path_drift(body)
    assert not report.missing, (
        f"the straddling citation must be dropped whole, not bisected into a "
        f"fabricated missing prefix; got {report.missing!r}"
    )
    assert all(not (cited.startswith(p) and p != cited) for p in report.checked)

    # The same citation fully INSIDE the cap is still detected and clean —
    # the whitespace cut drops only the straddling tail token, never a real
    # claim that fits.
    report_inside = detect_path_drift("f" * 100 + " " + cited + " tail")
    assert cited in tuple(report_inside.checked)
    assert not report_inside.missing


# ---------------------------------------------------------------------------
# Multi-segment route suppression.
#
# `_is_route` can only suppress a route when the SAME body also carries a
# domain-qualified URL for `_DOMAIN_ROUTE_RE` to harvest a vocabulary from.
# A memory citing only bare app routes got an empty vocabulary, so every
# route was stat'd and reported as a missing FILE — inflating
# staleness_verdict on a healthy record. Found by a sweep over web-app
# memories, which cite routes constantly and rarely write the host.
# ---------------------------------------------------------------------------


def test_bare_multi_segment_routes_are_not_missing_files() -> None:
    """The reported bug: no domain sibling, so no vocabulary to learn."""
    report = detect_path_drift(
        "Routes `/api/v1/events/presence` and `/admin/macros` are registered."
    )
    assert report.missing == ()
    # Dropped entirely — "we looked and it wasn't there" is a meaningless
    # statement about a URL path, so it must not even appear in `checked`.
    assert report.checked == ()


def test_route_suppression_no_longer_needs_a_domain_sibling() -> None:
    """Same routes, with and without a domain-qualified URL, agree."""
    routes = "Route `/portal/incidents/new` is live."
    with_domain = "See https://x.example.com/portal/health. Route `/portal/incidents/new` is live."
    assert detect_path_drift(routes).missing == ()
    assert detect_path_drift(with_domain).missing == ()


def test_existing_parent_directory_still_reports_real_drift(tmp_path: Path) -> None:
    """The escape that keeps path drift useful: a deleted file whose
    neighbourhood still exists is GENUINE drift, not a route. This is the
    exact case the feature exists to catch — it must survive the fix."""
    gone = tmp_path / "gone-xyz"
    body = f"The store lived at `{gone}`."
    report = detect_path_drift(body)
    assert str(gone) in report.missing


def test_extensioned_candidate_still_reports_missing() -> None:
    """A terminal extension reads as a file even when the parent is
    absent — `/srv/app/config.yaml` is a config path, not a route."""
    report = detect_path_drift("Config at `/srv/app/config.yaml` on the box.")
    assert "/srv/app/config.yaml" in report.missing


def test_remote_path_under_an_existing_root_still_reports_missing() -> None:
    """A remote-host citation whose ROOT exists locally (`/opt/gophish`
    where `/opt` is present) keeps flowing to `missing` until attested —
    the documented remote-host behaviour, deliberately unchanged.

    Gated on the root actually existing: the rule is parent-sensitive by
    design, so on a host without `/opt` (notably Windows, where no POSIX
    root exists) the same citation legitimately reads as a route. That
    platform difference is intended — a POSIX path cannot be
    meaningfully stat'd from Windows, so dropping beats manufacturing
    drift — but it makes the unattested half of this assertion
    environment-dependent.
    """
    body = "Gophish lives at `/opt/gophish` on the homelab board."
    if os.path.isdir("/opt"):
        assert "/opt/gophish" in detect_path_drift(body).missing
    attested = detect_path_drift(body, absent_paths=["/opt/gophish"])
    assert attested.missing == ()
    assert "/opt/gophish" in attested.expected_absent


def test_suppressed_routes_land_in_dropped_as_route() -> None:
    """THE IN-PROCESS OBSERVABILITY CONTRACT.

    A route-dropped candidate used to appear in NO bucket at all —
    absent from `checked`, `missing` and `expected_absent` alike, with
    nowhere on `PathDriftReport` to look. Leaving no trace is why
    3.25.2's over-broad rule swallowed real missing paths without anyone
    noticing. The suppressed set must now be readable off the report.

    SCOPE: this pins the report object only. A route-ONLY report like
    this one still emits nothing at the MCP surface — both handler gates
    are `has_drift or verified or expected_absent`, which it fails. See
    `test_dropped_as_route_ships_whenever_the_surface_gate_fires` and
    the reach note on `PathDriftReport`.
    """
    report = detect_path_drift(
        "Routes `/api/v1/events/presence` and `/admin/macros` are registered."
    )
    # Unchanged: a route is neither a checked file nor drift.
    assert report.checked == ()
    assert report.missing == ()
    assert report.has_drift is False
    # New: an in-process caller holding the report can SEE what the rule
    # ate, and it survives serialisation — so the two handler gates ship
    # it the moment their expression is widened to include the bucket.
    assert report.dropped_as_route == (
        "/api/v1/events/presence",
        "/admin/macros",
    )
    assert report.to_dict()["dropped_as_route"] == [
        "/api/v1/events/presence",
        "/admin/macros",
    ]


def test_extractor_dedupes_so_the_per_candidate_buckets_cannot_double_count() -> None:
    """THE REAL INVARIANT behind every "bucket X doesn't double-count"
    claim: `_extract_candidates` yields pairwise-distinct paths.

    This replaces a test that asserted `dropped_as_route` dedupes a
    doubled route citation. That test could not fail. The extractor
    already collapses the repeat, so `detect_path_drift` was handed ONE
    candidate either way — the assertion held identically with the
    bucket's `not in dropped_as_route` guard deleted, which is how the
    guard was found to be unreachable and removed. A test that passes
    whether or not the code it names exists is negative coverage: it
    advertises protection that is not there.

    So pin the property that actually does the work, at the layer that
    actually implements it. Asserted on the extractor's own output rather
    than through a bucket, because reading it through a bucket is exactly
    what made the old test unfalsifiable — break the `index_of` dedupe in
    `_extract_candidates` and this fails.
    """
    body = (
        "Route `/admin/macros` is registered; see `/admin/macros` in the router. "
        "Config at `/etc/bm-audit-nope.conf`, again at /etc/bm-audit-nope.conf."
    )
    candidates = _extract_candidates(body)
    paths = [path for path, _, _ in candidates]
    assert len(paths) == len(set(paths)), paths
    # Both repeats really were repeats — otherwise the assertion above is
    # vacuously true on a body that never duplicated anything.
    assert sorted(paths) == ["/admin/macros", "/etc/bm-audit-nope.conf"]

    # The consequence the removed guard was trying to buy, now a property
    # of the input rather than of a defensive branch.
    assert detect_path_drift(body).dropped_as_route == ("/admin/macros",)


def test_checked_keeps_its_dedupe_guard_because_it_holds_derived_prefixes(
    tmp_path: Path,
) -> None:
    """The asymmetry that makes `checked`'s guard live while the route
    bucket's was dead — pinned so the two are not "tidied" together.

    `dropped_as_route` only ever receives a candidate `path`, and the
    extractor already made those distinct. `checked` also receives a
    DERIVED value: the spaced-bare arm appends `path.split(" ", 1)[0]`
    when the prose-glue fallback fires. That prefix can equal a later,
    genuinely distinct candidate, so `checked` can be offered the same
    string twice and its `if path in checked` guard is reachable.

    Here the glued candidate `<dir> TCP/IP` and the plain `<dir>` are two
    separate extractor candidates; the first contributes `<dir>` to
    `checked` via the fallback, the second arrives as itself.
    """
    directory = tmp_path / "bm-audit-derived-prefix"
    directory.mkdir()
    real = str(directory)
    body = f"{real} TCP/IP keepalive, and also {real} again."
    paths = [path for path, _, _ in _extract_candidates(body)]
    # Two distinct candidates: the extractor's dedupe does NOT collapse
    # these, so the collision is created downstream, not upstream.
    assert paths == [f"{real} TCP/IP", real], paths
    # ...and `checked` still names the directory exactly once.
    assert detect_path_drift(body).checked == (real,)


def test_accepted_false_negative_shapes_all_reach_the_report() -> None:
    """The residue documented on `_is_multi_segment_routelike`, pinned by
    SHAPE rather than by a story about where the path came from.

    "Reach the report" is the whole claim: every shape below lands in
    `dropped_as_route` on the returned object. None of them reaches a
    tool caller, because each produces a route-ONLY report and both
    handler gates are `has_drift or verified or expected_absent`. Named
    for what it verifies rather than "visible now", which would promise
    a surface this does not exercise.

    The docstring used to call this residue "an extensionless remote-host
    citation", which reads as a promise that LOCAL paths are safe. They
    are not: an unmounted local volume (`/Volumes/...`) and a foreign-OS
    home path (`/home/...`, while this machine's `$HOME` is `/Users/...`)
    drop for exactly the same four structural reasons a genuinely remote
    path does. This test is the executable version of that correction —
    it fails the moment the residue's real extent stops matching the
    documented extent.
    """
    residue = (
        "/srv/docker/gitea",
        "/data/compose/stacks",
        "/mnt/tank/media",
        "/home/mattias/scripts/backup",
        "/Volumes/My Book/archive/2024",
    )
    for cited in residue:
        # Fixture assumption: the shape only holds while the parent is
        # genuinely absent here. If a host really has `/srv/docker`, the
        # parent escape fires and the candidate is honest drift instead.
        if os.path.isdir(os.path.dirname(cited)):
            continue
        report = detect_path_drift(f"the store lives at `{cited}` these days")
        assert report.missing == (), cited
        assert report.checked == (), cited
        # The partial mitigation: recorded on the report rather than
        # nowhere. Still emitted by no MCP surface — route-only.
        assert cited in report.dropped_as_route, cited
        assert (
            bool(report.has_drift or report.verified or report.expected_absent) is False
        ), cited


def test_attested_route_is_never_dropped() -> None:
    """Attestations pin a citation: a caller who explicitly named a path
    must still get its drift signal, so the route drop must sit behind
    the `not attested` guard."""
    report = detect_path_drift(
        "Route `/api/v1/events/presence` is registered.",
        absent_paths=["/api/v1/events/presence"],
    )
    assert "/api/v1/events/presence" in report.expected_absent
    assert report.missing == ()


# ---------------------------------------------------------------------------
# Route suppression must not overshoot into FALSE-NEGATIVE drift.
#
# 3.25.2's `_is_multi_segment_routelike` gated on the RAW spelling
# (`s.startswith("/")`), so `~/x/y/z` and its expanded twin
# `/Users/me/x/y/z` — one and the same path, which the rest of the module
# treats as equivalent — got OPPOSITE verdicts: drift reported for the
# tilde form, silently dropped as a "route" for the absolute form. A tool
# that reports "clean" about a genuinely-deleted path is worse than one
# that over-reports, because silence is indistinguishable from health.
# ---------------------------------------------------------------------------


def _home_path(*segments: str) -> Path:
    """A guaranteed-nonexistent path under the REAL home directory.

    Deliberately not a `monkeypatch`-ed `$HOME`: `expanduser` reads
    different env vars per platform (`HOME` on POSIX, `USERPROFILE` on
    Windows), and a fake home that failed to take would silently turn
    these into no-op tests. Building from `Path.home()` makes the
    under-home relationship true by construction everywhere. Nothing is
    created or removed — the citation is a string and the check is a
    stat.
    """
    return Path.home().joinpath(*segments)


def _outside_home_citation() -> str:
    """A POSIX citation whose parent is absent AND which cannot sit under
    `$HOME` on any platform.

    Deliberately NOT built from `tmp_path`. pytest's temp dir follows
    `$TMPDIR`, which routinely lands under the user's home
    (`TMPDIR=$HOME/tmp`, a redirected per-user temp dir, a CI image that
    sets it), and the home exemption then unshapes the hazard: the route
    rule never runs and an assertion written to pin the rule's BOUND
    quietly inverts instead. The CI matrix happening to be safe today is
    not a property worth depending on.

    Root-anchored is outside home by construction — `_is_under_home`
    already refuses to treat `HOME=/` as a home — and it is route-shaped
    on Windows too, where a drive-rooted `tmp_path` never even enters the
    route branch.
    """
    citation = "/bm-audit-no-such-root-dir/child"
    assert not os.path.exists(os.path.dirname(citation)), (
        "fixture assumption: the parent must be absent or the parent escape fires"
    )
    return citation


def test_deep_home_path_under_vanished_parent_still_reports_missing() -> None:
    """A renamed/deleted project directory is THE case path drift exists
    to catch, and it defeats the parent-exists escape: when the whole
    repo directory goes, the cited path's parent went with it.

    Pre-fix this landed in neither `checked` nor `missing` — silently
    dropped as an application route.
    """
    gone = _home_path("bm-audit-vanished-repo", "src", "handlers")
    assert not gone.exists(), "fixture assumption: the cited path must be absent"
    report = detect_path_drift(f"The handlers live at `{gone}` now.")
    assert str(gone) in report.missing


def test_both_spellings_of_one_home_path_agree() -> None:
    """`~/x/y/z` and `/Users/<user>/x/y/z` are the same path — the module
    normalises them together (`_normalize_for_compare`), so the drift
    verdict must not depend on which spelling the author typed.

    Pre-fix the tilde form reported drift and the absolute form was
    dropped entirely: opposite verdicts for one path.
    """
    tail = ("bm-audit-two-spellings", "src", "handlers")
    absolute = _home_path(*tail)
    assert not absolute.exists(), "fixture assumption: the cited path must be absent"
    tilde = "~/" + "/".join(tail)
    # Same path, two spellings — this is what makes the divergence a bug.
    assert _normalize_for_compare(tilde) == _normalize_for_compare(str(absolute))

    for template in ("cited at `{}` today", "cited at {} today"):
        tilde_report = detect_path_drift(template.format(tilde))
        abs_report = detect_path_drift(template.format(absolute))
        assert tilde_report.missing == (tilde,), template
        assert abs_report.missing == (str(absolute),), template
        assert tilde_report.has_drift == abs_report.has_drift, template

    # And the attested escape hatch still pins the absolute spelling.
    attested = detect_path_drift(
        f"cited at `{absolute}` today", absent_paths=[str(absolute)]
    )
    assert attested.missing == ()
    assert str(absolute) in attested.expected_absent

    # Sanity: the home exemption is about HOME, not about "any absolute
    # path" — a candidate outside home under a vanished parent is still
    # dropped, which is the documented remaining bound. Built root-anchored
    # rather than from `tmp_path` so the bound is pinned on every host
    # (see `_outside_home_citation`); the previous `tmp_path` construction
    # inverted under `TMPDIR=$HOME/...` and needed an `or os.name == "nt"`
    # escape that swallowed the failure on Windows too.
    outside = _outside_home_citation()
    assert _is_multi_segment_routelike(outside) is True
    # ...and the drop is recorded on the report rather than leaving no
    # trace (report-level only; no MCP surface emits a route-only report).
    assert detect_path_drift(f"the tree lived at `{outside}`").dropped_as_route == (
        outside,
    )


def test_home_exemption_follows_the_filesystem_on_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The home exemption compared byte-for-byte, so on a default
    case-insensitive macOS APFS volume a citation spelled
    `/users/me/x/y/z` missed it and was dropped as a route — while
    `/Users/me/x/y/z`, the SAME directory as far as the kernel is
    concerned, reported drift. Two spellings of one path, opposite
    verdicts: precisely the divergence the home exemption exists to
    kill, one layer down.

    Asserted against what the filesystem actually DOES rather than
    against `sys.platform`, because case sensitivity is a per-volume
    property (case-sensitive APFS exists; so do case-folding exFAT/SMB
    mounts on Linux). Both directions are pinned — on a folding volume
    the differently-cased citation must be exempt and report drift; on a
    case-sensitive one it must NOT be, since `/users/...` really is a
    different path there and exempting it would resurrect the false
    positives the route rule was built to kill.
    """
    home = tmp_path / "AuditHome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    # Built with `os.sep` so the fixture stays single-variable: this
    # test is about CASE folding only. `_is_under_home` now folds
    # `os.altsep` into `os.sep` before comparing, so a forward-slash
    # tail would be recognised on Windows too — the separator axis has
    # its own coverage in the "separator folding" section below; keeping
    # the spelling platform-native here keeps the two axes independent.
    tail = os.sep + os.sep.join(("bm-audit-case-fold", "src", "handlers"))
    exact = str(home) + tail
    reskinned = str(home).swapcase() + tail
    assert reskinned != exact, "fixture assumption: the home prefix must be cased"
    assert not os.path.exists(exact), "fixture assumption: the citation must be absent"

    # The exact spelling is exempt on every filesystem — that is the
    # pre-existing contract and it must not regress.
    assert _is_under_home(exact) is True
    assert str(Path(exact)) in detect_path_drift(f"cited at `{exact}` today").missing

    folds_case = _home_ignores_case(str(home))
    report = detect_path_drift(f"cited at `{reskinned}` today")
    if folds_case:
        assert _is_under_home(reskinned) is True
        assert _is_multi_segment_routelike(reskinned) is False
        assert reskinned in report.missing
        assert report.dropped_as_route == ()
    else:
        # A genuinely different path on this volume: no exemption, and
        # the route rule legitimately swallows it — recorded on the
        # report now, though still not emitted at any MCP surface.
        assert _is_under_home(reskinned) is False
        assert _is_multi_segment_routelike(reskinned) is True
        assert report.missing == ()
        assert reskinned in report.dropped_as_route


def test_home_case_probe_is_conservative_when_it_cannot_probe() -> None:
    """The probe must fail CLOSED. An uncased home makes the case-flip a
    no-op, which would make `samefile(home, home)` trivially true and
    report every volume as case-folding; an unresolvable home makes it
    raise. Both have to read as "case matters here", or the exemption
    widens on hosts where nothing justified widening it.
    """
    assert _home_ignores_case("/1234") is False
    assert _home_ignores_case("/bm-audit-no-such-Home/nested") is False


# ---------------------------------------------------------------------------
# Home exemption — separator folding.
#
# Windows accepts "/" interchangeably with "\" in paths, and
# `_normalize_for_compare` (via `pathlib`) already treats the two
# spellings as one path on that platform. `_is_under_home` used to
# compare raw strings against `home + os.sep`, so a forward-slash or
# mixed spelling of a home-rooted citation read as NOT under home — the
# gap the case-fold fixture above documented as "queued as its own
# item". Closed now: both sides fold `os.altsep` into `os.sep` before
# the prefix check. Windows semantics are exercised from any platform
# via explicit `ntpath` values (`_simulate_windows_home`); on a real
# Windows runner the same monkeypatches are identity writes.
# ---------------------------------------------------------------------------


def test_fold_altsep_is_windows_only_by_parameter() -> None:
    """The fold rewrites the alternate separator under `ntpath` values
    and is the identity under `posixpath` values — `\\` is a legal POSIX
    filename character, so folding it there would invent directory
    boundaries the filesystem does not have. Pure-string helper, so the
    two platforms' semantics are both pinned on every runner."""
    folded = _fold_altsep("C:/Users/me/x", ntpath.sep, ntpath.altsep)
    assert folded == r"C:\Users\me\x"
    # Mixed spelling — the exact shape `ntpath.expanduser` returns for
    # `~/x` (USERPROFILE's backslashes + the citation's forward slash).
    mixed = _fold_altsep(r"C:\Users\me/x", ntpath.sep, ntpath.altsep)
    assert mixed == r"C:\Users\me\x"
    # POSIX: altsep is None, so BOTH characters survive untouched.
    assert posixpath.altsep is None
    weird = r"back\slash and/slash"
    assert _fold_altsep(weird, posixpath.sep, posixpath.altsep) == weird


def _simulate_windows_home(monkeypatch: pytest.MonkeyPatch, home: str) -> None:
    """Pin `_is_under_home`'s inputs to explicit ntpath semantics.

    `os.sep` / `os.altsep` are set to the `ntpath` constants — identity
    writes on a real Windows runner, the simulation everywhere else —
    and the home env vars to `home` in its Windows spelling: HOME for
    the POSIX `expanduser`, USERPROFILE for the Windows one, with
    HOMEDRIVE/HOMEPATH cleared so USERPROFILE wins (same env discipline
    as `test_home_relative_single_segment_still_extracted`).
    """
    monkeypatch.setattr(os, "sep", ntpath.sep)
    monkeypatch.setattr(os, "altsep", ntpath.altsep)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)


def test_is_under_home_recognises_forward_slash_windows_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deferred item itself: `C:/Users/me/project` is a spelling
    Windows accepts for a home-rooted path, and the raw `home + os.sep`
    prefix check missed it — the citation read as not home-rooted, the
    opposite verdict from its backslash twin. All spellings of one path
    must agree, matching `_normalize_for_compare` (whose `pathlib`
    normalisation already folds `/` into `\\` on Windows)."""
    _simulate_windows_home(monkeypatch, r"C:\Users\bm-audit-user")
    # The backslash-canonical spelling — the pre-existing contract.
    assert _is_under_home(r"C:\Users\bm-audit-user\project") is True
    # Forward-slash spelling of the SAME path: the closed gap.
    assert _is_under_home("C:/Users/bm-audit-user/project") is True
    # Mixed spelling — what `ntpath.expanduser` produces for `~/project`.
    assert _is_under_home(r"C:\Users\bm-audit-user/project") is True
    # Home itself, forward-slash spelled.
    assert _is_under_home("C:/Users/bm-audit-user") is True
    # NOT under home under any separator spelling: folding must not
    # widen the exemption to merely drive-lettered paths.
    assert _is_under_home("D:/data/project") is False
    assert _is_under_home("C:/Users/bm-audit-other/project") is False


def test_is_under_home_folds_the_home_spelling_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fold has to apply to BOTH comparands: some Windows setups
    carry a forward-slash USERPROFILE (`C:/Users/me`), and a
    backslash-spelled citation under it is the same path."""
    _simulate_windows_home(monkeypatch, "C:/Users/bm-audit-user")
    assert _is_under_home(r"C:\Users\bm-audit-user\project") is True
    assert _is_under_home("C:/Users/bm-audit-user/project") is True
    assert _is_under_home(r"D:\data\project") is False


def test_root_home_still_disables_exemption_under_folding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`HOME=/` disables the exemption (documented on `_is_under_home`),
    and the fold must not weaken that: under ntpath semantics `/` folds
    to `os.sep` itself, so the root guard has to test the FOLDED home —
    otherwise every slash-rooted candidate would read as home-rooted and
    the route rule would be nullified wholesale."""
    _simulate_windows_home(monkeypatch, "/")
    assert _is_under_home("/Users/bm-audit-user/project") is False
    assert _is_under_home("C:/Users/bm-audit-user/project") is False


def test_forward_slash_home_citation_reports_drift_not_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end reach of the fold through `detect_path_drift`: the
    route rule consults `_is_under_home` only for slash-rooted
    candidates, so the configuration where the separator gap flipped a
    real VERDICT is a drive-less Windows home (`HOMEPATH`-style
    `\\Users\\me`, which `ntpath.expanduser` returns verbatim). A
    vanished-repo citation under such a home, written with forward
    slashes, has an absent parent, so pre-fold it was dropped as a route
    — the silent false negative the home escape exists to kill. Folded,
    it is honest drift: checked, missing, and NOT in
    `dropped_as_route`."""
    _simulate_windows_home(monkeypatch, r"\Users\bm-audit-user")
    cited = "/Users/bm-audit-user/bm-audit-vanished/repo"
    assert _is_under_home(cited) is True
    assert _is_multi_segment_routelike(cited) is False
    report = detect_path_drift(f"the tree lived at `{cited}` before the rename")
    assert cited in report.checked
    assert cited in report.missing
    assert report.dropped_as_route == ()


def test_posix_backslash_is_a_filename_character_not_a_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On POSIX `os.altsep` is None and the fold must be the identity: a
    backslash run names a FILE whose name contains backslashes, not a
    nested directory, so a `home\\child` spelling must NOT read as under
    home. Gated on the live `os.altsep` because under ntpath semantics
    the same spelling really IS a separator run (pinned above). Guards
    against an over-eager fold that rewrites both characters
    unconditionally."""
    if os.altsep is not None:
        pytest.skip("this platform has an altsep; the fold is MEANT to fire here")
    home = tmp_path / "bm-audit-posix-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert _is_under_home(f"{home}/child") is True
    assert _is_under_home(str(home) + r"\child") is False


def test_bare_app_routes_are_still_suppressed() -> None:
    """The false positive 3.25.2 shipped for must survive the fix: bare
    application routes are not files and must not be reported missing —
    nor even `checked`, since "we looked and it wasn't there" is a
    meaningless statement about a URL path."""
    report = detect_path_drift(
        "Routes `/api/v1/events/presence` and `/admin/macros` are registered."
    )
    assert report.missing == ()
    assert report.checked == ()
    assert _is_multi_segment_routelike("/api/v1/events/presence") is True
    assert _is_multi_segment_routelike("/admin/macros") is True


def test_route_check_stays_last_in_the_not_exists_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ORDERING CONSTRAINT, load-bearing.

    The bare-scan continuation rule can glue a prose acronym pair onto a
    real path (`<dir> TCP/IP keepalive`). The manufactured tail reads as
    a route, so if the route drop ran BEFORE the spaced-bare
    arbitration, the real directory would be dropped instead of
    recovered via the prefix-existence fallback.

    `$HOME` is pinned away from `tmp_path` for the duration because the
    hazard only EXISTS while the glued candidate is route-shaped, and
    the home exemption unshapes it whenever `$TMPDIR` sits under the real
    home — `TMPDIR=$HOME/tmp` reproduces it, and the hazard assertion
    then fails outright. The pin makes the construction independent of
    where the runner puts its temp dir, which is not a property this
    test should ever have depended on. The two ordering assertions below
    are untouched and stay the point of the test.
    """
    fake_home = tmp_path / "bm-audit-fake-home"
    fake_home.mkdir()
    # Both spellings: `expanduser` reads HOME on POSIX and USERPROFILE
    # first on Windows, and a pin that failed to take would silently
    # restore the fragility this fixture exists to remove.
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    cited = tmp_path.as_posix()
    glued = f"{cited} TCP/IP"
    if glued.startswith("/"):
        # The hazard itself: the glued form IS route-shaped. (On Windows
        # `tmp_path` is drive-rooted and never enters the route branch,
        # so there is nothing to pin there.)
        assert _is_multi_segment_routelike(glued) is True
    report = detect_path_drift(f"tuned {cited} TCP/IP keepalive overrides today")
    assert cited in report.checked
    assert report.missing == ()


def test_route_check_is_structurally_last_in_the_not_exists_block() -> None:
    """Pins the ordering structurally, so a refactor that reorders the
    block fails here even on a platform where the behavioural fixture
    above cannot construct the hazard.

    Asserts `_is_multi_segment_routelike` is the FINAL guard inside
    `detect_path_drift`'s `not exists and not attested` block.

    The guard is located by its TEST expression and the enclosing block
    by physical containment, then the ordering is pinned by identity.
    The earlier locator picked "the smallest `ast.If` mentioning the
    predicate anywhere that has more than one statement in its body",
    which silently re-targeted onto the route guard ITSELF the moment
    the guard grew a body (recording the suppressed candidate did
    exactly that) — and then reported the guard's own `continue` as a
    misordered block. Same class of fragility as the `tmp_path` hazards
    above: a locator that depends on an incidental property of the code
    it is inspecting. The ordering constraint being pinned is unchanged
    and now strictly harder to satisfy by accident.
    """
    import ast
    import inspect
    import textwrap

    from bettermemory import verify as verify_module

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(verify_module.detect_path_drift))
    )
    guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "_is_multi_segment_routelike" in ast.dump(node.test)
    ]
    assert len(guards) == 1, f"expected exactly one route guard, found {len(guards)}"
    guard = guards[0]
    enclosing = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and any(stmt is guard for stmt in node.body)
    ]
    assert enclosing, "could not locate the not-exists block"
    not_exists_block = enclosing[0]
    assert len(not_exists_block.body) > 1, (
        "the not-exists block collapsed to the route guard alone — the "
        "spaced-bare and ambiguous-truncation arms must still precede it"
    )
    assert not_exists_block.body[-1] is guard, (
        "the route check must stay LAST in the not-exists block — moved "
        "earlier, a prose-glued candidate reads as a route on its "
        "manufactured tail and skips the prefix-existence fallback"
    )


def test_verdict_from_signals_takes_exactly_three_signals() -> None:
    """The staleness rollup has three inputs BY DECISION, not by accident.

    A fourth — resolving body-cited or attested commit SHAs read-side —
    was designed and measured on 2026-07-26 and rejected on arithmetic.
    The distance rule (commits since the cited SHA > 0) fired on 34 of 34
    SHA-carrying in-repo memories in the dogfood store, min 3 / median
    188 / max 685 commits, not one token at zero: Youden's J = 0.000,
    arithmetically ``always_flag``. The memories it would have flipped
    were exactly the SHA carriers already reading fresh, so a zero-git
    predictor reproduced its entire output. The existence rule changed
    zero verdicts and both its fires were on permanently-true history;
    the ancestry rule fired zero times and its answer is a property of
    local ``git gc`` rather than of the project.

    This is a signature pin, deliberately, and not a behavioural one:
    ``compute_staleness_verdict`` takes no Memory, so an attestation list
    has no channel into the call and any value-level assertion here would
    be a tautology that passes against its own mutation. The behavioural
    counterpart lives at handler level in
    ``test_a_sha_citing_fresh_memory_reads_fresh_on_both_surfaces`` in
    ``tests/test_server_commit_drift.py``, which catches the route this
    one cannot — a leg wired only into the search-side recompute.

    Deleting this test is the intended way to re-open the item. Bring new
    EVIDENCE, not a new implementation: the full record, including the
    honest cost of the class left uncovered, is the ``SHA_MARKER``
    tombstone in ``src/bettermemory/durability.py`` and item 5 of
    the rot-bench notes' "What would actually improve the verdict".
    """
    import inspect

    expected = {"status", "path_drift_missing", "commit_drift_count"}
    params = inspect.signature(verdict_from_signals).parameters
    assert set(params) == expected, (
        f"verdict_from_signals grew a signal: {set(params) ^ expected}. The "
        "read-side commit-SHA leg was measured and rejected (J = 0.000, "
        "fires 34/34) — see the SHA_MARKER tombstone in "
        "src/bettermemory/durability.py before adding a fourth."
    )
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values()), (
        "the three signals are keyword-only so a positional add cannot slip in"
    )

    rollup = inspect.signature(compute_staleness_verdict).parameters
    assert set(rollup) == {
        "verification",
        "path_drift_missing",
        "commit_drift_count",
    }, f"compute_staleness_verdict grew a signal: {set(rollup)}"
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in rollup.values())


# ---------------------------------------------------------------------------
# unverifiable_attestations — the placeholder hole, and the shapes that stay
# exempt on purpose
# ---------------------------------------------------------------------------


class TestUnverifiableAttestations:
    """Write-side attestation check. The first version of this reused
    `_normalize_candidate` wholesale and inherited its PROSE heuristics,
    which silently exempted documentation placeholders — so
    `verified_paths=["/etc/foo"]` sailed through and still stamped the
    memory `fresh`. That is precisely the fabricated attestation the check
    exists to stop, so placeholders are refused ahead of the validator."""

    def test_documentation_placeholder_is_refused(self) -> None:
        from bettermemory.verify import unverifiable_attestations

        for placeholder in ("/etc/foo", "/foo/bar", "/path/to/thing.py"):
            assert unverifiable_attestations([placeholder]) == [placeholder], (
                f"{placeholder} must be refused — it is stat-able and never "
                "names a real file, so attesting it is fabrication"
            )

    def test_unstattable_shapes_stay_exempt(self) -> None:
        """A shape claim is not a concrete path, so it can be neither
        present nor absent. Refusing these would manufacture a failure out
        of a caller naming a pattern."""
        from bettermemory.verify import unverifiable_attestations

        for shape in (
            "/var/log/app/*.log",  # glob
            "/opt/{service}/data",  # template
            "https://example.com/x",  # URL
            "user@host:/srv/thing",  # SSH remote
            "//server/share",  # SMB
            "/healthz",  # single-segment route
        ):
            assert unverifiable_attestations([shape]) == [], (
                f"{shape} is a shape claim, not a stat-able path"
            )

    def test_real_path_passes_and_missing_concrete_path_is_refused(
        self, tmp_path: Path
    ) -> None:
        from bettermemory.verify import unverifiable_attestations

        real = tmp_path / "here.toml"
        real.write_text("x = 1\n", encoding="utf-8")
        assert unverifiable_attestations([str(real)]) == []
        gone = str(tmp_path / "gone.toml")
        assert unverifiable_attestations([gone]) == [gone]

    def test_windows_drive_paths_are_treated_as_absolute(self) -> None:
        """Runs on every platform, and would have caught the CI-only bug.

        `_is_absolute_attestation` originally tested only
        `startswith(("/", "~"))`, so a drive-absolute path counted as
        RELATIVE and — with no worktree to anchor it — hit the unanchored
        skip. Every absolute attestation on Windows was therefore exempt and
        the check was inert there, while all four POSIX jobs stayed green.

        Asserted through the classifier rather than through a stat so the
        expectation is identical on POSIX and Windows: the drive form must
        be recognised as anchored. Both spellings matter — `as_posix()`
        emits `C:/...`, the OS emits `C:\\...`."""
        from bettermemory.verify import _is_absolute_attestation

        for anchored in ("C:/Users/me/thing.toml", "C:\\Users\\me\\thing.toml"):
            assert _is_absolute_attestation(anchored), anchored
        # And the POSIX anchors it already handled.
        for anchored in ("/etc/thing.conf", "~/notes.md"):
            assert _is_absolute_attestation(anchored), anchored
        # A genuinely relative path stays relative — it needs an anchor.
        for rel in ("src/pkg/mod.py", "notes.md"):
            assert not _is_absolute_attestation(rel), rel

    def test_unstattable_drive_path_is_refused_unanchored(self) -> None:
        """The behavioural consequence: a drive path that cannot be stat'd
        is REFUSED even with no worktree_root, because it needs none. On
        POSIX no `C:` drive exists, so this exercises the same branch
        Windows does."""
        from bettermemory.verify import unverifiable_attestations

        gone = "C:/Users/nobody/definitely-not-here.toml"
        assert unverifiable_attestations([gone]) == [gone]

    def test_home_env_spellings_are_treated_as_absolute(self) -> None:
        """`$HOME/...` and `${HOME}/...` are the env-var spellings of
        `~/...` — `_normalize_candidate` canonicalizes them to `~` before
        validating, so the mirror obligation in
        `_is_absolute_attestation`'s docstring covers them too. Classified
        RELATIVE instead, they get joined onto the worktree root (refusing
        an attestation of a path that exists) or, with no root, silently
        skipped (the fabricated-attestation gate never runs)."""
        from bettermemory.verify import _is_absolute_attestation

        for anchored in ("$HOME/Documents/notes.md", "${HOME}/Documents/notes.md"):
            assert _is_absolute_attestation(anchored), anchored

    def test_fabricated_home_env_attestation_is_refused_unanchored(self) -> None:
        """A `$HOME/...` attestation needs no worktree to resolve, so a
        fabricated one must be refused even with no worktree_root —
        exactly the drive-path rule above, for the spelling `_BARE_RE`
        extracts from bodies. Reported in the canonical `~/` form, the
        spelling the read side stores and compares."""
        from bettermemory.verify import unverifiable_attestations

        assert unverifiable_attestations(["$HOME/bm-not-here-xyz/c.toml"]) == [
            "~/bm-not-here-xyz/c.toml"
        ]

    def test_braced_home_attestation_is_not_laundered_as_template(
        self, tmp_path: Path
    ) -> None:
        """`${HOME}/...` classified relative gets root-joined into
        `<root>/${HOME}/...`, whose braces hit `_normalize_candidate`'s
        template exemption — a fabricated attestation would be silently
        skipped instead of refused. Classified as anchored, the `$HOME`
        canonicalization runs first and the braces never reach the
        template gate."""
        from bettermemory.verify import unverifiable_attestations

        refused = unverifiable_attestations(
            ["${HOME}/bm-not-here-xyz/c.toml"], worktree_root=str(tmp_path)
        )
        assert refused == ["~/bm-not-here-xyz/c.toml"]

    def test_existing_home_env_path_passes_with_worktree_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The false-refusal direction: attesting a real `$HOME/...` path
        must pass even when a worktree root is present. Joined onto the
        root instead, the check stats `<root>/$HOME/...` and refuses a
        path the attester genuinely checked."""
        from bettermemory.verify import unverifiable_attestations

        home = tmp_path / "AttestHome"
        real = home / "attested-dir" / "real.toml"
        real.parent.mkdir(parents=True)
        real.write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        root = tmp_path / "repo"
        root.mkdir()

        assert (
            unverifiable_attestations(
                ["$HOME/attested-dir/real.toml"], worktree_root=str(root)
            )
            == []
        )

    def test_relative_attestation_needs_an_anchor(self, tmp_path: Path) -> None:
        """Unanchored means "could not ask", and could-not-ask must never
        manufacture a negative verdict — the same rule
        `compute_commit_drift` follows by returning None. With an anchor the
        check becomes real."""
        from bettermemory.verify import unverifiable_attestations

        assert unverifiable_attestations(["src/pkg/gone.py"]) == []
        refused = unverifiable_attestations(
            ["src/pkg/gone.py"], worktree_root=str(tmp_path)
        )
        assert refused and refused[0].endswith("gone.py")


# ---------------------------------------------------------------------------
# Path-drift PROVENANCE — which absences may raise the verdict
# ---------------------------------------------------------------------------
#
# `PathDriftReport.missing` used to be one bucket feeding one boolean into
# the verdict, and the 2026-07-26 store sweep measured what that bucket
# was made of: ~0 of 15 prose-extracted missing-path alerts were real
# drift, against 3 of 3 for anchored attestations. The split records the
# provenance at the point of the decision — after the fact a path in
# `missing` is just a string and carries no trace of where it came from.
#
# Two properties are pinned throughout, and they pull in opposite
# directions on purpose:
#
#   * `claim_anchored_missing` is a SUBSET of `missing`, never a
#     replacement for it. Prose evidence stays on the wire; a caller sees
#     everything it saw before.
#   * only `claim_anchored_missing` reaches `verdict_from_signals`. That
#     is what "surface evidence, not verdicts" means here — the noisy
#     half stops driving a tier the caller is told to act on, without
#     becoming invisible.
#
# Negative control: appending to `missing` without the paired append to
# `claim_anchored` (or the reverse) flips
# `test_claim_anchored_missing_is_always_a_subset_of_missing`; feeding
# `len(drift.missing)` back into any verdict site flips
# `test_no_verdict_site_escalates_on_the_full_missing_set`.


def test_prose_missing_is_visible_but_does_not_escalate(tmp_path: Path) -> None:
    """The whole point, in one report: a path scraped out of prose lands
    in `missing` (so the caller sees it) and NOT in
    `claim_anchored_missing` (so it cannot raise the tier)."""
    gone = tmp_path / "notes" / "runbook.md"
    gone.parent.mkdir()
    report = detect_path_drift(f"the runbook lives at `{gone}`")

    assert report.missing == (str(gone),)
    assert report.claim_anchored_missing == ()
    assert report.has_drift is True
    assert report.has_claim_anchored_drift is False
    # And the visibility half of the contract, as the handler gates read it.
    assert bool(report.has_drift or report.verified or report.expected_absent) is True


def test_verified_then_deleted_absolute_path_is_claim_anchored(
    tmp_path: Path,
) -> None:
    """The 3-of-3-real class. An absolute attestation is only ever
    existence-checked when the body also names it, so this is the ONE
    shape in the main extraction loop that earns escalation."""
    gone = tmp_path / "session.py"
    body = f"the validator lives at `{gone}`"
    report = detect_path_drift(body, verified_paths=[str(gone)])

    assert report.missing == (str(gone),)
    assert report.claim_anchored_missing == (str(gone),)
    assert report.has_claim_anchored_drift is True


def test_attested_absent_path_never_becomes_claim_anchored(tmp_path: Path) -> None:
    """`verified_absent_paths` is the escape hatch for a legitimately
    remote or platform-conditional path. It must not sneak into the
    escalating bucket through the attestation door — an absent-attested
    path is not in `missing` at all, so it cannot be in a subset of it."""
    remote = tmp_path / "opt" / "gophish" / "config.json"
    remote.parent.mkdir(parents=True)
    body = f"the phishing sim reads `{remote}` on the homelab box"
    report = detect_path_drift(body, absent_paths=[str(remote)])

    assert report.expected_absent == (str(remote),)
    assert report.missing == ()
    assert report.claim_anchored_missing == ()
    assert report.has_claim_anchored_drift is False


def test_anchored_relative_attestation_miss_is_claim_anchored(
    tmp_path: Path,
) -> None:
    """A relative attestation resolved against the memory's recorded
    worktree is a reviewed claim about one file in one tree — the other
    half of the 3-of-3 class, and the reason the anchoring exists."""
    (tmp_path / "src").mkdir()
    report = detect_path_drift(
        "the token check moved",
        verified_paths=["src/auth.py"],
        worktree_root=str(tmp_path),
    )

    resolved = str(tmp_path / "src" / "auth.py")
    assert report.missing == (resolved,)
    assert report.claim_anchored_missing == (resolved,)


def test_home_env_spelled_attestation_reads_verified_not_claim_anchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `$HOME/`-spelled attestation is ABSOLUTE — the env-var spelling
    of `~/` that `_is_absolute_attestation`, `_normalize_candidate`, and
    the write gate all accept — so the anchored-attestation pass must
    SKIP it, not join it onto the worktree root. Pre-fix, `_anchored`
    restated the split locally as `startswith(("/", "~"))`:
    `unverifiable_attestations` ACCEPTED `$HOME/.zshrc` at write time
    (existence-checked via `~` canonicalization) while every retrieval
    stat-failed the manufactured `<root>/$HOME/.zshrc` and appended it
    to `claim_anchored_missing` — a phantom permanently poisoning the
    escalating bucket whose justification is the 3-of-3-real precision
    of anchored attestations. With a LIVE worktree_root the attested,
    existing file must land in `verified` through the main loop's
    set-membership path and nowhere near the missing buckets."""
    from bettermemory.verify import unverifiable_attestations

    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("alias ll='ls -la'\n", encoding="utf-8")
    root = tmp_path / "worktree"
    root.mkdir()
    # Cross-platform `~` redirect — same env discipline as
    # `test_home_relative_single_segment_still_extracted`.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)

    # The write gate accepts this exact attestation; the read side must
    # not then flag it — that split-brain is the bug.
    assert unverifiable_attestations(["$HOME/.zshrc"], worktree_root=root) == []

    report = detect_path_drift(
        "shell aliases live in `~/.zshrc`",
        verified_paths=["$HOME/.zshrc"],
        worktree_root=str(root),
    )
    assert report.claim_anchored_missing == ()
    assert report.missing == ()
    assert "~/.zshrc" in report.verified
    assert str(root / "$HOME" / ".zshrc") not in report.checked


def test_anchored_relative_citation_miss_is_claim_anchored(tmp_path: Path) -> None:
    """The filtered body citation. It escalates because the FILTER earns
    it: a raw `_RELATIVE_CITATION_RE` match is prose noise, and what
    reaches the stat has survived the directory-segment, host-shape,
    extension, placeholder and live-parent rules inside a worktree the
    memory itself recorded."""
    (tmp_path / "src").mkdir()
    report = detect_path_drift(
        "see `src/auth.py` for the token check",
        worktree_root=str(tmp_path),
    )

    resolved = str(tmp_path / "src" / "auth.py")
    assert report.missing == (resolved,)
    assert report.claim_anchored_missing == (resolved,)


def test_claim_anchored_missing_is_always_a_subset_of_missing(
    tmp_path: Path,
) -> None:
    """The structural invariant, on a body that exercises all three
    producers at once plus the prose one.

    A parallel list is only safe while every append is paired. Dropping
    either half of a pair — or building `claim_anchored` by filtering
    strings after the fact, which cannot work because the provenance is
    gone by then — fails here.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    prose_gone = tmp_path / "elsewhere" / "prose-only.md"
    prose_gone.parent.mkdir()
    attested_gone = tmp_path / "attested.py"

    report = detect_path_drift(
        f"cites `{prose_gone}` and `{attested_gone}`, plus `src/auth.py`",
        verified_paths=[str(attested_gone), "docs/design.md"],
        worktree_root=str(tmp_path),
    )

    assert set(report.claim_anchored_missing) <= set(report.missing)
    assert str(prose_gone) in report.missing
    assert str(prose_gone) not in report.claim_anchored_missing
    for anchored in (
        str(attested_gone),
        str(tmp_path / "src" / "auth.py"),
        str(tmp_path / "docs" / "design.md"),
    ):
        assert anchored in report.missing, anchored
        assert anchored in report.claim_anchored_missing, anchored


def test_suppressed_candidates_reach_neither_bucket() -> None:
    """Routes and placeholders are dropped before the missing decision,
    so the new bucket cannot resurrect them. Pinned because
    `claim_anchored_missing` is the bucket a future "make it actionable"
    patch would be tempted to widen."""
    report = detect_path_drift(
        "hit `/api/v1/events/presence` then edit `/path/to/config.yaml`"
    )
    assert report.claim_anchored_missing == ()
    assert report.missing == ()


def test_report_to_dict_carries_a_populated_claim_anchored_missing() -> None:
    """Serialisation parity: both handler gates emit `to_dict()`
    wholesale, so the bucket reaching the wire is what makes the
    escalating evidence readable rather than merely inferable from the
    tier."""
    r = PathDriftReport(
        checked=("/a", "/b"),
        missing=("/a", "/b"),
        claim_anchored_missing=("/b",),
    )
    assert r.to_dict()["claim_anchored_missing"] == ["/b"]
    assert r.to_dict()["missing"] == ["/a", "/b"]


# ---------------------------------------------------------------------------
# The commit leg's ESCALATING term, isolated — gate fired, gate retracted
# ---------------------------------------------------------------------------
#
# The condition governing this term was a pre-registration, not a taste
# call: after the provenance split and the anchored-relative citation
# arm, `bench/rot`'s new arms get re-run, and only if pooled
# alerts-per-catch is still >= 1.5 does the commit leg come out of the
# escalation disjunction. Both preconditions shipped, so the condition
# went live and had to be graded. The re-run happened on 2026-07-31 and
# pooled alerts-per-catch read 3.4, over the line — and the leg still
# did NOT come out, because the dry run recorded in
# `bench/rot/results/escalation-off-60d-2026-07-31.json` scores the
# subtraction itself as `never_flag`. The gate was retracted rather than
# honoured; the write-up is in the rot-bench notes and the standing
# decision in `docs/ROADMAP.md`. So these tests pin SHIPPED behaviour
# (the leg escalates) and, separately, pin that flipping the switch does
# exactly one thing.
#
# The second half is the one with teeth. `stale` + a measured `0` reading
# `fresh` is the 58a4fa4 fix; removing `commit_drift_count` from the
# verdict wholesale would resurrect the J=0.000 constant function that
# `bench/rot` caught. So the switch must not be reachable from the
# demotion branch, and `None` must not collapse into `0` on the way.


def test_commit_leg_escalates_today() -> None:
    """Shipped behaviour, and the measurement that could have moved it
    has now run and did not.

    The B2(b) condition fired on 2026-07-31 — pooled alerts-per-catch for
    the escalating tier is 3.4, over the 1.5 line — and the gate was
    RETRACTED rather than honoured, because the flip turns out to score
    `never_flag` on `bench/rot`'s pinned window: every drift arm goes
    96.74% flagged to 0.00%, J 0.0339 to 0.000. That dry run is the
    bench artifact `bench/rot/results/escalation-off-60d-2026-07-31.json`,
    not the test below, which pins the switch's single in-process effect.
    So this assertion is no longer provisional. If it starts failing without
    `_COMMIT_DRIFT_ESCALATES` having been flipped deliberately, the
    disjunction lost a term by accident; if it fails *because* someone
    flipped it, the number that justifies the flip has to be a measured
    replacement signal, not the 3.4 — see the retraction in
    the rot-bench notes and the `Not planned` entry in
    `docs/ROADMAP.md`.
    """
    from bettermemory.verify import _COMMIT_DRIFT_ESCALATES

    assert _COMMIT_DRIFT_ESCALATES is True
    assert (
        verdict_from_signals(status="fresh", path_drift_missing=0, commit_drift_count=3)
        == "spot_check_recommended"
    )


def test_disabling_commit_escalation_leaves_the_demotion_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-process half of the B2(b) dry run: flip the switch and
    check that exactly the escalating term goes quiet. (The measured
    half — what that silence costs on real claims — is the bench
    artifact `bench/rot/results/escalation-off-60d-2026-07-31.json`.)

    Every other cell of the ladder must be untouched — above all the
    stale-demotion arm, which reads `commit_drift_count` directly rather
    than through the switch precisely so a subtraction here cannot take
    it out. `None` staying at `spot_check_required` is the other half:
    if the flip made "couldn't ask" behave like a measured zero, roughly
    a third of real bodies (the judgement class that anchors nothing)
    would mass-demote to `fresh`.
    """
    import bettermemory.verify as verify_mod

    monkeypatch.setattr(verify_mod, "_COMMIT_DRIFT_ESCALATES", False)

    # The one cell that moves.
    assert (
        verdict_from_signals(status="fresh", path_drift_missing=0, commit_drift_count=3)
        == "fresh"
    )
    # The demotion — untouched.
    assert (
        verdict_from_signals(status="stale", path_drift_missing=0, commit_drift_count=0)
        == "fresh"
    )
    # None is still not zero.
    assert (
        verdict_from_signals(
            status="stale", path_drift_missing=0, commit_drift_count=None
        )
        == "spot_check_required"
    )
    # A stale memory with a non-zero measured count still has nothing
    # standing its calendar leg down.
    assert (
        verdict_from_signals(status="stale", path_drift_missing=0, commit_drift_count=3)
        == "spot_check_required"
    )
    # `never` still pre-empts everything.
    assert (
        verdict_from_signals(status="never", path_drift_missing=0, commit_drift_count=0)
        == "spot_check_required"
    )
    # And the path leg keeps escalating on its own.
    assert (
        verdict_from_signals(
            status="fresh", path_drift_missing=1, commit_drift_count=None
        )
        == "spot_check_recommended"
    )


def test_the_retraction_artifact_resolves_from_every_citation() -> None:
    """The retraction's evidence is cited BY PATH from two files (three
    before the rot README moved to the owner-side archive, 2026-08-21),
    and nothing else makes those paths resolve.

    The whole argument for keeping `_COMMIT_DRIFT_ESCALATES` at `True`
    against its own fired pre-registration is one dry run, and the only
    place its numbers live is that artifact. Three documents point at it
    by filename; a citation that does not resolve turns the retraction
    back into an assertion, which is the state it exists to leave. The
    prose cannot notice a rename, a stray `git clean`, or a byte off in
    the path — this can, so the check is here rather than in a sentence
    telling readers to be careful.

    Both halves are asserted: the exact string still appears in the file
    that cites it, and the file it names is on disk. The first half is
    why this cannot pass by accident after a rename — moving the
    artifact means updating three citations and this list together.
    """
    root = Path(__file__).resolve().parents[1]
    artifact = "escalation-off-60d-2026-07-31.json"
    # (citing file, the path exactly as written there, what it resolves
    # against — the README's citation is relative to its own directory,
    # the other two are repo-root anchored).
    citations = [
        ("src/bettermemory/verify.py", f"bench/rot/results/{artifact}", root),
        ("docs/ROADMAP.md", f"bench/rot/results/{artifact}", root),
    ]

    checked = 0
    for source, cited, base in citations:
        text = (root / source).read_text(encoding="utf-8")
        assert cited in text, (
            f"{source} no longer cites {cited!r} verbatim — either the "
            "citation was reworded and this guard went blind, or the "
            "retraction lost its evidence pointer"
        )
        assert (base / cited).is_file(), (
            f"{source} cites {cited!r}, which does not resolve. The "
            "commit-escalation retraction rests entirely on that dry run."
        )
        checked += 1

    assert checked == 2, f"expected two live citations, checked {checked}"


def test_the_commit_escalation_switch_has_exactly_one_reader() -> None:
    """`_COMMIT_DRIFT_ESCALATES` is the ONE named place B2(b) flips, and
    that is only true while `_commit_leg_escalates` is its sole reader.

    A second reader — most plausibly someone "helpfully" guarding the
    demotion branch with it too — would turn a measured subtraction into
    a silent resurrection of the constant function. Source-level because
    the defect is about where the name appears, not about what any one
    call returns.
    """
    import inspect

    import bettermemory.verify as verify_mod

    source = inspect.getsource(verify_mod)
    reads = [
        line
        for line in source.splitlines()
        if "_COMMIT_DRIFT_ESCALATES" in line
        and not line.lstrip().startswith("#")
        and "_COMMIT_DRIFT_ESCALATES: bool" not in line
    ]
    assert reads == ["    if not _COMMIT_DRIFT_ESCALATES:"], reads


# ---------------------------------------------------------------------------
# The escalation input, checked where it is WIRED rather than where it is
# computed
# ---------------------------------------------------------------------------


def test_no_verdict_site_escalates_on_the_full_missing_set() -> None:
    """Every `path_drift_missing=` argument in `src/` must carry the
    claim-anchored count.

    The parameter kept its name (the signature is pinned to exactly
    three signals by
    `test_verdict_from_signals_takes_exactly_three_signals`), so nothing
    at a call site announces that the meaning moved. A new surface —
    or a merge that reverts one line — wiring `len(drift.missing)` back
    in would silently re-broaden the alarm to the ~0-of-15 prose class
    and no behavioural test on the other surfaces would notice. This
    reads the argument's SOURCE at every call site, which is the level
    the mistake happens on.

    No file is excluded. The since-removed web UI used to be: its
    memory-detail renderer passed the full set while claiming the page
    "cannot disagree with what the model sees for the same memory", and
    the carve-out existed to record that known divergence rather than
    to bless it. The divergence was repaired before 5.0 removed the
    page whole; no carve-out remains.
    """
    import ast

    root = Path(__file__).resolve().parents[1] / "src" / "bettermemory"

    offenders: list[str] = []
    seen_sites = 0
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "path_drift_missing=" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name not in {"compute_staleness_verdict", "verdict_from_signals"}:
                continue
            for kw in node.keywords:
                if kw.arg != "path_drift_missing":
                    continue
                seen_sites += 1
                arg_src = ast.get_source_segment(text, kw.value) or ""
                # `0` is the list-summary surface, which loads no body and
                # so has no drift report at all. The bare parameter name is
                # `compute_staleness_verdict` forwarding to the primitive it
                # wraps — a pass-through carries whatever its own caller
                # passed, and that caller is itself a site this scan sees.
                ok = "claim_anchored" in arg_src or arg_src.strip() in {
                    "0",
                    "path_drift_missing",
                }
                if ok:
                    continue
                offenders.append(f"{path.name}: path_drift_missing={arg_src}")

    assert seen_sites >= 5, (
        f"expected to find every verdict call site, found {seen_sites} — the "
        "scan stopped matching and is no longer guarding anything"
    )
    assert not offenders, (
        "these verdict sites escalate on the FULL missing set, including "
        f"prose-scraped absences: {offenders}. Pass "
        "len(drift.claim_anchored_missing) — see PathDriftReport."
    )


# ---------------------------------------------------------------------------
# The remote verification status (6.6.0)
# ---------------------------------------------------------------------------


def test_remote_verification_status_carries_the_stamp_and_no_local_age() -> None:
    from datetime import datetime, timezone

    from bettermemory.verify import remote_verification_status

    stamp = datetime(2026, 9, 1, tzinfo=timezone.utc)
    status = remote_verification_status(stamp, stale_after_days=30)
    assert status.status == "remote"
    assert status.last_verified_at == stamp
    assert status.age_days is None
    assert status.stale_after_days == 30
    assert status.recommendation is not None
    assert "another host" in status.recommendation
    assert "2026-09-01T00:00:00Z" in status.recommendation
    assert "memory_verify" in status.recommendation
    payload = status.to_dict()
    assert payload["status"] == "remote"
    assert payload["last_verified_at"] == "2026-09-01T00:00:00Z"
    assert payload["age_days"] is None


def test_remote_verification_status_treats_a_naive_stamp_as_utc() -> None:
    from datetime import datetime, timezone

    from bettermemory.verify import remote_verification_status

    status = remote_verification_status(datetime(2026, 9, 1), stale_after_days=-3)
    assert status.last_verified_at is not None
    assert status.last_verified_at.tzinfo == timezone.utc
    assert status.stale_after_days == 0


def test_verdict_reads_remote_like_never() -> None:
    from bettermemory.verify import verdict_from_signals

    for missing, drift in ((0, None), (0, 0), (2, 0), (0, 7)):
        assert (
            verdict_from_signals(
                status="remote", path_drift_missing=missing, commit_drift_count=drift
            )
            == "spot_check_required"
        )
    assert (
        verdict_from_signals(status="fresh", path_drift_missing=0, commit_drift_count=0)
        == "fresh"
    )
