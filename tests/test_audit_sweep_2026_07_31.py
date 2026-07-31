"""Regression tests for the 2026-07-31 adversarial sweep (@ bac16f4).

One file per audit round, following `test_audit_sweep_round77.py`. Every
test here reproduces a confirmed finding's failure mode and asserts the
fix, so a regression fails as the original defect rather than as a
changed assertion.

The sweep's spine is one class of defect: **a write reports success and
the record is then invisible to every read surface.** It was reachable
from five callers, all closed by one chokepoint —
`store._revalidate_before_persist` — and a sixth caller reached the same
end state through a scope that no filter can equal. The rest are the
durability edges the same pass turned up: a rebuild that destroyed the
index it was repairing, a restore that destroyed the tombstone it could
not re-admit, an admission path that could mint an un-removable record, a
serialisation bomb the expansion guard could not see, and two ways a body
or a frontmatter string failed to survive its own round trip.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bettermemory import _frontmatter as frontmatter
from bettermemory import index
from bettermemory.models import (
    LinkType,
    MemoryLink,
    generate_ulid,
    looks_truncated,
)
from bettermemory.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "memories")


def _write(store: Store, body: str = "Postgres listens on 5432 in staging.", **kw):
    return store.write(content=body, scopes=kw.pop("scopes", ["infra"]), **kw)


# ---------------------------------------------------------------------------
# The class: model_copy skips validators, and the store used to persist
# whatever it was handed
# ---------------------------------------------------------------------------


def test_over_cap_scopes_are_refused_instead_of_silently_vanishing(
    store: Store,
) -> None:
    """The headline defect, at the store level.

    `model_copy(update=...)` runs no field validators, so an over-cap
    record serialised fine (64 entries is nowhere near the 64 KB YAML
    cap), the write returned normally, and the next `_load_path` raised
    inside `Memory(...)` — which `load_all` catches and skips. The record
    left every read surface while its file sat on disk looking healthy.
    """
    memory = _write(store)
    over_cap = memory.model_copy(update={"scopes": [f"s{i}" for i in range(200)]})

    with pytest.raises(ValueError, match="fails its own model validation"):
        store.update(over_cap)

    # The pre-existing record is untouched and still readable — the
    # refusal must not be a partial write.
    assert [m.id for m in store.load_all()] == [memory.id]
    assert store.load_one(memory.id).scopes == ["infra"]


def test_over_cap_links_are_refused_instead_of_silently_vanishing(
    store: Store,
) -> None:
    """Same class, different field. `handlers/update.py` guards the links
    cap explicitly; `handlers/conflicts.py` appends its contradiction link
    with no such guard, so a source already at 64 links became 65 on disk
    and disappeared. The chokepoint covers the caller that has no guard.
    """
    memory = _write(store)
    links = [
        MemoryLink(type=LinkType.EXTENDS, target_id=generate_ulid(), note="n")
        for _ in range(65)
    ]

    with pytest.raises(ValueError, match="fails its own model validation"):
        store.update(memory.model_copy(update={"links": links}))

    assert [m.id for m in store.load_all()] == [memory.id]


def test_a_refused_write_leaves_the_record_loadable(store: Store) -> None:
    """The point of validating BEFORE serialising rather than after.

    A post-hoc check would already have replaced the file. This asserts
    the file on disk is still the old, valid record — not a truncated or
    half-written one.
    """
    memory = _write(store, body="The release runbook pushes main, then watches CI.")
    with pytest.raises(ValueError):
        store.update(
            memory.model_copy(update={"scopes": [f"s{i}" for i in range(100)]})
        )

    path = next(Path(store.root).glob("*.md"))
    reparsed = frontmatter.load(path)
    assert reparsed.metadata["scopes"] == ["infra"]
    assert "release runbook" in reparsed.content


# ---------------------------------------------------------------------------
# Index rebuild: the repair tool used to destroy what it was repairing
# ---------------------------------------------------------------------------


def test_rebuild_survives_two_files_sharing_one_id(store: Store) -> None:
    """A conflicted copy (`<name> 2.md` from iCloud/Dropbox, a `cp` before
    a hand-edit, a restored backup) puts two active files under one id.

    The rebuild used to `INSERT` each file, hit `UNIQUE constraint failed:
    memories.id` on the second, and fall into the corruption path — which
    unlinked the healthy index, retried the identical feed, and failed
    again. The store was left with an empty `needs_rebuild` index and
    `doctor` advising `bettermemory reindex`, the one command that could
    no longer succeed. Those are exactly the situations reindex exists for.
    """
    _write(store, body="The release runbook pushes main, then watches CI.")
    _write(store, body="Postgres listens on 5432 in staging.", scopes=["db"])
    root = Path(store.root)
    original = sorted(root.glob("*.md"))[0]
    shutil.copyfile(original, root / "conflicted copy 2.md")

    counted = index.rebuild(root, Store(root).iter_active())

    # Duplicate ids collapse to one row, matching what
    # `scan_active_memory_ids` documents for the same situation.
    assert counted == 2
    status = index.status(root)
    assert status["indexed_count"] == 2
    assert status["needs_rebuild"] is False


def test_a_constraint_violation_never_unlinks_a_healthy_index(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deeper hazard, independent of the duplicate-id trigger.

    `sqlite3.IntegrityError` says something about the ENTRIES, never about
    the file, so re-running the same feed against a fresh file fails
    identically — the only thing the corruption fallback accomplished was
    destroying a valid index. Any future constraint must propagate instead.
    """
    import sqlite3

    _write(store)
    root = Path(store.root)
    index.rebuild(root, Store(root).iter_active())
    index_file = index.index_path(root)
    size_before = index_file.stat().st_size
    assert size_before > 0

    def _boom(conn: object, memory: object, filename: str) -> None:
        raise sqlite3.IntegrityError("UNIQUE constraint failed: memories.id")

    monkeypatch.setattr(index, "_upsert_memory", _boom)
    with pytest.raises(sqlite3.IntegrityError):
        index.rebuild(root, Store(root).iter_active())

    assert index_file.exists(), "a feed-level error must not delete the index"
    assert index_file.stat().st_size == size_before


