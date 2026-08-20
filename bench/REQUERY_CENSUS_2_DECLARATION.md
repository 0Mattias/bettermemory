# Requery census 2 — the same family, on the instrument that can see it

The 2026-08-13 census parked two-pass feedback requery by its own §4
rule and permitted exactly one follow-up: "a new declared-first census
naming what would settle it" (`bench/REQUERY_CENSUS_DECLARATION.md`).
This document is that follow-up, and what settles it is the
instrument, not the mechanism:

- The parent census's earn-your-keep pool — asked probes whose gold
  ranked far or absent on pass 1 — had **four members** on the
  20-question instrument. Nothing that happened in that pool was
  measurable, in either direction.
- The 120-question instrument (`bench/retrieval/I1_RECORD.md`) has a
  paired resolution floor of six questions = 5.0 points, an asked
  baseline of 0.2167 recall@1 (so the far/absent pool is most of the
  instrument), and a human requery ceiling measured at 0.6917 recall@1
  in the shipped prefilter regime, its recall@5 row beside it in the
  same artifact
  (`bench/retrieval/results/i1-full120-off-2026-08-18.json`) — the
  best-supported effect the directory has (+58 of 120 at recall@1,
  p=7.7e-14).
- New on this instrument, and named before any number exists: pass 1's
  prefilter drops one gold in five before ranking (gold-in-pool 0.80
  asked), while the human requery probe re-nominates to 1.00. A second
  pass re-enters the engine as a fresh query and therefore gets a
  fresh nomination — on this corpus the mechanism is not only
  re-ranking, it is the first declared shot at golds the first pass
  never saw.

This census answers one question: does the parked mechanism's lift
exist at a scale the parent instrument could not resolve? It does not
relitigate the park — the parent verdict stands as published on its
instrument — and it does not touch the mechanism.

## 1. The mechanism family — frozen, inherited whole

Everything mechanism-shaped is the parent declaration's, by reference
and without amendment: the two passes, the engagement gate at the
engine's own shipped coverage constant (`search._RESCUE_COVERAGE_GATE`,
imported, not copied), the pass-2 query construction (feedback pool,
term scoring, kept/added sets), the 8-cell family crossing
`kept ∈ {all, hooked}` × `F ∈ {3, 5}` × `M ∈ {5, 10}`, the primary
cell `all_f3_m5` by the same minimal-intervention rule, no acceptance
rule in any cell, and the oracle-min diagnostic as a bound that gates
nothing. Eight cells are not eight hypotheses; the anti-gate-shopping
rule carries over verbatim.

Two things change, both instrument-side, both declared:

1. **The instrument.** The committed 120-question pair
   (`bench/retrieval/corpus.jsonl` + `questions.jsonl`, the I1
   instrument), through the runner's own store builder and probe
   constructors, `asked` and `control` kinds. The `requery` kind stays
   the HUMAN ceiling, cited from the committed I1 artifact, never
   re-run, read by no mechanism cell.
2. **The regime.** Pass 1 and pass 2 both run the engine's shipped
   default path over this corpus — which at 1,080 documents means the
   prefilter engages, exactly as the I1 runner's asked arm runs it.
   The parent census ranked the full corpus in process and declared
   the prefilter out of scope; this census inverts that, because the
   shipped truth on this corpus is the prefilter regime and because
   re-nomination is precisely the new headroom named above. The
   engine version executing is recorded in the artifact's provenance
   block, and nothing under `src/` changes.

## 2. The reads — tabulation, no selection

Per probe, per cell: engagement bit, pass-1 gold rank, pass-2 gold
rank (engaged probes; None past depth 50), gold-in-pool bits for both
passes (the re-nomination read), and the kept, dropped and added token
lists with the count of added terms present in the gold document.

Per cell, asked and control separately: n, engaged count, recall@1
and recall@5 for the pass-1 baseline (recorded once), for the cell
(engaged probes take pass 2 wholesale), and for oracle-min. New on
this instrument, because it can finally afford one: **the paired
McNemar exact read, cell versus pass-1 baseline, at both depths**
(`bench/interval.py`, the I1 tooling).

