# Membership redesign, round 2: the design

2026-10-20 (UTC). How bettermemory 9 decides which project a memory belongs to and where its paths point. Written by Claude Opus 5.5 for the round 2 brief of the directing session (Claude Fable 5.1), after the owner rejected round 1's six stated residuals; it replaces round 1's design and folds in the amendments A1 to A5. Against rs-f1 at 7032be0c. Design and experiments only; nothing in the product changed. Receipts: `experiments/README.md` (37 experiments, 389 checks, 0 failed, in `experiments/out/run_all.log`). Decisions: `decisions.md`. Plan and the RS-F6 declaration: `plan.md`.

## Status

This is the round 2 membership design as it was written on 2026-10-20. It was ruled on 2026-10-21, every decision on its recommended option (the appendix "Decisions D1 to D19"), built as RS-F6 within the build decisions of the appendix "Build decisions B1 to B24", and released in 9.0.0rc4 together with RS-F5 under RULE rs-v5. The experiments E01 to E37, the prototype under `experiments/proto/` and the files the text names beside them (`experiments/README.md`, `experiments/out/run_all.log`, `plan.md`) lived outside this repository; the cases of the experiments became the tests named by their section 7 rows (`test_t17_...` in `tests/test_membership_resolution.py` for row T17, for example), and `decisions.md` is the first appendix.

9.0.0rc6 (2026-10-21) retired decision D6 and the "Declare" item of section 8.8: nothing in the product requires a human at a terminal or a typed confirmation. `bettermemory project join | new | init` and `bettermemory memory move` run from any stdin, and the installer removes the deny rules earlier builds wrote. The agent answers membership questions through `memory_admin` (`answer_question`, `join_project`, `unjoin_project`), a join tethered to the user's own words (the last prompts seen for the checkout), and the lines L1 to L10 carry the question id and the exact call. A legacy question is opened only where the evidence decides something (`membership.legacy_asks`); the rest are closed as separate when the daemon starts. Decisions D5 and D14 read with that change.

Later releases changed the implementation around the design (the daemon's lock spelling, the restore lock, the key ring's binding). The text and its numbering are kept as written because the code cites them: `design N.N`, `decision Dn`, `decision Bn`, a witness code, a case row (I34, T6, X2) or an experiment (E10) in a comment or docstring names this document, and `tests/test_design_citations.py` checks that every cited section and decision is here. The owner's project names, paths and counts are left out; where they stood, a generic sentence says what the run showed.

## 1. The core

A memory's home is the checkout it was written in, recorded in its own table in the same transaction as the memory and never rewritten. A checkout is a git directory (or a plain folder with a project file) on one machine. A project is the set of checkouts that one body of evidence ties together. The evidence is a set of witnesses, each an observable fact about the caller's checkout: its git directory's filesystem object, its object store, the commits it holds, its remotes, its project file, its checkout token, its path, its content (section 2.2).

Every case ends in one of three outcomes, decided by one ladder (section 3.2):

- **SILENT.** A declaration, the same git directory, the same object store, or two independent witnesses that agree with nothing contradicting them. The session gets its project's memories on the first prompt and no line.
- **ASK ONCE.** One witness, witnesses that contradict each other, or content alone: the cases where two different situations look the same by every observable. The checkout gets a provisional project and a durable proposal: nothing of any other project is delivered there, what is written there stays with it, and every session's first output names the question and its two commands until it is answered. It is answered once for that checkout, by `bettermemory project join <id>` or `bettermemory project new`, or by evidence arriving (a commit, a fetch, a committed project file), which closes it with a row.
- **NEW.** No witness: a new project, named in the session's first output.

The design has no limits. Round 1's section 7 listed six cases as proven impossible; each is closed below (section 5.2 and the rows marked R1 in section 7), with an experiment for each. Under the owner's bar a case that could not be closed would be a blocker in the report, with two candidate mechanisms; there is none.

One function answers same project, sub-project, global, other project, other tenant or unknown for every surface (section 3.5). Run-state (episodes, the handoff, recall segments) stays per checkout. Claims anchor in the home checkout's live view. Every suppression, question, deferral and join writes a log row, and `bettermemory project why <id>` prints the rows behind any record's home. No memory or episode row is rewritten; the migration builds the tables from the store's own evidence with no manual step.

### 1.1 What bettermemory writes into a repository

Exactly one file, and only where it may: the checkout token, `info/bettermemory` inside the git directory (`.git/info/bettermemory`; in a linked worktree `.git/worktrees/<name>/info/bettermemory`; with `--separate-git-dir` or `GIT_DIR`, under that directory). It is written at a checkout's first sight, replaced only when the checkout leaves the line it was copied from (`project new`, or history superseding the token, E21 g), and holds two lines:

```
bettermemory checkout token v1
0XNK4RY1ZCMBN31P1J2EN2STQP
```

The second line is 26 random Crockford base-32 characters (130 bits). git never tracks or transfers `info/` contents: `git status` stays clean, `git clone` does not copy it, and git's maintenance leaves it as it was (E19 a: `git gc --aggressive`, `git repack -a -d`, `git worktree prune`, `git update-server-info`); git keeps its own files there under their own names (exclude, attributes, sparse-checkout, refs). What copies a git directory copies it (cp, rsync, Finder, Time Machine, an archive, a synced folder), which is what makes it a witness of moves and copies. It is not written into a bare repository (shared or served), into another owner's repository, where the directory is read-only (the failure is recorded and said in the line), or anywhere when the policy switch `membership.write_token = false` is set; every rung of the ladder works without it (section 3.2: history, remotes, the project file, the init stamps, the content witness).

Nothing is ever written into a working tree on bettermemory's own initiative. `bettermemory project init` writes `bettermemory.toml` into the directory it is run in, on that command only, for the user to commit (section 2.1).

### 1.2 Round 1's objections to an id file, answered one by one

Round 1 (its section 6 (c)) rejected an id file in `.git`. The token is that file made one witness among several:

1. *Copies share it and leak both ways.* A copy carries the token and the history, so it is a copy, and copies share (D1). One token on two live checkouts with no commit in common asks (E19 c, E22 a); against a project with commits the token counts only with its history (E21 a).
2. *A foreign repository can plant one.* Planted, it is one witness: asked, nothing delivered, its writes kept apart (E21 a, a3). Alone it reaches only a commit-less project whose checkout is not live, the case it exists for, and only with a 130-bit value read from that checkout (E21 a2).
3. *Read-only and foreign-owned repositories.* The write fails or is skipped, the line says which, and the other witnesses decide (E20).
4. *Windows.* A file in the git directory, written the same way (8.7).
5. *Policies against writing into `.git`.* The switch, with the init stamps (A2) and the content witness in its place (E15 f, g, E20 d, E33).
6. *Claims at mint, a `link` command.* The token claims nothing; it says which checkout a git directory descends from. Declarations stay CLI only (D6).

## 2. The model

### 2.1 Terms

