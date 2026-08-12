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

## P1a — store-derived PPMI, killed at the gate, 2026-08-11

Round 5 closed the vote-conditioning line and named what was left: the
remaining harm is in what the legs CONTAIN. P1a is the mechanism for
that — derive expansion vocabulary from the collection being ranked
instead of from committed tables, which is what C1 has demanded since
round 2 (identical code flips sign between corpora, so a static table
cannot be right for both).

**Addendum 8's Gate 0 fired. No engine code was written, no arms ran,
and the sealed held-out instrument was not spent.**

### The bar, and why it is the only defensible one

Round 5 established C5: `_hybrid_fuse` fuses by RANK, so a leg
contributes `_RESCUE_LEG_WEIGHT / (rrf_k + rank)` however thin its
evidence is, and the architecture offers **no way to discount a bad
leg's vote**. The incumbent static tables already had their default
killed on this corpus for being too imprecise. So a replacement source
has to be at least as precise as what it replaces — 1.0×, with nothing
to tune.

### Measured

Identically on both sides, over 40 dev probes: the fraction of emitted
terms that appear in the gold document.

| source | terms per probe | precision |
| --- | --- | --- |
| committed static tables (incumbent) | 5.65 | **0.2743** |
| best PPMI cell, 36-point grid | 9.78 | **0.1253** |

**0.46× the incumbent. Not one of the 36 grid cells reaches parity**;
the range is 0.22×–0.46× across every combination of minimum document
frequency (2/3/5), PPMI shift (1/2/4) and top-k (2/3/5/8). The best
cell is also the *tightest* one — precision falls as more associates
are admitted, which is the shape of a source with no usable head.

Artifact: `bench/retrieval/results/ppmi-census-2026-08-11.json`.

### The signal is real; the precision is not

This is not "co-occurrence carries nothing". PPMI finds **150–201 gold
terms the static tables miss**, on 34–38 of 40 probes. The association
is there. What is missing is a way to keep it without the 10–65 terms
per probe that come with it — and at 55 terms per probe the round-5
evidence rule stops protecting anything, because matching two emitted
terms becomes trivial for almost any document. **The guard that made
round 5 the lane's best form is weakest exactly where a wide source
needs it most.**

### What this retires, and what it leaves

**Retired: store-derived co-occurrence as an expansion source at
personal-store scale.** 180 documents is thin for PPMI, and that is not
an artifact of the benchmark — it is the size a personal memory store
actually is. A mechanism that needs a large collection to be precise is
the wrong mechanism for this product.

**Not retired: the sealed instrument.** It has never been scored. The
gate refusing to spend it is the point of having a gate: a single-use
held-out check is not spent on a mechanism already known to be worse
than the incumbent.

**What the record now says about the campaign.** Two lanes have been
measured to their end. Conditioning which legs vote plateaus at a
ceiling (rounds 3–5). Replacing the source with store-derived
statistics is less precise than the tables it would replace (P1a). Both
findings point the same way: the expansion leg's contribution to a
rank-based fusion cannot be made precise enough by choosing better
words or better voters. The next mechanism that could change the
picture has to change the FUSION — give the leg a contribution that
scales with its evidence instead of with its rank — and no number here
licenses that.

## P1e — the same statistic, trained instead of counted, 2026-08-11

P1a killed an ESTIMATOR. Raw PPMI over 180 documents is noisy where
PPMI is known to be worst, so the successor question was always whether
factorizing the same co-occurrence structure does better than reading
it raw. That became askable when the owner settled the WaC boundary on
2026-08-11: from-scratch-TRAINED models are legal, pretrained
third-party weights are not.

`bench/embed_train.py` trains GloVe in pure Python from committed
repository text — no network, no third-party model code, no numpy
(the dependency tree has carried none since 4.0), fixed seed, and
byte-identical across runs. `bench/embed_census.py` scores it against
**addendum 8's gate, quoted unchanged**, on the same dev probes.

**No preregistration was written and none is proposed here.** This is a
census, on the same terms as P1a's: statistics only, dev-side, and the
sealed instrument under `bench/heldout/` was not read.

