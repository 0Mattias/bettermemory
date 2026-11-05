"""A git directory a host and a VM-backed container share (ENG-215).

A re-mint names the lineage its writer is given: the checkout's token and
its git directory's device and inode number as the store records them for
the writer's machine (`checkout_token.successor`, ENG-205). A Linux bind
mount shows host and container one git directory at one device and inode
number, so either machine's record reads the other's re-mint as its own.
A share a VM-backed runtime makes (virtiofs under Docker Desktop, podman
machine or OrbStack; gRPC FUSE) shows them other numbers, and the
container no birth time: measured on OrbStack 2026-11-04, a git directory
at device 16777234 inode 410511702 on the macOS host was device 36 inode
11818 in the container, with no birth time there, and every file's mtime
and ctime the host's to the nanosecond. So a record without birth time did
not read the other machine's re-mint as its own: the host's next sighting,
without birth time on its filesystem, registered a second checkout of its
git directory, joined to the project the container's checkout was declared
into; and the container's, after the host's re-mint, a second container
checkout, its token re-minted once more.

A record now reads a token neither it records nor names its own lineage
as its re-mint where another machine's record of the same git directory
made or recorded it (`membership_shared`): its init stamps read there now
(the object is the recorded git directory), the other record shares an
init stamp with it by name, mtime, ctime and size (it saw the same files,
where a copy has ctimes of its own), and the token is a re-mint of its
token over that record's numbers, or the token that record holds; or,
the files' ctimes moved since, its stamps read there without them and
that record read them as they are now. A record's stamps follow its
files at each sighting its birth time or token proves; B reads such a
token as the bind mount does; a linked worktree is stamped by its
`commondir`; and a second checkout an earlier release registered for want
of the rule, which no one chose, yields to the first where nothing was
written at it, and is asked about where something was. A copy of the git
directory, the token
of a re-mint in a copy carried into the checkout, a copy landed on the
checkout's freed inode number that no record read, a token of another
line and a re-mint naming nothing are refused as before.

The container is played on this host (`Shared`): it posts made-up device
and inode numbers for every object and for its init stamps, keeping their
times and sizes, and no birth time, as the probe measured.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from bettermemory import checkout_token, location, location_git, membership_shared
from bettermemory.declare import declaring_model
from bettermemory.location import capture, remint_answer
from bettermemory.membership import ASK, SILENT, Declaration, Membership
from bettermemory.membership_records import (
    DUPLICATE_CHECKOUT,
    FILE_CONTRADICTION,
    LEGACY,
    LEGACY_SEPARATE,
    REMOTE_DIVERGED,
    SUBPROJECT_CONTRADICTION,
)
from bettermemory.membership_shared import _same_file
from bettermemory.store import Store

from .test_membership_remint import (
    Client,
    _file_token,
    _questions,
    _reminted_in,
)
from .test_membership_resolution import (
    Lab,
    _container_machine,
    copy,
    git,
    lab,
    lab_of,
    make_repo,
    neutral_git,
    no_birth,
    posing,
    pytestmark,
)

__all__ = ["lab", "lab_of", "neutral_git", "no_birth", "pytestmark"]


@dataclass
class Shared(Client):
    """A container over a VM-backed share of `real`: the objects of
    `Container` (another device and inode number, no birth time), and the
    init stamps' inode numbers its own too, their times and sizes the
    host's."""

    def posted(self, raw: dict[str, Any]) -> dict[str, Any]:
        raw = super().posted(raw)
        raw["stamps"] = [
            [s[0], 7_000_000 + i, *s[2:]] for i, s in enumerate(raw["stamps"])
        ]
        return raw


def _shared(tmp_path: Path, lab: Lab, name: str) -> tuple[Path, Shared, str, str]:
    """A repository on this host, seen here first, then through a share in
    a container: the repository, the container, the host's checkout and the
    container's, both of the host's project."""
    line = make_repo(tmp_path / "host" / name, commits=2)
    host = lab.write("h1", line)
    box = Shared(line, f"/workspaces/{name}", _container_machine(lab, name))
    joined = box.post(lab)
    assert host.checkout is not None and joined.checkout is not None
    assert joined.project == host.project and joined.checkout != host.checkout
    return line, box, host.checkout, joined.checkout


def _the_containers_re_mint(lab: Lab, box: Client, kid: str, store: str) -> None:
    """The container re-mints the token of its checkout `kid`, naming the
    lineage the store records for it (its own device and inode number):
    asked in the exchange after a declaration separated the checkout, the
    store recording it (``recorded``), or with the exchange ended after the
    write, the store never told (``missed``)."""
    if store == "recorded":
        declaring_model(lab.store, lab.config).declare(
            Declaration("new", "os:tester", checkout=kid, name="box line")
        )
        again = box.post(lab)
        assert again.checkout == kid and _reminted_in(lab, again)
        return
    lineage = lab.model().checkout(kid).lineage
    assert lineage is not None
    cap = capture(str(box.real), write_token=False, use_cache=False)
    wrote = remint_answer(
        cap, {"remint": True, "replacing": list(lineage), "write_token": True}
    )
    assert wrote["status"] == "written"


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("store", ["recorded", "missed"])
def test_a_host_without_birth_reads_a_containers_re_mint_as_its_own(
    tmp_path: Path, lab: Lab, store: str
) -> None:
    """The failing case ENG-215 names: a host whose filesystem gives no
    birth time shares its repository into a VM-backed container, and the
    container re-mints the token, the store recording the re-mint or not.
    The host's next sighting is its own checkout, silent, in its own
    project, and records the token: no second checkout, no question, and
    its memories still delivered there. Before, it registered a second
    checkout of its git directory, joined (T+H) to the container's
    declared project, where its memories were not delivered."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    token = _file_token(line)

    _the_containers_re_mint(lab, box, kid, store)

    new = _file_token(line)
    assert new not in (None, token)
    # The re-mint names the container's lineage, not the host's.
    assert not checkout_token.succeeds(new, lab.model().checkout(hid).lineage)
    asked = _questions(lab)
    location.forget()

    res, admitted = lab.view(line)

    assert (res.checkout, res.outcome, res.rule) == (hid, SILENT, "object+remint")
    assert res.project == lab.project_of("h1") and admitted == ["h1"]
    assert lab.model().checkout(hid).token == new
    mine = [k for k in lab.model().checkouts() if k.machine == lab.identity.id]
    assert [k.id for k in mine] == [hid]
    assert _questions(lab) == asked


def test_a_container_without_birth_reads_the_hosts_re_mint_as_its_own(
    tmp_path: Path, lab: Lab
) -> None:
    """The other way, the shape of a macOS host and OrbStack or Docker
    Desktop: the container reports no birth time, and the host separates
    its checkout (`project new`), re-minting the token in process with the
    host's lineage. The container's next sighting is its own checkout,
    silent, records the host's token and writes none. Before, its token
    superseded by the history, it registered a second container checkout
    and re-minted the token once more."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    token = _file_token(line)
    project = lab.model().checkout(kid).project
    declared = lab._declaring().declare(
        Declaration("new", "os:tester", checkout=hid, name="host line")
    )
    new = _file_token(line)
    assert new not in (None, token) and declared.project != project
    assert lab.model().checkout(hid).token == new
    assert not checkout_token.succeeds(new, lab.model().checkout(kid).lineage)
    asked = _questions(lab)

    again = box.post(lab)

    # Each machine's record keeps its own project, as over a bind mount.
    assert (again.checkout, again.project, again.outcome, again.rule) == (
        kid,
        project,
        SILENT,
        "object+remint",
    )
    assert not _reminted_in(lab, again)
    assert lab.model().checkout(kid).token == new == _file_token(line)
    mine = [k for k in lab.model().checkouts() if k.machine == box.identity.id]
    assert [k.id for k in mine] == [kid]
    assert _questions(lab) == asked


