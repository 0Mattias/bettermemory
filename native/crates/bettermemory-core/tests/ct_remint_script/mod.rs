//! The scripted OS of `checkout_token_remint.jsonl`: the write file's
//! script (`ct_write_script`) with `os.link`, `os.replace`, `os.lstat`
//! and the `os.stat` of a break mark answering in turn from the row's
//! `links`, `replaces`, `lstats` and `marks`, and a token or claim read
//! past the row's `reads` finding nothing (`gen_checkout_token_remint`).

#![allow(dead_code, unused_imports)]

#[path = "../ct_write_script/mod.rs"]
pub mod ct_write_script;

use std::cell::RefCell;
use std::collections::VecDeque;

use bettermemory_core::checkout_token::read::Fault;
use bettermemory_core::checkout_token::remint::{remint, BREAK_PREFIX};
use bettermemory_core::checkout_token::write::{Machine, TokenFs, TokenWrite};
use bettermemory_core::checkout_token::Lineage;
use bettermemory_core::fsdata::{Access, Flavour, Fs, Kind, OsError, PyErr, Stat, StatResult};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
pub use ct_write_script::{expected, fail, recorded, WMachine, WScript, CWD};

use serde_json::Value;

fn shown(path: &[u8]) -> Result<String, OsError> {
    if path.contains(&0) {
        return Err(OsError::value());
    }
    let text = String::from_utf8(path.to_vec()).expect("UTF-8 path");
    Ok(text
        .strip_prefix(&format!("{CWD}/"))
        .unwrap_or(&text)
        .to_owned())
}

fn queue(row: &Value, key: &str) -> RefCell<VecDeque<Value>> {
    RefCell::new(
        row.get(key)
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .cloned()
            .collect(),
    )
}

/// A row's answers: the write script and the four queues.
pub struct RScript {
    pub w: WScript,
    links: RefCell<VecDeque<Value>>,
    replaces: RefCell<VecDeque<Value>>,
    lstats: RefCell<VecDeque<Value>>,
    marks: RefCell<VecDeque<Value>>,
}

impl RScript {
    pub fn new(row: &Value) -> RScript {
        RScript {
            w: WScript::new(row),
            links: queue(row, "links"),
            replaces: queue(row, "replaces"),
            lstats: queue(row, "lstats"),
            marks: queue(row, "marks"),
        }
    }

    /// Whether every queued answer was taken.
    pub fn spent(&self) -> bool {
        self.w.read.left() == 0
            && [&self.links, &self.replaces, &self.lstats, &self.marks]
                .iter()
                .all(|q| q.borrow().is_empty())
    }

    fn next_ok(&self, q: &RefCell<VecDeque<Value>>) -> Result<(), OsError> {
        match q.borrow_mut().pop_front() {
            None => Ok(()),
            Some(Value::String(s)) if s == "ok" => Ok(()),
            Some(Value::String(s)) => Err(fail(&s)),
            Some(other) => panic!("an answer: {other}"),
        }
    }
}

