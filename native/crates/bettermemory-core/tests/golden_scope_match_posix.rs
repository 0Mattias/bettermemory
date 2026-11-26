//! Every case of `scope_match_posix.jsonl`,
//! `scope_match_posix_python.jsonl`, `scope_match_unicode.jsonl` and
//! `scope_match_volume.jsonl` against `scope_match`: the leaves that
//! read the separators and the home. In the first two the home is the
//! row's and the probe a table (the row's `folds`), and every path probed
//! is compared too; the sweep's draws are made again by the generator's
//! rule and compared by count and digest. The third is built again under
//! a scratch folder on this machine's disk, `@` standing for it, and
//! probed for real ([`volume_probe`] over `StdFs`): its answers are the
//! volume's, and it is gated on the platform.
//!
//! The fixtures follow `os.sep`, `os.altsep` and `PosixPath`, so they
//! exist on POSIX only (`scope_match_not_posix`).

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::StdFs;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::scope_match::{
    boundary_after, declared_root_covers, declared_root_spans, drop_ancestor_roots,
    find_root_occurrence, folded_home, home_alias, ignorecase_starts, ignorecase_starts_by_engine,
    roots_of, trim_evidence, volume_probe, Probe, ScopeError, EVIDENCE_PADDING,
};
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// A text of the fixture: a string, or `{"cps": [...]}`.
fn s(v: &Value) -> PyStr {
    match v {
        Value::String(t) => t.chars().map(u32::from).collect(),
        Value::Object(o) => o["cps"]
            .as_array()
            .expect("cps")
            .iter()
            .map(|c| u32::try_from(c.as_u64().expect("code point")).expect("u32"))
            .collect(),
        other => panic!("not a text: {other}"),
    }
}

/// The generator's `_s`.
fn text(t: &[u32]) -> Value {
    let plain: Option<String> = t.iter().map(|&c| char::from_u32(c)).collect();
    match plain {
        Some(p) if !p.contains("\\\\") => Value::String(p),
        _ => json!({ "cps": t }),
    }
}

fn texts(v: &Value) -> Vec<PyStr> {
    v.as_array().expect("texts").iter().map(s).collect()
}

fn pairs(v: &Value) -> Vec<(PyStr, PyStr)> {
    v.as_array()
        .expect("pairs")
        .iter()
        .map(|p| (s(&p[0]), s(&p[1])))
        .collect()
}

fn pairs_out(p: &[(PyStr, PyStr)]) -> Value {
    Value::Array(p.iter().map(|(a, b)| json!([text(a), text(b)])).collect())
}

fn idx(v: &Value) -> usize {
    usize::try_from(v.as_u64().expect("index")).expect("usize")
}

fn home_of(row: &Value) -> Option<PyStr> {
    match &row["home"] {
        Value::Null => None,
        v => Some(s(v)),
    }
}

fn raised(e: &ScopeError) -> Value {
    match e {
        ScopeError::Home(_) => json!({"raise": "RuntimeError"}),
        ScopeError::Index => json!({"raise": "IndexError"}),
        other => panic!("not an answer of the reference: {other:?}"),
    }
}

fn answer<T>(got: Result<T, ScopeError>, ok: impl FnOnce(T) -> Value) -> Value {
    match got {
        Ok(v) => ok(v),
        Err(e) => raised(&e),
    }
}

/// `by` as `_roots_of` gets it: `setdefault(scope, Counter())[cwd] += n`.
fn by_scope(v: &Value) -> Vec<(PyStr, Vec<(PyStr, u64)>)> {
    let mut by: Vec<(PyStr, Vec<(PyStr, u64)>)> = Vec::new();
    for entry in v.as_array().expect("by") {
        let scope = s(&entry[0]);
        let at = match by.iter().position(|(k, _)| *k == scope) {
            Some(at) => at,
            None => {
                by.push((scope, Vec::new()));
                by.len() - 1
            }
        };
        for count in entry[1].as_array().expect("counts") {
            let (cwd, n) = (s(&count[0]), count[1].as_u64().expect("count"));
            let counts = &mut by[at].1;
            match counts.iter_mut().find(|(k, _)| *k == cwd) {
                Some(c) => c.1 += n,
                None => counts.push((cwd, n)),
            }
        }
    }
    by
}

