//! Every query of `gitfile_follow.jsonl` (class i) and
//! `gitfile_follow_python.jsonl` (class ii, Python: `_git_common`'s strict
//! walk, a loop, a record past the JSON decoder's recursion limit, by
//! input), POSIX only: `_refusal`, `_entry_names`, `earliest_naming`,
//! `gitdir_pin.recorded` and `_spells` against `gitfile_rule::follow` over
//! the case's tree in memory (`MemFs`) and written to a scratch directory
//! (`StdFs`), with the case's pin records as data. On disk the ungated
//! file is also asked of the reference Python live (NOT RUN without
//! `BM_REFERENCE_PYTHON`).

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{samestat, Flavour, Fs, MemFs, Node, StatResult, StdFs};
use bettermemory_core::gitfile_rule::follow::{
    earliest_naming, entry_names, fsencode, recorded, refusal, spells, Records,
};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::gitfile_rule::trust::Refusal;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

const PLAIN: &str = "gitfile_follow.jsonl";
const GATED: &str = "gitfile_follow_python.jsonl";
const MEM_ROOT: &str = "/<scratch>";
const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

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
/// The query's arguments, split at `|`, with `<root>` in place.
fn args(raw: &[u8], root: &str) -> Vec<Vec<u8>> {
    raw.split(|&c| c == b'|')
        .map(|a| replaced(a, b"<root>", root.as_bytes()))
        .collect()
}

fn split(query: &[u8]) -> (&[u8], &[u8]) {
    let colon = query.iter().position(|&c| c == b':').unwrap();
    (&query[..colon], &query[colon + 1..])
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

/// A file's bytes: a string (with `<root>`), `{"p", "x", "n"}`,
/// `{"hex"}` or `{"nest"}`.
fn text(value: &Value, root: &str) -> Vec<u8> {
    match value {
        Value::Object(map) if map.contains_key("nest") => {
            let n = usize::try_from(map["nest"].as_u64().unwrap()).unwrap();
            let mut out = vec![b'['; n];
            out.resize(2 * n, b']');
            out
        }
        Value::Object(map) if map.contains_key("hex") => {
            common::unhex(map["hex"].as_str().unwrap())
        }
        Value::Object(map) => {
            let mut out = map
                .get("p")
                .and_then(Value::as_str)
                .unwrap_or("")
                .replace("<root>", root)
                .into_bytes();
            out.extend(
                map["x"]
                    .as_str()
                    .unwrap()
                    .repeat(usize::try_from(map["n"].as_u64().unwrap()).unwrap())
                    .into_bytes(),
            );
            out
        }
        other => other.as_str().unwrap().replace("<root>", root).into_bytes(),
    }
}

fn failure(f: Failure) -> Value {
    match f {
        Failure::NotPorted => json!({"native": "NotPorted"}),
        Failure::Value => json!({"e": "ValueError"}),
        Failure::Unsettled => json!({"e": "_Unsettled"}),
        Failure::Raised(name) => json!({"e": name}),
        Failure::Os(e) => match e.errno {
            Some(n) => json!({"e": e.class.name(), "n": n}),
            None => json!({"e": e.class.name()}),
        },
    }
}

fn spelled(data: &[u8], root: &str) -> Value {
    json!(b(&replaced(data, root.as_bytes(), b"<scratch>")))
}

fn refused(r: Option<Refusal>, root: &str) -> Value {
    match r {
        None => Value::Null,
        Some(r) => json!({"why": spelled(&r.why, root), "u": r.format_unknown}),
    }
}

/// The case's records, by the stat of each git directory they were
/// written for.
struct Kept(Vec<(StatResult, Vec<u8>)>);

impl Records for Kept {
    fn record(&self, git_dir: &StatResult) -> Option<Vec<u8>> {
        self.0
            .iter()
            .rev()
            .find(|(st, _)| samestat(st, git_dir) == Some(true))
            .map(|(_, data)| data.clone())
    }
}

fn kept<F: Fs>(fs: &F, case: &Value, root: &str) -> Kept {
    Kept(
        entries(case, "r")
            .into_iter()
            .map(|(rel, value)| {
                let st = fs
                    .stat(format!("{root}/{rel}").as_bytes())
                    .expect("a recorded git directory");
                (st, text(value, root))
            })
            .collect(),
    )
}

/// One query's answer, in the fixture's spelling.
fn answer<F: Fs>(fs: &F, records: &Kept, root: &str, op: &[u8], a: &[Vec<u8>]) -> Value {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: root.as_bytes(),
    };
    match op {
        b"g" => refusal(&ctx, fs_answers(), records, &a[0], &a[1], None, None, &GIT)
            .map_or_else(failure, |(why, provisional)| {
                json!([refused(why, root), provisional])
            }),
        b"e" => entry_names(&ctx, &a[0], &a[1]).map_or_else(failure, |n| json!(n.word())),
        b"E" => {
            let views: Vec<Vec<u8>> = a[2]
                .split(|&c| c == b';')
                .filter(|v| !v.is_empty())
                .map(<[u8]>::to_vec)
                .collect();
            let tree = (a[0] != b"-").then_some(a[0].as_slice());
            earliest_naming(&ctx, fs_answers(), &views, tree, &a[1])
                .map_or_else(failure, |v| v.map_or(Value::Null, |p| spelled(&p, root)))
        }
        b"p" => recorded(&ctx, records, &a[0]).map_or_else(failure, |pin| {
            pin.map_or(Value::Null, |pin| {
                json!([
                    spelled(&fsencode(&pin.git_dir).unwrap(), root),
                    spelled(&fsencode(&pin.tree).unwrap(), root)
                ])
            })
        }),
        b"s" => {
            let rec: Vec<u32> = String::from_utf8(a[0].clone())
                .unwrap()
                .chars()
                .map(u32::from)
                .collect();
            spells(&ctx, &rec, &a[1]).map_or_else(failure, |v| json!(v))
        }
        other => panic!("op {other:?}"),
    }
}

