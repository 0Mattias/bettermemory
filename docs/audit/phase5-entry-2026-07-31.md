# bettermemory Phase 5 entry brief — 2026-07-31

Companion to `docs/audit/upgrade-plan-2026-07-30.md` § "Phase 5 — Settlement +
hooks" and `docs/audit/upgrade-plan-facts/settlement.md`. Produced by three
read-only recon lanes (D1 / D2 / D3) plus an adjudicator that independently
spot-checked every anchor the lanes cited, because the fact pack's anchors were
verified at `95af021` and four phases of commits have moved the tree since.

Like the plan itself, this file lives under `docs/audit/` deliberately: that
directory is outside the doc-claims checker's corpus, so its line references
cannot fail CI as the code moves. Anchors below were verified at `7a79b61`
(v3.31.0) — treat them as anchors to re-verify, not gospel.

**Two findings here contradict the plan and the fact pack. Both are load-bearing:**

1. **D1's roster-2 omission has NO CI tripwire** (§6 Risk 1). The apparatus that
   looks like it guards this is tautological, because `ADMIN_RECORDED_EVENT_KINDS`
   is *derived* as `KNOWN − IN_SESSION`. Adding to `_KNOWN_SIDE_EFFECT_KINDS`
   alone passes every existing test and silently drops the event — and its whole
   session — from doctor's census. The guard test is mandatory, in the same
   commit as the roster edit.
2. **D1 as the plan specifies it would regress hookful stores** (§6 Risk 2).
   Eviction runs *before* the dedup purge, so a retrieval the Stop hook already
   settled, followed by an idle gap ≥ 1800 s, would be reported as a loss. No
   existing fixture emits `triggered_from="stop_hook"`, so a hookless test suite
   cannot see it. The `extra_pending` fix in §2 D1b is not optional.

A third correction is smaller but worth naming: the fact pack's hazard 6 claims
`test_help_lists_all_subcommands` pins the subcommand roster's order. It does
not — it is substring membership over a hand-picked 8-tuple. The in-source
comment at `cli/__init__.py:97-99` asserting otherwise is also wrong.

---

# PHASE 5 ADJUDICATED SPEC — `use_token_expired` / coverage honesty / SessionStart hook

