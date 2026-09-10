"""The identity resolver (`bettermemory.identity`): the four gates the
7.10.0 declaration set, and the channel and key rules they rest on.

- G1 — silence is unchanged: a client that declares nothing produces
  frontmatter byte-identical to 7.9.0, proven on a real file against an
  expected document rebuilt from the 7.9.0 key set alone.
- G2 — the registry actually separates: two clients declaring different
  identities against one server process get two `SessionState`s; a
  per-process channel (env, clientInfo) does NOT split.
- G3 — the source is honest: each channel resolves and records its own
  `source`, the cwd fallback is labeled, and the Hermes shape — a server
  started in `$HOME` with an env-declared workspace — records the declared
  workspace and says so.
- G4 — attestation cannot be forged: a declared client/model never
  populates `principal`; an unauthenticated transport yields None.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _frontmatter, identity
from bettermemory._response import ResponseBuilder
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.identity import (
    ENV_CLIENT,
    ENV_CLIENT_VERSION,
    ENV_MODEL,
    ENV_WORKSPACE,
    HEADER_CLIENT,
    HEADER_MCP_SESSION,
    HEADER_MODEL,
    HEADER_WORKSPACE,
    MAX_DECLARED_LEN,
    SOURCE_CLIENT_INFO,
    SOURCE_ENV,
    SOURCE_HEADER,
    SOURCE_PROCESS_CWD,
    SOURCE_ROOTS,
    SOURCE_TRANSCRIPT,
    SOURCE_TRANSPORT,
    Actor,
    Caller,
)
from bettermemory.init import patch_client_config, server_snippet
from bettermemory.models import SCHEMA_VERSION
from bettermemory.origin import Origin, capture
from bettermemory.server import build_server
from bettermemory.session import (
    _DEFAULT_CLIENT_KEY,
    SessionRegistry,
    SessionState,
    _decode_payload,
    _encode_payload,
)
from bettermemory.store import Store

from ._mcp import fake_ctx

_GIT_AVAILABLE = shutil.which("git") is not None
_ENV_KEYS = (ENV_CLIENT, ENV_CLIENT_VERSION, ENV_MODEL, ENV_WORKSPACE)


@pytest.fixture(autouse=True)
def _no_ambient_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts undeclared: no `BETTERMEMORY_*` identity variable
    in the environment and no caller published for this task. The
    resolver reads the live environment on purpose, so a developer's own
    shell must not leak into the assertions below."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    identity._CURRENT.set(None)


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """The unwrapped handler, so a forged `ctx` reaches it — the SDK's
    schema dispatch strips `Context`-typed parameters by design."""
    fn = server._tool_manager.get_tool(name).fn
    return await fn(**kwargs)


def _server(
    root: Path, *, state: Any = None, confirm: bool = False
) -> tuple[Any, Store]:
    cfg = Config(
        storage=StorageConfig(directory=str(root)),
        behavior=BehaviorConfig(require_write_confirmation=confirm),
    )
    store = Store(root)
    return build_server(config=cfg, store=store, state=state), store


def _init_repo(path: Path, *, remote: str) -> None:
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


def _memory_file(store: Store, memory_id: str) -> Path:
    (path,) = [p for p in store.root.glob("*.md") if memory_id in p.read_text()]
    return path


# ---------------------------------------------------------------------------
# G1 — silence is unchanged
# ---------------------------------------------------------------------------


async def test_g1_a_client_that_declares_nothing_writes_the_7_9_0_frontmatter(
    tmp_path: Path,
) -> None:
    """Proven on a real file, not asserted: the bytes the store wrote are
    compared to a document rebuilt from the 7.9.0 key set — nothing else
    is allowed to appear, including `actor` and `origin.source`."""
    server, store = _server(tmp_path / "store")
    committed = await _call(
        server,
        "memory_write",
        content="a durable fact about a project written by a silent client",
        scopes=["projects:silent"],
        ctx=fake_ctx(with_request=False),
    )
    assert committed["status"] == "committed"
    memory = store.load_one(committed["id"])
    assert memory is not None
    assert memory.actor is None
    # Reloaded from disk the origin names no channel: the process cwd is
    # implicit in the file (that is G1) and labeled on the read surface.
    assert memory.origin is not None and memory.origin.source is None

    path = _memory_file(store, memory.id)
    written = path.read_text(encoding="utf-8")

    # The 7.9.0 key set, in the order the 7.9.0 serializer emitted it, and
    # the 7.9.0 origin block: `exclude_none` over exactly four fields.
    origin_7_9_0 = {
        key: value
        for key, value in (
            ("cwd", memory.origin.cwd),
            ("repo", memory.origin.repo),
            ("branch", memory.origin.branch),
            ("worktree_root", memory.origin.worktree_root),
        )
        if value is not None
    }
    expected_meta: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": memory.id,
        "created": memory.created,
        "updated": memory.updated,
        "scopes": list(memory.scopes),
        "confidence": memory.confidence.value,
        "source": memory.source.value,
        "origin": origin_7_9_0,
        "category": "fact",
    }
    expected = _frontmatter.dumps(
        _frontmatter.Post(content=memory.body, metadata=expected_meta)
    )
    assert written == expected
    assert "actor" not in written
    assert "process-cwd" not in written


async def test_g1_contrast_a_declaring_client_adds_the_block_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The additive half of G1: a declared client lands as one extra
    `actor` block and the rest of the file is the 7.9.0 shape."""
    server, store = _server(tmp_path / "store")
    committed = await _call(
        server,
        "memory_write",
        content="a durable fact about a project written by a declaring client",
        scopes=["projects:declared"],
        ctx=fake_ctx(headers={HEADER_CLIENT: "hermes", HEADER_MODEL: "gpt-x"}),
    )
    memory = store.load_one(committed["id"])
    assert memory is not None
    post = _frontmatter.loads(_memory_file(store, memory.id).read_text())
    assert post.metadata["actor"] == {
        "client": "hermes",
        "model": "gpt-x",
        "sources": {"client": SOURCE_HEADER, "model": SOURCE_HEADER},
    }
    assert "source" not in post.metadata["origin"]
    assert committed["actor"]["client"] == "hermes"
    assert committed["actor"]["principal"] is None


