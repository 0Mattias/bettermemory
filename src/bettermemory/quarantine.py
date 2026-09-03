"""Quarantine: pulled memory files the admission gates refused.

`bettermemory sync pull` runs an admission chain over every memory file
a rebase brought down (`sync._admit_pulled_files`): a size cap, the
store's own parser, an id-alias check against the active set, and the
credential gate the write path runs. A file that fails is neither
deleted nor rewritten. It stays where git put it, tracked and
unchanged, so the worktree stays clean and no deletion propagates to
the remote or to the other hosts. It is excluded from the store's
active set instead, through the host-local sidecar this module owns,
and so never reaches the index, a search hit, a listing, `memory_show`,
`memory_health` or the recall hook.

The sidecar maps a filename to its refusal: the reason, a short detail
that never carries body text, the remote, when, and the size and sha256
of the refused bytes. The digest binds the entry to those bytes: every
later pull re-runs admission over the quarantined files, so a file
fixed upstream is admitted and its entry dropped, and `bettermemory
sync quarantine --release` runs the same chain by hand (`--force`
admits the file as it is).

Host-local by construction, like the proposals queue: it names files
by their position in this checkout, and pushing it would carry one
host's refusals into another host's store. `sync._GITIGNORE_LINES`
lists it. An unreadable sidecar reads as empty with a warning, and
`bettermemory doctor` reports it: the sidecar lives inside the store
directory, so it is exactly as trustworthy as the files it guards, and
a control that fails closed on its own corruption would take every read
down with it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._fsutil import atomic_write_bytes

log = logging.getLogger("bettermemory.quarantine")

QUARANTINE_FILENAME = ".quarantine.json"

REASON_CREDENTIAL = "credential"
REASON_OVERSIZE = "oversize"
REASON_UNPARSEABLE = "unparseable"
REASON_ID_ALIAS = "id_alias"
REASONS: tuple[str, ...] = (
    REASON_CREDENTIAL,
    REASON_OVERSIZE,
    REASON_UNPARSEABLE,
    REASON_ID_ALIAS,
)

_FORMAT_VERSION = 1
_HASH_CHUNK = 1 << 16


@dataclass(frozen=True)
class QuarantineEntry:
    """One refused file. `detail` is diagnostic only and never quotes
    the body: a credential refusal names the detector kinds, an alias
    refusal names the other filename, a parse refusal names the
    exception class."""

    filename: str
    reason: str
    detail: str
    remote: str
    pulled_at: str
    size: int
    sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "remote": self.remote,
            "pulled_at": self.pulled_at,
            "size": self.size,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, filename: str, raw: Any) -> QuarantineEntry | None:
        if not isinstance(raw, dict):
            return None
        reason = raw.get("reason")
        if reason not in REASONS:
            return None
        size = raw.get("size")
        sha256 = raw.get("sha256")
        return cls(
            filename=filename,
            reason=reason,
            detail=str(raw.get("detail") or ""),
            remote=str(raw.get("remote") or ""),
            pulled_at=str(raw.get("pulled_at") or ""),
            size=size if isinstance(size, int) and not isinstance(size, bool) else 0,
            sha256=sha256 if isinstance(sha256, str) else None,
        )


def quarantine_path(root: Path) -> Path:
    return Path(root) / QUARANTINE_FILENAME


def load_quarantine(root: Path) -> dict[str, QuarantineEntry]:
    """The sidecar's entries by filename. Never raises: an absent
    sidecar is the common case and reads as empty; an unreadable one
    reads as empty too, with a warning, and doctor's `sync_quarantine`
    check reports it (see the module docstring for why it fails open).
    Entries whose filename could reach outside the store root (a path
    separator, `..`) are dropped on read."""
    path = quarantine_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("quarantine sidecar %s is unreadable: %s", path, exc)
        return {}
    try:
        payload = json.loads(text)
    except ValueError as exc:
        log.warning("quarantine sidecar %s is not valid JSON: %s", path, exc)
        return {}
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        log.warning("quarantine sidecar %s has an unexpected shape", path)
        return {}
    out: dict[str, QuarantineEntry] = {}
    for filename, raw in files.items():
        if not _safe_filename(filename):
            continue
        entry = QuarantineEntry.from_dict(filename, raw)
        if entry is not None:
            out[filename] = entry
    return out


def sidecar_unreadable(root: Path) -> str | None:
    """A one-line description when the sidecar exists but cannot be
    read as this module writes it, else None. `load_quarantine` reads
    such a file as empty; this is the check that makes that visible."""
    path = quarantine_path(root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
        return "unexpected shape"
    return None


def save_quarantine(root: Path, entries: dict[str, QuarantineEntry]) -> None:
    """Write the sidecar atomically at 0o600, or remove it when there is
    nothing to hold, so an empty quarantine leaves no file behind."""
    path = quarantine_path(root)
    if not entries:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    payload = {
        "version": _FORMAT_VERSION,
        "files": {
            name: entries[name].to_dict()
            for name in sorted(entries)
            if _safe_filename(name)
        },
    }
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(path, data, mode=0o600)


def quarantined_names(root: Path) -> frozenset[str]:
    """The filenames the store must skip. One `stat` when no sidecar
    exists, which is every store that never quarantined anything."""
    if not quarantine_path(root).exists():
        return frozenset()
    return frozenset(load_quarantine(root))


def file_digest(path: Path, *, max_bytes: int) -> tuple[int, str | None]:
    """`(size, sha256)` of `path`. The digest is None above `max_bytes`:
    a file the store would refuse to read is not read here either, so a
    hostile multi-gigabyte push costs one stat."""
    size = path.stat().st_size
    if size > max_bytes:
        return size, None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return size, digest.hexdigest()


def _safe_filename(name: Any) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and "/" not in name
        and "\\" not in name
        and name not in (".", "..")
    )


__all__ = [
    "QUARANTINE_FILENAME",
    "REASONS",
    "REASON_CREDENTIAL",
    "REASON_ID_ALIAS",
    "REASON_OVERSIZE",
    "REASON_UNPARSEABLE",
    "QuarantineEntry",
    "file_digest",
    "load_quarantine",
    "quarantine_path",
    "quarantined_names",
    "save_quarantine",
    "sidecar_unreadable",
]
