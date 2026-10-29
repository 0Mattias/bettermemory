"""The checks ``backup.verify_bundle`` runs on a bundle, as named steps.

``verify_bundle`` reads the manifest, checks every file it lists
(``_check_bundle_files``), copies the snapshot into a scratch directory and
compares the manifest's claims with it (``_check_snapshot_claims``), files
the keys the snapshot's log names in a scratch key ring
(``_scratch_keys``), flags a foreign segment and reads the manifest's
signature (``_check_keys_and_signature``), then runs the store's own
``log_verify`` on the copy. Each step adds what it finds to the report's
``problems``. The verdicts are documented on ``verify_bundle``. ``backup``
imports these only when a bundle is verified, and they read ``backup``'s
names through the module when they run, so a patch on ``backup`` reaches
them.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import backup as _backup
from .log import Head, KeyRing, same_mac

if TYPE_CHECKING:
    from .backup import SnapshotFacts


def _check_bundle_files(
    bundle: Path,
    manifest: dict[str, Any],
    report: dict[str, Any],
    problems: list[dict[str, Any]],
) -> bool:
    """Check every file the manifest lists against its size and hash, adding to ``report["files"]`` and ``problems``; returns whether the snapshot could not be read."""
    snapshot_unreadable = False
    for name, entry in sorted(manifest["files"].items()):
        path = bundle / name
        result: dict[str, Any] = {
            "name": name,
            "status": _backup.OK,
            "sha256": entry["sha256"],
            "size": entry["size"],
        }
        if not path.is_file():
            result["status"] = "missing"
            problems.append({"problem": "file_missing", "file": name})
        else:
            try:
                digest, size = _backup._file_digest(path)
            except OSError as exc:
                # A file this account cannot read (its mode, a lock, a
                # read error) proves nothing either way about what the
                # manifest says of it.
                result["status"] = "unreadable"
                problems.append(
                    {"problem": "file_unreadable", "file": name, "detail": str(exc)}
                )
                report["files"].append(result)
                snapshot_unreadable = (
                    snapshot_unreadable or name == _backup.SNAPSHOT_NAME
                )
                continue
            if size != entry["size"]:
                result["status"] = "size_mismatch"
                problems.append({"problem": "size_mismatch", "file": name})
            elif not same_mac(digest, str(entry["sha256"])):
                result["status"] = "sha256_mismatch"
                problems.append({"problem": "sha256_mismatch", "file": name})
        report["files"].append(result)
    return snapshot_unreadable


def _check_snapshot_claims(
    manifest: dict[str, Any],
    facts: SnapshotFacts,
    head: Head,
    problems: list[dict[str, Any]],
) -> None:
    """Compare the manifest's store id, key and head with the snapshot's, adding to ``problems``."""
    for claim, found in (
        ("store_id", facts.store_id),
        ("key_fingerprint", facts.key_fingerprint),
    ):
        if manifest[claim] != found:
            problems.append(
                {"problem": "claim_mismatch", "field": claim, "found": found}
            )
    if (facts.head.seq, facts.head.mac) != (head.seq, head.mac):
        problems.append(
            {
                "problem": "claim_mismatch",
                "field": "head",
                "found": {"seq": facts.head.seq, "mac": facts.head.mac},
            }
        )


def _scratch_keys(
    manifest: dict[str, Any],
    facts: SnapshotFacts,
    live_ring: KeyRing,
    bundle_ring: KeyRing,
    scratch_ring: KeyRing,
    report: dict[str, Any],
    problems: list[dict[str, Any]],
) -> dict[str, tuple[bytes, str]]:
    """Find each key the snapshot names and file it in the scratch ring, adding to ``report["keys"]`` and ``problems``; returns the keys found, by fingerprint, with where."""
    # The keys directory first: a key it holds is one this host already
    # trusts, and the report says so. A key found only in the bundle
    # comes with the bundle and is worth what the bundle is.
    keys: dict[str, tuple[bytes, str]] = {}
    wanted = dict.fromkeys((*facts.key_fingerprints, str(manifest["key_fingerprint"])))
    for fp in wanted:
        source: str | None = None
        key = live_ring.key_for_fingerprint(fp)
        if key is not None:
            source = "keys_dir"
        else:
            key = bundle_ring.key_for_fingerprint(fp)
            source = "bundle" if key is not None else None
        report["keys"].append({"fingerprint": fp, "source": source})
        if key is None or source is None:
            problems.append({"problem": "key_absent", "fingerprint": fp})
            continue
        keys[fp] = (key, source)
        _backup._write_private(scratch_ring.retired_path(fp), key)
        if fp == facts.key_fingerprint:
            _backup._write_private(scratch_ring.current_path(), key)
    return keys


def _check_keys_and_signature(
    manifest: dict[str, Any],
    facts: SnapshotFacts,
    keys: dict[str, tuple[bytes, str]],
    report: dict[str, Any],
    problems: list[dict[str, Any]],
) -> None:
    """Flag a foreign segment and read the manifest's signature, setting ``report["manifest_mac"]`` and adding to ``problems``."""
    # The snapshot's log is checked under every key found, so a
    # segment under a key only the bundle carries verifies whatever
    # comes before it. The live store reads a segment whose key this
    # host does not hold, after one its key signs, as rows written
    # into its history without its key (`foreign_segment`), and so
    # does this check: otherwise a writer of the bundle could rekey
    # the owner's snapshot to a key of their own, add rows, and have
    # the log read `ok` and the bundle wait only on --trust-bundle-key.
    # An --include-key bundle on a machine that holds none of its keys
    # has no held segment and is not affected.
    held_seen = False
    for fp in facts.key_fingerprints:
        found_key = keys.get(fp)
        if found_key is None:
            continue
        if found_key[1] == "keys_dir":
            held_seen = True
        elif held_seen:
            problems.append({"problem": "foreign_segment", "fingerprint": fp})
    signing = keys.get(str(manifest["key_fingerprint"]))
    if "mac" not in manifest:
        report["manifest_mac"] = "absent"
        problems.append({"problem": "manifest_unsigned"})
    elif signing is None:
        report["manifest_mac"] = _backup.UNVERIFIABLE
    elif not same_mac(_backup.manifest_mac(signing[0], manifest), str(manifest["mac"])):
        report["manifest_mac"] = "mismatch"
        problems.append({"problem": "manifest_mac_mismatch"})
    elif signing[1] == "bundle":
        # The MAC holds, under a key the bundle itself supplied: anyone
        # who can write the bundle can remake it under a key of their
        # own and sign it so. It proves the bundle is whole, not that
        # this store's key holder wrote it.
        report["manifest_mac"] = _backup.SELF_SIGNED
        problems.append(
            {
                "problem": "manifest_self_signed",
                "fingerprint": str(manifest["key_fingerprint"]),
            }
        )
    else:
        report["manifest_mac"] = _backup.OK
