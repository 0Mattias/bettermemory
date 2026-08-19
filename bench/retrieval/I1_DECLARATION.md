# I1 — the dev instrument expansion: unit declaration, 2026-08-18

The dev instrument holds twenty questions. `bench/POWER_AUDIT.md`
prices what that costs: a forty-point Wilson interval at recall@1, a
paired resolution floor of six questions moving one way (a thirty-point
swing) before anything reaches alpha=0.05, and therefore not one
measurable dev cell in the whole campaign. Every bar written five
points above an incumbent — G1 at 60 against 55, twice — was written
below the noise floor of the instrument that grades it.

This unit grows the instrument until its bars are resolvable. It
trains nothing, ranks nothing new, and changes no engine behaviour.

## 1. Baselines this unit is judged against — committed, quoted

The instrument as it stands, at `bench/retrieval/corpus.jsonl`
(sha256 `c40acee9…`, 180 documents) and
`bench/retrieval/questions.jsonl` (20 questions):

- expansion off, asked: recall@1 35%, recall@5 60%
- static hand tables, asked: recall@1 55%, recall@5 90%
- requery, expansion off: recall@1 80%, recall@5 100%

These are the integrity anchors. **The expanded instrument must
reproduce every one of them on the original twenty questions**, which
is the whole reason the original twenty are retained verbatim rather
than regenerated (§3).

## 2. Why the corpus has to grow with the questions

The obvious move — add questions to `questions.jsonl` — is barred by
the instrument's own structure, and the tests say so:

- `test_every_gold_document_has_exactly_one_question` — questions and
  gold documents are one-to-one.
- `test_corpus_is_large_enough_for_retrieval_to_be_nontrivial` — the
  corpus carries exactly 20 gold documents in a field of ≥150.
- `test_every_gold_topic_has_near_duplicate_competition` — every gold
  topic needs at least five near-duplicates, or retrieval is trivially
  easy for it.

So a question needs a gold document, and a gold document needs five or
more near-duplicate distractors that are *not* secretly gold
(`test_no_distractor_is_secretly_a_gold_topic`). The instrument is a
structure, not a list, and expanding it means authoring topic families,
not sentences.

**Target: 120 questions over 120 gold topics, in a corpus of ~900–1,100
documents.** At n=120 the six-question resolution floor is five points
instead of thirty, and a 55%-vs-70% difference becomes resolvable at
80% power (n=163 would be needed for that at the two-proportion bound;
the paired design does better, and 120 is where the floor crosses the
size of effect the campaign actually argues about).

## 3. The authoring rule — the original twenty are frozen

1. **The existing 20 questions and their 20 gold topics are carried
   verbatim**, slug for slug, byte for byte. Their distractor families
   are carried unchanged. This is what makes every published figure on
   this instrument still checkable: the expanded instrument reports the
   original-twenty subset as its own cell alongside the full 120.
2. **New topics are authored blind to the engine.** The author works
   from `bench/heldout/FORMAT.md`'s discipline and the existing corpus
   as a style reference, and does not run the ranker, read its source,
   or see any arm's score while authoring. The dev instrument has never
   been blind and does not become sealed here — but authoring 100 new
   topics with the ranker's behaviour in view would let the instrument
   be shaped, however unconsciously, to the arms it is about to grade.
3. **Class mix is held** to the `has_checkable_literal` band that
   `test_class_mix_matches_what_a_real_store_measured` already pins
   across the whole corpus — that test stays the source of truth for
   the band, and this declaration does not restate its bounds. A new
   instrument that drifts the class mix is measuring a different store
   shape and is not comparable.
