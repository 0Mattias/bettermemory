# W3-D — the definitional-bridge unit at the preference class: unit declaration, 2026-08-17

Fourth running unit of Lane W under `bench/W_PROGRAM_DECLARATION.md`,
second of the W3 invention branch: non-neural, no training, no fetch.
Successor to W3-P2 through the door its declaration named in §9 and
its record confirmed as condition-met: the census parked, the bytes
are already pinned under the owner's broadened yes of 2026-08-17, and
the door opens on the standing delegation by declaring BEFORE reading.
This document is that declaration — extraction rule, census floors,
arms, budgets, bars — fixed before any pinned byte is read. The
enforcement record is the sha ordering: this commit (anatomy
re-earned), then the reader implementation with its CI leg, then the
census run, then — only if the census floors hold — the bridge build,
the tuning reads, and the gate. A miss is published, never
renegotiated.

The question W3-D asks, exactly: can DEFINITIONAL structure —
relation sections, definition glosses, and lead sentences extracted
from the pinned Wiktionary and Simple English Wikipedia dumps,
composed into a committed bridge table riding the rescue-expansion
leg's existing floors — supply the VERTICAL bridges (hypernym to
instance, topic to ingredient, affect to verb) whose absence from
duplicate-title paraphrase the W3-P2 census read, and connect the
ask→gold vocabulary gaps that hold the single-session-preference
class at 0.7333 (`bench/l/results/gate-lme-conv-a-2026-08-16.json`),
without repricing any match the engine already makes?

Why this unit fronts the queue: W3-P2's census proved the wall is
relation type, not register — 0.948 ask-vocabulary coverage in the
right registers and six of eight needs at zero, because duplicate
titles carry horizontal paraphrase and the misses need vertical
relations. Dictionaries and encyclopedic lead sentences are where
vertical relations are STATED rather than implied: a gloss says what
a thing IS. This is the last declared corpus-side route for the
preference class; the record that follows this unit says so plainly
either way.

## 1. Baselines this unit is judged against — carried, verified

Unchanged from W3-P2 §1 and quoted whole: the incumbent is the
shipped 6.1.0 default arm (conversational lane on, rescue expansion
off), gate artifact `bench/l/results/gate-lme-conv-a-2026-08-16.json`
with per-question sidecar
`bench/l/results/per-question/gate-lme-conv-a-pq-2026-08-16.json`:
LongMemEval full-500 macro recall@5 **0.9062**, macro recall@1
**0.5339** (paired lane-off 0.8935 / 0.5246); by type @5:
knowledge-update 0.9808, multi-session 0.8663,
single-session-assistant 1.0000, single-session-user 0.9714,
temporal-reasoning 0.8675, **single-session-preference 0.7333**
(22/30); dev instrument as-asked 35 / 60. The program horizon (dev 60;
LME macro@5 0.916) is carried, not claimed.

## 2. The anatomy — frozen at W3-P, re-earned at this commit

`w3p_anatomy.NEEDS` remains the single source of the bridge-need
token sets, fixed at 78fd0c3 and unedited; census floor C below is
defined over exactly these eight sets, and editing them now voids
this census. Re-earned rather than assumed:
`bench/w/w3p_anatomy.py` re-run at this commit, artifact
`bench/w/results/w3d-anatomy-2026-08-17.txt`, byte-identical
(sha256 `e1e7a895400f01f2fa75773ce2d47a64c4036a068221369ace58a13eceb581ed`)
to the W3-P and W3-P2 anatomy artifacts.

## 3. The corpus and the extraction rule — declared exactly

This declaration ADMITS, for reading, two register entries
(`bench/w/corpora.json`, both retrieved 2026-08-17, CC BY-SA 4.0,
publisher sha1 verified at fetch; the per-item sha256 pins are the
authority):

- `enwiktionary-20260801-pages-articles` — the eight bz2 part files
  under `bench/w/corpus/enwiktionary-20260801/`, page ids 1–11899399.
