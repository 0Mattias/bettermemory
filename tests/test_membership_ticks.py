"""Whether a git directory's file system keeps FILETIME's ticks, decided
over all a capture reads there (ENG-235).

Rung 2 reads no re-mint by another machine from files kept in FILETIME's
100 ns ticks (NTFS, and WSL2's drvfs view of it): a change time there is
set by any caller and kept by a CopyFile2 copy, so the init stamps cannot
tell a view from a copy (ENG-215). 9.0.0rc19's first cut read that from
the stamps alone, a checkout's one or two files, and a file system
keeping nanoseconds gives such stamps by chance (a linked worktree's one
about once in 100 on APFS), each such checkout failing closed: a second
checkout registered as in 9.0.0rc18. The capture now decides it once,
over the mtimes and ctimes of everything it reads on the git directory's
device, and from the mount table where it reads no birth time
(`location_stamps.kept_in_ticks`), and posts it with the location. The
checkout row keeps it inside its stamps column, which every release since
9.0.0rc15 reads past, so no schema step is needed
(`membership_rows.TICKS_KEY`). A client or a record that does not say
reads as before, by its stamps, but a record an earlier release wrote
that read the sighting's files to the nanosecond is read as the sighting
says.

Stamps fallen on ticks by chance are played by `ticks`, the capture
deciding no ticks (`posix_times`); NTFS by `ntfs`.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bettermemory import location, location_stamps, membership_rows, membership_shared
from bettermemory.location import Capture, capture
from bettermemory.membership import SILENT, Declaration
from bettermemory.membership_records import Checkout, InitStamp
from bettermemory.store_rows import _membership_spec, _membership_values

from .test_membership_remint import _file_token
from .test_membership_resolution import (
    Lab,
    _container_machine,
    git,
    lab,
    lab_of,
    make_repo,
    neutral_git,
    no_birth,
    pytestmark,
)
from .test_membership_shared_git_dir import (
    Replacing,
    Rounding,
    Shared,
    _rc18_registered,
    _shared,
    _the_containers_re_mint,
    _worktree_token,
    ntfs,
    posix_times,
    ticks,
    windows_chmod,
    windows_unlink,
)

__all__ = [
    "lab",
    "lab_of",
    "neutral_git",
    "no_birth",
    "ntfs",
    "posix_times",
    "pytestmark",
    "ticks",
    "windows_chmod",
    "windows_unlink",
]

#: A time on a whole 100 ns tick, not on a whole second.
_T = 1_793_810_133_992_271_400
#: The capture's decision as shipped: every test here stands another in
#: (`posix_times`), and those of the decision itself take it back.
_KEPT_IN_TICKS = location_stamps.kept_in_ticks


def _host_checkouts(lab: Lab) -> list[str]:
    return [k.id for k in lab.model().checkouts() if k.machine == lab.identity.id]


def _as_an_earlier_release_wrote(lab: Lab) -> None:
    """Every checkout row as 9.0.0rc18 wrote it: its stamps column holding
    the stamps alone, saying nothing of ticks."""
    model = lab.model()
    with model._call():
        for k in model.checkouts():
            model._put_checkout(dataclasses.replace(k, ticks=None))
    assert all(k.ticks is None for k in lab.model().checkouts())


def _the_hosts_re_mint(lab: Lab, line: Path, hid: str) -> str:
    """The host separates its checkout `hid` (`project new`), re-minting
    the token at `line` with its own lineage: the token now there."""
    lab._declaring().declare(
        Declaration("new", "os:tester", checkout=hid, name="host line")
    )
    found = _file_token(line)
    assert found is not None and lab.model().checkout(hid).token == found
    location.forget()
    return found


@dataclass
class Unsaying(Shared):
    """A container whose client is from before ENG-235 while `says` is
    False: its location token says nothing of ticks."""

    says: bool = False

    def posted(self, raw: dict[str, Any]) -> dict[str, Any]:
        raw = super().posted(raw)
        if not self.says:
            del raw["ticks"]
        return raw


# ---------------------------------------------------------------------------
# Stamps on whole ticks by chance
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("no_birth", "ticks")
@pytest.mark.parametrize("reader", ["host", "container", "linked_worktree"])
def test_stamps_on_ticks_by_chance_read_the_re_mint_as_any_stamps(
    tmp_path: Path, lab: Lab, reader: str
) -> None:
    """Init stamps on whole 100 ns ticks on a file system that keeps
    nanoseconds, whose capture decides it keeps no ticks: the host
    without birth time reads the container's re-mint as its own, the
    container the host's, and a linked worktree's host the container's,
    each silently (``object+remint``) as with any stamps. Before, each
    failed closed: the host registered a second checkout and asked
    (``ask:several_projects``), the container a second container
    checkout, the worktree's host a second worktree checkout."""
    if reader == "linked_worktree":
        line = make_repo(tmp_path / "host" / "line", commits=2)
        lab.write("h0", line)
        wt = tmp_path / "host" / "wt"
        git(line, "worktree", "add", "-q", str(wt))
        hid = lab.write("h1", wt).checkout
        assert hid is not None
        box: Shared = Replacing(wt, "/workspaces/wt", _container_machine(lab, "wt"))
        kid = box.post(lab).checkout
        assert kid is not None
        seen: Path = wt
    else:
        line, box, hid, kid = _shared(tmp_path, lab, "line")
        seen = line
    assert membership_shared._ticked(lab.model().checkout(hid).stamps)
    assert lab.model().checkout(hid).ticks is False

    if reader == "container":
        found = _the_hosts_re_mint(lab, line, hid)
        again = box.post(lab)
        assert (again.checkout, again.outcome, again.rule) == (
            kid,
            SILENT,
            "object+remint",
        )
        assert lab.model().checkout(kid).token == found
        return
    _the_containers_re_mint(lab, box, kid, "recorded")
    location.forget()

    res, admitted = lab.view(seen)

    assert (res.checkout, res.outcome, res.rule) == (hid, SILENT, "object+remint")
    assert admitted == (["h0", "h1"] if reader == "linked_worktree" else ["h1"])
    token = _worktree_token(line) if reader == "linked_worktree" else _file_token(line)
    assert lab.model().checkout(hid).token == token
    assert hid in _host_checkouts(lab) and len(_host_checkouts(lab)) == (
        2 if reader == "linked_worktree" else 1
    )


