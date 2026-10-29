"""Export and import of `membership.py` (design 8.7, E27).

`portable_identity` and `export_document` describe this store's projects
as another store can recognise them; `import_identity` maps an imported
identity to a local project, the one place an import decides membership,
opening an ``import`` question (L10) where the evidence falls short, and
`import_home` homes an imported record by that mapping.

Each function takes the model as `self: Membership` and reaches its state
(`_store`, `_projects`, `machine`) and core methods through it; the class
binds every one by name. This module lives apart from `membership.py` to
keep each module under the package's 2,000-line bound (ENG-96). It
imports the standard library and the records and lines modules, and
`membership` only under TYPE_CHECKING.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Collection, Sequence
from typing import TYPE_CHECKING, Any

from .membership_lines import line_import
from .membership_records import (
    EXPORT_WITNESSES,
    GLOBAL,
    IMPORT,
    IMPORT_CONTRADICTION,
    IMPORT_ONE_WITNESS,
    IMPORT_SEVERAL,
    UNRESOLVED_STATES,
    Checkout,
    F,
    H,
    ImportedProject,
    PortableIdentity,
    R,
)

if TYPE_CHECKING:
    from .membership import Membership


# -- export and import (design 8.7, E27) --------------------------------------------------


def portable_identity(
    self: Membership, project_id: str, *, parent: int | None = None
) -> PortableIdentity:
    """The project's portable identity (design 8.7): its bound file ids,
    the oldest and newest halves of its witness commits by arrival (at
    most `EXPORT_WITNESSES`), its remote keys other than ``local:``
    ones, with the projects merged into it; `parent` is the position of
    its parent in the document it goes into."""
    pid = self._canon(project_id)
    p = self.project(pid)
    members = self._members(pid)
    files = sorted(
        {
            str(r["file_id"])
            for r in self._store.membership_rows(
                "project_files", column_in=("project", members)
            )
        }
    )
    order = list(dict.fromkeys(str(r["commit_id"]) for r in self._witness_rows(pid)))
    half = EXPORT_WITNESSES // 2
    kept = order[:half] + [c for c in order[-half:] if c not in order[:half]]
    remotes = sorted(k for k in self._remotes_of(pid) if not k.startswith("local:"))
    return PortableIdentity(
        name=p.name,
        file_ids=tuple(files),
        witnesses=tuple(kept),
        remotes=tuple(remotes),
        folder=p.folder,
        boundary=p.boundary,
        parent=parent,
    )


def export_document(
    self: Membership, record_ids: Collection[str] | None = None
) -> dict[str, Any]:
    """What an export carries (design 8.7): this store's id
    (``store_id``), the portable identity of every project, or with
    `record_ids` of those records' projects and the parents on their
    chains (``projects``, parents first), and each record's home keyed
    by record id (``homes``: its kind and its project's position in
    ``projects``, null for global). A home naming a project this store
    has no row for is left out."""
    self._projects = None
    projects = self._all_projects()
    if record_ids is None:
        rows = self._store.membership_rows("homes")
        wanted = [p.id for p in self.projects()]
    else:
        rows = self._store.membership_rows(
            "homes", column_in=("record_id", sorted(set(record_ids)))
        )
        wanted = sorted(
            {
                self._canon(str(r["project"]))
                for r in rows
                if str(r["project"]) != GLOBAL and str(r["project"]) in projects
            }
        )
    position: dict[str, int] = {}
    identities: list[PortableIdentity] = []

    def place(pid: str, seen: frozenset[str]) -> int:
        if pid in position:
            return position[pid]
        parent = self.canonical(projects[pid].parent)
        at: int | None = None
        if parent is not None and parent in projects and parent not in seen:
            at = place(parent, seen | {pid})
        position[pid] = len(identities)
        identities.append(self.portable_identity(pid, parent=at))
        return position[pid]

    for pid in wanted:
        place(self._canon(pid), frozenset())
    homes: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: str(r["record_id"])):
        recorded = str(r["project"])
        if recorded == GLOBAL:
            homes[str(r["record_id"])] = {"kind": str(r["kind"]), "project": None}
        elif recorded in projects:
            homes[str(r["record_id"])] = {
                "kind": str(r["kind"]),
                "project": place(self._canon(recorded), frozenset()),
            }
    return {
        "store_id": self._store.membership_store_id,
        "projects": [i.to_json() for i in identities],
        "homes": homes,
    }


def _imported_before(self: Membership, marker: str) -> str | None:
    """The project an earlier import minted under `marker` (its
    ``project_minted`` reason), merges followed."""
    found: str | None = None
    for row in self._store.membership_log(["project_minted"]):
        try:
            payload = json.loads(row.payload)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("reason") == marker:
            found = str(payload.get("project"))
    if found is None or found not in self._all_projects():
        return None
    return self._canon(found)


def _evidence_checkout(self: Membership, project_id: str) -> Checkout | None:
    """The checkout an ``import`` question about the project is asked
    in: one of its checkouts, this machine's live ones first, then this
    machine's, then the others, retired ones last."""
    here = self.machine
    ranked = sorted(
        self._checkouts_of(project_id),
        key=lambda k: (
            k.retired,
            k.machine != here,
            k.machine != here or self.live_view(k) is None,
            k.id,
        ),
    )
    return ranked[0] if ranked else None


