"""An optional extra that is INSTALLED BUT BROKEN must not kill retrieval.

The 2026-08-01 outage: every `memory_search` call returned
`Error executing tool memory_search: frozenset()`. The store was fine, the
ranker was fine, the index was fine. What had happened is that iCloud had
partially evicted the `transformers` package out of an iCloud-synced venv —
226 of 2347 `.py` files left on disk — so `transformers/__init__.py`'s own
lazy-import scan found nothing and raised `KeyError: frozenset()` while
executing. `sentence_transformers` imports `transformers`; bettermemory
probes `sentence_transformers` to decide whether a semantic ranking leg is
available; that probe caught `ImportError` and nothing else. So a fault in
an OPTIONAL ranking leg propagated out of a boolean predicate and took down
the required search path entirely.

Why the existing suite did not catch it: every test of the semantic wiring
exercises the extra being ABSENT or PRESENT-AND-WORKING. Nothing exercised
the third state. An optional dependency has three states, not two, and the
missing one is the only one that can raise something you did not plan for.

These tests simulate the third state with a `sys.meta_path` finder that
claims the module exists and then raises while executing it — the same
shape as the real failure — so they need no broken package installed and
run on every matrix leg, including the ones with no extras at all.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any, Iterator

import pytest

from bettermemory import semantic, semantic_setup
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call

# The exact exception the real incident raised. Not an `ImportError`
# subclass, which is the whole point — parametrised over a couple of
# shapes so the guard is about "anything that isn't ImportError" and not
# about this one KeyError.
BROKEN_EXCEPTIONS: list[BaseException] = [
    KeyError(frozenset()),
    AttributeError("partially initialized module has no attribute 'AutoModel'"),
    RuntimeError("CUDA driver version is insufficient"),
]


class _ExplodingLoader(importlib.abc.Loader):
    """A loader that creates the module fine and blows up executing it.

    This is what a half-installed package actually does: the finder
    locates it (the directory and metadata are on disk), and the failure
    only happens once its module-level code runs.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> Any:
        return None  # default module creation

    def exec_module(self, module: Any) -> None:
        raise self._exc


