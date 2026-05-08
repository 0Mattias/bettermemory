# Contributing to bettermemory

Patches, bug reports, and design feedback all welcome. Most of what
follows is the small set of rules that exist so the project stays
predictable for downstream users.

## Local setup

See [`docs/installation.md`](docs/installation.md) for the install
side. For a development clone:

```sh
git clone https://github.com/0Mattias/bettermemory.git
cd bettermemory

# direnv handles UV_PROJECT_ENVIRONMENT=venv automatically; otherwise:
export UV_PROJECT_ENVIRONMENT=venv

uv sync --extra dev
source venv/bin/activate
```

The env directory is `venv/`, not `.venv/`, because macOS Sequoia
auto-applies `UF_HIDDEN` to anything literally named `.venv` inside
iCloud-synced folders. See the README's "macOS gotcha" section for
the full story.

## Running the suite

```sh
pytest -q                         # the whole suite
pytest tests/test_store.py        # one file
pytest -m "not no_extras"         # skip the embeddings-required slot

ruff check .
ruff format --check .
mypy

# Bench (not part of the test suite):
python bench/storage.py --sizes 1000,10000,50000
```

CI runs `ruff check . && ruff format --check . && mypy && pytest -q`
on Python 3.11 / 3.12 / 3.13 / 3.14 (Ubuntu) plus 3.13 macOS and
Windows slots, with an 80% coverage floor enforced via
`--cov-fail-under`. Anything that passes locally should pass CI;
anything that fails CI is blocking on merge.

## Pull request conventions

- One logical change per PR. Easier to review, easier to revert.
- Commit messages follow the form Claude Code is configured to emit
  (Conventional Commits: `feat:`, `fix:`, `docs:`, `test:`, `ci:`,
  `perf:`, `refactor:`). The body explains *why*, not just *what*.
  Several existing commits are good examples of the level of detail
  the project aims for.
- Update `CHANGELOG.md` under the `## Unreleased` heading with one
  of: `Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` /
  `Security`. Keep the entry to a couple of paragraphs at most,
  but include the *why* — readers come to the changelog for
  decisions, not just diffs.
- New tools, new configuration knobs, or anything else that
  expands the surface need a corresponding entry in
  [`docs/api.md`](docs/api.md), under the existing section
  taxonomy (Retrieval / Writing / Lifecycle / Verification /
  Curation / Session-local). Don't ship a tool whose contract
  isn't pinned in api.md.
- Tests are required for new behavior. The
  [`tests/`](tests/) directory has good examples of the
  hand-written + property-based mix the project aims for.

## Versioning and the 1.x compatibility contract

The project uses semver with the conventions below. The headline:
**within a 1.x line, the surface defined in
[`docs/api.md`](docs/api.md) and the on-disk format defined by
`models.SCHEMA_VERSION` are stable.** Strangers who pin
`bettermemory==1.x` get a contract they can rely on.

### Surface (the 17 MCP tools)

Stable within 1.x:

- Tool names. `memory_search` will not be renamed to `mem_search`
  in 1.x.
- Required parameter names and positions. `memory_remove(id,
  reason)` will not flip to `(reason, id)` in 1.x.
- Default values for optional parameters. `memory_search.expand_top`
  defaults to `False` in 1.x.
- Closed-set string values for enum-typed parameters. `confidence`
  is `"low"` / `"medium"` / `"high"` in 1.x; `outcome` is
  `"applied"` / `"ignored"` / `"contradicted"` in 1.x.
- Return-shape keys for the same status. A
  `memory_write` response with `status="duplicate"` will continue
  to carry a `matches` list in 1.x.

Permitted within 1.x:

- Adding new tools. Strangers don't break when their pinned client
  ignores tools it doesn't know about.
- Adding new optional parameters to existing tools, with defaults
  that preserve current behavior.
- Adding new fields to return shapes.
- Adding new return-status values to existing tools (clients should
  treat unknown status strings as a soft error and fall back to
  `memory_show`-style verification).
- Tightening validation in ways that turn previously-undefined
  inputs into clear errors. Loosening validation in ways that
  accept previously-rejected inputs is also permitted.

Forbidden within 1.x:

