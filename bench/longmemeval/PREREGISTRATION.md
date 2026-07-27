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

**Absolute levels are not anchored yet.** The paper publishes recall
figures for its own indexing strategies, and those have not been read into
this document. Until they are, the predictions below are deliberately
*relative* — which arm wins and by how much — because a pre-registered
absolute interval invented from memory is worse than none.

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

**3. Abstention questions are absent from the oracle file, and the ~30
figure above is not yet confirmed for the scored corpus.** Zero questions
carry the `_abs` suffix and zero have an empty `answer_session_ids` —
consistent with abstention questions being excluded from an evidence-only
file, since they have no evidence to include. The exclusion rule stated
above stands, but the **count** it applies to must be measured on
`longmemeval_s_cleaned.json` and published, not carried over from the
paper's prose.

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
