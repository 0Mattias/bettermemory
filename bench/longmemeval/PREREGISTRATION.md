# Pre-registration — LongMemEval retrieval comparison vs claude-mem

Committed **before the corpus is downloaded and before a line of adapter
code is written**. Every threshold below is falsifiable, and the results
published later mark each prediction hit or **MISSED** against these
exact numbers.

This is build-order item (e): the first artifact in this repository that
licenses a *comparative* claim. `bench/retrieval/` closes by saying
"Nothing here licenses a comparative claim" — that sentence is the reason
this directory exists.

## The instrument, and why this one

**LongMemEval** (Wu et al., ICLR 2025, arXiv:2410.10813v2 — MIT,
`xiaowu0162/LongMemEval`). 500 human-curated questions across seven
question types, embedded in freely-scalable synthetic chat histories.
(**Six** types in the corpus actually distributed — see addendum item 3.)

It replaces **LongMemCode**, which was retired *before* any adapter was
written when the label-provenance question was put to it: 84.5% of its
labels are `scip_roundtrip`, derived from the structural bundle format of
the vendor whose own memory system heads its scoreboard at 99.24%. The
scale and cost claims made for it were accurate; the neutrality claim —
the entire reason it was chosen — was not.

**LongMemEval was subjected to the same question first, and passed.**
From §3.2, Figure 2, and Appendices A.1/A.2 of the paper:

- Evidence-session labels are **construction-time ground truth
  established by insertion**. Human experts author the questions and
  hand-decompose each answer into *evidence statements*; each statement is
  then authored *into* a purpose-built session via LLM self-chat; those
  sessions are finally shuffled into a haystack of unrelated ones.
  `answer_session_ids` is known because the authors **placed** those
  sessions. Nothing is recovered post-hoc.
- **No retriever, embedding model, or similarity search appears anywhere
  in construction** — including distractor selection (25% ShareGPT / 25%
  UltraChat / 50% simulated-from-other-attributes, randomly sampled;
  non-conflict enforced structurally through a 164-attribute ontology,
  never by similarity).
- The human gate is load-bearing, not decorative: ~5% question yield after
  expert filtering, ~70% of evidence sessions hand-edited, annotators
  verifying both that the evidence is present **and** that no other
  evidence leaked in. That second check is what makes the label set
  exhaustive, and it is human rather than model-adjudicated.

> **Terminology trap, recorded so this check is not re-opened on a bad
> grep.** §3.2 reads "each evidence statement is then separately
> *embedded into* a task-oriented evidence session." *Embedded* there
> means **inserted into**, not vector-embedded. Anyone scanning for
> "embedding" lands on that sentence and can misread it as the exact
> failure mode that was ruled out.

**Retrieval arm only.** LongMemEval's headline QA evaluation scores
correctness with a GPT-4o judge and a mandatory `OPENAI_API_KEY`. That
collides with the autonomy criterion this project publishes against, so
the judged arm is **not** run as the primary result. The benchmark ships
labelled evidence sessions, so session-level recall@k is deterministic,
needs no judge and no key, and costs $0 — and retrieval is precisely the
axis the comparative claim is about.

Keep the distinction sharp: an LLM judge on the **scorer** side is
ordinary academic practice and says nothing about a memory system's
autonomy. What the autonomy tier is about is a system paying for an LLM
call at **read time**. Conflating the two would either wrongly disqualify
a good benchmark or wrongly flatter a competitor.

## The attribution rule — the crux, declared first

LongMemEval scores recall against **session ids**. Neither system under
test stores sessions: bettermemory stores memories, claude-mem stores
observations. **The mapping from a returned item back to a session is
therefore the whole comparison**, and it is the first thing a reviewer
should attack. So it is fixed here, before any number exists.

**Ingest unit: one item per conversational round** (one user message plus
its assistant reply), carrying its parent session id as metadata. Held
identical across both systems.

**Scoring: session-level recall@k from an item-level ranking.** Retrieve a
ranked list of items, map each to its parent session, dedup preserving
first occurrence, take the first *k* distinct sessions, and score against
`answer_session_ids`. Both systems get the byte-identical rule.

Three alternatives were considered and rejected, and the reasons are
recorded so the choice is auditable rather than convenient:

| granularity | why not the headline |
| --- | --- |
| one item per **session** | 1:1 mapping and trivially fair, but forces both systems to store whole-session bodies neither would ever write. Retained as a **sanity arm**, not the headline. |
| one item per **round** | **chosen.** Natural for both stores, holds the ingest unit constant, so the thing varying is retrieval. |
| each system's **native extraction** | the most realistic and the least controlled — it measures each project's extraction policy, not its retrieval. A legitimate separate experiment; conflating it with this one would be the error. |

**Item-level retrieval depth is fixed and published.** Items are retrieved
to a fixed depth *D* and then collapsed to distinct sessions; a system
that returns many items from one session is neither rewarded nor
punished by the collapse. *D* is declared in `run.py` as a constant with
its justification, and any question where *D* items yield fewer than *k*
distinct sessions is reported as depth-truncated rather than scored as a
miss.

