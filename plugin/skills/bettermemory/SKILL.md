---
name: bettermemory
description: Verification-grade memory between sessions. Use bettermemory's MCP tools (memory_search, memory_show, memory_write, memory_verify, memory_record_use, etc.) instead of writing to files when the user asks you to "remember" something or references shared context from a past session. Default is to NOT call memory_search; only retrieve when the user references context you don't have ("my project", "the script we wrote") or a request is ambiguous in a way stored preferences could resolve. Every hit carries a staleness_verdict (calendar + path-drift + commit-drift); when it isn't "fresh", spot-check a claim before relying and call memory_verify to attest. Every use should record a claim_excerpt so retrievals stay auditable.
---

# bettermemory: opt-in memory retrieval

Persistent memory between sessions lives in this plugin's MCP tools. **Do not fragment memory across ad-hoc files alongside** (`MEMORY.md`, scratch markdown elsewhere) — future sessions only see what these tools surface. If Claude Code's auto-memory at `~/.claude/projects/*/memory/` already exists from before bettermemory was installed, ingest it (one-shot `bettermemory ingest --from <path>`) rather than letting it accumulate alongside; the ingest CLI maps each auto-memory file to a bettermemory record, dedups against the active store and tombstone log, and stamps an `imported-from-claude-code` scope for traceability.

This skill is the long-form companion to the MCP server's `instructions` block: Claude Code truncates that block at ~1.8 KB, so the full writing-discipline / scope-hygiene / confirmation-tier policy lives here. Non-plugin clients get the same content via [`docs/system_prompt.md`](../../../docs/system_prompt.md).

## Quick card

| Decide | Rule |
|---|---|
| Search? | shared-context reference or ambiguity → yes. Otherwise no. |
| Write? | proactive — something durable just entered the conversation → yes. Don't wait for *"remember that"*. State or timestamps → no (the tool will reject). |
| Category? | claim about the user → `user-inference`. Atmospheric / no verifiable claims → `ambient`. Else → `fact`. |
| Outcome? | retrieval shaped reply → silence (auto-commits as `applied`). Off-topic or wrong → explicit `ignored` / `contradicted` / `corrected`. |
| Verify? | `staleness_verdict != "fresh"` → `path_drift.claim_anchored_missing` on the hit is the subset that moved it; `memory_update` those, `memory_verify` the rest with `verified_paths`. |
| Scope? | project name if obvious; never `general`. |

Detail on each tool lives in the tool's own description — this skill is the policy.

The plugin's `.mcp.json` launches the server with no config overrides, and `load_config()` defaults `[behavior] full_tool_surface` to false. So a plugin install registers the **lean** surface: the curation and power-user tools are absent until the user sets `full_tool_surface = true` in `config.toml`. Tools this skill names that only exist under that flag are called out as **full-surface** where they appear; where a lean install still needs the capability, the `bettermemory` CLI is the route that is always there.

## When to retrieve

Memory is **OPT-IN retrieval**. Stored memories are NOT in your context unless you call `memory_search` — with one narrow exception: the prompt-recall hook may inject a single id + snippet pointer when a stored memory scores high for the submitted prompt (~2% of turns). Treat an injected pointer as a lead, not a body: `memory_show` it before relying on it, and the transparency rule applies unchanged. **Default to not retrieving.** False positives (irrelevant context cascading through a conversation) are much worse than false negatives (one followup turn).

Call `memory_search` only when:

- the user references shared context you don't have (*"my project"*, *"the script we wrote"*, *"do you remember…"*)
- a request is ambiguous in a way stored preferences could resolve

Skip it for generic factual questions, self-contained technical questions, and fully-specified messages with no ambiguity.

### Session-start hint

One call to `memory_scope_overview` returns per-scope counts plus a `curation_pending` rollup (`{stale, never_verified, drifted, cold, dead, silent_misses, unique_silent_miss_memories, cold_endorsement_memories, conflicts}`: integer counts only). If `total=0`, skip `memory_search` for the rest of the session unless asked. Non-zero `dead`, `drifted`, or `conflicts` is the cue to suggest a curation pass when the conversation has time (`conflicts` = memory-vs-memory contradiction pairs awaiting a `memory_conflicts` verdict — a full-surface tool, so on a lean install the pass runs through `bettermemory health` and the CLI instead). Use this once per conversation; it's a yes/no signal, not something to poll.

