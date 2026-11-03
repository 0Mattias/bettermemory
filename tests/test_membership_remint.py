"""A checkout token's re-mint is written with the resolution that asks for
it (ENG-153).

When history outranks a checkout token (a copy carrying another line's
token, design 3.3) or a declaration separates a checkout, the checkout is
given a token of its own. The resolutions of the production paths ran with
no token writer: the model recorded the token as superseded and the caller
re-minted after the transaction, in-process in a second transaction
(`token_written`), and a hook client in another namespace after the final
reply, with no report back at all. The store kept the old token, so the
client's next sighting, its object without a birth time, failed the
token's continuity: a second checkout of the same git directory was
registered and its first sight decided again.

The token is now written inside the resolution's transaction by the
namespace that holds it: this process's, or the client's, asked in the
witness exchange (`location.AnsweredToken`, the client's `location.answer`)
and recorded with the resolution in its one reply. A client that cannot be
asked (one before 9.0.0rc14) re-mints after the final reply as before.

The token file can still run ahead of the store (ENG-205): a client before
9.0.0rc14 re-mints after the final reply, an exchange can end after the
client wrote the token, and a crash can fall between the write and the
commit. A re-mint the store asks for names the token it replaces in its
git directory (`checkout_token.successor`), so the store knows a token it
missed for the checkout's without birth time, whether it records the old
token as current or superseded (`location.continuity`); a client before
9.0.0rc14 is known by the last HEAD (ENG-193). It leaves only another
re-mint of the same token in place, so two exchanges re-minting one
checkout leave one token, and the client whose re-mint found the other's
posts what the file holds; a resolution this process reads itself reads
the token again under its location lock (ENG-214).

The tests drive `location.resolve_caller` and `location.resolve_hook` as
the daemon runs them, with no token writer given; the container is played
on this host (`tests.test_membership_resolution.Container`).
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from bettermemory import (
    _daemon_client,
    checkout_token,
    location,
    location_witness,
    membership_resolve,
)
from bettermemory.cli import project_cmd
from bettermemory.declare import declaring_model
from bettermemory.location import (
    AnsweredLiveness,
    NeedWitnesses,
    continuity,
    remint_answer,
)
from bettermemory.membership import (
    SILENT,
    SUPERSEDED,
    Checkout,
    Declaration,
    Location,
    Membership,
    Resolution,
)
from bettermemory.membership_records import NeedIndependent

from .test_membership_resolution import (
    Container,
    Lab,
    _container_machine,
    birth_reported,
    clone,
    commit,
    git,
    lab,
    lab_of,
    make_repo,
    neutral_git,
    no_birth,
    pytestmark,
    remove,
)

if TYPE_CHECKING:
    from bettermemory.cli._common import CliContext

__all__ = ["lab", "lab_of", "neutral_git", "no_birth", "pytestmark"]


@dataclass
class Client(Container):
    """The hook client's rounds as `_daemon_client._membership_rounds`
    runs them, a final reply asking for a re-mint included (the client
    re-mints after it). `answers_remint` False plays a client before
    9.0.0rc14, whose answers carry no re-mint."""

    answers_remint: bool = True

    def answer(
        self,
        cap: location.Capture,
        question: Any,
        answers: Any,
    ) -> dict[str, Any]:
        if not self.answers_remint:
            question = {k: v for k, v in question.items() if k != "remint"}
        out = super().answer(cap, question, answers)
        if not self.answers_remint:
            out.pop("remint_ok", None)
        return out

    def post(self, lab: Lab, *, session: str = "s-container") -> Resolution:
        res = super().post(lab, session=session)
        if res.remint_token:
            checkout_token.remint(
                self.real / ".git",
                policy=lab.config.membership.write_token,
                bare=False,
            )
        return res


def _carrying_the_token_of(line: Path, copy: Path) -> None:
    """`copy` carries `line`'s checkout token, as a copied ``.git/info``
    does."""
    src = checkout_token.token_path(line / ".git")
    dst = checkout_token.token_path(copy / ".git")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())


def _file_token(repo: Path) -> str | None:
    return checkout_token.read(repo / ".git").token


def _reminted_in(lab: Lab, res: Resolution) -> bool:
    """Whether a ``token_reminted`` row is one of the resolution's own."""
    return any(r.seq in res.rows for r in lab.store.membership_log(["token_reminted"]))


def _questions(lab: Lab) -> int:
    return len(lab.store.membership_rows("proposals"))


def test_g1_a_container_checkout_re_minted_in_the_exchange_stays_one_checkout(
    tmp_path: Path, lab: Lab
) -> None:
    """The review's scenario (G1), through the production exchange path
    with no token writer given: a container's repository carrying the
    host line's token and a history of its own is a project of its own and
    is given a token of its own; the client writes it when the daemon asks,
    the resolution records it in its own transaction and reply, and the
    container's next prompt (its object read with no birth time) is the
    same checkout, silent, with no question asked again."""
    line = make_repo(tmp_path / "host" / "line", commits=2)
    lab.write("h1", line)
    copy = make_repo(tmp_path / "box" / "copy", commits=2)
    _carrying_the_token_of(line, copy)
    box = Client(copy, "/workspaces/copy", _container_machine(lab, "container-a"))

    first = box.post(lab)
    asked = _questions(lab)
    again = box.post(lab)

    assert first.checkout is not None and first.project != lab.project_of("h1")
    assert (again.checkout, again.outcome) == (first.checkout, SILENT)
    mine = [k for k in lab.model().checkouts() if k.machine == box.identity.id]
    assert [k.id for k in mine] == [first.checkout]
    assert _questions(lab) == asked
    assert first.remint_token is False
    assert _reminted_in(lab, first)
    k = lab.model().checkout(first.checkout)
    assert k.token == _file_token(copy) and k.token != _file_token(line)
    assert k.token_status != SUPERSEDED


def test_an_in_process_re_mint_is_written_in_the_resolutions_transaction(
    tmp_path: Path, lab: Lab
) -> None:
    """`resolve_caller` (a hook the daemon reads for itself, a CLI or MCP
    write) re-mints the token of a copy whose history outranks its token
    in the resolution's own transaction: the ``token_reminted`` row is one
    of the resolution's rows, the recorded token is the file's, and the
    next sighting is the same checkout."""
    line = make_repo(tmp_path / "line", commits=2)
    lab.write("l1", line)
    copy = make_repo(tmp_path / "copy", commits=2)
    _carrying_the_token_of(line, copy)
    location.forget()

    res = lab.write("c1", copy)

    assert res.checkout is not None and res.remint_token is False
    assert _reminted_in(lab, res)
    k = lab.model().checkout(res.checkout)
    assert k.token == _file_token(copy) != _file_token(line)
    location.forget()
    again = lab.write("c2", copy)
    assert (again.checkout, again.outcome) == (res.checkout, SILENT)


def test_a_client_that_cannot_be_asked_re_mints_after_the_final_reply(
    tmp_path: Path, lab: Lab
) -> None:
    """A client before 9.0.0rc14 answers no re-mint question: the daemon
    asks it once, then leaves the re-mint to the final reply
    (`Resolution.remint_token`), as it did, and the checkout stays
    superseded until a new token is seen. ENG-205's first window: that
    client re-mints after the final reply, a token that names nothing, and
    tells the store nothing. The container's next prompt (its object read
    with no birth time) is the same checkout, silent, and records the
    file's token: a superseded token's git directory holding a token is
    left to the last HEAD (ENG-193), so the file and the store agree from
    that sighting on."""
    line = make_repo(tmp_path / "host" / "line", commits=2)
    lab.write("h1", line)
    copy = make_repo(tmp_path / "box" / "copy", commits=2)
    _carrying_the_token_of(line, copy)
    box = Client(
        copy,
        "/workspaces/copy",
        _container_machine(lab, "container-b"),
        answers_remint=False,
    )

    first = box.post(lab)

    assert first.checkout is not None and first.remint_token is True
    assert lab.model().checkout(first.checkout).token_status == SUPERSEDED
    assert _file_token(copy) != _file_token(line)
    asked = _questions(lab)
    again = box.post(lab)
    assert (again.checkout, again.outcome) == (first.checkout, SILENT)
    assert again.remint_token is False and _questions(lab) == asked
    k = lab.model().checkout(first.checkout)
    assert (k.token, k.token_superseded) == (_file_token(copy), False)
    mine = [x for x in lab.model().checkouts() if x.machine == box.identity.id]
    assert [x.id for x in mine] == [first.checkout]


