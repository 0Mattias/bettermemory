//! The spelling of `membership_rung.jsonl`
//! (`tests/contract/gen_membership_rung.py`): the heal harness's store
//! with every call, a stub `_shared_remint`'s and `_token_read`'s
//! included, in one log; the sighting with the machine, git directory and
//! object key of the case; and the token files read on the host.

#![allow(dead_code)]

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

use bettermemory_core::capture::location::{Location, ObjectKey};
use bettermemory_core::checkout_token::read::Fault;
use bettermemory_core::checkout_token::token_path;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::fsdata::{Flavour, Fs, StdFs};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::location_reads::Board;
use bettermemory_core::log::LogRow;
use bettermemory_core::membership::rung::{token_file, token_read, TokenBoard};
use bettermemory_core::membership::shared_heal::MembershipLog;
use bettermemory_core::membership::{
    Error, LivenessSeam, Membership, MembershipStore, Options, Query, Written,
};
use bettermemory_core::membership_records::records::{Checkout, View};
use bettermemory_core::project_file;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::create::system_monotonic;
use bettermemory_core::unicode::{cps, to_string};

use super::common::membership_core::{raised, tables_of, Fake};
use super::common::membership_rows::{enc, field, list, name};
use super::heal::{self, Heal};

/// The host's flavour.
/// A working directory no path read here is taken from.
pub const CWD: &[u8] = if cfg!(windows) { b"C:\\" } else { b"/" };

pub const HOST: Flavour = if cfg!(windows) {
    Flavour::Windows
} else {
    Flavour::Posix
};

fn text(v: Option<&PyValue>) -> Option<PyStr> {
    match v {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    }
}

/// `_loc(spec)`.
pub fn location(spec: &PyValue) -> Location {
    let mut loc = heal::location(spec);
    loc.machine = text(spec.get("m")).unwrap_or_else(|| cps("M1"));
    if let Some(status) = text(spec.get("st")) {
        loc.token_status = status;
    }
    loc.git_dir = match spec.get("g") {
        None => Some(cps("/w/r/.git")),
        Some(v) => text(Some(v)),
    };
    loc.object_key = match spec.get("o") {
        Some(PyValue::None) => None,
        None => Some(ObjectKey {
            device: cps("16777220"),
            inode: cps("123"),
            birth: None,
        }),
        Some(o) => {
            let o = list(o);
            Some(ObjectKey {
                device: text(Some(&o[0])).expect("a device"),
                inode: text(Some(&o[1])).expect("an inode"),
                birth: text(Some(&o[2])),
            })
        }
    };
    loc
}

/// `_LogStub` with every call, the stub's included, in one log.
pub struct Rung {
    pub heal: Heal,
    pub calls: Rc<RefCell<Vec<PyValue>>>,
}

impl Rung {
    fn drain(&self) {
        let mut inner = self.heal.inner.0.borrow_mut();
        self.calls.borrow_mut().append(&mut inner.log);
    }
}

macro_rules! drained {
    ($self:ident, $e:expr) => {{
        let out = $e;
        $self.drain();
        out
    }};
}

impl MembershipStore for Rung {
    type Error = PyStr;
    fn in_batch(&self) -> bool {
        self.heal.in_batch()
    }
    fn batch<T>(&self, f: &mut dyn FnMut() -> Result<T, Error<PyStr>>) -> Result<T, Error<PyStr>> {
        drained!(self, self.heal.batch(f))
    }
    fn put_membership(&self, k: &[u32], r: &PyValue, s: Option<&[u32]>) -> Result<Written, PyStr> {
        drained!(self, self.heal.put_membership(k, r, s))
    }
    fn delete_membership(
        &self,
        k: &[u32],
        r: &PyValue,
        s: Option<&[u32]>,
    ) -> Result<Written, PyStr> {
        drained!(self, self.heal.delete_membership(k, r, s))
    }
    fn record_membership(
        &self,
        k: &[u32],
        p: &PyValue,
        s: Option<&[u32]>,
    ) -> Result<Written, PyStr> {
        drained!(self, self.heal.record_membership(k, p, s))
    }
    fn membership_rows(&self, table: &str, q: &Query) -> Result<Vec<PyValue>, PyStr> {
        drained!(self, self.heal.membership_rows(table, q))
    }
    fn next_membership_id(&self, table: &str, prefix: &str) -> Result<PyStr, PyStr> {
        drained!(self, self.heal.next_membership_id(table, prefix))
    }
    fn next_membership_number(&self, table: &str, column: &str) -> Result<i64, PyStr> {
        drained!(self, self.heal.next_membership_number(table, column))
    }
}

impl MembershipLog for Rung {
    fn membership_log(
        &self,
        kinds: Option<&[&str]>,
        checkout: Option<&[u32]>,
    ) -> Result<Vec<LogRow>, PyStr> {
        drained!(self, self.heal.membership_log(kinds, checkout))
    }
}

pub type RungModel = Membership<Rung>;

