"""Tests for the git-based sync wrapper (T4.1 of the v1.6 plan).

Tests use a local bare repository as the "remote" so push and pull
exercise real git transport without needing network access. Each
test sets up its own pair of stores via tmp_path so the suite stays
hermetic.
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
from pathlib import Path

import pytest

from bettermemory import sync
from bettermemory.doctor import DOCTOR_PROBE_FILENAME
from bettermemory.events import EVENT_LOG_FILENAME
from bettermemory.index import INDEX_FILENAME
from bettermemory.ingest import INGEST_WATERMARK_FILENAME
from bettermemory.proposals import PROPOSALS_FILENAME
from bettermemory.semantic import (
    EMBEDDING_FILENAME_PREFIX,
    EMBEDDING_FILENAME_SUFFIX,
)
from bettermemory.store import Store


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not on PATH — sync tests need git installed",
)


def _git(cwd: Path, *args: str) -> str:
    """Helper for ad-hoc git in tests. Raises with stderr on failure."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"`git {' '.join(args)}` failed: {result.stderr.strip()}")
    return result.stdout


@pytest.fixture
def memory_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "memories"
    d.mkdir()
    # Redirect git's global config to a per-test tmp file. Without
    # this, the `git config --global user.{email,name}` calls below
    # silently overwrite the developer's ~/.gitconfig on every local
    # run. `GIT_CONFIG_GLOBAL` is git's documented sandbox mechanism
    # (since git 2.32) and is inherited by subprocesses through the
    # process env, so the redirect covers every git invocation any
    # test makes for the duration of the test.
    global_config = tmp_path / "test.gitconfig"
    global_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    # Identity so commits work in CI.
    _git(d.parent, "config", "--global", "user.email", "test@example.com")
    _git(d.parent, "config", "--global", "user.name", "Test")
    return d


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    """A local bare repo to act as the sync target. Same disk, no
    network. Initialise with `main` as the default branch so it
    matches `sync.init`'s default."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", str(bare)],
        check=True,
        capture_output=True,
    )
    return bare


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_repo_and_gitignore(memory_dir: Path) -> None:
    """First-time init: creates a git repo on the default branch and
    writes the canonical .gitignore. Idempotent on repeat — the
    test below covers that case."""
    result = sync.init(memory_dir)
    assert (memory_dir / ".git").is_dir()
    assert (memory_dir / ".gitignore").exists()
    ignore = (memory_dir / ".gitignore").read_text()
    assert ".index.sqlite" in ignore
    assert ".events.jsonl" in ignore
    assert result["already_repo"] is False


def test_init_is_idempotent(memory_dir: Path) -> None:
    """Running init twice should not fail or duplicate state. The
    second run reports already_repo=True; the .gitignore is
    refreshed in-place rather than re-appended."""
    sync.init(memory_dir)
    result = sync.init(memory_dir)
    assert result["already_repo"] is True
    # .gitignore should not be duplicated.
    ignore = (memory_dir / ".gitignore").read_text()
    assert ignore.count(".index.sqlite\n") == 1


# Store-root sidecars that are DELIBERATELY tracked by the sync repo. Empty
# today: everything the runtime writes beside the memories is host-local or
# regenerable. An entry here is a conscious decision that a sidecar's contents
# are portable across hosts AND worth versioning — not a place to silence the
# guard below.
_INTENTIONALLY_SYNCED_SIDECARS: frozenset[str] = frozenset()


def _sidecar_filename_constants() -> dict[str, str]:
    """Every `*_FILENAME` constant in the package whose value is a dotfile.

    Discovered by walking the package rather than hand-listed, because the
    whole point of the guard is to catch a sidecar whose author never thought
    about `sync`.

    `walk_packages`, not `iter_modules`: the latter does not descend into
    subpackages, so it sees `handlers` and `cli` as opaque names and only ever
    reads their `__init__` re-exports. A sidecar constant defined in, say,
    `handlers/write.py` was therefore invisible and the guard passed while the
    file leaked — the guard advertising complete coverage it did not have.
    `handlers/` is an actively developed 26-module package and the
    auto-consolidate clock is precedent for exactly the kind of store-root
    state file a handler could own.

    Keyed by QUALIFIED name (`module.ATTR`), not by the bare attribute: two
    modules may legitimately pick the same constant name for different files,
    and a dict keyed on the attribute alone would silently drop one of them.
    If the survivor happened to be gitignored, the other would leak past the
    guard — the precise failure this guard exists to make impossible.

    A module that cannot be imported is skipped rather than failing the guard;
    `onerror` swallows package-level import errors the same way. Today every
    module imports cleanly (optional deps are all lazily imported inside
    functions), so the skip is defensive, not load-bearing.
    """
    import importlib
    import pkgutil

    import bettermemory

    found: dict[str, str] = {}
    walk = pkgutil.walk_packages(
        bettermemory.__path__, prefix="bettermemory.", onerror=lambda _name: None
    )
    for mod_info in walk:
        try:
            mod = importlib.import_module(mod_info.name)
        except ImportError:
            continue
        for attr in dir(mod):
            if not attr.endswith("_FILENAME"):
                continue
            value = getattr(mod, attr)
            if isinstance(value, str) and value.startswith("."):
                found[f"{mod_info.name}.{attr}"] = value
    return found


def test_every_store_root_sidecar_is_gitignored() -> None:
    """STRUCTURAL GUARD. `sync push` runs `git add -A`, so any file the
    runtime writes into the store root that is absent from `_GITIGNORE_LINES`
    is staged, committed, and pushed to every clone — permanently, in
    plaintext. This has now happened three times, each time silently: the
    write-reflex proposal queue (raw user text that never passed the
    credential gate), orphaned atomic-write `*.tmp` sidecars (which carry a
    full memory body), and the ingest watermark (absolute host paths).

    Every leak shared a shape: the sidecar's `*_FILENAME` constant lives in
    its owning module, `sync.py` must list it by hand, and forgetting to
    breaks NO test. In a file-disjoint parallel audit the sidecar and the
    denylist even land in different agents' scopes, so nobody sees the seam.

    So the guard discovers the constants instead of trusting a hand-list: any
    `*_FILENAME` whose value is a dotfile must be matched by some pattern in
    `_GITIGNORE_LINES` (literal or glob). A new sidecar therefore fails HERE,
    at the moment it is introduced, rather than on someone's remote.

    `test_sidecar_discovery_descends_into_subpackages` guards the discovery
    itself — without it this guard is only as good as its walk, and its first
    version silently skipped every module under `handlers/` and `cli/`."""
    patterns = [
        line for line in sync._GITIGNORE_LINES if not line.lstrip().startswith("#")
    ]
    sidecars = _sidecar_filename_constants()
    # Sanity: discovery works at all. If this trips, the walk broke, not sync.
    assert any(name.endswith(".EVENT_LOG_FILENAME") for name in sidecars), (
        f"sidecar discovery found nothing recognisable: {sorted(sidecars)}"
    )

    unignored = {
        qualname: value
        for qualname, value in sidecars.items()
        if value not in _INTENTIONALLY_SYNCED_SIDECARS
        and not any(fnmatch.fnmatch(value, pat) for pat in patterns)
    }
    assert not unignored, (
        "store-root sidecar(s) missing from sync._GITIGNORE_LINES — "
        f"`sync push` would commit and push them to every clone: {unignored}. "
        "Add each constant to _GITIGNORE_LINES (import it from its owning "
        "module), or, if the file is genuinely portable and worth versioning, "
        "add its value to _INTENTIONALLY_SYNCED_SIDECARS with a rationale."
    )


def test_sidecar_discovery_descends_into_subpackages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard above is only as strong as this walk. Its first version used
    `pkgutil.iter_modules`, which does not recurse, so every module under
    `handlers/` and `cli/` was invisible: a sidecar declared there leaked while
    the guard reported success. Plant one in a real subpackage module and
    require the discovery to see it.

    This is the mutation guard for the discovery itself — revert
    `_sidecar_filename_constants` to `iter_modules` and this test fails while
    the guard above still passes, which is exactly the false confidence that
    made the hole survive review."""
    from bettermemory.handlers import write as write_handler

    monkeypatch.setattr(
        write_handler,
        "PLANTED_SIDECAR_FILENAME",
        ".planted-sidecar.json",
        raising=False,
    )
    found = _sidecar_filename_constants()
    assert (
        found.get("bettermemory.handlers.write.PLANTED_SIDECAR_FILENAME")
        == ".planted-sidecar.json"
    ), (
        "sidecar discovery did not descend into bettermemory.handlers — a "
        "store-root sidecar declared in a subpackage would leak past the guard. "
        f"discovered: {sorted(found)}"
    )


