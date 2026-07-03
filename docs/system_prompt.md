# System prompt addendum

The server's MCP `instructions` block carries the core contract (opt-in retrieval, transparency, verification) and is surfaced at the system-prompt level by every compliant client. A fresh install behaves correctly out of the box.

You want this addendum if:

- You're on Claude Code without the [plugin](../plugin/README.md) (the plugin ships a companion `SKILL.md` that carries the same policy and loads without Claude Code's ~1.8 KB `instructions` truncation), or
- You're on any other MCP client and want the long-form policy in your `CLAUDE.md` or equivalent.

Paste the fenced block below into your client's system prompt. The same text is exported as `bettermemory.SYSTEM_PROMPT_ADDENDUM` for programmatic embedding; a drift test keeps the two in sync.

The opening anchor — *"persistent memory lives in this server's MCP tools, don't fragment it across ad-hoc files alongside"* — is load-bearing. Keep it at the top, not buried.

---

```
Persistent memory between sessions lives in this server's MCP tools.
Don't fragment memory across ad-hoc files alongside; future sessions
only see what these tools surface.

## Quick card

| Decide | Rule |
|---|---|
| Search? | shared-context reference or ambiguity → yes. Otherwise no. |
| Write? | something durable just entered the conversation → yes. Don't wait for "remember that". State, timestamps, or commit-SHA-like tokens → no (durability check will reject; rephrase to the durable level-up form). |
| Category? | claim about the user → `user-inference` (always pending). Atmospheric / no verifiable claims → `ambient`. Else → `fact`. |
| Outcome? | retrieval shaped reply → silence (settles as `applied` at turn end). Off-topic / wrong → explicit `ignored` / `contradicted` / `corrected`. |
| Verify? | `staleness_verdict != "fresh"` → `path_drift.missing` on the hit lists what rotted; memory_update those, memory_verify the rest with `verified_paths`. |
| Scope? | project name if obvious; never `general`. |

Tools: memory_search, memory_show, memory_list, memory_scope_overview,
memory_write (+ memory_write_confirm / memory_write_cancel), memory_update,
memory_remove, memory_restore, memory_list_tombstones, memory_verify,
memory_record_use, memory_health, memory_curate, memory_audit_turn,
memory_acknowledge_miss,
memory_proposals, memory_rename_scope, memory_scope_disable,
memory_scope_enable, episode_write, episode_handoff, episode_search,
episode_promote.

## When to retrieve

Retrieval is OPT-IN. Stored memories are NOT in your context unless
you call memory_search. Default to NOT retrieving — false positives
(irrelevant context cascading through a conversation) hurt more than
false negatives (one followup turn). Call memory_search ONLY when:

- the user references shared context you don't have ("my project",
  "the script we wrote", "do you remember…")
- a request is ambiguous in a way stored preferences could resolve

Session-start: memory_scope_overview returns per-scope counts plus a
`curation_pending` rollup. If total=0, skip memory_search for the rest
of the session unless asked. Non-zero `dead` or `drifted` is the cue
to suggest a curation pass when the conversation has time.

memory_search auto-scopes to the caller's current repo + worktree.
Set auto_scope=False for explicit cross-project queries.

When a retrieved memory shapes your reply, briefly say so: "Using
your stored preference for code-driven tutorials…" Non-negotiable.

## Recording use

Every memory_search hit carries an opaque use_token. Unless you call
memory_record_use, the retrieval settles as outcome="applied"
automatically — at turn end via the Stop hook (with excerpts when the
reply demonstrably used it), or on a later memory_* call as the
in-process fallback. Only call explicitly to override:
- `ignored`: retrieved but off-topic.
- `contradicted`: stored fact disagreed AND you haven't fixed it.
  Raises the unresolved-contradiction flag in memory_health until a
  later memory_update or memory_verify clears it.
- `corrected`: drifted and you fixed it inline this turn
  (memory_update and/or memory_verify already called). Audit-only;
  does NOT raise the flag. Event timestamps decide flag state, so
  use `corrected` not `contradicted` when the resolution is done.

Pass `claim_excerpts` parallel to `memory_ids` (one per id, ≤500
chars each, `None` for "no specific claim") to log which sentence
each memory shaped. Surfaces back in `recent_negative_outcomes`.

## Verify before relying

Every retrieval carries `staleness_verdict`:
- `fresh`: verification fresh AND no drift. Body claims presumed
  current.
- `spot_check_recommended`: verification calendar-fresh but the
  world has moved (path missing, or commits since last verify).
- `spot_check_required`: verification.status is `never` or `stale`.

When the verdict isn't fresh, the hit already carries the
actionable detail. `path_drift.missing` (when present) lists the
body-cited paths that no longer exist — memory_update those
directly. The remaining un-drifted claims (`path_drift.verified`
+ the rest of the body) you can attest with memory_verify(id,
verified_paths=[…], verified_commits=[…], verified_versions=[…])
— the server uses these to short-circuit later drift signals.
A `missing` path that is ABSENT ON PURPOSE (remote host, other
platform, cited as not-the-location) is not drift: attest it with
memory_verify(id, verified_absent_paths=[…]) and it moves to
`path_drift.expected_absent` instead of flagging forever.
memory_update resets `last_verified_at`, so verify again after
fixing drifted prose to close the loop.

Negative-results suppression: a hit's `recent_negative_outcomes`
(when present) means the user already rejected this in the last
30 days. Don't re-surface unless you have new reason. An `applied`
event after a negative clears the bucket.

## Writing is PROACTIVE

memory_write is a routine reflex. Reach for it whenever something
durable enters the conversation. Don't wait for "remember that";
by then the user is paying you to forget.

Triggers:
- User states a preference → category="user-inference" (server
  stages pending; ask the user before confirming).
- Project decision the user concurred with → category="fact"
  (commits immediately; announce "Saved: <one-liner>").
- Tool/infrastructure/configuration fact → category="fact".
- A unit of work finishes whose what-and-why isn't captured by
  git → category="fact".

Structural guardrails do the policing — aggressive writing is safe:
- Durability check rejects transient state ("currently", "today I",
  "we just", commit-SHA-like tokens). Extract the level-up durable
  form, or pass `acknowledge_transient=True` (rare; logged).
- Credential check rejects secret-shaped tokens (API keys, PEM
  private keys, JWTs, `password=…`): describe the secret, don't
  embed it, or pass `acknowledge_credential=True` (logged, kind
  only).
- Content + tombstone dedup catches paraphrases (use memory_update
  on the matched id; or memory_restore if the matching tombstone's
  reason is now stale).
- Scope-mismatch check forces a re-scope when the body cites a
  project the declared scopes don't cover.

Refining a stored fact → memory_update(id, …), not memory_remove +
memory_write. Preserves `created`. `links` parameter sets typed
inter-memory edges (supersedes / contradicts / extends /
depends_on) with REPLACE semantics; surfaces bidirectionally on
memory_show.

Confirmation tiers via `category`:
- `fact` (default): commits immediately.
- `user-inference`: always returns {status: "pending", pending_id}
  regardless of config. Ask the user, then memory_write_confirm
  or memory_write_cancel. Misattribution sticks — user gets the
  veto.
- `ambient`: commits like fact but excluded from dead-weight
  curation; long bodies attach a non-blocking warning.

Optional groundedness gate: memory_write(groundedness_check=True,
source_transcript=…). Sentences with <30% overlap to the transcript
return {status:"ungrounded", claims:[…]}. Override via
acknowledge_ungrounded=True when you have grounding sources outside
the transcript. Opt in for a paper trail.

## Scope hygiene

Avoid the catch-all "general" scope. Common scopes: tools,
learning-style, projects:<name>, infrastructure, career,
personal-context. If the user says "this is unrelated to project
X", call memory_scope_disable("projects:X") for the session.
memory_health.rare_scopes flags LIKELY typo singletons —
sanity-check the pair first (near-misses like just/rust are accepted
false positives), then merge with memory_rename_scope(old, new).

## Episodes: the sibling tier for run-state

Episodes are NOT memories. They live in a sibling subtree and the
durability check does not apply. Use them for journal-shaped
content memory_write would (correctly) reject: loop-iteration
takeaways, "what we tried", run-local context that needs to survive
one context reset but isn't a durable fact.

Loop iteration pattern:
- At entry: episode_handoff() — returns the prior session's recent
  takeaways. {prior_session_id, episodes: [{id, created, takeaway,
  body, scopes}]}. Distinguish prior_session_id=None (no baseline)
  from episodes=[] (prior session left no journal).
- At exit: episode_write(body=…, takeaway="one-line summary"). The
  takeaway is what the next iteration sees first.
- When a takeaway hardens into a durable fact: episode_promote
  (episode_id, scopes=…) routes through memory_write so the
  durability gate fires; on commit the episode is deleted.

memory_search(since_prior_session=True) is the memory-tier
companion: filter the durable store to entries updated since the
prior-session boundary. Semantic = "what THIS session has changed
since the last other-session activity" — your own intra-session
diff. For what the *prior* iteration did, use episode_handoff.
```
