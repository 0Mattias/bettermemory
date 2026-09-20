"""The per-request store seam (Teams Phase 1 / D2).

What this file pins, and what it deliberately does not:

* `Store.load_many` — the batched id -> record path that replaced the
  hand-rolled loop in `_handlers.load_search_candidates`: ONE index
  connection for N ids, candidate skips preserved, index failures
  PROPAGATED rather than swallowed.
* `ToolHandlers.for_request` — the bundle, not the store, with `store`
  and `episode_store` rebound together and the recorder deliberately left
  on the process root.
* The ORDERING INVARIANT: all 27 facade methods resolve the bundle
  before they delegate, which is what makes the 108 `deps.store` reads
  and the 19 reads inside ctx-less helpers follow the right store with
  no edit at any of them.

What it does NOT pin, because the unit does not claim it: that any
request is served from a DIFFERENT store than the process store. The
shipped `DefaultStoreSource` returns one store for every request, so the
suite cannot observe a tenancy difference — the multi-tenant case is
exercised here only through a stand-in source, which proves the mechanism
routes and not that a policy exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory import index
from bettermemory._handlers import ToolHandlers
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.handlers._shared import Context
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import (
    MemoryStore,
    Store,
    StoreRegistry,
    StoreSource,
)

from ._mcp import call_tool as _mcp_call


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def store(memory_dir: Path) -> Store:
    return Store(memory_dir)


@pytest.fixture
def server(memory_dir: Path, store: Store) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(config=cfg, store=store, state=SessionState())


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    res = await _mcp_call(server, name, kwargs)
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


class _FixedSource:
    """A `StoreSource` that hands back a chosen store for every request.

    The seam's whole point is that the answer can vary; this is the only
    way to observe that it is asked at all, since the shipped default
    answers the same root every time.
    """

    def __init__(self, resolved: Any) -> None:
        self._resolved = resolved
        self.calls = 0

    def for_request(self, ctx: Any | None) -> Any:
        self.calls += 1
        return self._resolved


class _CountingCtx:
    """Minimal duck-type for a request context: `identity.bind` reads
    headers and a principal off whatever it is handed, and nothing here
    needs more than that."""

    def __init__(self) -> None:
        self.request_context = None

    @property
    def headers(self) -> dict[str, str]:
        return {}


# ---------------------------------------------------------------------------
# Store.load_many — the batched id -> record path
# ---------------------------------------------------------------------------


def test_load_many_opens_the_index_once_for_many_ids(memory_dir: Path) -> None:
    """ONE index connection for N ids, not one open per id.

    The regression this guards is not a timing one. Implementing
    `load_many` as `[load_one(i) for i in ids]` lands green and is
    invisible below the FTS prefilter threshold, while opening the index
    once per candidate — 50 connections for one search — which is the
    pathology `index.links_for_many` was written to kill one module over.
    So this COUNTS opens rather than timing anything, following that
    precedent's evidentiary standard.
    """
    store = Store.open(memory_dir)
    ids = [
        store.write(content=f"alpha note {i}", scopes=["tools"]).id for i in range(6)
    ]

    opened = {"n": 0}
    real_connect = index._connect

    def counting_connect(path: Path) -> Any:
        opened["n"] += 1
        return real_connect(path)

    original = index._connect
    index._connect = counting_connect
    try:
        loaded = store.load_many(ids)
    finally:
        index._connect = original

    assert {m.id for m in loaded} == set(ids)
    assert opened["n"] == 1, f"load_many opened the index {opened['n']} times"


def test_load_many_returns_empty_without_touching_the_index(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty-ids short circuit is free, and is relied on: the search
    prefilter calls this with whatever the FTS query returned, which is
    nothing whenever the query matched nothing."""
    store = Store(memory_dir)
    calls = {"n": 0}
    monkeypatch.setattr(
        index,
        "filenames_for_ids",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    assert store.load_many([]) == []
    assert calls["n"] == 0


def test_load_many_propagates_an_index_failure(memory_dir: Path) -> None:
    """It does NOT get `@best_effort`, unlike `_indexed_path_for_id`.

    A swallowing version would return whichever candidates happened to
    resolve, which the caller cannot distinguish from a complete pool —
    `load_search_candidates` would still set `prefiltered=True` on it and
    silently narrow the BM25 corpus-IDF denominator. The caller owns the
    degrade decision, and it can only make it if this raises.
    """
    store = Store(memory_dir)
    written = store.write(content="alpha note", scopes=["tools"])

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("index meta is unparseable")

    original = index.filenames_for_ids
    index.filenames_for_ids = boom
    try:
        with pytest.raises(ValueError):
            store.load_many([written.id])
    finally:
        index.filenames_for_ids = original


def test_load_many_chunks_a_batch_past_the_parameter_ceiling(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`filenames_for_ids` binds one host parameter per id in a single
    `IN (…)` and does not chunk itself. 50 candidates never reach the
    ceiling, but this is a public method and the call is counted rather
    than trusted to stay small."""
    from bettermemory.store import _FILENAMES_FOR_IDS_CHUNK

    store = Store(memory_dir)
    seen: list[int] = []

    def spy(_root: Path, ids: list[str]) -> dict[str, str]:
        seen.append(len(ids))
        return {}

    monkeypatch.setattr(index, "filenames_for_ids", spy)
    store.load_many([f"id{i}" for i in range(_FILENAMES_FOR_IDS_CHUNK * 2 + 7)])

    assert seen == [_FILENAMES_FOR_IDS_CHUNK, _FILENAMES_FOR_IDS_CHUNK, 7]


def test_load_many_skips_a_row_the_sidecar_refuses(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quarantine is PRESERVED here, not decided here.

    7.17.2 fixed the prefilter leak ahead of this unit, and this method
    inherited the predicate when the loop moved into the store. A refactor
    that drops it would serve a pulled-and-refused body through
    `memory_search` again, with the index row still present because
    nothing rebuilt since the pull."""
    from bettermemory.quarantine import QuarantineEntry, save_quarantine

    store = Store.open(memory_dir)
    kept = store.write(content="deploy token rotation note", scopes=["tools"])
    held = store.write(content="deploy token rotation SECRET", scopes=["tools"])
    held_name = index.filenames_for_ids(memory_dir, [held.id])[held.id]
    save_quarantine(
        memory_dir,
        {
            held_name: QuarantineEntry(
                filename=held_name,
                reason="credential",
                detail="github_pat",
                remote="origin",
                pulled_at="2026-09-03T00:00:00+00:00",
                size=123,
                sha256="ab" * 32,
            )
        },
    )

    loaded = store.load_many([kept.id, held.id])
    assert [m.id for m in loaded] == [kept.id]
    # The file is still on disk: quarantine is an admission verdict, not
    # a deletion, which is what makes this a read-path rule.
    assert (memory_dir / held_name).exists()


def test_load_many_skips_a_name_the_id_no_longer_belongs_to(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index-drift defense, moved here with the loop.

    `sync pull` rewrites files in place, so between a pull and the next
    reindex the filename column can point at a body belonging to a
    different memory. Scoring the candidate's FTS hit against that body
    is the defect the guard exists to prevent."""
    store = Store.open(memory_dir)
    a = store.write(content="alpha one", scopes=["tools"])
    b = store.write(content="beta two", scopes=["tools"])
    a_name = index.filenames_for_ids(memory_dir, [a.id])[a.id]
    b_name = index.filenames_for_ids(memory_dir, [b.id])[b.id]
    # Point the lookup at the WRONG file for this id, which is the shape
    # a stale row has.
    monkeypatch.setattr(
        index, "filenames_for_ids", lambda _root, ids: {i: b_name for i in ids}
    )
    assert store.load_many([a.id]) == []
    assert b_name != a_name


def test_load_many_agrees_with_load_one_on_every_active_id(memory_dir: Path) -> None:
    """Two methods on one protocol, one answer — the asymmetry that WOULD
    be legitimate (this one omitting an unindexed id where `load_one`
    falls back to a full walk) must not extend to an id both can see."""
    store = Store.open(memory_dir)
    ids = [
        store.write(content=f"alpha note {i}", scopes=["tools"]).id for i in range(5)
    ]
    assert [m.id for m in store.load_many(ids)] == [store.load_one(i).id for i in ids]


# ---------------------------------------------------------------------------
# The store source seam
# ---------------------------------------------------------------------------


def test_default_source_returns_the_process_store_for_every_request(
    tmp_path: Path,
) -> None:
    # A pure `Store` on a root that is never touched: this test is about
    # which OBJECT comes back, and `DefaultStoreSource` reads only `root`.
    store = Store(tmp_path / "process-store")
    from bettermemory.store import DefaultStoreSource

    source = DefaultStoreSource(store)
    assert source.for_request(None) is store
    assert source.for_request(_CountingCtx()) is store


def test_store_registry_opens_each_root_once_and_returns_the_same_object(
    tmp_path: Path,
) -> None:
    """`Store.open` provisions and runs the startup pair, so it is right
    for a root entering service and wrong per call. One open per root, and
    the same instance back afterwards — which is what makes
    `ToolHandlers.for_request` able to return `self` on the common path."""
    opened: list[Path] = []

    def opener(root: Path) -> Any:
        opened.append(root)
        return Store(root)

    registry = StoreRegistry(opener=opener)
    first = registry.get(tmp_path / "a")
    second = registry.get(tmp_path / "a")
    third = registry.get(tmp_path / "b")

    assert first is second
    assert first is not third
    assert opened == [tmp_path / "a", tmp_path / "b"]
    assert len(registry) == 2


def test_store_registry_is_capped_and_evicts_the_least_recently_used(
    tmp_path: Path,
) -> None:
    """An uncapped map keyed by anything a request can vary is the
    unbounded-growth hazard the LRU exists to stop; `SessionRegistry` caps
    at the same number for the same reason."""
    registry = StoreRegistry(opener=lambda root: Store(root), max_roots=2)
    registry.get(tmp_path / "a")
    registry.get(tmp_path / "b")
    registry.get(tmp_path / "a")  # touch, so `b` is now the oldest
    registry.get(tmp_path / "c")

    assert len(registry) == 2
    assert tmp_path / "b" not in registry._stores
    assert tmp_path / "a" in registry._stores


def test_registry_key_is_not_the_store_key() -> None:
    """A guard against the tempting reuse, stated as a test because it
    would look like a tidy-up.

    `identity.registry_key` appends the transport session and any
    header-declared client/model. As a STORE key that shards one person
    across per-connection stores, so the store key must be the principal
    alone — and `DefaultStoreSource.principal_of` is the accessor that
    exists for that policy."""
    from bettermemory.identity import Actor, registry_key

    actor = Actor(
        client="claude-code",
        client_version="1",
        model="opus",
        principal="person@example.com",
        session="transport-session-1",
        sources={
            "client": "header",
            "client_version": "header",
            "model": "header",
            "session": "transport",
        },
    )
    key = registry_key(actor)
    assert key is not None and "session=" in key
    assert actor.principal is not None and key != actor.principal


def test_principal_of_returns_none_when_there_is_nothing_attested(
    tmp_path: Path,
) -> None:
    """Through `identity.bind`, the same binder the session registry uses,
    so the store seam cannot disagree with the session seam about who is
    on the request.

    Only the unattested branch is asserted here, and that is the honest
    scope: `tests/test_identity.py` owns the attested-principal shapes
    (the SDK's `authenticated_principal` is only reachable through a real
    request context), and this store-side accessor is a read of what that
    resolver published. A hand-built stand-in here would assert this
    file's own fiction."""
    from bettermemory.store import DefaultStoreSource

    source = DefaultStoreSource(Store(tmp_path / "process-store"))
    assert source.principal_of(None) is None
    assert source.principal_of(_CountingCtx()) is None


# ---------------------------------------------------------------------------
# ToolHandlers.for_request — the bundle, not the store
# ---------------------------------------------------------------------------


def _handlers(
    store: Any,
    *,
    source: Any = None,
    memory_dir: Path | None = None,
) -> ToolHandlers:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir or store.root)))
    return ToolHandlers(
        config=cfg,
        store=store,
        sessions=SessionState(),
        recorder=Recorder(root=store.root, session_id="sess_test", enabled=False),
        responses=__import__(
            "bettermemory._response", fromlist=["ResponseBuilder"]
        ).ResponseBuilder(stale_after_days=30),
        store_source=source,
    )


