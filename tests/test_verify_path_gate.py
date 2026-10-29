"""The path gate: no read stats a network, device or escaping spelling.

A memory body is written by whatever the model was shown, so it can cite a
path spelled to reach outside the machine: ``/\\host\\share\\x.txt`` is a
UNC path on Windows, and every retrieval that read the body stat'd it,
which opens an SMB connection to that host and hands it the user's NTLM
hash (ENG-164). A relative citation or attestation joined onto the root,
``src/../../../../x``, named a file outside the tree it is a claim about
and was stat'd there too.

Every test here runs the reads with each stat-shaped call spied
(`stat_spy`), so a spelling that reaches the disk is recorded, never
followed: the spy answers "absent" for it without touching the network.
Each test asserts the spellings were never stat'd and that the report,
and so the record's verdict, reads as if the body had never named them.
"""

from __future__ import annotations

import ntpath
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.verify import (
    ClaimAnchor,
    PathDriftReport,
    _extract_candidates,
    _normalize_candidate,
    commit_drift_anchor_paths,
    compute_staleness_verdict,
    compute_verification_status,
    detect_path_drift,
    unverifiable_attestations,
)
from bettermemory import verify_candidates

#: The host every hostile spelling names; a stat-shaped call whose
#: argument carries it is recorded by `stat_spy`.
HOST = "evil.example"

#: Absolute spellings a body can cite: UNC with either slash, the device
#: namespace (``\\.\UNC\...``), and the POSIX SMB mount-source form.
ABSOLUTE = (
    f"/\\{HOST}\\share\\x.txt",
    f"/\\.\\UNC\\{HOST}\\share\\x.txt",
    f"//{HOST}/share/x.txt",
)

#: Spellings only an attestation carries (the body scan never lifts a
#: leading backslash or a URL): a plain UNC path, a slash-backslash pair,
#: a relative claim that climbs out of the root to a host-named directory
#: in both separators, a URL and a name holding a NUL byte.
ATTESTED_ONLY = (
    f"\\\\{HOST}\\share\\x.txt",
    f"\\/{HOST}/share/x.txt",
    f"src/../../../../{HOST}/share/x.py",
    f"src\\..\\..\\..\\..\\{HOST}\\share\\x.py",
    f"file://{HOST}/share/x.txt",
    f"src/{HOST}\0x.py",
)

#: The readers of an attestation: at a recorded root, at a claim anchor
#: with a root, and at a claim anchor with none (a record whose project
#: has no live checkout here, and a global record).
READERS = ["worktree_root", "anchor", "no_live_checkout", "global_record"]

#: A relative citation that climbs out of the root; it passes the citation
#: filter (a directory segment, a `.py` extension) and was joined and
#: stat'd before the gate.
ESCAPING_CITATION = f"src/../../../../{HOST}/share/x.py"

_BASE_BODY = "The loader is in `src/real.py` and the notes in `{root}/notes.md`."


@pytest.fixture
def stat_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every stat-shaped call (`os.stat`, `os.lstat` and the `os.path`
    predicates and resolvers) whose argument names `HOST`, recorded and
    answered as absent without running: on Windows the real call would
    open a connection to the host. Everything else runs as it did."""
    seen: list[str] = []

    def _named(arg: Any) -> bool:
        try:
            return HOST in os.fspath(arg)
        except TypeError:
            return False

    def _raising(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def spy(path: Any, *args: Any, **kwargs: Any) -> Any:
            if _named(path):
                seen.append(f"{name}({os.fspath(path)})")
                raise FileNotFoundError(path)
            return real(path, *args, **kwargs)

        return spy

    def _false(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def spy(path: Any, *args: Any, **kwargs: Any) -> Any:
            if _named(path):
                seen.append(f"{name}({os.fspath(path)})")
                return False
            return real(path, *args, **kwargs)

        return spy

    def _same(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def spy(path: Any, *args: Any, **kwargs: Any) -> Any:
            if _named(path):
                seen.append(f"{name}({os.fspath(path)})")
                return os.fspath(path)
            return real(path, *args, **kwargs)

        return spy

    monkeypatch.setattr(os, "stat", _raising("stat", os.stat))
    monkeypatch.setattr(os, "lstat", _raising("lstat", os.lstat))
    for name in ("exists", "lexists", "isdir", "isfile", "islink"):
        monkeypatch.setattr(os.path, name, _false(name, getattr(os.path, name)))
    monkeypatch.setattr(os.path, "realpath", _same("realpath", os.path.realpath))
    return seen


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A tree with one real cited file under ``src/`` and one notes file."""
    made = tmp_path / "proj"
    (made / "src").mkdir(parents=True)
    (made / "src" / "real.py").write_text("x = 1\n")
    (made / "notes.md").write_text("notes\n")
    return made


