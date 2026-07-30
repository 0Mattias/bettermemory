All facts verified. Assembling the report.

# Resident-footprint pins at HEAD 95af021 (clean tree, v3.30.0, venv Python 3.13.13, mcp SDK 1.27.0)

## 1. DESC_* module map + per-tool char counts (computed live; served desc == raw constant, verbatim)

Canonical definitions live in per-tool modules under `src/bettermemory/handlers/`; re-exported by `src/bettermemory/handlers/__init__.py` (e.g. :51) and again by the legacy shim `src/bettermemory/_handlers.py:66-93`. `builder.py:39-68` imports from `._handlers`.

**Lean surface (18 tools, shipped default) — total 26,336 chars:**

| tool | chars | constant location |
|---|---|---|
| memory_search | 3,389 | handlers/search.py:75 |
| memory_write | 3,108 | handlers/write.py:77 |
| memory_scope_overview | 2,820 | handlers/scope_overview.py:27 |
| episode_write | 2,350 | handlers/episode_write.py:30 |
| memory_update | 2,234 | handlers/update.py:51 (= base + `DESC_MEMORY_LINKS_TAIL` 888 chars, update.py:31, concatenated at update.py:78) |
| episode_search | 2,064 | handlers/episode_search.py:39 |
| memory_verify | 1,817 | handlers/verify.py:29 |
| episode_promote | 1,597 | handlers/episode_promote.py:209 |
| episode_handoff | 1,560 | handlers/episode_handoff.py:136 |
| memory_record_use | 1,556 | handlers/record_use.py:29 |
| memory_audit_turn | 1,365 | handlers/audit_turn.py:38 |
| memory_show | 851 | handlers/show.py:29 |
| memory_remove | 463 | handlers/remove.py:14 |
| memory_list | 454 | handlers/list_active.py:23 |
| memory_scope_disable | 231 | handlers/scope_toggle.py:21 |
| memory_write_cancel | 216 | handlers/write.py:152 |
| memory_write_confirm | 206 | handlers/write.py:144 |
| memory_scope_enable | 55 | handlers/scope_toggle.py:29 |

**Gated tools (9, full surface only) — 14,272 chars:** memory_health 2,600 (health.py:22), memory_conflicts 2,561 (conflicts.py:60), episode_patterns 2,490 (episode_patterns.py:51), memory_acknowledge_miss 2,020 (acknowledge_miss.py:60), memory_proposals 1,632 (proposals.py:28), memory_curate 1,214 (curate.py:24), memory_rename_scope 887 (rename_scope.py:14), memory_restore 439 (restore.py:14), memory_list_tombstones 429 (tombstones.py:14). Full-surface served desc total = 40,608 chars (27 tools).

`DESC_MEMORY_LINKS_TAIL` is NOT a tool; it is only consumed inside DESC_MEMORY_UPDATE — do not double-count it.

## 2. Registration mechanism + lean/full gate + config shape