def _carried_in(src: Path, dst: Path) -> None:
    """`src`'s checkout token written over `dst`'s, as a sync of a copy's
    `.git/info` back into the checkout carries it."""
    body = checkout_token.token_path(src / ".git").read_bytes()
    checkout_token.token_path(dst / ".git").write_bytes(body)


@pytest.mark.usefixtures("no_birth")
def test_a_re_mint_in_a_copy_carried_into_the_checkout_is_not_the_hosts(
    tmp_path: Path, lab: Lab
) -> None:
    """The container works on a copy of the host's repository (an image's
    `COPY` with `.git`), not on a share. Its checkout is separated and
    re-minted there, and the re-mint's token is carried into the host's
    git directory (a sync of `.git/info`). The copy's init stamps have
    ctimes of their own, so its record saw other files than the host's:
    the host does not read the token as its re-mint."""
    line = make_repo(tmp_path / "host" / "line", commits=2)
    host = lab.write("h1", line)
    hid = host.checkout
    assert hid is not None
    other = copy(line, tmp_path / "box" / "copy")
    box = Shared(other, "/workspaces/copy", _container_machine(lab, "copy"))
    joined = box.post(lab)
    assert joined.checkout is not None and joined.project == host.project
    _the_containers_re_mint(lab, box, joined.checkout, "recorded")
    _carried_in(other, line)
    found = _file_token(line)
    assert found is not None
    location.forget()

    res, _admitted = lab.view(line)

    assert res.rule != "object+remint" and res.checkout != hid
    assert lab.model().checkout(hid).token != found