4. **The register is held.** New topics stay in the same
   technical-operational register as the existing twenty, because the
   corpus register is a confound the campaign has already been burned
   by (W1's park) and this unit is not the place to change it.
5. **Authored, not bulk-generated — and the W clause is not the one
   that governs here.** An earlier draft of this rule read "no
   LLM-synthesized corpus text, same doctrine clause the W program
   runs under," which contradicted the instrument it expands: the
   original twenty were written by language models running as separate
   agents with separate contexts (`bench/retrieval/README.md`, "Blind
   authoring"), so a flat ban would bar the exact procedure that
   produced the questions §3.1 freezes verbatim. The W program's
   exclusion is narrower and aimed elsewhere — it bars LLM-synthesized
   *training pairs*, because a model's paraphrase judgements carry its
   learned geometry in through a side door, neither reproducible nor
   auditable. Authoring benchmark prose borrows no geometry: here the
   text is the instrument being read from, not a source of
   supervision. What this rule does bar is bulk generation. Each topic
   family is authored deliberately as a family — one gold document and
   its distractor set, same subsystem, different decisions — and no
   document is written in a context that has seen the question that
   will retrieve it.

## 4. The bars — fixed now

- **I1-G1, integrity.** On the original-twenty subset, the expanded
  instrument reproduces all three baselines of §1 EXACTLY. Any
  deviation fails the unit and is diagnosed before anything else is
  read — it would mean the expansion perturbed the documents the
  original figures were measured on.
- **I1-G2, power.** The expanded instrument's paired resolution floor
  is ≤ 6 points at recall@1, computed by `bench/interval.py` and
  reported in the run's own output. This is a property of n and is
  checked arithmetically, not by ranking anything.
- **I1-G3, structure.** Every test in `tests/test_bench_retrieval.py`
  passes unmodified against the expanded corpus, with the two count
  assertions (`== 20`, `>= 150`) updated to the new counts and nothing
  else touched. A test that has to be weakened to admit the new corpus
  is a signal the corpus is wrong, not the test.
- **I1-G4, difficulty is not reset.** The expansion must not make the
  instrument easier: expansion-off recall@1 across the full 120 must
  land within the original twenty's 95% Wilson interval of [18%, 57%].
  A full-set score above that band means the new topics are easier
  than the old ones and the instrument has been softened, which would
  manufacture a bar clearance out of nothing.

## 5. What is read, and when

One read, at the end, on the committed corpus: all three probes
(asked / requery / control), expansion off and static hand tables, full
120 and original-20 subset, with intervals and the paired floor
printed. No tuning trail — there is nothing to tune. The artifact is
dated and committed like every other.

## 6. Owner doors — named, not opened

- **Whether the campaign's standing bars are restated against the new
  instrument.** They were set against the twenty-question one, and
  G1's "60" was derived from its granularity. Restating them is a
  charter question, not this unit's; it declares the instrument and
  stops.
- **Whether the expanded dev instrument changes the sealed
  instruments' status.** It does not, and this unit opens neither.
  `bench/heldout/data2/` stays reserved for P2a.

## Declared confounds

1. **A blind author is not an unbiased author.** Authoring 100 topics
   in one register by one hand will carry that hand's idea of what a
   question looks like. The original-twenty subset cell is the
   detector: if the full-120 scores diverge sharply from the subset on
   the same arm, the new topics differ in kind and the record says so.
2. **More documents is also a harder ranking problem.** Going from 180
   to ~1,000 documents crosses the 500-memory index threshold, so the
   expanded instrument runs in the prefilter regime by default where
   the current one does not. The read reports both regimes, and G1's
   integrity check runs on the original 180-document corpus so the
   anchors are compared like for like.
3. **This unit buys resolution, not truth.** A larger instrument makes
   the campaign's differences measurable; it does not promise they
   will be found to be real. The likeliest outcome of I1 is that
   several published dev-side differences resolve to zero.

## What is not claimed

No engine change and no ship. No claim that any past verdict was
wrong — `bench/POWER_AUDIT.md` re-reads them and retracts none. No
claim that 120 is sufficient for every question the campaign might
ask; it is sufficient for the five-to-fifteen-point differences the
campaign has actually been arguing about, and the declaration says so
rather than implying a general fix. No comparative claim against any
other memory system.
