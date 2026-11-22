//! Every case of `machine_facts.jsonl`, `machine_facts_unicode.jsonl` and
//! `machine_facts_platform.jsonl` against `machine::facts`: `_read_id`,
//! `_read_record`, `_darwin_hardware`, `_linux_hardware`,
//! `_windows_hardware`, `current_hostname`, `read_hardware_id` and its
//! memo, and `_file_object`.

mod common;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::location_probe::Host;
use bettermemory_core::machine::facts::{self as f, HardwareMemo, HardwareSource};
use bettermemory_core::pyvalue::{PyError, PyValue};
use common::parity::Ledger;
use common::{jsonl, neutral, unhex};
use serde_json::Value;
use std::cell::Cell;

fn text(s: &[u32]) -> Value {
    match s
        .iter()
        .map(|&c| char::from_u32(c))
        .collect::<Option<String>>()
    {
        Some(t) => Value::String(t),
        None => panic!("a lone surrogate in {s:?}"),
    }
}

fn opt(v: Option<Vec<u32>>) -> Value {
    v.map_or(Value::Null, |s| text(&s))
}

fn cps(v: &Value) -> Vec<u32> {
    v.as_str().expect("a text").chars().map(u32::from).collect()
}

/// `{"r": ...}` or `{"x": ...}` of an answer.
fn same(row: &Value, got: Result<Value, &str>) -> bool {
    match got {
        Ok(v) => row.get("r") == Some(&v),
        Err(x) => row["x"] == x,
    }
}

const FILE: &[u8] = b"/s/file";

fn fs_for(row: &Value) -> (MemFs, Vec<u8>) {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/s", Node::Dir);
    let path = match row["k"].as_str() {
        None => {
            fs.insert(FILE, Node::File(unhex(row["b"].as_str().expect("b"))));
            FILE.to_vec()
        }
        Some("missing") => b"/s/missing".to_vec(),
        Some("dir") => {
            fs.insert(b"/s/folder", Node::Dir);
            b"/s/folder".to_vec()
        }
        Some("nul") => b"/s/a\0b".to_vec(),
        Some(other) => panic!("kind {other}"),
    };
    (fs, path)
}

fn err(e: PyError) -> &'static str {
    e.name()
}

struct Source {
    g: Option<[u8; 16]>,
    o: Option<Vec<u32>>,
    asked: Cell<bool>,
    guid: Option<PyValue>,
}

impl HardwareSource for Source {
    fn gethostuuid(&self) -> Option<[u8; 16]> {
        self.g
    }
    fn ioreg(&self) -> Result<Option<Vec<u32>>, PyError> {
        self.asked.set(true);
        Ok(self.o.clone())
    }
    fn machine_guid(&self) -> Option<PyValue> {
        self.guid.clone()
    }
}

struct NamedHost(Option<Vec<u8>>);

impl Host for NamedHost {
    fn hostname(&self) -> Option<Vec<u8>> {
        self.0.clone()
    }
    fn strerror(&self, _errno: &str) -> Option<String> {
        None
    }
}

/// `os.fsencode` with `surrogateescape`.
fn fsencode(s: &[u32]) -> Vec<u8> {
    let mut out = Vec::new();
    for &c in s {
        if (0xDC80..=0xDCFF).contains(&c) {
            out.push((c - 0xDC00) as u8);
        } else {
            let ch = char::from_u32(c).expect("a scalar");
            out.extend_from_slice(ch.encode_utf8(&mut [0; 4]).as_bytes());
        }
    }
    out
}

fn py_text_of(v: &PyValue) -> Vec<u32> {
    match v {
        PyValue::Str(s) => s.clone(),
        other => panic!("a text {other:?}"),
    }
}

struct FakeStat(Result<f::IdStat, f::IdStatError>);

impl f::IdFileStat for FakeStat {
    fn id_stat(&self, _path: &[u8]) -> Result<f::IdStat, f::IdStatError> {
        self.0
    }
}

