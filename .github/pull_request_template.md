<!--
  Thanks for contributing to bettermemory! A few quick checks below.
  See CONTRIBUTING.md for the full conventions; this template is the
  short reminder list, not the contract.

  Drop sections that don't apply.
-->

## What changed

<!-- One paragraph: the user-facing change. Skip the implementation —
the diff explains *what*; this section explains *what changed for the
caller*. -->

## Why

<!-- The problem this solves, or the design constraint that pushed
the change. The CONTRIBUTING note about commit-message bodies asks
for the same thing. -->

## Compatibility

<!-- Tick one: -->

- [ ] Additive — new tool, new optional argument, new config field.
      Compatible within the current major.
- [ ] Behavior fix — existing surface, corrected semantics. Compatible
      with anyone whose code didn't depend on the bug.
- [ ] Breaking — requires the next major. (If you check this, link the
      issue or discussion where the rename / removal was agreed. See
      CONTRIBUTING.md's "Versioning and the compatibility contract"
      for what counts as breaking within the current major line.)

## Checklist

- [ ] `pytest -q` passes locally
- [ ] `ruff check .` and `ruff format --check .` clean
- [ ] `mypy` clean
- [ ] CHANGELOG.md updated under `## Unreleased` (Added / Changed /
      Deprecated / Removed / Fixed / Security)
- [ ] Docs (README, `docs/api.md`, `docs/clients.md`, `docs/installation.md`)
      updated when the change is user-visible
- [ ] If touching the plugin scaffold, version bump in
      `pyproject.toml`, `plugin/.claude-plugin/plugin.json`, and
      `.claude-plugin/marketplace.json` is in sync (the
      `tests/test_plugin.py` version-sync tests will tell you if not)

## Notes for the reviewer

<!-- Optional: anything that would make the diff easier to land —
"the big move is in commit X; the rest is mechanical", "this is the
narrowest fix that addresses #N, design space discussed in #M", etc. -->
