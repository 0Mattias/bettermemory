# Contributing to bettermemory

Patches, bug reports, and design feedback all welcome. Most of what follows is the small set of rules that exist so the project stays predictable for downstream users.

## Local setup

See [`docs/installation.md`](docs/installation.md) for the install side. For a development clone:

```sh
git clone https://github.com/0Mattias/bettermemory.git
cd bettermemory

# direnv handles UV_PROJECT_ENVIRONMENT=venv automatically; otherwise:
export UV_PROJECT_ENVIRONMENT=venv

uv sync --extra dev
source venv/bin/activate
```

The env directory is `venv/`, not `.venv/`, because macOS Sequoia auto-applies the `UF_HIDDEN` BSD flag to any directory whose name starts with a leading dot inside iCloud-synced folders. Once flagged, the directory is invisible to `ls` (without `-la`), to most TUIs, and to the Finder — including the editor file pickers that surface "your active venv". The `.envrc` and `pyproject.toml` therefore point at `venv/` (no leading dot). If you don't sync the project directory through iCloud you can use `.venv/` as usual, but the checked-in defaults assume the conservative path. Run `chflags nohidden .venv` to unhide a directory that's already been flagged.

## Running the suite

```sh
pytest -q                         # the whole suite
pytest tests/test_store.py        # one file
pytest -m "not no_extras"         # skip the embeddings-required slot

ruff check .
ruff format --check .
mypy                              # strict, primary type gate
mypy --platform win32             # the Windows leg's type errors, locally
pyright                           # secondary type gate (scoped to src/)

# Bench (not part of the test suite):
python bench/storage.py --sizes 1000,10000,50000
```

CI runs `uv sync --extra dev --extra ui` followed by `ruff check . && ruff format --check . && mypy && pytest -q` on Python 3.11, 3.12, 3.13, and 3.14 (Ubuntu) plus 3.14 macOS and Windows slots, with an 80% coverage floor enforced via `--cov-fail-under`. A separate job runs `pyright` (the secondary type gate, scoped to `src/`) on 3.14. The `[ui]` extra is installed alongside `[dev]` so mypy and pyright can resolve the `fastapi` / `uvicorn` imports in `src/bettermemory/web.py` (strict mode flags missing types on imported decorators) and so `tests/test_web.py` runs as actual coverage. Anything that fails CI is blocking on merge.

Note the scopes differ: `mypy` type-checks `src/` **and** `tests/`, `pyright` only `src/`. So a type error in a test file goes red via mypy, not pyright.

**A local green does not imply a green matrix.** The macOS dev loop cannot see the Windows slot, and POSIX-only attributes (`signal.SIGHUP`, `signal.SIGKILL`, `os.fork`, `fcntl`) sit behind `if sys.platform != "win32"` in typeshed — so a bare reference type-checks here and fails `windows-latest` with `attr-defined`. A `@pytest.mark.skipif(sys.platform == "win32")` marker does not help: mypy checks the whole file regardless of runtime markers. Guard the *reference*, not the execution — bind it once (`_SIGHUP: int = getattr(signal, "SIGHUP", 1)`) or use the raw signal number with a comment. `mypy --platform win32` selects the same conditional stubs and catches this in seconds instead of ~17 minutes of CI. It swaps stubs only, so it will not reproduce Windows *runtime* behavior (path handling, CRLF, the `fcntl` no-op locking fallback) — that still needs the real runner.

## Pull request conventions

