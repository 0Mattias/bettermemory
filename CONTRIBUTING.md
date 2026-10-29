# Contributing to bettermemory

Patches, bug reports, and design feedback all welcome. Most of what follows is the small set of rules that exist so the project stays predictable for downstream users.

## Local setup

See [`docs/installation.md`](docs/installation.md) for the install side. For a development clone:

```sh
git clone https://github.com/0Mattias/bettermemory.git
cd bettermemory

# `.envrc` chooses the environment path: an in-repo `venv`, or a path
# outside the synced tree for a checkout under ~/Documents or ~/Desktop
# (see below). With direnv, `direnv allow` once per clone and it exports
# on every cd; without it, source the same rule rather than restating it.
. ./.envrc

uv sync --locked --extra dev
ln -sfn "$UV_PROJECT_ENVIRONMENT" .venv   # `[tool.pyright]` reads an in-repo `.venv`
source "$UV_PROJECT_ENVIRONMENT/bin/activate"
```

The default is `venv/`, not `.venv/`, because macOS Sequoia auto-applies the `UF_HIDDEN` BSD flag to any directory whose name starts with a leading dot inside iCloud-synced folders. Once flagged, the directory is invisible to `ls` (without `-la`), to most TUIs, and to the Finder, including the editor file pickers that surface "your active venv". Run `chflags nohidden .venv` to unhide a directory that's already been flagged.

Inside a synced folder the rename is not enough, because syncing itself damages an environment in place: duplicate `name 2.py` conflict copies, two dist-info directories for one package, a partially-materialised package tree that raises on import (see [`docs/incidents/2026-08-01-broken-optional-extra-killed-retrieval.md`](docs/incidents/2026-08-01-broken-optional-extra-killed-retrieval.md)). So [`.envrc`](.envrc) resolves a checkout under `~/Documents` or `~/Desktop` to `$HOME/.venvs/<checkout-dir-name>` (outside the synced tree, keyed by directory name so sibling worktrees keep separate environments) and leaves the in-repo `venv` default alone everywhere else.

## Running the suite

```sh
pytest -q                         # the whole suite
pytest tests/test_store.py        # one file

ruff check .
ruff format --check .
mypy                              # strict, primary type gate
mypy --platform win32             # the Windows leg's type errors, locally
pyright                           # secondary type gate

# Bench (not part of the test suite):
python bench/storage.py --sizes 1000,10000,50000
```

