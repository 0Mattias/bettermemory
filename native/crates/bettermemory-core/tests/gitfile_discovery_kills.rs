//! Inputs a plausible wrong reading of `gitfile_rule.py`'s judge and
//! discovery (1295-1537) answers otherwise than the reference. Each
//! assertion is the reference's answer (CPython 3.13.13) and the comment
//! names the wrong reading it kills. The trees are in memory, so every
//! platform holds them; those that can exist on disk are also cases of
//! `gitfile_discovery.jsonl`, and the rest (a `.git` at the file system's
//! root, a placeholder, an entry given another entry's `lstat`) follow from
//! the reference's text.

use bettermemory_core::fsdata::{samestat, Flavour, Fs, MemFs, Node, StatResult, SF_DATALESS};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::{
    discovery_reading, discovery_refusal, entry_reading, judge, reading_at, toplevel_agrees, Entry,
    Reading, Rule,
};
use bettermemory_core::gitfile_rule::follow::{NoRecords, Records};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::gitfile_rule::trust::Refusal;

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};
const V0: &str = "[core]\n\trepositoryformatversion = 0\n";
const HEAD: &str = "ref: refs/heads/main\n";

fn file(fs: &mut MemFs, path: &str, text: &str) {
    fs.insert(path.as_bytes(), Node::File(text.as_bytes().to_vec()));
}

fn link(fs: &mut MemFs, path: &str, to: &str) {
    fs.insert(path.as_bytes(), Node::Link(to.as_bytes().to_vec()));
}

fn dir(fs: &mut MemFs, path: &str) {
    fs.insert(path.as_bytes(), Node::Dir);
}

fn repo(fs: &mut MemFs, at: &str, config: &str) {
    file(fs, &format!("{at}/HEAD"), HEAD);
    dir(fs, &format!("{at}/objects"));
    dir(fs, &format!("{at}/refs"));
    file(fs, &format!("{at}/config"), config);
}

/// A world asked under git 2.54: by default from `/`, with no record and
/// no `GIT_COMMON_DIR`.
struct World<'a> {
    fs: &'a MemFs,
    cwd: &'a str,
    records: &'a dyn Records,
    env: Option<&'a str>,
}

fn at(fs: &MemFs) -> World<'_> {
    World {
        fs,
        cwd: "/",
        records: &NoRecords,
        env: None,
    }
}

type Judged = Result<Option<Entry>, Failure>;
type Read = Result<Option<Reading>, Failure>;

impl World<'_> {
    fn with<T>(&self, ask: impl FnOnce(&Rule<'_, MemFs, Hung, dyn Records + '_>) -> T) -> T {
        let ctx = Ctx {
            fs: self.fs,
            flavour: Flavour::Posix,
            cwd: self.cwd.as_bytes(),
        };
        ask(&Rule {
            ctx: &ctx,
            answers: &Hung::default(),
            records: self.records,
            env: self.env.map(str::as_bytes),
            git: &GIT,
        })
    }

    fn lstat(&self, path: &str) -> StatResult {
        let full = if path.starts_with('/') {
            path.to_owned()
        } else {
            format!("{}/{path}", self.cwd)
        };
        self.fs.lstat_result(full.as_bytes()).expect("the entry")
    }

    /// `judge(directory, dotgit, os.lstat(dotgit))`.
    fn judge(&self, directory: &str, dotgit: &str) -> Judged {
        self.judge_as(directory, dotgit, &self.lstat(dotgit), true, true)
    }

    fn judge_as(
        &self,
        directory: &str,
        dotgit: &str,
        st: &StatResult,
        fetch: bool,
        read: bool,
    ) -> Judged {
        self.with(|r| judge(r, directory.as_bytes(), dotgit.as_bytes(), st, fetch, read))
    }

    fn entry(&self, directory: &str, dotgit: &str) -> Read {
        let st = self.lstat(dotgit);
        self.with(|r| entry_reading(r, directory.as_bytes(), dotgit.as_bytes(), &st))
    }

    fn reading(&self, directory: &str) -> Read {
        self.with(|r| reading_at(r, directory.as_bytes()))
    }

    fn walk(&self, start: &str) -> Read {
        self.with(|r| discovery_reading(r, start.as_bytes()))
    }

    fn refusal(&self, start: &str) -> Result<Option<Refusal>, Failure> {
        self.with(|r| discovery_refusal(r, start.as_bytes()))
    }

    fn agrees(&self, start: &str, toplevel: &str) -> Result<bool, Failure> {
        self.with(|r| toplevel_agrees(r, start.as_bytes(), toplevel.as_bytes()))
    }
}

fn why(text: &str) -> Option<Refusal> {
    Some(Refusal {
        why: text.as_bytes().to_vec(),
        format_unknown: false,
    })
}

fn unknown(text: &str) -> Option<Refusal> {
    Some(Refusal {
        why: text.as_bytes().to_vec(),
        format_unknown: true,
    })
}

