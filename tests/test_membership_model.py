"""The membership model (RS-F6, design 2 and 3) on constructed locations.

Every location here is built by hand: the history reader is an in-memory
object store, the liveness check reads an in-memory disk, and the token
writer writes into that disk. So each rung of the ladder, each
contradiction, the tie-breaking, the strict switch, proposals and the
surface table run without git, a filesystem walk or a clock. Tests are
named by the design section 7 row they decide where one applies.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory import membership as mb
from bettermemory.location import NO_MACHINE, continuity
from bettermemory.membership import (
    ASK,
    DEFERRED,
    F,
    FOLDER,
    GLOBAL,
    GLOBAL_OUTCOME,
    GLOBAL_RELATION,
    GONE,
    H,
    INDETERMINATE,
    LIVE,
    LIVE_LEGACY,
    NEW,
    OTHER_PROJECT,
    OTHER_TENANT,
    OUTSIDE,
    PRIMARY,
    R,
    REPLACED,
    REPO,
    SAME,
    SAME_PROJECT,
    SILENT,
    SUB_PROJECT,
    SURFACES,
    UNKNOWN,
    UNPROVEN,
    UNREACHABLE,
    UNREADABLE,
    Checkout,
    Declaration,
    EpisodeRef,
    Home,
    InitStamp,
    Location,
    Manifest,
    Membership,
    ObjectKey,
    ProjectFileRef,
    Resolution,
    View,
    admitted,
    relation,
)
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import RecordHome, Store
from bettermemory.location_git import reachable_among

_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


# ---------------------------------------------------------------------------
# An object store, a disk and a world
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Commit:
    parents: tuple[str, ...]
    fingerprint: str
    blobs: tuple[str, ...]


class History:
    """A repository's objects as `HistoryReader` asks for them."""

    def __init__(self, commits: dict[str, _Commit] | None = None) -> None:
        self.commits: dict[str, _Commit] = dict(commits or {})
        self.objects: set[str] = set()

    def copy(self) -> History:
        out = History(self.commits)
        out.objects = set(self.objects)
        return out

    def holds(self, commits: Sequence[str]) -> set[str]:
        return {c for c in commits if c in self.commits}

    def holds_objects(self, ids: Sequence[str]) -> set[str]:
        return {i for i in ids if i in self.objects or i in self.commits}

    def fingerprint(self, commit: str) -> str | None:
        found = self.commits.get(commit)
        return None if found is None else found.fingerprint

    def parents(self, commit: str) -> Sequence[str]:
        found = self.commits.get(commit)
        return () if found is None else found.parents

    def reachable_among(self, commits: Sequence[str]) -> dict[str, frozenset[str]]:
        return reachable_among(self, commits)

    def blobs(self, commit: str) -> Sequence[str]:
        found = self.commits.get(commit)
        return () if found is None else found.blobs


_counter = itertools.count(1)


def _sha(label: str) -> str:
    return hashlib.sha1(f"{label}-{next(_counter)}".encode()).hexdigest()


def _token() -> str:
    return "".join(_CROCKFORD[next(_counter) * 7 % 32] for _ in range(20)) + (
        f"{next(_counter):06d}"
    )


def _file_id() -> str:
    return "bmp_" + "".join(
        _CROCKFORD[(next(_counter) * 11 + i) % 32] for i in range(26)
    )


@dataclass
class Repo:
    """One git directory on the in-memory disk."""

    machine: str
    path: str
    key: ObjectKey
    history: History
    head: str | None = None
    remotes: set[str] = field(default_factory=set)
    token: str | None = None
    kind: str = PRIMARY
    common_key: ObjectKey | None = None
    stamps: tuple[InitStamp, ...] = ()
    files: tuple[ProjectFileRef, ...] = ()
    shallow: bool = False
    manifest: Manifest | None = None
    token_policy: str | None = None  # a skipped or failed status
    root_key: ObjectKey | None = None
    evicted: bool = False

    def commit(self, *, rewrite: str | None = None, blobs: Iterable[str] = ()) -> str:
        sha = _sha(self.path)
        fingerprint = rewrite or hashlib.sha256(sha.encode()).hexdigest()[:32]
        made = tuple(blobs) or tuple(_sha("blob") for _ in range(3))
        self.history.commits[sha] = _Commit(
            (self.head,) if self.head else (), fingerprint, made
        )
        self.history.objects.update(made)
        self.head = sha
        return sha

    def location(self, **overrides: object) -> Location:
        fields: dict[str, object] = dict(
            machine=self.machine,
            cwd=self.path,
            state=REPO,
            root=self.path,
            kind=self.kind,
            git_dir=self.path if self.kind == "bare" else f"{self.path}/.git",
            object_key=self.key,
            common_object_key=self.common_key or self.key,
            root_object_key=self.root_key,
            head=self.head,
            remote_keys=frozenset(self.remotes),
            shallow=self.shallow,
            token=self.token,
            token_status="present" if self.token else "n/a",
            stamps=self.stamps,
            files=self.files,
            manifest=self.manifest,
            history=self.history,
            evicted=self.evicted,
            detail="HEAD" if self.evicted else "",
        )
        fields.update(overrides)
        return Location(**fields)  # type: ignore[arg-type]


#: The stores the worlds of one test opened, closed when it ends.
_OPEN_STORES: list[Store] = []


@pytest.fixture(autouse=True)
def _close_stores() -> Iterator[None]:
    yield
    while _OPEN_STORES:
        _OPEN_STORES.pop().close()


