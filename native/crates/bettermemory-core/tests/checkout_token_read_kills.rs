//! Plausible wrong implementations of the checkout token's reads, each
//! shown to answer differently from the reference on at least one row of
//! `checkout_token_read.jsonl`, where the port answers as the reference.
//! A mutant is the port run over a file system that behaves as the wrong
//! implementation would (a read made once, no `fcntl`, a close that never
//! fails, no stat before the plain open), or a wrong rule applied to the
//! row's answer.
//!
//! POSIX only, as `checkout_token_read.jsonl` is (`golden_checkout_token_read.rs`).

#![cfg(unix)]

mod common;
mod ct_read_script;

use bettermemory_core::checkout_token::read::{read_at, Fault};
use bettermemory_core::fsdata::{Access, Flavour, Fs, Kind, OsError, PyErr, Stat, StatResult};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use common::jsonl;
use ct_read_script::{expected, recorded, Script, CWD};
use serde_json::Value;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Mutant {
    /// One `os.read` and no loop.
    ReadOnce,
    /// No `fcntl`: a refused read is never made again.
    NoFcntl,
    /// `os.close`'s error swallowed.
    QuietClose,
    /// The plain open made without the stat before it.
    NoStat,
    /// Every errno retried, not only EINVAL and EIO.
    RetryAll,
    /// The non-blocking open's EINVAL taken as an unreadable file.
    NoPlainOpen,
}

struct Wrong<'a> {
    inner: &'a Script,
    mutant: Mutant,
    reads: std::cell::Cell<usize>,
}

