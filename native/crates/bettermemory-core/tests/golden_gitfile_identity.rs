//! Every query of `gitfile_identity.jsonl` (class i) and
//! `gitfile_identity_python.jsonl` (class ii, Python: a link loop, by the
//! standing rule), POSIX only: `gitfile_rule._names_here`, `_names_folder`,
//! `_same_folder`, `_is_link`, `_tree_of` and `_working_tree_of` against
//! `gitfile_rule::identity` over the case's tree in memory (`MemFs`) and
//! written to a scratch directory (`StdFs`), with the working directory
//! the case's own. On disk the ungated file is also asked of the reference
//! Python live, by class and errno name (NOT RUN without
//! `BM_REFERENCE_PYTHON`).

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{Flavour, Fs, MemFs, Node, OsError, StdFs};
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::gitfile_rule::identity::{
    is_link, names_folder, names_here, same_folder, tree_of, working_tree_of,
};
use bettermemory_core::gitfile_rule::{gitfile_target, small_text};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

const PLAIN: &str = "gitfile_identity.jsonl";
const GATED: &str = "gitfile_identity_python.jsonl";
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
        Failure::NotPorted => json!({"native": "NotPorted"}),
        Failure::Value => json!({"e": "ValueError"}),
        other => panic!("nothing else rises here: {other:?}"),
    }
}

fn os_error(e: OsError) -> Value {
    match e.errno {
        Some(n) => json!({"e": e.class.name(), "n": n}),
        None => json!({"e": e.class.name()}),
    }
}

/// The query's arguments, split at `|`, with `<root>` in place.
fn args(raw: &[u8], root: &str) -> Vec<Vec<u8>> {
    raw.split(|&c| c == b'|')
        .map(|a| replaced(a, b"<root>", root.as_bytes()))
        .collect()
}

/// One query's answer: `(key, value)` pairs, in the fixture's spelling.
fn answer<F: Fs>(fs: &F, root: &str, op: &[u8], a: &[Vec<u8>]) -> Vec<(&'static str, Value)> {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: root.as_bytes(),
    };
    let truth = |r: Result<Option<bool>, Failure>| r.map_or_else(failure, |v| json!(v));
    let place = |r: Result<Option<Vec<u8>>, Failure>| match r {
        Ok(Some(p)) => json!(b(&replaced(&p, root.as_bytes(), b"<scratch>"))),
        Ok(None) => Value::Null,
        Err(f) => failure(f),
    };
    match op {
        b"h" => vec![("v", truth(names_here(&ctx, &a[0], &a[1], &a[2])))],
        b"F" => vec![("v", truth(names_folder(&ctx, &a[0], &a[1], &a[2])))],
        b"s" => vec![("v", truth(same_folder(&ctx, &a[0], &a[1])))],
        b"k" => {
            let path = ctx.at(&a[0]).expect("POSIX");
            let v = match fs.lstat_result(&path) {
                Ok(st) => is_link(&st).map_or_else(failure, |v| json!(v)),
                Err(e) => os_error(e),
            };
            vec![("v", v)]
        }
        b"G" => vec![("v", place(gitfile_target(&ctx, &a[0])))],
        b"T" => vec![(
            "v",
            place(small_text(&ctx, &a[0]).map(|t| t.map(String::into_bytes))),
        )],
        b"t" => vec![
            ("v", place(tree_of(&ctx, &a[0]))),
            ("w", place(working_tree_of(&ctx, &a[0]))),
        ],
        other => panic!("op {other:?}"),
    }
}

fn split(query: &[u8]) -> (&[u8], &[u8]) {
    let colon = query.iter().position(|&c| c == b':').unwrap();
    (&query[..colon], &query[colon + 1..])
}

fn answers<F: Fs>(fs: &F, case: &Value, root: &str) -> Value {
    Value::Array(
        case["q"]
            .as_array()
            .unwrap()
            .iter()
            .map(|q| {
                let query = unspelled(q["p"].as_str().unwrap());
                let (op, raw) = split(&query);
                let mut out = json!({"p": q["p"]});
                for (key, value) in answer(fs, root, op, &args(raw, root)) {
                    out[key] = value;
                }
                out
            })
            .collect(),
    )
}

fn many(case: &Value) -> Vec<(&str, usize)> {
    entries(case, "N")
        .into_iter()
        .map(|(rel, n)| (rel, usize::try_from(n.as_u64().unwrap()).unwrap()))
        .collect()
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
    for (rel, n) in many(case) {
        fs.insert(&at(rel), Node::Dir);
        for i in 0..n {
            fs.insert(&at(&format!("{rel}/n{i}")), Node::File(Vec::new()));
        }
    }
    for (rel, to) in entries(case, "l") {
        let target = to.as_str().unwrap().replace("<root>", MEM_ROOT);
        fs.insert(&at(rel), Node::Link(target.into_bytes()));
    }
    for (rel, to) in entries(case, "h") {
        fs.insert(&at(rel), Node::Hard(at(to.as_str().unwrap())));
    }
    for (rel, mode) in entries(case, "m") {
        fs.set_mode(
            &at(rel),
            u32::from_str_radix(mode.as_str().unwrap(), 8).unwrap(),
        );
    }
    fs
}

