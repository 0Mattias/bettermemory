"""The vocabulary and records of `membership.py` (RS-F6, design 2 and 3).

The outcomes, witnesses, bounds, proposal kinds, relations, surfaces and
path states; the frozen records, from a capture's `Location` to the
`Resolution`, `Home` and `Declaration` the model reads and returns;
`relation` and `admitted`, the one membership function and the surface
table of design 3.5; and the helpers that compare and rebase recorded
paths in either spelling.

`membership.py` re-exports every name tests, bench and other modules read
from here, so they keep importing from it. This module lives apart from
`membership.py` to keep each module under the package's 2,000-line bound
(ENG-96). It is the family's bottom module: it imports the standard
library and `store` (for `RecordHome`) and no other membership module.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import posixpath
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from .store import RecordHome

if TYPE_CHECKING:
    from .log import LogRow


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The project of a record written outside any repository and any project
#: file, and the caller's project there.
GLOBAL = "global"

# Outcomes (design 1, 3.2).
SILENT = "silent"
ASK = "ask"
NEW = "new"
GLOBAL_OUTCOME = "global"
INDETERMINATE = "indeterminate"
DEFERRED = "deferred"

# What a capture found at the cwd (`Location.state`).
REPO = "repo"
OUTSIDE = "outside"
UNREADABLE = "indeterminate"

# Checkout kinds.
PRIMARY = "primary"
LINKED = "linked"
BARE = "bare"
FOLDER = "folder"
#: A path written at while nothing could be read there (decision B6).
UNRESOLVED = "unresolved"

#: The provisional project of a write made where nothing could be read
#: (decision B6): its mint reason, and the provisional reasons it carries.
UNRESOLVED_LOCATION = "unresolved_location"
UNRESOLVED_STATES = frozenset({INDETERMINATE, DEFERRED})

#: A checkout's token status while the token at its git directory is one
#: its history supersedes (or a declaration replaced) and no token of its
#: own is written yet (decision B15): its next sighting asks again
#: (`Resolution.remint_token`) until a new token is seen or written.
SUPERSEDED = "superseded"

# The witnesses (design 2.2).
O = "O"  # noqa: E741
S = "S"
H = "H"
HF = "Hf"
HB = "Hb"
R = "R"
D = "D"
F = "F"
T = "T"
B = "B"
C = "C"
_HISTORY = frozenset({H, HF, HB})

#: How a line names each witness.
WORDS: Mapping[str, str] = {
    O: "is its git directory",
    S: "shares its object store",
    H: "holds its commits",
    HF: "holds its commits rewritten (same authors, dates and messages)",
    HB: "holds the file contents of its commits",
    R: "names its remote",
    D: "names a remote you declared for it",
    F: "carries its project file",
    T: "carries its checkout token",
    B: "sits where its checkout was, with its commits",
    C: "has its folder name and the same files and sizes",
}

# The bounds of decision D15 and design 2.2.
WITNESS_BOUND = 64
#: A full object id (SHA-1 or SHA-256): what a witness row carries (B19).
#: The migration and an import read a recorded commit through it; an
#: abbreviation names whichever object a repository holds with that
#: prefix, so it witnesses nothing (ENG-157).
_FULL_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
KEEP_OLDEST = 16
BLOBS_PER_WITNESS = 8
#: Commits read from HEAD for the rewritten-history witness (Hf).
WALK_LIMIT = 512
#: The content witness: relative paths and sizes at this Jaccard or more.
CONTENT_MATCH = 0.9
#: A content question names at most this many candidates; with more, NEW.
CONTENT_MAX = 3
#: A checkout unseen this long leaves health (D15); no rule reads the flag.
RETIRE_AFTER = timedelta(days=180)
#: How stale a view's last sighting may get before a sighting writes it
#: again: only health's retirement reads it, so a sighting at a known
#: path writes nothing more often.
VIEW_REFRESH = timedelta(days=1)

# Proposal kinds and states (design 2.1, 3.4).
FIRST_SIGHT = "first_sight"
FILE_CONTRADICTION = "file_contradiction"
REMOTE_DIVERGED = "remote_diverged"
SUBPROJECT_CONTRADICTION = "subproject_contradiction"
LEGACY = "legacy"
IMPORT = "import"
OPEN = "open"
ANSWERED = "answered"

#: The reason of the migration's ``legacy`` question (L6), and the
#: principal that closes one whose evidence decides nothing.
LEGACY_NO_LIVE_CHECKOUT = "legacy_no_live_checkout"
LEGACY_SEPARATE = "rule:legacy_separate"


def legacy_asks(*, rule: str, same_name: bool, compatible: bool) -> bool:
    """Whether a migration's legacy question (L6) decides anything: the
    records' commits are held by more than one compatible live project
    (``rule`` ambiguous), or by citation alone by a compatible project of
    the same name. Held by citation alone under another name, or through
    a remote the holder does not carry, they decide nothing: the records
    stay a project of their own with no live checkout and no one is
    asked (the owner's ruling of 2026-10-21: nothing in the product waits
    on a human)."""
    return rule == "ambiguous" or (rule == "none" and same_name and compatible)


# Export and import (design 8.7, E27): the witness commits a portable
# identity carries (the oldest half and the newest half of the bounded
# set), the reasons an ``import`` question is opened for, and the format
# name of `bettermemory membership export`.
EXPORT_WITNESSES = 16
IMPORT_ONE_WITNESS = "import_one_witness"
IMPORT_CONTRADICTION = "import_contradiction"
IMPORT_SEVERAL = "import_several_projects"
EXPORT_FORMAT = "bettermemory membership export"
EXPORT_FORMAT_VERSION = 1

# The relations of design 3.5.
SAME_PROJECT = "same_project"
SUB_PROJECT = "sub_project"
GLOBAL_RELATION = "global"
#: A global record whose scopes name the caller's project (``projects:<name>``,
#: the name compared case-folded), unit RS-F7 part 2. In Stage A's grades
#: (2026-10-22, 100 prompts, top 10) such records were relevant 0.44 of the
#: time (0.62 in the top 3) against 0.31 for the caller's own project's
#: records, and global records naming only other projects or none 0.12.
GLOBAL_NAMED = "global_named"
OTHER_PROJECT = "other_project"
OTHER_TENANT = "other_tenant"
UNKNOWN = "unknown"
RELATIONS = (
    SAME_PROJECT,
    SUB_PROJECT,
    GLOBAL_RELATION,
    GLOBAL_NAMED,
    OTHER_PROJECT,
    OTHER_TENANT,
    UNKNOWN,
)

# The surfaces of design 3.5.
SESSION_BRIEF = "session_brief"
PROMPT_RECALL = "prompt_recall"
REVIEW_STRATA = "review_strata"
SEARCH_AUTO_SCOPE = "search_auto_scope"
POOL_PREFILTER = "pool_prefilter"
LINKS = "links"
SEARCH_UNSCOPED = "search_unscoped"
SURFACES = (
    SESSION_BRIEF,
    PROMPT_RECALL,
    REVIEW_STRATA,
    SEARCH_AUTO_SCOPE,
    POOL_PREFILTER,
    LINKS,
    SEARCH_UNSCOPED,
)

#: The scope prefix a record names a project with (`Membership.named`).
PROJECT_SCOPE = "projects:"

_AUTOMATIC = frozenset({SAME_PROJECT, GLOBAL_NAMED})
_SCOPED = frozenset({SAME_PROJECT, SUB_PROJECT, GLOBAL_RELATION, GLOBAL_NAMED})
#: What each surface admits for a caller in a project.
IN_PROJECT_POLICY: Mapping[str, frozenset[str]] = {
    SESSION_BRIEF: _AUTOMATIC,
    PROMPT_RECALL: _AUTOMATIC,
    REVIEW_STRATA: _AUTOMATIC,
    SEARCH_AUTO_SCOPE: _SCOPED,
    POOL_PREFILTER: _SCOPED,
    LINKS: _SCOPED,
    SEARCH_UNSCOPED: frozenset(
        {SAME_PROJECT, SUB_PROJECT, GLOBAL_RELATION, GLOBAL_NAMED, OTHER_PROJECT}
    ),
}
#: What each surface admits for a caller outside any project (D3).
OUTSIDE_POLICY: Mapping[str, frozenset[str]] = {
    SESSION_BRIEF: frozenset(),
    PROMPT_RECALL: frozenset(),
    REVIEW_STRATA: frozenset(),
    SEARCH_AUTO_SCOPE: frozenset({GLOBAL_RELATION}),
    POOL_PREFILTER: frozenset({GLOBAL_RELATION}),
    LINKS: frozenset({GLOBAL_RELATION}),
    SEARCH_UNSCOPED: frozenset({GLOBAL_RELATION, OTHER_PROJECT}),
}

# What a view's path holds now (`Liveness`).
LIVE = "live"
LIVE_LEGACY = "live_legacy"
GONE = "gone"
UNREACHABLE = "unreachable"
REPLACED = "replaced"
OTHER_MACHINE = "other_machine"
_LIVE_STATES = frozenset({LIVE, LIVE_LEGACY})

# How two object keys compare.
SAME = "same"
DIFFERENT = "different"
UNPROVEN = "unproven"

_TOKEN_NAME = "bettermemory"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectKey:
    """A filesystem object: device, inode (the volume serial and file id
    on Windows) and birth time in ns where the platform reports it, as
    text so any width fits."""

    device: str
    inode: str
    birth: str | None = None

    def compare(self, other: ObjectKey | None) -> str:
        """``same``, ``different`` or ``unproven`` (same device and inode,
        no birth time on one side: an inode number can be reused)."""
        if other is None or (self.device, self.inode) != (other.device, other.inode):
            return DIFFERENT
        if self.birth is not None and other.birth is not None:
            return SAME if self.birth == other.birth else DIFFERENT
        return UNPROVEN


class InitStamp(NamedTuple):
    """A file ``git init`` writes and no git command rewrites
    (``description``, ``info/exclude``), compared by equality only
    (amendment A2): no clock enters it."""

    name: str
    inode: int
    mtime_ns: int
    ctime_ns: int
    size: int


@dataclass(frozen=True)
class ProjectFileRef:
    """A ``bettermemory.toml`` the capture found at the cwd or above: its
    id, name, boundary switch, directory and path, and the directory's
    own object where it was read."""

    id: str
    name: str
    dir: str
    path: str
    boundary: bool = False
    object_key: ObjectKey | None = None


@dataclass(frozen=True)
class Manifest:
    """The content witness: the folder's name and the relative paths and
    sizes of its working tree (outside ``.git`` and the project file),
    bounded by the capture."""

    name: str
    entries: tuple[tuple[str, int], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "entries": [list(e) for e in self.entries]}

    @classmethod
    def from_json(cls, raw: Any) -> Manifest | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            return None
        entries = tuple(
            (str(path), int(size)) for path, size in raw.get("entries") or ()
        )
        return cls(name=raw["name"], entries=entries)


def manifest_match(a: Manifest | None, b: Manifest | None) -> float:
    """The Jaccard of two manifests' (path, size) sets; 0 for another
    folder name, and for two empty trees (an empty tree witnesses
    nothing)."""
    if a is None or b is None or a.name != b.name:
        return 0.0
    left, right = set(a.entries), set(b.entries)
    if not left and not right:
        return 0.0
    return len(left & right) / len(left | right)


class HistoryReader(Protocol):
    """The repository a `Location` was read from, as the ladder asks it:
    which of a batch of commits (or objects of any type) it holds, a
    commit's fingerprint (sha256 of its author and committer lines and
    message) and parents, the ancestry among a batch of commits (one
    bounded traversal: `location.reachable_among`, ENG-151), and the
    sample of blob ids a witness commit keeps. Phase 2's object reader
    implements it; a client that reads a path the daemon cannot see
    answers for it."""

    def holds(self, commits: Sequence[str]) -> set[str]: ...

    def holds_objects(self, ids: Sequence[str]) -> set[str]: ...

    def fingerprint(self, commit: str) -> str | None: ...

    def parents(self, commit: str) -> Sequence[str]: ...

    def reachable_among(self, commits: Sequence[str]) -> Mapping[str, Collection[str]]:
        """{commit: the other commits of `commits` it is reachable from},
        keyed by the full ids given, in lowercase."""
        ...

    def blobs(self, commit: str) -> Sequence[str]: ...


@dataclass(frozen=True)
class Location:
    """What a capture hands in (design 3.1). ``state`` is ``repo``,
    ``outside`` or ``indeterminate``; outside a repository with a project
    file at the cwd or above, ``kind`` is ``folder`` and ``root`` the
    nearest file's directory. ``token`` is the checkout token found,
    ``token_written`` one the capture wrote at this sighting and
    ``token_status`` what happened (``present``, ``written``,
    ``failed:<detail>``, ``skipped:<reason>``; a checkout's own status is
    also ``superseded``, decision B15). ``files`` are the project
    files from the cwd up, nearest first. ``evicted`` is set when HEAD or
    config is a file provider's placeholder the fetch did not bring in
    time; ``detail`` then says which. ``session`` is the harness session
    id the caller carries (None when unknown): a resolution at a path
    written at while it could not be read merges those writes when the
    same session resolves it (decision B6)."""

    machine: str
    cwd: str
    state: str
    root: str | None = None
    kind: str | None = None
    git_dir: str | None = None
    object_key: ObjectKey | None = None
    common_object_key: ObjectKey | None = None
    root_object_key: ObjectKey | None = None
    head: str | None = None
    branch: str | None = None
    ref_tips: tuple[str, ...] = ()
    remote_keys: frozenset[str] = frozenset()
    shallow: bool = False
    token: str | None = None
    token_written: str | None = None
    token_status: str = "n/a"
    stamps: tuple[InitStamp, ...] = ()
    files: tuple[ProjectFileRef, ...] = ()
    file_tracked: bool | None = None
    evicted: bool = False
    detail: str = ""
    manifest: Manifest | None = None
    session: str | None = None
    history: HistoryReader | None = field(default=None, compare=False, repr=False)

    @property
    def born(self) -> bool:
        """A commit is reachable from HEAD or another ref (design 2.1): an
        orphan branch over other refs' commits is born."""
        return self.head is not None or bool(self.ref_tips)

    @property
    def root_file(self) -> ProjectFileRef | None:
        """The project file at the repository's root, or for a folder
        checkout the nearest one: the checkout's own file."""
        if self.kind == FOLDER:
            return self.files[0] if self.files else None
        for f in self.files:
            if self.root is not None and _same_path(f.dir, self.root):
                return f
        return None

    @property
    def nested_files(self) -> tuple[ProjectFileRef, ...]:
        """Project files strictly inside the repository, between the cwd
        and the root, nearest first: sub-projects."""
        if self.root is None or self.kind == FOLDER:
            return ()
        return tuple(
            f
            for f in self.files
            if not _same_path(f.dir, self.root) and _inside(f.dir, self.root)
        )

    @property
    def enclosing_files(self) -> tuple[ProjectFileRef, ...]:
        """Project files above the repository's root (or above a folder
        checkout's own file): workspaces and tenant boundaries."""
        if self.kind == FOLDER:
            return self.files[1:]
        if self.root is None:
            return self.files
        return tuple(f for f in self.files if not _inside(f.dir, self.root))


