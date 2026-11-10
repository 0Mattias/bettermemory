"""Project stored absolute commit anchors through their checkout views.

The result is repository-relative before drift memoization, shared by the
per-memory readers and health. A path expected under the recorded home
but not placeable there is unknown, never an empty/non-applicable leg.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._paths import inside
from .attestation_paths import absolute_spelling

if TYPE_CHECKING:
    from .verify_anchoring import ClaimAnchor

UNMAPPED_ANCHOR = "unmapped_anchor"
UNMAPPED_REASON = "a commit anchor has no recorded checkout view"


def relative_under(path: str, root: str | None) -> str | None:
    """A recorded spelling below a root, in that root's path flavour.

    Parent traversal is not projected: a symlink can make lexical `..`
    differ from the filesystem's reading. POSIX backslashes remain data.
    """
    if root is None:
        return None
    flavour = posixpath if root.startswith("/") else ntpath
    parts = (
        path.split("/") if flavour is posixpath else path.replace("\\", "/").split("/")
    )
    if ".." in parts:
        return None
    path, root = flavour.normpath(path), flavour.normpath(root)
    if not inside(path, root):
        return None
    try:
        relative: str = flavour.relpath(path, root)
    except ValueError:
        return None
    return relative if flavour is posixpath else relative.replace("\\", "/")


def expected_home(path: str, roots: tuple[str, ...]) -> bool:
    """Whether the spelling names the original home rather than an external file."""
    return any(inside(path, root) for root in roots)


@dataclass(frozen=True)
class CommitAnchors:
    paths: tuple[str, ...]
    unmapped: bool = False


def project_commit_anchors(
    paths: tuple[str, ...], anchor: ClaimAnchor | None
) -> CommitAnchors:
    if anchor is None or anchor.rule == "global_record":
        return CommitAnchors(paths)
    roots = anchor.recorded_roots + (
        (anchor.origin_root,) if anchor.origin_root else ()
    )
    out: list[str] = []
    unmapped = False
    for path in paths:
        spelled = os.path.expanduser(path)
        expected = expected_home(spelled, roots)
        recorded = next(
            (
                value
                for root in sorted(anchor.recorded_roots, key=len, reverse=True)
                if (value := relative_under(spelled, root)) is not None
            ),
            None,
        )
        if recorded is not None:
            out.append(recorded)
            continue
        # A recorded Windows UNC view can establish the path's meaning
        # even on POSIX, where its leading backslashes are otherwise data.
        if not absolute_spelling(path) and not expected:
            out.append(path)
            continue
        # Preserve the existing filesystem reading for an in-place path
        # with parent traversal; never rebase it lexically across a move.
        separators = "/" if spelled.startswith("/") else "/\\"
        parts = (
            spelled.replace("\\", "/").split("/")
            if "\\" in separators
            else spelled.split("/")
        )
        if ".." in parts:
            unmapped |= expected and not (anchor.root and inside(spelled, anchor.root))
            out.append(path)
            continue
        target, _rule = anchor.absolute(spelled)
        relative = relative_under(target, anchor.root) if target is not None else None
        if relative is not None:
            out.append(relative)
        elif expected:
            unmapped = True
        else:
            out.append(path)
    return CommitAnchors(tuple(dict.fromkeys(out)), unmapped)
