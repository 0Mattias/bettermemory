//! The recording store of `tests/contract/gen_membership_core.py`
//! (`_Stub`) and the generator's steps, driven over the native
//! `Membership`: canned table rows, every call logged, a batch rolled back
//! on an error, a failure injected at the n-th call of a method.

use std::cell::RefCell;
use std::cmp::Ordering;
use std::collections::{BTreeMap, HashMap};

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::membership::{
    Error, LivenessSeam, Membership, MembershipStore, Options, Query, Written,
};
use bettermemory_core::membership_records::records::{Checkout, Project, View};
use bettermemory_core::membership_rows as rows;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::{self, cps};
use sha2::{Digest, Sha256};

use super::membership_rows::{dec, enc, field, int, list, name};

#[derive(Clone, Default)]
pub struct State {
    pub tables: BTreeMap<String, Vec<PyValue>>,
    pub log: Vec<PyValue>,
    pub seq: i64,
    pub depth: usize,
    pub outer: bool,
    pub fail: Option<(String, usize)>,
    pub calls: HashMap<String, usize>,
}

/// `_Stub`.
pub struct Fake(pub RefCell<State>);

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn opt(v: Option<&[u32]>) -> PyValue {
    v.map_or(PyValue::None, |t| PyValue::Str(t.to_vec()))
}

fn txt(v: &[u32]) -> String {
    unicode::to_string(v).expect("a name")
}

/// Python's `<` over the values the fake sorts: ints by value, strs by
/// code point, a non-str before a str.
pub fn order(a: &PyValue, b: &PyValue) -> Ordering {
    match (a, b) {
        (PyValue::Str(x), PyValue::Str(y)) => x.cmp(y),
        (PyValue::Str(_), _) => Ordering::Greater,
        (_, PyValue::Str(_)) => Ordering::Less,
        (PyValue::Int(x), PyValue::Int(y)) => x
            .parse::<i128>()
            .unwrap_or(0)
            .cmp(&y.parse::<i128>().unwrap_or(0)),
        _ => Ordering::Equal,
    }
}

fn kind_table(kind: &[u32]) -> (&'static str, &'static str) {
    match txt(kind).as_str() {
        "project" => ("projects", "id"),
        "checkout" => ("checkouts", "id"),
        "project_file" => ("project_files", "file_id"),
        other => panic!("no table for {other}"),
    }
}

static NONE: PyValue = PyValue::None;

fn get<'a>(row: &'a PyValue, k: &str) -> &'a PyValue {
    row.get(k).unwrap_or(&NONE)
}

impl Fake {
    pub fn new(
        tables: BTreeMap<String, Vec<PyValue>>,
        fail: Option<(String, usize)>,
        outer: bool,
    ) -> Fake {
        Fake(RefCell::new(State {
            tables,
            outer,
            fail,
            ..State::default()
        }))
    }

    fn note(&self, method: &str, mut args: Vec<PyValue>) -> Result<(), PyStr> {
        let mut st = self.0.borrow_mut();
        args.insert(0, s(method));
        st.log.push(PyValue::List(args));
        let n = st.calls.entry(method.to_string()).or_insert(0);
        *n += 1;
        let n = *n;
        if st
            .fail
            .as_ref()
            .is_some_and(|(m, at)| m == method && *at == n)
        {
            return Err(cps("injected"));
        }
        Ok(())
    }

    fn written(&self) -> Written {
        let mut st = self.0.borrow_mut();
        st.seq += 1;
        Written { seq: st.seq }
    }
}

impl MembershipStore for Fake {
    type Error = PyStr;

    fn in_batch(&self) -> bool {
        let st = self.0.borrow();
        st.outer || st.depth > 0
    }

