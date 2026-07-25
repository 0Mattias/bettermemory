"""Tests for the git-based sync wrapper (T4.1 of the v1.6 plan).

Tests use a local bare repository as the "remote" so push and pull
exercise real git transport without needing network access. Each
test sets up its own pair of stores via tmp_path so the suite stays
hermetic.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import logging
import shutil
import subprocess
import sys
import threading
from collections.abc import Generator
from pathlib import Path

import pytest

from bettermemory import sync
from bettermemory._fsutil import flock_excl
from bettermemory.cli import main as cli_main
from bettermemory.doctor import DOCTOR_PROBE_FILENAME
from bettermemory.episodes import EPISODES_DIR
from bettermemory.events import EVENT_LOG_FILENAME, _SEGMENT_TEMPLATE
from bettermemory.index import INDEX_FILENAME
from bettermemory.ingest import INGEST_WATERMARK_FILENAME
from bettermemory.proposals import PROPOSALS_FILENAME
from bettermemory.semantic import (
    EMBEDDING_FILENAME_PREFIX,
    EMBEDDING_FILENAME_SUFFIX,
)
from bettermemory.store import TOMBSTONE_DIR, Store


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
    plaintext. This has now happened SIX times, each time silently:

    * the write-reflex proposal queue `.write_proposals.jsonl` (raw user text
      that never passed the credential gate);
    * orphaned atomic-write `*.tmp` sidecars (which carry a full memory body,
      or the raw proposals queue);
    * the ingest watermark `.ingest-watermark.json` (absolute host paths);
    * the auto-consolidate clock (host-local debounce state, rewritten on
      every decision, so it also conflicts on every pull);
    * `episodes/` (host-local session run-state — it synced only by omission,
      being a directory the dotfile-oriented discovery never evaluated);
    * the rotated event-log archives `.events-{ts}.jsonl.gz` (session ids and,
      in verbatim mode, raw query text).

    The count and the list matter: this docstring claimed THREE for several
    releases after the sixth had shipped, which understates the base rate of
    the very class the guard exists to close and makes the guard look more
    speculative than it is.

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


def test_event_log_archives_shards_and_rotating_are_gitignored() -> None:
    """Regression: every event-log file the runtime writes into the
    store root must be excluded from sync — the legacy active log, the
    sharded active segments (`.events.NN.jsonl`, v3.24.0), the rotated
    `.events-{ts}.jsonl.gz` archives, AND the crashed-rotation
    `.rotating` holding files. All carry session ids and, in verbatim
    mode, raw query text.

    The archives were silently leaking: the pattern was
    `.events.jsonl.*.gz`, which matches NONE of the real archive names
    (they are `.events-{ts}.jsonl.gz` — a dash after "events", not a
    dot). The structural `test_every_store_root_sidecar_is_gitignored`
    guard couldn't catch it because an archive name is composed from
    `ARCHIVE_PREFIX` at runtime, not a single `*_FILENAME` constant it
    can discover. This pins the composed names directly."""
    patterns = [
        line for line in sync._GITIGNORE_LINES if not line.lstrip().startswith("#")
    ]
    must_ignore = [
        ".events.jsonl",  # legacy active log
        ".events.00.jsonl",  # sharded active segment
        ".events.15.jsonl",  # highest shard
        ".events-20260101T000000Z.jsonl.gz",  # rotated archive
        ".events-20260101T000000Z-sess-1.jsonl.gz",  # collision-suffixed archive
        ".events-20260101T000000Z.jsonl.rotating",  # crashed-rotation holding file
    ]
    for name in must_ignore:
        assert any(fnmatch.fnmatch(name, pat) for pat in patterns), (
            f"{name} is not excluded by any sync gitignore pattern — "
            "event-log data (session ids, query text) would be committed and "
            "pushed to every clone by `sync push`"
        )


# Sample tokens substituted into a `*_TEMPLATE` placeholder / appended to a
# `*_PREFIX` when reconstructing the names the runtime composes at run time.
# Three shapes because a template field can be numeric-padded (`{:02d}` →
# `00`), plain numeric, or free text (a timestamp, a session id), and a
# pattern that only covers one of those is a hole.
_SAMPLE_TOKENS = ("00", "0", "X")


def _composed_sidecar_names() -> dict[str, str]:
    """Every store-root filename the runtime COMPOSES at run time, keyed by
    the constant(s) it is composed from.

    The `*_FILENAME` guard above can only see names that exist as a whole
    string in some constant. The names that actually leaked came from
    fragments assembled at run time — `.events-{ts}.jsonl.gz` from
    `ARCHIVE_PREFIX`/`ARCHIVE_SUFFIX` (5th instance of the sidecar leak
    class), `.events.{shard:02d}.jsonl` from `_SEGMENT_TEMPLATE` (6th) — and
    both were invisible to it BY CONSTRUCTION, which is why each was closed
    by hand-writing one more literal test after the leak had already shipped.

    So reconstruct them. Fragment constants are recognised by suffix:

    * `*_TEMPLATE` with a dotfile value → rendered by substituting each
      `{...}` field with each sample token.
    * `*_PREFIX` with a dotfile value → joined with a sample token and, when
      its module also declares `*_SUFFIX` constants, with each of those
      (that pairing is exactly how `events` / `semantic` build their names:
      ``f"{ARCHIVE_PREFIX}{ts}{ARCHIVE_SUFFIX}"``). A module with no suffix
      constant yields the bare `prefix + token` form instead.
    * `*_SUFFIX` alone is never a filename, so it is only ever used as the
      tail of a composition.

    `*_FILENAME` is deliberately NOT re-checked here — that is the other
    guard's job, allowlist and all; this one owns the composed shapes.

    Over-generation is the intended failure mode: a prefix paired with an
    unrelated suffix yields a name nothing writes, and the worst that costs
    is one more (harmless, positive) glob in `_GITIGNORE_LINES` or an
    allowlist entry. Under-generation is what ships a leak.
    """
    import importlib
    import pkgutil
    import re

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
        fragments: dict[str, str] = {}
        for attr in dir(mod):
            if not attr.endswith(("_TEMPLATE", "_PREFIX", "_SUFFIX")):
                continue
            value = getattr(mod, attr)
            if isinstance(value, str) and value:
                fragments[attr] = value
        suffixes = {a: v for a, v in fragments.items() if a.endswith("_SUFFIX")}
        for attr, value in fragments.items():
            if not value.startswith("."):
                continue
            qual = f"{mod_info.name}.{attr}"
            if attr.endswith("_TEMPLATE"):
                for token in _SAMPLE_TOKENS:
                    found[f"{qual}[{token}]"] = re.sub(r"\{[^{}]*\}", token, value)
            elif attr.endswith("_PREFIX"):
                if suffixes:
                    for suffix_attr, suffix in suffixes.items():
                        found[f"{qual}+{suffix_attr}"] = f"{value}X{suffix}"
                else:
                    found[f"{qual}[X]"] = f"{value}X"
    return found


def test_every_runtime_composed_sidecar_name_is_gitignored() -> None:
    """STRUCTURAL GUARD, composed-name edition. Six store-root sidecar leaks
    have now been closed by adding one literal to `_GITIGNORE_LINES`, and the
    CLASS stayed open every time — in part because the structural guard walks
    `*_FILENAME` module constants and cannot see a name assembled at run
    time. The two most recent leaks were exactly that shape: the rotated
    archives (`ARCHIVE_PREFIX` + timestamp + `ARCHIVE_SUFFIX`) and the
    sharded active segments (`_SEGMENT_TEMPLATE.format(shard)`).

    This guard reconstructs those names from their fragments, so the NEXT
    template / prefix a contributor adds is checked at the moment it lands
    rather than after it has been pushed to somebody's remote.

    Mutation-sound, and specifically sound where the older guard is BLIND:
    delete `".events.*.jsonl"` from `sync._GITIGNORE_LINES` and this test
    fails naming `events._SEGMENT_TEMPLATE` while
    `test_every_store_root_sidecar_is_gitignored` stays green; delete
    `f"{ARCHIVE_PREFIX}*"` and it fails naming the
    `ARCHIVE_PREFIX+ARCHIVE_SUFFIX` / `+ROTATING_SUFFIX` compositions. Both
    verified at commit time."""
    patterns = [
        line for line in sync._GITIGNORE_LINES if not line.lstrip().startswith("#")
    ]
    composed = _composed_sidecar_names()
    # Sanity: reconstruction works at all. If this trips, the walk or the
    # rendering broke, not sync.
    assert any("._SEGMENT_TEMPLATE" in origin for origin in composed), (
        f"composed-name discovery found no event segment: {sorted(composed)}"
    )
    assert ".events.00.jsonl" in composed.values(), (
        f"template rendering did not reproduce a real shard name: {composed}"
    )

    unignored = {
        origin: name
        for origin, name in composed.items()
        if name not in _INTENTIONALLY_SYNCED_SIDECARS
        and not any(fnmatch.fnmatch(name, pat) for pat in patterns)
    }
    assert not unignored, (
        "runtime-composed store-root sidecar name(s) missing from "
        f"sync._GITIGNORE_LINES — `sync push` would commit and push them to "
        f"every clone: {unignored}. Add a glob covering the composed shape "
        "(build it from the same fragment constants, e.g. "
        "f'{ARCHIVE_PREFIX}*'), or, if the file is genuinely portable and "
        "worth versioning, add its value to _INTENTIONALLY_SYNCED_SIDECARS "
        "with a rationale."
    )


def test_composed_sidecar_discovery_sees_a_new_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation guard for the reconstruction itself — without it the guard
    above is only as good as its fragment discovery, and a guard that
    quietly discovers nothing is worse than no guard (it reports success).

    Plant a template and a prefix/suffix pair in a real subpackage module
    and require both composed shapes to surface, exactly as a future
    contributor's new sidecar would."""
    from bettermemory.handlers import write as write_handler

    monkeypatch.setattr(
        write_handler, "PLANTED_TEMPLATE", ".planted.{:02d}.log", raising=False
    )
    monkeypatch.setattr(write_handler, "PLANTED_PREFIX", ".planted-", raising=False)
    monkeypatch.setattr(write_handler, "PLANTED_SUFFIX", ".log.gz", raising=False)

    names = set(_composed_sidecar_names().values())
    assert ".planted.00.log" in names, (
        "composed-name discovery did not render a planted `*_TEMPLATE` — a "
        f"runtime-composed sidecar would leak past the guard: {sorted(names)}"
    )
    assert ".planted-X.log.gz" in names, (
        "composed-name discovery did not join a planted `*_PREFIX` with its "
        f"module's `*_SUFFIX`: {sorted(names)}"
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
    `_scan_parent_index_for_sidecars` translate `_GITIGNORE_LINES` via the
    shared `_pattern_matches_tracked_path`: each non-comment line is
    fnmatched against every `/`-separated component of each tracked path,
    and any matching component marks the path ignored (a matching directory
    name ignores everything beneath it). That translation is only
    equivalent to git's .gitignore semantics for POSITIVE, SLASH-FREE
    lines — and doctor's comments cite THIS test as what keeps the list
    that shape:

    - A `!` negation line un-ignores a file for git (it is DELIBERATELY
      tracked), but per-component fnmatch has no negation semantics
      either — fnmatch reads a leading `!` as a literal character, never
      an un-ignore: the kept file still matches some positive glob in one
      of its components, so doctor FAILs and its fix_hint walks the user
      through `git rm --cached`, a history rewrite, and secret rotation —
      a DESTRUCTIVE false positive against a file the gitignore
      intentionally keeps.
    - A `/`-anchored (or any slash-containing) line matches path segments
      relative to the .gitignore for git, but the components fnmatch sees
      come from splitting the repo-relative tracked path ON `/`, so no
      component ever contains a `/` and the line silently matches
      nothing — a false NEGATIVE in the exact leak guard meant to catch
      tracked secrets.

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
            "doctor's per-component translation (_pattern_matches_tracked_path, "
            "shared by _check_sync_tracked_ignored and "
            "_scan_parent_index_for_sidecars) has no negation semantics — "
            "fnmatch reads a leading `!` as a literal character — so the "
            "deliberately-kept file would still match a positive glob in one "
            "of its path components and doctor would FAIL with a destructive "
            "fix_hint (`git rm --cached` + history rewrite + secret rotation) "
            "for a file the gitignore keeps on purpose. Restructure the "
            "patterns so no negation is needed."
        )
        assert "/" not in line, (
            f"sync._GITIGNORE_LINES contains a slash-anchored line: {line!r}. "
            "doctor's per-component translation (_pattern_matches_tracked_path, "
            "shared by _check_sync_tracked_ignored and "
            "_scan_parent_index_for_sidecars) fnmatches each line against the "
            "`/`-separated components of each tracked path, and splitting on "
            "`/` means no component ever contains a `/` — so a `/`-bearing "
            "line can match no component and silently matches nothing: a "
            "false negative in the tracked-sidecar leak guard. Use a "
            "slash-free filename or glob instead."
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


def test_init_reports_an_unreadable_gitignore_instead_of_claiming_canonical(
    memory_dir: Path,
) -> None:
    """🟡 HONEST REPORTING. `_reconcile_gitignore` swallows an OSError on the
    read and stands down (deliberately — we cannot know what is in the file,
    and clobbering a user's exclusions is worse). But it used to signal that
    stand-down with the SAME value it used for success: a bare `[]`, which
    also means "every pattern was already present". `init` mapped the empty
    list onto the action line ".gitignore already in canonical shape" — so a
    store whose `.gitignore` could not be read at all was reported to the
    user as CORRECT, and the user had no way to learn otherwise.

    The outcome is explicit now, so the failure is stated in the action list
    and exposed as a machine-readable `gitignore_error` for `--json`
    consumers. A directory at the `.gitignore` path is the portable way to
    make the read fail.

    Mutation-sound: revert `_reconcile_gitignore` to returning `[]` on the
    read OSError and this fails on both the `gitignore_error` assertion and
    the "must not claim canonical" assertion."""
    sync.init(memory_dir)
    gitignore = memory_dir / ".gitignore"
    gitignore.unlink()
    gitignore.mkdir()

    result = sync.init(memory_dir)

    error = result["gitignore_error"]
    assert isinstance(error, str) and error, (
        f"an unreadable .gitignore was not reported as an error: {result}"
    )
    assert ".gitignore" in error, f"the error does not name the file: {error!r}"

    actions = result["actions"]
    assert isinstance(actions, list)
    assert not any("already in canonical shape" in str(a) for a in actions), (
        "init reported an UNREADABLE .gitignore to the user as already "
        f"correct — the exact false-assurance this pins: {actions}"
    )
    assert any("NOT reconciled" in str(a) for a in actions), (
        f"init's action list does not surface the failure: {actions}"
    )
    # …and the stand-down really stood down: the path is as we left it.
    assert gitignore.is_dir()


def test_init_reports_canonical_shape_when_the_gitignore_is_complete(
    memory_dir: Path,
) -> None:
    """The other side of the pin above: when the reconcile genuinely finds
    nothing missing, `gitignore_error` is None and the canonical-shape action
    line is still the one the user sees. Without this, a fix that reported
    "NOT reconciled" unconditionally would satisfy the test above."""
    sync.init(memory_dir)
    result = sync.init(memory_dir)
    assert result["gitignore_error"] is None
    actions = result["actions"]
    assert isinstance(actions, list)
    assert any("already in canonical shape" in str(a) for a in actions), actions


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


def _awkward_but_legal_names() -> list[str]:
    """File names that the newline-delimited porcelain format mangles.

    A space forces git to C-quote the whole path (`"with space.md"`), and a
    double quote is additionally backslash-escaped inside it. `core.quotePath`
    does not switch that off — it governs non-ASCII bytes only. NTFS forbids
    `"` in a file name, so the quote case is POSIX-only; the space case runs
    everywhere the suite does.
    """
    names = ["with space.md"]
    if sys.platform != "win32":
        names.append('quo"te.md')
    return names


def test_status_reports_renamed_and_quoted_paths_verbatim(memory_dir: Path) -> None:
    """🟡 THE PARSER MUST YIELD PATHS, NOT PORCELAIN FRAGMENTS.

    Two shapes defeat a positional slice of newline-delimited porcelain:

    * A rename is emitted as `XY ORIG_PATH -> PATH` on ONE line, so the
      slice yields the literal `"orig.md -> renamed.md"` as a single path.
    * A path containing a space or a quote is C-quoted, so the slice yields
      a path wrapped in `"` with `\\`-escaped innards.

    Both reach the user: `sync status` prints these as file names, and
    `pull`'s dirty-worktree error names them as the files to go fix. A name
    the user cannot find in their store is not an actionable error.

    `-z` fixes both — it turns quoting off and moves ORIG_PATH into its own
    field — and this test pins the outcome rather than the mechanism.

    Non-vacuous by construction: it asserts git really did emit a rename
    record before checking how it was parsed, so a git that stopped
    detecting the rename fails the test instead of skipping the branch.
    """
    sync.init(memory_dir)
    names = _awkward_but_legal_names()
    for name in [*names, "orig.md"]:
        (memory_dir / name).write_text("body\n", encoding="utf-8")
    _git(memory_dir, "add", "-A")
    _git(memory_dir, "commit", "-m", "seed")

    _git(memory_dir, "mv", "orig.md", "renamed.md")
    for name in names:
        path = memory_dir / name
        path.write_text(path.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")

    raw = subprocess.run(
        ["git", "status", "--porcelain", "-z"],
        cwd=memory_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert any(field.startswith("R") for field in raw.split("\0")), (
        f"git did not record the `git mv` as a rename, so the rename branch "
        f"of the parser is untested here: {raw!r}"
    )

    st = sync.status(memory_dir)

    assert "renamed.md" in st.modified, (
        f"the rename's destination path is not reported: {st.modified}"
    )
    assert not any("->" in path for path in st.modified), (
        f"a porcelain rename arrow leaked into a path: {st.modified}"
    )
    assert "orig.md" not in st.modified, (
        f"the rename's source path was reported as a modified file: {st.modified}"
    )
    for name in names:
        assert name in st.modified, (
            f"{name!r} is not reported under its real name: {st.modified}"
        )
    # One entry per edited file plus the rename — no ORIG_PATH field leaking
    # in as an extra entry of its own.
    assert len(st.modified) == len(names) + 1, st.modified


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


# Store-root DIRECTORIES that are DELIBERATELY tracked by the sync repo.
# `.tombstones/` holds the bodies of removed memories: canonical store data,
# not host-local runtime state — a removal made on one host must stay
# restorable (`memory_restore`) from every clone, and the sync module's own
# docstring reserves a conflict-detection pass over the tombstone audit
# trail. An entry here is a conscious decision that a directory's contents
# are portable across hosts AND worth versioning — not a place to silence
# the guard below.
_INTENTIONALLY_SYNCED_DIRS: frozenset[str] = frozenset({TOMBSTONE_DIR})


def _store_root_dir_constants() -> dict[str, str]:
    """Every `*_DIR` / `*_DIRNAME` constant in the package naming a
    store-root directory.

    Same walk, same keying, and same rationale as
    `_sidecar_filename_constants`: the guard discovers constants rather than
    trusting a hand-list. The value filter differs — directories are not
    dotfile-shaped, so a store-root directory constant is recognised by
    being a BARE RELATIVE dirname (a non-empty string with no path
    separator). As with `*_FILENAME`, the naming convention is the
    contract: a `*_DIR` constant that does not name a store-root directory
    shouldn't wear that suffix. Today exactly two exist
    (`store.TOMBSTONE_DIR`, `episodes.EPISODES_DIR`) and both are decided
    below.
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
            if not (attr.endswith("_DIR") or attr.endswith("_DIRNAME")):
                continue
            value = getattr(mod, attr)
            if (
                isinstance(value, str)
                and value
                and "/" not in value
                and "\\" not in value
            ):
                found[f"{mod_info.name}.{attr}"] = value
    return found


def test_every_store_root_directory_is_sync_decided() -> None:
    """STRUCTURAL GUARD, directory edition. The `*_FILENAME` guard above
    cannot see directories: `episodes/` shipped for weeks silently INCLUDED
    in `sync push` — never decided, just absent from `_GITIGNORE_LINES` in a
    shape the dotfile-oriented discovery never surfaced. Whole-directory
    payloads are the largest single thing `git add -A` can stage, so an
    undecided directory is the sidecar leak class at its widest.

    Every `*_DIR`/`*_DIRNAME` constant must therefore be either matched by a
    line in `_GITIGNORE_LINES` (slash-free by the pattern-shape fence above,
    so a bare name ignores the directory and everything beneath it) or
    listed in `_INTENTIONALLY_SYNCED_DIRS` with a rationale.

    Mutation-sound both ways: dropping `EPISODES_DIR` from
    `sync._GITIGNORE_LINES` fails this naming `episodes.EPISODES_DIR`;
    dropping `TOMBSTONE_DIR` from the allowlist fails it naming
    `store.TOMBSTONE_DIR`."""
    patterns = [
        line for line in sync._GITIGNORE_LINES if not line.lstrip().startswith("#")
    ]
    dirs = _store_root_dir_constants()
    # Sanity: discovery works at all. If this trips, the walk broke.
    assert any(name.endswith(".EPISODES_DIR") for name in dirs), (
        f"directory discovery found nothing recognisable: {sorted(dirs)}"
    )
    undecided = {
        qualname: value
        for qualname, value in dirs.items()
        if value not in _INTENTIONALLY_SYNCED_DIRS
        and not any(fnmatch.fnmatch(value, pat) for pat in patterns)
    }
    assert not undecided, (
        "store-root director(ies) with no sync decision — `sync push` would "
        f"commit and push their contents to every clone: {undecided}. Add "
        "the constant to sync._GITIGNORE_LINES (import it from its owning "
        "module), or, if the directory is canonical store data worth "
        "versioning on every clone, add it to _INTENTIONALLY_SYNCED_DIRS "
        "with a rationale."
    )


def test_push_excludes_episodes_and_keeps_tombstones(
    memory_dir: Path, bare_remote: Path
) -> None:
    """Episodes are host-local BY DESIGN (decided 2026-07-11; previously
    included only by omission): run-state bodies carry host-absolute
    `origin.worktree_root` paths, the TTL prune keys on mtimes that a
    clone's `git checkout` would reset, and `episode_handoff` adoption is
    worktree-strict, so a synced episode would be filtered on arrival
    anyway. `.tombstones/`, by contrast, is canonical store data: a removal
    made on one host must stay restorable from every clone.

    Mutation-sound: drop `EPISODES_DIR` from `sync._GITIGNORE_LINES` and the
    committed-tree assertion fails; the tombstone assertion pins the
    allowlist side so the directory guard's exemption stays real, not
    vestigial."""
    sync.init(memory_dir, remote=str(bare_remote))
    # A real memory gives the push canonical content to commit.
    Store(memory_dir).write(content="durable fact", scopes=["tools"])
    # Simulate a prior session's journal and a removed memory's tombstone.
    episode = memory_dir / EPISODES_DIR / "sess_testcafe" / "01EPISODETEST.md"
    episode.parent.mkdir(parents=True)
    episode.write_text(
        "run-state: tried X in /Users/nobody/worktree\n", encoding="utf-8"
    )
    tombstone = memory_dir / TOMBSTONE_DIR / "01TOMBSTONETEST.md"
    tombstone.parent.mkdir(exist_ok=True)
    tombstone.write_text("removed body\n", encoding="utf-8")

    result = sync.push(memory_dir)
    assert result["pushed"] is True

    committed = _git(memory_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert not any(p.startswith(f"{EPISODES_DIR}/") for p in committed), (
        f"episodes leaked into the committed tree: {committed}"
    )
    assert f"{TOMBSTONE_DIR}/01TOMBSTONETEST.md" in committed, (
        f".tombstones/ should sync (canonical store data): {committed}"
    )
    # git itself agrees the episode path is ignored (rc==0 means "ignored").
    check = subprocess.run(
        ["git", "check-ignore", f"{EPISODES_DIR}/sess_testcafe/01EPISODETEST.md"],
        cwd=memory_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, (
        "episodes/ is not gitignored; `sync push` would stage session journals"
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


# ---------------------------------------------------------------------------
# The UPGRADE PATH: `_GITIGNORE_LINES` reaching an ALREADY-INITIALISED store.
#
# Six store-root sidecar leaks have been closed by adding a line to
# `_GITIGNORE_LINES` — and until this commit that constant only ever reached a
# store's on-disk `.gitignore` through `sync.init`. Every store initialised
# before a given line existed therefore kept its old file forever, so each
# "fix" was inert exactly where the data already lived. `sync push` now
# reconciles the two on every run, so the seventh line lands everywhere.
# ---------------------------------------------------------------------------


def _gitignore_patterns() -> list[str]:
    """The non-comment lines of `_GITIGNORE_LINES` — the rules that must
    actually be present in a store's on-disk `.gitignore`."""
    return [
        line
        for line in sync._GITIGNORE_LINES
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _pre_shard_gitignore_text() -> str:
    """The `.gitignore` a store initialised before 3.24.0 carries: today's
    canonical block minus the `.events.*.jsonl` line 3.24.0 added for the
    sharded active event segments."""
    return (
        "\n".join(line for line in sync._GITIGNORE_LINES if line != ".events.*.jsonl")
        + "\n"
    )


def test_push_heals_pre_shard_gitignore_and_excludes_event_segments(
    memory_dir: Path, bare_remote: Path
) -> None:
    """🔴 PRIVACY REGRESSION (6th instance of the store-root-sidecar leak
    class). 3.24.0 added `.events.*.jsonl` to `_GITIGNORE_LINES` for the 16
    sharded active event segments, but only `sync.init` ever wrote that
    constant into a store's `.gitignore`. A store initialised BEFORE 3.24.0
    keeps its old file, so its segments stay UNIGNORED and the next
    `sync push`'s `git add -A` commits and pushes them to the user's remote:
    raw event telemetry — search queries, session ids, memory ids — landing
    permanently in a git history.

    The write path was narrower than the read path, which is the shape every
    instance of this class has had. `push` now reconciles the on-disk file
    against `_GITIGNORE_LINES` before staging, and this test pins the
    reconcile GENERICALLY: every current pattern must be present after the
    push, so the next line to join the list is carried to pre-existing
    stores by the same mechanism rather than needing its own new test.

    Mutation-sound: revert `sync.py` and this fails on the committed-tree
    assertion with `.events.03.jsonl` staged, committed, and pushed, session
    id and query text intact (verified by stashing the source change)."""
    import json

    sync.init(memory_dir, remote=str(bare_remote))
    gitignore = memory_dir / ".gitignore"
    # Roll the store back to the pre-3.24.0 on-disk shape and commit it, so
    # the repo is indistinguishable from one initialised before the line
    # existed.
    gitignore.write_text(_pre_shard_gitignore_text(), encoding="utf-8")
    assert ".events.*.jsonl" not in gitignore.read_text(encoding="utf-8")
    Store(memory_dir).write(content="durable fact", scopes=["tools"])
    _git(memory_dir, "add", "-A")
    _git(memory_dir, "commit", "-m", "pre-3.24.0 store")

    # The upgraded runtime now writes a sharded active segment. Its payload
    # is the real thing: a session id and (verbatim mode) the raw query text
    # the user typed.
    segment = memory_dir / _SEGMENT_TEMPLATE.format(3)
    query = "how do I rotate the prod signing key"
    segment.write_text(
        json.dumps(
            {
                "ts": "2026-07-19T00:00:00Z",
                "event": "search",
                "session_id": "sess-abc123",
                "query": query,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = sync.push(memory_dir)
    assert result["pushed"] is True

    # The leak itself first: the segment must reach neither the commit nor
    # the remote.
    committed = _git(memory_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert segment.name not in committed, (
        f"sharded event segment leaked into the committed tree: {committed}"
    )
    check = subprocess.run(
        ["git", "check-ignore", segment.name],
        cwd=memory_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, (
        f"{segment.name} is not gitignored (git check-ignore rc="
        f"{check.returncode}); `sync push` would stage it"
    )
    remote_history = _git(bare_remote, "log", "-p", "--all")
    assert query not in remote_history, (
        "raw query text from a sharded event segment reached the remote history"
    )
    assert "sess-abc123" not in remote_history, (
        "session id from a sharded event segment reached the remote history"
    )

    # …and the mechanism that prevented it: the reconcile healed the file,
    # GENERICALLY, for every pattern in the list.
    healed = gitignore.read_text(encoding="utf-8")
    missing = [pat for pat in _gitignore_patterns() if pat not in healed.splitlines()]
    assert not missing, (
        f"`sync push` left {missing} out of the store's .gitignore; a store "
        "initialised before those lines existed keeps leaking the files they "
        "cover"
    )


def test_push_gitignore_reconcile_is_idempotent(
    memory_dir: Path, bare_remote: Path
) -> None:
    """Reconciling on EVERY push only works if it is a no-op once the file
    is complete: an append that re-ran unconditionally would grow the
    `.gitignore` by a full copy of `_GITIGNORE_LINES` on every sync and
    dirty the tree on every run (an endless stream of gitignore-only sync
    commits). Each pattern must appear exactly once, and the file must be
    byte-identical across the second and third push."""
    sync.init(memory_dir, remote=str(bare_remote))
    gitignore = memory_dir / ".gitignore"
    gitignore.write_text(_pre_shard_gitignore_text(), encoding="utf-8")

    Store(memory_dir).write(content="durable fact", scopes=["tools"])
    sync.push(memory_dir)
    after_first = gitignore.read_text(encoding="utf-8")

    Store(memory_dir).write(content="another fact", scopes=["tools"])
    sync.push(memory_dir)
    after_second = gitignore.read_text(encoding="utf-8")
    sync.push(memory_dir)
    after_third = gitignore.read_text(encoding="utf-8")

    assert after_first == after_second == after_third, (
        "repeated syncs rewrote the .gitignore; the reconcile must be a "
        f"no-op once complete:\n{after_first!r}\nvs\n{after_second!r}"
    )
    lines = after_third.splitlines()
    counts = {pat: lines.count(pat) for pat in _gitignore_patterns()}
    assert all(count == 1 for count in counts.values()), (
        "repeated syncs duplicated ignore rules: "
        f"{ {p: c for p, c in counts.items() if c != 1} }"
    )
    # The upgrade header is a one-time marker, not a per-sync stamp.
    assert lines.count(sync._GITIGNORE_UPGRADE_HEADER) <= 1


def test_push_gitignore_reconcile_preserves_user_edits(
    memory_dir: Path, bare_remote: Path
) -> None:
    """A store's `.gitignore` is a file users legitimately edit — their own
    machine-local exclusions live there too. The reconcile is append-only
    for exactly that reason: rewriting the canonical block wholesale (what
    `init` used to do) silently deletes those lines, and the user finds out
    when their excluded file shows up on the remote.

    Also pins the no-trailing-newline case: a hand-edited file whose last
    line has no `\\n` must not get the first appended pattern glued onto it,
    which would corrupt BOTH rules."""
    sync.init(memory_dir, remote=str(bare_remote))
    gitignore = memory_dir / ".gitignore"
    # Pre-3.24.0 body + the user's own lines, deliberately without a
    # trailing newline on the last one.
    user_lines = ["# my own exclusions", "scratch-notes.txt", "*.bak"]
    gitignore.write_text(
        _pre_shard_gitignore_text() + "\n".join(user_lines),
        encoding="utf-8",
    )

    Store(memory_dir).write(content="durable fact", scopes=["tools"])
    (memory_dir / "scratch-notes.txt").write_text("private\n", encoding="utf-8")
    result = sync.push(memory_dir)
    assert result["pushed"] is True

    healed = gitignore.read_text(encoding="utf-8").splitlines()
    for line in user_lines:
        assert line in healed, (
            f"user-added .gitignore line {line!r} was destroyed by the sync "
            f"reconcile: {healed}"
        )
    assert ".events.*.jsonl" in healed  # …and the missing rule still landed.
    # The user's rule is still EFFECTIVE, not merely present as text — the
    # glued-line failure mode leaves the text visible but the rule broken.
    check = subprocess.run(
        ["git", "check-ignore", "scratch-notes.txt"],
        cwd=memory_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, (
        "the user's own ignore rule stopped working after the sync reconcile"
    )
    committed = _git(memory_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert "scratch-notes.txt" not in committed


def test_push_leaves_an_unreadable_gitignore_alone(
    memory_dir: Path, bare_remote: Path
) -> None:
    """If the `.gitignore` cannot be read, the reconcile stands down rather
    than clobbering it: we cannot know what is in it, and destroying a
    user's exclusions is worse than staging behaving as it does today
    (doctor's `sync_tracked_ignored` check still reports the store). A
    directory at the `.gitignore` path is the portable way to make the read
    fail; the push must survive it and leave the path as it found it."""
    sync.init(memory_dir, remote=str(bare_remote))
    gitignore = memory_dir / ".gitignore"
    gitignore.unlink()
    gitignore.mkdir()

    Store(memory_dir).write(content="durable fact", scopes=["tools"])
    result = sync.push(memory_dir)
    assert result["pushed"] is True
    assert gitignore.is_dir(), (
        "the reconcile overwrote an unreadable .gitignore instead of standing down"
    )


def test_push_survives_an_unwritable_gitignore(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """🟡 READ AND WRITE MUST SHARE ONE POLICY. `_reconcile_gitignore`
    guarded its READ against OSError with a deliberate stand-down (pinned by
    `test_push_leaves_an_unreadable_gitignore_alone`) but left BOTH
    `atomic_write_bytes` calls unguarded. That asymmetry was accidental, and
    it became load-bearing when the reconcile moved onto the `push` path:
    anything that broke the atomic write — an unwritable parent directory
    (read-only mount, permissions), ENOSPC — now raised a bare OSError out of
    `push`, so a push that previously succeeded took down the user's whole
    sync. Note a read-only `.gitignore` FILE is not in that class: the write
    is a rename into a writable directory, which POSIX allows regardless of
    the target file's own mode. The OSError is injected below rather than
    provoked for exactly that reason — the reachable causes are environmental
    and not portably reproducible in a test.

    The decided policy is stand down on BOTH halves: the reconcile is a
    healing side-effect on the push path, not the push's purpose, and
    trading "some ignore rules are not enforced yet" for "no memories reach
    the remote at all" is the worse bargain. It must be LOUD, though —
    logged at WARNING, naming the rules left unenforced — never silent.

    The store is rolled back to its pre-3.24.0 shape first so the reconcile
    has something to write; on an already-canonical file it never reaches
    the writer and the test would pass vacuously.

    Mutation-sound: remove the `try/except OSError` from
    `_write_gitignore_or_stand_down` and this fails with the injected
    OSError propagating out of `sync.push`."""
    sync.init(memory_dir, remote=str(bare_remote))
    gitignore = memory_dir / ".gitignore"
    gitignore.write_text(_pre_shard_gitignore_text(), encoding="utf-8")
    Store(memory_dir).write(content="durable fact", scopes=["tools"])

    def unwritable(path: Path, data: bytes, *, mode: int | None = None) -> None:
        raise OSError(30, "Read-only file system", str(path))

    # `sync.py` imports `atomic_write_bytes` by name, so the spy has to
    # replace the binding in `sync`'s own namespace.
    monkeypatch.setattr(sync, "atomic_write_bytes", unwritable, raising=False)

    with caplog.at_level(logging.WARNING, logger="bettermemory.sync"):
        result = sync.push(memory_dir)

    assert result["pushed"] is True, (
        "an unwritable .gitignore took down a push that would otherwise "
        "have succeeded — the user's memories stopped syncing entirely"
    )
    assert result["committed"] is True
    # The stand-down is loud: the warning names the file and the rules that
    # are still unenforced, so an operator can act on it.
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("could not write" in m for m in warnings), (
        f"the write stand-down was silent; warnings were: {warnings}"
    )
    assert any(".events.*.jsonl" in m for m in warnings), (
        f"the warning does not name the ignore rule left unenforced: {warnings}"
    )
    # And the file really was left as it was — no partial write.
    assert gitignore.read_text(encoding="utf-8") == _pre_shard_gitignore_text()


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


def _edit_tracked_memory(memory_dir: Path, store: Store, memory_id: str) -> str:
    """Append a line to an already-committed memory file, leaving the
    worktree dirty exactly as a user editing a memory would. Returns the
    file's repo-relative name."""
    path = store._find_path_for_id(memory_id)
    assert path is not None
    path.write_text(
        path.read_text(encoding="utf-8") + "\nedited locally\n", encoding="utf-8"
    )
    return path.name


def test_pull_names_the_dirty_files_instead_of_a_raw_git_error(
    memory_dir: Path, bare_remote: Path
) -> None:
    """🟡 ACTIONABLE FAILURE. `git pull --rebase` hard-refuses against a
    dirty worktree, and a live memory store is dirty most of the time —
    editing a memory and then syncing is the normal case. Pre-fix the user
    got git's generic "cannot pull with rebase: You have unstaged changes"
    wrapped in this wrapper's merge-conflict hint, which told them to run
    `git rebase --continue` for a situation where no rebase had started:
    advice that does nothing, attached to an error that never says WHICH
    file is in the way.

    The pre-check names the dirty files and the commands that fix it.

    Mutation-sound: remove the `_dirty_tracked_paths` pre-check from `pull`
    and this fails — git's own message contains neither the memory's
    filename nor a usable next step."""
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    memory = store.write(content="durable fact", scopes=["tools"])
    sync.push(memory_dir)

    edited = _edit_tracked_memory(memory_dir, store, memory.id)

    with pytest.raises(sync.SyncError) as excinfo:
        sync.pull(memory_dir)
    message = str(excinfo.value)
    assert edited in message, (
        f"the dirty-worktree error does not name the file blocking the pull: {message}"
    )
    assert "sync auto" in message or "sync push" in message, (
        f"the error gives the user no actionable next command: {message}"
    )
    # The inapplicable conflict hint must not be attached to this failure —
    # no rebase has started, so `git rebase --continue` cannot help.
    assert "rebase --continue" not in message, (
        f"dirty-worktree failure still carries the merge-conflict hint: {message}"
    )
    # Untracked files are NOT the problem and must not be named: a rebase
    # runs happily alongside them.
    (memory_dir / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    with pytest.raises(sync.SyncError) as second:
        sync.pull(memory_dir)
    assert "scratch.txt" not in str(second.value)


def test_pull_defers_to_rebase_autostash(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """🔴 THE PRE-CHECK MUST NOT REFUSE WHERE GIT WOULD HAVE COPED.

    `git pull --rebase` refuses against a dirty worktree only on a default
    config. With `rebase.autoStash` set, git stashes the local edits,
    rebases, and restores them — a configuration git supports precisely
    because this situation is common. An unconditional pre-check therefore
    turned a working `sync pull` into a hard error for exactly the users
    who had configured their way out of the problem.

    This test drives real git both ways from one fixture, so it pins the
    behaviour against git itself rather than against a belief about git:
    with autostash ON the pull must SUCCEED and the uncommitted edit must
    survive it; with the same worktree and autostash OFF the pull must
    still raise and name the file.

    Mutation-sound in both directions. Drop the `_rebase_autostash_enabled`
    gate in `pull` and the first half fails (SyncError where git was
    willing); drop the pre-check entirely and the second half fails (git's
    own message names neither the file nor a next step).

    The second clone advances the remote by editing a DIFFERENT memory, so
    the rebase has a commit to replay while the autostash pop has nothing
    to collide with — the test is about the guard, not about conflict
    resolution.
    """
    _git(memory_dir.parent, "config", "--global", "rebase.autoStash", "true")
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    mine = store.write(content="edited on this host", scopes=["tools"])
    theirs = store.write(content="edited on the other host", scopes=["tools"])
    sync.push(memory_dir)

    other = tmp_path / "other_clone"
    subprocess.run(
        ["git", "clone", str(bare_remote), str(other)],
        check=True,
        capture_output=True,
    )
    theirs_path = store._find_path_for_id(theirs.id)
    assert theirs_path is not None
    remote_copy = other / theirs_path.name
    remote_copy.write_text(
        remote_copy.read_text(encoding="utf-8") + "\nfrom the other host\n",
        encoding="utf-8",
    )
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "other host edit")
    _git(other, "push", "origin", "HEAD")

    edited = _edit_tracked_memory(memory_dir, store, mine.id)
    mine_path = store._find_path_for_id(mine.id)
    assert mine_path is not None
    assert sync._rebase_autostash_enabled(memory_dir) is True
    assert sync._dirty_tracked_paths(memory_dir), (
        "fixture left the worktree clean; the pre-check would not have fired "
        "either way and the test would pass vacuously"
    )

    result = sync.pull(memory_dir)

    assert result["pulled"] is True
    # The uncommitted edit came back out of the autostash.
    assert "edited locally" in mine_path.read_text(encoding="utf-8"), (
        "the pull ran but the autostashed local edit was not restored"
    )
    # And the remote commit really did land, so the pull was not a no-op.
    assert "from the other host" in (memory_dir / theirs_path.name).read_text(
        "utf-8"
    ), "the pull did not bring the remote commit down; nothing was rebased"

    # Same dirty worktree, autostash off: the guard is back and it names
    # the file. This is what proves the first half was the gate's doing.
    _git(memory_dir.parent, "config", "--global", "rebase.autoStash", "false")
    assert sync._rebase_autostash_enabled(memory_dir) is False
    with pytest.raises(sync.SyncError) as excinfo:
        sync.pull(memory_dir)
    assert edited in str(excinfo.value)


def test_pull_refuses_when_the_autostash_restore_conflicts(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """🟡 A PULL THAT EXITS 0 CAN STILL LEAVE CONFLICT MARKERS.

    Autostash restores the stashed edits after the rebase, and that
    restore can collide with what the rebase just brought down. Git then
    prints "Applying autostash resulted in conflicts", leaves `UU` in the
    worktree — and EXITS 0. Verified on git 2.50.1, which is why `pull`
    checks the worktree rather than trusting the return code.

    The stake is the reindex that comes next: on a zero exit `pull` would
    rebuild the FTS5 index from the on-disk files, ingesting
    `<<<<<<< Updated upstream` as memory body text and serving it in
    search results. Refusing leaves the index describing the pre-pull
    files — stale, but true.

    This path only became reachable when the dirty-worktree pre-check
    learned to defer to `rebase.autoStash`; before that, `pull` refused
    before git could ever autostash.

    Mutation-sound: drop the post-pull `_unmerged_paths` check and this
    fails — `pull` returns `pulled=True` on a worktree full of markers.
    """
    _git(memory_dir.parent, "config", "--global", "rebase.autoStash", "true")
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    memory = store.write(content="line one\nline two\nline three", scopes=["tools"])
    sync.push(memory_dir)

    local_path = store._find_path_for_id(memory.id)
    assert local_path is not None

    # The other clone rewrites the SAME line the local edit will touch, so
    # the rebase succeeds and the autostash restore is what conflicts.
    other = tmp_path / "other_clone"
    subprocess.run(
        ["git", "clone", str(bare_remote), str(other)],
        check=True,
        capture_output=True,
    )
    remote_copy = other / local_path.name
    remote_copy.write_text(
        remote_copy.read_text(encoding="utf-8").replace("line two", "from the remote"),
        encoding="utf-8",
    )
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "other host edit")
    _git(other, "push", "origin", "HEAD")

    local_path.write_text(
        local_path.read_text(encoding="utf-8").replace("line two", "from this host"),
        encoding="utf-8",
    )

    with pytest.raises(sync.SyncError) as excinfo:
        sync.pull(memory_dir)
    message = str(excinfo.value)

    # The worktree really is conflicted — the fixture reproduced the state.
    assert sync._unmerged_paths(memory_dir), (
        "the autostash restore did not conflict; the test would pass for the "
        "wrong reason"
    )
    assert local_path.name in message, message
    assert "<<<<<<<" in message, message
    assert "Do NOT run `bettermemory sync push`" in message, message


def _wedge_a_conflicted_rebase(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> Path:
    """Leave a SECOND clone parked mid-rebase after a conflicted pull.

    Divergent edits to the same memory on two clones, pushed from one and
    committed on the other, make `git pull --rebase` stop with `UU` in
    porcelain output and a live `.git/rebase-merge/`. Returns the wedged
    clone."""
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    memory = store.write(content="shared fact", scopes=["tools"])
    sync.push(memory_dir)

    other = tmp_path / "other_clone"
    subprocess.run(
        ["git", "clone", str(bare_remote), str(other)],
        check=True,
        capture_output=True,
    )

    origin_path = store._find_path_for_id(memory.id)
    assert origin_path is not None
    origin_path.write_text(
        origin_path.read_text(encoding="utf-8") + "\nfrom the first host\n",
        encoding="utf-8",
    )
    sync.push(memory_dir)

    clone_path = other / origin_path.name
    clone_path.write_text(
        clone_path.read_text(encoding="utf-8") + "\nfrom the second host\n",
        encoding="utf-8",
    )
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "second host edit")

    with pytest.raises(sync.SyncError):
        sync.pull(other)
    assert sync._rebase_in_progress(other), (
        "fixture did not actually wedge a rebase; the tests below would pass vacuously"
    )
    return other


def test_pull_gives_rebase_advice_when_a_rebase_is_unfinished(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """A conflicted pull leaves the repo mid-rebase, and its `UU` entries are
    INDISTINGUISHABLE from ordinary dirty files in `git status --porcelain`.
    The dirty-worktree pre-check must therefore let the more specific state
    win: telling a mid-rebase user to run `bettermemory sync push` would be
    actively destructive, because push runs `git add -A` and would commit the
    `<<<<<<<` conflict markers into their memories.

    Mutation-sound: drop the `_rebase_in_progress` branch from `pull` and this
    fails — the dirty-worktree message (with its `sync push` advice) is what
    the user gets instead."""
    other = _wedge_a_conflicted_rebase(memory_dir, bare_remote, tmp_path)

    with pytest.raises(sync.SyncError) as excinfo:
        sync.pull(other)
    message = str(excinfo.value)

    assert "rebase" in message.lower(), message
    assert "git rebase --continue" in message, (
        f"the mid-rebase error does not tell the user how to finish: {message}"
    )
    assert "git rebase --abort" in message, (
        f"the mid-rebase error does not offer the escape hatch: {message}"
    )
    assert "Do NOT run `bettermemory sync push`" in message, (
        "the error does not warn against the one command that would commit "
        f"conflict markers into the store: {message}"
    )


def test_autostash_does_not_relax_the_conflict_guard(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """🔴 THE TWO GUARDS ARE DISTINCT AND ONLY ONE OF THEM DEFERS TO CONFIG.

    `rebase.autoStash` earns `pull` the right to skip its DIRTY-WORKTREE
    pre-check, because git will stash and rebase instead of refusing. It
    earns nothing for UNRESOLVED CONFLICTS: a worktree holding `<<<<<<<`
    markers must still be refused, since the advice it needs (resolve, then
    finish the operation) is unchanged and the command it must not be sent
    to — `sync push`, which runs `git add -A` — is unchanged too.

    Mutation-sound: widen the autostash gate to cover
    `_require_no_unresolved_conflict` as well and this fails, because
    `pull` would hand the conflicted worktree to git instead of raising.
    """
    wedged = _wedge_a_conflicted_rebase(memory_dir, bare_remote, tmp_path)
    _git(wedged, "config", "--global", "rebase.autoStash", "true")

    assert sync._rebase_autostash_enabled(wedged) is True, (
        "the fixture did not actually enable autostash; the test would pass "
        "for the wrong reason"
    )
    assert sync._unmerged_paths(wedged), (
        "the fixture left nothing unmerged; the conflict guard would not have "
        "fired either way"
    )

    with pytest.raises(sync.SyncError) as excinfo:
        sync.pull(wedged)
    message = str(excinfo.value)

    assert "<<<<<<<" in message, (
        f"autostash turned the conflict refusal into some other error: {message}"
    )
    assert "Do NOT run `bettermemory sync push`" in message, message
    # Specifically NOT the dirty-worktree message, whose advice ("run sync
    # push") is the destructive one on this state.
    assert "refuses to run against a dirty worktree" not in message, message


def test_auto_refuses_to_commit_conflict_markers_mid_rebase(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """🔴 The sharp edge of committing BEFORE pulling. `auto` now stages and
    commits first, which puts a `git add -A` ahead of `pull`'s own guard —
    so on a repo parked mid-rebase it would commit the conflict markers into
    the user's memories, silently, as if they were resolved content. `auto`
    must refuse instead.

    Mutation-sound: drop the `_rebase_in_progress` guard from
    `_commit_local_changes` and this fails — a commit lands and the memory
    file in the tree contains `<<<<<<<`."""
    other = _wedge_a_conflicted_rebase(memory_dir, bare_remote, tmp_path)
    before = _git(other, "rev-list", "--count", "HEAD").strip()

    with pytest.raises(sync.SyncError, match="unfinished rebase"):
        sync.auto(other)

    after = _git(other, "rev-list", "--count", "HEAD").strip()
    assert before == after, (
        f"auto created a commit on a mid-rebase repo ({before} -> {after})"
    )
    tree = _git(other, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    for name in tree:
        if not name.endswith(".md"):
            continue
        body = _git(other, "show", f"HEAD:{name}")
        assert "<<<<<<<" not in body, (
            f"auto committed conflict markers into {name} — the store now "
            "contains a corrupted memory"
        )


def _assert_no_commit_carries_conflict_markers(repo: Path) -> None:
    """Every `.md` blob in every commit reachable from ANY ref is marker-free.

    `--all`, not `HEAD`, and that distinction is load-bearing: when `auto`
    committed the markers and then hit a conflicted rebase, the failed
    rebase left HEAD DETACHED at the upstream commit, so the corrupt commit
    — sitting at the tip of `main`, and the thing the next push would ship —
    was invisible to a HEAD-only scan. Verified empirically while building
    this test: the HEAD-based version passed against the very commit whose
    `main` tip carried `<<<<<<< HEAD`."""
    for rev in _git(repo, "rev-list", "--all").split():
        for name in _git(repo, "ls-tree", "-r", "--name-only", rev).splitlines():
            if not name.endswith(".md"):
                continue
            body = _git(repo, "show", f"{rev}:{name}")
            assert "<<<<<<<" not in body, (
                f"commit {rev} carries conflict markers in {name} — the "
                "corruption is now permanent in history"
            )


def _wedge_a_conflicted_merge(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> Path:
    """Leave a SECOND clone parked mid-MERGE after a conflicted `git merge`.

    Deliberately NOT a rebase. The porcelain output is indistinguishable
    from the mid-rebase fixture above — same `UU` entry on the same file —
    but no `rebase-merge`/`rebase-apply` directory exists, so a guard that
    probes only for rebase sentinels sees a perfectly ordinary dirty
    worktree. Returns the wedged clone."""
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    memory = store.write(content="shared fact", scopes=["tools"])
    sync.push(memory_dir)

    other = tmp_path / "merge_clone"
    subprocess.run(
        ["git", "clone", str(bare_remote), str(other)],
        check=True,
        capture_output=True,
    )

    origin_path = store._find_path_for_id(memory.id)
    assert origin_path is not None
    origin_path.write_text(
        origin_path.read_text(encoding="utf-8") + "\nfrom the first host\n",
        encoding="utf-8",
    )
    sync.push(memory_dir)

    clone_path = other / origin_path.name
    clone_path.write_text(
        clone_path.read_text(encoding="utf-8") + "\nfrom the second host\n",
        encoding="utf-8",
    )
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "second host edit")

    _git(other, "fetch", "origin")
    # Expected to fail: that failure IS the wedged state under test.
    subprocess.run(
        ["git", "merge", "origin/main"], cwd=other, capture_output=True, check=False
    )

    porcelain = _git(other, "status", "--porcelain")
    assert any(line.startswith("UU") for line in porcelain.splitlines()), (
        f"fixture did not wedge a conflicted merge; porcelain was: {porcelain!r}"
    )
    assert not sync._rebase_in_progress(other), (
        "fixture wedged a REBASE, not a merge — it would not exercise the "
        "gap this test exists to pin"
    )
    return other


def test_auto_refuses_to_commit_conflict_markers_mid_merge(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """🔴 DATA CORRUPTION. The commit-before-pull reordering put a
    `git add -A` ahead of `pull`'s own guard, and that guard asked the wrong
    question: it probed for `rebase-merge`/`rebase-apply` sentinel files. A
    repo left mid-MERGE (`MERGE_HEAD`), mid-cherry-pick, mid-revert, or
    holding a conflicted `git stash pop` (which leaves NO sentinel file at
    all) produces IDENTICAL `UU` porcelain entries and was NOT caught — so
    `auto` staged the `<<<<<<<` markers as if they were resolved content and
    committed them permanently into history, destined for every clone.

    The predicate is the porcelain status code now, not a sentinel-file
    enumeration: any unmerged entry refuses. That subsumes rebase, merge,
    cherry-pick, revert and stash-pop without having to enumerate the
    states, which is the point — the sentinel approach can only see the
    states someone remembered to list.

    Mutation-sound: restore the guard to `_rebase_in_progress` alone and
    this fails — a two-parent merge commit lands at the tip of `main` whose
    memory body contains `<<<<<<< HEAD`, verified empirically against the
    pre-remediation commit."""
    other = _wedge_a_conflicted_merge(memory_dir, bare_remote, tmp_path)
    before = _git(other, "rev-parse", "main").strip()

    with pytest.raises(sync.SyncError) as excinfo:
        sync.auto(other)

    # `main`, not `HEAD`: a conflicted rebase inside `auto`'s pull step
    # detaches HEAD, so the branch tip is where a corrupt commit actually
    # lands and what the next push would ship.
    after = _git(other, "rev-parse", "main").strip()
    assert before == after, (
        f"auto committed onto main with unmerged files present ({before} -> {after})"
    )
    _assert_no_commit_carries_conflict_markers(other)

    message = str(excinfo.value)
    # The message must not send the user to the one command that would
    # `git add -A` the markers in. Pre-remediation this state fell through
    # to the dirty-worktree branch, whose text is "Run `bettermemory sync
    # push` to commit and send them first".
    assert "Run `bettermemory sync push`" not in message, (
        "the unmerged-worktree error recommends `sync push`, which would "
        f"commit the conflict markers it just refused to commit: {message}"
    )
    assert "Do NOT run `bettermemory sync push`" in message, (
        f"the error does not warn against the destructive command: {message}"
    )
    assert "resolve" in message.lower(), (
        f"the error does not tell the user to resolve the conflict: {message}"
    )


def test_pull_refuses_and_advises_resolution_on_a_conflicted_merge(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """The same gap on the `pull` side. Pre-remediation a mid-merge repo
    fell through to the dirty-worktree branch, whose message tells the user
    to "Run `bettermemory sync push`" — the single command that would stage
    the conflict markers. `pull` must instead recognise the unmerged state
    and tell them to resolve the conflict.

    Mutation-sound: restore `pull`'s guard to `_rebase_in_progress` alone
    and this fails on the `sync push` assertion."""
    other = _wedge_a_conflicted_merge(memory_dir, bare_remote, tmp_path)

    with pytest.raises(sync.SyncError) as excinfo:
        sync.pull(other)
    message = str(excinfo.value)

    assert "conflict" in message.lower(), message
    assert "Run `bettermemory sync push`" not in message, (
        f"the unmerged-worktree error still recommends `sync push`: {message}"
    )
    # …and it must not hand out `git rebase --continue`, which simply
    # errors in a repo where no rebase is in progress.
    assert "git rebase --continue" not in message, (
        f"a mid-MERGE repo was given inapplicable rebase advice: {message}"
    )


def test_push_refuses_to_commit_or_ship_conflict_markers(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """🔴 DATA CORRUPTION, and the distributing one. `_stage_and_commit`
    holds the package's only `git add -A`, and of its two callers `push`
    was the one that reached it without a conflict guard, so on a repo
    holding unmerged files it staged the `<<<<<<<` markers as resolved
    content, committed them, and then SENT them to the remote. Verified
    empirically against the pre-remediation commit: `sync.push` returned
    `{'committed': True, 'pushed': True}` and left `<<<<<<< HEAD` in a
    memory body at the tip of `main` on both the clone and the bare remote.

    Worse than the `auto` variant this shares a fixture with: that one
    corrupted local history the user could still `git reset` away, while
    this one hands the markers to every clone that pulls.

    Asserts on the BARE REMOTE as well as the clone, because "did not
    commit" and "did not push" are separate failures — a guard placed after
    the commit would still fix only the first.

    Mutation-sound: drop `_require_no_unresolved_conflict` from `push` and
    this fails on the very first assertion, with `sync.push` returning
    rather than raising."""
    other = _wedge_a_conflicted_merge(memory_dir, bare_remote, tmp_path)
    before_local = _git(other, "rev-parse", "main").strip()
    before_remote = _git(bare_remote, "rev-parse", "main").strip()

    with pytest.raises(sync.SyncError) as excinfo:
        sync.push(other)

    # `main`, not `HEAD`: the sibling `auto` test documents that a failed
    # rebase can leave HEAD detached while the corrupt commit sits on the
    # branch tip, so the branch ref is the one that has to be pinned.
    assert _git(other, "rev-parse", "main").strip() == before_local, (
        "push committed onto main with unmerged files present"
    )
    assert _git(bare_remote, "rev-parse", "main").strip() == before_remote, (
        "push SHIPPED a commit to the remote with unmerged files present — "
        "every clone that pulls now gets the conflict markers"
    )
    # Both sides, all refs: the corruption is only contained if neither
    # repo can hand a marker-bearing blob to anyone.
    _assert_no_commit_carries_conflict_markers(other)
    _assert_no_commit_carries_conflict_markers(bare_remote)

    message = str(excinfo.value)
    # The user is already running `sync push`; telling them to run it is
    # both circular and destructive. That text is what the dirty-worktree
    # branch used to emit on this state.
    assert "Run `bettermemory sync push`" not in message, (
        f"push's own refusal tells the user to run `sync push`: {message}"
    )
    assert "resolve" in message.lower(), (
        f"the error does not tell the user to resolve the conflict: {message}"
    )


def test_push_refuses_conflict_markers_staged_without_porcelain_state(
    memory_dir: Path, bare_remote: Path
) -> None:
    """🔴 The residual-TOCTOU close: marker CONTENT with no conflict STATE.

    Every earlier guard gates on porcelain (`UU` entries), which
    describes how a conflict was CREATED. The window none of them can
    cover is content that arrives without that state — a half-resolved
    file left by the user's editor after a hand-run resolution, or a
    conflict landing between a caller's predicate and the `git add -A`.
    Simulated here at rest: a worktree with NO unmerged entries whose
    file bytes nonetheless carry a full marker triad. Every porcelain
    guard passes; only the staged-index scan in `_stage_and_commit`
    stands between these bytes and the remote.

    Mutation-sound: remove the index scan and `push` returns
    `{'committed': True, 'pushed': True}`, failing the first assertion.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    memory = store.write(content="clean baseline", scopes=["tools"])
    sync.push(memory_dir)
    before_local = _git(memory_dir, "rev-parse", "main").strip()
    before_remote = _git(bare_remote, "rev-parse", "main").strip()

    path = store._find_path_for_id(memory.id)
    assert path is not None
    path.write_text(
        path.read_text(encoding="utf-8")
        + "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n",
        encoding="utf-8",
    )
    assert "UU" not in _git(memory_dir, "status", "--porcelain"), (
        "fixture accidentally created porcelain conflict state — this "
        "test exists to cover the NO-state window"
    )

    with pytest.raises(sync.SyncError) as excinfo:
        sync.push(memory_dir)

    assert _git(memory_dir, "rev-parse", "main").strip() == before_local, (
        "push committed marker content that carried no porcelain state"
    )
    assert _git(bare_remote, "rev-parse", "main").strip() == before_remote, (
        "push SHIPPED marker content to the remote — every clone that pulls now gets it"
    )
    _assert_no_commit_carries_conflict_markers(memory_dir)
    _assert_no_commit_carries_conflict_markers(bare_remote)

    message = str(excinfo.value)
    assert "resolve" in message.lower(), (
        f"the refusal does not tell the user to resolve: {message}"
    )
    assert path.name in message, (
        f"the refusal does not name the offending file: {message}"
    )


def test_auto_refuses_conflict_markers_staged_without_porcelain_state(
    memory_dir: Path, bare_remote: Path
) -> None:
    """`auto` reaches the same staged-index scan through its commit-first
    ordering — `_commit_local_changes` -> `_stage_and_commit` — so the
    no-porcelain-state window is closed on both public entry points, not
    just `push`. Same shape as the push twin; asserts the local branch
    tip because a refused `auto` never reaches its pull or push steps.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    memory = store.write(content="clean baseline", scopes=["tools"])
    sync.push(memory_dir)
    before_local = _git(memory_dir, "rev-parse", "main").strip()

    path = store._find_path_for_id(memory.id)
    assert path is not None
    path.write_text(
        path.read_text(encoding="utf-8")
        + "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n",
        encoding="utf-8",
    )

    with pytest.raises(sync.SyncError):
        sync.auto(memory_dir)

    assert _git(memory_dir, "rev-parse", "main").strip() == before_local, (
        "auto committed marker content that carried no porcelain state"
    )
    _assert_no_commit_carries_conflict_markers(memory_dir)


def test_marker_scan_ignores_setext_underlines_and_indented_quotes(
    memory_dir: Path, bare_remote: Path
) -> None:
    """The scan's precision half: prose that only LOOKS marker-adjacent
    ships.

    Two legitimate shapes in real markdown memory bodies: a setext-style
    H1 underline (seven-plus `=` at column 0 — the reason bare `=======`
    is deliberately absent from `_STAGED_MARKER_PATTERN`), and a QUOTED
    conflict example indented by one space (the workaround the refusal
    message itself offers). A guard that bounced these would block
    honest memories about git conflicts — this package's own operator
    store carries exactly such bodies.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    store.write(
        content=(
            "Conflict notes\n=======\n\nHow a marker looks, quoted:\n"
            " <<<<<<< HEAD\n =======\n >>>>>>> theirs\n"
        ),
        scopes=["tools"],
    )

    result = sync.push(memory_dir)

    assert result["pushed"] is True, (
        "the marker scan bounced legitimate markdown (setext underline "
        "or an indented quoted marker)"
    )


def test_push_refuses_when_the_staged_marker_scan_itself_fails(
    memory_dir: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🟡 The scan's FAIL-CLOSED branch, which shipped with no test at all.

    `_stage_and_commit` treats `git grep` exit >= 2 as "the scan did not
    run" and refuses, because an unverified snapshot must not be
    committed. Nothing exercised that branch, so a refactor that flipped
    it open — reading any non-zero code as "clean", say — would have kept
    the suite green while silently retiring the guard on the ship path.
    That is the guard-decay pattern this module's history is full of.

    Monkeypatches `_run_git` in the shape the credential-redaction tests
    already use: intercept one subcommand, hand back a synthetic
    `CompletedProcess`, delegate everything else to the real thing. Asserts
    the refusal AND that neither the local branch nor the remote moved —
    "raised an error" and "committed nothing" are separate properties, and
    a guard placed after the commit would satisfy only the first.

    Mutation-sound: relax the check to `if scan.returncode == 0` and this
    fails on the first assertion, with `sync.push` returning a committed,
    pushed result built from a snapshot nothing ever scanned.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="clean baseline", scopes=["tools"])
    sync.push(memory_dir)
    before_local = _git(memory_dir, "rev-parse", "main").strip()
    before_remote = _git(bare_remote, "rev-parse", "main").strip()

    # Something must actually be staged, or `_stage_and_commit` returns
    # before it ever reaches the scan and the test proves nothing.
    Store(memory_dir).write(content="a second clean fact", scopes=["tools"])

    original_run_git = sync._run_git
    scan_attempted = False

    def fake_run_git(
        root: Path, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        nonlocal scan_attempted
        if args and args[0] == "grep":
            scan_attempted = True
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=2,
                stdout="",
                stderr="fatal: unable to read tree object",
            )
        return original_run_git(root, args, check=check)

    monkeypatch.setattr(sync, "_run_git", fake_run_git)

    with pytest.raises(sync.SyncError) as excinfo:
        sync.push(memory_dir)

    assert scan_attempted, (
        "`push` never ran the marker scan — this test no longer covers the "
        "branch it was written for"
    )

    message = str(excinfo.value)
    assert "scan failed" in message, (
        f"the refusal does not say the scan itself failed: {message}"
    )
    assert "git grep exited 2" in message, (
        f"the refusal does not surface the scanner's exit code: {message}"
    )

    # `_git` shells out directly, so the monkeypatch above cannot mask a
    # commit that really happened.
    assert _git(memory_dir, "rev-parse", "main").strip() == before_local, (
        "push committed on top of a snapshot the conflict scan never verified"
    )
    assert _git(bare_remote, "rev-parse", "main").strip() == before_remote, (
        "push SHIPPED an unverified snapshot to the remote"
    )


def test_push_commits_the_snapshot_the_scan_approved(
    memory_dir: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🟡 CHECK-THEN-ACT on the shared index, closed by a tree snapshot.

    The scan used to read the index (`git grep --cached`) and the commit
    used to read it AGAIN (`git commit`). Two reads of shared mutable
    state with a gap between them: marker content staged by anything else
    on the machine in that gap — a user's hand-run `git add` mid-sync, an
    editor plugin — was committed having never been scanned, which is a
    slice of the very race the scan was added to close.

    The window is reproduced exactly rather than approximated: the fake
    `_run_git` lets the real scan run, then, at the instant it returns,
    appends a marker triad to a tracked memory and `git add`s it. Under
    the old shape the following `git commit` picked that up. Under the
    snapshot shape the commit is built from the tree object that was
    scanned, so the injected bytes cannot reach it.

    The `git diff --cached` assertion is what keeps this test honest: it
    proves the index really was mutated in the gap, so a green result
    means "the commit ignored the mutation", not "the mutation never
    happened". Those leftover bytes staying staged IS the new invariant —
    they are not silently dropped, they simply belong to a later commit,
    which will scan them on its own terms.

    Mutation-sound: restore `git grep --cached` + `_run_git(["commit", ...])`
    and this fails in `_assert_no_commit_carries_conflict_markers`, with
    `<<<<<<< HEAD` in a memory body at the tip of `main` and on the remote.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    memory = store.write(content="clean baseline", scopes=["tools"])
    sync.push(memory_dir)

    store.write(content="a second clean fact", scopes=["tools"])
    path = store._find_path_for_id(memory.id)
    assert path is not None

    original_run_git = sync._run_git
    staged_in_the_gap = False

    def fake_run_git(
        root: Path, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        nonlocal staged_in_the_gap
        result = original_run_git(root, args, check=check)
        if args and args[0] == "grep" and not staged_in_the_gap:
            staged_in_the_gap = True
            # THE WINDOW: between the scan and the commit, somebody else's
            # git stages conflict content into the shared index.
            path.write_text(
                path.read_text(encoding="utf-8")
                + "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n",
                encoding="utf-8",
            )
            original_run_git(root, ["add", "-A"])
        return result

    monkeypatch.setattr(sync, "_run_git", fake_run_git)

    result = sync.push(memory_dir)

    assert staged_in_the_gap, (
        "the marker scan never ran, so nothing was injected — this test no "
        "longer covers the window it was written for"
    )
    assert result["committed"] is True, (
        "the clean change was not committed at all; the test needs a real "
        "commit for the snapshot assertion to mean anything"
    )
    # The corruption check FIRST, so a regression reports the thing that
    # actually went wrong. If the commit swallowed the injected bytes,
    # both this and the staging check below fail, and only this one names
    # the reason.
    _assert_no_commit_carries_conflict_markers(memory_dir)
    _assert_no_commit_carries_conflict_markers(bare_remote)
    # Anti-vacuity guard, reached only once history is known clean: the
    # injected bytes are still STAGED (index vs HEAD) after the push
    # returned, which proves the index really did change inside the
    # window rather than the injection having quietly missed.
    assert "<<<<<<<" in _git(memory_dir, "diff", "--cached"), (
        "the injected content never reached the index — the check-then-act "
        "window was not actually exercised"
    )


def _arm_a_commit_that_lands_mid_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    """Advance the branch by hand in the instant between `commit-tree` and
    `update-ref`, and report the sha that landed there.

    That instant is the whole window the compare-and-swap in
    `_commit_snapshot_tree` exists to close: the parent has already been
    read and baked into the new commit object, and the ref has not been
    written yet. The fake wraps `sync._run_git`, so every caller inside
    `sync.py` sees it.

    Returns the dict the fake records into. It holds `"tip"` — the
    interloper's sha — only if `commit-tree` actually ran, so a caller that
    asserts on the key cannot pass vacuously against a `push` that refused
    earlier for some unrelated reason.
    """
    original_run_git = sync._run_git
    interloper: dict[str, str] = {}

    def fake_run_git(
        root: Path, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = original_run_git(root, args, check=check)
        if args and args[0] == "commit-tree" and "tip" not in interloper:
            # THE WINDOW: a hand-run commit advances the branch after the
            # sync read its parent.
            (root / "hand-written.md").write_text("by hand\n", encoding="utf-8")
            original_run_git(root, ["add", "--", "hand-written.md"])
            original_run_git(root, ["commit", "-m", "hand-run commit"])
            interloper["tip"] = original_run_git(
                root, ["rev-parse", "HEAD"]
            ).stdout.strip()
        return result

    monkeypatch.setattr(sync, "_run_git", fake_run_git)
    return interloper


def test_push_refuses_rather_than_orphaning_a_commit_that_lands_mid_sync(
    memory_dir: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compare-and-swap the plumbing commit path depends on.

    `git commit-tree` bakes the parent into the commit object, so the
    parent has to be read before the ref is written. If someone else's
    commit lands on the branch in between and the ref write is
    unconditional, that commit stops being reachable — the sync's own
    commit points past it. Plain `git commit` never had this exposure;
    the move to plumbing created it, and `git update-ref`'s optional
    old-value argument is what closes it.

    The window is reproduced at the one instant that distinguishes the
    two: the fake fires when `commit-tree` returns, which is after the
    parent was read and before `update-ref` runs.

    Mutation-sound: drop the old-value argument from the `update-ref`
    call and this fails on the final assertion, with the hand-run commit
    unreachable from any ref.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    store.write(content="clean baseline", scopes=["tools"])
    sync.push(memory_dir)
    store.write(content="a second clean fact", scopes=["tools"])

    interloper = _arm_a_commit_that_lands_mid_sync(monkeypatch)

    with pytest.raises(sync.SyncError):
        sync.push(memory_dir)

    assert "tip" in interloper, (
        "`commit-tree` never ran — this test no longer covers the window it "
        "was written for"
    )
    assert _git(memory_dir, "rev-parse", "main").strip() == interloper["tip"], (
        "the sync overwrote a commit that landed mid-sync; that commit is "
        "now unreachable from any branch"
    )


def test_push_refuses_rather_than_reverting_a_commit_that_lands_on_the_snapshot(
    memory_dir: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🟡 The OTHER half of the mid-sync window, which the CAS used to miss.

    Its sibling above covers a commit landing after the parent was read:
    the ref write then names a stale old value and git refuses. This one
    covers the earlier gap — a commit landing between the SNAPSHOT and the
    parent read — where the failure mode is different and quieter. By the
    time the parent was read it WAS the interloper's tip, so the CAS was
    satisfied; but the tree being committed had been frozen before that
    commit existed, so the sync's commit deleted its files at the branch
    tip while leaving the commit itself reachable. Measured against the
    pre-fix base: `sync.push` returned `committed=True, pushed=True`,
    `hand-written.md` was absent from `main`'s tree, and only the worktree
    copy survived.

    Reading the parent immediately BEFORE `git write-tree` folds this gap
    into the same compare-and-swap, so it is a refusal like the other half.

    Mutation-sound: move the `rev-parse --verify HEAD` back below
    `write-tree` and this fails on the tree assertion, with `sync.push`
    returning instead of raising.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    store.write(content="clean baseline", scopes=["tools"])
    sync.push(memory_dir)
    store.write(content="a second clean fact", scopes=["tools"])

    original_run_git = sync._run_git
    interloper: dict[str, str] = {}

    def fake_run_git(
        root: Path, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = original_run_git(root, args, check=check)
        if args and args[0] == "write-tree" and "tip" not in interloper:
            # THE WINDOW: the snapshot is frozen; the parent has NOT been
            # read yet. A hand-run commit advances the branch here.
            (root / "hand-written.md").write_text("by hand\n", encoding="utf-8")
            original_run_git(root, ["add", "--", "hand-written.md"])
            original_run_git(root, ["commit", "-m", "hand-run commit"])
            interloper["tip"] = original_run_git(
                root, ["rev-parse", "HEAD"]
            ).stdout.strip()
        return result

    monkeypatch.setattr(sync, "_run_git", fake_run_git)

    with pytest.raises(sync.SyncError):
        sync.push(memory_dir)

    assert "tip" in interloper, (
        "`write-tree` never ran — this test no longer covers the window it "
        "was written for"
    )
    assert _git(memory_dir, "rev-parse", "main").strip() == interloper["tip"], (
        "the sync moved the branch past a commit that landed on its snapshot"
    )
    assert "hand-written.md" in _git(memory_dir, "ls-tree", "--name-only", "main"), (
        "a commit that landed mid-sync stayed reachable but its file was "
        "deleted from the branch tip by a tree snapshotted before it existed"
    )


_BYSTANDER_EDIT = "\nan edit that was never committed\n"


def _wedge_a_resolved_but_unconcluded_merge(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> tuple[Path, str]:
    """Leave a clone mid-merge with every conflict already RESOLVED, beside
    an uncommitted edit to an unrelated memory.

    The state `_require_no_sequencer_state` exists for, and the reason it
    could not be folded into the porcelain conflict guard: resolving and
    `git add`ing the conflicted files clears every `UU` code, and a merge
    leaves no rebase sentinel, so `_unmerged_paths` and
    `_rebase_in_progress` both come back empty. Only `MERGE_HEAD` still
    marks it.

    THE BYSTANDER IS NOT DECORATION. A refusal is only harmless if it
    changes nothing, and the two halves of "nothing" are visible in
    different columns of `git status --porcelain`: the resolved conflict
    sits in the STAGED column (`M `) and the bystander in the UNSTAGED one
    (` M`). A `git add -A` collapses the second into the first, and that
    collapse is exactly what makes the `git merge --abort` this refusal
    recommends destroy content — `reset --merge` keeps an
    index-vs-worktree delta and has nothing to keep once staging erased it.
    A fixture with only the conflicted file could not see that, because its
    entry reads `M ` either way.

    Written into `memory_dir` BEFORE `_wedge_a_conflicted_merge` runs, so
    the shared helper's own `sync.push` carries it into the bare remote and
    the clone inherits it — no change to that helper's signature, which
    four other tests depend on.

    Returns `(wedged clone, bystander filename)`.
    """
    bystander = Store(memory_dir).write(
        content="an unrelated memory the sync must not touch", scopes=["tools"]
    )
    bystander_path = Store(memory_dir)._find_path_for_id(bystander.id)
    assert bystander_path is not None

    other = _wedge_a_conflicted_merge(memory_dir, bare_remote, tmp_path)

    for line in _git(other, "status", "--porcelain").splitlines():
        if line.startswith("UU"):
            name = line[3:]
            (other / name).write_text("resolved by hand\n", encoding="utf-8")
            _git(other, "add", "--", name)

    clone_bystander = other / bystander_path.name
    assert clone_bystander.exists(), (
        "the bystander memory never reached the clone — the fixture's "
        "write-before-wedge ordering has drifted"
    )
    clone_bystander.write_text(
        clone_bystander.read_text(encoding="utf-8") + _BYSTANDER_EDIT,
        encoding="utf-8",
    )

    porcelain = _git(other, "status", "--porcelain").splitlines()
    assert not any(line.startswith("UU") for line in porcelain), (
        f"fixture did not resolve the conflict; porcelain: {porcelain!r}"
    )
    assert f" M {bystander_path.name}" in porcelain, (
        "the bystander edit is not an UNSTAGED tracked modification, so it "
        f"cannot show a staging side effect; porcelain: {porcelain!r}"
    )
    assert sync._git_path_exists(other, "MERGE_HEAD"), (
        "fixture left no MERGE_HEAD — the state under test is a RESOLVED "
        "but unconcluded merge"
    )
    return other, bystander_path.name


def _assert_the_merge_refusal_changed_nothing(
    other: Path, bystander: str, before_porcelain: str, before_tip: str
) -> None:
    """Every claim `_require_no_sequencer_state`'s message makes, checked.

    Three separate properties, and the first two are what the guard's old
    placement broke:

    * `git status --porcelain` is BYTE-IDENTICAL across the refusal. Not
      "no commit was made" — the refusal ran behind `_reconcile_gitignore`
      and `git add -A`, so it left `main` where it was while rewriting the
      index underneath it, which is precisely why the existing HEAD
      assertion could not see the defect.
    * `git merge --abort` — the recommended way out — still returns the
      bystander's uncommitted bytes. With the edit staged those bytes are
      unrecoverable: never committed, so no reflog entry and no dangling
      object holds them.
    * `main` did not move, the original claim, kept.
    """
    assert _git(other, "status", "--porcelain") == before_porcelain, (
        "the refusal mutated the index before refusing; `git merge --abort` "
        "will now discard uncommitted work that it would otherwise keep"
    )
    assert _git(other, "rev-parse", "main").strip() == before_tip, (
        "the sync wrote a single-parent commit over an unconcluded merge, "
        "dropping the merged branch from history"
    )
    _git(other, "merge", "--abort")
    assert _BYSTANDER_EDIT in (other / bystander).read_text(encoding="utf-8"), (
        "`git merge --abort`, which this refusal recommends, destroyed an "
        "uncommitted memory edit that survives the same abort on a store no "
        "sync ever touched"
    )


def test_push_refuses_to_conclude_a_half_finished_merge(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """The one semantic `commit-tree` cannot reproduce, refused explicitly.

    A merge whose conflicts the user RESOLVED and `git add`ed carries no
    `UU` codes and no rebase sentinel, so every conflict guard in this
    module passes — but `MERGE_HEAD` is still there. A plain `git commit`
    would conclude it as a two-parent merge commit and clear the state;
    `git commit-tree -p HEAD` writes a ONE-parent commit and leaves the
    sentinel, which loses the merged branch from history and parks the
    store mid-merge. Verified on git 2.50.1 in both directions.

    So the plumbing path refuses instead of approximating, and points at
    the command that does it properly. That is a deliberate behaviour
    change: before the snapshot rework, `sync push` would silently
    conclude this merge with a "bettermemory: sync" message.

    🔴 DATA LOSS on the ORDERING, which is why this test grew an index
    assertion. The refusal shipped inside `_commit_snapshot_tree`, behind
    `_reconcile_gitignore` and the `git add -A` — so it staged the whole
    worktree and only then declined, and the `git merge --abort` it
    recommends then reset the swept-in edit to its committed content.
    Measured against the pre-fix commit on this exact fixture: porcelain
    went `[" M bystander", "M  conflicted"]` -> `["M  bystander", "M
    conflicted"]` and the uncommitted bytes were gone after the abort,
    while the identical fixture with no sync run kept them.

    Mutation-sound in both directions: move either refusal back into
    `_commit_snapshot_tree` and the porcelain assertion fails; drop
    `_require_no_sequencer_state` entirely and the branch-tip assertion
    fails.
    """
    other, bystander = _wedge_a_resolved_but_unconcluded_merge(
        memory_dir, bare_remote, tmp_path
    )
    before_porcelain = _git(other, "status", "--porcelain")
    before_tip = _git(other, "rev-parse", "main").strip()

    with pytest.raises(sync.SyncError) as excinfo:
        sync.push(other)

    message = str(excinfo.value)
    assert "MERGE_HEAD" in message, (
        f"the refusal does not name the state that blocked it: {message}"
    )
    assert "git commit" in message, (
        f"the refusal does not point at the command that concludes it: {message}"
    )
    _assert_the_merge_refusal_changed_nothing(
        other, bystander, before_porcelain, before_tip
    )


def test_auto_refuses_to_conclude_a_half_finished_merge(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """The same refusal on the UNATTENDED path, index integrity included.

    `auto` is the cron / shell-alias one-shot, so it is the route on which
    a destructive side effect goes unwatched the longest — and it reaches
    the staging choke point through a different door
    (`_commit_local_changes` -> `_stage_and_commit`) than `push` does. The
    ordering defect reproduced identically through both, so both are
    pinned; a guard hoisted on one path only would leave the cron half
    still mutating the user's index before it declined.
    """
    other, bystander = _wedge_a_resolved_but_unconcluded_merge(
        memory_dir, bare_remote, tmp_path
    )
    before_porcelain = _git(other, "status", "--porcelain")
    before_tip = _git(other, "rev-parse", "main").strip()

    with pytest.raises(sync.SyncError) as excinfo:
        sync.auto(other)

    assert "MERGE_HEAD" in str(excinfo.value), (
        f"`auto` refused for some other reason: {excinfo.value}"
    )
    _assert_the_merge_refusal_changed_nothing(
        other, bystander, before_porcelain, before_tip
    )


def test_push_keeps_signing_commits_when_commit_gpgsign_is_set(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """`commit.gpgSign` survives the move off porcelain `git commit`.

    `git commit` honours the config; `git commit-tree` does not (verified
    on git 2.50.1 — it returns an unsigned commit where `git commit`
    fails), so the plumbing path passes `-S` itself. This half pins the
    FAILURE mode: point `gpg.program` at a binary that does not exist and
    a signing attempt must turn into a `SyncError` with nothing committed.
    Drop the `-S` and the push instead succeeds, quietly recording an
    UNSIGNED commit in a store whose owner asked for signatures — which is
    the regression this pins.

    It is deliberately only half. "Aborts when signing fails" and "always
    aborts when signing is on" are the same observation from here — both
    leave HEAD where it was — so the positive direction needs a real key
    and a real signature, which is
    `test_push_signs_exactly_when_the_store_asked_for_it` below.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="clean baseline", scopes=["tools"])
    sync.push(memory_dir)
    before = _git(memory_dir, "rev-parse", "main").strip()

    _git(memory_dir, "config", "commit.gpgSign", "true")
    _git(memory_dir, "config", "gpg.program", str(tmp_path / "no-such-gpg-binary"))
    Store(memory_dir).write(content="a fact that must be signed", scopes=["tools"])

    with pytest.raises(sync.SyncError):
        sync.push(memory_dir)

    assert _git(memory_dir, "rev-parse", "main").strip() == before, (
        "push recorded a commit although signing was configured and failed"
    )


def _configure_ssh_signing(repo: Path, keydir: Path) -> bool:
    """Point `repo` at a throwaway ssh signing key, or report it can't be.

    SSH rather than OpenPGP because it needs no keyring daemon and no
    agent socket: `ssh-keygen` writes a key pair, and an
    `gpg.ssh.allowedSignersFile` naming the committer is all `%G?` needs to
    report a VERIFIED signature. That keeps the test hermetic on every
    platform in the matrix.

    Returns False when `ssh-keygen` is missing or refuses, so the caller
    skips rather than asserting against an environment that cannot sign at
    all. `commit.gpgSign` is deliberately NOT set here — the caller turns
    signing on at the point in the story where it wants it.
    """
    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        return False
    keydir.mkdir(parents=True, exist_ok=True)
    key = keydir / "signing_key"
    generated = subprocess.run(
        [keygen, "-q", "-t", "ed25519", "-N", "", "-C", "bettermemory-sync-test"]
        + ["-f", str(key)],
        capture_output=True,
        text=True,
        check=False,
    )
    if generated.returncode != 0:
        return False
    public = key.with_name(key.name + ".pub")
    fields = public.read_text(encoding="utf-8").split()
    # The principal has to match the committer, which the `memory_dir`
    # fixture configures. Read it back rather than repeating the literal,
    # so the two cannot drift apart.
    email = _git(repo, "config", "user.email").strip()
    allowed = keydir / "allowed_signers"
    allowed.write_text(f"{email} {fields[0]} {fields[1]}\n", encoding="utf-8")
    _git(repo, "config", "gpg.format", "ssh")
    _git(repo, "config", "user.signingkey", public.as_posix())
    _git(repo, "config", "gpg.ssh.allowedSignersFile", allowed.as_posix())
    return True


def _head_signature(repo: Path) -> str:
    """`git log`'s `%G?` for HEAD: `G` verified, `N` unsigned, others bad."""
    return _git(repo, "log", "-1", "--pretty=%G?").strip()


def test_push_signs_exactly_when_the_store_asked_for_it(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """The commit `sync push` produces carries a REAL signature.

    The security property the move off porcelain `git commit` had to carry
    over. `git commit` honours `commit.gpgSign`, `git commit-tree` ignores
    it, and a store whose owner requires signed commits getting an
    unsigned stream instead is a trust regression, not a documentation
    item. Its sibling above observes only the failure mode, which cannot
    distinguish a working signing path from one that always errors — so
    this one configures a real key and reads the signature back.

    Both directions in one store, in the order the regression would
    appear: an unconfigured store must NOT sign (an unconditional `-S`
    would break every store without a key), then `commit.gpgSign=true`
    must produce a signature that verifies.

    Two skips, both anti-vacuity guards rather than escapes: the store
    must actually start out unsigned (otherwise something outside this
    test is signing and neither half means anything), and porcelain must
    be able to sign in this environment (ssh signing needs git >= 2.34 and
    an `ssh-keygen` git itself can reach). Past those, the assertions are
    unconditional.

    Mutation-sound, verified by reverting the code under it: drop the `-S`
    from `_commit_snapshot_tree` and the signed half fails reporting `N`;
    pass `-S` unconditionally and the unsigned half fails.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)

    store.write(content="a fact committed without signing", scopes=["tools"])
    assert sync.push(memory_dir)["committed"] is True
    unsigned_state = _head_signature(memory_dir)
    if unsigned_state != "N":
        pytest.skip(
            f"a store with no signing config produced {unsigned_state!r}, not "
            "'N' — something outside this test is signing commits, so "
            "neither direction can be measured here"
        )

    if not _configure_ssh_signing(memory_dir, tmp_path / "signing"):
        pytest.skip("ssh-keygen unavailable — cannot build a signing key")
    _git(memory_dir, "config", "commit.gpgSign", "true")

    # Control: prove PORCELAIN signs here before demanding it of the sync
    # path, so a git that cannot do ssh signing at all skips instead of
    # reporting a regression this package did not cause.
    store.write(content="a control fact committed by porcelain", scopes=["tools"])
    _git(memory_dir, "add", "-A")
    _git(memory_dir, "commit", "-m", "porcelain control")
    control_state = _head_signature(memory_dir)
    if control_state != "G":
        pytest.skip(
            f"porcelain `git commit` reported {control_state!r} rather than "
            "'G' — this git cannot produce a verifiable ssh signature"
        )

    store.write(content="a fact that must be signed", scopes=["tools"])
    assert sync.push(memory_dir)["committed"] is True
    assert _head_signature(memory_dir) == "G", (
        "the commit `sync push` created carries no verifiable signature "
        "although commit.gpgSign is set — the sync path silently downgraded "
        "a store whose owner requires signed commits"
    )


def test_push_refuses_a_commit_message_that_is_only_whitespace(
    memory_dir: Path, bare_remote: Path
) -> None:
    """Porcelain parity on the empty-message edge, refused before staging.

    `git commit -m "   "` aborts ("empty commit message"); `git commit-tree
    -m "   "` happily records one. `_cleanup_commit_message` reproduces
    git's `--cleanup=whitespace` byte-for-byte on every non-empty message,
    but cleanup alone cannot reproduce a REFUSAL, so the empty result is
    rejected explicitly by `_require_commit_message`. This pins the
    behaviour the porcelain path had, on the path that replaced it.

    🔴 And it pins WHEN. The check shipped inside `_commit_snapshot_tree`,
    downstream of `_reconcile_gitignore` and `git add -A`, so a blank
    `--message` — a typo, on a command that does nothing — rewrote the
    store's `.gitignore` and staged the entire worktree before declining.
    Both are asserted byte-for-byte here, and the stale `.gitignore` in
    this fixture is what gives the reconcile something to change: without
    it the file is already canonical and the assertion passes vacuously.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="clean baseline", scopes=["tools"])
    gitignore = memory_dir / ".gitignore"
    # `*.lock` is kept deliberately: `flock_excl` creates `.sync.lock` on
    # every sync op, and an unignored lockfile would show up as an
    # untracked porcelain entry — a side effect of taking the lock, not of
    # staging, which would make the assertion below fail for the wrong
    # reason. Everything else is left stale so the reconcile has work.
    gitignore.write_text("# hand-written\n.index.sqlite\n*.lock\n", encoding="utf-8")
    assert sync._missing_gitignore_patterns(gitignore.read_text(encoding="utf-8")), (
        "the fixture's .gitignore is already canonical, so a reconcile "
        "running before the refusal would leave no trace"
    )
    before_porcelain = _git(memory_dir, "status", "--porcelain")
    before_gitignore = gitignore.read_text(encoding="utf-8")

    with pytest.raises(sync.SyncError, match="empty"):
        sync.push(memory_dir, message="   \n\n  ")

    assert not _git(memory_dir, "rev-list", "--all").strip(), (
        "a commit was recorded despite the blank message"
    )
    assert _git(memory_dir, "status", "--porcelain") == before_porcelain, (
        "a blank --message staged the worktree before refusing"
    )
    assert gitignore.read_text(encoding="utf-8") == before_gitignore, (
        "a blank --message rewrote the store's .gitignore before refusing"
    )


def test_commit_message_cleanup_is_idempotent() -> None:
    """The property that makes `_commit_snapshot_tree`'s re-check free.

    `_stage_and_commit` validates the message up front — before anything on
    disk moves — and passes the CLEANED body down;
    `_commit_snapshot_tree` runs `_require_commit_message` again on what it
    receives, as a last statement before the write. That second call is
    only harmless if cleaning a cleaned body is a no-op: if it were not,
    the commit would carry a different message than the one that was
    validated, and the two call sites would silently disagree.

    Covers each rule `_cleanup_commit_message` applies (trailing
    whitespace, blank-line runs, leading and trailing blanks) plus a
    non-ASCII body, since `rstrip()` is unicode-aware.
    """
    for raw in (
        "subject",
        "  padded subject  \n\n\n  body with trailing space   \n\n",
        "\n\nsubject after blanks\n\n\ttab-indented body\t\n",
        "sujet accentué — é\n\n\ncorps \n",
        "a\nb\nc",
    ):
        once = sync._cleanup_commit_message(raw)
        assert sync._cleanup_commit_message(once) == once, (
            f"cleanup is not idempotent for {raw!r}: {once!r} -> "
            f"{sync._cleanup_commit_message(once)!r}"
        )


def _arm_a_conflicting_stash(memory_dir: Path, bare_remote: Path) -> None:
    """Leave `memory_dir` clean, synced, and holding a stash that will
    CONFLICT the moment it is popped.

    Chosen over `_wedge_a_conflicted_merge` for the TOCTOU test below
    because this shape leaves the repo with NO divergence from the remote:
    the conflict is purely local, so `auto`'s follow-on `pull --rebase` has
    nothing to replay and cannot fail. That is what lets the corrupt run
    finish silently instead of tripping over a later step. A conflicted
    `git stash pop` also writes no `MERGE_HEAD` and no `rebase-merge/`
    directory, so nothing but the porcelain `UU` code marks the state.

    On return: `main` == `origin/main`, worktree clean, one stash entry
    whose recorded change overlaps the committed one.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    memory = store.write(content="shared fact", scopes=["tools"])
    sync.push(memory_dir)

    path = store._find_path_for_id(memory.id)
    assert path is not None
    base = path.read_text(encoding="utf-8")

    # Change A goes into the stash; the worktree reverts to `base`.
    path.write_text(base + "\nfrom the stashed edit\n", encoding="utf-8")
    _git(memory_dir, "stash", "push", "--", path.name)

    # Change B is committed and pushed at the same spot, so the remote
    # stays in sync while the stash becomes unappliable.
    path.write_text(base + "\nfrom the committed edit\n", encoding="utf-8")
    sync.push(memory_dir)

    assert not _git(memory_dir, "status", "--porcelain").strip(), (
        "fixture left the worktree dirty; the test needs a clean starting state"
    )
    assert (
        _git(memory_dir, "rev-parse", "main").strip()
        == _git(bare_remote, "rev-parse", "main").strip()
    ), "fixture left the clone diverged from the remote"


def test_auto_checks_for_conflicts_after_taking_the_sync_lock(
    memory_dir: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 DATA CORRUPTION, TOCTOU. `_commit_local_changes` used to run
    `_require_no_unresolved_conflict` BEFORE `with flock_excl(...)` rather
    than as the first statement inside it — unlike `push` and `pull`, which
    both check after their acquire. A conflict arriving while `auto` waited
    for a contended lock therefore landed after the only check that would
    have caught it, and `git add -A` staged the markers as resolved content.

    This is the silent variant, and the reason it is worth a test of its
    own: with a purely-local conflicted `git stash pop` there is no
    divergence from the remote, so the follow-on `pull --rebase` is a no-op
    and never aborts the run. Against the pre-fix commit `sync.auto`
    RETURNED NORMALLY — no exception at all — having committed `<<<<<<<`
    onto `main` and PUSHED it to the bare remote. Verified empirically by
    running this test with the guard restored to its pre-fix position: it
    fails on the `isinstance(error, sync.SyncError)` assertion below,
    reporting the returned dict. With that assertion temporarily moved
    beneath them, the marker scans fail too — separately confirmed on the
    clone and on the bare remote.

    Deterministic, not timing-tuned. Two synchronisation points replace any
    sleep: a holder thread signals once it actually holds `.sync.lock`, and
    a one-shot wrapper around `sync.flock_excl` signals at the instant
    `_commit_local_changes` reaches its acquire. That instant is exactly
    the window under test — with the guard misplaced it has already run and
    passed, and with the guard correctly placed it has not run yet — so the
    conflict is injected at the one moment that distinguishes the two. The
    60-second waits are deadlock failsafes, not timing assumptions: no
    assertion here turns on how long a step takes.

    A same-process thread is a real lock holder here: `flock` ownership is
    per open file description, so a second `os.open` + `LOCK_EX` in the same
    process blocks until the first releases (probed directly before this
    test was written).
    """
    _arm_a_conflicting_stash(memory_dir, bare_remote)
    before_local = _git(memory_dir, "rev-parse", "main").strip()
    before_remote = _git(bare_remote, "rev-parse", "main").strip()

    holder_holds_lock = threading.Event()
    auto_reached_acquire = threading.Event()
    holder_may_release = threading.Event()

    def hold_the_sync_lock() -> None:
        # The real `flock_excl`, captured at import: `sync.flock_excl` is
        # monkeypatched below and must not be picked up here.
        with flock_excl(memory_dir / sync._SYNC_LOCK_NAME):
            holder_holds_lock.set()
            holder_may_release.wait(timeout=60)

    holder = threading.Thread(target=hold_the_sync_lock, daemon=True)
    holder.start()
    assert holder_holds_lock.wait(timeout=60), "lock holder never acquired"

    reached_once = False

    @contextlib.contextmanager
    def signalling_flock_excl(path: Path, **kwargs: str) -> Generator[None]:
        nonlocal reached_once
        # First acquire only — that is `_commit_local_changes`'. `pull` and
        # `push` take the same lock later in the same `auto` run and must
        # not re-signal.
        if not reached_once:
            reached_once = True
            auto_reached_acquire.set()
        with flock_excl(path, **kwargs):
            yield

    monkeypatch.setattr(sync, "flock_excl", signalling_flock_excl)

    outcome: dict[str, object] = {}

    def run_auto() -> None:
        try:
            outcome["returned"] = sync.auto(memory_dir)
        except BaseException as exc:  # noqa: BLE001 - re-asserted below
            outcome["error"] = exc

    auto_thread = threading.Thread(target=run_auto, daemon=True)
    auto_thread.start()
    assert auto_reached_acquire.wait(timeout=60), (
        "`auto` never reached a `flock_excl` acquire — if the sync lock was "
        "removed, this test no longer covers the window it was written for"
    )

    # THE WINDOW. `auto` is now blocked on the contended acquire. Expected
    # to fail: the failure IS the conflicted state under test.
    subprocess.run(
        ["git", "stash", "pop"], cwd=memory_dir, capture_output=True, check=False
    )
    porcelain = _git(memory_dir, "status", "--porcelain")
    assert any(line.startswith("UU") for line in porcelain.splitlines()), (
        f"fixture did not wedge a conflicted stash pop; porcelain: {porcelain!r}"
    )
    assert not sync._rebase_in_progress(memory_dir), (
        "fixture wedged a rebase, not a stash pop — the state under test is "
        "the one that leaves no sentinel file"
    )

    holder_may_release.set()
    auto_thread.join(timeout=60)
    assert not auto_thread.is_alive(), "`auto` never finished after the release"
    holder.join(timeout=60)

    error = outcome.get("error")
    assert isinstance(error, sync.SyncError), (
        "`auto` did not refuse a conflict that landed during the lock wait; "
        f"it returned {outcome.get('returned')!r}"
    )

    # `main`, not `HEAD`: the sibling conflict tests document that a failed
    # rebase can detach HEAD while the corrupt commit sits on the branch tip.
    assert _git(memory_dir, "rev-parse", "main").strip() == before_local, (
        "auto committed onto main with unmerged files present"
    )
    assert _git(bare_remote, "rev-parse", "main").strip() == before_remote, (
        "auto SHIPPED a commit to the remote with unmerged files present"
    )
    _assert_no_commit_carries_conflict_markers(memory_dir)
    _assert_no_commit_carries_conflict_markers(bare_remote)


def test_pull_still_works_on_a_clean_worktree(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """Negative control for the pre-check: it must gate ONLY on dirty
    TRACKED files. A clone with untracked-but-ignored runtime sidecars (the
    normal state of any live store — index, event log) is clean as far as
    the rebase is concerned and must still pull."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="python list comprehension", scopes=["tools"])
    sync.push(memory_dir)

    other_dir = tmp_path / "other_clone"
    subprocess.run(
        ["git", "clone", str(bare_remote), str(other_dir)],
        check=True,
        capture_output=True,
    )
    # Runtime sidecars a live store always has lying around.
    (other_dir / EVENT_LOG_FILENAME).write_text("{}\n", encoding="utf-8")
    (other_dir / "untracked-note.txt").write_text("scratch\n", encoding="utf-8")

    result = sync.pull(other_dir)
    assert result["pulled"] is True


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


def test_auto_succeeds_when_a_tracked_memory_has_local_edits(
    memory_dir: Path, bare_remote: Path
) -> None:
    """🟡 THE HEADLINE BUG. `auto` pulled FIRST, and `git pull --rebase`
    refuses to run against a dirty worktree — so `sync auto` failed OUTRIGHT
    whenever any already-tracked memory file had local edits. That is the
    NORMAL state of a live store: a user edits a memory, runs the "sync me"
    one-shot, and gets a hard failure. Verified empirically against the
    pre-fix source (init, push, edit one existing memory, `sync.auto`
    raises `SyncError: cannot pull with rebase: You have unstaged changes`).

    `auto` now commits local changes BEFORE it pulls. That is not a new side
    effect — `auto`'s push step has always run `git add -A` and committed
    everything in the worktree, so the same content reaches the same commit
    either way; only the order changed, to the one git actually supports.

    Mutation-sound: restore `auto` to `pull(...)` then `push(...)` and this
    fails at the `sync.auto` call with git's dirty-worktree refusal.

    Pins the whole round trip, not just the absence of an exception: the
    edit must reach the remote, because an `auto` that "succeeds" without
    shipping the user's edit is the same bug wearing a green test."""
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    memory = store.write(content="durable fact", scopes=["tools"])
    sync.push(memory_dir)

    # The normal thing a user does between syncs: edit an existing memory.
    edited = _edit_tracked_memory(memory_dir, store, memory.id)

    result = sync.auto(memory_dir)

    assert result["committed_before_pull"] is True, (
        "auto did not commit the dirty worktree before pulling; the pull "
        f"would have refused: {result}"
    )
    pull_result = result["pull"]
    push_result = result["push"]
    assert isinstance(pull_result, dict) and pull_result["pulled"] is True
    assert isinstance(push_result, dict) and push_result["pushed"] is True

    # The worktree is clean afterwards — the edit became a commit.
    assert not sync._dirty_tracked_paths(memory_dir)
    # …and the edit actually reached the remote, not just the local commit.
    remote_history = _git(bare_remote, "log", "-p", "--all")
    assert "edited locally" in remote_history, (
        f"auto reported success but the edit to {edited} never reached the remote"
    )


def test_auto_is_a_no_op_commit_on_a_clean_worktree(
    memory_dir: Path, bare_remote: Path
) -> None:
    """Negative control for the commit-before-pull step: on a clean store it
    must NOT manufacture an empty commit. Without this, a fix that committed
    unconditionally would satisfy the test above while dirtying every user's
    history with one empty commit per cron tick."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="durable fact", scopes=["tools"])
    sync.push(memory_dir)

    before = _git(memory_dir, "rev-list", "--count", "HEAD").strip()
    result = sync.auto(memory_dir)
    after = _git(memory_dir, "rev-list", "--count", "HEAD").strip()

    assert result["committed_before_pull"] is False, result
    assert before == after, (
        f"auto created a commit on a clean worktree ({before} -> {after})"
    )


def test_auto_errors_on_non_repo(tmp_path: Path) -> None:
    """`auto` validates the repo itself now that it acts (commits) before
    delegating to pull/push — the error must still point at `sync init`
    rather than surfacing from a git call deeper down."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(sync.SyncError, match="not a git repo"):
        sync.auto(plain)


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


# ---------------------------------------------------------------------------
# The CLI command bodies (`bettermemory sync …`)
#
# Everything above exercises `sync.py`'s public functions directly. These pin
# the layer the user actually reaches: the five `_cli_sync_*` bodies in
# `bettermemory/cli/sync.py`, each carrying its own `SyncError` catch and its
# own exit-code mapping. Every refusal raised above travels to the user
# through exactly one of them, so a refusal that exits 0, or whose message
# never leaves the process, is invisible to every test above — the wrapper
# would report success while the store was never synced.
#
# Driven in-process through the real `bettermemory.cli.main`, so the argparse
# definitions and the `run()` dispatch arms are on the path too, with
# `BETTERMEMORY_DIR` pointed at the fixture store. Same shape as
# `tests/test_cli_smoke.py`, which is the house convention for CLI tests.
# ---------------------------------------------------------------------------


def _run_cli(
    argv: list[str], *, monkeypatch: pytest.MonkeyPatch, directory: Path
) -> None:
    """Invoke `bettermemory <argv>` in-process against `directory`."""
    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    cli_main()


def _run_cli_expecting_exit(
    argv: list[str],
    *,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    directory: Path,
    command: str,
) -> str:
    """Run a `sync` subcommand that must refuse; return its stderr.

    Asserts BOTH halves of the mapping every `_cli_sync_*` body owns, because
    each fails independently and each fails silently:

    * the exit code is 2 — a refusal that printed correctly but exited 0
      leaves a cron job or shell alias treating an unsynced store as synced;
    * stderr carries the `sync <command> failed: ` prefix — a refusal routed
      to stdout, or swallowed, leaves the user with a command that did
      nothing and said nothing about why.

    `stderr` is returned rather than asserted whole so callers can pin the
    specific diagnosis. It is matched with `in`, not `startswith`: the first
    run in a fresh environment also emits `load_config`'s default-config
    notice there.
    """
    with pytest.raises(SystemExit) as excinfo:
        _run_cli(argv, monkeypatch=monkeypatch, directory=directory)
    assert excinfo.value.code == 2, (
        f"`bettermemory {' '.join(argv)}` exited {excinfo.value.code!r}, not 2"
    )
    captured = capsys.readouterr()
    assert f"sync {command} failed: " in captured.err, (
        f"the refusal never reached stderr; stderr was {captured.err!r} and "
        f"stdout was {captured.out!r}"
    )
    return captured.err


def test_cli_sync_init_reports_its_actions_and_emits_json(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`sync init` renders the action list, then the same result as JSON.

    The `--json` arm is what a script reads, and it carries
    `gitignore_error` — the machine-readable twin of the action line that
    distinguishes "gitignore is canonical" from "the gitignore could not be
    reconciled". A `None` there is the CLI asserting the store is safe, so
    it is worth pinning that the field is actually emitted.
    """
    _run_cli(
        ["sync", "init", "--remote", str(bare_remote)],
        monkeypatch=monkeypatch,
        directory=memory_dir,
    )
    out = capsys.readouterr().out
    assert f"Initialised sync in {memory_dir.resolve()}." in out
    assert "initialised git repo on branch 'main'" in out
    assert ".gitignore written" in out
    assert f"added origin → {bare_remote}" in out

    _run_cli(["sync", "init", "--json"], monkeypatch=monkeypatch, directory=memory_dir)
    payload = json.loads(capsys.readouterr().out)
    assert payload["already_repo"] is True
    assert payload["gitignore_error"] is None
    assert ".gitignore already in canonical shape" in payload["actions"]


def test_cli_sync_init_maps_a_rejected_branch_name_to_exit_2(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`init`'s pre-existing `SyncError` path, end to end.

    `_require_git_name` is what stops a `--default-branch` value from being
    handed to `git init --initial-branch` as something git could read as a
    flag. It raises before anything is created, and the CLI has to turn that
    into a non-zero exit — otherwise a provisioning script reads exit 0 and
    moves on believing the store is a repo.
    """
    err = _run_cli_expecting_exit(
        ["sync", "init", "--default-branch=not a branch"],
        monkeypatch=monkeypatch,
        capsys=capsys,
        directory=memory_dir,
        command="init",
    )
    assert "not a branch" in err, (
        f"the refusal does not name the value it rejected: {err!r}"
    )
    assert not (memory_dir / ".git").exists(), (
        "init rejected the branch name but had already created the repo"
    )


def test_cli_sync_status_on_a_non_repo_points_at_init(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`status` never raises, so its non-repo branch is the whole error
    surface: a store that was never `sync init`-ed has to be told so, and
    told which command fixes it, on a zero exit."""
    _run_cli(["sync", "status"], monkeypatch=monkeypatch, directory=memory_dir)
    out = capsys.readouterr().out
    assert "is not a git repo" in out
    assert "bettermemory sync init" in out


def test_cli_sync_status_renders_the_repo_and_truncates_the_file_list(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The text rendering, including the cap on the modified-file list.

    Twelve edited memories, so the `[:10]` slice and the `... and N more`
    tail are both exercised — an off-by-one there either hides a file from
    a user auditing what a sync is about to ship, or reports a count that
    does not match the list printed above it.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    memories = [
        store.write(content=f"tracked fact number {i}", scopes=["tools"])
        for i in range(12)
    ]
    sync.push(memory_dir)
    for memory in memories:
        _edit_tracked_memory(memory_dir, store, memory.id)

    _run_cli(["sync", "status"], monkeypatch=monkeypatch, directory=memory_dir)
    out = capsys.readouterr().out

    assert f"Memory directory: {memory_dir.resolve()}" in out
    assert "branch: main" in out
    assert f"remote: {bare_remote}" in out
    assert "ahead: 0  behind: 0" in out
    assert "untracked: 0  modified: 12" in out
    assert "modified files:" in out
    listed = [line.strip() for line in out.splitlines() if line.startswith("    ")]
    assert len(listed) == 11, (
        f"expected 10 filenames plus the truncation line, got {listed}"
    )
    assert listed[-1] == "... and 2 more", listed


def test_cli_sync_status_on_a_remoteless_repo_omits_the_tracking_line(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The other side of every conditional in the text rendering.

    A store that was `sync init`-ed without a remote is a supported shape —
    `init`'s `--remote` is optional — and it must not be shown an
    `ahead/behind` line, because there is nothing to be ahead OF. Run
    clean first (no `modified files:` block at all), then with two edits,
    which is below the ten-file cap: the `... and N more` tail must be
    absent rather than reading `... and -8 more`.
    """
    sync.init(memory_dir)
    store = Store(memory_dir)
    memories = [
        store.write(content=f"a local-only fact {i}", scopes=["tools"])
        for i in range(2)
    ]
    _git(memory_dir, "add", "-A")
    _git(memory_dir, "commit", "-m", "seed the local-only store")

    _run_cli(["sync", "status"], monkeypatch=monkeypatch, directory=memory_dir)
    clean = capsys.readouterr().out
    assert "remote: <none>" in clean
    assert "ahead:" not in clean, (
        f"a repo with no remote was shown a tracking position: {clean!r}"
    )
    assert "untracked: 0  modified: 0" in clean
    assert "modified files:" not in clean

    edited = [_edit_tracked_memory(memory_dir, store, m.id) for m in memories]
    _run_cli(["sync", "status"], monkeypatch=monkeypatch, directory=memory_dir)
    dirty = capsys.readouterr().out
    assert "untracked: 0  modified: 2" in dirty
    assert "modified files:" in dirty
    for name in edited:
        assert name in dirty
    assert "more" not in dirty, (
        f"a two-file list was given a truncation tail: {dirty!r}"
    )


def test_cli_sync_status_json_carries_the_status_dict(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--json` emits `SyncStatus.to_dict()` and nothing else on stdout —
    the contract a script piping `sync status --json` into `jq` depends
    on."""
    sync.init(memory_dir)
    Store(memory_dir).write(content="an uncommitted fact", scopes=["tools"])

    _run_cli(
        ["sync", "status", "--json"], monkeypatch=monkeypatch, directory=memory_dir
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["is_repo"] is True
    assert payload["branch"] == "main"
    assert payload["remote_url"] is None
    assert payload["has_changes"] is True
    assert payload["untracked_count"] >= 1
    assert payload["modified_count"] == 0


def test_cli_sync_push_reports_the_commit_then_the_no_op(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The two success renderings, which say materially different things.

    `sync.push` returns `committed=False` when there was nothing to commit
    but still pushes prior commits; the CLI has to distinguish that from a
    real commit, or a user who edited nothing reads "Committed" and
    believes an edit was picked up.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="a fact worth syncing", scopes=["tools"])

    _run_cli(["sync", "push"], monkeypatch=monkeypatch, directory=memory_dir)
    assert "Committed and pushed to origin." in capsys.readouterr().out

    _run_cli(["sync", "push"], monkeypatch=monkeypatch, directory=memory_dir)
    out = capsys.readouterr().out
    assert "No local changes to commit; pushed prior commits to origin." in out

    _run_cli(["sync", "push", "--json"], monkeypatch=monkeypatch, directory=memory_dir)
    payload = json.loads(capsys.readouterr().out)
    assert payload["committed"] is False
    assert payload["pushed"] is True
    assert payload["remote"] == "origin"


def test_cli_sync_push_maps_a_missing_remote_to_exit_2(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`push`'s pre-existing `SyncError` path: a repo with no `origin`.

    The commit is made before the remote is consulted, so the failure has
    to be loud — the local half succeeded and the distributing half did
    not, which is exactly the state a silent exit 0 would misreport.
    """
    sync.init(memory_dir)
    Store(memory_dir).write(content="a fact with nowhere to go", scopes=["tools"])

    err = _run_cli_expecting_exit(
        ["sync", "push"],
        monkeypatch=monkeypatch,
        capsys=capsys,
        directory=memory_dir,
        command="push",
    )
    assert "no remote named 'origin'" in err
    assert "bettermemory sync init --remote" in err


def test_cli_sync_push_maps_the_blank_message_refusal_to_exit_2(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NEW REFUSAL, and the one the CLI could most easily have hidden.

    `_cli_sync_push` resolves the message as `message or
    DEFAULT_COMMIT_MESSAGE`. A whitespace-only `--message` is truthy, so it
    reaches `_require_commit_message` and is refused there for parity with
    `git commit -m "   "` — which aborts, where `commit-tree` would have
    recorded the blank message. Pinned at the CLI boundary because the
    alternative reading of that `or` (treat blank as absent, silently
    substitute the default) is a one-character change away and would commit
    under a message the user never asked for.

    Nothing may be committed, which is a separate claim from "refused" and
    so gets its own assertion.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="a fact that must not ship blank", scopes=["tools"])

    err = _run_cli_expecting_exit(
        ["sync", "push", "--message", "   \n  "],
        monkeypatch=monkeypatch,
        capsys=capsys,
        directory=memory_dir,
        command="push",
    )
    assert "empty after whitespace cleanup" in err
    assert "non-blank `--message`" in err
    assert not _git(memory_dir, "rev-list", "--all").strip(), (
        "a commit was recorded despite the blank --message"
    )


def test_cli_sync_push_maps_the_unconcluded_merge_refusal_to_exit_2(
    memory_dir: Path,
    bare_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NEW REFUSAL: a resolved-but-unconcluded merge, seen from the CLI.

    `_require_no_sequencer_state` refuses rather than approximating a merge
    commit the plumbing path cannot write. The user's route out is entirely
    in the message — `git commit` to conclude, or `git merge --abort` to
    back out — so a refusal that exits non-zero but drops the text leaves
    them with a store parked mid-merge and no stated way forward.

    The porcelain assertion belongs here as much as on the library twin:
    the CLI is where a user meets this refusal, and both routes the message
    offers only work if the store is still in the shape they left it.
    """
    other, _bystander = _wedge_a_resolved_but_unconcluded_merge(
        memory_dir, bare_remote, tmp_path
    )
    before = _git(other, "rev-parse", "main").strip()
    before_porcelain = _git(other, "status", "--porcelain")

    err = _run_cli_expecting_exit(
        ["sync", "push"],
        monkeypatch=monkeypatch,
        capsys=capsys,
        directory=other,
        command="push",
    )
    assert "MERGE_HEAD" in err, (
        f"the refusal does not name the state that blocked it: {err!r}"
    )
    assert "git commit" in err
    assert "git merge --abort" in err
    assert _git(other, "rev-parse", "main").strip() == before, (
        "the CLI push wrote a single-parent commit over an unconcluded merge"
    )
    assert _git(other, "status", "--porcelain") == before_porcelain, (
        "the CLI push mutated the index before refusing, degrading both "
        "remedies its own error message offers"
    )


def test_cli_sync_push_maps_the_lost_ref_race_to_exit_2(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NEW REFUSAL: the compare-and-swap losing to a commit that lands
    mid-sync.

    Unlike the other two, this one is not raised by a purpose-built guard —
    it is `git update-ref`'s own failure surfacing through `_run_git`'s
    default `check=True`. That makes the CLI mapping the only thing
    standing between "another actor's commit was preserved" and a user who
    believes their memories were pushed: the sync did not commit and did
    not push, and exit 0 here would say otherwise.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    store.write(content="clean baseline", scopes=["tools"])
    sync.push(memory_dir)
    store.write(content="a second clean fact", scopes=["tools"])

    interloper = _arm_a_commit_that_lands_mid_sync(monkeypatch)

    err = _run_cli_expecting_exit(
        ["sync", "push"],
        monkeypatch=monkeypatch,
        capsys=capsys,
        directory=memory_dir,
        command="push",
    )

    assert "tip" in interloper, (
        "`commit-tree` never ran — this test no longer covers the window it "
        "was written for"
    )
    assert "update-ref" in err, (
        f"the refusal does not name the git operation that failed: {err!r}"
    )
    assert _git(memory_dir, "rev-parse", "main").strip() == interloper["tip"], (
        "the sync overwrote a commit that landed mid-sync"
    )


def test_cli_sync_pull_reports_the_reindex_and_the_skip(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both `pull` renderings. The reindex count is the user's only signal
    that the FTS5 index caught up with the files the rebase landed, and
    `--no-reindex` has to say plainly that it did not — otherwise a
    deferred rebuild is indistinguishable from a completed one."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="a fact from the first host", scopes=["tools"])
    sync.push(memory_dir)

    _run_cli(["sync", "pull"], monkeypatch=monkeypatch, directory=memory_dir)
    out = capsys.readouterr().out
    assert "Pulled from origin." in out
    assert "reindexed 1 memories" in out

    _run_cli(
        ["sync", "pull", "--no-reindex"], monkeypatch=monkeypatch, directory=memory_dir
    )
    out = capsys.readouterr().out
    assert "--no-reindex passed: run `bettermemory reindex` when ready" in out

    _run_cli(["sync", "pull", "--json"], monkeypatch=monkeypatch, directory=memory_dir)
    payload = json.loads(capsys.readouterr().out)
    assert payload["pulled"] is True
    assert payload["reindexed"] is True
    assert payload["indexed_count"] == 1


def test_cli_sync_pull_maps_the_dirty_worktree_refusal_to_exit_2(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`pull`'s pre-existing `SyncError` path: uncommitted tracked edits.

    The normal state of a live store, so this is the refusal a user meets
    most often. Its value is entirely in the detail it carries — which file
    is in the way and which command clears it — and none of that survives
    if the CLI drops the message or exits 0.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    memory = store.write(content="a fact I am still editing", scopes=["tools"])
    sync.push(memory_dir)
    edited = _edit_tracked_memory(memory_dir, store, memory.id)

    err = _run_cli_expecting_exit(
        ["sync", "pull"],
        monkeypatch=monkeypatch,
        capsys=capsys,
        directory=memory_dir,
        command="pull",
    )
    assert edited in err, f"the refusal does not name the dirty file: {err!r}"
    assert "bettermemory sync push" in err
    assert "rebase.autoStash" in err


def test_cli_sync_auto_reports_completion_and_emits_json(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`auto`'s success rendering, plus the JSON a cron job would read.

    The text arm deliberately says very little; the JSON arm is where the
    three sub-results live, so that is what has to carry whether the push
    half actually happened.
    """
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="a fact for the one-shot", scopes=["tools"])
    # `auto` pulls, and a pull needs an upstream — establish one first, the
    # same way `test_auto_pulls_then_pushes` does.
    sync.push(memory_dir)
    Store(memory_dir).write(content="a fact the one-shot must ship", scopes=["tools"])

    _run_cli(["sync", "auto"], monkeypatch=monkeypatch, directory=memory_dir)
    assert "Auto-sync complete (remote=origin)." in capsys.readouterr().out

    _run_cli(["sync", "auto", "--json"], monkeypatch=monkeypatch, directory=memory_dir)
    payload = json.loads(capsys.readouterr().out)
    assert payload["remote"] == "origin"
    assert payload["committed_before_pull"] is False
    assert payload["pull"]["pulled"] is True
    assert payload["push"]["pushed"] is True


def test_cli_sync_auto_maps_the_conflict_refusal_to_exit_2(
    memory_dir: Path,
    bare_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`auto`'s pre-existing `SyncError` path: an unresolved conflict.

    `auto` is the command people wire into cron and shell aliases, so it is
    the one whose exit code is most likely to be the only thing anyone
    reads. It must refuse before committing anything, and say so — the
    message is what keeps the user from reaching for `sync push`, which
    would `git add -A` the markers into their memories.
    """
    other = _wedge_a_conflicted_merge(memory_dir, bare_remote, tmp_path)
    before = _git(other, "rev-parse", "main").strip()

    err = _run_cli_expecting_exit(
        ["sync", "auto"],
        monkeypatch=monkeypatch,
        capsys=capsys,
        directory=other,
        command="auto",
    )
    assert "Do NOT run `bettermemory sync push`" in err, (
        f"the refusal does not warn against the destructive command: {err!r}"
    )
    assert _git(other, "rev-parse", "main").strip() == before, (
        "auto committed onto main with unmerged files present"
    )
    _assert_no_commit_carries_conflict_markers(other)


def test_cli_sync_without_a_subcommand_prints_help(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare `bettermemory sync` falls through `run()`'s dispatch arms to
    `sub_parser.print_help()` — it must list the sub-subcommands rather
    than exiting silently or, worse, defaulting to one of them."""
    _run_cli(["sync"], monkeypatch=monkeypatch, directory=memory_dir)
    out = capsys.readouterr().out
    for name in ("init", "status", "push", "pull", "auto"):
        assert name in out, f"`sync` help does not mention the {name!r} subcommand"
