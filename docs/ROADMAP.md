# Roadmap

Planned work, in rough priority order. Plans change; the
[CHANGELOG](../CHANGELOG.md) is the source of truth for what shipped.

## Planned

- ~~**Truncation as a write-time gate, deferred on budget.**~~ **SHIPPED
  2026-08-04.** `memory_update` returns `status="truncation_warning"` when
  a body edit both shrinks the record and leaves it ending mid-sentence,
  with `acknowledge_truncation=True` as the override. The predicate is
  `models.looks_truncated`, unchanged — the same one `doctor`'s
  `memory_body_completeness` reports on.
  The entry is kept because HOW it shipped is the reusable part. It was
  deferred for two releases on description budget, and that blocker was
  real: 25,890 live against a 26,000 ceiling and a 25,900 warning, i.e.
  10 characters of margin. It was also UNDER-stated. This entry priced the
  edit at ~112–150; the only measured instance of the same shape — the
  user-claim gate's status clause (+120) and its `acknowledge_user_claim`
  escape (+21) — says 141, so even the low end overran the HARD ceiling
  rather than merely the warning.
  The unblock was reclamation, not a ratchet: `DESC_MEMORY_LINKS_TAIL`
  collapsed from 888 characters to a four-name type index, because
  everything else it said (REPLACE semantics — already verbatim on the
  bullet six lines above it — self-link rejection, and how links surface at
  retrieval) was already in `docs/api.md`. Net −471; the surface now sits
  481 under the warning. The schema half was never the constraint:
  `acknowledge_truncation` cost 60 against 371 of remainder headroom.
  Two things worth keeping for the next field-pin. **Look for a duplicated
  paragraph before proposing a ceiling bump** — the 888 had been sitting
  there through every previous budget squeeze. And **the shrink conjunct is
  what makes a 0.4%-false-positive predicate tolerable as a gate**: alone it
  would refuse every edit to a body that legitimately ends on a bare
  identifier, forever, including edits that only grew it.
  Rejected alternatives, recorded so they are not re-derived: a
  "new body is a strict prefix of the old" guard is 0% false positive but
  misses the incident that motivated this (it was a rewrite that got cut,
  not a prefix); "new body is >30% shorter" false-positives on condensing
  edits, the single most common update shape on the dogfood store.
  **One honest caveat on the 0.4%, found by landing it.** That figure was
  measured over stored bodies AT REST, and the gate judges a different
  population: shrinking EDITS. Nothing in the store measures that
  population, so the false-positive rate on it is unmeasured. The first
  evidence arrived immediately — three existing tests went red on the new
  gate, all of them terse unpunctuated fixture bodies that shrink
  (`"zsh"`, `"rewritten body"`, `"kubernetes ingress nginx tls
  termination"`). Two were fixture noise and took a full stop; the third
  (`test_memory_update_can_take_a_body_below_the_floor`) now passes
  `acknowledge_truncation=True` and is the better pin for it, since the
  floor exemption and this gate are the same claim from two sides — a
  deliberate shortening is allowed, and a shortening has to be deliberate.
  Real bodies end on punctuation 233 times in 234, so the production
  surface is expected to be small; it is not zero, and "0.4%" should not
  be quoted about this gate without that qualification. This is the
  verifier-defines-its-own-input-population shape from
  `docs/incidents/`, caught early enough to be a caveat rather than
  an entry there.