    fn batch<T>(&self, f: &mut dyn FnMut() -> Result<T, Error<PyStr>>) -> Result<T, Error<PyStr>> {
        let snapshot = {
            let mut st = self.0.borrow_mut();
            st.log.push(PyValue::List(vec![s("batch")]));
            st.depth += 1;
            (st.tables.clone(), st.seq)
        };
        let got = f();
        let mut st = self.0.borrow_mut();
        match &got {
            Ok(_) => st.log.push(PyValue::List(vec![s("commit")])),
            Err(_) => {
                st.tables = snapshot.0;
                st.seq = snapshot.1;
                st.log.push(PyValue::List(vec![s("rollback")]));
            }
        }
        st.depth -= 1;
        got
    }

    fn put_membership(
        &self,
        kind: &[u32],
        row: &PyValue,
        session: Option<&[u32]>,
    ) -> Result<Written, PyStr> {
        self.note(
            "put",
            vec![PyValue::Str(kind.to_vec()), row.clone(), opt(session)],
        )?;
        let (table, key) = kind_table(kind);
        {
            let mut st = self.0.borrow_mut();
            let rs = st.tables.entry(table.to_string()).or_default();
            match rs.iter().position(|r| get(r, key) == get(row, key)) {
                Some(i) => rs[i] = row.clone(),
                None => rs.push(row.clone()),
            }
        }
        Ok(self.written())
    }

    fn delete_membership(
        &self,
        kind: &[u32],
        key: &PyValue,
        session: Option<&[u32]>,
    ) -> Result<Written, PyStr> {
        self.note(
            "delete",
            vec![PyValue::Str(kind.to_vec()), key.clone(), opt(session)],
        )?;
        let (table, col) = kind_table(kind);
        {
            let mut st = self.0.borrow_mut();
            let rs = st.tables.entry(table.to_string()).or_default();
            rs.retain(|r| get(r, col) != get(key, col));
        }
        Ok(self.written())
    }

    fn record_membership(
        &self,
        kind: &[u32],
        payload: &PyValue,
        session: Option<&[u32]>,
    ) -> Result<Written, PyStr> {
        self.note(
            "record",
            vec![PyValue::Str(kind.to_vec()), payload.clone(), opt(session)],
        )?;
        {
            let mut st = self.0.borrow_mut();
            st.tables
                .entry("events".to_string())
                .or_default()
                .push(PyValue::Dict(vec![
                    (s("kind"), PyValue::Str(kind.to_vec())),
                    (s("payload"), payload.clone()),
                ]));
        }
        Ok(self.written())
    }

    fn membership_rows(&self, table: &str, q: &Query) -> Result<Vec<PyValue>, PyStr> {
        let where_ = q.where_.as_ref().map_or(PyValue::None, |w| {
            PyValue::Dict(
                w.iter()
                    .map(|(k, v)| (PyValue::Str(k.clone()), v.clone()))
                    .collect(),
            )
        });
        let spelled = q.column_in.as_ref().map_or(PyValue::None, |(c, vs)| {
            let mut vs = vs.clone();
            vs.sort_by(order);
            PyValue::List(vec![PyValue::Str(c.clone()), PyValue::List(vs)])
        });
        let by = PyValue::List(q.order_by.iter().map(|c| PyValue::Str(c.clone())).collect());
        self.note("rows", vec![s(table), where_, spelled, by])?;
        let st = self.0.borrow();
        let mut out: Vec<PyValue> = st
            .tables
            .get(table)
            .map(|rs| {
                rs.iter()
                    .filter(|r| {
                        q.where_
                            .as_ref()
                            .is_none_or(|w| w.iter().all(|(k, v)| get(r, &txt(k)) == v))
                            && q.column_in
                                .as_ref()
                                .is_none_or(|(c, vs)| vs.contains(get(r, &txt(c))))
                    })
                    .cloned()
                    .collect()
            })
            .unwrap_or_default();
        for c in q.order_by.iter().rev() {
            let c = txt(c);
            out.sort_by(|a, b| order(get(a, &c), get(b, &c)));
        }
        Ok(out)
    }