Verified at HEAD `7a79b61` (v3.31.0). Working tree carries the unrelated 3.31.1 mcp-cap hotfix lane (`pyproject.toml`, `CHANGELOG.md`, `.github/workflows/ci.yml`, `server.json`, `plugin/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `uv.lock`, `docs/incidents/`). Only `plugin/.claude-plugin/plugin.json` sits near a Phase 5 blast radius (D3 edits `plugin/hooks/hooks.json`, a sibling file) — no conflict.

**Adjudication summary: D2 and D3 are accurate and adoptable nearly as written. D1 is the strongest analysis of the three and found a real, unlisted design defect — but its central *safety* claim (row 8) is FALSE, and correcting it changes D1 from "guarded by CI" to "unguarded; needs a new test."**

---

## 1. Anchor table — independently spot-checked at HEAD

### 1a. Anchors I personally re-read and CONFIRMED

| Anchor | HEAD | Lane claim | Verdict |
|---|---|---|---|
| `_PENDING_USE_TOKEN_TTL_SECONDS = 30*60` | `session.py:85` | D1 ✓ D3 ✓ | **confirmed** |
| `DEFAULT_USE_TOKEN_TTL_TURNS = 2` | `session.py:95` | D1 ✓ D3 ✓ | **confirmed** |
| `AUTO_COMMIT_MIN_AGE_SECONDS = 600.0` | `session.py:107` | D1 ✓ D3 ✓ | **confirmed** |
| `class PendingUseToken` | `session.py:176` | D1 said 175-195 | confirmed (decorator on 175) |
| `class SessionState` | `session.py:677` | D3 ✓ | **confirmed** |
| `pending_use_tokens` field | `session.py:701` | D1 ✓ | **confirmed** |
| `_expired_pending` / `_expired_pending_at` | `session.py:713` / `:714` | D1 ✓ | **confirmed** |
| `_evict_expired` (stash pattern) | `session.py:970`; stash `985-986`; GC `993-996` | D1 ✓ | **confirmed** |
| `pop_recently_expired` | `session.py:1012`; drain `1020-1021` | D1 ✓ | **confirmed** |
| `was_recently_expired` | `session.py:1027-1029` | D1 ✓ | **confirmed** |
| `advance_turn` | `session.py:1033`; **evict-tokens `1045`**, evict-pending `1046` | D1 ✓ | **confirmed — load-bearing for §2's defect** |
| `issue_use_tokens` | `session.py:1049` | D1 ✓ D3 ✓ | **confirmed** |
| `consume_old_tokens` | `session.py:1071`; late-resolve `1108-1109`; cutoffs `1110`/`1111`; predicate `1116` | D1 ✓ | **confirmed** |
| `purge_use_token` | `session.py:1122` | D1 ✓ | **confirmed** |
| **`_evict_expired_use_tokens`** | `session.py:1131-1139`, bare `del` at `1139` | D1 ✓ | **confirmed, quote byte-exact** |
| `reset()` clears tokens | `session.py:1151` | D1 ✓ | **confirmed** |
| `SessionRegistry.for_request` LRU evict | `session.py:1231`; `popitem(last=False)` `1255` | D1 said 1253-1256; D3 said 1231 | both ≈right |
| `_drain_pending_expired` | `_shared.py:259` | D1 ✓ D3 ✓ | **confirmed** |
| `_advance_turn` | `_shared.py:294`; body **332-346** | D1 ✓ D3 ✓ | **confirmed** |
| — `state.advance_turn()` / `_drain_pending_expired` | `_shared.py:332` / `333` | D1 ✓ | **confirmed** |
| — dedup gate / scan / purge | `_shared.py:334` / `335` / `337` | D1 ✓ | **confirmed** |
| — `consume_old_tokens` / auto-commit record | `_shared.py:338` / `339-346` | D1 ✓ | **confirmed** |
| `_already_recorded_pending_ids` | def `_shared.py:395`; guard `457`; map `459-461`; boundary `465`; `iter_events` `471`; early-exit `break` `487`; `kind != "use"` `488`; `ev_ts >= issued` `517` | D1 ✓ (all seven) | **confirmed exactly** |
| `_attach_use_tokens` | `_shared.py:528`; inject `545` | D1 ✓ D3 ✓ | **confirmed** |
| `_maybe_attach_curation_hint` / `curation_counts(` | `_shared.py:548` / `593` | D2 ✓ | **confirmed** |
| `_shared.__all__` | `623-641`; `"_drain_pending_expired"` at `632` | D1 ✓ | **confirmed** |
| `_KNOWN_SIDE_EFFECT_KINDS` | `eval.py:2403-2411` | D1 ✓ D3 ✓ plan ✓ | **confirmed** |
| "Verified at the call sites" comment | `eval.py:2413-2419` | D1 ✓ | **confirmed** |
| `_IN_SESSION_SIDE_EFFECT_KINDS` | `eval.py:2420-2422` | D1 ✓ plan ✓ | **confirmed** |
| `ADMIN_RECORDED_EVENT_KINDS` (derived) | `eval.py:2446-2448` | D1 ✓ | **confirmed** |
| `_is_dead_weight` | `health.py:142` | D2 ✓ | **confirmed** |
| `_is_dead_weight(` call sites | `health.py:1541`, `health.py:2720`, **`consolidate.py:1003`** | D2 ✓ (fact pack's `consolidate.py:843` is STALE) | **confirmed** |
| `find_demotion_candidates` | `consolidate.py:896`; walk `953`; grace `992`; whitelist `999` | D2 ✓ (fact pack's `793-830`/`839` STALE) | **confirmed** |
| `curation_counts` | `health.py:2489`; walk `2622`; `silent_miss_cutoff` `2631`; `miss_ack` `2638`; **`since` filter `2645-2654`**; `search` `2655`; `use/applied` `2667`, `is_auto` `2668`; `contradicted` `2674`; `search_miss` `2682`; `_is_dead_weight` `2720` | D2 ✓ (all ten) | **confirmed exactly** |
| `_handle_use` auto/explicit branch | `health.py:1185`; `if ev.get("auto") is True:` **1214**, `else:` **1216-1217** | D2 ✓ | **confirmed** |
| `_handle_turn_audited` | `health.py:1269` | D2 ✓ | **confirmed** |
| `_StatsAccumulator` / `handle_event` / dispatch / `rollups` / `_HANDLERS` | `health.py:1033` / `1130` / `1151` / `1359` / `1378` | D2 ✓ D3 ✓ | **confirmed** |
| `compute_health` / `report_for_directory` | `health.py:1392` / `2875` | D2 ✓ | **confirmed** |
| `class HealthReport` / `dead_weight` field | `health.py:801` / `810` | D2 ✓ | **confirmed** |
| `endorsement_ratio` / `_is_weakly_endorsed` / `_ENDORSEMENT_GRACE_DAYS` | `health.py:283` / `458` / `92` | D2 ✓ D3 ✓ | **confirmed** |
| `attribution` never read in health.py **or** consolidate.py | `grep -c` → **0 / 0** | D2 ✓ | **confirmed** |
| `curation_counts` callers | `_shared.py:593`, `scope_overview.py:175`, `scope_overview.py:246` | D2 ✓ | **confirmed** |
| `triggered_from="stop_hook"` in auto-consolidate | `consolidate.py:1632`, `1659` | D2's N5 ✓ (fact pack missed it) | **confirmed** |
| `_latest_in_process_session` walk | `hook.py:717-728`; **`triggered_from == "stop_hook"` skip at `719`** | D3 ✓, hazard 4 ✓ | **confirmed** |
| `hook._pending_retrievals` `kind == "use"` | `hook.py:898`; ids read `902` | D1 ✓ D2 ✓ | **confirmed** |
| mint sites | `search.py:1112` (`_attach_use_tokens`), `show.py:126` (`issue_use_tokens`) | D1 ✓ (fact pack's `show.py:120`, `search.py:1041` STALE) | **confirmed** |
| `doctor._check_audit_turn_cadence` | `doctor.py:920`; `is_admin_recorded_event` `965`; `total_events += 1` `984`; `session` `985` | D1 ✓ D3 ✓ | **confirmed** |
| doctor registration | **`doctor.py:2936-2941`** | D3 ✓ (fact pack's `2928-2933` STALE) | **confirmed** |
| `plugin/hooks/hooks.json` 16 lines, Stop-only, no matcher, `timeout: 30` | file read | D3 ✓ | **confirmed** |
| `test_plugin_ships_stop_hook` / `test_stop_hook_calls_audit_turn` / `test_stop_hook_has_reasonable_timeout` | `test_plugin.py:281` / `293` / `320` — **all three index `body["hooks"]["Stop"]` explicitly** | D3 ✓ | **confirmed; adding `SessionStart` breaks nothing** |
| `test_help_lists_all_subcommands` | `test_cli_smoke.py:56`; hand-picked 8-tuple loop at `68-77`, substring membership only | **D3's correction ✓** | **confirmed — fact-pack hazard 6 AND the in-source comment at `cli/__init__.py:97-99` are both WRONG** |
| `test_subparser_registry_matches_main_dispatch` | `test_cli_smoke.py:1155` | D3 ✓ | **confirmed** |
| `_all_registered_subcommands` / `test_subcommand_help_works` | `test_cli_smoke.py:609` / `620` | D3 ✓ | **confirmed** |
| CLI registry / order comment / `audit-turn` / `consolidate` | `cli/__init__.py:100-118` (17 entries) / `97-99` / `111` / `112` | D3 ✓ | **confirmed** |
| `corpus_document_frequencies` + columnar scan | `index.py:878`; `for row in conn.execute(` `950`; SELECT `951`; `scopes_json` parse `965` | D3 ✓ | **confirmed** |
| index schema columns | `index.py:198` (`scopes_json`), `206` (`origin_repo`), `207` (`origin_worktree`) | D3 ✓ | **confirmed** |
| `index.status` | `index.py:1250` | D3 ✓ | **confirmed** |
| index-trust gate to mirror | `_handlers.py:217` (`status=`), `224` (exists/corrupt/needs_rebuild), `226-228` (threshold arm — **do not copy**) | D3 ✓ | **confirmed** |
| `store.count_active_memory_files` | `store.py:2132` | D3 ✓ | **confirmed** |
| `search.candidate_admitted` | `search.py:1916` | D3 ✓ | **confirmed** |
| `config.resolved_directory` / `load_config` | `config.py:573` / `984` | D3 ✓ | **confirmed** |
| hazard-12 monkeypatches | **`test_server_v12_features.py:931` and `:1165`** | **D1's correction ✓** — fact pack's `868`/`1102` are WRONG | **confirmed** |
| `AUTO_COMMIT_MIN_AGE_SECONDS == ATTRIBUTION_LOOKBACK_SECONDS` pin | `test_telemetry_v2.py:626` | D1 ✓ | **confirmed** |
| `_evict_expired_use_tokens` test coverage | `grep -rn` over `tests/` → **0 hits**; `_PENDING_USE_TOKEN_TTL_SECONDS` → **0 hits** | D1 ✓ | **confirmed** |
| no `hookless` marker/test anywhere | `grep -rln hookless tests/` → **0 files** | D1 ✓ | **confirmed** |
| `docs/eval.md` side-effect enumeration omits `doctor_fix` | `docs/eval.md:182-185` | D1 ✓ | **confirmed stale** |
| `test_doc_claims` glob is `docs/*.md` (top level only) | `test_doc_claims.py:602` | D1 ✓ | **confirmed — `docs/audit/**` is uncovered, which is why the fact pack rotted invisibly** |
| `_admin_roster_forks` predicate | `test_eval.py:3000`; needs `∩ADMIN` non-empty **and** `∩IN_SESSION` empty | D1's row-9 trap ✓ | **confirmed** |
| DESC ceiling + lean fixture | `test_server.py:5932` (`_DESC_BUDGET_CEILING = 27_500`); `_lean_descriptions` built with `full_tool_surface=False` at `5920-5926`; `_DESC_BASELINE` `5973-5992`, 18 rows | D2 ✓ | **confirmed; summed the table myself = 27,048** |
| `memory_health` behind `full_tool_surface` | `builder.py:347` gate; registration `354-356` | D2 ✓ (said 355-357) | **confirmed** |
| `memory_scope_overview` in lean surface | `builder.py:254-256`; baseline 2820 | D2 ✓ | **confirmed** |
| scope_overview DESC brace block | `handlers/scope_overview.py:45-47` | D2 ✓ | **confirmed** |
| its parity test | `test_server_v12_features.py:1271` | D2 ✓ | **confirmed** |
| `curation_pending` 9-key wire pin | `test_server_v12_features.py:1216` (set-equality at `1219-1229`) | D2 ✓ | **confirmed** |
| three-surface parity pins | `test_health.py:240`, `:254`, `:325` (imports `find_demotion_candidates` at `335`) | D2 ✓ | **confirmed** |

### 1b. CORRECTIONS — lane claims that are WRONG at HEAD

| # | Lane | Claim | Reality at HEAD | Impact |
|---|---|---|---|---|
| **C1** | **D1 row 8** | "`tests/test_doctor.py:1105-1109` … **is the test that fails if you add to `_KNOWN_` but forget `_IN_SESSION_`**" | **FALSE.** `ADMIN_RECORDED_EVENT_KINDS` is *derived* (`eval.py:2446-2448`) as `KNOWN − IN_SESSION`. So `ADMIN ∪ IN_SESSION == KNOWN` is a **tautology** whenever `IN_SESSION ⊆ KNOWN`. Adding only to `_KNOWN_` grows ADMIN and the union still equals KNOWN → **passes**. | **CRITICAL — see §6 Risk 1.** D1's entire safety story for the second roster rests on this. |
| **C2** | D1 rows 7 + 8 (corollary) | implies `test_eval.py:2283` covers the other half | **FALSE.** `test_admin_recorded_kinds_derive_from_the_side_effect_roster` (`test_eval.py:2283-2297`) asserts `_IN_SESSION − _KNOWN == ∅` (the *opposite* direction), `not (ADMIN & IN_SESSION)` (tautology), and `"doctor_fix"/"silent_miss_cutoff" in ADMIN`. All three **pass** on a KNOWN-only edit. | same |
| **C3** | D1 (implicit) | the behavioural doctor test would catch it | **FALSE.** `test_audit_turn_cadence_excludes_every_admin_recorded_kind` (`test_doctor.py:1112-1139`) iterates `ADMIN_RECORDED_EVENT_KINDS` and asserts doctor **excludes** each. With `use_token_expired` mis-landed in ADMIN, it asserts the *broken* behaviour and **passes**. | same |
| C4 | D1 row 9 | "`tests/test_doctor.py:1097-1103` forbids `_ADMIN_EVENT_KINDS`" (D3 repeats) | `test_doctor.py:1096-1101` | cosmetic |
| C5 | D1 §4d | `_USE_OUTCOMES` at `_shared.py:47-59` | `_shared.py:49` | cosmetic |
| C6 | D1 §5 | id-reader inventory (7 readers) | **misses three**: `search.py:200`, `search.py:253`, `_response.py:1127`. **All three ARE kind-guarded** (`search.py:187`, `search.py:244`, `_response.py:1099`) — D1's *conclusion* ("`ids` is safe; no kind-agnostic reader exists") **holds across all 14 readers**, which I verified individually. | conclusion stands |
| C7 | D2 §3a/§5 | `test_handlers_table_matches_handle_methods` at `test_health.py:2624` | **`test_health.py:2626`** | fix before citing |
| C8 | D2 §4/N3 | `test_no_src_module_outside_eval_wires_up_a_single_axis` at `test_eval.py:3159` | **`test_eval.py:3160`** | fix before citing |
| C9 | D2 §5 | `test_server_v12_features.py:1235` (zero-dict pin) | **`:1234`** | cosmetic |
| C10 | D2 §5 | `to_dict()` `"dead_weight"` at `health.py:911`, `"recent_silent_misses"` at `:920` | def at **904**; `"dead_weight"` at **909**; `"recent_silent_misses"` at **927** | fix before citing |
| C11 | D2 §0 | `_AccumulatorRollups` at `health.py:938-984` | class at **939** | cosmetic |
| C12 | D2 §0 | `_shared.py:305 attribution="auto"` → 345 | **`_shared.py:345`** ✓ D2 already corrected this; noting it confirmed | — |

### 1c. Fact pack / plan / in-tree staleness to fold back

1. **Every `session.py` anchor in `docs/audit/upgrade-plan-facts/settlement.md` §1 and §11 is off by ~+730 lines**, and `docs/audit/upgrade-plan-facts/writepath.md:39` carries the same rot. Uncaught because `tests/test_doc_claims.py:602` globs `docs/*.md` only.
2. **Hazard 12's test anchors are wrong** — `test_server_v12_features.py:931` / `:1165`, not `868`/`1102`.
3. **Hazard 6 is wrong about `test_help_lists_all_subcommands`** — it pins neither order nor "all"; it's an 8-name substring subset. `src/bettermemory/cli/__init__.py:97-99` states the same falsehood in-tree (`tests/test_cli_smoke.py:1135` even says so). Fix the source comment in D3's commit.
4. **Hazard 10's cost model is wrong** — D3 measured `store.load_all()` at ~148 ms = 74 % of the 201 ms handler cost; the event walk is 30 ms and the dogfood store has **zero** `.jsonl.gz` archives. The fix "skip the curation rollup" buys ~35 ms; the real win is skipping `load_all`. I did not re-run D3's benchmark, but the *structural* claim it rests on — `curation_counts` takes `list[Memory]` (`health.py:2489`), so you cannot keep the rollup and skip `load_all` — is confirmed. Treat the millisecond figures as D3's measurement, the structural conclusion as verified.
5. **`docs/eval.md:182-185` omits `doctor_fix`** — pre-existing, fix in D1's commit.
6. Fact-pack §3/§6 `consolidate.py` and `health.py` anchors: consolidate's are ~+60/+160 stale; health's top-of-file are exact and its late ones are ~−130.
7. Fact-pack §4 says `plugin/.claude-plugin/plugin.json` is `3.30.0`; it is **3.31.1** in the working tree.
8. Two stale *comments* in-tree that will mislead the implementer: `eval.py:616` cites "health.py:736" (real: `health.py:1214`); `tests/test_health.py:2589,2591` cite "health.py:833 / :651" (real: `1378` / `1130`). Cheap to fix in D2's commit.

---

## 2. Per-item implementation plan

### D1 — `use_token_expired`

**Files:** `session.py`, `handlers/_shared.py`, `eval.py`, `docs/eval.md`, `CHANGELOG.md`, tests.

**D1a. `session.py` — stash instead of `del`.**
- Add ONE field beside `_expired_pending` (`session.py:713-714`):
  `_expired_use_tokens: dict[str, PendingUseToken] = field(default_factory=dict)`.
  **Adopt D1's single-map simplification.** Verified: `_expired_pending_at` exists solely to back `was_recently_expired` (`session.py:1027-1029`) for `memory_write_confirm`. `use_token` has no return path — `grep -rn use_token src/` shows it is emitted only at `_shared.py:545` and `show.py:158` and described in prose at `prompts.py:70` / `search.py:149`; `memory_record_use` takes `memory_ids`. No second map, no second-TTL GC.
- Rewrite `_evict_expired_use_tokens` (`session.py:1131-1139`) to `self._expired_use_tokens[mid] = self.pending_use_tokens.pop(mid)`, mirroring `_evict_expired`'s stash at `session.py:985-986`. Add the docstring the current function lacks.
- Add `pop_expired_use_tokens() -> list[PendingUseToken]` mirroring `pop_recently_expired` (`session.py:1012-1025`): drain + clear + idempotent.
- Add `self._expired_use_tokens.clear()` to `reset()` beside `session.py:1152-1153`.
- **Do not import `Recorder`.** `grep -n recorder src/bettermemory/session.py` → 0 hits; the no-recorder rule is documented at `session.py:99-100`.

**D1b. `handlers/_shared.py` — drain, emit, and FIX THE FALSE-EXPIRY DEFECT.**

This is the adjudication's most consequential adoption. **D1's §4c defect is REAL and I confirmed it structurally**: `_shared.py:332` calls `state.advance_turn()`, which at `session.py:1045` evicts expired tokens *before* the dedup purge at `_shared.py:334-337` can see them. A token the Stop hook settled at t=5 and that then sits through an idle gap ≥ 1800 s is evicted at line 332 and is invisible to line 335. A naive drain reports it as a loss. On a hookful store that inverts the item's purpose, and the fact pack does not mention it.

- Widen `_already_recorded_pending_ids` (`_shared.py:395-400`) with `extra_pending: dict[str, float] | None = None`; update the guard at `457` to `if not state.pending_use_tokens and not extra_pending:` and seed `pending_issued_at` (`459-461`) then `.update(extra_pending or {})` so `oldest_pending_issued_at` (`465`) covers the expired batch.
- Replace `_shared.py:332-346` with D1's shape: drain → fold expired into the log scan → purge → emit only the *unsettled* remainder → unchanged auto-commit. Keep `_drain_pending_expired` at 333 where it is.
- Add `_drain_expired_use_tokens` / `_emit_expired_use_tokens` and insert into `__all__` at **`_shared.py:632`** (alphabetically before `"_drain_pending_expired"`).
- **Do NOT add it to `_handlers.py`'s re-export block** (`_handlers.py:800-805`, `__all__` `836`). Its own comment scopes those to legacy out-of-tree callers; a new symbol has none. Nothing pins `_shared.__all__ ⊆ _handlers.__all__`.

**Event shape — adopt D1's batched form** (`ids`, `age_seconds`, `turns_since_issue`, `reason`), with its reasoning confirmed:
- **Batched, not per-token.** Unlike `pending_expired`'s one-per-drop, this population is the auto-commit's population; 20 events per search would inflate `total_events` in both `doctor.py:984` and `health.py:1134`.
- `ids` is safe: I verified **all 14** `.get("ids")` readers in `src/` sit inside a `kind` guard (C6).
- **No `outcome`, no `auto`, no `attribution`.** `attribution` is read by `is_admin_recorded_event` (`eval.py:2477-2507`); omitting it keeps the event in-session.
- No redaction collision (`events.py` `_REDACTED_TEXT_FIELDS` = `{query, probe_query}`).
- **Reject the `outcome="expired"` alternative**, per the plan and D1: `health._handle_use` and `curation_counts` branch on `outcome` *inside* `use`, and `_USE_OUTCOMES` (`_shared.py:49`) is `memory_record_use`'s validation set.

**D1c. `eval.py` rosters — BOTH, deliberately.**
- `eval.py:2403-2411` → add `"use_token_expired"` to `_KNOWN_SIDE_EFFECT_KINDS`.
- `eval.py:2420-2422` → add it to `_IN_SESSION_SIDE_EFFECT_KINDS`. **There is no CI tripwire for omitting this (C1/C2/C3).** Treat it as a hand-verified step and back it with the new test in §3.
- `eval.py:2413-2419` → extend the "Verified at the call sites" comment.
- `eval.py:2395-2402` → add the one-sentence rationale so the roster has no unexplained member.
- `docs/eval.md:182-185` → add `use_token_expired` **and** the already-missing `doctor_fix`.

### D2 — telemetry-coverage honesty

**Adopt D2's spec substantially as written.** Its walk anchors are the most accurate of the three (ten consecutive `curation_counts` line numbers exact).

**Predicate** (one boolean/int, computed on each surface's own existing walk):
```
kind == "use"          and (attribution == "hook" or triggered_from == "stop_hook")
kind == "turn_audited" and  triggered_from == "stop_hook"
```
Deliberately **excluding** `auto_consolidate` (`consolidate.py:1632`, `1659`) — D2's N5 find, confirmed. It is opt-in-gated, so including it would make the signal config-dependent.

**Surface A — `compute_health`.** New int field on `_StatsAccumulator` (after `health.py:1126`); bump inside the existing `_handle_use` (before the id loop at `1188`) and `_handle_turn_audited` (before the `repeat` early-out). Thread through `_AccumulatorRollups` (`health.py:939`) + `rollups()` (`1359-1373`) + the local re-bind block (`1501-1513`). Gate the bucket build at `1538-1556`.
⚠ **N1 tripwire confirmed**: `test_health.py:`**`2626`** requires 1:1 between `_handle_*` methods and `_HANDLERS` keys. Name any helper `_note_hook_telemetry`, never `_handle_*`.

**Surface B — `curation_counts`.** Bump **before** the `since` filter at `health.py:2645-2654`, using the same global-marker exemption `silent_miss_cutoff` (`2631`) and `miss_ack` (`2638`) already take, rationale written at `2624-2630`. D2's N2 is right: placing it after the filter makes the delta arm (`scope_overview.py:246`) manufacture a false "hookless". Gate the `dead += 1` at `2736`.

**Surface C — `find_demotion_candidates`.** Bump on its own walk (`consolidate.py:953-990`, `event.get("kind")` per-branch style); **refuse the pass** (`return []`) before the memory loop at `994`. This is the only *mutating* surface — reached unattended via `consolidate.py:1298` ← `1641`. Surface the refusal on `ConsolidateReport` (`consolidate.py:1306-1314`), D2's N7 — a silent `return []` on the Stop-hook path is correct but invisible.

**Default value — adopt D2's `None = "caller did not measure; assume covered"`**, with production entry points passing the derived value explicitly. Verified rationale: no fixture in `tests/test_health.py` or `tests/test_consolidate.py` emits `triggered_from="stop_hook"`, so a derive-by-default gate zeroes `dead_weight` across ~15 existing assertions. **But this default is itself the item's chief inert-ship risk** — see §6 Risk 3 and the §3 mitigation.

**Endorsement tier split.** Additive only; `explicit_applied_count` and `applied_explicit` keep exact current meaning (`_is_weakly_endorsed` `health.py:458` and `endorsement_ratio` `health.py:283` both key on them). Add `model_/hook_applied_count` at `health.py:1214-1217` and `applied_model/applied_hook` at `eval.py:659-665`. **Do not touch `endorsement_rate` (`eval.py:816`) or `memory_helped_rate` (`eval.py:813-815`)** — published rates with recorded baselines.
🚨 **N3 confirmed and re-anchored**: `test_eval.py:`**`3160`** AST-scans every `src/bettermemory/**/*.py` except `eval.py` and fails on any string constant equal to `ADMIN_RECORDED_ATTRIBUTION_PREFIX` (`"cli_"`) or any reference to either axis constant. So health.py/consolidate.py **must not** write `attribution.startswith("cli_")`. The only compliant derivation is D2's:
```
auto is True → "auto";  attribution == "hook" → "hook";  else → "model"   (None, "model", "cli_*")
```

### D3 — SessionStart hook

**Adopt D3's spec as written.** All of its structural claims verified; its two corrections to the fact pack (hazard 6's help-test claim; hazard 10's cost attribution) are sound.

1. `index.py` — new `scope_counts(root, *, admit)` beside `corpus_document_frequencies` (`878`), copying the columnar loop at `950-969` and the never-raises/return-`None` contract.
2. `cli/session_start_cmd.py` — new module, `audit_turn_cmd.py` shape. **`_cmd` suffix is mandatory**: `handlers/scope_overview.py` exists and `cli/__init__.py:53-62` documents the `audit_turn.py` → `audit_turn_cmd.py` rename forced by exactly this collision.
3. `cli/__init__.py` — import (`32-51`), registry entry between `"audit-turn"` (`111`) and `"consolidate"` (`112`), dispatch arm (`140-190`). **`test_cli_smoke.py:1155` requires the dispatch arm** — a registry entry without one fails CI. Fix the false "Order pinned by…" comment at `97-99`.
4. `plugin/hooks/hooks.json` — add the `SessionStart` sibling key. Keep `timeout ≤ 60`.
5. `doctor.py` — `_check_session_start_hook_wired`, registered at `2942` (after the `audit_turn_cadence` append at `2936-2941`, before `auto_memory_stranded`). **Config-shaped, not telemetry-shaped** — since the hook records nothing there is no footprint to count.
6. `plugin/README.md` — a 4th bullet, avoiding `"N tools"` phrasing (`test_eval.py:1669` scans it).

**The hard mandate (hazards 3+4).** Verified: `_latest_in_process_session` (`hook.py:717-728`) skips **only** `triggered_from == "stop_hook"` (line `719`) and never reads `attribution`. So hazard 3's suggested `attribution="cli_*"` workaround fixes doctor's census (`eval.py:2504-2507` → `doctor.py:965`) but **does not** fix anchor hijack. **The subcommand must construct no `Recorder` and call no `.record()`.** Concretely, do not port `scope_overview.py:307-319` (the `.record("scope_overview", …)`) or `scope_overview.py:100-101` (`for_request` + `_advance_turn`).

**Cheap-read design.** `load_config().resolved_directory()` (`config.py:984`, `573`) — **not** `cli_context()`, because `Store.__post_init__` (`store.py:196-237`) mkdirs, chmods, and can trigger a full index rebuild inside a session-start hook. Gate on `count_active_memory_files == 0` (`store.py:2132`). Index-trust gate mirroring `_handlers.py:217/224` but **not** the `indexed_count < resolve_index_threshold()` arm at `226-228` (that is a search-performance threshold, not a correctness one). Admission via `search.candidate_admitted` (`search.py:1916`) so the hook's counts provably agree with `scope_overview.py:129-134`. Stdout = the context block only; every diagnostic to stderr; always exit 0.

---

## 3. Test plan — with the negative controls that prove non-inertness

### D1

| # | Test | Where | Proves |
|---|---|---|---|
| **1** | **`test_use_token_expired_is_classified_in_session`** — assert `"use_token_expired" in _IN_SESSION_SIDE_EFFECT_KINDS` **and** `not in ADMIN_RECORDED_EVENT_KINDS`; plus a behavioural half: write `search` + `use_token_expired` under the **same** session into a tmp store and assert `_check_audit_turn_cadence` reports `total_events == 2`, `sessions == 1`. | `tests/test_doctor.py` (beside `:1112`) | **THE MISSING TRIPWIRE (C1/C2/C3).** No existing test catches the roster-2 omission. The behavioural half is kind-agnostic so it does not fork the roster (`_admin_roster_forks`, `test_eval.py:3000`, only flags literal sets that intersect ADMIN and miss IN_SESSION — a single-kind literal is spared once the roster edit lands). |
| **2** | **`test_hook_settled_token_expiring_does_not_report_a_loss`** — search → write a `use` under a `stop_hook`-shaped transcript session (mirror `test_server_v12_features.py:1113-1125`) → backdate the token past `_PENDING_USE_TOKEN_TTL_SECONDS` → one more `memory_*` call → assert **no** `use_token_expired` cites the id, and exactly one `use` total. | `tests/test_server_v12_features.py` | **The §4c regression guard.** Fails without the `extra_pending` fix. Highest-value test in the item. |
| 3 | `test_expired_use_token_emits_event_and_is_not_applied` — hookless e2e; assert one `use_token_expired` **and zero `use` events citing the id**. | same | hazard 7: expiry must never read as `applied` |
| 4 | `test_hookless_session_loses_no_retrieval_silently` — the AC, as a closure: `{ids in search/show} == {ids in use} ∪ {ids in use_token_expired}`, the two disjoint. | same | turns the slogan into an invariant |
| 5 | `test_evict_expired_use_tokens_stashes_for_drain` + idempotent second drain | `tests/test_session_tokens.py` | closes the **zero** existing coverage of `_evict_expired_use_tokens` |
| 6 | `test_drain_clears_stash_when_recorder_disabled` (`Recorder(enabled=False)`) | same | unbounded-growth guard; preserves `_drain_pending_expired`'s pop-before-record ordering (`_shared.py:271-273`) |
| 7 | extend `test_side_effect_event_kinds_are_not_counted_as_tool_calls` (`test_eval.py:1521-1536`) with `_ev("use_token_expired", ids=["m1"])` | `tests/test_eval.py` | `total_tool_calls == 0`, `unmapped_event_kinds == {}` |

Derived-and-automatic (no edit): the AST kind-parity test (`test_eval.py:1725`, asserts at `1786`/`1793`/`1800`) fails until the `_KNOWN_` entry lands and fails again if you add a roster entry with no literal call site.
**Backdate, don't monkeypatch** — mirror `test_server.py:365-367`; hazard 12's call-time-resolution contract (`session.py:1108-1109`) is what `test_server_v12_features.py:931,1165` depend on.

### D2

| # | Test | Proves |
|---|---|---|
| **1** | **`test_hookless_store_suppresses_dead_weight_through_report_for_directory`** — drive the **production entry point** (`health.py:2875`), not `compute_health` directly. | **The anti-inert control.** The `None = assume covered` default means a pure-function test proves nothing about shipped behaviour; only an entry-point test catches "the gate exists but nothing passes the value." |
| **2** | **`test_hookful_store_still_reports_dead_weight`** (same fixture + one `use` with `attribution="hook", triggered_from="stop_hook"`) | the positive control — proves the gate is not simply always-on |
| **3** | **`test_hookless_store_refuses_the_unattended_demotion_pass`** — through `consolidate(apply=True)` / `run_auto_consolidate` (`consolidate.py:1298` ← `1641`), asserting **zero retags on disk** | the only mutating surface; a `return []` that isn't reached is invisible |
| 4 | delta-arm test: two `curation_counts` calls (absolute + `since=boundary`) on a store whose hook events all predate the boundary → both read "covered" | N2 |
| 5 | tier-split conservation: `model + hook == explicit` (health) and `applied_model + applied_hook == applied_explicit` (eval); assert `endorsement_rate` and `memory_helped_rate` **byte-identical** before/after on a fixture with hook rows | hazard 9 / no published-rate drift |
| 6 | legacy back-compat: events with `auto=1` and `auto="true"` still land **explicit**; `attribution=None` lands **model**; `attribution="cli_acknowledge_debt"` lands **model** (not a 4th tier) | hazard 9, verified strict-`is True` in all four readers |
| — | unchanged-and-must-stay-green: `test_health.py:254`, `:325`, `:240`; `test_server_v12_features.py:1216`, `:1234`; `test_health.py:2626`; `test_eval.py:3160` | three-surface agreement + the two naming/AST tripwires |

### D3

| # | Test | Proves |
|---|---|---|
| **1** | **`test_session_start_records_nothing`** — snapshot the event log, run the subcommand on a populated store, assert the log gains **zero rows** (byte-identical), and assert no `Recorder` is constructed. | **The hazards 3+4 regression guard, and D3's only defence against silent re-introduction.** Without it the mandate is a comment. |
| 2 | empty store → **empty stdout**, exit 0 | the store-nonempty gate |
| 3 | populated store → stdout is **exactly** the context block, nothing else; stderr may carry diagnostics | hazard 5 (stdout is injected verbatim) |
| 4 | index-unusable / `indexed_count != disk_count` → **empty stdout**, exit 0, and **no `load_all`** (assert via monkeypatched sentinel) | silent degrade without the 150 ms bill |
| 5 | `scope_counts` agreement: index-derived total + scope map `==` `load_all`-derived, on a fixture with mixed origins | correctness of the cheap path |
| 6 | `plugin/hooks/hooks.json`: `SessionStart` present, matcher non-empty, `0 < timeout <= 60`, command contains `bettermemory session-start` and `|| true`; **and the three existing Stop tests untouched** | AC "existing Stop-hook tests untouched" |
| 7 | `tests/test_direct_imports.py` — one `test_cli_session_start_cmd_direct_import` (helper `_registered_parser` at `:404-419`) | house convention |
| 8 | `tests/test_doctor.py` — the new check: wired → ok; unwired + empty store → ok; unwired + non-empty → warn + fix_hint; unreadable foreign JSON → ok-with-note | never fail on a foreign config |

Automatic: `test_subparser_registry_matches_main_dispatch` (`test_cli_smoke.py:1155`) and the parametrized `--help` smoke (`:609-632`).

---

## 4. BUDGET VERDICT — **PASSES, at zero spend, with one binding constraint**

I re-derived both budgets from source rather than trusting the lanes.

**DESC.** Summed `_DESC_BASELINE` (`tests/test_server.py:5973-5992`, 18 rows) = **27,048**. `_DESC_BUDGET_CEILING = 27_500` (`:5932`). Slack = **452**. `_DESC_BUDGET_PRESSURE` = 27,400. The fixture measures `full_tool_surface=False` only (`:5920-5926`).

**Footprint remainder.** `_FOOTPRINT_BASELINE` (`tests/test_resident_footprint.py:124-131`): `input_schemas=7_170 + output_schemas=1_770 + skill_frontmatter=759` = **9,699** (definition at `:101-103`). `_REMAINDER_CEILING = 10_000` (`:167`), `_SCHEDULED_PARAM_RESERVE = 300` (`:169`), of which 93 is already spent on `acknowledge_user_claim`; the reserve comment enumerates the remaining scheduled costs as `episode_search include_bodies 76` + `episode_search ids 106` = **182 for Phase 7's G1**. Free slack for Phase 5 = 301 − 182 = **119 chars**.

| Item | DESC spend | Wire parameter | Input-schema | Output-schema | Verdict |
|---|---|---|---|---|---|
| **D1** | **0** | **none** | 0 | 0 | **FITS.** Event-log-only. D1 correctly rules DESC edits out of scope (`search.py:149` / `prompts.py:70` stay). |
| **D2** | **0** *(constrained)* | **none** | 0 | **0** | **FITS.** `memory_health` is registered only under `builder.py:347`'s `full_tool_surface` gate, is absent from `_DESC_BASELINE`'s 18 rows, and is absent from the 18-tool lean footprint measurement. Its return type is `dict[str, Any]` (`_handlers.py:657-663`), so a new `to_dict()` key adds **no** output-schema chars either. |
| **D3** | **0** | **none** | 0 | 0 | **FITS.** CLI subcommand + plugin JSON + doctor check. Touches no DESC, no MCP schema, and not `SKILL.md` frontmatter (the third remainder leg). |

**The binding constraint on D2 (hard):** the explanatory field goes on **`memory_health` only**. Adding a coverage key to `curation_counts`' return dict flows into `curation_pending` (`scope_overview.py:175 → 327`) and forces a `DESC_MEMORY_SCOPE_OVERVIEW` edit — its brace enumeration at `handlers/scope_overview.py:45-47` is set-compared against a hardcoded 9-name set at `test_server_v12_features.py:1271`. That is ~80-200 chars of the 452, plus edits to `test_health.py:240`, `test_server_v12_features.py:1216`/`1234`/`1271`, `docs/api.md`, and the key-set-equality invariant at `scope_overview.py:190-193`. The AC says *"dead_weight empty with an explanatory field"* — `dead_weight` is `memory_health`'s bucket, not scope_overview's `dead` count. **The AC is satisfiable at zero cost. Reject any variant that spends DESC.** Same for `curation_hint` (D2's N4): `_shared.py:591-620` feeds a **lean-surface** DESC (`handlers/write.py:148-150`, baseline 3325) — do not add a coverage key to its payload.

**Secondary constraint:** if the implementer is tempted to expose the gate as a *tool parameter* (`memory_health(assume_covered=…)`), that spends input-schema chars against the 119 free and re-opens the Phase 2 failure class. `hook_telemetry_events` belongs on the **pure functions** (`compute_health`, `curation_counts`, `find_demotion_candidates`) — none of which is an MCP handler. Do not let it reach a facade signature.

**One DESC-adjacent note:** if D2 documents `telemetry_coverage` inside `DESC_MEMORY_HEALTH`, place it **outside** the `"Returns buckets"`…`"CLI equivalent:"` region (`test_server_v12_features.py:1330-1331` slice, hardcoded 14-name `expected` at `1381-1394`) or add the name to `expected`/`NON_BUCKET`. Either way it is free — `memory_health` is not in the budget.

---

## 5. SEQUENCING VERDICT — **the plan's `D1 → D2` is NOT a real dependency; all three are parallel**

**Question asked: does D2's coverage gate need D1's new event kind? Evidence says no.**

D2's coverage predicate keys on `kind == "use"` with `attribution == "hook"` / `triggered_from == "stop_hook"`, and `kind == "turn_audited"` with `triggered_from == "stop_hook"`. `use_token_expired` carries **none** of those fields — deliberately, per D1's §5 (`attribution` is omitted precisely so `is_admin_recorded_event` at `eval.py:2504-2506` reads it as in-session). No coupling.

**And the reverse direction is clean too** — I checked all three of D2's walks against D1's new kind:
- `health._StatsAccumulator.handle_event` (`health.py:1130-1153`): `_HANDLERS.get("use_token_expired")` → `None` at `:1151` → no handler runs. It bumps `_total_events` (`:1134`) and the distinct-session set (`:1146-1148`), which is *correct* — it is real client-session activity. **No health.py change required for D1.**
- `curation_counts` (`health.py:2622-2685`): every branch is a positive `if kind == …`; unknown kinds fall through untouched.
- `find_demotion_candidates` (`consolidate.py:953-990`): reads only `search` (`954`) and `use` (`976`). Blind to the new kind.

**What the `D1 → D2` arrow actually buys** is file contention, not semantics: both items edit `eval.py` (D1 the rosters at `2403-2422` + the comment block; D2 the counters at `481-496` / `659-665` and `EvalReport` at `296-345`). Different regions, merge-safe, but serializing keeps the CHANGELOG attributable and keeps two reviewers off one file. **Keep D1 first as a convention, not as a gate** — if D1 stalls, D2 must not block.

**D3 is genuinely parallel.** Zero file overlap with D1 or D2. Its only shared file with anything is `CHANGELOG.md`.

**One hidden coupling found, running the OTHER way — and my recommendation is to NOT act on it.** D2's N6 is correct: `doctor.py:988` counts `turn_audited` with **no** `triggered_from` check, so a store driven only by in-process `memory_audit_turn` (`handlers/audit_turn.py:305`, `triggered_from="mcp_tool"`) reads as "hook is wired" — the exact conflation D2 exists to fix, in doctor. That makes doctor the natural third consumer of D2's coverage predicate, and D3 adds a check to the same file. **Do not couple them.** D3's check is config-shaped (it reads hook JSON, not telemetry) precisely because the SessionStart hook records nothing; wiring it to D2's telemetry predicate would make three items interdependent for no gain. File the doctor-cadence conflation as a follow-up.

**Revised graph:**
```
D1 ∥ D2 ∥ D3      (D1 nominally first: smallest, and it lands the eval-roster
                   edit D2's reviewer will want to have already read)
```

---

## 6. TOP RISKS — ranked by "ships inert or unsafe"

**A finding that reshapes the ranking: the Phase 2 failure class is now mechanically guarded.** `tests/test_proposals_gate_parity.py:532 test_no_handler_parameter_is_dead_at_the_mcp_boundary` is a **whole-surface, both-direction** parity check between every registered tool's served schema and `bettermemory.handlers.<name>`'s signature, and `:570 test_every_facade_parameter_is_actually_forwarded` catches the declare-but-don't-forward variant. A wire parameter can no longer ship inert. **None of D1/D2/D3 adds one anyway** — so the inert-ship risk has migrated entirely to the *unguarded* surfaces: the eval rosters and D3's negative mandate.

### Risk 1 (HIGHEST) — D1's roster-2 omission has NO tripwire, and D1 believes it does

**The defect in the recon, not the code.** D1 row 8 tells the implementer that `test_doctor.py:1105-1109` fails if you add to `_KNOWN_SIDE_EFFECT_KINDS` and forget `_IN_SESSION_SIDE_EFFECT_KINDS`. I traced it: because `ADMIN_RECORDED_EVENT_KINDS` is *derived* as `KNOWN − IN_SESSION` (`eval.py:2446-2448`), the assertion `ADMIN ∪ IN_SESSION == KNOWN` is a tautology whenever `IN_SESSION ⊆ KNOWN`. Adding only to `_KNOWN_` **passes**. So does `test_eval.py:2283-2297` (it checks the opposite containment). So does the behavioural `test_doctor.py:1112-1139` (it iterates ADMIN and asserts doctor *excludes* those kinds — i.e. it asserts the broken behaviour).

**Failure mode if shipped:** `use_token_expired` lands in `ADMIN_RECORDED_EVENT_KINDS`; `is_admin_recorded_event` returns True; `doctor._check_audit_turn_cadence` skips the event at `doctor.py:965` — dropping it from `total_events` **and its session from the census**. Eval's session tally treats the session as never having existed. Every test green. This is the precise failure class (`ADMIN_RECORDED_EVENT_KINDS` exists because doctor once manufactured phantom sessions) reappearing through the one hole the apparatus doesn't cover.

**Mitigation (mandatory):** D1 test #1 in §3 — an explicit membership assertion plus a kind-agnostic behavioural half. Do not rely on any existing test. Add the assertion in the **same commit** as the roster edit.

### Risk 2 (HIGH, unsafe rather than inert) — D1's false-expiry regression on hookful stores

Structurally confirmed: eviction at `_shared.py:332` (→ `session.py:1045`) runs before the dedup purge at `_shared.py:334-337`. Any retrieval the Stop hook settled, followed by an idle gap ≥ 1800 s with no `memory_*` call, is evicted unseen and — under the plan's naive design — reported as a loss. **Its own tests would pass**: no fixture in `tests/test_health.py`, `tests/test_consolidate.py`, or the token tests emits `triggered_from="stop_hook"`, so a hookless test suite cannot see a hookful regression. This directly violates the AC "hookful behavior unchanged."

**Mitigation:** the `extra_pending` fix in §2 D1b, plus D1 test #2 (the only test in Phase 5 that constructs a hookful store). Also record the residual honestly: `iter_events` reads active segments only (`events.py:913`), so a rotation between the hook's `use` and the eviction still yields a false expiry — the same residual the existing auto-commit path already carries, and implausible inside 30 minutes at a 10 MB cap.

### Risk 3 (MEDIUM-HIGH, classic inert) — D2's `None = assume covered` default

The default that keeps ~15 existing assertions green is exactly the default under which a forgotten production wiring is invisible: `report_for_directory` → `compute_health`, `scope_overview.py:175`/`:246`, `_shared.py:593`, and `consolidate.py:1298` must **each** pass the derived value. Miss one and that surface silently never gates, with the pure-function unit tests all passing and the three-surface parity tests (`test_health.py:254`, `:325`) still agreeing — because 0 == 0 == 0 agrees, and "covered everywhere" agrees too.

**Mitigation:** D2 tests #1-#3 in §3 must drive the **production entry points** (`report_for_directory`, `consolidate(apply=True)`/`run_auto_consolidate`, the `memory_scope_overview` handler), never the pure functions. Pair each with its hookful positive control so the gate cannot pass by being always-on.

### Risk 4 (MEDIUM) — D3's "records nothing" is a comment, and its matcher is unverified

Two distinct exposures. (a) The hazards 3+4 mandate has no mechanical enforcement; one future `.record()` — added by someone who reasonably thinks "just stamp it `cli_*` like consolidate does" — re-opens anchor hijack, because `hook.py:719` filters on `triggered_from` alone and never reads `attribution`. (b) The matcher string `"startup|resume"` is **unverified in this tree** — D3 flags this honestly; the only in-repo SessionStart examples are vendored/installed third-party manifests and none uses a matcher. A wrong matcher means the feature ships and simply never fires, with every test green.

**Mitigation:** D3 test #1 (zero-event-log-delta) as the standing guard for (a). For (b), re-fetch the hooks docs before pinning the matcher, or ship with the matcher **omitted** (fires on all five, matching the Stop entry's own shape and the behaviour the fact pack's docs claim) and add it once verified — the cost of firing on `clear`/`compact`/`fork` is ~12 ms of index scan, which D3's own measurement makes acceptable.

### Risk 5 (LOW-MEDIUM) — D3's field-inertness window

`uvx bettermemory session-start` does not exist on any published wheel until this ships; argparse exits 2 on an unknown subcommand (`test_cli_smoke.py:598-606` pins non-zero) and `|| true` swallows it. Every pre-upgrade install runs a no-op. That is the correct design — but it means "works on my machine" cannot be confirmed until the PyPI release lands. **Mitigation:** verify manually against the installed wheel after the release tag goes green, and keep `|| true` (its rationale is already documented at `plugin/hooks/hooks.json:2`).

### Risk 6 (LOW) — D2's `cli_` literal ban

`test_eval.py:`**`3160`** fails on any `"cli_"` constant or axis-constant reference anywhere in `src/` outside `eval.py`. The `else → model` fall-through in §2 is the only compliant shape. Loud failure, cheap fix — listed only so the implementer doesn't reach for the obvious wrong thing.