# ---------------------------------------------------------------------------
# G2 — the registry actually separates
# ---------------------------------------------------------------------------


def test_g2_two_header_declared_identities_get_two_states() -> None:
    registry = SessionRegistry()
    hermes = registry.for_request(fake_ctx(headers={HEADER_CLIENT: "hermes"}))
    claude = registry.for_request(fake_ctx(headers={HEADER_CLIENT: "claude-code"}))
    assert hermes is not claude
    assert hermes.client_key == "client=hermes"
    assert claude.client_key == "client=claude-code"
    assert registry.for_request(fake_ctx(headers={HEADER_CLIENT: "hermes"})) is hermes


def test_g2_two_transport_sessions_get_two_states() -> None:
    registry = SessionRegistry()
    a = registry.for_request(fake_ctx("sess-a"))
    b = registry.for_request(fake_ctx("sess-b"))
    assert a is not b
    assert {a.client_key, b.client_key} == {"session=sess-a", "session=sess-b"}


def test_g2_per_process_channels_do_not_split_the_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment and the handshake's clientInfo are facts about the
    PROCESS, not about one request among many: keying on them would move
    every stdio client out of the default bucket (and its pending-write
    sidecar rows) for no isolation gained."""
    monkeypatch.setenv(ENV_CLIENT, "hermes")
    registry = SessionRegistry()
    env_state = registry.for_request(fake_ctx(with_request=False))
    info_state = registry.for_request(fake_ctx(client_info=("claude-code", "2.1.0")))
    assert env_state is info_state is registry.for_request(None)
    assert env_state.client_key == _DEFAULT_CLIENT_KEY


async def test_g2_pending_writes_cannot_cross_header_declared_identities(
    tmp_path: Path,
) -> None:
    """The first time this code path runs in production shape: two clients
    that differ only in what they declare, against one server."""
    registry = SessionRegistry()
    server, _store = _server(tmp_path / "store", state=registry, confirm=True)
    hermes = fake_ctx(headers={HEADER_CLIENT: "hermes"})
    claude = fake_ctx(headers={HEADER_CLIENT: "claude-code"})

    pending = await _call(
        server,
        "memory_write",
        content="hermes staged a durable preference about editor tabs",
        scopes=["learning-style"],
        ctx=hermes,
    )
    assert pending["status"] == "pending"

    crossed = await _call(
        server, "memory_write_cancel", pending_id=pending["pending_id"], ctx=claude
    )
    assert crossed["existed"] is False

    disabled = await _call(server, "memory_scope_disable", scope="tools", ctx=hermes)
    assert "tools" in disabled["disabled_scopes"]
    other = await _call(server, "memory_scope_enable", scope="untouched", ctx=claude)
    assert other["disabled_scopes"] == []

    committed = await _call(
        server, "memory_write_confirm", pending_id=pending["pending_id"], ctx=hermes
    )
    assert committed["status"] == "committed"
    assert committed["actor"]["client"] == "hermes"


async def test_g2_a_staged_actor_survives_the_pending_sidecar_and_a_restart(
    tmp_path: Path,
) -> None:
    """The actor rides the staged payload through JSON and back: a server
    restart between `memory_write` and `memory_write_confirm` re-adopts
    the row under the same bucket and the committed record still names
    the writer."""
    root = tmp_path / "store"
    first, _ = _server(root, state=SessionRegistry(), confirm=True)
    ctx = fake_ctx(headers={HEADER_CLIENT: "hermes", HEADER_MODEL: "gpt-x"})
    pending = await _call(
        first,
        "memory_write",
        content="a durable claim staged before the server restarted",
        scopes=["projects:restart"],
        ctx=ctx,
    )
    assert pending["status"] == "pending"

    second, store = _server(root, state=SessionRegistry(), confirm=True)
    committed = await _call(
        second, "memory_write_confirm", pending_id=pending["pending_id"], ctx=ctx
    )
    assert committed["status"] == "committed"
    memory = store.load_one(committed["id"])
    assert memory is not None and memory.actor is not None
    assert memory.actor.client == "hermes"
    assert memory.actor.model == "gpt-x"
    assert memory.actor.sources == {"client": SOURCE_HEADER, "model": SOURCE_HEADER}


def test_payload_codec_round_trips_the_actor() -> None:
    actor = Actor(client="hermes", sources={"client": SOURCE_HEADER})
    encoded = _encode_payload({"content": "x", "actor": actor, "origin": None})
    assert encoded["actor"] == {
        "client": "hermes",
        "client_version": None,
        "model": None,
        "principal": None,
        "session": None,
        "sources": {"client": SOURCE_HEADER},
    }
    decoded = _decode_payload(encoded)
    assert decoded["actor"] == actor
    assert _decode_payload(_encode_payload({"actor": None}))["actor"] is None


# ---------------------------------------------------------------------------
# G3 — the source is honest
# ---------------------------------------------------------------------------


def test_g3_the_undeclared_workspace_is_the_labeled_process_cwd() -> None:
    caller = identity.resolve(None, environ={})
    assert caller.workspace.source == SOURCE_PROCESS_CWD
    assert caller.workspace.path == str(Path.cwd())
    assert caller.workspace.declared is False
    assert caller.actor.is_empty()


def test_g3_each_channel_resolves_with_its_own_source() -> None:
    env = {ENV_WORKSPACE: "/from/env", ENV_CLIENT: "env-client", ENV_MODEL: "env-model"}

    only_env = identity.resolve(
        fake_ctx(with_request=False).request_context, environ=env
    )
    assert only_env.workspace == identity.Workspace(path="/from/env", source=SOURCE_ENV)
    assert only_env.actor.client == "env-client"
    assert only_env.actor.sources == {"client": SOURCE_ENV, "model": SOURCE_ENV}

    header_over_env = identity.resolve(
        fake_ctx(
            "sess-1",
            headers={HEADER_WORKSPACE: "/from/header", HEADER_CLIENT: "hdr-client"},
            client_info=("claude-code", "2.1.0"),
        ).request_context,
        environ=env,
    )
    assert header_over_env.workspace.source == SOURCE_HEADER
    assert header_over_env.workspace.path == "/from/header"
    assert header_over_env.actor.client == "hdr-client"
    assert header_over_env.actor.client_version == "2.1.0"
    assert header_over_env.actor.model == "env-model"
    assert header_over_env.actor.session == "sess-1"
    assert header_over_env.actor.sources == {
        "client": SOURCE_HEADER,
        "client_version": SOURCE_CLIENT_INFO,
        "model": SOURCE_ENV,
        "session": SOURCE_TRANSPORT,
    }

    only_info = identity.resolve(
        fake_ctx(client_info=("cursor", "1.2")).request_context, environ={}
    )
    assert only_info.actor.client == "cursor"
    assert only_info.actor.sources == {
        "client": SOURCE_CLIENT_INFO,
        "client_version": SOURCE_CLIENT_INFO,
    }


class _Roots:
    """A `ServerSession` stand-in for the roots channel: declares (or
    not) the capability and answers `roots/list` with the given roots,
    counting how often it was asked."""

    def __init__(self, *, capable: bool, uris: list[str], delay: float = 0.0) -> None:
        self.capable = capable
        self.uris = uris
        self.delay = delay
        self.calls = 0

    def check_client_capability(self, capability: Any) -> bool:
        return self.capable

    async def list_roots(self) -> Any:
        import anyio

        self.calls += 1
        if self.delay:
            await anyio.sleep(self.delay)
        from mcp import types

        return types.ListRootsResult(roots=[types.Root(uri=uri) for uri in self.uris])


def _ctx_with_session(session: Any) -> Any:
    ctx = fake_ctx()
    ctx.request_context.session = session
    return ctx


async def test_g3_roots_resolve_once_per_connection_and_rank_below_env() -> None:
    session = _Roots(capable=True, uris=["file:///work/proj%20one", "file:///other"])
    ctx = _ctx_with_session(session)

    assert identity.resolve(ctx.request_context, environ={}).workspace.source == (
        SOURCE_PROCESS_CWD
    ), "nothing primed yet: the cwd fallback, labeled"

    assert await identity.prime_roots(ctx.request_context) == "/work/proj one"
    assert await identity.prime_roots(ctx.request_context) == "/work/proj one"
    assert session.calls == 1, "one round-trip per connection"

    primed = identity.resolve(ctx.request_context, environ={})
    assert primed.workspace == identity.Workspace(
        path="/work/proj one", source=SOURCE_ROOTS
    )

    env_wins = identity.resolve(ctx.request_context, environ={ENV_WORKSPACE: "/env"})
    assert env_wins.workspace.source == SOURCE_ENV


async def test_g3_a_client_without_roots_or_too_slow_offers_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incapable = _Roots(capable=False, uris=["file:///never"])
    assert (
        await identity.prime_roots(_ctx_with_session(incapable).request_context) is None
    )
    assert incapable.calls == 0

    monkeypatch.setattr(identity, "ROOTS_TIMEOUT_SECONDS", 0.05)
    slow = _Roots(capable=True, uris=["file:///late"], delay=1.0)
    assert await identity.prime_roots(_ctx_with_session(slow).request_context) is None

    no_file = _Roots(capable=True, uris=["https://example.test/not-a-dir"])
    assert (
        await identity.prime_roots(_ctx_with_session(no_file).request_context) is None
    )


def test_first_file_root_handles_drive_letters_and_unc_hosts() -> None:
    class _Root:
        def __init__(self, uri: str) -> None:
            self.uri = uri

    assert identity._first_file_root([_Root("file:///C:/work/proj")]) == "C:/work/proj"
    assert identity._first_file_root([_Root("file://server/share/dir")]) == (
        "//server/share/dir"
    )
    assert identity._first_file_root([_Root("file://localhost/srv/x")]) == "/srv/x"
    assert identity._first_file_root([_Root("s3://bucket/key")]) is None
    assert identity._first_file_root(None) is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_g3_the_hermes_shape_records_the_declared_workspace_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway started in `$HOME` with `BETTERMEMORY_WORKSPACE` naming the
    project: the memory's origin is the project — its git facts included —
    and the file says the environment named it."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project, remote="https://github.com/example/proj.git")
    monkeypatch.chdir(home)
    monkeypatch.setenv(ENV_WORKSPACE, str(project))

    server, store = _server(tmp_path / "store")
    committed = await _call(
        server,
        "memory_write",
        content="a durable fact recorded by a gateway that lives in home",
        scopes=["projects:proj"],
        ctx=fake_ctx(with_request=False),
    )
    memory = store.load_one(committed["id"])
    assert memory is not None and memory.origin is not None
    assert memory.origin.cwd == str(project.resolve())
    assert memory.origin.repo == "https://github.com/example/proj.git"
    assert memory.origin.worktree_root == str(project.resolve())
    assert memory.origin.source == SOURCE_ENV

    post = _frontmatter.loads(_memory_file(store, memory.id).read_text())
    assert post.metadata["origin"]["source"] == SOURCE_ENV
    assert post.metadata["origin"]["cwd"] == str(project.resolve())

    shown = await _call(server, "memory_show", id=memory.id, ctx=None)
    assert shown["origin"]["source"] == SOURCE_ENV


async def test_g3_the_read_surface_labels_the_implicit_process_cwd(
    tmp_path: Path,
) -> None:
    """On disk the process-cwd source stays implicit (G1); on every read
    surface it is spelled out, and so is a legacy origin that predates
    the field — both were captured from the server's cwd."""
    server, store = _server(tmp_path / "store")
    committed = await _call(
        server,
        "memory_write",
        content="a durable fact whose origin is the process cwd",
        scopes=["projects:cwd"],
        ctx=None,
    )
    shown = await _call(server, "memory_show", id=committed["id"], ctx=None)
    assert shown["origin"]["source"] == SOURCE_PROCESS_CWD
    assert "actor" not in shown
    memory = store.load_one(committed["id"])
    assert memory is not None
    assert (
        "source"
        not in _frontmatter.loads(_memory_file(store, memory.id).read_text()).metadata[
            "origin"
        ]
    )

    legacy = ResponseBuilder(stale_after_days=30).origin_to_dict(
        Origin(cwd="/legacy/dir", repo="https://example.test/r.git")
    )
    assert legacy == {
        "cwd": "/legacy/dir",
        "repo": "https://example.test/r.git",
        "source": SOURCE_PROCESS_CWD,
    }
    assert ResponseBuilder(stale_after_days=30).origin_to_dict(Origin()) is None


