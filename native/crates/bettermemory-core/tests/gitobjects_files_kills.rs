//! Answers of `gitobjects::files` that a plausible wrong port misses. Each
//! expectation was run on the reference (`gen_gitobjects_files`'s cases
//! or the same trees by hand); the trees are in memory, so every case runs
//! on every platform.

mod common;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFile, MemFs, Node, OsError, PyErr, Refused, Stat, StatResult,
};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitobjects::files::{
    is_evicted, FsHandle, ObjectReader, Stop, ALT_DEPTH, RECALL_ON_DATA_ACCESS, SF_DATALESS,
};
use bettermemory_core::gitobjects::stream::Handle;

const FACTS: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

fn tree(entries: &[(&str, Node)]) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/m/g/objects/pack", Node::Dir);
    for (path, node) in entries {
        fs.insert(path.as_bytes(), node.clone());
    }
    fs
}

fn reader(fs: &MemFs) -> ObjectReader<'_, MemFs> {
    ObjectReader::new(fs, Flavour::Posix, b"/", FACTS, b"/m/g")
}

fn file(text: &[u8]) -> Node {
    Node::File(text.to_vec())
}

/// A version 2 index of one sha1 name, as the generator's `_idx`.
fn idx1() -> Vec<u8> {
    let mut out = b"\xfftOc\x00\x00\x00\x02".to_vec();
    for b in 0..256u32 {
        out.extend_from_slice(&u32::from(b >= 1).to_be_bytes());
    }
    out.extend_from_slice(&[1; 20]);
    out.extend_from_slice(&[0; 4]);
    out.extend_from_slice(&12u32.to_be_bytes());
    out.extend_from_slice(&[0; 40]);
    out
}

fn stat_with(flags: u32, attributes: u32) -> StatResult {
    StatResult {
        kind: Kind::Regular,
        mode: None,
        ino: None,
        dev: None,
        size: 0,
        mtime_ns: 0,
        ctime_ns: None,
        flags,
        file_attributes: attributes,
        reparse_tag: None,
    }
}

#[test]
fn eviction_is_either_bit_alone_and_no_other() {
    assert!(is_evicted(&stat_with(SF_DATALESS, 0)));
    assert!(is_evicted(&stat_with(0, RECALL_ON_DATA_ACCESS)));
    assert!(!is_evicted(&stat_with(
        !SF_DATALESS,
        !RECALL_ON_DATA_ACCESS
    )));
    // RECALL_ON_OPEN (0x40000) is not the reference's bit, unlike
    // `gitfile_rule`'s placeholder test.
    assert!(!is_evicted(&stat_with(0, 0x0004_0000)));
}

#[test]
fn the_alternates_depth_lists_six_levels_and_reads_no_seventh() {
    assert_eq!(ALT_DEPTH, 5);
    let mut entries = vec![("/m/g/objects/info/alternates".to_owned(), file(b"/m/x1\n"))];
    for i in 1..=8 {
        entries.push((
            format!("/m/x{i}/info/alternates"),
            file(format!("/m/x{}\n", i + 1).as_bytes()),
        ));
    }
    let entries: Vec<(&str, Node)> = entries
        .iter()
        .map(|(p, n)| (p.as_str(), n.clone()))
        .collect();
    let fs = tree(&entries);
    let dirs = reader(&fs).object_dirs().unwrap();
    assert_eq!(dirs.len(), 7, "the objects directory and x1 to x6");
    assert_eq!(dirs.last().unwrap(), b"/m/x6");
}

#[test]
fn a_nul_raises_once_and_the_state_it_left_answers_after() {
    let fs = tree(&[
        ("/m/a/objects", Node::Dir),
        (
            "/m/g/objects/info/alternates",
            file(b"/m/a/objects\n\"x\\000y\"\n"),
        ),
    ]);
    let mut r = reader(&fs);
    assert_eq!(r.complete(), Err(Stop::Raised(PyErr::Value)));
    // Not loaded again, and never complete: no directory was kept.
    assert_eq!(r.complete(), Ok(false));
    assert_eq!(r.object_dirs(), Ok(Vec::new()));
    assert_eq!(r.hash_len, Some(20));
}

#[test]
fn evicted_loads_nothing() {
    let fs = tree(&[("/m/g/config", file(b""))]);
    let mut fs = fs;
    fs.set_flags(b"/m/g/config", SF_DATALESS);
    let mut r = reader(&fs);
    assert!(r.evicted().is_empty());
    assert_eq!(r.complete(), Ok(false));
    assert_eq!(r.evicted(), vec![b"/m/g/config".to_vec()]);
    assert_eq!(r.hash_len, None, "an evicted config is no format, not sha1");
}

