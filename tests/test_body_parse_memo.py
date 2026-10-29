"""The body parse memo (`verify._CANDIDATES`, `verify._ANCHORS`).

A verdict reads a memory's body twice: the path drift leg extracts its
path candidates, and the commit leg extracts them again with the relative
citations for the claim anchors. The memo keeps both results keyed on a
digest of the body, the attestations the anchors read and the home
directory. What this file pins: a hit equals the uncached parse, an edited
body is parsed afresh, every input the result reads is in the key, the
outputs are the same with the memo on and off, the bound, the threads, and
the registry.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _caches, verify, verify_candidates
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin
from bettermemory.verdict import memory_verdict
from bettermemory.verify import commit_drift_anchor_paths, detect_path_drift


def _corpus(root: Path) -> list[str]:
    """Bodies that reach every branch the two parses take: backtick and
    bare paths, a duplicate in both forms, `~` and `$HOME` spellings, a
    route, an ambiguous truncation, relative citations, and none."""
    present = root / "present.toml"
    present.write_text("x")
    (root / "src").mkdir()
    (root / "src" / "mod.py").write_text("x")
    return [
        f"The config lives at `{present}` and nowhere else.",
        f"Bare {present} then `{present}` again, and {root}/missing.conf. Done.",
        "Home spellings: ~/.config/tool/settings.json and $HOME/.zshrc here.",
        "A route https://example.com/api/v1 and /api/v1/users beside it.",
        f"Spaced {root}/Visual Studio Code.app is truncated here.",
        "Cites src/mod.py:12, docs/guide.md and CHANGELOG.md in prose.",
        "A preference with no path in it at all.",
        "",
    ]


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Counts of the uncached parses run, by kind."""
    counts = {"candidates": 0, "anchors": 0}
    scan, anchor = verify._scan_candidates, verify._anchor_paths

    def scan_spy(body: str) -> Any:
        counts["candidates"] += 1
        return scan(body)

    def anchor_spy(body: str, attested: tuple[str, ...]) -> Any:
        counts["anchors"] += 1
        return anchor(body, attested)

    monkeypatch.setattr(verify_candidates, "_scan_candidates", scan_spy)
    monkeypatch.setattr(verify, "_anchor_paths", anchor_spy)
    return counts


def test_a_hit_equals_the_parse_and_runs_none(counted: dict[str, int]) -> None:
    body = "Cites `/etc/hosts` and src/pkg/mod.py beside /var/log/app.log."
    first = verify._extract_candidates(body)
    second = verify._extract_candidates(body)
    assert first == second == verify_candidates._scan_candidates(body)
    assert counted["candidates"] == 2  # the first call and the direct one
    anchors = commit_drift_anchor_paths(body, ["/etc/hosts"])
    assert commit_drift_anchor_paths(body, ["/etc/hosts"]) == anchors
    assert anchors == verify._anchor_paths(body, ("/etc/hosts",))
    assert counted["anchors"] == 2


def test_a_caller_cannot_change_a_kept_result() -> None:
    body = "Cites `/etc/hosts` here."
    first = verify._extract_candidates(body)
    first.clear()
    assert verify._extract_candidates(body) == [("/etc/hosts", False, False)]


def test_an_edited_body_is_parsed_afresh(tmp_path: Path) -> None:
    present = tmp_path / "present.toml"
    present.write_text("x")
    gone = tmp_path / "gone.toml"
    now = datetime.now(timezone.utc)
    before = Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=f"The config is `{present}`.\n",
    )
    after = before.model_copy(update={"body": f"The config is `{gone}`.\n"})
    assert detect_path_drift(before.body).missing == ()
    assert detect_path_drift(after.body).missing == (str(gone),)
    assert commit_drift_anchor_paths(after.body) == (str(gone),)
    # The verdict a brief reads for the edited row names the new body.
    origin = Origin(repo=None, worktree_root=str(tmp_path), cwd=str(tmp_path))
    verdicts = [
        memory_verdict(m, caller_origin=origin, now=now, stale_after_days=30)
        for m in (before, after, before)
    ]
    assert verdicts[0] == verdicts[2]
    assert verdicts[1].path_drift.checked == (str(gone),)


