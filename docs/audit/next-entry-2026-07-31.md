<!--
Next-session entry brief. Successor to phase5-entry-2026-07-31.md.

Lives under docs/audit/ deliberately: that directory is outside the
doc-claims checker's corpus (which globs docs/*.md non-recursively), so
the line references below cannot fail CI as the code moves. They were
verified at HEAD 1281d57 (v3.32.0) — anchors to re-verify, not gospel.
-->

# bettermemory entry brief — 2026-07-31 (post-3.32.0)

Successor to `docs/audit/phase5-entry-2026-07-31.md`; same house format, same discipline. Produced by three read-only recon lanes (`sdk-delta`, `our-surface`, `backlog`) plus an adjudicator that independently re-opened every anchor the lanes' central recommendations rest on. **Every anchor below was personally re-read at HEAD `1281d57`; every number was measured this session, not copied.**

Like the plan it succeeds, this belongs under `docs/audit/` — that directory is outside the doc-claims checker's corpus, so its line references cannot fail CI as the code moves. Treat anchors as anchors to re-verify, not gospel.

**Four lane claims are WRONG at HEAD. Two are load-bearing:**

1. **`sdk-delta` says `ctx.client_id`'s replacement is gone, making the `SessionRegistry` key a design decision needing telemetry. It is not.** Its stated reason — "`RequestParamsMeta` is a TypedDict with exactly one key (`progress_token`), so even `getattr` can't reach it" — is false. Verified at `mcp_types/_types.py:79`: `class RequestParamsMeta(TypedDict, extra_items=Any)`. It is an **open map**; arbitrary keys round-trip. The read changes from `getattr(meta, "client_id", None)` to `meta.get("client_id")` and nothing else. This collapses the port's only claimed "design decision" into a one-line accessor. (`our-surface` had this right.)
2. **`backlog` publishes "16 skips = the healthy default leg." Measured here: 4213 passed, 19 skipped.** Same 4232 collected. The 3-skip delta is `tests/test_consolidate.py:2360/2392/2432`, gated by `_skip_without_cli` (`tests/test_consolidate.py:1880-1883`) — and it fires because **`.venv` is corrupted right now** (§5.1). 19 is not a healthy signature; it is the corruption tell. Do not publish 16 as a baseline without repairing first.

Two smaller ones, worth fixing so nobody chases them:

3. `backlog` cites `tests/test_server_v12_features.py:1271` for the pinned 9-key `curation_pending` set. Wrong line, wrong shape. The real pins are `:1488` (set-equality over the wire), `:1506` (dict-equality, zero-store), `:1651` (`test_desc_memory_scope_overview_enumerates_curation_pending_keys`, DESC-prose parity by regex).
4. `sdk-delta` sizes the `inputSchema`/`outputSchema` rename at "~25 sites" and puts the `mcp_types.Tool` fields at `:1415`/`:1426`. Measured: **39** attribute-access sites across 6 test files, and the fields are at `_types.py:1416`/`:1424`. `our-surface`'s 39 is correct.

Everything else both SDK lanes claimed about mcp 2.0.0 — I re-read from the unpacked wheel in the uv cache (`/Users/mattias/.cache/uv/archive-v0/1QjiOjhGUglKwD4z/mcp`, `.../WN3r3dzjraJ4EcI8/mcp_types`) and it holds.

---

## 1. State of the world

The 2026-07-30 upgrade plan (`docs/audit/upgrade-plan-2026-07-30.md`) is **fully drained**: Phases 0–7 all carry `PHASE STATUS: DONE` (`:239`, `:350`, `:421`, `:584`, `:753`, `:859`, `:973`, `:1201`). HEAD is `1281d57`, tag `v3.32.0`, tree clean, `version = "3.32.0"` (`pyproject.toml:3`). The 3.31.1 hotfix cap `mcp>=1.0.0,<2.0.0` is live at `pyproject.toml:44` with a 10-line rationale comment above it (`:33-43`). CI is now **10 legs**, not the 9 the incident doc names: 6 `test` matrix slots (`ci.yml:66-71`), `install-from-constraints` (`:106`, added by the hotfix), `typecheck-pyright` (`:181`), `test-embeddings` (`:205`), `test-embeddings-fast` (`:236`).

Measured at HEAD this session (`uv run --no-sync pytest tests/test_resident_footprint.py -q -s`):

| leg | live | ceiling | free |
|---|---|---|---|
| lean DESC | **25,773** | 26,000 (`tests/test_server.py:5932`) | 227 — but only **127** before the `_DESC_BUDGET_PRESSURE` UserWarning at 25,900 (`:5935`) |
| uncapped remainder | **7,069** | 7,500 (`tests/test_resident_footprint.py:253`) | 431, **all unencumbered** — `_SCHEDULED_PARAM_RESERVE = 0` (`:260`), `_SCHEDULED_PARAMS = ()` (`:389`) |
| instructions | 1,608 | — | |
| input / output schemas | 5,233 / 1,077 | — | |
| skill frontmatter | 759 | — | |
| **aggregate resident** | **34,450** | reported only | |
| toolcost-style blob (lean) | **33,960** | — | |

Lean surface 18 tools, full surface 27. The title scrub saves **2,812** chars; unscrubbed remainder is 9,881, which does not fit under 7,500 — the ceiling is the second net, exactly as `builder.py:456-462` claims.

**127 chars of DESC pressure margin is the tightest constraint on anything that touches a lean-surface description.** Budget it before you write it.

Suite baseline, measured (`uv run --no-sync pytest -q`): **4213 passed, 19 skipped in 272s**. See §5.1 before you trust that number.

---