#[test]
fn an_index_listed_without_its_pack_is_read_by_a_fresh_reader() {
    let mut fs = tree(&[("/m/g/objects/pack/p.idx", Node::File(idx1()))]);
    {
        let mut r = reader(&fs);
        assert_eq!(r.complete(), Ok(true));
        assert!(r.packs.is_empty());
    }
    fs.insert(b"/m/g/objects/pack/p.pack", file(b""));
    let mut r = reader(&fs);
    // A fresh reader reads it; the earlier one, rescanning, would not.
    assert_eq!(r.complete(), Ok(true));
    assert_eq!(r.packs.len(), 1);
    assert_eq!(r.rescan(), Ok(0));
}

#[test]
fn indexes_are_listed_in_byte_order() {
    let names = ["bx", "Zx", "a0x", "_x"];
    let mut entries = Vec::new();
    for n in names {
        entries.push((format!("/m/g/objects/pack/{n}.idx"), Node::File(idx1())));
        entries.push((format!("/m/g/objects/pack/{n}.pack"), file(b"")));
    }
    let entries: Vec<(&str, Node)> = entries
        .iter()
        .map(|(p, n)| (p.as_str(), n.clone()))
        .collect();
    let fs = tree(&entries);
    let mut r = reader(&fs);
    r.load().unwrap();
    let packs: Vec<&[u8]> = r.packs.iter().map(|p| p.pack.as_slice()).collect();
    assert_eq!(
        packs,
        [
            &b"/m/g/objects/pack/Zx.pack"[..],
            b"/m/g/objects/pack/_x.pack",
            b"/m/g/objects/pack/a0x.pack",
            b"/m/g/objects/pack/bx.pack",
        ]
    );
}

#[test]
fn a_relative_entry_resolves_against_the_real_path() {
    let fs = tree(&[
        ("/m/real/o/pack", Node::Dir),
        ("/m/real/x", Node::Dir),
        ("/m/g/x", Node::Dir),
        ("/m/g/objects", Node::Link(b"/m/real/o".to_vec())),
        ("/m/real/o/info/alternates", file(b"../x\n")),
    ]);
    let dirs = reader(&fs).object_dirs().unwrap();
    assert_eq!(dirs, vec![b"/m/g/objects".to_vec(), b"/m/real/x".to_vec()]);
}

