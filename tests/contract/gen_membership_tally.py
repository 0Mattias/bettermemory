"""Membership tally, strength and tie lines from the live Python model on a recording store."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import membership as mm
from bettermemory import membership_resolve as resolve
from bettermemory.membership_records import Specific

from ._common import jsonl
from .gen_membership_core import StubError, _raised, _spelled
from .gen_membership_rows import enc
from .gen_membership_shared_heal import _T, _LogStub
from .gen_membership_views import _History, _k, _loc, _p, _v, _w

_CODES = ("H", "Hf", "Hb", "R", "D", "F", "T", "B", "C", "custom")


class _Objects(_History):
    def holds_objects(self, ids: Any) -> set[str]:
        self.calls.append(["objects", list(ids)])
        return set(self.spec.get("objects", [])) & set(ids)


def _answer(t: Any) -> list[Any]:
    return [
        [[k, sorted(c)] for k, c in t.cands.items()],
        [list(v) for v in t.contradictions],
        list(map(list, t.rekey.items())),
        t.superseded,
        t.dropped,
        [[k, sorted(v)] for k, v in t.held.items()],
    ]


def _cases() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    cases: list[tuple[dict[str, Any], dict[str, Any]]] = []
    projects = [_p(p) for p in ("P", "Q", "F", "X", "Y")]
    tables: dict[str, Any] = {"projects": projects}
    born = {"head": "h", "kind": "linked"}
    cases.append(({}, {}))
    cases.append(({}, {"shallow": True}))
    rems = [{"project": p, "remote_key": "up"} for p in ("Q", "P")]
    declared = [{"project": p, "remote_key": "up"} for p in ("P", "X")]
    rows = tables | {"project_remotes": rems, "declared_remotes": declared}
    for shallow in (False, True):
        cases.append((rows, {"loc": born, "keys": ["up"], "shallow": shallow}))
    cases.append((rows, {"keys": ["up"], "exclude": ["Q", "P"]}))
    witnesses = [
        _w("X", "d" * 40, 1),
        _w("P", "A" * 40, 2, "finger"),
        _w("Y", "e" * 40, 3),
    ]
    hist = {"holds": ["a" * 40, "d" * 40, "e" * 40]}
    for seed_order in (
        ["A", "C", "B", "D"],
        ["D", "B", "A", "C"],
        ["C", "D", "B", "A"],
        ["A", "B", "D", "C"],
    ):
        ids = ["A", "B", "C", "D"]
        rows = tables | {
            "projects": [_p(p, tenant="outside") for p in ["P", "X", "Y", *ids]],
            "project_witnesses": witnesses,
            "checkouts": [_k("K" + p, p) for p in ids],
            "checkout_views": [_v("K" + p, "/" + p, i) for i, p in enumerate(ids)],
        }
        cases.append(
            (
                rows,
                {
                    "loc": born,
                    "keys": ["local:/" + p for p in seed_order],
                    "expect_dropped": ["P", *seed_order],
                    "expect_holds": ["a" * 40],
                    "history": hist,
                    "exclude": ["X", "Y"],
                },
            )
        )
    cases.append(
        (
            {
                "projects": [_p("Zeta", tenant="away"), _p("Alpha", tenant="away")],
                "project_witnesses": [
                    _w("Alpha", "a" * 40, 1, "fp-a"),
                    _w("Zeta", "b" * 40, 2, "fp-z"),
                ],
            },
            {
                "loc": {"kind": "linked", "head": "h0"},
                "history": {
                    "fp": {"h0": "fp-z", "h1": "fp-a", "h2": None},
                    "parents": {"h0": ["h1"], "h1": ["h2"]},
                },
                "expect_dropped": ["Zeta", "Alpha"],
            },
        )
    )
    rows = tables | {"project_witnesses": witnesses, "project_remotes": rems}
    cases.append(
        (
            rows,
            {
                "loc": born,
                "history": {"fp": {"h": "finger"}},
                "keys": ["up"],
                "exclude": ["X", "Y"],
            },
        )
    )
    frow = {"file_id": "id", "project": "F"}
    cases.append(
        (
            rows | {"project_files": [frow]},
            {
                "loc": born | {"files": [["id", "/w/r"]]},
                "history": hist,
                "exclude": ["X", "Y"],
            },
        )
    )
    for per, got in [
        ([["a"]], ["a"]),
        ([["a", "b", "c", "d"]], ["a", "b"]),
        ([["a", "b", "c", "d", "e"]], ["a", "b"]),
        ([["a", "a"], ["b", "c"]], ["a", "b"]),
        ([["e"], ["c", "b"]], ["c", "b"]),
    ]:
        blobs = [
            {"project": "P", "witness": str(i), "blob": b}
            for i, w in enumerate(per)
            for b in w
        ]
        cases.append(
            (
                tables | {"project_remotes": [rems[1]], "project_blobs": blobs},
                {"loc": born, "keys": ["up"], "history": {"objects": got}},
            )
        )
    for machine, has_commit, with_view in [
        ("M1", False, True),
        ("M1", False, False),
        ("M2", False, True),
        ("M1", True, True),
    ]:
        kt = tables | {
            "checkouts": [_k("K", "Q", machine=machine, token=_T)],
            "checkout_views": [_v("K", 'a"\udcff', 1, machine)] if with_view else [],
        }
        if has_commit:
            kt["project_witnesses"] = [_w("Q", "b" * 40, 1)]
        cases.append((kt, {"loc": born | {"token": _T}, "history": {}}))
    cases.append(
        (
            {
                "projects": [_p("P")],
                "checkouts": [_k("K1", "P", token=_T), _k("K2", "P", token=_T)],
                "checkout_views": [_v("K1", "/first", 1), _v("K2", "/second", 2)],
            },
            {"loc": {"kind": "linked", "token": _T}, "trace_liveness": True},
        )
    )
    for key, live, token in [
        (["1", "100", None], False, None),
        (["1", "100", "t"], False, None),
        (None, False, None),
        (["2", "1", "t"], False, None),
        (["2", "1", "t"], True, None),
        (["2", "1", "t"], False, _T),
    ]:
        rows = tables | {
            "project_witnesses": [_w("P", "a" * 40, 1)],
            "checkouts": [
                _k(
                    "K",
                    "P",
                    object_device="1",
                    object_inode="100",
                    object_birth=None,
                    token=None,
                )
            ],
            "checkout_views": [_v("K", "/w/r", 1)],
        }
        cases.append(
            (
                rows,
                {
                    "loc": {"head": "h", "object_key": key, "token": token},
                    "history": hist,
                    "live": live,
                },
            )
        )
    for bad in [
        {"project_remotes": [{"remote_key": "up"}]},
        {"checkout_views": [{"path": "/P", "checkout": "bad"}]},
        {
            "project_blobs": [{"project": "P", "blob": "z"}],
            "project_remotes": [rems[1]],
        },
    ]:
        cases.append(
            (tables | bad, {"keys": ["up", "local:/P"], "loc": born, "history": {}})
        )
    evidence = tables | {"project_witnesses": [_w("P", "a" * 40, 1, "fp")]}
    for fault in ("holds", "fingerprint", "parents"):
        cases.append(
            (
                evidence,
                {"loc": born, "history": {"fp": {"h": None}}, "fail_read": fault},
            )
        )
    cases.append(
        (
            tables
            | {
                "project_remotes": [rems[1]],
                "project_blobs": [{"project": "P", "blob": "x", "witness": "w"}],
            },
            {"loc": born, "keys": ["up"], "history": {}, "fail_read": "holds_objects"},
        )
    )
    cases.append(
        (
            tables | {"checkouts": [_k("K", "Q", token=_T)]},
            {"loc": born | {"token": _T}, "fail_read": "view_state"},
        )
    )
    base = evidence | {"checkout_views": [_v("K", "/w/r", 1)]}
    cases.append(
        (
            base | {"checkouts": [_k("K", "P", token=None)]},
            {
                "loc": {"head": "h"},
                "history": {"holds": ["a" * 40]},
                "fail_read": "live_view",
            },
        )
    )
    for birth in ("real", None):
        kt = base | {
            "checkouts": [
                _k(
                    "K",
                    "P",
                    object_device="1",
                    object_inode="1",
                    object_birth=birth,
                    token=None,
                )
            ]
        }
        cases.append(
            (
                kt,
                {
                    "loc": {"head": "h", "object_key": ["1", "1", birth]},
                    "history": {"holds": ["a" * 40]},
                    "live": False,
                    "expect_rekey": [] if birth else [["P", "K"]],
                },
            )
        )
    kt = base | {"checkouts": [_k("K", "P", token=None, retired=1)]}
    cases.append(
        (
            kt,
            {
                "loc": {"head": "h"},
                "history": {"holds": ["a" * 40]},
                "live": False,
                "expect_rekey": [],
                "log": [["checkout_retired", '{"into":"replacement"}']],
            },
        )
    )
    for foreign in (True, False):
        found = "0" * 26
        ks = [_k("K", "P", token=_T)]
        if foreign:
            ks.append(_k("Other", "P", machine="M2", token=found))
        kt = base | {"checkouts": ks}
        cases.append(
            (
                kt,
                {
                    "loc": {"head": "h", "token": found},
                    "history": {"holds": ["a" * 40]},
                    "live": False,
                    "expect_rekey": [["P", "K"]] if foreign else [],
                },
            )
        )
    cases.append(
        (tables | {"project_remotes": rems}, {"keys": ["up"], "fail": ["rows", 2]})
    )
    return cases


def _seeded() -> list[Any]:
    """A real frozenset, its held commits and its per-key project sets in this process."""
    shas = ["a" * 40, "b" * 40, "d" * 40, "e" * 40]
    triples = [
        ("Zulu", shas[0], "shared"),
        ("Kappa", shas[0], "shared"),
        ("Alpha", shas[1], "single"),
        ("LeftOut", shas[2], "excluded"),
        ("AlsoOut", shas[3], "excluded"),
    ]
    projects = [k for k, _, _ in triples]
    remotes = frozenset(["local:/Right", "local:/Left", "url"])
    tables = {
        "projects": [_p(p, tenant="away") for p in [*projects, "Right", "Left"]],
        "project_witnesses": [_w(p, c, i, fp) for i, (p, c, fp) in enumerate(triples)],
        "checkouts": [_k("K" + p, p) for p in ("Right", "Left")],
        "checkout_views": [
            _v("K" + p, "/" + p, i) for i, p in enumerate(("Right", "Left"))
        ],
    }
    excluded = ["LeftOut", "AlsoOut"]
    by_id: dict[str, set[str]] = defaultdict(set)
    by_fp: dict[str, set[str]] = defaultdict(set)
    for p, c, fp in triples:
        if p not in excluded:
            by_id[c].add(p)
            by_fp[fp].add(p)
    out = []
    for holds in ([], shas):
        spec = {
            "holds": holds,
            "fp": {"h": "shared", "parent": "single"},
            "parents": {"h": ["parent"]},
        }
        stub = _LogStub(tables, [])
        history = _Objects(spec, stub.log)
        loc_spec = {"head": "h", "kind": "linked"}
        loc = dataclasses.replace(_loc(loc_spec, history), remote_keys=remotes)
        # Observe the same set construction as Membership._holds without adding
        # this diagnostic read to the model/reader trace that the fixture tests.
        order = list(
            mm.Membership._holds(
                dataclasses.replace(loc, history=_Objects(spec, [])), sorted(by_id)
            )
        )
        model = mm.Membership(stub, "M1")  # type: ignore[arg-type]
        t = resolve._tally(model, loc, None, excluded)
        assert set(t.dropped) == {"Zulu", "Kappa", "Alpha", "Right", "Left"}
        assert [x[1] for x in stub.log if x[0] == "holds"] == [sorted(shas[:2])]
        out.append(
            dict(
                f="tally",
                keys=list(remotes),
                loc=loc_spec,
                exclude=excluded,
                history=spec,
                history_order=dict(
                    commits=order,
                    projects_by_commit={k: list(v) for k, v in by_id.items()},
                    projects_by_fingerprint={k: list(v) for k, v in by_fp.items()},
                ),
                o=_answer(t),
                calls=enc(stub.log),
            )
        )
    return [_spelled(tables), out]


def generate() -> dict[str, bytes]:
    rows: list[Any] = []
    table_sets: list[Any] = []
    for tables, case in _cases():
        spelled = _spelled(tables)
        if spelled not in table_sets:
            table_sets.append(spelled)
        stub = _LogStub(tables, case.get("log", []))
        stub.fail = case.get("fail")
        live = case.get("live", True)

        def liveness(k: Any, v: Any) -> str:
            if case.get("trace_liveness"):
                stub.log.append(["liveness", k.id, v.path])
            return "live" if live else "gone"

        model = mm.Membership(stub, "M1", liveness=liveness)  # type: ignore[arg-type]
        history = _Objects(case["history"], stub.log) if "history" in case else None
        keys = case.get("keys", [])
        # Materialize a caller-observed iteration order. The production frozenset
        # varies with PYTHONHASHSEED. Sorted bulk queries still sort these keys.
        loc = dataclasses.replace(
            _loc(case.get("loc", {}), history),
            remote_keys=tuple(keys),
            shallow=case.get("shallow", False),
        )
        result: Any
        fault = case.get("fail_read")

        def tally_once() -> Any:
            return _answer(
                resolve._tally(model, loc, case.get("tenant"), case.get("exclude", []))
            )

        try:
            if fault:
                owner: Any = model if fault in ("view_state", "live_view") else history
                original = getattr(owner, fault)

                def fail(*args: Any) -> Any:
                    if fault in ("view_state", "live_view"):
                        stub.log.append([fault, args[0].id])
                    else:
                        original(*args)
                    raise StubError("failed " + str(fault) + "\udcff")

                with patch.object(owner, fault, fail):
                    result = tally_once()
            else:
                result = tally_once()
        except Exception as error:  # noqa: BLE001
            result = _raised(error)
        if fault:
            assert result == {"raised": ["StubError", "failed " + fault + "\udcff"]}
        if case.get("trace_liveness"):
            assert [call for call in stub.log if call[0] == "liveness"] == [
                ["liveness", "K1", "/first"],
                ["liveness", "K2", "/second"],
            ]
            assert result[1] == [
                [
                    "token_shared_no_commit",
                    "P",
                    'carries the checkout token of "/first", a live checkout with no commit in common',
                ]
            ]
        if "expect_rekey" in case:
            assert result[2] == case["expect_rekey"], (case, result)
        if "expect_dropped" in case:
            assert result[4] == case["expect_dropped"]
        if "expect_holds" in case:
            assert [call[1] for call in stub.log if call[0] == "holds"] == [
                case["expect_holds"]
            ]
        rows.append(
            dict(
                case,
                f="tally",
                tables=table_sets.index(spelled),
                o=result,
                calls=enc(stub.log),
            )
        )
    for seed in ("1", "2"):
        child = "from tests.contract.gen_membership_tally import _seeded; import json; print(json.dumps(_seeded()))"
        output = subprocess.check_output(
            [sys.executable, "-c", child],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
        )
        spelled, seeded = json.loads(output)
        if spelled not in table_sets:
            table_sets.append(spelled)
        rows.extend(
            dict(row, tables=table_sets.index(spelled), seed=seed) for row in seeded
        )
    for born in (False, True):
        for shallow in (False, True):
            for has in (False, True):
                stub = _LogStub(
                    {
                        "projects": [_p("P")],
                        "project_witnesses": [_w("P", "a" * 40, 1)] if has else [],
                    },
                    [],
                )
                spelled = _spelled(stub.tables)
                if spelled not in table_sets:
                    table_sets.append(spelled)
                model = mm.Membership(stub, "M1")  # type: ignore[arg-type]
                loc = dataclasses.replace(
                    _loc({"head": "h" if born else None}, None), shallow=shallow
                )
                digits = []
                for mask in range(1 << len(_CODES)):
                    codes = [c for i, c in enumerate(_CODES) if mask & (1 << i)]
                    digits.append(
                        str(resolve._strength(model, loc, "P", codes + codes[:1]))
                    )
                token_only = 1 << _CODES.index("T")
                assert digits[token_only] == str(1 if born or has else 2)
                rows.append(
                    dict(
                        f="strength",
                        tables=table_sets.index(spelled),
                        born=born,
                        shallow=shallow,
                        has=has,
                        o="".join(digits),
                        calls=enc(stub.log),
                    )
                )
    for n in range(6):
        names = ["N\udcff", "Alpha", "Beta", "Delta", "E"][:n]
        for bounded in (False, True):
            spec = Specific(best=[], top=frozenset(), held=4, bounded=bounded)
            rows.append(
                dict(
                    f="unordered",
                    names=names,
                    bounded=bounded,
                    o=resolve._unordered(spec, names),
                )
            )
    # The native decoder also accepts independently generated plain answers.
    # Only the generated comparison values use membership's portable encoding.
    for row in rows:
        if row["f"] != "strength":
            row["encoded_o"] = enc(row.pop("o"))
        if "names" in row:
            row["names"] = enc(row["names"])
    return {
        "membership_tally.jsonl": jsonl(
            [{"rows": len(rows), "tables": table_sets, "codes": _CODES}, *rows]
        )
    }
