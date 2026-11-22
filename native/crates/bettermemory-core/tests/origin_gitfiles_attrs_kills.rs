//! Cases of `origin_gitfiles::attrs` the golden file does not reach, over
//! a memory file system: the index memo's bound and order, a kept answer
//! outliving a `sharedindex.*` file as the reference keys it, the read
//! that grows past several prefixes, the chain of `.gitattributes`
//! directories, the global file's fallback from an empty XDG_CONFIG_HOME
//! to HOME, the fallback past a repository of another owner, the links
//! `info/attributes` follows and `.gitattributes` does not, an index whose
//! open fails for a reason other than absence, and the declines of the
//! Windows flavour.

mod common;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFile, MemFs, Node, OsError, PyErr, Refused, Stat, StatResult,
};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{Env, Host, Owners};
use bettermemory_core::githead::GitDir;
use bettermemory_core::origin_gitfiles::attrs::{
    attribute_files_signature, gitattributes_signature, index_gitmodules, IndexFailure,
    IndexGitmodules, IndexMemo, INDEX_FIRST_READ, INDEX_MEMO_CAP, INDEX_READ_GROWTH,
};
use bettermemory_core::origin_gitfiles::facts::Held;
use bettermemory_core::origin_gitfiles::root::ConfigMemo;

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};
const INIT: &[u8] = b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n";

struct Mine;

struct Theirs;

impl Owners for Theirs {
    fn owner_uid(&self, _path: &[u8], _follow: bool) -> Result<u32, OsError> {
        Ok(1)
    }
}

impl Owners for Mine {
    fn owner_uid(&self, _path: &[u8], _follow: bool) -> Result<u32, OsError> {
        Ok(0)
    }
}

fn entry(name: &[u8], oid: &[u8]) -> Vec<u8> {
    let mut out = vec![0u8; 24];
    out.extend(0o100_644u32.to_be_bytes());
    out.extend([0u8; 12]);
    out.extend(oid);
    out.extend((name.len().min(0x0FFF) as u16).to_be_bytes());
    let pad = (out.len() + name.len() + 8) & !7;
    out.extend(name);
    out.resize(pad, 0);
    out
}

/// A version-2 index of `before` entries ahead of `.gitmodules`.
fn index(before: usize) -> Vec<u8> {
    let mut entries: Vec<Vec<u8>> = (0..before)
        .map(|i| entry(format!(".a{i:06}").as_bytes(), &[0x11; 20]))
        .collect();
    entries.push(entry(b".gitmodules", &[0x22; 20]));
    let mut out = b"DIRC\0\0\0\x02".to_vec();
    out.extend((entries.len() as u32).to_be_bytes());
    out.extend(entries.concat());
    out
}

fn repo(fs: &mut MemFs, at: &str, config: &[u8], index: Option<Vec<u8>>) -> GitDir {
    let git = format!("{at}/.git");
    for dir in [
        at.to_owned(),
        git.clone(),
        format!("{git}/objects"),
        format!("{git}/refs"),
    ] {
        fs.insert(dir.as_bytes(), Node::Dir);
    }
    fs.insert(
        format!("{git}/HEAD").as_bytes(),
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(
        format!("{git}/config").as_bytes(),
        Node::File(config.to_vec()),
    );
    if let Some(index) = index {
        fs.insert(format!("{git}/index").as_bytes(), Node::File(index));
    }
    GitDir {
        gitdir: git.clone().into_bytes(),
        commondir: git.into_bytes(),
    }
}

fn base() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/t", Node::Dir);
    fs
}

fn ctx(fs: &MemFs) -> Ctx<'_, MemFs> {
    Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/t",
    }
}

const ENTRY: fn() -> Option<IndexGitmodules> =
    || Some(IndexGitmodules::Entry(0o100_644, "22".repeat(20)));

#[test]
fn the_constants_are_the_reference_s() {
    assert_eq!(INDEX_MEMO_CAP, 64);
    assert_eq!(INDEX_FIRST_READ, 1 << 16);
    assert_eq!(INDEX_READ_GROWTH, 4);
}