impl Fs for RScript {
    type File = ();
    fn open(&self, path: &[u8]) -> Result<(), PyErr> {
        self.open_result(path).map_err(|e| e.class)
    }
    fn open_result(&self, path: &[u8]) -> Result<(), OsError> {
        if self.w.read.left() == 0 && !path.contains(&0) {
            self.w.log(format!("open {}", shown(path)?));
            return Err(fail("ENOENT"));
        }
        self.w.open_result(path)
    }
    fn open_blocking(&self, path: &[u8]) -> Result<(), OsError> {
        self.w.open_blocking(path)
    }
    fn fstat(&self, file: &mut ()) -> Result<Stat, PyErr> {
        self.w.fstat(file)
    }
    fn fstat_result(&self, file: &mut ()) -> Result<Stat, OsError> {
        self.w.fstat_result(file)
    }
    fn read(&self, file: &mut (), max: usize) -> Result<Vec<u8>, PyErr> {
        self.w.read(file, max)
    }
    fn read_once(&self, file: &mut (), max: usize) -> Result<Vec<u8>, OsError> {
        self.w.read_once(file, max)
    }
    fn close(&self, file: ()) -> Result<(), OsError> {
        self.w.close(file)
    }
    fn nonblock_flag(&self) -> Option<i32> {
        self.w.nonblock_flag()
    }
    fn status_flags(&self, file: &mut ()) -> Result<i32, OsError> {
        self.w.status_flags(file)
    }
    fn set_status_flags(&self, file: &mut (), flags: i32) -> Result<(), OsError> {
        self.w.set_status_flags(file, flags)
    }
    fn strerror(&self, error: &OsError) -> Option<String> {
        self.w.strerror(error)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.w.stat(path)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.w.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<(), OsError> {
        self.w.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.lstat_result(path).map(|s| s.kind).map_err(|e| e.class)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.w.readlink(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.w.log(format!("lstat {}", shown(path)?));
        let kind = match self.lstats.borrow_mut().pop_front() {
            None => Kind::Regular,
            Some(Value::String(s)) => match s.as_str() {
                "reg" => Kind::Regular,
                "dir" => Kind::Directory,
                "fifo" => Kind::Fifo,
                other => return Err(fail(other)),
            },
            Some(other) => panic!("an lstat: {other}"),
        };
        Ok(StatResult {
            kind,
            mode: None,
            ino: None,
            dev: None,
            size: 0,
            mtime_ns: 0,
            ctime_ns: None,
            flags: 0,
            file_attributes: 0,
            reparse_tag: None,
        })
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.w.listdir(path)
    }
}

impl TokenFs for RScript {
    type Created = u32;
    fn mkdir(&self, path: &[u8]) -> Result<(), OsError> {
        self.w.mkdir(path)
    }
    fn create_excl(&self, path: &[u8], mode: u32) -> Result<u32, OsError> {
        self.w.create_excl(path, mode)
    }
    fn write_once(&self, file: &mut u32, data: &[u8]) -> Result<usize, OsError> {
        self.w.write_once(file, data)
    }
    fn fsync_file(&self, file: &mut u32) -> Result<(), OsError> {
        self.w.fsync_file(file)
    }
    fn close_created(&self, file: u32) -> Result<(), OsError> {
        self.w.close_created(file)
    }
    fn link(&self, src: &[u8], dst: &[u8]) -> Result<(), OsError> {
        let line = format!("link {} {}", shown(src)?, shown(dst)?);
        self.w.log(line);
        self.next_ok(&self.links)
    }
    fn fsync_dir(&self, path: &[u8]) {
        self.w.fsync_dir(path)
    }
    fn scandir_names(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.w.scandir_names(path)
    }
    fn unlink(&self, path: &[u8]) -> Result<(), OsError> {
        self.w.unlink(path)
    }
    fn utime_now(&self, path: &[u8]) -> Result<(), OsError> {
        self.w.utime_now(path)
    }
    fn owner_uid(&self, path: &[u8]) -> Result<Option<u32>, OsError> {
        self.w.owner_uid(path)
    }
    fn lstat_mtime(&self, path: &[u8]) -> Result<f64, OsError> {
        self.w.lstat_mtime(path)
    }
    fn stat_mtime(&self, path: &[u8]) -> Result<f64, OsError> {
        let text = shown(path)?;
        if !text.contains(BREAK_PREFIX) {
            return self.w.stat_mtime(path);
        }
        self.w.log(format!("stat {text}"));
        match self.marks.borrow_mut().pop_front().expect("a mark's time") {
            Value::String(e) => Err(fail(&e)),
            m => Ok(m.as_f64().expect("a time")),
        }
    }
    fn replace(&self, src: &[u8], dst: &[u8]) -> Result<(), OsError> {
        let line = format!("replace {} {}", shown(src)?, shown(dst)?);
        self.w.log(line);
        self.next_ok(&self.replaces)
    }
}

/// The row's lineage, `rep`; where the row has none, the generator's
/// default (`0123456789ABCDEFGHJKMNPQRS`, device 1, inode 2).
pub fn lineage(row: &Value) -> Option<Lineage> {
    let default = serde_json::json!(["0123456789ABCDEFGHJKMNPQRS", "1", "2"]);
    let rep = row.get("rep").unwrap_or(&default).as_array()?;
    let text = |i: usize| -> Vec<u32> {
        rep[i]
            .as_str()
            .expect("text")
            .chars()
            .map(u32::from)
            .collect()
    };
    Some(Lineage {
        token: text(0),
        device: text(1),
        inode: text(2),
    })
}

/// `remint` as the row asks, over its script.
pub fn run(row: &Value, script: &RScript) -> Result<TokenWrite, Fault> {
    let ctx = Ctx {
        fs: script,
        flavour: Flavour::Posix,
        cwd: CWD.as_bytes(),
    };
    let mut machine = WMachine { script: &script.w };
    let machine: &mut dyn Machine = &mut machine;
    let git = row.get("git").and_then(Value::as_str).unwrap_or("r/.git");
    let policy = row.get("policy").and_then(Value::as_bool).unwrap_or(true);
    let bare = row.get("bare").and_then(Value::as_bool).unwrap_or(false);
    let sib = row.get("sib").and_then(Value::as_bool).unwrap_or(false);
    let rep = lineage(row);
    remint(&ctx, machine, git, policy, bare, rep.as_ref(), sib)
}