fn entry(git_dir: &str, why: Option<Refusal>, own: bool, provisional: bool) -> Judged {
    Ok(Some(Entry {
        git_dir: git_dir.as_bytes().to_vec(),
        why,
        own,
        provisional,
    }))
}

fn followed(root: &str) -> Read {
    Ok(Some(Reading {
        why: None,
        provisional: false,
        root: Some(root.as_bytes().to_vec()),
    }))
}

fn provisional(root: &str) -> Read {
    Ok(Some(Reading {
        why: None,
        provisional: true,
        root: Some(root.as_bytes().to_vec()),
    }))
}

fn refused(why: Option<Refusal>) -> Read {
    Ok(Some(Reading {
        why,
        provisional: false,
        root: None,
    }))
}

/// A repository with no root: bare, or a linked worktree's git directory.
const NO_ROOT: Read = Ok(Some(Reading {
    why: None,
    provisional: false,
    root: None,
}));

#[test]
fn a_link_is_judged_by_its_owner_before_its_kind() {
    let mut fs = MemFs::new(Flavour::Posix);
    file(&mut fs, "/ft/f", "");
    fs.insert(b"/ft/.git", Node::Fifo);
    link(&mut fs, "/lft/.git", "/ft/.git");
    // A tree holding its `.git` alone is no working tree: no owner.
    fs.insert(b"/solo/.git", Node::Fifo);
    link(&mut fs, "/lsolo/.git", "/solo/.git");
    let w = at(&fs);
    // Testing the kind first (a FIFO names nothing) answers None.
    let links = why("/lft/.git links to the .git of /ft");
    assert_eq!(
        w.judge("/lft", "/lft/.git"),
        entry("/ft/.git", links.clone(), false, false)
    );
    assert_eq!(w.reading("/lft"), refused(links.clone()));
    assert_eq!(w.walk("/lft"), refused(links));
    assert_eq!(w.judge("/lsolo", "/lsolo/.git"), Ok(None));
    assert_eq!(w.reading("/lsolo"), Ok(None));
}

#[test]
fn what_git_reads_comes_before_the_owner() {
    let mut fs = MemFs::new(Flavour::Posix);
    dir(&mut fs, "/dieown/.git/objects");
    dir(&mut fs, "/dieown/.git/refs");
    fs.insert(b"/dieown/.git/HEAD", Node::Fifo);
    file(&mut fs, "/dieown/f", "");
    link(&mut fs, "/ldie/.git", "/dieown/.git");
    file(&mut fs, "/emptygit/.git/keep", "");
    file(&mut fs, "/emptygit/f", "");
    link(&mut fs, "/lempty/.git", "/emptygit/.git");
    let w = at(&fs);
    // The owner first would say "links to the .git of /dieown". The reason
    // names the entry, the git directory is the one it reaches.
    assert_eq!(
        w.judge("/ldie", "/ldie/.git"),
        entry(
            "/dieown/.git",
            why("git cannot read /ldie/.git as a git directory"),
            false,
            false
        )
    );
    // A link to another tree's `.git` that is no git directory is passed
    // over, not refused for its owner.
    assert_eq!(w.judge("/lempty", "/lempty/.git"), Ok(None));
    assert_eq!(w.entry("/lempty", "/lempty/.git"), Ok(None));
    assert_eq!(w.reading("/lempty"), Ok(None));
}

#[test]
fn the_formats_refusal_comes_before_the_commondirs() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/plain/.git", V0);
    repo(
        &mut fs,
        "/v2/.git",
        "[core]\n\trepositoryformatversion = 2\n",
    );
    file(&mut fs, "/fv2/.git/HEAD", HEAD);
    file(&mut fs, "/fv2/.git/commondir", "/v2/.git\n");
    file(&mut fs, "/foreign/.git/HEAD", HEAD);
    file(&mut fs, "/foreign/.git/commondir", "/plain/.git\n");
    let w = at(&fs);
    // The commondir's test first would say "names the common directory".
    let format = unknown(
        "/fv2/.git's config cannot be read as git reads it: /v2/.git/config sets a \
         repository format version git does not read",
    );
    assert_eq!(
        w.judge("/fv2", "/fv2/.git"),
        entry("/fv2/.git", format.clone(), true, false)
    );
    assert_eq!(w.reading("/fv2"), refused(format));
    // `judge` alone follows an own `.git` whose commondir is foreign: the
    // test is `_entry_reading`'s, and the folder's own (`reading_at`).
    assert_eq!(
        w.judge("/foreign", "/foreign/.git"),
        entry("/foreign/.git", None, true, false)
    );
    let foreign =
        why("/foreign/.git names the common directory /plain/.git but is not one of its worktrees");
    assert_eq!(
        w.entry("/foreign", "/foreign/.git"),
        refused(foreign.clone())
    );
    assert_eq!(w.reading("/foreign"), refused(foreign));
    assert_eq!(
        w.reading("/foreign/.git"),
        refused(why(
            "/foreign/.git names the common directory /plain/.git but is not one of its worktrees"
        ))
    );
}

