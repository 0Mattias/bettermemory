//! Every case of `gitsystem_which_windows.jsonl` (any host) and, on POSIX,
//! of `gitsystem_ask.jsonl`, `gitsystem_ask_unicode.jsonl` and
//! `gitsystem_ask_python.jsonl` against `gitsystem::ask`: `shutil.which`
//! under Windows, and the whole ask driven by a scripted git and scripted
//! clocks, each command compared whole (argv, working directory,
//! environment, timeout), with the answer and how often each clock was
//! read. `gitsystem_which_windows_python.jsonl` holds the Windows cases
//! whose answer `shutil.which`'s version changes.

mod common;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitsystem::ask::which;
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn which_file(test: &str, file: &str) {
    let mut lines = jsonl(file);
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    let cwd = header["cwd"].as_str().unwrap().as_bytes().to_vec();
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    for (i, case) in lines.iter().enumerate() {
        let mut fs = MemFs::new(Flavour::Windows);
        for f in case["f"].as_array().unwrap() {
            fs.insert(f.as_str().unwrap().as_bytes(), Node::File(b"x".to_vec()));
        }
        for d in case["d"].as_array().unwrap() {
            fs.insert(d.as_str().unwrap().as_bytes(), Node::Dir);
        }
        let env: Vec<(Vec<u32>, Vec<u32>)> = case["e"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| (cps(p[0].as_str().unwrap()), cps(p[1].as_str().unwrap())))
            .collect();
        let got = which(&fs, Flavour::Windows, &cwd, &env, Some(&cps(".;C:\\bin")));
        let got = got.map_or(Value::Null, |g| {
            Value::from(
                g.iter()
                    .map(|&c| char::from_u32(c).unwrap())
                    .collect::<String>(),
            )
        });
        if got != case["r"] {
            failures.push(format!("#{i}: native {got} reference {}", case["r"]));
        }
    }
    ledger.settle(lines.len(), &failures);
}

#[test]
fn gitsystem_which_windows_every_case() {
    which_file(
        "golden_gitsystem_which_windows",
        "gitsystem_which_windows.jsonl",
    );
}

#[test]
fn gitsystem_which_windows_python_every_case() {
    which_file(
        "golden_gitsystem_which_windows_python",
        "gitsystem_which_windows_python.jsonl",
    );
}

/// A PATH entry naming a folder that is not UTF-8: `os.fsdecode` gives
/// the byte as U+DC80 to U+DCFF, and `os.access` writes it back as that
/// byte. No such folder can be made on macOS, so this is `MemFs`'s.
#[test]
fn which_finds_git_under_an_escaped_byte() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/b\xff", Node::Dir);
    fs.insert(b"/b\xff/git", Node::File(b"x".to_vec()));
    fs.set_mode(b"/b\xff/git", 0o755);
    let dir: Vec<u32> = vec![0x2f, 0x62, 0xdcff];
    let env = vec![(cps("PATH"), dir.clone())];
    let mut want = dir;
    want.extend(cps("/git"));
    assert_eq!(which(&fs, Flavour::Posix, b"/", &env, None), Some(want));
    // A lone surrogate outside the escapes is no name `os.access` takes.
    let env = vec![(cps("PATH"), vec![0x2f, 0x62, 0xd800])];
    assert_eq!(which(&fs, Flavour::Posix, b"/", &env, None), None);
}