### Auto-scoping

`memory_search` defaults to filtering by the caller's current repo + worktree. Memories from a different repo are filtered out. Legacy memories with no `origin` are global and always pass. Set `auto_scope=False` for explicit cross-project queries.

## Transparency requirement

When you retrieve and use a memory, briefly say so:

> *"Using your stored preference for code-driven tutorials…"*

Non-negotiable. The user needs to know when stored context shaped a reply.

## Recording use

Every `memory_search` hit and `memory_show` response carries an opaque `use_token`. **Unless you call `memory_record_use`, the retrieval settles as `outcome="applied"` automatically at turn end** — the Stop hook records reply-matched hits with excerpts (`attribution="hook"`) and the rest as the plain auto-fallback (`auto=true`); hookless setups settle on a later `memory_*` call. The common case handles itself.

Call `memory_record_use(memory_ids=[…], outcome=…)` explicitly only to override:

- `"ignored"`: retrieved but off-topic.
- `"contradicted"`: user or current state contradicted the stored fact AND you haven't fixed it yet. Raises the unresolved-contradiction flag until a later `memory_update` or `memory_verify` clears it.
- `"corrected"`: memory had drifted and you fixed it inline (same turn `memory_update` and/or `memory_verify`). Audit-only; does NOT raise the contradiction flag. Use this instead of `contradicted` when the resolution is already done.

The explicit override wins via override semantics — the server purges the pending token before recording.

Outcomes are not just audit rows. Under the usage-aware ranking flags (`[behavior] endorsement_boost` / `outcome_demotion`, both opt-in), an explicit `applied` nudges the memory up on later searches and an active `ignored`/`contradicted` slides it down — all bounded near-tie breakers, cleared by a later genuine `applied` or by `memory_update`/`memory_verify`. Record the outcome that actually occurred; the ranking flags read these rows.

### Claim-level provenance

When the memory shaped a user-visible sentence, pass the load-bearing phrase as `claim_excerpts` (parallel to `memory_ids`, one entry per id, `None` for "no specific claim"). Especially useful on `contradicted` / `corrected` so the audit log records *which* claim was wrong. Excerpts are quotes (max 500 chars), not whole bodies.

## Verify before relying

Memory is a snapshot; it does not auto-refresh. Every retrieval carries a derived `staleness_verdict`. Only CLAIM-ANCHORED drift moves it: a path you attested via `verified_paths`, a citation resolved against the memory's own recorded worktree, or a commit touching what the body cites. A path-shaped token scraped out of prose still ships as evidence but no longer raises a tier. The three tiers:

- `"fresh"`: verification fresh AND no claim-anchored drift. Body claims are presumed current. A hit can read `"fresh"` while carrying entries in `path_drift.missing` — those are the prose-scraped half, evidence you judge rather than a tier you act on.
- `"spot_check_recommended"`: verification calendar-fresh but the world has moved — an anchored path went missing, or the repo has commits since the last verify touching what the body cites. Quick check before relying.
- `"spot_check_required"`: the verification anchor is missing (`"never"`), or it is expired (`"stale"`) and no measurement is available to stand the calendar leg down. A `"stale"` memory whose commit-drift leg actually ran and returned zero reads `"fresh"` instead — the measurement wins, the calendar proxy yields. The leg returning `None` (it could not ask) is not a measurement and does not demote.

When the verdict isn't `"fresh"`, the hit already carries the actionable detail. `path_drift.claim_anchored_missing` (added to a search hit only when non-empty; carried on every non-null `memory_show` report, empty list included) is the subset that moved the verdict — `memory_update` those directly, no memory_show round-trip needed. It is always a subset of `path_drift.missing`, whose remaining entries are the prose-scraped absences: real evidence (a path the body names is gone), but weigh them rather than treating them as a tier. The remaining un-drifted claims (`path_drift.verified` plus the rest of the body) can be attested with `memory_verify(id, verified_paths=[…])`; paths are the attestation the drift legs read back, both against the memory's own worktree and as the anchor narrowing the commit-drift count. `verified_commits` and `verified_versions` are recorded for the audit trail and echoed back, but nothing on the read path resolves them — a commit attestation is provenance for the next reader, not a signal. `memory_update` resets `last_verified_at`, so verify again after fixing drifted prose to close the loop.

