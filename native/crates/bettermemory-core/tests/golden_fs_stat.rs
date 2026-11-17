//! Every query of `fs_stat.jsonl` (class i, POSIX only): `os.stat` and
//! `os.listdir` against `Fs::stat` and `Fs::listdir` over the case's tree
//! in memory (`MemFs`), and over the same tree written to a scratch
//! directory (`StdFs`), where the inode number and the times the fixture
//! leaves out are compared with the reference Python's `os.stat` of the
//! same paths. And `fs_access.jsonl` (class i, POSIX only): `os.access`
//! for each mode and the open that does not follow a final link, the same
//! two ways, the on-disk answers also asked of the reference Python live.

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{Access, Flavour, Fs, Kind, MemFs, Node, OsError, StdFs};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "fs_stat.jsonl";
const MEM_ROOT: &str = "/<scratch>";

fn cases() -> Vec<Value> {
    cases_of(FILE).1
}

/// A fixture's header and its cases.
fn cases_of(file: &str) -> (Value, Vec<Value>) {
    let lines = jsonl(file);
    let cases = lines[1..].to_vec();
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    (lines[0].clone(), cases)
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

fn entries<'a>(case: &'a Value, key: &str) -> Vec<(&'a str, &'a Value)> {
    match case.get(key) {
        Some(Value::Object(map)) => map.iter().map(|(k, v)| (k.as_str(), v)).collect(),
        Some(Value::Array(list)) => list
            .iter()
            .map(|v| (v.as_str().unwrap(), &Value::Null))
            .collect(),
        _ => Vec::new(),
    }
}

fn error(e: OsError) -> Value {
    match e.errno {
        Some(name) => json!({"e": e.class.name(), "n": name}),
        None => json!({"e": e.class.name()}),
    }
}

fn kind(k: Kind) -> &'static str {
    match k {
        Kind::Regular => "reg",
        Kind::Directory => "dir",
        Kind::Fifo => "fifo",
        Kind::Socket => "sock",
        Kind::Link => "link",
        Kind::Other => "other",
    }
}

/// Each query's answers through `fs`, the case's directory being `root`.
fn answers<F: Fs>(fs: &F, case: &Value, root: &str) -> Value {
    let queries = case["q"].as_array().unwrap();
    Value::Array(
        queries
            .iter()
            .map(|q| {
                let path = replaced(
                    &unspelled(q["p"].as_str().unwrap()),
                    b"<root>",
                    root.as_bytes(),
                );
                let s = match fs.stat(&path) {
                    Ok(st) => {
                        let mut out = json!({
                            "k": kind(st.kind),
                            "m": format!("{:o}", st.mode.expect("POSIX gives st_mode")),
                        });
                        if st.kind == Kind::Regular {
                            out["z"] = json!(st.size);
                        }
                        out
                    }
                    Err(e) => error(e),
                };
                let l = match fs.listdir(&path) {
                    Ok(mut names) => {
                        names.sort();
                        Value::Array(names.iter().map(|n| json!(b(n))).collect())
                    }
                    Err(e) => error(e),
                };
                json!({"p": q["p"], "s": s, "l": l})
            })
            .collect(),
    )
}

fn world(case: &Value) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    let at = |rel: &str| format!("{MEM_ROOT}/{rel}").into_bytes();
    fs.insert(MEM_ROOT.as_bytes(), Node::Dir);
    for (rel, text) in entries(case, "f") {
        fs.insert(
            &at(rel),
            Node::File(text.as_str().unwrap().as_bytes().to_vec()),
        );
    }
    for (rel, _) in entries(case, "d") {
        fs.insert(&at(rel), Node::Dir);
    }
    for (rel, to) in entries(case, "l") {
        let target = to.as_str().unwrap().replace("<root>", MEM_ROOT);
        fs.insert(&at(rel), Node::Link(target.into_bytes()));
    }
    for (rel, _) in entries(case, "p") {
        fs.insert(&at(rel), Node::Fifo);
    }
    for (rel, mode) in entries(case, "m") {
        fs.set_mode(
            &at(rel),
            u32::from_str_radix(mode.as_str().unwrap(), 8).unwrap(),
        );
    }
    fs
}

#[test]
fn fs_stat_every_case_in_memory() {
    let cases = cases();
    let ledger = Ledger::new("golden_fs_stat", FILE);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let got = answers(&world(case), case, MEM_ROOT);
        if got != case["q"] {
            failures.push(format!("#{i}: native {got} reference {}", case["q"]));
        }
    }
    ledger.settle(cases.len(), &failures);
}

