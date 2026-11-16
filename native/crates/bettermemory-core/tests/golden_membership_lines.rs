//! Every case of `membership_lines.jsonl` (not gated),
//! `membership_lines_python.jsonl` (gated on the Python release) and
//! `membership_lines_unicode.jsonl` (gated on the Unicode version) against
//! the native `printable` and `membership_lines`. The spelling of texts,
//! recipes and outputs is the generator's
//! (`tests/contract/gen_membership_lines.py`).

mod common;

use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::membership_lines::{
    self as ml, Candidate, ImportCandidate, Named, Previous, Raised,
};
use bettermemory_core::printable;
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

fn int(v: &PyValue) -> i128 {
    match v {
        PyValue::Int(t) => t.parse().expect("an int"),
        other => panic!("not an int: {other:?}"),
    }
}

fn list(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(items) => items,
        other => panic!("not a list: {other:?}"),
    }
}

fn joined(parts: &[PyValue]) -> PyStr {
    let mut out = Vec::new();
    for (i, part) in parts.iter().enumerate() {
        if i > 0 {
            out.push(u32::from(b'\\'));
        }
        out.extend(text(part));
    }
    out
}

/// A text as the generator spells it: a string, `{"c": cps}`, `{"b":
/// parts}` (joined by backslashes), `{"r": [[text, n], ...]}` or
/// `{"range": [lo, hi], "after": cp}`.
fn text(v: &PyValue) -> PyStr {
    match v {
        PyValue::Str(t) => t.clone(),
        PyValue::Dict(_) => {
            if let Some(c) = v.get("c") {
                list(c).iter().map(|x| int(x) as u32).collect()
            } else if let Some(b) = v.get("b") {
                joined(list(b))
            } else if let Some(r) = v.get("r") {
                let mut out = Vec::new();
                for pair in list(r) {
                    let p = list(pair);
                    let piece = text(&p[0]);
                    for _ in 0..int(&p[1]) {
                        out.extend_from_slice(&piece);
                    }
                }
                out
            } else if let Some(range) = v.get("range") {
                let r = list(range);
                let after = v.get("after").map(|a| int(a) as u32);
                let mut out = Vec::new();
                for cp in int(&r[0]) as u32..int(&r[1]) as u32 {
                    out.push(cp);
                    out.extend(after);
                }
                out
            } else {
                panic!("no text spelling: {v:?}")
            }
        }
        other => panic!("not a text: {other:?}"),
    }
}

fn opt(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::None => None,
        other => Some(text(other)),
    }
}

fn opt_int(v: &PyValue) -> Option<i64> {
    match v {
        PyValue::None => None,
        other => Some(int(other) as i64),
    }
}

fn flag(v: &PyValue) -> bool {
    match v {
        PyValue::Bool(b) => *b,
        other => panic!("not a bool: {other:?}"),
    }
}

fn texts(v: &PyValue) -> Vec<PyStr> {
    list(v).iter().map(text).collect()
}

/// An output as the generator spells it, compared with `got`.
fn same(got: &[u32], want: &PyValue) -> bool {
    match want {
        PyValue::Dict(_) if want.get("h").is_some() => {
            let n = int(field(want, "n")) as usize;
            let utf8 = unicode::to_string(got).expect("no surrogate in an output");
            let h = Sha256::digest(utf8.as_bytes());
            let hex: String = h.iter().map(|b| format!("{b:02x}")).collect();
            got.len() == n && PyValue::str(&hex) == *field(want, "h")
        }
        other => got == text(other).as_slice(),
    }
}

fn o(v: &Option<PyStr>) -> Option<&[u32]> {
    v.as_deref()
}

