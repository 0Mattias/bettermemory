# Phase 4 entry brief — Retrieval spend (C2-prereq → C2; C3; C5)

Written 2026-07-30 at HEAD `bbc7672` (v3.30.0 + four phases of Unreleased),
after a full anchor re-verification pass: the upgrade plan's Phase 4 section
and the retrieval fact pack were written at `95af021`, five substantive
commits back, and Phases 2–3 rewrote the exact pipeline region Phase 4 plugs
into. This brief is the corrected, self-contained entry point. The executing
session has no access to the sessions that produced it.

**Anchor rule (applies to every `file:line` in this document): all line
numbers are as-of `bbc7672`. Re-locate by the named symbol before editing.
If HEAD has moved past `bbc7672`, treat every number as a hint only.**

This file lives under `docs/audit/` deliberately: that directory is outside
the doc-claims checker's corpus (non-recursive `docs/*.md` glob), so these
citations are unchecked by CI. They were verified by hand at `bbc7672`;
keep them true anyway.

---

## 0. Entry protocol

You are executing autonomously (standing grant 2026-07-09: push main AND
tags/PyPI; scope confirmed "Everything"). In order:

1. Read this brief fully.
2. Confirm HEAD is at or past `bbc7672` and the tree is clean. CI for
   `bbc7672` is run `30575838549`, full matrix green (re-verified:
   `conclusion=success`, headSha `bbc7672…`). HEAD WILL be past `bbc7672`
   — the commit that lands this brief is itself past it. Cheap
   discriminator before re-verifying anything:
   `git diff --stat bbc7672..HEAD -- src/ tests/ bench/ .github/` —
   empty means only docs moved and every anchor in this document is still
   exact; non-empty means premise-check the touched files' anchors by
   symbol before implementing.
3. `memory_scope_overview`, then `episode_handoff` — the coordinator
   session's closing episode (written when this brief was committed)
   should name this brief as the entry point. If no episode names this
   brief, do not go hunting: THIS DOCUMENT is the authoritative entry
   point, and any episode dated before it (including Phase 3's closing
   episode and its "next work" pointer) is superseded for sequencing
   purposes.
4. `memory_show 01KYRSJZ377EDVZ39SCHX546FK` (audit record) and the release
   runbook `01KS9M8D32343QVS70RJHF7V6A`. The runbook memory currently shows
   `commit_drift` — spot-check one claim against the repo before relying on
   it; `memory_verify` if it holds.
5. Read `docs/audit/upgrade-plan-2026-07-30.md`: section 0 (operating
   rules), the Phase 4 section, AND the PHASE STATUS blocks for Phases 0–3 —
   the status blocks carry deltas that changed Phase 4's premises (B3's
   `matched_leg` above all).
6. Read `docs/audit/upgrade-plan-facts/retrieval-episodes.md` hazards 1–18
   WITH section 1 (stale claims) and section 8 (hazard overlay) of this
   brief laid over them. Do not act on a fact-pack line number.
7. Verify the suite baseline BEFORE touching anything. At `bbc7672`:
   default leg **4,073 passed / 16 skipped**; embeddings leg **3,998 passed
   / 3 skipped / 8 deselected** (invoked with
   `-m "not no_extras and not no_torch_embeddings"` — two `matched_leg`
   tests are `no_extras`-marked because the leg legitimately reads `both`
   under the extra). ruff, mypy, pyright clean. If the default leg's skip
   count is anything other than 16, the venv is not the default leg —
   most likely numpy leaked in after an embeddings swap (see section 5).

---

## 1. Stale claims — corrected herein; do not re-trust the originals

Every item below was true at `95af021` and is false or moved at `bbc7672`.

1. **Plan §0 step 4** says confirm HEAD at-or-past `95af021` with suite
   3,779/8. Superseded: at-or-past `bbc7672`, baselines in section 0 above.
2. **Plan risk register: "1,164 chars of [DESC] slack"** — stale. Live
   measurement at `bbc7672`: total **27,048 / 27,500 → 452 slack** (Phase 2
   spent 526, Phase 3 spent 188 net).
3. **Plan C2: "between the RRF fuse and the top-k trim
   (`search.py:2407→2409`)"** — the region moved and grew scaffolding. At
   `bbc7672` the fuse is `:2577`, the trim `:2579`, and B3's `matched_leg`
   bookkeeping now lives AROUND them (section 2, C2 below). The two lines
   are still adjacent — the insertion point itself is intact.
