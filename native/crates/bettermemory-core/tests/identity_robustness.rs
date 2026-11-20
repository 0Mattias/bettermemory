//! `gitfile_rule::identity` on hostile trees, on a 512 KiB thread (debug
//! build): typed answers, no panic; a crowded folder costs `same_folder`,
//! `names_here`, `tree_of` and `working_tree_of` what an empty one does
//! (they state, and read a folder up to its first name other than `.git`,
//! never whole), and that read answers as a listing does, in memory and on
//! disk; the Windows flavour declines; a relative path (here and in
//! `read_small`) is taken from the context's working directory, never the
//! process's; and a name in NFC against one in NFD is answered as the
//! platform's own file system answers it.

mod timing;

use bettermemory_core::fsdata::{Flavour, Fs, MemFs, Node, PyErr, StatResult};
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::gitfile_rule::identity::{
    is_link, names_folder, names_here, same_folder, tree_of, working_tree_of,
};

fn on_small_stack(work: impl FnOnce() + Send + 'static) {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(work)
        .expect("a thread")
        .join()
        .expect("no panic on a 512 KiB stack");
}

fn ctx(fs: &MemFs) -> Ctx<'_, MemFs> {
    Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    }
}

#[test]
fn hostile_trees_answer_typed_on_a_small_stack() {
    on_small_stack(|| {
        let mut fs = MemFs::new(Flavour::Posix);
        fs.insert(b"/w/.git", Node::Dir);
        fs.insert(b"/w/f", Node::File(Vec::new()));
        fs.insert(b"/loop", Node::Link(b"/loop".to_vec()));
        fs.insert(b"/deep", Node::Link(b"/w/.git".to_vec()));
        let mut chain = b"/deep".to_vec();
        for i in 0..200 {
            let name = format!("/c{i}");
            fs.insert(name.as_bytes(), Node::Link(chain.clone()));
            chain = name.into_bytes();
        }
        fs.insert(b"/locked", Node::Dir);
        fs.insert(b"/locked/.git", Node::Dir);
        fs.set_mode(b"/locked", 0);
        let c = ctx(&fs);
        let long = b"/a".repeat(5000);
        for path in [
            &b""[..],
            b".",
            b"..",
            b"/",
            b"/loop",
            b"/loop/.git",
            b"/x\x00y",
            b"/\xff\xfe",
            &chain,
            &long,
            b"/w/f/..",
            b"/locked/.git",
            b"/deep",
        ] {
            let _ = names_here(&c, path, b"/w", b"/w/.git").expect("ported");
            let _ = names_folder(&c, path, b"/w", b"/w").expect("ported");
            let _ = same_folder(&c, path, b"/w").expect("ported");
            let _ = same_folder(&c, b"/w", path).expect("ported");
            let _ = tree_of(&c, path).expect("ported");
            let _ = working_tree_of(&c, path).expect("ported");
        }
        assert_eq!(same_folder(&c, b"/x\x00y", b"/w"), Ok(Some(false)));
        assert_eq!(same_folder(&c, b"/loop", b"/w"), Ok(Some(false)));
        assert_eq!(same_folder(&c, b"/nothing", b"/w"), Ok(None));
        assert_eq!(tree_of(&c, b"/deep"), Ok(Some(b"/w".to_vec())));
        // `realpath` walks a chain of any length: only `stat` caps it.
        assert_eq!(tree_of(&c, &chain), Ok(Some(b"/w".to_vec())));
        assert_eq!(same_folder(&c, &chain, b"/w/.git"), Ok(Some(false)));
        assert_eq!(working_tree_of(&c, b"/deep"), Ok(Some(b"/w".to_vec())));
    });
}

/// `same_folder`, `names_here`, `tree_of` and `working_tree_of` of a
/// folder of 100,000 names cost what one of 10,000 does: they state two
/// paths, and `working_tree_of` reads the folder up to its first name
/// other than `.git`, as `scandir` under `all` does, never whole (best of
/// five, 200 calls each; a scan would be 10 times).
#[test]
fn a_crowded_folder_is_not_scanned() {
    fn tree(n: usize) -> MemFs {
        let mut fs = MemFs::new(Flavour::Posix);
        fs.insert(b"/w/.git", Node::Dir);
        for i in 0..n {
            fs.insert(format!("/w/n{i}").as_bytes(), Node::File(Vec::new()));
        }
        fs
    }
    let time = |fs: &MemFs| {
        let c = ctx(fs);
        timing::once(|| {
            for _ in 0..200 {
                assert_eq!(same_folder(&c, b"/w/.git/..", b"/w"), Ok(Some(true)));
                assert_eq!(names_here(&c, b".git", b"/w", b"/w/.git"), Ok(Some(true)));
                assert_eq!(tree_of(&c, b"/w/.git"), Ok(Some(b"/w".to_vec())));
                assert_eq!(working_tree_of(&c, b"/w/.git"), Ok(Some(b"/w".to_vec())));
            }
        })
    };
    let (small, large) = (tree(10_000), tree(100_000));
    timing::grows_within(
        "10,000 and 100,000 names",
        5,
        &mut || time(&small),
        &mut || time(&large),
    );
}