### Measured

Dev gold set, incumbent 0.2743 (62 of 226 emitted terms) at 5.65 terms
per probe (`../retrieval/results/embed-census-2026-08-11.json`):

| source | terms/probe | precision | x bar | p vs incumbent |
| --- | --- | --- | --- | --- |
| committed static tables (incumbent) | 5.65 | 0.2743 | 1.000 | — |
| P1a — raw store PPMI, best of 36 cells | 9.78 | 0.1253 | 0.46 | <0.001 |
| **P1e — trained on the store, tightest cell** | 2.95 | 0.2712 | **0.989** | 0.950 |
| **P1e — trained on the store, at the incumbent's width** | 5.65 | 0.2168 | **0.790** | 0.155 |
| P1e — trained on 13.5x more repository prose | 9.85 | 0.1015 | 0.370 | <0.001 |
| P1e — trained on conversational haystacks | 3.62 | 0.1034 | 0.377 | <0.001 |

**No cell passes, on any corpus, in either vector reading.** The
mechanism is nonetheless not what P1a was: training the statistic
roughly doubles reading it raw, and the remaining gap is inside the
noise of a 226-term sample. The gate is a point comparison and it is
missed — but the record says "missed, unresolvably" rather than
borrowing P1a's "missed decisively", because the two are not the same
measurement and the difference is checkable in the artifact.

### The conversational instrument cannot carry this gate, and that is a finding

Run here as well (`results/embed-census-2026-08-11.json`, 20 questions,
trained on a disjoint instance slice): **the committed tables emit 0.6
terms per probe on LongMemEval questions — twelve terms across twenty
probes, five of them in evidence.** A precision computed on twelve
terms is not a bar, so the census reports `gate_applicable: false` for
this instrument rather than publishing a ratio against it.

That is worth stating on its own. The lane's whole story on this corpus
has been that its expansion terms are harmful; the census adds that on
conversational queries the tables barely fire at all. Whatever the lane
costs here, it is not being paid by the static vocabulary tables.

### What P1e adds to the campaign's record

Rounds 3-5 measured that conditioning WHICH legs vote hits a ceiling.
P1a measured that replacing the source with store-derived counts is
less precise than the tables. **P1e measures that the counting was not
the limitation — the corpus is.** More text raises query-token coverage
from 68.6% to 92.3% and drops precision to 0.29x, and more training
epochs fit the co-occurrence matrix twenty times better while halving
precision. The only corpus that yields usable neighbours is the
collection being ranked, and at personal-store scale that collection is
35,000 tokens.

So the constraint P1a stated survives its own successor, in a stronger
form: it is not that a small store is thin for PPMI, it is that **no
admissible corpus is both large and on-topic**, and off-topic text is
worse than no text. Whether a mechanism at 0.79x-0.99x is worth an
engine and a preregistration is an owner's call, not a number this
census produces — and `../THIRD_INSTRUMENT.md` still blocks any
vocabulary-adapting mechanism on a clean held-out instrument that does
not exist.

### Two invented mechanisms, measured to the same standard

The textbook family is written for corpora four orders of magnitude
larger than a personal memory store, so its miss is not a terminal
verdict. `../embed_hybrid.py` proposes two mechanisms designed for the
regime the census described, and holds them to the same bar
(`../retrieval/results/embed-hybrid-2026-08-11.json`).

- **The agreement rule** — emit only terms ranked highly by BOTH PPMI
  and the trained vectors, on the hypothesis that two estimators of the
  same structure have independent errors. **Measured worse than the
  dense model alone, 0.44x against 0.79x at the incumbent's width.**
  The premise was wrong in a way worth recording: GloVe factorizes the
  matrix PPMI reads, so rank agreement selects for high-count pairs,
  and high-count pairs are the frequent, least discriminating terms.
- **N-gram bridging** — give an out-of-vocabulary query token a vector
  composed from the in-vocabulary terms it shares characters with.
  **Coverage 0.686 to 0.796, fifty tokens rescued, precision unmoved.**
  A mechanism that does exactly what it was built for, aimed at recall
  while the bar prices precision.

