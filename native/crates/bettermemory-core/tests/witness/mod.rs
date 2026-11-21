//! The rows of `witness_questions.jsonl` read into the port's values, and
//! the port's answers spelled as the generator spells the reference's.

use bettermemory_core::capture::location::{to_location, ObjectKey};
use bettermemory_core::capture::{Capture, Kind, Stamp, State};
use bettermemory_core::checkout_token::Lineage;
use bettermemory_core::json::{dumps as json_dumps, loads, DumpOptions};
use bettermemory_core::membership_records::records::{Checkout, View};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use bettermemory_core::witness_questions::AskedView;
use serde_json::Value;

pub fn cps(text: &str) -> PyStr {
    unicode::cps(text)
}

/// `json.loads` of a row's JSON text.
pub fn load(text: &Value) -> PyValue {
    loads(&cps(text.as_str().expect("JSON text"))).expect("the text loads")
}

/// `json.dumps(value)` with its defaults.
pub fn dumps(value: &PyValue) -> String {
    let out = json_dumps(value, DumpOptions::DEFAULT).expect("dumps");
    unicode::to_string(&out).expect("ASCII")
}

pub fn text(s: &[u32]) -> Value {
    Value::String(unicode::to_string(s).expect("UTF-8"))
}

pub fn opt_text(s: &Option<PyStr>) -> Value {
    s.as_ref().map_or(Value::Null, |s| text(s))
}

fn opt(v: &Value) -> Option<PyStr> {
    v.as_str().map(cps)
}

pub fn key(v: &Value) -> Option<ObjectKey> {
    let a = v.as_array()?;
    Some(ObjectKey {
        device: cps(a[0].as_str().unwrap()),
        inode: cps(a[1].as_str().unwrap()),
        birth: opt(&a[2]),
    })
}

pub fn checkout(k: &Value) -> Checkout {
    let stamps = k["stamps"].as_array().map_or(Vec::new(), |all| {
        all.iter()
            .map(|s| Stamp {
                name: cps(s[0].as_str().unwrap()),
                numbers: [1, 2, 3, 4].map(|i| s[i].to_string()),
            })
            .collect()
    });
    Checkout {
        id: cps("c1"),
        project: cps("p1"),
        machine: cps("m1"),
        kind: cps(k["kind"].as_str().unwrap_or("primary")),
        object_key: key(&k["key"]),
        common_object_key: key(&k["common"]),
        git_dir: Some(cps("/r/.git")),
        token: opt(&k["token"]),
        token_status: cps(k["status"].as_str().unwrap_or("present")),
        stamps,
        last_head: opt(&k["last_head"]),
        branch: None,
        remotes: Vec::new(),
        manifest: None,
        legacy: false,
        retired: false,
        registered: cps("2026-01-01T00:00:00+00:00"),
        views: Vec::new(),
        sessions: Vec::new(),
        replaced_by: None,
        ticks: None,
    }
}

pub fn view(path: &str) -> View {
    View {
        checkout: cps("c1"),
        machine: cps("m1"),
        path: cps(path),
        first_seen: cps("x"),
        last_seen: cps("x"),
        order: 0,
    }
}

fn kind(v: &Value) -> Option<Kind> {
    let name = v.as_str()?;
    Kind::ALL.into_iter().find(|k| k.as_str() == name)
}

pub fn capture(c: &Value) -> Capture {
    let mut cap = Capture::new(cps("/r"), State::Repo);
    cap.kind = kind(&c["kind"]);
    cap.git_dir = opt(&c["git_dir"]);
    cap.object_key = key(&c["key"]).map(|k| (k.device, k.inode, k.birth));
    cap.token = opt(&c["token"]);
    cap.token_written = opt(&c["written"]);
    cap
}

/// The location a `located` row names.
pub fn location(row: &Value) -> bettermemory_core::capture::location::Location {
    let mut cap = Capture::new(cps("/r"), State::Repo);
    cap.kind = Some(Kind::Primary);
    cap.object_key = key(&row["key"]).map(|k| (k.device, k.inode, k.birth));
    cap.token = opt(&row["token"]);
    cap.token_written = opt(&row["written"]);
    cap.token_status = cps("absent");
    to_location(&cap, &cps(row["machine"].as_str().unwrap()), None)
}

/// A re-mint call as the stub records it.
pub fn call(git_dir: &[u32], policy: bool, bare: bool, r: Option<Lineage>) -> Value {
    let r = r.map_or(Value::Null, |l| {
        Value::Array(vec![text(&l.token), text(&l.device), text(&l.inode)])
    });
    Value::Array(vec![
        text(git_dir),
        Value::Bool(policy),
        Value::Bool(bare),
        r,
    ])
}

fn key_value(k: &Option<(PyStr, PyStr, Option<PyStr>)>) -> PyValue {
    k.as_ref().map_or(PyValue::None, |(d, i, b)| {
        PyValue::List(vec![
            PyValue::Str(d.clone()),
            PyValue::Str(i.clone()),
            b.as_ref()
                .map_or(PyValue::None, |b| PyValue::Str(b.clone())),
        ])
    })
}

fn opt_value(v: &Option<PyStr>) -> PyValue {
    v.as_ref()
        .map_or(PyValue::None, |s| PyValue::Str(s.clone()))
}

/// `json.dumps(asked_views(...))`: each view as `[path, fields]`.
pub fn asked_text(views: &[(PyStr, AskedView)]) -> String {
    let field = |k: &str, v: PyValue| (PyValue::str(k), v);
    let list = views
        .iter()
        .map(|(path, v)| {
            let stamps = v
                .stamps
                .iter()
                .map(|s| {
                    let mut row = vec![PyValue::Str(s.name.clone())];
                    row.extend(s.numbers.iter().map(|n| PyValue::Int(n.clone())));
                    PyValue::List(row)
                })
                .collect();
            PyValue::List(vec![
                PyValue::Str(path.clone()),
                PyValue::Dict(vec![
                    field("kind", PyValue::Str(v.kind.clone())),
                    field("key", key_value(&v.key)),
                    field("token", opt_value(&v.token)),
                    field("last_head", opt_value(&v.last_head)),
                    field("stamps", PyValue::List(stamps)),
                    field("common_key", key_value(&v.common_key)),
                    field("superseded", PyValue::Bool(v.superseded)),
                ]),
            ])
        })
        .collect();
    dumps(&PyValue::List(list))
}
