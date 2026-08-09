# 2026-08-01 — a broken optional extra killed retrieval, and every check that should have said so was quiet

**Reported by:** the maintainer, as "had a few issues with bettermemory last night". No issue.
**bettermemory version at time of report:** 3.34.0 (the MCP server ran the live working tree; the same defect is in every published version back to the introduction of `_embeddings_extra_importable`).
**Fixed in:** v3.35.0
**Status:** fixed

## Symptom

Every `memory_search` call returned:

```
Error executing tool memory_search: frozenset()
```

The store was intact — 239 memories, all parsing, index reconciled, `PRAGMA
quick_check` clean. `memory_scope_overview`, `memory_show` and `memory_write`
all worked. Retrieval, the product, was gone, and the only thing the user had to
go on was the string `frozenset()`.

`bettermemory doctor` did not say anything useful either. Its one check that
names the embeddings extra reported:

```
✓ embeddings_extra: semantic_dedup disabled (no extras needed).
```

## Root cause

Three defects in a line, each of which alone would have made this survivable.

### 1. A capability probe modelled two states of a three-state world

`semantic_setup._embeddings_extra_importable` answered "is a semantic ranking
leg available" with:

```python
try:
    import sentence_transformers
    return True
except ImportError:
    pass
```

An optional dependency has **three** states — absent, working, and
installed-but-**broken** — and this models two. On this machine a cloud-sync
client had partially evicted the `transformers` tree out of the venv (226 of
2347 `.py` files left on disk), so the lazy-import scan transformers runs over
its own package tree found nothing and `transformers/__init__.py` raised
`KeyError: frozenset()` while executing. `sentence_transformers` imports
`transformers`, so the exception escaped a **predicate** — a function whose
entire contract is to return a bool — and travelled up through
`semantic_setup._semantic_model_or_none` and out of the MCP tool.

The asymmetry was already in the file. `semantic._load_torch_model` guarded the
model construction with `except Exception  # model load can fail many ways` and
the import immediately above it with `except ImportError`. Many failure modes
were anticipated for the constructor and exactly one for the import — though an
import executes arbitrary third-party module-level code and therefore has
strictly more.

Not the four-signal taxonomy; this is a **trust surface failing loudly in the
one direction the architecture promises it will not**. `hybrid`'s documented
behaviour is to degrade to keyword+BM25 when no model is available. The
degradation path existed and was unreachable, because reaching it required the
import to fail in the one way that had been imagined.

### 2. `doctor`'s embeddings check had gone silent on its own population

`doctor._check_embeddings_extra` returned early with "no extras needed" whenever
`semantic_dedup` was false — which is the default. That gate was correct when
the extra fed only write-time dedup. It has been stale since `ba7e857` made
`hybrid` resolve a model on installation alone: the extra now feeds **ranking**,
with no `semantic_dedup` involvement at all.

So the single check that names the embeddings extra was, by construction, silent
for exactly the configuration a broken extra degrades. This is the shape 3.34.0
was entirely about — a checker whose input population no longer matches the
population the feature serves — arriving from the other direction: not a
narrowed enumeration, but a gate that stopped selecting the right rows.

### 3. `_semantic_rank_leg_active`'s third condition was coarser than the routing

Found by an audit pass over the same class, not by the outage.
[`2026-07-25`](2026-07-25-doctor-false-green-on-importable-extra.md) replaced
"an extra is installed" with three conditions. The third, "an embeddings extra
imports", ORs across both providers — while `resolve_provider` commits to
exactly one. A healthy fastembed therefore vouched for a run that was going to
load torch.

`semantic_provider = "torch"` with torch not installed and fastembed healthy
made the OR true, resolution honoured the explicit preference, the loader
returned `None`, and nothing ranked semantically — while `doctor` reported `ok`,
printed *"a non-lexical signal scores every search"*, and **skipped its
retrieval-quality probe**. No damaged package is involved: it is a config typo,
which is precisely the class of thing `doctor` exists to catch. The same
resolver also preferred a broken torch over a working fastembed under
`"auto"`, losing the semantic leg on a machine that had a healthy provider
installed.

## Fix

`semantic.extra_importable` is now the one place that owns the three-state
answer. Broken returns `False` **and logs once per process at WARNING**; absent
returns `False` silently, because that is the default install and not a fault.
Logging the broken case is load-bearing: a bare `False` would have traded a loud
crash for a silent capability downgrade, where search quietly gets worse and
nothing in the process says why — the failure this directory exists to catch.
`extra_import_failure` retains the reason string, because the log file is not
what a user consults at diagnosis time and `doctor` needs the text.

Both model loaders grew the matching `except Exception` arm, and `find_spec` —
documented to return `None` for "not found" but which raises on some damaged
installs — now goes through `_spec_found`.

