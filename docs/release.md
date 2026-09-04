# Release process

Releases are cut by pushing an annotated `v<X.Y.Z>` tag. The `Release` workflow (`.github/workflows/release.yml`) handles the rest: build, full gating suite, PyPI publish, and GitHub release. No tokens; no manual `twine upload` step.

> **Before you push the very first tag, configure PyPI trusted publishing first** (next section). Without it, the workflow builds the wheel cleanly and then fails the publish step with `invalid-publisher: valid token, but no corresponding publisher`. The build itself succeeds, so re-running the workflow after the PyPI-side setup is straightforward, but it is strictly less painful to set up the publisher record before tagging.

## One-time setup: PyPI trusted publishing

Trusted publishing replaces long-lived API tokens with short-lived OIDC tokens minted per workflow run. The configuration lives on the PyPI side, not in this repo's secrets. There is nothing to copy into GitHub.

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

Working tree clean, on `main`, all CI green.

A release version has to land on **seven fields across six files**.
Miss one and the version-sync suite in step 5 fails rather than the
release shipping skewed — but you still have to know the file is on
the list to bump it:

| Surface | Field |
| --- | --- |
| `pyproject.toml` | `[project] version` (source of truth) |
| `plugin/.claude-plugin/plugin.json` | `.version` |
| `.claude-plugin/marketplace.json` | `.metadata.version` |
| `server.json` | `.version` **and** `.packages[0].version` — two places |
| `uv.lock` | the `bettermemory` self-entry (`uv lock` rewrites it) |
| `CHANGELOG.md` | the `## <X.Y.Z> - <date>` heading |

```sh
# 1. Bump the version in pyproject.toml. Edit by hand. Do not invoke
#    `uv version` or similar; that loses the trailing newline and
#    surfaces noise in the diff.
$EDITOR pyproject.toml

# 2. Bump the SAME version in the three manifests. server.json carries
#    it TWICE — the top-level `.version` and the nested package version
#    the registry validates against pypi.org — so it is the easiest one
#    to half-bump. The version-sync tests in tests/test_plugin.py and
#    tests/test_version.py fail if any of these drift apart. They are
#    the guardrail for the release ritual.
$EDITOR plugin/.claude-plugin/plugin.json    # `.version`
$EDITOR .claude-plugin/marketplace.json      # `.metadata.version`
$EDITOR server.json                          # `.version` AND
                                             # `.packages[0].version`

# 3. Move the relevant entries from the "Unreleased" section of
#    CHANGELOG.md into a new `## <X.Y.Z> - <date>` heading.
$EDITOR CHANGELOG.md

# 4. Refresh uv.lock so its editable self-entry tracks the new version.
#    `uv lock` rewrites only what changed. test_version.py guards this,
#    so a forgotten bump fails the suite here rather than landing as a
#    separate "sync uv.lock" follow-up commit later.
uv lock

# 5. Run the suite locally. The version-sync tests are the cheapest
#    check that all version surfaces agree (pyproject.toml is the source
#    of truth for `bettermemory.__version__`, which the plugin
#    manifests, both server.json fields, and uv.lock's self-entry must
#    match). test_changelog.py is included because it pins the
#    `## <X.Y.Z> - <date>` heading from step 3 — a
#    forgotten heading otherwise passes here and only trips in the full
#    release-workflow suite. Its release-window coverage check is inert
#    at this point (the new tag doesn't exist yet), which is why step 6
#    re-runs the file after tagging.
pytest tests/test_plugin.py tests/test_version.py tests/test_changelog.py -q

# 6. Commit, tag, re-check the changelog against the tag, then push.
#    The tag-window coverage check only sees the new tag once it
#    exists, and a coverage gap caught here costs a local re-tag
#    (`git tag -d v<X.Y.Z>`) instead of a post-release erratum.
git commit -am "release: <X.Y.Z>"
git tag -a v<X.Y.Z> -m "v<X.Y.Z>"
pytest tests/test_changelog.py -q
git push origin main
git push origin v<X.Y.Z>
```

The tag push triggers the `Release` workflow. Watch it from the Actions tab. On success:

- `bettermemory==<X.Y.Z>` is on PyPI.
- A GitHub release with auto-generated notes appears on the Releases page, with the sdist and wheel attached as assets.

## Dry run via TestPyPI

Before a real release (especially the first one, or any release with packaging changes), push to TestPyPI first via the manual trigger:

1. Bump the version in `pyproject.toml` to a pre-release candidate like `<X.Y.Z>rc1` or `<X.Y.Z>.dev1`. TestPyPI rejects re-uploading the same version, and `pip install` ignores pre-releases by default. So a pre-release candidate is the right shape: the candidate stays out of the way of regular installs while letting you bump `rc2` or `rc3` until you are happy.
2. Commit (do not tag).
3. Actions tab, then Release, then "Run workflow", and target `testpypi`.
4. Verify the rendered project page on <https://test.pypi.org/project/bettermemory/>.
5. `pip install -i https://test.pypi.org/simple/ bettermemory==<X.Y.Z>rcN` into a scratch venv and smoke-test.

Once you are satisfied, bump pyproject.toml back to the real version (without the `rcN` or `devN` suffix), commit, tag, and push.

## Version-tag mismatch protection