## Arms — crossed honestly

The single most defensible-looking cheat available here would be to run
bettermemory's embedding arm against claude-mem's Chroma-disabled arm.
Both systems ship both configurations, so the arms are crossed:

| arm | bettermemory | claude-mem |
| --- | --- | --- |
| lexical | `mode="hybrid"`, no embedding model | FTS5 only, Chroma disabled |
| semantic | `mode="hybrid"` + embedding model | Chroma enabled |

**Both claude-mem arms are published side by side.** Their reproducible
FTS5 defect — `SessionSearch.ts:285` and `:350` wrap the entire query in
double quotes, making it a phrase query — bites *only* with Chroma
disabled. Publishing only that arm would be indefensible, even though it
is a mode they ship, document, and recommend as a 35GB-RAM mitigation in
their own issue #707.

## Declared confounds

**Ingest bypasses bettermemory's write guardrails, and that is a
limitation, not a neutral choice.** `memory_write` enforces dedup,
transient-marker rejection, credential screening, and a pending-confirm
flow for user-inference. Pushing 500 haystacks of synthetic chat through
the real write path would measure the *write policy*, not retrieval. The
harness therefore writes at the storage layer. Consequence, stated
plainly: **this benchmark measures store + retrieval, not bettermemory's
end-to-end capture behaviour**, and the guardrails that are part of the
product's value are switched off for it. The store size actually written
is reported next to the number of rounds offered, so the gap is visible.

**Per-question isolation.** Each question's haystack is its own store.
bettermemory auto-scopes retrieval to the calling repository, and a shared
store would let sessions from one question serve as distractors for
another — changing the corpus in a way the labels do not describe.

**Abstention questions carry no evidence session.** ~30 of the 500 are
false-premise questions with no `has_answer` session anywhere. Recall@k is
**undefined** for them; they are excluded from every recall figure and
reported as their own line. Including them would silently depress both
systems by the same amount and make the headline look harder-won than it
is.

> **SUPERSEDED by addendum item 3, and left standing so the correction is
> visible.** This paragraph was written from the paper's prose. The
> distributed corpus contains **zero** abstention questions, so the rule
> has nothing to apply to — and the ability it was meant to protect is
> not measurable here at all.

**Absolute levels are not anchored yet.** The paper publishes recall
figures for its own indexing strategies, and those have not been read into
this document. Until they are, the predictions below are deliberately
*relative* — which arm wins and by how much — because a pre-registered
absolute interval invented from memory is worse than none.

## Addendum 2 — declared before the claude-mem arm is built

Written after reading `claude-mem@13.12.4`'s shipped SQLite layer and
probing its MCP server, and **before any claude-mem number exists.**

**1. The enrichment asymmetry, and it cuts AGAINST us — say it as loudly
as their FTS defect.** claude-mem's `observations_fts` index spans **six**
columns: `title`, `subtitle`, `narrative`, `text`, `facts`, `concepts`.
Their real pipeline populates all of them by running an LLM extraction
over a session before storage. This harness writes the raw conversational
round into `text` and leaves the other five empty, because the
alternative is worse in three ways: it costs an API key at write time
(the exact autonomy property this project publishes against), it makes
the run non-$0 and non-deterministic, and **it would mean authoring a
competitor's extraction step ourselves** — the vendor-adapter problem
that disqualified LongMemCode.

So the arms are symmetric in *input* — both systems receive the identical
raw round — and asymmetric in *how much of each system's design is
exercised*. bettermemory stores prose bodies natively and loses nothing.
claude-mem is built to search enriched fields and gets one of six
populated.

**This may understate claude-mem, and no result may be published without
stating it beside the number.** A reader who learns this from the source
rather than from us is entitled to discard the whole artifact. The
symmetric-looking alternative — enriching for them — is rejected on the
record above, not overlooked.

**2. `importObservation` dedups on `(memory_session_id, title,
created_at_epoch)`.** Unlike bettermemory's `Store.write`, which performs
no dedup at all, claude-mem's import path *will* silently collapse rounds
that share those three values. Each round is therefore given a distinct
title and a distinct `created_at_epoch` derived from its session date and
round index. **Items-written vs rounds-offered is reported for the
claude-mem arm too**, and a non-zero shortfall there is a real finding
rather than the by-construction zero that made P5 vacuous on our side.

**2b. The harness WIDENS claude-mem's default date window, and without
that they score zero for a reason unrelated to retrieval.**
`performChromaSemanticSearch` applies `Date.now() - RECENCY_WINDOW_MS`
(90 days) whenever the caller passes no explicit range, and drops every
match older than that *before* the store lookup. LongMemEval's corpus is
dated **2023-05**, roughly three years old, so the unmodified default
discards 100% of semantic matches and the arm reads 0.0 on all 500
questions. The harness therefore passes `dateStart=2020-01-01` /
`dateEnd=2030-01-01`.