#[test]
fn an_unreadable_config_reads_as_sha1_and_complete() {
    let mut fs = tree(&[(
        "/m/g/config",
        file(b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n"),
    )]);
    fs.set_mode(b"/m/g/config", 0);
    let mut r = reader(&fs);
    assert_eq!(r.complete(), Ok(true));
    assert_eq!(r.hash_len, Some(20));
}

#[test]
fn a_fifo_config_is_no_config() {
    let fs = tree(&[("/m/g/config", Node::Fifo)]);
    let mut r = reader(&fs);
    assert_eq!(r.complete(), Ok(true));
    assert_eq!(r.hash_len, Some(20));
}

/// `MemFs` that may answer every `fstat` with another kind (the file
/// replaced between the `stat` and the open) and every read with
/// BlockingIO (a non-blocking read with nothing yet).
struct Lying {
    inner: MemFs,
    fstat_kind: Option<Kind>,
    blocking: bool,
}

impl Fs for Lying {
    type File = MemFile;
    fn open(&self, path: &[u8]) -> Result<MemFile, PyErr> {
        self.inner.open(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.inner.open_result(path)
    }
    fn fstat(&self, file: &mut MemFile) -> Result<Stat, PyErr> {
        let mut st = self.inner.fstat(file)?;
        if let Some(kind) = self.fstat_kind {
            st.kind = kind;
        }
        Ok(st)
    }
    fn read(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, PyErr> {
        self.inner.read(file, max)
    }
    fn read_once(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, OsError> {
        if self.blocking {
            return Err(OsError {
                class: PyErr::BlockingIO,
                errno: Some("EAGAIN"),
                winerror: None,
            });
        }
        self.inner.read_once(file, max)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.inner.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.readlink(path)
    }
    fn readlink_result(&self, path: &[u8]) -> Result<Vec<u8>, OsError> {
        self.inner.readlink_result(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.listdir(path)
    }
    fn seek(&self, file: &mut MemFile, offset: u64) -> Result<(), Refused> {
        self.inner.seek(file, offset)
    }
}

const SHA256_CONFIG: &[u8] =
    b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n";

#[test]
fn a_file_no_longer_regular_once_open_is_not_read() {
    // `_open` asks again after the open: `stat` says regular, `fstat`
    // says FIFO, so the config is missing and the format sha1.
    let fs = Lying {
        inner: tree(&[("/m/g/config", file(SHA256_CONFIG))]),
        fstat_kind: Some(Kind::Fifo),
        blocking: false,
    };
    let mut r = ObjectReader::new(&fs, Flavour::Posix, b"/", FACTS, b"/m/g");
    assert_eq!(r.read_whole(b"/m/g/config"), Ok(None));
    assert_eq!(r.complete(), Ok(true));
    assert_eq!(r.hash_len, Some(20));
    let honest = Lying {
        inner: tree(&[("/m/g/config", file(SHA256_CONFIG))]),
        fstat_kind: None,
        blocking: false,
    };
    let mut r = ObjectReader::new(&honest, Flavour::Posix, b"/", FACTS, b"/m/g");
    assert_eq!(r.complete(), Ok(true));
    assert_eq!(r.hash_len, Some(32));
}

#[test]
fn a_read_that_would_block_at_once_is_none_not_empty() {
    // `readall` answers None when the first read has nothing yet.
    let fs = Lying {
        inner: tree(&[
            ("/m/g/objects/pack/p.idx", Node::File(idx1())),
            ("/m/g/objects/pack/p.pack", file(b"")),
        ]),
        fstat_kind: None,
        blocking: true,
    };
    let mut r = ObjectReader::new(&fs, Flavour::Posix, b"/", FACTS, b"/m/g");
    assert_eq!(r.read_whole(b"/m/g/objects/pack/p.idx"), Ok(None));
    assert_eq!(r.complete(), Ok(false));
    assert!(r.unreadable);
    assert!(r.packs.is_empty());
}

#[test]
fn an_evicted_path_opened_twice_is_recorded_once() {
    let mut fs = tree(&[("/m/g/objects/x", file(b"x"))]);
    fs.set_flags(b"/m/g/objects/x", SF_DATALESS);
    let mut r = reader(&fs);
    assert!(r.open(b"/m/g/objects/x").unwrap().is_none());
    assert!(r.open(b"/m/g/objects/x").unwrap().is_none());
    assert_eq!(r.evicted(), vec![b"/m/g/objects/x".to_vec()]);
}

#[test]
fn the_windows_flavour_declines_where_a_path_is_resolved() {
    let mut fs = MemFs::new(Flavour::Windows);
    fs.insert(b"/m/g/objects", Node::Dir);
    let mut r = ObjectReader::new(&fs, Flavour::Windows, b"/", FACTS, b"/m/g");
    assert_eq!(r.complete(), Err(Stop::NotPorted));
    let mut none = MemFs::new(Flavour::Windows);
    none.insert(b"/m/g", Node::Dir);
    let mut r = ObjectReader::new(&none, Flavour::Windows, b"/", FACTS, b"/m/g");
    assert_eq!(r.complete(), Ok(false));
}

#[test]
fn a_handle_over_an_open_file_seeks_and_reads() {
    let fs = tree(&[("/m/f", file(b"0123456789")), ("/m/p", Node::Fifo)]);
    let file = fs.open(b"/m/f").unwrap();
    let mut h = FsHandle { fs: &fs, file };
    h.seek(7).unwrap();
    assert_eq!(h.read(5).unwrap(), b"789");
    assert_eq!(h.read(5).unwrap(), b"");
    h.seek(100).unwrap();
    assert_eq!(h.read(1).unwrap(), b"");
    h.seek(2).unwrap();
    assert_eq!(h.read(3).unwrap(), b"234");
    let mut fifo = fs.open(b"/m/p").unwrap();
    assert_eq!(fs.seek(&mut fifo, 0), Err(Refused::Raised(PyErr::Os)));
    let mut h = FsHandle {
        fs: &fs,
        file: fifo,
    };
    assert_eq!(
        h.read(4).unwrap(),
        b"",
        "a FIFO with nothing reads as empty"
    );
}

#[cfg(unix)]
#[test]
fn the_std_seek_goes_past_the_end_and_a_fifo_refuses() {
    use bettermemory_core::fsdata::StdFs;
    use std::os::unix::ffi::OsStrExt;
    let dir = std::env::temp_dir().join(format!("bm-gobj-seek-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("f"), b"abcdef").unwrap();
    let made = std::process::Command::new("mkfifo")
        .arg(dir.join("p"))
        .status()
        .unwrap();
    assert!(made.success());
    let fs = StdFs;
    let file = fs.open(dir.join("f").as_os_str().as_bytes()).unwrap();
    let mut h = FsHandle { fs: &fs, file };
    h.seek(4).unwrap();
    assert_eq!(h.read(10).unwrap(), b"ef");
    h.seek(1 << 40).unwrap();
    assert_eq!(h.read(10).unwrap(), b"");
    // The open does not wait on a FIFO, and a seek on it is ESPIPE.
    let mut fifo = fs.open(dir.join("p").as_os_str().as_bytes()).unwrap();
    assert!(matches!(fs.seek(&mut fifo, 0), Err(Refused::Raised(_))));
    drop(fifo);
    std::fs::remove_dir_all(&dir).unwrap();
}