## When to write

Writing is **PROACTIVE** — `memory_write` is a routine reflex. Reach for it whenever something durable enters the conversation. Don't wait for *"remember that"*; by then the user is paying you to forget.

Triggers:

- User states a preference or convention → `category="user-inference"` (server stages pending; ask before confirming).
- Project decision the user concurred with → `category="fact"` (commits immediately; announce the save in one line).
- Tool / infrastructure / configuration fact (env vars, ports, paths, versions, topology) → `category="fact"`.
- A unit of work finishes whose what-and-why isn't captured by git or CHANGELOG → `category="fact"`.

The structural guardrails (durability check, dedup, user-inference pending tier, scope-mismatch check) do the policing. Aggressive writing is safe — write the fact, let the guardrails fire if it's wrong-shaped, fix it, re-write.

### Refining vs creating

Refining or correcting a stored fact → `memory_update(id, …)`, not `memory_remove` + `memory_write`. That preserves `created`. The `links` parameter sets typed inter-memory edges (`supersedes` / `contradicts` / `extends` / `depends_on`) with REPLACE semantics — pass the full list, or `[]` to clear.

### Optional groundedness gate

When the write captures a claim from the current conversation, opt into the structural check:

```text
memory_write(
    content="The user prefers terse code-driven explanations.",
    scopes=["learning-style"],
    groundedness_check=True,
    source_transcript="user: I want terse code-driven explanations, no prose.",
)
```

Sentences whose content tokens overlap the transcript by less than 30% come back as `{status: "ungrounded", claims: [...]}`. Override via `acknowledge_ungrounded=True` when you have other grounding sources (file reads, tool results) not in the transcript. Off by default — opt in when you want a paper trail.

## Episodes: the sibling tier for run-state

Episodes are a **sibling primitive to memory**, not a tier of it. They're the home for journal-shaped writes the durability gate explicitly rejects: loop-iteration takeaways, "what we tried", run-local context that needs to survive one context reset but isn't a durable fact.

Use episodes when `memory_write` would reject (or should reject) your content as transient:

- *"iteration N tried X, fell over at step 3"* → `episode_write`
- *"currently blocked on Y; next step is Z"* → `episode_write`
- *"this branch's release plan"* (state that changes weekly) → `episode_write`

Storage layout: `<root>/episodes/<session_id>/<ulid>.md`. Default 30-day TTL, pruned on each write. Episode *content* is **invisible to `memory_search` / `memory_health` / `memory_list`** — they live in a sibling subtree the memory iterators never see. The one thing that crosses over is aggregate size: `memory_health` (full-surface only; `bettermemory health` otherwise) returns `episode_volume` (`{sessions, episodes, bytes, prunable_sessions, ttl_days}`), a stat-only gauge. Check `prunable_sessions` if a long read-only loop has been running — pruning happens on `episode_write` and `bettermemory episodes prune`, so a loop that only reads never collects.

### The state channel: write state here, mint facts at close

Treat this as the routing rule, not one option among several. **Working state goes to `episode_write` while the run is in flight; at session close, the takeaways that hardened go through `episode_promote`.**

Without the rule, here is what happens instead, and it is not hypothetical — it is the dominant shape in a mature store. `memory_write`'s durability gate rejects state-shaped content. But rejecting it does not remove your need to write down where you are in a long run, so the state gets rephrased until it clears the gate and lands in the durable store dressed as a fact. Nothing ages it out, and every later `memory_search` steps over it.

Promoting at close is also simply a better write:

- The claim **survived its own session**. That is most of what the durability gate is trying to guess at from one sentence, and you get it for free by waiting.
- You can see the **whole session's takeaways at once**, so you promote one consolidated claim instead of the three in-flight fragments that would have hit dedup as separate near-duplicates.
- What did not harden costs nothing to abandon — it expires on the TTL instead of becoming curation debt.

