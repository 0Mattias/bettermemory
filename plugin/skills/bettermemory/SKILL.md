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
| Verify? | `staleness_verdict != "fresh"` → `path_drift.missing` on the hit lists what rotted; `memory_update` those, `memory_verify` the rest with `verified_paths`. |
| Scope? | project name if obvious; never `general`. |

Detail on each tool lives in the tool's own description — this skill is the policy.

## When to retrieve

Memory is **OPT-IN retrieval**. Stored memories are NOT in your context unless you call `memory_search`. **Default to not retrieving.** False positives (irrelevant context cascading through a conversation) are much worse than false negatives (one followup turn).

Call `memory_search` only when:

- the user references shared context you don't have (*"my project"*, *"the script we wrote"*, *"do you remember…"*)
- a request is ambiguous in a way stored preferences could resolve

Skip it for generic factual questions, self-contained technical questions, and fully-specified messages with no ambiguity.

### Session-start hint

One call to `memory_scope_overview` returns per-scope counts plus a `curation_pending` rollup (`{stale, never_verified, drifted, cold, dead, silent_misses, endorsement_debt}`: integer counts only). If `total=0`, skip `memory_search` for the rest of the session unless asked. Non-zero `dead` or `drifted` is the cue to suggest a curation pass when the conversation has time. Use this once per conversation; it's a yes/no signal, not something to poll.

### Auto-scoping

`memory_search` defaults to filtering by the caller's current repo + worktree. Memories from a different repo are filtered out. Legacy memories with no `origin` are global and always pass. Set `auto_scope=False` for explicit cross-project queries.

## Transparency requirement

When you retrieve and use a memory, briefly say so:

> *"Using your stored preference for code-driven tutorials…"*

Non-negotiable. The user needs to know when stored context shaped a reply.

## Recording use

Every `memory_search` hit and `memory_show` response carries an opaque `use_token`. **If you don't call `memory_record_use` within ~2 turns, the server auto-commits as `outcome="applied"`** on the next `memory_*` call (logged with `auto=true`). The common case handles itself.

Call `memory_record_use(memory_ids=[…], outcome=…)` explicitly only to override:

- `"ignored"`: retrieved but off-topic.
- `"contradicted"`: user or current state contradicted the stored fact AND you haven't fixed it yet. Raises the unresolved-contradiction flag until a later `memory_update` or `memory_verify` clears it.
- `"corrected"`: memory had drifted and you fixed it inline (same turn `memory_update` and/or `memory_verify`). Audit-only; does NOT raise the contradiction flag. Use this instead of `contradicted` when the resolution is already done.

The explicit override wins via override semantics — the server purges the pending token before recording.

### Claim-level provenance

When the memory shaped a user-visible sentence, pass the load-bearing phrase as `claim_excerpts` (parallel to `memory_ids`, one entry per id, `None` for "no specific claim"). Especially useful on `contradicted` / `corrected` so the audit log records *which* claim was wrong. Excerpts are quotes (max 500 chars), not whole bodies.

## Verify before relying

Memory is a snapshot; it does not auto-refresh. Every retrieval carries a derived `staleness_verdict`:

- `"fresh"`: verification fresh AND no drift. Body claims are presumed current.
- `"spot_check_recommended"`: verification calendar-fresh but the world has moved (path missing, or repo has commits since the last verify). Quick check before relying.
- `"spot_check_required"`: `verification.status` is `"never"` or `"stale"`. Pre-empts the drift inputs because the verification anchor itself is missing or expired.

When the verdict isn't `"fresh"`, the hit already carries the actionable detail. `path_drift.missing` (when present) lists the body-cited paths that no longer exist — `memory_update` those directly, no memory_show round-trip needed. The remaining un-drifted claims (`path_drift.verified` plus the rest of the body) can be attested with `memory_verify(id, verified_paths=[…], verified_commits=[…], verified_versions=[…])`; the server uses these to short-circuit later drift signals. `memory_update` resets `last_verified_at`, so verify again after fixing drifted prose to close the loop.

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

Storage layout: `<root>/episodes/<session_id>/<ulid>.md`. Default 30-day TTL, pruned on each write. Episodes are **invisible to `memory_search` / `memory_health` / `memory_list`** — they live in a sibling subtree the memory iterators never see.

### Loop-iteration pattern

A `/loop` iteration (or any agent resuming work in a worktree) should:

1. **At entry**: call `episode_handoff()`. Returns the prior session's recent takeaways with `{prior_session_id, episodes: [{id, created, takeaway, body, scopes}, ...]}`. Distinguish `prior_session_id is None` (no baseline) from `episodes == []` (prior session left no journal).
2. **At exit**: call `episode_write(body=..., takeaway="one-line summary")`. The takeaway is what the next iteration sees first.
3. **When a takeaway hardens into a durable fact**: call `episode_promote(episode_id, scopes=[...])`. Routes through `memory_write` so the durability gate fires; the source episode is deleted on commit, left in place on any rejection so you can adjust and retry.

`memory_search(since_prior_session=True)` is the memory-tier companion: filter the durable memory store to entries `updated` since the prior session boundary. The semantic is "what THIS session has changed since the last other-session activity" — your own intra-session diff. For what the *prior* iteration did, use `episode_handoff` instead.

### Subagent handoff

When a parent agent spawns a subagent, pass the parent's `session_id` along; the subagent calls `episode_handoff(prior_session_id=<parent>)` to inherit context. The handler skips the event-log walk when the id is explicit.

## Negative-results suppression

A `memory_search` hit whose memory was `ignored` or `contradicted` in the last 30 days AND not since `applied` carries `recent_negative_outcomes`. The user already rejected this recently. Don't re-surface unless you have new reason to think the rejection no longer applies. The `claim_excerpt` field (when present) lets you rephrase or skip just the offending sentence rather than the whole body.

## Scopes

Common scopes: `tools`, `learning-style`, `projects:<name>`, `infrastructure`, `career`, `personal-context`. Avoid the catch-all `general`.

If the user says *"this is unrelated to project X"*, call `memory_scope_disable("projects:X")` for the rest of the session.

`memory_health.rare_scopes` surfaces typo singletons. Fix via `memory_rename_scope(old, new)`.
