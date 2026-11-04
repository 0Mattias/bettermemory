"""The witness exchange's messages (design 8.7): `NeedWitnesses`, the
question the daemon raises when the membership model asks of a
repository it cannot read, and the readers that answer the model from
what the client sent instead: `AnsweredHistory` (the history),
`AnsweredToken` (the checkout token, and the location of a client whose
re-mint the store already recorded) and `AnsweredLiveness` (a view's
liveness); and the client's reading of a view question (`asked_views`),
its answer to a re-mint question (`remint_answer`) and whether that
answer found another session's re-mint (`remint_refused`), and its
answer to a question for git's reading of tied witnesses
(`independent_answer`); and what the daemon read itself for one
exchange, kept from round to round (`ExchangeReads`).

They live apart from `location.py` to keep that module under the
package's 2,000-line bound (ENG-96). They import nothing from
`location`: the standard library, `checkout_token` and `location_git`'s
hash pattern, kinds, bounds, `Capture`, history reader and object-key
reader, with the membership types they annotate under TYPE_CHECKING.
`location` imports
this module and re-exports the four readers under their old names, so a
hook loads it only with `location`, for the exchange.
"""

from __future__ import annotations

import dataclasses
import re
import secrets
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from . import checkout_token
from .location_git import (
    _FULL_HASH,
    BARE,
    INDEPENDENT_MAX,
    PRIMARY,
    Capture,
    Reach,
    RepositoryHistory,
    _key,
)

if TYPE_CHECKING:
    from .membership import Checkout, Location, Membership, View


class NeedWitnesses(Exception):
    """A question the model asked of a repository the daemon cannot read:
    `question` names what the client is to answer."""

    def __init__(self, question: Mapping[str, Any]) -> None:
        super().__init__("the client's repository must answer")
        self.question = dict(question)

    @classmethod
    def joined(cls, needs: Sequence[NeedWitnesses]) -> NeedWitnesses:
        """One question asking all that each of `needs` asks, so the client
        answers them in one round (`location.answer`), not one each (review
        M G4): each list a key names (the views, the held ids, the commits)
        holds every item any of them names, once, in the order first named
        (an item with a path, a view, by its path, the first asked kept, as
        the client answers a path once; any other item by itself), and any
        other value (a flag) is the first named."""
        question: dict[str, Any] = {}
        seen: dict[str, set[tuple[str, str]]] = {}
        for need in needs:
            for name, value in need.question.items():
                held = question.setdefault(
                    name, [] if isinstance(value, list) else value
                )
                if not isinstance(value, list) or not isinstance(held, list):
                    continue
                marks = seen.setdefault(name, set())
                for item in value:
                    if isinstance(item, Mapping) and "path" not in item:
                        if item not in held:
                            held.append(item)
                        continue
                    mark = (
                        ("path", repr(item["path"]))
                        if isinstance(item, Mapping)
                        else ("item", repr(item))
                    )
                    if mark not in marks:
                        marks.add(mark)
                        held.append(item)
        return cls(question)


