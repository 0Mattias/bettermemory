//! Cases a plausible wrong `parse` fails: the close of `_read_bounded`'s
//! `finally` raising over the body's answer, a read or an fstat refused,
//! reads asked for no more than is still wanted, a relative path taken
//! from the working directory, a NUL in the path, and `_os_text` without
//! a text or with an empty one, an fstat that disagrees with the stat, a
//! NUL reaching the open, and a lone surrogate under the Windows flavour.

use std::cell::RefCell;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFile, MemFs, Node, OsError, PyErr, Stat, StatResult,
};
use bettermemory_core::gitfile_rule::hostpath::text::Process;
use bettermemory_core::project_file::{
    evicted, parse, read_bounded, Fault, Found, Host, MAX_BYTES,
};
use bettermemory_core::pyvalue::PyStr;

const GOOD: &[u8] = b"[project]\nid = \"bmp_0123456789ABCDEFGHJKMNPQRS\"\n";

/// A `MemFs` whose fstat, reads and close can be made to fail, whose
/// reads answer at most `chunk` bytes, and which logs what each read asks.
struct Faulty {
    mem: MemFs,
    fstat: Option<OsError>,
    fstat_kind: Option<Kind>,
    read: Option<OsError>,
    close: Option<OsError>,
    chunk: usize,
    asked: RefCell<Vec<usize>>,
    texts: bool,
    empty_texts: bool,
}

impl Faulty {
    fn new(mem: MemFs) -> Faulty {
        Faulty {
            mem,
            fstat: None,
            fstat_kind: None,
            read: None,
            close: None,
            chunk: usize::MAX,
            asked: RefCell::new(Vec::new()),
            texts: true,
            empty_texts: false,
        }
    }
}

