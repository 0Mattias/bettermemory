"""Optional embedding-based similarity for `memory_write` dedup.

The default `find_similar` is Jaccard on stopword-stripped, kebab-expanded
token sets — fast, deterministic, no extra deps. It catches lexical
overlap well but misses paraphrases ("the database" vs "Postgres",
"shipped" vs "released"). When the user enables `[behavior]
semantic_dedup = true` in config and has installed one of the embedding
extras, we add a cosine-similarity pass that catches those.

Two providers ship; both expose the same `.encode(body,
normalize_embeddings=True) -> numpy.ndarray` shape so the dedup path is
provider-agnostic:

- **torch** (`[embeddings]` extra): sentence-transformers + PyTorch.
  Default model `all-MiniLM-L6-v2` (~80 MB). Heavier disk + memory
  footprint, but it's the well-trodden path with the broadest model
  catalogue.
- **fastembed** (`[embeddings-fast]` extra): fastembed + ONNX Runtime.
  Default model `BAAI/bge-small-en-v1.5` (~33 MB ONNX, ~50 MB runtime
  total). Same retrieval surface; smaller install for users who can't
  afford ~500 MB of torch.

When both extras are installed `torch` wins by default — existing
`.embeddings.<model>.npz` caches stay byte-stable. Override the
auto-detection precedence via `[behavior] semantic_provider = "fastembed"`.

Imports are lazy — the module loads cleanly with neither extra installed.
A failed `get_model()` returns None and the caller falls back to Jaccard
with a single WARNING log line, so a user who flipped the config bit
without installing the deps is told plainly.

Caching: an in-process dict keyed by `memory_id` — when a memory is
updated, its `updated` timestamp moves, the cache key (`updated_key`)
mismatches, and we recompute. Optionally, a persistent layer flushes the
cache to disk so a fresh MCP server doesn't have to re-embed the whole
store on first use. The persistent layer is opt-in via
`configure_persistent_cache(root, model_name, provider=...)`:

- torch: `<root>/.embeddings.<safe_model>.npz` (legacy layout — keeps
  existing caches working without migration).
- fastembed: `<root>/.embeddings.fastembed.<safe_model>.npz`. Provider
  namespacing prevents a vector produced by one provider from leaking
  into the other's run — fastembed and torch vectors live in different
  embedding spaces even at the same nominal dimensionality.

Cache invalidation hierarchy (most-frequent first):
- Body unchanged, in-memory hit: returns instantly.
- Body changed (updated_key mismatch): recompute, replace entry, mark
  dirty for the next persist.
- Server restart with persistent cache: hydrate from disk; entries
  whose memory_id no longer exists stay in the file but are inert
  (nothing to match against); a future migration may prune them.
- Provider/model swap: file is namespaced by both, so flipping
  `semantic_provider` or `semantic_model_name` produces a fresh file
  at first use rather than mixing incompatible vectors.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("bettermemory.semantic")


# Provider identifier. `torch` = sentence-transformers ([embeddings]
# extra). `fastembed` = fastembed ([embeddings-fast] extra). Used by
# the persistent-cache path-namespacing and the auto-detection rule.
Provider = Literal["torch", "fastembed"]

# Default model per provider. Override via the matching config knob
# (`semantic_model_name` for torch, `semantic_model_fastembed` for
# fastembed). Different providers use different model catalogues — same
# nominal task, different identifiers.
#
# `DEFAULT_MODEL_NAME` is the torch default, kept under its historic
# name for backwards-compatible callers that still pass it explicitly
# (and the tests that reference the constant).
DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"
DEFAULT_FASTEMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"


# Persistent-cache filename shape: `<root>/.embeddings.[<provider>.]<safe>.npz`
# (the provider segment is omitted for the legacy torch layout — see
# `configure_persistent_cache` below). Lifted to module-level constants so
# `sync.py`'s gitignore writer can build the matching glob by importing both
# halves rather than hardcoding a sibling `.embeddings.*.npz` literal that
# would drift silently if the on-disk shape ever moved.
EMBEDDING_FILENAME_PREFIX = ".embeddings."
EMBEDDING_FILENAME_SUFFIX = ".npz"


# ---------------------------------------------------------------------------
# Model loader (lazy, cached, fail-soft)
# ---------------------------------------------------------------------------


# Cache keys are `(provider, model_name)` tuples so a fastembed model
# and a torch model that happen to share a name (rare in practice but
# possible) don't collide. The legacy `_MODEL_CACHE: dict[str, Any]`
# would have failed silently in that case — the new key shape makes
# the provider distinction first-class.
_MODEL_CACHE: dict[tuple[Provider, str], Any] = {}
_LOAD_FAILED: set[tuple[Provider, str]] = set()
_LOAD_FAILED_LOGGED: set[tuple[Provider, str]] = set()

# Probe results for `extra_importable`, keyed by module name, plus the
# once-per-process log ledger for the broken-install branch.
_EXTRA_PROBE: dict[str, bool] = {}
_EXTRA_BROKEN_LOGGED: set[str] = set()
# `"<ExcType>: <message>"` per module that was PRESENT but failed to
# import. Absent modules are deliberately not recorded — see
# `extra_import_failure`, which is the read surface for this.
_EXTRA_BROKEN_REASON: dict[str, str] = {}


def _record_broken_extra(module: str, exc: BaseException) -> None:
    """File state (c): ``module`` is PRESENT and failed to import.

    Two things happen for a broken extra and both belong together —
    the reason string ``extra_import_failure`` reads back, and a
    once-per-process WARNING. Shared by both ``except`` arms of
    ``extra_importable`` because those arms differ only in HOW the
    breakage announced itself (an ``ImportError`` from a missing
    dependency of the extra, or anything else from its module-level
    code); what it means for the caller, and what the user has to do
    about it, is identical.
    """
    _EXTRA_BROKEN_REASON[module] = f"{type(exc).__name__}: {exc}"
    if module in _EXTRA_BROKEN_LOGGED:
        return
    log.warning(
        "optional extra %r is installed but failed to import "
        "(%s: %s). Treating it as unavailable and falling back "
        "to keyword/BM25 ranking (and Jaccard dedup). This is "
        "usually a damaged or partially-upgraded install — "
        "reinstall the extra to restore semantic ranking.",
        module,
        type(exc).__name__,
        exc,
    )
    _EXTRA_BROKEN_LOGGED.add(module)


def extra_importable(module: str) -> bool:
    """True when the optional extra ``module`` imports CLEANLY.

    The one place that answers "can this optional dependency be used",
    because the answer has three states and the obvious two-state read
    of it took the product down.

    An optional extra can be (a) absent, (b) present and working, or
    (c) present and BROKEN. Every probe in this codebase used to model
    only (a) and (b)::

        try:
            import sentence_transformers
            return True
        except ImportError:
            return False

    which fails in two different ways. It CRASHES when an installed
    package raises something that is not ``ImportError`` while executing
    its own ``__init__``: the exception propagates out of a capability
    PROBE — a function whose entire contract is to return a bool — and
    through whatever required path asked the question. And it
    MISCLASSIFIES when the package raises ``ImportError``, because (a)
    and (c) both do: ``sentence_transformers`` present with ``torch``
    uninstalled raises ``ModuleNotFoundError: No module named 'torch'``,
    which by exception type alone is indistinguishable from
    ``sentence_transformers`` never having been installed.

    So the state is decided by PRESENCE, not by exception type — and
    the two ``except`` arms establish presence by different means. The
    ``ImportError`` arm consults ``_spec_found`` — "is it on disk",
    answered without importing — because that type leaves presence
    genuinely open; the exception only supplies the reason string.
    Reading the type instead is what sent a torch-less
    ``[embeddings]`` install down the (a) path: no reason recorded for
    ``extra_import_failure``, no WARNING, and ``doctor`` telling the
    user to install what they already had. Distinguishing (a) from (c)
    is the whole reason this function exists.

    The ``except Exception`` arm does NOT consult ``_spec_found``, and
    that is the same rule applied rather than an exception to it: a
    non-``ImportError`` escaping ``import_module`` means the module was
    found and its own module-level code ran and raised, so presence is
    already established by EXECUTION and a spec lookup could at best
    confirm it. That arm therefore files (c) unconditionally; the
    comment on it records the one residual case — a damaged finder
    raising for a genuinely absent module — and why erring that way
    round is the cheaper mistake.

    That is not hypothetical. It is the 2026-08-01 outage: a
    ``transformers`` tree that iCloud had partially evicted (226 of
    2347 ``.py`` files left on disk) made its own lazy-import scan find
    nothing, so ``transformers/__init__.py`` raised
    ``KeyError: frozenset()``. ``sentence_transformers`` imports
    ``transformers``, so the probe raised, so
    ``semantic_setup._semantic_model_or_none`` raised, so EVERY
    ``memory_search`` call returned
    ``Error executing tool memory_search: frozenset()``. Retrieval —
    the product — was dead for a fault in an OPTIONAL ranking leg whose
    documented behaviour is to degrade to keyword+bm25.

    Note the asymmetry this repairs. The model CONSTRUCTION in
    ``_load_torch_model`` was already guarded ``except Exception  #
    model load can fail many ways``. The authors correctly anticipated
    that a third-party model load fails in many ways, and assumed the
    import preceding it fails in exactly one. An import runs arbitrary
    third-party module-level code; it has strictly MORE ways to fail
    than the constructor does.

    Broken (c) is deliberately NOT silent. Returning a bare ``False``
    for it would trade a loud crash for a silent capability downgrade —
    search quietly gets worse and nothing in the process says why. So
    (c) logs once per process at WARNING while (a) stays silent, since
    "no extra installed" is the default install and not a fault.

    Cached per module name: a broken extra costs a real import attempt
    (the evicted tree above walked the filesystem for seconds before
    failing) and ``memory_search`` probes on every single call.
    ``reset_caches()`` clears it.
    """
    cached = _EXTRA_PROBE.get(module)
    if cached is not None:
        return cached

    try:
        importlib.import_module(module)
    except ImportError as exc:
        # (a) OR (c) — an `ImportError` says only that some import in
        # the chain failed, not whose. `_spec_found` is what separates
        # them: spec on disk means the extra IS installed and its own
        # dependency chain is what broke (c); no spec means it was never
        # installed (a). (a) stays silent by design — it is the default
        # install, and `get_model` owns the install hint for callers
        # that asked for a model explicitly.
        if _spec_found(module):
            _record_broken_extra(module, exc)
        _EXTRA_PROBE[module] = False
        return False
    except Exception as exc:  # noqa: BLE001 — see the docstring: an
        # import executes arbitrary third-party module-level code and
        # can raise anything. The probe's contract is a bool. Filed as
        # (c) without a spec lookup: the only documented way to raise a
        # non-ImportError from `import_module` is for the module's own
        # code to run, and the 2026-08-01 incident is exactly that. If a
        # damaged finder ever raises here from a genuinely absent
        # module, the cost is one WARNING naming it — the wrong-way-round
        # error of the two.
        _record_broken_extra(module, exc)
        _EXTRA_PROBE[module] = False
        return False

    _EXTRA_PROBE[module] = True
    return True


def extra_import_failure(module: str) -> str | None:
    """``"<ExcType>: <message>"`` when ``module`` is present but BROKEN.

    ``None`` both when the extra imports cleanly and when it is simply
    absent — the two states a caller has nothing to report about. This
    exists so a DIAGNOSTIC can tell the user which of the three states
    they are in, because the advice differs and getting it wrong wastes
    their time: an absent extra wants "install it", a broken one wants
    "reinstall it", and telling someone to install what they already
    have is how a diagnostic sends them looking in the wrong place.

    That is not a hypothetical either. During the 2026-08-01 outage the
    only surface that mentions the extra at all
    (``doctor``'s ``retrieval_discrimination`` fix hint) would have said
    "Install an embeddings extra — that is now the whole fix" to a user
    whose embeddings extra was installed and gutted.

    Runs the probe (cached) so callers need not sequence the two calls.
    """
    extra_importable(module)
    return _EXTRA_BROKEN_REASON.get(module)


def _spec_found(module: str) -> bool:
    """True when ``module``'s import spec resolves, without importing it.

    ``find_spec`` is documented to return ``None`` for "not found", but
    it RAISES for several flavours of damaged install:
    ``ModuleNotFoundError`` when a parent package is missing,
    ``ValueError`` when an entry in ``sys.modules`` has a ``__spec__``
    of ``None``. Both are "you cannot use this extra", not "crash the
    caller" — same three-state argument as ``extra_importable``, minus
    the log, because a spec check is cheap enough to repeat and the
    import probe that follows it owns the WARNING.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:  # noqa: BLE001 — damaged install; see docstring.
        return False


class _FastembedAdapter:
    """Wraps a `fastembed.TextEmbedding` to expose the same `.encode`
    surface that `sentence_transformers.SentenceTransformer` ships.

    The dedup path in `cached_embed` calls
    `model.encode(text, normalize_embeddings=True)` and expects a 1-D
    numpy array. fastembed returns a generator of vectors from
    `model.embed([texts])` — already L2-normalised by default for the
    BGE family — so the adapter wraps a single-text encode into the
    list/generator dance and unboxes the first result.
    """

    def __init__(self, model: Any) -> None:
        self._model = model

    def encode(self, text: str, normalize_embeddings: bool = True) -> Any:
        # fastembed returns a generator that yields numpy arrays. The
        # BGE / E5 / nomic-embed families ship normalised by default;
        # the kwarg is accepted for API parity with sentence-transformers
        # but does not need to re-normalise here.
        del normalize_embeddings  # API-parity placeholder; see above.
        vectors = list(self._model.embed([text]))
        return vectors[0]


def _log_provider_unavailable_once(
    key: tuple[Provider, str], module: str, extra: str, exc: BaseException
) -> None:
    """One WARNING per ``(provider, model_name)`` for a provider that
    would not import, worded for the state the extra is actually in.

    The two loaders each used to pick the wording by EXCEPTION TYPE:
    ``ImportError`` got "the extra is not installed, install it" and
    anything else got "installed but broken, reinstall it". That split
    is wrong in the same way ``extra_importable``'s was — an
    ``ImportError`` is what an absent extra raises AND what a PRESENT
    one raises when its own dependency chain is broken, e.g.
    ``sentence_transformers`` on disk with ``torch`` uninstalled. So the
    user with a half-gutted install read "not installed" and went to
    install what they already had.

    ``_spec_found`` decides instead, and it is the same question
    ``extra_importable`` asks, deliberately: two surfaces describing one
    machine state should not be able to disagree about which state it is.
    """
    if key in _LOAD_FAILED_LOGGED:
        return
    if _spec_found(module):
        log.warning(
            "the %s extra is installed but `%s` failed to import "
            "(%s: %s). Falling back to Jaccard / keyword. Reinstall the "
            "extra to restore semantic ranking.",
            extra,
            module,
            type(exc).__name__,
            exc,
        )
    else:
        # Spelled by `_install_hints`, which owns the quoting and
        # tool-form-leads rationale for every surface. Imported lazily —
        # this branch runs at most once per (provider, model) and only
        # on failure, so `semantic`'s own import path stays free of it.
        from ._install_hints import dev_clone_editable, tool_reinstall

        log.warning(
            "semantic provider %r requested but the %s extra is not "
            "installed. Install with `%s` (from a development clone: "
            "`%s`). Falling back to Jaccard / keyword.",
            key[0],
            extra,
            tool_reinstall(extra),
            dev_clone_editable(extra),
        )
    _LOAD_FAILED_LOGGED.add(key)


def _load_torch_model(model_name: str) -> Any | None:
    """Best-effort load of a sentence-transformers model.

    Returns the model on success, None when the extra is missing or the
    model can't be loaded. Logs once per (process, "torch", model_name)
    at WARNING.
    """
    key: tuple[Provider, str] = ("torch", model_name)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    if key in _LOAD_FAILED:
        return None

    try:
        # Lazy import: the module loads cleanly without the extra.
        from sentence_transformers import (  # pyright: ignore[reportMissingImports]
            SentenceTransformer,
        )
    except Exception as exc:  # noqa: BLE001 — an installed-but-broken
        # extra raises whatever its own module-level code raises. Same
        # three-state argument as `extra_importable`: this loader's
        # contract is "model or None", and a damaged optional package
        # must not escape it into the required search path. One arm, not
        # an `ImportError` arm plus a catch-all, because the wording is
        # chosen by whether the package is on disk rather than by which
        # exception arrived — see `_log_provider_unavailable_once`.
        _log_provider_unavailable_once(key, "sentence_transformers", "embeddings", exc)
        _LOAD_FAILED.add(key)
        return None

    try:
        model = SentenceTransformer(model_name)
    except Exception as exc:  # noqa: BLE001 — model load can fail many ways.
        if key not in _LOAD_FAILED_LOGGED:
            log.warning(
                "failed to load sentence-transformers model %r: %s. "
                "Falling back to Jaccard / keyword.",
                model_name,
                exc,
            )
            _LOAD_FAILED_LOGGED.add(key)
        _LOAD_FAILED.add(key)
        return None

    _MODEL_CACHE[key] = model
    return model


def _load_fastembed_model(model_name: str) -> Any | None:
    """Best-effort load of a fastembed model wrapped in
    `_FastembedAdapter` so callers see the same `.encode` surface as the
    torch path.

    Returns the adapter on success, None when the extra is missing or
    the model can't be loaded. Logs once per (process, "fastembed",
    model_name).

    Network: fastembed downloads ONNX weights to its own cache on first
    use; air-gapped installs need to pre-stage the cache directory (see
    fastembed docs for `FASTEMBED_CACHE_DIR`). The runtime path doesn't
    gate on a network flag — that's left to the user's environment.
    """
    key: tuple[Provider, str] = ("fastembed", model_name)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    if key in _LOAD_FAILED:
        return None

    try:
        from fastembed import TextEmbedding  # pyright: ignore[reportMissingImports]
    except Exception as exc:  # noqa: BLE001 — absent or installed-but-
        # broken; see the matching branch in `_load_torch_model`.
        _log_provider_unavailable_once(key, "fastembed", "embeddings-fast", exc)
        _LOAD_FAILED.add(key)
        return None

    try:
        model: Any = TextEmbedding(model_name=model_name)
    except Exception as exc:  # noqa: BLE001 — model load can fail many ways.
        if key not in _LOAD_FAILED_LOGGED:
            log.warning(
                "failed to load fastembed model %r: %s. Falling back "
                "to Jaccard / keyword.",
                model_name,
                exc,
            )
            _LOAD_FAILED_LOGGED.add(key)
        _LOAD_FAILED.add(key)
        return None

    adapter = _FastembedAdapter(model)
    _MODEL_CACHE[key] = adapter
    return adapter


def _torch_extra_installed() -> bool:
    """Return True iff the sentence-transformers import resolves.

    Goes through `_spec_found`, which uses `importlib.util.find_spec`
    so we never actually import the module — checking the extra's
    presence shouldn't pay the import cost — and swallows the raises a
    damaged install can produce. Same idiom for
    `_fastembed_extra_installed`.

    PRESENCE, not health: this answers "is it installed", and an
    installed-but-broken extra answers True here. That is deliberate —
    `resolve_provider` uses it to pick WHICH provider to try, and
    `_load_torch_model` is the layer that discovers the breakage and
    degrades. Don't repoint this at `extra_importable`: that would pay
    a full import inside a function documented not to.
    """
    return _spec_found("sentence_transformers")


def _fastembed_extra_installed() -> bool:
    """Return True iff the fastembed import resolves. See
    `_torch_extra_installed` for the spec-check rationale."""
    return _spec_found("fastembed")


def resolve_provider(preference: str | None = None) -> Provider | None:
    """Pick a provider given a config preference.

    `preference` is the raw `[behavior] semantic_provider` value
    (typically "auto" / "torch" / "fastembed" / None). The resolution
    rule:

    - Explicit "torch" or "fastembed": honour it, even if the extra
      isn't installed or is broken. The caller then sees None from
      `get_model()` and the per-provider warning explains which. An
      explicit preference is an instruction, not a hint, and silently
      serving a different provider than the one named would make the
      embedding cache's provider namespacing a lie.
    - "auto" or None: the first provider that actually WORKS, torch
      first (existing caches stay byte-stable), then fastembed. If
      neither works but one is installed, that one is returned anyway so
      its loader fires the warning naming the breakage. None only when
      no extra is installed at all.

    Auto-detect deliberately asks about HEALTH, not presence, and that
    costs an import probe (cached). Presence alone picked a broken torch
    over a working fastembed and then returned no model — losing the
    semantic leg entirely on a machine that had a perfectly good
    provider installed, while doctor's leg-active predicate (removed
    with its section in 4.0.0) went on reporting that a semantic leg was
    scoring searches, because that predicate ORed over the providers and
    this function had already committed to one. Two extras exist so one
    can cover for the other; a resolver that stops at "is it on disk"
    cannot do that.

    Returns the chosen Provider, or None when no provider is available.
    """
    pref = (preference or "auto").strip().lower()
    if pref == "torch":
        return "torch"
    if pref == "fastembed":
        return "fastembed"
    if pref not in {"auto", ""}:
        log.warning(
            "unknown semantic_provider %r; falling back to auto-detect.",
            preference,
        )
    torch_present = _torch_extra_installed()
    fastembed_present = _fastembed_extra_installed()
    # Healthy first, in the historic preference order.
    if torch_present and extra_importable("sentence_transformers"):
        return "torch"
    if fastembed_present and extra_importable("fastembed"):
        return "fastembed"
    # Everything installed is broken. Return one anyway rather than None:
    # None reads as "no extra installed" downstream and earns
    # `get_model`'s install hint, which is the wrong advice for someone
    # who has it installed. Returning it routes to the loader whose
    # WARNING names the actual import failure.
    if torch_present:
        return "torch"
    if fastembed_present:
        return "fastembed"
    return None


def get_model(
    model_name: str = DEFAULT_MODEL_NAME,
    *,
    provider: Provider | None = None,
) -> Any | None:
    """Return a cached embedding model (or None for Jaccard fallback).

    By default this is the legacy torch path — passing only
    `model_name` keeps every existing call site (and every existing
    test) byte-stable. Pass `provider="fastembed"` to opt into the
    ONNX path explicitly, or `provider="torch"` to be explicit on the
    legacy path.

    The returned object exposes
    `.encode(text, normalize_embeddings=True) -> numpy.ndarray` for both
    providers. None is the "fall back to Jaccard" signal.
    """
    chosen: Provider = provider or "torch"
    if chosen == "torch":
        return _load_torch_model(model_name)
    # The Provider Literal narrows to "fastembed" in the only remaining
    # branch — no defensive else needed; mypy strict catches invalid
    # providers at type-check time.
    return _load_fastembed_model(model_name)


# ---------------------------------------------------------------------------
# Embedding cache — keyed by (memory_id, updated)
# ---------------------------------------------------------------------------


@dataclass
class _CachedEmbedding:
    memory_id: str
    updated_key: str  # opaque key — typically isoformat(updated). Bumps on update.
    vector: Any


_EMBEDDING_CACHE: dict[str, _CachedEmbedding] = {}

# Persistent cache layer — opt-in via `configure_persistent_cache`. None
# means "in-memory only" (the default and only behaviour pre-this-change).
_PERSISTENT_PATH: Path | None = None
# `_HYDRATED` is per-(_PERSISTENT_PATH) — flips back to False whenever the
# path changes so the next read pulls in the new file. Lazy-loaded on
# first cache access (not at configure time) to avoid touching the disk
# during MCP server startup before we know whether semantic dedup is
# actually used in this session.
_HYDRATED: bool = False
# `_DIRTY` flags whether the in-memory cache has changes not yet flushed
# to the persistent file. `flush_persistent_cache()` is a no-op when
# False — saves a useless file write on every find_similar call that
# happened to hit cache.
_DIRTY: bool = False

# `_MODEL_DIM` records the live embedding model's output dimension,
# learned from the first fresh encode this process does — None until
# then. Reset whenever the cache is cleared or the persistent path
# changes. See `_note_model_dimension`.
_MODEL_DIM: int | None = None

# Consecutive `flush_persistent_cache` failures. Every failure path in
# the flush is caught and logged at WARNING so a broken cache can never
# break the dedup path — correct, but it made a PERMANENT failure
# indistinguishable from a transient one. A Windows operator whose
# `.npz` genuinely cannot be renamed into place (a reader holding the
# destination open for longer than `replace_atomic`'s ~150ms retry
# budget, a read-only memory dir, a full disk) got one warning line per
# flush, forever, and nothing that says "this is not recovering".
#
# The counter is what makes the persistent case observable, and it lives
# in-process ON PURPOSE: `doctor` runs as a separate CLI invocation and
# structurally cannot read the MCP server process's flush history, so a
# doctor check could only re-probe the filesystem and would miss exactly
# the transient-looking-but-permanent case this is about. An in-process
# counter plus a severity escalation is the signal that actually reaches
# the operator whose server is failing.
_FLUSH_FAILURES: int = 0

# After this many CONSECUTIVE failures the log line escalates from
# WARNING to ERROR and starts reporting the streak. Three is chosen to
# sit just past `replace_atomic`'s own retry budget: one flush can lose
# a genuine race, and a second is bad luck, but three in a row is a
# standing condition an operator has to act on.
_FLUSH_FAILURE_ESCALATION = 3


def persistent_cache_flush_failures() -> int:
    """Consecutive `flush_persistent_cache` failures in this process.

    Zero after any successful flush. Non-zero means the on-disk
    embedding cache is not being maintained — recomputable, so never a
    correctness problem, but a silent and unbounded performance cliff
    (every restart re-embeds everything) that used to leave no trace
    beyond repeated WARNING lines.
    """
    return _FLUSH_FAILURES


def _note_flush_outcome(exc: BaseException | None, detail: str) -> None:
    """Record a flush outcome and log the failure case at a severity
    that reflects whether it is transient or standing.

    ``exc is None`` resets the streak. Otherwise the streak increments
    and the message is logged at WARNING below the escalation threshold
    and at ERROR (with the streak count) at or above it, so a persistent
    failure is distinguishable in the operator's log from the one-off
    race that the first warning represents.
    """
    global _FLUSH_FAILURES
    if exc is None:
        _FLUSH_FAILURES = 0
        return
    _FLUSH_FAILURES += 1
    if _FLUSH_FAILURES >= _FLUSH_FAILURE_ESCALATION:
        log.error(
            "%s: %s — %d consecutive embedding-cache flush failures; the "
            "on-disk cache at %s is not being maintained and every restart "
            "will re-embed from scratch. Check the directory's writability "
            "and free space.",
            detail,
            exc,
            _FLUSH_FAILURES,
            _PERSISTENT_PATH,
        )
    else:
        log.warning("%s: %s", detail, exc)


def configure_persistent_cache(
    root: Path | None,
    model_name: str,
    *,
    provider: Provider = "torch",
) -> None:
    """Enable (or disable) on-disk persistence of the embedding cache.

    `root` is the memory store directory; the cache file lives next to
    `.events.jsonl`. Pass None for `root` to disable persistence — the
    in-memory cache continues to work but a server restart recomputes
    everything.

    File layout (per provider):
    - torch: `<root>/.embeddings.<safe_model>.npz` (legacy — keeps
      pre-2.5.0 caches loadable without migration).
    - fastembed: `<root>/.embeddings.fastembed.<safe_model>.npz`. The
      provider segment prevents fastembed vectors and torch vectors
      (different embedding spaces) from being read into the same
      run.

    `model_name` is namespaced into the filename so swapping models
    produces a fresh file rather than mixing incompatible vectors.
    Characters not safe for filenames are replaced with underscore
    (e.g. `sentence-transformers/all-MiniLM-L6-v2` becomes
    `sentence-transformers_all-MiniLM-L6-v2`).

    Calling this doesn't trigger a load; the next `cached_embed` call
    hydrates lazily so we don't pay the disk hit when semantic dedup
    is never used in a session.

    When the resolved path differs from the previously-configured one
    (including the disable -> enable transition, OR a provider swap),
    the in-memory cache is cleared so a stale entry from a different
    provider/model can't hit on the next lookup. Without that,
    swapping providers would silently return the old provider's
    vector for the same `(memory_id, updated_key)` and leave
    `_DIRTY=False`, so the new file would never be written.
    """
    global _PERSISTENT_PATH, _HYDRATED, _DIRTY, _MODEL_DIM
    new_path: Path | None = None
    if root is not None:
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", model_name)
        if provider == "torch":
            # Legacy layout — preserved verbatim so pre-2.5.0
            # `.embeddings.<model>.npz` files keep loading without a
            # migration step.
            new_path = (
                Path(root)
                / f"{EMBEDDING_FILENAME_PREFIX}{safe}{EMBEDDING_FILENAME_SUFFIX}"
            )
        else:
            new_path = (
                Path(root)
                / f"{EMBEDDING_FILENAME_PREFIX}{provider}.{safe}{EMBEDDING_FILENAME_SUFFIX}"
            )

    if new_path != _PERSISTENT_PATH:
        # Drop the in-memory cache; vectors keyed under the old model
        # name aren't valid lookup hits for the new one, and any
        # already-flushed entries can be re-hydrated from the new
        # file (or recomputed if no file exists yet).
        _EMBEDDING_CACHE.clear()

    _PERSISTENT_PATH = new_path
    _HYDRATED = False
    _DIRTY = False
    # New path -> next access re-hydrates; the dimension check must
    # re-run against whatever the new file holds.
    _MODEL_DIM = None


def _hydrate_persistent_cache() -> None:
    """Load the on-disk cache into `_EMBEDDING_CACHE` on first use.

    Idempotent: subsequent calls after a successful hydration are no-ops.
    Failures are logged at WARNING and silently fall back to an empty
    in-memory cache — a corrupt or partial cache file should never
    block the dedup path.
    """
    global _HYDRATED
    if _HYDRATED or _PERSISTENT_PATH is None:
        return
    if not _PERSISTENT_PATH.exists():
        _HYDRATED = True
        return
    try:
        # numpy import is lazy — only the persistent path needs it.
        # The semantic extra brings sentence-transformers which depends
        # on numpy, so when this code runs numpy is available; we still
        # guard so a misconfiguration (persistent path set without the
        # extra) degrades gracefully.
        import numpy as np  # pyright: ignore[reportMissingImports]
    except ImportError:
        log.warning(
            "persistent embedding cache configured but numpy is not "
            "available — falling back to in-memory cache only."
        )
        _HYDRATED = True
        return

    try:
        with np.load(_PERSISTENT_PATH, allow_pickle=False) as data:
            ids = list(data["ids"])
            keys = list(data["keys"])
            vectors = data["vectors"]
        for i, (memory_id, key) in enumerate(zip(ids, keys)):
            _EMBEDDING_CACHE[str(memory_id)] = _CachedEmbedding(
                memory_id=str(memory_id),
                updated_key=str(key),
                vector=vectors[i],
            )
    except Exception as exc:  # noqa: BLE001 — corrupt cache must never crash dedup
        log.warning(
            "persistent embedding cache at %s is unreadable (%s); "
            "falling back to in-memory cache.",
            _PERSISTENT_PATH,
            exc,
        )
    _HYDRATED = True


def flush_persistent_cache() -> None:
    """Write the in-memory cache to disk if persistence is configured
    and the cache has unsaved changes. Atomic via `.tmp` + rename so a
    crash mid-write leaves the previous cache file intact.

    Callers should invoke this at natural batch boundaries — typically
    end of a `find_similar` call. A no-op when persistence is disabled
    or no entry has changed since the last flush.
    """
    global _DIRTY
    if _PERSISTENT_PATH is None or not _DIRTY:
        return
    try:
        import numpy as np  # pyright: ignore[reportMissingImports]
    except ImportError:
        # Same as hydrate — silently fall back. This branch is unusual
        # because semantic dedup is what populates the cache in the
        # first place; if numpy is genuinely missing, _EMBEDDING_CACHE
        # is empty too.
        _DIRTY = False
        return

    if not _EMBEDDING_CACHE:
        # Empty-but-dirty: every entry was purged after the dirty flag
        # was set (e.g. `_note_model_dimension` dropped a whole batch of
        # stale-dimension entries). The dirty state still has to be
        # *resolved* — returning early without clearing `_DIRTY` strands
        # the flag, so a later genuine write (which sets `_DIRTY=True`
        # again) sees an already-set flag and may be skipped, losing
        # that write. Persist the empty state: drop the now-stale
        # on-disk file (so a restart can't re-hydrate the very entries
        # the purge removed) and clear the flag. Best-effort and under
        # the same exclusive lock the write path uses, so concurrent
        # flushers serialise; an unlink failure is a cleanup miss, not a
        # correctness risk — the recomputable cache survives either way.
        from ._fsutil import flock_excl, fsync_dir

        try:
            with flock_excl(_PERSISTENT_PATH):
                _PERSISTENT_PATH.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    fsync_dir(_PERSISTENT_PATH.parent)
        except Exception as exc:  # noqa: BLE001 — never break the dedup path
            _note_flush_outcome(
                exc, f"failed to clear stale embedding cache at {_PERSISTENT_PATH}"
            )
        else:
            _note_flush_outcome(None, "")
        _DIRTY = False
        return
    ids = []
    keys = []
    vectors = []
    for memory_id, cached in _EMBEDDING_CACHE.items():
        ids.append(memory_id)
        keys.append(cached.updated_key)
        vectors.append(cached.vector)
    # Stack into a 2D array for compact storage; np.savez_compressed
    # handles the rest. Object arrays for ids/keys are fine —
    # they're short ULID-shaped strings.
    _PERSISTENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Process-unique tmp name so two concurrent flushes (e.g. two MCP
    # servers in the same memory dir) don't collide on the same
    # `.tmp` path. Worst case is still last-writer-wins on the
    # rename — that's OK because the cache is fully recomputable —
    # but with a shared `.tmp` they'd corrupt each other's writes.
    tmp = _PERSISTENT_PATH.with_suffix(f"{_PERSISTENT_PATH.suffix}.tmp.{os.getpid()}")
    from ._fsutil import flock_excl, fsync_dir, fsync_file, replace_atomic

    try:
        # Serialize the rename against concurrent flushes. flock_excl
        # is per-inode exclusive; concurrent writers all serialise
        # through the same lockfile. The lock surface is small
        # (rename + chmod + fsync_dir); contention is negligible.
        with flock_excl(_PERSISTENT_PATH):
            # Pass an open file handle rather than the path. np.savez_compressed
            # auto-appends `.npz` to a path string that doesn't already end in
            # it — which would turn our `.npz.tmp` into `.npz.tmp.npz` and
            # break the atomic rename below. Writing to a file object bypasses
            # that suffix-mangling. fsync the file before closing so the bytes
            # backing the rename are durable, mirroring `_atomic_write_post`'s
            # discipline in the main store.
            with open(tmp, "wb") as f:
                np.savez_compressed(
                    f,
                    ids=np.array(ids),
                    keys=np.array(keys),
                    vectors=np.stack(vectors),
                )
                # fchmod BEFORE the rename so the file is 0o600 the
                # moment it appears at `_PERSISTENT_PATH`. `.npz`
                # contains vector representations of memory bodies —
                # same privacy bar as the source memories, which use
                # 0o600. The pre-fix shape called `os.chmod(path,
                # 0o600)` AFTER the rename, opening a window where the
                # file was world-readable at the visible path (umask
                # is typically 0o644 on Linux/macOS, sometimes 0o664
                # on shared-user boxes). fchmod-before-rename closes
                # that window — see `store._atomic_write_post` for the
                # canonical write-up of this discipline. Suppressed —
                # Windows has no mode bits and some sandbox
                # filesystems reject fchmod; that's a permission-bit
                # loss, not a corruption risk. `sys.platform`
                # narrowing keeps mypy happy on Windows where
                # `os.fchmod` is absent from typeshed.
                if sys.platform != "win32":
                    with contextlib.suppress(OSError):
                        os.fchmod(f.fileno(), 0o600)
                f.flush()
                fsync_file(f.fileno())
            # `replace_atomic`, not a bare `tmp.replace(...)`. `flock_excl`
            # above serialises this rename against other *flushers*, but
            # NOT against *readers*: `_hydrate_persistent_cache` opens the
            # destination with `np.load(...)` and takes no lock, so a
            # second MCP server in the same memory dir can hold an open
            # handle on `_PERSISTENT_PATH` at exactly this moment. On
            # Windows that makes the rename fail with PermissionError
            # (ERROR_ACCESS_DENIED / ERROR_SHARING_VIOLATION) — the same
            # transient open-destination race 3.25.1 closed for the store
            # write path. The enclosing `except Exception` would swallow
            # it into a log warning, so the symptom is not a crash but a
            # cache that silently never persists on Windows.
            replace_atomic(tmp, _PERSISTENT_PATH)
            # Defensive post-rename chmod (belt-and-suspenders): if
            # the filesystem squashed the mode on rename (rare — most
            # POSIX filesystems preserve it) we can still recover.
            # This is a no-op when the fchmod above succeeded.
            with contextlib.suppress(OSError):
                os.chmod(_PERSISTENT_PATH, 0o600)
            # fsync the parent directory so the rename survives crash —
            # mirror `_atomic_write_post`.
            fsync_dir(_PERSISTENT_PATH.parent)
        _DIRTY = False
    except Exception as exc:  # noqa: BLE001 — never break the dedup path
        # Clean up the orphaned tmp so a half-written cache doesn't sit
        # in the persistence directory until the next successful flush
        # rolls over it. `missing_ok` covers the case where the failure
        # happened before the file was created (or after the rename).
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        # Counted and escalated, not just logged. `replace_atomic`'s
        # Windows retry absorbs the millisecond-scale open-destination
        # race; a failure that reaches here has already outlived that
        # budget, and repeating it means the cache is permanently not
        # being written. See `_note_flush_outcome`.
        _note_flush_outcome(
            exc, f"failed to persist embedding cache to {_PERSISTENT_PATH}"
        )
    else:
        _note_flush_outcome(None, "")


def _note_model_dimension(dim: int) -> None:
    """Record the live model's embedding dimension and, the first time
    it's seen, drop any cache entries that don't match it.

    A persistent cache written under one model checkpoint and hydrated
    under another (same `model_name`, different output dimension)
    would otherwise pair a stale-dimension cached vector against a
    freshly-computed one in `cosine_similarity_normalized`, whose
    `zip(strict=True)` raises `ValueError` — uncaught on the
    `memory_write` -> `find_similar` path, so the whole handler fails.

    Every caller that computes a *fresh* embedding feeds the dimension
    here: `cached_embed`'s own cache-miss branch, and the query encode
    in `find_similar` / `_search` / `find_similar_tombstones` (which
    runs before any `cached_embed`, so stale entries are purged before
    a cache hit can hand one back). The one-time purge forces every
    stale entry to recompute at the current dimension. No probe encode
    — the dimension is taken from work the caller already did.
    """
    global _MODEL_DIM, _DIRTY
    if _MODEL_DIM == dim:
        return
    _MODEL_DIM = dim
    stale = [mid for mid, c in _EMBEDDING_CACHE.items() if len(c.vector) != dim]
    if stale:
        for mid in stale:
            del _EMBEDDING_CACHE[mid]
        _DIRTY = True
        log.warning(
            "embedding cache: dropped %d stale-dimension entries — the "
            "persistent cache was written under a different model "
            "checkpoint; they will be recomputed at the current "
            "dimension.",
            len(stale),
        )


def cached_embed(
    model: Any,
    memory_id: str,
    updated_key: str,
    body: str,
) -> Any:
    """Return an embedding for `body`, caching by `(memory_id,
    updated_key)`.

    `updated_key` is whatever the caller treats as a freshness handle —
    typically `isoformat(memory.updated)`. When the memory is updated,
    `updated_key` changes and we recompute. The cache survives across
    `find_similar` calls within one process. When persistent caching
    is configured, hits also survive across process restarts; cache
    misses mark the cache dirty for the next `flush_persistent_cache`.
    """
    global _DIRTY
    _hydrate_persistent_cache()
    cached = _EMBEDDING_CACHE.get(memory_id)
    if (
        cached is not None
        and cached.updated_key == updated_key
        and (_MODEL_DIM is None or len(cached.vector) == _MODEL_DIM)
    ):
        return cached.vector
    vector = model.encode(body, normalize_embeddings=True)
    # A fresh encode — its length is the live model dimension. Feed it
    # to the reconcile pass so any stale-dimension hydrated entries are
    # purged before they can reach `cosine_similarity_normalized`.
    _note_model_dimension(len(vector))
    _EMBEDDING_CACHE[memory_id] = _CachedEmbedding(
        memory_id=memory_id, updated_key=updated_key, vector=vector
    )
    _DIRTY = True
    return vector


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def cosine_similarity_normalized(a: Any, b: Any) -> float:
    """Cosine similarity assuming both inputs are L2-normalized.

    For normalized vectors `cos(a, b) = a · b`. Works on numpy arrays,
    plain lists, or any iterable of floats — we don't import numpy
    explicitly, so the module remains usable when only sentence-transformers
    (which brings numpy) is installed but not used.

    Raises ``ValueError`` on dimension mismatch. ``zip(strict=True)``
    catches the case where the persistent cache was written with one
    embedding model's output dimension and is being read against
    another — pre-2.6.4 ``zip(a, b)`` truncated to the shorter input
    and produced a meaningless similarity over the overlap, which
    still passed the threshold and silently misranked dedup.
    """
    return float(sum(x * y for x, y in zip(a, b, strict=True)))


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def reset_caches() -> None:
    """Clear all module-level caches. Tests use this to isolate cases.

    Resets the persistent-cache configuration too — any in-progress
    `_HYDRATED` / `_DIRTY` state is cleared, and a subsequent call
    needs to reconfigure persistence explicitly. Doesn't touch the
    on-disk file; that's a deliberate filesystem effect that survives.
    """
    global _PERSISTENT_PATH, _HYDRATED, _DIRTY, _MODEL_DIM, _FLUSH_FAILURES
    _MODEL_CACHE.clear()
    _LOAD_FAILED.clear()
    _LOAD_FAILED_LOGGED.clear()
    _EXTRA_PROBE.clear()
    _EXTRA_BROKEN_LOGGED.clear()
    _EXTRA_BROKEN_REASON.clear()
    _EMBEDDING_CACHE.clear()
    _PERSISTENT_PATH = None
    _HYDRATED = False
    _DIRTY = False
    _MODEL_DIM = None
    _FLUSH_FAILURES = 0


__all__ = [
    "DEFAULT_MODEL_NAME",
    "DEFAULT_FASTEMBED_MODEL_NAME",
    "Provider",
    "configure_persistent_cache",
    "flush_persistent_cache",
    "get_model",
    "cached_embed",
    "cosine_similarity_normalized",
    "extra_import_failure",
    "extra_importable",
    "persistent_cache_flush_failures",
    "resolve_provider",
    "reset_caches",
]