- `build_server` (builder.py:87) constructs `FastMCP("bettermemory", instructions=…)` at builder.py:153-205 (instructions literal :170-204, served 1,608 chars / 1,622 UTF-8 bytes), then calls `_register_tools` (builder.py:213).
- Each tool = one `mcp.tool(name=…, description=DESC_…)(handlers.method)` call, builder.py:249-317 for the 18 unconditional registrations.
- **Gate 1** builder.py:322: `if config.behavior.full_tool_surface or config.proposals.auto_propose:` → registers `memory_proposals` only.
- **Gate 2** builder.py:347: `if config.behavior.full_tool_surface:` → registers the other 8 gated tools (:348-382).
- Episode tools are deliberately ungated: builder.py:291-305 records the 2026-07-30 evaluation that gating episode_search/episode_promote "is not available" (plugin skill instructs episode_promote, docs/system_prompt.md names it, audit-loop/curate-loop drive both, `episode_search(swarm_id=…)` is the fan-in primitive), and that "description prose is not part of the compatibility contract" (builder.py:304-305).
- Counts pinned: tests/test_tool_surface.py:66-67 `_LEAN_COUNT = 18`, `_FULL_COUNT = 27`; gated set `_GATED` :33-45; proposals auto-surface test :101-112.
- **Config shape:** `BehaviorConfig.full_tool_surface: bool = True` at config.py:473 (dataclass default = full, for programmatic/test construction); loader default is **False** (lean) at config.py:1097-1099 (`_coerce_bool(behavior_raw.get("full_tool_surface"), False)`, rationale comment :1091-1096 and :454-463). Asymmetry pinned by tests/test_config.py:161-162 and tests/test_tool_surface.py:115-126. String coercion (`"0"` → False) pinned at tests/test_config.py:364-377. NOTE: config.py:461 claims "these objects are frozen" but every decorator is bare `@dataclass` (config.py:323,328,476,481,499,520,536) — not actually frozen.
- Schemas derive from the `ToolHandlers` wrapper method signatures in `_handlers.py:331+` (comment :383-389: wrapper signature mirrors handler minus `deps`; `ctx: Context` is excluded via FastMCP's context-param detection). Handler-signature shape is pinned by `_snapshot_params` asserts in tests/test_direct_imports.py (e.g. :292-300).

## 3. Pydantic titles in inputSchema — yes, everywhere

Inspected the served `list_tools()` on a lean server: every one of the 67 properties across 18 tools carries an auto-generated `"title"` (e.g. `"Content"`, `"Scopes"`), and every schema carries a top-level `"title": "<toolname>Arguments"` — 85 title occurrences total. No `description` keys exist inside any inputSchema (param docs live only in DESC prose). All 18 tools ALSO serve an auto-derived `outputSchema` (from return annotations; e.g. `memory_searchOutput`, `memory_showDictOutput`), total 1,770 compact-JSON chars, with its own titles.

Measured: lean inputSchema total 7,077 chars compact JSON → 5,030 after recursively deleting `title` keys (**saves 2,047 chars**).

Mechanism (mcp SDK 1.27.0, pinned in uv.lock:986; pyproject.toml:34 constraint is only `mcp>=1.0.0`): `FastMCP.tool()` → `add_tool()` → `Tool.from_function()` in `mcp/server/fastmcp/tools/base.py`, which sets `parameters = func_arg_metadata.arg_model.model_json_schema(by_alias=True)`; `FastMCP.list_tools()` maps `inputSchema=info.parameters`, `outputSchema=info.output_schema`. There is no SDK hook to suppress titles.

**Cleanest hook point in this codebase:** post-registration scrub at the bottom of `builder._register_tools` (builder.py:213) — iterate `mcp._tool_manager._tools.values()` (`ToolManager._tools: dict[str, Tool]`; `Tool.parameters` is a plain mutable `dict[str, Any]` field, `Tool.model_config == {}` so attribute/dict mutation is legal) and delete `"title"` keys recursively; optionally same for `tool.fn_metadata.output_schema`. Call-time validation is unaffected (it uses `fn_metadata.arg_model`, never the emitted dict). `mcp.tool()(…)` returns the fn, not the Tool, so the scrub must go through the manager dict.

## 4. The caps and what "ratchet" means mechanically

- **Instructions cap:** tests/test_server.py:5691 `test_instructions_block_fits_under_truncation_budget` — hard `len(body) <= 1700` chars (:5709), floor `>= 800` (:5715), UTF-8 `<= 1750` bytes (:5722). Current body: 1,608 chars / 1,622 bytes → **only 92 chars of headroom**. Load-bearing phrase pin: test_server.py:5725-5755 (10 verbatim phrases incl. "OPT-IN retrieval", "Using your stored preference", "spot-check", "PROACTIVE", "your job is to capture").
- **Desc budget:** test_server.py:5804 `_DESC_BUDGET_CEILING = 27_500`; :5807 `_DESC_BUDGET_PRESSURE = 27_400` (warn only). Test :5862 `test_default_on_descriptions_fit_budget` sums the SERVED lean descriptions via `_lean_descriptions` (:5777-5798, builds a real server with `BehaviorConfig(full_tool_surface=False)`). Current total 26,336 → 1,164 chars of slack.
- **`_DESC_BASELINE`** :5814-5836 — 18 entries, explicitly DIAGNOSTIC ONLY (:5808-5813: "nothing asserts these"). Recorded sum 26,238; **stale at HEAD in 4 entries**: memory_search 3467→actual 3389, memory_scope_overview 2812→2820, memory_verify 1625→1817, memory_write 3132→3108. Only episode_search was re-measured (comment :5817-5819).
- **Ratchet mechanics** (docstring :5882-5903): (1) never raise the ceiling to re-admit collapsed policy; (2) a ceiling change is legitimate only as a deliberate recalibration — measured total unmoved, `_DESC_BASELINE` re-measured in the same commit, new ceiling a round number. Downward precedent exists: "27,800 → 27,100 (3.6.4 ratcheted the sweep in)" (:5914); history trail :5906-5918. So item (e) = lower the literal at :5804 + re-measure the baseline table in the same commit; `_DESC_BUDGET_PRESSURE` follows automatically (ceiling − 100).
- **Aggregate context (item d) reference numbers:** instructions 1,608 + lean descs 26,336 + lean inputSchemas 7,077 + lean outputSchemas 1,770 + skill frontmatter 759 ≈ 37.5k chars. External baseline: bench/toolcost/README.md:20-36 — 3.29.0 lean wire cost **38,009 bytes full** (names+descs 28,604 B, inputSchemas 7,096 B), 4.84x claude-mem; `measure()` in bench/toolcost/run.py:167-190 serializes the whole wire tool objects (so outputSchema is inside `full_bytes` but not in either sub-component).

## 5. CONTRIBUTING.md compatibility contract — exact terms

Section "Versioning and the compatibility contract", CONTRIBUTING.md:54-122. Headline (:56): within a major, the surface in docs/api.md + on-disk `SCHEMA_VERSION` are stable.

- **Stable within 3.x** (:62-68): tool names (:64); required param names/positions (:65); defaults of optional params (:66); closed enum sets (:67); return-shape keys per status (:68 — e.g. `duplicate` keeps `matches`, `ungrounded` keeps `claims`).
- **Permitted** (:70-77): adding tools, adding optional params (defaults preserving behavior), adding return fields, adding statuses, adding enum values, tightening/loosening validation.
- **Forbidden within a major** (:79-85): renaming a tool or parameter; **removing a tool or parameter**; changing a param/return-field type; changing an optional-param default; redefining an enum value.
- **DESC wording is NOT covered** — confirmed both by omission in :62-68 and positively by builder.py:304-305.
- **"core" preset:** registration-gating is deployment policy, not removal — precedent: the lean gate shipped in minor 3.4.0 (CHANGELOG.md:4318-4331) with an upgrade note, and docs/api.md:3 documents "27 tools, but only 18 register by default". A new opt-in preset (new config key or new accepted value) is additive → minor-OK, provided (a) the full surface still registers all 27, (b) docs/api.md:3's registration statement is updated, (c) the shipped DEFAULT stays lean-18 (shrinking the default would contradict builder.py:291-305's recorded decision that episode tools stay resident, and would break the lean-count pins in tests/test_tool_surface.py:66,90 until updated in the same commit). Repurposing/deprecating the `full_tool_surface` TOML key follows the config-key deprecation lane (:105 — one-time `log.warning`, pattern `_apply_legacy_endorsement_debt_alias`, config.py).
- **Micro-tool merges (write_confirm/cancel, scope_enable/disable):** removing any of the four is "Removing a tool" (:82) → **forbidden within 3.x; requires the deprecation cycle (:93-107: changelog `Deprecated` entry + runtime warning + keeps functioning) and removal only at 4.0** (:109-115). A merged replacement tool may be ADDED in a minor alongside the old ones.

