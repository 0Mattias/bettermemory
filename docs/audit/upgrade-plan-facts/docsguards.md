All facts pinned. Compiling the report.

# Current-code facts at HEAD (95af021, v3.30.0-26)

## 1. `tests/test_doc_claims.py` — plan-doc exemption (CRITICAL)

**The exemption is an exact-path frozenset, NOT a filename pattern.** `tests/test_doc_claims.py:237-243`:

```python
_PLAN_DOCS = frozenset(
    {
        "docs/ROADMAP.md",
        "docs/swarm-convergence-plan.md",
        "docs/v1.6-plan.md",
    }
)
```

Consumed in exactly one place (verified: only two `_PLAN_DOCS` occurrences in the file, the definition and this) — `check_paths`, `tests/test_doc_claims.py:806-810`:

```python
def check_paths(source: str, text: str, line_offset: int = 0) -> list[Failure]:
    """Anchored repo-relative path tokens must exist on disk."""
    out: list[Failure] = []
    if source in _PLAN_DOCS:
        return out
```

The module docstring's deliberately-not-checked bullet, `tests/test_doc_claims.py:174-177`:

> **Planning documents' path claims.** `docs/ROADMAP.md` and the `*-plan.md` files propose files that do not exist yet — that is what a plan is. Their line-refs and symbol claims *are* checked, since those cite current code.

Note the prose implies a `*-plan.md` pattern; the code is a literal list. **`docs/upgrade-plan-2026-07-30.md` gets NO exemption at HEAD** — and it does not even match the documented `*-plan.md` shape (that glob matches names *ending* in `-plan.md`, e.g. `2026-07-30-upgrade-plan.md`, not `upgrade-plan-2026-07-30.md`).

A new `docs/*.md` file enters the corpus automatically: `_living_docs()` globs `docs/*.md` non-recursively (`tests/test_doc_claims.py:602`; subdirectories `docs/incidents/`, `docs/eval/`, `docs/audit/` are NOT scanned). `collect_failures` (`:1140-1154`) then runs **all five checkers** on it. What still applies to a plan doc even if added to `_PLAN_DOCS`:

- **symbol** (`check_symbols`, `:829-851`): `` `sym` [≤2 plain words] in `module.py` `` must resolve by AST (`_SYMBOL_IN_MODULE`, `:438-442`). If the cited module does not exist, the claim is skipped (`:839-840` — `if not candidates: continue`), so proposed symbols in proposed files are safe; proposed symbols in *existing* files fail.
- **test-count** (`check_test_counts`, `:883-927`): "the/all/its N tests in `tests/x.py`" and "`tests/x.py` contains N tests". **Fails when the file is missing** (`:917-920`, "claims a test count for a file that is missing") — a plan proposing "the 12 tests in `tests/new_guard.py`" goes red regardless of `_PLAN_DOCS`. Escape hatches: delta qualifiers ("N *new* tests", `_DELTA_QUALIFIER` `:277-279`), restrictive tail ("that/which/covering/pinning/exercising", `_RESTRICTIVE` `:465`), or bare "N tests in X" with no determiner (`:1333-1337`).
- **line-ref** (`check_line_refs`, `:1023-1137`): `file.py:NNN[-MMM]` (bare or linked) must be in range, forward (start ≤ end), and land within `_ANCHOR_WINDOW = 15` lines (`:490`) of some backticked identifier the paragraph names that exists in the target. Two suppressions: a non-resolving verdict within `_NONRESOLVING_WINDOW = 120` chars (`:414`), or a paragraph-scoped commit pin — `` resolved against `<7-40 hex sha>` `` or "pinned to a named commit" (`_COMMIT_PINNED_PROSE`, `:429-433`). The pin does not cross a blank line (`:1913-1926`).
- **file-count** (`check_file_counts`, `:854-880`): "N files are named `x.py`" must match the repo scan.

**Writing rules that keep `docs/upgrade-plan-2026-07-30.md` green** (pick one strategy):
1. Add the exact path to `_PLAN_DOCS` (one-line edit; the self-test `test_plan_docs_exempt_from_path_claims` `:1255-1259` asserts membership behavior for `docs/swarm-convergence-plan.md` and non-membership for `docs/clients.md`, so adding an entry breaks nothing) — this clears **paths only**.
2. For proposed-file paths without editing the checker: avoid backticks (only `_BACKTICK`-quoted tokens are extracted, `:291`), or use a non-anchored spelling (only `src/ docs/ tests/ bench/ examples/ plugin/ .github/` prefixes match, `:281-289`), or end the preceding text with an illustrative cue "like / e.g. / such as / for example / for instance" immediately before the backtick on the same line (`_ILLUSTRATIVE_CUE` `:292-294` is `$`-anchored on the prefix), or use placeholder stems (`x, y, z, n, mod, spec, foo, bar, baz, qux, example, sample`, `:247-249`).
3. For citations of current code that the executor will then change: **commit-pin each citing paragraph** (`` resolved against `95af021` ``) — the sanctioned freeze; it suppresses both range and anchor halves, paragraph-scoped.
4. Never write total-marked test counts for files that don't exist yet; phrase as "N new tests".

