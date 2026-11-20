//! The scripted OS of `checkout_token_read.jsonl` as a file system: each
//! `_read_text` takes the next script of the row, and every call is
//! logged as the generator logs it (`gen_checkout_token_read`).

#![allow(dead_code)]

use std::cell::RefCell;
use std::collections::VecDeque;

use bettermemory_core::checkout_token::read::{Fault, TokenRead};
use bettermemory_core::fsdata::{Access, Fs, Kind, OsError, PyErr, Stat, StatResult};
use serde_json::Value;

/// The working directory a relative path is taken from.
pub const CWD: &str = "/cwd";
/// What `F_GETFL` answers besides the non-blocking flag.
pub const BASE: i32 = 0o2000;
/// The non-blocking flag of the scripted platform.
pub const NONBLOCK: i32 = 4;

const NAMES: &[&str] = &[
    "ENOENT",
    "ENOTDIR",
    "EACCES",
    "EPERM",
    "ELOOP",
    "ENAMETOOLONG",
    "EISDIR",
    "EINTR",
    "EAGAIN",
    "ENXIO",
    "EINVAL",
    "EIO",
    "EBADF",
];

pub fn errno(name: &str) -> &'static str {
    NAMES
        .iter()
        .find(|n| **n == name)
        .unwrap_or_else(|| panic!("an errno this script knows: {name}"))
}

pub fn fail(name: &str) -> OsError {
    let name = errno(name);
    OsError::new(PyErr::of_errno(name), name)
}

enum Item {
    Bytes(Vec<u8>),
    Error(&'static str),
}

pub struct Script {
    passes: RefCell<VecDeque<Value>>,
    now: RefCell<Value>,
    reads: RefCell<VecDeque<Item>>,
    pub log: RefCell<Vec<String>>,
    pub win32: bool,
}

fn answer(spec: &Value, key: &str, default: &str) -> String {
    spec.get(key)
        .and_then(Value::as_str)
        .unwrap_or(default)
        .to_owned()
}

impl Script {
    pub fn new(passes: &Value, win32: bool) -> Script {
        Script {
            passes: RefCell::new(passes.as_array().expect("passes").iter().cloned().collect()),
            now: RefCell::new(Value::Null),
            reads: RefCell::new(VecDeque::new()),
            log: RefCell::new(Vec::new()),
            win32,
        }
    }

    pub fn left(&self) -> usize {
        self.passes.borrow().len()
    }

    fn kind(&self, key: &str) -> Result<Kind, OsError> {
        match answer(&self.now.borrow(), key, "reg").as_str() {
            "reg" => Ok(Kind::Regular),
            "fifo" => Ok(Kind::Fifo),
            "dir" => Ok(Kind::Directory),
            other => Err(fail(other)),
        }
    }

    fn ok_or_fail(&self, key: &str) -> Result<(), OsError> {
        match answer(&self.now.borrow(), key, "ok").as_str() {
            "ok" => Ok(()),
            other => Err(fail(other)),
        }
    }
}

impl Fs for Script {
    type File = ();

    fn open(&self, path: &[u8]) -> Result<(), PyErr> {
        self.open_result(path).map_err(|e| e.class)
    }

    fn open_result(&self, path: &[u8]) -> Result<(), OsError> {
        if path.contains(&0) {
            return Err(OsError::value());
        }
        let spec = self.passes.borrow_mut().pop_front().expect("a script left");
        let mut reads = VecDeque::new();
        for item in spec
            .get("reads")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if let Some(e) = item.get("e") {
                reads.push_back(Item::Error(errno(e.as_str().expect("errno"))));
            } else if let Some(t) = item.get("t") {
                reads.push_back(Item::Bytes(t.as_str().expect("text").as_bytes().to_vec()));
            } else {
                let rep = &item["rep"];
                let byte = u8::from_str_radix(rep[0].as_str().expect("hex"), 16).expect("hex");
                let count = rep[1].as_u64().expect("count") as usize;
                reads.push_back(Item::Bytes(vec![byte; count]));
            }
        }
        *self.reads.borrow_mut() = reads;
        *self.now.borrow_mut() = spec;
        let text = String::from_utf8(path.to_vec()).expect("UTF-8 path");
        let shown = text
            .strip_prefix(&format!("{CWD}/"))
            .unwrap_or(&text)
            .to_owned();
        self.log.borrow_mut().push(format!("open {shown}"));
        self.ok_or_fail("open")
    }