def test_sidecar_discovery_does_not_collide_on_shared_constant_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two modules may pick the same constant name for different files. Keyed
    on the bare attribute, one silently overwrote the other — and if the
    survivor happened to be gitignored, the loser leaked past the guard while
    it reported success. Discovery keys by qualified name, so both survive and
    both get checked."""
    from bettermemory import store as store_mod
    from bettermemory.handlers import write as write_handler

    monkeypatch.setattr(store_mod, "DUP_FILENAME", ".alpha-sidecar.json", raising=False)
    monkeypatch.setattr(
        write_handler, "DUP_FILENAME", ".beta-sidecar.json", raising=False
    )
    values = set(_sidecar_filename_constants().values())
    assert {".alpha-sidecar.json", ".beta-sidecar.json"} <= values, (
        "same-named sidecar constants in two modules collided; one was dropped "
        f"from discovery and would leak past the guard. discovered: {sorted(values)}"
    )


def test_gitignore_lines_are_positive_and_slash_free() -> None:
    """STRUCTURAL SHAPE GUARD. Doctor's `_check_sync_tracked_ignored` and
    `_scan_parent_index_for_sidecars` translate `_GITIGNORE_LINES` into flat
    `fnmatch` patterns (each non-comment line fnmatched against every tracked
    path and its basename). That translation is only equivalent to git's
    .gitignore semantics for POSITIVE, SLASH-FREE lines — and doctor's
    comments cite THIS test as what keeps the list that shape:

    - A `!` negation line un-ignores a file for git (it is DELIBERATELY
      tracked), but doctor's fnmatch translation has no negation semantics:
      the kept file still matches some positive glob, so doctor FAILs and
      its fix_hint walks the user through `git rm --cached`, a history
      rewrite, and secret rotation — a DESTRUCTIVE false positive against a
      file the gitignore intentionally keeps.
    - A `/`-anchored (or any slash-containing) line matches path segments
      relative to the .gitignore for git, but fnmatch sees one flat string:
      store-relative tracked paths never start with `/`, and the basename
      fallback never contains `/`, so the line silently matches nothing — a
      false NEGATIVE in the exact leak guard meant to catch tracked secrets.

    `sync.init()` writes any future line verbatim into every store
    .gitignore, so the constraint must hold at the list itself, not at the
    call sites. Mutation-sound: append `"!keep.tmp"` or `"store/x"` to
    `sync._GITIGNORE_LINES` and this test fails naming the offending line."""
    # Same comment/blank filter doctor applies when building its patterns.
    for line in sync._GITIGNORE_LINES:
        if not line or line.lstrip().startswith("#"):
            continue
        assert not line.startswith("!"), (
            f"sync._GITIGNORE_LINES contains a `!` negation line: {line!r}. "
            "doctor's fnmatch translation (_check_sync_tracked_ignored / "
            "_scan_parent_index_for_sidecars) drops negation semantics, so "
            "the deliberately-kept file would still match a positive glob "
            "and doctor would FAIL with a destructive fix_hint (`git rm "
            "--cached` + history rewrite + secret rotation) for a file the "
            "gitignore keeps on purpose. Restructure the patterns so no "
            "negation is needed."
        )
        assert "/" not in line, (
            f"sync._GITIGNORE_LINES contains a slash-anchored line: {line!r}. "
            "doctor's fnmatch translation (_check_sync_tracked_ignored / "
            "_scan_parent_index_for_sidecars) matches each line as one flat "
            "pattern against store-relative paths and basenames, so a "
            "`/`-containing line silently matches nothing — a false negative "
            "in the tracked-sidecar leak guard. Use a slash-free filename or "
            "glob instead."
        )


def test_init_uses_atomic_write_for_gitignore(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The .gitignore writer must route through `atomic_write_bytes` so
    a power loss / process kill mid-write can't leave a truncated
    gitignore. A truncated gitignore lets the next `sync push` commit
    event logs / lockfiles / index sqlite to the remote — exactly the
    privacy regression the canonical gitignore is there to prevent.
    Pre-3.1.0 this was a plain `gitignore.write_text(...)`."""
    from bettermemory import _fsutil

    calls: list[tuple[Path, bytes]] = []
    real = _fsutil.atomic_write_bytes

    def spy(path: Path, data: bytes, *, mode: int | None = None) -> None:
        calls.append((path, data))
        real(path, data, mode=mode)

    # The `sync.py` module imports `atomic_write_bytes` by name (from
    # `._fsutil import atomic_write_bytes`), so the spy must replace
    # the name in `sync`'s module namespace — patching `_fsutil` itself
    # wouldn't shadow the local binding.
    monkeypatch.setattr(sync, "atomic_write_bytes", spy, raising=False)
    result = sync.init(memory_dir)
    assert result["already_repo"] is False
    assert len(calls) == 1, (
        f"expected exactly one atomic_write_bytes call for the "
        f".gitignore; got {len(calls)}. A regression to "
        f"`gitignore.write_text(...)` would surface as zero calls."
    )
    path, data = calls[0]
    assert path == memory_dir / ".gitignore"
    assert b".index.sqlite" in data
    assert b".events.jsonl" in data