def import_identity(
    self: Membership,
    identity: PortableIdentity,
    *,
    source: str,
    source_store: str | None = None,
    parent: str | None = None,
) -> ImportedProject:
    """Map an imported project to this store (design 8.7, E27, T16), the
    one place an import decides membership. A file id bound here to
    one project maps there (F), unless the identity's commits are
    another project's and none of that one's; else history and a
    remote together map to the one project showing both (H+R). Both
    are silent. One witness, several projects or that contradiction
    mints a project of the identity's own, kept apart and provisional,
    with an ``import`` question (line L10) in a checkout of each
    candidate project. No witness mints a project with no live
    checkout that a later checkout joins by the same evidence. A
    candidate across a boundary is dropped. `parent` is the local
    project the identity's parent was mapped to; `source` names the
    import in the line, `source_store` the store it came from. An
    identity an earlier import from the same store minted a project
    for maps to that project again."""
    with self._call():
        return self._import_identity(identity, source, source_store, parent)


def _import_identity(
    self: Membership,
    identity: PortableIdentity,
    source: str,
    source_store: str | None,
    parent: str | None,
) -> ImportedProject:
    marker = f"import:{source_store or 'unknown'}:{identity.digest()}"
    earlier = self._imported_before(marker)
    if earlier is not None:
        asked = tuple(
            pr.id
            for pr in self._open_proposals()
            if pr.kind == IMPORT
            and self.canonical(str(pr.evidence.get("imported_project"))) == earlier
        )
        return ImportedProject(earlier, "import:earlier", {}, asked)
    parent_id = self.canonical(parent) if parent is not None else None
    tenant = self.project(parent_id).tenant if parent_id is not None else None
    cands: dict[str, set[str]] = defaultdict(set)
    for fid in identity.file_ids:
        bound = self._file_binding(fid)
        if bound is not None:
            cands[bound].add(F)
    if identity.witnesses:
        for row in self._store.membership_rows(
            "project_witnesses", column_in=("commit_id", identity.witnesses)
        ):
            cands[self._canon(str(row["project"]))].add(H)
    if identity.remotes:
        for row in self._store.membership_rows(
            "project_remotes", column_in=("remote_key", identity.remotes)
        ):
            cands[self._canon(str(row["project"]))].add(R)
    for pid in list(cands):
        known = self.project(pid)
        if known.boundary:
            outside = (
                self.project(self._canon(known.parent)).tenant if known.parent else None
            )
        else:
            outside = known.tenant
        if known.provisional in UNRESOLVED_STATES or outside != tenant:
            # Writes made where nothing could be read witness nothing,
            # and nothing joins across a boundary (D17).
            del cands[pid]
    codes = {q: tuple(sorted(c)) for q, c in sorted(cands.items())}
    filed = sorted(q for q, c in cands.items() if F in c)
    strong = sorted(q for q, c in cands.items() if {H, R} <= c)
    contradiction = (
        len(filed) == 1
        and bool(identity.witnesses)
        and H not in cands[filed[0]]
        and any(H in c for q, c in cands.items() if q != filed[0])
    )
    target: str | None = None
    if len(filed) == 1 and not contradiction:
        target = filed[0]
    elif not filed and len(strong) == 1:
        target = strong[0]
    if target is not None:
        return ImportedProject(target, "import:" + "+".join(codes[target]), codes)
    if contradiction:
        reason = IMPORT_CONTRADICTION
    elif len(filed) > 1 or len(strong) > 1:
        reason = IMPORT_SEVERAL
    else:
        reason = IMPORT_ONE_WITNESS
    holders = [
        (q, k) for q in sorted(cands) if (k := self._evidence_checkout(q)) is not None
    ]
    p = self._mint_project(
        identity.name,
        reason=marker,
        provisional=IMPORT if holders else None,
        parent=parent_id,
        tenant=tenant,
        folder=identity.folder,
        boundary=identity.boundary,
    )
    for commit in identity.witnesses:
        self._add_witness(p.id, commit, source=f"import:{source}", history=None)
    self._add_remotes(p.id, identity.remotes)
    for fid in identity.file_ids:
        if self._file_binding(fid) is None:
            self._bind_file(fid, p.id, evidence=f"import from {source}")
    opened: list[str] = []
    for q, k in holders:
        pr = self._open_proposal(
            checkout=k.id,
            kind=IMPORT,
            candidates=[q],
            evidence={
                "imported_project": p.id,
                "source": source,
                "source_store": source_store,
                "codes": {q: list(codes[q])},
            },
            reason=reason,
            line=self._import_line(identity.name, source, q, codes[q]),
            signature=None,
            distinct="imported_project",
        )
        opened.append(pr.id)
    rule = f"import:ask:{reason}" if holders else "import:new:no_live_checkout"
    return ImportedProject(p.id, rule, codes, tuple(opened))