The allowlist is a last resort with a reason ≥40 chars (`test_allowlist_entries_carry_a_reason` `:1206-1209`), keyed `(source, kind, subject)` (`:503-505`), forward guard `test_no_unexpected_false_claims` `:1160-1174`, reverse guard `test_allowlist_has_no_stale_entries` `:1177-1203`.

## 2. Stale-claim fix sites

### 2a. `src/bettermemory/handlers/search.py:109-111` — the 10→65 sentence

Exact text (inside `DESC_MEMORY_SEARCH`, which starts at `:75`):

```python
    "Parameters:\n"
    "- `query`: nouns a memory would contain (tool, file, error names), "
    "not question phrasing — measured 10%→65% recall@1. Weak hits: "
    "re-query, different nouns.\n"
```

(`"Parameters:\n"` is line 108; the sentence spans 109-111.)

**Guard test** `test_search_desc_tells_the_caller_how_to_word_a_query`, `tests/test_server.py:6006-6043`. It pins exactly three things (`:6029-6043`):
- `"nouns" in desc` (:6030) — vocabulary is the measured lever, not keyword-vs-question phrasing;
- `"re-query" in desc` (:6035);
- `"paraphrase recall" not in desc` (:6039).

**It does NOT pin the numbers — changing "10%→65%" cannot break it.** But its own docstring restates the stale measurement ("185-memory store, a 20-question gold set … 10% recall@1 as asked and 65% re-queried", `tests/test_server.py:6011-6013`), as does the handler module docstring (`handlers/search.py:16-22`, which also carries the 90% ceiling arm and the 10% control). A number fix that touches only line 110 leaves three contradicting restatements in the same two files.

Provenance/current truth: the 10/65 pair is the *original live-store* measurement (lexical asked→requery), which `bench/retrieval/README.md:16-24` says "lived only in a commit message and a handful of docstrings" with the store "cited inconsistently as 185 in two places and 190 in four". The committed replacement artifacts measure the same pair at **35%→80%** (v2 canonical, `bench/retrieval/results/v2-unpadded-2026-07-26.json`, `README.md:145-152`) and **40%→95%** (v1, `results/unpadded-2026-07-26.json`, `README.md:205-212`), and the bench explicitly concludes "**no absolute number in this directory is comparable to the 185/190 figures**" (`bench/retrieval/README.md:174-175`) — the corpus is easier than a real store (`:227-231`). There is **no committed artifact for the original 10/65 measurement**; its only durable records are CHANGELOG prose (`CHANGELOG.md:734-742`, the 3.29.0 table: asked 10%/30%, requery 65%/80% at recall@1) and docstrings. Other live restatements of the family: `config.py:70-72`, `semantic_setup.py:114-116`, `doctor.py:2299-2300`, `tests/test_prompts.py:553-554`, `tests/test_semantic.py:423-426` (all docstrings/comments, none asserted on).

Budget interaction: `_DESC_BUDGET_CEILING = 27_500`, pressure at 27,400 (`tests/test_server.py:5804-5807`). Measured live at HEAD: lean total **26,336** chars, `memory_search` desc **3,389** chars → 1,164 chars of slack. `_DESC_BASELINE` (`:5814-5836`) is diagnostic-only ("nothing asserts these", `:5808-5813`) and its `memory_search: 3467` entry is already stale.

### 2b. `README.md:45-47` — cost sentence

```
- Nothing is auto-injected; retrieval is a deliberate tool call. The 18
  default tools do cost ~35 KB of schema per turn either way; the
  description half of that is capped in CI.
```

Current truth: the committed toolcost artifact (`bench/toolcost/results/bettermemory-vs-claude-mem-2026-07-26.json`) measured the full serialized `tools/list` at **38,009 bytes** (`full_bytes`), of which names+descriptions **28,604 B** and input schemas **7,096 B** — i.e. descriptions are ~75% of the total, not "half". That run predates the `episode_search` trim in 95af021 (−1,007 desc chars), so a fresh run lands lower (~37 KB). `bench/toolcost/README.md` ("It also corrects this project's own published figure") documents that the honest unit is the full serialization. "18 default tools" is true at HEAD (`tests/test_tool_surface.py:66`, `_LEAN_COUNT = 18`). "capped in CI" = the 27,500-char desc ceiling above.