This is declared rather than quietly applied because it is the single
most consequential knob in the claude-mem arm. bettermemory has no
comparable recency filter, so there is nothing symmetric to apply on our
side — the asymmetry is that their product has a sensible default for
live use which a historical benchmark corpus violates. **Publishing the
un-widened 0.0 would not be a weak result for a competitor, it would be
a false accusation**, and any future reader who finds this window in
their source is entitled to ask why we did not.

(The parameters are `dateStart`/`dateEnd`. `startDate`/`endDate` are
accepted and silently ignored — worth recording, since a harness that
used the wrong spelling would produce exactly the false zero described
above while appearing to have handled it.)

**3. Ingest cannot go through MCP; retrieval can and does.**
`observation_add` is filtered out of the 14 advertised tools unless the
runtime is Postgres "server" mode, so writes go through `SessionStore`
directly while reads go through the real `search` tool over stdio. This
is the same shape as our side (`Store.write` for ingest, the real search
path for retrieval), and both halves are disclosed together or not at
all.

## Predictions

**P1 — the crossed arms are the whole story, and claude-mem's spread is
wider than ours.** The gap between claude-mem's Chroma-on and Chroma-off
arms exceeds the gap between bettermemory's semantic and lexical arms, at
session-level recall@5, by **at least 10 points**. Mechanism named, not
vibes: their lexical leg degrades to a phrase query, so multi-word
questions — which is nearly all 500 — match only on contiguous runs.
**MISSED if** the spreads are within 10 points, which would mean the
phrase-query defect is masked at this query length and the "config
sensitivity" framing overstates it.

**P2 — the +25-point semantic lift from `bench/retrieval/` reproduces
directionally but shrinks.** bettermemory's semantic arm beats its own
lexical arm at recall@5, by **more than 5 and less than 25 points**.
Rationale: the gold set's documents are 1,100 characters of uniformly
well-written synthetic prose; LongMemEval rounds are colloquial,
deliberately indirect ("instead of stating *I bought a new car*, ask
about car insurance"), and much shorter. **MISSED if** ≤5 (the lift is a
property of our own corpus) or ≥25 (the gold set was *understating* it,
and its caveats need revisiting).

**P3 — multi-session reasoning is our worst class.** bettermemory's
session-level recall@5 on the `multi-session` question type lands at
least **15 points below** its own pooled average across the other scored
types. Most questions there need evidence from two to six sessions, and
recall@5 over distinct sessions is arithmetically brutal when four of the
five slots must all land. **MISSED if** within 15 points, which would be a
genuinely good result and should be reported as one.

**P4 — and this is the prediction that exists to stop a future
overclaim: we do NOT win on `knowledge-update`.** bettermemory's
recall@5 on the knowledge-update class is **within 10 points** of
claude-mem's best arm. Knowledge-update is this project's differentiating
axis and claude-mem is structurally N/A on *correctness* — but recall@k
does not measure correctness. It measures whether the evidence session
comes back. Both systems can retrieve a superseded fact perfectly well;
the difference is whether the store knows it is superseded, and **this
instrument cannot see that.** **MISSED if** we beat them by >10 points —
in which case the cause must be found before it is celebrated, because
the metric does not license the obvious story.

**P5 — the guardrail bypass is material.** Rounds actually stored differ
from rounds offered by **more than 2%** in at least one arm, through
dedup collapsing near-identical conversational filler. **MISSED if**
under 2% in both, meaning the bypass changed little and the confound
above is smaller than stated.

## Addendum — written after reading the schema, before any retrieval ran

The corpus was downloaded and its structure inspected before a single
retrieval call existed. **This text was committed before `run.py` did.**
Three properties were not anticipated by the rules above, and correcting
them afterwards would be how a pre-registration becomes decoration — so
they are declared here with the timestamp they deserve.

**1. `recall@k` needed a sharper definition than "recall@k", and the
distribution of evidence counts is why.** Evidence sessions per question
in the oracle file: **1 → 176, 2 → 250, 3 → 41, 4 → 19, 5 → 11, 6 → 3**.
So 324 of 500 questions require two or more sessions. The metric is
therefore fixed as:

> **recall@k = |retrieved evidence sessions ∩ evidence sessions| /
> |evidence sessions|**, computed per question over the top-*k* **distinct**
> sessions, then **macro-averaged** across questions.

with one consequence stated before it can be spun: for any question where
|evidence| > k, recall@k is **bounded below 1 by construction**. At k=5
that ceiling binds on the 3 six-evidence questions; at k=1 it binds on
324 of 500. Per-k ceilings are published alongside each figure, and the
headline k is **5**. Micro-averaged figures are reported too, because
macro-averaging over questions with wildly different evidence counts is a
choice and not an obviously correct one.

**2. The oracle variant contains no distractors at all, so it cannot
produce a number.** Sessions-per-question is *exactly* the evidence-count
distribution above — the two are identical. `longmemeval_oracle.json` is
the evidence sessions and nothing else, so any retriever that is not
actively broken scores ~1.0 and the corpus cannot discriminate. It is
used **solely to validate adapter plumbing** — ingest, the item→session
mapping, the scorer — and **no oracle figure may be published as a
result.** The headline corpus is `longmemeval_s_cleaned.json`.

