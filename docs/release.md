# Release process

Releases are cut by pushing an annotated `v<X.Y.Z>` tag — the
`Release` workflow (`.github/workflows/release.yml`) handles the rest:
build, full gating suite, PyPI publish, GitHub release. No tokens; no
manual `twine upload` step.

## One-time setup: PyPI trusted publishing

Trusted publishing replaces long-lived API tokens with short-lived
OIDC tokens minted per workflow run. The configuration lives on the
PyPI side, not in this repo's secrets — there is nothing to copy
into GitHub.

Set up two trusted publishers, one per target:

### Production PyPI

1. Sign in at <https://pypi.org/manage/account/publishing/>.
2. Pick **"Add a new pending publisher"** if `bettermemory` does not
   yet exist on PyPI; pick **"Add a new publisher"** under the project
   if it does.
3. Fill in:
   - **PyPI Project Name**: `bettermemory`
   - **Owner**: `0Mattias`
   - **Repository name**: `bettermemory`
   - **Workflow name**: `release.yml`
   - **Environment name**: `pypi`

### TestPyPI

1. Sign in at <https://test.pypi.org/manage/account/publishing/>.
2. Same fields, except the **Environment name** is `testpypi`.

The `pypi` and `testpypi` environment names match the `environment:`
blocks in `release.yml` — keep them in sync if you rename either.

## Cutting a release

Working tree clean, on `main`, all CI green:

```sh
# 1. Bump the version in pyproject.toml. Edit by hand — do not invoke
#    `uv version` or similar; that loses the trailing newline and
#    surfaces noise in the diff.
$EDITOR pyproject.toml

# 2. Move the relevant entries from the "Unreleased" section of
#    CHANGELOG.md into a new `## <X.Y.Z> — <date>` heading.
$EDITOR CHANGELOG.md

# 3. Commit, tag, push.
git commit -am "release: <X.Y.Z>"
git tag -a v<X.Y.Z> -m "v<X.Y.Z>"
git push origin main
git push origin v<X.Y.Z>
```

The tag push triggers the `Release` workflow. Watch it from the
Actions tab. On success:

- `bettermemory==<X.Y.Z>` is on PyPI
- a GitHub release with auto-generated notes appears on the Releases
  page, with the sdist and wheel attached as assets

## Dry run via TestPyPI

Before a real release — especially the first one, or any release
with packaging changes — push to TestPyPI first via the manual
trigger:

1. Bump the version in `pyproject.toml` to a pre-release candidate
   like `<X.Y.Z>rc1` or `<X.Y.Z>.dev1` (TestPyPI rejects re-uploading
   the same version, and `pip install` ignores pre-releases by default
   — so a pre-release candidate is the right shape: the candidate
   stays out of the way of regular installs while letting you bump
   `rc2` / `rc3` until you're happy).
2. Commit (do not tag).
3. Actions tab → Release → "Run workflow" → target `testpypi`.
4. Verify the rendered project page on
   <https://test.pypi.org/project/bettermemory/>.
5. `pip install -i https://test.pypi.org/simple/ bettermemory==<X.Y.Z>rcN`
   into a scratch venv and smoke-test.

Once you're satisfied, bump pyproject.toml back to the real version
(without the `rcN`/`devN` suffix), commit, tag, push.

## Version-tag mismatch protection

The build job verifies that the pyproject.toml version matches the
tag (`v<X.Y.Z>` → `<X.Y.Z>`). Tagging `v1.2.3` while pyproject still
says `1.2.2` aborts the workflow before any artifact is uploaded —
fix the version, force-push the tag (`git tag -af v<X.Y.Z>`), and
re-push.

## Yanking a bad release

Trusted publishing does not change the yank flow. From PyPI's project
page → "Manage" → "Releases" → "Options" on the bad version → "Yank".
Yanked releases are still installable when pinned exactly but stop
being chosen by `pip install bettermemory` resolvers, which is the
right behavior for "this version was published by accident."
