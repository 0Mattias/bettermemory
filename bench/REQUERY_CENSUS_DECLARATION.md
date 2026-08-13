# Requery census — two-pass feedback requery on the dev instrument, declared before any cell ran, 2026-08-13

The arc so far, cited rather than restated. The register/df census
(`bench/REGISTER_DF_CENSUS_DECLARATION.md`, artifact
`bench/retrieval/results/register-df-census-2026-08-12.json`)
measured where the vocabulary wall is: severe on this instrument's
as-asked probes, overwhelmingly made of vocabulary that exists
elsewhere in the ranked store, and bridged by the committed human
requeries through near-total vocabulary substitution. That census
attached no gate; this document is its first declared consumer. The
mechanism family it puts under census is the only one the forward
plan still holds live for as-asked headroom: an automatic requery.

**This is a mechanism that adapts query vocabulary to the store.**
The round 2-5 arc's closing obligation
(`bench/longmemeval/PREREGISTRATION.md`) binds any successor of that
class to say so in its first paragraph and to budget for a clean
third instrument before claiming held-out generalisation. Said, and
budgeted: this census is DEV-ONLY — LongMemEval is not read, not
run, and not touched; the successor preregistration (not this
document) carries the held-out ceremony, must cite the register
census and this one as prior knowledge, and inherits the
third-instrument budget line. Nothing here spends a sealed
instrument or a preregistration.

The parked mechanism this family must not relitigate: **RM3 as an
equal leg** (record in `bench/retrieval/README.md`), where feedback
vocabulary fused as a peer ranker lifted gold's cluster siblings as
much as gold and regressed as-asked recall@1. What survived that
kill is a shape constraint — expansion must be gated and
subordinate, never a peer ranker — and this family obeys it
structurally: no fusion leg is added, no vote is cast beside the
engine's. The mechanism is a SECOND FULL PASS of the unmodified
shipped engine over a rewritten query, fired only when the first
pass is weak by the engine's own shipped weakness signal. The
cluster failure mode does not vanish by restructuring, and §4's
reads are built to expose it rather than average over it.

The question, at its sharpest point: **can a requery built from
store-internal evidence alone — the first pass's own served
documents — recover a useful share of what the committed human
requeries recover, on the probes where the engine is weak?**
Everything below — the family, every definition, the reads, and the
licensing/parking criterion — is committed before a single census
number exists. The enforcement record is the sha ordering: this
commit, then the run commit. Nothing may be added to the family
afterwards.

## 1. The mechanism family, enumerated

