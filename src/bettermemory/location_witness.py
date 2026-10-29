"""The witness exchange's messages (design 8.7): `NeedWitnesses`, the
question the daemon raises when the membership model asks of a
repository it cannot read, and the readers that answer the model from
what the client sent instead: `AnsweredHistory` (the history),
`AnsweredToken` (the checkout token) and `AnsweredLiveness` (a view's
liveness).

They live apart from `location.py` to keep that module under the
package's 2,000-line bound (ENG-96). They import nothing from
`location`: the standard library, `checkout_token` and `location_git`'s
hash pattern, with the membership types they annotate under
TYPE_CHECKING. `location` imports this module and re-exports all four
under their old names, so a hook loads it only with `location`, for the
exchange.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from . import checkout_token
from .location_git import _FULL_HASH

if TYPE_CHECKING:
    from .membership import Checkout, View


class NeedWitnesses(Exception):
    """A question the model asked of a repository the daemon cannot read:
    `question` names what the client is to answer."""

    def __init__(self, question: Mapping[str, Any]) -> None:
        super().__init__("the client's repository must answer")
        self.question = dict(question)


class AnsweredHistory:
    """A `membership_records.HistoryReader` over the client's answers: each
    question it holds an answer for is answered, any other raises
    `NeedWitnesses` naming it, so the daemon reads none of the client's
    paths. The first question for held commits names every witness id the
    caller passed, so one round answers the model's one batch (design
    3.2)."""

    def __init__(self, answers: Mapping[str, Any]) -> None:
        self._asked = {str(x).lower() for x in answers.get("asked") or ()}
        self._held = {str(x).lower() for x in answers.get("held") or ()}
        raw_walk = answers.get("walk")
        self._walked = isinstance(raw_walk, Mapping)
        self._walk: dict[str, tuple[str | None, tuple[str, ...]]] = {}
        if isinstance(raw_walk, Mapping):
            for sha, pair in raw_walk.items():
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    fp = pair[0] if isinstance(pair[0], str) else None
                    parents = tuple(str(p).lower() for p in pair[1] or ())
                    self._walk[str(sha).lower()] = (fp, parents)
        self._ancestry_of = {str(x).lower() for x in answers.get("ancestry_of") or ()}
        self._ancestry = {
            (str(a).lower(), str(d).lower()) for a, d in (answers.get("ancestry") or ())
        }
        raw_blobs = answers.get("blobs")
        self._blobs: dict[str, tuple[str, ...]] = {}
        if isinstance(raw_blobs, Mapping):
            for sha, ids in raw_blobs.items():
                self._blobs[str(sha).lower()] = tuple(str(i).lower() for i in ids or ())

    def _ids(self, ids: Sequence[str]) -> set[str]:
        wanted = {i.lower() for i in ids}
        missing = sorted(wanted - self._asked)
        if missing:
            raise NeedWitnesses({"held": missing})
        return wanted & self._held

    def holds(self, commits: Sequence[str]) -> set[str]:
        return self._ids(commits)

    def holds_objects(self, ids: Sequence[str]) -> set[str]:
        return self._ids(ids)

    def _walk_entry(self, commit: str) -> tuple[str | None, tuple[str, ...]]:
        sha = commit.lower()
        hit = self._walk.get(sha)
        if hit is not None:
            return hit
        raise NeedWitnesses({"walk": True, "commits": [sha]})

    def fingerprint(self, commit: str) -> str | None:
        return self._walk_entry(commit)[0]

    def parents(self, commit: str) -> Sequence[str]:
        return self._walk_entry(commit)[1]

    def reachable_among(self, commits: Sequence[str]) -> dict[str, frozenset[str]]:
        """The client's `reachable_among` over the commits it was asked
        about; commits it was not asked about raise `NeedWitnesses`
        naming them with those it was, so its next answer covers both."""
        wanted = {c.lower() for c in commits if _FULL_HASH.fullmatch(c.lower())}
        if not wanted <= self._ancestry_of:
            raise NeedWitnesses({"ancestry": sorted(self._ancestry_of | wanted)})
        return {
            c: frozenset(d for a, d in self._ancestry if a == c and d in wanted)
            for c in wanted
        }

    def blobs(self, commit: str) -> Sequence[str]:
        sha = commit.lower()
        hit = self._blobs.get(sha)
        if hit is not None:
            return hit
        raise NeedWitnesses({"blobs": [sha]})


class AnsweredToken:
    """The `membership_records.TokenWriter` of a caller the daemon cannot write
    for (ENG-153): the checkout token the client re-minted in its own
    namespace when asked (`answer`, ``remint``), which the resolution
    records in its one transaction and reply. A client not yet asked is
    asked (`NeedWitnesses`); one that answered without saying it can
    re-mint (a client before 9.0.0rc14) re-mints after the final reply as
    it did (None: `Resolution.remint_token`). `posted` says whether the
    client has answered any question in this exchange."""

    def __init__(self, answers: Mapping[str, Any], *, posted: bool) -> None:
        self._answers = answers
        self._posted = posted

    def __call__(self, checkout: Checkout) -> tuple[str | None, str] | None:
        got = self._answers.get("remint")
        if isinstance(got, Mapping):
            token = got.get("token")
            status = got.get("status")
            return (
                token if checkout_token.valid(token) else None,
                status if isinstance(status, str) and status else "failed:io",
            )
        if self._posted and not self._answers.get("remint_ok"):
            return None
        raise NeedWitnesses({"remint": True})


class AnsweredLiveness:
    """The `membership_records.Liveness` of a caller the daemon cannot read:
    each view's state as the client answered it (`view_state` in its own
    namespace); a view not answered raises `NeedWitnesses`."""

    def __init__(self, answers: Mapping[str, Any]) -> None:
        raw = answers.get("views")
        self._states: dict[str, str] = {}
        if isinstance(raw, Mapping):
            self._states = {str(k): str(v) for k, v in raw.items()}

    def __call__(self, checkout: Checkout, view: View) -> str:
        hit = self._states.get(view.path)
        if hit is not None:
            return hit
        key = checkout.object_key
        raise NeedWitnesses(
            {
                "views": [
                    {
                        "path": view.path,
                        "kind": checkout.kind,
                        "object": None
                        if key is None
                        else [key.device, key.inode, key.birth],
                        "token": checkout.token,
                        "last_head": checkout.last_head,
                        "stamps": [list(s) for s in checkout.stamps],
                        "common_object": None
                        if checkout.common_object_key is None
                        else [
                            checkout.common_object_key.device,
                            checkout.common_object_key.inode,
                            checkout.common_object_key.birth,
                        ],
                    }
                ]
            }
        )
