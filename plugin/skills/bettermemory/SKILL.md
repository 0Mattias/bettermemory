---
name: bettermemory
description: Persistent memory between sessions, retrieved on demand. Use bettermemory's MCP tools (memory_search, memory_show, memory_write, etc.) instead of writing to files when the user asks you to "remember" something or references shared context from a past session. Default is to NOT call memory_search; only retrieve when the user references context you don't have ("my project", "the script we wrote") or a request is ambiguous in a way stored preferences could resolve.
---

# bettermemory: opt-in memory retrieval

Persistent memory between sessions lives in this plugin's MCP tools. **Do not fragment memory across ad-hoc files alongside** (`MEMORY.md`, `~/.claude/projects/*/memory/`, scratch markdown elsewhere). Future sessions only see what these tools surface, so anything stored elsewhere is invisible to next-week's-you.

## Quick card

| Decide | Rule |
|---|---|
| Search? | shared-context reference or ambiguity, then yes. Otherwise no. |
| Write? | proactive — something durable just entered the conversation, then yes. Don't wait for *"remember that"*. State or timestamps, then no (the tool will reject). |
| Category? | claim about the user, then `user-inference`. Atmospheric or no verifiable claims, then `ambient`. Else, `fact`. |
| Outcome? | retrieval shaped reply, then silence (auto-commits as `applied`). Off-topic or wrong, then explicit `ignored` / `contradicted` / `corrected`. |
| Verify? | `staleness_verdict != "fresh"`, then spot-check one claim before relying; pass `verified_paths` to `memory_verify`. |
| Scope? | project name if obvious; never `general`. |

The rest of this document is reference for when one of those answers needs more context.

## Available tools

- **Retrieval**: `memory_search` (with optional `mode` parameter: `keyword` default, `bm25`, `semantic`, or `hybrid`), `memory_show`, `memory_list`, `memory_scope_overview`
- **Writing**: `memory_write` (with optional `groundedness_check` + `source_transcript` for the HaluMem-style write-time gate), `memory_update` (now accepts typed `links` for inter-memory edges), plus `memory_write_confirm` and `memory_write_cancel` for the staged-write flow
- **Lifecycle**: `memory_remove`, `memory_restore`, `memory_list_tombstones`
- **Verification**: `memory_verify`
- **Curation**: `memory_record_use` (with optional `claim_excerpts` for claim-level provenance), `memory_health`, `memory_rename_scope`
- **Session-local**: `memory_scope_disable`, `memory_scope_enable`

## When to retrieve

Memory is **OPT-IN retrieval**. The user's stored memories are NOT in your context unless you call `memory_search`. **Default to not retrieving.** False positives (irrelevant stored context cascading through a whole conversation) are much worse than false negatives (missing context the user can supply in one followup turn).

Call `memory_search` ONLY when:

- the user references shared context you do not have (*"my project"*, *"the script we wrote"*, *"do you remember…"*)
- a request is ambiguous in a way stored preferences could resolve

Skip it for:

- generic factual questions (*"what is the capital of France"*)
- self-contained technical questions (*"how do I write a Python list comprehension"*)
- fully-specified messages with no ambiguity

### Session-start hint

One call to `memory_scope_overview` returns scope counts plus a `curation_pending` rollup (`{stale, never_verified, drifted, cold, dead}`: integer counts only, no row materialisation). If `total=0`, skip `memory_search` for the rest of the session unless the user explicitly asks. If counts are non-zero, `memory_search` is the way to retrieve content. The `curation_pending` rollup tells you whether the store has anything worth a curation pass without paying the `memory_health` cost. Non-zero `dead` or `drifted` is the cue to suggest one when the conversation has time. Use this once per conversation; it is a yes/no signal, not something to poll.

### Auto-scoping

`memory_search` is auto-scoped to the caller's current repository by default (`auto_scope=True`). Memories written from a different repo are filtered out automatically. Memories with no recorded origin (legacy entries, or writes from outside any repo) are treated as global and always pass. Set `auto_scope=False` only for explicit cross-project queries (*"anything I have stored about X across all my projects"*).

## Transparency requirement

When you do retrieve and use a memory, **briefly say so** in your response:

> *"Using your stored preference for code-driven tutorials…"*

This is non-negotiable. The user needs to know when stored context shaped a reply.

## Recording use (auto-commit and override)