### 2c. `docs/internals.md:81-86`

```
27 MCP tools; 18 register by default. Nine curation/power-user tools
sit behind `[behavior] full_tool_surface = true`, and most of those
have a CLI counterpart, so the default per-turn tool context stays
small. Grouped: retrieval, writing (with a staged-confirm flow),
lifecycle, verification, curation, session-local scope toggles, and
episodes. Signatures, defaults, and return shapes: [api.md](api.md).
```

All three numbers are **currently true** and mechanically guarded: `_LEAN_COUNT = 18` / `_FULL_COUNT = 27` (`tests/test_tool_surface.py:66-67`, asserted at `:90` and `:98`); the 9-member `_GATED` set (`:33-45`); `_EXPECTED_TOOL_COUNT = 27` (`tests/test_eval.py:1601`) with set-equality vs the eval enumeration (`:1639-1653`); and **`test_tool_count_prose_tracks_expected_count`** (`tests/test_eval.py:1669-1696`) regex-scans `_TOOL_COUNT_PROSE_FILES` = README.md, docs/api.md, docs/internals.md, CONTRIBUTING.md, plugin/README.md, .claude-plugin/marketplace.json (`:1659-1666`) for `\b(\d+)\s+(?:MCP\s+)?tools\b` and requires 27. Any plan item that adds/gates a tool must move `_LEAN_COUNT`/`_FULL_COUNT`, `_EXPECTED_TOOL_COUNT`, eval's `_TOOL_EVENT_KIND_TO_TOOL`/`TOOLS_WITHOUT_TELEMETRY`, and all six prose files in one commit. ("18 default tools" / "18 register by default" / "25 known tools" deliberately do NOT match that regex — interposed word.)

### 2d. `docs/eval-results.md:62-66` — the stale parenthetical

```
  data the threshold rule wants. A counterfactual sweep
  (`bettermemory eval --threshold-sweep`) replays the 15 v1-flagged
  misses against the stricter v2/v3/v4 rules, which flag none of them
  — so v1 isn't over-firing. (Strictly looser rules can't be
  evaluated from the log at all; `turn_audited` doesn't carry
  `top_hits`.)
```

The parenthetical (64-66) is **false at HEAD**: `src/bettermemory/eval.py:1539-1545` — "historically only `search_miss` events … carried `top_hits`. **Since 3.14 every miss-capable `turn_audited` event carries a compact `top_hits` payload** …"; the text-lane caveat already says the opposite ("Strictly looser rules replay over the turn_audited stream instead — see --widening-preview", `eval.py:1529-1531`); the CLI flag exists (`cli/eval.py:93`), and three labeling passes used it (`docs/eval/widening-labeling-2026-07-{08,22,29}.md`). This sentence is hand-authored prose, not `--report` output (the generated sweep footer is `eval.py:3077-3086` and carries no such claim).

### 2e. `docs/eval-results.md:106`

```
4,891 tool calls across 25 known tools — retrieval (`memory_search`,
5.7%) is dwarfed by upkeep (audit, verify, update, record_use).
```

"25 known tools" renders from `len(doc.tool_usage.rows)` (`eval.py:3112-3114`) — true at measurement (2026-07-16, pre-3.28.0 tool pair); a `--report` re-run at HEAD enumerates **27** (registered set == eval-side set, `tests/test_eval.py:1637-1646`). It is a frozen measurement whose regeneration changes it.

## 3. A5 guard design — DESC sweep, artifacts, allowlist idiom

**Sweep surface.** All 28 `DESC_*` constants are re-exported by `src/bettermemory/_handlers.py:66-93` (27 tool DESCs + `DESC_MEMORY_LINKS_TAIL`, a shared fragment, `:76`); each is defined next to its handler under `src/bettermemory/handlers/` (e.g. `search.py:75`, `write.py`, `episode_search.py:39`). Two sweep options: runtime `import bettermemory._handlers` + `dir()` filter (values are plain `str` built by implicit concatenation), or AST walk of `handlers/*.py` for `DESC_*` assignments. Registration consumes them in `builder._register_tools` (`builder.py:322,347` region).

