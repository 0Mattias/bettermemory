"""Wire bettermemory's config knobs to the embedding-model backend.

Pre-Round-3 these three helpers lived in ``server.py`` and were imported
back from ``cli/consolidate.py``, ``cli/reindex.py`` (and indirectly via
the test surface) — a back-edge that made ``cli/`` co-dependent with
``server.py`` and forced the cycle workaround documented in
``cli/serve.py``. Their job is purely the wiring (read provider /
model-name out of ``Config``, hand them to ``semantic.get_model`` or
``semantic.configure_persistent_cache``); they don't touch the MCP
server or anything else server-specific, so the colocation was historical, not
structural. Moving them to their own module lets ``cli/`` import them
directly without reaching back through ``server.py``.

``server.py`` still re-exports the three names so any out-of-tree
caller that imported them from there continues to resolve — and so
``_register_tools`` keeps its existing call site against the
re-exported symbol — but ``server.py`` is no longer the canonical
home. New callers should import from ``bettermemory.semantic_setup``.
"""

from __future__ import annotations

from typing import Any, cast

from .config import Config
from .store import Store


def _resolve_semantic_provider_and_model(
    config: Config,
) -> tuple[str | None, str | None]:
    """Pick the active embedding provider + its model name from config.

    Returns ``(provider, model_name)`` where provider is ``"torch"`` /
    ``"fastembed"`` and model_name is the matching config knob's value.
    Returns ``(None, None)`` when no provider is available (neither
    extra installed AND ``semantic_provider = "auto"``) — callers treat
    that as the Jaccard fallback signal.

    Honours ``[behavior] semantic_provider`` even when the corresponding
    extra isn't installed; the per-provider WARNING fires in
    ``semantic.get_model`` once the load attempt runs.
    """
    from .semantic import resolve_provider

    chosen = resolve_provider(config.behavior.semantic_provider)
    if chosen == "torch":
        return chosen, config.behavior.semantic_model_name
    if chosen == "fastembed":
        return chosen, config.behavior.semantic_model_fastembed
    return None, None


def _search_mode_needs_model(config: Config) -> bool:
    """True when ``[behavior] search_mode`` REQUIRES the embedding
    model — i.e. only ``semantic``, where the search handler
    hard-errors on a None model and the audit probe degrades to a
    permanent ``no_signal``. The factory must then at least attempt
    the load; a missing extra surfaces via ``get_model``'s
    per-provider WARNING plus the handler's install hint.

    ``hybrid`` is not a trigger HERE, and that is a statement about
    this predicate rather than about hybrid. Since ba7e857 hybrid DOES
    resolve a model — on the separate condition, in
    ``_semantic_model_configured``, that an embeddings extra actually
    imports. The two modes are split because they want opposite
    treatment in the case where no extra is installed:

    - ``semantic`` must attempt the load anyway. The handler raises
      either way, but only the attempt reaches ``get_model``, and
      ``get_model``'s per-provider WARNING is what puts the missing
      extra in the log rather than leaving the caller holding a tool
      error alone.
    - ``hybrid`` must not attempt it. It degrades to keyword+bm25 by
      design, so attempting would fire that same install-hint WARNING
      on every DEFAULT install — a config that asked for nothing — to
      benefit the minority who installed an extra.

    So this predicate answers "does the mode BREAK without a model",
    and ``_semantic_model_configured`` widens it to "…or would use one
    it can actually get". Widening it here instead would collapse the
    two answers onto the wrong one.

    The dedup side effect that used to be the stated reason for
    withholding hybrid is answered rather than ignored:
    ``handlers.write._resolve_dedup_thresholds`` reads
    ``semantic_dedup`` itself and asks the factory for nothing when it
    is off, so a model resolved for SEARCH no longer reaches the write
    path.
    """
    mode = (config.behavior.search_mode or "hybrid").strip().lower()
    return mode == "semantic"


