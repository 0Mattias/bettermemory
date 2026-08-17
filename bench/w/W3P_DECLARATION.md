# W3-P — the paraphrase-bridge unit at the preference class: unit declaration, 2026-08-17

Second running unit of Lane W under `bench/W_PROGRAM_DECLARATION.md`,
first of the W3 invention branch: non-neural, no training, no fetch.
The program frame carries the doctrine; this document is the unit
contract — extraction rule, census floors, arms, budgets, bars — fixed
before any corpus byte is read. The enforcement record is the sha
ordering: this commit (anatomy included), then the reader
implementation, then the census run, then — only if the census floors
hold — the bridge build, the tuning reads, and the gate. A miss is
published, never renegotiated.

The question W3-P asks, exactly: can pair-supervised paraphrase
structure — substitution evidence extracted from the pinned
duplicate-question corpus, composed into a committed bridge table
riding the rescue-expansion leg's existing floors — connect the
ask→gold vocabulary gaps that hold the single-session-preference class
at 0.7333, without repricing any match the engine already makes?

Why this unit fronts the queue: L2 parked Lane L's last cheap
deterministic route (`bench/l/L2_RECORD.md`) and left the residual
@5 distance on record as structural — the single-session-preference class, where
the incumbent reads 0.7333
(`bench/l/results/per-question/gate-lme-conv-a-pq-2026-08-16.json`,
n=30) against the reference stack's own preference row of 0.9667 on
the same corpus (`bench/longmemeval/results/claude-mem-full500.json`),
plus small temporal and multi-session residuals that pricing cannot
reach. Parity at the preference class alone would carry the macro past
the reference line. Pair supervision is precisely the structure W1's
corpus-frequency route lacked; that is the thesis under test. And L2's
mechanism finding governs the design: co-match separation is
load-bearing — this unit only ADDS vocabulary through a gated,
down-weighted leg; it reprices nothing.

## 1. Baselines this unit is judged against — committed, quoted

The incumbent is the shipped 6.1.0 default arm (conversational lane
on, rescue expansion off), gate artifact
`bench/l/results/gate-lme-conv-a-2026-08-16.json` with per-question
sidecar `bench/l/results/per-question/gate-lme-conv-a-pq-2026-08-16.json`:

- LongMemEval full-500: macro recall@5 **0.9062**, macro recall@1
  **0.5339**; paired lane-off arm 0.8935 / 0.5246.
- By type @5 (the guard values of §7): knowledge-update 0.9808,
  multi-session 0.8663, single-session-assistant 1.0000,
  single-session-user 0.9714, temporal-reasoning 0.8675,
  **single-session-preference 0.7333** (22/30).
- Dev instrument (`bench/retrieval/run.py`, unpadded, prefilter off,
  asked): recall@1 35%, recall@5 60% — lane-on equals lane-off
  byte-identically (L1's G2), and the rescue leg is off in the
  default arm.

The program horizon (dev as-asked 60,
`bench/retrieval/results/r1-unpadded-2026-08-13.json`; LongMemEval
macro@5 0.916, `bench/longmemeval/results/claude-mem-full500.json`) is
carried, not claimed. The standing pressure is on record in the
campaign plan: L2 parked, so if this unit also parks, the record says
plainly that the program is in trouble — that sentence is owed to the
owner, not optional.

## 2. The anatomy — published with this declaration, not after

`bench/w/w3p_anatomy.py` (committed beside this document) reproduces
each of the eight preference misses through the LongMemEval runner's
own store-building and search invocation, asserts per-question parity
with the committed sidecar, and prints the miss table; the dated
output is `bench/w/results/w3p-anatomy-2026-08-17.txt`. The preference
rows of the L1 and L2 gate sidecars are byte-identical (verified in
producing the artifact), so the anatomy reads the incumbent exactly.

The eight misses, compressed — gold session rank, then the unbridged
substitution the miss is attributable to:

| qid | rank | bridge need | register |
|---|---|---|---|
| 195a1a1b | 6 | evening activities ↔ schedule/tasks | everyday-planning |
| 505af2f5 | 6 | recipe ↔ homemade/making | food |
| 09d032c9 | 7 | battery ↔ charging/power | consumer-tech |
| 95228167 | 8 | guitar ↔ Gibson/Fender/Stratocaster | music-gear |
| d6233ab6 | 10 | nostalgic/reunion ↔ remember/memories | everyday-emotional |
| 1a1907b4 | 14 | cocktail ↔ drinks/glass | food/drink |
| 06f04340 | 25 | dinner/serve/homegrown ↔ recipe/basil/mint | food |
| 75832dbd | 30 | publications/conferences ↔ research/datasets | academic/technical |

The operationalized token sets live in `w3p_anatomy.NEEDS` — the
census (§4) imports them; they are fixed at this commit and editing
them afterwards voids the census. Five of eight golds sit in the
5–9 band a bounded boost can reach; three are deep. The register
column is the unit's honest weather report, priced in confound 1.

## 3. The corpus and the extraction rule — declared exactly

This declaration ADMITS `stackoverflow-posts-archive` to this unit,
for reading — the pin as the register (`bench/w/corpora.json`) records
it: sha256
`1fcde86b9a0d701261a96698e78f65b7436d896eece5361ec7922d7c725c41cd`,
CC BY-SA, fetched 2026-08-16 under the owner's plain-sentence yes. No
other corpus is read; no fetch happens under this document.

The extraction is one streaming pass, this repository's own committed
code — w3p_pairs.py under `bench/w/`, landing in the implementation
commit:

1. `bsdtar -xOf` the pinned 7z, `Posts.xml` only; the pass re-verifies
   the pinned sha256 over the archive bytes before reading and records
   it in the census artifact.
2. Row filter: `PostTypeId="1"` (questions) whose Body contains the
   legacy duplicate-closure blockquote — the literal text
   `Possible Duplicate` in either capitalization ("Possible Duplicate"
   / "Possible duplicate"). This is the OLD closure mechanism, which
   physically inserted the target link into the body; it is the only
   duplicate signal Posts.xml itself carries (PostLinks is not in the
   pin).
3. Pair rule: left title = the row's own Title with any trailing
   `[duplicate]` / `[closed]` marker stripped; right title = the
   anchor text of the first `<a>` inside that blockquote. Both
   HTML-unescaped; a pair is kept only if both titles are non-empty,
   distinct after lowercasing, and each yields ≥ 2 content tokens
   under the tokenizer below.
4. Tokenizer, for extraction, census, and build alike: lowercase,
   split on non-alphanumeric runs, keep tokens of length 3–30,
   drop the engine's query-filler stems (the same
   `QUERY_FILLER_WORDS` posture the leg already enforces).
