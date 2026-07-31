# bettermemory upgrade plan — 2026-07-30

Written at HEAD `95af021` (v3.30.0) after a full fresh-eyes audit: four
parallel claims-vs-code auditors (retrieval, write path, trust machinery,
economics), live-store probes, a full local suite run (3,779 passed /
8 skipped, 5m14s), then six fact-pinning agents that verified every code
anchor this plan cites. This document is self-contained: the executing
session has no access to the audit conversation.

This file lives under `docs/audit/` deliberately: that directory is outside
the doc-claims checker's corpus (it globs `docs/*.md` non-recursively), so
the plan's line references and proposed-file paths cannot fail CI as the
code moves. The line references below were verified at `95af021` — treat
them as anchors to re-verify, not gospel, once commits land.

**Companion material:**

- `docs/audit/upgrade-plan-facts/` — six fact reports (trust, settlement,
  footprint, writepath, docsguards, retrieval-episodes) with the full
  file:line evidence and, at the end of each, a PLAN HAZARDS section.
  **Read the hazards for a workstream before implementing it. They are the
  difference between this plan and a naive one.**
- Memory `01KYRSJZ377EDVZ39SCHX546FK` — the audit's verified defect queue +
  measured reality of the headline claims.
- The standing directive memory (2026-07-26): bettermemory must become "a
  direct upgrade to yourself" — evidence the model can check, not vibes it
  must trust. That is this plan's single design principle: **surface
  evidence, not verdicts; never quote an unpinned number; subtract on
  telemetry, never on taste.**

---

## 0. Executor protocol

You are executing autonomously. The user does not track this codebase; you
own it end-to-end (standing grant 2026-07-09 covers `git push origin main`
AND release tags; scope confirmed "Everything — main + tags/PyPI").

**Entry steps, in order:**

1. Read this document fully, then the hazards sections of the fact pack.
2. `memory_scope_overview`, then `episode_handoff` — the planning session's
   closing episode (written 2026-07-30, takeaway beginning "UPGRADE PLAN")
   names the state you inherit. Any episode dated BEFORE 2026-07-30 is
   superseded by this document — earlier episodes carry their own "next
   work" lists (read-side SHA anchor, LongMemCode adapter, …); disregard
   those, this plan is the priority order now. (Handoff auto-resolve
   requires running from this same checkout; from anywhere else pass an
   explicit `prior_session_id` or use `episode_search`.)
3. `memory_show 01KYRSJZ377EDVZ39SCHX546FK` (audit record) and the release
   runbook memory `01KS9M8D32343QVS70RJHF7V6A`.
4. Confirm HEAD is at or past `95af021` and `uv run pytest -q` is green
   before touching anything. If commits landed since this plan was written,
   premise-check each item before implementing: an already-fixed defect gets
   closed with a note, not re-implemented.

**Operating rules (non-negotiable, all grounded in project history):**

- **Ship discipline**: local full gate — BOTH legs, with and without the
  embeddings extra (different code paths since `ba7e857`) — then commit,
  `git push origin main`, WATCH the full CI matrix green (`gh run view
  --json conclusion` plus every leg), and only then any tag. Release
  mechanics: the runbook memory + `docs/release.md` ("seven fields across
  six files" — server.json carries the version twice). Where this bullet's
  order differs from `docs/release.md` step 6 (its back-to-back pushes),
  THIS bullet wins: create the tag only after CI is green on main, and
  re-run `tests/test_changelog.py` between `git tag` and the tag push —
  that re-run is the one with teeth.
- **Never snuff a feature.** Subtraction requires telemetry (precedent: the
  sha-marker retirement at 45/47 overrides, `durability.py:155-269`).
  Principled recuts are in scope; silent removals are not.
- **Bench religion**: every item marked BENCH-GATED ships only if its
  predicted win reproduces on the named benchmark. A negative result is a
  success outcome: commit it (bench README + results JSON) and close the
  item. Pre-registered claims and published scorecards are FROZEN — new
  behavior gets a NEW arm name, never a retroactive regrade (see B1).
- **Artifact pinning**: any number added to a model-facing surface (DESC_*,
  README, instructions) must be derivable from a committed artifact. Item
  A5 builds the CI guard for this; until it lands, enforce it by hand.
- **Budget caps**: lean descriptions are hard-capped at 27,500 chars
  (`tests/test_server.py:5804`, enforced at `:5919`); current total 26,336
  → 1,164 chars of slack. The instructions block is capped at 1,700 chars
  with only 92 chars of headroom — do not move prose into it. Roughly ten
  tests pin exact DESC substrings (see the footprint fact pack, hazard 1) —
  check them before any DESC edit.
- **Docs discipline**: run `tests/test_doc_claims.py` after every doc edit.
  Public docs stay lean (standing LESS IS MORE directive). CHANGELOG
  corrections to released sections use the erratum register: append an
  `### Erratum (YYYY-MM-DD)` subsection opening "The entry above is left as
  it shipped." — never rewrite shipped text.
- **Autonomy**: resolve every in-plan judgment call yourself. Stop only for
  a genuine contradiction between this plan and repo reality that
  premise-checking cannot resolve — and then leave a one-line resume note,
  not a multiple-choice question. Nothing in this plan touches the live
  user store destructively; it is code/docs/bench work only.
- **Journal**: `episode_write` at every session close and after every phase
  ships — takeaway names shipped SHAs, CI run ids, and the next entry
  point. Update this document's item statuses inline (DONE `sha` /
  CLOSED-negative / DEFERRED-reason) in the same commit as the work.

**Definition of done for the program:**

1. Zero false claims on resident/marketing surfaces; the A5 guard enforces
   number-pinning in CI.
2. The staleness alarm escalates only on evidence measured precise —
   pooled alerts-per-catch < 1.5 on the new bench/rot arms, or the
   demotion branch (B2b) shipped with DESCs updated to match.
3. Pooled LongMemEval R@5 ≥ +2 over the pinned baselines (89.3 lexical /
   91.8 semantic) via read-side diversification, or a committed negative
   result.
4. Lean DESC total ≤ ~24k chars with the ceiling ratcheted; served
   inputSchema stripped of pydantic title bloat (~2k chars); one aggregate
   footprint number computed in CI.
5. Zero silent TTL evictions of use-tokens (the expiry event lands), and a
   hookless store can no longer accumulate false dead-weight/demotion
   evidence. (Process exit, `SessionState.reset()`, and registry LRU
   eviction remain event-free loss paths — accepted, named in D1.)
6. Write-path holes closed with tests that would have caught them:
   F1 (force), F2 (scope-mismatch), F3 (confirm re-gate), F4 (pending
   persistence), F5 (user-claim gate), F7 (accept parity). F6 ships
   config-gated default-off (hardening, not a closure); F9 and F10 are
   measured-then-decided.
7. Suite + CI matrix green at every ship point; every phase independently
   releasable.

**Explicit anti-scope (do not do):**

- No embeddings-by-default (+2.5 R@5 pooled at 4× runtime per
  bench/longmemeval — doesn't pay). Default install stays lexical.
- No swarm phases 2–5; no cloud/SKU/multi-user/RBAC (roadmap anti-scope).
- No new always-resident tools without an explicit budget trade.
- No relevance-label default flip without the telemetry_v2 shadow lane
  (the v1→v2 flip was measured and rejected once; the roadmap documents
  why the successor rule needs a rule-signature change).
- No tool or parameter removal within 3.x — the CONTRIBUTING compatibility
  contract forbids it; merged replacements may be ADDED in a minor with a
  deprecation cycle, removal is 4.0 material.
- No editing frozen CHANGELOG history or frozen pre-registrations.
- Do not touch the `~/.claude/commands/` loop skills' STOP gates.
- Do not homogenize `Source.EXPLICIT` vs `Source.INFERRED` stamping across
  write paths as a side effect — provenance semantics feed curation.
- The transient gate's closed 51-phrase list is porous by design
  (precision-first; "presently" / "the build is red" pass). This is an
  ACCEPTED trade, same as the credential gate's all-lowercase guard —
  expanding the phrase list is whack-a-mole. If it is ever revisited, the
  entry ticket is override-rate telemetry per marker (the sha-marker
  retirement is the template), not taste.

---

## 1. What the audit established (compressed)

**Retrieval** (bench/longmemeval 500q; bench/retrieval): lexical R@5 89.3 /
semantic 91.8 vs claude-mem 91.6 — a tie; semantic costs 4× runtime for
+2.5 pooled. Re-query arm: lexical 80→100 R@5 — caller behavior is the
cheapest big lever. Read-side diversification is worth +3.2 pooled
(co-evidence sits at median rank 9 and is cut by top-k) — larger than the
whole semantic lift, no model needed. The live DESC still cites "measured
10%→65% recall@1", a number bench/retrieval was created to retire (blind
replication: 35–40% asked-baseline; the README forbids the comparison and
no committed artifact backs 10/65 at all).