def _semantic_model_configured(config: Config) -> bool:
    """True when SOME configured consumer wants the embedding model.
    Three arms, and they are not the same kind of condition:

    - write-dedup (``semantic_dedup = true``) — asked for, under any
      search mode;
    - retrieval that REQUIRES a model (``search_mode = "semantic"``,
      via ``_search_mode_needs_model``) — asked for;
    - retrieval that would USE one (``search_mode = "hybrid"``, the
      package default) — asked for AND gated on an embeddings extra
      importing. The long comment on that branch below is the why.

    This is the one place the arms are enumerated; the docstrings that
    used to restate them are the ones that went stale when the third
    was added, so point here instead of copying.

    The gate ``_semantic_model_or_none`` opens on, lifted out so a
    caller can ask the question WITHOUT triggering the model load. The
    web UI does exactly that: it never loads a model, and needs to know
    whether ``memory_search``'s ranking could be non-lexical while its
    own is. Answering it by restating the arms at the call site is how
    the two would drift, so there is one predicate and
    ``_semantic_model_or_none`` calls it too.

    But this predicate is only HALF of that web question — reading it as
    the whole answer is what once put a semantic-ranker caveat on
    ``/memories`` under configs where no semantic leg ranks at all. A
    loaded model reaches the RANKER only for ``search_mode`` in
    ``hybrid`` / ``semantic``
    (``handlers.search.memory_search`` leaves ``semantic_model=None``
    for ``keyword`` / ``bm25``), so ``semantic_dedup = true`` under
    ``keyword`` opens this gate for the write path while both search
    surfaces stay single-scorer. So: a caller asking whether to LOAD
    wants this predicate alone (``_semantic_model_or_none`` below); a
    caller asking whether a semantic leg RANKS must AND in the resolved
    search mode, as ``web._lexical_only_note`` does.

    What ``True`` promises differs by arm, so read it per arm. The two
    config arms probe nothing: ``True`` there means "the config asks
    for a model", and one may still not be returned —
    ``semantic_dedup = true`` with no extra installed opens this gate
    and gets ``None`` out of the factory. The ``hybrid`` arm DOES
    probe, returning ``_embeddings_extra_importable()``, so ``True``
    there also means an extra imported (an import, cached per process,
    not a model load). Even that is not a promise of a model: the OR in
    that helper spans both providers while ``_semantic_model_or_none``
    commits to the single one ``resolve_provider`` picks — the
    config-typo case is the whole of that distinction: an explicit
    torch preference with torch broken and fastembed healthy makes the
    OR true while the run loads nothing — and the load itself can fail. So no
    arm promises a model; one arm costs an import.
    """
    if bool(config.behavior.semantic_dedup) or _search_mode_needs_model(config):
        return True
    # `hybrid` (the package default) fuses a semantic leg when one is
    # handed to it, and MEASURED it is worth a lot: on a 180-document
    # blind-authored bench corpus (20 questions per probe; raw JSON in
    # bench/retrieval/results/v2-unpadded-2026-07-26.json) adding the leg
    # took recall@1 from 35% to 60% on questions as asked and from 80% to
    # 90% on re-queried ones — +25 points where the caller has to guess
    # the store's vocabulary, and +10 points ON TOP of the caller-side
    # query guidance, which is the part no prompt wording can recover.
    # That corpus is easier than a real store (bench/retrieval/README.md
    # says so of its own numbers), so the deltas are the finding; the
    # absolute rates are not a store's rates.
    #
    # So installing an embeddings extra is now sufficient to get it.
    # Requiring `semantic_dedup = true` as well made the extra a no-op
    # for the default mode — a documented foot-gun that cost two
    # sessions' worth of wrong install advice — and it opted the user
    # into an unrelated write-time behaviour change to buy a search
    # improvement. `handlers.write._resolve_dedup_thresholds` now reads
    # `semantic_dedup` directly, so resolving here no longer reaches the
    # write path.
    #
    # Gated on the extra actually importing, and that is load-bearing:
    # without it every default install would attempt a load and take
    # `get_model`'s install-hint WARNING on a config that asked for
    # nothing. Silence for the no-extra user is the whole point.
    if (config.behavior.search_mode or "hybrid") == "hybrid":
        return _embeddings_extra_importable()
    return False