/// The native function `f` of the case's arguments.
fn native(f: &str, a: &[PyValue]) -> PyStr {
    let t = |i: usize| text(&a[i]);
    let q = |i: usize| opt(&a[i]);
    let n = |i: usize| int(&a[i]) as i64;
    match f {
        "printable" => printable::printable(&t(0)),
        "quoted" => printable::quoted(&t(0)),
        "folder_said" => ml::folder_said(o(&q(0)), o(&q(1))),
        "run_in" => ml::run_in(o(&q(0)), o(&q(1))),
        "named" => ml::named(&t(0), &t(1)),
        "answer" => ml::answer(o(&q(0)), &t(1), o(&q(2)), flag(&a[3])),
        "line_first_sight" => {
            let cands: Vec<Candidate> = list(&a[1])
                .iter()
                .map(|c| {
                    let c = list(c);
                    Candidate {
                        name: text(&c[0]),
                        id: text(&c[1]),
                    }
                })
                .collect();
            ml::line_first_sight(&t(0), &cands, &t(2), o(&q(3)), flag(&a[4]), o(&q(5)))
        }
        "line_new" => {
            let prev: Vec<Previous> = list(&a[3])
                .iter()
                .map(|k| {
                    let k = list(k);
                    Previous {
                        checkout: text(&k[0]),
                        name: text(&k[1]),
                        id: text(&k[2]),
                    }
                })
                .collect();
            ml::line_new(&t(0), &t(1), &t(2), &prev, o(&q(4)))
        }
        "line_file_contradiction" => {
            ml::line_file_contradiction(&t(0), &t(1), &t(2), &t(3), &t(4), o(&q(5)))
        }
        "line_joined" => ml::line_joined(&t(0), &t(1), &t(2), &texts(&a[3])),
        "line_alias_joined" => {
            ml::line_alias_joined(&t(0), &t(1), &t(2), &t(3), &t(4), &texts(&a[5]))
        }
        "line_alias_held" => ml::line_alias_held(
            &t(0),
            &t(1),
            &t(2),
            &t(3),
            &t(4),
            &t(5),
            &t(6),
            o(&q(7)),
            o(&q(8)),
            &t(9),
        ),
        "line_remote_diverged" => {
            ml::line_remote_diverged(&t(0), &t(1), &t(2), &t(3), &t(4), o(&q(5)))
        }
        "line_legacy" => ml::line_legacy(
            &t(0),
            n(1),
            n(2),
            &t(3),
            n(4),
            &t(5),
            &t(6),
            opt_int(&a[7]),
            o(&q(8)),
        ),
        "line_subproject" => {
            ml::line_subproject(&t(0), &t(1), &t(2), &t(3), &t(4), &t(5), o(&q(6)))
        }
        "line_unreadable" => ml::line_unreadable(&t(0), &t(1)),
        "line_deferred" => ml::line_deferred(&t(0), &t(1)),
        "line_import" => {
            let cands: Vec<ImportCandidate> = list(&a[2])
                .iter()
                .map(|c| {
                    let c = list(c);
                    ImportCandidate {
                        name: text(&c[0]),
                        id: text(&c[1]),
                        codes: texts(&c[2]),
                    }
                })
                .collect();
            ml::line_import(&t(0), &t(1), &cands, o(&q(3)))
        }
        "line_unresolved" => ml::line_unresolved(&t(0), n(1), n(2), &t(3), &t(4), o(&q(5))),
        "line_folder_project" => ml::line_folder_project(&t(0), &t(1), flag(&a[2])),
        "line_new_folder" => ml::line_new_folder(&t(0), &t(1), &t(2)),
        "line_boundary_added" => ml::line_boundary_added(&t(0), &t(1), &t(2)),
        "line_boundary_dropped" => ml::line_boundary_dropped(&t(0), &t(1), &t(2), &t(3), &t(4)),
        "line_across_boundary" => ml::line_across_boundary(&t(0), &texts(&a[1])),
        "line_content_crowded" => ml::line_content_crowded(&t(0), n(1), n(2)),
        "line_restated" => {
            let named: Vec<Named> = list(&a[3])
                .iter()
                .map(|c| {
                    let c = list(c);
                    Named {
                        name: opt(&c[0]),
                        id: text(&c[1]),
                    }
                })
                .collect();
            ml::line_restated(&t(0), &t(1), &t(2), &named, flag(&a[4]), o(&q(5)), o(&q(6)))
        }
        other => panic!("no function {other}"),
    }
}

fn shown(v: &PyValue) -> String {
    json::utf8_text(&json::dumps(v, DumpOptions::SORTED_ASCII_COMPACT).expect("dumps"))
}