    fn next_membership_id(&self, table: &str, prefix: &str) -> Result<PyStr, PyStr> {
        self.note("next_id", vec![s(table), s(prefix)])?;
        let st = self.0.borrow();
        let mut high = 0u64;
        for r in st.tables.get(table).into_iter().flatten() {
            if let PyValue::Str(id) = get(r, "id") {
                let id = txt(id);
                if let Some(rest) = id.strip_prefix(prefix) {
                    if !rest.is_empty() && rest.bytes().all(|b| b.is_ascii_digit()) {
                        high = high.max(rest.parse().unwrap_or(0));
                    }
                }
            }
        }
        Ok(cps(&format!("{prefix}{:03}", high + 1)))
    }

    fn next_membership_number(&self, table: &str, column: &str) -> Result<i64, PyStr> {
        self.note("next_number", vec![s(table), s(column)])?;
        let st = self.0.borrow();
        let high = st
            .tables
            .get(table)
            .into_iter()
            .flatten()
            .map(|r| int(get(r, column)) as i64)
            .max()
            .unwrap_or(0);
        Ok(high + 1)
    }
}

pub type Model = Membership<Fake>;

/// The model over a fake, as `_sequence` builds it.
pub fn model(
    init: &PyValue,
    tables: BTreeMap<String, Vec<PyValue>>,
    fail: Option<(String, usize)>,
    outer: bool,
) -> Model {
    let now = list(field(init, "now"));
    let mut dt = DateTime::from_isoformat(&cps(&name(&now[0]))).expect("a datetime");
    dt.offset_us = Some(int(&now[1]) as i64 * 60_000_000);
    let session = match field(init, "session") {
        PyValue::Str(t) => Some(t.clone()),
        _ => None,
    };
    let liveness = if field(init, "given") == &PyValue::Bool(true) {
        LivenessSeam::Given(Box::new(|_: &Checkout, _: &View| cps("live")))
    } else {
        LivenessSeam::Default {
            one: Box::new(|_: &Checkout, _: &View| cps("live")),
            many: Box::new(|views: &[(Checkout, View)]| vec![None; views.len()]),
        }
    };
    Membership::new(
        Fake::new(tables, fail, outer),
        cps("M1"),
        Options {
            liveness,
            token_writer: None,
            now: Box::new(move || dt),
            session,
        },
    )
}

/// The rows of a spelled table set (`_spelled`).
pub fn tables_of(spelled: &PyValue) -> BTreeMap<String, Vec<PyValue>> {
    match spelled {
        PyValue::Dict(items) => items
            .iter()
            .map(|(k, v)| (name(k), list(v).iter().map(dec).collect()))
            .collect(),
        other => panic!("not tables: {other:?}"),
    }
}

/// `_sha(_spelled(tables))`.
pub fn tables_sha(st: &State) -> String {
    let spelled = PyValue::Dict(
        st.tables
            .iter()
            .map(|(k, v)| (s(k), enc(&PyValue::List(v.clone()))))
            .collect(),
    );
    let options = DumpOptions {
        sort_keys: true,
        item_separator: ",",
        key_separator: ":",
        ..DumpOptions::DEFAULT
    };
    let text = json::dumps(&spelled, options).expect("dumps");
    let mut h = Sha256::new();
    h.update(txt(&text).as_bytes());
    super::hex(&h.finalize())
}

pub fn project_out(p: &Project) -> PyValue {
    rows::project_row(p)
}

pub fn view_out(v: &View) -> PyValue {
    PyValue::Dict(vec![
        (s("checkout"), PyValue::Str(v.checkout.clone())),
        (s("machine"), PyValue::Str(v.machine.clone())),
        (s("path"), PyValue::Str(v.path.clone())),
        (s("first_seen"), PyValue::Str(v.first_seen.clone())),
        (s("last_seen"), PyValue::Str(v.last_seen.clone())),
        (s("order"), PyValue::int(i128::from(v.order))),
    ])
}