## 6. Gate reject-messages today (write path) + duplicated DESC prose — cut candidates

All soft-refusal responses carry a teaching `hint` string:

| status | reject built at | hint lines |
|---|---|---|
| `credential_warning` | handlers/write.py:257-281 (CredentialGate) | :263-273 |
| `transient_warning` | write.py:299-320 (TransientGate) | :305-312 |
| `scope_mismatch` | write.py:339-361 (ScopeMismatchGate) | :344-352 |
| `ungrounded` | write.py:384-406 (GroundednessGate) | :388-398 |
| `duplicate` | write.py:473-490 (DedupActiveGate) | :477-482 |
| `previously_removed` | write.py:519-541 (DedupTombstoneGate) | :525-533 |
| `pending` (staged, not reject) | write.py:893-952 `_stage_pending` | :909-919 |
| `credential_warning` (update) | handlers/update.py:187-210 | :200-207 |
| `stale` (update CAS) | update.py:303-325 | :320-323 |
| `stale` (verify CAS) | handlers/verify.py:186-201 | :196-200 |
| unstattable-path refusal (verify) | verify.py:154-159 (ValueError) | — |

Gate order + chain: `_WRITE_GATES` write.py:573-581 (order rationale :564-572), `CONTENT_GATES` :707-709, `apply_write_gates` :712-737 (shared by ingest/consolidate/proposals per :584-607).

