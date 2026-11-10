"""Compile explicit path attestations without borrowing prose semantics.

Reference spellings remain the legacy interpretation. A compiled path is
literal on every subsequent read: existence never chooses its meaning.
New optional target maps distinguish them; legacy fields keep their meaning.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
from collections.abc import Callable, Sequence
from typing import Any, Literal
from pathlib import Path

from .verify_candidates import (
    _escapes_root,
    _normalize_candidate,
    _refused_spelling,
    _is_placeholder_path,
    _trim_candidate,
    _MAX_PATH_LENGTH,
)

PathMode = Literal["auto", "literal", "reference"]


class LiteralPaths(tuple[str, ...]):
    """Canonical targets from an explicit stamp; never prose-normalized."""


class ReferencePaths(tuple[str, ...]):
    """Legacy reference spellings, interpreted by the legacy path grammar."""


def path_mode(paths: Sequence[str]) -> PathMode:
    return (
        "literal"
        if isinstance(paths, LiteralPaths)
        else ("reference" if isinstance(paths, ReferencePaths) else "auto")
    )


def interpret_path(
    raw: str, *, mode: PathMode = "auto", root: str | None = None
) -> str | None:
    """A request's path spelling, before joining or anchor translation.

    Auto interprets a newly supplied positive name against a literal
    target that exists now, then a reference. A stored list always names
    its representation explicitly, so later deletion never chooses one.
    """
    literal = literal_path(raw)
    if mode == "literal":
        return literal
    if (
        mode == "auto"
        and literal is not None
        and (root is not None or absolute_spelling(literal))
    ):
        try:
            impossible = os.name == "nt" and any(
                ch in ntpath.splitdrive(literal)[1] for ch in '<>:"|?*'
            )
            if not impossible and _may_be_literal(target_path(literal, root)):
                return literal
        except (OSError, ValueError, RuntimeError):
            pass
    return reference_path(raw)


def _may_be_literal(path: str) -> bool:
    """An existing directory entry or an unreadable one; no fallback on I/O failure.

    A broken symlink still names a literal entry. Admission checks its
    target separately and refuses it instead of verifying a reference twin.
    """
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError, RuntimeError):
        return True
    return True


def joined_path(root: str | Path, raw: str, *, mode: PathMode = "auto") -> str | None:
    """An attestation joined to its root using its recorded interpretation."""
    spelled = interpret_path(raw, mode=mode, root=str(root))
    if spelled is None:
        return None
    try:
        return target_path(spelled, str(root))
    except (OSError, ValueError, RuntimeError):
        return None


# A lexical stand-in for the old validator's absolute-path gate. It is
# never read from disk and never becomes a stored path.
_REFERENCE_ROOT = "/__attested_reference__"


def absolute_spelling(path: str) -> bool:
    """An absolute, home or drive spelling, independent of this host."""
    return path.startswith(("/", "~", "$HOME/", "${HOME}/")) or (
        len(path) >= 3 and path[0].isalpha() and path[1] == ":" and path[2] in "/\\"
    )


def literal_path(raw: str) -> str | None:
    """Validate a literal path without trimming punctuation or line suffixes.

    No prose regex runs over its contents. The request/record size and
    attestation-list bounds apply; a real long filename is not a prose
    candidate to drop at that extractor's length cap.
    """
    if not raw or _refused_spelling(raw):
        return None
    if not absolute_spelling(raw) and _escapes_root(raw):
        return None
    if raw.startswith("$HOME/"):
        return "~" + raw[len("$HOME") :]
    if raw.startswith("${HOME}/"):
        return "~" + raw[len("${HOME}") :]
    return raw


def reference_path(raw: str) -> str | None:
    """The path the legacy reference-style write gate interpreted."""
    spelled = raw.strip()
    if not spelled:
        return None
    if absolute_spelling(spelled):
        return _normalize_candidate(spelled)
    if _escapes_root(spelled):
        return None
    joined = _normalize_candidate(f"{_REFERENCE_ROOT}/{spelled}")
    prefix = _REFERENCE_ROOT + "/"
    return (
        joined[len(prefix) :]
        if joined is not None and joined.startswith(prefix)
        else None
    )


def placeholder_spelling(
    raw: str, *, mode: PathMode = "auto", root: str | None = None
) -> bool:
    """A refused placeholder, including its bounded reference aliases.

    A real literal punctuation name keeps its identity. A reference that
    normalization would drop must instead reach the explicit refusal gate.
    """
    if _is_placeholder_path(raw.strip()):
        return True
    if mode == "literal" or len(raw) > _MAX_PATH_LENGTH + 64:
        return False
    canonical = _trim_candidate(raw)
    if not _is_placeholder_path(canonical):
        return False
    if mode == "reference":
        return True
    literal = literal_path(raw)
    if literal is None or posixpath.normpath(literal) == posixpath.normpath(canonical):
        return True
    try:
        return not _may_be_literal(target_path(literal, root))
    except (OSError, ValueError, RuntimeError):
        return True


def target_path(spelled: str, root: str | None) -> str:
    """Resolve a spelling in its declared root, refusing relative escapes.

    Windows roots use Windows path rules even on a POSIX reader; POSIX
    roots preserve literal backslashes. This is path arithmetic alone.
    """
    if absolute_spelling(spelled):
        return (
            os.path.normpath(os.path.expanduser(spelled))
            if spelled.startswith("~")
            else spelled
        )
    if root is None:
        raise ValueError("a relative path attestation needs a declared root")
    if _escapes_root(spelled):
        raise ValueError("a relative path attestation cannot escape its declared root")
    paths = ntpath if ntpath.splitdrive(root)[0] else posixpath
    base: str = paths.normpath(root)
    target: str = paths.normpath(paths.join(base, spelled))
    try:
        inside = paths.commonpath((base, target))
    except ValueError:
        inside = ""
    if paths.normcase(inside) != paths.normcase(base):
        raise ValueError("a relative path attestation cannot escape its declared root")
    return target


def record_paths(record: Any, *, absent: bool = False) -> tuple[str, ...]:
    """The representation of one stored attestation leg, without a probe."""
    field = "verified_absent_path_targets" if absent else "verified_path_targets"
    legacy = "verified_absent_paths" if absent else "verified_paths"
    targets = getattr(record, field, None)
    return (
        ReferencePaths(getattr(record, legacy))
        if targets is None
        else LiteralPaths(target for target in targets.values() if target is not None)
    )


def commit_paths(record: Any) -> tuple[str, ...]:
    """Stored positive anchors and newly explicit negative targets.

    Legacy negatives retain the existing body-only commit-anchor policy.
    An explicit negative target is new evidence and anchors its appearance.
    """
    positive = record_paths(record)
    negative = getattr(record, "verified_absent_path_targets", None)
    if negative is None:
        return positive
    values = (target for target in negative.values() if target is not None)
    normalized = (
        positive
        if isinstance(positive, LiteralPaths)
        else tuple(
            path for raw in positive if (path := reference_path(raw)) is not None
        )
    )
    return LiteralPaths((*normalized, *values))


def compile_targets(
    values: Sequence[str],
    *,
    root: str | None,
    mode: PathMode = "auto",
    absent: bool = False,
) -> tuple[list[str], dict[str, str | None]]:
    """Freeze a request's targets and its safely representable legacy subset.

    The old list keeps only entries whose reference grammar describes the
    same target. A literal name that grammar trims lives solely in the new
    optional map; no old field asserts a different file on its behalf.
    Inert spellings retain null targets, as the existing admission gate
    skips shapes that cannot name a concrete local file.
    """
    if mode not in ("auto", "literal", "reference"):
        raise ValueError("path_mode must be auto, literal or reference")
    legacy: list[str] = []
    targets: dict[str, str | None] = {}
    for raw in values:
        chosen = (
            literal_path(raw)
            if absent and mode != "reference"
            else interpret_path(raw, mode=mode, root=root)
        )
        if not absent and placeholder_spelling(raw, mode=mode, root=root):
            # The admission gate explicitly refuses placeholders. Its
            # lexical evidence must survive reference normalization.
            chosen = literal_path(raw)
        if chosen is not None and chosen.startswith("~"):
            chosen = os.path.normpath(os.path.expanduser(chosen))
        targets[raw] = chosen
        reference = reference_path(raw)
        if chosen is None:
            legacy.append(raw)
        elif reference is not None:
            try:
                same = target_path(reference, root) == target_path(chosen, root)
            except (OSError, ValueError, RuntimeError):
                same = reference == chosen
            if same:
                legacy.append(raw)
    return legacy, targets


def unverifiable_inputs(
    record: Any, probe: Callable[[tuple[str, ...]], list[str]]
) -> list[str]:
    """New inputs whose frozen targets fail a probe; legacy behavior otherwise."""
    targets = getattr(record, "verified_path_targets", None)
    if targets is None:
        return probe(ReferencePaths(record.verified_paths))
    return [
        raw
        for raw, target in targets.items()
        if target is not None and probe(LiteralPaths((target,)))
    ]