#[test]
fn a_head_that_is_a_link_to_nothing_exists() {
    let mut fs = MemFs::new(Flavour::Posix);
    dir(&mut fs, "/old.git/objects");
    dir(&mut fs, "/old.git/refs");
    link(&mut fs, "/old.git/HEAD", "refs/heads/main");
    dir(&mut fs, "/oldt/.git/objects");
    dir(&mut fs, "/oldt/.git/refs");
    link(&mut fs, "/oldt/.git/HEAD", "refs/heads/main");
    file(&mut fs, "/oldt/f", "");
    let w = at(&fs);
    // `exists` (a `stat`) in place of `lexists` goes on to the folder above.
    assert_eq!(w.reading("/old.git"), NO_ROOT);
    assert_eq!(w.walk("/old.git/objects"), NO_ROOT);
    assert_eq!(w.reading("/oldt/.git"), followed("/oldt"));
    assert_eq!(w.walk("/oldt/.git/refs"), followed("/oldt"));
}

#[test]
fn the_walk_reads_the_root_itself_and_ends_there() {
    let mut fs = MemFs::new(Flavour::Posix);
    file(&mut fs, "/x/y/f", "");
    // Nothing anywhere: the walk ends at the root (`dirname` of it is it).
    assert_eq!(at(&fs).walk("/x/y"), Ok(None));
    assert_eq!(at(&fs).walk("/"), Ok(None));
    assert_eq!(at(&fs).agrees("/x/y", "/"), Ok(false));
    repo(&mut fs, "/.git", V0);
    let w = at(&fs);
    // A loop that stops before reading the root misses its `.git`.
    assert_eq!(w.walk("/x/y"), followed("/"));
    assert_eq!(w.walk("/x/y/f"), followed("/"));
    assert_eq!(w.walk("/gone/deeper"), followed("/"));
    assert_eq!(w.agrees("/x/y", "/"), Ok(true));
    assert_eq!(w.agrees("/x/y", "/x"), Ok(false));
    assert_eq!(w.judge("/", "/.git"), entry("/.git", None, true, false));
    assert_eq!(w.reading("/"), followed("/"));
    // `realpath` cuts two leading separators to one; a folder asked
    // directly keeps its own spelling (`join("//", ".git")` is `//.git`).
    assert_eq!(w.walk("//x/y"), followed("/"));
    assert_eq!(w.reading("//"), followed("//"));
    // From inside the root's `.git` the walk stops at the git directory.
    assert_eq!(w.walk("/.git/objects"), followed("/"));
}

#[test]
fn the_start_is_resolved_and_a_folder_asked_is_not() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/own/.git", V0);
    file(&mut fs, "/own/sub/f", "");
    link(&mut fs, "/lown", "/own");
    let w = at(&fs);
    // The root of a walk is the resolved folder; of one folder, the folder
    // as given. Resolving both, or neither, fails one of these.
    assert_eq!(w.walk("/lown/sub"), followed("/own"));
    assert_eq!(w.walk("/lown"), followed("/own"));
    assert_eq!(w.reading("/lown"), followed("/lown"));
    assert_eq!(
        w.judge("/lown", "/lown/.git"),
        entry("/lown/.git", None, true, false)
    );
    assert_eq!(w.walk("/own/sub/../sub/./"), followed("/own"));
}

#[test]
fn the_walk_stops_at_the_first_folder_git_stops_at() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/plain/.git", V0);
    file(&mut fs, "/plain/inner/.git", "junk\n");
    file(&mut fs, "/plain/inner/in/f", "");
    file(&mut fs, "/plain/skipme/.git/keep", "");
    file(&mut fs, "/plain/skipme/in/f", "");
    let w = at(&fs);
    let junk = why("/plain/inner/.git names no git directory membership follows");
    // Walking on past a refusal reaches the repository above and follows.
    assert_eq!(w.walk("/plain/inner/in"), refused(junk.clone()));
    assert_eq!(w.refusal("/plain/inner/in"), Ok(junk));
    assert_eq!(w.agrees("/plain/inner/in", "/plain"), Ok(false));
    // A `.git` directory git passes over does not stop it.
    assert_eq!(w.walk("/plain/skipme/in"), followed("/plain"));
    assert_eq!(w.refusal("/plain/skipme/in"), Ok(None));
    assert_eq!(w.agrees("/plain/skipme/in", "/plain"), Ok(true));
}