fn in_memory(test: &str, file: &str) {
    let (_, cases) = cases(file);
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let got = answers(&world(case), case, MEM_ROOT);
        if got != case["q"] {
            failures.push(format!("#{i}: native {got} reference {}", case["q"]));
        }
    }
    ledger.settle(cases.len(), &failures);
}

#[test]
fn gitfile_identity_every_case_in_memory() {
    in_memory("golden_gitfile_identity", PLAIN);
}

#[test]
fn gitfile_identity_gated_in_memory() {
    in_memory("golden_gitfile_identity_gated", GATED);
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

fn write_tree(case: &Value, root: &std::path::Path) {
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
        mode(path.parent().unwrap(), 0o755);
        std::fs::write(&path, text(value, real)).unwrap();
        mode(&path, 0o644);
    }
    for (rel, n) in many(case) {
        std::fs::create_dir_all(root.join(rel)).unwrap();
        for i in 0..n {
            std::fs::write(root.join(rel).join(format!("n{i}")), b"").unwrap();
        }
    }
    for (rel, to) in entries(case, "l") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        symlink(to.as_str().unwrap().replace("<root>", real), path).unwrap();
    }
    for (rel, to) in entries(case, "h") {
        std::fs::hard_link(root.join(to.as_str().unwrap()), root.join(rel)).unwrap();
    }
    let mut modes = entries(case, "m");
    modes.sort_by_key(|(rel, _)| std::cmp::Reverse(rel.matches('/').count()));
    for (rel, m) in modes {
        mode(
            &root.join(rel),
            u32::from_str_radix(m.as_str().unwrap(), 8).unwrap(),
        );
    }
}

/// The reference's own answers of the case's queries on this tree, run
/// live in `root` by `gen_identity._query`'s calls: an OSError as its
/// class and errno name (`errno.errorcode`).
fn reference_answers(python: &std::path::Path, root: &str, queries: &[String]) -> Vec<Value> {
    let script = "import errno, json, os, sys\n\
        from bettermemory import gitfile_rule as g\n\
        os.chdir(sys.argv[1])\n\
        real = os.getcwd()\n\
        def call(fn):\n\
        \x20   try:\n\
        \x20       out = fn()\n\
        \x20   except OSError as exc:\n\
        \x20       return [type(exc).__name__, errno.errorcode.get(exc.errno)]\n\
        \x20   except ValueError as exc:\n\
        \x20       return type(exc).__name__\n\
        \x20   if isinstance(out, str):\n\
        \x20       return out.replace(real, '<scratch>')\n\
        \x20   return out\n\
        out = []\n\
        for q in sys.argv[2:]:\n\
        \x20   op, _, raw = q.partition(':')\n\
        \x20   a = [x.replace('<root>', real) for x in raw.split('|')]\n\
        \x20   if op == 'h':\n\
        \x20       out.append([call(lambda: g._names_here(*a))])\n\
        \x20   elif op == 'F':\n\
        \x20       out.append([call(lambda: g._names_folder(*a))])\n\
        \x20   elif op == 's':\n\
        \x20       out.append([call(lambda: g._same_folder(*a))])\n\
        \x20   elif op == 'G':\n\
        \x20       out.append([call(lambda: g._gitfile_target(a[0]))])\n\
        \x20   elif op == 'T':\n\
        \x20       out.append([call(lambda: g._small_text(a[0]))])\n\
        \x20   elif op == 'k':\n\
        \x20       out.append([call(lambda: g._is_link(os.lstat(a[0])))])\n\
        \x20   else:\n\
        \x20       out.append([call(lambda: g._tree_of(a[0])), call(lambda: g._working_tree_of(a[0]))])\n\
        print(json.dumps(out))\n";
    let mut cmd = std::process::Command::new(python);
    cmd.env_clear()
        .arg("-I")
        .arg("-c")
        .arg(script)
        .arg(root)
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
fn native_live<F: Fs>(fs: &F, root: &str, query: &str) -> Value {
    let (op, raw) = split(query.as_bytes());
    let mut out = Vec::new();
    for (_, value) in answer(fs, root, op, &args(raw, root)) {
        out.push(match value {
            Value::String(s) => json!(String::from_utf8(unspelled(&s)).unwrap()),
            Value::Object(m) if m.contains_key("e") => json!([m["e"], m["n"]]),
            other => other,
        });
    }
    Value::Array(out)
}

fn on_disk(test: &str, file: &str) {
    let (_, cases) = cases(file);
    let ledger = Ledger::new(test, file);
    let base = Scratch(fresh_dir("bm-identity"));
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
        write_tree(case, &dir);
        let root = dir.to_str().unwrap();
        let got = answers(&StdFs, case, root);
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
            let theirs = reference_answers(python, root, &queries);
            for (query, want) in queries.iter().zip(theirs) {
                let mine = native_live(&StdFs, root, query);
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
fn gitfile_identity_every_case_on_disk() {
    on_disk("golden_gitfile_identity_stdfs", PLAIN);
}

#[test]
fn gitfile_identity_gated_on_disk() {
    on_disk("golden_gitfile_identity_gated_stdfs", GATED);
}
