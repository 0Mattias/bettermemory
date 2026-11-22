"""Fixtures: `project_file.py`, unit 3, the walk and `write`
(``project_walk.jsonl``, POSIX only).

The native build reads them in
``native/crates/bettermemory-core/src/project_walk.rs``.

Each ``find`` row builds a tree of folders, project files and links in a
fresh temporary folder and calls `find` there. Every path a row holds is
spelled relative to that folder (``.`` for the folder itself; a message
names it ``B``), so no row records a temporary path. A walk reads up to
the filesystem root: an item outside the folder stops the generator.
The rows with no repository root, and those whose root is ``/``, walk
the real ancestors of the temporary folder, so the output holds only
where no ancestor of the host's temporary folder holds a
``bettermemory.toml``; where one does, `_rel` raises AssertionError and
nothing is written.

A ``tree`` row lists the tree every ``find`` row builds: a file with its
bytes in hex, a folder, a link with its target, a mode set last.
``pf`` maps each project file the walk can stat to `_parse_stat`'s answer
(unit 2's, which the native walk takes as a closure); ``o`` spells the
`Walk` and every property, or ``err`` what `find` raises. ``cache`` names
an earlier `find` whose `Walk.above` is passed back, and ``add`` files made
between the two calls, which a reused `above` does not see.

``write`` rows call `write` with given random bytes and record the id, the
SHA-256 of the file's bytes after the call, or the exception's class and
(for a ValueError and the `FileNotFoundError` of a missing folder) its
message; an OSError by its errno name.

POSIX only: the tree holds symbolic links and a mode-000 folder, the
unreadable rows carry the C library's strerror text, and the paths are
spelled as the host's `Path` spells them.
"""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from bettermemory import project_file as pf

from ._common import jsonl
from .gen_checkout_token import _urandom

GOOD_ID = "bmp_" + "0123456789ABCDEFGHJKMNPQRS"
OTHER_ID = "bmp_" + "ABCDEFGHJKMNPQRS0123456789"
RANDOM = "0123456789abcdef0123456789abcdef42"


def _good(name: str | None = None, boundary: bool = False, ident: str = GOOD_ID) -> str:
    body = f'[project]\nid = "{ident}"\n'
    if name is not None:
        body += f'name = "{name}"\n'
    if boundary:
        body += "boundary = true\n"
    return body


# The tree: path -> content (text or bytes), or ("dir",), ("link", target), ("mode", bits).
TREE: dict[str, Any] = {
    "bettermemory.toml": _good("top", boundary=True, ident=OTHER_ID),
    "ws/bettermemory.toml": _good("workspace"),
    "ws/repo/bettermemory.toml": _good("repo"),
    "ws/repo/a/bettermemory.toml": _good(None, boundary=True),
    "ws/repo/a/b/bettermemory.toml": "[other]\nx = 1\n",
    "ws/repo/a/b/c": ("dir",),
    "ws/repo/sib/bettermemory.toml": _good("sibling"),
    "ws/repo/odd/bettermemory.toml": ("dir",),
    "ws/repo/file": "plain\n",
    "ws/repo/linked/bettermemory.toml": ("link", "../bettermemory.toml"),
    "ws/repo/dangling/bettermemory.toml": ("link", "nowhere/bettermemory.toml"),
    "ws/repo/loop/bettermemory.toml": ("link", "bettermemory.toml"),
    "ws/repo/bad/bettermemory.toml": '[project]\nid = "bmp_short"\n',
    "ws/repo/bad/deep": ("dir",),
    "ws/repo/locked/in": ("dir",),
    "ws/repo/locked": ("mode", 0o000),
    "plain/bettermemory.toml": _good("folder"),
    "plain/sub/bettermemory.toml": b'[project]\nid = "\xff"\n',
    "plain/sub/inner": ("dir",),
}

