//! Every query of `gitfile_trust.jsonl` (class i) and
//! `gitfile_trust_python.jsonl` (class ii, Python: `_git_common`'s strict
//! walk over a `commondir` with dot parts, by input), POSIX only: the
//! format from files and the trust of a `commondir` (`tree_format`,
//! `own_refusal`, `inside_reading`, `reads_worktree_config`,
//! `object_format_at`, `_common_of`, `_commondir`, `_foreign_commondir`,
//! `_registered`, `_is_git_directory`) against `gitfile_rule::trust` over
//! the case's tree in memory (`MemFs`) and written to a scratch directory
//! (`StdFs`), the working directory the case's own and the git on PATH
//! as the case's data. On disk the ungated file is also asked of the
//! reference Python live (NOT RUN without `BM_REFERENCE_PYTHON`).

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{Flavour, Fs, MemFs, Node, StdFs};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::gitfile_rule::trust::{
    common_of, commondir, foreign_commondir, inside_reading, is_git_directory, object_format_at,
    own_refusal, reads_worktree_config, registered, tree_format, Refusal,
};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const PLAIN: &str = "gitfile_trust.jsonl";
const GATED: &str = "gitfile_trust_python.jsonl";
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

/// A file's bytes: a string (with `<root>`), `{"p", "x", "n"}` or
/// `{"hex"}`.
fn text(value: &Value, root: &str) -> Vec<u8> {
    match value {
        Value::Object(map) if map.contains_key("hex") => {
            common::unhex(map["hex"].as_str().unwrap())
        }
        Value::Object(map) => {
            let mut out = map
                .get("p")
                .and_then(Value::as_str)
                .unwrap_or("")
                .as_bytes()
                .to_vec();
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
        Failure::Os(e) => match e.errno {
            Some(n) => json!({"e": e.class.name(), "n": n}),
            None => json!({"e": e.class.name()}),
        },
    }
}

fn facts(case: &Value) -> GitFacts {
    let release = match case.get("gv").and_then(Value::as_str) {
        None => Some((2, 54)),
        Some("none") => None,
        Some(gv) => {
            let (major, minor) = gv.split_once('.').unwrap();
            Some((major.parse().unwrap(), minor.parse().unwrap()))
        }
    };
    GitFacts {
        release,
        reads_compat: Some(true),
    }
}

/// A path or text, `<scratch>` for the root; one over 256 bytes by digest.
fn spelled(data: &[u8], root: &str) -> Value {
    let data = replaced(data, root.as_bytes(), b"<scratch>");
    if data.len() > 256 {
        let digest: String = Sha256::digest(&data)
            .iter()
            .map(|x| format!("{x:02x}"))
            .collect();
        return json!({"n": data.len(), "sha256": digest});
    }
    json!(b(&data))
}

fn refusal(r: Option<Refusal>, root: &str) -> Value {
    match r {
        None => Value::Null,
        Some(r) => json!({"why": spelled(&r.why, root), "u": r.format_unknown}),
    }
}

/// One query's answer, in the fixture's spelling.
fn answer<F: Fs>(fs: &F, root: &str, git: &GitFacts, op: &[u8], a: &[Vec<u8>]) -> Value {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: root.as_bytes(),
    };
    let path = |r: Result<Option<Vec<u8>>, Failure>| match r {
        Ok(Some(p)) => spelled(&p, root),
        Ok(None) => Value::Null,
        Err(f) => failure(f),
    };
    let env = a.get(1).map(Vec::as_slice);
    let p = &a[0];
    match op {
        b"f" => match tree_format(&ctx, p, None, git) {
            Ok(t) => match t.why {
                Some(why) => json!({"why": spelled(&why, root)}),
                None => json!({
                    "wt": t.worktree.map_or(Value::Null, |w| spelled(&w, root)),
                    "bare": t.bare,
                }),
            },
            Err(f) => failure(f),
        },
        b"o" => own_refusal(&ctx, p, &a[1], None, git).map_or_else(failure, |r| refusal(r, root)),
        b"I" => inside_reading(&ctx, p, git).map_or_else(failure, |(why, at)| {
            json!([
                refusal(why, root),
                at.map_or(Value::Null, |r| spelled(&r, root))
            ])
        }),
        b"w" => reads_worktree_config(&ctx, p, git).map_or_else(failure, |v| json!(v)),
        b"a" => object_format_at(&ctx, p, git)
            .map_or_else(failure, |v| v.map_or(Value::Null, |f| json!(b(&f)))),
        b"c" => {
            common_of(&ctx, p, env).map_or_else(failure, |(c, n)| json!([spelled(&c, root), n]))
        }
        b"n" => path(commondir(&ctx, p)),
        b"F" => path(foreign_commondir(&ctx, p)),
        b"r" => registered(&ctx, p, &a[1]).map_or_else(failure, |v| json!(v)),
        b"i" => is_git_directory(&ctx, p, env).map_or_else(failure, |v| json!(v)),
        other => panic!("op {other:?}"),
    }
}