`doctor._check_embeddings_extra` evaluates the broken state **before** the
`semantic_dedup` gate, names the module and the import error, says *reinstall*
rather than install (telling someone to install what they already have sends
them looking in the wrong place), and accepts either extra for dedup instead of
only sentence-transformers.

`resolve_provider`'s auto arm returns the first provider that actually imports,
falling back to naming an installed-but-broken one so its loader emits the
warning rather than the caller receiving a bare `None`. Explicit preferences are
still honoured verbatim — an explicit provider is an instruction, and quietly
serving a different one would make the embedding cache's provider namespacing a
lie. `_semantic_rank_leg_active`'s condition 3 became
`_resolved_provider_importable`.

`web._lexical_only_note` was NOT given the same narrowing, and the attempt is
worth recording because the two gates look identical. Applying it there fails
`test_lexical_only_note_fires_exactly_when_a_semantic_leg_ranks`, which injects
a working model and spies on which scorers actually run: the note describes what
the HANDLER does, so gating it on whether an extra imports in the ambient
process couples a description of handler behaviour to a probe the handler need
not have used. That is the substance of the "must not be merged" note the
2026-07-25 fix left on both predicates, and it survived contact with a change
that had every appearance of being the same fix.

## Verification

`tests/test_broken_optional_extra.py` — non-resolving at HEAD, removed with
the lane it guarded in 4.0.0 — carried 27 cases as shipped. The third state is simulated
with a `sys.meta_path` finder whose `find_spec` succeeds and whose
`exec_module` raises — the shape of a half-installed package, and strictly
better than `sys.modules` injection because `find_spec`-based probes then see
"present" exactly as they do in production. It needs no broken package
installed and runs on every matrix leg including no-extras.

The exception is parametrised over `KeyError(frozenset())`, `AttributeError` and
`RuntimeError`, so the guard is about *anything that is not `ImportError`*
rather than about this one `KeyError`.

Two properties are pinned that a narrower test would have missed:

- `test_broken_extra_is_logged_once_not_silent` — exactly one WARNING across
  five probe calls. Both halves matter: silence is a hidden downgrade, and a
  per-call warning would bury the log, since `memory_search` probes every call.
- `test_absent_extra_is_silent` — its counterweight. Without it, "missing" could
  start warning for every default install and the signal would mean nothing.

The end-to-end cases drive the real MCP tools and assert a hit comes back, and a
write commits, with the extra broken — every unit assertion above could pass
while the tool still died, because the defect was an exception crossing a layer
boundary.

`test_rank_leg_active_when_the_resolved_provider_does_load` is the
anti-regression for defect 3: without it, hardcoding condition 3 to `False`
would satisfy the case that found the bug.

Negative control, run before shipping: reverting the source fails all of it,
including a provider case where the presence-only resolver returns `torch` and
then no model at all.

## What the surface should do differently

1. **A capability probe must answer, never raise.** The states an optional
   dependency can be in are absent, working, and broken; code that models two of
   them converts a fault in an optional feature into an outage in a required
   one. Where a constructor is already guarded `except Exception` because "it
   can fail many ways", the import above it needs at least the same width — it
   runs more third-party code, not less.
2. **Degrading must be loud.** The correct fix for a crash-on-degradation is not
   a silent `False`. A capability that disappears without a word is the same
   false green as a check that reports `ok` over an input it did not measure;
   this directory already exists because of the second one.
3. **A skip condition must be as precise as the routing it describes.** The
   2026-07-25 fix replaced one condition with three and the third stayed coarser
   than the thing it stood for. "Some provider imports" and "the provider that
   will be loaded imports" are different propositions whenever resolution picks.
4. **A gate that selects rows ages like an enumeration that lists them.** 3.34.0
   was about checkers that enumerate their own input population. A check gated on
   a config flag that used to imply the feature is the same defect with a
   different mechanism, and it needs the same re-derivation when the feature's
   trigger moves.
5. **Prose guards need a population that grows on its own.** The install page
   carried a retired claim for three releases while five other surfaces were
   pinned against it, because the guard listed two files by hand — and the
   drift's wording matched none of its forbidden literals either. Both halves
   have to be widened; adding the file alone would still have passed.

## References

- Related incidents: [`2026-07-25-doctor-false-green-on-importable-extra.md`](2026-07-25-doctor-false-green-on-importable-extra.md) (same predicate, one condition further out), [`2026-07-31-index-health-certified-a-stale-index.md`](2026-07-31-index-health-certified-a-stale-index.md) (a check certifying green over the wrong input)
- Related code: `src/bettermemory/doctor.py`; non-resolving at HEAD, removed with the lane in 4.0.0: `src/bettermemory/semantic.py`, `src/bettermemory/semantic_setup.py`
- Tests: `tests/test_broken_optional_extra.py` — non-resolving at HEAD; the 4.0.0 strip removed the optional-extra surface, this suite with it