/// Runs `f` with the table probe: True for the paths in `folds`; answers
/// `f`'s value and every path probed, in order.
fn tabled<T>(folds: &[PyStr], f: impl FnOnce(&mut Probe<'_>) -> T) -> (T, Vec<PyStr>) {
    let mut log = Vec::new();
    let got = {
        let mut probe = |p: &[u32]| {
            log.push(p.to_vec());
            Ok::<bool, ScopeError>(folds.iter().any(|x| x == p))
        };
        f(&mut probe)
    };
    (got, log)
}

fn spans_out(spans: Vec<(usize, usize)>) -> Value {
    json!(spans)
}

/// One row of the two tabled files: its answer and the paths probed (None
/// where the row records none).
fn tabled_row(row: &Value) -> (Value, Option<Vec<PyStr>>) {
    let home = home_of(row);
    let home = home.as_deref();
    let folds = row.get("folds").map(texts).unwrap_or_default();
    match row["f"].as_str().expect("f") {
        "home" => (answer(folded_home(home), |h| text(&h)), None),
        "boundary" => (
            json!(boundary_after(&s(&row["body"]), idx(&row["end"]))),
            None,
        ),
        "occurrence" => (
            json!(find_root_occurrence(&s(&row["body"]), &s(&row["needle"]))),
            None,
        ),
        "trim" => (
            text(&trim_evidence(
                &s(&row["body"]),
                idx(&row["start"]),
                idx(&row["end"]),
                EVIDENCE_PADDING,
            )),
            None,
        ),
        "drop" => (
            answer(drop_ancestor_roots(pairs(&row["roots"])), |r| pairs_out(&r)),
            None,
        ),
        "roots" => {
            let by = by_scope(&row["by"]);
            let (got, log) = tabled(&folds, |p| roots_of(&by, home, p));
            (answer(got, |r| pairs_out(&r)), Some(log))
        }
        "alias" => {
            let path = s(&row["path"]);
            let (got, log) = tabled(&folds, |p| home_alias(&path, home, p));
            (
                answer(got, |a| a.map_or(Value::Null, |a| text(&a))),
                Some(log),
            )
        }
        "spans" => {
            let (body, declared, roots) = (
                s(&row["body"]),
                texts(&row["declared"]),
                pairs(&row["roots"]),
            );
            let (got, mut log) = tabled(&folds, |p| {
                declared_root_spans(&body, &declared, &roots, home, p)
            });
            log.sort();
            (answer(got, spans_out), Some(log))
        }
        "covers" => {
            let (body, root, declared) =
                (s(&row["body"]), s(&row["root"]), texts(&row["declared"]));
            let contracted = row["contracted"].as_bool().expect("contracted");
            let (got, log) = tabled(&folds, |p| {
                declared_root_covers(
                    &body,
                    idx(&row["idx"]),
                    &root,
                    &declared,
                    contracted,
                    home,
                    p,
                )
            });
            (answer(got, |c| json!(c)), Some(log))
        }
        other => panic!("unknown case {other}"),
    }
}

fn every_tabled_case(test: &str, file: &str) {
    let rows = common::jsonl(file);
    let cases = idx(&rows[0]["cases"]);
    assert!(
        cases > 0 && cases == rows.len() - 1,
        "{file}: {cases} cases"
    );
    let mut ledger = Ledger::new(test, file);
    for (i, row) in rows[1..].iter().enumerate() {
        if row["f"] == "sweep" {
            let seed = row["seed"].as_u64().expect("seed");
            let count = idx(&row["count"]);
            let (n, digest) = sweep(seed, count);
            ledger.case(format!("line {} (the sweep)", i + 2), || {
                assert_eq!(n, count);
                assert_eq!(digest, row["sha256"].as_str().expect("sha256"), "the sweep");
            });
            continue;
        }
        let (got, probes) = tabled_row(row);
        ledger.case(format!("line {}", i + 2), || {
            assert_eq!(got, row["r"], "{row}");
            if let Some(probes) = probes {
                let probes: Vec<Value> = probes.iter().map(|p| text(p)).collect();
                assert_eq!(Value::Array(probes), row["probes"], "probes of {row}");
            }
        });
    }
    ledger.finish(cases);
}

#[test]
fn scope_match_posix_every_case() {
    every_tabled_case("scope_match_posix_every_case", "scope_match_posix.jsonl");
}

#[test]
fn scope_match_posix_python_every_case() {
    every_tabled_case(
        "scope_match_posix_python_every_case",
        "scope_match_posix_python.jsonl",
    );
}

#[test]
fn scope_match_unicode_every_case() {
    every_tabled_case(
        "scope_match_unicode_every_case",
        "scope_match_unicode.jsonl",
    );
}

// -- the sweep (the generator's `_sweep`) -------------------------------------

const SWEEP_HOME: &str = "/h";
const BODY_PIECES: [&str; 16] = [
    "/h", "/a", "/A", "/b", "~", "/", "a", "B", "x", "-", "_", ".", " ", "\n", "/h/a", "/H",
];
const ROOT_PIECES: [&str; 8] = ["/a", "/A", "/b", "/h", "/H", "x", "-", "."];

/// `_Rng`: xorshift64*.
struct Rng(u64);

impl Rng {
    fn next(&mut self, bound: u64) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D) % bound
    }

    fn pick(&mut self, pieces: &[&str]) -> String {
        pieces[self.next(pieces.len() as u64) as usize].to_owned()
    }
}

