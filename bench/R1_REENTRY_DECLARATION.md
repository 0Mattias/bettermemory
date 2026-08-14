# R1 — the pretrained arm re-enters and must reproduce its own dated record, declared before any cell runs, 2026-08-13

Stage one of the reentry ladder `bench/DOOR_C_DECISION_BRIEF.md`
committed. The doctrine question is settled and does not reappear
here; this document exists to keep the reentry under the same
discipline that priced every from-scratch lane: the family, the
cells, the bars, the primary, and the parking criterion are all fixed
below before the lane is rebuilt or a single number exists. The
enforcement record is the sha ordering: this commit, then the
implementation commit, then the run commit. Nothing may be added
afterwards.

The owner's greenlight for the model fetch was given 2026-08-13
("you have the greenlight"), lifting the hard gate the decision brief
and forward plan recorded. The fetch this declaration authorizes is
the one named in §2 and no other.

## 1. What re-enters, precisely

The embedding lane removed whole by the 4.0.0 strip (commit
`1bb73bc`), restored surgically and modernized to the current engine,
**search path only**:

- the provider/cache module and its setup companion, restored from
  the pre-strip ancestry;
- the engine's semantic scoring leg and its two consumers: the pure
  `semantic` mode, and a third equal-weight leg in the `hybrid`
  fusion when a model is present — the pre-4.0 shape that produced
  the dated record, unchanged;
- the opt-in extras in packaging (torch and fastembed provider
  variants, as before the strip);
- the retrieval runner's `semantic` arm, un-dropped.

Deliberately NOT restored in R1: the write-path semantic dedup gate,
the reindex CLI, and every other pre-strip consumer of the model.
The door C contract admits pretrained weights to the retrieval arm;
R1 restores the minimum surface that arm needs and nothing else.
The default install and default engine are untouched: with no extra
installed, every code path above is inert and the hybrid fusion is
the shipped two-leg lexical fusion, byte-identical.

## 2. The pin — model, revision, digest, license

- Model: `all-MiniLM-L6-v2` (sentence-transformers namespace on
  Hugging Face) — the pre-strip torch-provider default
  (`DEFAULT_MODEL_NAME` at `1bb73bc^`), and the one embedding model
  present in this machine's Hugging Face cache, which is the
  checkpoint the dated record was produced with.
- Revision: `1110a243fdf4706b3f48f1d95db1a4f5529b4d41` (the cached
  `refs/main` snapshot).
- Weights digest (sha256 of the safetensors blob, already on disk):
  `53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db`.
- License: Apache-2.0 (recorded from the model card at the pinned
  revision; the run artifact records it again).
- Fetch surface: the model weights are ALREADY in the local cache —
  the run must execute with Hugging Face offline mode enforced so
  the pinned snapshot is the only possible source and no network
  fetch of model bytes occurs at run time. What IS fetched, once,
  explicitly, under the owner's greenlight: the provider runtime
  (the `embeddings` extra's packages) from PyPI into the project
  venv. Package names and versions land in the artifact's
  provenance.
- The artifact records: model name, revision, weights sha256,
  license, provider package versions, and the offline-mode flag.

## 3. The cells — the dated record's four invocations, re-run with the arm live

Same instrument, same committed corpus and questions, same runner
entry points; both arms in every run so every comparison is paired:

1. unpadded, prefilter off — the dated record's primary
   configuration;
2. padded to 600, prefilter off — the dilution regime;
3. padded to 600, prefilter both — production's own threshold and
   handler path: **the cell the pre-4.0 record never scored for the
   semantic arm** (its half of the original prediction 5);
4. forced-180 threshold, prefilter both — the no-filler prefilter
   regime, equally never scored for this arm.

Probes: asked, requery, control — the committed three. No new
questions, no new corpus, no parameter of the arm tuned against any
cell: the arm runs at the restored lane's defaults (cosine floor,
fusion weight 1.0 as an equal leg, RRF unchanged), which are the
pre-strip defaults. If any restored default cannot be carried
byte-for-byte into the modern engine, the deviation is recorded in
the implementation commit and named in the artifact — before the run.

## 4. The bars — fixed now, judged on cell 1

The dated record this must reproduce, quoted from its artifacts:
semantic/asked measured 60% recall@1 and 75% recall@5 against
lexical/asked 35%/60% on the unpadded corpus
(`bench/retrieval/results/unpadded-2026-08-08.json`), and held 60%
at recall@1 padded to 600 while lexical read 25%
(`bench/retrieval/results/padded600-2026-08-08.json`).