@pytest.mark.usefixtures("no_birth", "ticks")
def test_an_earlier_releases_second_checkout_yields_with_stamps_on_ticks_by_chance(
    tmp_path: Path, lab: Lab
) -> None:
    """A store 9.0.0rc18 left: a second checkout of the host's git
    directory registered after the container's re-mint, and no row saying
    whether its files keep ticks, the stamps on whole ticks by chance.
    The host's next sighting decides its files keep none, the container's
    record read the same files' times to the nanosecond, so the first
    checkout is healed back as with any stamps (`membership_shared._healed`)
    and takes the sighting's ``ticks``. Before, it failed closed: the
    second checkout stayed, holding the host's memories."""
    line, box, hid, kid = _shared(tmp_path, lab, "line")
    _the_containers_re_mint(lab, box, kid, "recorded")
    dup, found = _rc18_registered(lab, line)
    _as_an_earlier_release_wrote(lab)
    assert membership_shared._ticked(lab.model().checkout(hid).stamps)

    res, admitted = lab.view(line)

    assert (res.checkout, res.outcome, res.rule) == (hid, SILENT, "object+remint")
    assert admitted == ["h1"] and lab.model().checkout(hid).token == found
    assert lab.model().checkout(dup).retired
    assert lab.model().checkout(hid).ticks is False
    assert lab.model().checkout(kid).ticks is None


