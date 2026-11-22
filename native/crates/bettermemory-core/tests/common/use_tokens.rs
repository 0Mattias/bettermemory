//! The runner of `use_tokens.jsonl`'s cases (`gen_use_tokens.run`): the
//! scripted clock and counter, the stub recorder, each step's answer, and
//! the digests' random inputs.

use std::collections::BTreeSet;

use bettermemory_core::float_repr;
use bettermemory_core::pyvalue::{repr, PyStr, PyValue};
use bettermemory_core::session::{PendingUseToken, SessionState};
use bettermemory_core::use_tokens::{self, Row, UseLog, UseTokenError};
use serde_json::{json, Map, Value};

use super::session::{cps, float, snap, Fx};

/// The stub recorder of `gen_use_tokens._Rec`.
pub struct Rec {
    pub sid: PyStr,
    pub rows: Vec<Row>,
    pub w: Vec<Value>,
    pub q: u64,
    pub it: u64,
    pub pl: u64,
}

impl Rec {
    pub fn new(sid: &str) -> Rec {
        Rec {
            sid: cps(sid),
            rows: Vec::new(),
            w: Vec::new(),
            q: 0,
            it: 0,
            pl: 0,
        }
    }
}

fn text(p: &[u32]) -> String {
    p.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

impl UseLog for Rec {
    fn record(&mut self, kind: &str, fields: Row) {
        let r = repr(&PyValue::Dict(fields)).unwrap();
        self.w.push(json!([kind, text(&r)]));
    }

    fn session_for_request(&mut self) -> PyStr {
        self.q += 1;
        self.sid.clone()
    }

    fn events_backward(&mut self) -> Box<dyn Iterator<Item = Row> + '_> {
        self.it += 1;
        let pl = &mut self.pl;
        Box::new(self.rows.iter().map(move |r| {
            *pl += 1;
            r.clone()
        }))
    }
}

/// A JSON value as Python's `json.loads` gives it.
pub fn pyv(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) => match (n.as_i64(), n.as_u64()) {
            (Some(i), _) => PyValue::int(i128::from(i)),
            (_, Some(u)) => PyValue::int(i128::from(u)),
            _ => PyValue::Float(n.as_f64().unwrap()),
        },
        Value::String(s) => PyValue::str(s),
        Value::Array(a) => PyValue::List(a.iter().map(pyv).collect()),
        Value::Object(m) => row(m),
    }
}

fn row(m: &Map<String, Value>) -> PyValue {
    PyValue::Dict(m.iter().map(|(k, v)| (PyValue::str(k), pyv(v))).collect())
}

fn dict_items(v: &Value) -> Row {
    v.as_object()
        .expect("a row")
        .iter()
        .map(|(k, v)| (PyValue::str(k), pyv(v)))
        .collect()
}

/// A hit from its `[key, value]` pairs, in order.
pub fn hit(v: &Value) -> Row {
    v.as_array()
        .unwrap()
        .iter()
        .map(|p| (PyValue::str(p[0].as_str().unwrap()), pyv(&p[1])))
        .collect()
}

fn ids(v: &Value) -> Option<BTreeSet<PyStr>> {
    v.as_array()
        .map(|a| a.iter().map(|x| cps(x.as_str().unwrap())).collect())
}

fn tok_out(t: &PendingUseToken) -> Value {
    json!([
        text(&t.memory_id),
        t.token,
        float_repr::repr(t.issued_at),
        t.issued_at_turn
    ])
}

/// The exception as the file writes it: a `TypeError`'s text only in the
/// Python-gated file.
fn err(e: &UseTokenError, python: bool) -> Value {
    let text = match e {
        UseTokenError::Unhashable(_) if !python => Value::Null,
        _ => json!(e.message()),
    };
    json!({"err": [e.name(), text]})
}

fn sorted(s: &BTreeSet<PyStr>) -> Value {
    json!(s.iter().map(|x| text(x)).collect::<Vec<_>>())
}

