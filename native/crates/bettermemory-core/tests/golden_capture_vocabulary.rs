//! Every case of `capture_vocabulary.jsonl` (not gated) and
//! `capture_vocabulary_python.jsonl` (the RecursionError cases, gated on
//! the Python release, whose interpreter and stack decide them): a
//! capture's token read and written back, hostile tokens, `to_location`,
//! `remote_key`, `_paths` and `ObjectKey`, against the native `capture`
//! module. The recipes and the answer's spelling are the generator's
//! (`tests/contract/gen_capture_vocabulary.py`).

mod common;

use bettermemory_core::capture::location::{to_location, ObjectKey};
use bettermemory_core::capture::paths;
use bettermemory_core::capture::remote::{remote_key, RemoteKey};
use bettermemory_core::capture::{Capture, Key, TOKEN_KEYS};
use bettermemory_core::float_repr;
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use common::parity::Ledger;
use sha2::{Digest, Sha256};

fn lines(name: &str) -> Vec<PyValue> {
    let text = std::fs::read_to_string(common::golden_path(name))
        .unwrap_or_else(|e| panic!("cannot read golden file {name}: {e}"));
    text.lines()
        .map(|l| json::loads(&unicode::cps(l)).expect("a JSON line"))
        .collect()
}

fn field<'a>(v: &'a PyValue, k: &str) -> &'a PyValue {
    v.get(k).unwrap_or_else(|| panic!("no field {k}"))
}

fn s(v: &PyValue) -> PyStr {
    match v {
        PyValue::Str(t) => t.clone(),
        other => panic!("not a str: {other:?}"),
    }
}

fn opt_s(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::None => None,
        other => Some(s(other)),
    }
}

fn n(v: &PyValue) -> usize {
    match v {
        PyValue::Int(t) => t.parse().expect("a count"),
        other => panic!("not an int: {other:?}"),
    }
}

fn shown(v: &PyValue) -> String {
    json::utf8_text(&json::dumps(v, DumpOptions::SORTED_ASCII_COMPACT).expect("dumps"))
}

/// The generator's `built`: `$n` nesting, `$r` text, `$x` list recipes.
fn built(spec: &PyValue) -> PyValue {
    match spec {
        PyValue::Dict(items) if items.len() == 1 => {
            let (k, v) = &items[0];
            let pair = |v: &PyValue| match v {
                PyValue::List(p) if p.len() == 2 => (p[0].clone(), n(&p[1])),
                _ => panic!("a recipe pair"),
            };
            match k {
                PyValue::Str(t) if *t == unicode::cps("$n") => {
                    let (leaf, depth) = match v {
                        PyValue::List(p) => (built(&p[1]), n(&p[0])),
                        _ => panic!("a nesting"),
                    };
                    let mut value = leaf;
                    for _ in 0..depth {
                        value = PyValue::List(vec![value]);
                    }
                    value
                }
                PyValue::Str(t) if *t == unicode::cps("$r") => {
                    let (text, count) = pair(v);
                    PyValue::Str(s(&text).repeat(count))
                }
                PyValue::Str(t) if *t == unicode::cps("$i") => {
                    let PyValue::List(p) = v else {
                        panic!("an int recipe")
                    };
                    let sign = if p[1] == PyValue::Bool(true) { "-" } else { "" };
                    PyValue::Int(format!("{sign}{}", "9".repeat(n(&p[0]))))
                }
                PyValue::Str(t) if *t == unicode::cps("$x") => {
                    let (item, count) = pair(v);
                    PyValue::List((0..count).map(|_| built(&item)).collect())
                }
                _ => PyValue::Dict(vec![(k.clone(), built(v))]),
            }
        }
        PyValue::Dict(items) => {
            PyValue::Dict(items.iter().map(|(k, v)| (k.clone(), built(v))).collect())
        }
        PyValue::List(items) => PyValue::List(items.iter().map(built).collect()),
        other => other.clone(),
    }
}

