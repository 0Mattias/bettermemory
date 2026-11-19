//! Every query of `gitdir_read.jsonl` (class i) and
//! `gitdir_read_python.jsonl` (class ii, Python: by the strict walk's
//! input shape), POSIX only: `gitfile_rule._read`, `_real`,
//! `_real_to_last`, `_valid_headref`, `_git_common` and `_git_read`
//! against `gitfile_rule::gitdir` over the case's tree in memory
//! (`MemFs`) and written to a scratch directory (`StdFs`), with the
//! working directory the case's own. On disk the ungated file is also
//! asked of the reference Python live (NOT RUN without
//! `BM_REFERENCE_PYTHON`).

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{Flavour, Fs, MemFs, Node, StdFs};
use bettermemory_core::gitfile_rule::gitdir::{
    git_common, git_read, read, real, real_to_last, valid_headref, Ctx, Failure, GitRead,
};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

const PLAIN: &str = "gitdir_read.jsonl";
const GATED: &str = "gitdir_read_python.jsonl";
const MEM_ROOT: &str = "/<scratch>";

fn cases(file: &str) -> (Value, Vec<Value>) {
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

/// A file's text: a string, or `{"x": char, "n": count}`.
fn text(value: &Value, root: &str) -> Vec<u8> {
    match value {
        Value::Object(map) => map["x"]
            .as_str()
            .unwrap()
            .repeat(usize::try_from(map["n"].as_u64().unwrap()).unwrap())
            .into_bytes(),
        other => other.as_str().unwrap().replace("<root>", root).into_bytes(),
    }
}

fn failure(f: Failure) -> Value {
    match f {
        Failure::Unsettled => json!({"e": "_Unsettled"}),
        Failure::Raised(name) => json!({"e": name}),
        Failure::Value => json!({"e": "ValueError"}),
        Failure::Os(e) => match e.errno {
            Some(n) => json!({"e": e.class.name(), "n": n}),
            None => json!({"e": e.class.name()}),
        },
        Failure::NotPorted => json!({"native": "NotPorted"}),
    }
}

fn answers<F: Fs>(fs: &F, case: &Value, root: &str, limit: usize) -> Value {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: root.as_bytes(),
    };
    let spell = |p: &[u8]| json!(b(&replaced(p, root.as_bytes(), b"<scratch>")));
    let path_answer = |r: Result<Vec<u8>, Failure>| match r {
        Ok(p) => spell(&p),
        Err(f) => failure(f),
    };
    Value::Array(
        case["q"]
            .as_array()
            .unwrap()
            .iter()
            .map(|q| {
                let query = unspelled(q["p"].as_str().unwrap());
                let colon = query.iter().position(|&c| c == b':').unwrap();
                let (op, raw) = (&query[..colon], &query[colon + 1..]);
                let path = replaced(raw, b"<root>", root.as_bytes());
                let mut out = json!({"p": q["p"]});
                match op {
                    b"r" | b"n" => {
                        out["v"] = match read(&ctx, &path, limit, op == b"r") {
                            Ok(data) => json!(b(&data)),
                            Err(f) => failure(f),
                        };
                    }
                    b"R" => out["v"] = path_answer(real(&ctx, &path)),
                    b"t" => out["v"] = path_answer(real_to_last(&ctx, &path)),
                    b"h" => {
                        for (key, read_it) in [("v", true), ("u", false)] {
                            out[key] = match valid_headref(&ctx, &path, true, read_it) {
                                Ok(v) => json!(v),
                                Err(f) => failure(f),
                            };
                        }
                    }
                    b"g" => {
                        out["c"] = match git_common(&ctx, &path) {
                            Ok((p, named)) => json!([spell(&p), named]),
                            Err(f) => failure(f),
                        };
                        out["v"] = match git_read(&ctx, &path, true, true) {
                            Ok(GitRead::Found(p, named)) => json!([spell(&p), named]),
                            Ok(GitRead::NotGitDir) => Value::Null,
                            Ok(GitRead::Dies) => json!(false),
                            Err(f) => failure(f),
                        };
                    }
                    other => panic!("op {other:?}"),
                }
                out
            })
            .collect(),
    )
}

