"""Tests for verify.py — path-drift detection on memory bodies.

Detection is the load-bearing piece. Existence checks against the real
filesystem use temp paths fixture-style (paths created/deleted in the
test) to keep behaviour deterministic across machines.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bettermemory.verify import (
    PathDriftReport,
    detect_path_drift,
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
    assert d == {"checked": ["/a", "/b"], "missing": ["/b"]}


def test_has_drift_only_when_missing_nonempty() -> None:
    healthy = PathDriftReport(checked=("/a",), missing=())
    drifted = PathDriftReport(checked=("/a",), missing=("/a",))
    assert healthy.has_drift is False
    assert drifted.has_drift is True


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