- `simplewiki-20260801-pages-articles-multistream` — the single bz2
  at `bench/w/corpus/simplewiki-20260801/`.

No other corpus is read and no fetch happens under this document. The
extraction is one streaming pass, this repository's own committed
code — w3d_edges.py under `bench/w/`, landing in the implementation
commit. Every emitted edge is a directed tuple
(head, term, label, source); head and term are single tokens under
the W3-P tokenizer, carried verbatim (lowercase alphanumeric runs,
length 3–30, engine filler stems dropped); a rule step that names a
multi-word target contributes each of its first 3 content tokens as
separate terms; self-edges (term = head) are dropped; exact
duplicate tuples are counted once with a multiplicity.

**Wiktionary rule** — part files in ascending filename order, each
sha256-re-verified against its register pin before its first byte;
`<page>` blocks with ns 0, no redirect element, and a title matching
`^[A-Za-z][a-z0-9]{2,29}$` (single ASCII word, lowercase or
Titlecase; the head is the title lowercased); only the `==English==`
section (text up to the next level-2 heading):

1. **Relation edges** — under any `Synonyms`, `Hypernyms`, or
   `Hyponyms` heading (heading level 3–5), from lines starting `*`
   until the next heading: every `{{l|en|TERM}}` first positional
   target and every bare `[[TERM]]` / `[[TERM|...]]` link target;
   label = the section name lowercased; source = `wiktionary`.
2. **Inline synonym edges** — `{{syn|en|...}}` / `{{synonyms|en|...}}`
   templates anywhere in the section: each positional argument after
   the language code not containing `=`; label `synonyms`, source
   `wiktionary`.
3. **Gloss edges** — the first 3 lines of the section starting `# `
   (exactly one `#`; `##`, `#:`, `#*` lines are not glosses): link
   targets `[[X]]` / `[[X|...]]` yield label `gloss-link`; then, with
   templates `{{...}}` removed (innermost first, at most 5 passes),
   piped links replaced by their display text, bare links by their
   target, and remaining markup characters stripped, the line's first
   12 content tokens yield label `gloss`. Source `wiktionary`.

**Simple English Wikipedia rule** — the single file, sha256
re-verified first; `<page>` blocks with ns 0 and no redirect element;
the title with any trailing ` (...)` parenthetical removed must yield
exactly ONE content token — the head:

4. **Lead edges** — the lead sentence is found mechanically: skipping
   lines that are empty or start with `{{`, `|`, `}}`, `<`, `==`,
   `*`, `:`, `#`, `[[File:`, or `[[Image:`, the first remaining line,
   truncated at the first `. ` or at 400 characters; its link targets
   yield label `lead-link`, and its stripped text's first 12 content
   tokens yield label `lead`. Source `simplewiki`.

The edge file (one `head<TAB>term<TAB>label<TAB>source<TAB>count`
line, heads in first-emission order, then terms, labels, sources in
first-emission order within a head) is a derived intermediate: NOT
committed, living under `bench/w/corpus/derived/` with its sha256
recorded in every artifact that uses it.

Budget, hard: the sha re-verifications plus the full extraction pass
are ONE pass over the nine files, at most 2 wall-clock hours
aggregate; a second pass exists only for G3's determinism repeat.
Overrun is a published park.

## 4. Stage 0 — the definitional census and its floors

The cheap read this unit runs before any engine work, published
whatever it says — `w3d-census-<date>.json` in `bench/w/results/`:

- **Edge volume**: distinct directed (head, term) pairs, total and by
  source and by label.
- **Need support**: for each of the eight `NEEDS` entries (ask-side
  set A, gold-side set B), the connecting attestations — distinct
  (head, term, label, source) tuples with head ∈ A and term ∈ B or
  head ∈ B and term ∈ A — counted, AND published verbatim as a list,
  so the record can quote exactly which stated facts connect each
  need.