**3. There are no abstention questions in the distributed corpus at all,
and that costs this instrument one of the abilities it was chosen for.**
Measured on `longmemeval_s_cleaned.json`: **zero** questions carry the
`_abs` suffix and **zero** have an empty `answer_session_ids`. The six
non-abstention types sum to exactly 500 (knowledge-update 78,
multi-session 133, single-session-assistant 56, single-session-preference
30, single-session-user 70, temporal-reasoning 133). The paper describes
500 questions *including* 30 false-premise abstention items; the cleaned
release ships 500 questions containing **none**.

The exclusion rule written above is therefore moot — there is nothing to
exclude — but the honest consequence is larger than a dropped rule and is
recorded rather than quietly dropped: **Abstention was named as one of
the reasons to prefer this benchmark, and it is not measurable on the
artifact that is actually distributed.** Four scored abilities remain
(information extraction, multi-session reasoning, knowledge updates,
temporal reasoning). Any writeup that lists five abilities because the
paper lists five would be describing a corpus this project never ran.

**4. Thirteen questions repeat a session id inside their own haystack.**
`haystack_session_ids` is not unique within those instances, which
directly touches the attribution rule since scoring collapses to
*distinct* sessions. Handling is fixed here: session ids are deduped on
ingest, a repeated id maps to the union of its rounds, and the 13
affected questions are flagged in the result file so their contribution
can be isolated. They are **not** dropped — silently discarding
inconvenient instances is how a corpus gets tuned.

**5. The whole benchmark sits below the index threshold, so it measures
the same regime `bench/retrieval/` does.** Per-question haystacks hold
38–62 sessions (median 48) and 198–308 rounds (median 245, 123,249
rounds in total). With one store per question and one item per round,
every store lands near 245 items — comfortably under
`_INDEX_THRESHOLD_DEFAULT` (500). So retrieval ranks the full store and
production's SQLite bm25 prefilter never engages. This is the identical
caveat `bench/retrieval/README.md` raises about its own unpadded runs,
and it means **neither directory has yet measured the above-threshold
regime that a large real store would hit.**

**Also pinned: the distributed corpus is not the one the paper
measured.** The live artifacts are `longmemeval_s_cleaned.json` and
`longmemeval_m_cleaned.json` — a *cleaned* revision published after the
paper. Checksums are recorded in `run.py` and in every result file, and
any comparison to a number printed in the paper must name this
discrepancy rather than assume the corpora are identical.

Verified while reading, and worth recording because the adapter depends
on it: `haystack_session_ids`, `haystack_dates` and `haystack_sessions`
are index-aligned in all 500 instances (zero mismatches), every
`answer_session_ids` entry is a subset of its own haystack (zero
orphans), and every turn carries exactly `role` / `content` /
`has_answer`.

## What is not claimed

- **Not helpfulness.** Recall is not usefulness. A retrieved session can
  still be misread by whatever consumes it.
- **Not staleness accuracy.** The headline differentiator is measured in
  `bench/rot/`, on a corpus of real repositories, and nothing here
  touches it. See P4: this instrument is structurally blind to it.
- **Not end-to-end capture.** The guardrail bypass above means the write
  path both projects actually ship is not what ran.
- **Not a QA score.** No judged arm, so nothing here speaks to answer
  correctness — only to whether the evidence was findable.
- **Not a claim about claude-mem's product.** It is driven through a
  documented non-hook write path annotated "legacy" in its own
  `server-parity-map.md`. That is disclosed here rather than left for a
  reviewer to discover, and it is a real limitation: the hook pipeline is
  their default and it is not what ran.
- **A private trial run remains undetectable.** Same as `bench/rot/`: no
  cryptographic fix is available to a single-author project. What reduces
  it is predictions specific enough to be embarrassing, plus a committed
  runner and a pinned corpus checksum so a third party can replicate.
  **Replication is the actual evidence.**

## Addendum 3 — declared before the 5.1 rescue-expansion arm runs, 2026-08-09

The 5.1 engine adds the retrieval campaign's first lane to hybrid
ranking: a document-frequency floor for listed discourse-filler words,
plus a confidence-gated, down-weighted BM25 leg over synthesized
vocabulary (committed tables — inflection variants, clipping
full-forms, dev-domain synonym groups; `src/bettermemory/expansion.py`).

**Development and tuning happened entirely on `bench/retrieval/`, and
that has to be said in the same breath as any number.** Every
parameter — the 0.60 confidence gate, the 0.7 leg weight, the
half-the-collection df floor, the minimum expansion-term length, every
word in every table — was chosen against those twenty questions. That
gold set is the lane's development set, openly. THIS corpus is the
held-out check: untouched during lane development, and the predictions
below are committed before the first 5.1-engine run against it.

Baseline being defended: the committed
`results/baseline-both-arms-2026-08-08.json`, lexical arm — macro
recall@5 0.8935, macro recall@1 0.5246 — reproduced bit-for-bit at the
5.0.0 engine before lane work began.

**P6 — no regression, and this is the kill criterion.** With the lane
on, macro recall@5 lands at or above 0.8900. Below that the lane does
not ship, regardless of its gold-set numbers — a dev-set win paid for
by the held-out set is the definition of overfitting to twenty
questions.