Both negatives are the point rather than an embarrassment: the bar did
not move to accommodate an invention, and the invention's own premise
was reported as withdrawn by the data that withdrew it. The variant the
first failure points at — cosine threshold to select, sparse counts to
veto rather than to confirm — is named in the retrieval README and was
deliberately not run, because a grid explored until something passes
stops being evidence.

### Census 2 parks the lane, under a declaration written first

The one mechanism census 1 named and did not run — sparse counts as a
*veto* on the dense model's candidates rather than as a co-selector —
was run in census 2, with every cell, both readings of the bar, the
primary cell and the readiness criterion committed **before any number
existed** (`../P1E_CENSUS2_DECLARATION.md`, sha ordering: declaration
`155d6f0`, then the run). Full table in `../retrieval/README.md`;
artifact `bench/retrieval/results/embed-census2-2026-08-11.json`.

The veto is a real improvement — it raises precision in every cell of
the primary arm. Four cells clear P1a's gate outright. **All four emit
about half the incumbent's terms per probe**, so all four fail the
at-width reading the declaration fixed in advance, and none is
statistically separable from the incumbent besides. The declared
primary cell lands at 0.712x and misses two of its four conditions.

**Verdict, by the pre-written criterion: the lane is parked.** No cell
in the declared family reaches the gate while emitting at least the
incumbent's terms per probe.

Two things are worth carrying forward. The declaration's falsifiable
side-check held: re-estimating the incumbent at each challenger width by
subsampling reproduced its precision at every width, so census 1's
narrow-cell readings were sound rather than a width artifact. And the
value of declaring first is now measured rather than asserted — the
selectable headline here was "1.172x, the veto clears the bar", and it
is a real number that the pre-named primary cell and the at-width
reading correctly refuse to promote.

## Round 6 — the evidence-scaled vote clears the line, and the dev gate stops it, 2026-08-11

Five kills all named the same untested cause: `_hybrid_fuse` fuses by
RANK, so the rescue leg contributed the same 0.7 whether its rank-1
rested on a discriminating synonym or a single coincidental token.
Addendum 9 changed the vote itself — nothing below the round-5 evidence
floor, half weight at the floor, full weight at three matched terms.

**It produced the campaign's best held-out figure and cleared the kill
line for the first time in six rounds. It also cost the dev set two
questions, which is a preregistered kill, so the sealed instrument was
not spent.**

### Arms

| arm | macro@1 | macro@5 | macro@10 |
| --- | --- | --- | --- |
| baseline, lane off | 0.5246 | 0.8935 | 0.9443 |
| lane on, no cap (round 1) | 0.4772 | 0.8770 | 0.9471 |
| lane on, flat weight + evidence rule (round 5) | 0.5014 | 0.8823 | 0.9476 |
| **lane on, evidence-scaled weight** | **0.5134** | **0.8926** | **0.9463** |

### Scored predictions

| # | prediction | measured | outcome |
| --- | --- | --- | --- |
| P42 | lane-off byte-identical | dev cells identical; LongMemEval 0.5246/0.8935/0.9443 exactly | **HELD** |
| P43 | dev set does not regress | asked @5 **90%→80%**, control @5 **85%→80%** | **MISSED — KILL** |
| P44 | beats round 5 | **0.8926 > 0.8823**, **0.5134 > 0.5014** | **HELD** |
| P45 | macro@5 ≥ 0.8900 — the kill line | **0.8926** | **HELD** — first time in six rounds |
| P46 | macro@10 ≥ 0.9443 | **0.9463** | **HELD** |
| P47 | damping fires on some but not all | dev 78.6%, held-out 81.9% of voting legs damped | **HELD** |
| P48 | held-out confirms | **not run** — dev gate failed | — |

Five of seven held. Kill criterion 2 fired.

### What this establishes

