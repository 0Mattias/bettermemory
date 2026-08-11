# LongMemEval — session-level retrieval on third-party labels

Build-order item (e). `bench/retrieval/` ends by disclaiming a
comparative claim; this directory exists to earn one, on a corpus and
against labels that neither this project nor claude-mem authored.

```sh
.venv/bin/python bench/longmemeval/run.py --limit 20      # smoke
.venv/bin/python bench/longmemeval/run.py                 # full, ~27 min
.venv/bin/python bench/longmemeval/run.py --json
.venv/bin/python bench/longmemeval/run.py --json \
  --per-question results/per-question/YYYY-MM-DD.json     # + per-question sidecar
```

(Paths are resolved relative to this directory, same rule as `--corpus`.)

`--per-question` writes a second file: this run's meta (corpus, sha,
notes) plus, per arm, one record per scored question — `qid`, `type`,
`n_evidence`, `evidence_ranks`, `n_ranked`. `evidence_ranks` holds each
evidence session's 0-based rank in the distinct-session ranking, `null`
when it never surfaced within the retrieval depth. Every published
aggregate is a function of those fields:

```
recall@k          = |{r in evidence_ranks : r is not None and r < k}| / n_evidence
complete@k        = recall@k == 1        partial@k = 0 < recall@k < 1
depth-truncated@k = n_ranked < k
```

The summary emit keeps its own shape, so per-question records are a
separate dated artifact rather than a change to the published one.

Sidecars live in `results/per-question/`, one directory below the
summaries, and that placement is deliberate: `tests/test_number_claims.py`
globs `bench/*/results/*.json` one level deep to build the pool of
numbers a prose claim may pin against. A file holding 1,000 rank integers
would let almost any small number in any document find a "pin", which is
the one thing that guard exists to prevent. Sidecars are analysis input,
not citable evidence.

The corpus is not vendored (265 MB). Fetch it:

```sh
mkdir -p bench/longmemeval/data && cd bench/longmemeval/data
curl -LO https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
```

**Read [PREREGISTRATION.md](PREREGISTRATION.md) first.** It fixes the
attribution rule, the metric, and five falsifiable predictions, and it
was committed before the corpus was downloaded. Two of those predictions
are scored **MISSED** below, one of them against a shipped default.

## 4.0.0 restatement — behind by 2.3, lexical-only, 2026-08-09

The 4.0.0 purist strip removed the embedding lane from the product, and
with it the `semantic` arm this file's best-arm-vs-best-arm comparison
leaned on (the runner now drops a requested `semantic` arm with a note
instead of measuring a lane that no longer exists). The comparative
claim is restated without the tie:

**bettermemory retrieves with deterministic lexical code only and scores
89.3% macro recall@5 on this harness; claude-mem's embedding-native
stack scores 91.6%. We are 2.3 points behind, by our own measurement.**

The sections below stay as the dated record that earned the numbers.
Their "parity" framing compared an arm the product no longer ships and
must not be quoted as current. Closing those 2.3 points with code — no
borrowed weights — is the standing retrieval campaign.

## 5.1 rescue-expansion lane — the held-out check fired its kill, 2026-08-09

The campaign's first lane (filler df-floor + confidence-gated
vocabulary leg; developed and tuned entirely on `bench/retrieval`,
which its README states in bold) was checked here against predictions
committed before the run — PREREGISTRATION.md addendum 3. **The kill
criterion fired. The lane ships opt-in
(`[behavior] rescue_expansion`, default off), and this section is the
record of why.**

Provenance, from the artifacts rather than from memory: the three
lane rows ran at `6e87fad` (the 5.0.0 engine with the lane commit
`d78a620` in its history) and the baseline row at `9b68e74`, the
pre-lane engine — same corpus digest and harness throughout. Raw JSON
in `results/`:

| configuration | macro@1 | macro@5 | macro@10 | artifact |
| --- | --- | --- | --- | --- |
| baseline (lane absent) | 0.5246 | 0.8935 | 0.9443 | `baseline-reproduced-2026-08-09.json` |
| lane, both mechanisms | 0.4752 | **0.8770** | 0.9471 | `rescue-expansion-2026-08-09.json` |
| filler df-floor only | 0.5226 | **0.8935** | 0.9463 | `rescue-expansion-ablate-fcap-only-2026-08-09.json` |
| expansion leg only | 0.4732 | 0.8790 | 0.9471 | `rescue-expansion-ablate-leg-only-2026-08-09.json` |

### Predictions scored

Predictions are quoted verbatim from addendum 3. Correction
(2026-08-10): this table previously restated P6 as "or the lane does
not ship **default-on**", which is not what was preregistered — the
original says "the lane does not ship", full stop. The weaker wording
is restored to the committed text and the gap between it and what
shipped is stated in the row rather than written out of the criterion.

| # | prediction | outcome |
| --- | --- | --- |
| P6 | macro@5 ≥ 0.8900 or the lane does not ship | **KILL FIRED — 0.8770.** Partially honoured: the DEFAULT did not ship, but the lane shipped behind `[behavior] rescue_expansion`. That opt-in compromise was chosen after seeing the number and is a deviation from P6 as written, recorded here rather than folded into the criterion. |
| P7 | transfer small but real: macro@5 in [0.8930, 0.9050] | **MISSED, low** — the lane transferred harm, not help |
| P8 | the gate protects recall@1: within ±2 points of 0.5246 | **MISSED — −4.9 points.** The gate's dev-set calibration did not transfer: colloquial questions are low-coverage by nature, so the leg engaged broadly here |

### The ablation, and what it isolates

The two mechanisms were rerun separately through the identical
harness (a two-line driver patches the imported engine: the coverage
gate to never-engage for the floor-only arm; the filler table to
empty, with the leg forced on at the `search()` call site, for the
leg-only arm — the force matters, see the discard note below).

- **The filler df-floor is corpus-shape-neutral**: macro@5 identical
  to baseline to four decimals, macro@1 within a question. The
  mechanism's premise ("memory bodies are technical prose, filler is
  corpus-rare") simply stops mattering on conversational bodies where
  filler is genuinely common — `max(real df, floor)` converges on the
  real statistics.