@dataclass(frozen=True)
class Machine:
    """A machine as the store records it (design 2.1)."""

    id: str
    facts: Mapping[str, Any]
    predecessor: str | None
    minted: str


@dataclass(frozen=True)
class View:
    """A path at which a checkout was seen on a machine, with its first
    and last sighting and its order among sightings. A view says where,
    never what."""

    checkout: str
    machine: str
    path: str
    first_seen: str
    last_seen: str
    order: int


@dataclass(frozen=True)
class Checkout:
    """One git directory (or folder with a project file) on one machine,
    or (kind ``unresolved``, decision B6) a path written at while nothing
    could be read there: ``sessions`` are the sessions that wrote there,
    ``replaced_by`` the checkout whose resolution at that path settled it."""

    id: str
    project: str
    machine: str
    kind: str
    object_key: ObjectKey | None
    common_object_key: ObjectKey | None
    git_dir: str | None
    token: str | None
    token_status: str
    stamps: tuple[InitStamp, ...]
    last_head: str | None
    branch: str | None
    remotes: tuple[str, ...]
    manifest: Manifest | None
    legacy: bool
    retired: bool
    registered: str
    views: tuple[View, ...] = ()
    sessions: tuple[str, ...] = ()
    replaced_by: str | None = None

    def views_on(self, machine: str) -> list[View]:
        return sorted(
            (v for v in self.views if v.machine == machine), key=lambda v: v.order
        )

    @property
    def newest_view(self) -> View | None:
        return max(self.views, key=lambda v: v.order) if self.views else None


