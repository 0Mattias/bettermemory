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

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from bettermemory.origin import Origin
from bettermemory.verify import (
    DEFAULT_VERIFICATION_STALE_DAYS,
    CommitDriftStatus,
    PathDriftReport,
    VerificationStatus,
    _MAX_BODY_SCAN_BYTES,
    _MAX_PATH_LENGTH,
    _PLACEHOLDER_PATHS,
    _PLACEHOLDER_PREFIXES,
    _normalize_candidate,
    compute_commit_drift,
    compute_verification_status,
    detect_path_drift,
)


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
# The canonical bite: a memory body documenting `/verify` (the web UI
# POST route, NOT a filesystem path) was being extracted as a path
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


def test_oserror_during_exists_treated_as_missing() -> None:
    """If `Path.exists()` raises (permission denied, ELOOP), we don't crash."""
    body = "See `/tmp/some-real-looking-path` for the thing."

    class _Boom:
        def expanduser(self) -> "_Boom":
            return self

        def exists(self) -> bool:
            raise PermissionError("nope")

    with patch("bettermemory.verify.Path", lambda _x: _Boom()):
        report = detect_path_drift(body)
    # The candidate was checked; PermissionError -> missing bucket.
    assert "/tmp/some-real-looking-path" in report.checked
    assert "/tmp/some-real-looking-path" in report.missing


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
    }


def test_report_to_dict_includes_verified_paths() -> None:
    r = PathDriftReport(checked=("/a", "/b"), missing=("/b",), verified=("/a",))
    d = r.to_dict()
    assert d == {
        "checked": ["/a", "/b"],
        "missing": ["/b"],
        "verified": ["/a"],
        "expected_absent": [],
    }


def test_report_to_dict_includes_expected_absent() -> None:
    r = PathDriftReport(checked=("/a", "/b"), missing=(), expected_absent=("/b",))
    d = r.to_dict()
    assert d == {
        "checked": ["/a", "/b"],
        "missing": [],
        "verified": [],
        "expected_absent": ["/b"],
    }


def test_has_drift_only_when_missing_nonempty() -> None:
    healthy = PathDriftReport(checked=("/a",), missing=())
    drifted = PathDriftReport(checked=("/a",), missing=("/a",))
    assert healthy.has_drift is False
    assert drifted.has_drift is True


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
    calendar verification."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "older", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
    )
    assert result is not None
    assert result.status == "clean"
    assert result.commits_since_verify == 0
    assert result.recommendation is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_status_drift_when_commits_after_verify(
    tmp_path: Path,
) -> None:
    """The load-bearing case: commits landed since the last verify, so
    the calendar may say 'fresh' but the project has moved. Status is
    'drift', count matches, recommendation includes the count and
    actionable next steps (memory_verify / memory_update)."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "anchor", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _commit_at(tmp_path, "after-1", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    _commit_at(tmp_path, "after-2", when=datetime(2026, 2, 2, tzinfo=timezone.utc))
    _commit_at(tmp_path, "after-3", when=datetime(2026, 2, 3, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
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
    _commit_at(tmp_path, "after", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
    )
    assert result is not None
    assert result.commits_since_verify == 1
    assert result.recommendation is not None
    assert "1 commit landed" in result.recommendation
    assert "1 commits" not in result.recommendation


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_to_dict_shape_clean(tmp_path: Path) -> None:
    """JSON shape is uniform across status values so consumers can branch
    on `status` alone without an existence check on every field."""
    _init_repo_with_remote(tmp_path, remote=_REMOTE)
    _commit_at(tmp_path, "older", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        memory_origin_repo=_REMOTE,
        caller_origin=Origin(cwd=str(tmp_path), repo=_REMOTE, branch="main"),
    )
    assert result is not None
    payload = result.to_dict()
    assert payload == {
        "status": "clean",
        "commits_since_verify": 0,
        "recommendation": None,
    }


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_normalised_repo_url_still_matches(tmp_path: Path) -> None:
    """Memory's origin.repo is the SSH form; caller's is HTTPS. They
    describe the same project — repos_match should normalise away the
    surface form and commit_drift should fire."""
    _init_repo_with_remote(tmp_path, remote="https://github.com/example/foo.git")
    _commit_at(tmp_path, "anchor", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _commit_at(tmp_path, "after", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    result = compute_commit_drift(
        last_verified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        memory_origin_repo="git@github.com:example/foo.git",
        caller_origin=Origin(
            cwd=str(tmp_path),
            repo="https://github.com/example/foo.git",
            branch="main",
        ),
    )
    assert result is not None
    assert result.status == "drift"
    assert result.commits_since_verify == 1


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
