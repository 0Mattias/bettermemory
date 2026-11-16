//! Every case of `models.jsonl` (the payload form: a record as a log
//! payload carries it, through `<Model>.model_validate`) and
//! `models_rows.jsonl` (the row form: `store_rows`' readers), validated by
//! the native build and compared with the reference's committed outcome:
//! the record's payload text and row values by digest (in full for the
//! showcase cases, with the coerced field's dumped value where the file
//! gives it), or the error's class, title and `(loc, type)` pairs. The
//! batteries are regenerated from the files' linear congruence. No
//! reference Python is needed.

mod common;

use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::rows::{self, Row};
use bettermemory_core::records::{
    Actor, Episode, LocItem, Memory, MemoryLink, Origin, RecordError, Tombstone,
};
use bettermemory_core::store::SqlValue;
use bettermemory_core::unicode;
use common::golden_path;
use common::parity::Ledger;
use sha2::{Digest, Sha256};

const MODELS: [&str; 6] = ["memory", "tombstone", "episode", "origin", "actor", "link"];

fn lines(name: &str) -> Vec<PyValue> {
    let text = std::fs::read_to_string(golden_path(name)).expect("golden file");
    text.lines()
        .map(|l| json::loads(&unicode::cps(l)).expect("a JSON line"))
        .collect()
}

fn get<'a>(v: &'a PyValue, k: &str) -> &'a PyValue {
    v.get(k).unwrap_or_else(|| panic!("no key {k}"))
}

fn text(v: &PyValue) -> String {
    match v {
        PyValue::Str(s) => unicode::to_string(s).expect("text"),
        other => panic!("not a string: {other:?}"),
    }
}

fn list(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(items) => items,
        other => panic!("not a list: {other:?}"),
    }
}

fn int(v: &PyValue) -> i128 {
    match v {
        PyValue::Int(t) => t.parse().expect("int"),
        other => panic!("not an int: {other:?}"),
    }
}

/// A tagged value (`gen_models.tag`) as the Python value it names.
fn untag(v: &PyValue) -> PyValue {
    match v {
        PyValue::List(items) => PyValue::List(items.iter().map(untag).collect()),
        PyValue::Dict(items) => {
            if items.len() == 1 {
                let (k, inner) = &items[0];
                match unicode::to_string(k_str(k)).as_deref().unwrap_or("") {
                    "$cps" => {
                        return PyValue::Str(list(inner).iter().map(|c| int(c) as u32).collect())
                    }
                    "$float" => {
                        return PyValue::Float(match text(inner).as_str() {
                            "nan" => f64::NAN,
                            "inf" => f64::INFINITY,
                            _ => f64::NEG_INFINITY,
                        })
                    }
                    "$deep" => {
                        let n = int(inner) as usize;
                        return PyValue::str(&("[".repeat(n) + &"]".repeat(n)));
                    }
                    "$dict" => {
                        return PyValue::Dict(
                            list(inner)
                                .iter()
                                .map(|pair| {
                                    let pair = list(pair);
                                    (untag(&pair[0]), untag(&pair[1]))
                                })
                                .collect(),
                        )
                    }
                    "$bytes" => panic!("bytes in a payload value"),
                    _ => {}
                }
            }
            PyValue::Dict(items.iter().map(|(k, v)| (k.clone(), untag(v))).collect())
        }
        other => other.clone(),
    }
}

fn k_str(k: &PyValue) -> &[u32] {
    match k {
        PyValue::Str(s) => s,
        _ => &[],
    }
}

fn unhex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).expect("hex"))
        .collect()
}

