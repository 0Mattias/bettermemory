# Release process

Releases are cut by pushing an annotated `v<X.Y.Z>` tag. The `Release` workflow (`.github/workflows/release.yml`) handles the rest: build, full gating suite, PyPI publish, and GitHub release. No tokens; no manual `twine upload` step.

## One-time setup: PyPI trusted publishing

Trusted publishing replaces long-lived API tokens with short-lived OIDC tokens minted per workflow run. The configuration lives on the PyPI side, not in this repo's secrets. There is nothing to copy into GitHub. Without a matching publisher record the publish step fails with `invalid-publisher` (see Troubleshooting).

Set up two trusted publishers, one per target:

### Production PyPI

1. Sign in at <https://pypi.org/manage/account/publishing/>.
2. Pick **"Add a new pending publisher"** if `bettermemory` does not yet exist on PyPI; pick **"Add a new publisher"** under the project if it does.
3. Fill in:
   - **PyPI Project Name**: `bettermemory`
   - **Owner**: `0Mattias`
   - **Repository name**: `bettermemory`
   - **Workflow name**: `release.yml`
   - **Environment name**: `pypi`

### TestPyPI

1. Sign in at <https://test.pypi.org/manage/account/publishing/>.
2. Same fields, except the **Environment name** is `testpypi`.

The `pypi` and `testpypi` environment names match the `environment:` blocks in `release.yml`. Keep them in sync if you rename either.

## Cutting a release

Working tree clean, on the branch the tag will point at (normally `main`), all CI green.

A release version has to land on **seven fields across six files**.
The version-sync tests in step 5 (`tests/test_version.py`,
`tests/test_plugin.py`) fail when any of the five manifest and lock
fields disagrees with `pyproject.toml`; nothing checks the CHANGELOG
heading, so check it by eye.

| Surface | Field |
| --- | --- |
| `pyproject.toml` | `[project] version` (source of truth) |
| `plugin/.claude-plugin/plugin.json` | `.version` |
| `.claude-plugin/marketplace.json` | `.metadata.version` |
| `server.json` | `.version` **and** `.packages[0].version` (two places) |
| `uv.lock` | the `bettermemory` self-entry (`uv lock` rewrites it) |
| `CHANGELOG.md` | the `## <X.Y.Z> - <date>` heading |

```sh
# 1. Bump the version in pyproject.toml. Edit by hand. Do not invoke
#    `uv version` or similar; that loses the trailing newline and
#    surfaces noise in the diff.
$EDITOR pyproject.toml

# 2. Bump the SAME version in the three manifests. server.json carries
#    it TWICE (the top-level `.version` and the nested package version
#    the registry validates against pypi.org), so it is the easiest one
#    to half-bump.
$EDITOR plugin/.claude-plugin/plugin.json    # `.version`
$EDITOR .claude-plugin/marketplace.json      # `.metadata.version`
$EDITOR server.json                          # `.version` AND
                                             # `.packages[0].version`

# 3. Move the relevant entries from the "Unreleased" section of
#    CHANGELOG.md into a new `## <X.Y.Z> - <date>` heading. Read
#    `git log <previous tag>..` beside it: every commit that changed
#    behaviour is represented in the entry, by phrase or by short SHA;
#    a bullet that covers several commits cites their short SHAs. No
#    test checks this window.
$EDITOR CHANGELOG.md

# 4. Refresh uv.lock so its editable self-entry tracks the new version.
#    `uv lock` rewrites only what changed. test_version.py guards this
#    LOCALLY, so a forgotten bump fails the suite here rather than
#    landing as a separate "sync uv.lock" follow-up commit later.
#    In CI the guard is `uv sync --locked` (7.17.3), which refuses to
#    re-resolve a drifted lock. Before that the CI half of this promise
#    did not hold: bare `uv sync` regenerated uv.lock in the runner
#    before pytest opened it, so the test compared pyproject to itself
#    and d555025 shipped a 6.2.0 pyproject against a 6.1.0 lock entry
#    with all legs green.
uv lock

# 5. Run the version-sync tests locally. `uv sync` first:
#    `bettermemory/_version.py` is written by the build hook at install
#    time, and a stale one fails test_version.py until the editable
#    install is refreshed.
uv sync --locked --extra dev
pytest tests/test_plugin.py tests/test_version.py -q