@pytest.mark.parametrize("birth", ["birth", "no_birth"])
def test_a_copy_on_the_freed_number_is_asked_about_either_way(
    tmp_path: Path, lab: Lab, birth: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container shares the host's git directory, is separated and
    re-mints it; then the host's repository is replaced at its path by a
    copy (`cp -a`) whose git directory lands on the freed inode number
    (E15), its stamped files on numbers of their own. The copy holds the
    container's re-mint, and its stamps are not the host record's: never
    the host's checkout, and none of the host's memories delivered. It is
    asked about, with birth time (the copy's own) and without: B finds
    the host's checkout at the path, its object another or the same
    numbers with nothing to prove them the recorded git directory, and T
    the container's project (``ask:several_projects``). Before, without
    birth time B passed over a checkout at the same numbers, and the copy
    joined the container's project silently (``join:H+T``)."""
    if birth == "no_birth":
        monkeypatch.setattr(location, "REPORT_BIRTH", False)
        location.forget()
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    first = lab.model().checkout(hid).project
    _the_containers_re_mint(lab, box, kid, "recorded")
    found = _file_token(line)
    project = lab.model().checkout(kid).project
    freed = location.object_key(str(line / ".git"))
    assert found is not None and freed is not None
    os.rename(line, tmp_path / "host" / "line-old")
    copy(tmp_path / "host" / "line-old", line)
    own = location.object_key(str(line / ".git"))
    assert own is not None and _file_token(line) == found
    if birth == "birth" and own[2] is None:
        pytest.skip("no birth time here: BETTERMEMORY_TEST_NO_BIRTH, or the disk")
    location.forget()

    with posing(line / ".git", (freed[0], freed[1], own[2])):
        res, admitted = lab.view(line)

    assert res.checkout not in (None, hid) and admitted == []
    assert (res.rule, res.outcome) == ("ask:several_projects", ASK)
    assert res.proposal is not None
    asked = lab.model().proposal(res.proposal).candidates
    assert {lab.model().canonical(c) for c in asked} == {first, project}
    assert lab.model().checkout(hid).token != found


@pytest.mark.usefixtures("no_birth")
def test_a_copy_on_the_freed_number_in_one_project_is_the_recorded_checkout(
    tmp_path: Path, lab: Lab
) -> None:
    """The same copy on the freed number without birth time, where the
    container re-minted without leaving the host's project (the store
    missed the re-mint, and its next sighting recorded it): B, H and T
    all name that one project, so B stands alone and the sighting joins
    the host's checkout as the same object (B20, ``same_object``),
    silently, as a copy at another number re-keys it with birth time.
    Before, B passed over the same numbers and a second checkout was
    registered in that project (``join:H+T``)."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    _the_containers_re_mint(lab, box, kid, "missed")
    assert box.post(lab).checkout == kid
    found = _file_token(line)
    freed = location.object_key(str(line / ".git"))
    assert found is not None and freed is not None
    os.rename(line, tmp_path / "host" / "line-old")
    copy(tmp_path / "host" / "line-old", line)
    location.forget()

    with posing(line / ".git", freed):
        res, admitted = lab.view(line)

    assert (res.checkout, res.outcome) == (hid, SILENT)
    assert res.rule == "same_object:B+H+T" and admitted == ["h1"]


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("seen", ["unseen", "seen_by_the_container"])
def test_a_copy_on_reused_numbers_passes_only_where_the_other_side_read_it(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, seen: str
) -> None:
    """Where inode numbers are reused (ext4, an NFS export; posed here, as
    APFS never reuses one), a copy of the host's repository that keeps
    mtimes and sizes (`cp -a`) can land at the path with its git directory
    and its stamped files on the freed numbers, so the host record's
    stamps read there but for their ctimes. The container re-minted the
    original over the share. Where the container has not read the copy
    (``unseen``), no record of it holds the copy's ctimes: not the host's
    checkout, and asked about, B finding the host's checkout at the same
    numbers and T the container's project (``ask:several_projects``;
    before, B passed over the same numbers and the copy joined the
    container's project silently). Where the container's record read the
    copy since (``seen_by_the_container``), it is the host's checkout: a
    stated property, no more than ENG-205's own lineage accepts of a copy
    on the freed number that carries the checkout's re-mint."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    _the_containers_re_mint(lab, box, kid, "recorded")
    found = _file_token(line)
    freed = location.object_key(str(line / ".git"))
    assert freed is not None
    inodes = {s.name: s.inode for s in lab.model().checkout(hid).stamps}
    os.rename(line, tmp_path / "host" / "line-old")
    time.sleep(0.01)
    copy(tmp_path / "host" / "line-old", line)
    shutil.rmtree(tmp_path / "host" / "line-old")
    target = os.path.realpath(line / ".git")
    real = location_git._init_stamps

    def reused(
        git_dir: str, *, linked: bool = False
    ) -> tuple[tuple[str, int, int, int, int], ...]:
        got = real(git_dir, linked=linked)
        if os.path.realpath(git_dir) != target:
            return got
        return tuple((n, inodes.get(n, i), m, c, z) for n, i, m, c, z in got)

    monkeypatch.setattr(location, "_init_stamps", reused)
    if seen == "seen_by_the_container":
        assert box.post(lab).checkout == kid
    location.forget()

    with posing(line / ".git", freed):
        res, admitted = lab.view(line)

    if seen == "unseen":
        assert (res.rule, res.outcome) == ("ask:several_projects", ASK)
        assert res.checkout != hid and admitted == []
        assert lab.model().checkout(hid).token != found
    else:
        assert (res.checkout, res.outcome, res.rule) == (hid, SILENT, "object+remint")
        assert admitted == ["h1"] and lab.model().checkout(hid).token == found


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("moved", ["before_the_container", "after_both"])
def test_a_ctime_moved_before_the_containers_re_mint_reads_the_hosts(
    tmp_path: Path, lab: Lab, moved: str
) -> None:
    """Every stamped file's ctime moves (a `chmod -R` in a container's
    set-up) before the container first sees the git directory, or after
    both machines did; the container then sees it, is separated and
    re-mints, and the host's next prompt is the first sighting of the
    host since the move. The host's record reads its files here but for
    their ctimes, and the container's record, holding the token found,
    read them as they are now: the host's own checkout, silent, whose
    stamps then follow, so the next sighting is by its token. Before, a
    second checkout joined silently to the container's project
    (``join:H+T``), none of the host's memories delivered, for good."""
    line = make_repo(tmp_path / "host" / "line", commits=2)
    hid = lab.write("h1", line).checkout
    box = Shared(line, "/workspaces/line", _container_machine(lab, "line"))
    if moved == "after_both":
        assert box.post(lab).checkout is not None
    _ctime_moved(line)
    kid = box.post(lab).checkout
    assert kid is not None
    _the_containers_re_mint(lab, box, kid, "recorded")
    location.forget()

    res, admitted = lab.view(line)

    assert (res.checkout, res.outcome, res.rule) == (hid, SILENT, "object+remint")
    assert admitted == ["h1"] and _host_checkouts(lab) == [hid]
    location.forget()
    steady, _admitted = lab.view(line)
    assert (steady.checkout, steady.rule) == (hid, "object+token")


def _host_checkouts(lab: Lab) -> list[str]:
    return [k.id for k in lab.model().checkouts() if k.machine == lab.identity.id]


def _planted(line: Path, token: str) -> None:
    """`token` written into `line`'s git directory, as a copied `info`
    directory or a client of another release leaves it."""
    checkout_token.token_path(line / ".git").write_bytes(checkout_token.render(token))


# ---------------------------------------------------------------------------
# What the rule must not read as the host's
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("no_birth")
def test_a_token_of_another_line_in_the_shared_git_dir_is_not_the_hosts(
    tmp_path: Path, lab: Lab
) -> None:
    """The object is the host's git directory and the container's view saw
    its files, but the token there is another line's (copied in): no
    re-mint of the host's, and recorded by no other machine's view of
    these files. Not the host's re-mint."""
    line, _box, hid, _kid = _shared(tmp_path, lab, "line")
    other = make_repo(tmp_path / "host" / "other", commits=1)
    lab.write("o1", other)
    foreign = _file_token(other)
    assert foreign is not None
    _planted(line, foreign)
    location.forget()

    res, _admitted = lab.view(line)

    assert res.rule != "object+remint"
    assert lab.model().checkout(hid).token != foreign


@pytest.mark.usefixtures("no_birth")
def test_a_token_naming_no_lineage_over_the_share_is_not_the_hosts(
    tmp_path: Path, lab: Lab
) -> None:
    """A minted token that names no lineage (a client before 9.0.0rc18 in
    the container), the store never told: the object and the view hold,
    the link does not, so it is not the host's re-mint."""
    line, _box, _hid, _kid = _shared(tmp_path, lab, "line")
    _planted(line, checkout_token.mint())
    location.forget()

    res, _admitted = lab.view(line)

    assert res.rule != "object+remint"


@pytest.mark.usefixtures("no_birth")
def test_a_second_record_of_this_machine_is_no_view_of_its_files(
    tmp_path: Path, lab: Lab
) -> None:
    """A second record of the host's own machine that saw the same files
    under another device number (a snapshot of the git directory mounted
    here keeps its inode numbers and ctimes) and records the host's token;
    the token found is a re-mint of that token over the second record's
    numbers. A re-mint on this machine names this machine's numbers, so
    it is not the host's re-mint."""
    line = make_repo(tmp_path / "host" / "line", commits=2)
    hid = lab.write("h1", line).checkout
    assert hid is not None
    model = lab.model()
    k = model.checkout(hid)
    assert k.object_key is not None and k.token is not None
    twin = dataclasses.replace(k.object_key, device="999", birth=None)
    with lab.store.batch():
        model._put_checkout(
            dataclasses.replace(k, id="K900", object_key=twin, views=())
        )
    made = checkout_token.lineage(k.token, (twin.device, twin.inode))
    assert made is not None
    _planted(line, checkout_token.successor(made))
    location.forget()

    res, _admitted = lab.view(line)

    assert res.rule != "object+remint"


# ---------------------------------------------------------------------------
# Re-mints the host did not see one by one
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("chain", ["twice", "rebuilt"])
def test_a_chain_of_re_mints_on_the_other_side_is_the_hosts(
    tmp_path: Path, lab: Lab, chain: str
) -> None:
    """The container re-mints twice before the host's next sighting
    (``twice``), or a rebuilt container (a machine of its own) first sees
    the re-minted git directory and re-mints it in turn (``rebuilt``), each
    recorded. The last token names the first re-mint, not the host's
    token, but another machine's view of the host's files records it: the
    host's own checkout, silent. Before, a second checkout of the host's
    git directory, joined to the container's last project."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    _the_containers_re_mint(lab, box, kid, "recorded")
    if chain == "twice":
        _the_containers_re_mint(lab, box, kid, "recorded")
    else:
        rebuilt = Shared(line, "/workspaces/line", _container_machine(lab, "line-2"))
        seen = rebuilt.post(lab).checkout
        assert seen is not None and seen != kid
        _the_containers_re_mint(lab, rebuilt, seen, "recorded")
    new = _file_token(line)
    assert not checkout_token.succeeds(new, lab.model().checkout(hid).lineage)
    location.forget()

    res, admitted = lab.view(line)

    assert (res.checkout, res.outcome, res.rule) == (hid, SILENT, "object+remint")
    assert admitted == ["h1"] and lab.model().checkout(hid).token == new
    assert _host_checkouts(lab) == [hid]


# ---------------------------------------------------------------------------
# The stamps follow the files
# ---------------------------------------------------------------------------


def _ctime_moved(repo: Path) -> None:
    """Each stamped file's mode set to the mode it has: its ctime moves,
    as a `chmod -R` or `chown -R` in a container's set-up, an extended
    attribute or a backup's hard link moves it."""
    for name in ("description", "info/exclude"):
        path = repo / ".git" / name
        os.chmod(path, stat.S_IMODE(os.stat(path).st_mode))


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("moved", ["before_the_container", "after_both"])
def test_the_stamps_follow_a_moved_ctime_so_a_later_re_mint_is_the_hosts(
    tmp_path: Path, lab: Lab, moved: str
) -> None:
    """Every stamped file's ctime moves after the host's first sight,
    before the container first sees the git directory or after both did.
    Each machine's next sighting its token proves takes the stamps its
    files read now (the host's capture read afresh, as after the cache's
    lifetime). The container then re-mints, and the host's next prompt is
    its own checkout, in its own project: its capture is read afresh, the
    token being in the capture's signature. Before, the stamps of the
    first sight were kept, no longer read at the object, and the host
    registered a second checkout."""
    line = make_repo(tmp_path / "host" / "line", commits=2)
    hid = lab.write("h1", line).checkout
    assert hid is not None
    first = lab.model().checkout(hid).stamps
    box = Shared(line, "/workspaces/line", _container_machine(lab, "line"))
    if moved == "after_both":
        assert box.post(lab).checkout is not None
    _ctime_moved(line)
    location.forget()
    again, _admitted = lab.view(line)
    assert again.checkout == hid and lab.model().checkout(hid).stamps != first
    kid = box.post(lab).checkout
    assert kid is not None
    _the_containers_re_mint(lab, box, kid, "recorded")
    new = _file_token(line)

    res, admitted = lab.view(line)

    assert (res.checkout, res.outcome, res.rule) == (hid, SILENT, "object+remint")
    assert admitted == ["h1"] and lab.model().checkout(hid).token == new


@pytest.mark.usefixtures("no_birth")
def test_a_record_with_no_stamps_takes_them_at_its_next_proven_sighting(
    tmp_path: Path, lab: Lab
) -> None:
    """A linked worktree's record made before its `commondir` was stamped
    holds no stamps; its next sighting its token proves takes them."""
    line = make_repo(tmp_path / "host" / "line", commits=2)
    lab.write("h0", line)
    wt = tmp_path / "host" / "wt"
    git(line, "worktree", "add", "-q", str(wt))
    hid = lab.write("h1", wt).checkout
    assert hid is not None
    model = lab.model()
    with lab.store.batch():
        model._put_checkout(dataclasses.replace(model.checkout(hid), stamps=()))
    location.forget()

    again, _admitted = lab.view(wt)

    assert again.checkout == hid
    assert [s.name for s in lab.model().checkout(hid).stamps] == ["commondir"]


# ---------------------------------------------------------------------------
# B: another object at the host's path, carrying the token
# ---------------------------------------------------------------------------


@dataclass
class BindMount(Client):
    """A container over a Linux bind mount of `real`: the host's own device
    and inode numbers, no birth time."""

    def posted(self, raw: dict[str, Any]) -> dict[str, Any]:
        names = ("object", "common_object", "root_object", "cwd_object")
        kept = {n: raw[n] for n in names}
        raw = super().posted(raw)
        for n, v in kept.items():
            raw[n] = None if v is None else [v[0], v[1], None]
        return raw


@pytest.mark.parametrize("birth", ["birth", "no_birth"])
@pytest.mark.parametrize("share", ["virtiofs", "bind_mount"])
def test_b_reads_the_other_sides_re_mint_as_over_a_bind_mount(
    tmp_path: Path,
    lab: Lab,
    birth: str,
    share: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container separates its checkout and re-mints; before the host
    next sees it, the host's repository becomes another object at its
    path carrying the token (moved to another volume and back, restored).
    B finds the host's checkout at the path and T the container's
    project: asked, as over a bind mount, with birth time and without.
    Before, over a VM-backed share the token refuted B and the host joined
    the container's project silently."""
    if birth == "no_birth":
        monkeypatch.setattr(location, "REPORT_BIRTH", False)
        location.forget()
    line = make_repo(tmp_path / "host" / "line", commits=2)
    lab.write("h1", line)
    kind = Shared if share == "virtiofs" else BindMount
    box = kind(line, "/workspaces/line", _container_machine(lab, "line"))
    kid = box.post(lab).checkout
    assert kid is not None
    _the_containers_re_mint(lab, box, kid, "recorded")
    found = _file_token(line)
    moved = tmp_path / "host" / "line-moved"
    shutil.copytree(line, moved, symlinks=True)
    shutil.rmtree(line)
    os.rename(moved, line)
    assert _file_token(line) == found
    location.forget()

    res, _admitted = lab.view(line)

    assert (res.outcome, res.rule) == (ASK, "ask:several_projects")


# ---------------------------------------------------------------------------
# A linked worktree
# ---------------------------------------------------------------------------


@dataclass
class Replacing(Shared):
    """A container whose client answers the daemon's questions about its
    own views as a real one reads them (`location.view_state`) where the
    token refutes the view: a token at its git directory that is neither
    the one the question names nor a re-mint of its lineage reads
    replaced."""

    def answer(
        self,
        cap: location.Capture,
        question: Mapping[str, Any],
        answers: Mapping[str, Any],
    ) -> dict[str, Any]:
        out = super().answer(cap, question, answers)
        found = checkout_token.read(cap.git_dir).token if cap.git_dir else None
        states = dict(out.get("views") or {})
        for v in question.get("views") or ():
            token, path = v.get("token"), str(v.get("path"))
            if states.get(path) != "live" or not token or found == token:
                continue
            if not checkout_token.succeeds(
                found, checkout_token.lineage(token, v.get("object"))
            ):
                states[path] = "replaced"
        out["views"] = states
        return out


def _worktree_token(line: Path) -> str | None:
    return checkout_token.read(line / ".git" / "worktrees" / "wt").token


@pytest.mark.usefixtures("no_birth")
def test_a_linked_worktree_reads_the_other_sides_re_mint_either_way(
    tmp_path: Path, lab: Lab
) -> None:
    """A linked worktree's git directory holds neither `description` nor
    `info/exclude`; its `commondir` stamps it. The container re-mints the
    worktree's token and the host, without birth time, reads it as its
    own; the host then separates its worktree checkout, and the container,
    its client answering its own view replaced, reads the host's re-mint
    as its own. Before, with no stamps, the host registered a second
    checkout of its worktree (``common_object+pointer``), and the
    container, its view replaced, a second container checkout, asked
    (``ask:one_witness``)."""
    line = make_repo(tmp_path / "host" / "line", commits=2)
    lab.write("h0", line)
    wt = tmp_path / "host" / "wt"
    git(line, "worktree", "add", "-q", str(wt))
    hid = lab.write("h1", wt).checkout
    assert hid is not None
    assert [s.name for s in lab.model().checkout(hid).stamps] == ["commondir"]
    box = Replacing(wt, "/workspaces/wt", _container_machine(lab, "wt"))
    kid = box.post(lab).checkout
    assert kid is not None and kid != hid
    _the_containers_re_mint(lab, box, kid, "recorded")
    location.forget()

    res, _admitted = lab.view(wt)

    assert (res.checkout, res.outcome, res.rule) == (hid, SILENT, "object+remint")
    assert lab.model().checkout(hid).token == _worktree_token(line)

    project = lab.model().checkout(kid).project
    lab._declaring().declare(
        Declaration("new", "os:tester", checkout=hid, name="host wt")
    )

    again = box.post(lab)

    assert (again.checkout, again.project, again.outcome, again.rule) == (
        kid,
        project,
        SILENT,
        "object+remint",
    )
    assert lab.model().checkout(kid).token == _worktree_token(line)


# ---------------------------------------------------------------------------
# A store that met ENG-215 before this candidate
# ---------------------------------------------------------------------------


def _rc18_registered(lab: Lab, line: Path) -> tuple[str, str]:
    """What 9.0.0rc18 wrote at the host's sighting after the container's
    re-mint, built in the store as its rung 4 wrote it, with no ladder of
    this release read (its rung 2 and B read the token otherwise now): a
    second checkout of this machine at the host's object, its stamps and
    the token found, in the project of the container's checkout recording
    that token; its view at the path; ``checkout_registered`` with the
    rule ``join:H+T``; and ``resolution_joined``. That checkout and the
    token found."""
    found = _file_token(line)
    assert found is not None
    location.forget()
    cap = capture(str(line), write_token=False, use_cache=False)
    loc = location.to_location(cap, machine=lab.identity.id)
    model = lab.model()
    carriers = [k for k in model.checkouts(token=found) if k.machine != lab.identity.id]
    assert len(carriers) == 1 and loc.token == found
    project = model.canonical(carriers[0].project)
    assert project is not None
    with model._call():
        k = model._register(loc, project, "join:H+T")
        model._event(
            "resolution_joined", checkout=k.id, project=project, witnesses=["H", "T"]
        )
    location.forget()
    return k.id, found


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("files", ["as_they_were", "ctime_moved_since"])
def test_a_second_checkout_an_earlier_release_registered_yields_to_the_first(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, files: str
) -> None:
    """9.0.0rc18 registered a second checkout of the host's git directory
    after the container's re-mint, joined silently to the container's
    project, and ENG-205's order resolves the record holding the token
    found. That record was registered by a silent join on the token, with
    no question opened, no declaration made about it and nothing written
    at it, and the first record's files read at the object, as they were
    or with their ctimes moved since (``ctime_moved_since``, the
    container's record reading them as they are now, as the rule reads
    them): the host's next sighting is the first checkout again, which
    records the token and so comes first from then on. Its memories are
    delivered there again."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    _the_containers_re_mint(lab, box, kid, "recorded")
    dup, found = _rc18_registered(lab, line)
    assert dup != hid
    if files == "ctime_moved_since":
        _ctime_moved(line)
        assert box.post(lab).checkout == kid
        location.forget()

    res, admitted = lab.view(line)

    assert (res.checkout, res.outcome, res.rule) == (hid, SILENT, "object+remint")
    assert admitted == ["h1"] and lab.model().checkout(hid).token == found
    location.forget()
    steady, _admitted = lab.view(line)
    assert (steady.checkout, steady.rule) == (hid, "object+token")


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("action", ["join", "new"])
def test_a_second_checkout_a_declaration_named_is_kept(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """The same second checkout, which the user then declared where it is
    (``join``) or a project of its own (``new``, which also takes it out
    of the project of the checkouts recording the token): a choice, so it
    is kept."""
    line, box, _hid, kid = _shared(tmp_path, lab, "line")
    _the_containers_re_mint(lab, box, kid, "recorded")
    dup, _found = _rc18_registered(lab, line)
    target = lab.model().checkout(dup).project if action == "join" else None
    lab._declaring().declare(
        Declaration(action, "os:tester", checkout=dup, target=target, name="kept")
    )
    location.forget()

    res, _admitted = lab.view(line)

    assert res.checkout == dup


def _written_at(
    lab: Lab, label: str, line: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A memory written at `line` as 9.0.0rc18 resolved it, the rule off:
    homed at the second checkout."""
    with monkeypatch.context() as mp:
        mp.setattr(Membership, "_shared_remint", lambda *_a, **_k: False)
        lab.write(label, line)
    location.forget()


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize(
    "answer", ["join_first", "join_own", "separate", "project_new", "unanswered"]
)
def test_a_second_checkout_holding_records_is_asked_about_not_healed(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """The same second checkout, where the user wrote h2 since. The heal
    would hide it: the host's next sighting stays there, h2 delivered,
    and asks (``duplicate_checkout``), its line offering the first
    record's project, the checkout's own and a new one, in that order.
    Joining the first record's project moves the checkout and h2 there,
    h1 and h2 delivered; joining its own keeps it where it is; the
    separate answer (the agent's, naming the question) and `bettermemory
    project new` (naming none) make the checkout and h2 a project of
    their own, its token re-minted. Each is silent from then on, and the
    first checkout stays in its project; unanswered, the same question is
    asked again. Before, the heal took the first checkout silently, h1
    delivered and h2 not, with no line saying so; and `project new` only
    closed the question, the checkout and h2 left in the container's
    project."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    first = lab.model().checkout(hid).project
    _the_containers_re_mint(lab, box, kid, "recorded")
    dup, found = _rc18_registered(lab, line)
    project = lab.model().checkout(dup).project
    _written_at(lab, "h2", line, monkeypatch)
    assert lab.checkout_of("h2") == dup and project != first

    res, admitted = lab.view(line)

    assert (res.checkout, res.outcome, res.rule) == (dup, ASK, "object+token")
    assert admitted == ["h2"] and res.proposal is not None
    asked = lab.model().proposal(res.proposal)
    assert (asked.kind, asked.candidates) == (DUPLICATE_CHECKOUT, (first, project))
    shown = [x for x in res.lines if res.proposal in x]
    calls = [
        f'answer="join", target="{first}"',
        f'answer="join", target="{project}"',
        'answer="separate"',
    ]
    at = [shown[0].find(call) for call in calls] if len(shown) == 1 else []
    assert -1 not in at and at == sorted(at) and len(at) == 3, shown
    decl = {
        "join_first": Declaration(
            "join", "os:tester", checkout=dup, proposal=res.proposal, target=first
        ),
        "join_own": Declaration(
            "join", "os:tester", checkout=dup, proposal=res.proposal, target=project
        ),
        "separate": Declaration("new", "agent:s", checkout=dup, proposal=res.proposal),
        "project_new": Declaration("new", "os:tester", checkout=dup, name="mine"),
    }.get(answer)
    if decl is not None:
        lab._declaring().declare(decl)
    location.forget()

    after, admitted = lab.view(line)

    assert after.checkout == dup
    assert admitted == (["h1", "h2"] if answer == "join_first" else ["h2"])
    if answer == "unanswered":
        assert (after.outcome, after.proposal) == (ASK, res.proposal)
        assert lab.model().proposal(res.proposal).state == "open"
        return
    answered = {"join_first": f"join:{first}", "join_own": f"join:{project}"}
    assert lab.model().proposal(res.proposal).answer == answered.get(answer, "new")
    assert after.outcome == SILENT and lab.model().checkout(hid).project == first
    if answer in answered:
        assert after.project == (first if answer == "join_first" else project)
        assert lab.model().checkout(dup).token == found
    else:
        assert after.project not in (first, project)
        assert lab.project_of("h2") == after.project
        assert lab.model().checkout(dup).token != found


@pytest.mark.usefixtures("no_birth")
def test_a_second_checkout_in_the_first_ones_project_yields_with_its_records(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container re-minted without leaving the host's project (the
    store missed the re-mint, and the container's next sighting recorded
    it): 9.0.0rc18 registered the second checkout in the first one's
    project, and h2 was written at it. Taking the first checkout hides
    nothing, h2's project being the first's, so the sighting yields to
    it, h1 and h2 delivered, and asks nothing. Before, it stayed at the
    second checkout and asked a question naming that one project
    twice."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    first = lab.model().checkout(hid).project
    _the_containers_re_mint(lab, box, kid, "missed")
    assert box.post(lab).checkout == kid
    dup, found = _rc18_registered(lab, line)
    _written_at(lab, "h2", line, monkeypatch)
    assert lab.model().checkout(dup).project == first
    assert lab.checkout_of("h2") == dup

    res, admitted = lab.view(line)

    assert (res.checkout, res.outcome, res.rule) == (hid, SILENT, "object+remint")
    assert admitted == ["h1", "h2"] and res.proposal is None
    assert lab.model().checkout(hid).token == found
    assert not [p for p in lab.model().pending() if p.checkout == dup]


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("then", ["project_new", "join_other"])
@pytest.mark.parametrize("settled", ["heal", "join_first"])
def test_two_records_settled_into_one_carry_both_ones_records(
    tmp_path: Path,
    lab: Lab,
    monkeypatch: pytest.MonkeyPatch,
    settled: str,
    then: str,
) -> None:
    """Two records of the host's git directory settled into one: by the
    heal, h2 written at the second in the first one's project; or by
    joining the first record's project at the second one's question, h1
    at the first. The record no sighting reads from then on is retired
    into the one they read: its records homed there, its view at the
    path gone, a ``checkout_retired`` row naming the one kept. So when
    the user then moves that checkout (`project new` there, or `project
    join` into another project), h1 and h2, both written at that path,
    go with it and are delivered there. Before, the one left behind
    kept its own, in the old project."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    first = lab.model().checkout(hid).project
    _the_containers_re_mint(
        lab, box, kid, "missed" if settled == "heal" else "recorded"
    )
    if settled == "heal":
        assert box.post(lab).checkout == kid
    dup, _found = _rc18_registered(lab, line)
    _written_at(lab, "h2", line, monkeypatch)
    res, admitted = lab.view(line)
    kept, idle = (hid, dup) if settled == "heal" else (dup, hid)
    if settled == "join_first":
        assert res.proposal is not None
        lab._declaring().declare(
            Declaration(
                "join", "os:tester", checkout=dup, proposal=res.proposal, target=first
            )
        )
        location.forget()
        res, admitted = lab.view(line)
    assert (res.checkout, admitted) == (kept, ["h1", "h2"])
    assert lab.checkout_of("h1") == lab.checkout_of("h2") == kept
    gone = lab.model().checkout(idle)
    assert gone.retired and not gone.views_on(lab.identity.id)
    retired = [
        json.loads(r.payload) for r in lab.store.membership_log(["checkout_retired"])
    ]
    assert [(r["checkout"], r.get("into")) for r in retired] == [(idle, kept)]
    if then == "project_new":
        decl = Declaration("new", "os:tester", checkout=kept, name="mine")
    else:
        other = lab.write("r1", make_repo(tmp_path / "r", commits=1)).project
        decl = Declaration("join", "os:tester", checkout=kept, target=other)
    lab._declaring().declare(decl)
    location.forget()

    after, admitted = lab.view(line)

    assert after.checkout == kept and {"h1", "h2"} <= set(admitted)
    assert lab.project_of("h1") == lab.project_of("h2") != first
    assert lab.model().canonical(lab.project_of("h2")) == lab.model().canonical(
        after.project
    )


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("settled", ["heal", "join_first"])
def test_a_question_open_on_a_retired_record_is_closed_as_settled(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, settled: str
) -> None:
    """A changed remote's question still open on the record a heal (the
    second) or an answer joining the first record's project (the first)
    leaves unread: no sighting shows it at the path any more, and its
    answer would move a record no sighting reads (its separate answer, a
    project of its own with nothing delivered at the path). It is closed
    as settled into the record kept (``settled:<kept>``, by the heal or by
    the declaration), out of `project pending`, and an answer naming it is
    refused as already answered; the path stays where it was settled.
    Before, it stayed pending, shown nowhere."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    first = lab.model().checkout(hid).project
    _the_containers_re_mint(lab, box, kid, "recorded")
    dup, _found = _rc18_registered(lab, line)
    if settled == "heal":
        idle, kept, by = dup, hid, "heal"
        qid = _asked_on(lab, dup, REMOTE_DIVERGED, lab.model().checkout(dup).project)
    else:
        idle, kept, by = hid, dup, "declaration:os:tester"
        qid = _asked_on(lab, hid, REMOTE_DIVERGED, first)
        _written_at(lab, "h2", line, monkeypatch)
        asked, _admitted = lab.view(line)
        assert asked.proposal is not None and asked.proposal != qid
        lab._declaring().declare(
            Declaration(
                "join", "os:tester", checkout=dup, proposal=asked.proposal, target=first
            )
        )
    location.forget()

    res, admitted = lab.view(line)

    assert res.checkout == kept and "h1" in admitted
    pr = lab.model().proposal(qid)
    assert (pr.state, pr.answer, pr.answered_by) == ("answered", f"settled:{kept}", by)
    assert qid not in {p.id for p in lab.model().pending()}
    with pytest.raises(ValueError, match="already answered"):
        lab._declaring().declare(
            Declaration("new", "agent:s", checkout=idle, proposal=qid)
        )
    location.forget()
    after, again = lab.view(line)
    assert (after.checkout, again) == (kept, admitted)


@pytest.mark.usefixtures("no_birth")
def test_a_retired_record_is_not_re_keyed_at_its_old_path(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the heal settled the second record into the first (one
    project, h2 written at the second), the host's repository comes back
    at its path as another object (moved to another volume and back):
    B re-keys the record at the path, which is the first alone, its
    view the only one there, so it keeps h1 and h2 and a `project new`
    there takes both. Had the retired record kept its view, B would have
    re-keyed that one, which holds nothing now, and left h1 and h2
    behind."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    _the_containers_re_mint(lab, box, kid, "missed")
    assert box.post(lab).checkout == kid
    dup, _found = _rc18_registered(lab, line)
    _written_at(lab, "h2", line, monkeypatch)
    healed, _admitted = lab.view(line)
    assert healed.checkout == hid and lab.model().checkout(dup).retired
    os.rename(line, tmp_path / "host" / "line-away")
    copy(tmp_path / "host" / "line-away", line)
    location.forget()

    res, admitted = lab.view(line)

    assert res.rule.startswith("rekeyed:") and "B" in res.rule.split(":")[1]
    assert (res.checkout, admitted) == (hid, ["h1", "h2"])
    lab._declaring().declare(Declaration("new", "os:tester", checkout=hid, name="m"))
    location.forget()
    after, admitted = lab.view(line)
    assert (after.checkout, admitted) == (hid, ["h1", "h2"])


def _settled(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, how: str
) -> tuple[Path, Shared, str, str, str]:
    """Two records of the host's git directory settled into one, h1
    written at the first and h2 at the second: by the heal (the second in
    the first one's project) or by joining the first record's project at
    the second one's question (``join_first``). The repository, the
    container, the container's checkout, the record kept and the one
    retired."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    first = lab.model().checkout(hid).project
    _the_containers_re_mint(lab, box, kid, "missed" if how == "heal" else "recorded")
    if how == "heal":
        assert box.post(lab).checkout == kid
    dup, _found = _rc18_registered(lab, line)
    _written_at(lab, "h2", line, monkeypatch)
    res, _admitted = lab.view(line)
    if how == "join_first":
        assert res.proposal is not None
        lab._declaring().declare(
            Declaration(
                "join", "os:tester", checkout=dup, proposal=res.proposal, target=first
            )
        )
    location.forget()
    kept, idle = (hid, dup) if how == "heal" else (dup, hid)
    assert lab.model().checkout(idle).retired
    return line, box, kid, kept, idle


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("settled", ["heal", "join_first"])
def test_the_record_kept_takes_the_next_re_mint_by_the_container(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, settled: str
) -> None:
    """After the two records were settled into one, the container
    re-mints the token again, the store told at its next sighting. The
    host's next sighting continues the record kept on that re-mint, h1
    and h2 delivered, silent; the retired record stays retired; and a
    `project new` there then takes h1 and h2 with it. Before, after the
    answer joining the first record's project, the retired first record,
    first in order and continued by the re-mint as well, was read and
    brought back holding nothing: `project new` moved it and the path
    delivered nothing."""
    line, box, kid, kept, idle = _settled(tmp_path, lab, monkeypatch, settled)
    _the_containers_re_mint(lab, box, kid, "missed")
    assert box.post(lab).checkout == kid
    reminted = _file_token(line)
    location.forget()

    res, admitted = lab.view(line)

    assert (res.checkout, res.outcome, res.rule) == (kept, SILENT, "object+remint")
    assert admitted == ["h1", "h2"] and lab.model().checkout(kept).token == reminted
    assert lab.model().checkout(idle).retired
    location.forget()
    steady, _admitted = lab.view(line)
    assert (steady.checkout, steady.rule) == (kept, "object+token")
    lab._declaring().declare(Declaration("new", "os:tester", checkout=kept, name="m"))
    location.forget()
    after, admitted = lab.view(line)
    assert (after.checkout, admitted) == (kept, ["h1", "h2"])


@pytest.mark.usefixtures("no_birth")
def test_a_second_checkout_a_project_was_absorbed_at_is_kept(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the second checkout the agent absorbed the first checkout's
    project (`join_project`), which writes a ``project_absorbed`` row
    naming that checkout and the declaration: a choice made there, so the
    checkout is kept, and undoing the absorb (`unjoin_project`) leaves the
    host where the user worked. Before, the heal took the first checkout
    and the unjoin then left the host in the first project."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    first = lab.model().checkout(hid).project
    _the_containers_re_mint(lab, box, kid, "recorded")
    dup, _found = _rc18_registered(lab, line)
    project = lab.model().checkout(dup).project
    lab._declaring().declare(
        Declaration("absorb", "agent:s", checkout=dup, target=first)
    )
    location.forget()

    res, admitted = lab.view(line)

    assert (res.checkout, res.outcome) == (dup, SILENT) and admitted == ["h1"]
    lab._declaring().declare(Declaration("unjoin", "agent:s", target=first))
    location.forget()
    after, admitted = lab.view(line)
    assert (after.checkout, after.project, admitted) == (dup, project, [])


@pytest.mark.usefixtures("no_birth")
def test_a_second_checkout_its_project_file_names_is_kept(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the second checkout the user ran `project init`: the root's
    project file is bound to its project, a choice no log row about the
    checkout carries, so the checkout is kept, silent. Before, the heal
    took the first checkout and asked about the file
    (``object+remint``, a file contradiction), over the declaration."""
    line, box, _hid, kid = _shared(tmp_path, lab, "line")
    _the_containers_re_mint(lab, box, kid, "recorded")
    dup, _found = _rc18_registered(lab, line)
    with monkeypatch.context() as mp:
        mp.setattr(Membership, "_shared_remint", lambda *_a, **_k: False)
        lab.init_file(line, "line")
    location.forget()

    res, _admitted = lab.view(line)

    assert (res.checkout, res.outcome, res.rule) == (dup, SILENT, "object+token")


def _asked_on(lab: Lab, checkout: str, kind: str, project: str) -> str:
    """A question of `kind` opened by hand on `checkout`, naming
    `project`: its id."""
    model = lab.model()
    with model._call():
        pr = model._open_proposal(
            checkout=checkout,
            kind=kind,
            candidates=[project],
            evidence={"old": [], "new": [], "dir": "sub", "parent": project},
            reason=kind,
            line="a question about the second checkout",
            signature=None,
        )
    return pr.id


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize(
    ("kind", "answer", "records"),
    [
        (FILE_CONTRADICTION, "open", "none"),
        (FILE_CONTRADICTION, "join_own", "none"),
        (REMOTE_DIVERGED, "open", "none"),
        (REMOTE_DIVERGED, "open", "written"),
        (REMOTE_DIVERGED, "join_own", "none"),
        (REMOTE_DIVERGED, "join_own", "written"),
        (REMOTE_DIVERGED, "join_other", "none"),
        (REMOTE_DIVERGED, "decided_nothing", "none"),
        (REMOTE_DIVERGED, "decided_nothing", "written"),
        (LEGACY, "dismissed", "none"),
        (SUBPROJECT_CONTRADICTION, "join_own", "none"),
    ],
)
def test_a_question_on_a_second_checkout_keeps_it_where_it_asks_its_project(
    tmp_path: Path,
    lab: Lab,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    answer: str,
    records: str,
) -> None:
    """A question was opened on the same second checkout, still open or
    answered since. One that asks which project the checkout is in (its
    project file's contradiction) is a choice there, open or answered, so
    the checkout is kept, the question its only trace. One of another kind
    is a choice once its answer decided the checkout's project: a changed
    remote's answered by keeping its project (``join_own``) or by `project
    join` into another (``join_other``, merging the container's project
    there) keeps it, silent and not asked. Open, or closed by an answer
    that decided nothing (``decided_nothing``), it is not, nor is one
    about something else whatever its answer (the migration's question
    about a gone folder's records, closed as deciding nothing; a part's
    folder below the root, joined): the sighting yields to the first
    checkout where nothing was written at the second, and asks
    (``duplicate_checkout``) where h2 was. Before, an answer to a changed
    remote's question did not keep the checkout, so the heal or the
    question overrode it."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    _the_containers_re_mint(lab, box, kid, "recorded")
    dup, _found = _rc18_registered(lab, line)
    project = lab.model().checkout(dup).project
    if records == "written":
        _written_at(lab, "h2", line, monkeypatch)
    qid = _asked_on(lab, dup, kind, project)
    if answer in ("join_own", "join_other"):
        # `project join <target>` there, answering the open question.
        target: str | None = project
        if answer == "join_other":
            target = lab.write("r1", make_repo(tmp_path / "r", commits=1)).project
        assert target is not None
        lab._declaring().declare(
            Declaration("join", "os:tester", checkout=dup, target=target)
        )
        assert lab.model().proposal(qid).answer == f"join:{target}"
    elif answer != "open":
        model = lab.model()
        with model._call():
            if kind == LEGACY:
                model._close(model.proposal(qid), "new", LEGACY_SEPARATE)
            else:
                model._close(model.proposal(qid), "withdrawn", "a later release")
    location.forget()

    res, admitted = lab.view(line)

    asked = [p.kind for p in lab.model().pending() if p.checkout == dup]
    if (
        kind == FILE_CONTRADICTION
        or answer in ("join_own", "join_other")
        and (kind == REMOTE_DIVERGED)
    ):
        assert (res.checkout, res.rule) == (dup, "object+token")
        assert res.outcome == (ASK if answer == "open" else SILENT)
        assert DUPLICATE_CHECKOUT not in asked
    elif records == "none":
        assert (res.checkout, res.outcome, res.rule) == (hid, SILENT, "object+remint")
        assert admitted == ["h1"] and DUPLICATE_CHECKOUT not in asked
    else:
        assert (res.checkout, res.outcome, res.rule) == (dup, ASK, "object+token")
        assert admitted == ["h2"] and DUPLICATE_CHECKOUT in asked


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("records", ["none", "written"])
def test_a_second_checkout_its_remotes_answer_split_stays_split(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, records: str
) -> None:
    """A changed remote's question on the same second checkout, answered
    with a project of its own (the agent's separate): the checkout and h2
    become project N, its token re-minted into the shared git directory.
    The container reads that token as its own re-mint over the share,
    staying in its project, and its user then joins N (`project join`),
    so N is the project of another machine's checkout recording the token
    found. The answer decided the second checkout's project: the host's
    next sighting stays there, silent, and asks nothing. Before, it took
    the first checkout over the user's answer, or with h2 asked
    ``duplicate_checkout`` about a choice already made."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    first = lab.model().checkout(hid).project
    _the_containers_re_mint(lab, box, kid, "recorded")
    dup, found = _rc18_registered(lab, line)
    project = lab.model().checkout(dup).project
    if records == "written":
        _written_at(lab, "h2", line, monkeypatch)
    qid = _asked_on(lab, dup, REMOTE_DIVERGED, project)
    lab._declaring().declare(Declaration("new", "agent:s", checkout=dup, proposal=qid))
    split = lab.model().checkout(dup).project
    reminted = _file_token(line)
    assert split not in (first, project) and reminted not in (None, found)
    assert lab.model().checkout(dup).token == reminted
    location.forget()
    seen = box.post(lab)
    assert (seen.checkout, lab.model().canonical(seen.project)) == (kid, project)
    assert lab.model().checkout(kid).token == reminted
    lab._declaring(box.identity).declare(
        Declaration("join", "os:tester", checkout=kid, target=split)
    )
    assert lab.model().canonical(lab.model().checkout(kid).project) == split
    location.forget()

    res, admitted = lab.view(line)

    assert (res.checkout, res.outcome, res.rule) == (dup, SILENT, "object+token")
    assert lab.model().canonical(res.project) == split
    assert admitted == (["h2"] if records == "written" else [])
    assert lab.model().checkout(hid).token != reminted
    assert not [p for p in lab.model().pending() if p.checkout == dup]


@pytest.mark.usefixtures("no_birth")
def test_a_second_checkout_outside_the_tokens_project_is_kept(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container's checkout, which holds the token, was declared into
    the first checkout's project after 9.0.0rc18 registered the second
    one in the container's: the second checkout is no longer in the
    project of any checkout recording the token, so what registered it is
    not read as the silent join on that token, and it is kept (the
    project's guard alone: no declaration names it)."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    first = lab.model().checkout(hid).project
    _the_containers_re_mint(lab, box, kid, "recorded")
    dup, found = _rc18_registered(lab, line)
    lab._declaring(box.identity).declare(
        Declaration("join", "os:tester", checkout=kid, target=first)
    )
    assert lab.model().checkout(kid).project == first
    assert _file_token(line) == found
    location.forget()

    res, _admitted = lab.view(line)

    assert (res.checkout, res.rule) == (dup, "object+token")


# ---------------------------------------------------------------------------
# What the rule reads
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("no_birth")
def test_the_rule_reads_the_token_index_alone_and_only_on_a_mismatch(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A steady sighting reads nothing for the rule. A sighting whose token
    the host does not record reads the checkouts by token once each time
    rung 2 is read (the liveness plan before the transaction, and in it),
    and no views of them."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    reads: list[tuple[str, str]] = []
    real = Store.membership_rows

    def counted(self: Store, table: str, **kw: Any) -> Any:
        column = (kw.get("column_in") or ("",))[0]
        reads.append((table, column))
        return real(self, table, **kw)

    monkeypatch.setattr(Store, "membership_rows", counted)
    location.forget()
    steady, _admitted = lab.view(line)
    assert steady.checkout == hid and ("checkouts", "token") not in reads
    _the_containers_re_mint(lab, box, kid, "missed")
    location.forget()
    reads.clear()

    res, _admitted = lab.view(line)

    assert (res.checkout, res.rule) == (hid, "object+remint")
    at = [i for i, r in enumerate(reads) if r == ("checkouts", "token")]
    assert len(at) == 2
    assert all(reads[i + 1 : i + 2] != [("checkout_views", "checkout")] for i in at)


def test_the_log_rows_naming_a_checkout_are_read_alone(lab: Lab) -> None:
    """`Store.membership_log` with a checkout reads only the rows whose
    payload names it as their ``checkout``, of every kind, filtered in SQL:
    what the heal reads of a second checkout's history is as long as it
    is, not as the log's registrations and joins."""
    model = lab.model()
    with lab.store.batch():
        model._event(
            "resolution_joined", checkout="K901", project="P1", witnesses=["T"]
        )
        model._event(
            "resolution_joined", checkout="K902", project="P1", witnesses=["T"]
        )
        model._event(
            "project_absorbed",
            project="P2",
            into="P1",
            checkout="K901",
            evidence="declaration:os:tester",
        )

    rows = list(lab.store.membership_log(checkout="K901"))

    assert [(r.kind, json.loads(r.payload)["checkout"]) for r in rows] == [
        ("resolution_joined", "K901"),
        ("project_absorbed", "K901"),
    ]


def _steps(lab: Lab, checkout: str) -> tuple[bool, int]:
    """What the heal reads of `checkout`'s history
    (`_joined_silently_by_token`), and the SQLite instructions it ran."""
    model = lab.model()
    conn = lab.store._conn
    ran = [0]

    def tick() -> int:
        ran[0] += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        joined = membership_shared._joined_silently_by_token(model, checkout)
    finally:
        conn.set_progress_handler(None, 1)
    return joined, ran[0]


def test_the_heal_reads_no_mutation_row_nor_a_sightings(
    tmp_path: Path, lab: Lab
) -> None:
    """The heal's reading of a second checkout's history visits the rows
    of the kinds that register a checkout or record a declaration about
    it, through the log's kind index: 4,000 rows more of the kinds it does
    not read, each naming the checkout as its ``checkout`` (a view's
    deletion, a mutation row; a session resolved there), add no
    instruction to the read. Before, it visited every membership row of
    the log to filter them, 0.5 s a sighting at 373,000 rows."""
    line = make_repo(tmp_path / "line", commits=1)
    kid = lab.write("h1", line).checkout
    assert kid is not None
    before = _steps(lab, kid)
    conn = lab.store._conn
    seq = conn.execute("SELECT max(seq) FROM log").fetchone()[0]
    rows = []
    for i in range(4_000):
        kind = "checkout_view_delete" if i % 2 else "resolution_session"
        body = json.dumps({"checkout": kid, "machine": "M1", "path": f"/w/{i}"})
        rows.append(
            (seq + 1 + i, "2026-11-04T00:00:00Z", None, kind, body, "x", f"m{i}")
        )
    conn.executemany(
        "INSERT INTO log (seq, ts, session, kind, payload, prev_mac, mac) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )

    after = _steps(lab, kid)

    assert after[0] == before[0]
    assert after[1] - before[1] < 100, (before, after)


# ---------------------------------------------------------------------------
# The stamps' times across namespaces
# ---------------------------------------------------------------------------


def test_one_file_matches_to_the_nanosecond_or_within_a_whole_second() -> None:
    """Two namespaces read one stamped file alike when its name, size and
    times agree, the inode number left out; where either side keeps both
    times to the whole second (a share that rounds them), within a second
    each. Two fine times that differ do not match."""
    t = 1_793_810_133_992_271_456
    fine = ("description", 1, t, t, 73)
    other_inode = ("description", 7, t, t, 73)
    down = ("description", 9, t - t % 10**9, t - t % 10**9, 73)
    up = ("description", 9, t - t % 10**9 + 10**9, t - t % 10**9 + 10**9, 73)
    far = ("description", 9, t - t % 10**9 - 10**9, t - t % 10**9 - 10**9, 73)
    near = ("description", 1, t, t + 1, 73)
    bigger = ("description", 1, t, t, 74)
    assert _same_file(fine, other_inode)
    assert _same_file(fine, down) and _same_file(down, fine) and _same_file(fine, up)
    assert not _same_file(fine, far)
    assert not _same_file(fine, near) and not _same_file(fine, bigger)


def test_a_filetime_reads_as_unix_nanoseconds() -> None:
    """FILETIME counts 100 ns ticks from 1601-01-01 UTC."""
    epoch = 116_444_736_000_000_000
    assert location_git.filetime_ns(epoch) == 0
    assert location_git.filetime_ns(epoch + 1) == 100
    assert location_git.filetime_ns(epoch + 10_000_000) == 1_000_000_000
    assert location_git.filetime_ns(0) == -epoch * 100


def test_the_change_time_is_ntfs_change_time_on_windows(tmp_path: Path) -> None:
    """Off Windows the stamps' ctime is the inode's and no FILE_BASIC_INFO
    is read. On Windows, where `os.stat`'s ctime is the creation time, it
    is NTFS's ChangeTime: the basic information's write time is the one
    `os.stat` reports, its creation time the birth time, and a metadata
    change (the read-only attribute) moves its change time and not its
    write time."""
    path = tmp_path / "description"
    path.write_text("x\n")
    if sys.platform != "win32":
        assert location_git.basic_info_ns(str(path)) is None
        stamp = location_git._init_stamps(str(tmp_path))[0]
        assert stamp[3] == os.stat(path).st_ctime_ns
        return
    st = os.stat(path)
    info = location_git.basic_info_ns(str(path))
    assert info is not None
    created, _accessed, written, changed = info
    assert written == st.st_mtime_ns and changed >= written
    birth = getattr(st, "st_birthtime_ns", None)
    assert birth is None or created == birth
    os.chmod(path, stat.S_IREAD)
    try:
        after = location_git.basic_info_ns(str(path))
        assert after is not None and after[2] == written and after[3] >= changed
        assert location_git._init_stamps(str(tmp_path))[0][3] == after[3]
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.parametrize("read", ["read", "unread"])
def test_a_windows_stamp_holds_the_change_time_or_is_not_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    """On Windows a stamp's ctime is the FILE_BASIC_INFO change time
    (``read``), never ``os.stat``'s ctime, which is the creation time
    there: where the basic information cannot be read (``unread``) the
    file is not stamped, the other stamped files still are."""
    for name in ("description", "info/exclude"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x\n")
    changed = 1_793_810_133_992_271_400

    def basic(path: str) -> tuple[int, int, int, int] | None:
        if read == "unread" and path.endswith("description"):
            return None
        return (1, 2, 3, changed)

    monkeypatch.setattr(location_git, "_WINDOWS", True)
    monkeypatch.setattr(location_git, "basic_info_ns", basic)

    stamps = location_git._init_stamps(str(tmp_path))

    names = ["info/exclude"] if read == "unread" else ["description", "info/exclude"]
    assert [s[0] for s in stamps] == names
    assert all(s[3] == changed for s in stamps)
