"""One git directory that several machines see under other numbers
(ENG-215).

A re-mint names the checkout's token and its git directory's device and
inode number as the store records them for its writer's machine
(`checkout_token.successor`, ENG-205). A Linux bind mount shows a host
and a container one git directory at one device and inode number, so each
machine's record reads the other's re-mint as its own. A share a VM-backed
runtime makes (virtiofs under Docker Desktop and OrbStack, gRPC FUSE)
shows them other numbers, and the container no birth time, so the lineage
a re-mint names is its writer's alone. The rules here read such a token
from the records of the other machines that see the same files:

- `_shared_remint`, for rung 2 (`Membership._continuity`): a token at a
  checkout's own git directory is its re-mint where another machine's
  view of the same files made or recorded it (`_continues`);
- `_view_made`, for B (`membership_resolve._tally`): the same without
  the reading of the object here, as B reads a checkout at another
  object at its path;
- `_restamped`: a checkout's init stamps as its object reads now, where
  birth time or the token proved it, so the files the rules compare
  follow a mode or an extended attribute set on them;
- `_healed`: of two records of one git directory, the earlier one, where
  the later one is the second checkout a resolution that could not tell
  such a re-mint registered (9.0.0rc18 and earlier).

The init stamps are what tells a view from a copy: the name, mtime, ctime
and size of `description`, `info/exclude` and a linked worktree's
`commondir` (`location_git._init_stamps`), each namespace numbering the
inode its own way. No call sets a file's ctime, so a file-level copy has
ctimes of its own; a filesystem snapshot, an overlay's lower layer or a
block-level clone of a VM's disk keeps them. None of the rules reads a
token that is not in the checkout's own git directory: whatever writes
there can already make a token the checkout takes
(`checkout_token.succeeds`), and with birth time the checkout takes any
token found at its object.

Each function takes the model as `self: Membership`, as the ladder's do
(`membership_resolve`); the class binds `_shared_remint` by name. This
module imports the standard library, `checkout_token` and the records and
rows modules, and `membership` only under TYPE_CHECKING.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING

from . import checkout_token
from .membership_records import Checkout, Location, _found_token
from .membership_rows import _checkout_from

if TYPE_CHECKING:
    from .membership import Membership

#: The witnesses after which a checkout's init stamps are taken as its
#: object reads them (`_restamped`): its birth time, its token, or its
#: re-mint. The last HEAD, the init stamps themselves and another witness
#: of the object prove less, and leave the stamps as they were.
RESTAMPED_BY = frozenset({"birth", "token", "remint"})
#: Nanoseconds in a second: a share that keeps times to the second
#: (`_same_file`).
_SECOND_NS = 1_000_000_000


def _shared_remint(
    self: Membership, k: Checkout, loc: Location, found: str | None
) -> bool:
    """Whether `found`, a token at `k`'s object that `k` does not record
    and that names no lineage of `k`'s own, is `k`'s re-mint made through
    another machine's view of the same git directory (`_continues`). One
    read of the checkouts' token index, made only where `k`'s stamps are
    read here, with or without their ctimes. Read only for a sighting
    without birth time whose token is neither recorded nor `k`'s own
    re-mint."""
    if not k.token or not k.stamps or not checkout_token.valid(found):
        return False
    if found == k.token or not (_read_here(k, loc) or _moved_under(k, loc)):
        return False
    return _continues(k, loc, found, _carriers(self, (k.token, found)))


def _continues(
    k: Checkout, loc: Location, found: str, carriers: Collection[Checkout]
) -> bool:
    """The rule over `carriers`, the checkouts recording `k`'s token or
    `found`. Either one of `k`'s init stamps is read here, inode number
    included (A2: the object is the recorded git directory), and another
    machine's view of the same files made or recorded `found`
    (`_made_by`); or every one of `k`'s stamps is read here but for its
    ctime (`_moved_under`: a mode, an owner or an extended attribute set
    on the files since `k` last took them) and a record of another
    machine holding `found` read every stamp here exactly, ctime included
    (`_read_as_now`)."""
    if _read_here(k, loc):
        return _made_by(k, found, carriers)
    return _moved_under(k, loc) and _read_as_now(k, loc, found, carriers)


def _read_here(k: Checkout, loc: Location) -> bool:
    """Whether one of `k`'s init stamps is read at the sighting's object,
    inode number included."""
    return bool({tuple(s) for s in k.stamps} & {tuple(s) for s in loc.stamps})


def _moved_under(k: Checkout, loc: Location) -> bool:
    """Whether every one of `k`'s init stamps is read here by name, inode
    number, mtime and size, its ctime left out."""
    if not k.stamps or not loc.stamps:
        return False
    return _loose(k.stamps) <= _loose(loc.stamps)


def _read_as_now(
    k: Checkout, loc: Location, found: str, carriers: Collection[Checkout]
) -> bool:
    """Whether a record of another machine holding `found` read every
    init stamp here by name, mtime, ctime and size (its inode number its
    namespace's own)."""
    here = _exact(loc.stamps)
    for other in carriers:
        if other.machine == k.machine or other.token != found:
            continue
        if here and here <= _exact(other.stamps):
            return True
    return False


def _loose(stamps: Collection[Sequence[object]]) -> set[tuple[object, ...]]:
    return {(s[0], s[1], s[2], s[4]) for s in stamps if len(s) == 5}


def _exact(stamps: Collection[Sequence[object]]) -> set[tuple[object, ...]]:
    return {(s[0], s[2], s[3], s[4]) for s in stamps if len(s) == 5}


def _view_made(self: Membership, k: Checkout, found: str | None) -> bool:
    """Whether a checkout of another machine that saw the same files as
    `k` (an init stamp in common by name, mtime, ctime and size,
    `_same_files`) either records `found`, which it read or wrote in its
    view of that git directory, or names the device and inode number
    `found` is a re-mint of `k`'s token over (`checkout_token.succeeds`):
    so `found` was written into `k`'s git directory through that view.
    The candidates are the checkouts recording `k`'s token or `found`, one
    indexed read by token with no views (`_carriers`); a record of `k`'s
    own machine is never one, since a re-mint there names `k`'s own
    lineage. False for no token, `k`'s own, or a malformed one."""
    if not k.token or not checkout_token.valid(found) or found == k.token:
        return False
    if not k.stamps:
        return False
    return _made_by(k, found, _carriers(self, (k.token, found)))


def _made_by(k: Checkout, found: str, carriers: Collection[Checkout]) -> bool:
    """`_view_made` over `carriers`, the checkouts recording `k`'s token
    or `found`."""
    for other in carriers:
        key = other.object_key
        if (
            other.machine == k.machine
            or key is None
            or not _same_files(k.stamps, other.stamps)
        ):
            continue
        if other.token == found:
            return True
        made = checkout_token.lineage(k.token, (key.device, key.inode))
        if checkout_token.succeeds(found, made):
            return True
    return False


def _carriers(self: Membership, tokens: Collection[str]) -> list[Checkout]:
    """The checkouts recording any of `tokens`: one read of the
    checkouts' token index, their views not read (no rule here reads
    them)."""
    rows = self._store.membership_rows(
        "checkouts", column_in=("token", sorted(set(tokens)))
    )
    return [_checkout_from(row, ()) for row in rows]


def _same_files(
    mine: Collection[Sequence[object]], theirs: Collection[Sequence[object]]
) -> bool:
    """Whether two machines' init stamps share a file: one name and size,
    and the same mtime and ctime (`_same_file`). The inode number is left
    out: each namespace numbers it its own way."""
    return any(_same_file(a, b) for a in mine for b in theirs)


def _same_file(a: Sequence[object], b: Sequence[object]) -> bool:
    """One stamped file as two namespaces read it: ``(name, inode, mtime,
    ctime, size)``, the inode left out. The times match to the nanosecond,
    or within a second where either side keeps both to the whole second
    (a share that rounds them, or a filesystem that keeps no finer time):
    the second those times name is the one the other side reads finer."""
    if len(a) != 5 or len(b) != 5 or (a[0], a[4]) != (b[0], b[4]):
        return False
    am, ac, bm, bc = a[2], a[3], b[2], b[3]
    if not (
        isinstance(am, int)
        and isinstance(ac, int)
        and isinstance(bm, int)
        and isinstance(bc, int)
    ):
        return False
    if (am, ac) == (bm, bc):
        return True
    coarse = (am % _SECOND_NS == 0 and ac % _SECOND_NS == 0) or (
        bm % _SECOND_NS == 0 and bc % _SECOND_NS == 0
    )
    return coarse and abs(am - bm) < _SECOND_NS and abs(ac - bc) < _SECOND_NS


def _restamped(self: Membership, k: Checkout, loc: Location, how: str) -> Checkout:
    """`k` with the init stamps its object reads now, where rung 2 found
    it by birth time, its token or its re-mint (`RESTAMPED_BY`) and the
    stamps read are others than the recorded ones: a mode, an owner or an
    extended attribute set on a stamped file, or a hard link made to it,
    moves its ctime, and stamps kept from the first sight would then tell
    the checkout from no view of it (`_shared_remint`). A sighting that
    read no stamps leaves the recorded ones. One checkout row where they
    changed; a kept capture posts the stamps of the read it keeps, so a
    change is taken at the next capture read afresh (a token re-minted
    since is one: the token is in the capture's signature)."""
    if how not in RESTAMPED_BY or not loc.stamps:
        return k
    if {tuple(s) for s in loc.stamps} == {tuple(s) for s in k.stamps}:
        return k
    return self._put_checkout(dataclasses.replace(k, stamps=tuple(loc.stamps)))


def _healed(self: Membership, kept: Checkout, later: Checkout, loc: Location) -> bool:
    """Whether rung 2 takes `kept`, which `_shared_remint` continues,
    over `later`, a later record of the same object on this machine that
    records the token found (and which ENG-205's order would take): where
    `later` is the second checkout a resolution that could not read the
    other machine's re-mint registered (9.0.0rc18 and earlier, or while
    the stamps had moved). All of:

    - `kept` is continued by another machine's re-mint (`_continues`),
      not its own (`checkout_token.succeeds` over its own lineage fails):
      two records after a re-mint of this machine's own are ENG-205's,
      left in its order;
    - `later` was registered by a silent first-sight join on that token
      (``join:`` with T among its witnesses), into the project of a
      checkout of another machine recording the token, and no question
      was opened about it and no declaration named it: no one chose it.

    Then the git directory is `kept`'s and the token was written into it
    through another machine's view, so with birth time the sighting that
    registered `later` would have continued `kept`. The sighting records the token on `kept`, which then comes
    first in order, so this is read once; `later` stays, idle, with the
    records written at it in its project. One read of the token index
    (the rule's own, over the carriers it reads) and, where that and the
    project hold, of `later`'s questions and of the log's registrations
    and declarations (`_joined_silently_by_token`)."""
    found = _found_token(later, loc)
    if found is None or found != later.token or not kept.token or not kept.stamps:
        return False
    if found == kept.token or checkout_token.succeeds(found, kept.lineage):
        return False
    if not (_read_here(kept, loc) or _moved_under(kept, loc)):
        return False
    carriers = _carriers(self, (kept.token, found))
    if not _continues(kept, loc, found, carriers):
        return False
    projects = {
        self._canon(c.project)
        for c in carriers
        if c.token == found and c.machine != kept.machine
    }
    if self._canon(later.project) not in projects:
        return False
    if self._store.membership_rows("proposals", where={"checkout": later.id}):
        return False
    return _joined_silently_by_token(self, later.id)


def _joined_silently_by_token(self: Membership, checkout: str) -> bool:
    """Whether the membership log registers `checkout` by a first-sight
    join with the token among its witnesses, and names it in no
    declaration (a ``resolution_new``, or a ``resolution_joined`` whose
    witness is the declaration). Read only where `_healed` has found the
    rest, which is once a store's duplicate."""
    registered = False
    for row in self._store.membership_log(
        ["checkout_registered", "resolution_joined", "resolution_new"]
    ):
        try:
            payload = json.loads(row.payload)
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("checkout") != checkout:
            continue
        if row.kind == "resolution_new":
            return False
        if row.kind == "resolution_joined":
            if "declaration" in (payload.get("witnesses") or ()):
                return False
            continue
        rule = str(payload.get("rule") or "")
        codes = rule.split(":", 1)[1].split("+") if rule.startswith("join:") else []
        registered = "T" in codes
    return registered
