//! What the queue's tests share: `gen_conflicts`' compact value spelling
//! (`_v`, `_dv`), a queue row (`_row`, `_cand`), a memory of the fixture,
//! the recording stub store (`gen_conflicts.Stub`), the `before_stamp`
//! actions and a queue call run as `gen_conflicts._queue_call` runs it.

use std::collections::HashMap;

use bettermemory_core::conflicts::{
    conflicts_pending_count, scan_conflicts, ConflictCandidate, ConflictQueue, ConflictRows,
    ConflictWrites, QueueError, QueueStore, RawRow,
};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::{Confidence, Memory, RecordFields, Source};
use bettermemory_core::store::tables::CONFLICT_COLUMNS;
use bettermemory_core::store::tx::Transaction;
use bettermemory_core::store::SqlValue;
use bettermemory_core::unicode;
use serde_json::{json, Value};

use super::{cps_of, hex, text_field};

pub fn bits(x: f64) -> String {
    format!("{:016x}", x.to_bits())
}

pub fn of_bits(v: &Value) -> f64 {
    f64::from_bits(u64::from_str_radix(v.as_str().expect("bits"), 16).expect("hex"))
}

/// `_t`: a text, or its code points where it holds a surrogate.
pub fn tv(t: &[u32]) -> Value {
    match unicode::to_string(t) {
        Some(s) => json!(s),
        None => json!({ "cps": t }),
    }
}

fn opt(t: &Option<PyStr>) -> Value {
    t.as_ref().map_or(Value::Null, |t| tv(t))
}

fn column_of(k: &PyValue) -> Option<usize> {
    match k {
        PyValue::Str(s) => CONFLICT_COLUMNS.iter().position(|c| unicode::cps(c) == *s),
        _ => None,
    }
}

/// `_v`.
pub fn enc(v: &PyValue) -> Value {
    match v {
        PyValue::None => Value::Null,
        PyValue::Bool(b) => json!(b),
        PyValue::Int(t) => json!({ "i": t }),
        PyValue::Float(f) => json!({ "f": bits(*f) }),
        PyValue::Str(s) => tv(s),
        PyValue::List(items) => Value::Array(items.iter().map(enc).collect()),
        PyValue::Tuple(items) => json!({ "tu": items.iter().map(enc).collect::<Vec<_>>() }),
        PyValue::Dict(items) => {
            let at: Vec<Option<usize>> = items.iter().map(|(k, _)| column_of(k)).collect();
            let ordered = at.windows(2).all(|w| w[0] <= w[1]);
            if !items.is_empty() && at.iter().all(Option::is_some) && ordered {
                let last = at.last().copied().flatten().expect("a column");
                let cells: Vec<Value> = (0..=last)
                    .map(|i| match at.iter().position(|a| *a == Some(i)) {
                        Some(j) => enc(&items[j].1),
                        None => json!({}),
                    })
                    .collect();
                return json!({ "c": cells });
            }
            json!({ "d": items.iter().map(|(k, v)| json!([enc(k), enc(v)])).collect::<Vec<_>>() })
        }
        other => panic!("no compact spelling for {other:?}"),
    }
}

/// `_dv`: a dict keeps a repeated key's last value in its first place.
pub fn dec(j: &Value) -> PyValue {
    match j {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::String(s) => PyValue::str(s),
        Value::Array(items) => PyValue::List(items.iter().map(dec).collect()),
        Value::Object(map) => {
            let (tag, inner) = map.iter().next().expect("a tag");
            match tag.as_str() {
                "i" => PyValue::int_from_decimal(inner.as_str().expect("digits")).expect("an int"),
                "f" => PyValue::Float(of_bits(inner)),
                "cps" => PyValue::Str(cps_of(inner)),
                "tu" => PyValue::Tuple(inner.as_array().expect("items").iter().map(dec).collect()),
                "d" => {
                    let mut out: Vec<(PyValue, PyValue)> = Vec::new();
                    for pair in inner.as_array().expect("pairs") {
                        let (k, v) = (dec(&pair[0]), dec(&pair[1]));
                        match out.iter_mut().find(|(ok, _)| *ok == k) {
                            Some(slot) => slot.1 = v,
                            None => out.push((k, v)),
                        }
                    }
                    PyValue::Dict(out)
                }
                "c" => PyValue::Dict(
                    CONFLICT_COLUMNS
                        .iter()
                        .zip(inner.as_array().expect("cells"))
                        .filter(|(_, v)| !matches!(v, Value::Object(m) if m.is_empty()))
                        .map(|(c, v)| (PyValue::str(c), dec(v)))
                        .collect(),
                ),
                other => panic!("unknown tag {other}"),
            }
        }
        Value::Number(_) => panic!("a bare number in a compact value"),
    }
}

