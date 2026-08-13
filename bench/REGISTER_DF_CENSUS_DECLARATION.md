# Register/df census — the vocabulary gap, measured across three corpora, declared before any read ran, 2026-08-12

The arc so far, cited rather than restated. Three censuses parked
store-trained dense retrieval in every register-by-scale quadrant
measured (`bench/DENSE_SCORING_CENSUS_DECLARATION.md`,
`bench/MSC_SCALE_CENSUS_DECLARATION.md`, records in
`bench/retrieval/README.md`); the mechanism family still alive for
as-asked headroom is requery/vocabulary — the dev instrument's own
requery probes clear what its asked probes miss, a lift the committed
artifacts carry and this document does not restate. Before any requery
mechanism is authored, the campaign needs the quantity that mechanism
would live on: where, exactly, is the vocabulary wall in each corpus
it would have to cross?

The question, at its sharpest point: **of the content vocabulary an
as-asked probe carries, how much does its gold actually contain — and
of the missing share, how much exists elsewhere in the ranked
collection versus nowhere in it at all?** The split is the census's
whole point. A gap token present elsewhere in the store is vocabulary
some store-internal bridge (requery synthesis from store text, PPMI
neighbours, co-occurrence) could in principle reach; a gap token
absent from the store entirely is priceable only by knowledge the
store does not contain — external weights (door C) or the caller. One
census, three corpora, one shared ruler: the technical dev instrument,
the conversational held-out instrument, and the conversational
at-scale corpus. It feeds two consumers that must not be conflated:
the requery mechanism's design (which gets its own declared-first
document with its own bars) and the door C decision brief (the
owner's, not a measurement's).

Everything below — the corpora, the stores, the probe sets, every
definition, every read, and what the artifact may and may not contain
— is committed before a single number exists. The enforcement record
is the sha ordering: this commit, then the run commit. Nothing may be
added afterwards.

## 1. Corpora, stores, probes — all through committed machinery

No new corpus access path is built for this census; every byte is
read through the loaders the record already trusts.

| corpus | store | probes | gold documents |
| --- | --- | --- | --- |
| dev (`bench/retrieval/`) | the committed corpus, built unpadded by `run.py`'s own `build_store` | each committed question's three probe strings, built by the runner's own constructors (`asked` verbatim, `requery` verbatim, `control` via `strip_question_words`) | the slug-mapped memory, one per question |
| LongMemEval (`bench/longmemeval/`) | one store per instance, `run.py`'s own `build_question_store`, over `DEFAULT_CORPUS` (the gitignored cleaned S file, fingerprint recorded) | the instance's question string, as asked; instances whose `answer_session_ids` is empty (abstentions) contribute no probe and are counted | every item of every answer session, by the builder's own id-to-session map |
| MSC (`bench/msc/`) | the `A40` aggregate: the first forty episodes of the test split in file order, `build_aggregate_store` — the scale census's store-scale shape, its episode-count constant imported, not copied | the scale census's declared probe rules, executed by its own committed `build_probes` (alignment gate included) over the full split, restricted to the episodes the store holds | every item of the annotated session, by the builder's own id-to-session map |

Probe identity in the artifact: dev probes by (slug, probe kind);
LongMemEval probes by `question_id`; MSC probes by the scale census's
(episode id, session index, line-sha16) triple and its
`probe_set_sha256` over the restricted set. Pool sizes are whatever
these rules yield; nothing below adjusts them.

## 2. Definitions — engine-derived, verbatim

- **Query content tokens**: the engine's own pipeline,
  `sorted(set(_strip_stopwords(_expand_kebab(tokenize(q)))))` — the
  scale census's `_query_tokens`, imported. A probe whose content
  token set is empty is excluded from every aggregate and counted.
- **Document content tokens**: `set(_memory_tokens(m).content)` —
  the stream the BM25 legs score against, the same set the df census
  counted df over.
- **Gold token set**: the union of document content tokens over the
  probe's gold documents.
- **df**: for each query content token, the count of documents in
  the ranked collection whose content token set contains it, one
  pass per store; `df_ratio` is df over the collection size.
- **Token classes**, exhaustive and disjoint: `matched` (in the gold
  token set), `gap-elsewhere` (not in gold, df > 0), `gap-absent`
  (not in gold, df = 0).
- **Overlap share**: matched count over query content token count.
- **Register margins**, probe side: raw token count
  (`tokenize(q)`, repeats kept), content token count (the set
  above), and question-word count — raw tokens found in the dev
  runner's committed `_QUESTION_WORDS` lexicon, imported so the same
  fixed list rides every corpus.
- **Band histogram**: `df_census.py`'s own `_BANDS`, imported — the
  shared ruler that makes the three corpora's df structure
  comparable — with each class's tokens binned by `df_ratio`.

## 3. The reads — tabulation, no selection

