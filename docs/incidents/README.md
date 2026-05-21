# Incidents

This directory collects public postmortems for memory-rot bugs reported against bettermemory: cases where a stored memory misled the model in a way the verification surface should have caught and didn't.

## Why this exists

Memory-rot is the bug class bettermemory exists to make legible. When the verification trifecta (calendar age + path drift + commit drift) misses a case — or the staleness verdict fires when it shouldn't — that's the signal we most want to learn from. Each entry here documents the failure mode, the root cause, the fix, and what the verification surface should do differently next time.

Publishing these is deliberate. The competing memory systems don't surface their drift bugs because their architecture doesn't expose drift to begin with; staleness either fires or it doesn't, and the user never sees the misfire. bettermemory's contract is the opposite: the verdict is in the API response on every hit. That means we owe a public accounting when the verdict was wrong.

## Filing an incident

Open an issue at <https://github.com/0Mattias/bettermemory/issues> with the `memory-rot` label. Include:

- The memory body (redacted as needed) and the `staleness_verdict` it returned.
- What the model did with it — the reply that referenced a stale claim.
- What the verdict *should* have been, and what signal would have caught it.

If you'd rather not open a public issue, the same content emailed to the maintainer is fine. Once triaged, the postmortem lands here with the issue cross-linked.

## File layout

One file per incident, named `YYYY-MM-DD-short-slug.md`. Use [`TEMPLATE.md`](TEMPLATE.md) as the starting shape. Append the entry to the index below in reverse-chronological order.

## Index

_(No incidents yet. The first entry lands here when the first memory-rot report comes in.)_
