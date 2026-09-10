"""Who is calling, and from where — resolved once per request.

Until 7.10.0 both answers were inferred from process geometry and the
inference failed silently. A memory carried `origin{cwd, repo, branch,
worktree_root}` and no actor at all; the workspace was the server's own
`Path.cwd()`; the per-client `SessionRegistry` keyed on a `_meta.client_id`
that no known client sends, so every request collapsed into one shared
state even over HTTP. One long-lived gateway process serving many chat
platforms from `$HOME` (the Hermes shape) breaks every one of those
assumptions at once: nothing on the wire said which client, model or
person wrote a memory, and every write landed anchored to wherever the
gateway happened to start.

This module resolves ONE per-request `Caller` and publishes it through a
`ContextVar` so the three consumers read it without threading a new
argument through twenty handler sites:

* `origin.capture()` reads `workspace_declaration()` to decide WHICH
  directory to describe, and stamps the channel on `Origin.source`.
* `events.Recorder.record` stamps `current_actor()` on every event.
* `session.SessionRegistry` keys per-client state on `registry_key()`.

Two blocks, never merged:

* `Actor{client, client_version, model, principal, session, sources}` —
  the WHO. `principal` is ATTESTED: it comes only from the SDK's
  `authenticated_principal`, the (client, issuer, subject) triple of a
  verified OAuth token, and is `None` on every unauthenticated transport.
  Everything else is DECLARED and forgeable — a header is client input, an
  environment variable is operator config, `clientInfo` is what the
  client software says about itself. The two kinds stay in separate
  fields forever: this product is a trust layer, and folding a declared
  value into an attested field would launder a claim into evidence.
* `Workspace{path, source}` — the WHERE. `origin.capture()` turns the
  path into git facts; `source` says which channel supplied it.

`source` is the whole point. Every resolution records which channel
answered, in precedence order — attested principal, HTTP header,
environment variable, MCP roots, process cwd — and the cwd is kept as a
LABELED fallback, never a silent default. A reader can tell "this memory
says bettermemory because the client declared it" from "…because the
server happened to be started there".

Identity here is EVIDENCE — for attribution, filtering, per-model
telemetry and targeted rollback — and never a permission boundary. No
RBAC, no per-agent auth, no tenant isolation: `memory_show(id)` stays
unrestricted exactly as `origin.py` documents for auto-scope.

Leaf module: imports nothing else from this package and reaches for the
MCP SDK lazily, so `session.py` and `origin.py` can depend on it without
a cycle and the module stays importable where the SDK is absent.
"""

from __future__ import annotations

import logging
import os
import weakref
from collections.abc import Mapping
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, Field

log = logging.getLogger("bettermemory.identity")


# ---------------------------------------------------------------------------
# Channels — the `source` vocabulary
# ---------------------------------------------------------------------------

#: An attested OAuth principal, read through the SDK's
#: `mcp.server.request_state.authenticated_principal`. Actor only.
SOURCE_PRINCIPAL = "principal"
#: An `x-bettermemory-*` request header. HTTP transports only.
SOURCE_HEADER = "header"
#: A `BETTERMEMORY_*` environment variable on the server process — the
#: only out-of-band channel a stdio server has; `bettermemory init` writes
#: `BETTERMEMORY_CLIENT` into each client's config block.
SOURCE_ENV = "env"
#: The `clientInfo` block of the MCP `initialize` handshake. Optional under
#: the 2026-07-28 protocol, so its absence is normal, not an error.
SOURCE_CLIENT_INFO = "client-info"
#: The transport's own session id (`mcp-session-id` under streamable
#: HTTP). `None` on stdio and stateless HTTP.
SOURCE_TRANSPORT = "transport"
#: The first `file://` root the client offered on `roots/list`. Workspace
#: only; primed once per connection by `middleware`.
SOURCE_ROOTS = "roots"
#: The server process's working directory — the labeled fallback.
SOURCE_PROCESS_CWD = "process-cwd"
#: The Claude Code Stop hook, which reads the transcript rather than the
#: wire (`hook.py`); the session it names is the transcript id.
SOURCE_TRANSCRIPT = "transcript"

WORKSPACE_SOURCES: tuple[str, ...] = (
    SOURCE_HEADER,
    SOURCE_ENV,
    SOURCE_ROOTS,
    SOURCE_PROCESS_CWD,
)

