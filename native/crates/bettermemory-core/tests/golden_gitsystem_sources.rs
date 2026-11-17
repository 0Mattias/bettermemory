//! Every case of `gitsystem_sources.jsonl` and
//! `gitsystem_sources_python.jsonl` against `gitsystem::sources`: sequences
//! of `sources`, `reads_compat` and `is_at_least` over one held cache, with
//! git and the clocks scripted, each step's commands, answer, clock reads,
//! held answers and record compared. POSIX only: the ask reads modes on
//! disk.

#![cfg(unix)]

mod common;

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use bettermemory_core::float_repr;
use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitconfig::legacy::file_stamp;
use bettermemory_core::gitsystem::ask::{Budget, Clock, Command, Declined, Env, Git, Ran};
use bettermemory_core::gitsystem::probe::probe_folder;
use bettermemory_core::gitsystem::record::{
    surrogateescape, surrogatepass, Answer, Place, Refused, Sources,
};
use bettermemory_core::gitsystem::sources::{Caller, Fault, Held};
use bettermemory_core::json::{dumps, loads, DumpOptions};
use bettermemory_core::pyvalue::PyValue;
use common::gitconfig_file::content;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn b_spelled(data: &[u8]) -> String {
    if data
        .iter()
        .all(|&b| (0x20..0x7f).contains(&b) && b != b'\\')
    {
        return format!("={}", std::str::from_utf8(data).unwrap());
    }
    data.iter().map(|b| format!("{b:02x}")).collect()
}

/// The generator's `_t`: `_sp` of the text's `surrogatepass` bytes.
fn t(text: &[u32]) -> Value {
    let data = surrogatepass(text);
    let mut runs: Vec<(Vec<u8>, usize)> = Vec::new();
    let mut plain = Vec::new();
    let mut i = 0;
    while i < data.len() {
        let mut j = i;
        while j < data.len() && data[j] == data[i] {
            j += 1;
        }
        if j - i >= 32 {
            if !plain.is_empty() {
                runs.push((std::mem::take(&mut plain), 1));
            }
            runs.push((vec![data[i]], j - i));
        } else {
            plain.extend_from_slice(&data[i..j]);
        }
        i = j;
    }
    if runs.is_empty() {
        return json!(b_spelled(&data));
    }
    if !plain.is_empty() {
        runs.push((plain, 1));
    }
    json!(runs
        .iter()
        .map(|(p, n)| json!([b_spelled(p), n]))
        .collect::<Vec<_>>())
}

/// `bytes.decode("utf-8", "surrogatepass")` of a fixture's text.
fn text_of(v: &Value) -> Vec<u32> {
    let mut b: &[u8] = &content(v);
    let mut out = Vec::new();
    loop {
        match std::str::from_utf8(b) {
            Ok(s) => {
                out.extend(s.chars().map(u32::from));
                return out;
            }
            Err(e) => {
                let (good, rest) = b.split_at(e.valid_up_to());
                out.extend(std::str::from_utf8(good).unwrap().chars().map(u32::from));
                let [0xed, x @ 0xa0..=0xbf, y @ 0x80..=0xbf, ..] = rest else {
                    panic!("not surrogatepass UTF-8");
                };
                out.push(0xd000 | (u32::from(x & 0x3f) << 6) | u32::from(y & 0x3f));
                b = &rest[3..];
            }
        }
    }
}

struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        open_up(&self.0);
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn open_up(dir: &Path) {
    let _ = std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700));
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if std::fs::symlink_metadata(&path).is_ok_and(|m| m.is_dir()) {
            open_up(&path);
        }
    }
}

fn fresh_dir() -> PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bm-gitsyssrc-{}-{n}", std::process::id()));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    std::fs::canonicalize(&dir).expect("a resolved scratch directory")
}

fn set_mode(p: &Path, m: u32) {
    std::fs::set_permissions(p, std::fs::Permissions::from_mode(m)).unwrap();
}

fn rel(r: &str) -> String {
    if r == "P" || r.starts_with("P/") {
        format!("state/{}{}", probe_folder(), &r[1..])
    } else {
        r.to_owned()
    }
}

fn link_target(root: &Path, target: &str) -> PathBuf {
    match target.strip_prefix("@/") {
        Some(r) => root.join(r),
        None => PathBuf::from(target),
    }
}

