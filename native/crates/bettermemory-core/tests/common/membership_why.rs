//! The spelling of `membership_why.jsonl`
//! (`tests/contract/gen_membership_why.py`): the recording store of
//! `membership_core` (`Fake`) given the membership log of `_WhyStub`,
//! whose rows it keeps as the table `log_rows` of `[seq, kind, payload]`,
//! the steps of a sequence, and the stores of the seeded sweep.

use std::collections::BTreeMap;

use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::log::LogRow;
use bettermemory_core::membership::shared_heal::MembershipLog;
use bettermemory_core::membership::Error;
use bettermemory_core::membership_records::outcomes::Why;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::{self, cps};

use super::membership_core::{model, raised, Fake, Model};
use super::membership_rows::{dec, enc, field, int, list, name, Lcg};

/// The table the fake keeps its log rows in.
pub const LOG_ROWS: &str = "log_rows";

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

/// A log row as the fake keeps it.
pub fn log_row(seq: i64, kind: &str, payload: &str) -> PyValue {
    PyValue::List(vec![PyValue::int(i128::from(seq)), s(kind), s(payload)])
}

fn text(v: &PyValue) -> String {
    match v {
        PyValue::Str(t) => unicode::to_string(t).expect("a text"),
        other => panic!("not a str: {other:?}"),
    }
}

/// `_named_checkout(payload)`: the payload's `checkout` when it is JSON
/// of a dict.
fn names_checkout(payload: &str, checkout: &[u32]) -> bool {
    match json::loads(&cps(payload)) {
        Ok(v @ PyValue::Dict(_)) => {
            matches!(v.get("checkout"), Some(PyValue::Str(c)) if c == checkout)
        }
        _ => false,
    }
}

impl MembershipLog for Fake {
    fn membership_log(
        &self,
        kinds: Option<&[&str]>,
        checkout: Option<&[u32]>,
    ) -> Result<Vec<LogRow>, PyStr> {
        let mut st = self.0.borrow_mut();
        let asked = kinds.map_or(PyValue::None, |ks| {
            PyValue::List(ks.iter().map(|k| s(k)).collect())
        });
        let naming = checkout.map_or(PyValue::None, |c| PyValue::Str(c.to_vec()));
        st.log.push(PyValue::List(vec![s("log"), asked, naming]));
        let n = st.calls.entry("log".to_string()).or_insert(0);
        *n += 1;
        let n = *n;
        if st
            .fail
            .as_ref()
            .is_some_and(|(m, at)| m == "log" && *at == n)
        {
            return Err(cps("injected"));
        }
        let mut out = Vec::new();
        for r in st.tables.get(LOG_ROWS).into_iter().flatten() {
            let r = list(r);
            let (kind, payload) = (text(&r[1]), text(&r[2]));
            if kinds.is_some_and(|ks| !ks.contains(&kind.as_str())) {
                continue;
            }
            if checkout.is_some_and(|c| !names_checkout(&payload, c)) {
                continue;
            }
            out.push(LogRow {
                seq: int(&r[0]) as i64,
                ts: "t".into(),
                session: None,
                kind,
                payload,
                prev_mac: String::new(),
                mac: String::new(),
            });
        }
        Ok(out)
    }
}

/// The model `_model` builds: a fixed UTC time, the default liveness.
pub fn why_model(tables: BTreeMap<String, Vec<PyValue>>, fail: Option<(String, usize)>) -> Model {
    let init = PyValue::Dict(vec![
        (
            s("now"),
            PyValue::List(vec![s("2026-03-04T05:06:07"), PyValue::int(0)]),
        ),
        (s("session"), PyValue::None),
        (s("given"), PyValue::Bool(false)),
    ]);
    model(&init, tables, fail, false)
}

/// The tables a sequence names, a variant per table, with its log.
pub fn tables(header: &PyValue, seq: &PyValue) -> BTreeMap<String, Vec<PyValue>> {
    let all = field(header, "tables");
    let mut out = BTreeMap::new();
    if let PyValue::Dict(spec) = field(seq, "tables") {
        for (t, v) in spec {
            let rows = field(field(all, &name(t)), &name(v));
            out.insert(name(t), list(rows).iter().map(dec).collect());
        }
    }
    out.insert(
        LOG_ROWS.to_string(),
        log_of(header, &name(field(seq, "log"))),
    );
    out
}

/// A named log of the header, an int standing for the base log's row of
/// that sequence number.
pub fn log_of(header: &PyValue, which: &str) -> Vec<PyValue> {
    let logs = field(header, "logs");
    let base = list(field(logs, "base"));
    list(field(logs, which))
        .iter()
        .map(|r| match r {
            PyValue::Int(_) => base
                .iter()
                .find(|b| list(b)[0] == *r)
                .expect("a base row")
                .clone(),
            other => other.clone(),
        })
        .collect()
}

fn opt(v: &Option<PyStr>) -> PyValue {
    v.clone().map_or(PyValue::None, PyValue::Str)
}

