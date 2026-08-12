# MSC — the conversational corpus at scale, data not committed

MSC (Multi-Session Chat) is the corpus `bench/THIRD_INSTRUMENT.md`
identified as the right shape for both of the campaign's open data
questions: multi-session dialogues between recurring speaker pairs,
personal facts stated and referenced across sessions, casual register,
and recorded time gaps. Its code (ParlAI) is MIT; its DATA tarball
carries no license statement at its distribution point, which is why
nothing under `bench/msc/data/` is committed and why this directory
follows the pattern `bench/longmemeval/data/` established — documented
fetch, pinned sha256, gitignored payload, and any derived artifact
stating that it reproduces only for a holder of the same bytes.

## The owner decision this directory records

2026-08-12: the redistribution email — option 1 of
`bench/THIRD_INSTRUMENT.md`'s unblock list, and still the only path to
a COMMITTED third instrument — is deferred, not sent. Option 2 (a
deliberately non-committed corpus) is authorized for MSC, which is
exactly the decision that note said a bench author must not assume on
their own. Requirement 3 of the third-instrument spec is therefore
knowingly broken for local work, on record, by the owner. Everything
downstream of the data is built and ready; the grant remains the gate
for committing any MSC-derived subsample or instrument into the repo.

## Fetch, once

```
mkdir -p bench/msc/data && cd bench/msc/data
curl -LO https://parl.ai/downloads/msc/msc_v0.1.tar.gz
tar -xzf msc_v0.1.tar.gz
```

`bench/msc/load.py` verifies the tarball against its pinned sha256 on
every use and refuses an unpinned corpus outright — a mismatched
download produces numbers comparable to nothing, so it produces none.

## What is built

`load.py` reads `session_5/<split>.txt`, whose rows carry complete
five-session episodes (`previous_dialogs` holds the earlier sessions
with the gap recorded after each; `dialog` is the final session), so
the whole chain arrives without cross-file joining. The test split
holds 501 episodes of five sessions each.

- Synthetic dates: each episode's final session is anchored at a
  fixed epoch (`EPOCH`, part of every derived store's bytes) and
  earlier sessions step backwards through the recorded gaps,
  formatted exactly like the LongMemEval runner's bracket prefix so
  both conversational benches write the same body shape. The dates
  order sessions and feed recency features; they claim nothing about
  when MSC was collected.
- Rounds: alternating turns pair into `Speaker 1 / Speaker 2` rounds,
  trailing unpaired turn kept, mirroring `rounds_of` in the
  LongMemEval runner.
- Stores: `build_episode_store` (one episode, session keys `s<n>`)
  and `build_aggregate_store` (many episodes in one collection,
  session keys `<episode_id>/s<n>`) — the second is the store-scale
  shape, and mixing speaker pairs in one collection is a disclosed
  property any census over it must state, not an accident.
- `corpus_fingerprint(split)`: the sha256 any derived artifact
  records, over the exact file a run read.
- `--smoke`: loads, builds both store shapes, trains the store model
  with `bench/embed_train.py`'s own pipeline over a 40-episode
  aggregate, and scores one probe mechanically. Counts and timings
  only, no verdict.

Pins live in `tests/test_msc_loader.py`: the construction functions
are tested unconditionally (they are what a future census's
determinism rests on), and everything touching the data skips when
the download is absent — which is every CI run.

## What is deliberately NOT here

- **The census happened — through the gate, not around it.** The
  scale question ("does store-trained dense retrieval work at a
  thousand-item conversational store, given that it fails at a
  180-document technical one?") was put under test on 2026-08-12,
  declaration first: `bench/MSC_SCALE_CENSUS_DECLARATION.md`
  (committed `04a1907`), then `bench/msc_scale_census.py` composing
  exactly the pieces this directory built, artifact
  `bench/retrieval/results/msc-scale-census-2026-08-12.json` (run
  `7247c8e`). The answer is no — park, family-wide, at every scale,
  by the declaration's own ladder; the record lives in
  `bench/retrieval/README.md`.
- **No gold labels, no instrument.** MSC text plus self-authored
  questions would be the self-contamination
  `bench/THIRD_INSTRUMENT.md` rejects. An instrument over MSC gets
  authored under the blind protocol that produced instruments #1 and
  #2 (campaign-blind author, content-free driver, no-read
  attestation, sha ordering) — and stays uncommitted until the
  redistribution grant exists.
- **No training-source registration.** `embed_train.py`'s `SOURCES`
  deliberately does not gain an `msc` corpus: that table enumerates
  committed, licence-stated text, and this data is neither. A future
  declared census that trains on MSC does so through its own
  documented path, the way the `lme` diagnostic arm already works.
