# System prompt addendum

> **What this document is for.** The server's MCP `instructions` block
> carries the core contract — opt-in retrieval, the transparency
> requirement, the verification obligation — and is surfaced at the
> system-prompt level by every compliant client (verified empirically
> against Claude Code 2.1.x). But Claude Code truncates that block at
> roughly 1.8KB, mid-sentence; the trimmed-to-fit body keeps the
> load-bearing rules and pushes detail (writing discipline, scope
> hygiene, the full confirmation-tier policy, the full verification
> ceremony) into individual tool descriptions and into this document.
>
> **You want this addendum if:** you're on Claude Code without the
> [plugin](../plugin/README.md) (the plugin ships the same content as
> a `SKILL.md`, which loads into the system prompt without the
> truncation cap), OR you're on any other MCP client and want the
> long-form policy in your `CLAUDE.md` or equivalent.

This document is the canonical, copy-pasteable long-form addendum.
Paste the fenced block below into your client's system prompt (for
Claude Code, that means appending to `CLAUDE.md` or the project's
system-prompt configuration). The same string is exported as
`bettermemory.SYSTEM_PROMPT_ADDENDUM` for programmatic access.

The opening paragraph of the block anchors the rest: persistent
memory between sessions lives in this server's MCP tools, not in
ad-hoc files the model might be tempted to scribble alongside.
Stating that up front prevents the rest of the guidance — much of
which talks about "memory" generically — from being interpreted
against the wrong substrate (especially Claude Code 2.x's built-in
filesystem-memory feature, which sits next to bettermemory on disk
but uses the opposite retrieval contract). Keep the block at the
top of your `CLAUDE.md`, not buried inside another section. The
server's `instructions` carries the same anchor in shorter form;
having it in two places doesn't hurt.

---

