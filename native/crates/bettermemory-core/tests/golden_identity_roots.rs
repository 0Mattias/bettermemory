//! Every case of `identity_roots.jsonl` (not gated) and
//! `identity_roots_python.jsonl` (gated on the Python release) against
//! `identity::first_file_root` and `identity::workspace_declaration`. The
//! spelling is the generator's (`tests/contract/gen_identity_roots.py`):
//! a text is a string, or a list of code points where it holds a lone
//! surrogate. The home is the generator's fixed one, through the
//! `config::Fs` trait's `expanduser`.

mod common;

use bettermemory_core::config::{self, Flavour, Fs, OsFailure, Raised};
use bettermemory_core::identity::{self as id, Caller, Published, Workspace};
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use common::parity::Ledger;
use serde_json::Value;
use sha2::Digest;

const POSIX_HOME: &str = "/fixture-home/me";
const NT_HOME: &str = "C:\\Profiles\\fixture";

fn text(v: &Value) -> PyStr {
    match v {
        Value::String(s) => unicode::cps(s),
        Value::Array(_) => common::cps_of(v),
        other => panic!("not a text: {other:?}"),
    }
}

fn opt(v: &Value) -> Option<PyStr> {
    (!v.is_null()).then(|| text(v))
}

/// `Path(text).expanduser()` of CPython 3.13 over the generator's fixed
/// home: `posixpath.expanduser` with `HOME`, `ntpath.expanduser` with
/// `USERPROFILE` and `USERNAME` (`fixture`). No other POSIX user exists.
pub struct FixedHome(pub Flavour);

impl Fs for FixedHome {
    fn read(&self, _: &str) -> Result<Vec<u8>, Option<OsFailure>> {
        Err(None)
    }
    fn present(&self, _: &str) -> Result<bool, Raised> {
        Ok(false)
    }
    fn is_symlink(&self, _: &str) -> bool {
        false
    }
    fn is_dir(&self, _: &str) -> bool {
        false
    }
    fn resolve(&self, path: &str) -> Result<String, Raised> {
        Ok(path.to_owned())
    }
    fn expanduser(&self, path: &str) -> Result<String, Raised> {
        let (sep, anchored) = match self.0 {
            Flavour::Posix => ('/', path.starts_with('/')),
            Flavour::Windows => (
                '\\',
                path.starts_with('\\') || path.chars().nth(1) == Some(':'),
            ),
        };
        let (first, rest) = path.split_once(sep).unwrap_or((path, ""));
        if anchored || !first.starts_with('~') {
            return Ok(path.to_owned());
        }
        let home = match (self.0, first) {
            (Flavour::Posix, "~") => POSIX_HOME.to_owned(),
            (Flavour::Posix, _) => return Err(config::no_home()),
            (Flavour::Windows, "~" | "~fixture") => NT_HOME.to_owned(),
            (Flavour::Windows, user) => format!("C:\\Profiles\\{}", &user[1..]),
        };
        Ok(config::pure_path(&format!("{home}{sep}{rest}"), self.0))
    }
    fn join(&self, path: &str, name: &str) -> String {
        format!("{path}/{name}")
    }
    fn home(&self) -> Option<String> {
        None
    }
    fn cwd(&self) -> Option<String> {
        None
    }
    fn under(&self, _: &str, _: &str) -> bool {
        false
    }
}

fn raise_matches(want: &Value, got: &Raised) {
    let pair = want["raise"].as_array().expect("a raise");
    assert_eq!(pair[0].as_str(), Some(got.class));
    // A message holding a lone surrogate is written `\udcXX` (stated).
    let message: String = text(&pair[1])
        .iter()
        .map(|&c| char::from_u32(c).map_or_else(|| format!("\\u{c:04x}"), String::from))
        .collect();
    assert_eq!(message, got.message);
}

fn roots(v: &Value) -> Vec<Option<PyStr>> {
    v.as_array().expect("roots").iter().map(opt).collect()
}

fn check_root(row: &[Value]) {
    let got = id::first_file_root(&roots(&row[1]));
    match (&row[2], got) {
        (want @ Value::Object(_), Err(e)) => raise_matches(want, &e),
        (want, Ok(path)) if !want.is_object() => assert_eq!(path, opt(want), "{row:?}"),
        (want, got) => panic!("{row:?}: want {want:?}, got {got:?}"),
    }
}

