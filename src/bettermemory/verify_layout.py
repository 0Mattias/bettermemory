"""The any-touch floor for literal bindings a patch cannot address.

Source layout belongs to the checked commit window, never the working
tree: a continuation line can move without touching its binding line.
Both sides of every change matter even when HEAD is single-line again.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import _git, gitfile_rule, origin, origin_history
from .claims import Claim, _MAX_MODULE_BYTES, path_key
from .origin_patch import MAX_PATCH_STREAM_BYTES, _held, _listed
from .verify_spelling import Spelled


@dataclass(frozen=True)
class LiteralFloor:
    paths: frozenset[str] = frozenset()
    commits: frozenset[str] = frozenset()
    listed: frozenset[str] = frozenset()


def literal_floor(
    cwd: Path,
    shas: Sequence[str],
    specs: Sequence[str],
    pathspecs: list[str],
    claims: Sequence[Claim],
    spell: Spelled,
    *,
    toplevel: Path | None,
) -> LiteralFloor | None:
    """Literal paths with an unaddressable binding in any source version.

    Reuse the patch's bounded tree/blob listing. One byte-preserving
    batch reads the distinct local blobs, under the same no-fetch pins.
    The existing AST module ceiling applies to each, and the patch's
    byte budget to their total. Unknown layouts keep the file's exact
    tree-listed touch count, never an assertion about a named binding.
    """
    governed = {path_key(path) for path in specs}
    wanted: dict[str, set[str]] = {}
    for claim in claims:
        if claim.kind == "literal" and path_key(claim.rel_path) in governed:
            for name in spell.of(claim.rel_path):
                wanted.setdefault(name, set()).add(claim.name)
    if not wanted:
        return LiteralFloor()
    root = toplevel if toplevel is not None else origin.repo_toplevel(cwd)
    if root is None:
        return None
    listed = _listed(root, shas, pathspecs)
    if listed is None:
        return None
    changes, held = listed
    by_path: dict[str, set[str]] = {path: set() for path in wanted}
    blocked: set[str] = set()
    for found in changes.values():
        for change in found:
            if change.path in wanted:
                by_path[change.path].update(change.blobs)
                if change.status == "T":
                    blocked.add(change.path)
    missing = {blob for blobs in by_path.values() for blob in blobs if blob not in held}
    if missing:
        # A loose object can arrive without changing the pack-directory
        # stamp of the listing's memo. Recheck just its missing objects.
        held = dict(held) | (_held(root, missing) or {})
    for path, blobs in by_path.items():
        if not blobs or any(
            blob not in held or held[blob] > _MAX_MODULE_BYTES for blob in blobs
        ):
            blocked.add(path)
        if any(blob not in held for blob in blobs):
            # A fetched blob under the same HEAD can recover precision.
            # Keep no HEAD-keyed result based on its unavailable layout.
            _git.note_failed()
    asked = {blob for p, blobs in by_path.items() if p not in blocked for blob in blobs}
    if sum(held[blob] for blob in asked) > MAX_PATCH_STREAM_BYTES:
        blocked.update(wanted)
        asked.clear()
    sources = _read_blobs(root, asked, held) if asked else {}
    if sources is None:
        blocked.update(wanted)
        sources = {}
    layouts = {
        blob: _single_line_assignments(source) for blob, source in sources.items()
    }
    for path, blobs in by_path.items():
        if path not in blocked and any(
            not wanted[path] <= layouts[blob] for blob in blobs
        ):
            blocked.add(path)
    return LiteralFloor(
        frozenset(blocked),
        frozenset(
            sha
            for sha, found in changes.items()
            if any(c.path in blocked for c in found)
        ),
        frozenset(c.path for found in changes.values() for c in found),
    )


def _read_blobs(
    root: Path, blobs: set[str], held: Mapping[str, int]
) -> dict[str, bytes] | None:
    """Read known bounded blob identities without decoding the framing."""
    names = sorted(blobs)
    if gitfile_rule.discovery_refusal(os.fspath(root)) is not None or not all(
        origin.is_full_commit_sha(name) for name in names
    ):
        _git.note_failed()
        return None
    done = _git.run_git(
        (*origin_history.AS_STORED_CONFIG, "cat-file", "--batch"),
        cwd=root,
        stdin="".join(name + "\n" for name in names).encode("ascii"),
        timeout=5.0,
        text=False,
        env=origin_history.AS_STORED_ENV,
    )
    if done is None or done.returncode:
        if done is not None:
            _git.note_failed()
        return None
    found: dict[str, bytes] = {}
    raw, offset = done.stdout, 0
    for name in names:
        header = f"{name} blob {held[name]}\n".encode("ascii")
        if not raw.startswith(header, offset):
            break
        start = offset + len(header)
        end = start + held[name]
        if raw[end : end + 1] != b"\n":
            break
        found[name] = raw[start:end]
        offset = end + 1
    if len(found) != len(names) or offset != len(raw):
        _git.note_failed()
        return None
    return found


def _single_line_assignments(source: bytes) -> set[str]:
    """First supported assignments that the column-zero detector can see."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return set()
    found: set[str] = set()
    seen: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id not in seen:
                seen.add(target.id)
                if node.col_offset == 0 and node.lineno == node.end_lineno:
                    found.add(target.id)
    return found
