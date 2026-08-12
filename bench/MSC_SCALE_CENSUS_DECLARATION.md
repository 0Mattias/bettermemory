# MSC scale census — declared in full before any cell was run, 2026-08-12

The arc so far, cited rather than restated. The dense scoring census
(`bench/DENSE_SCORING_CENSUS_DECLARATION.md`, artifact
`bench/retrieval/results/dense-scoring-census-2026-08-12.json`) fixed
an 8-cell family and a reach criterion first, ran, and PARKED
store-trained dense scoring at its own rule: no cell of the family
reached the declared bar on the dev instrument's far/absent pool. The
margins live in that artifact, cited rather than restated, exactly as
this document treats every number it did not itself produce. That
verdict was measured on the dev instrument's technical corpus; its
conversational read was a twenty-question LongMemEval glance, disclosed
as underpowered and gating nothing.

This census moves the corpus constraint, which is the one lever the
park left standing. MSC (`bench/msc/README.md`) is conversational
register at store scale: recurring speaker pairs, personal facts
stated and referenced across five-session episodes, casual paraphrase
throughout. The owner's 2026-08-12 decision authorized it as a
deliberately non-committed corpus — pinned fetch, gitignored payload,
artifacts reproducible only for a holder of the same bytes — and the
machinery (loader, synthetic dates, episode and aggregate stores,
corpus fingerprint, trainer plumbing) landed with that decision. What
was deliberately NOT built then was this document's mechanical half:
`bench/msc/README.md` names the gate — no census without its own
declared-first document. This is that document.

The question, at its sharpest point: **does store-trained dense
retrieval work at conversational store scale — or does the wall that
parked it at technical store scale hold there too?** Everything below
— the stores, the probe construction, the arms, every read, and the
licensing/parking criterion — is committed before a single census
number exists. The enforcement record is the sha ordering: this
commit, then the run commit. Nothing may be added to the family
afterwards.

## 1. Corpus, stores, scales

Corpus: the MSC test split, read exclusively through
`bench/msc/load.py`, whose tarball pin refuses an unverified download
and whose `corpus_fingerprint` enters the artifact. Store bodies are
the loader's verbatim shape (synthetic bracket date plus speaker
rounds); session keys stay out of bodies and scopes, where they would
be retrievable content.

Three store shapes, all mechanical prefixes of file order — **no
episode selection**:

| store | construction | what it is |
| --- | --- | --- |
| `E1` | each of the first 40 episodes alone, `build_episode_store` | the small-scale conversational anchor: five sessions of one speaker pair per store |
| `A40` | the first 40 episodes in one collection, `build_aggregate_store` | the store-scale shape the machinery commit established (the smoke's own aggregate) |
| `A160` | the first 160 episodes in one collection, `build_aggregate_store` | the mass axis, in register: four times the text and four times the distractors |

Mixing disjoint speaker pairs in one aggregate is the disclosed store
property the loader records; every read below inherits it.

## 2. Probes — dataset-native annotation, no authored gold

A retrieval census is its gold labels, and this bench does not author
them. MSC ships its own: `msc_personasummary/session_{2,3,4}/test.txt`
carries, for every test episode and each of sessions 2, 3, and 4, the
session's dialogue with per-turn annotator-written persona lines
(`agg_persona_list`) — first-person restatements of facts grounded in
that turn. The probe rules, fixed now:

- A probe is one persona line, whitespace-stripped; empty lines are
  dropped. Within one (episode, session) the union over turns is
  deduplicated by exact string.
- A line annotated to more than one session of the same episode is
  dropped for that episode entirely — the annotation itself marks the
  gold ambiguous — and the drop is counted in the artifact.
- The gold is the annotated session, as the store's key for it
  (`s<k>` in `E1`, `<episode_id>/s<k>` in aggregates).
- Sessions 1 and 5 have no per-turn annotation file, so they
  contribute no probes; they remain full distractor mass in every
  store. Disclosed, not discovered.
- Alignment is a hard gate, not an assumption: for every (episode,
  session) contributing probes, the annotation row's turn texts must
  equal the loader session's turns exactly; any mismatch aborts the
  run with no artifact. A spot-check of this equality across the full
  test split preceded this declaration; the run re-asserts it.
- The artifact contains **no corpus text**. A probe appears as
  (episode id, session index, first 16 hex of the line's sha256), and
  one `probe_set_sha256` over the sorted identity triples lets two
  holders of the bytes confirm they scored the same probes.
- Pool sizes are whatever these rules yield; nothing below adjusts
  them.

Probes for a store are exactly the probes of the episodes it holds:
`E1` and `A40` share one probe set (the first 40 episodes'), `A160`
holds a superset.

## 3. The arms

**Lexical (the stratum source).** The shipped engine, invoked
exactly as `bench/longmemeval/run.py` invokes it for the default arm:
`search(memories, probe, max_results=200, mode="hybrid",
rescue_expansion=False)`, the item ranking collapsed to distinct
sessions first-occurrence-wins by the runner's own collapse. Runs at
`E1` and `A40`. A probe's stratum is its gold's 0-indexed collapsed
rank: `hit@1` (0), `near` (1-4), `mid` (5-9), `far` (10+), `absent`
(gold not served within the depth).

**Dense (the family under test).** Per-store self-trained GloVe —
the WaC product shape, trained on the collection it ranks: store
bodies through `bench/embed_train.py`'s own pipeline
(`token_streams`, `build_vocab`, `cooccurrence` filtered at
`MIN_COOC`, `train`) at the trainer's declared constants (`DIM`,
`EPOCHS`, its fixed seed), nothing swept. Scoring definitions are the
dense census declaration's §1, verbatim: query tokens from the
engine's own pipeline; document tokens from the engine's content
reader; pooled weighted mean, L2-normalised, dot-product score;
out-of-vocabulary query tokens bridged when the bridging axis is on
and dropped otherwise; out-of-vocabulary document tokens always
dropped; a document, gold, or query that pools to nothing is excluded
and counted, and an unpooled gold or query fails every reach test.
The dense item ranking collapses to sessions with the same
first-occurrence collapse as the lexical arm; item ties break on the
sha256 of the item body, a content-derived key that repeats across
runs.

**Cells.** At `A40`, the dense census's full 8-cell family, names
verbatim: `pooling` (`mean`, `idf`) × `postproc` (`raw`, `centred`) ×
`bridging` (off, on). At `E1` and `A160`, the primary cell only. The
primary cell is `mean_centred_bridge`, carried unchanged from the
dense census's own stated selection rule — it is not re-chosen here,
and this census sweeps nothing.

## 4. The reads — tabulation, no selection

- `A40`, per cell: every probe's 1-indexed dense gold-session rank
  (None-capable). Reach is rank ≤ 10. The primary read is the reach
  share on the far/absent pool; also tabulated: median and quartiles
  per stratum, the hit@1-pool median with None counted as +inf (the
  R2 read), the unconditional reach share over all probes (the
  cross-scale read), and pooling diagnostics (unpooled documents,
  bridged and dropped query tokens).
- `E1`, primary cell, per probe: the gold session's dense rank among
  its episode's five sessions; reported as the share ranked first and
  the median rank, lexical alongside. Chance for ranked-first is one
  in five. This is the register anchor — if dense cannot order five
  sessions of one speaker pair, register alone kills it before scale
  enters. Gates nothing.
- `A160`, primary cell: unconditional reach share and median rank
  over its probe set, with the store's session count recorded so each
  scale's share is read against its own collection size. Gates
  nothing; it is the mass trend.
- Vocabulary-overlap diagnostic, every probe: the share of the
  probe's query tokens present in the gold session's content tokens,
  engine tokenizer both sides, tabulated by stratum. It explains
  where the wall is — vocabulary gap or ranking failure — and gates
  nothing.

## 5. The criterion, stated before any result

Verdicts read the `A40` far/absent pool and nothing else. Floor: the
pool must hold at least 20 probes for any verdict; below that the
census records shape only. Given the corpus mass this floor is a
formality, and it is stated so it cannot be decided later.

The outcome is decided by the first matching rule, top to bottom:

1. **R1 (licensed):** the primary cell's far/absent reach share is
   ≥ 0.50. Writing a Track-B-shaped preregistration for conversational
   store scale is licensed. R2 then routes it, exactly as the dense
   census declared: R2 holds when the primary cell's hit@1-pool
   median (None as +inf) is ≤ 10; with R1 and R2 the preregistration
   may propose a dense leg or a rerank window, with R1 alone the
   rerank-window shape only — dense opinion may reorder lexical
   candidates but never remove one.
2. **Anti-gate-shopping,** the dense census's verbatim rule: the
   primary cell fails R1 but some non-primary cell reaches ≥ 0.50.
   That licenses at most a follow-up census declaration naming that
   cell as ITS primary. Eight cells are not eight hypotheses;
   non-primary cells report the family's shape and are not eligible
   to carry the verdict.
3. **Twitch:** no cell reaches ≥ 0.50 but some cell reaches ≥ 0.25.
   No license and no park. The recorded outcome is that the geometry
   moves at conversational scale but does not work; the only
   permitted follow-up is a new declared-first census naming what
   would settle it, and nothing relitigates this one.
4. **PARK:** every cell's far/absent reach share is < 0.25.
   Store-trained dense retrieval is dead at conversational store
   scale as it is at technical store scale, by this document's own
   rule. Door D closes negatively; the record is this document plus
   the run artifact; the campaign's remaining dense question is the
   pretrained-weights doctrine door, which no result here opens or
   closes.

## 6. The constraint ledger

- Both sealed instruments stay sealed; nothing under `bench/heldout/`
  is opened; the dev instrument under `bench/retrieval/` is not read.
  This census touches MSC bytes, the engine's public search path, and
  the trainer's pipeline — nothing else.
- No engine code, whatever the outcome. Nothing under `src/` changes;
  the census imports the engine's tokenizer, token reader, and search
  entry point, and alters nothing the product serves.
- Statistics only: no fusion, no weights, no thresholds, no engine
  integration. Ranks are tabulated, never served.
- The corpus stays uncommitted and the artifact carries no corpus
  text — ids, session indices, hashes, counts, ranks, and shares
  only. It records the tarball pin, the split fingerprint, the sha256
  of each annotation file read, and the probe-set digest, and it
  reproduces only for a holder of the same bytes — the caveat every
  LongMemEval-derived artifact in this bench already carries.
- No `SOURCES` registration. Training reaches MSC through the
  pipeline functions over store bodies — the documented path the
  machinery commit established — and `embed_train.py`'s `SOURCES`
  stays what it is: committed, licence-stated text only.
- Deterministic artifact: the trainer's fixed seed, sorted iteration,
  content-derived tie-breaks, no wall-clock content beyond the
  provenance date. Two runs at the same commit over the same corpus
  bytes produce the same artifact bytes.
- No changelog entry. Census commits ship none: no user-facing
  surface changes.

## Declared confounds

1. **Probe register.** Persona summary lines are first-person
   annotator restatements, not user queries. They are the probe set
   because their gold is dataset-native — the alternative, authoring
   probes here, is the self-contamination `bench/THIRD_INSTRUMENT.md`
   rejects and no census may commit. Their paraphrase distance from
   the dialogue is whatever MSC's annotators produced; the overlap
   diagnostic measures it rather than assuming it.
2. **Gold uniqueness.** A fact restated in a session the annotators
   did not mark makes that session a defensible answer this read
   counts as a miss. That noise depresses lexical and dense alike,
   and the strata comparison survives it; the multi-session drop rule
   removes exactly the cases the annotation itself flags. In
   aggregates, a generic line may also genuinely fit another speaker
   pair's session — that ambiguity IS distractor mass, part of the
   scale question, and removing it would require authored labels.
3. **Mass and difficulty move together.** In the WaC shape a bigger
   store is more training text AND more distractors; `E1`, `A40`, and
   `A160` read the joint effect, and no cell claims to isolate one
   factor.
4. **Chance differs across scales** — five candidate sessions in
   `E1`, two hundred in `A40`, eight hundred in `A160`. Shares are
   reported against recorded session counts, and the cross-scale
   comparison is directional, not chance-adjusted.
5. **The reach bar is generous by construction.** Rank ≤ 10 over an
   `A40`-sized collection is far looser than production's fusion
   window, as the dense census already stated for its own bar: the
   census measures whether the geometry points at the right session
   at all, and the gap to production shapes is the preregistration's
   problem, stated here so it cannot be discovered later.
