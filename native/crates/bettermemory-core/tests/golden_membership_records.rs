//! Every case of `membership_records_paths.jsonl` (not gated) and
//! `membership_records_paths_python.jsonl` (gated on the Python release)
//! against the native `membership_records` unit 1. The spelling of texts
//! and outputs, and the sweep's rule, are the generator's
//! (`tests/contract/gen_membership_records.py`).

mod common;

use bettermemory_core::capture::location::ObjectKey;
use bettermemory_core::fsdata::{Flavour, PyErr};
use bettermemory_core::gitfile_rule::hostpath::text::{Process, Raised};
use bettermemory_core::json;
use bettermemory_core::membership_records::{self as mr, paths};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use common::parity::Ledger;
use sha2::{Digest, Sha256};

/// `os.getcwd` answering a fixed path, or raising FileNotFoundError.
pub struct Cwd(pub Option<PyStr>);

impl Process for Cwd {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        self.0.clone().ok_or(PyErr::FileNotFound)
    }
}

fn lines(name: &str) -> Vec<PyValue> {
    let text = std::fs::read_to_string(common::golden_path(name))
        .unwrap_or_else(|e| panic!("cannot read golden file {name}: {e}"));
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

fn int(v: &PyValue) -> i128 {
    match v {
        PyValue::Int(t) => t.parse().expect("an int"),
        other => panic!("not an int: {other:?}"),
    }
}

fn text(v: &PyValue) -> PyStr {
    match v {
        PyValue::Str(t) => t.clone(),
        _ => {
            if let Some(c) = v.get("c") {
                return list(c).iter().map(|x| int(x) as u32).collect();
            }
            let parts = list(v.get("b").expect("a spelled text"));
            let mut out = Vec::new();
            for (i, p) in parts.iter().enumerate() {
                if i > 0 {
                    out.push(u32::from(b'\\'));
                }
                out.extend(text(p));
            }
            out
        }
    }
}

fn opt_text(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::None => None,
        other => Some(text(other)),
    }
}

fn boolean(v: &PyValue) -> bool {
    match v {
        PyValue::Bool(b) => *b,
        other => panic!("not a bool: {other:?}"),
    }
}

fn host(v: &PyValue) -> Flavour {
    if text(v) == unicode::cps("nt") {
        Flavour::Windows
    } else {
        Flavour::Posix
    }
}

fn key(v: &PyValue) -> Option<ObjectKey> {
    match v {
        PyValue::None => None,
        other => {
            let k = list(other);
            Some(ObjectKey {
                device: text(&k[0]),
                inode: text(&k[1]),
                birth: opt_text(&k[2]),
            })
        }
    }
}

/// An answer, as the generator writes it.
#[derive(Debug, PartialEq)]
enum Answer {
    Text(PyStr),
    Bool(bool),
    Null,
    Raised(String, PyStr),
}

fn expected(v: &PyValue) -> Answer {
    match v {
        PyValue::Bool(b) => Answer::Bool(*b),
        PyValue::None => Answer::Null,
        _ => match v.get("raised") {
            Some(r) => {
                let r = list(r);
                Answer::Raised(cls(&r[0]), text(&r[1]))
            }
            None => Answer::Text(text(v)),
        },
    }
}

fn cls(v: &PyValue) -> String {
    text(v).iter().filter_map(|&c| char::from_u32(c)).collect()
}

fn raised(r: Raised) -> Answer {
    match r {
        Raised::Value(t) => Answer::Raised("ValueError".into(), t),
        Raised::Os(e) => Answer::Raised(e.name().into(), Vec::new()),
    }
}

fn flavour_name(f: Flavour) -> PyStr {
    unicode::cps(match f {
        Flavour::Posix => "posix",
        Flavour::Windows => "nt",
    })
}