/// `gen_gitsystem_probe._make`, for the entries these trees use.
fn make(root: &Path, tree: &[Value]) {
    let mut modes = Vec::new();
    for e in tree {
        let path = root.join(rel(e[1].as_str().unwrap()));
        match e[0].as_str().unwrap() {
            "d" => {
                std::fs::create_dir(&path).unwrap();
                set_mode(&path, 0o755);
            }
            "f" => {
                std::fs::write(&path, content(&e[2])).unwrap();
                set_mode(&path, 0o644);
            }
            "l" => std::os::unix::fs::symlink(link_target(root, e[2].as_str().unwrap()), &path)
                .unwrap(),
            "m" => modes.push((path, e[2].as_u64().unwrap() as u32)),
            other => panic!("an entry of kind {other}"),
        }
    }
    for (path, mode) in modes.into_iter().rev() {
        set_mode(&path, mode);
    }
}

/// `text.replace(str(root), "@")`.
fn lab(root: &[u32], text: &[u32]) -> Vec<u32> {
    let mut out = Vec::with_capacity(text.len());
    let mut i = 0;
    while i < text.len() {
        if !root.is_empty() && text[i..].starts_with(root) {
            out.push(u32::from(b'@'));
            i += root.len();
        } else {
            out.push(text[i]);
            i += 1;
        }
    }
    out
}

/// `text.replace("@", str(root))`.
fn unlab(root: &[u32], text: &[u32]) -> Vec<u32> {
    let mut out = Vec::with_capacity(text.len());
    for &c in text {
        if c == u32::from(b'@') {
            out.extend_from_slice(root);
        } else {
            out.push(c);
        }
    }
    out
}

struct Script<'r> {
    root: &'r Path,
    root_text: Vec<u32>,
    responses: Vec<Value>,
    defaults: &'r [Value],
    asked: Vec<(Value, String)>,
}

impl Git for Script<'_> {
    fn run(&mut self, c: &Command) -> Ran {
        let argv: Vec<Value> = c.argv.iter().map(|a| t(&lab(&self.root_text, a))).collect();
        let cwd = t(&lab(&self.root_text, &surrogateescape(&c.cwd)));
        let env: Vec<Value> = c
            .env
            .iter()
            .map(|(k, v)| json!([t(k), t(&lab(&self.root_text, v))]))
            .collect();
        self.asked
            .push((json!([argv, cwd, env]), float_repr::repr(c.timeout)));
        assert!(!self.responses.is_empty(), "the script ran out");
        let mut r = self.responses.remove(0);
        if let Some(cp) = r.get("cp") {
            std::fs::copy(
                self.root.join(cp[0].as_str().unwrap()),
                self.root.join(cp[1].as_str().unwrap()),
            )
            .unwrap();
        }
        if let Some(gone) = r.get("rm") {
            std::fs::remove_file(self.root.join(gone.as_str().unwrap())).unwrap();
        }
        if let Some(d) = r.get("d") {
            r = self.defaults[d.as_u64().unwrap() as usize].clone();
        }
        match r.as_str() {
            Some("n") => return Ran::NotRun,
            Some("t") => return Ran::TimedOut,
            _ => {}
        }
        for w in r.get("w").and_then(Value::as_array).into_iter().flatten() {
            std::fs::write(self.root.join(rel(w[0].as_str().unwrap())), content(&w[1])).unwrap();
        }
        let mut stdout = content(&r["o"]);
        stdout.truncate(c.limit + 1);
        Ran::Exited {
            status: r["x"].as_i64().unwrap() as i32,
            stdout,
        }
    }
}

struct Clocks {
    mono: Vec<f64>,
    wall: f64,
    reads: [usize; 2],
}

impl Clock for Clocks {
    fn monotonic(&mut self) -> f64 {
        let v = self.mono[self.reads[0]];
        self.reads[0] += 1;
        v
    }
    fn wall(&mut self) -> f64 {
        assert!(self.reads[1] < 8, "the wall clock ran out");
        self.reads[1] += 1;
        self.wall
    }
}

/// Keys labelled `K<i>` by first use, and the root as `@`.
struct Labels {
    root: Vec<u32>,
    keys: Vec<String>,
}