**Five concrete cut candidates (exact substring char counts, measured):**

1. DESC_MEMORY_WRITE `transient_warning` bullet — **197 chars** (write.py:111-114). Fully re-taught by the TransientGate hint at reject time. No test pins it (test_server.py:5917 only narrates it in a comment).
2. DESC_MEMORY_WRITE `credential_warning` bullet — **192 chars** (write.py:115-118). Re-taught by CredentialGate hint. The schema test (test_server.py:6410-6428) pins the `acknowledge_credential` PARAM in inputSchema, not desc prose.
3. DESC_MEMORY_WRITE `previously_removed` bullet — **211 chars** (write.py:124-127). Re-taught by DedupTombstoneGate hint (inspect `removed_reason` / `memory_restore` / `force=True`).
4. DESC_MEMORY_UPDATE "Concurrency:" paragraph — **452 chars** (update.py:58-64). Re-taught by the `stale` hint (update.py:320-323) exactly when it matters.
5. DESC_MEMORY_VERIFY "Concurrency:" paragraph — **445 chars** (verify.py:38-46). Re-taught by verify's `stale` hint (verify.py:196-200).

Runner-ups: `scope_mismatch` bullet 127 chars (write.py:128-130); `duplicate` bullet 248 chars (write.py:119-123) — only partially cuttable: the corroboration semantics half is not in the reject hint (it IS observable via `corroboration_recorded`/`corroborations` response keys and documented in docs/api.md:83). Sum of the five = 1,497 chars; with runner-ups ≈ 1,800. Reaching ~14-15k from 26,336 requires far more than reject-duplicated prose — see hazards.

## 7. The plugin skill — sizes, resident vs on-activation

`plugin/skills/bettermemory/SKILL.md`: total 14,455 chars (14,541 bytes). Frontmatter 759 chars (`name: bettermemory` + `description:` whose value is **726 chars**); body **13,688 chars**.

- **Resident per session:** only the frontmatter name+description (the available-skills listing entry). The 13,688-char body loads **on activation only**.
- Other plugin files: plugin/.mcp.json (registers `uvx bettermemory` stdio server — the 1,608-char instructions block is resident via MCP, not the plugin), plugin/hooks/hooks.json (1,247 bytes; Stop hook → `memory_audit_turn`, pinned tests/test_plugin.py:281-320), plugin/.claude-plugin/plugin.json (version must match pyproject — tests/test_plugin.py:170-197).
- Skill pins: tests/test_plugin.py:203-233 (frontmatter shape; description ≥ 80 chars — **no maximum exists today**), :236-253 (body must name memory_search/write/show/verify/record_use/scope_overview), :256-273 (case-insensitive phrases "OPT-IN retrieval", "transparency", "verify"), tests/test_prompts.py:658-702 (every `memory_*`/`episode_*` name in SKILL.md must resolve on a FULL-surface server — the test constructs `Config(...)` which gets the dataclass full default). Parallel addendum pins: docs/system_prompt.md (10,116 bytes) ↔ `SYSTEM_PROMPT_ADDENDUM` parity at test_prompts.py:55, name-existence at :618.

