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
made it (`membership_resolve._shared_remint`): its init stamps read there
now (the object is the recorded git directory), the other record shares
an init stamp with it by name, mtime, ctime and size (it saw the same
files; no copy keeps a ctime), and the token is a re-mint of its token in
that record's git directory. A copy of the git directory, the token of a
re-mint in a copy carried into the checkout, and a copy landed on the
checkout's freed inode number are refused as before.

The container is played on this host (`Shared`): it posts made-up device
and inode numbers for every object and for its init stamps, keeping their
times and sizes, and no birth time, as the probe measured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from bettermemory import checkout_token, location
from bettermemory.declare import declaring_model
from bettermemory.location import capture, remint_answer
from bettermemory.membership import SILENT, Declaration

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


def _the_containers_re_mint(lab: Lab, box: Shared, kid: str, store: str) -> None:
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