def test_for_request_returns_self_when_the_store_is_unchanged(memory_dir: Path) -> None:
    """The common path must not allocate.

    Every request goes through here — all 27 facade methods call it first
    — so a copy per request would be a per-request allocation and a broken
    identity assumption for any caller that compares bundles."""
    handlers = _handlers(Store(memory_dir))
    assert handlers.for_request(None) is handlers
    # A duck-typed stand-in rather than a real `Context`: `for_request`
    # passes it straight to `identity.bind`, which reads it defensively.
    assert handlers.for_request(_CountingCtx()) is handlers  # type: ignore[arg-type]


def test_for_request_rebinds_store_and_episode_store_together(
    memory_dir: Path,
) -> None:
    """Rebinding one without the other serves tenant A's memories beside
    tenant B's journal, with nothing erroring — and it is not confined to
    the `episode_*` tools, since `memory_write_confirm` reaches
    `deps.episode_store` privately to delete a promoted source episode."""
    process_store = Store(memory_dir)
    other_root = memory_dir.parent / "other-tenant"
    other_store = Store.open(other_root)

    handlers = _handlers(process_store, source=_FixedSource(other_store))
    scoped = handlers.for_request(None)

    assert scoped is not handlers
    assert scoped.store.root == other_store.root
    assert scoped.episode_store.root == other_store.root
    # Everything else is shared, which is what makes this a shallow copy
    # rather than a second bundle.
    assert scoped.sessions is handlers.sessions
    assert scoped.responses is handlers.responses
    assert scoped.config is handlers.config


