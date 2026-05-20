# Your AI's memory is rotting. Here's how to tell.

*Draft — May 2026. By Mattias Rask, author of [bettermemory](https://github.com/0Mattias/bettermemory).*

---

Last quarter I told my AI assistant something it dutifully wrote down:

> The auth middleware lives in `src/auth/middleware.py`. JWT verification is in `verify_token()`. Sessions are stored in Redis with a 24h TTL.

It saved that to its memory store. Three sentences, plain markdown on disk, scoped to my project. Months passed. The codebase moved on: `middleware.py` got split, `verify_token()` got renamed, Redis got swapped for Postgres. The memory didn't.

Last week, fresh session, the assistant pulled that memory back in.

> Using your stored note on the auth middleware: `verify_token()` in `src/auth/middleware.py`…

Every word in that sentence was wrong. The file moved. The function was renamed. The middleware isn't even where the JWT verification lives anymore. The model had no way to know. It read text from a file it considered authoritative, surfaced the text in a reply, and confidently misled me about my own codebase.

This is the memory-rot problem, and almost no production memory system surfaces it to the model. Mem0 stores facts with timestamps but doesn't tell you when the world moved underneath one. Zep tracks bi-temporal validity but only for facts you explicitly mark with an invalidation. Letta has tiered memory but no per-fact freshness signal. Claude Code's auto-memory and Anthropic's "Dreaming" pass refresh things asynchronously — the model still sees the result as a single black-box string. Cursor's Memories, Windsurf's Cascade, GitHub Copilot Memory, claude-mem, the dozen SQLite-FTS5 MCP clones — none of them tell the model *whether the memory it just retrieved can still be trusted*.

bettermemory's wedge is that it tells the model exactly that, on every retrieval.

## The staleness verdict

Every `memory_search` hit comes back with a `staleness_verdict` field that takes one of three values:

- `fresh` — verification is current AND no drift detected.
- `spot_check_recommended` — verification calendar-fresh, but the world moved (a path the body cites no longer exists, or the origin repo has commits since the last verification touching files the memory cites).
- `spot_check_required` — verification itself is stale or never happened. The drift signals are moot because the anchor is gone.

The verdict is rolled up from three orthogonal drift signals, each derived from facts the model can interpret:

**Calendar age.** Memories carry a `last_verified_at` timestamp. By default, once it exceeds 30 days, verification falls out of "fresh". Calendar drift is the cheapest signal and the least informative — it just says *time has passed*.

**Filesystem path drift.** Memory bodies frequently cite paths: `src/auth/middleware.py`, `~/.config/foo.toml`, `/etc/nginx/sites-available/api`. When the memory is read, bettermemory extracts every path-shaped token from the body and `stat`s it. Missing paths get counted in `path_drift_missing`. The extractor knows the difference between a filesystem path and a URL route — `` `/login` `` in backticks is treated as a route, not a file to look for. (That distinction is what the 2.4.0 release narrowed; it changed exactly one line of behavior, and the changelog rationale runs about 800 words.)

**Commit drift.** Memories captured from inside a git checkout carry their `origin`: the repo URL, the branch, the worktree root, and (optionally) `verified_paths`. When the model is searching from inside a checkout of that same repo, bettermemory asks: *how many commits have landed since `last_verified_at` that touched any path the memory claims to be true about?* That count comes back as `commit_drift_count`. Zero means the code hasn't moved underneath the memory; nonzero means it has.

Each signal is cheap on its own. The verdict is the rollup. The model gets a single field to branch on, with the underlying details available if it wants to inspect them.

Now compare with what the rest of the field does. Mem0's "temporal" memory means the entity has a `created_at` and the SDK can answer "when did you learn this?" — useful for some queries, but it doesn't tell the model that a path moved. Zep's bi-temporal model is more sophisticated: facts have `t_valid` and `t_invalid` windows. But those windows are written by the ingest pipeline; they don't fire automatically when the world moves. The end-state is the same: the model reads the fact and has to decide what to do with it on vibes.

When `staleness_verdict != "fresh"`, the bettermemory contract tells the model to spot-check one verifiable claim. If the claim holds, call `memory_verify(id, verified_paths=[…])` to re-attest. If it's drifted, `memory_update` the body first, then verify the corrected version. The verify call refreshes `last_verified_at` and records *which paths and commits the model checked*, so later drift signals know which subset of the body is anchored.

That's the trifecta. Most of bettermemory's distinctive engineering is in making each of those signals reliable enough that the model can act on them without ceremony.

## The claim-level audit trail

The other thing memory systems don't do: they don't tell you which sentence of the model's reply was shaped by which retrieved fact.

This matters because hallucination at *retrieval time* is a real failure mode. The model pulls back four memories. It writes a reply. Six months later you want to know: did that reply faithfully use the memories, or did it embroider? In every other system the answer is "good luck." The retrieval log shows what was returned; the response log shows what was said; nothing connects them.

bettermemory connects them. Every retrieval comes back with a `use_token`. When the model writes its reply, it calls:

```python
memory_record_use(
    memory_ids=["01HXYZ123ABC"],
    outcome="applied",
    claim_excerpts=["Sessions are stored in Redis with a 24h TTL."],
)
```

That single call is the audit hook. `outcome` records the verdict (`applied` / `ignored` / `contradicted` / `corrected`). `claim_excerpts` records the load-bearing sentence each memory actually contributed. If the model never calls `record_use`, the server auto-commits as `applied` two turns later — the common case handles itself.

Months later, you can walk every reply back to the specific claim it leaned on. You can ask: "of the times this memory got pulled in, what claims did it shape, and were any of them wrong?" You can identify retrievals that were nominally `applied` but produced contradictions downstream. You can build evals that score *whether a memory was load-bearing for a correct answer or a wrong one*.

I'm not aware of any other memory system that records this. Letta has Evals as a separate framework you run offline. Mem0's API will tell you what was retrieved but not what the model did with it. Zep's audit log is graph-level. The category — *claim-to-memory traceability* — is essentially uncontested.

## Endorsement debt: the dead-letter queue of retrieval

Here's a failure mode that exists in every memory system but only bettermemory surfaces.

You write a fact. The model retrieves it. The model doesn't actually use it — the auto-applied tag fires two turns later because nothing else got recorded, but the model never *deliberately* reached for it. It just kept appearing in search results, getting reflexively logged, never shaping a reply.

That memory is dead weight. Worse, it's dead weight that looks alive in your metrics: `retrieval_count` is high, `applied_count` is high. The signal that distinguishes "the model uses this often" from "the ranker keeps surfacing this and the model is too polite to flag it" is the ratio of *explicit* applied calls to *auto* applied calls.

bettermemory tracks that distinction. `memory_health` exposes a bucket called `endorsement_debt`: memories with `retrieval_count >= 5` but `explicit_applied_count == 0`. They look retrieved-and-used, but every single applied event was the silent auto-fallback. The model never deliberately said *yes, I used this*. That's a dead-letter pattern unique to the bettermemory closed loop — you can't see it without recording the auto/explicit split, and no other system does.

Companion signal: `silent_misses`. The opposite failure mode. The user asks a question that *should* have triggered a search but didn't. From the system's view that's invisible — no search means no event, no event means no metric. bettermemory closes the loop with `memory_audit_turn`: after a turn, re-run the configured ranker over the user's message and check whether a `memory_search` or `memory_show` fired. If a high-relevance hit existed and the model didn't reach for it, record a `search_miss` event.

These two — endorsement debt and silent misses — bracket the retrieval contract. One catches noise the ranker is feeding the model that's getting ignored; the other catches signal the ranker has and the model is missing. Together they give you the first product-shaped instance of *"did memory actually help me?"* — a question every memory layer should be able to answer, and none of the funded ones currently do.

## What the numbers look like

The 2.5.0 release ships `bettermemory eval`, a CLI that computes those rates over the existing event log. I ran it against my own store the day the release tagged. Thirty-day window, default settings:

```
$ bettermemory eval --json | jq '{counts, endorsement_rate, silent_miss_rate, endorsement_debt: .endorsement_debt.total}'
{
  "counts": {
    "retrieval_occurrences": 325,
    "applied_total": 143,
    "applied_explicit": 15,
    "turns_audited": 33,
    "silent_misses": 5
  },
  "endorsement_rate":  { "rate": 0.105, "ci95": [0.065, 0.166] },
  "silent_miss_rate":  { "rate": 0.152, "ci95": [0.067, 0.309] },
  "endorsement_debt": 7
}
```

A few of those numbers are worth sitting with:

- **Seven memories in endorsement debt.** Each was retrieved 10–34 times in the window. Each was *auto-applied* every time the implicit timer fired. None of them got an explicit endorsement. They look load-bearing in the retrieval count; they're dead weight in the ranker.
- **Silent miss rate 15.2%.** One out of every seven audited turns surfaced a high-relevance memory the model should have retrieved and didn't. The probe runs after the turn finishes, so it doesn't slow anything down — it just makes the retrieval contract's slippage visible.
- **`memory_helped_rate` shows 0%** in this run because the explicit-claim-excerpt flow only landed days before the eval window started — most events in the log were captured before models started attaching `claim_excerpts` on `record_use`. That zero is an *adoption signal*, not a verdict on the memory layer. If anything it's evidence the metric is honest enough to surface its own propagation gap rather than hiding it behind a flattering composite.

I am the author of bettermemory, dogfooding it daily, and these are my numbers. The fact that endorsement debt and silent miss rate both fire on me is the point. The competing systems aren't measuring this. Mine is measuring it on me, and the numbers are imperfect, and that imperfection is exactly the bug class that's been invisible everywhere else.

## Why nobody else does this

The honest answer: it's not where the money is.

The enterprise market for memory systems is selling *more memory, faster*. Mem0 raised $24M to be the universal memory API. Zep's pitch is temporal reasoning at scale on Neo4j or FalkorDB. Letta is platforming agent infrastructure. Anthropic's Dreaming is a managed-agent feature that reconciles memory in the background without exposing the signals to the model. Cursor and Windsurf and GitHub are building memory into their IDEs as a vendor feature. All of those moves make sense for those companies. None of them have a structural reason to expose *whether the memory has rotted* to the model in-context, because that question is hard to monetize.

The other reason: this lane is *engineering-heavy and product-thin*. Calendar age, path drift, commit drift, claim excerpts, endorsement debt — each is conceptually small but operationally non-trivial. You need to extract path-shaped tokens from natural language reliably enough that backticked URL routes don't get stat'd as files. You need to attribute commits to memories scoped at a worktree level. You need to plumb a `use_token` through the MCP wire protocol on every hit and rotate it correctly. You need a `memory_health` view that joins event logs against the active store fast enough to be interactive. None of that is glamorous. None of it scales out via funding rounds. It's the kind of thing that gets built when a single engineer is annoyed enough by their AI's memory rotting that they refuse to ship until the rot is legible.

That engineer is me, and bettermemory has been my project to make memory legible for about three weeks of release tags. The 2.x line shipped five releases in five days. The architecture is plain markdown on a filesystem, MIT-licensed, 18 MCP tools, a FastAPI curation UI, a git-based cross-machine sync, a 1063-test suite enforcing 80% coverage. It's not a startup. It's a thing that exists because I couldn't find one that did.

## Try it

```sh
uv tool install bettermemory
bettermemory init --client claude-code  # or claude-desktop, cursor, continue, cline
```

Or, for Claude Code users:

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

The plugin lands an MCP server, a memory-discipline skill, and a Stop hook that runs the silent-miss audit. The first time the model retrieves a memory and the staleness verdict isn't `fresh`, it will spot-check before relying. The first time it pulls a memory back from a directory you `mv`'d last week, the verdict will tell you. The first time a memory gets pulled in but doesn't actually shape a reply, the endorsement-debt counter will tick up.

If you find a case where it's wrong — a verdict that fires when it shouldn't, or doesn't when it should — open an issue. The threshold rule for the silent-miss probe is versioned (`v1_top1_high`) and calibrated against my own usage; recalibrating it against more usage patterns is the open question for the next release. The verification surface is where this project plants its flag — the place memory rot becomes visible to the model that depends on it.

---

*Discussion: HN | Lobsters | r/LocalLLaMA | r/ClaudeAI*

*Source: [github.com/0Mattias/bettermemory](https://github.com/0Mattias/bettermemory). Memory rot is a real problem; if this resonates, the simplest help is to star the repo, file an issue describing how memory has rotted in your own workflow, or send a PR.*
