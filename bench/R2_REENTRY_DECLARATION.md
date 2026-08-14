# R2 — the held-out read: LongMemEval with the arm, declared before the runner regains it, 2026-08-14

Stage two of the reentry ladder `bench/DOOR_C_DECISION_BRIEF.md`
committed, licensed by R1's PASS (`bench/R1_REENTRY_DECLARATION.md`;
artifacts `bench/retrieval/results/r1-*-2026-08-13.json`). The ladder's
own definition governs: *"R2 — the held-out read. LongMemEval with the
arm on, against the macro criterion,"* with the C1 polarity lesson
riding along. The enforcement record is the sha ordering: this commit,
then the implementation commit, then the run commit. Nothing may be
added afterwards.

One sentence of the record is corrected rather than inherited: the
brief's evidence section said no LongMemEval run had ever combined
this engine with an embedding arm. The dated record holds two such
runs (the 3.30.0 record and its bit-for-bit 3.42.0 reproduction), and
the correction is now in the brief itself, dated. What remains true,
and what R2 exists to measure: the MODERN engine — post-strip,
post-restoration, 5.5.0 — has never run the arm on this instrument,
and the reference figure belongs to a different system's whole stack.
R2 is therefore the same shape as R1: a reproduction of our own dated
record through the restored lane, on the held-out conversational
instrument this time.

## 1. What runs, precisely