def test_a_declaration_about_a_container_checkout_is_re_minted_at_its_next_sighting(
    tmp_path: Path, lab: Lab
) -> None:
    """A declaration made on the host that separates a container's
    checkout (`project new` there, the agent's ``separate``) cannot write
    the token in the container's namespace: the checkout stays superseded,
    and the container's next prompt re-mints it in that resolution's
    transaction, the same checkout."""
    line = make_repo(tmp_path / "host" / "line", commits=2)
    lab.write("h1", line)
    box = Client(line, "/workspaces/line", _container_machine(lab, "container-c"))
    joined = box.post(lab)
    assert joined.checkout is not None and joined.project == lab.project_of("h1")
    token = _file_token(line)

    declared = declaring_model(lab.store, lab.config).declare(
        Declaration("new", "os:tester", checkout=joined.checkout, name="box line")
    )

    k = lab.model().checkout(joined.checkout)
    assert (k.token_status, _file_token(line)) == (SUPERSEDED, token)
    again = box.post(lab)
    assert (again.checkout, again.project) == (joined.checkout, declared.project)
    assert again.remint_token is False and _reminted_in(lab, again)
    k = lab.model().checkout(joined.checkout)
    assert k.token == _file_token(line) != token


# ---------------------------------------------------------------------------
# A superseded token whose re-mint the store missed (ENG-193)
# ---------------------------------------------------------------------------


def _resolve_unwritten(lab: Lab, path: Path) -> Resolution:
    """A resolution at `path` by a model with no token writer, so a
    re-mint it asks for is left to the caller, which may never report it
    (a hook client before 9.0.0rc14, an exchange out of rounds)."""
    location.forget()
    cap = location.capture(str(path), write_token=True, use_cache=False)
    loc = location.to_location(
        cap,
        machine=lab.identity.id,
        session="s",
        history=location.RepositoryHistory(str(cap.common_dir)),
    )
    return Membership(lab.store, lab.identity.id).resolve(loc)


def _superseded_and_reminted_unrecorded(tmp_path: Path, lab: Lab) -> tuple[Path, str]:
    """A copy carrying a line's token, its checkout superseded, and the
    copy's token re-minted without the store being told: the copy and the
    checkout id."""
    line = make_repo(tmp_path / "line", commits=2)
    lab.write("l1", line)
    copy = make_repo(tmp_path / "copy", commits=2)
    _carrying_the_token_of(line, copy)
    res = _resolve_unwritten(lab, copy)
    assert res.remint_token and res.checkout is not None
    k = lab.model().checkout(res.checkout)
    assert k.token_status == SUPERSEDED
    wrote = checkout_token.remint(copy / ".git", policy=True, bare=False)
    assert wrote.token not in (None, k.token)
    location.forget()
    return copy, res.checkout


def test_a_superseded_token_refutes_a_git_directory_holding_no_token() -> None:
    """`continuity` on a superseded token: a re-mint never removes a token,
    so no token found there is another object (E15); a token found there,
    the superseded one or a re-mint the store missed, leaves the last HEAD
    to decide; and where nothing else can be read the superseded token
    still in place is the checkout's, as it was before ENG-193."""

    def read(
        token_here: str | None, last_head: str | None = "a" * 40
    ) -> tuple[bool, str]:
        return continuity(
            "t-old",
            last_head,
            (),
            token_here=token_here,
            head_here="a" * 40,
            stamps_here=(),
            holds=lambda _commits: set(),
            superseded=True,
        )

    assert read(None) == (False, "token")
    assert read("t-new") == (True, "last_head")
    assert read("t-old") == (True, "last_head")
    assert read("t-new", last_head="b" * 40) == (False, "last_head")
    assert read("t-old", last_head=None) == (True, "token")
    assert read("t-new", last_head=None) == (False, "nothing_observable")


@pytest.mark.usefixtures("no_birth")
def test_a_fresh_clone_on_a_superseded_checkouts_inode_is_not_that_checkout(
    tmp_path: Path, lab: Lab
) -> None:
    """E15 for a superseded checkout: a container checkout of the host's
    line is declared a project of its own on the host, which leaves its
    token superseded (the host cannot write in the container); the
    checkout is deleted and a fresh clone of the line lands on its inode
    number with no birth time. The clone holds no token, so it is not the
    separated checkout and does not get its project's memories: it joins
    the line's project as any clone of the line does."""
    url = "https://example.invalid/line.git"
    line = make_repo(tmp_path / "host" / "line", commits=2, remote=url)
    lab.write("h1", line)
    machine = _container_machine(lab, "container-e")
    c_real = clone(line, tmp_path / "box" / "c", url=url)
    joined = Client(c_real, "/workspaces/c", machine).post(lab)
    assert joined.checkout is not None and joined.project == lab.project_of("h1")
    declared = declaring_model(lab.store, lab.config).declare(
        Declaration("new", "os:tester", checkout=joined.checkout, name="box fork")
    )
    assert lab.model().checkout(joined.checkout).token_status == SUPERSEDED

    remove(c_real)
    fresh = clone(line, tmp_path / "box" / "x", url=url)
    assert _file_token(fresh) is None
    res = Client(fresh, "/workspaces/x", machine).post(lab)

    assert res.checkout != joined.checkout
    assert res.project == lab.project_of("h1") != declared.project


@pytest.mark.usefixtures("no_birth")
def test_a_client_answers_a_superseded_checkouts_view_as_the_daemon_reads_it(
    tmp_path: Path, lab: Lab
) -> None:
    """The view question the daemon asks a client it cannot read for
    (`AnsweredLiveness`) names a superseded token as superseded, and the
    client's answer (`location.answer`) reads it as the daemon's own read
    does: a re-mint the store missed leaves the view live, not replaced."""
    copy, kid = _superseded_and_reminted_unrecorded(tmp_path, lab)
    k = lab.model().checkout(kid)
    view = k.views[0]

    daemon = location.path_liveness(k, view)
    with pytest.raises(NeedWitnesses) as need:
        AnsweredLiveness({})(k, view)
    question = need.value.question
    cap = location.capture(str(copy), write_token=False, use_cache=False)
    client = location.answer(question, {}, cap)["views"][view.path]

    assert question["views"][0]["superseded"] is True
    assert (daemon, client) == ("live", "live")


def test_a_superseded_checkout_restored_at_its_path_is_rekeyed(
    tmp_path: Path, lab: Lab
) -> None:
    """B for a superseded checkout whose re-mint the store missed: the copy
    is restored at its path as a new object (a backup restored). The token
    there is the unrecorded re-mint, which is no other lineage's, so the
    checkout is re-keyed silently rather than a third checkout registered
    and asked about."""
    if not birth_reported(tmp_path):
        pytest.skip("B needs birth time to tell the new object (design 2.2)")
    copy, kid = _superseded_and_reminted_unrecorded(tmp_path, lab)
    backup = tmp_path / "copy.bak"
    shutil.copytree(copy, backup, symlinks=True)
    remove(copy)
    os.rename(backup, copy)

    again = _resolve_unwritten(lab, copy)

    assert (again.checkout, again.outcome) == (kid, SILENT)
    assert again.rule.startswith("rekeyed:B")


# ---------------------------------------------------------------------------
# A re-mint the store missed, whatever it records (ENG-205)
# ---------------------------------------------------------------------------


class _Cut(Exception):
    """The process dies after the token file is written and before the
    transaction that records it commits."""


