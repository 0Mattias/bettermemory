# 2026-07-25 — doctor read an importable extra as a working semantic leg

**Reported by:** self-found, while working out how to actually turn semantic ranking on. No issue.
**bettermemory version at time of report:** 3.28.0 in development. Both commits land inside the same release window, so no published version ever carried the defect — it was live on `main` for about nine hours.
**Fixed in:** v3.29.0 (`316781e`); introduced the same day by `ad56c07`
**Status:** fixed

## Symptom

`ad56c07` added `retrieval_discrimination`, the first `doctor` check that asks whether a store can still be *retrieved* rather than whether it is stored, parsed, synced or fresh. It shipped with a skip that short-circuited to `ok` whenever `search_mode` was `hybrid`/`semantic` and an embeddings package merely imported (`src/bettermemory/doctor.py` as of `ad56c07`):

```python
if cfg.behavior.search_mode in ("semantic", "hybrid") and _semantic_importable():
    return Diagnosis(
        name="retrieval_discrimination",
        status="ok",
        message=(
            f"search_mode = {cfg.behavior.search_mode} with an embeddings "
            "extra importable; ranking has a non-lexical signal, so the "
            "lexical vocabulary ceiling this check probes does not bind."
        ),
    )
```

Under the configuration shipped at that moment — the default `search_mode = "hybrid"` with `semantic_dedup = false` — `_semantic_model_configured` never opened, the model factory returned `None`, and ranking was purely lexical no matter what was installed. So the check reported `ok`, printed "ranking has a non-lexical signal" as its evidence, and returned before its probe ran — for precisely the store that most needed the warning. A green light computed over the wrong input, which is the exact failure class this check was written to expose.

The check's `fix_hint` was wrong in the same direction: it told the operator to install an extra and set `search_mode = "hybrid"`, which on its own did nothing.

## Root cause

Not a drift signal, so the four-signal taxonomy does not fit; the nearest row is **threshold rule**, and only loosely — this is a skip condition evaluating the wrong predicate. It is in this directory because a check whose job is to warn you, reporting green over an input it does not measure, costs the reader exactly what a wrong verdict costs.

The skip asked *"is an embeddings package importable?"*. The question that decides whether the lexical vocabulary ceiling binds is *"would a semantic leg actually score a search?"* — three conditions, not one:

1. the config asks some consumer for a model;
2. the mode `handlers.search.memory_search` resolves is one it hands a model **to** — `keyword`/`bm25` pass `semantic_model=None` regardless of the dedup flag;
3. an extra imports — as `316781e` stated it, and the one of the three since narrowed (see **Superseded — condition 3** below).

Installation is one third of the question, and at the time the default config failed the other two. `_search_mode_needs_model` documented why `hybrid` deliberately did not trigger a load (the factory result also fed the write-dedup consumer, so resolving for hybrid would have flipped dedup from Jaccard to cosine for anyone who never opted in) — the skip was written against the mode name without reading the predicate that governs it.

## Fix

`316781e`. The skip runs through `semantic_setup._semantic_rank_leg_active`, which requires all three conditions above instead of installation alone. Mode comparison is left unnormalised on purpose, mirroring the handler: a mis-cased `"Hybrid"` gets no model there, so it must read as no-leg here.

The predicate lives in `semantic_setup.py` beside its siblings rather than becoming a third private copy in `doctor.py`, and its docstring records that it is **not** a clone of `web._lexical_only_note`'s gate and must not be merged with it: that one omits the importability condition deliberately, because it asks whether `memory_search`'s ranking *could* differ from the page's and answers the no-extra case in prose rather than by suppressing the caveat. Same first two conditions, different question, different correct answer.

`fix_hint` was rewritten to name both halves of the requirement instead of the install alone.

One consequence worth recording, because it is the test of whether the fix was written about the right thing: `ba7e857`, later in the same release, changed the routing so that installing an extra *is* now sufficient under `hybrid`. The configuration the original regression test described stopped existing. The invariant survived unchanged, because it was written about routing rather than installation — the regression test moved to `keyword` + `semantic_dedup = true`, where a model loads for the write path while the search handler still passes `semantic_model=None`, and the check must still warn.