/// Compares every case of `file`; a record with `each` holds one case per
/// code point. Returns the cases compared.
/// A row's value as the generator spells it: `{"$n": [depth, leaf]}`
/// lists around a leaf, `{"$p": n}` ten to the power n, else as read.
fn row_value(v: &PyValue) -> PyValue {
    if let PyValue::Dict(items) = v {
        if items.len() == 1 {
            if let Some(spec) = v.get("$n") {
                let spec = list(spec);
                let mut value = row_value(&spec[1]);
                for _ in 0..int(&spec[0]) {
                    value = PyValue::List(vec![value]);
                }
                return value;
            }
            if let Some(power) = v.get("$p") {
                return PyValue::Int(format!("1{}", "0".repeat(int(power) as usize)));
            }
        }
        return PyValue::Dict(
            items
                .iter()
                .map(|(k, x)| (k.clone(), row_value(x)))
                .collect(),
        );
    }
    if let PyValue::List(items) = v {
        return PyValue::List(items.iter().map(row_value).collect());
    }
    v.clone()
}

/// The row a `restated_row` case reads: the base row, its `d` values
/// put in place, its `drop` columns left out.
fn case_row(base: &PyValue, record: &PyValue) -> PyValue {
    let PyValue::Dict(columns) = base else {
        panic!("a base row")
    };
    let dropped: Vec<PyStr> = texts(field(record, "drop"));
    let changed = field(record, "d");
    let mut out = Vec::new();
    for (k, v) in columns {
        let PyValue::Str(name) = k else {
            panic!("a column")
        };
        if dropped.contains(name) {
            continue;
        }
        let key = unicode::to_string(name).expect("a column name");
        let value = changed.get(&key).unwrap_or(v);
        out.push((k.clone(), row_value(value)));
    }
    PyValue::Dict(out)
}

/// The native answer for a row: the restated line with every candidate's
/// project gone and nothing filed, or what is raised.
fn row_answer(row: &PyValue, at: Option<&[u32]>) -> Result<PyStr, Raised> {
    let r = ml::restated_row(row)?;
    let named: Vec<Named> = r
        .candidates
        .iter()
        .map(|c| Named {
            name: None,
            id: c.clone(),
        })
        .collect();
    Ok(ml::line_restated(
        &r.id,
        &r.kind,
        &r.reason,
        &named,
        false,
        r.folder.as_deref(),
        at,
    ))
}

