"""`Origin`, where a memory was written from: the working directory,
the repository's remote, the branch and the worktree root a write's
caller stood in, which `origin.capture` fills and every record carries.

It lives apart from `origin.py` to keep that module under the package's
2,000-line bound (ENG-96). It is a leaf: it imports pydantic and no
module of the package. `origin` imports it and re-exports `Origin`, so
every caller keeps reading `origin.Origin`.
"""

from __future__ import annotations

from pydantic import BaseModel, PrivateAttr


class Origin(BaseModel):
    """Where a memory was written from. All fields optional.

    A memory with `origin = None` is global (e.g. written before this
    feature shipped). A memory with `origin.cwd` set but `origin.repo`
    null was written outside any git repo, or in a checkout with no
    remotes at all (`worktree_root` distinguishes the two: it is set in
    the latter case). A memory with `origin.repo` set but a different
    value from the caller's current repo is cross-project and gets
    filtered out by `auto_scope=True`.

    `worktree_root` is the path of the git worktree the write happened
    in — `git rev-parse --show-toplevel` from the cwd. Repo URL
    matching alone treats two worktrees of the same repository as the
    same workspace, which means notes written while debugging
    `feature-x` in `~/repo-feature-x/` would surface for the user when
    they switch to `~/repo-bug-fix/` and trigger an unrelated search.
    The audit named that "worktree leakage" — capturing the worktree
    root and using it as a secondary discriminator in the auto-scope
    filter closes the hole without forcing the user to re-tag every
    write. Null when the write didn't happen inside a git checkout
    (then there's no worktree distinction to draw) or for memories
    written before this field shipped (legacy memories pass through
    the worktree filter, mirroring how legacy `repo`-less memories
    are treated as global).
    """

    cwd: str | None = None
    repo: str | None = None  # raw remote URL or null
    branch: str | None = None  # current branch or null (detached HEAD → null)
    worktree_root: str | None = None  # `git rev-parse --show-toplevel` or null
    # Which channel named the directory this origin describes — one of
    # `identity.WORKSPACE_SOURCES` when `capture()` resolved it (`header`,
    # `env`, `roots`, or the labeled fallback `process-cwd`); None on a
    # record written before the field existed, or captured from an
    # explicit `cwd` whose caller said nothing. Persisted only when it is
    # NOT the process cwd, so a non-declaring client's frontmatter stays
    # byte-identical to the pre-field shape; the read surfaces label the
    # absent case `process-cwd` explicitly (`ResponseBuilder.origin_to_dict`),
    # because an unlabeled cwd is exactly the silent default 7.10.0 retired.
    source: str | None = None

    # Other official spellings of the remote `repo` was read from — every
    # raw `git config --get-all remote.<name>.url` value other than the
    # captured URL itself. Populated by `capture()` on the CALLER-side
    # Origin only; a PRIVATE attribute, so it is never serialized into
    # memory frontmatter or event payloads (the on-disk format and event
    # schemas are unchanged). Exists because releases through v3.9.0
    # captured `repo` via `git config --get remote.origin.url` (last URL
    # of a multi-valued remote, raw insteadOf alias) while the current
    # idiom (`git remote get-url origin`) returns the first URL with
    # aliases expanded — stored origins from old captures need the
    # alternate spellings to keep matching. See `_CALLER_REPO_ALTERNATES`
    # for how `repos_match` consumes them.
    _repo_url_alternates: tuple[str, ...] = PrivateAttr(default=())

    # Whether git ANSWERED `capture()`'s probes. `repo` and `worktree_root`
    # are null both when git looked and said "not a repository" and when
    # git could not be asked — no binary on PATH, a timeout, an OSError
    # from the spawn — and the two mean opposite things to every consumer
    # that keys a shield on them: the first is a caller standing nowhere,
    # the second is a caller standing somewhere this process cannot see.
    # A repository git refuses to read (dubious ownership, an extension it
    # does not know) is the second kind too, though its `worktree_root` is
    # read from the files (`origin_gitfiles.refused_worktree_root`): git
    # named no remote and no branch there, so `repo` and `branch` are null
    # for want of an answer, not for want of a remote.
    # PRIVATE like the alternates above: never serialized into frontmatter
    # or an event payload; a fact about this capture, not about a memory.
    _git_indeterminate: bool = PrivateAttr(default=False)

    @property
    def git_indeterminate(self) -> bool:
        """True when this origin's null `repo` / `worktree_root` (and
        `branch`) are the product of git not answering rather than of git
        answering "no": git could not run, or it refused to read the
        repository the files place the caller in, whose root `worktree_root`
        then carries where the files settle it. Consumers that would open a
        scope boundary on a null must treat this as "could not tell" and
        hold the boundary instead."""
        return self._git_indeterminate

    # The first line git wrote when it refused the repository the files
    # place the caller in (ENG-175), for a refusal that names why; None
    # wherever git answered or could not run. PRIVATE, as above.
    _git_refusal: str | None = PrivateAttr(default=None)

    @property
    def git_refusal(self) -> str | None:
        """Why git refused the repository this origin stands in, as git
        said it; None where it did not refuse."""
        return self._git_refusal