/// `_row`.
pub fn row(c: &ConflictCandidate) -> Value {
    json!([
        tv(&c.id),
        tv(&c.a_id),
        tv(&c.b_id),
        tv(&c.summary_a),
        tv(&c.summary_b),
        bits(c.similarity),
        tv(&c.method),
        tv(&c.detector),
        tv(&c.created),
        tv(&c.status),
        opt(&c.verdict_ts),
        opt(&c.note),
        opt(&c.verdict_hash_a),
        opt(&c.verdict_hash_b)
    ])
}

fn text(v: &Value) -> PyStr {
    match &dec(v) {
        PyValue::Str(s) => s.clone(),
        other => panic!("not a text: {other:?}"),
    }
}

fn opt_text(v: &Value) -> Option<PyStr> {
    (!v.is_null()).then(|| text(v))
}

/// `_cand`.
pub fn cand(r: &Value) -> ConflictCandidate {
    ConflictCandidate {
        id: text(&r[0]),
        a_id: text(&r[1]),
        b_id: text(&r[2]),
        summary_a: text(&r[3]),
        summary_b: text(&r[4]),
        similarity: of_bits(&r[5]),
        method: text(&r[6]),
        detector: text(&r[7]),
        created: text(&r[8]),
        status: text(&r[9]),
        verdict_ts: opt_text(&r[10]),
        note: opt_text(&r[11]),
        verdict_hash_a: opt_text(&r[12]),
        verdict_hash_b: opt_text(&r[13]),
    }
}

pub fn dt(text: &str) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(text)).expect("an isoformat")
}

/// A memory the scan reads (`gen_conflicts._memory`).
pub fn memory(id: PyStr, body: PyStr, updated: &str) -> Memory {
    Memory {
        fields: RecordFields {
            id,
            created: dt("2024-03-01T12:00:00+00:00"),
            updated: dt(updated),
            scopes: vec![unicode::cps("global")],
            confidence: Confidence::MEMBERS[0].1,
            source: Source::MEMBERS[0].1,
            body,
            origin: None,
            actor: None,
            last_verified_at: None,
            category: None,
            verified_paths: Vec::new(),
            verified_commits: Vec::new(),
            verified_versions: Vec::new(),
            verified_absent_paths: Vec::new(),
            verified_path_targets: None,
            verified_absent_path_targets: None,
            claims: Vec::new(),
            verified_head: None,
        },
        links: Vec::new(),
        corroborations: "0".to_owned(),
        last_corroborated: None,
    }
}

/// A case's memory `{"i", "b", "u"}` (`u` default the header's).
pub fn memory_of(m: &Value, u: &str) -> Memory {
    memory(
        text_field(m, "i"),
        text_field(m, "b"),
        m["u"].as_str().unwrap_or(u),
    )
}

pub fn memories(ms: &Value, u: &str) -> Vec<Memory> {
    ms.as_array()
        .expect("memories")
        .iter()
        .map(|m| memory_of(m, u))
        .collect()
}

/// A cell of `list_conflicts` (`gen_conflicts._cell`).
pub fn cell(v: &SqlValue) -> Value {
    match v {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(n) => json!({ "i": n.to_string() }),
        SqlValue::Real(f) => json!({ "f": bits(*f) }),
        SqlValue::Text(t) => json!(t),
        SqlValue::Blob(b) => json!({ "b": hex(b) }),
    }
}

/// `_outcome`: the class, and the text where it is the product's own.
pub fn outcome(e: &QueueError) -> Value {
    let class = e.class();
    let mut out = json!({ "err": class });
    if matches!(class.as_str(), "ValueError" | "NotFoundError") {
        out["text"] = tv(&e.text().expect("a worded refusal"));
    }
    out
}