class _ExplodingFinder(importlib.abc.MetaPathFinder):
    def __init__(self, name: str, exc: BaseException) -> None:
        self._name = name
        self._exc = exc

    def find_spec(
        self, fullname: str, path: Any = None, target: Any = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != self._name:
            return None
        return importlib.machinery.ModuleSpec(fullname, _ExplodingLoader(self._exc))


@pytest.fixture
def broken_extra() -> Iterator[Any]:
    """Install a present-but-broken `sentence_transformers` for one test.

    Yields a callable so a test can choose the exception. Everything is
    torn down afterwards: the finder, any `sys.modules` residue, and
    bettermemory's own probe cache (which is a per-process cache by
    design — see `semantic.extra_importable`).
    """
    installed: list[_ExplodingFinder] = []
    saved = {
        name: sys.modules[name]
        for name in ("sentence_transformers", "fastembed")
        if name in sys.modules
    }

    def _install(exc: BaseException, name: str = "sentence_transformers") -> None:
        sys.modules.pop(name, None)
        finder = _ExplodingFinder(name, exc)
        sys.meta_path.insert(0, finder)
        installed.append(finder)
        semantic.reset_caches()

    yield _install

    for finder in installed:
        assert finder in sys.meta_path, "a test removed the finder behind our back"
        sys.meta_path.remove(finder)
    for name in ("sentence_transformers", "fastembed"):
        sys.modules.pop(name, None)
    sys.modules.update(saved)
    semantic.reset_caches()


@pytest.fixture
def working_fake_extra() -> Iterator[Any]:
    """Install a HEALTHY stand-in module for one test.

    The failover cases need one provider broken and the other working,
    and CI legs install at most one real extra. A minimal fake with the
    `.encode` surface `cached_embed` uses is enough — these tests are
    about which provider gets CHOSEN, not about embedding quality.
    """
    installed: list[str] = []
    saved = {
        name: sys.modules[name]
        for name in ("sentence_transformers", "fastembed")
        if name in sys.modules
    }

    def _install(name: str) -> None:
        module = types.ModuleType(name)

        class _Model:
            def __init__(self, *a: Any, **kw: Any) -> None: ...

            def encode(self, text: str, normalize_embeddings: bool = True) -> Any:
                return [0.0, 1.0]

            def embed(self, texts: list[str]) -> Any:
                return iter([[0.0, 1.0] for _ in texts])

        # Whichever symbol the matching loader reaches for.
        module.SentenceTransformer = _Model  # type: ignore[attr-defined]
        module.TextEmbedding = _Model  # type: ignore[attr-defined]
        # `importlib.util.find_spec` raises ValueError for a sys.modules
        # entry whose `__spec__` is None — which is the default for a
        # hand-built ModuleType. Without this the fake would read as
        # ABSENT to `_spec_found`, and the failover under test would be
        # exercised in name only.
        module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
        sys.modules[name] = module
        installed.append(name)
        semantic.reset_caches()

    yield _install

    for name in installed:
        sys.modules.pop(name, None)
    sys.modules.update(saved)
    semantic.reset_caches()


def _break_every_provider(broken_extra: Any, exc: BaseException) -> None:
    """Break BOTH providers.

    Any assertion of the form "no semantic leg is available" has to say
    this, because the three CI legs install different extras: the
    `embeddings` leg has a real sentence-transformers, the
    `embeddings-fast` leg a real fastembed, the default leg neither.
    Breaking only sentence-transformers leaves the OR in
    `_embeddings_extra_importable` satisfied by a healthy fastembed, and
    the test then passes on two legs and fails on the third — for the
    right reason, at the wrong time.

    The exploding finder makes `find_spec` succeed for both names, so
    "installed, and both broken" holds identically everywhere.
    """
    broken_extra(exc, "sentence_transformers")
    broken_extra(exc, "fastembed")


# ---------------------------------------------------------------------------
# The probe itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc", BROKEN_EXCEPTIONS, ids=lambda e: type(e).__name__)
def test_probe_returns_false_instead_of_raising(
    broken_extra: Any, exc: BaseException
) -> None:
    """`extra_importable` answers the question; it does not re-raise.

    This is the assertion whose absence WAS the outage.
    """
    broken_extra(exc)
    assert semantic.extra_importable("sentence_transformers") is False


@pytest.mark.parametrize("exc", BROKEN_EXCEPTIONS, ids=lambda e: type(e).__name__)
def test_setup_predicate_survives_broken_extra(
    broken_extra: Any, exc: BaseException
) -> None:
    """The predicate the search handler actually calls stays a predicate."""
    _break_every_provider(broken_extra, exc)
    cfg = Config(behavior=BehaviorConfig(search_mode="hybrid"))
    assert semantic_setup._embeddings_extra_importable() is False
    assert semantic_setup._semantic_model_or_none(cfg) is None


def test_broken_extra_is_logged_once_not_silent(
    broken_extra: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken extra degrades LOUDLY.

    Returning a bare False would trade a crash for a silent capability
    downgrade — search quietly gets worse and nothing says why. Equally,
    it must not log on every call: `memory_search` probes per call, so a
    per-call WARNING would bury the log.
    """
    broken_extra(KeyError(frozenset()))
    with caplog.at_level(logging.WARNING, logger="bettermemory.semantic"):
        for _ in range(5):
            assert semantic.extra_importable("sentence_transformers") is False

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one WARNING, got {len(warnings)}"
    message = warnings[0].getMessage()
    assert "sentence_transformers" in message
    assert "KeyError" in message


def test_absent_extra_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    """An ABSENT extra is the default install, not a fault — no WARNING.

    The counterweight to the test above: if "missing" also warned, the
    warning would fire for every default user and mean nothing.
    """
    semantic.reset_caches()
    with caplog.at_level(logging.WARNING, logger="bettermemory.semantic"):
        assert semantic.extra_importable("bettermemory_no_such_extra_xyz") is False
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


@pytest.mark.parametrize("exc", BROKEN_EXCEPTIONS, ids=lambda e: type(e).__name__)
def test_model_loader_returns_none_on_broken_extra(
    broken_extra: Any, exc: BaseException
) -> None:
    """`get_model` degrades to the documented None (Jaccard/keyword)."""
    broken_extra(exc)
    assert semantic.get_model("all-MiniLM-L6-v2", provider="torch") is None


def test_spec_probe_survives_a_raising_find_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`find_spec` raises on some damaged installs; presence probes cope.

    Documented to return None for "not found", but it raises
    `ModuleNotFoundError` when a parent package is missing and
    `ValueError` when a `sys.modules` entry has `__spec__ = None`.
    """

    def _boom(name: str, package: Any = None) -> Any:
        raise ValueError("sentence_transformers.__spec__ is None")

    monkeypatch.setattr(importlib.util, "find_spec", _boom)
    assert semantic._torch_extra_installed() is False
    assert semantic._fastembed_extra_installed() is False
    # `resolve_provider` sits directly on the search path via
    # `_resolve_semantic_provider_and_model`; it must still answer.
    assert semantic.resolve_provider("auto") is None


# ---------------------------------------------------------------------------
# End to end — the negative control that would have caught the outage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc", BROKEN_EXCEPTIONS, ids=lambda e: type(e).__name__)
async def test_memory_search_still_returns_hits_with_broken_extra(
    broken_extra: Any, exc: BaseException, memory_dir: Path
) -> None:
    """The product-level guard: retrieval survives a broken ranking leg.

    Every unit test above could pass while the tool still died, because
    the outage was about an exception crossing a layer boundary. So this
    one drives the real MCP tool and asserts a hit comes back.
    """
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(search_mode="hybrid"),
    )
    server = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState(), recorder=None
    )
    written = await _mcp_call(
        server,
        "memory_write",
        {
            "content": "The kubernetes ingress controller terminates TLS at the edge.",
            "scopes": ["infrastructure"],
        },
    )
    assert written["status"] == "committed"

    broken_extra(exc)

    result = await _mcp_call(
        server, "memory_search", {"query": "kubernetes ingress TLS", "max_results": 5}
    )
    assert result is not None, "memory_search returned nothing with a broken extra"
    hits = result.get("hits", result) if isinstance(result, dict) else result
    assert hits, f"expected a lexical hit despite the broken extra, got {result!r}"


