"""`[policy]`: the write gates' escape hatches taken away from the agent.

Pinned end to end through the MCP tool surface, because the refusal's
form there is the contract (`policy.py`): a call that passes a forbidden
flag as true is refused whole as a tool error whose words name the switch
and whether the managed file pins it, and nothing is written. With both
switches off, the two tools answer exactly as they did before the section
existed, which `test_defaults_keep_every_escape_hatch` checks against the
flags' own documented behaviour.

Secret-shaped fixtures are assembled from fragments so no complete token
literal sits in source (see tests/test_server_credentials.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory import _handlers, capture, identity
from bettermemory.config import (
    ACKNOWLEDGE_NAMES,
    Config,
    PolicyConfig,
    StorageConfig,
    load_config,
)
from bettermemory.events import Recorder
from bettermemory.origin import Origin
from bettermemory.policy import REFUSAL_STATUS, PolicyRefusal, check_overrides
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool, crash_text, input_schema

_AWS = "".join(("AKIA", "IOSFODNN7EXAMPLE"))
_SECRET_BODY = f"The AWS docs use the example key {_AWS} in their tutorial."
_CLEAN_BODY = "The deploy pipeline pins its formatter version in CI."


class World:
    """One in-process server over one scratch store, with a policy."""

    def __init__(self, root: Path, config: Config) -> None:
        self.store = Store(root)
        self.state = SessionState()
        self.server = build_server(
            config=config,
            store=self.store,
            state=self.state,
            recorder=Recorder(store=self.store, session_id=self.state.session_id),
        )

    async def call(self, tool: str, **kwargs: Any) -> Any:
        return await call_tool(self.server, tool, kwargs)

    async def refused(self, tool: str, **kwargs: Any) -> str:
        """The refusal's text, as the client reads it."""
        with pytest.raises(Exception) as excinfo:
            await self.call(tool, **kwargs)
        return crash_text(excinfo.value)

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.store.iter_events() if e["kind"] == kind]


def _config(
    root: Path,
    *,
    forbid_force: bool = False,
    forbid_acknowledge: list[str] | None = None,
    managed: frozenset[str] = frozenset(),
) -> Config:
    return Config(
        storage=StorageConfig(directory=str(root)),
        policy=PolicyConfig(
            forbid_force=forbid_force,
            forbid_acknowledge=list(forbid_acknowledge or []),
        ),
        managed_keys=managed,
    )


@pytest.fixture
def make_world(tmp_path: Path) -> Any:
    def make(**kwargs: Any) -> World:
        root = tmp_path / "store"
        return World(root, _config(root, **kwargs))

    return make


# ---------------------------------------------------------------------------
# Defaults: nothing changes
# ---------------------------------------------------------------------------


async def test_defaults_keep_every_escape_hatch(make_world: Any) -> None:
    """Both switches off: an acknowledged secret commits, `force` writes a
    duplicate, and memory_update takes its acknowledge flags, as before."""
    world = make_world()
    assert Config().policy == PolicyConfig()

    secret = await world.call(
        "memory_write",
        content=_SECRET_BODY,
        scopes=["reference"],
        acknowledge_credential=True,
    )
    assert secret["status"] == "committed", secret

    first = await world.call("memory_write", content=_CLEAN_BODY, scopes=["tools"])
    assert first["status"] == "committed", first
    duplicate = await world.call("memory_write", content=_CLEAN_BODY, scopes=["tools"])
    assert duplicate["status"] == "duplicate", duplicate
    forced = await world.call(
        "memory_write", content=_CLEAN_BODY, scopes=["tools"], force=True
    )
    assert forced["status"] == "committed", forced

    updated = await world.call(
        "memory_update",
        id=first["id"],
        content=_SECRET_BODY,
        acknowledge_credential=True,
    )
    assert updated["status"] == "committed", updated


def test_check_overrides_is_a_no_op_with_the_defaults() -> None:
    check_overrides(
        Config(),
        "memory_write",
        force=True,
        acknowledge={name: True for name in ACKNOWLEDGE_NAMES},
    )


# ---------------------------------------------------------------------------
# forbid_acknowledge
# ---------------------------------------------------------------------------


async def test_a_forbidden_acknowledge_refuses_the_write_whole(
    make_world: Any,
) -> None:
    world = make_world(forbid_acknowledge=["credential"])
    text = await world.refused(
        "memory_write",
        content=_SECRET_BODY,
        scopes=["reference"],
        acknowledge_credential=True,
    )
    assert f"{REFUSAL_STATUS}: memory_write was called with" in text, text
    assert "acknowledge_credential=True" in text
    assert "[policy] forbid_acknowledge" in text
    assert "set in the user's config" in text
    assert "managed" not in text
    assert "Nothing was written." in text
    # The secret is not echoed back, and nothing reached the store.
    assert _AWS not in text
    assert world.store.load_all() == []
    assert world.events("write") == []