impl Labels {
    fn add(&mut self, key: String) {
        if !self.keys.contains(&key) {
            self.keys.push(key);
        }
    }

    fn text(&self, text: &[u32]) -> String {
        let mut s: String = lab(&self.root, text)
            .iter()
            .map(|&c| char::from_u32(c).unwrap())
            .collect();
        for (i, k) in self.keys.iter().enumerate() {
            s = s.replace(k.as_str(), &format!("K{i}"));
        }
        s
    }
}

fn fresh_stamp(git: &[u32]) -> Option<[String; 5]> {
    file_stamp(&StdFs, &surrogatepass(git))
        .unwrap()
        .map(|f| f.map(|x| x.to_string()))
}

fn sources_json(s: &Sources) -> Value {
    let opt = |o: &Option<Vec<u32>>| o.as_ref().map_or(Value::Null, |v| t(v));
    json!({
        "own": s.own.iter().map(|o| t(o)).collect::<Vec<_>>(),
        "sy": opt(&s.system),
        "lg": s.legacy,
        "rs": s.resets,
        "px": opt(&s.prefix),
        "cd": s.compat_differ,
        "hc": s.hasconfig,
        "v": s.version.as_ref().map(|v| v.clone().map(|p| p.as_str().to_owned())),
    })
}

fn held_json(root: &[u32], a: &Answer) -> Value {
    let s = &a.sources;
    let opt = |o: &Option<Vec<u32>>| o.as_ref().map_or(Value::Null, |v| t(v));
    let st = a
        .stamp
        .as_ref()
        .map(|st| fresh_stamp(a.git.as_ref().unwrap()).as_ref() == Some(st));
    json!({
        "g": a.git.as_ref().map_or(Value::Null, |g| t(&lab(root, g))),
        "st": st,
        "a": float_repr::repr(a.asked),
        "ok": a.ok,
        "own": s.own.iter().map(|o| t(o)).collect::<Vec<_>>(),
        "sy": opt(&s.system),
        "lg": s.legacy,
        "rs": s.resets,
        "px": opt(&s.prefix),
        "cd": s.compat_differ,
        "hc": s.hasconfig,
        "v": s.version.as_ref().map(|v| v.clone().map(|p| p.as_str().to_owned())),
        "t": a.tried.map(float_repr::repr),
    })
}

/// The generator's `_rec`.
fn rec_json(labels: &Labels, entries_table: &mut Vec<String>, data: &[u8]) -> Value {
    let text: Vec<u32> = data.iter().map(|&b| u32::from(b)).collect();
    let layout = || -> Option<Vec<PyValue>> {
        let raw = loads(&text).ok()?;
        let (Some(format), Some(PyValue::List(entries))) = (raw.get("format"), raw.get("entries"))
        else {
            return None;
        };
        let whole = PyValue::Dict(vec![
            (PyValue::str("format"), format.clone()),
            (PyValue::str("entries"), PyValue::List(entries.clone())),
        ]);
        let mut again = dumps(&whole, DumpOptions::DEFAULT).ok()?;
        again.push(u32::from(b'\n'));
        if again != text || *format != PyValue::Int("5".into()) {
            return None;
        }
        Some(entries.clone())
    };
    let Some(entries) = layout() else {
        return json!(labels.text(&text));
    };
    let mut out = Vec::new();
    for mut entry in entries {
        if let PyValue::Dict(pairs) = &mut entry {
            let git = pairs
                .iter()
                .find(|(k, _)| *k == PyValue::str("git"))
                .and_then(|(_, v)| match v {
                    PyValue::Str(s) => Some(s.clone()),
                    _ => None,
                });
            for (k, v) in pairs.iter_mut() {
                if *k == PyValue::str("stamp") {
                    if let PyValue::List(parts) = v {
                        let have: Vec<String> = parts
                            .iter()
                            .map(|p| match p {
                                PyValue::Int(s) => s.clone(),
                                _ => String::new(),
                            })
                            .collect();
                        let fresh = git.as_deref().and_then(fresh_stamp);
                        *v = PyValue::Bool(fresh.is_some_and(|f| f.to_vec() == have));
                    }
                }
            }
        }
        let dumped = labels.text(&dumps(&entry, DumpOptions::DEFAULT).unwrap());
        let i = entries_table
            .iter()
            .position(|e| *e == dumped)
            .unwrap_or_else(|| {
                entries_table.push(dumped);
                entries_table.len() - 1
            });
        out.push(i);
    }
    json!(out)
}