def _live_key(k: Checkout, v: View) -> tuple[object, ...]:
    """What a view's liveness depends on (`Liveness`, ENG-156): the path
    and machine, and the checkout's recorded objects, token, last HEAD and
    init stamps; a value read for one key answers for that key only."""
    return (
        k.id,
        k.kind,
        k.object_key,
        k.common_object_key,
        k.token,
        k.last_head,
        k.stamps,
        v.machine,
        v.path,
    )


@dataclass(frozen=True)
class Project:
    """A set of checkouts and its evidence (design 2.1). ``merged_into``
    names the project it was merged into; ``provisional`` the reason
    while a first-sight proposal is open."""

    id: str
    name: str
    merged_into: str | None
    provisional: str | None
    parent: str | None
    tenant: str | None
    folder: bool
    boundary: bool
    minted: str


@dataclass(frozen=True)
class ProjectFile:
    """A project file id bound to a project, with the evidence."""

    file_id: str
    project: str
    evidence: str


@dataclass(frozen=True)
class Home:
    """A record's home (design 2.1) as `relation` reads it: the project
    it belongs to now (merges followed), that project's chain (itself and
    its parents) and tenant, the checkout it was written in, the rule
    that decided it and when. ``recorded_project`` is the project the
    row names. ``unresolved`` is set while that project is the
    provisional project of writes made where nothing could be read
    (decision B6). ``named`` is, for a global record, the projects (merges
    followed) its ``projects:<name>`` scopes name (`Membership.named`);
    empty for every other record."""

    record_id: str
    kind: str
    project: str
    checkout: str | None
    rule: str
    decided: str
    recorded_project: str
    chain: tuple[str, ...] = ()
    tenant: str | None = None
    unresolved: bool = False
    named: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Proposal:
    """A durable question on one checkout (design 3.4)."""

    id: str
    checkout: str | None
    kind: str
    candidates: tuple[str, ...]
    evidence: Mapping[str, Any]
    reason: str
    line: str
    state: str
    answer: str | None
    answered_by: str | None
    signature: tuple[Any, ...] | None
    opened: str


