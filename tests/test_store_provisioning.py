"""Construction is pure; provisioning is `ensure()`; startup is `Store.open()`.

Before 7.17.0, `Store.__post_init__` created two directories, chmod'd both,
rebuilt a flagged index (which can fork `git`), and printed a divergence
warning to stderr. Constructing a store was therefore a migration, and the
tree had already routed around it twice in writing: the Store-free counters
below `Store` exist, by their own docstring, "for callers that have no Store
instance and must not construct one (`Store.__post_init__` mkdirs and
auto-rebuilds — write side effects)", and `_warn_on_index_divergence` declines
a full reconcile partly to stay cheap "on every cheap `Store()`".

The split is the first step of the Teams Store seam: a protocol cannot carry a
constructor, so the side effects have to leave one before anything can stand
behind it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from bettermemory import store as store_mod
from bettermemory.store import MemoryStore, Store

pytestmark = pytest.mark.anyio

_ZERO = timedelta(0)


# ---------------------------------------------------------------------------
# G1 — construction is pure
# ---------------------------------------------------------------------------


def test_construction_touches_no_disk_and_says_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`Store(path)` creates nothing and prints nothing.

    Both halves matter and they fail differently. The disk half is what lets a
    diagnostic hold a Store without altering the thing it is diagnosing; the
    output half is what stops a constructor from emitting a user-facing
    warning, which is how `bettermemory doctor` came to report the
    constructor's opinion rather than its own check's."""
    root = tmp_path / "never-provisioned"

    store = Store(root)

    assert not root.exists(), (
        "constructing a Store created its root — construction must be pure; "
        "provisioning is `ensure()`, which every mutator calls"
    )
    assert store.root == root.resolve(), "the root is still normalised"
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "", (
        f"construction wrote to the console: out={captured.out!r} err={captured.err!r}"
    )


# ---------------------------------------------------------------------------
# G2 — every mutator provisions, so no existing write caller had to change
# ---------------------------------------------------------------------------


def _fresh(tmp_path: Path, name: str) -> Store:
    """An unprovisioned store: constructed, root deliberately absent."""
    store = Store(tmp_path / name)
    assert not store.root.exists()
    return store


def test_write_provisions_an_absent_root(tmp_path: Path) -> None:
    """The zero-churn promise, stated as a test.

    719 constructions in this suite and 11 in `src/` never called `ensure()`,
    because `__post_init__` did it for them. They keep working because
    provisioning is a precondition every MUTATOR states — not because
    construction still has a side effect."""
    store = _fresh(tmp_path, "written-into")

    memory = store.write(content="provisioned on demand", scopes=["gate"])

    assert store.root.is_dir()
    assert store.tombstone_dir.is_dir()
    assert store.load_one(memory.id).id == memory.id


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits not meaningful on Windows")
def test_provisioning_still_applies_the_0o700_boundary(tmp_path: Path) -> None:
    """Moving the mkdir did not move the mode with it.

    The store root is the access-control boundary and a memory's FILENAME
    embeds the first ~43 chars of its summary, so a 0o755 root discloses the
    gist of the whole store to `ls` whatever the bodies are chmod'd to."""
    previous = os.umask(0o022)
    try:
        store = _fresh(tmp_path, "mode-check")
        store.write(content="mode boundary", scopes=["gate"])
    finally:
        os.umask(previous)

    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.tombstone_dir.stat().st_mode & 0o777 == 0o700


def test_every_public_mutator_provisions(tmp_path: Path) -> None:
    """Each of the eight, driven on a store whose root does not exist.

    Parameterising over the list rather than testing `write` alone is the
    point: `ensure()` is stated eight times, so a revert that drops it from
    exactly one must fail here. Each mutator is given the smallest input that
    reaches its first disk touch."""
    seeded = _fresh(tmp_path, "seed")
    seeded.write(content="seed for the read-modify mutators", scopes=["gate"])
    seed_id = seeded.load_all()[0].id

    # `write` is covered above; the other seven each need a prior record, so
    # they run against a store provisioned only by that seeding write.
    checks: list[tuple[str, Callable[[Store], object]]] = [
        ("update", lambda s: s.update(s.load_one(seed_id))),
        ("mark_verified", lambda s: s.mark_verified(seed_id)),
        ("record_corroboration", lambda s: s.record_corroboration(seed_id)),
        ("rename_scope", lambda s: s.rename_scope("gate", "gate2")),
        ("prune_tombstones", lambda s: s.prune_tombstones(older_than=_ZERO)),
        ("tombstone", lambda s: s.tombstone(seed_id, reason="gate")),
        ("restore", lambda s: s.restore(seed_id)),
    ]
    for name, call in checks:
        # A FRESH Store object over the same root each time: the `_provisioned`
        # flag is per-instance, so this exercises the mutator's own call rather
        # than a flag another mutator already set.
        store = Store(seeded.root)
        assert store._provisioned is False, f"{name}: fixture is not fresh"
        call(store)
        assert store._provisioned is True, (
            f"`{name}` completed without calling `ensure()`. Provisioning is a "
            "precondition of writing and every mutator states it; a mutator "
            "that skips it works only while some other caller happens to have "
            "created the directories first."
        )