fn answers<F: Fs>(fs: &F, case: &Value, root: &str) -> Value {
    let git = facts(case);
    Value::Array(
        case["q"]
            .as_array()
            .unwrap()
            .iter()
            .map(|q| {
                let query = unspelled(q["p"].as_str().unwrap());
                let (op, raw) = split(&query);
                json!({"p": q["p"], "v": answer(fs, root, &git, op, &args(raw, root))})
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
    ledger.settle(cases.len(), &failures);
}

#[test]
fn gitfile_trust_every_case_in_memory() {
    in_memory("golden_gitfile_trust", PLAIN);
}

#[test]
fn gitfile_trust_gated_in_memory() {
    in_memory("golden_gitfile_trust_gated", GATED);
}

fn write_tree(case: &Value, root: &std::path::Path) {
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
        symlink(to.as_str().unwrap().replace("<root>", real), root.join(rel)).unwrap();
    }
    let mut modes = entries(case, "m");
    modes.sort_by_key(|(rel, _)| std::cmp::Reverse(rel.matches('/').count()));
    for (rel, m) in modes {
        let m = u32::from_str_radix(m.as_str().unwrap(), 8).unwrap();
        std::fs::set_permissions(root.join(rel), std::fs::Permissions::from_mode(m)).unwrap();
    }
}

/// The reference's own answers on this tree, run live in `root` as
/// `gen_trust._query` asks them, in the fixture's spelling.
fn reference_answers(
    python: &std::path::Path,
    root: &str,
    gv: &str,
    queries: &[String],
) -> Vec<Value> {
    let script = "import errno, hashlib, json, os, sys\n\
        from bettermemory import gitfile_rule as g\n\
        os.chdir(sys.argv[1])\n\
        real = os.getcwd()\n\
        gv = sys.argv[2]\n\
        rel = None if gv == 'none' else tuple(int(x) for x in gv.split('.'))\n\
        g._git_is_at_least = lambda want: None if rel is None else rel >= want\n\
        g._reads_compat = lambda: True\n\
        def b(d):\n\
        \x20   if all(0x20 <= c < 0x7f and c != 0x5c for c in d):\n\
        \x20       return '=' + d.decode('ascii')\n\
        \x20   return d.hex()\n\
        def sp(t):\n\
        \x20   d = t.replace(real, '<scratch>').encode('utf-8', 'surrogateescape')\n\
        \x20   if len(d) > 256:\n\
        \x20       return {'n': len(d), 'sha256': hashlib.sha256(d).hexdigest()}\n\
        \x20   return b(d)\n\
        def ref(r):\n\
        \x20   return None if r is None else {'why': sp(r), 'u': isinstance(r, g.FormatUnknown)}\n\
        def call(fn):\n\
        \x20   try:\n\
        \x20       return fn()\n\
        \x20   except OSError as exc:\n\
        \x20       return {'e': type(exc).__name__, 'n': errno.errorcode.get(exc.errno)}\n\
        \x20   except ValueError as exc:\n\
        \x20       return {'e': type(exc).__name__}\n\
        def tf(p):\n\
        \x20   t = g.tree_format(p)\n\
        \x20   if t.why is not None:\n\
        \x20       return {'why': sp(t.why)}\n\
        \x20   return {'wt': None if t.worktree is None else sp(t.worktree), 'bare': t.bare}\n\
        def ins(p):\n\
        \x20   why, at = g.inside_reading(p)\n\
        \x20   return [ref(why), None if at is None else sp(at)]\n\
        def opt(x):\n\
        \x20   return None if x is None else sp(x)\n\
        out = []\n\
        for q in sys.argv[3:]:\n\
        \x20   op, _, raw = q.partition(':')\n\
        \x20   a = [x.replace('<root>', real) for x in raw.split('|')]\n\
        \x20   os.environ.pop('GIT_COMMON_DIR', None)\n\
        \x20   if len(a) > 1 and op in 'ci':\n\
        \x20       os.environ['GIT_COMMON_DIR'] = a[1]\n\
        \x20   p = a[0]\n\
        \x20   fn = {\n\
        \x20       'f': lambda: tf(p),\n\
        \x20       'o': lambda: ref(g.own_refusal(p, a[1])),\n\
        \x20       'I': lambda: ins(p),\n\
        \x20       'w': lambda: g.reads_worktree_config(p),\n\
        \x20       'a': lambda: (lambda v: None if v is None else b(v))(g.object_format_at(p)),\n\
        \x20       'c': lambda: (lambda c: [sp(c[0]), c[1]])(g._common_of(p)),\n\
        \x20       'n': lambda: opt(g._commondir(p)),\n\
        \x20       'F': lambda: opt(g._foreign_commondir(p)),\n\
        \x20       'r': lambda: g._registered(p, a[1]),\n\
        \x20       'i': lambda: g._is_git_directory(p),\n\
        \x20   }[op]\n\
        \x20   out.append(call(fn))\n\
        print(json.dumps(out))\n";
    let mut cmd = std::process::Command::new(python);
    cmd.env_clear()
        .arg("-I")
        .arg("-c")
        .arg(script)
        .arg(root)
        .arg(gv)
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
    let base = Scratch(fresh_dir("bm-trust"));
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
        if let Some(Ok(python)) = &python {
            let gv = case.get("gv").and_then(Value::as_str).unwrap_or("2.54");
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
            let theirs = reference_answers(python, root, gv, &queries);
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
fn gitfile_trust_every_case_on_disk() {
    on_disk("golden_gitfile_trust_stdfs", PLAIN);
}

#[test]
fn gitfile_trust_gated_on_disk() {
    on_disk("golden_gitfile_trust_gated_stdfs", GATED);
}
