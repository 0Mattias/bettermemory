//! Every query of `gitfile_discovery.jsonl` (class i) and
//! `gitfile_discovery_python.jsonl` (class ii, Python: a link loop, a
//! record past the JSON decoder's recursion limit, `_git_common`'s strict
//! walk, by input), POSIX only: `judge`, `_entry_reading`, `reading_at`,
//! `discovery_reading`, `discovery_refusal` and `toplevel_agrees` against
//! `gitfile_rule::discovery` over the case's tree in memory (`MemFs`) and
//! written to a scratch directory (`StdFs`), with the case's pin records,
//! its `GIT_COMMON_DIR` and its git release as data. On disk the ungated
//! file is also asked of the reference Python live (NOT RUN without
//! `BM_REFERENCE_PYTHON`).
//!
//! A walk that finds no repository in its case goes on to the file
//! system's root. In memory nothing is above the case. On disk its answer
//! is the fixture's wherever no folder above the scratch directory holds a
//! `.git` or a `HEAD` entry, which `clear_above` checks first.

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{samestat, Flavour, Fs, MemFs, Node, StatResult, StdFs};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::{
    discovery_reading, discovery_refusal, entry_reading, judge, reading_at, toplevel_agrees, Entry,
    Reading, Rule,
};
use bettermemory_core::gitfile_rule::follow::Records;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::gitfile_rule::trust::Refusal;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

const PLAIN: &str = "gitfile_discovery.jsonl";
const GATED: &str = "gitfile_discovery_python.jsonl";
const MEM_ROOT: &str = "/<scratch>";

/// The file's cases, and the number of queries its header says they hold.
fn cases(file: &str) -> (Vec<Value>, usize) {
    let lines = jsonl(file);
    let cases = lines[1..].to_vec();
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    assert!(!cases.is_empty(), "{file}: no case");
    let queries = lines[0]["queries"].as_u64().expect("a count of queries");
    (cases, usize::try_from(queries).unwrap())
}