async def test_memory_write_still_commits_with_broken_extra(
    broken_extra: Any, memory_dir: Path
) -> None:
    """Writes degrade to Jaccard dedup rather than failing.

    `handlers.write` resolves the same factory as search
    (`_semantic_model_or_none`) to pick cosine-vs-Jaccard dedup, so the
    outage's blast radius covered capture as well as retrieval — the two
    halves of the product.
    """
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(search_mode="hybrid"),
    )
    server = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState(), recorder=None
    )
    broken_extra(KeyError(frozenset()))

    written = await _mcp_call(
        server,
        "memory_write",
        {
            "content": "Postgres connection pooling runs through pgbouncer in transaction mode.",
            "scopes": ["infrastructure"],
        },
    )
    assert written["status"] == "committed", written


def test_doctor_reports_rather_than_raising_on_broken_extra(
    broken_extra: Any, memory_dir: Path
) -> None:
    """`bettermemory doctor` keeps producing a report.

    Doctor's per-check `_safe` wrapper already stopped a raising check
    from taking down the whole report, so pre-fix this surfaced as
    `retrieval_discrimination: check raised KeyError: frozenset()` with
    the generic "this is a bettermemory bug" hint rather than a crash.
    Post-fix the check must RUN rather than be caught, so it produces a
    real diagnosis instead of that fallback.
    """
    from bettermemory.doctor import _check_retrieval_discrimination

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(search_mode="hybrid"),
    )
    _break_every_provider(broken_extra, KeyError(frozenset()))

    diag = _check_retrieval_discrimination(memory_dir, cfg)
    assert diag.name == "retrieval_discrimination"
    assert "check raised" not in diag.message
    # And it must not claim the broken extra is importable. That ok-branch
    # message ("with an embeddings extra importable and a config that routes
    # it into ranking") is gated on `_semantic_rank_leg_active`, which ANDs
    # in the importability probe — pinned here because a future refactor
    # that dropped the AND would report a semantic leg that cannot score.
    assert "extra importable" not in diag.message