This census runs **no retrieval anywhere**: no `search()` call, no
ranking, no recall, no stratum. It reads token sets and counts
documents. The only labels it touches are the gold identities each
instrument already declares (dev slugs, LongMemEval
`answer_session_ids`, MSC's annotated session), used solely to name
the gold token set.

- Per probe: raw/content/question-word counts, overlap share, count
  per class, band histogram per class, and the median and maximum
  `df_ratio` of `matched` and of `gap-elsewhere` tokens.
- Per (corpus, probe kind) aggregate: probe count, excluded counts,
  the overlap-share distribution (min, quartiles, p90, max, mean),
  pooled class shares over all tokens, pooled band histograms per
  class, and store shape (collection sizes, distinct vocabulary,
  summed distinct-per-document token mass; for LongMemEval, the
  per-instance store sizes summarised).
- Dev only — the requery decomposition, per question and pooled:
  the asked and requery content token sets split into kept, dropped
  (asked-only), and introduced (requery-only); for introduced
  tokens, the class each lands in against the same gold and df; for
  dropped tokens, the class each had. This is the only probe set
  with a paired human requery, and the decomposition is the design
  target: it records what the successful second attempt actually
  did — added gold vocabulary, added store vocabulary, or shed
  noise — without this census claiming any of it transfers.
- Token strings appear in dev records only (the dev corpus is
  committed text). LongMemEval and MSC records carry counts, bands,
  and identities — **no corpus text**, exactly the discipline every
  artifact over those corpora already keeps.

## 4. No criterion — and why that is the discipline here

This census licenses nothing, parks nothing, gates nothing, and
carries no predictions. It is corpus structure measured before any
requery mechanism exists, so that the mechanism's own declared-first
document can fix its gates from structure rather than fit them to
outcomes — the same lineage as the df census that preceded round 2
(`bench/longmemeval/PREREGISTRATION.md`, addendum 4), which is the
worked precedent for declaring a statistics-only read of the held-out
corpus before the round that uses it. The obligations this document
does create:

1. Any future requery preregistration that validates on LongMemEval
   must cite this census as prior knowledge, exactly as addendum 4
   disclosed its census to Gate 0's reader.
2. The door C brief may cite the `gap-absent` shares as the ceiling
   decomposition they are; it may not present them as a mechanism
   forecast in either direction.
3. Nothing in this artifact relitigates any parked lane.

## 5. The constraint ledger

- Both sealed instruments stay sealed; nothing under `bench/heldout/`
  is opened. The census touches the committed dev files, the
  gitignored LongMemEval corpus, MSC bytes through the pinned
  loader, and the engine's public tokenizer/token-reader surface —
  nothing else.
- No engine code, whatever the numbers say. Nothing under `src/`
  changes; the census imports and alters nothing the product serves.
- Statistics only: no ranking is computed, so no ranking outcome can
  be selected on. The script contains no call into `search()`.
- The artifact records the dev corpus sha, the LongMemEval corpus
  sha, the MSC tarball pin, split fingerprint and annotation-file
  shas (all via the loaders' own fingerprint functions), and the
  restricted probe-set digest — and it reproduces, for the two
  uncommitted corpora, only for a holder of the same bytes, the
  caveat every artifact over them already carries.
- Deterministic artifact: no randomness anywhere, sorted iteration,
  no wall-clock content beyond the provenance date. Two runs at the
  same commit over the same bytes produce the same bytes.
- No changelog entry. Census commits ship none: no user-facing
  surface changes.

## Declared confounds

1. **Probe provenance differs by corpus.** Dev asked probes are
   developer-authored questions, LongMemEval probes are
   benchmark-authored questions, MSC probes are annotator persona
   restatements (the scale census's confound, inherited). The
   cross-corpus comparison rides those three provenances; the
   register margins measure the difference rather than assume it
   away, and no read below claims the corpora are exchangeable.
2. **Gold uniqueness is noisy at the margin** (inherited from the
   scale census): a fact restated outside the declared gold makes a
   `gap-elsewhere` token out of what a defensible alternate gold
   would call `matched`. The class shares are read as aggregates;
   per-probe records are shape.
3. **Collections differ in size by orders of magnitude**, so df
   structure is compared as `df_ratio` against each store's own
   recorded size, on the shared bands — never as raw counts.
4. **Union gold is generous.** A probe whose tokens are scattered
   across many gold documents counts as covered vocabulary even
   though a ranker must still concentrate score on single documents.
   Overlap here upper-bounds what lexical matching could see; it
   does not predict rank.
5. **`gap-elsewhere` is reachability, not achievability.** A token
   present elsewhere in the store is bridgeable only if some
   store-internal signal actually links it toward gold vocabulary;
   the split decomposes the ceiling, and whether any mechanism
   attains it is precisely what this census refuses to guess.
6. **The requery decomposition describes twenty human second
   attempts** on one technical corpus. It is design input, not a
   distribution over requeries; any mechanism generalising from it
   carries that risk in its own document.
