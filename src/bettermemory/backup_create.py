"""The steps of ``backup.create_backup`` that fill the bundle.

``create_backup`` snapshots the store and writes the bundle's head; with
``include_key`` it writes the held keys through ``_write_bundle_keys``, and
``_bundle_manifest`` hashes what was written into the manifest, records the
origin anchor and signs it when the store's key is held. The bundle's
format is in ``backup``'s module docstring. ``backup`` imports these only
when a backup runs, and they read ``backup``'s names through the module
when they run, so a patch on ``backup`` reaches them.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import backup as _backup
from .log import KeyRing

if TYPE_CHECKING:
    from .backup import SnapshotFacts


def _write_bundle_keys(
    bundle: Path,
    facts: SnapshotFacts,
    *,
    held: dict[str, bytes | None],
    unreadable: dict[str, str],
    current_key: bytes | None,
    keys_root: Path,
    warnings: list[str],
    written: list[Path],
) -> None:
    """Write the held keys into the bundle for --include-key, adding to ``written`` and ``warnings``; returns nothing."""
    if current_key is None:
        raise _backup.BackupError(
            f"--include-key: no key under {keys_root} matches the "
            f"store's fingerprint {facts.key_fingerprint[:16]}"
        )
    ring = KeyRing(bundle, facts.store_id)
    for fp, key in held.items():
        if key is None:
            why = unreadable.get(fp)
            warnings.append(
                (
                    f"the retired key {fp[:16]}: {why}"
                    if why is not None
                    else f"the retired key {fp[:16]} is not under {keys_root}"
                )
                + "; its segment of the log verifies only where it is held"
            )
            continue
        path = (
            ring.current_path()
            if fp == facts.key_fingerprint
            else ring.retired_path(fp)
        )
        _backup._write_private(path, key)
        written.append(path)


def _bundle_manifest(
    written: list[Path],
    created_at: str,
    *,
    store_file: Path,
    facts: SnapshotFacts,
    include_key: bool,
    live: KeyRing,
    current_key: bytes | None,
    keys_root: Path,
    warnings: list[str],
) -> dict[str, Any]:
    """Hash the written files into the manifest and sign it when the key is held, adding to ``warnings``; returns the manifest."""
    from . import __version__

    files: dict[str, dict[str, Any]] = {}
    for path in written:
        digest, size = _backup._file_digest(path)
        files[path.name] = {"sha256": digest, "size": size}
    manifest: dict[str, Any] = {
        "format": _backup.BUNDLE_FORMAT,
        "format_version": _backup.BUNDLE_FORMAT_VERSION,
        "bettermemory_version": __version__,
        "created_at": created_at,
        "source": str(store_file),
        "store_id": facts.store_id,
        "schema_version": facts.schema_version,
        "key_fingerprint": facts.key_fingerprint,
        "key_fingerprints": list(facts.key_fingerprints),
        "key_included": include_key,
        "head": {"seq": facts.head.seq, "mac": facts.head.mac},
        "counts": facts.counts,
        "files": files,
    }
    # The origin anchor, as the head is recorded: what a restore puts
    # back so the restored store is checked against where this host's
    # history of it begins. An anchor past the snapshot's last row was
    # written after the snapshot began (the takeover that wrote it
    # committed meanwhile) and names a row the bundle does not hold.
    origin = live.read_origin()
    if origin is not None and origin.seq <= facts.head.seq:
        manifest["origin"] = origin.to_dict()
    elif origin is not None:
        warnings.append(
            f"the origin anchor at seq {origin.seq} is past the snapshot's "
            f"last row (seq {facts.head.seq}); the bundle records no anchor, "
            "and a restore of it leaves the next writing open to anchor one"
        )
    if current_key is not None:
        manifest["mac"] = _backup.manifest_mac(current_key, manifest)
    else:
        warnings.append(
            f"no key under {keys_root} matches the store's fingerprint "
            f"{facts.key_fingerprint[:16]}: the manifest is not signed, and "
            "`backup verify` reads the bundle as unverifiable"
        )
    return manifest
