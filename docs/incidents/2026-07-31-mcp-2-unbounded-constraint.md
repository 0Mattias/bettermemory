# 2026-07-31 — an unbounded dependency constraint made every published version un-installable

**Reported by:** self-found, by upgrading the maintainer's own global install at the start of a working session — `uv tool upgrade bettermemory` succeeded and the resulting binary would not start. Not by any instrument this project owns.
**bettermemory version at time of report:** 3.31.0, and — this is the material part — every version before it. See "Blast radius".
**Fixed in:** 3.31.1.
**Status:** fixed

## Symptom

A fresh install resolves, reports success, and cannot be imported:

```
$ uv pip install bettermemory
$ python -c "import bettermemory"
Traceback (most recent call last):
  File ".../bettermemory/builder.py", line 37, in <module>
    from mcp.server.fastmcp import FastMCP
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

There is no partial degradation to notice. `bettermemory` is an MCP server, `builder.py` is imported by `__init__.py`, and the import is module-level, so the failure is total and immediate: the CLI will not print `--version`, the server will not start, and a client configured to launch it gets a process that exits before it speaks a single frame.

## Blast radius

The instinct is to scope this to the newest release. That is wrong, and the correction is the reason this entry is filed rather than fixed quietly.

The constraint lives in the package metadata of every wheel already published. It is not re-resolved from the repository at install time; it is read from the artifact. So the defect is not "3.31.0 is broken" but **every published version of bettermemory was un-installable from PyPI**, including releases that were correct on the day they shipped:

```
$ uv pip install "bettermemory==3.30.0"     # released 2026-07-26, before mcp 2.0.0 existed
$ python -c "import bettermemory"
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

This removes the workaround that would normally absorb a bad release. A user who hits this cannot pin backwards to a known-good version, because there isn't one; the ordinary advice ("stay on the previous release until it's fixed") is unavailable. Publishing 3.31.1 was the only remedy, which is why it went out ahead of scheduled work rather than alongside it.

## Root cause

**An upper bound that was never there.** `pyproject.toml` has declared `mcp>=1.0.0` since `bac0d9a`, the initial commit (2026-05-07). No release ever narrowed it. For the project's whole 85-day life that was harmless, because there was no major above 1.

**Upstream removed the module, in a major, as it is entitled to.** mcp 2.0.0 deleted `mcp.server.fastmcp`; the successor is `mcp.server.mcpserver.MCPServer`. That is a correct thing to do in a major version bump, and the entire mechanism for surviving it — a `<2` cap — is the caller's responsibility. Four modules imported the deleted path at 3.31.0: `builder.py`, `handlers/_shared.py`, `session.py`, and `tests/test_resident_footprint.py`. That inventory is the count as it stood at the incident and is preserved as such; it is not the count today. The test module was rerouted through `tests/_mcp.py` in the pre-port prep, and the remaining three moved to `mcp.server.mcpserver` when the port shipped in 3.33.0 — see "Follow-on work" below.

**The timing was tight enough to be worth recording.** mcp 1.29.0 was uploaded to PyPI at 2026-07-28T13:41:40 and 2.0.0 at 13:45:28 — four minutes apart. bettermemory 3.31.0 was released three days later. It was therefore the first release published into a world where the unbounded constraint had a live 2.x to resolve to, and it broke on arrival without a single line of its own diff being at fault.

### Why CI was green through it

This is the part that belongs in this directory, because it is the same failure this project has now filed three times: **a check that reported green over the wrong input.**

Every job in `ci.yml` installs with `uv sync`, which obeys the committed `uv.lock`. The lock pinned `mcp==1.27.0`. So all nine legs — three platforms, four Pythons, both embeddings extras, pyright, mypy — installed a resolution that satisfied the *lock* and told us nothing whatsoever about the *constraint*. The release workflow gates the PyPI publish on that full matrix, so the gate was working exactly as designed and gated on the wrong question.

The distinction the test suite could not see:

- `uv.lock` records **a** resolution that worked once. It is the right input for reproducible test runs, and it is what makes a red CI leg mean "our code changed" rather than "the internet changed."
- `[project.dependencies]` is the contract **new installs** resolve against. It is what every user gets and what PyPI serves.

Nothing in the repository ever exercised the second one. The suite has 4,139 tests, several of which are specifically about honesty of published claims — `test_doc_claims`, the number-pinning guard, the changelog window check — and the installability of the artifact those claims describe was not among them. A lockfile is a snapshot of a past resolution; treating it as evidence about future ones is precisely the category error that `bench/rot` exists to make legible for memories.

## Fix

**The cap**, in `pyproject.toml`:

```toml
dependencies = [
    "mcp>=1.0.0,<2.0.0",
    ...
]
```

with a comment naming the four importing modules and pointing here, so the next person to widen it knows it is load-bearing rather than reflexive pessimism.

**The guard**, a new `install from declared constraints` job in `ci.yml` that deliberately does not use the lockfile:

```yaml
uv venv --python 3.13 /tmp/constraint-venv
uv pip install --python /tmp/constraint-venv/bin/python --resolution highest .
```

then imports the package, calls `build_server()`, and runs the CLI entry point. Two properties are load-bearing and one is deliberate belt-and-braces:

- **No lockfile.** This is the entire mechanism. A guard that reads `uv.lock` reproduces the blind spot it exists to close. Resolving the declared constraints afresh takes the newest allowed version of everything, which makes the job a *forward* alarm: the next upstream major that breaks us goes red on an ordinary push, not on a user's machine after a release. It follows that the job can go red with no local change — that is the design, and the remedy is a considered constraint edit or a port, never deleting the job.
- **Import and `build_server()`, not just install.** The failure was a module-level `ModuleNotFoundError`; a job that resolved and installed cleanly would still have shipped it. `build_server()` additionally exercises the SDK surface we actually call (tool registration), which is where a subtler upstream change would land instead.
- **`--resolution highest` spelled out.** This is uv's own default, so the flag changes nothing today; it is written explicitly because it is the behaviour the job's premise rests on, and a future `[tool.uv]` setting or upstream default change should not be able to disarm the alarm silently. It carries no claim about cache freshness — uv governs that separately, via `--refresh`.

## Verification

The guard was run against both trees before the fix was committed, because a guard that has not been observed failing is a guard whose teeth are hypothetical:

| Tree | Resolved `mcp` | Smoke test |
|---|---|---|
| `HEAD` before the fix (`mcp>=1.0.0`) | 2.0.0 | **exit 1** — `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` |
| With the cap (`mcp>=1.0.0,<2.0.0`) | 1.29.0 | exit 0 — `import + build_server OK` |

The negative control was produced from `git archive HEAD` into a scratch directory rather than by reverting in place, so the failing leg is the code as it was actually published.

Note the second row: the capped resolution picks 1.29.0, which is *newer* than the `uv.lock` pin of 1.27.0. The guard therefore also covers minor upstream movement inside the allowed range — a class of breakage the locked legs are structurally unable to see.

## What the surface should do differently

1. **A lockfile is evidence about the past; a constraint is a promise about the future. Test the promise.** Every CI leg here installed from the lock, which means the project verified — thoroughly, on three platforms — a configuration that no user of the published package ever receives. This is the same shape as the two false-green incidents below: the instrument was healthy, and it was pointed at the wrong input. The general form of the lesson is that reproducibility tooling and installability tooling answer different questions, and having a great deal of the first is not partial credit toward the second.
2. **The release gate should include "can it be installed".** `release.yml` gates the PyPI publish on the full CI matrix, which is a good gate that did not contain this question. It does now, because the new job is part of the reusable workflow the release gate consumes. Publish-time is the last moment the project controls; anything not asserted there is asserted by users.
3. **Unbounded majors are a bet that upstream will not take one.** `>=1.0.0` was written when the ecosystem had no 2.x and stayed correct by luck for the life of the project. The generalisable rule is not "cap everything" — caps have their own cost, and this one now has to be lifted deliberately — but that an unbounded major on a dependency whose *modules you import by path* is a standing bet, and bets should be visible. The comment at the constraint is that visibility.
4. **The maintainer's own upgrade is a test, and it should not be the one that finds this.** This was found because a session began with "make sure you're up to date" and the upgraded binary would not run. That is a real signal and it arrived three days late, from outside every instrument the project owns. Every check listed in this repository as an honesty mechanism concerns claims *in* the software; none concerned whether the software could be obtained.

## Follow-on work

Capping at `<2` restored installs and did not address mcp 2.x. The port — `mcp.server.fastmcp.FastMCP` → `mcp.server.mcpserver.MCPServer` — was tracked as a separate item rather than folded into a hotfix, on the reasoning that a rushed port of the SDK surface the entire tool sits on is a worse outcome than a supported 1.x for a few more days.

**Resolved in 3.33.0.** The bound is now `mcp>=2.0.0,<3.0.0`, and the order of operations held: the test-side reroute landed first (so the port edited one helper module rather than 83 sites), then the three `src/` changes, and the `install from declared constraints` job — which resolves without the lockfile — was green on the ported tree before the tag. Two things worth carrying forward from doing it this way:

- **The guard was run as a negative control, not just observed passing.** The ported tree against a clean `mcp==1.29.0` fails at `builder.py`'s import; the pre-port tree against the same install builds a server. So the floor is enforced by the code and not merely asserted by the metadata — the symmetry this directory's other entries kept failing to establish.
- **The cap outlived the port and now carries more weight than it did here.** Under mcp 1.x, `pydantic<3.0.0` was also inherited transitively from the SDK's own bound. mcp 2.0.0 declares `pydantic>=2.12.0` with no ceiling, so that inheritance is gone and the pydantic cap declared in `pyproject.toml` is the only one left. The gun this incident describes was re-armed by the port and is held down by a line that now has no backup.

## References

- Constraint introduced: `bac0d9a`, "Initial commit: memory-mcp v0.1.0" — unbounded from the first commit and never narrowed.
- Upstream: `mcp` 1.29.0 (2026-07-28T13:41:40) and 2.0.0 (2026-07-28T13:45:28) on PyPI; 2.0.0 removes `mcp.server.fastmcp` in favour of `mcp.server.mcpserver`.
- Fixed in 3.31.1, this commit; guard job `install from declared constraints` in `.github/workflows/ci.yml`.
- Importing modules, as of the port (all three now on `mcp.server.mcpserver`): `src/bettermemory/builder.py:37`, `src/bettermemory/handlers/_shared.py:24`, `src/bettermemory/session.py:59`. `tests/test_resident_footprint.py` no longer imports the SDK at all — it goes through `tests/_mcp.py`, which is what kept it collectable across the rename.
- Related incidents: [`2026-07-30-ingest-force-refused-by-its-own-gate.md`](2026-07-30-ingest-force-refused-by-its-own-gate.md), [`2026-07-26-staleness-verdict-constant-function.md`](2026-07-26-staleness-verdict-constant-function.md), [`2026-07-25-doctor-false-green-on-importable-extra.md`](2026-07-25-doctor-false-green-on-importable-extra.md) — the last two are false greens from an instrument pointed at the wrong input, which is this one's class exactly.