**Superseded — condition 3.** The Root cause and Fix above describe `_semantic_rank_leg_active` as `316781e` shipped it. `a3f5cbb` ("fix(extras): judge the semantic leg by the provider that will load", 3.35.0) narrowed the third condition from *an extra imports* to *the resolved provider imports*, `semantic_setup._resolved_provider_importable`. The coarse form ORs across both providers while `resolve_provider` commits to exactly one, so `semantic_provider = "torch"` with torch absent and fastembed healthy satisfied condition 3 as written above over a run whose loader returns `None` — this report's own false green, one condition further in, reached by a config typo rather than a damaged package. The first two conditions, the unnormalised mode comparison and the "must not be merged with `web._lexical_only_note`" note all survived the narrowing unchanged. The current predicate and the case that forced it are [`2026-08-01-broken-optional-extra-killed-retrieval.md`](2026-08-01-broken-optional-extra-killed-retrieval.md), root cause 3; its lesson 3 states the general rule condition 3 above fell short of: a skip condition must be as precise as the routing it describes.

## Verification

Since removed with its check in 4.0.0; as shipped: `test_retrieval_discrimination_does_not_skip_on_a_merely_installed_extra` in `tests/test_doctor.py`: the extra is patched importable, the config uses a mode the search handler never hands a model to, and the check must both `warn` **and** show that it ran — `assert diag.details["scopes"]`, "the probe must actually have run". Asserting on the status alone would have passed against a skip that guessed right for the wrong reason.

Its counterpart `test_retrieval_discrimination_short_circuits_when_semantic_available` pins the other direction by making the probe raise if it is called, so a legitimate skip cannot quietly start spending searches.

## What the surface should do differently

1. **A check may skip only on the condition it measures.** "An extra is installed" and "a semantic leg scores this search" are different propositions; the second is the one the ceiling depends on.
2. **Predicates belong next to the thing they describe.** The routing rule lives beside the factory that implements it, so a change to the routing moves one definition. Copying it into the diagnostic is how a check ends up describing a configuration the product no longer has.
3. **A skip branch must never be able to emit the check's success message without the check having run.** The regression test asserts the probe ran, not just the status — that assertion is what makes the invariant survive an unrelated behaviour change.
4. **`fix_hint` is a claim and rots like one.** It was wrong for the same reason the skip was: it described an install where the product needed a routing. Hints that tell an operator to set a flag should be validated against what the flag actually reaches.

## References

- Introduced: `ad56c07`, "feat(doctor): measure whether the store can still be found, not just stored".
- Fixed: `316781e`, "fix(doctor): an importable extra is not a semantic leg" — both in `## 3.29.0`, CHANGELOG section "Added — `doctor` can see whether retrieval still works" (the fix is folded into the "Reported, never auto-fixed. It skips only when a semantic leg would *actually score a search*" bullet, since neither commit had shipped yet).
- Related change: `ba7e857`, "feat(search): installing an embeddings extra now enables semantic search" — same release, and the reason the regression test's configuration moved.
- Superseded in part: `a3f5cbb`, "fix(extras): judge the semantic leg by the provider that will load", 3.35.0 — condition 3 became `_resolved_provider_importable`. This report's Root cause and Fix state the third condition as it shipped in 3.29.0.
- Related code: `_check_retrieval_discrimination` in `src/bettermemory/doctor.py`; `_semantic_rank_leg_active`, `_resolved_provider_importable`, `_semantic_model_configured` and `_search_mode_needs_model` in `src/bettermemory/semantic_setup.py` — the latter module non-resolving at HEAD; the 4.0.0 purist strip removed the semantic lane whole.
- Related incidents: [`2026-07-26-staleness-verdict-constant-function.md`](2026-07-26-staleness-verdict-constant-function.md) — the same failure class one day later, and measured rather than reasoned about; [`2026-08-01-broken-optional-extra-killed-retrieval.md`](2026-08-01-broken-optional-extra-killed-retrieval.md) — the same predicate one condition further in, and where its current form is documented.