/// Each step's `{"r", "n", "k", "w", "q", "it", "pl", "st"}`; `python`
/// for `use_tokens_python.jsonl`.
pub fn run(case: &Value, python: bool) -> Vec<Value> {
    let mut fx = Fx {
        now: 0.0,
        n: case["seed"].as_u64().unwrap(),
        reads: 0,
        draws: 0,
    };
    let mut rec = Rec::new(case["sid"].as_str().unwrap());
    let st = SessionState::new(&mut fx);
    let mut out = Vec::new();
    for step in case["s"].as_array().unwrap() {
        if let Some(t) = step.get("t") {
            fx.now = float(t);
        }
        if let Some(log) = step.get("log") {
            rec.rows = log.as_array().unwrap().iter().map(dict_items).collect();
        }
        fx.reads = 0;
        fx.draws = 0;
        rec.w.clear();
        (rec.q, rec.it, rec.pl) = (0, 0, 0);
        let do_ = step["do"].as_array().unwrap();
        let a = &do_[1..];
        let r: Result<Value, UseTokenError> = match do_[0].as_str().unwrap() {
            "iss" => {
                let ids: Vec<PyStr> = a[0]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|x| cps(x.as_str().unwrap()))
                    .collect();
                let got = st.lock().issue_use_tokens(&ids, &mut fx);
                Ok(json!(got
                    .iter()
                    .map(|(_, t)| t.clone())
                    .collect::<Vec<_>>()))
            }
            "adv" => use_tokens::advance_turn(&st, &mut rec, ids(&a[0]).as_ref(), &mut fx)
                .map(|()| Value::Null),
            "drain" => {
                let mut g = st.lock();
                use_tokens::drain_expired_use_tokens(&mut g, &mut rec, ids(&a[0]).as_ref())
                    .map(|got| json!(got.iter().map(tok_out).collect::<Vec<_>>()))
            }
            "emit" => {
                let toks: Vec<PendingUseToken> = a[0]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|t| PendingUseToken {
                        token: t[1].as_str().unwrap().to_owned(),
                        memory_id: cps(t[0].as_str().unwrap()),
                        issued_at: float(&t[2]),
                        issued_at_turn: t[3].as_i64().unwrap(),
                    })
                    .collect();
                let g = st.lock();
                use_tokens::emit_expired_use_tokens(&g, &mut rec, &toks, &mut fx)
                    .map(|()| Value::Null)
            }
            op @ ("arp" | "hap") => {
                let xs = ids(&a[0]);
                let xp: Option<Vec<(PyStr, f64)>> = a[1].as_array().map(|v| {
                    v.iter()
                        .map(|p| (cps(p[0].as_str().unwrap()), float(&p[1])))
                        .collect()
                });
                let f = if op == "arp" {
                    use_tokens::already_recorded_pending_ids
                } else {
                    use_tokens::hook_attributed_pending_ids
                };
                f(&st, &mut rec, xs.as_ref(), xp.as_deref()).map(|s| sorted(&s))
            }
            "att" => {
                let mut hits: Vec<Row> = a[0].as_array().unwrap().iter().map(hit).collect();
                use_tokens::attach_use_tokens(&mut hits, &st, &mut fx).map(|()| {
                    let list = PyValue::List(hits.into_iter().map(PyValue::Dict).collect());
                    json!(text(&repr(&list).unwrap()))
                })
            }
            "ets" => {
                let raw = pyv(&a[0]);
                Ok(json!(match use_tokens::event_ts_epoch(Some(&raw)) {
                    Some(f) => float_repr::repr(f),
                    None => "None".to_owned(),
                }))
            }
            "turn" => {
                st.lock().turn_counter = a[0].as_i64().unwrap();
                Ok(Value::Null)
            }
            other => panic!("op {other}"),
        };
        let mut m = Map::new();
        m.insert("r".into(), r.unwrap_or_else(|e| err(&e, python)));
        m.insert("n".into(), json!(fx.reads));
        m.insert("k".into(), json!(fx.draws));
        m.insert("w".into(), Value::Array(rec.w.clone()));
        m.insert("q".into(), json!(rec.q));
        m.insert("it".into(), json!(rec.it));
        m.insert("pl".into(), json!(rec.pl));
        m.insert("st".into(), snap(&st.lock()));
        out.push(Value::Object(m));
    }
    out
}