#[test]
fn the_index_memo_keeps_the_last_64_used() {
    let mut fs = base();
    let gds: Vec<GitDir> = (0..=INDEX_MEMO_CAP)
        .map(|i| repo(&mut fs, &format!("/t/r{i}"), INIT, Some(index(1))))
        .collect();
    let ctx = ctx(&fs);
    let (mut memo, mut config) = (IndexMemo::new(), ConfigMemo::new());
    for gd in &gds[..INDEX_MEMO_CAP] {
        assert_eq!(
            index_gitmodules(&mut memo, &mut config, &ctx, gd, &GIT),
            Ok(ENTRY())
        );
    }
    // A hit moves the first read last, so the second is dropped.
    index_gitmodules(&mut memo, &mut config, &ctx, &gds[0], &GIT).unwrap();
    index_gitmodules(&mut memo, &mut config, &ctx, &gds[INDEX_MEMO_CAP], &GIT).unwrap();
    let keys = memo.keys();
    assert_eq!(keys.len(), INDEX_MEMO_CAP);
    assert_eq!(keys[0], b"/t/r2/.git/index");
    assert_eq!(keys[INDEX_MEMO_CAP - 2], b"/t/r0/.git/index");
    assert!(!keys.contains(&b"/t/r1/.git/index".to_vec()));
}

#[test]
fn a_kept_answer_outlives_a_split_index_until_the_memo_is_cleared() {
    let mut fs = base();
    let gd = repo(&mut fs, "/t/r", INIT, Some(index(0)));
    let (mut memo, mut config) = (IndexMemo::new(), ConfigMemo::new());
    assert_eq!(
        index_gitmodules(&mut memo, &mut config, &ctx(&fs), &gd, &GIT),
        Ok(ENTRY())
    );
    fs.insert(b"/t/r/.git/sharedindex.0", Node::File(b"x".to_vec()));
    assert_eq!(
        index_gitmodules(&mut memo, &mut config, &ctx(&fs), &gd, &GIT),
        Ok(ENTRY())
    );
    memo.clear();
    assert_eq!(
        index_gitmodules(&mut memo, &mut config, &ctx(&fs), &gd, &GIT),
        Err(IndexFailure::Unsettled)
    );
}

#[test]
fn the_read_grows_until_the_entry_is_reached() {
    // 72 bytes an entry: past 64 KiB, 256 KiB and 1 MiB.
    let mut fs = base();
    let gd = repo(&mut fs, "/t/r", INIT, Some(index(20_000)));
    let (mut memo, mut config) = (IndexMemo::new(), ConfigMemo::new());
    assert_eq!(
        index_gitmodules(&mut memo, &mut config, &ctx(&fs), &gd, &GIT),
        Ok(ENTRY())
    );
}

#[test]
fn a_sha256_repository_and_an_absent_index() {
    let mut fs = base();
    let gd = repo(
        &mut fs,
        "/t/r",
        b"[extensions]\n\tobjectFormat = sha256\n",
        Some(index(0)),
    );
    let (mut memo, mut config) = (IndexMemo::new(), ConfigMemo::new());
    assert_eq!(
        index_gitmodules(&mut memo, &mut config, &ctx(&fs), &gd, &GIT),
        Err(IndexFailure::Unsettled)
    );
    let mut fs = base();
    let gd = repo(&mut fs, "/t/s", b"", None);
    assert_eq!(
        index_gitmodules(&mut memo, &mut config, &ctx(&fs), &gd, &GIT),
        Ok(None)
    );
}

#[test]
fn the_chain_is_each_directory_once_in_the_order_first_named() {
    let mut fs = base();
    fs.insert(b"/t/a", Node::Dir);
    fs.insert(b"/t/a/.gitattributes", Node::File(b"x".to_vec()));
    let mut held = Held::new();
    let specs: [&[u8]; 3] = [b"a/b/c", b"a/d", b"e"];
    let sig = gitattributes_signature(&ctx(&fs), b"/t", &specs, Some(&mut held))
        .unwrap()
        .unwrap();
    assert_eq!(sig.len(), 3);
    assert!(sig[0].is_none() && sig[1].is_some() && sig[2].is_none());
    // `/t/a/b` is missing: its nearest existing ancestor is held.
    let keys: Vec<&[u8]> = held.keys().map(Vec::as_slice).collect();
    assert_eq!(keys, [&b"/t"[..], b"/t/a"]);
    for spec in [
        &b"a*"[..],
        b"a?",
        b"[a",
        b"a\\b",
        b":a",
        b"a/",
        b"./a",
        b"a/..",
    ] {
        assert_eq!(
            gitattributes_signature(&ctx(&fs), b"/t", &[spec], None),
            Ok(None)
        );
    }
    let windows = Ctx {
        fs: &fs,
        flavour: Flavour::Windows,
        cwd: b"C:\\t",
    };
    assert!(gitattributes_signature(&windows, b"C:\\t", &[b"a"], None).is_err());
}

