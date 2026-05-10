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

## Quick card

| Decide | Rule |
|---|---|
| Search? | shared-context reference or ambiguity → yes. Otherwise no. |
| Write? | durable in a week with no maintenance → yes. State / timestamps → no (the tool will reject). |
| Category? | claim about the user → `user-inference`. Atmospheric / no verifiable claims → `ambient`. Else → `fact`. |
| Outcome? | retrieval shaped reply → silence (auto-commits as `applied`). Off-topic / wrong → explicit `ignored` / `contradicted` / `corrected`. |
| Verify? | `staleness_verdict != "fresh"` → spot-check one claim before relying; pass `verified_paths` to `memory_verify`. |
| Scope? | project name if obvious; never `general`. |

The rest of this document is reference for when one of those answers
needs more context.

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

One call to `memory_scope_overview` returns scope counts plus a
`curation_pending` rollup (`{stale, never_verified, drifted, cold,
dead}` integer counts only — no row materialisation). If `total=0`,
skip `memory_search` for the rest of the session unless the user
explicitly asks. If counts are non-zero, `memory_search` is the way
to retrieve content. The `curation_pending` rollup tells you whether
the store has anything worth a curation pass without paying the
`memory_health` cost — non-zero `dead` or `drifted` is the cue to
suggest one when the conversation has time. Use this once per
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

## Recording use (auto-commit + override)