/// `_why(w)`: the record, its home, the rows by sequence number, the text.
pub fn why_out(w: &Why) -> PyValue {
    PyValue::Dict(vec![
        (s("record_id"), PyValue::Str(w.record_id.clone())),
        (s("project"), opt(&w.project)),
        (s("checkout"), opt(&w.checkout)),
        (s("rule"), opt(&w.rule)),
        (
            s("rows"),
            PyValue::List(
                w.rows
                    .iter()
                    .map(|r| PyValue::int(i128::from(r.seq)))
                    .collect(),
            ),
        ),
        (s("text"), PyValue::Str(w.text.clone())),
    ])
}

/// One step: what it answered (spelled) and the store calls it made.
pub fn step(m: &mut Model, st: &PyValue) -> (PyValue, PyValue) {
    let op = name(field(st, "op"));
    let a = field(st, "a");
    let start = m.store().0.borrow().log.len();
    let o = match op.as_str() {
        "set" => {
            let a = list(a);
            let rows = list(&dec(&a[1])).to_vec();
            m.store().0.borrow_mut().tables.insert(name(&a[0]), rows);
            PyValue::None
        }
        "why" => {
            let rid = match a {
                PyValue::Str(t) => t.clone(),
                other => panic!("not a record id: {other:?}"),
            };
            match m.why(&rid) {
                Ok(w) => enc(&why_out(&w)),
                Err(e) => raised(&e),
            }
        }
        other => panic!("no op {other}"),
    };
    let log = enc(&PyValue::List(m.store().0.borrow().log[start..].to_vec()));
    (o, log)
}

/// The class of what a call raised.
pub fn class_of(e: &Error<PyStr>) -> String {
    match e {
        Error::Store(_) => "StubError".into(),
        Error::Raised(r) => r.class.to_string(),
    }
}

// ---------------------------------------------------------------------------
// The seeded sweep: `_store` of the generator, its pools read from the row
// ---------------------------------------------------------------------------

/// The pools of the digest row.
pub struct Pools {
    pub names: Vec<String>,
    pub cids: Vec<Option<String>>,
    pub pools: BTreeMap<String, Vec<String>>,
    pub odd: Vec<String>,
    pub kinds: Vec<String>,
    pub keys: Vec<String>,
    pub body_keys: Vec<String>,
    pub evidence: Vec<String>,
}

fn strings(v: &PyValue) -> Vec<String> {
    list(v).iter().map(text).collect()
}

impl Pools {
    pub fn of(row: &PyValue) -> Pools {
        let pools = match field(row, "pools") {
            PyValue::Dict(items) => items.iter().map(|(k, v)| (name(k), strings(v))).collect(),
            other => panic!("not pools: {other:?}"),
        };
        Pools {
            names: strings(field(row, "names")),
            cids: list(field(row, "cids"))
                .iter()
                .map(|c| match c {
                    PyValue::None => None,
                    other => Some(text(other)),
                })
                .collect(),
            pools,
            odd: strings(field(row, "odd")),
            kinds: strings(field(row, "kinds")),
            keys: strings(field(row, "keys")),
            body_keys: strings(field(row, "body_keys")),
            evidence: strings(field(row, "evidence")),
        }
    }

    fn pick<'a, T>(rng: &mut Lcg, from: &'a [T]) -> &'a T {
        &from[rng.next(from.len())]
    }

    fn value(&self, rng: &mut Lcg, key: &str) -> String {
        if rng.next(40) == 0 {
            return Self::pick(rng, &self.odd).clone();
        }
        Self::pick(rng, &self.pools[key]).clone()
    }

    fn body(&self, rng: &mut Lcg) -> String {
        if rng.next(6) == 0 {
            return self.value(rng, "id");
        }
        let mut parts = Vec::new();
        for key in &self.body_keys {
            if rng.next(2) == 0 {
                parts.push(format!("\"{key}\": {}", self.value(rng, key)));
            }
        }
        format!("{{{}}}", parts.join(", "))
    }

    fn payload(&self, rng: &mut Lcg, kind: &str) -> String {
        match rng.next(24) {
            0 => return "{".into(),
            1 => return "[\"K1\"]".into(),
            2 => return "\"K1\"".into(),
            _ => {}
        }
        let mut parts = Vec::new();
        if let Some(table) = kind.strip_suffix("_put") {
            if rng.next(5) != 0 {
                parts.push(format!("\"{table}\": {}", self.body(rng)));
            }
        }
        let retired = kind == "checkout_retired";
        for key in &self.keys {
            if (retired && (key == "checkout" || key == "into")) || rng.next(2) == 0 {
                let pool = if retired && key == "into" {
                    "checkout"
                } else {
                    key
                };
                parts.push(format!("\"{key}\": {}", self.value(rng, pool)));
            }
        }
        format!("{{{}}}", parts.join(", "))
    }

    /// `_store(rng)`: the tables and the log of one store.
    pub fn store(&self, rng: &mut Lcg, asked: &[String]) -> BTreeMap<String, Vec<PyValue>> {
        let n = 1 + rng.next(5);
        let ids: Vec<String> = (1..=n).map(|i| format!("P{i:03}")).collect();
        let mut links: Vec<Option<String>> = vec![None; 5];
        links.push(Some("global".into()));
        links.extend(ids.iter().cloned().map(Some));
        let mut parents = links.clone();
        parents.push(Some("PX".into()));
        let ostr = |v: &Option<String>| v.as_deref().map_or(PyValue::None, s);
        let mut projects = Vec::new();
        for pid in &ids {
            let pname = Self::pick(rng, &self.names).clone();
            let merged = Self::pick(rng, &links).clone();
            let parent = Self::pick(rng, &parents).clone();
            projects.push(project(pid, &pname, &ostr(&merged), &ostr(&parent)));
        }
        let mut homed: Vec<String> = Vec::new();
        for _ in 0..3 {
            homed.extend(ids.iter().cloned());
        }
        homed.push("global".into());
        homed.push("PX".into());
        let mut homes = Vec::new();
        for rid in &asked[..3] {
            if rng.next(4) == 0 {
                continue;
            }
            let project = Self::pick(rng, &homed).clone();
            let cid = Self::pick(rng, &self.cids).clone();
            let rule = format!("r{}", rng.next(3));
            homes.push(home(rid, &project, &ostr(&cid), &rule));
        }
        let mut proposals = Vec::new();
        for i in 0..rng.next(5) {
            let kind = ["first_sight", "import"][rng.next(2)];
            let checkout = Self::pick(rng, &self.cids).clone();
            let evidence =
                json::loads(&cps(Self::pick(rng, &self.evidence).as_str())).expect("JSON");
            proposals.push(question(
                &format!("Q{}", i + 1),
                &ostr(&checkout),
                kind,
                evidence,
            ));
        }
        let mut log = Vec::new();
        for i in 0..rng.next(20) {
            let kind = Self::pick(rng, &self.kinds).clone();
            let payload = self.payload(rng, &kind);
            log.push(log_row(i as i64 + 1, &kind, &payload));
        }
        BTreeMap::from([
            ("projects".to_string(), projects),
            ("homes".to_string(), homes),
            ("proposals".to_string(), proposals),
            (LOG_ROWS.to_string(), log),
        ])
    }
}