async def test_the_flag_is_refused_even_where_its_gate_would_not_fire(
    make_world: Any,
) -> None:
    """The policy is about the flag, not the body: a clean body sent with
    the flag is refused too, so the agent learns the flag is gone."""
    world = make_world(forbid_acknowledge=["credential"])
    text = await world.refused(
        "memory_write",
        content=_CLEAN_BODY,
        scopes=["tools"],
        acknowledge_credential=True,
    )
    assert "acknowledge_credential=True" in text
    assert world.store.load_all() == []


async def test_without_the_flag_the_gate_answers_as_usual(make_world: Any) -> None:
    world = make_world(forbid_acknowledge=["credential", "transient"])
    warning = await world.call(
        "memory_write", content=_SECRET_BODY, scopes=["reference"]
    )
    assert warning["status"] == "credential_warning", warning
    committed = await world.call("memory_write", content=_CLEAN_BODY, scopes=["tools"])
    assert committed["status"] == "committed", committed
    # A flag the policy does not name keeps working.
    passed = await world.call(
        "memory_write",
        content="We use ruff for linting in this repository.",
        scopes=["tools"],
        acknowledge_user_claim=True,
    )
    assert passed["status"] == "committed", passed


async def test_every_forbidden_flag_is_named_in_one_refusal(make_world: Any) -> None:
    world = make_world(
        forbid_force=True, forbid_acknowledge=["credential", "scope_mismatch"]
    )
    text = await world.refused(
        "memory_write",
        content=_CLEAN_BODY,
        scopes=["tools"],
        force=True,
        acknowledge_credential=True,
        acknowledge_scope_mismatch=True,
        acknowledge_transient=True,
    )
    assert (
        "force=True and acknowledge_credential=True and "
        "acknowledge_scope_mismatch=True" in text
    ), text
    assert "acknowledge_transient" not in text
    assert "[policy] forbid_force" in text
    assert "[policy] forbid_acknowledge" in text
    assert "those flags" in text


async def test_a_managed_switch_says_so(make_world: Any) -> None:
    world = make_world(
        forbid_acknowledge=["credential"],
        managed=frozenset({"policy.forbid_acknowledge"}),
    )
    text = await world.refused(
        "memory_write",
        content=_SECRET_BODY,
        scopes=["reference"],
        acknowledge_credential=True,
    )
    assert "[policy] forbid_acknowledge (managed:" in text, text
    assert "user's own config cannot change" in text


async def test_memory_update_takes_the_same_policy(make_world: Any) -> None:
    world = make_world(forbid_acknowledge=["credential", "truncation"])
    written = await world.call(
        "memory_write",
        content="The release checklist lists the three gates a tag passes before PyPI.",
        scopes=["tools"],
    )
    before = world.store.load_one(written["id"])

    for flag in ("acknowledge_credential", "acknowledge_truncation"):
        text = await world.refused(
            "memory_update",
            id=written["id"],
            content="The release checklist lists the",
            **{flag: True},
        )
        assert f"{REFUSAL_STATUS}: memory_update was called with" in text, text
        assert f"{flag}=True" in text
        assert "Nothing was changed." in text

    assert world.store.load_one(written["id"]).body == before.body
    # No update event at all: a committed update records one without a
    # `status`, so filtering on the status would miss the very row a
    # refusal must not leave.
    assert world.events("update") == []

    # A flag the policy does not name passes, and the gate behind it runs.
    claimed = await world.call(
        "memory_update",
        id=written["id"],
        content="We use ruff for linting in this repository.",
        acknowledge_user_claim=True,
    )
    assert claimed["status"] == "committed", claimed


# ---------------------------------------------------------------------------
# forbid_force
# ---------------------------------------------------------------------------


async def test_forbid_force_refuses_force_and_keeps_the_duplicate_gate(
    make_world: Any,
) -> None:
    world = make_world(forbid_force=True)
    first = await world.call("memory_write", content=_CLEAN_BODY, scopes=["tools"])
    assert first["status"] == "committed", first

    text = await world.refused(
        "memory_write", content=_CLEAN_BODY, scopes=["tools"], force=True
    )
    assert "force=True" in text
    assert "[policy] forbid_force (set in the user's config)" in text
    assert "memory_update" in text
    assert len(world.store.load_all()) == 1

    duplicate = await world.call("memory_write", content=_CLEAN_BODY, scopes=["tools"])
    assert duplicate["status"] == "duplicate", duplicate


async def test_forbid_force_leaves_memory_update_alone(make_world: Any) -> None:
    """memory_update has no `force`, so the switch never reaches it."""
    world = make_world(forbid_force=True)
    written = await world.call("memory_write", content=_CLEAN_BODY, scopes=["tools"])
    updated = await world.call(
        "memory_update", id=written["id"], scopes=["tools", "reference"]
    )
    assert updated["status"] == "committed", updated


# ---------------------------------------------------------------------------
# Capture writes are unchanged
# ---------------------------------------------------------------------------


_WORKTREE = "/work/alpha"
_STOP_SESSION = "0b9e6f6a-7d0c-4a58-9b8e-stopsession1"