/// A tagged row value as the SQLite value it names.
fn sql(v: &PyValue) -> SqlValue {
    if let PyValue::Dict(items) = v {
        if let Some((k, inner)) = items.first() {
            match unicode::to_string(k_str(k)).as_deref().unwrap_or("") {
                "$bytes" => return SqlValue::Blob(unhex(&text(inner))),
                "$cps" => {
                    let cps: Vec<u32> = list(inner).iter().map(|c| int(c) as u32).collect();
                    return SqlValue::Text(unicode::to_string(&cps).expect("row text"));
                }
                "$deep" => {
                    let n = int(inner) as usize;
                    return SqlValue::Text("[".repeat(n) + &"]".repeat(n));
                }
                "$float" => {
                    return SqlValue::Real(match text(inner).as_str() {
                        "inf" => f64::INFINITY,
                        "-inf" => f64::NEG_INFINITY,
                        _ => f64::NAN,
                    })
                }
                _ => {}
            }
        }
    }
    match v {
        PyValue::None => SqlValue::Null,
        PyValue::Int(t) => SqlValue::Integer(t.parse().expect("i64")),
        PyValue::Float(f) => SqlValue::Real(*f),
        PyValue::Str(s) => SqlValue::Text(unicode::to_string(s).expect("row text")),
        other => panic!("not a row value: {other:?}"),
    }
}

/// `gen_models.tag` of an outcome value: a surrogate string as `$cps`.
fn tag(v: &PyValue) -> PyValue {
    match v {
        PyValue::Float(f) if !f.is_finite() => PyValue::Dict(vec![(
            PyValue::str("$float"),
            PyValue::str(if f.is_nan() {
                "nan"
            } else if *f > 0.0 {
                "inf"
            } else {
                "-inf"
            }),
        )]),
        PyValue::Str(s) if s.iter().any(|&c| (0xD800..0xE000).contains(&c)) => {
            PyValue::Dict(vec![(
                PyValue::str("$cps"),
                PyValue::List(s.iter().map(|&c| PyValue::int(i128::from(c))).collect()),
            )])
        }
        PyValue::List(items) => PyValue::List(items.iter().map(tag).collect()),
        PyValue::Dict(items) => {
            PyValue::Dict(items.iter().map(|(k, v)| (k.clone(), tag(v))).collect())
        }
        other => other.clone(),
    }
}

/// Equal as the file writes values: sorted keys.
fn same(a: &PyValue, b: &PyValue) -> bool {
    json::dumps(a, DumpOptions::SORTED_ASCII_COMPACT)
        == json::dumps(b, DumpOptions::SORTED_ASCII_COMPACT)
}

/// `gen_models._file_value`: `tag`, and text holding a backslash as code
/// points.
fn file_tag(v: &PyValue) -> PyValue {
    match v {
        PyValue::Str(s) if s.contains(&u32::from(b'\\')) => PyValue::Dict(vec![(
            PyValue::str("$cps"),
            PyValue::List(s.iter().map(|&c| PyValue::int(i128::from(c))).collect()),
        )]),
        PyValue::List(items) => PyValue::List(items.iter().map(file_tag).collect()),
        PyValue::Dict(items) if !items.iter().any(|(k, _)| tag(k) != *k) => PyValue::Dict(
            items
                .iter()
                .map(|(k, v)| (k.clone(), file_tag(v)))
                .collect(),
        ),
        other => tag(other),
    }
}

/// A row value as `gen_models.tag` spells it.
fn sql_tagged(v: &SqlValue) -> PyValue {
    match v {
        SqlValue::Null => PyValue::None,
        SqlValue::Integer(i) => PyValue::int(i128::from(*i)),
        SqlValue::Real(f) => tag(&PyValue::Float(*f)),
        SqlValue::Text(t) => tag(&PyValue::str(t)),
        SqlValue::Blob(b) => PyValue::Dict(vec![(
            PyValue::str("$bytes"),
            PyValue::str(&b.iter().map(|x| format!("{x:02x}")).collect::<String>()),
        )]),
    }
}

fn digest(outcome: &PyValue, length: usize) -> String {
    let text = json::dumps(&tag(outcome), DumpOptions::SORTED_ASCII_COMPACT).expect("dumps");
    let bytes: Vec<u8> = text.iter().map(|&c| c as u8).collect();
    let hash = Sha256::digest(&bytes);
    hash.iter().map(|b| format!("{b:02x}")).collect::<String>()[..length].to_owned()
}