**Live numeric content of DESCs at HEAD** (measured by sweep): exactly **one** measurement claim — `DESC_MEMORY_SEARCH`: `measured 10%→65% recall@1`. Everything else numeric is a contract constant enforced by adjacent code, not a measurement: `DESC_EPISODE_WRITE` "4 KB"/"64 KB" (frontmatter caps, `handlers/episode_write.py:64-71,108-116`), `DESC_MEMORY_ACKNOWLEDGE_MISS`/`DESC_MEMORY_RECORD_USE`/`DESC_MEMORY_VERIFY` "500 chars" (enforced e.g. `handlers/record_use.py:131`), `DESC_MEMORY_RECORD_USE` "2x" (contradicted weighting), `DESC_MEMORY_WRITE` "<30% token overlap" (groundedness gate, `handlers/write.py:103`). A naive `N%`/`N KB` pattern therefore false-positives on 5 of 6 hits; the discriminator that works on the real corpus is the word **"measured"** adjacent to the number (only DESC_MEMORY_SEARCH has it).

**Committed bench artifacts to pin against** (all git-tracked; logs are not):
- `bench/retrieval/results/{unpadded,padded600,v2-unpadded,v2-padded600}-2026-07-26.json` — shape: `results[]` rows `{arm, probe, n, recall_at_1, recall_at_5}` plus `bettermemory_version`, `commit`, `corpus_sha256` (v2 files), `corpus_size`, `padded`. v2-unpadded is canonical (`bench/retrieval/README.md:135-152`).
- `bench/rot/results/{bettermemory-30d,bettermemory-60d}-2026-07-26.json` (dict keys incl. `repo, t0, t1, claims, …, t0_pinned`), `multirepo.json`, `scorecard.json` (list of 7), `resolution.json` (contains an absolute local store path `/Users/mattias/.claude-memory` and `resolution_rate: null` with an UNDEFINED status note).
- `bench/toolcost/results/bettermemory-vs-claude-mem-2026-07-26.json` — `full_bytes: 38009`, `name_description_bytes: 28604`, `input_schema_bytes: 7096`, `tool_count: 18`, plus the 18 tool names.
- `bench/longmemeval/results/{claude-mem-full500,claude-mem-subset40,s-cleaned-both-arms,claude-mem-full500-INVALID-partial-index}.json`.
- `docs/eval/comparative-live-2026-07-03.json` — `{workload, k, generated_at, results}`.

**Allowlist idiom to copy**: `test_doc_claims._ALLOWLIST` — dict keyed `(source, kind, subject)` with a mandatory ≥40-char reason (`:524-552`, `:1206-1209`), paired forward/reverse ratchet tests (`:1160-1203`), reverse-guard failure text distinguishing "repaired → delete" from "extractor stopped matching → investigate" (`:1191-1203`), and the retired-entries cautionary note (`:553-591`). A second, simpler precedent for "prose number must equal a code constant" is `tests/test_eval.py:1594-1696` (`_EXPECTED_TOOL_COUNT` + `_TOOL_COUNT_PROSE_FILES` + a deliberately narrow regex whose non-matches are documented as deliberate).

Also relevant: `bench/claims.py`'s classifier has its own adversarial suite `tests/test_bench_claims.py` (loads by file location since `bench/` is not a package, `:27-36`) — the pattern to follow if the A5 guard needs to read `bench/` code.

## 4. `docs/incidents/`

Directory holds exactly two files: `README.md` and `TEMPLATE.md`; index says "_(No incidents yet. …)_" (`docs/incidents/README.md:27`). Naming convention `YYYY-MM-DD-short-slug.md` (`README.md:23`). **Framing**: "public postmortems for memory-rot bugs **reported against** bettermemory" (`README.md:3`) — user-report-oriented, not self-found-defect-oriented.