/// The folders `holds_other_than` is asked of: what a listing of each
/// says (`any` name other than `.git`), and its error where it fails.
fn listed<F: Fs>(fs: &F, path: &[u8]) -> Result<bool, PyErr> {
    fs.listdir(path)
        .map(|names| names.iter().any(|name| name.as_slice() != b".git"))
        .map_err(|e| e.class)
}

/// The folders of `scanned_as_listed`, as (path, files below it) pairs,
/// and the links: names sorting on either side of `.git` and between its
/// own key and the keys below it (`.git-x`, `.git.`, `.git0`).
const SCANNED: &[(&str, &[&str])] = &[
    ("empty", &[]),
    ("dirgit", &[".git/HEAD", ".git/objects/x/y", ".git/refs/z"]),
    ("filegit", &[".git"]),
    ("bang", &["!", ".git/HEAD"]),
    ("gi", &[".gi", ".git/HEAD"]),
    ("dash", &[".git-x", ".git/HEAD"]),
    ("dot", &[".git."]),
    ("zero", &[".git/HEAD", ".git0"]),
    ("after", &[".git/HEAD", "z"]),
    ("filez", &[".git", "z"]),
    ("gitdash", &[".git", ".git-x"]),
    ("locked", &[".git/HEAD", "f"]),
];

/// `holds_other_than(path, ".git")` answers what a listing of the folder
/// says, error for error, in memory and on disk: names sorting before
/// `.git`, after it, and between its own key and the keys below it in
/// `MemFs`'s map; a folder holding `.git` alone, as a folder with keys
/// below it or as a file; an empty folder; a link to a folder; a file,
/// nothing, a NUL, and a folder that cannot be listed.
#[test]
fn scanned_as_listed() {
    let mut fs = MemFs::new(Flavour::Posix);
    for (folder, files) in SCANNED {
        fs.insert(format!("/s/{folder}").as_bytes(), Node::Dir);
        for file in *files {
            fs.insert(
                format!("/s/{folder}/{file}").as_bytes(),
                Node::File(Vec::new()),
            );
        }
    }
    // The `.git` folder's own key, and keys below it.
    fs.insert(b"/s/dirgit/.git", Node::Dir);
    fs.insert(b"/s/ldirgit", Node::Link(b"/s/dirgit".to_vec()));
    fs.insert(b"/s/lafter", Node::Link(b"after".to_vec()));
    fs.insert(b"/s/afile", Node::File(Vec::new()));
    fs.lock(b"/s/locked");
    let mut paths: Vec<String> = SCANNED.iter().map(|(f, _)| format!("/s/{f}")).collect();
    for extra in ["ldirgit", "lafter", "afile", "nothing"] {
        paths.push(format!("/s/{extra}"));
    }
    let answers = |fs: &dyn Fn(&[u8]) -> Result<bool, PyErr>| {
        paths.iter().map(|p| fs(p.as_bytes())).collect::<Vec<_>>()
    };
    let want = vec![
        Ok(false),
        Ok(false),
        Ok(false),
        Ok(true),
        Ok(true),
        Ok(true),
        Ok(true),
        Ok(true),
        Ok(true),
        Ok(true),
        Ok(true),
        Err(PyErr::Permission),
        Ok(false),
        Ok(true),
        Err(PyErr::NotADirectory),
        Err(PyErr::FileNotFound),
    ];
    let scanned = |p: &[u8]| fs.holds_other_than(p, b".git").map_err(|e| e.class);
    assert_eq!(answers(&|p| listed(&fs, p)), want);
    assert_eq!(answers(&scanned), want);
    assert_eq!(
        fs.holds_other_than(b"/s/a\0b", b".git")
            .map_err(|e| e.class),
        Err(PyErr::Value)
    );
    #[cfg(unix)]
    {
        use bettermemory_core::fsdata::StdFs;
        use std::os::unix::ffi::OsStrExt;
        use std::os::unix::fs::{symlink, PermissionsExt};
        let base = scratch("scan");
        for (folder, files) in SCANNED {
            std::fs::create_dir(base.join(folder)).unwrap();
            for file in *files {
                let path = base.join(folder).join(file);
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(&path, b"").unwrap();
            }
        }
        symlink(base.join("dirgit"), base.join("ldirgit")).unwrap();
        symlink("after", base.join("lafter")).unwrap();
        std::fs::write(base.join("afile"), b"").unwrap();
        let locked = base.join("locked");
        std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).unwrap();
        let root = base.as_os_str().as_bytes().to_vec();
        let on_disk = |p: &[u8]| {
            let mut full = root.clone();
            full.extend_from_slice(&p[2..]);
            full
        };
        // Root reads a mode-000 folder; the rest holds there too.
        let refused = std::fs::read_dir(&locked).is_err();
        let listed_here = answers(&|p| listed(&StdFs, &on_disk(p)));
        let scanned_here = answers(&|p| {
            StdFs
                .holds_other_than(&on_disk(p), b".git")
                .map_err(|e| e.class)
        });
        let _ = std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o700));
        let _ = std::fs::remove_dir_all(&base);
        assert_eq!(scanned_here, listed_here);
        if refused {
            assert_eq!(scanned_here, want);
        }
    }
}

