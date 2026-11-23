"""The caller's alternate spellings and its linked worktree marker."""

from __future__ import annotations

import functools
import os
import random
import tempfile
from contextlib import chdir
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import origin

from ._common import jsonl


def generate() -> dict[str, bytes]:
    rng = random.Random(805931)
    pool = [
        "",
        "opaque",
        "OPAQUE",
        "https://example.invalid/Team/Repo.git",
        "git@example.invalid:team/repo",
        "alias:Team/Repo",
        "ssh://mirror.invalid/Team/Archive",
        "https://example.invalid/TEΑM/ΟΣ",
        "git@example.invalid:teαm/ος",
        "https://example.invalid/ß/one",
        "https://example.invalid/SS/one",
        "https://example.invalid/İ/x",
        "https://example.invalid/i\u0307/x",
        "opaque\udcfe",
        "opaque\udcff",
        "opaque\0",
        *(f"local spelling {i}" for i in range(39)),
    ]
    rows: list[Any] = []

    def register(key: int, values: list[int]) -> None:
        origin._register_caller_alternates(pool[key], tuple(pool[i] for i in values))
        rows.append(["set", key, values])

    def query(a: int | None, b: int | None, values: list[int] | None) -> None:
        ok = origin.repos_match(
            None if a is None else pool[a],
            None if b is None else pool[b],
            caller_alternates=None
            if values is None
            else tuple(pool[i] for i in values),
        )
        rows.append(["match", a, b, values, ok])

    with patch.object(origin, "_CALLER_REPO_ALTERNATES", {}):
        for a, b in [(3, 4), (7, 8), (9, 10), (11, 12), (13, 14), (1, 2)]:
            query(a, b, None)
        register(3, [3, 6, 5])
        query(5, 3, None)
        query(3, 5, None)
        query(5, 3, [])
        query(5, 4, None)
        register(3, [])
        query(5, 3, None)
        for i in range(16, 47):
            register(i, [2])
        register(3, [5])
        register(47, [5])
        query(5, 3, None)
        query(2, 16, None)
        ids: list[int | None] = [None, *range(len(pool))]
        for _ in range(1350):
            if rng.randrange(4) == 0:
                register(
                    rng.randrange(len(pool)),
                    rng.sample(range(len(pool)), rng.randrange(4)),
                )
            else:
                query(
                    rng.choice(ids),
                    rng.choice(ids),
                    None
                    if rng.randrange(3)
                    else rng.sample(range(len(pool)), rng.randrange(4)),
                )
    header = {"pool": [list(map(ord, value)) for value in pool], "rows": len(rows)}
    return {"origin_match.jsonl": jsonl([header, *rows])}


def generate_posix() -> dict[str, bytes]:
    # Replace the decorated function, restoring the same wrapper and every
    # pre-existing (including negative) cache entry when generation finishes.
    primary = functools.lru_cache(maxsize=64)(origin._primary_root_of.__wrapped__)
    contents = [
        b"",
        b"gitdir: nowhere\n",
        b"gitdir: <ROOT>/primary/.git/worktrees/leaf\n",
        b"first\rgitdir: <ROOT>/primary/.git/worktrees/leaf\r",
        b"gitdir:\r\n  <ROOT>/primary/.git/worktrees/leaf\r\n",
        b"gitdir: <ROOT>/a/../primary/.git/worktrees/leaf",
        b"gitdir: <ROOT>/a/\\.git\\worktrees\\x/.git/worktrees/y",
        b"gitdir: <ROOT>/primary\\.git\\worktrees\\leaf",
        b"gitdir: <ROOT>/raw-\xff/.git/worktrees/x",
        b" gitdir: <ROOT>/primary/.git/worktrees/x",
        b"gitdir:   \n gitdir: relative/primary/.git/worktrees/x",
        b"gitdir: /.git/worktrees/x",
        b"gitdir: ../leaf/.git/worktrees/x",
        b"gitdir: a/../leaf/.git/worktrees/x",
        b"ignored\xe2\x80\xa8gitdir: <ROOT>/primary/.git/worktrees/x",
        b"gitdir:\xe2\x80\x87<ROOT>/primary/.git/worktrees/x",
        b"gitdir: <ROOT>/one/.git/worktrees/\ngitdir: <ROOT>/two/.git/worktrees/x",
    ]
    rows: list[Any] = []
    with (
        tempfile.TemporaryDirectory() as directory,
        tempfile.TemporaryDirectory(dir=Path(directory).resolve()) as private_cwd,
        chdir(private_cwd),
        patch.object(origin, "_primary_root_of", primary),
    ):
        cwd = Path.cwd()
        root = cwd.parent / "markers"
        root.mkdir()
        for i, body in enumerate(contents):
            worktree = root / f"link{i}"
            worktree.mkdir()
            (worktree / ".git").write_bytes(body.replace(b"<ROOT>", os.fsencode(root)))
            value = origin._primary_root_of(str(worktree))
            if value is not None:
                # Both the caller and its parent are private to this fixture.
                for path, label in [
                    (root, "<ROOT>"),
                    (cwd, "<CWD>"),
                    (cwd.parent, "<PARENT>"),
                ]:
                    if value == str(path) or value.startswith(str(path) + "/"):
                        value = label + value[len(str(path)) :]
                        break
            rows.append([list(body), None if value is None else list(map(ord, value))])
    return {"origin_match_posix.jsonl": jsonl([{"rows": len(rows)}, *rows])}
