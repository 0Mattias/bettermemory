//! The checkout token's reads on hostile input: a file system that hands
//! out more than asked, a byte at a time, or refuses every read; paths of
//! a million bytes; a FIFO with no writer (never waits); typed answers and
//! no panic, with reads bounded by `_MAX_BYTES` and time linear in the
//! path's length.

use std::cell::Cell;
use std::time::{Duration, Instant};

use bettermemory_core::checkout_token::read::{read_at, Fault, ReadState, MAX_BYTES};
use bettermemory_core::fsdata::{Access, Flavour, Fs, Kind, OsError, PyErr, Stat, StatResult};
use bettermemory_core::gitfile_rule::gitdir::Ctx;

#[derive(Clone, Copy)]
enum Mode {
    /// Every read answers this many bytes, whatever was asked.
    Flood(usize),
    /// Every read answers one byte, forever.
    Drip,
    /// Every read is refused with EINVAL.
    Refuse,
    /// The open is refused with EACCES, whose text is empty.
    Mute,
}

struct Hostile {
    mode: Mode,
    reads: Cell<usize>,
    bytes: Cell<usize>,
}

impl Hostile {
    fn new(mode: Mode) -> Hostile {
        Hostile {
            mode,
            reads: Cell::new(0),
            bytes: Cell::new(0),
        }
    }
}

impl Fs for Hostile {
    type File = ();
    fn open(&self, _: &[u8]) -> Result<(), PyErr> {
        Ok(())
    }
    fn open_result(&self, path: &[u8]) -> Result<(), OsError> {
        if path.contains(&0) {
            return Err(OsError::value());
        }
        if let Mode::Mute = self.mode {
            return Err(OsError::new(PyErr::Permission, "EACCES"));
        }
        Ok(())
    }
    fn fstat(&self, _: &mut ()) -> Result<Stat, PyErr> {
        Ok(Stat {
            kind: Kind::Regular,
            identity: None,
            size: 0,
        })
    }
    fn fstat_result(&self, _: &mut ()) -> Result<Stat, OsError> {
        self.fstat(&mut ()).map_err(OsError::of_class)
    }
    fn read(&self, _: &mut (), max: usize) -> Result<Vec<u8>, PyErr> {
        self.read_once(&mut (), max).map_err(|e| e.class)
    }
    fn read_once(&self, _: &mut (), _max: usize) -> Result<Vec<u8>, OsError> {
        self.reads.set(self.reads.get() + 1);
        let n = match self.mode {
            Mode::Flood(n) => n,
            Mode::Drip => 1,
            Mode::Refuse | Mode::Mute => return Err(OsError::new(PyErr::Os, "EINVAL")),
        };
        self.bytes.set(self.bytes.get() + n);
        Ok(vec![b'a'; n])
    }
    fn nonblock_flag(&self) -> Option<i32> {
        Some(4)
    }
    fn status_flags(&self, _: &mut ()) -> Result<i32, OsError> {
        Ok(4)
    }
    fn set_status_flags(&self, _: &mut (), _: i32) -> Result<(), OsError> {
        Ok(())
    }
    fn strerror(&self, _: &OsError) -> Option<String> {
        if let Mode::Mute = self.mode {
            return Some(String::new());
        }
        Some("Invalid argument".to_owned())
    }
    fn access(&self, _: &[u8], _: Access) -> Result<bool, PyErr> {
        Ok(true)
    }
    fn open_no_follow(&self, _: &[u8]) -> Result<(), OsError> {
        Ok(())
    }
    fn lstat(&self, _: &[u8]) -> Result<Kind, PyErr> {
        Ok(Kind::Regular)
    }
    fn readlink(&self, _: &[u8]) -> Result<Vec<u8>, PyErr> {
        Err(PyErr::Os)
    }
    fn stat(&self, _: &[u8]) -> Result<StatResult, OsError> {
        Err(OsError::of_class(PyErr::Os))
    }
    fn lstat_result(&self, _: &[u8]) -> Result<StatResult, OsError> {
        Err(OsError::of_class(PyErr::Os))
    }
    fn listdir(&self, _: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        Ok(Vec::new())
    }
}

fn ctx(fs: &Hostile) -> Ctx<'_, Hostile> {
    Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/cwd",
    }
}