const IDS: [&str; 4] = ["m1", "m2", "m3", "m4"];
const DELTAS: [f64; 5] = [0.5, 1.0, 600.0, 1800.0, 1801.0];

fn random_row(r: &mut dyn FnMut(u64) -> u64) -> Value {
    let kinds = ["use", "use", "use", "search"];
    let ts = [
        json!("1970-01-01T00:16:40Z"),
        json!("1970-01-01T00:16:41Z"),
        json!("1970-01-01T00:26:40Z"),
        json!("1970-01-01T00:46:41Z"),
        json!("1970-01-01T01:46:40+01:00"),
        json!("bad"),
        Value::Null,
        json!(5),
    ];
    let sessions = [
        json!("S"),
        json!("S"),
        json!("T"),
        json!(""),
        Value::Null,
        json!(["x"]),
        json!({"a": 1}),
    ];
    let mut row = Map::new();
    row.insert("kind".into(), json!(kinds[r(4) as usize]));
    let t = ts[r(8) as usize].clone();
    if !t.is_null() {
        row.insert("ts".into(), t);
    }
    let s = sessions[r(7) as usize].clone();
    if !s.is_null() {
        row.insert("session".into(), s);
    }
    if r(2) != 0 {
        row.insert("triggered_from".into(), json!("stop_hook"));
    }
    match r(6) {
        0 => {
            let n = r(3) + 1;
            let v: Vec<&str> = (0..n).map(|_| IDS[r(4) as usize]).collect();
            row.insert("ids".into(), json!(v));
        }
        1 => {
            row.insert("ids".into(), json!([]));
        }
        2 => {
            row.insert("ids".into(), json!(IDS[r(4) as usize]));
        }
        3 => {
            row.insert("memory_ids".into(), json!([IDS[r(4) as usize]]));
        }
        4 => {
            row.insert("ids".into(), json!([1, IDS[r(4) as usize]]));
        }
        _ => {}
    }
    Value::Object(row)
}

/// `gen_use_tokens._random_case`.
pub fn random_case(next: &mut dyn FnMut() -> u64) -> Value {
    let mut r = |n: u64| next() % n;
    let mut case = json!({"seed": r(1 << 32), "sid": "S"});
    let mut steps = Vec::new();
    let mut t = 1000.0f64;
    for _ in 0..16 {
        let mut step = Map::new();
        if r(3) == 0 {
            t += DELTAS[r(5) as usize];
            step.insert("t".into(), json!(float_repr::repr(t)));
        }
        if r(4) == 0 {
            let n = r(5);
            let log: Vec<Value> = (0..n).map(|_| random_row(&mut r)).collect();
            step.insert("log".into(), json!(log));
        }
        let do_ = match r(8) {
            0 | 1 => {
                if r(2) != 0 {
                    json!(["adv", null])
                } else {
                    json!(["adv", [IDS[r(4) as usize]]])
                }
            }
            2 => {
                let n = r(3) + 1;
                let v: Vec<&str> = (0..n).map(|_| IDS[r(4) as usize]).collect();
                json!(["iss", v])
            }
            3 => json!(["drain", null]),
            4 => json!(["arp", null, null]),
            5 => json!(["turn", r(5)]),
            6 => {
                let n = r(3);
                let v: Vec<Value> = (0..n)
                    .map(|_| json!([["id", IDS[r(4) as usize]]]))
                    .collect();
                json!(["att", v])
            }
            _ => json!(["adv", null]),
        };
        step.insert("do".into(), do_);
        steps.push(Value::Object(step));
    }
    case["s"] = json!(steps);
    case
}