def _import_line(
    self: Membership, name: str, source: str, candidate: str, codes: Sequence[str]
) -> Callable[[str], str]:
    """An import question's line (L10), built from the question's id."""

    def line(q: str) -> str:
        return line_import(
            name,
            source,
            [(self.project(candidate).name, candidate, codes)],
            question=q,
        )

    return line


def import_home(
    self: Membership,
    record_id: str,
    kind: str,
    imported: ImportedProject | None,
    *,
    source: str,
    source_store: str | None = None,
) -> bool:
    """Home a record present in this store and homed nowhere by its
    imported identity's mapping (`import_identity`; None for a global
    record): its home row, with no checkout, and an ``import_homed``
    row naming the rule and the source. A record that already has a
    home keeps it (a home is written once, design 1); returns whether
    one was written."""
    if kind not in ("memory", "episode"):
        raise ValueError(f"a home is a memory's or an episode's, not {kind!r}")
    with self._call():
        if self._store.membership_rows("homes", where={"record_id": record_id}):
            return False
        if imported is None:
            project, rule = GLOBAL, "import:global"
        else:
            project, rule = self._canon(imported.project), imported.rule
        self._put(
            "home",
            {
                "record_id": record_id,
                "kind": kind,
                "project": project,
                "checkout": None,
                "rule": rule,
                "decided": self._stamp(),
            },
        )
        self._event(
            "import_homed",
            record=record_id,
            kind=kind,
            project=project,
            rule=rule,
            source=source,
            source_store=source_store,
        )
    return True