/// The generator's `_same`: equal in type and value, a mapping's order
/// aside, iteratively.
fn same(a: &PyValue, b: &PyValue) -> bool {
    let mut work = vec![(a, b)];
    while let Some((x, y)) = work.pop() {
        match (x, y) {
            (PyValue::List(p), PyValue::List(q)) => {
                if p.len() != q.len() {
                    return false;
                }
                work.extend(p.iter().zip(q));
            }
            (PyValue::Dict(p), PyValue::Dict(q)) => {
                // Keys are strs here; the file's sorted keys lose order.
                if p.len() != q.len() {
                    return false;
                }
                for (k, v1) in p {
                    match q.iter().find(|(k2, _)| k2 == k) {
                        Some((_, v2)) => work.push((v1, v2)),
                        None => return false,
                    }
                }
            }
            (PyValue::Float(p), PyValue::Float(q)) => {
                if float_repr::repr(*p) != float_repr::repr(*q) {
                    return false;
                }
            }
            (PyValue::Str(p), PyValue::Str(q)) => {
                if p != q {
                    return false;
                }
            }
            (PyValue::Int(p), PyValue::Int(q)) => {
                if p != q {
                    return false;
                }
            }
            (PyValue::Bool(p), PyValue::Bool(q)) => {
                if p != q {
                    return false;
                }
            }
            (PyValue::None, PyValue::None) => {}
            _ => return false,
        }
    }
    true
}

fn digest(text: &[u32]) -> PyValue {
    let mut bytes = Vec::new();
    for &c in text {
        // UTF-8 with surrogates passed through, as `surrogatepass`.
        let mut buf = [0u8; 4];
        match char::from_u32(c) {
            Some(ch) => bytes.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes()),
            None => bytes.extend_from_slice(&[
                0xE0 | (c >> 12) as u8,
                0x80 | ((c >> 6) & 0x3F) as u8,
                0x80 | (c & 0x3F) as u8,
            ]),
        }
    }
    let sha: String = Sha256::digest(&bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    PyValue::Dict(vec![
        (PyValue::str("$len"), PyValue::int(text.len() as i128)),
        (PyValue::str("$sha"), PyValue::str(&sha)),
    ])
}

/// The generator's `_short`.
fn short(v: &PyValue) -> PyValue {
    match v {
        PyValue::Int(t) if t.trim_start_matches('-').len() > 200 => digest(&unicode::cps(t)),
        PyValue::Str(t) if t.len() > 200 => digest(t),
        PyValue::List(items) => {
            if items.len() > 8 && items.iter().all(|i| same(&items[0], i)) {
                PyValue::Dict(vec![(
                    PyValue::str("$x"),
                    PyValue::List(vec![short(&items[0]), PyValue::int(items.len() as i128)]),
                )])
            } else {
                PyValue::List(items.iter().map(short).collect())
            }
        }
        PyValue::Dict(items) => {
            PyValue::Dict(items.iter().map(|(k, v)| (k.clone(), short(v))).collect())
        }
        other => other.clone(),
    }
}

fn token_input(record: &PyValue, bases: &[PyValue]) -> PyValue {
    if let Some(raw) = record.get("raw") {
        return built(raw);
    }
    let PyValue::Dict(base) = &bases[n(field(record, "b"))] else {
        panic!("a base token")
    };
    let mut items = base.clone();
    if let Some(PyValue::List(dels)) = record.get("d") {
        for d in dels {
            items.retain(|(k, _)| k != d);
        }
    }
    if let Some(PyValue::Dict(changes)) = record.get("s") {
        for (k, v) in changes {
            let value = built(v);
            match items.iter_mut().find(|(key, _)| key == k) {
                Some(slot) => slot.1 = value,
                None => items.push((k.clone(), value)),
            }
        }
    }
    PyValue::Dict(items)
}

fn check_order(token: &PyValue, order: &[PyStr]) {
    let PyValue::Dict(items) = token else {
        panic!("a token dict")
    };
    let keys: Vec<PyStr> = items.iter().map(|(k, _)| s(k)).collect();
    assert_eq!(keys, order, "to_token's key order");
}