**Pass 1 (baseline, shared by every cell).** The shipped default
engine over the committed dev store, unpadded, exactly as the
runner's lexical arm invokes it: `search(memories, q,
max_results=50, mode="hybrid", rescue_expansion=False)`. Gold ranks
are observed to depth 50; deeper is None.

**Engagement gate (shared by every cell).** Pass 2 fires for a
probe if and only if the pass-1 top hit's `match_terms` cover less
than the engine's own shipped rescue-coverage constant
(`search._RESCUE_COVERAGE_GATE`, imported, not copied) of the
probe's unique content tokens — the same arithmetic
`bench/df_census.py` derived from the engine, at the same constant,
with an empty pass-1 result engaging by definition. No new
threshold is invented anywhere in this census. The constant's dev
firing rate is already committed in the df census artifact and is
re-recorded here per probe.

**Pass 2 query construction.** From pass-1 evidence only:

- Feedback pool: the top `F` documents of pass 1 (fewer if fewer
  were served). Feedback vocabulary is their content tokens
  (`_memory_tokens(m).content`), minus the probe's own content
  tokens.
- Term score: (number of feedback documents containing the term) ×
  ln(N/df), df over the full ranked collection, N its size — both
  committed quantities of the register census's own `term_df`,
  imported. Ties break lexicographically. The top `M` terms are the
  ADDED set.
- Kept set, by the `kept` axis: `all` keeps every asked content
  token; `hooked` keeps only asked content tokens present in at
  least one feedback document (the tokens that demonstrably hooked
  something), falling back to `all` when that set is empty.
- The pass-2 query is the kept set followed by the added set,
  joined with spaces, re-entering the engine through its public
  tokenizer like any user query.

**Pass 2 (per cell).** The identical shipped-engine call over the
rewritten query. The cell's ranking for an engaged probe IS pass
2's — engaged probes take the requery outcome, unengaged probes
keep pass 1. No acceptance rule is applied in any cell: choosing
one tonight would be fitting it to tonight's outcomes. Both passes'
gold ranks enter the artifact per probe, so the successor
preregistration can fix an acceptance rule from committed evidence
— and §3's oracle diagnostic bounds what any such rule could buy.

**The axes, crossed completely — 8 cells, no more:**

| axis | values | what it is |
| --- | --- | --- |
| `kept` | `all`, `hooked` | whether the asked tokens are kept wholesale or pruned to the ones pass 1's evidence supports |
| `F` | 3, 5 | feedback depth — documents contributing vocabulary |
| `M` | 5, 10 | terms added to the query |

The primary cell is `all_f3_m5`, by a stated rule rather than by
prospects: the cell that changes the asked query least — every
asked token kept, the smallest declared feedback depth, the fewest
added terms. No axis has a committed prior record, so the
minimal-intervention cell is the null hypothesis's neighbour.

## 2. Instrument, probes, arms

The committed dev instrument only: the unpadded corpus through the
runner's own `build_store`, the committed questions through the
runner's own probe constructors, `asked` and `control` kinds. The
`requery` kind is the HUMAN ceiling; it is cited from committed
artifacts, not re-run, and no mechanism cell reads it. Gold is the
slug-mapped memory. The prefilter regime is out of scope: this
census ranks the full corpus in process, the pre-3.30 arm shape,
and says so in the artifact.

## 3. The reads — tabulation, no selection

- Per probe, per cell: engagement bit, pass-1 gold rank, pass-2
  gold rank (engaged probes; None past depth), the kept, dropped
  and added token lists (dev is committed text), and the count of
  added terms present in the gold document — the census's link back
  to the register census's `matched` class.
- Per cell, asked and control separately: n, engaged count,
  recall@1 and recall@5 for pass 1 (the baseline row, identical
  across cells, recorded once), for the cell (engaged probes take
  pass 2), and for the oracle-min diagnostic (each probe at the
  better of its two ranks) — the upper bound any acceptance rule
  could reach, gating nothing.
- Per cell: the same recalls restricted to the pass-1 far/absent
  pool (pass-1 rank 10+ or None, 0-indexed) — the pool where the
  register census located the vocabulary gap and where a requery
  must earn its keep — and to the pass-1 hit@1 pool, where the read
  is what engagement-gated replacement puts at risk. Cluster
  exposure, RM3's inherited risk, shows here: a cell that gains
  far/absent recall@5 but not recall@1 is surfacing the cluster,
  not the document.

## 4. The criterion, stated before any result

Committed reference points, cited: the lane-off as-asked baseline
(recall@1 0.35 on this instrument, `rebaseline-off-unpadded`) and
the committed human requery ceiling (0.80-0.85 across the arc's
artifacts). Bars are placed between them, before any number exists:

1. **License:** the primary cell's asked recall@1 is ≥ 0.50 AND its
   asked recall@5 is ≥ the pass-1 baseline's. Writing the successor
   preregistration is licensed — the document that carries the
   held-out ceremony, the acceptance-rule choice, the
   store-adaptation first paragraph, and the third-instrument
   budget. Half the human gap, from store evidence alone, without
   paying for it at depth, is worth a preregistration's effort.
2. **Anti-gate-shopping,** the standing verbatim rule: the primary
   fails but some other cell reaches ≥ 0.50 — that licenses at most
   a follow-up census declaration naming that cell as ITS primary.
   Eight cells are not eight hypotheses.
3. **Twitch:** no cell reaches 0.50 but some cell reaches ≥ 0.45
   asked recall@1. No license and no park; the recorded outcome is
   that feedback requery moves but does not clear, and the only
   permitted follow-up is a new declared-first census naming what
   would settle it.
4. **PARK:** every cell below 0.45. Two-pass feedback requery from
   pass-1 evidence is parked on this instrument by this document's
   own rule; the record is this document plus the artifact; the
   lane's remaining as-asked food is whatever the register census's
   requery decomposition licenses that this shape did not reach.

## 5. The constraint ledger

- DEV-ONLY: nothing under `bench/longmemeval/` or `bench/msc/` is
  read or run; nothing under `bench/heldout/` is opened. The
  census touches the committed dev files and the engine's public
  search surface — nothing else.
- No engine code, whatever the outcome. Nothing under `src/`
  changes; the mechanism prototype lives entirely in bench and
  invokes the engine's public entry point twice.
- No fusion, no weights in the engine's path, no acceptance rule:
  ranks are tabulated, and engaged probes take the second pass
  wholesale.
- Deterministic artifact: no randomness, lexicographic tie-breaks,
  sorted iteration, no wall-clock content beyond the provenance
  date. Two runs at the same commit produce the same bytes.
- No changelog entry. Census commits ship none: no user-facing
  surface changes.

## Declared confounds

1. **The cluster risk is inherited, not escaped.** RM3's kill
   mechanism — feedback vocabulary is cluster-level on a corpus of
   near-duplicate distractors — applies to any feedback-built
   vocabulary, including this one. The far/absent-pool recall@1
   read exists precisely to catch a cell that finds the
   neighbourhood and not the document; a @5-without-@1 pattern
   there is the failure, named in advance.
2. **Twenty questions, one technical register.** The register
   census showed this instrument's wall is register-specific, so
   nothing measured here transfers to conversational stores by
   default; the successor preregistration owns that burden, on the
   instruments built for it.
3. **The engagement gate is reused, not designed.** The shipped
   rescue-coverage constant was chosen for a different mechanism;
   it is used because it is committed, shipped, and not fitted to
   this census. Probes it declines to engage are losses this
   family accepts silently, and the per-probe engagement bits
   record how many.
4. **The oracle-min diagnostic is a bound, not a mechanism.** Any
   real acceptance rule pays a selection cost the oracle does not;
   the successor document that picks one must predict that cost,
   not assume the bound.
5. **`hooked` prunes by pass-1 evidence,** so on a probe whose
   only matching token hooked a distractor, pruning can delete a
   hook the gold needed — the round-2 warning about stripping
   query tokens, returning in feedback form. The kept-token lists
   in the artifact make the damage countable.
