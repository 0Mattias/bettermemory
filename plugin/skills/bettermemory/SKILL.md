---
name: bettermemory
description: Persistent memory between sessions, retrieved on demand. Use bettermemory's MCP tools (memory_search, memory_show, memory_write, etc.) instead of writing to files when the user asks you to "remember" something or references shared context from a past session. Default is to NOT call memory_search — only retrieve when the user references context you don't have ("my project", "the script we wrote") or a request is ambiguous in a way stored preferences could resolve.
---

# bettermemory: opt-in memory retrieval

Persistent memory between sessions lives in this plugin's MCP tools.
**Don't fragment memory across ad-hoc files alongside** (`MEMORY.md`,
`~/.claude/projects/*/memory/`, scratch markdown elsewhere) — future
sessions only see what these tools surface, so anything stored
elsewhere is invisible to next-week's-you.

## Available tools

- **Retrieval**: `memory_search`, `memory_show`, `memory_list`, `memory_scope_overview`
- **Writing**: `memory_write` (+ `memory_write_confirm` / `memory_write_cancel` for the staged-write flow), `memory_update`
- **Lifecycle**: `memory_remove`, `memory_restore`, `memory_list_tombstones`
- **Verification**: `memory_verify`
- **Curation**: `memory_record_use`, `memory_health`, `memory_rename_scope`
- **Session-local**: `memory_scope_disable`, `memory_scope_enable`

## When to retrieve

Memory is **OPT-IN retrieval**. The user's stored memories are NOT in
your context unless you call `memory_search`. **Default to not
retrieving** — false positives (irrelevant stored context cascading
through a whole conversation) are much worse than false negatives
(missing context the user can supply in one followup turn).

Call `memory_search` ONLY when:

- the user references shared context you don't have (*"my project"*,
  *"the script we wrote"*, *"do you remember…"*)
- a request is ambiguous in a way stored preferences could resolve

Skip it for:

- generic factual questions (*"what's the capital of France"*)
- self-contained technical questions (*"how do I write a Python list comprehension"*)
- fully-specified messages with no ambiguity

### Session-start hint

One call to `memory_scope_overview` returns scope counts without
bodies. If `total=0`, skip `memory_search` for the rest of the session
unless the user explicitly asks for stored context. If non-zero,
`memory_search` is the way to retrieve content. Use this once per
conversation — it's a yes/no signal, not something to poll.

### Auto-scoping

