//! Every query of `location_probe.jsonl` (class i),
//! `location_probe_python.jsonl` (class ii, Python: link loops and
//! paths holding a NUL) and
//! `location_probe_errno.jsonl` (class iii: every errno number and the C
//! library's text for it are the platform's), POSIX only: `view_path` and
//! `probe` against `location_probe` over the case's tree written to a
//! scratch directory (`StdFs`, relative paths read from that directory as
//! the reference's process reads them from its own), with `os.stat`'s
//! injected errors (by name, or by number as `StdFs` raises one), a
//! failing `getcwd` and the host's name as each row states them.

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{
    Access, Fs, Kind, Numbered, OsError, PyErr, StatResult, StdFile, StdFs,
};
use bettermemory_core::location_probe::{
    probe, view_path, Host, ProbeAnswer, StdHost, INDETERMINATE, TOKEN_VERSION,
};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Map, Value};

/// The machine's file system with relative paths read from `root`, and
/// `os.stat` raising `fault` where one is set.
struct Rooted {
    root: Vec<u8>,
    fault: Option<Numbered>,
}

impl Rooted {
    fn at(&self, path: &[u8]) -> Vec<u8> {
        if path.is_empty() || path.starts_with(b"/") {
            return path.to_vec();
        }
        [self.root.as_slice(), b"/", path].concat()
    }
}

impl Fs for Rooted {
    type File = StdFile;
    fn open(&self, path: &[u8]) -> Result<StdFile, PyErr> {
        StdFs.open(&self.at(path))
    }
    fn fstat(&self, file: &mut StdFile) -> Result<bettermemory_core::fsdata::Stat, PyErr> {
        StdFs.fstat(file)
    }
    fn read(&self, file: &mut StdFile, max: usize) -> Result<Vec<u8>, PyErr> {
        StdFs.read(file, max)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        StdFs.access(&self.at(path), mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<StdFile, OsError> {
        StdFs.open_no_follow(&self.at(path))
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        StdFs.lstat(&self.at(path))
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        StdFs.readlink(&self.at(path))
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.stat_numbered(path).map_err(|raised| raised.error)
    }
    fn stat_numbered(&self, path: &[u8]) -> Result<StatResult, Numbered> {
        if let Some(fault) = self.fault {
            return Err(fault);
        }
        StdFs.stat_numbered(&self.at(path))
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        StdFs.lstat_result(&self.at(path))
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        StdFs.listdir(&self.at(path))
    }
}

/// The machine's `strerror`, the row's host name.
struct Named(Option<Vec<u8>>);

impl Host for Named {
    fn hostname(&self) -> Option<Vec<u8>> {
        self.0.clone()
    }
    fn strerror(&self, errno: &str) -> Option<String> {
        StdHost.strerror(errno)
    }
    fn strerror_code(&self, code: i32) -> Option<String> {
        StdHost.strerror_code(code)
    }
}

fn b(data: &[u8]) -> String {
    if data
        .iter()
        .all(|&c| (0x20..0x7f).contains(&c) && c != b'\\')
    {
        return format!("={}", std::str::from_utf8(data).expect("ASCII"));
    }
    common::hex(data)
}

fn unspelled(text: &str) -> Vec<u8> {
    match text.strip_prefix('=') {
        Some(rest) => rest.as_bytes().to_vec(),
        None => common::unhex(text),
    }
}

fn replaced(data: &[u8], from: &[u8], to: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len());
    let mut rest = data;
    while !rest.is_empty() {
        if rest.starts_with(from) {
            out.extend_from_slice(to);
            rest = &rest[from.len()..];
        } else {
            out.push(rest[0]);
            rest = &rest[1..];
        }
    }
    out
}

fn errno_named(name: &str) -> &'static str {
    [
        "ENOENT",
        "ENOTDIR",
        "ELOOP",
        "ENAMETOOLONG",
        "EACCES",
        "EPERM",
        "EIO",
        "EINVAL",
        "EINTR",
        "EBADF",
        "EISDIR",
        "EEXIST",
        "EHOSTDOWN",
    ]
    .into_iter()
    .find(|n| *n == name)
    .unwrap_or_else(|| panic!("an errno the test does not know: {name}"))
}

