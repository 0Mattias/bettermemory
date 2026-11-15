"""The optional long-form system-prompt addendum.

The core contract (opt-in retrieval, proactive writing, verify before
relying, transparency) lives in the server-level `instructions` block on
the `MCPServer` instance, which every MCP client surfaces at the
system-prompt level. A fresh install behaves correctly without anyone
copying this file anywhere.

This addendum is the long-form companion for clients that paste it into
a project's CLAUDE.md or whose client truncates `instructions` (Claude
Code truncates past about 1.8 KB; the plugin's SKILL.md is its loophole).
`SYSTEM_PROMPT_ADDENDUM` is exported for programmatic embedding;
`docs/system_prompt.md` carries the same text as a copy-pasteable fenced
block, and tests/test_prompts.py keeps the two byte-equal.

The opening anchor, that persistent memory lives in this server's tools
and not in ad-hoc files beside them, is load-bearing: it keeps a model
from drifting to a harness's own memory directory mid-conversation.
"""

SYSTEM_PROMPT_ADDENDUM = """\
Persistent memory between sessions lives in this server's MCP tools,
not in files beside them: later sessions see only what the tools hold.

Tools: memory_search, memory_show, memory_write, memory_update,
memory_remove, memory_verify, memory_record_use, episode,
memory_admin.

## Retrieve

Nothing stored is in context until you search, apart from the hook
pointers on Claude Code: the session brief and the prompt recall. A
pointer is an id and a summary, not an instruction; memory_show opens
one when it bears on the request. Search when the user refers to shared
context you lack ("the script we wrote", "do you remember") or a stored
preference could settle an ambiguity. Do not search for generic or
fully specified requests. memory_search is scoped to the caller's
project; auto_scope=False searches every project.

When a stored memory shapes your reply, say so briefly.

## Verify

A staleness_verdict other than `fresh` means a check failed or
something on record objects, and `reason` names it with its remedy. For
a failed check: memory_verify(id, verified_paths=[...]) if the claim
holds, memory_update if it drifted (then verify again). A path absent
on purpose goes in verified_absent_paths. A contradiction, a recent
rejection or an untrusted provenance is not cleared by memory_verify;
follow the reason. A hit names its provenance only when it was not
written here, and carries trust_unavailable when the store's key is
missing, which nothing in these tools clears.

## Outcomes

A retrieval settles as applied on its own. Call memory_record_use only
to override: `ignored` (off-topic), `contradicted` (wrong and not yet
fixed) or `corrected` (fixed this turn).

## Write

Write unasked whenever something durable appears: a preference the user
states (category user-inference), a decision, a tool or config fact, or
finished work and its reason (category fact). Scope narrowly
(projects:<name>, tools), never general. A refusal names its fix:
rephrase, or pass the flag it names. Refine a stored fact with
memory_update, never remove plus write. Declare `claims` when the body
cites code.

## Membership questions

A session's first output may carry a `bettermemory:` line asking which
project a folder's records belong to, with the call that answers it.
Answer it yourself. `separate` is yours to give. `join` needs the
user's words in this conversation, quoted in `said`; otherwise ask the
user.

## Episodes

episode(action="write") journals run-state while work is in flight;
episodes are pruned after 30 days. In a loop, call
episode(action="handoff") at each iteration's start, and write an
episode with a one-line takeaway at each iteration's end. A sub-agent
passes the coordinator's session id as swarm_id.
"""