def test_for_request_leaves_the_recorder_on_the_process_root(
    memory_dir: Path,
) -> None:
    """A STATED limit, pinned so the split is asserted rather than assumed.

    The recorder's root comes from config, never from the store, and it
    carries the event log the provenance tier is computed from. A recorder
    that followed a per-request store would be a per-tenant event log,
    which is a policy decision this unit does not make. The consequence,
    recorded here so it cannot be discovered later as a surprise: a
    hosted bundle's `store.root` and `recorder.root` can disagree, and no
    other test in the suite asserts that they agree."""
    process_store = Store(memory_dir)
    other_store = Store.open(memory_dir.parent / "other-tenant")

    handlers = _handlers(process_store, source=_FixedSource(other_store))
    scoped = handlers.for_request(None)

    assert scoped.store.root != handlers.recorder.root
    assert scoped.recorder is handlers.recorder


async def test_a_request_reads_the_store_the_source_resolved(memory_dir: Path) -> None:
    """The mechanism end to end: with a source that answers a different
    store, the tool call reads THAT store — with no edit at any read
    site, which is the property the bundle shape was chosen for."""
    process_store = Store.open(memory_dir)
    process_store.write(content="belongs to the process store", scopes=["tools"])

    other_root = memory_dir.parent / "other-tenant"
    other_store = Store.open(other_root)
    tenant = other_store.write(content="belongs to the other tenant", scopes=["tools"])

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(
        config=cfg,
        store=process_store,
        state=SessionState(),
        store_source=_FixedSource(other_store),
    )

    listed = await _call(server, "memory_list")
    assert [row["id"] for row in listed] == [tenant.id]