/// A record's outcome: `["ok", dump, dump_error, row]`.
fn ok_outcome(
    dump: Result<String, &'static str>,
    row: Result<Vec<PyValue>, &'static str>,
) -> PyValue {
    let (dump, mut dump_error) = match dump {
        Ok(t) => (PyValue::str(&t), PyValue::None),
        Err(e) => (PyValue::None, PyValue::str(e)),
    };
    let row = match row {
        Ok(r) => PyValue::List(r),
        Err(e) => {
            if dump_error == PyValue::None {
                dump_error = PyValue::str(e);
            }
            PyValue::None
        }
    };
    PyValue::List(vec![PyValue::str("ok"), dump, dump_error, row])
}

fn err_outcome(e: &RecordError) -> PyValue {
    match e {
        RecordError::Raised(cls) => PyValue::List(vec![
            PyValue::str("err"),
            PyValue::str(cls),
            PyValue::None,
            PyValue::None,
        ]),
        RecordError::Validation { title, errors } => PyValue::List(vec![
            PyValue::str("err"),
            PyValue::str("ValidationError"),
            PyValue::str(title),
            PyValue::List(
                errors
                    .iter()
                    .map(|e| {
                        PyValue::List(vec![
                            PyValue::List(
                                e.loc
                                    .iter()
                                    .map(|l| match l {
                                        LocItem::Key(k) => PyValue::Str(k.clone()),
                                        LocItem::Index(i) => PyValue::int(*i as i128),
                                    })
                                    .collect(),
                            ),
                            PyValue::str(e.kind),
                        ])
                    })
                    .collect(),
            ),
        ]),
    }
}

fn name<T>(r: Result<T, bettermemory_core::pyvalue::PyError>) -> Result<T, &'static str> {
    r.map_err(|e| e.name())
}

fn origin_row(o: &Origin) -> Result<Vec<PyValue>, &'static str> {
    name(bettermemory_core::records::origin_json(&Some(o.clone()))).map(|v| vec![v])
}

/// The native outcome for a payload-form input.
fn payload_outcome(model: &str, data: &PyValue) -> PyValue {
    let r =
        match model {
            "memory" => Memory::from_payload(data)
                .map(|m| ok_outcome(name(m.payload_text()), name(m.row()))),
            "tombstone" => Tombstone::from_payload(data)
                .map(|m| ok_outcome(name(m.payload_text()), name(m.row()))),
            "episode" => Episode::from_payload(data)
                .map(|m| ok_outcome(name(m.payload_text()), name(m.row()))),
            "origin" => Origin::from_value(data).map(|o| {
                ok_outcome(
                    name(json::canonical_payload(&o.dump(false))),
                    origin_row(&o),
                )
            }),
            "actor" => Actor::from_value(data).map(|a| {
                ok_outcome(
                    name(json::canonical_payload(&a.dump())),
                    name(bettermemory_core::records::actor_json(&Some(a.clone()))).map(|v| vec![v]),
                )
            }),
            "link" => MemoryLink::from_value(data).map(|l| {
                ok_outcome(
                    name(json::canonical_payload(&l.dump(false))),
                    name(bettermemory_core::records::links_json(
                        std::slice::from_ref(&l),
                    ))
                    .map(|t| vec![PyValue::Str(t)]),
                )
            }),
            _ => unreachable!(),
        };
    r.unwrap_or_else(|e| err_outcome(&e))
}

fn row_outcome(model: &str, row: &Row) -> PyValue {
    let r = match model {
        "memory" => rows::memory(row).map(|m| ok_outcome(name(m.payload_text()), name(m.row()))),
        "tombstone" => {
            rows::tombstone(row).map(|m| ok_outcome(name(m.payload_text()), name(m.row())))
        }
        "episode" => rows::episode(row).map(|m| ok_outcome(name(m.payload_text()), name(m.row()))),
        _ => unreachable!(),
    };
    r.unwrap_or_else(|e| err_outcome(&e))
}

struct Codec {
    types: Vec<String>,
    titles: Vec<(String, String)>,
}

