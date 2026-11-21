//! The spelling of `membership_rows.jsonl` (`tests/contract/gen_membership_rows.py`):
//! values decoded and encoded, a case's row built, and the native answer
//! for a case, encoded as the generator encodes the reference's.

use std::collections::BTreeSet;

use bettermemory_core::capture::location::{Location, ProjectFileRef};
use bettermemory_core::capture::{Kind, State};
use bettermemory_core::json;
use bettermemory_core::membership_lines::Raised;
use bettermemory_core::membership_records::records::View;
use bettermemory_core::membership_rows as rows;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use sha2::{Digest, Sha256};

pub fn lines(name: &str) -> Vec<PyValue> {
    let text = std::fs::read_to_string(super::golden_path(name))
        .unwrap_or_else(|e| panic!("cannot read golden file {name}: {e}"));
    text.lines()
        .map(|l| json::loads(&unicode::cps(l)).expect("a JSON line"))
        .collect()
}

pub fn field<'a>(v: &'a PyValue, k: &str) -> &'a PyValue {
    v.get(k).unwrap_or_else(|| panic!("no field {k} in {v:?}"))
}

pub fn list(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(items) => items,
        other => panic!("not a list: {other:?}"),
    }
}

pub fn name(v: &PyValue) -> String {
    match v {
        PyValue::Str(t) => unicode::to_string(t).expect("a name"),
        other => panic!("not a name: {other:?}"),
    }
}

pub fn int(v: &PyValue) -> i128 {
    match v {
        PyValue::Int(t) => t.parse().expect("an int"),
        other => panic!("not an int: {other:?}"),
    }
}

fn tagged(v: &PyValue) -> Option<(String, &PyValue)> {
    match v {
        PyValue::Dict(items) if items.len() == 1 => match &items[0].0 {
            PyValue::Str(k) if k.first() == Some(&u32::from(b'$')) => {
                Some((unicode::to_string(k).expect("a tag"), &items[0].1))
            }
            _ => None,
        },
        _ => None,
    }
}

/// A value as the file spells it, decoded.
pub fn dec(v: &PyValue) -> PyValue {
    if let PyValue::List(items) = v {
        return PyValue::List(items.iter().map(dec).collect());
    }
    let Some((tag, body)) = tagged(v) else {
        return v.clone();
    };
    match tag.as_str() {
        "$d" => PyValue::Dict(
            list(body)
                .iter()
                .map(|p| (dec(&list(p)[0]), dec(&list(p)[1])))
                .collect(),
        ),
        "$t" => PyValue::Tuple(list(body).iter().map(dec).collect()),
        "$i" => PyValue::int_from_decimal(&name(body)).expect("an int"),
        "$f" => PyValue::Float(name(body).parse().expect("a float")),
        "$c" => PyValue::Str(list(body).iter().map(|c| int(c) as u32).collect()),
        "$p" => PyValue::Int(format!("1{}", "0".repeat(int(body) as usize))),
        "$n" => {
            let b = list(body);
            let mut out = dec(&b[1]);
            for _ in 0..int(&b[0]) {
                out = PyValue::List(vec![out]);
            }
            out
        }
        other => panic!("unknown tag {other}"),
    }
}

fn tag(t: &str, body: PyValue) -> PyValue {
    PyValue::Dict(vec![(PyValue::str(t), body)])
}