Every `memory_search` hit and `memory_show` response carries an
opaque `use_token`. **If you don't call `memory_record_use` within
~2 turns, the server auto-commits the retrieval as
`outcome="applied"`** on the next `memory_*` call (logged with
`auto=true` for audit). Bookkeeping for the most-forgotten step is
opt-out instead of opt-in — the common case (retrieval shaped the
response, you'd record `applied`) handles itself.

Call `memory_record_use(memory_ids=[…], outcome=…)` explicitly only
when you want to override the auto-commit:

- `"ignored"` — retrieved but turned out off-topic.
- `"contradicted"` — the user or current state contradicted the
  stored fact AND you have not fixed it yet. Raises the
  unresolved-contradiction flag in `memory_health` until a later
  `memory_update` or `memory_verify` clears it.
- `"corrected"` — the memory had drifted and you fixed it inline
  (called `memory_update` and/or `memory_verify` in the same turn).
  Audit-only — does NOT raise the contradiction flag. Use this
  instead of `"contradicted"` when the resolution is already done.

The explicit override wins via override semantics: the server
purges the pending token before recording the explicit outcome, so
the auto-commit cannot shadow it. `"applied"` can also be passed
explicitly when you want the audit trail; it just happens to also
be the default.

## Verify before relying

Memory is a snapshot — it does not auto-refresh. Every retrieval
carries a derived `staleness_verdict` plus the underlying signals
it's computed from. **Branch on the verdict first**; consult the
underlying signals only when you need to know which axis tripped.

`staleness_verdict` is one of:

- `"fresh"` — verification is fresh AND no path/commit drift. The
  body's claims are presumed current.
- `"spot_check_recommended"` — verification is calendar-fresh but the
  world has moved (a path went missing, or the repo has commits since
  the last verify). Worth a quick check before relying on the body.
- `"spot_check_required"` — `verification.status` is `"never"` or
  `"stale"`. Pre-empts the drift inputs because the verification
  anchor itself is missing or expired.

Beyond the verdict, three structured signals are available:

1. **`verification` block** (status: `"never"` / `"stale"` / `"fresh"`)
   on every `memory_show`, `memory_search` hit, and `memory_list` row.
2. **`path_drift`** counts (`path_drift_checked` /
   `path_drift_missing` per hit, full report on `memory_show`).
   Filesystem paths cited in the body that no longer exist on disk.
   Advisory — drift can be a temporary mount or a path on a different
   machine — but a drifted path on a never-verified memory is the
   highest-risk profile.
3. **`commit_drift`** counts (`commit_drift_count` per hit when the
   caller is in the memory's origin repo, full block on
   `memory_show`). Commits authored after `last_verified_at`. The
   load-bearing case: `verification.status == "fresh"` only proves
   the calendar is fresh. A non-zero `commit_drift` is the cue to
   spot-check anyway — the project moved since the last
   `memory_verify`.

When the verdict is not `"fresh"`, spot-check at least one verifiable
claim from the body (file path, version number, configuration)
against ground truth. If the check passes, call `memory_verify(id,
verified_paths=[…], verified_commits=[…], verified_versions=[…],
note="…")` — passing the actual claims you spot-checked. The server
uses these to short-circuit later drift signals: future retrievals
of the same memory whose path_drift would have flagged a path in
`verified_paths` (and the path still exists) downgrade the verdict,
and `commit_drift` narrows the count to commits that actually
touched any of `verified_paths`. If a claim has drifted, fix the
body via `memory_update` first — then `memory_verify` the corrected
version (the update resets `last_verified_at` to null because the
prior verification was for prose that no longer exists).

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

**Scope mismatch is also caught at write time.** If the body cites a
known `projects:<name>` scope's name token (or a path under another
project's tree) AND that scope isn't in the declared scope list,
`memory_write` returns `{status:"scope_mismatch",
suggested_scopes:[…], matches:[…]}` instead of committing. Pick a
suggestion and re-write, or pass `acknowledge_scope_mismatch=True`
when the cross-reference is intentional (an infrastructure note that
mentions multiple projects by design).

### Confirmation policy is tiered

Structurally enforced via the `category` parameter on `memory_write`:

- `category="fact"` (default) — project / infrastructure / reference /
  tooling facts about the world. Commits immediately; announce the
  save in one line so the user can object (*"Saved: bettermemory env
  var rename to BETTERMEMORY_DIR"*).
- `category="user-inference"` — claims *about* the user themselves
  (preferences, beliefs, working style). The server returns
  `{status:"pending", pending_id, pending_reason:"user-inference"}`
  instead of committing. Ask the user in plain language (*"Want me to
  remember that you prefer X?"*) and only then call
  `memory_write_confirm(pending_id)`, or
  `memory_write_cancel(pending_id)` if they decline. Fires regardless
  of the global `require_write_confirmation` config — misattribution
  sticks, so the user always gets the veto on claims about themselves.
- `category="ambient"` — atmospheric, response-shaping context that
  informs every reply without being cited (user identity, persistent
  environment quirks). Commits immediately like `fact`, but the
  dead-weight curation rule excludes ambient memories — their value
  is implicit, so a count of zero `applied` events is not an
  indictment. Long bodies (>500 words) attach a non-blocking
  `ambient_body_long` warning so you can decide whether to split.

## Scopes

Tag with appropriate scopes. Avoid the catch-all `general` scope.

Common scopes: `tools`, `learning-style`, `projects:<name>`,
`infrastructure`, `career`, `personal-context`.

If the user says *"this is unrelated to project X"*, call
`memory_scope_disable("projects:X")` for the rest of the session.

## Scope hygiene and curation

`memory_health` aggregates over the event log + active memories.
Surfaces:

- **`dead_weight`** — created before the window, retrieved at least
  once, never `applied`. The body is misleading enough that retrieval
  doesn't help.
- **`cold_memories`** — created before the window, never retrieved at
  all. Distinct from `dead_weight`: the body might be fine, but the
  trigger isn't firing. Different curation question.
- **`heavily_used`** — frequently `applied`. Worth keeping fresh.
- **`contradicted`** — unresolved contradictions. Each carries a
  `resolution_timeline` (chronological log of `update` / `verify` /
  `contradicted` / `corrected` events) so a stuck flag can be
  self-diagnosed without grepping the event log.
- **`verification_debt`** / **`commit_drift_debt`** — calendar-stale
  vs. world-moved buckets, partitioned for batch curation.
- **`rare_scopes`** — singletons within Levenshtein distance 2 of
  another scope (likely typos). Fix via
  `memory_rename_scope(old, new)`.
- **`marker_stats`** — transient-marker fire/override counts. High
  override rate means the marker is producing too many false
  positives and should be trimmed.

The lighter `curation_pending` rollup on `memory_scope_overview`
(five integer counts, no row materialisation) is the cheap
session-start triage; reach for `memory_health` when one of those
counts is non-zero and you want the actual rows.