# ---------------------------------------------------------------------------
# What does not say
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("ticks")
@pytest.mark.parametrize("record", ["said", "unsaid"])
@pytest.mark.parametrize("files", ["ntfs", "chance"])
def test_a_view_that_rounds_the_times_reads_the_other_record_as_it_says(
    tmp_path: Path,
    lab: Lab,
    request: pytest.FixtureRequest,
    files: str,
    record: str,
) -> None:
    """A container over a share keeping whole seconds decides its own
    files keep no ticks, and reads the host's record as it says. Of files
    NTFS keeps (``ntfs``) the host's record is passed over either way: it
    says so (``said``), or, written by an earlier release (``unsaid``),
    it shares no stamp with the container's to the nanosecond and its
    stamps read as ticks. Of files kept in nanoseconds whose stamps fell
    on ticks by chance (``chance``), the host's record that says no ticks
    is read, so the host's re-mint is the container's own; one an earlier
    release wrote is passed over, failing closed, until the host's next
    sighting says (`test_a_record_takes_the_ticks_its_sighting_says`)."""
    request.getfixturevalue("ntfs" if files == "ntfs" else "ticks")
    line = make_repo(tmp_path / "host" / "line", commits=2)
    hid = lab.write("h1", line).checkout
    assert hid is not None
    assert lab.model().checkout(hid).ticks is (files == "ntfs")
    assert membership_shared._ticked(lab.model().checkout(hid).stamps)
    box = Rounding(line, "/workspaces/line", _container_machine(lab, "line"))
    kid = box.post(lab).checkout
    assert kid is not None and lab.model().checkout(kid).ticks is False
    if record == "unsaid":
        _as_an_earlier_release_wrote(lab)
    found = _the_hosts_re_mint(lab, line, hid)

    again = box.post(lab)

    if (files, record) == ("chance", "said"):
        assert (again.checkout, again.rule) == (kid, "object+remint")
        assert lab.model().checkout(kid).token == found
    else:
        assert again.rule != "object+remint"
        assert lab.model().checkout(kid).token != found


@pytest.mark.usefixtures("ticks")
@pytest.mark.parametrize("record", ["never_said", "said_before"])
def test_a_client_that_does_not_say_reads_as_its_record_or_its_stamps(
    tmp_path: Path, lab: Lab, record: str
) -> None:
    """A container's client from before ENG-235 says nothing of ticks.
    Its sighting reads as its record last said (``said_before``: its
    files keep none, so the host's re-mint is its own), and where the
    record never said, as its stamps read, which on whole ticks by chance
    fail closed (``never_said``), as in 9.0.0rc19's first cut."""
    line = make_repo(tmp_path / "host" / "line", commits=2)
    hid = lab.write("h1", line).checkout
    assert hid is not None
    box = Unsaying(
        line,
        "/workspaces/line",
        _container_machine(lab, "line"),
        says=record == "said_before",
    )
    kid = box.post(lab).checkout
    assert kid is not None
    assert lab.model().checkout(kid).ticks is (False if box.says else None)
    box.says = False
    found = _the_hosts_re_mint(lab, line, hid)

    again = box.post(lab)

    if record == "said_before":
        assert (again.checkout, again.rule) == (kid, "object+remint")
        assert lab.model().checkout(kid).token == found
    else:
        assert again.rule != "object+remint"
        assert lab.model().checkout(kid).token != found


@pytest.mark.usefixtures("no_birth")
def test_a_record_takes_the_ticks_its_sighting_says(tmp_path: Path, lab: Lab) -> None:
    """A record an earlier release wrote says nothing of ticks; its next
    sighting proven by its token says, and the row keeps it beside the
    stamps (`membership_rows.TICKS_KEY`)."""
    line = make_repo(tmp_path / "host" / "line", commits=1)
    hid = lab.write("h1", line).checkout
    assert hid is not None and lab.model().checkout(hid).ticks is False
    _as_an_earlier_release_wrote(lab)
    location.forget()

    res, _admitted = lab.view(line)

    assert (res.checkout, res.rule) == (hid, "object+token")
    assert lab.model().checkout(hid).ticks is False
    rows = lab.store.membership_rows("checkouts", where={"id": hid})
    assert rows[0]["stamps_json"][-1] == {"ticks": False}


def test_ticks_ride_in_the_stamps_column_earlier_releases_read_past() -> None:
    """A checkout's ``ticks`` is an object beside its stamps in
    ``stamps_json``: the stamps reader, unchanged since 9.0.0rc15, reads
    five-element lists alone, so every release since reads the stamps as
    before; the row needs no column of its own, which the log's fold of
    an earlier release would refuse in a put; and a row an earlier
    release wrote says nothing."""
    stamp = InitStamp("description", 7, _T, _T + 300, 73)
    k = Checkout(
        id="K001",
        project="P001",
        machine="m",
        kind="primary",
        object_key=None,
        common_object_key=None,
        git_dir=None,
        token=None,
        token_status="n/a",
        stamps=(stamp,),
        last_head=None,
        branch=None,
        remotes=(),
        manifest=None,
        legacy=False,
        retired=False,
        registered="2026-11-05T00:00:00Z",
    )
    for said in (True, False, None):
        row = membership_rows._checkout_row(dataclasses.replace(k, ticks=said))
        extra = [] if said is None else [{"ticks": said}]
        assert row["stamps_json"] == [list(stamp), *extra]
        assert membership_rows._stamps_from(row["stamps_json"]) == (stamp,)
        assert membership_rows._ticks_from(row["stamps_json"]) is said
        assert _membership_values(_membership_spec("checkout"), row)
    assert membership_rows._ticks_from([list(stamp), {"ticks": 1}]) is None
    assert membership_rows._ticks_from(None) is None