Every `memory_search` hit and `memory_show` response carries an opaque `use_token`. **If you do not call `memory_record_use` within roughly 2 turns, the server auto-commits the retrieval as `outcome="applied"`** on the next `memory_*` call (logged with `auto=true` for audit). Bookkeeping for the most-forgotten step is opt-out instead of opt-in. The common case (retrieval shaped the response, you would record `applied`) handles itself.

Call `memory_record_use(memory_ids=[…], outcome=…)` explicitly only when you want to override the auto-commit:

- `"ignored"`: retrieved but turned out off-topic.
- `"contradicted"`: the user or current state contradicted the stored fact AND you have not fixed it yet. Raises the unresolved-contradiction flag in `memory_health` until a later `memory_update` or `memory_verify` clears it.
- `"corrected"`: the memory had drifted and you fixed it inline (called `memory_update` and/or `memory_verify` in the same turn). Audit-only; does NOT raise the contradiction flag. Use this instead of `"contradicted"` when the resolution is already done.

The explicit override wins via override semantics: the server purges the pending token before recording the explicit outcome, so the auto-commit cannot shadow it. `"applied"` can also be passed explicitly when you want the audit trail; it just happens to also be the default.

## Verify before relying

Memory is a snapshot; it does not auto-refresh. Every retrieval carries a derived `staleness_verdict` plus the underlying signals it is computed from. **Branch on the verdict first.** Consult the underlying signals only when you need to know which axis tripped.

`staleness_verdict` is one of:

- `"fresh"`: verification is fresh AND no path or commit drift. The body's claims are presumed current.
- `"spot_check_recommended"`: verification is calendar-fresh but the world has moved (a path went missing, or the repo has commits since the last verify). Worth a quick check before relying on the body.
- `"spot_check_required"`: `verification.status` is `"never"` or `"stale"`. Pre-empts the drift inputs because the verification anchor itself is missing or expired.

Beyond the verdict, three structured signals are available:

1. **`verification` block** (status: `"never"`, `"stale"`, or `"fresh"`) on every `memory_show`, `memory_search` hit, and `memory_list` row.
2. **`path_drift`** counts (`path_drift_checked` and `path_drift_missing` per hit, full report on `memory_show`). Filesystem paths cited in the body that no longer exist on disk. Advisory: drift can be a temporary mount or a path on a different machine. A drifted path on a never-verified memory is the highest-risk profile.
3. **`commit_drift`** counts (`commit_drift_count` per hit when the caller is in the memory's origin repo, full block on `memory_show`). Commits authored after `last_verified_at`. The load-bearing case: `verification.status == "fresh"` only proves the calendar is fresh. A non-zero `commit_drift` is the cue to spot-check anyway, because the project moved since the last `memory_verify`.

When the verdict is not `"fresh"`, spot-check at least one verifiable claim from the body (file path, version number, configuration) against ground truth. If the check passes, call `memory_verify(id, verified_paths=[…], verified_commits=[…], verified_versions=[…], note="…")`, passing the actual claims you spot-checked. The server uses these to short-circuit later drift signals: future retrievals of the same memory whose path_drift would have flagged a path in `verified_paths` (and the path still exists) downgrade the verdict, and `commit_drift` narrows the count to commits that actually touched any of `verified_paths`. If a claim has drifted, fix the body via `memory_update` first, then `memory_verify` the corrected version. The update resets `last_verified_at` to null because the prior verification was for prose that no longer exists.

## When to write

Writing is the opposite axis from retrieval: **PROACTIVE**. `memory_write` is a routine reflex, not a special-occasion tool — reach for it whenever something durable enters the conversation. Don't wait for the user to say *"remember that"*; by then the user is paying you to forget. If you finish a session having retrieved memory but written nothing, you missed the trigger somewhere.

Call `memory_write` when:

- the user states a preference or convention (*"I prefer X"*, *"always use Y"*) — `category="user-inference"`, the server stages pending so the user can confirm before a claim about them lands.
- a project decision is reached and the user concurred — `category="fact"`, commits immediately, announce the save in one line so the user can object (*"Saved: bettermemory env var rename to BETTERMEMORY_DIR"*).
- a tool / infrastructure / configuration fact becomes part of the work (env vars, service ports, key file locations, dependency versions, deployment topology) — `category="fact"`.
- a unit of work finishes whose what-and-why isn't captured by git or the CHANGELOG (architectural decisions, why a refactor went one way and not the other, conventions established mid-session, the reasoning behind a non-obvious choice) — `category="fact"`.

The structural guardrails do the policing for you, so aggressive writing is safe:

- **Durability check** rejects transient state (*"currently"*, *"today I"*, *"we just"*, *"the new"*, commit-SHA-like hex tokens). When it fires, the durable fact is one level up — extract the decision and the why, drop the timestamp.
- **Dedup** catches duplicates and routes you to `memory_update` on the matched id instead of letting a parallel entry land.
- **User-inference pending tier** stages every `category="user-inference"` write for confirmation regardless of config, so claims about the user can't sneak in.
- **Scope-mismatch check** forces a re-scope when the body cites a project the declared scopes don't cover.

Your job is to capture; the server's job is to gate. Write the fact, let the guardrails fire if it's wrong-shaped, fix it, re-write.

## Writing memory

**Durable only.** Memory is for facts that will still be true in a week if nobody updates them. The tool enforces this structurally: `memory_write` returns `{status:"transient_warning", markers:[…]}` instead of committing when the body contains transient markers (*"currently"*, *"today I"*, *"we just"*, *"the new"*, commit-SHA-like hex tokens). When that fires, the durable fact you actually want is one level up. Extract the architectural decision, the why, the what-was-built, and discard the timestamp or state. Pass `acknowledge_transient=True` only when the marker is genuinely durable in context (rare).

**Refining or correcting a stored fact?** Call `memory_update(id, …)` instead of `memory_remove` plus `memory_write`. That preserves the original `created` timestamp.

**Dedup is automatic at write time.** `memory_write` returns `{status:"duplicate", matches:[…]}` when the new body has high content overlap with an existing memory; the right response is `memory_update` on the matched id. Tombstone-aware dedup also runs: high overlap with a previously-removed memory returns `{status:"previously_removed", removed_matches:[…]}` carrying the original `removed_reason`. Inspect the reason. If the rejection still applies, drop the write. If the fact is now correct, call `memory_restore(id)` on the tombstone.

**Scope mismatch is also caught at write time.** If the body cites a known `projects:<name>` scope's name token (or a path under another project's tree) AND that scope is not in the declared scope list, `memory_write` returns `{status:"scope_mismatch", suggested_scopes:[…], matches:[…]}` instead of committing. Pick a suggestion and re-write, or pass `acknowledge_scope_mismatch=True` when the cross-reference is intentional (an infrastructure note that mentions multiple projects by design).