4. **Fact pack: every `search.py` anchor shifted ~+120–180.** Notables:
   `_build_hit` `:1816-1857` → `:1930-1978`; snippet call `:1842` →
   `:1956`; browse `_build_hit` `:2252` → `:2388`; SimilarHit
   `:2785/:2803` → `:2964/:2982`; `hit_to_dict` snippet `_response.py:147`
   → `:161`; `snippet_for` `models.py:874-885` → `:889-900`;
   `_truncate_at_word` `:888-904` → `:903-919`. `consolidate.py` was
   reworked (+220/−60 lines, B4): the seven `snippet_for` sites are now
   `:747, :749, :809, :811, :886, :888, :1031` (all still `max_chars=100`).
5. **Fact pack §2's pipeline map** (`search():2110`, scoring `2285-2407`,
   trim `2409-2412`) — all moved; symbols unchanged. `search()` is at
   `:2231`; `_hybrid_fuse` impl `:2192-2228`; `reciprocal_rank_fusion`
   `:1825-1858`; `_RRF_K_DEFAULT = 60` `:1784`.
6. **Fact-pack hazard 16 is RESOLVED**: `bbc7672` fixed the stale Episode
   scopes docstring (`models.py:660-671` now reads "stored exactly as
   passed — nothing defaults them from origin").
7. **Fact-pack hazard 18 got bigger**: `hit_to_dict` grew a field in Phase 3
   (`path_drift_claim_anchored_missing_paths`, B2a), and the payload-shape
   tests moved (`tests/test_server_v12_features.py` +271 lines). Re-check
   those pins before assuming audit/wire byte-stability. `audit.py` itself
   is untouched since `95af021` — the `hit.snippet` miss-trace coupling
   holds (`audit.py:269-271, :893`).
8. **New premise the fact pack does not contain**: `matched_leg` exists.
   `search()` grew a keyword param `matched_leg_out: dict[str,str] | None`
   (`search.py:2250`); the handler threads it (`handlers/search.py:847,
   :869`) and stamps `row["matched_leg"]` on wire dicts (`:886-889`,
   omitted when no leg). Section 3 lists every interaction.
9. **The relevance label rule is UNCHANGED.** The plan's "B3 shipped as
   matched_leg + a recut label" means matched_leg + the DESC/expand_top
   recut — NOT a new label function. The cosine-band recut closed as a
   measured negative (0 of 274 logged top hits had `matched_unique==0`;
   rationale in `_relevance_label`'s docstring, `search.py:1055-1102`).
   Labels are still v1 lexical coverage. C2/C3 premises unaffected.
10. **Untouched since `95af021`** (verified: the git diff over
    `bench/longmemeval/`, `bench/retrieval/` is empty): both benches, their
    READMEs, committed results, `index.py`, `audit.py`, `store.py`,
    `tests/test_search.py`, `tests/test_models_slug.py`,
    `tests/test_bench_longmemeval.py`, `tests/test_bench_retrieval.py`,
    `tests/test_server_search_mode.py`. Every fact-pack claim about THOSE
    files stands as written, line numbers included.

---

## 2. The four items, refreshed

Order inside the phase: **C2-prereq (solo, first) → C2; C3 after C2 in the
same lane; C5 harness parallel, final measurement last.** Rationale in
section 4.

### C2-prereq — per-question output + committed baseline (FIRST)

The longmemeval runner emits `by_type` aggregates only — no per-question
record exists anywhere. The `--json` emit is `main()`'s branch at
`bench/longmemeval/run.py:470-509`, `json.dumps` **printed to stdout**
(per-arm keys: `arm, n, seconds, items_written, rounds_offered,
dup_session_questions, macro, micro, ceiling, depth_truncated, by_type,
type_n`); text report otherwise (`:511`). Files in `results/` are
hand-saved redirections.

**Why the ordering is load-bearing:** the +3.2 rescue estimate
(`bench/longmemeval/README.md:308-310`, 89.3 → 92.5 pooled) came from a
one-off re-run whose partial/complete table is NOT reproducible from
`results/`. Without a per-question baseline committed against UNMODIFIED
ranking, C2's improvement cannot be verified question-by-question — and a
baseline captured after the re-ranker exists is measured on modified code
or requires a revert dance. Baseline artifacts must exist in git BEFORE the
re-ranker does. This is also the frozen-claims discipline: the published
numbers are compared against, never regenerated in place; new runs are NEW
dated artifacts.

Runner facts (all `run.py`, untouched since the fact pack):
`RETRIEVAL_DEPTH = 200` (`:123`); `K_VALUES = (1, 5, 10)` (`:113`);
`run_arm` (`:244-307`) builds a fresh per-question temp store and calls
`run_search` = `bettermemory.search.search` (import `:88`) with
`max_results=RETRIEVAL_DEPTH, mode="hybrid"` (`:270-276`); stores ~245
items — below the 500 threshold, prefilter never engages (`:453-458`).
Corpus: `bench/longmemeval/data/longmemeval_s_cleaned.json`, 277,383,467
bytes, sha256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`
— live-hashed at `bbc7672`, exact match against `KNOWN_CORPORA`
(`:99-106`). Oracle variant present and pinned unpublishable (`ORACLE_SHA`
`:111`, guard note `:405-410`).

`tests/test_bench_longmemeval.py` (untouched) pins METHOD/corpus
invariants only — no result numbers: checksum `:188-194`; 500 instances
`:197-199`; depth>k `:112-116`; runner k/depth parity `:119-126`;
round-pairing `:53-73`; session-id never retrievable `:165-171` — this
last one asserts two literal source substrings; **keep those lines intact
when editing `run.py`**.

AC: per-question output lands in the emit (or a sidecar file — executor's
call, but committed and documented in the bench README); a fresh both-arms
baseline run with per-question data is committed as a NEW dated artifact
BEFORE any ranking edit; existing artifacts untouched.

### C2 — read-side co-evidence rescue. BENCH-GATED.

**The plug-in point at `bbc7672`.** In `search()` (`search.py:2231`), the
hybrid branch ends:

- `:2577` — `scored = _hybrid_fuse(rankings, rrf_k=rrf_k)`. Returns
  `[(Memory, rrf_score, matched_union_sorted)]`, desc by
  `(score, created, id)` (`_hybrid_fuse` `:2192-2228`;
  `reciprocal_rank_fusion` `:1825-1858`; `_RRF_K_DEFAULT = 60` `:1784`).
- `:2579` — `trimmed = scored[:max_results]`. **Nothing sits between them;
  the rescue lands between `:2577` and `:2579`, inside the hybrid branch**
  (hybrid is the mode both benches run; the trim line itself is shared by
  all modes — keyword/bm25/semantic set `scored` and fall through).
- The rescue must preserve the `(Memory, float, list[str])` tuple shape.

**What B3 put around it (do not disturb):**

- `:2407-2408` — `lexical_ids` / `semantic_ids` sets, populated per
  mode-branch with the ids each leg SCORED, only when `matched_leg_out is
  not None`. In hybrid the lexical legs are snapshotted at `:2545-2548`
  **before** the semantic ranking is appended at `:2574` (a comment there
  warns against indexing `rankings` after the fact; semantic ids at
  `:2575-2576`). Keep that ordering.
- `:2580-2587` — the `matched_leg` emission loop reads **`trimmed`**
  (post-trim) and maps hit ids against the FULL leg id-sets via
  `_matched_leg` (`:1035-1052`; vocabulary `lexical/semantic/both`, `""`
  for browse). Because the sets cover the full leg rankings, a pre-trim
  rescue that promotes a deep hit gets its leg stamped correctly with zero
  extra work.
- `:2588-2591` — `_build_hit(memory, score, matched,
  query_unique=query_unique)` over `trimmed`.

**Interaction you must decide deliberately — rescued scores.** `expand_top`
now has a score-margin arm (`handlers/search.py:1031-1034`): it fires on
`relevance=="high"` OR `top_hit_leads_runner_up(out[0].score,
out[1].score)` (`search.py:1807-1822`; `EXPAND_TOP_SCORE_MARGIN` `:1804`,
derived from RRF spacing `(k+3)/(k+1)`). A rescue that reorders WITHOUT
recomputing scores breaks the desc-monotonicity of `scored`: rank 1 can
carry a lower score than rank 2 (the margin arm silently can't fire), or a
demoted near-tie can make it newly fire. Decide what score a rescued
ranking reports and write the decision down. Handler-side only — invisible
to both benches — but pinned by `tests/test_relevance_label.py` (`:242+`)
and `tests/test_server_v12_features.py`.

**Other consequences, accepted:** search events log per-hit arrays
`relevance`, `relevance_v2`, `matched_leg` (`handlers/search.py:1131-1143`)
— a reorder changes those logged series and silent-miss audit verdicts (the
probe shares `resolve_search_pool` and the same search path,
`handlers/audit_turn.py:167`). Fact-pack hazard 7 accepts this; the logged
surface is just bigger now.

**k=200 sanity is mandatory.** The bench calls `search()` directly with
`max_results=200`; `clamp_search_width` / `MAX_SEARCH_RESULTS = 50` are
handler-only (`handlers/search.py:470-486`). The rescue must be sane — in
behavior and in cost — at k=200, not just k=5.

**Gate numbers (baselines from the committed artifact
`bench/longmemeval/results/s-cleaned-both-arms.json` and
`bench/longmemeval/README.md:32-33, :93-94`; FROZEN — compare, never
regrade):**

| measure | baseline | gate |
|---|---|---|
| lexical pooled R@5 | **0.8935** | ≥ +2 pts (macro@5 ≥ 0.9135) |
| lexical temporal-reasoning @5 | **0.8372** | no regression > 1 pt |
| lexical multi-session @5 | **0.8487** | no regression > 1 pt (any class) |
| lexical arm runtime | **328.0 s** | ≤ ~1.2× (~394 s) |
| semantic pooled R@5 | **0.9185** | must not regress; no headroom prediction exists — report what it does |

The +3.2 estimate (`README.md:308-310`) was measured on the lexical arm
only. Re-run BOTH arms. **Negative result = success outcome: commit the
per-question artifacts + a README note, REVERT the ranking change, close
the item.** Per-question artifacts committed either way.

### C3 — query-biased snippets, Python-side, `_build_hit` only

**Design (fact pack §1, confirmed at `bbc7672`).** FTS5 `snippet()` stays a
dead end: the index stores preprocessed token streams only
(`index.py:195-226`, untouched); raw body is not indexed. The workable
shape: `_TOKEN_RE.finditer` over the RAW body (`_TOKEN_RE` `search.py:82`;
`_tokenize_impl` `:770` uses `findall` at `:780/:796` — positionless, do
not reuse it for offsets), normalize each raw token individually
(`_stem_token` `:706`; kebab handling in both directions — `_kebab_parts`
`:851`, index-side `_expand_kebab` `:802` means matched `python` may exist
only inside raw `python-frontmatter`), test membership against the hit's
`matched` list, pick the densest window, cut with word-boundary logic.
Never fold the whole body and reuse offsets (`_fold_ascii_safe` is
length-changing).

**Scope:** the snippet call inside `_build_hit` ONLY —
`snippet_for(memory.body)` at `search.py:1956`, blind head-of-body today.
`_build_hit` (`:1930-1978`) already receives `matched: list[str]` and uses
it only for `relevance` (`:1958`) and `match_terms` (`:1959`). In hybrid,
`matched` is the sorted matched-terms union across rankers (stemmed/folded,
deduped). `snippet_for` itself (`models.py:889-900`, `_truncate_at_word`
`:903-919`) is SHARED — consolidate summaries (7 sites, `max_chars=100`,
anchors in section 1 item 4), the audit silent-miss trace (`audit.py:893`,
rationale `:269-271`), SimilarHit dedup (`search.py:2964, :2982`). Do not
change it globally; bodies ≤200 chars must still return whole.

**Fallbacks:** head-of-body when `matched` is empty. Both empty-`matched`
populations still exist: paraphrase-only semantic hits (`_score_semantic`
literal-`matched` intersection, `:2156-2173`) and browse mode
(`_build_hit` call at `:2388`, `matched=[]`, `query_unique=0`).

**Pins to update deliberately, never bypass:**
`tests/test_search.py:493-497` (total length ≤ 203 — a mid-body window's
LEADING ellipsis must fit inside that budget) and `:500-513` (endswith
`"..."`, no mid-word cut); `tests/test_models_slug.py:125-152`
(newline-boundary backoff) — both files untouched, both will notice.
Audit-payload coupling: after the change the retained miss-trace snippet
varies per query; `hit_to_dict` serializes `snippet` at `_response.py:161`
and grew a field in Phase 3 — re-check the payload pins in
`tests/test_server_v12_features.py` before assuming byte-stability.

**Cost:** per EMITTED hit only (handler clamp 50; `_build_hit`'s stat
budget docstring `:1939-1943` — ≤8 stats per hit stands). Never run window
finding over the candidate pool. No DESC or `docs/api.md` text promises
head-of-body snippets (verified at `bbc7672`) — zero doc/DESC spend needed.

AC (plan, unchanged): a hit whose match is at char 4,000 shows the matching
window; fallbacks correct; snippet-shape and audit-payload tests updated
deliberately.

### C5 — above-threshold prefilter measurement (bench only)

Every published retrieval number is below the 500-memory prefilter
threshold. `--pad-to` does NOT measure it: `bench/retrieval/run.py`'s
`run_arm` (`:211-240`) calls `search.search` on
`memories = Store(root).load_all()` (`:331-332`) — the prefilter never
engages, even padded. The README names this gap verbatim
(`bench/retrieval/README.md:94-112`: padded run is an "upper bound…not a
simulation of production"; closing it "means driving the real handler
path").

**The real path to drive:** `load_search_candidates`
(`_handlers.py:143-323`) → `resolve_search_pool`
(`handlers/search.py:514-664`). Facts at `bbc7672`:

- `_INDEX_THRESHOLD_DEFAULT = 500` (`_handlers.py:116`); `_PREFILTER_CAP =
  50` (`:123`); `resolve_index_threshold()` reads
  `BETTERMEMORY_INDEX_THRESHOLD` per search (`:126-140`; invalid/≤0 →
  default) — the env-override-to-1 idiom is established in
  `tests/test_hook.py:1249, :2401`, `tests/test_store.py:1205`,
  `tests/test_server_search_index.py`, `tests/test_audit_sweep_round77.py:268`.
- `load_search_candidates` fallback ladder: empty query → `load_all`
  (`:215-216`); index status gates + `indexed_count < threshold` →
  `load_all` (`:224-228`); `_index.query(..., max_results=_PREFILTER_CAP)`
  (`:248-251`); index-read failure / zero candidates / all-ids-missed →
  `load_all` (`:255-273`, `:314-322`); saturation pinned from index row
  count pre-load (`:276`); per-id load with id-drift guard (`:286-313`).
- `resolve_search_pool`: starvation guard (`:621-640` — post-cap filters
  active AND saturated AND `len(survivors) < min_survivors` → full reload,
  `prefiltered=False`); corpus-IDF provider only when `prefiltered`
  (`:661-663`); `memory_search` passes clamped `max_results` as
  `min_survivors` (`:820-828`). The audit probe shares it
  (`handlers/audit_turn.py:167`).
- Store plumbing to copy: `build_store` (`run.py:168+`) writes a real
  on-disk Store; `Store.write` incrementally indexes via
  `_index_upsert_quietly` (`store.py:451`, untouched). `INDEX_THRESHOLD =
  500` duplicated at `run.py:87-91`, cross-pinned by
  `tests/test_bench_retrieval.py:170-175` — a threshold change trips it by
  design. Prefilter behavior: `tests/test_search_prefilter.py` (8 tests,
  incl. the min-survivors verdict-flip test cited at
  `handlers/search.py:584`).
- Existing artifacts to sit beside, not replace:
  `bench/retrieval/results/{unpadded,padded600,v2-unpadded,v2-padded600}-2026-07-26.json`.

**Measurement-only.** Measure prefilter recall loss above threshold through
the REAL handler path (a `Store` + built index +
`load_search_candidates`/`resolve_search_pool`, or the env override);
commit the finding as a NEW dated artifact; narrow or close the gap
paragraph in BOTH bench READMEs (`bench/retrieval/README.md:94-112`;
`bench/longmemeval/README.md:325-329` is the above-threshold item there).
Code changes to the prefilter are a separate decision — NOT Phase 4.

**Final measurement runs after `search.py` settles** — the artifact must
reflect shipped ranking code, or it measures a phantom.

AC: committed artifact answering "what does the prefilter cost above
threshold"; both READMEs' declared gap closed or narrowed; production code
untouched by this item.

---

## 3. Phase 3 → Phase 4 interactions (verified hands-on, exhaustive)

1. Fuse→trim is still empty — B3 inserted nothing between `:2577` and
   `:2579`. The insertion point is exactly as planned; everything around it
   is new.
2. `matched_leg` bookkeeping: leg id-sets snapshotted per-leg PRE-fuse;
   emission reads `trimmed`. A pre-trim rescue propagates legs
   automatically. Do not disturb the snapshot-lexical-before-semantic-append
   ordering (`:2541-2548` comment).
3. `expand_top` score-margin arm: a reorder without score recompute breaks
   desc-monotonicity — decide rescued scores deliberately (section 2, C2).
4. Search events log per-hit `relevance` / `relevance_v2` / `matched_leg`
   arrays; reorders change logged series and audit verdicts. Accepted
   (fact-pack hazard 7), surface is bigger now.
5. Relevance label rule UNCHANGED (v1 lexical coverage); B3's cosine recut
   closed as a measured negative. C2/C3 premises unaffected.
6. `_build_hit` grew claim-anchored drift plumbing (B2a: `:1977`, new
   `MemoryHit` field `models.py:505`, extended `hit_to_dict`). C3's
   snippet line is untouched but its neighbourhood moved; payload-shape
   tests moved (+271 lines in `test_server_v12_features.py`) — re-check
   before assuming wire byte-stability.
7. Fact-pack hazard 16 resolved in `bbc7672` (Episode scopes docstring).
8. Affirmative negative: all remaining Phase 2/3 deltas (consolidate/B4,
   symbols.py/B5, verify.py/B1-B2a, session.py/F4 sidecar, ingest F-items,
   proposals) were checked against C2/C3/C5's premises and ACs — **no other
   interaction**. None touch ranking, snippets, the trim, the prefilter, or
   either bench.

---

## 4. Sequencing and lane discipline (ultracode executor)

Learned the hard way in Phases 0–3; none of this is optional.

- **Prereq lane runs SOLO, first.** Baseline bench runs must execute
  against UNMODIFIED ranking, and the semantic arm's venv swap (embeddings
  extra) must not race any other lane's test runs.
- **C2 and C3 both edit `search.py` → ONE lane, sequential.** C2 before C3
  (C2 changes what `trimmed` contains; C3 reads it). C5's harness can build
  in parallel (it touches `bench/retrieval/` only), but its FINAL
  measurement runs after `search.py` settles — the artifact must reflect
  shipped code.
- **Adversarial verify lanes are mandatory and must run BOTH dependency
  legs.** Phase 3 shipped two embeddings-only test failures because every
  lane ran the default venv. Phase 2's first pass shipped two
  inert-or-unsafe features whose own tests passed — a lane's "all green"
  is a hypothesis until a verifier reproduces it.
- **Never stage while verifiers run** (they mutate-and-restore). Check
  `git status` for stray scratch files immediately before staging. New
  test files are invisible to the doc-claims corpus until staged; the
  walk-fallback trio failing on only-untracked files is expected, not a
  defect.
- **`pipeline([null], ...)` silently runs zero agents.** Confirm each lane
  actually dispatched before trusting its silence.
- Bench runs go OFF the critical path (full longmemeval both-arms ~27 min;
  never block a ship on an in-flight bench — gate the ITEM, not the
  release).

---

## 5. Environment

- **venv**: dev(+ui) extras only — the default leg. numpy/torch ship ONLY
  with the embeddings extras; 14 of the default leg's 16 skips are numpy
  `importorskip`s (3 `test_fsutil.py`, 1 `test_semantic_fastembed.py`,
  10 `test_semantic_persistence.py`) and are correct; the other 2 are the
  `BM_EVAL_LIVE` maintainer lane. Live-verified at `bbc7672`: `import
  numpy` fails in `.venv`, and a full default-leg run reproduces
  4,073 / 16 exactly.
- **Semantic arm needs a venv swap**: `uv sync --extra dev --extra
  embeddings` (CI's recipe, `ci.yml:149`); swap back (`uv sync --extra
  dev`) before any default-leg suite run — with numpy present the numpy
  `importorskip`s execute instead of skipping (skip count moves off 16)
  and the baseline comparison is invalid.
- **iCloud has corrupted `.venv` THREE times mid-session** (numpy, then
  fastapi — every file in the package renamed with a `" 2"` suffix — and a
  third, subtler variant while this brief was being shipped: the editable
  install's `.pth` file present and correct but silently unprocessed, so
  `import bettermemory` fails only outside pytest and the sole symptom is
  the 3 CLI-on-PATH tests quietly flipping from pass to skip, 16 → 19).
  It presents as inexplicable single-library failures (22 test failures the
  first time; 11 mypy + 6 pyright errors confined to `web.py` the second;
  a 3-skip drift the third). **Rebuild, never debug** — and treat ANY skip
  count other than 16 as a corrupted or wrong-legged venv, including drifts
  that look harmless.
- Corpus present: `bench/longmemeval/data/longmemeval_s_cleaned.json`
  (277,383,467 bytes ≈ 265 MB, sha-pinned, verified). Oracle variant
  present, unpublishable. The bench/rot clone cache is warm — irrelevant to
  Phase 4, noted so nobody re-clones 30 repos.
- Runtimes: lexical arm 328 s, semantic 1,286 s (~4×), both-arms ~27 min;
  `--limit 20` smoke exists. If the embeddings extra is not importable the
  semantic arm is SKIPPED with a note, not an error (`run.py:422-432`).
  bench/retrieval runs are minutes.

---

## 6. Guards that will fire (budget state live-measured at `bbc7672`)

- **`tests/test_number_claims.py` (A5)**: sweeps README + `docs/internals.md`
  + the 28 DESCs + instructions. Neither resident surface mentions
  longmemeval or its numbers (verified by grep) — so C2's re-run triggers
  no A5 obligations beyond bench-README self-consistency. Measured claims
  pin against `bench/*/results/*.json` (glob at test `:337`): **commit the
  new artifact in the same change as any README number.** C2/C5 result
  numbers belong in bench READMEs + artifacts, NEVER in DESCs.
- **`tests/test_resident_footprint.py` (E4)**: uncapped remainder
  (schemas + frontmatter) **9,699 / 10,000 → 301 slack**. Of that, **182
  chars are reserved for Phase 7's G1** (`include_bodies` 76 + `ids` 106;
  F5's 93 already spent as designed — `:124-168`). Pressure warning line is
  9,890; projected post-G1 remainder 9,881 — 9 chars under it. **Phase 4
  must add ZERO new wire params / schema bytes**; anything ≥10 chars trips
  the pressure warning once G1 lands. C2/C3/C5 are internal/bench-only —
  they need none.
- **DESC budget**: lean total **27,048 / 27,500 → 452 slack** (the plan's
  "1,164" is stale); instructions **1,608 / 1,700**. Phase 4 should spend
  ~zero — no DESC or `docs/api.md` text promises head-of-body snippets
  (verified). If a DESC edit somehow becomes necessary, re-measure
  `_DESC_BASELINE` in the same commit (per-tool table
  `tests/test_server.py:5973-5997`, re-measured in `bbc7672`, matches live).
- **Frozen-claims discipline**: published longmemeval numbers (89.3 / 91.8,
  the per-class rows, `README.md:291`'s "reproduces the published 83.7% /
  84.9% exactly") are baselines to compare against — never regrade, never
  regenerate in place. New results are NEW dated artifacts beside the old.
- **`tests/test_bench_longmemeval.py`**: method/corpus invariants only, no
  result numbers — but the session-id test asserts two literal source
  substrings in `run.py`; keep those lines intact when adding per-question
  output.

---

## 7. Ship discipline and release state

- Per ship: BOTH legs green locally (default + embeddings with the
  `-m "not no_extras and not no_torch_embeddings"` filter), ruff + mypy +
  pyright, commit, `git push origin main`, **WATCH the full CI matrix**
  (`gh run view --json conclusion` — a `null` conclusion means
  still-running, not failed), and only then any tag. Re-run
  `tests/test_changelog.py` between `git tag` and the tag push.
- **Release backlog**: version is **3.30.0** with FOUR phases (0–3) of
  Unreleased CHANGELOG accumulated. Cutting **3.31.0** either BEFORE Phase
  4 starts (a clean, all-green point at `bbc7672`) or at Phase 4's exit is
  the executor's call — per the runbook memory `01KS9M8D32343QVS70RJHF7V6A`
  (version bump spans five files, `server.json` carrying two fields, plus
  the CHANGELOG entry; `docs/release.md`). That memory currently shows
  `commit_drift` (11 commits since verify): spot-check it before use.
- **Phase 4 exit gates**: CHANGELOG entry in the same window; `episode_write`
  with shipped SHAs + CI run ids; the plan doc's Phase 4 section gets a
  PHASE STATUS block updated inline **in the same commit as the work**
  (match the Phase 0–3 house style: deltas from the plan as written, each
  verified against artifacts). Phases 3–4 additionally: bench artifacts
  committed and their pinning tests re-recorded deliberately.

---

## 8. Fact-pack hazard overlay (verdicts at `bbc7672`)

Read the fact pack's hazards, then apply this table. "CONFIRMED-MOVED" =
the hazard holds, its anchors moved to the locations herein.

| # | Verdict | Anchor at bbc7672 (re-locate by symbol) |
|---|---|---|
| 1 FTS5 snippet dead end | CONFIRMED | `index.py` untouched; preprocessed cols `:195-226` |
| 2 normalized offsets don't map | CONFIRMED | `_fold_ascii_safe`/`findall`, `search.py:770-796` |
| 3 matched terms stemmed/folded | CONFIRMED | `_stem_token` `:706`, `_expand_kebab` `:802`, `_kebab_parts` `:851` |
| 4 snippet_for shared | CONFIRMED-MOVED | consolidate `:747/:749/:809/:811/:886/:888/:1031`; `audit.py:269-271,:893`; SimilarHit `:2964/:2982` |
| 5 snippet shape pins | CONFIRMED | `test_search.py:493-497`; `test_models_slug.py:125-152` (both untouched) |
| 6 match_terms=[] hits exist | CONFIRMED-MOVED | paraphrase `:2156-2173`; browse `:2388` |
| 7 diversify in search(), not handler | CONFIRMED-MOVED | fuse `:2577` / trim `:2579`; benches call `search.search` (`bench/retrieval/run.py:231`, lme `run.py:270`); probe shares pool (`audit_turn.py:167`); extended by section 3 items 3–4 |
| 8 k=200 sanity | CONFIRMED | clamp handler-only (`handlers/search.py:470-486`); runner passes 200 |
| 9 per-hit cost budget | CONFIRMED | `_build_hit` docstring ≤8 stats `:1939-1943`; emitted-hit cap 50 |
| 10 --pad-to ≠ prefilter | CONFIRMED-MOVED | `run.py:231/:331-332`; `INDEX_THRESHOLD` `:91`; cross-pin `test_bench_retrieval.py:175` |
| 11 per-question data missing | CONFIRMED | by_type-only emit `run.py:470-509`, stdout |
| 12 DESC budget policed | CONFIRMED, numbers updated | slack **452** now; `test_prompts.py` pins survive at ~`:244-280` |
| 13 facade mirrors wire schema | CONFIRMED-MOVED | doc `_handlers.py:8-21`; `memory_search` facade `:391`, `episode_search` `:492` |
| 14–15 episodes | not re-verified | Phase 7 material, not Phase 4 — re-verify then |
| 16 stale Episode-scopes docstring | **RESOLVED** in bbc7672 | `models.py:660-671` now correct |
| 17 mode="semantic" config-dependent | CONFIRMED | `_semantic_model_configured` `semantic_setup.py:81`; contract test `test_server_search_mode.py:102-122` untouched |
| 18 snippets ride audit payloads | CONFIRMED, bigger | `audit.py` untouched; `hit_to_dict` grew a field in Phase 3 — re-check `test_server_v12_features.py` pins |

---

## 9. After Phase 4

The remaining sequence is unchanged from the plan: **Phase 5** (D1 → D2;
D3 parallel — settlement + hooks), then **Phase 7 BEFORE Phase 6**
(G1/G2/G3 episode work lands its DESC/schema deltas — including G1's
182-char reserve — before any ratchet), then **Phase 6 last** (E5 → E1 →
E3?, with the final `_DESC_BUDGET_CEILING` ratchet + toolcost re-run only
after every DESC delta has settled). E2 is a 4.0 note only. Phase 4's exit
episode should name Phase 5's entry point (D1: use-token expiry event —
plan's Phase 5 section) and whether 3.31.0 was cut, so the next session
starts as cleanly as this one.