fn cps(t: &str) -> PyStr {
    t.chars().map(u32::from).collect()
}

fn ascii(t: &[u32]) -> String {
    t.iter()
        .map(|&c| char::from_u32(c).expect("ascii"))
        .collect()
}

/// `_draw`'s draws in its order: the roots, the body and the two masks.
fn draw(rng: &mut Rng) -> (Vec<String>, String, u64, u64) {
    let n = rng.next(4) + 1;
    let mut roots = Vec::new();
    for _ in 0..n {
        let k = rng.next(3) + 1;
        roots.push((0..k).map(|_| rng.pick(&ROOT_PIECES)).collect::<String>());
    }
    let len = rng.next(24);
    let body: String = (0..len).map(|_| rng.pick(&BODY_PIECES)).collect();
    let declared_mask = rng.next(1 << n);
    let folds_mask = rng.next(1 << (n + 1));
    (roots, body, declared_mask, folds_mask)
}

fn sweep_line(rng: &mut Rng) -> String {
    let (roots, body, declared_mask, folds_mask) = draw(rng);
    let n = roots.len() as u64;
    let declared: Vec<usize> = (0..n as usize)
        .filter(|i| declared_mask >> i & 1 == 1)
        .collect();
    let mut folds: Vec<PyStr> = (0..n as usize)
        .filter(|i| folds_mask >> i & 1 == 1)
        .map(|i| cps(&roots[i]))
        .collect();
    if folds_mask >> n & 1 == 1 {
        folds.push(cps(SWEEP_HOME));
    }
    let home = cps(SWEEP_HOME);
    let home = Some(home.as_slice());
    let body = cps(&body);
    let project_roots: Vec<(PyStr, PyStr)> = roots
        .iter()
        .enumerate()
        .map(|(i, r)| (cps(&format!("s{i}")), cps(r)))
        .collect();
    let scopes: Vec<PyStr> = declared.iter().map(|i| cps(&format!("s{i}"))).collect();
    let declared_roots: Vec<PyStr> = declared.iter().map(|&i| cps(&roots[i])).collect();
    let (spans, mut span_probes) = tabled(&folds, |p| {
        declared_root_spans(&body, &scopes, &project_roots, home, p).expect("spans")
    });
    span_probes.sort();
    let mut per_root = Vec::new();
    for root in &roots {
        let root = cps(root);
        let (got, probes) = tabled(&folds, |p| {
            let occ = find_root_occurrence(&body, &root);
            let cov = occ.map(|(i, _)| {
                declared_root_covers(&body, i, &root, &declared_roots, false, home, p)
                    .expect("covers")
            });
            let alias = home_alias(&root, home, p).expect("alias");
            let (mut occ2, mut cov2) = (None, None);
            if let Some(a) = &alias {
                occ2 = find_root_occurrence(&body, a);
                cov2 = occ2.map(|(i, _)| {
                    declared_root_covers(&body, i, &root, &declared_roots, true, home, p)
                        .expect("covers")
                });
            }
            json!([occ, cov, alias.map(|a| ascii(&a)), occ2, cov2])
        });
        let mut got = got.as_array().expect("row").clone();
        got.push(json!(probes.iter().map(|p| ascii(p)).collect::<Vec<_>>()));
        per_root.push(Value::Array(got));
    }
    let span_probes: Vec<String> = span_probes.iter().map(|p| ascii(p)).collect();
    serde_json::to_string(&json!([spans, span_probes, per_root])).expect("json")
}

fn sweep(seed: u64, count: usize) -> (usize, String) {
    let mut rng = Rng(seed);
    let mut digest = Sha256::new();
    let mut n = 0;
    for _ in 0..count {
        digest.update(sweep_line(&mut rng).as_bytes());
        digest.update(b"\n");
        n += 1;
    }
    (n, common::hex(&digest.finalize()))
}