## 2. Headline: the mcp 2.x port

### 2.1 What is actually broken

`mcp.server.fastmcp` does not exist in 2.0.0 — confirmed by `ls` on the unpacked wheel; there is no shim and no deprecation path. `mcp/server/mcpserver/` does not exist in any 1.x (`sdk-delta` checked 1.27.0 → 1.29.0, the last 1.x). **There is no overlap version.** Any dual support must be a genuine `try/except ImportError` fork, not an aliased import.

Import inventory at HEAD — **8 sites in 5 files**, of which **5 in 4 files** are load-bearing:

| site | what it is | 2.x status |
|---|---|---|
| `src/bettermemory/builder.py:37` | `from mcp.server.fastmcp import FastMCP` | breaks |
| `src/bettermemory/handlers/_shared.py:24` | `Context as _FastMCPContext` | breaks |
| `src/bettermemory/session.py:57` | `Context`, under `TYPE_CHECKING` (guard opens `:48`) | breaks type-check only |
| `tests/test_resident_footprint.py:69` | bare `FastMCP` probe | breaks |
| `tests/test_schema_title_scrub.py:50` | bare `FastMCP` probe | breaks |
| `tests/eval/live_adapters.py:207,217,218` | `import mcp`, `ClientSession`, `StdioServerParameters`, `stdio_client` | **safe** — all three still exported at 2.0.0 `mcp/__init__.py:66-68,81,131,143` |

`bench/` and `plugin/` have zero mcp imports (`plugin/` is 5 files: `.mcp.json`, `README.md`, `hooks/hooks.json`, `.claude-plugin/plugin.json`, `skills/bettermemory/SKILL.md`).