HEADER_CLIENT = "x-bettermemory-client"
HEADER_CLIENT_VERSION = "x-bettermemory-client-version"
HEADER_MODEL = "x-bettermemory-model"
HEADER_WORKSPACE = "x-bettermemory-workspace"
#: The streamable-HTTP transport's session header, per the MCP spec.
HEADER_MCP_SESSION = "mcp-session-id"

ENV_CLIENT = "BETTERMEMORY_CLIENT"
ENV_CLIENT_VERSION = "BETTERMEMORY_CLIENT_VERSION"
ENV_MODEL = "BETTERMEMORY_MODEL"
ENV_WORKSPACE = "BETTERMEMORY_WORKSPACE"

#: Declared values are client input and land in YAML frontmatter, which
#: has a hard byte cap. Anything longer than this is not an identity.
MAX_DECLARED_LEN = 256

#: How long `middleware` waits for a client to answer `roots/list` before
#: recording "no roots" for the connection. One round-trip per connection,
#: never per request.
ROOTS_TIMEOUT_SECONDS = 2.0


# ---------------------------------------------------------------------------
# The two blocks
# ---------------------------------------------------------------------------


class Actor(BaseModel):
    """The WHO of a request.

    `sources` maps each DECLARED field that is set to the channel that
    supplied it (`header`, `env`, `client-info`, `transport`,
    `transcript`). `principal` never appears there: it has exactly one
    channel and it is attested, so listing it would blur the one line
    this record exists to draw.
    """

    client: str | None = None
    client_version: str | None = None
    model: str | None = None
    principal: str | None = None
    session: str | None = None
    sources: dict[str, str] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        return (
            self.client is None
            and self.client_version is None
            and self.model is None
            and self.principal is None
            and self.session is None
        )

    def to_record(self) -> dict[str, Any]:
        """The additive, conditional shape memory frontmatter and event
        payloads carry: set fields only, `sources` only when non-empty.
        An actor with nothing set serialises to `{}`, and the writers drop
        an empty block entirely — a non-declaring client's memory is
        byte-identical to the 7.9.0 format."""
        out = self.model_dump(mode="json", exclude_none=True)
        if not out.get("sources"):
            out.pop("sources", None)
        return out


def actor_matches(
    actor: Actor | None, *, client: str | None, model: str | None
) -> bool:
    """Does a record written by `actor` survive a client/model filter?

    The single definition of the actor-selection rule, read by
    `search.candidate_admitted` (and so by the ranked set and the BM25
    corpus-IDF denominator alike) and by `memory_list`. It lives here,
    beside the type it reads, so those surfaces cannot drift apart.

    Two properties the callers depend on:

    * **Exact, case-sensitive, no normalisation.** A declared value has
      already been through `_clean`, so it is a bounded exact string,
      and equality here is the same set `index.query`'s SQL `WHERE`
      selects. Folding either side would break that: `COLLATE NOCASE`
      folds ASCII only and Python's `.casefold()` does not agree with it
      beyond ASCII, so the SQL prefilter and this predicate would
      silently disagree on exactly the non-ASCII client names a filter
      would be introduced to fold.
    * **No actor matches no filter.** `None` here is a record whose
      writer declared nothing — every record written before 7.10.0, and
      any written since by a client that names itself in no channel. It
      is not evidence of some OTHER writer, so it is excluded rather
      than passed. This is where the rule differs from
      `origin.should_include_for_caller`, which passes an unlabelled
      memory because that one is an admission rule and an origin-less
      memory is global. Selection and admission are not the same
      question, and the two must not be "harmonised".
    """
    if client is None and model is None:
        return True
    if actor is None:
        return False
    if client is not None and actor.client != client:
        return False
    if model is not None and actor.model != model:
        return False
    return True


class Workspace(BaseModel):
    """The WHERE of a request, before git is asked about it.

    `path` is the directory the declaration named; `None` only when the
    process cwd itself could not be read. `source` is one of
    `WORKSPACE_SOURCES` and is never `None`: an undeclared workspace is
    the process cwd, and it says so.
    """

    path: str | None = None
    source: str = SOURCE_PROCESS_CWD

    @property
    def declared(self) -> bool:
        """True when a channel other than the process cwd answered."""
        return self.source != SOURCE_PROCESS_CWD and self.path is not None


