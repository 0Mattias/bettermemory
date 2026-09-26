---
name: bettermemory
description: Memory between sessions that you can verify. Use bettermemory's MCP tools (memory_search, memory_show, memory_write, memory_update, memory_verify, memory_record_use, episode, memory_admin) instead of writing to files when the user asks you to remember something or references shared context from a past session. Retrieval is opt-in; search only when the user references context you lack or a request is ambiguous in a way stored preferences could resolve. Every hit carries a staleness_verdict; when it is not fresh, check one claim before relying and call memory_verify to attest. Writing is proactive: store durable facts as they enter the conversation.
---

# bettermemory: memory you can verify

Persistent memory between sessions lives in this plugin's MCP tools. **Do not keep it in ad-hoc files beside them** (`MEMORY.md`, scratch markdown elsewhere): later sessions see only what these tools hold.

This skill is the long-form companion to the server's `instructions` block, which Claude Code truncates at about 1.8 KB. The policy lives here; the mechanics of each tool live in its own description. Non-plugin clients get the same text from [`docs/system_prompt.md`](../../../docs/system_prompt.md).

## Quick card

| Decide | Rule |
|---|---|
| Search? | the user references shared context you lack, or the request is ambiguous in a way stored preferences could resolve. Otherwise no. |
| Write? | something durable just entered the conversation. Do not wait to be asked. State and timestamps are refused; write the durable form. |
| Category? | a claim about the user: `user-inference`. Context that shapes replies without being cited: `ambient`. Else `fact`. |
| Outcome? | a retrieval shaped the reply: silence, it settles as `applied`. Off-topic or wrong: `ignored`, `contradicted` or `corrected`. |
| Verify? | `staleness_verdict` not `fresh`: check one claim; `memory_verify` if it holds, `memory_update` if it drifted. |
| Scope? | the project's name when obvious; never `general`. |

## The nine tools

`memory_search`, `memory_show`, `memory_write`, `memory_update`, `memory_remove`, `memory_verify`, `memory_record_use`, `episode` (actions `write` and `handoff`) and `memory_admin` (actions `restore`, `tombstones`, `health`, `rename_scope`, `conflicts`, `acknowledge_miss`, `disable_scope`, `enable_scope`). The `bettermemory` command carries the offline work: `health`, `eval`, `export`, `tombstones`, `episodes`, `rename-scope`, `migrate`, `log verify`.

## When to retrieve

Memory is **opt-in retrieval**. Nothing stored is in your context until you call `memory_search`, with one narrow exception: the prompt-recall hook may inject a single pointer (id, scopes, snippet) when a stored memory scores high for the submitted prompt. Treat a pointer as a lead, not a body: `memory_show` it before relying on it, and the transparency rule applies unchanged.

**Default to not retrieving.** A wrong hit cascading through a conversation costs more than one follow-up turn. Call `memory_search` when:

- the user references shared context you do not have ("my project", "the script we wrote", "do you remember");
- a request is ambiguous in a way stored preferences could resolve.

Skip it for generic factual questions, self-contained technical questions and fully specified requests.

`memory_search` auto-scopes to the caller's current repo and worktree; memories from another repo are filtered out and legacy memories with no origin pass as global. Pass `auto_scope=False` for a cross-project query. `scopes` keeps only those tags, `exclude_scopes` drops them for one call, and `memory_admin(action="disable_scope", scope=...)` drops one for the rest of the session. `since_prior_session=True` lists what this session changed since the last other-session activity.

The session-start hook prints the per-scope counts for the current repository when the store holds anything for it; a session that sees no block has nothing stored for the repo.

## Transparency

When a retrieved memory shapes your reply, say so briefly:

> *"Using your stored preference for code-driven tutorials."*

This is not optional. The user needs to know when stored context shaped a reply.

## Recording use

Every `memory_search` hit and `memory_show` response carries a `use_token`. **Unless you call `memory_record_use`, the retrieval settles as `applied` on its own**: the Stop hook records reply-matched hits with excerpts, the rest as the plain auto-fallback, and a setup without the hook settles on a later tool call. The common case handles itself.

Call `memory_record_use(memory_ids=[...], outcome=...)` only to override:

- `ignored`: retrieved but off-topic.
- `contradicted`: the stored fact disagreed and you have not fixed it. Raises the unresolved-contradiction flag until a later `memory_update` or `memory_verify` clears it.
- `corrected`: the memory had drifted and you fixed it this turn (`memory_update` and/or `memory_verify` already called). Audit only.

When the memory shaped a user-visible sentence, pass the load-bearing phrase as `claim_excerpts` (parallel to `memory_ids`, `None` for no specific claim), so the audit log records which claim was used or was wrong.

## Verify before relying