/// UTF-8 with surrogates written as three bytes (`surrogatepass`).
pub fn utf8(text: &[u32]) -> Vec<u8> {
    let mut out = Vec::new();
    for &c in text {
        if (0xD800..0xE000).contains(&c) {
            out.extend([
                0xE0 | (c >> 12) as u8,
                0x80 | ((c >> 6) & 0x3F) as u8,
                0x80 | (c & 0x3F) as u8,
            ]);
        } else {
            let ch = char::from_u32(c).expect("a code point");
            let mut b = [0; 4];
            out.extend(ch.encode_utf8(&mut b).as_bytes());
        }
    }
    out
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// A value as the generator's `enc` spells it.
pub fn enc(v: &PyValue) -> PyValue {
    match v {
        PyValue::Int(t) => {
            let digits = t.trim_start_matches('-');
            if digits.len() <= 16 {
                let n: i128 = t.parse().expect("an int");
                if n.abs() < (1i128 << 53) {
                    return v.clone();
                }
            }
            let zeros = &digits[1..];
            if !t.starts_with('-') && digits.starts_with('1') && zeros.bytes().all(|b| b == b'0') {
                return tag("$p", PyValue::int(zeros.len() as i128));
            }
            tag("$i", PyValue::Str(unicode::cps(t)))
        }
        PyValue::Float(f) if !f.is_finite() => {
            let text = if f.is_nan() {
                "nan"
            } else if *f > 0.0 {
                "inf"
            } else {
                "-inf"
            };
            tag("$f", PyValue::str(text))
        }
        PyValue::Str(s) if s.len() > 256 => tag(
            "$h",
            PyValue::List(vec![
                PyValue::int(s.len() as i128),
                PyValue::str(&hex(&Sha256::digest(utf8(s)))),
            ]),
        ),
        PyValue::Str(s)
            if s.iter()
                .any(|&c| c == 0x5C || (0xD800..0xE000).contains(&c)) =>
        {
            tag(
                "$c",
                PyValue::List(s.iter().map(|&c| PyValue::int(i128::from(c))).collect()),
            )
        }
        PyValue::Tuple(items) => tag("$t", PyValue::List(items.iter().map(enc).collect())),
        PyValue::List(items) => PyValue::List(items.iter().map(enc).collect()),
        PyValue::Dict(items) => tag(
            "$d",
            PyValue::List(
                items
                    .iter()
                    .map(|(k, x)| PyValue::List(vec![enc(k), enc(x)]))
                    .collect(),
            ),
        ),
        other => other.clone(),
    }
}

/// A case's row: the base row with `d` put in place and `drop` left out.
pub fn case_row(bases: &PyValue, case: &PyValue) -> PyValue {
    let f = name(field(case, "f"));
    let mut row = match &dec(field(bases, &f)) {
        PyValue::Dict(items) => items.clone(),
        _ => panic!("a base row"),
    };
    let drop: Vec<&PyValue> = list(field(case, "drop")).iter().collect();
    row.retain(|(k, _)| !drop.contains(&k));
    if let PyValue::Dict(d) = field(case, "d") {
        for (k, v) in d {
            let v = dec(v);
            match row.iter_mut().find(|(c, _)| c == k) {
                Some(slot) => slot.1 = v,
                None => row.push((k.clone(), v)),
            }
        }
    }
    PyValue::Dict(row)
}

pub fn view_of(v: &PyValue) -> View {
    let t = |i: usize| match &list(v)[i] {
        PyValue::Str(s) => s.clone(),
        other => panic!("not a str: {other:?}"),
    };
    View {
        checkout: t(0),
        machine: t(1),
        path: t(2),
        first_seen: t(3),
        last_seen: t(4),
        order: int(&list(v)[5]) as i64,
    }
}

fn view_tuple(v: &View) -> PyValue {
    PyValue::List(vec![
        PyValue::Str(v.checkout.clone()),
        PyValue::Str(v.machine.clone()),
        PyValue::Str(v.path.clone()),
        PyValue::Str(v.first_seen.clone()),
        PyValue::Str(v.last_seen.clone()),
        PyValue::int(i128::from(v.order)),
    ])
}

fn s(key: &str, v: PyValue) -> (PyValue, PyValue) {
    (PyValue::str(key), v)
}

pub fn raised(e: &Raised) -> PyValue {
    if e.class == rows::REFUSED {
        return PyValue::Dict(vec![s("refused", PyValue::Bool(true))]);
    }
    PyValue::Dict(vec![s(
        "raised",
        PyValue::List(vec![PyValue::str(e.class), PyValue::str(&e.message)]),
    )])
}

fn opt(v: &Option<PyStr>) -> PyValue {
    v.as_ref()
        .map_or(PyValue::None, |t| PyValue::Str(t.clone()))
}

/// The native answer for a row, in full, spelled as the generator's
/// `_answer` spells the reference's (a raise with its message kept).
pub fn answer(f: &str, row: &PyValue, views: &[View]) -> PyValue {
    let out = match f {
        "checkout" => rows::checkout_from(row, views).map(|k| {
            PyValue::Dict(vec![
                s("replaced_by", enc(&opt(&k.replaced_by))),
                s("row", enc(&rows::checkout_row(&k))),
                s(
                    "sessions",
                    PyValue::List(
                        k.sessions
                            .iter()
                            .map(|x| enc(&PyValue::Str(x.clone())))
                            .collect(),
                    ),
                ),
                s(
                    "views",
                    PyValue::List(k.views.iter().map(|v| enc(&view_tuple(v))).collect()),
                ),
            ])
        }),
        "view" => rows::view_from(row).map(|v| enc(&view_tuple(&v))),
        "project" => rows::project_from(row).map(|p| enc(&rows::project_row(&p))),
        "proposal" => rows::proposal_from(row).map(|pr| {
            let written = match rows::proposal_row(&pr) {
                Ok(w) => enc(&w),
                Err(e) => raised(&e),
            };
            let sig = pr
                .signature
                .clone()
                .map_or(PyValue::None, |v| enc(&PyValue::Tuple(v)));
            PyValue::Dict(vec![
                s("ev", enc(&pr.evidence)),
                s("row", written),
                s("sig", sig),
            ])
        }),
        other => panic!("unknown function {other}"),
    };
    out.unwrap_or_else(|e| raised(&e))
}

/// A file's answer expanded against the base row's (`full`): `w` laid
/// over the full row's columns, every other key over the full answer.
pub fn expanded(f: &str, full: &PyValue, o: &PyValue) -> PyValue {
    if f == "view" || o.get("raised").is_some() || o.get("refused").is_some() {
        return o.clone();
    }
    let base = field(full, f);
    let mut out: Vec<(PyValue, PyValue)> = match f {
        "project" => vec![s("row", base.clone())],
        _ => match base {
            PyValue::Dict(items) => items.clone(),
            other => panic!("a full answer: {other:?}"),
        },
    };
    let PyValue::Dict(given) = o else {
        panic!("an answer")
    };
    for (k, v) in given {
        if *k == PyValue::str("w") {
            let slot = out
                .iter_mut()
                .find(|(c, _)| *c == PyValue::str("row"))
                .expect("row");
            let pairs = match &mut slot.1 {
                PyValue::Dict(d) => match &mut d[0].1 {
                    PyValue::List(p) => p,
                    _ => panic!("pairs"),
                },
                _ => panic!("a row"),
            };
            let PyValue::Dict(w) = v else { panic!("w") };
            for (col, val) in w {
                for p in pairs.iter_mut() {
                    if let PyValue::List(cv) = p {
                        if cv[0] == *col {
                            cv[1] = val.clone();
                        }
                    }
                }
            }
        } else {
            match out.iter_mut().find(|(c, _)| c == k) {
                Some(slot) => slot.1 = v.clone(),
                None => out.push((k.clone(), v.clone())),
            }
        }
    }
    if f == "project" {
        return out.remove(0).1;
    }
    PyValue::Dict(out)
}

/// Whether a native answer is the file's: a raise's message is compared
/// only where the file keeps it.
pub fn same(got: &PyValue, want: &PyValue) -> bool {
    if let (Some(g), Some(w)) = (got.get("raised"), want.get("raised")) {
        let (g, w) = (list(g), list(w));
        return g[0] == w[0] && (w[1] == PyValue::None || g[1] == w[1]);
    }
    match (got, want) {
        (PyValue::Dict(g), PyValue::Dict(w)) if tagged(got).is_none() && tagged(want).is_none() => {
            g.len() == w.len()
                && g.iter()
                    .all(|(k, v)| want.get(&name(k)).is_some_and(|x| same(v, x)))
        }
        _ => got == want,
    }
}

/// A raise's message as the file keeps it: a KeyError's and two of
/// CPython's conversion texts; null for every other.
pub fn kept(o: PyValue) -> PyValue {
    let Some(r) = o.get("raised") else {
        return o;
    };
    let r = list(r);
    let m = name(&r[1]);
    let keep = name(&r[0]) == "KeyError"
        || m == "'int' object is not iterable"
        || (m.starts_with("dictionary update sequence element #")
            && m.ends_with("; 2 is required"));
    if keep {
        return o;
    }
    PyValue::Dict(vec![s(
        "raised",
        PyValue::List(vec![r[0].clone(), PyValue::None]),
    )])
}

/// The location a `location` case names: the generator's base spec with
/// the case's changes.
pub fn location(change: &PyValue) -> Location {
    let base = json::loads(&unicode::cps(
        r#"{"root": "/w/r", "kind": "primary", "git_dir": "/w/r/.git", "head": "h1",
        "remote_keys": ["b", "a", "C"], "shallow": false, "token": "t1",
        "token_written": "t2", "token_status": "present",
        "files": [["F1", "/w/r/sub"], ["F2", "/w/r"], ["F3", "/w/r"]]}"#,
    ))
    .expect("the base spec");
    let get = |k: &str| dec(change.get(k).unwrap_or_else(|| field(&base, k)));
    let text = |k: &str| match &get(k) {
        PyValue::Str(t) => Some(t.clone()),
        PyValue::None => None,
        other => panic!("not a text: {other:?}"),
    };
    let kind = text("kind").map(|k| match unicode::to_string(&k).expect("a kind").as_str() {
        "folder" => Kind::Folder,
        "primary" => Kind::Primary,
        other => panic!("kind {other}"),
    });
    let files = list(&get("files"))
        .iter()
        .map(|p| {
            let p = list(p);
            let t = |v: &PyValue| match v {
                PyValue::Str(t) => t.clone(),
                _ => panic!("a text"),
            };
            let dir = t(&p[1]);
            let mut path = dir.clone();
            path.extend(unicode::cps("/bettermemory.toml"));
            ProjectFileRef {
                id: t(&p[0]),
                name: unicode::cps("n"),
                dir,
                path,
                boundary: false,
                object_key: None,
            }
        })
        .collect();
    let remote_keys: BTreeSet<PyStr> = list(&get("remote_keys"))
        .iter()
        .map(|k| match k {
            PyValue::Str(t) => t.clone(),
            _ => panic!("a key"),
        })
        .collect();
    Location {
        machine: unicode::cps("M1"),
        cwd: unicode::cps("/w"),
        state: State::Repo,
        root: text("root"),
        kind,
        git_dir: text("git_dir"),
        object_key: None,
        common_object_key: None,
        root_object_key: None,
        head: text("head"),
        branch: None,
        ref_tips: Vec::new(),
        remote_keys,
        alias_keys: BTreeSet::new(),
        shallow: get("shallow") == PyValue::Bool(true),
        token: text("token"),
        token_written: text("token_written"),
        token_status: text("token_status").expect("a status"),
        stamps: Vec::new(),
        ticks: None,
        files,
        file_tracked: None,
        evicted: false,
        detail: Vec::new(),
        manifest: None,
        session: None,
    }
}

/// The generator's `_Lcg`.
pub struct Lcg(pub u64);

impl Lcg {
    pub fn next(&mut self, n: usize) -> usize {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        ((self.0 >> 33) % n as u64) as usize
    }
}
