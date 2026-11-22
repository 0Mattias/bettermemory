//! The doubles the transcript tests share: a file held in memory with the
//! generator's faults, and an `OwnerPrompt` that gives back the answers
//! the reference gave, in order.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::transcript::{Opener, OsFailure, OwnerPrompt};
use serde_json::Value;

/// One file in memory, shared so a test can change it mid-walk.
#[derive(Clone, Default)]
pub struct MemTranscript {
    pub data: Rc<RefCell<Vec<u8>>>,
    /// None: the file; Some("missing") or Some("dir"): the open fails.
    pub refuse: Option<&'static str>,
    /// What `os.fstat` adds to the size.
    pub extra: u64,
    /// Counts of each call, for the kills.
    pub reads: Rc<RefCell<usize>>,
    /// Fail the read with this index (0-based), when set.
    pub fail_read: Option<usize>,
}

pub struct MemHandle {
    at: u64,
}

impl MemTranscript {
    pub fn new(data: Vec<u8>) -> MemTranscript {
        MemTranscript {
            data: Rc::new(RefCell::new(data)),
            ..MemTranscript::default()
        }
    }
}

impl Opener for MemTranscript {
    type File = MemHandle;

    fn open(&self, _path: &[u8]) -> Result<MemHandle, OsFailure> {
        match self.refuse {
            Some(_) => Err(OsFailure),
            None => Ok(MemHandle { at: 0 }),
        }
    }

    fn size(&self, _file: &mut MemHandle) -> Result<u64, OsFailure> {
        Ok(self.data.borrow().len() as u64 + self.extra)
    }

    fn seek(&self, file: &mut MemHandle, offset: u64) -> Result<(), OsFailure> {
        file.at = offset;
        Ok(())
    }

    fn read(&self, file: &mut MemHandle, n: usize) -> Result<Vec<u8>, OsFailure> {
        let index = {
            let mut reads = self.reads.borrow_mut();
            *reads += 1;
            *reads - 1
        };
        if self.fail_read == Some(index) {
            return Err(OsFailure);
        }
        let data = self.data.borrow();
        let start = usize::try_from(file.at)
            .unwrap_or(usize::MAX)
            .min(data.len());
        let end = start.saturating_add(n).min(data.len());
        file.at = end as u64;
        Ok(data[start..end].to_vec())
    }
}

/// A call into `owner_prompt.py` and the answer the reference gave.
#[derive(Clone, Debug)]
pub enum Call {
    Submitted(bool),
    Owner(Option<PyStr>),
    Blocks(Option<PyStr>),
}

/// Gives back the recorded answers in order; a call out of order panics.
#[derive(Default)]
pub struct Replay {
    pub calls: RefCell<VecDeque<Call>>,
}

fn opt(value: &Value) -> Option<PyStr> {
    if value.is_null() {
        None
    } else {
        Some(super::text_field(value, "v"))
    }
}

impl Replay {
    pub fn of(calls: &Value) -> Replay {
        let calls = calls
            .as_array()
            .expect("calls")
            .iter()
            .map(|c| match c[0].as_str().unwrap() {
                "s" => Call::Submitted(c[1].as_bool().unwrap()),
                "o" => Call::Owner(opt(&c[1])),
                "t" => Call::Blocks(opt(&c[1])),
                other => panic!("call {other}"),
            })
            .collect();
        Replay {
            calls: RefCell::new(calls),
        }
    }

    pub fn left(&self) -> usize {
        self.calls.borrow().len()
    }

    fn next(&self, what: &str) -> Call {
        self.calls
            .borrow_mut()
            .pop_front()
            .unwrap_or_else(|| panic!("an extra call: {what}"))
    }
}

impl OwnerPrompt for Replay {
    fn is_submitted(&self, _row: &PyValue) -> bool {
        match self.next("is_submitted") {
            Call::Submitted(b) => b,
            other => panic!("is_submitted where the reference made {other:?}"),
        }
    }

    fn owner_prompt(&self, _row: &PyValue, cut: bool) -> Option<PyStr> {
        assert!(cut, "stop_turn asks with the default cut");
        match self.next("owner_prompt") {
            Call::Owner(answer) => answer,
            other => panic!("owner_prompt where the reference made {other:?}"),
        }
    }

    fn submitted_text(&self, _row: &PyValue) -> Option<PyStr> {
        panic!("stop_turn makes no submitted_text call")
    }

    fn text_blocks(&self, _content: &PyValue) -> Option<PyStr> {
        match self.next("text_blocks") {
            Call::Blocks(answer) => answer,
            other => panic!("text_blocks where the reference made {other:?}"),
        }
    }
}

/// The generator's runs, joined.
pub fn runs(value: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    for run in value.as_array().expect("runs") {
        let piece = super::unhex(run[0].as_str().unwrap());
        for _ in 0..run[1].as_u64().unwrap() {
            out.extend_from_slice(&piece);
        }
    }
    out
}

/// The generator's `_big_lines(count)`.
pub fn big_lines(count: usize) -> Vec<u8> {
    let mut out = Vec::new();
    for i in 0..count {
        out.extend_from_slice(format!("line-{i}-").as_bytes());
        out.extend(std::iter::repeat_n(b'x', i % 97));
        out.push(b'\n');
    }
    out
}
