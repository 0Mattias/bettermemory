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

## Addendum 5 — round-3 experiment: capping the rescue leg's VOTE when its own evidence is weak, 2026-08-10

Round 2 killed df-gating and named the mechanism on its way out. This
addendum tests what that finding pointed at. Predictions continue the
numbering — P1–P5, P6–P8 (addendum 3), P9–P15 (addendum 4) — so this
document owns **P16–P21**.

Two instruments, both bound: `bench/retrieval/` (development) and this
directory (held-out).

### What round 2 established, and why it changes the target

Addendum 4's Gate 0 fired: on the 25 regressed held-out questions the
emitted terms sit at median df/N **0.0268**, against **0.0361** on the
dev set's leg-engaging asked probes — 0.74× where 5× was required, and
no τ separates the two populations (`results/gate0-2026-08-10.json`).

The harmful terms are individually **rare**. So the damage was never
that the leg's vocabulary is common; it is that the leg's *vote* is
unconditional. `_hybrid_fuse` fuses by RANK: the leg contributes
`_RESCUE_LEG_WEIGHT / (rrf_k + rank)` whether its rank-1 was found by a
discriminating synonym or by a near-tie among candidates it can barely
tell apart. Round 2's own "why df, when BM25 already prices on df"
section is the argument — and it applies just as well against df as
for it, which is what the kill demonstrated.

**Round 3 therefore leaves the vocabulary alone and conditions the
VOTE.**

### Hypothesis

> **The expansion leg's own internal separation — how far its rank-1
> stands above its rank-2 — predicts whether that leg is about to vote
> correctly, and it predicts it well enough that withholding the vote
> of a poorly-separated leg costs a technical store little and saves a
> conversational one.**

Falsifiable, and its most likely failure is that separation predicts on
the dev set and not on the held-out set — the same transfer failure
round 1 suffered, which is why the held-out arm is the test and the dev
arm is only a guard.

### Why `margin_ratio`, and why not the alternatives

Measured on the dev set (`bench/retrieval/results/leg-census-2026-08-10.json`,
41 engaged legs — 14 whose rank-1 is the gold document, 27 whose is
not):

| signal | correct legs (p50) | incorrect legs (p50) | separation |
| --- | --- | --- | --- |
| `top_score` | 10.63 | 6.60 | 1.6× |
| `margin` (top1 − top2) | 1.42 | 0.30 | 4.7× |
| **`margin_ratio`** ((top1 − top2)/top1) | **0.189** | **0.047** | **4.0×** |
| `top_matched` | 2.0 | 2.0 | none |
| `leg_size` | 35 | 40 | none |

`top_matched` and `leg_size` carry no signal at all and are out.
`top_score` and `margin` both separate, and both are **rejected on a
transfer argument, not a fitting one**: a raw BM25 score depends on
collection size, average document length and the IDF scale, none of
which are comparable between 180 documents of technical prose and ~245
conversational rounds. C1 requires a mechanism that is a function of
the store, not a constant; an absolute score threshold is exactly the
constant that cannot transfer. **`margin_ratio` is scale-free by
construction** — it is a ratio of two scores drawn from the same leg,
in the same units, on the same collection — which is the only reason
it is a candidate for a threshold fixed on one corpus and applied to
another.

### Exact mechanism

One insertion point, one constant. No new config key, no change to any
table, no change to `morph_variants`, no change to the coverage gate,
no change to the index stream. Entirely inside the existing
`rescue_expansion` lane, so a default install is untouched.

In `search()`'s hybrid rescue block, after the leg is scored and before
it joins the fusion:

```
exp_leg = _score_bm25(candidates, exp_terms, ...)      # unchanged
if exp_leg:
    if _leg_margin_ratio(exp_leg) < _RESCUE_LEG_MIN_MARGIN:   # NEW
        exp_leg = []                                          # NEW
if exp_leg:
    scored = _hybrid_fuse(rankings + [exp_leg], ...)   # unchanged
```

`_leg_margin_ratio` reads the leg's own ordering — the same
`(score, created, id)` ordering `_id_order` applies before fusion, so
the "rank-1" the cap judges is the rank-1 that would have voted — and
returns `(top - runner_up) / top`, or `1.0` for a single-candidate leg
(nothing competes with it) and `0.0` for a non-positive top score.

**A capped leg does not run.** `scored` stays the base fusion,
`expansion_ids` stays empty, `matched_leg` reports `lexical`, and the
result is byte-identical to `rescue_expansion=False` for that query.
This is the same shape the lane already has when `exp_terms` is empty —
not a new failure mode, an existing one reached by a new condition.

### Parameters

| name | value | how it is set |
| --- | --- | --- |
| `_RESCUE_LEG_MIN_MARGIN` (θ) | **0.12** | dev-set leg census; rule below |
| `_RESCUE_COVERAGE_GATE` | 0.60 | **unchanged.** Frozen. |
| `_RESCUE_LEG_WEIGHT` | 0.7 | **unchanged.** Frozen. |
| `_FILLER_DF_FLOOR_RATIO` | 0.5 | **unchanged.** Frozen. |
| `_MIN_EXPANSION_LEN` | 3 | **unchanged.** Frozen. |

**Exactly one parameter moves**, as in round 2.

**θ = 0.12 by a stated rule, applied before any recall run: the largest
round value strictly below the dev set's correct-leg `margin_ratio`
p25 (0.1235).** The rule is "preserve, then take the largest", the same
discipline addendum 4 used — not "take the value that scores best",
which at n=41 is fitting noise. Its consequence is arithmetic, not a
target: the cap keeps **12 of 14** correct legs and drops **23 of 27**
incorrect ones, lifting the precision of the surviving legs from
**0.341 to 0.750** (2.20×).

The two correct legs it drops both sit at `margin_ratio` 0.0493, inside
the incorrect legs' range. They are the price, they are visible here in
advance, and P17 is what says how much of the dev-set result they are
allowed to cost.

### Gate 0 — dev-side only, and weaker than round 2's on purpose

Addendum 4 spent a held-out corpus statistic to build its pre-run kill,
and its own confound 1 recorded the consequence: *"LongMemEval has now
informed a parameter and cannot be spent twice."* **Round 3 honours
that. No held-out statistic is read before the run — not a census, not
a distribution, nothing.** The cost is stated plainly: this round has
**no cheap pre-run check that can predict transfer**, and Gate 0 below
is a sanity floor on the dev side alone, not evidence about the
held-out set.

**Gate 0a — the signal exists on the dev set.** Correct legs' median
`margin_ratio` ≥ 2× incorrect legs'. *(Measured before this commit:
0.189 vs 0.047, 4.0×.)*

**Gate 0b — θ is not merely an off-switch.** At θ the cap must keep ≥ 10
of the 14 correct legs AND lift kept-leg precision ≥ 1.5× over no cap.
*(Measured: 12 of 14, 0.750/0.341 = 2.20×.)*

Both are computable from the committed leg census, and **both pass** —
which is why this round proceeds to implementation where round 2 did
not. A reader should weight that accordingly: passing a dev-side gate
is much weaker evidence than passing a two-corpus one, and the
held-out arm is doing nearly all the work.

### Instrument A — `bench/retrieval/` (DEVELOPMENT set)

20 blind-authored questions, 180 documents, lexical arm. **n=20: one
question is 5 points.**

```sh
.venv/bin/python bench/retrieval/run.py --rescue-expansion on --json
.venv/bin/python bench/retrieval/run.py --rescue-expansion on --pad-to 600 --prefilter both --json
.venv/bin/python bench/retrieval/run.py --rescue-expansion on --index-threshold 180 --prefilter both --json
```

Cap off must reproduce `rebaseline-lane-*-2026-08-10.json` exactly.

### Instrument B — this directory (HELD-OUT set)

500 questions, `longmemeval_s_cleaned.json` sha256 `d6f21ea9…c3a442`,
depth 200, lexical arm, `--per-question` sidecar mandatory. Four arms:

1. **baseline, lane off** — must reproduce 0.5246 / 0.8935 / 0.9443 to
   four decimals. **If it does not, STOP.**
2. **lane on, cap off** — must reproduce 0.4772 / 0.8770 / 0.9471.
3. **lane on, cap on at θ** — the experiment.
4. **`--ablate leg-only` + cap on** — isolates the cap against the
   mechanism that carried the whole regression, against 0.4732 /
   0.8790 / 0.9471 uncapped.

Every artifact carries `provenance.tree_dirty == false`; both flags are
committed, so no arm needs a working-tree patch. θ is frozen before
Instrument B runs.

### Predictions

**P16 — the cap fires where the damage is.** On the held-out set the
cap withholds the leg on at least **40%** of the questions where it
engages. Mechanism: the leg's separation is what the dev census says
distinguishes a correct leg, and the held-out regression is 25 of the
165 engaging questions. **MISSED if** below 40% — the cap is not
reaching the population, and any null below is vacuous rather than
informative.

**P17 — the dev-set win survives.** Unpadded lexical: asked recall@1 ≥
**45%** and recall@5 ≥ **85%** (one question of slack each against
50%/90%), requery **exactly** 80%/100%. **MISSED if** asked drops more
than one question at either k, or requery moves at all — the cap is
withholding legs that were carrying the result, and the two correct
legs at `margin_ratio` 0.0493 were not the only price.

**P18 — no held-out regression. THE kill criterion, at round 1's exact
line.** macro@5 ≥ **0.8900**. **Below that the default does not flip**,
whatever the dev set says. (Same line and same declared-weaker
consequence as addendum 4's P11: the lane's existing opt-in status is
not re-litigated by this experiment.)

**P19 — recall@1 recovers, because that is where the lane died.**
macro@1 ≥ **0.5046**, i.e. recovering at least 2.74 of the 4.74 points
the uncapped lane loses. Mechanism: 25 questions moved down under
unconditional votes; a withheld vote restores the baseline ranking
exactly. **MISSED if** below 0.5046 — separation does not transfer, and
the dev-side signal was a property of 41 observations rather than of
the mechanism.

