//! The Python model and the native model read the same row sets and recording history.

use super::common::{
    membership_core::{raised, tables_of, Fake},
    membership_rows::{dec, enc, field, int, list, name},
    membership_views::location,
};
use bettermemory_core::{
    datetime::DateTime,
    log::LogRow,
    membership::{
        shared_heal::MembershipLog, Error, LivenessSeam, Membership, MembershipStore, Options,
        Query, Written,
    },
    membership_records::records::{Checkout, HistoryReader, Specific},
    membership_resolve::{
        tally::{strength, tally_in_order, tally_with_reads, unordered, TallyReads, TallyViews},
        TallyHistory,
    },
    membership_rows::Tally,
    pyvalue::{PyStr, PyValue},
    unicode::cps,
};
use std::{
    collections::{BTreeMap, BTreeSet},
    rc::Rc,
};

struct ReaderStore(Rc<Fake>, Vec<LogRow>);
impl MembershipStore for ReaderStore {
    type Error = PyStr;
    fn in_batch(&self) -> bool {
        self.0.in_batch()
    }
    fn batch<T>(&self, _: &mut dyn FnMut() -> Result<T, Error<PyStr>>) -> Result<T, Error<PyStr>> {
        panic!("a tally opened a batch")
    }
    fn put_membership(&self, _: &[u32], _: &PyValue, _: Option<&[u32]>) -> Result<Written, PyStr> {
        panic!("a tally put a row")
    }
    fn delete_membership(
        &self,
        _: &[u32],
        _: &PyValue,
        _: Option<&[u32]>,
    ) -> Result<Written, PyStr> {
        panic!("a tally deleted a row")
    }
    fn record_membership(
        &self,
        _: &[u32],
        _: &PyValue,
        _: Option<&[u32]>,
    ) -> Result<Written, PyStr> {
        panic!("a tally wrote an event")
    }
    fn membership_rows(&self, t: &str, q: &Query) -> Result<Vec<PyValue>, PyStr> {
        self.0.membership_rows(t, q)
    }
    fn next_membership_id(&self, _: &str, _: &str) -> Result<PyStr, PyStr> {
        panic!("a tally allocated an ID")
    }
    fn next_membership_number(&self, _: &str, _: &str) -> Result<i64, PyStr> {
        panic!("a tally allocated a number")
    }
}
impl MembershipLog for ReaderStore {
    fn membership_log(
        &self,
        kinds: Option<&[&str]>,
        checkout: Option<&[u32]>,
    ) -> Result<Vec<LogRow>, PyStr> {
        self.0 .0.borrow_mut().log.push(PyValue::List(vec![
            PyValue::str("log"),
            kinds.map_or(PyValue::None, |ks| {
                PyValue::List(ks.iter().map(|k| PyValue::str(k)).collect())
            }),
            checkout.map_or(PyValue::None, |k| PyValue::Str(k.to_vec())),
        ]));
        Ok(self.1.clone())
    }
}
fn text(v: &PyValue) -> PyStr {
    match v {
        PyValue::Str(s) => s.clone(),
        _ => panic!("string {v:?}"),
    }
}
fn values(v: Option<&PyValue>) -> Vec<PyStr> {
    v.map_or_else(Vec::new, |v| list(v).iter().map(text).collect())
}
fn strings<'a>(v: impl IntoIterator<Item = &'a PyStr>) -> PyValue {
    PyValue::List(v.into_iter().cloned().map(PyValue::Str).collect())
}
struct History<'a> {
    spec: &'a PyValue,
    store: Rc<Fake>,
}
impl History<'_> {
    fn read(&self, f: &str, args: PyValue) {
        self.store
            .0
            .borrow_mut()
            .log
            .push(PyValue::List(vec![PyValue::str(f), args]));
    }
    fn holds_some(&self, f: &str, ids: &[PyStr]) -> BTreeSet<PyStr> {
        self.read(f, strings(ids));
        let present = values(self.spec.get(f));
        ids.iter()
            .filter(|v| present.contains(v))
            .cloned()
            .collect()
    }
}
impl HistoryReader for History<'_> {
    fn holds(&self, ids: &[PyStr]) -> BTreeSet<PyStr> {
        self.holds_some("holds", ids)
    }
    fn holds_objects(&self, ids: &[PyStr]) -> BTreeSet<PyStr> {
        self.holds_some("objects", ids)
    }
    fn fingerprint(&self, sha: &[u32]) -> Option<PyStr> {
        self.read("fingerprint", PyValue::Str(sha.to_vec()));
        self.spec
            .get("fp")?
            .get(&name(&PyValue::Str(sha.to_vec())))
            .filter(|v| **v != PyValue::None)
            .map(text)
    }
    fn parents(&self, sha: &[u32]) -> Vec<PyStr> {
        self.read("parents", PyValue::Str(sha.to_vec()));
        values(
            self.spec
                .get("parents")
                .and_then(|m| m.get(&name(&PyValue::Str(sha.to_vec())))),
        )
    }
    fn reachable_among(&self, _: &[PyStr]) -> (BTreeMap<PyStr, BTreeSet<PyStr>>, Option<bool>) {
        panic!("unexpected history read")
    }
    fn blobs(&self, _: &[u32]) -> Vec<PyStr> {
        panic!("unexpected blob listing")
    }
}
struct FaultHistory<'a> {
    native: &'a History<'a>,
    fail: &'a str,
}
fn fault_error(read: &str) -> Error<PyStr> {
    let mut message = cps(&format!("failed {read}"));
    message.push(0xdcff);
    Error::Store(message)
}
impl FaultHistory<'_> {
    fn after<T>(&self, read: &str, answer: T) -> Result<T, Error<PyStr>> {
        if read == self.fail {
            Err(fault_error(read))
        } else {
            Ok(answer)
        }
    }
}
impl TallyHistory<PyStr> for FaultHistory<'_> {
    fn holds(&self, c: &[PyStr]) -> Result<BTreeSet<PyStr>, Error<PyStr>> {
        self.after("holds", self.native.holds(c))
    }
    fn fingerprint(&self, c: &[u32]) -> Result<Option<PyStr>, Error<PyStr>> {
        self.after("fingerprint", self.native.fingerprint(c))
    }
    fn parents(&self, c: &[u32]) -> Result<Vec<PyStr>, Error<PyStr>> {
        self.after("parents", self.native.parents(c))
    }
    fn holds_objects(&self, c: &[PyStr]) -> Result<BTreeSet<PyStr>, Error<PyStr>> {
        self.after("holds_objects", self.native.holds_objects(c))
    }
}
struct FaultViews<'a>(&'a str);
impl FaultViews<'_> {
    fn check(
        &self,
        model: &Membership<ReaderStore>,
        kind: &str,
        k: &Checkout,
    ) -> Result<(), Error<PyStr>> {
        if self.0 == kind {
            model.store().0 .0.borrow_mut().log.push(PyValue::List(vec![
                PyValue::str(kind),
                PyValue::Str(k.id.clone()),
            ]));
            return Err(fault_error(kind));
        }
        Ok(())
    }
}
impl TallyViews<Membership<ReaderStore>> for FaultViews<'_> {
    fn view_state(
        &mut self,
        m: &mut Membership<ReaderStore>,
        k: &Checkout,
    ) -> Result<PyStr, Error<PyStr>> {
        self.check(m, "view_state", k)?;
        Ok(m.view_state(k, None))
    }
    fn live_view(
        &mut self,
        m: &mut Membership<ReaderStore>,
        k: &Checkout,
    ) -> Result<Option<PyStr>, Error<PyStr>> {
        self.check(m, "live_view", k)?;
        Ok(m.live_view(k))
    }
}
fn pairs(v: &[(PyStr, BTreeSet<PyStr>)]) -> PyValue {
    PyValue::List(
        v.iter()
            .map(|(p, c)| PyValue::List(vec![PyValue::Str(p.clone()), strings(c)]))
            .collect(),
    )
}
fn answer(t: Tally) -> PyValue {
    PyValue::List(vec![
        pairs(&t.cands),
        PyValue::List(
            t.contradictions
                .iter()
                .map(|(a, b, c)| strings([a, b, c]))
                .collect(),
        ),
        PyValue::List(t.rekey.iter().map(|(a, b)| strings([a, b])).collect()),
        strings(&t.superseded),
        strings(&t.dropped),
        pairs(&t.held),
    ])
}
fn history_order(case: &PyValue) -> bettermemory_core::membership_resolve::HistoryOrder {
    let Some(order) = case.get("history_order") else {
        return Default::default();
    };
    let map = |k| match order.get(k) {
        Some(PyValue::Dict(items)) => items
            .iter()
            .map(|(k, v)| (text(k), values(Some(v))))
            .collect(),
        _ => BTreeMap::new(),
    };
    bettermemory_core::membership_resolve::HistoryOrder {
        commits: values(order.get("commits")),
        projects_by_commit: map("projects_by_commit"),
        projects_by_fingerprint: map("projects_by_fingerprint"),
    }
}
fn flag(case: &PyValue, k: &str) -> bool {
    case.get(k) == Some(&PyValue::Bool(true))
}
pub fn run(header: &PyValue, case: &PyValue) -> (PyValue, Option<PyValue>) {
    let f = name(field(case, "f"));
    if f == "unordered" {
        let spec = Specific {
            best: vec![],
            held: 4,
            top: BTreeSet::new(),
            bounded: flag(case, "bounded"),
        };
        let names = dec(field(case, "names"));
        return (PyValue::Str(unordered(&spec, &values(Some(&names)))), None);
    }
    let tables = &list(field(header, "tables"))[int(field(case, "tables")) as usize];
    let fail = case
        .get("fail")
        .map(|a| (name(&list(a)[0]), int(&list(a)[1]) as usize));
    let store = Rc::new(Fake::new(tables_of(tables), fail, false));
    let logs = case
        .get("log")
        .map(|rs| {
            list(rs)
                .iter()
                .enumerate()
                .map(|(i, row)| LogRow {
                    seq: i as i64 + 1,
                    kind: name(&list(row)[0]),
                    payload: name(&list(row)[1]),
                    ts: "t".into(),
                    session: None,
                    prev_mac: String::new(),
                    mac: String::new(),
                })
                .collect()
        })
        .unwrap_or_default();
    let live = case.get("live") != Some(&PyValue::Bool(false));
    let trace_liveness = flag(case, "trace_liveness");
    let view_store = Rc::clone(&store);
    let mut model = Membership::new(
        ReaderStore(Rc::clone(&store), logs),
        cps("M1"),
        Options {
            liveness: LivenessSeam::Given(Box::new(move |checkout, view| {
                if trace_liveness {
                    view_store.0.borrow_mut().log.push(PyValue::List(vec![
                        PyValue::str("liveness"),
                        PyValue::Str(checkout.id.clone()),
                        PyValue::Str(view.path.clone()),
                    ]));
                }
                cps(if live { "live" } else { "gone" })
            })),
            token_writer: None,
            now: Box::new(|| DateTime::from_isoformat(&cps("2026-03-04T05:06:07+00:00")).unwrap()),
            session: None,
        },
    );
    let mut loc = location(case.get("loc").unwrap_or(&PyValue::None));
    loc.shallow = flag(case, "shallow");
    let result = if f == "strength" {
        if flag(case, "born") {
            loc.head = Some(cps("h"));
        }
        let all = values(header.get("codes"));
        let mut digits = String::new();
        for mask in 0..1_usize << all.len() {
            let mut codes: Vec<_> = all
                .iter()
                .enumerate()
                .filter(|(i, _)| mask & (1 << i) != 0)
                .map(|(_, v)| v.clone())
                .collect();
            if let Some(first) = codes.first() {
                codes.push(first.clone());
            }
            digits.push_str(
                &strength(&mut model, &loc, &cps("P"), &codes)
                    .unwrap()
                    .to_string(),
            );
        }
        PyValue::str(&digits)
    } else {
        let ordered = values(case.get("keys"));
        loc.remote_keys = ordered.iter().cloned().collect();
        let history = case
            .get("history")
            .filter(|v| **v != PyValue::None)
            .map(|spec| History {
                spec,
                store: Rc::clone(&store),
            });
        let tenant = case
            .get("tenant")
            .filter(|v| **v != PyValue::None)
            .map(text);
        let result = if let Some(fail) = case.get("fail_read") {
            let fail = name(fail);
            let fault_history = history.as_ref().map(|native| FaultHistory {
                native,
                fail: &fail,
            });
            let mut views = FaultViews(&fail);
            tally_with_reads(
                &mut model,
                &loc,
                tenant.as_deref(),
                &values(case.get("exclude")),
                &mut TallyReads {
                    history: fault_history
                        .as_ref()
                        .map(|h| h as &dyn TallyHistory<PyStr>),
                    remote_order: &ordered,
                    history_order: &history_order(case),
                    views: Some(&mut views),
                },
            )
        } else {
            tally_in_order(
                &mut model,
                &loc,
                history.as_ref().map(|h| h as &dyn HistoryReader),
                tenant.as_deref(),
                &values(case.get("exclude")),
                &ordered,
                &history_order(case),
            )
        };
        result.map_or_else(|error| raised(&error), answer)
    };
    let calls = enc(&PyValue::List(store.0.borrow().log.clone()));
    (result, Some(calls))
}