**P7 — the transfer is small but real.** macro recall@5 lands between
0.8930 and 0.9050. Mechanism named: LongMemEval questions are
colloquial, so the filler df-floor should help; the synonym table is
dev-domain vocabulary and should mostly not fire on personal-life
chat; rule-generated inflection variants fire everywhere but add only
near-spellings. **MISSED if** below 0.8930 (the lane transferred
nothing — and if P6 failed too, it transferred harm) or above 0.9050
(filler mispricing was costing chat-style retrieval far more than the
gold set suggested, and the gold set's difficulty caveats need
re-reading in that light).

**P8 — the gate protects confident questions here too.** macro
recall@1 moves by less than two points in either direction from
0.5246. Mechanism: recall@1 is dominated by questions the base
ranking already answers confidently, and the coverage gate should
keep the rescue leg out of exactly those. **MISSED if** the move is
two points or larger either way — the gate's dev-set calibration did
not transfer.

## Addendum 4 — round-2 experiment 1: OUTPUT-SIDE df-gating of emitted expansion terms, 2026-08-10

Round 1's record names the next experiment and leaves it unrun:
*"the named next experiment is df-gating the emitted terms,
re-preregistered on both instruments"* (`README.md`, "What this buys
the campaign"). This is that re-preregistration. Predictions continue
the existing numbering — P1–P5 (original), P6–P8 (addendum 3) — so
this document owns **P9–P15**.

It is a **two-instrument** commitment: `bench/retrieval/` (development)
and this directory (held-out) are both bound, because the thing under
test is precisely a sign flip between them.

### Ordering, stated plainly because it is the one thing a reader cannot check later

A pre-registration is worth exactly what its ordering is worth, so here
is this one's, without varnish:

1. The **decision rules and every threshold below were fixed before the
   statistic that judges them existed.** They are inherited verbatim
   from a round-2 draft written during the 2026-08-10 audit sweep,
   before `bench/df_census.py` was written and before any census ran.
   Nothing in Gate 0 was chosen after seeing a number.
2. The **census was committed before this document** (`df-census-2026-08-10.json`,
   statistics only — no recall, no labels, no ranking outcome). So at
   the moment this text lands, Gate 0's *verdict* is computable from
   committed artifacts, and its author knows it. That is disclosed
   rather than hidden: the property that makes a pre-registration mean
   something is that the RULE predates the OUTCOME it judges, and that
   property holds. The reader can check it the hard way — a 5×
   requirement is nowhere near any number the census produced, in
   either direction, which is not what a fitted threshold looks like.
3. **No gated recall run has been executed at the time of writing**, on
   either instrument. P10–P15 judge runs that do not exist yet.

### The round-1 measurements this document is not allowed to contradict

Re-derived on the **5.1.1 engine** — round 1's constants were measured
before the lane's filler-emission repair, so every one of them was
re-run rather than copied. Artifacts: `rebaseline-*-2026-08-10.json`
here, `rebaseline-*-2026-08-10.json` in `bench/retrieval/results/`.

| arm | macro@1 | macro@5 | macro@10 | vs round 1 |
| --- | --- | --- | --- | --- |
| baseline, lane off | 0.5246 | 0.8935 | 0.9443 | identical |
| lane on, both mechanisms | **0.4772** | 0.8770 | 0.9471 | @1 was 0.4752 |
| filler df-floor only | 0.5226 | 0.8935 | 0.9463 | identical |
| expansion leg only | 0.4732 | 0.8790 | 0.9471 | identical |

**C1 — expansion value flips SIGN with store shape, from identical
code.** On the technical-prose gold set the lane is worth **+15
recall@1 / +30 recall@5** as-asked (35%/60% → 50%/90%,
`rebaseline-lane-unpadded-2026-08-10.json` against
`rebaseline-off-unpadded-2026-08-10.json`) — every dev-set cell
byte-identical to round 1, so the 5.1.1 repairs are dev-set neutral.
On this corpus the same engine costs **−4.74 macro@1 and −1.65
macro@5**. No parameter differed. Any round-2 mechanism must therefore
be a function of the STORE, not a constant.

**C2 — input-side token gating is spent, in both of its forms.** The
hard form (strip filler from the query) was measured on the gold set
and rejected: filler words *"delete the only hooks some queries
have"*. The soft form that shipped — the filler df-floor — reproduces
this corpus's baseline to four decimals (0.8935). Aggressive destroys
the dev set; conservative does nothing here. **Round 2 must not touch
query tokens at all.**

**C3 — the emitted terms carry the entire regression.** The leg-only
ablation reproduces the full damage without the floor (0.4732/0.8790),
now from a committed `--ablate leg-only` rather than a working-tree
patch. Per question, re-derived from the 5.1.1 sidecar: **25 questions
moved down at @5, 9 up.** The named mechanism: `"planning" → "plan"`
is matched by many rounds in a chat store and by almost nothing in a
technical one.

