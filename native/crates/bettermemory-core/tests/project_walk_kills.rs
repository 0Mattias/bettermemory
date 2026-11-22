//! Cases a plausible wrong walk or `write` fails, over `MemFs` and a
//! scripted disk, so they run on every host.

use std::cell::RefCell;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFile, MemFs, Node, OsError, PyErr, Stat, StatResult,
};
use bettermemory_core::gitfile_rule::hostpath::text::Process;
use bettermemory_core::project_file::{Above, Found, ProjectFile};
use bettermemory_core::project_walk::{
    above_start, find, is_relative_to, look, path_eq, pure_text, write, Fault, Placement, Walk,
    WriteEffects,
};
use bettermemory_core::pyvalue::PyStr;

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

struct Cwd(Option<&'static str>);

impl Process for Cwd {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        self.0.map(cps).ok_or(PyErr::FileNotFound)
    }
}

fn file(path: &str, boundary: bool) -> Found {
    Found::File(ProjectFile {
        id: cps("bmp_0123456789ABCDEFGHJKMNPQRS"),
        name: cps("n"),
        boundary,
        path: cps(path),
    })
}

/// Every looked-at path answers a file, boundary where `boundary` says.
fn run(
    fs: &MemFs,
    flavour: Flavour,
    cwd: &str,
    root: Option<&str>,
    above: Option<Above>,
    seen: &RefCell<Vec<(String, Kind)>>,
) -> Result<Walk, Fault> {
    let mut look = |path: &[u32], st: &StatResult| -> Result<Found, Fault> {
        let text: String = path.iter().map(|&c| char::from_u32(c).unwrap()).collect();
        seen.borrow_mut().push((text.clone(), st.kind));
        Ok(file(&text, text.contains("edge")))
    };
    let root = root.map(cps);
    find(
        fs,
        flavour,
        &Cwd(Some("/r")),
        &cps(cwd),
        root.as_deref(),
        above,
        &mut look,
    )
}

fn tree(paths: &[&str]) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    for p in paths {
        fs.insert(p.as_bytes(), Node::File(b"x".to_vec()));
    }
    fs
}

#[test]
fn a_linked_file_is_stat_through_the_link() {
    let mut fs = tree(&["/r/real"]);
    fs.insert(b"/r/a/bettermemory.toml", Node::Link(b"/r/real".to_vec()));
    let seen = RefCell::new(Vec::new());
    run(&fs, Flavour::Posix, "/r/a", Some("/r/a"), None, &seen).unwrap();
    assert_eq!(
        seen.borrow()[0],
        ("/r/a/bettermemory.toml".into(), Kind::Regular)
    );
}

#[test]
fn the_root_bounds_inner_and_the_rest_is_above() {
    let fs = tree(&[
        "/r/bettermemory.toml",
        "/r/a/bettermemory.toml",
        "/r/a/b/bettermemory.toml",
    ]);
    let seen = RefCell::new(Vec::new());
    let w = run(&fs, Flavour::Posix, "/r/a/b", Some("/r/a"), None, &seen).unwrap();
    assert_eq!(w.inner.len(), 2);
    assert_eq!(w.above.start, Some(cps("/r")));
    assert_eq!(w.above.items, vec![file("/r/bettermemory.toml", false)]);
    assert_eq!(w.root().unwrap().path, cps("/r/a/bettermemory.toml"));
    assert_eq!(w.nearest().unwrap().path, cps("/r/a/b/bettermemory.toml"));
    let enclosing = w.enclosing()[0].clone();
    assert_eq!(w.placement(&enclosing), Ok(Placement::Enclosing));
}

#[test]
fn a_cwd_outside_the_root_starts_at_the_root() {
    let fs = tree(&["/r/s/bettermemory.toml", "/r/a/bettermemory.toml"]);
    let seen = RefCell::new(Vec::new());
    let w = run(&fs, Flavour::Posix, "/r/s", Some("/r/a"), None, &seen).unwrap();
    assert_eq!(w.inner, vec![file("/r/a/bettermemory.toml", false)]);
    assert_eq!(w.cwd, cps("/r/s"));
}

#[test]
fn a_prefix_is_not_a_parent() {
    assert!(!is_relative_to(Flavour::Posix, &cps("/r/ab"), &cps("/r/a")));
    assert!(is_relative_to(Flavour::Posix, &cps("/r/a/b"), &cps("/r/a")));
    assert!(is_relative_to(Flavour::Posix, &cps("/r"), &cps("/")));
}