The mirror-image rule matters as much: content that is *still* run-state at close does **not** get promoted "to be safe". Leaving it in the journal is the point.

### Loop-iteration pattern

A `/loop` iteration (or any agent resuming work in a worktree) should:

1. **At entry**: call `episode_handoff()`. Returns the prior session's recent takeaways with `{prior_session_id, episodes: [{id, created, takeaway, body, scopes}, ...]}`. Distinguish `prior_session_id is None` (no baseline) from `episodes == []` (prior session left no journal).
2. **Each iteration**: call `episode_write(body=..., takeaway="one-line summary")`. The takeaway is what the next iteration sees first. This is where run-state lives — no judgement call needed, write it every time.
3. **At session close**: scan what this session concluded, then promote the survivors:

```python
episode_search(parent_session_id=<this session id>, include_bodies=False)  # cheap takeaway-only scan
episode_promote(episode_id="01K...", scopes=["projects:bettermemory"])     # only the ones that hardened
```

Step 3's promote is a **filter, not a loop** over the scan — a twelve-takeaway session typically promotes zero or one. `episode_promote` routes through `memory_write`, so a promotion that should not have happened still meets the durability gate, dedup and (for `user-inference`) the confirmation flow; the source episode is deleted on commit and left in place on any rejection, so guessing wrong costs a status code rather than the work.

`memory_search(since_prior_session=True)` is the memory-tier companion: filter the durable memory store to entries `updated` since the prior session boundary. The semantic is "what THIS session has changed since the last other-session activity" — your own intra-session diff. For what the *prior* iteration did, use `episode_handoff` instead.

### Subagent handoff

When a parent agent spawns a subagent, pass the parent's `session_id` along; the subagent calls `episode_handoff(prior_session_id=<parent>)` to inherit context. The handler skips the event-log walk when the id is explicit.

## Negative-results suppression

A `memory_search` hit whose memory was `ignored` or `contradicted` in the last 30 days AND not since `applied` carries `recent_negative_outcomes`. The user already rejected this recently. Don't re-surface unless you have new reason to think the rejection no longer applies. The `claim_excerpt` field (when present) lets you rephrase or skip just the offending sentence rather than the whole body.

## Duplicates are evidence, not waste

A `memory_write` that comes back `status="duplicate"` credits the matched memory a **corroboration** (`corroboration_recorded: true`, once per session): the claim re-entered a conversation, which is evidence it still holds. The rollup keeps corroborated memories out of dead-weight curation and (under `[behavior] corroboration_boost`) nudges them up near-ties. So don't force-write around dedup out of capture anxiety — the rejection already landed the signal. `force=True` remains for claims that are genuinely different.

## Corpus curation: conflicts and cross-session patterns

Two full-surface tools drain what no single conversation can see:

- `memory_conflicts` — the server mechanically flags pairs of stored memories that likely disagree (near-identical bodies with a negation flip or a numeric divergence like "port 5432" vs "5433"); you judge each pair. `verdict="contradiction"` writes a `contradicts` link both sides surface at retrieval; follow up with `memory_verify` on the correct side and `memory_update`/`memory_remove` on the wrong one. `verdict="compatible"` dismisses (sticky until either body changes). Scans run automatically on applying curation passes; `scan=True` forces one.
- `episode_patterns` — themes recurring across ≥3 distinct sessions' episodes, the consolidation `episode_promote` can't see. YOU author the promoted body (the listed `terms` are evidence pointers, not a synthesis); the write runs the full `memory_write` gate stack and a `duplicate` outcome still corroborates the existing memory. Dismiss patterns that are vocabulary coincidence — a new member episode legitimately reopens them.

## Scopes

Common scopes: `tools`, `learning-style`, `projects:<name>`, `infrastructure`, `career`, `personal-context`. Avoid the catch-all `general`.

If the user says *"this is unrelated to project X"*, call `memory_scope_disable("projects:X")` for the rest of the session.

`memory_health.rare_scopes` surfaces typo singletons; fix via `memory_rename_scope(old, new)` — both are full-surface tools, so a lean install reads the bucket from `bettermemory health` and renames with `bettermemory rename-scope`.