# ---------------------------------------------------------------------------
# The ordering invariant
# ---------------------------------------------------------------------------


def test_every_facade_method_resolves_the_bundle_before_delegating() -> None:
    """The invariant D2 PINS rather than creates.

    All 27 registered entry points called `sessions.for_request(ctx)` and
    none read `deps.store` before that line, which is why re-binding the
    bundle covers every read site with zero edits at those sites. That was
    true by convention; it is a rule now, so a tool added later cannot
    silently inherit the process store by forgetting one line.
    """
    import ast
    import inspect

    source = inspect.getsource(
        __import__("bettermemory._handlers", fromlist=["_handlers"])
    )
    module = ast.parse(source)
    cls = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "ToolHandlers"
    )
    checked = 0
    for method in cls.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if method.name in ("__init__", "for_request"):
            continue
        resolve_lines = [
            node.lineno
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "for_request"
        ]
        delegate_lines = [
            node.lineno
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_handlers_pkg"
        ]
        assert len(resolve_lines) == 1, (
            f"{method.name} calls for_request {len(resolve_lines)}x"
        )
        assert len(delegate_lines) == 1, (
            f"{method.name} delegates {len(delegate_lines)}x"
        )
        assert resolve_lines[0] < delegate_lines[0], (
            f"{method.name} delegates before resolving the store"
        )
        checked += 1
    # The count is the point: a refactor that drops a method from this
    # class silently narrows the pinned surface unless it is asserted.
    assert checked == 27, f"expected 27 facade methods, checked {checked}"


def test_the_store_the_handlers_hold_satisfies_the_protocol(memory_dir: Path) -> None:
    """The protocol is now CHECKED, not just declared.

    Through 7.17.x `MemoryStore` was used by no annotation in `src/` and
    verified by no type checker: the conformance test compares name SETS
    one way only, and its docstring claimed a mypy-clean assignment that
    did not exist in the tree. `build_server(store: MemoryStore)` is that
    assignment. This test is the runtime half of the same claim: the
    object the bundle holds really carries every protocol member."""
    store = Store(memory_dir)
    handlers = _handlers(store)
    for name in dir(MemoryStore):
        if name.startswith("_"):
            continue
        assert hasattr(handlers.store, name), f"store is missing {name}"
    # And the new member specifically, since it is the one D2 added.
    assert callable(handlers.store.load_many)


def test_a_custom_source_satisfies_the_protocol(memory_dir: Path) -> None:
    """`StoreSource` is structural, so a backend that resolves stores some
    other way needs no import from this project — only the method. The
    protocol is not `runtime_checkable` on purpose (an isinstance check
    would test method NAMES, not signatures), so the claim is checked the
    way mypy checks it: by assigning it."""
    source: StoreSource = _FixedSource(Store(memory_dir))
    resolved: MemoryStore = source.for_request(None)
    assert resolved.root == memory_dir


def test_context_defaults_to_none_and_is_never_required(memory_dir: Path) -> None:
    """The whole existing suite, the CLI and the Stop hook call handlers
    without a `ctx`. `for_request`'s parameter defaults, so `ctx=None`
    resolves the default bucket rather than raising — which is the
    difference between an additive seam and a flag day."""
    handlers = _handlers(Store(memory_dir))
    assert handlers.for_request().store.root == handlers.store.root
    assert handlers.for_request(Context()).store.root == handlers.store.root