#[test]
fn a_flood_is_cut_after_one_read() {
    let fs = Hostile::new(Mode::Flood(64 << 20));
    let found = read_at(&ctx(&fs), b"/t", &mut |_| panic!("no pause")).expect("an answer");
    assert_eq!(found.state, ReadState::Malformed);
    assert_eq!(found.detail, "larger than a token file");
    assert_eq!(fs.reads.get(), 1);
}

#[test]
fn a_drip_stops_past_the_bound() {
    let fs = Hostile::new(Mode::Drip);
    let found = read_at(&ctx(&fs), b"/t", &mut |_| panic!("no pause")).expect("an answer");
    assert_eq!(found.detail, "larger than a token file");
    assert_eq!(fs.bytes.get(), MAX_BYTES + 1);
    assert_eq!(fs.reads.get(), MAX_BYTES + 1);
}

#[test]
fn a_refusal_every_time_ends_after_two_reads() {
    let fs = Hostile::new(Mode::Refuse);
    let found = read_at(&ctx(&fs), b"/t", &mut |_| panic!("no pause")).expect("an answer");
    assert_eq!(found.state, ReadState::Unreadable);
    assert_eq!(found.detail, "read: Invalid argument (EINVAL)");
    assert_eq!(fs.reads.get(), 2);
}

#[test]
fn integer_bounds_of_a_chunk() {
    for n in [0, 1, MAX_BYTES, MAX_BYTES + 1, MAX_BYTES * 2] {
        let fs = Hostile::new(Mode::Flood(n));
        let found = read_at(&ctx(&fs), b"/t", &mut |_| {}).expect("an answer");
        if n == 0 {
            assert_eq!(found.detail, "empty");
        } else if n <= MAX_BYTES {
            // Reads repeat until past the bound.
            assert_eq!(found.detail, "larger than a token file", "{n}");
        }
    }
}

fn fastest(path: &[u8]) -> Duration {
    (0..3)
        .map(|_| {
            let fs = Hostile::new(Mode::Flood(0));
            let start = Instant::now();
            let found = read_at(&ctx(&fs), path, &mut |_| {}).expect("an answer");
            assert_eq!(found.detail, "empty");
            start.elapsed()
        })
        .min()
        .expect("three runs")
}

/// A relative path of a million bytes is joined to the working directory
/// once per open: time at 2n is within a small multiple of time at n.
#[test]
fn a_long_path_costs_linear_time() {
    let n = 1 << 20;
    let one = fastest(&vec![b'a'; n]);
    let two = fastest(&vec![b'a'; 2 * n]);
    assert!(
        two <= one * 6 + Duration::from_millis(20),
        "n: {one:?}, 2n: {two:?}"
    );
}

#[test]
fn a_nul_anywhere_raises() {
    for path in [&b"\0"[..], b"/a\0", b"rel\0x"] {
        let fs = Hostile::new(Mode::Drip);
        assert!(read_at(&ctx(&fs), path, &mut |_| {}).is_err());
    }
}