**P20 — the expected shape is a NULL, declared so it cannot be sold as
a win.** macro@5 in **[0.8900, 0.8970]** and macro@1 in
**[0.5046, 0.5346]**. **The cap is not predicted to help conversational
stores. It is predicted to stop hurting them.** Anything above those
bands must be explained by the arms before it is celebrated.

**P21 — reach is preserved.** macro@10 ≥ **0.9443** (baseline).
Uncapped, the lane *improves* @10 (0.9471) while destroying @1 and @5 —
the signature of a recall mechanism with a precision problem. A cap
that fixes precision should keep that. **MISSED if** @10 falls below
baseline: the cap is withholding good legs too.

### Kill criteria, collected in one place

1. **Gate 0a or 0b fails** → dead on arrival, no implementation, no run.
2. **Baseline does not reproduce to four decimals** → stop.
3. **Uncapped lane arm does not reproduce 0.4772 / 0.8770 / 0.9471** →
   stop, the harness or engine moved.
4. **macro@5 < 0.8900** → the default does not flip (P18).
5. **Dev set loses more than one question on the casual probe, or
   requery moves at all** → the cap is not free on technical stores.
6. **The cap fires on < 40% of engaging held-out questions** → any null
   is vacuous (P16).
7. **`tree_dirty` true on any artifact** → run void.

### What would justify flipping `rescue_expansion` default-on

**All seven, conjunctively. Any one short and the lane stays opt-in.**

1. macro@5 ≥ 0.8900 (P18).
2. macro@5 ≥ **0.8935**, the baseline itself — the held-out set pays
   nothing at all for the dev set's win.
3. macro@1 ≥ 0.5046 (P19).
4. macro@10 ≥ 0.9443 (P21).
5. Dev-set win preserved: asked ≥ 45%/85%, requery exactly 80%/100%.
6. Non-vacuity: the cap fires on ≥ 40% of engaging held-out questions
   and the leg still runs on the rest.
7. Both determinism reproductions exact, `tree_dirty: false` everywhere,
   artifacts committed, and the flip lands as its own reviewed change
   citing this document.

**What explicitly does NOT justify a flip:** a larger dev-set win (the
gold set is the development set); a held-out gain concentrated in one
question class; an improvement only at @10; or "it is opt-in anyway".

### Declared confounds

**1. θ is fitted to 41 dev-set observations.** Fourteen of them are the
correct-leg population the preserve rule keys on. That is a small
sample, the rule was chosen to be robust rather than optimal, and it is
still fitting. The held-out arm is the only thing that can speak to
whether the number transfers.

**2. The dev set's correctness label is "the leg's rank-1 is the gold
document", not "the leg helped".** A leg whose rank-1 is wrong can
still move the gold document up the fused list from rank 3 to rank 2,
and this census would count it as incorrect. So the 27 "incorrect" legs
are an upper bound on the harmful population, and θ may be more
aggressive than the harm warrants. P17 is the guard.

**3. Above the index threshold the leg ranks a bm25-nominated slice.**
The margin is then computed over a pool already biased toward the
caller's vocabulary, so `margin_ratio` means something slightly
different there. Instrument A measures this regime (padded-600 and
forced-180); Instrument B has never run above the threshold at all.

**4. Scope changes the pool, and production is scoped.** A leg's
separation depends on what else is in the admitted set. Neither
instrument measures a scoped store.

**5. Single-candidate legs are maximally separated by definition** and
always survive the cap. On a small store that is most legs, so the cap
does progressively less as a store shrinks — the opposite of the
df-gate's failure mode, and equally worth stating.

**6. n=20 on the dev set.** One question is 5 points.

**7. Cost.** The cap reads two floats off a list the engine already
built and sorted. No new pass over anything. Runtime guard: the capped
arm lands within **1.05×** the uncapped arm in the same session.

### What is not claimed

- **Not that the cap will help conversational stores.** P20 predicts a
  null on purpose. The claim under test is "stops hurting".
- **Not helpfulness, not correctness, not staleness.**
- **Not a comparative claim.** No claude-mem arm runs in round 3.
- **Not the above-threshold regime on Instrument B.**
- **Not that `margin_ratio` is the best available signal** — only that
  it is the best of the five the dev census measured, and the only one
  of the two that separate which can transfer across corpora.

## Addendum 6 — round-4 experiment: the cap carries its own calibration, 2026-08-10

Round 3 gained the campaign's first held-out ground and then failed on
calibration, not on direction. This addendum fixes the calibration
problem the round-3 results section states. Predictions continue the
numbering — P1–P5, P6–P8, P9–P15, P16–P21 — so this document owns
**P22–P28**.

### What round 3 established

| arm | macro@1 | macro@5 | macro@10 |
| --- | --- | --- | --- |
| baseline, lane off | 0.5246 | 0.8935 | 0.9443 |
| lane on, no cap | 0.4772 | 0.8770 | 0.9471 |
| lane on, fixed θ = 0.12 | 0.4916 | 0.8830 | 0.9466 |

Conditioning the leg's vote recovered 30% of the macro@1 loss and 36%
of the macro@5 loss — the first mechanism to move this corpus toward
baseline. It missed the 0.8900 line and cost the dev set three
questions at recall@5.

**The measured cause was calibration, not mechanism.** θ = 0.12 sits
*above* the dev set's median engaged-leg `margin_ratio` (0.0698) and
*below* the held-out median (0.1359), so one constant was aggressive
on the corpus it came from and permissive on the corpus it was aimed
at. It fired on 61% of engaged dev legs and 43.9% of held-out ones.
Scale-free was necessary and not sufficient: the ratio ignores
collection size, but its *distribution* still differs by corpus, and a
fixed quantile is not a fixed value.

### Hypothesis

> **A leg has no opinion when its top candidate does not stand out
> against the leg's OWN internal structure — and that comparison, being
> drawn entirely from the leg being judged, calibrates itself to
> whatever store the leg was built on.**

The prediction that makes it falsifiable is not about recall alone: a
self-calibrating criterion should fire at a **comparable rate on both
corpora**, where the fixed θ fired at 61% and 43.9%. P22 is that test,
and it can fail even if recall improves.

### The derivation rule — this, not a value, is what is preregistered

Every input, the statistic, the window, the bounds and the degenerate
cases are fixed here, before any code exists.

**Statistic — `standout`.** Over the leg's own fusion ordering
(`(score, created, id)` descending, the ordering `_id_order` applies,
so the rank-1 judged is the rank-1 that would vote):

```
scores = [s0, s1, ... ]            # the leg's top _STANDOUT_WINDOW candidates
gaps   = [s0-s1, s1-s2, ... ]      # adjacent differences
standout = gaps[0] / mean(gaps[1:])
```

The top gap, measured against the average gap elsewhere in the same
leg. **The leg votes iff `standout >= K`.**

**Window — the leg's top 12 candidates (`_STANDOUT_WINDOW = 12`).**
Load-bearing and fixed here because the constant means nothing without
it: a long flat tail would drag `mean(gaps[1:])` toward zero and make
every leg look like a standout. 12 is the window the dev census
recorded and therefore the window K is derived on; engine and census
must read the same shape or the number does not transfer for a reason
that has nothing to do with corpora.

**Degenerate cases — all fail OPEN (the leg votes), matching the
df-gate's "a stale index degrades the gate to today's behaviour, never
to silent deletion":**

- fewer than 3 candidates → no comparison set exists → vote;
- `mean(gaps[1:]) <= 0` (everything below rank 1 tied) with a positive
  top gap → a perfect standout → vote;
- the same with a zero top gap → a total plateau, no opinion → **do not
  vote**. This is the one degenerate case that withholds, and it is the
  shape the whole mechanism is named for.

> **Correction, 2026-08-10, made during implementation and before any
> arm ran.** Addenda 5 and 6 both describe a withheld leg as leaving
> the query "byte-identical to `rescue_expansion=False`". That is
> wrong, and the unit tests caught it: the filler df-floor is keyed on
> `rescue_expansion`, not on the leg, so it still applies when the leg
> is withheld. A withheld leg reproduces **a lane-on query whose leg
> found nothing**, which is the same shape the lane already has when
> `exp_terms` comes back empty. On a store where the floor is inert the
> two coincide, which is why the overstatement survived round 3. The
> mechanism is unchanged and no prediction moves — arm 4 (`floor-off`)
> is in fact the arm that prices the difference — but the claim was
> wrong and is corrected here rather than quietly.

**K = 2.5, by the same preserve-then-take-the-largest rule addenda 4
and 5 used:** the largest round value strictly below the minimum
`standout` among the dev set's correct legs (2.6618). Not the value
that scores best — at n=41 that is fitting noise.

**Why `standout` and not the four alternatives.** All five were
computed on the same committed census
(`bench/retrieval/results/leg-census-2026-08-10.json`), and the choice
is stated so it cannot be re-litigated after results:

| statistic | separation (right p50 / wrong p50) | best precision keeping ALL 14 correct legs |
| --- | --- | --- |
| `gap[0] / mean(other gaps)` | 4.04× | **0.583 (1.71×)** |
| `gap[0] / median(other gaps)` | 6.40× | 0.483 (1.41×) |
| `(s0−s1) / (s0−s_last)` | 2.92× | 0.560 (1.64×) |
| plateau fraction within 10% of top | 0.50× | 0.378 (1.11×) |
| round 3's `margin_ratio` (fixed level) | 4.0× | — kills 2 correct legs |

`gap/median` separates hardest and discriminates worst under the
preserve constraint, which is why the selection criterion is stated as
"precision achievable while keeping every correct leg" rather than
"separation": round 3 failed on the legs it dropped, not on the ones it
kept.

**Why this should transfer where θ could not.** `margin_ratio`
normalises by ONE number, the top score, so it inherits whatever the
corpus does to score magnitudes but nothing about the shape of the
competition. `standout` normalises by the leg's whole gap structure —
n−1 numbers drawn from the same store, same query, same scorer — so a
corpus whose legs are uniformly more or less compressed moves numerator
and denominator together. That is the argument; P22 is the test, and a
firing-rate gap as wide as round 3's falsifies it regardless of what
recall does.

