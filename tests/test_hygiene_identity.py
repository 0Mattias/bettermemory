"""Identity hygiene (9.0.0rc12).

- A declared identity loses its Unicode format characters (bidi controls,
  zero-width characters), stripped rather than refused; a declared
  workspace path and the attested principal keep theirs.
- The CLI's `os:<user>` principal comes from the password database, not
  from $USER.
"""

from __future__ import annotations

import getpass
import os
import sys
from typing import Any

import pytest

from bettermemory import identity
from bettermemory.cli import project_cmd
from bettermemory.identity import (
    ENV_CLIENT,
    ENV_MODEL,
    ENV_WORKSPACE,
    HEADER_CLIENT,
    HEADER_MODEL,
    HEADER_SESSION,
    HEADER_WORKSPACE,
)

from ._mcp import fake_ctx


# ---------------------------------------------------------------------------
# A declared identity loses its format characters
# ---------------------------------------------------------------------------


@pytest.fixture
def undeclared(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (ENV_CLIENT, ENV_MODEL, ENV_WORKSPACE):
        monkeypatch.delenv(key, raising=False)
    identity._CURRENT.set(None)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("evil\u202egpt", "evilgpt"),  # right-to-left override
        ("claude\u200b-code", "claude-code"),  # zero-width space
        ("\ufeffhermes", "hermes"),  # byte-order mark
        ("a\u2066b\u2069c", "abc"),  # isolates
        ("op\u00adus", "opus"),  # soft hyphen
        ("tag\U000e0041ged", "tagged"),  # tag character
        (" \u200b\u200d ", None),  # nothing but format characters and spaces
        ("plain-name", "plain-name"),
    ],
)
def test_clean_strips_format_characters(raw: str, clean: str | None) -> None:
    assert identity._clean(raw) == clean


def test_a_value_padded_to_the_cap_with_format_characters_is_kept() -> None:
    """Stripped first, then measured: the visible name is what is bounded."""
    raw = "c" + "\u200b" * identity.MAX_DECLARED_LEN
    assert identity._clean(raw) == "c"


def test_declared_identity_resolves_without_format_characters(undeclared: None) -> None:
    caller = identity.resolve(
        fake_ctx(
            headers={
                HEADER_CLIENT: "claude\u202e-code",
                HEADER_MODEL: "opus\u200b",
                HEADER_SESSION: "sess\u200d-1",
            }
        ).request_context,
        environ={},
    )
    assert caller.actor.client == "claude-code"
    assert caller.actor.model == "opus"
    assert caller.actor.session == "sess-1"
    from_env = identity.resolve(
        None, environ={ENV_CLIENT: "\u202ahermes\u202c", ENV_MODEL: "m\u2060x"}
    )
    assert (from_env.actor.client, from_env.actor.model) == ("hermes", "mx")


def test_the_stop_hook_actor_is_cleaned_the_same_way(undeclared: None) -> None:
    caller = identity.bind_transcript(session_id="s\u200b1", model="opus\u202e")
    assert (caller.actor.session, caller.actor.model) == ("s1", "opus")
    identity._CURRENT.set(None)


def test_a_declared_workspace_path_keeps_its_code_points(undeclared: None) -> None:
    """A path names a directory: a zero-width joiner is part of an emoji
    name, and stripping it would point the record at another folder."""
    path = "/work/team-\U0001f469\u200d\U0001f4bb/app"
    over_http = identity.resolve(
        fake_ctx(headers={HEADER_WORKSPACE: path}).request_context, environ={}
    )
    assert over_http.workspace.path == path
    from_env = identity.resolve(None, environ={ENV_WORKSPACE: path})
    assert from_env.workspace.path == path


def test_the_attested_principal_is_never_folded(undeclared: None) -> None:
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken, principal_components
    from mcp.server.request_state import compact_json

    token = AccessToken(
        token="opaque",
        client_id="oauth-client",
        scopes=[],
        subject="user\u200b42",
        claims={"iss": "https://issuer.test"},
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        caller = identity.resolve(fake_ctx().request_context, environ={})
    finally:
        auth_context_var.reset(reset)
    assert caller.actor.principal == compact_json(principal_components(token))
    assert identity._clean("user\u200b42", strip_format=False) == "user\u200b42"


# ---------------------------------------------------------------------------
# The CLI's OS principal comes from the password database
# ---------------------------------------------------------------------------


def test_the_os_principal_ignores_user_and_logname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX password database")
    if not hasattr(os, "getuid"):  # pragma: no cover - non-unix
        pytest.skip("POSIX uids")
    import pwd

    for var in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.setenv(var, "spoofed-principal")
    assert getpass.getuser() == "spoofed-principal", "what getpass would have kept"
    real = pwd.getpwuid(os.getuid()).pw_name
    assert project_cmd.principal() == f"os:{real}"


def test_a_uid_with_no_database_entry_logs_the_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX password database")
    if not hasattr(os, "getuid"):  # pragma: no cover - non-unix
        pytest.skip("POSIX uids")
    import pwd

    def missing(uid: int) -> Any:
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", missing)
    monkeypatch.setenv("USER", "spoofed-principal")
    assert project_cmd.principal() == f"os:uid:{os.getuid()}"


def test_without_a_password_database_getpass_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(getpass, "getuser", lambda: "winuser")
    assert project_cmd.principal() == "os:winuser"

    def unreadable() -> str:
        raise OSError("no user")

    monkeypatch.setattr(getpass, "getuser", unreadable)
    assert project_cmd.principal() == "os:unknown"
