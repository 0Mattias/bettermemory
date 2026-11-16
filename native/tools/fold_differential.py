"""A differential of the fold check: the reference against native, on
random stores (a tool, not part of the test suite).

Run from the repository root with the project's Python, after a release
build of the example (``cargo build --release --example bm-verify`` in
``native``)::

    .venv/bin/python native/tools/fold_differential.py --stores 600 --seed 1

Each store is built in a temporary directory, through the reference, from
the intact base of ``store_bases.json`` and one of three preludes of
``tests/contract/gen_fold.py`` (populated; populated plus every kind;
populated plus every kind plus drift), and then either

- edited behind the log: one to three random edits of any of the 22
  compared tables (a column set to 1, 1.0, '1', X'31', -0.0, 2^53,
  2^53 + 1, 9007199254740992.0, i64 max, 1e308, NFC and NFD 'é', a text
  with a NUL, its own value upper-cased, cast to BLOB or INTEGER; a row
  duplicated, planted under another key, or deleted), or
- given a signed row with a fuzzed payload: a valid payload of a random
  kind truncated, given NaN, Infinity or 1e999, a bad escape, a lone
  surrogate, a NUL, a duplicate key, a field of the wrong type or missing,
  or the payload of another kind.

An edit that would put a REAL into a column of TEXT affinity is skipped:
SQLite stores it as text spelled by its own version's rules, and the
reference's SQLite and native's are not the same version, so the replay
on each side would spell it differently (a difference of SQLite, not of
the port).

On each store the reference's ``log_verify()`` (``path`` left out) and the
release ``bm-verify --fold`` run on the same file; every field is
compared (a raise by its class). It prints the counts and each
difference, and exits 1 on any difference.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bettermemory.store_schema import _MEMBERSHIP_SPECS  # noqa: E402
from tests.contract import gen_fold as gf  # noqa: E402
from tests.contract import gen_store_files as sf  # noqa: E402
from tests.contract._sandbox import world  # noqa: E402

TABLES = [*gf.FOLDED_TABLES, *gf.MEMBERSHIP_TABLES]

VALUES: list[tuple[str, bool]] = [
    # (SQL, whether it is a REAL)
    ("1", False),
    ("1.0", True),
    ("'1'", False),
    ("X'31'", False),
    ("-0.0", True),
    ("9007199254740992", False),
    ("9007199254740993", False),
    ("9007199254740992.0", True),
    ("9223372036854775807", False),
    ("1e308", True),
    ("'" + unicodedata.normalize("NFC", "é") + "'", False),
    ("'" + unicodedata.normalize("NFD", "é") + "'", False),
    ("'a' || char(0) || 'b'", False),
]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _shape(store: Path) -> dict[str, list[tuple[str, bool, str]]]:
    """Each compared table's columns: name, TEXT affinity, declared type."""
    conn = sqlite3.connect(store.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        out: dict[str, list[tuple[str, bool, str]]] = {}
        for table in TABLES:
            cols = []
            for col in conn.execute(f"PRAGMA table_info({_q(table)})"):
                declared = str(col[2]).upper()
                text = any(t in declared for t in ("CHAR", "CLOB", "TEXT"))
                cols.append((str(col[1]), text, declared))
            out[table] = cols
        return out
    finally:
        conn.close()


def _edit(
    rng: random.Random, shape: dict[str, list[tuple[str, bool, str]]]
) -> str | None:
    """One random edit's SQL, or None when it would put a REAL into a
    TEXT-affinity column."""
    table = rng.choice(TABLES)
    cols = shape[table]
    if not cols:
        return None
    t = _q(table)
    pick = f"rowid = (SELECT rowid FROM {t} ORDER BY random() LIMIT 1)"
    what = rng.choice(
        ["set", "set", "set", "upper", "blob", "integer", "dup", "plant", "delete"]
    )
    name, text, _ = rng.choice(cols)
    c = _q(name)
    if what == "set":
        sql, real = rng.choice(VALUES)
        if real and text:
            return None
        return f"UPDATE {t} SET {c} = {sql} WHERE {pick};"
    if what == "upper":
        # A REAL upper-cased is text: only where the column turns it back.
        if text:
            return f"UPDATE {t} SET {c} = upper({c}) WHERE {pick} AND typeof({c}) != 'real';"
        return f"UPDATE {t} SET {c} = upper({c}) WHERE {pick};"
    if what == "blob":
        return f"UPDATE {t} SET {c} = CAST({c} AS BLOB) WHERE {pick} AND typeof({c}) != 'real';"
    if what == "integer":
        return f"UPDATE {t} SET {c} = CAST({c} AS INTEGER) WHERE {pick};"
    listed = ", ".join(_q(n) for n, _, _ in cols)
    if what == "dup":
        return f"INSERT OR IGNORE INTO {t}({listed}) SELECT {listed} FROM {t} WHERE {pick};"
    if what == "plant":
        key = gf._TABLE_KEYS[table][0]
        picked = ", ".join(
            f"{_q(n)} || '-planted'" if n == key else _q(n) for n, _, _ in cols
        )
        return f"INSERT OR IGNORE INTO {t}({listed}) SELECT {picked} FROM {t} WHERE {pick};"
    return f"DELETE FROM {t} WHERE {pick};"


def _fuzz(rng: random.Random, rows: list[tuple[str, Any]]) -> tuple[str, str]:
    """A signed row's kind and a fuzzed payload text."""
    kind, payload = rng.choice(rows)
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    how = rng.choice(
        [
            "truncate",
            "nan",
            "inf",
            "big",
            "escape",
            "surrogate",
            "nul",
            "dup",
            "type",
            "type",
            "missing",
            "missing",
            "swap",
        ]
    )
    if how == "truncate":
        return kind, text[: rng.randrange(1, max(2, len(text)))]
    if how in ("nan", "inf", "big"):
        token = {"nan": "NaN", "inf": "Infinity", "big": "1e999"}[how]
        if rng.random() < 0.5:
            # In place of the first value.
            head, _, rest = text.partition(":")
            return kind, head + ":" + token + ',"_x":' + rest
        return kind, text[:-1] + ',"_n":' + token + "}"
    if how == "escape":
        return kind, text[:-1] + ',"_e":"a\\qb"}'
    if how == "surrogate":
        return kind, text[:-1] + ',"_s":"\\ud800"}'
    if how == "nul":
        return kind, text.replace('":"', '":"\\u0000', 1)
    if how == "dup" and len(text) > 2:
        return kind, "{" + text[1:-1] + "," + text[1:-1] + "}"
    if how == "swap":
        return rng.choice(rows)[0], text
    # A field of the wrong type, or missing, at the top level or one down.
    value = json.loads(text)
    target = value
    inner = [k for k, v in value.items() if isinstance(v, dict)]
    if inner and rng.random() < 0.6:
        target = value[rng.choice(inner)]
    if target:
        field = rng.choice(sorted(target))
        if how == "missing":
            del target[field]
        else:
            target[field] = rng.choice([[1], {"a": 1}, 1, 1.5, "x", None, True])
    return kind, json.dumps(value, sort_keys=True, separators=(",", ":"))


def _native(binary: Path, case: Path) -> Any:
    done = subprocess.run(
        [str(binary), "--fold", str(case / "memory.sqlite"), str(case / "keys")],
        capture_output=True,
        text=True,
        check=False,
    )
    out = json.loads(done.stdout)
    if "log_verify" in out:
        return out["log_verify"]
    error = str(out.get("error", ""))
    for cls in error.replace(":", " ").split():
        if cls.endswith(("Error", "Exception")):
            return {"raised": cls}
    return {"error": error}


def _norm(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stores", type=int, default=600)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--bm-verify",
        type=Path,
        default=ROOT / "native" / "target" / "release" / "examples" / "bm-verify",
    )
    args = parser.parse_args()
    rng = random.Random(args.seed)
    bases = sf.load_bases()
    every = list(gf.EVERY_KIND)
    for kind, payload in gf.POPULATED:
        member = kind.removesuffix("_put")
        if kind.endswith("_put") and member in _MEMBERSHIP_SPECS:
            every.extend(gf._second(member, payload[member]))
    populated = gf.prelude(bases, [], gf.POPULATED)
    on_every = [*populated, *gf.prelude(bases, populated, every)]
    drift = [*on_every, *gf.prelude(bases, on_every, [], gf.DRIFT)]
    preludes = {"populated": populated, "every_kind": on_every, "drift": drift}
    rows = [(k, p) for k, p in [*gf.POPULATED, *every] if isinstance(p, dict)]
    counts = {"stores": 0, "edited": 0, "fuzzed": 0, "skipped_edits": 0, "same": 0}
    differences: list[str] = []
    with tempfile.TemporaryDirectory() as tmp, world():
        root = Path(tmp).resolve()
        probe = root / "probe"
        sf._materialize(bases, "intact", populated, probe)
        shape = _shape(probe / "memory.sqlite")
        shutil.rmtree(probe)
        for i in range(args.stores):
            name = rng.choice(sorted(preludes)) if i >= 3 else sorted(preludes)[i]
            ops = list(preludes[name])
            what = f"{name}"
            if i >= 3 and rng.random() < 0.42:
                sqls = []
                for _ in range(rng.randint(1, 3)):
                    sql = _edit(rng, shape)
                    if sql is None:
                        counts["skipped_edits"] += 1
                    else:
                        sqls.append(sql)
                if sqls:
                    ops.append({"sql": "\n".join(sqls)})
                    counts["edited"] += 1
                    what += " edited: " + " ".join(sqls)
            elif i >= 3:
                kind, text = _fuzz(rng, rows)
                ops = list(preludes[name])
                ops += gf.prelude(bases, ops, [], [(kind, text)])
                counts["fuzzed"] += 1
                what += f" fuzzed {kind}: {text[:160]!r}"
            case = root / f"s{i}"
            sf._materialize(bases, "intact", ops, case)
            reference = gf.reports(case).get("log_verify")
            native = _native(args.bm_verify, case)
            counts["stores"] += 1
            if _norm(reference) == _norm(native):
                counts["same"] += 1
            else:
                differences.append(
                    f"store {i} ({what}):\n  reference {json.dumps(_norm(reference))[:600]}"
                    f"\n  native    {json.dumps(_norm(native))[:600]}"
                )
            shutil.rmtree(case, ignore_errors=True)
    print(json.dumps(counts | {"different": len(differences)}))
    for d in differences:
        print(d)
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