struct Scratch(std::path::PathBuf);

/// A new directory of this test's own (the process id and a process-wide
/// counter, made with `create_dir`), resolved.
fn fresh_dir(stem: &str) -> std::path::PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("{stem}-{}-{n}", std::process::id()));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    std::fs::canonicalize(&dir).expect("a resolved scratch directory")
}

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
        let _ =
            std::fs::set_permissions(&self.0, std::os::unix::fs::PermissionsExt::from_mode(0o700));
        open_up(&self.0);
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// Refuse to run where a mode-000 folder can be listed (root): the
/// fixture holds a user's answers.
fn refuse_root(base: &std::path::Path) {
    use std::os::unix::fs::PermissionsExt;
    let probe = base.join("probe");
    std::fs::create_dir(&probe).unwrap();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o000)).unwrap();
    let listed = std::fs::read_dir(&probe).is_ok();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o700)).unwrap();
    std::fs::remove_dir(&probe).unwrap();
    assert!(
        !listed,
        "a mode-000 folder can be listed here (running as root?): the fixture holds a user's answers"
    );
}

fn write_tree(case: &Value, root: &std::path::Path) -> bool {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let mode = |p: &std::path::Path, m: u32| {
        std::fs::set_permissions(p, std::fs::Permissions::from_mode(m)).unwrap()
    };
    mode(root, 0o755);
    for (rel, _) in entries(case, "d") {
        std::fs::create_dir_all(root.join(rel)).unwrap();
        mode(&root.join(rel), 0o755);
    }
    for (rel, text) in entries(case, "f") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, text.as_str().unwrap()).unwrap();
        mode(&path, 0o644);
    }
    let real = root.to_str().unwrap();
    for (rel, to) in entries(case, "l") {
        symlink(to.as_str().unwrap().replace("<root>", real), root.join(rel)).unwrap();
    }
    for (rel, _) in entries(case, "p") {
        let made = std::process::Command::new("mkfifo")
            .arg(root.join(rel))
            .status()
            .is_ok_and(|s| s.success());
        if !made {
            return false;
        }
        mode(&root.join(rel), 0o644);
    }
    let mut modes = entries(case, "m");
    modes.sort_by_key(|(rel, _)| std::cmp::Reverse(rel.matches('/').count()));
    for (rel, m) in modes {
        mode(
            &root.join(rel),
            u32::from_str_radix(m.as_str().unwrap(), 8).unwrap(),
        );
    }
    true
}

/// The reference Python's `os.stat` of `paths`: `[ino, mtime_ns,
/// ctime_ns, size, mode]` each, or null where it raises.
fn reference_stats(python: &std::path::Path, paths: &[String]) -> Vec<Value> {
    let script = "import json, os, sys\n\
        out = []\n\
        for p in sys.argv[1:]:\n\
        \x20   try:\n\
        \x20       s = os.stat(p)\n\
        \x20   except (OSError, ValueError):\n\
        \x20       out.append(None)\n\
        \x20       continue\n\
        \x20   out.append([s.st_ino, s.st_mtime_ns, s.st_ctime_ns, s.st_size, s.st_mode])\n\
        print(json.dumps(out))\n";
    let mut cmd = std::process::Command::new(python);
    cmd.env_clear().arg("-I").arg("-c").arg(script).args(paths);
    if let Some(p) = std::env::var_os("PATH") {
        cmd.env("PATH", p);
    }
    let out = cmd.output().expect("the reference runs");
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice::<Vec<Value>>(&out.stdout).expect("JSON")
}