pub fn checkout_out(k: &Checkout) -> PyValue {
    PyValue::List(vec![
        rows::checkout_row(k),
        PyValue::List(k.views.iter().map(view_out).collect()),
    ])
}

fn strs(v: impl IntoIterator<Item = PyStr>) -> PyValue {
    PyValue::List(v.into_iter().map(PyValue::Str).collect())
}

fn ostr(v: Option<PyStr>) -> PyValue {
    v.map_or(PyValue::None, PyValue::Str)
}

fn arg(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::Str(t) => Some(t.clone()),
        PyValue::None => None,
        other => panic!("not a str or null: {other:?}"),
    }
}

fn a_str(v: &PyValue) -> PyStr {
    arg(v).expect("a str")
}

/// What a raise is spelled as in the file.
pub fn raised(e: &Error<PyStr>) -> PyValue {
    let (class, text) = match e {
        Error::Store(m) => ("StubError".to_string(), PyValue::Str(m.clone())),
        Error::Raised(r) if r.class == "KeyError" => (r.class.to_string(), s(&r.message)),
        Error::Raised(r) => (r.class.to_string(), PyValue::None),
    };
    PyValue::Dict(vec![(s("raised"), PyValue::List(vec![s(&class), text]))])
}

/// `_answer(m, op, a)`, its value already spelled.
pub fn answer(m: &mut Model, op: &str, a: &PyValue) -> Result<PyValue, Error<PyStr>> {
    Ok(match op {
        "reads_here" => PyValue::Bool(m.reads_here()),
        "stamp" => PyValue::Str(m.stamp()?),
        "project" => project_out(&m.project(&a_str(a))?),
        "projects" => PyValue::List(m.projects()?.iter().map(project_out).collect()),
        "canonical" => ostr(m.canonical(arg(a).as_deref())?),
        "canon" => PyValue::Str(m.canon(&a_str(a))?),
        "named" => {
            let scopes: Vec<PyStr> = list(a).iter().map(a_str).collect();
            strs(m.named(&scopes)?)
        }
        "chain_of" => strs(m.chain_of(arg(a).as_deref())?),
        "members" => strs(m.members(&a_str(a))?),
        "members_index" => PyValue::List(
            m.members_index()?
                .iter()
                .map(|(k, v)| PyValue::List(vec![PyValue::Str(k.clone()), strs(v.clone())]))
                .collect(),
        ),
        "witnessed" => strs(m.witnessed()?),
        "witness_commits" => strs(m.witness_commits(arg(a).as_deref())?),
        "has_witness" => {
            let a = list(a);
            PyValue::Bool(m.has_witness(&a_str(&a[0]), &a_str(&a[1]))?)
        }
        "has_witnesses" => PyValue::Bool(m.has_witnesses(&a_str(a))?),
        "file_binding" => ostr(m.file_binding(&a_str(a))?),
        "file_binding_read" => ostr(m.file_binding_read(&a_str(a))?),
        "checkout" => checkout_out(&m.checkout(&a_str(a))?),
        "checkouts" => PyValue::List(
            m.checkouts(arg(a).as_deref())?
                .iter()
                .map(checkout_out)
                .collect(),
        ),
        "checkouts_of" => PyValue::List(
            m.checkouts_of(&a_str(a))?
                .iter()
                .map(checkout_out)
                .collect(),
        ),
        "checkouts_by_project" => {
            let a = list(a);
            let ids: Vec<PyStr> = list(&a[0]).iter().map(a_str).collect();
            let got = m.checkouts_by_project(&ids, arg(&a[1]).as_deref())?;
            PyValue::List(
                got.into_iter()
                    .map(|(k, v)| {
                        PyValue::List(vec![
                            PyValue::Str(k),
                            PyValue::List(v.iter().map(checkout_out).collect()),
                        ])
                    })
                    .collect(),
            )
        }
        "name" => PyValue::Str(m.name(&a_str(a))?),
        "put_project" => {
            let p = rows::project_from(&dec(a)).map_err(Error::Raised)?;
            m.put_project(p)?;
            PyValue::None
        }
        "mint_project" => {
            let g = |k: &str| a.get(k).and_then(arg);
            let flag = |k: &str| a.get(k) == Some(&PyValue::Bool(true));
            let p = m.mint_project(
                &g("name").expect("a name"),
                &g("reason").expect("a reason"),
                g("provisional"),
                g("parent"),
                g("tenant"),
                flag("folder"),
                flag("boundary"),
            )?;
            project_out(&p)
        }
        "put_checkout" => {
            let k = rows::checkout_from(&dec(a), &[]).map_err(Error::Raised)?;
            checkout_out(&m.put_checkout(k)?)
        }
        "put" | "delete" => {
            let a = list(a);
            let kind = name(&a[0]);
            if op == "put" {
                m.put(&kind, &dec(&a[1]))?;
            } else {
                m.delete(&kind, &dec(&a[1]))?;
            }
            PyValue::None
        }
        "event" => {
            let a = list(a);
            let d = dec(&a[1]);
            let PyValue::Dict(items) = &d else {
                panic!("a payload")
            };
            let keys: Vec<String> = items.iter().map(|(k, _)| name(k)).collect();
            let payload = keys
                .iter()
                .map(String::as_str)
                .zip(items.iter().map(|(_, v)| v.clone()))
                .collect();
            m.event(&name(&a[0]), payload)?;
            PyValue::None
        }
        other => panic!("no op {other}"),
    })
}