**Measured consequence on the dev census, stated in advance:** K = 2.5
keeps **all 14** correct legs (round 3's θ dropped 2), drops 17 of 27
incorrect ones, fires on **41.5%** of engaged legs (round 3: 61%), and
lifts kept-leg precision from 0.341 to **0.583** (1.71×, against round
3's 2.20× — deliberately less aggressive).

**This REPLACES the fixed θ.** `_RESCUE_LEG_MIN_MARGIN` and its helper
are removed rather than stacked; round 3's results section retires the
fixed global threshold, and keeping both would leave two constants
where the campaign has argued for none.

### What "default-on" means for a self-calibrating mechanism

Stated before results because the phrase is now ambiguous. Flipping
`rescue_expansion` default-on would ship **the lane with the derivation
rule active and K at its committed value** — the rule is part of the
mechanism, not a tuning knob layered on top, and there is no
configuration in which the lane runs with the leg unconditioned. K is a
source constant with a stated derivation, like
`_RESCUE_COVERAGE_GATE` and `_RESCUE_LEG_WEIGHT`; it is not exposed as
a config key, and no store-specific value is written anywhere. "Carries
its own calibration" means the STATISTIC adapts to the store, not that
the constant is fitted per store — nothing is learned, persisted, or
derived at install time, which is what keeps the lane deterministic and
reviewable under the WaC rules.

### Instrument A — `bench/retrieval/` (DEVELOPMENT set)

```sh
.venv/bin/python bench/retrieval/run.py --rescue-expansion on --json
.venv/bin/python bench/retrieval/run.py --rescue-expansion on --pad-to 600 --prefilter both --json
.venv/bin/python bench/retrieval/run.py --rescue-expansion on --index-threshold 180 --prefilter both --json
```

Cap off (`--leg-margin-cap off`) must reproduce
`rebaseline-lane-*-2026-08-10.json` exactly.

### Instrument B — this directory (HELD-OUT set)

500 questions, sha256 `d6f21ea9…c3a442`, depth 200, lexical arm,
`--per-question` mandatory. **Five arms:**

1. **baseline, lane off** — must reproduce 0.5246 / 0.8935 / 0.9443.
   **If it does not, STOP.**
2. **lane on, no cap** — must reproduce 0.4772 / 0.8770 / 0.9471.
3. **lane on, standout cap at K** — the experiment.
4. **lane on, standout cap, `--ablate floor-off`** — the floor
   interaction, cleanly. Round 3 measured leg-only+cap (0.8870) above
   full-lane+cap (0.8830), but `leg-only` empties the filler table and
   so disables the floor AND the 5.1.1 emission filter at once. The new
   `floor-off` mode disables only the floor, which is the arm that can
   actually attribute that 0.0040.
5. **`--ablate leg-only` + standout cap** — kept for comparability with
   round 3's arm 4.

All artifacts carry `provenance.tree_dirty == false`; every arm is a
committed flag. K is frozen before Instrument B runs.

### Predictions

**P22 — the rule self-calibrates. THE test of the hypothesis, and it is
about firing rate, not recall.** The cap's firing rate on engaged
held-out questions lands within **±10 percentage points** of its
firing rate on engaged dev questions (41.5%), i.e. in **[31.5%,
51.5%]**. Round 3's fixed θ spanned 61% → 43.9%, a 17-point gap.
**MISSED if** outside that band — the statistic is no more
self-calibrating than the one it replaced, and any recall result below
is a coincidence of this corpus rather than a property of the rule.

**P23 — the dev-set win survives intact this time.** Unpadded lexical:
asked **exactly 50%/90%**, requery **exactly 80%/100%**, control
**exactly 45%/85%**. Not "within a question" — the rule keeps all 14
correct legs by construction, so anything less is the correctness proxy
failing again. **MISSED if** any cell moves.

**P24 — no held-out regression. THE kill criterion, at the line that
has stood three rounds.** macro@5 ≥ **0.8900**. Below that the default
does not flip.

**P25 — it beats round 3.** macro@5 > **0.8830** and macro@1 >
**0.4916**. Mechanism: the rule withholds fewer legs (41.5% vs 61% on
dev) while keeping every correct one, so it should lose less of the
lane's benefit while still removing the opinionless votes. **MISSED
if** either is at or below round 3 — a gentler rule that gains nothing
means the fixed θ's aggression was doing the work, and self-calibration
is not the lever.

**P26 — the filler floor is not paying for itself under a cap.** Arm 4
(floor off) ≥ arm 3 (floor on) at macro@5. Mechanism: round 3 measured
the floor costing 0.0040 in combination, and the floor's own ablation
reproduces baseline exactly, so it contributes nothing here while
interacting with the leg. **MISSED if** arm 4 < arm 3 — the round-3
reading was the `leg-only` confound rather than the floor, and the
floor should stay.

**P27 — reach is preserved.** macro@10 ≥ **0.9443** (baseline).

**P28 — the expected shape is a NULL, declared so it cannot be sold as
a win.** macro@5 in **[0.8900, 0.8990]** and macro@1 in **[0.5046,
0.5346]**. The cap is not predicted to help conversational stores; it
is predicted to stop hurting them.

### Kill criteria

1. **Baseline does not reproduce to four decimals** → stop.
2. **Uncapped arm does not reproduce 0.4772 / 0.8770 / 0.9471** → stop.
3. **macro@5 < 0.8900** → the default does not flip (P24).
4. **Any dev-set cell moves** → the rule is not free on technical
   stores (P23).
5. **Firing rate outside [31.5%, 51.5%]** → the rule does not
   self-calibrate; any recall result is corpus-specific (P22).
6. **macro@5 ≤ 0.8830** → no gain over round 3 (P25).
7. **`tree_dirty` true on any artifact** → run void.

### What would justify flipping `rescue_expansion` default-on

**All eight, conjunctively.**

1. macro@5 ≥ 0.8900 (P24).
2. macro@5 ≥ **0.8935**, the baseline itself — the held-out set pays
   nothing for the dev set's win.
3. macro@1 ≥ 0.5046 (P26's sibling, P28's band).
4. macro@10 ≥ 0.9443 (P27).
5. Dev-set cells all unmoved (P23).
6. Firing rate inside the P22 band on BOTH corpora — the mechanism has
   to be shown self-calibrating, not merely lucky.
7. Both determinism reproductions exact, `tree_dirty: false`, artifacts
   committed.
8. The flip lands as its own reviewed change citing this document.

**What does NOT justify a flip:** a larger dev-set win; a gain
concentrated in one question class; an improvement only at @10; or a
recall gain with a firing rate outside the P22 band, which would mean
the number came from this corpus rather than from the rule.

### Declared confounds

**1. K is still fitted to 41 dev observations**, 14 of them the
correct-leg population the preserve rule keys on. The rule choice is
better argued than round 3's; the sample is the same size.

**2. The correctness proxy is unchanged and was round 3's undoing.**
"Correct" still means the leg's rank-1 is the gold document, and a leg
whose rank-1 is wrong can still lift the gold document. Round 3's cost
came from exactly this. The mitigation is that K preserves ALL correct
legs rather than 12 of 14, which bounds the damage the proxy can do —
P23 is the guard, and it is stated as exact equality for that reason.

**3. The window is a third constant.** `_STANDOUT_WINDOW = 12` is
inherited from the census rather than derived, and a different window
would move K. It is fixed here so it cannot drift, but it is not
independently justified.

**4. Above the index threshold the leg ranks a nominated slice**, so
its gap structure is drawn from a pool already biased toward the
caller's vocabulary. Instrument A measures that regime; Instrument B
never has.

**5. Small stores make the rule inert.** Fewer than 3 candidates fails
open by construction, so on a small scope most legs vote unconditioned
— the same shape round 3's single-candidate case had.

**6. n=20 on the dev set.** One question is 5 points.

**7. Cost.** One pass over at most 12 already-computed scores. Runtime
guard: within **1.05×** the uncapped arm in the same session.

### What is not claimed

- **Not that the cap helps conversational stores.** P28 predicts a null.
- **Not that `standout` is optimal** — only that it is the best of the
  five the dev census measured under a stated selection criterion.
- **Not helpfulness, correctness, or staleness.**
- **Not a comparative claim.** No claude-mem arm runs in round 4.
- **Not the above-threshold regime on Instrument B.**

## Addendum 7 — round-5 experiment: the leg needs more than one word of evidence, 2026-08-10

Round 4 retired threshold FORM as the lever and named what replaced it
as the binding constraint: every rule so far was calibrated against a
proxy. This addendum removes the proxy. Predictions continue —
P1–P5, P6–P8, P9–P15, P16–P21, P22–P28 — so this owns **P29–P34**.

Two instruments, both bound. **This is the campaign's round-5
experiment and the last of the arc**; the result is a win or a kill
either way.

### What rounds 2–4 established

| round | mechanism | held-out @1 | held-out @5 | what it settled |
| --- | --- | --- | --- | --- |
| 2 | df-gate emitted terms | — | — | killed pre-run: the harmful terms are RARE, so vocabulary frequency does not separate |
| 3 | fixed margin threshold | 0.4916 | 0.8830 | first ground gained; a fixed level cannot transfer |
| 4 | self-calibrating standout | 0.4896 | 0.8790 | calibration solved (2.0-pt firing gap vs 17.1) and insufficient |
| — | lane, no cap | 0.4772 | 0.8770 | |
| — | baseline | 0.5246 | 0.8935 | |

Rounds 3 and 4 both cost the dev set (three questions at recall@5, then
two) while both caught the same harm. **The threshold was never the
problem; the label it was fitted to was.**

### The label, and what it revealed

`bench/leg_labels.py` runs the shipped ranker twice per dev question —
leg voting, leg withheld — and records where the gold document lands.
On 39 engaged dev legs (`results/leg-labels-2026-08-10.json` under
`bench/retrieval/`):

| verdict | n |
| --- | --- |
| helped (rescued into the top 5, or moved up) | **21** |
| hurt (broke out of the top 5, or moved down) | **3** |
| neutral | 15 |

**Only three legs of thirty-nine actually harm anything.** Round 3
withheld 25 engaged legs and round 4 withheld 17 — to catch those
three. Against the true labels they withheld **9** and **7 helpful
legs** respectively. That is the dev-set regression, exactly.

### Hypothesis

> **A leg whose top candidate matched only ONE synthesized term has a
> coincidence, not a paraphrase match. Two independent synthesized
> terms agreeing on the same document is evidence, and requiring it
> separates the harmful legs from the helpful ones without touching the
> helpful ones at all.**

### The rule

**The leg votes iff its rank-1 candidate matched at least
`_RESCUE_LEG_MIN_EVIDENCE` (2) synthesized terms.** Nothing else — no
score, no ratio, no distribution, no window.

```
if exp_leg and len(top_of(exp_leg).matched) < _RESCUE_LEG_MIN_EVIDENCE:
    exp_leg = []
```

**Why 2 and why it should transfer.** It is the minimum non-trivial
count: "more than one piece of evidence" is a bar stated independently
of any measurement, and the labels confirm it rather than select it.
This is the first rule in the arc that is **not a threshold on a
distribution** — it is a count of independent agreeing terms, so there
is no distribution to shift between corpora. Rounds 3 and 4 failed
because `margin_ratio` and `standout` are statistics whose spread
depends on the store; a count of matched terms has no such spread. The
transfer argument is therefore structural rather than empirical, which
is the strongest form available after two empirical ones failed.

**Measured consequence on the dev labels, stated in advance:** withholds
**3 of 3** harmful legs, **0 of 21** helpful legs, and 8 of 15 neutral
ones. Firing rate **31.7%** of engaged legs (round 3: 61%, round 4:
41.5%).

**This REPLACES the standout cap.** `_RESCUE_LEG_STANDOUT`,
`_STANDOUT_WINDOW` and `_leg_standout` are removed. Round 4 retired
threshold form; keeping a retired mechanism beside its replacement
would leave two constants where the campaign has argued down to one.

### Gate 0 — dev-side, and honestly derivable

**Gate 0a — perfect separation on true labels.** The rule must withhold
**100% of harmful** legs and **0% of helpful** ones on the committed
dev labels. Anything less and it is another proxy. *(Measured before
this commit: 3/3 and 0/21.)*

**Gate 0b — non-trivial.** It must withhold at least one leg and fewer
than half of the engaged population, so it is neither a no-op nor an
off-switch. *(Measured: 13 of 41, 31.7%.)*

Both pass. As in round 4, this is a dev-side gate and therefore weak
evidence about transfer; the held-out arm does the work. **No held-out
statistic is read before the run.**

### Instrument A — `bench/retrieval/` (DEVELOPMENT set)

```sh
.venv/bin/python bench/retrieval/run.py --rescue-expansion on --json
.venv/bin/python bench/retrieval/run.py --rescue-expansion on --pad-to 600 --prefilter both --json
.venv/bin/python bench/retrieval/run.py --rescue-expansion on --index-threshold 180 --prefilter both --json
```

### Instrument B — this directory (HELD-OUT set)

500 questions, sha256 `d6f21ea9…c3a442`, depth 200, lexical arm,
`--per-question` mandatory. **Four arms** — round 4 settled the floor
question (the clean `floor-off` arm reproduced floor-on exactly), so
the floor ablation is retired and not repeated:

1. **baseline, lane off** — must reproduce 0.5246 / 0.8935 / 0.9443.
   **If it does not, STOP.**
2. **lane on, no cap** — must reproduce 0.4772 / 0.8770 / 0.9471.
3. **lane on, evidence rule** — the experiment.
4. **`--ablate leg-only` + evidence rule** — the leg in isolation,
   kept because it is the arm that carried every regression.

**Out of scope by standing guard:** round 4's finding that `leg-only`
beats table-intact by 0.0040 — filler back in the emitted terms helping
this corpus — is recorded and NOT acted on. The 5.1.1 emission filter
is untouched this round and is a candidate for its own preregistration
only.

### Predictions

**P29 — the dev set is preserved EXACTLY. The explicit target.**
Unpadded lexical: asked **50%/90%**, requery **80%/100%**, control
**45%/85%** — every cell identical to the uncapped lane. Rounds 3 and
4 lost three and two questions here; the rule withholds zero helpful
legs by construction, so anything less means the true labels did not
generalize even within the dev set. **MISSED if** any cell moves.

**P30 — no held-out regression. THE kill criterion, at the line that
has stood four rounds.** macro@5 ≥ **0.8900**.

**P31 — it beats every prior round.** macro@5 > **0.8830** (round 3's
best) and macro@1 > **0.4916**. **MISSED if** either is at or below —
removing the proxy bought nothing, and the lane's harm is not
concentrated in the legs the labels say it is.

**P32 — recall@1 recovers materially.** macro@1 ≥ **0.5046**, within
two points of baseline.

**P33 — reach is preserved.** macro@10 ≥ **0.9443**.

**P34 — the firing rate is LOW and comparable across corpora.** The
rule fires on ≤ **45%** of engaged held-out questions, and within **±15
points** of the dev rate (31.7%), i.e. in **[16.7%, 45%]**. A count has
no distribution to shift, so a wide gap here would falsify the
structural transfer argument even if recall improves.

### Kill criteria

1. Baseline does not reproduce to four decimals → stop.
2. Uncapped arm does not reproduce 0.4772 / 0.8770 / 0.9471 → stop.
3. macro@5 < 0.8900 → the default does not flip (P30).
4. Any dev-set cell moves → the rule is not free on technical stores.
5. macro@5 ≤ 0.8830 → no gain over round 3 (P31).
6. Firing rate outside [16.7%, 45%] → the structural transfer argument
   is false (P34).
7. `tree_dirty` true on any artifact → run void.

### What would justify flipping `rescue_expansion` default-on

**All eight, conjunctively.** 1. macro@5 ≥ 0.8900. 2. macro@5 ≥
**0.8935**, the baseline itself. 3. macro@1 ≥ 0.5046. 4. macro@10 ≥
0.9443. 5. Every dev cell unmoved. 6. Firing rate inside the P34 band
on both corpora. 7. Both determinism reproductions exact,
`tree_dirty: false`, artifacts committed. 8. The flip lands as its own
reviewed change citing this document.

**What does NOT justify a flip:** a larger dev-set win; a gain
concentrated in one question class; an improvement only at @10.

### Declared confounds

**1. Three harmful legs.** The rule is confirmed against a harmful
population of THREE. That is the weakest evidence base in the arc, and
it is why the constant is argued structurally (the minimum non-trivial
count) rather than fitted — there is no threshold to tune here, only a
bar to state. A different corpus could have harmful legs at
`top_matched` 2, and nothing in this document would have seen it.

**2. The label is dev-side and gold-anchored.** It needs a gold
document, so it cannot be computed on the held-out corpus at all —
which is what keeps that corpus clean, and also what means the rule has
never been checked against held-out harm.

**3. `top_matched` counts terms, not their quality.** Two matched
synonyms from the same table row are less independent than two from
different sources, and the rule cannot tell them apart.

**4. Above the index threshold the leg ranks a nominated slice**, so
its rank-1 may match fewer terms for pool reasons rather than evidence
reasons. Instrument A measures that regime; Instrument B never has.

**5. Small stores.** A leg with one candidate can still match one term
and would be withheld, where rounds 3 and 4 failed open. This rule is
STRICTER on tiny legs than its predecessors, which is a deliberate
consequence of counting evidence rather than measuring spread.

**6. n=20 on the dev set.** One question is 5 points.

**7. Cost.** One `len()` on a list the engine already built.

### What is not claimed

- **Not that the rule helps conversational stores** — only that it
  should stop hurting them while leaving technical stores alone.
- **Not that two terms is optimal** — only that it is the minimum
  non-trivial count and that the dev labels separate perfectly on it.
- **Not helpfulness, correctness, or staleness.**
- **Not a comparative claim.** No claude-mem arm runs.
- **Not the above-threshold regime on Instrument B.**

---

## The retrieval campaign's round 2-5 arc, closed 2026-08-10

Five preregistered experiments, one index so the record reads as one
argument rather than five documents.

| addendum | round | mechanism | verdict | held-out @1 / @5 |
| --- | --- | --- | --- | --- |
| 3 | 1 | the lane itself (filler floor + gated leg) | KILL — default did not ship | 0.4752 / 0.8770 |
| 4 | 2 | df-gate the emitted terms | **KILL before running** — Gate 0a 0.74x against a 5x bar | — |
| 5 | 3 | fixed margin threshold | KILL — first ground gained, a fixed level cannot transfer | 0.4916 / 0.8830 |
| 6 | 4 | self-calibrating standout | KILL — calibration solved (2.0-pt gap vs 17.1), insufficient | 0.4896 / 0.8790 |
| 7 | 5 | evidence count (two agreeing terms) | KILL — dev set preserved, best @1/@10, line still missed | 0.5014 / 0.8823 |

Reference rows: baseline (lane off) 0.5246 / 0.8935; lane on, no cap
0.4772 / 0.8770.

**What each round established, in one line:**

- **Round 2** — the harmful expansion terms are individually RARE, so
  vocabulary frequency does not separate the class that helps a
  technical store from the class that harms a conversational one. Two
  populations, same df band. Killed offline for the price of a census.
- **Round 3** — conditioning the leg's VOTE is the right target; a
  fixed level is the wrong instrument, because its distribution differs
  by corpus.
- **Round 4** — a self-calibrating statistic really does transfer (a
  2.0-point firing-rate gap where the fixed level spanned 17.1), and
  transfer alone buys nothing: firing at the right RATE is not firing
  on the right LEGS.
- **Round 5** — the binding constraint was never the threshold but the
  LABEL every threshold was fitted to. True labels preserve the dev set
  completely and produce the arc's best @1 and @10. They do not reach
  the line, and the "a count has no distribution" argument was wrong
  (15.4-point firing gap).

**The arc's finding: a ceiling, not a tuning problem.** Three
structurally different withholding rules — a level, a shape, a count —
land within 0.004 of each other at held-out macro@5 (0.8790, 0.8823,
0.8830), recovering a third to a half of the lane's damage and none of
it reaching baseline. Conditioning which legs vote cannot repair a lane
whose remaining harm is in what the legs contain.

**Consequence for the next preregistration.** Addendum 4's confound 1
still binds: LongMemEval has now informed parameters across four
rounds, and a genuinely clean held-out check needs a third instrument.
Any successor experiment that adapts expansion vocabulary to the store
(rather than adapting which legs vote) should say so in its first
paragraph and budget for that instrument.

**That instrument was searched for on 2026-08-11 and not found.**
Every conversational long-term-memory corpus surveyed is either
non-commercial (LoCoMo, DailyDialog, EmpatheticDialogues), carries no
identifiable license (PerLTQA, Cornell Movie-Dialogs), or ships its
data separately from its permissively-licensed code with no
redistribution grant (MSC). Synthesising one is licence-clean and
methodologically disqualified — a held-out set whose paraphrase gaps
its own author chose cannot check a paraphrase mechanism that author
designed. The candidates, the evidence and the three ways forward are
in [`../THIRD_INSTRUMENT.md`](../THIRD_INSTRUMENT.md). **P1a is blocked
on it and is deliberately not preregistered.**

---

## Addendum 8 — P1a: store-derived PPMI expansion, 2026-08-11

The campaign's first mechanism that changes what the legs CONTAIN
rather than which of them vote. Predictions continue — P1–P5, P6–P8,
P9–P15, P16–P21, P22–P28, P29–P34 — so this document owns **P35–P41**.

**Three instruments, for the first time.** `bench/retrieval` (dev),
this directory (**dev-contaminated**, and labelled so: it has informed
parameters across four rounds and is no longer a clean check), and
`bench/heldout` — blind-authored, sealed, and scored exactly once.

### Ordering and the no-read attestation

- Instrument data landed at **`35227dd`**, authored independently.
- **Attestation: at the time of writing, no one implementing this
  mechanism has read `bench/heldout/data/questions.json`,
  `personas.json`, or any gold label.** The instrument has been
  exercised only through `run.py --validate`, which prints counts and
  no content, and through `harness.load` in a test that asserts counts
  and the seal flag.
- The enforcement record is the sha ordering: **data `35227dd` <
  preregistration (this commit) < run commit.**
- The dev census (`bench/retrieval/results/ppmi-census-2026-08-11.json`)
  predates this document, exactly as addendum 4's did. Gate 0's bar
  below is structural rather than fitted, and the disclosure is here
  rather than implied.

### What rounds 2–5 bind

**C1 — the vocabulary is the problem.** Identical code flips sign
between corpora, so a static table cannot be right for both. That is
the argument FOR P1a.

**C5 (new, from round 5) — the fusion cannot use an imprecise leg.**
`_hybrid_fuse` fuses by RANK, so a leg contributes
`_RESCUE_LEG_WEIGHT / (rrf_k + rank)` regardless of how good its
evidence is. Three rounds of conditioning which legs vote recovered at
most half the damage and plateaued. **Any new SOURCE therefore has to
be at least as precise as the one it replaces, because the architecture
offers no way to discount a bad leg's vote.**

**Round 2 is not to be relitigated.** df was measured and killed as a
separator for emitted terms; no df gate on emitted terms appears here.

### The mechanism, had it been implemented

Per-query, derived from the collection being ranked (which is what
makes it a function of the store, per C1): for each query token, count
the terms co-occurring with it across the candidate documents, score by
shifted positive pointwise mutual information over document
frequencies, clamp, and keep the top-k. Emission-side discipline **by
construction**: query tokens excluded, **filler stems excluded** (the
5.1.1 invariant applies to a derived source exactly as to the tables),
length floor applied. Voting form: **the round-5 evidence rule**, the
leg voting only when its rank-1 matches at least two emitted terms.

### Gate 0 — precision parity, and why it is the right bar

**Gate 0 — a replacement source must be at least as precise as the
incumbent.** Measured identically on the same dev probes: the fraction
of emitted terms that appear in the gold document. The committed static
tables are the incumbent. **Requirement: the best grid cell reaches
≥ 1.0× the static tables' precision.**

The bar is structural, not tuned. The incumbent's default was **killed**
on the held-out set for being too imprecise; C5 says the architecture
cannot discount an imprecise leg; so replacing that source with a *less*
precise one cannot help, whatever its recall. There is no threshold to
choose here — 1.0× is the only defensible number, and it was fixed
before the census was read.

**Failing it publishes "store-derived co-occurrence does not reach
usable precision at this scale" and ENDS the experiment: no engine
code, no arms, no instrument spent.** The sealed instrument is
explicitly NOT run on a failed gate — spending a single-use held-out
check on a mechanism already known to be worse would waste the thing
the arc spent a burst acquiring.

### Instruments and arms, had Gate 0 passed

1. `bench/retrieval` — as-asked, control, requery; unpadded and both
   prefilter regimes.
2. This directory — four arms as in rounds 3–5, labelled
   **dev-contaminated** in every published row.
3. `bench/heldout` — **once, last**, after the other two are scored.

### Predictions

**P35 — Gate 0 passes.** The best grid cell reaches ≥ 1.0× the static
tables' precision. **MISSED if** below — publish the negative, run
nothing.

**P36** — dev-set recall@5 ≥ the current lane's 90%/85%/100%.
**P37** — LongMemEval macro@5 ≥ 0.8900 (the line, five rounds standing).
**P38** — LongMemEval macro@1 ≥ 0.5046.
**P39** — held-out macro@5 within 0.01 of its lane-off baseline.
**P40** — emitted terms per probe stay within 2× the static tables'.
**P41** — the leg's firing rate stays inside round 5's measured band.

### Kill criteria

1. **Gate 0 fails** → dead on arrival; no implementation, no arms, and
   the sealed instrument is NOT spent.
2. Dev-set recall@5 falls → not free on technical stores.
3. LongMemEval macro@5 < 0.8900 → the default does not flip.
4. Held-out macro@5 below its lane-off baseline → the mechanism harms a
   corpus that has never informed a parameter.
5. `tree_dirty` true on any artifact → run void.

### What would justify flipping `rescue_expansion` default-on

All of: Gate 0 passed; dev cells unmoved or better; LongMemEval macro@5
≥ 0.8935 (baseline, not merely the kill line); **held-out macro@5 ≥ its
lane-off baseline** — the clean instrument is the one that matters and
it gets a veto; provenance clean; and the flip lands as its own
reviewed change citing this document.

### Declared confounds

**1. The precision proxy.** "Appears in the gold document" is a proxy
for "helps". A term absent from the gold is not necessarily harmful —
but the arc has established that unhelpful emitted terms are exactly
what the held-out set charges for, and the comparison against the
incumbent is measured identically on both sides.

**2. Scale.** The dev corpus is 180 documents. Co-occurrence statistics
are thin there, and PPMI is known to be noisy on small collections —
`min_df` exists for that. A much larger store might support a cleaner
table. **This experiment therefore speaks to stores of roughly this
size, which is the size a personal memory store actually is.**

**3. Per-query derivation, not an index-time table.** The campaign plan
describes an index-time table; deriving per query from the candidate
pool is the same statistic without a schema change, and is what the
census measured.

### What is not claimed

- Not that co-occurrence carries no signal. It demonstrably finds gold
  terms the static tables miss.
- Not that a larger store would behave this way.
- Not a comparative claim, and no claude-mem arm runs.

---

## Addendum 9 — round 6: the leg's contribution scales with its evidence, 2026-08-11

The window's final experiment, and the one every prior kill pointed at.
Predictions continue — so this document owns **P42–P48**.

### Why this, and why now

Five preregistered experiments have failed, and they failed in a
pattern. Rounds 3, 4 and 5 each changed WHICH legs vote and landed
within 0.004 of each other at held-out macro@5 (0.8790 / 0.8823 /
0.8830, baseline 0.8935). Addendum 8's P1a changed WHAT the legs
contain and could not reach even the incumbent's precision (0.46×).

Every one of those write-ups names the same cause without testing it:

> `_hybrid_fuse` fuses by RANK, so a leg contributes
> `_RESCUE_LEG_WEIGHT / (rrf_k + rank)` **whether its rank-1 was found
> by a discriminating synonym or by a single coincidental token**. IDF
> only reorders WITHIN the leg; it cannot reduce the leg's influence.

Choosing better voters plateaued. Choosing better words was worse than
the incumbent. **The remaining variable is the vote itself**, and it has
been constant at 0.7 through every round.

### Scope constraint, stated as a binding limit

This changes **only** how the rescue-expansion leg's contribution
enters fusion, and **only** when the lane is on. Two things follow and
both are preregistered rather than assumed:

- **The default engine's fusion path must be byte-identical.** P42
  asserts it as an arm, not an assumption.
- **Generalising evidence-scaled fusion to the base legs is OUT OF
  SCOPE for this window regardless of results.** If the dev evidence
  suggests it generalises, that is recorded below as a named future
  hypothesis and left alone.

### Hypothesis

> **A leg's contribution should scale with how much evidence stands
> behind its top candidate. Legs at the evidence floor are helpful more
> often than not but not reliably; legs above it are reliably helpful;
> and a fusion that gives both the same vote is discarding the
> distinction it already has in hand.**

### The dev evidence, and the curve derived from it

From the committed round-5 labels
(`bench/retrieval/results/leg-labels-2026-08-10.json`, 39 engaged legs
labelled by whether the leg's vote actually moved the gold document):

| evidence (matched terms at the leg's rank-1) | n | helped | neutral | hurt | % helped |
| --- | --- | --- | --- | --- | --- |
| 1 | 11 | 0 | 8 | 3 | **0%** |
| 2 | 22 | 15 | 7 | 0 | **68.2%** |
| ≥ 3 | 6 | 6 | 0 | 0 | **100%** |

Monotone, with no inversion. The floor stratum is already excluded by
the round-5 rule and stays excluded. What is new is the middle: a leg at
exactly the floor is helpful about two times in three, and a leg above
it was helpful every time.

**The curve, and the rule that produced it.** The leg earns its FULL
weight at the evidence count where the dev labels first read 100%
helpful, and scales linearly from the floor to there:

```
scale(m) = 0.0                              if m < _RESCUE_LEG_MIN_EVIDENCE
         = min(1.0, (m - 1) / (_EVIDENCE_FULL_AT - 1))   otherwise
weight   = _RESCUE_LEG_WEIGHT * scale(m)
```

with `_EVIDENCE_FULL_AT = 3`, giving:

| m | scale | leg weight |
| --- | --- | --- |
| < 2 | 0.00 | 0.000 (withheld — round-5 rule, unchanged) |
| 2 | 0.50 | 0.350 |
| ≥ 3 | 1.00 | 0.700 (the current constant, unchanged) |

**Exactly one new constant**, and it is read off the labels by a stated
rule rather than tuned: 3 is where the dev labels reach 100%. The scale
is bounded in [0, 1] by construction, so the leg can never contribute
MORE than today — this experiment can only ever reduce the leg's
influence, never amplify it. That bound is deliberate: an amplifying
change would need a different safety argument than a damping one.

**Ordering disclosure.** The leg-labels artifact predates this
document, exactly as addenda 4 and 8 disclosed for their censuses.
Gate 0's bar below is structural and was fixed before the strata were
tabulated; the reader can check that a 90% bar is nowhere near the
0%/68.2%/100% the table reports, which is not what a fitted bar looks
like.

### Gate 0 — is there anything for a curve to express?

**Gate 0 — the evidence level must stratify the labels.** The top
stratum must be **≥ 90% helpful** and the floor stratum **materially
lower**, or a flat curve is the null hypothesis and there is nothing to
scale. *(Measured: 100% and 68.2%.)* **Passes.**

**As in addendum 8, a failed gate would spend nothing** — no fusion
code, no arms, and above all no held-out run.

### Arms

**Instrument A — `bench/retrieval` (dev).** Unpadded, padded-600 and
both prefilter regimes, lane on.

**Instrument B — this directory (dev-contaminated, labelled so in every
row).** baseline lane-off; lane-on with the current flat weight; lane-on
with the evidence-scaled weight.

**Instrument C — `bench/heldout` (blind, sealed).** Scored **once,
last, and only if the dev gates below pass** — the same protection that
saved it from P1a. **No-read attestation: no gold label or question
text from that instrument has been read by the implementer at the time
of writing; the sha ordering is data `35227dd` < this commit < any run
commit.**

### Predictions

**P42 — the lane-off path is byte-identical. An arm, not an
assumption.** With `rescue_expansion=False`, hit ids AND scores are
identical to the pre-change engine across the dev corpus and a
LongMemEval baseline arm reproducing 0.5246 / 0.8935 / 0.9443 to four
decimals. **MISSED if** anything moves — the scope constraint is
violated and the change is reverted regardless of every other result.

**P43 — the dev set does not regress.** Unpadded lexical: asked ≥
50%/90%, requery exactly 80%/100%, control ≥ 45%/85%. Damping a leg
that is already helping is the obvious way this hurts.

**P44 — LongMemEval improves on round 5.** macro@5 > **0.8830** (the
arc's best) and macro@1 > **0.5014**.

**P45 — THE kill criterion, at the line that has stood five rounds.**
macro@5 ≥ **0.8900**.

**P46 — reach is preserved.** macro@10 ≥ **0.9443**.

**P47 — the damping is real and bounded.** The fraction of engaged legs
receiving the reduced weight is > 0 and < 100% on both dev instruments
— a curve that never fires, or always fires, is a constant by another
name.

**P48 — held-out, if it runs.** macro@5 ≥ its own lane-off baseline on
`bench/heldout`. The clean instrument gets a veto: a mechanism that
helps two dev-informed corpora and harms the blind one is overfitting
with extra steps.

### Kill criteria

1. **P42 fails** → scope violated; revert, publish, stop.
2. Dev-set recall@5 falls → not free on technical stores.
3. macro@5 < 0.8900 → the default does not flip.
4. macro@5 ≤ 0.8830 → no gain over round 5; the fusion hypothesis is
   measured and dead like the other two.
5. Held-out macro@5 below its lane-off baseline → the blind instrument
   vetoes.
6. `tree_dirty` true on any artifact → run void.

### What flips `rescue_expansion` default-on

Stated exactly, because this is the first mechanism with a plausible
route to it. **All seven, conjunctively:**

1. **P42 holds** — lane-off byte-identical.
2. LongMemEval macro@5 ≥ **0.8935** — the baseline itself, not merely
   the 0.8900 kill line. The lane must cost this corpus nothing.
3. LongMemEval macro@1 ≥ **0.5246**, the baseline.
4. LongMemEval macro@10 ≥ 0.9443.
5. Dev set at or above its current lane-on figures (asked 50%/90%,
   requery 80%/100%, control 45%/85%).
6. **`bench/heldout` macro@5 ≥ its lane-off baseline** — the blind
   instrument confirms rather than merely fails to contradict.
7. Provenance clean everywhere, and the flip lands as its own reviewed
   change citing this document.

Anything short and the lane stays opt-in. In particular a LongMemEval
result between 0.8900 and 0.8935 clears the kill line **and does not
flip the default** — it means the lane still costs the corpus
something.

### Named future hypothesis, deliberately not tested here

**H-fusion-general: the base legs may have the same problem.** Keyword
and BM25 also contribute by rank, so a base leg with a weak top hit
votes as hard as one with a strong one. If round 6 works, that is the
obvious next question — and it is out of scope for this window by the
constraint above, because changing base-leg fusion changes the default
engine's ranking for every user, which needs its own preregistration
and its own held-out budget. Recorded so it is not rediscovered as a
surprise.

### Declared confounds

**1. Six legs above the floor.** The 100%-helpful stratum is n=6. The
curve's upper anchor rests on that, which is why the rule reads the
count where the labels *first* reach 100% rather than fitting a slope.

**2. The proxy is one step removed.** "Helped" means the leg moved the
gold document, measured on the dev corpus only. The held-out set has no
such labels and never will — it can only be scored, not diagnosed.

**3. Damping cannot rescue a leg that is simply wrong.** The mechanism
reduces a weak leg's influence; it does not improve the leg. If the
held-out harm comes from legs with high evidence counts that are
nonetheless wrong, this changes nothing, and P44 is where that shows.

**4. Instrument B is dev-contaminated** and labelled so in every row.

### What is not claimed

- Not that the base legs should change — explicitly out of scope.
- Not that damping helps conversational stores; P44/P45 are the tests.
- Not helpfulness, correctness or staleness.
- Not a comparative claim; no claude-mem arm runs.

---

## Addendum 10 — round 7: the same mechanism, a structural curve, 2026-08-11

Round 6 confirmed the fusion hypothesis and failed its dev gate. This
recalibrates the curve and nothing else. Predictions continue — this
document owns **P49–P55**.

### What round 6 settled, and what it broke

| arm | macro@1 | macro@5 | macro@10 |
| --- | --- | --- | --- |
| baseline, lane off | 0.5246 | 0.8935 | 0.9443 |
| lane on, flat weight (round 5) | 0.5014 | 0.8823 | 0.9476 |
| lane on, scaled `(m-1)/(F-1)` (round 6) | 0.5134 | **0.8926** | 0.9463 |

Round 6 cleared the 0.8900 kill line for the first time in six rounds
and landed 0.0009 under baseline. It also cost the dev set two
questions at recall@5 (asked 90%→80%, control 85%→80%), which is a
preregistered kill, so the default did not flip and the blind
instrument was not spent.

The diagnosis is specific: the damping fires on ~79% of voting legs,
because the two-matched-terms stratum is most of the population, and
the dev labels put that stratum at **68.2% helpful**. Round 6 halved
its vote. **The mechanism is right; the weight at the floor is too
aggressive.**

### The curve, and why this is a structural change rather than a fit

Round 6 used `scale(m) = (m - 1) / (F - 1)`. That form has an offset
whose only justification was that it maps the floor to exactly 0.5 —
which was a choice, not a derivation, and it is the choice that cost
the dev set.

**Round 7 uses the quantity the mechanism actually names:**

```
scale(m) = 0.0                       if m < _RESCUE_LEG_MIN_EVIDENCE
         = min(1.0, m / _EVIDENCE_FULL_AT)   otherwise
weight   = _RESCUE_LEG_WEIGHT * scale(m)
```

A leg's weight is **the fraction of the full-evidence bar its own
evidence reaches**. There is no offset to justify, and:

**Round 7 introduces ZERO new constants.** `_EVIDENCE_FULL_AT` stays 3,
derived in addendum 9 by a stated rule (the count at which the dev
labels first read 100% helpful). `_RESCUE_LEG_MIN_EVIDENCE` stays 2 and
`_RESCUE_LEG_WEIGHT` stays 0.7. Only the FORM changes, from a rescaled
offset to the plain ratio.

| m | round 6 weight | **round 7 weight** |
| --- | --- | --- |
| < 2 | 0.000 (withheld) | 0.000 (withheld) |
| 2 | 0.350 | **0.467** |
| ≥ 3 | 0.700 | 0.700 |

**The convergence, stated as evidence rather than as a fit.** The
structural form puts the floor stratum at **2/3 = 0.6667** of full
weight. The round-5 labels independently measure that stratum at
**68.2% helpful**. The gap is **1.5 points**, and the two numbers come
from different places: one from the mechanism's own arithmetic
(evidence over the full-evidence bar), one from counting outcomes on
39 labelled legs. Neither was derived from the other. **That agreement
is corroboration; had the structural form been fitted to 0.682 it would
read 0.682, and it does not.**

Three fitted constants have died in this arc (a df threshold, a fixed
margin, a self-calibrating ratio). This one is not fitted at all.

### Contamination statement, required and explicit

**Curve selection used no LongMemEval outcome from round 6.** The form
comes from the mechanism's own quantities; the anchor `F = 3` was fixed
in addendum 9 before round 6 ran; the corroborating 68.2% is from the
round-5 label artifact, which predates round 6 entirely. **The 0.8926
figure played no part in choosing this curve**, and it must not be
tuned against in any successor either — it is dev-contaminated for
curve selection from here on.

### The blind instrument

`bench/heldout` is scored **once, last, and if and only if the dev
gates below pass.** That protection has now saved it twice — from P1a
and from round 6.

**No-read attestation:** no gold label or question text from that
instrument has been read by the implementer at the time of writing.
The enforcement record is the sha ordering: data `35227dd` < this
commit < any run commit.

### Predictions

**P49 — the lane-off path is byte-identical. An arm, not an
assumption.** Dev cells identical with `rescue_expansion=False`, and a
LongMemEval baseline arm reproducing 0.5246 / 0.8935 / 0.9443 to four
decimals. **MISSED if** anything moves — scope violated, revert.

**P50 — the dev set is preserved at its current lane-on figures. The
gate round 6 failed, stated exactly.** Unpadded lexical:

- asked **recall@1 ≥ 55%** and **recall@5 ≥ 90%**
- requery **exactly 80% / 100%**
- control **recall@1 ≥ 50%** and **recall@5 ≥ 85%**

**MISSED if** any of those falls. A gentler curve that still costs the
gold set is the same failure round 6 had.

**P51 — LongMemEval macro@5 ≥ 0.8900**, the kill line, six rounds
standing.

**P52 — macro@5 ≥ 0.8935**, the baseline itself. This is the bar that
matters for a default flip: the lane must cost the corpus nothing, not
merely stay above the kill line.

**P53 — macro@1 ≥ 0.5134**, round 6's figure, and the flip case
additionally needs ≥ 0.5246 (baseline).

**P54 — macro@10 ≥ 0.9443.**

**P55 — the held-out instrument confirms, if it runs.** `bench/heldout`
macro@5 ≥ its own lane-off baseline. The clean instrument holds a veto:
a mechanism that helps two dev-informed corpora and harms the blind one
is overfitting with extra steps.

### Kill criteria

1. P49 fails → scope violated; revert, publish, stop.
2. Any P50 figure falls → not free on technical stores; **the blind
   instrument is NOT spent.**
3. macro@5 < 0.8900 → the default does not flip.
4. Held-out macro@5 below its lane-off baseline → the blind instrument
   vetoes.
5. `tree_dirty` true on any artifact → run void.

### The preregistered case for `rescue_expansion` default-on

**If and only if ALL SEVEN hold, the case is MADE:**

1. **P49 holds** — lane-off byte-identical on both instruments.
2. **Dev set at or above its current lane-on figures** — asked ≥
   55%/90%, requery exactly 80%/100%, control ≥ 50%/85% (P50).
3. **LongMemEval macro@5 ≥ 0.8935** — the baseline, not the kill line
   (P52).
4. **LongMemEval macro@1 ≥ 0.5246** — the baseline (P53).
5. **LongMemEval macro@10 ≥ 0.9443** (P54).
6. **`bench/heldout` macro@5 ≥ its lane-off baseline** (P55) — the
   blind instrument confirms rather than merely failing to contradict.
7. **Provenance clean everywhere**: `tree_dirty: false` on every
   artifact, both determinism arms exact, all artifacts committed.

**Protocol, explicit: even with all seven, the implementer does NOT
flip the default.** The finding is reported as *the preregistered case
is MADE*, quoting this list with each item's measured value, and the
owner executes the flip and the release as its own reviewed change
citing this document. Anything short of all seven and the case is NOT
made and the lane stays opt-in — including the specific case where
macro@5 lands between 0.8900 and 0.8935, which clears the kill line and
still means the lane costs the corpus something.

### Declared confounds

**1. `F = 3` rests on six legs.** Unchanged from addendum 9 and still
the weakest anchor in the design.

**2. The convergence could be coincidence.** Two numbers agreeing to
1.5 points on one dev corpus is corroboration, not proof. If round 7
succeeds, the honest follow-up is whether `m/F` holds its shape on a
different corpus — and the blind instrument can only answer that once.

**3. Damping cannot fix a leg that is simply wrong**, unchanged from
addendum 9.

**4. Instrument B is dev-contaminated** and labelled so in every row.

### What is not claimed

- Not that the base legs should change — `H-fusion-general` remains
  out of scope for this window, as recorded in addendum 9.
- Not that `m/F` is optimal; only that it is structural, introduces no
  constant, and corroborates independently.
- Not a comparative claim; no claude-mem arm runs.

---

## Addendum 11 — round 8: a store-adaptive floor weight, 2026-08-11

The endgame lever. Round 7 reduced the campaign's obstacle to one
scalar; this asks whether that scalar can be derived from the store.
Predictions continue — this document owns **P56–P62**.

### What round 7 established

Sweeping the floor stratum's weight, the only quantity differing
between rounds 5, 6 and 7:

| weight at the floor stratum | dev asked recall@5 | LongMemEval macro@5 |
| --- | --- | --- |
| 0.00 | 0.65 | — |
| 0.35 | 0.80 | 0.8926 |
| 0.467 | 0.80 | 0.8901 |
| 0.60 | 0.85 | — |
| 0.70 | 0.90 | 0.8823 |

**Monotone, in opposite directions, roughly twofold apart in the
optimum.** The technical corpus wants full weight; the conversational
one wants it damped. No constant satisfies both.

### Hypothesis

> **The right floor weight is a property of the store, and the store
> says which it is.** A statistic the engine can compute from the
> store's own text should place a technical corpus near full weight and
> a conversational one lower, letting one rule serve both.

### Contamination line, binding

**0.8926 and 0.8901 are dev-contaminated for weight selection.** The
adaptation rule may not be chosen or tuned to hit them, and — equally —
its clamp bounds may not be chosen *because* a contaminated number was
best there. Bounds must be structural, in the sense round 6's
damping-only [0, `_RESCUE_LEG_WEIGHT`] range was structural. Derivation
is restricted to mechanism quantities and the round-5 dev labels.

### Gate 0 — can any store statistic separate the corpora at all?

Round 7 measured the weight needing to move about **twofold** between
the corpora. A rule keyed on a statistic has to amplify that
statistic's spread into the weight's spread, so:

**Gate 0 — separation.** At least one cheap, store-computable statistic
must separate the two corpora by **≥ 2×** (or ≤ 0.5×). Below that, any
rule steep enough to move the weight twofold is a high-gain amplifier
on a near-constant input — unstable under ordinary corpus variation,
and a fit rather than a derivation.

**Gate 0a** — the rule computed on the dev store lands at or near the
dev optimum (full or near-full weight).
**Gate 0b** — the rule computed on the LongMemEval store moves in the
damped **direction**. Sign only; its magnitude target is contaminated.

The bar is fixed before the census is read, and it is not a free
parameter: it is round 7's own measured requirement. **Failing Gate 0
ends round 8 before any engine code, and does NOT spend the sealed
instrument** — the protection that has now held through P1a, round 6
and round 7.

### The candidate statistics

All computable from the store's text alone, deterministic, no schema
change: document count, mean document length, type–token ratio, hapax
share, **filler-token share** (the quantity C1's own story names — "memory
bodies are technical prose, so conversational filler is corpus-RARE"),
and stopword share.

### Predictions

**P56 — Gate 0 passes.** Some statistic separates the corpora by ≥ 2×.
**MISSED if** none does — publish the negative, write no rule, spend
nothing.

**P57** — lane-off byte-identical (the arm, again).
**P58** — dev preserved: asked ≥ 55%/90%, requery exactly 80%/100%,
control ≥ 50%/85%.
**P59** — LongMemEval macro@5 ≥ 0.8935 (baseline).
**P60** — macro@1 ≥ 0.5246 (baseline).
**P61** — macro@10 ≥ 0.9443.
**P62** — `bench/heldout` macro@5 ≥ its lane-off baseline, if it runs.

### Kill criteria

1. **Gate 0 fails** → dead on arrival; no rule, no arms, instrument NOT
   spent.
2. P57 fails → scope violated; revert.
3. Any P58 figure falls → not free on technical stores; instrument NOT
   spent.
4. macro@5 < 0.8900 → the default does not flip.
5. Held-out below its lane-off baseline → the blind instrument vetoes.
6. `tree_dirty` true on any artifact → run void.

### The preregistered case for `rescue_expansion` default-on

Carried forward verbatim from addendum 10. **All seven, conjunctively:**
lane-off byte-identical; dev at or above its current lane-on figures;
LongMemEval macro@5 ≥ 0.8935; macro@1 ≥ 0.5246; macro@10 ≥ 0.9443;
`bench/heldout` macro@5 ≥ its lane-off baseline; provenance clean
everywhere. **Even with all seven the implementer does not flip** — the
case is reported as MADE and the owner executes it.

### Declared confounds

**1. "Cheap" excludes semantics.** These statistics are surface
counts. If what distinguishes the corpora is meaning rather than
distribution, no statistic here will see it — and that would itself be
the finding.

**2. Two corpora is a small basis** for claiming a statistic does or
does not separate register in general.

**3. Instrument B is dev-contaminated** and labelled so in every row.

---

## Addendum 12 — round 9: H-fusion-general — the trailing base leg does not vote, 2026-08-12

Predictions continue — this document owns **P63–P70**.

### Why this, and why now

Addendum 9 closed with a named future hypothesis, recorded so it would
not be rediscovered as a surprise:

> **H-fusion-general: the base legs may have the same problem.** Keyword
> and BM25 also contribute by rank, so a base leg with a weak top hit
> votes as hard as one with a strong one.

The rescue-leg version of that hypothesis is the only mechanism in
eight rounds that moved the conversational corpus (round 6 recovered
76% of the lane's macro@1 loss and cleared the kill line for the first
time). Its curve — the graded weight between 0 and 1 — is what failed
two dev gates, and round 7 located why: the right graded constant
differs monotonically and oppositely between corpora, and round 8
measured that the store cannot supply it. This addendum tests the
general hypothesis with a mechanism that owns NO graded constant.

### The dev evidence

`bench/base_leg_census.py` (committed `f0182e3`), artifact
`bench/retrieval/results/base-leg-labels-2026-08-12.json` (committed
`07ad967`): counterfactual labels for both base legs — the gold
document's fused rank with the leg voting and with its weight driven to
zero, the method `bench/leg_labels.py` established for the rescue leg —
over 60 probes per corpus regime (20 questions × asked/requery/control),
unpadded and padded-600.

**Absolute evidence does not stratify base-leg helpfulness.** Keyword
at m=2/3/4+: 0% / 0% / 20% helped (unpadded). BM25 INVERTS: 60% / 57% /
10%. The rescue leg's absolute-count story does not transfer to the
base pair — at a base leg's rank-1, the matched-term count mostly
measures query length.

**Relative evidence stratifies it sharply.** Pooling both legs and both
corpus regimes, by the leg's evidence delta at rank-1:

| stratum | n | helped | hurt | % helped |
| --- | --- | --- | --- | --- |
| leading (Δ ≥ +1) | 29 | 20 | 6 | 69% |
| tied (Δ = 0) | 182 | 29 | 36 | 16% |
| trailing by 1 | 17 | 2 | 7 | 11.8% |
| trailing by 2+ | 12 | 0 | 6 | **0%** |

(The tied stratum's helped/hurt counts are the two legs' complementary
counterfactuals, listed for population context — a relative rule cannot
touch a tie.)

Structurally, with exactly two legs in the base fusion, ONLY the weight
ratio matters — scaling both legs equally is a no-op. So the labels
locate the entire design space in one question: what happens to the
trailing leg.

### Hypothesis

> **A base leg whose rank-1 evidence trails its peer's does not get to
> vote.** Withholding-entirely is already the shipped grammar for thin
> evidence (`_RESCUE_LEG_MIN_EVIDENCE`: "a leg with one word of
> evidence does not get to vote"); this generalizes it to the base pair
> as "a leg whose top candidate matched fewer query terms than its
> peer's does not get to vote." Ties — 80% of dev probes — change
> nothing, byte-identically.

### The rule, and why it is not a curve

```
m_kw, m_bm = matched-term count at each base leg's rank-1
             (the existing _leg_top_evidence, unchanged)
tie (m_kw == m_bm)  ->  weights None — byte-identical shipped fusion
else                ->  leading leg 1.0, trailing leg 0.0
```

**Zero graded constants.** The stated derivation from the labels: every
trailing stratum is net-harmful (2 helped vs 13 hurt pooled; 0% helped
at Δ ≤ −2), so the trailing leg's expected contribution is negative at
every measured deficit; and any weight BETWEEN 0 and 1 is precisely the
scalar rounds 6–7 measured as unresolvable across corpora (monotone,
opposite optima). This mechanism declines to own such a scalar.
Withholding is also count-relative and scale-free — no distribution
spread to shift between corpora, the property round 8's kill demands of
anything that hopes to transfer.

The withheld leg's ranking stays IN the fusion at weight 0 (its unique
candidates keep tail positions rather than vanishing) — the exact
counterfactual the census measured, so the labels transfer 1:1.

### Scope, binding

- Applies to the base pair in BOTH hybrid fusion calls (the base fuse
  and, when the lane is on, the three-leg rescue fuse) — one code path,
  no fork. The rescue leg's own weight machinery is untouched.
- Skipped on the stopword fallback, exactly as the rescue leg is — the
  fallback's TF stream has different matched semantics.
- `keyword` and `bm25` single-leg modes untouched (no fusion there).
- A module constant (`_BASE_LEG_TRAILING_WITHHOLD`) exists so the off
  arm reproduces the pre-change engine exactly; it ships True if and
  only if every gate below passes.

### Instrument assignment — declared before any arm runs

Two sealed instruments now exist. **This experiment's held-out check is
instrument #1 (`bench/heldout/data/`, committed `35227dd`, never
scored).** Instrument #2 (`bench/heldout/data2/`, committed `9524b88`,
blind-authored today under the same protocol) is RESERVED for P2a and
is not read, not scored, and not cited by any arm here — declared now
so two live mechanisms never face one bullet.

**No-read attestation:** no gold label or question text from
`bench/heldout/data/` or `bench/heldout/data2/` has been read by the
implementer at the time of writing. Sha ordering: data `35227dd` <
data2 `9524b88` < this commit < any run commit.

**Ordering disclosure:** the base-leg-labels artifact predates this
document, exactly as addenda 4, 8 and 9 disclosed for their censuses.
The rule above is read off the labels by the stated derivation; the
sharp dev predictions below are deducible from those committed labels
(ties are byte-identical and a withheld-leg query IS the census's own
counterfactual arm), which is disclosed rather than hidden. LongMemEval
and the held-out instrument remain genuinely unmeasured.

### Arms

**Instrument A — `bench/retrieval` (dev).** Unpadded and padded-600,
both prefilter regimes, LANE OFF (the default engine this changes) —
mechanism off vs on. Plus a lane-ON regression pair (unpadded), because
the lane-on path inherits the base weights.

**Instrument B — this directory (dev-contaminated, labelled so in
every row).** Baseline (mechanism off) vs mechanism on, lane off both.

**Instrument C — `bench/heldout/data/` (blind, sealed, instrument #1).**
Scored once, last, and only if every dev gate passes: mechanism off vs
on in the single scoring session. This is the instrument's first and
only spend.

### Predictions

**P63 — off-arm byte-identity.** With the constant False, dev cells
(ids AND scores) and the LongMemEval baseline reproduce exactly
(0.5246 / 0.8935 / 0.9443 to four decimals; reproduction of the
baseline at `693da40` confirmed before this document was committed).
**MISSED if** anything moves — revert regardless of every other result.

**P64 — tie-stratum byte-identity.** On every dev probe where
m_kw == m_bm, mechanism-on output (ids and scores) is byte-identical to
mechanism-off. The blast radius is exactly the trailing-leg queries.

**P65 — the withholding is real and bounded.** The fraction of dev
probes with a withheld leg is > 0 and < 50% on both corpus regimes
(census read: 20.0% unpadded, 28.3% padded).

**P66 — dev does not regress, any stratum, any regime.** Lane off:
asked ≥ 35%/60%, requery ≥ 80%/100%, control ≥ 35%/60% unpadded;
asked ≥ 25%/60%, requery ≥ 70%/100%, control ≥ 25%/60% padded-600;
prefilter-on cells at or above their committed baselines (asked 30/60,
requery 75/100, control 30/60 above-threshold; 35/60, 80/100, 35/60
forced-180). Lane on (unpadded): asked ≥ 55%/90%, requery = 80%/100%,
control ≥ 50%/85%.

**P67 — dev improves where the labels locate the damage.** Padded-600
lane-off recall@1: asked 35% (+10), requery 85% (+15), control 35%
(+10). Unpadded recall@1: requery 85% (+5), asked and control
unchanged at 35%. recall@5 unchanged in every lane-off cell. Sharp
because deducible from the committed labels; a deviation means the
census's counterfactual arms do not reproduce, which is itself a
finding.

**P68 — LongMemEval costs nothing.** macro@5 ≥ 0.8935 AND
macro@1 ≥ 0.5246 AND macro@10 ≥ 0.9443 — the baseline itself on all
three. The hope is a gain toward the 0.9160 line; the GATE is no-cost.

**P69 — held-out confirms, if it runs.** Instrument #1 mechanism-on
macro@5 ≥ mechanism-off macro@5 AND macro@1 ≥ mechanism-off macro@1,
both arms from the same spend.

**P70 — provenance.** `tree_dirty` false on every artifact.

### Kill criteria

1. **P63 fails** → revert, publish, stop.
2. **Any dev stratum falls below its committed baseline** (P66) → the
   mechanism does not ship. Publish the miss. Instrument #1 NOT spent.
3. **Any of the three LongMemEval macros falls below baseline** (P68)
   → does not ship; instrument #1 NOT spent. Publish.
4. **The blind instrument vetoes** (P69) → the default does not change.
   The mechanism survives only as measured record, published.
5. **`tree_dirty` true on any artifact** → run void.

### What ships, stated exactly

`_BASE_LEG_TRAILING_WITHHOLD = True` as the DEFAULT engine — all of
P63–P70, conjunctively, with P67 read as "dev strictly improves at
least at padded asked@1 and requery@1". The flip lands as its own
commit citing this document (one-hop revertable), under the standing
full-ship grant; the session report to the owner leads with it.
Anything short and the constant ships False — code present, default
unchanged, record published.

### Declared confounds

**1. Trailing strata are small.** n=17 at deficit 1, n=12 at deficit
2+. The rule's floor rests on 0/12, which is why it is stated as a
withholding (the shipped grammar for thin evidence) rather than a
fitted weight.

**2. The proxy is one step removed.** "Helped" means the leg moved the
gold document, dev corpus only; LongMemEval and the held-out set can
only be scored, not diagnosed.

**3. The padded regime drives the headline gains** (asked +10 at @1),
and padding's df shifts are exactly what moves BM25's top candidate
onto lower-evidence docs. That is production's corpus shape
(above-threshold), but it is one synthetic corpus's version of it.

**4. Instrument B is dev-contaminated** and labelled so in every row.

**5. The census ran without the prefilter.** Prefilter-regime cells
are gated at no-regression (P66) but their gains are not predicted.

### What is not claimed

- Not that the rescue leg's graded curve was wrong to reject — this
  mechanism deliberately owns no such scalar.
- Not helpfulness, correctness or staleness.
- Not a comparative claim; no claude-mem arm runs.
- Not that the campaign's success bar (as-asked ≥ 60 at recall@1 /
  macro@5 ≥ 0.9160) is reached — P67's ceiling is 35% as-asked at @1.
  This is one mechanism, not the campaign.