/// Fails where the queries checked are none or not the file's count.
fn all_checked(file: &str, checked: usize, queries: usize) {
    assert!(checked > 0, "{file}: no query checked");
    assert_eq!(
        checked, queries,
        "{file}: checked {checked} of {queries} queries"
    );
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
        let _ = std::fs::set_permissions(&self.0, std::fs::Permissions::from_mode(0o700));
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

/// Refuse to run where the scratch directory or a folder above it holds a
/// `.git` or a `HEAD` entry (`gen_discovery._clear_above`): a walk that
/// leaves its case would read it.
fn clear_above(base: &std::path::Path) {
    for dir in base.ancestors() {
        for name in [".git", "HEAD"] {
            let entry = dir.join(name);
            assert!(
                std::fs::symlink_metadata(&entry).is_err(),
                "{} exists above the scratch directory: run this test with a \
                 temporary directory (TMPDIR) outside any repository",
                entry.display()
            );
        }
    }
}

/// A file's bytes: a string (with `<root>`), `{"p", "x", "n"}`,
/// `{"hex"}` (with `<root>`) or `{"nest"}`.
fn text(value: &Value, root: &str) -> Vec<u8> {
    match value {
        Value::Object(map) if map.contains_key("nest") => {
            let n = usize::try_from(map["nest"].as_u64().unwrap()).unwrap();
            let mut out = vec![b'['; n];
            out.resize(2 * n, b']');
            out
        }
        Value::Object(map) if map.contains_key("hex") => replaced(
            &common::unhex(map["hex"].as_str().unwrap()),
            b"<root>",
            root.as_bytes(),
        ),
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

fn entry(seen: Option<Entry>, root: &str) -> Value {
    match seen {
        None => Value::Null,
        Some(e) => json!({
            "g": spelled(&e.git_dir, root),
            "why": refused(e.why, root),
            "own": e.own,
            "prov": e.provisional,
        }),
    }
}

fn reading(found: Option<Reading>, root: &str) -> Value {
    match found {
        None => Value::Null,
        Some(r) => json!({
            "why": refused(r.why, root),
            "prov": r.provisional,
            "root": r.root.map_or(Value::Null, |p| spelled(&p, root)),
        }),
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

/// The git release the case names (`"gv"`), 2.54 otherwise.
fn git_of(case: &Value) -> GitFacts {
    let release = case.get("gv").map_or((2, 54), |gv| {
        let part = |i: usize| u32::try_from(gv[i].as_u64().unwrap()).unwrap();
        (part(0), part(1))
    });
    GitFacts {
        release: Some(release),
        reads_compat: Some(true),
    }
}

/// `GIT_COMMON_DIR` as the case sets it (`"env"`), unset otherwise.
fn env_of(case: &Value, root: &str) -> Option<Vec<u8>> {
    case.get("env")
        .map(|env| env.as_str().unwrap().replace("<root>", root).into_bytes())
}

/// What a case is answered with.
struct Given<'a, F: Fs> {
    fs: &'a F,
    records: &'a Kept,
    root: &'a str,
    env: Option<Vec<u8>>,
    git: GitFacts,
}

/// One query's answer, in the fixture's spelling.
fn answer<F: Fs>(given: &Given<'_, F>, op: &[u8], a: &[Vec<u8>]) -> Value {
    let root = given.root;
    let ctx = Ctx {
        fs: given.fs,
        flavour: Flavour::Posix,
        cwd: root.as_bytes(),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: given.records,
        env: given.env.as_deref(),
        git: &given.git,
    };
    // `os.lstat` in the generator: the entry exists.
    let lstat = |path: &[u8]| {
        given
            .fs
            .lstat_result(&ctx.at(path).expect("POSIX"))
            .expect("the fixture's lstat")
    };
    match op {
        b"j" => {
            let flags: &[u8] = a.get(2).map_or(b"", Vec::as_slice);
            let st = lstat(a.get(3).unwrap_or(&a[1]));
            let (fetch, read) = (!flags.contains(&b'F'), !flags.contains(&b'R'));
            judge(&rule, &a[0], &a[1], &st, fetch, read).map_or_else(failure, |e| entry(e, root))
        }
        b"e" => {
            let st = lstat(&a[1]);
            entry_reading(&rule, &a[0], &a[1], &st).map_or_else(failure, |r| reading(r, root))
        }
        b"r" => reading_at(&rule, &a[0]).map_or_else(failure, |r| reading(r, root)),
        b"d" => discovery_reading(&rule, &a[0]).map_or_else(failure, |r| reading(r, root)),
        b"D" => discovery_refusal(&rule, &a[0]).map_or_else(failure, |r| refused(r, root)),
        b"t" => toplevel_agrees(&rule, &a[0], &a[1]).map_or_else(failure, |v| json!(v)),
        other => panic!("op {other:?}"),
    }
}

fn answers<F: Fs>(fs: &F, records: &Kept, case: &Value, root: &str) -> Value {
    let given = Given {
        fs,
        records,
        root,
        env: env_of(case, root),
        git: git_of(case),
    };
    Value::Array(
        case["q"]
            .as_array()
            .unwrap()
            .iter()
            .map(|q| {
                let query = unspelled(q["p"].as_str().unwrap());
                let (op, raw) = split(&query);
                json!({"p": q["p"], "v": answer(&given, op, &args(raw, root))})
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
    for (rel, _) in entries(case, "p") {
        fs.insert(&at(rel), Node::Fifo);
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

/// Each query of the case against the fixture: how many were compared.
fn compare(got: &Value, case: &Value, i: usize, failures: &mut Vec<String>) -> usize {
    let (mine, theirs) = (got.as_array().unwrap(), case["q"].as_array().unwrap());
    assert_eq!(mine.len(), theirs.len());
    for (mine, theirs) in mine.iter().zip(theirs) {
        if mine != theirs {
            failures.push(format!("#{i}: native {mine} reference {theirs}"));
        }
    }
    theirs.len()
}

fn in_memory(test: &str, file: &str) {
    let (cases, queries) = cases(file);
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    let mut checked = 0;
    for (i, case) in cases.iter().enumerate() {
        let (fs, records) = world(case);
        let got = answers(&fs, &records, case, MEM_ROOT);
        checked += compare(&got, case, i, &mut failures);
    }
    all_checked(file, checked, queries);
    ledger.settle(checked, &failures);
}

#[test]
fn gitfile_discovery_every_case_in_memory() {
    in_memory("golden_gitfile_discovery", PLAIN);
}

#[test]
fn gitfile_discovery_gated_in_memory() {
    in_memory("golden_gitfile_discovery_gated", GATED);
}

/// The tree on disk, and its records; None where `mkfifo` cannot run.
fn write_tree(case: &Value, root: &std::path::Path) -> Option<Kept> {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let mode = |path: &std::path::Path, m: u32| {
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(m)).unwrap();
    };
    let real = root.to_str().unwrap();
    for (rel, _) in entries(case, "d") {
        std::fs::create_dir_all(root.join(rel)).unwrap();
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
            return None;
        }
        mode(&path, 0o644);
    }
    let records = kept(&StdFs, case, real);
    let mut modes = entries(case, "m");
    modes.sort_by_key(|(rel, _)| std::cmp::Reverse(rel.matches('/').count()));
    for (rel, m) in modes {
        mode(
            &root.join(rel),
            u32::from_str_radix(m.as_str().unwrap(), 8).unwrap(),
        );
    }
    Some(records)
}

/// What the live reference is run with, beside the case's directory.
struct Live<'a> {
    pins: &'a std::path::Path,
    records: Vec<(String, Vec<u8>)>,
    env: Option<String>,
    release: (u32, u32),
}

/// The reference's own answers on this tree, run live in `root` as
/// `gen_discovery._query` asks them, with the case's records written to a
/// store of its own.
fn reference_answers(
    python: &std::path::Path,
    root: &str,
    live: &Live<'_>,
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
        given = json.loads(sys.argv[3])\n\
        for rel, hexed in given['records']:\n\
        \x20   (pins / gitdir_pin._name(os.stat(os.path.join(real, rel)))).write_bytes(bytes.fromhex(hexed))\n\
        gitdir_pin.pins_dir = lambda: pins\n\
        release = tuple(given['release'])\n\
        g._git_is_at_least = lambda want: release >= want\n\
        g._reads_compat = lambda: True\n\
        g._RECORDED_BUDGET = 60.0\n\
        os.environ.pop('GIT_COMMON_DIR', None)\n\
        if given['env'] is not None:\n\
        \x20   os.environ['GIT_COMMON_DIR'] = given['env']\n\
        def b(d):\n\
        \x20   if all(0x20 <= c < 0x7f and c != 0x5c for c in d):\n\
        \x20       return '=' + d.decode('ascii')\n\
        \x20   return d.hex()\n\
        def sp(t):\n\
        \x20   return b(t.replace(real, '<scratch>').encode('utf-8', 'surrogateescape'))\n\
        def ref(r):\n\
        \x20   return None if r is None else {'why': sp(r), 'u': isinstance(r, g.FormatUnknown)}\n\
        def reading(r):\n\
        \x20   if r is None:\n\
        \x20       return None\n\
        \x20   return {'why': ref(r.why), 'prov': r.provisional, 'root': None if r.root is None else sp(r.root)}\n\
        def entry(e):\n\
        \x20   if e is None:\n\
        \x20       return None\n\
        \x20   return {'g': sp(e.git_dir), 'why': ref(e.why), 'own': e.own, 'prov': e.provisional}\n\
        def call(fn):\n\
        \x20   try:\n\
        \x20       return fn()\n\
        \x20   except OSError as exc:\n\
        \x20       return {'e': type(exc).__name__, 'n': errno.errorcode.get(exc.errno)}\n\
        \x20   except (ValueError, RecursionError) as exc:\n\
        \x20       return {'e': type(exc).__name__}\n\
        def judged(a):\n\
        \x20   flags = a[2] if len(a) > 2 else ''\n\
        \x20   st = os.lstat(a[3] if len(a) > 3 else a[1])\n\
        \x20   return call(lambda: entry(g.judge(a[0], a[1], st, fetch='F' not in flags, read='R' not in flags)))\n\
        def entered(a):\n\
        \x20   st = os.lstat(a[1])\n\
        \x20   return call(lambda: reading(g._entry_reading(a[0], a[1], st)))\n\
        out = []\n\
        for q in sys.argv[4:]:\n\
        \x20   op, _, raw = q.partition(':')\n\
        \x20   a = [x.replace('<root>', real) for x in raw.split('|')]\n\
        \x20   out.append({\n\
        \x20       'j': lambda: judged(a),\n\
        \x20       'e': lambda: entered(a),\n\
        \x20       'r': lambda: call(lambda: reading(g.reading_at(a[0]))),\n\
        \x20       'd': lambda: call(lambda: reading(g.discovery_reading(a[0]))),\n\
        \x20       'D': lambda: call(lambda: ref(g.discovery_refusal(a[0]))),\n\
        \x20       't': lambda: call(lambda: g.toplevel_agrees(a[0], a[1])),\n\
        \x20   }[op]())\n\
        print(json.dumps(out))\n";
    let records: Vec<(String, String)> = live
        .records
        .iter()
        .map(|(rel, data)| (rel.clone(), common::hex(data)))
        .collect();
    let given = json!({
        "records": records,
        "env": live.env,
        "release": [live.release.0, live.release.1],
    });
    let mut cmd = std::process::Command::new(python);
    cmd.env_clear()
        .arg("-I")
        .arg("-c")
        .arg(script)
        .arg(root)
        .arg(live.pins)
        .arg(given.to_string())
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
    let (cases, queries) = cases(file);
    let ledger = Ledger::new(test, file);
    let base = Scratch(fresh_dir("bm-discovery"));
    refuse_root(&base.0);
    clear_above(&base.0);
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
    let mut checked = 0;
    for (i, case) in cases.iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        let Some(records) = write_tree(case, &dir) else {
            failures.push(format!("#{i}: mkfifo could not run"));
            continue;
        };
        let root = dir.to_str().unwrap();
        let got = answers(&StdFs, &records, case, root);
        checked += compare(&got, case, i, &mut failures);
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
            let pins = base.0.join(format!("pins-{i}"));
            let live = Live {
                pins: &pins,
                records: entries(case, "r")
                    .into_iter()
                    .map(|(rel, value)| (rel.to_owned(), text(value, root)))
                    .collect(),
                env: env_of(case, root).map(|e| String::from_utf8(e).unwrap()),
                release: git_of(case).release.unwrap(),
            };
            let theirs = reference_answers(python, root, &live, &queries);
            for ((query, mine), want) in asked.iter().zip(theirs) {
                if **mine != want {
                    failures.push(format!("#{i} {query}: native {mine} reference {want}"));
                }
            }
        }
    }
    drop(base);
    // A tree that could not be written (no `mkfifo`) is a failure of its
    // own; every other case's queries are all checked.
    if failures
        .iter()
        .all(|f| !f.ends_with("mkfifo could not run"))
    {
        all_checked(file, checked, queries);
    }
    ledger.settle(checked.max(failures.len()), &failures);
}

#[test]
fn gitfile_discovery_every_case_on_disk() {
    on_disk("golden_gitfile_discovery_stdfs", PLAIN);
}

#[test]
fn gitfile_discovery_gated_on_disk() {
    on_disk("golden_gitfile_discovery_gated_stdfs", GATED);
}
