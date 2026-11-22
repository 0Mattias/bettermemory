"""The contract driver's check that a generator leaves no process-wide
state of the reference set (`tests.contract.generate.run_generator`).

The as-found run makes every golden file in one process, in registration
order; a caller one generator leaves published answers
`identity.workspace_declaration` in a later one. The suite resets the
caller before each test, so only this check meets such a leak here."""

from __future__ import annotations

import os

import pytest

from bettermemory import git_read_budget, identity
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
    monkeypatch.delenv("BM_PLANTED_LEAK", raising=False)

    def leaky() -> dict[str, bytes]:
        os.environ["BM_PLANTED_LEAK"] = "1"
        return {}

    with pytest.raises(LeftStateError, match=r"planted_env .*BM_PLANTED_LEAK"):
        run_generator("planted_env", leaky)