class Caller(BaseModel):
    """One request's resolved identity: actor and workspace, plus the
    request id the resolution was made for (None outside a request)."""

    actor: Actor = Field(default_factory=Actor)
    workspace: Workspace = Field(default_factory=Workspace)
    request_id: str | None = None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _clean(value: object) -> str | None:
    """A declared value, or None when it is not a usable identity: not a
    string, blank, control characters, or longer than `MAX_DECLARED_LEN`.
    Silently dropping a bad value is the right failure here — the fields
    are evidence, and a mangled header is no evidence of anything."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_DECLARED_LEN:
        return None
    if any(ch.isspace() and ch != " " for ch in text) or any(
        ord(ch) < 32 or ord(ch) == 127 for ch in text
    ):
        return None
    return text


def _headers_of(request_context: Any) -> dict[str, str]:
    """The request's headers as a lower-cased plain dict, `{}` when the
    transport has none. Read the same way the SDK's `Context.headers`
    reads them — `request_context.request.headers` — so a forged
    stand-in with that path works and stdio (no `request`) yields `{}`."""
    request = getattr(request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return {}
    try:
        items = headers.items()
    except AttributeError:
        return {}
    out: dict[str, str] = {}
    for key, value in items:
        if isinstance(key, str) and isinstance(value, str):
            out[key.lower()] = value
    return out


def _client_info_of(request_context: Any) -> tuple[str | None, str | None]:
    """`(name, version)` from the `initialize` handshake's `clientInfo`,
    `(None, None)` when the client sent none — optional under the
    2026-07-28 protocol, so the absence is ordinary."""
    session = getattr(request_context, "session", None)
    params = getattr(session, "client_params", None)
    info = getattr(params, "client_info", None)
    if info is None:
        return None, None
    return _clean(getattr(info, "name", None)), _clean(getattr(info, "version", None))


def _principal_of(request_context: Any) -> str | None:
    """The attested principal, or None. Read ONLY through the SDK's
    binding — never from a header, never from `clientInfo` — so a
    declared identity cannot reach this field by any path."""
    if request_context is None:
        return None
    try:
        from mcp.server.request_state import authenticated_principal
    except ImportError:  # pragma: no cover — the SDK is a runtime dependency
        return None
    try:
        return _clean(authenticated_principal(request_context))
    except Exception as exc:  # noqa: BLE001 — a broken auth context is "not attested"
        log.debug("authenticated_principal raised %r; treating as unauthenticated", exc)
        return None


def _request_id_of(request_context: Any) -> str | None:
    rid = getattr(request_context, "request_id", None)
    return str(rid) if rid is not None else None


def _first_file_root(roots: Any) -> str | None:
    """The filesystem path of the first `file://` root, None otherwise."""
    for root in roots or ():
        uri = str(getattr(root, "uri", "") or "")
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            continue
        path = unquote(parsed.path)
        # `file:///C:/work` parses to `/C:/work`; strip the leading slash
        # that a drive-lettered path does not want.
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        if parsed.netloc and parsed.netloc != "localhost":
            # A UNC host: `file://server/share/dir`.
            path = f"//{parsed.netloc}{path}"
        if path:
            return path
    return None


# Roots primed per connection by `middleware`, keyed weakly by the SDK
# session object so a closed connection takes its entry with it. `None`
# means the client was asked (or could not be asked) and offered no
# `file://` root; absence means nobody has asked yet.
_ROOTS: "weakref.WeakKeyDictionary[Any, str | None]" = weakref.WeakKeyDictionary()


def _roots_of(request_context: Any) -> str | None:
    session = getattr(request_context, "session", None)
    if session is None:
        return None
    try:
        return _ROOTS.get(session)
    except TypeError:
        # A stand-in session that is not weak-referenceable was never
        # primed either.
        return None


def resolve(
    request_context: Any | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Caller:
    """Resolve the caller of one request.

    `request_context` is the SDK's `ServerRequestContext` (what a
    middleware receives, and what a handler's `Context.request_context`
    returns), a duck-typed stand-in carrying the same attribute paths,
    or None for an in-process call outside any request — where only the
    environment and the process cwd can answer.

    Pure over its inputs apart from the roots cache and `os.environ`
    (overridable through `environ` for tests). Never raises: a channel
    that cannot be read is a channel that did not answer.
    """
    env = os.environ if environ is None else environ
    headers = _headers_of(request_context)
    sources: dict[str, str] = {}

    def declared(field: str, header: str, env_key: str) -> str | None:
        value = _clean(headers.get(header))
        if value is not None:
            sources[field] = SOURCE_HEADER
            return value
        value = _clean(env.get(env_key))
        if value is not None:
            sources[field] = SOURCE_ENV
            return value
        return None

    client = declared("client", HEADER_CLIENT, ENV_CLIENT)
    client_version = declared(
        "client_version", HEADER_CLIENT_VERSION, ENV_CLIENT_VERSION
    )
    if client is None or client_version is None:
        info_name, info_version = _client_info_of(request_context)
        if client is None and info_name is not None:
            client, sources["client"] = info_name, SOURCE_CLIENT_INFO
        if client_version is None and info_version is not None:
            client_version, sources["client_version"] = info_version, SOURCE_CLIENT_INFO
    model = declared("model", HEADER_MODEL, ENV_MODEL)

    session = _clean(headers.get(HEADER_MCP_SESSION))
    if session is not None:
        sources["session"] = SOURCE_TRANSPORT

    actor = Actor(
        client=client,
        client_version=client_version,
        model=model,
        principal=_principal_of(request_context),
        session=session,
        sources=sources,
    )

    workspace = Workspace()
    declared_path = _clean(headers.get(HEADER_WORKSPACE))
    if declared_path is not None:
        workspace = Workspace(path=declared_path, source=SOURCE_HEADER)
    else:
        declared_path = _clean(env.get(ENV_WORKSPACE))
        if declared_path is not None:
            workspace = Workspace(path=declared_path, source=SOURCE_ENV)
        else:
            root = _roots_of(request_context)
            if root is not None:
                workspace = Workspace(path=root, source=SOURCE_ROOTS)
            else:
                try:
                    workspace = Workspace(
                        path=str(Path.cwd()), source=SOURCE_PROCESS_CWD
                    )
                except (FileNotFoundError, OSError):
                    workspace = Workspace(path=None, source=SOURCE_PROCESS_CWD)

    return Caller(
        actor=actor, workspace=workspace, request_id=_request_id_of(request_context)
    )


# ---------------------------------------------------------------------------
# The published caller — what the three consumers read
# ---------------------------------------------------------------------------

_CURRENT: ContextVar[Caller | None] = ContextVar("bettermemory_caller", default=None)


def current() -> Caller | None:
    """The caller published for this task, or None outside any request."""
    return _CURRENT.get()


def publish(caller: Caller) -> Token[Caller | None]:
    """Publish `caller` for the current task; the token resets it."""
    return _CURRENT.set(caller)


def reset(token: Token[Caller | None]) -> None:
    _CURRENT.reset(token)


def bind(ctx: Any | None) -> Caller:
    """Resolve from a handler `Context` (or None) and publish the result.

    The handler-entry chokepoint: `SessionSource.for_request(ctx)` calls
    this before anything else in a tool call runs, so `origin.capture()`
    and `Recorder.record` further down the same call see the request's
    declarations. The SDK's `Context.request_context` raises outside a
    request and a forged stand-in may lack it; either way the request
    contributes nothing and the environment still can.
    """
    request_context: Any | None = None
    if ctx is not None:
        try:
            request_context = ctx.request_context
        except (AttributeError, ValueError):
            request_context = None
    caller = resolve(request_context)
    publish(caller)
    return caller


def bind_transcript(
    *,
    session_id: str | None,
    model: str | None,
    client: str | None = "claude-code",
) -> Caller:
    """Publish the Stop hook's caller: the hook reads a transcript, not
    the wire, so its session is the transcript id and its client the
    one whose hook protocol it implements. Every value is declared;
    `principal` stays None."""
    sources: dict[str, str] = {}
    fields: dict[str, str | None] = {
        "client": _clean(client),
        "model": _clean(model),
        "session": _clean(session_id),
    }
    for name, value in fields.items():
        if value is not None:
            sources[name] = SOURCE_TRANSCRIPT
    actor = Actor(
        client=fields["client"],
        model=fields["model"],
        session=fields["session"],
        sources=sources,
    )
    workspace = resolve(None).workspace
    caller = Caller(actor=actor, workspace=workspace)
    publish(caller)
    return caller


def current_actor() -> Actor | None:
    """The published actor when it carries anything, else None — the
    shape the frontmatter and event writers key their conditional
    emission on."""
    caller = _CURRENT.get()
    if caller is None or caller.actor.is_empty():
        return None
    return caller.actor


def workspace_declaration() -> tuple[Path, str] | None:
    """`(path, source)` when a channel other than the process cwd named
    the workspace, else None — the question `origin.capture()` asks.

    Outside any request the environment alone is consulted, so a CLI
    process started with `BETTERMEMORY_WORKSPACE` set still records what
    it was told rather than where it stands.
    """
    caller = _CURRENT.get()
    workspace = caller.workspace if caller is not None else resolve(None).workspace
    if not workspace.declared or workspace.path is None:
        return None
    return Path(workspace.path).expanduser(), workspace.source


def registry_key(actor: Actor) -> str | None:
    """The per-client session bucket an actor belongs to, or None for the
    shared default.

    Keys on what can DIFFER between two requests reaching one process:
    the attested principal, the transport session, and header-declared
    client/model. Not on `clientInfo` or the environment — those are
    per-process, so keying on them would move every stdio client out of
    the default bucket for no isolation gained, and the pending-write
    sidecar files rows under the bucket name.
    """
    parts: list[str] = []
    if actor.principal is not None:
        parts.append(f"principal={actor.principal}")
    if actor.session is not None and actor.sources.get("session") == SOURCE_TRANSPORT:
        parts.append(f"session={actor.session}")
    if actor.client is not None and actor.sources.get("client") == SOURCE_HEADER:
        parts.append(f"client={actor.client}")
    if actor.model is not None and actor.sources.get("model") == SOURCE_HEADER:
        parts.append(f"model={actor.model}")
    return "|".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Wire-path middleware — the one async channel
# ---------------------------------------------------------------------------


async def prime_roots(request_context: Any) -> str | None:
    """Ask the client for its roots once per connection and cache the
    first `file://` path (or None) under the session object.

    Only a client that declared the `roots` capability is asked; the
    request is bounded by `ROOTS_TIMEOUT_SECONDS`; any failure records
    "no roots" rather than raising into the tool call. `roots/list` is
    deprecated by the 2026-07-28 protocol revision, so the SDK's
    deprecation warning is expected and silenced here — a client that
    still offers roots is telling us where it works, and that is worth
    one round-trip.
    """
    session = getattr(request_context, "session", None)
    if session is None:
        return None
    try:
        if session in _ROOTS:
            return _ROOTS[session]
    except TypeError:
        return None

    path: str | None = None
    try:
        import warnings

        import anyio
        from mcp import types

        if session.check_client_capability(
            types.ClientCapabilities(roots=types.RootsCapability())
        ):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with anyio.fail_after(ROOTS_TIMEOUT_SECONDS):
                    result = await session.list_roots()
            path = _first_file_root(getattr(result, "roots", None))
    except Exception as exc:  # noqa: BLE001 — a client that cannot answer offered no roots
        log.debug("roots/list did not answer: %r", exc)
        path = None
    try:
        _ROOTS[session] = path
    except TypeError:
        pass
    return path


async def middleware(ctx: Any, call_next: Any) -> Any:
    """`ServerMiddleware`: prime the roots channel before a tool call and
    publish the resolved caller for the request's task.

    Registered on the server in `builder.build_server`. Runs on the wire
    path only — in-process callers (tests, the CLI) reach `bind` through
    `for_request` instead, which is why the handler entry resolves again:
    the two agree on everything but roots, which only this path can ask
    for. The publication is reset when the request ends so a task reused
    by the runtime starts clean.
    """
    if getattr(ctx, "method", None) == "tools/call":
        await prime_roots(ctx)
    token = publish(resolve(ctx))
    try:
        return await call_next(ctx)
    finally:
        reset(token)


__all__ = [
    "Actor",
    "Caller",
    "Workspace",
    "WORKSPACE_SOURCES",
    "SOURCE_PRINCIPAL",
    "SOURCE_HEADER",
    "SOURCE_ENV",
    "SOURCE_CLIENT_INFO",
    "SOURCE_TRANSPORT",
    "SOURCE_ROOTS",
    "SOURCE_PROCESS_CWD",
    "SOURCE_TRANSCRIPT",
    "HEADER_CLIENT",
    "HEADER_CLIENT_VERSION",
    "HEADER_MODEL",
    "HEADER_WORKSPACE",
    "HEADER_MCP_SESSION",
    "ENV_CLIENT",
    "ENV_CLIENT_VERSION",
    "ENV_MODEL",
    "ENV_WORKSPACE",
    "MAX_DECLARED_LEN",
    "ROOTS_TIMEOUT_SECONDS",
    "resolve",
    "current",
    "publish",
    "reset",
    "bind",
    "bind_transcript",
    "current_actor",
    "workspace_declaration",
    "registry_key",
    "prime_roots",
    "middleware",
]