async def test_a_capture_write_is_unchanged_by_any_policy(
    make_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capture path clears `force` and every acknowledge flag before
    the policy is checked: its flags were never in force, so a capture
    that passes them commits as it would without a policy."""
    monkeypatch.setattr(
        _handlers,
        "capture_origin",
        lambda *a, **k: Origin(
            cwd=_WORKTREE,
            repo="https://example.test/alpha.git",
            branch="main",
            worktree_root=_WORKTREE,
        ),
    )
    monkeypatch.setenv(identity.ENV_HARNESS_SESSION, _STOP_SESSION)
    world = make_world(forbid_force=True, forbid_acknowledge=list(ACKNOWLEDGE_NAMES))
    batch = capture.open_batch(
        world.store,
        session_id=_STOP_SESSION,
        transcript_bytes=100_000,
        offset=0,
        worktree_root=_WORKTREE,
    )
    result = await world.call(
        "memory_write",
        content="The bettermemory daemon binds 127.0.0.1 only and listens on port 7397.",
        scopes=["tools"],
        source=f"capture:{batch.token}",
        force=True,
        acknowledge_credential=True,
    )
    assert result["status"] == "committed", result


# ---------------------------------------------------------------------------
# The refusal's type and the names
# ---------------------------------------------------------------------------


def test_the_refusal_is_a_value_error_carrying_its_parts() -> None:
    config = _config(
        Path("unused"),
        forbid_acknowledge=["transient"],
        managed=frozenset({"policy.forbid_acknowledge"}),
    )
    with pytest.raises(PolicyRefusal) as excinfo:
        check_overrides(
            config,
            "memory_update",
            acknowledge={"transient": True, "credential": True},
        )
    refusal = excinfo.value
    assert isinstance(refusal, ValueError)
    assert refusal.tool == "memory_update"
    assert refusal.flags == ("acknowledge_transient",)
    assert refusal.keys == ("policy.forbid_acknowledge",)
    assert refusal.managed == frozenset({"policy.forbid_acknowledge"})
    assert "that flag" in str(refusal)


async def test_the_names_are_the_tools_acknowledge_parameters(
    tmp_path: Path,
) -> None:
    """`ACKNOWLEDGE_NAMES` is spelled in config.py so the loader can
    validate it without importing a handler; this pins it to the two
    tools' served parameters, so a new acknowledge flag cannot ship
    without a policy name, nor a name outlive its flag."""
    server = build_server(
        config=_config(tmp_path),
        store=Store(tmp_path),
        state=SessionState(),
    )
    tools = {t.name: t for t in await server.list_tools()}
    served: set[str] = set()
    for name in ("memory_write", "memory_update"):
        props = input_schema(tools[name])["properties"]
        served |= {
            p.removeprefix("acknowledge_")
            for p in props
            if p.startswith("acknowledge_")
        }
    assert served == set(ACKNOWLEDGE_NAMES)
    assert "force" in input_schema(tools["memory_write"])["properties"]
    assert "force" not in input_schema(tools["memory_update"])["properties"]


# ---------------------------------------------------------------------------
# Loading [policy]
# ---------------------------------------------------------------------------


def _load(tmp_path: Path, text: str) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_policy_keys_load_as_written(tmp_path: Path) -> None:
    loaded = _load(
        tmp_path,
        '[policy]\nforbid_force = "yes"\n'
        'forbid_acknowledge = ["credential", "truncation"]\n',
    )
    assert loaded.policy.forbid_force is True
    assert loaded.policy.forbid_acknowledge == ["credential", "truncation"]


def test_an_unknown_policy_name_is_a_located_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="forbid_acknowledge") as excinfo:
        _load(tmp_path, '[policy]\nforbid_acknowledge = ["secrets"]\n')
    message = str(excinfo.value)
    assert str(tmp_path / "config.toml") in message
    assert "'secrets'" in message
    assert "credential, transient, scope_mismatch, user_claim, truncation" in message


def test_a_prefixed_name_is_refused_with_the_bare_one(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='write "credential"'):
        _load(tmp_path, '[policy]\nforbid_acknowledge = ["acknowledge_credential"]\n')


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ('[policy]\nforbid_acknowledge = "credential"\n', "brackets"),
        ("[policy]\nforbid_acknowledge = [1]\n", "a string (in a list)"),
        ('[policy]\nforbid_force = "ture"\n', "true or false"),
        ("[policy]\nforbid_force = 1\n", "true or false"),
        ("policy = true\n", "[policy] = True must be a table"),
    ],
)
def test_malformed_policy_values_fail_the_load(
    tmp_path: Path, text: str, fragment: str
) -> None:
    with pytest.raises(ValueError) as excinfo:
        _load(tmp_path, text)
    assert fragment in str(excinfo.value), str(excinfo.value)


def test_the_shipped_config_documents_the_policy_off(tmp_path: Path) -> None:
    """DEFAULT_CONFIG carries the section with both switches commented
    out, so a fresh install forbids nothing and the file names them."""
    from bettermemory.config import DEFAULT_CONFIG

    loaded = _load(tmp_path, DEFAULT_CONFIG)
    assert loaded.policy == PolicyConfig()
    block = DEFAULT_CONFIG.split("[policy]")[1].split("[daemon]")[0]
    assert "# forbid_force" in block
    assert "# forbid_acknowledge" in block
    for name in ACKNOWLEDGE_NAMES:
        assert name in block, name