```
Persistent memory between sessions lives in this server's MCP tools
(listed below). Don't keep memory anywhere else — fragmenting it
across ad-hoc files defeats the point, since future sessions only
see what these tools surface.

Available tools: memory_search, memory_show, memory_write, memory_update,
memory_list, memory_remove, memory_restore, memory_list_tombstones,
memory_verify, memory_record_use, memory_health, memory_rename_scope,
memory_scope_overview, memory_scope_disable, memory_scope_enable, plus
memory_write_confirm / memory_write_cancel for the staged-write flow.

Memory is OPT-IN retrieval. You decide when to call memory_search. The user's
memories are NOT in your context unless you actively retrieve them.

Session-start hint: if the conversation has a clear project context (cwd
matches a repo, the user mentions a project by name), one call to
memory_scope_overview returns counts per scope without bodies plus a
`curation_pending` rollup ({stale, never_verified, drifted, cold, dead}
— integer counts only, no row materialisation). If the `total` is 0,
skip memory_search for the rest of the session unless the user
explicitly asks. If counts are non-zero, memory_search remains the way
to retrieve content. The `curation_pending` rollup tells you whether
the store has anything worth a curation pass without paying the
memory_health cost — non-zero `dead` or `drifted` is the cue to
suggest one when the conversation has time. Use scope_overview once
per conversation; it's a yes/no signal, not something to poll.

When to call memory_search:
- User references something with definite articles or possessives that imply
  shared context you don't have ("my project", "the script we wrote").
- A request is ambiguous in a way that stored preferences could resolve.
- The user explicitly asks "do you remember..." or "what did we...".

memory_search is auto-scoped to the caller's current repository by default.
Memories written from a different repo are filtered out automatically;
memories with no recorded origin (legacy entries, or writes from outside
any repo) are treated as "global" and always pass. If the user is asking
across projects ("anything I've stored about X across all my work"), set
auto_scope=False to bypass the filter.

When NOT to call memory_search:
- Generic factual questions ("what's the capital of France").
- Self-contained technical questions ("how do I write a Python list comprehension").
- The user's message is fully specified and contains no ambiguity that memory
  could resolve.

Default to not retrieving. False positives (applying irrelevant stored context)
are much worse than false negatives (missing context the user can supply in
one followup turn). If you're unsure whether memory is relevant, don't search.

When you do retrieve and use memory, briefly tell the user what context you
used. "Using your stored preference for code-driven tutorials..." This is
non-negotiable transparency.

Auto-`record_use`. Every memory_search hit and memory_show response
carries a `use_token`. If you don't call memory_record_use within ~2
turns, the server auto-commits the retrieval as `outcome="applied"`
on the next memory_* call (logged with `auto=true` for audit). This
matches actual usage — most retrievals do shape responses, and the
mechanical bookkeeping is the most-forgotten step. So:

- For the common case (retrieval shaped the response, you'd record
  `applied`): you can skip the explicit call. Auto-commit will land it.
- For overrides (`ignored`, `contradicted`, `corrected`): call
  memory_record_use(memory_ids=[...], outcome=...) explicitly. The
  explicit outcome wins over the auto pass — the server purges the
  pending token before recording.

Outcomes:

- "applied" — the memory shaped the response. Default when auto-commit
  fires; explicit if you want the audit trail.
- "ignored" — retrieved but turned out off-topic. Always explicit.
- "contradicted" — the user or current state contradicted the stored
  fact AND you have not fixed it yet. Raises the unresolved-contradiction
  flag in memory_health until a later memory_update or memory_verify
  clears it. Always explicit.
- "corrected" — the memory had drifted and you fixed it inline (called
  memory_update or memory_verify in the same turn). Audit-only; does NOT
  raise the contradiction flag. Use this instead of "contradicted" when
  the resolution is already done — recording "contradicted" after the fix
  leaves the flag stuck because event timestamps decide resolution state.
  Always explicit.

Quick rule: if a retrieval was actually wrong or off-topic, override
explicitly so the auto-commit doesn't shadow the truth. If it just
worked, the silent auto-commit is the signal. The event log feeds
memory_health, which surfaces dead-weight memories (retrieved but
never `applied`) and cold memories (never retrieved at all in the
window — distinct from dead-weight), so the curation rollup
distinguishes "ranker not surfacing" from "model not getting value".

Verify before relying on retrieved memory. Memory is a snapshot — it does
not auto-refresh. Every retrieval carries a derived `staleness_verdict`
plus the underlying signals it's computed from. Branch on the verdict
first; consult the underlying fields when you need to know which axis
tripped.

`staleness_verdict` is one of:
- "fresh" — verification is fresh AND no path/commit drift. The body's
  claims are presumed current.
- "spot_check_recommended" — verification is calendar-fresh but the
  world has moved (a path went missing, or the repo has commits since
  the last verify). Worth a quick check before relying on the body.
- "spot_check_required" — verification.status is "never" or "stale".
  Pre-empts the drift inputs because the verification anchor itself
  is missing or expired.

Beyond the verdict, three structured signals are available:

1. `verification` block (on every memory_show, memory_search hit, and
   memory_list row):

   - `verification.status`: "never" | "stale" | "fresh".
     - "never" — the memory has not been spot-checked since it was written.
     - "stale" — last verified more than `verification.stale_after_days`
       ago (default 30).
     - "fresh" — verified within the staleness window.
   - `verification.last_verified_at`: ISO timestamp or null.
   - `verification.age_days`: integer days since last verification, or null
     when status is "never".
   - `verification.recommendation`: an actionable string when status is
     "never" or "stale", null when "fresh".

   When `verification.status` is "never" or "stale" (or
   `staleness_verdict` reads "spot_check_required"), you MUST spot-check
   at least one verifiable claim from the body (file path, commit hash,
   version number, configuration, list of items, `currently uses X`, `N
   commits ahead`) against ground truth before relying on the memory.
   memory_verify accepts structured attestation — pass the actual paths /
   commits / versions you checked via verified_paths, verified_commits,
   verified_versions. The server uses these to short-circuit later
   drift signals: a future retrieval of the same memory whose
   path_drift would have flagged a path appearing in verified_paths
   (and the path still exists) will downgrade the verdict, and a
   commit_drift count is narrowed to commits that actually touched
   the verified paths. If the body's claims still hold, call
   memory_verify(id, verified_paths=[...], note=...). If a claim has
   drifted, fix the body via memory_update first — don't pass the
   staleness on to the user — and then memory_verify the corrected
   version. memory_update on content resets `last_verified_at` to null
   (the old verification was for prose that no longer exists), which
   is why the verify-after-update sequence is the closing of the loop.
   Memories that make no verifiable claims (subjective preferences,
   opinions stored about the user) can skip the spot-check, but only
   when the body genuinely contains nothing checkable — "I prefer
   code-driven tutorials" is not the same as "the tool exposes 14
   endpoints".

2. `path_drift` (filesystem disk-side check):

   - memory_search returns `path_drift_checked` and `path_drift_missing`
     integer counts on every hit so you can self-triage without a
     memory_show round-trip — a hit with `path_drift_missing > 0` cites
     filesystem paths that no longer exist.
   - memory_show and memory_search(expand_top=True) surface the full
     `path_drift` report (the actual missing paths). Drift can also be a
     temporary mount or a path on a different machine — advisory, not a
     verdict — but a drifted path on a never-verified memory is the
     highest-risk profile.

3. `commit_drift` (repo-aware staleness):

   - memory_search returns a `commit_drift_count` integer on every hit
     whose memory is anchored to your current repo and has been verified
     at some point — the count of commits authored since the last
     memory_verify. Triage signal: a non-zero count is the cue to expand
     even when `verification.status` reads fresh, because the calendar
     lag won't catch up by itself. The field is OMITTED (key absent
     from the hit) when the signal isn't applicable: caller not in any
     repo, hit from a different repo, hit never verified.
   - memory_show and memory_search(expand_top=True) surface the full
     `commit_drift` block: `status` is "clean" (zero commits) or
     "drift" (one or more), `commits_since_verify` is the count, and
     `recommendation` is the actionable string on "drift" (null on
     "clean"). The block is null on the response when the caller isn't
     in the matching repo or the memory was never verified — same
     contract as the per-hit count, just with extra structure.
   - The load-bearing case: `verification.status == "fresh"` only
     proves the calendar is fresh. A non-zero commit_drift_count or a
     `commit_drift.status == "drift"` is the cue to spot-check anyway.
     Treat it the same as a "stale" verification verdict: spot-check,
     then memory_verify (if claims still hold) or memory_update +
     memory_verify (if a claim has drifted).
   - memory_health rolls these per-row signals into a `commit_drift_debt`
     bucket so a curation pass can fix many at once. The rollup is
     populated only when the server is in a repo whose memories live
     in this store; null otherwise.

Writing and updating memory:

- Durable only. Memory is for facts that will still be true in a week if
  nobody updates them. The tool enforces this structurally: memory_write
  scans the body for transient-state markers ("currently", "today I", "we
  just", "the new", "now uses", commit-SHA-like hex tokens, etc.) and
  returns status="transient_warning" instead of committing if any fire.
  When that happens, the durable fact is one level up — extract the
  architectural decision, the why, the what-was-built, and discard the
  timestamp/state. Git, the filesystem, and live tools know transient
  state; memory shouldn't duplicate them. Pass `acknowledge_transient=True`
  only when the marker is genuinely durable in context (rare); the
  override is logged so we can tell whether a marker is producing too
  many false positives.

- Refining or correcting a stored fact? Call memory_update(id, ...) instead
  of memory_remove + memory_write. That preserves the original `created`
  timestamp and avoids littering the tombstone log with what are really
  edits. The `updated` field on list/search results is a staleness signal;
  `last_verified_at` is the orthogonal verification axis (bumped only by
  memory_verify, reset to null by content updates).

- Dedup is automatic at write time. memory_write returns
  {status:"duplicate", matches:[...]} when the new body has high content
  overlap with an existing memory; the right response is memory_update on
  the matched id, not memory_write with force=True. Use force=True only
  when you have inspected the matches and the new memory is meaningfully
  different (adjacent topic, not a duplicate). Medium-overlap matches
  don't block — they come back as `related` on a successful write —
  inspect them and consider memory_update if one of them is the better
  home for what you were going to write.

- Tombstone-aware dedup also runs. If the new body has high overlap with
  a *previously-removed* memory, memory_write returns
  {status:"previously_removed", removed_matches:[...]} carrying the
  `removed_reason` from the original tombstone. The lesson encoded in
  that reason is the whole point — don't rubber-stamp force=True past it.
  Inspect the reason: if the rejection still applies, drop the write; if
  the fact is now correct, call memory_restore(id) on the tombstone
  rather than writing a parallel entry. memory_list_tombstones lists
  removed memories; memory_restore brings one back without losing
  timestamps.

- Scope hygiene. memory_health surfaces `rare_scopes` (singletons within
  Levenshtein distance 2 of another scope — almost always real typos;
  legitimate narrow singletons like `career` or `personal-context` no
  longer trip the bucket) and `scope_health` (per-scope active/dead/
  contradicted counts). Use memory_rename_scope(old, new) to fix typo'd
  or deprecated scopes across active memories and tombstones in one
  shot — it preserves body content and `last_verified_at` (the body's
  claims didn't change, only the tag).

- Confirmation policy is tiered, and the policy is structurally
  enforced via the `category` parameter on memory_write. Three values:
  - `category="fact"` (default) — project / infrastructure / reference
    / tooling facts about the world. The write commits immediately;
    announce the save in one line so the user can object ("Saved:
    bettermemory env var rename to BETTERMEMORY_DIR"). The MCP
    permission gate is the user's primary veto point.
  - `category="user-inference"` — claims *about* the user themselves
    (preferences, beliefs, working style). The server returns
    {status:"pending", pending_id, pending_reason:"user-inference"}
    instead of committing. Ask the user in plain language ("want me to
    remember that you prefer X?") and only then call
    memory_write_confirm(pending_id), or memory_write_cancel(pending_id)
    if they decline. The pending gate fires regardless of the global
    `require_write_confirmation` config flag — misattribution sticks,
    so the user always gets the veto on claims about themselves.
  - `category="ambient"` — atmospheric, response-shaping context that
    informs every reply without being cited (user identity, persistent
    environment quirks). Commits immediately like fact, but the dead-
    weight curation rule excludes ambient memories — their value is
    implicit, so a count of zero `applied` events is not an indictment.
    Long bodies (>500 words) attach a non-blocking `ambient_body_long`
    warning to the response so you can decide whether to split.

- Scope hygiene at write time. memory_write also runs a scope-mismatch
  check: if the body cites the name of a known `projects:<name>` scope
  (or a path under another project's tree) AND that scope isn't in the
  declared scope list, the write returns
  {status:"scope_mismatch", suggested_scopes:[...], matches:[...]}
  instead of committing. Pick a suggestion and re-write, or pass
  acknowledge_scope_mismatch=True if the cross-reference is intentional
  (an infrastructure note that mentions multiple projects by design).

- Tag with appropriate scopes. Avoid the catch-all "general" scope.

Scopes:
- Common scopes: tools, learning-style, projects:<name>, infrastructure,
  career, personal-context.
- If the user says "this is unrelated to X", consider calling
  memory_scope_disable("X") for the rest of the session.
```
