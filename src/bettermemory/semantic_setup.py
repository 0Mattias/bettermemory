"""Wire bettermemory's config knobs to the embedding-model backend.

Pre-Round-3 these three helpers lived in ``server.py`` and were imported
back from ``cli/consolidate.py``, ``cli/reindex.py`` (and indirectly via
the test surface) — a back-edge that made ``cli/`` co-dependent with
``server.py`` and forced the cycle workaround documented in
``cli/serve.py``. Their job is purely the wiring (read provider /
model-name out of ``Config``, hand them to ``semantic.get_model`` or
``semantic.configure_persistent_cache``); they don't touch FastMCP or
anything else server-specific, so the colocation was historical, not
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

    ``hybrid`` is deliberately NOT a trigger, even with an extra
    installed. The factory result feeds every consumer — including
    the write-dedup gates (``handlers/write.py`` passes any non-None
    factory result straight into ``find_similar``) — so resolving for
    hybrid would silently flip dedup from Jaccard to cosine for any
    extra-installed user who never opted into ``semantic_dedup``, and
    would make the DEFAULT config (hybrid) pay a model load the
    moment an extra is present. Hybrid keeps its graceful
    keyword+bm25 degrade, and per-call ``mode="semantic"`` without
    the config-level opt-in keeps its explicit install-hint error
    (``tests/test_server_search_mode.py`` pins that contract).
    Decoupling the search model from the dedup model would let hybrid
    fuse semantic without the dedup side effect; that needs the
    write-path consumer to stop reading the shared factory first.
    """
    mode = (config.behavior.search_mode or "hybrid").strip().lower()
    return mode == "semantic"


def _semantic_model_configured(config: Config) -> bool:
    """True when SOME configured consumer wants the embedding model —
    write-dedup (``semantic_dedup = true``) or retrieval
    (``search_mode = "semantic"``, via ``_search_mode_needs_model``).

    The gate ``_semantic_model_or_none`` opens on, lifted out so a
    caller can ask the question WITHOUT triggering the load. The web
    UI does exactly that: it never loads a model, and uses this to
    decide whether to tell the reader that its ranking is lexical
    while ``memory_search``'s may not be. Answering it by restating
    the two clauses at the call site is how the two would drift, so
    there is one predicate and ``_semantic_model_or_none`` calls it too.

    Says nothing about whether an embeddings extra is actually
    installed — that answer costs a load attempt. ``True`` here means
    "the config asks for a model", not "a model will be returned".
    """
    return bool(config.behavior.semantic_dedup) or _search_mode_needs_model(config)


def _semantic_model_or_none(config: Config) -> Any:
    """Lazy load the embedding model when a configured consumer needs
    it: write-dedup (``semantic_dedup = true``) or retrieval
    (``search_mode = "semantic"`` — see ``_search_mode_needs_model``).
    Returns ``None`` otherwise — callers treat ``None`` as the
    Jaccard / keyword+bm25 fallback signal.

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
