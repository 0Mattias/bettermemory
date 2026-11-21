//! Every case of `membership_records_records.jsonl` (not gated) against
//! `membership_records::records`: the manifest's JSON and match, the
//! checkout's token rules, its views, `_live_key` and `_found_token`. The
//! spelling of each row and the sweep's rule are the generator's
//! (`tests/contract/gen_membership_records.py`, units 2 and 3).

mod common;

use bettermemory_core::capture::location::{to_location, ObjectKey};
use bettermemory_core::capture::{Capture, Manifest, Stamp, State};
use bettermemory_core::json;
use bettermemory_core::membership_records::records::{
    found_token, live_key, manifest_from_json, manifest_match, manifest_to_json, Checkout, View,
};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use sha2::{Digest, Sha256};

const FILE: &str = "membership_records_records.jsonl";

fn rows() -> Vec<PyValue> {
    let text = std::fs::read_to_string(common::golden_path(FILE))
        .unwrap_or_else(|e| panic!("cannot read golden file {FILE}: {e}"));
    text.lines()
        .map(|l| json::loads(&unicode::cps(l)).expect("a JSON line"))
        .collect()
}

fn list(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(items) => items,
        other => panic!("not a list: {other:?}"),
    }
}

fn field<'a>(v: &'a PyValue, k: &str) -> &'a PyValue {
    v.get(k).unwrap_or_else(|| panic!("no field {k}"))
}

fn text(v: &PyValue) -> PyStr {
    match v {
        PyValue::Str(s) => s.clone(),
        other => panic!("not a str: {other:?}"),
    }
}

fn opt_text(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::None => None,
        other => Some(text(other)),
    }
}

fn int(v: &PyValue) -> i64 {
    match v {
        PyValue::Int(t) => t.parse().expect("an i64"),
        other => panic!("not an int: {other:?}"),
    }
}

fn key(v: &PyValue) -> Option<ObjectKey> {
    match v {
        PyValue::None => None,
        PyValue::List(k) => Some(ObjectKey {
            device: text(&k[0]),
            inode: text(&k[1]),
            birth: opt_text(&k[2]),
        }),
        other => panic!("not a key: {other:?}"),
    }
}

fn cps(s: &str) -> PyStr {
    unicode::cps(s)
}

/// A checkout with every field the rows do not name at a fixed value.
fn checkout(token: Option<PyStr>, status: PyStr, object_key: Option<ObjectKey>) -> Checkout {
    Checkout {
        id: cps("k1"),
        project: cps("p1"),
        machine: cps("m1"),
        kind: cps("primary"),
        object_key,
        common_object_key: None,
        git_dir: Some(cps("/r/.git")),
        token,
        token_status: status,
        stamps: Vec::new(),
        last_head: Some(cps("h1")),
        branch: Some(cps("main")),
        remotes: Vec::new(),
        manifest: None,
        legacy: false,
        retired: false,
        registered: cps("t0"),
        views: Vec::new(),
        sessions: Vec::new(),
        replaced_by: None,
        ticks: None,
    }
}

fn checkout_of(v: &PyValue) -> Checkout {
    let mut k = checkout(
        opt_text(field(v, "token")),
        text(field(v, "token_status")),
        key(field(v, "object_key")),
    );
    k.id = text(field(v, "id"));
    k.project = text(field(v, "project"));
    k.machine = text(field(v, "machine"));
    k.kind = text(field(v, "kind"));
    k.common_object_key = key(field(v, "common_object_key"));
    k.last_head = opt_text(field(v, "last_head"));
    k.branch = opt_text(field(v, "branch"));
    k.registered = text(field(v, "registered"));
    k.stamps = list(field(v, "stamps"))
        .iter()
        .map(|s| {
            let s = list(s);
            let n = |i: usize| match &s[i] {
                PyValue::Int(t) => t.clone(),
                other => panic!("not an int: {other:?}"),
            };
            Stamp {
                name: text(&s[0]),
                numbers: [n(1), n(2), n(3), n(4)],
            }
        })
        .collect();
    k
}

fn view_of(v: &PyValue) -> View {
    let v = list(v);
    View {
        checkout: text(&v[0]),
        machine: text(&v[1]),
        path: text(&v[2]),
        first_seen: text(&v[3]),
        last_seen: text(&v[4]),
        order: int(&v[5]),
    }
}

fn raised(v: &PyValue) -> Option<String> {
    match v.get("raised") {
        Some(PyValue::Str(s)) => {
            Some(String::from_utf8(s.iter().map(|&c| c as u8).collect()).unwrap())
        }
        _ => None,
    }
}

/// A dict's items in key order, as the file's `sort_keys` spells them.
fn sorted(v: PyValue) -> PyValue {
    let PyValue::Dict(ref items) = v else {
        return v;
    };
    let mut items = items.clone();
    items.sort_by(|a, b| text(&a.0).cmp(&text(&b.0)));
    PyValue::Dict(items)
}

fn bits(x: f64) -> String {
    format!("{:016x}", x.to_bits())
}

fn check_from_json(i: usize, row: &PyValue) {
    let got = manifest_from_json(field(row, "a"));
    let want = field(row, "o");
    match (got, raised(want)) {
        (Err(e), Some(class)) => assert_eq!(e.name(), class, "row {i}"),
        (Ok(None), None) => assert_eq!(*want, PyValue::None, "row {i}"),
        (Ok(Some(m)), None) => assert_eq!(sorted(manifest_to_json(&m)), *want, "row {i}"),
        (got, _) => panic!("row {i}: got {got:?}, want {want:?}"),
    }
}

fn manifest(v: &PyValue) -> Option<Manifest> {
    manifest_from_json(v).expect("a well-formed manifest")
}