# ---------------------------------------------------------------------------
# consolidate --llm: the scope carry the non-LLM path already had
# ---------------------------------------------------------------------------


def test_an_llm_merge_carries_the_duplicate_s_scopes_onto_the_keeper(
    store: Store,
) -> None:
    """Similarity is scope-blind, so two near-identical bodies in disjoint
    project scopes cluster well over the dedup threshold. The non-LLM path
    merges the scopes for that reason, with a comment saying so; the LLM
    path seeds from the SAME scope-blind pass and did not, so
    `consolidate --llm --apply --yes` removed the fact from one project
    with no error and no report entry.
    """
    from bettermemory import consolidate
    from bettermemory import llm

    keeper = store.write(
        content="Release checklist: push main, watch the matrix, then push the tag.",
        scopes=["projects:alpha"],
    )
    duplicate = store.write(
        content="Release checklist: push main, watch the CI matrix, then push tag.",
        scopes=["projects:beta"],
    )
    by_id = {m.id: m for m in store.load_all()}

    consolidate._apply_llm_proposal(
        store,
        llm.MergeProposal(
            keeper_id=keeper.id,
            duplicate_ids=(duplicate.id,),
            new_body="Release checklist: push main, watch the matrix, then push the tag.\n",
            rationale="near-duplicates",
        ),
        by_id,
        session_id="s1",
    )

    assert store.load_one(keeper.id).scopes == ["projects:alpha", "projects:beta"]


# ---------------------------------------------------------------------------
# Restore: proving re-admission before destroying the only other copy
# ---------------------------------------------------------------------------


