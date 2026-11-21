//! The scripted OS of `checkout_token_write.jsonl` as a [`TokenFs`] and a
//! [`Machine`]: the token reads are the read file's script
//! (`ct_read_script`), each `_read_text` taking the next of the row's
//! `reads`; the write-side calls answer as the row says; every call is
//! logged as the generator logs it (`gen_checkout_token_write`).

#![allow(dead_code)]

#[path = "../ct_read_script/mod.rs"]
pub mod ct_read_script;

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, VecDeque};

use bettermemory_core::checkout_token::read::Fault;
use bettermemory_core::checkout_token::write::{Machine, TokenFs, TokenWrite};
use bettermemory_core::float_repr::repr;
use bettermemory_core::fsdata::{Access, Fs, Kind, OsError, PyErr, Stat, StatResult};
use ct_read_script::Script;
use serde_json::{json, Value};

pub use ct_read_script::CWD;

const NAMES: &[&str] = &[
    "ENOENT", "ENOTDIR", "EACCES", "EPERM", "EROFS", "EEXIST", "EIO", "ENOSPC", "EXDEV", "EMLINK",
    "EISDIR", "EINVAL", "EBADF", "EAGAIN", "EINTR",
];

pub fn fail(name: &str) -> OsError {
    let name = NAMES
        .iter()
        .find(|n| **n == name)
        .unwrap_or_else(|| panic!("an errno this script knows: {name}"));
    OsError::new(PyErr::of_errno(name), name)
}

fn shown(path: &[u8]) -> String {
    String::from_utf8(path.to_vec()).expect("UTF-8 path")
}

fn nul(path: &[u8]) -> Result<String, OsError> {
    if path.contains(&0) {
        return Err(OsError::value());
    }
    Ok(shown(path))
}

/// A row's write-side answers over the read script.
pub struct WScript {
    pub read: Script,
    row: Value,
    creates: RefCell<VecDeque<Value>>,
    created: RefCell<Vec<Option<Value>>>,
    writes: RefCell<VecDeque<Value>>,
    unlinks: RefCell<VecDeque<String>>,
    pub clock: Cell<f64>,
    draws: Cell<u32>,
    times: RefCell<HashMap<String, Value>>,
}

impl WScript {
    pub fn new(row: &Value) -> WScript {
        let reads = row.get("reads").cloned().unwrap_or(json!([]));
        let strings = |key: &str| -> VecDeque<String> {
            row.get(key)
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .map(|v| v.as_str().expect("text").to_owned())
                .collect()
        };
        WScript {
            read: Script::new(&reads, row["plat"] == "win32"),
            row: row.clone(),
            creates: RefCell::new(
                row.get("creates")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .cloned()
                    .collect(),
            ),
            created: RefCell::new(Vec::new()),
            writes: RefCell::new(VecDeque::new()),
            unlinks: RefCell::new(strings("unlinks")),
            clock: Cell::new(100.0),
            draws: Cell::new(0),
            times: RefCell::new(
                row.get("scan")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(|item| {
                        let name = item.get("name")?.as_str()?.to_owned();
                        Some((name, item["m"].clone()))
                    })
                    .collect(),
            ),
        }
    }

    pub fn log(&self, line: String) {
        self.read.log.borrow_mut().push(line);
    }

    pub fn logged(&self) -> Value {
        Value::Array(
            self.read
                .log
                .borrow()
                .iter()
                .map(|s| s.clone().into())
                .collect(),
        )
    }

    fn answer(&self, key: &str) -> Result<(), OsError> {
        match self.row.get(key).and_then(Value::as_str).unwrap_or("ok") {
            "ok" => Ok(()),
            other => Err(fail(other)),
        }
    }

    fn number(&self, key: &str, default: f64) -> Result<f64, OsError> {
        match self.row.get(key) {
            None => Ok(default),
            Some(Value::String(e)) => Err(fail(e)),
            Some(v) => Ok(v.as_f64().expect("a number")),
        }
    }

    fn spec_of(&self, file: u32, key: &str) -> Result<(), OsError> {
        let created = self.created.borrow();
        match created[file as usize]
            .as_ref()
            .and_then(|s| s.get(key))
            .and_then(Value::as_str)
            .unwrap_or("ok")
        {
            "ok" => Ok(()),
            other => Err(fail(other)),
        }
    }
}