**The fusion hypothesis is correct.** Six rounds argued that the leg's
influence was independent of its evidence, and the first experiment to
change that recovers **76% of the lane's macro@1 loss** (0.4772 →
0.5134 against a 0.5246 baseline) and lands macro@5 **0.0009 below
baseline**. Compare the three vote-conditioning rounds, which plateaued
at 0.8790 / 0.8823 / 0.8830, and P1a, which never reached the
incumbent's precision. Nothing else in the campaign has come close.

**And the curve is mis-calibrated on the dev side.** The damping fires
on ~79% of voting legs — the m=2 stratum is most of the population, and
the dev labels put it at 68% helpful. Halving the vote of a stratum
that is helpful two times in three costs the gold set two questions at
recall@5, while the same damping is what buys the conversational
corpus its gain. The mechanism is right and the constant is too
aggressive for a technical store.

### Why the sealed instrument did not run

Addendum 9 scores `bench/heldout` "only if the dev gates pass". P43 is
a dev gate and it failed, so the instrument stays unspent — the same
protection that kept it from being burned on P1a. It has still never
been scored.

That is the correct outcome even though the LongMemEval number is
tempting. The default-flip definition requires the dev set at or above
its current figures, so this result could not have flipped the default
whatever the blind instrument said, and spending a single-use check to
satisfy curiosity about a disqualified configuration is exactly what
the gate exists to prevent.

### What the record now supports

A round 7 with a **gentler curve**, preregistered against the same
three instruments, is the obvious next experiment: the mechanism is
demonstrated, and only the weight at the floor stratum is wrong. Any
such attempt has to fix its curve from dev evidence before code, as
this one did, and must not be tuned against the 0.8926 already
observed — that number is now dev-contaminated for curve selection.

**No number here licenses that experiment**; it needs its own
preregistration, and the blind instrument is still available to check
it exactly once.

## Round 7 — the structural curve, and the trade-off located exactly, 2026-08-11

Round 6 confirmed the fusion hypothesis and failed its dev gate by
halving the floor stratum's vote. Addendum 10 replaced the curve's form
with the quantity the mechanism names — a leg's weight is the fraction
of the full-evidence bar its evidence reaches, `m/F` — which lifts the
floor from 0.35 to 0.467 and **introduces no new constant**.

**The dev gate failed again, so the blind instrument was not spent for
the third time. And the arms together locate the campaign's obstacle in
a single number.**

### Arms

| arm | macro@1 | macro@5 | macro@10 |
| --- | --- | --- | --- |
| baseline, lane off | 0.5246 | 0.8935 | 0.9443 |
| lane on, flat weight (round 5) | 0.5014 | 0.8823 | 0.9476 |
| lane on, `(m−1)/(F−1)` (round 6) | 0.5134 | **0.8926** | 0.9463 |
| **lane on, `m/F` (round 7)** | **0.5074** | **0.8901** | 0.9468 |

### Scored predictions

| # | prediction | measured | outcome |
| --- | --- | --- | --- |
| P49 | lane-off byte-identical | dev cells identical; LongMemEval 0.5246/0.8935/0.9443 exact | **HELD** |
| P50 | dev preserved (asked ≥55/90, requery =80/100, control ≥50/85) | asked **(55, 80)**, requery (80, 100), control **(50, 85)** | **MISSED** — asked@5 |
| P51 | macro@5 ≥ 0.8900 | **0.8901** | **HELD** |
| P52 | macro@5 ≥ 0.8935 (baseline) | 0.8901 | **MISSED** |
| P53 | macro@1 ≥ 0.5134 | 0.5074 | **MISSED** |
| P54 | macro@10 ≥ 0.9443 | **0.9468** | **HELD** |
| P55 | held-out confirms | **not run** — dev gate failed | — |

Three of six scoreable held. Kill criterion 2 fired.

### The finding: one number, two corpora, opposite directions

The gentler curve recovered `control` (80%→85%) and did **not** recover
`asked`. Sweeping the floor stratum's weight directly — the only
quantity that differs between rounds 5, 6 and 7 — shows why:

| weight at the floor stratum | dev asked recall@5 | LongMemEval macro@5 |
| --- | --- | --- |
| 0.00 (withheld) | 0.65 | — |
| **0.35** (round 6) | 0.80 | **0.8926** |
| **0.467** (round 7) | 0.80 | **0.8901** |
| 0.60 | 0.85 | — |
| **0.70** (round 5, no damping) | **0.90** | **0.8823** |

The dev figures come from a direct sweep over that weight on the gold
set; the three LongMemEval figures are the committed arms of rounds 5,
6 and 7.

**Both columns are monotone and they run in opposite directions.** The
technical corpus wants the floor stratum at full weight; the
conversational corpus wants it damped, and the more it is damped the
better that corpus does. There is no constant that satisfies both — not
because the search was insufficient, but because the two optima are at
opposite ends of the same axis.

That is C1 restated at the finest resolution the campaign has achieved.
Six rounds ago it was "identical code flips sign between corpora". It
is now **one stratum of one leg, one scalar, measured monotone in both
directions**.

### Why the blind instrument still has not run

Addendum 10 spends it only if the dev gates pass. They did not. It has
now been protected from P1a, round 6 and round 7, and has never been
scored.

That remains correct: the flip case requires the dev set at or above
its current figures, so no configuration measured here could have
flipped the default whatever the blind instrument said.

### What the record supports next

**A store-adaptive weight, not another constant.** Round 4 proved a
self-calibrating criterion transfers — its firing rate matched across
corpora to 2.0 points where a fixed threshold spanned 17.1 — but it
self-calibrated a *threshold*, and thresholds were the wrong lever.
Nobody has self-calibrated the *weight*, and the sweep above is exactly
the shape that motivates it: the right weight is a property of the
store, and both corpora say so by disagreeing monotonically.

**No number here licenses that experiment.** It needs its own
preregistration, and the blind instrument is still available to check
it exactly once.

## Round 8 — the store cannot tell the campaign which weight it wants, 2026-08-11

Round 7 reduced the obstacle to one scalar. Addendum 11 asked whether
that scalar could be derived from the store, which is what C1 has
demanded since round 2. **Gate 0 fired. No adaptation rule was written,
no arms ran, and the sealed instrument was not spent.**

### The bar, and why it is round 7's own measurement

The floor weight's optimum differs about **twofold** between the
corpora (0.70 for the technical one, ~0.35 for the conversational one),
monotone and opposed. A rule keyed on a store statistic has to amplify
that statistic's spread into the weight's spread — so the statistic has
to separate the corpora by at least as much as the weight must move.
Below that, the rule is a high-gain amplifier on a near-constant input:
unstable under ordinary corpus variation, and a fit rather than a
derivation. **2× is round 7's requirement, not a chosen threshold.**

### Measured — six statistics, neither corpus distinguishable

| statistic | dev | LongMemEval | ratio | separates? |
| --- | --- | --- | --- | --- |
| documents | 180 | 255.6 | 1.42 | no |
| mean document length | 123.2 | 209.4 | **1.70** | no |
| type–token ratio | 0.1829 | 0.1288 | 0.70 | no |
| hapax share | 0.4397 | 0.3781 | 0.86 | no |
| **filler-token share** | 0.0108 | 0.0122 | **1.13** | no |
| stopword share | 0.3803 | 0.3551 | 0.93 | no |

Artifact: `bench/retrieval/results/store-census-2026-08-11.json`.

**Not one statistic clears the bar.** The closest is mean document
length at 1.70×, and that is a length artifact — chat rounds are longer
than technical notes — not a register signal; keying the weight on it
would damp any store that writes in paragraphs.

**The most instructive failure is filler-token share at 1.13×.** That
is the quantity the lane's own origin story names: *"memory bodies are
technical prose, so conversational filler is corpus-RARE."* The two
corpora are **12% apart** on it. The premise that motivated the filler
df-floor in 5.1 is directionally true and nowhere near strong enough to
carry a decision.

### What this retires