fn answer(input: &PyValue, order: &[PyStr]) -> PyValue {
    match Capture::from_token(input) {
        Err(e) => PyValue::Dict(vec![(PyValue::str("raise"), PyValue::str(e.name()))]),
        Ok(None) => PyValue::None,
        Ok(Some(cap)) => {
            let back = cap.to_token();
            check_order(&back, order);
            let PyValue::Dict(items) = &back else {
                unreachable!()
            };
            PyValue::Dict(
                items
                    .iter()
                    .filter(|(k, v)| {
                        let key = unicode::to_string(&s(k)).expect("a key");
                        !input.get(&key).is_some_and(|i| same(i, v))
                    })
                    .map(|(k, v)| (k.clone(), short(v)))
                    .collect(),
            )
        }
    }
}

fn key_value(k: &Option<ObjectKey>) -> PyValue {
    match k {
        None => PyValue::None,
        Some(k) => PyValue::List(vec![
            PyValue::Str(k.device.clone()),
            PyValue::Str(k.inode.clone()),
            k.birth.clone().map_or(PyValue::None, PyValue::Str),
        ]),
    }
}

fn location_value(record: &PyValue, bases: &[PyValue]) -> PyValue {
    let cap = Capture::from_token(&bases[n(field(record, "b"))])
        .expect("no raise")
        .expect("a capture");
    let session = opt_s(field(record, "x"));
    let l = to_location(&cap, &s(field(record, "m")), session.as_deref());
    let st = |t: &PyStr| PyValue::Str(t.clone());
    let opt = |t: &Option<PyStr>| t.as_ref().map_or(PyValue::None, st);
    let opt_b = |b: Option<bool>| b.map_or(PyValue::None, PyValue::Bool);
    let list = |v: &mut dyn Iterator<Item = &PyStr>| PyValue::List(v.map(st).collect());
    let e = |k: &str, v: PyValue| (PyValue::str(k), v);
    PyValue::Dict(vec![
        e("machine", st(&l.machine)),
        e("cwd", st(&l.cwd)),
        e("state", PyValue::str(l.state.as_str())),
        e("root", opt(&l.root)),
        e(
            "kind",
            l.kind.map_or(PyValue::None, |k| PyValue::str(k.as_str())),
        ),
        e("git_dir", opt(&l.git_dir)),
        e("object_key", key_value(&l.object_key)),
        e("common_object_key", key_value(&l.common_object_key)),
        e("root_object_key", key_value(&l.root_object_key)),
        e("head", opt(&l.head)),
        e("branch", opt(&l.branch)),
        e("ref_tips", list(&mut l.ref_tips.iter())),
        e("remote_keys", list(&mut l.remote_keys.iter())),
        e("alias_keys", list(&mut l.alias_keys.iter())),
        e("shallow", PyValue::Bool(l.shallow)),
        e("token", opt(&l.token)),
        e("token_written", opt(&l.token_written)),
        e("token_status", st(&l.token_status)),
        e(
            "stamps",
            PyValue::List(
                l.stamps
                    .iter()
                    .map(|x| {
                        let mut row = vec![st(&x.name)];
                        row.extend(x.numbers.iter().map(|n| PyValue::Int(n.clone())));
                        PyValue::List(row)
                    })
                    .collect(),
            ),
        ),
        e("ticks", opt_b(l.ticks)),
        e(
            "files",
            PyValue::List(
                l.files
                    .iter()
                    .map(|f| {
                        PyValue::Dict(vec![
                            e("id", st(&f.id)),
                            e("name", st(&f.name)),
                            e("dir", st(&f.dir)),
                            e("path", st(&f.path)),
                            e("boundary", PyValue::Bool(f.boundary)),
                            e("object", key_value(&f.object_key)),
                        ])
                    })
                    .collect(),
            ),
        ),
        e("file_tracked", opt_b(l.file_tracked)),
        e("evicted", PyValue::Bool(l.evicted)),
        e("detail", st(&l.detail)),
        e(
            "manifest",
            l.manifest.as_ref().map_or(PyValue::None, |m| {
                PyValue::Dict(vec![
                    e("name", st(&m.name)),
                    e(
                        "entries",
                        PyValue::List(
                            m.entries
                                .iter()
                                .map(|(p, n)| PyValue::List(vec![st(p), PyValue::Int(n.clone())]))
                                .collect(),
                        ),
                    ),
                ])
            }),
        ),
        e("session", opt(&l.session)),
        e("born", PyValue::Bool(l.born())),
    ])
}