def test_restore_keeps_the_tombstone_when_the_record_cannot_be_re_admitted(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_find_tombstone_path_for_id` accepts any tombstone whose
    frontmatter parses, which is strictly wider than what
    `_parse_memory_file` re-admits — a tombstone from a newer
    `schema_version` (a `sync pull` from a newer host, or a downgrade) is
    the live case. Unlinking first left the record in neither listing:
    gone from `list_tombstones`, unparseable in `load_all`, and a retry
    raising `NotTombstonedError`.
    """
    memory = _write(store)
    store.tombstone(memory.id, reason="cycle", session_id="s1")
    assert len(store.list_tombstones()) == 1

    def _refuse(self: Store, path: Path) -> None:
        raise ValueError("schema_version 2 is newer than this reader supports")

    with monkeypatch.context() as patched:
        # Patched on the class, so it must be undone before the
        # recovery assertions below — a Store built while it is active
        # would inherit the refusing loader.
        patched.setattr(Store, "_load_path", _refuse)
        with pytest.raises(ValueError):
            store.restore(memory.id)

    assert len(store.list_tombstones()) == 1, "the tombstone must survive"
    # And the record is genuinely recoverable, which is the property the
    # unlink-first ordering destroyed.
    assert store.restore(memory.id).id == memory.id
    assert [m.id for m in store.load_all()] == [memory.id]


# ---------------------------------------------------------------------------
# Admission reservation on the frontmatter-YAML axis
# ---------------------------------------------------------------------------


def test_an_update_cannot_mint_an_un_removable_record(store: Store) -> None:
    """64 links each carrying a ~950-char note are all individually legal
    and together push the frontmatter to within a few dozen bytes of the
    64 KiB YAML cap. The record committed, and `memory_remove` then failed
    forever because the tombstone's `removed:` metadata no longer fit —
    as did both escape hatches `handlers/remove.py` documents.

    The file axis has reserved that budget at admission since 3.14.1; this
    asserts the YAML axis now does too.
    """
    memory = _write(store)
    fat = [
        MemoryLink(type=LinkType.EXTENDS, target_id=generate_ulid(), note="x" * 952)
        for _ in range(64)
    ]

    with pytest.raises(ValueError, match="un-removable"):
        store.update(memory.model_copy(update={"links": fat}))

    # The record the refusal protected is still removable, which is the
    # whole point of refusing.
    store.tombstone(memory.id, reason="rm", session_id="s1")
    assert store.load_all() == []


# ---------------------------------------------------------------------------
# Body round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        # CRLF normalises on the way in, so the file and every reader
        # agree from the first write rather than after the first re-dump.
        ("alpha\r\nbeta\r\ngamma", "alpha\nbeta\ngamma"),
        # Both CRs go, because `dumps` mirrors the read normalisation rather
        # than approximating it — otherwise the disagreement just moves one
        # carriage return deeper.
        ("alpha\r\r\nbeta", "alpha\nbeta"),
        # A lone CR is content, not a terminator this format knows about.
        ("alpha\rbeta", "alpha\rbeta"),
        ("plain\nbody", "plain\nbody"),
        (
            "a line that is exactly\n---\nand more",
            "a line that is exactly\n---\nand more",
        ),
    ],
)
def test_bodies_round_trip_through_frontmatter(body: str, expected: str) -> None:
    """`loads` strips the CR off every line so a Windows-authored file reads
    the same everywhere, and `dumps` used to write the body verbatim — so a
    CRLF body sat on disk with its CRs while every reader returned it
    without them, until the first lifecycle re-dump made the readers'
    version permanent. `dumps` now normalises to match, which is the half
    that was missing.
    """
    post = frontmatter.Post(content=body, metadata={"id": generate_ulid()})
    assert frontmatter.loads(frontmatter.dumps(post)).content == expected


def test_disk_and_readers_agree_on_a_crlf_body_from_the_first_write(
    store: Store,
) -> None:
    """The end-to-end version, and the property that actually matters: what
    `memory_show` returns is what the bytes on disk say, with no lifecycle
    event needed to reconcile them.
    """
    memory = store.write(content="alpha\r\nbeta\r\ngamma", scopes=["infra"])
    path = next(Path(store.root).glob("*.md"))
    assert b"\r" not in path.read_bytes()
    assert store.load_one(memory.id).body == "alpha\nbeta\ngamma\n"

    # And a re-dump changes nothing, because there is nothing left to
    # launder — the failure mode was that this step silently rewrote the file.
    before = path.read_bytes()
    store.tombstone(memory.id, reason="cycle", session_id="s1")
    store.restore(memory.id)
    assert next(Path(store.root).glob("*.md")).read_bytes() == before


@pytest.mark.parametrize(
    "value",
    [
        "ran the migration\x85then reverted it",
        "\x85\x85",
        "a\x85  b",
        "plain value with no line breaks",
    ],
)
def test_frontmatter_strings_round_trip_through_u0085(value: str) -> None:
    """U+0085 is a YAML 1.1 line break. Emitted raw inside a single-quoted
    scalar it folds back to a space on read, so every frontmatter string —
    an episode takeaway, a tombstone's `removed_reason`, a link note —
    was silently rewritten. Double-quoted style emits `\\N` and survives.
    """
    post = frontmatter.Post(content="body", metadata={"reason": value})
    assert frontmatter.loads(frontmatter.dumps(post)).metadata["reason"] == value


# ---------------------------------------------------------------------------
# Serialisation bombs
# ---------------------------------------------------------------------------


def test_a_few_large_scalars_are_as_much_a_bomb_as_many_small_ones() -> None:
    """The node budget charges a 50,000-character scalar and the letter `a`
    the same 1 node, so a bomb built from a few huge scalars walked straight
    through it: 12,359 expanded nodes against a 65,536 budget, then 218
    seconds and 1.1 GB of RSS inside `yaml.dump` to produce a 555 MB string
    for the post-hoc byte cap to reject. The record was also left
    permanently un-removable, since `tombstone` re-serialises it.

    Reached through the module's documented threat model: a hostile `sync
    pull` or a hand-edit dropping a 50 KB `.md` into the memory dir.
    """
    big = "A" * 50_000
    level1 = [big] * 10
    level2 = [level1] * 10
    level3 = [level2] * 10
    level4 = [level3] * 10
    post = frontmatter.Post(
        content="body", metadata={"id": generate_ulid(), "s": big, "l4": level4}
    )

    with pytest.raises(ValueError, match="scalar bytes"):
        frontmatter.dumps(post)


def test_the_densest_realistic_record_still_serialises() -> None:
    """The other half of a budget: it has to admit real records. This is
    every list-shaped field at its cap, with long notes and paths.
    """
    post = frontmatter.Post(
        content="body",
        metadata={
            "id": generate_ulid(),
            "scopes": [f"scope-number-{i}" for i in range(64)],
            "verified_paths": [
                f"~/some/project/path/number/{i}/file.py" for i in range(64)
            ],
            "links": [
                {"type": "extends", "target_id": generate_ulid(), "note": "n" * 400}
                for _ in range(64)
            ],
        },
    )
    assert frontmatter.dumps(post)


# ---------------------------------------------------------------------------
# Anchors: `$` matches before a trailing newline, `\Z` does not
# ---------------------------------------------------------------------------


def test_a_scope_with_a_trailing_newline_is_rejected() -> None:
    """`"projects:foo\\n"` passed `_SCOPE_RE` and nothing on the write path
    strips it, so the record was filed under a scope no filter can equal —
    invisible to scope filters, auto-scope resolution and `memory_list`,
    and indistinguishable on disk from a normal scope.
    """
    from bettermemory.models import validate_scope

    assert validate_scope("projects:foo") == "projects:foo"
    with pytest.raises(ValueError, match="invalid scope"):
        validate_scope("projects:foo\n")


def test_a_ulid_with_a_trailing_newline_is_rejected() -> None:
    """Same anchor bug: a newline-suffixed `target_id` on a `MemoryLink` is
    accepted and then equality-matches no memory id that will ever exist.
    """
    from bettermemory.models import is_valid_ulid

    valid = generate_ulid()
    assert is_valid_ulid(valid)
    assert not is_valid_ulid(valid + "\n")


# ---------------------------------------------------------------------------
# Truncation detection
# ---------------------------------------------------------------------------


def test_looks_truncated_flags_the_incident_shape() -> None:
    """The body of `01KY0PHMFC2ZDR5G4EX7GFK89A`, as it sat on disk for ten
    days with every check green. Nothing in the store truncated it — it
    arrived that way from the caller — but nothing could say so either.
    """
    assert looks_truncated("The whole security/red-team stack (Hak5")
    assert looks_truncated("Forgejo dumped to ~/Documents/homelab-backups/forgejo")


@pytest.mark.parametrize(
    "body",
    [
        "Postgres listens on 5432 in staging.",
        "Does the release gate on CI?",
        "Stop. Read the runbook first!",
        "The keeper wins (see the tie-break rule)",
        'The rule is simple: "always take the lock first"',
        "Config lives at `~/.config/bettermemory/config.toml`",
        "| tool | cost |\n| --- | --- |\n| search | 200 |",
        "See the plan: docs/ROADMAP.md;",
    ],
)
def test_looks_truncated_does_not_flag_complete_bodies(body: str) -> None:
    """Measured false-positive rate on the maintainer's live store is
    1 in 234. These are the endings that must stay clean.
    """
    assert not looks_truncated(body)


def test_looks_truncated_ignores_an_empty_body() -> None:
    """An empty body is refused at the handler boundary; it is not this
    predicate's business to report it as truncated as well."""
    assert not looks_truncated("")
    assert not looks_truncated("   \n  ")


def test_dumps_output_is_a_fixed_point_of_loads(store: Store) -> None:
    """The general property, rather than one shape at a time: whatever
    `dumps` writes must survive `loads` unchanged, or some body somewhere
    reads back as something other than the bytes on disk.
    """
    for body in (
        "alpha\r\nbeta",
        "alpha\r\r\nbeta",
        "alpha\rbeta",
        "\r\n\r\nleading blanks",
        "trailing\r\n\r\n",
        "plain",
    ):
        post = frontmatter.Post(content=body, metadata={"id": generate_ulid()})
        once = frontmatter.dumps(post)
        twice = frontmatter.dumps(frontmatter.loads(once))
        assert once == twice, f"{body!r} is not a fixed point"