def test_auto_detect_fails_over_from_a_broken_torch_to_a_working_fastembed(
    broken_extra: Any, working_fake_extra: Any
) -> None:
    """Two extras exist so one can cover for the other.

    `resolve_provider`'s auto branch used to ask only whether a provider
    was ON DISK, so a broken sentence-transformers beat a healthy
    fastembed, `_load_torch_model` then returned None, and the semantic
    leg was lost on a machine with a perfectly good provider installed.
    """
    working_fake_extra("fastembed")
    broken_extra(KeyError(frozenset()))

    assert semantic.resolve_provider("auto") == "fastembed"
    assert semantic.get_model(provider="fastembed") is not None


def test_auto_detect_still_names_a_provider_when_every_extra_is_broken(
    broken_extra: Any,
) -> None:
    """All-broken returns a provider, not None.

    None means "nothing installed" downstream and earns `get_model`'s
    install hint — the wrong advice for someone who has it installed.
    Naming it routes to the loader whose WARNING says what actually
    failed.

    Both providers are broken deliberately — see `_break_every_provider`.
    """
    _break_every_provider(broken_extra, KeyError(frozenset()))
    assert semantic.resolve_provider("auto") == "torch"


def test_explicit_provider_preference_is_still_honoured_when_broken(
    broken_extra: Any, working_fake_extra: Any
) -> None:
    """An explicit preference is an instruction, not a hint.

    Silently serving fastembed to someone who wrote
    `semantic_provider = "torch"` would make the embedding cache's
    provider namespacing a lie — the vectors would be filed under the
    provider that did not produce them.
    """
    working_fake_extra("fastembed")
    broken_extra(KeyError(frozenset()))
    assert semantic.resolve_provider("torch") == "torch"


def test_doctor_accepts_fastembed_alone_for_semantic_dedup(
    working_fake_extra: Any,
) -> None:
    """Either extra satisfies write-time dedup.

    The check asked only about sentence-transformers, so an
    `[embeddings-fast]`-only user with `semantic_dedup = true` was told
    "the extra is not installed" while their cosine dedup worked fine —
    naming one member of a set the feature treats as interchangeable.
    """
    from bettermemory.doctor import _check_embeddings_extra

    working_fake_extra("fastembed")
    cfg = Config(behavior=BehaviorConfig(semantic_dedup=True))

    diag = _check_embeddings_extra(cfg)
    assert diag.status == "ok", diag
    assert "fastembed" in diag.message