#[test]
fn what_judge_passes_over_is_a_refusal_only_for_a_file() {
    let mut fs = MemFs::new(Flavour::Posix);
    file(&mut fs, "/junk/.git", "junk\n");
    file(&mut fs, "/junkfile", "junk\n");
    link(&mut fs, "/ljunk/.git", "/junkfile");
    file(&mut fs, "/skip/.git/keep", "");
    link(&mut fs, "/dangle/.git", "/nothing");
    fs.insert(b"/fifo/.git", Node::Fifo);
    file(&mut fs, "/plaindir/f", "");
    link(&mut fs, "/ldir/.git", "/plaindir");
    // A `.git` file naming a directory git dies on: none to `judge`.
    dir(&mut fs, "/dying/objects");
    dir(&mut fs, "/dying/refs");
    fs.insert(b"/dying/HEAD", Node::Fifo);
    file(&mut fs, "/dies/.git", "gitdir: /dying\n");
    let w = at(&fs);
    for folder in ["junk", "ljunk", "skip", "dangle", "fifo", "ldir", "dies"] {
        let (directory, dotgit) = (format!("/{folder}"), format!("/{folder}/.git"));
        assert_eq!(w.judge(&directory, &dotgit), Ok(None), "{folder}");
    }
    // Git follows a `.git` file or dies on it; the test is of what the
    // entry reaches (`stat`), so a link to a file is one too.
    for folder in ["junk", "ljunk", "dies"] {
        let (directory, dotgit) = (format!("/{folder}"), format!("/{folder}/.git"));
        let none = refused(why(&format!(
            "{dotgit} names no git directory membership follows"
        )));
        assert_eq!(w.entry(&directory, &dotgit), none, "{folder}");
        assert_eq!(w.reading(&directory), none, "{folder}");
    }
    for folder in ["skip", "dangle", "fifo", "ldir"] {
        let (directory, dotgit) = (format!("/{folder}"), format!("/{folder}/.git"));
        assert_eq!(w.entry(&directory, &dotgit), Ok(None), "{folder}");
        assert_eq!(w.reading(&directory), Ok(None), "{folder}");
    }
    // The same directory git dies on, as a `.git` directory and as the
    // folder itself, is a refusal that names it.
    assert_eq!(
        w.reading("/dying"),
        refused(why("git cannot read /dying as a git directory"))
    );
    assert_eq!(
        w.walk("/dying/objects"),
        refused(why("git cannot read /dying as a git directory"))
    );
}

#[test]
fn the_entry_is_read_before_the_folder_itself() {
    let mut fs = MemFs::new(Flavour::Posix);
    let bare = format!("{V0}\tbare = true\n");
    // A git directory holding a `.git` file: git follows the file or dies.
    repo(&mut fs, "/both.git", &bare);
    file(&mut fs, "/both.git/.git", "junk\n");
    // One holding a `.git` folder git passes over: the folder itself.
    repo(&mut fs, "/nested.git", &bare);
    file(&mut fs, "/nested.git/.git/keep", "");
    // A HEAD alone makes no git directory: git goes on.
    file(&mut fs, "/top/headonly/HEAD", HEAD);
    file(&mut fs, "/top/headonly/in/f", "");
    repo(&mut fs, "/top/.git", V0);
    let w = at(&fs);
    let junk = refused(why(
        "/both.git/.git names no git directory membership follows",
    ));
    // The folder first would read a bare repository.
    assert_eq!(w.reading("/both.git"), junk);
    assert_eq!(w.walk("/both.git/refs"), junk);
    // An entry that gives nothing must not end the folder's reading.
    assert_eq!(w.judge("/nested.git", "/nested.git/.git"), Ok(None));
    assert_eq!(w.reading("/nested.git"), NO_ROOT);
    // Reading on where git passes over the folder would refuse or follow.
    assert_eq!(w.reading("/top/headonly"), Ok(None));
    assert_eq!(w.walk("/top/headonly/in"), followed("/top"));
}

#[test]
fn the_git_directory_is_spelled_as_each_kind_names_it() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/w/plain/.git", V0);
    repo(&mut fs, "/sep.git", V0);
    link(&mut fs, "/l2", "/sep.git");
    link(&mut fs, "/l1/.git", "/l2");
    repo(&mut fs, "/lg_real/sep.git", V0);
    file(&mut fs, "/lg_real/x/f", "");
    link(&mut fs, "/lg", "/lg_real");
    file(&mut fs, "/viaf/.git", "gitdir: ../lg/x/../sep.git\n");
    file(&mut fs, "/w/a/b/.git", "gitdir: ../../../sep.git\n");
    let w = World {
        cwd: "/w",
        ..at(&fs)
    };
    // A folder's own `.git`: the entry as given, never made absolute.
    assert_eq!(
        w.judge("plain", "plain/.git"),
        entry("plain/.git", None, true, false)
    );
    assert_eq!(w.reading("plain"), followed("plain"));
    // A link: where it leads, every link on the way resolved.
    assert_eq!(
        w.judge("/l1", "/l1/.git"),
        entry("/sep.git", None, false, true)
    );
    // A `.git` file: its text from its folder, normalized by the text
    // alone (`normpath`), so a link in it stays and `x/..` goes unasked.
    assert_eq!(
        w.judge("/viaf", "/viaf/.git"),
        entry("/lg/sep.git", None, false, true)
    );
    assert_eq!(
        w.judge("a/b", "a/b/.git"),
        entry("../sep.git", None, false, true)
    );
    assert_eq!(w.reading("a/b"), provisional("a/b"));
    assert_eq!(w.walk("a/b"), provisional("/w/a/b"));
}

