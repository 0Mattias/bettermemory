# T3 — the note cap, decided: raise to 800, refusal preserved, 2026-08-15

The last unit Lane T's first census scoped. T1's decision rule 2,
declared before that census ran
(`T1_LIVE_STORE_DECLARATION.md`): *"T-P5 hit → the note-cap change is
scoped (raise vs truncate-with-acknowledge decided then, against the
measured distribution). MISSED → closed."*

T-P5 hit — 11.5% of 1,257 recorded notes at length ≥ 450 against a
10% bar (`bench/rot/results/live-store-2026-08-14.json`, `e_notes`
and `predictions`) — so the change is licensed and the only question
left is which one. This is a decision document in R3's mold, not a
preregistration: every quantity it reads is already in the committed
T1 artifact or in git, and nothing new runs.

## Finding 1 — the leak question, closed before the decision

The T1 read carried an unpredicted finding: the artifact's histogram
shows 21 entries in buckets at or above the 500 cap, and the cap
refuses rather than truncates, so the log should not be able to show
overruns — "the gate is younger than the log or a path bypasses it"
(`bench/rot/README.md`, T1 section). Located 2026-08-15, scanning ALL
events via `iter_all_events` — the active-shard glob alone misses
rotated archives, which is exactly where these were:

- 18 notes are strictly over the cap (> 500). Every one is a `verify`
  event dated 2026-05-08 → 2026-05-17.
- The cap shipped 2026-05-20 (`d656a6d`, "fix: small hardening — note
  cap, …", first released in v2.3.1).
- Post-cap overruns: zero, across the entire log.
- The remaining 3 of the 21 bucket entries sit at exactly 500, which
  the inclusive cap admits — boundary-legal, not overruns.

The disjunction resolves to its first half: **CAP-POSTDATES-LOG.** The
gate is younger than the log; no path bypasses it. There is nothing to
seal, and the ten pre-cap days become the one thing a refusing cap can
never produce afterwards: an unconstrained sample of what notes want
to be.

## Finding 2 — the distribution, read for the decision

From the committed artifact's `e_notes` (aggregates only, as declared):

| bucket | n | | bucket | n |
| --- | --- | --- | --- | --- |
| 000–099 | 175 | | 500–599 | 8 |
| 100–199 | 233 | | 600–699 | 6 |
| 200–299 | 349 | | 700–799 | 4 |
| 300–399 | 268 | | 800–899 | 1 |
| 400–499 | 211 | | 1100–1199 | 1 |
| | | | 1400–1499 | 1 |

Two facts carry the verdict:

1. **The squeeze is real but the bulk is comfortable.** The mode is
   200–299 and 78% of notes end below 400; the cap does not distort
   ordinary use. The pressure is in the tail: 144 notes (11.5%) within
   50 characters of the ceiling, where a writer trimming to fit is
   indistinguishable from a writer who stopped naturally — except by
   the next fact.
2. **The unconstrained tail has a knee at 800.** In the ten pre-cap
   days, overruns decay 8 → 6 → 4 across the 500/600/700 buckets —
   ordinary rationales that needed one more paragraph — then collapse
   to scatter: one note in 800–899, one at 1100+, one at 1400+. The
   three full buckets carry 18 of the 21 at-or-over entries; past 800
   what remains is exactly the paste-shaped material the cap's own
   comment names as its target ("pasting whole transcripts belongs in
   a memory body, not in an event note").

## Finding 3 — truncate-with-acknowledge, refused on the same evidence

The alternative loses on every axis the census can see:

- **It fires on a path the log shows is never taken.** Post-cap wire
  overruns are zero; writers trim before calling — that is what the
  squeeze mass at 400–499 *is*. An acknowledge-truncation flag would
  almost never execute, and when it did it would move the cutting from
  the writer, who knows which clause is load-bearing, to the server,
  which cuts at a character count.
- **It breaks the log's honesty property.** Today every recorded note
  is exactly what the writer sent — T1's declaration leaned on this
  ("the cap refuses rather than truncates, so the log cannot show
  overruns") and Finding 1's leak analysis was only possible because
  of it. A truncated note is a mutated record in an audit log whose
  entire value is being exact.
- **It relieves nothing.** The measured problem is that evidence gets
  compressed to fit the ceiling. Server-side truncation compresses
  harder, blindly. Only a higher ceiling answers the finding.

Refusal also keeps its teaching function: the error message names the
cap and the remedy, and costs one retry on a path taken approximately
never.

## The verdict

**Raise `_NOTE_MAX_LEN` from 500 to 800
(`src/bettermemory/handlers/_shared.py`). Refusal semantics unchanged
in kind: over-cap notes are refused with the teaching error, never
truncated.**

800 and not more: at 1000 the refused set collapses to the two extreme
outliers and the ceiling stops disciplining anything short of an
actual paste; the DoS purpose (megabytes, not hundreds of characters)
is served equally at any of these values, so the quality knee decides.
800 and not 750: the artifact publishes buckets of 100, and 750 splits
a bucket the data cannot resolve — 800 is a boundary the histogram can
actually see, admitting the three decaying buckets whole and refusing
the scatter.

### Scope, exactly

Changed:

- `_NOTE_MAX_LEN` 500 → 800, with the constant's comment carrying this
  decision's receipt.
- The two description lines that state the cap to the model
  (`memory_verify`, `memory_record_use`: "≤500 chars" → "≤800 chars").
  Same character count, so the resident footprint is unmoved — the
  A-P3 quantities (descriptions 25,993 of 26,000, schema remainder
  7,438) stay exactly where T2 left them, and no space needs freeing.
- The four contract tests (two per handler: over-cap refused,
  boundary accepted) move to 801/800. A deliberate contract change
  updating its own pins, not an assertion weakened.

Deliberately untouched, each for its own reason:

- **`claim_excerpts` cap (500).** An excerpt is a quote, not a
  rationale — "the excerpt is supposed to be a quote, not a copy" —
  and the census measured no squeeze on excerpts.
- **`memory_acknowledge_miss`'s `_MAX_REASON_LENGTH` (500).** A triage
  one-liner over a tiny population (11 acks in the whole log at T1).
  Its comment stops claiming to mirror `_NOTE_MAX_LEN` and states its
  own ceiling on its own merits.
- **`live_census.py`'s `NOTE_CAP` / `NOTE_SQUEEZE_FLOOR` (500 / 450).**
  Declaration constants, verbatim from T1's sealed declaration; they
  describe the contract T1 ran under and grading T-P5 against them is
  what keeps re-runs comparable. A comment marks the divergence. Any
  future census of note pressure under the 800 contract declares fresh
  constants in its own declaration.
- **`SCHEMA_VERSION`.** A validation-cap loosening accepts inputs it
  previously refused and refuses nothing it previously accepted —
  additive by the compatibility contract, a minor release.

## What is not claimed

- One store, one user, one machine — T1's own caveat, inherited whole.
- The unconstrained sample is ten days and 21 notes. Small; but it is
  the only sample a refusing cap can ever have produced, and the
  decision uses it for the shape of the tail, not for a rate.
- Nothing here re-reads T-P5. The squeeze definition (≥ 450, within
  50 of the 500 cap) remains the T1 artifact's own term; whether
  squeeze recurs against 800 is a question for a future declared
  census, not this document.

## Owner doors, untouched

Lane T criterion v1 remains proposed, not self-ratified; no v2
criterion is drafted; no default moves. This decision exercises
exactly the delegation T1's decision rule 2 pre-committed, and
nothing else.