#[test]
fn fs_stat_every_case_on_disk() {
    let cases = cases();
    let ledger = Ledger::new("golden_fs_stat_stdfs", FILE);
    let base = Scratch(fresh_dir("bm-fsstat"));
    refuse_root(&base.0);
    let python = common::created::reference_python();
    if let Err(why) = &python {
        // The kinds, sizes and errors are still held; the comparison of
        // inode numbers and times against the reference is not, and the
        // ledger says so rather than passing in silence.
        let test = "golden_fs_stat_stdfs_reference";
        eprintln!("NOT RUN {test}: inode numbers and times not compared: {why}");
        common::parity::record(
            test,
            "reference Python",
            "i",
            0,
            &[test.to_owned()],
            std::slice::from_ref(why),
        );
    }
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        if !write_tree(case, &dir) {
            failures.push(format!("#{i}: mkfifo could not run"));
            continue;
        }
        let root = dir.to_str().unwrap();
        let got = answers(&StdFs, case, root);
        if got != case["q"] {
            failures.push(format!("#{i}: native {got} reference {}", case["q"]));
        }
        // The fields the fixture cannot hold, against the reference's.
        if let Ok(python) = &python {
            let paths: Vec<String> = case["q"]
                .as_array()
                .unwrap()
                .iter()
                .map(|q| unspelled(q["p"].as_str().unwrap()))
                .filter(|p| std::str::from_utf8(p).is_ok() && !p.contains(&0))
                .map(|p| String::from_utf8(p).unwrap().replace("<root>", root))
                .collect();
            let theirs = reference_stats(python, &paths);
            for (path, want) in paths.iter().zip(theirs) {
                let mine = StdFs.stat(path.as_bytes()).ok().map(|s| {
                    json!([
                        s.ino,
                        s.mtime_ns as i64,
                        s.ctime_ns.map(|c| c as i64),
                        s.size,
                        s.mode
                    ])
                });
                if mine.unwrap_or(Value::Null) != want {
                    failures.push(format!(
                        "#{i} {path}: stat fields differ from the reference's"
                    ));
                }
            }
        }
    }
    drop(base);
    ledger.settle(cases.len(), &failures);
}

/// `errno_class.jsonl` (class i, POSIX only): the class CPython raises for
/// each errno name, against `PyErr::of_errno`; and the fixture's names are
/// exactly those `errno_name` answers for some code.
#[test]
fn errno_class_every_name() {
    use bettermemory_core::fsdata::PyErr;
    const CLASSES: &str = "errno_class.jsonl";
    let lines = jsonl(CLASSES);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(rows.len() as u64));
    let ledger = Ledger::new("golden_errno_class", CLASSES);
    let mut failures = Vec::new();
    let mut named = std::collections::BTreeSet::new();
    for row in rows {
        let name = row["n"].as_str().expect("a name");
        named.insert(name.to_owned());
        let got = PyErr::of_errno(name).name();
        if Some(got) != row["e"].as_str() {
            failures.push(format!("{name}: {got}, reference {}", row["e"]));
        }
    }
    let known: std::collections::BTreeSet<String> = (0..4096)
        .filter_map(bettermemory_sys::errno_name)
        .map(str::to_owned)
        .collect();
    if known != named {
        failures.push(format!("errno_name knows {known:?}, the fixture {named:?}"));
    }
    ledger.settle(rows.len(), &failures);
}

const ACCESS: &str = "fs_access.jsonl";

/// `os.access` for R_OK, W_OK and X_OK as `"rwx"`, and the no-follow
/// open with its kind and, for a regular file, `_read`'s bytes up to
/// `limit + 1`.
fn access_answers<F: Fs>(fs: &F, case: &Value, root: &str, limit: usize) -> Value {
    let queries = case["q"].as_array().unwrap();
    Value::Array(
        queries
            .iter()
            .map(|q| {
                let path = replaced(
                    &unspelled(q["p"].as_str().unwrap()),
                    b"<root>",
                    root.as_bytes(),
                );
                let mut a = String::new();
                let mut value = false;
                for (c, mode) in [
                    ('r', Access::READ),
                    ('w', Access::WRITE),
                    ('x', Access::EXECUTE),
                ] {
                    match fs.access(&path, mode) {
                        Ok(true) => a.push(c),
                        Ok(false) => a.push('-'),
                        Err(_) => value = true,
                    }
                }
                let a = if value {
                    json!({"e": "ValueError"})
                } else {
                    json!(a)
                };
                let o = match fs.open_no_follow(&path) {
                    Err(e) => error(e),
                    Ok(mut file) => {
                        let st = fs.fstat(&mut file).expect("fstat of an open file");
                        let mut out = json!({"k": kind(st.kind)});
                        if st.kind == Kind::Regular {
                            let mut data = Vec::new();
                            while data.len() <= limit {
                                let chunk =
                                    fs.read(&mut file, limit + 1 - data.len()).expect("a read");
                                if chunk.is_empty() {
                                    break;
                                }
                                data.extend_from_slice(&chunk);
                            }
                            out["d"] = json!(b(&data));
                        }
                        out
                    }
                };
                json!({"p": q["p"], "a": a, "o": o})
            })
            .collect(),
    )
}

fn limit_of(header: &Value) -> usize {
    usize::try_from(header["limit"].as_u64().expect("a limit")).unwrap()
}