#[cfg(unix)]
mod disk {
    use super::*;
    use bettermemory_core::float_repr;
    use bettermemory_core::fsdata::StdFs;
    use bettermemory_core::gitconfig::legacy::file_stamp;
    use bettermemory_core::gitsystem::ask::{Asker, Budget, Clock, Command, Declined, Git, Ran};
    use bettermemory_core::gitsystem::probe::probe_folder;
    use bettermemory_core::gitsystem::record::surrogatepass;
    use bettermemory_core::gitsystem::record::{Answer, Place};
    use common::gitconfig_file::content;
    use serde_json::json;
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};

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

    /// `bytes.decode("utf-8", "surrogatepass")`.
    fn passed(mut b: &[u8]) -> Vec<u32> {
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

    fn text_of(v: &Value) -> Vec<u32> {
        passed(&content(v))
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
        let dir = std::env::temp_dir().join(format!("bm-gitsysask-{}-{n}", std::process::id()));
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

    /// `gen_gitsystem_probe._make`, for the entries the ask's trees use.
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
                "l" => {
                    let target = e[2].as_str().unwrap();
                    let target = match target.strip_prefix("@/") {
                        Some(r) => root.join(r),
                        None => PathBuf::from(target),
                    };
                    std::os::unix::fs::symlink(target, &path).unwrap();
                }
                "p" => {
                    let ok = std::process::Command::new("mkfifo")
                        .arg(&path)
                        .status()
                        .is_ok_and(|s| s.success());
                    assert!(ok, "mkfifo could not run");
                }
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
            let cwd = t(&lab(
                &self.root_text,
                &bettermemory_core::gitsystem::record::surrogateescape(&c.cwd),
            ));
            let env: Vec<Value> = c
                .env
                .iter()
                .map(|(k, v)| json!([t(k), t(&lab(&self.root_text, v))]))
                .collect();
            self.asked
                .push((json!([argv, cwd, env]), float_repr::repr(c.timeout)));
            assert!(!self.responses.is_empty(), "the script ran out");
            let mut r = self.responses.remove(0);
            if let Some(d) = r.get("d") {
                r = self.defaults[d.as_u64().unwrap() as usize].clone();
            }
            match r.as_str() {
                Some("n") => return Ran::NotRun,
                Some("t") => return Ran::TimedOut,
                _ => {}
            }
            for w in r.get("w").and_then(Value::as_array).into_iter().flatten() {
                std::fs::write(self.root.join(rel(w[0].as_str().unwrap())), content(&w[1]))
                    .unwrap();
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
        wall: Vec<f64>,
        reads: [usize; 2],
    }

    impl Clock for Clocks {
        fn monotonic(&mut self) -> f64 {
            let v = self.mono[self.reads[0]];
            self.reads[0] += 1;
            v
        }
        fn wall(&mut self) -> f64 {
            let v = self.wall[self.reads[1]];
            self.reads[1] += 1;
            v
        }
    }

    fn floats(v: &Value) -> Vec<f64> {
        v.as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap().parse().unwrap())
            .collect()
    }

    fn answer_json(root: &[u32], cwd: &[u8], a: &Answer) -> Value {
        let s = &a.sources;
        let opt = |o: &Option<Vec<u32>>| o.as_ref().map_or(Value::Null, |v| t(v));
        let stamp = a.stamp.as_ref().map(|st| {
            let git: Vec<u8> = a
                .git
                .as_ref()
                .map(|g| bettermemory_core::gitsystem::record::surrogatepass(g))
                .unwrap();
            let git = bettermemory_core::gitconfig::paths::opened(Flavour::Posix, &git, cwd);
            let fresh = file_stamp(&StdFs, &git).unwrap();
            fresh.is_some_and(|f| f.iter().zip(st).all(|(x, y)| x.to_string() == *y))
        });
        json!({
            "g": a.git.as_ref().map_or(Value::Null, |g| t(&lab(root, g))),
            "st": stamp,
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
        })
    }

    fn run_case(header: &Value, root: &Path, case: &Value) -> Value {
        let root_text: Vec<u32> = cps(root.to_str().unwrap());
        let tree = case.get("t").unwrap_or(&header["tree"]);
        make(root, tree.as_array().unwrap());
        let env: Vec<(Vec<u32>, Vec<u32>)> = case
            .get("e")
            .unwrap_or(&header["env"])
            .as_array()
            .unwrap()
            .iter()
            .map(|p| (text_of(&p[0]), unlab(&root_text, &text_of(&p[1]))))
            .collect();
        let cs: Option<Vec<u32>> = match case.get("cs") {
            None | Some(Value::Null) => None,
            Some(v) => Some(unlab(&root_text, &text_of(v))),
        };
        let defaults = header["git"].as_array().unwrap();
        let responses = case
            .get("g")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_else(|| defaults.clone());
        let mono = match case.get("c") {
            Some(v) => floats(v),
            None => {
                let c = &header["clock"];
                let start: f64 = c[0].as_str().unwrap().parse().unwrap();
                let step: f64 = c[1].as_str().unwrap().parse().unwrap();
                (0..c[2].as_u64().unwrap())
                    .map(|i| start + i as f64 * step)
                    .collect()
            }
        };
        let wall = case.get("wc").map_or(vec![2e9], floats);
        let mut budget = match case.get("b") {
            None => Budget::Inactive,
            Some(v) if v == "new" => Budget::Active { deadline: None },
            Some(v) => Budget::Active {
                deadline: Some(v.as_str().unwrap().parse().unwrap()),
            },
        };
        let state = format!("{}/state", root.to_str().unwrap());
        let place = Place {
            fs: &StdFs,
            flavour: Flavour::Posix,
            state_dir: state.as_bytes(),
            cwd: root.to_str().unwrap().as_bytes(),
        };
        let mut git = Script {
            root,
            root_text: root_text.clone(),
            responses,
            defaults,
            asked: Vec::new(),
        };
        let mut clock = Clocks {
            mono,
            wall,
            reads: [0, 0],
        };
        let key = case.get("k").and_then(Value::as_str).unwrap_or("k");
        let now: f64 = case
            .get("now")
            .and_then(Value::as_str)
            .unwrap_or("1000.0")
            .parse()
            .unwrap();
        let got = Asker {
            place: &place,
            git: &mut git,
            clock: &mut clock,
            env: &env,
            cs_path: cs.as_deref(),
            budget: &mut budget,
        }
        .ask(key, now);
        let r = match got {
            Ok(a) => answer_json(&root_text, root.to_str().unwrap().as_bytes(), &a),
            Err(Declined::Raised(c)) => json!(c),
            Err(Declined::NotPorted) => json!("NotPorted"),
        };
        let table = header["commands"].as_array().unwrap();
        let q: Vec<Value> = git
            .asked
            .iter()
            .map(|(cmd, timeout)| match table.iter().position(|c| c == cmd) {
                Some(i) => json!([i, timeout]),
                None => json!([cmd, timeout]),
            })
            .collect();
        let mut out = json!({"q": q, "r": r, "n": clock.reads});
        if let Budget::Active { deadline } = budget {
            out["bd"] = deadline.map_or(Value::Null, |d| json!(float_repr::repr(d)));
        }
        out
    }

    fn file(test: &str, name: &str) {
        let mut lines = jsonl(name);
        let header = lines.remove(0);
        assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
        let ledger = Ledger::new(test, name);
        let base = Scratch(fresh_dir());
        let mut failures = Vec::new();
        for (i, case) in lines.iter().enumerate() {
            let dir = base.0.join(i.to_string());
            std::fs::create_dir(&dir).unwrap();
            set_mode(&dir, 0o755);
            let got = run_case(&header, &dir, case);
            let mut want = json!({"q": case["q"], "r": case["r"], "n": case["n"]});
            if let Some(bd) = case.get("bd") {
                want["bd"] = bd.clone();
            }
            if got != want {
                failures.push(format!("#{i}: native {got}\n reference {want}"));
            }
        }
        drop(base);
        ledger.settle(lines.len(), &failures);
    }

    #[test]
    fn gitsystem_ask_every_case() {
        file("golden_gitsystem_ask", "gitsystem_ask.jsonl");
    }

    #[test]
    fn gitsystem_ask_unicode_every_case() {
        file(
            "golden_gitsystem_ask_unicode",
            "gitsystem_ask_unicode.jsonl",
        );
    }

    #[test]
    fn gitsystem_ask_python_every_case() {
        file("golden_gitsystem_ask_python", "gitsystem_ask_python.jsonl");
    }
}