impl Codec {
    /// `given`: the value the edit set, tagged (None for a deletion).
    fn compact(
        &self,
        model: &str,
        outcome: &PyValue,
        field: Option<&str>,
        given: &PyValue,
    ) -> PyValue {
        let parts = list(outcome);
        if text(&parts[0]) == "ok" {
            let head = PyValue::str(&digest(outcome, 8));
            if let (Some(field), PyValue::Str(dump)) = (field, &parts[1]) {
                if let Ok(dumped @ PyValue::Dict(_)) = json::loads(dump) {
                    if let Some(v) = dumped.get(field) {
                        if !same(&tag(v), given) {
                            return PyValue::List(vec![head, file_tag(v)]);
                        }
                    }
                }
            }
            return head;
        }
        let cls = text(&parts[1]);
        if cls != "ValidationError" {
            return PyValue::str(&format!("!{cls}"));
        }
        let items = list(&parts[3])
            .iter()
            .map(|e| {
                let pair = list(e);
                let code = self
                    .types
                    .iter()
                    .position(|t| *t == text(&pair[1]))
                    .map_or(PyValue::str(&format!("?{}", text(&pair[1]))), |i| {
                        PyValue::int(i as i128)
                    });
                let at_field = field.is_some_and(|f| {
                    list(&pair[0]).len() == 1 && list(&pair[0])[0] == PyValue::str(f)
                });
                if at_field {
                    code
                } else {
                    PyValue::List(vec![pair[0].clone(), code])
                }
            })
            .collect();
        let title = text(&parts[2]);
        let own = self
            .titles
            .iter()
            .find(|(m, _)| m == model)
            .map(|(_, t)| t.clone());
        if own.as_deref() == Some(title.as_str()) {
            PyValue::Dict(vec![(PyValue::str("e"), PyValue::List(items))])
        } else {
            PyValue::Dict(vec![
                (PyValue::str("t"), PyValue::str(&title)),
                (PyValue::str("e"), PyValue::List(items)),
            ])
        }
    }
}

fn codec(header: &PyValue) -> Codec {
    let PyValue::Dict(titles) = get(header, "titles") else {
        panic!("titles")
    };
    Codec {
        types: list(get(header, "types")).iter().map(text).collect(),
        titles: titles.iter().map(|(k, v)| (text(k), text(v))).collect(),
    }
}

fn apply_payload(base: &PyValue, ops: &[(String, Option<PyValue>)]) -> PyValue {
    let mut data = base.clone();
    for (field, value) in ops {
        if field == "$root" {
            data = value.clone().expect("a root value");
            continue;
        }
        let PyValue::Dict(items) = &mut data else {
            continue;
        };
        let key = PyValue::str(field);
        match value {
            None => items.retain(|(k, _)| *k != key),
            Some(v) => match items.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = v.clone(),
                None => items.push((key, v.clone())),
            },
        }
    }
    data
}

fn apply_row(base: &Row, ops: &[(String, Option<SqlValue>)]) -> Row {
    let mut row = base.clone();
    for (column, value) in ops {
        match value {
            None => row.columns.retain(|(k, _)| k != column),
            Some(v) => match row.columns.iter_mut().find(|(k, _)| k == column) {
                Some(slot) => slot.1 = v.clone(),
                None => row.columns.push((column.clone(), v.clone())),
            },
        }
    }
    row
}

/// An edit list as written: `[field, index]`, `[field, {"v": value}]` or
/// `[field]`.
fn ops_of<T>(ops: &PyValue, pool: &[T], literal: impl Fn(&PyValue) -> T) -> Vec<(String, Option<T>)>
where
    T: Clone,
{
    list(ops)
        .iter()
        .map(|op| {
            let op = list(op);
            let field = text(&op[0]);
            let value = op.get(1).map(|v| match v {
                PyValue::Int(_) => pool[int(v) as usize].clone(),
                other => literal(get(other, "v")),
            });
            (field, value)
        })
        .collect()
}

struct Lcg(u64);

impl Lcg {
    fn draw(&mut self, mult: u64, inc: u64) -> u64 {
        self.0 = self.0.wrapping_mul(mult).wrapping_add(inc);
        self.0 >> 33
    }
}