def test_init_sets_remote(memory_dir: Path, bare_remote: Path) -> None:
    """When `--remote` is passed, init adds (or updates) the origin
    remote. The actual URL ends up in `git remote get-url origin`."""
    sync.init(memory_dir, remote=str(bare_remote))
    url = _git(memory_dir, "remote", "get-url", "origin").strip()
    assert url == str(bare_remote)


def test_init_updates_remote_on_second_run(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """A second init with a different --remote replaces the URL
    rather than failing. Users iterating on remote setup shouldn't
    have to remember whether they've initialised yet."""
    other = tmp_path / "other.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", str(other)],
        check=True,
        capture_output=True,
    )
    sync.init(memory_dir, remote=str(other))
    sync.init(memory_dir, remote=str(bare_remote))
    url = _git(memory_dir, "remote", "get-url", "origin").strip()
    assert url == str(bare_remote)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_on_non_repo_returns_is_repo_false(tmp_path: Path) -> None:
    """A plain directory should report is_repo=False rather than
    raise. Lets the CLI render a helpful "run init first" message."""
    plain = tmp_path / "plain"
    plain.mkdir()
    st = sync.status(plain)
    assert st.is_repo is False
    assert st.branch is None
    assert st.remote_url is None


def test_status_on_repo_reports_branch_and_changes(memory_dir: Path) -> None:
    """After init + a write, status should show the current branch
    and a non-empty modified/untracked list."""
    sync.init(memory_dir)
    store = Store(memory_dir)
    store.write(content="test memory", scopes=["tools"])
    st = sync.status(memory_dir)
    assert st.is_repo is True
    assert st.branch == "main"
    assert st.has_changes is True