- **Machine.** One OS user on one host, VM or container: a random id in the state directory, minted with three facts (hardware or machine id: IOPlatformUUID, `/etc/machine-id`, MachineGuid; hostname; the id file's own object), compared at every open; carried to another machine, it gets a new id and a `machine_cloned` row before any resolution (E26). A block-level clone with every fact equal keeps one id, harmless locally (each store decides only for its own disk); at sync and import a machine is keyed by (machine id, store id), and a fork takes a new store id, so two stores never merge their checkouts (section 8.7, E26 e).
- **Checkout.** One git directory on one machine, recognised by its filesystem object (device, inode, birth time where reported). A plain folder with a project file is a folder checkout, recognised the same way.
- **Commit-less.** No commit reachable from any ref: HEAD, `refs/` and `packed-refs` (the capture reads up to 64 ref tips). A repository on an orphan branch (`git checkout --orphan`) has an unresolvable HEAD and commits on other refs, so it is born and its history decides (E21 g).
- **View.** A path at which a checkout was seen, with first and last sight. A view says where, never what.
- **Project.** A set of checkouts and its evidence: witness commits (HEAD at first sight, writes and verifies; at most 64, the 16 oldest kept), each with a fingerprint (author and committer lines and message, sha256) and up to 8 file blobs; remote keys; bound file ids; a parent (sub-projects); a tenant. Merges are rows naming the evidence.
- **Project file.** `bettermemory.toml`: `[project]` with `id = "bmp_<26>"` (random), `name`, and optionally `boundary = true`. The nearest file at the cwd or an ancestor names the session's project. At a repository's root it binds the repository's project (it travels with every clone, fork, container and CI runner); below the root it names a sub-project; above the root, a workspace (a folder project around repositories); with `boundary = true`, a tenant boundary that nothing crosses. A plain folder with a file is a folder project.
- **Home.** For each memory and episode: the checkout it was written in and that checkout's project, or `global` for a write outside any repository and any project file.
- **Proposal.** A durable question on one checkout: its kind (first_sight, file_contradiction, remote_diverged, subproject_contradiction, legacy, import), candidates, evidence, the exact line, and its state. `bettermemory project pending` lists the open ones.
- **Declaration.** The user's statement from a terminal (decision D6): `project join`, `project new`, `project init [--new | --rebind]`, `memory move <id> --project`. Declarations are log rows with the OS user and outrank evidence.

### 2.2 The witnesses

| Code | The witness | Counts when |
|---|---|---|
| O | the same git-directory object (device, inode, birth); for a folder checkout the folder's object | birth on both sides; without birth, confirmed by the token, else the last HEAD held, else the init stamps (A2) by equality. After `git init` in a folder project, O of the enclosing folder with F re-keys the folder checkout to the new git directory (`checkout_rekeyed`, E23 j) |
| S | the same object store (a linked worktree's common directory) | always |
| H | holds witness commits of the project | one batch over every project's bounded set |
| Hf | holds commits with a witness's fingerprint (a rewritten history) | no witness commit is held |
| Hb | holds the file contents of witness commits: at least 2 blobs and half of one witness's sample | a remote names the project and history did not speak; never with a file alone (a template's product holds the template's files, E22 e) |
| R | names a remote key of the project (every remote, not only origin; a `file://` or path remote names the views seen there) | always |
| D | names a remote the user declared for the project when answering a remote-only question in a shallow clone | the caller is shallow |
| F | carries a project file whose id is bound to the project | always; with R alone counts as two only where history cannot speak (unborn, shallow) |
| T | carries the checkout token of one of the project's checkouts | with another witness; alone only when caller and project are both commit-less (section 2.1) and no live checkout carries it; dropped when history disagrees, and the checkout then gets a token of its own (E21 g) |
| B | sits at the path of a primary checkout of the project whose object is live nowhere, holding its commits, its token compatible | this machine only |
| C | content: the same folder name, and relative paths and sizes at Jaccard 0.9 or both empty | for a repository never joins: it turns NEW into ASK ONCE naming at most 3 candidates, and with more than 3 it is NEW, the line giving the count and `project join` (E20 g); for a folder project, whose only other witness is its file, C with F counts as two (E23 d, e) |

Two witnesses are independent when they rest on different facts: H, Hf and Hb count as one history witness; C counts only beside F in a folder project.

### 2.3 What membership does not rest on

What a record is about (scopes, attested paths, cited commits), the harness's project key, a path alone, and the root commit. A path enters as B, together with the project's commits. The reasons are round 1's (its section 2.2) and stand.

## 3. The decision rule

Prototype: `experiments/proto/` (`capture.py`, `objects.py`, `registry.py`, `membership.py`, `portable.py`).

### 3.1 Capture

Each caller reads its location in its own namespace (the hook client, the MCP server, the CLI) from files: top level, git and common directories and their objects, HEAD, every remote URL, the shallow marker, the kind, the token, the init stamps (`description` and `info/exclude`: inode, mtime, ctime, size) and the project files from the cwd up. Commit and blob lookups read loose objects, pack indexes and packs (with deltas and alternates) directly (`proto/objects.py`), with `git cat-file --batch-check` as the fallback; reading files runs no repository configuration, so git's safe.directory refusal never applies and nothing overrides it (E20 f). The capture is cached against a file signature; the walk above the root is cached apart. An evicted file (SF_DATALESS on macOS, cloud placeholders elsewhere) is never read on the hook's path: a fetch starts with the capture's budget (300 ms in the prototype) (E36 c). The location token goes to the daemon, which never has to read the caller's paths.

### 3.2 The ladder

The first rung that answers decides (`Registry._resolve`).

0. **Nothing readable** (the cwd unreadable to the session itself, a dropped mount, an rc3 client sending no location token for a path the daemon cannot see): nothing from any project, line L8, a `resolution_indeterminate` row. **An evicted HEAD or config at first sight, the fetch outrunning the budget**: the same for that prompt, line L9 and a `resolution_deferred` row; the next prompt decides. **Outside any repository and any project file**: global.
1. **A declaration** (an answered proposal, `project init`, `memory move`).
2. **O**: the same checkout. A new path adds a view (`checkout_moved` or `checkout_view_added`).
3. **S**: another checkout of the same project (a linked worktree).
4. **Two independent witnesses agreeing, nothing contradicting**: the project (`resolution_joined` with the witnesses); with B, the recorded checkout re-keyed (`checkout_rekeyed`); after `git init` in a folder project, O of the folder with F, the folder checkout re-keyed to the git directory (`checkout_rekeyed`, E23 j). F with R counts as two only where history cannot speak; T alone counts as two only for a commit-less caller and project with no live carrier. Several projects qualify: the stronger evidence, then the most specific line (the witnesses that descend from the others'), else ASK ONCE. Under the A3 switch (`membership.strict = true`, decision D8) a join resting on history without F or B asks.
5. **A contradiction, one witness, or content alone**: ASK ONCE (section 3.4).
6. **No witness**: NEW, line L2.

Every sighting adds its HEAD as a witness, its remotes and its root file. A provisional project re-tallies when its evidence signature changes (HEAD, remotes, file, token, shallow) and merges by itself when two witnesses now agree (`proposal_answered` by evidence; E32 b).

### 3.3 Contradictions

- **token_shared_no_commit**: the caller carries the token of a live checkout with which it shares no commit (template siblings, a planted token).
- **file_names_other_history**: the root file names project P while the repository holds another project's commits and none of P's.
- **token_superseded** (not a question): both sides have commits and share none (an orphan branch counts as born, section 2.1); history outranks the token, the token is dropped from the tally, a row says so, and the checkout gets a token of its own, so its copies are not taken for the other line (E21 g).
- **Across a boundary** a witness is dropped and a line says "kept apart" (E34 d).

### 3.4 Proposals

A proposal is opened once per checkout and question and named in every session's first output there until answered; `bettermemory project pending` lists all of them. A first-sight proposal re-tallies when the checkout's evidence changes, so a commit, a fetch or a committed project file answers it by evidence, on every machine whose checkout sees it. Answering `join` to a question that rested on the remote alone in a shallow clone declares that remote the project's for shallow clones (witness D), so a CI fleet is asked once per store (E32 e, f). The exact lines are in section 5.4 (L1 to L10).

### 3.5 One membership function

`relation(home, caller)` answers `same_project` (the home is on the caller's chain: its project, a sub-project's parents, the workspaces around it), `sub_project` (the home is a sub-project of the caller's), `other_tenant` (the two sit on different sides of a boundary), `global`, `other_project`, or `unknown` (nothing readable, or deferred). Each surface admits a fixed subset; nothing else decides membership.

| Surface | Caller in a project | Caller outside any project |
|---|---|---|
| session brief, prompt recall, review strata | same_project | nothing |
| search auto-scope, the hook's pool prefilter, links | same_project, sub_project, global | global |
| search with auto_scope off (an explicit request) | every project except other_tenant | global, other_project |

`unknown` is admitted nothing, with a row. Copies, clones, worktrees, containers and other machines of one project share its memories (D1).

### 3.6 Run-state and anchoring

Both as in round 1 (its sections 3.4 and 3.5), re-run unchanged (E01, E04, E09, E11, E14 18 of 18). Episodes, the handoff, recall segments and session joins key on the checkout; a primary checkout with no episode of its own is offered the newest session of a non-live primary checkout of its project, labelled; nothing takes a live sibling's run-state, and another machine's checkout is never live from here (E26, E36 a). Claims resolve at the home checkout's live view, translate across recorded views, and in a stand-in checkout confirm by presence without convicting. Folder checkouts anchor the same way: relative claims resolve at the folder root, and absolute ones translate across the folder's recorded views.

## 4. The eight failures

Each is HANDLED; the receipts are round 1's experiments re-run under the round 2 ladder (198 checks in E01 to E18) and the new ones.

1. **A remote-less checkout that moves loses its memories** (seen in the owner's store). The moved folder is the same git directory (O) on one filesystem; across filesystems it carries its token and its history (T+H). E01 (rc3 loses 3 of 3, the design keeps 3 of 3), E02, E06, E11, E15 (b). E17 V2: without the manual re-home the migration brings the moved folder's memories into the live checkout's project.
2. **A checkout that gains a remote loses its remote-less memories** (seen in the owner's store). The checkout is the same object before and after `git remote add` or a URL change; a remote is evidence added to the project, never a condition on the caller. E05 (rc3 keeps 1 of 3 and, after a URL change in place, 1 of 4; the design keeps all); E17.
3. **A remote-having checkout that moves keeps its memories only while the old path is positively gone.** No rule reads the old location. E08, E01, E14 case 3.
4. **A different repository created at a reused path inherits remote-less memories.** It is another object, without the token the checkout carried and without its commits: NEW with line L2 naming the checkout that was there and the join command; the moved original keeps its own. E07, E15 (a), and (e) for round 1's item 4 (below).
5. **A folder deleted, or moved before any fix lands, orphans its memories** (seen in the owner's store). A memory never depends on its folder: its project keeps its other checkouts, gains one when a folder carrying its token or holding its commits appears, or waits with no live checkout. The legacy cases the store cannot tie to a live checkout are durable proposals in the candidate checkout (L6): in E17 two such projects (one with 3 of its 4 recorded commits held by a live checkout, one with 1 of 29).
6. **Attestations and citations anchor at origin.worktree_root.** Section 3.7. E14 (18 of 18); E17: 69 paths of 23 memories under a dead worktree read 9 confirmed, 60 unconfirmed, 0 drift.
7. **Episode handoff uses strict worktree equality** (43 episodes of one project). Section 3.6. E01, E06, E09, E11, E37 (a); E17 V1: the handoff pool at that project goes from rc3's 3 episodes to 46.
8. **All of it is silent.** Every suppressed recall turn writes `recall_suppressed`; every question, deferral, unreadable caller and first-sight join writes its row; health counts them; the first output names every open question. E13, E36 (c3), E37 (d).

## 5. Round 1's terms, closed: the rejected limits and the six

### 5.1 The third RS-F4 draft's five

- **d(i) A moved repository with no commits cannot prove continuation.** On one filesystem the object needs no commit (E06). Across filesystems, and for a copy with the original deleted, the token travels with the git directory: T+R with a remote, T alone without one (E02, E19 b, c). With the token write denied (read-only, policy) the content witness turns NEW into ASK ONCE with the line saying what could not be written (E20 c, d).
- **d(ii) A container sharing the host's store sees a second path as a separate checkout.** A second path never isolates: membership is the project's. E10 runs in real OrbStack containers with no network. Inside the container the bind-mounted git directory is another device and inode (virtiofs), and the container is a machine of its own, so it is a second checkout of the project, joined silently from the location token by the host checkout's token and the commits the container checked (T+H+R); a clone made inside joins by H+R; throwaway containers with the committed project file join by F+R; a copy at a path the host cannot see resolves by T+H from the token captured inside (E10 a to e); statx inside the container reads no birth time on the bind mount, and the token and history decide there (E10 f). Run-state stays per checkout.
- **d(iii) After a copy and the original's deletion, whichever copy is seen first continues it.** Nothing continues exclusively; both copies are checkouts of the project and the answers are the same in either order (E04, E19 d).
- **d(iv) Legacy memories whose folder vanished need an admin rebind.** Zero manual steps where the store holds evidence (E17 V2 brings a moved project's memories and episodes home); where it holds only partial evidence, a durable proposal in the candidate checkout, answered by one command or by a committed project file (E17).
- **d(v) When the old location is indeterminate, nothing moves.** No rule reads an old location (E11: EACCES on the old parent, a detached volume, a renumbering remount).

### 5.2 Round 1's six, closed

Round 1's section 7 ("Proven impossible") listed six cases. Each now has a mechanism and a receipt; none is documented as a limit.

1. **An unborn remote-less repository moved to another filesystem, or copied with the original deleted.** Closed by the token: SILENT by T (E02, E19 b, c delete-first). Copied while the original lives: two live commit-less checkouts with one token, which template siblings also are: ASK ONCE (E19 c). Token write denied: the content witness, ASK ONCE (E20 c, d).
2. **A repository built to impersonate** (the project's commits and its remote). It is a clone by every observable: SILENT under the two-witness rule (E12 g, E21 d), ASK ONCE under amendment A3 (E21 d), a choice for the owner (decision D8). Every partial impersonation asks and receives nothing: a planted token (E21 a, a3), a planted file (E21 b), a copied file without commits (E21 c), a file naming P over another project's history (E21 f), the remote alone (E07, E12 a).
3. **Template siblings before divergence.** Commit-less siblings carry one token while both live: ASK ONCE each, answered `project new`, never asked again (E22 a, a2). Born siblings made products by `project init --new` and the committed file: SILENT, separate, their teammates' clones too (E22 b). Re-pointed to their own remotes: H alone, ASK ONCE (E22 c); re-pointed later: L5 (E22 c). A sibling that keeps the template's remote is a clone of the template and joins it until `project init --new` (E22 c, D8).
4. **An unborn repository re-created at the same path with the same inode number, without birth time.** Refuted by the token it lacks: NEW with L2 (E15 e). With the token write denied: amendment A2, the init stamps compared by equality (E15 f, g), and no clock enters it (E33: the decisions are identical with the clock a day ahead and a day behind; the comparison A2 first proposed flips with the clock).
5. **A caller git cannot read.** Another owner's repository that git refuses is read from its files by the object reader (E20 f: history and remote join it, no token written). A path the daemon cannot see is resolved from the client's location token (E10 a, e). What remains unreadable is unreadable to the session too (a cwd it cannot open, a dropped mount): L8, nothing of any project, and the first readable prompt decides (E13). An evicted file is fetched or deferred one prompt (E36 c).
6. **Legacy records with no live folder.** Durable proposals in the candidate checkout, the counts unchanged from round 1, rows rewritten 0, the store copy's sha256 unchanged (E17).

### 5.3 What closed them

Three mechanisms, each one witness among several and none sufficient alone against history: the checkout token (items 1, 3 and 4), the project file (item 3, host migrations, history rewrites, CI), and durable proposals (items 2 under A3, 3 and 6). Two readers (the object reader and the client's location token) close item 5. The two-witness rule decides what is silent; the rest asks once.

### 5.4 The lines

Every line the closed cases print, as the prototype prints them (`<...>` filled in; `[...]` when it applies). They are the session's first output and the health listing; section 7 refers to them by number.

- **L1 first sight**: `bettermemory: <root> may be project "<name>" (<id>): <witness words>[ (could not write <git dir>/info/bettermemory: <status>)]. Until you answer, nothing from other projects is shown here and what is written here stays with this folder. Same project: bettermemory project join <id>; separate: bettermemory project new` (when the root's project file is bound, the separate command is `bettermemory project init --new`, which gives the file an id of its own; E22 e)
- **L2 new**: `bettermemory: <root> is a new project "<name>" (<id>)[; checkout <K> of "<name>" (<id>) was here before and this repository is not it (no token, commit or file of it); if it is: bettermemory project join <id>][; could not write its token (<status>)]`
- **L3 file contradiction**: `bettermemory: <root> is project "<name>" (<id>) but its project file names "<name>" (<id>). Keep: bettermemory project init --rebind; move this checkout: bettermemory project join <id>`
- **L4 joined by evidence**: `bettermemory: <root> is project "<name>" (<id>) (<witness words>); what was written here joined it`
- **L5 remote diverged**: `bettermemory: <root> now names <new key>, while project "<name>" (<id>) elsewhere still names <old key>. Still the same project: bettermemory project join <id>; a project of its own: bettermemory project new`
- **L6 legacy** (the migration): `bettermemory: the records of <name> (<n> memories, <m> episodes; its folder <path> is gone) may belong to project "<name>" here: this repository holds <k> of their <n> recorded commits. Same project: bettermemory project join <id>; ...`
- **L7 sub-project contradiction**: `bettermemory: <file> names "<name>" (<id>), which is not part of "<repo project>"; treated as "<parent>" until you decide. That folder is "<name>" (<id>): bettermemory project join <id>; a part of "<parent>" with an id of its own: bettermemory project init --new (in that folder)`
- **L8 nothing readable**: `bettermemory: cannot tell what <cwd> is (<detail>); nothing from any project is shown here`
- **L9 deferred**: `bettermemory: <root>: <file> is not on this disk yet (evicted by the file provider); it was asked for; this prompt shows nothing from any project and the next one decides`
- **L10 import**: `bettermemory: imported project "<name>" from <source> may be "<name>" (<id>: <codes>). Same project: bettermemory project join <id> (in that checkout); keep apart: bettermemory project new`

## 6. Rejected approaches, and their recorded reasons

Round 1's (its section 6) stand, with one changed:

- **(a) Widening in_project by root commit, "not a repository" read as gone, a backfill through Store.update.** Not re-proposed.
- **(b) A daily check, an orphan line, a manual rehome.** Not re-proposed; the decision is right on the next call.
- **(c) A random id file inside each repository's `.git`.** Adopted in a changed form: the checkout token is one witness, never enough alone against history, carried with the git directory and never transferred by git. Round 1's six objections are answered in section 1.2.
- **(d) The third RS-F4 draft's checkout registry.** Answered in 5.1.
- **The worktree-leakage decision, reversed** (D1), as in round 1.

Rejected in round 2:

- **A question at every first sight** (round 1's D8-B as written). Every fresh clone, container run and CI runner would ask, and a question asked where the answer is observable trains the owner to answer without reading. The ladder asks where two situations are identical by every observable; A3 remains the owner's switch for history joins (D8).
- **The token alone as identity.** It would let a planted token claim a project and let template siblings merge; it is one witness (E21, E22).
- **A clock comparison for amendment A2** ("config newer than the last sighting"). E33 shows its answer flipping with a day of skew; the stamps are compared with each other.
- **Content similarity as a joining witness.** A copy and a template sibling have the same files; content turns NEW into ASK ONCE and never joins (E20 c, E23 f).
- **Re-keying by path across machines.** A cloned VM keeps paths and inode numbers; re-keying would hand the clone the original's run-state (E26 c shows the hand-over without the machine-clone detection). Migrated machines join as new checkouts and are offered the old session, labelled.
- **Writing the project file without a command, or tracking the token.** A tracked token would make every clone a copy of one checkout; the working tree is written only by `bettermemory project init`.

## 7. Every case

Witness codes as in section 2.2; lines L1 to L10 as in section 5.4. Every ASK ONCE is answered by `bettermemory project join <id>` or `bettermemory project new` unless the row names another command. "R1.n" marks round 1's item n of its section 7, closed. Receipts are checks in `experiments/out/run_all.log` (E01 to E37 name experiments; I, T and X name rows); "argued" names the CI leg that turns the argument into a run (plan, phase 7).

### 7.1 Individual

| # | Case | The store knows | Witnesses | Outcome | Receipt |
|---|---|---|---|---|---|
| I1 | move on one filesystem | the object | O | SILENT | E01 |
| I2 | move to another filesystem, with commits | token, witnesses | T+H | SILENT; the old checkout's session offered, labelled | E02, E11 |
| I3 | the same, commit-less with a remote | token, remote | T+R | SILENT | E02 |
| I4 R1.1 | commit-less, remote-less, moved to another filesystem | token | T | SILENT | E02, E19 b |
| I5 R1.1 | the same copied, the original deleted, then seen | token, no live carrier | T | SILENT | E19 c |
| I5a | a born repository on an orphan branch (HEAD unborn, commits on other refs) carrying a commit-less project's token | its commits, its token | H+R; H | SILENT, the project its history names; without a remote ASK ONCE, L1; never the token's project by T alone | E21 g |
| I6 R1.1 | the same copied while the original lives | token on a live checkout, no shared commit | contradiction | ASK ONCE, L1 | E19 c |
| I7 R1.1 | the same with the token write denied (read-only, policy) | content recorded at first sight | C | ASK ONCE, L1 with "could not write ..." | E20 c, d |
| I7a | an empty commit-less repository with the token denied, after empty ones of the same folder name were deleted | their content (both empty) | C | ASK ONCE naming up to 3; with more than 3 NEW, the line giving the count and `project join` | E20 g |
| I8 | a copy, the original live | token, witnesses | T+H | SILENT; two checkouts, run-state per checkout | E03, E37 b |
| I9 | a copy, the original deleted, either order | token, witnesses | T+H | SILENT; the same answers in both orders | E04, E19 d |
| I10 | remote added, URL changed in place | the object | O | SILENT | E05 |
| I11 | commit-less moved on one filesystem, then a remote, a commit | the object | O | SILENT | E06 |
| I12 | another repository at a reused path | a checkout seen at that path | none | NEW, L2 naming that checkout and `project join` | E07, E15 a |
| I13 R1.4 | commit-less re-created at the same path, same inode, no birth time | token, init stamps | O refuted | NEW, L2 | E15 e, f, E33 |
| I14 | commit-less move without birth time | token, init stamps | O confirmed | SILENT | E15 c, g, E33 |
| I15 | a plain folder re-created at the old path (an iCloud shell) | nothing read there | O at the new path | SILENT; the shell is outside any project | E01, E08 |
| I16 | linked worktrees added, moved, removed, pruned, re-added | the object store | S, O | SILENT; a re-added worktree does not inherit run-state | E09, E19 e |
| I17 | the old path unreadable, a volume detached | object, token | O, T+H | SILENT | E11 |
| I18 | a remount renumbering the object; a restore at the same path | path, commits, token | B+H+T | SILENT, re-keyed, run-state continues | E11 c, E37 a |
| I19 R1.5 | another owner's repository that git refuses | read from its files | H+R | SILENT; no token written | E20 f |
| I20 R1.5 | a cwd the session itself cannot read, a dropped mount | nothing | none | NEW said so: L8, nothing from any project; the first readable prompt decides | E13 |
| I21 | HEAD or config evicted by a file provider | object; a fetch started | O; fetched; deferred | SILENT; offline at first sight L9, SILENT on the next prompt | E36 c1 to c3 (flag simulated) |
| I22 | GIT_DIR and GIT_WORK_TREE; `--separate-git-dir` | the object | O | SILENT; the token under that git directory | E30 |
| I23 | bare and mirror clones; worktrees of a bare repository | witnesses, remote | H+R, S | SILENT; no token in a bare repository | E31 |
| I24 | a shallow clone at a tip no witness names, no file | remote | R | ASK ONCE, L1; closed by evidence after `git fetch --unshallow` (L4) | E05, E32 a, b |
| I25 | a shallow clone at a recorded tip; with the file | witnesses; file | H+R; F+R | SILENT | E32 c, d |
| I26 | history rewritten in place (git filter-repo) | the object | O (T without birth) | SILENT | E25 a |
| I27 | a fresh clone of a rewritten history, no file | fingerprints, blobs, remote | Hf+R; Hb+R | SILENT | E25 b, b2 |
| I28 | a rewrite of dates and contents, no file | remote | R | ASK ONCE per store; with the file SILENT | E25 d, e |
| I29 | a plain folder with no project file | nothing | none | global; `bettermemory project init` makes it a project | E23 a, b |
| I30 | a folder project moved; copied; its file copied elsewhere | object, file, content | O; F+C; F | SILENT; SILENT; ASK ONCE | E23 c to f |
| I30a | `git init` in a folder project (the file at the new repository's root, no commit) | the folder's object, its file | O of the folder, F | SILENT: the folder checkout re-keyed to the git directory (`checkout_rekeyed`), its memories delivered; the first commit changes nothing | E23 j |
| I31 | nested folder projects; a repository inside one | files | F chain | SILENT; delivery up the chain | E23 g, h |
| I32 | a submodule; a sparse checkout | object, tree | O, H | SILENT; sparse paths checked in the tree | E18 |
| I33 | the daemon's or the filesystem's clock a day off | stamps by equality | unchanged | the same decisions at every skew | E33 |
| I34 | eight sessions resolving one new checkout at once | per-location lock | one checkout | one row, one answer; one token write wins | E28 |
| I35 | a `.git` in a synced folder, two machines | token, witnesses | T+H; T | SILENT; a checkout per machine, run-state per machine | E36 a, b |
| I36 | a laptop migrated or restored, the old one gone | machine facts, token, witnesses | T+H; T | SILENT; the old session offered, labelled | E26 d |
| I37 | a VM or disk cloned, both running | machine facts | new id, T+H | SILENT; separate checkouts and run-state | E26 b, c |
| I37a | a cloud image cloned block for block and booted twice: every machine fact equal, the store inside the image | the same machine id and store id | O in each store | SILENT: harmless locally; at import or sync a `store_forked` row and a new store id; no checkouts merge | E26 e |
| I38 | the store restored from an older backup | rows up to the backup | T+H | SILENT | E37 c |
| I39 | a repository bind-mounted into a container (OrbStack: virtiofs, another device and inode) | the host checkout's token, the commits the container checked | T+H+R | SILENT; a checkout of the project per container machine; run-state per checkout | E10 a (real) |
| I40 | a clone made inside a container; a copy inside at a path the host cannot see | the client's location token | H+R; T+H | SILENT | E10 b, e (real) |
| I41 | throwaway containers cloning a repository with the committed file | file id, remote | F+R | SILENT, no question | E10 c (real) |
| I42 | an rc3 hook client posting a container path the daemon cannot see | nothing | none | L8 for that prompt; the rc4 client sends the location token | E10 a control (real) |
| I43 | the owner's store at the upgrade | origins, re-homes, commits | per root | counts as round 1; 2 proposals (L6) | E17 |

### 7.2 Team

| # | Case | The store knows | Witnesses | Outcome | Receipt |
|---|---|---|---|---|---|
| T1 | a teammate's fresh clone, no file | witnesses, remote | H+R | SILENT | E05, E24 b |
| T2 | a fresh clone with the project file committed | file id | F+H(+R) | SILENT | E22 b, E24 d, E35 c |
| T3 | a fork naming its upstream | witnesses, remote | H+R | SILENT | E12 d |
| T4 | a fork under its own remote only (an Enterprise fork) | witnesses | H | ASK ONCE, L1; with the file committed upstream F+H, SILENT | E12 d, E21 e |
| T5 R1.2 | a repository with the project's commits and remote | witnesses, remote | H+R | SILENT (a clone); under A3 ASK ONCE | E12 g, E21 d, D8 |
| T6 R1.2 | a planted token or file; a file over another history | token, file id | T, F, contradiction | ASK ONCE; nothing delivered; its writes stay apart | E21 a to c, f |
| T7 R1.3 | a commit-less template copied twice, the template live | token on a live checkout | contradiction | ASK ONCE each; `project new` settles it for good | E22 a, a2 |
| T8 R1.3 | template clones made products by `project init --new` | file ids | F | SILENT, separate | E22 b |
| T9 R1.3 | a template clone keeping the template's remote | witnesses, remote | H+R | SILENT: the template's project until `project init --new` | E22 c |
| T10 R1.3 | a template clone re-pointed to its own remote | witnesses | H | ASK ONCE; re-pointed after first sight: L5 | E22 c |
| T10a | GitHub's "Use this template" or a cookiecutter product: the tree with the committed project file, a fresh root commit, its own remote | the file id | F (the template's files do not count without its remote) | ASK ONCE, L1 offering `project init --new`; answered, its own project, its clones SILENT; the template keeps its own | E22 e |
| T11 | a host migration (every URL replaced), no file | witnesses, old keys | H; O with L5 | ASK ONCE per store, then SILENT | E24 a, a', b, c |
| T12 | a host migration with the file | file id | F+H | SILENT | E24 d |
| T13 | CI runners, a shallow clone at a new tip each run, no file | remote, then a declared remote | R; D+R | ASK ONCE per store, then SILENT | E32 e, f |
| T14 | CI runners with the file; full clones | file id; witnesses | F+R; H+R | SILENT | E32 c, d |
| T15 | Codespaces or any cloud clone with the store shared or imported | witnesses, remote, file | H+R, F | SILENT | argued: a fresh clone on another machine (E05, E24, E27) |
| T16 | records imported from a teammate's store | file id, witnesses, remotes | F; H+R; one | SILENT; SILENT; ASK ONCE, L10 | E27 |
| T17 | a shared bare repository on a file server, cloned by path | views, witnesses | H+R | SILENT | E31 d |

### 7.3 Enterprise

| # | Case | The store knows | Witnesses | Outcome | Receipt |
|---|---|---|---|---|---|
| X1 | a consultant with client A's and B's repositories, no files | separate evidence | none shared | NEW each; nothing of A in B but an explicit unscoped search | E34 a, b |
| X2 | the same with `boundary = true` in each client's folder | tenants | dropped at the boundary | nothing of A in B on any surface, unscoped search included | E34 d |
| X3 | one open-source library cloned under both clients | witnesses, remote | H+R | one project without boundaries (D1), two with them | E34 e |
| X4 | a note written in a client's folder outside any repository | workspace file | F | that client's repositories only, never global | E34 f |
| X5 | a monorepo with nested project files | file ids | F chain | SILENT; a package gets its own and the root's, never a sibling's | E35 a to c |
| X6 | a nested file whose id belongs to another repository | a binding elsewhere | contradiction | ASK ONCE, L7; `project join` or `project init --new` | E35 e to g |
| X7 | a policy against writes into repositories | no token | A2, C, H, R, F | as the rows above, the line saying skipped:policy | E15 f, g, E17, E20 d, E33 |
| X8 | read-only checkouts; NFS (no birth time; exports read-only or another account's) | object, token, stamps | O with T or A2; H+R | SILENT; no token where it cannot be written | E20 a to c, E15; NFS argued, CI leg |
| X9 | SMB (renumbered objects, no hard links) | path, commits, token | B+H+T | SILENT, re-keyed; the token by exclusive create | E11 c; SMB argued, CI leg |
| X10 | Windows | NTFS file id, volume serial, creation time | O, T, H | as on macOS | argued, CI leg windows-latest |
| X11 | 10,000 checkouts, 64,000 witnesses, 1,000,000 records | indexes | none needed | one lookup; one batch at first sight; one indexed join per brief | E29 |
| X12 | quiet checkouts | a retired flag no rule reads | none needed | out of health; seen again, resolved as before | E29 d |
| X13 | an auditor asks why a record is where it is | the log | none needed | `bettermemory project why <id>`: project, checkout, rule, rows | E37 d, e |
| X14 | every uncertain call | rows | none needed | rows, health counts, `bettermemory project pending` | E13, E36 c3 |
| X15 | Linux workstations, servers, WSL distributions | the object with birth by statx where the filesystem reports it; else the token, the last HEAD, the init stamps | O; O with T or A2 | as on macOS; an inode number reused without birth is refuted | E15 (the no-birth path), E10 f (statx in a Linux container, real), CI leg ubuntu-latest (the suite already runs there) |

## 8. Every level

### 8.1 Capture

- **Hooks** (session-start, prompt, stop through the daemon on port 7397): the hook client, standard library only, posts the location token, including its own held-witness check where the daemon cannot read the path (E10 a, e). It writes the checkout token at first sight in its own namespace (the daemon may not see the path).
- **MCP writes** (memory_write, episode writes, memory_verify) capture the declared workspace and write the record, its home row and the HEAD witness in one transaction. **CLI**: the same capture.
- **`origin.capture()`** is extended with the objects, every remote key, the token, the init stamps, the project files and the evicted-file check; githead gains the object reader. The origin dict keeps being written.

### 8.2 Membership sites

Round 1's list (its section 8.2) stands, each through `membership.admitted(surface, home, caller)`: recall.py `_select_brief`, `prompt_recall`, `in_repository`; search.py `candidate_admitted` for handlers/search.py `memory_search` and `resolve_search_pool`, hook.py `_probe_message`, review.py `related_for`, handlers/write.py `memory_write` and `_reviewed_write`; store.py `brief_rows`, `scope_counts`, `category_rows`, `document_frequencies`, `_read_doc_freq_inputs`; audit.py `probe_for_miss`, `_caller_in_top_hit_project`, `_count_recent_retrievals`; recall_review.py `_project_test`; _response.py `lookup`, `attach_depends_on_resolved`, `attach_link_annotations`. New: the search with auto_scope off checks `other_tenant` (E34 d). A guard test fails when any module decides membership outside `membership.py`.

### 8.3 Anchoring sites

Unchanged from round 1 (its section 8.3).

### 8.4 Episodes and events

As in round 1 (its section 8.4): episodes are homed to checkouts, the handoff selects by checkout, the event recorder stamps the checkout id beside worktree_root, and session joins key on the checkout id. Events also carry the machine id, so a synced store's sessions stay per machine (E36 a).

### 8.5 The store

- **Tables**: `machines` (id, facts, predecessor); `projects` (id, name, merged_into, provisional reason, parent, tenant, folder, boundary); `project_witnesses` (project, commit, arrival order, source, fingerprint; at most 64 per project); `project_blobs` (project, blob, witness; at most 8 per witness); `project_remotes`; `project_files` (file id, project, evidence); `declared_remotes` (remote key, project, principal); `checkouts` (id, project, machine, kind, object key, common object key, token, token status, init stamps, last HEAD, branch, content manifest, legacy, retired); `checkout_views`; `homes` (record id, kind, project, checkout, rule, decided at); `proposals` (id, checkout, kind, candidates, evidence, reason, line, state, answer, answered by, signature).
- **The log**: every change is a row of its own mutation kind, MAC'd and chained like every row and folded by `refold`, so `bettermemory log verify` covers membership: a planted or edited home, binding or answer reads as unaccounted.
- **Records**: put_memory and put_episode write records as given and the home row joins the same transaction; Store.update never touches homes; the origin columns keep being written and are read by the migration, by a rollback and by the shadow column of section 8.11.
- **Indexes**: homes by project (the brief's join), projects by merged_into, checkouts by (machine, device, inode), by common object, by token, witnesses by commit, fingerprints, remote keys, file ids. E29 (e): the brief's selection over 1,000,000 records in 10,000 projects is one indexed join (0.028 ms per selection in an in-memory SQLite here); a merge is one project row, never a rewrite of records.
- **Schema**: additive tables and kinds, schema_version unchanged (D9).

### 8.6 Migration

Runs once when the upgraded daemon or CLI first opens the store, and on demand as `bettermemory membership migrate --dry-run`, which prints E17's report. Idempotent, logged (`membership_migrated` with its counts), zero manual steps, and it writes no token and no file into any repository (the probe reads; tokens are written at each checkout's next sighting, in the caller's namespace). The steps are round 1's (its section 8.6): live roots become checkouts; a gone root becomes a prior view of a live checkout when the log shows its records re-homed there, else a checkout of the one project holding its records' verified heads, else of the one holding its cited commits under the same folder name, else of the one carrying its remote; the rest are projects with no live checkout. New in round 2: each of those whose recorded commits a live checkout partly holds becomes a durable `legacy` proposal in that checkout (L6), so the question is asked where the answer can be given; a folder of theirs that returns is a first sight like any other and joins their project by its own evidence.

A dry run on a copy of the owner's store (E17) rewrote no memory or episode row, homed every memory and episode, and wrote no token into any repository.

### 8.7 Environments

- **Several machines, sync, export and import.** Checkouts, views and events carry the machine id. A project's portable identity is its file ids, else the oldest 8 and newest 8 witness commits and its remote keys; no local id (P001, K003) and no path travel. An export carries the source store's id, minted with the store, and at sync and import a machine is keyed by (machine id, store id). An import maps by F (silent), by H with R (silent), by one of them (an `import` proposal, L10), or by none (a project with no live checkout that a later checkout joins by the same evidence) (E27). A hosted or synced store uses the same identity.
- **Machine clones and migrations.** The machine-clone check (section 2.1) runs before any resolution; checkouts never re-key across machines; a migrated laptop's checkouts join by T+H (E26). A block-level clone with every fact equal (a cloud image booted twice, the store inside it) keeps one machine id and is harmless locally; the first import or sync that brings a record carrying the importer's store id, which it never wrote, is a fork, and the importer takes a new store id (`store_forked`), so two stores carrying one machine id never merge their checkouts (E26 e).
- **Containers and devcontainers.** A container is a machine of its own. Ran for real in OrbStack (docker 29.8.2, `--network none`, E10): a bind-mounted repository is seen through virtiofs with another device and inode, so it is a second checkout of the project joined by T+H+R from the location token; a clone inside joins by H+R; throwaway containers with the committed file by F+R; a copy at a path the host cannot see by T+H from the token captured inside. The host never reads the container's paths: the container's hook client sends the location token and checks the witness commits the daemon names for it (the daemon sends the candidates' bounded sets: at 64,000 witnesses about 1.3 MB, once per new checkout). On docker under Linux a bind mount keeps the device and inode, and the container is still a machine of its own, so the outcome is the same (a checkout per container machine, T+H); argued, CI leg: an ubuntu-latest job with docker runs E10 there.
- **Linux.** The capture reads birth time through statx (STATX_BTIME, called through ctypes, so glibc and musl alike) where the filesystem reports it; inside the Linux container the overlay filesystem reported it and the virtiofs bind mount did not (E10 f, real). Without birth, O is confirmed by the token, else the last HEAD held, else the init stamps (A2). ext4 and xfs hand freed inode numbers to new files quickly, so that path carries Linux fleets (E15 runs it with birth switched off). The CI matrix's ubuntu-latest entries already run the suite, so the Linux object reads are a run, not an argument.
- **WSL.** A distribution runs the Linux capture; Windows and the distribution are two machines.
- **Codespaces and cloud agents.** A fresh clone on another machine (T15).
- **Windows.** The object is the volume serial number and the NTFS file index (FILE_ID_INFO's 128-bit id on ReFS) with the creation time as birth; the token is written to a temporary file and hard-linked into place with CreateHardLinkW, or with CREATE_NEW where links are refused; the object reader reads the same files; githead declines on Windows, so the capture runs git on a cache miss as rc3 does. Argued; CI leg: the windows-latest entry already in `.github/workflows/ci.yml` runs the experiments as tests.
- **CI.** A full clone joins by H+R, a shallow one by F+R or, without the file, once per store and then D+R (E32). Runners with a store of their own have nothing to deliver.
- **Ephemeral agent worktrees.** Linked worktrees: S (E09, E19 e).
- **Monorepos, sparse checkouts, submodules.** Nested project files make sub-projects (E35); without them one repository is one project (E18 d).
- **Read-only and network checkouts.** NFS: no birth time on v3, so O is confirmed by the token or A2; a read-only export or one another account owns gets no token (failed or skipped:foreign_owner) and resolves by H+R or F. SMB: a remount can renumber objects (B+H+T re-keys, E11 c); a server without hard links gets the exclusive create, and a reader that finds the token empty reads again. Argued; CI legs: an ubuntu-latest job exporting a loopback directory over NFSv3 and another mounting a loopback Samba share run E11, E15, E19, E20 and E33 there.
- **Synced folders** (iCloud Drive, Dropbox, OneDrive). Each machine has its own checkout of one project (E36 a, b); an evicted HEAD or config is fetched within the budget or deferred one prompt (E36 c, the flag simulated). Argued for real eviction: `brctl evict` on a scratch repository in iCloud Drive, on the owner's word.
- **Multi-user machines.** One store per OS user; another user's repository is read from its files and gets no token.
- **Tenants.** A `boundary = true` file makes a folder a tenant: no witness crosses it, and no surface delivers across it, the unscoped search included (E34).

### 8.8 Security

Membership is a delivery policy, not the boundary around the store: any process running as the owner reads the store and the daemon's token. The threat is inadvertent or induced delivery: a repository that arrives (a download, a clone, an archive, an agent's creation) receiving another project's memories, writing into another project, or claiming a project's identity.

- **Receive.** Two independent witnesses or a declaration. The remote alone, the token alone against a project with commits, a file alone, a file over another project's history: ASK ONCE and nothing delivered (E07, E12 a, E21). A1 is folded: a remote-less repository holding a project's commits has one witness and asks (E12 d).
- **Write into another project.** Writes in a provisional project stay there until answered (E12 a, E21).
- **Plant identity.** The token is 130 random bits in the git directory; a planted copy is a contradiction while its carrier lives (E21 a3). A committed file names a project only with its history or, where history cannot speak, its remote.
- **Tenants.** Nothing crosses a boundary (E34 d).
- **Declare.** CLI only, never an MCP tool; with A4 folded (decision D6): the commands refuse without a terminal and ask for the project's name to be typed, and the install adds a deny rule for `bettermemory project` and `bettermemory memory move` to the Claude Code permissions, enforced by the harness before anything runs. (RETIRED by 9.0.0rc6; see the status note.)
- **Store edits.** Membership rows are MAC'd log rows, folded and verified.
- **git itself.** No hooks, no fsmonitor, `GIT_OPTIONAL_LOCKS=0`; the object reader runs no repository configuration; safe.directory is never set.
- **rc3 exposures closed**: E12 b, c, h (as in round 1).

### 8.9 Performance

- Per prompt at the membership step (E16, counted): steady state 0 git spawns and 12 stat calls (rc3: 0 and 20; round 1's design: 10, the two added are the token and the in-repository file walk); first prompt after a cache clear 3 spawns and 43 stats (rc3: 4 and 40); first sight of a copy 3 and 60.
- At scale (E29, measured here on the prototype): a known checkout among 10,000 is one index lookup (0.070 ms); first sight over 64,003 witnesses is one batch, 0 spawns and 188.0 ms with the object reader against 1 spawn and 1572.1 ms through `git cat-file --batch-check`; 301 sightings keep 64 witnesses (237 released); the brief over 1,000,000 records is one indexed join.
- The prompt arm of `bench/daemon/latency.py` was not run: the owner's rule asks for his yes before a bench run. Prediction: p50 within 2 ms of rc3 at known checkouts.

### 8.10 Observability

- **Rows**: resolution_indeterminate, resolution_deferred, resolution_joined, resolution_new, proposal_opened, proposal_answered (with the principal, or the evidence), checkout_registered, checkout_moved, checkout_view_added, checkout_rekeyed, checkout_retired, token_superseded, token_reminted, remote_declared, project_minted, project_merged, project_file_bound, machine_cloned, machine_facts_updated, witness_put, witness_released, import_homed, recall_suppressed, membership_migrated.
- **Lines**: L1 to L10 in the session's first output; an open proposal is named in every session there until answered.
- **Health**: open proposals with their lines, provisional projects, suppressed recall turns per checkout, indeterminate and deferred captures, checkouts with no live view (retired ones excluded).
- **CLI**: `bettermemory project list | show | why <id> | pending | join <id> | new | init [--new | --rebind]`, `bettermemory memory move <id> --project`, `bettermemory membership migrate [--dry-run]`, `bettermemory membership export | import`. `why` prints a record's project, checkout, rule and every row that decided it (E37 d).

### 8.11 Measurement continuity

RS-F5 (committed on rs-f1 as 06c81f86 and 7032be0c, RULE rs-v4, never released) and RS-F6 ship as one release, 9.0.0rc4, with RULE rs-v5. rs-v4 exists only in the suite; live rows go from rs-v3 to rs-v5. Every read splits on the row's rule, never on a clock. Under rs-v5 each brief and recall delivery row carries the membership rule and `rc3_admitted` (rc3's `in_project` answer computed from the origin columns, which keep being written), so RS-F5's predictions read on the rs-v5 rows where membership agrees with rc3's, and RS-F6's on the rows where it differs. ENG-7 grading samples deliveries per rule. Decision D7 is moot.

### 8.12 Docs, tests, rollback

- **Docs**: docs/internals.md (a membership section: witnesses, the ladder, the token, the project file, proposals); docs/api.md (the CLI, the file format, the policy switch, the auto-scope wording); docs/eval.md (rs-v5, the rows, the shadow column); CHANGELOG.md (9.0.0rc4: RS-F5 and RS-F6).
- **Tests**: every experiment becomes a test named by its section 7 row (for example `test_I4_r1_1_commitless_moved_token_alone`), the guard test, the refold test, and the CI legs of section 8.7.
- **Rollback**: reinstall the previous build. The origin columns are as they were and rc3 ignores the added tables and kinds, so nothing in the store needs undoing. Tokens in `info/` are inert for rc3 and for git; a committed `bettermemory.toml` is inert for rc3 and the user's to keep or delete. Re-upgrading re-runs the idempotent migration, which homes what the previous build wrote meanwhile.

## Decisions D1 to D19

The decisions D1 to D19 (round 2), ruled 2026-10-21: each settled on its recommended option A.

**D1. Do the checkouts of one project share its memories?** A, share: copies, clones, worktrees, containers and other machines see the project's memories; episodes, the handoff and recall segments stay per checkout (E03, E04, E09, E10, E36). B, rc3's isolation while the sibling is live. Why A: B hides memories on an ordinary copy; the token now makes a copy recognisable as one (E03, E37 b). `project new` separates any checkout.

**D2. A repository holding a project's history only under another remote, or under none** (a fork without its upstream, a re-pointed template, an unpacked archive). Folds A1. A, one witness, ASK ONCE: nothing delivered until answered (E12 d, e, E22 c). A remote-less repository holding a project's commits is the same case (A1). B, joined by history. Why A: history alone is what a fork, a template product and a stranger's archive share; a fork naming its upstream, or carrying the committed project file, joins silently (E12 d, E21 e).

**D3. Search for a caller outside any project.** A, the auto-scoped search admits global records only; a search with auto_scope off reaches every project except across a boundary (E12 c, E34 a, d). B, rc3: every project's memories in the auto-scoped search.

**D4. Root-less legacy records whose cwd lies inside a recorded root.** A, home them to that checkout's project (E17). B, only the one captured with a source field. C, none.

**D5. Two legacy projects of the owner's store with no live checkout.** A, each a project with no live checkout and a durable `legacy` proposal in the candidate checkout (line L6), named in every session there until answered by `project join` or `project new` (E17 V1 and V2). B, join one to the other on its cited commits (the migration refuses: cited commits can name another project's work). C, archive them. (Since 9.0.0rc6 a legacy question is opened only where the evidence decides something; the rest are closed as separate at the daemon's start.)

**D6. Who can declare** (join, new, init, memory move). Folds A4. A, the CLI only, with a terminal required, the project's name typed to confirm, and a deny rule for `bettermemory project` and `bettermemory memory move` in the Claude Code permissions added at install. B, the CLI only, as round 1. C, MCP tools behind an acknowledgment flag. (RETIRED by 9.0.0rc6: nothing in the product may require a human at a terminal; the agent answers membership questions through `memory_admin`, a join tethered to the user's words; the installer removes the deny rules.)

**D7. When it lands.** Moot. RS-F5 and RS-F6 ship together as 9.0.0rc4 with RULE rs-v5; reads split on the row's rule, and the shadow column `rc3_admitted` separates RS-F5's measurement from RS-F6's change (design 8.11). No date enters the plan.

**D8. The two-witness rule against A3.** A, two independent agreeing witnesses join silently; `membership.strict = true` (A3) is available as a switch. B, A3 by default: every join resting on history asks; joins by the project file, a declaration, the object or the object store stay silent (E21 d, e). Why A: B asks where the observables agree (a move across filesystems carries the token and the history), and the case A3 targets, a repository with the project's commits and its remote that is not a clone, is a clone by every observable and needs a process on the machine, which could read the store itself. The switch serves an owner who wants B.

**D9. Rollback form.** A, additive tables and kinds, schema_version unchanged. B, schema_version 2 with a reset command.

**D10. The token's name and format.** A, `info/bettermemory` in the git directory, two lines: `bettermemory checkout token v1` and 26 Crockford base-32 characters (E19 a). B, a key in `.git/config`: git rewrites config under its own lock, `git config -l` and includes expose it, and tools edit it. C, an extended attribute on `.git`: many copy tools and filesystems drop it, so moves lose it. Why A: `info/` is git's place for local, never-transferred files (exclude, attributes); a copy carries it and a clone does not.

**D11. When the token is written.** A, at the first sight of every checkout: a born remote-less checkout moved across filesystems joins by T+H (E02, E11). B, only for commit-less checkouts: fewer files, but the born case above rests on history alone and asks.

**D12. The project file's name, format and placement.** A, `bettermemory.toml` (`[project]`, `id = "bmp_<26>"`, `name`, optional `boundary = true`), at the repository root, written only by `bettermemory project init` and committed by the user (E22 b, E24 d, E32 d, E35). B, a dotfile (`.bettermemory`): hidden from the reviewers who should see a project binding. C, a key in pyproject.toml or package.json: tied to one language.

**D13. Nested project files.** A, sub-projects: a session gets its own and its parents' memories, never a sibling's; the root's search reaches the sub-projects (E35, E23 g). B, separate projects: nothing shared with the root. C, nested files ignored: one project per repository (E18 d).

**D14. Proposal commands.** A, `bettermemory project pending | join <id> | new | init [--new | --rebind]`, and answers by evidence (a commit, a fetch, a committed file) (E19 c, E32 b, E35 f, g). Where the root's project file is bound, the line's command for "separate" is `project init --new`, so a "Use this template" or cookiecutter product gets a file id of its own (E22 e). B, an MCP tool to answer: refused by D6. C, proposals expiring after some sessions: a silent decision. (Since 9.0.0rc6 the agent answers through `memory_admin` as well: answer_question, join_project, unjoin_project.)

**D15. The witness bound and the retirement period.** A, 64 witness commits per project (the 16 oldest kept, the newest 48 rolling), 8 blobs per witness; a checkout unseen for 180 days is retired from health, and no rule reads the flag (E29 c, d). B, unbounded: first-sight cost grows with every commit seen. C, 16: a clone of an old branch finds fewer witnesses.

**D16. The machine-clone fact.** A, the hardware or machine id, the hostname and the id file's object, compared at every open (E26 a, b). Where every fact is equal (a cloud image cloned block for block and booted twice, the store inside it), a machine is keyed by (machine id, store id) at sync and import, and an import that brings a record with the importer's own store id which it never wrote is a fork: the importer takes a new store id (E26 e). B, the hostname alone: a rename reads as a clone. C, none: a cloned VM's checkout is taken for the original's and handed its run-state (E26 c shows it).

**D17. Tenancy.** A, `boundary = true` in a folder's project file: no witness crosses it and no surface delivers across it, the unscoped search included (E34 d, e). B, the boundary on the automatic surfaces only: an explicit search would still reach another client.

**D18. A declared remote for shallow clones.** A, answering `join` to a remote-only question in a shallow clone declares that remote the project's for shallow clones (witness D) (E32 e, f). B, none: each CI run at a new tip asks unless the file is committed.

**D19. Evicted files in synced folders.** A, fetch within the capture's budget, else defer one prompt with line L9; a known checkout reads nothing (E36 c1 to c3, the flag simulated). B, read synchronously: the hook waits on the provider, and offline it hangs. C, indeterminate until someone acts.

**The amendments, folded.** A1: D2. A2: the init stamps compared by equality, no clock (E15 f, g, E33). A3: D8. A4: D6. A5: the CI legs of design section 8.7; the container run is real (E10 in OrbStack).

## Build decisions B1 to B24

The build decisions B1 to B24 (the RS-F6 build, 2026-10-21, within the rulings).

B1 a malformed project file above a root fails closed as a boundary with a line; B2 a file without name takes the directory name; B3 stale token temp files swept after an hour; B4 a cwd outside the work tree walks from the root; B5 the machine id in machine.py; B6 a write where nothing could be read is homed in a provisional project and settled by session; B7 phase 1 deviations accepted; B8 checkouts.unresolved_json; B9 boundary honoured at every placement; B10 moving declarations carry the checkout's parts, shared-file parts ask L7; B11 project new refuses on a bound root file and names init --new; B12 a flag added applies, a flag removed lifts only by init --rebind; B13 project_reparented row; B14 root-less episodes homed by the cwd rule, removed memories homed, one legacy question per project; B15 a superseded token re-minted at the next sighting; B16 writes without a machine id settle by session across machines; B17 init, init --new and init --rebind below a root act on the folder (a part), never a split; B18 liveness applies the whole of witness O (token, last HEAD held, init stamps); B19 abbreviated cited commits resolve to full ids (ambiguous or absent are not held); B20 no re-key row for the same device and inode without birth time; B21 prompt_recall = "everywhere" delivers under the unscoped search's admission; B22 the handoff's notes come from the checkout's own rows; B23 the verification path compares no remotes (a record anchors at its home or nowhere); B24 a write at a path the daemon cannot read is homed where the same session's hook resolution stands (resolution_session row).