**The store-adaptive family, on cheap statistics.** Not "we did not
find the right rule" — the inputs a rule could use do not separate the
things it would have to tell apart. Any rule that appeared to work
would be amplifying a ≤1.7× signal into a 2× decision, and would swing
on ordinary corpus variation.

**And it closes C1 with a measurement rather than a hope.** Six rounds
have said "make it a function of the store". Round 8 says the store, as
the engine can cheaply see it, **does not contain the distinction** —
what separates a corpus the lane helps from one it harms is semantic,
not distributional. That is a real result about the whole adaptive
family and it cost one census.

### Where the campaign stands

The lane ships opt-in and unchanged. Its best measured configuration is
round 6's (held-out macro@5 **0.8926**, 0.0009 under baseline) and that
configuration costs the dev set two questions, so it cannot flip the
default. The blind instrument has been protected through P1a, round 6,
round 7 and now round 8, and **has never been scored** — it is still
available, exactly once, for a mechanism that clears the dev gates.

What is left is not another lever on this lane. Every one measured —
which legs vote, what they contain, how hard they vote, and whether the
store can say how hard — is now closed with evidence.

## Round 9 — the trailing base leg is withheld, and the corpora split the verdict, 2026-08-12

Addendum 9 closed with H-fusion-general as a named future hypothesis:
the BASE legs also vote by rank, so a keyword or BM25 leg whose rank-1
rests on one coincidental token votes as hard as one whose top matched
four query terms. Addendum 12 tested it with a mechanism that owns no
graded constant — the scalar rounds 6–7 measured as unresolvable across
corpora. The base-leg census (`bench/base_leg_census.py`, artifact
`base-leg-labels-2026-08-12.json`) found that absolute rank-1 evidence
does not stratify base-leg helpfulness (BM25 runs backwards under it),
but RELATIVE evidence does: leading legs helped 20/29 labelled cases,
trailing legs 2/29, none of twelve at a deficit of two or more. The
rule: the trailing leg does not vote; ties — 80% of dev probes — fuse
byte-identically.

**It produced the campaign's largest dev-set gains — census-exact, with
recall@5 untouched — and cost the conversational corpus on all three
macros, which is a preregistered kill. The default does not change and
the sealed instrument was not spent, for the fifth time.**

### Arms (LongMemEval, lane off)

| arm | macro@1 | macro@5 | macro@10 |
| --- | --- | --- | --- |
| baseline, mechanism off | 0.5246 | 0.8935 | 0.9443 |
| **mechanism on** | **0.5131** | **0.8861** | **0.9429** |

### Dev cells (recall@1/recall@5, lane off)

| regime | probe | off | on |
| --- | --- | --- | --- |
| unpadded | asked | 35/60 | 35/60 |
| unpadded | requery | 80/100 | **85**/100 |
| unpadded | control | 35/60 | 35/60 |
| padded-600 | asked | 25/60 | **35**/60 |
| padded-600 | requery | 70/100 | **85**/100 |
| padded-600 | control | 25/60 | **35**/60 |
| prefilter above-threshold, on-cells | asked | 30/60 | **35**/60 |
| prefilter above-threshold, on-cells | requery | 75/100 | **85**/100 |
| prefilter above-threshold, on-cells | control | 30/60 | 30/60 |
| prefilter forced-180, on-cells | asked | 35/60 | 35/60 |
| prefilter forced-180, on-cells | requery | 80/100 | **85**/100 |
| prefilter forced-180, on-cells | control | 35/60 | 35/60 |

Lane-on regression pair (unpadded): asked 55/90 → 55/90, requery
80/100 → 85/100, control 50/85 → 55/85. Artifacts:
`retrieval/results/round9-*-2026-08-12.json`, all at `a1fd750`,
tree clean.

### Scored predictions