#[test]
fn a_head_goes_unread_or_unfetched_only_when_asked() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/sepbad.git", V0);
    file(&mut fs, "/sepbad.git/HEAD", "garbage\n");
    file(&mut fs, "/sbad/.git", "gitdir: /sepbad.git\n");
    repo(&mut fs, "/ph.git", V0);
    file(&mut fs, "/ph.git/HEAD", "garbage\n");
    fs.set_flags(b"/ph.git/HEAD", SF_DATALESS);
    file(&mut fs, "/p/.git", "gitdir: /ph.git\n");
    repo(&mut fs, "/pown/.git", V0);
    file(&mut fs, "/pown/.git/HEAD", "garbage\n");
    fs.set_flags(b"/pown/.git/HEAD", SF_DATALESS);
    repo(&mut fs, "/gown/.git", V0);
    file(&mut fs, "/gown/.git/HEAD", "garbage\n");
    let w = at(&fs);
    let st = w.lstat("/sbad/.git");
    let ask = |fetch, read| w.judge_as("/sbad", "/sbad/.git", &st, fetch, read);
    assert_eq!(ask(true, true), Ok(None));
    assert_eq!(ask(false, true), Ok(None));
    let unread = entry("/sepbad.git", None, false, true);
    assert_eq!(ask(true, false), unread);
    assert_eq!(ask(false, false), unread);
    // A placeholder's HEAD passes unread only with `fetch` False.
    let st = w.lstat("/p/.git");
    let ask = |fetch, read| w.judge_as("/p", "/p/.git", &st, fetch, read);
    assert_eq!(ask(true, true), Ok(None));
    assert_eq!(ask(false, true), entry("/ph.git", None, false, true));
    let st = w.lstat("/pown/.git");
    let ask = |fetch, read| w.judge_as("/pown", "/pown/.git", &st, fetch, read);
    assert_eq!(ask(true, true), Ok(None));
    assert_eq!(ask(false, true), entry("/pown/.git", None, true, false));
    assert_eq!(ask(true, false), entry("/pown/.git", None, true, false));
    // A folder's own `.git` takes the two the same way round: a HEAD that
    // is no placeholder is read unless `read` is False.
    let st = w.lstat("/gown/.git");
    let ask = |fetch, read| w.judge_as("/gown", "/gown/.git", &st, fetch, read);
    assert_eq!(ask(true, true), Ok(None));
    assert_eq!(ask(false, true), Ok(None));
    assert_eq!(ask(true, false), entry("/gown/.git", None, true, false));
    // The walk fetches and reads, always.
    assert_eq!(w.reading("/pown"), Ok(None));
    assert_eq!(
        w.reading("/p"),
        refused(why("/p/.git names no git directory membership follows"))
    );
}

#[test]
fn the_lstat_given_decides_what_the_entry_is() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/own/.git", V0);
    file(&mut fs, "/own/f", "");
    link(&mut fs, "/w2/.git", "/own/.git");
    repo(&mut fs, "/sep.git", V0);
    file(&mut fs, "/gf/.git", "gitdir: /sep.git\n");
    file(&mut fs, "/gf/f", "");
    fs.insert(b"/afifo", Node::Fifo);
    let w = at(&fs);
    let (linked, folder, regular, fifo) = (
        w.lstat("/w2/.git"),
        w.lstat("/own/.git"),
        w.lstat("/gf/.git"),
        w.lstat("/afifo"),
    );
    let ask = |directory, dotgit, st| w.judge_as(directory, dotgit, st, true, true);
    // A link then, the folder's own `.git` now: a pointer, not its own.
    // Reading the entry again in place of the `lstat` given says own.
    assert_eq!(
        ask("/own", "/own/.git", &linked),
        entry("/own/.git", None, false, true)
    );
    assert_eq!(
        ask("/own", "/own/.git", &folder),
        entry("/own/.git", None, true, false)
    );
    // A link then, a `.git` file now: read through, as a link to it is.
    assert_eq!(
        ask("/gf", "/gf/.git", &linked),
        entry("/sep.git", None, false, true)
    );
    // A folder then, a file now, and the other way: none.
    assert_eq!(ask("/gf", "/gf/.git", &folder), Ok(None));
    assert_eq!(ask("/own", "/own/.git", &regular), Ok(None));
    // Neither a file nor a folder then: none, whatever it is now.
    assert_eq!(ask("/own", "/own/.git", &fifo), Ok(None));
    assert_eq!(ask("/gf", "/gf/.git", &fifo), Ok(None));
    // An entry gone since: none for a link (its `stat` fails) and a file.
    assert_eq!(ask("/own", "/nothing/.git", &linked), Ok(None));
    assert_eq!(ask("/own", "/nothing/.git", &regular), Ok(None));
    assert_eq!(ask("/own", "/nothing/.git", &folder), Ok(None));
}