fn check_match(i: usize, row: &PyValue) {
    let a = list(field(row, "a"));
    let got = manifest_match(manifest(&a[0]).as_ref(), manifest(&a[1]).as_ref());
    assert_eq!(cps(&bits(got)), text(field(row, "o")), "row {i}");
}

fn check_checkout(i: usize, row: &PyValue) {
    let a = field(row, "a");
    let k = checkout(
        opt_text(field(a, "token")),
        text(field(a, "status")),
        key(&match field(a, "key") {
            PyValue::List(k) if k.len() == 2 => {
                PyValue::List(vec![k[0].clone(), k[1].clone(), PyValue::None])
            }
            other => other.clone(),
        }),
    );
    let o = field(row, "o");
    assert_eq!(
        PyValue::Bool(k.token_superseded()),
        *field(o, "superseded"),
        "row {i}"
    );
    let lineage = k.lineage().map_or(PyValue::None, |l| {
        PyValue::List(vec![
            PyValue::Str(l.token),
            PyValue::Str(l.device),
            PyValue::Str(l.inode),
        ])
    });
    assert_eq!(lineage, *field(o, "lineage"), "row {i}");
    for pair in list(field(o, "refutes")) {
        let pair = list(pair);
        let found = opt_text(&pair[0]);
        let got = k.token_refutes(found.as_deref());
        match (got, raised(&pair[1])) {
            (Ok(b), None) => assert_eq!(PyValue::Bool(b), pair[1], "row {i} {found:?}"),
            (Err(e), Some(class)) => assert_eq!(e.class(), class, "row {i}"),
            (got, _) => panic!("row {i}: got {got:?}, want {:?}", pair[1]),
        }
    }
}

fn check_views(i: usize, row: &PyValue) {
    let mut k = checkout(None, cps("own"), None);
    k.views = list(field(row, "a"))
        .iter()
        .enumerate()
        .map(|(n, v)| {
            let v = list(v);
            View {
                checkout: cps("k1"),
                machine: text(&v[0]),
                path: cps(&format!("/p{n}")),
                first_seen: cps("t0"),
                last_seen: cps("t1"),
                order: int(&v[1]),
            }
        })
        .collect();
    let index = |v: &View| k.views.iter().position(|w| std::ptr::eq(w, v)).unwrap();
    let on: Vec<PyValue> = k
        .views_on(&text(field(row, "m")))
        .into_iter()
        .map(|v| PyValue::int(index(v) as i128))
        .collect();
    let o = field(row, "o");
    assert_eq!(PyValue::List(on), *field(o, "on"), "row {i}");
    let newest = k
        .newest_view()
        .map_or(PyValue::None, |v| PyValue::int(index(v) as i128));
    assert_eq!(newest, *field(o, "newest"), "row {i}");
}

fn check_live_key(i: usize, row: &PyValue) {
    let a = list(field(row, "a"));
    let left = live_key(&checkout_of(&a[0]), &view_of(&a[1]));
    let right = live_key(&checkout_of(&a[2]), &view_of(&a[3]));
    assert_eq!(PyValue::Bool(left == right), *field(row, "o"), "row {i}");
}

fn check_found_token(i: usize, row: &PyValue) {
    let a = list(field(row, "a"));
    let k = checkout(None, text(&a[0]), None);
    let mut cap = Capture::new(cps("/w"), State::Repo);
    cap.token = opt_text(&a[1]);
    cap.token_written = opt_text(&a[2]);
    let loc = to_location(&cap, &cps("m1"), None);
    let got = found_token(&k, &loc).cloned();
    assert_eq!(got, opt_text(field(row, "o")), "row {i}");
}

/// `_Rng`: a 64-bit LCG (Knuth's MMIX constants), its top 32 bits drawn.
struct Rng(u64);

impl Rng {
    fn next(&mut self, bound: u64) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        (self.0 >> 32) % bound
    }
}

fn swept(rng: &mut Rng) -> Option<Manifest> {
    if rng.next(16) == 0 {
        return None;
    }
    let name = if rng.next(8) == 0 {
        cps(["a", "b"][rng.next(2) as usize])
    } else {
        cps("n")
    };
    let n = rng.next(9);
    let entries = (0..n)
        .map(|_| {
            let p = cps(["p", "q", "r", "s"][rng.next(4) as usize]);
            (p, rng.next(3).to_string())
        })
        .collect();
    Some(Manifest { name, entries })
}

fn check_sweep(i: usize, row: &PyValue) {
    let mut rng = Rng(int(field(row, "seed")) as u64);
    let n = int(field(row, "n"));
    let lines: Vec<String> = (0..n)
        .map(|_| {
            let a = swept(&mut rng);
            let b = swept(&mut rng);
            bits(manifest_match(a.as_ref(), b.as_ref()))
        })
        .collect();
    assert!(n > 0, "row {i}: an empty sweep");
    let digest = common::hex(&Sha256::digest(lines.join("\n").as_bytes()));
    assert_eq!(cps(&digest), text(field(row, "o")), "row {i}");
}

#[test]
fn every_case_answers_as_the_reference() {
    let rows = rows();
    let mut checked = 0;
    for (i, row) in rows.iter().enumerate() {
        match String::from_utf8(text(field(row, "f")).iter().map(|&c| c as u8).collect())
            .unwrap()
            .as_str()
        {
            "from_json" => check_from_json(i, row),
            "match" => check_match(i, row),
            "checkout" => check_checkout(i, row),
            "views" => check_views(i, row),
            "live_key" => check_live_key(i, row),
            "found_token" => check_found_token(i, row),
            "match_sweep" => check_sweep(i, row),
            other => panic!("row {i}: unknown function {other}"),
        }
        checked += 1;
    }
    common::assert_count(FILE, checked, rows.len());
}