/// A FIFO with no writer is opened non-blocking and reported, never
/// waited on.
#[cfg(unix)]
#[test]
fn a_fifo_never_waits() {
    use bettermemory_core::fsdata::StdFs;
    let root = std::env::temp_dir().join(format!("bm-ct-fifo-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).expect("root");
    let fifo = root.join("fifo");
    let status = std::process::Command::new("mkfifo")
        .arg(&fifo)
        .status()
        .expect("mkfifo");
    assert!(status.success());
    let (tx, rx) = std::sync::mpsc::channel();
    let path = fifo.to_str().expect("UTF-8").to_owned();
    std::thread::spawn(move || {
        let ctx = Ctx {
            fs: &StdFs,
            flavour: Flavour::Posix,
            cwd: b"/",
        };
        let found = read_at(&ctx, path.as_bytes(), &mut |_| {}).expect("an answer");
        tx.send(found.detail).expect("send");
    });
    let detail = rx
        .recv_timeout(Duration::from_secs(20))
        .expect("the read returns");
    assert_eq!(detail, "not a regular file");
    std::fs::remove_dir_all(&root).expect("remove");
}

/// An error whose text is empty is declined: the reference would word it
/// from `str(exc)`, which is not kept.
#[test]
fn an_empty_error_text_declines() {
    let fs = Hostile::new(Mode::Mute);
    let found = read_at(&ctx(&fs), b"/t", &mut |_| {});
    assert!(matches!(found, Err(Fault::NotPorted)), "{found:?}");
}

/// `MemFs`'s texts are the C library's on this machine, and the ones the
/// reference prints for them.
#[cfg(unix)]
#[test]
fn the_model_texts_are_the_c_library_texts() {
    use bettermemory_core::fsdata::{MemFs, StdFs};
    let texts = [
        ("ENOENT", "No such file or directory"),
        ("ENOTDIR", "Not a directory"),
        ("EISDIR", "Is a directory"),
        ("EACCES", "Permission denied"),
        ("EPERM", "Operation not permitted"),
        ("EEXIST", "File exists"),
        ("ELOOP", "Too many levels of symbolic links"),
        ("ENAMETOOLONG", "File name too long"),
        ("EIO", "Input/output error"),
        ("EINVAL", "Invalid argument"),
        ("EINTR", "Interrupted system call"),
        ("EBADF", "Bad file descriptor"),
    ];
    let mem = MemFs::new(Flavour::Posix);
    for (name, text) in texts {
        let error = OsError::new(PyErr::of_errno(name), name);
        assert_eq!(mem.strerror(&error).as_deref(), Some(text), "{name}");
        assert_eq!(StdFs.strerror(&error).as_deref(), Some(text), "{name}");
    }
    let windows = MemFs::new(Flavour::Windows);
    assert_eq!(windows.strerror(&OsError::new(PyErr::Os, "EIO")), None);
}

/// One `read_once` of `StdFs` answers all it was asked for, up to its
/// cap, and a count past the cap allocates no more than the cap.
#[test]
fn one_read_answers_the_whole_count() {
    use bettermemory_core::fsdata::{StdFs, READ_ONCE_CAP};
    let root = std::env::temp_dir().join(format!("bm-ct-once-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).expect("root");
    let file = root.join("f");
    std::fs::write(&file, vec![b'a'; 2 * READ_ONCE_CAP]).expect("write");
    let path = file.to_str().expect("UTF-8").as_bytes().to_vec();
    let mut open = StdFs.open_result(&path).expect("open");
    assert_eq!(StdFs.read_once(&mut open, 4097).expect("read").len(), 4097);
    let rest = StdFs.read_once(&mut open, usize::MAX).expect("read");
    assert_eq!(rest.len(), READ_ONCE_CAP);
    StdFs.close(open).expect("close");
    std::fs::remove_dir_all(&root).expect("remove");
}

/// A token file opened once every descriptor is taken: the open's EMFILE
/// is worded as the reference words it, never declined. The read runs in
/// a child of this test with its descriptors capped at 64.
#[cfg(unix)]
#[test]
fn no_descriptor_left_is_unreadable() {
    use bettermemory_core::fsdata::StdFs;
    const CHILD: &str = "BM_CT_READ_EMFILE";
    if let Ok(path) = std::env::var(CHILD) {
        let mut held = Vec::new();
        while let Ok(file) = std::fs::File::open("/dev/null") {
            held.push(file);
        }
        let ctx = Ctx {
            fs: &StdFs,
            flavour: Flavour::Posix,
            cwd: b"/",
        };
        let found = read_at(&ctx, path.as_bytes(), &mut |_| {});
        drop(held);
        let found = found.expect("an answer");
        assert_eq!(found.state, ReadState::Unreadable);
        assert_eq!(found.detail, "open: Too many open files (EMFILE)");
        return;
    }
    let root = std::env::temp_dir().join(format!("bm-ct-emfile-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).expect("root");
    let token = root.join("token");
    std::fs::write(
        &token,
        b"bettermemory checkout token v1\n0123456789ABCDEFGHJKMNPQRS\n",
    )
    .expect("token");
    let exe = std::env::current_exe().expect("the test binary");
    let status = std::process::Command::new("sh")
        .arg("-c")
        .arg(
            "ulimit -n 64 && exec \"$0\" --exact no_descriptor_left_is_unreadable --test-threads 1",
        )
        .arg(&exe)
        .env(CHILD, &token)
        .status()
        .expect("the child runs");
    std::fs::remove_dir_all(&root).expect("remove");
    assert!(status.success(), "the child's read: {status}");
}