fn world(case: &Value) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    let at = |rel: &str| format!("{MEM_ROOT}/{rel}").into_bytes();
    fs.insert(MEM_ROOT.as_bytes(), Node::Dir);
    for (rel, value) in entries(case, "f") {
        fs.insert(&at(rel), Node::File(text(value, MEM_ROOT)));
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

fn limit_of(header: &Value) -> usize {
    usize::try_from(header["limit"].as_u64().unwrap()).unwrap()
}

fn in_memory(test: &str, file: &str) {
    let (header, cases) = cases(file);
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let got = answers(&world(case), case, MEM_ROOT, limit_of(&header));
        if got != case["q"] {
            failures.push(format!("#{i}: native {got} reference {}", case["q"]));
        }
    }
    ledger.settle(cases.len(), &failures);
}

#[test]
fn gitdir_read_every_case_in_memory() {
    in_memory("golden_gitdir_read", PLAIN);
}

#[test]
fn gitdir_read_gated_in_memory() {
    in_memory("golden_gitdir_read_gated", GATED);
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
                } else if std::fs::symlink_metadata(&path).is_ok_and(|m| m.is_file()) {
                    let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
                }
            }
        }
        let _ =
            std::fs::set_permissions(&self.0, std::os::unix::fs::PermissionsExt::from_mode(0o700));
        open_up(&self.0);
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

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

/// Refuse to run where a mode-000 folder can be listed (root).
fn refuse_root(base: &std::path::Path) {
    use std::os::unix::fs::PermissionsExt;
    let probe = base.join("probe");
    std::fs::create_dir(&probe).unwrap();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o000)).unwrap();
    let listed = std::fs::read_dir(&probe).is_ok();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o700)).unwrap();
    std::fs::remove_dir(&probe).unwrap();
    assert!(!listed, "a mode-000 folder can be listed here (root?)");
}

fn write_tree(case: &Value, root: &std::path::Path) -> bool {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let mode = |p: &std::path::Path, m: u32| {
        std::fs::set_permissions(p, std::fs::Permissions::from_mode(m)).unwrap()
    };
    let real = root.to_str().unwrap();
    mode(root, 0o755);
    for (rel, _) in entries(case, "d") {
        std::fs::create_dir_all(root.join(rel)).unwrap();
        mode(&root.join(rel), 0o755);
    }
    for (rel, value) in entries(case, "f") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, text(value, real)).unwrap();
        mode(&path, 0o644);
    }
    for (rel, to) in entries(case, "l") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        symlink(to.as_str().unwrap().replace("<root>", real), path).unwrap();
    }
    for (rel, _) in entries(case, "p") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let made = std::process::Command::new("mkfifo")
            .arg(&path)
            .status()
            .is_ok_and(|s| s.success());
        if !made {
            return false;
        }
        mode(&path, 0o644);
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

