"""Fixtures: a hook's caller resolved from what it posted
(``location_hook.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location_hook.rs``. This pins it to
`location.resolve_hook`, driven with recording fakes.

Nothing reads the machine. `_hooked`, ``os.getcwd``, `object_key`,
`_hostname`, `machine.open_machine` and `membership.Membership` are the
case's; each records its call in ``calls``, in order. The nonce an
exchange mints (`secrets.token_hex(16)`) is ``ab`` sixteen times, and the
kept exchanges are cleared before each case.

Each line holds ``k`` (the case's name) and ``rounds``: each round's
``posted`` (a `neutral` value, or null), ``cwd``, ``session``, ``wt``,
``strict``, and what the doubles answer (``open``, ``record``, ``res``,
``need``, ``reads``, ``checkouts``, ``here``, ``local`` and
``need`` (both `neutral`),
``g``); and ``r``:
per round the calls, then ``res`` (the resolution's fields), ``ask`` (the
exchange's question, `neutral`) or ``raised`` (the exception's class),
then the stands kept, as [session, machine, path, checkout].
"""

from __future__ import annotations

import os
import secrets
import types
from typing import Any, cast
from unittest import mock

from bettermemory import location as L
from bettermemory import location_witness as LW
from bettermemory import machine as M
from bettermemory import membership as MB
from bettermemory.location_git import Capture
from bettermemory.membership_records import Location, Resolution

from ._common import jsonl, neutral


class _Other(Exception):
    pass


def _res(spec: dict[str, Any]) -> Resolution:
    return Resolution(
        outcome=spec["o"],
        project=spec.get("p"),
        checkout=spec.get("c"),
        rule=spec.get("rule", "r"),
        lines=tuple(spec.get("lines", ())),
    )


def _res_json(res: Resolution) -> dict[str, Any]:
    return {
        "o": res.outcome,
        "p": res.project,
        "c": res.checkout,
        "rule": res.rule,
        "lines": list(res.lines),
    }


def _loc_json(loc: Location) -> list[Any]:
    key = loc.object_key
    return [
        loc.machine,
        loc.cwd,
        loc.state,
        loc.detail,
        loc.session,
        loc.git_dir,
        loc.token,
        loc.token_status,
        None if key is None else [key.device, key.inode, key.birth],
    ]


def _raise(kind: str | None) -> None:
    if kind == "os":
        raise OSError(2, "no state")
    if kind == "other":
        raise _Other


class _Model:
    def __init__(self, rnd: dict[str, Any], calls: list[Any]) -> None:
        self.rnd = rnd
        self.calls = calls
        self.machine = ""

    def make(
        self,
        store: Any,
        machine: str,
        *,
        liveness: Any = None,
        token_writer: Any = None,
        session: str | None = None,
    ) -> _Model:
        self.calls.append(
            ["model", machine, session, liveness is not None, token_writer is not None]
        )
        _raise(self.rnd.get("make"))
        self.machine = machine
        return self

    def record_machine(self, identity: Any) -> None:
        self.calls.append(["record", identity.id])
        _raise(self.rnd.get("record"))

    def checkouts(self, *, token: str) -> list[Any]:
        self.calls.append(["checkouts", token])
        out = []
        for machine, key in self.rnd.get("checkouts", []):
            ok = None if key is None else MB.ObjectKey(key[0], key[1], None)
            out.append(types.SimpleNamespace(machine=machine, object_key=ok))
        return out

    def resolve(
        self, loc: Location, *, strict: bool, declared: Any = None
    ) -> Resolution:
        held = None if declared is None else sorted(declared.items())
        self.calls.append(["resolve", _loc_json(loc), strict, held])
        _raise(self.rnd.get("resolve"))
        if declared is not None:
            for folder in self.rnd.get("reads", []):
                declared[folder] = "s:" + folder
        need = self.rnd.get("need")
        if need is not None:
            raise LW.NeedWitnesses(need)
        return _res(self.rnd.get("res", {"o": "silent", "p": "p", "c": "k"}))