/// What one step answered, the store calls it made and the rows after it.
pub type Seen = (PyValue, PyValue, PyValue);

/// One step: `{"o", "log", "rows"}`, the inner steps of a call, and
/// whether it raised.
pub fn step(m: &mut Model, st: &PyValue) -> (Seen, Vec<Seen>, bool) {
    let op = name(field(st, "op"));
    let a = st.get("a").unwrap_or(&NONE);
    let start = m.store().0.borrow().log.len();
    let mut inner = Vec::new();
    let mut failed = false;
    let o = if op == "call" {
        let ops = list(field(a, "ops")).to_vec();
        let boom = a.get("raise") == Some(&PyValue::Bool(true));
        let got = m.call(|m| {
            for s in &ops {
                let ((o, log, rows), _, bad) = step(m, s);
                inner.push((o.clone(), log, rows));
                if bad {
                    return Err(back(&o));
                }
            }
            if boom {
                return Err(Error::Raised(bettermemory_core::membership_lines::Raised {
                    class: "_Boom",
                    message: "boom".into(),
                }));
            }
            Ok(())
        });
        match got {
            Ok(()) => PyValue::None,
            Err(e) => raised(&e),
        }
    } else {
        match answer(m, &op, a) {
            Ok(v) => enc(&v),
            Err(e) => {
                failed = true;
                raised(&e)
            }
        }
    };
    let log = enc(&PyValue::List(m.store().0.borrow().log[start..].to_vec()));
    let rows = PyValue::List(
        m.rows()
            .iter()
            .map(|&n| PyValue::int(i128::from(n)))
            .collect(),
    );
    ((o, log, rows), inner, failed)
}

/// The error a spelled raise stands for, to carry it out of a call.
fn back(o: &PyValue) -> Error<PyStr> {
    let r = list(field(o, "raised"));
    let class = name(&r[0]);
    let text = match &r[1] {
        PyValue::Str(t) => t.clone(),
        _ => PyStr::new(),
    };
    if class == "StubError" {
        return Error::Store(text);
    }
    let class: &'static str = Box::leak(class.into_boxed_str());
    Error::Raised(bettermemory_core::membership_lines::Raised {
        class,
        message: txt(&text),
    })
}