## PLAN HAZARDS

1. **Wording-locked DESC substrings — a blind trim will fail CI in ~10 distinct tests.** Exact-substring pins that must survive any cut: `test_policy_lives_once_not_triplicated_in_descriptions` (test_server.py:5939-5979) requires each of "Using your stored preference", "do NOT call", "non-negotiable", "PROACTIVELY", "aggressive writing is safe" in **exactly one** lean desc — `!= 1` fails, so deleting a phrase (0 copies) fails the same as duplicating it. Point-of-call pins test_server.py:5982-6003 (search/write). Query-cue pins :6006-6043 ("nouns", "re-query" required; "paraphrase recall" forbidden). Field pins: test_prompts.py:212/219/224/229 (recently_removed_in_worktree, recommendations, depends_on_resolved, curation_hint), :262-296 (max_takeaway_bytes, pruned_sessions, most-recent, worktree, disabled_scopes, promoted_from_episode_id), :351-377 (PERMISSIVE, LINKED, "gone from disk", strict), :393 (memory_list in AUDIT_TURN); test_episode_handoff_guard.py:1008 (synonym set). Set-equality parity pins: test_server_v12_features.py:1208-1251 regex-extracts the FIRST `\{[a-z_,\s]+\}` block in DESC_MEMORY_SCOPE_OVERVIEW and requires exactly the 9 curation_pending keys; :1253-1330 extracts backticked bucket names between "Returns buckets" and "CLI equivalent:" in DESC_MEMORY_HEALTH; :1360-1390 forbids "endorsement_debt"/"cleanup_endorsement_debt" and requires "cold_endorsement_memories"/"cleanup_cold_endorsements". Any rewrite must update these lockstep lists in the same commit or keep the phrases.
2. **The ~14-15k target cannot be met from reject-duplicated policy alone.** Verified duplicated-policy cuts total ≈ 1.8k chars. The remaining ~9-10k must come from field-reference prose that tests explicitly protect as "genuine field-discoverability reference" (test_server.py:5878-5880) and from parity-pinned enumerations (hazard 1) — those enumerations can move (e.g. to docs) only if their tests are rewritten simultaneously. Budget-ceiling rule 2 (:5889-5892) demands baseline re-measure + round-number ceiling in the same commit for every recalibration.
3. **`_DESC_BASELINE` is already stale at HEAD** (4 entries; recorded 26,238 vs actual 26,336). It is diagnostic-only, so nothing fails today — but a plan that computes "chars to cut" from the table instead of from a live measurement starts 98 chars wrong, and memory_verify is off by +192 (grew in a59f640 without a table update).
4. **Instructions block has 92 chars of headroom** (1,608 of 1,700, test_server.py:5709). Item (a)'s "move policy into gate reject-messages" is viable; moving anything into `instructions` is effectively not. Reject-hint text is also not free: `hint` strings ship on every rejection response — growing them is a per-reject cost and several hint sentences are asserted in handler tests (grep the status name in tests/ before rewording; e.g. test_server_credentials.py, test_server_durability.py exercise these responses).
5. **Title-stripping must be a post-registration dict scrub, not an SDK/config change.** `Tool.from_function` hard-codes `model_json_schema(by_alias=True)`; there is no FastMCP option. Scrub `mcp._tool_manager._tools[*].parameters` (private attr — pin the SDK version or feature-detect; pyproject.toml:34 allows any `mcp>=1.0.0`, so a user's older/newer SDK may differ). Do NOT drop outputSchema by registering with `structured_output=False` — that removes `structuredContent` from every tool result (a wire-shape change); title-scrubbing `fn_metadata.output_schema` in place is safe (validation uses `output_model`). Keep `properties`/`required` intact: test_server.py:6423-6428 asserts `acknowledge_credential` in inputSchema properties for 3 tools, and test_server.py:82-87 asserts a non-empty inputSchema per tool.
6. **"core" preset semantics collide with recorded decisions and tests.** (i) The shipped default must stay lean-18 or `_LEAN_COUNT`/`_FULL_COUNT` (test_tool_surface.py:66-67, :90, :98, :112) and docs/api.md:3 fail; a core preset must be a new opt-in value, and `memory_proposals` must keep auto-surfacing when `[proposals] auto_propose` is on (builder.py:319-325, test :101-112). (ii) builder.py:291-305 records that episode tools are load-bearing for /loop, audit-loop, curate-loop and the server instructions reference them (instructions name memory_scope_overview, memory_search, memory_write, memory_record_use, memory_verify, memory_update — test_server.py:5730-5753); a ~6-tool core that drops episode_handoff/episode_write breaks those loops on any client using the preset, and the plugin's Stop hook drives memory_audit_turn (tests/test_plugin.py:293) — dropping it from a preset silently degrades plugin installs that opt in. (iii) The curate-loop skill requires full surface (config.py:461-463, builder.py:344-346). (iv) If `full_tool_surface: bool` is replaced by a string knob, every `BehaviorConfig(full_tool_surface=…)` construction in tests (test_tool_surface.py, test_conflicts.py, test_curate.py, test_episode_patterns.py, test_bench_toolcost.py, test_server.py, test_config.py) plus cli/tombstones.py:204 prose and docs/api.md, docs/internals.md, plugin/README.md references must move in the same commit; keeping the bool and adding a separate key avoids the churn and the config-deprecation lane.
7. **Tool merges are contract-breaking now.** `memory_write_confirm`/`memory_write_cancel`/`memory_scope_enable`/`memory_scope_disable` removal is forbidden within 3.x (CONTRIBUTING.md:82); the pending-write flow is also structurally enforced for `user-inference` (write.py:548-561) and DESC/api.md promise the confirm/cancel pair by name (write.py:92-95, :909-919; docs/api.md). A merge needs: new merged tool added in a minor, `Deprecated` changelog entry, runtime warnings, removal at 4.0 with migration notes (CONTRIBUTING.md:93-122). Handler signatures are additionally pinned by tests/test_direct_imports.py `_snapshot_params`.
8. **The aggregate footprint test (item d) must not double-govern.** Instructions and descs already have individual caps with their own ratchet law (test_server.py:5691, :5862 + docstring rules); a single-budget test that includes them creates two failure surfaces for one edit. Also: the skill body (13,688 chars) is NOT resident — summing it into a "resident footprint" budget misstates the cost; only the 726-char frontmatter description is. If the aggregate mirrors bench/toolcost's `full_bytes`, note that measurement includes outputSchema and JSON syntax and sorts keys (bench/toolcost/run.py:170-171) — pick one serialization and state it, or the number will disagree with the desc-budget test's `len(description)` sum.
9. **Desc constants are string-concatenation literals** — several char counts above span multi-line adjacent-literal blocks; the pinned regexes (v12:1223 brace block, v12:1265-1268 index anchors "Returns buckets"/"CLI equivalent:") operate on the RUNTIME string, so edits that reflow the source are safe but edits that break an anchor phrase fail with `.index` ValueError, not an assert message.
10. **`_lean_descriptions` (the budget test's measurement) builds the server with `proposals=ProposalsConfig()`** (auto_propose off, test_server.py:5792-5796). If a core preset changes what registers under that construction, the budget test's denominator silently changes — the `_DESC_BASELINE` breakdown will report tools as "gone from the lean surface" (:5853-5855). Recalibrate table + ceiling in the same commit as any surface change.