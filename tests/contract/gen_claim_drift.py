"""Fixtures: the drift index of `claims.py` (unit 3): `_Headers`,
`build_binding_index` and `claim_level_drift` over synthesized
``git log -p -U0`` streams.

Three files, one JSON object per line, keys sorted, ASCII:

- ``claim_drift.jsonl``, not gated. A ``stream`` row holds a stream, the
  answers of `string_fragment` and `_rhs_repr` for every content line the
  index reads (``frag``, ``rhs``, in call order), the index rendered
  (``index``) and claims scored against it, each with the answers of
  `anchors_from_value` and of `ast.literal_eval` (when a `str`) for its
  value. The Rust test replays those answers as its literal reader, so the
  rows do not follow the Python release. A ``sweep`` row pins seeded
  random streams over a fixed vocabulary (``vocab`` rows) by count and
  SHA-256. Every text is made of code points below U+0100, the
  surrogates and the Arabic-Indic digits, so no Unicode table changes it.
- ``claim_drift_unicode.jsonl``, gated on the Unicode version: every code
  point as a changed line's edge (`str.strip`) and as a hunk count
  (`\\d`), in blocks of 0x10000, by SHA-256.
- ``claim_drift_python.jsonl``, gated on the Python release: hunk counts
  at `int`'s 4,300-digit limit, where the index raises ValueError.

The render: the reference's keys in order, each set a sorted list, each
dict a list of ``[key, value]`` pairs sorted by key (a binding's key is
path, kind and name), dumped with ``ensure_ascii`` and compact separators.
A text is a JSON string where it holds no surrogate and no backslash,
else a list of its code points, or ``{"r": [[text, count], ...]}``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from bettermemory import claims as c

from ._common import jsonl
from .gen_claims import _built, _t

M = c.COMMIT_MARK
A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40
E = "e" * 64


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _set(s: set[str]) -> list[str]:
    return sorted(s)


def _pairs(d: dict[str, set[str]]) -> list[Any]:
    return [[k, _set(v)] for k, v in sorted(d.items())]


def render_index(ix: dict[str, Any]) -> dict[str, Any]:
    bindings = [
        [
            k[0],
            k[1],
            k[2],
            {
                "commits": _set(e["commits"]),
                "adds": e["adds"],
                "removes": e["removes"],
                "edit_lines": e["edit_lines"],
                "rhs_added": _set(e["rhs_added"]),
                "rhs_removed": _set(e["rhs_removed"]),
            },
        ]
        for k, e in sorted(ix["bindings"].items())
    ]
    return {
        "bindings": bindings,
        "changed_text": [[p, _pairs(m)] for p, m in sorted(ix["changed_text"].items())],
        "changed_fragments": [
            [p, _pairs(m)] for p, m in sorted(ix["changed_fragments"].items())
        ],
        "deleted": _set(ix["deleted"]),
        "files": _set(ix["files"]),
        "header_only": _pairs(ix["header_only"]),
        "commits": ix["commits"],
        "unindexed_commits": _set(ix["unindexed_commits"]),
        "hunks": ix["hunks"],
        "parse_mismatches": ix["parse_mismatches"],
    }


def render_drift(d: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    out["binding_shas"] = _set(d["binding_shas"])
    out["anchor_shas"] = _set(d["anchor_shas"])
    return out


def _claimed(value: str) -> str | None:
    try:
        v = ast.literal_eval(value)
    except Exception:
        return None
    return v if isinstance(v, str) else None


def _opt(text: str | None) -> Any:
    return None if text is None else _t(text)


def _recorded(stream: str) -> tuple[dict[str, Any], list[Any], list[Any]]:
    """The index of `stream` and the literal answers it asked for."""
    frag: dict[str, str | None] = {}
    rhs: dict[str, str | None] = {}
    real_frag, real_rhs = c.string_fragment, c._rhs_repr

    def f(content: str) -> str | None:
        frag[content] = real_frag(content)
        return frag[content]

    def r(content: str) -> str | None:
        rhs[content] = real_rhs(content)
        return rhs[content]

    with patch.object(c, "string_fragment", f), patch.object(c, "_rhs_repr", r):
        ix = c.build_binding_index(stream)
    return (
        ix,
        [[_t(k), _opt(v)] for k, v in frag.items()],
        [[_t(k), _opt(v)] for k, v in rhs.items()],
    )


def _claim_row(ix: dict[str, Any], spec: tuple[str, str, str, str]) -> dict[str, Any]:
    kind, path, name, value = spec
    cite = c.Claim(kind, path, name, value)
    return {
        "claim": [kind, _t(path), _t(name), _t(value)],
        "anchors": [_t(a) for a in c.anchors_from_value(value)],
        "claimed": _opt(_claimed(value)),
        "o": _dump(render_drift(c.claim_level_drift(cite, ix))),
    }


def _stream_row(
    name: str, stream: str, claims: list[tuple[str, str, str, str]]
) -> dict[str, Any]:
    ix, frag, rhs = _recorded(stream)
    return {
        "f": "stream",
        "name": name,
        "stream": _t(stream),
        "frag": frag,
        "rhs": rhs,
        "index": _dump(render_index(ix)),
        "claims": [_claim_row(ix, s) for s in claims],
    }


def _lines(*lines: str) -> str:
    return "\n".join(lines)


_DOC = "'a long docstring line here'"

_STREAMS: list[tuple[str, str, list[tuple[str, str, str, str]]]] = [
    (
        "bindings",
        _lines(
            M + A,
            "diff --git a/m.py b/m.py",
            "index 1111111..2222222 100644",
            "--- a/m.py",
            "+++ b/m.py",
            "@@ -1 +1 @@",
            "-TIMEOUT = 30",
            "+TIMEOUT = 45",
            "@@ -10,0 +11,3 @@",
            "+def handler(event):",
            "+    " + _DOC + ",",
            "+    ('closing fragment text')",
            "@@ -20,2 +21,0 @@",
            "-class Store:",
            "-async def fetch_remote(x):",
            "@@ -30 +28,4 @@",
            "-RATE: float = 1.5",
            "+RATE: float = 2",
            "+RATE += 1",
            "+x == 1",
            "+NAME = 'v'  # comment",
        ),
        [
            ("symbol", "m.py", "handler", ""),
            ("symbol", "m.py", "Store", ""),
            ("symbol", "m.py", "fetch_remote", ""),
            ("symbol", "m.py", "absent_name", ""),
            ("literal", "m.py", "TIMEOUT", "30"),
            ("literal", "m.py", "TIMEOUT", "45"),
            ("literal", "m.py", "RATE", "1.5"),
            ("literal", "m.py", "DOC", "'x a long docstring line here y'"),
            ("literal", "m.py", "DOC", "'a long docstring line'"),
            ("literal", "m.py", "DOC", "'... closing fragment text ...'"),
            ("literal", "m.py", "DOC", "b'a long docstring line here'"),
            ("literal", "m.py", "DOC", "not a literal"),
            ("literal", "other.py", "TIMEOUT", "30"),
            ("path", "m.py", "", ""),
        ],
    ),
    (
        "headers",
        _lines(
            M + B,
            "diff --git a/old.py b/new.py",
            "similarity index 100%",
            "rename from old.py",
            "rename to new.py",
            "diff --git a/gone.txt b/gone.txt",
            "deleted file mode 100644",
            "index e69de29..0000000",
            "diff --git a/x.sh b/x.sh",
            "old mode 100644",
            "new mode 100755",
            "diff --git a/a.py b/c.py",
            "copy from a.py",
            "copy to c.py",
            "diff --git a/empty b/empty",
            "new file mode 100644",
            "index 0000000..e69de29",
            M + C,
            "diff --git a/d.py b/d.py",
            "deleted file mode 100644",
            "--- a/d.py",
            "+++ /dev/null",
            "@@ -1,2 +0,0 @@",
            "-X = 1",
            "-def f(): pass",
            "diff --git a/dir/a b/dir/a",
            "deleted file mode 100644",
            "diff --git a/dir/b b/dir/b",
            "new file mode 100644",
            "rename from dir/c",
        ),
        [
            ("path", "gone.txt", "", ""),
            ("path", "d.py", "", ""),
            ("path", "old.py", "", ""),
            ("symbol", "d.py", "f", ""),
            ("literal", "d.py", "X", "1"),
            ("absent", "new.py", "", ""),
            ("absent", "gone.txt", "", ""),
            ("absent", "dir", "", ""),
            ("absent", "dir//", "", ""),
            ("absent", "di", "", ""),
            ("absent", "nothing", "", ""),
        ],
    ),
    (
        "quoted",
        _lines(
            M + D,
            'diff --git "a/sp ace.py" "b/sp ace.py"',
            "new file mode 100644",
            "--- /dev/null",
            '+++ "b/sp ace.py"',
            "@@ -0,0 +1 @@",
            "+NAME = 'value'",
            "diff --git a/x b/y b/x b/y",
            "deleted file mode 100644",
            "diff --git a/p b/q",
            "deleted file mode 100644",
            "rename from p",
            "diff --git a/t b/t ",
            "new mode 100644",
        ),
        [
            ("literal", '"b/sp ace.py"', "NAME", "'value'"),
            ("path", "x b/y", "", ""),
            ("absent", "x b", "", ""),
        ],
    ),
    (
        "content",
        _lines(
            M + E,
            "diff --git a/t.txt b/t.txt",
            "--- a/t.txt",
            "+++ b/t.txt",
            "@@ -1,2 +1,3 @@",
            "-diff --git a/fake b/fake",
            "-@@ -1 +1 @@",
            "+diff --git a/fake b/fake",
            "\\ No newline at end of file",
            "++++ b/inner",
            "+@@ -9 +9 @@",
            "@@ -5 +5 @@",
            "-" + M + A,
            "+deleted file mode 100644",
            "rename from z",
        ),
        [("path", "inner", "", ""), ("absent", "t.txt", "", "")],
    ),
    (
        "mismatches",
        _lines(
            "@@ -1 +1 @@",
            "+++ b/early.py",
            "@@ -1 +1 @@",
            "-A_VALUE = 1",
            M + "  " + A + " \t",
            M + A.upper(),
            M + A[:39],
            M + "",
            "diff --git a/s.py b/s.py",
            "--- a/s.py",
            "+++ b/s.py",
            "@@ -1,3 +1 @@",
            "-ONE = 1",
            " context line",
            "+TWO = 2",
            "@@ garbage",
            "@@ -1,١ +1,0 @@",
            "-LONG_LINE_HERE",
            "@@ -1,٣ +1,0 @@",
            "-ONLY_ONE_LINE_",
            "@@ -1,0 +1,00 @@",
            "@@ -1,0 +1,18446744073709551617 @@",
            "+end",
        ),
        [("symbol", "s.py", "ONE", ""), ("literal", "early.py", "A_VALUE", "1")],
    ),
    (
        "surrogates",
        _lines(
            M + A,
            "diff --git a/\ud800.py b/\ud800.py",
            "deleted file mode 100644",
            "--- a/s\udc00.py",
            "+++ b/s\udc00.py",
            "@@ -0,0 +1 @@",
            "+Vé = '\ud800 and more text'",
        ),
        [("path", "\ud800.py", "", ""), ("literal", "s\udc00.py", "Vé", "'x'")],
    ),
    ("empty", "", [("path", "", "", "")]),
    ("only mark", M + A, [("absent", "", "", "")]),
    (
        "trailing",
        _lines(M + A, "diff --git a/z b/z", "new file mode 100644", ""),
        [],
    ),
]


# --- the seeded sweep --------------------------------------------------------

_VOCAB = [
    M + A,
    M + B,
    M + "zz",
    "diff --git a/v.py b/v.py",
    "diff --git a/w.py b/w.py",
    "deleted file mode 100644",
    "new file mode 100644",
    "rename from v.py",
    "rename to u.py",
    "--- a/v.py",
    "+++ b/v.py",
    "+++ /dev/null",
    "@@ -1 +1 @@",
    "@@ -1,2 +1,0 @@",
    "@@ -1,0 +1,2 @@",
    "-LIMIT = 10",
    "+LIMIT = 20",
    "+LIMIT = 'ten'",
    "-def run(x):",
    "+def run(x):",
    "+class Box:",
    "+    'a string fragment line',",
    '-    "another fragment text"',
    "+plain words in a line",
    "\\ No newline at end of file",
    " context",
    "",
]
_SWEEP_CLAIMS: list[tuple[str, str, str, str]] = [
    ("symbol", "v.py", "run", ""),
    ("literal", "v.py", "LIMIT", "10"),
    ("literal", "v.py", "DOC", "'.. a string fragment line ..'"),
    ("absent", "u.py", "", ""),
    ("path", "v.py", "", ""),
]
_SEED = 20261121
_SWEEP_COUNT = 2000
_SWEEP_LINES = 24


def _lcg(state: int) -> int:
    return (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)


def sweep_streams() -> list[str]:
    state = _SEED
    out = []
    for _ in range(_SWEEP_COUNT):
        picked = []
        for _ in range(_SWEEP_LINES):
            state = _lcg(state)
            picked.append(_VOCAB[(state >> 33) % len(_VOCAB)])
        out.append("\n".join(picked))
    return out


def _sweep_rows() -> list[dict[str, Any]]:
    contents = {line[1:] for line in _VOCAB if line}
    vocab = {
        "f": "vocab",
        "lines": [_t(x) for x in _VOCAB],
        "frag": [[_t(x), _opt(c.string_fragment(x))] for x in sorted(contents)],
        "rhs": [[_t(x), _opt(c._rhs_repr(x))] for x in sorted(contents)],
        "claims": [
            {
                "claim": list(s),
                "anchors": [_t(a) for a in c.anchors_from_value(s[3])],
                "claimed": _opt(_claimed(s[3])),
            }
            for s in _SWEEP_CLAIMS
        ],
    }
    h = hashlib.sha256()
    for stream in sweep_streams():
        ix = c.build_binding_index(stream)
        h.update(_dump(render_index(ix)).encode("ascii") + b"\n")
        for s in _SWEEP_CLAIMS:
            d = c.claim_level_drift(c.Claim(*s), ix)
            h.update(_dump(render_drift(d)).encode("ascii") + b"\n")
    sweep = {
        "f": "sweep",
        "seed": _SEED,
        "count": _SWEEP_COUNT,
        "lines": _SWEEP_LINES,
        "h": h.hexdigest(),
    }
    return [vocab, sweep]


def _generate() -> dict[str, bytes]:
    rows = [_stream_row(n, s, cl) for n, s, cl in _STREAMS]
    rows.extend(_sweep_rows())
    return {"claim_drift.jsonl": jsonl(rows)}


_INT_DIGITS = 4300


def _default_digits(make: Callable[[], dict[str, bytes]]) -> dict[str, bytes]:
    limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(_INT_DIGITS)
    try:
        return make()
    finally:
        sys.set_int_max_str_digits(limit)


def generate() -> dict[str, bytes]:
    return _default_digits(_generate)


# --- every code point (gated on Unicode) -------------------------------------


def unicode_stream(ch: str) -> str:
    return _lines(
        M + A,
        "diff --git a/f b/f",
        "+++ b/f",
        "@@ -0,0 +1 @@",
        "+" + ch + "#abcdefghijkl" + ch,
        "@@ -1," + ch + " +1 @@",
        "-x",
        "+y",
    )


def _unicode_answer(ch: str) -> str:
    ix = c.build_binding_index(unicode_stream(ch))
    keys = ",".join(
        "-".join(f"{ord(x):x}" for x in k)
        for k in sorted(ix["changed_text"].get("f", {}))
    )
    return f"{keys}|{ix['hunks']},{ix['parse_mismatches']}"


def generate_unicode() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for lo in range(0, 0x110000, 0x10000):
        h = hashlib.sha256()
        for cp in range(lo, lo + 0x10000):
            h.update(f"{cp:x}:{_unicode_answer(chr(cp))}\n".encode("ascii"))
        rows.append(
            {"f": "strip_and_count", "lo": lo, "hi": lo + 0x10000, "h": h.hexdigest()}
        )
    return {"claim_drift_unicode.jsonl": jsonl(rows)}


# --- int's digit limit (not gated: the limit is 4,300 on 3.11 to 3.14) -------


def _python_rows() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for n in (4300, 4301):
        for prefix, suffix in (("@@ -1,", " +1 @@"), ("@@ -1 +1,", " @@")):
            for path in ("+++ b/p", "--- x"):
                recipe = {
                    "r": [
                        [M + A + "\n" + path + "\n" + prefix, 1],
                        ["0", n],
                        [suffix + "\n-x", 1],
                    ]
                }
                try:
                    out: Any = _dump(
                        render_index(c.build_binding_index(_built(recipe)))
                    )
                except ValueError:
                    out = {"raised": "ValueError"}
                rows.append({"f": "limit", "stream": recipe, "o": out})
    return {"claim_drift_python.jsonl": jsonl(rows)}


def generate_python() -> dict[str, bytes]:
    return _default_digits(_python_rows)