@dataclass(frozen=True)
class Declaration:
    """A statement that outranks evidence (design 8.8): ``join``
    (``project join <target>``, or the agent's ``answer_question`` with
    ``join``), ``new`` (``project new``, or ``answer_question`` with
    ``separate``), ``init`` (``project init`` writing ``file_id``),
    ``init_new`` (``project init --new``), ``init_rebind`` (``project init
    --rebind``), ``move`` (``memory move <record> --project <target>``),
    ``absorb`` (the agent's ``join_project``: project ``target`` merged
    into the checkout's) and ``unjoin`` (the agent's ``unjoin_project``:
    the merge of project ``target`` undone). ``principal`` names who made
    it: the OS user of a command (``os:<user>``), the agent of a tool
    call (``agent:<session>``), or the rule that settled a question no
    one needed to answer (``rule:<name>``). ``said`` is the user's words
    a join rests on, logged with it. ``checkout`` is the checkout it is
    made in; ``proposal`` the question it answers, when one is open
    there. ``directory`` is the folder an
    ``init`` wrote its file in: the model decides from it whether the
    file lies at the checkout's root or below it (`Membership.below_root`,
    decision B17; None reads as the root). Below the root ``target``
    names the project enclosing that folder as the caller's resolution
    there found it (the nearest bound file above the folder, else the
    checkout's project; decision D13), and ``replaced`` the id of the file
    an ``init_rebind`` replaced, when it could be read. ``boundary`` is
    the flag of the file an ``init`` wrote: the project the file binds
    becomes a tenant (B9), and an ``init_rebind`` whose fresh file lacks it
    lifts the project's boundary (B12), returning it to ``outer_tenant``,
    the tenant around the folder."""

    action: str
    principal: str
    checkout: str | None = None
    proposal: str | None = None
    target: str | None = None
    file_id: str | None = None
    name: str | None = None
    directory: str | None = None
    record: str | None = None
    boundary: bool = False
    outer_tenant: str | None = None
    replaced: str | None = None
    said: str | None = None