def _hostile_body(root: Path) -> str:
    cited = " ".join(f"`{s}` and {s} ." for s in ABSOLUTE)
    return (
        _BASE_BODY.format(root=root)
        + f" Also {cited} The share copy is `{ESCAPING_CITATION}`, or"
        f" {ESCAPING_CITATION} bare."
    )


def _verdict(report: PathDriftReport) -> str:
    """The verdict of a record verified yesterday that carries `report`:
    fresh unless a claim-anchored miss escalates it."""
    now = datetime.now(timezone.utc)
    return compute_staleness_verdict(
        verification=compute_verification_status(now - timedelta(days=1), now=now),
        path_drift_missing=len(report.claim_anchored_missing),
        commit_drift_count=None,
    )


def test_the_gate_refuses_each_network_and_device_spelling() -> None:
    for spelled in (*ABSOLUTE, *ATTESTED_ONLY[:2]):
        assert verify_candidates._refused_spelling(spelled), spelled
        assert _normalize_candidate(spelled) is None, spelled
    for spelled in (f"src/{HOST}\0x.py", f"file://{HOST}/x", "https://h/x.py"):
        assert verify_candidates._refused_spelling(spelled), spelled
        assert _normalize_candidate(spelled) is None, spelled
    for spelled in ("/etc/hosts", "~/x/y.txt", "C:\\Users\\me\\x.txt", "C:/a/b.py"):
        assert not verify_candidates._refused_spelling(spelled), spelled


@pytest.mark.parametrize(
    ("rel", "escapes"),
    [
        ("src/x.py", False),
        ("src/../x.py", False),
        ("./src/x.py", False),
        ("a/b/../../c.py", False),
        ("..", True),
        ("../x.py", True),
        ("src/../../x.py", True),
        ("src/../../proj/src/x.py", True),
        ("src\\..\\..\\x.py", True),
        ("/abs/x.py", True),
        ("\\rooted\\x.py", True),
        ("C:x.py", True),
        ("C:\\x.py", True),
        ("file://host/x.py", True),
        ("src/a\0b.py", True),
    ],
)
def test_escapes_root_reads_both_separators_lexically(rel: str, escapes: bool) -> None:
    assert verify_candidates._escapes_root(rel) is escapes


def test_the_body_scan_emits_no_hostile_candidate(
    root: Path, stat_spy: list[str]
) -> None:
    candidates = [path for path, _, _ in _extract_candidates(_hostile_body(root))]
    assert not [c for c in candidates if HOST in c]
    assert stat_spy == []


@pytest.mark.parametrize("reader", ["worktree_root", "anchor"])
def test_a_hostile_body_is_never_stat_and_reads_as_the_plain_one(
    root: Path, stat_spy: list[str], reader: str
) -> None:
    def read(body: str, **kw: Any) -> PathDriftReport:
        if reader == "anchor":
            anchor = ClaimAnchor(root=str(root), rule="home_checkout")
            return detect_path_drift(body, anchor=anchor, **kw)
        return detect_path_drift(body, worktree_root=str(root), **kw)

    plain = read(_BASE_BODY.format(root=root))
    hostile = read(_hostile_body(root))
    assert stat_spy == []
    assert hostile == plain
    assert _verdict(hostile) == _verdict(plain) == "fresh"
    assert not any(HOST in p for p in hostile.checked)