/// Every path answers: `MemFs` and the local disk never hang.
fn fs_answers() -> &'static bettermemory_core::gitfile_rule::bounded::Hung {
    static NONE: std::sync::OnceLock<bettermemory_core::gitfile_rule::bounded::Hung> =
        std::sync::OnceLock::new();
    NONE.get_or_init(Default::default)
}

fn answers<F: Fs>(fs: &F, records: &Kept, case: &Value, root: &str) -> Value {
    Value::Array(
        case["q"]
            .as_array()
            .unwrap()
            .iter()
            .map(|q| {
                let query = unspelled(q["p"].as_str().unwrap());
                let (op, raw) = split(&query);
                json!({"p": q["p"], "v": answer(fs, records, root, op, &args(raw, root))})
            })
            .collect(),
    )
}

fn world(case: &Value) -> (MemFs, Kept) {
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
    let records = kept(&fs, case, MEM_ROOT);
    for (rel, mode) in entries(case, "m") {
        fs.set_mode(
            &at(rel),
            u32::from_str_radix(mode.as_str().unwrap(), 8).unwrap(),
        );
    }
    (fs, records)
}

fn compare(got: &Value, case: &Value, i: usize, failures: &mut Vec<String>) {
    for (mine, theirs) in got
        .as_array()
        .unwrap()
        .iter()
        .zip(case["q"].as_array().unwrap())
    {
        if mine != theirs {
            failures.push(format!("#{i}: native {mine} reference {theirs}"));
        }
    }
}

fn in_memory(test: &str, file: &str) {
    let (_, cases) = cases(file);
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let (fs, records) = world(case);
        compare(
            &answers(&fs, &records, case, MEM_ROOT),
            case,
            i,
            &mut failures,
        );
    }
    ledger.settle(cases.len(), &failures);
}

#[test]
fn gitfile_follow_every_case_in_memory() {
    in_memory("golden_gitfile_follow", PLAIN);
}

#[test]
fn gitfile_follow_gated_in_memory() {
    in_memory("golden_gitfile_follow_gated", GATED);
}

/// The tree on disk, and its records.
fn write_tree(case: &Value, root: &std::path::Path) -> Kept {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let real = root.to_str().unwrap();
    for (rel, _) in entries(case, "d") {
        std::fs::create_dir_all(root.join(rel)).unwrap();
    }
    for (rel, value) in entries(case, "f") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, text(value, real)).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
    }
    for (rel, to) in entries(case, "l") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        symlink(to.as_str().unwrap().replace("<root>", real), path).unwrap();
    }
    let records = kept(&StdFs, case, real);
    let mut modes = entries(case, "m");
    modes.sort_by_key(|(rel, _)| std::cmp::Reverse(rel.matches('/').count()));
    for (rel, m) in modes {
        let m = u32::from_str_radix(m.as_str().unwrap(), 8).unwrap();
        std::fs::set_permissions(root.join(rel), std::fs::Permissions::from_mode(m)).unwrap();
    }
    records
}

