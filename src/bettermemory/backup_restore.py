"""The plan ``backup.restore_bundle`` reads before it changes anything.

Once the bundle verifies, ``restore_bundle`` reads what the restore would
change through ``_plan_restore``: the key actions, where the store is moved
aside, the head and origin anchor before and after, the binding and the
record's path. It sets the report's ``plan`` and returns a ``_RestorePlan``
that ``backup._apply_restore`` carries out. The order and why are in
``backup``'s module docstring. ``backup`` imports this when a restore runs,
since this module reads its names.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import restore_lock
from .backup import (
    _SIBLING_SUFFIXES,
    _aside_base,
    _key_actions,
    _origin_dict,
    _path_str,
    _replaced_log_end,
    _self_signed_fingerprint,
    _sibling,
    _store_facts,
    restore_record_path,
)
from .log import Head, HistoryOrigin, KeyRing, KeyUnreadableError, parse_origin
from .time_utils import isoformat_utc


@dataclass(frozen=True)
class _RestorePlan:
    """What a restore changes, read before it changes anything."""

    store_id: str
    live: KeyRing
    bundle_ring: KeyRing
    head: Head
    actions: list[dict[str, Any]]
    aside: Path
    origin: HistoryOrigin | None
    bound: Path | None
    binding_after: str
    record: Path
    trusted_key: str | None


def _plan_restore(
    verification: dict[str, Any],
    self_signed: bool,
    bundle: Path,
    store_file: Path,
    keys_root: Path,
    moment: datetime,
    stamp: str,
    report: dict[str, Any],
) -> _RestorePlan | None:
    """Read what the restore would change and set the report's plan; returns the plan, or None with the refusal in the report."""
    trusted_key = _self_signed_fingerprint(verification) if self_signed else None
    store_id = str(verification["store_id"])
    live = KeyRing(keys_root, store_id)
    bundle_ring = KeyRing(bundle, store_id)
    head = Head(
        seq=int(verification["head"]["seq"]),
        mac=str(verification["head"]["mac"]),
        ts=isoformat_utc(moment.astimezone(timezone.utc)),
    )
    needed = [str(k["fingerprint"]) for k in verification["keys"]]
    final = str(verification["log"]["key_fingerprint"])
    try:
        actions = _key_actions(live, bundle_ring, needed, final)
    except KeyUnreadableError as exc:
        # Read as missing, the store's current key would be written over
        # by the bundle's. Refused before anything changes.
        report["detail"] = f"{exc}; make it readable and run the restore again"
        return None
    aside = _aside_base(store_file, stamp)
    before = live.read_head()
    facts = _store_facts(store_file)
    current_store_id = facts[0]
    origin = parse_origin(verification["origin"])
    origin_before = live.read_origin()
    try:
        bound = live.read_binding()
    except OSError:
        bound = None
    binding_before = _path_str(bound)
    binding_after = str(store_file.resolve())
    log_end = _replaced_log_end(store_file, store_id, facts, bound)
    record = restore_record_path(keys_root, store_id)
    report["plan"] = {
        "current_store_id": current_store_id,
        "bundle_store_id": store_id,
        "move_aside": [
            {"from": str(_sibling(store_file, s)), "to": str(_sibling(aside, s))}
            for s in _SIBLING_SUFFIXES
            if _sibling(store_file, s).exists()
        ],
        "keys": actions,
        "head": {
            "before": None
            if before is None
            else {"seq": before.seq, "mac": before.mac},
            "after": {"seq": head.seq, "mac": head.mac},
        },
        "log_end_before": log_end,
        "origin": {
            "before": _origin_dict(origin_before),
            "after": _origin_dict(origin),
        },
        "binding": {"before": binding_before, "after": binding_after},
        "record": str(record),
        "trusted_bundle_key": trusted_key,
        "unfinished_restore": restore_lock.read_marker(store_file),
    }
    return _RestorePlan(
        store_id=store_id,
        live=live,
        bundle_ring=bundle_ring,
        head=head,
        actions=actions,
        aside=aside,
        origin=origin,
        bound=bound,
        binding_after=binding_after,
        record=record,
        trusted_key=trusted_key,
    )