fn hex32(line: &[u32]) -> String {
    let utf8 = unicode::to_string(line).expect("no surrogate in a line");
    Sha256::digest(utf8.as_bytes())
        .iter()
        .take(16)
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn run(test: &str, file: &str, records: &[PyValue], base_row: Option<&PyValue>) {
    let mut ledger = Ledger::new(test, file);
    let mut total = 0;
    for (i, record) in records.iter().enumerate() {
        let f = match field(record, "f") {
            PyValue::Str(t) => unicode::to_string(t).expect("a name"),
            other => panic!("not a name: {other:?}"),
        };
        if let Some(each) = record.get("each") {
            let outs = list(field(record, "o"));
            for (cp, want) in list(each).iter().zip(outs) {
                let cp = int(cp) as u32;
                let got = native(&f, &[PyValue::Str(vec![cp])]);
                total += 1;
                let label = format!("line {} {f} U+{cp:04X}", i + 1);
                let ok = same(&got, want);
                assert!(
                    !ledger.differs_fatally(&label, ok),
                    "{label}: got {:?}, want {}",
                    unicode::to_string(&got),
                    shown(want)
                );
            }
            continue;
        }
        if f == "restated_grid" {
            let a = list(field(record, "a"));
            let outs = list(field(record, "o"));
            let mut k = 0;
            for kind in list(field(record, "kinds")) {
                for reason in list(field(record, "reasons")) {
                    let mut args = a.to_vec();
                    args[1] = kind.clone();
                    args[2] = reason.clone();
                    let got = hex32(&native("line_restated", &args));
                    total += 1;
                    let label = format!("line {} grid {} {}", i + 1, shown(kind), shown(reason));
                    let ok = PyValue::str(&got) == outs[k];
                    assert!(!ledger.differs_fatally(&label, ok), "{label}: got {got}");
                    k += 1;
                }
            }
            assert_eq!(k, outs.len());
            continue;
        }
        if f == "restated_row" {
            let row = case_row(base_row.expect("the tables' row"), record);
            let at = opt(field(record, "at"));
            let got = row_answer(&row, at.as_deref());
            let want = field(record, "o");
            total += 1;
            let label = format!("line {} row {}", i + 1, shown(field(record, "d")));
            let ok = match (&got, want.get("raised")) {
                (Err(e), Some(r)) => {
                    let r = list(r);
                    let message_same = match &r[1] {
                        PyValue::None => true,
                        m => *m == PyValue::str(&e.message),
                    };
                    r[0] == PyValue::str(e.class) && message_same
                }
                (Ok(line), None) => same(line, want),
                _ => false,
            };
            assert!(
                !ledger.differs_fatally(&label, ok),
                "{label}: got {got:?}, want {}",
                shown(want)
            );
            continue;
        }
        let args = list(field(record, "a"));
        let got = native(&f, args);
        let want = field(record, "o");
        total += 1;
        let label = format!("line {} {f} {}", i + 1, shown(field(record, "a")));
        let ok = same(&got, want);
        assert!(
            !ledger.differs_fatally(&label, ok),
            "{label}: got {:?}, want {}",
            unicode::to_string(&got),
            shown(want)
        );
    }
    ledger.finish(total);
}

fn cps_of(v: &PyValue) -> Vec<u32> {
    list(v).iter().map(|x| int(x) as u32).collect()
}

#[test]
fn membership_lines_every_case() {
    let all = lines("membership_lines.jsonl");
    let tables = &all[0];
    assert_eq!(field(tables, "k"), &PyValue::str("tables"));
    assert_eq!(cps_of(field(tables, "quote_like")), printable::QUOTE_LIKE);
    assert_eq!(
        cps_of(field(tables, "quote_combining")),
        printable::QUOTE_COMBINING
    );
    let words: Vec<(PyStr, PyStr)> = list(field(tables, "words"))
        .iter()
        .map(|p| (text(&list(p)[0]), text(&list(p)[1])))
        .collect();
    let native_words: Vec<(PyStr, PyStr)> = ml::WORDS
        .iter()
        .map(|(k, w)| (unicode::cps(k), unicode::cps(w)))
        .collect();
    assert_eq!(words, native_words);
    let said: Vec<(PyStr, PyStr)> = list(field(tables, "reason_said"))
        .iter()
        .map(|p| (text(&list(p)[0]), text(&list(p)[1])))
        .collect();
    let native_said: Vec<(PyStr, PyStr)> = ml::REASON_SAID
        .iter()
        .map(|(k, w)| (unicode::cps(k), unicode::cps(w)))
        .collect();
    assert_eq!(said, native_said);
    assert_eq!(
        field(tables, "reason_unnamed"),
        &PyValue::str(ml::REASON_UNNAMED)
    );
    let kinds: Vec<PyStr> = texts(field(tables, "kinds"));
    let native_kinds: Vec<PyStr> = [
        ml::FIRST_SIGHT,
        ml::FILE_CONTRADICTION,
        ml::REMOTE_DIVERGED,
        ml::SUBPROJECT_CONTRADICTION,
        ml::DUPLICATE_CHECKOUT,
        ml::LEGACY,
        ml::IMPORT,
    ]
    .iter()
    .map(|k| unicode::cps(k))
    .collect();
    assert_eq!(kinds, native_kinds);
    run(
        "membership_lines_every_case",
        "membership_lines.jsonl",
        &all[1..],
        Some(field(tables, "row")),
    );
}

#[test]
fn membership_lines_unicode_every_case() {
    let all = lines("membership_lines_unicode.jsonl");
    run(
        "membership_lines_unicode_every_case",
        "membership_lines_unicode.jsonl",
        &all,
        None,
    );
}

/// The stored rows written with a value nested 100,000 deep in one
/// column (`membership_lines_python.jsonl`, gated on the Python release,
/// placed by input): seven record the reference's RecursionError (the
/// interpreter build's and its stack's limit), the others a line or a
/// ValueError. The native answer is the port's own depth limit.
#[test]
fn membership_lines_python_every_case() {
    std::thread::Builder::new()
        .stack_size(8 << 20)
        .spawn(|| {
            let all = lines("membership_lines_python.jsonl");
            let header = &all[0];
            assert_eq!(field(header, "k"), &PyValue::str("python"));
            assert_eq!(int(field(header, "cases")) as usize, all.len() - 1);
            run(
                "membership_lines_python_every_case",
                "membership_lines_python.jsonl",
                &all[1..],
                Some(field(header, "row")),
            );
        })
        .expect("a thread")
        .join()
        .expect("the test");
}