/// The model as `_run` builds it: `here` false gives a client's liveness.
pub fn model(case: &PyValue) -> RungModel {
    let tables = tables_of(case.get("tables").unwrap_or(&PyValue::Dict(Vec::new())));
    let log = case
        .get("log")
        .map(|l| {
            list(l)
                .iter()
                .enumerate()
                .map(|(i, r)| {
                    let r = list(r);
                    LogRow {
                        seq: i as i64 + 1,
                        ts: "t".into(),
                        session: None,
                        kind: name(&r[0]),
                        payload: name(&r[1]),
                        prev_mac: String::new(),
                        mac: String::new(),
                    }
                })
                .collect()
        })
        .unwrap_or_default();
    let dt = DateTime::from_isoformat(&cps("2026-03-04T05:06:07+00:00")).expect("a datetime");
    let liveness = if case.get("here") == Some(&PyValue::Bool(false)) {
        LivenessSeam::Given(Box::new(|_: &Checkout, _: &View| cps("live")))
    } else {
        LivenessSeam::Default {
            one: Box::new(|_: &Checkout, _: &View| cps("live")),
            many: Box::new(|views: &[(Checkout, View)]| vec![None; views.len()]),
        }
    };
    Membership::new(
        Rung {
            heal: Heal {
                inner: Fake::new(tables, None, false),
                log,
            },
            calls: Rc::new(RefCell::new(Vec::new())),
        },
        cps("M1"),
        Options {
            liveness,
            token_writer: None,
            now: Box::new(move || dt),
            session: None,
        },
    )
}

fn id(k: &Option<Checkout>) -> PyValue {
    k.as_ref()
        .map_or(PyValue::None, |k| PyValue::Str(k.id.clone()))
}

fn opt(v: Option<&PyStr>) -> PyValue {
    v.map_or(PyValue::None, |s| PyValue::Str(s.clone()))
}

/// The model's answer for a case, before spelling.
pub fn answer(m: &mut RungModel, case: &PyValue) -> Result<PyValue, Error<PyStr>> {
    let f = name(field(case, "f"));
    let loc = location(field(case, "loc"));
    let calls = Rc::clone(&m.store().calls);
    Ok(match f.as_str() {
        "rung_two" | "by_object" => {
            let said = case.get("remint") == Some(&PyValue::Bool(true));
            let mut remint = |k: &Checkout, _: &Location, found: Option<&PyStr>| {
                calls.borrow_mut().push(PyValue::List(vec![
                    PyValue::str("remint"),
                    PyValue::Str(k.id.clone()),
                    opt(found),
                ]));
                Ok(said)
            };
            if f == "rung_two" {
                let (k, how, first, settled) = m.rung_two(&loc, None, &mut remint)?;
                PyValue::List(vec![id(&k), PyValue::str(how), id(&first), id(&settled)])
            } else {
                let (k, how) = m.by_object(&loc, None, &mut remint)?;
                PyValue::List(vec![id(&k), PyValue::str(how)])
            }
        }
        "token_now" => {
            let disk = case
                .get("disk")
                .cloned()
                .unwrap_or(PyValue::Dict(Vec::new()));
            let before = loc.clone();
            let mut read = |g: &PyStr| {
                calls.borrow_mut().push(PyValue::List(vec![
                    PyValue::str("read"),
                    PyValue::Str(g.clone()),
                ]));
                Ok(text(disk.get(&to_string(g).expect("a git directory"))))
            };
            let got = m.token_now(loc, &mut read)?;
            PyValue::List(vec![
                opt(got.token.as_ref()),
                opt(got.token_written.as_ref()),
                PyValue::Str(got.token_status.clone()),
                PyValue::Bool(got == before),
            ])
        }
        other => panic!("no function {other}"),
    })
}

/// The case's answer and calls, spelled as the file spells them.
pub fn run(case: &PyValue) -> (PyValue, PyValue) {
    let mut m = model(case);
    let o = match answer(&mut m, case) {
        Ok(v) => enc(&v),
        Err(e) => raised(&e),
    };
    m.store().drain();
    let calls = enc(&PyValue::List(m.store().calls.borrow().clone()));
    (o, calls)
}

/// The board of the token reads, on the monotonic clock.
pub fn board() -> TokenBoard {
    Board::new(Arc::new(system_monotonic))
}

/// `_token_read(git_dir)` over the host's files, `evicted` forcing the
/// eviction test to answer true.
pub fn read_token(git_dir: &str, evicted: bool) -> Result<Option<PyStr>, Fault> {
    let path = token_path(git_dir, HOST);
    let git_dir = git_dir.to_owned();
    token_read(&board(), &path, move || {
        let ctx = Ctx {
            fs: &StdFs,
            flavour: HOST,
            cwd: CWD,
        };
        let test = |p: &[u8]| {
            let st = StdFs.stat(p)?;
            Ok(evicted || project_file::evicted(&st, cfg!(target_os = "macos")))
        };
        token_file(&ctx, &git_dir, &test, &mut |_| {})
    })
}