# Two doctor-facing predicates lived here until 4.0.0 and stayed out when
# the door C reentry restored this module (search path only, R1
# declaration §1): the leg-active predicate ("would a semantic leg
# ACTUALLY score a search": model configured AND the mode resolves a
# model AND the RESOLVED provider imports) and its resolved-provider
# probe. The probe's lesson is kept where its callers went: "an extra
# imports" must ask about the provider resolution COMMITS to, because an
# explicit `semantic_provider = "torch"` with torch broken and fastembed
# healthy makes the either-extra OR true while the run loads nothing —
# doctor's 2026-07-25 false green, one condition further in. Rebuild
# both from this note if doctor's embeddings checks ever return.


def _embeddings_extra_importable() -> bool:
    """True when either embeddings extra can be imported.

    Mirrors what ``get_model`` will find at runtime rather than asking
    the config what it wants — the failure this exists to catch is a
    config that asks for a model no install can supply.

    Delegates the actual probing to ``semantic.extra_importable``, which
    owns the three-state answer (absent / working / installed-but-
    BROKEN). This function used to inline the probe with an
    ``except ImportError`` on each arm, which meant an installed-but-
    broken extra propagated its import-time exception out of a
    predicate and killed every ``memory_search`` — see that function's
    docstring for the incident. The knowledge of how an optional import
    can fail belongs in one place; this is the wiring.
    """
    from .semantic import extra_importable

    return extra_importable("sentence_transformers") or extra_importable("fastembed")


def _semantic_model_or_none(config: Config) -> Any:
    """Lazy load the embedding model when a configured consumer needs
    it. WHICH consumers those are is ``_semantic_model_configured``'s
    question and it is enumerated there alone — this function is the
    load, not a second copy of the policy. Returns ``None`` otherwise —
    callers treat ``None`` as the Jaccard / keyword+bm25 fallback
    signal.

    Gating on ``semantic_dedup`` alone (the pre-fix shape) conflated
    the dedup opt-in with search-mode model resolution: a user setting
    ``search_mode = "semantic"`` without the dedup flag hard-errored
    every memory_search and no_signal'd every audit probe even with an
    extra installed, contradicting the documented contract (config
    prose + docs/api.md: semantic mode needs only the extra).

    The first call after a consumer is enabled pays the model-load
    cost (~1-2s); subsequent calls hit ``semantic.get_model``'s
    in-memory cache.
    """
    if not _semantic_model_configured(config):
        return None
    from .semantic import Provider, get_model

    provider, model_name = _resolve_semantic_provider_and_model(config)
    if provider is None or model_name is None:
        # No extra installed and no explicit provider preference; let
        # get_model() emit its WARNING via the default torch path so
        # the user sees the install hint.
        return get_model(config.behavior.semantic_model_name)
    return get_model(model_name, provider=cast("Provider", provider))


def _configure_persistent_embeddings(config: Config, store: Store) -> None:
    """Hook the persistent embedding cache to the active store dir when
    semantic dedup is enabled. The cache file lives next to the events
    log and the memory bodies so it shares the same trust boundary —
    nothing new in the permissions story. No-op when semantic dedup is
    off: on-disk persistence (and the ``.embeddings.<model>.npz`` file
    it creates) stays part of the dedup opt-in. A search-mode-only
    consumer (see ``_search_mode_needs_model``) runs on the in-memory
    cache alone — deliberately, so flipping ``search_mode`` never
    starts writing new files into the store dir.
    """
    if not config.behavior.semantic_dedup:
        return
    from .semantic import Provider, configure_persistent_cache

    provider, model_name = _resolve_semantic_provider_and_model(config)
    if provider is None or model_name is None:
        # No active provider — leave the persistent cache disabled so
        # we don't create a `.embeddings.<model>.npz` file we'd never
        # hydrate from.
        return
    configure_persistent_cache(
        store.root, model_name, provider=cast("Provider", provider)
    )


__all__ = [
    "_resolve_semantic_provider_and_model",
    "_semantic_model_configured",
    "_semantic_model_or_none",
    "_configure_persistent_embeddings",
]