| # | prediction | measured | outcome |
| --- | --- | --- | --- |
| P63 | off-arm byte-identity (dev cells + LME to four decimals) | every off cell = its committed baseline; LME 0.5246/0.8935/0.9443 exact | **HELD** |
| P64 | tie probes byte-identical, full depth window | 48/48 unpadded, 43/43 padded, zero violations (`round9-tie-identity`) | **HELD** |
| P65 | withholding real and bounded (0 < share < 0.5) | 20.0% / 28.3% | **HELD** |
| P66 | dev does not regress, any stratum, any regime | no cell fell anywhere; the lane-on requery clause written "= 80/100" measured **85**/100 | **HELD in substance; one clause missed upward** (below) |
| P67 | census-sharp improvements | padded @1 35/85/35 (+10/+15/+10), unpadded requery 85 (+5), recall@5 unchanged in every cell | **HELD — every cell exact** |
| P68 | LME costs nothing (all three ≥ baseline) | 0.5131 / 0.8861 / 0.9429 — **all three below** (−1.15 / −0.74 / −0.14 pts) | **MISSED — KILL** |
| P69 | held-out confirms (instrument #1, first spend) | not run — kill 3 fired first | — |
| P70 | provenance | `tree_dirty` false on every artifact, all at `a1fd750` | **HELD** |

Kill criterion 3 fired. Per addendum 12's own outcome clause, the
constant ships False: the code stays committed and exercisable
(`--base-withhold on` in all three runners), the DEFAULT engine is
byte-identical to 5.3.0 (P63 and P64 are the proof), and this section
is the published record.

### The mis-specified clause, disclosed

P66's lane-on line predicted "requery exactly 80/100", copied from
rounds 5–7 — where it was an invariant because those mechanisms only
damped the rescue leg, which requery's high coverage never engages.
Round 9 changes the BASE fusion, which requery absolutely can feel, and
P67 itself predicted the +5 on the identical underlying queries. The
measurement (85/100) exceeded the equality. Scored missed-as-written;
no kill keys on it (kill 2 reads "falls below baseline"). The lesson:
an equality prediction is a claim about the mechanism's REACH — copy
the number, re-derive the reach.

### Where the damage lands (per-question sidecars, both arms)

Only **56 of 500 questions** saw any evidence-rank movement — the
LongMemEval blast radius is 11.2%, against 20–28% of dev probes. The
loss is thin and wide rather than concentrated: at recall@1 the flips
run 3 up / 13 down, and every question type except
single-session-assistant loses somewhere —
single-session-preference −6.7 pts at @1, multi-session −1.5,
single-session-user −1.4, temporal-reasoning −0.6. The one gain is
multi-session at recall@5 (+0.6, 3 up / 1 down), historically this
instrument's weakest column. Sidecars:
`results/per-question/round9-{off,withhold}-pq-2026-08-12.json`.

The read: on conversational stores the trailing leg is RIGHT often
enough that silencing it costs almost everywhere — the exact inverse
of the dev labels, where trailing legs helped 2 of 29. The same
quantity, relative rank-1 evidence, predicts helpfulness on technical
prose and mis-predicts it on conversational chat.

### What this establishes

**C1 now has a base-pair measurement.** Eight rounds measured the
corpus sign-flip on the rescue lane; round 9 measures it on the BASE
fusion. The same withholding that buys the technical corpus its largest
gains of the campaign (+10/+15/+10 at recall@1, nothing lost anywhere,
byte-identical ties, census-exact reproduction) costs the
conversational corpus on all three macros. What separates the corpora
is semantic (round 8), it governs the base legs too (round 9), and no
constant — graded or zero–one — satisfies both.

**The census methodology is validated end-to-end.** P67's cells were
predicted from the committed labels before the arms ran, and every cell
landed exactly: the counterfactual arms reproduce through the shipped
engine 1:1. Future mechanisms whose blast radius is expressible as
census counterfactuals can preregister sharp cells instead of hedged
floors.

**Named open option, owner-shaped (recorded, not built):** the
mechanism is knob-polarised exactly like the rescue lane — it helps the
audience that turns `rescue_expansion` on and harms the store shape
that leaves it off. Whether that means a second `[behavior]` knob, one
combined technical-store switch, or nothing, is a product-surface
decision for the owner. No number here licenses it.

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