def test_status_porcelain_parses_modified_path_cleanly(
    memory_dir: Path,
) -> None:
    """Porcelain output is `XY␣path`. For modified-not-staged files
    the X char is a space (` M filename`); a partition on the first
    space would drop the status char into the path and store the
    modified file as `"M filename"`. Lock in that the path is parsed
    cleanly so the CLI shows the actual filename in `sync status`."""
    sync.init(memory_dir)
    store = Store(memory_dir)
    memory = store.write(content="original body", scopes=["tools"])

    # First commit so the file is tracked.
    _git(memory_dir, "add", "-A")
    _git(memory_dir, "commit", "-m", "seed")

    # Modify in place — the resulting status code is " M" (space, M).
    path = store._find_path_for_id(memory.id)
    assert path is not None
    path.write_text(path.read_text() + "\nappended line\n", encoding="utf-8")

    st = sync.status(memory_dir)
    assert st.is_repo is True
    assert len(st.modified) == 1
    parsed = st.modified[0]
    # The path must NOT carry a leading "M " from the status code.
    assert not parsed.startswith("M ")
    assert parsed.endswith(".md")


def test_status_redacts_credentialed_remote_url(memory_dir: Path) -> None:
    """A remote URL with embedded credentials must not surface in
    SyncStatus.remote_url. The credential lives in git config (where
    it belongs); CLI output / `--json` payloads should not echo it."""
    secret_url = "https://alice:ghp_topsecret@github.com/example/repo.git"
    sync.init(memory_dir, remote=secret_url)
    st = sync.status(memory_dir)
    assert st.remote_url is not None
    assert "ghp_topsecret" not in st.remote_url
    assert "alice" not in st.remote_url
    assert "github.com/example/repo.git" in st.remote_url


def test_redact_url_helper_handles_common_shapes() -> None:
    """Direct unit coverage on _redact_url for the cases the SyncStatus
    test doesn't exercise: ssh URLs, anonymous HTTPS, malformed input."""
    from bettermemory.sync import _redact_url

    # SSH URL — `git@host:path` has no scheme; the `git@` is a username
    # not a token, leave it alone.
    assert _redact_url("git@github.com:foo/bar.git") == "git@github.com:foo/bar.git"
    # Anonymous HTTPS — nothing to redact.
    assert (
        _redact_url("https://github.com/foo/bar.git")
        == "https://github.com/foo/bar.git"
    )
    # Token-bearing HTTPS — strip the userinfo.
    assert (
        _redact_url("https://x-access-token:abc123@github.com/foo/bar.git")
        == "https://github.com/foo/bar.git"
    )
    # None passthrough.
    assert _redact_url(None) is None


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def test_push_commits_and_pushes(memory_dir: Path, bare_remote: Path) -> None:
    """End-to-end push: init with remote, write a memory, push.
    The committed=True + pushed=True flags must both fire on the
    first push of new content."""
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    store.write(content="durable fact", scopes=["tools"])

    result = sync.push(memory_dir)
    assert result["committed"] is True
    assert result["pushed"] is True

    # Confirm the commit landed on the bare remote.
    log = _git(bare_remote, "log", "--oneline")
    assert "bettermemory: sync" in log


def test_push_no_op_when_nothing_changed(memory_dir: Path, bare_remote: Path) -> None:
    """A push when there are no local changes should NOT create an
    empty commit. The committed=False signal is how the CLI tells
    the user "nothing to do" without an error."""
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    store.write(content="initial", scopes=["tools"])
    sync.push(memory_dir)

    # Second push with no changes.
    result = sync.push(memory_dir)
    assert result["committed"] is False
    assert result["pushed"] is True