def test_rank_leg_inactive_when_the_RESOLVED_provider_cannot_load(
    working_fake_extra: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy fastembed does not make a torch run non-lexical.

    No damaged package anywhere — just `semantic_provider = "torch"` with
    torch not installed. `_embeddings_extra_importable` ORs the two
    providers while `resolve_provider` honours the explicit preference and
    commits to one, so every predicate built on the OR claimed a semantic
    leg was scoring searches over a run whose model is None. `doctor` then
    reported `ok` AND skipped its retrieval probe.

    This is the same false green as
    `docs/incidents/2026-07-25-doctor-false-green-on-importable-extra.md`,
    one condition further in.
    """
    working_fake_extra("fastembed")
    cfg = Config(
        behavior=BehaviorConfig(search_mode="hybrid", semantic_provider="torch")
    )
    # Torch absent, expressed in the two pieces of production state that
    # say so: the presence probe and the import-probe cache. Seeding these
    # rather than patching the predicate under test keeps the assertions
    # below measuring real code — and this CI leg genuinely HAS
    # sentence-transformers installed, so "absent" has to be simulated or
    # the test would silently measure the healthy path (and download a
    # model doing it).
    monkeypatch.setattr(semantic, "_torch_extra_installed", lambda: False)
    monkeypatch.setitem(semantic._EXTRA_PROBE, "sentence_transformers", False)
    monkeypatch.setitem(
        semantic._MODEL_CACHE, ("torch", cfg.behavior.semantic_model_name), None
    )

    # The premise: resolution honours the explicit preference and gets nothing.
    assert semantic.resolve_provider("torch") == "torch"
    assert semantic_setup._semantic_model_or_none(cfg) is None
    # The old, coarser condition 3 still says yes — that is the bug.
    assert semantic_setup._embeddings_extra_importable() is True
    # The precise one, and the two surfaces built on it, must say no.
    assert semantic_setup._resolved_provider_importable(cfg) is False
    assert semantic_setup._semantic_rank_leg_active(cfg) is False


def test_rank_leg_active_when_the_resolved_provider_does_load(
    working_fake_extra: Any,
) -> None:
    """Anti-regression: the narrowing must not report no-leg everywhere.

    Without this, returning a constant False would pass the test above —
    the failure mode this project has published a postmortem about.
    """
    working_fake_extra("fastembed")
    cfg = Config(
        behavior=BehaviorConfig(search_mode="hybrid", semantic_provider="fastembed")
    )
    assert semantic_setup._resolved_provider_importable(cfg) is True
    assert semantic_setup._semantic_rank_leg_active(cfg) is True


@pytest.mark.parametrize("dedup", [True, False], ids=["dedup_on", "dedup_off"])
def test_rank_leg_predicate_is_false_when_the_extra_is_broken(
    broken_extra: Any, dedup: bool
) -> None:
    """`semantic_dedup = true` opens `_semantic_model_configured` without
    probing importability, so the AND in `_semantic_rank_leg_active` is
    the only thing keeping a BROKEN extra from being reported as a live
    semantic ranking leg. Both dedup settings pinned.
    """
    _break_every_provider(broken_extra, KeyError(frozenset()))
    cfg = Config(behavior=BehaviorConfig(search_mode="hybrid", semantic_dedup=dedup))
    assert semantic_setup._semantic_rank_leg_active(cfg) is False


def test_doctor_names_the_broken_extra_under_the_default_config(
    broken_extra: Any,
) -> None:
    """Doctor must SAY "installed but broken", on the default config.

    The gap this closes: `_check_embeddings_extra` used to answer
    "semantic_dedup disabled (no extras needed)" whenever
    `semantic_dedup` was false — which is the DEFAULT — even though the
    extra feeds ranking under the default `hybrid` mode. So during the
    outage the one check that names the embeddings extra reported ok.

    BOTH providers are broken deliberately, so this test states its own
    population instead of inheriting the machine's. `fail` asserts that
    ranking is really degraded, and that is only true when NO provider
    resolves — with a healthy sibling the check correctly de-escalates to
    `warn`, which
    ``test_doctor.py::test_embeddings_extra_warns_when_a_healthy_sibling_is_the_resolved_one``
    pins from the other side. Breaking only
    `sentence_transformers` made the verdict depend on whether fastembed
    happened to be installed: green on a CI leg that has neither, red on a
    developer box with both.
    """
    from bettermemory.doctor import _check_embeddings_extra

    cfg = Config(behavior=BehaviorConfig(search_mode="hybrid", semantic_dedup=False))
    broken_extra(KeyError(frozenset()))
    broken_extra(KeyError(frozenset()), name="fastembed")

    diag = _check_embeddings_extra(cfg)
    assert diag.status == "fail", diag
    assert "INSTALLED but" in diag.message
    assert "KeyError" in diag.message
    # The advice must be "reinstall", never "install" — the user has it.
    assert diag.fix_hint is not None
    assert "Reinstall" in diag.fix_hint


def test_doctor_stays_quiet_when_the_extra_is_merely_absent() -> None:
    """The counterweight: a default install with no extra is not a fault.

    Without this, the branch above would fire for every user who never
    installed an embeddings extra, and `embeddings_extra` would go
    permanently red on the most common configuration there is.
    """
    from bettermemory.doctor import _check_embeddings_extra

    semantic.reset_caches()
    cfg = Config(behavior=BehaviorConfig(search_mode="hybrid", semantic_dedup=False))
    diag = _check_embeddings_extra(cfg)
    assert diag.status == "ok", diag


# ---------------------------------------------------------------------------
# (a) vs (c) is a question of PRESENCE, not of exception type
#
# The tests above all break the extra with something that is NOT an
# `ImportError` — which is the shape of the 2026-08-01 incident, and also
# the only shape the first fix could tell apart from "absent". But (a) and
# (c) share `ImportError`: a `[embeddings]` install whose `torch` has gone
# raises `ModuleNotFoundError: No module named 'torch'`, and so does a
# machine that never installed the extra at all. Classifying by exception
# type filed the first as the second — no reason recorded, no WARNING, and
# every diagnostic telling the user to install what they already had.
#
# `ModuleNotFoundError` is used deliberately below rather than a bare
# `ImportError`: it is the subclass a real missing dependency raises, so
# a fix that special-cased only the base class would not pass.
# ---------------------------------------------------------------------------


def test_installed_extra_missing_its_own_dependency_is_broken_not_absent(
    broken_extra: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """`sentence_transformers` on disk with `torch` gone is state (c).

    The user-visible stake is the advice: (a) wants "install it" and (c)
    wants "reinstall it", and `extra_import_failure` is how every
    diagnostic downstream tells them apart. Filed as (a), it returned
    None and the WARNING never fired.
    """
    broken_extra(ModuleNotFoundError("No module named 'torch'", name="torch"))

    with caplog.at_level(logging.WARNING, logger="bettermemory.semantic"):
        assert semantic.extra_importable("sentence_transformers") is False

    reason = semantic.extra_import_failure("sentence_transformers")
    assert reason is not None, "an ImportError from a PRESENT extra is state (c)"
    assert "ModuleNotFoundError" in reason and "torch" in reason
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one WARNING, got {len(warnings)}"
    assert "installed but failed to import" in warnings[0].getMessage()


def test_a_genuinely_absent_extra_is_still_absent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The counterweight, and it is not decorative.

    Deciding by presence is only better than deciding by exception type
    if the presence probe can still say no. A `_spec_found` that answered
    True for everything would satisfy the test above and turn every
    default install's silent (a) into a WARNING plus a `doctor` failure.
    """
    semantic.reset_caches()
    with caplog.at_level(logging.WARNING, logger="bettermemory.semantic"):
        assert semantic.extra_importable("bettermemory_no_such_extra_xyz") is False
    assert semantic.extra_import_failure("bettermemory_no_such_extra_xyz") is None
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_loader_warning_says_reinstall_when_the_extra_is_present_but_broken(
    broken_extra: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The model loaders carried the same split, in a third copy.

    `_load_torch_model` / `_load_fastembed_model` chose between "the
    extra is not installed, install it" and "installed but broken,
    reinstall it" by which `except` arm caught — so the whole
    ImportError branch, including a present extra with a missing
    dependency, got the install wording. The log is where a user who
    never runs `doctor` finds out anything at all.
    """
    broken_extra(ModuleNotFoundError("No module named 'torch'", name="torch"))

    with caplog.at_level(logging.WARNING, logger="bettermemory.semantic"):
        assert semantic.get_model("all-MiniLM-L6-v2", provider="torch") is None

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Reinstall the extra" in m for m in messages), messages
    assert not any("is not installed" in m for m in messages), messages


def test_loader_warning_still_says_install_when_the_extra_is_absent(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterweight for the loader wording.

    Simulated rather than inherited: on the `embeddings` CI leg the real
    `sentence_transformers` imports fine, so an absent extra has to be
    staged or this asserts against the machine. A meta-path finder that
    raises for the name reproduces "not on disk" — `_spec_found` swallows
    the raise and answers False, which is the state under test.
    """

    class _Absent(importlib.abc.MetaPathFinder):
        def find_spec(
            self, fullname: str, path: Any = None, target: Any = None
        ) -> importlib.machinery.ModuleSpec | None:
            if fullname == "sentence_transformers":
                raise ModuleNotFoundError("No module named 'sentence_transformers'")
            return None

    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_Absent(), *sys.meta_path])
    semantic.reset_caches()
    try:
        with caplog.at_level(logging.WARNING, logger="bettermemory.semantic"):
            assert semantic.get_model("all-MiniLM-L6-v2", provider="torch") is None
    finally:
        semantic.reset_caches()

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("is not installed" in m for m in messages), messages
    assert not any("Reinstall" in m for m in messages), messages