class AnsweredHistory:
    """A `membership_records.HistoryReader` over the client's answers: each
    question it holds an answer for is answered, any other raises
    `NeedWitnesses` naming it, so the daemon reads none of the client's
    paths. The first question for held commits names every witness id the
    caller passed, so one round answers the model's one batch (design
    3.2). It is a `membership_records.IndependentReader`: git's reading
    of tied witnesses is asked of a client that says it answers one
    (`independent`, ENG-216)."""

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
        # A client before 9.0.0rc18 does not say: read as cut short.
        self._ancestry_bounded = answers.get("ancestry_bounded") is not False
        raw_blobs = answers.get("blobs")
        self._blobs: dict[str, tuple[str, ...]] = {}
        if isinstance(raw_blobs, Mapping):
            for sha, ids in raw_blobs.items():
                self._blobs[str(sha).lower()] = tuple(str(i).lower() for i in ids or ())
        # A client before 9.0.0rc19 does not say: the traversal decides.
        self._independent_ok = answers.get("independent_ok") is True
        self._independent = _independent_answers(answers.get("independent"))

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

    def reachable_among(self, commits: Sequence[str]) -> Reach:
        """The client's `reachable_among` over the commits it was asked
        about, with whether its bound cut it short (ENG-203); commits it
        was not asked about raise `NeedWitnesses` naming them with those
        it was, so its next answer covers both."""
        wanted = {c.lower() for c in commits if _FULL_HASH.fullmatch(c.lower())}
        if not wanted <= self._ancestry_of:
            raise NeedWitnesses({"ancestry": sorted(self._ancestry_of | wanted)})
        return Reach(
            {
                c: frozenset(d for a, d in self._ancestry if a == c and d in wanted)
                for c in wanted
            },
            bounded=self._ancestry_bounded,
        )

    def blobs(self, commit: str) -> Sequence[str]:
        sha = commit.lower()
        hit = self._blobs.get(sha)
        if hit is not None:
            return hit
        raise NeedWitnesses({"blobs": [sha]})

    def independent(self, commits: Sequence[str]) -> frozenset[str] | None:
        """The commits of `commits` (full ids) no other of them reaches, as
        the client's ``git merge-base --independent`` named them in its own
        namespace (`independent_answer`, ENG-216): the call the daemon
        makes where it reads the repository
        (`location_git.RepositoryHistory.independent`). A set not answered
        yet raises `NeedWitnesses` naming it; the resolution asks before
        its transaction (`membership_resolve.resolve`), so its round ends
        there. None, and the bounded traversal decides as before, for a
        client before 9.0.0rc19 (no ``independent_ok``), where the
        client's git did not answer, and for more than `INDEPENDENT_MAX`
        commits, which are never asked."""
        given = tuple(
            sorted({c.lower() for c in commits if _FULL_HASH.fullmatch(c.lower())})
        )
        if len(given) < 2:
            return frozenset(given)
        if len(given) > INDEPENDENT_MAX or not self._independent_ok:
            return None
        if given in self._independent:
            return self._independent[given]
        raise NeedWitnesses({"independent": list(given)})


def _independent_answers(raw: Any) -> dict[tuple[str, ...], frozenset[str] | None]:
    """The client's answers to the daemon's ``independent`` questions, by
    the commits asked: each ``{"of": [...], "top": [...] or null}``, where a
    ``top`` that names no commit, or one not asked, is no answer (None), as
    the daemon reads its own git's (`RepositoryHistory.independent`).
    Malformed entries are left out."""
    out: dict[tuple[str, ...], frozenset[str] | None] = {}
    for entry in raw if isinstance(raw, list) else ():
        if not isinstance(entry, Mapping) or not isinstance(entry.get("of"), list):
            continue
        of = tuple(sorted({str(c).lower() for c in entry["of"]}))
        top = entry.get("top")
        named = (
            frozenset(str(c).lower() for c in top) if isinstance(top, list) else None
        )
        out[of] = named if named and named <= set(of) else None
    return out