#[test]
fn an_empty_xdg_config_home_falls_back_to_home() {
    let mut fs = base();
    repo(&mut fs, "/t/r", INIT, None);
    let ctx = ctx(&fs);
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let host = Host {
        euid: 0,
        owners: &Mine,
    };
    let env = Env::default();
    let mut memo = ConfigMemo::new();
    let mut sig = |xdg: Option<&[u8]>, home: Option<&[u8]>, env: &Env| {
        attribute_files_signature(
            &mut memo, &rule, &host, env, xdg, home, b"/t/r", b"/t/r", None,
        )
        .unwrap()
        .map(|s| s.global_file)
    };
    assert_eq!(
        sig(Some(b""), Some(b"/h"), &env),
        Some(Some(b"/h/.config/git/attributes".to_vec()))
    );
    assert_eq!(
        sig(Some(b"x"), None, &env),
        Some(Some(b"/t/r/x/git/attributes".to_vec()))
    );
    assert_eq!(sig(None, None, &env), Some(None));
    let moved = Env {
        git_dir: Some(b"/t/r/.git".to_vec()),
        ..Env::default()
    };
    assert_eq!(sig(Some(b"/x"), None, &moved), None);
}

const S_IFMT: i128 = 0o170_000;

fn rule_sig(
    fs: &MemFs,
    euid: u32,
    owners: &dyn Owners,
    xdg: Option<&[u8]>,
    home: Option<&[u8]>,
) -> Option<bettermemory_core::origin_gitfiles::attrs::AttributeFiles> {
    let ctx = ctx(fs);
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let host = Host { euid, owners };
    attribute_files_signature(
        &mut ConfigMemo::new(),
        &rule,
        &host,
        &Env::default(),
        xdg,
        home,
        b"/t/r",
        b"/t/r",
        None,
    )
    .unwrap()
}

#[test]
fn a_repository_of_another_owner_falls_back_while_xdg_is_set() {
    // find_gitdir declines; the reference answers None after the locate
    // fallback only when XDG_CONFIG_HOME is empty or unset and HOME unset.
    let mut fs = base();
    repo(&mut fs, "/t/r", INIT, None);
    let got = rule_sig(&fs, 0, &Theirs, Some(b"/t/r/xdg"), None);
    assert_eq!(
        got.map(|s| s.global_file),
        Some(Some(b"/t/r/xdg/git/attributes".to_vec()))
    );
    assert_eq!(rule_sig(&fs, 0, &Theirs, Some(b""), None), None);
    assert_eq!(rule_sig(&fs, 0, &Theirs, None, None), None);
}

#[test]
fn info_attributes_and_the_global_file_are_stamped_through_a_link() {
    let mut fs = base();
    repo(&mut fs, "/t/r", INIT, None);
    fs.insert(b"/t/r/.git/info", Node::Dir);
    fs.insert(b"/t/target", Node::File(b"* text\n".to_vec()));
    fs.insert(
        b"/t/r/.git/info/attributes",
        Node::Link(b"/t/target".to_vec()),
    );
    fs.insert(b"/t/x/git", Node::Dir);
    fs.insert(b"/t/x/git/attributes", Node::Link(b"/t/target".to_vec()));
    let sig = rule_sig(&fs, 0, &Mine, Some(b"/t/x"), None).unwrap();
    let info = sig.info_attributes.unwrap();
    let global = sig.global_attributes.unwrap();
    assert_eq!(info[4] & S_IFMT, 0o100_000);
    assert_eq!(global[4] & S_IFMT, 0o100_000);
    assert_eq!(info, global);
}