#[test]
fn a_toplevel_is_compared_as_a_file_and_only_with_a_root() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/plain/.git", V0);
    file(&mut fs, "/plain/f", "");
    file(&mut fs, "/plain/sub/f", "");
    link(&mut fs, "/lplain", "/plain");
    repo(&mut fs, "/bare/.git", &format!("{V0}\tbare = true\n"));
    repo(&mut fs, "/bare.git", &format!("{V0}\tbare = true\n"));
    repo(&mut fs, "/else/.git", &format!("{V0}\tworktree = /plain\n"));
    file(&mut fs, "/none/f", "");
    let w = at(&fs);
    // Comparing the texts fails each of the first four.
    assert_eq!(w.agrees("/plain/sub", "/plain"), Ok(true));
    assert_eq!(w.agrees("/plain/sub", "/plain/"), Ok(true));
    assert_eq!(w.agrees("/plain/sub", "/plain/sub/.."), Ok(true));
    assert_eq!(w.agrees("/plain/sub", "/lplain"), Ok(true));
    assert_eq!(w.agrees("/lplain/sub", "/plain"), Ok(true));
    assert_eq!(w.agrees("/plain/sub", "/plain/sub"), Ok(false));
    assert_eq!(w.agrees("/plain", "/nothing"), Ok(false));
    assert_eq!(w.agrees("/plain", "/plain/f"), Ok(false));
    // From inside the git directory the root is the folder above it.
    assert_eq!(w.agrees("/plain/.git/objects", "/plain"), Ok(true));
    // A refusal, a bare repository and no repository have no root, though
    // the toplevel is the folder the walk stopped at, or git's own answer.
    assert_eq!(w.agrees("/else", "/else"), Ok(false));
    assert_eq!(w.agrees("/else", "/plain"), Ok(false));
    assert_eq!(w.agrees("/bare", "/bare"), Ok(false));
    assert_eq!(w.agrees("/bare/.git", "/bare"), Ok(false));
    assert_eq!(w.agrees("/bare.git", "/bare.git"), Ok(false));
    assert_eq!(w.agrees("/none", "/none"), Ok(false));
}

#[test]
fn a_refusal_alone_is_the_why_with_its_kind() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/plain/.git", V0);
    file(&mut fs, "/plain/sub/f", "");
    repo(
        &mut fs,
        "/v2/.git",
        "[core]\n\trepositoryformatversion = 2\n",
    );
    repo(&mut fs, "/bare/.git", &format!("{V0}\tbare = true\n"));
    file(&mut fs, "/none/f", "");
    let w = at(&fs);
    // None both where the repository is followed and where there is none.
    assert_eq!(w.refusal("/plain/sub"), Ok(None));
    assert_eq!(w.refusal("/none"), Ok(None));
    // A bare repository reached from inside is no refusal either.
    assert_eq!(w.refusal("/bare/.git"), Ok(None));
    assert_eq!(
        w.refusal("/bare"),
        Ok(why("/bare/.git's config names no working tree (core.bare)"))
    );
    // `FormatUnknown` stays the type it is.
    assert_eq!(
        w.refusal("/v2"),
        Ok(unknown(
            "/v2/.git's config cannot be read as git reads it: /v2/.git/config sets a \
             repository format version git does not read"
        ))
    );
}

/// Records by the git directory's path, looked up by its stat now.
struct ByPath<'a> {
    fs: &'a MemFs,
    records: Vec<(&'static str, String)>,
}

impl Records for ByPath<'_> {
    fn record(&self, git_dir: &StatResult) -> Option<Vec<u8>> {
        self.records.iter().find_map(|(path, data)| {
            let st = self.fs.stat(path.as_bytes()).ok()?;
            (samestat(&st, git_dir) == Some(true)).then(|| data.clone().into_bytes())
        })
    }
}

#[test]
fn a_provisional_follow_is_carried_to_the_reading() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/sep.git", V0);
    file(&mut fs, "/a/.git", "gitdir: /sep.git\n");
    file(&mut fs, "/a/in/f", "");
    file(&mut fs, "/b/.git", "gitdir: /sep.git\n");
    let w = at(&fs);
    // No record: followed, provisionally, with its root.
    assert_eq!(
        w.judge("/a", "/a/.git"),
        entry("/sep.git", None, false, true)
    );
    assert_eq!(w.entry("/a", "/a/.git"), provisional("/a"));
    assert_eq!(w.reading("/a"), provisional("/a"));
    assert_eq!(w.walk("/a/in"), provisional("/a"));
    assert_eq!(w.refusal("/a/in"), Ok(None));
    assert_eq!(w.agrees("/a/in", "/a"), Ok(true));
    // A record naming the folder: followed for good. Naming another whose
    // `.git` names the git directory too: refused.
    let records = ByPath {
        fs: &fs,
        records: vec![(
            "/sep.git",
            "{\"format\": 1, \"git_dir\": \"/sep.git\", \"tree\": \"/a\"}\n".to_owned(),
        )],
    };
    let w = World {
        records: &records,
        ..at(&fs)
    };
    assert_eq!(
        w.judge("/a", "/a/.git"),
        entry("/sep.git", None, false, false)
    );
    assert_eq!(w.walk("/a/in"), followed("/a"));
    let other = why("/sep.git is the git directory of the working tree /a");
    assert_eq!(
        w.judge("/b", "/b/.git"),
        entry("/sep.git", other.clone(), false, false)
    );
    assert_eq!(w.walk("/b"), refused(other));
    assert_eq!(w.agrees("/b", "/b"), Ok(false));
}