**C4 — the lane engages on a third of this corpus, not on most of it
(new, and it corrects round 1's prose).** The census measures the
coverage gate opening on **165 of 500** questions here and 43 of 60
dev probes. Round 1's README says the leg "engaged broadly" on
conversational questions; measured, it is 33%. Every rate below is
stated against that denominator instead of an impression.

### Hypothesis

> **An emitted expansion term's document frequency in the collection
> being ranked separates the term class that helps from the term class
> that harms, and the separation is available in code before the term
> casts a single vote.**

Its most likely failure is not "the gate is badly tuned" but "df is not
the separating variable" — which Gate 0 detects **before** any gated
arm runs.

### Why df, when BM25 already prices on df

`_hybrid_fuse` fuses by **RANK, not score**. IDF therefore only
reorders candidates *within* the expansion leg; it cannot reduce the
leg's *influence*, because the contribution is
`_RESCUE_LEG_WEIGHT / (rrf_k + rank)` whether the leg's rank-1
candidate was found by a rare discriminating synonym or by a verb half
the store contains. Okapi's non-negativity compounds it: at df = N/2
the IDF is `log 2` ≈ 0.693, which the shipped code calls *"~14% of a
genuinely-rare term's weight"* — deflated, never zero, and invisible to
the fusion after `_id_order`. Removal changes the leg's ORDER, which is
the only thing RRF reads; and when removal empties the term list the
leg does not run at all.

### Why this is not C2 in a new coat

The gate operates on **synthesized** vocabulary the caller never typed.
Dropping `"plan"` (emitted from `"planning"`) removes nothing the
caller said — `"planning"` still scores in both base legs. C2's failure
mode is structurally unreachable from the output side. The symmetric
risk is the opposite one, and it is preregistered as P10.

### Exact mechanism

One insertion point, one constant, one telemetry channel. No new config
key, no change to any table, no change to `morph_variants`, no change
to the index stream.

Inside the hybrid rescue block, between the `exp_stats` resolution and
the leg's scoring call:

```
exp_terms = _expansion_terms_impl(...)                 # unchanged
exp_stats = merge(hybrid_stats, provider(exp_fetch))   # unchanged
kept      = _df_gate_terms(exp_terms, stats=exp_stats,
                           candidate_tokens=candidate_tokens,
                           pool_n=len(candidates))     # NEW
if kept:                                               # NEW
    exp_leg = _score_bm25(candidates, kept, ..., corpus_stats=exp_stats)
```

Ordering is load-bearing: the provider fetch stays on the **ungated**
list (you need df to decide), the gate runs on its result, and
`_score_bm25` receives only survivors.

**df resolution**, mirroring `compute_idf`'s override rule so gate and
scorer cannot disagree: provider df when present and `> 0`; else pool
df over the already-materialized `candidate_tokens` content streams in
ONE pass (not one per term); else **fail OPEN** — keep the term. A
stale index degrades the gate to today's behaviour, never to silent
term deletion.

**Keep `t` iff `df / N <= τ`.** `df == 0` terms are kept; they match
nothing and cost nothing, and they are counted separately so they
cannot inflate the anti-vacuity metric.

**Empty survivors: the leg does not run.** `scored` stays the base
fusion, `matched_leg` reports `lexical`, and the result is
byte-identical to `rescue_expansion=False` for that query. Not a
fallback — the intended behaviour on a store where every emitted term
is common.

**Telemetry**: an optional out-dict on `search()`, following the
`matched_leg_out` pattern, default `None` so production pays nothing.
Per query: `emitted`, `kept`, `kept_with_df_gt0`, `dropped`, `leg_ran`,
`max_df_ratio_dropped`. Without it P13 is unscoreable.

### Parameters

| name | value | how it is set |
| --- | --- | --- |
| `_RESCUE_EXPANSION_DF_MAX` (τ) | **0.05** | from the df census alone — see below |
| `_RESCUE_COVERAGE_GATE` | 0.60 | **unchanged.** Frozen for round 2. |
| `_RESCUE_LEG_WEIGHT` | 0.7 | **unchanged.** Frozen for round 2. |
| `_FILLER_DF_FLOOR_RATIO` | 0.5 | **unchanged.** Neutral on this corpus (C2). |
| `_MIN_EXPANSION_LEN` | 3 | **unchanged.** |

**Exactly one parameter moves in round 2.** Round 1 changed two
mechanisms at once and needed a three-arm ablation to find out which
did the damage; that cost is not paid twice.

**τ is fixed at 0.05 from corpus statistics only — no recall input at
any stage.** This is a deliberate strengthening over the round-2 draft,
which selected τ by intersecting a dev-set *recall* sweep with the
census and therefore could not have τ final in this commit. The rule
applied instead: τ is the round number nearest the held-out live
emitted-term p75 (0.0607) that also sits above the dev-set median
(0.0361), so the gate is non-binding for the median dev-set term and
binding for the upper quartile of held-out terms. A dev-set recall
sweep is still reported afterwards, as a robustness check on a fixed τ
rather than as the thing that chose it.

### Gate 0 — the pre-run kill, and the cheapest thing in this document

This directory's own closing lesson is that *"the oracle ceiling is
cheap to compute and would have closed this item in an afternoon
instead of a phase"*. Round 2 pays it forward: two offline checks,
computable from committed artifacts, run before a single gated recall
number is produced.

**Gate 0a — separability.** The median df/N of emitted live terms on
the round-1 regressed questions must be **at least 5×** the median
df/N of emitted live terms on the dev set's leg-engaging asked probes.
**MISSED if** below 5×, under every reasonable reading of "the dev
set's rescued questions" — the reading is not allowed to be chosen
after the fact, so all four are reported.

**Gate 0b — reachability.** At τ, the gate must drop at least one
emitted term on **≥ 20 of the 25** regressed questions, and must leave
`a89d7624`'s emitted set changed. If it would not alter the term set on
the questions that broke, it cannot repair them.

**Failing either publishes "df does not identify the promiscuous
class" as the result and ENDS the experiment: no gate is implemented,
no gated arm runs, `rescue_expansion` stays opt-in.** That negative is
worth more than a null recall run, because it retires the mechanism
rather than leaving it "untuned".

### Instrument A — `bench/retrieval/` (DEVELOPMENT set)

20 blind-authored questions, 180 documents, lexical arm only. **n=20:
one question is 5 points, and nothing here resolves finer.**

```sh
.venv/bin/python bench/retrieval/run.py --rescue-expansion on --json
.venv/bin/python bench/retrieval/run.py --rescue-expansion on --pad-to 600 --prefilter both --json
```

Gate off must reproduce `rebaseline-lane-*-2026-08-10.json` exactly.

### Instrument B — this directory (HELD-OUT set)

500 questions, `longmemeval_s_cleaned.json` sha256 `d6f21ea9…c3a442`,
retrieval depth 200, lexical arm, `--per-question` sidecar
**mandatory**. Four arms, in this order:

1. **baseline, lane off** — must reproduce 0.5246 / 0.8935 / 0.9443 to
   four decimals. **If it does not, STOP.**
2. **lane on, gate off** — must reproduce **0.4772 / 0.8770 / 0.9471**.
   Second determinism check and the paired control.
3. **lane on, gate on at τ** — the experiment.
4. **`--ablate leg-only` + gate on** — isolates the gate against the
   mechanism that carried the whole regression, against 0.4732 /
   0.8790 / 0.9471.

Process requirements: every published artifact carries
`provenance.tree_dirty == false` — **now achievable for every arm**,
since `--rescue-expansion` and `--ablate` are committed flags and no
arm needs a working-tree patch. τ is frozen before Instrument B runs;
any post-hoc change voids the arm and requires a new pre-registration
with a new date.

### Predictions

**P9 — the gate reaches the failure.** Gate 0a and Gate 0b both pass.
**MISSED if** either fails — publish "df is not the separating
variable", run nothing else, the default stays off.

**P10 — the dev-set win survives the gate.** Unpadded lexical: asked
recall@1 ≥ 45% and recall@5 ≥ 85% (one question of slack each), requery
**exactly** 80%/100%. **MISSED if** asked drops by more than one
question at either k, or requery moves at all.

**P11 — no held-out regression. THE kill criterion.** macro@5 ≥
**0.8900**.

> **Revision, declared rather than inherited.** Addendum 3's P6 reads
> "Below that **the lane does not ship**, regardless of its gold-set
> numbers." What shipped was an opt-in lane — the default did not ship,
> but the lane did. That was a decision taken after seeing the number,
> and round 1's results table restated the criterion to match it
> instead of recording the deviation. P11 keeps P6's LINE (0.8900) and
> states its consequence honestly and in advance: **below 0.8900 the
> default does not flip, and the lane's existing opt-in status is not
> re-litigated by this experiment.** That is weaker than P6 as written.
> It is declared here, before the run, in place of being applied
> afterwards.

**P12 — recall@1 is the sharper test, because it is where round 1
died.** macro@1 ≥ **0.5046** (within 2 points of the 0.5246 baseline),
i.e. recovering at least 2.74 of the 4.74 points the ungated lane
lost. **MISSED if** below 0.5046.

**P13 — the expected shape is a NULL on the held-out set, declared in
advance so it cannot be sold as a win.** macro@5 in [0.8900, 0.8970]
and macro@1 in [0.5046, 0.5346]. **The lane is not predicted to help
conversational stores. It is predicted to stop hurting them.**
**Anti-vacuity clause:** a null only counts if the leg actually ran.
The census measures the coverage gate opening on **165 of 500 (33.0%)**
questions with the gate absent, so the bar is set against that measured
denominator rather than an impression: `leg_ran` must hold on **≥ 20%
of all questions** (≥ 100 of 500, i.e. the gate may not silence more
than about two fifths of the engaged population), with non-empty
`kept_with_df_gt0` on most of those. Below that the gate has turned the
lane off in disguise and P13 is scored **vacuous, not held**.

**P14 — reach is preserved, not just noise removed.** macro@10 ≥
**0.9443** (baseline). **MISSED if** below.

**P15 — the above-threshold regime does not get worse.** With the gate
on, recall@5 stays ≥ 70% asked / ≥ 75% control padded-600, and ≥ 75%
asked / ≥ 75% control forced-180 — round 1's measured post-prefilter
levels, both probes stated because the as-asked and control cells moved
by different amounts (15 and 10 points).

### Kill criteria, collected in one place

1. **Gate 0a or 0b fails** → dead on arrival, no gate implemented, no
   recall run, publish the negative (P9).
2. **Baseline does not reproduce to four decimals** → stop.
3. **Ungated lane arm does not reproduce 0.4772 / 0.8770 / 0.9471** →
   stop, the harness or engine moved.
4. **macro@5 < 0.8900** → the default does not flip (P11).
5. **Dev set loses more than one question on the casual probe, or
   requery moves at all** → the gate is not free on technical stores.
6. **`tree_dirty` true on any artifact** → run void.

### What would justify flipping `rescue_expansion` default-on

**All seven, conjunctively. Any one short and the lane stays opt-in.**

1. macro@5 ≥ **0.8900** (P11). *(Round 2's draft stated this twice —
   once as ≥ 0.8900 and once as "≥ baseline − 0.0035", which is the
   same number to the digit. It is one condition and is written once.)*
2. macro@5 additionally ≥ **0.8935**, the baseline itself. This is the
   clause the redundant pair was reaching for and never expressed: not
   merely "above the kill line" but "the held-out set is not paying at
   all for the dev set's win".
3. macro@1 ≥ 0.5046 (P12).
4. macro@10 ≥ 0.9443 (P14).
5. Dev-set win preserved: asked ≥ 45%/85%, requery exactly 80%/100%.
6. Non-vacuity: `leg_ran` on ≥ 20% of held-out questions with non-empty
   `kept_with_df_gt0`.
7. Both determinism reproductions exact, `tree_dirty: false`
   everywhere, artifacts committed, and the flip lands as its own
   reviewed change citing this document.

**What explicitly does NOT justify a flip:** a larger dev-set win (the
gold set is the development set; more of it is more overfitting); a
held-out gain concentrated in one question class; an improvement only
at @10; or "it is opt-in anyway, so the risk is low" — the flip is
precisely the removal of that property.

### Declared confounds

**1. The held-out set is no longer perfectly held out.** The census
reads this corpus's emitted-term df histograms. That is a corpus
statistic, never an outcome, and it is declared before it is read — but
it is a real weakening. **Consequence owed: if this ships, a third
instrument is required for the next clean held-out check. LongMemEval
has now informed a parameter and cannot be spent twice.**

**2. The df source differs by regime, and only one of the three is
clean.** Below the index threshold — both instruments' default, and
this corpus's *only* mode, since the runner passes no
`corpus_stats_provider` — the pool **is** the admitted collection and
pool df is exact. Above the threshold the pool is a bm25-nominated
top-50 slice biased *away* from synthesized vocabulary, so the gate
must use provider df; where the provider is silent it fails open,
meaning promiscuous terms survive above the threshold. Unmeasured on
Instrument B, which has never run above the threshold at all.

**3. Scope changes the denominator, and production is scoped.** df/N is
computed over the ADMITTED set, which under auto-scoping is one repo. A
term rare store-wide can be common inside a single scope, so τ
calibrated on two unscoped benchmark corpora may not transfer. **This
is the most likely place for the gate to mis-calibrate in real use, and
neither instrument can see it.**

**4. Small stores make the gate meaningless.** At ~245 items per
question here, df/N moves in 0.4% steps and τ=0.05 is a ~12-document
boundary. On a 20-memory scope the boundary is one document and the
gate is noise. Both instruments measure the 180–600 document regime
only.

**5. Emitted-term counts include non-words.** `morph_variants` is a
rule, so `"planning"` emits `planed`/`plann` beside `plan`. They have
df 0, cost nothing, and are harmless to ranking — but they inflate
"terms survived the gate". Hence `kept_with_df_gt0` as a separate field
and hence the anti-vacuity clause keying on it. Measured: **646 of
1094** emitted terms here have df > 0.

**6. n=20 on the dev set.** One question is 5 points; "preserved"
cannot mean anything finer.

**7. Cost.** The gate adds **one** pass over the already-materialized
candidate token streams per rescued query, not one per term. Runtime
guard: the gated arm must land within **1.10×** the ungated lane arm
measured in the same session.

### What is not claimed

- **Not that the lane will help conversational stores.** P13 predicts a
  null there on purpose. The claim under test is "stops hurting".
- **Not helpfulness.** Recall is not usefulness, on either instrument.
- **Not correctness or staleness.** `bench/rot/` owns that axis.
- **Not a comparative claim.** No claude-mem arm runs in round 2.
- **Not the above-threshold regime on Instrument B.** Never run there.
  Nominating on query + expansion terms remains the next increment and
  is out of scope: it changes the pool, and this document changes only
  the votes.
- **A private trial run remains undetectable.** What reduces it is
  predictions specific enough to be embarrassing, committed runner
  flags instead of patch drivers, pinned checksums, and replication.