/// The native answer to one case.
pub(crate) fn answer(f: &str, a: &[PyValue]) -> Answer {
    match f {
        "legacy_asks" => Answer::Bool(mr::legacy_asks(
            &text(&a[0]),
            boolean(&a[1]),
            boolean(&a[2]),
        )),
        "same_number" => Answer::Bool(mr::same_number(key(&a[0]).as_ref(), key(&a[1]).as_ref())),
        "same_path" => Answer::Bool(paths::same_path(&text(&a[0]), &text(&a[1]))),
        "flavour" => Answer::Text(flavour_name(paths::flavour(&text(&a[0])))),
        "basename" => Answer::Text(paths::basename(&text(&a[0]))),
        "path_under" => {
            let parts: Vec<PyStr> = list(&a[1]).iter().map(text).collect();
            let refs: Vec<&[u32]> = parts.iter().map(Vec::as_slice).collect();
            Answer::Text(paths::path_under(&text(&a[0]), &refs))
        }
        "rebase" => {
            let cwd = Cwd(opt_text(&a[3]));
            match paths::rebase(&text(&a[0]), &text(&a[1]), &text(&a[2]), &cwd) {
                Ok(t) => Answer::Text(t),
                Err(r) => raised(r),
            }
        }
        "common_by_pointer" => {
            let kind = opt_text(&a[1]);
            let git_dir = opt_text(&a[2]);
            match paths::common_by_pointer(host(&a[0]), kind.as_deref(), git_dir.as_deref()) {
                Some(t) => Answer::Text(t),
                None => Answer::Null,
            }
        }
        "dir_key" => Answer::Text(paths::dir_key(host(&a[0]), &text(&a[1]))),
        "dir_below" => Answer::Bool(paths::dir_below(host(&a[0]), &text(&a[1]), &text(&a[2]))),
        other => panic!("unknown function {other}"),
    }
}

/// The generator's `_Rng`.
struct Rng(u64);

impl Rng {
    fn next(&mut self, bound: u64) -> usize {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        ((self.0 >> 32) % bound) as usize
    }
}

const PIECES: [&str; 15] = [
    "a",
    "b",
    "Ab",
    "/",
    "\\",
    ".",
    "..",
    ":",
    "C:",
    "c:",
    "x",
    "worktrees",
    "\u{3a3}",
    "\u{130}",
    " ",
];

fn draw(rng: &mut Rng) -> PyStr {
    let n = rng.next(7);
    let mut out = Vec::new();
    for _ in 0..n {
        out.extend(unicode::cps(PIECES[rng.next(PIECES.len() as u64)]));
    }
    out
}

fn line(a: &Answer) -> PyStr {
    match a {
        Answer::Bool(true) => unicode::cps("t"),
        Answer::Bool(false) => unicode::cps("f"),
        Answer::Null => unicode::cps("n"),
        Answer::Raised(c, t) => [unicode::cps(&format!("e:{c}:")), t.clone()].concat(),
        Answer::Text(t) => [unicode::cps("s:"), t.clone()].concat(),
    }
}

fn utf8(cps: &[u32]) -> Vec<u8> {
    // UTF-8 with surrogates passed, as `surrogatepass` writes them.
    let mut out = Vec::new();
    for &c in cps {
        let mut buf = [0u8; 4];
        match char::from_u32(c) {
            Some(ch) => out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes()),
            None => out.extend_from_slice(&[
                0xE0 | (c >> 12) as u8,
                0x80 | ((c >> 6) & 0x3F) as u8,
                0x80 | (c & 0x3F) as u8,
            ]),
        }
    }
    out
}

/// Whether a release reads a path through `ntpath` otherwise: one leading
/// separator, a separator then a colon, or a `\\\\?\\` or `\\\\.\\` prefix
/// (the generator's `_release_bound`).
fn release_bound(p: &[u32]) -> bool {
    let p: Vec<u32> = p
        .iter()
        .map(|&u| {
            if u == u32::from(b'/') {
                u32::from(b'\\')
            } else {
                u
            }
        })
        .collect();
    let bs = u32::from(b'\\');
    let one_sep = p.first() == Some(&bs) && p.get(1) != Some(&bs);
    let holds = |needle: &str| {
        let n = unicode::cps(needle);
        p.windows(n.len()).any(|w| w == n.as_slice())
    };
    one_sep || holds("\\:") || holds("\\\\?\\") || holds("\\\\.\\")
}