def test_push_does_not_stage_proposals_queue(
    memory_dir: Path, bare_remote: Path
) -> None:
    """The write-reflex proposal queue (`.write_proposals.jsonl`) holds RAW
    captured user text that never passed the write-path credential gate — a
    secret-shaped capture ("my staging DB password is …") sits there verbatim
    until the model reviews it. It MUST be gitignored so `sync push`'s
    `git add -A` never stages, commits, or pushes it. Pre-fix (3.18.1) the
    queue was absent from `_GITIGNORE_LINES`, so the plaintext capture landed
    in the committed tree AND on the remote, where git history makes it
    permanent — the 🔴 leak this test guards.

    Mutation-sound: drop `PROPOSALS_FILENAME` from `sync._GITIGNORE_LINES`
    and both the committed-tree and check-ignore assertions fail (the queue
    file is staged, committed, and pushed with the secret intact)."""
    import json

    sync.init(memory_dir, remote=str(bare_remote))
    # A real memory gives the push canonical (non-secret) content to commit.
    Store(memory_dir).write(content="durable fact", scopes=["tools"])
    # Simulate the Stop hook having queued a secret-bearing proposal.
    secret = "Xk92mQz7Lp4R9t"  # synthetic test fixture, not a live secret
    queue_file = memory_dir / PROPOSALS_FILENAME
    queue_file.write_text(
        json.dumps(
            {
                "id": "c1",
                "body": f"my staging DB password is {secret}",
                "source_excerpt": "",
                "suggested_category": "fact",
                "created": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = sync.push(memory_dir)
    assert result["pushed"] is True

    # The committed tree (local HEAD) must not contain the queue file.
    committed = _git(memory_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert PROPOSALS_FILENAME not in committed, (
        f"proposals queue leaked into the committed tree: {committed}"
    )
    # git itself agrees the path is ignored (rc==0 means "is ignored").
    check = subprocess.run(
        ["git", "check-ignore", PROPOSALS_FILENAME],
        cwd=memory_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, (
        f"{PROPOSALS_FILENAME} is not gitignored (git check-ignore rc="
        f"{check.returncode}); `sync push` would stage it"
    )
    # And the secret never reached the remote's history.
    remote_history = _git(bare_remote, "log", "-p", "--all")
    assert secret not in remote_history, (
        "secret from the proposals queue reached the remote git history"
    )


def test_push_does_not_stage_ingest_watermark(
    memory_dir: Path, bare_remote: Path
) -> None:
    """The ingest watermark (`.ingest-watermark.json`) is host-local state and
    must never sync.

    It maps ABSOLUTE source-file paths on the capturing host (under
    `~/.claude/projects/<sanitized-cwd>/memory/`) to the content hashes already
    imported, so `doctor`'s stranded-auto-memory check can distinguish "never
    ingested" from "ingested, then curated". Both halves are host-local: the
    paths do not exist on another machine, and a clone that inherited them
    would believe it had already imported sources it has never seen —
    suppressing the very check the watermark exists to feed. Pushing it also
    leaks the local filesystem layout to every clone.

    This is a cross-item seam: the watermark and `_GITIGNORE_LINES` were
    introduced/owned by different fixes in the same parallel drain round, so
    neither could register the other.

    Mutation-sound: drop `INGEST_WATERMARK_FILENAME` from
    `sync._GITIGNORE_LINES` and both the committed-tree and check-ignore
    assertions fail (the watermark is staged, committed, and pushed)."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="durable fact", scopes=["tools"])
    # A watermark as apply_ingest_plan would leave it: absolute host paths.
    watermark = memory_dir / INGEST_WATERMARK_FILENAME
    host_path = "/Users/someone/.claude/projects/-Users-someone-repo/memory/a.md"
    watermark.write_text(
        f'{{"version": 1, "sources": {{"{host_path}": "deadbeef"}}}}\n',
        encoding="utf-8",
    )

    result = sync.push(memory_dir)
    assert result["pushed"] is True

    committed = _git(memory_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert INGEST_WATERMARK_FILENAME not in committed, (
        f"ingest watermark leaked into the committed tree: {committed}"
    )
    check = subprocess.run(
        ["git", "check-ignore", INGEST_WATERMARK_FILENAME],
        cwd=memory_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, (
        f"{INGEST_WATERMARK_FILENAME} is not gitignored (git check-ignore rc="
        f"{check.returncode}); `sync push` would stage it"
    )
    # And the host-local path never reached the remote's history.
    remote_history = _git(bare_remote, "log", "-p", "--all")
    assert host_path not in remote_history, (
        "host-local ingest path from the watermark reached the remote history"
    )


def test_push_does_not_stage_atomic_write_tmp_orphans(
    memory_dir: Path, bare_remote: Path
) -> None:
    """Orphaned atomic-write `*.tmp` sidecars must never sync.

    `_fsutil.atomic_write_bytes` writes `<target>.<random>.tmp` next to its
    target and only unlinks it inside a caught-exception `finally` — a hard
    crash / SIGKILL / power loss between tmp creation and `os.replace` leaves
    the orphan behind. That orphan carries the SAME plaintext payload as the
    file it was about to become: a full memory body (`<mem>.md.<rand>.tmp`) or
    the raw-capture proposals queue (`.write_proposals.jsonl.<rand>.tmp`, which
    is host-local by design and never meant to leave the capturing host).
    Without a `*.tmp` glob in `_GITIGNORE_LINES` the next `sync push`'s
    `git add -A` stages, commits, and pushes that orphan to every clone, where
    git history makes it permanent — the same leak class the PROPOSALS_FILENAME
    line closes for the committed queue, reopened through the tmp sidecar.

    Mutation-sound: drop `"*.tmp"` from `sync._GITIGNORE_LINES` and the
    committed-tree, check-ignore, and remote-history assertions all fail — both
    orphans get staged, committed, and pushed with their secrets intact."""
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    # A real memory gives the push canonical content to commit, and its
    # on-disk path is the faithful base for the tmp-orphan name.
    memory = store.write(content="durable fact", scopes=["tools"])
    mem_path = store._find_path_for_id(memory.id)
    assert mem_path is not None

    # Orphan #1: a crash mid-write of a memory body strands
    # `<mem>.md.<rand>.tmp` next to the real memory file.
    mem_secret = "Qw83Zx01Vb52Nm"  # synthetic test fixture, not a live secret
    mem_tmp = mem_path.with_name(mem_path.name + ".a1b2c3.tmp")
    mem_tmp.write_text(
        f"---\nid: {memory.id}\n---\nprivate memory body {mem_secret}\n",
        encoding="utf-8",
    )
    # Orphan #2: a crash mid-write of the proposals queue strands
    # `.write_proposals.jsonl.<rand>.tmp` holding a raw (never-gated) capture.
    queue_secret = "Xk92mQz7Lp4R9t"  # synthetic test fixture, not a live secret
    queue_tmp = memory_dir / f"{PROPOSALS_FILENAME}.d4e5f6.tmp"
    queue_tmp.write_text(
        f"my staging DB password is {queue_secret}\n", encoding="utf-8"
    )

    result = sync.push(memory_dir)
    assert result["pushed"] is True

    # Neither orphan may appear in the committed tree (local HEAD).
    committed = _git(memory_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert mem_tmp.name not in committed, (
        f"memory-body tmp orphan leaked into the committed tree: {committed}"
    )
    assert queue_tmp.name not in committed, (
        f"proposals-queue tmp orphan leaked into the committed tree: {committed}"
    )

    # git itself agrees both orphans are ignored (rc==0 means "is ignored").
    for orphan in (mem_tmp.name, queue_tmp.name):
        check = subprocess.run(
            ["git", "check-ignore", orphan],
            cwd=memory_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        assert check.returncode == 0, (
            f"{orphan} is not gitignored (git check-ignore rc="
            f"{check.returncode}); `sync push` would stage it"
        )

    # And neither secret reached the remote's permanent history.
    remote_history = _git(bare_remote, "log", "-p", "--all")
    assert mem_secret not in remote_history, (
        "memory body from an atomic-write tmp orphan reached the remote history"
    )
    assert queue_secret not in remote_history, (
        "raw capture from a proposals-queue tmp orphan reached the remote history"
    )


def test_push_errors_without_remote(memory_dir: Path) -> None:
    """A push against a repo with no `origin` should raise SyncError
    with an actionable message. The CLI catches this and renders a
    clean error."""
    sync.init(memory_dir)  # no --remote passed
    Store(memory_dir).write(content="x", scopes=["tools"])
    with pytest.raises(sync.SyncError, match="no remote"):
        sync.push(memory_dir)


def test_push_errors_on_non_repo(tmp_path: Path) -> None:
    """A push against a non-repo directory should raise SyncError
    pointing to `sync init`."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(sync.SyncError, match="not a git repo"):
        sync.push(plain)


def test_push_redacts_credentialed_url_in_error(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a failed `git push` echoes the full remote URL into
    stderr, including the userinfo segment for HTTPS-token auth. The
    SyncError must redact that before raising — otherwise the
    credential lands in CLI output and any log capture downstream.

    The original `_run_git` already redacts via the default check=True
    path; sync.push uses check=False (it wants to attach the
    "rebase --continue" hint), so the redact wrapper has to be applied
    in its own SyncError construction — which the fix added."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="x", scopes=["tools"])

    secret_url = "https://alice:ghp_topsecret@github.com/example/repo.git"
    fake_stderr = f"fatal: unable to access '{secret_url}': connection refused"
    original_run_git = sync._run_git

    def fake_run_git(
        root: Path, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "push":
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=1,
                stdout="",
                stderr=fake_stderr,
            )
        return original_run_git(root, args, check=check)

    monkeypatch.setattr(sync, "_run_git", fake_run_git)

    with pytest.raises(sync.SyncError) as excinfo:
        sync.push(memory_dir)
    error_text = str(excinfo.value)
    assert "ghp_topsecret" not in error_text, (
        f"token leaked into SyncError: {error_text}"
    )
    assert "alice" not in error_text, f"username leaked into SyncError: {error_text}"
    # And confirm the redaction marker shows up — useful to debug the
    # error without exposing what was hidden.
    assert "<redacted>" in error_text or "redacted" in error_text


def test_pull_redacts_credentialed_url_in_error(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric to the push redaction test: a failed `git pull --rebase`
    echoes the credentialed URL into stderr, and the SyncError must
    redact it. The pull path attaches a conflict-resolution hint, so
    like push it builds its own SyncError with the raw text — the
    redact wrapper had to be applied there too."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="x", scopes=["tools"])
    sync.push(memory_dir)

    secret_url = "https://alice:ghp_topsecret@github.com/example/repo.git"
    fake_stderr = f"fatal: unable to access '{secret_url}': connection refused"
    original_run_git = sync._run_git

    def fake_run_git(
        root: Path, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "pull":
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=1,
                stdout="",
                stderr=fake_stderr,
            )
        return original_run_git(root, args, check=check)

    monkeypatch.setattr(sync, "_run_git", fake_run_git)

    with pytest.raises(sync.SyncError) as excinfo:
        sync.pull(memory_dir)
    error_text = str(excinfo.value)
    assert "ghp_topsecret" not in error_text, (
        f"token leaked into SyncError: {error_text}"
    )
    assert "alice" not in error_text, f"username leaked into SyncError: {error_text}"


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


def test_pull_rebuilds_index(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """End-to-end pull: write+push from one clone, pull from another,
    verify the FTS5 index was rebuilt to reflect the pulled
    files."""
    # Set up source clone, write a memory, push.
    sync.init(memory_dir, remote=str(bare_remote))
    src_store = Store(memory_dir)
    src_store.write(content="python list comprehension", scopes=["tools"])
    sync.push(memory_dir)

    # Set up a separate clone that pulls.
    other_dir = tmp_path / "other_clone"
    subprocess.run(
        ["git", "clone", str(bare_remote), str(other_dir)],
        check=True,
        capture_output=True,
    )

    result = sync.pull(other_dir)
    assert result["pulled"] is True
    assert result["reindexed"] is True
    assert result["indexed_count"] == 1

    # The pulled file should be searchable via the rebuilt index.
    from bettermemory import index as _index

    candidates = _index.query(other_dir, "python")
    assert candidates


def test_pull_no_reindex_flag(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """`pull(reindex=False)` skips the rebuild — useful in batched
    sync scripts that defer the rebuild to the end."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="x", scopes=["tools"])
    sync.push(memory_dir)

    other_dir = tmp_path / "other_clone"
    subprocess.run(
        ["git", "clone", str(bare_remote), str(other_dir)],
        check=True,
        capture_output=True,
    )

    result = sync.pull(other_dir, reindex=False)
    assert result["pulled"] is True
    assert result["reindexed"] is False
    assert result["indexed_count"] is None


def test_pull_errors_without_remote(memory_dir: Path) -> None:
    """A pull against a repo with no `origin` should raise SyncError."""
    sync.init(memory_dir)
    with pytest.raises(sync.SyncError, match="no remote"):
        sync.pull(memory_dir)


# ---------------------------------------------------------------------------
# auto
# ---------------------------------------------------------------------------


def test_auto_pulls_then_pushes(memory_dir: Path, bare_remote: Path) -> None:
    """`auto` is pull + push in one call. With nothing on the remote
    yet, the pull is effectively a no-op; the push commits and
    sends. Both sub-results should be present in the return."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="hello", scopes=["tools"])

    # Wait — pull with no remote tracking yet would fail. Initialize
    # the remote first by pushing once so HEAD has an upstream.
    sync.push(memory_dir)

    # Add another change, then auto.
    Store(memory_dir).write(content="another fact", scopes=["tools"])
    result = sync.auto(memory_dir)
    assert "pull" in result
    assert "push" in result
    pull = result["pull"]
    push = result["push"]
    assert isinstance(pull, dict)
    assert isinstance(push, dict)
    assert pull["pulled"] is True
    assert push["pushed"] is True


# ---------------------------------------------------------------------------
# Name validation (F-S2 / F-S3) and pull --no-tags (L6)
# ---------------------------------------------------------------------------


def test_init_rejects_unsafe_default_branch(memory_dir: Path) -> None:
    """`default_branch` is positional to `git init --initial-branch`.
    A value starting with `-` (or containing shell-meaningful chars)
    could in some git versions get parsed as a flag rather than a
    branch name. The validator rejects anything outside the conservative
    safe set."""
    with pytest.raises(sync.SyncError, match="default_branch"):
        sync.init(memory_dir, default_branch="--exec=evil")


def test_push_rejects_unsafe_remote_name(memory_dir: Path, bare_remote: Path) -> None:
    """Same rule on the `remote` arg passed to push: validate against
    the safe charset before letting it through to `git push <remote>`."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="x", scopes=["tools"])
    with pytest.raises(sync.SyncError, match="remote"):
        sync.push(memory_dir, remote="--exec=evil")


def test_pull_rejects_unsafe_remote_name(memory_dir: Path) -> None:
    """Same rule on the `remote` arg passed to pull."""
    sync.init(memory_dir)
    with pytest.raises(sync.SyncError, match="remote"):
        sync.pull(memory_dir, remote="--upload-pack=evil")


def test_pull_uses_no_tags(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: `git pull --rebase` must include `--no-tags`. A
    hostile or sloppy remote pushing refs under `refs/tags/` would
    otherwise be silently mirrored into the local `.git/refs/tags/`,
    where a tag named `main` could shadow the branch."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="x", scopes=["tools"])
    sync.push(memory_dir)

    captured_args: list[list[str]] = []
    original_run_git = sync._run_git

    def capturing_run_git(
        root: Path, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        captured_args.append(list(args))
        return original_run_git(root, args, check=check)

    monkeypatch.setattr(sync, "_run_git", capturing_run_git)
    sync.pull(memory_dir, reindex=False)

    pull_calls = [a for a in captured_args if a and a[0] == "pull"]
    assert pull_calls, f"no pull subcommand in captured args: {captured_args}"
    assert "--no-tags" in pull_calls[0], (
        f"`git pull` missing --no-tags: {pull_calls[0]}"
    )


# ---------------------------------------------------------------------------
# Filename-constant cross-module parity (Class 6 — closed by this commit).
#
# `_GITIGNORE_LINES` in `sync.py` enumerates the regenerable / transient
# filenames the runtime writes alongside the canonical markdown store —
# the FTS5 index (`.index.sqlite` + its `-shm` / `-wal` sidecars), the
# event log (`.events.jsonl` + rotated `.events.jsonl.*.gz`), the
# embedding cache (`.embeddings.<safe>.npz`), and the doctor probe
# (`.doctor-probe`). Each filename is also defined as a canonical
# constant in the module that *writes* the file:
#
#   - `events.py:EVENT_LOG_FILENAME`            → `.events.jsonl`
#   - `index.py:INDEX_FILENAME`                 → `.index.sqlite`
#   - `semantic.py:EMBEDDING_FILENAME_PREFIX`   → `.embeddings.`
#     `semantic.py:EMBEDDING_FILENAME_SUFFIX`   → `.npz`
#   - `doctor.py:DOCTOR_PROBE_FILENAME`         → `.doctor-probe`
#
# Hazard: a future rename of any canonical filename constant updates
# the writer but, prior to this commit, would leave the sync gitignore
# referring to a stale literal — silently checking the newly-named
# regenerable file into the user's sync repo. Class 6 in the
# tick-23 Branch B audit.
#
# Fix shape: `sync.py` now IMPORTS the constants instead of hardcoding
# the literals; this guard asserts the constant values actually appear
# in `_GITIGNORE_LINES`, so a future contributor who imports the
# constant but accidentally drops it from the list still trips the
# pin.
#
# Negative-controls verified at commit time (see commit message for
# detail).
# ---------------------------------------------------------------------------


def test_gitignore_lines_include_canonical_filename_constants() -> None:
    """`_GITIGNORE_LINES` (`sync.py`) MUST include the canonical
    filename constants from the modules that write those files.
    Renaming a constant updates the writer but, without this guard,
    silently leaves the gitignore referring to a stale literal — and
    the now-tracked regenerable file lands in the user's sync repo.

    Closes Class 6 (filename-constant cross-module parity) from the
    tick-23 Branch B audit."""
    assert INDEX_FILENAME in sync._GITIGNORE_LINES, (
        f"sync._GITIGNORE_LINES missing INDEX_FILENAME "
        f"({INDEX_FILENAME!r}); see sync.py:_GITIGNORE_LINES"
    )
    assert EVENT_LOG_FILENAME in sync._GITIGNORE_LINES, (
        f"sync._GITIGNORE_LINES missing EVENT_LOG_FILENAME "
        f"({EVENT_LOG_FILENAME!r}); see sync.py:_GITIGNORE_LINES"
    )
    assert DOCTOR_PROBE_FILENAME in sync._GITIGNORE_LINES, (
        f"sync._GITIGNORE_LINES missing DOCTOR_PROBE_FILENAME "
        f"({DOCTOR_PROBE_FILENAME!r}); see sync.py:_GITIGNORE_LINES"
    )
    # The write-reflex proposal queue is host-local transient state that
    # carries raw captured user text (possibly a secret that never passed the
    # write-path credential gate) — it must never sync. A rename of the
    # constant that misses the gitignore would re-open the 🔴 plaintext leak.
    assert PROPOSALS_FILENAME in sync._GITIGNORE_LINES, (
        f"sync._GITIGNORE_LINES missing PROPOSALS_FILENAME "
        f"({PROPOSALS_FILENAME!r}); see sync.py:_GITIGNORE_LINES"
    )
    # Embedding cache is a glob; assert it was built from the lifted
    # prefix/suffix constants. A rename of either half without
    # updating the sync glob would strand the rebuilt cache in git.
    embedding_glob = f"{EMBEDDING_FILENAME_PREFIX}*{EMBEDDING_FILENAME_SUFFIX}"
    assert embedding_glob in sync._GITIGNORE_LINES, (
        f"sync._GITIGNORE_LINES missing embedding-cache glob "
        f"({embedding_glob!r}) built from "
        f"semantic.EMBEDDING_FILENAME_PREFIX / SUFFIX"
    )
    # Orphaned atomic-write `*.tmp` sidecars carry the same plaintext payload
    # as the file they were about to become (a memory body, or the raw-capture
    # proposals queue). `_fsutil.atomic_write_bytes` strands one when a crash
    # lands between tmp creation and `os.replace`; without this glob the next
    # `sync push` stages it. Pin the literal so a refactor of
    # `_GITIGNORE_LINES` can't silently drop the guard.
    assert "*.tmp" in sync._GITIGNORE_LINES, (
        "sync._GITIGNORE_LINES missing the '*.tmp' glob; orphaned "
        "atomic_write_bytes temp files (which carry raw memory / proposal "
        "payloads) would be staged, committed, and pushed by `sync push`"
    )