fn battery_ops(
    fields: usize,
    pool: usize,
    n: usize,
    seed: u64,
    lcg: (u64, u64),
) -> Vec<Vec<(usize, usize)>> {
    let mut s = Lcg(seed);
    (0..n)
        .map(|_| {
            let k = 1 + s.draw(lcg.0, lcg.1) % 3;
            (0..k)
                .map(|_| {
                    let f = (s.draw(lcg.0, lcg.1) % fields as u64) as usize;
                    let v = (s.draw(lcg.0, lcg.1) % (pool as u64 + 1)) as usize;
                    (f, v)
                })
                .collect()
        })
        .collect()
}

fn lcg_of(header: &PyValue) -> (u64, u64) {
    let l = list(get(header, "lcg"));
    (int(&l[0]) as u64, int(&l[1]) as u64)
}

#[derive(Default)]
struct Tally {
    checked: usize,
    failed: Vec<String>,
}

impl Tally {
    fn check(&mut self, what: impl FnOnce() -> String, ok: bool) {
        self.checked += 1;
        if !ok {
            self.failed.push(what());
        }
    }
}

#[test]
fn golden_models_payload_form() {
    let file = lines("models.jsonl");
    let header = &file[0];
    let codec = codec(header);
    let pool: Vec<PyValue> = list(get(header, "pool")).iter().map(untag).collect();
    let lcg = lcg_of(header);
    let base = |m: &str| untag(get(get(header, "bases"), m));
    let mut tally = Tally::default();
    let mut grid = 0;
    let mut hand = 0;
    let mut full = 0;
    let mut battery = 0;
    for line in &file[1..] {
        if let PyValue::Dict(_) = line {
            let model = text(get(line, "battery"));
            let n = int(get(line, "n")) as usize;
            let seed = int(get(line, "seed")) as u64;
            let digests = text(get(line, "digests"));
            let fields: Vec<String> = list(get(get(header, "fields"), &model))
                .iter()
                .map(text)
                .collect();
            let base = base(&model);
            for (i, ops) in battery_ops(fields.len(), pool.len(), n, seed, lcg)
                .into_iter()
                .enumerate()
            {
                let ops: Vec<(String, Option<PyValue>)> = ops
                    .into_iter()
                    .map(|(f, v)| (fields[f].clone(), pool.get(v).cloned()))
                    .collect();
                let data = apply_payload(&base, &ops);
                let got = digest(&payload_outcome(&model, &data), 6);
                let want = &digests[i * 6..i * 6 + 6];
                tally.check(
                    || {
                        format!(
                            "battery {model} #{i} {ops:?}: {:?}",
                            payload_outcome(&model, &data)
                        )
                    },
                    got == want,
                );
                battery += 1;
            }
            continue;
        }
        let parts = list(line);
        let model = text(&parts[0]);
        let base = base(&model);
        match &parts[1] {
            PyValue::Str(field) => {
                let field = unicode::to_string(field).expect("field");
                let outcomes = list(&parts[2]);
                for (i, want) in outcomes.iter().enumerate() {
                    let op = (field.clone(), pool.get(i).cloned());
                    let data = apply_payload(&base, std::slice::from_ref(&op));
                    let given = tag(op.1.as_ref().unwrap_or(&PyValue::None));
                    let got = codec.compact(
                        &model,
                        &payload_outcome(&model, &data),
                        Some(&field),
                        &given,
                    );
                    tally.check(
                        || {
                            format!(
                                "{model}.{field}[{i}] = {:?}: got {got:?}, want {want:?}",
                                op.1
                            )
                        },
                        same(&got, want),
                    );
                    grid += 1;
                }
            }
            ops_value => {
                let ops = ops_of(ops_value, &pool, untag);
                let data = apply_payload(&base, &ops);
                let outcome = payload_outcome(&model, &data);
                let want = &parts[2];
                if let PyValue::List(w) = want {
                    if w.first() == Some(&PyValue::str("full")) {
                        let got = tag(&outcome);
                        tally.check(
                            || format!("{model} full {ops:?}: got {got:?}, want {:?}", w[1]),
                            same(&got, &w[1]),
                        );
                        full += 1;
                        continue;
                    }
                }
                let got = codec.compact(&model, &outcome, None, &PyValue::None);
                tally.check(
                    || format!("{model} {ops:?}: got {got:?}, want {want:?}"),
                    same(&got, want),
                );
                hand += 1;
            }
        }
    }
    Ledger::new("golden_models_payload_form", "models.jsonl").settle(tally.checked, &tally.failed);
    assert_eq!((grid, hand, full, battery), (8_302, 37, 12, 12_000));
    let _ = MODELS;
}