`memory_search` is auto-scoped to the caller's current repository by
default (`auto_scope=True`). Memories written from a different repo
are filtered out automatically; memories with no recorded origin
(legacy entries, or writes from outside any repo) are treated as
global and always pass. Set `auto_scope=False` only for explicit
cross-project queries (*"anything I've stored about X across all my
projects"*).

## Transparency requirement

When you do retrieve and use a memory, **briefly say so** in your
response:

> *"Using your stored preference for code-driven tutorials…"*

This is non-negotiable. The user needs to know when stored context
shaped a reply.

## Recording use

After your response uses a retrieved memory, call
`memory_record_use(ids, outcome)` once with the ids that actually
shaped the reply. Outcomes:

- `"applied"` — the memory shaped the response.
- `"ignored"` — retrieved but turned out off-topic.
- `"contradicted"` — the user or current state contradicted the stored
  fact AND you have not fixed it yet. Raises the unresolved-contradiction
  flag in `memory_health` until a later `memory_update` or
  `memory_verify` clears it.
- `"corrected"` — the memory had drifted and you fixed it inline
  (called `memory_update` and/or `memory_verify` in the same turn).
  Audit-only — does NOT raise the contradiction flag. Use this
  instead of `"contradicted"` when the resolution is already done.

Quick rule: if you've already fixed the drift, log `"corrected"`; if
you've only noticed it, log `"contradicted"` and let
`memory_update` / `memory_verify` clear the flag later. **Skip the call
entirely** when no retrieved memory shaped your response — the absence
of an `applied` event is itself the signal that the memory wasn't
useful. Don't fabricate a `record_use` call just to be tidy.

## Verify before relying

Memory is a snapshot — it does not auto-refresh. Every retrieval
carries up to three structured staleness signals:

1. **`verification` block** (status: `"never"` / `"stale"` / `"fresh"`).
   When status is not `"fresh"`, spot-check at least one verifiable
   claim from the body (file path, version number, configuration)
   against ground truth. If the check passes, call `memory_verify(id,
   note=...)` to record what you confirmed. If a claim has drifted,
   fix the body via `memory_update` first — then `memory_verify` the
   corrected version (the update resets `last_verified_at` to null
   because the prior verification was for prose that no longer
   exists).

2. **`path_drift`** counts. Filesystem paths cited in the body that no
   longer exist on disk. Advisory — drift can be a temporary mount
   or a path on a different machine — but a drifted path on a
   never-verified memory is the highest-risk profile.

3. **`commit_drift`** counts (when caller is in the memory's origin
   repo). Commits authored after `last_verified_at`. The load-bearing
   case: `verification.status == "fresh"` only proves the calendar is
   fresh. A non-zero `commit_drift` is the cue to spot-check anyway —
   the project moved since the last `memory_verify`.

## Writing memory

**Durable only.** Memory is for facts that will still be true in a
week if nobody updates them. The tool enforces this structurally:
`memory_write` returns `{status:"transient_warning", markers:[…]}`
instead of committing when the body contains transient markers
(*"currently"*, *"today I"*, *"we just"*, *"the new"*, commit-SHA-like
hex tokens). When that fires, the durable fact you actually want is
one level up — extract the architectural decision, the why, the
what-was-built, and discard the timestamp/state. Pass
`acknowledge_transient=True` only when the marker is genuinely durable
in context (rare).

**Refining or correcting a stored fact?** Call `memory_update(id, …)`
instead of `memory_remove` + `memory_write`. That preserves the
original `created` timestamp.

**Dedup is automatic at write time.** `memory_write` returns
`{status:"duplicate", matches:[…]}` when the new body has high content
overlap with an existing memory; the right response is `memory_update`
on the matched id. Tombstone-aware dedup also runs: high overlap with
a previously-removed memory returns `{status:"previously_removed",
removed_matches:[…]}` carrying the original `removed_reason`. Inspect
the reason — if the rejection still applies, drop the write; if the
fact is now correct, call `memory_restore(id)` on the tombstone.

**Confirmation policy is tiered**, structurally enforced via
`category`:

- For project, infrastructure, reference, and tooling memories — call
  `memory_write` with the default `category="fact"`. The write commits
  immediately; announce the save in one line so the user can object
  (*"Saved: bettermemory env var rename to BETTERMEMORY_DIR"*).
- For memories that capture inferences about the user (preferences,
  beliefs, claims about how they want to work) — pass
  `category="user-inference"`. The server returns `{status:"pending",
  pending_id, pending_reason:"user-inference"}` instead of committing.
  Ask the user in plain language (*"Want me to remember that you
  prefer X?"*) and only then call `memory_write_confirm(pending_id)`,
  or `memory_write_cancel(pending_id)` if they decline.

## Scopes

Tag with appropriate scopes. Avoid the catch-all `general` scope.

Common scopes: `tools`, `learning-style`, `projects:<name>`,
`infrastructure`, `career`, `personal-context`.

If the user says *"this is unrelated to project X"*, call
`memory_scope_disable("projects:X")` for the rest of the session.

## Scope hygiene and curation

`memory_health` aggregates over the event log + active memories.
Surfaces dead-weight memories (retrieved often, never `applied`),
heavily-used memories, unresolved contradictions, transient-marker
fire/override rates, scope distribution, `rare_scopes` (singletons
within Levenshtein distance 2 of another scope — likely typos), and
`verification_debt` / `commit_drift_debt` rollups. Use it for periodic
curation passes; fix typos via `memory_rename_scope(old, new)`.