# (cwd, root, cache, add); cache is (cwd, root) of the earlier call.
CASES: list[tuple[str, str | None, tuple[str, str | None] | None, str | None]] = [
    ("ws/repo/a/b/c", "ws/repo", None, None),
    ("ws/repo", "ws/repo", None, None),
    ("ws/repo/a", "ws/repo/a", None, None),
    ("ws/repo/sib", "ws/repo/a", None, None),
    ("ws/repo/a/b/c", None, None, None),
    ("ws/repo/a/b/c/../../b/./c/", "ws/repo/", None, None),
    ("ws", None, None, None),
    ("ws/repo/odd", "ws/repo", None, None),
    ("ws/repo/file", "ws/repo", None, None),
    ("ws/repo/file/x", "ws/repo", None, None),
    ("ws/repo/linked", "ws/repo", None, None),
    ("ws/repo/dangling", "ws/repo", None, None),
    ("ws/repo/loop", "ws/repo", None, None),
    ("ws/repo/bad/deep", "ws/repo", None, None),
    ("ws/repo/locked/in", "ws/repo", None, None),
    ("plain/sub/inner", None, None, None),
    ("plain/sub/inner", "plain/sub", None, None),
    ("ws/repo/a", "ws/repo", ("ws/repo", "ws/repo"), "ws/bettermemory.toml"),
    ("ws/repo/a", "ws/repo", ("ws/repo/sib", "ws/repo"), None),
    ("ws/repo", None, ("ws/repo", "ws/repo"), None),
    ("ws", "/", None, None),
    ("ws", "ws/repo", ("ws", "/"), None),
    ("ws", "/", ("ws", "/"), None),
    ("ws", "/", ("ws", None), None),
]


def _build(base: Path) -> None:
    modes: list[tuple[Path, int]] = []
    for rel, spec in TREE.items():
        path = base / rel
        if isinstance(spec, bytes):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(spec)
        elif isinstance(spec, str):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(spec, encoding="utf-8")
        elif spec[0] == "dir":
            path.mkdir(parents=True, exist_ok=True)
        elif spec[0] == "link":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(spec[1])
        else:
            modes.append((path, spec[1]))
    for path, bits in modes:
        path.chmod(bits)


def _unlock(base: Path) -> None:
    for rel, spec in TREE.items():
        if isinstance(spec, tuple) and spec[0] == "mode":
            (base / rel).chmod(0o755)


