//! Every case of `ntrealpath_python.jsonl` (class ii, Python: `ntpath`'s
//! walk is 3.13.13's): `nt_realpath`, `nt_abspath` and `view_path_text`
//! over the case's table of Windows answers, as the generator's fake `nt`
//! gives them. Pure text: it runs on every platform.

mod common;
mod ntfake;

use bettermemory_core::fsdata::realpath::{nt_abspath, nt_realpath, NtRaised};
use bettermemory_core::location_probe::{view_path_text, Unbound};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use common::jsonl;
use common::parity::Ledger;
use ntfake::Table;
use serde_json::{json, Value};

/// The file spells a backslash as `|` (`gen_ntrealpath`).
fn text(s: &[u32]) -> Value {
    let spelled: String = s.iter().map(|&u| char::from_u32(u).unwrap()).collect();
    json!(spelled.replace('\\', "|"))
}

fn read(s: &str) -> PyStr {
    cps(&s.replace('|', "\\"))
}

fn cwd_of(t: &Value) -> Result<PyStr, Option<i64>> {
    match &t["c"] {
        Value::Null => Ok(cps("C:\\w")),
        Value::String(s) => Ok(read(s)),
        v => Err(v.as_i64()),
    }
}

#[test]
fn ntrealpath_every_case() {
    let name = "ntrealpath_python.jsonl";
    let lines = jsonl(name);
    let mut ledger = Ledger::new("ntrealpath_every_case", name);
    for (i, row) in lines[1..].iter().enumerate() {
        ledger.case(i, || {
            let nt = Table::of(&unpiped(&row["t"]));
            let q = read(row["q"].as_str().unwrap());
            let cwd = cwd_of(&row["t"]);
            let cwd = cwd.as_deref().map_err(|e| *e);
            let real = match nt_realpath(&nt, &q, cwd) {
                Ok(s) => text(&s),
                Err(NtRaised::Os(w)) => json!({"raise": "OSError", "w": w}),
                Err(NtRaised::Value) => json!({"raise": "ValueError"}),
                Err(NtRaised::Unbound) => json!({"raise": "UnboundLocalError"}),
            };
            assert_eq!(real, row["real"], "realpath, case {i}");
            let abs = match nt_abspath(&nt, &q, cwd) {
                Ok(s) => text(&s),
                Err(w) => json!({"raise": "OSError", "w": w}),
            };
            assert_eq!(abs, row["abs"], "abspath, case {i}");
            let view = match view_path_text(&nt, &q, cwd) {
                Ok(s) => text(&s),
                Err(Unbound) => json!({"raise": "UnboundLocalError"}),
            };
            assert_eq!(view, row["view"], "view_path, case {i}");
        });
    }
    ledger.finish(lines[0]["cases"].as_u64().unwrap() as usize);
}

/// The case's table with each `|` read as a backslash.
fn unpiped(v: &Value) -> Value {
    match v {
        Value::String(s) => Value::String(s.replace('|', "\\")),
        Value::Array(a) => Value::Array(a.iter().map(unpiped).collect()),
        Value::Object(o) => Value::Object(
            o.iter()
                .map(|(k, v)| (k.replace('|', "\\"), unpiped(v)))
                .collect(),
        ),
        v => v.clone(),
    }
}