fn fault_name(f: &Fault) -> Value {
    match f {
        Fault::Declined(Declined::Raised(c)) => json!(c),
        Fault::Declined(Declined::NotPorted) | Fault::Refused(Refused::NotPorted) => {
            json!("NotPorted")
        }
        Fault::Refused(Refused::Raises(e)) => json!(format!("{e:?}")),
    }
}

/// One case's steps as the native build answers them; record entries are
/// by index into the fixture's table, extended where native writes
/// another.
fn run_case(header: &Value, root: &Path, case: &Value, entries: &mut Vec<String>) -> Vec<Value> {
    let root_text = cps(root.to_str().unwrap());
    make(
        root,
        case.get("t").unwrap_or(&header["tree"]).as_array().unwrap(),
    );
    let defaults = header["git"].as_array().unwrap();
    let answers_table = header["answers"].as_array().unwrap();
    let mac = case.get("mac").and_then(Value::as_bool).unwrap_or(false);
    let link = format!("{}/xcode", root.to_str().unwrap());
    let state = format!("{}/state", root.to_str().unwrap());
    let place = Place {
        fs: &StdFs,
        flavour: Flavour::Posix,
        state_dir: state.as_bytes(),
        cwd: root.to_str().unwrap().as_bytes(),
    };
    let record = root.join("state/git-config-sources-v5.json");
    let mut held = Held::new();
    let mut labels = Labels {
        root: root_text.clone(),
        keys: Vec::new(),
    };
    let mut env: Env = vec![(cps("PATH"), unlab(&root_text, &cps("@/bin")))];
    let mut before: Option<Vec<u8>> = None;
    let mut out = Vec::new();
    for step in case["s"].as_array().unwrap() {
        if let Some(e) = step.get("e") {
            env = e
                .as_array()
                .unwrap()
                .iter()
                .map(|p| (text_of(&p[0]), unlab(&root_text, &text_of(&p[1]))))
                .collect();
        }
        for a in step
            .get("pre")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let kind = a[0].as_str().unwrap();
            if kind == "clear" {
                held.clear();
                continue;
            }
            let path = root.join(a[1].as_str().unwrap());
            match kind {
                "w" => {
                    if !path.parent().unwrap().exists() {
                        std::fs::create_dir(path.parent().unwrap()).unwrap();
                        set_mode(path.parent().unwrap(), 0o700);
                    }
                    std::fs::write(&path, content(&a[2])).unwrap();
                }
                "rm" => std::fs::remove_file(&path).unwrap(),
                "l" => {
                    if std::fs::symlink_metadata(&path).is_ok() {
                        std::fs::remove_file(&path).unwrap();
                    }
                    std::os::unix::fs::symlink(link_target(root, a[2].as_str().unwrap()), &path)
                        .unwrap();
                }
                "m" => set_mode(&path, a[2].as_u64().unwrap() as u32),
                "edit" => {
                    let path = root.join("state/git-config-sources-v5.json");
                    let text: Vec<u32> = std::fs::read(&path)
                        .unwrap()
                        .iter()
                        .map(|&b| u32::from(b))
                        .collect();
                    let mut raw = loads(&text).unwrap();
                    // The value as JSON text, parsed exactly (`json.loads`).
                    let value = loads(&cps(a[2].as_str().unwrap())).unwrap();
                    if let PyValue::Dict(top) = &mut raw {
                        for (k, v) in top.iter_mut() {
                            if *k == PyValue::str("entries") {
                                if let PyValue::List(es) = v {
                                    if let PyValue::Dict(first) = &mut es[0] {
                                        for (fk, fv) in first.iter_mut() {
                                            if *fk == PyValue::str(a[1].as_str().unwrap()) {
                                                *fv = value.clone();
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    let mut out = dumps(&raw, DumpOptions::DEFAULT).unwrap();
                    out.push(u32::from(b'\n'));
                    let bytes: Vec<u8> = out.iter().map(|&c| c as u8).collect();
                    std::fs::write(&path, bytes).unwrap();
                }
                "cp" => {
                    std::fs::copy(&path, root.join(a[2].as_str().unwrap())).unwrap();
                }
                other => panic!("a step action {other}"),
            }
        }
        let responses = step
            .get("g")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_else(|| defaults.clone());
        let mut git = Script {
            root,
            root_text: root_text.clone(),
            responses,
            defaults,
            asked: Vec::new(),
        };
        let c = &header["clock"];
        let start: f64 = c[0].as_str().unwrap().parse().unwrap();
        let every: f64 = c[1].as_str().unwrap().parse().unwrap();
        let mut clock = Clocks {
            mono: (0..c[2].as_u64().unwrap())
                .map(|i| start + i as f64 * every)
                .collect(),
            wall: step["now"].as_str().unwrap().parse().unwrap(),
            reads: [0, 0],
        };
        let mut budget = match step.get("b") {
            None => Budget::Inactive,
            Some(v) if v == "new" => Budget::Active { deadline: None },
            Some(v) => Budget::Active {
                deadline: Some(v.as_str().unwrap().parse().unwrap()),
            },
        };
        let mut caller = Caller {
            place: &place,
            git: &mut git,
            clock: &mut clock,
            env: &env,
            cs_path: None,
            budget: &mut budget,
            macos: mac,
            link: link.as_bytes(),
        };
        labels.add(caller.key());
        let do_ = &step["do"];
        let r = match do_.as_str() {
            Some("s") | Some("s0") => caller
                .sources(&mut held, do_ == "s")
                .map(|s| sources_json(&s)),
            Some("rc") => caller.reads_compat(&mut held).map(|v| json!(v)),
            _ => caller
                .is_at_least(
                    &mut held,
                    (do_[0].as_u64().unwrap(), do_[1].as_u64().unwrap()),
                )
                .map(|v| json!(v)),
        };
        let r = r.unwrap_or_else(|f| fault_name(&f));
        let h: Vec<Value> = held
            .entries()
            .iter()
            .map(|(k, a)| {
                let a = held_json(&root_text, a);
                let i = answers_table.iter().position(|x| *x == a);
                json!([labels.text(&cps(k)), i.map_or(a, |i| json!(i))])
            })
            .collect();
        let table = header["commands"].as_array().unwrap();
        let q: Vec<Value> = git
            .asked
            .iter()
            .map(|(cmd, timeout)| match table.iter().position(|c| c == cmd) {
                Some(i) => json!([i, timeout]),
                None => json!([cmd, timeout]),
            })
            .collect();
        let mut line = json!({"q": q, "r": r, "n": clock.reads, "h": h});
        let now = std::fs::read(&record).ok();
        if now != before {
            line["rec"] = match &now {
                None => Value::Null,
                Some(d) => rec_json(&labels, entries, d),
            };
            before = now;
        }
        out.push(line);
    }
    out
}

fn file(test: &str, name: &str) {
    let mut lines = jsonl(name);
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    let ledger = Ledger::new(test, name);
    let mut entries: Vec<String> = header["entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e.as_str().unwrap().to_owned())
        .collect();
    let base = Scratch(fresh_dir());
    let mut failures = Vec::new();
    for (i, case) in lines.iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        set_mode(&dir, 0o755);
        let got = run_case(&header, &dir, case, &mut entries);
        for (j, (g, w)) in got.iter().zip(case["s"].as_array().unwrap()).enumerate() {
            let mut want = json!({"q": w["q"], "r": w["r"], "n": w["n"], "h": w["h"]});
            if let Some(rec) = w.get("rec") {
                want["rec"] = rec.clone();
            }
            if *g != want {
                failures.push(format!("#{i} step {j}: native {g}\n reference {want}"));
                break;
            }
        }
    }
    drop(base);
    ledger.settle(lines.len(), &failures);
}

#[test]
fn gitsystem_sources_every_case() {
    file("golden_gitsystem_sources", "gitsystem_sources.jsonl");
}

#[test]
fn gitsystem_sources_python_every_case() {
    file(
        "golden_gitsystem_sources_python",
        "gitsystem_sources_python.jsonl",
    );
}
