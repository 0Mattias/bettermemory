"""Row conversions of `membership.py`: its records to and from table rows.

`_checkout_from`, `_project_from`, `_proposal_from` and `_view_from` read
a membership table row into its record and `_checkout_row`,
`_project_row` and `_proposal_row` write one, with the object-key and
stamp codecs beside them. `_jsonable` makes a payload plain JSON,
`_signature` and `_unwritten` read a location for a proposal and a line,
and `_Tally` is what the ladder's tally collects.

`membership.py` imports back what the model reads. This module lives
apart from `membership.py` to keep each module under the package's
2,000-line bound (ENG-96); it imports the standard library,
`membership_records` and `history` (whose `printable` a stored line is
read through), and `membership.py` imports it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .history import printable
from .membership_records import (
    UNRESOLVED,
    Checkout,
    InitStamp,
    Location,
    Manifest,
    ObjectKey,
    Project,
    Proposal,
    View,
    _TOKEN_NAME,
    _path_under,
)


# ---------------------------------------------------------------------------
# Row conversions
# ---------------------------------------------------------------------------


def _key_columns(prefix: str, key: ObjectKey | None) -> dict[str, Any]:
    return {
        f"{prefix}_device": None if key is None else key.device,
        f"{prefix}_inode": None if key is None else key.inode,
        f"{prefix}_birth": None if key is None else key.birth,
    }


def _key_from(row: Mapping[str, Any], prefix: str) -> ObjectKey | None:
    device, inode = row[f"{prefix}_device"], row[f"{prefix}_inode"]
    if device is None or inode is None:
        return None
    birth = row[f"{prefix}_birth"]
    return ObjectKey(str(device), str(inode), None if birth is None else str(birth))


def _stamps_from(raw: Any) -> tuple[InitStamp, ...]:
    out: list[InitStamp] = []
    for item in raw or ():
        if isinstance(item, (list, tuple)) and len(item) == 5:
            out.append(
                InitStamp(
                    str(item[0]), int(item[1]), int(item[2]), int(item[3]), int(item[4])
                )
            )
    return tuple(out)


def _view_from(row: Mapping[str, Any]) -> View:
    return View(
        checkout=str(row["checkout"]),
        machine=str(row["machine"]),
        path=str(row["path"]),
        first_seen=str(row["first_seen"]),
        last_seen=str(row["last_seen"]),
        order=int(row["seen_order"]),
    )


def _checkout_from(row: Mapping[str, Any], views: Sequence[View]) -> Checkout:
    raw = row.get("unresolved_json")
    unresolved: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return Checkout(
        id=str(row["id"]),
        project=str(row["project"]),
        machine=str(row["machine"]),
        kind=str(row["kind"]),
        object_key=_key_from(row, "object"),
        common_object_key=_key_from(row, "common"),
        git_dir=row["git_dir"],
        token=row["token"],
        token_status=str(row["token_status"]),
        stamps=_stamps_from(row["stamps_json"]),
        last_head=row["last_head"],
        branch=row["branch"],
        remotes=tuple(str(r) for r in row["remotes_json"] or ()),
        manifest=Manifest.from_json(row["manifest_json"]),
        legacy=bool(row["legacy"]),
        retired=bool(row["retired"]),
        registered=str(row["registered"]),
        views=tuple(sorted(views, key=lambda v: v.order)),
        sessions=tuple(str(x) for x in unresolved.get("sessions") or ()),
        replaced_by=unresolved.get("replaced_by"),
    )


def _checkout_row(k: Checkout) -> dict[str, Any]:
    return {
        "id": k.id,
        "project": k.project,
        "machine": k.machine,
        "kind": k.kind,
        **_key_columns("object", k.object_key),
        **_key_columns("common", k.common_object_key),
        "git_dir": k.git_dir,
        "token": k.token,
        "token_status": k.token_status,
        "stamps_json": [list(s) for s in k.stamps],
        "last_head": k.last_head,
        "branch": k.branch,
        "remotes_json": list(k.remotes),
        "manifest_json": None if k.manifest is None else k.manifest.to_json(),
        "legacy": 1 if k.legacy else 0,
        "retired": 1 if k.retired else 0,
        "registered": k.registered,
        "unresolved_json": (
            {"sessions": list(k.sessions), "replaced_by": k.replaced_by}
            if k.kind == UNRESOLVED
            else None
        ),
    }


def _project_from(row: Mapping[str, Any]) -> Project:
    return Project(
        id=str(row["id"]),
        name=str(row["name"]),
        merged_into=row["merged_into"],
        provisional=row["provisional"],
        parent=row["parent"],
        tenant=row["tenant"],
        folder=bool(row["folder"]),
        boundary=bool(row["boundary"]),
        minted=str(row["minted"]),
    )


def _project_row(p: Project) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "merged_into": p.merged_into,
        "provisional": p.provisional,
        "parent": p.parent,
        "tenant": p.tenant,
        "folder": 1 if p.folder else 0,
        "boundary": 1 if p.boundary else 0,
        "minted": p.minted,
    }


def _proposal_from(row: Mapping[str, Any]) -> Proposal:
    signature = row["signature_json"]
    return Proposal(
        id=str(row["id"]),
        checkout=row["checkout"],
        kind=str(row["kind"]),
        candidates=tuple(str(c) for c in row["candidates_json"] or ()),
        evidence=dict(row["evidence_json"] or {}),
        reason=str(row["reason"]),
        # A line stored before its values were escaped (ENG-154) is read
        # through the same function, so no stored line forges another.
        line=printable(str(row["line"])),
        state=str(row["state"]),
        answer=row["answer"],
        answered_by=row["answered_by"],
        signature=None if signature is None else tuple(signature),
        opened=str(row["opened"]),
    )


def _proposal_row(pr: Proposal) -> dict[str, Any]:
    return {
        "id": pr.id,
        "checkout": pr.checkout,
        "kind": pr.kind,
        "candidates_json": list(pr.candidates),
        "evidence_json": _jsonable(pr.evidence),
        "reason": pr.reason,
        "line": pr.line,
        "state": pr.state,
        "answer": pr.answer,
        "answered_by": pr.answered_by,
        "signature_json": None if pr.signature is None else list(pr.signature),
        "opened": pr.opened,
    }


def _jsonable(value: Any) -> Any:
    """`value` as plain JSON values: tuples and sets become sorted or
    ordered lists, mappings dicts."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _signature(loc: Location) -> tuple[Any, ...]:
    """A checkout's evidence signature (design 3.2): HEAD, remotes, root
    file, token, shallow. A first-sight proposal re-tallies when it
    changes. The token is the one the checkout carries after this
    sighting, so a token the capture wrote at first sight is no change."""
    rf = loc.root_file
    return (
        loc.head,
        sorted(loc.remote_keys),
        None if rf is None else rf.id,
        loc.token or loc.token_written,
        loc.shallow,
    )


def _unwritten(loc: Location) -> str | None:
    """The token file and status a line names when the capture could not
    write the token (read-only, another owner, the policy switch)."""
    if not loc.token_status.startswith(("failed", "skipped")):
        return None
    where = _path_under(loc.git_dir or "", "info", _TOKEN_NAME)
    return f"{where}: {loc.token_status}"


@dataclass
class _Tally:
    cands: dict[str, set[str]] = field(default_factory=dict)
    contradictions: list[tuple[str, str, str]] = field(default_factory=list)
    rekey: dict[str, str] = field(default_factory=dict)
    superseded: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    held: dict[str, set[str]] = field(default_factory=dict)