@contextlib.contextmanager
def _cut_after_the_write(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every re-mint in the span writes its token, then the process dies
    (`_Cut`) before the transaction that asked for it commits."""
    with monkeypatch.context() as m:
        real = checkout_token.remint

        def written_then_cut(git_dir: Any, **kw: Any) -> checkout_token.TokenWrite:
            real(git_dir, **kw)
            raise _Cut

        m.setattr(checkout_token, "remint", written_then_cut)
        with pytest.raises(_Cut):
            yield


def _split_cut_off(lab: Lab, checkout: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`project new` in the checkout (`declaring_model`, the CLI's and the
    agent's model), cut off between its re-mint and its commit."""
    with _cut_after_the_write(monkeypatch):
        declaring_model(lab.store, lab.config).declare(
            Declaration("new", "os:tester", checkout=checkout, name="split")
        )


class _Rounds:
    """One hook client's exchange against `location.resolve_hook`, a post
    at a time, as `_daemon_client._membership_rounds` runs it: a re-mint
    answer that finds another session's re-mint in place of the token it
    replaces posts a fresh capture (`location_witness.remint_refused`).
    `asked` holds the questions it answered."""

    def __init__(self, box: Client, lab: Lab, session: str) -> None:
        self.box, self.lab, self.session = box, lab, session
        self.cap = self._capture()
        self.answers: dict[str, Any] = {}
        self.asked: list[dict[str, Any]] = []
        self.reply = self._send({"location": box.probe()})

    def _capture(self) -> location.Capture:
        return location.capture(
            str(self.box.real),
            write_token=self.lab.config.membership.write_token,
            use_cache=False,
        )

    def _send(self, posted: dict[str, Any]) -> Resolution | location.Exchange:
        return location.resolve_hook(
            self.lab.store,
            self.lab.config,
            self.box.mount,
            posted,
            session=self.session,
        )

    def answer(self) -> dict[str, Any]:
        """The next post: the location the reply asks for, or the answers to
        its question (a re-mint's writes the token)."""
        assert isinstance(self.reply, location.Exchange), self.reply
        question = dict(self.reply.question)
        posted: dict[str, Any] = {
            "location": self.box.token(self.cap),
            "machine": location.identity_to_json(self.box.identity),
        }
        if question.get("location") or question.get("machine"):
            return posted
        self.asked.append(question)
        self.answers = self.box.answer(self.cap, question, self.answers)
        if location_witness.remint_refused(question, self.answers):
            self.cap = self._capture()
            posted["location"] = self.box.token(self.cap)
        return {**posted, "answers": self.answers}

    def post(self, posted: dict[str, Any]) -> None:
        self.reply = self._send(posted)

    def to_the_re_mint(self) -> None:
        """Post until the reply asks for the re-mint."""
        for _ in range(location.EXCHANGE_ROUNDS):
            reply = self.reply
            if isinstance(reply, location.Exchange) and reply.question.get("remint"):
                return
            self.post(self.answer())
        raise AssertionError("no re-mint was asked")

    def settled(self) -> Resolution:
        for _ in range(location.EXCHANGE_ROUNDS):
            if isinstance(self.reply, Resolution):
                return self.reply
            self.post(self.answer())
        raise AssertionError("the exchange did not settle")


class _BeforeRc18(_Rounds):
    """`_Rounds` of a hook client of 9.0.0rc14 to rc17: asked to re-mint, it
    renames a token minted at random over whatever the file holds, with no
    claim, compare or lineage, and posts its capture as it was
    (`location._remint_here` before 9.0.0rc18)."""

    def answer(self) -> dict[str, Any]:
        assert isinstance(self.reply, location.Exchange), self.reply
        question = dict(self.reply.question)
        if not question.get("remint") or "remint" in self.answers:
            return super().answer()
        self.asked.append(question)
        assert self.cap.git_dir
        wrote = checkout_token.remint(
            self.cap.git_dir, policy=bool(question.get("write_token")), bare=False
        )
        rest = {k: v for k, v in question.items() if k not in ("remint", "replacing")}
        self.answers = {
            **self.box.answer(self.cap, rest, self.answers),
            "remint": {"token": wrote.token, "status": wrote.status},
        }
        return {
            "location": self.box.token(self.cap),
            "machine": location.identity_to_json(self.box.identity),
            "answers": self.answers,
        }


def _exchange(
    box: Client, lab: Lab, session: str, *, lose_the_re_mint: bool = False
) -> tuple[Resolution | location.Exchange | None, list[dict[str, Any]]]:
    """The hook client's rounds (`_Rounds`, at most `EXCHANGE_ROUNDS` posts
    after the first), and the questions it answered. With
    `lose_the_re_mint` the client answers the re-mint question, which
    writes the token, and its post never reaches the store (it was out of
    rounds, or the post failed): the reply is then None."""
    rounds = _Rounds(box, lab, session)
    for _ in range(location.EXCHANGE_ROUNDS):
        reply = rounds.reply
        if not isinstance(reply, location.Exchange):
            break
        posted = rounds.answer()
        if lose_the_re_mint and reply.question.get("remint"):
            return None, rounds.asked
        rounds.post(posted)
    return rounds.reply, rounds.asked


def _head_rewritten(repo: Path, recorded: str | None) -> None:
    """HEAD amended and the commit it replaced pruned, as a rebase and a
    gc leave a repository: `recorded`, the last HEAD the store holds for
    the checkout, is held nowhere there (`location.continuity` reads it)."""
    assert recorded is not None and git(repo, "rev-parse", "HEAD") == recorded
    git(repo, "commit", "--amend", "-q", "-m", "rewritten")
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now")
    assert not location.RepositoryHistory(str(repo / ".git")).holds([recorded])


def _separated_in_a_container(
    tmp_path: Path, lab: Lab, name: str, *, ahead: bool = False
) -> tuple[Client, str, str]:
    """A container's clone of the host's line, seen there, then declared a
    project of its own on the host (`project new`), which cannot write in
    the container: the checkout is recorded superseded, to be re-minted
    at its next sighting there. With `ahead`, the clone has a commit of
    its own before its first sighting, so its last HEAD is on no remote.
    The client, the checkout and the declared project."""
    url = "https://example.invalid/line.git"
    line = make_repo(tmp_path / "host" / "line", commits=2, remote=url)
    lab.write("h1", line)
    real = clone(line, tmp_path / "box" / name, url=url)
    if ahead:
        commit(real, "box only")
    box = Client(real, f"/workspaces/{name}", _container_machine(lab, name))
    joined = box.post(lab)
    assert joined.checkout is not None and joined.project == lab.project_of("h1")
    declared = declaring_model(lab.store, lab.config).declare(
        Declaration("new", "os:tester", checkout=joined.checkout, name="box fork")
    )
    assert declared.project is not None
    assert lab.model().checkout(joined.checkout).token_superseded
    return box, joined.checkout, declared.project


def test_a_re_mint_naming_the_recorded_token_and_object_continues_it() -> None:
    """`continuity` (witness O without birth time, B18) reads a token that
    names the recorded token and object as the ones it replaced
    (`checkout_token.succeeds`) as the checkout's re-mint, which the store
    missed, whether it records the token as current or superseded and
    whatever the last HEAD says; any other token found is read as before,
    a re-mint of the same token in another git directory among them."""
    was = checkout_token.Lineage(checkout_token.mint(), "4242", "9000000")
    new = checkout_token.successor(was)

    def read(
        here: str | None, *, superseded: bool = False, last_head: str | None = None
    ) -> tuple[bool, str]:
        return continuity(
            was.token,
            last_head,
            (),
            token_here=here,
            head_here="a" * 40,
            stamps_here=(),
            holds=lambda _commits: set(),
            superseded=superseded,
            reminted=lambda: checkout_token.succeeds(here, was),
        )

    assert read(new) == (True, "remint")
    assert read(new, superseded=True, last_head="b" * 40) == (True, "remint")
    assert read(was.token) == (True, "token")
    assert read(checkout_token.mint()) == (False, "token")
    assert read(None) == (False, "token")
    assert read(None, superseded=True) == (False, "token")
    copied = checkout_token.successor(was._replace(inode="9000001"))
    assert read(copied) == (False, "token")
    assert read(copied, superseded=True, last_head="b" * 40) == (False, "last_head")


def test_a_client_names_the_lineage_its_question_names_else_its_own(
    tmp_path: Path,
) -> None:
    """The client's re-mint (`location_witness.remint_answer`) names the
    lineage the daemon's question names, the store's record of the
    checkout (here under the object a bind mount reports, as
    `Container.token` posts it), and none where the question names none
    (the store records no token or object for the checkout); asked by a
    daemon before 9.0.0rc18, whose question has no ``replacing``, it names
    its capture's own token and object, which are what it posted."""
    repo = make_repo(tmp_path / "repo")
    cap = location.capture(str(repo), write_token=True, use_cache=False)
    own = checkout_token.lineage(cap.token or cap.token_written, cap.object_key)
    assert own is not None
    named = own._replace(device="4242", inode="9000000")
    asked = {"remint": True, "write_token": True}

    def answered(question: dict[str, Any]) -> dict[str, Any]:
        # Each re-mint replaces the token captured, as one exchange's does.
        path = checkout_token.token_path(repo / ".git")
        path.write_bytes(checkout_token.render(own.token))
        return remint_answer(cap, question)

    told = answered({**asked, "replacing": list(named)})
    old_daemon = answered(asked)
    unnamed = answered({**asked, "replacing": None})

    assert told["status"] == old_daemon["status"] == unnamed["status"] == "written"
    assert checkout_token.succeeds(told["token"], named)
    assert not checkout_token.succeeds(told["token"], own)
    assert checkout_token.succeeds(old_daemon["token"], own)
    assert not any(checkout_token.succeeds(unnamed["token"], x) for x in (own, named))


def _client_rounds(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    replies: Sequence[dict[str, Any] | Callable[[], dict[str, Any]]],
) -> list[dict[str, Any]]:
    """The hook client's rounds (`_daemon_client._membership_rounds`) at
    `repo`, the daemon's first reply asking for the location and each
    later one taken from `replies` (one that is a function is called for
    it, as the post is made): what the client posted, in order."""
    posted: list[dict[str, Any]] = []
    pending = list(replies)

    def post(_port: Any, _token: Any, _path: str, payload: Any, **_kw: Any) -> Any:
        posted.append(dict(payload))
        reply = pending.pop(0)
        return reply() if callable(reply) else reply

    monkeypatch.setattr(_daemon_client, "post", post)
    ask = {"location": True, "machine": True, "write_token": True}
    _daemon_client._membership_rounds(
        {"port": 0, "token": "t"},
        "prompt",
        {"cwd": str(repo)},
        {"membership": {"ask": ask}},
    )
    return posted


def test_a_re_mint_the_final_reply_asks_names_the_captured_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook client's re-mint after a final reply that asks for one
    (`_daemon_client._membership_rounds`, for a daemon that could not ask
    in the exchange) names the token and object it captured, which are
    the ones it posted, so a store that never hears of the re-mint still
    knows it for that checkout's."""
    repo = make_repo(tmp_path / "repo")
    first = checkout_token.write(repo / ".git", policy=True, bare=False)

    posted = _client_rounds(repo, monkeypatch, [{"membership": {"remint": True}}])

    sent = posted[0]["location"]
    was = checkout_token.lineage(sent["token"], sent["object"])
    assert was is not None and was.token == first.token
    assert checkout_token.succeeds(_file_token(repo), was)


def test_a_re_mint_asked_after_the_clients_last_round_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client's end of the rounds: a re-mint question that comes as the
    reply to its last post (`EXCHANGE_ROUNDS`) is never answered, so the
    token file stays as the store knows it. With the daemon's end
    (`test_no_round_follows_the_re_mint_question`), no exchange ends after
    the client re-minted while the store stands still."""
    repo = make_repo(tmp_path / "repo")
    first = checkout_token.write(repo / ".git", policy=True, bare=False)
    held = {"membership": {"ask": {"held": [], "write_token": True}}}
    late = {"membership": {"ask": {"remint": True, "write_token": True}}}

    posted = _client_rounds(
        repo, monkeypatch, [held] * (location.EXCHANGE_ROUNDS - 1) + [late]
    )

    assert len(posted) == location.EXCHANGE_ROUNDS
    assert not any("remint" in (p.get("answers") or {}) for p in posted)
    assert _file_token(repo) == first.token


def test_a_client_whose_re_mint_finds_another_posts_what_the_file_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook client's rounds (`_daemon_client._membership_rounds`):
    asked to re-mint the token it captured after another session has
    re-minted it, the client writes nothing, answers the other session's
    token (`exists`), and posts a fresh capture, which holds that token,
    with the answer, so its round reads the git directory as it is."""
    repo = make_repo(tmp_path / "repo")
    first = checkout_token.write(repo / ".git", policy=True, bare=False)
    cap = location.capture(str(repo), write_token=False, use_cache=False)
    was = checkout_token.lineage(first.token, cap.object_key)
    assert was is not None
    ask = {"remint": True, "replacing": list(was), "write_token": True}
    other: list[checkout_token.TokenWrite] = []

    def asked_after_another_session_re_minted() -> dict[str, Any]:
        other.append(
            checkout_token.remint(repo / ".git", policy=True, bare=False, replacing=was)
        )
        return {"membership": {"ask": ask}}

    posted = _client_rounds(
        repo, monkeypatch, [asked_after_another_session_re_minted, {"membership": {}}]
    )

    assert other[0].status == "written" and other[0].token not in (None, first.token)
    assert posted[0]["location"]["token"] == first.token
    assert posted[1]["answers"]["remint"] == {
        "token": other[0].token,
        "status": "exists",
    }
    assert posted[1]["location"]["token"] == other[0].token
    assert _file_token(repo) == other[0].token


@pytest.mark.usefixtures("no_birth")
def test_a_declarations_re_mint_cut_off_before_its_commit_stays_its_checkouts(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-205's third window, with the store recording the token as
    current: `project new` in a checkout re-mints its token inside the
    declaration's transaction (the in-process writer, `declaring_model`),
    and the process dies between the write and the commit. The store
    keeps the old token, not superseded; the file holds the new one.
    Without birth time the token proves the object, and the new token
    names the old one in this git directory: the next sighting is the
    same checkout, silent, and records it. Before, the new token refuted
    the object and a second checkout of the same git directory was
    registered."""
    me = location.process_machine()
    repo = make_repo(tmp_path / "repo", commits=2)
    first = lab.write("r1", repo, identity=me)
    kid = first.checkout
    assert kid is not None
    old = _file_token(repo)

    _split_cut_off(lab, kid, monkeypatch)

    k = lab.model(me).checkout(kid)
    assert (k.token, k.token_superseded) == (old, False)
    assert _file_token(repo) not in (None, old)
    location.forget()
    again = lab.write("r2", repo, identity=me)
    assert (again.checkout, again.outcome, again.rule) == (kid, SILENT, "object+remint")
    assert again.project == first.project
    assert lab.model(me).checkout(kid).token == _file_token(repo)
    assert [x.id for x in lab.model(me).checkouts()] == [kid]


@pytest.mark.usefixtures("no_birth")
def test_a_view_holding_a_missed_re_mint_is_live_to_the_daemon_and_the_client(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A view's liveness applies rung 2's rule (B18): a checkout whose
    declaration's re-mint was cut off before its commit holds, at its
    view, a token the store never recorded that names the recorded one in
    its git directory. The daemon's read (`location.path_liveness`) and
    the client's answer to the view question (`location.answer`) both
    read the view live, not replaced."""
    me = location.process_machine()
    repo = make_repo(tmp_path / "repo", commits=2)
    kid = lab.write("r1", repo, identity=me).checkout
    assert kid is not None
    _split_cut_off(lab, kid, monkeypatch)
    k = lab.model(me).checkout(kid)
    view = k.views[0]

    daemon = location.path_liveness(k, view)
    with pytest.raises(NeedWitnesses) as need:
        AnsweredLiveness({})(k, view)
    cap = location.capture(str(repo), write_token=False, use_cache=False)
    client = location.answer(need.value.question, {}, cap)["views"][view.path]

    assert (daemon, client) == ("live", "live")


@pytest.mark.usefixtures("no_birth")
def test_an_in_process_re_mint_cut_off_before_its_commit_stays_its_checkouts(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-205's third window on the resolution's own writer
    (`location._caller`, a hook the daemon reads for itself, a CLI or MCP
    write): a copy carrying a line's token is recorded superseded, its next
    sighting re-mints it in the resolution's transaction, and the process
    dies before the commit. Its last HEAD is then rewritten away, so only
    the token can say what the git directory is: the next sighting without
    birth time is the same checkout, silent, and records the file's token,
    which names the superseded one in this git directory."""
    line = make_repo(tmp_path / "line", commits=2)
    lab.write("l1", line)
    copy = make_repo(tmp_path / "copy", commits=2)
    _carrying_the_token_of(line, copy)
    kid = _resolve_unwritten(lab, copy).checkout
    assert kid is not None
    old = _file_token(copy)
    location.forget()
    with _cut_after_the_write(monkeypatch):
        lab.write("c1", copy)
    k = lab.model().checkout(kid)
    assert (k.token, k.token_superseded) == (old, True)
    assert _file_token(copy) not in (None, old)
    _head_rewritten(copy, k.last_head)
    location.forget()

    again = lab.write("c2", copy)

    assert (again.checkout, again.outcome, again.rule) == (kid, SILENT, "object+remint")
    k = lab.model().checkout(kid)
    assert (k.token, k.token_superseded) == (_file_token(copy), False)
    assert len(lab.model().checkouts()) == 2


@pytest.mark.parametrize("separated", [False, True], ids=["first_sight", "declared"])
def test_no_round_follows_the_re_mint_question(
    tmp_path: Path, lab: Lab, separated: bool
) -> None:
    """ENG-205's second window, an exchange that runs out of rounds after
    the client re-minted, cannot open while the store stands still: the
    re-mint question is the last one a resolution asks. `_give_token` runs
    after every read of the client's repository (the views, asked in one
    round since review M G4, the history, the blobs), and what the
    resolution reads after it (its sub-projects, questions and unresolved
    writes) is the store's; the client's answers accumulate, so the round
    that answers the re-mint reads all it read before, answered, and
    settles. Asked as the reply to the client's last post, the re-mint is
    never answered and nothing is written
    (`test_a_re_mint_asked_after_the_clients_last_round_is_not_written`).
    Shown for both re-mints the exchange asks: a first sight whose token
    history outranks (the copy of G1) and the next sighting of a checkout
    separated on the host. What this shows holds while the store stands
    still. A post that fails, or a store changed between the rounds so
    that the round answering the re-mint asks again, can still end the
    exchange after the write: the store then keeps the token replaced,
    and the next sighting reads the file's token as the checkout's re-mint
    (`test_a_container_re_mint_whose_round_was_lost_is_still_its_checkouts`).
    Where the change is another exchange's re-mint of the same checkout,
    the later re-mint writes nothing and its round posts what the file
    holds (`test_two_exchanges_re_minting_one_checkout_leave_it_one_token`)."""
    kid: str | None = None
    if separated:
        box, kid, _project = _separated_in_a_container(tmp_path, lab, "c")
    else:
        line = make_repo(tmp_path / "host" / "line", commits=2)
        lab.write("h1", line)
        copy = make_repo(tmp_path / "box" / "copy", commits=2)
        _carrying_the_token_of(line, copy)
        box = Client(copy, "/workspaces/copy", _container_machine(lab, "copy"))

    reply, asked = _exchange(box, lab, "s-rounds")

    assert isinstance(reply, Resolution), reply
    assert [bool(q.get("remint")) for q in asked][-1:] == [True], asked
    assert not any(q.get("remint") for q in asked[:-1])
    assert len(asked) < location.EXCHANGE_ROUNDS
    assert _reminted_in(lab, reply)
    if kid is not None:
        assert reply.checkout == kid
    k = lab.model().checkout(str(reply.checkout))
    assert k.token == _file_token(box.real) and not k.token_superseded


def test_a_container_re_mint_whose_round_was_lost_is_still_its_checkouts(
    tmp_path: Path, lab: Lab
) -> None:
    """ENG-205's second window and the re-mint of a checkout recorded as
    superseded on another machine, exercised: a container's checkout
    separated on the host is re-minted at its next prompt there; the
    client writes the token, naming the one the store records, and the
    round that would record it never reaches the store (the client ran
    out of rounds as the store changed under it, or the post failed). The
    store keeps the superseded token. Meanwhile the commit the store
    recorded as the checkout's last HEAD is amended and pruned (a rebase
    and a gc), so only the token can say what the git directory is: the
    container's next prompt (no birth time there) is the same checkout in
    its declared project, silent, and records the file's token, with no
    second checkout and no question. Before, the last HEAD held nowhere
    refuted the object and the clone joined the line as a new checkout."""
    box, kid, project = _separated_in_a_container(tmp_path, lab, "c", ahead=True)
    old = _file_token(box.real)
    recorded = lab.model().checkout(kid).last_head
    asked = _questions(lab)

    # The client names the lineage the question names (``replacing``), not
    # its capture's own, and this test depends on that only because
    # `Container.token` posts made-up object keys (device 4242), which the
    # store records; in a real container the capture's keys are the ones
    # posted, so the two lineages are one.
    reply, _asked = _exchange(box, lab, "s-lost", lose_the_re_mint=True)

    assert reply is None and _file_token(box.real) not in (None, old)
    k = lab.model().checkout(kid)
    assert (k.token, k.token_superseded) == (old, True)
    _head_rewritten(box.real, recorded)

    again = box.post(lab)

    assert (again.checkout, again.project) == (kid, project)
    assert (again.outcome, again.rule) == (SILENT, "object+remint")
    k = lab.model().checkout(kid)
    assert (k.token, k.token_superseded) == (_file_token(box.real), False)
    mine = [x for x in lab.model().checkouts() if x.machine == box.identity.id]
    assert [x.id for x in mine] == [kid]
    assert _questions(lab) == asked


@pytest.mark.parametrize("order", ["recorded_first", "seen_first"])
def test_two_exchanges_re_minting_one_checkout_leave_it_one_token(
    tmp_path: Path, lab: Lab, order: str
) -> None:
    """Two sessions in a container prompt at once in a checkout separated
    on the host, and both exchanges are asked to re-mint its one
    superseded token. The first answer writes its re-mint. The second
    finds that token where the one it replaces was, writes nothing, and
    posts a fresh capture holding it (`checkout_token.remint`'s claim and
    compare, `location_witness.remint_refused`). The first round may be
    recorded before the second arrives, or after it: the second's capture
    then records the re-mint, and the first's own round, its capture older
    than the file, is read with the token it wrote
    (`AnsweredToken.located`). Either way both resolve to the one
    checkout, which records the file's one token, and the next prompt is
    silent and asks nothing. Before, the second re-mint replaced the
    first's token, its round read its old capture as a first sight (a
    second checkout of the git directory, and a question), and the file's
    token was then read as that checkout's re-mint."""
    box, kid, project = _separated_in_a_container(tmp_path, lab, "c")
    old = _file_token(box.real)
    asked = _questions(lab)
    first, second = _Rounds(box, lab, "s-first"), _Rounds(box, lab, "s-second")
    first.to_the_re_mint()
    second.to_the_re_mint()

    answered = first.answer()
    written = _file_token(box.real)
    if order == "recorded_first":
        first.post(answered)
        second.post(second.answer())
    else:
        second.post(second.answer())
        first.post(answered)
    replies = (first.settled(), second.settled())

    assert written not in (None, old) and _file_token(box.real) == written
    assert [(r.checkout, r.project) for r in replies] == [(kid, project)] * 2
    k = lab.model().checkout(kid)
    assert (k.token, k.token_superseded) == (written, False)
    again = box.post(lab)
    assert (again.checkout, again.outcome, again.project) == (kid, SILENT, project)
    mine = [x for x in lab.model().checkouts() if x.machine == box.identity.id]
    assert [x.id for x in mine] == [kid]
    assert _questions(lab) == asked