impl Fs for Faulty {
    type File = MemFile;
    fn open(&self, path: &[u8]) -> Result<MemFile, PyErr> {
        self.mem.open(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.mem.open_result(path)
    }
    fn fstat(&self, file: &mut MemFile) -> Result<Stat, PyErr> {
        self.mem.fstat(file)
    }
    fn fstat_result(&self, file: &mut MemFile) -> Result<Stat, OsError> {
        match self.fstat {
            Some(e) => Err(e),
            None => self.mem.fstat_result(file).map(|mut st| {
                if let Some(kind) = self.fstat_kind {
                    st.kind = kind;
                }
                st
            }),
        }
    }
    fn read(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, PyErr> {
        self.mem.read(file, max)
    }
    fn read_once(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, OsError> {
        self.asked.borrow_mut().push(max);
        if let Some(e) = self.read {
            return Err(e);
        }
        self.mem.read_once(file, max.min(self.chunk))
    }
    fn close(&self, file: MemFile) -> Result<(), OsError> {
        drop(file);
        self.close.map_or(Ok(()), Err)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.mem.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.mem.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.mem.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.mem.readlink(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.mem.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.mem.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.mem.listdir(path)
    }
    fn strerror(&self, error: &OsError) -> Option<String> {
        if self.empty_texts {
            Some(String::new())
        } else if self.texts {
            self.mem.strerror(error)
        } else {
            None
        }
    }
}

struct Cwd(&'static str);

impl Process for Cwd {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        if self.0.is_empty() {
            return Err(PyErr::FileNotFound);
        }
        Ok(cps(self.0))
    }
}

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn with(file: &[u8]) -> Faulty {
    let mut mem = MemFs::new(Flavour::Posix);
    mem.insert(b"/w/bettermemory.toml", Node::File(file.to_vec()));
    Faulty::new(mem)
}

fn run(fs: &Faulty, path: &str, cwd: &'static str) -> Result<Found, Fault> {
    let process = Cwd(cwd);
    let host = Host {
        fs,
        flavour: Flavour::Posix,
        process: &process,
        darwin: true,
    };
    parse(&host, &cps(path))
}

fn reason(found: Result<Found, Fault>) -> (String, String) {
    match found {
        Ok(Found::Error(e)) => (
            e.reason.name().to_owned(),
            e.detail
                .iter()
                .map(|&c| char::from_u32(c).unwrap())
                .collect(),
        ),
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_failed_close_raises_over_a_good_read() {
    let mut fs = with(GOOD);
    fs.close = Some(OsError::new(PyErr::Os, "EIO"));
    assert_eq!(
        run(&fs, "/w/bettermemory.toml", "/"),
        Err(Fault::Raised("OSError"))
    );
}

#[test]
fn a_failed_close_raises_over_a_read_error() {
    let mut fs = with(GOOD);
    fs.read = Some(OsError::new(PyErr::Permission, "EACCES"));
    fs.close = Some(OsError::new(PyErr::Interrupted, "EINTR"));
    let got = run(&fs, "/w/bettermemory.toml", "/");
    assert_eq!(got, Err(Fault::Raised("InterruptedError")));
}

#[test]
fn a_refused_read_or_fstat_is_unreadable_with_its_text() {
    let mut fs = with(GOOD);
    fs.read = Some(OsError::new(PyErr::Os, "EIO"));
    let want = ("unreadable".to_owned(), "Input/output error".to_owned());
    assert_eq!(reason(run(&fs, "/w/bettermemory.toml", "/")), want);
    let mut fs = with(GOOD);
    fs.fstat = Some(OsError::new(PyErr::Permission, "EACCES"));
    let want = ("unreadable".to_owned(), "Permission denied".to_owned());
    assert_eq!(reason(run(&fs, "/w/bettermemory.toml", "/")), want);
}

#[test]
fn reads_ask_only_what_is_still_wanted() {
    let mut fs = with(&vec![b'#'; MAX_BYTES + 5]);
    fs.chunk = 30000;
    let want = ("too_large".to_owned(), "larger than 65536 bytes".to_owned());
    assert_eq!(reason(run(&fs, "/w/bettermemory.toml", "/")), want);
    assert_eq!(*fs.asked.borrow(), vec![65537, 35537, 5537]);
}

#[test]
fn a_file_at_the_bound_is_read_to_its_end() {
    let mut body = GOOD.to_vec();
    body.push(b'#');
    body.resize(MAX_BYTES, b'a');
    let mut fs = with(&body);
    fs.chunk = 40000;
    assert!(matches!(
        run(&fs, "/w/bettermemory.toml", "/"),
        Ok(Found::File(_))
    ));
    assert_eq!(*fs.asked.borrow(), vec![65537, 25537, 1]);
}

#[test]
fn a_relative_path_is_taken_from_the_working_directory() {
    let fs = with(GOOD);
    match run(&fs, "x/../bettermemory.toml", "/w") {
        Ok(Found::File(f)) => {
            assert_eq!(f.path, cps("/w/bettermemory.toml"));
            assert_eq!(f.name, cps("w"));
        }
        other => panic!("{other:?}"),
    }
    assert_eq!(
        run(&fs, "bettermemory.toml", ""),
        Err(Fault::Raised("FileNotFoundError"))
    );
}

#[test]
fn a_nul_in_the_path_raises_value_error() {
    let fs = with(GOOD);
    assert_eq!(run(&fs, "/w/a\0b", "/"), Err(Fault::Raised("ValueError")));
}

#[test]
fn an_error_without_text_declines() {
    let mut fs = with(GOOD);
    fs.texts = false;
    assert_eq!(run(&fs, "/w/missing", "/"), Err(Fault::NotPorted));
}

#[test]
fn a_missing_file_is_unreadable() {
    let fs = with(GOOD);
    let want = (
        "unreadable".to_owned(),
        "No such file or directory".to_owned(),
    );
    assert_eq!(reason(run(&fs, "/w/missing", "/")), want);
}

#[test]
fn eviction_reads_the_flags_on_darwin_alone() {
    let mut st = StatResult {
        kind: Kind::Regular,
        mode: None,
        ino: None,
        dev: None,
        size: 0,
        mtime_ns: 0,
        ctime_ns: None,
        flags: 0x4000_0000,
        file_attributes: 0,
        reparse_tag: Some(0),
    };
    assert!(evicted(&st, true));
    assert!(!evicted(&st, false));
    st.flags = 0;
    st.file_attributes = 0x0040_0000;
    assert!(evicted(&st, false));
    st.file_attributes = 0x0000_0400;
    assert!(!evicted(&st, true));
}

#[test]
fn an_id_with_a_trailing_newline_is_malformed() {
    let fs = with(b"[project]\nid = \"bmp_0123456789ABCDEFGHJKMNPQRS\\n\"\n");
    assert_eq!(
        reason(run(&fs, "/w/bettermemory.toml", "/")).0,
        "id_malformed"
    );
}

#[test]
fn an_empty_error_text_declines() {
    // `exc.strerror or str(exc)`: an empty strerror falls to str(exc).
    let mut fs = with(GOOD);
    fs.empty_texts = true;
    assert_eq!(run(&fs, "/w/missing", "/"), Err(Fault::NotPorted));
}

#[test]
fn an_fstat_that_is_not_regular_is_not_a_file() {
    // The stat said regular; the file was swapped before the open.
    for kind in [Kind::Fifo, Kind::Directory] {
        let mut fs = with(GOOD);
        fs.fstat_kind = Some(kind);
        let want = ("not_a_file".to_owned(), "not a regular file".to_owned());
        assert_eq!(reason(run(&fs, "/w/bettermemory.toml", "/")), want);
    }
}

#[cfg(unix)]
#[test]
fn a_nul_reaching_the_open_raises_value_error() {
    let process = Cwd("/");
    let host = Host {
        fs: &bettermemory_core::fsdata::StdFs,
        flavour: Flavour::Posix,
        process: &process,
        darwin: cfg!(target_os = "macos"),
    };
    assert_eq!(
        read_bounded(&host, &cps("/nonexistent/a\0b")),
        Err(Fault::Raised("ValueError"))
    );
}

#[test]
fn a_lone_surrogate_under_windows_declines() {
    let mem = MemFs::new(Flavour::Windows);
    let process = Cwd("C:\\");
    let host = Host {
        fs: &mem,
        flavour: Flavour::Windows,
        process: &process,
        darwin: false,
    };
    let mut path = cps("C:\\w\\");
    path.push(0xD800);
    assert_eq!(read_bounded(&host, &path), Err(Fault::NotPorted));
}