def test_doctor_says_reinstall_when_the_break_arrives_as_an_ImportError(
    broken_extra: Any,
) -> None:
    """The payoff at the surface a user actually reads.

    Same assertion as `test_doctor_names_the_broken_extra_under_the_default
    _config`, with the breakage arriving as an `ImportError` instead. That
    one passed while this one could not: doctor reads
    `extra_import_failure`, which was empty for the whole ImportError
    branch, so the check fell through to its ok-return.
    """
    from bettermemory.doctor import _check_embeddings_extra

    cfg = Config(behavior=BehaviorConfig(search_mode="hybrid", semantic_dedup=False))
    _break_every_provider(
        broken_extra, ModuleNotFoundError("No module named 'torch'", name="torch")
    )

    diag = _check_embeddings_extra(cfg)
    assert diag.status == "fail", diag
    assert "INSTALLED but" in diag.message
    assert diag.fix_hint is not None and "Reinstall" in diag.fix_hint


# ---------------------------------------------------------------------------
# `mode='semantic'` — the per-request surface, told apart by state
#
# `memory_search(mode='semantic')` hard-errors when no model resolves, and
# that error is the only thing most callers will ever see about the extra.
# It spoke two states and offered one instruction, so the broken-install
# user was told to install what they had — the exact wrong turn
# `extra_import_failure` was added to stop `doctor` from taking.
# ---------------------------------------------------------------------------