- **The expansion leg carries the entire regression.** Per-question:
  25 questions moved down, 9 up; four `single-session-assistant`
  questions fell from 1.00 to 0.00 — the evidence session pushed
  clean out of the top five. The mechanism is legible in the queries:
  "…the hostel you *recommended* last time" engages the gate (long
  colloquial questions are low-coverage), and the leg's inflection
  variants ("recommended" → "recommend", "planning" → "plan") are
  PROMISCUOUS matchers in a store where hundreds of rounds contain
  those verbs — the exact inverse of the technical gold set, where
  expansion vocabulary is rare and discriminating. The cleanest
  single proof is question `a89d7624`, which contains no filler-list
  word at all (the floor cannot touch it) and still fell 1.00 → 0.00.

One discard, recorded rather than hidden: the first leg-only run was
invalidated before publication — it raced a concurrent working-tree
edit that flipped the engine's default, imported the flipped module,
and measured pure baseline while claiming to measure the leg
(baseline-identical numbers across all three k's are what gave it
away). The published leg-only artifact is the rerun with the leg
forced at the call site.

Correction (2026-08-10): this paragraph previously said the rerun was
"generated from the clean committed tree", and named the `tree_dirty`
flag as what exposed the discarded run. Both published ablation
artifacts carry `tree_dirty: true`, so neither claim survives contact
with the files. They are dirty BY CONSTRUCTION — an ablation is a
two-line driver patch on the imported engine, and that patch is not
committed, so a clean-tree ablation is not a thing this harness can
produce. What actually exposed the discarded run was the numbers, not
the flag. The two ablation rows are therefore the one place in this
directory where a reader is asked to trust a working-tree edit; the
patch is described in full above so it can be reapplied, and the
`tree_dirty: true` in each payload is the honest marker that it was.

Reproducing the lane row: `--rescue-expansion on` (added 2026-08-10 —
the artifacts predate it, and were lane-on because `6e87fad` still
shipped the lane default-on; `fe57f05` flipped that, which left the
published row unreachable from this runner until the flag existed).
Note that the 5.1 filler-stem fix changed what the leg emits: the
rule source no longer regenerates listed filler words, which is a
different engine from the one these rows measured. A re-run therefore
measures the CURRENT lane, not this kill — and re-earning the default
needs a fresh preregistration on both instruments, not a better
number from a changed engine.

### What this buys the campaign

The lane stays shipped, opt-in, for stores shaped like the gold set —
technical prose queried casually, where it is worth +15/+30
recall@1/@5 as-asked. The default stays off until an experiment earns
it: the promiscuous-variant failure class is DETECTABLE IN CODE
(an emitted expansion term's document frequency in the pool is known
before it gets a single vote), so the named next experiment is
df-gating the emitted terms, re-preregistered on both instruments.
macro@10 IMPROVING under the lane (0.9443 → 0.9471) while @1/@5 fall
is the shape of a recall mechanism with a precision problem — the
vocabulary reach is real; the votes land too bluntly.

## Round 2 — the df gate was preregistered and its pre-run kill fired, 2026-08-10

Round 1 closed by naming one experiment: df-gate the EMITTED expansion
terms, the class detectable in code, and re-preregister on both
instruments. That experiment was preregistered
(`PREREGISTRATION.md` addendum 4, τ = 0.05 fixed from corpus
statistics with no recall input) and **it is dead. No gate was
implemented and no gated arm ran, because the pre-run check said the
mechanism cannot work.**

### What Gate 0 asked, before any gate existed

The hypothesis was that an emitted term's document frequency separates
the vocabulary that helps a technical store from the vocabulary that
harms a conversational one — that `"planning" → "plan"` is common here
and the gold set's synonyms are rare there. That is checkable from
corpus structure alone, so it was checked first
(`results/df-census-2026-08-10.json`, statistics only; verdict in
`results/gate0-2026-08-10.json`, recomputable by `gate0.py` from
committed artifacts).

**Gate 0a — separability: FAILED, and the sign is backwards.**

| population | median df/N | n terms |
| --- | --- | --- |
| the 25 regressed held-out questions | **0.0268** | 76 |
| dev set, leg-engaging asked probes | **0.0361** | 66 |

Required: ≥ 5×. Measured: **0.74×**. The terms that broke this corpus
are *rarer* than the ones that carry the gold-set win, not five times
more common. All four readings of "the dev set's rescued questions"
were computed so the flattering one could not be chosen afterwards;
they span 0.74×–0.80× and every one fails.

**Gate 0b — reachability: FAILED.** At τ = 0.05 the gate alters the
emitted set on **17 of 25** regressed questions; ≥ 20 was required. It
does change `a89d7624`, the question round 1 called the cleanest single
proof — that one emits `plan` at df/N 0.1235 — but the poster child is
necessary, not sufficient.

**And no τ rescues it.** The sweep is the substance of the kill:

| τ | regressed questions altered (of 25) | dev-set engaged probes altered (of 38) |
| --- | --- | --- |
| 0.02 | 24 | 35 |
| 0.05 | 17 | 34 |
| 0.10 | 7 | 21 |
| 0.20 | 1 | 8 |
| 0.35 | 0 | 2 |

Every τ that reaches the failure hits the dev set at least as hard.
The two populations occupy the same df/N band, which is precisely the
"DEAD ON ARRIVAL" condition addendum 4 declared in advance.

### What this retires, and what it does not

**Retired: document frequency as the separating variable for emitted
expansion terms.** Not "untuned" — measured, on both corpora, with the
threshold fixed beforehand. The promiscuity story in the round-1
section above is a good description of the *mechanism* (`plan` really
does match many rounds here) and a bad *predictor*: promiscuity in a
chat store is not what df measures, because the harmful terms are
individually rare and the damage comes from how the leg VOTES rather
than from how common its vocabulary is. `_hybrid_fuse` fuses by rank,
so a leg built of rare-but-wrong terms still emits a confident rank-1
and still gets 0.7 of a vote — the argument addendum 4 makes for why
df-gating could help is the same argument for why df-gating cannot.

**Not retired: the lane.** `rescue_expansion` stays opt-in and
unchanged; nothing here touches a shipped default.

**Not retired: the campaign.** What the census does say is that the
next mechanism has to key on something other than a term's corpus
frequency — the leg's *influence* rather than its vocabulary. Two
candidates the data points at, neither preregistered here: capping the
leg's rank contribution when its own top candidate is weak, and
nominating on query + expansion terms above the index threshold (the
increment addendum 4 explicitly scopes out, since it changes the pool
rather than the votes).

### One correction to the round-1 record

That section says the leg "engaged broadly" on conversational
questions. Measured by the census, the coverage gate opens on **165 of
500** questions here — 33%, not most — against 43 of 60 dev probes.
The lane's damage is concentrated, not diffuse, which is consistent
with 25 questions moving down and 9 up.