/// The test of a followed entry's `commondir` is of a folder's own `.git`
/// alone. A link to a git directory whose `commondir` is foreign is
/// refused by `judge` itself, unless `GIT_COMMON_DIR` makes it a
/// worktree's: then it is followed, and `_entry_reading` adds no test.
#[test]
fn a_followed_pointers_commondir_is_not_tested_again() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/gd", V0);
    file(&mut fs, "/fgd/HEAD", HEAD);
    file(&mut fs, "/fgd/commondir", "/gd\n");
    file(&mut fs, "/fgd/gitdir", "/lwx/.git\n");
    link(&mut fs, "/lwx/.git", "/fgd");
    let foreign = why("/fgd names the common directory /gd but is not one of its worktrees");
    for env in [None, Some("")] {
        let w = World { env, ..at(&fs) };
        assert_eq!(
            w.judge("/lwx", "/lwx/.git"),
            entry("/fgd", foreign.clone(), false, false)
        );
        assert_eq!(w.reading("/lwx"), refused(foreign.clone()));
    }
    let w = World {
        env: Some("/gd"),
        ..at(&fs)
    };
    assert_eq!(
        w.judge("/lwx", "/lwx/.git"),
        entry("/fgd", None, false, false)
    );
    // Testing the `commondir` of every followed entry refuses these.
    assert_eq!(w.entry("/lwx", "/lwx/.git"), followed("/lwx"));
    assert_eq!(w.reading("/lwx"), followed("/lwx"));
    assert_eq!(w.walk("/lwx"), followed("/lwx"));
    // The git directory itself, as a folder, is refused whatever is set.
    assert_eq!(w.reading("/fgd"), refused(foreign));
}

/// Pinned otherwise only in the file gated on the Python release (an
/// empty, `.` or `..` part in the query): the folder is kept as given.
#[test]
fn a_folder_is_kept_as_given_empty_and_dotted() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/w/.git", V0);
    file(&mut fs, "/w/sub/f", "");
    repo(&mut fs, "/main/.git", V0);
    file(&mut fs, "/own/f", "");
    let w = World {
        cwd: "/w",
        ..at(&fs)
    };
    // The working directory, asked as the empty path: its root is empty.
    assert_eq!(w.reading(""), followed(""));
    assert_eq!(w.reading("."), followed("."));
    assert_eq!(w.judge("", ".git"), entry(".git", None, true, false));
    assert_eq!(w.judge(".", "./.git"), entry("./.git", None, true, false));
    assert_eq!(w.entry("", ".git"), followed(""));
    // Inside it, by a relative path: the folder above `.git` is empty too.
    assert_eq!(w.reading(".git"), followed(""));
    assert_eq!(w.reading("sub"), Ok(None));
    // A walk resolves its start, so its root is never empty.
    assert_eq!(w.walk(""), followed("/w"));
    assert_eq!(w.walk("."), followed("/w"));
    assert_eq!(w.walk("sub"), followed("/w"));
    assert_eq!(w.walk(".git/objects"), followed("/w"));
    // An empty toplevel is no folder; `.` is the working directory.
    assert_eq!(w.agrees("sub", ""), Ok(false));
    assert_eq!(w.agrees("", ""), Ok(false));
    assert_eq!(w.agrees("sub", "."), Ok(true));
    assert_eq!(w.agrees("sub", "sub/.."), Ok(true));
    assert_eq!(w.agrees("sub", "sub"), Ok(false));
    // A trailing separator, a doubled one, `.` and `..` stay in the root.
    assert_eq!(w.reading("/main/"), followed("/main/"));
    assert_eq!(w.reading("//main"), followed("//main"));
    assert_eq!(w.reading("/main/."), followed("/main/."));
    assert_eq!(w.reading("/own/../main"), followed("/own/../main"));
    assert_eq!(
        w.judge("/main/.", "/main/./.git"),
        entry("/main/./.git", None, true, false)
    );
}