fn object_key(v: &PyValue) -> ObjectKey {
    let PyValue::List(p) = v else { panic!("a key") };
    let k: Key = (s(&p[0]), s(&p[1]), opt_s(&p[2]));
    ObjectKey::from_key(&k)
}

fn native(record: &PyValue, header: &PyValue, order: &[PyStr]) -> (PyValue, PyValue) {
    let bases = match field(header, "bases") {
        PyValue::List(b) => b.as_slice(),
        _ => panic!("bases"),
    };
    let kind = unicode::to_string(&s(field(record, "k"))).expect("a kind");
    let got = match kind.as_str() {
        "cap" | "tok" => answer(&token_input(record, bases), order),
        "loc" => location_value(record, bases),
        "rk" => {
            let url = opt_s(field(record, "u"));
            match remote_key(url.as_deref()) {
                None => PyValue::None,
                Some(RemoteKey::Hosted(k)) => PyValue::Str(k),
                Some(RemoteKey::Local { text, file_url }) => PyValue::List(vec![
                    PyValue::str("local"),
                    PyValue::Str(text),
                    PyValue::Bool(file_url),
                ]),
            }
        }
        "path" => {
            let p = s(field(record, "p"));
            let PyValue::List(all) = field(header, "paths") else {
                panic!("paths")
            };
            let bits: String = all
                .iter()
                .map(|r| if paths::inside(&p, &s(r)) { '1' } else { '0' })
                .collect();
            let got = PyValue::List(vec![
                PyValue::Str(paths::norm(&p)),
                PyValue::Str(paths::basename(&p)),
                PyValue::str(&bits),
            ]);
            let want = PyValue::List(vec![
                field(record, "n").clone(),
                field(record, "b").clone(),
                field(record, "in").clone(),
            ]);
            return (got, want);
        }
        "key" => {
            let a = object_key(field(record, "a"));
            let b = match field(record, "b") {
                PyValue::None => None,
                v => Some(object_key(v)),
            };
            let y = b
                .as_ref()
                .map(|b| a.on_any_device(&b.inode, b.birth.as_deref()));
            let got = PyValue::List(vec![
                PyValue::str(a.compare(b.as_ref()).as_str()),
                y.map_or(PyValue::None, PyValue::Bool),
            ]);
            let want = PyValue::List(vec![field(record, "c").clone(), field(record, "y").clone()]);
            return (got, want);
        }
        other => panic!("no kind {other}"),
    };
    (got, field(record, "o").clone())
}

fn run(test: &str, file: &str) {
    let all = lines(file);
    let header = &all[0];
    let order: Vec<PyStr> = TOKEN_KEYS.iter().map(|k| unicode::cps(k)).collect();
    if let Some(PyValue::List(o)) = header.get("order") {
        assert_eq!(o.iter().map(s).collect::<Vec<_>>(), order, "header order");
    }
    let records = &all[1..];
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    for (i, record) in records.iter().enumerate() {
        let (got, want) = native(record, header, &order);
        let (g, w) = (shown(&got), shown(&want));
        if g != w {
            let mut text = shown(record);
            text.truncate(200);
            failures.push(format!("#{i} {text}: native {g} reference {w}"));
        }
    }
    ledger.settle(records.len(), &failures);
}

#[test]
fn capture_vocabulary_every_case() {
    // Deep values are built and read here: a thread with room for the
    // test's own frames, though nothing in the port recurses on depth.
    std::thread::Builder::new()
        .stack_size(8 << 20)
        .spawn(|| run("golden_capture_vocabulary", "capture_vocabulary.jsonl"))
        .expect("a thread")
        .join()
        .expect("the test");
}

#[test]
fn capture_vocabulary_python_every_case() {
    std::thread::Builder::new()
        .stack_size(8 << 20)
        .spawn(|| {
            run(
                "golden_capture_vocabulary_python",
                "capture_vocabulary_python.jsonl",
            )
        })
        .expect("a thread")
        .join()
        .expect("the test");
}
