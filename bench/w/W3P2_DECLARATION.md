# W3-P2 — the paraphrase-bridge unit on the register-matched corpus: unit declaration, 2026-08-17

Third running unit of Lane W under `bench/W_PROGRAM_DECLARATION.md`,
successor to W3-P through the owner door its record named: the
register-matched pair corpus. The owner's plain-sentence yes of
2026-08-17 covered the fetch and its same-day broadening (the
acquisition commit's twenty pins); this document is the unit contract —
extraction rule, census floors, arms, budgets, bars — fixed before any
pinned byte is read. The enforcement record is the sha ordering: this
commit (anatomy re-earned), then the reader implementation with its CI
leg, then the census run, then — only if the census floors hold — the
bridge build, the tuning reads, and the gate. A miss is published,
never renegotiated.

The question is W3-P's, unchanged: can pair-supervised paraphrase
structure — substitution evidence, now from LABELED duplicate-question
edges in register-matched corpora — composed into a committed bridge
table riding the rescue-expansion leg's existing floors, connect the
ask→gold vocabulary gaps that hold the single-session-preference class
at 0.7333, without repricing any match the engine already makes?

What changed since W3-P, exactly two things, both corpus-side: the
duplicate signal is the platform's labeled closure mechanism
(`PostLinks.xml`, LinkTypeId 3) instead of the thin legacy body
blockquote, and the registers are the anatomy's own — food, drink,
music-gear, consumer-tech, planning, academia — instead of
programming. The mechanism, the arm, the read protocol, and the bars
are carried verbatim; this unit tests the corpus decision, not a new
idea.

## 1. Baselines this unit is judged against — carried, verified

The incumbent is unchanged: the shipped 6.1.0 default arm
(conversational lane on, rescue expansion off), gate artifact
`bench/l/results/gate-lme-conv-a-2026-08-16.json` with per-question
sidecar `bench/l/results/per-question/gate-lme-conv-a-pq-2026-08-16.json`:

- LongMemEval full-500: macro recall@5 **0.9062**, macro recall@1
  **0.5339**; paired lane-off arm 0.8935 / 0.5246.
- By type @5 (the guard values of §7): knowledge-update 0.9808,
  multi-session 0.8663, single-session-assistant 1.0000,
  single-session-user 0.9714, temporal-reasoning 0.8675,
  **single-session-preference 0.7333** (22/30).
- Dev instrument (`bench/retrieval/run.py`, unpadded, prefilter off,
  asked): recall@1 35%, recall@5 60%.

The program horizon (dev as-asked 60; LongMemEval macro@5 0.916,
`bench/longmemeval/results/claude-mem-full500.json`) is carried, not
claimed. The campaign plan's standing sentence remains on the record:
L2 parked and W3-P parked at its census, so the program is in trouble
on the PRIOR corpus inventory — this unit is the corpus decision the
owner took in response, and its census is that sentence's first test.

## 2. The anatomy — frozen at W3-P, re-earned at this commit

`w3p_anatomy.NEEDS` is the single source of the bridge-need token
sets, fixed at the W3-P declaration commit (78fd0c3) and UNEDITED
since — the git history of `bench/w/w3p_anatomy.py` is the proof, and
editing it now voids this census exactly as it would have voided
W3-P's. The census floor C below is defined over exactly these eight
sets.

Re-earned rather than assumed: `bench/w/w3p_anatomy.py` was re-run at
this commit and reproduced every miss rank against the committed
sidecar; the dated artifact `bench/w/results/w3p2-anatomy-2026-08-17.txt`
is byte-identical (sha256
`e1e7a895400f01f2fa75773ce2d47a64c4036a068221369ace58a13eceb581ed`)
to the W3-P anatomy artifact — the incumbent the bars guard is exactly
the incumbent that produced the misses.

## 3. The corpus and the extraction rule — declared exactly

This declaration ADMITS, for reading, the eighteen per-site Stack
Exchange archives the register (`bench/w/corpora.json`) records as
retrieved 2026-08-17 under the owner's yes — by register name:

`academia-stackexchange-archive`, `android-stackexchange-archive`,
`apple-stackexchange-archive`, `beer-stackexchange-archive`,
`coffee-stackexchange-archive`, `cooking-stackexchange-archive`,
`diy-stackexchange-archive`, `fitness-stackexchange-archive`,
`gardening-stackexchange-archive`, `interpersonal-stackexchange-archive`,
`lifehacks-stackexchange-archive`, `movies-stackexchange-archive`,
`music-stackexchange-archive`, `outdoors-stackexchange-archive`,
`parenting-stackexchange-archive`, `pets-stackexchange-archive`,
`superuser-archive`, `travel-stackexchange-archive`.

The register rows are the authoritative pins (sha256 per archive,
CC BY-SA family, publisher md5 verified at fetch). No other corpus is
read; in particular the `enwiktionary-20260801-pages-articles` and
`simplewiki-20260801-pages-articles-multistream` pins are NOT admitted
by this document (§9), and no fetch happens under it.

The extraction is this repository's own committed code — w3p2_pairs.py
under `bench/w/`, landing in the implementation commit — processing
archives in ascending order of their `local_path` basename, per
archive:

1. Re-verify the register's pinned sha256 over the exact archive bytes
   before reading any member; a mismatch stops the unit — the pin is
   the authority. The pass records the member list of each archive.
2. **Edge pass** — `PostLinks.xml` streamed via `bsdtar -xOf`: rows
   with `LinkTypeId="3"` yield directed duplicate edges
   (PostId, RelatedPostId), kept in document order, exact repeats of
   an already-seen (PostId, RelatedPostId) edge dropped. Edge counts
   (total link rows, duplicate edges, deduped) are recorded per site.
3. **Title pass** — `Posts.xml` streamed the same way: every row
   counts toward the per-site PostTypeId census (the reader census
   W1b's declaration may reuse); rows with `PostTypeId="1"` whose Id
   appears on either side of a duplicate edge contribute their Title.
4. **Pair rule**: for each deduped edge in document order, left title
   = the PostId row's Title, right title = the RelatedPostId row's
   Title — both HTML-unescaped, trailing `[duplicate]` / `[closed]`
   markers stripped repeatedly; the pair is kept only if both titles
   resolved (both posts present in the dump as questions), are
   non-empty, distinct after lowercasing, and each yields ≥ 2 content
   tokens. Edges whose titles do not resolve are counted per site as
   unresolved, published, and contribute nothing.
5. Tokenizer, for extraction, census, and build alike, verbatim from
   W3-P: lowercase, split on non-alphanumeric runs, keep tokens of
   length 3–30, drop the engine's query-filler stems.
6. The pair file (one `site<TAB>left<TAB>right` line per pair, sites
   in pass order, edges in document order) is a derived intermediate:
   NOT committed (CC BY-SA per-post attribution), living beside the
   corpus under `bench/w/corpus/derived/` with its sha256 recorded in
   every artifact that uses it.
7. An archive missing `Posts.xml` or `PostLinks.xml` is recorded as
   `missing-member` for that site, contributes zero pairs, and does
   NOT stop the pass — the census publishes the hole; the floors then
   judge what remains.

Budget, hard: the sha re-verifications plus the full extraction pass
are ONE pass over the eighteen archives, at most 2 wall-clock hours
aggregate; a second pass exists only for G3's determinism repeat.
Overrun is a published park.

## 4. Stage 0 — the register census and its floors

The cheap read this unit runs before any engine work, published
whatever it says — `w3p2-census-<date>.json` in `bench/w/results/`:

- **Pair volume**: total well-formed pairs, aggregate and per site.
- **Need support**: for each of the eight `NEEDS` entries, the count
  of pairs (T1, T2) with a token a from one side's set in one title, a
  token b from the other side's set in the other title, and neither in
  the opposite title — exclusive substitution, either direction,
  verbatim W3-P §4 — aggregate, with a per-site breakdown published
  informationally.
- **Register overlap** (informational, gates nothing): the fraction of
  the 30 preference asks' content tokens, and of the 8 miss golds'
  content tokens, present anywhere in the pair vocabulary.
- **Continuity row** (informational, gates nothing): the verdict the
  W3-P floors (V ≥ 50,000; C ≥ 3 of 8) would have returned on these
  counts, so the two censuses read side by side without renegotiation.

The floors, fixed now, with their derivations published so the change
from W3-P is arithmetic, not appetite:

- **V (volume)**: ≥ **25,000** aggregate pairs. Derivation: the
  bridge build (§5) admits a substitution pair type only at count
  ≥ 10, and its table is capped at 5,000 head terms; a pair corpus
  below ~25k observations cannot populate more than a token fraction
  of even a minimal general table at those floors — below this, either
  the labeled-edge rule failed to resolve titles at scale or the sites
  cannot carry a general bridge, and nothing downstream is meaningful.
  (W3-P's 50,000 was the rule-failure floor on a 24.1M-question
  corpus; the continuity row reports it, the floor here is the one
  derived from what the build actually consumes.)
- **C (coverage)**: ≥ **4** of the 8 needs each supported by ≥ 5
  exclusive-substitution pairs. Derivation: G1p (§7) requires the
  class to reach 26/30 — four whole questions above the incumbent's
  22 — and a need with no pair support cannot move its question, so
  with fewer than four supported needs the class bar is arithmetically
  unreachable before any engine read. The census kills what the
  arithmetic already killed; five-per-need is W3-P's evidentiary
  minimum, unchanged.

**G0 verdict**: V AND C hold → the ladder (§5–§7) runs. Either fails →
**W3P2-PARK-AT-CENSUS**: the unit stops before any engine read, the
census is the record, and the park prices the register-matched corpus
premise itself — with the honest consequence that the paraphrase-bridge
route has then failed on BOTH the wrong-register corpus and the
right-register one, and what remains for the preference class is the
definitional-bridge route through the unread pins (§9), which is a new
unit's declaration, not this one's.

## 5. The bridge — build rule and artifact

Runs only under a G0 pass. Verbatim W3-P §5, retargeted at the new
pair file. From the pair file, this repository's own committed code —
w3p2_bridge.py under `bench/w/`, landing with the harness:

- **Signal**: exclusive substitution — token a in T1, token b in T2,
  a ∉ T2, b ∉ T1, counted symmetrically over all pairs. Site is not a
  feature; the aggregate pair file is the corpus.
- **Score**: PPMI over the substitution co-occurrence table.
- **Floors, declared defaults** (tunable only under §6's protocol;
  finals in the artifact): substitution count ≥ 10; PPMI ≥ 2.0;
  mutual rank ≤ 8; at most 4 bridge terms per head term; at most
  5,000 head terms; the emitted table source capped at 300 KB. The
  emission rides `expansion_terms`' existing filters — minimum length
  3, filler stems excluded — through the same `build_tables` path W1
  declared; it bypasses nothing.
- The artifact is the emitted bridge table, committed under
  `bench/w/artifacts/` beside a run JSON carrying the pair-file
  sha256, the floors used, and the sha256 of the table source. The
  build is deterministic: same pair file, same floors → same bytes.

## 6. The arm, the seam, and the read protocol

Verbatim W3-P §6. One arm, sealed now — there is no grid to shop:

- **W3P2-bridge**: the emitted bridge table rides the rescue-expansion
  leg as its ONLY table (the three hand tables are out; the claim
  under test is pair-derived structure, not curation). The measurement
  harness is bench-side and swaps table contents through the same
  `ExpansionTables` shape and `build_tables` path W1 declared; `src/`
  is not edited; the ranking path is byte-identical to shipped
  `search(rescue_expansion=True, conversational=True)`.
- Every arm of every read runs lane-on; the paired off arm is
  rescue-off lane-on — the incumbent.

Reads:

- **Dev instrument**: unlimited during tuning, every read published
  and numbered in `bench/w/results/`.
- **LongMemEval tuning**: at most THREE half-500 reads (the L-lane's
  `--half even` machinery — the tuning half), all published.
- **The gate read**, one: the artifact is committed first, then in one
  invocation set — full-500 LME bridge arm, full-500 paired incumbent
  arm, dev both arms, and the primary invocation run twice for
  determinism. The gate is the last read; post-gate tuning does not
  exist.
- **Sealed stays sealed**: nothing under `bench/heldout/` is opened.

## 7. The bars — fixed now, verbatim W3-P §7

- **G1, the horizon bar** (gate, full 500): macro recall@5 ≥ 0.916
  AND macro recall@1 ≥ 0.5339 — the reference line, with no @1
  give-back.
- **G1p, the class bar** (gate, full 500): single-session-preference
  recall@5 ≥ 0.8667 (26/30).
- **G1h, the generalization bar**: the gate's odd-half @5 delta over
  the incumbent is ≥ 0.
- **G2, the no-damage bar** (gate): dev as-asked with the bridge armed
  ≥ 35 / ≥ 60; every non-preference type's @5 within 0.5 points of its
  §1 guard value; and the paired incumbent arm reproduces
  0.9062 / 0.5339 exactly (the clock control).
- **G3, the determinism bar**, unconditional: the extraction pass
  repeated → identical pair-file sha256; the build repeated →
  identical table sha256; the gate's primary invocation repeated →
  identical results block; and a CI check exercising the extraction
  and census code paths — including the PostLinks join, edge dedup,
  unresolved-edge accounting, and the missing-member rule — on a
  committed synthetic fixture (hand-written rows, no corpus bytes)
  asserting byte-stable output on every push:
  `tests/test_w3p2_determinism.py`.
- **Verdicts.** W3P2-PASS: G3, G1, G1p, G2 all hold. W3P2-PARTIAL: G3
  and G2 hold, the class moves by ≥ 2 whole questions at @5 with
  macro@5 ≥ the incumbent, but G1 or G1p is missed — published as a
  real mechanism finding; no ship sentence. W3P2-PARK: G0 fails
  (park-at-census), or G2 or G3 fails, or nothing moves, or a budget
  is overrun. Every verdict publishes.

## 8. Declared confounds

1. **The register thesis is now the thing under test — prediction
   registered.** W3-P's census taught that bridge supervision lives in
   pair structure, and its park predicted a register-matched source
   would carry it. The pre-read prediction, recorded so the census can
   refute it: V clears with room; needs `battery↔charging` (android,
   apple, superuser), `guitar↔gibson/fender` (music),
   `publications↔research` (academia), `dinner↔recipe/basil` and
   `recipe↔homemade` (cooking), and `cocktail↔drinks/glass` (beer,
   cooking) find support; `evening↔schedule` (lifehacks) is uncertain;
   `nostalgic↔remember` reads at or near zero — duplicate-question
   structure may be structurally unable to carry the emotional
   register, which is exactly what the §9 door exists for. Predicted C
   outcome: 4–6 of 8 supported. A miss in either direction publishes.
2. **Small-site sparsity and closure-culture variance.** Beer, coffee,
   and lifehacks are tiny archives; duplicate-closure practice varies
   by site. The per-site rows record it; the floors are aggregate by
   design.
3. **Title-only pairing.** Needs whose vocabulary lives in bodies
   rather than titles undercount, as in W3-P. Priced, not corrected.
4. **Cross-site aggregation.** The pair vocabulary mixes eighteen
   registers; a bridge admitted on evidence from one site applies
   engine-wide. The build floors (count ≥ 10, PPMI ≥ 2.0, mutual rank
   ≤ 8) are the noise filter; G2's per-type tripwires are the
   detector.
5. **Additive-leg dilution — W1's polarity lesson.** Boost-only design
   bounds the blast radius; G2 detects the rest.
6. **Granularity.** Preference n=30: one question is 3.33 class
   points; G1p moves in whole questions.
7. **Half-fitting — L2's lesson.** G1h is the institutional answer.

## 9. Owner doors — named, not opened

- **The definitional-bridge source.** The `enwiktionary-20260801`
  parts and `simplewiki-20260801` pins were fetched under the same
  broadened yes for the need classes duplicate-question structure
  cannot carry (the emotional register above all). They stay UNREAD
  under this document. If this census parks, or passes with the
  nostalgic need unsupported, the successor unit that reads them
  declares its own extraction rule and floors first — that declaration
  is the door, and it opens on the owner's standing delegation without
  a new fetch sentence, the bytes being already pinned.
- **Any wiring of the bridge into the package** — opt-in or default —
  is a separate unit with its own plain sentence, after the read.
- The Lane T criterion-v1 ratification stands where the last record
  left it.

## What is not claimed

No criterion claim (§1). No ship sentence from any verdict of this
unit. No comparative claim against any other memory system from a
single-system artifact. No W1b entry decision — the per-site row
censuses are shared infrastructure, but the retrain is its own
declared unit. No reading of the enwiktionary or simplewiki pins under
this document. No reuse of the pair file or the bridge outside the
declared arm until a unit declares it.