def test_g3_capture_labels_the_fallback_and_honours_a_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert capture().source == SOURCE_PROCESS_CWD
    assert capture(cwd=tmp_path).source is None, "an explicit cwd says nothing"
    assert capture(cwd=tmp_path, source="caller").source == "caller"

    declared = tmp_path / "declared"
    declared.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE, str(declared))
    origin = capture()
    assert origin.cwd == str(declared.resolve())
    assert origin.source == SOURCE_ENV
    assert identity.workspace_declaration() == (declared, SOURCE_ENV)

    monkeypatch.delenv(ENV_WORKSPACE)
    assert identity.workspace_declaration() is None
    via_header = tmp_path / "via-header"
    identity.publish(
        Caller(workspace=identity.Workspace(path=str(via_header), source=SOURCE_HEADER))
    )
    assert identity.workspace_declaration() == (via_header, SOURCE_HEADER)


# ---------------------------------------------------------------------------
# G4 — attestation cannot be forged
# ---------------------------------------------------------------------------


def test_g4_a_declared_identity_never_populates_the_principal() -> None:
    forged = fake_ctx(
        "sess",
        headers={
            HEADER_CLIENT: "claude-code",
            HEADER_MODEL: "claude-opus-5",
            "x-bettermemory-principal": '{"client":"me","sub":"root"}',
            "authorization": "Bearer forged",
        },
        client_info=("claude-code", "2.1.0"),
    )
    caller = identity.resolve(forged.request_context, environ={ENV_CLIENT: "hermes"})
    assert caller.actor.principal is None
    assert caller.actor.client == "claude-code"
    assert caller.actor.sources["client"] == SOURCE_HEADER
    assert "principal" not in caller.actor.sources
    assert identity.registry_key(caller.actor) == (
        "session=sess|client=claude-code|model=claude-opus-5"
    )