#[test]
fn golden_models_row_form() {
    let file = lines("models_rows.jsonl");
    let header = &file[0];
    let codec = codec(header);
    let pool: Vec<SqlValue> = list(get(header, "pool")).iter().map(sql).collect();
    let lcg = lcg_of(header);
    let base = |m: &str| {
        let PyValue::Dict(items) = get(get(header, "bases"), m) else {
            panic!("base")
        };
        Row {
            columns: items.iter().map(|(k, v)| (text(k), sql(v))).collect(),
        }
    };
    let mut tally = Tally::default();
    let (mut grid, mut hand, mut full, mut battery) = (0, 0, 0, 0);
    for line in &file[1..] {
        if let PyValue::Dict(_) = line {
            let model = text(get(line, "battery"));
            let n = int(get(line, "n")) as usize;
            let seed = int(get(line, "seed")) as u64;
            let digests = text(get(line, "digests"));
            let fields: Vec<String> = list(get(get(header, "fields"), &model))
                .iter()
                .map(text)
                .collect();
            let base = base(&model);
            for (i, ops) in battery_ops(fields.len(), pool.len(), n, seed, lcg)
                .into_iter()
                .enumerate()
            {
                let ops: Vec<(String, Option<SqlValue>)> = ops
                    .into_iter()
                    .map(|(f, v)| {
                        let column = fields[f].clone();
                        let value = match pool.get(v) {
                            Some(v) => Some(v.clone()),
                            None if column == "attestation_payload" => None,
                            None => Some(SqlValue::Null),
                        };
                        (column, value)
                    })
                    .collect();
                let row = apply_row(&base, &ops);
                let got = digest(&row_outcome(&model, &row), 6);
                let want = &digests[i * 6..i * 6 + 6];
                tally.check(
                    || {
                        format!(
                            "row battery {model} #{i} {ops:?}: {:?}",
                            row_outcome(&model, &row)
                        )
                    },
                    got == want,
                );
                battery += 1;
            }
            continue;
        }
        let parts = list(line);
        let model = text(&parts[0]);
        let base = base(&model);
        match &parts[1] {
            PyValue::Str(column) => {
                let column = unicode::to_string(column).expect("column");
                for (i, want) in list(&parts[2]).iter().enumerate() {
                    let op = (column.clone(), Some(pool[i].clone()));
                    let row = apply_row(&base, std::slice::from_ref(&op));
                    let given = sql_tagged(&pool[i]);
                    let got =
                        codec.compact(&model, &row_outcome(&model, &row), Some(&column), &given);
                    tally.check(
                        || {
                            format!(
                                "row {model}.{column}[{i}] = {:?}: got {got:?}, want {want:?}",
                                op.1
                            )
                        },
                        same(&got, want),
                    );
                    grid += 1;
                }
            }
            ops_value => {
                let ops = ops_of(ops_value, &pool, sql);
                let row = apply_row(&base, &ops);
                let outcome = row_outcome(&model, &row);
                let want = &parts[2];
                if let PyValue::List(w) = want {
                    if w.first() == Some(&PyValue::str("full")) {
                        let got = tag(&outcome);
                        tally.check(
                            || format!("row {model} full: got {got:?}, want {:?}", w[1]),
                            same(&got, &w[1]),
                        );
                        full += 1;
                        continue;
                    }
                }
                let got = codec.compact(&model, &outcome, None, &PyValue::None);
                tally.check(
                    || format!("row {model} {ops:?}: got {got:?}, want {want:?}"),
                    same(&got, want),
                );
                hand += 1;
            }
        }
    }
    Ledger::new("golden_models_row_form", "models_rows.jsonl").settle(tally.checked, &tally.failed);
    assert_eq!((grid, hand, full, battery), (4_316, 15, 3, 6_000));
}