- **Register overlap** (informational, gates nothing): the fraction
  of the 30 preference asks' content tokens, and of the 8 miss
  golds', present among edge heads and terms.

The floors, fixed now, with their derivations:

- **V (volume)**: ≥ **250,000** distinct (head, term) edges.
  Derivation: this floor guards catastrophic parse failure, nothing
  finer — the admitted dump spans page ids to 11.9M with glosses
  contributing up to 12 tokens each, so a structurally working parser
  lands far above this line, and a reading an order of magnitude
  below it means the wikitext rule failed, not that the dictionary is
  thin. Floor C carries the judgment.
- **C (coverage)**: ≥ **4** of the 8 needs, each connected by ≥ **2**
  attestations. Derivation, in two halves: the 4 is G1p's arithmetic,
  unchanged from W3-P2 — the class bar needs four whole questions and
  an unconnected need cannot move its question. The 2-attestation
  minimum replaces W3-P2's 5-pairs-per-need because the evidence unit
  changed kind: a pair corpus repeats its evidence token by token,
  while a definitional corpus states a fact once — demanding five
  distinct statements of one vertical fact would demand the
  dictionary say the same thing five different ways. Two attestations
  is corroboration: the same connection stated in two structures, or
  two distinct connecting facts.

**G0 verdict**: V AND C hold → the ladder (§5–§7) runs. Either fails
→ **W3D-PARK-AT-CENSUS**: the unit stops before any engine read, the
census is the record, and the consequence the W3-P2 record
pre-committed comes due: the preference class has then exhausted this
program's corpus-side routes, and the record says plainly that the
remaining distance to the reference line is not reachable by the
program's non-neural means as declared.

## 5. The bridge — build rule and artifact

Runs only under a G0 pass. From the edge file, this repository's own
committed code — w3d_bridge.py under `bench/w/`, landing with the
harness:

- **Score**: per directed (head, term), the weighted sum of its
  attestations — `synonyms` 6, `hypernyms` and `hyponyms` 4,
  `gloss-link` and `lead-link` 2, `gloss` and `lead` 1 — each
  distinct (label, source) attestation counted once.
- **Floors, declared defaults** (tunable only under §6's protocol;
  finals in the artifact): score ≥ 2; at most 4 bridge terms per head
  (ties and order by score descending, then term ascending); at most
  5,000 head terms (by total score descending, then head ascending);
  the emitted table source capped at 300 KB. The emission rides
  `expansion_terms`' existing filters — minimum length 3, filler
  stems excluded — through the same `build_tables` path W1 declared;
  it bypasses nothing.
- The artifact is the emitted bridge table, committed under
  `bench/w/artifacts/` beside a run JSON carrying the edge-file
  sha256, the floors used, and the sha256 of the table source. The
  build is deterministic: same edge file, same floors → same bytes.

## 6. The arm, the seam, and the read protocol

Verbatim W3-P2 §6. One arm, sealed now — there is no grid to shop:
**W3D-bridge**, the emitted table as the rescue-expansion leg's ONLY
table, swapped bench-side through the same `ExpansionTables` shape
and `build_tables` path; `src/` is not edited; the ranking path is
byte-identical to shipped
`search(rescue_expansion=True, conversational=True)`. Every arm of
every read runs lane-on; the paired off arm is the incumbent.

Reads: dev instrument unlimited during tuning, every read published
and numbered; at most THREE half-500 LME tuning reads (`--half even`);
one gate read — artifact committed first, then in one invocation set:
full-500 bridge arm, full-500 paired incumbent arm, dev both arms,
primary invocation run twice for determinism. The gate is the last
read; post-gate tuning does not exist. Nothing under `bench/heldout/`
is opened.

## 7. The bars — fixed now, verbatim W3-P2 §7

- **G1** (gate, full 500): macro recall@5 ≥ 0.916 AND macro recall@1
  ≥ 0.5339.
- **G1p** (gate, full 500): single-session-preference recall@5 ≥
  0.8667 (26/30).