/// The reference's own answers of the case's queries on this tree, run
/// live in `root` by `gen_gitdir._query`'s calls: an OSError as its class
/// and errno name (`errno.errorcode`), so a wrong errno is caught against
/// the live reference too.
fn reference_answers(
    python: &std::path::Path,
    root: &str,
    limit: usize,
    queries: &[String],
) -> Vec<Value> {
    let script = "import errno, json, os, sys\n\
        from bettermemory import gitfile_rule as g\n\
        os.chdir(sys.argv[1])\n\
        real = os.getcwd()\n\
        limit = int(sys.argv[2])\n\
        def call(fn):\n\
        \x20   try:\n\
        \x20       out = fn()\n\
        \x20   except OSError as exc:\n\
        \x20       return [type(exc).__name__, errno.errorcode.get(exc.errno)]\n\
        \x20   except (ValueError, g._Unsettled) as exc:\n\
        \x20       return type(exc).__name__\n\
        \x20   if isinstance(out, bytes):\n\
        \x20       return out.hex()\n\
        \x20   if isinstance(out, str):\n\
        \x20       return out.replace(real, '<scratch>')\n\
        \x20   if isinstance(out, tuple):\n\
        \x20       return [out[0].replace(real, '<scratch>'), out[1]]\n\
        \x20   return out\n\
        out = []\n\
        for q in sys.argv[3:]:\n\
        \x20   op, _, p = q.partition(':')\n\
        \x20   p = p.replace('<root>', real)\n\
        \x20   if op in 'rn':\n\
        \x20       out.append(call(lambda: g._read(p, limit, follow=op == 'r')))\n\
        \x20   elif op == 'R':\n\
        \x20       out.append(call(lambda: g._real(p)))\n\
        \x20   elif op == 't':\n\
        \x20       out.append(call(lambda: g._real_to_last(p)))\n\
        \x20   elif op == 'h':\n\
        \x20       out.append([call(lambda: g._valid_headref(p)), call(lambda: g._valid_headref(p, read=False))])\n\
        \x20   else:\n\
        \x20       out.append([call(lambda: g._git_common(p)), call(lambda: g._git_read(p))])\n\
        print(json.dumps(out))\n";
    let mut cmd = std::process::Command::new(python);
    cmd.env_clear()
        .arg("-I")
        .arg("-c")
        .arg(script)
        .arg(root)
        .arg(limit.to_string())
        .args(queries);
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

/// The native answers in the shape `reference_answers` gives.
fn native_live<F: Fs>(fs: &F, root: &str, limit: usize, query: &str) -> Value {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: root.as_bytes(),
    };
    let name = |f: Failure| match f {
        Failure::Unsettled => json!("_Unsettled"),
        Failure::Raised(name) => json!(name),
        Failure::Value => json!("ValueError"),
        Failure::Os(e) => json!([e.class.name(), e.errno]),
        Failure::NotPorted => json!("NotPorted"),
    };
    let spell = |p: Vec<u8>| json!(String::from_utf8(p).unwrap().replace(root, "<scratch>"));
    let (op, raw) = query.split_once(':').unwrap();
    let path = raw.replace("<root>", root).into_bytes();
    let hex = |d: Vec<u8>| json!(d.iter().map(|c| format!("{c:02x}")).collect::<String>());
    match op {
        "r" | "n" => read(&ctx, &path, limit, op == "r").map_or_else(name, hex),
        "R" => real(&ctx, &path).map_or_else(name, spell),
        "t" => real_to_last(&ctx, &path).map_or_else(name, spell),
        "h" => json!([
            valid_headref(&ctx, &path, true, true).map_or_else(name, |v| json!(v)),
            valid_headref(&ctx, &path, true, false).map_or_else(name, |v| json!(v)),
        ]),
        _ => json!([
            git_common(&ctx, &path).map_or_else(name, |(p, n)| json!([spell(p), n])),
            match git_read(&ctx, &path, true, true) {
                Ok(GitRead::Found(p, n)) => json!([spell(p), n]),
                Ok(GitRead::NotGitDir) => Value::Null,
                Ok(GitRead::Dies) => json!(false),
                Err(f) => name(f),
            },
        ]),
    }
}

fn on_disk(test: &str, file: &str) {
    let (header, cases) = cases(file);
    let limit = limit_of(&header);
    let ledger = Ledger::new(test, file);
    let base = Scratch(fresh_dir("bm-gitdir"));
    refuse_root(&base.0);
    let python = (file == PLAIN).then(common::created::reference_python);
    if let Some(Err(why)) = &python {
        let test = format!("{test}_reference");
        eprintln!("NOT RUN {test}: the live reference not asked: {why}");
        common::parity::record(
            &test,
            "reference Python",
            "i",
            0,
            std::slice::from_ref(&test),
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
        let got = answers(&StdFs, case, root, limit);
        if got != case["q"] {
            failures.push(format!("#{i}: native {got} reference {}", case["q"]));
        }
        if let Some(Ok(python)) = &python {
            let queries: Vec<String> = case["q"]
                .as_array()
                .unwrap()
                .iter()
                .map(|q| unspelled(q["p"].as_str().unwrap()))
                .filter(|p| !p.contains(&0))
                .filter_map(|p| String::from_utf8(p).ok())
                .collect();
            let theirs = reference_answers(python, root, limit, &queries);
            for (query, want) in queries.iter().zip(theirs) {
                let mine = native_live(&StdFs, root, limit, query);
                if mine != want {
                    failures.push(format!("#{i} {query}: native {mine} reference {want}"));
                }
            }
        }
    }
    drop(base);
    ledger.settle(cases.len(), &failures);
}

#[test]
fn gitdir_read_every_case_on_disk() {
    on_disk("golden_gitdir_read_stdfs", PLAIN);
}

#[test]
fn gitdir_read_gated_on_disk() {
    on_disk("golden_gitdir_read_gated_stdfs", GATED);
}