#[test]
fn a_cached_above_is_not_read_again() {
    let mut fs = tree(&["/r/a/bettermemory.toml"]);
    let seen = RefCell::new(Vec::new());
    let first = run(&fs, Flavour::Posix, "/r/a", Some("/r/a"), None, &seen).unwrap();
    fs.insert(b"/r/bettermemory.toml", Node::File(b"x".to_vec()));
    seen.borrow_mut().clear();
    let w = run(
        &fs,
        Flavour::Posix,
        "/r/a",
        Some("/r/a"),
        Some(first.above),
        &seen,
    )
    .unwrap();
    assert!(w.above.items.is_empty());
    assert_eq!(seen.borrow().len(), 1);
}

#[test]
fn a_mismatched_cache_names_both_starts() {
    let fs = tree(&[]);
    let seen = RefCell::new(Vec::new());
    let cached = Above {
        start: None,
        items: Vec::new(),
    };
    let err = run(
        &fs,
        Flavour::Posix,
        "/r/a",
        Some("/r/a"),
        Some(cached),
        &seen,
    )
    .unwrap_err();
    assert_eq!(
        err,
        Fault::Value(cps("the cached walk starts at None, this one at /r"))
    );
    assert!(seen.borrow().is_empty());
}

#[test]
fn without_a_repository_the_first_file_is_the_folder() {
    let fs = tree(&["/r/a/bettermemory.toml", "/r/edge/bettermemory.toml"]);
    let seen = RefCell::new(Vec::new());
    let w = run(&fs, Flavour::Posix, "/r/edge/x/../", None, None, &seen).unwrap();
    assert_eq!(w.cwd, cps("/r/edge"));
    assert_eq!(w.folder().unwrap().path, cps("/r/edge/bettermemory.toml"));
    assert!(w.enclosing().is_empty());
    assert_eq!(w.boundaries().len(), 1);
    let folder = w.folder().unwrap().clone();
    assert_eq!(w.placement(&folder), Ok(Placement::Folder));
    let stranger = ProjectFile {
        path: cps("/elsewhere"),
        ..folder
    };
    assert_eq!(
        w.placement(&stranger),
        Err(Fault::Value(cps("/elsewhere is not on this walk")))
    );
}

#[test]
fn a_relative_cwd_asks_the_process() {
    let fs = tree(&[]);
    let mut look = |_: &[u32], _: &StatResult| -> Result<Found, Fault> { unreachable!() };
    let err = find(
        &fs,
        Flavour::Posix,
        &Cwd(None),
        &cps("a"),
        None,
        None,
        &mut look,
    );
    assert_eq!(err, Err(Fault::Cwd(PyErr::FileNotFound)));
    let start = above_start(Flavour::Posix, &Cwd(Some("/r")), &cps("/"), Some(&cps("/")));
    assert_eq!(start, Ok(None));
}

#[test]
fn windows_paths_compare_without_case() {
    assert!(path_eq(
        Flavour::Windows,
        &cps("C:\\Repo"),
        &cps("c:\\repo")
    ));
    assert!(!path_eq(Flavour::Posix, &cps("/Repo"), &cps("/repo")));
    let mut fs = MemFs::new(Flavour::Windows);
    fs.insert(b"c:\\repo\\bettermemory.toml", Node::File(b"x".to_vec()));
    fs.insert(b"c:\\repo\\a", Node::Dir);
    let seen = RefCell::new(Vec::new());
    let w = run(
        &fs,
        Flavour::Windows,
        "c:\\repo\\a",
        Some("C:\\Repo"),
        None,
        &seen,
    )
    .unwrap();
    assert_eq!(w.inner.len(), 1);
    assert!(w.root().is_some());
    assert_eq!(w.above.start, Some(cps("C:\\")));
}

