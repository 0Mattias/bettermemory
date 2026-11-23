"""The two clocks and eviction order of the origin cache, and the directory
signature used to validate a capture. Each POSIX row replays a scratch
tree. File stamps are numbered by first occurrence within the row.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import location_git, origin

from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_origin_gitfiles_attrs import _git, _memos
from .gen_origin_gitfiles_walk import _INIT, _machine, _packed, _repo

_LIMIT = 600.0


def _entry(key: str, token: str, signature: Any) -> origin._OriginEntry:
    return origin._OriginEntry(
        origin.Origin(cwd=key, repo=token), (token,), signature, 0.0, 0.0
    )


def generate() -> dict[str, bytes]:
    rows: list[Any] = []
    with tempfile.TemporaryDirectory() as folder:
        cwd = Path(folder).resolve()
        key = str(cwd)
        for mono, wall, equal in (
            (0.0, 0.0, True),
            (599.999999, 599.999999, True),
            (_LIMIT, 1.0, True),
            (1.0, _LIMIT, True),
            (601.0, 601.0, True),
            (-0.001, 0.0, True),
            (0.0, -0.001, True),
            (2.0, 2.0, False),
        ):
            with (
                mock.patch.object(
                    origin, "_ORIGIN_CACHE", {key: _entry(key, "saved", (1,))}
                ),
                mock.patch.object(
                    origin,
                    "time",
                    SimpleNamespace(monotonic=lambda: mono, time=lambda: wall),
                ),
                mock.patch.object(
                    origin, "_capture_signature", return_value=(1,) if equal else (2,)
                ),
                mock.patch.object(
                    origin, "_probe_worktree_root", return_value=(None, False, None)
                ),
                mock.patch.object(origin, "_register_caller_alternates"),
            ):
                hit = origin.capture(cwd).repo == "saved"
            rows.append(["age", mono, wall, equal, hit])
    with mock.patch.object(origin, "_ORIGIN_CACHE", {}):
        ops: list[list[Any]] = [
            ["range", 0, 256],
            ["read", 0],
            ["write", 256, "new"],
            ["read", 0],
            ["read", 1],
            ["write", 1, "updated"],
            ["write", 257, "last"],
            ["read", 1],
            ["read", 2],
            ["read", 3],
            ["clear"],
            ["read", 1],
            ["read", 257],
        ]
        for op in ops:
            action, *args = op
            answer: Any = None
            if action == "range":
                for i in range(*args):
                    origin._remember(str(i), _entry(str(i), str(i), (1,)))
            elif action == "write":
                key, token = str(args[0]), args[1]
                origin._remember(key, _entry(key, token, (1,)))
            elif action == "clear":
                origin._clear_origin_cache()
            else:
                entry = origin._ORIGIN_CACHE.get(str(args[0]))
                answer = (
                    None if entry is None else [entry.origin.repo, *entry.alternates]
                )
            rows.append(["cache", op, answer])
    return {"origin_cache.jsonl": jsonl([{"rows": len(rows)}, *rows])}


def _scenarios() -> list[list[list[Any]]]:
    def sign(directory: str = "r") -> list[Any]:
        return ["sign", directory]

    cfg = _INIT + '[remote "origin"]\n url = here\n'
    linked = "r/.git/worktrees/l"
    reuse_cases = [
        [
            *_repo(),
            ["write", "machine", '[remote "origin"]\n url = before\n'],
            ["prime", "r/.git"],
            ["write", "machine", '[remote "origin"]\n url = changed value\n'],
            ["sign", "r", reuse],
            sign(),
            sign(),
        ]
        for reuse in (True, False)
    ]
    return [
        *reuse_cases,
        [
            ["dir", "r"],
            sign(),
            sign(),
            *_repo(),
            sign(),
            sign(),
            ["rename", "r", "moved"],
            sign("moved"),
            sign("r"),
        ],
        [
            *_repo(),
            sign(),
            ["write", "r/.git/config", cfg],
            sign(),
            ["write", "machine", "[include]\n path = more\n"],
            sign(),
            ["write", "more", '[url "https://host/"]\n insteadOf = host:\n'],
            sign(),
            ["write", "r/.git/remotes/origin", "URL: https://remote/\n"],
            sign(),
            ["write", "r/.git/branches/origin", "https://branch/\n"],
            sign(),
            ["rm", "r/.git/remotes/origin"],
            sign(),
        ],
        [
            *_repo(),
            ["dir", "l"],
            ["write", "l/.git", f"gitdir: <root>/{linked}\n"],
            ["write", linked + "/commondir", "../..\n"],
            ["write", linked + "/gitdir", "<root>/l/.git\n"],
            ["write", linked + "/HEAD", "ref: refs/heads/main\n"],
            sign("l"),
            sign("l"),
            ["write", "r/.git/refs/tags/main", "a" * 40 + "\n"],
            sign("l"),
            ["write", linked + "/refs/tags/main", "b" * 40 + "\n"],
            sign("l"),
            [
                "write",
                linked + "/config.worktree",
                '[remote "origin"]\n url = initial\n',
            ],
            sign("l"),
            [
                "write",
                "r/.git/config",
                _INIT.replace("version = 0", "version = 1")
                + "[extensions]\n worktreeConfig = true\n",
            ],
            sign("l"),
            ["write", linked + "/config.worktree", "[core]\n bare = false\n"],
            sign("l"),
            ["write", linked + "/HEAD", "3" * 40 + "\n"],
            sign("l"),
        ],
        [
            *_repo(),
            ["write", "target", "old"],
            sign(),
            ["link", "r/.git/refs/tags/main", "../../../../target"],
            sign(),
            ["write", "target", "new contents"],
            sign(),
        ],
        [*_repo(), sign(), ["link", "r/.git/refs/tags", "tags"], sign(), sign()],
    ]


def _posix_case(root: Path, steps: list[list[Any]]) -> list[Any]:
    past: list[Any] = []
    stamps: list[Any] = []
    answers: list[Any] = []

    def number(value: Any, seen: list[Any]) -> int:
        if value not in seen:
            seen.append(value)
        return seen.index(value)

    def stamp(value: Any) -> int | None:
        return None if value is None else number(value, stamps)

    def path(value: Any) -> str:
        return str(value).replace(str(root), "<root>")

    def files(v: Any) -> list[Any]:
        return [[path(n), stamp(s)] for n, s in v]

    for op, *args in steps:
        at = root / args[0]
        if op == "prime":
            location_git.machine_config_reads(str(at))
            answers.append(None)
            continue
        if op == "sign":
            signature: tuple[Any, ...] = origin._capture_signature(
                at, reuse=args[1] if len(args) > 1 else False
            )
            first = signature[0]
            tag = (
                path(first)
                if isinstance(first, str)
                else None
                if first is None
                else "unknown"
            )
            extended: list[Any] | str | None = None
            if len(signature) == 10:
                ext = signature[-1]
                if len(ext) == 1:
                    extended = "unknown"
                else:
                    machine, repository, legacy, spelling, *refs = ext
                    names, *reads = machine
                    extended = [
                        [list(map(path, names)), *map(files, reads)],
                        list(map(files, repository)),
                        list(map(stamp, legacy)),
                        path(spelling) if isinstance(spelling, str) else "unknown",
                        list(map(stamp, refs)),
                    ]
            answers.append([number(signature, past), tag, extended])
            continue
        if op == "dir":
            at.mkdir(parents=True, exist_ok=True)
        elif op == "rename":
            at.rename(root / args[1])
        elif op == "rm":
            at.unlink()
        else:
            at.parent.mkdir(parents=True, exist_ok=True)
            if op == "link":
                at.symlink_to(args[1])
            else:
                at.write_text(args[1].replace("<root>", str(root)), encoding="utf-8")
        answers.append(None)
    return answers


def generate_posix() -> dict[str, bytes]:
    refuse_root()
    rows: list[Any] = []
    with tempfile.TemporaryDirectory() as raw:
        for i, steps in enumerate(_scenarios()):
            root = Path(os.path.realpath(raw)) / str(i)
            root.mkdir()
            with (
                _machine(root),
                _git(),
                _memos(),
                _packed(),
                mock.patch.object(
                    location_git,
                    "_machine_configs",
                    return_value=(str(root / "machine"),),
                ),
                mock.patch.object(location_git, "_home", return_value=str(root)),
                mock.patch.object(
                    location_git, "time", SimpleNamespace(monotonic=lambda: 0.0)
                ),
                mock.patch.object(location_git, "_STAMPED_READS", {}),
                mock.patch.object(location_git, "_FRESH_READS", {}),
            ):
                rows.append([steps, _posix_case(root, steps)])
    return {"origin_cache_posix.jsonl": jsonl([{"rows": len(rows)}, *rows])}