@dataclass(frozen=True)
class Resolution:
    """The outcome of the ladder for one location: ``outcome`` is SILENT,
    ASK, NEW, GLOBAL_OUTCOME, INDETERMINATE or DEFERRED; ``project`` the
    caller's project (a sub-project where a nested file names one;
    ``global`` outside any project; None when nothing was readable);
    ``chain`` that project, its parents and the folder projects around
    it, nearest first; ``tenant`` the nearest boundary on it.
    ``witnesses`` are the codes each candidate showed, ``rows`` the
    sequence numbers of the log rows this resolution wrote, ``lines``
    the session's first output. ``remint_token`` asks the caller to give
    the checkout a token of its own (no token writer was given)."""

    outcome: str
    project: str | None
    checkout: str | None
    rule: str
    proposal: str | None = None
    candidates: tuple[str, ...] = ()
    witnesses: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    chain: tuple[str, ...] = ()
    tenant: str | None = None
    lines: tuple[str, ...] = ()
    rows: tuple[int, ...] = ()
    remint_token: bool = False

    @property
    def home(self) -> RecordHome | None:
        """The home a record written now is given (`Store.put_memory`);
        None where nothing could be read (INDETERMINATE, DEFERRED), where
        the write's home is `Membership.unresolved_home` (decision B6)."""
        if self.project is None:
            return None
        return RecordHome(self.project, self.checkout, self.rule)