- **Write-path hardening, remaining items.** `apply_write_gates` is the
  shared gate chain and `memory_verify` refuses unverifiable path
  attestations (both shipped in 3.31.0). Remaining:
  1. Reconcile the private gate copies against the shared chain.
     `handlers/proposals.accept_proposal` now calls `apply_write_gates`;
     `consolidate._apply_llm_proposal` still hand-rolls size, transient
     and similarity, each with a deliberate rationale (it gates the LLM
     claim, not the stamped body), so that half is policy review rather
     than a mechanical reroute. `ingest.apply_ingest_plan` is the third:
     it bypasses `_validate_write_payload` entirely. The `[scopes]
     allowed` half of that residue is now closed —
     `ingest._scope_allowlist_reason` runs beside the gate loop in
     `apply_ingest_plan`, which stays the enforcement boundary because the
     library entry point is reachable without the CLI, and it flips an
     offending row to `skip_invalid` rather than aborting the batch.
     `compute_ingest_plan` runs the same predicate when it is given a
     `Config`, so `--dry-run` predicts the commit instead of contradicting
     it. Only scopes the CALLER supplied are checked: the provenance and
     type-derived scopes ingest stamps itself are exempt, because
     enforcing a user-scope policy against scopes the tool chose refused
     every row for anyone with a whitelist. What is left of the residue is
     the three caps in the same `_validate_write_payload` that no gate
     reads either: `max_content_bytes`, `min_content_tokens`,
     `max_scopes_per_write`. Those are unenforced at BOTH phases, which
     is what makes this half unlike the `[scopes] allowed` half:
     `compute_ingest_plan` never checks them and `apply_ingest_plan`
     builds its `Store.write` payload by hand, so the plan and the commit
     agree exactly and neither one refuses. Measured 2026-08-02 against a
     throwaway store with all three set tight — `max_content_bytes` 200,
     `min_content_tokens` 50, `max_scopes_per_write` 1: a 3,098-byte body
     and a 3-token body, two scopes on each, every row planned as `write`
     and every row committed. So there is no `--dry-run` over-promise to
     fix; the residue is that ingest lands rows `memory_write` would
     refuse, and closing it means adding the predicate on both sides at
     once rather than reconciling a divergence. Two earlier wordings
     called the caps "apply-time-only" — introduced in f281c39, carried
     through 964baad. That described `_validate_write_payload`'s position
     on the `memory_write` path, never anything ingest does;
     `apply_ingest_plan`'s docstring, written in that same f281c39, has
     said the caps are "still missed on this path" correctly all along.
     The two halves of one commit disagreed from the day they landed.
     `memory_update` is the fourth, and it grew rather than shrank: it now
     mirrors two gates by hand — the credential scan, and the user-claim
     gate, added when write-then-update turned out to launder exactly the
     body `memory_write` refuses. Routing it through `apply_write_gates`
     is not mechanical either: `find_similar` takes no exclusion id, so
     the dedup gates would score an edited body against the record's own
     stored copy and report it as its own duplicate.
  2. Provenance on the read surface, after a design change: a tier
     derived from local write events would label an injection-driven
     write `locally-written` — its cleanest tier — so it cannot see the
     reachable attack. Prefer recording what source material was in
     context at write time; `groundedness_check` / `source_transcript`
     are the existing seed.
  3. `sync pull` trust boundary. `sync.py` pulls and re-indexes with no
     content validation, and `SECURITY.md` does not name sync as
     attacker-reachable. The one genuinely remote path.
- **Claims-at-write.** Promoted above "Standing tier" and "Event-time"
  on 2026-07-31: it is the only measured route to a drift signal cheaper
  than the one shipped, and the shipped one is expensive. A real-prose
  claim extractor is an open problem only because extraction is post-hoc;
  the author of a memory knows what it is claiming at write time.
  Structured claims on the write/verify surface (the shape
  `verified_paths` already has) would give `build_binding_index` real
  input — which the claim-level `weak` drift tier needs before it can
  ship. On the 30-repository corpus
  (`bench/rot/results/multirepo-anchored-2026-07-30.json`, 37,635 claims)
  `weak` costs **1.1 alerts per catch at 94% precision** against the
  shipped verdict's **3.4**. Quote those corpus figures, not the
  superseded single-repo pilot's 25.1 → 2.0.
  What makes it the priority is the incumbent's own measurement on that
  corpus: J = 0.2875, 77.8% of claims flagged, 29.5% precision, 3.4
  alerts per catch, against `always_flag`'s J = 0.000, 22.9% precision
  and 4.4 — a real signal, and a weak one. On this repository the leg is
  saturated: measured 2026-07-31 against the dogfood store from this
  checkout, **68 of the 74 memories whose commit leg can speak carry
  commits since their last verify (92%)** — 29 of the 31 verified in the
  2026-07-24 clear are firing again a week later, and 19 of the 20
  verified that same day were already firing. The answer to that cost is
  a replacement measured first, not a subtraction; see
  `_COMMIT_DRIFT_ESCALATES` under "Not planned". Backfill is one curation
  pass over the ~143 checkable live bodies.