### Confirmation policy is tiered

Structurally enforced via the `category` parameter on `memory_write`:

- `category="fact"` (default): project, infrastructure, reference, or tooling facts about the world. Commits immediately; announce the save in one line so the user can object (*"Saved: bettermemory env var rename to BETTERMEMORY_DIR"*).
- `category="user-inference"`: claims *about* the user themselves (preferences, beliefs, working style). The server returns `{status:"pending", pending_id, pending_reason:"user-inference"}` instead of committing. Ask the user in plain language (*"Want me to remember that you prefer X?"*) and only then call `memory_write_confirm(pending_id)`, or `memory_write_cancel(pending_id)` if they decline. Fires regardless of the global `require_write_confirmation` config; misattribution sticks, so the user always gets the veto on claims about themselves.
- `category="ambient"`: atmospheric, response-shaping context that informs every reply without being cited (user identity, persistent environment quirks). Commits immediately like `fact`, but the dead-weight curation rule excludes ambient memories. Their value is implicit, so a count of zero `applied` events is not an indictment. Long bodies (over 500 words) attach a non-blocking `ambient_body_long` warning so you can decide whether to split.

## Scopes

Tag with appropriate scopes. Avoid the catch-all `general` scope.

Common scopes: `tools`, `learning-style`, `projects:<name>`, `infrastructure`, `career`, `personal-context`.

If the user says *"this is unrelated to project X"*, call `memory_scope_disable("projects:X")` for the rest of the session.

## Claim-level provenance (when applicable)

When recording an `applied` (or `ignored` / `contradicted` / `corrected`) outcome on a memory whose claim actually shaped a user-visible sentence in your reply, pass the load-bearing phrase as `claim_excerpts` — a list parallel to `memory_ids` with one entry per id (`None` for "no specific claim noted"). The audit log captures *which* claim was used, not just *that* one was. Especially useful on `contradicted` / `corrected` so the audit records which claim was wrong, not just that the memory had drift.

```text
memory_record_use(
    memory_ids=[mem_id],
    outcome="applied",
    claim_excerpts=["the user prefers terse code-driven explanations"],
)
```