The instrument, unchanged from the dated record in every particular:
`bench/longmemeval` under its committed runner; the committed corpus
`longmemeval_s_cleaned.json` whose sha256 the runner records and this
declaration pins (`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`
— a run against any other bytes is void); 500 instances; retrieval
depth 200; distinct-session collapse; per-question stores ingested
through `Store.write`. Stores hold ~249 items against a 500-item index
threshold, so the prefilter never engages — the dated record's regime,
kept, and its limitation kept too (the above-threshold arm stays this
directory's named open item; it is not R2).

The implementation this declaration licenses, and nothing else: the
runner regains its `semantic` arm by reverse-applying the drop
(`c4ebd30`, the R1 method), adapted to the restored lane's modern
surface exactly as `bench/retrieval/run.py` already carries it —
offline mode enforced in the environment before the model import,
provider resolved, the R1 §2 provenance block stamped into the
artifact, the arms default restored to `lexical,semantic`, and a
SKIPPED note when the extra is absent. Both arms invoke
`mode="hybrid"` as they did before the drop: the lexical arm with no
model — the shipped two-leg fusion, byte-identical to the default
engine — and the semantic arm with the pinned model present as the
third equal-weight leg, the pre-4.0 shape R1 restored and validated.
No engine code changes. No new flags. Round-era mechanisms stay at
their shipped defaults (rescue lane off, base-leg withholding off, no
ablation). If any restored detail cannot be carried byte-for-byte
into the modern runner, the deviation is recorded in the
implementation commit and named in the artifact — before the run.

## 2. The pin — R1's, unchanged, and zero network anywhere

Model `all-MiniLM-L6-v2` at revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, weights sha256
`53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db`,
Apache-2.0 — the R1 declaration's §2 pin verbatim, already resident in
the local cache and already validated by R1's byte-for-byte dev
reproduction. The provider runtime (torch) is already installed in the
project venv from R1's greenlit fetch; resolved package versions land
in the artifact's provenance block. The corpus has been on disk since
2026-07-27 and its sha is asserted per §1.

So R2's every phase — smoke, cells, repeat — runs with Hugging Face
offline mode enforced and touches the network zero times: no model
fetch, no package install, no corpus download. There is nothing left
to fetch.

## 3. The cells

1. **The reproduction cell**: one invocation, both arms, full 500 —
   the dated record's exact configuration.
   `--arms lexical,semantic --json` with a per-question sidecar.
   Artifact `results/r2-both-arms-2026-08-14.json`; sidecar
   `results/per-question/r2-pq-2026-08-14.json`.
2. **The determinism repeat**: the identical invocation a second
   time. Artifact `results/r2-both-arms-repeat-2026-08-14.json`;
   sidecar `results/per-question/r2-repeat-pq-2026-08-14.json`.

One `--limit 20` smoke invocation before cell 1 is permitted for
plumbing (the runner's own smoke convention). Its output is not an
artifact, is not written to `results/`, and its numbers are not
citable — the runner already stamps subset output as not publishable.

No new questions, no new corpus, no parameter of the arm tuned against
any cell: one configuration, the restored lane's defaults, no grid.

## 4. The bars — fixed now, judged on cell 1

The dated record this must reproduce, quoted from
`results/baseline-both-arms-2026-08-08.json`: semantic macro recall
0.5622 / 0.9185 / 0.9561 at k = 1/5/10 against lexical
0.5246 / 0.8935 / 0.9443, a semantic-over-lexical lift of +0.0250 at
macro recall@5.

- **Primary cell**: semantic arm / macro recall@5 / cell 1.
- **R2-PASS requires both**, on the primary cell:
  - (a) macro recall@5 **within one point, either side, of the dated
    0.9185**
    (`bench/longmemeval/results/baseline-both-arms-2026-08-08.json`)
    — that number plus and minus one point is the whole band, and its
    sole derivation. One point is roughly five questions'
    worth of movement on this 500-question instrument: wide enough to
    absorb provider-numerics drift across torch versions (R1 measured
    that drift at zero on the dev instrument), far too narrow for a
    broken port, whose failure shape is the lexical figure or worse.
  - (b) lift: semantic minus lexical macro recall@5 of at least
    **+0.0100** — the reproduction is of the LIFT, not just the
    level; a run where both arms drift together must still show the
    arm earning its keep.
- **Determinism**: cell 2's results and per-question records must be
  identical to cell 1's, excluding each arm's wall-clock `seconds`
  and the provenance date. A mismatch is a FAIL regardless of the
  numbers.
- **Integrity reads** (must hold for PASS; any miss is recorded and
  demotes the verdict to PARTIAL pending diagnosis):
  - lexical macro recall equal to the committed
    0.5246 / 0.8935 / 0.9443 to four decimals at k = 1/5/10 — the
    engine-drift control that separates "the engine moved" from "the
    port is wrong", exactly R1's structure. This read doubles as the
    restoration's held-out inertness proof: 5.5.0's
    inert-by-default claim has until now been proven on the dev
    instrument only.
  - semantic macro recall@1 within 0.015 of the dated 0.5622, and
    above the lexical arm's figure;
  - semantic macro recall@10 within 0.010 of the dated 0.9561;
  - single-session-preference recall@5 margin (semantic minus
    lexical) of at least +10 points — the dated margin is +23.3
    points on a 30-question class, and it is the lift's signature: the class
    where question and evidence share no vocabulary. An arm that
    reproduces the pooled number without this class is not
    reproducing the mechanism.
  - depth-truncation counts unchanged (0 / 2 / 9 questions at
    k = 1/5/10) in both arms.
- **R2-PARK**: (a) or (b) missed, or determinism FAIL. A park stops
  the ladder: no R3, and the diagnosis (port error, provider
  numerics, engine drift) gets its own document before any re-run.
  Parking publishes exactly like every park before it.

## 5. Declared reads — measured, not gated

- **The reference line.** Whether the semantic arm's macro recall@5
  meets or exceeds 0.916 — the dated claude-mem@13.12.4 figure
  (`bench/longmemeval/results/claude-mem-full500.json`), their whole
  stack, measured 2026-07-27. Recorded whichever way it lands.
  This is NOT the campaign's success criterion: that criterion reads
  "on the DEFAULT engine" and belongs to R3. Nothing in R2 claims the
  bar, whatever this read says.
- **The multi-session slice.** Per-arm recall@5 against the dated
  record's own multi-session column, both arms
  (`bench/longmemeval/results/baseline-both-arms-2026-08-08.json`),
  beside the reference stack's
  (`bench/longmemeval/results/claude-mem-full500.json`) — the
  measured deficit lives in this class, and this
  is the read that matters for R3's design.
- **C1 polarity, per class.** All six question types' semantic-minus-
  lexical deltas at recall@5, recorded. The dated record shows no
  class harmed on this instrument; the polarity lesson rides anyway —
  this is its first read through the modern engine, and any class
  where the arm hurts is a finding under the knob precedent, not a
  failure.
- **Cost.** Per-arm wall seconds against the dated 331.4 / 1229.3 —
  the ~3.7× embedding-arm factor re-measured, per-question store
  encoding included, feeding R3's default-question pricing.
- **Micro recall**, both arms, against the dated
  0.4357 / 0.8671 / 0.9325 and 0.4662 / 0.8903 / 0.9441.
- **Abstention.** Zero abstention instances exist in the distributed
  corpus (the data-integrity note stands), so that ability remains
  unmeasurable here. Named, and no handling code is added for it.

## 6. The constraint ledger

- Sealed instruments stay sealed: nothing under `bench/heldout/` is
  opened; instrument #2 stays reserved for P2a; the blind
  instrument's single-spend rules are untouched by this document.
- The runner alone changes. No engine code, no config surface, no
  packaging change; the default install and default engine are
  untouched — with no extra installed the restored arm is inert and
  the runner emits its SKIPPED note.
- Offline at run time (§2): the runner enforces offline mode in the
  environment before the model import, so the pinned snapshot is the
  only possible source.
- No tuning against any cell; one configuration; there is no grid, so
  there is nothing to shop.
- Per-question sidecars are analysis input, not citable evidence —
  the number-claims guard's pool design, unchanged.
- No changelog entry: R2 is bench-only, and no release rides it.
- Test discipline: the suite must be green before each commit ships,
  under the venv's marker convention for an extras-carrying
  environment.

## Declared confounds

1. **Provider runtime version drift.** The dated record's arm ran at
   July's package versions; today's venv resolves torch 2.13-era
   packages. The pin fixes the WEIGHTS; runtime numerics may differ
   at floating-point tolerance. R1 measured this confound's dev-
   instrument effect at exactly zero (byte-identical cells); the §4
   band absorbs what remains here, and the artifact records the
   resolved versions.
2. **Engine drift is entangled with the port.** Fourteen-plus
   releases separate the dated record's engine from today's. The
   lexical integrity read is the control: if lexical reproduces to
   four decimals and semantic does not, the port or the arm is
   implicated; if lexical moved too, the engine did.
3. **The dated record's artifacts do not name their provider.** Same
   confound as R1's §3, inherited: the LongMemEval artifacts stamp
   the engine version but not the model or provider. The
   identification — torch provider, the §2 pin — rests on the
   pre-strip default plus the single-model cache remnant, now
   corroborated by R1's byte-for-byte reproduction of the dev record
   under exactly this pin. On this instrument it stays a named
   confound until cell 1 lands inside the band.