### The 5.1.1 re-baseline underneath all of this

Every round-1 constant was re-measured on the current engine rather
than copied, through the new committed `--ablate` flags:

| arm | macro@1 | macro@5 | macro@10 | vs round 1 |
| --- | --- | --- | --- | --- |
| baseline, lane off | 0.5246 | 0.8935 | 0.9443 | identical |
| lane on | **0.4772** | 0.8770 | 0.9471 | @1 was 0.4752 |
| filler df-floor only | 0.5226 | 0.8935 | 0.9463 | identical |
| expansion leg only | 0.4732 | 0.8790 | 0.9471 | identical |

The 5.1.1 filler-emission repair is worth **+0.0020 macro@1** here and
nothing at @5 — directionally right, nowhere near the 0.8900 kill
line. The leg-only arm reproducing round 1 exactly is the internal
check that says so honestly: that arm empties the filler table, which
neutralises the 5.1.1 filter by construction, so identity is what a
correct ablation had to produce. Every dev-set cell is unchanged too.

## Round 3 — capping the leg's vote: the first mechanism to move this corpus, and still a kill, 2026-08-10

Round 2's kill said the harmful terms are individually rare, so the
damage is in the leg's VOTE rather than its vocabulary. Addendum 5
preregistered the obvious consequence — make the leg earn its vote —
and ran it. **It moved the number in the right direction for the first
time, and it did not clear the line. The default does not flip.**

The mechanism: `_hybrid_fuse` fuses by rank, so the leg contributes
`_RESCUE_LEG_WEIGHT / (rrf_k + rank)` however thin its rank-1 is. The
cap withholds the leg entirely when its own separation
`(top − runner_up) / top` falls below θ = 0.12, which makes the query
byte-identical to `rescue_expansion=False`. θ was fixed from the dev
set's leg census before the code existed.

### Arms

| arm | macro@1 | macro@5 | macro@10 |
| --- | --- | --- | --- |
| baseline, lane off | 0.5246 | 0.8935 | 0.9443 |
| lane on, cap off | 0.4772 | 0.8770 | 0.9471 |
| **lane on, cap on (θ=0.12)** | **0.4916** | **0.8830** | **0.9466** |
| `--ablate leg-only` + cap on | 0.4936 | 0.8870 | 0.9446 |

Both determinism checks reproduced to four decimals, so the comparison
is licensed. The cap recovers **30% of the macro@1 loss** (+0.0144 of
0.0474) and **36% of the macro@5 loss** (+0.0060 of 0.0165). Real,
repeatable, and not close to enough: 0.8830 against a 0.8900 kill line
that has now stood through two rounds.

### Scored predictions