Excerpts are quotes (max 500 chars), not whole bodies. The body is already on disk; the excerpt is the audit trail.

## Negative-results suppression

When a `memory_search` hit's memory was previously ignored or contradicted in the last 30 days AND not since applied, the hit carries `recent_negative_outcomes`. This is a strong signal: the user already rejected this memory recently. Don't surface the same claim again unless you have new reason to think the rejection no longer applies. The `claim_excerpt` field on each entry (when present) tells you not just *that* the memory was rejected but *which specific claim* — so you can rephrase or skip just the offending sentence rather than the whole body. `applied` events after a negative event clear the negative-bucket entries automatically, since the user already validated the memory after the rejection.

## Hybrid retrieval

`memory_search` accepts an optional `mode` parameter. Defaults to `keyword` (the original TF + coverage + recency scorer, byte-stable to 1.x). Other modes:

- `bm25`: Okapi BM25 with the same scope-bonus + recency. Better recall on rare-term queries.
- `semantic`: sentence-transformers cosine; requires the embeddings extra. Errors with an install hint if missing.
- `hybrid`: Reciprocal Rank Fusion over keyword + BM25 (plus semantic when the extra is installed). Gracefully degrades to keyword+BM25 fusion when no model is available.

Use `hybrid` when the query is a paraphrase of what you expect the memory to say. Stick with `keyword` when the query contains literal identifiers, file paths, or unique tokens you remember writing down. The fused hybrid score lives in a much smaller scale (~`0.01-0.05` from RRF) than single-ranker scores — compare across modes via `relevance`, not raw `score`.

## Typed inter-memory links

Memories can carry `links` of type `supersedes`, `contradicts`, `extends`, or `depends_on`, each pointing at another memory's id with an optional `note`. Set via `memory_update(id, links=[...])` — REPLACE semantics; pass the full new list, or `[]` to clear. Self-links are rejected. `memory_show` surfaces both the forward `links` on the source memory and `reverse_links` on the target (with `source_id` in place of `target_id`).

When you encounter two memories that conflict, prefer linking them via `contradicts` to recording a `contradicted` use outcome — the link is durable on disk; the use outcome is per-retrieval. Use `supersedes` when one memory definitively replaces another; the retrieval consumer can suppress the superseded one. Use `extends` when one adds nuance to another (both stay relevant). Use `depends_on` when the source only makes sense in the target's context.

## Optional: write-time groundedness gate

When a write captures a claim sourced from the current conversation, you can opt into the structural check:

```text
memory_write(
    content="The user prefers terse code-driven explanations.",
    scopes=["learning-style"],
    groundedness_check=True,
    source_transcript="user: I want terse code-driven explanations, no prose.",
)
```

The server walks the proposed body sentence-by-sentence and flags any sentence whose content tokens overlap the transcript by less than 30%. Returns `{status: "ungrounded", claims: [...]}` with the offending sentences. The override is `acknowledge_ungrounded=True`, used when you have other grounding sources (a file read, a tool result) not represented in the transcript. Off by default; opt in when you want a paper trail proving the memory came from the conversation, not from training-data confabulation.

## Scope hygiene and curation

`memory_health` aggregates over the event log and active memories. It surfaces:

- **`dead_weight`**: created before the window, retrieved at least once, never `applied`. The body is misleading enough that retrieval does not help.
- **`cold_memories`**: created before the window, never retrieved at all. Distinct from `dead_weight`: the body might be fine, but the trigger is not firing. A different curation question.
- **`heavily_used`**: frequently `applied`. Worth keeping fresh.
- **`contradicted`**: unresolved contradictions. Each carries a `resolution_timeline` (chronological log of `update`, `verify`, `contradicted`, and `corrected` events) so a stuck flag can be self-diagnosed without grepping the event log.
- **`verification_debt`** and **`commit_drift_debt`**: calendar-stale vs world-moved buckets, partitioned for batch curation.
- **`rare_scopes`**: singletons within Levenshtein distance 2 of another scope (likely typos). Fix via `memory_rename_scope(old, new)`.
- **`marker_stats`**: transient-marker fire and override counts. A high override rate means the marker is producing too many false positives and should be trimmed.

The lighter `curation_pending` rollup on `memory_scope_overview` (five integer counts, no row materialisation) is the cheap session-start triage. Reach for `memory_health` when one of those counts is non-zero and you want the actual rows.