`TEMPLATE.md` structure (lines 1-40): H1 `# YYYY-MM-DD — short-slug`; bold header fields **Reported by** / **bettermemory version at time of report** / **Fixed in** / **Status** (open / fixed / wontfix-with-rationale); sections `## Symptom`, `## Root cause` (with a fixed four-bullet signal taxonomy: Calendar age / Path drift / Commit drift / Threshold rule, plus "Cite the file and line where the bug lives"), `## Fix`, `## Verification`, `## What the surface should do differently`, `## References` (Issue #N / PR #N / Related incidents / Related code `src/bettermemory/<module>.py:<line>`).

`docs/incidents/*.md` is **outside** the doc-claims corpus (non-recursive `docs/*.md` glob) — line-refs there are unchecked.

### Candidate A — staleness-verdict constant function, fixed in 58a4fa4 (shipped v3.30.0)

- Commit: `58a4fa4` 2026-07-26 "fix(verdict): the calendar leg was erasing the measurement it exists to back up". Defect: past `verification_stale_days`, a `never`/`stale` verification status pre-empted both drift legs → every calendar-stale memory read `spot_check_required` regardless of drift; bench/rot measured the shipped default at **100% flag rate, Youden's J = 0.000 — arithmetically `always_flag`** (commit body; `CHANGELOG.md:255-290` under `## 3.30.0 - 2026-07-26` at `:135`).
- Fix shape: calendar-stale + **measured-zero** commit leg → `fresh`; three guards (`never` never demotes; `None` never demotes; path existence alone never demotes). New public primitive `verdict_from_signals` in `src/bettermemory/verify.py:2093`, shared with `_response.attach_commit_drift_counts` (previously a mirrored re-implementation guarded only by comments).
- Evidence trail on disk: touched files per `git show --stat 58a4fa4` — `verify.py` (+113), `_response.py`, `tests/test_verify.py` (+186), `tests/test_bench_rot.py` (+59), `bench/rot/run.py` + both result JSONs re-measured (30d J 0.000→0.111, 60d →0.034, convergence pinned as regression test), `docs/system_prompt.md`, `prompts.py`, `CHANGELOG.md`. Side finding recorded in the commit body: bench/rot's own `verdict_for` conflated "could not ask" with "measured zero". Live-store check documented: 0 of 209 verdicts changed at the shipped default.
- Template fit: maps cleanly onto the taxonomy (Calendar age + Commit drift rows); "Reported by" has no issue — self-found via `bench/rot`.

### Candidate B — doctor false-green, introduced ad56c07, fixed 316781e (both shipped v3.29.0, same day 2026-07-25)

- `ad56c07` 04:43 "feat(doctor): measure whether the store can still be found, not just stored" — added `retrieval_discrimination`; shipped with a skip that short-circuited to `ok` whenever an embeddings package *imported* under hybrid/semantic mode.
- `316781e` 13:47 "fix(doctor): an importable extra is not a semantic leg" — under the shipped default (`hybrid` + `semantic_dedup = false`) the factory returns None and ranking is purely lexical no matter what is installed, so the old skip reported `ok` "for precisely the store that most needs the warning". Also: the check's own `fix_hint` was "actively misleading" and rewritten. Files: `doctor.py`, `semantic_setup.py` (new `_semantic_rank_leg_active`), `tests/test_doctor.py`, CHANGELOG.
- Regression test: `test_retrieval_discrimination_does_not_skip_on_a_merely_installed_extra`, `tests/test_doctor.py:1394` (both extras patched importable, default config, must still warn AND have run its probe); check impl `_check_retrieval_discrimination` imported at `tests/test_doctor.py:49`.
- CHANGELOG record: `## 3.29.0 - 2026-07-26` (`:365`), retrieval_discrimination bullet `:687-719` (the false-green fix is the "Reported, never auto-fixed. It skips only when a semantic leg would *actually score a search*" bullet, `:702-714`); the measurement table that motivated the related default flip at `:734-742`. Root-cause taxonomy fit is poor (it is a doctor-check defect, not a verdict/drift signal) — "Threshold rule" is the nearest bullet.

### Candidate C — ingest `--force` regression from 0073c70 (UNRELEASED, live at HEAD)

- `0073c70` 2026-07-30 "feat(write): shared write-gate chain; ingest now runs the content gates". `apply_ingest_plan` now runs `CONTENT_GATES` (= `_WRITE_GATES` minus `PendingGate`, `handlers/write.py:707-708`) via `apply_write_gates` with a `GateContext` whose `force=False` is hardcoded (`ingest.py:514-533`, the literal at `:526`).
- The regression: plan-time `--force` (CLI flag `cli/ingest.py:62`, threaded to `compute_ingest_plan(force=...)` `:96,172`; semantics "bypasses the active-store dedup gate … tombstone dedup is always honoured", `ingest.py:406-407`) admits the row as `write` — then apply-time `DedupActiveGate` (skips only on `gc.force`, `handlers/write.py:459-460`) re-rejects it. **Reproduced live at HEAD**: forced plan row `write` → after apply `skip_invalid`, reason `write gate refused: duplicate — … Pass force=True if the new memory is meaningfully different.` (the hint tells the operator to pass the flag they already passed), `written_id=None`.
- CI is green because the force tests stop at the plan: `test_force_bypasses_active_dedup_but_not_tombstones` asserts only `plan_force.rows[0].action == "write"` and never applies it (`tests/test_ingest.py:573-610`); `apply_ingest_plan` has no `force` parameter at all (`ingest.py:551-558`).
- Not acknowledged anywhere: the Unreleased CHANGELOG entry (`CHANGELOG.md:36-55`) documents the gate chain and the per-row `skip_invalid` reporting but never mentions `--force`. This candidate's postmortem can be written entirely from `ingest.py`, `handlers/write.py`, `tests/test_ingest.py`, and the commit — but the *fix* does not exist yet; "Fixed in" would be "open" unless the plan also schedules the fix (e.g. thread plan-level force into the `GateContext` for the two dedup gates only — note the deliberate asymmetry that tombstone dedup must stay, which `DedupTombstoneGate.evaluate`'s `gc.force` skip at `handlers/write.py:505-506` would violate if force were threaded naively).