def test_the_attestations_are_in_the_anchor_key() -> None:
    body = "Cites src/pkg/mod.py in prose."
    assert commit_drift_anchor_paths(body) == ("src/pkg/mod.py",)
    assert commit_drift_anchor_paths(body, ["/etc/hosts"]) == (
        "/etc/hosts",
        "src/pkg/mod.py",
    )
    # Entries the anchors skip (empty, not a string) key as if absent.
    assert commit_drift_anchor_paths(body, ["", 3]) == ("src/pkg/mod.py",)  # type: ignore[list-item]


def _home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point `Path.home()` at `home` on every platform: HOME for the POSIX
    `expanduser`, USERPROFILE for the Windows one, with HOMEDRIVE and
    HOMEPATH cleared so USERPROFILE wins."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)


def test_the_home_directory_is_in_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`~/x` and the expanded spelling are one claim only under the home
    that expands to it: the dedup reads the home, so the key does too."""
    body = f"Both ~/notes/a.txt and {tmp_path}/one/notes/a.txt are cited."
    _home(monkeypatch, tmp_path / "one")
    assert len(verify._extract_candidates(body)) == 1
    _home(monkeypatch, tmp_path / "two")
    assert len(verify._extract_candidates(body)) == 2
    assert verify._extract_candidates(body) == verify._scan_candidates(body)


def test_the_outputs_are_the_same_with_the_memo_on_and_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bodies = _corpus(tmp_path)
    attested = [(), (str(tmp_path / "present.toml"),), ("src/mod.py", "")]

    def outputs() -> list[Any]:
        return [
            (
                verify._extract_candidates(body),
                detect_path_drift(body, worktree_root=tmp_path),
                [commit_drift_anchor_paths(body, list(a)) for a in attested],
            )
            for body in bodies
        ]

    monkeypatch.setattr(verify_candidates, "BODY_PARSE_ENTRIES", 0)
    off = outputs()
    assert not verify._CANDIDATES and not verify._ANCHORS
    monkeypatch.setattr(verify_candidates, "BODY_PARSE_ENTRIES", 4096)
    cold = outputs()
    assert verify._CANDIDATES and verify._ANCHORS
    warm = outputs()
    assert off == cold == warm


def test_the_memo_is_bounded_to_the_most_recently_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_candidates, "BODY_PARSE_ENTRIES", 3)
    bodies = [f"Cites `/srv/app{i}/config.toml` here." for i in range(5)]
    for body in bodies:
        verify._extract_candidates(body)
    verify._extract_candidates(bodies[2])  # used again: newest
    verify._extract_candidates(bodies[0])  # parsed again, evicts bodies[3]
    kept = list(verify._CANDIDATES)
    assert len(kept) == 3
    assert kept == [verify._body_key(b) for b in (bodies[4], bodies[2], bodies[0])]


def test_threads_read_and_fill_the_memo_together(tmp_path: Path) -> None:
    bodies = _corpus(tmp_path) * 3
    expected = [
        (verify._scan_candidates(b), verify._anchor_paths(b, ())) for b in bodies
    ]
    _caches.clear_all()
    failures: list[int] = []

    def work(offset: int) -> None:
        for _ in range(50):
            for index in range(len(bodies)):
                position = (index + offset) % len(bodies)
                body = bodies[position]
                got = (
                    verify._extract_candidates(body),
                    commit_drift_anchor_paths(body),
                )
                if got != expected[position]:
                    failures.append(position)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []


def test_the_registry_empties_the_memo() -> None:
    verify._extract_candidates("Cites `/etc/hosts` here.")
    commit_drift_anchor_paths("Cites `/etc/hosts` here.")
    assert verify._CANDIDATES and verify._ANCHORS
    _caches.clear_all()
    assert not verify._CANDIDATES and not verify._ANCHORS