/// `str(PureWindowsPath(t))` gives a drive-only UNC path the root
/// separator `ntpath.abspath` leaves off, and no other text changes.
#[test]
fn a_bare_unc_share_reads_as_its_path_text() {
    let w = Flavour::Windows;
    for (text, pure) in [
        ("\\\\srv\\sh", "\\\\srv\\sh\\"),
        ("\\\\?\\UNC\\srv\\sh", "\\\\?\\UNC\\srv\\sh\\"),
        ("\\\\srv\\sh\\", "\\\\srv\\sh\\"),
        ("\\\\.\\x", "\\\\.\\x"),
        ("\\\\?\\x", "\\\\?\\x"),
        ("\\\\srv", "\\\\srv"),
        ("\\\\\\x\\y", "\\\\\\x\\y"),
        ("C:\\", "C:\\"),
    ] {
        assert_eq!(pure_text(w, &cps(text)), cps(pure), "{text}");
    }
    assert_eq!(pure_text(Flavour::Posix, &cps("//srv/sh")), cps("//srv/sh"));
    assert!(path_eq(w, &cps("\\\\srv\\sh"), &cps("\\\\SRV\\sh\\")));
    assert!(is_relative_to(
        w,
        &cps("\\\\srv\\sh\\b\\A\\\u{df}"),
        &cps("\\\\srv\\sh")
    ));
    assert_eq!(
        above_start(w, &Cwd(None), &cps("\\\\srv\\sh\\b"), None),
        Ok(Some(cps("\\\\srv\\sh\\b")))
    );
    assert_eq!(
        above_start(
            w,
            &Cwd(None),
            &cps("\\\\srv\\sh\\b"),
            Some(&cps("\\\\srv\\sh"))
        ),
        Ok(None)
    );
}

/// The reference: with the repository at a bare share, the share's file
/// is the root and the cwd's is nested.
#[test]
fn a_repository_at_a_bare_unc_share_keeps_its_root() {
    let mut fs = MemFs::new(Flavour::Windows);
    fs.insert(b"\\\\srv\\sh\\bettermemory.toml", Node::File(b"x".to_vec()));
    fs.insert(
        b"\\\\srv\\sh\\b\\bettermemory.toml",
        Node::File(b"x".to_vec()),
    );
    let seen = RefCell::new(Vec::new());
    let w = run(
        &fs,
        Flavour::Windows,
        "\\\\srv\\sh\\b",
        Some("\\\\srv\\sh"),
        None,
        &seen,
    )
    .unwrap();
    assert_eq!(w.cwd, cps("\\\\srv\\sh\\b"));
    assert_eq!(w.repository_root, Some(cps("\\\\srv\\sh\\")));
    assert_eq!(w.inner.len(), 2);
    let root = w.root().expect("the share's file is the root");
    assert_eq!(root.path, cps("\\\\srv\\sh\\bettermemory.toml"));
    assert_eq!(w.placement(root), Ok(Placement::Root));
    let nested = w.nested();
    assert_eq!(nested.len(), 1);
    assert_eq!(nested[0].path, cps("\\\\srv\\sh\\b\\bettermemory.toml"));
    assert_eq!(
        w.nearest().map(|f| f.path.clone()),
        Some(nested[0].path.clone())
    );
    assert_eq!(w.above.start, None);
    let cached = Above {
        start: Some(cps("\\\\srv\\sh")),
        items: Vec::new(),
    };
    let w = run(
        &fs,
        Flavour::Windows,
        "\\\\srv\\sh",
        None,
        Some(cached),
        &seen,
    )
    .unwrap();
    assert_eq!(w.cwd, cps("\\\\srv\\sh\\"));
}

/// A stat that fails with an errno whose text is empty: `_look` would
/// carry the empty text, which the port has no source for, so it declines.
struct EmptyText(MemFs);