**The incident doc is stale on both counts** and should be corrected in the port commit: `docs/incidents/2026-07-31-mcp-2-unbounded-constraint.md:41` and `:113` name four modules and miss `tests/test_schema_title_scrub.py:50` (which shipped in 3.32.0's E5, after the incident was written); `:113` also cites `_shared.py:23`, which is `:24` at HEAD.

### 2.2 What changes in `src/` — three lines

Verified against 2.0.0 source, one by one:

- **`MCPServer.__init__` accepts everything we pass.** `builder.py:153` passes positional `"bettermemory"` and `instructions=`. Both survive as `name: str | None = None` and `instructions: str | None = None` (`mcp/server/mcpserver/server.py:146-152`). Everything gone from the constructor (`host`, `port`, `sse_path`, `stateless_http`, …) is something we never passed.
- **`.tool()` is signature-identical**, `mcpserver/server.py:621-630`: `(self, name=None, title=None, description=None, annotations=None, icons=None, meta=None, structured_output=None)`. All 27 `mcp.tool(name=…, description=…)` calls (`builder.py:249-382`) port untouched. `mcp.run("stdio")` (`cli/serve.py:59`) and `mcp.instructions` (read at `tests/test_resident_footprint.py:315`) both survive.
- **`Context` generic arity 3 → 2.** 1.x: `Generic[ServerSessionT, LifespanContextT, RequestT]` (`fastmcp/server.py:1098`). 2.0: `Generic[LifespanContextT, RequestT]` (`mcpserver/context.py:32`). This is a **hard import-time `TypeError`**, not lazy, because `handlers/_shared.py:40` is a module-level `TypeAlias`. Fix is two character-ranges: `_shared.py:40` and `session.py:59`, each `[Any, Any, Any]` → `[Any, Any]`.

**Porting `_shared.py:40` ports the whole handler package.** 54 `ctx: Context | None = None` annotation sites resolve through that one alias — 27 in `_handlers.py`, 27 across `handlers/*.py`, measured — and 24 handler modules plus `_handlers.py:42` re-import it from there.

- **`Context.client_id` is gone** (grepped: zero hits anywhere in `mcp/server/mcpserver/`). 1.x's implementation, verified byte-exact at `fastmcp/server.py:1286-1290`, is `getattr(self.request_context.meta, "client_id", None)`. In 2.0 `Context.request_context` still exists (`mcpserver/context.py:95-99`) and `ServerRequestContext.meta: RequestParamsMeta | None` still exists (`mcp/server/context.py:46`) — only its type changed from a pydantic model with `extra='allow'` to a TypedDict with `extra_items=Any`. **Same data, different accessor.** `session.py:1326`'s single read becomes `meta.get("client_id")`; and `request_context` raises `ValueError` outside a request (`mcpserver/context.py:97-98`), which `_key_for_ctx`'s existing `except (AttributeError, ValueError)` at `session.py:1325` already catches. **Nothing is subtracted; the never-snuff rule is satisfied by a one-line accessor, not by a telemetry campaign.**

### 2.3 What does NOT change — the part everyone expected to be scary

Every private attribute `_strip_schema_titles` (`builder.py:464-479`) reaches through survives verbatim in 2.0.0:

| our reach-through | 2.0.0 |
|---|---|
| `mcp._tool_manager` | `mcpserver/server.py` — present |
| `._tools` (mutable dict) | `mcpserver/tools/tool_manager.py:23` |
| `tool.parameters` (mutable dict) | `mcpserver/tools/base.py:35` |
| `tool.fn_metadata.output_schema` | `base.py:36` |
| `_tool_manager.get_tool(name).fn` | present |

And the three non-obvious behaviours the scrub's correctness rests on, all re-read in 2.0.0:

- `Tool.from_function` still hard-codes `parameters = arg_model.model_json_schema(by_alias=True)` (`base.py:100`), so pydantic titles remain unavoidable — **the scrub is still needed.**
- `Tool.output_schema` is still a `cached_property` over `fn_metadata.output_schema` (`base.py:53-55`), so the in-place-mutation requirement documented at `builder.py:430-441` carries over unchanged and is still the correct design.
- There is still **no supported way to suppress titles.** `_tool_manager` remains private and unhooked; the migration guide does not mention it. E5's feature-detect-and-no-op shape stays exactly right.

**Budgets do not move.** `sdk-delta` built the real 27-tool server under 2.0.0 behind a shim and measured 9,132 served schema bytes, 0 residual titles, per-tool byte-identical to 1.27.0. `_DESC_BUDGET_CEILING` and `_REMAINDER_CEILING` are driven by our own constants and are SDK-version-independent. **The port consumes zero headroom.**

### 2.4 What it costs — all of it in `tests/`

Measured at HEAD, not estimated:

| class | break | files | sites |
|---|---|---|---|
| A | `from mcp.server.fastmcp import FastMCP` → collection error | 2 | 2 |
| B | `content, structured = await srv.call_tool(...)` — 1.x returns a bare list **or a 2-tuple** (`fastmcp/server.py:343`); 2.0 returns `CallToolResult \| InputRequiredResult` (`mcpserver/server.py:498-504`) | **44** | **exactly 1 unpack per file — 44 one-line edits** |
| C | `.inputSchema` / `.outputSchema` → `.input_schema` / `.output_schema` | 6 | **39** (28 in `test_schema_title_scrub.py`, 6 in `test_resident_footprint.py`, 2 in `test_server.py`, 1 each in `test_server_user_claims.py`, `test_episode_search_scan_and_fetch.py`, `test_proposals_gate_parity.py`) |
| D | `_tool_manager` reach-through | 4 | 6 |

Total 73 `call_tool` references across 47 files, but only 44 are unpacks; the other 29 are prose, `live_adapters.py` client calls, or single-value reads. `src/` mentions `inputSchema` exactly once and it is a comment (`builder.py:425`). `tests/test_tool_surface.py` reads only `tool.name` and survives untouched. `test_server.py`'s DESC budget block reads `t.description`, which is unchanged in 2.0 — **the budget guard itself does not break.**

`test_schema_title_scrub.py` is the heaviest single file (A + C + D). Its oracle at `:157`, `:188`, `:249` and its warm-cache mutation probe at `:329`/`:407` all hit anchors that survive in 2.0.0 — **those tests port; they do not need re-derivation.**

### 2.5 The dependency-closure hazard — this is the part that repeats the incident

mcp 2.0.0's declared deps, read from METADATA:

```
pydantic>=2.12.0          ← NO UPPER BOUND
mcp-types==2.0.0          ← exact pin
httpx2>=2.5.0, opentelemetry-api>=1.28.0, jsonschema>=4.20.0,
pyjwt[crypto]>=2.10.1, sse-starlette>=3.0.0, ...
Requires-Python: >=3.10
```

mcp **1.27.0** declares `pydantic<3.0.0,>=2.11.0`. Our own `pyproject.toml:45` is `"pydantic>=2.0"` — also unbounded. **Today we are protected from pydantic 3.0 only transitively, by mcp 1.x's cap.** Lifting the mcp cap silently removes our only pydantic upper bound, and pydantic 3.0 would land in `install from declared constraints` exactly as mcp 2.0 did. **Cap pydantic in the same commit as the port.** Our effective pydantic floor also rises `2.0` → `2.12.0`.

`requires-python` needs no change: both mcp majors are `>=3.10`, we are `>=3.11` (`pyproject.toml:6`). No matrix change.

### 2.6 Minor or 4.0? — **Minor. Not close.**

Three independent arguments, all checked:

1. **No tool or parameter is renamed or removed.** The port changes how we call the SDK, not what we register. Nothing in CONTRIBUTING's forbidden list (`CONTRIBUTING.md:79-85`: rename, remove, change a type, change a default, redefine an enum) is engaged.
2. **The wire is byte-identical, and class C is why that needs saying.** `mcp_types.MCPModel` sets `model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)` (`mcp_types/_types.py:48`) and the transport serializes `by_alias=True`. `input_schema` → `inputSchema` on the wire, exactly as 1.27.0 emits it. **The rename is Python-attribute-only. A pinned client sees no diff.**
3. **One Python-API wrinkle, name it and move on.** `build_server` is public (`__init__.py` `__all__`) and its return annotation changes `FastMCP` → `MCPServer` (`builder.py:93`). `docs/api.md` documents neither type and `Context` is not exported publicly. The compatibility contract scopes itself to the 27 MCP tools and the on-disk format; a builder's return-type annotation is neither. Release-note it; do not cut a 4.0 for it.

### 2.7 Dual support vs. floor bump — **recommend the clean floor bump**

**Recommendation: port to `mcp>=2.0.0,<3.0.0` in a minor, with `pydantic>=2.12,<3` added in the same commit. Do not build a compat shim.**

Evidence, in order of weight:

- **Dual support's cost is not the shim, it's the permanently branched type surface.** Because there is no overlap version, `_mcp_compat.py` would carry a `try/except ImportError` fork whose exported types differ by branch — and both mypy (`ci.yml:100`) and pyright (`ci.yml:181`) gate on it. Proving it works also needs the matrix run under both majors: today's ten legs all install from `uv.lock`, which pins one `mcp`. That is +2 lockless legs minimum, permanently.
- **The users a shim protects have a workaround this time.** They can pin `bettermemory<the-port-release`. During the 3.31.1 incident they explicitly could not — every published wheel was dead, which is why the hotfix was the only remedy. That asymmetry is the whole argument.
- **The risky part of the port turns out not to change.** Everything the E5 scrub depends on survives verbatim (§2.3), so the shim would be insuring against a risk that was measured and found absent.

**On the E5 precedent, which points the other way and must be addressed rather than ignored.** `builder.py:453-454` refuses a floor bump in as many words: *"Raising the floor to protect a size optimisation would be a real install-compat break in exchange for nothing"*, restated at `tests/test_schema_title_scrub.py:14-18` as *"the `mcp>=1.0.0` floor is an install-compat promise."* That reasoning does not transfer, and the clause it turns on is **"in exchange for nothing."** E5 was asked to break installs to buy a size win. The port is asked to break installs to buy *being installable on the SDK line upstream now ships by default*. What the E5 stance still binds is the **manner**: the floor is a promise, so lifting it is a deliberate, announced, changelog'd act — never a side effect of a refactor. Both `builder.py:449-454` and `test_schema_title_scrub.py:14-18` must be rewritten in the port commit or they become lies in the tree.

### 2.8 Schedule pressure is lower than the incident doc implies

mcp 2.0.0's own METADATA (line 65) says v1.x *"continues to receive critical bug fixes and security patches"* and recommends verbatim: keep *"a `<2` upper bound on your requirement (for example `mcp>=1.28,<2`) until you've migrated."* **3.31.1's cap is upstream's own recommended posture, not merely a holding action.** Note their suggested floor is `>=1.28`; ours is `>=1.0.0`. If the port slips, raising our floor to `>=1.28,<2` is a defensible interim that costs nothing.

### 2.9 The one trap that ships silently

2.0.0 injects `Context` by matching `typing.get_type_hints(fn)` against the `Context` **class** (`mcpserver/utilities/context_injection.py`). If the alias at `_shared.py:40` still resolves to a 1.x `Context` — say, a partial port, or a shim branch that picks wrong — **injection stops firing, `ctx` is always `None`, and every client collapses into `_DEFAULT_CLIENT_KEY`.** No exception. No red test, because `_key_for_ctx` (`session.py:1322-1336`) swallows exactly that shape into the default bucket by design. `tests/test_session_registry.py` (which already reaches `_tool_manager.get_tool(name).fn` at `:181` precisely because `ctx` does not survive `call_tool`) is where a **positive** assertion belongs: two distinct client ids must produce two distinct keys, through the wire.

### 2.10 Acceptance test

`install from declared constraints` (`ci.yml:106`) is the gate, and it must be green **before** the tag. Its smoke step already imports `bettermemory` and calls `build_server()` under `BETTERMEMORY_DIR` (`ci.yml:169-176`) — it exercises exactly the surface the port moves. Its comment block (`:117-137`) explicitly says the fix for a red run is "a considered constraint edit (or a port), never deleting the job."

---

## 3. Deferred backlog — sized, anchored, prioritized

Priority order below is justified by one rule: **items that make a published claim false rank above items that leave a claim un-made.** Silent rot in numbers we publish is the failure mode this project has now filed three times.

### P1 — F: four published footprint numbers are stale (+238 / +246). **XS. New this session; on nobody's list.**

Phase 6's final DESC edits moved `_DESC_BASELINE` 25,535 → 25,773 and updated `tests/test_server.py:6006-6044`. **Four other surfaces were not updated, and they are the published ones.**

| surface | says | live (measured) | delta |
|---|---|---|---|
| `tests/test_resident_footprint.py:180` | 25,535 | **25,773** | +238 |
| `docs/audit/upgrade-plan-2026-07-30.md:982` aggregate | 34,212 | **34,450** | +238 |
| `CHANGELOG.md:16-17` | 34,212 / 33,714 | **34,450 / 33,960** | +238 / +246 |
| `bench/toolcost/README.md:25`, `docs/internals.md:90`, `bench/toolcost/results/bettermemory-2026-07-31.json:12,16` | 33,714 / 26,846 | **33,960 / 27,092** | +246 |

**I reproduced this independently**, replicating `bench/toolcost/run.py:167-191`'s `measure()` in-process against the lean 18-tool surface: `input_schema_bytes` came out **5,252 = 5,252**, matching the artifact exactly — which validates the method — while `name_description_bytes` came out **27,092** against the artifact's 26,846. The footprint test's own toolcost-convention blob prints **33,960** against the artifact's 33,714. Both deltas are +246, consistent with +238 chars of prose plus JSON escaping.

Three consequences to state plainly:

- **Nothing is red and nothing will go red.** `_FOOTPRINT_BASELINE` is diagnostic-only by design (`tests/test_resident_footprint.py:111-121`). This is silent rot, not a broken build — which is precisely why it needs a scheduled fix rather than waiting for CI.
- **The Phase 6 table at `:979-983` fails by addition alone**: it lists lean DESC 25,773 and aggregate 34,212, but 25,773 + 7,069 + 1,608 = 34,450.
- **The artifact is labelled `"bettermemory_version": "3.31.1"` while `CHANGELOG.md` quotes it as the 3.32.0 figure.** It was generated before the follow-up DESC edits that landed in the same commit (`164c019`).

This is the repo's own rule — *every published number traces to a committed artifact* — failing in the one way the rule cannot catch: the artifact is committed, it just does not describe the tree it was committed with. **Fix = re-run `bench/toolcost/run.py`, then correct the five prose sites.** Note the runner prefers `repo/venv/bin/python` (`run.py:196`), which is the healthy interpreter here (§5.1).

### P2 — A: the transient reject hint never names `episode_write`. **XS. One string literal.**

Recorded at `docs/audit/upgrade-plan-2026-07-30.md:1240-1241`. The hint is `src/bettermemory/handlers/write.py:317-323`; it offers exactly two remedies — rephrase to the durable form, or `acknowledge_transient=True` — and never mentions the tier G2 just blessed as the home for that content.

Sized, not assumed: **zero test pins** (`grep` for the hint's phrases across `tests/` and `docs/` returns only the three source lines; tests pin the `transient_warning` *status*, never the string), and **zero budget** — reject hints are runtime response payloads, in neither `_DESC_BASELINE` nor any `_FOOTPRINT_BASELINE` leg. The reverse direction is already covered (`handlers/episode_write.py:39`: *"Use this for content memory_write would reject as transient"*), so the gap is strictly one-way. **Cheapest open item in the repo.**

### P3 — B: `SYSTEM_PROMPT_ADDENDUM` carries pre-G2 loop wording. **S. Zero budget.**

Same plan anchor (`:1242-1243`). Block at `src/bettermemory/prompts.py:179-202`, mirrored byte-for-byte at `docs/system_prompt.md:174-197` and pinned equal by `tests/test_prompts.py:55` (`test_addendum_matches_docs`).

Measured: addendum **9,065** chars total, episode block **1,241** of them. Not a footprint leg → no budget cost, but `docs/system_prompt.md:10` warns Claude Code truncates the paste at ~1.8 KB, so **net-neutral is the right target**, not net-add.

What it is missing, against what SKILL.md was rewritten to say (`plugin/skills/bettermemory/SKILL.md:124-153`): the headed section *"The state channel: write state here, mint facts at close"*, the explicit routing rule (*"Treat this as the routing rule, not one option among several"*), and the *"promote is a filter, not a loop"* framing. The addendum still describes episodes as a place you *may* use, and its "Loop iteration pattern" predates both G1's cheap-scan parameters and G2's minting moment.

Pins to respect: `tests/test_prompts.py:69-82` (all four `episode_*` names must survive) and `test_addendum_tool_names_exist_on_server` (`:678`) — every `memory_*` / `episode_*` token must resolve to a registered tool.

### P4 — D: `cold_endorsement_memories` is not gated by the D2 coverage predicate. **M — and it wants a decision, not an implementation.**

Recorded in the code, not the plan: a labelled `FOLLOW-UP, deliberately not done here` block at `src/bettermemory/health.py:2933-2945`.

State verified at HEAD: only `dead_weight` is gated — `health.py:1870` `if telemetry_covered:` wraps the dead-weight loop and nothing else; `ColdEndorsementMemories` is built unconditionally at `health.py:2045`. The predicate it would use, `is_hook_telemetry_event`, exists at `health.py:224` and is already exported (`:3371`).

**The dependency is mechanical, and I confirmed it rather than trusting the comment.** `_is_weakly_endorsed` (`health.py:667`, gate at `:701-702`) returns True on `explicit_applied_count == 0`, and `explicit_applied_count` is everything not `auto` — both `hook` and `model` tiers route into it. So the Stop hook's containment matcher is a direct producer. On a hookless store the only remaining producer is a deliberate `memory_record_use`, so the bucket over-fires exactly where D2's gate exists to stop over-firing.

**Blast radius is the curation hint** — the one curation surface the model gets without asking. `handlers/_shared.py:752` sums `dead + drifted + cold_endorsement_memories`, and `:750` already passes `hook_telemetry_events=0` to arm the *dead* leg. A hookless store is therefore nagged through the ungated leg while the gated one correctly reads zero.

**Hard constraint carried from D2:** the returned key set is pinned. Keys flow into `curation_pending` (`handlers/scope_overview.py`) and are set-compared against a hardcoded 9-name list at **`tests/test_server_v12_features.py:1488`**, dict-compared at **`:1506`**, and regex-parity-checked against the DESC prose at **`:1651`**. The fix must not add a key. `docs/api.md:56` already documents the `dead`-only gate and points at `telemetry_coverage` for the why — it will need the same treatment.

Two viable shapes, and choosing between them is the decision: widen `telemetry_covered` to cover the cold-endorsement leg, or drop that leg from the hint's pressure sum when coverage is absent. The docstring itself says this *"changes what a published rollup means on the one surface the model does not have to ask for, so it wants its own decision."*

### P5 — E: `doctor.py`'s `turn_audited` count does not check `triggered_from`. **S/M.**

Filed at `docs/audit/phase5-entry-2026-07-31.md:319`, with an explicit recommendation *not* to couple it to D3. Confirmed at HEAD: `src/bettermemory/doctor.py:988` is a bare `if event.get("kind") == "turn_audited"`. An in-process `memory_audit_turn` stamps `triggered_from="mcp_tool"`, so an MCP-only store reads as "hook is wired" — the exact conflation D2 fixed everywhere else.

Import direction is safe: `health.py` does not import `doctor`, and `doctor.py` already imports from `eval`, which imports `health.applied_tier`. So `health` is already in doctor's transitive graph.

What makes it M rather than S: three verdict branches (`doctor.py:1012`, `:1035`, `:1053`) and the published `turn_audited_events` info key (`:1001`) all read the counter, so tightening it changes what `bettermemory doctor` says on stores that are genuinely fine but MCP-driven.

### P6 — C: `episode_search(ids=…)` by-filename fast path. **S, but UNMEASURED — bench first.**

Recorded at plan `:1221-1226`; the refusal is written into the code with its reasoning at `src/bettermemory/handlers/episode_search.py:229-238`. The validator to mirror already exists and is 8 lines: `src/bettermemory/episodes.py:124-131` (`_session_dir`, alnum + `_` + `-`, raises `ValueError`).

Two things to know before spending on it:

1. **The win is conditional.** The fast path only skips the walk when a session is *also* known. On a bare `ids`-only call the candidate loop still iterates `iter_session_ids()` (`:216-221`), so the saving is `O(total episodes, frontmatter-parsed)` → `O(sessions × len(ids))` stats — real, but proportional to session count, not to 1.
2. **Three behaviours ride the post-load loop** and must survive any short-circuit: the floor skip (`:252`, with a load-bearing asymmetry comment at `:250-251`), the `since`/scope/excluded-scope filters (`:254-258`), and the datetime sort whose rationale is at `:274-282`. Worktree isolation is *not* one of them — `apply_worktree_filter` is already False whenever `id_filter is not None` (`:177-182`), so the fast path does not lose it.

**No measurement exists.** This is the one open item whose "filed as a follow-up" does not come with a number. Bench first, ship second.

### Not in the numbered order — `docs/ROADMAP.md` triage

Three entries are stale and one gap is unrecorded. This is cheap (prose only) and can ride any commit:

- **`ROADMAP.md:8-10`** says `apply_write_gates` and `memory_verify`'s attestation refusal are "both Unreleased." **Both shipped in 3.31.0** (`CHANGELOG.md:677`).
- **`ROADMAP.md:11-15`** — reconciling the private gate copies is now **half done**. `handlers/proposals.py:223` calls `apply_write_gates`. `consolidate._apply_llm_proposal` (`consolidate.py:2278`) still hand-rolls its own chain — size, transient, similarity — each with a deliberate rationale (gate the LLM claim, not `body_with_provenance`), so it remains policy review as the roadmap says. **Only the consolidate half is open.**
- **`ROADMAP.md:25-32` (Standing tier)** — its premise sentence is now false. Phase 5's D3 shipped `bettermemory session-start` (`src/bettermemory/cli/session_start_cmd.py`) wired to a **SessionStart hook with the matcher deliberately omitted** (`plugin/hooks/hooks.json`), whose stdout is injected verbatim. **The transport layer for the standing tier exists and ships.** What is missing is content: the block is scope *counts* only (`session_start_cmd.py:303`, `_render_block`) and prints nothing on an empty store. Rewrite as "the channel exists; the budgeted, verified knowledge payload does not."
- **Unrecorded gap:** `search.py:1086-1118` carries a *new negative result* dated 2026-07-30 — the cosine-band recut, closed because 0 of 274 top hits on the dogfood store carried `matched_unique == 0`, so the population a band rule changes has zero representation in the instrument. The roadmap does not carry it. It belongs next to the w2 entry.
- **Also unrecorded, inherited from 3.31.0:** the changelog states plainly that ingest bypasses `_validate_write_payload`. Confirmed — `grep -n "_validate_write_payload" src/bettermemory/ingest.py` returns nothing. That belongs under roadmap item 1 as a fourth sub-item.

**Verified NOT invalidated** (recorded so nobody re-checks): item 6's relevance-label v2 flip. `_relevance_label` (`search.py:1079-1128`) is byte-identical v1 — B3 shipped `matched_leg` *instead of* a recut. The ~51%-over-79-promotions figure still stands.

### E2 — what 4.0 owes (no code; keep it recorded)

Scope: merge `memory_write_confirm`/`memory_write_cancel` and `memory_scope_enable`/`memory_scope_disable` into one call each. Recorded at plan `:1181` (`ITEM STATUS: RECORDED as a 4.0 line item — no code`) and `ROADMAP.md:120`.

**The removal *is* the entire saving.** `CONTRIBUTING.md:82` forbids removing a tool or parameter within a major; `:72` permits only adding. So inside 3.x a merged replacement **grows** the DESC budget. Combined DESC of the four, measured live this session: `scope_disable` 231 + `scope_enable` 55 + `write_confirm` 515 + `write_cancel` 216 = **1,017 chars**.

4.0 also owes: a full deprecation cycle first (`CONTRIBUTING.md:93-107` — changelog `Deprecated` entry naming surface + replacement + target version, then a runtime warning on the correct lane, with the load-bearing phrase `deprecated and will be removed in bettermemory` that `pyproject.toml`'s `filterwarnings` and `tests/test_origin.py`'s fence both key on); unwinding every promise of the names, which are not confined to the tools' own descriptions (`handlers/write.py` DESC lines, `DESC_MEMORY_SCOPE_OVERVIEW`'s `pending_writes` line, the staging path's hints, `docs/api.md`, and the symmetric-pair contract in `handlers/scope_toggle.py`'s module docstring); and a 4.0 release-notes reiteration of every removed item.

**Structural blocker on the write pair:** the staged flow is *enforced*, not optional, for `category='user-inference'` — the pending state has no other exit.

**One asymmetry worth fixing cheaply:** E3 got a code-side pointer (`config.py:499-500`, next to `full_tool_surface`, so the decision is reachable from the code). E2 did not — it is discoverable only from `ROADMAP.md` and the plan document. If the plan is ever archived, E2's rationale goes with it. A three-line comment at `handlers/scope_toggle.py` and `handlers/write.py` fixes that for free.

---

## 4. Recommended order of work

**0. Repair `.venv` and re-establish the baseline. (minutes, blocking)** See §5.1. Everything downstream reports numbers; do not report numbers from a corrupted interpreter. Confirm the suite reads **4216 passed / 16 skipped** after repair — if it still reads 19, the CLI is still broken and the three `test_consolidate` subprocess tests are inert.

**1. Ship P1 (stale numbers) and P2 (the reject hint) as one small commit.** Both are XS, neither touches the SDK, and P1 must land *before* the port because the port will move footprint numbers again and you do not want two generations of rot compounding. Re-run `bench/toolcost/run.py`, correct the five prose sites, add one string literal to `write.py:317-323`. Zero budget spend on both.

**2. Do the mcp 2.x port, as a floor bump, in one release.** In order inside it: (a) `_shared.py:40` + `session.py:59` arity, `session.py:1326` accessor, `builder.py:37/93/214/420` type swap; (b) the 44 class-B unpacks and 39 class-C renames; (c) `pyproject.toml:44-45` — `mcp>=2.0.0,<3.0.0` **and** `pydantic>=2.12,<3.0.0` in the same diff; (d) rewrite `builder.py:449-454` and `test_schema_title_scrub.py:14-18`, which currently assert a floor that no longer exists; (e) add the positive Context-injection assertion to `tests/test_session_registry.py`; (f) correct `docs/incidents/2026-07-31-mcp-2-unbounded-constraint.md:41,113`. Gate the tag on `install from declared constraints` green.

**3. Then P3 (addendum rewrite).** It is independent of the port and net-neutral by design, but it wants the port's churn out of the way because both touch prose the doc-claims checker reads.

**4. Then P4 (`cold_endorsement_memories` gating) as a decision item, with P5 (doctor) riding along.** They share a premise — telemetry-coverage honesty — and P5's own filing note says not to couple it to D3, which is now moot; coupling it to P4 is the natural pairing and keeps one reviewer on one idea.

### What should NOT be done next, and why

- **Do not build the dual-support shim.** §2.7. The cost is a permanently branched type surface past two type-checkers, bought for users who have a working `bettermemory<X` pin — which is the workaround that did not exist during the incident and does exist now.
- **Do not do P6 (the `ids` fast path) before benching it.** It is the only open item with no number, its win is conditional on a session also being supplied, and three correctness behaviours ride the loop it would short-circuit. Ship a measurement, not an optimization.
- **Do not open E2.** It is 4.0 material by construction — inside 3.x a merged replacement *adds* to a budget with 127 chars of pressure margin. Record the code-side pointer (§3, last paragraph) and stop.
- **Do not spend the 431-char remainder or the 127-char DESC margin on anything in this cycle.** The port needs zero and the P1–P5 items need zero. The next phase that schedules a *parameter* will want that reserve, and `_SCHEDULED_PARAM_RESERVE` is already at 0.
- **Do not start a fact-pack re-anchoring pass as its own项目.** §6 — the packs' prose is sound; only coordinates rotted. Re-anchor opportunistically, or run the 90-line script once *inside* the port commit for `footprint.md` and `docsguards.md`, the two the port actually leans on.

---

## 5. Hazards

### 5.1 `.venv` is corrupted right now. This is not a warning about the past.

Measured this session:

```
uv run --no-sync which bettermemory   → .venv/bin/bettermemory          (exists)
.venv/bin/bettermemory --help         → ModuleNotFoundError: bettermemory
.venv/bin/python -c "import bettermemory" → ModuleNotFoundError
venv/bin/python  -c "import bettermemory" → OK 3.32.0
```

Diagnosis: `.venv/lib/python3.13/site-packages/_editable_impl_bettermemory.pth` exists and contains the correct path (`…/bettermemory/src`), and `bettermemory-3.32.0.dist-info` is present — but that path is **absent from `sys.path`**, so `.pth` processing is not taking effect. `pyvenv.cfg` names `home = …/cpython-3.13-macos-aarch64-none/bin` while the live interpreter resolves under `cpython-3.13.13-…`; the venv's base interpreter moved out from under it. That is the corruption the plan documents at `docs/audit/upgrade-plan-2026-07-30.md:903-905` (*"recurred twice more (sixth and seventh) … plain `uv run` re-syncs implicitly and does it too. Use `uv run --no-sync` for every invocation here"*).

**Consequences the next session must know:**

- **The suite still passes** — `tests/conftest.py:20-21` inserts `src/` into `sys.path`, so in-process tests are immune. Only the three **subprocess** CLI tests notice, via `_cli_is_functional()` (`tests/test_consolidate.py:1863-1878`) which shells out to `bettermemory --help`.
- **The 3-skip delta IS the tell.** 19 skips = corrupted; 16 skips = healthy. The 16 environment-independent skips are 2 × `tests/eval/test_live_adapters.py` (`BM_EVAL_LIVE=1` maintainer lane) + 14 × numpy-absent (`test_fsutil.py` ×3, `test_semantic_fastembed.py` ×1, `test_semantic_persistence.py` ×10). Anything *other* than 16 or 19 means rebuild, don't debug.
- **Two venvs exist and they disagree.** `venv/` (no dot) is healthy and is what `bench/toolcost/run.py:196` prefers. `.venv/` is what `uv run` uses and is broken. When re-running the toolcost benchmark for P1, this works in your favour — but do not assume the two are interchangeable.
- **`uv run --no-sync` for every invocation, including the repair-adjacent ones.** A bare `uv run` re-syncs implicitly and is a known cause.

### 5.2 Port-specific traps

- **The silent-injection failure (§2.9).** The highest-value new test in the whole port, because the existing code is *designed* to swallow it.
- **`_strip_schema_titles` fails silently by design.** `builder.py:466-470` logs at debug and returns. Two guards watch for it (`test_schema_title_scrub.py` measures the scrub still finds something; the remainder ceiling sits below the unscrubbed total) — but **both live in class-A files that will not even collect until they are ported.** For the window between the `src/` change and the test sweep, the scrub is unguarded. Port `test_schema_title_scrub.py` early, not last.
- **`pydantic` unbounded (§2.5).** Cap it in the same commit or the port re-arms the exact gun that fired on 3.31.0.
- **The DESC budget has 127 chars before it warns.** Any port-adjacent prose edit — a rewritten `builder.py` comment is free, a rewritten *description* is not — must be measured, not eyeballed.
- **`_FOOTPRINT_BASELINE` is diagnostic-only** (`test_resident_footprint.py:111-121`). It will not fail when you break it. It is already broken (P1). Re-measure in the same commit as anything that moves a leg — the file says so at `:114-116` and the last phase did not.

### 5.3 Release discipline (unchanged, restated because it is easy to get backwards)

Push main → **watch the full CI matrix green** → then push the tag. `release.yml` reuses `ci.yml` to gate the PyPI publish, and a leg marked `experimental: true` is `continue-on-error` and therefore **silently exempt** from that gate (`ci.yml:24-33`). None are marked today. Do not add one without reading that comment.

---

## 6. What is safe to trust

**Current — trust these:**

- `docs/audit/upgrade-plan-2026-07-30.md` **as a status ledger**. Every phase carries an accurate `PHASE STATUS: DONE`. Its *numbers* are not current (P1: `:982` is wrong by +238 and fails by addition).
- `docs/audit/phase5-entry-2026-07-31.md` — its corrections and its follow-up filings (including the doctor item at `:319`) all still hold.
- `docs/incidents/2026-07-31-mcp-2-unbounded-constraint.md` **as a narrative**. Its root-cause analysis and its "why CI was green" section are exactly right and worth reading before the port. Its *inventory* is stale in two places (§2.1).
- `CONTRIBUTING.md`'s compatibility contract (`:68-107`) — re-read this session, unchanged, and it is what makes the port a minor.
- In-code rationale comments in `builder.py`, `episode_search.py`, `health.py`, `session.py`, `_shared.py` — I opened a dozen of these and every one described HEAD accurately. **This project's comments are more trustworthy than its docs**, which is worth knowing when the two disagree.

**Stale — verify before relying:**

- **`docs/audit/upgrade-plan-facts/*.md` — all six, badly.** Last modified 2026-07-30, before Phases 5–7. `backlog`'s mechanical sweep found **161 of 496 anchors (32%) still exact; 290 (58%) findable within ±40 lines; 206 far or gone.** I hand-confirmed the worst offenders at HEAD: `settlement.md:31` puts `handle_event` at `health.py:1130` → actually **`:1389`**; `settlement.md:7` puts `issue_use_tokens` at `session.py:303` → **`:1065`**; `settlement.md:64` puts `_is_weakly_endorsed` at `health.py:458-500` → **`:667`** (moved *earlier* — drift runs both directions); `trust.md:16` puts `_RELATIVE_CITATION_RE.finditer` at `verify.py:1793` → **`:1048`**; `trust.md:21` puts `detect_path_drift` at `:447` → **`:549`**; `writepath.md:7` puts `apply_ingest_plan` at `ingest.py:551` → **`:643`**; `footprint.md:116` puts `test_policy_lives_once_not_triplicated_in_descriptions` at `test_server.py:5939` → **`:6168`**. Underlying churn is `+19,453 / −635 across 81 files` since `95af021`.
  **Verdict: trust the prose, never the line number.** Every claim I hand-checked was substantively correct; only the coordinates rotted. `footprint.md` is the most reliable pack (79% within ±40) and is one of the two the port will lean on. The plan's own risk register predicted this at `:1347-1349`; that instruction has come due.
- **`docs/ROADMAP.md`** — three stale entries and two unrecorded gaps (§3).
- **`CHANGELOG.md:16-17`, `bench/toolcost/README.md:25`, `docs/internals.md:90`, `bench/toolcost/results/bettermemory-2026-07-31.json`** — all four carry pre-Phase-6-final numbers (P1). The JSON artifact is additionally mislabelled `3.31.1`.
- **`tests/test_resident_footprint.py:180`** — `descriptions=25_535` is 238 stale. Diagnostic only; it will never fail.

**Two files whose fact-pack anchors still hold exactly**, hand-confirmed, because Phases 3–7 did not touch them structurally: `hook.py` (`run_audit` `:307` ✓, `_disabled_scopes_from_events` `:637` ✓, `_latest_in_process_session` `:687` ✓) and `doctor.py` (`_check_audit_turn_cadence` `:920` ✓).

---

## 7. Honest uncertainty

Three things this recon could not settle, and the experiment for each:

1. **Nobody has run the full suite under mcp 2.0.0.** `sdk-delta` built the real 27-tool server under 2.0.0 in a throwaway venv behind a one-line shim and reported 27 tools / 9,132 bytes / 0 residual titles, byte-identical to 1.27.0 — I could not re-execute that (it requires installing 2.0.0), so I verified its *premises* from the cached wheel source instead, and every one held. **The experiment that settles it: after the `src/` changes, `uv pip install --resolution highest .` into a scratch venv and run the ported class-A/B/C/D files.** That is exactly what `install from declared constraints` does in CI, so this is a dress rehearsal for the gate, not extra work.
2. **The real cost of the 44 + 39 test edits is estimated from grep, not from doing them.** The shape is high-confidence — class B is exactly one unpack per file, all inside a per-file `_call` helper — but "83 mechanical edits" has been wrong before. There is no cheap way to de-risk this beyond doing the first five and re-estimating.
3. **P6's win is unquantified** and P4's user-visible impact is unquantified. For P4 specifically: nobody has measured how many memories in the dogfood store would actually change bucket under the gate. `memory_health` on a hookless store answers it in one call, and that measurement should precede the decision, not follow it.