@dataclass(frozen=True)
class Declared:
    """What a declaration changed: the proposal it closed, the project
    the checkout belongs to now, the rows written, and the checkout that
    needs a token of its own when no token writer was given."""

    action: str
    proposal: str | None
    project: str | None
    rows: tuple[int, ...]
    remint_checkout: str | None = None


@dataclass(frozen=True)
class Why:
    """``bettermemory project why <id>`` (design 8.10): the record's
    project, checkout and rule, and every log row that decided them,
    oldest first."""

    record_id: str
    project: str | None
    checkout: str | None
    rule: str | None
    rows: tuple[LogRow, ...]
    text: str


@dataclass(frozen=True)
class UnresolvedProject:
    """A provisional project of decision B6, as health lists it among the
    open questions: the writes made at `path` while nothing could be read
    there, waiting for the resolution that settles them."""

    project: str
    name: str
    state: str
    checkout: str | None
    path: str | None
    memories: int
    episodes: int


@dataclass(frozen=True)
class Health:
    """The membership section of health (design 8.10). ``unresolved``
    lists the provisional projects of decision B6 with their record
    counts, beside the open proposals; ``provisional`` holds the others."""

    open_proposals: tuple[Proposal, ...]
    provisional: tuple[Project, ...]
    suppressed_turns: Mapping[str, int]
    indeterminate: int
    deferred: int
    without_live_view: tuple[str, ...]
    unresolved: tuple[UnresolvedProject, ...] = ()


@dataclass(frozen=True)
class EpisodeRef:
    """An episode as the handoff selects it."""

    id: str
    session_id: str
    created: datetime


@dataclass(frozen=True)
class Handoff:
    """The handoff's selection (design 3.6): the session whose episodes
    are offered, the rule, the episodes, and the label of a session taken
    from another checkout."""

    session_id: str | None
    rule: str
    episodes: tuple[str, ...] = ()
    label: str | None = None


@dataclass(frozen=True)
class RecallAdmission:
    """A prompt recall's admission over its relevant hits."""

    delivered: tuple[str, ...]
    suppressed: tuple[tuple[str, str], ...]


def _strings(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"a project identity's {key!r} is a list of strings")
    return tuple(value)


def _flag(raw: Mapping[str, Any], key: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"a project identity's {key!r} is true or false")
    return value