def independent_answer(
    question: Mapping[str, Any],
    answers: Mapping[str, Any],
    history: RepositoryHistory | None,
) -> dict[str, Any]:
    """What the client adds to its answers for git's reading of tied
    witnesses (ENG-216): ``independent_ok``, which says it answers the
    question, and, where the question names commits (``independent``), the
    ones ``merge-base --independent`` names among them, read in its own
    namespace by the call the daemon makes on a repository it reads
    (`RepositoryHistory.independent`: no replace refs, commit-graph or
    grafts, no fetch, one second, at most `INDEPENDENT_MAX` ids), kept
    beside the answers of earlier rounds. Git runs only when asked, which
    is when a resolution ties; ``top`` is null where git did not answer or
    no repository is here, and the daemon's traversal decides."""
    out: dict[str, Any] = {"independent_ok": True}
    kept = [e for e in answers.get("independent") or () if isinstance(e, Mapping)]
    asked = question.get("independent")
    if isinstance(asked, list) and asked:
        of = sorted({str(c).lower() for c in asked})
        top = None if history is None else history.independent(of)
        kept = [e for e in kept if e.get("of") != of]
        kept.append({"of": of, "top": None if top is None else sorted(top)})
    if kept:
        out["independent"] = kept
    return out


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
        # The lineage the re-mint replaces, as the store records it, which
        # the new token names (`remint_answer`, ENG-205); a client before
        # 9.0.0rc18 skips the key and mints a token that names nothing.
        lineage = checkout.lineage
        raise NeedWitnesses(
            {"remint": True, "replacing": None if lineage is None else list(lineage)}
        )

    def located(self, loc: Location, model: Membership) -> Location:
        """`loc` holding the token the client wrote in this exchange (its
        re-mint answer) in place of the one its capture read, where a
        checkout of its machine at its object already records that token:
        another session's capture of the git directory recorded the
        re-mint before this round arrived, and the capture is older than
        the file (ENG-205). Otherwise `loc` as posted, so a round whose
        store still records the token replaced records the re-mint as the
        question asked (`__call__`)."""
        got = self._answers.get("remint")
        token = got.get("token") if isinstance(got, Mapping) else None
        key = loc.object_key
        if (
            not isinstance(got, Mapping)
            or got.get("status") != "written"
            or not checkout_token.valid(token)
            or token == loc.token
            or key is None
        ):
            return loc
        if not any(
            k.machine == loc.machine
            and k.object_key is not None
            and (k.object_key.device, k.object_key.inode) == (key.device, key.inode)
            for k in model.checkouts(token=token)
        ):
            return loc
        return dataclasses.replace(
            loc, token=token, token_written=None, token_status="present"
        )


#: A nonce `ExchangeReads` names an exchange by: 128 bits, as hex.
_NONCE = re.compile(r"[0-9a-f]{32}")
#: The exchanges whose reads wait for the client's next round, oldest
#: dropped first past `_EXCHANGES_CAP`: as many as the captures kept
#: (`location_git.CACHE_CAP`, 256, under 1 MB as JSON), each holding a
#: folder path or a few, far smaller than a capture. A name of its own, so
#: a test's patch of the captures' bound does not reach it.
_EXCHANGES_CAP = 256
_EXCHANGES: OrderedDict[str, dict[str, str | None]] = OrderedDict()
_EXCHANGES_LOCK = threading.Lock()


class ExchangeReads(dict[str, str | None]):
    """What the daemon read in its own namespace for one witness exchange,
    kept from one round to the next (ENG-213): the declared folders
    `membership_resolve.prefetch_declared` read, by folder (None where the
    read gave no spelling), which a round takes as read, so each is read
    once an exchange, not once a round. A round whose question asks again
    names the exchange (``exchange``, a nonce the daemon mints, `carried`),
    and the client names it back with its answers (`location.answer`); the
    round that finds it takes the reads out of the daemon's keeping and
    puts them back only when it asks again. So they never outlive the
    exchange: a resolution ends it, a new prompt's exchange has a nonce of
    its own and reads afresh, and one the client left (out of rounds, or
    its post failed) is crowded out, never named again. A client before
    9.0.0rc19 names none back, and each of its rounds reads as before."""

    def __init__(self, answers: Mapping[str, Any]) -> None:
        named = answers.get("exchange")
        got: dict[str, str | None] | None = None
        if isinstance(named, str) and _NONCE.fullmatch(named):
            with _EXCHANGES_LOCK:
                got = _EXCHANGES.pop(named, None)
        super().__init__(got or {})
        self.nonce: str = (
            named
            if isinstance(named, str) and got is not None
            else secrets.token_hex(16)
        )

    def carried(self) -> dict[str, str]:
        """Keep the reads for the exchange's next round, and the question's
        key naming them; nothing where nothing was read."""
        if not self:
            return {}
        with _EXCHANGES_LOCK:
            _EXCHANGES[self.nonce] = dict(self)
            while len(_EXCHANGES) > _EXCHANGES_CAP:
                _EXCHANGES.popitem(last=False)
        return {"exchange": self.nonce}