CI runs `uv sync --locked --extra dev`, then `ruff check .`, `ruff format --check .`, `mypy` and `pytest` with an 80% coverage floor (`--cov-fail-under=80`), on Python 3.11, 3.12, 3.13 and 3.14 on Ubuntu plus 3.14 on macOS and Windows. Separate jobs run `pyright` on 3.14, install the package from its declared constraints without `uv.lock` and import it, and lint the commit messages (see [Commit messages](#commit-messages)). Anything that fails CI is blocking on merge.

Note the scopes differ: `mypy` type-checks `src/` **and** `tests/`, `pyright` only `src/`. So a type error in a test file goes red via mypy, not pyright.

**A local green does not imply a green matrix.** The macOS dev loop cannot see the Windows slot, and POSIX-only attributes (`signal.SIGHUP`, `signal.SIGKILL`, `os.fork`, `fcntl`) sit behind `if sys.platform != "win32"` in typeshed, so a bare reference type-checks here and fails `windows-latest` with `attr-defined`. A `@pytest.mark.skipif(sys.platform == "win32")` marker does not help: mypy checks the whole file regardless of runtime markers. Guard the *reference*, not the execution: bind it once (`_SIGHUP: int = getattr(signal, "SIGHUP", 1)`) or use the raw signal number with a comment. `mypy --platform win32` selects the same conditional stubs and catches this in seconds instead of a full CI run. It swaps stubs only, so it will not reproduce Windows *runtime* behavior (path handling, CRLF, the `fcntl` no-op locking fallback); that still needs the real runner.

## Pull request conventions

- One logical change per PR. Easier to review, easier to revert.
- Commit messages follow the standard in [Commit messages](#commit-messages) below, and CI enforces it.
- Update `CHANGELOG.md` under the `## Unreleased` heading with one of: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, or `Security`. Keep the entry to a couple of paragraphs at most, but include the *why*. Readers come to the changelog for decisions, not just diffs.
- New tools and new tool parameters need an entry in [`docs/api.md`](docs/api.md) under its existing sections (Retrieval, Writing, Lifecycle, Verification, Curation, Session-local, Identity, Episodes, The store, Membership, CLI, Naming conventions). A new configuration key needs a commented entry in the `config.toml` template in `src/bettermemory/config.py` and a mention under Configuration in [`docs/internals.md`](docs/internals.md#configuration). Do not ship a tool whose contract is not pinned in api.md.
- Tests are required for new behavior. The [`tests/`](tests/) directory has good examples of the hand-written plus property-based mix the project aims for.
- A version bump lands on the seven fields [`docs/release.md`](docs/release.md#cutting-a-release) lists, among them the plugin scaffold's manifests (`plugin/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`) and both `server.json` fields. [`tests/test_version.py`](tests/test_version.py) and [`tests/test_plugin.py`](tests/test_plugin.py) fail when a manifest or the `uv.lock` self-entry disagrees with `pyproject.toml`; fix it before pushing.

## Commit messages

The commit log is a permanent technical record that strangers read years later, usually while bisecting a regression at an inconvenient hour. Write it for that reader.

**Form.** Conventional Commits, subject at most 72 characters:

```
type(scope): imperative description of the change

Body: what changed and why, in plain declarative prose, wrapped at 100.
```

`type` is one of `feat`, `fix`, `docs`, `test`, `ci`, `perf`, `refactor`, `build`, `chore`, `style`, `revert`, `bench`, `release`. `scope` is optional and lowercase. The description starts lowercase, is written in the imperative mood ("release the lock", not "released the lock"), and does not end with a period. A `!` before the colon marks a breaking change.

**Register.** Technical and declarative. Describe the change, not the session that produced it, and not how anyone felt about it.

- **No first person.** No "I", "my", "myself". A quoted user utterance or a literal query is a citation and is exempt: `memory_search("how do I cut a release")` is fine.
- **No narration of the process.** "turns out", "finally!", "oops", "as promised" tell the reader nothing about the code. If a wrong first hypothesis is worth recording, record the *conclusion* it produced and the evidence, not the journey.
- **No references to the sitting that produced the commit.** "this session", "the last round", "an adversarial pass over the previous six commits" name a boundary that exists only in the author's calendar. The reader is bisecting; give them the sha. A domain window named plainly ("a demotion window", "the tag range") is unaffected, but the rule matches the demonstratives regardless of what they point at, so "this window" about a race window in the code trips it. Waive that case by name with a `Lint-skip:` trailer rather than rephrasing around the linter.
- **No editorialising about the project's own record.** State what the code did and what it does now. That a defect was embarrassing, is the third of its kind, or is "worth stating plainly" grades the defect instead of describing it, and none of it is actionable. What *is* actionable survives: name the incident file, the defect class, the earlier commit.
- **No aphorisms, jokes, or slogans in the subject.** The subject is an index entry. `perf(footprint): stop paying for prose and titles nobody reads` is a slogan; `perf(builder): drop pydantic schema titles from the served tool surface` is an index entry. A `release:` subject is the version alone; the release's argument goes in the CHANGELOG entry the tag points at, which has room to cite.
- **No anthropomorphism.** Guards do not have patience and ratchets cannot be taught. Say what the code now does.

**Body.** Explain *why* the change is correct and what it costs. Cite files, symbols, measured numbers, and commit SHAs. Every published number must trace to a committed artifact; see [Project values](#project-values).

Length follows from that and from nothing else. A body earns each paragraph by carrying a fact the reader cannot get from the diff: a reproduction, a measurement, a rejected alternative, a consequence. Bodies here run long because the changes are usually load-bearing, not because length is the house style; a one-line body over a one-line fix is correct, and a long body that restates the diff in prose is not.

**Enforcement.** [`tools/commit_lint.py`](tools/commit_lint.py) encodes the mechanical rules: envelope, subject shape and length, blank line before the body, body wrapping, first person, filler, session references, editorialising, and the bare `release:` subject. The rest of tone is not machine-checkable and is left to review. The `commit messages` CI job lints exactly the commits a push or pull request introduces; history written before the rules landed is not re-graded.

Two escapes exist, both narrow. A message may waive one **named** wording rule with a `Lint-skip: <rule>` trailer (`Lint-skip: session-narration`) for the residual case where a pattern cannot tell a real ambiguity apart. There is no wildcard, the envelope rules are not waivable, and the waiver stays in the permanent record where review sees it. Separately, a commit git attributes to a bot (`dependabot[bot]` and friends) is not graded in `--range` mode, because its subject and body come from a template the author cannot edit; `.github/dependabot.yml` pins a conforming `commit-message.prefix` so the exemption carries as little as possible. To catch a violation before it is recorded rather than after, install the hook once per clone:

```bash
git config core.hooksPath .githooks
```

## Versioning and the compatibility contract

The project uses semver with the conventions below. The headline: **within a major line, the surface defined in [`docs/api.md`](docs/api.md) and the store schema versioned by `store.SCHEMA_VERSION` are stable.** Anyone who pins `bettermemory~=9.0` gets a contract they can rely on. The current major is 9. The rules below have held across every major line since 1.0; they describe the project's stance on stability, not a one-off cleanup.

### Surface (the nine MCP tools)

Stable within the current major (9.x):

- Tool names. `memory_search` will not be renamed to `mem_search`.
- Required parameter names and positions. `memory_remove(id, reason)` will not flip to `(reason, id)`.
- Default values for optional parameters. `memory_search.expand_top` defaults to `False`; `memory_search.mode` defaults to `"hybrid"` (since 2.6.8).
- Closed-set string values for enum-typed parameters. `confidence` is `"low"` / `"medium"` / `"high"`; `outcome` is `"applied"` / `"ignored"` / `"contradicted"` / `"corrected"`; `category` is `"fact"` / `"user-inference"` / `"ambient"`; `mode` is `"keyword"` / `"bm25"` / `"hybrid"` (the pre-4.0 `"semantic"` value went out with the embedding lane); `link.type` is `"supersedes"` / `"contradicts"` / `"extends"` / `"depends_on"`.
- Return-shape keys for the same status. A `memory_write` response with `status="duplicate"` will continue to carry a `matches` list.

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

### On-disk format (`store.SCHEMA_VERSION`)

`SCHEMA_VERSION = 1` is the constant in `src/bettermemory/store_schema.py` (re-exported by `store.py`), recorded in the store's `meta` table beside the tokenizer and engine fingerprints. The 9.0 store is one SQLite file whose tables are the fold of a hash-chained log; `models.SCHEMA_VERSION`, the frontmatter version every 1.x through 8.x memory file carried, stays at 1 for the frozen v8 readers and the mirror, which still read and write those files.

Within a major, all changes to the store schema are **additive only**: new nullable columns, new tables, new log kinds, never a renamed or removed column, never a re-defined one. A step that changes the data within that schema may still ship within a major when it raises at most `min_writer_revision` ("Store versions", below). A reader from a later minor opens a store written by an earlier minor without a migration step, and a reader from an earlier minor opens one written by a later minor as long as the later minor only added what the earlier reader ignores. A log kind added in a later minor is folded by an earlier reader as a telemetry row, which is the rule that keeps the chain verifiable across minors.

### Store versions: who may read and who may write

Two builds of one major meet on one store: the daemon keeps a newer daemon running beside an older client, and an older CLI writes the store directly. So a store records which builds may read it and which may write it, and every open checks (`store_schema.check_versions`). The vocabulary is two integers and a package version, each with one job; all four live in the store's `meta`:

- `schema_version` (`SCHEMA_VERSION`): the major line's format. It moves only with a major release, and every build refuses a store whose value is above its own, including the builds from before this contract.
- `schema_revision` (`SCHEMA_REVISION`, 1 in 9.0): what the store holds within that format. A change to what a build writes raises it.
- `min_reader_revision` and `min_writer_revision`: the lowest revision that may read the store, and the lowest that may write it. `MIN_READER_REVISION` and `MIN_WRITER_REVISION` are what a store a build creates carries.
- `engine_version`: the package version of the newest build that opened the store to write, in release order (`_daemon_client.version_key`, a local `+...` part left off). It names a build in messages; nothing gates on it.

The rule every open applies, in this order:

1. A build whose `SCHEMA_REVISION` is below `min_reader_revision` refuses the store (`StoreVersionError`, a `ValueError`), naming the newest writer.
2. A build whose `SCHEMA_REVISION` is below `min_writer_revision` opens the store for reading only: its connection refuses every write as on read-only media, and the owner's open neither rekeys, binds the key ring nor anchors. Its reads still answer: a tool resolves its caller in a transaction that is rolled back (`store_schema.writes_rolled_back`), and a write through a tool is refused in words.
3. Otherwise the store's schema steps run (`store_steps.run_steps`, below): the owner's open (the daemon, the CLI, the hooks and the migrations open through it) applies each step numbered above `schema_revision`, in order, and each records its number as the revision and raises the minimums to its own, never lowering one. The owner's open then names its build in `engine_version` when it comes later. A reader's open applies no step and writes nothing; while a step that is not additive is pending it reads the store as it stands with every write refused (`PRAGMA query_only`), as `backup verify` and `backup restore` read a bundle taken before the step.

A store from before the contract (9.0.0rc13 and earlier) carries no revision and no minimums; they read as 0, so every build opens it, and its first writing open records them with step 1. A value that is not a whole number refuses the open: `meta` is not authenticated, and guessing would admit a build the store's writer kept out or keep out one it admitted.

What a change raises, decided by who could be misled by it:

- A change older readers and writers both leave alone (a table or an index only newer builds read and write) raises `schema_revision` only.
- A change older writers would leave stale (a column or table every write must keep up, a value derived from fields an older writer edits) also raises `min_writer_revision` to the new revision: older builds still read the store and open it for reading only.
- A change older readers would misread (a renamed, removed or redefined column, a changed fold for an existing log kind, a new log kind that mutates a table older builds fold, which their `log verify` would then read as tampered) also raises `min_reader_revision`. Within a major that is not allowed: it is a major release, which moves `SCHEMA_VERSION` too, so the builds from before this contract refuse the store as well.

A change to the schema ships as a numbered step in `store_steps.STEPS`: the next number, which becomes `SCHEMA_REVISION`, with the minimum reader and writer revisions a store needs once the step has run, decided by the list above. `MIN_READER_REVISION` and `MIN_WRITER_REVISION` are the highest any step names, and `tests/test_store_steps.py` pins the three. `Store.create` writes every step's tables, so a new store starts at the newest revision, and every step is idempotent. `meta`'s `schema_steps` lists each step the runner applied, with its build, its time and its backup; no check reads it.

- An additive step adds only what a store lacks (`CREATE ... IF NOT EXISTS`, a column every reader may ignore), takes its own transaction, and leaves a file it cannot write as it is; the runner records it after it runs, so a crash between the two runs it again and finds nothing to add. Step 1, the membership tables and indexes of decision D9 (`_add_membership_tables`), is one; `Store.open` also runs it at every open, as before the runner. An additive step must not add a column to a table the log folds until refold compares over the columns both sides hold: a reader's open does not run the step, so that build's `log verify` of a store not yet opened to write, and its `backup verify` of every bundle taken before the step, would read the missing column as tampering.
- A step that is not additive changes what is there. The runner takes the store's write lock, reads the revision again under it, writes a backup bundle of the store as it stands (`backup.create_backup`, signed when the store's key is held), verifies it (`backup.verify_bundle`, which folds the bundle's log again: about a third of a second for 5,000 memories), then applies the step and records it in that one transaction. A crash or an error at any point leaves the store as it was, readable and writable by the builds that used it before, and the bundle in place; the next owner's open backs the store up again and runs the step. A step whose backup fails or does not verify does not run, and the open fails naming why. A reader's open of a store waiting on such a step reads the store as it stands and writes nothing, so a step must leave the store readable by its own build before it has run. The steps run before the owner's open claims the key ring (`Store.open`), so an owner's open of a second file that carries a store's id while the ring is bound to another runs a step and signs its bundle's manifest with the ring's key before it goes on as a reader; 9.0 ships no step that is not additive, and `backup create` already signs any file that carries the id.

A field a later build adds to a record (`Memory`, `TombstonedMemory`) travels in the payload of the record's put, is optional like every field added within a major, and raises no minimum: an older reader reads the record without it, and an older writer refuses every change to a record whose last put carries a field its model does not define (`UnknownFieldsError`, a `ValueError` naming the fields) rather than write the record back without it. Round-tripping the field would keep its bytes and lose whatever the later build ties to the fields the older one edits (a body edit clears the verification it vouched for), so the older writer leaves such a record to the build that understands it. The same holds for a removal, a restore and a purge; a prune and a scope rename leave such a record and go on to the rest. In the store such a field gets a column of its own, which an older build's fold leaves out (`store_rows._table_rows` compares the columns that build knows), never a place inside a column an older build compares, where that build's `log verify` would read every such row as tampered.

The builds from before this contract read none of the three revision keys: they open and write a store whatever its minimums say, and only `schema_version` keeps them out. They also drop a field they do not define when they write its record back.

### Deprecation cycle

When a tool, Python API, config key, parameter, or field is destined for removal at the next major bump:

1. The deprecation lands in a minor of the current major with a `Deprecated` entry in the changelog. The entry names the deprecated surface, the replacement (if any), and the planned-removal target version.
2. The implementation emits a runtime warning when the deprecated surface is used, with the same replacement pointer. Which channel carries the warning depends on who consumes the surface: two lanes, described below.
3. The deprecated surface keeps working, as semver requires, until the next major.
4. That major removes it, and its release notes list every removed item.

Write step 1's planned-removal target as the *next* major at the time of writing, and re-target it if the surface outlives that major. The `origin.py` trio below is the worked example of the failure mode: it was deprecated against 4.0, 4.0 and 5.0 both shipped as removal releases without taking it, and the messages went on naming a version that was already history. Nothing forces the removal except the release that decides to take it; 7.0 took it.

The two warning lanes, keyed on who the consumer is:

- **Python-API deprecations** (functions, methods and parameters, the surfaces *code* imports and calls) use `warnings.warn(..., DeprecationWarning, stacklevel=2)`: raised per call, on Python's standard warnings channel. That channel is the one downstream tooling already hooks: this repo's message-pattern `filterwarnings` line escalates our own deprecations to test errors, and consumers get `-W` policy flags and their own pytest fences for free; display policy belongs to the consumer's filters, not to us. `stacklevel=2` attributes the warning to the caller's frame, so each call site is the one pointed at. The worked example was the trio in `src/bettermemory/origin.py` (`commits_since`, `commits_touching_pathspecs`, `commits_since_touching_paths`): deprecated against 4.0, re-targeted at 6.0 and then 7.0, removed in 7.0.0. Its messages followed the shape `<name> is deprecated and will be removed in bettermemory <version>; <replacement guidance>`, which every future deprecation keeps. The phrase `deprecated and will be removed in bettermemory` is **load-bearing**: the message-scoped `filterwarnings` regex in `pyproject.toml` keys on exactly that text, and the "Deprecation fence" tests in `tests/test_origin.py` scan the package for every `DeprecationWarning` and pin its message against the regex, so a reworded message would silently escape the fence. Do not also log from API deprecations: production log readers are not the audience, and the warnings channel already carries it.
- **Config-key and other runtime-operational deprecations** (TOML keys: anything an operator *sets* rather than code calls) log a one-time WARNING per process via `log.warning`, guarded by a module-level seen-set, the pattern `_apply_legacy_endorsement_debt_alias` in `src/bettermemory/config.py` established for the 3.2.0 `endorsement_debt_ratio_threshold` rename. Their consumers read server logs, not the Python warnings channel: a `DeprecationWarning` is default-invisible in production (Python's default filters silence it outside `__main__`, and it lands on stderr rather than the log stream), and per-call logging would spam a long-lived server that rereads config on signal.

Patches and bug fixes do not count as "uses" of the deprecated surface for the warning; the warning fires when *callers* use the surface. The implementation may continue to call into the deprecated path internally for compatibility; in the API lane that means routing internal callers through a non-deprecated module-private seam rather than letting internal frames trip the warning (the pattern the deprecated commit-count trio in `src/bettermemory/origin.py` followed until 7.0.0 removed it).

### Major bumps

A major bump is reserved for genuinely breaking changes:

- Any of the "forbidden within a major" list above.
- A non-additive store schema change (a renamed or removed column or table, a changed fold for an existing log kind, a `SCHEMA_VERSION` bump).
- A change in the relationship between tools (for example, requiring `memory_write` to be paired with a `memory_record_use` call that is currently optional).

The 2.0 and 3.0 releases are the examples of what does *not* require a hard-break major bump under this policy: 2.0 shipped nine additive features with no renames, and 3.0 trimmed defensive `bettermemory.server` re-exports after verifying zero in-tree consumers, a soft API break narrow enough to be the *only* break in the release. Each bump was a scope signal to consumers ("the surface meaningfully grew" / "an import path you may have relied on is gone") rather than a wholesale compatibility break.

4.0, 5.0 and 9.0 are the examples of what *does*. The first two removed whole surfaces the "forbidden within a major" list protects (4.0 the embedding lane, 5.0 the web UI), and 9.0 replaced the store: one SQLite file with a signed log in place of a markdown directory with a derived index, twenty-seven tools reduced to nine, and ten off-by-default flags removed with their code. The 8.x directory is imported once by `bettermemory migrate v8` and never written again.

Every major bump ships with:

- A `Migration` section in the release's `CHANGELOG.md` entry (or its own `docs/migrations/<from>-to-<to>.md` if substantial) walking the user through the upgrade; 9.0.0 is the most recent example.
- A `bettermemory migrate <subcommand>` for any breaking store change. The migration is idempotent (re-running imports only what is new) and atomic (one transaction; a failure part way leaves the store as it was). `bettermemory migrate v8` is the pattern.

## High-leverage contributions

- Run `bettermemory eval` against your own usage and file anomalies.
  The silent-miss threshold rule is calibrated on one user's data;
  more distributions is the open question.
- Setup notes for MCP clients beyond the ones in
  [docs/clients.md](docs/clients.md).
- Reports of stored memories that misled you in a way the verification
  surface did not catch; those locate exactly where the drift
  detection needs to widen.

## Project values

These are not rules so much as the trade-offs the project makes consistently. Use them to judge whether a change is in or out of scope:

- **Memory is opt-in retrieval, guarded where it is not.** Indiscriminate auto-injection is the failure mode this project exists to fix. The two surfaces that put context in front of the model unasked, the session brief and the prompt recall, deliver pointers and never bodies: an id and an 80-character summary (the recall adds a matched snippet of at most 100 characters), eight standing and eight work pointers at most at session start and three per prompt, and no id twice in a session segment. Each has an off switch (`[behavior] session_brief = false`, `[behavior] prompt_recall = "off"`). Default-to-not-retrieve over default-to-include.
- **False negatives beat false positives.** Missed context the user supplies in one followup turn is much cheaper than irrelevant context cascading through a conversation.
- **The store is the user's data, and it keeps receipts.** One SQLite file the user can open with `sqlite3`, export as JSON or as the 8.x markdown directory, and audit with `bettermemory log verify`. Code that obfuscates the records (encoding bodies, hashing them, anything that requires the running server to read them back) is out; the log's MACs authenticate the rows; they do not hide them.
- **Disclosure beats caveats.** The "Limitations" section of [`docs/internals.md`](docs/internals.md) lists what the project does not do. New limitations land there explicitly when discovered, rather than being papered over in a footnote elsewhere.
- **Static surfaces beat configuration.** Each new config-toml-knob is friction and documentation debt; default behavior should be sensible without ever editing the file. When a knob really is needed (`verification_stale_days`, `conversational`), it lives in `[behavior]` with prose explaining when to flip it. 9.0 removed ten off-by-default flags with their code because none had earned its keep on a measurement.

## Releasing

Out of scope for typical contributions, but documented for completeness in [`docs/release.md`](docs/release.md). Releases are cut by project maintainers via tag push; trusted-publishing on PyPI handles the rest.

## License

By contributing, you agree your contributions land under the project's MIT license. See [`LICENSE`](LICENSE).
