//! The spellings of `gen_store_rows`: tagged SQLite values, names as text
//! or code points, nested payloads, and the seeded sort sweep.
#![allow(dead_code)]

use bettermemory_core::float_repr;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::SqlValue;
use bettermemory_core::unicode;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// A tagged value as SQLite gives it.
pub fn sql(v: &Value) -> SqlValue {
    if v.is_null() {
        return SqlValue::Null;
    }
    let x = &v[1];
    match v[0].as_str().unwrap() {
        "i" => SqlValue::Integer(x.as_i64().unwrap()),
        "f" => SqlValue::Real(x.as_str().unwrap().parse().unwrap()),
        "s" => SqlValue::Text(x.as_str().unwrap().to_owned()),
        "b" => SqlValue::Blob(super::common::unhex(x.as_str().unwrap())),
        other => panic!("tag {other}"),
    }
}

/// The generator's `tag`.
pub fn tag(v: &SqlValue) -> Value {
    match v {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(i) => json!(["i", i]),
        SqlValue::Real(f) => json!(["f", float_repr::repr(*f)]),
        SqlValue::Text(t) => json!(["s", t]),
        SqlValue::Blob(b) => json!(["b", super::common::hex(b)]),
    }
}

/// The generator's `_text`: a string, or its code points.
pub fn text(v: &Value) -> PyStr {
    match v {
        Value::String(s) => unicode::cps(s),
        _ => super::common::cps_of(v),
    }
}

/// The generator's `_nest`.
pub fn nest(spec: &Value) -> String {
    let (open, n, inner, close) = (
        spec[0].as_str().unwrap(),
        spec[1].as_u64().unwrap() as usize,
        spec[2].as_str().unwrap(),
        spec[3].as_str().unwrap(),
    );
    format!("{{\"x\": {}{inner}{}}}", open.repeat(n), close.repeat(n))
}

/// The generator's `_Lcg`.
pub struct Lcg(pub u64);

impl Lcg {
    pub fn next(&mut self, n: u64) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (self.0 >> 33) % n
    }
}

/// `json.dumps(answer)` of a sort answer, with a line feed.
pub fn answer_line(v: &Value) -> String {
    fn put(v: &Value, out: &mut String) {
        match v {
            Value::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    put(item, out);
                }
                out.push(']');
            }
            Value::Object(map) => {
                out.push('{');
                for (i, (k, item)) in map.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    out.push_str(&serde_json::to_string(k).unwrap());
                    out.push_str(": ");
                    put(item, out);
                }
                out.push('}');
            }
            other => out.push_str(&serde_json::to_string(other).unwrap()),
        }
    }
    let mut out = String::new();
    put(v, &mut out);
    out.push('\n');
    out
}

/// The generator's `_sweep` over its first `palettes` pools: `lists` seeded lists, each answered by
/// `answer` (a line), counted and digested in order.
pub fn sweep(
    rng: &mut Lcg,
    lists: usize,
    palettes: u64,
    values: &[Value],
    mut answer: impl FnMut(&Value, &Value) -> String,
) -> (usize, String) {
    let mut digest = Sha256::new();
    let mut count = 0;
    for _ in 0..lists {
        let n = rng.next(64);
        let palette = rng.next(palettes);
        let pool: Vec<Value> = match palette {
            0 => values[..7].to_vec(),
            1 => vec![
                Value::Null,
                values[7].clone(),
                values[8].clone(),
                values[9].clone(),
            ],
            _ => values.to_vec(),
        };
        let mut rows = Vec::new();
        for _ in 0..n {
            let a = pool[rng.next(pool.len() as u64) as usize].clone();
            let b = pool[rng.next(pool.len() as u64) as usize].clone();
            rows.push(json!({"a": a, "b": b}));
        }
        let by = [
            json!(["a"]),
            json!(["-a"]),
            json!(["a", "-b"]),
            json!(["-b", "a"]),
        ][rng.next(4) as usize]
            .clone();
        digest.update(answer(&Value::Array(rows), &by).as_bytes());
        count += 1;
    }
    let hex: String = digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    (count, hex)
}

/// A dict's keys, as text.
pub fn dict_keys(v: &PyValue) -> Vec<String> {
    let PyValue::Dict(items) = v else {
        panic!("not a dict: {v:?}")
    };
    items
        .iter()
        .map(|(k, _)| match k {
            PyValue::Str(s) => unicode::to_string(s).expect("text"),
            other => panic!("key {other:?}"),
        })
        .collect()
}