class World:
    """A store, a model over it, and the disk its liveness reads."""

    def __init__(self, tmp_path: Path, machine: str = "M1") -> None:
        self.store = Store(tmp_path / "store")
        _OPEN_STORES.append(self.store)
        self.machine = machine
        self.disk: dict[tuple[str, str], Repo | str] = {}
        self.clock = _NOW
        self.inode = itertools.count(1000)
        self.model = self.model_for(machine)
        self.records: dict[str, Resolution] = {}

    def model_for(self, machine: str) -> Membership:
        return Membership(
            self.store,
            machine,
            liveness=self.liveness,
            token_writer=self.write_token,
            now=lambda: self.clock,
        )

    # -- the disk --------------------------------------------------------

    def key(self, *, device: str = "1", birth: bool = True) -> ObjectKey:
        inode = str(next(self.inode))
        return ObjectKey(device, inode, f"{inode}000" if birth else None)

    def liveness(self, checkout: Checkout, view: View) -> str:
        here = self.disk.get((view.machine, view.path))
        if here is None:
            return GONE
        if here == "unreachable":
            return UNREACHABLE
        assert isinstance(here, Repo)
        if checkout.object_key is None:
            return LIVE_LEGACY
        verdict = checkout.object_key.compare(here.key)
        if verdict == SAME:
            return LIVE
        if verdict == UNPROVEN:
            # Witness O without birth time, the rule rung 2 applies (B18).
            held, _how = continuity(
                checkout.token,
                checkout.last_head,
                checkout.stamps,
                token_here=here.token,
                head_here=here.head,
                stamps_here=here.stamps,
                holds=here.history.holds,
            )
            return LIVE if held else REPLACED
        return REPLACED

    def write_token(self, checkout: Checkout) -> tuple[str | None, str]:
        token = _token()
        for (machine, path), repo in self.disk.items():
            if isinstance(repo, Repo) and repo.key == checkout.object_key:
                repo.token = token
        return token, "written"

    def repo(
        self,
        path: str,
        *,
        commits: int = 2,
        remote: str | None = None,
        machine: str | None = None,
        birth: bool = True,
        device: str = "1",
        with_token: bool = False,
        **extra: object,
    ) -> Repo:
        repo = Repo(
            machine=machine or self.machine,
            path=path,
            key=self.key(device=device, birth=birth),
            history=History(),
            remotes={remote} if remote else set(),
            token=_token() if with_token else None,
            **extra,  # type: ignore[arg-type]
        )
        for _ in range(commits):
            repo.commit()
        self.disk[(repo.machine, path)] = repo
        return repo

    def move(
        self, repo: Repo, path: str, *, same_fs: bool = True, machine: str | None = None
    ) -> Repo:
        del self.disk[(repo.machine, repo.path)]
        repo.path = path
        repo.machine = machine or repo.machine
        if not same_fs:
            repo.key = self.key(device="2")
            if repo.common_key is not None:
                repo.common_key = repo.key
        self.disk[(repo.machine, path)] = repo
        return repo

    def copy(
        self, repo: Repo, path: str, *, machine: str | None = None, **extra: object
    ) -> Repo:
        fields: dict[str, object] = dict(
            path=path,
            machine=machine or repo.machine,
            key=self.key(),
            common_key=None,
            history=repo.history.copy(),
            remotes=set(repo.remotes),
        )
        fields.update(extra)
        twin = replace(repo, **fields)  # type: ignore[arg-type]
        self.disk[(twin.machine, path)] = twin
        return twin

    def clone(
        self, repo: Repo, path: str, *, remote: str | None = None, **extra: object
    ) -> Repo:
        """A fresh clone: the history, another object, no token, the
        upstream's remote unless another is named."""
        fields: dict[str, object] = dict(
            path=path,
            key=self.key(),
            common_key=None,
            history=repo.history.copy(),
            remotes={remote} if remote else set(repo.remotes),
            token=None,
            files=(),
        )
        fields.update(extra)
        twin = replace(repo, **fields)  # type: ignore[arg-type]
        self.disk[(twin.machine, path)] = twin
        return twin

    def delete(self, repo: Repo) -> None:
        self.disk.pop((repo.machine, repo.path), None)

    # -- sightings and records ---------------------------------------------------

    def see(
        self,
        repo: Repo,
        *,
        strict: bool = False,
        model: Membership | None = None,
        **overrides: object,
    ) -> Resolution:
        """A sighting as the capture makes it: a checkout without a token
        gets one written (unless the policy or the directory refuses)."""
        written: str | None = None
        status = "present" if repo.token else "n/a"
        if repo.token is None and repo.kind != "bare":
            if repo.token_policy:
                status = repo.token_policy
            else:
                written = _token()
                status = "written"
        loc = repo.location(token_written=written, token_status=status, **overrides)
        res = (model or self.model).resolve(loc, strict=strict)
        if written is not None:
            repo.token = written
        return res

    def write(self, res: Resolution, kind: str = "memory") -> str:
        home = res.home
        assert home is not None
        rid = self.put(home, kind, f"sess_{res.checkout}")
        self.records[rid] = res
        return rid

    def put(self, home: RecordHome, kind: str = "memory", session: str = "s") -> str:
        """A record written with `home`, as the write path writes it."""
        rid = generate_ulid()
        if kind == "memory":
            memory = Memory(
                id=rid,
                created=_NOW,
                updated=_NOW,
                scopes=["tools"],
                confidence=Confidence.MEDIUM,
                source=Source.EXPLICIT,
                body=f"memory {rid}\n",
            )
            self.store.put_memory(memory, home=home)
        else:
            from bettermemory.models import Episode

            self.store.put_episode(
                Episode(
                    id=rid,
                    session_id=session,
                    created=self.clock,
                    body=f"episode {rid}\n",
                ),
                home=home,
            )
        return rid

    def write_unresolved(
        self, loc: Location, kind: str = "memory", *, session: str | None = None
    ) -> str:
        """A write where the resolution decided nothing (decision B6)."""
        res = self.model.resolve(loc)
        assert res.outcome in (INDETERMINATE, DEFERRED) and res.home is None
        home = self.model.unresolved_home(loc, session=session)
        rid = self.put(home, kind, session or loc.session or "s")
        self.records[rid] = res
        return rid

    def brief(self, caller: Resolution, surface: str = mb.SESSION_BRIEF) -> list[str]:
        homes = self.model.homes(list(self.records))
        return sorted(
            rid for rid, home in homes.items() if admitted(surface, home, caller)
        )

    def project_of(self, rid: str) -> str:
        home = self.model.home(rid)
        assert home is not None
        return home.project

    def kinds(self, res: Resolution) -> list[str]:
        rows = {r.seq: r.kind for r in self.store.log_rows()}
        return [rows[seq] for seq in res.rows]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _file(
    directory: str, *, name: str = "", boundary: bool = False, **extra: object
) -> ProjectFileRef:
    return ProjectFileRef(
        id=_file_id(),
        name=name or directory.rsplit("/", 1)[-1],
        dir=directory,
        path=f"{directory}/bettermemory.toml",
        boundary=boundary,
        **extra,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 7.1 Individual
# ---------------------------------------------------------------------------


def test_i01_move_on_one_filesystem_keeps_the_checkout(world: World) -> None:
    repo = world.repo("/w/app")
    first = world.see(repo)
    assert first.outcome == NEW
    rid = world.write(first)
    world.move(repo, "/w/moved")
    again = world.see(repo)
    assert again.outcome == SILENT and again.rule == "object"
    assert again.checkout == first.checkout
    assert "checkout_moved" in world.kinds(again)
    assert world.brief(again) == [rid]


def test_i02_move_to_another_filesystem_joins_by_token_and_history(
    world: World,
) -> None:
    repo = world.repo("/w/app")
    first = world.see(repo)
    rid = world.write(first)
    episode = world.write(first, kind="episode")
    world.move(repo, "/vol/app", same_fs=False)
    again = world.see(repo)
    assert again.outcome == SILENT and again.rule == "join:H+T"
    assert again.checkout != first.checkout
    assert world.project_of(rid) == again.project
    assert world.brief(again) == sorted([rid, episode])
    offered = world.model.handoff(
        again, [EpisodeRef(episode, f"sess_{first.checkout}", _NOW)]
    )
    assert offered.rule == "inherited_from_non_live_checkout"
    assert offered.episodes == (episode,) and offered.label is not None
    assert "resolution_joined" in world.kinds(again)


def test_i03_commitless_with_a_remote_joins_by_token_and_remote(world: World) -> None:
    repo = world.repo("/w/u", commits=0, remote="example.invalid/acme/u")
    first = world.see(repo)
    world.move(repo, "/vol/u", same_fs=False)
    again = world.see(repo)
    assert (again.outcome, again.rule) == (SILENT, "join:R+T")
    assert again.project == first.project


def test_i04_r1_1_commitless_remoteless_moved_token_alone(world: World) -> None:
    repo = world.repo("/w/u", commits=0)
    first = world.see(repo)
    world.move(repo, "/vol/u", same_fs=False)
    again = world.see(repo)
    assert (again.outcome, again.rule) == (SILENT, "join:T")
    assert again.project == first.project


def test_i05_r1_1_copied_original_deleted_token_alone(world: World) -> None:
    repo = world.repo("/w/u", commits=0)
    first = world.see(repo)
    twin = world.copy(repo, "/vol/u")
    world.delete(repo)
    again = world.see(twin)
    assert (again.outcome, again.rule) == (SILENT, "join:T")
    assert again.project == first.project


def _orphan_over(
    world: World, source: Repo, path: str, token: str | None
) -> tuple[Repo, tuple[str, ...]]:
    """A born repository on an orphan branch: HEAD resolves to nothing,
    the source's commits on another ref."""
    orphan = world.clone(source, path)
    orphan.token = token
    assert source.head is not None
    tips = (source.head,)
    orphan.head = None
    world.disk[(orphan.machine, path)] = orphan
    return replace(orphan, head=None), tips


def test_i05a_orphan_branch_is_born_and_its_history_decides(world: World) -> None:
    template = world.repo("/w/u", commits=0)
    world.see(template)
    upstream = world.repo("/w/x", remote="example.invalid/acme/x")
    joined = world.see(upstream)
    orphan, tips = _orphan_over(world, upstream, "/w/orphan", template.token)
    res = world.see(orphan, ref_tips=tips, head=None)
    assert (res.outcome, res.rule) == (SILENT, "join:H+R")
    assert res.project == joined.project
    assert "token_superseded" in world.kinds(res)
    assert "token_reminted" in world.kinds(res)
    bare, tips = _orphan_over(world, upstream, "/w/orphan2", template.token)
    bare.remotes = set()
    res = world.see(bare, ref_tips=tips, head=None, remote_keys=frozenset())
    assert res.outcome == ASK and res.rule == "ask:one_witness"
    assert res.candidates == (joined.project,)


def test_i06_r1_1_copied_while_the_original_lives_asks(world: World) -> None:
    repo = world.repo("/w/u", commits=0)
    first = world.see(repo)
    rid = world.write(first)
    twin = world.copy(repo, "/w/u-copy")
    res = world.see(twin)
    assert res.outcome == ASK and res.rule == "ask:token_shared_no_commit"
    assert "carries the checkout token of /w/u" in res.lines[-1]
    assert world.brief(res) == []
    mine = world.write(res)
    assert world.brief(world.see(repo)) == [rid]
    assert relation(world.model.home(mine), world.see(repo)) == OTHER_PROJECT  # type: ignore[arg-type]


def test_i07_r1_1_token_denied_content_asks_naming_it(world: World) -> None:
    files = Manifest("u", (("plan.md", 12), ("notes/a.md", 40)))
    repo = world.repo("/w/u", commits=0, manifest=files, token_policy="skipped:policy")
    first = world.see(repo)
    world.move(repo, "/vol/u", same_fs=False)
    res = world.see(repo)
    assert res.outcome == ASK and res.rule == "ask:content_only"
    assert res.candidates == (first.project,)
    assert (
        "could not write /vol/u/.git/info/bettermemory: skipped:policy" in res.lines[-1]
    )


def test_i07a_empty_commitless_repositories_name_at_most_three(world: World) -> None:
    empty = Manifest("x", ())
    made: list[str] = []
    for base in ("/a", "/b", "/c"):
        repo = world.repo(
            f"{base}/x", commits=0, manifest=empty, token_policy="skipped:policy"
        )
        made.append(str(world.see(repo).project))
        world.delete(repo)
    asked = world.see(
        world.repo("/d/x", commits=0, manifest=empty, token_policy="skipped:policy")
    )
    assert asked.outcome == ASK and sorted(asked.candidates) == sorted(made)
    world.delete(world.disk[(world.machine, "/d/x")])  # type: ignore[arg-type]
    fifth = world.see(
        world.repo("/e/x", commits=0, manifest=empty, token_policy="skipped:policy")
    )
    assert fifth.outcome == NEW
    assert any("of 4 commit-less projects" in line for line in fifth.lines)
    assert any("bettermemory project join <id>" in line for line in fifth.lines)


def test_i08_copy_with_the_original_live_joins_with_its_own_run_state(
    world: World,
) -> None:
    repo = world.repo("/w/app")
    first = world.see(repo)
    episode = world.write(first, kind="episode")
    twin = world.copy(repo, "/w/app-copy")
    res = world.see(twin)
    assert (res.outcome, res.rule) == (SILENT, "join:H+T")
    assert res.checkout != first.checkout and res.project == first.project
    handoff = world.model.handoff(res, [EpisodeRef(episode, "s1", _NOW)])
    assert handoff.rule == "none" and handoff.session_id is None
    own = world.model.handoff(first, [EpisodeRef(episode, "s1", _NOW)])
    assert own.rule == "own_checkout" and own.episodes == (episode,)


def test_i09_copies_after_the_original_is_deleted_answer_alike_in_either_order(
    tmp_path: Path,
) -> None:
    answers = []
    for order in ((0, 1), (1, 0)):
        world = World(tmp_path / f"order{order[0]}")
        repo = world.repo("/w/app")
        first = world.see(repo)
        twins = [world.copy(repo, "/w/one"), world.copy(repo, "/w/two")]
        world.delete(repo)
        seen = [world.see(twins[i]) for i in order]
        assert all(r.outcome == SILENT and r.project == first.project for r in seen)
        answers.append(sorted(r.rule for r in seen))
    assert answers[0] == answers[1] == ["join:H+T", "join:H+T"]


def test_i10_remote_added_and_changed_in_place_keeps_the_checkout(world: World) -> None:
    repo = world.repo("/w/app")
    first = world.see(repo)
    repo.remotes = {"example.invalid/acme/app"}
    added = world.see(repo)
    repo.remotes = {"example.invalid/other/app"}
    changed = world.see(repo)
    assert [r.rule for r in (added, changed)] == ["object", "object"]
    assert {added.checkout, changed.checkout} == {first.checkout}
    assert changed.outcome == SILENT and world.model.pending() == []


def test_i11_commitless_moved_then_a_remote_and_a_commit(world: World) -> None:
    repo = world.repo("/w/u", commits=0)
    first = world.see(repo)
    world.move(repo, "/w/u2")
    repo.remotes = {"example.invalid/acme/u"}
    repo.commit()
    res = world.see(repo)
    assert (res.outcome, res.rule, res.checkout) == (SILENT, "object", first.checkout)


def test_i12_another_repository_at_a_reused_path_is_new_naming_the_old(
    world: World,
) -> None:
    repo = world.repo("/w/x")
    first = world.see(repo)
    world.move(repo, "/w/elsewhere")
    stranger = world.repo("/w/x")
    res = world.see(stranger)
    assert (res.outcome, res.rule) == (NEW, "new:reused_path")
    assert f"checkout {first.checkout} of" in res.lines[-1]
    assert f"bettermemory project join {first.project}" in res.lines[-1]


def test_i13_r1_4_recreated_at_the_same_inode_without_birth_is_new(
    world: World,
) -> None:
    stamps = (InitStamp("description", 5, 1, 1, 73),)
    repo = world.repo("/w/u", commits=0, birth=False, stamps=stamps)
    first = world.see(repo)
    world.delete(repo)
    again = replace(repo, token=None, stamps=(InitStamp("description", 5, 2, 2, 73),))
    world.disk[(again.machine, again.path)] = again
    res = world.see(again)
    assert res.outcome == NEW and res.checkout != first.checkout
    # Token write denied: the init stamps refute it by equality (A2).
    denied = World(world.store.path.parent.parent / "denied")
    a = denied.repo(
        "/w/u", commits=0, birth=False, stamps=stamps, token_policy="skipped:policy"
    )
    denied_first = denied.see(a)
    b = replace(a, stamps=(InitStamp("description", 5, 9, 9, 73),))
    denied.disk[(b.machine, b.path)] = b
    assert denied.see(b).checkout != denied_first.checkout


def test_i14_commitless_move_without_birth_confirmed_by_token_or_stamps(
    world: World,
) -> None:
    repo = world.repo("/w/u", commits=0, birth=False)
    first = world.see(repo)
    world.move(repo, "/w/u2")
    res = world.see(repo)
    assert (res.rule, res.checkout) == ("object+token", first.checkout)
    stamps = (InitStamp("description", 7, 1, 1, 73),)
    denied = world.repo(
        "/w/v", commits=0, birth=False, stamps=stamps, token_policy="skipped:policy"
    )
    seen = world.see(denied)
    world.move(denied, "/w/v2")
    assert world.see(denied).rule == "object+init_stamps"
    assert world.see(denied).checkout == seen.checkout


def test_i34_without_birth_the_writer_of_the_token_seen_after_its_reader_is_one_checkout(
    world: World,
) -> None:
    """I34 without birth time: sightings of a fresh git directory race to
    write its token, and the one whose capture wrote it can be resolved
    after one that read it registered the checkout with it. The token as
    written confirms the object (witness O): the same checkout, silent."""
    repo = world.repo("/w/fresh", commits=1, birth=False, with_token=True)
    reader = world.see(repo)
    assert reader.outcome == NEW
    writer = world.model.resolve(
        repo.location(token=None, token_written=repo.token, token_status="written")
    )
    assert (writer.outcome, writer.rule, writer.checkout) == (
        SILENT,
        "object+token",
        reader.checkout,
    )


def test_i15_a_plain_folder_at_the_old_path_is_outside_any_project(
    world: World,
) -> None:
    repo = world.repo("/w/app")
    first = world.see(repo)
    world.move(repo, "/w/moved")
    shell = Location(machine=world.machine, cwd="/w/app", state=OUTSIDE)
    res = world.model.resolve(shell)
    assert (res.outcome, res.project, res.rule) == (
        GLOBAL_OUTCOME,
        GLOBAL,
        "outside_repository",
    )
    assert world.see(repo).checkout == first.checkout


def test_i16_linked_worktrees_join_by_the_object_store(world: World) -> None:
    primary = world.repo("/w/app")
    first = world.see(primary)
    episode = world.write(first, kind="episode")
    linked = world.repo("/w/app-wt", commits=0, kind="linked", common_key=primary.key)
    linked.history, linked.head = primary.history, primary.head
    res = world.see(linked)
    assert (res.outcome, res.rule) == (SILENT, "common_object")
    assert res.project == first.project and res.checkout != first.checkout
    world.delete(linked)
    again = world.repo("/w/app-wt", commits=0, kind="linked", common_key=primary.key)
    again.history, again.head = primary.history, primary.head
    readded = world.see(again)
    assert readded.checkout not in (res.checkout, first.checkout)
    handoff = world.model.handoff(readded, [EpisodeRef(episode, "s1", _NOW)])
    assert handoff.session_id is None


def test_i16_without_birth_a_linked_worktree_joins_by_its_pointers(
    world: World,
) -> None:
    """Without birth time the object store's number cannot prove itself
    and a new worktree has no token of the primary's: its git directory
    under the primary's ``.git/worktrees/`` proves the link
    (``common_object+pointer``). One whose git directory sits under
    another directory's ``worktrees/`` is not linked by the number alone."""
    primary = world.repo("/w/app", birth=False)
    first = world.see(primary)
    linked = world.repo(
        "/w/app-wt", commits=0, kind="linked", common_key=primary.key, birth=False
    )
    linked.history, linked.head = primary.history, primary.head
    res = world.see(linked, git_dir="/w/app/.git/worktrees/app-wt")
    assert (res.outcome, res.rule, res.project) == (
        SILENT,
        "common_object+pointer",
        first.project,
    )
    assert res.checkout != first.checkout
    stray = world.repo(
        "/w/stray-wt", commits=0, kind="linked", common_key=primary.key, birth=False
    )
    stray.history, stray.head = primary.history, primary.head
    apart = world.see(stray, git_dir="/w/other/.git/worktrees/stray-wt")
    assert apart.rule == "ask:one_witness"


def test_i17_old_path_unreachable_joins_by_token_and_history(world: World) -> None:
    repo = world.repo("/mnt/old/app")
    first = world.see(repo)
    twin = world.copy(repo, "/w/app")
    world.disk[(world.machine, "/mnt/old/app")] = "unreachable"
    res = world.see(twin)
    assert (res.outcome, res.rule, res.project) == (SILENT, "join:H+T", first.project)


def test_i18_remount_renumbering_rekeys_and_keeps_run_state(world: World) -> None:
    repo = world.repo("/w/app")
    first = world.see(repo)
    episode = world.write(first, kind="episode")
    repo.key = world.key(device="9")
    res = world.see(repo)
    assert (res.outcome, res.rule) == (SILENT, "rekeyed:B+H+T")
    assert res.checkout == first.checkout
    assert "checkout_rekeyed" in world.kinds(res)
    handoff = world.model.handoff(res, [EpisodeRef(episode, "s1", _NOW)])
    assert handoff.rule == "own_checkout"


def test_i19_r1_5_another_owners_repository_joins_by_history_and_remote(
    world: World,
) -> None:
    mine = world.repo("/w/app", remote="example.invalid/acme/app")
    first = world.see(mine)
    theirs = world.clone(mine, "/home/other/app", token_policy="skipped:foreign_owner")
    res = world.see(theirs)
    assert (res.outcome, res.rule, res.project) == (SILENT, "join:H+R", first.project)
    assert world.model.checkout(res.checkout).token is None  # type: ignore[arg-type]


def test_i20_r1_5_an_unreadable_cwd_shows_nothing_with_a_row(world: World) -> None:
    rid = world.write(world.see(world.repo("/w/app")))
    res = world.model.resolve(
        Location(
            machine=world.machine,
            cwd="/w/locked",
            state=UNREADABLE,
            detail="permission denied",
        )
    )
    assert (res.outcome, res.project) == (INDETERMINATE, None)
    assert res.lines == (
        "bettermemory: cannot tell what /w/locked is (permission denied); nothing "
        "from any project is shown here",
    )
    assert world.kinds(res) == ["resolution_indeterminate"]
    home = world.model.home(rid)
    assert home is not None and relation(home, res) == UNKNOWN
    assert not any(admitted(s, home, res) for s in SURFACES)
    assert res.home is None


def test_i21_evicted_head_defers_first_sight_and_never_a_known_checkout(
    world: World,
) -> None:
    known = world.repo("/icloud/app")
    first = world.see(known)
    known.evicted = True
    assert world.see(known).outcome == SILENT
    fresh = world.repo("/icloud/new", evicted=True)
    deferred = world.see(fresh)
    assert (deferred.outcome, deferred.project) == (DEFERRED, None)
    assert "is not on this disk yet" in deferred.lines[-1]
    assert world.kinds(deferred) == ["resolution_deferred"]
    fresh.evicted = False
    assert world.see(fresh).outcome == NEW
    assert world.see(known).checkout == first.checkout


def test_i22_separate_git_dir_resolves_by_its_object(world: World) -> None:
    repo = world.repo("/w/tree")
    first = world.see(repo, git_dir="/elsewhere/tree.git")
    world.move(repo, "/w/tree2")
    res = world.see(repo, git_dir="/elsewhere/tree.git")
    assert (res.rule, res.checkout) == ("object", first.checkout)


def test_i23_bare_clones_join_by_history_and_remote_without_a_token(
    world: World,
) -> None:
    repo = world.repo("/w/app", remote="example.invalid/acme/app")
    first = world.see(repo)
    bare = world.clone(repo, "/srv/app.git", kind="bare")
    res = world.see(bare)
    assert (res.rule, res.project) == ("join:H+R", first.project)
    assert world.model.checkout(res.checkout).token is None  # type: ignore[arg-type]
    worktree = world.repo(
        "/w/app-bare-wt", commits=0, kind="linked", common_key=bare.key
    )
    assert world.see(worktree).rule == "common_object"


def _shallow_tip(world: World, upstream: Repo, path: str) -> Repo:
    clone = world.clone(upstream, path, shallow=True)
    clone.history = History()
    clone.head = None
    clone.commit()
    return clone


def test_i24_shallow_clone_at_a_new_tip_asks_and_is_closed_by_evidence(
    world: World,
) -> None:
    upstream = world.repo("/w/x", remote="example.invalid/acme/x")
    joined = world.see(upstream)
    shallow = _shallow_tip(world, upstream, "/ci/x")
    asked = world.see(shallow)
    assert (asked.outcome, asked.rule) == (ASK, "ask:one_witness")
    written = world.write(asked)
    assert world.brief(asked) == [written]
    shallow.history.commits.update(upstream.history.commits)
    tip = shallow.history.commits[shallow.head]  # type: ignore[index]
    shallow.history.commits[shallow.head] = replace(tip, parents=(upstream.head,))  # type: ignore[index, arg-type]
    shallow.shallow = False
    closed = world.see(shallow)
    assert closed.outcome == SILENT and closed.project == joined.project
    assert any("what was written here joined it" in line for line in closed.lines)
    answered = world.model.proposal(asked.proposal)  # type: ignore[arg-type]
    assert answered.state == "answered" and answered.answered_by == "evidence:H+R"
    assert world.project_of(written) == joined.project


def test_i25_shallow_clone_at_a_recorded_tip_or_with_the_file(world: World) -> None:
    upstream = world.repo("/w/x", remote="example.invalid/acme/x")
    upstream.files = (_file("/w/x", name="x"),)
    joined = world.see(upstream)
    at_tip = world.clone(upstream, "/ci/one", shallow=True)
    assert world.see(at_tip).rule == "join:H+R"
    with_file = _shallow_tip(world, upstream, "/ci/two")
    with_file.files = (replace(upstream.files[0], dir="/ci/two"),)
    res = world.see(with_file)
    assert (res.outcome, res.rule, res.project) == (SILENT, "join:F+R", joined.project)


def test_i26_history_rewritten_in_place_keeps_the_checkout(world: World) -> None:
    repo = world.repo("/w/app")
    first = world.see(repo)
    repo.history = History()
    repo.head = None
    repo.commit()
    res = world.see(repo)
    assert (res.rule, res.checkout) == ("object", first.checkout)


def _rewritten(world: World, upstream: Repo, path: str, *, keep: str) -> Repo:
    """A fresh clone of a rewritten history: every commit id changed,
    `keep` naming what the rewrite kept (fingerprints or file contents)."""
    clone = world.clone(upstream, path)
    clone.history = History()
    clone.head = None
    order: list[str] = []
    sha: str | None = upstream.head
    while sha:
        order.append(sha)
        parents = upstream.history.parents(sha)
        sha = parents[0] if parents else None
    for old in reversed(order):
        source = upstream.history.commits[old]
        if keep == "fingerprints":
            clone.commit(rewrite=source.fingerprint)
        else:
            clone.commit(blobs=source.blobs)
    return clone


def test_i27_fresh_clone_of_a_rewritten_history_joins_silently(world: World) -> None:
    upstream = world.repo("/w/lib", remote="example.invalid/acme/lib")
    joined = world.see(upstream)
    by_fingerprint = world.see(_rewritten(world, upstream, "/w/b", keep="fingerprints"))
    assert (by_fingerprint.rule, by_fingerprint.project) == (
        "join:Hf+R",
        joined.project,
    )
    by_contents = world.see(_rewritten(world, upstream, "/w/b2", keep="contents"))
    assert (by_contents.rule, by_contents.project) == ("join:Hb+R", joined.project)


def test_i28_rewrite_of_dates_and_contents_asks_once_and_the_file_keeps_it_silent(
    world: World,
) -> None:
    upstream = world.repo("/w/lib", remote="example.invalid/acme/lib")
    joined = world.see(upstream)
    clone = _rewritten(world, upstream, "/w/d1", keep="nothing")
    clone.history = History()
    clone.head = None
    clone.commit()
    asked = world.see(clone)
    assert asked.rule == "ask:one_witness"
    world.model.declare(
        Declaration("join", "os:tester", checkout=asked.checkout, target=joined.project)
    )
    second = world.clone(clone, "/w/d2")
    assert world.see(second).rule == "join:H+R"
    opened = [r for r in world.store.log_rows() if r.kind == "proposal_opened"]
    assert len(opened) == 1
    # With the project file committed before a rewrite that kept contents.
    filed = World(world.store.path.parent.parent / "filed")
    up = filed.repo("/w/lib", remote="example.invalid/acme/lib")
    up.files = (_file("/w/lib", name="lib"),)
    first = filed.see(up)
    kept = _rewritten(filed, up, "/w/e", keep="contents")
    kept.files = (replace(up.files[0], dir="/w/e"),)
    res = filed.see(kept)
    assert res.outcome == SILENT and F in res.rule and res.project == first.project


def test_i29_a_plain_folder_without_a_file_is_global(world: World) -> None:
    res = world.model.resolve(
        Location(machine=world.machine, cwd="/w/notes", state=OUTSIDE)
    )
    rid = world.write(res)
    home = world.model.home(rid)
    assert home is not None and home.project == GLOBAL and res.outcome == GLOBAL_OUTCOME


def _folder(
    world: World, path: str, file: ProjectFileRef, manifest: Manifest, **kw: object
) -> Location:
    return Location(
        machine=world.machine,
        cwd=path,
        state=OUTSIDE,
        root=path,
        kind=FOLDER,
        object_key=kw.pop("key", None) or world.key(),  # type: ignore[arg-type]
        files=(file,),
        manifest=manifest,
        **kw,  # type: ignore[arg-type]
    )


def test_i30_folder_project_moved_copied_and_its_file_copied_elsewhere(
    world: World,
) -> None:
    file = _file("/w/notes", name="notes")
    files = Manifest("notes", (("a.md", 10), ("b.md", 20)))
    first_loc = _folder(world, "/w/notes", file, files)
    first = world.model.resolve(first_loc)
    assert (first.outcome, first.rule) == (NEW, "new_folder")
    moved = world.model.resolve(
        replace(
            first_loc,
            cwd="/w/moved/notes",
            root="/w/moved/notes",
            files=(replace(file, dir="/w/moved/notes"),),
        )
    )
    assert (moved.rule, moved.checkout) == ("folder_object", first.checkout)
    copied = world.model.resolve(
        _folder(world, "/x/notes", replace(file, dir="/x/notes"), files)
    )
    assert (copied.outcome, copied.rule, copied.project) == (
        SILENT,
        "join:C+F",
        first.project,
    )
    elsewhere = world.model.resolve(
        _folder(
            world,
            "/y/other",
            replace(file, dir="/y/other"),
            Manifest("other", (("z", 1),)),
        )
    )
    assert (elsewhere.outcome, elsewhere.rule) == (ASK, "ask:one_witness")


def test_i30a_git_init_in_a_folder_project_rekeys_the_folder_checkout(
    world: World,
) -> None:
    file = _file("/w/notes", name="notes")
    folder_key = world.key()
    folder = world.model.resolve(
        _folder(world, "/w/notes", file, Manifest("notes", ()), key=folder_key)
    )
    rid = world.write(folder)
    repo = world.repo("/w/notes", commits=0, root_key=folder_key)
    repo.files = (file,)
    res = world.see(repo)
    assert (res.outcome, res.rule, res.checkout) == (
        SILENT,
        "rekeyed:F+O",
        folder.checkout,
    )
    assert world.brief(res) == [rid]
    repo.commit()
    assert world.see(repo).checkout == folder.checkout


def test_i31_nested_folder_projects_deliver_up_the_chain(world: World) -> None:
    outer = _file("/w/clients", name="clients")
    inner = _file("/w/clients/acme", name="acme")
    top = world.model.resolve(
        Location(
            machine=world.machine,
            cwd="/w/clients",
            state=OUTSIDE,
            root="/w/clients",
            kind=FOLDER,
            object_key=world.key(),
            files=(outer,),
        )
    )
    note = world.write(top)
    below = world.model.resolve(
        Location(
            machine=world.machine,
            cwd="/w/clients/acme",
            state=OUTSIDE,
            root="/w/clients/acme",
            kind=FOLDER,
            object_key=world.key(),
            files=(inner, outer),
        )
    )
    assert below.chain[-1] == top.project and below.project != top.project
    assert world.brief(below) == [note]
    assert world.brief(top) == [note]


def test_i35_a_synced_git_directory_is_a_checkout_per_machine(world: World) -> None:
    repo = world.repo("/Users/k/iCloud/app")
    first = world.see(repo)
    other = world.copy(repo, "/Users/k/iCloud/app", machine="M2")
    res = world.see(other, model=world.model_for("M2"))
    assert (res.outcome, res.rule, res.project) == (SILENT, "join:H+T", first.project)
    assert world.model.checkout(res.checkout).machine == "M2"  # type: ignore[arg-type]


def test_i36_a_migrated_laptop_joins_and_is_offered_the_old_session(
    world: World,
) -> None:
    repo = world.repo("/Users/k/app")
    first = world.see(repo)
    episode = world.write(first, kind="episode")
    # The new laptop has a new machine id; the repository came with the
    # migration, token and all; the old laptop is gone.
    laptop = world.model_for("M2")
    moved = world.copy(repo, "/Users/k/app", machine="M2")
    world.delete(repo)
    res = world.see(moved, model=laptop)
    assert (res.outcome, res.rule, res.project) == (SILENT, "join:H+T", first.project)
    assert res.checkout != first.checkout
    offered = laptop.handoff(res, [EpisodeRef(episode, "s-old", _NOW)])
    assert offered.rule == "inherited_from_non_live_checkout"
    assert offered.label is not None and str(first.checkout) in offered.label


@pytest.mark.parametrize("skew", [timedelta(days=-1), timedelta(0), timedelta(days=1)])
def test_i33_a_clock_a_day_off_decides_alike(tmp_path: Path, skew: timedelta) -> None:
    world = World(tmp_path)
    world.clock = _NOW + skew
    stamps = (InitStamp("description", 7, 1, 1, 73),)
    repo = world.repo(
        "/w/v", commits=0, birth=False, stamps=stamps, token_policy="skipped:policy"
    )
    first = world.see(repo)
    world.move(repo, "/w/v2")
    moved = world.see(repo)
    assert (moved.rule, moved.checkout) == ("object+init_stamps", first.checkout)
    world.delete(repo)
    again = replace(repo, stamps=(InitStamp("description", 7, 2, 2, 73),))
    world.disk[(again.machine, again.path)] = again
    assert world.see(again).checkout != first.checkout


def test_i37_a_cloned_vm_with_a_new_machine_id_is_a_new_checkout(world: World) -> None:
    repo = world.repo("/srv/app")
    first = world.see(repo)
    episode = world.write(first, kind="episode")
    clone = world.copy(repo, "/srv/app", machine="M1-clone", key=repo.key)
    model = world.model_for("M1-clone")
    res = world.see(clone, model=model)
    assert res.checkout != first.checkout and res.project == first.project
    handoff = model.handoff(res, [EpisodeRef(episode, "s1", _NOW)])
    assert handoff.rule != "own_checkout"


def test_i39_a_bind_mount_in_a_container_joins_from_the_location_token(
    world: World,
) -> None:
    host = world.repo("/w/app", remote="example.invalid/acme/app")
    first = world.see(host)
    mounted = world.copy(host, "/workspace/app", machine="container-1")
    res = world.see(mounted, model=world.model_for("container-1"))
    assert (res.rule, res.project) == ("join:H+R+T", first.project)


def test_i40_a_clone_inside_a_container_and_a_copy_the_host_cannot_see(
    world: World,
) -> None:
    host = world.repo("/w/app", remote="example.invalid/acme/app")
    first = world.see(host)
    model = world.model_for("container-2")
    inside = world.clone(host, "/src/app", machine="container-2")
    assert world.see(inside, model=model).rule == "join:H+R"
    copy = world.copy(host, "/tmp/app", machine="container-2", remotes=set())
    res = world.see(copy, model=model, remote_keys=frozenset())
    assert (res.rule, res.project) == ("join:H+T", first.project)


def test_i41_throwaway_containers_with_the_committed_file_join_silently(
    world: World,
) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    upstream.files = (_file("/w/app", name="app"),)
    first = world.see(upstream)
    for n in range(3):
        box = _shallow_tip(world, upstream, f"/box{n}/app")
        box.files = (replace(upstream.files[0], dir=f"/box{n}/app"),)
        box.machine = f"box-{n}"
        world.disk[(box.machine, box.path)] = box
        res = world.see(box, model=world.model_for(box.machine))
        assert (res.outcome, res.rule, res.project) == (
            SILENT,
            "join:F+R",
            first.project,
        )
    assert world.model.pending() == []


def test_i42_an_rc3_client_path_the_daemon_cannot_see_is_unreadable(
    world: World,
) -> None:
    res = world.model.resolve(
        Location(
            machine=world.machine,
            cwd="/workspace/app",
            state=UNREADABLE,
            detail="no location token",
        )
    )
    assert res.outcome == INDETERMINATE and "no location token" in res.lines[0]


# ---------------------------------------------------------------------------
# 7.2 Team
# ---------------------------------------------------------------------------


def test_t01_a_teammates_fresh_clone_joins_by_history_and_remote(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    first = world.see(upstream)
    rid = world.write(first)
    res = world.see(world.clone(upstream, "/home/teammate/app"))
    assert (res.outcome, res.rule) == (SILENT, "join:H+R")
    assert world.brief(res) == [rid]


def test_t02_a_fresh_clone_with_the_committed_project_file(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    upstream.files = (_file("/w/app", name="app"),)
    first = world.see(upstream)
    clone = world.clone(upstream, "/w/two")
    clone.files = (replace(upstream.files[0], dir="/w/two"),)
    res = world.see(clone)
    assert (res.outcome, res.rule, res.project) == (SILENT, "join:F+H+R", first.project)


def test_t03_a_fork_naming_its_upstream_joins(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    first = world.see(upstream)
    fork = world.clone(upstream, "/w/fork", remote="example.invalid/acme/app")
    fork.remotes.add("example.invalid/me/app")
    assert world.see(fork).project == first.project


def test_t04_a_fork_under_its_own_remote_asks_and_the_file_answers(
    world: World,
) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    first = world.see(upstream)
    rid = world.write(first)
    fork = world.clone(upstream, "/w/fork", remote="example.invalid/enterprise/app")
    asked = world.see(fork)
    assert (asked.outcome, asked.rule) == (ASK, "ask:one_witness")
    assert asked.lines[-1].startswith("bettermemory: /w/fork may be project")
    assert world.brief(asked) == []
    # The project file committed upstream and pulled into the fork: F+H
    # answers the open question by evidence.
    upstream.files = (_file("/w/app", name="app"),)
    world.see(upstream)
    fork.files = (replace(upstream.files[0], dir="/w/fork"),)
    fork.commit()
    closed = world.see(fork)
    assert closed.outcome == SILENT and closed.project == first.project
    assert world.model.proposal(asked.proposal).answered_by == "project_file"  # type: ignore[arg-type]
    assert world.brief(closed) == [rid]


def test_t05_r1_2_commits_and_remote_join_and_ask_under_strict(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    first = world.see(upstream)
    lookalike = world.clone(upstream, "/w/lookalike")
    assert world.see(lookalike).rule == "join:H+R"
    strict = world.clone(upstream, "/w/strict")
    asked = world.see(strict, strict=True)
    assert (asked.outcome, asked.rule) == (ASK, "ask:a3_history_join")
    assert asked.candidates == (first.project,)
    # Under the switch the file still joins: it is the declaration (E21 e).
    upstream.files = (_file("/w/app", name="app"),)
    world.see(upstream)
    filed = world.clone(upstream, "/w/filed")
    filed.files = (replace(upstream.files[0], dir="/w/filed"),)
    assert world.see(filed, strict=True).outcome == SILENT


def test_t06_r1_2_planted_token_or_file_and_a_file_over_another_history(
    world: World,
) -> None:
    core = world.repo("/w/core", remote="example.invalid/acme/core")
    core.files = (_file("/w/core", name="core"),)
    first = world.see(core)
    secret = world.write(first)
    # (a) a planted token in an unborn repository while core lives.
    planted = world.repo("/w/planted", commits=0, with_token=False)
    planted.token = core.token
    res = world.see(planted)
    assert res.rule == "ask:token_shared_no_commit" and world.brief(res) == []
    # (b) a planted file in a repository with its own commits, no remote.
    spoof = world.repo("/w/spoof")
    spoof.files = (replace(core.files[0], dir="/w/spoof"),)
    res = world.see(spoof)
    assert res.rule == "ask:one_witness" and secret not in world.brief(res)
    written = world.write(res)
    assert relation(world.model.home(written), world.see(core)) == OTHER_PROJECT  # type: ignore[arg-type]
    # (f) a file naming core over another project's commits.
    other = world.repo("/w/other", remote="example.invalid/acme/other")
    world.see(other)
    mixed = world.clone(other, "/w/mixed")
    mixed.files = (replace(core.files[0], dir="/w/mixed"),)
    res = world.see(mixed)
    assert res.rule == "ask:file_names_other_history"
    assert world.brief(res) == []


def test_t07_r1_3_template_siblings_ask_once_each_and_new_settles_it(
    world: World,
) -> None:
    template = world.repo("/w/template", commits=0)
    world.see(template)
    one = world.copy(template, "/w/one")
    asked = world.see(one)
    assert asked.rule == "ask:token_shared_no_commit"
    done = world.model.declare(Declaration("new", "os:tester", checkout=asked.checkout))
    assert done.remint_checkout is None
    assert any(
        world.store.log_rows()[seq - 1].kind == "token_reminted" for seq in done.rows
    )
    for _ in range(2):
        again = world.see(one)
        assert (again.outcome, again.checkout) == (SILENT, asked.checkout)
    assert world.model.project(again.project).provisional is None  # type: ignore[arg-type]
    two = world.copy(template, "/w/two")
    assert world.see(two).outcome == ASK


def test_t08_r1_3_products_made_by_init_new_stay_separate(world: World) -> None:
    template = world.repo("/w/template", remote="example.invalid/acme/template")
    first = world.see(template)
    product = world.clone(template, "/w/product")
    joined = world.see(product)
    assert joined.project == first.project
    fid = _file_id()
    world.model.declare(
        Declaration(
            "init_new",
            "os:tester",
            checkout=joined.checkout,
            file_id=fid,
            name="product",
        )
    )
    product.files = (
        ProjectFileRef(fid, "product", "/w/product", "/w/product/bettermemory.toml"),
    )
    split = world.see(product)
    assert split.project != first.project
    teammate = world.clone(
        product, "/home/t/product", remote="example.invalid/acme/template"
    )
    teammate.files = (replace(product.files[0], dir="/home/t/product"),)
    res = world.see(teammate)
    assert res.outcome == SILENT and res.project == split.project


def test_t09_r1_3_a_template_clone_keeping_its_remote_joins_the_template(
    world: World,
) -> None:
    template = world.repo("/w/template", remote="example.invalid/acme/template")
    first = world.see(template)
    assert world.see(world.clone(template, "/w/sibling")).project == first.project


def test_t10_r1_3_repointed_template_clones_ask_and_l5_after_first_sight(
    world: World,
) -> None:
    template = world.repo("/w/template", remote="example.invalid/acme/template")
    first = world.see(template)
    own = world.clone(template, "/w/own", remote="example.invalid/me/own")
    assert world.see(own).rule == "ask:one_witness"
    later = world.clone(template, "/w/later")
    joined = world.see(later)
    assert joined.project == first.project
    later.remotes = {"example.invalid/me/later"}
    res = world.see(later)
    assert res.outcome == ASK and res.checkout == joined.checkout
    assert "now names example.invalid/me/later" in res.lines[-1]
    assert "elsewhere still names example.invalid/acme/template" in res.lines[-1]
    split = world.model.declare(Declaration("new", "os:tester", checkout=res.checkout))
    assert split.project not in (None, first.project)
    assert world.see(later).project == split.project


def test_t10a_use_this_template_asks_offering_init_new(world: World) -> None:
    template = world.repo("/w/template", remote="example.invalid/acme/template")
    template.files = (_file("/w/template", name="template"),)
    first = world.see(template)
    blobs = template.history.blobs(template.head)  # type: ignore[arg-type]
    product = world.repo("/w/product", commits=0, remote="example.invalid/me/product")
    product.commit(blobs=blobs)
    product.files = (replace(template.files[0], dir="/w/product"),)
    asked = world.see(product)
    assert (asked.outcome, asked.rule) == (ASK, "ask:one_witness")
    assert asked.lines[-1].endswith(
        "separate: bettermemory project init --new (a fresh project file)"
    )
    fid = _file_id()
    world.model.declare(
        Declaration("init_new", "os:tester", checkout=asked.checkout, file_id=fid)
    )
    product.files = (
        ProjectFileRef(fid, "product", "/w/product", "/w/product/bettermemory.toml"),
    )
    mine = world.see(product)
    assert mine.outcome == SILENT and mine.project not in (first.project, None)
    clone = world.clone(product, "/home/t/product")
    clone.files = (replace(product.files[0], dir="/home/t/product"),)
    assert world.see(clone).project == mine.project
    assert world.see(template).project == first.project


def test_t11_host_migration_asks_once_per_store_and_l5_on_a_known_checkout(
    world: World,
) -> None:
    upstream = world.repo("/w/app", remote="old.invalid/acme/app")
    first = world.see(upstream)
    second_checkout = world.clone(upstream, "/w/app2")
    world.see(second_checkout)
    fresh = world.clone(upstream, "/w/fresh", remote="new.invalid/acme/app")
    asked = world.see(fresh)
    assert asked.rule == "ask:one_witness"
    world.model.declare(
        Declaration("join", "os:tester", checkout=asked.checkout, target=first.project)
    )
    again = world.clone(upstream, "/w/fresh2", remote="new.invalid/acme/app")
    assert world.see(again).rule == "join:H+R"
    upstream.remotes = {"new2.invalid/acme/app"}
    res = world.see(upstream)
    assert res.outcome == ASK and res.checkout == first.checkout
    assert sum(1 for r in world.store.log_rows() if r.kind == "proposal_opened") == 2


def test_t12_host_migration_with_the_file_is_silent(world: World) -> None:
    upstream = world.repo("/w/app", remote="old.invalid/acme/app")
    upstream.files = (_file("/w/app", name="app"),)
    first = world.see(upstream)
    moved = world.clone(upstream, "/w/fresh", remote="new.invalid/acme/app")
    moved.files = (replace(upstream.files[0], dir="/w/fresh"),)
    res = world.see(moved)
    assert (res.outcome, res.rule, res.project) == (SILENT, "join:F+H", first.project)


def test_t13_ci_shallow_runners_ask_once_then_the_declared_remote(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    first = world.see(upstream)
    run1 = _shallow_tip(world, upstream, "/runner/1/app")
    asked = world.see(run1)
    assert asked.rule == "ask:one_witness"
    world.model.declare(Declaration("join", "os:ci", checkout=asked.checkout))
    assert world.store.membership_rows("declared_remotes") == [
        {
            "remote_key": "example.invalid/acme/app",
            "project": first.project,
            "principal": "os:ci",
        }
    ]
    run2 = _shallow_tip(world, upstream, "/runner/2/app")
    res = world.see(run2)
    assert (res.outcome, res.rule, res.project) == (SILENT, "join:D+R", first.project)
    full = world.clone(upstream, "/runner/3/app")
    assert world.see(full).rule == "join:H+R"


def test_t14_ci_runners_with_the_file_or_full_clones(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    upstream.files = (_file("/w/app", name="app"),)
    first = world.see(upstream)
    run = _shallow_tip(world, upstream, "/runner/app")
    run.files = (replace(upstream.files[0], dir="/runner/app"),)
    assert world.see(run).rule == "join:F+R"
    full = world.clone(upstream, "/runner/full")
    assert world.see(full).project == first.project


def test_t15_a_cloud_clone_with_the_store_shared(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    upstream.files = (_file("/w/app", name="app"),)
    first = world.see(upstream)
    rid = world.write(first)
    cloud = world.model_for("codespace-1")
    clone = world.clone(upstream, "/workspaces/app", machine="codespace-1")
    res = world.see(clone, model=cloud)
    assert (res.outcome, res.rule, res.project) == (SILENT, "join:H+R", first.project)
    assert world.brief(res) == [rid]
    filed = world.clone(upstream, "/workspaces/filed", machine="codespace-1")
    filed.files = (replace(upstream.files[0], dir="/workspaces/filed"),)
    assert world.see(filed, model=cloud).rule == "join:F+H+R"


def test_t17_a_shared_bare_repository_cloned_by_path(world: World) -> None:
    upstream = world.repo("/w/app")
    bare = world.clone(upstream, "/srv/share/app.git", kind="bare")
    first = world.see(bare)
    clone = world.clone(
        bare, "/w/mine", kind=PRIMARY, remote="local:/srv/share/app.git"
    )
    res = world.see(clone)
    assert (res.outcome, res.rule, res.project) == (SILENT, "join:H+R", first.project)


# ---------------------------------------------------------------------------
# 7.3 Enterprise
# ---------------------------------------------------------------------------


def _client(world: World, name: str, *, boundary: bool) -> ProjectFileRef:
    return _file(
        f"/clients/{name}", name=name, boundary=boundary, object_key=world.key()
    )


def test_x01_separate_clients_are_new_each_and_meet_only_in_an_unscoped_search(
    world: World,
) -> None:
    a = world.see(world.repo("/clients/a/app"))
    b = world.see(world.repo("/clients/b/app"))
    assert (a.outcome, b.outcome) == (NEW, NEW)
    rid = world.write(a)
    home = world.model.home(rid)
    assert home is not None and relation(home, b) == OTHER_PROJECT
    assert world.brief(b) == []
    assert world.brief(b, mb.SEARCH_UNSCOPED) == [rid]


def test_x02_boundaries_keep_clients_apart_on_every_surface(world: World) -> None:
    fa, fb = _client(world, "a", boundary=True), _client(world, "b", boundary=True)
    ra = world.repo("/clients/a/app")
    ra.files = (fa,)
    a = world.see(ra)
    rb = world.repo("/clients/b/app")
    rb.files = (fb,)
    b = world.see(rb)
    rid = world.write(a)
    home = world.model.home(rid)
    assert home is not None and relation(home, b) == OTHER_TENANT
    assert not any(admitted(s, home, b) for s in SURFACES)
    outside = world.model.resolve(
        Location(machine=world.machine, cwd="/tmp", state=OUTSIDE)
    )
    assert not any(admitted(s, home, outside) for s in SURFACES)


def test_x03_one_library_under_two_clients_with_and_without_boundaries(
    tmp_path: Path,
) -> None:
    plain = World(tmp_path / "plain")
    lib = plain.repo("/clients/a/lib", remote="example.invalid/oss/lib")
    first = plain.see(lib)
    assert plain.see(plain.clone(lib, "/clients/b/lib")).project == first.project
    bounded = World(tmp_path / "bounded")
    fa, fb = _client(bounded, "a", boundary=True), _client(bounded, "b", boundary=True)
    lib = bounded.repo("/clients/a/lib", remote="example.invalid/oss/lib")
    lib.files = (fa,)
    a = bounded.see(lib)
    other = bounded.clone(lib, "/clients/b/lib")
    other.files = (fb,)
    b = bounded.see(other)
    assert b.outcome == NEW and b.project != a.project
    assert any("across a boundary; kept apart" in line for line in b.lines)


def test_x04_a_note_in_a_clients_folder_reaches_only_that_clients_repositories(
    world: World,
) -> None:
    fa = _client(world, "a", boundary=True)
    repo = world.repo("/clients/a/app")
    repo.files = (fa,)
    in_repo = world.see(repo)
    note_loc = Location(
        machine=world.machine,
        cwd="/clients/a/notes",
        state=OUTSIDE,
        root="/clients/a",
        kind=FOLDER,
        object_key=fa.object_key,
        files=(fa,),
    )
    folder = world.model.resolve(note_loc)
    assert folder.rule == "folder_object" and folder.project != GLOBAL
    note = world.write(folder)
    assert note in world.brief(in_repo)
    outside = world.model.resolve(
        Location(machine=world.machine, cwd="/tmp", state=OUTSIDE)
    )
    assert world.brief(outside, mb.SEARCH_UNSCOPED) == []
    other = world.see(world.repo("/clients/b/app"))
    assert note not in world.brief(other, mb.SEARCH_UNSCOPED)


def _monorepo(
    world: World,
) -> tuple[Repo, ProjectFileRef, ProjectFileRef, ProjectFileRef]:
    root = _file("/m", name="mono")
    pkg_a = _file("/m/pkg/a", name="a")
    pkg_b = _file("/m/pkg/b", name="b")
    repo = world.repo("/m")
    repo.files = (root,)
    return repo, root, pkg_a, pkg_b


def test_x05_monorepo_packages_get_their_own_and_the_roots_never_a_siblings(
    world: World,
) -> None:
    repo, root, pkg_a, pkg_b = _monorepo(world)
    at_root = world.see(repo)
    in_a = world.see(repo, cwd="/m/pkg/a", files=(pkg_a, root))
    in_b = world.see(repo, cwd="/m/pkg/b", files=(pkg_b, root))
    assert in_a.checkout == in_b.checkout == at_root.checkout
    assert in_a.chain == (in_a.project, at_root.project)
    root_note, a_note, b_note = (world.write(r) for r in (at_root, in_a, in_b))
    assert world.brief(in_a) == sorted([root_note, a_note])
    assert world.brief(in_b) == sorted([root_note, b_note])
    assert world.brief(at_root) == [root_note]
    assert world.brief(at_root, mb.SEARCH_AUTO_SCOPE) == sorted(
        [root_note, a_note, b_note]
    )
    home_a = world.model.home(a_note)
    assert home_a is not None and relation(home_a, at_root) == SUB_PROJECT
    assert relation(home_a, in_b) == OTHER_PROJECT


def test_x06_a_nested_file_of_another_repository_asks_l7(world: World) -> None:
    vendor_file = _file("/w/y", name="y")
    y = world.repo("/w/y")
    y.files = (vendor_file,)
    y_res = world.see(y)
    repo, root, _a, _b = _monorepo(world)
    at_root = world.see(repo)
    nested = replace(
        vendor_file, dir="/m/vendor/y", path="/m/vendor/y/bettermemory.toml"
    )
    res = world.see(repo, cwd="/m/vendor/y", files=(nested, root))
    assert res.outcome == ASK and res.project == at_root.project
    assert "/m/vendor/y/bettermemory.toml names" in res.lines[-1]
    world.model.declare(
        Declaration("join", "os:tester", checkout=res.checkout, proposal=res.proposal)
    )
    answered = world.see(repo, cwd="/m/vendor/y", files=(nested, root))
    assert answered.outcome == SILENT and answered.project == y_res.project
    # The other answer: an id of its own, a part of the monorepo.
    other = World(world.store.path.parent.parent / "other")
    y2 = other.repo("/w/y")
    y2.files = (vendor_file,)
    other.see(y2)
    mono = other.repo("/m")
    mono.files = (root,)
    root_res = other.see(mono)
    asked = other.see(mono, cwd="/m/vendor/y", files=(nested, root))
    fid = _file_id()
    done = other.model.declare(
        Declaration("init_new", "os:tester", checkout=asked.checkout, file_id=fid)
    )
    own = replace(nested, id=fid)
    part = other.see(mono, cwd="/m/vendor/y", files=(own, root))
    assert part.outcome == SILENT and part.project == done.project
    assert part.chain == (done.project, root_res.project)


def test_x07_a_policy_against_writes_leaves_no_token_and_says_so(
    world: World,
) -> None:
    upstream = world.repo(
        "/w/app", remote="example.invalid/acme/app", token_policy="skipped:policy"
    )
    first = world.see(upstream)
    assert upstream.token is None
    clone = world.clone(upstream, "/w/clone")
    res = world.see(clone)
    assert (res.outcome, res.rule, res.project) == (SILENT, "join:H+R", first.project)
    assert clone.token is None
    assert world.model.checkout(res.checkout).token_status == "skipped:policy"  # type: ignore[arg-type]


def test_x12_quiet_checkouts_are_retired_from_health_and_return(world: World) -> None:
    repo = world.repo("/w/app")
    first = world.see(repo)
    world.delete(repo)
    assert first.checkout in world.model.health().without_live_view
    world.clock = _NOW + timedelta(days=181)
    assert world.model.retire_quiet() == [first.checkout]
    assert first.checkout not in world.model.health().without_live_view
    world.disk[(repo.machine, repo.path)] = repo
    again = world.see(repo)
    assert again.checkout == first.checkout
    assert world.model.checkout(str(first.checkout)).retired is False


def test_x13_why_names_the_project_checkout_rule_and_rows(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    world.see(upstream)
    fork = world.clone(upstream, "/w/fork", remote="example.invalid/me/app")
    asked = world.see(fork)
    rid = world.write(asked)
    world.model.declare(Declaration("join", "os:tester", checkout=asked.checkout))
    why = world.model.why(rid)
    assert why.checkout == asked.checkout and why.rule == "ask:one_witness"
    kinds = [row.kind for row in why.rows]
    for kind in (
        "home_put",
        "checkout_registered",
        "proposal_opened",
        "proposal_answered",
        "project_merged",
    ):
        assert kind in kinds
    assert why.project == world.see(upstream).project
    assert "rule ask:one_witness" in why.text
    assert world.model.why(generate_ulid()).project is None


def test_x14_health_counts_every_uncertain_call(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    up = world.see(upstream)
    world.write(up)
    asked = world.see(world.clone(upstream, "/w/fork", remote="example.invalid/me/app"))
    world.model.resolve(
        Location(machine=world.machine, cwd="/x", state=UNREADABLE, detail="EACCES")
    )
    world.see(world.repo("/icloud/a", evicted=True))
    world.model.admit_recall(asked, [list(world.records)[0]])
    health = world.model.health()
    assert [p.id for p in health.open_proposals] == [asked.proposal]
    assert [p.id for p in health.provisional] == [asked.project]
    assert health.suppressed_turns == {asked.checkout: 1}
    assert (health.indeterminate, health.deferred) == (1, 1)


# ---------------------------------------------------------------------------
# Design 3.2 rung 4: several projects qualify
# ---------------------------------------------------------------------------


def _merged(world: World, a: Repo, b: Repo, path: str) -> Repo:
    """A repository whose history merges two unrelated lines, with both
    remotes."""
    assert a.head is not None and b.head is not None
    history = a.history.copy()
    history.commits.update(b.history.commits)
    history.objects |= b.history.objects
    repo = world.repo(path, commits=0)
    repo.history = history
    repo.remotes = a.remotes | b.remotes
    sha = _sha(path)
    history.commits[sha] = _Commit((a.head, b.head), "merge-" + sha[:24], ())
    repo.head = sha
    return repo


def test_rung4_several_projects_the_stronger_evidence_decides(world: World) -> None:
    a = world.repo("/w/a", remote="example.invalid/acme/a")
    a.files = (_file("/w/a", name="a"),)
    pa = world.see(a)
    b = world.repo("/w/b", remote="example.invalid/acme/b")
    pb = world.see(b)
    both = _merged(world, a, b, "/w/both")
    both.files = (replace(a.files[0], dir="/w/both", path="/w/both/bettermemory.toml"),)
    res = world.see(both)
    assert list(res.witnesses[str(pa.project)]) == [F, H, R]
    assert list(res.witnesses[str(pb.project)]) == [H, R]
    assert (res.outcome, res.rule, res.project) == (SILENT, "join:F+H+R", pa.project)
    assert not any("commits read" in line for line in res.lines)


def test_rung4_several_projects_the_most_specific_line_decides(world: World) -> None:
    template = world.repo("/w/template", remote="example.invalid/acme/template")
    first = world.see(template)
    product = world.clone(template, "/w/product")
    joined = world.see(product)
    split = world.model.declare(
        Declaration("new", "os:tester", checkout=joined.checkout, name="product")
    )
    product.commit()
    world.see(product)
    res = world.see(world.clone(product, "/w/fresh"))
    assert set(res.witnesses) == {first.project, split.project}
    assert list(res.witnesses[str(first.project)]) == list(
        res.witnesses[str(split.project)]
    )
    assert (res.outcome, res.project) == (SILENT, split.project)


def test_rung4_several_projects_on_unrelated_lines_ask_once(world: World) -> None:
    a = world.repo("/w/a", remote="example.invalid/acme/a")
    b = world.repo("/w/b", remote="example.invalid/acme/b")
    pa, pb = world.see(a), world.see(b)
    both = _merged(world, a, b, "/w/both")
    res = world.see(both)
    assert (res.outcome, res.rule) == (ASK, "ask:several_projects")
    assert set(res.candidates) == {pa.project, pb.project}
    assert res.lines[-1].startswith("bettermemory: /w/both may be")
    # Review M G2: the question says the most specific line was read and
    # not decided within the walk's bound.
    assert "descends from another's within the 512 commits read" in res.lines[-1]
    assert world.brief(res) == []


# ---------------------------------------------------------------------------
# The A3 switch (decision D8): a parameter, never read from config
# ---------------------------------------------------------------------------


def test_strict_asks_only_for_a_join_resting_on_history(world: World) -> None:
    repo = world.repo("/w/app", remote="example.invalid/acme/app")
    first = world.see(repo, strict=True)
    assert first.outcome == NEW
    # O: the same checkout, moved on one filesystem.
    world.move(repo, "/w/moved")
    assert world.see(repo, strict=True).checkout == first.checkout
    # B: the checkout re-keyed at its path still joins silently.
    repo.key = world.key(device="9")
    rekeyed = world.see(repo, strict=True)
    assert (rekeyed.outcome, rekeyed.rule) == (SILENT, "rekeyed:B+H+R+T")
    # T alone for a commit-less checkout is not history.
    bare = world.repo("/w/u", commits=0)
    seen = world.see(bare, strict=True)
    world.move(bare, "/vol/u", same_fs=False)
    moved = world.see(bare, strict=True)
    assert (moved.rule, moved.project) == ("join:T", seen.project)
    # H with R joins without the switch and asks under it.
    joined = world.see(world.clone(repo, "/w/clone"))
    assert (joined.outcome, joined.rule) == (SILENT, "join:H+R")
    asked = world.see(world.clone(repo, "/w/clone2"), strict=True)
    assert (asked.outcome, asked.rule) == (ASK, "ask:a3_history_join")
    assert asked.candidates == (first.project,)


def test_strict_keeps_a_provisional_project_from_merging_by_evidence(
    world: World,
) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    world.see(upstream)
    fork = world.clone(upstream, "/w/fork", remote="example.invalid/me/app")
    asked = world.see(fork, strict=True)
    assert asked.outcome == ASK
    fork.remotes.add("example.invalid/acme/app")
    again = world.see(fork, strict=True)
    assert again.outcome == ASK and again.proposal == asked.proposal
    assert world.model.proposal(asked.proposal).state == mb.OPEN  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Design 3.5: one membership function, a fixed table
# ---------------------------------------------------------------------------

#: The table of design 3.5, written out: (caller in a project, caller
#: outside any project). ``global_named`` (RS-F7 part 2) joins
#: ``same_project`` on the three automatic surfaces.
_TABLE: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    mb.SESSION_BRIEF: (frozenset({SAME_PROJECT, mb.GLOBAL_NAMED}), frozenset()),
    mb.PROMPT_RECALL: (frozenset({SAME_PROJECT, mb.GLOBAL_NAMED}), frozenset()),
    mb.REVIEW_STRATA: (frozenset({SAME_PROJECT, mb.GLOBAL_NAMED}), frozenset()),
    mb.SEARCH_AUTO_SCOPE: (
        frozenset({SAME_PROJECT, SUB_PROJECT, GLOBAL_RELATION, mb.GLOBAL_NAMED}),
        frozenset({GLOBAL_RELATION}),
    ),
    mb.POOL_PREFILTER: (
        frozenset({SAME_PROJECT, SUB_PROJECT, GLOBAL_RELATION, mb.GLOBAL_NAMED}),
        frozenset({GLOBAL_RELATION}),
    ),
    mb.LINKS: (
        frozenset({SAME_PROJECT, SUB_PROJECT, GLOBAL_RELATION, mb.GLOBAL_NAMED}),
        frozenset({GLOBAL_RELATION}),
    ),
    mb.SEARCH_UNSCOPED: (
        frozenset(
            {SAME_PROJECT, SUB_PROJECT, GLOBAL_RELATION, mb.GLOBAL_NAMED, OTHER_PROJECT}
        ),
        frozenset({GLOBAL_RELATION, OTHER_PROJECT}),
    ),
}


def _home(
    project: str,
    *,
    chain: tuple[str, ...] = (),
    tenant: str | None = None,
    named: frozenset[str] = frozenset(),
) -> Home:
    return Home(
        record_id="r",
        kind="memory",
        project=project,
        checkout=None,
        rule="test",
        decided="",
        recorded_project=project,
        chain=chain or ((project,) if project != GLOBAL else ()),
        tenant=tenant,
        named=named,
    )


def test_the_table_covers_every_surface() -> None:
    assert set(_TABLE) == set(SURFACES)
    with pytest.raises(ValueError):
        admitted("elsewhere", _home(GLOBAL), Resolution(SILENT, "P1", "K1", "object"))


@pytest.mark.parametrize("surface", SURFACES)
def test_every_relation_on_every_surface_follows_the_table(surface: str) -> None:
    in_project, outside = _TABLE[surface]
    caller = Resolution(SILENT, "P1", "K1", "object", chain=("P1", "W1"))
    homes = {
        SAME_PROJECT: [_home("P1"), _home("W1")],
        SUB_PROJECT: [_home("S1", chain=("S1", "P1"))],
        GLOBAL_RELATION: [_home(GLOBAL), _home(GLOBAL, named=frozenset({"P2"}))],
        mb.GLOBAL_NAMED: [
            _home(GLOBAL, named=frozenset({"P1"})),
            _home(GLOBAL, named=frozenset({"W1", "P2"})),
        ],
        OTHER_PROJECT: [_home("P2"), _home("S2", chain=("S2", "P2"))],
        OTHER_TENANT: [_home("P3", tenant="T1")],
    }
    for rel, those in homes.items():
        for home in those:
            assert relation(home, caller) == rel
            assert admitted(surface, home, caller) is (rel in in_project)
    out = Resolution(GLOBAL_OUTCOME, GLOBAL, None, "outside_repository")
    for rel, home in (
        (GLOBAL_RELATION, _home(GLOBAL)),
        (GLOBAL_RELATION, _home(GLOBAL, named=frozenset({"P1"}))),
        (OTHER_PROJECT, _home("P2")),
        (OTHER_TENANT, _home("P3", tenant="T1")),
    ):
        assert relation(home, out) == rel
        assert admitted(surface, home, out) is (rel in outside)
    for nothing in (
        Resolution(INDETERMINATE, None, None, "indeterminate"),
        Resolution(DEFERRED, None, None, "deferred:file_evicted"),
    ):
        for those in homes.values():
            for home in those:
                assert relation(home, nothing) == UNKNOWN
                assert not admitted(surface, home, nothing)


def test_a_global_record_names_projects_by_its_scopes(world: World) -> None:
    """RS-F7 part 2: a global record's ``projects:<name>`` scopes name the
    projects of that name, the name compared case-folded (scopes are
    lowercase), merges followed; the homes reader fills them in."""
    mine = world.see(world.repo("/w/WidgetDB"))
    other = world.see(world.repo("/w/other"))
    assert world.model.project(mine.project).name == "WidgetDB"  # type: ignore[arg-type]
    assert world.model.named(["projects:widgetdb", "tools"]) == {mine.project}
    assert world.model.named(["projects:", "tools", "projects:absent"]) == frozenset()
    outside = world.model.resolve(
        Location(machine=world.machine, cwd="/tmp", state=OUTSIDE)
    )
    rid = world.put(outside.home)  # type: ignore[arg-type]
    world.store.put_memory(
        world.store.load_one(rid).model_copy(update={"scopes": ["projects:widgetdb"]})
    )
    home = world.model.home(rid)
    assert home is not None and home.named == {mine.project}
    assert relation(home, mine) == mb.GLOBAL_NAMED
    assert relation(home, other) == relation(home, outside) == GLOBAL_RELATION
    assert world.model.admit_recall(mine, [rid]).delivered == (rid,)
    assert world.model.admit_recall(other, [rid]).delivered == ()


def test_a_tenants_caller_meets_its_own_tenant_only(world: World) -> None:
    fa = _client(world, "a", boundary=True)
    one = world.repo("/clients/a/one")
    one.files = (fa,)
    two = world.repo("/clients/a/two")
    two.files = (fa,)
    r1, r2 = world.see(one), world.see(two)
    assert r1.tenant == r2.tenant is not None and r1.project != r2.project
    rid = world.write(r1)
    home = world.model.home(rid)
    assert home is not None and relation(home, r2) == OTHER_PROJECT
    assert world.brief(r2, mb.SEARCH_UNSCOPED) == [rid]
    outside = world.model.resolve(
        Location(machine=world.machine, cwd="/tmp", state=OUTSIDE)
    )
    assert relation(home, outside) == OTHER_TENANT


def test_a_records_home_follows_a_merge(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    up = world.see(upstream)
    fork = world.clone(upstream, "/w/fork", remote="example.invalid/me/app")
    asked = world.see(fork)
    rid = world.write(asked)
    home = world.model.home(rid)
    assert home is not None and relation(home, up) == OTHER_PROJECT
    world.model.declare(Declaration("join", "os:tester", checkout=asked.checkout))
    home = world.model.home(rid)
    assert home is not None
    assert (home.project, home.recorded_project) == (up.project, asked.project)
    assert relation(home, world.see(upstream)) == SAME_PROJECT


# ---------------------------------------------------------------------------
# Design 3.4: proposals
# ---------------------------------------------------------------------------


def _proposal_rows(world: World) -> list[str]:
    return [r.kind for r in world.store.log_rows() if r.kind.startswith("proposal")]


def test_a_proposal_is_opened_once_per_checkout_and_question(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    world.see(upstream)
    fork = world.clone(upstream, "/w/fork", remote="example.invalid/me/app")
    asked = world.see(fork)
    assert asked.proposal is not None
    for _ in range(3):
        again = world.see(fork)
        assert (again.outcome, again.proposal) == (ASK, asked.proposal)
        assert world.model.first_output(again)[-1] == asked.lines[-1]
    assert _proposal_rows(world).count("proposal_opened") == 1
    assert [p.id for p in world.model.pending()] == [asked.proposal]
    # Another checkout asks its own question.
    other = world.see(world.clone(upstream, "/w/fork2", remote="example.invalid/me/b"))
    assert other.proposal not in (None, asked.proposal)
    assert len(world.model.pending()) == 2


def test_a_proposal_retallies_only_when_its_signature_changes(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    up = world.see(upstream)
    fork = world.clone(upstream, "/w/fork", remote="example.invalid/me/app")
    asked = world.see(fork)
    quiet = world.see(fork)
    assert "proposal_put" not in world.kinds(quiet)
    fork.commit()
    moved = world.see(fork)
    assert "proposal_put" in world.kinds(moved)
    assert world.model.proposal(asked.proposal).state == mb.OPEN  # type: ignore[arg-type]
    # The upstream remote fetched into the fork: two witnesses now agree,
    # and the evidence answers the question (L4).
    fork.remotes.add("example.invalid/acme/app")
    closed = world.see(fork)
    pr = world.model.proposal(asked.proposal)  # type: ignore[arg-type]
    assert (pr.state, pr.answered_by) == (mb.ANSWERED, "evidence:H+R")
    assert closed.project == up.project
    assert any("joins" in line or "joined" in line for line in closed.lines)
    assert world.model.pending() == []


def test_answering_new_keeps_the_checkout_its_own_and_unasked(world: World) -> None:
    upstream = world.repo("/w/app", remote="example.invalid/acme/app")
    up = world.see(upstream)
    fork = world.clone(upstream, "/w/fork", remote="example.invalid/me/app")
    asked = world.see(fork)
    done = world.model.declare(
        Declaration(
            "new", "os:tester", checkout=asked.checkout, proposal=asked.proposal
        )
    )
    assert done.project == asked.project
    pr = world.model.proposal(asked.proposal)  # type: ignore[arg-type]
    assert (pr.state, pr.answer, pr.answered_by) == (
        mb.ANSWERED,
        "new",
        "declaration:os:tester",
    )
    again = world.see(fork)
    assert again.outcome == SILENT and again.project not in (None, up.project)
    with pytest.raises(ValueError):
        world.model.declare(
            Declaration(
                "join", "os:tester", checkout=asked.checkout, proposal=asked.proposal
            )
        )


# ---------------------------------------------------------------------------
# Surfaces over the store: recall, anchoring, the handoff
# ---------------------------------------------------------------------------


def test_a_recall_suppressing_every_hit_writes_a_row(world: World) -> None:
    a = world.see(world.repo("/w/a"))
    b = world.see(world.repo("/w/b"))
    mine, theirs = world.write(a), world.write(b)
    loose = world.write(
        world.model.resolve(Location(machine=world.machine, cwd="/tmp", state=OUTSIDE))
    )
    got = world.model.admit_recall(a, [theirs, mine])
    assert got.delivered == (mine,)
    assert got.suppressed == ((theirs, OTHER_PROJECT),)
    assert not any(r.kind == "recall_suppressed" for r in world.store.log_rows())
    none = world.model.admit_recall(a, [theirs, loose, generate_ulid()])
    assert none.delivered == ()
    assert [why for _rid, why in none.suppressed] == [
        OTHER_PROJECT,
        GLOBAL_RELATION,
        "no_home",
    ]
    rows = [r for r in world.store.log_rows() if r.kind == "recall_suppressed"]
    assert len(rows) == 1 and json.loads(rows[0].payload)["checkout"] == a.checkout


def test_claims_anchor_at_the_home_checkout_and_translate_across_views(
    world: World,
) -> None:
    repo = world.repo("/w/app", remote="example.invalid/acme/app")
    first = world.see(repo)
    rid = world.write(first)
    assert world.model.anchor(rid, None) == ("/w/app", "home_checkout")
    world.move(repo, "/w/moved")
    world.see(repo)
    assert world.model.anchor(rid, None) == ("/w/moved", "home_checkout")
    assert world.model.translate("/w/app/src/x.py") == ("/w/moved/src/x.py", "checkout")
    assert world.model.translate("/elsewhere/x.py") == ("/elsewhere/x.py", "absolute")
    # The home checkout gone: a stand-in of the project, the caller's first.
    world.delete(repo)
    stand_in = world.see(world.clone(repo, "/w/other"))
    assert stand_in.project == first.project
    assert world.model.anchor(rid, stand_in) == (
        "/w/other",
        "caller_checkout_same_project",
    )
    assert world.model.translate("/w/app/src/x.py", stand_in) == (
        "/w/other/src/x.py",
        "caller_checkout_same_project",
    )
    stranger = world.see(world.repo("/w/stranger"))
    assert world.model.anchor(rid, stranger) == ("/w/other", "project_checkout")
    world.delete(world.disk[(world.machine, "/w/other")])  # type: ignore[arg-type]
    assert world.model.anchor(rid, stranger) == (None, "no_live_checkout")
    assert world.model.translate("/w/app/src/x.py", stranger) == (
        "/w/app/src/x.py",
        "unanchored",
    )
    loose = world.write(
        world.model.resolve(Location(machine=world.machine, cwd="/tmp", state=OUTSIDE))
    )
    assert world.model.anchor(loose, None) == (None, "global_record")


def test_translate_writes_the_spelling_of_the_view_it_lands_in(world: World) -> None:
    """A recorded path is rebased in the spelling of the view it lands in,
    never in the platform's: a Windows-spelled view is joined with
    backslashes on any host and a POSIX view with slashes, and a path
    recorded on a machine of one kind translates at a checkout of the
    other kind in that checkout's spelling (the model's paths cross
    machines, so the platform this runs on says nothing about them)."""
    repo = world.repo("C:\\w\\app", remote="example.invalid/acme/app")
    first = world.see(repo, git_dir="C:\\w\\app\\.git")
    rid = world.write(first)
    world.move(repo, "C:\\w\\moved")
    world.see(repo, git_dir="C:\\w\\moved\\.git")
    assert world.model.anchor(rid, None) == ("C:\\w\\moved", "home_checkout")
    assert world.model.translate("C:\\w\\app\\src\\x.py") == (
        "C:\\w\\moved\\src\\x.py",
        "checkout",
    )
    # A POSIX machine's record read at this project's Windows-spelled
    # stand-in on another machine, and the reverse.
    posix = world.repo("/w/tool", remote="example.invalid/acme/tool")
    recorded = world.write(world.see(posix))
    other = world.model_for("M2")
    twin = world.see(
        world.clone(posix, "D:\\src\\tool", machine="M2"),
        model=other,
        git_dir="D:\\src\\tool\\.git",
    )
    assert other.anchor(recorded, twin) == (
        "D:\\src\\tool",
        "caller_checkout_same_project",
    )
    assert other.translate("/w/tool/src/y.py", twin) == (
        "D:\\src\\tool\\src\\y.py",
        "caller_checkout_same_project",
    )
    back = world.see(world.clone(repo, "/home/kim/app"))
    assert world.model.translate("C:\\w\\app\\src\\x.py", back) == (
        "C:\\w\\moved\\src\\x.py",
        "checkout",
    )
    world.delete(repo)
    assert world.model.translate("C:\\w\\app\\src\\x.py", back) == (
        "/home/kim/app/src/x.py",
        "caller_checkout_same_project",
    )


def test_an_ask_line_spells_the_token_file_as_the_git_directory_is_spelled(
    world: World,
) -> None:
    """The token file an ask line names sits under the git directory in
    that directory's own spelling: a Windows-spelled git directory gives
    a Windows-spelled file on any host (I07, R1.1)."""
    files = Manifest("u", (("plan.md", 12), ("notes/a.md", 40)))
    repo = world.repo(
        "C:\\w\\u", commits=0, manifest=files, token_policy="skipped:policy"
    )
    first = world.see(repo, git_dir="C:\\w\\u\\.git")
    world.move(repo, "D:\\vol\\u", same_fs=False)
    res = world.see(repo, git_dir="D:\\vol\\u\\.git")
    assert res.outcome == ASK and res.rule == "ask:content_only"
    assert res.candidates == (first.project,)
    assert (
        "could not write D:\\vol\\u\\.git\\info\\bettermemory: skipped:policy"
        in res.lines[-1]
    )


def test_the_handoff_never_takes_a_live_siblings_run_state(world: World) -> None:
    repo = world.repo("/w/app")
    first = world.see(repo)
    sibling = world.see(world.copy(repo, "/w/copy"))
    assert sibling.project == first.project and sibling.checkout != first.checkout
    episode = world.write(first, kind="episode")
    refs = [EpisodeRef(episode, f"sess_{first.checkout}", _NOW)]
    assert world.model.handoff(sibling, refs).rule == "none"
    assert world.model.handoff(first, refs).rule == "own_checkout"
    world.delete(repo)
    offered = world.model.handoff(sibling, refs)
    assert offered.rule == "inherited_from_non_live_checkout"
    assert offered.episodes == (episode,)
    assert offered.label is not None and str(first.checkout) in offered.label
    # Another machine's checkout is never live from here.
    other = world.model_for("M2")
    there = world.see(world.copy(repo, "/w/app", machine="M2"), model=other)
    assert other.handoff(there, refs).rule in (
        "inherited_from_non_live_checkout",
        "none",
    )


# ---------------------------------------------------------------------------
# Decision B6: writes where nothing could be read
# ---------------------------------------------------------------------------


def _unreadable(world: World, cwd: str, session: str | None) -> Location:
    return Location(
        machine=world.machine,
        cwd=cwd,
        state=UNREADABLE,
        detail="EACCES",
        session=session,
    )


def _invisible_everywhere(world: World, rid: str, *callers: Resolution) -> None:
    home = world.model.home(rid)
    assert home is not None and home.unresolved
    outside = world.model.resolve(
        Location(machine=world.machine, cwd="/tmp", state=OUTSIDE)
    )
    for caller in (*callers, outside):
        for surface in SURFACES:
            assert not admitted(surface, home, caller), (surface, caller.rule)


def test_b6_a_write_under_each_state_gets_a_provisional_home(world: World) -> None:
    loc = _unreadable(world, "/w/app", "s1")
    rid = world.write_unresolved(loc, session="s1")
    home = world.model.home(rid)
    assert home is not None and home.rule == "unresolved:indeterminate"
    k = world.model.checkout(str(home.checkout))
    assert (k.kind, k.sessions, k.replaced_by) == (mb.UNRESOLVED, ("s1",), None)
    assert [v.path for v in k.views] == ["/w/app"]
    p = world.model.project(home.project)
    assert p.provisional == INDETERMINATE and p.merged_into is None
    registered = [
        json.loads(r.payload)
        for r in world.store.log_rows()
        if r.kind == "checkout_registered"
    ]
    assert registered[-1]["checkout_kind"] == mb.UNRESOLVED
    assert registered[-1]["rule"] == "unresolved:indeterminate"
    minted = [
        json.loads(r.payload)
        for r in world.store.log_rows()
        if r.kind == "project_minted"
    ]
    assert minted[-1]["reason"] == mb.UNRESOLVED_LOCATION
    # Every later write at that path finds the same checkout; a second
    # session is added to it.
    again = world.write_unresolved(_unreadable(world, "/w/app", "s2"), session="s2")
    other = world.model.home(again)
    assert other is not None and other.checkout == home.checkout
    assert world.model.checkout(str(home.checkout)).sessions == ("s1", "s2")
    # Deferred: an evicted HEAD at first sight.
    evicted = world.repo("/icloud/notes", evicted=True)
    deferred = world.write_unresolved(evicted.location(session="s3"), session="s3")
    dhome = world.model.home(deferred)
    assert dhome is not None and dhome.rule == "unresolved:deferred"
    assert world.model.project(dhome.project).provisional == DEFERRED
    # A path that was read takes its resolution's home.
    with pytest.raises(ValueError):
        world.model.unresolved_home(world.repo("/w/read").location(), session="s1")
    elsewhere = world.see(world.repo("/w/other"))
    _invisible_everywhere(world, rid, elsewhere)
    assert world.model.health().provisional == ()


def test_b6_the_same_session_merges_the_writes_at_the_next_prompt(
    world: World,
) -> None:
    rid = world.write_unresolved(_unreadable(world, "/w/app/src", "s1"), session="s1")
    episode = world.write_unresolved(
        _unreadable(world, "/w/app/src", "s1"), "episode", session="s1"
    )
    before = world.model.home(rid)
    assert before is not None
    repo = world.repo("/w/app")
    res = world.see(repo, cwd="/w/app/src", session="s1")
    assert res.outcome == NEW and res.checkout is not None
    merged = [
        json.loads(r.payload)
        for r in world.store.log_rows()
        if r.kind == "project_merged"
    ]
    assert merged[-1] == {
        "project": before.project,
        "into": res.project,
        "evidence": "written while the path could not be read; the same "
        "session resolved it at the next prompt",
    }
    settled = world.model.checkout(str(before.checkout))
    assert settled.replaced_by == res.checkout
    after = world.model.home(rid)
    assert after is not None and not after.unresolved
    assert (after.project, after.recorded_project, after.rule) == (
        res.project,
        before.project,
        "unresolved:indeterminate",
    )
    assert world.brief(res) == sorted([rid, episode])
    assert world.model.pending() == [] and world.model.health().unresolved == ()
    # The path unreadable again later: a new unresolved checkout.
    later = world.write_unresolved(_unreadable(world, "/w/app/src", "s9"), session="s9")
    fresh = world.model.home(later)
    assert fresh is not None and fresh.checkout != before.checkout


def _asked(
    world: World, session: str | None
) -> tuple[Resolution, Resolution, str, str]:
    """A known checkout at /w/app, writes there while it could not be
    read by session s1, then a sighting by `session`."""
    repo = world.repo("/w/app")
    first = world.see(repo, session="s0")
    rid = world.write_unresolved(_unreadable(world, "/w/app", "s1"), session="s1")
    episode = world.write_unresolved(
        _unreadable(world, "/w/app", "s1"), "episode", session="s1"
    )
    res = world.see(repo, session=session)
    return first, res, rid, episode


@pytest.mark.parametrize("session", ["s2", None])
def test_b6_another_session_meets_one_legacy_question(
    world: World, session: str | None
) -> None:
    first, res, rid, episode = _asked(world, session)
    assert res.outcome == ASK and res.checkout == first.checkout
    assert res.proposal is not None
    home = world.model.home(rid)
    assert home is not None and home.unresolved
    expected = (
        "bettermemory: the records written at /w/app while it could not be "
        "read (1 memories, 1 episodes) may belong to project "
        f'"app" ({first.project}) here. Same project, when the user says so: '
        f'memory_admin(action="answer_question", id="{res.proposal}", answer="join", '
        f'target="{first.project}", said="<the user\'s words>"); a project of '
        f'their own: memory_admin(action="answer_question", id="{res.proposal}", '
        'answer="separate")'
    )
    assert res.lines[-1] == expected
    pending = world.model.pending()
    assert [(p.kind, p.checkout, p.reason) for p in pending] == [
        (mb.LEGACY, first.checkout, mb.UNRESOLVED_LOCATION)
    ]
    _invisible_everywhere(world, rid, res)
    # Seen again: the same question named again, not opened again.
    again = world.see(world.disk[(world.machine, "/w/app")], session="s3")  # type: ignore[arg-type]
    assert (again.outcome, again.proposal) == (ASK, res.proposal)
    assert again.lines.count(expected) == 1
    assert world.model.first_output(again).count(expected) == 1
    assert len(world.model.pending()) == 1
    assert [r.kind for r in world.store.log_rows()].count("proposal_opened") == 1
    health = world.model.health()
    assert [(u.project, u.memories, u.episodes, u.path) for u in health.unresolved] == [
        (home.project, 1, 1, "/w/app")
    ]
    assert [u.state for u in health.unresolved] == [INDETERMINATE]
    assert health.provisional == ()
    assert home.checkout not in health.without_live_view


def test_b6_answering_join_merges_the_writes(world: World) -> None:
    first, res, rid, episode = _asked(world, "s2")
    done = world.model.declare(
        Declaration("join", "os:tester", checkout=res.checkout, target=first.project)
    )
    assert done.project == first.project
    home = world.model.home(rid)
    assert home is not None and home.project == first.project and not home.unresolved
    assert world.brief(world.see(world.disk[(world.machine, "/w/app")])) == sorted(  # type: ignore[arg-type]
        [rid, episode]
    )
    assert world.model.pending() == [] and world.model.health().unresolved == ()
    settled = world.model.checkout(str(home.checkout))
    assert settled.replaced_by == res.checkout


def test_b6_answering_new_makes_the_writes_a_project_of_their_own(
    world: World,
) -> None:
    first, res, rid, episode = _asked(world, "s2")
    pr = world.model.pending()[0]
    done = world.model.declare(
        Declaration("new", "os:tester", checkout=res.checkout, proposal=pr.id)
    )
    home = world.model.home(rid)
    assert home is not None and done.project == home.project != first.project
    p = world.model.project(home.project)
    assert p.provisional is None and not home.unresolved
    assert world.model.live_view(world.model.checkout(str(home.checkout))) is None
    later = world.see(world.disk[(world.machine, "/w/app")], session="s4")  # type: ignore[arg-type]
    assert relation(home, later) == OTHER_PROJECT
    assert world.brief(later) == []
    assert world.brief(later, mb.SEARCH_UNSCOPED) == sorted([rid, episode])
    assert world.model.pending() == [] and world.model.health().unresolved == ()
    proposal = world.model.proposal(pr.id)
    assert (proposal.state, proposal.answer) == (mb.ANSWERED, "new")


def test_b6_another_machine_settles_the_writes_only_by_their_session(
    world: World,
) -> None:
    """Decisions B6 and B24: a resolution at the path on another machine
    by another session never settles the writes; the session that wrote
    there merges them from any machine, the session proving the
    namespace."""
    rid = world.write_unresolved(_unreadable(world, "/w/app", "s1"), session="s1")
    there = world.model_for("M2")
    repo = world.repo("/w/app", machine="M2")
    res = world.see(repo, model=there, session="s2")
    assert res.outcome == NEW
    assert world.model.pending() == []
    assert len(world.model.health().unresolved) == 1
    home = world.model.home(rid)
    assert home is not None and home.unresolved
    again = world.see(repo, model=there, session="s1")
    assert (again.outcome, again.checkout) == (SILENT, res.checkout)
    after = world.model.home(rid)
    assert after is not None and not after.unresolved
    assert after.project == res.project
    assert world.model.pending() == [] and world.model.health().unresolved == ()


def _write_without_machine(world: World, cwd: str, session: str) -> str:
    """A write by a process that could not open this machine's state
    directory (decision B16): its location keyed on `location.NO_MACHINE`,
    nothing readable there, homed by `unresolved_home`."""
    blind = Location(
        machine=NO_MACHINE,
        cwd=cwd,
        state=UNREADABLE,
        detail="this machine's state directory could not be opened",
        session=session,
    )
    machineless = world.model_for(NO_MACHINE)
    assert machineless.resolve(blind).outcome == INDETERMINATE
    home = machineless.unresolved_home(blind, session=session)
    rid = world.put(home, "memory", session)
    world.records[rid] = machineless.resolve(blind)
    k = world.model.checkout(str(home.checkout))
    assert (k.machine, k.kind, k.sessions) == (NO_MACHINE, mb.UNRESOLVED, (session,))
    return rid


def test_b16_a_write_without_a_machine_merges_when_its_session_resolves_there(
    world: World,
) -> None:
    rid = _write_without_machine(world, "/w/app", "s1")
    before = world.model.home(rid)
    assert before is not None and before.unresolved
    res = world.see(world.repo("/w/app"), session="s1")
    assert res.outcome == NEW and res.checkout is not None
    after = world.model.home(rid)
    assert after is not None and not after.unresolved
    assert after.project == res.project
    assert world.brief(res) == [rid]
    settled = world.model.checkout(str(before.checkout))
    assert settled.replaced_by == res.checkout
    merged = [
        json.loads(r.payload)
        for r in world.store.log_rows()
        if r.kind == "project_merged"
    ]
    assert merged[-1]["project"] == before.project
    assert merged[-1]["into"] == res.project
    assert world.model.pending() == [] and world.model.health().unresolved == ()


@pytest.mark.parametrize("answer", ["join", "new"])
def test_b16_a_write_without_a_machine_asks_another_session(
    world: World, answer: str
) -> None:
    repo = world.repo("/w/app")
    first = world.see(repo, session="s0")
    rid = _write_without_machine(world, "/w/app", "s1")
    res = world.see(repo, session="s2")
    assert res.outcome == ASK and res.checkout == first.checkout
    expected = (
        "bettermemory: the records written at /w/app while it could not be "
        "read (1 memories, 0 episodes) may belong to project "
        f'"app" ({first.project}) here. Same project, when the user says so: '
        f'memory_admin(action="answer_question", id="{res.proposal}", answer="join", '
        f'target="{first.project}", said="<the user\'s words>"); a project of '
        f'their own: memory_admin(action="answer_question", id="{res.proposal}", '
        'answer="separate")'
    )
    assert res.lines[-1] == expected
    [pr] = world.model.pending()
    assert (pr.kind, pr.checkout, pr.reason) == (
        mb.LEGACY,
        first.checkout,
        mb.UNRESOLVED_LOCATION,
    )
    assert world.brief(res) == []
    if answer == "join":
        world.model.declare(
            Declaration("join", "os:tester", checkout=res.checkout, proposal=pr.id)
        )
        home = world.model.home(rid)
        assert home is not None and home.project == first.project
        assert world.brief(world.see(repo, session="s3")) == [rid]
    else:
        world.model.declare(
            Declaration("new", "os:tester", checkout=res.checkout, proposal=pr.id)
        )
        home = world.model.home(rid)
        assert home is not None and home.project != first.project
        assert not home.unresolved
        later = world.see(repo, session="s3")
        assert world.brief(later) == []
        assert world.brief(later, mb.SEARCH_UNSCOPED) == [rid]
    assert world.model.pending() == [] and world.model.health().unresolved == ()


def test_b17_the_model_decides_where_an_init_was_made(world: World) -> None:
    """Decision B17: the model reads an ``init`` declaration's folder
    against the checkout's newest view here: at the root the file names
    the checkout's project; below it a part of the enclosing project (the
    checkout's, with no other named), the checkout keeping its own, and
    the folder's first sight resolves to the part; a folder the view does
    not hold is refused."""
    repo = world.repo("/w/mono")
    root = world.see(repo)
    assert root.checkout is not None and root.project is not None
    k = world.model.checkout(root.checkout)
    assert world.model.below_root(k, "/w/mono") is False
    assert world.model.below_root(k, "/w/mono/") is False
    assert world.model.below_root(k, "/w/mono/packages/api") is True
    assert world.model.below_root(k, "/w/mono-two") is None
    assert world.model.below_root(k, "/w") is None
    fid = _file_id()
    done = world.model.declare(
        Declaration(
            "init",
            "os:tester",
            checkout=root.checkout,
            file_id=fid,
            name="api",
            directory="/w/mono/packages/api",
        )
    )
    assert done.project not in (None, root.project)
    assert world.model.project(str(done.project)).parent == root.project
    assert world.model.checkout(root.checkout).project == root.project
    with pytest.raises(ValueError, match="cannot tell where /w/elsewhere stands"):
        world.model.declare(
            Declaration(
                "init",
                "os:tester",
                checkout=root.checkout,
                file_id=_file_id(),
                directory="/w/elsewhere",
            )
        )
    at_root = world.model.declare(
        Declaration(
            "init",
            "os:tester",
            checkout=root.checkout,
            file_id=_file_id(),
            directory="/w/mono/",
        )
    )
    assert at_root.project == root.project
    api = _file("/w/mono/packages/api", name="api")
    api = replace(api, id=fid)
    part = world.see(repo, cwd="/w/mono/packages/api", files=(api,))
    assert (part.outcome, part.project, part.checkout) == (
        SILENT,
        done.project,
        root.checkout,
    )
    assert part.chain == (done.project, root.project)