@pytest.mark.parametrize("reader", READERS)
def test_hostile_attestations_are_never_stat_and_change_no_bucket(
    root: Path, stat_spy: list[str], reader: str
) -> None:
    body = _BASE_BODY.format(root=root)
    hostile = [*ABSOLUTE, *ATTESTED_ONLY]

    def read(verified: list[str], absent: list[str]) -> PathDriftReport:
        if reader != "worktree_root":
            anchor = _anchor(root, reader)
            return detect_path_drift(
                body, verified_paths=verified, absent_paths=absent, anchor=anchor
            )
        return detect_path_drift(
            body,
            verified_paths=verified,
            absent_paths=absent,
            worktree_root=str(root),
        )

    plain = read(["src/real.py"], [])
    attested = read(["src/real.py", *hostile], hostile)
    assert stat_spy == []
    assert attested == plain
    assert _verdict(attested) == _verdict(plain) == "fresh"
    assert not attested.claim_anchored_missing


def _anchor(root: Path, reader: str) -> ClaimAnchor:
    """The claim anchor `reader` names: the home checkout at `root`, or
    one with no root under the rule `reader` names."""
    if reader == "anchor":
        return ClaimAnchor(root=str(root), rule="home_checkout")
    return ClaimAnchor(root=None, rule=reader)


#: Spellings Windows reads as absolute (``ntpath.isabs``) that POSIX reads
#: as relative: a resolve of any of them on Windows opens the host.
WINDOWS_ABSOLUTE = (
    f"\\\\{HOST}\\share\\x.txt",
    f"\\/{HOST}/share/x.txt",
    f"\\\\?\\UNC\\{HOST}\\share\\x.txt",
    f"\\\\.\\UNC\\{HOST}\\share\\x.txt",
)


@pytest.mark.parametrize("rule", ["no_live_checkout", "global_record"])
def test_a_rootless_anchor_resolves_no_network_attestation_as_windows_would(
    stat_spy: list[str], monkeypatch: pytest.MonkeyPatch, rule: str
) -> None:
    """With no root to join onto, a relative attestation used to be read
    unconfirmed as spelled, past the gate; on Windows each of these
    spellings is absolute, and keying the read resolved it, which opens an
    SMB connection to the host. Read here with Windows' ``isabs``."""
    monkeypatch.setattr(os.path, "isabs", ntpath.isabs)
    anchor = ClaimAnchor(root=None, rule=rule)
    hostile = list(WINDOWS_ABSOLUTE)
    report = detect_path_drift(
        "notes", verified_paths=hostile, absent_paths=hostile, anchor=anchor
    )
    assert stat_spy == []
    assert not [p for p in report.unconfirmed if HOST in p]
    assert report == detect_path_drift("notes", anchor=anchor)


def test_the_write_gate_skips_hostile_attestations_without_a_stat(
    root: Path, stat_spy: list[str]
) -> None:
    hostile = [*ABSOLUTE, *ATTESTED_ONLY]
    anchor = ClaimAnchor(root=str(root), rule="home_checkout")
    assert unverifiable_attestations(hostile, worktree_root=str(root)) == []
    assert unverifiable_attestations(hostile, anchor=anchor) == []
    assert unverifiable_attestations(hostile, anchor=anchor, stored=True) == []
    assert stat_spy == []


def test_no_commit_anchor_carries_a_hostile_spelling(
    root: Path, stat_spy: list[str]
) -> None:
    hostile = [*ABSOLUTE, *ATTESTED_ONLY]
    anchors = commit_drift_anchor_paths(_hostile_body(root), hostile)
    assert not [a for a in anchors if HOST in a]
    assert "src/real.py" in anchors
    assert stat_spy == []