fn get<'r>(row: &'r RawRow, key: &str) -> Option<&'r PyValue> {
    let key = PyValue::str(key);
    row.iter().find(|(k, _)| *k == key).map(|(_, v)| v)
}

/// `gen_conflicts.Stub`.
#[derive(Debug, Default)]
pub struct Stub {
    pub table: Vec<RawRow>,
    pub counts: Vec<i64>,
    /// The op, the count of its calls at which it raises, the class.
    pub fail: Option<(String, usize, String)>,
    pub n: HashMap<String, usize>,
    pub calls: Vec<Value>,
}

impl Stub {
    pub fn of(spec: &Value) -> Stub {
        let rows = spec["rows"].as_array().expect("rows");
        Stub {
            table: rows
                .iter()
                .map(|r| match &dec(r) {
                    PyValue::Dict(items) => items.clone(),
                    other => panic!("a row that is no dict: {other:?}"),
                })
                .collect(),
            counts: spec["counts"]
                .as_array()
                .expect("counts")
                .iter()
                .map(|c| c.as_i64().expect("a count"))
                .collect(),
            fail: spec.get("fail").map(|f| {
                (
                    f["op"].as_str().expect("op").to_owned(),
                    f["at"].as_u64().expect("at") as usize,
                    f["class"].as_str().expect("class").to_owned(),
                )
            }),
            n: HashMap::new(),
            calls: Vec::new(),
        }
    }

    pub fn table_value(&self) -> Value {
        Value::Array(
            self.table
                .iter()
                .map(|r| enc(&PyValue::Dict(r.clone())))
                .collect(),
        )
    }

    fn call(&mut self, op: &str, args: Vec<Value>) -> Result<(), QueueError> {
        let mut entry = vec![json!(op)];
        entry.extend(args);
        self.calls.push(Value::Array(entry));
        let n = self.n.entry(op.to_owned()).or_insert(0);
        *n += 1;
        match &self.fail {
            Some((fop, at, class)) if fop == op && at == n => {
                Err(QueueError::Raised(class.clone()))
            }
            _ => Ok(()),
        }
    }
}

impl ConflictRows for Stub {
    fn list_conflicts(&mut self) -> Result<Vec<RawRow>, QueueError> {
        self.call("list", Vec::new())?;
        Ok(self.table.clone())
    }
    fn count_memories(&mut self) -> Result<i64, QueueError> {
        self.call("count", Vec::new())?;
        let i = self.n["count"].min(self.counts.len()) - 1;
        Ok(self.counts[i])
    }
}

impl ConflictWrites for Stub {
    fn put_conflict(&mut self, record: &PyValue, session: Option<&str>) -> Result<(), QueueError> {
        self.call("put", vec![enc(record), json!(session)])?;
        let PyValue::Dict(items) = record else {
            panic!("a record that is no dict")
        };
        let id = get(items, "id").expect("an id").clone();
        match self.table.iter_mut().find(|r| get(r, "id") == Some(&id)) {
            Some(slot) => *slot = items.clone(),
            None => self.table.push(items.clone()),
        }
        Ok(())
    }
    fn delete_conflict(
        &mut self,
        conflict_id: &[u32],
        session: Option<&str>,
    ) -> Result<(), QueueError> {
        self.call("delete", vec![tv(conflict_id), json!(session)])?;
        let id = PyValue::Str(conflict_id.to_vec());
        self.table.retain(|r| get(r, "id") != Some(&id));
        Ok(())
    }
}

impl QueueStore for Stub {
    type Batch<'t> = Stub;
    fn batch<R>(
        &mut self,
        body: impl FnOnce(&mut Stub) -> Result<R, QueueError>,
    ) -> Result<R, QueueError> {
        self.calls.push(json!(["batch"]));
        let saved = self.table.clone();
        match body(self) {
            Ok(v) => {
                self.calls.push(json!(["commit"]));
                Ok(v)
            }
            Err(e) => {
                self.table = saved;
                self.calls.push(json!(["rollback", e.class()]));
                Err(e)
            }
        }
    }
}

/// What a `before_stamp` action does on the batch it runs on.
pub trait Stamps: ConflictWrites {
    fn stamp(&mut self, action: &str, header: &Value) -> Result<(), QueueError>;
}