**Staleness** (bench/rot, 30 repos / 37,635 claims): shipped detector
J=0.2875, precision 0.2945, 3.4 alerts per catch; the path-drift leg fires
on 0.0% of claims in the relative-citation arm (relative citations are
excluded from existence checking by design); own-repo pilot J=0.034,
p=0.415. Claim-level detectors reach J≈0.99 but exist only in the bench.
No real-world accuracy number exists (`resolution.py` parses 0/143
checkable real bodies; `resolution_rate` is UNDEFINED — and the module has
no tests). In-tree measured split that motivates the recut: prose-path
alerts ~0/15 real vs anchored-attestation alerts 3/3 (`verify.py:2207-2214`
records the numbers).

**Relevance label**: lexical-coverage thresholds, self-documented in-code
as "measuring LENGTH, not relevance" (`search.py:1066`, n=195 bucket
table). Pure-semantic paraphrase hits get `match_terms=[]` → `"low"`; the
DESC says treat low as noise; `expand_top` refuses non-high. The 4×-cost
capability is suppressed by the field callers branch on.

**Write gates**: precision-first and porous — closed 51-phrase transient
list; vendor-prefix-only credential shapes ("hunter2" and seed phrases
pass; a tested trade); Jaccard-only dedup (4/5 real paraphrase pairs
commit as parallel entries); no minimum body length; PendingGate triggers
on the category LABEL, so user-claims written as `category='fact'` commit
instantly (`proposals._PREFERENCE_RE` does content-shape detection, is
tested, and is wired only into the Stop-hook path). Fresh regressions from
`0073c70`: `ingest --force` is a silent end-to-end no-op (reproduced), and
ingest's ScopeMismatchGate hard-refuses realistic imports on any non-empty
store (reproduced; all four gate tests use empty stores where the gate is
structurally disabled). `memory_write_confirm` replays the staged payload
through zero gates; pending writes are in-process only and die on restart.
`accept_proposal` runs 1 gate of 6.

**Telemetry/hooks**: turn-end settlement, silent-miss telemetry,
endorsement excerpts, and by_model exist only via the plugin's single Stop
hook; the in-process fallback needs a second `memory_*` call inside
[600s, 1800s) or the use-token is deleted with NO event
(`session.py:385-393`). Hookless stores under-count applied and would feed
false dead-weight/demotion if `consolidate.auto_apply` were enabled. The
plugin has no SessionStart hook. Live store: endorsement ratios 0.09–0.15
on the heavily-used set (auto-applied dominates ~10:1); 64 memories
re-drifted within ~5 days of a full verify pass; the most-used memory is
an audit-loop state blob — durable working state needs a first-class home
(episodes).

**Economics**: 18 lean tools = 26,336 desc chars + ~7.1k inputSchema +
~1.8k outputSchema + 1,608-char instructions ≈ 37.5k chars (~9k tokens)
always-resident; 4.84× claude-mem per the project's own toolcost bench
(38,009 B published). Descriptions are ~75% of the wire cost, not the
README's "half". Three budgets are each policed; nothing sums them. Only
~1.8k chars of DESC prose is genuinely duplicated policy — the rest is
test-pinned reference material, so footprint work is a scalpel job, not a
chainsaw.

**Conflicts**: `_has_negation` is whole-body token presence (order-blind);
"Deploy blue-green; never in-place" vs the inverse scores Jaccard 1.0 with
no polarity flip → unattended consolidation would tombstone one side.
Fences today: `auto_apply` defaults False; tombstones reversible.

**What held** (don't re-fix): BM25/RRF/FTS5 real and rigorously tested; no
hidden LLM calls anywhere; telemetry local/redacted/0600; attestation gate
real since `a59f640`; benchmark integrity exceptional (three discarded
self-flattering runs, competitor's broken filter widened in the
competitor's favor, pre-registered MISSes published). The honesty
machinery covers mechanical claims only — every surviving lie is semantic
prose in a model-facing surface.

---

## 2. Workstreams

Sizing: S = hours, M = a session, L = multiple sessions. Every item lists
acceptance criteria (AC). File anchors were verified at `95af021`.
Item numbering has deliberate gaps (e.g. there is no C1/C4) — letters of
items culled during fact-checking stay retired; nothing is missing.

### Phase 0 — Truth-sync the resident surfaces (S each; ship first)

**PHASE STATUS: DONE 2026-07-30.** All items below shipped in one commit
(`docs+desc: truth-sync the resident surfaces`). Suite green both legs
(3,779 passed / 8 skipped default; 3,698 passed / 3 skipped / 6 deselected
embeddings), mypy + pyright + ruff clean. Executed by a 6-lane workflow with
a 3-lane adversarial verify pass. Deltas from the plan as written, all
verified against artifacts:
- A1 found **eight** live sites, not seven: `doctor`'s
  `retrieval_discrimination` fix hint (live operator output, not a docstring)
  and `config.py`'s `DEFAULT_CONFIG` (ships verbatim into every user's
  `config.toml`) were the two the plan's list under-weighted. Two *derived*
  claims were also false and are not in the plan's site list — "three times
  the cold-query hit rate" and "+15 points on top of" — both recomputed from
  the artifact (+25 asked, +10 re-queried).
- `DESC_MEMORY_SEARCH` ships with **no number**; the measurement plus its
  caveat moved to the module docstring. Net −2 chars (26,336 → 26,334).
- A7 grew by three sites the plan did not enumerate: `eval.py`'s "19-tool
  memory_* + 4-tool episode_*" (truly 22+5), `server.py`'s tool inventory
  (listed 21 of 27 while claiming addendum parity), and
  `bench/rot/corpus.py`'s docstring advertising `--filter=blob:none` clones
  that its own `clone()` documents as measured-wrong.
- The 3.29.0 CHANGELOG section got an **erratum** (append, not rewrite): its
  arithmetic was internally sound, but the measurement underneath it was
  retired, which is a different defect than the one first diagnosed.
- A6 re-measured: `_DESC_BASELINE` was stale in all **four** rows and its own
  explanatory note misattributed the drift across two commits; both fixed.

**A1. Retire the dead recall number everywhere; teach re-query instead.**
The sentence lives in `DESC_MEMORY_SEARCH` (`handlers/search.py:109-111`).
The guard test (`tests/test_server.py:6006-6043`) pins "nouns" and
"re-query" present and "paraphrase recall" absent — it does NOT pin the
numbers, so the fix cannot break it. But the same measurement family is
restated in `handlers/search.py:16-30`, the guard test's own docstring,
`config.py:70-72`, `semantic_setup.py:114-116`, `doctor.py:2299-2300`,
`tests/test_prompts.py:552-554`, `tests/test_semantic.py:423-430` — sweep
them all in one commit, or the DESC fix ships with seven live
contradictions. The sites split into TWO different measurements — do not
stamp one number into both:
- Query-discipline sites (the DESC, the guard-test docstring, the search
  module docstring): the lexical asked→requery pair. Replacement: no
  number, or 35%→80% recall@1
  (`bench/retrieval/results/v2-unpadded-2026-07-26.json`) with the
  bench's own caveat that its corpus is easier than a real store.
- Semantic-leg justification sites (`config.py:70-74`,
  `semantic_setup.py:114-118`, `doctor.py:2299-2301` — they justify
  installing the embeddings extra): the semantic pair from the SAME
  artifact — asked 35%→60%, requery 80%→90% — or no number.
Do not touch the 185-vs-190 store-size prose without also normalizing it
(the bench README documents the inconsistency).
AC: zero restatements of 10/65 as LIVE claims — CHANGELOG history,
bench/retrieval/README.md's retirement narrative, and docs/audit/ are
exempt; DESC under budget; suite green.

**A2. Fix the cost claims.** `README.md:45-47`: replace "~35 KB… the
description half" with artifact-backed numbers (38,009 B full serialized,
names+descriptions 28,604 B — call it what it is) or drop the numbers and
link `bench/toolcost/`. `docs/internals.md:81-86`: delete "stays small";
state the measured cost and the 4.84× comparison the project itself
publishes. Note: the toolcost artifact predates the `95af021` DESC trim,
so a re-run lands slightly lower — if you re-run, commit the new artifact
and cite that.
AC: `test_doc_claims` green; no unpinned numbers; A5 guard (Phase 1) will
hold this.