def test_the_location_token_carries_ticks() -> None:
    """The capture posts its ``ticks`` in the location token; a token from
    a client before ENG-235 holds none, which reads as not said, and a
    value that is not a boolean refuses the token."""
    raw = Capture(cwd="/w", state=location.REPO, ticks=True).to_token()
    assert raw["ticks"] is True
    cap = Capture.from_token(raw)
    assert cap is not None and cap.ticks is True
    assert location.to_location(cap, machine="m").ticks is True
    del raw["ticks"]
    unsaid = Capture.from_token(raw)
    assert unsaid is not None and unsaid.ticks is None
    raw["ticks"] = "yes"
    assert Capture.from_token(raw) is None


# ---------------------------------------------------------------------------
# The capture's decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("birth", ["birth", "no_birth"])
def test_the_capture_decides_over_all_it_reads_in_the_git_directory(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    birth: str,
) -> None:
    """The capture decides once, over the mtimes and ctimes of the git
    directory, HEAD, config, the root and the init stamps, and hands the
    mount table the git directory's device only where it reads no birth
    time."""
    if birth == "no_birth":
        request.getfixturevalue("no_birth")
    seen: list[tuple[set[int], int | None]] = []

    def spy(times: Any, device: int | None = None) -> bool:
        seen.append((set(times), device))
        return False

    monkeypatch.setattr(location_stamps, "kept_in_ticks", spy)
    line = make_repo(tmp_path / "line", commits=1)
    gd = line / ".git"

    cap = capture(str(line), write_token=False, use_cache=False)

    assert cap.ticks is False and len(seen) == 1
    times, device = seen[0]
    want = {t for s in location._init_stamps(str(gd)) for t in (s[2], s[3])}
    for p in (gd, gd / "HEAD", gd / "config", line):
        st = os.stat(p)
        want |= {st.st_mtime_ns, st.st_ctime_ns}
    assert want <= times
    assert cap.object_key is not None
    assert device == (os.stat(gd).st_dev if cap.object_key[2] is None else None)


def _st(dev: int, mtime: int, ctime: int) -> Any:
    return SimpleNamespace(st_dev=dev, st_mtime_ns=mtime, st_ctime_ns=ctime)


def test_the_capture_reads_no_time_of_another_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the capture read on another device than its git directory's
    (a cwd or a root on another mount) does not enter the decision; with
    no git directory read there is none."""
    seen: list[tuple[list[int], int | None]] = []

    def spy(times: Any, device: int | None = None) -> bool:
        seen.append((sorted(times), device))
        return True

    monkeypatch.setattr(location_stamps, "kept_in_ticks", spy)
    read = [_st(1, _T, _T + 100), _st(2, 5, 5), None]
    stamps = [("description", 7, _T + 200, _T + 300, 73)]

    assert (
        location_stamps.capture_ticks(_st(1, 0, 0), ("1", "9", "1"), read, stamps)
        is True
    )
    assert (
        location_stamps.capture_ticks(_st(1, 0, 0), ("1", "9", None), read, stamps)
        is True
    )
    assert location_stamps.capture_ticks(None, None, read, stamps) is None

    want = [_T, _T + 100, _T + 200, _T + 300]
    assert seen == [(want, None), (want, 1)]


def test_ticks_are_whole_100_ns_and_not_all_whole_seconds_over_any_times() -> None:
    sec = _T - _T % 1_000_000_000
    assert location_stamps.in_ticks([_T, _T + 300])
    assert not location_stamps.in_ticks([_T, _T + 1])
    assert not location_stamps.in_ticks([sec, sec + 1_000_000_000])
    assert location_stamps.in_ticks([sec, _T])
    assert not location_stamps.in_ticks([])


def test_windows_keeps_ticks_whatever_its_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows every file system keeps FILETIMEs (NTFS, ReFS, FAT and
    SMB alike), so the capture decides ticks without reading its times;
    elsewhere the mount table is read only for a device it is handed."""
    monkeypatch.setattr(location_stamps, "_WINDOWS", True)
    assert _KEPT_IN_TICKS([_T + 1])
    monkeypatch.setattr(location_stamps, "_WINDOWS", False)

    def unread(device: int) -> bool:
        raise AssertionError("the mount table is read")

    monkeypatch.setattr(location_stamps, "_drvfs", unread)
    assert not _KEPT_IN_TICKS([_T + 1])
    assert _KEPT_IN_TICKS([_T, _T + 300])