/// The digests of a table's rows read natively: `[id, digest]`, or
/// `"!sql"` when SQLite will not read the table.
fn table_digests(
    conn: &rusqlite::Connection,
    select: &str,
    read: impl Fn(&Row) -> PyValue,
) -> PyValue {
    match bettermemory_core::records::stored::select_rows(conn, select) {
        Err(_) => PyValue::str("!sql"),
        Ok(rows) => PyValue::List(
            rows.iter()
                .map(|row| {
                    let id = match row.get("id") {
                        Some(SqlValue::Text(t)) => PyValue::str(t),
                        _ => PyValue::None,
                    };
                    PyValue::List(vec![id, PyValue::str(&digest(&read(row), 16))])
                })
                .collect(),
        ),
    }
}

fn store_digests(path: &std::path::Path) -> Vec<(&'static str, PyValue)> {
    use bettermemory_core::records::stored::{EPISODE_SELECT, MEMORY_SELECT, TOMBSTONE_SELECT};
    let conn =
        rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .expect("open");
    vec![
        (
            "memories",
            table_digests(&conn, MEMORY_SELECT, |r| row_outcome("memory", r)),
        ),
        (
            "tombstones",
            table_digests(&conn, TOMBSTONE_SELECT, |r| row_outcome("tombstone", r)),
        ),
        (
            "episodes",
            table_digests(&conn, EPISODE_SELECT, |r| row_outcome("episode", r)),
        ),
    ]
}

/// Every record row of the store fixtures (each `store_files.jsonl` case
/// rebuilt, and `store_real.sqlite`) read into a record natively and
/// compared by digest with the reference's reading in
/// `models_stores.jsonl`; and the same through `StoreFile`'s record
/// readers for the real store.
#[test]
fn golden_models_stores() {
    let file = lines("models_stores.jsonl");
    let mut ledger = Ledger::new("golden_models_stores", "models_stores.jsonl");
    let (_, cases) = common::stores::cases();
    let mut rows_checked = 0;
    let mut stores_checked = 0;
    for line in &file[1..] {
        let name = text(get(line, "case"));
        let dir = common::stores::scratch("models");
        let path = if name == "store_real.sqlite" {
            let path = dir.0.join("memory.sqlite");
            std::fs::copy(golden_path("store_real.sqlite"), &path).expect("copy");
            path
        } else {
            let case = cases
                .iter()
                .find(|c| c["case"] == name.as_str())
                .unwrap_or_else(|| panic!("no case {name}"));
            common::stores::materialize(case, &dir.0);
            dir.0.join("memory.sqlite")
        };
        ledger.case(&name, || {
            for (table, got) in store_digests(&path) {
                let want = get(line, table);
                assert!(
                    same(&got, want),
                    "{name}.{table}: got {got:?}, want {want:?}"
                );
                if let PyValue::List(items) = want {
                    rows_checked += items.len();
                }
            }
            if name == "store_real.sqlite" {
                let store = bettermemory_core::store::StoreFile::open(&path).expect("admitted");
                let memories = store.memory_records().expect("memories");
                let want = list(get(line, "memories"));
                assert_eq!(memories.len(), want.len());
                for ((row, record), want) in memories.iter().zip(want) {
                    let outcome = match record {
                        Ok(m) => ok_outcome(name_of(m.payload_text()), name_of(m.row())),
                        Err(e) => err_outcome(e),
                    };
                    assert_eq!(digest(&outcome, 16), text(&list(want)[1]), "{row:?}");
                    let m = record.as_ref().expect("a valid memory");
                    let ranked = m.to_search_record().expect("a search record");
                    assert_eq!(ranked.id, text(&list(want)[0]));
                }
                assert_eq!(
                    store.tombstone_records().expect("tombstones").len(),
                    list(get(line, "tombstones")).len()
                );
                assert_eq!(
                    store.episode_records().expect("episodes").len(),
                    list(get(line, "episodes")).len()
                );
            }
        });
        stores_checked += 1;
    }
    ledger.finish(file.len() - 1);
    assert_eq!((stores_checked, rows_checked), (41, 231));
}

fn name_of<T>(r: Result<T, bettermemory_core::pyvalue::PyError>) -> Result<T, &'static str> {
    r.map_err(|e| e.name())
}