**A3. docs/incidents/: make the promise true (do not delete it).**
The directory charter (`docs/incidents/README.md:3`) currently scopes to
*reported* rot bugs — widen it in the same commit to cover self-found
defects, or the new postmortems contradict their own index. Write two now:
(1) the staleness-verdict constant function (J=0.000 at shipped defaults,
fixed `58a4fa4`; full evidence trail in the docsguards fact pack §4A);
(2) the `ad56c07` doctor false-green (fixed `316781e` same day; §4B).
Write (3) — the `0073c70` ingest --force regression — AFTER F1 ships so
"Fixed in" is real. Update the index (the "(No incidents yet.)"
placeholder is itself a claim that becomes false). `docs/incidents/` is
outside the doc-claims corpus; keep citations true anyway.
AC: ≥2 postmortems live; charter matches contents; README's promise true.

**A4. eval-results: two surgical hand edits.** `docs/eval-results.md:64-66`
(the `top_hits` parenthetical was false when written — `top_hits` ships on
`turn_audited` since 3.14.0, and `--widening-preview` exists precisely to
replay looser rules) and `:106` ("25 known tools" → note it reflects the
2026-07-16 snapshot; 27 exist at HEAD). Add a dated correction note; do
NOT re-run `bettermemory eval --report` to fix this — regeneration moves
every number and the measured date, desynchronizing the hand-authored
narrative (all-or-nothing; docsguards fact pack §6 and hazard 9).
AC: the two lines corrected, snapshot coherence preserved.

**A6. Re-measure `_DESC_BASELINE`** (`tests/test_server.py:5814-5836`;
4 entries stale at HEAD) in the same commit as any Phase-0 DESC edit — its
own stated rule.

**A7. Comment-truth sweep (S).** Three stale in-code prose sites verified
at HEAD: `models.py:650-652` claims episode scopes are auto-defaulted
from origin at the handler layer (not implemented);
`handlers/search.py:3` says "the 25 tools" (27 exist);
`tests/test_server.py:5780` says "24-tool power-user surface". Fix all
three; they are docstring/comment-only.

