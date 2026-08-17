# W3-D — the record: parked at the census, 2026-08-17

The unit ran exactly as declared and stopped exactly where the
declaration said a failed census stops it: before any engine read.
The chain is sha-ordered — declaration with the anatomy re-earned
byte-identical (21c230d), reader implementation with its CI
determinism leg (5d31605), then the census run — and the census
artifact is `bench/w/results/w3d-census-2026-08-17.json`, published
whole. One provenance note, stated rather than smoothed: a style-only
commit (961a72f, a test-fixture reflow) landed while the pass was
running, so the artifact's commit stamp is 961a72f; the reader and
the declaration are byte-identical at 5d31605 and 961a72f, and the
extraction those bytes define is the one that ran. Nothing below
renegotiates anything; the floors were fixed at the declaration
commit with their derivations in the open, and the read fell one
need short of floor C.

## The census, measured

One declared pass over the nine admitted files, each pinned sha256
re-verified before its first byte (501.5 seconds whole, against a
2-hour budget), artifact
`bench/w/results/w3d-census-2026-08-17.json`:

- 457,535 Wiktionary heads and 69,299 Simple English Wikipedia heads
  yielded edges; 3,670,112 distinct directed (head, term) edges,
  4,880,463 attestations. **Floor V holds** at 3,670,112 against
  250,000 — the wikitext rule parsed at scale, with the label mix
  published (glosses dominate; 165,801 synonym edges, 10,957
  hypernym/hyponym edges).
- **Need support: three of eight** reach the two-attestation floor —
  `guitar↔gibson/fender/stratocaster` at 7 (the brands' own glosses
  and leads state the instrument), `battery↔charging/power` at 3,
  and `cocktail↔drink` at 3 (including a literal Wiktionary Synonyms
  entry). The other five — `dinner↔recipe/basil`,
  `evening↔schedule`, `recipe↔homemade/making`,
  `publications↔research`, `nostalgic↔remember/memories` — read
  **zero**. **Floor C fails at 3 against 4.**
- The overlap rows close the vocabulary question for good: **1.0000**
  of the preference asks' content tokens and **0.9551** of the miss
  golds' exist among the edge vocabulary. Every word is known; five
  connections are simply never stated.

**G0 verdict: W3D-PARK-AT-CENSUS**, per the declared grid. No bridge
was built, no tuning read ran, no instrument was touched. The engine
is behaviorally unchanged and the honest interim sentence stands as
the L2 record left it.

## What the census teaches

1. **The vertical thesis is confirmed exactly where it is a
   definitional fact — and the guitar need is the proof.** W3-P2
   read `guitar↔gibson` at zero across 37k register-matched title
   pairs; this census reads it at 7, because "a brand of electric
   guitar" is what a dictionary says about Fender and a lead sentence
   says about Gibson. Where the needed relation is IS-A or stated
   synonymy (guitar, cocktail, battery), definitional structure
   carries it. The mechanism premise was right about the relation
   class it named.
2. **The five zeros name a third relation class the program has now
   measured twice from different sides: situational association.**
   `dinner↔basil`, `evening↔schedule`, `recipe↔homemade`,
   `publications↔research`, `nostalgic↔memories` are not paraphrase
   (W3-P2 read them at zero in duplicate titles) and are not
   definitional facts (this census reads them at zero in glosses and
   leads — with the vocabulary fully present). They are associations
   of situations, not of word meanings: nothing ABOUT the word
   "dinner" mentions basil; the two co-occur in kitchens and in
   running text, which is co-occurrence-statistics territory, not
   stated-fact territory. The taxonomy across the two censuses:
   two needs are paraphrase-shaped, three are definitional, five are
   situational — the classes overlap at battery, which all three
   structures carry.
3. **The pre-read prior missed optimistic for the third consecutive
   unit, and the declaration pre-registered that outcome as a
   datum.** Predicted C at 5–7 of 8; read 3. The
   `recipe↔homemade` and `nostalgic↔memories` predictions were
   wrong in the same direction W3-P's and W3-P2's were. Pre-read
   priors on this grid systematically overestimate how often a
   needed connection is explicitly present; the floors, not the
   priors, are doing the epistemic work — which is why they exist.

## What the park licenses, exactly

Nothing ships and no ship sentence is put. What is now priced by
measurement, across three units and three structures: title
paraphrase carries two of the eight needs; definitional statement
carries three; five of eight — the situational class — are carried by
neither, with the vocabulary fully known in both corpora. The bridge
MECHANISM on definitional sources remains unmeasured past the census
(no bridge was built), exactly as W3-P left pair supervision
unmeasured; what is refuted is corpus structure, not composition.
Lane W's queue after this read: W1b stays staged and is unaffected;
W2 remains the conditional endgame rung.

## The program sentence, owed and put

The W3-D declaration pre-committed this sentence on a G0 failure, and
it is put plainly: **the preference class has exhausted this
program's corpus-side routes, and the remaining distance to the
reference line is not reachable by the program's non-neural means as
declared.** Every pinned corpus has now been read under a
declaration; no unread pin remains that addresses the situational
class; and no further fetch is proposed, because the two censuses
together show the missing relation class is not one that gets STATED
in any corpus of statements — it is one that accumulates in running
text.

Scoped exactly, as the declaration wrote it: "as declared." Two
mechanism-side doors survive the sentence, both compositions over
bytes already read, neither a new corpus, and both named below
rather than opened.

## Doors — named, not opened

- **The composition unit.** The W3-P2 pair file and the W3-D edge
  file are committed-provenance derived intermediates whose reuse
  outside their declared arms requires a new declaration. A unit
  that composes both evidence kinds into one bridge table under the
  same bars would enter its census with support already published at
  five of eight needs (battery and publications from pair structure;
  guitar, cocktail, battery from definitional structure) — enough,
  on the published artifacts, to clear a four-need coverage floor
  without reading a new byte. Whether bridges that exist at census
  SURVIVE the build floors and MOVE four whole questions at the gate
  is exactly what W3-D's fan-out confound declined to predict — a
  supported census can still yield a parked gate, and that would be
  a mechanism finding. The
  door opens the standing way: by declaring first.
- **W1b, re-motivated.** The situational class is co-occurrence
  structure in running text — precisely what the wide-register
  vocabulary retrain reads. W1b stays staged with its declaration
  owed; this census gives it a sharper question than it had.
- **Lane T criterion v1** stands where the last record left it.
- Any wiring of any bridge into the package remains a separate unit
  with its own plain sentence, after its read.

## What is not claimed

No criterion claim; the interim sentence stands. No judgment of the
bridge mechanism on any source — no bridge was built. No claim that
the composition unit clears its gate — its census arithmetic is
quoted from published artifacts; its bars are not pre-judged. No
W1b entry decision. No comparative claim against any other system.
The confound-7 prediction (C at 5–7) is REFUTED and recorded, the
third consecutive optimistic prior, per its own pre-registration.