async def test_semantic_mode_error_says_REINSTALL_when_the_extra_is_broken(
    broken_extra: Any, memory_dir: Path
) -> None:
    """A broken extra must not be answered with an install command."""
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(search_mode="hybrid"),
    )
    server = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState(), recorder=None
    )
    _break_every_provider(broken_extra, KeyError(frozenset()))

    with pytest.raises(Exception) as caught:
        await _mcp_call(server, "memory_search", {"query": "x", "mode": "semantic"})

    message = str(caught.value)
    assert "IS installed but fails to import" in message, message
    assert "Reinstall it" in message, message
    assert "KeyError" in message, "the reason belongs in the message, not just the log"
    # The pre-fix wording, which is what this test exists to keep out.
    assert "Install with" not in message, message


async def test_semantic_mode_error_still_says_install_when_nothing_is_installed(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterweight: (a) must keep the install hint.

    A message that said "reinstall" unconditionally would pass the test
    above and send every no-extra user looking for a package they do not
    have. The no-extra condition is simulated rather than inherited from
    the environment — two of the three CI legs install a real extra, and
    a test that reads its own machine measures nothing.
    """
    monkeypatch.setattr(semantic, "_torch_extra_installed", lambda: False)
    monkeypatch.setattr(semantic, "_fastembed_extra_installed", lambda: False)
    monkeypatch.setattr(
        "bettermemory.semantic_setup._embeddings_extra_importable", lambda: False
    )
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(search_mode="hybrid"),
    )
    server = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState(), recorder=None
    )

    with pytest.raises(Exception) as caught:
        await _mcp_call(server, "memory_search", {"query": "x", "mode": "semantic"})

    message = str(caught.value)
    assert "embeddings extra" in message, message
    assert "Install with" in message, message
    assert "Reinstall" not in message, message