- **Standing tier.** Opt-in retrieval cannot serve knowledge whose
  trigger condition is not knowing you need it. The `ambient` category
  is still retrieval-gated and `memory_scope_overview` returns counts
  only, so nothing in the product delivers unconditionally. Candidate:
  a hard-budgeted tier delivered at session start — the delivery shape
  `episode_handoff` already uses — under the same verification
  discipline as the rest of the store. Prior art: Letta's core-memory
  blocks; the differentiator is the budget and the verification.
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
- **Relevance-label v2 default flip — w2 dropped; next candidate
  needs a rule-signature change.** The measurement half shipped in
  3.14.0, and three hand-labeling passes over live turns have now
  scored the widening candidates against a ≥~70% precision gate.
  `w1_top1_v2_high` (the bare matched-token floor) was ruled out at
  ~15–30%. `w2_top1_v2_high_from_medium` (promote medium→high only)
  held ~48–54% across three independent windows — ~51% combined over
  79 labeled promotions — and is **dropped as the flip candidate**
  per the recorded band rule. Both stay in `WIDENING_RULES` as
  preview-only baselines; the live label and the shadow contract are
  unchanged. Passes:
  [2026-07-08](eval/widening-labeling-2026-07-08.md) ·
  [2026-07-22](eval/widening-labeling-2026-07-22.md) ·
  [2026-07-29](eval/widening-labeling-2026-07-29.md).

  The successor is now measured, and the program is closed rather than
  continued — on a criterion that was added after the numbers were in,
  which is recorded plainly here because the alternative is claiming a
  gate did the work it did not do. That successor is w2 **minus flags
  whose top hit the same session had just written or updated** —
  content already in context, so an impossible retrieval win. Excluding
  them lifts the charitable read to 15/21 = **0.714**, and the
  pre-registered gate was ≥~70%: **the point estimate met the gate as
  written.** Declining the build rests on a second criterion the
  registration never named — the interval rather than the point. On
  n = 21 the 95% Wilson interval is **[0.500, 0.862]**, a floor at
  coin-flip, from one labeler, one store and one window; the pass says
  as much in its own words — "that single exclusion is doing all the
  work" — and strict precision on the same 21 reads 0.381. Adding the
  floor is defensible on exactly those grounds; it is a new criterion
  all the same, and pass #3 did not gate the build on an interval
  either — it recorded the rule-signature change as "the blocking next
  step". What pass #3 did close is further labeling **on w2**, so no
  fourth read arrives on its own; the live decision is therefore the
  build, and it is **not planned** (below).
  One argument that looks available and is not: the refined 15/21 and
  the dropped w2's 15/31 share a numerator, so their intervals overlap
  by construction and comparing them says nothing. The after-the-fact
  floor carries this decision alone.
  If it is ever revisited, pre-register the sample size **and the
  interval criterion** before labeling: at this point estimate the
  Wilson floor clears 0.60 only at n ≈ 71, so n ≥ ~80 promotions.
  Note for whoever builds it — the
  exclusion is **not** a session-id join: mutation events carry the MCP
  server session, `turn_audited` carries the client session UUID, and
  the namespaces do not map. Once it exists it replays over history
  already on disk, so no new observation window is needed.

### Small and anchored

Each of these is scoped and has a known landing site; none is large
enough to earn its own entry above.

- **The transient-reject hint never names `episode_write`.** The hint in
  `handlers/write.py` offers two remedies — rephrase to the durable form,
  or `acknowledge_transient=True` — and does not mention the tier that
  exists to hold exactly that content. The reverse direction is already
  covered in `handlers/episode_write.py`, so the gap is one-way. No test
  pins the string and reject hints are runtime payloads, so this costs
  nothing against the resident budget.
- **`SYSTEM_PROMPT_ADDENDUM` predates the episodes-as-state-channel
  rewrite.** The block in `prompts.py` is mirrored byte-for-byte in
  `docs/system_prompt.md` and pinned equal by `tests/test_prompts.py`. It
  still describes episodes as a place you *may* use, and its loop-iteration
  pattern predates both the cheap-scan parameters and the minting moment
  that `plugin/skills/bettermemory/SKILL.md` now leads with. Target
  net-neutral, not net-add: `docs/system_prompt.md` warns that Claude Code
  truncates the paste at ~1.8 KB.