# ---------------------------------------------------------------------------
# G3 — absent reads EMPTY, unreadable still RAISES
# ---------------------------------------------------------------------------


def test_absent_root_reads_empty_but_unreadable_root_still_raises(
    tmp_path: Path,
) -> None:
    """Both directions in one test, because the failure is folding them.

    A store that was never provisioned is an empty store, and that is a
    measurement. A store that cannot be listed is the THIRD VALUE and must
    propagate — reporting it as empty is the could-not-ask defect this project
    has drained three times (7.13.0 verdicts, 7.14.0 censuses, 7.15.0
    selection). Before construction became pure the first branch was
    unreachable, because constructing a Store created the directory."""
    absent = Store(tmp_path / "absent")
    assert absent.load_all() == [], "an absent store is an empty store"
    assert list(absent.iter_active()) == []

    # `sys.platform`, not `os.name`: mypy narrows on the former, so the
    # `os.geteuid` call below is unreachable on Windows as far as the type
    # checker is concerned. `os.name == "nt"` short-circuits at RUNTIME but
    # mypy still checks the whole expression, and `os.geteuid` does not exist
    # in the Windows stubs — which is why this only ever failed on the
    # windows-latest leg, where the type check runs against those stubs.
    if sys.platform == "win32":
        pytest.skip("POSIX mode bits do not deny the owner on Windows")
    if os.geteuid() == 0:
        pytest.skip("root is not denied by mode bits")

    unreadable = Store(tmp_path / "unreadable")
    unreadable.ensure()
    unreadable.root.chmod(0o000)
    try:
        with pytest.raises(OSError) as excinfo:
            unreadable.load_all()
        assert not isinstance(excinfo.value, FileNotFoundError), (
            "a root that cannot be READ must not be reported as ABSENT"
        )
    finally:
        unreadable.root.chmod(0o700)


# ---------------------------------------------------------------------------
# G4 — the startup checks fire from `Store.open()` and NOT from `Store()`
# ---------------------------------------------------------------------------


def test_flagged_index_is_rebuilt_by_open_and_left_alone_by_construction(
    tmp_path: Path,
) -> None:
    """Assert the PREMISE first: the index really is flagged.

    Otherwise this passes on a store that had nothing to rebuild, which is the
    rescue class round 199 recorded (a fixture that never created the
    condition)."""
    from tests.test_store import _flag_index_rebuild_pending

    store = Store.open(tmp_path / "flagged")
    store.write(content="something to rebuild", scopes=["gate"])

    _flag_index_rebuild_pending(store.root)
    assert _rebuild_is_pending(store.root), "premise: the index must be flagged"

    Store(store.root)
    assert _rebuild_is_pending(store.root), (
        "plain construction cleared the rebuild flag — the startup auto-heal "
        "belongs to `Store.open()`, so that a diagnostic can inspect a flagged "
        "index without healing it out from under itself"
    )

    Store.open(store.root)
    assert not _rebuild_is_pending(store.root), (
        "`Store.open()` did not run the schema-upgrade auto-heal"
    )


def _rebuild_is_pending(root: Path) -> bool:
    from bettermemory import index as _index

    return bool(_index.status(root).get("needs_rebuild"))


# ---------------------------------------------------------------------------
# G5 — a diagnostic does not mutate what it inspects (drives the CLI)
# ---------------------------------------------------------------------------