@dataclass(frozen=True)
class PortableIdentity:
    """A project as another store can recognise it (design 8.7, E27): its
    bound file ids, its oldest and newest witness commits (at most
    `EXPORT_WITNESSES`), its remote keys (a ``local:`` key names a path and
    never travels), its display name, whether it is a folder project or a
    boundary, and the position of its parent in the same document. No
    local id (P001, K003) and no path."""

    name: str
    file_ids: tuple[str, ...] = ()
    witnesses: tuple[str, ...] = ()
    remotes: tuple[str, ...] = ()
    folder: bool = False
    boundary: bool = False
    parent: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "file_ids": list(self.file_ids),
            "witnesses": list(self.witnesses),
            "remotes": list(self.remotes),
            "folder": self.folder,
            "boundary": self.boundary,
            "parent": self.parent,
        }

    @classmethod
    def from_json(cls, raw: Any) -> PortableIdentity:
        """The identity an export document carries; ValueError names what
        is malformed, a witness that is not a full commit id included
        (`_FULL_SHA`, ENG-157): an abbreviation would be held by whichever
        repository has an object with that prefix. Remote keys naming a
        path are dropped, and at most `WITNESS_BOUND` witnesses are read."""
        if not isinstance(raw, Mapping):
            raise ValueError("a project identity is an object")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("a project identity names the project")
        parent = raw.get("parent")
        if parent is not None and (
            not isinstance(parent, int) or isinstance(parent, bool) or parent < 0
        ):
            raise ValueError("a project identity's parent is a position or null")
        remotes = {k for k in _strings(raw, "remotes") if not k.startswith("local:")}
        witnesses = tuple(w.strip().lower() for w in _strings(raw, "witnesses"))
        for commit in witnesses:
            if not _FULL_SHA.fullmatch(commit):
                raise ValueError(
                    f"a project identity's witness {commit[:72]!r} is not a full "
                    "commit id"
                )
        return cls(
            name=name,
            file_ids=tuple(sorted(set(_strings(raw, "file_ids")))),
            witnesses=tuple(dict.fromkeys(witnesses))[:WITNESS_BOUND],
            remotes=tuple(sorted(remotes)),
            folder=_flag(raw, "folder"),
            boundary=_flag(raw, "boundary"),
            parent=parent,
        )

    def digest(self) -> str:
        """The identity's evidence and name as one value, so an import of
        the same identity from the same store finds the project an earlier
        import minted for it."""
        body = json.dumps(
            {
                "name": self.name,
                "file_ids": sorted(self.file_ids),
                "witnesses": list(self.witnesses),
                "remotes": sorted(self.remotes),
                "folder": self.folder,
                "boundary": self.boundary,
            },
            sort_keys=True,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class ImportedProject:
    """How `Membership.import_identity` mapped one imported identity: the
    local project its records are homed in, the rule their home rows
    carry, the witness codes each local candidate showed, and the
    ``import`` questions opened for it."""

    project: str
    rule: str
    codes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    proposals: tuple[str, ...] = ()


class Liveness(Protocol):
    """What a view's path holds now: LIVE (the checkout's own object),
    LIVE_LEGACY (a repository at a legacy checkout's path), GONE,
    UNREACHABLE (the OS could not say) or REPLACED."""

    def __call__(self, checkout: Checkout, view: View) -> str: ...


#: The declarations that write a project file, and the command each names.
_COMMANDS: Mapping[str, str] = {
    "init": "init",
    "init_new": "init --new",
    "init_rebind": "init --rebind",
}
_INITS = frozenset(_COMMANDS)

#: Writes a checkout a token of its own (a declared separation, a
#: superseded token); returns the token or None, and the status.
TokenWriter = Callable[[Checkout], tuple[str | None, str]]


# ---------------------------------------------------------------------------
# The one membership function (design 3.5)
# ---------------------------------------------------------------------------


def relation(home: Home, caller: Resolution) -> str:
    """How a record's home stands to the caller: ``unknown`` when nothing
    could be read where the caller is; ``global_named`` for a record
    written outside any project whose scopes name a project on the
    caller's chain (`Home.named`), ``global`` for any other record written
    outside any project; ``other_tenant`` when the two sit on different
    sides of a boundary; ``same_project`` when the home's project is on
    the caller's chain (its project, a sub-project's parents, the
    workspaces around it); ``sub_project`` when it is a sub-project of
    the caller's; ``other_project`` otherwise, and for every project
    record when the caller is outside any project."""
    if caller.project is None:
        return UNKNOWN
    if home.project == GLOBAL:
        if caller.project != GLOBAL and not home.named.isdisjoint(
            caller.chain or (caller.project,)
        ):
            return GLOBAL_NAMED
        return GLOBAL_RELATION
    if (home.tenant or None) != (caller.tenant or None):
        return OTHER_TENANT
    if caller.project == GLOBAL:
        return OTHER_PROJECT
    if home.project in caller.chain:
        return SAME_PROJECT
    if caller.project in home.chain:
        return SUB_PROJECT
    return OTHER_PROJECT


def admitted(surface: str, home: Home, caller: Resolution) -> bool:
    """Whether `surface` may deliver the record to the caller: the fixed
    table of design 3.5. A caller nothing could be read for is admitted
    nothing; its resolution wrote the row. A record written where nothing
    could be read (decision B6) is admitted only to a caller resolved to
    its provisional project, which no caller is until a merge or a
    declaration settles it: until then no surface delivers it."""
    if surface not in IN_PROJECT_POLICY:
        raise ValueError(f"unknown surface {surface!r}")
    rel = relation(home, caller)
    if rel == UNKNOWN:
        return False
    if home.unresolved and rel != SAME_PROJECT:
        return False
    if caller.project == GLOBAL:
        return rel in OUTSIDE_POLICY[surface]
    return rel in IN_PROJECT_POLICY[surface]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _norm(path: str) -> str:
    stripped = path.rstrip("/\\")
    return stripped or path


def _same_path(a: str, b: str) -> bool:
    return _norm(a) == _norm(b)


def _inside(path: str, root: str) -> bool:
    """`path` is `root` or below it, whichever separator the two are
    spelled with: recorded paths cross machines (`_flavour`)."""
    path, root = _norm(path), _norm(root)
    return path == root or any(path.startswith(root + sep) for sep in ("/", "\\"))


_DRIVE_PATH = re.compile(r"^[A-Za-z]:")


class _PathModule(Protocol):
    """What `_flavour` needs of ``posixpath`` and ``ntpath``."""

    @property
    def sep(self) -> str: ...

    def join(self, a: str, /, *paths: str) -> str: ...

    def relpath(self, path: str, start: str) -> str: ...

    def normpath(self, path: str) -> str: ...


def _flavour(path: str) -> _PathModule:
    """The path functions a recorded path is joined, split and normalised
    with: ``ntpath`` when it is spelled with backslashes or a drive,
    ``posixpath`` otherwise. Recorded paths cross machines (a record
    written on Windows is read on macOS and the reverse, and a view on
    one machine is extended by a path recorded on another), so the
    spelling of the path decides, never the platform this runs on: a
    POSIX path extended on Windows stays a POSIX path."""
    if "\\" in path or _DRIVE_PATH.match(path):
        return ntpath
    return posixpath


def _path_under(base: str, *parts: str) -> str:
    """`parts` under `base`, in `base`'s spelling (`_flavour`)."""
    return _flavour(base).join(base, *parts)


def _rebase(path: str, root: str, target: str) -> str:
    """`path`, which lies under the view `root`, at the same place under
    `target`: the part below `root` read in `root`'s spelling, written in
    `target`'s, and the result normalised in `target`'s."""
    source, dest = _flavour(root), _flavour(target)
    relative = source.relpath(path, root)
    if source is not dest:
        relative = relative.replace(source.sep, dest.sep)
    return dest.normpath(dest.join(target, relative))


def _basename(path: str) -> str:
    return os.path.basename(_norm(path).replace("\\", "/").rstrip("/")) or path


def _same_number(a: ObjectKey | None, b: ObjectKey | None) -> bool:
    """Two objects with one device and inode number, birth time aside: a
    re-key between them changes nothing (decision B20)."""
    return (
        a is not None and b is not None and (a.device, a.inode) == (b.device, b.inode)
    )


def _common_by_pointer(kind: str | None, git_dir: str | None) -> str | None:
    """The common directory a git directory names by where it sits, read
    off the path alone: a linked worktree's is the directory above its
    ``worktrees/<name>`` (git makes the worktree's git directory there and
    points its ``.git`` file and ``commondir`` at it); a primary's or a
    bare repository's git directory is its own common directory. None for
    anything else (design 8.7)."""
    if not git_dir:
        return None
    if kind == LINKED:
        admin = os.path.dirname(_norm(git_dir))
        if os.path.basename(admin) != "worktrees":
            return None
        return os.path.dirname(admin)
    if kind in (PRIMARY, BARE):
        return _norm(git_dir)
    return None


#: The path module `_dir_key` folds with: a test swaps in ``ntpath`` to
#: read Windows spellings on any host (`location.view_path` likewise).
_DIR_PATHS: Any = os.path


def _dir_key(path: str) -> str:
    """A directory as a declaration's folder is compared with a view or
    a question's folder: normalised, case folded where the platform folds
    case. A folder from outside the capture is read as a view records it
    first (`location.view_path`, ENG-152)."""
    return str(_DIR_PATHS.normcase(_DIR_PATHS.normpath(path)))


def _dir_below(here: str, root: str) -> bool:
    """Whether the directory key `here` lies below the key `root`
    (`_dir_key`), in the separator they were folded with."""
    sep = str(_DIR_PATHS.sep)
    return here.startswith(root.rstrip(sep) + sep)
