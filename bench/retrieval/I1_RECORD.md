# I1 — the dev instrument expansion: the read and the park, 2026-08-18

The unit `bench/retrieval/I1_DECLARATION.md` declared ran whole. The
instrument holds **120 questions over 120 gold topics in a corpus of
1,080 documents**, the original twenty are carried byte-for-byte, and
all four gates read. Nothing about the engine moved: this unit trained
nothing, ranked nothing new, and shipped no behaviour change.

## What is committed

| file | shape | sha256 |
|---|---|---|
| `corpus.jsonl` | 1,080 documents, 120 gold, 8 near-duplicates each | `4fbe2fd59f0c41f4…` |
| `questions.jsonl` | 120 questions | `88d8d6501b22bfbb…` |
| `corpus-v2.jsonl` | the retained 180-document original | `c40acee95ce1bb70…` |
| `questions-v2.jsonl` | the retained 20 questions | — |

The retention is not decoration. Every dev-side figure the campaign
published before today was measured on the 180/20 pair, `run.py` pins
that corpus digest as `_V2_CORPUS_SHA256` to decide whether a run
reproduces a committed artifact, and I1-G1's anchor has to have
something to run against. Two tests now hold it: the digest, and that
the expanded files still literally begin with the old ones.

## The gates

**I1-G1 — integrity. PASS, exactly.** On `corpus-v2.jsonl` +
`questions-v2.jsonl` the three declared anchors reproduce to the digit
(`results/i1-anchor-{off,tables}-2026-08-18.json`):

| arm, asked probe | recall@1 | recall@5 | declared |
|---|---|---|---|
| expansion off | 35% | 60% | 35% / 60% ✓ |
| static hand tables | 55% | 90% | 55% / 90% ✓ |
| requery, expansion off | 80% | 100% | 80% / 100% ✓ |

**I1-G2 — power. PASS.** The paired resolution floor is **6 questions
= 5.0 points** at n=120, against 6 questions = 30 points at n=20. The
gate asked for ≤ 6 points. This is arithmetic on n, not a measurement.

**I1-G3 — structure. PASS, with a deviation this record declares
rather than hides.** All 39 tests in `tests/test_bench_retrieval.py`
pass. The declaration allowed exactly two count assertions to change
and "nothing else touched"; four things changed, and none of them
weakened an assertion:

1. `== 20` → `== 120` and `>= 150` → `>= 1000`. The allowed edit.
2. `test_prefilter_artifacts_are_internally_consistent` resolved the
   artifact's corpus **by digest** instead of by filename. Four
   committed artifacts record `"corpus": "corpus.jsonl"` at digest
   `c40acee9…`; that filename now holds different bytes and those
   exact bytes live at `corpus-v2.jsonl`. Checking the digest against
   the corpora actually in the tree keeps the property the line exists
   for — the corpus an artifact was measured on is still here, byte
   for byte — and is stronger than trusting a name that can be reused.
3. `test_the_prefilter_really_engages_on_every_committed_question`
   now names `--corpus corpus-v2.jsonl --questions questions-v2.jsonl`.
   It reproduces a golden measured on the 180-document corpus and
   asserts `n == 20` in its own body, so it was always about that
   instrument; it had simply never had to say so.
4. Two tests added: the v2 retention, and the byte-prefix check on
   §3.1's freeze.

Items 2 and 3 are the same defect — two tests said "the corpus on
disk" when they meant "the corpus this artifact names", which was
unambiguous while there was one corpus and stopped being so today.
A reviewer who thinks that reading is too convenient should say so;
the alternative reading is that a corpus may never be expanded, which
the declaration rejected.

**I1-G4 — difficulty is not reset. PASS.** Full-120 expansion-off
asked recall@1 is **21.7%** (95% CI [15.2%, 29.9%]), inside the
declared band [18%, 57%]. The gate guarded against the instrument
getting *easier*; it got harder, and the next section prices why.

## The expansion did not change what the instrument measures

Confound 1 predicted the detector: if the full-120 diverges sharply
from the original-twenty subset on the same arm, the new topics differ
in kind. Scored in the same 1,080-document field, expansion off, asked:

| cell | recall@1 | recall@5 |
|---|---|---|
| original twenty | 20.0% | 40.0% |
| full 120 | 21.7% | 47.5% |

The new hundred behave like the old twenty. The drop from the
original instrument's 35% is **dilution, not difficulty**: the same
twenty questions fall 35% → 20% purely from adding 900 competitors,
and the new topics land on top of them. The register held.

## What the instrument now says, that it could not say before

**The static hand tables' advantage does not replicate.** This is the
result the unit was built to make sayable, and it goes against the arm
the campaign has carried.

| instrument | tables minus off, asked | @1 | @5 |
|---|---|---|---|
| 20 questions / 180 docs | point estimates | +20 pts | +30 pts |
| 120 questions / 1,080 docs | paired, McNemar exact | **net 0 of 120, p=1.000** | **net +4 of 120, p=0.289** |

Neither is measurable, on an instrument whose floor is 5 points. This
is not the small-sample excuse `bench/POWER_AUDIT.md` applied to the
old reading — the audit showed the +20 was never *measurable*; this
shows it is not *there* at six times the scale.

The tables are general by construction — `src/bettermemory/expansion.py`
says so in its own comment, "nothing corpus-specific belongs here",
and the groups are ordinary dev vocabulary (`auth`/`authentication`,
`db`/`database`). So their benefit was expected to apply across all
120 topics, which is what makes this the right test and a powered one.
A +20-point general effect would have moved about 24 questions. It
moved none.

**What this does NOT establish**, stated because the temptation is
real: the corpus grew 6× at the same time the question count did, so
the honest claim is that the advantage does not survive a
1,080-document field — not that it never existed at 180. Both arms
were scored on the same field here, so the comparison is clean; what
is confounded is the comparison *between instruments*. Untangling
scale from breadth needs its own unit and does not happen here.

**The requery finding replicates, overwhelmingly.**

| | 20 questions | 120 questions |
|---|---|---|
| requery vs asked @1 | +9, p=0.022 | **+58 of 120, p=7.7e-14** |
| requery vs asked @5 | +8, p=0.008 | **+55 of 120, p=3.3e-14** |

The README's central claim — that typing the content words a document
contains buys a large, real lift — is now the best-supported finding
this instrument has.

**The control finding mostly replicates, with one number to watch.**
Control vs asked is net 0 of 120 at recall@5 (p=1.000), reproducing
"stripping interrogatives buys nothing". At recall@1 it is **+5 of
120, p=0.063** — control slightly *ahead* of asked, just under the
threshold. On 20 questions this cell was exactly 0. It is not a
finding and is not claimed as one; it is recorded so that the next
read on this instrument knows to look at it.

**A new fact about the prefilter, not previously visible.** At 1,080
documents the corpus crosses the 500-memory index threshold, so the
default install now runs the prefilter path on this instrument. The
FTS5 slice, capped at 50, **drops one gold document in five before
ranking**: gold-in-pool is 0.80 on the asked probe and 0.81 on
control, against 0.90–1.00 at 180 documents. Recall loss is 0.0 points
at both depths, because the golds it discards were not being retrieved
anyway — but the margin between "nominated but unranked" and "never
nominated" has been spent, and at the next corpus size it will not be
there. Requery keeps 1.00 nomination, which is the same lesson the
requery finding teaches, arriving through a different door.

## Confound 2, as declared

The expanded instrument runs in the prefilter regime by default where
the old one did not. Both regimes are reported in every full-120 and
subset cell above, and G1's anchor runs on the original 180-document
corpus so the declared numbers are compared like for like.

## Owner doors — still closed

Unchanged from the declaration. Whether the campaign's standing bars
are restated against this instrument is a charter question, not this
unit's — and it is now a live one, because G1's "60" was derived from
the twenty-question granularity and the arm it was written against
just read as no better than expansion-off. `bench/heldout/data2/`
stays reserved for P2a; this unit did not touch it.

## What is not claimed

No engine change and no ship. No claim that the hand tables are
harmful — the read is "not measurable", in both directions. No claim
that 120 questions is sufficient for every question the campaign might
ask. No comparative claim against any other memory system.
