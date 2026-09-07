# Roadmap

Planned work, in rough priority order. Plans change; the
[CHANGELOG](../CHANGELOG.md) is the source of truth for what shipped,
and an entry leaves this file when it lands there.

## Planned

- **The usage-signal flags: four bars declared, read once, all four
  HOLD — and the next read is an evidence trigger, not a date.** Four
  ranking/delivery flags are built, tested and OFF: `endorsement_boost`,
  `outcome_demotion`, `corroboration_boost` (`config.py`, all default
  false) and `standing_tier` (shipped 3.42.0, "flip only with dogfood
  evidence"). The bars stand exactly as declared; what the read changed
  is the checkpoint's clock. Read with `bettermemory eval
  --usage-replay` (methodology in eval.md), which measures and never
  flips, alongside a fresh `eval --report` snapshot
  ([eval-results.md](eval-results.md)):
  1. `endorsement_boost` — flip when the explicit-endorse density holds
     ≥40 distinct memories over the trailing 30 days AND an offline
     replay of the window's audited turns (per-turn `top_hits`, flag
     toggled, no store mutation) shows at least two-thirds of changed
     top-1s improving and no miss-labeled turn worsening, on n ≥ 10
     changed turns; fewer changed turns is a hold for blast-radius
     evidence. **HOLD:** density clears comfortably, and the replay
     produced no changed top-1 at all — the flag engaged on a handful
     of turns, so there is nothing yet to judge.
  2. `outcome_demotion` — same replay protocol at ≥20 negative-outcome
     density, plus one invariant: zero demoted memories that were a
     later turn's explicitly-applied top-1 inside the window. **HOLD:**
     density clears, the invariant is clean, and the handful of changed
     top-1s is far under the n ≥ 10 floor. Direction is mildly
     encouraging (one improving, none worsening, the rest neutral) and
     that is not evidence at this n — which is what the floor is for.
  3. `corroboration_boost` — liveness gate before any replay: ≥10
     memories with ≥1 corroboration and ≥3 with ≥2. **HOLD, as
     pre-recorded:** not one memory in the store carries a
     corroboration, so the signal this flag ranks on has still never
     fired live. The cheapest flip stays the least ready. This one is
     no longer waiting on evidence in the way the other two are, and
     the next read on it should answer a different question: after four
     months of daily use with zero corroborations recorded, is the
     signal unreachable in practice — nothing in any shipped workflow
     asks for one — or is the flag ranking on an event that does not
     occur? The first is a gap to close, the second retires the flag
     and its config surface. Deciding that needs no new telemetry.
  4. `standing_tier` — two-stage. Dogfood-config flip (never the
     shipped default) when ≥2 receipts exist of standing content going
     unserved by retrieval in 30 days; shipped-default flip only after
     ≥2 weeks of dogfood soak with no misdelivery and the 1024-byte
     budget holding. **HOLD:** receipt #1 (the 2026-07-26
     STOP-SURFACING directive, recategorized `ambient` and so
     deliverable the moment the flag flips) has aged out of any 30-day
     window and no second receipt was recorded. This flag is also the
     one the replay surface cannot speak to, so its hold rests on
     receipts alone.
  **Why the next read is not another date.** The 2026-09-09 checkpoint
  was derived from the `delivered_reason` calendar, but the binding
  clock is the exact per-turn toggle capture the production ranker
  began recording on 2026-08-30 — turns before it are counted
  not-replayable, never approximated. `--usage-replay --since all`
  returns replay counts identical to the 30-day window, so the read
  already consumed every capture in existence and waiting would have
  added days, not evidence. Both replay bars are gated on n ≥ 10
  changed top-1s, and the observed change rate does not reach that
  within days of the original checkpoint. So the trigger replaces the
  date: re-read when `--usage-replay` reports n ≥ 10 changed top-1s on
  either replay bar. An unread bar is still a hold, not a pass, and a
  hold at n = 0 is a statement about evidence rather than about the
  flags.
- **Cause provenance.** The 6.5.0 label says how a file entered the
  store, not what was in context when the model wrote it, so an
  injection-driven legitimate write reads `local`. A write-time record
  of the source material (`groundedness_check` / `source_transcript`
  are the seed) is the open question behind the label.
- **Write-path hardening, remaining items.** `apply_write_gates` is the
  shared chain (3.31.0) and ingest runs the caps and the scope
  allowlist through it (3.39.0). Two paths still keep their own copies,
  each for a reason that makes the reroute policy review rather than
  mechanics: `consolidate._apply_llm_proposal` judges the LLM-authored
  claim rather than the stamped body, so it hand-rolls size, transient
  and similarity (`tests/test_proposals_gate_parity.py` pins the
  divergence); and `memory_update` mirrors the credential and
  user-claim gates by hand because `find_similar` takes no exclusion
  id, so the dedup gates would score an edited body against the
  record's own stored copy and report it as its own duplicate.
- **Event-time on the memory record.** Every timestamp on `Memory`
  (`created`, `updated`, `last_verified_at`, `last_corroborated`) is
  storage time; nothing represents when a fact is *about*, or when it
  stops being true. `_recency_factor` is deliberately a maintenance
  signal — a 1.1x-capped bump on `max(created, updated)` — not a
  temporal one. Zep's Graphiti ships validity intervals and
  point-in-time queries today, so this is a gap against shipping
  product rather than a nicety.
- **Encryption at rest.** An `[encrypted]` extra with `age`-backed
  per-file envelope encryption, complementing the write-time
  credential check. Not expected in 2026.
- **`episode_search(ids=…)` has no by-filename fast path, and the win is
  unmeasured.** The refusal is written into `handlers/episode_search.py`
  with its reasoning, and the validator to mirror already exists in
  `episodes.py`. Two things to know first: on a bare `ids`-only call the
  candidate loop still walks every session, so the saving is proportional
  to session count rather than to 1; and three behaviours ride the
  post-load loop (the floor skip, the `since`/scope filters, the datetime
  sort) that any short-circuit must preserve. Bench before building.
- **A renamed checkout orphans a memory's drift legs, and no shipped
  path reconciles it.** `origin.worktree_root` is captured once at write
  time and never re-resolved, so renaming a project directory leaves
  every memory written from the old one pointing at a path that is gone.
  `memory_health`'s estate check reports that group under `skipped`
  ("worktree missing on disk") and those memories stop being judged
  entirely — not fresh, not drifted, unjudgeable, and absent from
  `curation_pending`, which is why a curation pass driven off the rollup
  walks past them. `migrate origin --repair` cannot help: its two rules
  only ever rewrite `repo`, which in this shape is already correct. The
  origin block is not on the `memory_update` surface either, so the
  repair today is a hand edit plus `reindex` — the exact write
  `memory_content_evidence` is built to flag. The open question is which
  leg to fix: adopt the live worktree the estate check already resolves
  for that repo's other memories (read-only, and it fixes the health
  surface alone), or stop trusting a recorded root wherever the repo
  resolves elsewhere on disk (which also fixes relative-citation
  resolution, and changes drift semantics — the reason this is an entry
  here rather than a patch). Found by sweeping every recorded
  `worktree_root` against disk, which is the check the store does not
  currently run for itself.

## Not planned

- **Flipping `_COMMIT_DRIFT_ESCALATES` to `False`.** The switch's own
  pre-registered condition — recorded in a source comment in
  `verify.py`, citing an "upgrade plan item B2b" that exists nowhere in
  this repository — fired on 2026-07-31, and the reading is a
  retraction rather than a flip. The trigger is
  `pooled.file_level_incumbent.ALL.alerts_per_catch` = 3.4 ≥ 1.5 on the
  30-repository corpus; that is the column the gate meant, because it is
  scored on `_MODES[0]` rows where the calendar leg is stood down and
  path drift fires zero times, so every one of its flags *is* the
  escalating commit term. The same artifact's
  `path_drift_anchored_relative_arm.ALL.alerts_per_catch` = 1.0 reads
  "stays True" and is not the same term — it grades the path leg.
  A dry run with the switch monkeypatched off, over the pinned 60-day
  window, measures the consequence: every drift arm goes 96.74% → 0.00%
  flagged and J 0.0339 → 0.000, which is exactly `never_flag` — the
  mirror image of the `always_flag` constant function 3.30.0 fixed and
  postmortemed — while `shipped_default` stays bit-identical, because
  the demotion branch reads `commit_drift_count` directly and bypasses
  the switch. The gate's premise is falsified with it: the anchored path
  leg it assumed would substitute reads `flag_rate` 0.0073 at
  `unflagged_stale_rate` 0.968. Rejected alternative, since it is the
  obvious one: flip anyway and let the path leg carry escalation — that
  is the constant function `bench/rot` exists to catch, with the sign
  reversed. Reopening needs a replacement measured first, which
  claims-at-write (3.40.0) supplied as upstream narrowing with the
  switch untouched. Write-up in the rot-bench notes; artifact
  `bench/rot/results/escalation-off-60d-2026-07-31.json`.
- **A per-memory mutation index for the relevance-label widening
  program.** The measurement half shipped in 3.14.0, and three
  hand-labeling passes over live turns scored the widening candidates
  against a ≥~70% precision gate
  ([2026-07-08](eval/widening-labeling-2026-07-08.md) ·
  [2026-07-22](eval/widening-labeling-2026-07-22.md) ·
  [2026-07-29](eval/widening-labeling-2026-07-29.md)).
  `w1_top1_v2_high` (the bare matched-token floor) was ruled out at
  ~15–30%; `w2_top1_v2_high_from_medium` (promote medium→high only)
  held ~48–54% across three independent windows, ~51% combined over 79
  labeled promotions, and is dropped per the recorded band rule. Both
  stay in `WIDENING_RULES` as preview-only baselines; the live label
  and the shadow contract are unchanged. The surviving candidate is w2
  minus flags whose top hit the same session had just written or
  updated (content already in context, so an impossible retrieval
  win), and its only implementable form is the `write`/`update` event
  stream plumbed through both widening lanes: `ThresholdRule.check` is
  a pure per-turn predicate with no access to event history, so the
  exclusion cannot be a registry entry the way `w2` was. It is not a
  session-id join either: mutation events carry the MCP server
  session, `turn_audited` carries the client session UUID, and the
  namespaces do not map. The candidate does not earn the build, and it
  cleared the registered gate to get here, which the refusal has to
  own: 15/21 = 0.714 meets ≥~70% on the point estimate, and what
  declines it is a 95% Wilson floor of 0.500 on n = 21, from one
  labeler, one store and one window, added after the labeling rather
  than registered before it. Rejected alternatives: shipping the flip
  on the charitable cut (the strict cut reads 0.381 on those same 21);
  scheduling a fourth labeling pass (pass #3 closed further passes on
  w2, and a pass on a rule that does not exist yet measures nothing);
  and arguing from interval overlap against the dropped w2, which is
  meaningless when the two rates share a numerator. Reopening means
  pre-registering n ≥ ~80 promotions and the interval criterion first:
  at this point estimate the Wilson floor clears 0.60 only at n ≈ 71.
  Once built it replays over history already on disk, so no new
  observation window is needed.
- **Branch coverage as the answer to the "a guard that cannot fail"
  class.** Rejected as the remedy, not as a tool. Branch coverage
  records arc traversal, not semantic correctness: a branch that is
  fully exercised and wrong is invisible to it by construction, because
  the arc was taken. That is the shape of both instances the class is
  named for — the
  [2026-07-26 constant-function verdict](incidents/2026-07-26-staleness-verdict-constant-function.md),
  where the flagging branch ran on every input and could therefore
  neither be wrong nor right, and the
  [2026-07-25 doctor false green](incidents/2026-07-25-doctor-false-green-on-importable-extra.md).
  Turn it on for its own reasons — dead paths, untested error legs — or
  not at all; what it cannot do is close this class, and adopting it as
  the answer would retire the class while leaving it open. No coverage
  figure is quoted here on purpose: line coverage on that defect was
  never measured, and the argument does not need it.
- **Managed cloud SKU.** Local-first is the design, not a missing
  feature.
- **Team-shared multi-user store / RBAC.** `sync` handles one user on
  many machines; many users on one store is a different product.
- **Knowledge-graph backend.** Typed links cover what retrieval needs;
  a graph store gives up the plain-markdown format.
- **Non-MCP SDK / REST endpoint.** Programmatic users can `import
  bettermemory` directly — see
  [examples/programmatic_client.py](../examples/programmatic_client.py).
- **Removing `verified_commits` / `verified_versions` within a major.**
  The compatibility contract forbids removing a parameter within a
  major; they are documented as audit-trail-only. 4.0, 5.0 and 6.0 all
  shipped without taking them — 6.0 spent its breaking budget elsewhere
  — so this is a 7.0 question at most.
- **Gating the low-use episode tools out of the lean surface.**
  Evaluated against the event log; not available — the shipped plugin
  skill, the system-prompt addendum, and the swarm fan-in path depend
  on them. Rationale at the episode block in `builder.py`; the
  per-turn cost was addressed by trimming `DESC_EPISODE_SEARCH`.
- **A "core" tool-surface preset — a third registration tier below the
  default lean surface.** Measured and closed. Every tool such a preset
  would drop is named by shipped guidance as a call the model is
  supposed to make, so a genuinely flow-complete core *is* the lean
  surface and saves nothing: `memory_show` is the rebase step both
  optimistic-concurrency stale hints hand back, `memory_remove` is the
  only action `memory_health`'s two largest recommendations offer and
  it is the one tool with no `bettermemory` CLI counterpart to fall
  back on, `memory_scope_disable` is instructed verbatim by the
  system-prompt addendum and the plugin skill (and
  `memory_scope_enable` is its documented undo), and `memory_list` sits
  in the addendum's tool headline and in `memory_audit_turn`'s
  retrieval-event set. Dropping all five anyway is 9% of the resident
  tool surface and breaks four of those. Meanwhile a schema-deferring
  client already pays under 1% by listing tool names and fetching
  schemas on demand — the same win, two orders of magnitude larger, for
  free — which is why the server's instructions block names the four
  tools to load first instead. Rationale next to the knob in
  `config.py`; the per-tool figures come from
  `tests/test_resident_footprint.py`, which measures them on every run.
- **Merging the micro-tool pairs within a major** —
  `memory_write_confirm` / `memory_write_cancel` and
  `memory_scope_enable` / `memory_scope_disable` into one call each.
  The compatibility contract forbids removing a tool within a major,
  and the economics are backwards without the removal: a merged
  replacement can only be *added* in a minor, so inside the line it
  would grow the description budget rather than shrink it. 4.0, 5.0
  and 6.0 all passed on it — 6.0 spent its breaking budget elsewhere —
  so it is a 7.0 question: deprecation cycle first, removal at the
  major with migration notes.

## Contributing

High-leverage contributions:

- Run `bettermemory eval` against your own usage and file anomalies.
  The silent-miss threshold rule is calibrated on one user's data;
  more distributions is the open question.
- Setup notes for MCP clients beyond the five in
  [clients.md](clients.md).
- Reports of stored memories that misled you in a way the verification
  surface did not catch — those locate exactly where the drift
  detection needs to widen.