#[test]
fn a_gitattributes_link_is_stamped_as_the_link() {
    let mut fs = base();
    fs.insert(b"/t/target", Node::File(b"* text\n".to_vec()));
    fs.insert(b"/t/.gitattributes", Node::Link(b"/t/target".to_vec()));
    let sig = gitattributes_signature(&ctx(&fs), b"/t", &[b"f"], None)
        .unwrap()
        .unwrap();
    assert_eq!(sig[0].unwrap()[4] & S_IFMT, 0o120_000);
}

/// A memory file system whose open of the index fails with
/// FileNotFoundError carrying ERROR_BAD_NETPATH, as a Windows share that
/// does not answer raises it: `path_missing` rejects it.
struct Unanswered(MemFs);

impl Fs for Unanswered {
    type File = MemFile;
    fn open(&self, path: &[u8]) -> Result<MemFile, PyErr> {
        self.0.open(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<MemFile, OsError> {
        if path.ends_with(b"/index") {
            return Err(OsError {
                winerror: Some(53),
                ..OsError::new(PyErr::FileNotFound, "ENOENT")
            });
        }
        self.0.open_result(path)
    }
    fn fstat(&self, file: &mut MemFile) -> Result<Stat, PyErr> {
        self.0.fstat(file)
    }
    fn fstat_result(&self, file: &mut MemFile) -> Result<Stat, OsError> {
        self.0.fstat_result(file)
    }
    fn fstat_full(&self, file: &mut MemFile) -> Option<Result<StatResult, OsError>> {
        self.0.fstat_full(file)
    }
    fn read(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, PyErr> {
        self.0.read(file, max)
    }
    fn read_once(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, OsError> {
        self.0.read_once(file, max)
    }
    fn close(&self, file: MemFile) -> Result<(), OsError> {
        self.0.close(file)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.0.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.0.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.0.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.0.readlink(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.0.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.0.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.0.listdir(path)
    }
    fn rewind(&self, file: &mut MemFile) -> Result<(), Refused> {
        self.0.rewind(file)
    }
    fn kernel_path(&self, file: &mut MemFile) -> Result<Vec<u8>, Refused> {
        self.0.kernel_path(file)
    }
}

#[test]
fn an_index_open_that_is_not_absence_raises() {
    let mut fs = base();
    let gd = repo(&mut fs, "/t/r", INIT, Some(index(0)));
    let fs = Unanswered(fs);
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/t",
    };
    let (mut memo, mut config) = (IndexMemo::new(), ConfigMemo::new());
    assert_eq!(
        index_gitmodules(&mut memo, &mut config, &ctx, &gd, &GIT),
        Err(IndexFailure::Raised(PyErr::FileNotFound))
    );
}

#[test]
fn of_two_stage_0_entries_the_last_is_kept() {
    // The reference's scan lists every `.gitmodules` entry, a repeated
    // stage 0 among them, and keeps the last stage-0 one.
    let mut fs = base();
    let mut data = b"DIRC\0\0\0\x02\0\0\0\x02".to_vec();
    data.extend(entry(b".gitmodules", &[0x33; 20]));
    data.extend(entry(b".gitmodules", &[0x22; 20]));
    let gd = repo(&mut fs, "/t/r", INIT, Some(data));
    let (mut memo, mut config) = (IndexMemo::new(), ConfigMemo::new());
    assert_eq!(
        index_gitmodules(&mut memo, &mut config, &ctx(&fs), &gd, &GIT),
        Ok(ENTRY())
    );
}

#[test]
fn an_unparsed_config_worktree_answers_none() {
    // A NUL byte in a comment: githead reads the repository, but
    // `_config_entries` gives no names, and the reference answers None as
    // it does for a file that sets `log.follow`.
    let common: &[u8] =
        b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tworktreeConfig = true\n";
    let mut fs = base();
    repo(&mut fs, "/t/r", common, None);
    fs.insert(
        b"/t/r/.git/config.worktree",
        Node::File(b"[core]\n".to_vec()),
    );
    assert!(rule_sig(&fs, 0, &Mine, None, Some(b"/h")).is_some());
    fs.insert(
        b"/t/r/.git/config.worktree",
        Node::File(b"[core]\n# \0\n".to_vec()),
    );
    assert_eq!(rule_sig(&fs, 0, &Mine, None, Some(b"/h")), None);
}