| # | prediction | measured | outcome |
| --- | --- | --- | --- |
| P16 | cap fires on ≥ 40% of engaging questions | **43.9%** (69 of 157) | **HELD** |
| P17 | dev win survives: asked ≥ 45%/85%, requery exactly 80%/100% | asked 50%/**75%**, requery 80%/100% | **MISSED** — 3 questions at @5 |
| P18 | macro@5 ≥ 0.8900 — **the kill criterion** | **0.8830** | **MISSED — KILL FIRED** |
| P19 | macro@1 ≥ 0.5046 | **0.4916** | **MISSED** |
| P20 | null shape: @5 ∈ [0.8900, 0.8970], @1 ∈ [0.5046, 0.5346] | 0.8830, 0.4916 | **MISSED**, both low |
| P21 | macro@10 ≥ 0.9443 | **0.9466** | **HELD** |

Kill criteria 4 (macro@5 below the line) and 5 (dev set loses more than
one question) both fired. Two of six predictions held.

### Why it failed, and the confound that called it

**Addendum 5's confound 2 named this failure in advance:** the dev
census labels a leg "correct" when its rank-1 is the gold document, and
a leg whose rank-1 is wrong can still push the gold document up the
fused list. So "incorrect" was an upper bound on "harmful", and θ was
free to be more aggressive than the harm warranted. It was. θ = 0.12
preserves 12 of the 14 legs whose rank-1 is gold — and withholds **25
of the 41 engaged dev legs, 61%**. The 16 surviving legs are not enough
to hold 90% recall@5, and the dev set drops to 75%.

The transfer picture is the sharper lesson. Measured after the arms
ran, the two corpora's engaged legs are separated *differently*:

| | p25 | p50 | p75 |
| --- | --- | --- | --- |
| dev-set engaged legs | 0.0403 | 0.0698 | 0.1893 |
| held-out engaged legs | 0.0568 | **0.1359** | 0.2627 |

θ = 0.12 sits **above** the dev median and **below** the held-out
median. The same constant is therefore aggressive on the corpus it was
derived from and permissive on the corpus it was aimed at — the exact
inversion C1 warns about, arrived at through a signal specifically
chosen to be scale-free. Scale-free was necessary and not sufficient:
the ratio does not depend on collection size or IDF scale, but its
*distribution* still differs by corpus, and a fixed quantile is not a
fixed value.

### What this retires, and what it does not

**Not retired: the mechanism.** Conditioning the vote is the first
thing in three rounds to move this corpus toward baseline instead of
away from it, on both the full lane and the leg-only ablation. The
direction is established.

**Retired: a fixed global θ.** A constant calibrated on one corpus's
leg-separation distribution and applied to another's is the third
version of the same mistake — the df threshold, the coverage gate's
transfer, and now this. What the numbers point at is a **relative**
criterion: withhold the leg when its separation is weak *for this
store*, e.g. against a quantile of the legs that store's own queries
produce, so the mechanism carries its calibration with it instead of
importing one. That is not preregistered and no number here licenses
it.

**Also worth recording:** the leg-only ablation with the cap on
(0.8870) beats the full lane with the cap on (0.8830). With the cap
present the filler df-floor is costing 0.0040 macro@5 — a small,
repeatable interaction between two mechanisms that were each measured
neutral or positive alone, and a reminder that this lane now has three
interacting parts.

## Round 4 — the cap self-calibrates, and it was not enough, 2026-08-10

Round 3 failed on calibration: a fixed threshold fired on 61% of dev
legs and 43.9% of held-out ones. Addendum 6 replaced it with a
criterion drawn entirely from the leg being judged — the top adjacent
gap over the mean of the leg's other gaps, K = 2.5, over the top 12
candidates.

**The calibration problem is solved. The recall problem is not.**

### Arms

| arm | macro@1 | macro@5 | macro@10 |
| --- | --- | --- | --- |
| baseline, lane off | 0.5246 | 0.8935 | 0.9443 |
| lane on, no cap | 0.4772 | 0.8770 | 0.9471 |
| **lane on, standout cap** | **0.4896** | **0.8790** | **0.9466** |
| standout cap, `--ablate floor-off` | 0.4876 | 0.8790 | 0.9446 |
| standout cap, `--ablate leg-only` | 0.4896 | 0.8830 | 0.9446 |
| *(round 3, fixed θ = 0.12)* | *0.4916* | *0.8830* | *0.9466* |

Both determinism arms reproduced to four decimals.

### Scored predictions

| # | prediction | measured | outcome |
| --- | --- | --- | --- |
| P22 | firing rate within ±10 pts of dev's 41.5% | dev **41.5%**, held-out **39.5%** — **2.0-point gap** | **HELD** |
| P23 | every dev cell exactly unmoved | asked 90%→**80%**, control 85%→**75%** | **MISSED** |
| P24 | macro@5 ≥ 0.8900 — **kill criterion** | **0.8790** | **MISSED — KILL** |
| P25 | beats round 3 (>0.8830, >0.4916) | 0.8790, 0.4896 | **MISSED**, both |
| P26 | floor-off ≥ floor-on at macro@5 | 0.8790 = 0.8790 | **HELD** |
| P27 | macro@10 ≥ 0.9443 | **0.9466** | **HELD** |
| P28 | null band @5 [0.8900, 0.8990], @1 [0.5046, 0.5346] | 0.8790, 0.4896 | **MISSED**, both |

Three of seven held. Kill criteria 3 (below the line), 4 (dev cells
moved) and 6 (no gain over round 3) all fired.

### The finding: self-calibration was achieved and was not sufficient

**P22 is the hypothesis, and it held with room to spare.** The rule
fires on 41.5% of engaged dev legs and 39.5% of engaged held-out ones —
a **2.0-point** gap where round 3's fixed threshold spanned **17.1**.
A criterion drawn from the leg's own gap structure really does carry
its calibration across corpora with different score distributions. That
is a real, reusable result about the *form* of a threshold.

**And the recall did not follow.** 0.8790 is *below* round 3's 0.8830,
on a rule that fires at a more consistent rate and preserves more of
the dev set. So firing at the right RATE is not the same as firing on
the right LEGS. The fixed θ was aggressive in a way that happened to
catch more harmful legs on this corpus; the self-calibrating rule
spreads its withholding more evenly and catches fewer of them. Round
3's advantage was not calibration — it was aggression, and aggression
is what the dev set was paying for.

**Confound 2 predicted the dev-set cost for the second time.** The
census labels a leg "correct" when its rank-1 is the gold document, and
K preserves all 14 such legs — yet the dev set still lost two questions
at recall@5 (round 3 lost three). A leg whose rank-1 is wrong can still
lift the gold document, and no rule keyed on the rank-1 proxy can see
that. **The proxy, not the threshold, is now the binding constraint on
this whole family.**

### What the floor arm settled

Round 3 read `leg-only + cap` (0.8870) above `full lane + cap`
(0.8830) and attributed 0.0040 to the filler df-floor. **That
attribution was wrong**, and the clean arm says so: with the floor
disabled and the table intact, macro@5 is *identical* to floor-on
(0.8790), while @1 and @10 are slightly worse. The floor costs
nothing under a cap.

What round 3 actually measured was the `leg-only` confound — emptying
the filler table disables the floor **and** the 5.1.1 emission filter
together. Round 4 reproduces it: `leg-only + standout` reads 0.8830
against 0.8790 with the table intact. So the 0.0040 belongs to putting
filler back INTO the emitted terms, not to removing the floor. That is
a real effect and an uncomfortable one — the 5.1.1 filler-emission
repair, which is right on principle and neutral at @1, costs this
conversational corpus 0.0040 at @5 under a cap. Recorded, not acted
on: no preregistration covers it.

### Where this leaves the campaign

**Retired: threshold form as the lever.** Three rounds have now moved
the threshold — a df level, a fixed margin level, a self-calibrating
shape — and the held-out ceiling has moved 0.8770 → 0.8830 → 0.8790.
The mechanism family is real but bounded, and the binding constraint
has moved off the threshold entirely.

**What the data points at instead** is the correctness signal itself.
Every rule so far has been calibrated against "the leg's rank-1 is the
gold document", which confound 2 has now cost two rounds running. A
criterion trained on "did the leg IMPROVE the fused result" — which the
per-question sidecars can label directly, without a new instrument —
is the obvious next thing to try, and nothing here licenses it.

## Round 5 — evidence instead of a threshold, and the arc closes, 2026-08-10

Round 4 retired threshold form and named the binding constraint: every
rule so far was fitted to a proxy. Addendum 7 removed it.

`bench/leg_labels.py` runs the shipped ranker twice per dev question —
leg voting, leg withheld — and records where the gold document lands.
**Of 39 engaged dev legs: 21 help, 3 hurt, 15 are neutral.** Rounds 3
and 4 withheld 25 and 17 legs to catch those three, and against the
true labels they were paying **9** and **7 helpful legs** to do it.
Every harmful leg rested on a rank-1 matching exactly ONE synthesized
term; no helpful leg did. So the rule became a count: **the leg votes
only if its rank-1 matched at least two synthesized terms.**

### Arms

| arm | macro@1 | macro@5 | macro@10 |
| --- | --- | --- | --- |
| baseline, lane off | 0.5246 | 0.8935 | 0.9443 |
| lane on, no cap | 0.4772 | 0.8770 | 0.9471 |
| **lane on, evidence rule** | **0.5014** | **0.8823** | **0.9476** |
| `--ablate leg-only` + evidence | 0.4954 | 0.8823 | 0.9456 |

Both determinism arms reproduced to four decimals.

### Scored predictions

| # | prediction | measured | outcome |
| --- | --- | --- | --- |
| P29 | every dev cell identical to uncapped | asked @1 **50→55%**, control @1 **45→50%**, all @5 held | **MISSED** — both moves are UP |
| P30 | macro@5 ≥ 0.8900 — **kill criterion** | **0.8823** | **MISSED — KILL** |
| P31 | beats round 3 (>0.8830 @5, >0.4916 @1) | 0.8823 (**−0.0007**), 0.5014 | **MISSED** @5, **HELD** @1 |
| P32 | macro@1 ≥ 0.5046 | **0.5014** (short by 0.0032) | **MISSED** |
| P33 | macro@10 ≥ 0.9443 | **0.9476** | **HELD** |
| P34 | fires ≤ 45%, within ±15 pts of dev's 31.7% | held-out **47.1%**, gap **15.4** | **MISSED**, both bounds |

Kill criteria 3, 4, 5 and 6 fired.

### What round 5 got, and what it did not

**Got: the dev set, properly.** This is the only rule in the arc that
costs the gold set nothing — recall@5 holds at 90%/85%/100%, and
recall@1 *gains* a question on both casual probes. Rounds 3 and 4 lost
three and two questions at @5. Removing the proxy did exactly what the
labels said it would.

**Got: the best held-out @1 and @10 of the arc.** 0.5014 recovers
**51%** of the uncapped lane's macro@1 loss, against 30% and 26%. And
@10 (0.9476) is the highest figure any arm has produced, above
baseline.

**Did not get: the line.** macro@5 0.8823 is 0.0077 short of 0.8900,
and 0.0007 *below* round 3. **The default does not flip.**

**Did not get: structural transfer.** P34 was the claim that a count
has no distribution to shift. It does: the rule fires on 31.7% of dev
legs and **47.1%** of held-out ones, a 15.4-point gap — wider than
round 4's 2.0 and nearly round 3's 17.1. How many terms a leg's rank-1
matches depends on how much expansion vocabulary the corpus overlaps,
which varies by corpus exactly like a score distribution does. The
argument was wrong and the prediction caught it.

### The plateau, which is the arc's real finding

| round | mechanism | held-out @5 |
| --- | --- | --- |
| — | lane on, no cap | 0.8770 |
| 4 | self-calibrating standout | 0.8790 |
| 5 | evidence count | 0.8823 |
| 3 | fixed margin level | 0.8830 |
| — | baseline (lane off) | 0.8935 |

**Three structurally different withholding rules — a level, a shape,
and a count — land within 0.004 of each other**, recovering between a
third and a half of the lane's damage and none of them reaching
baseline. That is a ceiling, not a tuning problem. Conditioning *which
legs vote* cannot fix a lane whose remaining harm is in *what the legs
contain*: the expansion vocabulary itself is wrong for conversational
stores, which is what C1 said from the beginning when identical code
flipped sign between the two corpora.

**The lane is closed as an experimental line.** It ships opt-in with
the evidence rule, which is its best form: strictly better than
uncapped on the dev set, and the best @1/@10 it has produced on the
held-out set.

## The pre-4.0 headline: parity, not victory (dated record)

On third-party ground, against labels neither party authored,
**bettermemory and claude-mem retrieve about equally well.**

| system / arm | @1 | @5 | @10 |
| --- | --- | --- | --- |
| bettermemory lexical | 52.5% | 89.3% | 94.4% |
| bettermemory semantic | **56.2%** | **91.8%** | 95.6% |
| claude-mem lexical | 0.1% | 0.1% | 0.1% |
| claude-mem semantic | 54.2% | 91.6% | **96.9%** |

Best arm against best arm, macro recall@5: **91.8% vs 91.6% — a
+0.2-point difference.** At recall@1 we are ahead by 2.0. At recall@10
**they are ahead by 1.3.** Per question type the two trade places:

| question type | bettermemory | claude-mem | Δ | n |
| --- | --- | --- | --- | --- |
| single-session-assistant | 100.0% | 100.0% | — | 56 |
| single-session-preference | 96.7% | 96.7% | — | 30 |
| temporal-reasoning | 86.1% | 86.2% | −0.1 | 133 |
| knowledge-update | 98.1% | 95.5% | +2.6 | 78 |
| single-session-user | 97.1% | 92.9% | +4.3 | 70 |
| multi-session | 86.7% | **89.3%** | **−2.6** | 133 |

**This is the answer to build-order item (e), and it is not the answer
the build order was hoping for.** Item (e) existed to establish that
bettermemory out-retrieves claude-mem on neutral ground. It does not. It
ties. Any competitive claim this project makes has to rest somewhere
other than retrieval recall — see "What this means" below.

Run validity, since three earlier runs were discarded for exactly this:
`chroma: {"embedded": 124361, "complete": true}`, ingest shortfall
0.000%, **zero questions returning empty** in the semantic arm.

## Results — bettermemory 3.30.0, `longmemeval_s_cleaned.json`

500/500 instances scored. sha256
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`.
Retrieval depth 200 items, collapsed to distinct sessions. Raw JSON in
`results/`.

Session-level recall@k, macro-averaged, **[ceiling]** = maximum
achievable at that k given questions with more evidence sessions than k:

| arm | @1 | @5 | @10 |
| --- | --- | --- | --- |
| lexical | 52.5% **[64%]** | 89.3% [100%] | 94.4% [100%] |
| semantic | **56.2%** **[64%]** | **91.8%** [100%] | **95.6%** [100%] |

Micro-averaged (evidence-weighted):

| arm | @1 | @5 | @10 |
| --- | --- | --- | --- |
| lexical | 43.6% | 86.7% | 93.2% |
| semantic | 46.6% | 89.0% | 94.4% |

**The @1 ceiling is 64%, not 100%**, because 324 of 500 questions need
two or more evidence sessions and only one slot exists. So lexical@1 is
82% of what is arithmetically reachable, not 52% of a possible 100.

### By question type, macro recall@5

| question type | lexical | semantic | n | Δ |
| --- | --- | --- | --- | --- |
| single-session-assistant | 100.0% | 100.0% | 56 | — |
| knowledge-update | 98.1% | 98.1% | 78 | — |
| single-session-user | 97.1% | 97.1% | 70 | — |
| multi-session | 84.9% | 86.7% | 133 | +1.8 |
| temporal-reasoning | 83.7% | 86.1% | 133 | +2.4 |
| **single-session-preference** | 73.3% | **96.7%** | 30 | **+23.3** |

Cost: lexical 328 s, semantic 1,286 s — the embedding arm is **~4×**
slower for +2.5 points pooled.

### Re-run at the 3.43.0 engine, 2026-08-08

The identical invocation at commit `7b63e07` reproduces every figure
above **bit-for-bit** — macro, micro, and ceilings, both arms, all k
(`results/baseline-both-arms-2026-08-08.json`, provenance-stamped;
per-question records under `results/per-question/`). Nine releases of
engine change did not move session-level recall on this corpus, and
the identity doubles as a determinism check on the harness. The
claude-mem side of the comparison is unchanged and stays dated: those
artifacts remain the 2026-07-27 measurements of `claude-mem@13.12.4`
(the tooling is no longer installed here), so the headline table reads
current bettermemory against claude-mem as of 2026-07-27.

## Predictions scored

| # | prediction | outcome |
| --- | --- | --- |
| P1 | claude-mem's arm spread exceeds ours by ≥10 pts | **HELD — +89.0** (91.5 vs 2.5) |
| P2 | semantic beats lexical at @5 by >5 and <25 pts | **MISSED — +2.5** |
| P3 | multi-session ≥15 pts below the other types | **MISSED — 5.5 pts** (the good branch) |
| P4 | we do *not* win knowledge-update | **HELD — +2.6 pts** |
| P5 | ≥2% of offered rounds lost to dedup | **MISSED — 0.000%**, and badly posed |

### P4 held, and it is the prediction that earned its keep

P4 was written to stop a future overclaim by us: knowledge-update is this
project's differentiating axis, so the temptation is to point at that
column as proof the correctness machinery works. Measured: **98.1% vs
95.5%, a 2.6-point difference** — well inside the 10-point band that
pre-registered a non-win.

The reasoning behind it is now empirically confirmed rather than merely
argued. Recall@k asks whether the evidence session comes back. It cannot
ask whether the store *knows a fact was superseded*, which is the actual
claim. Both systems retrieve a superseded fact perfectly well. **Anyone
citing a knowledge-update recall number as evidence of bettermemory's
verification mechanism is misreading this benchmark**, and the
pre-registration said so before the number existed.

### P1 held, but read what it actually measures

Their arm spread is 91.5 points against our 2.5. That is not a statement
about retrieval quality — it is the phrase-query defect, and it is
config sensitivity rather than a headline. Their FTS **index is
correct**: a single content word retrieves the evidence session in 25 of
25 sampled questions. Any multi-word query is wrapped into an FTS5
phrase and requires contiguity, so it returns nothing, and stripping
stopwords does not rescue it. Report it as a defect in multi-word query
handling on their *fallback* path — Chroma-on is what ships by default,
and on that path they score 91.6%.

### P2 is the finding, and it is against us

`bench/retrieval/` measures a **+25-point** recall@1 lift from routing an
embedding model into ranking, on both its v1 and v2 corpora. That lift is
**the load-bearing evidence for the 3.29.0 default flip.** On
third-party ground it is **+2.5 points at recall@5 and +3.7 at recall@1.**

Part of that is a ceiling effect and saying so is fair rather than
exculpatory: at k=5 lexical is already at 89.3%, leaving 10.7 points of
headroom, so the arms cannot separate much. But at k=1 there are 11.5
points of headroom against the 64% ceiling and semantic still takes only
3.7 of them. **The +25 lift does not reproduce at anything like its
published magnitude, and ceiling effects account for only part of the
gap.**

This does not overturn the flip. It relocates it. The per-class table
shows why:

**The entire pooled lift is one class.** `single-session-preference`
moves +23.3 points; every other class moves between 0.0 and 2.4. Those 30
questions are precisely the ones where the question and its evidence
share no vocabulary — a user asking what they'd prefer, against a session
where they mentioned a preference obliquely. Everywhere a literal exists
to match on, lexical retrieval already wins and the embedding model adds
nothing.

That is **`bench/retrieval/`'s own conclusion, sharpened.** That directory
found the lift was *vocabulary*, not phrasing — `control` scored like
`asked`, and only `requery` (content words the document contains) moved
the number. This corpus says the same thing from the other direction: the
lift is confined to the class where no shared vocabulary is available.
So the mechanism replicated on independent ground even though **the
magnitude did not**, and a 30-question class carrying a pooled average is
exactly the shape that a single-corpus benchmark reports as a general
+25.

**What this changes:** `bench/retrieval/`'s +25 should be read as a
property of a corpus whose 20 gold topics sit in distinct subsystems, not
as the expected lift on arbitrary content. The flip still looks correct —
+23 points on the vocabulary-gap class is a real user benefit, and 4×
retrieval cost for it is a defensible trade — but "embeddings buy +25
points of recall" is not a claim this project should keep making.

### P3 missed on the good branch

Multi-session reasoning sits 5.5 points under the mean of the other
classes (84.9% vs 90.4%), not the ≥15 predicted. The pre-registration
committed in advance to reporting that as a genuinely good result, so:
retrieving evidence spread across two to six sessions degrades far less
than expected. Note what it is *not* evidence of — recall finding the
sessions says nothing about whether anything downstream can reason across
them.

### P5 was a badly posed prediction, not a finding

It predicted ≥2% of offered rounds would be lost to dedup. Measured:
**124,361 items written from 124,361 rounds offered, 0.000%.** The same
document that made the prediction also specifies ingest through
`Store.write`, the raw storage layer, which performs no dedup at all — so
the shortfall was zero *by construction*. The prediction was of an effect
whose mechanism it had disabled two sections earlier. Recorded as a flaw
in the pre-registration rather than dressed up as a result.

### P4 cannot be scored, and that is worth noticing

`knowledge-update` scores **98.1% in both arms** — effectively saturated.
P4 predicted we would *not* out-retrieve claude-mem on this class because
recall@k cannot see correctness. At 98.1% there is 1.9 points of headroom,
so the metric cannot separate two competent retrievers here at all. This
**strengthens** P4's reasoning: whatever bettermemory's knowledge-update
advantage is, **this instrument structurally cannot show it.** That axis
belongs to `bench/rot/`, and any writeup that points at a
knowledge-update recall number as evidence of the correctness mechanism
would be misreading its own benchmark.

## Data-integrity notes

- **13 questions repeat a session id** inside their own haystack. Deduped
  on ingest, counted in every result file, not dropped.
- **Depth truncation is negligible**: 0 questions at k=1, 2 at k=5, 9 at
  k=10 failed to yield k distinct sessions from 200 ranked items.
- **Zero abstention questions** exist in the distributed corpus, so one
  of the five abilities the paper advertises is unmeasurable here. Four
  are scored. See PREREGISTRATION.md addendum item 3.

## What this does not measure

- **Any competitor inside `run.py`.** This runner scores bettermemory
  alone; the claude-mem arms run through `cm_run.py` and publish their
  own artifacts (the comparison quoted above). A single-system artifact
  licenses no comparative claim by itself — the comparison rests on the
  paired artifacts and the preregistered attribution rule, never on one
  run's numbers.
- **End-to-end capture.** Ingest bypasses `memory_write`'s dedup,
  transient screening and confirmation flow: `run.py` calls `Store.write`
  in `src/bettermemory/store.py` directly. This is store + retrieval.
- **The above-threshold regime.** Per-question stores hold ~249 items
  against a 500-item index threshold, so SQLite bm25 prefiltering never
  engages and the full store is ranked. `bench/retrieval/` closed this
  gap for itself on 2026-07-30 — it now drives the production pool
  resolver and measures zero recall@5 lost to the prefilter on its
  lexical arm (`bench/retrieval/results/prefilter-above-threshold-2026-07-30.json`).
  **This directory has not.** Nothing here has been run above the
  threshold, and the retrieval result does not transfer: it was measured
  on a 180-document synthetic corpus with an off-domain-padded variant,
  not on LongMemEval's haystack.
- **Answer correctness.** No judged arm, by design: it requires a GPT-4o
  judge and an API key, which collides with the autonomy criterion.
- **Staleness accuracy.** See P4. `bench/rot/` owns that axis.

## What this means for the competitive case

Item (e) was commissioned to prove bettermemory out-retrieves claude-mem
on ground neither party authored. **It measured a tie.** That result is
kept as the headline rather than buried under the per-class rows where
we win three and lose one.

Two consequences follow, and both are more useful than a win would have
been:

1. **Retrieval is not the differentiator.** Two systems with entirely
   different architectures — memory files plus SQLite FTS5 and an
   optional embedding model, versus observations plus ChromaDB — land
   within 0.2 points of each other on 500 third-party questions. That is
   a strong hint that session-level recall on this kind of corpus is
   near saturation for any competent design, and that competing on it is
   competing on a solved axis.
2. **The correctness axis is where the claim has to live**, and this
   instrument is structurally blind to it (see P4). `bench/rot/` owns
   that, on a 30-repository corpus, where claude-mem scores a structural
   **N/A** because it has no `verified_at`, no `superseded_by`, and no
   lifecycle verb but DELETE. The defensible sentence is "we verify and
   here is the measured accuracy," not "we retrieve better."

## Three discarded runs, and why they are worth recording

Every one of these produced a number that flattered bettermemory, and
none survived:

| run | claude-mem @5 | why it was void |
| --- | --- | --- |
| first 40-question | 7.5% | 20 s fixed sleep; index barely built |
| full 500 (#1) | 54.1% | Chroma 57% built, 210/500 empty |
| **full 500 (#2)** | **91.6%** | valid — index 100%, 0 empty |

The middle row is the one to keep in mind. It would have published a
**+37.7-point win** over a competitor. The margin was almost entirely a
half-built vector index on this machine. It was caught because
`await_chroma_backfill` measures readiness and marks the run
`complete: false`, not because the number looked wrong — 54% was
perfectly plausible.

**The dominant failure mode in a comparative benchmark is not
mismeasuring yourself, it is mismeasuring the competitor in your own
favour.** Three for three here. The invalid artifact is retained as
`results/claude-mem-full500-INVALID-partial-index.json`.

## Next

0. **`temporal-reasoning` and `multi-session` are ONE failure mode, and it
   is a top-k budget problem.** Measured 2026-07-29 by re-running this
   directory's own ingest and `distinct_sessions` collapse per class
   (lexical arm; reproduces the published 83.7% / 84.9% exactly):

   | class | complete @5 | **partial** | total miss | n |
   | --- | --- | --- | --- | --- |
   | temporal-reasoning | 73.7% | **17.3%** | 9.0% | 133 |
   | multi-session | 68.4% | **29.3%** | 2.3% | 133 |

   On the partial questions the FIRST evidence session ranks #1 in 16/23
   and 26/39; the co-evidence it drops sits at **median rank 9**, and only
   1 of 30 / 4 of 54 fall outside the top 200 at all. So the evidence is
   retrieved and then cut off — not a date-understanding failure and not an
   indexing failure. It matches the question shapes: temporal-reasoning
   here is overwhelmingly two-event span and ordering ("how many days
   between A and B", "which happened first"), so the query carries
   vocabulary for two events living in two different sessions, and
   `score_memory`'s `0.5 + 0.5 * coverage` multiplier cannot be satisfied
   by either one alone.

   A re-ranker that rescues only co-evidence **already inside the top 10**
   is worth **+3.2 pooled macro recall@5** (89.3% -> 92.5%) — larger than
   the entire semantic-vs-lexical lift (+2.5) and with no embedding model.
   Together these two classes are 53% of the corpus and ~89% of all
   remaining recall@5 error. There is no read-side diversification
   anywhere in `search.py` today: dedup runs at write time only, and RRF
   fuses rankers that agree on the dominant sub-topic, so fusion
   reinforces the monopoly rather than breaking it.

   **Prerequisite CLOSED 2026-07-30**, and closing it refuted the
   paragraph above. `--per-question` now persists per-question records,
   and `results/baseline-both-arms-2026-07-30.json` +
   `results/per-question/baseline-2026-07-30.json` are a fresh both-arms
   run against unmodified ranking. It reproduces every published figure
   exactly — pooled 89.35 / 91.85, temporal-reasoning 83.72,
   multi-session 84.87 — and the partial/complete table above to the
   tenth of a point, this time derivable from a committed file instead
   of a throwaway re-run. Per-type ceilings are ~100% at k=5 for every
   class (temporal-reasoning 99.6%), so the headroom is real rather than
   arithmetic. Closing the prerequisite also **refuted the diagnosis in
   this item** — see "Read-side diversification, measured" below.

1. **An above-threshold arm.** Per-question stores hold ~249 items
   against a 500-item index threshold, so bm25 prefiltering never
   engages. `bench/retrieval/` measured this regime on 2026-07-30 and
   found the prefilter cost it no recall@5 on the lexical arm, because
   bm25 nominated the gold document more often than the ranker could
   place it. That is a result about a 180-document synthetic corpus, not
   about this haystack, and this directory has still never run above the
   threshold — it remains the most likely place for the tie to break in
   either direction.
2. **Enrichment parity.** claude-mem's `observations_fts` spans six
   columns its own pipeline fills by LLM extraction; this harness fills
   one. Their 91.6% is therefore a floor, not a ceiling — see
   PREREGISTRATION.md addendum 2.
3. Not a judged QA arm. It needs a GPT-4o judge and an API key, which
   collides with the autonomy criterion this project publishes against.

## Read-side diversification, measured

Item 0 of "Next" above diagnosed this project's largest remaining
retrieval error as a coverage problem and prescribed a read-side
re-ranker worth +3.2 pooled recall@5. The diagnosis is wrong. This
section is the measurement that killed it, kept beside the claim it
refutes rather than replacing it — the item is shipped history and the
correction reads better next to it.

Measured 2026-07-30, lexical arm, full 500 questions. Artifacts:
`results/baseline-both-arms-2026-07-30.json`,
`results/coverage-probe-2026-07-30.json`,
`results/co-evidence-rescue-2026-07-30.json`.

### The prediction, and what it actually measures

If a two-event question loses recall because no single session covers all
of its vocabulary, then the evidence session DROPPED out of the top 5
should carry query terms the survivors do not. `coverage_probe.py`
measures exactly that, against two reference sets — because the answer
moves by nine points between them, and publishing only the flattering one
would be a choice rather than a measurement.

65 partial questions; 87 dropped evidence sessions, of which 5 scored in
no ranker at all and 82 were ranked.

| terms the dropped session carries that the top 5 lacks | broad reference | strict reference |
| --- | --- | --- |
| none | 81 (93.1%) | 74 (85.1%) |
| exactly one | 6 | 12 |
| two | 0 | 1 |
| three or more | 0 | 0 |

*Broad* counts against every hit belonging to a top-5 session; *strict*
against one representative hit per top-5 session, which is the fairer
analogue of the list head a re-ranker between the fuse and the trim
actually holds — `search()` has no notion of sessions at all. Under
either, the dropped evidence is not answering a different half of the
question. Like for like — one best hit per session on both sides — it
matches *fewer* terms than the survivors it lost to, median 2 against 3.
It is a strict subset of what the head already carries, and it loses on
scoring, at a median item rank of 13.5. Any score keyed on coverage ranks
it below the survivors, not above.

The structural reason: in 337 of 500 questions the top 5 already carries
*every* term anything in the corpus matched. Matched terms are a subset
of a short query's vocabulary — median 7 unique terms per question, 1 per
hit — so there is very little room for novelty to exist at all.

### The ceiling, which is what closes the item

A tuning failure and an impossible mechanism look identical from the
outside, so the probe also bounds an **omniscient** rescue: one that
promotes exactly the dropped evidence carrying a novel term, into the top
5, with zero false promotions. Nothing real can beat it.

One ceiling alone would invite the obvious rebuttal — loosen the novelty
test and the ceiling rises — so the probe reports the ceiling **and** the
precision, across every reference including `blind`, which promotes all
82 ranked dropped sessions and asks nothing at all:

| novelty test | promotes | distractors it also promotes | precision | lift vs blind | oracle gain |
| --- | --- | --- | --- | --- | --- |
| blind (no filter) | 82 | 1,749 | 4.48% | 1.00× | +5.21 |
| broad reference | 6 | 104 | 5.45% | **1.22×** | +0.33 |
| strict reference | 13 | 233 | 5.28% | 1.18× | +0.79 |
| top-1 reference (loosest) | 40 | 906 | 4.23% | **0.94×** | +2.59 |

**Read the lift column, not the gain column.** A looser test raises the
ceiling only by converging on promoting everything — and the one
reference whose ceiling clears the gate has precision *below* blind
promotion, meaning it has stopped filtering rather than started finding.
The best novelty signal available is a 1.22× lift on a 4.5% base rate.
Clearing +2.00 needs something on the order of 25–30% precision at high
recall. That is the finding, and it does not depend on which reference
you prefer.

### Measured anyway, because a plausible mechanism is not a result

The rescue was built the shippable way: a bounded marginal-coverage bonus
for hits carrying terms the head misses, then a re-sort on the existing
`(score, created, id)` key — scoring rather than reordering, because a
list that is not descending by score silently disables
`top_hit_leads_runner_up` and with it half of `expand_top`'s coverage.
Parameters were chosen on a deterministic held-out half: 29
configurations were scored offline against the captured pre-trim
rankings, and the one the protocol selected was then run for real. The
offline prediction matched that real run to four decimals on the pooled
figure and on all six per-class figures. The sweep is method, not
evidence — the numbers below come from the committed artifacts, and the
ceiling above is what actually closes the item.

| measure | baseline | with rescue | gate |
| --- | --- | --- | --- |
| pooled macro recall@5 | 0.8935 | 0.8941 | **≥ 0.9135 — MISSED** |
| pooled **micro** recall@5 | 0.8671 | **0.8650** | not gated; moved backwards |
| temporal-reasoning @5 | 0.8372 | 0.8360 | no regression > 1pt — held |
| multi-session @5 | 0.8487 | 0.8450 | no regression > 1pt — held |
| lexical arm runtime | 321.2 s | 303.7 s | ≤ ~394 s — held |

**+0.06 points against a pre-stated +2.00**, and the effect is four
questions out of five hundred — one up, three down. The single gain is a
`single-session-preference` question, a class with n=30, which is the
whole of that column's +3.3. Evidence-weighted recall moved the other
way. Nothing in the sweep approached the gate. The ranking change is
reverted. The artifacts, the probe, and this section stay.

One thing deliberately *not* claimed: any single number out of the
held-out half. That split is unstratified and has no power at this effect
size — the two halves' own baseline recalls differ by more than every
effect in the sweep put together — so it is a selection protocol here and
not a significance test. The +0.06 is likewise reported as "no measurable
change" rather than a small win: four questions moved, three of them
downward, and evidence-weighted recall fell.

### The headroom is real; this closes an item, not a question

Perfect rescue of evidence already inside the first 10 distinct sessions
is worth **+5.0 pooled** (89.35% → 94.36%), inside the first 7 is worth
+3.0, and every per-class ceiling at k=5 is ~100%. The `--per-question`
records put the dropped sessions at a median distinct-session rank of 8:
the evidence is retrieved and then ordered wrongly. What is now excluded
is that the matched-term set expresses *why*. A future attempt needs a
signal the fused ranking does not already contain, and it should start by
pointing `coverage_probe.py` at whatever it proposes to key on — the
oracle ceiling is cheap to compute and would have closed this item in an
afternoon instead of a phase.