## 5. ROADMAP, release runbook, CHANGELOG conventions

**`docs/ROADMAP.md` at HEAD — full item list** (needed for supersession marking):
- Planned (`:6-79`): 1. Write-path hardening, remaining items (`:8-24`) — sub-items: reconcile private gate copies in `consolidate._apply_llm_proposal` + `handlers/proposals.accept_proposal` (policy review); provenance on the read surface (after design change re injection-driven writes); `sync pull` trust boundary. 2. Standing tier (`:25-32`). 3. Claims-at-write (`:33-40`). 4. Event-time on the memory record (`:41-48`). 5. Encryption at rest (`:49-51`). 6. Relevance-label v2 default flip — w2 dropped; successor rule needs a rule-signature change + per-memory mutation index; explicitly warns the exclusion is NOT a session-id join (`:52-79`).
- Not planned (`:81-99`): managed cloud SKU; team-shared store/RBAC; knowledge-graph backend; non-MCP SDK/REST; removing `verified_commits`/`verified_versions` in 3.x; gating low-use episode tools out of the lean surface (`:95-99`, resolved by the DESC trim).
- Contributing (`:101-113`).
- ROADMAP is in `_PLAN_DOCS` (path claims exempt) but its line-refs/symbols/counts are checked.

**`docs/release.md`** current state: tag-push driven (`release.yml` → build, gate, PyPI trusted publishing, GitHub release) (`:3`); **seven fields across six files** table (`:35-47`): pyproject.toml, plugin/.claude-plugin/plugin.json, .claude-plugin/marketplace.json, server.json (×2), uv.lock, CHANGELOG heading; step list `:49-97` (edit by hand, `uv lock`, `pytest tests/test_plugin.py tests/test_version.py tests/test_changelog.py`, commit, tag, **re-run test_changelog.py between tag and push** `:88-96`); release-window coverage check `test_newest_tag_window_commits_are_represented` with the three representation tiers (SHA citation / shared bigram / near-total unigram) and trivial-type exemptions (docs, style, test, chore, ci, build, bench, release) (`:120-130`; impl `tests/test_changelog.py:488`, exemptions `:259-266`).

**CHANGELOG correction conventions**: released sections are frozen prose; corrections land as an appended `### Erratum (YYYY-MM-DD)` subsection inside the released section, opening in the register "The entry above is left as it shipped." then stating what should have been there (precedents: `CHANGELOG.md:1163`, `:1430` (2026-07-24), `:1728`, `:1815`, `:1963` (2026-07-19); convention established with 3.24.0/`096218e`). Claims false at ship are "qualified rather than rewritten" (`:1025-1028`, `d321646`). The register is named in the current Unreleased section as "correct forward, don't rewrite the record" citing `0fdf436` (`:71-73`). No test mechanically freezes released sections — but `test_doc_claims` checks **path and symbol claims changelog-wide** (`:49-55`), with 3 standing allowlist entries all keyed to `CHANGELOG.md` (`:524-552`), so a new erratum must keep its backticked paths/symbols true at HEAD or take an allowlist entry.

## 6. Eval publishing surfaces — recomputable vs frozen

`docs/eval-results.md` splits explicitly (`:3-7`):