/// The spans' walk over a literal prefix answers as the engine searching
/// from each start, on every draw of the sweep: each root, the root behind
/// `~`, and each root repeated (a needle that overlaps itself), in the
/// body, in the body twice over and in the body less its first code point.
/// The draws are the generator's, from the seed and count of the sweep's
/// row.
#[test]
fn the_prefix_walk_answers_as_the_engine_over_the_sweep() {
    let rows = common::jsonl("scope_match_posix.jsonl");
    let row = rows
        .iter()
        .find(|row| row["f"] == "sweep")
        .expect("the sweep's row");
    let count = idx(&row["count"]);
    let mut rng = Rng(row["seed"].as_u64().expect("seed"));
    let mut compared = 0usize;
    for _ in 0..count {
        let (roots, body, _, _) = draw(&mut rng);
        let body = cps(&body);
        let mut twice = body.clone();
        twice.extend_from_slice(&body);
        let hays = [body.clone(), twice, body.get(1..).unwrap_or(&[]).to_vec()];
        for root in &roots {
            let root = cps(root);
            let mut alias = vec![u32::from(b'~')];
            alias.extend_from_slice(&root);
            let mut double = root.clone();
            double.extend_from_slice(&root);
            for needle in [&root, &alias, &double] {
                for hay in &hays {
                    assert_eq!(
                        ignorecase_starts(hay, needle),
                        ignorecase_starts_by_engine(hay, needle),
                        "{needle:?} in {hay:?}"
                    );
                    compared += 1;
                }
            }
        }
    }
    assert!(compared >= count * 9, "{compared}");
}

// -- the volume's own probe ---------------------------------------------------

const VOLUME_DIRS: [&str; 5] = ["Base", "Base/Work", "Base/Work/Sub", "Proj", "Proj/Child"];

/// A scratch folder holding the fixture's tree, removed on drop.
struct Scratch(std::path::PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn place(v: &Value, at: &str) -> PyStr {
    cps(&ascii(&s(v)).replace('@', at))
}

fn back(t: &[u32], at: &str) -> Value {
    let spelled: String = t
        .iter()
        .map(|&c| char::from_u32(c).expect("char"))
        .collect();
    text(&cps(&spelled.replace(at, "@")))
}

#[test]
fn scope_match_volume_every_case() {
    let file = "scope_match_volume.jsonl";
    let rows = common::jsonl(file);
    let cases = idx(&rows[0]["cases"]);
    assert!(
        cases > 0 && cases == rows.len() - 1,
        "{file}: {cases} cases"
    );
    let made = std::env::temp_dir().join(format!("scope-match-volume-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&made);
    std::fs::create_dir_all(&made).expect("scratch");
    let scratch = Scratch(std::fs::canonicalize(&made).expect("canonical scratch"));
    for d in VOLUME_DIRS {
        std::fs::create_dir_all(scratch.0.join(d)).expect("dir");
    }
    let at = scratch
        .0
        .to_str()
        .expect("an ASCII scratch folder")
        .to_owned();
    let home = cps(&format!("{at}/Base"));
    let home = Some(home.as_slice());
    let fs = StdFs;
    let mut probe = volume_probe(&fs);
    let mut ledger = Ledger::new("scope_match_volume_every_case", file);
    let folds = probe(&cps(&format!("{at}/Proj"))).expect("probe");
    ledger.case("the volume", || assert_eq!(json!(folds), rows[0]["folds"]));
    for (i, row) in rows[1..].iter().enumerate() {
        let got = match row["f"].as_str().expect("f") {
            "roots" => {
                let mut by = by_scope(&row["by"]);
                for (_, counts) in &mut by {
                    for (cwd, _) in counts.iter_mut() {
                        *cwd = cps(&ascii(cwd).replace('@', &at));
                    }
                }
                let got = roots_of(&by, home, &mut probe).expect("roots");
                Value::Array(
                    got.iter()
                        .map(|(sc, r)| json!([text(sc), back(r, &at)]))
                        .collect(),
                )
            }
            "alias" => {
                match home_alias(&place(&row["path"], &at), home, &mut probe).expect("alias") {
                    Some(a) => back(&a, &at),
                    None => Value::Null,
                }
            }
            "spans" => {
                let body = place(&row["body"], &at);
                let roots: Vec<(PyStr, PyStr)> = pairs(&row["roots"])
                    .into_iter()
                    .map(|(sc, r)| (sc, cps(&ascii(&r).replace('@', &at))))
                    .collect();
                let spans =
                    declared_root_spans(&body, &texts(&row["declared"]), &roots, home, &mut probe)
                        .expect("spans");
                let unplaced = |p: usize| ascii(&body[..p]).replace(&at, "@").chars().count();
                json!(spans
                    .iter()
                    .map(|&(a, b)| [unplaced(a), unplaced(b)])
                    .collect::<Vec<_>>())
            }
            "covers" => {
                let declared: Vec<PyStr> = row["declared"]
                    .as_array()
                    .expect("declared")
                    .iter()
                    .map(|d| place(d, &at))
                    .collect();
                json!(declared_root_covers(
                    &place(&row["body"], &at),
                    idx(&row["idx"]),
                    &place(&row["root"], &at),
                    &declared,
                    row["contracted"].as_bool().expect("contracted"),
                    home,
                    &mut probe,
                )
                .expect("covers"))
            }
            other => panic!("unknown case {other}"),
        };
        ledger.case(format!("line {}", i + 2), || {
            assert_eq!(got, row["r"], "{row}");
        });
    }
    ledger.finish(cases + 1);
}