- One logical change per PR. Easier to review, easier to revert.
- Commit messages follow the standard in [Commit messages](#commit-messages) below. It is enforced: the `commit messages` CI job lints every commit a push or PR introduces.
- Update `CHANGELOG.md` under the `## Unreleased` heading with one of: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, or `Security`. Keep the entry to a couple of paragraphs at most, but include the *why*. Readers come to the changelog for decisions, not just diffs.
- New tools, new configuration knobs, or anything else that expands the surface need a corresponding entry in [`docs/api.md`](docs/api.md), under the existing section taxonomy (Retrieval, Writing, Lifecycle, Verification, Curation, Session-local, Episodes). Do not ship a tool whose contract is not pinned in api.md.
- Tests are required for new behavior. The [`tests/`](tests/) directory has good examples of the hand-written plus property-based mix the project aims for.
- The Claude Code plugin scaffold at the repo root (`.claude-plugin/marketplace.json` and `plugin/`) carries its own version number that has to stay in sync with `pyproject.toml`. Bumping `pyproject.toml` without bumping `plugin/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` lights up the version-sync tests in [`tests/test_plugin.py`](tests/test_plugin.py); fix the manifest before pushing.

## Commit messages

The commit log is a permanent technical record that strangers read years later, usually while bisecting a regression at an inconvenient hour. Write it for that reader.

**Form.** Conventional Commits, subject at most 72 characters:

```
type(scope): imperative description of the change

Body: what changed and why, in plain declarative prose, wrapped at 100.
```

`type` is one of `feat`, `fix`, `docs`, `test`, `ci`, `perf`, `refactor`, `build`, `chore`, `style`, `revert`, `bench`, `release`. `scope` is optional and lowercase. The description starts lowercase, is written in the imperative mood ("release the lock", not "released the lock"), and does not end with a period. A `!` before the colon marks a breaking change.

**Register.** Describe the change, not the session that produced it, and not how anyone felt about it.

- **No first person.** No "I", "my", "myself". A quoted user utterance or a literal query is a citation and is exempt — `memory_search("how do I cut a release")` is fine.
- **No narration of the process.** "turns out", "finally!", "oops", "as promised" tell the reader nothing about the code. If a wrong first hypothesis is worth recording, record the *conclusion* it produced and the evidence, not the journey.
- **No aphorisms, jokes, or slogans in the subject.** The subject is an index entry. `perf(footprint): stop paying for prose and titles nobody reads` is a slogan; `perf(builder): drop pydantic schema titles from the served tool surface` is an index entry.
- **No anthropomorphism.** Guards do not have patience and ratchets cannot be taught. Say what the code now does.

**Body.** Explain *why* the change is correct and what it costs, at whatever length that honestly takes — this project's bodies are long on purpose and that is not the problem being corrected here. Cite files, symbols, measured numbers, and commit SHAs. Every published number must trace to a committed artifact; see [Project values](#project-values-in-case-they-help-review-judgment).

**Enforcement.** [`tools/commit_lint.py`](tools/commit_lint.py) encodes the mechanical rules — envelope, subject shape and length, blank line before the body, body wrapping, first person, filler. Tone is not machine-checkable and is left to review. The `commit messages` CI job lints exactly the commits a push or pull request introduces; history written before the rules landed is not re-graded. To catch a violation before it is recorded rather than after, install the hook once per clone:

```bash
git config core.hooksPath .githooks
```

## Versioning and the compatibility contract

The project uses semver with the conventions below. The headline: **within a major line, the surface defined in [`docs/api.md`](docs/api.md) and the on-disk format defined by `models.SCHEMA_VERSION` are stable.** Strangers who pin `bettermemory==3.x` get a contract they can rely on. The current major is 3; the same shape held for 1.x and 2.x and will hold for any future major line.

The 2.0 bump itself was a scope-only bump — nine 1.6-plan features shipped in one release. SCHEMA_VERSION stayed at 1, every new wire field was opt-in or absence-as-signal, and no 1.x surface was renamed or removed. The 3.0 bump was the same shape: a soft API break trimming defensive `bettermemory.server` re-exports after verifying zero in-tree consumers, packaged with the post-2.7.3 audit-loop. SCHEMA_VERSION stayed at 1 across both transitions; treat the rules below as continuous across the 1→2 and 2→3 boundaries — they describe the project's stance on stability, not a one-off cleanup.

### Surface (the 27 MCP tools)

Stable within the current major (3.x):

- Tool names. `memory_search` will not be renamed to `mem_search`.
- Required parameter names and positions. `memory_remove(id, reason)` will not flip to `(reason, id)`.
- Default values for optional parameters. `memory_search.expand_top` defaults to `False`; `memory_search.mode` defaults to `"hybrid"` (since 2.6.8); `memory_write.groundedness_check` defaults to `False` (since 2.0).
- Closed-set string values for enum-typed parameters. `confidence` is `"low"` / `"medium"` / `"high"`; `outcome` is `"applied"` / `"ignored"` / `"contradicted"` / `"corrected"`; `category` is `"fact"` / `"user-inference"` / `"ambient"`; `mode` is `"keyword"` / `"bm25"` / `"semantic"` / `"hybrid"`; `link.type` is `"supersedes"` / `"contradicts"` / `"extends"` / `"depends_on"`.
- Return-shape keys for the same status. A `memory_write` response with `status="duplicate"` will continue to carry a `matches` list; the `status="ungrounded"` value (from the optional groundedness gate) will continue to carry `claims`.

Permitted within a major:

- Adding new tools. Strangers do not break when their pinned client ignores tools it does not know about.
- Adding new optional parameters to existing tools, with defaults that preserve current behavior.
- Adding new fields to return shapes.
- Adding new return-status values to existing tools (clients should treat unknown status strings as a soft error and fall back to `memory_show`-style verification).
- Adding new enum values to the closed-set parameters above. Forward-compat: e.g. a future `link.type` like `"refines"` would load as an unknown link type on older readers without failing the whole record (the policy `MemoryLink`'s loader has enforced since 2.0).
- Tightening validation in ways that turn previously-undefined inputs into clear errors. Loosening validation in ways that accept previously-rejected inputs is also permitted.

Forbidden within a major:

- Renaming a tool or parameter.
- Removing a tool or parameter.
- Changing the type of a parameter or return field.
- Changing the default value of an optional parameter.
- Changing the meaning of an enum value (for example, redefining what `"applied"` means in `memory_record_use`).

### On-disk format (`models.SCHEMA_VERSION`)

`SCHEMA_VERSION = 1` is the constant in `src/bettermemory/models.py`. Every memory and tombstone written by 1.x, 2.x, and 3.x carries `schema_version: 1` in its frontmatter. Readers default to `1` when the field is missing (the implicit version of memories written before the constant existed). 2.0 added several optional frontmatter fields (the typed `links` list, the parallel `verified_paths` / `verified_commits` / `verified_versions` attestation lists, `origin.worktree_root`) but every one is purely additive: legacy memories load unchanged, and the constant stays at 1. 3.0 made no on-disk-format changes.

Within a major, all changes to the on-disk format are **additive only**: new optional frontmatter fields, never renamed, never removed, never re-defined. A reader from a later minor will load files written by an earlier minor without any migration step. A reader from an earlier minor will load files written by a later minor as long as the later minor only added fields the earlier reader does not recognize (and tolerates), which is the rule above.

### Deprecation cycle

When a tool, Python API, config key, parameter, or field is destined for removal at the next major bump:

1. The deprecation lands in a minor of the current major with a `Deprecated` entry in the changelog. The entry names the deprecated surface, the replacement (if any), and the planned-removal target version.
2. The implementation emits a runtime warning when the deprecated surface is used, with the same replacement pointer. Which channel carries the warning depends on who consumes the surface — two lanes, described below.
3. The deprecated surface continues to function, since semver says so, until the next major bump (4.0).
4. At 4.0, the surface is removed. The 4.0 release notes reiterate every removed item.

The two warning lanes, keyed on who the consumer is:

- **Python-API deprecations** (functions, methods, parameters — surfaces *code* imports and calls) use `warnings.warn(..., DeprecationWarning, stacklevel=2)`: raised per call, on Python's standard warnings channel. That channel is the one downstream tooling already hooks — this repo's message-pattern `filterwarnings` line escalates our own deprecations to test errors, and consumers get `-W` policy flags and their own pytest fences for free; display policy belongs to the consumer's filters, not to us. `stacklevel=2` attributes the warning to the caller's frame, so each call site is the one pointed at. The canonical example is the 4.0-removal trio in `src/bettermemory/origin.py` (`commits_since`, `commits_touching_pathspecs`, `commits_since_touching_paths`), whose messages follow the shape `<name> is deprecated and will be removed in bettermemory <version>; <replacement guidance>`. The phrase `deprecated and will be removed in bettermemory` is **load-bearing**: the message-scoped `filterwarnings` regex in `pyproject.toml` keys on exactly that text, and the "Deprecation fence" tests in `tests/test_origin.py` pin the regex against the emitted messages — a reworded message would silently escape the fence. Do not also log from API deprecations: production log readers are not the audience, and the warnings channel already carries it.
- **Config-key and other runtime-operational deprecations** (TOML keys — anything an operator *sets* rather than code calls) log a one-time WARNING per process via `log.warning`, guarded by a module-level seen-set — the pattern `_apply_legacy_endorsement_debt_alias` in `src/bettermemory/config.py` established for the 3.2.0 `endorsement_debt_ratio_threshold` rename. Their consumers read server logs, not the Python warnings channel: a `DeprecationWarning` is default-invisible in production (Python's default filters silence it outside `__main__`, and it lands on stderr rather than the log stream), and per-call logging would spam a long-lived server that rereads config on signal.

Patches and bug fixes do not count as "uses" of the deprecated surface for the warning; the warning fires when *callers* use the surface. The implementation may continue to call into the deprecated path internally for compatibility — in the API lane that means routing internal callers through a non-deprecated seam (see `_commits_touching_pathspecs_impl` in `origin.py`) rather than letting internal frames trip the warning.

### Major bumps (4.0 and beyond)

A major bump is reserved for genuinely breaking changes:

- Any of the "forbidden within a major" list above.
- A non-additive on-disk format change (renamed or removed frontmatter fields, changed serialization for an existing field, change in the `.tombstones/` layout, a `SCHEMA_VERSION` bump).
- A change in the relationship between tools (for example, requiring `memory_write` to be paired with a `memory_record_use` call that is currently optional).

The 2.0 and 3.0 releases are the examples of what does *not* require a hard-break major bump under this policy: 2.0 shipped nine additive features with no renames, and 3.0 trimmed defensive `bettermemory.server` re-exports after verifying zero in-tree consumers — a soft API break narrow enough to be the *only* break in the release. SCHEMA_VERSION stayed at 1 across both. Each bump was a scope signal to consumers ("the surface meaningfully grew" / "an import path you may have relied on is gone") rather than a wholesale compatibility break. A future major would carry a wider break.

Every major bump ships with:

- A `Migration` section in `docs/release.md` (or its own `docs/migrations/<from>-to-<to>.md` if substantial) walking the user through the upgrade.
- A `bettermemory migrate <subcommand>` for any breaking on-disk-format change. The migration is idempotent (re-running is safe) and atomic per file (`.tmp` plus rename). See `bettermemory migrate origin` (a 0.x to 0.x migration that shipped before this policy was written) for the existing pattern.

## Project values, in case they help review judgment

These are not rules so much as the trade-offs the project makes consistently. They are helpful for guessing whether a change is in or out of scope:

- **Memory is opt-in retrieval.** Anything that auto-injects context the model did not ask for is the failure mode this project exists to fix. Default-to-not-retrieve over default-to-include.
- **False negatives beat false positives.** Missed context the user supplies in one followup turn is much cheaper than irrelevant context cascading through a conversation.
- **The on-disk format is the user's data.** It is plain markdown with YAML frontmatter so the user can `grep`, `git log`, and hand-edit it. Code that obfuscates the format (binary encoding, opaque hashing of the bodies, anything that requires the running server to interpret) is out.
- **Honest disclosure beats clever caveats.** The README's "Limitations" section lists what the project does not do. New limitations land there explicitly when discovered, rather than being papered over in a footnote elsewhere.
- **Static surfaces beat configuration.** Each new config-toml-knob is friction and documentation debt; default behavior should be sensible without ever editing the file. When a knob really is needed (`semantic_dedup`, `verification_stale_days`), it lives in `[behavior]` with prose explaining when to flip it.

## Releasing

Out of scope for typical contributions, but documented for completeness in [`docs/release.md`](docs/release.md). Releases are cut by project maintainers via tag push; trusted-publishing on PyPI handles the rest.

## License

By contributing, you agree your contributions land under the project's MIT license. See [`LICENSE`](LICENSE).