@pytest.mark.parametrize("birth", [True, False], ids=["birth", "no_birth"])
@pytest.mark.parametrize("recorded", ["older_first", "newer_first"])
def test_a_client_re_mint_finding_an_older_clients_leaves_it(
    tmp_path: Path,
    lab: Lab,
    monkeypatch: pytest.MonkeyPatch,
    birth: bool,
    recorded: str,
) -> None:
    """Two sessions in a container prompt at once in a checkout separated
    on the host, one hook client of 9.0.0rc14 to rc17, and both are asked
    to re-mint its superseded token. The older client's re-mint writes
    first, a token that names nothing. The newer client read the token it
    replaces moments ago, so it takes any other token in its place for
    another session's re-mint: it writes nothing (``exists``) and posts a
    fresh capture holding that token. Whichever round is recorded first,
    both resolve to the one checkout, which records the file's one token,
    and the next prompt is silent and asks nothing. Were the newer
    re-mint to leave only a re-mint of its own lineage, as a declaration's
    does, it would replace the older one's: its token would be the
    store's, the file would hold another, and the next sighting would
    register a second checkout of the git directory (ENG-205)."""
    if not birth:
        monkeypatch.setattr(location, "REPORT_BIRTH", False)
    box, kid, project = _separated_in_a_container(tmp_path, lab, "c")
    asked = _questions(lab)
    older, newer = _BeforeRc18(box, lab, "s-older"), _Rounds(box, lab, "s-newer")
    older.to_the_re_mint()
    newer.to_the_re_mint()

    from_older = older.answer()
    written = _file_token(box.real)
    from_newer = newer.answer()
    if recorded == "older_first":
        older.post(from_older)
        newer.post(from_newer)
    else:
        newer.post(from_newer)
        older.post(from_older)
    replies = (older.settled(), newer.settled())

    assert _file_token(box.real) == written
    assert newer.answers["remint"] == {"token": written, "status": "exists"}
    assert [(r.checkout, r.project) for r in replies] == [(kid, project)] * 2
    k = lab.model().checkout(kid)
    assert (k.token, k.token_superseded) == (written, False)
    again = box.post(lab)
    assert (again.checkout, again.outcome, again.project) == (kid, SILENT, project)
    mine = [x for x in lab.model().checkouts() if x.machine == box.identity.id]
    assert [x.id for x in mine] == [kid]
    assert _questions(lab) == asked


