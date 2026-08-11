# The third instrument — search, and why it stopped, 2026-08-11

`bench/longmemeval/PREREGISTRATION.md`'s arc index states the debt this
document tries to pay:

> LongMemEval has now informed parameters across four rounds, and a
> genuinely clean held-out check needs a third instrument. Any successor
> experiment that adapts expansion vocabulary to the store … should say
> so in its first paragraph and budget for that instrument.

P1a (store-derived PPMI co-occurrence expansion) is that successor. It
adapts the expansion vocabulary to the store, which is precisely the
lever four rounds of vote-conditioning could not reach — and it is the
first mechanism in the campaign whose held-out check has to be clean,
because it is the first one that could plausibly earn a default flip.

**The search for that instrument did not find one. This note records the
candidates, the disqualifier for each, and the evidence, so the next
attempt starts from the answer rather than repeating the sweep.**

## The requirements, and why each is hard to satisfy together

1. **Strictly held out.** Nothing about it may inform any parameter, and
   its answer set is never executed against the engine until the
   preregistered run. That rules out anything already in this repo.
2. **Conversational-shaped.** The measured failure domain is casual and
   paraphrased queries over conversational memories — C1 in addendum 4
   is the whole point: identical code flips SIGN between a technical
   corpus and a conversational one. A second technical corpus would
   measure nothing new.
3. **Redistributable, and checked in.** The bench rule is no runtime
   downloads: an artifact has to be reproducible from the repository.
   That means the corpus (or a committed script plus committed seed
   text) lives here, under a license that permits it. This repo is MIT
   (`LICENSE`); a non-commercial or share-alike corpus cannot be
   redistributed inside it.
4. **Small enough to commit.** `.pre-commit-config.yaml` caps added
   files at **500 kB** (`check-added-large-files --maxkb=500`).
   LongMemEval is exempt from this only because it is NOT committed —
   `bench/longmemeval/data/` is untracked, which is exactly the
   dependency the third instrument was supposed to avoid.

Requirements 3 and 4 are what every candidate died on.

## Candidates, and the disqualifier for each

Checked 2026-08-11. Licenses read from the canonical distribution
point, not from a paper's prose.

| candidate | license, at source | disqualifier |
| --- | --- | --- |
| **MSC (Multi-Session Chat)** | ParlAI **code** is MIT (`facebookresearch/ParlAI/LICENSE`). The **data** is a separate tarball, `parl.ai/downloads/msc/msc_v0.1.tar.gz`, **51,116,516 bytes**, carrying no license statement located at that endpoint or in `parlai/tasks/msc/`. | **No data-redistribution grant.** The MIT header in `parlai/tasks/msc/build.py` covers that source file; it does not license the tarball the file downloads. Separately 100× over the commit cap. |
| **PerLTQA** | `Elvin-Yiming-Du/PerLTQA` — GitHub reports **`NOASSERTION`** (no license file it can identify). | **No identifiable license.** Absence of a license is not permission. |
| **LoCoMo** | `adymaharana/locomo` — **CC BY-NC 4.0**. | **Non-commercial.** Cannot ship inside an MIT repository. |
| **DailyDialog** | `li2017dailydialog/daily_dialog` — **CC BY-NC-SA 4.0**. | **Non-commercial AND share-alike.** Doubly incompatible. |
| **EmpatheticDialogues** | `facebook/empathetic_dialogues` — **CC BY-NC 4.0**. | **Non-commercial.** Also not memory-shaped: single-session emotional support, no cross-session evidence. |
| **Cornell Movie-Dialogs** | `cornell-movie-dialog/cornell_movie_dialog` — **no license tag**; original release states research use. | **No redistribution grant**, and film dialogue is not personal-memory shaped — there are no durable facts a later question could retrieve. |
| **Deterministic synthesis from public-domain dialogue** (Project Gutenberg) | Public domain — **the only licence-clean option**. | **Disqualified on methodology, not licensing.** See below. |

## Why synthesis is the wrong answer, even though it is licence-clean

It is the tempting escape and it would produce a bad instrument.

A retrieval benchmark is not its text; it is its **gold labels** — the
mapping from a question to the sessions that answer it. Synthesising a
corpus from public-domain prose means *I* author that mapping, which
means I choose the paraphrase gaps, the vocabulary distance between
question and evidence, and therefore the difficulty. P1a is a mechanism
for closing paraphrase gaps. **An instrument whose paraphrase gaps I
author cannot be a clean held-out check on a mechanism I also design.**

That is a worse contamination than the one this instrument exists to
fix. LongMemEval is compromised because four rounds read statistics off
it; a synthetic set would be compromised at construction, and its
numbers would look exactly as credible while meaning nothing. Round 1's
kill is the precedent — a dev-set win paid for by a held-out set was
"the definition of overfitting to twenty questions", and a
self-authored held-out set is that failure with the evidence hidden.

The bench's own rule covers this: `bench/longmemeval/run.py` bars the
oracle variant from producing a published figure because "any retriever
that is not actively broken scores ~1.0" against it. A self-authored
corpus is the same class of instrument — one whose answer is decided by
its construction.

## What would actually unblock this

In rough order of cost:

1. **An explicit redistribution grant for MSC.** The data is the right
   shape (multi-session, personal facts, casual register). What is
   missing is one sentence of licence. Asking the authors, or finding a
   mirror published under CC BY / MIT, converts the best candidate into
   a usable one. A 500 kB subsample is ample — LongMemEval scores 500
   questions and the gold set scores 20.
2. **Accepting a non-committed corpus** with a pinned checksum and a
   documented fetch, the way `bench/longmemeval/data/` already works.
   This breaks requirement 3 and should be a deliberate, recorded
   decision by the owner rather than something a bench author assumes —
   it is the reason LongMemEval's corpus is invisible to CI.
3. **A human-authored held-out set**, written by someone who has not
   seen the mechanism, on the `bench/retrieval` model — that gold set is
   credible precisely because it was blind-authored. Twenty questions
   took a person an afternoon; the cost is real but the instrument is
   clean, and it is the only option here that is BOTH licence-clean and
   methodologically clean.

Option 3 is the recommendation. Option 1 is worth one email.

## Status

**P1a is not preregistered and no P1a engine code exists.** Writing the
preregistration before the instrument exists would fix its arms against
a held-out set that does not exist, which is the one thing a
preregistration must not do. The campaign is blocked on this note, by
design, rather than proceeding on an instrument that cannot support the
conclusion.
