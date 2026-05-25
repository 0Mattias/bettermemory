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


def _semantic_model_or_none(config: Config) -> Any:
    """Lazy load the embedding model when ``semantic_dedup = true`` and
    an extra is installed. Returns ``None`` otherwise — callers treat
    ``None`` as the Jaccard fallback signal. The first call after
    ``semantic_dedup`` is enabled pays the model-load cost (~1-2s);
    subsequent calls hit ``semantic.get_model``'s in-memory cache.
    """
    if not config.behavior.semantic_dedup:
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
    off; when off, the in-memory cache is unused too, so persistence
    would be a write-only cycle.
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
    "_semantic_model_or_none",
    "_configure_persistent_embeddings",
]
