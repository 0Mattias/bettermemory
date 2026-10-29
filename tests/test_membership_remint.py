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

The tests drive `location.resolve_caller` and `location.resolve_hook` as
the daemon runs them, with no token writer given; the container is played
on this host (`tests.test_membership_resolution.Container`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bettermemory import checkout_token, location
from bettermemory.declare import declaring_model
from bettermemory.membership import SILENT, SUPERSEDED, Declaration, Resolution

from .test_membership_resolution import (
    Container,
    Lab,
    _container_machine,
    lab,
    lab_of,
    make_repo,
    neutral_git,
    pytestmark,
)

__all__ = ["lab", "lab_of", "neutral_git", "pytestmark"]


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
    superseded until a new token is seen."""
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