**Recomputable via `bettermemory eval` — but only against the author's live log** (the raw event log "is personal and stays local", `:14-15`):
- The trio table (`:28-32`), scan-detail line (`:34-36`), per-model table (`:73-77`), threshold-sweep table (`:81-89`), tool-usage table + totals line (`:93-107`) — all emitted by `bettermemory eval --report` (`:17-19`; renderer `eval.py` §`render_report`, e.g. sweep `:3059-3087`, tool usage `:3089-3115`, footer "Generated by `bettermemory eval --report` v…" `:3121-3123`). Sub-commands: `eval --since 30d`, `--threshold-sweep` (`:162-165`), plus `--widening-preview` (exists in CLI, `cli/eval.py:93`; not mentioned in the doc's Reproduce section).
- Regenerating **moves every number and the "measured 2026-07-16" date** (`:13`): store size (134 → current), event counts, the 2-miss narrative (`:54-66`), and "25 known tools" → 27.

**Frozen measurements (hand-authored prose or committed artifacts; not regenerated by the CLI):**
- All "Reading it honestly" bullets (`:38-69`) including the 45%→32%→0%→3% message-length artifact (backed by `docs/eval.md:85`'s 195-turn/185-memory study) and the stale `top_hits` parenthetical (`:64-66`).
- The whole Comparative harness section (`:109-158`): pinned to `tests/eval/comparative.py --live`, run 2026-07-03, artifact `docs/eval/comparative-live-2026-07-03.json` (`:111-113`); the capability matrix's bettermemory row is pinned at version **3.13.0** (`:120`); re-running requires the maintainer live lane `tests/eval/run_live.sh` (~2 GB model download, Node 20+, `:167-172`).

## PLAN HAZARDS

1. **The plan doc's own filename defeats the exemption it assumes.** `_PLAN_DOCS` is a literal frozenset (`test_doc_claims.py:237-243`), not a pattern; `docs/upgrade-plan-2026-07-30.md` is not in it and does not even match the docstring's `*-plan.md` shape. Every backticked, prefix-anchored, suffix-matching path to a *proposed* file fails `check_paths` the moment the doc lands in `docs/`. Either add the exact path to the frozenset in the same commit, name the file `…-plan.md` AND still add it (the pattern is prose, not code), or write proposed paths in one of the non-claim spellings (§1). Blind executors that create the doc first and the code later will have a red CI window in between.

2. **`_PLAN_DOCS` exempts paths ONLY.** A plan doc's symbol claims (`` `sym` in `existing.py` `` for a symbol that doesn't exist yet), total-marked test counts against **not-yet-existing** test files (`check_test_counts` fails on the missing file, `:917-920` — no plan exemption), line-refs, and file-counts are all still checked. Phrase future work as "N new tests"; never use the `` `sym` in `mod.py` `` construction for proposed symbols in existing modules.

3. **Line-refs in the plan rot as the executor edits the cited files.** The plan cites `handlers/search.py:109-111` etc.; once the fix lands, those citations drift and `check_line_refs`' anchor check can go red on the plan itself (the doc is a living doc). Commit-pin every citing paragraph (`` resolved against `95af021` ``, backticked hex mandatory, paragraph-scoped, does not cross blank lines — `test_doc_claims.py:429-433, 991-1004, 1913-1926`) or the executor must re-touch the plan on every code move. Corollary: keep live present-tense claims out of pinned paragraphs (`:156-160` — the disclosed blind spot).

4. **Fixing the 10→65 number in one place creates contradictions in four others.** The same measurement family is restated in `handlers/search.py:16-30` (module docstring, incl. 90% ceiling arm), `tests/test_server.py:6009-6028` (guard-test docstring), `config.py:70-72`, `semantic_setup.py:114-116`, `doctor.py:2299-2300`, `tests/test_prompts.py:552-554`, `tests/test_semantic.py:423-430` — plus the 185-vs-190 store-size inconsistency that `bench/retrieval/README.md:19-21` already calls out is still live at HEAD across those sites. None are CI-checked (percentages are invisible to test_doc_claims), so a partial fix ships silently. Distinguish measurements that must stay (CHANGELOG history; the 195-turn silent-miss study in `search.py:1059` / `docs/eval.md:85` is a *different* measurement) from ones being superseded.

5. **There is no committed artifact backing 10%→65%.** If the A5 guard pins DESC numbers against `bench/*/results/*.json`, the current DESC value cannot pass — the artifacts say 35→80 (v2) / 40→95 (v1), and the bench README forbids treating them as comparable to the original figures (`bench/retrieval/README.md:174-175`). The guard design must pick one: rewrite the DESC to artifact-backed numbers (then the guard is a straight equality against `v2-unpadded-2026-07-26.json` `results[]`), or keep historical numbers under an allowlist entry with reason — a guard born with its only measured claim allowlisted protects nothing (the disable-risk bias `test_doc_claims.py:10-14` warns about).

6. **A naive "N%"/"N KB" DESC sweep false-positives on 5 of its 6 hits.** Only `DESC_MEMORY_SEARCH` carries a measurement; the other numeric DESCs are enforced contract constants (500-char caps, 64 KB frontmatter ceiling, 30% groundedness threshold, 2x weighting). Anchor the pattern on "measured" (or maintain a checked-constant map), or the guard gets disabled — the exact failure mode test_doc_claims documents.

7. **DESC edits are budget-coupled, not free.** Lean desc total is 26,336 of the 27,500 ceiling (measured live; `tests/test_server.py:5804`) — 1,164 chars of slack, with a pressure warning at 27,400. A longer artifact-cited sentence fits, but ceiling raises are governed by the two-rule recalibration policy in `test_default_on_descriptions_fit_budget`'s docstring (`:5882-5903`): never to re-admit policy, and only with `_DESC_BASELINE` re-measured in the same commit. `_DESC_BASELINE` is already stale for `memory_search` (3467 recorded vs 3389 actual) — harmless (diagnostic-only) but will confuse an executor that treats it as asserted.

8. **Tool-count claims are triple-guarded; a plan that adds/removes/gates any tool must move seven surfaces atomically**: `_LEAN_COUNT`/`_FULL_COUNT` (`test_tool_surface.py:66-67`), `_EXPECTED_TOOL_COUNT` (`test_eval.py:1601`), the eval enumeration (`_TOOL_EVENT_KIND_TO_TOOL` + `TOOLS_WITHOUT_TELEMETRY`, set-equality asserted `test_eval.py:1639`), and any "N tools" phrasing in the six `_TOOL_COUNT_PROSE_FILES` (`test_eval.py:1659-1666`). Conversely, phrasings with an interposed word ("18 default tools", "25 known tools", `test_server.py:5780`'s stale "24-tool power-user surface", `handlers/search.py:3`'s stale "the 25 tools") are UNGUARDED — a truth-sync that normalizes them into "N tools" phrasing inside the six files suddenly makes them guarded; outside those files they stay silent rot.

9. **eval-results regeneration is all-or-nothing per section.** The tables are `--report` output but the analysis bullets (`:38-69`) are hand-authored against the 2026-07-16 snapshot (2 silent misses, 15 v1-flagged, per-model counts). Re-running `--report` at HEAD to fix "25 known tools" refreshes *every* number and the measured date, desynchronizing the prose narrative — including the "first silent misses" story — unless the prose is rewritten in the same pass. Fixing only lines 64-66 and 106 by hand keeps the frozen snapshot coherent; mixing strategies breaks it. Comparative-section numbers (bettermemory 3.13.0 row) are NOT recomputable by `bettermemory eval` at all — only by the maintainer live lane.

10. **The incidents workstream contradicts the directory's charter.** `docs/incidents/README.md:3` scopes the directory to *reported* memory-rot cases where the verification surface missed; all three candidates are self-found, and candidate B (doctor false-green) and C (ingest --force) are not memory-rot at all under the template's four-signal root-cause taxonomy (`TEMPLATE.md:14-19`). Writing them in without widening README.md's charter (and the "Reported by:" field semantics) produces documents that contradict their own directory's index prose. Also: the index at `README.md:25-27` must be updated (reverse-chronological) — the "(No incidents yet…)" placeholder is itself a claim that becomes false.

11. **Candidate C has no fix at HEAD.** The ingest `--force` regression is live (reproduced: forced row → `skip_invalid` "write gate refused: duplicate", with a hint telling the user to pass the flag they passed) and invisible to CI (`tests/test_ingest.py:573-610` never applies a forced plan; `apply_ingest_plan` takes no `force`). A postmortem with "Fixed in: vX.Y.Z" cannot be written until the plan also lands the fix + an apply-level regression test; and threading force through the gate context naively re-opens tombstone resurrection (`DedupTombstoneGate` skips on `gc.force`, `handlers/write.py:505-506`), violating the pinned asymmetry (`test_force_does_not_resurrect_tombstoned_memory`, `tests/test_ingest.py:612-642`).

12. **CHANGELOG corrections to released sections must follow the erratum register and survive two mechanical checks**: doc-claims' changelog-wide path/symbol scan (a new erratum citing a since-renamed path needs an allowlist entry with the frozen-history reason shape, `test_doc_claims.py:524-552`), and — if the correction commits land in a tagged window later — the release-window coverage tiers (`tests/test_changelog.py:488`; SHA citation is the deterministic tier for grouped bullets). Editing shipped release text in place has an explicit anti-precedent ("qualified rather than rewritten", `CHANGELOG.md:1025-1028`).

13. **The doc-claims corpus boundary cuts both ways.** New guard/incident files under `docs/incidents/` or `docs/eval/` escape all five checkers (non-recursive glob, `test_doc_claims.py:602`) — nothing polices their citations; but any new `.py` file's *docstrings* enter the corpus automatically via tracked-files discovery (`:629-654`, `:711-732`) with the no-allowlist-for-docstrings rule enforced (`test_no_allowlist_entry_covers_a_docstring_source`, `:1212-1226`) — a new guard module's docstring may not need an exemption, ever, and synthetic false examples must live in code/comments, never docstrings (`:94-108`).