**F8-docs. Correct `0073c70`'s two false CHANGELOG claims — IN PLACE.**
Both sentences (the "scope allowlist … apply" line — no gate reads
`config.scopes.allowed`; enforcement lives in `_validate_write_payload`,
which ingest never calls — and "deliberately stricter copies" for
accept_proposal, which runs 1 gate of 6) sit under `## Unreleased`, which
has NOT shipped: unreleased text is not frozen history, so rewrite the
two sentences directly. The erratum register ("The entry above is left as
it shipped…") applies only if a release has already frozen them by
execution time — premise-check first. Also fix the overstating chokepoint
comment at `handlers/write.py:588-595` ("all four callers share" — only
memory_write and ingest do).

### Phase 1 — Guards (S/M; lock Phase 0 permanently)

**PHASE STATUS: DONE 2026-07-30.** A5, E4, H1 all shipped. Suite 3,818 passed
/ 19 skipped default leg, 3,745 passed / 6 skipped / 6 deselected embeddings;
ruff + mypy + pyright clean. Deltas from the plan:
- **A5 found a claim Phase 0 missed** — `doctor`'s fix hint asserting "100% on
  rare-term queries", in a string printed to operators. Phase 0 had hand-audited
  that very string and repaired the four rates below it. The guard earned its
  cost on its first run; both surviving claims were repaired, not allowlisted,
  so `_ALLOWLIST` is empty at HEAD.
- The plan (and docsguards fact pack §3) says the DESCs carry "exactly one"
  measurement claim. **Stale**: Phase 0 removed it, so it is zero. A5 verifies
  that rather than assuming it.
- The `measured`-cue discriminator has a real hole the plan did not name: the
  pre-sync README cost claim carried **no cue at all**. A5 adds one narrow
  cue-free byte/ratio rule scoped to README + internals, with a
  contract-constant exemption tested for ADJACENCY — a chunk-level cap test
  looks equivalent and would have blinded the guard to that same README bullet.
- **E4's ceiling was sized against measured param costs**, not a guess:
  `acknowledge_user_claim` 93 + `include_bodies` 76 + `ids` 106 = 275 chars, so
  the projected remainder after F5 and G1 is 9,881 against a 10,000 ceiling.
  Phase 2 and Phase 7 can build on it without a recalibration.
- Local-environment hazard worth recording: the default-leg baseline is
  **3,779 passed / 19 skipped**, not the 8 skips this document states — the
  11 extra are `importorskip("numpy")`, and numpy ships only with the
  embeddings extras. The plan's baseline was measured on a venv carrying a
  leftover numpy. Separately, iCloud renamed every file in `.venv`'s numpy to
  a ` 2` suffix mid-session, which defeats `importorskip` (the package still
  imports, as a namespace package) and turns clean skips into 22 failures.
  Rebuild `.venv` before trusting any local run that looks like that.

**A5. The number-pinning guard.** New test sweeping the 28 `DESC_*`
constants (re-exported by `_handlers.py:66-93`) + README + instructions
+ `docs/internals.md` (A2 adds measured numbers there; doc-claims never
checks numbers) for measurement claims. Key design fact: a naive `N%`/`N KB` pattern
false-positives on 5 of its 6 current hits (contract constants like the
4 KB / 64 KB episode caps and the 30% groundedness threshold, each
enforced by adjacent code); the discriminator that works is the word
**"measured"** adjacent to the number. Pin measured claims against
committed artifacts (`bench/*/results/*.json` — inventory in the
docsguards fact pack §3); allowlist idiom copied from
`test_doc_claims._ALLOWLIST` (keyed entries, ≥40-char reasons, forward AND
reverse ratchets). Include a negative self-test (inject a fake number →
guard must fail) — synthetic false examples live in comments, never
docstrings (new-module docstrings auto-enter the doc-claims corpus).
AC: guard passes at HEAD only because Phase 0 fixed the claims; the
negative self-test proves it can fail.

**E4. Aggregate footprint number.** One test computing instructions chars
+ lean DESC chars + serialized inputSchema + outputSchema + plugin skill
frontmatter (759 chars = the FULL name+description block; the fact pack's
726 is the description value alone — state the choice in the test; the
13.7k skill body is NOT resident, do not sum it). Reference values at
HEAD ≈ 37.5k chars. Design constraints: (a) don't double-govern —
instructions and DESCs already have individually-ratcheted caps, so the
new ceiling binds only the currently-uncapped remainder (schemas +
frontmatter) while REPORTING the total; (b) state the serialization
convention explicitly (bench/toolcost sorts keys and includes
outputSchema — pick one and say which); (c) set the remainder ceiling
with measured headroom for the schema growth this plan itself schedules
(F5's `acknowledge_user_claim` param, G1's two episode_search params) —
a ceiling that Phase 2 immediately re-recalibrates is noise.
AC: committed baseline + ceiling on the uncapped remainder; a silent
schema-growth regression fails it.

**H1. Tests for `bench/rot/resolution.py`** (newest bench module, zero
coverage): pin the 3/3 synthetic control, the 0-of-N real-body parse, and
`resolution_rate: null` (UNDEFINED, not zero) semantics.
AC: a regression that silently starts "parsing" real bodies (or flips
null to 0.0) fails.

### Phase 2 — Write-path hole closure (S/M each; parallelizable)

**PHASE STATUS: DONE 2026-07-30.** F1 F2 F3 F4 F5 F6 F7 F9 F10 all closed;
A3c postmortem written. The first implementation pass FAILED adversarial
verification — worth recording, because the failures were all one shape:
- **Two shipped features were inert or unsafe**, and their own tests passed.
  F6's `min_content_tokens` was never threaded from config into either
  validator call site, so the knob did nothing while `docs/api.md` and the
  shipped `DEFAULT_CONFIG` both promised it worked — the `--force` regression
  repeated exactly, inside the very phase whose postmortem names that lesson.
  F4's sidecar let a stale in-memory snapshot resurrect a consumed pending id,
  turning one `memory_write` into two durable memories.
- **A parameter reached a handler but not the wire** for the second time in one
  phase (`memory_proposals`' three overrides), because the `_handlers.py` facade
  signature IS the served schema.
- F5's gate correctly fired on 10 pre-existing fixtures that were genuinely
  user claims filed as `fact`/`ambient`; they took `acknowledge_user_claim=True`
  rather than any weakening of the gate.
All repaired and re-proven by reproduction, including a negative control on the
F4 fix (restoring the old semantics puts the store back to 2 memories).
- **F9 CLOSED with a measurement** (`bench/dedup/`), F10(c) done, F10(a)/(b)
  measured-then-decided — see the lane reports in the commit.
- Guard note: Phase 1's `_FOOTPRINT_BASELINE` had a real defect this phase
  exposed — the scheduled-reserve assertion read the recorded literal instead
  of measuring live, so the one guard meant to announce that promised headroom
  is gone could not see it go. Verdicts now measure; the table stays diagnostic.

**F1. Make `ingest --force` real.** The trap: threading `gc.force=True`
also skips `DedupTombstoneGate` (`handlers/write.py:505-506`) and
resurrects tombstones, violating the pinned asymmetry
(`test_force_does_not_resurrect_tombstoned_memory`). Correct shape: give
`apply_ingest_plan` a `force` param, thread from `cli/ingest.py:201`, and
when set drop `DedupActiveGate` from the gates tuple (keep `gc.force`
False) — e.g. filter the tuple by gate type. Add the missing END-TO-END
test: apply a forced plan, assert `action=="write"`, `written_id` set,
store grew (the current test stops at the plan — `test_ingest.py:573-610`).
Then write incident postmortem #3 (A3).
AC: `--force` writes the duplicate row it documents; tombstone dedup still
holds under force; e2e test in place.

**F2. Ingest scope-mismatch.** Set `acknowledge_scope_mismatch=True` in
ingest's `_gate_context` (`ingest.py:514-533`). Rationale (from the fact
pack): ingest is user-initiated CLI; auto-memory files citing their own
project's name are the norm, not the mis-tag signal the gate exists for;
and ack=True also skips the gate's per-row `load_all`. Add non-empty-store
tests (seed a `projects:<name>` memory, import a row citing `<name>`,
assert it lands) — the existing four gate tests all run on empty stores
where the gate cannot fire.
AC: realistic imports land on non-empty stores; the reproduced refusal
case becomes a test.

**F3. Re-gate at confirm time — without destroying the staged write.**
`take_pending` POPS (`session.py:201-204`); a gate reject after it leaves
nothing to re-confirm and orphans the promotion linkage. Design: peek
first (add a non-consuming lookup to SessionState), run Credential +
DedupActive + DedupTombstone against `pending.payload`, and only on
Continue consume + write. On reject return the normal gate status plus the
still-valid `pending_id`. Second trap: the original call's
`force`/`acknowledge_*` flags are NOT stored on `PendingWrite` — persist
them at `_stage_pending` (`handlers/write.py:893-952`) so a
force-staged write isn't re-refused at confirm. Update
`DESC_MEMORY_WRITE_CONFIRM` (currently promises only `committed`).
Ordering: F3 lands BEFORE F4 — F4 serializes `PendingWrite`, so its
sidecar schema must round-trip whatever fields F3 adds.
AC: duplicate landing during the 1h TTL → confirm returns `duplicate` and
the pending survives for a decision; force-staged writes confirm cleanly.

**F4. Persist pending writes.** Sidecar JSONL + flock, copying the
`ProposalQueue` idiom (`proposals.py:251-383`: read-modify-write under
`flock_excl`, `atomic_write_bytes(mode_before_rename=0o600)`). Traps from
the fact pack: (a) `payload` holds enum str-subclasses (fine) but
`origin` is a pydantic model — `model_dump()` on save, rebuild on load;
(b) pending writes are per-session state and the SessionRegistry exists
to prevent cross-client confirm — key rows by session id and preserve
that isolation; (c) TTL + the `was_recently_expired` window must be
enforced on load or the "expired vs never existed" distinction degrades;
(d) a new `*_FILENAME` dotfile constant trips `tests/test_sync.py`'s
structural gitignore test — add it to `sync._GITIGNORE_LINES` (staged
payloads are un-synced user content, same privacy stance as proposals).
AC: restart mid-confirmation preserves the staged write; expiry visible;
test_sync green.

**F5. User-claim soft gate on memory_write.**
New gate positioned after TransientGate, before ScopeMismatchGate (it is
a body-classification gate; must precede dedup and Pending). Fires only
when category != user-inference and the body matches a user-claim shape;
returns a `user_claim_warning`-shaped refusal with a new
`acknowledge_user_claim` escape (the framework has no non-blocking warn
on refusal paths — blocking-with-escape is the house shape).
Pattern design — VERIFIED CONSTRAINT: `_PREFERENCE_RE`
(`proposals.py:173-181`) matches FIRST-PERSON shapes only ("i prefer…",
"we use…", "^my/our…") — it was built for extracting from the user's own
words in the Stop hook. A model-authored `memory_write` claim is usually
third-person ("Mattias prefers tabs", "the user prefers…"), which it
does NOT match. Build a NEW gate-local pattern = `_PREFERENCE_RE`'s
shapes PLUS third-person forms (`the user|<name>` + preference verbs);
do NOT edit `_PREFERENCE_RE` itself (shared with the tested extractor).
Mirror production's application: per-sentence after smart-apostrophe
normalization (`proposals.py:475-485`), or the `^my/our` branch and
curly-quote bodies silently degrade.
Traps: (a) `CONTENT_GATES` is derived by exclusion
(`handlers/write.py:707-709`), so the new gate would auto-apply to
ingest AND (via F7) to accept_proposal — exclude it from ingest's tuple
explicitly, and see F7 for the proposals exclusion (the extractor stamps
explicit captures "remember that I prefer X" as `fact` by design; the
gate must not refuse their acceptance); (b) any new `GateContext` field
needs a default or both construction sites break; (c) new `memory_write`
params must be mirrored in the `_handlers.py` facade (`:417-448`) or the
MCP schema never exposes them.
AC: "Mattias prefers tabs" AND "I prefer tabs" as category='fact' warn
with a re-categorize hint; acknowledged writes proceed; ingest and
proposal acceptance unaffected; unattended callers get the refusal for
free.

**F6. Minimum-content floor — config-gated, default OFF.**
The fact-check killed the default-on version: `content="x"` is a
legitimate fixture in 34 handler-path test call sites, and the only
in-repo floor precedent (proposals' 30 chars / 6 tokens) is an order of
magnitude above what the test corpus treats as valid. Ship
`[behavior] min_content_tokens` (default 0 = off) enforced in
`_validate_write_payload` (which also serves update and accept_proposal —
document the three-tool blast radius), recommend it in docs, revisit the
default at 4.0.
AC: floor works when enabled; default behavior unchanged; no test churn.

**F7. accept_proposal gate parity.** Replace the hand-rolled
credential-only scan (`handlers/proposals.py:134-164`) with
`apply_write_gates(...)` with a tuple = CONTENT_GATES MINUS the F5
user-claim gate (filter by type, same idiom as F1) — the extractor
deliberately stamps explicit captures ("remember that I prefer X") as
`fact`, and their bodies match the preference shapes, so including F5's
gate would hard-refuse exactly the proposals the queue exists to carry.
All six content gates only READ the store, so the "nothing writes until
accept" invariant survives. Keep the refusal BEFORE `queue.remove`
(proposal stays queued); fold
`action`/`proposal_id` into the gate response; no event on refusal
(docstring contract). This deliberately changes duplicate-accept from
"reviewer's job" to a hard `duplicate` refusal — document it, update
`DESC_MEMORY_PROPOSALS`'s status vocabulary and the CLI flags
(`cli/proposals.py`). Do NOT convert consolidate's hand-rolled copy: its
stamped-vs-unstamped scan split is a measured decision one `GateContext`
cannot express (writepath fact pack §7 + hazard 10) — the roadmap's
"policy review" item stays open for it.
AC: a transient-marker or duplicate proposal can't be accepted unwarned;
refused proposals stay queued; consolidate untouched.

**F9 (optional, measured).** The dedup `related`-breadcrumb floor: 3/5
real paraphrase pairs get no breadcrumb (Jaccard 0.17–0.33 < 0.40).
Measure a 0.30 medium floor against the live store's shape before
shipping; if noise dominates, commit the negative result and close.

**F10 (S, measured-then-decided).** Ingest residuals from the audit
record that F1/F2 don't cover: (a) ingest runs dedup TWICE per row with
different threshold sources — compute is always lexical-0.75, apply reads
`semantic_dedup` config, so under `semantic_dedup=true` a green
`--dry-run` can commit fewer rows (the dry-run-lies class); aligning them
means threading config into `compute_ingest_plan` (signature change,
test fallout — writepath fact pack hazard 12); (b) the apply path is
O(rows × store) (2× `load_all` + `load_tombstones` per row) — measure on
a realistic import before optimizing; (c) delete the vestigial
always-true `gate_deps is not None` guard at `ingest.py:639`. Do (c)
now; (a)/(b) measure first, fix or close-with-rationale.

### Phase 3 — Trust recut: evidence, not verdicts (M/L; the design core)

**PHASE STATUS: DONE 2026-07-30.** B1 BENCH-GATE PASSED on the full 30-repo
corpus; B2a shipped; B2b implemented-but-NOT-flipped (correctly — the
measurement says it should not be); B3 shipped as `matched_leg` + a recut
label; B4 and B5 shipped. Suite 4,073 passed / 16 skipped default,
3,998 / 3 / 8 embeddings; ruff, mypy, pyright clean.

**B1 measured result** (`bench/rot/results/multirepo-anchored-2026-07-30.json`,
37,635 claims, 30 repos): the new `drift_only_relative_cite_anchored` arm
flags 0.73% of claims at **precision 1.000, false-alarm rate 0.0,
alerts/catch 1.0, J=0.032 pooled** (J=0.0505 on path-shaped claims, n=5,272,
Fisher p=0.0), repo-level paired **19 wins / 0 losses / 7 ties**. The
pre-registered `drift_only_relative_cite` arm still reads exactly 0.0, and
`multirepo.json` + `scorecard.json` are untouched — the new arm is appended
and separately published, so no prediction was regraded. Honest reading: the
recall is SMALL (0.73% against a 22.9% base rate); what the arm buys is that
a leg which fired on nothing now fires precisely on the subset it can prove.
- **B2b stays unflipped, on evidence.** The plan's flip condition was pooled
  alerts-per-catch >= 1.5. The commit leg's own measured cost does not meet
  it, so `commit_drift_count` stays in the escalation disjunction; the switch
  is isolated at one named place with a test pinning current behaviour, so a
  future measurement can flip it without archaeology. The demotion branch is
  untouched and separately pinned — removing it would resurrect the J=0.000
  constant function that Phase 0's postmortem memorialises.
- **B1 was NOT inert when first implemented**, which the AC required for a
  bench-gated item. Caught by verification; resolved by actually running the
  30-repo corpus rather than by adding a flag.
- Two embeddings-only test failures were invisible to every lane (they all
  ran the default venv): `matched_leg` legitimately reads `both` when the
  semantic leg is installed. Marked `no_extras` rather than loosened, because
  the exact string is what distinguishes the leg that RAN from the mode
  requested.
- Environment: iCloud corrupted `.venv` a second time, now `fastapi` (29 of
  29 files renamed). It presents as 11 mypy + 6 pyright errors in `web.py`
  and nothing else. Rebuild, do not debug.

Read the trust fact pack in full first — it has 12 hazards, and B1/B2
walk through frozen pre-registrations and signature pins.

**B1. Relative-citation path drift. BENCH-GATED, NEW ARM.**
Goal: existence-check relative citations resolved against the memory's
recorded `origin.worktree_root`, closing the "0.0% fire on the style
people actually write" gap. Constraints that shape the design:
- The existing zero-flag behavior is PINNED
  (`test_relative_citations_get_no_path_checking_at_all`) and P2 of the
  rot pre-registration ("relative arm: exactly zero path-drift flags") is
  a frozen published claim. The extension must be gated on
  `worktree_root is not None` (bare `detect_path_drift(body)` keeps
  returning nothing for relatives), and the bench gets a NEW
  anchored-relative arm — never regrade the existing arms
  (`_MODES` is positionally consumed; append only).
- `_RELATIVE_CITATION_RE` (`verify.py:280-285`) is deliberately
  over-matchy because commit-drift anchors are phantom-neutral; existence
  checking INVERTS that asymmetry (a matched bare domain like `pypi.org`
  would stat missing → fabricated drift). A stricter filter layer
  (require a known path prefix or ≥1 directory segment + real-extension
  heuristics + the existing route/placeholder guards) is mandatory, plus
  a stat-budget decision (relative anchors cap at 24; existence stats cap
  at 8 — reconcile).
- Cross-host: anchored checking against a synced-from-elsewhere
  `worktree_root` marks everything missing. The root-liveness leniency
  that exists in origin.py's filter side was deliberately NOT given to
  verify (`origin.py:374-379` records the opposite bias). Extending
  leniency to citation checks reverses a recorded decision — write the
  argument down (fail-open when the recorded worktree is absent/unstattable
  is the defensible line: a machine that has never seen the checkout has
  no evidence either way), and extend the adversarial zero-false-positive
  prose suite.
AC: new bench/rot arm shows path-leg J materially > 0 at precision ≥ 0.9
on the 30-repo corpus; zero new prose false positives; recorded-root-gone
→ skip (no false missing); existing arms and scorecard untouched.

**B2. Escalate the verdict only on evidence measured precise.
BENCH-GATED.**
The in-tree measurement (`verify.py:2207-2214`): prose-extracted path
alerts ~0/15 real; anchored-attestation alerts 3/3. The shipped pooled
detector: J=0.2875 at 3.4 alerts/catch. Recut in two steps:
(a) **Split path-drift provenance.** `PathDriftReport.missing` merges
anchored-attestation misses and prose-extraction misses, and
`has_drift = bool(missing)` is the only path input to the verdict. Add
provenance (anchored vs prose) and feed ONLY anchored misses (+ B1's
filtered relative citations once landed) into `path_drift_missing` for
verdict escalation; prose misses stay on the wire as advisory evidence.
Threading warning: a new bucket/tag must ride `MemoryHit` →
`hit_to_dict` → all three independently-gated emit sites, or it is
invisible (the `dropped_as_route` reach-note documents this exact
failure).
(b) **Commit leg**: it is already claim-anchored (the filtered
`git log -- <specs>` IS a changed-files intersection) — its noise comes
from over-broad anchors and the unfiltered fallback. After (a) + B1,
re-run the new bench arms; if pooled alerts/catch is still ≥ 1.5, drop
`commit_drift_count` from the ESCALATION disjunction ONLY and update the
DESCs to describe the verdict honestly. THE DEMOTION BRANCH IS
UNTOUCHABLE: `stale` + measured-zero → `fresh` IS the `58a4fa4` fix —
removing `commit_drift_count` from the verdict wholesale would resurrect
the J=0.000 constant function that A3's postmortem #1 memorializes, and
its regression tests exist to stop exactly that. Either branch is a
success; the measurement decides.
Signature constraints: `verdict_from_signals` is pinned to exactly three
signals (inspect-based test; its docstring names the evidence bar for
changing that) — implement by changing what FEEDS the signals. The
None-vs-0 distinction is load-bearing in both directions (measured 0
demotes calendar-stale to fresh; None must never become 0 or ~36% of
judgment-class bodies mass-demote). The per-search git cost shape
(2 + drifting-anchored-hits forks) is pinned — a batched `--name-only`
index (the bench has the parity reference) moves that test in the same
commit. The bench monkeypatches four `verify.py` module globals by name —
do not rename them.
AC: pooled alerts/catch < 1.5 on the new arms, or the (b) demotion branch
shipped; prose evidence still visible; all pins moved deliberately, none
bypassed.

**B5. Verify-time symbol-existence check (advisory).**
83 of 194 reference-store memories carry backticked symbol tokens, and no
production code resolves a symbol against a file — the claim-level
machinery that scores J≈0.99 is bench-only, and its corpus-format parser
reads 0% of real prose by construction. The viable production shape: at
`memory_verify` time (between snapshot-load and `mark_verified`,
mirroring `unverifiable_attestations`), AST-check body citations of the
`` `sym` in `mod.py` `` family against the file, and report an advisory
`symbol_drift` field. No new frozen-surface vocabulary
(`verified_commits`/`verified_versions` stay audit-only; the roadmap's
Claims-at-write item is the eventual structured answer — this is its
cheapest real precursor). `Store.mark_verified` stays policy-free.
AC: verify reports symbol existence for the citation shapes it can parse;
zero false alarms on the adversarial prose suite; advisory only (no
verdict input) until a bench measures its precision.

**B3. Relevance label recut. SHADOW-VALIDATED.**
(a) Add `matched_leg` (lexical / semantic / both) per hit — cheap,
additive, tells the caller WHY a hit surfaced. (b) Stop labelling
pure-semantic hits by lexical coverage: when the only leg is semantic,
label from cosine bands calibrated on the two existing gold sets
(bench/retrieval's committed set; the 124-real-query numbers preserved in
memory `01KYD9PXN3R27B421NHFGGXTJZ`). (c) Fix `DESC_MEMORY_SEARCH`:
describe what the label measures; drop the "treat low as noise"
absolutism; make `expand_top` gate on rank + score margin instead of the
label string. Validate any labelling change through the telemetry_v2
shadow lane against logged turns BEFORE flipping — the v2 candidate was
measured length-credulous and rejected; that discipline stands (roadmap
item 6 documents the successor-rule constraints).
AC (objective bar, measured on the telemetry_v2 shadow replay over the
logged turns): no length bucket at 0% high-rate AND max/min bucket
spread < 3× (v1 today: 45/32/0/3 — spread unbounded with a zero bucket);
LongMemEval re-run shows no R@5 regression; semantic-only hits no longer
suppressed by `expand_top`.

**B4. Conflict safety.**
(a) Auto-consolidate must never tombstone a pair showing negation or
numeric-divergence signals — those route to the conflicts queue ONLY,
regardless of Jaccard (make them structurally unreachable by
`_pick_keeper`). (b) Add order-sensitivity for near-identical pairs
(sentence-scoped negation or bigram containment) — test-gated, small.
(c) Document the detector's limits in `docs/api.md` (not a DESC — budget).
The adversarial pairs, verbatim (the audit conversation is not available
to you — these are the test bodies):
- Negation/order pair: "Deploy with the blue-green strategy; never do
  in-place." vs "Deploy with the in-place strategy; never do blue-green."
  (Jaccard 1.0 today, no polarity flip detected.)
- Also: "Always squash-merge; do not rebase." vs "Never squash-merge;
  always rebase." (Jaccard 0.75, both bodies negated → no flip today.)
- Numeric-divergence pair (construct): "The staging DB listens on port
  5432." vs "The staging DB listens on port 5433."
AC: all three pairs route to the queue in tests; the unattended pass
provably cannot tombstone any of them.

### Phase 4 — Retrieval spend (M; only measured wins)

Entry brief for this phase: `docs/audit/phase4-entry-2026-07-30.md` (verified at `bbc7672`).

**PHASE STATUS: DONE 2026-07-30.** C2-prereq shipped; **C2 CLOSED-negative and
reverted**; C3 shipped; C5 shipped as measurement. Deltas from the plan as
written, each verified against a committed artifact:

- **The C2 estimate was wrong about its own mechanism, and the prerequisite is
  what proved it.** The plan inherited `bench/longmemeval/README.md`'s diagnosis
  — two-event questions split their vocabulary across two sessions, so
  `score_memory`'s coverage multiplier cannot be satisfied by either — and
  budgeted a re-ranker against it. Per-question records refute it: of 87 dropped
  evidence sessions, 81 (broad reference) / 74 (strict) carry NO query term the
  surviving top 5 lacks, and like-for-like the dropped evidence matches *fewer*
  terms than the survivors that beat it (median 2 vs 3). In 337 of 500 questions
  the top 5 already carries every term anything matched.
- **The item was closed by a ceiling, not by a sweep**, which is the reusable
  lesson. 29 bonus configurations found nothing; the dev-selected one was run for
  real at **0.8935 → 0.8941, +0.06 against a +2.00 gate**. But a sweep can only
  ever say "we did not find one". `coverage_probe.py` bounds an omniscient rescue
  across four novelty references and prices each against `blind` promotion:
  loosening the test raises the ceiling (+0.33 → +0.79 → +2.59) only by
  converging on blind's +5.21, and the reference whose ceiling clears the gate has
  precision BELOW blind (0.94x lift). Best available signal: 1.22x on a 4.5% base
  rate, against a ~25–30% requirement. That is definition-independent and it is
  what makes this a closed item rather than an untuned one.
- **Ordering the prerequisite first was load-bearing and nearly was not enough.**
  The baseline had to exist in git before the re-ranker did; it does (`4457134`).
  What the plan did not anticipate is that the same records also had to be able
  to refute the plan's own premise.
- **The negative was adversarially audited before publication, and three
  published numbers were wrong.** Two auditors plus an adjudicator found: the
  dropped-session count was 82, not 87 (five never scored in any leg and were
  silently skipped); the headline zero-novelty fraction moves nine points between
  two defensible reference sets, so both now ship; and a matched-term comparison
  put a per-session median against a per-hit mean, which reversed its sign. All
  three are corrected in the artifact and the README. **The conclusion survived
  every attack and got stronger** — the precision table came out of the audit.
- **C3 shipped as specified**, scoped to `_build_hit`'s call site. One recon
  design error was caught by its implementer (the density window was off by the
  lead-context offset, and no test written for the feature caught it until a
  discriminating fixture was built for exactly that). One recon claim about a CJK
  fixture was wrong and is documented rather than quietly fixed.
- **C5 shipped, and found the `--pad-to` artifacts had been measuring the wrong
  thing.** Padding grows the corpus; it does not engage the prefilter, because
  `run_arm` calls `search.search` on a memory list. Every retrieval artifact
  dated before now measures dilution. `--prefilter` now picks the code path
  separately, drives production's own `resolve_search_pool`, and **refuses to run
  blind**: engagement is asserted per query against the corpus-IDF provider (the
  exact IFF), and the runner exits non-zero rather than print full-corpus numbers
  under a `prefilter: true` heading. Recall@5 loss is exactly zero in six of six
  cells — but the README states the narrow reason (nomination is not the
  bottleneck *at this recall level*) rather than "prefiltering is free".
- **Budgets: zero spent, as predicted.** No new wire params, no DESC edits. All
  four items are internal or bench-only.
- **Environment: iCloud corrupted `.venv` twice more** (fourth and fifth
  occurrences), both the `.pth`-present-but-unprocessed variant whose only symptom
  is the three CLI-on-PATH tests flipping pass→skip. The brief's rule — treat any
  unexpected skip count as a corrupted venv and rebuild, never debug — caught both.

**C2. Read-side diversification. BENCH-GATED, with a prerequisite.**
Prerequisite first: the longmemeval runner emits `by_type` aggregates
only — the +3.2 rescue table is not reproducible from `results/`. Add
per-question output to `bench/longmemeval/run.py` and commit a baseline
artifact BEFORE building the re-ranker, or the improvement can't be
verified. Then: implement co-evidence rescue between the RRF fuse and the
top-k trim (`search.py:2407→2409`) — that is the only point where the
full fused ranking exists; the handler is too late, and both benches call
`search.search` directly so a handler-side rescue would be invisible to
the bench that motivates it. Must be sane at k=200 (the bench's retrieval
depth), not just k=5. Re-run both arms.
AC: lexical-arm pooled R@5 ≥ +2 vs 89.3 with no per-class regression
> 1pt and runtime within ~1.2× lexical (the +3.2 estimate was measured
on the lexical arm only); semantic arm must not regress, but has no
measured headroom prediction — report what it does. Per-question
artifacts committed. Negative result → commit and close.

**C3. Query-biased snippets — Python-side only.**
FTS5 `snippet()` is a dead end: the index stores the preprocessed token
stream, so it returns stemmed soup, never prose. Design (retrieval fact
pack §1): `_TOKEN_RE.finditer` over the RAW body, normalize each token
(stem + kebab handling in both directions — matched terms are
stemmed/folded), pick the densest window against the hit's existing
`matched` list (already passed into `_build_hit` and ignored), cut with
word-boundary logic. Scope to `_build_hit`'s call site only —
`snippet_for` is shared by consolidate summaries, the audit miss-trace,
and dedup hits. Fallbacks: head-of-body when `matched` is empty
(paraphrase-only semantic hits and browse mode both produce that). Tests
pin total length ≤ 203 — a mid-body window's leading ellipsis must fit
the budget. Cost: per emitted hit only (≤50), never the candidate pool.
AC: a hit whose match is at char 4,000 shows the matching window;
fallback correct; snippet-shape and audit-payload tests updated
deliberately.

**C5. Above-500 regime measurement (bench only).**
Every published retrieval number is below the 500-memory FTS prefilter
threshold (cap 50 nominees). `--pad-to` does NOT measure it — the bench
calls `search.search` on a memory list, so the prefilter never engages. A
real arm needs a `Store` + built index through
`load_search_candidates`/`resolve_search_pool` (or the
`BETTERMEMORY_INDEX_THRESHOLD` env override). Measure prefilter recall
loss; commit the finding; code changes are a separate decision.
AC: committed artifact answering "what does the prefilter cost above
threshold"; both bench READMEs' declared gap closed or narrowed.

### Phase 5 — Settlement + hooks (M)

Entry brief for this phase: `docs/audit/phase5-entry-2026-07-31.md` (verified at `7a79b61`).

**PHASE STATUS: DONE 2026-07-31.** D1, D2, D3 all shipped. Suite 4,174 passed /
16 skipped default leg, 4,099 / 3 / 8 embeddings; ruff, mypy (209 files),
pyright (0 errors) clean. Executed by a 3-lane recon → adjudicator → 3-lane
implement → 3-lane adversarial verify → 3-lane repair → 3-lane re-verify chain.
Deltas from the plan as written, each reproduced rather than reasoned:

- **The plan's `D1 → D2` arrow is not a real dependency.** `use_token_expired`
  carries none of the fields D2's coverage predicate keys on (deliberately — the
  omission of `attribution` is what keeps it in-session). All three items are
  parallel; D1 was kept first only so the eval-roster edit landed before D2's
  reviewer read that file.
- **Risk that the plan did not name, and the fact pack contradicts: adding a kind
  to `_KNOWN_SIDE_EFFECT_KINDS` and forgetting `_IN_SESSION_SIDE_EFFECT_KINDS`
  has NO tripwire.** `ADMIN_RECORDED_EVENT_KINDS` is *derived* as
  `KNOWN − IN_SESSION`, so the parity assertion is a tautology. Reproduced: with
  only the `_KNOWN_` entry, `tests/test_doctor.py` + `tests/test_eval.py` ran
  329 passed / 0 failed while the event — and its whole session — dropped out of
  doctor's census. The hand-written membership guard is the only thing there.
- **D1 as specified would have regressed hookful stores, twice.** Eviction runs
  inside `state.advance_turn()`, *before* the dedup purge, so a retrieval the
  Stop hook had already settled read as a loss after an idle gap. The
  `extra_pending` fix closes that. Adversarial verification then found the same
  defect open on a third path the brief had not considered: `memory_record_use`
  writes its `use` event *after* `_advance_turn` returns, so the log scan
  structurally cannot see it — the append-only log held one retrieval as both
  settled and lost. Fixed by threading `override_ids` into the drain.
- **D2's shared predicate shipped mutation-silent.** Three of five branches of
  `is_hook_telemetry_event` — including the `turn_audited` / `stop_hook` check the
  plan calls out by name — could be deleted with all 4,159 tests green, because
  every fixture used one event shape. Closed with a truth-table unit test plus
  end-to-end variants. Separately, `telemetry_coverage` first shipped as an
  undocumented wire key: the test that exists to prevent exactly that compared the
  DESC against a *hardcoded* expected set, so it passed. It now derives from
  `HealthReport.to_dict()`.
- **D3's "always exits 0" contract was false as first written.** `print(block)`
  sat outside the guard; `PYTHONIOENCODING=ascii` exited 1 with a traceback on the
  em dash. Beyond moving it inside, the flush and a `/dev/null` descriptor salvage
  are both load-bearing — without the salvage a hung-up reader exits **120** from
  CPython's shutdown flush, *after* `run` returned `SystemExit(0)`. Guarded by a
  subprocess test, since the bug lives past the last line any in-process test runs.
- **Budgets: zero spent, as the brief predicted.** No new wire params, no lean-DESC
  edits. `memory_health` is outside the 18-tool lean surface, so its new field and
  DESC sentence are free. Lean DESC stays 27,048/27,500; footprint remainder
  9,699/10,000 with G1's 182-char reserve intact.
- **Environment: the `.venv` corruption recurred twice more** (sixth and seventh).
  Refined trigger: it is not only an extras SWAP — plain `uv run` re-syncs
  implicitly and does it too. Use `uv run --no-sync` for every invocation here.

**D1. Make use-token loss visible — and do NOT convert it to applied.**
The audit draft's "settle prior-session tokens as auto-applied at next
session start" is WRONG by the system's own semantics: applied events
feed cold-endorsement and block dead-weight — settling evidence-free
leftovers as applied converts dead-weight signal into fake endorsements
wholesale (settlement fact pack hazard 7). Correct design: mirror the
pending-write pattern — `_evict_expired_use_tokens` stashes evicted
tokens, a drain emits an explicit expiry event from
`handlers/_shared._advance_turn` (session.py stays recorder-free by
documented design). Event-kind decision (hazard 1, pick deliberately):
a NEW kind `use_token_expired` (literal string at the call site — the
AST kind-parity test requires it in exactly one roster; the rosters are
`_KNOWN_SIDE_EFFECT_KINDS` at `eval.py:2403` AND
`_IN_SESSION_SIDE_EFFECT_KINDS` at `eval.py:2420` — in eval.py, not
doctor.py; miss the in-session entry and doctor's census silently drops
the sessions) — noting the `kind=="use"` dedup scans won't see it, which
is acceptable because expired tokens are outside the hook's 600s
lookback by construction.
AC: hookless simulation shows zero silent losses; health/eval handle the
new kind; hookful behavior unchanged; kind-parity and admin-parity tests
green.

**D2. Telemetry-coverage honesty in curation.**
Coverage signal: `use` events with `attribution="hook"` /
`triggered_from="stop_hook"`, or `turn_audited` with
`triggered_from="stop_hook"` (bare `turn_audited` is NOT hook-proof — the
in-process tool emits `mcp_tool`). Neither is accumulated today. Thread a
coverage gate through ALL THREE dead-weight surfaces (the report bucket,
`curation_counts`, and `find_demotion_candidates` — tests pin numerical
agreement between them) so a hookless store annotates or refuses
dead-weight/demotion instead of reading "applied=0" as "useless". Split
endorsement reporting by attribution tier (model / hook / auto) in health
+ eval surfaces with the documented back-compat rules (absent attribution
+ auto≠True → model; `cli_*` rows remain genuine endorsements).
AC: hookless store → dead_weight empty with an explanatory field;
hookful unchanged; the three-surface agreement tests still pass;
endorsement_ratio no longer conflates a 60%-containment phrase match with
model deliberation.

**D3. SessionStart hook in the plugin.**
Verified against the official hooks docs: SessionStart stdout IS injected
as context; matchers are startup/resume/clear/compact/fork; default
timeout 600s. Design constraints from the fact pack (§ hazards 3-6, 10):
there is no scope-overview CLI today — add a purpose-built subcommand
(the help-listing test pins the roster) that prints ONLY the intended
context block (stdout is injected verbatim); do NOT reuse the MCP
handler blindly — it records a `scope_overview` event and walks the full
event log + `curation_counts` (compute_health-scale cost). The hook
variant should read cheaply (index/store counts; skip or cache the
curation rollup), record NOTHING (or admin-attributed only — a fresh
session id per boot manufactures phantom sessions in doctor's census and
can hijack the in-process session anchor), scope matchers explicitly
(`startup`, probably `resume`), set a small explicit timeout with
`|| true`, and gate on store-nonempty. Add a doctor check mirroring the
Stop-hook check. Update plugin/README (it is currently honest — keep it
that way).
AC: fresh session on a plugin install sees scope counts with zero tool
calls; hook failure can't block the session; no phantom sessions; doctor
validates the wiring; existing Stop-hook tests untouched (they read only
the Stop key).

### Phase 6 — Footprint (M; LAST among code phases — DESCs must settle.
Phase 7's G1/G2 DESC deltas land BEFORE E1's final ratchet + toolcost
re-run, or the just-ratcheted ceiling gets immediately re-recalibrated —
see the sequencing section)