The pools, inherited: the pass-1 far/absent pool (rank 10+ or None,
0-indexed) — on this instrument a majority pool rather than a
four-member one — and the pass-1 hit@1 pool, where engagement-gated
replacement puts existing wins at risk. The cluster-risk pattern is
inherited by name: a cell that gains far/absent recall@5 but not
recall@1 is surfacing the neighbourhood, not the document, and that
is the failure mode, not a partial success.

## 3. The criterion, stated before any result

Reference points, cited from committed artifacts: the asked pass-1
baseline row and the human-ceiling row of
`bench/retrieval/results/i1-full120-off-2026-08-18.json`, shipped
regime (recall@1 endpoints 0.2167 and 0.6917; the midpoint of that
gap is 0.4542). The bars sit on the instrument's own 5-point grid at
or below the midpoint, mirroring the parent's placement rule, and are
fixed exactly as this block states them:

```
LICENSE  primary cell, asked: recall@1 >= 0.45
         AND recall@5 >= the pass-1 baseline's recall@5
         AND paired McNemar vs pass-1 at recall@1 reaches p < 0.05
TWITCH   any cell, asked: recall@1 >= 0.40 AND recall@5 >= baseline
PARK     everything below
```

1. **LICENSE** is half the human gap from store evidence alone,
   damage-free at depth, measured rather than eyeballed. What it
   licenses is the successor preregistration — the document that owns
   the acceptance rule, the held-out ceremony, the
   conversational-register burden, and any path toward the engine.
2. **Anti-gate-shopping, verbatim from the parent:** the primary
   fails but some other cell clears every license condition — that
   licenses at most a follow-up census declaration naming that cell
   as ITS primary.
3. **TWITCH** is recorded as "moves but does not clear"; the only
   permitted follow-up is a declared census naming what would settle
   it.
4. **PARK** closes the two-pass shape on dev for good — unlike the
   parent park, it falls on the instrument that CAN see the effect.
   The lane's remaining food would then be whatever mechanism family
   attacks the vocabulary wall by a different shape, declared first.

## 4. The constraint ledger — inherited, with one inversion

- DEV-ONLY: nothing under `bench/longmemeval/`, `bench/msc/` or
  `bench/heldout/` is read or run.
- No engine code, whatever the outcome; the mechanism prototype stays
  in bench and calls the engine's public entry point twice.
- No fusion, no weights in the engine's path, no acceptance rule.
- Deterministic artifact: lexicographic tie-breaks, sorted iteration,
  two runs at the same commit produce the same bytes.
- No changelog entry.
- The regime inversion (§1.2) is this document's one departure from
  the parent ledger, declared there and here.

## Declared confounds

1. **Cluster risk, inherited.** Feedback vocabulary is cluster-level
   on a corpus built from near-duplicate distractors — now 1,080 of
   them. The far/absent recall@1-vs-@5 read exists to catch it, and
   the pattern is named in §2.
2. **Register, inherited and unpaid.** All 120 topics are the
   technical register; nothing here transfers to conversational
   stores by default. The successor preregistration owns that burden.
3. **The engagement gate is reused, not designed** — chosen because
   it is committed and shipped, not fitted. Probes it declines are
   silent losses, counted per probe.
4. **The ceiling and the baseline come from a different runner** (the
   I1 runner) than the census script. The census records its own
   pass-1 baseline row; if that row disagrees with the cited I1
   asked cell beyond rounding, the run stops and the disagreement is
   the finding.
5. **Prefilter interaction is new territory.** The parent census
   never ran this regime; a pass-2 query that re-nominates golds can
   also nominate fresh distractors. The gold-in-pool bits for both
   passes make the trade countable.

## What is not claimed

- Not a lift on LongMemEval or any conversational corpus.
- Not an engine change or a default: license here licenses a
  preregistration, nothing else.
- Not a relitigation of the parent park; its verdict stands on its
  instrument.