    fn open_blocking(&self, _path: &[u8]) -> Result<(), OsError> {
        self.log.borrow_mut().push("open2".to_owned());
        self.ok_or_fail("open2")
    }

    fn fstat(&self, file: &mut ()) -> Result<Stat, PyErr> {
        self.fstat_result(file).map_err(|e| e.class)
    }

    fn fstat_result(&self, _file: &mut ()) -> Result<Stat, OsError> {
        self.log.borrow_mut().push("fstat".to_owned());
        Ok(Stat {
            kind: self.kind("fstat")?,
            identity: None,
            size: 0,
        })
    }

    fn read(&self, file: &mut (), max: usize) -> Result<Vec<u8>, PyErr> {
        self.read_once(file, max).map_err(|e| e.class)
    }

    fn read_once(&self, _file: &mut (), max: usize) -> Result<Vec<u8>, OsError> {
        self.log.borrow_mut().push(format!("read {max}"));
        let mut reads = self.reads.borrow_mut();
        match reads.pop_front() {
            None => Ok(Vec::new()),
            Some(Item::Error(name)) => Err(fail(name)),
            Some(Item::Bytes(mut data)) => {
                if data.len() > max {
                    let rest = data.split_off(max);
                    reads.push_front(Item::Bytes(rest));
                }
                Ok(data)
            }
        }
    }

    fn close(&self, _file: ()) -> Result<(), OsError> {
        self.log.borrow_mut().push("close".to_owned());
        self.ok_or_fail("close")
    }

    fn nonblock_flag(&self) -> Option<i32> {
        (!self.win32).then_some(NONBLOCK)
    }

    fn status_flags(&self, _file: &mut ()) -> Result<i32, OsError> {
        self.log.borrow_mut().push("getfl".to_owned());
        self.ok_or_fail("getfl")?;
        Ok(BASE | NONBLOCK)
    }

    fn set_status_flags(&self, _file: &mut (), flags: i32) -> Result<(), OsError> {
        assert_eq!(flags, BASE, "F_SETFL is given the flags less O_NONBLOCK");
        self.log.borrow_mut().push("setfl".to_owned());
        self.ok_or_fail("setfl")
    }

    fn strerror(&self, error: &OsError) -> Option<String> {
        error.errno.map(|name| format!("text of {name}"))
    }

    fn stat(&self, _path: &[u8]) -> Result<StatResult, OsError> {
        self.log.borrow_mut().push("stat".to_owned());
        Ok(StatResult {
            kind: self.kind("stat")?,
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

    fn access(&self, _path: &[u8], _mode: Access) -> Result<bool, PyErr> {
        unreachable!("the reads ask no access")
    }
    fn open_no_follow(&self, _path: &[u8]) -> Result<(), OsError> {
        unreachable!("the reads follow links")
    }
    fn lstat(&self, _path: &[u8]) -> Result<Kind, PyErr> {
        unreachable!("the reads make no lstat")
    }
    fn readlink(&self, _path: &[u8]) -> Result<Vec<u8>, PyErr> {
        unreachable!("the reads read no link")
    }
    fn lstat_result(&self, _path: &[u8]) -> Result<StatResult, OsError> {
        unreachable!("the reads make no lstat")
    }
    fn listdir(&self, _path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        unreachable!("the reads list no folder")
    }
}

/// An answer as the file records it: `{"r": ...}` or `{"x": class}`.
pub fn recorded(answer: &Result<TokenRead, Fault>) -> Value {
    match answer {
        Ok(found) => serde_json::json!({"r": {
            "state": found.state.name(),
            "token": found.token,
            "detail": found.detail,
        }}),
        Err(Fault::Raised(error)) => serde_json::json!({"x": error.class.name()}),
        Err(Fault::NotPorted) => serde_json::json!({"not_ported": true}),
    }
}

/// The row's answer from the file, in the form [`recorded`] gives.
pub fn expected(row: &Value) -> Value {
    match row.get("r") {
        Some(r) => serde_json::json!({ "r": r }),
        None => serde_json::json!({ "x": row["x"] }),
    }
}