The build job verifies that the pyproject.toml version matches the tag (`v<X.Y.Z>` to `<X.Y.Z>`). Tagging `v1.2.3` while pyproject still says `1.2.2` aborts the workflow before any artifact is uploaded. Fix the version, force-push the tag (`git tag -af v<X.Y.Z>`), and re-push.

## Release-window CHANGELOG coverage

`test_newest_tag_window_commits_are_represented` in `tests/test_changelog.py` guards the omission class where a commit ships inside a release tag with no trace in that release's notes — the way `096218e` shipped inside `v3.24.0` and had to be repaired by erratum. For the newest `v<X.Y.Z>` tag it walks every non-merge commit in the window from the previous tag and requires each one to be represented in that release's `## <X.Y.Z> - <date>` entry. Commits whose conventional-commit type is trivial (docs, style, test, chore, ci, build, bench, release), or whose every scope is test/docs/ci/bench tooling, are exempt. Every other commit has to clear one of three tiers, checked in this order:

1. **Its short SHA appears in the entry** — the deterministic escape hatch for when the entry deliberately paraphrases, or groups several commits under one bullet (the shape the erratum bullets already use).
2. **A two-word phrase from its subject is reused in the entry.** This is the primary lexical path: notes that genuinely document a commit almost always echo one of its phrases, and requiring a *pair* is what keeps the tier honest.
3. **Near-total single-word overlap** — at least four distinctive subject words, and no more than one of them missing. A deliberately weak fallback for a subject the entry rewrote wholesale. It takes a pair or a near-complete sentence precisely because single generic words (`search`, `verdict`, `memory`) recur in every entry this project has ever written, so one of them matching is not evidence of anything.

Writing the entry in prose that reuses the commits' own phrasing is what clears tier 2 for most of a window. Reach for a SHA citation in the two cases prose cannot cover: a bullet that intentionally covers several commits at once, and a subject that cannot produce a matchable pair at all. The second is not rare — the judge drops stopwords and any token under three characters *before* forming pairs, so a short or glue-heavy subject may have no two adjacent distinctive words left to echo. When that happens the entry is not deficient; cite the short SHA. Since the SHA has to exist first, that means a follow-up `docs(changelog):` commit, which is how `3ea1ffe` and `9b68e74` were written.

The check needs the tag to exist: during step 5 it evaluates the previous, already-frozen window, and on CI's shallow checkouts — which lack the tag pair and the window's history — it skips. The moment it has teeth is between `git tag` and `git push origin v<X.Y.Z>`, which is exactly where step 6 re-runs it.

## Yanking a bad release

Trusted publishing does not change the yank flow. From PyPI's project page, go to "Manage", then "Releases", then "Options" on the bad version, then "Yank". Yanked releases are still installable when pinned exactly but stop being chosen by `pip install bettermemory` resolvers, which is the right behavior for "this version was published by accident."

## Listing in the MCP registry

`.github/workflows/publish-mcp.yml` submits `server.json` to the MCP registry.
The registry refuses a listing whose version is not yet on PyPI, so the trigger
has to *follow* the PyPI publish rather than race it off the `v*` tag push the
upstream guide suggests. Three triggers are declared, and on the automated path
the one that actually fires is `workflow_run` against the **Release** workflow:
it observes that run finishing, and on a tag push that run cannot conclude
`success` unless its `publish-pypi` job did — `publish-pypi` is not skipped on
a `push` event, and `github-release` further declares `needs: publish-pypi`.
The job is gated on a succeeded run whose `head_branch` starts with `v`, so a
failed release and the TestPyPI dispatch list nothing, and it checks out that
tag rather than the default branch — under `workflow_run` the default checkout
is wherever `main` points *now*, which would publish a `server.json` describing
some version other than the one that just went to PyPI.

`release: published` is the second trigger and is kept deliberately, but it
covers only a release cut by hand or with a PAT. A release created by
`release.yml`'s `github-release` job raises no run-starting event: GitHub
suppresses events raised by a job authenticating with the default
`GITHUB_TOKEN` — a recursion guard — and that job uses exactly that token. The
workflow shipped in 3.35.0 with `release: published` as its only automatic
trigger and consequently never fired once; the 3.34.0 listing that existed had
come from the `workflow_dispatch` backfill below, so a succeeding fallback hid
a primary path that could not run. If you are reasoning about why a release did
or did not get listed, the Release run is the thing to look at, not the
release event.

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
currently says.

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
3. Re-run the publish without re-tagging. The build artifact is still good. From the GitHub Actions tab, go to the "Release" workflow, then "Run workflow", set `target=pypi`, and run. This dispatches the workflow on the same `main` HEAD; the build job re-runs (cheap) and the publish step exchanges the new OIDC token, which now has a matching trusted-publisher record. The `github-release` job is skipped on dispatch (it only fires on tag push), so you will need to create the GitHub release by hand from the tag if you want one. Or push a fresh patch tag (`v<X.Y.Z+1>`) once you are happy the trust setup works.

### `version mismatch` build-job failure

The build job verifies that the version in `pyproject.toml` matches the tag (`v1.2.3` to `1.2.3`). If they disagree, the run aborts before any artifact ships.

Fix: bump `pyproject.toml` to match the tag and force-update the tag to the new commit:

```sh
git tag -f v<X.Y.Z>
git push --force origin v<X.Y.Z>
```

The forced re-push fires the workflow again.