5. The pair file (one tab-separated pair per line, deterministic
   corpus order) is a derived intermediate: NOT committed (CC BY-SA
   per-post attribution makes verbatim redistribution a burden this
   unit does not need to carry) — it lives beside the corpus under
   `bench/w/corpus/derived/` with its sha256 recorded in every
   artifact that used it. The census artifact records the pair count,
   the row counts by PostTypeId (the reader census W1b reuses), and
   the extraction-pass wall-clock.

Budget, hard: the extraction pass is ONE pass, at most 3 wall-clock
hours; a second pass exists only for G3's determinism repeat. Overrun
is a published park.

## 4. Stage 0 — the register census and its floors

The cheap read this unit runs before any engine work, published
whatever it says — w3p-census-2026-08-17.json in `bench/w/results/`:

- **Pair volume**: total well-formed pairs.
- **Need support**: for each of the eight `NEEDS` entries, the count
  of pairs (T1, T2) with a token a from one side's set appearing in
  one title, a token b from the other side's set in the other title,
  and neither token in the opposite title (exclusive substitution,
  either direction).
- **Register overlap** (informational, gates nothing): the fraction
  of the 30 preference asks' content tokens, and of the 8 miss golds'
  content tokens, that appear anywhere in the pair vocabulary.

The floors, fixed now:

- **V (volume)**: ≥ 50,000 pairs. Below it the extraction rule
  itself failed on this corpus and nothing downstream is meaningful.
- **C (coverage)**: ≥ 3 of the 8 needs each supported by ≥ 5
  exclusive-substitution pairs.

**G0 verdict**: V AND C hold → the ladder (§5–§7) runs. Either
fails → **W3P-PARK-AT-CENSUS**: the unit stops before any engine
read, the census is the record, and the park prices the CORPUS
REGISTER, not the mechanism — the pair-supervision thesis stays open
on a register-matched pair source, which is an owner door (§9), not a
fetch this unit may take.

## 5. The bridge — build rule and artifact

Runs only under a G0 pass. From the pair file, this repository's own
committed code — w3p_bridge.py under `bench/w/`, landing with the
harness:

- **Signal**: exclusive substitution — token a in T1, token b in T2,
  a ∉ T2, b ∉ T1, counted symmetrically over all pairs.
- **Score**: PPMI over the substitution co-occurrence table.
- **Floors, declared defaults** (tunable only under §6's protocol;
  finals in the artifact): substitution count ≥ 10; PPMI ≥ 2.0;
  mutual rank ≤ 8; at most 4 bridge terms per head term; at most
  5,000 head terms; the emitted table source capped at 300 KB. The
  emission rides
  `expansion_terms`' existing filters — minimum length 3, filler
  stems excluded — through the same `build_tables` path W1 declared;
  it bypasses nothing.
- The artifact is the emitted bridge table, committed under
  `bench/w/artifacts/` beside a run JSON carrying the pair-file
  sha256, the floors used, and the sha256 of the table source. The
  build is deterministic: same pair file, same floors → same bytes.

## 6. The arm, the seam, and the read protocol

One arm, sealed now — there is no grid to shop:

- **W3P-bridge**: the emitted bridge table rides the
  rescue-expansion leg as its ONLY table (the three hand tables are
  out for this arm; the claim under test is pair-derived structure,
  not curation). The measurement harness is bench-side and swaps
  table contents through the same `ExpansionTables` shape and
  `build_tables` path W1 declared; `src/` is not edited; the ranking
  path is byte-identical to shipped
  `search(rescue_expansion=True, conversational=True)`.
