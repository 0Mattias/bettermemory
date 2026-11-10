"""A literal target stays the same through a real verify, mutation and read."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory.origin import Origin
from bettermemory.store import Store

from .test_relative_attestation_suffixes import _T0, _commit, _git, _repo, home
from .test_server_commit_drift import _REMOTE, _call, server_with_fake_origin

__all__ = ["home", "server_with_fake_origin"]

_LATER = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _spelling(target: Path, repo: Path, kind: str) -> str:
    if kind == "relative":
        return target.relative_to(repo).as_posix()
    if kind == "home":
        return "~/" + target.relative_to(Path(os.path.realpath(Path.home()))).as_posix()
    return str(target)


def _literal_directory(target: Path) -> None:
    target.mkdir(parents=True)
    if target.name not in {entry.name for entry in target.parent.iterdir()}:
        pytest.skip("this filesystem normalizes the literal trailing dot")


@pytest.mark.parametrize("name", ["routes/[slug]", "app/[locale]", "docs/v1."])
@pytest.mark.parametrize("kind", ["relative", "absolute", "home"])
@pytest.mark.parametrize("basis", ["reachability", "author-date"])
async def test_verified_literal_directory_is_still_that_directory_after_deletion(
    home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_with_fake_origin,
    name: str,
    kind: str,
    basis: str,
) -> None:
    repo, _ = _repo(home)
    _git(repo, "remote", "add", "origin", _REMOTE)
    target = repo / name
    _literal_directory(target)
    _commit(repo, name + "/settings.py", "LIMIT = 1\n", _T0)
    raw = _spelling(target, repo, kind)
    server = server_with_fake_origin(
        Origin(cwd=str(repo), repo=_REMOTE, branch="main", worktree_root=str(repo))
    )
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    written = await _call(
        server,
        "memory_write",
        content="The loader reads its settings once at startup.",
        scopes=["tools"],
    )
    verified = await _call(
        server, "memory_verify", id=written["id"], verified_paths=[raw]
    )
    assert verified["verified_paths"] == []
    assert raw in verified["verified_path_targets"]
    if basis == "author-date":
        with Store(memory_dir) as store:
            store.mark_verified(written["id"], verified_head=None)
    _git(repo, "rm", "-r", "--", name)
    _git(repo, "commit", "-q", "-m", "remove checked directory", when=_LATER)
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["path_drift"]["missing"] == [str(target)]
    assert shown["path_drift"]["claim_anchored_missing"] == [str(target)]
    assert shown["commit_drift"]["basis"] == basis
    assert shown["commit_drift"]["commits_since_verify"] == 1
    with pytest.raises(Exception, match="cannot attest"):
        await _call(server, "memory_verify", id=written["id"])


@pytest.mark.parametrize("name", ["routes/[slug]", "app/[locale]", "docs/v1."])
@pytest.mark.parametrize("kind", ["relative", "absolute", "home"])
@pytest.mark.parametrize("basis", ["reachability", "author-date"])
async def test_an_intentionally_absent_literal_directory_anchors_its_appearance(
    home: Path,
    memory_dir: Path,
    server_with_fake_origin,
    name: str,
    kind: str,
    basis: str,
) -> None:
    repo, _ = _repo(home)
    _git(repo, "remote", "add", "origin", _REMOTE)
    target = repo / name
    raw = _spelling(target, repo, kind)
    server = server_with_fake_origin(
        Origin(cwd=str(repo), repo=_REMOTE, branch="main", worktree_root=str(repo))
    )
    written = await _call(
        server,
        "memory_write",
        content="The loader reads its settings once at startup.",
        scopes=["tools"],
    )
    verified = await _call(
        server, "memory_verify", id=written["id"], verified_absent_paths=[raw]
    )
    assert verified["verified_absent_paths"] == []
    assert raw in verified["verified_absent_path_targets"]
    assert (await _call(server, "memory_verify", id=written["id"]))[
        "verified"
    ] == written["id"]
    if basis == "author-date":
        with Store(memory_dir) as store:
            store.mark_verified(written["id"], verified_head=None)
    _literal_directory(target)
    _commit(repo, name + "/settings.py", "LIMIT = 1\n", _LATER)
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["path_drift"]["present_but_attested_absent"] == [str(target)]
    for expand in (False, True):
        found = await _call(
            server,
            "memory_search",
            query="loader settings",
            auto_scope=False,
            expand_top=expand,
        )
        hits = found.get("result", found) if isinstance(found, dict) else found
        hit = next(hit for hit in hits if hit["id"] == written["id"])
        assert hit["path_drift"]["present_but_attested_absent"] == [str(target)]
    assert shown["commit_drift"]["basis"] == basis
    assert shown["commit_drift"]["commits_since_verify"] == 1


@pytest.mark.parametrize(
    "name",
    [
        "src/app.py,",
        pytest.param(
            "src/app.py:2",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="colon is not a Windows filename"
            ),
        ),
    ],
)
@pytest.mark.parametrize("mode", ["auto", "literal", "reference"])
@pytest.mark.parametrize("basis", ["reachability", "author-date"])
async def test_path_mode_fixes_the_choice_between_a_literal_and_a_reference_twin(
    home: Path,
    memory_dir: Path,
    server_with_fake_origin,
    name: str,
    mode: str,
    basis: str,
) -> None:
    repo, _ = _repo(home)
    _git(repo, "remote", "add", "origin", _REMOTE)
    _commit(repo, name, "literal = 1\n", _T0)
    server = server_with_fake_origin(
        Origin(cwd=str(repo), repo=_REMOTE, branch="main", worktree_root=str(repo))
    )
    written = await _call(
        server,
        "memory_write",
        content="The loader reads its settings once at startup.",
        scopes=["tools"],
    )
    verified = await _call(
        server, "memory_verify", id=written["id"], verified_paths=[name], path_mode=mode
    )
    chosen = "src/app.py" if mode == "reference" else name
    assert verified["verified_path_targets"] == {name: chosen}
    assert verified["verified_paths"] == ([name] if mode == "reference" else [])
    if basis == "author-date":
        with Store(memory_dir) as store:
            store.mark_verified(written["id"], verified_head=None)
    _git(repo, "rm", "--", name)
    _git(repo, "commit", "-q", "-m", "remove literal twin", when=_LATER)
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["commit_drift"]["basis"] == basis
    assert shown["commit_drift"]["commits_since_verify"] == (
        0 if mode == "reference" else 1
    )
    if mode == "reference":
        assert shown["path_drift"]["missing"] == []
    else:
        assert shown["path_drift"]["missing"] == [str(repo / name)]


async def test_body_edits_clear_targets_and_metadata_edits_preserve_them(
    home: Path,
    memory_dir: Path,
    server_with_fake_origin,
) -> None:
    repo, _ = _repo(home)
    _git(repo, "remote", "add", "origin", _REMOTE)
    _commit(repo, "src/app.py,", "literal = 1\n", _T0)
    server = server_with_fake_origin(
        Origin(cwd=str(repo), repo=_REMOTE, branch="main", worktree_root=str(repo))
    )
    written = await _call(
        server,
        "memory_write",
        content="The loader reads its settings once at startup.",
        scopes=["tools"],
    )
    await _call(
        server, "memory_verify", id=written["id"], verified_paths=["src/app.py,"]
    )
    await _call(server, "memory_update", id=written["id"], confidence="high")
    with Store(memory_dir) as store:
        assert store.load_one(written["id"]).verified_path_targets == {
            "src/app.py,": "src/app.py,"
        }
    await _call(
        server,
        "memory_update",
        id=written["id"],
        content="The loader now reads its settings for each request.",
    )
    with Store(memory_dir) as store:
        updated = store.load_one(written["id"])
        assert updated.last_verified_at is None
        assert updated.verified_path_targets is None
        assert updated.verified_absent_path_targets is None
        assert updated.verified_paths == []
        assert store.log_verify()["status"] == "ok"


@pytest.mark.parametrize(
    "placeholder", ["/etc/foo", "/path/to/thing", "/path/to/config.toml"]
)
async def test_compiling_targets_does_not_bypass_the_placeholder_refusal(
    home: Path,
    server_with_fake_origin,
    placeholder: str,
) -> None:
    repo, _ = _repo(home)
    _git(repo, "remote", "add", "origin", _REMOTE)
    server = server_with_fake_origin(
        Origin(cwd=str(repo), repo=_REMOTE, branch="main", worktree_root=str(repo))
    )
    written = await _call(
        server,
        "memory_write",
        content="The loader reads its settings once at startup.",
        scopes=["tools"],
    )
    with pytest.raises(Exception, match="cannot attest"):
        await _call(
            server, "memory_verify", id=written["id"], verified_paths=[placeholder]
        )