/// The reference's own answers on this tree, run live in `root` as
/// `gen_follow._query` asks them, with the case's records written to a
/// store of its own.
fn reference_answers(
    python: &std::path::Path,
    root: &str,
    pins: &std::path::Path,
    records: &[(String, Vec<u8>)],
    queries: &[String],
) -> Vec<Value> {
    let script = "import errno, json, os, sys\n\
        from pathlib import Path\n\
        from bettermemory import gitdir_pin\n\
        from bettermemory import gitfile_rule as g\n\
        os.chdir(sys.argv[1])\n\
        real = os.getcwd()\n\
        pins = Path(sys.argv[2])\n\
        pins.mkdir()\n\
        recs = json.loads(sys.argv[3])\n\
        for rel, hexed in recs:\n\
        \x20   (pins / gitdir_pin._name(os.stat(os.path.join(real, rel)))).write_bytes(bytes.fromhex(hexed))\n\
        gitdir_pin.pins_dir = lambda: pins\n\
        g._git_is_at_least = lambda want: (2, 54) >= want\n\
        g._reads_compat = lambda: True\n\
        g._RECORDED_BUDGET = 60.0\n\
        os.environ.pop('GIT_COMMON_DIR', None)\n\
        def b(d):\n\
        \x20   if all(0x20 <= c < 0x7f and c != 0x5c for c in d):\n\
        \x20       return '=' + d.decode('ascii')\n\
        \x20   return d.hex()\n\
        def sp(t):\n\
        \x20   return b(t.replace(real, '<scratch>').encode('utf-8', 'surrogateescape'))\n\
        def ref(r):\n\
        \x20   return None if r is None else {'why': sp(r), 'u': isinstance(r, g.FormatUnknown)}\n\
        def call(fn):\n\
        \x20   try:\n\
        \x20       return fn()\n\
        \x20   except OSError as exc:\n\
        \x20       return {'e': type(exc).__name__, 'n': errno.errorcode.get(exc.errno)}\n\
        \x20   except (ValueError, RecursionError) as exc:\n\
        \x20       return {'e': type(exc).__name__}\n\
        def refuse(a):\n\
        \x20   why, prov = g._refusal(a[0], a[1])\n\
        \x20   return [ref(why), prov]\n\
        def early(a):\n\
        \x20   found = g.earliest_naming([v for v in a[2].split(';') if v], None if a[0] == '-' else a[0], a[1])\n\
        \x20   return None if found is None else sp(found)\n\
        def pin(a):\n\
        \x20   p = gitdir_pin.recorded(a[0])\n\
        \x20   return None if p is None else [sp(p.git_dir), sp(p.tree)]\n\
        out = []\n\
        for q in sys.argv[4:]:\n\
        \x20   op, _, raw = q.partition(':')\n\
        \x20   a = [x.replace('<root>', real) for x in raw.split('|')]\n\
        \x20   fn = {\n\
        \x20       'g': lambda: refuse(a),\n\
        \x20       'e': lambda: g._entry_names(a[0], a[1]),\n\
        \x20       'E': lambda: early(a),\n\
        \x20       'p': lambda: pin(a),\n\
        \x20       's': lambda: g._spells(a[0], a[1]),\n\
        \x20   }[op]\n\
        \x20   out.append(call(fn))\n\
        print(json.dumps(out))\n";
    let recs: Vec<(String, String)> = records
        .iter()
        .map(|(rel, data)| (rel.clone(), common::hex(data)))
        .collect();
    let mut cmd = std::process::Command::new(python);
    cmd.env_clear()
        .arg("-I")
        .arg("-c")
        .arg(script)
        .arg(root)
        .arg(pins)
        .arg(serde_json::to_string(&recs).unwrap())
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

fn on_disk(test: &str, file: &str) {
    let (_, cases) = cases(file);
    let ledger = Ledger::new(test, file);
    let base = Scratch(fresh_dir("bm-follow"));
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
        let records = write_tree(case, &dir);
        let root = dir.to_str().unwrap();
        let got = answers(&StdFs, &records, case, root);
        compare(&got, case, i, &mut failures);
        if let Some(Ok(python)) = &python {
            let asked: Vec<(String, &Value)> = got
                .as_array()
                .unwrap()
                .iter()
                .filter_map(|q| {
                    let query = unspelled(q["p"].as_str().unwrap());
                    (!query.contains(&0))
                        .then(|| String::from_utf8(query).ok())
                        .flatten()
                        .map(|s| (s, &q["v"]))
                })
                .collect();
            let queries: Vec<String> = asked.iter().map(|(q, _)| q.clone()).collect();
            let recs: Vec<(String, Vec<u8>)> = entries(case, "r")
                .into_iter()
                .map(|(rel, value)| (rel.to_owned(), text(value, root)))
                .collect();
            let theirs = reference_answers(
                python,
                root,
                &base.0.join(format!("pins-{i}")),
                &recs,
                &queries,
            );
            for ((query, mine), want) in asked.iter().zip(theirs) {
                if **mine != want {
                    failures.push(format!("#{i} {query}: native {mine} reference {want}"));
                }
            }
        }
    }
    drop(base);
    ledger.settle(cases.len(), &failures);
}

#[test]
fn gitfile_follow_every_case_on_disk() {
    on_disk("golden_gitfile_follow_stdfs", PLAIN);
}

#[test]
fn gitfile_follow_gated_on_disk() {
    on_disk("golden_gitfile_follow_gated_stdfs", GATED);
}
