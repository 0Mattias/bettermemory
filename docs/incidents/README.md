# Incidents

This directory collects public postmortems for defects in the surfaces this project asks you to trust: the staleness verdict and its drift legs first, and any check whose job is to tell you that memory has gone wrong. Whether a user reported it or one of our own instruments found it makes no difference to the accounting.

## Why this exists

Memory-rot is the bug class bettermemory exists to make legible. When the verification trifecta (calendar age + path drift + commit drift) misses a case — or the staleness verdict fires when it shouldn't — that's the signal we most want to learn from. A check that reports green over the wrong input belongs here too: `doctor`, the benchmarks and CI all make the same promise the verdict does, and a false green from any of them costs the reader the same thing. That includes greens about the package itself rather than the memories in it — a test matrix that passes while the published artifact cannot be installed is the same failure aimed at a different surface, and the reader is misled in the same direction. Each entry documents the failure mode, the root cause, the fix, and what the surface should do differently next time.

Publishing these is deliberate. The competing memory systems don't surface their drift bugs because their architecture doesn't expose drift to begin with; staleness either fires or it doesn't, and the user never sees the misfire. bettermemory's contract is the opposite: the verdict is in the API response on every hit. That means we owe a public accounting when the verdict was wrong — including, and so far mostly, when we found it ourselves by pointing an instrument at the product.

## Filing an incident

Open an issue at <https://github.com/0Mattias/bettermemory/issues> with the `memory-rot` label. Include:

- The memory body (redacted as needed) and the `staleness_verdict` it returned.
- What the model did with it — the reply that referenced a stale claim.
- What the verdict *should* have been, and what signal would have caught it.

If what misled you was a check rather than a memory — a `doctor` green that should have warned — report what it said and what the store actually was instead of the bullets above.

If you'd rather not open a public issue, the same content emailed to the maintainer is fine. Once triaged, the postmortem lands here with the issue cross-linked.

## File layout

One file per incident, named `YYYY-MM-DD-short-slug.md`. Use [`TEMPLATE.md`](TEMPLATE.md) as the starting shape. Append the entry to the index below in reverse-chronological order.

**Reported by** names whoever or whatever found it: an issue link, "private report", or the instrument that surfaced it (`bench/rot`, `doctor`, a live-store sweep). Self-found via a benchmark is a legitimate reporter — it is how most of these arrive.

## Index

- [2026-07-31 — index health certified a stale index](2026-07-31-index-health-certified-a-stale-index.md). `doctor` reported "Index healthy … matches disk" over an index that had stopped describing the store: equal row counts returned `ok` before any identity or content comparison ran, so an out-of-band swap (remove one memory, add another) and a hand-edit of a body in place both certified clean. Third occurrence of the same check certifying a broken index. Found by an audit pass pointed at `doctor` itself; fixed on `main` after 3.33.0.
- [2026-07-31 — an unbounded dependency constraint made every published version un-installable](2026-07-31-mcp-2-unbounded-constraint.md). `mcp>=1.0.0` had no upper bound; mcp 2.0.0 deleted the module four of ours import, so `pip install bettermemory` resolved, installed, and would not import — for every release on PyPI, not just the newest. All nine CI legs were green because they install from `uv.lock`, which pins a resolution no user receives. Found by upgrading the maintainer's own install; fixed in 3.31.1.
- [2026-07-30 — `ingest --force` was refused by the gate it was passed to bypass](2026-07-30-ingest-force-refused-by-its-own-gate.md). The flag reached the plan and not the commit, so a forced import wrote nothing and reported a refusal telling the operator to pass `force=True`. The guard test had asserted the plan and stopped one call short since the flag shipped. Found by the write-path audit hours after `0073c70` introduced it; never in a release.
- [2026-07-26 — the staleness verdict was a constant function](2026-07-26-staleness-verdict-constant-function.md). Past the freshness window the calendar leg pre-empted both drift legs, so every calendar-stale memory read `spot_check_required` regardless of drift. Found by `bench/rot` (Youden's J = 0.000 at the shipped default); fixed in v3.30.0.
- [2026-07-25 — an importable extra was read as a working semantic leg](2026-07-25-doctor-false-green-on-importable-extra.md). `doctor`'s new `retrieval_discrimination` check short-circuited to `ok` whenever an embeddings package merely imported, silencing itself for the default config it was written for. Found and fixed inside the v3.29.0 window.