/// `Ctx::cwd` is the caller's contract: what `os.getcwd()` returned, so
/// physical (free of links). Handed one with a link, the port takes it as
/// given; pinned here so a change to that is seen.
#[test]
fn a_cwd_with_a_link_is_taken_as_given() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/real/w/.git", Node::Dir);
    fs.insert(b"/real/w/f", Node::File(Vec::new()));
    fs.insert(b"/l", Node::Link(b"/real".to_vec()));
    let c = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/l/w",
    };
    assert_eq!(tree_of(&c, b".git"), Ok(Some(b"/l/w".to_vec())));
    assert_eq!(tree_of(&c, b"../w/.git"), Ok(Some(b"/l/w".to_vec())));
    assert_eq!(same_folder(&c, b".", b"/real/w"), Ok(Some(true)));
}

#[test]
fn the_windows_flavour_declines() {
    let mut fs = MemFs::new(Flavour::Windows);
    fs.insert(b"C:/w/.git", Node::Dir);
    let c = Ctx {
        fs: &fs,
        flavour: Flavour::Windows,
        cwd: b"C:/",
    };
    let no = Err(Failure::NotPorted);
    assert_eq!(names_here(&c, b"C:/w/.git", b"C:/w", b"C:/w/.git"), no);
    assert_eq!(names_here(&c, b"C:/none", b"C:/w", b"C:/w/.git"), no);
    assert_eq!(names_folder(&c, b"C:/w/.git", b"C:/w", b"C:/w"), no);
    assert_eq!(same_folder(&c, b"C:/w", b"C:/w"), no);
    assert_eq!(tree_of(&c, b"C:/w/.git"), Err(Failure::NotPorted));
    assert_eq!(working_tree_of(&c, b"C:/w/.git"), Err(Failure::NotPorted));
    let st: StatResult = fs.lstat_result(b"C:/w").unwrap();
    assert_eq!(is_link(&st), Err(Failure::NotPorted));
}

#[test]
fn a_link_is_a_link_on_posix() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/d", Node::Dir);
    fs.insert(b"/l", Node::Link(b"/d".to_vec()));
    assert_eq!(is_link(&fs.lstat_result(b"/l").unwrap()), Ok(true));
    assert_eq!(is_link(&fs.lstat_result(b"/d").unwrap()), Ok(false));
}

#[cfg(unix)]
fn scratch(tag: &str) -> std::path::PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bm-identity-{tag}-{}-{n}", std::process::id()));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    std::fs::canonicalize(&dir).expect("resolved")
}

