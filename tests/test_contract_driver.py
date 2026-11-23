"""The contract driver's check that a generator leaves no process-wide
state of the reference set (`tests.contract.generate.run_generator`).

The as-found run makes every golden file in one process, in registration
order; a caller one generator leaves published answers
`identity.workspace_declaration` in a later one. The suite resets the
caller before each test, so only this check meets such a leak here."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from bettermemory import events, git_read_budget, identity
from tests.contract.generate import LeftStateError, run_generator


def _caller() -> identity.Caller:
    workspace = identity.Workspace(path="/planted", source="env")
    return identity.Caller(workspace=workspace)


def test_a_generator_that_leaves_a_caller_published_fails_by_name() -> None:
    def leaky() -> dict[str, bytes]:
        identity.publish(_caller())
        return {"planted.json": b"{}\n"}

    found = identity._CURRENT.set(None)
    try:
        with pytest.raises(LeftStateError, match=r"generator planted_leak .*_CURRENT"):
            run_generator("planted_leak", leaky)
    finally:
        identity._CURRENT.reset(found)


def test_a_generator_that_restores_the_caller_passes() -> None:
    def clean() -> dict[str, bytes]:
        token = identity.publish(_caller())
        identity.reset(token)
        return {"planted.json": b"{}\n"}

    assert run_generator("planted_clean", clean) == {"planted.json": b"{}\n"}


def test_a_caller_published_before_the_generator_is_not_its_fault() -> None:
    found = identity.publish(_caller())
    try:
        assert run_generator("planted_clean", lambda: {"a": b""}) == {"a": b""}
    finally:
        identity.reset(found)


@pytest.mark.parametrize("name", ["event_recorder", "event_recorder_python"])
def test_recorder_fixture_is_independent_of_request_context(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    expected = run_generator(name)
    caller = _caller()
    token = identity.publish(caller)
    try:
        with (
            events.suppressed(),
            caplog.at_level(logging.ERROR, logger=events.log.name),
        ):
            assert run_generator(name) == expected
            assert identity.current() is caller
    finally:
        identity.reset(token)


def test_another_context_var_left_set_fails_by_name() -> None:
    def leaky() -> dict[str, bytes]:
        git_read_budget._ROOT.set(("/planted", True))
        return {}

    found = git_read_budget._ROOT.set(None)
    try:
        with pytest.raises(LeftStateError, match=r"planted_root .*_ROOT"):
            run_generator("planted_root", leaky)
    finally:
        git_read_budget._ROOT.reset(found)


def test_an_environment_variable_left_set_fails_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BM_PLANTED_LEAK", "before the test")
    monkeypatch.delenv("BM_PLANTED_LEAK", raising=False)

    def leaky() -> dict[str, bytes]:
        os.environ["BM_PLANTED_LEAK"] = "1"
        return {}

    with pytest.raises(LeftStateError, match=r"planted_env .*BM_PLANTED_LEAK"):
        run_generator("planted_env", leaky)
    assert "BM_PLANTED_LEAK" not in os.environ


@pytest.mark.parametrize("raises", [False, True])
def test_next_generator_inherits_the_original_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises: bool
) -> None:
    original_dir = Path.cwd()
    original_caller = identity.current()
    monkeypatch.setenv("BM_PLANTED_EXISTING", "original")
    monkeypatch.setenv("BM_PLANTED_ADDED", "before the test")
    monkeypatch.delenv("BM_PLANTED_ADDED")
    failure = ValueError("planted generator failed")

    def leaky() -> dict[str, bytes]:
        identity.publish(_caller())
        os.chdir(tmp_path)
        del os.environ["BM_PLANTED_EXISTING"]
        os.environ["BM_PLANTED_ADDED"] = "temporary"
        if raises:
            raise failure
        return {}

    # Keep the test worker usable even if the guard regresses.
    monkeypatch.chdir(original_dir)
    token = identity._CURRENT.set(original_caller)
    try:
        if raises:
            with pytest.raises(ValueError) as caught:
                run_generator("leaky", leaky)
            assert caught.value is failure
            diagnostics = " ".join(failure.__notes__)
        else:
            with pytest.raises(LeftStateError) as leak:
                run_generator("leaky", leaky)
            diagnostics = str(leak.value)
        for detail in (
            "leaky",
            "_CURRENT",
            "working directory",
            "BM_PLANTED_EXISTING",
            "BM_PLANTED_ADDED",
        ):
            assert detail in diagnostics
        assert Path.cwd() == original_dir
        assert identity.current() is original_caller
        assert os.environ["BM_PLANTED_EXISTING"] == "original"
        assert "BM_PLANTED_ADDED" not in os.environ
    finally:
        identity._CURRENT.reset(token)


@pytest.mark.skipif(os.chdir not in os.supports_fd, reason="directory handles required")
def test_a_generator_can_rename_its_original_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "before"
    moved = tmp_path / "after"
    original.mkdir()
    monkeypatch.chdir(original)
    monkeypatch.setenv("BM_PLANTED_EXISTING", "original")
    failure = ValueError("planted generator failed")

    def leaky() -> dict[str, bytes]:
        original.rename(moved)
        os.chdir(tmp_path)
        os.environ["BM_PLANTED_EXISTING"] = "temporary"
        raise failure

    with pytest.raises(ValueError) as caught:
        run_generator("rename", leaky)
    assert caught.value is failure
    assert Path.cwd() == moved
    assert os.environ["BM_PLANTED_EXISTING"] == "original"
    assert any("working directory" in note for note in failure.__notes__)


@pytest.mark.parametrize("raises", [False, True])
def test_inherited_mutable_context_is_private(raises: bool) -> None:
    from bettermemory import config

    original = _caller()
    token = identity.publish(original)
    notes = ["before"]
    trial = config._TRIAL.set(notes)
    error = ValueError("fixture failed")
    try:
        with git_read_budget.call_scope(original, "driver") as owner:
            # A progress object normally belongs to the store and can be shared
            # by the owner and active budget. Caller objects may be unhashable.
            owner.progress = git_read_budget._Progress(resume=("resume", True))
            with git_read_budget.reading("original") as active:
                assert owner is active

                def mutate() -> dict[str, bytes]:
                    caller = identity.current()
                    assert caller is not None and caller == original
                    caller.workspace.path = "/inside"
                    config._trial_note("inside")
                    local = git_read_budget._ACTIVE.get()
                    assert local is git_read_budget._OWNER.get()
                    assert local is not None and local.progress is not None
                    assert local.progress.resume == ("resume", True)
                    assert owner.progress is not None
                    assert local.progress.lock is not owner.progress.lock
                    git_read_budget.allowance(1.0, "root", None)
                    local.finish()
                    if raises:
                        raise error
                    return {}

                if raises:
                    with pytest.raises(ValueError) as caught:
                        run_generator("mutate", mutate)
                    assert caught.value is error
                    diagnostic = " ".join(error.__notes__)
                else:
                    with pytest.raises(LeftStateError) as leak:
                        run_generator("mutate", mutate)
                    diagnostic = str(leak.value)
                for name in ("identity._CURRENT", "config._TRIAL", "_ACTIVE", "_OWNER"):
                    assert name in diagnostic
                assert (
                    identity.current() is original
                    and original.workspace.path == "/planted"
                )
                assert config._TRIAL.get() is notes and notes == ["before"]
                assert active.deadline is None and active.last_root is None
                assert owner.progress.resume == ("resume", True)
                assert run_generator("next", dict) == {}
    finally:
        config._TRIAL.reset(trial)
        identity.reset(token)


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("raises", [False, True])
def test_as_found_stops_after_unrecoverable_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace: bool, raises: bool
) -> None:
    from tests.contract import generate

    original = tmp_path / "original"
    original.mkdir()
    monkeypatch.chdir(original)
    monkeypatch.setattr(os, "supports_fd", set())
    monkeypatch.setenv("BM_DRIVER_ENV", "before")
    error = ValueError("original failure")

    def lose_original() -> dict[str, bytes]:
        os.chdir(tmp_path)
        original.rename(tmp_path / "moved")
        if replace:
            original.mkdir()
            os.chdir(original)
        os.environ["BM_DRIVER_ENV"] = "changed"
        if raises:
            raise error
        return {}

    def successor() -> dict[str, bytes]:
        pytest.fail("a generator ran in the wrong working directory")

    monkeypatch.setattr(
        generate, "GENERATORS", {"first": lose_original, "next": successor}
    )
    with pytest.raises((ValueError, LeftStateError)) as caught:
        generate.as_found(tmp_path / "out")
    if raises:
        assert caught.value is error
        assert any("could not restore" in note for note in error.__notes__)
    else:
        assert "could not restore" in str(caught.value)
    assert os.environ["BM_DRIVER_ENV"] == "before"


@pytest.mark.skipif(
    os.chdir not in os.supports_fd
    or not (hasattr(os, "O_SEARCH") or hasattr(os, "O_PATH")),
    reason="searchable directory descriptor required",
)
def test_searchable_cwd_is_restored_after_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "searchable"
    original.mkdir(mode=0o111)
    monkeypatch.chdir(original)
    renamed = tmp_path / "renamed"

    def change() -> dict[str, bytes]:
        os.chdir(tmp_path)
        original.rename(renamed)
        original.mkdir()
        os.chdir(original)
        return {}

    try:
        with pytest.raises(LeftStateError, match="working directory"):
            run_generator("rename", change)
        assert os.path.samestat(os.stat("."), renamed.stat())
    finally:
        (renamed if renamed.exists() else original).chmod(0o700)