- **Primary cell**: unpadded / prefilter off / `semantic` arm /
  `asked` probe / recall@1.
- **R1-PASS requires both**, on the primary cell:
  - (a) recall@1 in the band 50–70% — within two questions of the
    dated record on this twenty-question instrument;
  - (b) margin over the same-run lexical/asked recall@1 of at least
    ten points — the reproduction is of the LIFT, not just the
    level; a run where both arms drift up together must still show
    the arm earning its keep.
- **Integrity reads** (must hold for PASS; any miss is recorded and
  demotes the verdict to PARTIAL pending diagnosis):
  - semantic/requery ≥ semantic/asked at recall@1 (a requery that
    scores below asked signals a broken leg, not a corpus shift);
  - semantic/control within two questions of semantic/asked at
    recall@1 (the vocabulary finding: control tracks asked);
  - lexical cells within two questions of their committed values in
    the same artifacts (the restoration must not have moved the
    default engine).
- **Determinism**: the primary invocation runs twice; the two
  artifacts' results blocks must be identical. A mismatch is a FAIL
  regardless of the numbers.
- **R1-PARK**: primary recall@1 below 50%, or margin below ten
  points, or determinism fails. A park stops the ladder: no R2, and
  the diagnosis (engine drift, provider numerics, prefilter
  interaction) gets its own document before any re-run. Parking
  publishes exactly like every park before it.

## 5. Cells 3 and 4 — measured, not gated

The two prefiltered cells are first measurements, not reproductions:
no gate, no bar, no verdict rides on them in R1. Declared reads, to
be recorded whatever they say:

- semantic-arm recall@1/@5 deltas, prefilter on vs off, per probe —
  the arm's real cost under production nomination, where a document
  only paraphrase would find may never reach the pool. The named
  risk, from the rescue lane's measurement of the same regime: the
  5.1 lane lost fifteen points of recall@5 to nomination on the
  as-asked probe
  (`bench/retrieval/results/prefilter-above-threshold-2026-08-09.json`);
  the semantic arm may pay similarly, and R2/R3 design must eat that
  number, not argue with it.
- `gold_nomination_rate` per cell, unchanged meaning.

These reads feed R2's declaration; they gate nothing here.

## 6. The constraint ledger

- Sealed instruments stay sealed; nothing under `bench/heldout/` is
  opened; instrument #2 stays reserved for P2a. LongMemEval is not
  touched in R1 — it is R2's instrument, with its own declaration.
- The lexical engine's code paths are not edited beyond what
  restoring the leg's call sites requires; every restored default is
  the pre-strip value or a named deviation per §3.
- No tuning against any cell: this family has one configuration.
  There is no grid, so there is nothing to shop.
- Offline at run time (§2): weights load from the pinned local
  snapshot with offline mode enforced; the runner asserts it.
- The artifact is deterministic modulo the provenance date; the
  determinism bar in §4 enforces it where it counts.
- Test discipline: the restored lane's pre-strip test modules return
  with it, adapted only where the modern engine's interfaces moved;
  strip-era guard tests that assert the lane's ABSENCE are updated
  to assert the new contract (opt-in, inert-by-default) — a
  principled amendment under the door C decision, not a snuff.
- No changelog entry for the declaration or run commits; the
  implementation commit carries the user-facing changelog line for
  the restored extra.

## Declared confounds

1. **Provider runtime version drift.** The dated record's provider
   ran at whatever package versions July's environment held; today's
   install resolves current versions of the same packages. The pin
   fixes the WEIGHTS; runtime numerics may differ at floating-point
   tolerance. The reproduction band absorbs this; the artifact
   records the resolved versions.
2. **Engine drift is the quantity under test, entangled with the
   port.** Nine releases separate the dated record's engine from
   today's. Cell 1's lexical integrity read (§4) is the control that
   separates "the engine moved" from "the port is wrong": if lexical
   cells reproduce and semantic does not, the port or the arm is
   implicated; if lexical cells moved too, the engine did.
3. **The dated record's own artifacts do not name their provider.**
   The model identification in §2 rests on the pre-strip default
   plus the single-model cache remnant. If the record was in fact
   produced by the fastembed provider, the checkpoint is different
   and the band comparison inherits that unknown; the confound is
   named rather than resolvable.