impl Fs for Wrong<'_> {
    type File = ();
    fn open(&self, path: &[u8]) -> Result<(), PyErr> {
        self.inner.open(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<(), OsError> {
        self.reads.set(0);
        match self.inner.open_result(path) {
            Err(e)
                if self.mutant == Mutant::NoPlainOpen
                    && matches!(e.errno, Some("EINVAL" | "EIO")) =>
            {
                Err(OsError::new(PyErr::Os, "EBADF"))
            }
            other => other,
        }
    }
    fn open_blocking(&self, path: &[u8]) -> Result<(), OsError> {
        self.inner.open_blocking(path)
    }
    fn fstat(&self, file: &mut ()) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn fstat_result(&self, file: &mut ()) -> Result<Stat, OsError> {
        self.inner.fstat_result(file)
    }
    fn read(&self, file: &mut (), max: usize) -> Result<Vec<u8>, PyErr> {
        self.inner.read(file, max)
    }
    fn read_once(&self, file: &mut (), max: usize) -> Result<Vec<u8>, OsError> {
        self.reads.set(self.reads.get() + 1);
        if self.mutant == Mutant::ReadOnce && self.reads.get() > 1 {
            return Ok(Vec::new());
        }
        match self.inner.read_once(file, max) {
            Err(e) if self.mutant == Mutant::RetryAll => Err(OsError::new(e.class, "EINVAL")),
            other => other,
        }
    }
    fn close(&self, file: ()) -> Result<(), OsError> {
        let closed = self.inner.close(file);
        if self.mutant == Mutant::QuietClose {
            return Ok(());
        }
        closed
    }
    fn nonblock_flag(&self) -> Option<i32> {
        if self.mutant == Mutant::NoFcntl {
            return None;
        }
        self.inner.nonblock_flag()
    }
    fn status_flags(&self, file: &mut ()) -> Result<i32, OsError> {
        self.inner.status_flags(file)
    }
    fn set_status_flags(&self, file: &mut (), flags: i32) -> Result<(), OsError> {
        self.inner.set_status_flags(file, flags)
    }
    fn strerror(&self, error: &OsError) -> Option<String> {
        self.inner.strerror(error)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        let found = self.inner.stat(path);
        if self.mutant == Mutant::NoStat {
            return found.map(|mut s| {
                s.kind = Kind::Regular;
                s
            });
        }
        found
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<(), OsError> {
        self.inner.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.readlink(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.listdir(path)
    }
}

fn rows() -> Vec<Value> {
    jsonl("checkout_token_read.jsonl")
        .into_iter()
        .skip(1)
        .filter(|r| r.get("path").is_some())
        .collect()
}

fn run(row: &Value, mutant: Option<Mutant>) -> Value {
    let script = Script::new(&row["passes"], row["plat"] == "win32");
    let wrong = mutant.map(|mutant| Wrong {
        inner: &script,
        mutant,
        reads: std::cell::Cell::new(0),
    });
    let path = row["path"].as_str().expect("a path").as_bytes();
    let mut pause = |_: f64| {};
    // A mutant's script may run out or meet a call the reference never
    // made; that too is a different answer.
    let answer = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| match &wrong {
        Some(wrong) => {
            let ctx = Ctx {
                fs: wrong,
                flavour: Flavour::Posix,
                cwd: CWD.as_bytes(),
            };
            recorded(&read_at(&ctx, path, &mut pause))
        }
        None => {
            let ctx = Ctx {
                fs: &script,
                flavour: Flavour::Posix,
                cwd: CWD.as_bytes(),
            };
            recorded(&read_at(&ctx, path, &mut pause))
        }
    }));
    answer.unwrap_or(Value::String("panicked".to_owned()))
}

fn killed(mutant: Mutant) -> usize {
    let rows = rows();
    assert!(!rows.is_empty());
    let mut differ = 0;
    for row in &rows {
        let want = expected(row);
        assert_eq!(run(row, None), want, "port on {}", row["n"]);
        if run(row, Some(mutant)) != want {
            differ += 1;
        }
    }
    differ
}

#[test]
fn a_single_read_is_killed() {
    assert!(killed(Mutant::ReadOnce) > 0);
}

#[test]
fn a_read_never_made_again_is_killed() {
    assert!(killed(Mutant::NoFcntl) > 0);
}

#[test]
fn a_close_that_never_fails_is_killed() {
    assert!(killed(Mutant::QuietClose) > 0);
}

#[test]
fn a_plain_open_without_the_stat_is_killed() {
    assert!(killed(Mutant::NoStat) > 0);
}

#[test]
fn every_errno_retried_is_killed() {
    assert!(killed(Mutant::RetryAll) > 0);
}

#[test]
fn no_plain_open_is_killed() {
    assert!(killed(Mutant::NoPlainOpen) > 0);
}

fn answer_of(name: &str) -> Value {
    let rows = jsonl("checkout_token_read.jsonl");
    let row = rows.iter().find(|r| r["n"] == name).expect("the row");
    expected(row)
}

/// `len(data) > _MAX_BYTES`, not `>=`: 4,096 bytes are read whole and
/// judged by their text; 4,097 are too large.
#[test]
fn the_size_bound_is_strict() {
    let detail = |name: &str| answer_of(name)["r"]["detail"].clone();
    assert_eq!(
        detail("4096 bytes"),
        "not a bettermemory checkout token v1 file"
    );
    assert_eq!(detail("4097 bytes"), "larger than a token file");
}

/// The size is judged before the text: large non-ASCII bytes are too
/// large, not "not ASCII text".
#[test]
fn the_size_is_judged_before_the_text() {
    assert_eq!(
        answer_of("not ascii and large")["r"]["detail"],
        "larger than a token file"
    );
}

/// The detail names the call that failed and the errno's name in
/// brackets after its text.
#[test]
fn the_detail_names_the_call_and_the_errno() {
    assert_eq!(
        answer_of("fstat fails")["r"]["detail"],
        "read: text of EIO (EIO)"
    );
    assert_eq!(
        answer_of("open einval, stat eio")["r"]["detail"],
        "open: text of EIO (EIO)"
    );
}

/// A ValueError is no OSError: it raises out of the read.
#[test]
fn a_nul_raises() {
    let script = Script::new(&serde_json::json!([]), false);
    let ctx = Ctx {
        fs: &script,
        flavour: Flavour::Posix,
        cwd: CWD.as_bytes(),
    };
    let answer = read_at(&ctx, b"/a\0b", &mut |_| {});
    assert!(matches!(answer, Err(Fault::Raised(e)) if e.class == PyErr::Value));
}