#: /proc/self/mountinfo lines: WSL2's drive mount, a container's bind
#: mount of it (an optional field before the separator), WSL1's, another
#: 9p mount, a virtiofs share, a line too short to read, and WSL2's
#: opt-in virtiofs drive mount as 2.7 tags it (microsoft/WSL#14301).
_MOUNTS = (
    "102 87 0:52 / /mnt/c rw,noatime - 9p C:\\134 rw,dirsync,"
    "aname=drvfs;path=C:\\;uid=1000;gid=1000;symlinkroot=/mnt/,mmap,"
    "access=client,msize=262144,trans=virtio\n"
    "1500 1400 0:56 /Users/me/line /workspaces/line rw,relatime master:9 - "
    "9p C:\\134 rw,dirsync,aname=drvfs;path=C:\\;uid=0;gid=0,mmap,"
    "access=client,msize=65536,trans=fd,rfd=4,wfd=4\n"
    "30 24 0:53 / /mnt/d rw,noatime shared:12 - drvfs D:\\134 rw,case=off\n"
    "40 24 0:54 / /mnt/wslg rw,relatime - 9p none rw,aname=wslg,trans=fd\n"
    "50 24 0:55 / /workspaces rw,relatime - virtiofs mount0 rw\n"
    "60 24 0:57\n"
    "70 24 0:58 / /mnt/e rw,noatime - virtiofs drvfsE0 rw\n"
)


@pytest.mark.skipif(sys.platform == "win32", reason="no mount table on Windows")
@pytest.mark.parametrize(
    ("minor", "drvfs"),
    [
        (52, True),
        (56, True),
        (53, True),
        (54, False),
        (55, False),
        (57, False),
        (58, True),
        (99, False),
    ],
)
def test_drvfs_is_read_from_the_mount_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, minor: int, drvfs: bool
) -> None:
    """A device a 9p mount naming ``aname=drvfs`` (WSL2, a container's
    bind mount of it), a virtiofs one tagged drvfs (WSL2's opt-in
    virtiofs) or a ``drvfs`` one (WSL1) holds keeps ticks whatever its
    times; another 9p mount, another virtiofs share, a line too short and
    a device no line names do not."""
    table = tmp_path / "mountinfo"
    table.write_text(_MOUNTS, encoding="utf-8")
    monkeypatch.setattr(location_stamps, "_MOUNTINFO", str(table))
    monkeypatch.setattr(location_stamps, "_LINUX", True)

    assert location_stamps._drvfs(minor) is drvfs
    assert _KEPT_IN_TICKS([_T + 1], minor) is drvfs


@pytest.mark.skipif(sys.platform == "win32", reason="no mount table on Windows")
@pytest.mark.usefixtures("no_birth")
def test_a_capture_without_birth_on_drvfs_decides_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture that reads no birth time, of a git directory on a device
    the mount table names drvfs, decides its files keep ticks whatever
    its times; without the mount, by its times, which on a file system
    keeping nanoseconds are no ticks but by a chance this test skips."""
    monkeypatch.setattr(location_stamps, "kept_in_ticks", _KEPT_IN_TICKS)
    line = make_repo(tmp_path / "line", commits=1)
    dev = os.stat(line / ".git").st_dev
    table = tmp_path / "mountinfo"
    monkeypatch.setattr(location_stamps, "_MOUNTINFO", str(table))
    monkeypatch.setattr(location_stamps, "_LINUX", True)
    table.write_text("", encoding="utf-8")

    plain = capture(str(line), write_token=False, use_cache=False)
    if plain.ticks:
        pytest.skip("this file system's times fell on ticks")
    table.write_text(
        f"9 1 {location_stamps._device_name(dev)} / /mnt/c rw - 9p C:\\134 "
        "rw,aname=drvfs;path=C:\\\n",
        encoding="utf-8",
    )
    location.forget()

    cap = capture(str(line), write_token=False, use_cache=False)

    assert cap.ticks is True