def remint_answer(cap: Capture, question: Mapping[str, Any]) -> dict[str, Any]:
    """The client's answer to the re-mint question `AnsweredToken` asks
    (ENG-153): the checkout token of its capture re-minted under the
    question's policy, naming the lineage the question names (the store's
    record of the checkout; none where it names none) or, from a daemon
    before 9.0.0rc18, whose question has no ``replacing``, the capture's
    own (ENG-205); and the token and status written. Where the file holds
    another token than the one replaced (another session re-minted the
    checkout first), nothing is written and that token is answered as
    ``exists`` (`checkout_token.remint`, `remint_refused`)."""
    if not cap.git_dir:
        return {"token": None, "status": "skipped:no_git_dir"}
    if "replacing" in question:
        replacing = checkout_token.Lineage.read(question["replacing"])
    else:
        replacing = checkout_token.lineage(
            cap.token or cap.token_written, cap.object_key
        )
    wrote = checkout_token.remint(
        cap.git_dir,
        policy=bool(question.get("write_token")),
        bare=cap.kind == BARE,
        replacing=replacing,
    )
    return {"token": wrote.token, "status": wrote.status}


def remint_refused(question: Mapping[str, Any], answers: Mapping[str, Any]) -> bool:
    """Whether the client's answer to this round's re-mint question found
    another token in place of the one it replaces and wrote nothing
    (`remint_answer`, ``exists``): another session re-minted the checkout
    first. The client then posts a fresh capture, which holds that token,
    with its answers, so its round reads the git directory as it is
    (`_daemon_client._membership_rounds`, ENG-205)."""
    got = answers.get("remint")
    return (
        bool(question.get("remint"))
        and isinstance(got, Mapping)
        and got.get("status") == "exists"
    )


class AnsweredLiveness:
    """The `membership_records.Liveness` of a caller the daemon cannot read:
    each view's state as the client answered it (`view_state` in its own
    namespace); a view not answered raises `NeedWitnesses` naming it,
    which a resolution's prefetch joins with those of the other views it
    plans (`membership_resolve.prefetch_liveness`)."""

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
                        # Read by `location.view_state` as `continuity`
                        # reads a superseded token (ENG-193); a client
                        # before 9.0.0rc18 skips the key and reads the
                        # token as recorded, as it did.
                        "superseded": checkout.token_superseded,
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


def asked_views(question: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Each view `question` names, as `AnsweredLiveness` asks it: its path
    and the keyword arguments the client reads it with in its own
    namespace (`location.view_state`), a malformed field read as absent
    and a malformed object key raising `ValueError`."""
    out: list[tuple[str, dict[str, Any]]] = []
    for v in question.get("views") or ():
        if not isinstance(v, Mapping) or not isinstance(v.get("path"), str):
            continue
        token = v.get("token")
        out.append(
            (
                v["path"],
                {
                    "kind": str(v.get("kind") or PRIMARY),
                    "key": _key(v.get("object")),
                    "token": token if isinstance(token, str) else None,
                    "last_head": _asked_head(v.get("last_head")),
                    "stamps": _asked_stamps(v.get("stamps")),
                    "common_key": _key(v.get("common_object")),
                    "superseded": v.get("superseded") is True,
                },
            )
        )
    return out


def _asked_head(value: Any) -> str | None:
    """A last HEAD a view question names, when it is a full hash."""
    if isinstance(value, str) and _FULL_HASH.fullmatch(value.lower()):
        return value.lower()
    return None


def _asked_stamps(value: Any) -> tuple[tuple[str, int, int, int, int], ...]:
    """The init stamps a view question names, the malformed ones left out."""
    out: list[tuple[str, int, int, int, int]] = []
    if isinstance(value, list):
        for s in value[:8]:
            if not isinstance(s, (list, tuple)) or len(s) != 5:
                continue
            try:
                out.append((str(s[0]), int(s[1]), int(s[2]), int(s[3]), int(s[4])))
            except (TypeError, ValueError):
                continue
    return tuple(out)