fn check_declaration(row: &[Value]) {
    let flavour = if row[1] == "nt" {
        Flavour::Windows
    } else {
        Flavour::Posix
    };
    let mut published = Published::default();
    if let Some(p) = row[2].as_array() {
        published.publish(Caller {
            workspace: Workspace {
                path: opt(&p[0]),
                source: text(&p[1]),
            },
            ..Caller::default()
        });
    }
    let env: Vec<(String, PyStr)> = row[3]
        .as_array()
        .expect("env")
        .iter()
        .map(|p| (p[0].as_str().expect("key").to_owned(), text(&p[1])))
        .collect();
    let mut reads = Vec::new();
    let mut ncwd = 0;
    let cwd = opt(&row[4]);
    let got = id::workspace_declaration(
        &published,
        &mut |k| {
            reads.push(Value::from(k));
            env.iter().find(|(e, _)| e == k).map(|(_, v)| v.clone())
        },
        &mut || {
            ncwd += 1;
            cwd.clone()
        },
        &FixedHome(flavour),
        flavour,
    );
    let declined = matches!(&got, Err(r) if r.class == "NotPorted");
    match (&row[5], got) {
        // Stated decline: a declared path holding a lone surrogate.
        (Value::Array(p), Err(r)) if declined => {
            assert!(p[0].is_array(), "{row:?}: declined {r:?}");
        }
        (want @ Value::Object(_), Err(e)) => raise_matches(want, &e),
        (Value::Null, Ok(None)) => {}
        (Value::Array(p), Ok(Some((path, source)))) => {
            assert_eq!(path, text(&p[0]), "{row:?}");
            assert_eq!(source, text(&p[1]), "{row:?}");
        }
        (want, got) => panic!("{row:?}: want {want:?}, got {got:?}"),
    }
    assert_eq!(Value::Array(reads), row[6], "{row:?}: environment reads");
    assert_eq!(Value::from(ncwd), row[7], "{row:?}: cwd reads");
}

const PIECES: [&str; 27] = [
    "file:",
    "FILE:",
    "http:",
    "//",
    "/",
    "localhost",
    "srv",
    "C:",
    ":",
    "%",
    "%4",
    "%41",
    "%C3",
    "%A9",
    "%E2%82",
    "%FF",
    "%00",
    "\u{e9}",
    ";",
    "?",
    "#",
    " ",
    "\t",
    "\x01",
    "a",
    "\u{ff0f}",
    "@",
];

fn splitmix(state: &mut u64) -> u64 {
    *state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
    let mut z = *state;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

/// The generator's `digest_uris`.
pub fn digest_uris(seed: u64, count: usize) -> Vec<PyStr> {
    let mut state = seed;
    (0..count)
        .map(|_| {
            let n = splitmix(&mut state) % 9;
            let mut uri = String::new();
            for _ in 0..n {
                uri.push_str(PIECES[(splitmix(&mut state) % PIECES.len() as u64) as usize]);
            }
            unicode::cps(&uri)
        })
        .collect()
}

/// One answer as the generator's JSON line.
fn answer_line(got: Result<Option<PyStr>, Raised>) -> String {
    let value = match got {
        Ok(None) => PyValue::None,
        Ok(Some(p)) if p.iter().any(|&c| (0xD800..0xE000).contains(&c)) => {
            PyValue::List(p.iter().map(|&c| PyValue::int(c.into())).collect())
        }
        Ok(Some(p)) => PyValue::Str(p),
        Err(r) => PyValue::Dict(vec![(
            PyValue::str("raise"),
            PyValue::List(vec![PyValue::str(r.class), PyValue::str(&r.message)]),
        )]),
    };
    let line = json::dumps(&value, DumpOptions::SORTED_ASCII_COMPACT).expect("JSON");
    line.iter().filter_map(|&c| char::from_u32(c)).collect()
}

fn check_digest(row: &[Value]) {
    let seed = row[1].as_u64().expect("seed");
    let count = row[2].as_u64().expect("count") as usize;
    let uris = digest_uris(seed, count);
    assert_eq!(uris.len(), count);
    let lines: Vec<String> = uris
        .iter()
        .map(|u| answer_line(id::first_file_root(&[Some(u.clone())])))
        .collect();
    let sha = common::hex(&sha2::Sha256::digest(lines.join("\n").as_bytes()));
    assert_eq!(Value::from(sha), row[3], "digest of {count} URIs");
}

fn run(name: &str) {
    let lines = common::jsonl(name);
    let header = &lines[0];
    let rows = &lines[1..];
    let in_file: u64 = ["r", "w", "d"]
        .iter()
        .map(|k| header[*k].as_u64().expect("count"))
        .sum();
    assert_eq!(rows.len() as u64, in_file, "{name}: header counts");
    let mut ledger = Ledger::new(&format!("golden_{}", name.trim_end_matches(".jsonl")), name);
    for (i, row) in rows.iter().enumerate() {
        let row = row.as_array().expect("a row").clone();
        ledger.case(format!("row {i}"), || match row[0].as_str() {
            Some("r") => check_root(&row),
            Some("w") => check_declaration(&row),
            Some("d") => check_digest(&row),
            other => panic!("unknown row kind {other:?}"),
        });
    }
    ledger.finish(rows.len());
}

#[test]
fn identity_roots_every_case() {
    run("identity_roots.jsonl");
}

#[test]
fn identity_roots_python_every_case() {
    run("identity_roots_python.jsonl");
}