fn answer(q: &Value, root: &str) -> Value {
    let path = replaced(
        &unspelled(q["p"].as_str().unwrap()),
        b"<root>",
        root.as_bytes(),
    );
    // `e`: the errno by its name alone, as an error made in memory
    // carries it; `n`: by its number, as `StdFs` raises it.
    let named = q.get("e").map(|e| {
        let name = errno_named(e.as_str().unwrap());
        Numbered::from(OsError::new(PyErr::of_errno(name), name))
    });
    let fault = named.or_else(|| {
        let n = i32::try_from(q.get("n")?.as_i64().unwrap()).unwrap();
        Some(Numbered::of_io(&std::io::Error::from_raw_os_error(n)))
    });
    let fs = Rooted {
        root: root.as_bytes().to_vec(),
        fault,
    };
    let host = Named(match q.get("h") {
        None => Some(b"probe-host".to_vec()),
        Some(Value::Null) => None,
        Some(h) => Some(unspelled(h.as_str().unwrap())),
    });
    let cwd = if q.get("g").is_some() {
        None
    } else {
        Some(root.as_bytes())
    };
    let spell = |p: &[u8]| json!(b(&replaced(p, root.as_bytes(), b"<scratch>")));
    let mut out = Map::new();
    for key in ["p", "e", "n", "g", "h"] {
        if let Some(v) = q.get(key) {
            out.insert(key.into(), v.clone());
        }
    }
    out.insert(
        "v".into(),
        match view_path(&fs, bettermemory_core::fsdata::Flavour::Posix, &path, cwd) {
            Ok(v) => spell(&v),
            Err(d) => json!({ "declined": format!("{d:?}") }),
        },
    );
    let o = match probe(
        &fs,
        &host,
        bettermemory_core::fsdata::Flavour::Posix,
        &path,
        cwd,
    ) {
        Err(d) => json!({ "declined": format!("{d:?}") }),
        Ok(p) => {
            let mut o = Map::new();
            o.insert("v".into(), json!(TOKEN_VERSION));
            o.insert("probe".into(), json!(true));
            o.insert("cwd".into(), spell(&p.cwd));
            match p.answer {
                ProbeAnswer::Indeterminate { detail } => {
                    o.insert("state".into(), json!(INDETERMINATE));
                    o.insert("detail".into(), json!(detail));
                }
                ProbeAnswer::Present { dev, ino, hostname } => {
                    let st = StdFs.stat(&fs.at(&p.cwd)).expect("the resolved cwd");
                    let same = st.dev == Some(dev) && st.ino == Some(ino);
                    o.insert(
                        "cwd_object".into(),
                        if same {
                            json!("stat")
                        } else {
                            json!([dev, ino])
                        },
                    );
                    o.insert(
                        "hostname".into(),
                        hostname.map_or(Value::Null, |h| json!(b(&h))),
                    );
                }
            }
            Value::Object(o)
        }
    };
    out.insert("o".into(), o);
    Value::Object(out)
}

struct Scratch(std::path::PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        use std::os::unix::fs::PermissionsExt;
        fn open_up(dir: &std::path::Path) {
            let Ok(entries) = std::fs::read_dir(dir) else {
                return;
            };
            for entry in entries.flatten() {
                let path = entry.path();
                if std::fs::symlink_metadata(&path).is_ok_and(|m| m.is_dir()) {
                    let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700));
                    open_up(&path);
                }
            }
        }
        open_up(&self.0);
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn fresh_dir(stem: &str) -> std::path::PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("{stem}-{}-{n}", std::process::id()));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    std::fs::canonicalize(&dir).expect("a resolved scratch directory")
}

/// Writes the case's tree; false where a mode-000 folder can still be
/// listed (a root user), so its answers cannot be the reference's.
fn write_tree(case: &Value, root: &std::path::Path) -> bool {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let real = root.to_str().unwrap();
    for d in case["d"].as_array().into_iter().flatten() {
        std::fs::create_dir_all(root.join(d.as_str().unwrap())).unwrap();
    }
    for (rel, text) in case["f"].as_object().into_iter().flatten() {
        std::fs::write(root.join(rel), text.as_str().unwrap()).unwrap();
    }
    for (rel, to) in case["l"].as_object().into_iter().flatten() {
        symlink(to.as_str().unwrap().replace("<root>", real), root.join(rel)).unwrap();
    }
    for (rel, mode) in case["m"].as_object().into_iter().flatten() {
        let bits = u32::from_str_radix(mode.as_str().unwrap(), 8).unwrap();
        std::fs::set_permissions(root.join(rel), std::fs::Permissions::from_mode(bits)).unwrap();
        if bits == 0 && std::fs::read_dir(root.join(rel)).is_ok() {
            return false;
        }
    }
    true
}

fn run(test: &str, file: &str) {
    let lines = jsonl(file);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    let ledger = Ledger::new(test, file);
    let base = Scratch(fresh_dir("bm-lprobe"));
    let mut failures = Vec::new();
    let mut total = 0;
    for (i, case) in cases.iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        if !write_tree(case, &dir) {
            eprintln!("#{i}: a mode-000 folder is searchable here (root); skipped");
            continue;
        }
        let root = dir.to_str().unwrap();
        for q in case["q"].as_array().unwrap() {
            total += 1;
            let got = answer(q, root);
            if &got != q {
                failures.push(format!("#{i}: native {got} reference {q}"));
            }
        }
    }
    drop(base);
    assert!(total > 0, "{file}: no query checked");
    ledger.settle(total, &failures);
}

#[test]
fn location_probe_every_query() {
    run("golden_location_probe", "location_probe.jsonl");
}

#[test]
fn location_probe_loops() {
    run("golden_location_probe_loops", "location_probe_python.jsonl");
}

#[test]
fn location_probe_errnos() {
    run("golden_location_probe_errnos", "location_probe_errno.jsonl");
}