- **`cold_endorsement_memories` is not gated by the telemetry-coverage
  predicate, and that is a decision, not an implementation.** Only
  `dead_weight` is gated in `health.py`. On a hookless store the
  cold-endorsement leg over-fires exactly where the gate exists to stop
  over-firing, and it reaches the model through the curation hint's
  pressure sum — the one curation surface delivered without asking. Two
  viable shapes: widen the coverage gate to cover the leg, or drop the leg
  from the pressure sum when coverage is absent. Hard constraint: the
  `curation_pending` key set is pinned by set-equality, dict-equality and a
  DESC-prose regex, so the fix must not add a key.
- **`doctor`'s `turn_audited` count does not check `triggered_from`.** An
  in-process `memory_audit_turn` stamps `triggered_from="mcp_tool"`, so an
  MCP-only store reads as "hook is wired" — the conflation fixed everywhere
  else. Three verdict branches and a published info key read the counter,
  so tightening it changes what `doctor` says on stores that are genuinely
  fine but MCP-driven.
- **`bench/retrieval/README.md`'s "What this does not measure" is short
  two structural caveats.** Both are properties of the corpus, so they
  bound every published cell rather than any single one, and both were
  derived while auditing something else — recorded here so they are not
  re-derived. (i) **The recency knob is out of scope by construction.**
  The corpus is written in a single pass, so `_recency_factor` in
  `src/bettermemory/search.py` (`src/bettermemory/search.py:1255-1261`)
  — the one ranking knob live by default, applied in three scorers and
  configured by `recency_boost_half_life_days` in
  `src/bettermemory/config.py` (`src/bettermemory/config.py:352`) —
  sees ages that differ by microseconds across the whole store. Every
  published number therefore describes ranking with that factor held
  flat. (ii) **The corpus cannot exercise `auto_scope`.** `build_store`
  (`bench/retrieval/run.py:213-221`) writes every memory with no
  `Origin`, and `should_include_for_caller`
  (`src/bettermemory/origin.py:449-472`) treats a null memory origin as
  global, so every published recall figure describes a store where
  scope filtering structurally cannot bite. Landing site is that
  README's existing list.
- **`episode_search(ids=…)` has no by-filename fast path, and the win is
  unmeasured.** The refusal is written into `handlers/episode_search.py`
  with its reasoning, and the validator to mirror already exists in
  `episodes.py`. Two things to know first: on a bare `ids`-only call the
  candidate loop still walks every session, so the saving is proportional
  to session count rather than to 1; and three behaviours ride the
  post-load loop (the floor skip, the `since`/scope filters, the datetime
  sort) that any short-circuit must preserve. Bench before building.

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
  reversed. Reopening needs a replacement measured first, which is
  "Claims-at-write" above. Write-up in
  [bench/rot/README.md](../bench/rot/README.md); artifact
  `bench/rot/results/escalation-off-60d-2026-07-31.json`.
- **A per-memory mutation index for the relevance-label widening
  program.** The `write`/`update` event stream plumbed through both
  widening lanes, which is the only implementable form of the surviving
  candidate — `ThresholdRule.check` is a pure per-turn predicate with no
  access to event history, so the exclusion cannot be a registry entry
  the way `w2` was. The candidate does not earn the build — and it
  cleared the registered gate to get here, which the refusal has to own:
  15/21 = 0.714 meets ≥~70% on the point estimate, and what declines it
  is a 95% Wilson floor of 0.500 on n = 21, from one labeler, one store
  and one window, added after the labeling rather than registered before
  it. Rejected alternatives: shipping the flip on the charitable cut
  (the strict cut reads 0.381 on those same 21); scheduling a fourth
  labeling pass (pass #3 closed further passes on w2, and a pass on a
  rule that does not exist yet measures nothing); and arguing from
  interval overlap against the
  dropped w2, which is meaningless when the two rates share a
  numerator. Reopening means pre-registering n ≥ ~80 promotions first.
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
- **Removing `verified_commits` / `verified_versions` in 3.x.** The
  compatibility contract forbids removing a parameter within a major;
  they are documented as audit-trail-only. A 4.0 question at most.
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
- **Merging the micro-tool pairs in 3.x** — `memory_write_confirm` /
  `memory_write_cancel` and `memory_scope_enable` /
  `memory_scope_disable` into one call each. The compatibility contract
  forbids removing a tool within a major, and the economics are
  backwards without the removal: a merged replacement can only be
  *added* in a minor, so inside 3.x it would grow the description
  budget rather than shrink it. A 4.0 question — deprecation cycle
  first, removal at the major with migration notes.

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