impl Fs for WScript {
    type File = ();
    fn open(&self, path: &[u8]) -> Result<(), PyErr> {
        self.read.open(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<(), OsError> {
        self.read.open_result(path)
    }
    fn open_blocking(&self, path: &[u8]) -> Result<(), OsError> {
        self.read.open_blocking(path)
    }
    fn fstat(&self, file: &mut ()) -> Result<Stat, PyErr> {
        self.read.fstat(file)
    }
    fn fstat_result(&self, file: &mut ()) -> Result<Stat, OsError> {
        self.read.fstat_result(file)
    }
    fn read(&self, file: &mut (), max: usize) -> Result<Vec<u8>, PyErr> {
        self.read.read(file, max)
    }
    fn read_once(&self, file: &mut (), max: usize) -> Result<Vec<u8>, OsError> {
        self.read.read_once(file, max)
    }
    fn close(&self, file: ()) -> Result<(), OsError> {
        self.read.close(file)
    }
    fn nonblock_flag(&self) -> Option<i32> {
        self.read.nonblock_flag()
    }
    fn status_flags(&self, file: &mut ()) -> Result<i32, OsError> {
        self.read.status_flags(file)
    }
    fn set_status_flags(&self, file: &mut (), flags: i32) -> Result<(), OsError> {
        self.read.set_status_flags(file, flags)
    }
    fn strerror(&self, error: &OsError) -> Option<String> {
        self.read.strerror(error)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.read.stat(path)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.read.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<(), OsError> {
        self.read.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.read.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.read.readlink(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.read.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.read.listdir(path)
    }
}

impl TokenFs for WScript {
    type Created = u32;

    fn mkdir(&self, path: &[u8]) -> Result<(), OsError> {
        self.log(format!("mkdir {}", nul(path)?));
        self.answer("mkdir")
    }

    fn create_excl(&self, path: &[u8], mode: u32) -> Result<u32, OsError> {
        self.log(format!("create {} {mode:o}", nul(path)?));
        let spec = self.creates.borrow_mut().pop_front().unwrap_or(json!({}));
        if let Some(e) = spec.get("open").and_then(Value::as_str) {
            if e != "ok" {
                return Err(fail(e));
            }
        }
        *self.writes.borrow_mut() = spec
            .get("writes")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .cloned()
            .collect();
        let mut created = self.created.borrow_mut();
        created.push(Some(spec));
        Ok((created.len() - 1) as u32)
    }

    fn write_once(&self, _file: &mut u32, data: &[u8]) -> Result<usize, OsError> {
        self.log(format!("write {}", data.len()));
        match self.writes.borrow_mut().pop_front() {
            None => Ok(data.len()),
            Some(Value::String(e)) => Err(fail(&e)),
            Some(n) => Ok(n.as_u64().expect("a count") as usize),
        }
    }

    fn fsync_file(&self, file: &mut u32) -> Result<(), OsError> {
        self.log("fsync".to_owned());
        self.spec_of(*file, "fsync")
    }

    fn close_created(&self, file: u32) -> Result<(), OsError> {
        self.log("close".to_owned());
        let answer = self.spec_of(file, "close");
        self.created.borrow_mut()[file as usize] = None;
        answer
    }

    fn link(&self, src: &[u8], dst: &[u8]) -> Result<(), OsError> {
        let line = format!("link {} {}", nul(src)?, nul(dst)?);
        self.log(line);
        self.answer("link")
    }

    fn fsync_dir(&self, path: &[u8]) {
        self.log(format!("dopen {}", shown(path)));
        self.log("fsync".to_owned());
        self.log("dclose".to_owned());
    }

    fn scandir_names(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.log(format!("scandir {}", nul(path)?));
        match self.row.get("scan") {
            None => Ok(Vec::new()),
            Some(Value::String(e)) => Err(fail(e)),
            Some(Value::Array(items)) => Ok(items
                .iter()
                .take_while(|item| item.get("err").is_none())
                .map(|item| item["name"].as_str().expect("name").as_bytes().to_vec())
                .collect()),
            Some(other) => panic!("a scan: {other}"),
        }
    }

    fn lstat_mtime(&self, path: &[u8]) -> Result<f64, OsError> {
        let text = shown(path);
        let name = text.rsplit('/').next().expect("a name");
        self.log(format!("lstat {name}"));
        let times = self.times.borrow();
        match times.get(name).expect("the entry") {
            Value::String(e) => Err(fail(e)),
            m => Ok(m.as_f64().expect("a time")),
        }
    }

    fn unlink(&self, path: &[u8]) -> Result<(), OsError> {
        self.log(format!("unlink {}", nul(path)?));
        match self.unlinks.borrow_mut().pop_front() {
            None => Ok(()),
            Some(e) if e == "ok" => Ok(()),
            Some(e) => Err(fail(&e)),
        }
    }

    fn utime_now(&self, path: &[u8]) -> Result<(), OsError> {
        self.log(format!("utime {}", nul(path)?));
        self.answer("utime")
    }

    fn owner_uid(&self, path: &[u8]) -> Result<Option<u32>, OsError> {
        self.log(format!("stat {}", nul(path)?));
        Ok(Some(self.number("owner", 501.0)? as u32))
    }

    fn stat_mtime(&self, path: &[u8]) -> Result<f64, OsError> {
        self.log(format!("stat {}", nul(path)?));
        self.number("mtime", 5000.0)
    }
}

/// The row's machine, logging into its script.
pub struct WMachine<'a> {
    pub script: &'a WScript,
}

impl Machine for WMachine<'_> {
    fn euid(&mut self) -> u32 {
        self.script.log("geteuid".to_owned());
        self.script
            .row
            .get("euid")
            .and_then(Value::as_u64)
            .unwrap_or(501) as u32
    }
    fn win32(&self) -> bool {
        self.script.row["plat"] == "win32"
    }
    fn urandom(&mut self, count: usize) -> Vec<u8> {
        self.script.log(format!("urandom {count}"));
        let k = self.script.draws.get();
        self.script.draws.set(k + 1);
        (0..count)
            .map(|i| ((37 * k as usize + 11 * i + 5) % 256) as u8)
            .collect()
    }
    fn monotonic(&mut self) -> f64 {
        self.script.log("now".to_owned());
        self.script.clock.get()
    }
    fn sleep(&mut self, seconds: f64) {
        self.script.log(format!("pause {}", repr(seconds)));
        self.script.clock.set(self.script.clock.get() + seconds);
    }
}

/// An answer as the file records it.
pub fn recorded(answer: &Result<TokenWrite, Fault>) -> Value {
    match answer {
        Ok(w) => json!({"r": {"status": w.status.name(), "token": w.token, "detail": w.detail},
                        "ok": w.ok()}),
        Err(Fault::Raised(error)) => json!({"x": error.class.name()}),
        Err(Fault::NotPorted) => json!({"not_ported": true}),
    }
}

/// The row's answer from the file, in the form [`recorded`] gives.
pub fn expected(row: &Value) -> Value {
    match row.get("r") {
        Some(r) => json!({"r": r, "ok": row["ok"]}),
        None => json!({"x": row["x"]}),
    }
}
