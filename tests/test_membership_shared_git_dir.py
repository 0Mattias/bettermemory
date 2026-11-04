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
token over that record's numbers, or the token that record holds. A
record's stamps follow its files at each sighting its birth time or token
proves; B reads such a token as the bind mount does; a linked worktree is
stamped by its `commondir`; and a second checkout an earlier release
registered for want of the rule yields to the first. A copy of the git
directory, the token of a re-mint in a copy carried into the checkout, a
copy landed on the checkout's freed inode number, a token of another line
and a re-mint naming nothing are refused as before.

The container is played on this host (`Shared`): it posts made-up device
and inode numbers for every object and for its init stamps, keeping their
times and sizes, and no birth time, as the probe measured.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from bettermemory import checkout_token, location, location_git
from bettermemory.declare import declaring_model
from bettermemory.location import capture, remint_answer
from bettermemory.membership import ASK, SILENT, Declaration, Membership
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
@pytest.mark.parametrize("shape", ["copy_carried_in", "copy_on_the_freed_number"])
def test_a_re_mint_in_a_copy_or_a_copy_on_the_freed_number_is_not_the_hosts(
    tmp_path: Path, lab: Lab, shape: str
) -> None:
    """The mapping joins no other git directory to the host's checkout.

    ``copy_carried_in``: the container works on a copy of the host's
    repository (an image's `COPY` with `.git`), not on a share. Its
    checkout is separated and re-minted there, and the re-mint's token is
    carried into the host's git directory (a sync of `.git/info`). The
    copy's init stamps have ctimes of their own, so its record saw other
    files than the host's: the host does not read the token as its
    re-mint.

    ``copy_on_the_freed_number``: the container shares the host's git
    directory and re-mints it; then the host's repository is replaced at
    its path by a copy that lands on its inode number (E15). The copy
    holds the re-mint, and its init stamps are not the recorded ones: not
    the host's checkout."""
    if shape == "copy_carried_in":
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
        key = None
    else:
        line, box, hid, kid = _shared(tmp_path, lab, "line")
        _the_containers_re_mint(lab, box, kid, "recorded")
        found = _file_token(line)
        key = location.object_key(str(line / ".git"))
        os.rename(line, tmp_path / "host" / "line-old")
        copy(tmp_path / "host" / "line-old", line)
    assert found is not None and found == _file_token(line)
    location.forget()

    if key is None:
        res, _admitted = lab.view(line)
    else:
        with posing(line / ".git", key):
            res, _admitted = lab.view(line)

    assert res.rule != "object+remint" and res.checkout != hid
    assert lab.model().checkout(hid).token != found


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


def _without_the_rule(
    lab: Lab, line: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str]:
    """The host's sighting as 9.0.0rc18 resolved it, the rule off: a
    second checkout of its git directory, joined to the container's
    project. That checkout and the token found."""
    found = _file_token(line)
    assert found is not None
    location.forget()
    with monkeypatch.context() as mp:
        mp.setattr(Membership, "_shared_remint", lambda *_a, **_k: False)
        before, _admitted = lab.view(line)
    assert before.rule == "join:H+T" and before.checkout is not None
    location.forget()
    return before.checkout, found


@pytest.mark.usefixtures("no_birth")
def test_a_second_checkout_an_earlier_release_registered_yields_to_the_first(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """9.0.0rc18 registered a second checkout of the host's git directory
    after the container's re-mint, joined silently to the container's
    project, and ENG-205's order resolves the record holding the token
    found. That record was registered by a silent join on the token, with
    no question opened and no declaration made about it, and the first
    record's files read at the object: the host's next sighting is the
    first checkout again, which records the token and so comes first from
    then on. Its memories are delivered there again."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    _the_containers_re_mint(lab, box, kid, "recorded")
    dup, found = _without_the_rule(lab, line, monkeypatch)
    assert dup != hid

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
    dup, _found = _without_the_rule(lab, line, monkeypatch)
    target = lab.model().checkout(dup).project if action == "join" else None
    lab._declaring().declare(
        Declaration(action, "os:tester", checkout=dup, target=target, name="kept")
    )
    location.forget()

    res, _admitted = lab.view(line)

    assert res.checkout == dup


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