# 5b. IF THIS RELEASE CHANGES THE STORE (`store.SCHEMA_VERSION`, a table,
#     a fold), it ships a `bettermemory migrate <subcommand>` and the
#     release notes say so (CONTRIBUTING.md, the compatibility
#     contract); `bettermemory migrate v8` is the pattern. The daemon
#     needs no restart ritual: the first call from an upgraded shim or
#     hook replaces a running daemon of an older version (a newer one
#     is kept, so an older client never restarts it).

# 6. Commit, tag, push the branch, then the tag.
git commit -am "release: <X.Y.Z>"
git tag -a v<X.Y.Z> -m "v<X.Y.Z>"
git push origin <branch>
git push origin v<X.Y.Z>
```

The tag push triggers the `Release` workflow. Watch it from the Actions tab. On success:

- `bettermemory==<X.Y.Z>` is on PyPI.
- A GitHub release appears on the Releases page, with the sdist and wheel attached as assets. Its notes are the version's CHANGELOG section (`tools/changelog_section.py` prints it in the `github-release` job; the `build` job runs it first and fails when the tag's version has no section, so nothing reaches PyPI for such a tag), followed by GitHub's generated compare link (`generate_release_notes` in `release.yml`; merged pull requests would be listed there too, but this repository lands commits directly). For a stable tag the section is the summary under the `## <X.Y.Z> - <date>` heading, above the first candidate's subsection.
- For a stable tag, `publish-mcp.yml` lists the version in the MCP registry once the Release run succeeds.

## Pre-release tags

A tag whose version carries any letter (`v9.0.0rc1`, `v9.0.0b1`, `v9.0.0.dev1`) runs the same workflow with three differences, each keyed on that letter rather than on a flag. The test is any letter, so a `.postN` tag would be treated the same way; do not cut post-releases.

- PyPI gets the version, and resolvers skip it: `pip install bettermemory`, `uv tool install bettermemory` and the plugin's `uvx bettermemory` keep resolving the latest stable release until a stable tag follows. Asking for it by version (`uvx bettermemory==9.0.0rc1`) installs it.
- The GitHub release is created marked **pre-release**, so it never takes the "latest" slot (`release.yml`, the "Read the tag's kind" step).
- The MCP registry listing is skipped (`publish-mcp.yml`): the registry carries one version per server and a listing is what registry clients install by default. The stable tag that follows lists as usual.

A pre-release keeps the CHANGELOG heading as `## <X.Y.Z> - Unreleased`; the stable tag dates it.

## Dry run via TestPyPI

Before a real release (especially one with packaging changes), push to TestPyPI first via the manual trigger:

1. Set `<X.Y.Z>rcN` (or `<X.Y.Z>.devN`) on the version fields of steps 1 and 2 under Cutting a release, and run `uv lock` (step 4). The build and CI jobs run `uv sync --locked` and the version-sync tests, so a bump of `pyproject.toml` alone fails before anything is uploaded. TestPyPI rejects re-uploading a version and pip skips pre-releases by default, so a pre-release number keeps the candidate out of regular installs and lets you move to `rc2` or `rc3`.
2. Commit (do not tag).
3. Actions tab, then Release, then "Run workflow" on a branch whose name does not start with `v` (see Listing in the MCP registry), and target `testpypi`.
4. Verify the rendered project page on <https://test.pypi.org/project/bettermemory/>.
5. `pip install -i https://test.pypi.org/simple/ bettermemory==<X.Y.Z>rcN` into a scratch venv and smoke-test.

Once you are satisfied, set the real version on the same fields, run `uv lock`, commit, tag and push.

## Yanking a bad release

Trusted publishing does not change the yank flow. From PyPI's project page, go to "Manage", then "Releases", then "Options" on the bad version, then "Yank". Yanked releases are still installable when pinned exactly but stop being chosen by `pip install bettermemory` resolvers, which is the right behavior for "this version was published by accident."

## Listing in the MCP registry

