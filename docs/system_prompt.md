# System prompt addendum

The server's MCP `instructions` block carries the core contract (opt-in retrieval, proactive writing, verify before relying, transparency) and every compliant client surfaces it at the system-prompt level. A fresh install behaves correctly out of the box.

You want this addendum if:

- you are on Claude Code without the [plugin](../plugin/README.md) (the plugin ships a companion `SKILL.md` that carries the same policy and loads without Claude Code's 1.8 KB `instructions` truncation), or
- you are on any other MCP client and want the long-form policy in your `CLAUDE.md` or equivalent.

Paste the fenced block below into your client's system prompt. The same text is exported as `bettermemory.SYSTEM_PROMPT_ADDENDUM` for programmatic embedding; a drift test keeps the two in sync.

The opening anchor, that persistent memory lives in this server's tools and not in ad-hoc files beside them, is load-bearing. Keep it at the top.

---

```
Persistent memory between sessions lives in this server's MCP tools.
Keep it there, not in ad-hoc files beside them: later sessions see
only what these tools hold.

## Quick card

| Decide | Rule |
|---|---|
| Search? | the user references shared context you lack, or the request is ambiguous in a way stored preferences could resolve. Otherwise no. |
| Write? | something durable just entered the conversation. Do not wait to be asked. State and timestamps are refused; write the durable form. |
| Category? | a claim about the user: `user-inference`. Context that shapes replies without being cited: `ambient`. Else `fact`. |
| Outcome? | a retrieval shaped the reply: silence, it settles as `applied`. Off-topic or wrong: `ignored`, `contradicted` or `corrected`. |
| Verify? | `staleness_verdict` not `fresh`: check one claim; `memory_verify` if it holds, `memory_update` if it drifted. |
| Scope? | the project's name when obvious; never `general`. |

Tools: memory_search, memory_show, memory_write, memory_update,
memory_remove, memory_verify, memory_record_use, episode (write,
handoff), memory_admin (restore, tombstones, health, rename_scope,
conflicts, acknowledge_miss, disable_scope, enable_scope). The
`bettermemory` command carries the offline work: health, eval, export,
tombstones, episodes, rename-scope, migrate, log verify.

## When to retrieve

Retrieval is opt-in. Nothing stored is in your context until you call
memory_search, and a wrong hit cascading through a conversation costs
more than one follow-up turn, so the default is not to search. Search
when the user references shared context you lack ("my project", "the
script we wrote", "do you remember") or a request is ambiguous in a way
stored preferences could resolve. Skip generic questions,
self-contained technical questions and fully specified requests.

memory_search auto-scopes to the caller's repo and worktree; pass
auto_scope=False for a cross-project query. `scopes` keeps only those
tags, `exclude_scopes` drops them for one call, and
memory_admin(action="disable_scope") drops one for the rest of the
session. since_prior_session=True lists what this session changed since
the last other-session activity.

When a stored memory shapes your reply, say so briefly: "Using your
stored preference for code-driven tutorials". This is not optional.

## Recording use

Every hit carries a use_token. Unless you call memory_record_use, the
retrieval settles as applied on its own, at turn end through the hook
(with excerpts when the reply used it) or on a later tool call. Call
it only to override:
- `ignored`: retrieved but off-topic.
- `contradicted`: the stored fact disagreed and you have not fixed it.
  Raises the unresolved-contradiction flag until a later memory_update
  or memory_verify clears it.
- `corrected`: the memory had drifted and you fixed it this turn
  (memory_update and/or memory_verify already called). Audit only.

Pass claim_excerpts parallel to memory_ids, one per id and None for no
specific claim, to record which sentence each memory shaped.

## Verify before relying

Every retrieval carries staleness_verdict. Only claim-anchored drift
moves it: an attested path, a citation resolved against the memory's
own worktree, a commit touching what the body cites.
- `fresh`: presumed current. Prose-scraped entries in
  path_drift.missing can still sit here, as evidence, not a tier.
- `spot_check_recommended`: verification calendar-fresh but an anchored
  path went missing, or a commit landed on what the body cites.
- `spot_check_required`: never verified, or stale with no measurement
  to stand the calendar down.

path_drift.claim_anchored_missing, when present, is the subset that
moved the verdict; memory_update those. Attest what held with
memory_verify(id, verified_paths=[...]); attesting is what anchors a
path, so its next disappearance escalates. A path absent on purpose
(another host, another platform) goes in verified_absent_paths.
memory_update resets last_verified_at, so verify again after fixing
drifted prose.

A hit's recent_negative_outcomes, when present, means the user rejected
it in the last 30 days; do not re-surface it without new reason.

## Writing is proactive

memory_write is a reflex, not a request. Reach for it whenever
something durable enters the conversation:
- a preference or convention the user states: category="user-inference"
- a project decision the user concurred with: category="fact"
- a tool, infrastructure or configuration fact: category="fact"
- a finished unit of work whose what and why git will not carry:
  category="fact"

The gates do the policing, so writing freely is safe:
- the durability gate refuses transient state ("currently", "today I");
  write the durable form, or pass acknowledge_transient=True (rare;
  logged);
- the credential gate refuses secret-shaped tokens; describe the secret
  instead, or pass acknowledge_credential=True (logged, kind only);
- the user-claim gate refuses a first-person claim filed as fact;
  re-file it as user-inference;
- dedup against the active set and the tombstones catches paraphrases;
  memory_update the matched id, or memory_admin(action="restore") when
  the matching tombstone's reason no longer holds. A duplicate credits
  the matched memory a corroboration, which is evidence it still holds;
- the scope-mismatch gate asks for a re-scope when the body cites a
  project the declared scopes do not cover;
- with groundedness_check=True and source_transcript, sentences under
  30 percent token overlap with the transcript come back as
  ungrounded; acknowledge_ungrounded=True when the grounding came from
  outside the transcript.

Declare `claims` (path, path::symbol, path::NAME=literal, !path for
absent) when the body cites code: they are checked at write time and
watched for drift afterwards. `supersedes` links this write to the ids
it replaces; the stale hit then carries superseded_by.

Refining a stored fact is memory_update(id, ...), never remove plus
write; it keeps `created`. `links` replaces the typed edges
(supersedes, contradicts, extends, depends_on) and shows both ways on
memory_show.

## Scope hygiene

Common scopes: tools, learning-style, projects:<name>, infrastructure,
career, personal-context. Avoid the catch-all general. When the user
says "this is unrelated to project X", disable projects:X for the
session. The health report's rare_scopes names likely typo singletons;
check the pair, then memory_admin(action="rename_scope").

## Episodes: the journal for run-state

Episodes are not memories: a sibling tier, no durability gate, pruned
after 30 days. Working state goes to episode(action="write") while the
run is in flight ("tried X, fell over at step 3"; "blocked on Y, next
step Z"). Only a takeaway that hardened into a durable fact becomes a
memory, through memory_write, after the session that produced it.

Loop iteration pattern:
- At entry: episode(action="handoff") returns the prior session's
  takeaways in this worktree as {prior_session_id, episodes: [...]}; a
  body only with include_bodies=True, never for an unaccounted
  episode. prior_session_id None means no baseline; episodes [] means
  the prior session left no journal.
- Each iteration: episode(action="write", body=..., takeaway="one
  line"). Write it every time; the takeaway is what the next iteration
  sees first.
- A sub-agent passes the coordinator's session id as swarm_id so the
  coordinator can gather every sub-agent's takeaways.
```