def test_g4_an_attested_principal_is_read_only_from_the_sdk_binding() -> None:
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken, principal_components
    from mcp.server.request_state import compact_json

    token = AccessToken(
        token="opaque",
        client_id="oauth-client",
        scopes=[],
        subject="user-42",
        claims={"iss": "https://issuer.test"},
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        caller = identity.resolve(
            fake_ctx(headers={HEADER_CLIENT: "hermes"}).request_context, environ={}
        )
    finally:
        auth_context_var.reset(reset)

    assert caller.actor.principal == compact_json(principal_components(token))
    assert caller.actor.client == "hermes", "declared stays declared beside it"
    assert "principal" not in caller.actor.sources
    assert identity.registry_key(caller.actor) == (
        f"principal={caller.actor.principal}|client=hermes"
    )
    # The same request, unauthenticated: nothing attested.
    assert (
        identity.resolve(
            fake_ctx(headers={HEADER_CLIENT: "hermes"}).request_context, environ={}
        ).actor.principal
        is None
    )


def test_registry_key_ignores_per_process_channels_and_the_empty_actor() -> None:
    assert identity.registry_key(Actor()) is None
    assert (
        identity.registry_key(Actor(client="x", sources={"client": SOURCE_ENV})) is None
    )
    assert (
        identity.registry_key(Actor(client="x", sources={"client": SOURCE_CLIENT_INFO}))
        is None
    )
    assert identity.registry_key(
        Actor(session="s", sources={"session": SOURCE_TRANSCRIPT})
    ) is (None)
    assert identity.registry_key(
        Actor(session="s", sources={"session": SOURCE_TRANSPORT})
    ) == ("session=s")


def test_declared_values_are_bounded_client_input() -> None:
    caller = identity.resolve(
        fake_ctx(
            headers={
                HEADER_CLIENT: "  spaced name  ",
                HEADER_MODEL: "line\nbreak",
                HEADER_WORKSPACE: "x" * (MAX_DECLARED_LEN + 1),
                HEADER_MCP_SESSION: "   ",
            }
        ).request_context,
        environ={ENV_CLIENT_VERSION: "tab\tbed"},
    )
    assert caller.actor.client == "spaced name"
    assert caller.actor.model is None
    assert caller.actor.client_version is None
    assert caller.actor.session is None
    assert caller.workspace.source == SOURCE_PROCESS_CWD
    assert caller.actor.sources == {"client": SOURCE_HEADER}


# ---------------------------------------------------------------------------
# The consumers: events, the hook, init, the read surface
# ---------------------------------------------------------------------------


def test_events_carry_the_actor_only_when_something_was_declared(
    tmp_path: Path,
) -> None:
    recorder = Recorder(root=tmp_path, session_id="sess_test")
    recorder.record("probe", n=1)
    identity.publish(
        Caller(
            actor=Actor(
                client="hermes", model="gpt-x", sources={"client": SOURCE_HEADER}
            )
        )
    )
    recorder.record("probe", n=2)
    recorder.record("probe", n=3, actor={"client": "handler-wins"})

    events = [e for e in iter_events(tmp_path) if e["kind"] == "probe"]
    assert "actor" not in events[0]
    assert events[1]["actor"] == {
        "client": "hermes",
        "model": "gpt-x",
        "sources": {"client": SOURCE_HEADER},
    }
    assert events[2]["actor"] == {"client": "handler-wins"}


def test_the_stop_hook_publishes_a_transcript_actor(tmp_path: Path) -> None:
    caller = identity.bind_transcript(
        session_id="transcript-uuid", model="claude-opus-5"
    )
    assert caller.actor.to_record() == {
        "client": "claude-code",
        "model": "claude-opus-5",
        "session": "transcript-uuid",
        "sources": {
            "client": SOURCE_TRANSCRIPT,
            "model": SOURCE_TRANSCRIPT,
            "session": SOURCE_TRANSCRIPT,
        },
    }
    assert caller.actor.principal is None
    assert identity.registry_key(caller.actor) is None, (
        "a transcript id is not a bucket"
    )
    recorder = Recorder(root=tmp_path, session_id="transcript-uuid")
    recorder.record("turn_audited")
    (event,) = list(iter_events(tmp_path))
    assert event["actor"]["session"] == "transcript-uuid"


async def test_the_middleware_publishes_for_the_request_and_resets_after() -> None:
    session = _Roots(capable=True, uris=["file:///from/roots"])
    ctx = fake_ctx("sess-mw", request_id="req-7")
    ctx.request_context.session = session
    ctx.request_context.method = "tools/call"
    seen: dict[str, Any] = {}

    async def call_next(rc: Any) -> dict[str, Any]:
        current = identity.current()
        assert current is not None
        seen["caller"] = current
        return {"ok": True}

    assert await identity.middleware(ctx.request_context, call_next) == {"ok": True}
    assert seen["caller"].request_id == "req-7"
    assert seen["caller"].actor.session == "sess-mw"
    assert seen["caller"].workspace.source == SOURCE_ROOTS
    assert identity.current() is None, "reset when the request ends"

    listing = fake_ctx()
    listing.request_context.session = _Roots(capable=True, uris=["file:///x"])
    listing.request_context.method = "tools/list"
    await identity.middleware(listing.request_context, call_next)
    assert listing.request_context.session.calls == 0, "only a tool call primes roots"


def test_bind_tolerates_a_context_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Bare:
        @property
        def request_context(self) -> Any:
            raise ValueError("Context is not available outside of a request")

    monkeypatch.setenv(ENV_CLIENT, "from-env")
    caller = identity.bind(_Bare())
    assert caller.actor.client == "from-env"
    assert identity.current() is caller
    assert SessionState().for_request(None) is not None
    assert identity.current() is not caller, "for_request re-binds"


def test_init_declares_the_client_in_the_env_block(tmp_path: Path) -> None:
    assert (
        server_snippet(binary="/bin/bettermemory")["mcpServers"]["bettermemory"]["env"]
        == {}
    )
    assert server_snippet(binary="/bin/bettermemory", client="cursor")["mcpServers"][
        "bettermemory"
    ]["env"] == {ENV_CLIENT: "cursor"}

    target = tmp_path / "mcp.json"
    first = patch_client_config(target, binary="/bin/bettermemory", client="cursor")
    assert first["action"] == "added"
    import json

    entry = json.loads(target.read_text())["mcpServers"]["bettermemory"]
    assert entry["env"] == {ENV_CLIENT: "cursor"}
    again = patch_client_config(target, binary="/bin/bettermemory", client="cursor")
    assert again["action"] == "noop"

    # A value the user set wins; declared is declared.
    entry["env"][ENV_CLIENT] = "my-fork"
    target.write_text(json.dumps({"mcpServers": {"bettermemory": entry}}))
    patch_client_config(target, binary="/bin/bettermemory", client="cursor")
    assert json.loads(target.read_text())["mcpServers"]["bettermemory"]["env"] == {
        ENV_CLIENT: "my-fork"
    }


async def test_memory_show_renders_the_full_actor_shape(tmp_path: Path) -> None:
    server, _store = _server(tmp_path / "store")
    committed = await _call(
        server,
        "memory_write",
        content="a durable fact whose writer declared a client through the handshake",
        scopes=["projects:show"],
        ctx=fake_ctx(client_info=("claude-code", "2.1.9")),
    )
    shown = await _call(server, "memory_show", id=committed["id"], ctx=None)
    assert shown["actor"] == {
        "client": "claude-code",
        "client_version": "2.1.9",
        "model": None,
        "principal": None,
        "session": None,
        "sources": {"client": SOURCE_CLIENT_INFO, "client_version": SOURCE_CLIENT_INFO},
    }