`.github/workflows/publish-mcp.yml` submits `server.json` to the MCP registry.
The registry refuses a listing whose version is not yet on PyPI, so the trigger
has to *follow* the PyPI publish rather than race it off the `v*` tag push the
upstream guide suggests. Three triggers are declared, and on the automated path
the one that actually fires is `workflow_run` against the **Release** workflow:
it observes that run finishing, and on a tag push that run cannot conclude
`success` unless its `publish-pypi` job did (`publish-pypi` is not skipped on
a `push` event, and `github-release` further declares `needs: publish-pypi`).
The job is gated on a succeeded run whose `head_branch` starts with `v`, and it
checks out that ref rather than the default branch. Under `workflow_run` the
default checkout is wherever `main` points *now*, which would publish a
`server.json` describing some version other than the one that just went to
PyPI. A tag run carries its tag in `head_branch`, so a failed release lists
nothing, and a TestPyPI dispatch from `main` lists nothing. A dispatch from a
branch whose name starts with `v`, such as `v9`, passes the gate as well: the job checks out that branch and fails red at its tag check,
listing nothing.

`release: published` is the second trigger and is kept deliberately, but it
covers only a release cut by hand or with a PAT. A release created by
`release.yml`'s `github-release` job raises no run-starting event: GitHub
suppresses events raised by a job authenticating with the default
`GITHUB_TOKEN` (a recursion guard), and that job uses exactly that token.

It is a separate workflow rather than another job in `release.yml` so that a
registry outage cannot turn a good PyPI release red.

The precondition step reads the version's PyPI JSON endpoint before it calls the
publisher, and retries that read for up to two minutes: the endpoint lags the
upload by seconds, and the first 7.0.0 listing attempt read a 404 one second
before the same URL served the release. A failed listing is re-run with
`gh run rerun <id> --failed`, which keeps the `workflow_run` context and so
checks out the tag rather than `main`.

No secret is involved. Authentication is GitHub OIDC (`id-token: write`), and
the token's repository-owner claim is what authorises the
`io.github.0Mattias/*` namespace.

Two things the registry checks that are easy to break silently, both asserted
by the workflow before it calls the publisher so the log names which one
failed:

- **The `mcp-name:` marker in `README.md`.** The registry verifies PyPI
  ownership by finding `mcp-name: io.github.0Mattias/bettermemory` in the
  package description, which is built from the README. Nothing else reads that
  line, so deleting it as stray HTML would break listing and nothing else.
- **`server.json`'s two version fields.** The top-level `.version` and the
  nested package version must agree with each other and with the tag.

To list a release that predates this workflow, or to retry after a
registry-side failure, run it by hand: Actions → "Publish to MCP Registry" →
"Run workflow". A dispatch has no tag, so it publishes whatever `server.json`
currently says, unless that version is a pre-release, which is never listed.

## Troubleshooting

### `invalid-publisher: valid token, but no corresponding publisher`

The workflow built the wheel and sdist successfully, then the `Publish to PyPI` job failed at the OIDC token exchange. PyPI logs the OIDC claims it received. They look right (`repository`: `0Mattias/bettermemory`, `workflow_ref`: `.../release.yml@refs/tags/v<X.Y.Z>`, `environment`: `pypi`), but no trusted-publisher record on PyPI matches.

Cause: the PyPI-side trusted-publisher record (the "One-time setup" section above) has not been created yet, or one of the field values was entered wrong. The most common typos:

- Repository name has a stray prefix or the wrong casing. PyPI's matcher is case-sensitive on the repo name; not on the owner.
- `Workflow name` field set to the path (`.github/workflows/release.yml`) rather than the basename (`release.yml`).
- `Environment name` mismatched: the workflow uses `pypi` for prod PyPI and `testpypi` for TestPyPI. If you typed `production` or similar in the PyPI form, no match.

Fix:

1. Go to <https://pypi.org/manage/account/publishing/>.
2. If the project does not exist on PyPI yet, register a **Pending Publisher** with the exact field values from the "One-time setup" section. If it does exist, edit the existing publisher under that project to match.
3. Re-run the failed job of the tag's Release run without re-tagging: `gh run rerun <run-id> --failed`. The build is reused, the publish job mints a fresh OIDC token that now matches, and `github-release` follows as on any tag push. Or push a fresh patch tag (`v<X.Y.Z+1>`) once the trust setup works.

### `pyproject.toml version (...) does not match tag (...)`

The build job compares the tag with `pyproject.toml` and stops before any artifact ships. Bump the version fields, commit, then move the annotated tag to that commit:

```sh
git tag -af v<X.Y.Z> -m "v<X.Y.Z>"
git push --force origin v<X.Y.Z>
```

The forced push runs the workflow again.
