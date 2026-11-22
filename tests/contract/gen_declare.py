"""Fixtures: the membership model a declaration runs on
(`declare.declaring_model`, 19-47).

The reference runs with `location.process_machine` answering a fixed
identity, `Membership.open` replaced by a recorder of what it is given,
and `checkout_token.remint` replaced by a recorder that answers by a
fixed rule: with the policy on, the token ``tok:`` and the git directory,
status ``written``; with it off, no token, status ``skipped:policy``. The
token writer the model is given is then called over checkouts chosen to
cover its branches: no git directory, an empty one, another machine, a
bare kind and kinds that only resemble it, a lineage that is set and one
that is malformed (no object key, a device that is no decimal, a token
that is no token).

One file, ``declare.jsonl``, not gated: no row depends on the Python
release, the platform or a library version.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import checkout_token, declare, location
from bettermemory import membership as mm
from bettermemory import membership_records as mr

from ._common import jsonl

_TOKEN = "0123456789ABCDEFGHJKMNPQRS"
_HERE = "M-here"


def _checkout(**kw: Any) -> mr.Checkout:
    base: dict[str, Any] = {
        "id": "k1",
        "project": "p1",
        "machine": _HERE,
        "kind": "primary",
        "object_key": mr.ObjectKey("10", "20", "30"),
        "common_object_key": None,
        "git_dir": "/r/.git",
        "token": _TOKEN,
        "token_status": "own",
        "stamps": (),
        "last_head": None,
        "branch": None,
        "remotes": (),
        "manifest": None,
        "legacy": False,
        "retired": False,
        "registered": "t0",
    }
    base.update(kw)
    return mr.Checkout(**base)


def _cases() -> list[dict[str, Any]]:
    k = mr.ObjectKey
    return [
        {},
        {"git_dir": None},
        {"git_dir": ""},
        {"machine": "M-other"},
        {"machine": "M-other", "git_dir": None},
        {"machine": ""},
        {"machine": "m-here"},
        {"machine": _HERE + " "},
        {"kind": "bare"},
        {"kind": "Bare"},
        {"kind": "bare "},
        {"kind": "worktree"},
        {"kind": ""},
        {"token": None},
        {"token": ""},
        {"token": "not a token"},
        {"token": _TOKEN.lower()},
        {"object_key": None},
        {"object_key": k("x10", "20", None)},
        {"object_key": k("10", "", None)},
        {"object_key": k("0", "0", None)},
        {"object_key": k("18446744073709551616", "99999999999999999999", "b")},
        {"git_dir": "/r\u00e9/\U0001f600/.git", "kind": "bare"},
        {"git_dir": "C:\\r\\.git"},
    ]


def _key(key: mr.ObjectKey | None) -> Any:
    return None if key is None else [key.device, key.inode, key.birth]


def _ck(c: mr.Checkout) -> dict[str, Any]:
    return {
        "machine": c.machine,
        "kind": c.kind,
        "git_dir": c.git_dir,
        "token": c.token,
        "object_key": _key(c.object_key),
    }


def _run(
    policy: bool, session: str | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    calls: list[list[Any]] = []

    def remint(git_dir: str, **kw: Any) -> Any:
        calls.append(
            [
                git_dir,
                kw["policy"],
                kw["bare"],
                None if kw["replacing"] is None else list(kw["replacing"]),
                kw["only_siblings"],
            ]
        )
        if kw["policy"]:
            return SimpleNamespace(token="tok:" + git_dir, status="written")
        return SimpleNamespace(token=None, status="skipped:policy")

    opened: dict[str, Any] = {}

    def open_(store: Any, identity: Any, *, token_writer: Any, session: Any) -> str:
        opened.update(
            store=store, identity=identity.id, writer=token_writer, session=session
        )
        return "model"

    identity = SimpleNamespace(id=_HERE)
    config = SimpleNamespace(membership=SimpleNamespace(write_token=policy))
    with (
        mock.patch.object(location, "process_machine", lambda: identity),
        mock.patch.object(checkout_token, "remint", remint),
        mock.patch.object(mm.Membership, "open", staticmethod(open_)),
    ):
        model = declare.declaring_model("store", config, session=session)  # type: ignore[arg-type]
        head = {
            "row": "open",
            "policy": policy,
            "session": session,
            "answer": model,
            "opened": {
                "store": opened["store"],
                "identity": opened["identity"],
                "session": opened["session"],
            },
        }
        rows = []
        for case in _cases():
            c = _checkout(**case)
            calls.clear()
            token, status = opened["writer"](c)
            rows.append(
                {
                    "row": "write",
                    "policy": policy,
                    "checkout": _ck(c),
                    "answer": [token, status],
                    "remint": list(calls),
                }
            )
    return head, rows


def generate() -> dict[str, bytes]:
    out: list[dict[str, Any]] = []
    for policy, session in ((True, None), (False, "s-1")):
        head, rows = _run(policy, session)
        out.append(head)
        out.extend(rows)
    return {"declare.jsonl": jsonl(out)}