fn d(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(items.into_iter().map(|(k, v)| (s(k), v)).collect())
}

/// `_p(pid, name, merged_into=..., parent=...)`.
pub fn project(id: &str, pname: &str, merged_into: &PyValue, parent: &PyValue) -> PyValue {
    d(vec![
        ("id", s(id)),
        ("name", s(pname)),
        ("merged_into", merged_into.clone()),
        ("provisional", PyValue::None),
        ("parent", parent.clone()),
        ("tenant", PyValue::None),
        ("folder", PyValue::int(0)),
        ("boundary", PyValue::int(0)),
        ("minted", s("2026-01-01T00:00:00")),
    ])
}

/// `_h(rid, project, checkout, rule)`.
pub fn home(rid: &str, project: &str, checkout: &PyValue, rule: &str) -> PyValue {
    d(vec![
        ("record_id", s(rid)),
        ("kind", s("memory")),
        ("project", s(project)),
        ("checkout", checkout.clone()),
        ("rule", s(rule)),
        ("decided", s("2026-01-01T00:00:00Z")),
    ])
}

/// `_q(qid, checkout, kind, evidence_json=...)`.
pub fn question(qid: &str, checkout: &PyValue, kind: &str, evidence: PyValue) -> PyValue {
    d(vec![
        ("id", s(qid)),
        ("checkout", checkout.clone()),
        ("kind", s(kind)),
        ("candidates_json", PyValue::List(Vec::new())),
        ("evidence_json", evidence),
        ("reason", s("r")),
        ("line", s("l")),
        ("state", s("open")),
        ("answer", PyValue::None),
        ("answered_by", PyValue::None),
        ("signature_json", PyValue::None),
        ("opened", s("2026-01-01T00:00:00")),
    ])
}

fn t(v: Option<&PyStr>) -> String {
    v.map_or_else(|| "~".into(), |x| unicode::to_string(x).expect("ASCII"))
}

/// `_calls(log)`: the store calls as `json.dumps(enc(log), sort_keys=True,
/// separators=(",", ":"))`.
pub fn calls(log: &[PyValue]) -> String {
    let options = DumpOptions {
        sort_keys: true,
        item_separator: ",",
        key_separator: ":",
        ..DumpOptions::DEFAULT
    };
    let text = json::dumps(&enc(&PyValue::List(log.to_vec())), options).expect("dumps");
    unicode::to_string(&text).expect("ASCII")
}

/// One line of the sweep's digest: the answer, then the store calls.
pub fn digest_line(rid: &str, got: &Result<Why, Error<PyStr>>, log: &[PyValue]) -> String {
    let head = match got {
        Ok(w) => {
            let seqs: Vec<String> = w.rows.iter().map(|r| r.seq.to_string()).collect();
            format!(
                "{rid}|{}|{}|{}|{}|{}",
                t(w.project.as_ref()),
                t(w.checkout.as_ref()),
                t(w.rule.as_ref()),
                seqs.join(","),
                t(Some(&w.text)),
            )
        }
        Err(e) => format!("{rid}|raised|{}", class_of(e)),
    };
    format!("{head}|{}\n", calls(log))
}
