//! The leaves of the `.git` rule on hostile input: typed answers, no
//! panic, reads bounded by count, nothing timed.

mod common;

use std::cell::Cell;
use std::collections::BTreeSet;

use bettermemory_core::fsdata::{Flavour, Fs, Kind, MemFs, Node, PyErr, Stat};
use bettermemory_core::gitfile_rule::bounded::{bounded_read, bounded_reads, Hung, Job, Read};
use bettermemory_core::gitfile_rule::format::{object_format, GitFacts};
use bettermemory_core::gitfile_rule::probe::{path_gone, path_missing, Raised};
use bettermemory_core::gitfile_rule::{
    gitfile_target, headref_content, parse_gitfile, small_text, GITFILE_LIMIT,
};

/// A file system that counts the bytes it hands back.
struct Counting {
    inner: MemFs,
    read: Cell<usize>,
}

impl Fs for Counting {
    type File = <MemFs as Fs>::File;
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.inner.open(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        let chunk = self.inner.read(file, max)?;
        self.read.set(self.read.get() + chunk.len());
        Ok(chunk)
    }
    fn access_read(&self, path: &[u8]) -> bool {
        self.inner.access_read(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.readlink(path)
    }
    fn stat(
        &self,
        path: &[u8],
    ) -> Result<bettermemory_core::fsdata::StatResult, bettermemory_core::fsdata::OsError> {
        self.inner.stat(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, bettermemory_core::fsdata::OsError> {
        self.inner.listdir(path)
    }
}

/// A file system on which every call fails.
struct Failing(PyErr);

impl Fs for Failing {
    type File = ();
    fn open(&self, _: &[u8]) -> Result<(), PyErr> {
        Err(self.0)
    }
    fn fstat(&self, _: &mut ()) -> Result<Stat, PyErr> {
        Err(self.0)
    }
    fn read(&self, _: &mut (), _: usize) -> Result<Vec<u8>, PyErr> {
        Err(self.0)
    }
    fn access_read(&self, _: &[u8]) -> bool {
        false
    }
    fn lstat(&self, _: &[u8]) -> Result<Kind, PyErr> {
        Err(self.0)
    }
    fn readlink(&self, _: &[u8]) -> Result<Vec<u8>, PyErr> {
        Err(self.0)
    }
    fn stat(
        &self,
        _: &[u8],
    ) -> Result<bettermemory_core::fsdata::StatResult, bettermemory_core::fsdata::OsError> {
        Err(bettermemory_core::fsdata::OsError {
            class: self.0,
            errno: None,
            winerror: None,
        })
    }
    fn listdir(&self, _: &[u8]) -> Result<Vec<Vec<u8>>, bettermemory_core::fsdata::OsError> {
        Err(bettermemory_core::fsdata::OsError {
            class: self.0,
            errno: None,
            winerror: None,
        })
    }
}

/// A file system that opens anything, then fails the stat or the read.
struct FailsLater {
    stat: bool,
}

impl Fs for FailsLater {
    type File = ();
    fn open(&self, _: &[u8]) -> Result<(), PyErr> {
        Ok(())
    }
    fn fstat(&self, _: &mut ()) -> Result<Stat, PyErr> {
        if self.stat {
            return Err(PyErr::Os);
        }
        Ok(Stat {
            kind: Kind::Regular,
            identity: None,
            size: 0,
        })
    }
    fn read(&self, _: &mut (), _: usize) -> Result<Vec<u8>, PyErr> {
        Err(PyErr::Os)
    }
    fn access_read(&self, _: &[u8]) -> bool {
        true
    }
    fn lstat(&self, _: &[u8]) -> Result<Kind, PyErr> {
        Ok(Kind::Regular)
    }
    fn readlink(&self, _: &[u8]) -> Result<Vec<u8>, PyErr> {
        Err(PyErr::Os)
    }
    fn stat(
        &self,
        _: &[u8],
    ) -> Result<bettermemory_core::fsdata::StatResult, bettermemory_core::fsdata::OsError> {
        Err(bettermemory_core::fsdata::OsError {
            class: PyErr::Os,
            errno: None,
            winerror: None,
        })
    }
    fn listdir(&self, _: &[u8]) -> Result<Vec<Vec<u8>>, bettermemory_core::fsdata::OsError> {
        Err(bettermemory_core::fsdata::OsError {
            class: PyErr::Os,
            errno: None,
            winerror: None,
        })
    }
}

fn mem(flavour: Flavour, nodes: &[(&[u8], Node)]) -> MemFs {
    let mut fs = MemFs::new(flavour);
    for (path, node) in nodes {
        fs.insert(path, node.clone());
    }
    fs
}

#[test]
fn a_fifty_megabyte_dotgit_is_refused_at_the_bound() {
    let mut big = b"gitdir: x\n".to_vec();
    big.resize(50 << 20, b'\n');
    let fs = Counting {
        inner: mem(Flavour::Posix, &[(b"/r/.git", Node::File(big))]),
        read: Cell::new(0),
    };
    assert_eq!(gitfile_target(&fs, b"/r/.git", Flavour::Posix), None);
    assert!(fs.read.get() <= GITFILE_LIMIT + 1, "{}", fs.read.get());
    fs.read.set(0);
    assert_eq!(small_text(&fs, b"/r/.git"), None);
    assert!(fs.read.get() <= GITFILE_LIMIT + 1, "{}", fs.read.get());
    // At the bound the file reads.
    let mut at = b"gitdir: x".to_vec();
    at.resize(GITFILE_LIMIT, b' ');
    let fs = mem(Flavour::Posix, &[(b"/r/.git", Node::File(at))]);
    assert_eq!(
        gitfile_target(&fs, b"/r/.git", Flavour::Posix),
        Some(b"/r/x".to_vec())
    );
}

#[test]
fn an_endless_device_is_read_to_the_bound_only() {
    let fs = Counting {
        inner: mem(
            Flavour::Posix,
            &[
                (b"/dev/zero", Node::Endless),
                (b"/r/.git", Node::Link(b"/dev/zero".to_vec())),
            ],
        ),
        read: Cell::new(0),
    };
    assert_eq!(gitfile_target(&fs, b"/r/.git", Flavour::Posix), None);
    assert!(fs.read.get() <= GITFILE_LIMIT + 1);
}

#[test]
fn a_fifo_socket_or_folder_is_not_read() {
    for node in [Node::Fifo, Node::Socket, Node::Dir] {
        let fs = mem(Flavour::Posix, &[(b"/r/.git", node.clone())]);
        assert_eq!(
            gitfile_target(&fs, b"/r/.git", Flavour::Posix),
            None,
            "{node:?}"
        );
        assert_eq!(small_text(&fs, b"/r/.git"), None, "{node:?}");
    }
}

#[cfg(unix)]
#[test]
fn a_real_fifo_does_not_block() {
    use bettermemory_core::fsdata::StdFs;
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "bm-gfleaves-fifo-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    let fifo = dir.join(".git");
    let made = std::process::Command::new("mkfifo")
        .arg(&fifo)
        .status()
        .is_ok_and(|s| s.success());
    let path = fifo.to_str().expect("UTF-8").as_bytes().to_vec();
    let answers = made.then(|| {
        (
            gitfile_target(&StdFs, &path, Flavour::Posix),
            small_text(&StdFs, &path),
        )
    });
    let _ = std::fs::remove_dir_all(&dir);
    assert_eq!(answers, Some((None, None)));
}

#[test]
fn a_path_that_does_not_answer_is_unanswered_and_not_read() {
    let hung = Hung([b"/share/x".to_vec()].into_iter().collect::<BTreeSet<_>>());
    let ran = Cell::new(false);
    let got: Result<Read<()>, ()> = bounded_read(&hung, b"/share/x", || {
        ran.set(true);
        Ok(())
    });
    assert_eq!(got, Ok(Read::Unanswered));
    assert!(!ran.get());
    let jobs: Vec<Job<'_, u8, &str>> = vec![
        (b"/share/x".to_vec(), Box::new(|| Ok(1))),
        (b"/ok".to_vec(), Box::new(|| Err("EIO"))),
        (b"/ok".to_vec(), Box::new(|| Ok(3))),
    ];
    assert_eq!(
        bounded_reads(&hung, jobs, false),
        Ok(vec![Read::Unanswered, Read::NotRead, Read::Value(3)])
    );
}

#[test]
fn a_file_system_that_fails_every_call_reads_nothing() {
    for error in [
        PyErr::FileNotFound,
        PyErr::NotADirectory,
        PyErr::Permission,
        PyErr::BlockingIO,
        PyErr::Interrupted,
        PyErr::Os,
        PyErr::Value,
    ] {
        let fs = Failing(error);
        assert_eq!(gitfile_target(&fs, b"/r/.git", Flavour::Posix), None);
        assert_eq!(small_text(&fs, b"/r/.git"), None);
        let exc = Raised::from(error);
        assert!(!path_missing(exc) || path_gone(exc));
    }
    for stat in [true, false] {
        let fs = FailsLater { stat };
        assert_eq!(gitfile_target(&fs, b"/r/.git", Flavour::Windows), None);
        assert_eq!(small_text(&fs, b"/r/.git"), None);
    }
}

#[test]
fn nul_and_non_utf8_paths_and_contents_give_typed_answers() {
    let fs = mem(
        Flavour::Posix,
        &[(
            b"/r/\xff/.git",
            Node::File(b"gitdir: \xfe\x00\xc3".to_vec()),
        )],
    );
    // A path with a NUL is a ValueError: nothing is read.
    assert_eq!(gitfile_target(&fs, b"/r/\x00/.git", Flavour::Posix), None);
    assert_eq!(
        gitfile_target(&fs, b"/r/\xff/.git", Flavour::Posix),
        Some(b"/r/\xff/\xef\xbf\xbd\x00\xef\xbf\xbd".to_vec())
    );
    // The Windows flavour refuses a path that is not UTF-8 (ValueError).
    let win = mem(
        Flavour::Windows,
        &[(b"C:/r/.git", Node::File(b"gitdir: x".to_vec()))],
    );
    assert_eq!(
        gitfile_target(&win, b"C:/r/\xff/.git", Flavour::Windows),
        None
    );
    assert_eq!(
        gitfile_target(&win, b"C:/r/.git", Flavour::Windows),
        Some(b"C:\\r\\x".to_vec())
    );
    assert_eq!(
        small_text(&fs, b"/r/\xff/.git").as_deref(),
        Some("gitdir: \u{fffd}\0\u{fffd}")
    );
}

#[test]
fn a_megabyte_of_dots_normalizes_in_one_pass() {
    for piece in [&b"./"[..], b"../", b"a/../", b"\\..\\.\\", b"//"] {
        let mut data = b"gitdir: ".to_vec();
        while data.len() + piece.len() <= GITFILE_LIMIT {
            data.extend_from_slice(piece);
        }
        for (flavour, dotgit) in [
            (Flavour::Posix, &b"/w/r/.git"[..]),
            (Flavour::Windows, b"C:\\w\\r\\.git"),
        ] {
            let got = parse_gitfile(&data, dotgit, flavour).expect("a path");
            assert!(got.len() <= data.len() + dotgit.len(), "{flavour:?}");
        }
    }
}

#[test]
fn headref_and_format_take_any_bytes() {
    for data in [&b""[..], b"ref:", b"ref:\xff", &[0xff; 300], &[b'a'; 40]] {
        let _ = headref_content(data);
    }
    assert!(headref_content(&[b'a'; 40]));
    let git = GitFacts {
        release: None,
        reads_compat: None,
    };
    let mut many = Vec::new();
    for _ in 0..20_000 {
        many.extend_from_slice(b"[extensions]\n\trefstorage = files\n");
    }
    assert_eq!(object_format(&many, &git), Some(b"sha1".to_vec()));
    many.splice(0..0, *b"[core]\n\trepositoryformatversion = 1\n");
    assert_eq!(object_format(&many, &git), None);
    assert_eq!(object_format(&vec![b'#'; (2 << 20) + 1], &git), None);
}