def _round(rnd: dict[str, Any]) -> dict[str, Any]:
    calls: list[Any] = []
    model = _Model(rnd, calls)

    def hooked(store: Any, config: Any, cwd: Any, session: Any) -> Resolution:
        calls.append(["hooked", cwd, session])
        _raise(rnd.get("hooked"))
        return _res({"o": "silent", "p": "h", "c": "hk"})

    def getcwd() -> str:
        calls.append(["getcwd"])
        g = rnd.get("g")
        if g is None:
            raise OSError(2, "gone")
        return str(g)

    def object_key(path: str) -> Any:
        calls.append(["key", path])
        here = rnd.get("here")
        return None if here is None else tuple(here)

    def hostname() -> str | None:
        calls.append(["host"])
        local = rnd.get("local")
        return None if local is None else str(local)

    def open_machine() -> Any:
        calls.append(["open"])
        if rnd.get("open") is None:
            raise OSError(2, "no state")
        if rnd["open"] == "other":
            raise _Other
        return types.SimpleNamespace(id=rnd["open"])

    mem = types.SimpleNamespace(
        write_token=rnd.get("wt", True), strict=rnd.get("strict", False)
    )
    config = cast(Any, types.SimpleNamespace(membership=mem))
    store = cast(Any, types.SimpleNamespace(path="/s"))
    out: dict[str, Any] = {}
    with (
        mock.patch.object(L, "_hooked", hooked),
        mock.patch.object(os, "getcwd", getcwd),
        mock.patch.object(L, "object_key", object_key),
        mock.patch.object(L, "_hostname", hostname),
        mock.patch.object(M, "open_machine", open_machine),
        mock.patch.object(MB, "Membership", model.make),
    ):
        try:
            got = L.resolve_hook(
                store,
                config,
                rnd.get("cwd"),
                rnd.get("posted"),
                session=rnd.get("session"),
            )
        except (
            _Other,
            OSError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as e:
            out["raised"] = type(e).__name__
        else:
            if isinstance(got, L.Exchange):
                out["ask"] = neutral(dict(got.question))
            else:
                out["res"] = _res_json(got)
    out["stands"] = [
        [k[1], s.machine, s.path, s.resolution.checkout] for k, s in L._STANDS.items()
    ]
    return {"calls": calls, **out}


def _case(name: str, rounds: list[dict[str, Any]]) -> dict[str, Any]:
    L._clear_machine()
    L._clear_stands()
    LW._EXCHANGES.clear()
    with mock.patch.object(secrets, "token_hex", lambda n: "ab" * n):
        results = [_round(r) for r in rounds]
    shown = [
        {
            **r,
            "posted": None if r.get("posted") is None else neutral(r["posted"]),
            **{k: neutral(r[k]) for k in ("local", "need") if k in r},
        }
        for r in rounds
    ]
    return {"k": name, "rounds": shown, "r": results}


def _token(**fields: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "cwd": "/w",
        "state": "repo",
        "root": "/w",
        "kind": "primary",
        "git_dir": "/w/.git",
        "object_key": ("1", "2", None),
        "cwd_key": ("1", "9", "b"),
        "head": "a" * 40,
    }
    spec.update(fields)
    return Capture(**spec).to_token()


MACHINE = {"id": "mc", "facts": {"hardware": "H", "hostname": "n", "id_file": None}}
GOOD = "0123456789ABCDEFGHJKMNPQRS"


def _probe(**extra: Any) -> dict[str, Any]:
    return {"v": 1, "probe": True, "cwd": "/p", "state": "indeterminate", **extra}


def _cases() -> list[dict[str, Any]]:
    tok = _token()
    near = {"v": 1, "probe": True, "cwd": "/w", "cwd_object": ["1", "9", "b"]}
    out = [
        _case("no posted", [{"posted": None, "cwd": "/h", "session": "s"}]),
        _case("no posted, no cwd", [{"posted": None, "g": "/g", "session": "s"}]),
        _case("no posted, getcwd fails", [{"posted": None, "session": "s"}]),
        _case("location not a mapping", [{"posted": {"location": "x"}, "cwd": "/h"}]),
        _case("location a list", [{"posted": {"location": [1]}, "cwd": "/h"}]),
        _case(
            "hooked raises",
            [{"posted": None, "cwd": "/h", "hooked": "other"}],
        ),
        _case(
            "probe indeterminate",
            [
                {
                    "posted": {"location": _probe(detail="dd")},
                    "open": "m1",
                    "session": "s",
                    "res": {"o": "indeterminate", "p": None, "c": "k"},
                }
            ],
        ),
        _case(
            "probe indeterminate, strict, detail not text",
            [{"posted": {"location": _probe(detail=3)}, "open": "m1", "strict": True}],
        ),
        _case(
            "probe indeterminate, empty cwd uses the argument",
            [{"posted": {"location": _probe(cwd="")}, "cwd": "/arg", "open": "m1"}],
        ),
        _case(
            "probe indeterminate, no cwd at all",
            [{"posted": {"location": _probe(cwd=None)}, "open": "m1"}],
        ),
        _case(
            "probe indeterminate, cwd an int",
            [{"posted": {"location": _probe(cwd=7)}, "open": "m1", "session": "s"}],
        ),
        _case(
            "probe indeterminate, cwd a list",
            [{"posted": {"location": _probe(cwd=["a", None])}, "open": "m1"}],
        ),
        _case(
            "probe indeterminate, open fails",
            [{"posted": {"location": _probe()}, "session": "s"}],
        ),
        _case(
            "probe indeterminate, open raises other",
            [{"posted": {"location": _probe()}, "open": "other"}],
        ),
        _case(
            "probe indeterminate, record fails",
            [{"posted": {"location": _probe()}, "open": "m1", "record": "os"}],
        ),
        _case(
            "probe indeterminate, record raises other",
            [{"posted": {"location": _probe()}, "open": "m1", "record": "other"}],
        ),
        _case(
            "probe indeterminate, model raises os",
            [{"posted": {"location": _probe()}, "open": "m1", "make": "os"}],
        ),
        _case(
            "probe indeterminate, resolve raises",
            [{"posted": {"location": _probe()}, "open": "m1", "resolve": "other"}],
        ),
        _case(
            "probe indeterminate twice records once",
            [
                {"posted": {"location": _probe()}, "open": "m1", "session": "s"},
                {
                    "posted": {"location": _probe(cwd="/q")},
                    "open": "m1",
                    "session": "s",
                },
            ],
        ),
        _case(
            "indeterminate without probe",
            [{"posted": {"location": {"state": "indeterminate", "cwd": "/p"}}}],
        ),
        _case(
            "probe in another state",
            [{"posted": {"location": _probe(state="repo")}, "here": None}],
        ),
        _case(
            "same namespace",
            [{"posted": {"location": near}, "here": ["1", "9", "b"], "session": "s"}],
        ),
        _case(
            "same namespace, no birth here",
            [{"posted": {"location": near}, "here": ["1", "9", None]}],
        ),
        _case(
            "other birth",
            [{"posted": {"location": near}, "here": ["1", "9", "c"]}],
        ),
        _case(
            "other inode",
            [{"posted": {"location": near}, "here": ["1", "8", "b"]}],
        ),
        _case(
            "same host",
            [
                {
                    "posted": {"location": {**near, "hostname": "hn"}},
                    "here": ["1", "9", "b"],
                    "local": "hn",
                }
            ],
        ),
        _case(
            "other host",
            [
                {
                    "posted": {"location": {**near, "hostname": "hn"}},
                    "here": ["1", "9", "b"],
                    "local": "ho",
                }
            ],
        ),
        _case(
            "no local host",
            [
                {
                    "posted": {"location": {**near, "hostname": "hn"}},
                    "here": ["1", "9", "b"],
                }
            ],
        ),
        _case(
            "host not text",
            [
                {
                    "posted": {"location": {**near, "hostname": 5}},
                    "here": ["1", "9", "b"],
                    "local": "5",
                }
            ],
        ),
        _case(
            "host with a surrogate",
            [
                {
                    "posted": {"location": {**near, "hostname": "h\udcff"}},
                    "here": ["1", "9", "b"],
                    "local": "h\udcff",
                }
            ],
        ),
        _case(
            "empty cwd",
            [{"posted": {"location": {**near, "cwd": ""}}, "here": ["1", "9", "b"]}],
        ),
        _case(
            "cwd not text",
            [{"posted": {"location": {**near, "cwd": 4}}, "here": ["1", "9", "b"]}],
        ),
        _case(
            "key of two",
            [
                {
                    "posted": {"location": {**near, "cwd_object": ["1", "9"]}},
                    "here": ["1", "9", "b"],
                }
            ],
        ),
        _case(
            "key not a list",
            [{"posted": {"location": {**near, "cwd_object": "1:9"}}}],
        ),
        _case(
            "key of ints",
            [
                {
                    "posted": {"location": {**near, "cwd_object": [1, 9, None]}},
                    "here": ["1", "9", "b"],
                }
            ],
        ),
        _case(
            "no key",
            [{"posted": {"location": {**near, "cwd_object": None}}}],
        ),
        _case(
            "probe asks for the token",
            [
                {
                    "posted": {"location": near, "machine": MACHINE},
                    "here": None,
                    "wt": False,
                }
            ],
        ),
        _case(
            "token without machine",
            [{"posted": {"location": tok}, "here": None}],
        ),
        _case(
            "token with a bad machine",
            [{"posted": {"location": tok, "machine": {"id": 3}}, "here": None}],
        ),
        _case(
            "bad token",
            [
                {
                    "posted": {"location": {**tok, "v": 99}, "machine": MACHINE},
                    "here": None,
                }
            ],
        ),
        _case(
            "token with an infinite stamp",
            [
                {
                    "posted": {
                        "location": {**tok, "stamps": [["a", float("inf")]]},
                        "machine": MACHINE,
                    },
                    "here": None,
                }
            ],
        ),
        _case(
            "token and machine",
            [
                {
                    "posted": {"location": tok, "machine": MACHINE},
                    "here": None,
                    "session": "s",
                }
            ],
        ),
        _case(
            "token resolved strictly, no session",
            [
                {
                    "posted": {"location": tok, "machine": MACHINE},
                    "here": None,
                    "strict": True,
                }
            ],
        ),
        _case(
            "token with file errors",
            [
                {
                    "posted": {
                        "location": _token(file_errors=("e1", "l1", "e1", "\x1bz")),
                        "machine": MACHINE,
                    },
                    "here": None,
                    "session": "s",
                    "res": {"o": "silent", "p": "p", "c": "k", "lines": ["l1"]},
                }
            ],
        ),
        _case(
            "answers not a mapping",
            [
                {
                    "posted": {"location": tok, "machine": MACHINE, "answers": [1]},
                    "here": None,
                }
            ],
        ),
        _case(
            "answers with views",
            [
                {
                    "posted": {
                        "location": tok,
                        "machine": MACHINE,
                        "answers": {"views": {"a": "live"}},
                    },
                    "here": None,
                }
            ],
        ),
        _case(
            "model raises",
            [
                {
                    "posted": {"location": tok, "machine": MACHINE},
                    "here": None,
                    "make": "other",
                }
            ],
        ),
        _case(
            "record raises os on the token path",
            [
                {
                    "posted": {"location": tok, "machine": MACHINE},
                    "here": None,
                    "record": "os",
                }
            ],
        ),
        _case(
            "resolve raises",
            [
                {
                    "posted": {"location": tok, "machine": MACHINE},
                    "here": None,
                    "resolve": "other",
                }
            ],
        ),
        _case(
            "re-mint located",
            [
                {
                    "posted": {
                        "location": tok,
                        "machine": MACHINE,
                        "answers": {"remint": {"token": GOOD, "status": "written"}},
                    },
                    "here": None,
                    "checkouts": [["x", ["1", "2"]], ["mc", ["1", "2"]]],
                }
            ],
        ),
        _case(
            "re-mint held elsewhere",
            [
                {
                    "posted": {
                        "location": tok,
                        "machine": MACHINE,
                        "answers": {"remint": {"token": GOOD, "status": "written"}},
                    },
                    "here": None,
                    "checkouts": [["mc", ["1", "3"]], ["mc", None]],
                }
            ],
        ),
        _case(
            "re-mint not written",
            [
                {
                    "posted": {
                        "location": tok,
                        "machine": MACHINE,
                        "answers": {"remint": {"token": GOOD, "status": "failed"}},
                    },
                    "here": None,
                    "checkouts": [["mc", ["1", "2"]]],
                }
            ],
        ),
        _case(
            "need witnesses, nothing read",
            [
                {
                    "posted": {"location": tok, "machine": MACHINE},
                    "here": None,
                    "need": {"heads": ["h"], "write_token": "x"},
                    "wt": False,
                }
            ],
        ),
        _case(
            "need witnesses, read",
            [
                {
                    "posted": {"location": tok, "machine": MACHINE},
                    "here": None,
                    "need": {"heads": ["h"]},
                    "reads": ["/f"],
                    "session": "s",
                }
            ],
        ),
        _case(
            "need witnesses, silent client",
            [
                {
                    "posted": {
                        "location": tok,
                        "machine": MACHINE,
                        "answers": {"remint_ok": True},
                    },
                    "here": None,
                    "need": {"heads": ["h"]},
                    "reads": ["/f"],
                }
            ],
        ),
        _case(
            "need witnesses, an exchange key",
            [
                {
                    "posted": {"location": tok, "machine": MACHINE, "answers": {}},
                    "here": None,
                    "need": {"exchange": "old", "a": 1},
                    "reads": ["/f"],
                }
            ],
        ),
        _case(
            "two rounds take up the reads",
            [
                {
                    "posted": {"location": tok, "machine": MACHINE},
                    "here": None,
                    "need": {"heads": ["h"]},
                    "reads": ["/f"],
                    "session": "s",
                },
                {
                    "posted": {
                        "location": tok,
                        "machine": MACHINE,
                        "answers": {"exchange": "ab" * 16, "remint_ok": True},
                    },
                    "here": None,
                    "session": "s",
                },
            ],
        ),
        _case(
            "two rounds of another session",
            [
                {
                    "posted": {"location": tok, "machine": MACHINE},
                    "here": None,
                    "need": {"heads": ["h"]},
                    "reads": ["/f"],
                    "session": "s",
                },
                {
                    "posted": {
                        "location": tok,
                        "machine": MACHINE,
                        "answers": {"exchange": "ab" * 16},
                    },
                    "here": None,
                    "session": "t",
                },
            ],
        ),
    ]
    return out


def generate() -> dict[str, bytes]:
    lines = _cases()
    header = {"fixture": "location_hook", "cases": len(lines)}
    return {"location_hook.jsonl": jsonl([header, *lines])}
