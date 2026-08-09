# Changelog

Notable changes between releases. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely. From 1.0
onward the project uses semver in the standard way: major bumps for
breaking changes, minor for additive features, patch for fixes. The
[compatibility contract](CONTRIBUTING.md#versioning-and-the-compatibility-contract)
spells out exactly what's stable.

## 5.0.0 - 2026-08-09

### Removed — the web UI, whole

An owner decision, same register as 4.0.0's: the store is markdown
files plus your normal tools, and a bundled dashboard is surface area,
not product. Removed rather than parked (`66e302f`):

- The `web` module and every route it served, the `bettermemory ui`
  subcommand (`--tunnel` included), and the `[ui]` extra — `fastapi`
  and `uvicorn` leave the dependency tree entirely. There are no
  runtime extras left.
- The web-only test surface and the CI installs that existed to
  type-check it; the matrix now syncs `--extra dev` alone.

Curation happens where the memories live: in-conversation via the MCP
tools, `bettermemory health` / `bettermemory consolidate` on the CLI,
or any editor pointed at the markdown. Internals that existed to keep
the page honest survive where they earn per-turn keep — the shared
ranking-input shape (`resolve_ranking_inputs`) stays so the next
ranking surface starts threaded instead of drifted, and the 500-char
`note` cap outlives the form it originally matched.

### Migration

- `bettermemory ui` now exits with argparse's unknown-command error;
  there is nothing to configure away. An installed `bettermemory[ui]`
  spec should drop the bracket. `fastapi` / `uvicorn` disappear from
  the lock on the next sync.

### Changed — the README stops carrying numbers

The front page's "What it does" wall quoted dated measurements
(a serialized tool-surface byte count measured at 3.32.0, benchmark
precision figures with their artifact ceremony) that rot faster than
anyone re-measures them — outdated the day after they were written
down, and wrong-by-staleness on a project whose whole thesis is that
stale claims get flagged. The README now states what the product does
in timeless terms and carries ZERO measurements; the evidence lives in
`bench/` and the docs, beside its dates and caveats, and the
number-claims floor test enforces the new contract (a measurement
appearing in README.md is a regression).

## 4.0.0 - 2026-08-09

### Removed — the embedding lane, whole

An owner decision, recorded as such: bettermemory is a purist product —
**the code is the model.** No embedding models, no borrowed pretrained
weights, no third-party neural components anywhere. This release removes
the entire embedding lane rather than disabling it:

- The `[embeddings]` (sentence-transformers/PyTorch) and
  `[embeddings-fast]` (fastembed/ONNX) optional extras, the `semantic`
  and `semantic_setup` modules behind them, and the
  `[behavior] semantic_provider` knob that arbitrated between providers.
- The hybrid ranker's third leg. `hybrid` is RRF fusion of keyword +
  BM25, full stop; every ranker is deterministic lexical code.
- The `"semantic"` search mode. A per-call `mode="semantic"` raises
  `unknown search mode` like any other bad value.
- `[behavior] semantic_dedup` and the cosine write-dedup path. Dedup is
  Jaccard on stopword-stripped, kebab-expanded token sets at every
  entry point (0.75/0.40 manual, 0.90 unattended auto-consolidate).
- The persistent embedding cache (`.embeddings.*.npz`), its flush/hydrate
  machinery, and `reindex --embeddings`.
- `doctor`'s embeddings checks and install hints, including the
  recall-lift fix hint.
- The two extras CI legs (`test-embeddings`, `test-embeddings-fast`) —
  the embeddings jobs were also the matrix's only model-download legs,
  the class behind the 3.43.0 release run's one-hour stall.
- The bench semantic arms: both runners now refuse a requested
  `semantic` arm with an explicit note instead of measuring a lane the
  product no longer ships. The semantic arms' historical results stay in
  the bench READMEs as dated record.

### Migration

- A config file still saying `search_mode = "semantic"` is normalised to
  `hybrid` at load with one loud warning — delete the line to silence
  it. (A page or server must not go down over a stale config line; an
  explicit per-call ask must not silently rank with a different
  algorithm — so the per-call form raises instead.)
- A leftover `semantic_dedup` / `semantic_provider` line is ignored;
  delete it.
- Install specs referencing the removed extras
  (`bettermemory[embeddings]`, `bettermemory[embeddings-fast]`) should
  drop the bracket. Leftover `.embeddings.*.npz` cache files in a store
  are inert and safe to delete; `sync` already excluded them from
  shared history.

### Changed — the comparative claims, restated without the tie

The removed leg measured real recall, and the record keeps saying so:
the retrieval bench's semantic arm beat lexical by 25 points at recall@1
(60% vs 35% as-asked), and on LongMemEval the pre-4.0
best-arm-vs-best-arm framing read as parity with claude-mem (91.8% vs
91.6% macro recall@5). That tie framing is retired with the lane.
Restated honestly: **bettermemory retrieves with deterministic lexical
code only and scores 89.3% macro recall@5 on LongMemEval; claude-mem's
embedding-native stack scores 91.6%. We are 2.3 points behind, by our
own harness.** Closing that gap in code — no borrowed weights — is the
standing retrieval campaign, and no current claim should be read as it
having closed. `docs/api.md`, `docs/internals.md`,
`docs/installation.md`, and both bench READMEs carry the migration
notes and the dated restatement; incident reports keep their citations
of the removed modules explicitly marked non-resolving at HEAD.

## 3.43.0 - 2026-08-08

### Fixed — the whole-tree audit: thirty-five findings drained

A maintainer-mandated whole-tree audit read every tracked source, doc,
bench, and CI file end-to-end through thirteen domain lenses, with
every candidate finding independently re-derived before it counted.
Thirty-four survived, two severe, and this release drains all of them
plus one carried side-finding. The severe pair sat in the product's
two youngest lanes:

- **`memory_search`'s expand-top block computed commit drift without
  the memory's declared claims** — the pre-claims any-touch policy —
  so the loudest retrieval surface could report a different
  `staleness_verdict` than `memory_show` for the same memory in the
  same turn. The web detail page and `_response`'s per-hit attach
  carried the same divergence; all four surfaces now share one
  claim-narrowed policy.
- **`episode_handoff`'s prior-session auto-resolution admitted
  hook phantoms** — transcript-id sessions written by the client-side
  hooks,
  worktree-matching candidates that can never hold episodes — burying
  the real predecessor's takeaway behind a zero-episode ghost. The
  walk now applies the same `_OUT_OF_PROCESS_TRIGGERS` skip the
  session bridge uses.

The rest, by area: `memory_verify`'s optimistic-concurrency check
fingerprints the (`last_verified_at`, `updated`) pair, closing the
None-equals-None pass that let a concurrent edit get certified unread,
and `$HOME`-spelled attestations are recognized as absolute. The
retrieval shield matches the union of the anchored server session and
the caller's own id, so a prompt-recall delivery outside a git
checkout stops re-flagging its own turn; the recall block's cap now
bounds the scope list its comment always claimed to guard. The index's
first-touch schema stamp serialises on the migration branch's flock,
ending a two-process IntegrityError cascade that could unlink a
healthy index; the event recorder heals a crash-torn tail so the next
fsynced event stays readable. BM25 corpus statistics cover the kebab
parts the conjunctive fallback prices under the prefilter. Body edits
now run the transient gate `memory_write` always had (the last
laundering route into a committed record), with ordered pendings
putting the user-inference veto ahead of global confirmation.
Truncation overrides are recorded only when the gate fired, and literal-claim
equality is order-insensitive for sets and dicts. Conflict verdicts
claim their queue row under the flock before touching links, so a
lost race mutates nothing. Doctor bounds its `Name:` search to the
metadata header, tolerates a non-UTF8 client config as that file's
finding, and health renders `no_signal` audits distinguishably from a
hook that never fired. The web UI rejects untrusted Host headers on
loopback binds (DNS rebinding reached every read route) and its
detail page agrees with `memory_show` on claim-carrying memories.
The `bettermemory ui` fallback hint composes from the install-hint
atoms with the tool form leading, and the cli help texts tell the
truth about the migrate fallback chain and the config cache that
never existed, and a new ratchet key pins the
bare-pip spelling extinct. Commit-lint skips manual dispatch runs
instead of green-lighting an empty range.

### Changed — the benchmarks describe this engine again

The published retrieval and LongMemEval artifacts measured 3.29.0 and
3.30.0. Both suites re-ran unchanged at this release's engine: the
retrieval headline reproduces (the semantic margin at recall@1 holds
at the published size, control still tracks asked, and the prefilter
still costs nothing at recall@5 in any cell), and LongMemEval comes
back bit-for-bit identical on both arms at every k — the identity
doubling as a harness determinism check. Both runners now stamp a
`provenance` block (version, commit, dirty flag, date, machine) on
every artifact, restoring what the retrieval runner's rewrite dropped;
an artifact that cannot say what it measured is the failure both
benchmarks exist to end. Dated sections in `bench/retrieval/README.md`
and `bench/longmemeval/README.md` carry the fresh tables; the
claude-mem comparison arm stays its dated 2026-07-27 artifact rather
than pretending a live re-run that tooling no longer supports.

### Docs

Six prose surfaces stopped contradicting shipped state, including the
LongMemEval README declaring its own published comparison unearned,
CONTRIBUTING's opt-in value predating the shipped recall lanes, two
"unreleased" fix-tracking lines for fixes that shipped in v3.26.0 and
v3.31.0, and `docs/api.md` documenting the verify oracle's
caller-checkout fallback for legacy origins.

## 3.42.0 - 2026-08-06

### Added — the standing tier: fresh-verified ambient bodies at session start

Opt-in retrieval cannot serve knowledge whose trigger condition is not
knowing you need it. The 3.41.0 prompt-recall hook delivers
conditionally — query-matched, score-gated, on the ~2% of prompts the
silent-miss probe flags — which serves "you asked about something you
forgot is stored" and still cannot serve "you didn't ask". The standing
tier (`[behavior] standing_tier`, **default off**) is the unconditional
lane: `bettermemory session-start` now appends the caller-scoped
`ambient` memories whose staleness verdict computes fresh to the
SessionStart hint — whole bodies, not pointers, because a pointer still
requires the model to know to dereference it. Prior art is Letta's
core-memory blocks; the differentiator is the budget and the
verification.

The tier ships on the five decisions the ROADMAP settled build-ready
(2026-08-05), unchanged by implementation:

- **The cohort is the existing `ambient` category, not a new flag.**
  The category's docstring ("atmospheric context that shapes replies
  without being cited") is already the tier's definition, and ambient
  is already excluded from dead-weight curation. Scope matching reuses
  the session hint's `candidate_admitted` predicate, so what the tier
  delivers is provably a subset of what `memory_search` would admit.
- **Verification is the admission ticket.** Each candidate runs the
  same chain `memory_show` computes — calendar verification,
  claim-anchored path drift, commit drift against the caller's
  checkout — at the gate, no relaxed session-start variant. Anything
  not fresh is never delivered; it collapses into one aggregate
  "N standing memories are stale — verify to restore delivery" line,
  which converts the verification debt the tier would otherwise ship
  into visible pressure to pay it. A calendar-fresh memory whose
  attested path vanished is caught by the drift leg and held back
  (pinned by test).
- **Hard byte budget, whole-memory truncation only.** Entries walk
  newest-verified first under a 1 KB ceiling. A body over the entire
  budget is skipped whole — never trimmed, a truncated fact is a
  different fact — so it cannot head-of-line-block smaller memories
  behind it; a body that merely doesn't fit the remaining budget stops
  the walk, because delivering an older body after declining a newer
  one would invert the priority order. Both land in the "…and K more
  (`memory_list`)" overflow count.
- **Default OFF at introduction.** The recall hook's default-on was
  earned by a measured 2% firing bar; this tier fires on every session
  open for whoever holds ambient memories, and no equivalent
  measurement exists yet. It flips only with dogfood evidence.
- **The session-start negative mandate stays intact, and the cost is
  named.** Delivery records nothing — no event, no session — so v1
  adoption is deliberately unmeasured; the two instrumentation shapes
  considered and rejected (a `standing_delivered` event, retroactive
  stamping from the first Stop hook) are recorded in the ROADMAP entry
  with the census-corruption argument that killed them.
  `test_standing_tier_records_nothing` enforces the mandate on the
  flag-on path with a real delivery.

The read stays index-first: the new `index.category_rows` names which
files hold admitted ambient rows (same never-raises degrade contract as
`scope_counts`) and only those files are parsed — the flag does not buy
a `load_all`. The parse re-checks category and admission against the
parsed truth, because the index-trust gates establish file identity,
not file content. A failure anywhere in the standing computation
degrades to a stderr note and the base hint ships without the section;
with the flag off, the printed block is byte-identical to the pre-tier
surface, also pinned by test.

## 3.41.0 - 2026-08-05

### Added — score-gated recall at prompt time: the probe's answer, delivered instead of filed

For as long as the Stop hook has existed, the product has computed the
answer to "did this turn need memory?" on every turn — production's
search pool, production's ranking inputs, the versioned threshold rule,
all four shields — and written it to the event log at the one moment it
could no longer help. The new UserPromptSubmit hook (`bettermemory
prompt-recall`) runs the SAME probe before the turn starts:
`hook._probe_message` is shared code, not a parallel implementation, so
the recall path fires exactly where the audit would otherwise have
flagged a `search_miss`. On a would-be miss it prints a one-hit pointer
block — memory id, scopes, the query-biased snippet that cleared the
bar, and the verify-first instruction — which Claude Code injects into
the model's context, and records a `prompt_recall` event carrying the
same replayable `MissHit` shapes as `search_miss` plus `probe_mode` and
`injected_chars`.

The founding "don't pollute generic answers" stance is carried by the
bar, not by opt-in — those two were conflated. The predicate fired on
3 of 128 audited turns on the dogfood store
(docs/eval-results.md, the 2026-08-04 snapshot; a this-store
measurement, not a promise), so ~98% of prompts inject nothing and the
flagged few get a pointer, never a body: the staleness read path
(verdicts, path drift, claim drift) stays on `memory_show`, which the
injected block instructs the model to call before relying — the one
delivery in the product that bypasses the tool surface still cannot
bypass verification.

Three interlocking decisions keep the telemetry honest, spelled out in
docs/ROADMAP.md so they are not re-litigated: `prompt_recall` joins the
retrieval-event set (a delivered pointer is not a SILENT miss, so the
Stop audit reports `ok`, and a second injection self-suppresses for the
attribution window — the anti-spam bound is the existing 600s constant,
not a new knob); `telemetry.enabled = false` refuses to inject rather
than deliver off the books (an unlogged injection would re-flag its own
turn and be unmeasurable); and the hook stamps
`triggered_from="prompt_hook"`, with `hook._latest_in_process_session`
widened to skip the full out-of-process set — a recall row admitted as
the server-session anchor would have handed the shield a transcript id
and silently killed it. The miss lane's denominator semantics change
with this release and are named in docs/eval.md and
docs/eval-results.md before the first affected snapshot, the same
discipline as the 2026-07-22 cutoff. `[behavior] prompt_recall = false`
restores purely opt-in retrieval exactly.

### Fixed — the claim oracle reaches legacy no-worktree memories from a matching checkout

The 3.40.0 backfill hit 8 of 128 repo-matched memories whose origin
carries `repo` but no `worktree_root` — records written before the
field existed. The verify-side claim oracle refused all 8 for the
missing tree, permanently: nothing rewrites a stored origin, so a
legacy record could never carry claims at all. `memory_verify` now
falls back to the CALLER's checkout when its `repo` matches the
memory's origin repo; the write-side oracle is untouched, and the
attestation existence check and the advisory symbol check inherit the
same root on the same argument.

### Changed — the pending-token dedup parses the tail it examines, not the log

`_already_recorded_pending_ids` documented a backward early-exit and
paid an O(N) toll before reaching it: `list(iter_events(root))` and the
`_stop_hook_session_ids` pre-pass both parsed the entire active log on
every turn that held pending tokens — 14.0ms of a 15.6ms call was that
parse, while the backward loop then examined exactly one event
(measured on the round-179 dogfood log; the numbers are in the
function's own docstring). The new `events.iter_events_backward` reads
each active segment's bytes once, splits lines without parsing, and
JSON-parses lazily as the newest-first merge yields — so the demolition
demonstration (8,003-line synthetic log, three fresh tokens) went from
8,003 parses to 4, with an identical result set. The session bridge
that justified the pre-pass is now derived per-event: every hook-written
`use` row carries `triggered_from="stop_hook"` itself, proven by
enumerating all four `use` producers in `src/` (the CLI acknowledge-debt
writer is untagged and was never bridged under either shape), and the
producer-side stamp is now pinned in both attribution shapes' tests so
the equivalence cannot silently rot. Two parse-count ratchets join the
round-181 iteration ratchet — the property "examined ≈ parsed" is what
the fix adds, so that is what the tests count; removing the early-exit
takes the demonstration from 4 parses back to thousands and both go
red. Bounded by the 10MB rotation cap before and after — this was a
constant-factor tax on every turn, not a leak, and it is now paid only
on the tail.

### Changed — one spelling for the extras install commands

`doctor._install_extra_command` / `_reinstall_extra_command`, the
search handler's semantic-mode-unavailable message, and `semantic.py`'s
provider-unavailable warning each hand-spelled the same install
commands — three copies of the drift that produced the 2026-08-01
false-green's misleading remedy. The spellings now live once in
`_install_hints.py` (import-free; `semantic.py` takes it as a lazy
import inside the failure branch, keeping the hot path clean), doctor's
names re-export it, and every composed message was verified
byte-identical to its predecessor before landing. A shrink-only ratchet
pins each command literal's per-file occurrence count over `src/`, so
the fourth copy fails in CI rather than in a postmortem; the known
hand-spelled `[ui]`/`[dev]` variants in `web.py`, `llm.py`, and
`cli/ui.py` are frozen as recorded debt in that same ratchet.

## 3.40.0 - 2026-08-04

### Added — claims-at-write: declared claims, checked at declaration, watched by the drift leg

A `claims` list on `memory_write` and `memory_verify` — `path`,
`path::symbol`, or `path::NAME=literal` strings, the three kinds the rot
benchmark measured. The premise from `docs/ROADMAP.md`, unchanged by
contact with implementation: a real-prose claim extractor is an open
problem only because extraction is post-hoc, and the author of a memory
knows what it claims at the moment of writing. So the product asks, and
the extractor is nobody's problem.

Declaration is verification. `claims.check_claim` — the bench oracle
`label_claim`, promoted verbatim: existence, a top-level AST lookup, a
`repr`-space literal comparison, never an inference — runs at declare
time and REFUSES a claim that is false right now, naming what the tree
actually says. No `acknowledge_*` escape exists, deliberately: the read
side's trust in claims is that every stored claim held at declaration.
`memory_verify` re-runs the oracle over STORED claims before stamping,
so a stored claim the tree now contradicts blocks the stamp
(`claims=[]` is the audited clear-and-stamp escape; body edits clear
claims alongside `last_verified_at`, and `memory_verify(id,
claims=[...])` is the backfill surface for pre-3.40 memories).

The payoff is on the read side. For a memory that declares claims, the
commit-drift leg splits its anchors: claim-governed files escalate the
staleness verdict only for commits the claim-level `weak` tier
implicates (`claims.build_binding_index` over a `--no-walk -U0` patch
stream of the post-verify window, column-0 binding match), while
unclaimed cited files keep the incumbent any-touch rule; the halves
union on commit identity, and a measured zero still demotes a
calendar-stale verdict. Method-body churn in a claimed file now reads
`clean` with `claim_drift: {checked, drifted: []}` attached — the
commit-drift block and drifting search hits both carry the new
sub-dict, and a fired claim is named in the recommendation. On the
30-repository corpus the weak tier costs 1.1 alerts per genuine catch
at 94% precision against the per-file incumbent's 3.4
(`bench/rot/results/multirepo-anchored-2026-07-30.json`) — the
"replacement measured first" that the `_COMMIT_DRIFT_ESCALATES`
retraction demanded, landed as upstream narrowing with the switch
untouched. Those figures grade the detector on extracted corpus claims;
the live population (author-declared, oracle-gated) is cleaner by
construction and unmeasured until the dogfood backfill mints a
denominator — recorded in the roadmap entry before the first telemetry,
not after.

`bench/rot/run.py` now imports the promoted detector from
`bettermemory.claims` — the bench measures the shipped functions, per
its own "the shipped function, not a reimplementation" rule, and its 42
tests pass unchanged against the promoted copies. All four commit-drift
surfaces (`memory_show`, the `memory_search` fold, both `memory_health`
rollups) route claims through the one shared core; `claims` sits on
`resolve_commit_drift_count`'s own signature so a count-only surface
cannot compute a different policy than the display surfaces. Tool
descriptions grew a deliberately terse bullet each (+438 total,
25,857 against the 26,000 ceiling): the declare-time gate's refusal
teaches the full syntax per-defect at the only moment it is
actionable, the same hint-carries-the-remedy split as the E1 cuts.

## 3.39.0 - 2026-08-04

### Fixed — the three `[behavior]` write caps now bind the ingest path

`max_content_bytes`, `min_content_tokens`, and `max_scopes_per_write` were
unenforced at BOTH ingest phases: `compute_ingest_plan` never checked them and
`apply_ingest_plan` builds its `Store.write` payload by hand, so the plan and
the commit agreed exactly — on landing rows `memory_write` refuses. Measured
2026-08-02 with all three set tight (200 bytes / 50 tokens / 1 scope): a
3,098-byte body and a 3-token body, two caller scopes on each, every row
planned as `write` and every row committed. Unlike the `[scopes] allowed` gap
this closes alongside, there was no `--dry-run` over-promise window to repeat,
which is why the fix is one predicate added to both sides at once
(`_write_caps_reason`) rather than a reconciliation.

The predicate calls the shared validators from `handlers/_shared.py` in
`_validate_write_payload`'s own order — floor, size, count, ahead of the
allowlist, ahead of the gates — so a row that breaks several caps reports the
same sentence `memory_write` would have raised first, and the message cannot
drift from `memory_write`'s because it is `memory_write`'s. Two decisions
travel with it. The scope-count cap counts caller-supplied scopes only: ingest
stamps a provenance scope plus a type tag on every row, so counting them would
let `max_scopes_per_write = 1` refuse every import including one with no
`--scope` at all — the same broke-every-allowlist-user regression, re-armed as
arithmetic. And `config=None` means the SHIPPED cap defaults, not caps-off:
the allowlist's unset value is a no-op, the byte and scope caps' unset values
are not, so treating absence as absence would enforce different caps on the
plan and the commit. Refusals stay per-row (`skip_invalid`), never a batch
abort, and surface in `render_ingest_text` beside every other skip reason.

## 3.38.0 - 2026-08-04

### Added — `memory_update` refuses a body edit that shrinks and ends mid-sentence

`looks_truncated` had been detection-only: `doctor`'s `memory_body_completeness`
names bodies whose last non-whitespace character is not sentence- or
structure-terminal, after the tail is already gone and with no older copy to
recover it from. One body sat cut mid-word for ten days with every check green.
`memory_update` is the only surface holding both the old body and the new one,
so it now returns `status="truncation_warning"` when an edit both shortens the
record and leaves it ending mid-sentence, with `acknowledge_truncation=True` as
the override. Nothing persists on the refusal; the event log records the two
lengths and never the body text, since a truncated body is as likely to carry a
secret as any other.

The shrink conjunct is what makes a 0.4%-false-positive predicate usable as a
gate rather than a report: alone it would refuse every edit to a body that
legitimately ends on a bare identifier or a list item, including edits that only
grew it. Both directions are pinned by tests that fail under deletion of the
clause they name. One caveat travels with the rate, recorded in
`docs/ROADMAP.md`: 0.4% was measured over stored bodies at rest, and the gate
judges shrinking edits — a population nothing has measured. Three existing tests
went red on the gate the hour it landed, all terse unpunctuated fixture bodies.

The gate had been deferred two releases on description budget, and the blocker
was dissolved by reclamation rather than a ceiling bump. `DESC_MEMORY_LINKS_TAIL`
spent 888 always-resident characters restating what `docs/api.md` already
carried — REPLACE semantics appeared verbatim six lines above it in the same
description — and collapses to a four-name type index that keeps only the
glosses deciding which edge type to use. Net −471 on the lean surface (25,890 →
25,419 against the 25,900 warning line); the `acknowledge_truncation` parameter
itself cost 60 of 371 remainder headroom, so prose was the entire constraint.
`_DESC_BASELINE`, `_FOOTPRINT_BASELINE`, and `_LANDED_PARAM_BUDGET` were
re-measured in the same commit that moved them.

Measured before tagging: the blind-authored retrieval gold set produces
byte-identical results on this tree and on v3.37.0 in the same environment —
every arm, every metric — so the release changes what `memory_update` refuses
and nothing about what `memory_search` returns.

### Added — tests that execute the consolidate refusal arms nothing ran

`_apply_llm_proposal`'s hand-rolled gates are a deliberate divergence from the
shared write-gate chain — they judge the LLM-authored claim rather than the
stamped body that persists — and `tests/test_proposals_gate_parity.py` exists to
stop anyone rerouting them. Three of the five refusal arms turned out to be
unexecuted code: deleting the transient-marker gate outright passed the full
suite, because the parity test asserts on `consolidate.py`'s source text and so
passes over a gate that cannot fire. The transient-marker, `max_content_bytes`,
and previously-removed-twin arms now each have a test asserting the specific
refusal reason and an unchanged store, and each fails when the arm it names is
disabled. The size-cap fixture is built so the body is under the cap alone and
over it once the provenance stamp lands, which is the only input that tells that
gate's deliberate reading of the stamped body apart from the other gates'
reading of the claim.

## 3.37.0 - 2026-08-03

### Fixed — shipped install commands that the default macOS shell refuses

`[` and `]` are glob characters, and zsh — the default shell on macOS since
Catalina — refuses a command whose unquoted argument matches nothing. So
`pip install bettermemory[ui]` exited 1 with "no matches found" and installed
nothing. Six surfaces printed such a command: both branches of the
`mode='semantic'` error from `memory_search`, the provider-unavailable server
WARNING, both web-UI import errors, the Ollama provider's httpx error, and the
`ui` subcommand's help text.

Each now quotes the spec and leads with `uv tool install --reinstall`, matching
what `bettermemory doctor` emits and what `docs/installation.md` documents —
`uv pip` writes to whichever virtualenv is active rather than to the tool
environment the documented install actually runs from. The rule is pinned over
every tracked source under `src/`, matching an `install` command rather than a
bare mention, so prose that merely names an extra is untouched. This shape had
been repaired in three separate rounds, each closing an instance; the pin closes
the class.

### Fixed — `memory_update` refused a re-tag over scopes it was only carrying

`scopes` REPLACES the stored list, so keeping a scope means resubmitting it, and
`[scopes] allowed` was checked over everything submitted. A row written by
`bettermemory ingest` carries the provenance scope and type tag ingest stamps
itself — which the allowlist deliberately exempts at write time — so re-tagging
an imported row was refused for those stamps, and the only way to add a
sanctioned scope was to drop the provenance stamp the exemption exists to
preserve.

The check now runs over what an edit ADDS. A scope already on the record passes
because it was already accepted; one that is not is still checked by name, so
the exemption cannot be borrowed to plant an unallowed scope. Being a delta rule
rather than a stamp-name carve-out, it needs no list of ingest's tags to stay
correct. The `[scopes]` comment written into every user's `config.toml` is
corrected with it.

### Fixed — the commit hook and CI disagreed about comment lines

A message whose only violation sat on a `#` line passed `.githooks/commit-msg`
and then failed the `commit messages` job, turning a one-line edit into a
rebase. The two modes are genuinely asymmetric: when git opens an editor it
writes a comment preamble and discards those lines before storing, so grading
them would report violations about text that never enters history, while `-m`
and `-F` store `#` lines verbatim. The hook now tells them apart by the
environment git hands it — `GIT_EDITOR` is `:` for `-m` and `-F`, and the real
editor command otherwise, verified against git 2.50.1 by recording what a hook
observes on all three paths.

### Fixed — `doctor` reported an all-green install whose search was dead

With `search_mode = "semantic"` and neither embeddings extra installed,
`embeddings_extra` returned `ok` and the report exited 0, while `memory_search`
raised on every call: `semantic` is the one mode that does not fall back to
keyword/BM25. The verdict was decided by `semantic_dedup`, a different fact from
the one the check names, because the early return for a disabled dedup sat above
every probe of whether a consumer would load a model — the check had already
computed `wants_model` into its own details and never read it again. That install
now fails and says the mode does not degrade. The default `hybrid` install with
no extra still passes quietly, since that mode degrades by design, and a test
pins it so the gate cannot be widened into a false alarm.

Fix hints across both embedding checks now render through one helper each and
lead with the form that matches how bettermemory is running —
`uv tool install --reinstall 'bettermemory[embeddings]'`, with pipx and quoted
development-clone variants — instead of a `uv pip install -e .[embeddings]` that
cannot repair a tool install and that zsh refuses to run unquoted.
`retrieval_discrimination` no longer opens with "install an embeddings extra" on
an install that already has one: it branches on the same probes `embeddings_extra`
reads and names the repair this install actually needs.

### Fixed — `doctor` counted a disabled plugin as live session-start wiring

Claude Code records plugin enablement in an `enabledPlugins` map inside the
settings files, separately from `installed_plugins.json`, and `claude plugin
disable` leaves the install record and its `hooks/hooks.json` where they were.
Nothing read that map, so a switched-off plugin's manifest won the "runnable
binding" slot and the check reported a wired hook that Claude Code reads only to
decline registering. A record is now dropped only on an explicit `false` for its
`plugin@marketplace` key, read across the settings files in ascending precedence
so a project can switch a plugin off for itself alone. Every other shape — absent
key, list value, unparseable file, and an administrator's managed-policy disable,
which lives in a file doctor never reads — keeps the record, because the failure
to prefer here is a false alarm at someone who wired everything correctly. The
function's stated contract was narrowed to what it now measures.

### Fixed — the commit-message linter failed pushes over input its author cannot control

Dependabot writes its subject and body from a template, and both subjects it has
produced in this repo are shapes the `commit-lint` job rejects, so every weekly
bump — including a security patch — turned CI red on something no reviewer could
edit. `.github/dependabot.yml` now pins a Conventional-Commits prefix on both
updaters, and a commit git attributes to a bot is not graded in `--range` mode at
all, which covers the generated body line no configuration can reshape.

The wording rules also stopped matching inside hyphenated compounds (`this
round-trip`, `embarrassing-looking`), stopped skipping any body line that opened
with a capitalised word and a colon — `Note:`, `Tests:` and `Gate:` were
exempting every rule for the rest of the line — and now grade whole paragraphs,
so a banned phrase split across the body wrap is still caught. `--range` mode
grades a stored message exactly as git kept it, since `git commit -m` does not
strip `#` lines and the linter no longer does either. For the residual case where
a pattern cannot tell session narration from a real race window in the code, a
message may waive one **named** wording rule with a `Lint-skip:` trailer; there is
no wildcard, the envelope rules are not waivable, and the waiver stays in the
permanent record.

### Fixed — shipped comments, a docstring and an incident report described code that had moved

The `semantic_provider = "auto"` bullet written verbatim into every user's
`config.toml` documented presence-based resolution, which the resolver stopped
doing when it began requiring the extra to import as well; a broken torch now
loses to a working fastembed instead of taking the semantic leg down. The
`[scopes]` comment stated the stamped-scope exemption as a property of the scopes
themselves — it is a property of the ingest path, and `memory_update` replaces the
scope list wholesale, so re-tagging an imported row resubmits ingest's own stamps
and is refused unless `allowed` names them.

`Store`'s boot-time divergence check described its equal-count gate as missing
exactly one shape, an out-of-band swap of one memory for another. It compares
index rows against a raw `.md` file count that includes unparseable files, so it
is silent on any state whose row count equals its file count — including a memory
whose frontmatter breaks in place while its index row survives, which drops the
memory out of `memory_search` with nothing added or removed. The docstring names
both families and points at `bettermemory doctor`, whose identity reconciliation
runs with no count gate in front of it.

`docs/incidents/2026-07-25-doctor-false-green-on-importable-extra.md` stated a
superseded predicate in the present tense: its third condition, "an extra
imports", was narrowed in 3.35.0 to "the resolved provider imports", so a reader
sent there by the code or a commit message took away a form the repo's own tests
label a bug. Condition 3 is marked as the shape it shipped as, a Superseded
paragraph gives the current predicate, and the historical account is unchanged.

### Added — `ingest` regression coverage for the commit half of its config wiring

Only the `compute_ingest_plan` side of the plan/commit pair was guarded, so the
`config` argument on the `apply_ingest_plan` call could be dropped with the whole
suite still green — a loss that would score `--dry-run` under the user's
`[behavior] semantic_dedup` and thresholds while the commit fell back to lexical
Jaccard defaults, turning a promised `would write 1` into `wrote 0 / skip invalid
1`. A CLI test now drives a corpus the two scorers disagree on and asserts the dry
run and the commit report the same per-row action and reason.

### Changed — the commit-message register is narrower, and three more rules are enforced

The "Register" rules in `CONTRIBUTING.md` said to describe the change rather than
the session that produced it, and left everything past first-person narration and
conversational filler to review. Review did not hold the line: over the 600
non-merge commits ending at `5dededd~1`, 31 bodies name the sitting that produced
them ("this window", "the last round", "an adversarial pass over the previous six
commits"), 4 grade a defect instead of describing it ("worth stating plainly",
"cheerfully reported", "for the third time"), and 49 of the 50 release subjects
carry a parenthetical thesis beside the version. None of it is actionable for the
reader the log is written for, who is bisecting a regression and has no access to
anyone's calendar.

`tools/commit_lint.py` encodes the decidable part as three new rules that reject
session references and editorialising: `session-narration`, `editorial`, and
`release-subject`. Over that same frozen range they fire 88 times and every hit is
a genuine instance; the determiners in the session rule are enumerated rather than
open (`the current window` is absent) so that this project's real domain windows —
demotion, verification, tag ranges — do not trip it when they are named plainly,
and the editorial rule leaves the facts that make a recurrence actionable alone:
incident paths, defect classes, cited shas. A `release:` subject is now the version alone, since the release's argument
belongs in the CHANGELOG entry the tag points at. History is not re-graded; CI
lints only the commits a push introduces.

The prose rules moved with the linter, and `CONTRIBUTING.md`'s body guidance no
longer licenses length for its own sake: a body earns each paragraph by carrying a
fact the diff does not, and a one-line body over a one-line fix is correct.

### Changed — shipped prose states its limits without apologising for them

`README.md` carried three self-undermining asides: a caveat about the age of the
`bench/toolcost` head-to-head ratio, inside a bullet that does not state the ratio
and links to the bench README that owns the caveat in more precise terms; a
hedge on the published eval; and "you shouldn't need it, but" in front of the
documentation index. The facts are unchanged and the measurements are untouched —
the serialized `tools/list` figures still trace to their artifact.

The 3.36.0 entry above lost its aphorism lede and four phrases that graded the
release instead of describing it. Four source comments went the same way
(`sync.py`, `store.py`, `origin.py`, `handlers/scope_overview.py`).

Six passages were repaired in the same pass: the shipped plugin skill told the
model to "record honestly" and credited a ranker with learning, where the
mechanism is `endorsement_boost` and `outcome_demotion` reading a table;
`docs/eval-results.md`, which README links, introduced its published figures
under "reading it honestly"; and four passages in the incident and planning
documents now state a fact rather than commenting on it.

Sweeping every tracked markdown file except this one with
`commit_lint._WORDING_RULES` still reports 68 hits, and that residue is
deliberate rather than a backlog: the bench preregistrations grade their own
pre-committed predictions as their method, the MCP *session* concept is what
several tools are named for, the incident records have a field whose job is to
name the sweep that found them, and `CONTRIBUTING.md` cannot state a rule without
quoting the words it bans. The figure is a measurement of prose, not a budget —
re-run the rules over the corpus rather than trusting this sentence.

### Fixed — `CONTRIBUTING.md` described a setup that no longer exists

The "Local setup" section still told a contributor to `source venv/bin/activate`
and explained the `venv/` rename as the whole story, which stopped being true when
`.envrc` began resolving a checkout under `~/Documents` or `~/Desktop` to
`$HOME/.venvs/<checkout-dir-name>` — so the documented command activates nothing
on exactly the machine the workaround exists for. The prose was corrected first,
but only the comment above the command branched: the runnable line still read
`export UV_PROJECT_ENVIRONMENT=venv`, so pasting the block on a checkout under
`~/Documents` created the in-repo virtualenv inside the synced tree that
`docs/incidents/2026-08-01-broken-optional-extra-killed-retrieval.md` is about.
The block now sources `.envrc` instead of restating its rule, so the branch has
one definition, and it carries the `direnv allow` step and the `.venv` symlink
`[tool.pyright]` needs. The "Project values" section also pointed at a
"Limitations" section of the README; that section is in `docs/internals.md`.

## 3.36.0 - 2026-08-02

Nearly every fix below is a check, gate or verdict that reported on
something other than the thing it claimed to be reporting on. `doctor` certified
a hook whose binary does not exist, asserted an extra "imports cleanly" without
importing it, and counted another project's plugin as this project's wiring. The
`[scopes] allowed` whitelist governed every write path except the one that
imports in bulk. `export --strict` promised to name the files it had only
counted. The health report answered nothing at all because a stat-only footnote
could not read one directory. The detail page and `memory_show` gave different
staleness verdicts for the same memory, and the gate chain that refuses a claim
about the user was never consulted by the tool that edits one.

The shared defect is not a missing check but a check that skips, or answers, on
a condition other than the one it measures. Such a check is indistinguishable
from a correct one until the two conditions disagree.

### Fixed — `ingest` checks `[scopes] allowed`, and only against scopes you typed

`bettermemory ingest` builds its `Store.write` payload by hand and never reached
`_validate_write_payload`, so the whitelist that governs every other write path
did not exist on this one: with `allowed = ["tools"]`, an `--scope rogue` row
committed anyway. The scope allowlist is enforced here now, per row and ahead of
the gate chain, and an offending row takes the existing `skip_invalid` outcome
rather than aborting the batch.

Enforcing it naively broke the opposite way, and that half shipped and was
caught inside this release. Ingest stamps
every row with `imported-from-claude-code` plus a tag derived from the row's own
type, and the first cut checked *those* against the operator's allowlist. Nobody
types them and nobody can opt out of them, so any allowlist that failed to name
them refused every row of every import — while `--dry-run`, which ran no such
check, reported the writes that were about to be refused. Only the
scopes ingest stamps itself are exempt now, derived per row from that row's
type, so `--scope feedback` on a `project` row is still caller-supplied and
still checked. `compute_ingest_plan` runs the same predicate in the same
position, so the plan and the commit agree on the reason and not merely on the
count.

`config.toml`'s own `[scopes]` comment — written verbatim into every new install
— still stated the unconditional rule, "writes with scopes outside this list
fail". It names the exemption now, since that file is where someone decides what
to put in the list.

### Fixed — `doctor` reported a semantic leg it never probed

`_check_embeddings_extra` learned to resolve the configured provider and probe
it (`0bf7a49`, and again since) — but only on the branch where some extra was
already installed-and-broken. `extra_import_failure` returns nothing both for
"imports cleanly" and for "not installed at all", so an *absent* provider never
put a row in that list, and the check fell through to code that asks the
resolver nothing: it returned `ok` on `semantic_dedup = false`, or ORed
importability across both extras and returned `ok` naming whichever happened to
be present.

So `semantic_provider = "torch"` with only `[embeddings-fast]` installed — the
plain typo class, and the shape of one of this project's own CI legs — got a
green light reading "semantic_dedup enabled and fastembed importable" over a
process where `resolve_provider` returns `torch`, no model loads, hybrid ranking
is lexical-only and cosine dedup has fallen back to Jaccard. The verdict was
decided by an irrelevant fact: break the fastembed nobody resolved to, and the
same install correctly reported `fail`. That is lesson 1 of the 2026-07-25
incident — a check may skip only on the condition it measures — recurring on the
branch the earlier repair did not cover.

The resolver and the probe are hoisted above that split now, so every branch
reports the resolved provider and whether it imports. The severity also stops
overstating: the escalation and the "silently degraded" clause are gated on
whether any consumer would load a model at all, so a broken extra under
`search_mode = "keyword"` with `semantic_dedup = false` is a `warn` about dead
weight rather than a `fail` — which had been exiting 2 on installs whose
configured behaviour was entirely intact. And the hint stops recommending `auto`
in the one branch where `auto` cannot help: when the named extra is absent and
the other one is broken there is no working provider to resolve to, and it says
so instead.

### Fixed — `doctor` counted another project's plugin as this project's wiring

`_installed_plugin_roots` read `installPath` out of every record in
`installed_plugins.json` and ignored the `scope` and `projectPath` sitting
beside it, so a plugin install scoped `local` to one project counted as live
wiring in every other project `doctor` ran from. Such an install's `hooks.json`
binds `uvx bettermemory session-start || true`, which the path probe
deliberately declines to judge, so it won the "runnable" slot and the
session-start check returned `ok` — "hook is wired" — for a project where the
plugin is not enabled and the hook never fires. A genuinely stale binding in the
user's own settings was demoted to a footnote on that `ok`.

A record is skipped now only when it is explicitly `scope: "local"` for a
project directory that is neither the current one nor an ancestor of it. Every
uncertainty — no `scope`, an unfamiliar `scope`, a missing or unusable
`projectPath` — keeps the record, because the failure to prefer here is a false
alarm at someone who wired everything correctly, not a false green.

The same function also treated the manifest as authoritative on its top-level
shape alone and then returned nothing when no record inside it parsed, while its
docstring promised the `cache/` fallback covers a manifest "shaped in a way this
doesn't recognise". That file is version-stamped because its shape is not ours
to fix, and on a shape whose records we cannot read the plugin arm went silent
and the check told plugin users to install a plugin they already had. "Nothing
is installed" and "nothing here parsed" are two different answers now, and only
the first returns empty.

### Fixed — one unreadable subtree sank the whole health report

`report_for_directory` called `EpisodeStore(root).volume()` unguarded, and
`iter_session_ids` reaches the tree through a bare `iterdir()`. An `episodes`
FILE where a directory belongs — the shape a bad sync or a half-finished export
leaves — raised `NotADirectoryError`; an unreadable directory raised
`PermissionError`. Neither stayed local to the episode gauge. Both came out of
`memory_health`, `bettermemory health` and the web health pages alike, so every
staleness, drift and curation signal in the report was intact and unreachable,
sunk by a stat-only footnote that parses no frontmatter and answers three
integers.

Both shapes were reproduced before the fix. The episode volume now degrades to
absent the way any other unavailable input does, and the rest of the report is
delivered.

### Fixed — a claim about the user could be laundered in through `memory_update`

`UserClaimGate` exists so a body that reads as a claim ABOUT THE USER cannot
commit as a `fact` without the pending handshake: misattribution sticks, so the
user gets the veto. It sits in the shared gate chain, which `memory_update`
never consulted, so the refusal was one hop wide. Write "the deploy script
lives in bin/" as a `fact`, then `memory_update`
that body to "Mattias prefers tabs over spaces", and the exact body
`memory_write` hard-refuses landed verbatim, in the category `PendingGate`
reads, with nobody asked. It is the same laundering shape the credential gate on
this surface already closes for secrets.

`memory_update` now runs the gate on the new body and returns the same
`user_claim_warning` shape — a new status on that tool — judged against the
category the record will HAVE after the edit, which can only be `user-inference`
when the record already was one, since the retag INTO that category is refused a
few lines above. Metadata-only edits are untouched, or curating the mis-filed
records that predate the gate would be impossible.

The write path's `acknowledge_user_claim` escape is mirrored here, and the gap
between the two surfaces was not cosmetic: `_find_user_claims` ORs in a
case-insensitive `we (?:use|prefer|avoid|always|never)` branch, so an ordinary
project memory — "We use ruff for linting in this repo." — trips the refusal.
That body is writable with `memory_write(..., acknowledge_user_claim=True)`, so
without the parameter on this surface there was a body you could CREATE and then
could not EDIT into an existing record by any route, while passing the flag to
`memory_update` was dropped as an unknown argument and the refusal came back
anyway with nothing saying the flag did nothing. It has to reach the
`ToolHandlers.memory_update` facade to be on the wire at all — the served schema
is built from that signature, so a handler-only parameter never leaves the
process.

### Fixed — `doctor` judged another repo's attestations against this one

`_check_attestation_anchors` claimed cross-repo memories were "skipped, since
their anchors resolve against a worktree this process is not in". The loop never
read `memory.origin`. Relative `verified_paths` were joined to the CURRENT
worktree, so on a store shared across projects a memory written in repo B
attesting `pyproject.toml` was judged against repo A's copy and reported as
drift, with a hint to re-attest an anchor that was never wrong. The claim held
only for absolute anchors. It skips on repo mismatch now, matching how
`unverifiable_attestations` already anchors.

### Fixed — `export --strict` counted what it promised to name

Three `export --strict` surfaces promised that `doctor` would NAME the skipped
files; `doctor` reported a count. It names them now — the walk it needed was
already there. And `skipped_active_files` counted files AFTER loading them, so a
`memory_write` landing between the two walks manufactured a skip that never
happened and exited 1: routine capture turning a clean backup into a red cron
job on a store that was never damaged. Both walks take their count first now,
which reads the interleaved write as the non-event it is, and `doctor`'s twin of
that race is closed the same way. A `memory_remove` in the same gap still
over-reports by one — the rarer half, since writes are a reflex and removals are
curation — and the warning sends the reader to `doctor` rather than asserting
which file is at fault.

### Fixed — the detail page and `memory_show` disagreed about staleness

`_render_memory_detail` computed its verdict from the FULL `path_drift.missing`
count while every MCP surface had been narrowed to `claim_anchored_missing`. So
a calendar-fresh memory whose body merely MENTIONS a path this machine does not
have — a remote host's config, an `/etc/...` example — rendered `spot-check
recommended` on the page and `fresh` from `memory_show`, sending a curator to
re-verify a memory the sweep says is almost certainly fine. Its own docstring
asserted that the page "cannot disagree with what the model sees for the same
memory", and this is the second time web-versus-MCP staleness diverged on this
one file.

The page passes the anchored subset now. The evidence stays on it — the missing
path is still rendered, it just no longer speaks as a verdict — and the
docstring points at the test that enforces the parity instead of asserting it.

Two more readers of the pre-narrowing rule went with it.
`SYSTEM_PROMPT_ADDENDUM` is resident on every turn and still defined `fresh` as
"verification fresh AND no drift", so the model was being pointed at exactly the
set the verdict deliberately ignores; it is rewritten against what
`compute_staleness_verdict` does, came out shorter, and `docs/system_prompt.md`,
pinned byte-for-byte to it, moved with it. And the `/memories` note that told the
user "an embeddings extra is installed" renders behind a gate that opens on
`semantic_dedup = true` alone — with that set and no extra present it asserted an
install the user does not have. It names its condition now instead of asserting
it.

### Changed — an extra that is installed but fails to import is BROKEN, not absent

3.35.0 shipped the three-state reading of an optional extra — absent, working,
installed-but-broken — and two surfaces still collapsed it to two.

`semantic.extra_importable` classified by exception TYPE: any `ImportError` was
filed as "not installed". But absent and broken both raise it —
`sentence_transformers` present with `torch` uninstalled raises
`ModuleNotFoundError: No module named 'torch'`, which by exception type alone is
indistinguishable from `sentence_transformers` never having been installed. The
state is decided by PRESENCE now, by whichever means the arm has: the
`ImportError` arm consults `_spec_found` — is it on disk, answered without
importing — which this file already had, and the exception only supplies the
reason string, while a non-`ImportError` needs no lookup because the module was
found and its own code ran to raise it. Grepping for
clones found the same exception-type split inside `_load_torch_model` and
`_load_fastembed_model`; both pick their wording by presence through one shared
helper.

Two things change for the user. `doctor` stops reporting `ok` over an extra that
is installed and cannot be imported — that `ok` was not merely uninformative, it
asserted "no extra is installed-and-broken" while the probe behind it could not
see a broken one. Whether the fault reads as `fail` or as a `warn` about dead
weight depends on whether the config asks for a model at all; see "`doctor`
reported a semantic leg it never probed" above, which settled that split and the
"reinstall it" wording along with it. And the `mode='semantic'` hard error no
longer tells someone who already
has the extra to `pip install` it: it resolves the provider that will actually
load, consults `extra_import_failure`, and branches three ways — reinstall,
install, or no-model-resolved, naming both candidate causes without asserting
either.

### Fixed — `session_start_hook` certified a hook that could not run

The check reported `ok` on the strength of a `SessionStart` command string
containing `bettermemory session-start`. It never asked whether that command
could execute — so a hook naming a binary that no longer exists read as fully
wired.

The gap is reachable on a normal install and produces no signal the user could
act on. A hook command that names its binary by absolute path is a hand-edit — `init`
writes no hook at all, it patches only `mcpServers`, and the plugin ships
`uvx bettermemory session-start` — and a hand-wired path goes stale exactly the
way the MCP client's binary path does when an environment is rebuilt, moved, or
deleted. The trailing `|| true` that every documented form carries then turns
the missing binary into a *successful* no-op. And unlike the Stop hook, this one
records nothing by design, so there is no
telemetry to notice its absence: the hook is configured, contributes nothing,
and says nothing. `doctor`'s green light was the only observable, and it was
green for the wrong reason.

The check now warns when the command names its binary by an explicit path that
does not exist or is not executable, naming the path and pointing at the fix
(`init` refreshes the MCP command and never touches hook commands, so this one
is edited by hand).

It judges that one shape deliberately. `uvx bettermemory session-start` names a
launcher that fetches the tool on demand; `env …`, `cd … &&`, `sh -c "…"` and
`${CLAUDE_PLUGIN_ROOT}/…` cannot be adjudicated from a string at all. All of
them stay quiet, and the executable is located as the token before
`session-start` rather than the first token of the command — reading `cd` or
`env` as the binary is how a check invents failures that aren't there. On a
check whose value is a green light, a false alarm is expensive and a missed
alarm merely restores the previous behaviour.

The probe was initially a no-op on Windows.
POSIX `shlex` treats `\` as an ESCAPE, so `C:\Users\me\bin\bettermemory`
tokenized to `C:Usersmebinbettermemory` — no separator left, so the probe
declined to judge it as a path and went on reporting a stale hook as wired. It
tokenizes with `posix=False` on Windows now, and strips the quotes non-posix
mode leaves behind, since `C:\Program Files\…` has to be quoted to survive its
space and a candidate still wearing them resolves nowhere. `os.X_OK` is
meaningful only on POSIX — `os.access` reports it for any existing file on
Windows — so there the check reduces to existence, which is the half that
actually rots. The hook path's platform is an explicit `windows=` argument
rather than a read of `os.name`, because patching that global made `pathlib`
build a `WindowsPath` on Linux on Python 3.11 and older, which turned a failed
assertion into a whole-run crash.

And it judges the set, not the first hit. The check originally warned on the
first candidate file carrying a matching command whose path would not run, and
stopped scanning. Claude Code merges the `SessionStart` bindings it finds across
settings files and plugin manifests and runs all of them, so a user who
hand-wired an absolute path years ago and has since installed the plugin carries
two bindings and a hint that reaches the model — and `doctor` called that hook
broken. Every binding in every readable candidate FILE is judged now — both
bindings can sit in one settings.json, so a stale one above a runnable one no
longer hides it, and when they are all stale the warning counts the bindings it
actually read rather than the files it looked at. One runnable or unjudgeable
binding anywhere returns `ok`, and the stale spelling is named as a detail on
that `ok`: dead config rather than a broken hook.

### Changed — inside a cloud-synced folder, `.envrc` puts the venv outside it

Renaming uv's `.venv` to `venv` addressed macOS hiding dot-directories in
iCloud-synced folders. It did not address the syncing, which is the part that
does the damage: duplicate `name 2.py` conflict copies, two dist-info
directories for one package (making `importlib.metadata.version` return `None`),
and — on 2026-08-01 — a `transformers` tree left holding 226 of its 2347 `.py`
files, which raised `KeyError: frozenset()` on import and took `memory_search`
down entirely.

`.envrc` now routes a checkout under `~/Documents` or `~/Desktop` to
`$HOME/.venvs/<checkout-dir-name>`, keyed by directory name so sibling worktrees
keep separate environments. Anywhere else the in-repo `venv` default is
unchanged, so this is a no-op for a checkout that was never in a synced folder.

### Fixed — the MCP registry publish could never fire on a release

`publish-mcp.yml` shipped in 3.35.0 triggered on `release: published`, with a
header arguing carefully for that choice over the tag push the upstream guide
suggests. The argument was sound and the trigger was dead: GitHub does not start
a workflow run from an event raised by a job authenticating with the default
`GITHUB_TOKEN` — a recursion guard — and `release.yml`'s `github-release` job
uses exactly that token.

So the automatic path had never fired once. It looked like it worked because the
3.34.0 listing was published by hand through the `workflow_dispatch` backfill,
and a fallback that succeeds is the most effective way to hide a primary path
that cannot run. Observed on the 3.35.0 release: PyPI got 3.35.0, the GitHub
release was created, and no registry run was queued at all.

The workflow now also triggers on `workflow_run` against the Release workflow,
which observes that run finishing rather than the event it emits, and is not
suppressed. It is gated on a succeeded run whose `head_branch` starts with `v`,
so a failed release or the TestPyPI dispatch does not publish a listing, and it
checks out the TAG rather than the default branch — under `workflow_run` the
default checkout is wherever `main` points now, which would list a `server.json`
describing a version other than the one just published. `release: published` is
kept alongside it, because a release cut by hand or with a PAT does raise a real
event.

## 3.35.0 - 2026-08-01

This release has one subject: **an optional dependency has three states —
absent, working, and installed-but-broken — and a probe that models two of them
turns a fault in an optional feature into an outage in a required one.** A
partially-evicted `transformers` tree took `memory_search` down completely,
through a predicate whose entire job was to return a bool. The same two-state
assumption then turned up in the provider resolver, in the `doctor` check that
names the extra, and in the install docs.

### Fixed — an optional extra that was installed and BROKEN killed all retrieval

`memory_search` returned `Error executing tool memory_search: frozenset()` for
every query. The store, the index and the ranker were all fine. What had
happened is that a `transformers` install had been partially evicted by iCloud
out of a venv living under `~/Documents` — 226 of 2347 `.py` files left on disk
— so the lazy-import scan `transformers/__init__.py` runs over its own package
tree found nothing and raised `KeyError: frozenset()` while executing.

`sentence_transformers` imports `transformers`. bettermemory imports
`sentence_transformers` to answer one boolean — is a semantic ranking leg
available — and that probe caught `ImportError` and nothing else. So the
exception escaped a PREDICATE, travelled up through
`semantic_setup._semantic_model_or_none` and out of the MCP tool. A fault in an
OPTIONAL ranking leg, whose entire documented behaviour is to degrade to
keyword+BM25, took down required retrieval. The same factory backs write-time
dedup, so capture was in the blast radius too — both halves of the product,
from a dependency neither half requires.

An optional dependency has THREE states — absent, working, and
installed-but-broken — and every probe here modelled two. The asymmetry is
visible in the old code: the model CONSTRUCTION in `_load_torch_model` was
already guarded `except Exception  # model load can fail many ways`, while the
import directly above it caught only `ImportError`. Many failure modes were
anticipated for the constructor and exactly one for the import, though an
import runs arbitrary third-party module-level code and so has strictly more.

`semantic.extra_importable` is now the single place that owns the three-state
answer. Broken returns False and logs once per process at WARNING — silence
there would trade a loud crash for a silent capability downgrade, which is the
worse failure, because search quietly gets worse and nothing in the process
says why. Absent stays silent, since that is the default install and not a
fault. `semantic.extra_import_failure` exposes the reason for diagnostics.
Both model loaders grew the matching branch, and `find_spec` — documented to
return `None` for "not found" but which raises on some damaged installs — now
goes through `_spec_found`.

### Fixed — `doctor` was silent about a broken extra on the default config

`_check_embeddings_extra` returned `semantic_dedup disabled (no extras needed)`
and stopped, whenever `semantic_dedup` was false. That is the DEFAULT. The gate
was correct when the extra fed only write-time dedup and has been stale since
`hybrid` became the default search mode and the extra started feeding ranking
with no `semantic_dedup` involvement at all. So the one check that names the
embeddings extra went quiet on precisely the population a broken extra
degrades — this project's recurring shape, a check whose input population no
longer matches the one the feature serves, and during the outage above it
reported `ok`.

The broken branch is now evaluated before the `semantic_dedup` gate, names the
module and the import error, and says **reinstall** rather than install —
telling someone to install what they already have sends them looking in the
wrong place. The absent case still respects the gate: a default install with no
extra is not a fault, and `retrieval_discrimination` already owns that advice
with a measurement behind it.

### Fixed — auto-detect picked a broken provider over a working one

Two embeddings extras exist so one can cover for the other, and
`resolve_provider`'s auto branch could not do that: it asked whether a provider
was on disk, not whether it worked. With both installed and
sentence-transformers broken, it returned `torch`, the loader then returned
`None`, and the semantic leg was lost on a machine with a perfectly good
fastembed installed. Worse, `_semantic_rank_leg_active` ORs across the
providers, so it went on reporting that a semantic leg was scoring searches
while none was — a false green in `doctor` and the web UI both.

Auto-detect now returns the first provider that actually imports. When every
installed extra is broken it still names one rather than returning `None`,
because `None` reads as "no extra installed" downstream and earns the install
hint — the wrong advice for someone who has it installed; naming it routes to
the loader whose WARNING says what actually failed. An explicit
`semantic_provider` is still honoured verbatim, broken or not: it is an
instruction, and quietly serving a different provider would make the embedding
cache's provider namespacing a lie.

The same check also asked only about sentence-transformers when deciding
whether `semantic_dedup = true` had what it needs, so `[embeddings-fast]`-only
users were told the extra was not installed while their cosine dedup worked
fine. Either extra now satisfies it.

### Fixed — a semantic leg was reported over a run that loads no model

`_semantic_rank_leg_active` exists because
`docs/incidents/2026-07-25-doctor-false-green-on-importable-extra.md` caught
`doctor` reporting a semantic leg on the strength of an extra merely being
importable. That fix replaced one condition with three. The third — "an
embeddings extra imports" — stayed coarser than the routing it describes: it
ORs across both providers, while `resolve_provider` commits to exactly one.

So `semantic_provider = "torch"` with torch not installed and fastembed healthy
made the OR true, resolution honoured the explicit preference and returned
`torch`, the loader returned `None`, and nothing ranked semantically — while
`doctor` reported `ok`, printed "a non-lexical signal scores every search", and
**skipped its retrieval-quality probe**, and `/memories` told the reader
`memory_search` fuses a leg it does not have. No damaged package is involved;
it is a config typo, which is the class of thing `doctor` exists to catch.

Condition 3 is now `_resolved_provider_importable` — the health of the provider
that will actually be loaded.

`web._lexical_only_note` is deliberately NOT changed, and the attempt to change
it is worth recording. Its gate looks like the same defect, and the same
narrowing applied there fails
`test_lexical_only_note_fires_exactly_when_a_semantic_leg_ranks`: that test
injects a working model and spies on which scorers actually run, because the
note describes what the HANDLER does. Gating it on whether an extra imports in
the ambient process couples a description of handler behaviour to a probe the
handler need not have used — which is the substance of the "must not be merged"
note the 2026-07-25 incident left on both predicates. Two gates that look alike
answering different questions is the thing that report is about.

### Fixed — the guard that should have caught the stale install doc could not

`test_docs_state_semantic_is_enabled_by_the_extra_alone` pinned the install
contract against a hand-written list of two files. `docs/installation.md` was
never in it, so the retired claim sat on the canonical install page — the page
`doctor`'s own `fix_hint` links to — for three releases while five other
surfaces were pinned. The forbidden literals were too narrow as well: the drift
wrote "the extra alone doesn't change ranking — semantic search also needs the
config opt-in", which matched neither of them, so adding the file without
widening the wording would still have passed.

The population now comes from `git ls-files` over every tracked markdown file,
the same correction 3.34.0 applied to the prose corpora, with `CHANGELOG.md`
exempted because a changelog has to be able to quote the wording it retired.
Verified by re-introducing the exact sentence and watching the guard fail.

### Fixed — `docs/installation.md` still described the pre-3.x install contract

It said the embeddings extra "alone doesn't change ranking — semantic search
also needs the config opt-in". That has been false since `hybrid` became the
default: installing either extra is sufficient, and `semantic_dedup` — the flag
a reader would reach for to "activate" it — only ever controlled write-time
dedup. This is the same foot-gun `semantic_setup` documents as already fixed
("a documented foot-gun that cost two sessions' worth of wrong install
advice"); the install page was the last surface still carrying it, and it is
the page `doctor`'s own fix hint sends people to.

`tests/test_broken_optional_extra.py` simulates the third state with a
`sys.meta_path` finder whose loader raises at exec time, so it needs no broken
package installed and runs on every matrix leg including the no-extras one.
Verified as a negative control: reverting the source fails all of it, including
an end-to-end case that reproduces the exact
`Error executing tool memory_search: ...` string, and a provider case where the
old presence-only resolver returns `torch` and then no model at all.

### Added — the release lists itself in the MCP registry

`.github/workflows/publish-mcp.yml` submits `server.json` after a release is
published. `server.json` has been complete and correct for some time and was
never being submitted, so the registry had no entry for this project.

It fires on `release: published` rather than on the `v*` tag the upstream guide
suggests, because the registry refuses a listing whose version is not yet on
PyPI, and a tag-triggered job races `release.yml`'s own publish. `release.yml`'s
`github-release` job declares `needs: publish-pypi`, so a published release is
the earliest moment that precondition is known to hold — enforced by the job
graph rather than by a sleep. It is a separate workflow so that a registry
outage cannot turn a good PyPI release red.

Authentication is GitHub OIDC, so no secret exists to leak or rotate. The
workflow asserts each of the registry's preconditions itself before calling the
publisher — the two `server.json` version fields agreeing with each other and
with the tag, the version being live on PyPI, and the `mcp-name:` marker being
present in the published description — because the registry reports all of them
as one opaque "Package validation failed". That marker lives in `README.md` and
is read by nothing else, which makes it exactly the kind of line a tidy-up
deletes.

## 3.34.0 - 2026-08-01

This release has one subject: **a checker that enumerates its own input
population stops covering what it claims to cover, and says nothing when it
does.** Every published incident in `docs/incidents/` is that shape — a
`doctor` skip asking whether an extra was importable instead of whether a
semantic leg would score a search; a calendar leg pre-empting the drift
measurement it exists to back up; a guard asserting the ingest *plan* and never
the commit; nine CI legs installing from `uv.lock` instead of the constraint
users resolve against. A seven-lane audit went looking for the next one and
found six, none of the lanes looking for the same thing. This ships the fixes
and, where the class allows it, the ratchet that makes a recurrence fail the
build.

### Fixed — `doctor` certified a stale index, for the third time

`index_health` answered a store holding one memory the index had never seen and
one row for a memory that had been deleted with `Index healthy: 2 memories
indexed (matches disk; PRAGMA quick_check passed)`. "Matches disk" was a claim
about a row count; nothing had compared the index to disk in any other sense.
Both reproductions run through the workflow this project advertises as its
differentiator — one file per memory, grep-able and hand-editable — an
out-of-band identity swap, and a hand-edit that leaves the id and the filename
alone.

The gate was `if indexed_count == disk_count: return ok`. Everything downstream
of it — the parse-aware refinement, the unparseable-file arithmetic — lived on
the *unequal*-count path, so the identity comparison was unreachable at exactly
the counts a swap produces. `store._warn_on_index_divergence` carried the same
early return and was silent on both stores for the same reason.

It now reconciles before it certifies. `_reconcile_index_against_disk` runs an
identity leg (routed through `store._has_confirmed_index_gap`, not a raw
`indexed_ids` set difference — a raw diff reports holes that close a
millisecond later under concurrent writes, which is the false positive that
helper exists to kill) and a content leg (`SELECT id, scopes_json, body`
compared against memories `doctor` already loads). Both record in `details`
whether they RAN, because "reconciled and clean" and "could not reconcile" are
different claims and this check's whole history is of the second being reported
as the first. The `ok` message is now an inventory of the legs that ran rather
than the word "healthy" over an unexamined index.

Two docstrings that asserted the opposite of the code went with it:
`_build_context_block`'s claim that the session-start count was **provably**
what the other surfaces would report, and `_warn_on_index_divergence`'s claim
that the raw count was "only the cheap TRIGGER, never the verdict". The
session-start gate took the strictly stronger free upgrade instead — it
compares the index's `filename` column against the directory listing it already
performs, no parse, and declines to publish a scope table when they disagree.

Blast radius, stated exactly rather than dramatically: below
`_INDEX_THRESHOLD_DEFAULT` (500) `memory_search` loads from disk, so on the
maintainer's 239-memory store a stale index poisoned the session-start scope
table and the persisted FTS text but not candidate selection. Above 500 it is
candidate selection too, which means this class of latent damage silently
promotes itself from cosmetic to retrieval at a size boundary nobody watches.

Postmortem: [`docs/incidents/2026-07-31-index-health-certified-a-stale-index.md`](docs/incidents/2026-07-31-index-health-certified-a-stale-index.md).
Its second lesson is the one worth carrying: **fixing a false green by adding
evidence beneath the gate leaves the gate.** Twice before, the repair was a new
probe — a parse walk, then `quick_check` — wired in below an untouched
`if a == b: return ok`. Both probes were real. Both times the shape that
produced the incident was still reachable.

### Fixed — the prose-honesty ratchets scanned 12 of 43 tracked documents

`tests/test_doc_claims.py` built its corpus as `README.md` plus
`glob("docs/*.md")`. The repository tracks 43 markdown files. So every incident
postmortem, the model-facing plugin skill, `SECURITY.md`, `CONTRIBUTING.md` and
every bench README were outside the honesty check — proven by negative control
rather than argued: a fabricated claim citing a `verdict_ladder` module that
does not resolve anywhere in this repository, appended to five of those
documents on a `git archive HEAD` scratch tree, still gave `4274 passed`. The
same line in `docs/api.md` fails the build with a named error.

`tests/test_number_claims.py` had the same shape one level cruder: a two-element
literal. Its surface set goes 2 → 24.

Both corpora now derive from `git ls-files -- '*.md'` minus a named exclusion
set, each entry carrying its reason inline, reusing the `_git_tracked_files`
helper that had been sitting twenty lines below the glob that didn't. The
predicted rot was already in the blind set and is repaired here:
`bench/retrieval/README.md` asserted that staleness-verdict accuracy "has no
accuracy measurement anywhere yet", which `bench/rot` has falsified across 30
repositories.

**The coverage ratchets were rebuilt to be falsifiable.** The first draft
asserted `set(tracked) - scanned - exclusions == ∅` where `scanned` was *derived
from* `tracked` — structurally empty for every input, a guard that cannot fail,
inside the change that exists to close guards that cannot fail. The population
is now an independent filesystem walk, so the two enumerations of "markdown in
this repo" have to agree; the walk-versus-git difference (untracked and ignored
files) is subtracted explicitly and named in the docstring rather than papered
over. Narrowing the shared listing — the regression the first draft could not
see — now reds both ratchets while the original assertion bodies stay green.

### Fixed — two shipped-prose guards ran against a tool surface no install builds

`BehaviorConfig.full_tool_surface` defaults to `True` as a dataclass field and
`False` in `load_config()`, which is the only path `bettermemory` runs. The two
guards that keep shipped guidance from naming unreachable tools built their
server the first way. Re-pointed at the surface that actually ships, they fail
on nine tools in the system-prompt addendum and four in the plugin skill — and
`plugin/.mcp.json` runs `uvx bettermemory` with an empty env, so **a plugin
install is the lean surface**, shipping a skill that names four tools its own
MCP entry never registers.

Both guards are now parametrized over both surfaces. The addendum and the skill
mark the full-surface-only tools, following the convention the skill already
used correctly in one place.

Downstream of the same split, the curation-pressure hint — a runtime payload
that reaches every install — read `Call memory_health for full buckets;
memory_remove or memory_verify to resolve`. `memory_health` is registered only
under `full_tool_surface`. The remedy was also on the wrong axis: the pressure
was entirely cold-endorsement, whose predicate is `explicit_applied_count == 0`,
and `memory_verify` writes a verification rather than a use event, so it cannot
move that count at all. The hint now names routes that exist on every install,
and a new ratchet asserts that — mechanically, against the real argparse
subparsers and the real lean tool set, with a non-empty-extraction guard so it
cannot go vacuous when the message is reworded.

### Added — `export --strict`, and an export that says what it dropped

`bettermemory export` ran `store.load_all()` and `store.load_tombstones()`,
both of which swallow `PARSE_SKIP_EXCEPTIONS` — literally `(Exception,)` — and
then reported the survivors as the backup. Three memory files in, "Exported 2
active memories + 0 tombstones", exit 0. A backup that is silently short is the
worst failure a backup can have: invisible at capture, discovered when the
source is gone. Worse, `tombstoned_memories` was emitted as `[]`, which
export's own contract documents as "no tombstones present" — a dropped
tombstone was affirmatively reported as absent.

The payload now carries `skipped_active_files` and `skipped_tombstone_files`
unconditionally, and a warning goes to stderr pointing at `bettermemory
doctor`. The keys deliberately do not say "unparseable": the value is a
two-walk count delta that cannot distinguish malformed frontmatter from a file
this install intentionally skipped, which is the same distinction `doctor`
already refuses to collapse.

`--strict` is new and opt-in: it exits non-zero when anything was dropped. The
default stays exit 0, because `export -o` is advertised as the scripted-backup
path and flipping its exit status would break cron callers silently — the
failure mode this section is about, aimed at the fix for it.

### Changed — the commit-drift escalation pre-registration is graded, and retracted

`src/bettermemory/verify.py` carried a pre-registration in a source comment:
if alerts-per-catch for the escalating tier still read `>= 1.5` once two
conditions shipped, `_COMMIT_DRIFT_ESCALATES` flips to `False` — "either
outcome is a result; commit the numbers." Both conditions shipped. The trigger
read **3.4**. Nobody graded it, and `B2b` appears nowhere in the roadmap; the
upgrade plan it cites does not exist in the repository.

Graded here, and the gate is **retracted rather than honoured**, because
honouring it was measured to be wrong. With the switch off, the drift arms go
`flag 96.74% → 0.00%` and `J 0.0339 → 0.000` — exactly `never_flag`, the mirror
image of the `always_flag` constant function v3.30.0 fixed and postmortemed —
while `shipped_default` is bit-identical, because the demotion branch reads
`commit_drift_count` directly and bypasses the switch entirely. The gate's
premise was that the anchored path leg would substitute; that leg reaches
`flag_rate 0.0073` with `unflagged_stale_rate 0.968`. The counterfactual run is
committed as an artifact beside the control rather than described, and a new
test fails the build if any of the three by-path citations to it dangle.

Recorded with it, in `docs/ROADMAP.md` so it stops living in a source comment:
the shipped verdict measures J = 0.2875 at 77.8% flag and 29.5% precision on
the pre-registered 30-repository corpus against `always_flag`'s J = 0.000 and
22.9% — a real signal, and a weak one. **Claims-at-write** is promoted above
the standing-tier and event-time items on the strength of the claim-level
detector's 1.1 alerts-per-catch at 94% precision against the shipped 3.4.

### Fixed — three surfaces that described behaviour they did not have

- `--typo-distance` is inert. `find_scope_typo_pairs` never reads the
  parameter; the value is threaded faithfully to the last call and dropped. The
  CLI help promised "raise to 3 to surface more pairs". The flag stays accepted
  — removing it would break scripted callers — and its help now says it is
  ignored, pinned by an assertion against the parser's own help string so the
  correction is itself a build-failing guard rather than prose.
- `memory_audit_turn`'s description and module docstring said the Stop hook
  reaches the tool "through the MCP channel". The shipped hook dispatches
  `uvx bettermemory audit-turn --quiet`, the CLI. Correcting it returns 24
  characters to the metered description budget (25,773 → 25,749); both
  footprint baselines are re-measured in this commit, per the rule that module
  states about itself. The tool's registration is deliberately unchanged: zero
  MCP dispatches in one maintainer's event log is n = 1, not evidence about
  other clients.
- `sync pull`'s rebase-failure exit told the user to run `git rebase
  --continue/--abort` and never mentioned `bettermemory reindex`, which its
  sibling autostash exit does. Both error paths now carry the same recovery.

### Added — `_frontmatter.normalise_body`, one definition of a rule with two readers

`dumps` strips CR-before-newline on the way to disk while the index is written
from the in-memory record, so `doctor`'s new content leg compares across that
normalisation and would have called an untouched CRLF body drift — a `warn`
saying the index no longer describes a store that is in fact perfect. The rule
is now a shared function rather than a second copy, which is the standing
lesson from the 2026-07-26 postmortem: mirrored implementations guarded by
comments are a defect with a countdown.

## 3.33.0 - 2026-07-31

### Changed — bettermemory runs on the mcp 2.x SDK. **The floor is now `mcp>=2.0.0`**

**Upgrade note.** This is the one thing in this release that can require
action. `mcp.server.fastmcp` does not exist in mcp 2.x and
`mcp.server.mcpserver` exists in no 1.x, so there is no overlap version and no
way to support both without a permanently forked type surface. If you need to
stay on mcp 1.x, pin `bettermemory<3.33.0`. That pin genuinely works, which is
the difference between this and 3.31.1: there, an unbounded constraint had
already poisoned every published wheel, so "stay on the last good version" was
advice with no version behind it.

**It is still a minor, and the wire is why.** No tool, parameter, or response
field is renamed, removed or reshaped. The SDK's rename of
`Tool.inputSchema` → `input_schema` is a Python attribute change only:
`mcp_types.MCPModel` sets an alias generator and the transport serialises
`by_alias=True`, so a connected client receives the same `inputSchema` bytes it
received from 1.27.0. Nothing the schema-title scrub reaches through moved
either — `_tool_manager._tools`, `tool.parameters`, `fn_metadata.output_schema`
and the `cached_property` on `Tool.output_schema` were each checked against a
real 2.0.0 install and are identical, so the scrub is still needed and still
mutates in place.

**One wire field does change, and it was wrong before.** `serverInfo.version`,
which a client reads on `initialize`, is now this project's own version. mcp
1.x's lowlevel server defaulted an unset version to the *mcp package's* version,
so every release up to and including 3.32.0 introduced itself to clients as
`1.27.0` or whatever SDK it resolved — the server reporting its dependency's
version as its own. 2.x replaced that fallback with `version: str = ""`, which
would have turned the same never-passed argument into an empty string on the
wire. `build_server` now passes `__version__` explicitly, read from the package
rather than re-derived, so the field cannot drift from
`bettermemory.__version__`. A client that keys off `serverInfo.version` sees a
changed value here; a client that keys off tool names and schemas sees nothing.

**Three changes in `src/`.** The server-class import and its annotations in
`builder.py`; the `Context` generic arity, which went from three parameters to
two when 2.x dropped the session type, in `handlers/_shared.py` and
`session.py`; and one accessor in `session.py`, where the per-client id moved
from the `Context.client_id` property 1.x offered to a mapping read on the
request's `_meta`. That last one is reachable rather than lost: 2.x types
`_meta` as `RequestParamsMeta`, an open `TypedDict` with `extra_items=Any`, so
arbitrary keys still round-trip and `meta.get("client_id")` returns what
`ctx.client_id` used to.

**The floor was verified as a floor, in both directions.** The
`install from declared constraints` job — which resolves without the lockfile,
and exists because of the 3.31.0 incident — is green on the ported tree. As a
negative control, the ported tree against a clean `mcp==1.29.0` fails at
`builder.py`'s import while the pre-port tree against that same install builds
a server. The break is exactly the port and nothing else.

**Why the test suite did not need 83 edits.** mcp 2.0.0 also changed what
`call_tool` returns and which attribute names carry the schemas — 44 unpack
sites across 44 test files, 43 schema reads across 6, and 2 files that would not
even collect. All of it was routed through `tests/_mcp.py` first, so the port
edited one helper module instead of the suite. With one major now in play, the
1.x branches in that module are gone: a branch no installable configuration can
reach is not compatibility, it is untested code that reads like a promise. The
forged request `Context` two test modules each kept a private copy of moved
there too — both copies broke on the same change, which is the tax that module
exists to stop paying.

**`pydantic` is capped at `<3.0.0`, and that line now stands alone.** Under mcp
1.x we also inherited a `pydantic<3.0.0` bound from the SDK itself. mcp 2.0.0
declares `pydantic>=2.12.0` with no ceiling of its own, so the port removed that
inheritance and this cap is the only ceiling left — the same gun that fired on
3.31.0, held down by one line with no backup.

**The failure mode that ships silently is guarded.** 2.0.0 decides whether to
inject a `Context` by matching a handler's resolved type hints against its own
`Context` class. If the alias in `handlers/_shared.py` ever points at a
different class, injection stops firing, `ctx` arrives as `None` on every call,
and every client collapses into one shared session — with no exception and no
failing test, because `SessionRegistry._key_for_ctx` swallows exactly that shape
by design. `tests/test_session_registry.py` asserts the identity positively, and
that a registered handler still declares a resolvable `ctx` hint.

Two guards were rewritten rather than re-pinned, because both were pinned to the
SDK's spelling for no reason the rename made worth keeping: the AST reader that
extracts the server `instructions` block now matches on the `instructions=`
keyword instead of on a call to a class named `FastMCP`, and the two passages
asserting that the mcp floor is an install-compat promise now say what actually
binds — the *manner* a floor moves, deliberately and announced, not the
particular number it was sitting at.

### Removed — the internal audit plans, which belong in a memory store rather than a public repo

`docs/audit/` carried about 3,400 lines of session-internal material: a drained
phase plan whose every phase had shipped, three superseded entry briefs, and six
"fact packs" of line-number anchors that their own sweep measured as 32% still
exact. The prose in them was sound and the coordinates had rotted — which is
what a snapshot does, and why the directory sat deliberately outside the
doc-claims checker's corpus, where nothing could ever fail on it.

The mcp 2.x port analysis moved to the durable store and, briefly, to
`docs/ROADMAP.md` — briefly because the port shipped in this same release, so
the roadmap entry is already gone and what survives of the analysis is the
section above. The deferred backlog it carried is now a "Small and anchored"
section of the roadmap, in house voice. Kept:
`docs/audit/extractor-hunt-2026-06-09.{md,json}`, a measurement artifact three
changelog entries cite by path.

Also corrected in passing: the roadmap described `apply_write_gates` and
`memory_verify`'s attestation refusal as unreleased. Both shipped in 3.31.0.

### Fixed — six ways a committed record could disappear, and the chokepoint that closes the class

An adversarial sweep over the persistence, parse, gate and index surfaces
found one defect reached from six directions: **a write reports `committed`
and the record is then invisible to every read surface.**

The mechanism is that `model_copy(update=...)` runs no pydantic field
validators, and nearly every mutation in the codebase is a `model_copy`. An
over-cap record therefore serialises happily — 65 links is nowhere near the
64 KB YAML cap, so `_frontmatter.dumps` sees nothing wrong — the write
returns normally, and the next read re-constructs through `Memory(...)`,
raises, and `load_all` catch-and-skips the file. The record leaves search,
list, show and health while its `.md` sits on disk looking healthy. Three
call sites had each been patched individually before this; three had not:

- `memory_curate` merging two 33-scope duplicates produced a 66-scope keeper,
  **destroyed both records**, and reported `applied: true, failures: []`.
- `memory_conflicts` resolving a contradiction appended a 65th link to a
  source already at the cap and reported `link_written: true`.
- `memory_update` did the same with scopes whenever `max_scopes_per_write`
  was raised above 64 or set to `0` — the value `config.py` documents as
  "disable the handler cap (the model-layer cap still fires at 64)", which
  was true for `memory_write` and false here.

Rather than patch the three and leave the fourth caller open, `Store`
re-validates at the single chokepoint every persist routes through
(`_write_path`), and refuses with a `ValueError` naming the failing field.
This is the byte-axis lesson — `dumps` is the one place size is checked —
applied to the semantic axis. Cost is one pydantic round-trip per write,
against an fsync.

A sixth route reached the same end state without any cap: `_SCOPE_RE` and
`_ULID_RE` anchored with `$`, which in Python matches before a trailing
newline, so `"projects:foo\n"` validated and persisted verbatim. The record
was then filed under a scope that no filter, auto-scope resolution or
`memory_list` query can equal — and on disk it renders as `- 'projects:foo`
plus a blank line, which does not look wrong either. Both now anchor `\Z`.

### Fixed — the index rebuild destroyed the index it was repairing

Two active `.md` files can legitimately carry one id: an iCloud or Dropbox
conflicted copy, a `cp` before a hand-edit, a backup restored by copying
files back in — the exact situations `bettermemory reindex` exists for. The
rebuild `INSERT`ed each file, hit `UNIQUE constraint failed: memories.id` on
the second, and fell into the corruption fallback, which unlinked the healthy
index, retried the identical feed, and failed again. The store was left with
an empty `needs_rebuild` index and `doctor` recommending the one command that
could no longer succeed.

The rebuild now upserts, collapsing duplicate ids exactly as the incremental
path and `scan_active_memory_ids` already document. Independently,
`sqlite3.IntegrityError` no longer reaches the corruption fallback at all: a
constraint violation is a statement about the entries, never about the file,
so unlinking a valid index over one was always wrong.

### Fixed — a restore that could not re-admit the record destroyed the tombstone anyway

`restore` wrote the active file, unlinked the tombstone, and only then tried
to load what it had written. A tombstone whose frontmatter parses but which
`Memory` will not re-admit — one written by a newer `schema_version`, the
state a `sync pull` from a newer host or a downgrade produces — left the
record in neither listing: gone from `list_tombstones`, unparseable in
`load_all`, with a retry raising `NotTombstonedError`. The load now happens
first and a failure rolls the whole restore back.

### Fixed — an update could mint a permanently un-removable record

The file axis has reserved room for a record's own removal metadata at
admission since 3.14.1; the frontmatter-YAML axis reserved it only on
lifecycle re-dumps, on the reading that a first write cannot approach 64 KiB
of frontmatter. `memory_update` admits content on the same path and can grow
the frontmatter without touching the body: 64 links each carrying a ~950-char
note, all individually legal, landed a record whose `memory_remove` then
failed forever — as did both escape hatches `handlers/remove.py` names. The
YAML axis now reserves the budget at admission too.

### Fixed — a serialisation bomb the expansion guard could not see

`_guard_dump_expansion` charged one node per value regardless of size, so a
bomb built from a few large scalars rather than many small ones walked
through it: a 50 KB file expanded to 12,359 nodes against a 65,536 budget,
then spent 218 seconds and 1.1 GB of RSS inside `yaml.dump` producing a
555 MB string for the post-hoc byte cap to reject — the precise failure the
guard's own docstring says it prevents. Reachable through the documented
threat model, a hostile `sync pull` or hand-edit dropping a `.md` into the
memory directory, and it left the record un-removable because `tombstone`
re-serialises. The walk now carries a scalar-byte budget beside the node
budget; the same bomb is refused in under a millisecond at 66 MB of RSS, and
the densest realistic record still serialises.

### Fixed — two round trips that rewrote what they stored

`_frontmatter.loads` strips the CR off every line so a Windows-authored file,
or one checked out with `core.autocrlf`, reads the same on every platform.
That normalisation is right and stays; what was missing was its other half.
`dumps` wrote the body verbatim, so a CRLF body reached disk with its CRs
while every reader returned it without them — `alpha\r\nbeta` on disk,
`alpha\nbeta` from `memory_show` — until the first tombstone, restore,
rename or update re-dump silently made the readers' version permanent.

`dumps` now applies the same per-line strip, so the two agree from the first
write. Mirroring the operation rather than approximating it with a
`replace("\r\n", "\n")` is load-bearing: under a plain replace,
`alpha\r\r\nbeta` lands `alpha\r\nbeta` on disk and still reads back
`alpha\nbeta` — the identical bug, one carriage return deeper. A test asserts
the general property, that `dumps` output is a fixed point of `loads`.

Separately, U+0085 (NEL) is a YAML 1.1 line break, and pyyaml emitted it raw
inside single-quoted scalars, so the loader folded it back to a space. Every
frontmatter string was affected — an episode takeaway, a tombstone's
`removed_reason`, a link note. Strings containing it now serialise
double-quoted, which round-trips.

### Fixed — `consolidate --llm` merges dropped the duplicate's scopes

The non-LLM dedup path merges them, with a comment explaining why:
similarity is scope-blind, so two near-identical bodies in disjoint project
scopes cluster well over the threshold, and a keeper that does not inherit
the duplicate's scope is invisible to that project's auto-scoped retrieval.
The LLM path seeds its clusters from the same scope-blind pass and did not
merge, so `consolidate --llm --apply --yes` silently removed the fact from
one project. Measured: the same pair keeps one hit through `consolidate
--apply` and zero through the LLM path.

### Added — `doctor` reports memory bodies that end mid-sentence

`memory_parse_health` answers "does the frontmatter parse", which is a true
statement about file structure and says nothing about whether the content is
whole. A body that arrives already truncated from the caller — an interrupted
tool call, a model that hit its output cap mid-argument — round-trips
byte-exactly and is reported healthy by every check. One memory in the
maintainer's store sat cut off at "The whole security/red-team stack (Hak5"
for ten days with a clean bill of health; the lost tail held a correction
another memory pointed at, and it is unrecoverable.

To be clear about the cause, since it was an open question: **no code path in
bettermemory truncates a body.** Every size cap raises rather than trims,
every write path was traced, and the event log shows an ordinary
`memory_update` carrying `fields: ["content"]`. The store persisted exactly
what it was handed. What was missing was any surface that would say so.

`models.looks_truncated` is the predicate — the last non-whitespace character
is not sentence- or structure-terminal — and the new `memory_body_completeness`
check reports the ids that trip it. Measured on the maintainer's 234-record
store: 1 false positive (a bullet list ending in a bare word), 93.9% recall
against mid-body cuts. It warns and names ids; it never fails, and it is
deliberately not a write-time gate — that would cost a parameter and a
description sentence against 127 characters of resident-surface margin. See
`docs/ROADMAP.md`.

### Added — a commit-message standard, and a linter that enforces the mechanical half

The log had drifted into first-person narration and aphoristic subjects — entries
that read as a diary of the session rather than a description of the change.
`perf(footprint): stop paying for prose and titles nobody reads` is a slogan;
what a bisecting reader needs is which surface shrank and by how much.

[`tools/commit_lint.py`](tools/commit_lint.py) encodes the part of the standard
that can be decided without taste: the Conventional Commits envelope, a 72-char
subject in the imperative mood with no trailing period, a blank line before the
body, 100-char body wrapping, and two wording rules — no first-person narration
and no conversational filler. Tone is not machine-checkable and stays in prose in
CONTRIBUTING.md, enforced by review.

Two things keep the gate from becoming friction. Quoted material is exempt, so a
commit that records the literal query a search was run with
(`memory_search("how do I cut a release")`) is a citation rather than a
violation. And only the commits a push or pull request *introduces* are graded —
history written before the rules existed is never re-linted, because failing
every run over something nobody can fix teaches people to bypass the gate.

The new `commit messages` CI job runs the linter over the event's range with
`fetch-depth: 0`. An unresolvable range (a force push, a first push, a shallow
clone) reports and exits 0 rather than blocking. For local feedback before the
commit is recorded, `git config core.hooksPath .githooks` installs the
`commit-msg` hook, which runs the same rules.

### Removed — 198 abandoned workflow branches

Prior multi-agent runs left one `worktree-wf_*` branch per agent behind after
their worktrees were pruned. Every one was verified patch-equivalent to main
(`git cherry`) or, for the single commit git could not match on patch id, checked
line by line against the tree before deletion.

## 3.32.0 - 2026-07-31

### Changed — the always-resident surface is ~12% smaller, with nothing taught less

Every tool description and schema this server registers is sent on every turn,
whether or not a single memory tool is called. That cost was 38,887 chars; it is
now 34,212 — and the toolcost benchmark's own measure of the served payload drops
38,424 → 33,714 bytes.

Two independent cuts. **Pydantic annotates every parameter with an auto-generated
`title` that restates the parameter's own name** (`"title": "Include Bodies"` next
to `include_bodies`); no client reads them and the SDK offers no way to suppress
them, so they are now stripped from every served schema after registration —
2,812 chars across the lean surface, 4,219 across the full one. The scrub is
structure-aware rather than a blind recursive walk: values under `properties` and
`$defs` are keyed by caller-chosen names, so a parameter genuinely named `title`
would otherwise be deleted from the wire. None exists today; the guard is for the
one that eventually does.

**Roughly 1.9k chars of duplicated policy prose came out of the descriptions.**
The rule applied throughout: prose earns its place only if nothing else teaches
the same thing at the moment it is needed. Where a write gate already refuses with
a hint naming both the remedy and its override flag, the description restating
that remedy was paying rent twice. Where it did not — `memory_verify`'s
`verified_*` REPLACE semantics bite on a *successful* verify, so no refusal can
ever teach them — the prose stayed. Every removed line was traced to what carries
it now; the descriptions still name every status, and the reject hints did not
grow to compensate.

Behaviour is unchanged and was checked rather than assumed: an independently
written stripper reproduces the served schemas exactly, `properties` order,
`required`, `type` and `additionalProperties` are identical on all 45 tool legs,
structured output stays enabled, and the guard test re-runs the same validation
the MCP client performs on every structured result — so a client accepts and
rejects precisely what it did before.

The description ceiling ratchets 27,500 → 26,000 and the schema-remainder ceiling
10,000 → 7,500, so the space cannot quietly refill.

### Added — read a journal without paying for every body

`episode_search` returns full bodies, which is right for reading one session and
wrong for the thing the tool is mostly used for: scanning takeaways across many.
Two additive parameters make the cheap read possible. `include_bodies=False`
drops the `body` key from each row; `ids=[...]` fetches specific episodes back,
and — because naming episodes is deliberate cross-worktree intent, the same rule
`swarm_id` and `parent_session_id` already follow — bypasses worktree scoping.

They compose into scan-then-fetch: page takeaways cheaply, then pull back the one
or two bodies worth reading in full. Measured on a 138-episode store, a 10-row
page drops from 50.2 KB to 4.8 KB and a 20-row page from 84.1 KB to 8.9 KB, with
a single body fetch adding back ~3 KB. That is roughly a 90% cut, but the honest
framing is 4-10x rather than 50x: the saving is bounded by takeaway length, and
takeaways are not short in practice — `max_takeaway_bytes` is 4 KB and writers
use it. Both defaults preserve existing behaviour exactly.

### Added — episodes are the state channel, and the docs now say so

The live store's single most-used memory is an audit-loop state blob, retrieved
135 times. That is not an accident of one loop: the no-state write policy told
callers not to put run-state in the fact layer without giving them anywhere else
to put it, so state was rephrased until it cleared the durability gate and landed
there anyway. Episodes have been the right home since they shipped; nothing said
so at the moment of the decision.

`DESC_EPISODE_PROMOTE` now states the convention in one line — loop and working
state belong in episodes; session close is when to promote the takeaways that
hardened — with the worked three-call flow in `docs/api.md` and the loop steps in
the plugin skill rewritten to match. Guidance only: no schema change, and the
existing state memory is left where it is, because the loop depends on it.

### Added — journal growth is visible before it becomes a problem

Episode GC (`prune_old_sessions`, 30-day TTL) fires only on `episode_write` and
the CLI, so a read-only loop never collects anything and the journal grows with
nothing reporting it. `memory_health` now carries `episode_volume`
(`sessions`, `episodes`, `bytes`, `prunable_sessions`, `ttl_days`), surfaced in
the CLI text and JSON output and in the web health page, with a warn tile when
sessions are collectable.

It stays off the hot path deliberately: the gauge is computed in
`report_for_directory`, not `compute_health`, so no per-turn tool walks the
episode subtree — a test asserts `volume()` has no call site outside `health.py`
and fails if one appears. Measured on the live 164-episode store at 1.6 ms, 0.6%
of a health report. The prune predicate is stated once and shared with
`episodes prune --dry-run`; the gauge evaluates the same rule inline to keep the
scan single-pass, and a parity test holds the two copies together.

### Added — a retrieval that nothing settled is now recorded, not discarded

A use-token that reached its 30-minute wall-clock eviction with nothing having
settled it — no Stop-hook attribution, no explicit `memory_record_use`, and no
in-process auto-commit because the session went idle — used to vanish on a bare
`del`. Downstream the memory then read "retrieved, never applied", which is
exactly the shape dead-weight curation punishes. The evidence had been thrown
away, not withheld, and nothing said so.

`SessionState` now stashes evicted tokens the way it already stashes expired
pending writes, and the handler layer drains the stash and records one batched
`use_token_expired` event (`ids`, `age_seconds`, `turns_since_issue`, `reason`).
It deliberately carries no `outcome`/`auto`/`attribution`: an expiry is the
*absence* of evidence, and settling evidence-free leftovers as `applied` would
manufacture endorsements out of precisely the retrievals that earned none —
poisoning the cold-endorsement signal and suppressing dead weight wholesale.

The drain subtracts all three settling surfaces before reporting a loss, and two
of them are invisible to a naive log scan. A retrieval the Stop hook settled
before an idle gap is evicted *before* the dedup purge can see it; and an
explicit `memory_record_use` writes its `use` event only *after* `_advance_turn`
returns, so no amount of scanning back through the log can find it. Both were
reproduced, and without the fix the append-only log permanently held one
retrieval as simultaneously settled and lost.

### Added — curation no longer reads a missing Stop hook as rot

Dead weight means "retrieved but never applied", and settlement is overwhelmingly
the Stop hook's job. On a store whose hook was never wired, every retrieved
memory therefore looked like dead weight, and the machinery said so with total
confidence across three surfaces — `memory_health`'s bucket, `curation_pending`'s
`dead` count, and the unattended demotion pass that can retag memories to
`ambient` on disk.

All three now gate on whether the event log carries any Stop-hook settlement
telemetry at all, via one shared predicate. Hookless stores get an empty bucket
plus a `telemetry_coverage` field saying why, and the mutating pass refuses with
a reason rather than silently retagging. The signal is deliberately narrow: an
in-process `memory_audit_turn` emits `triggered_from="mcp_tool"` and does not
count, because it is not evidence the hook is wired. Endorsement reporting also
splits by attribution tier (model / hook / auto) — `endorsement_ratio` no longer
conflates a hook's containment phrase-match with model deliberation. The
published `endorsement_rate` and `memory_helped_rate` are untouched.

### Added — a SessionStart hook, so a fresh session starts oriented

The plugin now ships a `SessionStart` hook running a purpose-built
`bettermemory session-start`, which prints what is stored for the current
repository. A new session sees its scope counts with zero tool calls.

It is built to be cheap and inert: counts come off the SQLite index rather than
`load_all`, it reads config directly instead of constructing a `Store` (whose
initialisation mkdirs, chmods and can trigger a full index rebuild), it stays
silent on an empty store, an untrusted index, or nothing in scope, and it
**records no events at all**. That last one is a hard mandate with a standing
test, because a fresh session id written from a hook manufactures phantom
sessions in doctor's census and hijacks the in-process session anchor. `doctor`
gained a check for whether the hook is actually wired.

### Erratum (2026-07-31)

The entry above is left as it shipped. Three of its footprint numbers were
measured one commit too early: the resident total is **34,450**, not 34,212, and
the toolcost figure is **33,960**, not 33,714. The description cut was
1,625 chars, not the ~1.9k the entry implies.

The cause is the erratum's own subject. Phase 6's closing follow-ups restored
`status="stale"` to the `memory_update` and `memory_verify` descriptions and
re-pointed `memory_write`'s `duplicate` bullet at its hint — +238 chars, landing
in the same commit as the numbers but after they were taken. Nothing caught it,
because the footprint baseline is diagnostic by design: a stale row is not a
failing row. The published surfaces (`README.md`, `docs/internals.md`, the
toolcost artifact and its README) carried the pre-follow-up figures until a
recon pass recomputed them for the next session's brief.

The shape of the claim is unchanged — the resident surface did shrink by roughly
12%, and both ceilings still hold with room to spare. What was wrong is the
precision, in a project whose rule is that every published number traces to a
committed artifact. The artifact
(`bench/toolcost/results/bettermemory-2026-07-31.json`) has been re-generated
against the tree it actually describes and now records `3.32.0` rather than the
`3.31.1` it was stamped with.

## 3.31.1 - 2026-07-31

### Fixed — `pip install bettermemory` works again (it did not, for any version)

`mcp` was declared as `mcp>=1.0.0` with no upper bound, from the initial commit
onward. mcp 2.0.0 — published 2026-07-28, four minutes after 1.29.0 — deletes the
`mcp.server.fastmcp` module that `builder.py`, `handlers/_shared.py` and
`session.py` import by path. A fresh install therefore resolved 2.0.0, installed
without complaint, and raised `ModuleNotFoundError` on `import bettermemory`. The
process could not start, so this was total rather than degraded: no CLI, no
server, no frame sent to a client.

Because the constraint is baked into the metadata of every wheel already on PyPI,
this was not confined to the newest release. **Every published version was
un-installable**, including ones that were correct on the day they shipped — 3.30.0
resolves mcp 2.0.0 today and fails identically. That removed the usual escape
hatch of pinning to the previous release, and is why this went out as a hotfix
ahead of scheduled work.

The dependency is now capped at `mcp>=1.0.0,<2.0.0`. Supporting mcp 2.x means
porting `FastMCP` to its successor `mcp.server.mcpserver.MCPServer` across those
modules; that is deliberately a separate change, on the reasoning that a rushed
port of the SDK the whole tool sits on is worse than a supported 1.x for a few
more days.

### Added — CI installs from the declared constraints, not from the lockfile

All nine CI legs were green throughout, which is the part worth explaining. Every
job installs with `uv sync`, which obeys the committed `uv.lock` and its
`mcp==1.27.0` pin. The suite thus verified — on three platforms, four Pythons and
both embeddings extras — a resolution that no user of the published package
receives. A lockfile records a resolution that worked once; `[project.dependencies]`
is the contract new installs resolve against, and nothing here had ever exercised
the second one.

The new `install from declared constraints` job builds a bare venv, runs
`uv pip install .` with no lockfile, then imports the package, calls `build_server()`
and runs the CLI entry point. Not reading the lockfile is the whole mechanism, and it
makes the job a forward alarm rather than a regression test: resolving the declared
constraints afresh takes the newest allowed version of everything, so the next
upstream major that breaks us goes red on an ordinary push instead of on a user's
machine after a release. It follows that the job can go red without any local change
— that is the design, and the fix is a considered constraint edit or a port, never
deleting the job. It runs inside the reusable workflow `release.yml` gates on, so
installability is now part of the publish gate. (The command also spells out
`--resolution highest`, which is uv's own default — written explicitly so a future
config default cannot disarm the alarm quietly, not because it changes behaviour.)

Verified in both directions before shipping: against `HEAD` as published the smoke
test exits 1 with the `ModuleNotFoundError`; with the cap it resolves mcp 1.29.0 —
newer than the lock's 1.27.0, so the guard also covers minor movement the locked
legs cannot see — and exits 0.

Postmortem: [`docs/incidents/2026-07-31-mcp-2-unbounded-constraint.md`](docs/incidents/2026-07-31-mcp-2-unbounded-constraint.md).

## 3.31.0 - 2026-07-31

### Added — search snippets show why the hit came back, not what the memory opens with

A hit's `snippet` was a blind head-of-body truncation. On a long memory the terms
that actually matched routinely sit past character 200, so the caller was handed
the opening of an unrelated paragraph and had no way to see why the hit ranked at
all. The snippet is now windowed on the matched terms.

The window is found Python-side, over the RAW body. SQLite's `snippet()` is a
dead end here: the FTS table indexes the preprocessed token stream, so it returns
stemmed soup rather than prose. Nor can the normalised text be used to locate
anything — NFKC, lowercasing and diacritic folding are all length-changing, so
offsets taken there do not map back. `_query_biased_snippet` walks the body with
the tokenizer's own regex and normalises each raw token individually, which makes
membership in the hit's `match_terms` the same predicate that put it there.
Symbol-aliased terms (`C++`, `.NET`) are invisible to a token scan because their
source characters are not word characters, so those anchors are found by
re-running the alias patterns over the raw text.

Scoped to one call site. `snippet_for` keeps its head-of-body contract for every
other consumer — write-time dedup, consolidate summaries — none of which have a
query to bias toward. Every fallback delegates to it rather than re-deriving it:
a body inside the budget, a hit with no literal match terms (browse mode, and
semantic hits that matched by paraphrase alone), a match that landed on a scope
rather than the body, or an anchor already inside the head window. That last one
is what keeps head-anchored hits byte-identical to what they were.

Total length stays inside the bound a head truncation had, by charging the
leading `...` against the content budget instead of adding it on top. The scan
costs one tokenizer call per raw token, bounded to the first 8,000 characters and
paid only for bodies longer than the budget — and only on RETURNED hits, at most
`max_results` of them, never over the candidate pool. Both facts are pinned by
tests rather than asserted, including a call-count test, because the existing
per-search tokenizer budget test happens to use 46-character fixtures and would
not have noticed.

Two visible consequences, both intended. The silent-miss audit trace retains the
hit's snippet, so a retained trace now carries the query-biased window — replay
parity holds, since the probe re-runs the same search over the same message. And
the web UI derives a result card's title from the snippet, so a mid-body window
puts a leading `...` there; that is the only place the snippet surfaces in the
UI, so the ellipsis is doing real work — it marks the line as an excerpt rather
than the memory's opening.

### Added — what the FTS prefilter costs above its threshold, finally measured

Every retrieval number this project has published was measured below the
500-memory index threshold, where the prefilter never engages and the full corpus
is ranked. `bench/retrieval/README.md` said so about itself and named driving the
real handler path as the missing increment. `--pad-to` did not close it: padding
grows the corpus, but `run_arm` calls `search.search` on a memory list, so the
pool was still everything. Every artifact dated before now measures *dilution*,
not prefiltering — honest upper bounds and nothing more.

`--prefilter {off,on,both}` now picks the code path, separately from `--pad-to`
picking the corpus. `on` drives production's own `resolve_search_pool`, so bm25
nominates the pool and the corpus-IDF provider prices the terms — the part a
hand-rolled harness would omit, and omitting it scores a capped pool with
collapsed pool-derived IDF.

**The measurement refuses to run blind**, which is the part that makes it worth
anything. Seven paths quietly return the full corpus — six inside the loader,
plus the cap-starvation reload one layer up in `resolve_search_pool`, which is
why the arm passes no scope, repo or worktree filter — and a run that hit any of
them would print full-corpus numbers under a `prefilter: true`
heading — indistinguishable from "the prefilter costs nothing". So engagement is
checked per query against the corpus-statistics provider, which is attached if
and only if the FTS path served the pool, and the runner exits non-zero with an
index census if a single query fell back.

Measured on the canonical corpus padded to 600 (production's real threshold, no
override) and again on the unpadded 180 with the threshold forced down, which
reaches the same code path without filler — padding is a confound, since 420 of
the padded corpus's documents are deliberately off-domain and bm25 will never
nominate them. **Recall@5 loss is exactly zero in all six cells**
(`bench/retrieval/results/prefilter-above-threshold-2026-07-30.json`,
`bench/retrieval/results/prefilter-forced-180-2026-07-30.json`).

The reason is narrower than "prefiltering is free", and the README says so: bm25
does drop the gold document on 5–10% of casually-phrased questions, but those are
questions the full-corpus ranker also failed at k=5. The nomination ceiling is
real and sits above the recall either arm reaches, so it never binds. On a corpus
where the ranker reached 90%, a 90% nomination ceiling would cut in directly.
Lexical arm only — the measuring machine has no embeddings extra, both artifacts
record that, and the semantic half of the relevant prediction stays unscored.

### Changed — read-side diversification was measured, and the evidence it exists to rescue is invisible to it

`bench/longmemeval/README.md` explained this project's largest remaining
retrieval error as a coverage problem. Temporal-reasoning and multi-session
questions lose recall the same way — some of a question's evidence lands in the
top 5 and some does not — and the stated cause was that such a question carries
vocabulary for two events living in two sessions, so `score_memory`'s
`0.5 + 0.5 * coverage` multiplier cannot be satisfied by either one alone. The
prescribed repair was a read-side re-ranker rescuing the dropped co-evidence,
estimated at **+3.2 pooled recall@5** — larger than the entire
semantic-vs-lexical lift, and with no embedding model.

That estimate came from a throwaway re-run, because the runner persisted
`by_type` aggregates only. So the first thing built was per-question records,
and a both-arms baseline against unmodified ranking, committed before any
ranking edit existed (`bench/longmemeval/results/baseline-both-arms-2026-07-30.json`).
It reproduces every published figure exactly — pooled 89.35 / 91.85,
temporal-reasoning 83.72, multi-session 84.87 — and the README's
partial/complete table to the tenth of a point.

Those records then refute the diagnosis. `coverage_probe.py` takes each dropped
evidence session's best-ranked hit and counts the matched query terms it carries
that the surviving top 5 does not carry between them. Of 87 dropped sessions,
**81 carry none at all** against the generous reference set and **74 against the
strict one** — the headline moves nine points between two defensible
definitions, so both ship
(`bench/longmemeval/results/coverage-probe-2026-07-30.json`). The dropped
evidence is not answering a different half of the question: like for like it
matches *fewer* terms than the survivors that beat it (median 2 against 3), a
strict subset of what the head already carries, and it loses on scoring at a
median item rank of 13.5. In 337 of 500 questions the top 5 already carries every
term anything in the corpus matched, which is the structural reason there is so
little novelty to find.

What closes the item is not the sweep but the ceiling. The probe bounds an
**omniscient** rescue — one promoting exactly the novel-carrying dropped
evidence, with zero false promotions, which nothing real can beat — and reports
it against every novelty reference, including a `blind` one that promotes all 82
ranked dropped sessions and asks nothing. Loosening the test raises the ceiling
(+0.33 → +0.79 → +2.59) only by converging on blind promotion's +5.21, and the
one reference whose ceiling clears the gate has **precision below blind
promotion's** — it has stopped filtering, not started finding. The best novelty
signal available is a 1.22x lift on a 4.5% base rate, where clearing +2.00 needs
something nearer 25–30%. The mechanism is impossible here, not merely untuned,
and that conclusion does not depend on which reference you prefer.

Measured rather than argued from there. The rescue was built the shippable way —
a bounded marginal-coverage bonus applied to hits carrying terms the head of the
ranking misses, then a re-sort on the existing `(score, created, id)` key, so the
list stays monotone and `top_hit_leads_runner_up` keeps working — and its
parameters were chosen on a held-out half of the corpus. Twenty-nine
configurations were scored offline against the captured pre-trim rankings; the
dev-selected one was then run for real, and reproduced the offline prediction to
four decimals on the pooled figure and all six per-class figures. Pooled
recall@5 **0.8935 → 0.8941: +0.06 points against a pre-stated gate of +2.00**
(`bench/longmemeval/results/co-evidence-rescue-2026-07-30.json`). The effect is
four questions out of five hundred, one up and three down; evidence-weighted
micro recall moved backwards, 0.8671 → 0.8650. The ranking change is reverted.
The baseline, the probe, and the negative result stay.

**The headroom is real and stays open**, which is why this is a closed item and
not a closed question. Perfect rescue of evidence already inside the first 10
distinct sessions would be worth +5.0 pooled, and every class ceiling at k=5 is
~100%. What this measurement establishes is only that term coverage is the wrong
lever — the ranking already has the evidence in hand and orders it wrongly for
reasons the matched-term set does not express.

### Added — relative citations finally get path protection, anchored to their own worktree

`detect_path_drift` excluded relative paths by design: without a root, checking
them would mean stat-ing the reader's cwd, which is meaningless. The cost of
that exclusion was measured on 37,635 claims across 30 repositories — the path
leg fired **exactly zero times** in the relative-citation arm, with thousands of
real deletions in front of it. The citation style people actually write got no
protection at all.

A memory records its own `origin.worktree_root`, so the root is not unknown —
it just was not being used. Relative citations now resolve against it before
the existence check. The anchor regex could not be reused as-is: it is
deliberately over-matchy because a phantom commit-drift anchor touches no commit
and is verdict-neutral, but existence checking inverts that — a matched bare
domain like `pypi.org` would stat as missing and fabricate drift. A filter layer
requires a real directory segment, rejects host-shaped first segments and
placeholder paths, and reports missing only when the parent directory exists.

Measured on the same corpus (`bench/rot/results/multirepo-anchored-2026-07-30.json`,
a fourth arm appended — the frozen `multirepo.json` and scorecard are untouched,
so no published prediction was regraded): **precision 1.000, false-alarm rate
0.0, one alert per catch**, J 0.000 → 0.032 pooled, and 19 wins / 0 losses / 7
ties repo-to-repo. The recall is small — 0.73% of claims against a 22.9% base
rate — and that is the honest headline: this does not find most rot, it makes a
dead leg fire precisely on the subset it can prove.

The cross-host case is what would have made it dangerous. A store synced from
another machine carries a `worktree_root` that does not exist locally, and
checking against it would mark every citation missing at once. The check fails
open when the recorded root is absent or unstattable — a deliberate, scoped
reversal of the bias `origin.py` records for files underneath a live worktree.
That also closes a latent bug: a recorded empty `worktree_root` used to become
`Path(".")` and anchor attestations against the reader's cwd, which is precisely
what the relative exclusion existed to prevent.

### Changed — only claim-anchored evidence escalates the staleness verdict

`PathDriftReport.missing` merged two populations with very different track
records: misses from paths the memory itself attested, and misses from paths
extracted out of prose. The in-tree measurement is stark — prose-extracted path
alerts run about 0 of 15 real, attestation-anchored alerts 3 of 3 — and
`has_drift` treated them identically as the only path input to the verdict.

Path drift now carries provenance. Only anchored misses, plus the newly
checkable relative citations, feed verdict escalation; prose misses stay on the
wire as advisory evidence. Nothing is hidden from the caller — only what
*escalates* changed, which is the distinction this whole release is built on:
surface evidence, let the reader judge.

The commit leg was the other candidate for removal from the escalation
disjunction, gated on pooled alerts-per-catch reaching 1.5. It does not, so it
stays, and the switch is isolated at one named place with a test pinning today's
behaviour. The demotion branch — calendar-stale plus a *measured* zero reading
fresh — is untouched and separately pinned; removing it would resurrect the
constant-function defect that `docs/incidents/` now documents.

### Changed — the relevance label stops measuring length

The `relevance` field was computed from lexical coverage, and the code said so
about itself: "measuring LENGTH, not relevance". A pure-semantic paraphrase hit
has no matched terms, so it was labelled `low` — while the tool description told
callers to treat low as noise and `expand_top` refused anything not `high`. The
4x-cost semantic leg was suppressed by the one field callers branch on.

Hits now carry `matched_leg` (lexical / semantic / both), which reports why a hit
surfaced instead of asking the caller to infer it, and a semantic-only hit is no
longer labelled by a lexical statistic it cannot have.

### Changed — an unattended consolidation pass cannot tombstone a contradiction

Negation detection was whole-body token presence, so "Deploy with the blue-green
strategy; never do in-place." and its exact inverse scored Jaccard 1.0 with no
polarity flip detected, and the keeper-picking pass would have tombstoned one
side. Pairs showing negation or numeric-divergence signals now route to the
conflicts queue only, structurally unreachable by the keeper pass rather than
guarded at the call site — a fence you can forget to call is not a fence.

### Added — `memory_verify` reports symbol drift, advisory only

83 of 194 memories in the reference store cite backticked symbols, and no
production code ever resolved one against a file; the machinery that scores
J≈0.99 is bench-only and its parser reads 0% of real prose by construction.
`memory_verify` now AST-checks the citation shapes it can parse and reports
`symbol_drift`. It feeds no verdict until a bench measures its precision, and
`Store.mark_verified` stays a policy-free persistence primitive.

### Fixed — `ingest --force` was a silent no-op, and three more write-path holes

`--force` documented itself as writing a duplicate the store already holds.
Plan-time it did admit the row; apply-time the shared dedup gate refused it
again, with a hint telling the operator to pass the flag they had just passed.
CI stayed green because the guard test asserted the *plan* and never applied
it — a test that asserts an intermediate artifact does not test the behaviour a
flag promises. `apply_ingest_plan` now takes `force` and drops the active-dedup
gate from its tuple rather than setting the context's `force` bit, because that
bit is read by the tombstone gate too and would have resurrected tombstones —
an asymmetry ingest documents and now proves end to end.

Ingest's scope-mismatch gate refused realistic imports on any non-empty store:
imported rows carry only provenance and type scopes, so an auto-memory file
naming its own project tripped the project-name check. All four of its gate
tests ran against empty stores, where that check is structurally disabled and
cannot fire. Ingest is a user-initiated bulk import of the user's own prior
memory files, not a model mis-tagging a conversational write, so it now
acknowledges the mismatch — and a test pins that the acknowledgement has exactly
one reader, so it cannot quietly become an amnesty for gates added later.

`memory_write_confirm` replayed its staged payload through zero gates: a
duplicate or a tombstone that landed during the one-hour TTL committed
unchecked. It now re-runs the credential and both dedup gates. The obvious
implementation is wrong — the lookup pops, so a refusal after it destroys the
staged write it was protecting — so the pending write is peeked, judged, and
only consumed on success; a refusal returns the normal status plus
`pending_retained` and the still-valid id, and leaves the promotion source
episode on disk. The staging call's `force` and acknowledge flags are persisted
with the pending write, so a write forced at stage time is not re-refused at
confirm time.

Pending writes were an in-process dict that died on restart with no event. They
now persist to a `0600` sidecar beside the store, keyed per client so one client
still cannot confirm another's staged write, with the TTL and the
expired-versus-never-existed distinction enforced on load.

### Added — `memory_write` classifies claims about the user by body, not by label

`PendingGate` triggers on the category *label*, so a claim about the user
written as `category='fact'` committed instantly and the staging flow whose
entire purpose is the user's veto never ran. The content-shape detector that
could have caught it existed, was tested, and was wired only into the Stop hook.

A new gate classifies the body. It cannot reuse that detector: it matches
first-person shapes only, because it was built to read the user's own words,
while a model-authored `memory_write` claim is usually third person — "Mattias
prefers tabs", "the user prefers…". The gate composes the existing pattern with
third-person shapes rather than editing it, since the extractor's own tests pin
its behaviour, and it applies them the way production does: per sentence, after
smart-apostrophe normalisation, or the anchored possessive branch and every
curly-quoted body silently stop matching.

The blast radius needed deliberate work. `CONTENT_GATES` is derived by
exclusion, so a new gate joins it automatically — which would have made ingest
refuse imported preference files, and made proposal acceptance refuse exactly
the explicit captures ("remember that I prefer X") the extractor deliberately
stamps as `fact`. Both are now excluded by name, with the reasoning recorded at
the definition: neither has a human present to flip a flag.

The gate is precision-first and porous by the same trade the transient and
credential gates make — a nominalised "<Name>'s preference is tabs" passes, and
widening to possessive-plus-noun would refuse "the parser's preference is the
longest match". Acknowledged writes record the phrase they overrode, so the
entry ticket for revisiting the pattern is override-rate telemetry rather than
taste.

### Changed — `memory_proposals` accept runs the same gates as every other write

Accepting a proposal ran one gate of six: a hand-rolled credential scan, while
the commit that introduced the shared chain described these copies as
"deliberately stricter". It now runs the shared content gates. This is a
deliberate behaviour change — a proposal that near-duplicates an existing memory
was previously the reviewer's problem and is now a `duplicate` refusal — so the
tool and the CLI grew the matching overrides, the refusal still fires before the
queue removal (a refused proposal stays queued), and no event is recorded on
refusal. Consolidate's copy is untouched: its stamped-versus-unstamped scan
split is a measured decision that one gate context cannot express.

### Added — `[behavior] min_content_tokens`, default 0

A minimum body length, off by default. It is hardening rather than a closure:
`content="x"` is a legitimate fixture in dozens of handler-path tests, and the
only in-repo floor precedent sits an order of magnitude above what this corpus
treats as valid. The validator it lives in also serves `memory_update` and
proposal acceptance, so the three-tool blast radius is documented, and a
structural test requires every call site to name the parameter explicitly —
opting out is a decision someone writes down, not one they fall into.

### Added — a number on a resident surface must now cite a committed artifact

`tests/test_doc_claims.py` closes the mechanically-decidable slice of "prose
asserts something the code does not do" — paths, symbols, test counts, line
refs, file counts. Numbers were its one structural blind spot, and that is
exactly where every surviving false claim had collected. `test_number_claims.py`
closes it: a number presented as a measurement on a tool description, the
server instructions block, README, `docs/internals.md`, or `doctor`'s prose
must be derivable from a bench result committed to this repo.

The discriminator is the word "measured", not a percent-or-bytes pattern. A
naive shape rule false-positives on almost every number in the descriptions,
because those are contract constants enforced by adjacent code — the episode
frontmatter ceilings, the note and excerpt caps, the groundedness threshold —
and a checker with false positives gets switched off. One narrow cue-free rule
covers byte and ratio figures on the two documents that state this project's
own footprint, because the README's cost claim carried no cue at all and a
cue-anchored guard would have missed the defect that shipped through it. Byte
figures a cap word actually governs are exempt, tested for *adjacency* rather
than presence: the pre-sync README bullet said "cost ~35 KB … the description
half of that is capped in CI", where the cap governs a different quantity one
clause away, so a presence test would have blinded the guard to the very claim
it was built to catch.

The allowlist is empty, deliberately. The guard found two claims the
truth-sync pass had missed, both in operator-facing `doctor` prose — a
threshold comment citing a measured scope spread that was never captured, and
a fix hint asserting a perfect rate on rare-term queries that no artifact
backs, in a string the check prints to humans. Both were repaired rather than
exempted; a guard born with its findings allowlisted protects nothing. It is
pinned against the pre-sync tree as a fixture: 11 findings there, 0 here.

### Added — the resident footprint has one number, and a ceiling on the part nothing capped

Three budgets were each policed and nothing summed them: the instructions
block, the lean description total, and the toolcost bench. The served JSON
schemas and the plugin skill frontmatter were governed by nothing at all.
`tests/test_resident_footprint.py` records the aggregate — instructions 1,608
+ descriptions + inputSchemas + outputSchemas 1,770 + skill frontmatter 759,
**37,548 chars** when the ceiling was set and 38,167 after the write-path work
below spent 93 chars of the reserve budgeted for it — and puts a ceiling only
on the previously-uncapped remainder, so an edit cannot fail two budgets under
two different recalibration rules. The skill body is excluded and the test derives
why rather than asserting it: 13,688 chars that load on activation, not per
turn.

### Added — tests for `bench/rot/resolution.py`

The newest bench module had zero coverage, and it is the one that decides
whether this project may claim a real-world staleness-accuracy number. Its
parser reads the anchored corpus templates and 0 of 216 real memory bodies,
by construction — pinned now as a structural property, so a regression that
loosens the anchors and starts "parsing" real prose fails instead of quietly
manufacturing an accuracy figure. `resolution_rate` stays `null` (UNDEFINED,
not zero); a flip to `0.0` fails.

### Fixed — the resident surfaces carried a measurement the project had already retired

`DESC_MEMORY_SEARCH` told every model, every turn, that wording a query in
nouns was worth a "measured 10%→65% recall@1". No committed artifact ever
backed that pair. It came from an early live-store probe, and the blind
replication built later in `bench/retrieval` measures the asked baseline at
35%, not 10% — the bench README already said no number in that directory is
comparable to the old figures, which is another way of saying the old figure
had been retired everywhere except the surfaces that quote it.

The same measurement family was restated at seven more sites, two of which
were not docstrings: `doctor`'s `retrieval_discrimination` fix hint, printed
to operators, and `config.py`'s `DEFAULT_CONFIG`, which ships verbatim into
every user's `config.toml`. Two derived adjectives had gone false on their
own terms as well — "three times the cold-query hit rate" and "+15 points on
top of" describe 10→30 and 65→80, and the replacement artifact measures
35→60 and 80→90.

All eight sites now cite `bench/retrieval/results/v2-unpadded-2026-07-26.json`
with its easier-than-a-real-store caveat, and the two measurement families
are kept apart: query-discipline sites carry the lexical asked→re-queried
pair (35% → 80%), embeddings-extra justification sites carry the semantic
lift (35% → 60% asked, 80% → 90% re-queried). `DESC_MEMORY_SEARCH` itself
now carries the instruction with **no number**: the claim is only honest
beside its caveat, and a caveat is not what an always-resident description
should spend characters on. The measurement lives in the module docstring
instead. Net −2 chars on the lean budget.

Also corrected in the same sweep: README and `docs/internals.md` claimed the
default tool surface cost "~35 KB" with "the description half" capped in CI
and that per-turn tool context "stays small" — the project's own
`bench/toolcost` artifact measures 38,009 bytes of serialized `tools/list`,
of which 28,604 is names and descriptions (≈75%, not half), and 4.84× a
comparable tool. `docs/eval-results.md` carried a caveat that was false when
written (`turn_audited` has carried `top_hits` since 3.14.0, and
`--widening-preview` exists precisely to replay looser rules). Three stale
in-code counts ("the 25 tools", a "24-tool power-user surface", a 19+4 tool
split that is really 22+5), a `server.py` tool inventory missing six tools
while claiming to mirror the system-prompt addendum, an `Episode.scopes`
docstring describing an auto-defaulting that was never implemented, and a
`bench/rot/corpus.py` docstring advertising partial clones that its own
`clone()` documents as measured-wrong.

### Added — `docs/incidents/` has postmortems in it

The directory has advertised public postmortems since 2026-05-21 and
contained none — an index reading "(No incidents yet.)" under a README
promising them. Its charter was also scoped to rot bugs *reported against*
bettermemory, which no self-found defect could satisfy. The charter now
covers any check whose job is to tell you memory has gone wrong, reporter
neutrality is stated, and two postmortems are written from the record:
the staleness verdict that was arithmetically `always_flag` at shipped
defaults (Youden's J = 0.000, fixed in `58a4fa4`), and the doctor check
that reported green for precisely the store most needing its warning
(fixed in `316781e`, about nine hours after it shipped).

### Fixed — `memory_verify` accepted attestations it could not check

`Store.mark_verified` stamped `last_verified_at` and stored the caller's
`verified_*` lists verbatim, with no check of any kind — a memory could
read `fresh` on an attestation naming a path that never existed. The read
side cannot catch the absolute case on its own: an attested path is only
existence-checked when the body also names it.

`memory_verify` now refuses `verified_paths` entries the attesting machine
cannot stat. Relative entries resolve against the memory's own
`origin.worktree_root`; documentation placeholders (`/etc/foo`,
`/path/to/...`) are refused outright rather than inheriting the prose
validator's exemption; Windows drive-absolute forms (`C:\...`, `C:/...`)
are recognized as anchored. Shape claims — globs, templates, URLs, SSH
remotes, SMB shares, single-segment routes — are exempt, since a pattern
can be neither present nor absent, and `verified_absent_paths` is exempt
because non-existence is its claim.

The read side stays lenient on purpose: a memory attested on one host is
legitimately read on another that lacks the path (`sync`), so the
existence demand applies only at the moment of attestation. The check
lives in the `memory_verify` handler; `Store.mark_verified` remains a
policy-free persistence primitive, and a test pins that split.

### Added — `apply_write_gates()`: one gate chain, two write paths on it

`_WRITE_GATES` was reachable only from `memory_write`; three other paths
called `Store.write` directly with divergent policy subsets
(`ingest.apply_ingest_plan` ran no gates, `consolidate._apply_llm_proposal`
hand-reimplemented four, `handlers/proposals.accept_proposal` mirrored the
credential gate alone). The chain is now `apply_write_gates()` behind a
`GateDeps` protocol — satisfied structurally by `ToolHandlers`, or by
`GateBundle` from a bare `Store` + `Config` — sitting strictly above
`Store.write`; the locking rationale there is untouched.

`ingest.apply_ingest_plan` now runs `CONTENT_GATES` (every gate except the
confirmation handshake, which ingest bypasses deliberately: the source
file is the user's own act of commit, a tested contract) with all
`acknowledge_*` overrides off. Rejections surface per-row as
`skip_invalid` in the gate's own status vocabulary; previously a pasted
credential in an auto-memory file imported silently. The CLI threads the
real config rather than the `Config()` fallback, so the configured dedup
thresholds and the semantic-dedup switch reach the gates. The `[scopes]
allowed` allowlist does not: it is enforced by `_validate_write_payload`
in `handlers/_shared.py`, which ingest never calls — ingest builds its
`Store.write` payload directly — so an imported row can still land in an
unsanctioned scope, and the content-size and scope-count caps are missed
on the same path. Ingest is the only one of the four without an allowlist
check; `consolidate` hand-rolls its own.

`consolidate` and `accept_proposal` still hand-roll their subsets, and the
two are not comparable. `consolidate._apply_llm_proposal` diverges for
reasons it measured: it refuses hard, because an unattended run has nobody
to offer an `acknowledge_*` override to, and it scopes the transient and
similarity checks to the LLM-authored claim rather than the
provenance-stamped body — the stamp quotes the transcript verbatim, so it
carries transient phrasing of its own, and being shared by every proposal
citing the same turn it dominates the Jaccard sets (the measured overlap is
recorded at the gate). `accept_proposal` is the thin one: payload
validation plus a hand-rolled credential scan, one of the six content
gates. Putting it on the chain is unfinished work, not a policy stance.

### Changed — `episode_search` description trimmed to proportionate length

3,165 → 2,064 chars; lean default-on total 27,437 → 26,336 of the 27,500
ceiling. The removed prose was rationale `docs/api.md` carries in full;
every parameter, return-shape key, and cue pinned by
`tests/test_prompts.py` is kept. Gating low-use episode tools out of the
lean surface was evaluated and is not available — dependencies are
recorded at the episode block in `builder.py`.

### Changed — the read-side repair 3.30.0 promised was measured, and it is a dead end

3.30.0 retired the write-side commit-SHA transient marker and said the
better home for the one class it caught was read-side, "where a body-cited
SHA is a resolvable commit rather than a regex judging English." That
sentence is shipped history and stays where it is; this entry is the
correction, in the register `0fdf436` established — correct forward, don't
rewrite the record.

The proposed leg was to resolve a cited SHA at verification time: does the
commit still exist, is it still an ancestor of HEAD, how many commits
since. Designed and measured against the live 211-body dogfood store and
the 30-repository corpus behind `bench/rot`. All three rules fail on
arithmetic.

**Distance** (commits since the cited SHA) fires on **34 of 34**
SHA-carrying in-repo memories — min 3 commits, median 188, max 685, not one
token sitting at zero, so there is no threshold at which it goes quiet.
Youden's **J = 0.000**: arithmetically `always_flag`, the same pathology
this release cycle already found in the default operating point. And the
memories it would flip are exactly the SHA carriers already reading fresh,
so "the verdict is fresh and the body holds a hex token" reproduces its
entire output with no git calls at all. **Existence** changes zero verdicts,
and both its live fires are on permanently-true history — a release tag
moved during the 2.7.1 incident, and another repository's commit quoted
here. **Ancestry** fires zero times — every cited SHA that resolves here is
an ancestor of HEAD, 88 of 88 — and its answer is a property of local
`git gc` rather than of the project: the same SHA
resolves on the forge forever, resolves in the author's checkout until it
is pruned, and never resolves in a fresh clone.

The corpus is what actually condemns it, and it inverts the premise. Across
4,647 merged pull requests in 29 repositories, **3,573 head SHAs end up
unreachable from the default branch — and all 3,573 belong to work that
merged.** Under squash and rebase merge, "the commit you cited is gone" is
the signature of successful integration, not of rot. J = 0.231 pooled, 0.053
median, and exactly 0.000 in 11 of 28 repositories. Worth stating plainly:
bettermemory commits straight to main and reads 88 of 88 cited SHAs
reachable, so testing this locally would have shown a harmless 0% false
positive rate on a signal that is `always_flag` in eleven real repositories
— the mirror of `bench/rot` finding this repo a near-worst case for drift.

**The honest cost, not dropped:** the bare unhedged branch pointer 3.30.0
named as newly uncovered *stays* uncovered, and the calendar leg remains its
only backstop. The measured population is about one memory in 211. A larger
one would be new evidence, and the item would legitimately re-open — which
is why the record is a tombstone and two tests rather than a deletion.

### Fixed — four model-facing sites said `verified_commits` feeds the drift legs

It does not, and never has: `verify.py` reads the field zero times. The
claim appeared in the `memory_verify` tool description, the always-loaded
system-prompt addendum and its `docs/system_prompt.md` mirror, the plugin
skill, and a `memory_health` recommendation telling the model to
"re-anchor" with it. `docs/api.md` went further and advised attesting
commits *instead of* paths — steering the model away from the one
attestation the read path actually resolves.

The correction is a split, not a blanket: `verified_paths` **is** read back
— checked against the memory's own worktree, and used as the anchor that
narrows the commit-drift count — while `verified_commits` and
`verified_versions` are provenance for the next reader. Replacing one
falsehood with its mirror would have cost every path-attested memory its
only anchor.

Also fixed in passing: the plugin skill still described `spot_check_required`
as pre-empting the drift inputs, which `58a4fa4` removed in this same
release cycle.

## 3.30.0 - 2026-07-26

The window where the instruments turned on the product. 3.29.0 built
machinery to make this project's claims about itself checkable; this
release is what that machinery found, and it is not flattering.

Three of the four entries below are a mechanism failing a measurement it
had never been subjected to. The staleness verdict — the field the README
tells consumers to branch on first — was a **constant function** at its
shipped default, flagging every memory past the freshness window
regardless of what the drift legs found. The path-drift detector scored
95.7% against real deletions and fired **exactly zero times** in the
citation style developers actually write. And the transient-marker gate,
asked for the first time what its own event log said about it, turned out
to be **overridden on 45 of 47 blocks** for commit hashes — a speed bump
that had taught its caller the override reflex. That marker is now gone,
executing a tuning protocol the module had carried in its docstring, and
never run, since the day it was written.

None of this was found by review. Each came from pointing an instrument
at the thing and reading the number, which is the only reason any of it is
in a changelog rather than in the code.

### Added — attested relative paths are checked, against the memory's own worktree

`detect_path_drift` excluded relative paths by design: without an anchor,
checking one would mean checking the cwd at *retrieval* time, making a
verdict depend on where the reader happens to stand. The rot corpus put a
number on what that exclusion costs. Across 37,635 claims with 8,627 real
deletions in front of it, the path leg fired **exactly zero times** in the
relative-citation arm — while scoring 95.7% of gone-file claims at a 0.0%
false-alarm rate when the citation was absolute. Citation style alone
decided whether the detector ever ran, and relative is how a developer
writes.

A census of a real 206-memory store showed the cost in place: 136
memories carry attested `verified_paths`, **104 of them attest relative
paths**, and 72 memories were receiving no path check of any kind. Three
attested files were already gone with nothing surfacing it, and each had
failed differently — one deleted, one moved, and one whose attestation
was already wrong when it was made.

The cwd objection does not reach an *attestation*, because
`origin.worktree_root` is captured at write time and stored on the
memory. The anchor already existed; the five call sites all held the
memory object and none were passing it. They do now.

Scoped deliberately to `verified_paths` / `verified_absent_paths` and
**not** to relative paths in body prose: an attestation is an explicit,
reviewed claim, while prose is the false-positive swamp the original
exclusion was right to avoid. Two ordering traps are now pinned by tests
— anchoring has to happen *before* candidate validation, and again before
attestation normalisation, or the paths are silently dropped and the
`verified_absent_paths` escape hatch inverts into a permanent false
alarm.

### Fixed — the SHA marker never bucketed, so its own override rate was unmeasurable

`find_transient_markers` emitted `sha:<7-char prefix>` as the marker
*name*, while the comment beside it said "bucket all SHA hits under one
canonical marker". The `break` bucketed within a body; across bodies every
write minted a fresh name.

That defeated the one report built to answer the question. `marker_stats`
exists so "is this marker's override rate high enough to drop it?" gets
answered from telemetry rather than vibes, and it aggregates by name — so
the highest-volume marker class in the store sat as 89 events smeared
across 52 rows, largest n = 5. No row could ever carry enough evidence to
act on. Pooled, it was overridden on what looked like a coin flip against
0.169 for the phrase markers.

The emitted name is now canonical, mirroring the `as of <date>` marker
that was modelled on this one and got it right; the offending hash still
travels in the match's snippet, which is what the caller was already
shown. `canonical_marker` folds the pre-fix names read-side, since the
event log is append-only and is not rewritten.

Deliberately *not* decided here: whether that override rate meant the
marker should go. Taking the policy call off the same data in the same
commit is the tune-after-seeing lever this project's pre-registration
discipline exists to prevent. It shipped with a row it could accumulate
on — and the next section is what the accumulated row then said.

### Performance — one sort at the source, not one per memory

Found by pointing the rot harness at its first unfamiliar repository:
scipy would not finish, and a profile put 82 of 90 seconds inside
`compute_commit_drift`, in a single `sorted()`.

`commit_author_timestamps` documented its order as "whatever git emits —
callers that want ascending for bisect should sort explicitly", and all
four callers then did exactly that, because all four follow it with
`bisect_right`. Nobody wanted git's order. Three of them hoist the sort
out of their per-memory loop; the fourth *is* the per-memory call, so it
re-sorted the repository's entire history once per memory evaluated —
2,163 times at 38ms each, on a list its own caller had already sorted.

The sort was also slower than it looked. `%aI` preserves each author's
own UTC offset, so the list carries thousands of distinct `tzinfo`
objects (17,100 in a scipy sample); CPython's same-tzinfo fast path never
fires and every comparison makes a Python-level `utcoffset()` call.
Keying on the absolute instant pays that once per element instead of once
per comparison. Both sources now guarantee ascending order, pinned by
mutation-sound tests rather than by a docstring.

    scipy: did not finish in 20+ min -> 14.8s, 9,591 claims

The published 60-day report reproduces full-document identical after
these changes, which is what the pre-registration requires of anything
touching the instrument before results exist.

### Fixed — the staleness verdict was a constant function at the shipped default

`staleness_verdict` is the field this project tells consumers to branch
on first, and past the freshness window it was not a signal at all.
`compute_staleness_verdict` let a `never`/`stale` verification status
pre-empt **both** drift inputs, so every memory older than
`verification_stale_days` (default 30) reported `spot_check_required`
no matter what path drift and commit drift actually found. The legs
carrying all of the discrimination were unreachable in exactly the
configuration most users run.

`bench/rot` had already measured the consequence and named it the most
actionable finding it produced: the `shipped_default` arm flagged 100%
of claims in every class and both windows, Youden's J = 0.000 —
arithmetically identical to a detector that flags everything.

A calendar-stale memory now reads `fresh` when its commit-drift leg
returns a **measured zero**: no commit touched anything the memory
cites since its own `last_verified_at`. That is the question the
calendar leg is a crude proxy for, so the measurement wins and the
proxy yields — which is the division of labour `compute_commit_drift`
already documented from the other side ("calendar staleness remains the
backstop for that class"). Three guards keep the demotion from becoming
a false green: `never` never demotes (no anchor, so no "since when"),
`commit_drift_count is None` never demotes (the leg could not ask —
this is what keeps preference and lesson memories loud), and path
existence alone never demotes (on the live store, ~0 of 15 missing-path
alerts raised from body prose were real drift, against 3 of 3 for
anchored attestations). Drift still raises the verdict exactly as
before.

Re-measured on the same pinned windows, `shipped_default` moves from
J = 0.000 to **0.034 (60d)** and **0.111 (30d)**, converging exactly
onto the arm that had the calendar leg disabled — that convergence is
now a regression test. The other arms, the claim-level detectors and
the reference baselines reproduce identically. The ceiling did not
move: the default now *reaches* the existing weak signal rather than
the signal getting better.

`verify.verdict_from_signals` is new public API — the primitive both
emission sites now share, replacing a re-implementation in
`_response.attach_commit_drift_counts` that three separate "mirror the
gate in verify.py" comments had been warning about.

### Removed — the commit-SHA transient marker, and the rubber-stamp it trained

The durability module has carried a tuning protocol since it was
written: *"a high override rate is a signal that a marker is producing
too many false positives and should be removed … tune against real
traffic, not vibes."* This release executes it for the first time, and
the marker it removes is the one that detected commit hashes. A memory
body may now cite a commit without the write gate objecting.

The telemetry that condemned it, from the dogfood event log at the moment
the call was made: **47 fires against 45 overrides**. (The row is not
frozen at that figure — it still accepts events from any server process
running pre-3.30.0 code, so a live rollup may read a little higher until
those restart. What cannot grow again is the class.) Read the record as 45
of 47 blocks overridden —
`override_rate` divides by fires *plus* overrides, so its 0.489 is 97.8%
of the 0.500 that metric can reach when every block is answered. The
same metric pooled across the phrase markers is 0.161. Of the 47 blocks,
36 were answered by an explicit `acknowledge_transient=True` in the same
session, **median gap 25 seconds**; a further 9 overrides have no
preceding block at all, because the caller had started pre-acknowledging
before being asked — 7 of those in the final fortnight. That is not a
gate. It is a speed bump that taught its caller the override reflex,
which the marker-list comment names as worse than having no marker.

The corpus agrees from the other side. Of 210 accepted bodies, 79
contained text the detector fires on: **66 referential** ("the fix
landed in 68aff13"), **10 positional** ("main is at 68aff13"), **3
incidental** — a restic snapshot id, a Cloudflare build id, a container
image tag, none of them commits at all. Only the positional class was
ever the target, and 2 of those 10 were already caught by another marker
in the same body, so 8 firings in 79 were catches nothing else would
make. Meanwhile 64 of the 79 bodies carry `verified_commits`
attestations: this project's own verification system treats commit
identity as a durable anchor, and the write gate was arguing with it and
losing 45 times out of 47.

One of the ten genuine catches is a machine-written loop ledger
(`last_audited_sha: e3e4ba5`). A structured field cannot be rephrased,
which is precisely the shape the title-case skip list already documents
as disqualifying — every fire on it could only ever train the override.

Two paths had no override valve at all, and there the marker was not a
gate but a data-loss valve. In the proposal extractor a sentence like
"I prefer to cite the commit that introduced a regression, e.g. a1b2c3d,
in the postmortem" was discarded outright while the identical sentence
without the hash was captured; the LLM consolidation path raised. Both
now keep the sentence.

Removed with the detector: the git-describe companion pattern (zero
occurrences across the 210 live bodies), and the UUID mask and 32-hex
skip, which existed only to stop durable identifiers — tenant UUIDs, KMS
key ids, MD5 artifact hashes — from failing closed against a scan that
no longer runs.

**Kept deliberately, and it looks like dead code:** the `SHA_MARKER`
name, `canonical_marker` and the legacy-name fold. They read the
append-only event log, never a candidate body, and they are what folds 92
historical events (54 distinct raw names, 21 of them singletons) into the
single row quoted above. Deleting them as leftovers would make the
evidence for this removal unreproducible from the store. The constant now
carries a tombstone comment saying so, and a test pins the split.

**The honest cost.** A body whose only transient signal is a bare branch
pointer — "main is at 68aff13", "head 1dc5bfe", "prod runs main@ed415c5"
— now commits unremarked. Every hedged or dated spelling of the same
fact still fires ("currently", "as of `<date>`", "the latest", "commits
ahead"); what is uncovered is specifically the terse, unhedged pointer.
The read side does **not** rescue it: `compute_commit_drift` anchors on
paths, and a pure branch pointer has none, so the leg returns `None` —
which is exactly the case that never demotes. The calendar leg is the
only backstop for that shape. The right repair is read-side, resolving a
body-cited SHA against the repo at verification time rather than a regex
judging English, and it is deliberately not in this release.

## 3.29.0 - 2026-07-26

Almost no new feature, and one reversal. This window is one thing:
making the project's claims about itself checkable, and repairing what
that check found. The headline is a data-loss regression this same
window introduced and then caught — `sync` refusals that fired *after*
mutating the user's index — but the durable output is machinery. Four
times a manual sweep for false prose was replaced by a test that
re-derives the claim, and three times that machinery was then audited
and found weaker than its own docstring.

The genuinely new capability is two ratchets pointed at the store's own
honesty: `doctor` can now measure whether a store can still be *found* by
the questions a model asks, and whether the attestations underneath its
freshness signals point at files that carry the claims they attest.
Neither was checked by anything before. The first of those measurements
is also what forced the reversal: installing `bettermemory[embeddings]`
now actually enables semantic search, where the extra used to be inert
unless you also opted into an unrelated write-time flag.

Minor rather than patch: `patterns.clusterable_episodes` is new public
API, three response shapes gain keys (`episodes_clustered`,
`gc_deferred`, `pending_rows_on_disk`), and `doctor` reports two new
check names (`retrieval_discrimination`, `attestation_anchors`) in its
text and `--json` output.
`SCHEMA_VERSION` is unchanged and no tool gains or loses a parameter. A
default install with no embeddings extra ranks byte-identically to
3.28.0 — the new checks only read. An install that *has* an embeddings
extra does not, and that is the one change here a user will feel.

### Fixed — `sync` could destroy uncommitted work

- **🔴 A refusal that fires after a mutation is not a refusal
  (`cb32eca`).** Both refusals the 3.28.0 snapshot rework introduced —
  the empty commit message and `_require_no_sequencer_state` — lived
  inside `_commit_snapshot_tree`, which runs *after* `_stage_and_commit`
  has already reconciled `.gitignore` and run `git add -A`. On a store
  parked mid-merge with conflicts resolved and staged, `sync push` and
  the unattended `sync auto` therefore mutated the index and only then
  declined. Measured on a two-clone store with an unrelated uncommitted
  edit: that memory's porcelain code flipped from unstaged to staged
  across the refusal, and the refusal's own printed remedy `git merge
  --abort` then reset it to its committed content — bytes that had never
  been committed, so no reflog entry and no dangling object.
  Unrecoverable. The identical fixture with no sync run kept them.

  Both refusals now fire at the top of `_stage_and_commit`, which holds
  the package's only `git add -A`, so a third caller cannot forget the
  guard. They remain in `_commit_snapshot_tree` as idempotent
  re-assertions. Tests assert `git status --porcelain` is BYTE-IDENTICAL
  across the refusal on `push`, on `auto`, and at the CLI boundary.

- **🟡 The compare-and-swap did not span the window it protected
  (`cb32eca`).** It covered `rev-parse --verify HEAD` → `update-ref`,
  while the tree being committed was frozen earlier at `write-tree`. A
  commit landing in that gap passed the CAS — its tip *was* the parent by
  then — and the older tree then deleted its files from the branch tip.
  Measured: `sync push` returned `committed=True, pushed=True` with a
  hand-written memory absent from `main`'s tree. The parent is now read
  immediately before the snapshot, so the CAS spans the whole window.

- **The staged-marker scan judged a different tree than it committed
  (`a64d051`).** `git grep --cached` read the index, then `git commit`
  read it again; content staged in that gap was committed unscanned.
  `_stage_and_commit` now snapshots with `git write-tree`, greps that
  object, and commits *that* object via `git commit-tree` +
  `update-ref`. Porcelain semantics were reproduced against git 2.50.1
  rather than assumed: `-S` is passed explicitly (`commit.gpgSign` is
  honoured by `git commit` and ignored by `commit-tree`), `git commit
  -m`'s `--cleanup=whitespace` is reproduced byte-for-byte, and
  `update-ref` gets HEAD as its expected old value so a mid-sync commit
  is refused rather than orphaned. Two differences are not reproduced and
  are documented as losses: commit hooks do not fire, and
  `COMMIT_EDITMSG` is not written. A half-finished merge is refused
  rather than approximated.

- **Commit signing is unchanged, and now pinned by a test that observes a
  real signature (`6b12c98`).** The audit premise was that the porcelain
  → plumbing switch had dropped signatures. Measured rather than assumed,
  it had not: `sync push` produces a commit that verifies (`%G?` = `G`)
  under both `gpg.format=ssh` and `openpgp`, while a control
  `commit-tree` without `-S` returns `N`. What was missing was the
  guarantee — the only signing test asserted the failure mode, which
  cannot distinguish "signs correctly" from "errors whenever signing is
  on". Now pinned end-to-end with a throwaway ssh key, hermetic across
  the matrix, asserting both directions.

### Fixed — worktree isolation

- **The dead-worktree degrade fired on evidence it never had
  (`7dd2a11`).** `worktrees_match` keyed its degrade on
  `Path(...).exists()` under a blanket `except OSError: return True`, so
  every way of failing to stat a path read as "that worktree is dead" and
  relaxed the filter to repo-level. A live checkout behind a chmod-000
  parent, an unmounted volume, a detached network share: all degraded the
  isolation OPEN, on the surface whose only job is keeping one
  workspace's notes out of another's. `_worktree_root_is_gone` now stats
  directly and enumerates only the GONE side (ENOENT, ENOTDIR, ELOOP,
  ENAMETOOLONG, plus the two Windows codes CPython does not fold in).
  Everything else is "I could not find out" and holds the isolation, so
  an unclassified future errno fails closed. `ERROR_NOT_READY` is
  deliberately excluded where `pathlib` includes it — for an isolation
  boundary a disconnected volume is exactly the fail-open being closed.

  Same commit: `_count_recent_tombstones` compared roots with a raw `!=`,
  making `recently_removed_in_worktree` the one worktree-keyed surface
  with no degrade and no linked-worktree leg. After an ordinary `mv` of a
  checkout it silently read 0 — "nothing was trimmed here" about a
  workspace that had just trimmed something — while retrieval kept
  working. It now routes through `should_include_for_caller`, the same
  helper the active per-scope counts in that handler already use.

- **`episode_patterns` read and deleted across the isolation boundary
  (`b0a3063`).** It consulted neither `state.disabled_scopes` nor any
  worktree guard, so a caller saw sibling-worktree and scope-hidden
  bodies through the pattern snippets — and a committed promote
  bulk-DELETED those member episodes. Both filters now mirror
  `episode_search`, behind a new `auto_scope=True` parameter; the
  dismissal GC deliberately keeps keying off the unfiltered on-disk set,
  so a dismissal recorded in another worktree is not collected as "aged
  out". Also in that commit: `PatternDismissals.dismissed_ids` loaded its
  rows outside the flock and locked only around the GC rewrite, so a
  concurrent `dismiss()` was silently erased and the pattern resurfaced.
  Lock, then load, then rewrite.

- **The worktree-confinement claim was false on four legs, in four
  places (`c3d481a`, `e75773c`).** `episode_promote`'s carve-out argument
  ended on an absolute — default-scoped `episode_search` and
  `episode_patterns` "never" hand out a foreign episode id — which
  `worktrees_match`'s permissive legs contradict. Reproduced three ways
  on real fixtures with the foreign worktree ALIVE on disk: a caller
  outside any git checkout, and a caller in a linked worktree whose
  primary wrote the episodes, both get another journal's ids from a
  default listing and both delete them on promote. The carve-out itself
  is unchanged and still right — the bound is the SELECTOR, never "no
  read will ever name a foreign one for you" — and the prose now argues
  at that strength. `DESC_EPISODE_PATTERNS`, the model-facing copy and
  the one nobody re-reads critically, was corrected last and enumerates
  all four legs rather than pointing at the authority.

### Fixed — the silent-miss audit probe now measures production

- **The probe ranked a different candidate pool than production
  (`8fc7afc`).** Both producers handed it an unconditional
  `store.load_all()` with no corpus-statistics provider, while production
  ranks the FTS5 prefilter's capped, query-biased slice with
  corpus-derived document frequencies. Above the index threshold that
  superset let a memory production's prefilter would have dropped take
  rank 1 and decide the miss verdict alone. `resolve_search_pool` is now
  the one implementation all three surfaces build their pool with, and
  both producers thread the whole `RankingInputs` (the probe previously
  took `applied_by_id` only, while `memory_search` also passed
  `negative_by_id` and `corroboration_boost`). This is a redo of a
  rejected attempt that reassigned `run_audit`'s `memories` to the capped
  pool and silently disarmed the auto-consolidate size guard, which reads
  that list's length as the store's active-set size.

- **The probe's search width sat outside production's range
  (`eec3efa`).** Both producers passed `default_max_results` raw while
  `memory_search` clamped the identical parameter to [1, 50] first, and
  the config knob is range-checked at load only for int-ness. Measured
  with a spy over the three call sites: `default_max_results=100` gave
  widths [100, 100, 50] and `0` gave [0, 0, 1]. Above the served cap the
  starvation guard holds unconditionally on every saturated slice; at ≤ 0
  it can never fire, so the probe returns `no_signal` where production
  reports `miss`. `clamp_search_width` is now the single arithmetic site
  for the range. The same commit deletes `ToolHandlers._index_threshold`,
  dead with no citations.

- **The width difference that remains reaches the verdict, and the prose
  said otherwise (`dd6ff31`).** `min_survivors` differs by caller —
  request width for `memory_search`, config default for both producers —
  and both call sites claimed the guard therefore fires "on the same
  slices" production's would. Measured on a purpose-built corpus: the
  same probe over the two pools reports `ok` (rank 1 a 1/2-coverage
  medium hit) and `miss` (rank 1 a 2/2-coverage high hit). Not closed by
  threading a request width — the miss verdict's own precondition is that
  no retrieval happened — nor from the production end, which would reopen
  a fixed starvation bug. So the widths stay and the prose is corrected,
  with the residual stated: a model habitually passing a wider
  `max_results` is audited against a narrower counterfactual than its own
  habit.

### Fixed — conflict arbitration lifecycle

- **A settled verdict could be erased by one bad read (`bd35cc8`).**
  `upsert_scan` treated "absent from the caller's `load_all()` snapshot"
  as proof a member had died and permanently deleted the row — status,
  `verdict_ts`, note and all. But `Store.load_all` skips a file on
  `PARSE_SKIP_EXCEPTIONS`, which is `(Exception,)`, so one truncated
  write or bad `chmod` erased an arbitration with no way back:
  re-detection can only ever re-file the pair as `pending`. GC now runs
  only from a snapshot accounting for every active `.md` file under the
  root; a short snapshot merges and refreshes as usual, collects nothing,
  and reports the new `gc_deferred` counter. Same commit: the resurrect
  rule keyed on `m.updated > verdict_ts`, which cannot tell a claim edit
  from an `updated` bump the arbitration surface itself caused on a
  different row — so arbitrating one pair resurrected unrelated dismissed
  pairs. Verdicts now fingerprint the two bodies they judged. And the
  `compatible` branch returned before the `recorder.record` only the
  contradiction branch reached; both now record `conflict_verdict`.

- **Three lifecycle gaps in the verdict handler (`02d69be`).** A verdict
  against a member that is no longer active is refused, naming the
  re-scan as the remedy, instead of writing a link to a dead target; a
  `compatible` dismissal now clears any `contradicts` edge between the
  pair first, in both directions, so the queue and the link layer cannot
  end up permanently disagreeing (echoed as `links_cleared`); and the
  queue GC is gated rather than firing from any snapshot.

- **The store stopped answering "how many pending?" two different ways
  (`9155d9e`, `3c5573f`, `076f02e`).** `memory_conflicts` derives
  `pending_total` from the rows it can actually list and judge;
  `memory_scope_overview`'s `curation_pending.conflicts` routes through
  the same `conflicts.split_judgeable` predicate rather than counting raw
  rows, so a candidate whose member died since the last scan is no longer
  advertised as pending work; and on a GC-deferring scan the response no
  longer carried two keys named `pending_total` with different numbers —
  `upsert_scan` returns `pending_rows_on_disk`, which is a different
  question and now says so. The omitted-row hint fires in scan mode too,
  naming the deferral as the cause.

### Fixed — store concurrency

- **`Store.update` reverted a concurrent corroboration bump
  (`a841b3e`).** `record_corroboration` deliberately never bumps
  `updated` — a recurrence is not a rewrite — so a bump landing between
  the caller's snapshot read and the write lock sails through the
  `updated` CAS, and writing the snapshot's stale counter back silently
  erased it. The rollup is now store-owned like `updated`: re-copied from
  disk on every path, with or without `preserve_verification`, with or
  without `force`. The counter is monotonic, so re-copying is always the
  correct merge; there is no opt-out, because a content edit does not
  invalidate recurrence evidence the way it invalidates an attestation.

### Fixed — surfaces

- **The web UI showed a staleness verdict the MCP surface disagreed with
  (`70437ab`).** `/memories` search rendered a verdict it computed itself
  from verification plus path drift — which is `hit_to_dict`'s *pre*-attach
  initial value — while `memory_search` then runs
  `attach_commit_drift_counts` and upgrades hits whose anchors moved. So a
  calendar-fresh, drifted memory wore a green "fresh" chip on the search
  page and "spot-check recommended" one click away on its own detail page;
  live-reproduced on 18 of 89 hits. Hits now render through the shared
  response pipeline and the page derives no verdict of its own. The same
  commit threads the handler's ranking inputs through a shared
  `resolve_ranking_inputs` so a flag flip moves both surfaces, renders
  `polarity_skipped` and `orphan_use_events`, and replaces a false "every
  bucket rendered" claim with a rendered/disclaimed split a test pins
  against `HealthReport.to_dict()`.

- **Three false claims on that same surface (`fb070b4`), and the caveat
  that fixed one of them fired on the wrong predicate (`6294606`).** The
  commit-drift cost claim ("one call for the whole search … independent
  of result count") measured 5 git processes for 3 hits and 8 for 6, and
  now states the real arithmetic. `/memories`' "same ranker, same
  tokenizer, same relevance labels as memory_search" was reproduced false
  under two shipped configs; the claim is scoped by a declared partition
  of `search.search`'s keyword parameters that a test enforces, rather
  than by a sentence. The fallback event-read comment named the wrong
  window constant. The lexical-only caveat added there then gated on the
  model-LOAD question rather than on whether a semantic leg actually
  RANKS: over a 10-row config matrix driving the real handler with spies
  on each scorer, the caveat showed on 7 rows before and 4 after, and
  those 4 are exactly the rows where `_score_semantic` runs. Its text
  also described a fusion that `search_mode = "semantic"` does not do, so
  it is now two notes over a shared lead.

- **`bettermemory health` computed a different bucket than
  `memory_health` (`52e6d70`).** `_cli_health` was the last production
  `report_for_directory` caller dropping `cold_endorsement_ratio_threshold`,
  so the CLI reported `cold_endorsement_memories` under the strict
  `explicit == 0` semantics no matter what the config said.

- **`dropped_as_route` reached every path-drift surface but one
  (`5771fe3`, `474a957`, `2311bfd`).** `MemoryHit` grows
  `path_drift_dropped_as_route_paths` and `hit_to_dict` folds a non-empty
  suppressed set into the per-hit block, additively like
  `expected_absent`, closing the one MCP surface that silently dropped
  the bucket; `docs/api.md` documents the key on both the `memory_search`
  and `memory_show` shapes. Emit gates are unchanged — a route-only
  report stays invisible everywhere.

- **`docs/api.md` caught up to the 3.28.0 contract (`c4f23f6`,
  `b0d1ba5`, `4760222`).** The `outcome_demotion` / `corroboration_boost`
  ranking effects, the `curation_pending` conflicts key, the
  corroboration response keys and the `with_bodies` pair were all
  undocumented; every prose surface still claimed 25 tools / seven gated
  after 3.28.0 took the surface to 27 / nine, and api.md — the pinned 3.x
  contract — was missing `memory_conflicts` and `episode_patterns`
  entirely. A new test asserts every "N tools" claim in the
  non-historical surfaces equals `_EXPECTED_TOOL_COUNT`.

### Fixed — Windows path spellings

Five commits closing one class: raw string comparisons that assumed
`os.sep`, in code paths whose whole job is deciding whether two spellings
name the same place. Windows accepts `/` interchangeably with `\`, so a
forward-slash or mixed spelling of a home-rooted citation read as not
home-rooted — dropping a vanished-repo citation as a route instead of
reporting it missing, which is the silent false negative the home escape
exists to kill (`8f07c7b`). The same fold was missing in
`scope_match._home_alias` (`6aced64`), in the degenerate-root guard,
pass-2 membership and `_declared_root_covers` (`2532bb0`), and the case
axis plus pass-1 span search were still byte-comparing after the fold
(`ea7025f`) — so on a case-folding volume, the lowercase drive letter
some shells record into a synced store slipped the home guard. A repo
root's trailing separator is now spelled `os.sep` rather than hardcoded
(`aeb9135`). On POSIX `os.altsep` is `None` and every fold is the
identity. Tests drive Windows semantics from any platform via explicit
`ntpath` separators with monkeypatched `HOME`/`USERPROFILE`.

### Added — `doctor` can see whether retrieval still works

Every other check asks whether memory is stored, parsed, synced or fresh.
None asked whether it can be **retrieved**, and a store passes all of
them while the ranker cannot surface the right memory for a plainly
worded question. The failure is silent from both ends: the caller gets
five confident-looking hits and never learns that the one it needed
ranked sixth.

- **`retrieval_discrimination`** samples each scope and queries every
  sampled memory twice using terms from its own body — once with its
  rarest, once with its most topical. The rare arm is a control: when it
  is high, the ranker, the index and the fusion are all working, so a low
  topical arm isolates query-document vocabulary mismatch rather than
  blaming a component. On this project's own store the control arm scored
  100% in all seven scopes while the topical arm ranged 0%–50%.
- The cause is a ceiling, not a bug. Lexical retrieval needs the query to
  share rare terms with the target, and inside a topically coherent scope
  the shared vocabulary carries almost no information — the subject words
  appear in nearly every member, so their IDF is near zero. Pricing IDF
  over the collection the caller can actually retrieve is *correct*
  (`c58c836`) and deliberately kept; the consequence is that the ceiling
  falls as a scope grows more coherent, and coherent scopes are exactly
  what good scope hygiene produces.
- Reported, never auto-fixed. It skips only when a semantic leg would
  *actually score a search* — the new shared predicate
  `semantic_setup._semantic_rank_leg_active`, which requires an importable
  extra AND a config that routes it into ranking. Importability alone is
  not that: under the default `search_mode = "hybrid"` with
  `semantic_dedup = false` the model never resolves, so ranking stays
  lexical no matter what is installed, and skipping there would report
  `ok` for exactly the configuration that most needs the warning. The
  `fix_hint` therefore names both halves, and notes that the one flag
  which routes the model also switches write-time dedup from Jaccard to
  cosine — a coupling `_search_mode_needs_model` documents and which
  decoupling would require the write path to stop reading the shared
  factory.
- The docstring states what the probe cannot see: a high score does not
  mean retrieval is good, because real queries are not bags of a
  document's own words. It is a floor — a low score proves a problem, a
  high score proves only that this particular failure is absent — and it
  is explicitly not a helpfulness metric.

### Changed — installing an embeddings extra now actually enables semantic search

This is the release's one behaviour change that a user will feel, and it
reverses a decision this project defended for several versions.

Installing `bettermemory[embeddings]` did **nothing** under the default
`search_mode = "hybrid"`. The model resolved only when `semantic_dedup = true`
— a flag about *write-time duplicate detection* — so anyone who installed an
embedding model to improve **search** got no search change at all, and the
documented fix was to opt into an unrelated write-time behaviour to buy it.
`docs/api.md` had said for a long time that semantic mode "needs only the
extra"; the code disagreed, and the code was wrong.

- **Measured before changing it**, on a 190-memory store over a 20-question
  gold set authored document-first in caller voice:

  | | recall@1 | recall@5 |
  |---|---|---|
  | question as asked, lexical | 10% | 45% |
  | question as asked, **+semantic** | **30%** | **70%** |
  | re-queried, lexical | 65% | 95% |
  | re-queried, **+semantic** | **80%** | 95% |

  Three times the cold-query hit rate, and **+15 points on top of** the
  caller-side query guidance shipped in this same window. That second number
  is the one that decided it: what remains after the guidance is the caller
  *guessing* the store's vocabulary, and no prompt wording recovers it —
  only a model that knows "cut a release" relates to "publish a version"
  without having seen the store.
- **The original objection was real and is now answered rather than
  overridden.** The factory is shared with write-dedup, so resolving under
  `hybrid` would have silently flipped dedup from Jaccard to cosine — and
  scored it against Jaccard-calibrated thresholds.
  `handlers.write._resolve_dedup_thresholds` now reads `semantic_dedup`
  itself and asks the factory for nothing when it is off. Pinned by
  `test_write_dedup_ignores_a_model_resolved_for_search`, which primes the
  factory with a model that raises if touched.
- **Users without an extra are unaffected and stay silent.** Resolution is
  gated on the extra actually importing, so no default install attempts a
  load or takes `get_model`'s install-hint warning.
- Fallout, all in the same direction: `mode="semantic"` now works with just
  the extra (matching what the docs always claimed), the `/memories` lexical
  caveat now names the extra rather than the dedup flag as its trigger, and
  `doctor`'s `retrieval_discrimination` hint is one sentence shorter — the
  fix is "install it".

### Added — `doctor` checks whether an attestation points at the claim

`memory_verify(id, verified_paths=[...])` records the files someone read to
confirm a memory, and everything downstream trusts that list: `path_drift`
watches those paths, `commit_drift` counts commits against them to decide
whether a calendar-fresh memory has gone stale.

- `path_drift` only catches an anchor whose file is **missing**. An anchor
  that exists but is **irrelevant** was invisible to every signal — and it is
  the worse failure, because the memory reads green forever while its real
  ground truth moves unwatched. The instance that prompted this: a memory
  attesting a 3,166-line `eval.py` for a claim about symbols that had never
  been under `src/` at all.
- **`attestation_anchors`** extracts the body's identifier-shaped tokens and
  requires at least one attested file to contain at least one of them. That
  proves only that an anchor mentions the vocabulary, never that it supports
  the claim — it is a smoke alarm, and says so.
- Tuned so everything it cannot judge stays silent, because a diagnostic that
  fires on the unjudgeable stops being read. Exempt: memories with no
  attestation, memories with no identifier-shaped tokens (preferences and
  directives legitimately have no code anchor), and memories whose anchors
  this method cannot read — directories, virtualenvs, paths outside the repo.
  Fenced blocks count as evidence, which removed the one measured false
  positive: a memory whose load-bearing claim was a command chain mirrored in
  `ci.yml`, where the anchor was doing real work the extractor couldn't see.
- Measured on a 189-memory store: 63 unanchored, 65 exempt for tokens, 25
  exempt for unreadable anchors, 36 checked, **1 finding** — a memory about
  virtualenv contents attesting `src/bettermemory/builder.py`, so its
  commit-drift tracked a file unrelated to every claim it made. Re-anchored;
  the check now reports clean.

### Changed — `memory_search` documents how to word a query

The check above measures the gap; this acts on it, from the only side
that can. The mismatch is between the asker's vocabulary and the store's,
and the asker's vocabulary originates outside the store — a corpus cannot
teach itself words it does not contain. But the caller is a frontier model
that already relates "cut a release" to "tag", "pypi" and "changelog", and
nothing on the tool surface had ever asked it to say so.

- **`query` stopped being documented as "free text".** Measured on a
  185-memory store over a 20-question gold set authored document-first in
  caller voice — each target chosen by reading it, then a question written
  the way a caller asks *before* knowing that memory exists, so the ranker
  never picked the labels. Questions as asked retrieve at **10% recall@1**;
  the same questions re-queried in concrete nouns reach **65%** (13
  improved, 0 regressed), with an insider-vocabulary ceiling arm at 90%.
- **The control arm is what fixed the wording.** Stripping the question
  words and keeping the content words scores **10%** — byte-identical to
  asking outright, because the ranker already strips stopwords. "Use
  keywords, not questions" would therefore buy exactly nothing. The new
  line names the lever that actually moved, the vocabulary a memory would
  literally contain, plus the re-query reflex — a weak first hit is the
  only signal a caller ever gets that its wording missed.
- **Retracted in the same pass:** `mode`'s "`hybrid` for paraphrase
  recall". With no semantic leg configured — the package default —
  `hybrid` is RRF over keyword and BM25, both lexical, so that line
  promised the caller the exact capability whose absence this window
  spent a check measuring.
- Paid for inside the existing description budget rather than by raising
  the ratchet: `since_prior_session` gave back the sentence deriving why
  its boundary is strict-`>`, rationale the caller cannot act on and whose
  real guard is `test_api_md_since_prior_session_strict_after`. The lean
  default-on surface **shrank**, 27,245 → 27,155 chars.

### Fixed — "worktree isolation" was never isolation

`auto_scope`'s worktree half runs `origin.worktrees_match`, which is
**permissive**: it passes a memory or episode through on four cases, and both
the model-facing descriptions and `docs/api.md` listed two. The unlisted pair
is the pair nobody can infer — a caller sitting in a **linked worktree** of
the recording checkout, and a recorded worktree **positively gone from disk**.

- The linked-worktree case is not a corner: `git worktree`-based agent fan-out
  runs there, and under it the primary checkout's memories and episodes are
  fully visible. That is deliberate and desirable — it is why the relaxation
  exists — but a reader told "isolation" will conclude the opposite.
- `DESC_EPISODE_SEARCH` additionally claimed to mirror "the isolation
  `episode_handoff` enforces". It does not: handoff uses strict equality, this
  uses the permissive rule, so the two disagree on **every** pass-through case.
  `docs/api.md` carried the same claim.
- `episode_patterns` is where it bites hardest, and its doc now says so: that
  call's commit path DELETES the episodes its filter admits, so each
  pass-through case is a potential cross-worktree delete.
- Corrected in all three places (`memory_search`, `episode_search`,
  `episode_patterns`) and guarded two ways — one test asserts against
  `inspect.getsource`, so swapping either handler's filter names the
  description that has gone stale, and one asserts `docs/api.md` cannot
  quietly restore the stronger word. What genuinely stays isolated, and is now
  stated positively, is the live-sibling case.

The handlers' own inline comments were right the whole time. Only the copy a
reader sees was wrong, which is the shape this window keeps finding.

### Fixed — one `search_mode` string, three behaviours

`[behavior] search_mode` was loaded with a bare `str(...)` and read by three
consumers that each assumed something different about it:
`semantic_setup._search_mode_needs_model`, which decides whether an
embedding model is **loaded**, compared `.strip().lower()`;
`handlers.search` passed the raw value to a dispatcher that raises on
anything outside the four literals; and the web UI rewrote an unrecognised
value to `hybrid` and ranked with it, because a page cannot 500 on a config
typo.

- So `search_mode = "Semantic"` loaded an embedding model, made **every**
  `memory_search` call raise `unknown search mode` mid-conversation, and
  served a healthy-looking lexical web page. `"HYBRID"` was the cruel one:
  capitalising the *default* value killed memory search outright while every
  other surface looked fine.
- Now normalised once at the loader, which is where the policy layer already
  lives — so the whole system agrees with the assumption
  `_search_mode_needs_model` was making on its own. An unrecognised value
  falls back to `hybrid` rather than raising, per `default_max_results`' rule
  that one bad knob must not take the server down, and it **warns**: a silent
  fallback would leave someone who asked for semantic ranking quietly getting
  lexical, which no other surface reports.
- The mode tuple is a hand-copy of `search.SearchMode` (config cannot import
  search — the dependency runs the other way) and is now cross-pinned, since
  a drifted copy would either reject a real mode or admit one the ranker
  raises on.

### Changed — the shadow relevance label stays shadow, with a reason

`_relevance_label_v2`'s contract said live behaviour holds on v1 "until
the logged v1/v2 disagreement data justifies a flip." That data now
exists — 195 `turn_audited` events carrying both labels — and it answers
the question in the negative. Recorded in the docstring so the next reader
inherits the verdict rather than the open question.

- Bucketing the same turns by the length of the user message that produced
  them, v1's "high" rate runs 45% → 32% → 0% → 3% as messages grow, while
  v2's runs 47% → 63% → 83% → **100%**. Neither label is measuring
  relevance; both are measuring length, in opposite directions. v1's
  coverage denominator grows with the query, so long messages can never
  clear 0.75 — the blind spot v2 exists to close — but v2's absolute floor
  replaces a length-blind rule with a length-credulous one, because four
  distinct content tokens landing *somewhere* in a 185-document store is
  near-certain for any long message.
- Flipping would have taken this store from 11 flagged misses to 105 of
  195 turns, dominated by bare continuations and ordinary work turns with
  no memory to miss.
- A rule's flag rate not tracking message length is a necessary condition
  and needs no ground truth, so it is the screen used: max-minus-min flag
  rate across those buckets is 45% for v1, 53% for v2 — worse than what it
  replaces — and 29% for the conjunctive `coverage>=0.75 AND matched>=4`.
  The matched-token count is worth keeping; the `or` is the defect. That
  conjunction is what a v5 should be calibrated from, against labelled
  turns, since this screen can rank candidates but never confirm one.
- Consequence worth stating plainly: `silent_miss_rate` is a length
  artifact under either rule, so a low value is not evidence that a store
  is being retrieved well.

### Added — machinery that re-derives instead of restating

The window's most valuable output. A commit dedicated to hunting
duplicated false claims minted a new one in its own repair prose —
`list_row_to_dict`, a symbol that has never existed in this repo
(`f0e8f0b`) — which proved the class was structural and that a manual
sweep is unfalsifiable. Four ratchets landed, and each was then audited
and found weaker than its own docstring.

- **`tests/test_symbol_citations.py` (`e913346`)** scans docstrings AND
  `#` comments from every tracked `*.py` — `tests/test_doc_claims.py`
  names comments as its own blind spot, and five of the defects lived in
  one. Four rules, each tightened against the whole corpus until live
  findings equalled the allowlist; suppressions are extractor rules
  rather than allowlist entries, so they cannot rot; keys are never line
  numbers. It repaired eleven citations to land green, two pairs of which
  it found itself rather than the sweep that motivated it.

- **"Bound" is not "reached" (`e3e4ba5`).** The private-symbol rule asked
  only whether a name was bound anywhere, and a dead delegate satisfies
  that forever. The new `unreferenced-private` rule fires when every
  binding is an undecorated def/class and nothing in the tracked Python
  loads, imports, or names it in a lookup string. Each clause was
  measured rather than assumed: without the undecorated clause the scan
  yields seven extra names and four live false positives; the identical
  scan over public names flags ~79% of them. Proven by copying it onto
  the unchanged parent tree, where it reports exactly 26 citations across
  ten files, all naming a dead `ToolHandlers._load_search_candidates`
  alias — now retargeted and deleted.

- **A vocabulary tuned for one consumer fails open in another
  (`6487ea2`, `939c181`).** The historical-prose exemption first searched
  an imported regex over a 60-character lookback, so ordinary English
  anywhere in the window silenced the citation beside it; requiring the
  marker to ATTACH fixed that. Then attachment itself proved defeated by
  the same words, because for `before` / `until` / `once` / `was`
  adjacency IS their ordinary reading — "Called before `_flush` acquires
  the lock" is a live claim, silently exempt from all four rules, and it
  also silenced the brand-new rule 5. Root cause: sharing a vocabulary
  across two consumers with different reading disciplines. A module-local
  `_ATTRIBUTIVE_MARKERS` list now holds only markers that can pre-modify
  a name, measured at 152 → 55 attached exemptions with live findings
  unchanged at 5. The import is demoted to a bound: a test asserts every
  local marker is still recognised by the shared list, so this module can
  only ever be stricter.

- **`tests/test_platform_fixture_lint.py` (`adac167`)** was asked for by
  a commit message that wanted "a lint rather than a sixth discovery" of
  the same class: a fixture manipulates paths POSIX-shaped while its
  assertion is platform-neutral, so the defect is invisible locally and
  on ubuntu and costs a windows-latest round-trip. Its exemptions then
  took three generations to get right — direction-aware for the platform
  gate (`caf59f5`), for the probe gate (`2610a74`), and finally ordered
  against the flagged shape (`0abe0cc`), because both were position-blind
  and a skip written *below* a filesystem touch still excused it.

- **Release notes are now gated on commit coverage (`dcf00ca`).** Every
  non-merge commit in the newest tag's window must be represented in that
  release's entry — short-SHA citation, a shared two-word phrase, or a
  near-total unigram match — with trivial types and tooling-only scopes
  exempt. It skips on shallow CI checkouts by design; the teeth are the
  local run between `git tag` and `git push`, where a gap costs a re-tag
  instead of an erratum. Landing it surfaced the class live in the
  v3.28.0 window.

- **The line-ref checker learned what it may not judge (`5b6a842`,
  `11c029b`, `f7cfce1`, `760b2ac`, `2501545`, `3156862`, `266d860`).**
  Errata quote their own rotten citations with the verdict in the same
  sentence, so the prose failed precisely where it was right; a
  non-resolving rule and a companion commit-pinned rule now suppress
  those, and both retired allowlist entries the reverse guard then forced
  out. `_LINEREF_BARE` never captured a range end, so a bare `file.py:N-M`
  was checked as line `N` alone — the same bare/linked asymmetry that let
  a wrong citation ship before — and a reversed range is now rejected as
  malformed rather than validated by its end alone. Four subsequent
  commits tightened the non-resolving vocabulary from bare participles to
  constructions that name what they judge.

- **The description budget got its slack back (`d1585d3`).** Measured
  27,248 of a 27,250 ceiling at the 3.28.0 release commit — two
  characters. At that width it is not a budget, it is a tripwire on
  unrelated work that fires far from its cause, and it had already
  started shaping edits it has no business shaping. The ceiling moves to
  27,500 with a diagnostic per-tool baseline and a pressure warning on
  passing runs. The test's own "never raise the ceiling" instruction is
  unchanged and still absolute for the case it was written for; this is a
  recalibration of the slack, in which the measured total does not move.

### Fixed — false claims in prose

Beyond the ratchets, the repairs they demanded and the sweeps that
preceded them. One claim needed seven corrections before it was gone
(the audit probe "runs against the active store" — `b4c450f`, `4e963ad`,
`12ef7fb`), another four (the links-vs-drift absence analogy — `497daa8`,
`42fe4e1`, `308e743`, `e91759c`), and a repair is itself a claim. Also
repaired: `store.py`'s rotted tombstone citations, symbol-anchored rather
than line-numbered (`612ed49`, `8dff3dc`); four comment sites dating
their own legacy behaviour "Pre-2.7" when git says `bc47593`/3.0.0
(`86b46a0`); `test_tool_surface` prose derived from its pinning constants
instead of stale literals (`3d45445`); `episode_promote`'s deliberate
no-filter carve-out, disclosed and pinned against a reflex fix
(`fd26136`); and a section header left pointing at an alias the next
commit deleted (`e1d7b46`).

Three errata correct entries already shipped, following the 3.24.0
convention. The frozen 3.26.0 entry carried no trace of ten substantive
commits in its own window, found by retro-running the new coverage check
(`a7103e0`); two further 3.26.0 claims were false at ship and are
qualified rather than rewritten (`d321646`); and the omission claim
itself was over-broad and is now scoped to the release entry, carving out
`b3cc470` (`8a32fc8`).

One inherited finding was REFUTED rather than repaired (`5a960a3`,
`e1d7b46`). The parked extractor-hunt record claimed strict
`worktree_root` equality hides every memory for a repo after a checkout
moves or the store syncs to another machine. It does not reproduce at
HEAD: it was closed by `b0ab779` — the same commit that wrote the parked
JSON, so it was filed and fixed together — and shipped in v3.9.0. Now
pinned by three end-to-end cases (moved checkout, synced foreign-OS path,
and the negative control that a second LIVE checkout stays isolated,
which is what distinguishes the real fix from "drop the worktree
clause"). No production change. The record's own correction then
overstated in the other direction — the entry was open at the hunt's base
— and was itself corrected.

### Changed

- `src/bettermemory/cli/sync.py` goes 27% → 100% statement coverage
  (`153d284`). It sat with every `_cli_sync_*` body uncovered, so nothing
  pinned the layer that turns a `SyncError` into an exit code and a
  stderr message — the layer this window's three new refusals reach users
  through. Sixteen tests driven in-process through the real CLI entry
  point, mutation-checked against four separate mutants. No mapping bug
  found.

### Erratum (2026-07-30)

The entry above is left as it shipped. Its arithmetic is internally
sound — 10% → 30% is three times, 65% → 80% is fifteen points — but the
measurement underneath it no longer stands. That 190-memory live-store
pair was never captured as a committed artifact, and the blind
replication built afterwards in `bench/retrieval` could not reproduce
its asked-baseline: the replication measures 35% asked, not 10%, and its
own README records that no absolute number in that directory is
comparable to the live-store figures. The decision this entry describes
was right, and the newer artifact still supports it; only the
magnitudes were overstated.

What the superseding artifact
(`bench/retrieval/results/v2-unpadded-2026-07-26.json`, a synthetic
180-document corpus, 20 questions per probe) measures for the same
comparison, at recall@1:

| | lexical | +semantic |
|---|---|---|
| question as asked | 35% | 60% |
| re-queried | 80% | 90% |

So +25 points on the cold query rather than a tripling, and +10 points
on top of the query guidance rather than +15. The model-facing sites
that had restated the retired pair — `DESC_MEMORY_SEARCH`, the
`handlers/search.py` module docstring, `config.py`'s shipped
`DEFAULT_CONFIG` prose, `semantic_setup.py`, and `doctor`'s
`retrieval_discrimination` fix hint — were re-pointed at this artifact
in the 2026-07-30 window; the resident description now carries the
instruction with no number at all, because that claim is only honest
next to its caveat.

## 3.28.0 - 2026-07-23

The learning-loop release: three features that move the store from
"archive with telemetry" toward "system that learns from its own
experience". A design review of what the system *lacked* found the
adaptive loops half-built — negative outcomes collected but never fed
back into ranking, recurrence suppressed by dedup instead of
accumulated, and corpus-level disagreement computed by the dedup scan
and thrown away with the report. All three close here. Every ranking
change is opt-in and the shipped default ranking is byte-stable.

### Added — negative outcomes demote ranking (`outcome_demotion`)

- Endorsement has boosted explicitly-applied memories since the
  `endorsement_boost` flag shipped; the negative half of the same
  signal (`ignored` / `contradicted`) only ever annotated. With
  `[behavior] outcome_demotion = true`, active negatives now slide a
  memory down: `1 - 0.15*(1 - exp(-(ignored + 2*contradicted)/3))` —
  the mirror of the endorsement curve, floored at -15% so it breaks
  near-ties and can never bury a strongly-relevant hit. Contradicted
  weighs double: an off-topic surfacing is often the query's fault; a
  stored claim that disagreed with reality is the memory's.
- "Active" is pinned server-side and matches what the other surfaces
  already call settled: a 30-day window (mandatory-cutoff contract,
  same as the endorsement tally), a later NON-AUTO `applied`
  supersedes (`recent_negative_outcomes` parity), and a
  `memory_update` / `memory_verify` postdating the event clears it
  (`health._has_unresolved_contradiction`'s rule applied per-event).
  Three independent guards against the rich-get-poorer spiral.
- With the flag on, the per-search event read widens from the 600s
  attribution horizon to the full 30-day negative window — which also
  upgrades the `recent_negative_outcomes` annotation's 30-day contract
  from best-effort (rotation luck) to guaranteed. The tallies cannot
  cross-contaminate: each enforces its own cutoff internally.

### Added — recurrence accumulates (`corroborations` +
`corroboration_boost`)

- Dedup used to SUPPRESS recurrence evidence: the tenth time a claim
  re-entered a conversation, the write bounced and nothing accumulated
  — a one-off remark and a bedrock preference had equal standing. Now
  a duplicate-rejected `memory_write` credits the matched memory a
  corroboration: persisted `corroborations` count plus
  `last_corroborated`, a third timestamp axis (`updated` = content
  edits, `last_verified_at` = reality-checks, `last_corroborated` =
  recurrence). Once per (memory, session); best-effort by contract;
  `updated` deliberately untouched — a recurrence must not fake a
  rewrite. The rejection response says so (`corroboration_recorded`),
  so the model stops treating duplicates as wasted calls.
- Curation counts it as life: the freshest-touch window behind
  `dead`-bucket health, the consolidate demotion pass, and cold-scope
  detection all gain the fourth axis. A corroborated memory is not
  dead weight.
- Opt-in ranking nudge `[behavior] corroboration_boost = true`: same
  +10% ceiling and saturating shape as endorsement, fed from the
  record itself (no event-log walk). `episode_promote` routes through
  the write path, so an episode restating a known fact corroborates it
  for free.

### Added — corpus-level inference (`memory_conflicts`,
`episode_patterns`)

- **The store can now notice its own contradictions.** New
  numeric-divergence detector inside both dedup scans, beside the
  polarity guard: near-identical bodies whose number-bearing tokens
  mutually diverge ("port 5432" vs "port 5433", 3.27.0 vs 3.27.1) are
  a value-level disagreement. Previously that pair was a DEDUP
  CANDIDATE — an applying consolidate pass tombstoned one side on
  recency rather than truth. Now both detectors' skips persist into a
  verdict queue (`.conflicts.jsonl`) instead of dying with the report:
  stable pair ids, re-scans refresh rather than duplicate, and rows
  with dead members GC.
- `memory_conflicts` (full surface) lists pending pairs with both
  bodies inline and takes the model's verdict.
  `verdict="contradiction"` writes the `contradicts` link (before the
  verdict stamp, so the link's own `updated` bump can't re-trigger
  anything) — both memories then surface it at retrieval, and
  resolution proceeds through the normal verbs. `verdict="compatible"`
  dismisses sticky — until either member's content changes, which
  legitimately reopens the question. Scans run on demand
  (`scan=True`) and automatically on every APPLYING consolidate pass;
  dry-run stays zero-side-effect. `curation_pending` gains a
  `conflicts` key in both the absolute and delta views.
- `episode_patterns` (full surface) surfaces themes recurring across
  ≥3 DISTINCT sessions' episodes — the consolidation no single session
  can see. Detection is conservative (unstemmed distinctive terms,
  ubiquity ceiling against project vocabulary, with a monothematic-
  journal fallback); the model AUTHORS the promoted body and the write
  routes through the full `memory_write` gate stack. Committed
  promotes delete their member episodes; a `duplicate` rejection still
  lands value — it corroborates the existing memory. Dismissals
  persist keyed by member-set hash: sticky for that exact evidence,
  reopened by a new member episode.

### Changed

- `memory_record_use`'s outcome table states the ranking consequence
  of each outcome under the new flag; `memory_write`'s `duplicate`
  bullet documents the corroboration credit. The eval tool map covers
  the two new tools (+3 event kinds), and the eval markdown renderer
  now PINS untelemetered rows into the published table past the top-10
  slice — at 27 tools a structurally-zero row can never crack top-10,
  and the "(no telemetry)" note must not silently vanish.
- `sync push`'s gitignore covers the two new store-root sidecars
  (`.conflicts.jsonl`, `.episode_patterns.jsonl`) — host-local
  curation state, caught by the structural sidecar guard before it
  could repeat the plaintext-push incident class.

### Erratum (2026-07-24)

The entry above is left as it shipped. It is incomplete: the preamble
counts "three features", but tag `v3.28.0` carries two further
substantive commits that received no entry anywhere in this file — the
same omission class as v3.24.0's `096218e`, caught this time by the
release-window coverage check that now lives in
`tests/test_changelog.py`. Stated now as the bullets the release
should have carried:

- **Web UI overhauled — verdict parity, ranked search, three new
  read-only pages (`74f625d`).** The UI had frozen around the 3.18
  surface while eval telemetry, the episode tier, and the curation
  engine shipped around it, and its `/memories` search was a
  contiguous-substring filter over the summary line — a query could
  return zero rows against a store whose memory bodies carried it.
  Search now runs through the same `search.search` ranker
  `memory_search` uses (`semantic` degrades to `hybrid`: the web
  process loads no embedding model), and pages speak the staleness
  verdict through the same `compute_staleness_verdict` chain the MCP
  handlers use — the web computes no verdict arithmetic of its own.
  Three read-only pages close the feature gap (`/eval`, `/episodes`,
  `/curation`) and `/health` gains the modern buckets. The mutation
  surface is unchanged: verify stays the only POST, still answered
  403 in the `--tunnel` posture.

- **`sync` refuses to commit staged conflict markers (`295f20a`).**
  Closes the residual 3.26.0 disclosed ("a conflict created between
  the guard and `git add -A` is still possible"): the porcelain
  guards describe how a conflict was CREATED, so conflict content
  arriving from the user's own git between a caller's predicate and
  the stage was committed as if resolved. `_stage_and_commit` — the
  package's single `git add -A` choke point, reached by `push` and
  `auto` alike — now scans the index between staging and committing
  for line-start `<<<<<<<` / `>>>>>>>` / `|||||||` markers (exactly
  seven, per git's default `conflict-marker-size`; deliberately not
  the bare `=======` divider, which a setext markdown underline puts
  at column 0 in legitimate memory bodies) and refuses with
  file-and-line detail when any are staged. The scan fails closed — a
  git-grep failure refuses rather than commits. Losing the predicate
  race now costs a refusal instead of a corrupt commit; the race
  itself is not claimed gone.

## 3.27.0 - 2026-07-20

A store root that was world-listable, and BM25 pricing term rarity
against the wrong collection. Minor rather than patch because the index
schema bumps to v6 (one-time rebuild on first use), the store root's
permissions change on disk when it is opened, and search ranking moves
for any store past the 500-memory prefilter threshold.

### Fixed — store permissions

- **The store root was created world-readable and world-listable.**
  `Store.__post_init__` created it with a bare
  `mkdir(parents=True, exist_ok=True)` — no mode — so under the usual 022
  umask every store root on disk was `0o755`. Immediately below it, the
  tombstone directory already took an explicit `mode=0o700` with a
  comment saying not to rely on the caller's umask; the reasoning was
  written down and simply never applied to the root.

  This is a disclosure, not a tidiness point: a memory's **filename**
  embeds the first ~43 characters of its summary, so a listable root
  hands any local account the gist of the whole store from `ls` alone —
  `2026-07-20-acquisition-talks-with-northstar-closing-in-<ulid>.md`.
  The `0o600` on the bodies never came into it, and SECURITY.md names
  filesystem permissions as the access-control boundary.

  Roots created by earlier versions are healed when the store is opened.
  The heal only ever CLEARS group/other bits, is POSIX-only, and is
  best-effort: a sandboxed or network filesystem that refuses `chmod`
  leaves the store fully usable and `doctor` reports the residual
  exposure rather than failing to open it.

- **`doctor` reports a root it could not tighten**, and its `--fix` chmod
  is reachable again. The fixer previously returned early whenever the
  directory was writable — which a `0o755` root is — so the chmod five
  lines below could never run for the one mode nearly every store had.
  The check heals before it judges, so a legacy root does not turn
  `doctor` red on the first run after upgrading; only a chmod that
  genuinely could not land surfaces as `warn`.

### Fixed — search ranking

- **BM25 priced term rarity against the wrong collection.** Above the
  500-memory threshold the FTS prefilter hands the ranker at most 50
  candidates, every one of them present *because* it matched the query.
  Document frequency counted over that pool makes the query's own
  discriminative terms look ubiquitous — df approaches N by construction
  — and Okapi IDF collapses toward zero for exactly the terms that should
  dominate, degenerating BM25 into length normalisation plus recency.
  Measured through the live handler on a 608-memory store where 8
  memories carried the queried term: `IDF` 0.0572 pool-derived against
  4.2718 corpus-wide, a 74x error. The same arithmetic on a 63-memory
  store with 3 carriers reads 0.1335 against 2.9 — the gap narrows with
  the corpus but never closes.

  Frequencies now come from the index, over the collection the search
  will actually rank — scopes, excluded scopes, repo and worktree, the
  same admission rule `_filter_candidates` applies. That matters under
  auto-scope, where whole-store frequencies would price rarity against
  memories the caller cannot retrieve. The rule is evaluated in Python on
  index rows rather than pushed into SQL, because `repos_match` compares
  on `(host, owner, name)` and consults per-process alternate spellings —
  a SQL filter would quietly disagree with the ranked set for precisely
  the multi-remote stores that mechanism exists to serve.

  Stores below the threshold are unaffected: there the candidate pool
  already is the collection, so the shipped ranking is unchanged.

- **`migrate` left the index holding pre-migration frontmatter.** It
  rewrites `.md` files directly rather than through a Store mutator, and
  nothing kept the index in step — the startup divergence check compares
  counts, and rewriting scopes or an origin block in place changes none,
  so the skew was invisible. A migration that changed anything now flags
  the index for rebuild, which fails safe (search falls back to the full
  scan) and auto-heals on the next store construction.

### Changed

- **Index schema v6** adds `origin_repo` / `origin_worktree`, mirroring
  the origin block already on each `.md` file, so search admission can be
  decided without parsing bodies. As with every schema bump the existing
  index is dropped and flagged for rebuild; the markdown files are
  canonical and nothing is lost. Search routes to a full scan until the
  rebuild completes.

- **The server's `instructions` block no longer carries the `/loop`
  sentence.** It told every connecting client to call `episode_handoff`
  at entry and `episode_write` at exit — a directive for one operator's
  audit-loop harness, resident in the system prompt of every user who had
  no such workflow. The `episode_*` tools are unchanged and still
  documented in their own descriptions.

## 3.26.0 - 2026-07-20

Silent event-log data loss, and three ways `sync` could commit conflict
markers into a memory store. Minor rather than patch because the on-disk
rotated-archive filename changes shape, `PathDriftReport` gains a field,
and `sync push` / `sync auto` / `sync pull` now REFUSE in situations
where they previously succeeded. Read the upgrade notes at the bottom
before upgrading a store you sync.

### Fixed — event log

- **Cross-shard rotation destroyed whole segments.** 3.24.0 sharded the
  active event log and gave each shard its own append lock, but left the
  rotation namespace global: the archive stem carried a one-second
  timestamp and no shard component, and the `.rotating` holding path was
  never existence-checked at all. Two shards crossing `max_bytes` in the
  same second both renamed onto the identical path, and the second
  `os.replace` unlinked the first shard's entire segment. `record()`
  swallows the exception, so the only trace was a `rotation compress
  failed` warning. Reproduced across separate OS processes; at a 500 KB
  segment size single collisions destroyed hundreds to thousands of
  events. Uniform crc32 striping makes shards fill in phase, so
  same-second rotation is the correlated case under exactly the
  concurrent workload sharding was built for — not a rare interleaving.
  Rotation names are now partitioned by shard and both the archive and
  the holding path are probed before a name is taken.
- **`iter_all_events` was no longer chronological.** It yielded every
  archive before every active segment — sound when one active file
  rotated wholesale into strictly-older archives, wrong once shards
  rotate independently, because a quiet shard's active segment can hold
  events older than a busy shard's archive. It now `heapq.merge`s
  per-shard archive chains with the active stream on the event
  timestamp. Untagged archives from a pre-3.26 store have no known shard,
  so they form one chain ordered among themselves by mtime; ordering
  across that chain's members is best-effort by construction.
- **`iter_events_window` could miss a shard's rotated history.** It
  decided coverage from the globally-oldest event, so a single cold shard
  holding a stale event suppressed the rotated-segment prepend for a
  different shard that had just rotated. A session's own `search` event
  could go invisible to the retrieval shield, re-firing the turn as a
  false `search_miss` and inflating the published silent-miss rate.
  Coverage is now decided per active segment.
- **Same-second archives sorted by filename.** `_archive_sort_key` moved
  off `mtime_ns` (a `.gz`'s mtime is when compression finished, not when
  rotation happened) and now tiebreaks same-second segments on write
  evidence rather than alphabetically.
- **Crash-orphan recovery could reclaim a live rotation.** The sweep now
  skips holding files tagged for another shard and runs under a
  store-wide rotation lock.

### Fixed — sync could commit conflict markers

Three separate paths let `git add -A` stage a conflicted file as if it
were resolved, committing `<<<<<<<` into a memory body — permanently, and
in one case onward to every clone.

- **`sync push` committed AND pushed them.** It never consulted the
  conflict guard. Verified end to end: `push` returned
  `committed=True, pushed=True` and left markers in a memory body at the
  bare remote's `main` tip.
- **`sync auto` committed them through a lock race.** Its guard ran
  *before* taking the sync lock, so a conflict arriving during the wait
  was invisible. In the worst shape — a conflicted `git stash pop`, which
  leaves no sentinel file and no remote divergence for the follow-on
  rebase to trip on — `auto` returned normally, with no error at all, and
  the markers reached the remote. The guard now runs inside the lock, as
  `push` and `pull` already did.
- **The guard keyed on the wrong thing.** It probed for rebase sentinel
  files, which cannot see a conflicted merge, cherry-pick, revert, or
  stash pop. It now gates on git's seven unmerged porcelain codes
  (`DD AU UD UA DU AA UU`), which covers all of them.

A residual remains and is documented rather than claimed closed: the sync
lock serialises bettermemory's own operations, not the user's hand-run
git, so a conflict created between the guard and `git add -A` is still
possible. The window is short but not zero.

### Fixed — diagnostics and reporting

- **`doctor --fix` deleted user-added `.gitignore` lines**, rewriting the
  store's file wholesale. It now reconciles additively, and `sync` does
  the same reconciliation on push rather than only at init — so a store
  created before 3.24.0 finally gets the sharded-event-log ignore rule
  instead of pushing raw telemetry to its remote.
- **`doctor`'s event-log check probed only the first shard** while
  returning a verdict covering all of them, so a mispermissioned segment
  was invisible behind a green result.
- **`eval --report` published misleading figures**: window-labelled
  columns carrying all-time counts, phantom sessions from admin CLI
  events, and untelemetered tools rendered indistinguishably from
  never-called ones.
- **The FTS5 divergence check cried wolf** on healthy stores, reporting
  in-flight writes as an index desync and burning its one-shot warning
  budget so a real desync could never be reported. It now settles on the
  writer's lock instead of a timer.

### Fixed — path drift

- **3.25.2's route-suppression overshot.** It stopped inventing drift and
  started hiding it: `/srv/docker/gitea`-shaped citations, and any
  home-rooted path written in its absolute form, were silently dropped
  rather than reported missing. The two spellings of one path now agree,
  case-insensitive volumes are handled, and the suppressed set is
  visible in a new `dropped_as_route` bucket on `PathDriftReport` — an
  in-process bucket; it does not yet reach the MCP response surface.

### Added

- **`tests/test_doc_claims.py`** — a CI guard on the truth of shipped
  prose. It extracts checkable claims from the changelog, README and
  docs (paths, symbols, test counts, line citations) and fails when the
  repo does not support one. Known-false claims sit in a ratcheted
  allowlist: an exemption that stops matching a real failure also fails
  the suite, so it cannot outlive the defect it excuses. This exists
  because a documented claim being false is the single most common
  defect class in this project's history.

### Upgrade notes

- **Rotated archives change filename shape** to
  `.events-{ts}-s{NN}.jsonl.gz`. Existing untagged archives are still
  read and recovered; no migration is needed and nothing is rewritten.
  Tooling that globs the old shape by hand should be checked.
- **`sync push`, `sync auto` and `sync pull` now raise** when the store
  has unmerged files, instead of committing them. If you run `sync auto`
  from cron or a shell alias, it will start failing on a conflicted
  store rather than silently corrupting it. Resolve the conflict and
  re-run; the error names the files.
- **`sync push` now updates the store's `.gitignore`** to the current
  canonical set. On a store initialised before 3.24.0 this adds the
  sharded event-log rule, which stops raw event telemetry being pushed
  to your remote. User-added lines are preserved.

### Erratum (2026-07-24)

The entry above is left as it shipped. It is incomplete: tag
`v3.26.0`'s window (`v3.25.2..v3.26.0`) carries ten substantive fix
commits the entry left unrepresented — which is what the
release-window coverage check in `tests/test_changelog.py` measures:
representation inside one release's own section, not presence
somewhere in the file. Nine of the ten are mentioned nowhere else in
this file either — the same omission class as v3.24.0's `096218e`
and the two commits the v3.28.0 erratum repairs. `b3cc470` is the
exception, and the reason this claim is scoped to the entry rather
than to the file: the 3.25.1 erratum already names that SHA as the
follow-up that routed the missed fourth rename site through
`replace_atomic`, and did so in the file as `v3.26.0` shipped and as
this erratum was written. What `b3cc470` never got was a bullet in
the notes of the release that carried it. The coverage check judges
only the newest tag's window by design — older entries are a frozen
record it deliberately does not re-litigate — so nothing in CI forces
this repair: the ten surfaced by retro-running that check against
this window (the adjacent `v3.26.0..v3.27.0` window retro-runs
clean), and the repair is editorial. The entry summarized four Fixed
themes and dropped the rest of the round wholesale — several of the
ten touch the very subsystems it covers. Stated now as the bullets
the release should have carried:

- **A 3.24.x/3.25.x store's rotated history went invisible to
  windowed reads (`7f76801`).** The shard-partitioned rotation
  namespace (`eace517`, the first event-log bullet above) made
  `_newest_rotated_segment_for_shard` accept only candidates whose
  parsed shard matched the active segment's, deliberately refusing
  untagged ones. A store upgrading from 3.24.0/3.25.x is exactly the
  shape that owns ONLY untagged archives — its active log was already
  sharded while rotation had not yet learned the `-s{NN}` tag — so its
  windowed reader could never reach its own rotated history until
  every pre-upgrade archive aged out; a regression, confirmed by
  running the pre-`eace517` module against identical on-disk state.
  The fallback is restored: tagged candidates win, untagged ones are
  eligible only when a shard has none — over-inclusion of same-store
  events, which the ts-merge and the caller's window filter absorb,
  versus under-inclusion that loses events the caller cannot recover.
  Same commit: window coverage now derives from the union of the
  shards with an active segment and the tagged shards among the
  rotated candidates, so a shard with rotated history but no active
  file on disk (reachable mid-rotation and after a crash) still gets
  its history prepended; `_archive_sort_key` stopped ranking archives
  by mtime — a segment recovered from a days-old crashed rotation
  carries the mtime of TODAY's compression, so recovery displaced
  real history — and ranks by the rotation timestamp stamped in the
  filename (the "moved off `mtime_ns`" half of the same-second bullet
  above; the write-evidence tiebreak arrived separately, `f6659b8`);
  and `Recorder.session_id` is folded through `_safe_stem_component`
  before it lands in a rotation filename, where a separator or `..`
  could have pointed the rename outside the store root.

- **The embedding-cache rename skipped the Windows-safe helper
  (`b3cc470`).** 3.25.1 added `replace_atomic` — a bounded retry for
  Windows' refusal to rename over an open destination — and wired
  three call sites; a fourth was missed: `flush_persistent_cache` in
  `semantic.py` renamed the embedding-cache `.npz` into place with a
  bare `Path.replace`. The miss was structural: the rename primitive
  has four stdlib spellings and the 3.25.1 sweep grepped for one. The
  site is a genuine instance — cache hydration opens the destination
  with no lock, so a second MCP server on the same store is exactly
  the open handle Windows refuses, and the flush's enclosing
  `except Exception` swallowed the resulting PermissionError into a
  warning. The Windows symptom was an embedding cache that silently
  never persisted, re-embedding the whole store on every start. The
  rename now routes through the helper, and the durable half is an
  AST guard in `tests/test_fsutil.py` that finds every rename-shaped
  call in the package — all spellings, aliased imports included — and
  permits them only inside the helper itself.

- **That guard was itself half-closed, and flush failures stayed
  unobservable (`9a7b087`).** An adversarial verifier planted
  `shutil.move` in the package and the new guard passed green:
  `shutil.move` degrades to `os.rename` on a same-filesystem move, so
  it carries the identical open-destination exposure. Zero live
  instances existed — a guard repair, not a live bug — but the
  detector was an enumeration of known spellings, so widening it by
  one name would repeat the mistake at a different offset. It now
  also covers `os.renames`, Python 3.14's `Path.move` /
  `Path.move_into`, and every import aliasing of `shutil` — and, the
  part that ends the pattern, a test DERIVES the expected coverage
  from the running stdlib, so a spelling nobody thought of turns the
  guard red instead of leaving it vacuously green. The arity rule's
  docstring had justified itself with a false premise
  (`datetime.replace` is not keyword-only); it now names the accepted
  false positive instead. Same commit: `replace_atomic` joined
  `_fsutil.__all__`, and the embedding-cache flush gained a
  consecutive-failure counter (`persistent_cache_flush_failures`)
  with a WARNING-to-ERROR escalation at three in a row, because a
  cache that permanently cannot persist was indistinguishable in the
  log from one that lost a single race.

- **`flock_excl`'s prose mis-dated its own history and let callers
  infer a bounded POSIX wait (`02c37d7`).** Three comments dated the
  removal of the Windows lock's silent no-op to 2.7; the branch is
  still a bare `yield` at `v2.7.3` and the commit introducing
  `msvcrt.locking` first ships in `v3.0.0`, so all three now read
  3.0.0 — the correction `82b010a` had already made in the test
  suite. And the docstring named the 30s `BETTERMEMORY_FLOCK_TIMEOUT`
  ceiling only in its Windows paragraph while saying nothing about
  the POSIX wait — the likely origin of the false sync-lock comment
  `60b7553` had to delete, which applied that ceiling to a POSIX
  acquire that has none. The docstring now states the asymmetry from
  both ends — the POSIX acquire is blocking and unbounded (plain
  `LOCK_EX`, no deadline; the env var is read only by
  `_flock_windows`), the Windows acquire is bounded and raises
  `TimeoutError` — with behavioural and AST pins so neither claim can
  rot silently.

- **`migrate --repair`'s printed action list could lie, or never
  arrive (`8ef0f2f`).** The report is what a user reads before
  applying a bulk mutation over the whole store, and it had three
  ways to go wrong. A torn record whose `scopes` value held a
  non-string element raised `TypeError` from outside the per-file
  try/except — the routing lookups hash every element — so ONE
  malformed memory aborted the entire plan, with nothing repaired and
  no report at all; scopes are now screened through
  `_routable_scopes`, the bad record lands in `report.malformed` (the
  CLI prints it as "Malformed (skipped)") and planning continues,
  with `plan_repair` filtering identically so the exported entry
  point cannot abort for a direct caller either. The
  `repaired_anchored` / `repaired_demoted` breakdown was incremented
  BEFORE the write was attempted, so a failed write left the summary
  overstating what landed; both counters now move only after a
  successful write, keeping the dry-run action list equal to what
  apply persists on the success path. And
  `migrate_origin_in_directory`'s docstring claimed both flags are
  inert when `repair=False` — untrue for `keep_global`, which gates
  the legacy backfill on exactly that path; it now states each flag's
  real gating.

- **Four more window-labelled "Events scanned" figures were all-time,
  and phantom sessions had a second way in (`f90fa18`).** The
  diagnostics bullet above covers `5832717`, which fixed the markdown
  denominator note; the text renderers were still wrong:
  `render_text`, `render_threshold_sweep_text`,
  `render_widening_preview_text` and `render_tool_usage_text` each
  stamped a "— last {window}" header over `total_events_scanned`,
  which counts the WHOLE log (marker resolution runs ahead of the
  window filter by design). `ThresholdSweepReport`,
  `WideningPreviewReport` and `ToolUsageReport` now carry their own
  window-scoped `events_in_window` twin, computed over exactly the
  population the all-time counter covers, and all four renderers read
  it. The session-tally exclusion also became two-axis
  (`is_admin_recorded_event`): `consolidate --acknowledge-debt`
  records `kind="use"` rows — a kind genuine sessions also emit, so
  kind-based exclusion structurally cannot catch them without
  blinding the tally to real sessions — under a throwaway session id,
  and the rows' `cli_` attribution prefix (verified present on the
  real write path) is the second axis. Scoped to the session tally
  only: those rows still count as genuine endorsements. An AST parity
  scan now holds any literal fork of the admin-kind roster equal to
  the canonical set, because the comment claiming every consumer
  reads the shared constant was false at its own introduction —
  doctor carried a hand-written copy.

- **The fifth all-time-under-a-window-label surface (`d85798e`).**
  `WideningDetailReport` declared only the all-time counter, and its
  `to_dict` emitted it right next to `window_seconds` — the CLI dumps
  that dict verbatim for `eval --widening-preview --detail --json`,
  so a JSON consumer read an all-time figure under a window label. It
  has no "Events scanned" text row, which is how the `f90fa18` sweep
  missed it and then asserted in a comment that the report publishes
  no event count at all. The report now carries the
  `events_in_window` twin, fed from the count the shared audit walk
  already computed, and an AST enumeration test asserts that every
  dataclass in the module declaring the all-time field declares the
  window twin, and that every `to_dict` emitting one key emits the
  other — the convention is enforced rather than left to an eye that
  had by then missed a surface twice.

- **`doctor`'s cadence census applied half the admin classification
  (`69decd3`).** `_check_audit_turn_cadence` excluded admin-recorded
  events by the kind roster alone, so the `kind="use"` rows that only
  `f90fa18`'s attribution axis can catch still landed in its session
  census: one real session plus one `consolidate --acknowledge-debt`
  run put the census at two and flipped the check from `ok` to
  `warn` — the exact false positive the two-session floor was added
  to kill, re-entering through the axis this consumer wasn't reading.
  The census now calls `is_admin_recorded_event` and holds no roster
  of its own, and an AST scan fails any module outside `eval.py` that
  names either axis in code — because "imports the shared constant"
  had proven satisfiable while still implementing half the rule.

- **Two green-in-isolation repairs collided on a changed sync return
  type (`e88fc88`).** One repair changed `_reconcile_gitignore` to
  return a `_GitignoreReconcile` instead of a bare list (so "every
  pattern already present" and "could not read/write the file" stop
  collapsing onto one `[]`), while another rewrote doctor's
  `_fix_sync_gitignore` to reuse that helper; each was verified
  against a base where the other did not exist, their file sets were
  disjoint, and the integrated result called `len()` on the
  dataclass. Unwrapping the added-lines list was not enough: the same
  sync change stopped RAISING on write failure — it stands down and
  reports — so doctor's `except OSError` no longer saw that case. The
  result now carries `failed_stage` ("read" / "write" / None) and
  doctor branches on it: a write stand-down (knew exactly what to
  append, could not) reports an honest not-applied fix result; a read
  stand-down (never learned what an overwrite would destroy)
  correctly declines and leaves the finding manual. `push` and `init`
  still treat both halves identically — never fail a sync over a
  healing side-effect — which is why the distinction lives in the
  result rather than in two return types.

- **The path-drift bucket's honest reach sentence has a commit behind
  it (`211b68f`).** The caveat closing the path-drift bullet above —
  "an in-process bucket; it does not yet reach the MCP response
  surface" — is this commit's finding, not part of the bucket's
  design. The route-suppression repair (`c1ede35`) had shipped a
  `has_findings` predicate on `PathDriftReport` whose docstring
  claimed to BE the retrieval surfaces' emit gate and claimed that
  gate would notice a newly added bucket. Both claims were false, the
  second self-refutingly so: `dropped_as_route` IS a new bucket, the
  real gates (inline expressions in the show/search handlers) never
  consulted the predicate, and nothing noticed. The dead predicate —
  zero consumers outside its own unit test — is deleted, `has_drift`
  now documents itself as one term of the gate rather than the gate,
  and the bucket's reach is stated as measured: in-process callers
  always see it; a mixed report (drift plus suppressed routes) does
  ship it, because the gates emit the whole `to_dict()` once they
  fire; and a route-ONLY report — precisely the case the bucket was
  added for — reached no MCP surface at all, documented as the
  residual defect instead of as shipped observability.

Separately, two sentences in the frozen bullets above do not survive
checking against this same window's commits — already false when the
tag was cut, not rotted after it:

- **"does not yet reach the MCP response surface" was false at the
  cut.** `c1ede35` — the in-window commit that created the bucket —
  also serialised it, so `to_dict()` has carried a `dropped_as_route`
  key from the bucket's first commit; and the real emit gates in
  `memory_show` and `memory_search`'s expanded top hit, which predate
  the bucket and are unchanged at the tag, emit that dict wholesale
  once any of `missing`, `verified` or `expected_absent` fires them.
  So at 3.26.0 the bucket reached the MCP response surface whenever a
  report was emitted for another reason; the sentence's true part is
  the narrower residual the `211b68f` bullet above states as
  measured — a route-ONLY report, the bucket with nothing else to
  fire the gate, reached no MCP surface at all.

- **"ordered among themselves by mtime" was stale at the cut.**
  `7f76801` — also in-window, before the tag — moved
  `_archive_sort_key` off mtime to the rotation timestamp stamped in
  the filename, and rewrote `iter_all_events`' docstring the same way
  ("ordered by their filename rotation timestamp"). As v3.26.0
  shipped, mtime survived only as the same-second tiebreak evidence
  `_segments_in_write_order` collects (`f6659b8`). The same-second
  bullet above already records the move ("`_archive_sort_key` moved
  off `mtime_ns`"), so the entry contradicted itself as it shipped;
  the untagged-chain sentence is the half that never got updated. Its
  best-effort qualifier still holds — the mechanism it names is what
  was wrong.

## 3.25.2 - 2026-07-19

A path-drift false positive that made healthy web-app memories look stale.
Found by dogfooding — a verification sweep over a Go+React project's
memories, where four independent agents flagged the same phantom drift.

### Fixed

- **Bare application routes were reported as missing files.** Route
  suppression (`_is_route`) learns its vocabulary from
  `_DOMAIN_ROUTE_RE`, which only matches domain-qualified URLs like
  `example.com/api`. A memory citing bare routes — `/api/v1/events/
  presence`, `/admin/macros`, `/portal/incidents/new` — produced an
  *empty* vocabulary, so every route fell through to the filesystem check
  and landed in `path_drift.missing`. Identical citations were suppressed
  correctly if the body happened to contain any fully-qualified URL, and
  reported as drift if it didn't.

  Because `path_drift_missing` feeds `staleness_verdict`, this pushed
  healthy memories to `spot_check_recommended`/`required`, spending
  verification attention on records whose only "drift" was imaginary and
  diluting the signal that is supposed to mean *this memory's cited
  ground truth moved*. Web-app memories were worst affected: they cite
  routes constantly and rarely write the host.

  New `_is_multi_segment_routelike` drops a non-existent leading-slash
  candidate that looks like a route. Two escapes keep genuine filesystem
  drift reportable: an **extension** on the terminal segment
  (`/srv/app/config.yaml` reads as a file), and an **existing parent
  directory** (`/Users/me/gone`, `/etc/nope` — the neighbourhood is real,
  so absence is real drift). Single-segment candidates are excluded, so
  the documented remote-host behaviour for `/opt/gophish`-style citations
  is unchanged: those still flow to `missing` until attested via
  `memory_verify(verified_absent_paths=[...])`.

  The check sits **last** in the not-exists block, after the spaced-bare
  and ambiguous-truncation arms, so a prose-glued candidate
  (`/tmp/real-dir TCP/IP`) still reaches the prefix-existence fallback
  that recovers the real path rather than being written off as a route on
  its manufactured tail. It is also behind the `not attested` guard, so
  an explicitly-named path always keeps its drift signal.

### Erratum (2026-07-19)

The entry above is left as it shipped. One of its claims does not
survive checking against the code.

- **The `/opt/gophish` paragraph is wrong on both halves.** It shipped
  saying *"Single-segment candidates are excluded, so the documented
  remote-host behaviour for `/opt/gophish`-style citations is
  unchanged."*

  Wrong on the mechanism: the single-segment exclusion is
  `s.count("/") < 2`, and `"/opt/gophish".count("/") == 2`, so that arm
  never fires for such a citation. What actually preserves it is the
  **existing-parent escape** — `/opt` is a real directory on a POSIX
  host, so the neighbourhood reads as real and the candidate keeps
  flowing to `missing` until attested. The preservation is therefore a
  property of the *local filesystem*, not of the path's shape: on a
  host with no `/opt`, the identical citation is dropped. (This is the
  same environment-sensitivity `_is_multi_segment_routelike`'s own
  platform note already records for Windows.)

  Wrong on the blast radius: "unchanged" is not true of the wider
  class. As 3.25.2 shipped, a multi-segment extensionless path whose
  immediate parent is absent locally is **dropped rather than
  reported** — `/srv/docker/gitea`, `/data/compose/stacks`,
  `/mnt/tank/media` and the rest of the remote-host, NAS and
  deploy-target vocabulary that infrastructure memories cite
  constantly. Those used to reach `path_drift.missing` and no longer
  do. (`main` has since added a third escape for home-rooted
  candidates, so a path under this machine's `$HOME` is no longer
  dropped; the three examples above are not under it and still are.
  `_is_multi_segment_routelike`'s docstring states the drop condition
  as a numbered shape — treat that as canonical over any prose here.)

  Anyone who wants the drift signal back on such a path must attest it
  (`memory_verify(verified_paths=[...])` or
  `verified_absent_paths=[...]`), which puts it behind the `not
  attested` guard and past the route check entirely.

## 3.25.1 - 2026-07-19

A Windows-only durability gap in the atomic write path, found by a flaky
release-gate job rather than by a user report.

### Fixed

- **Concurrent `os.replace` could fail hard on Windows.** The store's
  write discipline is tmp → fsync → rename. POSIX allows renaming over a
  destination another process still holds open; Windows does not, and
  fails with `ERROR_ACCESS_DENIED` (5) or `ERROR_SHARING_VIOLATION` (32)
  — both surfaced by Python as `PermissionError`. That window is
  milliseconds wide and purely transient, but the rename had no retry,
  so it became a hard failure mid-write. Two consequences: a concurrent
  store mutation on Windows could raise `PermissionError` instead of the
  documented `ConcurrentUpdateError`, breaking callers written against
  the documented contract; and
  `test_mark_verified_cas_threaded_one_winner` flaked on the
  `windows-latest` leg, red-lighting the v3.25.0 release run while the
  identical job passed on that commit's CI run.

  New `_fsutil.replace_atomic` wraps the rename in a bounded retry — 5
  attempts, doubling 10ms backoff, ~150ms ceiling. It is deliberately
  **Windows-only** (on POSIX a `PermissionError` from `os.replace` is
  never this race; it means the directory genuinely is not writable, and
  retrying would only delay a real diagnosis) and deliberately **narrow**
  (only `PermissionError` retries — ENOSPC, EXDEV and friends propagate
  on the first attempt, because a blanket `except OSError` would disguise
  a full disk as a slow rename). After the budget the original error
  propagates unchanged, with its true type and errno.

  Applied at all three rename sites: `atomic_write_bytes` (every memory,
  episode and index write) plus the event-log rotation and archive
  renames in `events.py` — the latter two matter more since 3.24.0, as
  sharding multiplied the number of concurrent segment readers.

### Changed

- `test_mark_verified_cas_threaded_one_winner` now records an `errored`
  outcome for a worker that dies in any way other than a clean
  `ConcurrentUpdateError`, and inlines the exception text into the
  assertion message. Previously such a thread appended nothing at all,
  so the failure read "expected exactly one stale-CAS loser, got 0" —
  naming the symptom while hiding the cause, which is precisely why this
  bug took a release-gate failure to surface. The arm does not paper
  over the defect: an `errored` outcome satisfies neither assertion, so
  the test still fails — it just fails while naming the exception.

### Erratum (2026-07-19)

The entry above is left as it shipped. Its coverage claim was too
strong.

- **"all three rename sites" was four.** The entry says the retry is
  *"Applied at all three rename sites: `atomic_write_bytes` (every
  memory, episode and index write) plus the event-log rotation and
  archive renames in `events.py`."* Those three are real and are
  covered (`_fsutil.py` `atomic_write_bytes`, `events.py` rotation,
  `events.py` archive). But they are not all of them: the embedding
  cache's persistent flush in `semantic.py` performs a fourth
  tmp → fsync → rename, and as 3.25.1 shipped it called `Path.replace`
  directly rather than `_fsutil.replace_atomic`, so it never got the
  Windows retry. That release therefore left the site exposed to the
  exact `PermissionError` race it set out to close — two flushes
  against one memory dir on Windows could still fail the rename hard.

  The consequence is milder than for the store proper: the embedding
  cache is a fully recomputable derived artifact, and the flush already
  swallows its own exceptions, so a lost rename costs a cache rebuild
  rather than data. It is a durability gap, not a correctness one — but
  the entry's "all three" asserted a completeness that did not hold.

  The site has since been routed through `replace_atomic` by `b3cc470`,
  which landed on `main` after the `v3.25.2` tag — so, as with
  `eace517` in the 3.24.0 erratum, no release at or below 3.25.2
  carries it and the first release cut from `main` after that point
  does. Stated that way on purpose:
  "in a follow-up release" was the previous wording, and a promise
  about an unnamed future release cannot be checked or falsified.

## 3.25.0 - 2026-07-19

`migrate origin` can now repair an origin that was captured *wrong*,
not just backfill one that was never captured at all.

### Added

- **`migrate origin --repair`.** The original backfill only ever fired
  on memories with no `origin` block, which turns out to be the rarer
  failure. The common one: a write made from a parent directory
  (`~/Documents`) or `$HOME` sits outside any git checkout, so
  `capture()` records a cwd with `repo=None` — and `repos_match` treats
  a null repo as matching *every* caller. The memory silently becomes
  global and leaks into every project's auto-scoped search. Worse, a
  memory written while sitting in project B but scoped to project A ends
  up anchored to B and goes **dark** in A: still listed under
  `projects:a`, never retrievable from it.

  `--repair` lifts the skip and applies two rules to an existing origin:

  | rule | condition | action |
  |---|---|---|
  | anchor | `repo` null, scopes name exactly one mapped repo | adopt that repo |
  | demote | `repo` contradicts one of the memory's own mapped scopes | clear `repo` + `worktree_root` |

  The rules move in opposite directions deliberately. Anchoring makes a
  memory *less* visible, so it demands unambiguous evidence; demoting
  makes it *more* visible, so it is safe on any genuine mismatch. A
  memory spanning two projects cannot be represented by a single-repo
  origin at all, so global is the honest answer for it.

- **`migrate origin --keep-global SCOPE`** (repeatable, requires
  `--repair`). Names a cross-cutting scope — `infrastructure`, `tools`,
  `workflow` — that must never be anchored to one repo, since anchoring
  a genuinely project-spanning memory hides it from everywhere else. It
  guards both the repair and the legacy-backfill routes: honouring it on
  only one would let the older path quietly do the damage the newer one
  refuses to. It never triggers a demote — treating it as one would
  strip the anchor off every `projects:x`+`workflow` memory in a store
  and make the leak dramatically worse.

Both flags are inert unless passed: without `--repair` the migration
path is byte-for-byte unchanged, and still skips any memory that already
has an origin. Only the `origin` block is ever rewritten — body, id, and
every other frontmatter key are untouched. Pair with `--dry-run` first;
repair prints the anchor/demote split so a run is reviewable before it
is applied.

## 3.24.1 - 2026-07-19

Two fixes on top of 3.24.0's event-log sharding — one a pre-existing
privacy leak, one a shard-awareness gap in `doctor`.

### Fixed

- **Rotated event-log archives were being pushed to sync clones.** The
  `sync` gitignore carried `.events.jsonl.*.gz` for the event-log
  archives, but rotated archives are named `.events-{ts}.jsonl.gz`
  (dash after "events", not dot), so the pattern matched *nothing
  real*: every gzipped archive — and every crashed-rotation
  `.rotating` holding file — was staged by `sync push`'s `git add -A`
  and committed to every clone, in plaintext, carrying session ids and
  (in verbatim mode) raw query text. Pre-existing since archives were
  introduced; the structural sidecar-coverage guard could not catch it
  because an archive name is composed from `ARCHIVE_PREFIX` at runtime,
  not a discoverable `*_FILENAME` constant. The pattern is now
  `.events-*`, which covers both the `.gz` archives and the `.rotating`
  holding files, and a test pins the composed names directly. (Note:
  gitignore only stops *future* staging — a clone that already
  committed archives must `git rm --cached` them; run `bettermemory
  doctor`.)

### Changed

- **`doctor` event-log checks are shard-aware.** The writability probe
  and the 0600 permission healer looked only at the legacy single
  `.events.jsonl`; on a sharded store (3.24.0) they now probe a real
  active segment and heal every mispermissioned `.events.NN.jsonl`
  segment in one pass. Shards are created 0600 by the Recorder, so
  this is the safety net for a segment that somehow lost it.

## 3.24.0 - 2026-07-19

One additive feature. Minor rather than patch because the event log's
on-disk layout changes (a new sharded active-file shape) and a new
`sync` ignore line ships — but it is fully backward-compatible: a
pre-upgrade `.events.jsonl` is still read, no migration runs, and
every read/consumer contract is byte-identical.

### Changed

- **Event-log active file is sharded to kill the last global write
  lock (swarm-convergence).** Every `Recorder.record` used to append
  to one `.events.jsonl` under a single `fcntl` flock, so every agent
  sharing a store serialised on it — the Phase-0 fleet benchmark
  (`bench/swarm.py`) measured that lock at ~7-17% of throughput. The
  active log now splits into a fixed set of per-shard files at the
  store root, `.events.00.jsonl` … `.events.{SHARD_COUNT-1:02d}.jsonl`
  (16 shards), and a Recorder picks its shard by `crc32(session_id) %
  SHARD_COUNT`. Writers from different sessions land on different files
  and no longer contend; fixed striping (not one file per session)
  keeps the file count and a reader's simultaneously-open fds bounded
  no matter how many sessions a store accumulates. `iter_events` merges
  the shards — plus any legacy pre-sharding `.events.jsonl`, read-only
  from here on — into chronological order by event `ts` with a
  streaming `heapq.merge`; every other reader (`iter_all_events`,
  `iter_events_window`) and consumer composes on top of it unchanged,
  as do rotation, gzip archives, and crash recovery (still one shared
  scheme). `sync` now also excludes `.events.*.jsonl` so the per-shard
  segments — which carry session ids and, in verbatim mode, raw query
  text — never leave the host. After the change the benchmark's
  event-log tax drops to ~1% (the residual is per-event redaction +
  fsync, not the lock). Nine tests pin the new behaviour: striping,
  per-session shard stability, cross-shard chronological merge with
  per-session order preserved, and legacy `.events.jsonl` merge-in.

### Erratum (2026-07-19)

The entry above is left as it shipped. Four of its claims do not
survive checking against the code and the tag.

- **Not "the last global write lock".** The title claims the event-log
  shard killed the last one. It did not. Every memory mutation still
  serialises store-globally on the FTS5 index: `write`, `update`,
  `mark_verified`, `tombstone`, `restore` and `rename_scope` all call
  `_index_upsert_quietly` / `_index_remove_quietly`, which open the
  single `<root>/.index.sqlite` and write it. SQLite in WAL mode admits
  concurrent readers but still admits exactly one writer at a time
  (contenders wait out the 5-second busy timeout), so the fleet-wide
  write serialisation point moved from the event log to the index
  rather than disappearing. Accurate title: *sharded to remove the
  event-log global write lock*. The index write path is the remaining
  one. `docs/swarm-convergence-plan.md` carried the same overclaim in
  its Phase 1b note ("The one true global serialization point.
  Removed.") and is corrected alongside this erratum.

- **Not "one additive feature" — a second change shipped
  undocumented.** Tag `v3.24.0` also carries commit `096218e`, which
  rerouted `load_one` and `_find_path_for_id` — and therefore
  `memory_show`, `memory_update`, `memory_verify` and `memory_remove`
  — through the FTS5 index. It received no entry anywhere in this file.
  Stated now as the `### Changed` bullet the release should have
  carried:

  - **By-id lookup is index-backed — O(corpus) walk → O(1) resolve
    (swarm-convergence Phase 1).** `load_one` and `_find_path_for_id`
    used to resolve an id by walking the active directory and
    reparsing every file's frontmatter until one matched. New
    `_indexed_path_for_id` resolves id → filename through
    `index.filenames_for_ids` in one indexed query instead. **The walk
    is retained as the authoritative fallback**, not replaced: the
    index is consulted first and the walk runs whenever it does not
    produce a usable answer. The safety property is that a wrong index
    can only cost time, never correctness — `_indexed_path_for_id`
    returns a path only when `_id_still_at_path` re-reads the named
    file and confirms it *still* carries that id, so a row pointing at
    a moved, renamed or tombstoned file yields `None` and the caller
    falls through to the walk, which finds the true path. A stale index
    row therefore degrades the lookup to slow, never to wrong. Two
    observable consequences: (1) the resolver is wrapped in
    `@best_effort`, so a corrupt, locked or unreadable index logs a
    warning **per lookup** (previously such a store simply walked in
    silence) — the message names the id and carries the `bettermemory
    reindex` repair hint; (2) `memory_show` now opens the index
    **twice** rather than once — one purposeful open for the id → path
    resolve plus the existing `links_for_with_status` open — which is
    what `test_no_inbound_show_opens_index_twice` pins, a third open
    still failing it.

- **"Nine tests" was four.** The release commit (`59a1e08`) adds four
  new test functions, all in `tests/test_events.py`:
  `test_same_session_maps_to_a_stable_shard`,
  `test_sessions_stripe_across_multiple_shard_files`,
  `test_iter_events_merges_shards_preserving_per_session_order` and
  `test_legacy_events_jsonl_merges_in_after_sharding`. Its other test
  edits are helper refactors, not additions. The "nine" appears to have
  been carried over from the one other commit in the tag whose message
  states a test count — `096218e`, which likewise claims nine for
  `tests/test_indexed_lookup.py` — that commit adds eight test
  functions to that file, with no parametrisation to make up the
  difference. (Counted on `096218e` itself. The file has grown since,
  so a count taken against `main` answers a different question — which
  is why this one names the commit.)

- **"every read/consumer contract is byte-identical" was false as
  shipped.** The preamble's backward-compatibility claim, and the
  Changed bullet's "cross-shard chronological merge", both assert a
  read contract that sharding did not in fact preserve:
  `iter_all_events` is not chronological post-sharding. The bullet's
  per-session ordering claim does hold (a session maps to a fixed shard
  by `crc32(session_id) % SHARD_COUNT`, so its own events stay in
  append order within one file); the *global* cross-consumer ordering
  claim does not.

  This is a code defect, not just a documentation one. The fix has
  landed on `main` as `eace517` ("partition rotation by shard and make
  read order real"), which replaces the archives-then-active walk with
  a `heapq.merge` on the event `ts` across per-shard archive chains,
  untagged legacy archives, orphan `.rotating` segments and the active
  stream. The same commit repairs two sibling assumptions the shard
  split left standing: an unshared rotation namespace (two shards
  crossing `max_bytes` in the same UTC second could derive the same
  holding path, and the second rename destroyed the first shard's
  segment) and `iter_events_window`'s global `oldest_ts` shield.

  **The restored ordering is not total, and the exception belongs in
  this entry rather than rounded off it.** `heapq.merge` orders its
  input streams against each other; it cannot repair a stream that is
  not itself sorted. Every archive cut by a post-`eace517` rotation
  carries a shard tag (`_next_rotation_paths` derives every candidate
  stem from `{ARCHIVE_PREFIX}{ts}-s{shard:02d}`), so it joins its own
  shard's chain, and such a chain is ts-ordered by construction — a
  shard rotates its own segment wholesale, under that shard's append
  lock. Archives whose names predate the tag have no recoverable shard
  — `_rotated_segment_shard` returns `None` for them deliberately
  rather than guessing shard 0 — so they all share ONE chain, sequenced
  by the rotation timestamp in the filename. Rotation time is not event
  time: two untagged archives cut by different shards can hold
  overlapping event-time ranges, and when they do, that chain is not
  ts-sorted and the merged output is not either.

  So the guarantee is **total across shard-tagged archives, and
  best-effort across untagged ones**. That bites a store which rotated
  under a released version between 3.24.0 and 3.25.2 — the window where
  the active log was sharded but all 16 shards still rotated into one
  untagged namespace. A store that only ever rotated before 3.24.0 is
  unaffected in practice: there was a single active log, so its
  untagged archives came off one writer stream and are already in event
  order. No events are lost in any of these cases.

  Two things that would be convenient to assume, and are not true.
  No retention policy removes rotated archives, and no code path in the
  package deletes a canonical one. The prune routines that do exist are
  about other data: `EpisodeStore.prune_old_sessions` drops episode
  session DIRECTORIES past a TTL, and `Store.prune_tombstones`
  hard-deletes tombstone files past a retention cutoff — neither so
  much as reads the event log's filenames. `events.py`'s own deletes
  reach only non-canonical scratch: the `.rotating` holding file and
  the `.jsonl.gz.tmp` of an in-progress compression, never a
  `.jsonl.gz`. So an affected store does not grow out of the exception
  on its own; the untagged archives stay until someone removes them by
  hand. And post-`eace517` code can still *create* an untagged archive:
  `_recover_orphan_rotations` reclaims a pre-upgrade `.rotating` orphan
  by compressing it under its existing untagged stem. `iter_all_events`'
  own docstring flags the same untagged chain as its one approximate
  stream — but that docstring also says the chain "degrades to exact as
  pre-3.25 archives age out of a store", which is the assumption this
  paragraph opened by correcting.

  `eace517` landed on `main` after the `v3.25.2` tag, so no release at
  or below 3.25.2 contains it, and the first release cut from `main`
  after that point does. That phrasing is deliberate: it is checkable
  at any time (`git tag --contains eace517`) and it stays true through
  the release that ships the commit, rather than needing someone to
  remember to edit it during the cut. Both earlier revisions of this
  sentence instead stated release membership as of the day they were
  written — the first sent the reader up the file to an entry that did
  not exist; the second asserts the commit is in no tagged release,
  which is true only until the next cut.

## 3.23.0 - 2026-07-12

One additive feature. Minor rather than patch because `eval --report`
is a new CLI mode (and `--output` a new option); nothing renamed or
removed — every existing eval mode and its `--json` shape are
byte-compatible.

### Added

- **`eval --report` — the telemetry as a publishable artifact.** One
  flag renders what `bettermemory eval` already computes into a
  self-contained markdown document: store shape (counts only), the
  three effectiveness rates with Wilson 95% CIs for the `--since`
  window and all-time side by side, a reading-the-numbers-honestly
  section, the per-model audit slices, the v1–v4 threshold-sweep
  counterfactual, the tool-usage top ten, and a versioned methodology
  footer. `--output FILE` writes it to disk. The safety property is
  the point and is a tested contract, not a hope: the report carries
  rates, counts, CIs, model names, and static registry names ONLY —
  a canary test seeds a store whose memory bodies, summaries, scope
  names, logged queries, session ids, and directory name all carry a
  token and asserts it appears nowhere while the seeded counts still
  land as numbers. So the output is publishable as-is: share your
  distribution (the docs/eval-results.md refreshes are generated this
  way from now on) without sharing your store. `--report` combined
  with `--json` or any other mode flag is a hard error; everything
  else about `eval` is unchanged.

## 3.22.1 - 2026-07-12

Patch release: repairs from the multi-agent post-ship audit of the
3.22.0 window and the re-audit of the repairs themselves (14 confirmed
findings, every one verified with a live reproduction; one
release-blocker). No new surfaces; `--json` shapes unchanged except
where noted.

### Fixed

- **`doctor --fix` no longer corrupts its own verdict.** The
  `doctor_fix` audit event was counted by the audit-turn-cadence
  check's session census, so a fully-healed low-cadence store (one
  real session, zero `turn_audited` — hookless, CI, or programmatic
  installs) exited 1 after a successful fix, with a false Stop-hook
  warning persisting for the 7-day window. Admin/CLI event kinds
  (`doctor_fix`, and `silent_miss_cutoff` from `consolidate
  --acknowledge-misses-before`, which had the same phantom-session
  effect) are now excluded from the census, and the exclusion set is
  parity-pinned against eval's side-effect registry so the two can't
  drift.
- **The telemetry opt-out now really means everywhere.** `doctor
  --fix` and `bettermemory ingest` both constructed bare Recorders,
  so an applied fix or an ingest run on a store with `[telemetry]
  enabled = false` created and appended to the event log the user had
  turned off. Both thread the config now — so the 3.22.0 sentence
  "every applied fix lands one `doctor_fix` event" holds only with
  telemetry on — and a class-check test enumerates every Recorder
  construction site under `src/` and fails if any omits
  config-sourced `enabled=`.
- **Symlinked event logs are refused, not chmod'd through.** The
  event-log permission fixer declined nothing before: on a store
  whose `.events.jsonl` was a symlink it chmod'd the link's target
  and appended audit bytes into it. It now declines (mirroring the
  lockfile fixers' refuse-on-symlink standard), and the check's fix
  hint for a symlinked log is a non-executable steer instead of a
  pasteable `chmod` that would hit the target.
- **The event-log writability check now probe-appends for real**, as
  its docstring always claimed — closing a Windows false-green where
  `os.access` consults only the readonly attribute the fixer itself
  just cleared. The probe opens without `O_CREAT`, so a log that
  vanishes mid-run is reported, never silently recreated as a
  umask-mode file outside the Recorder's 0600-on-first-write path.
- **`doctor --fix` re-runs diagnostics when any fix was attempted,
  not only when one applied.** The vanished-artifact race (a stale
  lockfile disappearing between diagnosis and fix) previously exited
  1 on a healthy store beside a payload whose fix result already said
  the check healed; it now exits on the honest post-fix state.
- **`--fix` text output tells the neighbour-heal story straight.**
  Checks healed as a side effect of another fix were listed as
  "manual-only finding(s)" pointing at hints that don't exist; they
  now render as healed-by-another-fix, and the manual list is
  computed against post-fix state.

### Tests

- The `--fix` contract is now mutation-hardened: a CLI-level
  mixed-outcome exit pin (applied fix + still-red post → exit 2)
  kills two mutants that previously survived the full suite; the
  audit trail is pinned from both sides (no `doctor_fix` event for a
  non-applied fix; mixed `fixes_applied` counts); every per-fixer
  real failure branch and every decline-guard stand-down path is
  covered with no-mutation pins; the plain `doctor --json` dispatch
  is pinned (shape, no fix keys, exit code).
- Child-interpreter spawns across the test suite share one shielded
  environment helper, so macOS iCloud-synced checkouts stop silently
  skipping the subprocess CLI tests.

### Docs

- `docs/installation.md` §Troubleshooting documents `--fix`.

## 3.22.0 - 2026-07-12

One additive feature: doctor grows the repair half of its contract.
Minor rather than patch because `doctor --fix` is a new CLI surface
(and `--json` with `--fix` a new payload shape); nothing renamed or
removed — plain `doctor`, its 0/1/2 exit codes, and the bare `--json`
shape are byte-compatible.

### Added

- **`doctor --fix` — the safe repairs, applied.** Doctor's checks
  already print pasteable, shell-safe remediation hints; `--fix`
  executes the safe subset by calling the same underlying functions
  the hints point at (never by re-parsing hint strings): store /
  event-log permission heals (`chmod 0700`/`0600`), search-index
  rebuild (the exact `reindex` code path — the index is derived
  state), removal of the 0-byte 3.15.0 `<config>.lock` artifacts (the
  same heal `init` applies; a client's live directory lock is never
  touched), and the sync repo's `.gitignore` refresh (`sync init`'s
  own idempotent write — a partial fix reported honestly as
  still-red, since gitignore cannot untrack, but without it the
  manual `git rm --cached` remediation silently un-does itself on the
  next `sync push`). Each fix re-runs its check and is reported
  "fixed" only when the re-run is green; the exit code reflects the
  POST-fix state, so `doctor --fix && …` keeps the existing 0/1/2
  contract. Plain `doctor` remains the dry run. Destructive
  remediations — untracking, history rewrites, MCP client config
  edits, anything that could delete possibly-unique user content,
  anything on another host — stay hints forever. Every applied fix
  lands one `doctor_fix` event in the store's event log. `doctor
  --json` without `--fix` keeps its exact prior shape; with `--fix`
  it gains a `fixes` array and a `fixes_applied` count.

## 3.21.0 - 2026-07-12

Two small features that close long-open design questions, plus the
finish of the trust-layer copy retune. Minor rather than patch: `sync`
stops staging a directory it previously pushed (`episodes/` — a new
line in every store sync repo's refreshed `.gitignore`), and
`memory_proposals` gains a `credential_warning` status that
tool-response readers parse. Nothing renamed or removed.

### Changed

- **Episodes are host-local by design — `sync` now excludes
  `episodes/`.** The transient tier synced only by omission: session
  run-state carries host-absolute worktree paths, the 30-day TTL prune
  keys on mtimes a clone's `git checkout` would reset, and
  `episode_handoff` adoption is worktree-strict, so a synced episode
  was filtered on arrival anyway. A structural guard now forces the
  sync decision for every store-root directory constant the way the
  sidecar guard forces it for dotfiles — `.tombstones/` takes the
  deliberate stays-synced seat, because a removal made on one host
  must stay restorable from every clone. Migrating a pre-3.21.0
  multi-host sync repo: untrack on every host before its next
  `sync pull` — the `sync_tracked_ignored` hint now spells out that
  pulling another host's untrack commit deletes your tracked working
  copies of those paths.
- **`memory_proposals` accept refuses credential-bearing bodies with
  the same structured shape `memory_write` uses.** Previously a raised
  error, which reached MCP clients as an opaque tool error; now
  `{status: "credential_warning", markers, hint}` with the proposal
  still queued and nothing persisted. The CLI human lane keeps exit 2
  with the detector kinds and the `--acknowledge-credential` spelling
  in the message; under `--json` the refusal is data on stdout with
  exit 0 (the `not_found` precedent), now test-pinned. `docs/api.md`'s
  contract line also gains the `acknowledge_credential` parameter it
  had been missing since the flag shipped.

### Fixed

- **The CLI `--help` description joins the trust-layer framing.** The
  argparse lead still opened with the retired 1.4.2 tagline after the
  positioning pass moved every other identity surface; both smoke-test
  pins retuned with it. The README now says a wrong verdict is *owed*
  a public postmortem — with zero incidents on file, present-tense
  "given" overclaimed.

### Internal

- `tests/test_doctor.py`'s local git-discovery-ceiling helper folded
  into the shared `tests/conftest.py` helper its docstring already
  sanctioned; six call sites repointed, the doctor-specific provenance
  kept in the shared docstring. `doctor --fix` promoted to the ROADMAP
  as the next planned feature.

## 3.20.0 - 2026-07-11

An audit release over the 3.19.0 follow-up queue: four parallel drain
rounds, a set-audit of the whole window as one range, a repair round, a
re-audit of the repairs, and one repair-of-repairs. Every fix carries a
regression test that was verified to fail against the pre-fix source.

Minor rather than patch: `bettermemory doctor --json` gains two check
names (`sync_tracked_ignored`, which can fail with a `tracked_ignored`
details key, and `store_nested_in_parent_repo`, warn-only, with
`parent_toplevel(s)` / `store_prefix` / `tracked_sidecars` /
`tracked_by_parent` / `scanned_parent_toplevels` / `patterns_checked`
details); doctor gains a new exit-code outcome (a store whose sync repo
still tracks a pre-denylist sidecar now exits 2 — deliberate, it flags a
real plaintext leak); and three functions now emit `DeprecationWarning`,
which `-W error` consumers will see. Tooling that branches on doctor's
exit code or parses its JSON sees new values; nothing was renamed or
removed.

### Security

- **`doctor` now finds the sidecar leaks that 3.19.0's gitignore fixes
  could not stop retroactively.** A sync repo initialised before the
  denylist landed keeps already-tracked sidecars tracked — gitignore only
  stops future staging — so the proposals queue and friends kept pushing
  in plaintext. The new `sync_tracked_ignored` check detects any tracked
  path matching the denylist and prints the full remediation (`git rm
  --cached`, history rewrite for anything already pushed, secret
  rotation). This is the designated migration surface for the 3.19.0
  leak class: run `bettermemory doctor` once after upgrading.

- **`doctor` also looks outward: a foreign parent repo tracking files
  under a nested store is now detected.** A store living inside a
  dotfiles-managed home directory (or any enclosing repo) leaks the same
  sidecars through the parent's `git add -A`, invisibly to sync. The new
  `store_nested_in_parent_repo` check walks every enclosing worktree —
  plain nesting, a store that became its own repo after the parent had
  already tracked its files, doubly-nested chains, and stores whose
  `.git` gitfile is broken or dangling (a shape that previously made
  every check stand down while the parent kept staging new captures) —
  and warns with per-repo tracked paths and remediation.

### Fixed

- **Doctor's sidecar matching now agrees with `git check-ignore`.** The
  original translation let `*` cross `/` when matching full paths, so
  `.embeddings.*.npz` could flag `.embeddings.cache/model.npz` — a file
  git legitimately tracks — for destructive remediation, and files under
  ignored directories were missed entirely. Matching is now
  per-component in the store's own frame, pinned by an oracle test that
  compares doctor's verdict against real `git check-ignore` across a
  56-cell matrix. The store-frame part matters: an intermediate fix
  matched the store's *own path components* too, so a store directory
  named `state.tmp` had every legitimate memory inside it reported as a
  leak, with remediation that could never converge. The re-audit caught
  that regression before it shipped.

- **Every command `doctor` tells a user to paste is now shell-safe.**
  Remediation hints interpolated raw paths into `git rm --cached` and
  `chmod u+w` commands: a `:`-leading store name made the command fail
  outright, a space-bearing path shell-split, and a bracket path could
  silently untrack an innocent sibling file. All hint sites emit
  `':(literal)…'` pathspecs through `shlex.quote`, and tests execute the
  emitted commands verbatim and assert they touch only their target.

- **The deprecation fence is mechanical, not review-dependent.**
  `error::DeprecationWarning:bettermemory` never escalated unwrapped
  deprecated calls made *from test files* — `stacklevel=2` attributes
  the warning to the caller's frame — so a future test could silently
  exercise deprecated API. A message-scoped filter line now escalates
  bettermemory-emitted deprecations from any frame, proven both
  directions by a subprocess probe.

- **`bettermemory ui --tunnel` exit-code contract is pinned end to
  end.** A bind failure exits 3 through click to the OS boundary —
  propagation was already correct; it is now tested against both
  remap and swallow mutations, so `systemd`'s `Restart=on-failure`
  behavior can't regress silently.

- **The test suite is hermetic against repo-rooted temp directories.**
  Running with `TMPDIR`/`--basetemp` inside any git checkout used to
  false-fail eleven outside-repo-premise tests (git discovery escaped
  the sandbox and found the enclosing repo) and color-forcing shells
  (`PY_COLORS=1`) false-failed the fence probe. Both closed, with the
  git-discovery ceiling documented and shared in `conftest.py`.

### Deprecated

- **`origin.commits_since`, `origin.commits_touching_pathspecs`, and
  `origin.commits_since_touching_paths`** now emit `DeprecationWarning`
  and will be removed in bettermemory 4.0. All three count commits on
  committer dates with `--since`, semantics the commit-drift path
  deliberately abandoned (a rebase moves committer dates, inflating
  counts; `None`-on-all-dropped erased the empty-vs-unresolvable
  distinction). The author-date mechanism behind
  `verify.resolve_commit_drift_count` is the replacement, and each
  warning names it. They had no production callers — the deprecation
  exists so nobody wires the inflatable semantics back in.
  `CONTRIBUTING.md` now documents two explicit deprecation lanes:
  `warnings.warn(DeprecationWarning)` for Python API, log-once for
  config keys.

### Changed

- Every subcommand's `--help` now states what the command does —
  previously argparse rendered the descriptions only in the top-level
  `bettermemory -h` listing, so `bettermemory doctor --help` printed
  usage and options with no statement of what doctor covers. The
  `--help` smoke sweep derives from the parser registry, so new
  subcommands can't ship without it.
- `doctor`'s user-facing summaries (`--help`, the installation guide)
  describe check categories instead of enumerating check names; the
  previous enumerations had silently gone stale twice.
- The `episode_handoff` tool description was compressed to the
  model-facing minimum, restoring MCP description-budget headroom from
  24 to 318 characters of the 27,250 ceiling — canonical detail lives in
  `docs/api.md` and the module docstring.

### Performance

- `memory_show`'s commit-drift computation resolves the repo toplevel
  once per call instead of forking `git rev-parse --show-toplevel` in
  each helper — one fewer subprocess on the interactive read path.

The structural lesson repeats from 3.19.0, with sharper numbers: all 24
individually-verified fixes came back clean from their own reviews, yet
reading the window across its commits found ten more defects, the
re-audit of the repairs found six, and one of those was a regression a
repair itself introduced. Four driver-side claims (item premises and a
commit message) were falsified by agents or audits along the way. The
between-commits reads stay mandatory.

## 3.19.0 - 2026-07-10

An audit release. Two parallel drain rounds over the 3.15.1–3.18.1
window, then a set-audit of the drain itself, then a re-audit of the
repairs. Every fix below carries a regression test that was verified to
fail against the pre-fix source.

Minor rather than patch: the runtime now writes a new store-root file
(`.ingest-watermark.json`), `write_confirm` and `write_cancel` events
carry new fields, and `commit_drift` reports *not applicable* for a
cohort of memories that previously received an affirmative `clean`.
Every one of those is additive or a corrected verdict rather than a
renamed or removed surface, so the compatibility contract holds — but
software reading those surfaces will see a difference, and a patch bump
would understate that.

Two findings were security-relevant, and both were reproduced end to end
before being fixed. The audit's most useful result, though, was
structural: reading the drain window *across* its commits found eight
defects that verifying each commit *individually* could not, because
they lived between the fixes rather than inside any one of them.

### Security

- **`sync push` no longer publishes the write-reflex proposal queue.**
  `.write_proposals.jsonl` holds raw captured user text that never passed
  the write-path credential gate. It was absent from sync's gitignore
  denylist, so `git add -A` staged, committed, and pushed it: a
  secret-shaped capture reached every clone in plaintext, and git history
  is permanent. The accept gate refused that same body afterwards, citing
  cross-host sync — too late to matter. The queue is now gitignored, and
  `extract_proposals` additionally drops credential-bearing sentences at
  capture, logging the detector kind and never the value.

- **Three sibling leaks of the same class are closed, and the class is
  now guarded.** Orphaned `*.tmp` files from atomic writes carry the full
  payload of the file they were about to become — a memory body, or the
  proposals queue. The ingest watermark carries absolute host paths. The
  auto-consolidate clock is host-local debounce state. All three synced.
  A structural test now walks the package for store-root sidecar
  constants and fails if any is missing from the denylist, so the next
  one cannot leak silently.

- **`memory_update` records credential-gate overrides.** Passing
  `acknowledge_credential=True` on a body edit persisted the secret with
  no trace in the audit log, so a forensic sweep missed every secret
  introduced by editing. The update event now carries
  `credentials_acknowledged` (marker kinds only, never values).

### Fixed

- **Commit drift no longer reports a confident `clean` for citations it
  cannot see.** Sub-root (`handlers/search.py`), bare-filename,
  dotted-module, spaced and dash-leading citations resolved lexically to
  pathspecs no commit ever touched, and the resulting zero count was
  surfaced as an affirmative "the claims' ground truth has not moved" on
  `memory_show`, `memory_search`, and both `memory_health` rollups. Those
  now report not-applicable. The count itself is computed on author dates
  via a single `git log`, so a rebase can no longer inflate it past the
  truth or flip a clean memory to drifted.

- **`ui --tunnel` notices when the tunnel dies, and stays quiet when it
  doesn't.** The supervisor shim mirrors its provider's exit, so a share
  that never came up (tailscaled down, Funnel not enabled in the tailnet
  ACLs) or dropped mid-session is reported instead of silently serving
  loopback forever. A clean `Ctrl-C` or `systemctl stop` no longer prints
  a false "the shared URL is now DEAD" error — including when a slow
  request is still draining, which a first attempt at this fix got wrong.
  A `nohup`-detached tunnel survives terminal hangup: an inherited
  `SIG_IGN` is no longer clobbered at either install site. And a
  tunnel-mode bind failure exits non-zero again, so `systemd`'s
  `Restart=on-failure` still works.

- **`doctor`'s stranded auto-memory check tells the truth.** It mirrored
  Claude Code's directory sanitizer for only three characters, so any
  project path containing `_`, `!`, a space, or a parenthesis resolved to
  nothing while the check asserted no auto-memory directory existed. It
  also flagged every ingested-then-edited memory as un-ingested forever
  and advised re-running `ingest`, which resurrected the stale pre-edit
  body as a near-duplicate. Ingest now records provenance.

- **`eval --widening-preview`, `--detail`, and `--threshold-sweep`
  survive a poisoned event log.** One hand-edited row with a non-dict
  `top_hits` element took the whole run down.

- **`consolidate --llm` no longer silently drops a cluster's proposals.**
  A json-tagged schema echo could outrank the real bare-fenced payload and
  return zero proposals with no error, and an unhashable `category` value
  aborted the entire cluster instead of skipping the one malformed entry.

- **A record can be removed after `mark_verified` fills its frontmatter.**
  `tombstone` budgeted its removal metadata on the file-size axis only, so
  a record whose YAML sat near the 64 KiB cap became un-removable with a
  misleading diagnosis. All three lifecycle re-dump callers —
  `mark_verified`, `rename_scope`, and the `migrate` origin backfill — now
  reserve room on the YAML axis, and `migrate` surfaces a record it cannot
  safely grow instead of growing it into the un-removable band.

- **`episode_handoff` stops asserting things it cannot know.** A staged
  promotion that was cancelled or expired used to read as a committed one.
  The fix for that then told a genuinely-committed promotion in an older
  event log that it "journaled no takeaway". Both are gone: an unprovable
  promotion is reported as staged-but-unconfirmed. `docs/api.md` and the
  runtime tool description now document the `note` key the handler has
  been returning all along.

### Changed

- The runtime writes `.ingest-watermark.json` into the store root, mapping
  ingested source files to their content hashes. Host-local; never synced.
- `write_confirm` and `write_cancel` events carry the linked
  `episode_id`, so a deferred promotion can be proven rather than
  inferred. Event logs written before this release carry neither; a
  promotion recorded there is reported as unconfirmed rather than
  guessed at.

### Removed

- `origin.any_pathspec_in_history`, introduced and subsumed within this
  unreleased window — the author-date `git log` answers existence and
  recency in one call. Its tests were retargeted onto the function that
  replaced it rather than deleted.

### Internal

- `CONTRIBUTING.md` documents `mypy --platform win32`, which reproduces
  the Windows type-check leg locally, and no longer claims that a local
  green implies a green matrix.

## 3.18.1 - 2026-07-10

Live validation of the tunnel on a real tailnet — the follow-through
3.18.0 shipped without — caught a real leak: kill the server and the
tunnel child survived indefinitely, keeping the share URL alive,
proxying 502s, and pointed at whatever binds the port next. Two
mechanisms conspired. uvicorn's signal capture re-raises the caught
signal with default handlers restored *inside* `run()`, so a
`finally`-based teardown never runs on any signal exit (the old
mocked test certified exactly the path reality bypasses). And
`tailscale serve` does not exit on stdin EOF, so nothing reaped it
once its parent was gone.

### Fixed

- **Tunnel teardown now survives every exit path.** The provider
  runs under a small supervisor shim whose stdin is a pipe the
  server holds open and never writes: when the server dies —
  SIGTERM, SIGINT, SIGHUP, an exception, or SIGKILL, where no
  userspace teardown can run — the kernel closes the pipe and the
  shim reaps the provider before exiting. `serve()` additionally
  installs teardown handlers that compose with uvicorn's re-raise
  chain (reap, restore the default disposition, re-deliver), so
  orderly kills tear down synchronously and the process still dies
  BY its signal, preserving exit-code etiquette for supervisors.
- **The fake-Popen orchestration test is gone**, replaced by real
  process-lifecycle regression tests: a subprocess driver runs
  `serve(tunnel=...)` for real and the suite asserts the shim and
  the provider both die within seconds of SIGTERM and of SIGKILL.
  The stub provider deliberately ignores stdin — the worst case,
  and, per the live run, the real one.

## 3.18.0 - 2026-07-09

`bettermemory ui --tunnel` — the roadmap's one-shot sharing item.
The design splits responsibility deliberately: the tunnel CLI
(Tailscale or cloudflared) owns transport and prints its own URL, so
bettermemory does no output parsing; bettermemory owns POLICY — a
tunneled UI is always the read-only app, enforced at the app layer
rather than trusted to the transport, so pointing any other tunnel at
the port cannot re-expose the mutation.

### Added

- **`bettermemory ui --tunnel [auto|tailnet|funnel|cloudflare]`.**
  Bare `--tunnel` auto-picks `tailnet` (Tailscale `serve` — your own
  devices only) when the tailscale CLI is installed, falling back to
  a cloudflared quick tunnel. `funnel` (Tailscale Funnel) and
  `cloudflare` are PUBLIC: anyone with the URL can read the store,
  and the server says so loudly before the tunnel comes up. On macOS
  the resolver also probes the Tailscale app bundle
  (`/Applications/Tailscale.app/…/Tailscale`), which ships the CLI
  without putting it on PATH. `--tunnel` with a non-loopback
  `--host` is rejected — the tunnel is the front door.
- **Read-only mode in the web app** (`build_app(read_only=True)`).
  The verify endpoint answers a policy 403 before any CSRF/origin
  parsing, the verify form disappears from detail pages, the CSRF
  meta tag and helper script are not emitted at all (a tunneled page
  should not hand out a token that names a mutation surface), and
  the header shows a `read-only` badge so a viewer understands why
  the buttons are gone. The gate is mutation-tested: removing it
  flips the regression suite red.

## 3.17.1 - 2026-07-09

Root-anchor fix for claim-anchored commit drift, found by the
feature's first live curation pass: a memory whose body cites the repo
root ("the project lives at `~/…/bettermemory/`") had its anchor set
resolve to the pathspec `.`, which matches every commit. That memory
read 149 commits of "drift" — exactly the unfiltered count the 3.17.0
change exists to remove — while its discriminating anchors read zero.

### Fixed

- **`resolve_repo_pathspecs` drops inputs that resolve to the repo
  root itself.** A root citation is a location claim, not a content
  claim — its existence is path drift's axis; as a commit anchor it
  degenerates to match-all. Root-only anchor sets now resolve to the
  empty result, which the shared drift policy
  (`resolve_commit_drift_count`) already reads as not-applicable, and
  memories that also cite discriminating paths keep exactly those.
  The legacy composition (`commits_since_touching_paths`) keeps its
  documented contract: an all-dropped result stays "no useful filter,
  fall back to the unfiltered count".

## 3.17.0 - 2026-07-08

Claim-anchored commit drift. The commit-drift signal now counts only
commits that could actually invalidate a memory's claims, and goes
silent for memories that make no path-shaped claims at all. The change
was measured before it was made: two hand-labeled reads of the
dogfood store's drift queue — 12 flags at 3.13.0, 24 at 3.16.0 —
found **zero true positives**. Every flagged memory was a preference,
lesson, or reflection that merely *originated* in the repo; the bare
repo-wide commit count carried no information about any of them, and
a signal that is always wrong trains its consumer to ignore it. (The
residual was known and deliberately deferred in the 3.13.0
verified-paths pass pending exactly this evidence.)

### Changed

- **Commit drift is claim-anchored on all four surfaces** (`memory_show`,
  the `memory_search` per-hit fold and `expand_top` block, and both
  `memory_health` rollups — `commit_drift_debt` and
  `curation_pending.drifted`). Each memory's *anchors* are its
  `verified_paths` attestations plus the paths its body cites — the
  existing absolute/`~` extractor, now joined by repo-relative
  citations (`src/mod.py:42`, `docs/spec.md`, `CHANGELOG.md`), the
  dominant citation style path-drift could never check (nothing to
  stat without a root; the origin repo IS the root here). The drift
  count is narrowed to commits touching an anchor. A memory with no
  anchors — or none inside the caller's repo — reads `commit_drift:
  null` / omits `commit_drift_count` / leaves the health rollups: the
  signal is *not applicable*, not zero. The calendar staleness window
  is the deliberate backstop for that class, so nothing is exempt
  from re-verification forever. Two behavior flips ride along, both
  intentional: previously a memory with NO attested paths drifted on
  every repo commit forever (the gap-1 noise this release closes),
  and a memory whose attested paths all resolved outside the repo
  fell back to the unfiltered count (now: not applicable). Cited-path
  anchoring also *catches* drift the old filter missed — a memory
  whose body cites files it was never path-attested for now flags
  when those files change (the live store surfaced three such
  memories on the first read). Infrastructure failures still fall
  back to the unfiltered count — git being unreachable must never
  widen the exemption.
- **The drift recommendation names the anchor semantics** ("commits
  touching this memory's cited or attested paths landed…") so the
  re-verify prompt describes what was actually counted.

### Added

- `verify.commit_drift_anchor_paths` / `verify.resolve_commit_drift_count`
  — the shared anchor derivation and count policy behind all four
  surfaces (one decision function is what keeps them in lockstep), and
  `origin.repo_toplevel` / `origin.resolve_repo_pathspecs` /
  `origin.commits_touching_pathspecs` — the pathspec-resolution split
  that lets callers distinguish "git can't answer" (conservative
  unfiltered fallback) from "these claims don't anchor here" (not
  applicable). `origin.commits_since_touching_paths` survives unchanged
  as their composition. The relative-citation extractor is bounded and
  backtrack-proof (same ReDoS discipline as the 3.15.1 domain-route
  fix; adversarial 200 KB bodies scan in ~2 ms), rejects prose
  (`CI/CD`, `e.g.`), version strings, and URL tokens, and over-matches
  only phantom-safely (a phantom anchor resolves to a path no commit
  touched and contributes zero).

### Attestation guidance

For preference/lesson memories, attest freshness with
`verified_commits` + `note` rather than paths — a `verified_paths`
attestation anchors the memory to those files' commit history, which
for a churn-prone file (`CHANGELOG.md`) re-flags it on every release.
`memory_verify(id, verified_paths=[])` clears a prior path attestation
(REPLACE semantics), returning an untethered memory to the exempt
class. `docs/api.md` carries the same guidance.

## 3.16.0 - 2026-07-08

A measurement-driven calibration release. The centerpiece: the
relevance-v2 widening got its first live precision read — 103 replayable
audited turns, every flagged turn hand-labeled against its logged
evidence — and the verdict reshaped the roadmap. The bare matched-token
floor (`w1_top1_v2_high`) measured ~15–30% precision and is ruled out as
the flip target: its v1-low→high promotions (long pasted messages
crossing `matched_unique >= 4` against any domain-adjacent memory at
coverage ~0.2) were almost pure noise, while its v1-medium→high
promotions measured ~50% and contained every clearly-real catch. The
tooling that produced the readout ships in this release, so the next
calibration pass is one command, not an archaeology project.
Methodology and aggregates: `docs/eval/widening-labeling-2026-07-08.md`
(raw turns stay in the local event log — they contain user-message
previews).

### Added

- **`bettermemory eval --widening-preview --detail`** — the
  precision-labeling surface behind the widening counts. Dumps each
  flagged turn's logged evidence (the redacted `probe_query` preview,
  the top hit's raw coverage pair, both relevance labels, the hit's
  memory id joined against the active store + tombstone log for a
  summary) plus a per-memory concentration rollup, since concentration
  is the first diagnostic: N flags on two memories is a ranking
  problem, N flags across N memories is a wide label change. Reads
  only what the event log already holds — no new logging, no exposure
  widening. Both widening lanes now share one event-filter pipeline
  (`_collect_replayable_audits`), so the counts and the listed turns
  can never disagree.
- **`w2_top1_v2_high_from_medium` widening candidate** — v1's high arm
  plus the shadow floor's medium→high promotions only, i.e. exactly
  the original blind-spot thesis (long natural-language queries
  landing at *medium* on strong matches) without the junk-dominated
  low→high cohort. On the labeling window it flags 12 turns (Δ v1
  +11) vs w1's 32, at ~50% labeled precision. The flip gate is now:
  a follow-up labeling pass over a few more weeks holding w2 at
  ≥~70%.
- **`doctor` gained an `auto_memory_stranded` check.** Claude Code's
  filesystem auto-memory (`~/.claude/projects/<sanitized-cwd>/memory/`)
  accumulates facts bettermemory retrieval never sees; `bettermemory
  ingest` imports them, but nothing *surfaced* that stranded files
  exist. The check reuses `compute_ingest_plan`'s dedup classification
  rather than a bare file count, so a completed import goes quiet even
  though ingest deliberately never mutates the source files.
- **Deferred-harness loading hint in the server instructions.** Clients
  that gate MCP tool schemas behind a ToolSearch step (Claude Code
  with large tool surfaces) were nudged into loading bettermemory's
  tools one round-trip at a time; the instructions block now names the
  core four to load in one call. Paid for inside the existing 1700-char
  truncation budget by compressing the write-trigger list — the
  detail lives on `memory_write`'s own description, per the block's
  push-detail-down doctrine.

### Changed

- `docs/ROADMAP.md`'s relevance-v2 flip item restated empirically: the
  flip target is w2, w1-as-is is ruled out, and the gate is a
  precision number on a named artifact rather than "an acceptable
  widening delta."

## 3.15.1 - 2026-07-08

A post-release external review of 3.15.0 itself — five parallel
adversarial slice reviews, every finding reproduced against a live store
or client before it was filed — confirmed nine defects that release's
own set-audit had converged past, several *introduced by* its fixes. The
recurring shape: each miss sat just outside a fix's framing (a missed
mutator, an unwired tool boundary, a foreign lock protocol). This release
fixes all of them, moves the relevant guards to shared choke points so a
per-caller miss of the same class can't recur, and ships a mutation-sound
regression test with every fix (each verified to fail against 3.15.0).

### Fixed

- **`init --client claude-code` no longer wedges Claude Code's own config
  lock.** 3.15.0's RMW lock left a persistent regular file at
  `~/.claude.json.lock` — the exact path Claude Code locks with a
  mkdir-style directory lock — so the client's stale-lock cleanup failed
  (`ENOTDIR`) forever and its config saves broke until the file was
  deleted by hand; conversely, the client's live lock directory crashed
  init with `EISDIR`. bettermemory now locks a private
  `<config>.bettermemory.lock` sidecar, heals the poisoned 3.15.0
  artifact on the next `init` run (the result carries
  `removed_stale_lockfile`), and `doctor` gained a
  `stale_config_lockfiles` check that reports it with the fix. The
  pre-write guard also covers the create path now: a config file that
  APPEARS between init's read and write aborts loudly instead of being
  replaced by the skeleton doc.
- **The band-cap discipline now holds at every mutator, in both
  directions.** 3.15.0 closed grow-into-the-reserved-band for
  `mark_verified` / `rename_scope` but missed `migrate origin`, whose
  re-dump ran at the full read cap — an origin backfill (caller- and
  environment-controlled bytes) could still grow a just-under-cap record
  into the band and toward the hard-delete chain. And the band arm itself
  capped at the flat read cap, so a *legal* verify could eat the
  removal-metadata budget and leave a band record un-tombstoneable. One
  shared `_lifecycle_redump_cap` now caps every lifecycle re-dump: sub-cap
  records at the write cap, band records at the read cap minus the
  removal-metadata budget (their own tombstone always fits), and legacy
  records above that ceiling frozen at their current size.
- **Band tombstones are no longer a one-way door.** 3.15.0's restore
  refused any tombstone whose stripped record exceeded the write cap;
  with no tombstone-edit surface, a refused record just waited for
  `prune_tombstones` to hard-delete it. Restore re-admits at the read cap
  (band actives are maintainable and removable now, so nothing is lost),
  and `tombstone` adaptively trims its removal metadata — the session
  join is dropped first, then the reason is trimmed toward empty — so
  even a legacy record within the old fixed budgets of the read cap stays
  removable. `memory_remove` explains the shrink-first remediation for
  the truly unfittable sliver.
- **One malformed event can no longer take down retrieval.** The
  endorsement tally and the `recent_negative_outcomes` attach iterated
  raw event id fields, so a scalar or nested-list `ids` in the plaintext,
  hand-editable log failed every `memory_search` (the attach needs no
  feature flag) — the exact poison shapes 3.15.0 hardened `memory_health`
  against. The normalizer moved to `events.py` as the single shared choke
  point, with an index-preserving variant so `claim_excerpts` (recorded
  parallel to the raw list) still attributes to the right memory when
  malformed elements are dropped.
- **`episode_handoff` survives scope-disable.** A worktree session whose
  takeaways were all scope-hidden suppressed the fallback machinery, so
  the handoff collapsed to the first-ever-invocation shape
  (`prior_session_id: None`) — a regression below 3.14.1's honest note.
  The hidden session now resolves as the prior with a note naming the
  disabled-scope cause and the `memory_scope_enable` way back;
  rewind-through to an older *visible* takeaway is unchanged. The
  floor-only / zero-episode notes also stop mis-reporting a healthy
  write-then-promote session as "crashed or read-only": promotion is
  detected from the event log (including the deferred-confirm path) and
  named as the cause.
- **`acknowledge_credential` on proposal-accept actually works.** The
  3.15.0 escape hatch was dead at both shipped surfaces: the MCP
  wrapper's signature omitted the parameter (so the schema never carried
  it and FastMCP silently dropped it) and the CLI had no flag — while the
  refusal message told users to pass exactly that. The wrapper forwards
  it, the CLI grew `--acknowledge-credential`, and the forced-override
  audit event (detector kinds only, never the value) is recorded inside
  `accept_proposal` — one choke point, every surface, exactly once. The
  regression tests now pin the registered tool schema and the full
  MCP/CLI round-trips, not just the in-process core.
- **The verify scan cap can only drop path claims, never invent one.**
  The 32 KiB input bound sliced at a hard byte offset, so a legitimate
  citation straddling the boundary was bisected into a phantom prefix
  that validated as a path, failed the disk check, and fabricated a
  `path_drift_missing` — a false non-fresh staleness verdict from a body
  whose real path exists. The cut now lands on the last whitespace inside
  the cap.
- **`uv`/`uvx` entry recognition is positional and pin-aware.** The
  any-arg scan recognized `--with bettermemory` dependencies of FOREIGN
  servers as ours (init would delete and rewrite that entry), while the
  version-pinned shapes uvx documents (`bettermemory@latest`,
  `bettermemory==3.15.0`) and the Windows `uvx.exe` spelling matched
  nothing — resurrecting the doctor false-negative 3.15.0 claimed
  eliminated. The shared recognizer now walks the arg vector (skipping
  run-subcommands and flag values) to the first real positional, and
  doctor consumes it directly so the two can never drift again.
- **LLM fence extraction skips a leading non-JSON code fence** (a stray
  python scratch block ahead of the real payload), preferring
  json-tagged fences in document order; a genuinely unparseable response
  still raises the distinct cluster failure rather than counting as zero
  proposals.
- **Doc/comment honesty.** The 3.15.0 notes claimed the config migration
  preserves `disabled:` — it deliberately drops it (born-enabled; only a
  `disabled` set on the surviving new-name entry survives), and the entry
  below is corrected in place. The containment scorer's in-code claim
  that dropping the size-ratio gate "doesn't widen the firing set" was
  false: the advisory band IS wider for comparable-length pairs —
  deliberate, ceiling-bounded, and now pinned by a test that keeps the
  comment and the behavior telling the same story.

## 3.15.0 - 2026-07-08

A deep audit-drain of the 3.14.1 release itself. A set-audit of the
3.14.1 window surfaced 26 confirmed findings — many of them defects the
3.14.1 fixes had *introduced* (a green test suite is not evidence) — and
a multi-round adversarial drain fixed all of them, then set-audited its
own output twice more until the finding count converged to zero. Every
fix ships with a mutation-sound regression test (it fails without the
fix), and the highest-consequence ones were reproduced dynamically
against a running store, not just reasoned about. Behaviour visible to a
correctly-sized memory and a well-formed request is unchanged; the new
`acknowledge_credential` parameter on the proposal-accept path and the
richer `episode_handoff` output (chain rewind + an honest soft note) are
additive, which is why this is a minor rather than a patch.

### Fixed

- **Silent data loss: a verify or scope-rename could freeze a memory.**
  `mark_verified` and `rename_scope` re-dumped at the full read cap, so
  caller-controlled growth (`verified_paths`, a longer scope) on a memory
  admitted just under the write cap pushed it into the reserved
  maintenance band — after which `update` rejected it, `tombstone` could
  cross the read cap (un-removable), and `restore` refused re-admission,
  so pruning eventually hard-deleted it. Both paths now cap by which band
  the record is already in: a sub-cap record cannot grow into the band; a
  record already in the band (e.g. a pre-3.14.1 file) stays maintainable
  up to the read cap, including a first verify.
- **The removal-metadata caps are budgeted on serialized size.**
  `removed_reason` and `removed_session` are bounded so a tombstone's
  YAML-escaped growth provably fits the maintenance headroom.
- **`restore` and `rename_scope` no longer race a concurrent remove.**
  `restore` now writes the active file, unlinks the tombstone, and
  upserts the index all under the active-path lock; `rename_scope` guards
  each record's re-dump and reports a partial run instead of aborting
  mid-loop with a diverged index.
- **bm25 search recovers X-to-X compound queries** (`end-to-end`,
  `back-to-back`, `to-do`, …) that previously returned nothing, and the
  content-dedup containment check no longer over-flags comparable-length
  distinct writes while still catching a short fact buried in a long one.
- **Semantic dedup falls back correctly when the embeddings extra is
  absent** (cosine thresholds are no longer applied to Jaccard scores, so
  dedup actually fires).
- **Endorsement telemetry reads the right window.** `_explicit_applied_counts`
  now enforces its own attribution horizon so no caller can over-count
  against the wider re-audit window; the health rollup tolerates every
  malformed-event shape instead of one bad event blanking
  `memory_health` / `memory_scope_overview` / `doctor`.
- **`episode_handoff` no longer severs the journal chain.** A clean
  read-only tick (floor-only *or* zero-episode) is rewound past to the
  most recent real takeaway, with an honest note that no longer claims a
  crash — and, for a zero-episode session, no longer claims a handoff
  floor was written.
- **Credential and scope guardrails now cover the LLM-consolidate and
  proposal-accept write paths**, which previously bypassed the
  credential scan, origin capture, and scope allowlist that
  `memory_write` enforces; both now expose the same
  `acknowledge_credential` escape hatch and log a forced override (kind
  only) to the event stream.
- **Path-drift scanning is bounded on untrusted input** (a poisoned
  memory body can no longer drive super-linear work per search hit), and
  `migrate origin` routes dict/set-shaped `scopes` the same way the store
  reader does instead of silently stamping the wrong repo.
- **LLM JSON extraction is robust to fenced responses.** A provider that
  wraps its answer in a ```` ```json ```` fence (the expected Anthropic
  shape) — even one whose body contains a nested fence — is parsed
  instead of silently truncated, and a genuinely unparseable response is
  recorded as a cluster failure rather than counted as zero proposals.
- **Client-config migration preserves user keys and stays schema-clean.**
  The legacy → `bettermemory` rename keeps `env.BETTERMEMORY_DIR`, `cwd`,
  and `timeout` (a legacy `disabled:` is deliberately dropped — the
  migrated entry is born enabled, and only a `disabled` set on the
  surviving new-name entry survives; this sentence originally claimed
  `disabled` was kept — corrected in 3.15.1), drops remote-transport keys
  that would make a strict client reject the stdio entry wholesale, migrates via a
  recognizer that matches the bare-name and `uvx` shapes (so `doctor`
  stops reporting a healthy `uvx` install as missing), and captures its
  change-signature before the read to close a concurrent-writer window.

## 3.14.1 - 2026-07-06

An audit-backlog drain: the 2026-07-06 whole-repo review surfaced 15
confirmed findings (2 data-loss, 6 correctness, 7 low/observation),
each adversarially verified with a runnable repro before it was
filed. This release fixes all of them. Every fix ships with a
regression test that fails without it; no behavior visible to a
correctly-sized memory changes.

### Fixed

- **Silent data loss: a legally-sized write could be unreadable.**
  `_frontmatter.dumps` capped only the frontmatter region against
  `_MAX_YAML_BYTES`, but `load` rejects the whole file against the
  larger `_MAX_FILE_BYTES`. A body near the `max_content_bytes`
  handler cap plus a dense-but-legal frontmatter (many
  `verified_paths`) could serialize past the read cap, whereupon the
  file was stat-rejected on read and the malformed-file skip dropped
  the record from every surface — while the write reported committed.
  `dumps` now enforces a total-file guard at the one serialization
  chokepoint, turning silent permanent loss into a clean `ValueError`.
  Content-admitting writes reserve a small headroom below the read cap
  (`_MAX_WRITE_BYTES`) so any accepted record can always be tombstoned
  or renamed and stay readable; the lifecycle re-dump paths pass the
  full read cap for that bounded metadata growth.
- **Migration dropped a relocated store.** The legacy `memory` →
  `bettermemory` rename in `init.patch_client_config` seeded the new
  entry from the *new* name (absent on the rename path), so
  `env.BETTERMEMORY_DIR` (and any `disabled` / `timeout` / transport
  keys) on the legacy entry were discarded — a user who had relocated
  their store then booted against the default dir and saw it empty.
  The rename now carries the legacy entry's keys forward, overwriting
  only the canonical `type` / `command` / `args`.
- **ReDoS on the search hot path.** `_DOMAIN_ROUTE_RE` ran unbounded
  (`(?:\.[\w-]+)+`) via `finditer` over the entire body inside
  `detect_path_drift`, which fires per search hit; a poisoned body
  (`a.a.a…`) backtracked catastrophically (seconds-to-minutes) — the
  path cap only applies *after* the scan. The repetition is now
  bounded to `{1,20}`, far past any real FQDN.
- **`migrate origin --scope-repo` crashed on a scalar `scopes`.** The
  scope-routing `scope in memory_scopes` test sat outside the per-file
  guard, so one malformed file with a scalar `scopes` raised
  `TypeError` and aborted the entire migration. `scopes` is now
  coerced to a list before the membership test.
- **Same-day path collision could clobber a memory.** `_path_for`
  (and `restore`) used only the last 6 ULID chars (30 bits) for the
  filename suffix; non-ASCII bodies all slugify to the bare `memory`
  fallback, so two same-day writes could birthday-collide and silently
  overwrite one. Both now use the full ULID; the stale "two writers
  can never collide" docstring is corrected.
- **`restore` upserted the search index under the wrong lock.** The
  FTS upsert ran under the tombstone-path lock rather than the
  active-path lock every other mutator holds (audit invariant H1),
  so a concurrent `update` / `verify` could diverge index from disk.
  The upsert now runs under the active-path lock.
- **Re-audit dedup missed events across a log rotation.**
  `memory_audit_turn` read only the narrow probe window (≤600s) but
  deduped over `REAUDIT_DEDUP_WINDOW_SECONDS` (3600s), so a prior
  `turn_audited` beyond the probe window slipped through and a
  duplicate `search_miss` inflated the miss numerator. The read now
  covers the full dedup window; each consumer still applies its own
  narrower cutoff (matching the Stop hook).
- **A scalar id-field could blank `memory_health`.** Six sites
  iterating event id-fields (`for mid in ev.get(...)`) would raise
  `TypeError` on a well-formed dict event carrying a scalar where a
  list was expected, taking down `memory_health` / `scope_overview` /
  `doctor` wholesale. All sites now normalize to a list.
- **bm25 mode returned nothing for hyphenated stopword queries.**
  `end-to-end`, `up-to-date`, `time-to-live` and kin failed because
  the conjunctive kebab fallback counted components off the
  stopword-stripped stream, so a stopword component scored zero and
  collapsed the `min()`. Component counts now come off the unstripped
  stream, preserving the `python-frontmatter` precision guard.
- **Dedup missed a short restatement contained in a long memory.**
  `_pairwise_content_jaccard` is symmetric, so a near-verbatim
  restatement of one sentence of a long memory scored far below the
  `related` floor and committed. A containment score
  (`|∩| / min(|A|,|B|)`), gated on large size asymmetry, now flags it —
  bounded by an absolute floor on the smaller token set and capped into
  the `related` band so it surfaces a near-duplicate without ever
  silently blocking a legitimately distinct short write.
- **LLM-distilled memories were mis-attributed.** `consolidate --llm
  --from-transcript` wrote proposals with `source=explicit-statement`
  instead of `inferred`, defeating the provenance distinction the
  proposal-accept path already keeps. Now `Source.INFERRED`.
- **A clean read-only tick was mislabeled a crash.** An entry-floor is
  written unconditionally at handoff entry, so a legitimate
  handoff-without-takeaway read as "prior session crashed before
  writing a takeaway". The determination now distinguishes the
  read-only-tick case from a genuine crash.
- **Unbounded LLM provider retries widened the timeout bound.** The
  Anthropic and OpenAI clients kept the SDK default `max_retries=2`,
  and since `APITimeoutError` is retryable the `timeout=` bound could
  stack to ~3× against a hung provider. Both clients now set
  `max_retries=0`, removing that stacking. (The remaining `timeout=`
  is a bare float httpx applies per phase — connect/read/write each get
  it — so it bounds each phase rather than total wall time, not a single
  wall-clock deadline as first described.)
- **mypy silently type-checked nothing when numpy was installed.**
  With the `[embeddings]` extra present, mypy aborted parsing
  numpy 2.5's `.pyi` stubs (a `type` statement needs 3.12) under
  `python_version = "3.11"`, emitting "errors prevented further
  checking" and checking zero files; CI dodged it by installing only
  `dev`+`ui`. A `[[tool.mypy.overrides]]` block now sets
  `follow_imports = "skip"` **and** `follow_imports_for_stubs = true`
  for numpy — the stub setting is load-bearing, since `follow_imports`
  is otherwise ignored for `.pyi` files and mypy would still open
  numpy's stubs. `python_version` stays pinned to the project minimum
  (3.11) — bumping it would silently admit 3.12-only syntax the shipped
  package can't run.

### Documented

- Path-drift on a bare absolute path that legitimately lives on a
  remote host (e.g. `/opt/gophish` on a homelab board) is stat'd
  against the local filesystem and reads as `missing` until attested
  via `memory_verify(verified_absent_paths=[...])`. This is the
  intended escape hatch — there is no local-only heuristic that
  separates a legitimately-remote path from a deleted local one
  without suppressing real drift — and is now called out in
  `detect_path_drift`.

## 3.14.0 - 2026-07-03

The measurement release: three layers of the effectiveness telemetry
were structurally miscalibrated — the relevance label compressed
on long queries, the helped-rate under-measured because the in-process
auto-commit raced the Stop hook's attribution pass, and the silent-miss
counters double-counted multi-stop turns. Every change here is
measurement-precision work surfaced by the 2026-07-03 model dogfood
sessions (Sonnet 5 and Fable 5 reflections) and the live event log.

### Added

- **Shadow relevance label v2 + raw coverage features in the log.**
  `search._relevance_label_v2` keeps the v1 coverage mapping and adds
  an absolute matched-token floor to the "high" arm (`matched_unique
  >= 4`), targeting the documented blind spot where long
  natural-language queries land at "medium" on strong matches. SHADOW
  ONLY: the label rides on `search` / `turn_audited` / `search_miss`
  events, never in an MCP response. `search` events additionally log
  per-hit `scores` / `match_counts` and the per-search `query_unique`
  denominator, making the historical record formula-agnostic — any
  future labeling rule can be replayed from the log alone.
- **Per-turn calibration payload on `turn_audited`.** Every audited
  turn now carries `probe_query` (redacted `{hash, preview, len}`
  unless `log_queries_verbatim`) and a compact `top_hits` list
  (id/score/relevance/relevance_v2/matched_unique/query_unique — no
  snippets). This closes the "threshold-sweep can narrow but never
  widen" constraint at the data layer.
- **`bettermemory eval --widening-preview`.** Replays candidate
  LOOSER threshold rules (registry: `eval.WIDENING_RULES`, bundled
  candidate `w1_top1_v2_high`) over the turn_audited stream against a
  replayed v1 baseline — the forward-looking counterpart to
  `--threshold-sweep`.
- **Per-model telemetry.** The Stop hook reads the model id off the
  transcript's newest assistant row and stamps `client_model` on the
  `turn_audited` / `search_miss` / `use` events it emits; `bettermemory
  eval` reports a per-model breakdown (`by_model`). The MCP channel
  carries no model identity, so the hook is the only possible source.
- **`pending_writes` on `memory_scope_overview`.** Counts this
  session's staged writes still awaiting confirm/cancel, so a dangling
  user-inference confirmation is visible before its silent 1-hour
  expiry (the live log showed staged writes expiring unresolved).

### Changed

- **End-of-turn use settlement (auto-commit race fix).** The
  in-process auto-commit's clock is handler entries, so a tool-heavy
  turn auto-committed its own retrievals mid-turn — before the reply
  existed — starving the Stop hook's attribution matcher of every id
  it would have matched (~98% of applied events on the dogfood store
  were bare autos). `session.consume_old_tokens` now requires BOTH
  axes: ≥2 handler entries AND ≥`AUTO_COMMIT_MIN_AGE_SECONDS` (600s,
  mirroring the hook's attribution window; cross-pinned in tests). The
  Stop hook settles each turn at turn end instead: reply-matched
  retrievals record `attribution="hook"` with excerpts, and the
  unmatched remainder records the plain `auto=true` fallback in the
  same pass. Hookless deployments keep the in-process fallback (first
  handler call past the floor, still inside the 30-minute eviction
  window); a session's final-turn retrievals with no hook remain
  unsettled, as before.
- **Re-audit dedup.** A long autonomous turn stops many times without
  a new user message, and each stop re-probed and re-flagged the same
  message (7 identical `search_miss` events from one ship-go message
  on the 2026-07-03 log). Repeats inside an hour now record
  `turn_audited` with `repeat: true` — cadence stays observable — and
  never emit a second `search_miss`; eval and health exclude repeats
  from every denominator.

## 3.13.1 - 2026-07-03

### Added

- **The comparative eval's live competitor lane.** `python -m
  tests.eval.comparative --live` swaps the honest stubs for executing
  adapters where execution is honest: mem0 runs fully local and
  keyless (HuggingFace MiniLM embedder, embedded qdrant in a tempdir,
  `infer=False` so its LLM extraction pipeline is never invoked —
  measured claim: its retrieval stack over verbatim facts), and the
  Anthropic reference `@modelcontextprotocol/server-memory` is bridged
  over stdio with the `mcp` client the project already ships (its
  native `search_nodes` is whole-query substring matching, so the
  harness donates a tokenized-OR ranker — documented, and a test pins
  that no gold probe matches verbatim, i.e. the raw server scores 0/7
  by construction). agentmemory joins the matrix as a fifth,
  documented-unavailable row (the PyPI package died in Oct 2023; the
  trending 2026 namesake is an unrelated TypeScript service), and the
  claude-mem row's prose is refreshed against its 2026 plugin
  architecture. Competitor stacks never touch the dev venv, uv.lock,
  or CI — `tests/eval/run_live.sh` builds a throwaway `.eval-venv/`,
  and the live integration tests self-skip without `BM_EVAL_LIVE=1`.
  Ran-rows now carry `system_version`; the first committed artifact
  lives at `docs/eval/comparative-live-2026-07-03.json`.
- **`docs/eval-results.md` — the published numbers.** Production
  telemetry trio (two months of the author's real usage, n=1 stated)
  plus the comparative capability matrix and live recall rows, with
  every fairness accommodation spelled out and exact reproduce
  commands. Linked from the README's "numbers, not vibes" bullet;
  the corresponding ROADMAP item retires.

### Fixed

- **The four GC-timed sqlite `ResourceWarning`s under `-W default` are
  gone — all were test-side.** Four tests tweaked the index with
  `with sqlite3.connect(...)`, which scopes the *transaction*, not the
  handle; the leaked connections surfaced as ResourceWarnings
  attributed to whatever test was running when GC fired (and kept an
  open handle that Windows CI's unlink is sensitive to). Production
  code audited clean — every `src/` connect site closes
  deterministically. The tests now use
  `contextlib.closing(...) as conn, conn:` so the transaction still
  commits before the handle closes.
- **`doctor` no longer warns forever about deliberate multi-install
  topologies, and the stale-path warn names the client.** A client
  config pointing at a different install than PATH resolves (dev venv
  vs `uv tool`) warned on every run even when both binaries ran the
  same release; the check now probes both with `--version` (only in
  the mismatch branch, memoized) and downgrades the same-version case
  to ok with a "different install, same version: <client>" note.
  Genuinely stale paths still warn — and the message now carries the
  client name, config path, and both versions inline (previously only
  in `--json` details), so the fix_hint names the actual client
  (`bettermemory init --client claude-code`) instead of the literal
  placeholder "client X". Also corrects a latent test whose
  `/nonexistent/old/bm` fixture missed the "bettermemory" entry filter
  and asserted against the no-references branch instead of the
  stale-path branch it claimed to cover.

## 3.13.0 - 2026-07-03

### Changed

- **FTS index schema v5: one-time reindex (automatic), plus a
  tokenizer-fingerprint ratchet so persisted-stream drift can't recur
  silently.** Schema v4 persists `tokenize()` output on disk
  (`body_fts`/`scopes_fts`), so query/index parity requires the
  persisted stream to match the live tokenizer — and four post-3.12.0
  tokenizer fixes (stopword curation, final-y normalisation, CJK
  index-side unigrams, the NFKC fold) respelled the stream with no
  schema bump, leaving every 3.12.0-built index stale-spelled: a
  live query 'todo'/'cooki' could not match the indexed
  'todos'/'cooky', so prefiltered search on large stores silently
  dropped exactly the memories the rankers rate high. The v5 bump
  routes existing indexes through the schema heal path — machinery
  that is itself new this cycle (3.12.0's v4 migration dropped the
  tables and waited for incremental writes to repopulate): the wipe,
  version stamp, and rebuild-pending flag commit in one atomic
  flock-revalidated transaction, the flag keeps search on full scans
  (slower, never lossy), and the next Store construction
  auto-rebuilds — no manual `bettermemory reindex` needed. The
  ratchet: index meta now records a tokenizer fingerprint (sha256 of
  `fts_index_text` over a fixed multilingual probe corpus) next to
  `schema_version`, stamped in the same atomic migration
  transaction; on open, a fingerprint mismatch at the current
  version migrates exactly like an older version, and a pinned
  regression test asserts the recorded constant matches the
  live pipeline, so any future stream change fails CI until the
  constant is deliberately re-pinned. Existing indexes heal either
  way — the runtime compares the live fingerprint, not the pin — so
  the `SCHEMA_VERSION` bump the test's failure message prescribes
  keeps version semantics honest rather than gating the heal.

### Fixed

- **Sixteen developer-vocabulary collisions un-stopworded from the
  multilingual lists.** todo/todos, su, dom, hat, uber, die, ist,
  para, fur, hay, si, finns, sans, ses — and 'sin', which 3.12.0
  promised out but leaked back via the Swedish possessive — were
  absorbed as function words, and stopword-only queries
  short-circuited to zero hits before any ranker. `search()` also
  falls back to the unstripped tokens when stripping empties a query,
  so no future stopword addition can make a query unanswerable.
- **Plural-stemmer symmetry restored: -ie/-y nouns share one key,
  s-final singulars pinned.** 'cookies' stemmed to 'cooky' but
  'cookie' to 'cooki' — two index keys, zero matches for every
  movie/rookie/hoodie-shaped lexeme; a final-y normalisation
  mirroring the final-e rule puts both inflections on one key
  ('policy' meets 'policies'). s-final singulars outside the
  ss/us/is guard (alias, atlas, bias, canvas, lens) are pinned whole
  so they meet their own -es plurals.
- **Single-character CJK queries match inside runs.** '猫' scored
  zero against a body plainly containing 猫 — bigram segmentation
  left no single-char token to equal. The index side now emits each
  bigram's characters as unigrams (queries keep bigram phrase
  semantics), so one-character queries hit in every ranker and the
  FTS index.
- **NFKC compatibility fold in the tokenizer.** Fullwidth Latin and
  digits ('ＧＰＵ', '２０２６' — standard Japanese IME output) never
  met 'gpu'/'2026', halfwidth katakana never met fullwidth, and the
  halfwidth voiced-sound marks stranded as junk tokens. NFKC ahead of
  the case/diacritic folds fixes all three, symmetrically on query
  and index side.
- **The audit's acknowledgment gate compares surface spellings again,
  restoring its 3.11 width.** 3.12.0 canonicalised `_ACK_SURFACE`
  through the now-stemming `tokenize`, which put generic stems in
  `_ACK_TOKENS` ('works' → 'work', 'sounds' → 'sound', 'done' → 'don',
  'nice' → 'nic', 'fine' → 'fin') — so ordinary content queries whose
  tokens all landed on those stems ("does the sound work", 'Don' the
  name, 'NIC' the card) were classified `no_signal` and never probed
  for a retrieval miss, silently under-detecting the telemetry the
  eval/health pipeline reads. The set is now built from the UNSTEMMED
  tokenization and the gate compares the message's unstemmed tokens
  against it — the curation is a judgment about spellings, so the
  membership check happens in the space where it was made. Real
  acknowledgments ("sounds good", "thanks, done!") gate exactly as
  before; the two-content-token floor stays in stemmed space, matching
  the ranker's coverage denominator.
- **Link surfaces degrade to scans instead of silently dropping
  annotations while the index is unhealthy.** Search-hit link
  annotations and `memory_show` reverse-links honour the
  rebuild-pending flag, and a corrupt or version-newer index takes
  the same candidate-scan fallback for search-hit annotations —
  previously those states returned hits with their links quietly
  missing.
- **Poisoned index meta is corruption, not a crash.** A readable
  index whose meta rows were hand-edited or written by a foreign tool
  (non-integer `schema_version` / `indexed_count`) crashed
  `index.status()` with ValueError — and OSError from a concurrent
  unlink did the same — taking doctor and reindex diagnostics down on
  exactly the corruption they exist to report. `status()` now returns
  its degraded corrupt shape in both cases, `rebuild()` recovers from
  the poisoned meta instead of raising, and `memory_show`'s link
  guard treats it as corruption too.
- **`bettermemory doctor` checks FTS index health, and the
  index-divergence warning is parse-aware.** A new `index_health`
  check surfaces a corrupt, missing, rebuild-pending, or
  out-of-sync-with-disk index (one repair: `bettermemory reindex`).
  Doctor and the startup S4 warning both subtract unparseable files a
  rebuild can never index — previously they prescribed a reindex that
  could never clear the gap — and parse-health counts files with the
  store's own enumeration (README.md, dot-prefixed .md) so the two
  checks cannot disagree.

- **Acronym plurals meet their singulars.** 'APIs' tokenized whole
  while 'API' stemmed to 'api' — cross-inflection queries missed
  entirely for the most common tech plurals. A small irregular-plural
  map (apis, clis, cpus, gpus, guis, skus, uris) folds them ahead of
  the suffix rules; status/basis/analysis/redis stay guarded. This
  respells the persisted stream: the tokenizer fingerprint is
  re-pinned and existing indexes heal automatically.
- **The stopword-only query fallback works in bm25 mode too.** The
  fallback ranked unstripped tokens in keyword and hybrid mode, but
  bm25 counted term frequency on the stripped stream — so
  `mode="bm25"` still returned zero hits for queries like 'des'.
  Fallen-back tokens now count against the unstripped body stream.
- **A fresh index inside a populated store rebuilds instead of
  trusting itself.** Deleting `.index.sqlite` (the historical recovery
  advice) created an empty index stamped current; once enough
  post-creation writes accumulated, the prefilter re-engaged and
  untouched legacy memories vanished from search — the migration
  recall hole, alive on the first-touch path. First touch on a
  populated store now stamps rebuild-pending, and the next Store
  construction auto-rebuilds.
- **Page-level index corruption degrades instead of crashing.**
  `status()` reads only meta pages, so a torn data/FTS b-tree page
  passed the health gate — and then `memory_search` crashed at the
  tool boundary, doctor certified the index healthy, and reindex
  crashed mid-rebuild: the exact corruption class the repair path
  exists for. Search now routes to the full scan, doctor runs a real
  integrity probe (`PRAGMA quick_check`), and `rebuild()`'s data phase
  recovers by recreating the file and retrying once.
- **Annotation and show guards complete their truth tables.**
  Search-hit link annotations treat an absent or never-populated index
  like rebuild-pending (same candidate-scan fallback), and
  `memory_show`'s link guard tolerates OSError from the migration
  lock alongside the corruption cases.
- **One adversarial file can't take down a read surface.** A memory,
  tombstone, or episode whose frontmatter parses but carries the wrong
  shape (`scopes: 5`) crashed Store construction, `memory_search`'s
  scan, tombstone listing and pruning, or episode handoff — whichever
  touched it first. Every per-file parse catch now shares one
  skip-set: malformed files are counted and skipped, never fatal.
- **Failed auto-rebuilds back off.** A deterministically failing index
  rebuild (read-only dir, disk full) re-ran a full-store
  re-tokenization on every CLI invocation and server boot. Failures
  record an in-process memo plus a best-effort cross-process marker;
  retries wait out an hour-long window, `bettermemory reindex` always
  bypasses, and a successful rebuild clears both.

### Performance

- **Search tokenizes each candidate once instead of six times.** One
  token stream per candidate now threads through the keyword scorer,
  IDF, BM25, and the semantic literal-match block, and pure-ASCII
  input skips the Unicode folds entirely; hybrid search over a
  499-memory store drops 91.5 ms → 26.8 ms with byte-identical hits,
  ordering, and relevance labels (pinned by golden streams).
- **Diacritic folding via a precomputed translate table.** Common
  Latin (U+0000–U+017F) folds through a dense `str.translate` table
  built at import instead of a per-character NFD genexp; anything
  beyond the range routes to the unchanged NFD path. Non-ASCII Latin
  bodies tokenize at 1.17x ASCII cost, down from 1.46x.

## 3.12.0 - 2026-07-02

The tokenizer v2 release — the "Tokenization v2" feature-class
residual parked in the 2026-06-09 extractor hunt
(`docs/audit/extractor-hunt-2026-06-09.md`), shipped as one coherent
change to the shared pipeline instead of six heuristic patches. The
headline: CJK-language memories stop being write-only (they were
written successfully, passed dedup, and were then unfindable in every
ranker AND the FTS5 index), plural/singular query inflection stops
being a total miss, and the groundedness gate's stopword defence
stops being English-only. One consequence worth knowing: FTS index
schema v3 → v4 forces a one-time rebuild (`bettermemory reindex`, or
let the write hooks repopulate; search falls back to full scans
meanwhile). Tests 2,546 → 2,565 under the full local extras matrix.

### Added

- **CJK bigram segmentation in the shared tokenizer.** `\w`-based
  tokenization treated an unspaced CJK clause as ONE giant token —
  `東京オフィスは移転する` was a single 12-char "word" only a
  byte-exact query could match. `tokenize` now emits overlapping
  character bigrams for Han / Kana / Hangul / Thai runs (the standard
  dictionary-free segmentation, per Lucene's CJKAnalyzer), applied
  symmetrically to query and indexed text. Because search, write-time
  dedup, groundedness, and the audit probe all consume the one
  pipeline, three parked findings close at once: CJK bodies are
  searchable (every lexical ranker + FTS5), Jaccard dedup sees CJK
  near-duplicates (previously similarity 0.0 for any rephrase), and
  the groundedness gate actually evaluates CJK sentences instead of
  skipping them under `MIN_CONTENT_TOKENS`. Hangul is bigrammed in
  its NFD Jamo form (the diacritic fold decomposes syllables first)
  — consistent on both sides, so matching works.
- **Light plural stemmer.** Matching was exact token equality, so
  'standups' vs a body saying 'standup' returned nothing anywhere.
  `tokenize` now folds plural inflection — 'sses'/'ies'/final-'s'
  rules plus a final-e normalisation that collapses the '-es
  attachment' ambiguity ('branches'/'branch' and 'caches'/'cache'
  both fold correctly, which no dictionary-free rule can split
  without normalising the singular too). Deliberately NOT Porter:
  relevance buckets and dedup Jaccard feed automation, so
  derivational conflation is the worse failure mode. Guards:
  stopwords exempt by surface form ('does' can't leak 'doe' into
  content-token counts), rule results landing on a stopword revert
  ('ones' folds to 'one', never to 'on'), 'ss'/'us'/'is' endings,
  digit-final acronyms ('k8s'), and 3-letter tokens ('aws', 'dns',
  'yes') stay whole. Compounds stem per segment so `_expand_kebab` /
  `_kebab_parts` stay coherent ('docker-containers' →
  'docker-container').
- **Multilingual stopword lists (sv/de/fr/es).** The stopword defence
  — the only thing keeping filler from anchoring groundedness ratios
  and inflating relevance-coverage denominators — was English-only,
  so a hallucinated Swedish claim grounded on {vill, att, på} against
  any Swedish transcript. Four curated function-word sets join
  `_STOPWORDS`, spelled in post-diacritic-fold form ('på' → 'pa').
  Collision-curated: 'vi'/'du'/'man' (unix), 'mit' (the license),
  'war' (.war artifacts), 'sin'/'con'/'y'/'o' (math, pros-and-cons)
  and friends stay searchable; accepted borderline cases ('ar', 'es',
  'est', 'en', 'de') are documented at the definition. English gains
  'am' and the third-person pronouns (he/she/him/his/her/hers).

### Changed

- **FTS index schema v4: prefilter/ranker parity by construction.**
  The FTS5 table stops indexing the raw body under unicode61 and
  indexes new `body_fts` / `scopes_fts` columns holding
  `search.fts_index_text` output — the exact token stream the Python
  rankers score. Every past parity bug (diacritic folds, symbol
  aliases, contraction strips) came from the index having its own
  spelling authority that `fts_match_query` had to hand-mirror; now
  both sides of a MATCH speak `tokenize` tokens, and the raw-symbol
  OR-variant ('cpp' also trying '"c++"') is gone because the indexed
  text already says 'cpp'. Raw `body`/`scopes_text` stay on the
  content table (LIKE scope filter, debuggability). On-disk v3
  indexes are dropped and recreated empty on first touch — run
  `bettermemory reindex` to repopulate eagerly.
- **`match_terms` now carries the tokenizer's normal forms.** A query
  'standups' that hits reports 'standup'; 'caches' reports 'cach'.
  The stems are index keys, not words — the field still means "which
  query tokens actually hit", the spellings are just canonical now.
- **Groundedness: full-width sentence boundaries and a surface-form
  alias rescue.** The splitter treats 。！？； as terminators without
  requiring trailing whitespace (CJK prose has none), so a
  hallucinated second sentence can't hide behind a grounded first.
  The zero-anchor alias rescue ("Prefers VSCode." vs a transcript
  saying "VS Code") now compares UNSTEMMED spellings — spelling
  relations are surface properties, and the stem 'cod' fell under
  the rescue's particle length gate.
- **`audit._ACK_TOKENS` canonicalises through the tokenizer at
  import.** The acknowledgment gate compares against `tokenize`
  output; a hand-maintained literal set silently detached when the
  stemmer landed ('done' → 'don'). Mapping the surface list through
  `tokenize` at import means the two can't drift again.
- **Docs rewritten for brevity.** The README had grown into a pitch
  deck — competitor tables, narrative walkthroughs, feature essays
  restating each other. It, `docs/ROADMAP.md`, `docs/eval.md`,
  `docs/installation.md`, and `plugin/README.md` are rewritten in
  conventional OSS register: what it is, install, features as
  one-liners, docs links. No behavioral claims changed; `docs/api.md`
  (the pinned tool contract) and the drift-tested
  `docs/system_prompt.md` block are untouched. A follow-up pass in
  this release re-aims the README at the person deciding whether to
  install rather than the person reading the code: an illustrative
  two-session transcript up top (remember across the gap, distrust
  the drifted fact), benefit-led "why this one" bullets, and the
  mechanics compressed below an explicit fold. The drift-tested
  claims (semantic opt-in knobs) are preserved verbatim.

## 3.11.0 - 2026-07-02

The eval-consistency release, opened by a removal: the key-gated
`LiveAgent` role-play path is gone (it demanded a raw API key the
project's own agent workflow never holds, and a staged one-shot
completion is not a measurement), and the removal's diff-audit chain
then surfaced a real divergence family between the two silent-miss
reporting surfaces — `bettermemory eval` and `memory_health` now apply
identical invalidation semantics (bulk cutoffs, per-miss acks,
tombstoned top-hits). Tests 2,533 → 2,552; no breaking changes
(`compute_eval`'s new parameter is optional and default-preserving).

### Fixed

- **`bettermemory eval` now honors the silent-miss invalidation
  markers.** `compute_eval` counted every in-window `turn_audited` /
  `search_miss` event, while the `memory_health` /
  `memory_scope_overview` rollups drop telemetry invalidated by a
  `silent_miss_cutoff` event (`consolidate
  --acknowledge-misses-before`) or a per-event `miss_ack`
  (`memory_acknowledge_miss`) — so after either escape hatch ran, the
  eval CLI's `silent_miss_rate` silently disagreed with the health
  surfaces over the same event stream, and the eval CLI is exactly the
  surface docs/eval.md tells people to compute the publishable trio
  from. `compute_eval` now applies the same invalidation semantics
  (latest `cutoff_ts` wins; both markers resolve globally even when
  their own ts falls outside `--since`): pre-cutoff events drop from
  the numerator, the miss-capable denominator, `turns_no_signal`, and
  the inline triage list alike; an acked miss drops from the numerator
  only, since the audit itself wasn't the false positive. Streams with
  no cutoff/ack events — including the comparative harness's — are
  byte-identical to before. Follow-up in the same defect class closed
  the two remaining gaps: (a) **tombstone parity** — health's
  `_silent_miss_stats` also drops misses whose canonical top-hit
  memory has been tombstoned (numerator only; the audited denominator
  keeps its turns), which `compute_eval` couldn't see (its `memories`
  param is active-only), so after a miss's top-hit was removed the
  eval CLI's numerator exceeded `memory_health`'s `miss_total` over
  the same stream. `compute_eval` now takes an optional
  `tombstoned_ids` set (default `None` — byte-identical, so the
  comparative harness / scripted driver are untouched) applying
  health's filter #2 exactly (canonical `top_hits[0].id` only — no
  legacy `top_hit_ids` fallback, matching health's conservative
  read), and the eval CLI passes the store's real tombstone set. (b)
  **threshold-sweep policy** — `compute_threshold_sweep` replayed ALL
  logged misses, including bulk-cutoff-invalidated ones (flagged by a
  since-fixed code bug, so replaying them polluted the "is v1
  over-firing" calibration); it now applies the `silent_miss_cutoff`
  filter with the same global latest-wins resolution, while
  deliberately RETAINING acked misses in the replay — a confirmed
  false positive is exactly the calibration signal a stricter rule is
  judged against — with the asymmetry documented in the sweep
  docstring and docs/eval.md.
- **Corrupt event-log lines that parse as valid JSON but not an object
  no longer crash `memory_health`.** A hand-edited / partially
  overwritten line in the plain-text, git-syncable event log that
  json-parses to a list, string, number, or null slipped past the
  reader's JSONDecodeError guard and flowed to consumers as a non-dict,
  violating the iterator's declared `Iterator[dict]` contract: the eval
  surfaces' isinstance guards skipped the row, but `compute_health`'s
  first `ev.get(...)` raised AttributeError — one corrupt line took
  `memory_health` / `memory_scope_overview` / `report_for_directory`
  down entirely, the same eval-vs-health corrupt-row-tolerance
  divergence this release's theme covers. Such lines are now skipped at
  the shared parse site (`_iter_json_lines`) like any other corrupt
  line, so every reader — `iter_events`, `iter_all_events`,
  `iter_events_window` — is protected identically, matching the eval
  surfaces.

### Removed

- **The key-gated `LiveAgent` eval path.** `tests/eval/driver.py` no longer
  ships the one-shot Anthropic-API "agent", and `--driver live` is gone from
  `python -m tests.eval.comparative`. Three reasons: it required a raw
  `ANTHROPIC_API_KEY` that the project's own agent workflow never holds (so
  it could never actually run where the eval is driven from); it had already
  cost two honesty-defect fixes (3.7.1); and a staged single-turn completion
  role-playing "an agent answering with these memories" is not an agent
  session — its output would have worn the "publishable measurement" label
  the eval surface exists to keep honest. The `Agent` protocol, `run_driver`,
  and the deterministic `ScriptedAgent` (the CI-exercised compute-path proof)
  all stay; the honest source for live `memory_helped_rate` /
  `endorsement_rate` numbers is production telemetry — `bettermemory eval`
  over a real store's event log. The driver's internal event label
  `triggered_from` changed `live_agent_driver` → `agent_driver` (nothing
  consumes it), and docs/ROADMAP re-scope the comparative-publication plan
  onto dogfood telemetry.

## 3.10.0 - 2026-06-10

The heuristic-correctness release: the parked 146-finding extractor-hunt
backlog drained end to end, then the drain itself adversarially
re-audited and every confirmed finding fixed. ~150 defects closed across
the durability / credential / groundedness / proposals / scope-match /
origin / audit / consolidate / search heuristics, each with a regression
test (~2,100 → 2,540 tests). No breaking changes; additive wire fields
only.

### Fixed

- **Credential gate**: catastrophic-backtracking (ReDoS) in the new
  env-prefix clause — a dense snake_case/kebab paste could hang the
  write path for tens of seconds; the prefix repetition is now bounded
  (lossless for recall) and pinned by a perf regression test. Plus:
  connective separators (`is set to` / `was rotated to`), quoted/JSON
  keys, env-prefixed keywords, connection-URI passwords, `Bearer`
  tokens, `:=`/`=>` separators, a datetime-shape guard, the 200→1024
  value cap, and snake_case/kebab identifier false positives.
- **Durability gate**: ~20 transient-marker fixes — fronted/medial
  "today", "as of <date>", future-scheduling ("next week", "tonight"),
  in-progress vocabulary, git-state phrases ("unpushed",
  "uncommitted changes" with copula anchoring), UUID/hex false
  positives on the commit-SHA marker, proper-noun guards ("the New
  York office", kanban "In Progress" columns), habitual "temporarily".
- **Groundedness gate**: contraction fragments are no longer universal
  anchors; a zero-anchor floor catches short hallucinated claims (with
  alias/prefix tolerance — "Neovim" grounds on "nvim"); mid-line
  speaker labels no longer donate freebie anchors; dotted
  abbreviations, ISO-date forms, sentence-split edge cases.
- **Proposals extractor**: smart-apostrophe (U+2018/U+2019)
  normalization; negated-contraction question rejects; markdown-bullet
  and past-tense/conditional guards; deictic "remember this" requests
  no longer mint contentless proposals; explicit capture requests
  dropped by the transient gate now emit an observable WARNING.
- **Search & ranking**: per-term body TF saturation (keyword spam can
  no longer outrank full-coverage matches; single-term queries keep
  discrimination), body-vs-scope idf split, FTS5 MATCH expressions now
  built from the ranker's own tokenization (symbol aliases like
  `C++`/`C#`/`.NET` and joined tokens no longer miss index
  candidates), dotted-version and NFC/NFD normalization, possessive
  fragments, suspended hyphenation, diacritic folding, and an
  index-level saturation signal so auto-scope filtering on large
  stores can't silently starve to zero hits.
- **Audit / Stop hook**: the retrieval shield, attribution window, and
  endorsement tally now share one 600s substrate across all three
  producers; a separate 60s creation shield (a memory written this
  turn isn't a miss, but ten-minute-old memories are visible again);
  event-log rotation no longer hides the turn's own search; the shield
  counts same-worktree retrievals regardless of session; semantic-mode
  configs record an explicit `no_signal` instead of silently crashing
  the probe; probes rank with the configured half-life/endorsement
  knobs (probe-matches-the-ranker).
- **Consolidate / health**: one shared dead-weight predicate
  (freshest-touch window, 2-day endorsement grace,
  unresolved-contradiction parking) behind `memory_health`,
  `memory_scope_overview`, and the demotion pass; the
  from-transcript provenance stamp is stripped before dedup
  similarity in both paths (two distinct facts from one transcript
  can no longer dedup-tombstone each other); scope-merge and demotion
  retags preserve verification attestations; scope-typo detection
  unified with health's length-scaled rule; `--from-transcript` no
  longer mines harness-injected synthetic rows.
- **Origin matching**: remote-name-agnostic capture; Azure DevOps /
  Bitbucket `/scm/` / SSH-over-443 / scp-form / `git+ssh` /
  `insteadOf` / push-mirror normalization (with a bridge for origins
  captured under the old idiom); vendor route-prefix stripping gated
  to known hosts so GitLab subgroups can't merge distinct repos;
  worktree_root captured for remoteless repos; `ingest` stamps origin
  from session evidence with a collision-safe cwd cross-check.

### Added

- `turn_audited` events and `MissReport` carry `no_signal_reason`;
  `memory_health` carries `no_signal_total` and `bettermemory eval`
  carries `turns_no_signal` — `no_signal` audits are excluded from the
  silent-miss-rate denominator so a probe stuck at "declined" can't
  read as a healthy 0% miss rate.
- `memory_curate` / `consolidate` reports carry `polarity_skipped` —
  near-duplicate pairs the negation guard refused to auto-tombstone,
  surfaced suggest-only as possible contradictions.

### Docs

- `docs/api.md`, README, tool descriptions, and `config.toml` prose
  updated to the real semantic-mode contract (an installed embeddings
  extra alone does not enable semantic participation — the config-level
  opt-in does) and the shared dead-weight rule.
- `docs/audit/extractor-hunt-2026-06-09.md` rewritten to drained
  status; the JSON remains the archival artifact.

## 3.9.0 - 2026-06-09

A feature release centered on killing path-drift false signals — the
phantom `path_drift_missing` flags that made healthy memories read as
stale forever. Driven by live false flags found in the dogfood store and
a 4-round multi-agent hunt (224 agents, 10 heuristic surfaces, every
finding adversarially re-verified with a runnable repro). No breaking
changes; one wire-shape addition.

### Added

- **`verified_absent_paths` attestation on `memory_verify`.** The mirror
  axis to `verified_paths`: body-cited paths you confirm are
  *intentionally* absent on this machine — a remote host's path, a
  platform-conditional location (`~/.config/...` cited for Linux while
  running on macOS), a path the body cites precisely because it is NOT
  the real one. Path-drift reports them under a new
  `path_drift.expected_absent` bucket instead of `missing`, so the
  staleness verdict stops nagging about absences that are the expected
  state. Persisted in frontmatter, preserved through scope-only updates,
  tombstone/restore, and no-arg verifies; surfaced on `memory_show`,
  expanded search hits, and the web UI detail view. Extraction
  heuristics can't read that context — the attestation layer is where
  human/agent judgment lands.

### Fixed

- **Path extractor: spaced directory segments.** Bare
  `~/Library/Application Support/...` citations used to truncate at the
  space, and the truncated prefix false-flagged missing on every
  retrieval. The bare scan now continues through title-cased spaced
  segments that resume with a slash; terminal spaced components it
  can't capture safely are dropped when missing rather than flagged
  (the flag would be manufactured by our own truncation). Drive and
  home anchors now count as directory boundaries, so
  `C:\Program Files\...` and `~/Calibre Library/...` are extracted;
  shell-escaped spaces (`My\ Drive`) are unescaped.
- **Path extractor: URL routes.** A body citing a domain-attached route
  (`pypi.org/pypi/bettermemory/<ver>/json`) no longer gets same-rooted
  absolute candidates (`/pypi/bettermemory/json`) stat'd as local
  files; well-known web filenames (`/robots.txt`, `/openapi.json`, …)
  are recognized as routes despite their extensions.
- **Path extractor: the rest of the confirmed hunt findings.**
  Code-citation line suffixes (`file.py:407`, `:445-461`, `:12:5`)
  check the underlying file; `@`/`+`/`%` survive in bare paths
  (homebrew kegs, systemd templates); `VAR=/path` and `--flag=/path`
  assignments, markdown table cells, and smart-quoted paths are
  extracted; `$HOME/` canonicalizes to `~/`; balanced trailing `)` is
  kept (`project (archived)`); glob, template-placeholder
  (`<app>`/`{service}`), and `//host/share` SMB citations are excluded
  as shape claims; single-argument commands (`/opt/homebrew/bin/brew
  upgrade`) no longer flag; sentence-final citations flag correctly
  while `report (2).pdf`-style continuations don't; attested paths
  always flag when deleted (verified-then-deleted is real drift);
  citation order no longer decides whether drift is reported; `~/x`
  and `/Users/me/x` spellings dedup to one claim; acronym glue
  (`/etc/hosts TCP/IP`) falls back to the real path.
- **Credential gate (HIGH): sentence-final periods masked real
  secrets.** `my password is <secret>.` was read as a dotted module
  reference and waved through; trailing prose punctuation is now
  stripped before the guards. Coverage also extended: encrypted-PKCS#8
  PEM headers and Slack `xapp-`/`xoxc-`/`xoxe-` token families.
- **Auto-scope (HIGH): linked-worktree blackout.** Sessions running in
  a `git worktree` checkout (spawned agent worktrees, PR-review trees)
  could not see ANY memory written in the primary checkout — the
  repo's shared knowledge — because the worktree filter required exact
  root equality. A caller in a linked worktree now matches memories
  from its primary (derived from the worktree's `.git` file, no
  subprocess), and memories recorded in since-deleted worktrees degrade
  to repo-level matching instead of being invisible forever. Live
  sibling worktrees stay isolated — the original leakage fix is
  preserved.

### Notes

- The hunt that drove this release hit its round cap still finding
  fresh issues; the 146 remaining verified findings are parked with
  full detail in `docs/audit/extractor-hunt-2026-06-09.{md,json}` as a
  pre-verified queue for future audit passes.

## 3.8.0 - 2026-06-09

A feature release. Adds a write-time credential check so a secret pasted
into a memory body is refused before it reaches the plain-text store. No
breaking changes.

### Added

- **Credential check at write time.** `memory_write` now runs a
  credential-shaped-token detector (`src/bettermemory/credentials.py`) as
  the first write gate, ahead of the durability check. A body that embeds a
  secret-shaped token — a vendor-prefixed API key (AWS `AKIA…`,
  OpenAI/Anthropic `sk-…`, GitHub `ghp_…`/`github_pat_…`, Slack `xox…`,
  Google `AIza…`, Stripe `sk_live_…`), a private-key PEM header, a JWT, or a
  guarded `password=…`/`api_key=…` assignment with a high-entropy value —
  returns `{status: "credential_warning", markers: [...]}` and persists
  nothing. The store is plain-text markdown that `sync` pushes across hosts
  via git, so a captured secret would otherwise rot there unencrypted and in
  the `.events.jsonl` audit trail. **The matched value is redacted from both
  the warning and the event log** (only the detector `kind` is recorded), so
  the gate never re-leaks what it refused. The detector is precision-first —
  prose that merely mentions "api_key" or "password" never fires — and the
  rare legitimate case (a documented public/example credential) passes with
  `acknowledge_credential=True`, logged as an override by kind. Symmetric in
  shape to the durability gate; default-on, no config knob.

## 3.7.1 - 2026-06-09

A patch release. An adversarial diff-audit of the 3.7.0 changes (the
reactive sweep over the just-shipped feature commits) surfaced three real
issues — one user-facing performance regression and two honesty defects in
the new eval driver — fixed here. No breaking changes, no new public
surface; the 25-tool count is unchanged.

### Fixed

- **Search no longer opens the link index once per hit.** The 3.7.0
  `supersedes`/`contradicts` activation (`attach_link_annotations`) is
  default-on on every hit-producing search, but it called `links_for` per
  hit — each a full SQLite open (connect + PRAGMAs + sibling chmod-stat +
  schema-ensure). A 50-hit search did up to 50 sequential index opens. A new
  `index.links_for_many` resolves all hits in a single connection (two
  `IN (...)` queries); per-id results and ordering are identical, only the
  latency changes.
- **Live-agent eval driver: `searched` is now a real model decision.**
  `LiveAgent` derived `searched` from `bool(hits)` — the ranker's own
  output — so any probe with a hit scored as "the agent searched", collapsing
  the live `silent_miss_rate` toward 0 regardless of model behaviour, under a
  "publishable measurement" banner. It now reads the model's explicit
  decision (the parse is extracted into a unit-tested pure function; only the
  model call stays the live boundary).
- **Live-agent eval driver: citation excerpts are validated against the
  body.** The honesty guard checked the truncated *snippet* (which carries a
  synthetic `...`), so a model echoing the ellipsis could inflate
  `memory_helped_rate` with a phrase the memory never contained. Validation
  now runs against the full body, centralized in `run_driver` so it holds for
  every agent; a type-wrong excerpt is dropped rather than crashing the run,
  and the "a citation implies searched" coherence applies only to citations
  that survive that body check.

## 3.7.0 - 2026-06-09

A feature release that rotates the project off the post-3.6.x bug-audit
treadmill (whose whole-codebase sweep yield had gone asymptotic) and onto
value: one new MCP tool, one onboarding command, two retrieval-quality
levers, and the eval driver that unblocks the comparative publication. The
25-tool count (21 `memory_*` + 4 `episode_*`) reflects the new
`memory_curate`. No breaking changes.

### Added

- **`memory_curate` MCP tool** — execute the curation `memory_health` only
  describes. Its recommendations pointed at a `bettermemory consolidate` CLI
  an in-session model can't run; the consolidate engine had no MCP handler.
  The tool wraps that same hardened engine (the one the Stop-hook
  `run_auto_consolidate` path uses) behind a `dry_run=True`-by-default
  contract: a side-effect-free preview, then on `dry_run=False` only the two
  reversible actions — tombstone near-duplicates (undo via `memory_restore`)
  and demote dead-weight facts to `ambient` (undo via `memory_update`).
  Gated behind `full_tool_surface`, so zero default-surface context cost.
- **`bettermemory try`** — a 60-second, zero-network demo of the staleness
  verdict. In an isolated temp store it writes a memory citing a file,
  attests it, deletes the file, then shows the next search flagging it
  (`staleness_verdict: spot_check_recommended`, `path_drift.missing`
  populated). The headline differentiator is otherwise invisible on a fresh
  store; this makes it visible on demand. Exits non-zero if it can't
  reproduce the drift (a self-test of the verify→drift→verdict path).
- **Usage-aware ranking** (`[behavior] endorsement_boost`, opt-in, default
  off) — a bounded endorsement factor (mirrors the recency boost, capped at
  +10%) nudges memories the model has *explicitly* applied up the results, so
  a load-bearing fact wins a near-tie. Capped so it can never override
  relevance; explicit applies only (the auto-fallback is excluded). Off by
  default: it reorders results and costs one event-log read per search, so
  the shipped default is byte-stable.
- **`supersedes` / `contradicts` link activation** — these `MemoryLink` edge
  types existed since 2.x but retrieval ignored them. Search hits now carry
  `superseded_by` (active memories that supersede this hit — prefer them) and
  `contradicts` (memories in unresolved contradiction, either direction),
  purely additively (annotation only — never reorders or drops a hit), with
  the same caps / targeted-load / scope-refilter discipline as
  `depends_on_resolved`.
- **Live-agent eval driver** (`tests/eval/driver.py`,
  `python -m tests.eval.comparative --driver scripted|live`) — the machinery
  that was the open piece before the comparative publication. An `Agent`
  protocol + `run_driver` turn a real agent's cite-decisions into the full
  `memory_helped_rate` / `endorsement_rate` / `silent_miss_rate` trio. A
  deterministic `ScriptedAgent` proves the compute path in CI (authored
  citations — a demonstration, not a measurement; `LiveAgent`, gated behind
  `ANTHROPIC_API_KEY`, produces the publishable numbers). The offline
  adapter's honest `n/a` is unchanged.

### Internal

- Subtracted accreted duplication (post-3.6.5 inverse-of-the-audit-loop
  pass): a shared `_resolve_dedup_thresholds` across both write-dedup gates,
  a shared `_parse_silent_miss_event` across the two silent-miss readers in
  `health.py` (making their required numerical agreement structural), and a
  redundant local `import re` in `sync.py`. An adversarial verification pass
  correctly *kept* 5 of 14 removal candidates that were deliberate
  defense-in-depth.

### Fixed

- **`.gitignore`** now ignores the `.venv` *symlink* form, not just the
  `.venv/` directory — the dev-machine symlink could otherwise be committed
  as a machine-specific absolute path.

## 3.6.5 - 2026-06-08

The first whole-codebase shippability sweep run *at the shipped tree* (prior
audit rounds had only diff-audited, so files no recent change touched had
never been read end to end) surfaced ten correctness/security gaps in stable
code. Every fix carries a fail-before/pass-after regression test; the
embedding-dedup fix below was itself caught and corrected by an adversarial
review of the sweep commit.

### Fixed

- **Read-path DoS (`_frontmatter`).** A memory file with deeply-nested YAML
  frontmatter raised `RecursionError` during parse — which is not a
  `yaml.YAMLError`, so it escaped both the parser's catch and the store's
  malformed-file skip. One ~1 KB crafted file (a hand-edit, or a hostile
  `sync pull` writing into the memory directory) then propagated out of
  `load_all` / `load_one` / `load_tombstones` / `rename_scope` and DoS'd reads
  of the *whole* store. It is now translated to `ValueError`, so the existing
  skip drops just the bad file and the rest of the store stays readable —
  restoring the module's "one corrupt file shouldn't blind the store" contract
  on the read side (the write side already guarded nesting depth).
- **Embedding `encode()` fail-open (`search`).** A loaded embedding model that
  raised at `encode()` time (a device / OOM / tokenizer fault — distinct from
  the already-handled "model failed to load" case) crashed the live
  `memory_write` dedup gate and default-mode `memory_search` instead of
  degrading to lexical scoring. All four semantic dispatch points (hybrid +
  semantic search, active + tombstone dedup) now fall back to keyword / Jaccard
  with a warning. The dedup fallback uses the Jaccard-natural thresholds (not
  the cosine thresholds the write-dedup gate supplies), so a near-duplicate the
  gate should block is still caught rather than committed as a silent parallel
  duplicate.
- **Scoped search under-return (`search`).** On a store large enough to use the
  FTS5 candidate pre-filter (>500 memories), a scoped query whose in-scope
  matches all ranked outside the global top-50 returned empty, because the
  pre-filter queried the index scope-blind. It now threads the scope filter
  into the index query.
- **`migrate` trust-boundary gaps.** `bettermemory migrate` no longer rewrites
  a memory whose `schema_version` is newer than this reader supports (matching
  the store's forward-compatibility gate), and no longer follows a symlinked
  `.md` (matching the store iterators' symlink rejection) — both close
  `sync pull` exposure.
- **`reindex` exit code.** A write failure during `bettermemory reindex`
  (read-only directory, full disk, SQLite I/O error) now exits 2 with a clean
  `error:` message like the other write-capable CLI commands, instead of
  dumping a traceback and exiting 1.

### Changed

- **Config validation (`config`).** `[scopes] allowed` now rejects a bare
  string scalar — a forgotten-brackets `allowed = "myproject"` previously
  char-exploded into a per-character allowlist that rejected every real write —
  and the numeric config fields report a clear, located error on a non-numeric
  value instead of an opaque `int()` / `float()` traceback escaping
  `load_config` (which crashed `serve` startup).

### Internal

- **Event-log append cost (`events`).** Orphan-rotation recovery, which scans
  the (shared) store directory, no longer runs on every event append — it is
  deferred to the rotation path, where it is actually needed. On a large store
  this removes an O(directory-size) scan from every tool call that records an
  event.

### Documentation

- **README top rewritten for fast comprehension and install.** A one-line value
  proposition, a tighter opening pitch, and a prominent **Quick start** section
  pulled ahead of the deep-dive, so a first-time reader grasps what the project
  is and how to install it before the differentiators. ROADMAP refreshed to the
  current release and test count.

## 3.6.4 - 2026-06-06

### Internal

- **Completed the tool-description context-valve sweep.** 3.6.2 closed the valve
  for the two policy-heaviest default-on tool descriptions (`memory_search`,
  `memory_write`) — "first 2 of 18." This finishes it: the remaining 16
  default-on descriptions were audited (one finder per description, every
  proposed cut adversarially verified to preserve information and discoverability)
  and their residual redundancy collapsed, with **all** field/parameter reference
  and test-pinned substrings preserved. `memory_write`'s trigger→category
  enumeration (a second verbatim copy of the instructions-block policy) became a
  redirect; `memory_verify`'s optimistic-concurrency paragraph now cross-references
  `memory_update` instead of duplicating it; `memory_scope_overview` shed two
  illustrative restatements; `episode_search`/`episode_promote` were tightened.
  Net: the lean default-on descriptions resident on **every** turn dropped from
  27,681 to 26,976 chars (~6,920 → ~6,744 tokens). The ratchet ceiling guarding
  this (`test_default_on_descriptions_fit_budget`) drops 27,800 → 27,100 to lock
  the win in; policy lives once (the `instructions` block), descriptions keep
  genuine field reference plus at most one inline point-of-call cue.

## 3.6.3 - 2026-06-06

### Internal

- **Audit-sweep follow-up to the 3.6.2 context-valve collapse.** Two findings
  surfaced by an adversarially-verified diff-audit of the 3.6.2 commit, both
  narrow:
  - `DESC_MEMORY_WRITE`'s point-of-call trigger cue had drifted from "a project
    decision *the user* concurred with" to "...*you* concurred with" during the
    de-triplication. Every canonical policy home (the `instructions` block,
    `prompts.py`, `docs/system_prompt.md`) kept the USER subject; only the inline
    description drifted. "you concurred" reads as license for the model's own
    agreement to mint a user-attributed `fact` — exactly the misattribution the
    user-inference/veto design exists to prevent — so the inline cue is restored
    to match canonical policy.
  - `test_policy_lives_once_not_triplicated_in_descriptions` used `len(...) > 1`,
    which only catches re-triplication of the *current* wording. Three of the
    five tracked policy phrases had no survival floor elsewhere, so a harmless
    reword would drop the substring to zero matches and the guard would pass
    vacuously — silently un-tracking that rule. Tightened to `!= 1` so a reword
    fails loudly; this now floors those three phrases.

## 3.6.2 - 2026-06-06

### Internal

- **Closed the tool-description context valve.** The lean default-on tool
  descriptions — resident in context on *every* turn, including the ~90% that
  never touch memory — had grown to ~27.9 KB by restating the opt-in /
  announce-on-search / proactive-write policy verbatim across three places: the
  server `instructions` block, the `DESC_*` strings, and
  `SYSTEM_PROMPT_ADDENDUM`. The instructions-block budget test had quietly made
  descriptions the sanctioned overflow sink ("push detail down into the tool
  descriptions, which are not truncated") with no counter-guard, so for a
  project whose whole purpose is minimising per-turn context the valve was
  leaking. Policy now lives once (the `instructions` block); `memory_search` and
  `memory_write` keep genuine parameter reference plus at most one inline
  point-of-call cue. Three regression guards close it permanently: a sum-ceiling
  ratchet that fails toward collapsing policy rather than raising the cap, a
  de-triplication invariant (each policy phrase in ≤1 default-on description),
  and a dissent guard pinning the surviving point-of-call cues that the offline
  eval can't yet protect.

## 3.6.1 - 2026-06-05

### Fixed

- **`consolidate --acknowledge-debt` no longer shields dead-weight memories
  from removal.** Its inline cold-endorsement filter omitted the
  `applied_count > 0` gate the canonical `health` rollup enforces, so a pure
  dead-weight memory (retrieved over the floor but *never* applied — zero auto
  *and* zero explicit) was wrongly swept into the endorsement pass and handed a
  fabricated `use(applied)` event. That bumped its applied-count to 1 and
  permanently excluded it from the dead-weight removal/demotion bucket (which
  requires `applied_count == 0`). The CLI filter now matches the canonical
  predicate exactly.
- **`consolidate --acknowledge-debt` refuses when telemetry is disabled**
  instead of printing "wrote N events" and exiting 0 while the disabled
  recorder silently dropped every write. Mirrors the existing
  `--acknowledge-misses-before` guard.
- **`bettermemory reindex` can now repair a corrupt or version-skewed index.**
  `index.rebuild()` — the documented recovery primitive — previously crashed on
  exactly the inputs it exists to fix: a torn `.index.sqlite`
  (`sqlite3.DatabaseError`) or an on-disk schema version newer than the running
  code (`IndexVersionError`). It now drops the index file and its `-wal`/`-shm`
  siblings and rebuilds from the canonical Markdown files. The read path still
  refuses a newer-version index.
- **No more bare `OSError` (carrying the absolute store path) leaking past the
  MCP boundary on a disk-full / EIO / EACCES write.** `memory_proposals(accept)`,
  `memory_write`, and `memory_write_confirm` now translate a disk-level
  `OSError` into a clean structured error, matching the other lifecycle tools.
  The proposals-accept error also flags that the queued entry may have been
  consumed, so a retry is safe to reason about.
- **`bettermemory export -o` and `bettermemory init --config-path` exit cleanly
  (code 2) on a bad or unwritable target path** instead of dumping a raw
  `FileNotFoundError` / `PermissionError` traceback — completing the gated-tool
  CLI error-handling parity started in 3.6.0.

### Internal

- Post-3.6.0 whole-codebase audit sweep (12 domains / ~36k LOC) with adversarial
  verification of every fix, plus a second pass that caught and drained three
  regressions in the fixes themselves. +11 regression tests (2017 → 2028).

## 3.6.0 - 2026-06-04

### Added

- **CLI parity for the gated curation tools.** Five of the six tools held back
  from the lean default surface (`[behavior] full_tool_surface = false`) now
  have a direct `bettermemory` CLI counterpart, closing most of the gap between
  that promise (made in the README and the 3.4.0 notes) and the shipped surface:
  - `bettermemory tombstones restore <id>` — un-tombstone a memory (the CLI
    counterpart of `memory_restore`). Pairs with the existing `tombstones
    list`; previously a removed memory could be *listed* from the CLI but not
    *restored* without enabling the full tool surface.
  - `bettermemory rename-scope <old> <new>` — bulk-rename a scope tag across
    active memories and tombstones (the CLI counterpart of
    `memory_rename_scope`), with `--no-tombstones` and `--json`.
  - `bettermemory proposals list | accept <id> --scope … | dismiss <id>` —
    review the write-reflex proposal queue from the CLI (the counterpart of
    `memory_proposals`). `accept` shares the MCP tool's exact
    validate → atomic-claim → write core
    (`handlers.proposals.accept_proposal`), so the write-policy and
    no-double-accept guarantees can't drift between the two entry points.

  The sixth, `memory_acknowledge_miss`, keeps its per-event ack MCP-only; the
  CLI's bulk `consolidate --acknowledge-misses-before` cutoff is the closest
  existing counterpart, not a 1:1 replacement.

### Fixed

- **(docs) README / ROADMAP / api.md drift against the code.** A whole-diff
  audit of the 3.5.0 docs overhaul, plus a closing self-audit, corrected the
  blanket "all six gated tools reachable via the CLI" overclaim (now spelled out
  per tool — five have direct CLI counterparts; `memory_acknowledge_miss`'s
  per-event ack is MCP-only, with only a bulk CLI cutoff); the ROADMAP
  attributing the lean default surface to 3.3.4 instead of 3.4.0 (two places);
  and the ROADMAP double-counting the four `episode_*` tools as "plus 4" on top
  of the 24 when they are already inside the 24 (and inside the default 18).

## 3.5.0 - 2026-06-01

A correctness + robustness release from a whole-codebase audit sweep of the
post-3.4.2 tree: 17 fixes across the store, episodic, telemetry, verification,
consolidation, web, and CLI surfaces — including two that could lose or hide
data. The minor bump reflects one behavioral change: `episode_search` now
scopes its bare discovery walk to the caller's git worktree by default (new
`auto_scope` parameter). Explicit `swarm_id` / `parent_session_id` reads are
unaffected.

### Fixed

- **`memory_show` — a corrupt or version-newer index no longer crashes the
  read.** `_links_payload` called `index.links_for_with_status` with no
  exception guard, so a torn/truncated `.index.sqlite`
  (`sqlite3.DatabaseError`) or an on-disk `schema_version` newer than the
  reader (`IndexVersionError`) propagated out of the bare-registered
  `memory_show` tool and failed *every* id until `reindex` — even though the
  canonical `.md` files were intact and `memory_search` already degrades
  gracefully via the tolerant `index.status()`. `memory_show` now catches both
  and falls back to the `load_all` reverse-link scan.

- **`memory_update` — an over-cap links list silently lost the whole record.**
  `model_copy(update=...)` skips field validators, so a `memory_update` with
  more than 64 links committed as `status="committed"` but then vanished from
  every read surface: load-time re-validation hit the 64-link cap and the
  record was skipped. The cap is now enforced on the update path, matching the
  existing `scopes` / `verified_*` guards against the same bypass.

- **`episode_search` — the most-recent-N window sorts by datetime, not the ISO
  string.** `datetime.isoformat()` omits fractional seconds at microsecond 0,
  so a whole-second / bare-date episode mis-sorted as the newest of its second
  and could drop a genuinely-newer episode from the cap (and surface a
  non-chronological order).

- **`episode_handoff` — an invalid explicit `prior_session_id` degrades to an
  empty result** instead of raising a raw `ValueError` on the iteration-entry
  hot path, matching the auto-resolution branch and `episode_search`.

- **Telemetry double-count of hook-attributed retrievals.** A Stop-hook
  `applied` event (written under the Claude Code transcript id) was not
  deduplicated against the in-process auto-commit (keyed on the server
  `sess_<hex>` id), so every hook-attributed retrieval was counted twice —
  inflating `memory_helped_rate`, the dead-weight / cold-endorsement split, and
  the explicit-vs-auto ratio. The dedup now bridges both id spaces.

- **`cold_endorsement` vs `dead_weight` overlap.** A never-applied memory past
  the retrieval floor was counted in *both* buckets (and routed to
  `acknowledge-debt` when it belonged on the removal path). The
  cold-endorsement bucket is now gated on "at least one apply happened" across
  all three surfaces (`compute_health`, `curation_counts`, `eval`).

- **Episode prune — the lock-sidecar unlink moved past `flock` release.**
  `prune_old_sessions` unlinked the `.session-<id>.lock` sidecar inside the
  `flock` block — the same Windows open-handle class as the 3.4.2 store fix,
  dead on Windows and masked only by the post-loop orphan sweep. It is now
  deferred to after the lock releases.

- **Commit-drift verdict unified across surfaces.** `memory_show` counted drift
  via `git rev-list --since` (committer date, inclusive whole-second) while
  `memory_search` and `memory_health` bisect over author timestamps
  (strictly-greater, microsecond). The same memory could read drifted on one
  surface and clean on another — after a rebase, *and* with zero rebases when
  `last_verified_at` landed in a commit's UTC second. All four sites now share
  the author-date + `bisect_right` path with an identical `count > 0`
  verified-paths narrowing guard, resolving a long-parked divergence.

- **`consolidate` demotion reads the full event history.** The demotion /
  cold-scope passes read only the active event log, so after log rotation a
  genuinely endorsed fact whose `applied` events had aged into a `.gz` archive
  was auto-demoted `fact → ambient` (including on the unattended
  auto-consolidate path). They now read the rotated archives too, matching
  `memory_health`'s canonical rule.

- **Durability gate — decimal numbers are no longer flagged as commit SHAs.**
  The transient-marker regex matched any 7–40 character run of `[0-9a-f]`,
  including pure decimals, so a durable fact mentioning an epoch / phone number
  / numeric id was wrongly rejected as transient. A SHA match now requires at
  least one `a–f` hex letter.

- **Web UI — the memory detail page flags stale verification.** A memory
  verified long outside the staleness window rendered identically to one
  verified today; the detail page now shows a stale cue, matching the rest of
  the curation surface.

- **`_frontmatter` dump — alias expansion is bounded before materializing.**
  `dumps()` disabled YAML aliases, so crafted nested-alias frontmatter (via
  `sync pull` or a hand-edit) expanded to hundreds of MB on a re-dump before
  the size cap (checked post-hoc) could reject it — a CPU/memory DoS under the
  per-file lock. Expansion is now bounded up front.

- **`migrate origin` — one failing write no longer aborts the directory.** The
  per-file write was outside the try/except the read already had, so a single
  `ENOSPC` / `EACCES` aborted the loop and over-counted `updated`. Failures are
  recorded and skipped; the count reflects what actually persisted.

- **CLI — `tombstones list --scope <bad>` gives a clean error.** It raised a
  raw traceback leaking internal paths; it now exits 2 with an argparse-style
  message, like the sibling `export` command.

- **`memory_rename_scope` — disk errors surface as a structured error.** A
  genuine `OSError` mid-rename leaked raw through the MCP boundary (unlike the
  `remove` / `restore` / `update` / `verify` handlers); it is now wrapped in a
  clean `ValueError`.

### Changed

- **`episode_search` scopes to the caller's git worktree by default.** The bare
  discovery walk (no `swarm_id` / `parent_session_id`) now drops episodes from
  a different worktree of the same repository sharing one memory root —
  mirroring `memory_search`'s auto-scope and the isolation `episode_handoff`
  enforces. Explicit `swarm_id` (the N:1 swarm fan-in) and `parent_session_id`
  lookups are deliberate cross-tree reads and are never worktree-filtered.

### Added

- **`episode_search(auto_scope=...)`** (default `True`) — set `False` to sweep
  the bare discovery walk across every worktree sharing the memory root.

## 3.4.2 - 2026-06-01

A Windows-only follow-up to 3.4.1. 3.4.1's reverse-link, datetime, and
robustness fixes were all sound, but its tombstone-prune lock-sidecar cleanup
leaked the sidecar on Windows — caught by the CI matrix before publish, so
3.4.1 never shipped to PyPI. 3.4.2 is 3.4.1's full set of fixes plus the
Windows correction; upgrade directly from 3.4.0.

### Fixed

- **store (Windows)** — `prune_tombstones` unlinked each tombstone's `.lock`
  sidecar from inside the `with _locked(path):` block. On POSIX the held
  descriptor keeps the inode alive so the in-lock unlink succeeds, but on
  Windows `msvcrt.locking` holds the file handle open for the duration of the
  block and the OS refuses to delete a file with an open handle, so the unlink
  raised, was swallowed by the best-effort `except OSError`, and the sidecar
  leaked one orphan per pruned tombstone. The sidecars are now swept after the
  lock is released, mirroring `episodes._cleanup_orphan_lockfiles`.

### Note

- 3.4.1 was tagged but never published to PyPI (the Windows CI leg failed the
  pre-publish gate). Its changelog entry is retained below for history; 3.4.2
  supersedes it and is the first release in this line to ship.

## 3.4.1 - 2026-05-31

A parallel audit-loop backlog drain — 13 confirmed bug, robustness, and
efficiency fixes across the store, hook, config, attribution, request-handlers,
CLI, datetime/IO, and the search index. Each landed with its own regression
test.

### Fixed

- **hook** — `_pending_retrievals` used a bespoke ISO parser that returned a
  naive datetime (compared in local time, off by the UTC offset); replaced with
  `parse_event_ts` (tz-aware UTC). And the retrieval-kind dispatch only
  recognised `search`/`show`, so memories surfaced via `memory_list` were never
  eligible for Stop-hook attribution — `list`/`list_active` are now included.
- **store** — `prune_tombstones` deleted the tombstone file but leaked its
  `.lock` sidecar (one per pruned tombstone, unbounded); the sidecar is now
  unlinked best-effort. And `_as_dt` raised on a YAML bare-date scalar
  (`created: 2025-01-01` parses to a `datetime.date`), which upstream load
  swallowed as a skip — silently dropping the whole memory; it is now coerced to
  UTC midnight.
- **config / events** — boolean config keys were coerced with a naive
  `bool(...)`, so a quoted TOML value like `log_queries_verbatim = "false"` read
  as `True` — silently inverting a privacy opt-out; a string-aware coercion now
  handles it. And a non-positive `telemetry.max_bytes` made the rotation guard
  never hold, gzip-rotating the event log on every append; it is now clamped at
  load and guarded in the rotation check.
- **attribution** — claim-excerpt matching compared text without Unicode
  normalization, so a memory body using precomposed accents (U+00E9) never
  matched a reply using the decomposed form (U+0065 U+0301), silently
  undercounting `memory_helped_rate` on non-ASCII text; `_normalize` now
  NFC-folds and token extraction runs on the folded text.
- **request-handler robustness** — `memory_search` top-hit body expansion
  aborted the whole search on a transient `OSError` (now skips the inline body);
  `acknowledge_miss` reason had no maximum length (now bounded); proposal
  `accept` was non-atomic across three locks so a concurrent double-accept could
  duplicate (now claim-before-write idempotent); `episode_promote` advanced the
  record-use turn counter twice (now once).
- **core robustness** — remote LLM providers (Anthropic, OpenAI) issued requests
  with no timeout, so a hung provider could block indefinitely (now a default
  timeout); and the origin-backfill migrator mislabeled a file tombstoned
  mid-run as malformed (now skipped quietly).
- **search index** — the reverse-link index collapsed two links that shared a
  target but carried distinct typed notes, losing a note that exists on disk;
  the `memory_links` schema was widened (`SCHEMA_VERSION` 2 → 3) so
  distinct-note links are preserved while exact-duplicate links still collapse
  to a single row. `memory_show` serves correct `reverse_links` during the
  one-time post-upgrade index rebuild (see Upgrade notes), reading the inbound
  links and the index-populated signal in a single index open on the common
  no-inbound-link path.
- **scope / semantic** — scope-root matching guarded the leading but not the
  trailing boundary, so `projects:foo` could over-match `projects:foobar` (the
  trailing boundary is now enforced); and an empty-but-dirty semantic cache
  short-circuited its flush, stranding the dirty flag so a later write could be
  lost (it now flushes and clears).
- **CLI ergonomics** — `export --scope` and `eval --since` surfaced raw
  `ValueError`/`OverflowError` tracebacks on bad input (now clean CLI errors);
  `episodes prune --dry-run` diverged from the real prune for `ttl_days <= 0`
  (now matches); `reindex --embeddings` warmed the cache from the unstripped
  body while readers key on the stripped form (now strips); plus a corrected
  `detect_path_drift` docstring.
- **datetime / IO robustness** — the silent-miss probe (`audit.probe_for_miss`)
  passed an injected `now` through without UTC-coercion, raising `TypeError`
  against tz-aware event timestamps (now coerced via `ensure_utc`); the
  negative-outcomes enrichment parsed timestamps with a local helper that
  returned a naive datetime on offset-less input (now uses `parse_event_ts`);
  the `depends_on` targeted-load in `memory_search` caught only
  `MemoryNotFoundError`/`TombstonedError`, so a transient `OSError` reading a
  link target aborted an otherwise-successful search (now degrades gracefully);
  and `EpisodeStore` applies the same date-aware coercion to a hand-edited or
  legacy episode `created` field that previously crashed the whole episode read
  surface (including `episode_handoff`).

### Upgrade notes

- **Index schema → v3.** The reverse-link fix (item above) widened the on-disk
  search index schema (v2 → v3). The first time a bettermemory server starts on
  3.4.1 it rebuilds the local index once, automatically — no action required;
  `memory_show` serves correct `reverse_links` throughout the rebuild. If you
  run multiple bettermemory server processes against one store, restart them
  after upgrading so none keeps a v2 reader open against the rebuilt v3 index.

## 3.4.0 - 2026-05-31

Three fixes from a diagnostic pass over the dogfood event log (2,205 events,
41 memories). Two of them close gaps where a headline feature was silently
not firing in practice; the third trims the per-turn context cost the project
exists to minimise. The verification-surface fixes are the load-bearing ones —
a trust signal that cries wolf, and an audit trail that never captured its
positive label, are both worse than not having the feature.

### Changed

- **Lean default tool surface.** The server previously registered all 24 MCP
  tools unconditionally — ~9,500 tokens of descriptions in every turn's
  context. Dogfood telemetry (190 sessions) showed 43% of sessions never call
  any memory tool, and six curation/power-user tools saw 0–8 organic calls
  each. Those six — `memory_health`, `memory_acknowledge_miss`,
  `memory_rename_scope`, `memory_restore`, `memory_list_tombstones`,
  `memory_proposals` — now gate behind a new `[behavior] full_tool_surface`
  flag, so the **shipped default is 18 tools** (`memory_proposals` also
  auto-registers when `[proposals]` is enabled). All six stay reachable via the
  `bettermemory` CLI. **Upgrade note:** machines running the curate-loop /
  audit-loop skills drive `memory_health` / `memory_acknowledge_miss` /
  `memory_restore` as MCP tools and must set `full_tool_surface = true`. The
  episode tier and the two-phase write / scope-toggle tools stay in the default
  surface; gating those is deferred. New coverage in `tests/test_tool_surface.py`.

### Fixed

- **Commit-drift rollups ignored `verified_paths`.** `memory_scope_overview`'s
  `curation_pending.drifted` and `memory_health`'s `commit_drift_debt` counted
  every commit after `last_verified_at`, unlike `memory_show` and
  `memory_search`, which already narrow to commits that touched the paths the
  user attested. The rollup was the loudest surface and the only one missing
  the filter, so it flagged `spot_check` on memories deliberately marked stable
  and disagreed with the per-hit verdict — exactly the cry-wolf failure that
  trains the model to ignore the signal. In the dogfood store all 12 "drifted"
  memories were false positives (warm, applied, still-correct). `health.py`'s
  two drift loops now apply the same path filter via a `verified_paths_by_id`
  side-map, guarded so a caught-up memory pays no extra git call. Regression
  tests on both surfaces. (Residual: memories with no `verified_paths` still
  drift on every commit — a principled verdict-policy fix is queued.)
- **The Stop-hook claim-excerpt backfill never fired.** `attribute_uses` linked
  a memory to a reply only on a ≥30-char *verbatim* substring. Models paraphrase
  rather than quote, so the hook logged zero `attribution="hook"` events across
  the entire event history — and `memory_helped_rate`, the claim-level
  audit-trail metric, read a structural 0 as a result. Adds a precision-first
  token-containment tier: a candidate sentence also matches when ≥60% of its
  distinct content (non-stopword) tokens appear in the reply, with an absolute
  floor of 4 matched tokens. Calibrated so genuine reflections (≥0.8 overlap)
  match while coincidental topical overlap (≤0.4) does not; verbatim stays
  tier 1, so prior behaviour is preserved. Four regression tests pin both the
  new recall and the retained precision.

## 3.3.4 - 2026-05-30

The first **whole-tree** coverage audit — applying the diff-only-blind-spot
lesson the audit-loop's v5 `full_sweep_sha` gate was built for, but that had
never actually been run against bettermemory's own source. Every one of the
80 source files was read as whole files (not diffs) by a read-only review
fan-out, and each finding was adversarially verified against ground truth:
44 confirmed real (1 high, 7 medium, the rest low), 13 by-design residuals,
5 false positives. This release drains the high + medium tier plus the
confirmed documentation drift; the low-severity tail is queued for the loop.

### Fixed

- **(high) Stop-hook silent-miss telemetry was structurally inflated.**
  `run_audit` passed Claude Code's *transcript* session id into the probe's
  "did the model already retrieve this turn?" shield, but the server stamps
  its `search` / `show` / `list` events with its own `sess_<hex>` id — a
  different id space — so the shield never matched and was dead in the
  production Stop-hook path. Every turn where the model *did* search yet a
  high-relevance hit existed could still emit `search_miss`, inflating the
  silent-miss counters the curation surfaces triage. `probe_for_miss` now
  takes a `retrieval_session_id`, and the hook bridges to the live
  in-process server session (the same bridge `_emit_hook_attributions`
  already used); emitted events keep the transcript id.
- **(medium) Metadata-only `memory_update` could clobber a concurrent
  `memory_verify`.** `Store.update` CASes on `updated`, but verify bumps
  `last_verified_at` without touching `updated`, so a verify landing between
  a handler's snapshot read and its write passed the CAS and was silently
  overwritten by the stale snapshot. `Store.update` gained
  `preserve_verification`, which the metadata path uses to keep the freshest
  on-disk verification; content edits still reset it.
- **(medium) `memory_proposals` accept bypassed the scope whitelist and cap
  (fail-open).** Accepting a proposal wrote straight through `store.write`,
  enforcing only the content-size guard — not the `[scopes] allowed`
  whitelist or `max_scopes_per_write` that `memory_write` / `memory_update`
  / `memory_rename_scope` enforce. It now routes through the shared
  `_validate_write_payload`.
- **(medium) A naive-datetime episode crashed the whole episode read
  surface.** An episode whose frontmatter `created` had no UTC offset
  (hand-edited / legacy) raised an uncaught `TypeError` in the `created`
  sort and the `episode_search` `since` filter, failing every read instead
  of skipping the one row. `EpisodeStore._load_path` now normalises
  `created` to aware UTC at load.
- **(medium) The web dashboard understated verification debt.** The overview
  read `len()` of the capped never-verified / stale row lists (capped at 20)
  instead of the uncapped `*_total` fields, so the headline froze at 20 on
  larger stores.
- **(medium) `consolidate --semantic-threshold` help was wrong.** It claimed
  the flag is ignored without the embeddings extra, but the value is applied
  as the Jaccard cutoff — a different scale. Corrected.
- **(docs) API-reference drift.** Added the missing
  `memory_acknowledge_miss` subsection and `recent_silent_misses` in the
  `memory_health` return; corrected the `memory_rename_scope` return shape
  (`old_scope` / `new_scope` were omitted) and the unknown-link-type
  behavior (a bad entry is dropped, valid links still load); fixed the
  doctor-check count (ten, not seven) and a stale eval test count.

### Known / deferred

- `origin.commits_since` counts **committer**-date while `memory_search` and
  the health rollup count **author**-date, so a rebase can make one memory
  read drifted via `memory_show` yet clean via `memory_search`. The lying
  "author-date" docstring is corrected here; unifying both signals onto
  author-date — which must move the paths-filtered variant in lockstep and
  ship rebase-fixture tests — is deferred rather than rushed into a
  half-fix. The low-severity robustness tail (scattered naive-datetime read
  paths, a tombstone `.lock` sidecar leak, CLI argument-error ergonomics, a
  missing LLM request timeout) is queued for the audit loop to drain.

## 3.3.3 - 2026-05-28

A head-to-toe audit (24-unit subagent fan-out, every finding
adversarially re-verified) of the codebase plus the new
comparative-evaluation harness, draining 36 confirmed defects in one
pass. No breaking changes; several fixes correct silently-wrong
behavior, so a few observable signals shift — all toward honesty.

### Added

- **Comparative-evaluation harness** (`tests/eval/`, runnable via
  `python -m tests.eval.comparative`). A fixed synthetic workload run
  through the real `search` / `probe_for_miss` / `compute_eval` code,
  rendered as a capability matrix plus bettermemory's measured recall and
  silent-miss lanes. Live-agent rates (`memory_helped_rate` /
  `endorsement_rate`) report `n/a` offline rather than a misleading
  `0.0`, and competitor adapters never fabricate numbers.

### Fixed

- **Silent data loss via frontmatter overflow.** Oversized `links` notes
  or `verified_paths` / `verified_commits` / `verified_versions` could
  push a record's serialized frontmatter past the 64 KB YAML cap; the
  write reported success but the file then failed to parse on every read,
  dropping the memory from search/list/show/health. A write-side size
  guard now lives in `_frontmatter.dumps` (mirroring the read cap), and
  the `memory_verify` handler caps the attestation lists' count and
  per-item length (the model field validator is bypassed on the verify
  path's `model_copy`). `memory_proposals(accept)` now runs the same
  `max_content_bytes` guard every other write path enforces.
- **commit-drift no longer undercounts to zero from a repo
  subdirectory.** Git pathspecs are anchored at the repo root, so a
  verified path whose file genuinely moved is no longer reported
  `clean` / `fresh` when memory_show / memory_search runs from a subdir.
- **Stop-hook claim-excerpt attribution fires in production.** It was
  matching retrievals against the wrong session-id space (the Claude
  transcript id vs the in-process server's `sess_<hex>`), so it never
  attributed and `memory_helped_rate` could never rise from the hook
  tier; the lookup now bridges to the in-process session.
- **`consolidate` dedup no longer collapses a memory into an
  already-tombstoned keeper** in a transitive-similarity cluster — a
  dangling-reference data loss that ran unattended in the Stop hook.
- **memory_search per-hit commit-drift honors `verified_paths`,** so a
  memory attested as stable for a path no longer shows spurious
  spot-check noise on the search surface (now matches memory_show).
- **An auto-applied `record_use` no longer clears a `contradicted`
  flag** in `recent_negative_outcomes` — a memory the model flagged as
  wrong keeps its warning across the unattended auto-commit.
- **The event-log reader survives corruption.** A truncated or
  CRC-corrupt gzip archive, or a stray non-UTF-8 byte, no longer crashes
  `memory_health` / `memory_scope_overview` / eval / doctor — each
  degrades per-record instead of taking down the whole telemetry surface.
- **`bettermemory init` re-run preserves user customizations** on its
  own MCP entry (`env` incl. `BETTERMEMORY_DIR`, `disabled`, `timeout`)
  instead of clobbering them with the canonical shape.
- **Honest retrieval signals.** The hybrid semantic ranker reports only
  the query tokens that literally matched (no fabricated `match_terms` /
  `high` relevance on paraphrase-only hits); the LLM merge validator
  deduplicates repeated `duplicate_ids` (no false "concurrent tombstone"
  abort); `consolidate --from-transcript` gates transient markers on the
  durable body, not the verbatim provenance quote.
- Plus: `health` recent-silent-miss ordering, `ingest` intra-batch dedup
  and up-front `--scope` validation, `reindex --embeddings`
  stale-dimension purge, `doctor` symlink / stale-binary false positives,
  and a uv.lock version-sync guard. Documentation reconciled with
  behavior across `docs/eval.md`, `SECURITY.md` (the CSRF token gate),
  the event-log privacy note, and several stale code comments.

## 3.3.2 - 2026-05-28

Closes deferred queue item C3, plus a round of audit hardening on the
recent C3 / #28 changes.

### Fixed

- **The Stop hook now respects session-disabled scopes.** Previously a
  scope the user disabled in-session via `memory_scope_disable` (e.g.
  "this is unrelated to project X") still produced silent-miss flags from
  the client-side Stop hook, because the hook runs in a separate process
  and can't read the server's in-memory `SessionState`. The hook now
  reconstructs the disabled set from the event log — `scope_disable` /
  `scope_enable` are already persisted as events stamped with the
  server's stable per-process session id — and feeds it into the audit
  probe as `excluded_scopes`, the same shield the in-process
  `memory_audit_turn` applies. A turn the user framed as out-of-scope is
  no longer false-flagged. No new write path, no sidecar file, no schema
  change — the event log was already the single source of truth (patch
  bump). Reset-on-restart is eventual, not atomic: the reconstruction
  anchors to the most recent in-process server session, so a restarted
  server keeps replaying the prior session's disables until it writes its
  first in-process event (a conservative, self-correcting gap window).
  Residual divergence (two directions): under multiple concurrent
  sessions sharing one memory dir the anchor can cross-attribute disabled
  scopes (over-suppress), and the active-log-only read can drop a disable
  that rotated out while its session is still live (over-flag) — both
  narrow, matching the single-active-server deployment.

### Internal

Docstring accuracy and test/CI hardening from a multi-agent audit of the
C3 and #28 commits (no behavioral change):

- The C3 hook docstrings no longer describe reset-on-restart as "free" /
  atomic, and the residual-divergence note now distinguishes the
  over-suppress (concurrent sessions) and over-flag (active-log rotation)
  cases instead of claiming both suppress.
- `episode_handoff`'s module and handler docstrings are updated for the
  post-#28 reality: events now carry `worktree_root`, so a named-worktree
  caller can adopt a matching zero-episode prior session, with legacy
  (pre-#28) events falling back to the conservative None-only-matches-None
  rule.
- Tests: added a legacy-no-worktree fallback guard and a restart-gap-window
  test, tightened the C3 suppression assertion to pin the exact `no_signal`
  verdict, and pinned the loop-iteration end-to-end test to a named
  worktree so it no longer goes vacuous when run outside a git checkout.
- `ci.yml` documents that an `experimental` matrix leg would be exempt from
  the PyPI publish gate, so re-adding one is a deliberate decision rather
  than a silent bypass of the full-matrix-green contract.

## 3.3.1 - 2026-05-28

A single targeted fix closing queue item #28.

### Fixed

- **`episode_handoff` now matches episode-less prior sessions in the
  same worktree.** The `Recorder` captures its process worktree once at
  construction and stamps `worktree_root` on every event it writes.
  `episode_handoff`'s zero-episode branch reads the candidate session's
  worktree from the event log rather than treating it as unknown, so a
  prior session that wrote only events — a search-only loop tick, or one
  that crashed before `episode_write` — is correctly adopted as
  `prior_session_id` when it shares the caller's worktree. Previously
  such a session was skipped (the conservative None-only-matches-None
  rule), leaving a loop's next tick with no handoff anchor. Legacy
  events written before this release lack the field and fall back to the
  prior conservative behavior, so the change is backward compatible and
  the on-disk schema is unchanged (patch bump).

## 3.3.0 - 2026-05-28

Three additive features land this batch, all serving the same north
star — memory that compounds across many agents and over time.
**Multi-agent swarm fan-in for episodes** lets one coordinator gather
what all its parallel sub-agents learned; **opt-in self-improving
consolidation** lets the store quietly curate itself between turns; and
**opt-in write-reflex capture** closes the other half of that loop by
proposing durable statements the model forgot to save, for one-tap
review. All three ride alongside the documentation-accuracy and
durability test hardening already queued behind 3.2.2. Everything here
is additive (new optional parameters, two off-by-default config
sections, and one new MCP tool, no on-disk schema bump), so the batch
lands as a minor bump rather than a patch.

### Added

- **Multi-agent swarm fan-in for episodes (`swarm_id`).** Episodes can
  now carry an optional `swarm_id` cohort tag, and
  `episode_search(swarm_id=…)` (plus the new `EpisodeStore.list_by_swarm`
  primitive) gathers every episode bearing that tag across all session
  directories. This fills the gap between `episode_handoff` — a *1:1,
  single-chain* "my previous self → me" handoff resolved via the event
  log — and the *N:1* shape a swarm needs: a coordinator that fans out
  N parallel sub-agents (e.g. via the Agent tool) had no way to gather
  their collective takeaways. The flow: the coordinator passes its
  session id down to each sub-agent, every sub-agent stamps its
  `episode_write(swarm_id=<coordinator id>)`, and the coordinator then
  reads the whole cohort with `episode_search(swarm_id=<own id>)`
  ("what did all my sub-agents conclude"). `swarm_id` composes with
  `parent_session_id` to narrow a fan-in to a single sub-agent's
  session, and the returned rows now include `swarm_id` so a caller can
  correlate a takeaway back to its cohort. The storage layer already
  serialized concurrent multi-agent writes safely (per-file `flock`,
  index upsert inside the lock, WAL, CAS on update); this adds the
  missing *semantic* coordination on top of that foundation.

  Additive and backward-compatible: `swarm_id` defaults `None`, the
  writer only emits the frontmatter key when set (so non-swarm episodes
  keep their exact pre-field on-disk shape and `SCHEMA_VERSION` is
  unchanged), and legacy episodes load as `swarm_id=None`. The value is
  validated to the filesystem-safe id charset with a 128-char cap so a
  runaway value can't bloat the episode frontmatter — it is matched for
  equality at fan-in time, never used as a path component.

- **Opt-in self-improving consolidation (`[consolidate] auto_apply`).**
  A new off-by-default config section lets the store curate itself
  unattended. When enabled (and `[telemetry]` is on), the existing
  `audit-turn` Stop hook runs the *structurally-safe* consolidation
  subset at turn end — conservative near-duplicate dedup (a reversible
  tombstone) and demote-never-applied (a non-destructive fact→ambient
  retag). No LLM passes, no contradiction resolution; nothing that needs
  judgement. The pass is **debounced** (at most once per
  `auto_apply_interval_hours`, default 24h, clocked off a sidecar
  timestamp file that survives event-log rotation), **bounded** (skipped above
  `auto_apply_max_memories`, default 500, so the O(N²) dedup never stalls
  the turn-end hook), and **conservative** (Jaccard ≥ 0.90, stricter than
  the 0.75 manual default, with no embedding model loaded in the hook).
  Every action lands as a reviewable, reversible tombstone plus an
  `auto_consolidate` event (`memory_list_tombstones` + the event log) — the
  deliberate opposite of invisible "Dreaming" consolidation. Gated on
  `telemetry.enabled` because the event log is the reviewable audit trail for
  every auto-mutation: no log, no auto-mutation. This closes the loop the curation telemetry
  (silent-misses, cold-endorsements) previously only *surfaced* as
  recommendations a human had to read and act on.

- **Opt-in write-reflex capture (`[proposals] auto_propose` + the new
  `memory_proposals` tool).** Closes the documented writing-reflex gap —
  the model under-writes durable content during head-down work (the gap
  `attribution.py` exists to measure). When enabled, the `audit-turn`
  Stop hook scans each turn's USER message for durable-looking
  statements the model didn't save (explicit "remember…" requests,
  first-person preferences/setup facts; questions, task-requests, and
  transient run-state are rejected) and queues them as **inert**
  proposals at `<root>/.write_proposals.jsonl` (recording a
  `proposals_enqueued` event for observability). The new `memory_proposals`
  tool (24th MCP tool) is the review surface — `list` them, then `accept`
  one (a normal memory write, source=inferred — `scopes` required, the
  queue doesn't guess them) or `dismiss` it; `memory_scope_overview`
  reports `proposals_pending` so the queue is discoverable at session
  start. Nothing is ever written without an explicit accept, so the
  "writes are confirmed, never silent" contract holds even though
  capture is automatic. Extraction is the symmetric *capture* half of
  the self-improving loop (consolidation being the *curate* half): a
  cheap, no-LLM, conservative heuristic that runs without blocking the
  turn-end hook, deduped against the queue and capped at `max_pending`
  (default 20). The queue + review surface are deliberately
  generation-agnostic, so an LLM-backed pass (`consolidate
  --from-transcript`) can populate the same queue later.

### Fixed

- **`_fsutil.atomic_write_bytes` docstring accuracy.** The module and
  function docstrings described the chmod-after-rename `mode` parameter
  as the path used by the config / `.gitignore` / JSON-export writers.
  In fact no caller passes `mode`: those writers (`config.py`, `init.py`,
  `cli/export.py`, `sync.py`) inherit `NamedTemporaryFile`'s 0o600
  default — strictly safe (owner-only) for these owner-scoped files —
  and `mode` currently has no callers, retained only as a documented
  chmod-after-rename affordance. Corrected the attribution so the doc
  matches the code. Also refreshed a `test_fsutil` docstring that still
  framed the Q29 helper migration as future work.

### Internal

- **Durability test hardening (Q29 follow-up).** Added direct coverage
  for two previously-untested branches of the consolidated atomic-write
  path: (1) when `os.fchmod` raises (sandbox filesystems that reject it),
  the suppressed error does not propagate and the defensive post-rename
  `os.chmod` recovers the requested mode; (2) `store._atomic_write_post`
  routes through the full `fsync_file → rename → fsync_dir(parent)`
  ceremony — parity with the existing `episodes` spy test — so a future
  edit that re-inlined the write and dropped the dir-fsync fails loudly
  instead of passing the 0o600/round-trip checks.

## 3.2.2 - 2026-05-28

Internal hardening, tooling, and documentation. No behavioral or API
changes — a safe patch upgrade from 3.2.1.

### Added

- **Pyright as a second type-check gate in CI.** A dedicated
  `typecheck-pyright` job runs `pyright` (standard mode, scoped to the
  package) alongside the existing strict mypy gate, so the two checkers
  cover each other's blind spots. Configuration moved into
  `[tool.pyright]` in `pyproject.toml` (replacing a stray
  `pyrightconfig.json` that pinned the MCP-server runtime venv, which CI
  doesn't build); the optional lazy-imports (sentence-transformers,
  fastembed, numpy, anthropic, openai) carry targeted
  `# pyright: ignore[reportMissingImports]` mirroring the existing mypy
  `ignore_missing_imports` override. `reportUnreachable` is left off
  deliberately: the handlers' defense-in-depth `isinstance` / None
  guards at the MCP-JSON boundary are "unreachable" to the static
  checker but validate untyped runtime input — the same reason the mypy
  config does not enable `warn_unreachable`.

### Changed

- **One definition of the durable-private-write discipline (Q29).**
  `store._atomic_write_post` and `episodes._write_path` were two
  near-identical hand-rolled copies of the tmp + fchmod-before-rename +
  fsync + rename + dir-fsync ceremony. Both now delegate to
  `_fsutil.atomic_write_bytes`, which gained a `mode_before_rename`
  option — `os.fchmod` on the tmp file descriptor before the rename, so
  a 0o600 file is never world-readable at its visible path even for an
  instant (mutually exclusive with the existing chmod-after-rename
  `mode`). Behaviour-preserving: identical privacy guarantee, fsync
  ordering, dir-fsync ceremony, and orphan-tmp cleanup. The two
  remaining bespoke writers stay bespoke for good reason —
  `events._compress_rotating` streams gzip in 64 KB chunks and
  `semantic.flush_persistent_cache` writes a numpy container under a
  flock, so neither is a plain-bytes-in-memory caller. A future fix to
  the write discipline now lands in one place instead of three.

### Fixed

- **Documentation accuracy.** Corrected a `Store.restore` `except`
  comment that attributed the malformed-`created` `ValueError` to a
  non-existent `_load_tombstone_path` symbol — it's raised inline in
  `Store.restore` (Y9-fu3). Refreshed the `docs/ROADMAP.md` "where we
  are" version label (v3.1.0 → v3.2.2), whose body already described the
  3.2.x feature set. Added the missing `unique_silent_miss_memories`
  field to the `curation_pending` rollup list in the plugin skill doc,
  aligning it with `docs/api.md`.

## 3.2.1 - 2026-05-28

### Fixed

- **TOML back-compat shim for `endorsement_debt_ratio_threshold` (T9).**
  3.2.0's T1 rename of the `[behavior] endorsement_debt_ratio_threshold`
  key to `cold_endorsement_ratio_threshold` shipped without an alias,
  so a user upgrading from 3.1.x with the old key in their config
  silently lost the threshold:
  `behavior_raw.get("cold_endorsement_ratio_threshold", 0.0)` fell
  through to the dataclass default and the bucket reverted to the
  strict "explicit == 0" check, dropping the loosened-ratio behaviour
  the user had configured. No warning fired — the misconfiguration
  was invisible. T9 adds a shim inside `load_config` (before
  `BehaviorConfig` construction) covering four cases: (1) old key
  only → populate the new key with the legacy value and emit a
  one-shot DEPRECATION warning naming both keys plus the resolved
  path; (2) new key only → no-op (post-3.2.0 happy path); (3) both
  present → the new key wins and a STRONGER one-shot warning instructs
  deletion rather than rename; (4) neither → no-op, dataclass default
  applies. The one-shot guard is keyed on `(resolved_config_path,
  key_kind)` and mirrors the `_DIVERGENCE_WARNED_ROOTS` discipline
  in `store.py`: a long-lived server that rereads config on signal
  doesn't spam the log, but two distinct configs in the same process
  each get their own warning. Path resolution falls back to the
  unresolved path on `OSError` so the guard still works if the config
  moved between `open()` and warn-time. This supersedes the
  "silently fall back to the default `0.0`" guidance in the 3.2.0
  T1 entry above — the deprecation window is now explicit and
  loud-on-load instead of silent-on-rollup.
- **`Store.restore` under-lock recheck (W7).** Mirror of the W1
  `tombstone()` recheck. Pre-W7 `Store.restore` walked
  `_find_tombstone_path_for_id` and `_find_path_for_id` unlocked,
  then acquired `_locked(tombstone_path)` and called
  `frontmatter.load(tombstone_path)` without a recheck. Two
  restorers of the same id, or a restore racing with a concurrent
  `prune_tombstones`, would both pass the unlocked finds; the loser
  hit a bare `FileNotFoundError` from inside `frontmatter.load` —
  caught by an inline arm and re-raised as `MemoryNotFoundError`,
  but with no symmetric "raced with" message and no recheck for the
  case where the active record was re-created in the restore window
  (silent `_atomic_write_post` clobber). Fix adds two under-lock
  rechecks mirroring W1's discipline: (1) `_id_still_at_path(tombstone_path,
  memory_id)` — the tombstone still carries the expected id (catches
  the parallel-restore / prune race and the extremely-unlikely
  tombstone-stem-reuse edge); (2) `_find_path_for_id(memory_id) is None`
  — defensive against the narrow case where a parallel restore
  unlinked the tombstone AND a separate path re-created an active
  file at the predicted slug-suffix-determined `active_path` between
  the pre-lock active check and the lock acquisition. Either recheck
  failing raises a typed exception (`MemoryNotFoundError` /
  `NotTombstonedError`) with a "raced with concurrent restore [or
  prune]" hint, matching W1's find-time pre-lock fallback message
  shape. The `memory_restore` handler gains an `OSError` arm mirroring
  W1's `memory_remove` so genuine disk-level failures during the
  restore write / unlink (EIO, ENOSPC, EACCES, …) surface as
  structured `ValueError` instead of leaking as bare `OSError`.
- **`config.load_config` first-run default-config write is now atomic
  (Q29).** The first-run writer used
  `config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")`, which
  leaves a truncated TOML on power loss / process kill mid-write;
  the next run would see the malformed config and crash at
  `tomllib.load`, turning a single bad shutdown into a hard-block
  first-run experience. Now routes through `_fsutil.atomic_write_bytes`
  (the F5/F6 helper), same shape as the F5 `init.py` MCP-client-config
  and F6 `sync.py` `.gitignore` migrations: plain bytes payload, no
  special mode bits, no privacy bar — the helper's chmod-after-rename
  posture is the right fit.
- **`bettermemory export -o PATH` is now atomic (Q29).** The CLI
  export writer used `out_path.write_text(text + "\n", encoding="utf-8")`,
  which leaves a truncated JSON on power loss / process kill
  mid-write. For a CLI intended for scripted backup
  (`bettermemory export -o backup.json`), a half-written file is the
  exact failure mode the user is trying to guard against. Now routes
  through `_fsutil.atomic_write_bytes`, completing Queue #29's
  simple-bytes Branch A targets (F5 init.py, F6 sync.py, Q29 config.py,
  Q29 cli/export.py). The remaining deferred sites
  (`_atomic_write_post`, `episodes._write_path`,
  `semantic.flush_persistent_cache`, `events._compress_rotating`)
  all need fchmod-before-rename, which the current helper contract
  doesn't cover.
- **`Store.mark_verified` race shape — attestation overwrite (W8).**
  Mirror of the W2 `Store.update` CAS pattern, applied to the
  verification path. Pre-W8 two agents calling `memory_verify` on the
  same id with disjoint attestations (e.g. agent A spot-checking path
  #1, agent B spot-checking path #2 simultaneously) would silently
  last-write-wins on the `verified_paths` / `verified_commits` /
  `verified_versions` lists, because both `mark_verified` calls read
  the same on-disk snapshot and the second write clobbered the first
  without a CAS check. The REPLACE-not-append semantics on those
  lists (by design — the event log is the audit trail) made the
  silent loss especially nasty: one of the two agents' attestations
  vanished without surface. Post-W8 `Store.mark_verified` gains an
  optional `expected_last_verified_at` snapshot fingerprint plus a
  `check_expected=True` opt-in; under the lock, after the C2 recheck,
  the on-disk `last_verified_at` is compared to the caller's
  snapshot. On mismatch raise `ConcurrentUpdateError` carrying the
  on-disk `updated` (kept uniform with W2's exception contract — the
  caller's rebase action is the same: re-fetch via memory_show and
  retry). The `memory_verify` handler loads its snapshot first and
  opts in; legacy direct-store callers (the web UI verify form, the
  no-arg slide-the-timestamp-forward use cases, the existing tests)
  keep the back-compat `check_expected=False` default. MCP response
  shape on CAS failure exactly mirrors `memory_update`'s W2 stale
  payload: `{"status": "stale", "memory_id": ..., "current_updated":
  ..., "hint": ...}`. Fingerprint choice: `updated` doesn't move on a
  verify (orthogonal to content edits) so checking it would never
  catch the verify-vs-verify race; `last_verified_at` is the field
  that always moves on a successful verify, so it's the cheapest
  correct fingerprint.
- **`bettermemory export -o PATH` raises on a missing parent
  directory again (Y1).** The Q29 atomic-write migration above
  inadvertently changed user-facing semantics: pre-3.2.1 the CLI
  used a bare `out_path.write_text(...)`, which raised
  `FileNotFoundError` when `--output`'s parent directory didn't
  exist; routing through `_fsutil.atomic_write_bytes` (whose
  `mkdir(parents=True, exist_ok=True)` is intentional for
  fresh-install callers like `init.py` under `~/.config` and
  `sync.py` under a fresh sync root) silently created the parent
  tree instead, burying a `bettermemory export -o /typod/path/backup.json`
  backup at an unintended location with no error. A pre-check in
  `_cli_export` restores the 3.2.0 loud-error contract for the
  export caller while leaving the helper's auto-mkdir intact for
  its other callers. A bare filename (`-o backup.json`, parent
  `Path(".")`) still works since the cwd always exists.

### Internal

- **`EndorsementDebtRow` → `ColdEndorsementMemoriesRow` eval-module
  rename (T8).** 3.2.0's T1 renamed the public `endorsement_debt`
  field to `cold_endorsement_memories` across the health surface
  but explicitly deferred the eval-module internal classes
  (`EndorsementDebtRow`, `endorsement_debt_rows`,
  `endorsement_debt_total`) as a separate API. That left drift
  between the renamed public JSON key (already
  `cold_endorsement_memories`) and the still-old internal Python
  identifiers that filled it. T8 closes the drift:
  `EndorsementDebtRow` → `ColdEndorsementMemoriesRow`;
  `EvalReport.endorsement_debt_rows` → `cold_endorsement_memories_rows`;
  `EvalReport.endorsement_debt_total` → `cold_endorsement_memories_total`;
  function-local `debt_rows`/`debt_total` → `cold_rows`/`cold_total`;
  comments/docstrings/render-text header ("Endorsement-debt memories"
  → "Cold-endorsement memories") plus CLI `--min-retrievals` help
  text; `TestEndorsementDebt` → `TestColdEndorsementMemories` and
  `test_endorsement_debt_section` →
  `test_cold_endorsement_memories_section`. Internal-only rename:
  the MCP wire shape and eval JSON output key were already renamed
  by T1; this only touches Python-level identifiers and CLI text.
  No alias — the eval module's only external consumers are its
  tests and the CLI wrapper, both migrated. Remaining
  `endorsement_debt` snake_case sightings are confined to T9's
  back-compat TOML alias in `config.py` and its tests, and
  explanatory DESC-string regression-guard assertions in
  `test_server_v12_features.py` / `test_health.py`.
- **`flock_excl` annotation tightened to `Generator[None, None, None]`
  (F15).** The `@contextlib.contextmanager`-decorated `flock_excl`
  in `_fsutil.py` was typed as `Iterator[None]`, which loses
  `send()` / return-T type information that `Generator[YieldT,
  SendT, ReturnT]` preserves. Explicit three-arg form for Python
  3.11+ compatibility (the single-arg `Generator[T]` shorthand is
  3.13+ only). Purely type-annotation; no runtime impact.
- **`_flock_windows` annotation matches F15 (F15-followup).**
  Completes the F15 pattern sweep. `_flock_windows` is the generator
  helper used via `yield from _flock_windows(...)` inside
  `flock_excl`'s Windows branch; same shape, same fix. `Iterator`
  is now unused in `_fsutil.py` and leaves the imports too. Purely
  type-annotation; no runtime impact.
- **`docs/api.md` documents the W2/W8 stale + W7 race-loss surfaces
  (R3).** Three tools landed structured race-loss / stale responses
  in 3.2.0–3.2.1 that the API contract documented only the happy
  path of. Append a paragraph to each so multi-agent callers learn
  about the `{"status": "stale", ...}` shape on `memory_update`
  (W2) / `memory_verify` (W8) and the structured
  `ValueError("raced with ...")` translation on `memory_restore`
  (W7); W8 also calls out `last_verified_at` as the snapshot
  fingerprint (not `updated`, since verify is orthogonal to content
  edits). Documentation only.
- **`_fsutil.atomic_write_bytes` docstrings refreshed post-Q29
  (Y2).** Module + function docstrings claimed two callsites and a
  forward-looking promise to migrate `_atomic_write_post` +
  `episodes._write_path`; reality at HEAD is that Q29 deferred those
  two privacy-critical writers (they need fchmod-before-rename,
  which the helper doesn't support) and instead migrated
  `config.py`'s default-config writer and `cli/export.py`'s `-o`
  output writer. Switched to caller-shape framing ("plain-bytes
  payload, no privacy-0o600 requirement") so the prose stops
  drifting on every new caller, and rewrote the Q29 caveat to
  reflect what shipped. Docstring only.
- **T1 rename completed in user-facing prose (Y10+Y11+Y12).**
  3.2.0's T1 finished renaming `endorsement_debt` →
  `cold_endorsement_memories` in code, MCP wire keys, DESC strings,
  and the eval render header, but prose docs still taught the old
  branding. Swept `README.md`, `docs/ROADMAP.md`, and `docs/eval.md`
  to the new vocabulary. Historical CHANGELOG / api.md references to
  the old key are preserved on purpose — they document the 3.1.x →
  3.2.0 wire-shape break and the T9 TOML back-compat shim. Docs only.
- **`Raises:` blocks added to `Store.update` / `mark_verified` /
  `tombstone` (Y9).** Symmetric with W7's `Store.restore`.
  Documents the `ConcurrentUpdateError` / `MemoryNotFoundError` /
  `TombstonedError` race-loss paths in structured form so IDE
  introspection matches what the type checker already reads from the
  bodies. Docstring only.
- **Test-scaffold accuracy fixes (Y3+Y6+Y8).** Three small
  test-only fixes batched: `test_store_locking.py`'s `traced_locked`
  annotation upgraded `Iterator[None]` → `Generator[None, None,
  None]` (completes the F15 sweep on the test side);
  `test_concurrency.py`'s multi-process restore test gains a
  closed-set assertion on the three known loser-message shapes
  (the prior version was not a true W7 regression guard); and
  `test_config.py` gains `test_load_config_falsy_old_key_emits_warning`
  pinning that an explicit `endorsement_debt_ratio_threshold = 0.0`
  still fires the deprecation warning and migrates (presence
  triggers, value doesn't), with a corrected docstring on the
  absent-key sibling.
- **Test pinning the BOTH-keys legacy-alias one-shot guard (Y4).**
  `_apply_legacy_endorsement_debt_alias`'s `+both` guard tuple is
  logically distinct from the old-only one; a sibling test loads a
  both-keys TOML three times and asserts exactly one BOTH warning
  fires, guarding against a regression that collapses the suffix or
  cross-suppresses between branches. Test only.
- **Test covering `Store.restore`'s active-recheck branch (Y7).**
  The existing W7 monkeypatch test exercised the tombstone-recheck
  (which fires first) rather than the active-recheck
  (`NotTombstonedError`) branch it claimed to. Added a dedicated
  test that patches `_find_path_for_id` only on the under-lock call
  site so the active-recheck runs deterministically, and tightened
  the original test to the exception it actually raises
  (`MemoryNotFoundError`). Test only.
- **`eval.py` comment justifying the ENDORSEMENT vs
  COLD_ENDORSEMENT name choice (Y5).** Expanded the
  literal-duplication comment to also explain why the eval-side
  identifier omits the `cold_` bucket prefix that health carries:
  the eval output nests under `cold_endorsement_memories`, so the
  bucket scope is already supplied and the parameter prefix would be
  redundant — whereas health's flat-kwarg path needs it.
  Comment only.
- **Tests pinning cross-branch independence of the alias guards
  (Y4-fu).** Y4 pinned same-branch repeat-suppression but reset the
  guard at entry, leaving the cross-branch transition (old-only on
  path P, then both-keys on the same path with no reset) untested.
  Added both directions, asserting each branch's warning fires fresh
  and both guard tuples are present, so a regression that collapsed
  the `+both` suffix into a shared tuple can't cross-suppress and
  still pass. Test only.
- **Multi-process W7 race test rendezvous (Y6-fu).** Added a
  file-based rendezvous (per-worker ready-marker + a shared
  go-marker the parent touches once all workers are ready) so all
  losers route through the W7 under-lock recheck and carry the
  "raced with" hint, letting the strict assertion the threaded
  variant already uses hold for the multi-process variant too —
  upgrading Y6's pragmatic closed-set concession. Test only.

## 3.2.0 - 2026-05-27

The **multi-agent hardening + audit-turn semantics** release: a
10-commit campaign hardening bettermemory for production-grade
concurrent access by multiple Claude Code sub-agents, plus a
telemetry overhaul that makes the dashboard counts accurate and
individually-actionable. Tool count goes from 22 to 23 (new
`memory_acknowledge_miss`); on-disk format unchanged
(SCHEMA_VERSION stays at 1, additive-only).

### Changed

- **Silent-miss rollup gains 4-stage filter (T4).** The 3-stage
  filter (cutoff + tombstone + dedup, post-T2/T3) now includes an
  ack-filter step in both `compute_health` and `curation_counts`.
  Pre-T4: `silent_miss_cutoff` was the only escape; tombstoning the
  top-hit memory was the only per-event drop. Post-T4: the rollup
  also drops events whose `event_id` appears in any `miss_ack` event
  in the log. Both numerator (`miss_total`) and unique-memory count
  (`unique_miss_memories`) honour the new filter; the denominator
  (`audited_total`) stays at "audits the hook ran" because the
  audit itself ran legitimately. `_silent_miss_stats` gains an
  optional `acknowledged_event_ids` parameter (default empty set,
  preserves legacy behaviour for callers that haven't been
  updated). Event log entries without an `event_id` field continue
  to read cleanly — the rollup degrades to event-count-only for
  those legacy entries.
- **`search_miss` event shape gains stable `event_id`.** Every new
  `search_miss` written via `search_miss_fields` now carries a
  per-event ULID under the top-level `event_id` field. Pure
  additive — legacy consumers reading the event by `kind` and
  field-set see no break. The id is the handle
  `memory_acknowledge_miss` keys on; pre-T4 events lack it and
  fall through the ack-filter (the bulk cutoff remains the only
  escape for those).
- **`endorsement_debt` → `cold_endorsement_memories` (T1).** Renamed
  the field on `HealthReport`, the dataclass (`EndorsementDebt` →
  `ColdEndorsementMemories`), the `curation_counts` dict key, the
  parameter prefix (`endorsement_debt_min_retrievals` →
  `cold_endorsement_min_retrievals`,
  `endorsement_debt_ratio_threshold` →
  `cold_endorsement_ratio_threshold`), the `BehaviorConfig` field
  and TOML key (`[behavior] endorsement_debt_ratio_threshold` →
  `cold_endorsement_ratio_threshold`), and the recommendation
  kind (`cleanup_endorsement_debt` → `cleanup_cold_endorsements`).
  DESC strings for `memory_health` and `memory_scope_overview`
  now describe the field accurately as "memories with `retrieval_count
  >= N` AND zero explicit applies — per-memory count, NOT per-turn."
  The old name suggested "9 turns where retrieval shaped the reply
  without `record_use`" but the actual semantic is "9 distinct
  memories that crossed the retrieval floor with zero explicit
  endorsement" — a per-memory state, so the name now matches.
  Wire-shape: `memory_health` response replaces the
  `endorsement_debt` key with `cold_endorsement_memories` (same
  payload shape: `{min_retrievals, total, rows}`);
  `memory_scope_overview.curation_pending` and the `curation_hint`
  block emitted on `memory_write` likewise rename the key. Pure
  rename — no alias — since the MCP responses are read by Claude
  (which gets fresh tool descriptions per session) and no
  persisted state references the old key. TOML configs carrying the
  old `endorsement_debt_ratio_threshold` key will silently fall
  back to the default `0.0` after upgrade; rename to
  `cold_endorsement_ratio_threshold` if a non-default value
  matters. **Note (corrected in 3.2.1):** the silent-fallback
  behaviour described in the preceding two sentences no longer
  applies. T9 adds a back-compat shim in `config.load_config` that
  honours the legacy key (mapping it to `cold_endorsement_ratio_threshold`
  when the new key is absent) and emits a one-shot deprecation
  warning naming both keys plus the resolved config path. See the
  T9 entry under Unreleased / 3.2.1 for the full four-case behaviour.

### Added

- **`memory_acknowledge_miss(event_id, reason)` MCP tool (T4).**
  Per-event escape hatch for silent-miss false positives. Tool count
  is now 23 (19 `memory_*` + 4 `episode_*`). The bulk
  `bettermemory consolidate --acknowledge-misses-before <ts>`
  command still exists and wipes EVERY pre-cutoff `search_miss` in
  one stroke; this tool surgically targets one event so legitimate
  misses keep counting. Each `search_miss` event now carries a
  stable per-event ULID (`event_id` field stamped at emission time
  by `search_miss_fields`); the model reads ids off
  `memory_health.recent_silent_misses` (new bounded inline list,
  cap 10, newest-first) and calls `memory_acknowledge_miss(event_id,
  reason)` to acknowledge one. Emits one `miss_ack` event referencing
  the original `event_id`; subsequent rollups drop the acked event
  from both `miss_total` and `unique_miss_memories`. Idempotent —
  a second ack for the same `event_id` returns the success shape
  without emitting a duplicate. Returns `{"status": "not_found",
  ...}` for unknown ids (including legacy `search_miss` events
  written before T4 added the field — those remain ack-able only
  through the bulk cutoff) and `{"status": "wrong_kind", ...}` when
  the id points at a non-`search_miss` event. `reason` is required
  (≥ 8 chars after stripping whitespace) so the audit trail carries
  signal. Routes through the same `Recorder` / `_advance_turn` /
  session-state plumbing every other handler uses. Existing event
  log lines without the new `event_id` field continue to read
  cleanly — the rollup degrades to event-count-only behaviour for
  those entries.
- **`HealthReport.recent_silent_misses` (T4).** Bounded inline list
  (cap 10, newest-first) on the `memory_health` response payload.
  Each entry: `{event_id, top_hit_id, query_preview, ts}` — the
  triage surface the model reads to discover ack-able event ids.
  Filtered against the same cutoff / tombstone / ack set the rollup
  uses, so a non-empty `recent_silent_misses` always pairs with a
  non-zero `miss_total` (modulo the legacy events that lack an
  `event_id`).
- **`silent_misses.unique_miss_memories` + `curation_pending.unique_silent_miss_memories`.**
  Additive `int` field on `SilentMissStats` and matching key on the
  `curation_counts` dict. Dedups in-window `search_miss` events by
  their top-hit `memory_id` so the rollup distinguishes "9 events
  hammering 1 mis-tagged memory" from "9 distinct unretrieved
  memories" (the existing `miss_total` / `silent_misses` event count
  is preserved unchanged for back-compat — both surfaces now ship).
  Wire-shape: `silent_misses` payload gains `unique_miss_memories`;
  `memory_scope_overview.curation_pending` gains
  `unique_silent_miss_memories`. `memory_scope_overview` and
  `memory_health` tool descriptions updated to enumerate the new
  field.
- **`_fsutil.atomic_write_bytes` helper (F5/F6).** New centralised
  tmp+fsync+rename+fsync_dir primitive in `_fsutil.py`. Used by
  `init.py:333` (user MCP client config write — e.g. `~/.claude.json`)
  and `sync.py:313` (`.gitignore` write). The existing per-module
  atomic writers (`store._atomic_write_post`, `episodes._write_path`,
  `events` rotation) are unchanged this release — they keep their
  fchmod-before-rename ceremony for private memory bodies; the new
  helper uses chmod-after-rename since the new callsites carry
  files that are explicitly NOT privacy-sensitive (the MCP config
  is read by every other MCP client; `.gitignore` is committed to
  git).
- **`Store.__post_init__` index-divergence warning (S4).** When the
  Store opens a root it compares `index.status(root).indexed_count`
  to the live `.md` count from a single `iterdir()` walk. If they
  diverge, emit a one-shot WARNING per resolved root naming both
  counts and pointing at `bettermemory reindex` for recovery. The
  check is idempotent per process (module-level
  `_DIVERGENCE_WARNED_ROOTS: set[Path]`). Three shapes recognised:
  missing index file, corrupt index (`status["corrupt"]`), and
  count mismatch. Catches the architectural failure mode where an
  external editor, a `sync pull`, or a sub-agent's generic `Write`
  tool wrote an `.md` directly bypassing the Store hooks — pre-S4
  `memory_search` would rank against stale candidate ids silently.
  README's Performance section gains an "Index consistency"
  subsection documenting the canonical writer set and the recovery
  flow.
- **`Episode.is_floor` field + `EpisodeStore.write_floor` (E2).**
  New tag-shaped episode kind that carries an `origin` but no
  takeaway content. Pure additive on the frontmatter (default
  `false` is omitted from YAML so existing episodes serialise
  identically). Written exclusively by `episode_handoff` at handler
  entry to seed a journal floor for the current `session_id`
  before recording the handoff event — see the Fixed entry below.

### Fixed

- **Silent-miss rollup drops tombstone-targeted events (T3).** The
  `silent_miss` aggregation in `compute_health` and `curation_counts`
  now drops events whose top-hit `memory_id` is in `tombstoned_ids` —
  once the memory is gone, the miss is no longer actionable. Other
  rollups (`dead_weight`, `heavily_used`, `orphan_use_events`)
  already cross-reference against the tombstone set at the same
  call site; the silent-miss bucket was the holdout, so a memory
  tombstoned after accruing miss events kept inflating the count
  forever. Applies to both `miss_total` and the new
  `unique_miss_memories` counter. `scope_overview` now threads the
  store's tombstone set through `curation_counts` so the session-start
  view agrees with the deep health view.
- **`store.tombstone()` under-lock recheck (W1).** The mutator was the
  one missing the `_id_still_at_path` recheck under `_locked(path)`
  that `update()` / `mark_verified()` use. Two agents calling
  `tombstone(id)` concurrently would both find the same path; agent A
  won the lock, tombstoned, unlinked, released; agent B then acquired
  the now-stale lock and `frontmatter.load(path)` raised a bare
  `FileNotFoundError` that escaped through the handler as a 500-shaped
  MCP error for what should be a clean "already tombstoned" semantic.
  The under-lock recheck now raises `TombstonedError` with a message
  that mirrors the find-time pre-lock fallback.
- **`memory_remove` catches `OSError` (W5).** Independent of the W1
  race fix, bare `OSError` from genuine disk-level failures during
  the tombstone write (EIO mid-write, ENOSPC during the atomic
  rename, EACCES on the unlink, …) still leaked through the handler
  to the MCP boundary. Converted to `ValueError` with a descriptive
  message; the original `OSError` is preserved on `__cause__` for
  diagnostics.
- **`Store.update` optimistic-concurrency CAS (W2).** Pre-W2,
  `memory_update` was silent last-write-wins: two agents concurrently
  editing the same memory with disjoint changes would both load the
  same snapshot via `load_one`, serialise on the per-file `_locked`
  flock, and whichever wrote second silently clobbered the first
  writer's edit. The existing `_id_still_at_path` C2 recheck only
  defended against tombstone-vs-update; the body itself was never
  compared against the snapshot the caller built their edit on.
  `Store.update` now re-loads the current Memory under the lock and
  compares its `updated` to the caller's `memory.updated` (the
  snapshot timestamp). On mismatch it raises a new
  `ConcurrentUpdateError` carrying the on-disk `updated`. The
  `memory_update` handler catches it and returns a structured
  `status="stale"` payload mirroring the other soft-refusal shapes
  in `write.py` (`scope_mismatch`, `transient_warning`, …): the
  caller re-fetches via `memory_show` and retries the edit on top of
  the current snapshot. Single-writer happy-path callers
  (`load_one` → edit → `update`) see no behavior change. A
  `force=True` escape hatch on `Store.update` lets in-process
  reconciliation tooling bypass the CAS; it is NOT exposed through
  the MCP handler boundary.
- **`yaml.YAMLError` translated to `ValueError` at the `_frontmatter`
  parser boundary (F1).** PyYAML's `YAMLError` does not inherit from
  `ValueError`, so the project-wide `except (ValueError, KeyError,
  OSError)` defensive tuple in `store.py` (verified at lines 177,
  193, 225, 522, 545, 629, 642, 760, 1043, 1314 at HEAD pre-fix)
  silently failed to catch parse errors. One malformed `.md` file
  (sync-pull truncation, hand-edit typo, partial-write recovery
  leaving a torn `<!--` opener) raised `yaml.scanner.ScannerError`
  straight out of `load_all` / `iter_active` / `_load_path` and
  killed `memory_search` / `memory_list` / `memory_health` /
  `memory_scope_overview`. `_frontmatter.loads` now catches
  `yaml.YAMLError` and re-raises as `ValueError(f"malformed YAML:
  {exc}") from exc` (`__cause__` preserves the original);
  `_frontmatter.load(path)` prepends the file path to the message.
  The defensive tuples downstream now catch the parse failure
  cleanly. Also tightened the only other YAML-error-aware catch in
  the repo (`ingest.py:243`) from a blanket `except Exception` to
  the same `(ValueError, KeyError, OSError)` shape and refreshed
  the comment.
- **Unguarded `frontmatter.load` in tombstone-fallback branches
  (F2).** Three call sites (`load_one`, `mark_verified`,
  `tombstone` at `store.py:231-237, 452-458, 520-524` pre-fix)
  iterated tombstones with `post = frontmatter.load(tpath)` and
  no surrounding `try/except`. A truncated tombstone (sync-pull
  race), a peer prune unlinking the file between
  `_iter_tombstone_paths()` and the read, or a malformed YAML body
  crashed `memory_show`, `memory_record_use`, and the hook's
  attribution loop. Each iteration body now wraps in `try/except
  (FileNotFoundError, ValueError, KeyError, OSError,
  yaml.YAMLError): continue` — same discipline the existing
  `load_tombstones` reader at `store.py:583` already used.
- **`prune_old_sessions` unlinks per-session lockfile (E1, ≡
  carryover A3-13).** Pre-E1, `episodes.py:149` wrote
  `.session-<id>.lock` on first write to a fresh `session_id` and
  the rmtree branch (`:443`) and rmdir branch (`:382`) deliberately
  did not unlink it (per-inode-identity race documented inline).
  With each `/loop` tick running in a fresh process under a fresh
  `session_id`, the lockfiles accumulated unbounded across tick
  count: after N≈10⁵ the `iterdir(episodes_dir)` over the orphans
  dominated handoff latency (every prune pass, every
  `iter_session_ids` call, every `episode_search` /
  `episode_promote` walk hit it). Both prune branches now also
  `unlink(missing_ok=True)` and `fsync_dir(episodes_dir)` after
  the rmtree/rmdir, while still holding the per-session flock —
  safe for past-TTL sessions because the per-inode race the comment
  warned about only matters with live writers, and a 30-day-stale
  `session_id` has no live writer. The peer-prune race (when
  `fresh_mtime is None` because a peer already wiped the
  `session_dir` during our unlocked stat → flock acquisition
  window) is now also handled. A new `_cleanup_orphan_lockfiles`
  sweep runs at the end of `prune_old_sessions` to mop up pre-fix
  orphans on legacy stores; it is bounded (single `iterdir` +
  per-file stat, skips lockfiles whose `session_dir` still exists).
- **`init.py:333` writes user MCP client config atomically (F5).**
  Pre-F5, the JSON write for the user's MCP client config (e.g.
  `~/.claude.json`) used a plain `target_path.write_text(...)`. A
  power loss or process kill mid-write would truncate the user's
  ENTIRE Claude MCP config (every MCP server they had configured,
  not just bettermemory). Bypassed the `_atomic_write_post`
  discipline the rest of the codebase had internalised. Now routes
  through `_fsutil.atomic_write_bytes`.
- **`sync.init:313` writes `.gitignore` atomically (F6).** Pre-F6,
  the `.gitignore` write used `gitignore.write_text(...)`. A
  truncated gitignore would let the next `sync push` commit event
  logs and lockfiles to the remote — a real privacy regression
  vector. Now routes through `_fsutil.atomic_write_bytes`.
- **`episode_handoff` writes session-tag floor episode before
  recording the handoff event (E2).** Pre-E2, tick T calling
  `episode_handoff` then crashing before `episode_write` left T
  with an `episode_handoff` event in the event log but zero
  journal files under `episodes/<T-session-id>/`. T+1's handoff
  resolved T's `session_id` via the event log, called
  `list_by_session(T-session-id)` → empty, hit the zero-episode
  branch in `handlers/episode_handoff.py:234-242` (strict-worktree
  filter: only adopt zero-episode candidates when caller's worktree
  is None), and in a real worktree silently walked past T and
  adopted T-1 — dropping T's full history. The exact "loop↔memory
  boundary buggy" failure mode reported in the campaign brief.
  The handler now writes a floor episode at the very top of the
  handoff path (BEFORE recording the handoff event, so even crashes
  between the floor write and the event record leave a journal
  floor on disk that future `prune_old_sessions` will TTL-clean at
  30 days). Floors are filtered out of `episode_search` candidate
  sets and `episode_promote` rejects them with a clean error.
  Floor-only adoption (when T crashed before any real
  `episode_write`) surfaces `note: "Prior session crashed before
  writing a takeaway."` so the model can distinguish a clean
  crash from a benign no-write tick.

### Internal

## 3.1.0 - 2026-05-27

The **loop story**: a sibling tier for journal-shaped run-state, plus
five surface improvements that close adjacent audit gaps. Tool count
goes from 18 to 22; on-disk format unchanged (SCHEMA_VERSION stays at
1, additive-only).

### Added — episode tier (sibling to memory)

- **`episode_write(body, takeaway?, scopes?)`.** Journal-shaped writes
  for run-state and iteration takeaways. The `durability` gate
  (`TRANSIENT_PHRASE_MARKERS`) that rejects state-shaped content on
  `memory_write` doesn't apply here — episodes are the alternative
  home transient content used to lack. Storage at
  `<root>/episodes/<session_id>/<ulid>.md`, 30-day TTL, pruned on
  each write. Invisible to `memory_search` / `memory_health` /
  `memory_list`.
- **`episode_handoff(prior_session_id?, max_episodes?)`.** Read the
  most-recent N takeaways from a prior session in this worktree.
  Auto-resolves the session via the event-log boundary when
  `prior_session_id` is omitted. Designed as the first MCP call at
  `/loop` iteration entry.
- **`episode_search(scopes?, parent_session_id?, since?, max_results?)`.**
  Cross-session lookup. Not ranked — episodes are chronological,
  filtered by scope intersection / session id / ISO timestamp.
- **`episode_promote(episode_id, scopes, category?, ..., use_body?)`.**
  Distill a takeaway into a durable memory via the standard
  `memory_write` path (full durability gate fires). Deletes the
  source episode on commit; leaves it intact on any non-committed
  status so the caller can adjust and retry.
- **`bettermemory episodes list | prune` CLI.** Mirrors
  `bettermemory tombstones` in shape — offline inspection and a
  manual TTL-based cleanup pass.

### Added — memory_search proactive surface

- **`memory_search(since_prior_session=True)` filter.** Restricts
  candidates to memories `updated` at or after the prior-session
  boundary (`find_prior_session_boundary` against the recorder's
  session_id). Loop entry can now ask "what's changed in this
  worktree since last time I was here?" without scanning. Empty
  return when no prior session exists — caller distinguishes
  "nothing new" from "no baseline" via
  `curation_pending_new_since_last_session`.
- **`depends_on_resolved` on hits.** When a hit's memory carries
  `MemoryLink(type="depends_on", ...)` links, the targets'
  summaries (and link notes) are inlined automatically. Bounded:
  3 per hit, 10 total. Closes the "graph in the schema, retrieval
  ignores it" gap that's been open since 2.x.

### Added — proactive curation surface

- **`HealthReport.recommendations`.** Distills the bucket rollups
  (dead_weight, contradicted, endorsement_debt, drifted, rare_scopes)
  into actionable one-line suggestions with `{kind, summary, action,
  count, memory_ids}` shape. Closed enum `RECOMMENDATION_KINDS` so a
  consumer can switch over them. Size-driven kinds fire at 3+; per-row
  kinds at 1+.
- **Inline `curation_hint` on `memory_write` responses.** One-shot per
  session: when `dead + drifted + endorsement_debt >= threshold`
  (configurable, default 5), the first successful write inlines a
  one-line nudge. New `curation_hint_threshold` and
  `curation_hint_enabled` config knobs; 0 / false disables.
- **`endorsement_debt_ratio_threshold` config knob.** Default 0.0
  preserves the existing strict "zero explicit applies" rule. Setting
  > 0 also flags memories whose explicit/total-applied ratio falls
  below the threshold — catches the "1 explicit endorsement out of 50
  auto" case the binary check misses.
- **`recently_removed_in_worktree` on `memory_scope_overview`.** Count
  of tombstones removed in the last 7 days, filtered by
  `origin.worktree_root` under `auto_scope=True`. Hint when the model
  is about to re-cover ground it already explicitly trimmed.

### Fixed

The loops-phase-1 surface ran two audit drains and a post-merge polish
pass before this release; the fixes below catch the airtight-blocking
items the audit cycle surfaced. Each line names the user-visible win;
the commit hash carries the implementation.

**Concurrency / multi-MCP correctness:**

- Empty-dir prune branch now serialised against concurrent
  `episode_write` under the per-session flock, with a recheck after
  acquisition so a sibling worktree can't lose its just-written
  episode to a stale prune decision [cef3e23].
- `_delete_source_episode` (called from `episode_promote`) holds the
  per-session flock for the unlink + empty-dir rmdir window, so a
  concurrent `episode_write` to the same session can't observe a
  half-deleted directory tree [5910a39].
- `prune_old_sessions` past-cutoff branch acquires the same
  per-session flock and re-checks the prune predicate after lock
  acquisition, closing a TOCTOU window where a concurrent
  `episode_write` could land an episode mid-prune [a4565b8].

**Durability (POSIX fsync discipline):**

- `Episode._write_path` now uses `fsync_file` + `fsync_dir` on the
  atomic rename, matching the memory writer's crash-durability
  guarantees [7017b2c].
- First write of a fresh event log calls `fsync_dir` on the parent so
  an OS-level crash between create-and-write doesn't leave the dirent
  in flight [0ea5094].
- Episode prune (rmdir + rmtree), the first write to a brand-new
  `session_dir`, and `_delete_source_episode` all now `fsync_dir` the
  parent after dirent-mutating operations so the directory state
  survives a crash on the same footing as the file contents
  [36fc35f].
- `semantic.flush_persistent_cache` chmods the temp file *before*
  atomic-rename so a process crash between rename and chmod can't
  leave the cache world-readable [d77217b].

**Scoping / privacy:**

- `episode_search` and `episode_handoff` now honor `disabled_scopes`,
  so the same session-local opt-out users already trust on the memory
  side applies to the episode tier [b982ad0].
- `episode_handoff` filters `prior_session_id` candidates by the
  caller's worktree before adoption, so an episode-bearing prior
  session from a sibling worktree isn't accidentally adopted
  [2988fff].
- `episode_handoff` applies the same worktree filter to the
  zero-episode candidate-adoption path, closing the gap where a
  prior session with no episodes could still be adopted across
  worktrees [1a77999].
- `memory_search` re-applies the active scope filter to the FTS
  prefilter result before depends_on auto-pull, so a graph edge
  can't drag in a target from a disabled scope [bf92912].
- depends_on auto-pull targeted-load applies scope and origin
  filters to targets fetched outside the FTS prefilter set, so the
  graph-edge expansion respects the same isolation as direct hits
  [00ac037].

**Size caps / data integrity:**

- `episode_write` enforces `max_content_bytes` on the body and
  returns a structured rejection, so an oversized journal entry
  can't silently truncate at the storage layer [a60bce2].
- `episode_write` enforces `max_takeaway_bytes` on the takeaway
  field separately from the body so a giant takeaway can't silently
  drop on commit [4d36967].
- `Episode.scopes` and `Memory.scopes` both cap at 64 entries on
  load, preventing pathological scope lists from blowing up FTS
  prefilter cost or scope-overview pagination [e928b33].

**Search correctness:**

- `memory_search(since_prior_session=True)` bypasses the FTS
  prefilter so candidates that genuinely matter after the prior
  session boundary aren't dropped by a pre-boundary token-frequency
  cutoff [3bd27dc].
- `since_prior_session` boundary is now strict-after — the
  prior-session boundary memory itself is excluded from results, so
  the count aligns with `curation_pending_new_since_last_session`'s
  delta semantics [ffad750].
- `endorsement_debt_ratio_threshold` config knob now threads through
  every callsite (`compute_health`, `curation_counts`, the CLI), so
  setting it once actually changes the rollups everywhere they're
  surfaced [3db9cfc].
- `episode_search(max_results=N)` returns the most-recent N
  episodes instead of the oldest N, matching the loop-iteration
  intent where recent run-state is the relevant slice [3d77bac].

**Tests pinning previously-implicit invariants:**

- `recently_removed_in_worktree` worktree filter pinned by an
  explicit test so a future refactor of `memory_scope_overview`
  can't quietly drop the per-worktree slicing [0c131b9].
- `max_total` cross-hit cap for depends_on auto-pull pinned in
  `00ac037` so a graph-heavy memory can't blow the global budget
  even when each hit stays under its per-hit cap.

**Documentation accuracy (model-facing):**

- Public API docs sync for the episode tier and the curation
  surface so a consumer reading `docs/api.md` gets shape-accurate
  return values for every tool in the 22-tool surface [1b41b51].
- Handler DESC strings synced across `memory_search`,
  `memory_health`, `memory_scope_overview`, and `memory_write` so
  the FastMCP-published descriptions match the implementation's
  field enumeration [053ab9d].
- `docs/api.md` sweep: `episode_search` shape, `memory_show` full
  field enumeration, `memory_health` timestamp surface, and
  `episode_handoff` filter semantics all corrected so the page
  ships as the canonical reference [8c072f9].
- DESC drift cleanup: `memory_audit_turn` predicate language,
  `since_prior_session` wording, and the four episode-tier DESCs
  all reworded for accuracy against the implementation [a2076d8].

### Internal

- New `Episode` Pydantic model + `EpisodeStore` (lazy directory
  creation, atomic frontmatter writes, traversal-safe `session_id`
  validation, TTL-based prune that exempts the active session).
- `_TOOL_REF_RE` in `tests/test_prompts.py` broadened to cover both
  `memory_*` and `episode_*` families so SKILL.md / system prompt
  parity catches drift in either tool group.
- FastMCP `instructions` block carries a one-line loop pointer
  (`episode_handoff` at entry, `episode_write(takeaway)` at exit)
  under the 1700-char ceiling.
- `plugin/skills/bettermemory/SKILL.md` gains a full **Episodes:
  the sibling tier for run-state** section with the loop-iteration
  pattern, storage layout, and the `episode_promote` lifecycle note.
- README, `docs/api.md`, `docs/ROADMAP.md`, `CONTRIBUTING.md`, and
  `plugin/README.md` all updated to the new 22-tool count with the
  `memory_*` + `episode_*` split called out.
- 50+ new tests across `test_episodes.py`, `test_server.py`,
  `test_health.py`, `test_cli_smoke.py`, `test_config.py`,
  `test_direct_imports.py`, `test_prompts.py`, `test_eval.py`.

## 3.0.2 - 2026-05-26

Post-3.0.1 hotfix. Closes the Windows-matrix mypy gaps + flaky
perf-test threshold that CI surfaced on the 3.0.1 merge to main.
No on-disk format changes, no public API changes.

### Fixed — Windows py3.14 mypy

- **`_fsutil.py:314,337` msvcrt.locking ignore compound.** py3.14
  typeshed types `msvcrt.locking` + `LK_NBLCK` + `LK_UNLCK`, so
  the prior `# type: ignore[attr-defined]` was reported as
  unused on the windows-latest slot. Switched to
  `[attr-defined,unused-ignore]` so the same comment satisfies
  both sides of the matrix: Unix mypy still suppresses the
  `attr-defined` (msvcrt absent from typeshed on POSIX) and
  Windows mypy stays quiet about the now-unused suppression.
- **`store.py:1235` `os.fchmod` platform guard.** `os.fchmod` is
  absent from Windows typeshed (POSIX-only call). Wrapped the
  call in `if sys.platform != "win32":` so mypy narrows it out
  on the Windows slot. Runtime behaviour unchanged — the prior
  `contextlib.suppress(OSError)` already swallowed the
  AttributeError-via-non-existence case at execution time.

### Fixed — flaky perf-test threshold

- **`test_already_recorded_pending_ids_early_exits_on_old_events`
  threshold widened 100ms → 500ms.** Observed 151ms on a shared
  ubuntu-latest runner during the 3.0.1 release run, well within
  optimisation-working territory (the broken-case full forward
  scan over 10k events runs in seconds, not hundreds of
  milliseconds). The bound still firmly detects O(N) regression
  while absorbing shared-runner contention. Threshold rationale
  also pinned in the docstring with the 151ms observation logged
  so a future tightening pass has the context.

## 3.0.1 - 2026-05-26

Post-3.0.0 audit-loop follow-up. Mostly low-impact: one
defense-in-depth security tightening, one CI-gate repair, and a
sweep of test-rigour pins closing the same class hazard
(asymmetric whitelist coverage) the prior audit-loop already
worked through. No on-disk format changes, no public API changes.

### Security — Defense-in-depth

- **`llm.py` transcript fence scan symmetrised across all four
  nonce-anchored markers.** Pre-`520bb6d` the transcript scan
  checked only `{trn_end, mem_end}`; the body and excerpt scans
  already checked all four (`{trn_end, trn_begin, mem_end,
  mem_begin}`). The random per-prompt nonce already makes
  hard-coding a marker infeasible, so this is defense-in-depth
  rather than a live break — but the three scan sites now line
  up on the same predicate. Pinned by parametrised regression
  tests covering both `<<<BM_TRANSCRIPT_*_BEGIN>>>` and
  `<<<BM_MEMORY_*_BEGIN>>>` in the transcript body.

### Fixed — CI gate

- **Wider CI gate (ruff lint / ruff format / mypy strict) green
  at HEAD.** The post-2.7.3 audit-loop converged with pytest
  green but the wider gate was never run during the loop; five
  distinct gaps surfaced at the 3.0.0 release boundary and were
  closed in `25d2dea`: an obsolete `# type: ignore[import-not-
  found]` on `import msvcrt` in `_fsutil.py` (typeshed's
  cross-platform stub made it unused), two mid-file imports
  flagged E402 (`store.py` and `test_concurrency.py`), four
  `test_llm.py` fence-injection tests monkeypatching
  `bettermemory.llm.secrets.token_hex` directly on the module
  namespace (rewritten to `pytest.MonkeyPatch.setattr` with
  dotted-path form — mypy-clean and removes manual cleanup
  boilerplate), and 21 files needing `ruff format` reflow.
  Behaviour unchanged.

### Fixed — Test rigour (asymmetric whitelist coverage)

- **Closed-protocol whitelists pinned at every member.** Eight
  test commits closed the same class hazard the prior
  audit-loop worked through: a closed-protocol frozenset
  (`_PLACEHOLDER_PATHS`, `_INDEX_FILENAMES`,
  `_RETRIEVAL_EVENT_KINDS`, `_same_origin` loopback hosts,
  `_VERDICT_RAISE_STATUSES`, plus the four nonce-anchored
  llm.py fence markers) had only partial regression coverage,
  so a stray deletion would silently re-introduce false
  positives or 403 users without CI noticing. Representative
  pins: all 8 members of `_PLACEHOLDER_PATHS` (`55431ae`); the
  `INDEX.md` clause of `_INDEX_FILENAMES` (`cc2345b`); the
  `list` clause of `_RETRIEVAL_EVENT_KINDS` (`e360058`); all
  three loopback hosts (`localhost`, `127.0.0.1`, `::1`) in
  `_same_origin` (`f4dd2a4`); `{never, stale}` membership of
  the `staleness_verdict` raise-gate across `verify.py` and
  `_response.py` (`0d56a50`, paired with a `_VERDICT_RAISE_
  STATUSES` DRY extraction); and BEGIN+END fence symmetry
  across all three scan sites in `llm.py` — excerpt
  (`40341a2`), transcript-end (`a14dd6b`), and body
  (`93838db`). Each pin uses a hardcoded expected-membership
  tuple plus a separate guard against additions to the source
  set, so the assertion keeps firing on a deleted member
  rather than being silently skipped. All verified by negative
  control.

### Documentation

- **`{search, show, list}` retrieval-event drift sweep
  completed.** `520bb6d` updated one site
  (`_count_recent_retrievals`); a fresh-eyes pass caught
  five parallel sites in `audit.py` with the same {search,
  show} drift (list omitted) plus one stale handler pointer
  invalidated by the post-`582a5d2` handler decomposition
  (`9437d1c`). The two user-facing copies in `docs/api.md` and
  `docs/eval.md` were then mirrored to match (`a24eade`).
  Where prose was abstractable, the rewrite anchors to the
  existing `_RETRIEVAL_EVENT_KINDS` constant so a future
  addition to the frozenset doesn't re-fork the docs.
- **2.x → 3.x live-contract framing swept across user-facing
  docs.** 3.0.0 shipped at `1322b53` but `docs/api.md`,
  `CONTRIBUTING.md`, `SECURITY.md`, and `docs/ROADMAP.md`
  still advertised 2.x as the live contract (`948e04e`). The
  `memory_search.mode` default in `CONTRIBUTING.md` was also
  corrected from "keyword (new in 2.0)" to "hybrid (since
  2.6.8)" since that flip already shipped. `README.md`
  "Further reading" ROADMAP summary similarly framed
  `consolidate --acknowledge-misses-before` as an "unreleased
  follow-up" though it shipped in 3.0.0; reframed to
  declarative past tense under a 3.0.0 milestone with the
  companion `bettermemory.server` re-export trim noted as the
  second 3.0.0 line item (`6a980a6`).

## 3.0.0 - 2026-05-25

**Companion escape hatch for the 2.7.3 cwd-suppression fix.** v2.7.3
stopped emitting same-repo silent-miss false positives going forward,
but the events log still carried the batch of pre-fix `search_miss` /
`turn_audited` rows that skew the miss-rate rollup. This release adds
an additive CLI cutoff so that batch can be invalidated without
rewriting the log.

### Added — Curation

- **`bettermemory consolidate --acknowledge-misses-before <ISO_TS>`**
  writes one `silent_miss_cutoff` event with `cutoff_ts=<ISO_TS>`
  through the shared `Recorder`. `compute_health` and
  `curation_counts` honor the latest `cutoff_ts` they observe and
  drop any `turn_audited` / `search_miss` events earlier than it —
  filtering both numerator and denominator so the miss-rate metric
  doesn't skew low or high. Mirrors `--acknowledge-debt`'s surface:
  always commits, no `--apply` gate (events are additive and a
  misapplied cutoff can be superseded by a later one), text and JSON
  output, validates the ISO timestamp up front so a typo surfaces as
  exit 1 instead of writing an event the rollup will silently
  ignore. `silent_miss_cutoff` is classified as a side-effect kind in
  `eval.py` so the tool-usage rollup doesn't count CLI admin
  operations as tool invocations — same rationale as `search_miss`
  and `pending_expired`.

### Fixed — Audit follow-up

- **`--acknowledge-misses-before` no longer silently stamps naive
  timestamps as UTC.** A bare ISO timestamp without an offset (e.g.
  `2026-05-25T10:00:00`) from a non-UTC user used to produce an
  off-by-zone cutoff with no warning; the CLI now rejects naive
  input and prints a clear stderr message pointing at the explicit
  offset / `Z` syntax it accepts.
- **`--acknowledge-misses-before` refuses to run with telemetry
  disabled** instead of returning exit 0 having written nothing.
  The cutoff is itself a telemetry event; a disabled `Recorder`
  swallows the write so the user thought the cutoff had landed when
  it had not. Post-write verification reads the events log back to
  catch any remaining silent-failure modes (chmod failure, I/O
  error). The CLI errors with exit 1 and a clear message in either
  case.
- **`compute_health` and `curation_counts` now use the same
  `cutoff_ts` parser** (`_ensure_utc(_parse_event_ts(...))`).
  Previously the two paths used different parsers, so a naive
  `cutoff_ts` value could produce divergent rollups against the
  same store. Event timestamps in `compute_health` are also normalized
  to UTC so any legacy naive `ts` compares cleanly against the
  aware cutoff.
- **`curation_counts(since=...)` resolves `silent_miss_cutoff`
  events before applying the delta filter.** Previously a cutoff
  event whose own `ts` fell under `since` was silently dropped,
  causing a delta run to over-count pre-cutoff misses. Cutoffs are
  global markers — they now always apply regardless of `--since`.

### Fixed — Docs & examples

- README on-disk-format example: `origin.worktree` corrected to
  `origin.worktree_root` (the actual field name the writer emits);
  example ULIDs expanded from 12 chars to the 26-char form the
  validator requires.
- `examples/memories/2025-05-10-ci-runner-migration.md` now has a
  resolvable `supersedes` target — added
  `examples/memories/2025-02-10-atlas-jenkins-ci.md` as the
  predecessor so the link demonstrates the feature it markets
  instead of dangling.
- `examples/memories/README.md`: the path-drift relationship is
  corrected (drift is computed against body-cited paths with
  `verified_paths` *excluding* matched paths from the signal, not
  "against verified_paths"); the legacy-memory claim now correctly
  states such memories surface as `spot_check_required` rather than
  `fresh`.
- `examples/programmatic_client.py`: the per-hit
  `staleness_verdict` print loop now iterates over the actual MCP
  response shape (one `TextContent` per hit) instead of the
  non-existent `{"hits": [...]}` envelope; run command updated from
  `venv/bin/python` to `uv run python`.

### Changed — Module layout

- **`build_server` extracted to `bettermemory.builder`.** `build_server`
  and `_register_tools` now live in a new `bettermemory.builder`
  module; `cli/serve.py` can import them at module top level instead
  of through the previous function-local lazy import that worked
  around the `cli ↔ server` cycle. `bettermemory.server` becomes a
  back-compat re-export shim — `from bettermemory.server import
  build_server` keeps working unchanged.
- **`bettermemory.__init__` imports from canonical homes.** Package
  init now pulls `build_server` from `.builder` and `main` from
  `.cli` directly, bypassing the `server.py` shim. Public surface
  re-exported from the package root is unchanged.

### Removed — Defensive `bettermemory.server` re-exports

- **`bettermemory.server.__all__` trimmed to its actually-used
  surface.** After verifying zero in-tree consumers, the following
  symbols were dropped from `bettermemory.server`: `_register_tools`,
  `load_config`, and three `_*_semantic*` helpers. Downstream code
  doing `from bettermemory.server import load_config` or `from
  bettermemory.server import _register_tools` will now raise
  `ImportError`; the canonical paths are `bettermemory.config.load_config`
  and `bettermemory.builder._register_tools` respectively. The shim's
  retained surface is `build_server`, `main`, `SYSTEM_PROMPT_ADDENDUM`,
  `capture_origin`, and three `_cli_*` helpers (the `_cli_*` trio kept
  because the test suite monkeypatches them at the `server.` import
  path). This is a soft API break — narrow in scope, but consumers
  pinning to the old import paths must update.

### Added — Diagnostics

- **`bettermemory doctor` detects `.dist-info` dirs missing canonical
  `METADATA`.** A new check walks `site-packages` and warns on any
  `*.dist-info` directory whose `METADATA` is absent or non-readable —
  the exact failure mode that surfaces when iCloud Drive renames
  `METADATA` to `METADATA 2` mid-sync and crashes the MCP server with
  a `-32000` pydantic validation failure. Sites scanned cover both
  `site.getsitepackages()` and (when `ENABLE_USER_SITE` is true)
  `site.getusersitepackages()`, so `pip install --user` installs are
  also covered. +4 tests for the detector.

### Added — Test-suite hygiene

- **Platform-mocked coverage for the Windows `flock` branch.**
  `_flock_windows` is now exercised from a POSIX dev box via a
  `_FakeMsvcrt` injected through `sys.modules`. Tests cover the
  retry/backoff loop under simulated contention, the `LK_UNLCK` /
  `LK_NBLCK` symmetry, and `BETTERMEMORY_FLOCK_TIMEOUT` env-var
  parsing including the invalid-string fallback. +6 tests; first
  coverage of the Windows branch outside CI.
- **Direct-import smoke tests at the package boundaries.**
  `tests/test_direct_imports.py` imports every public module under
  `handlers/` (15) and `cli/` (14) and snapshots the full parameter
  signature (name, default, and `POSITIONAL_OR_KEYWORD` kind) of each
  handler, so signature drift at the import boundary fails at
  collection time rather than masquerading as a runtime `AttributeError`
  in a downstream consumer. +30 tests.

### Fixed — Doctor dist-info detector

- **Empty `METADATA` files no longer pass the `.is_file()` check.**
  The original predicate accepted zero-byte `METADATA` even though
  the pydantic loader still rejects it; the check now requires
  `is_file() AND stat().st_size > 0` so the doctor flags the empty
  case alongside the missing-file case. A whitespace-only `METADATA`
  (e.g. `"   \n  \n"` from a partial sync or manual edit) also slips
  past size > 0 while still tripping the same downstream crash, so
  the predicate now additionally reads the first 256 bytes and
  requires the canonical `Name:` header that `importlib.metadata.
  version()` parses. +2 tests pinning the zero-byte and
  whitespace-only paths.
- **`_discover_site_packages` honours `site.ENABLE_USER_SITE`.**
  Previously only `site.getsitepackages()` was scanned, so a
  `pip install --user` install with a broken dist-info would silently
  evade the detector. The user-site path is now included when (and
  only when) `ENABLE_USER_SITE` is truthy. +2 tests covering the
  enabled and disabled branches.

### Fixed — Test rigour

- **Windows `flock` env-var fallback test proves the fallback is
  non-zero.** `test_env_var_invalid_string_falls_back_to_default`
  previously asserted only that the call returned without raising; it
  now uses `always_fail=True` + `pytest.raises(TimeoutError)` + a
  retry-count assertion to prove the default timeout actually elapsed,
  catching a "fallback silently resolves to 0" regression class the
  weaker assertion would have missed.
- **Backoff test asserts monotonic growth and the 100 ms cap.**
  `test_retries_with_backoff_until_acquired` now records every
  `time.sleep` duration and asserts the sequence is monotonically
  non-decreasing and that no single sleep exceeds the 100 ms ceiling
  the production loop enforces. Prior assertion only counted retries.

### Documentation

- Stale docstring / comment refresh across `server.py`,
  `cli/__init__.py`, `builder.py`, and `cli/export.py` — six spots
  that still described the pre-2.7.3 single-module layout were
  updated to point at the post-extract structure (canonical homes
  in `builder.py`, the shim role of `server.py`, etc.). Code paths
  unchanged.

## 2.7.3 - 2026-05-25

**Post-2.7.2 dogfood audit follow-up.** The threshold-sweep on the
2.7.x `search_miss` log surfaced that 20 of 21 replayable misses were
probes asked from inside the matching project repo ("update
bettermemory" / "push it" / "is X up to date"); the model had source
open and didn't need a `memory_search`. This release suppresses that
class. A second fix adds a CLI path to clear the related
`endorsement_debt` curation bucket without touching memory bodies.

### Fixed — Audit false-positive class

- **`audit.probe_for_miss` suppresses misses when the caller's repo
  matches a top-hit memory's `origin.repo` AND the hit carries a
  `projects:` scope.** Returns `verdict="ok"` for the suppressed case
  so no `search_miss` event is emitted; `turn_audited` still records
  the verdict. The auto-scope filter on `run_search` already covers
  most of this at the search level, but the explicit check keeps the
  suppression self-contained for offline callers (eval replays,
  curation passes) that bypass auto-scope. Both predicates are
  load-bearing — a `projects:` hit from another repo still flags
  (real cross-project miss), and a same-repo global memory still
  flags (no project boundary to suppress against). Uses `repos_match`
  for URL normalisation so SSH and HTTPS forms of the same remote
  compare equal.

### Added — Curation

- **`bettermemory consolidate --acknowledge-debt`** walks the
  `endorsement_debt` bucket (memories the ranker keeps surfacing
  where every applied event came from the auto-fallback path) and
  writes one explicit `use(applied, auto=False,
  attribution="cli_acknowledge_debt")` event per id. Retroactively
  clears the curation signal without altering bodies or scopes.
  Always commits (additive; reversible with a `corrected` follow-up
  on `memory_record_use`), goes through the shared `Recorder` so
  file locking and rotation match the other CLI write paths. Filter
  re-derived inline because `EndorsementDebt.rows` caps at 20 for
  inline display and the CLI needs every debt id.

### Deferred — Threshold-rule v5

The sweep on the live log shows v3 (dominance, 2× ratio) drops only
1 of 21 misses, and v2/v4 (score floor) drops 10 but depends on the
keyword score scale — silently breaks once the running server picks
up the hybrid default. After the cwd-suppression ships, ~1 miss
remains in the corpus — too thin to calibrate a new rule against.
Deferred to a fresh dogfood window so v5 can be designed against
post-suppression false positives.

## 2.7.2 - 2026-05-25

**Windows CI repair.** The 2.7.0 auto-memory-bridge work shipped Windows-only
regressions that only surfaced in the 2.7.1 CI run (masked by the version-sync
failures that fired first). All three fixes are test-side or pure path
normalisation; no runtime behaviour change on POSIX.

### Fixed — Windows test compatibility

- **`discover_default_source_root` normalises Windows paths.** `cwd.resolve()`
  on Windows produces backslash-separated paths with a drive-letter prefix;
  swapping to `as_posix()` + stripping the `:` from `C:/Users/...` keeps the
  sanitisation a one-liner that produces a valid filename on both platforms.
  POSIX output is unchanged (`as_posix()` is a no-op on POSIX absolute paths).
- **`test_finds_auto_memory_for_simple_cwd` and `_for_dotted_cwd` mirror the
  production normalisation.** Previously the tests computed an expected
  sanitised name using the old `str(...).lstrip("/")` form, which on Windows
  leaves a `C:\` prefix that `mkdir` rejects with `WinError 123` before the
  assertion runs.
- **`test_kind_map_parity_with_recorder_call_sites` reads source files as
  UTF-8.** The default `read_text()` uses the locale encoding (`cp1252` on
  Windows), which couldn't decode a non-ASCII byte in a bettermemory docstring.

## 2.7.1 - 2026-05-24

**Post-2.7.0 audit follow-up + concurrency test coverage.** A four-agent
production-readiness audit of the 2.7.0 branch flagged security, eval-correctness,
long-running-mode, and test-hygiene gaps; this release bundles every fix.
A review pass on top added two `SessionRegistry` concurrency tests that prove
the `threading.Lock` actually serializes contention (the prior sequential
tests only proved the `OrderedDict` mechanics) and dropped a dead
`# type: ignore` that mypy was already flagging.

### Fixed — Security & robustness

- **`ingest`: symlinks skipped before any read.** A hostile auto-memory
  directory containing `secret.md -> /etc/shadow` could otherwise smuggle the
  target's contents into a memory record. A new `skip_symlink` action surfaces
  in the rendered plan summary so the skip is auditable.
- **`audit.turn_audited_fields` / `search_miss_fields` reject unknown
  `triggered_from` values at runtime.** Mirrors the 2.6.7 search-mode pattern;
  typos now fail fast at the dispatch boundary instead of silently producing
  unsplittable eval rows.
- **`events.redact_query` strips known secret shapes before the 32-char
  preview.** Five patterns (`sk-ant-…`, `sk-…`, `ghp_…`, `github_pat_…`,
  `AKIA…`); a GitHub PAT or AWS key can no longer leak via partial-token
  capture in the truncation. `SECURITY.md` carries a threat-model note for
  the query-redaction defense-in-depth.

### Fixed — Eval correctness

- **`compute_eval` dedupes `memory_ids` within a single `record_use` event.**
  `docs/eval.md` spells out the per-id denominator semantics so consumers
  know exactly what each count represents.
- **`RateCI.torn_read` flag set when numerator > denominator.** The renderer
  emits an explicit warning line, and `to_dict` exposes the flag so CI
  consumers can branch on it (a torn read indicates log rotation raced).
- **Wilson interval pinned against numerical gold** — `(50, 100)` and
  `(1, 10)` now assert exact bounds with tight tolerance, distinguishing
  Wilson from naive Wald. The prior six structural assertions would have
  passed with either formula.

### Added — Long-running-mode preparation

- **`SessionRegistry` is now LRU-bounded under a lock.** `OrderedDict`
  backing with a `max_clients=256` cap, `threading.Lock` for atomic
  touch+insert+evict, and a `stats()` introspection surface. The stdio
  transport collapses every request into one bucket, so behaviour there is
  unchanged; the LRU and lock matter for HTTP/SSE transports that fan
  arbitrary `client_id` values through one process.
- **`_already_recorded_pending_ids` scans the event log in reverse** and
  early-exits on `ev_ts < oldest_pending_issued_at`, bounding the hot-path
  scan to the pending-token window rather than the full 10 MB active log.

### Added — Test-suite & test-env hygiene

- **`conftest.pytest_collection_modifyitems` auto-skips `no_extras` /
  `no_torch_embeddings` / `no_fastembed`** when the relevant extra IS
  installed locally, eliminating false failures on dev machines that have
  the optional dependencies present.
- **`test_consolidate` subprocess gate probes `bettermemory --help`**
  instead of relying solely on `shutil.which`, catching stale editable
  installs where the binary is on `PATH` but the import is broken.
- **New `test_kind_map_parity_with_recorder_call_sites`** AST-walks `src/`
  and asserts every `recorder.record()` kind appears in either the tool-event
  map or the side-effect set. Guards against the unmapped-footer slow-drift
  bug class — extractor limitations documented inline.
- **Two new concurrent `SessionRegistry` tests.** 32-thread same-key
  contention must return one `is`-identity state; 8×25 distinct-key insertion
  past `cap=16` must preserve `size + evicted == total_inserts`. Without
  these, removing the lock would still pass the rest of the suite — the
  regression would only surface in production under HTTP/SSE fan-out.

### Build

- mypy ✓, ruff ✓, 1387 passed / 9 skipped / 0 failed (CI baseline; dev
  machines with extras installed see 11 skipped via the new auto-skip).

## 2.7.0 - 2026-05-24

**Calibration evidence + Claude Code auto-memory bridge.** Four additions land
together because they answer questions the project has flagged as open for
several releases: *which MCP tools is the model actually reaching for?* (data
for the "trim the surface" roadmap item), *is `v1_top1_high` over-firing?*
(the calibration question `audit.py`'s docstring calls out), *how do we
prompt for curation without nagging across sessions?* (the rollup-vs-delta
gap on `memory_scope_overview`), and *how do users who already accumulated
Claude Code auto-memory upgrade?* (the bridge from
`~/.claude/projects/*/memory/` into bettermemory's audit layer).

Net effect: ~80 new tests, ~3500 lines of code + docs (≈1700 of which are
tests), zero changes to the on-disk schema, zero changes to the 18-tool MCP
surface. Existing memories load and search unchanged.

### Added — `bettermemory eval --tool-usage`

- **Per-MCP-tool call-count rollup from the event log.** One row per known
  tool with absolute counts, share of total, and a bar visualisation. Tools
  without a dedicated event (today: `memory_health`) surface with a
  zero count and a "no telemetry" annotation rather than being silently
  dropped — distinguishes "this tool is not counted" from "this tool was
  never called." A new map (`eval._TOOL_EVENT_KIND_TO_TOOL`) collapses the
  per-tool event-kind variants (`write` can land with `status="ok"`,
  `"pending"`, `"duplicate"`, etc., but it's still one tool call;
  `memory_audit_turn` always emits `turn_audited` and *optionally* a
  `search_miss` side-effect — counting raw kinds would double-count it).
  Honours `--since` and `--json`; ignores the rate-mode knobs.
- **Unmapped-event-kind footer.** A future contributor who adds a new MCP
  tool without updating the map will see the unmapped kind surface in the
  output's footer rather than have its calls vanish silently. Guardrail
  against map drift over time.

### Added — `bettermemory eval --threshold-sweep`

- **Counterfactual replay of logged `search_miss` events under alternative
  threshold rules.** Closes the calibration question
  `audit.py`'s docstring flags as open. Bundled rules (all stricter than v1
  so the comparison is well-defined):

  - `v1_top1_high` — current default (reference).
  - `v2_top1_high_score_50` — v1 + top-1 score >= 50. Filters single-token
    high-coverage hits.
  - `v3_top1_high_dominant` — v1 + top-1 score >= 2× top-2 score. Distinguishes
    obvious match from borderline tie.
  - `v4_top1_high_strict_combined` — intersection of v2 and v3.

  On the maintainer's dogfood log (~14 memories, 12 replayable misses since
  2.6.4), v2 would halve the v1 miss count — direct evidence the score
  floor is a defensible tightening. Adding a new rule is two lines (a checker
  function + a `ThresholdRule` entry in `eval.THRESHOLD_RULES`).
- **Honest about its limitation.** The sweep is *relative*: strictly-looser
  rules can't be evaluated from the log alone because the companion
  `turn_audited` event doesn't carry `top_hits`. The caveat is in the
  text rendering, in the docs, and in the module docstring. Going further
  would mean adding `top_hits` to every `turn_audited` event, which bloats
  the log meaningfully — kept as a deliberate trade-off, not a roadmap commitment.
- **Pre-2.6.4 event compatibility.** Legacy hook-originated `search_miss`
  events that carry only `top_hit_ids` (no relevance label) can't be
  replayed; they surface in `skipped_legacy_event_count` so the
  `replayable_misses` denominator stays honest.

### Added — `bettermemory ingest`

- **Bridge from Claude Code auto-memory.** New CLI subcommand walks
  `~/.claude/projects/<sanitized-cwd>/memory/` (or any path passed via
  `--from`), parses the auto-memory format (frontmatter `name`,
  `description`, both nested `metadata.type` and flat top-level `type:`
  shapes the auto-memory feature has emitted across versions, plus body),
  maps the type to a bettermemory category, dedups against the active
  store and tombstone log via the existing `find_similar` /
  `find_similar_tombstones` Jaccard pass, and writes survivors as
  ordinary records carrying an `imported-from-claude-code` provenance
  scope plus a type-derived second scope (`feedback`, `project-context`,
  `user-inferences`, `reference`). Auto-discovery's path sanitiser
  replaces both `/` and `.` with `-` to match Claude Code's on-disk
  layout — so a worktree at `~/projects/foo/.claude/worktrees/bar`
  resolves correctly rather than silently missing.
- **Category mapping.** `user` → `Category.USER_INFERENCE`, `feedback` →
  `Category.FACT`, `project` → `Category.FACT`, `reference` →
  `Category.AMBIENT`, anything else / missing → `Category.FACT`. The MCP
  write handler's always-pending gate for `user-inference` does NOT
  apply to ingest — an ingest run is the user telling bettermemory "these
  pre-existing user-curated files are mine, ingest them" and routing each
  one through pending-confirm would be ergonomic theatre. The category
  still lands on the record so downstream curation treats them as
  user-claim memories.
- **No source-file mutation.** Considered and rejected — modifying the
  source `.md` files would race Claude Code's own auto-memory writes, and
  the dedup gate already makes re-ingestion safe (matching content
  Jaccards at 1.0 and trips the high-similarity threshold). Re-running
  ingest on an already-ingested source produces the expected
  `skip_duplicate` rows.
- **Plugin SKILL.md banner loosened.** The pre-2.7.0 banner read
  *"Do not fragment memory across ad-hoc files alongside …
  `~/.claude/projects/*/memory/` …"* — implicitly framing the auto-memory
  feature as adversarial. The new banner names the auto-memory path
  specifically and points to the ingest CLI, flipping the framing from
  "fight" to "consume rather than fight."

### Added — `memory_scope_overview` delta field

- **`curation_pending_new_since_last_session`.** Sibling to the existing
  absolute `curation_pending` dict. Same key shape (`stale`,
  `never_verified`, `drifted`, `cold`, `dead`, `silent_misses`,
  `endorsement_debt`) but counted only against events emitted and
  memories created after the latest event from a session other than
  the current one. The field is `null` when no prior session exists in
  the event log (first session ever, or after a wipe) — the model branches
  on null vs. dict to tell "no baseline" apart from "nothing new since
  baseline." The tool description tells the model: prompt about curation
  based on the *delta*, surface the *absolute* on demand.
- **`find_prior_session_boundary` helper.** Pure function in `health.py`
  that walks the event stream once and returns the max ts among events
  whose `session` field differs from the caller's current `session_id`.
  Accepts both the canonical `session` and the legacy `session_id` field
  names so pre-unification archives still resolve to a usable boundary.
  Materialisation in the handler is intentional — the handler runs three
  passes over the same in-memory event list (absolute rollup, boundary
  helper, delta rollup); the events list is bounded by the active log
  + rotated archives (same scale `compute_health` already pays at
  session-start), and re-walking the iterator three times would do
  three times the I/O for the same result.
- **`curation_counts` gains a `since` parameter.** When set, filters
  events to `ts > since` and memories to `created > since` (the
  boundary value IS the prior session's last event timestamp, so it
  belongs to the prior session, not the delta). The same helper
  produces both the absolute and delta views from the handler — no
  parallel implementation to drift.

### Changed — plugin SKILL.md banner

- The pre-2.7.0 anti-fragmentation banner named
  `~/.claude/projects/*/memory/` as forbidden alongside ad-hoc files
  like `MEMORY.md`. The new banner singles that path out specifically as
  *"ingest it once if it exists"* and links to `bettermemory ingest`.
  Lets users who came to bettermemory after months of auto-memory
  accumulation upgrade cleanly. The `MEMORY.md` / scratch-markdown
  proscription is preserved verbatim.

### Pre-tag audit fixes (folded in)

A second-pass audit of the 2.7.0 surface (four parallel fresh-eyes
agents) caught several correctness gaps in the new features.
Addressed before the tag rather than as a 2.7.1:

- **`memory_scope_overview` delta is correct under SessionRegistry.**
  The handler was passing `state.session_id` to
  `find_prior_session_boundary`, but every event the recorder writes
  carries the recorder's process-lifetime `session_id` (a different
  value when SessionRegistry is in use). In multi-client mode that
  collapsed the delta to ~empty because the handler treated every
  recorded event as "from another session." The handler now passes
  `self.recorder.session_id`. Regression test:
  `test_scope_overview_delta_uses_recorder_session_not_state`.
- **`curation_counts(since=…)` boundary is exclusive.** The filter was
  strict `<` (`ev_ts < since` skipped), which meant the prior session's
  last event itself slipped into the delta. Now `<=` for both event
  timestamp and memory `created`. The CHANGELOG promise of "events
  emitted and memories created since the previous session ended"
  required the boundary value to be exclusive (it IS the prior
  session's last event ts). Lock-in tests:
  `test_curation_counts_since_filter_is_exclusive_at_boundary`,
  `test_curation_counts_since_excludes_old_memory_aging_into_stale`.
- **`memory_list` events count as retrievals in `compute_eval`.**
  `audit.py:88` treats `{"search","show","list"}` as the retrieval set;
  `compute_eval` was only counting `search` + `show`, narrowing the
  `retrieval_occurrences` denominator vs. the audit cadence and
  distorting `memory_helped_rate` downward for workflows that lean on
  `memory_list`. Lock-in test:
  `TestComputeEvalListKind.test_list_event_counts_as_retrieval`.
- **`v1_drift` surfaces in `compute_threshold_sweep`.** The previous
  docstring promised v1's replay must equal `replayable_misses` but
  the production helper never raised on mismatch — only a 3-event
  synthetic test enforced it. New `v1_drift` field on
  `ThresholdSweepReport` carries `replayable_misses - v1_would_flag`;
  the text renderer surfaces a warning line when non-zero.
- **`_parse_ts` returns tz-aware on naive ISO input.** The recorder
  always writes `Z`-suffixed timestamps, but external producers or
  older binaries could emit naive ISO strings. `_parse_ts` was
  returning naive datetimes for those, and the downstream `<` against
  the tz-aware cutoff would raise `TypeError` mid-iteration. Naive
  inputs are now stamped as UTC.
- **`recent_retrieval_count` excludes `bool`.** `isinstance(True, int)`
  is True in Python; a stray `True` / `False` in the field would
  silently count as 1 / 0. Bools are now guarded out at both
  `_silent_miss_from_event` and the threshold-sweep replay.
- **`bettermemory ingest --force` for parity with `memory_write`.** The
  active-store dedup gate can be bypassed for the rare case of a
  legitimately-near auto-memory record. Tombstone dedup remains in
  force — re-importing a deliberately-removed memory stays disallowed.
- **`apply_ingest_plan` no longer swallows `MemoryError` per row.**
  The bare `except Exception` would retry-and-eat disk-full / OOM
  errors on every subsequent row. Narrowed to `(ValueError, OSError)`
  so hard system failures propagate instead of being relabeled as
  per-row `skip_invalid`.
- **`_TYPE_TO_CATEGORY` / `_TYPE_TO_EXTRA_SCOPE` key invariant.** A
  module-import-time assert pins the two maps to the same key set,
  catching typos that would otherwise silently downgrade ingest
  behaviour (missing extra-scope loses the type-derived scope;
  missing category falls back to `FACT`).
- **`IngestPlan.summary` zero-init is driven by `_ACTIONS`.** The
  hardcoded zero-init list silently dropped any future `Action`
  literal a contributor added. Now derived from the single `_ACTIONS`
  tuple.
- **Nested-vs-flat `type:` precedence is documented and tested.** Both
  shapes ship in real auto-memory directories. Precedence: nested
  wins on conflict. Lock-in test: `test_nested_type_wins_when_both_present`.
- **`discover_default_source_root` positive tests.** The 2.7.0 audit
  added dot-replacement to the path sanitiser; until now no positive
  test exercised the sanitiser at all, so a refactor that reverted to
  slash-only behaviour would pass the negative test silently. Tests
  added for both the simple and `.claude/worktrees/*`-style dotted
  cases.
- **Tone polish.** `DESC_MEMORY_SCOPE_OVERVIEW` "drifting into stale"
  changed to "aging into stale" to disambiguate from the separate
  `drifted` bucket. Tool-usage footer wording acknowledges side-effect
  event kinds. `_humanize_seconds` no longer prints "1 day" for 1d
  while emitting "30d" elsewhere. `docs/eval.md` example block now
  matches actual `render_text` output.

## 2.6.8 - 2026-05-24

**External audit follow-up.** A four-agent fresh-eyes audit of 2.6.7
found one ranker default that underperformed by design, three event-log
and session-state correctness issues the dogfood ~14-memory scale would
never hit, one privacy-by-default gap, and one README over-promise.
Every finding is fixed in this release. The two findings the audit
flagged but the maintainer kept out of scope — empirical recalibration
of the 30-day staleness window and the 0.3 semantic threshold against
a real dataset — are tracked for the next minor; both are observability
questions the eval CLI was designed to answer once enough turns of
dogfood traffic exist.

### Changed — default `search_mode` is now `hybrid`

- **`behavior.search_mode` defaults to `"hybrid"` (was `"keyword"`).**
  Hybrid runs RRF over keyword + BM25 (and semantic when the
  `[embeddings]` extra is installed), gracefully degrading to
  keyword + BM25 fusion when no embedding extra is present — so the
  flip doesn't add a dep requirement. The legacy keyword scorer
  lacks IDF weighting and underperforms on rare-term queries. The
  1.6.0 default is still selectable explicitly via `mode="keyword"`
  for byte-stable behaviour on identifier-heavy queries. `docs/api.md`,
  the `DEFAULT_CONFIG` template, and `BehaviorConfig.search_mode`
  all carry matching comments now.

### Fixed — HIGH: event log rotation could double-count on crash

- **`events._rotate_if_needed` had a crash window between gzip-close
  and source unlink that left both files present.** Recovery via
  `iter_all_events` then yielded every rotated event twice — and the
  eval framework's `silent_miss_rate` / `endorsement_rate` numerators
  are computed off that stream, so a single crashed rotation would
  inflate the denominators for the lifetime of the active log. Fixed
  with a `.rotating` two-phase rename: the active log is atomically
  renamed to `.events-{ts}.jsonl.rotating` *first*, then compressed
  into a `.jsonl.gz.tmp` sibling, then renamed atomically to the
  canonical `.gz`, then the `.rotating` holding file is unlinked. A
  crash at any step is recoverable on the next rotation via a sweep
  at the top of `_rotate_if_needed`. Archives inherit the active-log's
  `0o600` permissions via an explicit `chmod` before the canonical
  rename. `iter_all_events` reads orphan `.rotating` files only when
  no matching `.gz` exists.

### Fixed — HIGH: silent pending-write expiry

- **`SessionState._evict_expired` dropped pending writes silently.**
  A user saying "yes, save it" 61+ minutes after the prompt got back
  `no pending write with id ...` — the eviction was indistinguishable
  from a typo and left no event-log trail. Two related changes:
  - `_advance_turn` now calls `state._evict_expired()` and any drop
    populates `_expired_pending`, which `_drain_pending_expired`
    consumes to emit one `pending_expired` event per drop (carrying
    the `pending_id`, the `ttl_seconds` that elapsed, and the
    proposed-memory `category` so downstream curation can flag lost
    `user-inference` confirmations specifically).
  - `memory_write_confirm` now consults
    `SessionState.was_recently_expired(pending_id)` and raises a
    targeted error — `"pending write {pid!r} expired before
    confirmation (the 1-hour TTL elapsed). The proposed memory was
    not saved. Re-stage with memory_write to create a fresh pending
    id."` — so the model knows whether to apologise-and-re-ask or
    debug a phantom id.

### Fixed — HIGH: auto-`record_use` race when log events landed after the scan

- **`_advance_turn`'s pre-consume dedup scanned only
  `attribution="hook"` events and matched on `(session, memory_id)`
  alone.** Two failure modes followed:
  - A model that did `memory_search` → `memory_record_use(applied)`
    → `memory_search` (same id) had its *fresh* second token
    falsely purged by the *stale* first record_use event, dropping
    the auto-commit cadence on a legitimate new retrieval.
  - Any non-hook attribution that landed in the log after the search
    but before the auto-fire could produce two `use` events for the
    same `(turn, memory_id)` pair.
  Replaced with `_already_recorded_pending_ids`, which: (a) covers
  any `use` event regardless of `attribution` tier, and (b) gates
  on `event.ts >= token.issued_at` so a stale event from a prior
  retrieval cycle no longer purges a fresh token. The hook-only
  function name is kept as a backwards-compat alias.

### Fixed — MED: search query text logged verbatim to disk by default

- **`Recorder.record` wrote `query` / `probe_query` field values
  verbatim.** A user pasting `key=sk-very-secret-...` into a
  `memory_search` landed the full secret on disk. Two changes:
  - New `telemetry.log_queries_verbatim` flag (default `false` since
    2.6.8) replaces the field with `{"hash": "<16-hex sha256
    prefix>", "preview": "<first 32 chars>", "len": N}` before the
    event is serialised. Cross-event correlation still works (a
    repeated query has the same hash); the first 32 characters
    survive for triage; the full body is not recoverable.
  - Rotated archives now match the active log's `0o600` permissions
    (was umask-default — defense-in-depth so the chmod miss on the
    archive doesn't undo the active-log permission story).
  Set `telemetry.log_queries_verbatim = true` to restore the legacy
  shape for ranker debugging.

### Fixed — LOW: README over-promised "every use is logged with claim-level excerpt"

- The phrasing implied every retrieval landed in the log with an
  excerpt. In practice only the *model-explicit*
  (`memory_record_use(claim_excerpts=…)`) and *hook-attributed*
  (Stop hook substring match) tiers carry excerpts; the
  `attribution="auto"` fallback for retrievals neither path covers
  has no excerpt and is excluded from `memory_helped_rate`'s
  numerator. README and the "Claim-level audit trail" bullet now
  spell out the three tiers and which one carries excerpts.

## 2.6.7 - 2026-05-23

**Post-2.6.6 audit follow-up.** A six-agent meta-audit of the 2.6.6
release found two HIGH-severity contract drifts (one doc, one
test), eight MEDIUM items spanning correctness / concurrency /
contract / release-hygiene, and three LOW hardening items. Two
agents flagged the same `consolidate.py:407` legacy-fallback gap
independently. Every finding worth fixing in code is in this
release; the LOWs explicitly out of scope (web UI defense-in-depth
headers, `init.py` atomic write, sub-microsecond chmod window on
freshly-created index/embedding files) are squarely inside the
documented same-machine trust model.

### Fixed — HIGH: contract drift between handler and docs

- **`docs/api.md` memory_write success status said `"ok"`,
  emitted value is `"committed"`.** The exact drift CHANGELOG
  2.6.2 announced as fixed in `DESC_MEMORY_WRITE` — but
  `docs/api.md` (the file `CONTRIBUTING.md:44` calls "pinned"
  as the stability contract) was never updated in lockstep.
  A library author or programmatic client branching on the
  documented value got a no-match against every successful
  write. Fixed to `"committed"` matching the handler description
  and `_response.py:277`.

### Fixed — HIGH: regression test that didn't exercise the production path

- **`test_find_similar_dispatches_to_jaccard_without_model`
  asserted `len(hits) >= 0` — tautology.** The docstring
  promised "two bodies that share no tokens shouldn't surface
  via Jaccard," but the test bodies shared the token `postgres`,
  the inline comment contradicted the docstring, and the
  assertion was a tautology. Same shape as the 2.6.6
  `test_schema_rebuild_executescript_is_transactional` rewrite —
  a test named for a production branch it never exercised.
  Rewritten with a positive case (shared distinctive tokens →
  Jaccard hit at similarity > 0.40) plus a negative case
  (disjoint token sets → no hit, no exception, no hidden
  semantic-fallback path) so the dispatch boundary is pinned.

### Fixed — MEDIUM: incomplete generalisation (two agents flagged independently)

- **`consolidate.py:407` `find_demotion_candidates` missed the
  `memory_ids` legacy fallback.** Read `event.get("returned") or
  event.get("hit_ids") or []`, missing the canonical-first /
  legacy-second / oldest-third chain every other call site
  uses (`eval.py:361`, `hook.py:365-367`, `health.py:699`,
  `health.py:1423`). Two of the six audit agents flagged this
  independently. Same pattern as the 2.6.6 `health.py:679` fix
  — the 2.6.5 sweep missed two sites, not one. Fix: insert
  `memory_ids` between `returned` and `hit_ids`.

### Fixed — MEDIUM: telemetry false positives

- **`audit.py` `_RETRIEVAL_EVENT_KINDS` whitelist excluded
  `list` events.** `memory_list(scopes=[…])` returns ids (and
  bodies when `with_bodies=True`) and logs `kind="list",
  returned=[…]` — same retrieval semantics as `search` and
  `show`, but the silent-miss probe only counted the first two.
  A model using `memory_list` to triage would be flagged for a
  silent miss even though it had the content in front of it.
  Fix: add `list` to the frozenset.

### Fixed — MEDIUM: dormant concurrency TOCTOU

- **`SessionRegistry.for_request` had a read-then-write race
  on shared mutable state with no lock.** Two concurrent
  callers observing the same missing `client_id` both create
  fresh `SessionState` instances and the second `__setitem__`
  wipes the first writer's `pending_writes` / `disabled_scopes`
  / `turn_counter`. Stdio collapses every request into a single
  default-client key so the race is dormant today, but the
  class docstring anticipates an HTTP/SSE transport that fans
  distinct `client_id`s in parallel — at which point the race
  becomes live and silent. Fix: `dict.setdefault` is atomic on
  CPython, so concurrent callers observing a missing key all
  receive the same `SessionState` instance.

### Fixed — MEDIUM: tool description omitted a returned bucket

- **`DESC_MEMORY_HEALTH` listed `dead_weight` but never
  mentioned `cold_memories`.** Description claimed
  `dead_weight = created > window_days AND never applied`.
  Code additionally requires `retrieval_count > 0`;
  never-retrieved memories route to the separate `cold_memories`
  bucket the description never named. A model curating against
  `dead_weight` would miss the cold subset entirely. Fix:
  mirror the `docs/api.md` framing — dead is *"retrieved but
  didn't help"*, cold is *"the ranker isn't surfacing this at
  all"*.

### Fixed — MEDIUM: source enum under-documented

- **`docs/api.md` listed only `"explicit-statement"` and
  `"inferred"` for `memory_write.source`.** `models.py` defines
  a third value `"user-correction"` that the handler accepts
  and that `examples/memories/2025-04-15-projects-foo-stack.md`
  actively uses. The doc is the contract; the validator was
  wider than the contract. Fix: include `"user-correction"`
  with prose covering the post-hoc-correction semantics.

### Fixed — MEDIUM: ROADMAP version stale

- **`docs/ROADMAP.md` "Where we are" pinned at v2.6.3.**
  CHANGELOG 2.6.3 flagged "this count rots fast" but no test
  pins the version, so the same drift recurred immediately
  through 2.6.4 / 2.6.5 / 2.6.6. Fix: bump to v2.6.6. (A
  sync-guard test would close this structurally; out of scope
  for this round.)

### Fixed — MEDIUM: dispatch path defense-in-depth + test

- **`search.search()` had no runtime mode validator.** The
  `SearchMode` Literal pinned modes at the type-checker layer
  but Python doesn't enforce Literals at call time, so any
  unknown string from a future programmatic caller would fall
  through the if/elif chain into the `else` branch and silently
  run hybrid. Fix: runtime validator at the dispatch boundary;
  unknown modes raise `ValueError` with the closed-set message.
  `test_mode_invalid_returns_typed_error` was the test that
  named the missing validator ("we can't easily check this at
  runtime") — now actually exercises the production rejection.

### Fixed — MEDIUM: weak structural pin on cold_memories

- **`test_cold_memories_field_returned_by_health` only asserted
  `isinstance(res["cold_memories"], list)`.** A regression that
  always returned `[]` would pass. Rewritten to drive the
  routing predicate end-to-end (write a fact memory, call
  `memory_health(window_days=0)`, assert the id lands in
  `cold_memories` AND NOT in `dead_weight`). Misroutes between
  the two buckets now fail this test.

### Fixed — MEDIUM: broad pytest.raises pattern

- **30 sites used `pytest.raises(Exception)` without `match=`**
  across test_server.py, test_server_record_use.py,
  test_rename_scope.py, test_server_v12_features.py,
  test_session_registry.py, test_audit.py,
  test_server_tombstones.py, test_server_links.py. Bare
  `Exception` catches any error type with any message — a
  refactor that swapped a clean `ValueError` for
  `AttributeError: 'NoneType'` would keep tests green while
  users see uninformative tracebacks at the MCP boundary. Fix:
  every site now pins the actual error-message substring via
  `match="…"`, following the pattern test_server.py:205
  established. Where two validation layers can fire (pydantic
  vs. handler `isinstance` checks), the regex covers both so a
  rearrangement of validation order doesn't fail tests for the
  wrong reason.

### Fixed — LOW: discipline alignment

- **`auto` discriminator strictness drift** — `health.py:736`
  used `is True`, `eval.py:387` used `bool(...)`. Production
  traffic only writes literal True so no current bug, but
  identical structural class to the session-id sweep
  2.6.5/2.6.6 already addressed. Fix: aligned `eval.py:387` to
  `ev.get("auto") is True`.

- **`events.py:_archive_sort_key` mis-parsed session_ids with
  internal dashes** (Claude Code session_id is a full UUID).
  Naive `inner.split("-")[-1]` fell into the numeric-tail
  branch for UUIDs whose last hex chunk happened to be all
  digits, producing a wrong sort key. Most consumers use the
  embedded `ts` so impact is small, but the parser was broken.
  Fix: regex anchored to end-of-string for the `-N` counter
  suffix.

### Fixed — LOW: per-tool description wording

- **`DESC_MEMORY_SEARCH`** now explicitly notes that memories
  with no recorded origin pass `auto_scope=True` as global —
  mirrors the sibling `DESC_MEMORY_SCOPE_OVERVIEW` wording.

- **`DESC_MEMORY_RECORD_USE`** now documents the empty-string
  rejection on `claim_excerpts` (handler raises ValueError
  pointing the caller at `None` for "no specific claim").

## 2.6.6 - 2026-05-23

**Post-2.6.5 audit follow-up.** A four-agent meta-audit of the 2.6.5
"post-2.6.4 audit follow-up" release found two items: one
pattern-discipline gap that 2.6.5 swept everywhere else but missed,
and one regression test that pinned a stdlib property instead of
the production call site it was named after.

### Fixed — incomplete generalisation

- **`health.py` distinct-session aggregation read canonical-only.**
  The 2.6.5 sweep applied the canonical-first / legacy-second
  fallback to five other `health.py` event reads but missed line
  679's `sess = ev.get("session")`. The Recorder stamps `session`
  on most canonical events, but `turn_audited` / `search_miss` use
  `session_id` as their canonical field — under-counting the
  distinct-session metric in `compute_health`'s rollup whenever
  those event kinds were the only events in a session. Fix:
  `ev.get("session") or ev.get("session_id")`, matching the
  pattern applied at the four other `health.py` sites.

### Fixed — regression test that didn't exercise the production path

- **`test_schema_rebuild_executescript_is_transactional` pinned a
  stdlib property, not the production call site.** The 2.6.5 test
  opened a raw `sqlite3.Connection`, hand-rolled the `BEGIN
  IMMEDIATE … COMMIT`-embedded executescript pattern, and asserted
  SQLite genuinely wraps. That verifies the property the fix
  depends on, but a regression in `_ensure_schema` itself (e.g.,
  reverting to the 2.6.4 shape: `conn.execute("BEGIN IMMEDIATE")`
  then a separate `executescript`) would still pass the test —
  the production code path isn't called. Rewrite: sets up a v1
  index with a row, injects a broken `_SCHEMA` via monkeypatch,
  calls `index._ensure_schema` directly, asserts the row survives.
  The 2.6.4 buggy shape would commit the DROP in autocommit mode
  before the failing CREATE, losing the row; the 2.6.5 fix
  preserves it.

## 2.6.5 - 2026-05-23

**Post-2.6.4 audit follow-up.** A six-agent meta-audit of the 2.6.4
"structural audit" release found several claims that didn't hold and
one regression the release itself introduced. This release addresses
every HIGH and MEDIUM finding plus the LOW/NIT items with clean
fixes.

### Fixed — live bugs in 2.6.4

- **`bounded_read` flattened `FileNotFoundError` to bare `OSError`.**
  The 2.6.4 catch-and-re-raise turned `Store.restore` and
  `Store.rename_scope`'s `except FileNotFoundError` handlers into
  dead code AND made `test_concurrency`'s stress test flaky (~20%
  under contention) — a regression introduced by the audit release
  itself. Fix: drop the re-wrap; `path.stat()` raises its native
  subclass unchanged.

- **`index._ensure_schema` `BEGIN IMMEDIATE` wrapped nothing.**
  Python's `sqlite3.executescript()` implicitly commits any pending
  transaction before it runs, so 2.6.4's
  `conn.execute("BEGIN IMMEDIATE")` followed by two `executescript()`
  calls left the DROP/CREATE unprotected — a concurrent reader could
  still see "no such table: memories" mid-rebuild, exactly the gap
  the 2.6.4 fix claimed to close. Fix: move `BEGIN`/`COMMIT` inside
  the executescript string. Verified atomic on CPython 3.11–3.13.

- **`consolidate` merge-rollback orphaned tombstoned duplicates.**
  In a 3+-member cluster, if dup A tombstoned but dup B failed,
  2.6.4's rollback restored the keeper but left A's content in
  *neither* the keeper (rolled back, never received the merge) nor
  the active set (tombstoned) — silent data loss until a manual
  `memory_restore`. Fix: track successfully-tombstoned ids and
  `store.restore()` them on failure.

- **`_frontmatter.load` silently masked UTF-8 corruption.** 2.6.4
  swapped `read_text(encoding="utf-8")` (raises on invalid UTF-8)
  for `decode("utf-8", errors="replace")` (silent U+FFFD
  substitution). A corrupt memory file then loaded into the
  retrieval surface, `doctor` reported it clean, and the next
  mutator rewrote the file — laundering the corruption permanently.
  Fix: strict decode, raise `ValueError` so the store's
  malformed-file skip path fires (the pre-2.6.4 contract).

- **`health.py` (×4) and `eval.py` (×2) missed the legacy-name
  fallback.** 2.6.4 applied the canonical-first / legacy-second
  discipline to five consumers but skipped the core curation engine
  (`compute_health`, `curation_counts`) and the silent-miss eval
  renderer. Same discipline applied.

- **`sync.py` push/pull lock did NOT coordinate with `Store.write`**
  as the 2.6.4 CHANGELOG claimed. `.sync.lock` is a different inode
  from per-memory `<id>.md.lock`, so `flock` never serialized them.
  The lock genuinely serializes sync-vs-sync (push-vs-push,
  push-vs-pull). 2.6.4 CHANGELOG and in-code comments corrected.
  True sync↔Store coordination would require global write
  serialization and is left as a deliberate future decision.

### Fixed — incomplete generalisations

- **Shared `turn_audited` / `search_miss` field builders**
  (`audit.turn_audited_fields`, `audit.search_miss_fields`). The
  2.6.4 audit found the Stop hook and the in-process MCP handler
  emitting these events with hand-copied kwarg lists that had
  *already* drifted (`triggered_from` on one, absent on the other).
  Both producers now route through the shared builders, so they
  cannot drift again. Handler tags `triggered_from="mcp_tool"`;
  `search_miss` carries `recent_retrieval_count` so
  `eval._silent_miss_from_event`'s column stops being permanently
  blank.

- **`semantic` stale-dimension cache crashed `memory_write`.**
  `cosine_similarity_normalized` with `zip(strict=True)` (added in
  2.6.4) raises `ValueError` on mismatched-dimension vectors —
  uncaught on the `memory_write` → `find_similar` path. When a
  persistent embedding cache was written under one model checkpoint
  and hydrated under another (same `model_name`, different output
  dimension), every comparison raised and the whole handler failed.
  New `semantic._note_model_dimension` learns the live dimension
  from encodes the callers already do (no probe encode) and purges
  stale entries; `find_similar` / `_search` /
  `find_similar_tombstones` prime it from their query encode;
  `_find_dedup_semantic` gets a defensive `except ValueError`.

- **`bounded_tail_read` hung on writer-less FIFO** via
  `consolidate._load_transcript`. Pointing
  `consolidate --llm --from-transcript` at a FIFO would block
  `open("rb")` forever. Added `is_file()` guard mirroring the hook's;
  corrected `bounded_tail_read`'s docstring FIFO language.

### Fixed — LOW / NIT

- `audit._count_recent_retrievals` — added the canonical-first
  session fallback the 2.6.4 release applied everywhere else but
  missed on the silent-miss probe's own hot path.
- `_handlers.py` comment falsely claimed "canonical handler writes
  both `session` and `session_id`" — corrected (the Recorder always
  stamps `session`, never `session_id`; only events whose producer
  passes `session_id=` explicitly carry both).
- `tests/test_event_helpers.py` "contract" test emitted
  `claim_excerpts` / `lookback_seconds` / `probe_query` / `query`
  without asserting them — a producer-side rename would have slipped
  through the very test it was supposed to pin. Assertions added.
- `tests/test_changelog.py` — added `encoding="utf-8"` to the
  `plugin.json` / `marketplace.json` reads (the same anti-pattern
  the 2.6.4 CI fix corrected for `CHANGELOG.md` in the same file).
- CHANGELOG 2.6.4 entry corrections: `hook.py:_run_audit` →
  `run_audit` (function name); "Six other consumers" → "Five"
  (matches the parenthetical); the false sync-vs-Store coordination
  claim corrected as noted above.
- `llm.py` Ollama-truncation comment described an unimplemented
  "empty `done` flag (older)" branch — tightened to what the code
  actually checks.

### Added — regression tests

- `test_fsutil.test_missing_file_raises_filenotfounderror` pins the
  `OSError` subclass contract (the test that should have caught the
  2.6.4 flattening regression but only asserted `OSError`).
- `test_frontmatter.test_load_rejects_invalid_utf8` pins the
  strict-decode contract.
- `test_index.test_schema_rebuild_executescript_is_transactional`
  pins the `executescript`-with-embedded-`BEGIN` atomicity property
  the fix relies on.
- `test_consolidate_llm.test_merge_rollback_restores_earlier_tombstoned_duplicates`
  covers the multi-duplicate rollback.
- `test_consolidate_llm.test_load_transcript_does_not_hang_on_fifo`
  — daemon-thread regression that pins the FIFO guard without
  hanging the suite on regression.
- `test_audit.test_event_field_builders_pin_canonical_shape` pins
  the shared builders' output, including the two 2.6.4-audit gaps
  (`triggered_from` and `recent_retrieval_count` on `search_miss`).
- `test_semantic.test_stale_dimension_cache_entries_are_purged`
  pins the dimension reconcile.

## 2.6.4 - 2026-05-21

**Fourth audit pass over the 2.6.x surface, this one structural.** The
prior three audits found instances of named bug classes; each fix
landed in one location while the same pattern lived elsewhere
unchecked. This release inverts the discipline: instead of finding
more instances, it makes whole classes structurally impossible. Six
parallel agent audits hunted (1) bounds enforcement, (2) field-name
drift, (3) cross-process concurrency primitives, (4) pattern non-
generalization from prior fixes, (5) test-fixture honesty, and (6)
novel bug classes the first three passes missed. The big find: the
2.1.0 silent-miss telemetry flagship is partially broken for hook-
originated events — three audits walked past it.

### Added — structural foundations

- **`_fsutil.bounded_read` / `bounded_tail_read` / `bounded_stream_read`.**
  Single point of enforcement for resource caps on input. The 2.6.2
  and 2.6.3 releases fixed three separate unbounded-read defects (the
  consolidate transcript, the hook transcript, the byte-vs-char trap
  on the cap constant); the underlying class is one this codebase
  kept producing because each call site re-derived its own
  `.read(N)` discipline. Centralising here means the next time
  someone adds a "read this user-controlled file" helper, the cap
  honours bytes (not characters), the error path is named
  (`ValueError`, not OOM), and the byte-vs-char trap is structurally
  impossible because the helpers open in binary mode. Unit-tested
  against the 4-byte-codepoint case directly.
- **`_fsutil.flock_excl` — single definition for the locking
  primitive.** `store.py:_locked` and `events.py:_locked` had been
  duplicate implementations of the same fcntl-based exclusive
  lock since the start. The 2.6.3 audit-pass-of-audit-pass fix
  touched both files because the unlink-in-finally bug lived in both
  copies. This release lifts the canonical definition to
  `_fsutil.flock_excl`; store / events / sync all alias to it.
  Future locking-discipline fixes land in one place — the
  3× duplication that the 2.6.3 audit cycle had to chase is gone.
- **`tests/_event_helpers.EventLog` + `event_log` fixture.** Real
  `Recorder`-backed event log for tests. The 2.6.2 and 2.6.3 bugs
  both shipped because test fixtures hand-built event dicts with
  field names the canonical `Recorder` doesn't emit (`memory_search`
  / `memory_ids` / `hit_ids` instead of `search` / `ids` /
  `returned`). Tests passed, production silently failed. `EventLog`
  routes through the real `Recorder` so any future field rename
  fails the suite at write time instead of shipping. Includes
  `test_shape_matches_real_handlers_emission` which pins the
  canonical key set explicitly — drift trips the suite.
- **Multi-process lockfile fault-injection tests
  (`test_concurrency.py`).** Two new deterministic tests:
  `test_store_locked_persists_lockfile_after_exit` and
  `test_events_locked_persists_lockfile_after_exit` assert that the
  lockfile must NOT be unlinked on context-manager exit (the exact
  2.6.3 regression). `test_locked_serializes_two_spawned_processes`
  spawns two interpreters and asserts B blocks on A's lock for the
  hold window. The stress test alone wouldn't catch a regression of
  the inode-identity invariant — these close that gap.
- **`test_changelog.py` — CHANGELOG hygiene lint.** Asserts every
  `## <version> -` heading is well-formed AND the version in
  `pyproject.toml` has a matching entry. The 2.6.2 release noted
  three missing-heading defects (1.2.1, 1.3.0, 2.6.0); the prose
  body in each case was intact but the heading had silently
  disappeared. Also pins `plugin.json` and `marketplace.json`
  version against `pyproject.toml` since the recurring foot-gun
  of one-of-three drifting bit the project on three separate
  releases. The next missing-heading or version-drift instance
  trips CI instead of an audit pass.

### Fixed — live shipping bugs

- **CRITICAL: silent-miss telemetry partially broken for hook-
  originated events (the 2.1.0 flagship).** `hook.py:run_audit`
  emitted `search_miss` with `top_hit_ids=[strings]` and omitted
  `threshold_rule` / `lookback_seconds`, while `_handlers.py:_advance_turn`
  (the in-process MCP handler) emits `top_hits=[dicts]` with both
  fields. `eval.py:_silent_miss_from_event` reads the canonical
  names — so every hook-originated silent miss surfaced in `bettermemory
  eval` showed blank `top_missed_id` / `top_missed_relevance` /
  `threshold_rule` columns. The Stop hook is the *primary* production
  source of search_miss events (model-side `memory_audit_turn` rarely
  fires unprompted), so the flagship eval feature was running blind
  on real traffic. **Three audit passes missed this.** Fix: hook
  now emits the canonical shape (`session_id=` kwarg, `top_hits=
  [h.to_dict() for h in report.top_hits]`, `threshold_rule`,
  `lookback_seconds`, `probe_mode`); eval reader tolerates the
  legacy `top_hit_ids` shape with `None` relevance so pre-2.6.4
  archived events still render the id column. Regression coverage
  in `test_hook.py` (pins canonical shape on every hook emission)
  and `test_eval.py:test_legacy_hook_top_hit_ids_shape_still_renders`.
- **HIGH: `_frontmatter.load` read whole file before YAML cap fired.**
  `_frontmatter.py:108-110` called `Path.read_text()` with no
  pre-flight size check. The existing `_MAX_YAML_BYTES = 64 KiB`
  cap only protects the frontmatter region — a hostile `sync pull`
  pushing a multi-GB `.md` would OOM the loader before the YAML
  parser ran. Three audit agents flagged this independently. Fix:
  stat-rejects above `_MAX_FILE_BYTES = 1 MiB` (250× the largest
  legitimate memory body, 16× the YAML cap) using `bounded_read`.
- **HIGH: `hook.py:main` read entire stdin payload with no cap.**
  `sys.stdin.read()` before `json.loads` would buffer GB of
  garbage from a misbehaving pipe writer into memory before the
  parser got a chance to reject. Stop hooks fire on every assistant
  turn — the blast radius is wide. Fix: `bounded_stream_read(
  sys.stdin.buffer, 64 KiB)` with oversized-payload treated as
  malformed (silent no-op, preserving the hook's "never break the
  turn end" contract).
- **HIGH: `migrate.py` rewrote memory files without `_locked`.**
  `migrate_origin_in_directory` walked the active set and
  read-modify-wrote each file via tmp+rename WITHOUT acquiring the
  per-file lock the rest of the store uses. A concurrent
  `Store.update` / `tombstone` / `mark_verified` from a running MCP
  server could land between the migrate read and the migrate
  rename, silently losing the in-flight edit. Fix: wrap each
  per-file RMW in `_locked(path)` AND route through
  `_atomic_write_post` (which the rest of the store already uses) —
  the migrate path was also dropping the 0o600 chmod, so
  post-migration files inherited umask (0o644) and ended up
  world-readable. Two fixes for the price of one structural
  consolidation.
- **MEDIUM: `sync.py push` / `pull` ran git operations with no
  mutual exclusion.** Two concurrent `bettermemory sync` runs (push
  racing push, or push racing pull) interleave their `git add` /
  `commit` / `pull --rebase` with nothing serializing them. Fix:
  both functions now hold `flock_excl(root / ".sync")` for the
  duration of the git operation sequence, making each sync op an
  atomic boundary against the other. The lock covers pull's reindex
  too so the FTS5 rebuild sees the same on-disk state the rebase
  landed. Pull's error message gained the `git rebase --abort`
  recovery hint for the crash-mid-rebase case. *Known limitation
  (corrected post-release): this lock does NOT coordinate against
  the in-process `Store`.* `Store.write` holds a per-memory-file
  lock on a different inode, so a `Store.write` landing mid-`git
  add -A` can still stage a half-written file-set (at worst one
  commit stale; the next sync corrects it). True sync↔Store
  coordination would require `Store`'s mutators to take the
  `.sync` lock too — a global write-serialization tradeoff
  deferred as a separate decision.
- **HIGH: LLM providers (Ollama, OpenAI) had no output-token cap.**
  Ollama call had no `num_predict` in `options`; the OpenAI
  provider passed no `max_tokens` (while Anthropic had carried
  `max_tokens=2048` from the start). A runaway local model can
  return arbitrarily many tokens; httpx buffers the whole body
  before `.json()` so the consolidate process OOMs on the response
  side. Fix: shared `DEFAULT_MAX_OUTPUT_TOKENS = 2048` enforced
  on all three providers. Plus a new `LLMResponseTruncated`
  exception raised when the provider signals it hit the cap
  (`done_reason="length"` / `stop_reason="max_tokens"` /
  `finish_reason="length"`) — pre-2.6.4 the truncated JSON
  silently fell through `parse_and_validate` as malformed, hiding
  the real root cause from the operator. Now the consolidate
  report surfaces "raise the cap or split the cluster" explicitly.
- **MEDIUM: `store.prune_tombstones` and `store.tombstone`
  concurrency.** `prune_tombstones` read+stat+unlink each
  tombstone WITHOUT the per-file lock — a concurrent `restore(id)`
  race could either un-tombstone or double-unlink. Fix: wrap the
  per-tombstone read/unlink in `_locked(path)`. Separately, the
  tombstone-naming TOCTOU (`if target.exists(): target = ...
  ULID-suffixed`) is killed by always using the ULID-suffixed
  filename — unique by construction, no race possible. Existing
  unsuffixed tombstones on disk continue to load (the reader keys
  off the `id` field, not the filename).
- **MEDIUM: `index.py:_ensure_schema` downgrade ran outside a
  transaction.** On schema-version-down, `DROP TABLE` then
  `CREATE TABLE` ran in autocommit. A parallel connection
  opening between the drop and the create saw a schema with no
  `memories` table and SELECTs failed (no BUSY raised because
  the table simply wasn't there yet). Fix: wrap drop+recreate
  in `BEGIN IMMEDIATE` ... `COMMIT` with rollback on exception.
- **MEDIUM: `semantic.flush_persistent_cache` cache flush race.**
  Two MCP servers in the same memory dir would both write
  `<root>/.embeddings.npz.tmp` and race the rename, last-writer
  -wins corrupting whichever lost. Plus no `fsync_dir`, no
  `chmod 0o600` (vector representations of memory bodies have
  the same privacy bar as the source memories). Fix:
  process-unique tmp name (`.tmp.<pid>`), `flock_excl` around
  the rename, `os.chmod(_PERSISTENT_PATH, 0o600)` post-rename,
  `fsync_dir` post-chmod.
- **MEDIUM: `events.py` chmod 0o600 failure silently suppressed.**
  `contextlib.suppress(OSError)` on the chmod meant a failure
  left the log world-readable with no signal. Fix: log WARNING
  on failure so the operator can investigate (typical causes:
  noexec/nosuid container mounts, restricted filesystems).
- **MEDIUM: `semantic.cosine_similarity_normalized` truncated to
  shorter input on dimension mismatch.** `zip(a, b)` over
  different-length vectors produced a similarity over the
  overlap only — meaningless number that still passed the
  threshold. The case fires when a persistent cache from one
  embedding model is read against another (config swap without
  `flush_persistent_cache`). Fix: `zip(a, b, strict=True)`
  raises `ValueError` on dimension mismatch.
- **MEDIUM: LLM merge apply had no partial-failure recovery.**
  `consolidate.apply_llm_proposal` updated the keeper's body
  then iterated `store.tombstone(dup_id)` for each duplicate.
  If the third of five tombstones failed, the keeper had the
  merged body while two duplicates were still active —
  retrieval would surface both the merged record and the
  unmerged duplicates. Fix: catch exception in the tombstone
  loop, roll back the keeper to its pre-merge body, then
  re-raise. The rollback is best-effort but the raise gives
  the operator a clean signal instead of silent half-done state.

### Fixed — pattern-generalization (event consumer fallbacks)

- The 2.6.3 fix added tolerant `event.get("returned") or
  event.get("memory_ids")` reads to `llm.py` only. Five other
  consumers (`_handlers.py`, `_response.py`, `hook.py`,
  `consolidate.find_demotion_candidates`,
  `consolidate.find_cold_scopes`) read canonical-only — pre-2.6.3
  archived events on disk were silently dropped from those passes.
  All five now use the same canonical-first-then-legacy discipline.
  Same fix applied to `session` / `session_id` divergence:
  pre-2.6.4 hook wrote `session=`, handler writes `session_id=`,
  the Recorder auto-stamps `session`. All consumers now read
  `event.get("session") or event.get("session_id")`.

### Changed

- **`tests/test_eval.py:_ev()` helper no longer fabricates
  impossible session shapes.** Pre-2.6.4 the helper hardcoded
  `session: "sess-test"` regardless of any `session_id=` the
  caller passed; the resulting event had both fields
  disagreeing — a state production never produces (both fields
  derive from `state.session_id`). Helper now omits the
  `session` default when the caller provides either field.
  Migration to `_canonical_event` for legacy hand-built
  fixtures is follow-up work; the helper docstring directs
  new tests to the `event_log` fixture.

### Audit framing — why a fourth pass

After three audits in one day the question shifted from "are
there more instances of these bug classes?" to "why do these
bug classes keep producing new instances?" The structural fixes
above (single helpers for byte caps + flock + test fixtures)
make the *class* impossible — not "the next instance harder to
find." That's the differential from the 2.6.1 / 2.6.2 / 2.6.3
audit-pass approach, which was finding instances of named bug
classes one at a time. Two of four 2.6.3 bug classes (byte-vs-
char, field-name drift) are now structurally impossible at the
write site. Concurrency primitive duplication is gone (3× → 1×).
The fourth pass found one critical-severity live bug
(silent-miss flagship broken for hook traffic) that three prior
passes missed — confirming the audit-of-audit-of-audit
diminishing-returns thesis: more audits surface different bugs
not because they're more thorough, but because each pass's
attention budget runs out before exhausting the surface.

Suite: 1277 passed, 9 skipped (+32 tests vs 2.6.3, including
the structural-tripwire trio: byte-cap unit tests, EventLog
shape-pinning, lockfile fault-injection).

## 2.6.3 - 2026-05-21

**Audit-pass-of-the-audit-pass-of-the-audit-pass.** A third multi-agent
review of the 2.6.2 surface — this time scoped to "find the bugs the
last two audits' fix patterns should have generalized" — caught one
CRITICAL concurrency bug, two HIGH transcript-read DoS surfaces, and a
latent field-name drift in `llm.py` that is the same class as the
`find_demotion_candidates` bug 2.6.2 fixed in `consolidate.py`. Plus the
matching docs follow-ups. No on-disk format changes, no wire-shape
changes; one new constant and one new test surface.

### Fixed

- **`store.py:_locked` and `events.py:_locked` unlinked the lockfile
  inside `finally`, silently breaking mutual exclusion under
  contention.** The context manager opened `lock_path` with
  `O_CREAT`, `flock()`-ed the fd, then on exit unlocked → closed →
  **unlinked** the file. If process A unlinks the lockfile after B has
  already opened it but before C calls `os.open(lock_path, O_CREAT)`,
  B keeps its fd on the now-defunct inode and C creates a fresh one;
  flock identity is per-inode, so B and C then both believe they hold
  the lock. The bug was invisible under low contention (each lock
  acquire-and-release was serial within one process) but loosens to
  full lost-update territory under cross-process load — exactly the
  scenario `bettermemory sync` and any future multi-client HTTP/SSE
  posture introduces. Fix: drop the unlink; persist the 0-byte
  lockfile so every `os.open` sees the same inode. Comment in both
  files records the trade-off so a future "the lockfiles are clutter,
  let's clean them up" PR fails the review instead of the production.
- **`hook.py:_extract_last_exchange` read the entire transcript with
  no cap.** Same OOM class the 2.6.2 release fixed in
  `consolidate.py:_load_transcript` for the consolidate path, left
  unaddressed in the Stop-hook path. Claude Code transcripts grow
  monotonically over a session; in extended pairing sessions the JSONL
  reaches hundreds of MB, and the hook fires after every assistant
  turn. The old read+splitlines pattern allocated the whole file twice
  before the reverse walk even started. Fix: seek to the end and read
  the trailing `_TRANSCRIPT_TAIL_READ_BYTES = 1_048_576` bytes, then
  discard the first partial line (the next newline starts a complete
  record). The hook only needs the latest user+assistant pair, which
  always sits at the tail of an append-only log — the head bytes are
  dead weight. Unseekable streams (FIFOs from `mkfifo`-based fixtures)
  fall back to a bounded forward read; binary-mode read with
  `errors="replace"` decode handles UTF-8 codepoints split at the
  truncation boundary.
- **`consolidate.py:_load_transcript` counted characters, not bytes,
  despite the cap constant being named `_TRANSCRIPT_READ_CAP_BYTES`.**
  The 2.6.2 release added `fh.read(_TRANSCRIPT_READ_CAP_BYTES)` on a
  *text*-mode stream, which reads at most that many *characters*.
  Worst-case multibyte UTF-8 (4 bytes/char) read up to ~4 MiB into
  memory before the cap kicked in — defeating the "1 MiB hard cap"
  the comment claimed. Fix: open in binary mode, read raw bytes,
  decode with `errors="replace"` so a partial codepoint at the
  truncation boundary doesn't raise. Same byte-vs-char trap as the
  classic `max-length` validators in HTTP frameworks; the fix is one
  call-shape swap.
- **`llm.py:_collect_contradiction_targets` and
  `_build_cluster_member` read the wrong event field names.** Code
  checked `kind == "memory_search"` / `"memory_record_use"` and
  `event.get("memory_ids")`, but the canonical `Recorder` writes
  `kind="search"` / `"use"` with `returned=[…]` / `ids=[…]` (see
  `_handlers.py:1049` and `_handlers.py:2039`). **Same class as the
  `find_demotion_candidates` bug 2.6.2 fixed** in `consolidate.py`:
  the tests passed because the fixtures used the legacy field names,
  so the production-shape mismatch never surfaced under CI. Result:
  contradiction clusters silently always empty against real event
  logs; the LLM never saw the `contradicted` signal it relies on to
  judge whether a near-duplicate pair is actually in opposition. Fix:
  read the canonical names with a tolerant fallback to the legacy
  shape (mirroring 2.6.2's `event.get("returned") or
  event.get("hit_ids")` discipline), plus `event.get("session") or
  event.get("session_id", "")` to tolerate both auto-emitted and
  hand-rolled session keys. New regression test
  `test_build_clusters_seeds_contradiction_from_real_recorder` in
  `tests/test_llm.py` round-trips events through a real `Recorder` so
  a future drop of the canonical-name path fails at suite time — the
  same discipline the 2.6.2 demotion fix established.

### Changed

- **`SECURITY.md` — corrected the Web UI CSRF claim.** The hardening
  notes still described the pre-2.3.0 permissive header-less POST
  behavior ("Header-less POSTs fall through… refusing every
  header-less POST would break the normal in-UI flow"). 2.3.0 closed
  that path: `web.py:_same_origin` now returns False when both
  `Origin` and `Referer` are absent, and the existing `test_web.py`
  regression coverage pins it. The doc now reads "Header-less POSTs
  are rejected." with the CLI-scripting escape hatch (`-H "Origin:
  http://127.0.0.1:<port>"`) called out explicitly. Security
  documentation overstating an attack surface that has already been
  closed is worse than understating it, but only by a hair — the fix
  brings the prose in line with the code.
- **`CHANGELOG.md` — restored the missing `## 1.2.1 - 2026-05-10`
  heading.** Same defect 2.6.2 fixed for `## 2.6.0` (and the 1.3.2
  entry already noted for 1.3.0 itself). The 1.2.1 narrative flowed
  out of the 1.2.2 entry without a separator; renderers walking the
  heading hierarchy saw 1.2.2's `### Fixed` body continuing into the
  1.2.1 prose. **Pattern-recognition note** (load-bearing for future
  audits): three releases now have shipped without their `##` heading.
  Worth adding a CI lint that asserts every `## <version> -` heading
  has a matching `[project] version = "<version>"` entry in
  `pyproject.toml`'s history (or a release tag) so the next instance
  trips the suite instead of an audit pass.
- **`docs/ROADMAP.md` — version pin and test count.** "Where we are"
  header read `(May 2026, v2.6.0)`; bumped to `v2.6.3`. The
  `1234 tests` line was off by ~20 after the 2.6.1 / 2.6.2 / 2.6.3
  additions; replaced with `1200+ tests` so the next minor doesn't
  drift again from the same precise-number-rots-fast root cause.

## 2.6.2 - 2026-05-21

**Audit-pass-of-the-audit-pass.** A multi-agent re-audit of the 2.6.1
surface caught four real correctness gaps in the consolidate path that
the previous read-through missed, plus several doc-vs-behavior drifts
worth fixing while the surface was warm. No on-disk format changes, no
wire-shape changes — only existing-but-skipped checks getting wired up
correctly.

### Fixed

- **`consolidate.py:find_demotion_candidates` was reading the wrong
  event field.** The demotion pass keyed retrieval counts off
  `event.get("hit_ids")`, but `_handlers.memory_search` records the
  result-id list as `returned` — has done since the recorder shape
  stabilised. Every real event log silently scored zero retrievals,
  which means the demote-never-applied rule never proposed a single
  candidate against a production store. The unit tests passed because
  the synthetic fixtures used the legacy `hit_ids` field. Now reads
  `returned` with a `hit_ids` fallback for any pre-rename event logs;
  added a regression test (`test_consolidate.py`) that rounds through
  a real `Recorder` so a future drop of the `returned`-aware path
  fails at suite time.
- **`consolidate.py:_apply_llm_proposal` `propose_new` branch
  bypassed every write-time guardrail.** `memory_write` runs
  `find_similar` (active + tombstones), `find_transient_markers`, and
  the new 2.6.1 `max_content_bytes` cap before committing — but the
  LLM-proposed branch went straight to `store.write` with none of
  them. Since the LLM only sees ~8 cluster members as "don't
  duplicate these" context, dedup against the full active set is
  load-bearing: without it, `consolidate --llm --from-transcript`
  would happily re-create memories the user already wrote (or
  already removed). All four gates now fire before the write; gate
  failures raise `RuntimeError` which `consolidate_llm` already
  records as `LLMClusterFailure`, so the operator sees the rejection
  reason in the report.
- **`consolidate.py:_load_transcript` had no read-size cap.** The
  whole transcript file was `path.read_text()`-ed before any
  truncation — same unbounded-input class 2.6.1's `max_content_bytes`
  work closed for memory bodies. A multi-GB transcript would OOM the
  process. Now caps the read at 1 MiB via a single `f.read(N)` on the
  text stream; the downstream prompt builder still truncates again
  at `MAX_TRANSCRIPT_CHARS` (12 KB).
- **`llm.py:_validate_propose_new` didn't call `validate_scope`.** A
  syntactically-bad scope (e.g. `"foo bar"`, anything outside the
  lowercase-alphanumeric-plus-hyphens-and-colons grammar) passed
  validation and crashed at apply time — *after* the user had
  already seen and accepted the `+ NEW MEMORY` diff. Now uses the
  same `validate_scope` helper `memory_write`'s payload validator
  does, so bad scopes are rejected before the diff renderer sees
  them.
- **`store.py` — three bare `except Exception:` narrowed.**
  `_find_tombstone_path_for_id` (line 600), `rename_scope`'s
  tombstone branch (line 845), and `_find_path_for_id` (line 952)
  were catching every exception from `frontmatter.load`, including
  ones that should propagate (e.g. `MemoryError` on a pathological
  file). Tightened to `(ValueError, KeyError, OSError)` to match the
  rest of the file's convention. Tests unchanged; no behavioral
  difference on the well-formed-frontmatter happy path.
- **`examples/memories/*.md` were silently broken.** All three
  placeholder IDs (`01HXYZTUTORIALSTYLEEXAMPLE`,
  `01HXYZHOMELABNETWORKEXAMPL`,
  `01HXYZPROJECTFOOSTACKEXAMP`) contained `I` / `L` / `O` / `U`
  characters, which the Crockford-base32 `_ULID_RE` rejects. `Memory()`
  validation failed and `Store.load_all` swallowed the `ValidationError`
  — so following the README's "drop these into `~/.claude-memory/`"
  instruction produced an empty store with no error visible to the
  user. Regenerated three valid IDs via `generate_ulid()`.

### Changed

- **`CHANGELOG.md` — restored the missing `## 2.6.0 - 2026-05-21`
  heading.** The 2.6.1 insertion landed without recreating the
  separator between releases, so the 2.6.1 "Fixed" bullets flowed
  directly into the 2.6.0 body text. Cosmetic but unambiguously
  wrong for changelog consumers (rendered HTML, parsers that walk
  the heading hierarchy). Also added the missing 2.6.1 "Fixed"
  bullet for commit `00521d5` — the deleted-CWD Stop-hook fix
  shipped in 2.6.1 but never made it into the changelog entry — and
  scoped the 2.4.0 system-dirs claim to clarify Windows behaviour.
- **`SECURITY.md` — reworded the YAML claim.** "No `yaml.load`
  anywhere" was an overclaim — `_frontmatter.py:100` does call
  `yaml.load(..., Loader=yaml.SafeLoader)`. Same safety property,
  but the literal sentence was wrong. Now reads "every `yaml.load`
  call pins `Loader=yaml.SafeLoader`" plus the 64 KB pre-flight cap
  noted in the parser as belt-and-suspenders.
- **`CONTRIBUTING.md` — inlined the macOS `UF_HIDDEN` explanation.**
  The previous text cross-referenced a README "macOS gotcha"
  section that doesn't exist. Now self-contained, with the
  `chflags nohidden .venv` recovery command for an already-flagged
  directory.
- **`docs/installation.md` — added the `[embeddings-fast]` extra.**
  Shipped in 2.5.0 but the install page only listed `[embeddings]`
  and `[ui]`. New entry calls out the PyTorch-vs-ONNX trade-off (~500
  MB vs ~50 MB on disk) and the `[behavior] semantic_provider` knob
  for selecting fastembed explicitly when both extras are installed.
- **`_handlers.py:DESC_MEMORY_WRITE`** — the documented success
  status was `"ok"` but the actual emitted value is `"committed"`
  (`_response.committed` has been stable on `"committed"` since 2.x).
  Models branching on the documented value got a no-match. Now
  matches the emission.
- **`server.py` module docstring** — Curation list extended to
  include `memory_audit_turn`. Was 17 of 18; registration was always
  correct, the docstring was stale.

## 2.6.1 - 2026-05-21

**Audit-pass follow-up.** A read-through of the 2.6.0 surface
surfaced one defensible bug, two defence-in-depth gaps, an unbounded
input on `memory_write`, and a smoke test that was conflating benign
lifecycle events with the failure mode it was meant to catch. None of
the changes touch the on-disk format, the wire shape, or any contract
the model branches on; older callers see byte-stable behaviour.

### Added

- **`[behavior] max_content_bytes` write-time cap (default 1 MB,
  `0` disables).** Closes the only unbounded-input surface left after
  the YAML / note / origin trust-boundary work in 1.x. The event log
  already rotates at 10 MB, but the memory file itself was previously
  unbounded — a runaway model or hostile client could fill disk with a
  multi-gigabyte body. `memory_write` and `memory_update` now share a
  `_validate_content_size` helper that measures encoded UTF-8 byte
  length (the unit that actually lands on disk and in the JSONL log,
  not character count) and raises a clear `ValueError` past the cap.
  Existing on-disk memories are never re-validated, so raising the cap
  downward never rejects already-stored data.

### Changed

- **`orphan_use_events` is now a clean fabrication smoke test.** The
  rollup previously incremented on every `record_use` referencing an
  id not in the active store — which conflated benign
  tombstone-after-use lifecycle events with model hallucination. The
  CLAUDE.md / health output advised treating a growing count as "model
  is hallucinating ids", but in practice the count was dominated by
  legitimate post-tombstone references. `compute_health` now accepts
  an optional `tombstoned_ids` set; ids in that set are filtered out
  of the orphan count. `report_for_directory` passes the live
  tombstone set from `store.load_tombstones()`, so production callers
  via the MCP tool and CLI subcommand get the sharpened signal.
  Callers that don't pass `tombstoned_ids` see the legacy conflated
  count — backward compatibility for offline tooling that builds
  events without a live store.

### Fixed

- **`store.py:Store.__post_init__` — explicit `mode=0o700` on the
  tombstone directory.** Previously `mkdir(exist_ok=True)` relied on
  the caller's umask for owner-only permissions. On a system with a
  loose umask (0o027 or higher), the tombstone directory could be
  group- or world-listable. The active memory dir has always been
  owner-only via the per-file 0o600 fanout; the tombstone dir now
  matches at the directory level too. Tombstones carry the same trust
  boundary as active memories (paths in `removed_reason`, body hashes
  for dedup), so directory listing should require the owner.
- **`web.py` `/memories?scope=…` query param now validates.** The
  scope query param fed straight into `store.list_summaries` with no
  validation — not an injection vector (set-intersection on scope
  strings, no SQL exposure), but inconsistent with the MCP handlers
  that all call `validate_scope`. A malformed scope (e.g.
  `?scope=../etc/passwd`) silently returned an empty list, masking the
  user's typo as "no results". The route now returns a clear 400 with
  the same error message MCP handlers produce.
- **`_response.py:_attach_commit_drift_to_hits` — defensive `.get()`
  on `path_drift_missing` during late verdict recomputation.** The
  late recompute reads `hit_dict["path_drift_missing"]` set earlier by
  `hit_to_dict`. The dependency was safe today but invisible across
  function boundaries; a future refactor that changed when the field
  attached would `KeyError` retrieval. Now uses `.get("…", 0)` — costs
  nothing, removes the implicit invariant.
- **`attribution.py:_SENTENCE_SPLIT_RE` docstring.** The comment
  claimed the trailing-space requirement avoided breaking
  abbreviations into pseudo-sentences. It only achieves that for
  decimal numbers (`1.5`) and version strings (`v2.6.0`) where the
  dot is followed by a digit; prose abbreviations like `Dr. Smith` or
  `e.g. foo` do split. The over-split is accepted by design (the same
  boundary is tested from the other side, so attribution survives the
  loss of one fragment), but the comment was misleading and would
  have steered a future maintainer toward the wrong fix. Comment
  rewritten to match what the regex actually does.
- **`hook.py` Stop-hook tolerates a deleted CWD.** The audit path
  read `Path.cwd()` so the per-turn event-log walk could attach an
  `origin` field; if the user `rm -rf`-ed the project directory mid-
  session (or any other producer of `FileNotFoundError` /  `OSError`
  on `getcwd`), the hook would tear down the whole session instead of
  just dropping the attribution. Now: catch `(FileNotFoundError,
  OSError)` and continue with `origin=None`. The Stop hook is best-
  effort; one cwd-resolution failure shouldn't kill the rest of the
  attribution pass.

## 2.6.0 - 2026-05-21

**Three writing-reflex / audit-attribution levers that close the gap
between the verification contract and what the model actually does.**
The MCP contract asks the model to attach `claim_excerpts` on explicit
`memory_record_use`, to spot-check claims when the staleness verdict
isn't fresh, and to call `memory_write` whenever something durable
enters the conversation. In dogfood the model defaults to the cheap
auto-commit path on all three — `memory_helped_rate` reads 0%, the
spot-check ceremony asks the model to recompute what the server
already knows, and most durable facts never get written. This release
closes each gap by moving the load-bearing work off the model:
the server already knows which paths drifted, the Stop hook already
sees the assistant reply, and `consolidate --llm --from-transcript`
already has the conversation. None of the three change the surface
contract; they just stop asking the model to do something the system
is in a better position to do itself.

### Added

- **`path_drift` lists inline on every search hit.** The search
  pipeline already runs `detect_path_drift` inside `_build_hit` but
  was discarding the actual path lists, keeping only the integer
  counts. The model got a non-fresh `staleness_verdict` with no
  actionable handle — its only options were `memory_show` round-trips
  or manually re-scanning the snippet. New `MemoryHit` fields
  (`path_drift_checked_paths` / `path_drift_missing_paths` /
  `path_drift_verified_paths`) carry the `PathDriftReport`'s lists
  through and the search response surfaces them under
  `path_drift = {checked, missing, verified}` — the same key shape
  `memory_show` already uses. A `spot_check_recommended` hit with
  `path_drift.missing = ["src/auth/middleware.py"]` is now directly
  actionable: `memory_update` the rotted bit or `memory_verify` the
  rest, no round-trip needed. Side effect: `_build_hit` now passes
  `verified_paths` to `detect_path_drift` (it wasn't before — the
  `verified` field of the report was always empty on search hits, bug
  fix). The spot-check ceremony language across
  `DESC_MEMORY_SEARCH`, `SYSTEM_PROMPT_ADDENDUM`,
  `plugin/skills/bettermemory/SKILL.md`, and `docs/api.md` updated:
  the previous contract asked the model to recompute what the server
  already knew; the new contract reads the missing-paths list
  directly.
- **Stop-hook post-hoc `claim_excerpt` attribution.** New
  `attribution.py` module runs a precision-tuned substring match
  (sentences ≥6 tokens AND ≥30 chars, stopword-filtered,
  case- and whitespace-normalised) between recently-retrieved memory
  bodies and the assistant reply text. When a body sentence appears
  in the reply, the Stop hook emits one `record_use` event per
  (memory, matched-sentence) pair with `outcome="applied"`,
  `auto=false`, `attribution="hook"`, and the matched phrase as the
  `claim_excerpt`. New `attribution` field on `use` events with three
  tiers — `model` (explicit by AI), `hook` (substring-match), `auto`
  (the fallback). Older events without the field fall back at read
  time (`auto=false → model`, `auto=true → auto`). `_advance_turn`
  reads recent events at the start of each `memory_*` call, purges
  hook-attributed ids from the pending map, and skips the
  auto-commit pass for them — so each retrieval generates exactly one
  `applied` event (hook, model, or auto). The hook also filters
  memory_ids that already have any `use` event in the lookback window
  (600s default), so a model that DID record use explicitly doesn't
  get a redundant hook attribution. `bettermemory eval` and
  `docs/eval.md` updated to describe the tier; the three-way split
  surfaces in the `applied_total` / `applied_explicit` counts so
  consumers can recompute against a stricter "model only" definition.
  Tests: 11 new in `tests/test_attribution.py`, 3 new in
  `tests/test_hook.py`, 1 new in `tests/test_server_v12_features.py`.
- **`bettermemory consolidate --llm --from-transcript PATH`.** The
  MCP contract asks the model to call `memory_write` whenever
  something durable enters the conversation; in practice the bar for
  "durable" is fuzzy and head-down task focus wins. The new flag
  reads the conversation after the fact (plain text, Markdown, or
  Claude Code session JSONL — autodetected by extension) and asks
  the LLM to propose new memories worth saving. Fifth proposal type
  `propose_new(scope, category, body, source_excerpt, rationale)`
  joins the existing four (merge / resolve_contradiction /
  rewrite_relative_date / demote_tier) under the same audit gate —
  every proposal renders as a "+ NEW MEMORY" diff preview, `--apply`
  requires either `--yes` (batch accept) or an interactive y/N
  prompt. Hallucination defences fire: `scope` must be non-empty and
  not "general" (the catch-all); `category` must be `fact` or
  `ambient` — never `user-inference` (that tier requires explicit
  user confirmation the consolidate path can't supply);
  `source_excerpt` is required and capped at 500 chars; `body` must
  be non-empty. The excerpt is stamped into the new body as a
  provenance line so future audits trace each claim back to a
  transcript turn. Existing memories (most-recently-updated, capped
  at 8) ride along as the "don't propose duplicates of these"
  context. Cluster shape extended with optional `transcript: str |
  None` and `cluster_kind="transcript_facts"`; existing cluster types
  unaffected. Tests: 9 new in `tests/test_llm.py`, 6 new in
  `tests/test_consolidate_llm.py`. Full suite: 1234 passed, 9
  skipped.
- **`docs/incidents/` postmortem scaffold.** A public-postmortem
  directory for reported memory-rot bugs — cases where the
  verification trifecta (calendar age + path drift + commit drift)
  missed a stale claim or fired on a fresh one. Competing memory
  systems don't surface their drift bugs because their architecture
  doesn't expose drift to begin with; bettermemory's contract puts
  the verdict in every retrieval response, so we owe a public
  accounting when the verdict was wrong. `README.md` explains
  why-and-how-to-file; `TEMPLATE.md` is the fillable shape (Symptom
  / Root cause / Fix / Verification / What the surface should do
  differently). Index is empty until the first report lands.

## 2.5.0 - 2026-05-20

**Verification-grade memory lane: positioning + eval CLI + recall fix +
Dreaming defense.** The verification-first rebrand, the `bettermemory
eval` CLI (`memory_helped_rate`, `endorsement_rate`, `silent_miss_rate`),
the `[embeddings-fast]` extra that closes the recall objection without
PyTorch, AND the `bettermemory consolidate --llm` Dreaming-defense pass
that proposes merges / contradiction resolutions / relative-date rewrites
/ tier demotions from a local Ollama model — refusing to commit any of
them without your explicit accept. The narrative phrase: *Anthropic's
Dreaming consolidates invisibly; bettermemory's `--llm` shows every
proposed diff and refuses to commit without your accept.*

### Added

- **`bettermemory consolidate --llm`: LLM-driven consolidation pass.**
  Extends the existing four offline passes (dedup, demote-never-applied,
  cold-scope, scope-typo) with a fifth that clusters related memories
  and asks an LLM to propose four kinds of mutation: `merge` (combine
  near-duplicates into a single keeper, tombstone the rest),
  `resolve_contradiction` (pick a winner from two memories that
  disagree, tombstone the loser), `rewrite_relative_date` (substitute
  absolute dates for "today" / "last week" phrases, with today's date
  passed via the prompt so the model doesn't infer it from stale
  training data), and `demote_tier` (retag `fact` -> `ambient` when
  the verifiable claim has been superseded). The `--llm-provider` flag
  picks between `ollama` (default — local HTTP on port 11434, no
  egress, no API key), `anthropic` (env `ANTHROPIC_API_KEY`, lazy-
  imports the `anthropic` SDK), and `openai` (env `OPENAI_API_KEY`,
  lazy-imports `openai`). Dry-run by default; `--apply` requires
  *either* `--yes` (batch accept) or an interactive TTY (per-proposal
  prompt). Hallucinated memory IDs (LLM produces a memory_id not
  in the cluster) are rejected at validation time *before* the diff
  renderer sees them. New module `src/bettermemory/llm.py` carries the
  proposal dataclasses, provider abstraction, prompt builder, validator,
  cluster builder (union-find on near-duplicate pairs + contradiction-
  event seeding), and the unified-diff renderer; `consolidate.py` gains
  `consolidate_llm()`, `_apply_llm_proposal()`, and the
  `LLMConsolidateReport` / `LLMProposalAction` / `LLMClusterFailure`
  dataclasses. 38 tests across `tests/test_llm.py` and
  `tests/test_consolidate_llm.py` cover validation, hallucination
  rejection, the apply gate, each proposal-type application, and the
  per-cluster failure-isolation contract.
- **`[embeddings-fast]` extra: fastembed + ONNX Runtime.** Same
  retrieval surface as `[embeddings]` (sentence-transformers), a tenth
  the install size. Default model `BAAI/bge-small-en-v1.5` (384-dim,
  ~33 MB ONNX) mirrors the dimensionality of `all-MiniLM-L6-v2` so
  cosine thresholds remain comparable. `[behavior] semantic_provider`
  picks between providers: `"auto"` (default) prefers torch when both
  installed (existing `.embeddings.<model>.npz` caches stay
  byte-stable), then fastembed, then Jaccard fallback. Explicit
  `"torch"` or `"fastembed"` honoured even when the extra isn't
  installed — the per-provider WARNING surfaces the missing-extra
  hint. Persistent cache namespaces by provider:
  `.embeddings.<model>.npz` (torch, legacy layout) vs
  `.embeddings.fastembed.<model>.npz` so flipping providers produces
  a fresh file rather than mixing incompatible vectors. CI gains a
  `test-embeddings-fast` job pinned to Python 3.13 (fastembed wheels
  lag 3.14); see `pyproject.toml` for the matching
  `no_fastembed` / `no_torch_embeddings` pytest markers.
- **`bettermemory reindex --embeddings`.** After rebuilding the FTS5
  index, re-embed every active body into the persistent cache.
  Provider+model-namespaced, so a config swap from torch to fastembed
  needs warming the new cache file — this is the surface for that
  warming pass. Reports `embedded` count, resolved provider/model,
  and the cache path on success; clean exit with an actionable
  message when the provider isn't available.
- **`bettermemory eval` CLI subcommand**. Reads `iter_all_events`
  output plus the active store, joins on memory id, and reports the
  three rates with Wilson 95% confidence intervals. Flags:
  `--since {N{s|m|h|d}|all}` (default `30d`, mirroring
  `verification_stale_days`), `--scope SCOPE`, `--min-retrievals N`
  (default 5, shared with `health._ENDORSEMENT_DEBT_MIN_RETRIEVALS`),
  `--silent-miss-limit N` (default 20), `--json`. Text renderer
  includes the rates, the endorsement-debt rows, and the recent
  silent-miss candidates; JSON renderer carries every count + CI
  bound for CI pipelines. Pure compute layer in
  `src/bettermemory/eval.py` (`compute_eval`, `parse_since`,
  `_wilson_interval`, `render_text`); 52 tests in
  `tests/test_eval.py` cover each numerator/denominator path,
  scope/since filtering, the ambient + tombstoned + has-explicit
  endorsement-debt exclusions, the silent-miss buffer cap, and a
  CLI smoke run.
- **`docs/eval.md`** defines the three rates publicly so they're
  citable by any system that exposes the right telemetry, not just
  this one. Includes the 2×3 healthy-vs-pathological matrix,
  comparison to LongMemEval, the CLI shape, and the calibration
  caveats (the `v1_top1_high` threshold rule's behaviour on real
  distributions is the open question).
- **`docs/blog/memory-is-rotting.md`** standalone post draft on the
  motivating problem (auth-middleware example), the staleness-verdict
  trifecta, claim-level audit, and the endorsement-debt category.
  Designed for HN / Lobsters / r/LocalLLaMA discussion.
- **`docs/ROADMAP.md`** publicly commits the next four work items
  (optional fastembed embeddings, the eval CLI shipped here, local
  `consolidate --llm` Dreaming-equivalent, Claude Code auto-memory
  ingest bridge) and the deliberately-out-of-scope list (managed
  cloud, multi-user RBAC, graph backend, non-MCP SDK, LongMemEval
  leaderboard chase).

### Changed (positioning, no behaviour change)

- **README, pyproject `description`, plugin marketplace, plugin
  README, and SKILL frontmatter rebranded around verification.**
  Hero line is now "Memory you can verify"; the comparison table
  bolds the four rows where bettermemory uniquely runs (per-hit
  staleness verdict, claim-level audit trail, user-inference
  confirmation tier, endorsement-debt visibility); the Features
  list leads with a "Verification surface" section. The previous
  "persistent memory for Claude Code, retrieved on demand" framing
  remained accurate but didn't differentiate from a now-commoditized
  local-MCP memory market (claude-mem at 65k stars, a dozen
  SQLite-FTS5 clones, Anthropic's free vendor-native auto-memory +
  Dreaming). The verification surface is the lane no funded
  competitor (Mem0, Zep, Letta, Cognee, Supermemory, claude-mem)
  occupies; the rebrand makes that legible at the PyPI/GitHub
  surface.

## 2.4.0 - 2026-05-20

**Path-drift extractor: narrow single-segment routes.** A bugfix
release, but tagged minor because it changes the path-candidate set
the `path_drift` signal acts on — consumers tracking
`path_drift_missing` counts will see lower numbers on bodies that
document URL routes inline.

### Fixed (correctness)

- **`/verify`-shaped URL routes no longer flagged as missing
  filesystem paths.** The path extractor in `verify.py` was treating
  backtick-wrapped single-segment absolute paths (`/verify`,
  `/healthz`, `/login`, `/api`) as filesystem candidates and
  stat'ing them. They reliably failed the stat and surfaced as
  `path_drift_missing` on every retrieval of any memory whose body
  documents a route-typed API. The canonical bite: the bettermemory
  memory documenting the 2.0.0 web UI fix ("Web UI ``/verify`` POST:
  CSRF Origin check and length cap.") produced a phantom drift
  signal on every search. New helper
  `verify._is_single_segment_routelike` rejects extensionless
  single-segment absolute paths at extraction time. Multi-segment
  paths (`/Users/...`, `/etc/foo.conf`), home-relative paths
  (`~/...`), Windows paths, and extensioned single-segment paths
  (`/foo.txt`) are unaffected. Bare top-level system dirs (`/etc`,
  `/var`, `/usr`) get filtered too — on POSIX they exist by
  definition so the filter is a no-op for real drift, and on Windows
  they don't exist as bare roots so filtering them strips a false-
  positive at zero cost. Five regression
  tests in `tests/test_verify.py` cover the production bite, the
  broader route class, and the unaffected-by-narrowing edges.

## 2.3.2 - 2026-05-20

**Polish release.** One model-facing terminology fix on top of
2.3.1, plus dependabot bumps on the release workflow's action
versions. No code-logic, schema, or surface changes.

### Fixed (housekeeping)

- **`DESC_MEMORY_RECORD_USE` terminology drift.** The first sentence
  said "auto-recorded" while the next sentence and every other
  surface (docs, SKILL.md, this CHANGELOG) said "auto-committed".
  Model-facing string, so consistency lands every Claude session.
  Aligned to "auto-committed".

### Chores

- Bump CI release-workflow actions: `actions/upload-artifact` v4→v7,
  `actions/download-artifact` v4→v8, `softprops/action-gh-release`
  v2→v3. Workflow-only; no behavior change for downstream users.

## 2.3.1 - 2026-05-20

**Audit-pass follow-up.** 2.3.0 cut as a single rebased push of 12
audit commits — the chain never ran CI individually, so the first
push to main failed format check (16 unrun files) and the format
fix-up exposed a mypy regression in the new lock-discipline tests.
This patch closes the deeper bugs the rebase-squash papered over.
All fixes are correctness-only; no surface or schema changes.

### Fixed (correctness)

- **`rename_scope` tombstone branch TOCTOU.** The 2.3.0 lock-reads
  fix covered the active-side branch but left the tombstone branch
  reading `frontmatter.load(tpath)` outside the lock. A concurrent
  `restore` could land between the read and the write and have its
  rewrite clobbered. The read now lives inside the same
  `_locked(tpath)` block as the write. Regression test in
  `tests/test_store_locking.py` traces `fm_load` against the
  tombstone path's `_locked` window.

- **Index `filename` column wrong for collision-suffixed files.**
  The schema-v2 id → filename lookup derived its filename via
  `_filename_for(memory)`, which only knew `(created, slug)` — no
  collision suffix. Two memories sharing a date+slug had their
  index rows both pointing at the unsuffixed file; a search hit on
  the second memory resolved to the first memory's body, tripped
  the `memory.id != cid` defense, and got dropped. Fix threads the
  actual filename through `index.upsert(..., *, filename=)`,
  `index.rebuild(..., items: Iterable[tuple[Path, Memory]])`, and
  `_index_upsert_quietly(..., *, filename=)`. New helper
  `Store.iter_active()` yields `(path, memory)` pairs for the
  rebuild path. Regression test in `tests/test_index.py`.

- **`_load_search_candidates` empty-loaded fallback.** When the
  FTS pre-filter returned candidate ids but every filename lookup
  missed (pre-v2 schema rows, every match tombstoned mid-search,
  etc.), the handler returned an empty list — the comment in
  `_filename_for` had claimed the `load_all` fallback covered that
  case; it didn't. The fallback is now actually wired up. Regression
  test blanks every filename column in the index and confirms
  search still surfaces the matching memory.

- **Stop-hook event missing `assistant_present`.** `hook.run_audit`
  accepted `assistant_response: str | None` but never wrote it to
  the `turn_audited` event, while the in-process
  `memory_audit_turn` handler did emit the flag. Downstream rollups
  joining the two event sources saw an inconsistent field shape.
  Hook now mirrors the handler. Regression tests cover both
  branches (text block present → True; thinking/tool_use only →
  False).

### Fixed (housekeeping)

- **`index._connect` style cleanup.** `import contextlib` and
  `import os as _os` moved from inside the function to module top;
  `except BaseException` narrowed to `except Exception` so a
  `KeyboardInterrupt` / `SystemExit` during connect setup doesn't
  get swallowed silently.

- **`test_store_locking.py` mypy regression** (already in main via
  `1f72222`). The fixture patched `store_module.frontmatter.load`,
  which tripped mypy's strict re-export rule since
  `store.py` aliases the module via `from . import _frontmatter as
  frontmatter`. Switched the patch target to the already-imported
  `_fm` alias.

## 2.3.0 - 2026-05-20

**Production-readiness audit pass.** 12 commits closing ~28 audit
findings across correctness, security, performance, and the
model-facing surface. The release is cut as a minor bump because
the FTS5 index schema goes from v1 to v2 (drop-and-rebuild on first
launch, transparent), a new `bettermemory audit-turn` CLI subcommand
ships, and the plugin manifest now declares a Stop hook that fires
silent-miss telemetry on every assistant turn. Schema_version on
memory files stays at 1 — no on-disk format change.

### Fixed (correctness)

- **TOCTOU race in mutation paths.** `mark_verified` / `tombstone` /
  `restore` / `rename_scope` previously read the target file before
  acquiring the file lock, opening a window where a concurrent
  `update` from `web.py` or `sync.py` could be silently clobbered.
  Reads now happen inside the same `_locked()` block as the write.
- **FTS5 index drift on `rename_scope` and `restore`.** Renaming a
  scope wrote the new list to disk without updating the index's
  `scopes_text` column; BM25 ranking on the renamed scope read
  against stale text until the next manual reindex. `restore` was
  missing the index upsert entirely — restored memories were absent
  from indexed search until reindex. Both paths now call
  `_index_upsert_quietly` after the file write.
- **Consolidate failures aggregated, not silently swallowed.** The
  dedup / demotion apply loops logged per-failure warnings but
  never rolled them up; a run hitting 10 disk-full errors scrolled
  past the user's terminal with no summary signal. Added
  `ConsolidateFailure` plus a `failures` list on `ConsolidateReport`;
  both the text and JSON renderings now show the rollup.
- **SQLite connection leak on PRAGMA failure.** `index._connect`
  could leak the open `sqlite3.Connection` when a PRAGMA raised
  mid-setup (corrupt or zero-byte DB). Surfaced as
  `ResourceWarning: unclosed database` in two tests. Now wraps the
  post-connect setup in a try/except that closes on failure.

### Added (security)

- **Sync stderr redaction in push/pull error paths.** The default
  `_run_git` path already scrubbed credentialed URLs through
  `_redact_text`; the `push` and `pull` paths built their own
  `SyncError` from raw stderr to attach actionable hints, and that
  branch leaked the URL. Both now wrap the surfaced text.
- **Symlink rejection in store iteration.** `_iter_active_paths`
  and `_iter_tombstone_paths` previously called `entry.is_file()`,
  which follows symlinks. A hostile remote pushing `something.md`
  as a symlink to an arbitrary readable file would have its target
  loaded and parsed on the next `load_all`. Now skips
  `is_symlink()` entries.
- **CSRF header-less POST rejection.** `web._same_origin` accepted
  state-changing POSTs that arrived without `Origin` or `Referer`
  headers on the rationale that some browsers strip Referer. In
  practice modern browsers send Origin reliably; a header-less
  POST is a non-browser tool (`curl -X POST`) hitting the endpoint
  directly. Header-less POSTs are now rejected; CLI scripts that
  drive the UI should set `-H "Origin: http://127.0.0.1:<port>"`.
- **YAML body-size cap in frontmatter parser.** `_frontmatter.loads`
  uses YAML `SafeLoader`, which protects against `!!python/object`
  but not against alias-expansion DoS (the "billion laughs" pattern).
  The store widens its trust boundary once `sync pull` is in use
  (a remote can write into the memory directory); a 64 KB pre-flight
  check now rejects oversized frontmatter before `yaml.load` sees it.
- **`note` field length cap on memory_verify / memory_record_use.**
  The web `/verify` endpoint already capped `note` at 500 chars; the
  MCP entry points didn't, leaving a hostile-client surface to
  inflate the JSONL event log. Same 500-char ceiling now enforced
  on the MCP side.
- **Git argv validation.** `sync.init` / `push` / `pull` validate
  `remote` and `default_branch` against `^[A-Za-z0-9][A-Za-z0-9._/-]*$`
  before passing positionally to git. Belt-and-suspenders against a
  value like `--exec=evil` being parsed as a flag in older gits.
- **`--no-tags` on `git pull --rebase`.** Hostile / sloppy remotes
  pushing refs under `refs/tags/` would otherwise be mirrored into
  the local `.git/refs/tags/`; a tag named `main` could shadow the
  branch on a later checkout.
- **0o600 permissions on data files.** Memory `.md` files, the
  event log `.events.jsonl`, the SQLite index and its WAL/SHM
  siblings all inherit the user umask (typically 0o644 — world-
  readable on default Linux/macOS). Lock files already used 0o600;
  the data path now matches. Best-effort; no-op on Windows.
- **System-directory warning on misconfigured `BETTERMEMORY_DIR`.**
  `config.resolved_directory` now logs a WARNING when the resolved
  path lives under `/etc`, `/usr`, `/bin`, `/sbin`, `/boot`, `/dev`,
  `/proc`, or `/sys`. Catches the typical footgun where someone
  typed a system path by mistake; `/var` is intentionally excluded
  because macOS routes the per-user tmp through `/var/folders/...`.

### Added (features)

- **`bettermemory audit-turn` CLI subcommand** wraps the silent-
  miss audit (previously only the `memory_audit_turn` MCP tool) for
  client-side hook invocation. Reads the Claude Code Stop-hook
  stdin JSON (session_id + transcript_path), parses the transcript
  to find the latest user message, and runs `probe_for_miss`
  against the active store. Always exits 0 by design so a hook
  misfire never breaks the turn-end pipeline.
- **Plugin Stop hook** in `plugin/hooks/hooks.json` declares the
  binding: `uvx bettermemory audit-turn --quiet || true`. Closes
  the silent-miss feedback loop without requiring the model to
  remember to call the MCP tool. The `|| true` is belt-and-
  suspenders so an old PyPI snapshot or an `uvx` cold-start issue
  never surfaces as a Claude Code error banner.
- **Cross-process session-disabled-scopes divergence (known
  limitation)**: the Stop hook can't read the MCP server's
  in-memory `SessionState`, so scopes the user disabled in the
  current session via `memory_scope_disable` are still in scope
  for the hook's audit. Stop-hook events carry
  `triggered_from="stop_hook"` so downstream rollups can
  distinguish; the model-side `memory_audit_turn` events remain
  the strict source of truth.

### Performance

- **FTS5 schema v2: id→filename + memory_links tables.** Two hot
  paths in `_handlers.py` were still `load_all`-ing per call
  despite the index being available:
  - `_load_search_candidates` intersected the FTS candidate set
    against `store.load_all()` for every search.
  - `_links_payload` walked every active memory's frontmatter on
    every `memory_show` to compute reverse-links.
  Schema v2 adds a `filename` column to the `memories` table and a
  separate `memory_links` table (with a DELETE-cascade trigger so
  reverse-link queries don't dangle on tombstone). Both handlers
  use the new index helpers; the index now does what it always
  said it did.
- **v1 → v2 migration**: `_ensure_schema` detects the version
  mismatch, drops the data tables, and recreates empty. The Store
  hooks repopulate gradually as writes land; `bettermemory reindex`
  does the explicit full rebuild. The fallback in
  `_load_search_candidates` routes to `load_all` while the index is
  empty, so search keeps working through the transition with no
  user-visible break.
- **Index drift defense on candidate loads** (review fix): after
  resolving a candidate id to a filename and reading the file,
  verify the loaded memory's id matches the candidate id before
  appending to the result. Catches the `sync pull` window where
  the index's filename column briefly points at a path whose body
  has changed.

### Changed (model-facing surface)

- **Tool descriptions trimmed.** `DESC_MEMORY_SEARCH`, `_WRITE`,
  `_SHOW`, `_HEALTH`, `_UPDATE`, `_VERIFY`, `_RECORD_USE`, and
  `_SCOPE_OVERVIEW` were rewritten around "API surface + branching
  cues" rather than repeating policy that lives in the system
  `instructions` block and SKILL.md. Combined: 23,202 → 16,774
  chars (~5,800 → ~4,193 tokens). Every branching field a model
  needs to call the tool correctly survives.
- **`SYSTEM_PROMPT_ADDENDUM` restructured around the same quick-card
  opener SKILL.md uses.** The previous addendum was prose-heavy
  and lacked the decide-at-a-glance table; the rewrite is closer
  in shape to the skill, which makes "addendum and skill carry the
  same policy" closer to true. 8,255 → 6,512 chars (~2,063 → ~1,628
  tokens). The byte-equality drift test against
  `docs/system_prompt.md` is updated to match.
- **`memory_scope_overview` description** now spells out all seven
  keys it returns (was claiming three) including the load-bearing
  `curation_pending` rollup the addendum tells the model to read
  at session start.
- **18-tool count corrected.** The 2.1.0 release added
  `memory_audit_turn` (the 18th tool); the 2.1.1 docs-condense pass
  propagated the prior "17 tools" count throughout the live
  surface. README, api.md, plugin/README, CONTRIBUTING, and the
  server.py registration comment now read "18".

### Changed (tests + dev workflow)

- **`tests/test_sync.py` sandboxed.** The fixture was running
  `git config --global user.{email,name}` to make commits work,
  which silently overwrote the developer's `~/.gitconfig` on every
  local run. Fix: redirect git's global config to a per-test tmp
  file via `GIT_CONFIG_GLOBAL`.
- **Subprocess tests gated.** Five tests that invoke
  `bettermemory` as a subprocess fail on local checkouts without
  `pip install -e .`; now skip with a clear "run `pip install -e .`
  locally" reason rather than failing loud.
- **Three new structural drift tests** in `tests/test_prompts.py`:
  every `memory_*` name referenced in `SYSTEM_PROMPT_ADDENDUM` and
  in the plugin's `SKILL.md` must resolve to a tool the server
  registers. A rename or removal that forgets the policy surfaces
  shows up here.
- **In-process CLI coverage.** New tests for `consolidate` (text +
  --json), `tombstones list` (text + --json), `export -o`,
  `reindex`, and `migrate origin` exercise the dispatch arms that
  the subprocess tests previously protected. `server.py` coverage:
  41% → 55%.

### Documentation

- `examples/memories/*.md` files now carry `schema_version: 1` as
  the first frontmatter key (matching what `store.py` actually
  writes; the previous examples were missing the field).
- `CHANGELOG.md:7` anchor link to CONTRIBUTING.md was broken
  (`#versioning-and-the-1x-compatibility-contract` — the `1x-`
  was dropped during the 2.0 rewrite). Fixed.
- `docs/api.md` `memory_write` parameter signature reordered to
  match `_handlers.py`.
- The 2.2.0 entry's lede ("No code... behaviour is byte-identical")
  was self-contradicting against the `_handlers.py` / `audit.py` /
  `groundedness.py` edits listed two paragraphs below. Lede
  reworded to "No behavioural changes — only docstrings and code
  comments were touched."

### Deferred

- L9 (`time.sleep(0.01)` → freezegun-style explicit timestamps in
  test fixtures). The refactor needs a fixture that monkeypatches
  `utcnow` across every consumer module — multiple
  `from .models import utcnow` sites capture the reference at
  import time, so patching the canonical source doesn't propagate.
  The sleeps work today; the refactor warrants its own focused
  commit rather than bundling here.

## 2.2.0 - 2026-05-20

**Documentation tone pass.** No behavioural, on-disk-format, or
tool-surface changes; the source-file edits below are scoped to
docstrings and code comments, so runtime behaviour is byte-identical
to 2.1.1. The release is cut as a minor bump rather than a patch
because the public-facing language in `README.md` and
`docs/v1.6-plan.md` changed materially — anyone linking to the
prior README will land on a different framing of the project.

### Changed

- `README.md` comparison table reworked. The previous version
  compared bettermemory to mem0 / Letta / Zep / Cognee / Anthropic
  Memory Tool in a "Yes / No" scoreboard format, included a
  "Production junk-rate report" row citing one specific issue in a
  competitor's tracker, and several cells were stale or factually
  off (the mem0 retrieval contract, mem0's typed graph edges, mem0's
  temporal reasoning, Cognee's explicit `search()` API, the
  cross-host sync "Cloud-only" framing for self-hostable
  competitors). Rewritten as a neutral six-row design-space table
  that describes each system in its own terms.
- `README.md` opening framing and "Out of scope" section rewritten
  to lead with what bettermemory *is*, not what other tools aren't.
  Removed the "home-lab notes" example. The "Origins" personal
  anecdote was condensed into a short "Design notes" paragraph
  focused on the motivating problem and the design response.
- `docs/v1.6-plan.md` rewritten as a clean historical planning
  record. The May 2026 landscape snapshot now describes each related
  project in its own design language; competitive-pitch and
  weakness columns dropped. The tier-1 heading no longer reads as a
  rebuttal frame.
- `CHANGELOG.md` 2.0.0 entries cleaned. The descriptive prose for
  T1.1 (provenance), T1.3 (groundedness gate), and the
  claim-excerpts feature no longer names a specific competitor or
  cites a specific issue number when describing the failure mode
  these features address. The technical descriptions are preserved
  verbatim.
- `src/bettermemory/audit.py`, `src/bettermemory/groundedness.py`,
  and `src/bettermemory/_handlers.py` (groundedness-check comment +
  one tool-description example string) had the same scrubbing pass
  applied — module docstrings now describe the auto-extraction
  failure mode generically rather than via a competitor's bug
  tracker.

## 2.1.1 - 2026-05-20

**Documentation pass.** No code or on-disk format changes; the
exported `SYSTEM_PROMPT_ADDENDUM` constant is shorter but carries
the same load-bearing policy. Plugin users get a shorter `SKILL.md`
on next install; programmatic consumers of `SYSTEM_PROMPT_ADDENDUM`
get the trimmed body.

### Changed

- `README.md` rewritten end-to-end. ~66% shorter (489 → 164 lines).
  Lost the per-feature "(new in 2.0)" / "(new in 2.1)" markers
  (CHANGELOG owns history), the duplicate install paths, the full
  17-row tool table (now a grouped list pointing at `docs/api.md`),
  and the internals deep-dives that belong in `/docs` (event log,
  durability check internals, groundedness gate internals,
  performance characteristics, full config sample). Comparison
  table trimmed from 16 to 10 rows. The PyPI landing page renders
  this README, so the change ships to PyPI on next release.
- `plugin/README.md`, `plugin/skills/bettermemory/SKILL.md`,
  `docs/installation.md`, `docs/clients.md`, `docs/system_prompt.md`,
  and `src/bettermemory/prompts.py` (`SYSTEM_PROMPT_ADDENDUM`)
  condensed in the same pass. The drift test keeps the addendum and
  its doc copy byte-identical.
- `docs/api.md` reorganized. The previous version listed
  `memory_write` twice (once in the retrieval section, then again
  under writing), put `memory_show` after `memory_write`, and was
  missing `memory_audit_turn` entirely. Tools now appear in the
  documented group order (Retrieval, Writing, Lifecycle,
  Verification, Curation, Session-local) and all 18 are covered.

## 2.1.0 - 2026-05-20

**Silent-miss telemetry and endorsement-debt curation.** Two additive
features close the false-negative half of the opt-in retrieval
contract and add a "weakly endorsed" curation pivot. No on-disk
breaking changes — every new wire field is opt-in or absence-as-signal,
SCHEMA_VERSION stays at 1, and legacy events load unchanged
(`auto`-absent reads as explicit so pre-auto-commit history isn't
silently relabelled). Test count: 970 → 1021 (+51).

### Added

- `memory_audit_turn` MCP tool. Fires from a client-side end-of-turn
  hook with the user's message; runs a search probe over the active
  store using the model's configured search mode and asks whether a
  `search` or `show` event fired in the same session within
  `lookback_seconds` (default 60s, clamped to [1, 600]). When a
  high-relevance hit exists AND no retrieval happened in the window,
  emits a `search_miss` event so curation views surface the rate.
  Always emits `turn_audited` so audit cadence is visible in the log
  even when nothing's flagged. The threshold rule is versioned
  (`THRESHOLD_RULE_V1 = "v1_top1_high"`) and recorded on every event
  so a later calibration pass can replay historical logs under a new
  threshold without losing the audit trail. Surface:
  `bettermemory.audit` module exports `probe_for_miss`,
  `MissReport`, `MissHit`, `DEFAULT_LOOKBACK_SECONDS`,
  `THRESHOLD_RULE_V1`.
- Auto-vs-explicit applied count split on `MemoryStats`.
  `applied_count` (the total) is now backed by `auto_applied_count`
  (the server's auto-commit pass) plus `explicit_applied_count`
  (model called `memory_record_use` directly), with
  `endorsement_ratio = explicit / total` (or `None` on zero applies).
  Legacy events without the `auto` field count as explicit so
  pre-auto-commit history reads cleanly. The `heavily_used` render
  in `memory_health` now shows `applied=N (auto=X exp=Y)`.
- `endorsement_debt` rollup on `HealthReport` and `curation_counts`.
  The "weakly endorsed" bucket: memories the ranker keeps surfacing
  (`retrieval_count >= 5`) that the model never deliberately reaches
  for (`explicit_applied_count == 0`). Complement to `dead_weight`
  (never applied at all, auto included): dead_weight says the model
  doesn't even let the auto pass run on this; endorsement_debt says
  applies happened, but every single one was the auto fallback.
  Ambient memories are excluded — their value is implicit and
  explicit use events are structurally rare. Capped rows for inline
  display plus an uncapped `total` for bucket size. Threshold
  tunable via `endorsement_debt_min_retrievals` (clamped to >=1).
- `silent_misses` rollup on `HealthReport` and `curation_counts`.
  Counts `turn_audited` (denominator) and `search_miss` (numerator)
  events; the two-count shape distinguishes "stalled hook"
  (audited_total=0) from "healthy run" (audited_total>>0,
  miss_total=0). `memory_scope_overview.curation_pending` surfaces
  the miss numerator alongside `endorsement_debt` so session-start
  signals whether either pile is non-empty.

### Internal

- Health renderer fix: rename the inner `rate_pct` binding in the
  silent-misses block so it doesn't shadow the marker-stats one
  inside the same function scope.

## 2.0.0 - 2026-05-16

**Verification-grade memory.** The 1.6 plan in `docs/v1.6-plan.md`
shipped as one major release: nine features in three tiers turn
bettermemory into the only memory MCP with claim-level provenance,
write-time hallucination detection, an FTS5 inverted index over the
file-backed store, git-based cross-host sync, and a local web UI for
curation. Test count: 821 → 970 (+149). No on-disk breaking changes
— legacy memories load unchanged, every new wire field is opt-in or
absence-as-signal, and the SCHEMA_VERSION stays at 1. The 2.0 bump
reflects scope, not incompatibility.

What's new at a glance:

| Tier | Feature | Closes |
|---|---|---|
| T1.1 | Claim-level provenance (`claim_excerpts` on `memory_record_use`) | the hallucination-amplification gap in auto-extracting systems |
| T1.2 | Hybrid retrieval (BM25 + Jaccard + semantic via RRF) | the "keyword-only search" rebuttal |
| T1.3 | Write-time groundedness gate on `memory_write` | the HaluMem benchmark, operationalised |
| T2.1 | `bettermemory consolidate` CLI | the Letta sleep-time gap, no dual-agent topology |
| T2.2 | Typed inter-memory links (supersedes / contradicts / extends / depends_on) | graph-lite without graph DB infra |
| T2.3 | `recent_negative_outcomes` annotation on search hits | "model keeps re-suggesting rejected memories" |
| T3.1 | SQLite FTS5 inverted index + `bettermemory reindex` CLI | the load_all linear-scan ceiling at ~5-10K |
| T4.1 | `bettermemory sync` (git-based) | cross-host replication without a custom protocol |
| T4.3 | `bettermemory ui` local web UI | curation surfaces where a UI beats tool calls |

The competitive landscape (May 2026) is detailed in
`docs/v1.6-plan.md`. Per-feature detail follows.

### Added

- Local web UI (T4.3 of the 1.6 plan in `docs/v1.6-plan.md`). A
  small FastAPI app surfacing the curation surfaces that beat
  tool calls in a browser: memory_health rollups (active count,
  never-verified, stale verifications, dead-weight, cold,
  unresolved contradictions), a searchable memory list with scope
  filter, per-memory detail view showing body / scopes /
  timestamps / verified paths / typed links, and a one-click
  "Mark verified now" form that bumps `last_verified_at` and 303s
  back to the detail page (PRG pattern — refreshes don't repeat
  the verify). Tombstone browser with removal reasons. Run via
  `bettermemory ui --host 127.0.0.1 --port 8765` (local-only by
  default; binding non-loopback logs a warning since the UI
  exposes curation surfaces). The handler renders inline HTML
  with `html.escape` everywhere — no template engine, no JS
  framework, no XSS via memory_write. Gated behind a new
  optional `[ui]` extra (fastapi + uvicorn + httpx); the CLI
  prints a clean install hint when the extra is missing. No
  editing surface — writes happen in-conversation, the UI is
  read-mostly with verify as the one mutation since "I just
  spot-checked this" is a natural human action. Surface:
  `bettermemory.web` module exports `build_app(config, store)`
  for callers who want to mount the app under their own server
  and `serve(config, host=, port=)` for the standard uvicorn
  case.
- `bettermemory sync` CLI subcommand for cross-host replication
  (T4.1 of the 1.6 plan in `docs/v1.6-plan.md`). Thin wrapper over
  git — the memory directory is already plain markdown, so git's
  history / distributed copies / three-way merge handle the
  interesting cases without a custom protocol. Five subcommands:
  `sync init [--remote URL]` initialises the dir as a git repo and
  writes a `.gitignore` that excludes the regenerable caches
  (`.index.sqlite`, `.events.jsonl`, `.embeddings.*.npz`, lock
  files, doctor probes); `sync status` reports branch, pending
  changes, and remote ahead/behind counts; `sync push` stages,
  commits with a default `bettermemory: sync` message, and pushes
  (no-op when nothing changed locally — the `committed=False`
  signal in the response distinguishes this from "pushed prior
  commits"); `sync pull` rebase-pulls and rebuilds the FTS5 index
  so the runtime view matches the new file contents (Store hooks
  bypassed during the merge); `sync auto` is pull-then-push, the
  shell-alias / cron one-shot. `--set-upstream` is automatic on
  the first push so a subsequent `pull` has a tracking branch.
  Merge conflicts fall through to git's normal flow — `git
  rebase --continue` from the memory directory once resolved.
  Surface: `bettermemory.sync` module exports `init()`, `status()`,
  `push()`, `pull()`, `auto()`, the `SyncStatus` dataclass, the
  `SyncError` exception, and the `DEFAULT_COMMIT_MESSAGE` constant.
- SQLite FTS5 inverted index (T3.1 of the 1.6 plan in
  `docs/v1.6-plan.md`). Files on disk stay canonical; the index
  is a derived cache at `<store>/.index.sqlite`. Schema: a
  `memories` table mirroring the on-disk records plus an FTS5
  virtual table over body + scope text, kept in sync by three
  triggers. Store hooks keep the index live on every `write`,
  `update`, and `tombstone`. Index hooks are best-effort: a
  corrupted database or missing file logs a warning and lets the
  canonical write proceed, so on-disk truth is never blocked on
  an index failure. New CLI subcommand `bettermemory reindex`
  rebuilds the index from scratch (use it after hand-editing
  memory files or restoring from backup). A schema-version field
  in the `meta` table refuses to load indexes newer than the
  reader supports. `memory_search` now uses the index as a
  candidate pre-filter when `indexed_count >= 500` (tunable via
  the `BETTERMEMORY_INDEX_THRESHOLD` env var): up to 50
  candidates from the FTS5 query, then the existing rankers
  reorder within that pool. Falls back to `load_all` when the
  index is missing, corrupt, below threshold, or returns zero
  candidates — small stores see byte-stable behaviour, large
  stores skip the linear scan that starts to bite at ~5-10K
  memories. Surface: `bettermemory.index` module exports
  `rebuild()`, `upsert()`, `remove()`, `query()`, `status()`,
  the `IndexVersionError` exception, and the `INDEX_FILENAME` /
  `SCHEMA_VERSION` constants.
- Typed inter-memory links (T2.2 of the 1.6 plan in
  `docs/v1.6-plan.md`). New `links` field on the `Memory` model:
  a list of `{type, target_id, note?}` entries where `type` is one
  of `supersedes`, `contradicts`, `extends`, `depends_on`.
  Persisted in YAML frontmatter; legacy memories load with an empty
  list. Settable via the new `links` parameter on `memory_update`
  (REPLACE semantics: pass the full new list, or `[]` to clear).
  Self-links are rejected; target_id must be a valid ULID. Surface
  at retrieval is bidirectional: `memory_show` on a source memory
  returns the forward `links`; `memory_show` on the target carries
  `reverse_links` (with `source_id` instead of `target_id`) so the
  consumer sees the relationship from either side. Forward-compat
  guarantee: unknown link types on disk load silently as empty
  rather than failing the whole record. Graph-lite without the
  graph DB infra burden — adopted from mcp-memory-service's typed-
  edges idea but plumbed into retrieval, not just storage.
- Write-time groundedness gate on `memory_write` (T1.3 of the 1.6
  plan in `docs/v1.6-plan.md`). Optional, opt-in via the new
  `groundedness_check=True` parameter plus a `source_transcript`
  (recent conversation turns). The server walks the body sentence-
  by-sentence and flags any sentence whose stopword-stripped, kebab-
  expanded content tokens overlap the transcript's token set by less
  than 30% — the "fact pulled from thin air" failure mode common to
  auto-extracting memory systems. Returns
  `{status: "ungrounded", claims: [{sentence, overlap_ratio}, ...]}`
  instead of committing; the caller can rephrase or pass the new
  `acknowledge_ungrounded=True` override (same family as
  `acknowledge_transient` and `acknowledge_scope_mismatch`) when
  they have other grounding sources (a file read, a tool result)
  not represented in the transcript. Off by default — back-compat
  for every existing caller. Implements HaluMem-style operation-
  level write-time hallucination evaluation inline.
  Surface: `bettermemory.groundedness` module exports
  `check_groundedness()`, the `UngroundedClaim` dataclass, and the
  threshold constants for callers wanting to wire the gate into
  alternate flows.
- Negative-outcome annotations on `memory_search` hits (T2.3 of the
  1.6 plan in `docs/v1.6-plan.md`). When a hit's memory has been
  `ignored` or `contradicted` within the last 30 days AND not since
  been `applied`, the hit carries a `recent_negative_outcomes` field
  — a list (at most one entry per outcome type, so two entries max)
  describing the rejection. Each entry has `outcome`,
  `most_recent_ts`, `count_in_window`, `session_id`, `note`, and
  `claim_excerpt` (when the original record_use carried one — T1.1
  integration). The supersession rule is the load-bearing semantic:
  an `applied` event after a negative event clears the negative-
  bucket entries, because the user already validated the memory
  after the rejection; surfacing the rejection then would be
  misleading. `corrected` outcomes never surface (audit-only — the
  drift was salvaged inline). The field is OMITTED from the hit when
  no qualifying negatives exist — absence is the default. Stops the
  "model keeps re-suggesting memories the user already rejected"
  failure mode without any state on the client side. One event-log
  iteration per search call, then per-id bucketing; cost is bounded
  regardless of result count.
- `bettermemory consolidate` CLI subcommand (T2.1 of the 1.6 plan in
  `docs/v1.6-plan.md`). Offline curation pass over the store with
  four operations: near-duplicate dedup (semantic when the
  embeddings extra is installed, Jaccard otherwise — the newer-
  `updated` member wins, ties broken by `verified_paths` attestation
  then ULID), demote-never-applied (mirrors `memory_health`'s
  dead-weight rule; retags `category=ambient` so the memory stops
  appearing in the dead-weight bucket without losing the body),
  cold-scope suggestions (scopes whose newest memory has aged past
  `--cold-scope-days` AND with no `applied` events on any member),
  and scope-typo pairs (Levenshtein ≤ `--typo-distance` neighbors;
  the scope with more memories is the keeper, the lesser is the
  proposed typo). Dry-run by default; `--apply` commits dedup
  tombstones and category demotions (cold scopes and typo pairs
  stay suggest-only regardless — they touch shape that needs human
  review). `--json` for machine consumption. Closes the Letta-style
  sleep-time consolidation gap without the dual-agent topology.
  Surface: `bettermemory.consolidate` module exports
  `consolidate()`, the per-pass `find_*` helpers, the
  `ConsolidateReport` dataclass, and `render_text` / `render_json`
  for callers that want the data without going through the CLI.
- Claim-level provenance on `memory_record_use` (T1.1 of the 1.6 plan
  in `docs/v1.6-plan.md`). New optional `claim_excerpts` parameter —
  a list parallel to `memory_ids` (same length, one entry per id, or
  `None` per slot for "no specific claim noted") carrying the
  load-bearing phrase the model applied, ignored, contradicted, or
  corrected from each memory. Stored in the event log so a later
  audit can trace any response back to the specific claim, not just
  the memory id. Excerpts strip surrounding whitespace, reject empty
  strings (use `None` for "no claim"), and cap at 500 chars to keep
  the audit log small and discourage dumping bodies. Byte-stable on
  the wire when not used: existing event-log readers don't see a new
  null key on every old event. Works for all four record_use
  outcomes; especially useful for `contradicted` and `corrected` so
  the audit log records which claim was wrong, not just that the
  memory had drift. Closes the provenance gap in auto-extraction
  systems, which amplify hallucinations because the audit trail
  doesn't tie a wrong response back to the specific stored claim
  that caused it.
- Hybrid retrieval for `memory_search` (T1.2 of the 1.6 plan in
  `docs/v1.6-plan.md`). The original keyword scorer (TF + coverage +
  recency) is now one of four selectable rankers; the new ones are
  Okapi BM25 (IDF-weighted, TF-saturated, length-normalised — closes
  the recall gap on rare-term queries), sentence-transformers cosine
  (paraphrase matching when the embeddings extra is installed), and
  hybrid (Reciprocal Rank Fusion over keyword + BM25, plus semantic
  when available). Selection is per-call via the new `mode` parameter
  on `memory_search` (`"keyword"` | `"bm25"` | `"semantic"` |
  `"hybrid"`) or globally via `[behavior] search_mode` in config. The
  default stays `keyword` in 2.0.0 to keep ranking byte-stable; the
  flip to `hybrid` is planned for a later release once dogfooding
  shakes out regressions. Hybrid mode without the embeddings extra
  degrades gracefully to keyword + BM25 fusion; `mode="semantic"`
  without the extra raises with an install hint. The fused-score
  scale (~0.01 – 0.05 from `1/(k+rank)` summed) differs from the
  single-ranker scales, so consumers should keep using the
  `relevance` label, not the raw `score`, for cross-mode comparison.
  Surface: `bettermemory.search.compute_idf`,
  `score_memory_bm25`, `reciprocal_rank_fusion`, and the `SearchMode`
  Literal type are exported for callers that want to wire the
  rankers directly without going through `search()`.

### Fixed

Five fixes landed inside the v2.0.0 tag window: the initial
`release: 2.0.0` commit went out, two CI failures (sdist excludes
and a ruff format miss) blocked the Release workflow, and during
the retag cycle these five fixes were picked up before the green
tag landed. All are documented here for the audit trail.

- `memory_write` and `bettermemory consolidate`: in 3+ way duplicate
  clusters, the dedup pass could tombstone a memory it had crowned
  as the keeper of an earlier pair (when the same id appeared as
  the duplicate in a later pair), leaving the first pair's
  "near-duplicate of X" tombstone reason dangling against a
  now-tombstoned X. The apply loop now tracks `keepers_so_far`
  alongside `tombstoned_ids` and skips any pair that names a prior
  keeper as its duplicate.
- `memory_update`: editing the body reset `last_verified_at` to
  null but left `verified_paths`, `verified_commits`, and
  `verified_versions` populated from the prior content. Those
  attestations were attached to prose that no longer exists, so a
  later `memory_search` could read a stale `verified_paths` set
  against new body text and suppress the path-drift signal it
  should have produced. Body-edit updates now clear the structured
  attestation lists in lockstep with `last_verified_at`. Scope,
  confidence, category, and links edits still preserve the
  attestation (they don't touch the body's claims).
- `memory_search`: `MemoryHit.category` is declared as
  `Category | None` on the model, but `_build_hit` constructed the
  hit without the field, so every result silently carried
  `category=None` regardless of the stored memory's actual category.
  Hits now carry the persisted category, surfacing ambient and
  user-inference markers to callers that filter on it.
- `bettermemory sync status`: `git status --porcelain` v1 uses a
  fixed-width `XY␣path` shape where the X char is a space for
  modified-not-staged files. The previous `line.partition(" ")`
  split dropped the status char into the path, recording the
  modified file as `"M filename"` in `SyncStatus.modified`. Now
  parsed by position. Separately, `init` action strings,
  `SyncStatus.remote_url`, and `SyncError` messages echoed credentialed
  HTTPS remote URLs (`https://user:token@github.com/...`) verbatim,
  so a piped `bettermemory sync status --json` or a `git push`
  failure surfaced the token. Added `_redact_url` and `_redact_text`
  helpers that mask the userinfo segment while leaving SSH URLs
  (`git@host:path`) alone.
- `bettermemory ui`: the one state-changing endpoint
  (`POST /memories/{id}/verify`) now requires the request's Origin
  (preferred) or Referer header to point at a loopback host —
  loopback binding alone doesn't stop a malicious page in another
  browser tab from POSTing to localhost and forging a verify event,
  which would corrupt a load-bearing trust signal. Header-less POSTs
  (server-rendered classic forms under stricter referrer-policy
  settings) still fall through, since refusing every header-less
  POST would break the normal in-UI flow; the guard catches the
  case where a third-party origin actively attaches its own header
  (the default cross-site form behaviour in mainstream browsers).
  The `note` form field is also now capped at 500 chars (matching
  `claim_excerpts` on `memory_record_use`) so a paste-bomb can't
  inflate the event log.

## 1.5.0 - 2026-05-13

A multi-agent audit pass surfaced six bugs and one missing feature
spread across the data, search, verify, and origin layers. Six fix
commits and one feature commit landed off that audit. No on-disk
format changes — `Origin.worktree_root` is an additive optional field
and legacy memories without it pass through every new filter
unchanged. Consumers pinned to `>=1.4.2` upgrade transparently;
behaviour changes are observable but each one is a bug fix in the
direction the docstring already promised.

### Added

- `Origin.worktree_root` is captured at write time via
  `git rev-parse --show-toplevel` and threaded through the auto-scope
  filter on `memory_search` and `memory_scope_overview`. Fixes the
  audit's "worktree leakage" scenario where two `git worktree add`
  checkouts of one repo shared `origin.repo` and so cross-contaminated
  each other's search results — repo-only matching had no signal to
  tell sibling worktrees apart, and a memory written from
  `~/repo-feature/` would surface in a search run from
  `~/repo-bugfix/`. Worktree filtering rides on the same
  `auto_scope` toggle as repo filtering (one knob, not two), and a
  legacy memory with no `worktree_root` field always passes the new
  filter, so nothing pre-existing gets silently hidden.

### Fixed

- **`migrate.py` durability**: `migrate_origin_in_directory` was the
  only persistent-write site that bypassed
  `store._atomic_write_post`'s fsync discipline — bare `write_bytes`
  + `replace`. POSIX guarantees the rename is atomic but doesn't
  guarantee the data backing it is on disk; a power loss between
  rename and the next background flush could leave a zero-byte file
  at the target path. Now mirrors the helper's flush + fsync_file +
  rename + fsync_dir sequence.
- **Unicode tokenization**: `_TOKEN_RE = r"[a-z0-9][a-z0-9\-_]*"`
  was ASCII-only after `.lower()` reduced casing, so accented
  codepoints fell out of the character class and `tokenize("Niño café")`
  returned `['ni', 'o', 'caf']`. Any non-English memory body was
  effectively unsearchable. Switched to `\w[\w\-]*` so the codepoints
  stay whole; a query for "café" now finds a memory body about the
  café del puerto.
- **Search tiebreaker**: `hits.sort(key=lambda h: (h.score, h.created),
  reverse=True)` left ordering undefined for two memories sharing
  both score AND created timestamp — a real case under
  microsecond-tied writes and clock-mocked tests. Added `h.id` as
  the final discriminator; ULID-shaped ids sort lexically by time so
  the tiebreaker also gives "newer wins" semantics.
- **`store._as_dt` naive-string branch**: the `datetime` branch
  coerced naive values to UTC-aware before returning, but the `str`
  branch handed back whatever `datetime.fromisoformat` produced. A
  hand-edited frontmatter with a *quoted* timestamp like
  `'last_verified_at: "2025-01-01T10:00:00"'` flowed through the str
  branch as a naive datetime, then crashed
  `health.compute_health` on the first `naive < aware_cutoff`
  comparison. Both branches now coerce to UTC-aware.
- **Verify staleness boundary**: `age_days >= threshold` made a
  memory at exactly `stale_after_days` flip from fresh to stale at
  midnight UTC on the boundary day. The intuitive reading of "fresh
  for 30 days, then stale" naturally means "stale starts on day 31",
  so the comparison is now strict-greater on actual elapsed seconds.
  The `stale_after_days=0` carve-out still works because any
  measurable elapsed time satisfies `age_seconds > 0`.
- **`verify.detect_path_drift` `verified_paths` normalisation**:
  body candidates passed through `_normalize_candidate` (trims
  trailing punctuation) before the set-membership check, but
  `verified_paths` only went through `_normalize_for_compare`. So an
  attestation like `verified_paths=["/path/to/foo,"]` (trailing
  comma copied from prose) failed to match the body candidate
  `/path/to/foo` that had already been trimmed. Both sides now run
  through the same trim/validate pipeline.
- **`doctor.py` probe-write cleanup**: ENOSPC mid-write could leave
  a zero-byte `.doctor-probe` file in the user's store directory
  because the `unlink` was reached only on the success path. Moved
  to a `finally` arm with `missing_ok=True`.
- **`semantic.py` flush durability**: `flush_persistent_cache`
  renamed `.npz.tmp` into place without an explicit `fsync` and
  orphaned the `.tmp` if `np.savez_compressed` raised mid-write.
  Added `fsync_file` before close and a cleanup arm on failure;
  brings the cache flush in line with the rest of the store's
  durability discipline.

## 1.4.2 - 2026-05-13

Metadata and CI hygiene. No runtime, wire-shape, or on-disk format
changes versus 1.4.1; consumers pinned to `>=1.4.1` upgrade
transparently.

- pyproject `description` (PyPI's "summary" field) and the plugin
  manifest description now read "Persistent memory for Claude Code,
  retrieved on demand." — matching the GitHub About text. The old
  string ("Local file-backed memory MCP server with retrieval-on-demand")
  was correct but mechanical; the new line is the project's actual
  positioning.
- `tests/test_events.py::test_rotation_fsyncs_archive_after_gzip_trailer_is_flushed`
  now gates its `fcntl` block behind `sys.platform == "win32"` so
  `mypy --strict` resolves the body under POSIX stubs on Linux/macOS
  and skips it on win32. The `@pytest.mark.skipif` decorator already
  prevented runtime execution; only the type-checker pass needed the
  narrowing.
- `tests/test_config.py` `resolved_directory` tests now redirect
  `Path.home()` via a `_set_fake_home(monkeypatch, home)` helper that
  sets both `HOME` (POSIX) and `USERPROFILE` (Windows). Setting only
  `HOME` was a no-op on Windows — `ntpath.expanduser` reads
  `USERPROFILE` first — so three tests had been silently asserting
  against the runner's real home directory.

## 1.4.1 - 2026-05-13

Republish from cleaned history. No code, behavior, wire-shape, or
on-disk format changes versus 1.4.0 — pyproject `version` bumped so a
fresh PyPI artifact can be published after the project's release
history was reset. Any consumer pinned to `>=1.4.0` upgrades
transparently.

## 1.4.0 - 2026-05-13

Audit-fixes release. Internal hardening across durability, the
server-side state shape, the surface-filter callsites, and the
2972-line server module. The wire surface (17 MCP tools, names,
schemas, JSON shapes) is unchanged from 1.3.x; every public default
is preserved. Most installs will notice nothing — that's the
intended shape for a minor bump driven by infrastructure work.

The one user-facing addition: a `SessionRegistry` routing layer that
makes a single long-running server process safe to serve multiple
MCP clients (each `Context.client_id` resolves to its own
`SessionState`, so pending writes / disabled scopes / use-tokens
never leak between clients). For stdio (the primary transport,
one client per process), this collapses to a single state under a
default-bucket key — same observable behavior as before.
`build_server(state=...)` still accepts a bare `SessionState` for
back-compat with the existing single-client test surface.

### Added

- **`SessionRegistry` for multi-client server processes**
  (`src/bettermemory/session.py`). New `SessionSource` protocol;
  `SessionState` (single client) and `SessionRegistry` (multi
  client) both satisfy it. `build_server` defaults to the
  process-wide `get_default_registry()`. All 17 tool handlers
  gained `ctx: Context | None = None` and resolve their per-request
  state via `sessions.for_request(ctx)` at entry. Ten new unit +
  end-to-end isolation tests in `tests/test_session_registry.py`.
  The unbounded `_states` dict is a documented trade-off matched
  to the current stdio-primary deployment; revisit if HTTP/SSE
  becomes a supported transport.
- **`should_include_for_caller`** in `origin.py` — the canonical
  surface-filter for "this memory belongs to this caller's
  project," shared between `memory_search` and
  `memory_scope_overview`. Commit-drift callsites continue to
  use the stricter `repos_match` (no global-memory pass-through),
  documented in the helper docstring.
- New `tests/test_config.py` with 18 unit tests covering TOML
  coercion (bool/int/float/str) and the `resolved_directory`
  resolution tree (env / explicit / project-scoped / global
  fallback, plus the `~`-expansion and defensive-against-a-file
  cases).
- New `tests/test_addendum_tool_names_exist_on_server`: parses
  every `memory_*` ref out of `SYSTEM_PROMPT_ADDENDUM` and asserts
  it exists as a registered tool on `build_server()`. Catches
  doc-drift between the prompt addendum and the actual tool
  surface before it ships.
- New `tests/test_events.py::test_rotation_fsyncs_archive_after_
  gzip_trailer_is_flushed`: pins the structural shape of the
  rotation fsync (see the durability fix below).

### Changed

- **Server split** (`src/bettermemory/server.py` → `_handlers.py` +
  `_response.py`). The 2972-line `server.py` shrinks to 1014 lines
  of wiring + CLI; the 17 tool handlers move to a `ToolHandlers`
  class on `_handlers.py`, the JSON-shaping helpers move to a
  `ResponseBuilder` class on `_response.py`. Wiring is unchanged —
  every tool name, every schema, every response shape is identical
  to 1.3.x; tests reach handlers the same way (via
  `mcp._tool_manager.get_tool(name).fn`, which resolves to the
  bound method post-split). Pure refactor; the only visible delta
  is `find handlers/ -size` is now actually a useful operation.
- **Tiered git logging** (`src/bettermemory/origin.py:_git`). The
  common "not a git repository" case now logs at DEBUG instead of
  WARNING — clears the noise on installs where memories live in a
  non-repo directory. Real failure modes (missing binary, command
  timeout, OSError) stay at WARNING.

### Fixed

- **fsync on every persistent write**
  (`src/bettermemory/_fsutil.py`, `store.py`, `events.py`). Atomic
  writes were tmp-file + rename, but the actual data and the
  directory inode were left for the kernel's writeback to flush at
  its own pace. A power-loss between rename and the next writeback
  could leave a zero-length file or a missing entry in the parent
  directory. All four persistent-write paths in store.py now route
  through a single `_atomic_write_post` that fsyncs the file before
  the rename and the parent directory after; the event log fsyncs
  each append and the rotation archive. Side-fix: `rename_scope`'s
  tombstone overwrite was previously a non-atomic in-place
  truncating write — now goes through tmp+rename like the other
  persistent paths.
- **Rotation fsync runs after the gzip trailer is flushed**
  (`src/bettermemory/events.py`). The initial durability commit
  fsynced the gzip write fd from inside the `with gzip.open(...)
  as dst:` block, but `GzipFile.close()` writes the CRC32+ISIZE
  trailer at `with` exit — so the fsync race could leave a body-
  only archive that `gzip.open(...)` rejects on read. Now re-opens
  the archive read-only after the `with` block and fsyncs that
  fresh fd. Source memory files were never affected; bounded to
  archived-audit-log corruption.

### Internal

- The `_FakeCtx` duck-type in `tests/test_session_registry.py`
  picked up a `_fake_ctx` `Any`-typed helper so strict mypy
  accepts it where `for_request` expects a `Context[Any, Any, Any]`
  — clears nine pre-existing arg-type errors without changing the
  test semantics.

## 1.3.2 - 2026-05-13

Writing-policy calibration. The on-disk format, the wire surface,
every public default, and the 17-tool count are unchanged. Four
shipped strings did change content (the FastMCP `instructions`
block, the `memory_write` tool description, the plugin `SKILL.md`,
and `SYSTEM_PROMPT_ADDENDUM` with the matching fenced block in
`docs/system_prompt.md`), which is why this rides as a patch
release rather than a docs-only commit, matching the precedent
set by 1.3.1. The README was brought up to date alongside, so the
at-a-glance pitch surfaces the new write-side axis.

Pre-1.3.2 the docs carried the mechanics of writing (durability
check, dedup, confirmation tiers) but no positive triggers, no
text telling the model WHEN to write. A reading model defaulted
to not writing, producing the failure mode where a session
retrieves memory but records nothing, and the user re-explains
the same project context every chat. This release adds an
explicit, symmetric "writing is PROACTIVE" rule to every
model-facing surface, parallel to the existing "retrieval is
OPT-IN" rule. The opt-in retrieval contract is preserved verbatim;
only the write-side calibration changed.

### Changed

- **MCP `instructions` block (`src/bettermemory/server.py`).** Adds
  a "Writing is the OPPOSITE axis: PROACTIVE" paragraph with the
  four triggers (user states a preference; a project decision the
  user concurred with; a tool / infra / config fact entering the
  work; a unit of work finishing with a why git won't capture) and
  the load-bearing summary "your job is to capture". The retrieval
  paragraph was compressed to keep the full block under the
  1700-char Claude Code truncation budget (1672 chars / 1686 bytes
  post-edit, 28 chars headroom). The opt-in retrieval contract
  phrasing ("Memory is OPT-IN retrieval... Default to NOT
  retrieving.") is preserved verbatim.
- **`memory_write` tool description (`src/bettermemory/server.py`).**
  Leads with "Call this PROACTIVELY whenever something durable
  enters the conversation" and the same four triggers. The previous
  opening ("Durable facts only") moves to a second paragraph behind
  the trigger guidance so the trigger lands first on a reading
  model.
- **Plugin `SKILL.md` (`plugin/skills/bettermemory/SKILL.md`).**
  Adds a "When to write" section paralleling the existing "When to
  retrieve" section, with the same four triggers and an explanation
  of how the structural guardrails (durability, dedup, pending tier,
  scope-mismatch) make aggressive writing safe. The Quick-card
  "Write?" row leads with "proactive, something durable just
  entered the conversation, then yes."
- **`SYSTEM_PROMPT_ADDENDUM` (`src/bettermemory/prompts.py`) and the
  matching fenced block in `docs/system_prompt.md`.** Adds the
  identical PROACTIVE-writing preamble and four-trigger list at the
  top of the "Writing and updating memory" section, before the
  existing mechanics bullets. The drift test in
  `tests/test_prompts.py` continues to pin these to byte-equality.
- **`README.md`.** "What you get" gains a sibling **Proactive
  writing** bullet next to the existing **Opt-in retrieval**
  bullet so the dual-axis framing is visible on the at-a-glance
  pitch. The "Install in Claude Code" line names both policies the
  SKILL carries (previously: just "the opt-in retrieval policy"),
  and the "How the policy lands at the system-prompt level"
  paragraph names the proactive-writing rule alongside the
  retrieval contract.

### Added

- **`must_have` regression in `tests/test_server.py` for the
  writing-side calibration.** The existing instructions-block
  regression test now asserts the rendered block contains the
  load-bearing write-side phrases (`"memory_write"`, `"PROACTIVE"`,
  and `"your job is to capture"`) alongside the existing
  retrieval-side phrases. Without this, a future shorten-pass could
  silently un-do the write-side calibration the way the project's
  previous "lock writing down further" regression nearly did on the
  retrieval side.

### Fixed

- **`CHANGELOG.md` 1.3.0 heading restored.** The
  `## 1.3.0 - 2026-05-10` heading went missing in an earlier edit,
  leaving the 1.3.0 entry's body content (the `category` parameter
  on `memory_update` and the slug-builder double-date fix) visually
  merged into the 1.3.1 entry. The heading is restored above the
  orphaned "Same-day minor following 1.2.2" rationale paragraph; no
  entry body content changed.

## 1.3.1 - 2026-05-10

Documentation and prose pass. The on-disk format, the wire surface,
and every public default are unchanged. Two shipped strings did
change content (the FastMCP `instructions` block and
`SYSTEM_PROMPT_ADDENDUM` in `prompts.py`), which is why this rides
as a patch release rather than a docs-only commit. Both still pass
the byte-budget regression test and carry the same load-bearing
phrases.

### Changed

- **README, `docs/`, `plugin/README.md`, `plugin/skills/bettermemory/SKILL.md`,
  `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`.** Rewritten
  to drop em dashes throughout and to read in shorter, more direct
  sentences. Same content, different prose. The audit pass also
  added `category="ambient"` to the listed stable enum values in
  `CONTRIBUTING.md` (it was already shipped in 1.2 but missing from
  the compatibility-contract list), the `category` parameter on
  `memory_update` and the `verified_paths` / `verified_commits` /
  `verified_versions` parameters on `memory_verify` to the README's
  tool table (additions in 1.2 and 1.3 that the previous table
  omitted), and the v1.2 / v1.3 surface entries to `docs/api.md`
  (`staleness_verdict`, auto-`record_use`, `cold_memories`,
  `curation_pending`, `acknowledge_scope_mismatch`, the structured
  `verified_*` parameters, and the additional update flow).
- **`SYSTEM_PROMPT_ADDENDUM` in `src/bettermemory/prompts.py` and
  the matching fenced block in `docs/system_prompt.md`.** Same
  policy, em dashes removed. The drift test in `tests/test_prompts.py`
  still pins these to byte-equality.
- **MCP `instructions` block in `src/bettermemory/server.py`.** One
  em dash replaced with a sentence break; final body stays well
  inside the 1700-char regression budget.
- **CHANGELOG version headings.** Switched from `## X.Y.Z — date`
  to `## X.Y.Z - date` so future entries match the project's
  no-em-dash style. Historical entry bodies were left alone (they
  are immutable release records).

## 1.3.0 - 2026-05-10

Same-day minor following 1.2.2. Two surface changes shaken out by a
maintenance audit pass: an additive `category` parameter on
`memory_update` (so legacy memories written before the `ambient`
tier can be retagged without remove+rewrite) and a slug-builder fix
for bodies that begin with their own date. Both are purely
additive on the wire — legacy clients never pass `category` to
`memory_update`, and the slug change only fires when the body's
first line starts with an ISO date.

### Added

- **`category` parameter on `memory_update`.** Joins `content`,
  `scopes`, and `confidence` as an updatable field. Accepts
  `"fact"` and `"ambient"` (the same values `memory_write` accepts
  except `"user-inference"`, which is rejected here — that
  category gates the pending-confirm WRITE flow and there is no
  equivalent gate on update). The motivating case: legacy
  memories written before 1.2.0 carry no category and read as
  `fact` for runtime semantics, but ambient-class memories
  (user-identity blurbs, persistent-environment quirks) really
  belong in the `ambient` bucket so they're excluded from the
  dead-weight curation rule. Pre-1.3 the only retag path was
  remove+rewrite, which lost the original `created` timestamp and
  littered `.tombstones/`. Category retags are metadata-only —
  `last_verified_at` is preserved across the change, the same way
  scope and confidence edits preserve it. Seven new regression
  tests in `tests/test_server.py` cover retag-to-ambient,
  retag-back-to-fact, verification-preserved, omission-preserves,
  user-inference rejected, unknown rejected, and category-only
  satisfying the at-least-one-field guard.

### Fixed

- **Slug builder no longer duplicates a leading ISO date.**
  `make_slug` in `src/bettermemory/models.py` now strips a leading
  `YYYY-MM-DD` (and the optional `Thh:mm[:ss][Z|±hh:mm]` time
  fragment) from the first line of the body before word-splitting.
  Without the strip, a body starting with "2026-05-07 tightened
  the mvp" produced a slug `2026-05-07-tightened-the-mvp` which
  `build_filename` then prefixed *again* with the memory's
  `created` date — the maintainer's own store had a real
  `2026-05-07-2026-05-07-tightened-the-mvp.md` file as evidence.
  The strip is conservative: only a leading date is touched, so a
  date in the middle of a title (`released 2026-05-07 cut`)
  survives, a bare year (`2026 retro`) is left alone, and a partial
  date (`2026-05 monthly review`) is preserved. New
  `tests/test_models_slug.py` (18 tests) covers the regression
  plus the keep-existing-behaviour cases.

## 1.2.2 - 2026-05-10

Same-day patch following 1.2.1. The path-drift extractor was firing
phantom `path_drift_missing` entries on memories whose bodies use
documentation-placeholder paths (`/etc/foo`, `/path/to/file`,
`/foo/bar`) to illustrate path-typed APIs — discovered when the
project's own overview memory documented `verified_paths` semantics
with `/etc/foo` as the example path and read back as
`staleness_verdict: "spot_check_recommended"` immediately after
being verified.

### Fixed

- **Documentation-placeholder paths no longer trigger phantom drift.**
  `_normalize_candidate` in `verify.py` now consults a small frozen
  set of canonical placeholder paths (`/etc/foo`, `/etc/bar`,
  `/etc/baz`, `/foo`, `/foo/bar`, `/foo/baz`, `/foo/bar/baz`,
  `/path/to`) plus two prefix patterns (`/path/to/...`,
  `~/path/to/...`). Candidates that match are dropped before disk
  stat, the same way URLs and SSH-style remotes are. Single-extension
  variants are also dropped — `/etc/foo.conf` reads as a placeholder
  via the `/etc/foo` entry. The list is deliberately narrow:
  terminal-component `foo` / `bar` are NOT filtered, so the
  `/tmp/foo`-shaped tmp-path test fixtures real test suites use
  remain valid path candidates. Seven new regression tests in
  `tests/test_verify.py` lock the contract — including a
  `test_dot_prefixed_real_path_not_misclassified_as_placeholder`
  test pinning that `~/.claude-memory` and similar leading-dot
  paths don't trip the extension-stripping branch.

## 1.2.1 - 2026-05-10

Same-day docs-surface follow-up to 1.2.0. The v1.2.0 release added
`staleness_verdict`, auto-`record_use` via `use_token`,
`curation_pending` rollup, `category="ambient"`, `scope_mismatch`
warning, and `verified_claims` on `memory_verify`, but the doc
surfaces that ship to clients (the FastMCP `instructions` block, the
plugin `SKILL.md`) hadn't been updated to mention them. A model
installing 1.2.0 would still see the v1.1 contract and miss the
headline UX wins. 1.2.1 brings both surfaces in line.

### Changed

- **MCP `instructions` block (`src/bettermemory/server.py`).** Rewritten
  to surface the v1.2 headline UX wins where every compliant client
  sees them: `staleness_verdict` (the rolled-up branch-on-this-first
  field) and the auto-`record_use` flow (the most-forgotten step is
  now opt-out). Previously the block named the three underlying
  staleness signals individually and described `memory_record_use` as
  a per-response obligation, neither of which matched the v1.2
  surface. Tightened the false-positives sentence and the
  session-start hint to make room for the new content under the
  1700-char regression budget (final body 1681 chars / 1687 utf-8
  bytes; all must-have phrases preserved). Also adds the
  `curation_pending` rollup mention to the session-start paragraph
  and `verified_paths` to the verify guidance.
- **Plugin `SKILL.md`.** Now opens with a six-row quick-card
  (Search? / Write? / Category? / Outcome? / Verify? / Scope?) so the
  decision rules are cheap to keep in working memory; the
  prose-heavy reference moves below it. Updated to the full v1.2
  surface throughout: `staleness_verdict` rollup as the
  branch-on-this-first field, auto-`record_use` via `use_token`,
  `category="ambient"`, `scope_mismatch` warning at write time,
  structured `verified_claims` on `memory_verify`, and the
  `dead_weight` / `cold_memories` split with the matching
  `curation_pending` rollup on `memory_scope_overview`.

### Fixed

- **Stale `uv.lock` package version.** The lockfile still pinned
  `bettermemory==0.1.0` from before the 1.0 release; refreshed to
  match `pyproject.toml`. No dependency changes.

## 1.2.0 - 2026-05-10

Seven additive surface changes targeting the curation-and-feedback
loop. All purely additive on disk (`SCHEMA_VERSION` stays at 1) and
on the wire (legacy clients still get the same shape modulo the new
fields). Two themes: making the use-recording flow opt-out instead
of opt-in, and tightening the staleness-and-curation signal so the
model can self-prioritise without paying the full `memory_health`
cost on every turn.

### Added

- **`category="ambient"` on `memory_write`.** Joins `fact` (default,
  unchanged) and `user-inference` (existing pending-write gate). Use
  for atmospheric / response-shaping memories that don't make crisp
  verifiable claims (user identity, persistent environment quirks).
  Persisted on the memory record (legacy memories load with
  `category=None`; runtime treats that as the legacy fact-default).
  Ambient memories are excluded from the dead-weight curation rule
  because their value is implicit. A non-blocking
  `ambient_body_long` warning attaches to commits whose body exceeds
  500 words, so ambient memories don't drift into catch-all dumps.
- **`cold_memories` bucket on `memory_health`.** Memories created
  before the window with zero retrievals — distinct from
  `dead_weight`, which now means "retrieved but never `applied`".
  The two together separate "ranker isn't surfacing this memory"
  from "model retrieves but never gets value", so a curation pass
  can act on the right axis. `ScopeHealth.cold` mirrors `dead` for
  the per-scope rollup; `bettermemory health` text rendering shows
  both sections.
- **`staleness_verdict` derived field on every retrieval.** One of
  `"fresh" | "spot_check_recommended" | "spot_check_required"`,
  rolled up from `verification.status`, `path_drift_missing`, and
  `commit_drift_count`. Surfaced on `memory_show`, every
  `memory_search` hit (re-derived for the expanded top hit once
  body-level drift is known), `memory_list`, and the `with_bodies`
  list shape. The underlying signals stay; the verdict is the
  load-bearing field consumers should branch on first.
- **Auto-`record_use` via `use_token`.** Every `memory_search` hit and
  `memory_show` response now includes an opaque `use_token`. If the
  model doesn't call `memory_record_use` within ~2 turns, the server
  auto-commits the retrieval as `outcome="applied"` on the next
  memory_* call (logged with `auto=true`). The mechanical
  bookkeeping that was the most-forgotten step is now opt-out
  instead of opt-in. Explicit `memory_record_use(memory_ids=[...],
  outcome="ignored"|"contradicted"|"corrected")` still wins — the
  override path purges the pending token before recording so the
  auto-commit can't shadow the explicit outcome.
- **`curation_pending` rollup on `memory_scope_overview`.** Five
  integer counts — `{stale, never_verified, drifted, cold, dead}`
  — derived from the same logic as `memory_health` but without
  row materialisation. Lets the model spot pending curation at
  session start without paying the full health cost.
- **`scope_mismatch` warning at `memory_write` time.** Same design
  family as `transient_warning` and `duplicate`. If the body cites a
  known `projects:<name>` scope's name token (or a path under
  another project's tree) AND that scope isn't in the declared
  scope list, the write returns
  `{status:"scope_mismatch", suggested_scopes:[...], matches:[...]}`
  instead of committing. Override via
  `acknowledge_scope_mismatch=True` for legitimate cross-project
  references.
- **Structured `verified_claims` on `memory_verify`.**
  `verified_paths`, `verified_commits`, and `verified_versions`
  optional list parameters (caller passes the actual claims they
  spot-checked). Persisted on the memory record. The path-drift
  detector now surfaces a `verified` set on `PathDriftReport` (paths
  in the body that the caller previously attested AND that still
  exist on disk). The commit-drift signal narrows the count to
  commits that actually touched any of `verified_paths` — a memory
  verified for `[/etc/foo]` reads as `clean` even when the
  surrounding project moved, as long as `/etc/foo` itself didn't.
  `commits_since_touching_paths` in `origin.py` is the new git
  helper underneath. Calling `memory_verify` with `verified_paths=None`
  preserves any prior attestation; an explicit empty list `[]` clears
  it.

### Changed

- **`dead_weight` rule.** Was: `created_before_window AND applied_count == 0`.
  Now: `created_before_window AND retrieval_count > 0 AND applied_count == 0`,
  with ambient-category memories excluded entirely. Memories that aren't
  being retrieved are no longer mis-classified as dead — they go to the
  new `cold_memories` bucket where the curation question is "does the
  trigger for this memory still exist?", not "is the body misleading?".
- **`Memory` frontmatter.** New optional fields: `category`,
  `verified_paths`, `verified_commits`, `verified_versions`. Additive;
  legacy memories load cleanly with default values. Unknown category
  values fall back to `None` rather than raising — older readers
  encountering a future-introduced category degrade to fact semantics.

### Internal

- New `scope_match.py` module with `detect_scope_mismatch`,
  `collect_project_scopes`, `collect_project_roots`. Mirrors the
  shape of `durability.py`'s transient-marker module.
- New `compute_staleness_verdict` helper in `verify.py`. Single
  source of truth for the three-valued rollup.
- New `curation_counts` helper in `health.py`. Reuses the partitioning
  logic from `compute_health` but skips row materialisation; numerical
  contract locked in via tests.
- New `commits_since_touching_paths` helper in `origin.py`. Path-filtered
  variant of `commits_since`; returns `None` (not 0) when no useful
  filter survives the repo-root resolve, so the verified-paths
  short-circuit falls back to the unfiltered count rather than
  under-reporting drift.
- `SessionState` extended with `pending_use_tokens`, `turn_counter`,
  `issue_use_tokens`, `consume_old_tokens`, `purge_use_token`,
  `advance_turn`. The auto-`record_use` flow is implemented as a
  per-handler `_advance_turn(state, recorder)` call at every
  memory_* tool entry.

## 1.1.1 - 2026-05-09

Packaging metadata patch. The 1.1.0 PyPI listing rendered without a
"Project links" sidebar because `pyproject.toml` had no `[project.urls]`
table — visitors landed on the package page with no path back to source,
issues, or release notes. No code changes; PyPI re-publish only.

### Added

- **`[project.urls]` table in `pyproject.toml`.** Surfaces Homepage,
  Repository, Issues, and Changelog links on the PyPI project page's
  "Project links" rail. Without these, the package page on PyPI had no
  path back to GitHub, the issue tracker, or the changelog. Picked up
  automatically at wheel-build time — no other plumbing required.

## 1.1.0 - 2026-05-09

Three themes:

1. **A third staleness axis on every retrieval** — repo-aware
   commit-drift, the cwd-aware sibling of `verification` and
   `path_drift`. Catches the failure mode where calendar verification
   reads fresh but the project the memory describes has moved on.
2. **Structural fixes for the audit-after-fix workflow** that left
   `has_unresolved_contradiction` stuck — a new `corrected` outcome
   on `memory_record_use`, plus a `resolution_timeline` on each stuck
   row so the next mis-step is self-diagnosable.
3. **Distribution** — a Claude Code plugin (`/plugin install
   bettermemory@bettermemory`) that bundles the MCP server
   registration with a system-prompt-level skill, plus install-friction
   cleanup (canonical snippet shape, namespaced default entry name,
   `--version` flag, `importlib.metadata`-sourced `__version__`,
   trimmed MCP `instructions` block that fits under Claude Code's
   truncation cap).

### Added

#### Retrieval & verification

- **`commit_drift` advisory on retrieval.** Repo-aware sibling of
  `verification` and `path_drift`. When the caller is in a checkout of
  a memory's origin repo, `memory_show` and
  `memory_search(expand_top=True)` attach a `commit_drift` block:
  `status` is `"clean"` (zero commits since the last `memory_verify`)
  or `"drift"` (the count is positive). On `"drift"`, `recommendation`
  is an actionable string. Absent on the response when the caller
  isn't in any repo, is in a different repo, or the memory was never
  verified — silence beats a noisy "unknown" branch every consumer
  would have to filter. `memory_search` hits also carry a cheap
  per-row `commit_drift_count` integer for triage without an
  `expand_top` round-trip. Closes the gap where
  `verification.status == "fresh"` only proves calendar freshness
  while the repo can sit several commits ahead. Implemented as
  `verify.compute_commit_drift` plus two helpers in `origin.py`:
  `commits_since(cwd, since)` (per-memory cost) and
  `commit_author_timestamps(cwd)` (one git call + bisect, used by the
  health rollup so the cost is independent of memory count).

#### Curation

- **`commit_drift_debt` rollup on `memory_health`.** When the server
  is in a repo whose memories live in this store, surfaces rows whose
  verification anchor sits behind HEAD, sorted most-commits-ahead
  first. Capped row list (top 20) plus an uncapped `total_drifted`
  count, matching the `verification_debt` shape. `current_repo` and
  `current_cwd` are echoed back. Null when the server isn't in a
  repo, git is unreachable, or no memory's origin matches the current
  repo. Distinct from `verification_debt`: that bucket asks "how long
  since I checked?", this one asks "did the world I was checking
  against move?".

- **`verification_debt` rollup on `memory_health`.** Partitions active
  memories into `never_verified` / `stale` / `fresh` against the
  configured `behavior.verification_stale_days` threshold. Capped row
  lists (top 20, oldest-first) for inline display, plus uncapped
  totals so a curation pass can tell "5 stale" from "500 stale"
  without enumerating. The three counts always sum to
  `total_active_memories`. Surfaced in both the JSON tool output and
  the `bettermemory health` CLI's text rendering.

- **`corrected` outcome on `memory_record_use`.** A fourth value
  alongside `applied` / `ignored` / `contradicted`, for the
  noticed-and-fixed-inline workflow: the caller has already run
  `memory_update` and/or `memory_verify` in the same turn, and this
  event is the audit-trail entry. Audit-only — increments a new
  `corrected_count` on `MemoryStats` but never raises the
  `has_unresolved_contradiction` flag, so the previous foot-gun
  (`contradicted` logged *after* the fix → flag stuck because the
  event timestamp landed later than the resolution) is gone
  structurally.

- **`resolution_timeline` on each `memory_health.contradicted` row.**
  Chronological list of `update` / `verify` / `contradicted` /
  `corrected` events for the memory, with their notes. Lets the model
  self-diagnose a stuck flag as out-of-order audit logging (resolution
  events present but predating the contradicted event) vs. genuinely
  unresolved (no resolution events after the contradiction) without
  grepping `.events.jsonl`. Only populated for rows in the
  contradicted bucket; other rows keep an empty list.

#### Writing

- **`category="user-inference"` structural confirmation tier on
  `memory_write`.** A second value alongside the default
  `category="fact"`. When the caller passes `"user-inference"` the
  write goes pending and returns
  `{status:"pending", pending_id, pending_reason:"user-inference"}`
  instead of committing — the consumer is expected to ask the user
  conversationally before calling `memory_write_confirm(pending_id)`
  (or `memory_write_cancel(pending_id)` if the user declines). Fires
  regardless of the global `behavior.require_write_confirmation`
  config: misattribution sticks, so the user always gets the veto on
  claims about themselves. Project / infra / reference / tooling
  facts continue to commit immediately under the default category.

#### CLI

- **`bettermemory export`** dumps the active memory store (and
  tombstones, by default) as a single self-describing JSON document.
  Round-trippable; intended for backup, machine-to-machine migration,
  or feeding an external indexer. Writes to stdout unless `--output`
  is given.

- **`bettermemory --version`** prints `bettermemory <version>` and
  exits 0. The version is sourced from `importlib.metadata`, so it
  matches whatever `pip show bettermemory` reports — single source of
  truth, no drift.

#### Distribution

- **Claude Code plugin** (`/plugin marketplace add 0Mattias/bettermemory`
  → `/plugin install bettermemory@bettermemory`). The repo doubles as
  a plugin marketplace; `.claude-plugin/marketplace.json` at the root
  lists `plugin/` as the plugin source. The plugin bundles
  `plugin/.mcp.json` (registers the MCP server via `uvx bettermemory`,
  so users only need `uv` on PATH) plus
  `plugin/skills/bettermemory/SKILL.md` (the long-form policy as a
  Claude Code skill, which loads into the system prompt without the
  truncation cap that limits the MCP `instructions` block). Manual
  install (`bettermemory init --client claude-code`) remains supported
  and unchanged in shape.

- **Public-repo hygiene files**: `SECURITY.md` (threat model + private
  disclosure flow + supported-versions matrix), `CODE_OF_CONDUCT.md`
  (Contributor Covenant 2.1), `.github/ISSUE_TEMPLATE/` (install
  failure, bug report, feature request — install failure asks for
  `bettermemory doctor --json` output up front),
  `.github/pull_request_template.md`.

#### Tests

- **CLI smoke tests** (`tests/test_cli_smoke.py`). 17 tests pinning
  the argparse glue: `--help`, `--version`, every subcommand's
  `--help`, in-process invocations of `health` / `doctor` / `init` /
  unknown-subcommand exit code, plus two subprocess tests that pin
  the `python -m bettermemory` packaging path. Lifts `server.py`
  coverage from 65 % → 74 %.

- **Plugin manifest tests** (`tests/test_plugin.py`). Cheap validation
  guards: every plugin file exists, every JSON manifest parses,
  `marketplace.json` lists the plugin under the expected source path,
  the plugin manifest carries the conventional fields,
  `plugin/.mcp.json` registers the server under the canonical
  `bettermemory` key, the `SKILL.md` frontmatter has a non-trivial
  description and references the load-bearing tools. Plus
  version-sync tests that catch the case where `pyproject.toml`,
  `plugin/.claude-plugin/plugin.json`, and
  `.claude-plugin/marketplace.json` drift apart.

- **Version + instructions-budget regression tests**
  (`tests/test_version.py`, two checks in `tests/test_server.py`).
  Pin `bettermemory.__version__ == importlib.metadata.version("bettermemory")`,
  pin the `--version` output prefix, pin the MCP `instructions` body
  length under Claude Code's ~1.8KB truncation budget with both an
  upper bound (regrowth catch) and a lower bound (accidental wipe
  catch), pin a small set of load-bearing phrases in the body so a
  trimming pass that drops one shows up in CI.

### Changed

- **Default MCP entry name renamed `memory` → `bettermemory`.** The
  1.0 default was generic enough to collide with other MCP servers
  and with Claude Code's evolving built-in memory features; the new
  default is unambiguous. `bettermemory init --client X` detects a
  legacy `memory` entry whose `command` resolves to the same binary
  and removes it as part of the patch — upgrading users don't end up
  with the server registered twice. Migration is binary-equality
  gated, so a `memory` entry pointing at a different memory MCP
  server is left alone. The `--name` flag still overrides the default
  if you want the short name back.

- **MCP snippet shape includes `type: "stdio"` and `env: {}`.** Both
  optional in the MCP spec but match what `claude mcp add` and Claude
  Code 2.x write — the snippet now looks the same as the user's
  hand-added entries instead of looking deliberately minimal.

- **`bettermemory.__version__` reads from `importlib.metadata`.** The
  prior 0.x-era hard-coded literal had drifted past 1.0; switching to
  the metadata source makes drift structurally impossible. Falls back
  to `"0+unknown"` only when the package is imported from a source
  tree without an install (rare); a regression test pins the equality
  to `pip show`'s reported version.

- **MCP `instructions` block trimmed to ~1.6KB.** Claude Code 2.1.x
  truncates the block at roughly 1.8KB and renders an ellipsis
  mid-sentence; the previous body was over the cap and lost the
  writing-discipline tail to truncation. The trimmed body keeps the
  load-bearing parts (opt-in retrieval, when to / when not to call
  `memory_search`, the session-start hint, the transparency
  requirement, the verification rule) and pushes structural detail
  down to the individual tool descriptions, which are not subject to
  the same truncation. The longer-form policy lives in
  `docs/system_prompt.md` and in the plugin's `SKILL.md`.

### Fixed

- **`memory_verify` now resolves an unresolved contradiction.** Before
  this, only `memory_update` (which bumps `updated`) cleared the
  `has_unresolved_contradiction` flag. That left a sticky-flag failure
  mode: a session detects a contradiction, fixes the body, calls
  `memory_verify`, and *then* logs `record_use(contradicted)` after
  the fact — the event's timestamp lands later than the verify, so
  the flag never clears. The new rule treats either `updated` *or*
  `last_verified_at` newer than `last_contradicted_at` as resolution.
  Legacy stuck flags clear by re-running `memory_verify` after the
  contradiction event. The new `corrected` outcome (above) is the
  forward-looking fix for the same workflow.

- **`rare_scopes` only flags singletons that look like typos.** The
  previous heuristic flagged every `n=1` scope, which produced too
  many false positives — narrow legitimate scopes like `career` or
  `personal-context` got reported as suspect. The bucket now requires
  the singleton to be within Levenshtein distance 2 of another scope
  (`projct:foo` against `projects:foo`, `tool` against `tools`,
  `bug` / `bugs` pairs). Standalone narrow singletons no longer trip
  the bucket. Implemented via a new module-private
  `_edit_distance_within(a, b, max_dist)` helper in `health.py`.

- **`path_drift` no longer flags slash-prefixed CLI invocations as
  missing paths.** The extractor was treating backtick-wrapped Claude
  Code slash commands (`/plugin marketplace add owner/repo`,
  `/plugin install foo@bar`) and shell invocations starting at an
  absolute binary (`/usr/bin/env python -m bettermemory`) as path
  candidates, then routing them to `path_drift_missing` when the
  whole-string `Path.exists()` returned False — noisy false positives
  on any memory that quoted the install path or a shell example. The
  extractor now distinguishes command shape from real-paths-with-
  internal-whitespace by counting slashes: a CLI has either a
  single-slash command name as its first whitespace-separated chunk
  (`/plugin install …`) or 2+ adjacent slashless argument tokens
  (`/usr/bin/env python -m foo`), while a real path with internal
  whitespace (`/Users/Some User/file`) has slashes separating each
  directory boundary, so neither pattern fires. Counts `\` as well
  as `/` so Windows paths with internal spaces still pass. Five new
  regression tests in `tests/test_verify.py`.

## 1.0.0 - 2026-05-08

The first stable release. Three things changed under the hood between
the last 0.x and this one — they collectively retire every "but" the
README used to publish.

- **The system-prompt addendum is no longer required for correctness.**
  The opt-in retrieval policy, transparency requirement, and
  verification obligation now live in the MCP server's
  `instructions` block, which clients surface at the system-prompt
  level. Strangers who install bettermemory and skip the addendum get
  the right behavior anyway. The addendum file remains as the
  advanced tightening surface (full scope hygiene, confirmation-tier
  policy, expanded record-use guidance), but nothing breaks without
  it.

- **One-command onboarding.** `bettermemory init --client X` writes
  the right MCP config snippet into the right file for Claude Code,
  Claude Desktop, Cursor, Continue, or Cline. Idempotent: re-run
  with the same arguments is a no-op, with a different binary path
  is an update. `bettermemory doctor` diagnoses the most common
  install failures (binary on PATH, storage writable, MCP client
  configs cross-checked against the resolved binary path) with a
  one-line fix hint per check.

- **The 1.x contract is contractual.** Every memory and tombstone
  carries `schema_version: 1` in its frontmatter. Within 1.x the
  on-disk format and the 17-tool surface (signatures, defaults,
  return shapes — all pinned in `docs/api.md`) are stable. A
  multi-process concurrency stress test exercises the cross-process
  fcntl locks under contention to retire the previous "untested"
  caveat. Property-based tests pin the store's identity / round-trip
  / tombstone-restore invariants under random input.

Other surface updates: storage benchmark + Performance section in
the README documenting the practical ceiling (~50k memories before
the no-index walk dominates), CONTRIBUTING.md with the explicit
deprecation policy, programmatic-client example in
`examples/programmatic_client.py`, PyPI release workflow with
trusted publishing.

### Added

### Added

- **Cline support in `bettermemory init`** plus a comprehensive
  per-client setup doc at `docs/clients.md`.
  - Cline (the VS Code extension by `saoudrizwan.claude-dev`)
    joins the `--client` choices alongside claude-code,
    claude-desktop, cursor, and continue. Default target is the
    standard VS Code `globalStorage` path; Code-Insiders /
    Codium / VSCodium variants override via `--config-path`.
  - `docs/clients.md` collects the canonical config snippet and
    config-file location for each supported client, plus the
    expected restart behavior (Claude Desktop loads at startup
    and needs a restart; Continue auto-reloads; Cursor reloads
    per-window). The "snippet shape" is the same `mcpServers`
    map for every client because that's what the MCP spec
    standardizes — only the file path varies.

- **Programmatic client example**
  (`examples/programmatic_client.py`). A self-contained Python
  script that spawns `bettermemory` over stdio (via the official
  `mcp` SDK that's already a runtime dep, so no extra install)
  and walks through write → search → show → remove. Useful as a
  reference for integration tests, custom agents that want
  memory tools without a third-party MCP host, and one-off
  scripted curation passes. Defaults to a tmp dir for
  `BETTERMEMORY_DIR` so it never touches the user's real
  store; falls back to `python -m bettermemory` when no
  installed `bettermemory` binary is on PATH.

- **`CONTRIBUTING.md`**, with the explicit 1.x compatibility
  contract. Covers local dev setup, PR conventions, the
  versioning + deprecation policy, and the project values that
  shape review judgment. Headline: within 1.x, the surface in
  `docs/api.md` and the on-disk format pinned by
  `models.SCHEMA_VERSION` are stable; renames / removals /
  semantic redefinitions land at 2.0 with a documented
  migration path. Deprecation cycle requires at least one
  minor's notice in the changelog plus a one-time WARNING log
  per process before any 2.0 removal.

- **API surface document** (`docs/api.md`) pinning the 17-tool
  surface as the 1.0 contract. Covers signatures, defaults,
  return-status shapes (e.g. memory_write's
  `ok` / `transient_warning` / `duplicate` /
  `previously_removed` / `pending` discrimination), and the
  audit conclusions for each consistency dimension we checked
  (naming, plural-vs-singular, required-vs-optional,
  enums-as-strings, mutually-exclusive optionals). Findings:
  no signature requires a rename or default change before 1.0;
  the surface is frozen. README links to the doc from the
  Tools section.

- **Property-based tests for `Store` invariants**
  (`tests/test_store_properties.py`, six properties under
  `hypothesis`). Each property mints its own per-example subdir
  under `tmp_path` so hypothesis's fixture-reuse model doesn't
  accumulate disk state across examples. Properties under test:
  write round-trip identity (body and scopes survive); update
  preserves `id` + `created` and bumps `updated` monotonically;
  tombstone-then-restore is body- and timestamp-preserving;
  `mark_verified` is idempotent and monotonic; `load_all` is
  order-deterministic across consecutive calls; independent
  writes don't pollute each other on disk. `max_examples=10` per
  property — each example does real disk I/O, so the goal is
  breadth of input space (Unicode, near-empty strings, scope
  shapes that pass the regex but stress the formatter), not
  exhaustive enumeration. Adds `hypothesis>=6.0` to dev deps.

- **Storage benchmark** (`bench/storage.py`) and a **Performance
  characteristics** section in the README. Bench measures `Store.write`
  throughput, `Store.load_all` full-corpus scan, and `search()` keyword
  scoring across configurable corpus sizes. Default sizes are
  `1000,10000,50000`; output is a markdown table or `--json`. Bench
  runs in a `tempfile.mkdtemp` directory and tears it down on exit
  rather than ever touching the user's real `~/.claude-memory/`.

  Numbers from one run on Apple Silicon ship in the README so users
  have a reference shape for the latency curve before doing the bench
  themselves: ~16 ms search median at 1k memories, ~170 ms at 10k,
  ~1 s at 50k. Practical ceiling for the current
  no-index-walk-every-file architecture sits around 50k memories,
  which is far past where most stores ever grow if curated.

- **`schema_version` on frontmatter** (`models.SCHEMA_VERSION`,
  currently `1`). Every new memory and tombstone written by the
  store carries `schema_version: 1` as the first frontmatter
  key. Readers default to `1` when the field is absent — that's
  the implicit version of memories written before this constant
  existed (additive-fields-only era, where backward compat held
  by virtue of every new field being `Optional`).

  Forward-compatibility rule (now contractual rather than
  implicit): a reader that sees a memory whose `schema_version`
  is *strictly greater* than its own `SCHEMA_VERSION` raises
  `ValueError` on load. `Store.load_all` and
  `Store.load_tombstones` catch that and skip the file with a
  logged warning; `bettermemory doctor`'s `memory_parse_health`
  check surfaces the count gap. Net effect: a user who downgrades
  bettermemory after writing some memories under a newer minor
  sees those memories drop out of the retrieval surface (and
  flagged by doctor) rather than risk silent semantic
  misinterpretation. Tombstones share the same gate.

  Within a major version, bumps remain additive-only — new
  optional fields, never renamed, never removed, never
  re-defined. A *major* bump (1 → 2) is reserved for genuinely
  breaking format changes and will ship alongside a
  `bettermemory migrate` subcommand. The constant stays at 1
  until that day.

- **Multi-process concurrency stress test** (`tests/test_concurrency.py`).
  The README previously hedged: *"A file-lock guard is in place;
  multi-process is still untested."* This test spawns four worker
  processes (each its own Python interpreter via `spawn`, not `fork`,
  so the cross-process fcntl lock is actually exercised) and runs 50
  random write / update / remove / restore operations per worker on a
  shared store directory. Post-conditions assert: every active `.md`
  file parses cleanly (no torn writes), every tombstone carries the
  expected removal frontmatter, the event log is fully parseable JSONL
  (no half-line corruption at the append boundary), the active +
  tombstoned file count matches the worker write totals (no lost or
  duplicated IDs), and the lock isn't pathologically over-contended
  (concurrency_errors stay below ~25% of total attempts). The README
  caveat is updated accordingly: multi-process on Unix is now an
  exercised guarantee. Windows still falls back to a no-op lock — the
  MVP single-process recommendation stands there.

- **`bettermemory doctor` subcommand.** Self-diagnostic for the
  install: a series of independent checks that each return an
  `ok` / `warn` / `fail` verdict with an actionable fix hint when
  not ok. Exit code is `0` / `1` / `2` so the command is
  scriptable.

  Checks: Python version, binary on `$PATH` (warn when missing —
  GUI MCP clients spawn with a minimal PATH), config loadable,
  storage directory exists/writable (probe-write a sentinel file),
  memory frontmatter parses on every active memory, event log
  writable, semantic-dedup extras present when `semantic_dedup =
  true`, and a cross-check of every known MCP client's config file
  against the resolved binary path (catches the "I reinstalled
  bettermemory into a different venv and now nothing works"
  failure mode — the registered command path is stale).

  Each check is wrapped in `try/except` so a single broken probe
  surfaces as a `fail` diagnosis rather than crashing the whole
  report. JSON output (`--json`) is the machine-readable view for
  tooling; text output uses ✓ / ⚠ / ✗ glyphs and includes the fix
  hint inline. The `docs/installation.md` troubleshooting section
  now leads with `bettermemory doctor` rather than walking down
  the failure list manually.

- **`bettermemory init` subcommand.** One-shot onboarding that
  replaces the old "find your client's MCP config file, hand-edit
  the JSON" step. Two modes:
  - Show-and-tell (no flag): prints the resolved `bettermemory`
    binary path, the canonical `mcpServers` snippet, and a list of
    common per-client config locations with `[✓]` markers showing
    which already exist on the machine.
  - Patch (`--client X`): idempotently merges the bettermemory entry
    into the named client's MCP config file (creating parents and
    the file if missing). Re-running with an unchanged target is a
    no-op; a stale binary path is updated rather than duplicated;
    other entries in `mcpServers` are preserved.

  Supported clients: `claude-code`, `claude-desktop`, `cursor`,
  `continue`. Each entry is one getter function in `init.py`'s
  registry; adding a new client is a single-file change.

  Additional flags: `--print-only` (dump snippet without writing,
  useful for `| jq`), `--json` (structured output for tooling),
  `--name` (override the `mcpServers` key, default `memory`),
  `--config-path` (override the default target path for `--client`),
  `--with-addendum` (also print the optional advanced-tightening
  addendum from `docs/system_prompt.md`).

  README install instructions now lead with `bettermemory init
  --client X` rather than walking through manual JSON editing.
  `docs/installation.md` reframed in the same shape.

- **PyPI release workflow** (`.github/workflows/release.yml`).
  Tag-triggered: pushing `v<X.Y.Z>` runs the full gating suite (ruff,
  format, mypy strict, pytest with coverage floor), builds the wheel
  + sdist via `uv build`, publishes to PyPI through trusted
  publishing (no API tokens in repo secrets), and creates a GitHub
  release with auto-generated notes. Manual `workflow_dispatch`
  trigger supports a TestPyPI dry-run path. The build job verifies
  pyproject.toml version matches the tag before any artifact ships,
  so an off-by-one tag fails fast. Process documented in
  `docs/release.md`, including the one-time PyPI-side trusted-publisher
  setup. The 1.0 tag uses this workflow to publish — strangers get
  `uv tool install bettermemory` from PyPI directly.

### Changed

- **System-prompt addendum is no longer required for correctness.**
  Previously, `docs/system_prompt.md` was an explicit setup step
  (README "Quick start" step 3, with a bold warning that *"without
  this, the model will overuse memory"*). The opt-in policy,
  transparency requirement, and verification obligation now live in
  the server-level FastMCP `instructions` block — which every MCP
  client surfaces at the system-prompt level — and in each tool's
  `description`, refreshed per-call. A fresh install of bettermemory
  behaves correctly without copying anything from
  `docs/system_prompt.md`.

  The addendum file remains the canonical surface for **advanced
  tightening**: fuller scope hygiene, the confirmation-tier policy
  for preferences vs. facts, expanded record-use guidance,
  detailed verification ceremony. It complements the server
  `instructions`; it does not replace them.

  Touched: `src/bettermemory/server.py` (instructions block expanded
  from a 3-sentence hint to the full opt-in / transparency / verify
  briefing; per-tool descriptions on `memory_search`, `memory_show`,
  `memory_write`, and `memory_verify` extended to carry the
  obligations alongside their parameter docs). No behavior change in
  handlers; this is documentation-surface only. README and
  `docs/installation.md` updated to reframe the addendum as
  optional.

### Added

- **Structured `verification` block on every retrieval.** `last_verified_at`
  used to be a raw timestamp the consuming model had to do staleness
  arithmetic on — and prose-only guidance ("spot-check before relying")
  failed open whenever the model's attention wavered. A real-world
  drift escaped the system this way (a memory whose tool list lagged
  the code by three new tools went undetected because the consumer
  didn't notice `last_verified_at: null`). Retrieval responses now
  carry a structured verdict the model cannot easily skim past:

  ```json
  "verification": {
    "status": "never" | "stale" | "fresh",
    "last_verified_at": "<iso>" | null,
    "age_days": <int> | null,
    "recommendation": "<actionable string>" | null,
    "stale_after_days": <int>
  }
  ```

  - `status="never"` when the memory has not been spot-checked since
    write — `recommendation` carries an explicit "spot-check before
    relying, then call memory_verify" instruction.
  - `status="stale"` past `behavior.verification_stale_days` (default
    30, mirroring `recency_boost_half_life_days`) — `recommendation`
    names the age in days and asks for a re-spot-check.
  - `status="fresh"` within the window — `recommendation: null` is
    the explicit "nothing to do" signal so consumers branch on a
    stable shape.

  Surfaced on `memory_show`, every `memory_search` hit, every
  `memory_list` row (both summary and `with_bodies=True` variants).
  `last_verified_at` is preserved as a top-level field for back-compat;
  the new block is the structured replacement. The system-prompt
  addendum was rewritten to direct the model to branch on
  `verification.status` rather than read the prose. New
  `compute_verification_status` / `VerificationStatus` exports in
  `bettermemory.verify`. New `behavior.verification_stale_days` config
  knob — set to 0 to mark every verified memory stale immediately
  (test affordance), or raise the threshold for caches of facts whose
  ground truth changes slowly.

- **First-class tombstone lifecycle.** Removed memories used to be a
  black hole on the read side — invisible to dedup, invisible to search,
  with no path to restore short of hand-editing files. They now have a
  full lifecycle:
  - **`memory_list_tombstones(scopes?)`** lists removed records with
    their full removal metadata (`removed`, `removed_reason`,
    `removed_session`). Mirrors `memory_list` body-stripping for
    cheap triage. Sorted most-recent-first.
  - **`memory_restore(id)`** brings a tombstone back to the active
    set. Strips the removal frontmatter, preserves `created`,
    `updated`, and `last_verified_at` — the body didn't change while
    the record was gone, so the recency boost stays honest. Raises
    `NotTombstonedError` on active ids and `MemoryNotFoundError` on
    unknown ones; the asymmetry routes the caller to `memory_update`
    when they actually meant to edit.
  - **`bettermemory tombstones list` / `prune` CLI subcommands.**
    `list` mirrors the MCP tool. `prune --older-than DAYS` is the
    only path that hard-deletes tombstones; the default cutoff comes
    from new config knob `behavior.tombstone_retention_days` (0 means
    "no default — the flag is required"). Active memories are
    untouched. `--dry-run` previews; the prune is atomic and returns
    pruned ids in chronological order.
  - **`removed_session` frontmatter on tombstones.** The originating
    session id is now stamped into the file itself, so the join from
    a tombstone back to the session that produced it survives event-
    log rotation. Additive: legacy tombstones load with
    `removed_session=None`.
  - **`Store.restore`, `Store.list_tombstones`, `Store.load_tombstone`,
    `Store.prune_tombstones`** as the underlying API. The `restore`
    path handles active-filename collisions (when a new memory has
    squatted the slug since removal) by falling back to a short-id
    suffix, the same rule the active write path already uses.
- **Tombstone-aware dedup.** `memory_write` now scores the new body
  against the tombstone set in addition to active memories. A high
  overlap with a removed memory returns `status="previously_removed"`
  carrying the original `removed_reason` — the lesson encoded in the
  removal isn't silently re-discarded on re-write. The model can
  inspect the reason and either drop the write, call
  `memory_restore(id)` if the fact is now correct, or pass `force=true`
  if the new memory is meaningfully different. Medium-overlap
  tombstone matches surface as `removed_related` on a successful
  write, parallel to the active-side `related`. `SimilarHit` grew
  optional `removed_at` and `removed_reason` fields populated only
  for tombstone matches; the `relevance` ladder gained
  `"high-removed"` and `"medium-removed"` labels.
- **New `find_similar_tombstones` in `bettermemory.search`.** Mirrors
  `find_similar` for the tombstone path with both Jaccard and
  semantic-cosine modes. The semantic cache key uses the tombstone's
  `removed` timestamp, distinct from the active path's `updated`,
  so a restore-then-tombstone cycle produces correct cache
  invalidation.
- **`memory_rename_scope(old, new, include_tombstones?)`.** The cheap
  fix for typo'd or deprecated scopes (`projct:foo` -> `projects:foo`,
  `infra` -> `infrastructure`). Walks active memories — and
  tombstones, by default — and replaces the old scope with the new
  one, deduplicating if the new scope was already present. Bumps
  `updated` (metadata moved); preserves `last_verified_at` (the body
  didn't change). Validates both scopes against the standard scope
  format; rejects renames into a non-allowed scope when
  `[scopes] allowed` is non-empty. Returns
  `{active: [ids], tombstoned: [ids]}` for records actually modified.
- **`memory_health` observability extensions:**
  - **`scope_health`**, a per-scope rollup with active/dead/contradicted
    counts and an applied-events sum. Sorted by `active` descending so
    the heaviest-trafficked scopes lead. Sum of `active` across scopes
    can exceed `total_active_memories` because a memory tagged with N
    scopes is counted in each — that's the right shape for "where is
    the rot concentrated?"
  - **`rare_scopes`**, the singleton-scope bucket. Most often these
    are typos worth fixing via `memory_rename_scope`; occasionally
    they're legitimate one-offs to promote deliberately.
  - **`orphan_use_events`**, a counter of `memory_record_use` events
    whose memory_ids resolved to neither active nor tombstoned
    records. Growing counts are the smoke test for fabricated ULIDs
    on the model side.
- **Path-drift counts on every search hit.** `MemoryHit` carries
  `path_drift_checked` and `path_drift_missing` integers regardless
  of `expand_top`. The model can self-triage without a memory_show
  round-trip — high `path_drift_missing` is the cue to expand.
  `expand_top=True` continues to surface the full `PathDriftReport`
  on the top hit. Cost: one regex pass + up to 8 stat() calls per
  matched memory; the bodies are already in memory at search time.
- **Persistent embedding cache.** Behind
  `configure_persistent_cache(root, model_name)`, the in-process
  semantic-dedup cache flushes to
  `<root>/.embeddings.<safe_model>.npz` at the end of each
  `find_similar` call and rehydrates lazily on first use. A fresh
  MCP server doesn't have to re-embed the whole store. Wired up
  automatically when `[behavior] semantic_dedup = true`. Atomic
  `.tmp` + rename for crash safety; corrupt files log a WARNING
  and fall back to in-memory only. Model name is namespaced into
  the filename so swapping models produces a new file rather than
  mixing incompatible vectors. Numpy-only — degrades gracefully
  when the embeddings extra isn't installed.
- **`load_all` and `load_tombstones` are race-safe.** Both now catch
  `OSError` (notably `FileNotFoundError`) in addition to
  `ValueError`/`KeyError`, so a concurrent tombstone or prune that
  moves a file out from under the iteration yields the surviving
  records rather than crashing the call. Closes the gap where
  `memory_list(with_bodies=True)` could blow up mid-iteration.
- New `bettermemory.models.TombstonedMemory` and `TombstonedSummary`
  Pydantic models. Distinct types from `Memory` / `MemorySummary`
  so the type checker catches accidental mixing of active and
  removed records in callers walking both.
- New `bettermemory.store.NotTombstonedError` for the
  active-id-on-restore path.

### Changed

- **`memory_remove` stamps `removed_session` on tombstones.** The
  active session id is captured at removal time via the new
  `Store.tombstone(..., session_id=...)` keyword argument. Existing
  callers that don't pass `session_id` still work; the field is
  omitted from frontmatter when absent.
- **Auto-scope filter documented as a UX filter, not access control.**
  `memory_show(id)` doesn't auto-scope, by design — the threat model
  is "don't surface irrelevant memories by accident", not "prevent
  cross-project information flow". For real isolation, use
  project-scoped stores via `./.claude-memory/` or `BETTERMEMORY_DIR`.
  Clarified in `origin.py` module docstring and the README.
- **`memory_write` dedup short-circuit order:** active high-overlap
  match wins over tombstone high-overlap match, since there's a live
  record to update. Medium matches from both passes surface as
  `related` and `removed_related` respectively. `force=true`
  bypasses both gates as before.
- **`memory_search` description updated** to advertise
  `path_drift_checked` / `path_drift_missing` on every hit.
- **`memory_health` description updated** to advertise `scope_health`,
  `rare_scopes`, and `orphan_use_events`.
- **`SYSTEM_PROMPT_ADDENDUM` updated** with the new tools, the
  tombstone-aware dedup contract, and scope-hygiene guidance.

### Behavior changes worth flagging

- A `memory_write` whose body re-creates a previously-removed memory
  no longer commits silently. It returns `status="previously_removed"`.
  This is intentional and is the whole point of tombstone-aware
  dedup. The `force=true` override is the same one used for active
  duplicates. Tests that asserted the old "tombstones are ignored"
  invariant have been rewritten.

- **`memory_verify` tool + `last_verified_at` field.** The orthogonal
  axis to content edits: `memory_verify(id, note=...)` bumps
  `last_verified_at` to now after the caller has spot-checked the body's
  claims against ground truth. Distinct from `updated`, which moves
  whenever `memory_update` rewrites content — verification is "a
  human/agent confirmed reality matched the body on this date", editing
  is "the body changed on this date". A typo fix bumps `updated` but
  not `last_verified_at`; a verify call bumps `last_verified_at` but
  not `updated`. The field is surfaced on every retrieval response
  (`memory_show`, `memory_search` hits, `memory_list`, `_committed`,
  `MemoryStats`) so staleness is visible at a glance — `null` means
  "never verified since write". Frontmatter is additive: legacy
  memories without the field load fine; malformed values silently fall
  back to `None` rather than crashing the load. Idempotent (calling
  twice slides the timestamp forward); records a `kind: "verify"` event
  with the optional note.
- **`memory_scope_overview` tool.** Cheap session-start hint —
  per-scope counts (no bodies, IDs, or summaries) so the model can
  decide whether `memory_search` is likely to be fruitful before
  spending tokens on it. Auto-scoped to the current repo by default
  (uses bit-identical `repos_match` semantics as `memory_search`, so
  "5 here" reconciles with "5 in search"). Returns
  `{current_repo, current_cwd, auto_scope, scopes, total,
  disabled_scopes}` with scopes sorted count-desc then name-asc for
  determinism. Respects session-disabled scopes. Pass
  `auto_scope=False` for the cross-project view.
- **Path-drift detection on retrieval.** `memory_show` and
  `memory_search(expand_top=True)` extract path-shaped tokens from the
  body and stat them. Drift is surfaced as `path_drift.missing` —
  advisory, not a verdict (could be a temporary mount or a path on a
  different machine). `path_drift` is `null` when no drift is found so
  the consumer branches cleanly. Detection covers backtick-wrapped
  paths (highest precision), bare absolute Unix paths, `~/`-rooted
  paths, and Windows drive-letter paths; URLs, SSH remotes,
  `user@host:path`, and short paths (`/x`) are filtered. Two-pass
  extraction with backtick spans masked before the bare scan avoids
  double-counting. Capped at 8 paths per body and 512 chars per path.
  `OSError` from `Path.exists()` (permission denied, ELOOP, etc.) is
  treated as missing — semantically correct for a staleness signal.
- New `bettermemory.verify` module: `PathDriftReport`,
  `detect_path_drift()`.
- `Store.mark_verified(id)` — bumps `last_verified_at` without
  touching `updated`.
- **`bettermemory health --min-applied N` CLI flag** to override the
  configured `heavily_used_min_applied` floor for one invocation.
- **`bettermemory migrate origin` CLI subcommand.** One-shot backfill for
  legacy memories that pre-date the auto-scope feature (no `origin:`
  block in frontmatter). Three routing modes, in priority order:
  - **`--scope-repo SCOPE=URL`** (repeatable): route by tag. Right tool
    for global memory directories whose memories already carry
    `projects:<name>` scopes — first matching scope wins. Memories
    matching nothing in the map fall through.
  - **`--repo URL`**: force-tag every legacy memory with this URL.
    Coarse — only right when you know all memories in the dir really
    do come from one repo.
  - **Auto-inference**: when memory_dir's parent is itself a git repo
    (project-scoped layout), the repo URL is read from `git config`
    and the parent path becomes the origin's cwd.

  `cwd` is set only on the auto-inferred path — that's the only mode
  where we have legitimate evidence for a per-memory cwd. The other
  modes leave it null rather than fabricating one. Branch is always
  null since we don't know the original.

  Idempotent (memories with existing origin are skipped), atomic per
  file (`.tmp` + rename), `--dry-run` to preview. Tombstones are
  skipped — backfilling origin into a removal record would change the
  audit log retroactively.
- New `bettermemory.migrate` module: `infer_origin_for_memory_dir`,
  `migrate_origin_in_directory`, `MigrationReport`.
- **Semantic dedup (opt-in).** Behind `[behavior] semantic_dedup = true`,
  `memory_write` dedup uses sentence-transformers cosine similarity
  instead of Jaccard on token sets — catches paraphrases ("the database"
  vs "Postgres") that lexical overlap misses. Requires the `embeddings`
  extra (`pip install bettermemory[embeddings]`); falls back to Jaccard
  with a single WARNING log if the extra isn't installed, so flipping
  the toggle without the deps is safe. Embeddings are cached per-process
  keyed by `(memory_id, updated)`, so an updated memory busts its own
  cache entry. New config knobs: `semantic_model_name` (default
  `"all-MiniLM-L6-v2"`), `semantic_high_threshold` (0.85),
  `semantic_medium_threshold` (0.65).
- New `bettermemory.semantic` module: `get_model()`, `cached_embed()`,
  `cosine_similarity_normalized()`, `reset_caches()`. Imports of the
  optional extra are lazy — the module loads cleanly without it.
- **`memory_health` tool + `bettermemory health` CLI subcommand.**
  Aggregates the event log against the active store and returns a
  structured report: dead-weight memories (created beyond `window_days`
  ago, never `applied`), heavily-used memories (top-K by `applied`
  count), unresolved contradictions (memories with a `contradicted`
  event whose timestamp is after their last `updated`), per-marker fire
  and override rates for the durability gate, and the scope distribution
  histogram. The CLI mirrors the tool — `bettermemory health` for
  human-readable text, `bettermemory health --json` for machine-readable.
  Use the CLI for offline curation; use the tool for in-conversation
  introspection.
- New `bettermemory.health` module: `compute_health()`, `HealthReport`,
  `MemoryStats`, `MarkerStats`, `render_text()`, `render_json()`,
  `report_for_directory()`.
- `bettermemory.__main__` shim so `python -m bettermemory` mirrors the
  installed `bettermemory` script.
- **`memory_record_use` tool.** The model reports how a retrieved memory
  landed — `"applied"` (shaped the response), `"ignored"` (off-topic),
  or `"contradicted"` (user/state contradicted the stored fact) — with
  an optional free-form `note`. Each call writes one `kind: "use"` event
  to the log. This is the feedback signal that makes dead-weight pruning
  and contradiction surfacing possible in the upcoming memory_health
  view; without it, retrieval is write-only from the model's POV.
- **Auto-scope metadata.** `memory_write` captures the current cwd, git
  remote URL, and branch at write time and persists them under an `origin:`
  block in the memory's frontmatter. `memory_search(auto_scope=True)` (the
  new default) filters results to memories whose `origin.repo` matches the
  caller's current repo — addressing the cross-project leakage failure
  mode where a memory written for Project A surfaces during Project B
  conversations. Legacy memories (no `origin` field) and writes from
  outside any repo are treated as global and always surface. `auto_scope`
  is logged on each search event so the filter's behaviour is auditable.
  `memory_show` and `memory_list(with_bodies=True)` surface the full
  origin so a caller can verify which repo a memory came from.
- New `bettermemory.origin` module: `Origin` (Pydantic model), `capture()`,
  `repos_match()` (URL-form-agnostic equality — `git@github.com:o/r.git`
  and `https://github.com/o/r` describe the same project).
- **Append-only event log** at `<storage>/.events.jsonl`. Every tool call
  records one JSON line: query/returned IDs for searches, status/scopes for
  writes, ID for shows/updates/removes, etc. Auto-rotates (gzip) when the
  active log crosses `[telemetry] max_bytes` (default 10 MB). Each event
  carries a per-process `session` id so retrieval and write streams can be
  correlated.
- **`[telemetry]` config section** with `enabled` (default `true`) and
  `max_bytes` (default `10_000_000`). `enabled = false` makes every event a
  no-op and never creates the log file.
- **Structural durability gate.** `memory_write` now runs a regex check
  against the body for transient-state markers ("currently", "today I",
  "we just", "the new", commit-SHA-like hex tokens, etc.) before dedup.
  Hits return `{status: "transient_warning", markers: [...]}` instead of
  committing. Pass `acknowledge_transient=True` to override after rephrasing
  or deciding the marker is durable in context. Overrides are recorded in
  the event log per-marker so we can compute the false-positive rate and
  trim the marker list against real traffic.
- New `bettermemory.durability` module: `TRANSIENT_PHRASE_MARKERS` (the
  canonical list, single source of truth) and `find_transient_markers()`.
- New `bettermemory.events` module: `Recorder`, `iter_events`,
  `iter_all_events`.

### Changed

- **`heavily_used_min_applied` threshold (default 3).** New config knob
  in `[behavior]` floors the `heavily_used` bucket on `applied_count` —
  at 1 the bucket was dominated by one-off acknowledgements rather than
  repeat-use signal. Threaded through `compute_health`,
  `report_for_directory`, the `memory_health` tool (`min_applied` arg),
  and the `bettermemory health --min-applied` CLI flag. Clamped to ≥1
  internally so a misconfigured `0` doesn't dump every memory into the
  bucket. Lower it to 1 on a fresh store; raise it as the event log
  matures.
- **`MemoryStats` carries `last_verified_at`** so a curation pass can
  treat applied count and verification age as orthogonal staleness axes
  without a second round-trip through the store.
- **`memory_update` resets `last_verified_at` when content changes.**
  The prior verification was for prose that no longer exists; resetting
  forces the caller to spot-check the new body before a downstream
  consumer trusts the timestamp. Scope-only or confidence-only updates
  preserve `last_verified_at` since the body's claims didn't move.
- **`SYSTEM_PROMPT_ADDENDUM` hoists the no-filesystem-memory override
  to the first paragraph.** Many client harnesses ship their own
  filesystem-backed memory description in their default system prompt;
  the override has to land at the top so it wins before any later
  instruction can re-frame the model into filesystem mode. Also
  documents the new tools (`memory_verify`, `memory_scope_overview`),
  the staleness signals (`last_verified_at`, `path_drift`), and the
  session-start hint pattern. `docs/system_prompt.md` updated to match.
- `SYSTEM_PROMPT_ADDENDUM` rewritten so the durability rule references the
  structural enforcement rather than enumerating markers. The model gets
  the principle from the prompt and the specific marker that fired from
  the tool response. `docs/system_prompt.md` updated to match.
- `SYSTEM_PROMPT_ADDENDUM` lists the full current tool surface
  (`memory_health`, `memory_write_confirm`, `memory_write_cancel` were
  missing) and explicitly overrides any harness-injected file-based
  memory directory (e.g. `~/.claude/projects/*/memory/` or a `MEMORY.md`
  index). The Claude Code harness injects a `# Memory` section pointing
  at a per-project filesystem path; without an explicit override in the
  addendum the model sees two memory systems and splits facts between
  them. The `memory_record_use` paragraph now also references
  `memory_health` so the dead-weight feedback loop is visible from the
  prompt itself. `docs/system_prompt.md` updated to match.
- `build_server()` accepts an optional `recorder=` argument. When omitted,
  a `Recorder` is constructed from the resolved `Config`.
- `SessionState` now carries a stable `session_id` for the lifetime of the
  process. `state.reset()` deliberately preserves it.
- **Testing & CI hardening.** Several gaps caught in one sweep:
  - Python 3.14 added to the matrix (was 3.11–3.13). macOS and Windows
    slots added (one Python version each, 3.13) for platform coverage —
    `platformdirs`, `fcntl`-based locking, and the macOS UF_HIDDEN
    workaround in `tests/conftest.py` are all platform-sensitive.
  - `--cov-fail-under=80` enforces a coverage floor (current 85.32%).
  - `ruff format --check` runs in CI; the tree was reformatted to bring
    26 previously-unchecked files into compliance.
  - `mypy --strict` runs in CI, configured in `pyproject.toml` (strict
    on `src/bettermemory`, looser on `tests/` to avoid pytest-fixture
    `Any` noise). A `py.typed` marker ships with the package so
    consumers get types. `types-PyYAML` and `mypy` added to the `dev`
    extra.
  - New `test-embeddings` CI job installs `--extra embeddings` and
    runs `pytest -m "not no_extras"` so the cosine-similarity code
    path is exercised against a real `sentence-transformers` install.
    The three `test_semantic.py` tests that assert *absence* of the
    extra are tagged `@pytest.mark.no_extras` (registered in
    `pyproject.toml`).
  - `.pre-commit-config.yaml` mirrors the cheap CI checks (ruff,
    end-of-file-fixer, trailing-whitespace, yaml/toml syntax) for
    local pre-push catch.
  - `.github/dependabot.yml` keeps `github-actions` and `pip` deps
    current on a weekly cadence with grouped runtime/dev PRs.