- Renaming a tool or parameter.
- Removing a tool or parameter.
- Changing the type of a parameter or return field.
- Changing the default value of an optional parameter.
- Changing the meaning of an enum value (e.g. redefining what
  `"applied"` means in `memory_record_use`).

### On-disk format (`models.SCHEMA_VERSION`)

`SCHEMA_VERSION = 1` is the constant in `src/bettermemory/models.py`.
Every memory and tombstone written by 1.x carries
`schema_version: 1` in its frontmatter. Readers default to `1`
when the field is missing (the implicit version of memories
written before the constant existed).

Within 1.x, all changes to the on-disk format are
**additive-only**: new optional frontmatter fields, never
renamed, never removed, never re-defined. A reader from a later
1.x minor will load files written by an earlier minor without any
migration step. A reader from an earlier minor will load files
written by a later minor as long as the later minor only added
fields the earlier reader doesn't recognize (and tolerates)
— which is the rule above.

### Deprecation cycle

When a tool, parameter, or field is destined for removal at the
next major bump:

1. The deprecation lands in a 1.x minor with a `Deprecated`
   entry in the changelog. The entry names the deprecated
   surface, the replacement (if any), and the planned-removal
   target version.
2. The implementation logs a one-time WARNING per process when
   the deprecated surface is used, with the same replacement
   pointer.
3. The deprecated surface continues to function — semver says
   so — until the next major bump (2.0).
4. At 2.0, the surface is removed. The 2.0 release notes
   reiterate every removed item.

Patches and bug fixes don't count as "uses" of the deprecated
surface for the WARNING; the warning fires when *callers* use
the surface. The implementation may continue to call into the
deprecated path internally for compatibility.

### Major bumps (2.0 and beyond)

A major bump is reserved for genuinely breaking changes:

- Any of the "forbidden within 1.x" list above.
- A non-additive on-disk format change (renamed or removed
  frontmatter fields, changed serialization for an existing
  field, change in the `.tombstones/` layout).
- A change in the relationship between tools (e.g. requiring
  `memory_write` to be paired with a `memory_record_use` call
  that's currently optional).

Every major bump ships with:

- A `Migration` section in `docs/release.md` (or its own
  `docs/migrations/<from>-to-<to>.md` if substantial) walking
  the user through the upgrade.
- A `bettermemory migrate <subcommand>` for any breaking
  on-disk-format change. The migration is idempotent
  (re-running is safe) and atomic per file (`.tmp` + rename).
  See `bettermemory migrate origin` (a 0.x → 0.x migration that
  shipped before this policy was written) for the existing
  pattern.

## Project values, in case they help review judgment

These aren't rules so much as the trade-offs the project makes
consistently. Helpful for guessing whether a change is in or
out of scope:

- **Memory is opt-in retrieval.** Anything that auto-injects
  context the model didn't ask for is the failure mode this
  project exists to fix. Default-to-not-retrieve over
  default-to-include.
- **False negatives beat false positives.** Missed context the
  user supplies in one followup turn is much cheaper than
  irrelevant context cascading through a conversation.
- **The on-disk format is the user's data.** It's plain markdown
  with YAML frontmatter so the user can `grep`, `git log`, and
  hand-edit it. Code that obfuscates the format (binary
  encoding, opaque hashing of the bodies, anything that
  requires the running server to interpret) is out.
- **Honest disclosure beats clever caveats.** The README's
  "Limitations" section lists what the project doesn't do. New
  limitations land there explicitly when discovered, rather
  than being papered over in a footnote elsewhere.
- **Static surfaces beat configuration.** Each new
  config-toml-knob is friction and documentation debt; default
  behavior should be sensible without ever editing the file.
  When a knob really is needed (`semantic_dedup`,
  `verification_stale_days`), it lives in
  `[behavior]` with prose explaining when to flip it.

## Releasing

Out of scope for typical contributions, but documented for
completeness in [`docs/release.md`](docs/release.md). Releases
are cut by project maintainers via tag push; trusted-publishing
on PyPI handles the rest.

## License

By contributing, you agree your contributions land under the
project's MIT license — see [`LICENSE`](LICENSE).