fn stamp_record(header: &Value) -> PyValue {
    dec(&header["stamp_record"])
}

impl Stamps for Stub {
    fn stamp(&mut self, action: &str, header: &Value) -> Result<(), QueueError> {
        match action {
            "log" => {
                self.calls.push(json!(["stamp"]));
                Ok(())
            }
            "put" => self.put_conflict(&stamp_record(header), Some("s-stamp")),
            _ => Err(QueueError::Raised("RuntimeError".to_owned())),
        }
    }
}

impl Stamps for Transaction<'_> {
    fn stamp(&mut self, action: &str, header: &Value) -> Result<(), QueueError> {
        match action {
            "put" => ConflictWrites::put_conflict(self, &stamp_record(header), Some("s-stamp")),
            "mem" => {
                let payload = super::rows::py(&header["stamp_memory"]);
                self.scope(|tx| tx.mutate("memory_put", &payload, Some("s-stamp")))?;
                Ok(())
            }
            _ => Err(QueueError::Raised("RuntimeError".to_owned())),
        }
    }
}

fn counters(c: &bettermemory_core::conflicts::Upserted) -> Value {
    Value::Array(c.items().iter().map(|(k, n)| json!([k, n])).collect())
}

/// `gen_conflicts._queue_call`: what the case's call answers. `path` is
/// `str(store.path)`; the warnings go to `warns`.
pub fn queue_call<S>(
    store: &mut S,
    c: &Value,
    header: &Value,
    path: &str,
    warns: &mut Vec<String>,
) -> Result<Value, QueueError>
where
    S: QueueStore,
    for<'t> S::Batch<'t>: Stamps,
{
    let now = dt(header["now"].as_str().expect("now"));
    let u = header["u"].as_str().expect("u");
    let session = c.get("session").and_then(Value::as_str);
    let mut warn = |w: &str| warns.push(w.to_owned());
    match c["q"].as_str().expect("q") {
        "load" => Ok(Value::Array(
            ConflictQueue::new(store).load()?.iter().map(row).collect(),
        )),
        "pending" => Ok(Value::Array(
            ConflictQueue::new(store)
                .pending()?
                .iter()
                .map(row)
                .collect(),
        )),
        "count" => Ok(json!(conflicts_pending_count(store, path, &mut warn))),
        "file" => {
            let status = ConflictQueue::new(store).file_pair(&cand(&c["cand"]), session)?;
            Ok(tv(&status))
        }
        "upsert" => {
            let fresh: Vec<ConflictCandidate> = c["fresh"]
                .as_array()
                .expect("fresh")
                .iter()
                .map(cand)
                .collect();
            let ms = memories(&c["m"], u);
            let out = ConflictQueue::new(store).upsert_scan(&fresh, &ms, session, &mut warn)?;
            Ok(counters(&out))
        }
        "scan" => {
            let th = (!c["th"].is_null()).then(|| of_bits(&c["th"]));
            let ms = memories(&c["m"], u);
            Ok(counters(&scan_conflicts(
                store, &ms, th, session, &now, &mut warn,
            )?))
        }
        "resolve" => {
            let bodies: HashMap<PyStr, PyStr> = match c["bodies"].as_array() {
                None => HashMap::new(),
                Some(pairs) => pairs.iter().map(|p| (text(&p[0]), text(&p[1]))).collect(),
            };
            let actions: Vec<String> = c
                .get("stamp")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .map(|s| s.as_str().expect("action").to_owned())
                        .collect()
                })
                .unwrap_or_default();
            let run = move |tx: &mut S::Batch<'_>| -> Result<(), QueueError> {
                for a in &actions {
                    tx.stamp(a, header)?;
                }
                Ok(())
            };
            let stamp = if c["stamp"].as_array().is_some_and(|a| !a.is_empty()) {
                Some(run)
            } else {
                None
            };
            let note = opt_text(&c["note"]);
            let hit = ConflictQueue::new(store).resolve(
                &text(&c["id"]),
                &text(&c["status"]),
                note.as_deref(),
                &bodies,
                stamp,
                session,
                &now,
            )?;
            Ok(hit.as_ref().map_or(Value::Null, row))
        }
        other => panic!("unknown queue call {other}"),
    }
}