fn check_row(row: &Value) -> bool {
    match row["f"].as_str().expect("f") {
        "rid" => {
            let (fs, path) = fs_for(row);
            let got = f::read_id(&fs, &path)
                .map(|v| v.map_or(Value::Null, Value::String))
                .map_err(err);
            same(row, got)
        }
        "rrec" => {
            let (fs, path) = fs_for(row);
            match f::read_record(&fs, &path) {
                Ok(None) => row.get("r") == Some(&Value::Null),
                // Spelled, so that NaN equals NaN.
                Ok(Some(v)) => {
                    !row["r"].is_null() && format!("{:?}", neutral(&row["r"])) == format!("{v:?}")
                }
                Err(e) => row["x"] == e.name(),
            }
        }
        "rrec_deep" => {
            let n = row["n"].as_u64().expect("n") as usize;
            let body = format!("{{\"a\": {}{}}}", "[".repeat(n), "]".repeat(n));
            let mut fs = MemFs::new(Flavour::Posix);
            fs.insert(FILE, Node::File(body.into_bytes()));
            let got = f::read_record(&fs, FILE)
                .map(|v| v.map_or(Value::Null, |_| Value::from("ok")))
                .map_err(err);
            same(row, got)
        }
        "dar" => {
            let g = match row["g"].as_str().expect("g") {
                "fail" | "raise" => None,
                hex => Some(unhex(hex).try_into().expect("16 bytes")),
            };
            let src = Source {
                g,
                o: row["o"].as_str().map(|_| cps(&row["o"])),
                asked: Cell::new(false),
                guid: None,
            };
            let got = f::darwin_hardware(src.gethostuuid(), || src.ioreg()).expect("no raise");
            opt(got) == row["r"] && src.asked.get() == row["asked"].as_bool().expect("asked")
        }
        "lin" => {
            let mut fs = MemFs::new(Flavour::Posix);
            let mut paths = Vec::new();
            for (i, spec) in row["files"].as_array().expect("files").iter().enumerate() {
                let path = format!("/l/{i}").into_bytes();
                match spec.as_str() {
                    None => {}
                    Some("dir") => fs.insert(&path, Node::Dir),
                    Some("nul") => {
                        paths.push(b"/l/a\0b".to_vec());
                        continue;
                    }
                    Some(hex) => fs.insert(&path, Node::File(unhex(hex))),
                }
                paths.push(path);
            }
            let refs: Vec<&[u8]> = paths.iter().map(Vec::as_slice).collect();
            let got = f::linux_hardware(&fs, &refs).map(opt).map_err(err);
            same(row, got)
        }
        "win" => {
            let value = row.get("a").map(neutral);
            match f::windows_hardware(value.as_ref()) {
                Ok(None) => row.get("r") == Some(&Value::Null),
                Ok(Some(s)) => neutral(&row["r"]) == PyValue::Str(s),
                Err(e) => row["x"] == e.name(),
            }
        }
        "host" => {
            let name = match neutral(&row["a"]) {
                PyValue::None => None,
                v => Some(fsencode(&py_text_of(&v))),
            };
            let got = f::current_hostname(&NamedHost(name));
            match neutral(&row["r"]) {
                PyValue::None => got.is_none(),
                v => got == Some(py_text_of(&v)),
            }
        }
        "hw" => {
            struct Named;
            impl HardwareSource for Named {
                fn gethostuuid(&self) -> Option<[u8; 16]> {
                    None
                }
                fn ioreg(&self) -> Result<Option<Vec<u32>>, PyError> {
                    Ok(Some(cps(&Value::from("\"IOPlatformUUID\" = \"D\""))))
                }
                fn machine_guid(&self) -> Option<PyValue> {
                    Some(PyValue::str("W"))
                }
            }
            let mut fs = MemFs::new(Flavour::Posix);
            fs.insert(b"/etc/machine-id", Node::File(b"L\n".to_vec()));
            let mut memo = HardwareMemo::default();
            let p = row["p"].as_str().expect("p");
            let got = f::read_hardware_id(&mut memo, p, &Named, &fs, &f::LINUX_MACHINE_ID);
            got.map(opt).ok().as_ref() == row.get("r")
        }
        "memo" => {
            let fills = Cell::new(0);
            let mut memo = HardwareMemo::default();
            let fill = || {
                fills.set(fills.get() + 1);
                Ok::<_, ()>(None)
            };
            let mut answers = vec![memo.get_or_fill(fill).expect("a")];
            answers.push(memo.get_or_fill(fill).expect("b"));
            memo.clear();
            answers.push(memo.get_or_fill(fill).expect("c"));
            let answers: Vec<Value> = answers.into_iter().map(opt).collect();
            row["r"].as_array() == Some(&answers) && row["fills"] == fills.get()
        }
        "fo" => check_fo(row),
        other => panic!("row kind {other}"),
    }
}