/// Whether a path begins with two separators but names no server and
/// share (the generator's `_short_unc`): 3.11's `ntpath.join` writes a
/// separator after such a drive that 3.12's does not.
fn short_unc(p: &[u32]) -> bool {
    let bs = u32::from(b'\\');
    let sep = |u: &u32| *u == bs || *u == u32::from(b'/');
    if p.len() < 2 || !sep(&p[0]) || !sep(&p[1]) {
        return false;
    }
    let mut parts = p[2..].split(sep);
    let server = parts.next().unwrap_or_default();
    let share = parts.next().unwrap_or_default();
    server.is_empty() || share.is_empty()
}

/// The sweep: `n` path triples drawn from `seed`, every function asked.
/// A `stable` sweep leaves out `rebase` and redraws a triple whose first
/// path reads as `ntpath` and any of whose paths a release reads otherwise
/// (`release_bound`) or `path_under` joins otherwise (`short_unc`).
pub(crate) fn sweep(seed: u64, n: usize, nt: bool, stable: bool) -> String {
    let mut rng = Rng(seed);
    let mut answers: Vec<PyStr> = Vec::new();
    let s = |t: &PyStr| PyValue::Str(t.clone());
    for _ in 0..n {
        let (a, b, c) = loop {
            let (a, b, c) = (draw(&mut rng), draw(&mut rng), draw(&mut rng));
            if stable
                && paths::flavour(&a) == Flavour::Windows
                && [&a, &b, &c]
                    .iter()
                    .any(|p| release_bound(p) || short_unc(p))
            {
                continue;
            }
            if nt
                || [&a, &b, &c]
                    .iter()
                    .all(|p| paths::flavour(p) == Flavour::Posix)
            {
                break (a, b, c);
            }
        };
        let h = s(&flavour_name(paths::flavour(&a)));
        let cwd = s(&unicode::cps("/w/cwd"));
        let parts = PyValue::List(vec![s(&b), s(&c)]);
        let linked = s(&unicode::cps("linked"));
        let mut asked: Vec<Answer> = vec![
            answer("same_path", &[s(&a), s(&b)]),
            answer("flavour", &[s(&a)]),
            answer("path_under", &[s(&a), parts]),
            answer("basename", &[s(&a)]),
        ];
        if !stable {
            asked.push(answer("rebase", &[s(&a), s(&b), s(&c), cwd]));
        }
        asked.extend([
            answer("common_by_pointer", &[h.clone(), linked, s(&a)]),
            answer("dir_key", &[h.clone(), s(&a)]),
            answer("dir_below", &[h, s(&a), s(&b)]),
        ]);
        answers.extend(asked.iter().map(line));
    }
    let joined = answers.join(&u32::from(b'\n'));
    let mut hasher = Sha256::new();
    hasher.update(utf8(&joined));
    hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn run(test: &str, name: &str) {
    let rows = lines(name);
    let mut ledger = Ledger::new(test, name);
    for (i, row) in rows.iter().enumerate() {
        let f = cls(row.get("f").expect("f"));
        if f == "sweep" {
            let seed = int(row.get("seed").expect("seed")) as u64;
            let n = int(row.get("n").expect("n")) as usize;
            let nt = boolean(row.get("flavour").expect("flavour"));
            let stable = row.get("stable").is_some_and(boolean);
            let want = cls(row.get("o").expect("o"));
            ledger.case(format!("{name}:{i} sweep"), || {
                assert_eq!(sweep(seed, n, nt, stable), want, "{name}:{i} sweep {seed}");
            });
            continue;
        }
        let args = list(row.get("a").expect("a"));
        let want = expected(row.get("o").expect("o"));
        ledger.case(format!("{name}:{i} {f}"), || {
            assert_eq!(answer(&f, args), want, "{name}:{i} {f} {args:?}");
        });
    }
    ledger.finish(rows.len());
}

#[test]
fn every_case_of_the_ungated_file() {
    run(
        "golden_membership_records",
        "membership_records_paths.jsonl",
    );
}

#[test]
fn every_case_of_the_python_gated_file() {
    run(
        "golden_membership_records",
        "membership_records_paths_python.jsonl",
    );
}
