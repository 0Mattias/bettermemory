"""Fixtures: the key ring's files (``log_keyfiles.jsonl``,
``log_keyfiles_posix.jsonl``, ``log_keyfiles_darwin.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/log/ring.rs`` (`KeyRing.__post_init__`,
the paths, `load_current`, `retire_current`, `read_origin`,
`write_origin`, `name_origin_key`, `replace_origin`, `read_binding`,
`rebind_store`, `_dangling_link`).

``log_keyfiles.jsonl`` touches no file system: the store id check
(``part`` ``id``: the id as a neutral value and ``ok`` or the error and
its text).

``log_keyfiles_posix.jsonl`` (POSIX only) drives the reference over a
scratch directory, ``<root>``, whose ``keys`` folder is the keys
directory. A row: ``setup`` (``file`` name, hex bytes, mode; ``deep``
name, depth: ``[`` times depth then ``]`` times depth; ``dir`` name,
mode; ``link`` name, target), ``op`` (the method and its arguments) and
``out`` (``ok`` and the value, or ``raised``: the class, the errno's
name, and the message when the text is the module's own), then
``after``: every entry under the root in name order, a file as its text
(``<root>`` for the scratch path) or hex and its mode, a folder as its
mode, a link as its target. A link's own mode is left out (macOS and
Linux give links different modes). The errno names are those macOS and
glibc share.

``log_keyfiles_darwin.jsonl`` (platform gated) holds the message of
each `KeyUnreadableError` whose reason is the C library's ``strerror``,
which glibc spells otherwise for some errnos.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from bettermemory import log as blog

from ._common import jsonl, neutral

STORE_ID = "0123456789abcdef0123456789abcdef"
M1 = "a" * 64
M2 = "b" * 64
FPX = "c" * 64
KEY = bytes(range(32))
FP = blog.fingerprint(KEY)
DEEP = 100_000


def _anchor(seq: Any, mac: Any, named: Any = None) -> str:
    doc: dict[str, Any] = {"seq": seq, "mac": mac}
    if named is not None:
        doc["key_fingerprint"] = named
    return json.dumps(doc)


def _f(name: str, data: bytes | str, mode: int = 0o600) -> list[Any]:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return ["file", name, raw.hex(), mode]


KEYS = ["dir", "keys", 0o700]
KEYFILE = f"keys/{STORE_ID}.key"
ORIGIN = f"keys/{STORE_ID}.origin"
BINDING = f"keys/{STORE_ID}.path"
RETIRED = f"keys/{STORE_ID}.{FP[:16]}.key"

Scenario = tuple[str, list[list[Any]], list[Any]]


def _key_scenarios() -> list[Scenario]:
    lc = ["load_current"]
    rc = ["retire_current"]
    return [
        ("lc_absent", [KEYS], lc),
        ("lc_keys_missing", [], lc),
        ("lc_present", [KEYS, _f(KEYFILE, KEY)], lc),
        ("lc_empty", [KEYS, _f(KEYFILE, b"")], lc),
        ("lc_unreadable", [KEYS, _f(KEYFILE, KEY, 0)], lc),
        ("lc_directory", [KEYS, ["dir", KEYFILE, 0o700]], lc),
        ("lc_dangling", [KEYS, ["link", KEYFILE, "gone.key"]], lc),
        ("lc_dangling_abs", [KEYS, ["link", KEYFILE, "<root>/gone"]], lc),
        ("lc_dir_dangling", [["link", "keys", "nowhere"]], lc),
        ("lc_keys_is_file", [_f("keys", b"x")], lc),
        ("lc_loop", [KEYS, ["link", KEYFILE, f"{STORE_ID}.key"]], lc),
        ("lc_link_ok", [KEYS, _f("keys/real", KEY), ["link", KEYFILE, "real"]], lc),
        (
            "lc_dir_link_ok",
            [
                ["dir", "real", 0o700],
                _f(f"real/{STORE_ID}.key", KEY),
                ["link", "keys", "real"],
            ],
            lc,
        ),
        ("rc_none", [KEYS], rc),
        ("rc_move", [KEYS, _f(KEYFILE, KEY)], rc),
        ("rc_taken", [KEYS, _f(KEYFILE, KEY), _f(RETIRED, b"old")], rc),
        ("rc_unreadable", [KEYS, _f(KEYFILE, KEY, 0)], rc),
        ("rc_target_dir", [KEYS, _f(KEYFILE, KEY), ["dir", RETIRED, 0o700]], rc),
        ("rc_target_dangling", [KEYS, _f(KEYFILE, KEY), ["link", RETIRED, "gone"]], rc),
        (
            "rc_target_loop",
            [KEYS, _f(KEYFILE, KEY), ["link", RETIRED, f"{STORE_ID}.{FP[:16]}.key"]],
            rc,
        ),
        ("rc_keys_readonly", [["dir", "keys", 0o500], _f(KEYFILE, KEY)], rc),
        ("rc_dangling", [KEYS, ["link", KEYFILE, "gone"]], rc),
    ]


def _origin_scenarios() -> list[Scenario]:
    wo = ["write_origin", 7, M2, None]
    won = ["write_origin", 7, M2, FPX]
    valid = _f(ORIGIN, _anchor(3, M1))
    named = _f(ORIGIN, _anchor(3, M1, FPX))
    nk = ["name_origin_key", 3, M1, FPX]
    new = {"seq": 9, "mac": M2, "key_fingerprint": FPX}
    return [
        ("ro_read_absent", [KEYS], ["read_origin"]),
        ("ro_read_valid", [KEYS, named], ["read_origin"]),
        ("ro_read_unreadable", [KEYS, _f(ORIGIN, _anchor(3, M1), 0)], ["read_origin"]),
        ("ro_read_deep", [KEYS, ["deep", ORIGIN, DEEP, 0o600]], ["read_origin"]),
        ("wo_absent", [KEYS], wo),
        ("wo_absent_named", [KEYS], won),
        ("wo_text_mac", [KEYS], ["write_origin", 1, "café", "x"]),
        ("wo_valid", [KEYS, valid], won),
        ("wo_unparseable", [KEYS, _f(ORIGIN, "not json")], wo),
        ("wo_shape", [KEYS, _f(ORIGIN, _anchor(0, M1))], wo),
        ("wo_true_seq", [KEYS, _f(ORIGIN, '{"seq": true, "mac": "' + M1 + '"}')], wo),
        ("wo_non_utf8", [KEYS, _f(ORIGIN, b"\xff")], wo),
        ("wo_deep", [KEYS, ["deep", ORIGIN, DEEP, 0o600]], wo),
        ("wo_unreadable", [KEYS, _f(ORIGIN, "not json", 0)], wo),
        ("wo_unreadable_valid", [KEYS, _f(ORIGIN, _anchor(3, M1), 0)], wo),
        ("wo_directory", [KEYS, ["dir", ORIGIN, 0o700]], wo),
        ("wo_dangling", [KEYS, ["link", ORIGIN, "gone"]], wo),
        ("wo_keys_missing", [], wo),
        ("wo_keys_open", [["dir", "keys", 0o755]], wo),
        ("wo_keys_file", [_f("keys", b"x")], wo),
        ("wo_lock_dir", [KEYS, ["dir", ORIGIN + ".lock", 0o700]], wo),
        ("nk_none", [KEYS], nk),
        ("nk_ok", [KEYS, valid], nk),
        ("nk_named", [KEYS, named], nk),
        ("nk_bad_named", [KEYS, _f(ORIGIN, _anchor(3, M1, "xyz"))], nk),
        ("nk_other_seq", [KEYS, valid], ["name_origin_key", 4, M1, FPX]),
        ("nk_other_mac", [KEYS, valid], ["name_origin_key", 3, M2, FPX]),
        ("nk_deep", [KEYS, ["deep", ORIGIN, DEEP, 0o600]], nk),
        (
            "nk_extra_keys",
            [KEYS, _f(ORIGIN, '{"mac": "' + M1 + '", "seq": 3, "x": 1}')],
            nk,
        ),
        ("rp_none_absent", [KEYS], ["replace_origin", None]),
        ("rp_none_present", [KEYS, named], ["replace_origin", None]),
        ("rp_none_garbage", [KEYS, _f(ORIGIN, "[]")], ["replace_origin", None]),
        ("rp_new_absent", [KEYS], ["replace_origin", new]),
        ("rp_new_unnamed", [KEYS, valid], ["replace_origin", {"seq": 9, "mac": M2}]),
        ("rp_new_over_valid", [KEYS, named], ["replace_origin", new]),
        (
            "rp_new_over_unreadable",
            [KEYS, _f(ORIGIN, _anchor(3, M1), 0)],
            ["replace_origin", new],
        ),
        ("rp_deep", [KEYS, ["deep", ORIGIN, DEEP, 0o600]], ["replace_origin", new]),
        ("rp_keys_missing", [], ["replace_origin", None]),
    ]


def _binding_scenarios() -> list[Scenario]:
    rb = ["read_binding"]

    def b(text: str | bytes, mode: int = 0o600) -> list[Any]:
        return _f(BINDING, text, mode)

    store = ["rebind_store", "<root>/store.db"]
    return [
        ("rb_absent", [KEYS], rb),
        ("rb_ok", [KEYS, b('{"path": "/abs/x.db"}')], rb),
        ("rb_messy", [KEYS, b('{"path": "//a//./b/"}')], rb),
        ("rb_three_slashes", [KEYS, b('{"path": "///a/../b"}')], rb),
        ("rb_relative", [KEYS, b('{"path": "rel/x.db"}')], rb),
        ("rb_empty", [KEYS, b('{"path": ""}')], rb),
        ("rb_nul", [KEYS, b('{"path": "/a\\u0000b"}')], rb),
        ("rb_not_str", [KEYS, b('{"path": 5}')], rb),
        ("rb_list", [KEYS, b('["/abs"]')], rb),
        ("rb_dup", [KEYS, b('{"path": "/a", "path": "/b"}')], rb),
        ("rb_dup_bad_last", [KEYS, b('{"path": "/a", "path": 1}')], rb),
        ("rb_crlf", [KEYS, b('{"path":\r\n"/a"}\r')], rb),
        ("rb_escaped", [KEYS, b('{"path": "/caf\\u00e9"}')], rb),
        ("rb_garbage", [KEYS, b("nope")], rb),
        ("rb_bom", [KEYS, b(b'\xef\xbb\xbf{"path": "/a"}')], rb),
        ("rb_non_utf8", [KEYS, b(b'{"path": "/\xff"}')], rb),
        ("rb_unreadable", [KEYS, b('{"path": "/a"}', 0)], rb),
        ("rb_directory", [KEYS, ["dir", BINDING, 0o700]], rb),
        ("rb_dangling", [KEYS, ["link", BINDING, "gone"]], rb),
        ("rb_deep", [KEYS, ["deep", BINDING, DEEP, 0o600]], rb),
        ("bs_absent", [KEYS], store),
        ("bs_keys_missing", [], store),
        ("bs_same", [KEYS, b('{"path":"<root>/store.db"}')], store),
        ("bs_other", [KEYS, b('{"path": "/elsewhere/x.db"}')], store),
        ("bs_relative_bound", [KEYS, b('{"path": "store.db"}')], store),
        ("bs_unreadable", [KEYS, b('{"path": "/a"}', 0)], store),
        ("bs_directory", [KEYS, ["dir", BINDING, 0o700]], store),
        ("bs_garbage", [KEYS, b("{")], store),
        ("bs_deep", [KEYS, ["deep", BINDING, DEEP, 0o600]], store),
        (
            "bs_via_link",
            [KEYS, ["dir", "real", 0o700], ["link", "alias", "real"]],
            ["rebind_store", "<root>/alias/store.db"],
        ),
        ("bs_dotdot", [KEYS], ["rebind_store", "<root>/sub/../store.db"]),
        ("bs_relative", [KEYS], ["rebind_store", "store.db"]),
        ("bs_empty", [KEYS], ["rebind_store", ""]),
        ("bs_keys_file", [_f("keys", b"x")], store),
    ]


def _scenarios() -> list[Scenario]:
    return [*_key_scenarios(), *_origin_scenarios(), *_binding_scenarios()]


def _sub(text: str, root: Path) -> str:
    return text.replace(str(root), "<root>")


def _unsub(text: str, root: Path) -> str:
    return text.replace("<root>", str(root))


def _build(root: Path, setup: list[list[Any]]) -> None:
    for step in setup:
        kind, name = step[0], step[1]
        path = root / name
        if kind == "file":
            text = _unsub(bytes.fromhex(step[2]).decode("latin-1"), root)
            path.write_bytes(text.encode("latin-1"))
        elif kind == "deep":
            path.write_bytes(b"[" * step[2] + b"]" * step[2])
        elif kind == "dir":
            path.mkdir()
        elif kind == "link":
            os.symlink(_unsub(step[2], root), path)
    for step in reversed(setup):
        if step[0] in ("file", "dir", "deep"):
            os.chmod(root / step[1], step[-1])


def _snapshot(root: Path) -> list[list[Any]]:
    out: list[list[Any]] = []

    def walk(folder: Path) -> None:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return
        for name in names:
            path = folder / name
            rel = path.relative_to(root).as_posix()
            st = os.lstat(path)
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                out.append([rel, "link", _sub(os.readlink(path), root)])
            elif stat.S_ISDIR(st.st_mode):
                out.append([rel, "dir", mode])
                walk(path)
            else:
                try:
                    os.chmod(path, mode | 0o400)
                    data = path.read_bytes()
                finally:
                    os.chmod(path, mode)
                if len(data) > 256:
                    out.append([rel, "file", {"len": len(data)}, mode])
                    continue
                try:
                    text: dict[str, str] = {"text": _sub(data.decode("utf-8"), root)}
                except UnicodeDecodeError:
                    text = {"hex": data.hex()}
                if not data.isascii() or any(
                    c < 0x20 and c not in (10, 13) for c in data
                ):
                    text = {"hex": data.hex()}
                out.append([rel, "file", text, mode])

    walk(root)
    return out


def _value(value: Any, root: Path) -> Any:
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, blog.HistoryOrigin):
        return value.to_dict()
    if isinstance(value, Path):
        return {"path": _sub(str(value), root)}
    return value


def _run(ring: blog.KeyRing, op: list[Any], root: Path) -> Any:
    name = op[0]
    if name == "load_current":
        return ring.load_current()
    if name == "retire_current":
        return ring.retire_current()
    if name == "read_origin":
        return ring.read_origin()
    if name == "write_origin":
        return ring.write_origin(seq=op[1], mac=op[2], key_fingerprint=op[3])
    if name == "name_origin_key":
        return ring.name_origin_key(blog.HistoryOrigin(seq=op[1], mac=op[2]), op[3])
    if name == "replace_origin":
        arg = None if op[1] is None else blog.HistoryOrigin(**op[1])
        return ring.replace_origin(arg)
    if name == "read_binding":
        return ring.read_binding()
    if name == "rebind_store":
        cwd = os.getcwd()
        os.chdir(root)
        try:
            return ring.rebind_store(Path(_unsub(op[1], root)))
        finally:
            os.chdir(cwd)
    raise ValueError(name)


def _unlock(root: Path) -> None:
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(folder) / name
            if not path.is_symlink():
                os.chmod(path, 0o700)


def _files() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    for name, setup, op in _scenarios():
        # The scratch folder by its real path (macOS's temporary folder is
        # under a link), so a resolved path and the root spell alike.
        scratch = Path(os.path.realpath(tempfile.mkdtemp(prefix="bm-keyfiles-")))
        root = scratch / "r"
        root.mkdir()
        try:
            _build(root, setup)
            ring = blog.KeyRing(root / "keys", STORE_ID)
            try:
                out: dict[str, Any] = {"ok": _value(_run(ring, op, root), root)}
            except OSError as exc:
                raised: dict[str, Any] = {
                    "class": type(exc).__name__,
                    "errno": errno.errorcode.get(exc.errno) if exc.errno else None,
                }
                if isinstance(exc, blog.KeyUnreadableError):
                    message = _sub(str(exc), root)
                    if exc.reason.startswith("it is a symbolic link"):
                        raised["message"] = message
                    else:
                        texts.append({"case": name, "message": message})
                out = {"raised": raised}
            except (RecursionError, ValueError) as exc:
                out = {"raised": {"class": type(exc).__name__}}
            after = _snapshot(root)
        finally:
            _unlock(scratch)
            shutil.rmtree(scratch)
        rows.append(
            {"case": name, "setup": setup, "op": op, "out": out, "after": after}
        )
    return rows, texts


def _ids() -> list[dict[str, Any]]:
    values: list[Any] = [
        STORE_ID,
        "0" * 32,
        "f" * 32,
        "0" * 31,
        "0" * 33,
        STORE_ID.upper(),
        STORE_ID + "\n",
        "g" * 32,
        "０" * 32,
        "../" + "0" * 29,
        "",
        "x" * 100,
        "it's",
        "\ud800" + "0" * 31,
        "é" * 50,
        5,
        None,
        True,
        1.5,
        ["a", 1],
        {"k": "v"},
    ]
    rows = []
    for value in values:
        try:
            blog.KeyRing(Path("keys"), value)
            out: dict[str, Any] = {"ok": True}
        except ValueError as exc:
            out = {"raised": "ValueError", "text": str(exc)}
        rows.append({"part": "id", "id": neutral(value), "out": out})
    return rows


def generate() -> dict[str, bytes]:
    records = _ids()
    header = {"fixture": "log_keyfiles", "cases": len(records)}
    return {"log_keyfiles.jsonl": jsonl([header, *records])}


def generate_posix() -> dict[str, bytes]:
    rows, _texts = _files()
    header = {"fixture": "log_keyfiles_posix", "cases": len(rows), "store_id": STORE_ID}
    return {"log_keyfiles_posix.jsonl": jsonl([header, *rows])}


def generate_darwin() -> dict[str, bytes]:
    _rows, texts = _files()
    header = {"fixture": "log_keyfiles_darwin", "cases": len(texts)}
    return {"log_keyfiles_darwin.jsonl": jsonl([header, *texts])}