@pytest.mark.usefixtures("no_birth")
def test_a_checkout_recording_the_token_outranks_its_predecessors_re_mint(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rung 2 (O) between two records of one git directory, as a reader
    before 9.0.0rc18 leaves them: a declaration's re-mint is cut off
    before its commit, and a reader that cannot tell the re-mint
    (`checkout_token.succeeds` unknown to it, a daemon not yet restarted)
    registers the file's token as a second checkout. The checkout that
    records the token found is then the one resolved, not the first, which
    the token continues only as its re-mint."""
    me = location.process_machine()
    repo = make_repo(tmp_path / "repo", commits=2)
    kid = lab.write("r1", repo, identity=me).checkout
    assert kid is not None
    _split_cut_off(lab, kid, monkeypatch)
    with monkeypatch.context() as m:
        m.setattr(checkout_token, "succeeds", lambda *_a, **_k: False)
        location.forget()
        second = lab.write("r2", repo, identity=me).checkout
    assert second not in (None, kid)
    assert lab.model(me).checkout(str(second)).token == _file_token(repo)
    location.forget()

    again = lab.write("r3", repo, identity=me)

    assert (again.checkout, again.rule) == (second, "object+token")


@pytest.mark.usefixtures("no_birth")
def test_a_re_mint_outranks_a_record_continued_only_by_a_weaker_witness(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rung 2 (O): a checkout the token found continues as its own re-mint
    yields only to a record that birth time continues or that records the
    token found, not to one continued by a weaker witness. Here a stale
    record at the same object, superseded with a token the git directory
    does not hold, registered after the checkout (a store state made by
    hand), continues by the last HEAD; the checkout's re-mint, cut off
    before its commit, decides."""
    me = location.process_machine()
    repo = make_repo(tmp_path / "repo", commits=2)
    kid = lab.write("r1", repo, identity=me).checkout
    assert kid is not None
    model = lab.model(me)
    k = model.checkout(kid)
    stale = dataclasses.replace(
        k, id="KZZZ", token=checkout_token.mint(), token_status=SUPERSEDED, views=()
    )
    with model._call():
        model._put_checkout(stale)
    _split_cut_off(lab, kid, monkeypatch)
    location.forget()

    again = lab.write("r2", repo, identity=me)

    assert (again.checkout, again.rule) == (kid, "object+remint")


def _reminted_rows(lab: Lab, rows: Collection[int]) -> list[dict[str, Any]]:
    """The payloads of the ``token_reminted`` rows among `rows`."""
    return [
        json.loads(r.payload)
        for r in lab.store.membership_log(["token_reminted"])
        if r.seq in rows
    ]


@pytest.mark.usefixtures("no_birth")
@pytest.mark.parametrize("found", ["foreign", "sibling"])
def test_a_declarations_re_mint_replaces_any_token_but_a_re_mint_of_its_own(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, found: str
) -> None:
    """`project new` (the in-process writer, `declaring_model`) re-mints the
    checkout's token. Where the file holds another checkout's token,
    copied in since the last sighting, the re-mint replaces it as any
    token: the checkout takes a token of its own, the row says it was
    written, and the CLI says it was re-minted. Were that token left, the
    checkout would adopt it, two checkouts in two projects would share it,
    and the CLI would say "re-minted". Where the file holds another re-mint of the same
    token (another session's, here one cut off before its commit), that
    one is left and recorded as the checkout's, and the row and the CLI
    say this declaration wrote nothing (ENG-205)."""
    me = location.process_machine()
    other = make_repo(tmp_path / "other", commits=2)
    lab.write("o1", other, identity=me)
    repo = make_repo(tmp_path / "repo", commits=2)
    kid = lab.write("r1", repo, identity=me).checkout
    assert kid is not None
    if found == "foreign":
        _carrying_the_token_of(other, repo)
    else:
        _split_cut_off(lab, kid, monkeypatch)
    there = _file_token(repo)

    done = declaring_model(lab.store, lab.config).declare(
        Declaration("new", "os:tester", checkout=kid, name="split")
    )

    k = lab.model(me).checkout(kid)
    assert k.token == _file_token(repo)
    carriers = [x.id for x in lab.model(me).checkouts() if x.token == k.token]
    assert carriers == [kid]
    (row,) = _reminted_rows(lab, done.rows)
    ctx = cast("CliContext", SimpleNamespace(store=lab.store))
    said = project_cmd._token_said(ctx, done)
    if found == "foreign":
        assert k.token != there and (row["status"], row["written"]) == ("written", True)
        assert said == "; its checkout token was re-minted"
    else:
        assert k.token == there and (row["status"], row["written"]) == ("exists", False)
        assert "another session" in said


@pytest.mark.usefixtures("no_birth")
def test_a_re_mint_kept_from_its_claim_leaves_the_checkout_superseded(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-mint whose claim other re-mints held through the whole wait
    writes nothing (`checkout_token.CONTENDED`): the checkout stays
    superseded with the line's token, its row says nothing was written,
    and its next sighting asks for the re-mint again and records it.
    Were the contention read as a failed write, it would move the
    checkout out of superseded for good, keeping the line's token
    (ENG-205)."""
    line = make_repo(tmp_path / "line", commits=2)
    lab.write("l1", line)
    copy = make_repo(tmp_path / "copy", commits=2)
    _carrying_the_token_of(line, copy)
    kid = _resolve_unwritten(lab, copy).checkout
    assert kid is not None
    old = _file_token(copy)
    location.forget()
    with monkeypatch.context() as m:
        m.setattr(
            checkout_token,
            "_claim",
            lambda _info, _temp: checkout_token.TokenWrite(checkout_token.CONTENDED),
        )
        held = lab.write("c1", copy)

    k = lab.model().checkout(kid)
    assert (held.checkout, k.token, k.token_superseded) == (kid, old, True)
    assert _file_token(copy) == old
    assert [(r["status"], r["written"]) for r in _reminted_rows(lab, held.rows)] == [
        (checkout_token.CONTENDED, False)
    ]
    location.forget()
    again = lab.write("c2", copy)
    k = lab.model().checkout(kid)
    assert again.checkout == kid and _reminted_in(lab, again)
    assert (k.token, k.token_superseded) == (_file_token(copy), False)
    assert k.token != old


@pytest.mark.parametrize("birth", [True, False], ids=["birth", "no_birth"])
def test_a_resolution_whose_capture_predates_anothers_re_mint_reads_it_again(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, birth: bool
) -> None:
    """ENG-214: two resolutions this process reads itself (two sessions'
    hooks in one checkout, or the CLI beside them) capture a checkout's
    superseded token at once, and the first re-mints it in its
    transaction and commits. The second's capture predates the re-mint:
    in its transaction a checkout at its object records another token
    than the one it holds, so the token is read again
    (`membership_resolve._token_now`) and the second resolves to the
    checkout by the token the first wrote, silently, the cwd captured
    once. Before, without birth time, the superseded token the store no
    longer recorded refuted the object: a second checkout of the git
    directory and a question."""
    if not birth:
        monkeypatch.setattr(location, "REPORT_BIRTH", False)
    line = make_repo(tmp_path / "line", commits=2)
    lab.write("l1", line)
    copy = make_repo(tmp_path / "copy", commits=2)
    _carrying_the_token_of(line, copy)
    kid = _resolve_unwritten(lab, copy).checkout
    assert kid is not None
    location.forget()
    stale = location.capture(str(copy), write_token=True, use_cache=False)
    location.forget()
    first = lab.write("a", copy)
    assert first.checkout == kid and _file_token(copy) not in (None, stale.token)
    asked = _questions(lab)
    real = location.capture
    handed: list[location.Capture] = []

    def taken_before_the_re_mint(*args: Any, **kw: Any) -> location.Capture:
        handed.append(real(*args, **kw) if handed else stale)
        return handed[-1]

    monkeypatch.setattr(location, "capture", taken_before_the_re_mint)
    second = lab.write("b", copy)

    assert handed == [stale]
    assert (second.checkout, second.outcome) == (kid, SILENT)
    k = lab.model().checkout(kid)
    assert (k.token, k.token_superseded) == (_file_token(copy), False)
    assert len(lab.model().checkouts()) == 2
    assert _questions(lab) == asked


def _location_of(lab: Lab, path: Path) -> Location:
    """This process's location of `path` as `location._caller` builds it,
    captured afresh."""
    location.forget()
    cap = location.capture(str(path), write_token=True, use_cache=False)
    return location.to_location(
        cap,
        machine=lab.identity.id,
        session="s",
        history=location.RepositoryHistory(str(cap.common_dir)),
    )


def _resolving_here(lab: Lab) -> Membership:
    """A model as `location._caller` opens one: this machine's, reading its
    own namespace, its re-mints written in the resolution's transaction."""

    def write_token(k: Checkout) -> tuple[str | None, str] | None:
        if k.git_dir is None:
            return None
        made = checkout_token.remint(
            k.git_dir, policy=True, bare=False, replacing=k.lineage
        )
        return made.token, made.status

    return Membership.open(lab.store, lab.identity, token_writer=write_token)


@pytest.mark.parametrize("birth", [True, False], ids=["birth", "no_birth"])
def test_a_resolution_in_another_process_reads_the_token_its_capture_predates(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, birth: bool
) -> None:
    """ENG-214 across processes: two models on one store (the daemon's and
    a CLI command's), each with its own location lock, resolve a checkout
    whose token is superseded. This one captures first; the other
    captures, re-mints the token in its transaction and commits before
    this one's transaction begins. In it a checkout at the capture's
    object records another token than the capture's, so the token is read
    again there, bounded (`membership_resolve._token_now`), and this one
    resolves to the checkout by the token the other wrote: no third
    checkout, no question, and the store records the file's token. Before,
    with birth time the checkout took back the superseded token until its
    next sighting; without it, a third checkout and a question."""
    if not birth:
        monkeypatch.setattr(location, "REPORT_BIRTH", False)
    copy, kid, stale = _stale_after_another_re_mint(tmp_path, lab)
    asked = _questions(lab)

    second = _resolving_here(lab).resolve(stale)

    assert (second.checkout, second.outcome) == (kid, SILENT)
    k = lab.model().checkout(kid)
    assert (k.token, k.token_superseded) == (_file_token(copy), False)
    assert len(lab.model().checkouts()) == 2
    assert _questions(lab) == asked


def _stale_after_another_re_mint(
    tmp_path: Path, lab: Lab
) -> tuple[Path, str, Location]:
    """A copy carrying a line's token, its checkout superseded; this
    process's location of it, captured before another model (another
    process's) re-minted the token and committed. The copy, the checkout
    and the stale location."""
    line = make_repo(tmp_path / "line", commits=2)
    lab.write("l1", line)
    copy = make_repo(tmp_path / "copy", commits=2)
    _carrying_the_token_of(line, copy)
    kid = _resolve_unwritten(lab, copy).checkout
    assert kid is not None
    stale = _location_of(lab, copy)
    first = _resolving_here(lab).resolve(_location_of(lab, copy))
    assert first.checkout == kid and _file_token(copy) not in (None, stale.token)
    return copy, kid, stale


def test_a_token_read_again_on_a_hung_mount_ends_within_its_budget(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-156, ENG-214: the token read again in the store transaction is
    bounded, its stat included. Where the token's mount hangs, the
    resolution goes on after `PROBE_BUDGET` with the capture's token, and
    holds the store's write lock no longer. Were the stat made ahead of
    the bounded read, it would hold the transaction while it hung."""
    copy, _kid, stale = _stale_after_another_re_mint(tmp_path, lab)
    token_file = str(checkout_token.token_path(copy / ".git"))
    hung: list[str] = []
    release = threading.Event()
    real_stat = os.stat

    def stat(path: Any, *args: Any, **kw: Any) -> os.stat_result:
        if os.fspath(path) == token_file and not hung:
            hung.append(token_file)
            release.wait(5.0)
        return real_stat(path, *args, **kw)

    monkeypatch.setattr(os, "stat", stat)
    started = time.monotonic()
    try:
        _resolving_here(lab).resolve(stale)
    finally:
        took = time.monotonic() - started
        release.set()

    assert hung == [token_file]
    assert took < location.PROBE_BUDGET + 1.0


@pytest.mark.parametrize("model", ["other_machine", "client_namespace"])
def test_a_token_read_again_only_in_the_models_own_namespace(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """ENG-205, ENG-214: the token is read again only where the model reads
    this process's files for the capture's machine. A model of another
    machine, and one that reads a client's answers (a container's hook
    client, whose git directory may name a path the host holds another
    repository at, or the same files through a bind mount), resolve the
    capture as posted: the file is not read and its token is not taken,
    though a checkout at the capture's object records another."""
    _copy, _kid, stale = _stale_after_another_re_mint(tmp_path, lab)
    if model == "other_machine":
        resolving = Membership(lab.store, "OTHERMACHINE0000000000000000")
    else:
        views = {v.path: "live" for k in lab.model().checkouts() for v in k.views}
        resolving = Membership(
            lab.store, lab.identity.id, liveness=AnsweredLiveness({"views": views})
        )
    reads: list[str] = []
    seen: list[str | None] = []
    real_read, real_resolve = membership_resolve._token_read, Membership._resolve

    def token_read(git_dir: str) -> str | None:
        reads.append(git_dir)
        return real_read(git_dir)

    def resolve(self: Membership, loc: Location, strict: bool) -> Resolution:
        seen.append(loc.token)
        return real_resolve(self, loc, strict)

    monkeypatch.setattr(membership_resolve, "_token_read", token_read)
    monkeypatch.setattr(Membership, "_resolve", resolve)
    with contextlib.suppress(NeedWitnesses):
        resolving.resolve(stale)

    assert reads == []
    assert seen and set(seen) == {stale.token}


@pytest.mark.parametrize("birth", [True, False], ids=["birth", "no_birth"])
def test_a_resolution_run_again_for_git_reads_the_token_again(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch, birth: bool
) -> None:
    """ENG-203, ENG-214: a resolution that asks git for the tied witnesses
    rolls back and asks with no store lock held (`NeedIndependent`), and
    another process re-mints the checkout's token and commits meanwhile.
    The attempt that runs once more reads the token again in its own
    transaction, so the resolution follows the re-mint: the checkout,
    silently, recording the file's token. Read on the first attempt only,
    the checkout took back the superseded token with birth time, and
    without it a third checkout and a question."""
    if not birth:
        monkeypatch.setattr(location, "REPORT_BIRTH", False)
    line = make_repo(tmp_path / "line", commits=2)
    lab.write("l1", line)
    copy = make_repo(tmp_path / "copy", commits=2)
    _carrying_the_token_of(line, copy)
    kid = _resolve_unwritten(lab, copy).checkout
    assert kid is not None
    asked = _questions(lab)
    location.forget()
    raised: list[int] = []
    other: list[Resolution] = []
    real_resolve, real_lock = Membership._resolve, location._location_lock

    def resolve(self: Membership, loc: Location, strict: bool) -> Resolution:
        if not raised and not other:
            raised.append(1)
            raise NeedIndependent(("0" * 40,))
        return real_resolve(self, loc, strict)

    def lock(loc: Location) -> Any:
        # The other process takes no lock this one holds.
        return contextlib.nullcontext() if raised and not other else real_lock(loc)

    def independent(_self: Any, _commits: Any) -> None:
        location.forget()
        other.append(lab.write("a", copy))

    monkeypatch.setattr(Membership, "_resolve", resolve)
    monkeypatch.setattr(location, "_location_lock", lock)
    monkeypatch.setattr(location.RepositoryHistory, "independent", independent)
    res = lab.write("b", copy)
    monkeypatch.undo()

    assert raised and [r.checkout for r in other] == [kid]
    assert (res.checkout, res.outcome) == (kid, SILENT)
    k = lab.model().checkout(kid)
    assert (k.token, k.token_superseded) == (_file_token(copy), False)
    assert len(lab.model().checkouts()) == 2
    assert _questions(lab) == asked


def test_an_evicted_checkout_is_captured_once_and_its_token_left_unread(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D19, ENG-214: where a file provider holds a checkout's HEAD, config
    and token elsewhere and none is fetched within the budget, the capture
    leaves the token unread, and each prompt resolves with one capture
    and no read of the token file. A resolution that read the token again
    under its location lock, unbounded (a provider downloads the file, or
    blocks while offline), would find it differ from the none the capture
    held and capture the cwd twice. A token read again in the transaction
    is never fetched either."""
    repo = make_repo(tmp_path / "repo", commits=2)
    lab.write("r1", repo)
    calls = {"capture": 0, "read": 0}
    real_capture, real_read = location.capture, checkout_token.read

    def capture(*args: Any, **kw: Any) -> location.Capture:
        calls["capture"] += 1
        return real_capture(*args, **kw)

    def read(git_dir: Any) -> checkout_token.TokenRead:
        calls["read"] += 1
        return real_read(git_dir)

    monkeypatch.setattr(location, "capture", capture)
    monkeypatch.setattr(checkout_token, "read", read)
    monkeypatch.setattr(location, "_evicted", lambda _st: True)
    monkeypatch.setattr(location, "_fetch", lambda _path, _budget: False)
    for _ in range(2):
        location.forget()
        calls.update(capture=0, read=0)
        caller = location.resolve_caller(lab.store, lab.config, str(repo))
        assert caller.location.token_status == "not_read:evicted"
        assert caller.resolution.rule == "deferred:file_evicted"
        assert calls == {"capture": 1, "read": 0}
    assert membership_resolve._token_read(str(repo / ".git")) is None
    assert calls["read"] == 0


def test_a_steady_sighting_reads_no_token_again(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-214: the token is read again in a resolution's transaction only
    where a checkout at the capture's object records another token. A
    first sight, a checkout seen again, and a superseded checkout whose
    file still holds the superseded token (recorded as it is) read
    nothing more than the capture did."""
    reads: list[str] = []
    real = membership_resolve._token_read

    def token_read(git_dir: str) -> str | None:
        reads.append(git_dir)
        return real(git_dir)

    monkeypatch.setattr(membership_resolve, "_token_read", token_read)
    repo = make_repo(tmp_path / "repo", commits=2)
    for label in ("r1", "r2", "r3"):
        location.forget()
        lab.write(label, repo)
    line = make_repo(tmp_path / "line", commits=2)
    lab.write("l1", line)
    copy = make_repo(tmp_path / "copy", commits=2)
    _carrying_the_token_of(line, copy)
    for _ in range(2):
        res = _resolve_unwritten(lab, copy)
        assert res.remint_token and _file_token(copy) == _file_token(line)
    assert reads == []


def test_a_resolutions_re_mint_finding_a_token_it_did_not_read_records_none(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolution this process reads itself re-mints a checkout's
    superseded token, and between its read of the file and its re-mint
    another checkout's token is copied in. The re-mint leaves it
    (``exists``, `checkout_token.remint`), and the checkout does not take
    it: it keeps its record, superseded, and the row says nothing was
    written, so the sighting that reads the file decides. Recording the
    token found would leave two checkouts carrying one token (ENG-205)."""
    line = make_repo(tmp_path / "line", commits=2)
    lab.write("l1", line)
    other = make_repo(tmp_path / "other", commits=2)
    oid = lab.write("o1", other).checkout
    copy = make_repo(tmp_path / "copy", commits=2)
    _carrying_the_token_of(line, copy)
    kid = _resolve_unwritten(lab, copy).checkout
    assert kid is not None and oid is not None
    recorded = lab.model().checkout(kid).token
    foreign = _file_token(other)
    real = checkout_token.remint

    def copied_in_first(*args: Any, **kw: Any) -> checkout_token.TokenWrite:
        _carrying_the_token_of(other, copy)
        return real(*args, **kw)

    monkeypatch.setattr(checkout_token, "remint", copied_in_first)
    location.forget()
    res = lab.write("c1", copy)

    assert res.checkout == kid and _file_token(copy) == foreign
    k = lab.model().checkout(kid)
    assert (k.token, k.token_superseded) == (recorded, True)
    assert [x.id for x in lab.model().checkouts() if x.token == foreign] == [oid]
    (row,) = _reminted_rows(lab, res.rows)
    assert (row["status"], row["written"]) == ("exists", False)


@pytest.mark.parametrize("found", ["foreign", "sibling"])
def test_a_re_mint_reported_after_the_reply_records_only_its_own_lineage(
    tmp_path: Path, lab: Lab, found: str
) -> None:
    """A caller that re-mints after the reply reports what it found
    (`Membership.token_written`). Another re-mint of the checkout's token,
    found in place, is recorded as the checkout's; any other token found
    is not, and the checkout stays superseded, to be asked again
    (ENG-205)."""
    line = make_repo(tmp_path / "line", commits=2)
    lab.write("l1", line)
    copy = make_repo(tmp_path / "copy", commits=2)
    _carrying_the_token_of(line, copy)
    kid = _resolve_unwritten(lab, copy).checkout
    assert kid is not None
    k = lab.model().checkout(kid)
    assert k.lineage is not None
    there = (
        checkout_token.mint()
        if found == "foreign"
        else checkout_token.successor(k.lineage)
    )

    lab.model().token_written(kid, there, "exists")

    after = lab.model().checkout(kid)
    if found == "foreign":
        assert (after.token, after.token_superseded) == (k.token, True)
    else:
        assert (after.token, after.token_status) == (there, "exists")


def _the_hook_client(
    box: Client,
    lab: Lab,
    monkeypatch: pytest.MonkeyPatch,
    session: str,
    when_asked_to_re_mint: Callable[[], None],
) -> Resolution:
    """The hook client's own rounds (`_daemon_client._membership_rounds`) at
    the container's checkout, each post handed to `location.resolve_hook`
    as the container's: its location token as `Container.posted` writes
    it, its machine the container's, and its answers for views at its own
    mount ``live`` (as `Container.answer` reads them; this host cannot
    read the mount's path). `when_asked_to_re_mint` runs once, when the
    daemon first asks for the re-mint, before the client answers. The
    exchange's resolution."""
    replies: list[Resolution | location.Exchange] = []

    def post(_port: Any, _token: Any, _path: str, payload: Any, **_kw: Any) -> Any:
        posted: dict[str, Any] = {
            "location": box.posted(copy.deepcopy(payload["location"])),
            "machine": location.identity_to_json(box.identity),
        }
        if "answers" in payload:
            answers = copy.deepcopy(payload["answers"])
            views = answers.get("views") or {}
            for path in views:
                if path == box.mount or path.startswith(box.mount + "/"):
                    views[path] = "live"
            posted["answers"] = answers
        asked_before = any(
            isinstance(r, location.Exchange) and r.question.get("remint")
            for r in replies
        )
        reply = location.resolve_hook(
            lab.store, lab.config, box.mount, posted, session=session
        )
        replies.append(reply)
        if not isinstance(reply, location.Exchange):
            return {"membership": {}}
        if reply.question.get("remint") and not asked_before:
            when_asked_to_re_mint()
        return {"membership": {"ask": dict(reply.question)}}

    monkeypatch.setattr(_daemon_client, "post", post)
    location.forget()
    ask = {"location": True, "machine": True, "write_token": True}
    _daemon_client._membership_rounds(
        {"port": 0, "token": "t"},
        "prompt",
        {"cwd": str(box.real)},
        {"membership": {"ask": ask}},
    )
    assert isinstance(replies[-1], Resolution), replies[-1]
    return replies[-1]


def test_a_hook_client_finding_another_sessions_re_mint_stays_its_checkout(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook client's own rounds (`_daemon_client._membership_rounds`) in
    the overlap of `test_two_exchanges_re_minting_one_checkout_leave_it_one_
    token`: its exchange is asked to re-mint a container's superseded
    checkout just after another session's exchange re-minted it and was
    recorded. The client's re-mint finds that one and writes nothing, and
    it posts a fresh capture: its round resolves to the checkout, with no
    second checkout and no question, and the store and the file hold the
    first session's token."""
    box, kid, project = _separated_in_a_container(tmp_path, lab, "c")
    asked = _questions(lab)
    first = _Rounds(box, lab, "s-first")
    first.to_the_re_mint()

    def the_first_re_mints_and_is_recorded() -> None:
        first.post(first.answer())

    reply = _the_hook_client(
        box, lab, monkeypatch, "s-client", the_first_re_mints_and_is_recorded
    )

    assert isinstance(first.reply, Resolution) and first.reply.checkout == kid
    assert (reply.checkout, reply.project, reply.outcome) == (kid, project, SILENT)
    k = lab.model().checkout(kid)
    assert (k.token, k.token_superseded) == (_file_token(box.real), False)
    mine = [x for x in lab.model().checkouts() if x.machine == box.identity.id]
    assert [x.id for x in mine] == [kid]
    assert _questions(lab) == asked


def test_a_checkout_restored_at_its_path_after_a_missed_re_mint_is_rekeyed(
    tmp_path: Path, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B for a checkout whose re-mint the store missed while it records the
    token as current: a declaration's re-mint is cut off before its commit,
    then the checkout is restored at its path as a new object (a backup
    restored). The token there names the recorded one as the token it
    replaced in the checkout's git directory, so it is no other lineage's
    (`Checkout.token_refutes` reads it as `location.continuity` does) and
    the checkout is re-keyed silently, not a second one registered."""
    if not birth_reported(tmp_path):
        pytest.skip("B needs birth time to tell the new object (design 2.2)")
    me = location.process_machine()
    repo = make_repo(tmp_path / "repo", commits=2)
    kid = lab.write("r1", repo, identity=me).checkout
    assert kid is not None
    _split_cut_off(lab, kid, monkeypatch)
    backup = tmp_path / "repo.bak"
    shutil.copytree(repo, backup, symlinks=True)
    remove(repo)
    os.rename(backup, repo)
    location.forget()

    again = lab.write("r2", repo, identity=me)

    assert (again.checkout, again.outcome) == (kid, SILENT)
    assert again.rule.startswith("rekeyed:B")
    assert lab.model(me).checkout(kid).token == _file_token(repo)