/// Run in a child whose working directory holds decoys of every relative
/// name: the reads and the identity helpers over `StdFs`, with `cwd` given
/// as the case's own root, must answer from the root alone.
#[cfg(unix)]
#[test]
fn a_relative_path_is_never_taken_from_the_process_directory() {
    use bettermemory_core::fsdata::StdFs;
    use bettermemory_core::gitfile_rule::{gitfile_target, small_text};
    const CHILD: &str = "BM_IDENTITY_DECOY_ROOT";
    let name = "a_relative_path_is_never_taken_from_the_process_directory";
    if let Some(root) = std::env::var_os(CHILD) {
        let root = root.into_string().expect("a UTF-8 root").into_bytes();
        let c = Ctx {
            fs: &StdFs,
            flavour: Flavour::Posix,
            cwd: &root,
        };
        let at = |rel: &[u8]| [&root[..], b"/", rel].concat();
        assert_eq!(gitfile_target(&c, b"w/.git"), Ok(Some(b"w/real".to_vec())));
        assert_eq!(small_text(&c, b"w/note"), Ok(Some("root".to_owned())));
        assert_eq!(small_text(&c, b"only-decoy"), Ok(None));
        assert_eq!(names_here(&c, b"w", b"", &at(b"w")), Ok(Some(true)));
        assert_eq!(names_here(&c, b"only-decoy", b"", &at(b"w")), Ok(None));
        assert_eq!(same_folder(&c, b"w", &at(b"w")), Ok(Some(true)));
        assert_eq!(same_folder(&c, b"only-decoy", &at(b"w")), Ok(None));
        assert_eq!(names_folder(&c, b"t/.git", b"", &at(b"t")), Ok(Some(true)));
        assert_eq!(tree_of(&c, b"t/.git"), Ok(Some(at(b"t"))));
        assert_eq!(working_tree_of(&c, b"t/.git"), Ok(None));
        return;
    }
    let base = scratch("decoy");
    let (root, decoys) = (base.join("root"), base.join("decoys"));
    for (dir, text) in [(&root, "root"), (&decoys, "decoy")] {
        std::fs::create_dir_all(dir.join("w")).unwrap();
        std::fs::write(dir.join("w/.git"), format!("gitdir: {text}")).unwrap();
        std::fs::write(dir.join("w/note"), text).unwrap();
    }
    std::fs::write(root.join("w/.git"), "gitdir: real").unwrap();
    std::fs::create_dir_all(root.join("t/.git")).unwrap();
    std::fs::create_dir_all(decoys.join("t/.git")).unwrap();
    std::fs::write(decoys.join("t/more"), "").unwrap();
    std::fs::write(decoys.join("only-decoy"), "decoy").unwrap();
    let status = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", name, "--test-threads=1"])
        .current_dir(&decoys)
        .env(CHILD, &root)
        .status()
        .expect("the child runs");
    let _ = std::fs::remove_dir_all(&base);
    assert!(status.success());
}

/// A name in NFD on disk, asked for in NFC: the answer is the platform's
/// own (macOS's file systems find it, Linux's do not), never a guess. Not
/// in the shared fixture, as the platforms differ.
#[cfg(unix)]
#[test]
fn nfc_against_nfd_is_answered_as_the_platform_answers() {
    use bettermemory_core::fsdata::StdFs;
    use std::os::unix::ffi::OsStrExt;
    let base = scratch("nfd");
    let nfd = base.join("e\u{301}");
    std::fs::create_dir(&nfd).unwrap();
    let nfc = base.join("\u{e9}");
    let finds = std::fs::metadata(&nfc).is_ok();
    let cwd = base.as_os_str().as_bytes().to_vec();
    let c = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: &cwd,
    };
    let got = same_folder(&c, nfc.as_os_str().as_bytes(), nfd.as_os_str().as_bytes());
    let named = names_here(&c, "\u{e9}".as_bytes(), &cwd, nfd.as_os_str().as_bytes());
    let _ = std::fs::remove_dir_all(&base);
    let want = if finds { Some(true) } else { None };
    assert_eq!(got, Ok(want));
    assert_eq!(named, Ok(want));
}

/// A stat without an inode (as Windows' is, not ported) is declined,
/// never read as another file.
#[test]
fn a_stat_without_an_inode_is_declined() {
    use bettermemory_core::fsdata::{Access, Kind, OsError, PyErr, Stat};
    struct NoIno(MemFs);
    impl Fs for NoIno {
        type File = <MemFs as Fs>::File;
        fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
            self.0.open(path)
        }
        fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
            self.0.fstat(file)
        }
        fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
            self.0.read(file, max)
        }
        fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
            self.0.access(path, mode)
        }
        fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
            self.0.open_no_follow(path)
        }
        fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
            self.0.lstat(path)
        }
        fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
            self.0.readlink(path)
        }
        fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
            self.0.stat(path).map(|st| StatResult { ino: None, ..st })
        }
        fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
            self.0
                .lstat_result(path)
                .map(|st| StatResult { ino: None, ..st })
        }
        fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
            self.0.listdir(path)
        }
    }
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w/.git", Node::Dir);
    fs.insert(b"/v", Node::Dir);
    let fs = NoIno(fs);
    let c = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    let no = Err(Failure::NotPorted);
    assert_eq!(same_folder(&c, b"/w", b"/v"), no);
    assert_eq!(names_here(&c, b"/w", b"/", b"/w"), no);
    assert_eq!(names_folder(&c, b"/w/.git", b"/", b"/w"), no);
    assert_eq!(tree_of(&c, b"/w/.git"), Err(Failure::NotPorted));
    assert_eq!(working_tree_of(&c, b"/w/.git"), Err(Failure::NotPorted));
}