fn check_fo(row: &Value) -> bool {
    let st = &row["st"];
    if let Some(class) = st.as_str() {
        let e = if class == "ValueError" {
            f::IdStatError::Value
        } else {
            f::IdStatError::Os
        };
        return match f::file_object(&FakeStat(Err(e)), b"x") {
            Ok(None) => row.get("r") == Some(&Value::Null),
            Err(f::FileObjectError::Raised(x)) => row["x"] == x.name(),
            _ => false,
        };
    }
    let dev = st["dev"].as_u64().expect("dev").to_string();
    let ino = st["ino"].as_u64().expect("ino").to_string();
    let birth = if let Some(ns) = st.get("ns") {
        let ns = i128::from(ns.as_i64().expect("ns"));
        let (sec, nsec) = (ns.div_euclid(1_000_000_000), ns.rem_euclid(1_000_000_000));
        f::birth_text(Some((sec as i64, nsec as u32)), true)
    } else if let Some(s) = st.get("s") {
        Some(f::ns_of_seconds(float_from_hex(s.as_str().expect("s"))).expect("finite"))
    } else {
        f::birth_text(None, false)
    };
    Value::from(vec![
        Value::from(dev),
        Value::from(ino),
        birth.map_or(Value::Null, Value::from),
    ]) == row["r"]
}

/// `float.fromhex` of what `float.hex` wrote.
fn float_from_hex(s: &str) -> f64 {
    let (neg, s) = s.strip_prefix('-').map_or((false, s), |r| (true, r));
    let s = s.strip_prefix("0x").expect("0x");
    let (mant, exp) = s.split_once('p').expect("p");
    let (int, frac) = mant.split_once('.').unwrap_or((mant, ""));
    let mut v = u64::from_str_radix(int, 16).expect("int") as f64;
    let mut scale = 1.0 / 16.0;
    for d in frac.chars() {
        v += f64::from(d.to_digit(16).expect("hex")) * scale;
        scale /= 16.0;
    }
    let v = v * 2f64.powi(exp.parse::<i32>().expect("exp"));
    if neg {
        -v
    } else {
        v
    }
}

fn check_file(name: &str) {
    let lines = jsonl(name);
    let count = lines[0]["count"].as_u64().expect("count") as usize;
    let mut ledger = Ledger::new("golden_machine_facts", name);
    for (i, row) in lines[1..].iter().enumerate() {
        ledger.case(format!("{name} row {i}"), || {
            assert!(check_row(row), "{name} row {i}: {row}");
        });
    }
    ledger.finish(count);
}

#[test]
fn machine_facts_every_case() {
    check_file("machine_facts.jsonl");
}

/// The row nested 100,000 levels, gated on the Python release: the port
/// raises at its fixed depth as 3.13.13 does.
#[test]
fn machine_facts_python_every_case() {
    check_file("machine_facts_python.jsonl");
}

#[test]
fn machine_facts_unicode_every_case() {
    check_file("machine_facts_unicode.jsonl");
}

/// The real stat: the header's record of `os.stat_result` against
/// [`f::BIRTH_NS`], and `_file_object` of a real file through
/// [`f::StdIdFileStat`] against what the reference found.
#[test]
fn machine_facts_platform_every_case() {
    let name = "machine_facts_platform.jsonl";
    let lines = jsonl(name);
    let header = &lines[0];
    let mut ledger = Ledger::new("golden_machine_facts", name);
    let dir = std::env::temp_dir().join(format!("bm-machine-facts-{}", std::process::id()));
    std::fs::create_dir_all(&dir).expect("a folder");
    let path = dir.join("machine-id");
    std::fs::write(&path, b"x").expect("a file");
    let raw = path.to_string_lossy().into_owned().into_bytes();
    let got = f::file_object(&f::StdIdFileStat, &raw);
    std::fs::remove_dir_all(&dir).ok();
    for (i, row) in lines[1..].iter().enumerate() {
        ledger.case(format!("{name} row {i}"), || {
            assert_eq!(header["has_birthtime_ns"], f::BIRTH_NS, "{header}");
            if cfg!(windows) {
                // The Windows flavour declines (ENG-124).
                assert!(matches!(got, Err(f::FileObjectError::Declined(_))));
                return;
            }
            let (dev, ino, birth) = got.clone().expect("a stat").expect("an object");
            assert!(!dev.is_empty() && !ino.is_empty());
            assert_eq!(Value::from(birth.is_some()), row["birth"], "{row}");
            assert_eq!(row["dev"], true);
            assert_eq!(row["ino"], true);
            assert_eq!(row["birth_from_ns"], birth.is_some() && f::BIRTH_NS);
            assert_eq!(row["birth_from_s"], birth.is_some() && !f::BIRTH_NS);
        });
    }
    ledger.finish(header["count"].as_u64().expect("count") as usize);
}
