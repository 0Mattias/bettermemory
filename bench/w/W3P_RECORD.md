# W3-P — the record: parked at the census, 2026-08-17

The unit ran exactly as declared and stopped exactly where the
declaration said a failed census stops it: before any engine read.
The chain is sha-ordered — declaration with published anatomy
(78fd0c3), reader implementation with its CI determinism leg
(298e8dd), then the census run at that commit — and the census
artifact is `bench/w/results/w3p-census-2026-08-17.json`, published
whole. Nothing below renegotiates anything; the floors were fixed at
the declaration commit and the read fell under both.

## The census, measured

One streaming pass at commit 298e8dd, the pinned archive's sha256
re-verified over the exact bytes before the first row
(`bench/w/results/w3p-census-2026-08-17.json`; 28.7 seconds for the
hash, 747.3 for the pass, inside the declared budget):

- 59,819,048 rows scanned; 24,101,803 questions and 35,603,624
  answers in the PostTypeId census the reader publishes for W1b's
  reuse.
- 40,785 rows carry the legacy "Possible Duplicate" closure
  blockquote; 40,103 yield well-formed title pairs under the declared
  rule. **Floor V fails**: 40,103 against the 50,000 floor.
- Need support: **zero of eight** anatomy needs reach the five-pair
  floor. The single strongest reading anywhere in the grid is one
  pair for the battery↔charging need; every other need reads zero.
  **Floor C fails** at 0 against 3.
- The pair vocabulary holds 15,924 terms. The overlap rows are the
  finding of the unit: 0.7457 of the preference asks' content tokens
  and 0.5435 of the miss golds' exist somewhere in that vocabulary.

**G0 verdict: PARK-AT-CENSUS**, per the declared grid. No bridge was
built, no tuning read ran, no instrument was touched. The engine is
behaviorally unchanged and the honest interim sentence stands as the
L2 record left it.

## What the census teaches

1. **The register wall is a pair-structure wall, not a vocabulary
   wall.** Confound 1 predicted the everyday-register needs would
   read near zero and allowed the two technical-adjacent needs to
   find support; the read came in harder — zero of eight, one pair
   total. And the overlap rows show why the distinction matters: the
   corpus KNOWS most of the words (0.7457 of ask tokens present) but
   never PAIRS them. Bridge supervision lives in the pair structure,
   and the pair structure is programming-register even where the
   vocabulary is general English. A corpus cannot be judged
   register-fit by its word list.
2. **The legacy-blockquote signal is thin in the modern dump.**
   40,103 pairs out of 24,101,803 questions: the old closure banner
   survives in well under two of every thousand questions — bodies
   get edited, and the modern closure mechanism never wrote into
   bodies at all. The extraction rule itself was sound (40,103 of
   40,785 marker rows yielded a well-formed pair); the corpus is
   simply thin in this signal. Labeled duplicate edges live in
   PostLinks, which the pinned archive does not carry.
3. **The reader is proven at corpus scale and is W1b's on-ramp.**
   The declared pass streamed the full archive in under thirteen
   minutes on this machine, sha-first, deterministic, with the row
   census W1b's own declaration will want. That infrastructure is the
   unit's durable product.

## What the park licenses, exactly

Nothing ships and no ship sentence is put — a park earns none. The
paraphrase-bridge MECHANISM is unmeasured, not refuted: no bridge
was built, so nothing here prices pair supervision itself. What is
priced is the corpus: the pinned StackOverflow archive cannot supply
preference-register pair structure at the declared floors, by
published count rather than by prediction. Lane W's queue after this
read: W1b (the wide-register vocabulary retrain, a different
mechanism on the same pinned bytes) stays staged and is unaffected;
W2 remains the conditional endgame rung; the preference route
continues only through a register-matched pair corpus, which is a
fetch — and fetches are the owner's.

## The program sentence, owed and put

The campaign plan bound this in advance: if L2 and the preference
route both park, say plainly that the program is in trouble. L2
parked on 2026-08-16; W3-P parked at its census today. So, plainly:
**the program is in trouble on its current corpus inventory.** Lane
L's pricing routes are exhausted by measurement, and no corpus now
pinned can reach the preference class that holds the remaining
distance to the reference line. The route that remains is not a
mechanism idea but a corpus decision, and it is one only the owner
can take.

## Owner doors

- **The register-matched pair corpus — the fetch sentence this park
  converts to.** Fetch the per-site Stack Exchange dumps for a small
  set of everyday-register sites (cooking, music, fitness,
  interpersonal skills) from the same archive.org stackexchange item
  as the existing pin — a few hundred MB compressed apiece, same
  CC BY-SA license family — whose PostLinks.xml carries labeled
  duplicate edges, replacing the legacy-blockquote heuristic
  outright and rerunning this census in the right register. Yes or
  no, one sentence; on yes, a successor unit declares its own floors
  before the bytes are read.
- **Lane T criterion v1** stands where the last record left it.

## What is not claimed

No criterion claim; the interim sentence stands. No judgment of pair
supervision as a mechanism — the census priced the corpus before the
mechanism could run, and saying more would be prediction, not
measurement. No W1b entry decision. No comparative claim against any
other system.