- Every arm of every read runs lane-on (the lane IS the default
  engine); the paired off arm is rescue-off lane-on — the incumbent.

Reads:

- **Dev instrument**: unlimited during tuning, every read published
  and numbered in `bench/w/results/`.
- **LongMemEval tuning**: at most THREE half-500 reads (the L-lane's
  `--half even` machinery — the tuning half), all published.
- **The gate read**, one: the artifact is committed first, then in
  one invocation set — full-500 LME bridge arm, full-500 paired
  incumbent arm, dev both arms, and the primary invocation run twice
  for determinism. The gate is the last read; post-gate tuning does
  not exist.
- **Sealed stays sealed**: nothing under `bench/heldout/` is opened.

## 7. The bars — fixed now

- **G1, the horizon bar** (gate, full 500): macro recall@5 ≥ 0.916
  AND macro recall@1 ≥ 0.5339 — the reference line, with no @1
  give-back.
- **G1p, the class bar** (gate, full 500): single-session-preference
  recall@5 ≥ 0.8667 (26/30) — halving the gap to the reference row's
  0.9667, in whole questions.
- **G1h, the generalization bar** (L2's lesson institutionalized):
  the gate's odd-half @5 delta over the incumbent is ≥ 0 — a tuning
  gain that inverts on the untouched half is a fail whatever the
  full-500 says.
- **G2, the no-damage bar** (gate): dev as-asked with the bridge
  armed ≥ 35 / ≥ 60; every non-preference type's @5 within 0.5
  points of its §1 guard value; and the paired incumbent arm
  reproduces 0.9062 / 0.5339 exactly (the clock control).
- **G3, the determinism bar**, unconditional: the extraction pass
  repeated → identical pair-file sha256; the build repeated →
  identical table sha256; the gate's primary invocation repeated →
  identical results block; and a CI check exercising the extraction
  and build code paths on a committed synthetic fixture (hand-written
  rows, no corpus bytes) asserting byte-stable output on every push.
- **Verdicts.** W3P-PASS: G3, G1, G1p, G2 all hold. W3P-PARTIAL: G3
  and G2 hold, the class moves by ≥ 2 whole questions at @5 with
  macro@5 ≥ the incumbent, but G1 or G1p is missed — published as a
  real mechanism finding; no ship sentence. W3P-PARK: G0 fails
  (park-at-census), or G2 or G3 fails, or nothing moves, or a budget
  is overrun. Every verdict publishes.

## 8. Declared confounds

1. **The register wall — the live risk, prediction registered.** The
   anatomy's needed bridges are overwhelmingly everyday-register
   (food, drink, music-gear, planning, emotion); the pinned corpus is
   programming-register. The pre-read prediction, recorded so the
   census can refute it: needs `battery↔charging/power` and possibly
   `publications↔research` find support; the rest read near zero, and
   G0 kills at floor C. A census park prices the corpus, not the
   mechanism — and the honest sentence that follows is that the
   preference route needs a register-matched pair source (§9), which
   no autonomous session may fetch.
2. **Extraction-rule bias.** The legacy blockquote covers roughly the
   first eight years of closures; the pair vocabulary skews early-SO.
   Floor V detects outright failure; the skew itself is recorded, not
   corrected.
3. **Additive-leg dilution — W1's polarity lesson.** Expansion terms
   dilute precision on conversational stores (the hand tables cost
   −1.65 @5 on this instrument). Boost-only design bounds the blast
   radius; G2's per-type tripwires are the detector.
4. **Granularity.** Preference n=30: one question is 3.33 class
   points; G1p moves in whole questions, and sub-point macro deltas
   are finer than the class can resolve. Priced, as in W1 confound 2.
5. **Half-fitting — L2's lesson.** Even-half tuning curves can invert
   on the holdout; G1h is the institutional answer, and which
   questions move is a property of the half until the gate says
   otherwise.

## 9. Owner doors — named, not opened

- **The register-matched pair corpus.** If G0 parks at the census,
  the successor fetch this record will propose in one plain sentence:
  per-site Stack Exchange dumps from the same archive.org item
  (cooking, music, interpersonal, fitness — everyday-register sites,
  a few hundred MB compressed each, same CC BY-SA family), whose
  `PostLinks.xml` carries duplicate edges explicitly, replacing the
  legacy-blockquote heuristic with labeled pairs. Named now so the
  census park converts to a decision the owner can take in one
  sentence.
- **Any wiring of the bridge into the package** — opt-in or default —
  is a separate unit with its own plain sentence, after the read.
- The Lane T criterion-v1 ratification stands where the last record
  left it.

## What is not claimed

No criterion claim (§1). No ship sentence from any verdict of this
unit — the arm is measured on an opt-in leg. No comparative claim
against any other memory system from a single-system artifact. No
W1b entry decision — the reader this unit builds is shared
infrastructure, but the retrain is its own declared unit. No reuse of
the pair file or the bridge outside the declared arm until a unit
declares it.