**E5. Strip schema title bloat (do first — biggest clean win).**
Every param carries an auto-generated pydantic `"title"`; measured
saving: 2,047 chars of the 7,077-char lean inputSchema, plus outputSchema
titles. No SDK hook exists — the clean point is a post-registration scrub
at the bottom of `builder._register_tools`, iterating
`mcp._tool_manager._tools` and deleting `title` keys recursively
(private attr: feature-detect or pin the SDK floor; `mcp>=1.0.0` is the
current constraint). Keep `properties`/`required` intact (tests assert
param presence); do NOT disable structured output (wire-shape change) —
scrub the output schema's titles in place.
AC: served schemas shrink ~2k chars with byte-identical call behavior;
E4 baseline re-recorded.

**E1. Description cuts — scalpel, honest target.**
Verified reality: only ~1.8k chars of DESC prose is duplicated policy
(five concrete cut candidates with exact spans in the footprint fact pack
§6 — transient/credential/previously_removed bullets in
DESC_MEMORY_WRITE, the two Concurrency paragraphs in update/verify).
Everything else is field-reference prose that ~10 tests pin by exact
substring (the pin inventory is hazard 1 — read it before editing).
Execute the five cuts + runner-ups, making the gate reject hints carry
anything they don't already teach (hint growth is a per-reject cost —
keep it lean; several hint strings are themselves asserted). Ratchet
`_DESC_BUDGET_CEILING` down to a round number with `_DESC_BASELINE`
re-measured in the same commit (the documented two-rule recalibration).
Moving parity-pinned enumerations (scope_overview's curation keys,
health's bucket list) into docs is allowed ONLY with the lockstep test
rewrite in the same commit — decide per enumeration whether the resident
copy pays rent.
AC: lean total ≤ ~24k with zero policy loss (the "exactly once" phrase
pins still pass); toolcost bench re-run and re-published; A5/E4 green.

**E3. "core" tool-surface preset — design-gated.**
Additive only: a NEW config key (keep the `full_tool_surface` bool;
repurposing it triggers the config-deprecation lane and mass test churn);
shipped default stays lean-18 (pinned in tests + docs). Hard constraints
that shrink the win: the staging pair (write_confirm/cancel) must ride
with memory_write (user-inference staging deadlocks without it); the
instructions block names verify/update/scope_overview/record_use (phrase
pins); episode tools are recorded as load-bearing for the loop skills;
proposals must keep auto-surfacing under `[proposals] auto_propose`;
memory_audit_turn stays (plugin Stop hook). Realistic core is therefore
~12-13 tools, and the honest measured saving is only ~2k desc chars
(show 851 + remove 463 + list 454 + scope_disable 231 + scope_enable 55
= 2,054) plus ~1k of schemas — NOT a large win. The likely outcome is
CLOSE with rationale; ship only if a coherent flow-complete subset
saves materially more than that (deferred-loading harnesses like
ToolSearch already solve this class — the audit session loaded 7 of 27
tools on demand — so the preset serves non-deferring clients only).
AC: preset registers a working flow-complete subset, or the item is
closed with the measured rationale; default surface byte-identical.

**E2. Micro-tool merges — 4.0 material, do not do now.** Tool removal is
forbidden within 3.x by the compatibility contract. A merged replacement
may be ADDED in a minor with a `Deprecated` changelog entry + runtime
warnings; removal waits for 4.0. Record as a 4.0 line item; skip
otherwise.

### Phase 7 — Episodes as the primary continuity layer (M)

**G1. Takeaway-only episode reads.** Add `include_bodies` (default True —
compat) and an optional `ids` filter to `episode_search` (no new tool: a
fetch-by-id tool would trip the seven-surface tool-count atomicity and
the desc budget; `episode_promote` is currently the only by-id path and
it's write-side). Mirror the params in the `_handlers.py` facade
(`:490-503` — FastMCP builds the schema from the facade signature; a
handler-only param never reaches the wire). Update DESC (substring pins:
"most-recent" and the worktree-language checks must survive) + api.md +
isolation tests.
AC: a 10-episode scan costs ~1-2 KB instead of ~30 KB; body fetch by id
round-trips; pins green.

**G2. Bless episodes as the state channel.** The live store's most-used
memory is an audit-loop state blob — the no-state write policy pushed
state into the fact layer anyway. Codify what the project already
half-does: loop/working state belongs in episodes; facts get minted from
episodes at session close via `episode_promote` (the "cured write" path —
less transient junk, better dedup context). This is guidance (DESC nudge
+ docs + one worked example), NOT a schema change and NOT a migration of
the existing state memory (it is load-bearing for the audit loop; leave
it).
AC: documented convention; episode_promote DESC nudges close-of-session
minting; no behavior broken.

**G3. Episode-volume visibility.** Episode walks are O(total episodes)
and GC (`prune_old_sessions`, TTL 30d) fires only on `episode_write` and
the CLI — a read-only loop never GCs. Add an episode count/size line to
`memory_health` (or scope_overview if budget allows) so journal growth is
visible.
AC: episode volume visible in one rollup; no new walk on the hot path.

---

## 3. Sequencing

```
Phase 0 (A1 A2 A3ab A4 A6 A7 F8) ──► Phase 1 (A5 E4 H1)
        │
        ▼
Phase 2 (F1 F2 F5 F6 F7 F9 F10 parallel; F3 ──► F4; F1 ──► A3c postmortem)
        │
        ▼
Phase 3 (B1 ──► B2 re-bench; B3, B4, B5 parallel with B1)
        │
        ▼
Phase 4 (C2-prereq ──► C2; C3; C5 parallel)
        │
        ▼
Phase 5 (D1 ──► D2; D3 parallel)
        │
        ▼
Phase 7 (G1 G2 G3 parallel)   ◄── episodes phase runs BEFORE the ratchet
        │
        ▼
Phase 6 (E5 ──► E1 ──► E3?; E2 = 4.0 note only)   ◄── final ratchet last
```

Collision notes: DESC-touching items (A1, F3, F5, F7, B2b, B3c, G1, G2,
E1, E3) contend on the desc budget and its substring-pin tests —
serialize DESC edits within a phase, keep them within budget (growth
only where an item requires it), and re-measure `_DESC_BASELINE` in the
same commit per the rules. Phase 7 executes before Phase 6 so the final
ratchet + toolcost re-publish happen once, after every DESC delta has
landed. bench/rot re-runs (B1, B2) are long — run off the critical path;
never block a release on an in-flight bench (gate the ITEM, not the
release). Each phase is one or more MINOR releases; follow the runbook
memory and the version-bump worked example (memory
`01KX5XZM8TTYG7EVVG81YXKYZ8`). Nothing here is a major; E2 is explicitly
deferred to 4.0.

## 4. Measurement gates

| Item | Bench | Win condition | On miss |
|---|---|---|---|
| B1 | bench/rot, NEW anchored-relative arm, 30-repo corpus | path-leg J > 0 at precision ≥ 0.9 | commit negative result; keep absolute+attested only |
| B2 | bench/rot, new arms pooled | alerts/catch < 1.5 for the escalating tier | execute B2b demotion branch |
| B3 | telemetry_v2 shadow + longmemeval re-run | flatter length profile; no R@5 regression | keep v1 label; ship matched_leg only |
| C2 | bench/longmemeval (per-question output first) | lexical pooled R@5 ≥ +2, no class −1, ≤1.2× runtime; semantic no-regress | commit negative result; close |
| B4b | pinned adversarial-pair tests (three pairs in B4) | all route to queue; auto pass cannot tombstone | rework detector; pairs stay pinned |
| C5 | bench/retrieval, real above-threshold arm | measurement committed | n/a (measurement-only) |
| E1/E5 | bench/toolcost re-run | total ↓, artifact re-published | n/a (prose/schema work) |
| F9 | live-store shape measurement | breadcrumb precision acceptable | commit negative result; close |

## 5. Risk register

- **Frozen pre-registrations** (B1): regrading existing rot arms or
  scorecard entries retroactively is the one unforgivable move — new
  behavior, new arm, appended only.
- **Signature/wording pins**: `verdict_from_signals` arity, DESC
  substring pins, cost-shape pins, kind-parity AST tests — every one is
  movable, none is bypassable. Move the pin in the same commit with the
  reasoning, or don't move it.
- **Cross-host false-missing** (B1/B2): anchored relative checking on a
  machine without the recorded worktree must fail open (skip), or synced
  stores light up with fabricated drift.
- **Applied-event poisoning** (D1): never convert unsettled tokens into
  applied events. Expiry is a first-class outcome; visibility, not
  fabrication.
- **DESC budget churn**: before Phase 6, DESC edits must stay within
  budget — net-negative or neutral wherever possible, growth only where
  an item requires it (F3/F5/F7/G1/G2 do), with `_DESC_BASELINE`
  re-measured in the same commit. 1,164 chars of slack at HEAD.
- **Plan rot**: this document and the fact pack cite `95af021` line
  numbers. Re-verify anchors after big diffs; update item statuses inline
  as phases land; journal the delta.

## 6. Per-phase exit gates

Every phase: suite green both legs, CI matrix green, CHANGELOG entry in
the same window, `episode_write` with SHAs + CI run ids, and this
document's item statuses updated. Phases 3–4 additionally: bench
artifacts committed and their pinning tests re-recorded deliberately.
Phase 6 additionally: toolcost re-published and the eval-results
comparative row annotated with the re-measured date.