#[test]
fn fs_access_every_case_in_memory() {
    let (header, cases) = cases_of(ACCESS);
    let ledger = Ledger::new("golden_fs_access", ACCESS);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let got = access_answers(&world(case), case, MEM_ROOT, limit_of(&header));
        if got != case["q"] {
            failures.push(format!("#{i}: native {got} reference {}", case["q"]));
        }
    }
    ledger.settle(cases.len(), &failures);
}

/// The reference Python's answers of `paths` on this host, as the
/// generator makes them (`gen_fs_access._access` and `_open`).
fn reference_access(python: &std::path::Path, limit: usize, paths: &[String]) -> Vec<Value> {
    let script = "import json, os, stat, sys\n\
        limit = int(sys.argv[1])\n\
        out = []\n\
        for p in sys.argv[2:]:\n\
        \x20   a = ''.join(c if os.access(p, m) else '-' for c, m in (('r', os.R_OK), ('w', os.W_OK), ('x', os.X_OK)))\n\
        \x20   try:\n\
        \x20       fd = os.open(p, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)\n\
        \x20   except OSError as e:\n\
        \x20       out.append([a, type(e).__name__, None])\n\
        \x20       continue\n\
        \x20   mode = os.fstat(fd).st_mode\n\
        \x20   data = os.read(fd, limit + 1) if stat.S_ISREG(mode) else b''\n\
        \x20   os.close(fd)\n\
        \x20   out.append([a, stat.S_IFMT(mode), data.hex()])\n\
        print(json.dumps(out))\n";
    let mut cmd = std::process::Command::new(python);
    cmd.env_clear()
        .arg("-I")
        .arg("-c")
        .arg(script)
        .arg(limit.to_string())
        .args(paths);
    if let Some(p) = std::env::var_os("PATH") {
        cmd.env("PATH", p);
    }
    let out = cmd.output().expect("the reference runs");
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice::<Vec<Value>>(&out.stdout).expect("JSON")
}

#[test]
fn fs_access_every_case_on_disk() {
    let (header, cases) = cases_of(ACCESS);
    let limit = limit_of(&header);
    let ledger = Ledger::new("golden_fs_access_stdfs", ACCESS);
    let base = Scratch(fresh_dir("bm-fsaccess"));
    refuse_root(&base.0);
    let python = common::created::reference_python();
    if let Err(why) = &python {
        let test = "golden_fs_access_stdfs_reference";
        eprintln!("NOT RUN {test}: the live reference not asked: {why}");
        common::parity::record(
            test,
            "reference Python",
            "i",
            0,
            &[test.to_owned()],
            std::slice::from_ref(why),
        );
    }
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        if !write_tree(case, &dir) {
            failures.push(format!("#{i}: mkfifo could not run"));
            continue;
        }
        let root = dir.to_str().unwrap();
        let got = access_answers(&StdFs, case, root, limit);
        if got != case["q"] {
            failures.push(format!("#{i}: native {got} reference {}", case["q"]));
        }
        // The same tree asked of the reference Python live: the access
        // bits, the open's class or kind, and the first read.
        if let Ok(python) = &python {
            let paths: Vec<String> = case["q"]
                .as_array()
                .unwrap()
                .iter()
                .map(|q| unspelled(q["p"].as_str().unwrap()))
                .filter(|p| std::str::from_utf8(p).is_ok() && !p.contains(&0))
                .map(|p| String::from_utf8(p).unwrap().replace("<root>", root))
                .collect();
            let theirs = reference_access(python, limit, &paths);
            for (path, want) in paths.iter().zip(theirs) {
                let p = path.as_bytes();
                let a: String = [
                    ('r', Access::READ),
                    ('w', Access::WRITE),
                    ('x', Access::EXECUTE),
                ]
                .iter()
                .map(|&(c, m)| {
                    if StdFs.access(p, m) == Ok(true) {
                        c
                    } else {
                        '-'
                    }
                })
                .collect();
                let mine = match StdFs.open_no_follow(p) {
                    Err(e) => json!([a, e.class.name(), null]),
                    Ok(mut f) => {
                        let st = StdFs.fstat(&mut f).unwrap();
                        let data = if st.kind == Kind::Regular {
                            StdFs.read(&mut f, limit + 1).unwrap()
                        } else {
                            Vec::new()
                        };
                        let ifmt = bettermemory_core::fsdata::type_bits(st.kind);
                        let hex: String = data.iter().map(|b| format!("{b:02x}")).collect();
                        json!([a, ifmt, hex])
                    }
                };
                if mine != want {
                    failures.push(format!("#{i} {path}: native {mine} reference {want}"));
                }
            }
        }
    }
    drop(base);
    ledger.settle(cases.len(), &failures);
}