def test_doctor_does_not_warn_or_heal_the_store_it_inspects(tmp_path: Path) -> None:
    """Enters by the CLI, not by the library.

    Round 202's rule: a gate must enter by the path the defect lives on.
    Driving `Store` directly would prove only that `Store` is innocent — the
    trap lives in `bettermemory doctor` constructing six of them."""
    root = tmp_path / "doctored"
    store = Store.open(root)
    store.write(content="under inspection", scopes=["gate"])
    # An out-of-band `.md` the Store API never saw: index and disk now disagree,
    # which is exactly what the S4 divergence check warns about.
    (root / "2026-01-01-planted.md").write_text(
        "---\nid: 01J0000000000000000000000A\n"
        "created: 2026-01-01T00:00:00Z\nupdated: 2026-01-01T00:00:00Z\n"
        "scopes: [gate]\nconfidence: medium\nsource: explicit-statement\n"
        "category: fact\n---\n\nplanted\n"
    )

    env = {**os.environ, "BETTERMEMORY_DIR": str(root)}
    proc = subprocess.run(
        [sys.executable, "-m", "bettermemory", "doctor"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert "FTS5 index appears out-of-sync" not in proc.stderr, (
        "`bettermemory doctor` emitted the constructor's S4 divergence warning. "
        "Doctor has its own index-health check; a warning printed from a Store "
        "constructor reports the constructor's opinion, and it fires while "
        "doctor is still deciding what to report.\n"
        f"stderr:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# G6 — `Store` satisfies `MemoryStore`
# ---------------------------------------------------------------------------


def test_store_satisfies_the_memory_store_protocol() -> None:
    """Every public instance method on `Store` is named by the protocol.

    mypy and pyright already check the SIGNATURES structurally (both run in
    CI, and `store.py` type-checks clean with `Store` assigned to a
    `MemoryStore`). What a type checker will not tell you is that `Store` grew
    a NEW public method the protocol forgot — the seam silently narrowing
    while both sides still type-check."""
    protocol_names = {
        name
        for name in vars(MemoryStore)
        if not name.startswith("_") and name != "root"
    }
    store_names = {
        name
        for name, value in vars(Store).items()
        if not name.startswith("_")
        and (callable(value) or isinstance(value, property))
        and not isinstance(value, classmethod)
    }

    missing = store_names - protocol_names
    assert not missing, (
        f"`Store` has public members the `MemoryStore` protocol does not "
        f"name: {sorted(missing)}. Add them to the protocol, or make them "
        "private — a seam that silently omits part of the surface it claims "
        "to describe is worse than no seam."
    )


def test_the_store_free_counters_still_need_no_store() -> None:
    """The workaround this change makes unnecessary is not yet removed.

    `count_active_memory_files` and its siblings exist because constructing a
    Store was unsafe. That is no longer true, but deleting them is a separate
    change with its own callers to move, so this pins the CURRENT state rather
    than letting the docstring quietly become false: the functions are still
    here, and their stated reason is now historical."""
    for name in (
        "count_active_memory_files",
        "active_memory_filenames",
        "iter_active_memory_paths",
        "count_unparseable_memory_files",
        "scan_active_memory_ids",
    ):
        assert hasattr(store_mod, name), f"{name} disappeared without a migration"


# ---------------------------------------------------------------------------
# The 7.17.1 follow-on: what else assumed the constructor provisioned
# ---------------------------------------------------------------------------


def test_episode_write_provisions_an_absent_memory_root(tmp_path: Path) -> None:
    """`EpisodeStore` must not inherit a promise from a sibling class.

    Its `__post_init__` used to carry "Don't create the directory eagerly.
    `Store.__post_init__` already made `root` exist" — true until 7.17.0 made
    construction pure, and then silently false. `episodes_dir.mkdir` had no
    `parents=True`, so writing an episode to a store nobody had provisioned
    raised `FileNotFoundError`.

    Not reachable from any shipped entry point — `build_server` and
    `cli_context` both open through `Store.open()` — which is exactly why the
    suite stayed green and why this needs its own gate: the hole was in the
    library contract, where no production path walks."""
    from bettermemory.episodes import EpisodeStore

    root = tmp_path / "no-memory-root-yet"
    assert not root.exists()

    episode = EpisodeStore(root).write(
        session_id="s1", body="written before any memory was", takeaway="t"
    )

    assert episode.id
    assert (root / "episodes").is_dir()


# Every surviving mention of the old constructor behaviour in `src/`, keyed on
# CONTENT rather than on a line number. Both are deliberately HISTORICAL — they
# say what the constructor used to do and why a workaround exists — and both
# read in the past tense. A mention that asserts the behaviour in the PRESENT
# is the defect this ratchet exists for.
_HISTORICAL_POST_INIT_MENTIONS = (
    "`Store.__post_init__` mkdir'd and auto-rebuilt, write side effects from",
    '# dir. This used to add "`Store.__post_init__` already made `root`',
)


def test_no_source_prose_claims_the_constructor_still_provisions() -> None:
    """A published string is a claim, and eleven of them went false at once.

    7.17.0 made construction pure and left eleven comments and docstrings
    across six files asserting that `Store.__post_init__` mkdirs, chmods, or
    auto-rebuilds. Each was load-bearing prose — one of them (`episodes.py`)
    was the stated reason a `mkdir` had no `parents=True`, so the false
    comment and the real bug were the same defect wearing two hats.

    This is the same instrument `tests/test_roadmap_citations.py` uses for the
    same class: allowlist what legitimately survives, by content, and fail on
    anything new."""
    src = Path(__file__).resolve().parents[1] / "src" / "bettermemory"
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if "Store.__post_init__" not in line:
                continue
            if any(known in line for known in _HISTORICAL_POST_INIT_MENTIONS):
                continue
            offenders.append(
                f"  {path.relative_to(src.parent.parent)}:{lineno}: {line.strip()}"
            )

    assert not offenders, (
        "source prose still describes `Store.__post_init__` as provisioning. "
        "Since 7.17.0 construction is pure: it normalises the path and does "
        "nothing else. Point the claim at `Store.ensure()` (provisioning) or "
        "`Store.open()` (provisioning + the startup checks), or mark it "
        "explicitly historical and add it to the allowlist above:\n"
        + "\n".join(offenders)
    )


def test_the_allowlist_has_no_dead_entries() -> None:
    """An allowlist entry that matches nothing is a claim that went stale in
    the other direction — it suggests a mention survives when it does not,
    and it quietly widens the ratchet for whatever lands on that text next."""
    src = Path(__file__).resolve().parents[1] / "src" / "bettermemory"
    blob = "\n".join(p.read_text() for p in sorted(src.rglob("*.py")))
    dead = [known for known in _HISTORICAL_POST_INIT_MENTIONS if known not in blob]
    assert not dead, f"allowlist entries match nothing in src/: {dead}"