Memory is a snapshot; it does not refresh itself. Every retrieval carries `staleness_verdict`, and only claim-anchored drift moves it: a path you attested with `verified_paths`, a citation resolved against the memory's own worktree, or a commit touching what the body cites. A path scraped out of prose ships as evidence but does not move the tier.

- `fresh`: verification fresh and no claim-anchored drift. Entries in `path_drift.missing` can still sit here as evidence.
- `spot_check_recommended`: verification calendar-fresh but an anchored path went missing, or commits landed on what the body cites.
- `spot_check_required`: never verified, or stale with no measurement to stand the calendar down.

When the verdict is not `fresh`, the hit carries the detail: `path_drift.claim_anchored_missing` is the subset that moved it, so `memory_update` those. Attest what held with `memory_verify(id, verified_paths=[...])`; attesting is what anchors a path, so its next disappearance escalates. A path absent on purpose (another host, another platform) goes in `verified_absent_paths`. `memory_update` resets `last_verified_at`, so verify again after fixing drifted prose.

A hit's `recent_negative_outcomes`, when present, means the user rejected it in the last 30 days. Do not re-surface it without new reason.

## Writing is proactive

`memory_write` is a reflex, not a request. Reach for it whenever something durable enters the conversation. Do not wait for "remember that"; by then the user is paying you to forget.

Triggers:

- the user states a preference or convention: `category="user-inference"`;
- a project decision the user concurred with: `category="fact"`, announced in one line;
- a tool, infrastructure or configuration fact (env vars, ports, paths, versions): `category="fact"`;
- a finished unit of work whose what and why git will not carry: `category="fact"`.

The gates do the policing, so writing freely is safe:

- the durability gate refuses transient state ("currently", "today I"); write the durable form, or pass `acknowledge_transient=True` (rare, logged);
- the credential gate refuses secret-shaped tokens; describe the secret instead, or pass `acknowledge_credential=True` (logged, kind only);
- the user-claim gate refuses a first-person claim filed as `fact`; re-file it as `user-inference`;
- dedup against the active set and the tombstones catches paraphrases; `memory_update` the matched id, or `memory_admin(action="restore", id=...)` when the matching tombstone's reason no longer holds. A duplicate credits the matched memory a corroboration: evidence the claim still holds, so do not force-write around it;
- the scope-mismatch gate asks for a re-scope when the body cites a project the declared scopes do not cover;
- with `groundedness_check=True` and `source_transcript`, sentences under 30 percent token overlap with the transcript come back as `ungrounded`; `acknowledge_ungrounded=True` when the grounding came from outside the transcript.

Declare `claims` (`path`, `path::symbol`, `path::NAME=literal`, `!path` for absent) when the body cites code: they are checked at write time and watched for drift afterwards. `supersedes` links this write to the ids it replaces; the stale hit then carries `superseded_by`. A write that diverges from a stored claim without a change cue is filed as a conflict for `memory_admin(action="conflicts")`.

Refining a stored fact is `memory_update(id, ...)`, never remove plus write; it keeps `created`. `links` replaces the typed edges (`supersedes`, `contradicts`, `extends`, `depends_on`) and shows both ways on `memory_show`.

## Episodes: the journal for run-state

Episodes are not memories: a sibling tier, no durability gate, pruned after 30 days, invisible to `memory_search`. Working state goes to `episode(action="write")` while the run is in flight:

- "iteration N tried X, fell over at step 3"
- "currently blocked on Y; next step is Z"
- "this branch's release plan" (state that changes weekly)

Only a takeaway that hardened into a durable fact becomes a memory, through `memory_write`, after the session that produced it. What did not harden expires on its own instead of becoming curation debt.

Loop iteration pattern:

1. **At entry**: `episode(action="handoff")` returns the prior session's takeaways in this worktree as `{prior_session_id, episodes: [{id, created, takeaway, scopes, provenance}]}`. Pass `include_bodies=True` when a takeaway needs its body (never delivered for an `unaccounted` episode). `prior_session_id` of `None` means no baseline; `episodes` of `[]` means the prior session left no journal.
2. **Each iteration**: `episode(action="write", body=..., takeaway="one line")`. Write it every time; the takeaway is what the next iteration sees first.
3. **Sub-agents**: a sub-agent passes the coordinator's session id as `swarm_id`; a sub-agent resuming a parent's work calls `episode(action="handoff", prior_session_id=<parent>)`.

## Curation

`memory_admin` is one action per call. `health` returns the store's health report (verification debt, drifted claims, dead weight, silent misses, rare scopes); `conflicts` lists the contradiction pairs the server flagged mechanically and takes your verdict (`a`, `b`, `both`, `neither`) with a `note`; `acknowledge_miss` marks a `search_miss` a false positive; `rename_scope` fixes a typo scope; `restore` and `tombstones` handle removals. Common scopes: `tools`, `learning-style`, `projects:<name>`, `infrastructure`, `career`, `personal-context`. Avoid the catch-all `general`.