/// Pinned otherwise only in the file gated on the Python release: a link
/// loop is read as nothing there.
#[test]
fn a_link_loop_names_nothing() {
    let mut fs = MemFs::new(Flavour::Posix);
    link(&mut fs, "/l1", "l2");
    link(&mut fs, "/l2", "l1");
    link(&mut fs, "/loopy/.git", "/loopy/.git");
    repo(&mut fs, "/main/.git", V0);
    let w = at(&fs);
    assert_eq!(w.judge("/loopy", "/loopy/.git"), Ok(None));
    assert_eq!(w.entry("/loopy", "/loopy/.git"), Ok(None));
    assert_eq!(w.reading("/loopy"), Ok(None));
    assert_eq!(w.reading("/l1"), Ok(None));
    assert_eq!(w.walk("/loopy"), Ok(None));
    assert_eq!(w.walk("/l1/x"), Ok(None));
    assert_eq!(w.agrees("/l1", "/l1"), Ok(false));
    assert_eq!(w.reading("/main"), followed("/main"));
}

/// A link to a git directory is judged with the common directory `judge`
/// read through the link, so a reason names the config as spelled
/// through the entry. Reading the common directory again from the
/// resolved target names the target's config instead.
#[test]
fn a_links_common_directory_is_spelled_through_the_link() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(
        &mut fs,
        "/g1.git",
        "[core]\n\trepositoryformatversion = 2\n",
    );
    link(&mut fs, "/a/.git", "/g1.git");
    file(&mut fs, "/a/f", "");
    let w = at(&fs);
    let format = unknown(
        "/g1.git's config cannot be read as git reads it: /a/.git/config sets a \
         repository format version git does not read",
    );
    assert_eq!(
        w.judge("/a", "/a/.git"),
        entry("/g1.git", format.clone(), false, false)
    );
    assert_eq!(w.entry("/a", "/a/.git"), refused(format.clone()));
    assert_eq!(w.reading("/a"), refused(format.clone()));
    assert_eq!(w.walk("/a"), refused(format));
}

/// The folder itself is asked of git as the walk asks it, fetching and
/// reading its HEAD: a HEAD that is no ref, or a placeholder whose bytes
/// are none, is no git directory, and the walk goes on to the repository
/// above. Not fetching, or not reading, stops the walk there with no
/// root.
#[test]
fn the_folder_itself_is_fetched_and_read() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/top/.git", V0);
    repo(&mut fs, "/top/bad.git", V0);
    file(&mut fs, "/top/bad.git/HEAD", "garbage\n");
    repo(&mut fs, "/top/ph.git", V0);
    file(&mut fs, "/top/ph.git/HEAD", "garbage\n");
    fs.set_flags(b"/top/ph.git/HEAD", SF_DATALESS);
    let w = at(&fs);
    for folder in ["/top/bad.git", "/top/ph.git"] {
        let inside = format!("{folder}/objects");
        assert_eq!(w.reading(folder), Ok(None), "{folder}");
        assert_eq!(w.walk(&inside), followed("/top"), "{folder}");
        assert_eq!(w.refusal(&inside), Ok(None), "{folder}");
        assert_eq!(w.agrees(&inside, "/top"), Ok(true), "{folder}");
    }
}

/// A link target and a `commondir` whose `.` and `..` parts come before
/// any name climb from the folder they are read in: a relative link at
/// `.git` to another tree's own `.git` is that tree's, and a linked
/// worktree whose `commondir` mixes `.`, `..` and a name is followed.
#[test]
fn a_link_and_a_commondir_climb_from_their_folder() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/main/.git", V0);
    file(&mut fs, "/main/f", "");
    repo(&mut fs, "/own/.git", V0);
    file(&mut fs, "/own/f", "");
    for (wt, commondir) in [("wd", "../../../.git\n"), ("ws", "./../..\n")] {
        let at_ = format!("/main/.git/worktrees/{wt}");
        file(&mut fs, &format!("{at_}/HEAD"), HEAD);
        file(&mut fs, &format!("{at_}/commondir"), commondir);
        file(&mut fs, &format!("{at_}/gitdir"), &format!("/{wt}/.git\n"));
        file(&mut fs, &format!("/{wt}/.git"), &format!("gitdir: {at_}\n"));
        file(&mut fs, &format!("/{wt}/f"), "");
    }
    link(&mut fs, "/rel/.git", "../own/.git");
    let w = at(&fs);
    for wt in ["wd", "ws"] {
        let (directory, dotgit) = (format!("/{wt}"), format!("/{wt}/.git"));
        let git_dir = format!("/main/.git/worktrees/{wt}");
        assert_eq!(
            w.judge(&directory, &dotgit),
            entry(&git_dir, None, false, false)
        );
        assert_eq!(w.entry(&directory, &dotgit), followed(&directory));
        assert_eq!(w.reading(&directory), followed(&directory));
        assert_eq!(w.walk(&directory), followed(&directory));
        assert_eq!(w.agrees(&directory, &directory), Ok(true));
        assert_eq!(w.reading(&git_dir), NO_ROOT);
    }
    let links = why("/rel/.git links to the .git of /own");
    assert_eq!(
        w.judge("/rel", "/rel/.git"),
        entry("/own/.git", links.clone(), false, false)
    );
    assert_eq!(w.entry("/rel", "/rel/.git"), refused(links.clone()));
    assert_eq!(w.reading("/rel"), refused(links.clone()));
    assert_eq!(w.walk("/rel"), refused(links));
}