impl Fs for EmptyText {
    type File = MemFile;
    fn open(&self, path: &[u8]) -> Result<MemFile, PyErr> {
        self.0.open(path)
    }
    fn fstat(&self, file: &mut MemFile) -> Result<Stat, PyErr> {
        self.0.fstat(file)
    }
    fn read(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, PyErr> {
        self.0.read(file, max)
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
    fn stat(&self, _: &[u8]) -> Result<StatResult, OsError> {
        Err(OsError::new(PyErr::Permission, "EACCES"))
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.0.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.0.listdir(path)
    }
    fn strerror(&self, _: &OsError) -> Option<String> {
        Some(String::new())
    }
}

#[test]
fn an_empty_strerror_text_declines() {
    let fs = EmptyText(MemFs::new(Flavour::Posix));
    let mut look_file =
        |_: &[u32], _: &StatResult| -> Result<Found, Fault> { Ok(file("/x", false)) };
    let got = look(&fs, &cps("/x/bettermemory.toml"), &mut look_file);
    assert!(matches!(got, Err(Fault::NotPorted)), "{got:?}");
}

#[derive(Default)]
struct Script {
    exists: bool,
    write_counts: Vec<usize>,
    write_fails: bool,
    close_fails: bool,
    log: RefCell<Vec<String>>,
}

impl WriteEffects for Script {
    type File = ();
    fn is_dir(&self, _: &[u32]) -> bool {
        self.log.borrow_mut().push("is_dir".into());
        true
    }
    fn create_excl(&self, _: &[u32], mode: u32) -> Result<(), OsError> {
        self.log.borrow_mut().push(format!("create {mode:o}"));
        if self.exists {
            return Err(OsError::new(PyErr::FileExists, "EEXIST"));
        }
        Ok(())
    }
    fn write_once(&self, _: &mut (), data: &[u8]) -> Result<usize, OsError> {
        let n = self
            .log
            .borrow()
            .iter()
            .filter(|l| l.starts_with("write"))
            .count();
        self.log.borrow_mut().push(format!("write {}", data.len()));
        if self.write_fails {
            return Err(OsError::new(PyErr::Os, "EIO"));
        }
        Ok(self.write_counts.get(n).copied().unwrap_or(data.len()))
    }
    fn fsync_file(&self, _: &mut ()) {
        self.log.borrow_mut().push("fsync".into());
    }
    fn close(&self, _: ()) -> Result<(), OsError> {
        self.log.borrow_mut().push("close".into());
        if self.close_fails {
            return Err(OsError::new(PyErr::Os, "EBADF"));
        }
        Ok(())
    }
    fn unlink_quietly(&self, _: &[u32]) {
        self.log.borrow_mut().push("unlink".into());
    }
    fn atomic_write_bytes(&self, _: &[u32], data: &[u8], mode: u32) -> Result<(), Fault> {
        self.log
            .borrow_mut()
            .push(format!("atomic {} {mode:o}", data.len()));
        Ok(())
    }
    fn fsync_dir(&self, _: &[u32]) {
        self.log.borrow_mut().push("fsync_dir".into());
    }
}

const RANDOM: [u8; 17] = [0x42; 17];

fn go(script: &Script, name: &str, rebind: bool) -> Result<ProjectFile, Fault> {
    write(
        script,
        Flavour::Posix,
        &Cwd(Some("/r")),
        &cps("d"),
        &cps(name),
        false,
        rebind,
        &RANDOM,
    )
}

#[test]
fn write_takes_every_partial_write_then_syncs() {
    let script = Script {
        write_counts: vec![10, 5],
        ..Script::default()
    };
    let p = go(&script, "n", false).unwrap();
    assert_eq!(p.path, cps("/r/d/bettermemory.toml"));
    let log = script.log.borrow();
    assert_eq!(log[1], "create 644");
    assert!(
        log[2].starts_with("write ")
            && log[3].starts_with("write ")
            && log[4].starts_with("write ")
    );
    assert_eq!(&log[5..], ["fsync", "close", "fsync_dir"]);
}

#[test]
fn a_close_error_wins_and_the_file_is_removed() {
    let script = Script {
        write_fails: true,
        close_fails: true,
        ..Script::default()
    };
    let err = go(&script, "n", false).unwrap_err();
    assert_eq!(err, Fault::Os(OsError::new(PyErr::Os, "EBADF")));
    assert_eq!(script.log.borrow().last().unwrap(), "unlink");
    let script = Script {
        write_fails: true,
        ..Script::default()
    };
    assert_eq!(
        go(&script, "n", false).unwrap_err(),
        Fault::Os(OsError::new(PyErr::Os, "EIO"))
    );
}

#[test]
fn an_existing_file_is_left_alone() {
    let script = Script {
        exists: true,
        ..Script::default()
    };
    let err = go(&script, "n", false).unwrap_err();
    assert_eq!(err, Fault::Os(OsError::new(PyErr::FileExists, "EEXIST")));
    assert_eq!(*script.log.borrow(), ["is_dir", "create 644"]);
}

#[test]
fn rebind_replaces_atomically_and_a_bad_name_comes_first() {
    let script = Script::default();
    go(&script, "n", true).unwrap();
    assert_eq!(*script.log.borrow(), ["is_dir", "atomic 270 644"]);
    let script = Script::default();
    assert!(matches!(
        go(&script, "a\u{85}", false),
        Err(Fault::Value(_))
    ));
    assert!(script.log.borrow().is_empty());
    let script = Script::default();
    assert_eq!(go(&script, "\u{d7ff}\u{e000}", false).map(|_| ()), Ok(()));
}