- **G1h**: the gate's odd-half @5 delta over the incumbent ≥ 0.
- **G2** (gate): dev as-asked ≥ 35 / ≥ 60 with the bridge armed;
  every non-preference type's @5 within 0.5 points of its §1 guard
  value; the paired incumbent arm reproduces 0.9062 / 0.5339 exactly.
- **G3, unconditional**: extraction repeated → identical edge-file
  sha256; build repeated → identical table sha256; gate primary
  invocation repeated → identical results block; and the CI leg
  `tests/test_w3d_determinism.py` — hand-written page blocks, no
  corpus bytes — pinning the page gate, the English-section bound,
  each of the four rule steps, template stripping, the lead-sentence
  finder, tuple dedup, and the floors, byte-stable on every push.
- **Verdicts.** W3D-PASS: G3, G1, G1p, G2 all hold. W3D-PARTIAL: G3
  and G2 hold, the class moves ≥ 2 whole questions at @5 with
  macro@5 ≥ the incumbent, but G1 or G1p missed — published as a real
  mechanism finding; no ship sentence. W3D-PARK: G0 fails, or G2 or
  G3 fails, or nothing moves, or a budget is overrun. Every verdict
  publishes.

## 8. Declared confounds

1. **Wikitext is hostile to regex-level parsing.** The rule above is
   deliberately mechanical — headings, templates, links, one gloss
   shape — and will misread real pages at the margins. Floor V and
   the per-source, per-label totals detect structural collapse;
   marginal misreads are priced, not corrected. The CI fixture pins
   the rule, not wikitext's full grammar.
2. **Gloss and lead tokens are noisy by construction.** A gloss
   mentions many things besides the definiendum's genus. The build's
   label weights and caps are the filter; G2's per-type tripwires are
   the detector.
3. **Vertical direction fans out.** The table the leg consumes
   expands ASK tokens, so the guitar need requires `gibson` to
   survive inside `guitar`'s top-4 against every other guitar-related
   term — the hyponym direction is the wide one. The census floor
   deliberately does not test this (it prices whether the facts are
   STATED; the build and the reads price whether they COMPETE);
   confound 3 is the named reason a supported census can still yield
   a parked gate, and that outcome would be a mechanism finding, not
   a corpus finding.
4. **The single-token head gate.** Multi-word and hyphenated entries
   are skipped whole. Every token in every need set is a single
   word, so the census is not blinded by this; the bridge vocabulary
   is narrowed by it. Priced.
5. **Granularity.** Preference n=30; one question is 3.33 class
   points; G1p moves in whole questions.
6. **Half-fitting.** G1h is the institutional answer.
7. **The prior, registered with its own track record.** Pre-read
   prediction: V clears by an order of magnitude; needs
   `guitar↔gibson/fender/stratocaster`, `cocktail↔drinks/glass`,
   `recipe↔homemade/making`, `battery↔charging/power`, and
   `nostalgic↔remember/memories` find ≥ 2 attestations;
   `publications↔research` likely; `dinner↔recipe/basil` and
   `evening↔schedule` uncertain. Predicted C: 5–7 of 8. The two
   prior units' predictions both erred optimistic; a third optimistic
   miss would itself be a datum about pre-read priors on this grid,
   and is registered as such in advance.

## 9. Owner doors — named, not opened

- **Any wiring of any bridge into the package** — opt-in or default —
  is a separate unit with its own plain sentence, after the read.
- **W1b** (the wide-register vocabulary retrain) stays staged with
  its own declaration owed before its bytes are read for training.
- The Lane T criterion-v1 ratification stands where the last record
  left it.

## What is not claimed

No criterion claim (§1). No ship sentence from any verdict of this
unit. No comparative claim against any other memory system from a
single-system artifact. No judgment of W3-P2's paraphrase route
beyond its own record. No reading of any archive outside the two
admitted entries. No reuse of the edge file or the bridge outside the
declared arm until a unit declares it.