def _rel(base: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    if path == base:
        return "."
    if str(path) == "/":
        return "/"
    if not path.is_relative_to(base):
        raise AssertionError("a walk item lies outside the scratch folder")
    return str(path.relative_to(base))


def _found(base: Path, item: pf.Found) -> dict[str, Any]:
    if isinstance(item, pf.ProjectFile):
        return {
            "p": _rel(base, item.path),
            "id": item.id,
            "n": item.name,
            "b": item.boundary,
        }
    return {"p": _rel(base, item.path), "r": item.reason, "d": item.detail}


def _at(base: Path, rel: str) -> str:
    return rel if rel.startswith("/") else str(base) + "/" + rel


def _arg(base: Path, rel: str | None) -> str | None:
    return None if rel is None else _at(base, rel)


def _walk_out(base: Path, walk: pf.Walk) -> dict[str, Any]:
    def rels(files: tuple[pf.ProjectFile, ...]) -> list[str | None]:
        return [_rel(base, f.path) for f in files]

    def one(file: pf.ProjectFile | None) -> str | None:
        return None if file is None else _rel(base, file.path)

    return {
        "cwd": _rel(base, walk.cwd),
        "root": _rel(base, walk.repository_root),
        "inner": [_found(base, i) for i in walk.inner],
        "start": _rel(base, walk.above.start),
        "above": [_found(base, i) for i in walk.above.items],
        "items": [_rel(base, i.path) for i in walk.items],
        "files": rels(walk.files),
        "errors": [_rel(base, e.path) for e in walk.errors],
        "rootf": one(walk.root),
        "nested": rels(walk.nested),
        "folder": one(walk.folder),
        "enclosing": rels(walk.enclosing),
        "nearest": one(walk.nearest),
        "boundaries": rels(walk.boundaries),
        "place": [walk.placement(f) for f in walk.files],
    }


def _parsed(base: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for rel in sorted(TREE):
        path = base / rel
        if path.name != pf.PROJECT_FILE_NAME:
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        out[rel] = _found(base, pf._parse_stat(path, st))
    return out


def _message(base: Path, exc: Exception) -> str:
    return str(exc).replace(str(base), "B")


def _tree_row() -> dict[str, Any]:
    entries: list[list[Any]] = []
    for rel, spec in TREE.items():
        if isinstance(spec, str):
            entries.append([rel, "file", spec.encode("utf-8").hex()])
        elif isinstance(spec, bytes):
            entries.append([rel, "file", spec.hex()])
        else:
            entries.append([rel, *spec])
    return {"k": "tree", "entries": entries}


def _find_rows(base: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [_tree_row()]
    for cwd, root, cache, add in CASES:
        row: dict[str, Any] = {"k": "find", "cwd": cwd, "root": root}
        _build(base)
        try:
            row["pf"] = _parsed(base)
            above = None
            if cache is not None:
                row["cache"] = list(cache)
                above = pf.find(_at(base, cache[0]), _arg(base, cache[1])).above
            if add is not None:
                row["add"] = [add, _good("added")]
                (base / add).write_text(_good("added"), encoding="utf-8")
            try:
                walk = pf.find(_at(base, cwd), _arg(base, root), above=above)
            except ValueError as exc:
                row["err"] = {"c": "ValueError", "m": _message(base, exc)}
            else:
                row["o"] = _walk_out(base, walk)
        finally:
            _unlock(base)
            _clear(base)
        rows.append(row)
    return rows


def _clear(base: Path) -> None:
    for root, dirs, files in os.walk(base, topdown=False):
        for name in files:
            os.unlink(os.path.join(root, name))
        for name in dirs:
            path = os.path.join(root, name)
            if os.path.islink(path):
                os.unlink(path)
            else:
                os.rmdir(path)


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# (directory, name, boundary, rebind); the steps run in order in one folder.
WRITES: list[tuple[str, str, bool, bool]] = [
    ("w", "n", False, False),
    ("w", "n", False, False),
    ("w", "renamed", True, True),
    ("w", "a\nb", False, True),
    ("w", " ", False, False),
    ("missing", "n", False, False),
    ("file", "n", False, False),
    ("v", "\ud800", False, False),
    ("v", '"q" \\ é', True, False),
    ("v/../u", "x", False, False),
    ("v/../u/", "\u009f ok", False, False),
    ("u", "plain", False, True),
]


def _write_rows(base: Path) -> list[dict[str, Any]]:
    for rel in ("w", "v", "u"):
        (base / rel).mkdir()
    (base / "file").write_text("x\n", encoding="utf-8")
    rows: list[dict[str, Any]] = []
    for folder, name, boundary, rebind in WRITES:
        row: dict[str, Any] = {
            "k": "write",
            "dir": folder,
            "name": [ord(c) for c in name],
            "b": boundary,
            "rebind": rebind,
            "x": RANDOM,
        }
        try:
            with _urandom(bytes.fromhex(RANDOM)):
                project = pf.write(
                    _at(base, folder),
                    name=name,
                    boundary=boundary,
                    rebind=rebind,
                )
        except (ValueError, FileNotFoundError) as exc:
            if isinstance(exc, UnicodeEncodeError):
                row["err"] = {"c": "UnicodeEncodeError"}
            elif isinstance(exc, FileNotFoundError) and exc.errno is not None:
                row["err"] = {"c": "OSError", "e": errno.errorcode[exc.errno]}
            else:
                row["err"] = {"c": type(exc).__name__, "m": _message(base, exc)}
        except OSError as exc:
            assert exc.errno is not None
            row["err"] = {"c": "OSError", "e": errno.errorcode[exc.errno]}
        else:
            row["o"] = {"id": project.id, "p": _rel(base, project.path)}
        row["sha"] = _sha(base / os.path.normpath(folder) / pf.PROJECT_FILE_NAME)
        rows.append(row)
    return rows


def generate() -> dict[str, bytes]:
    """``project_walk.jsonl``: POSIX only."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(os.path.realpath(tmp))
        (base / "t").mkdir()
        (base / "w").mkdir()
        rows = _find_rows(base / "t") + _write_rows(base / "w")
    return {"project_walk.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])}
