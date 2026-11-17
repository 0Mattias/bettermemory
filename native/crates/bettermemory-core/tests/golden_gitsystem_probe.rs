//! Every case of `gitsystem_probe_names.jsonl` and
//! `gitsystem_probe_names_unicode.jsonl` (any host) and, on POSIX, of
//! `gitsystem_probe.jsonl` against `gitsystem::probe`: the probe's name,
//! and the probe and the ask folder built, checked, swept and cleared in
//! trees holding links, FIFOs, modes and names that only resemble a
//! probe's. Every tree is made in a fresh scratch folder.

mod common;

use bettermemory_core::gitsystem::probe::{probe_name, PROBE_FILES};
use common::gitconfig_file::content;
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;

/// `bytes.decode("utf-8", "surrogatepass")`.
fn surrogatepass_decode(mut b: &[u8]) -> Vec<u32> {
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

fn b_spelled(data: &[u8]) -> String {
    if data
        .iter()
        .all(|&b| (0x20..0x7f).contains(&b) && b != b'\\')
    {
        return format!("={}", std::str::from_utf8(data).unwrap());
    }
    data.iter().map(|b| format!("{b:02x}")).collect()
}

fn names(test: &str, file: &str) {
    let mut lines = jsonl(file);
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    for (i, case) in lines.iter().enumerate() {
        let files: Vec<(Vec<u32>, Vec<u8>)> = case["n"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| (surrogatepass_decode(&content(&p[0])), content(&p[1])))
            .collect();
        let folders: Vec<Vec<u32>> = case["d"]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| surrogatepass_decode(&content(n)))
            .collect();
        let got = match probe_name(&files, &folders) {
            Ok(name) => name,
            Err(_) => "UnicodeEncodeError".to_owned(),
        };
        if case["r"].as_str() != Some(got.as_str()) {
            failures.push(format!("#{i}: native {got} reference {}", case["r"]));
        }
    }
    ledger.settle(lines.len(), &failures);
}

#[test]
fn gitsystem_probe_names_every_case() {
    names(
        "golden_gitsystem_probe_names",
        "gitsystem_probe_names.jsonl",
    );
}

#[test]
fn gitsystem_probe_names_unicode_every_case() {
    names(
        "golden_gitsystem_probe_names_unicode",
        "gitsystem_probe_names_unicode.jsonl",
    );
}

#[test]
fn the_probe_files_are_the_reference_s() {
    let mut lines = jsonl("gitsystem_probe_names.jsonl");
    lines.truncate(2);
    // The first case is the reference's own files and folders.
    let case = &lines[1];
    let got: Vec<(String, String)> = PROBE_FILES
        .iter()
        .map(|(n, d)| (b_spelled(n.as_bytes()), b_spelled(d)))
        .collect();
    let want: Vec<(String, String)> = case["n"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| {
            (
                p[0].as_str().unwrap().to_owned(),
                p[1].as_str().unwrap().to_owned(),
            )
        })
        .collect();
    assert_eq!(got, want);
    assert_eq!(
        Value::from(bettermemory_core::gitsystem::probe::probe_folder()),
        case["r"]
    );
}

#[cfg(unix)]
mod disk {
    use super::*;
    use bettermemory_core::fsdata::{Flavour, StdFs};
    use bettermemory_core::gitsystem::probe::{probe_folder, Refusal, PROBE_FOLDERS};
    use bettermemory_core::gitsystem::record::Place;
    use serde_json::json;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    use std::path::{Path, PathBuf};

    const FILE: &str = "gitsystem_probe.jsonl";
    const PYTHON_FILE: &str = "gitsystem_probe_python.jsonl";

    pub(crate) struct Scratch(pub PathBuf);

    impl Drop for Scratch {
        fn drop(&mut self) {
            open_up(&self.0);
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    pub(crate) fn open_up(dir: &Path) {
        let _ = std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700));
        let Ok(entries) = std::fs::read_dir(dir) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let Ok(meta) = std::fs::symlink_metadata(&path) else {
                continue;
            };
            if meta.is_dir() {
                open_up(&path);
            }
        }
    }

    pub(crate) fn fresh_dir(tag: &str) -> PathBuf {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("bm-{tag}-{}-{n}", std::process::id()));
        std::fs::create_dir(&dir).expect("a new scratch directory");
        std::fs::canonicalize(&dir).expect("a resolved scratch directory")
    }

    pub(crate) fn refuse_root(base: &Path) {
        let probe = base.join("probe");
        std::fs::create_dir(&probe).unwrap();
        set_mode(&probe, 0o000);
        let listed = std::fs::read_dir(&probe).is_ok();
        set_mode(&probe, 0o700);
        std::fs::remove_dir(&probe).unwrap();
        assert!(
            !listed,
            "a mode-000 folder can be listed here (running as root?)"
        );
    }

    pub(crate) fn set_mode(p: &Path, m: u32) {
        std::fs::set_permissions(p, std::fs::Permissions::from_mode(m)).unwrap();
    }

    pub(crate) fn mkfifo(path: &Path) {
        let made = std::process::Command::new("mkfifo")
            .arg(path)
            .status()
            .is_ok_and(|s| s.success());
        assert!(made, "mkfifo could not run");
    }

    /// A link's (or FIFO's) own modification time, whole seconds (`touch -h`).
    fn set_link_time(path: &Path, secs: i64) {
        let date = std::process::Command::new("date")
            .env("TZ", "UTC")
            .args(["-u", "-r", &secs.to_string(), "+%Y%m%d%H%M.%S"])
            .output();
        // BSD `date -r` takes seconds; GNU `date -r` takes a file.
        let stamp = match date {
            Ok(o) if o.status.success() => String::from_utf8(o.stdout).unwrap(),
            _ => {
                let o = std::process::Command::new("date")
                    .env("TZ", "UTC")
                    .args(["-u", "-d", &format!("@{secs}"), "+%Y%m%d%H%M.%S"])
                    .output()
                    .unwrap();
                String::from_utf8(o.stdout).unwrap()
            }
        };
        let ok = std::process::Command::new("touch")
            .env("TZ", "UTC")
            .args(["-h", "-t", stamp.trim()])
            .arg(path)
            .status()
            .is_ok_and(|s| s.success());
        assert!(ok, "touch -h could not run");
        let got = std::fs::symlink_metadata(path).unwrap().mtime();
        assert_eq!(got, secs, "the link's time was not set");
    }

    fn set_time(path: &Path, secs: i64, nanos: u32) {
        let meta = std::fs::symlink_metadata(path).unwrap();
        // A link's own time, or a FIFO's, which an open would wait on.
        if !(meta.is_dir() || meta.is_file()) {
            assert_eq!(nanos, 0);
            set_link_time(path, secs);
            return;
        }
        let at = std::time::UNIX_EPOCH + std::time::Duration::new(secs as u64, nanos);
        let times = std::fs::FileTimes::new().set_accessed(at).set_modified(at);
        let file = std::fs::File::open(path).unwrap();
        file.set_times(times).unwrap();
    }

    pub(crate) struct Fixture {
        probe: String,
        outside: Vec<Value>,
    }

    fn rel(fx: &Fixture, rel: &str) -> String {
        if rel == "P" || rel.starts_with("P/") {
            format!("state/{}{}", fx.probe, &rel[1..])
        } else {
            rel.to_owned()
        }
    }

    fn expanded(fx: &Fixture, tree: &[Value]) -> Vec<Value> {
        let mut out = Vec::new();
        for e in tree {
            match e[0].as_str().unwrap() {
                "O" => out.extend(fx.outside.iter().cloned()),
                "B" => {
                    let under = e[1].as_str().unwrap();
                    let skip: Vec<&str> = e[2]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|s| s.as_str().unwrap())
                        .collect();
                    out.push(json!(["d", under]));
                    for name in PROBE_FOLDERS {
                        if !skip.contains(&name) {
                            out.push(json!(["d", format!("{under}/{name}")]));
                        }
                    }
                    for (name, _) in PROBE_FILES {
                        if !skip.contains(&name) {
                            out.push(json!(["f", format!("{under}/{name}"), {"p": name}]));
                        }
                    }
                }
                _ => out.push(e.clone()),
            }
        }
        out
    }

    fn make(fx: &Fixture, root: &Path, tree: &[Value]) {
        let mut modes = Vec::new();
        for e in expanded(fx, tree) {
            let path = root.join(rel(fx, e[1].as_str().unwrap()));
            match e[0].as_str().unwrap() {
                "d" => {
                    std::fs::create_dir(&path).unwrap();
                    set_mode(&path, 0o755);
                }
                "f" => {
                    let data = match e[2].get("p") {
                        Some(name) => PROBE_FILES
                            .iter()
                            .find(|(n, _)| *n == name.as_str().unwrap())
                            .unwrap()
                            .1
                            .to_vec(),
                        None => content(&e[2]),
                    };
                    std::fs::write(&path, data).unwrap();
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
                "p" => mkfifo(&path),
                "t" => set_time(&path, e[2].as_i64().unwrap(), e[3].as_u64().unwrap() as u32),
                "m" => modes.push((path, e[2].as_u64().unwrap() as u32)),
                other => panic!("an entry of kind {other}"),
            }
        }
        for (path, mode) in modes.into_iter().rev() {
            set_mode(&path, mode);
        }
    }

    fn label(root: &str, path: &str) -> String {
        if path == root || path.starts_with(&format!("{root}/")) {
            format!("@{}", &path[root.len()..])
        } else {
            path.to_owned()
        }
    }

    fn sha16(data: &[u8]) -> String {
        use sha2::{Digest, Sha256};
        Sha256::digest(data)
            .iter()
            .take(8)
            .map(|b| format!("{b:02x}"))
            .collect()
    }

    /// The generator's `_walk`.
    pub(crate) fn walk(root: &Path) -> Vec<Value> {
        fn visit(root: &Path, path: &Path, rel: String, out: &mut Vec<Value>) {
            let meta = std::fs::symlink_metadata(path).unwrap();
            let mode = meta.mode() & 0o7777;
            let ft = meta.file_type();
            let low = mode & 0o077;
            if ft.is_symlink() {
                let target = std::fs::read_link(path).unwrap();
                let target = label(root.to_str().unwrap(), target.to_str().unwrap());
                out.push(json!([rel, "l", low, target]));
            } else if ft.is_dir() {
                out.push(json!([rel, "d", low, null]));
                if mode & 0o500 != 0o500 {
                    set_mode(path, 0o700);
                }
                let mut names: Vec<String> = std::fs::read_dir(path)
                    .unwrap()
                    .map(|e| e.unwrap().file_name().into_string().unwrap())
                    .collect();
                names.sort();
                for name in names {
                    let sub = if rel.is_empty() {
                        name.clone()
                    } else {
                        format!("{rel}/{name}")
                    };
                    visit(root, &path.join(&name), sub, out);
                }
                set_mode(path, mode);
            } else if ft.is_file() {
                if mode & 0o400 == 0 {
                    set_mode(path, 0o600);
                }
                let data = std::fs::read(path).unwrap();
                set_mode(path, mode);
                let what = if data.len() <= 48 {
                    b_spelled(&data)
                } else {
                    format!("#{}", sha16(&data))
                };
                out.push(json!([rel, "f", low, what]));
            } else {
                use std::os::unix::fs::FileTypeExt;
                let kind = if ft.is_fifo() { "p" } else { "o" };
                out.push(json!([rel, kind, low, null]));
            }
        }
        let mut out = Vec::new();
        let mut names: Vec<String> = std::fs::read_dir(root)
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        names.sort();
        for name in names {
            visit(root, &root.join(&name), name.clone(), &mut out);
        }
        out
    }

    /// The generator's `_diff`, into `line`.
    fn diff(before: &[Value], after: &[Value], line: &mut serde_json::Map<String, Value>) {
        let key = |e: &Value| e[0].as_str().unwrap().to_owned();
        let was: std::collections::BTreeMap<String, &Value> =
            before.iter().map(|e| (key(e), e)).collect();
        let now: std::collections::BTreeMap<String, &Value> =
            after.iter().map(|e| (key(e), e)).collect();
        let gone: Vec<Value> = was
            .keys()
            .filter(|p| !now.contains_key(*p))
            .map(|p| Value::from(p.clone()))
            .collect();
        let new: Vec<Value> = now
            .iter()
            .filter(|(p, e)| was.get(*p) != Some(*e))
            .map(|(_, e)| (*e).clone())
            .collect();
        if !gone.is_empty() {
            line.insert("g".into(), Value::Array(gone));
        }
        if !new.is_empty() {
            line.insert("n".into(), Value::Array(new));
        }
    }

    fn refused(e: Refusal) -> Value {
        match e {
            Refusal::Raised(e) => e.name().into(),
            Refusal::NotPorted => panic!("not ported under the POSIX flavour"),
        }
    }

    fn float_of(v: &Value) -> f64 {
        v.as_str().unwrap().parse().unwrap()
    }

    fn run(root: &Path, op: &[Value]) -> Value {
        let root_text = root.to_str().unwrap();
        let state = format!("{root_text}/state");
        let place = Place {
            fs: &StdFs,
            flavour: Flavour::Posix,
            state_dir: state.as_bytes(),
            cwd: b"/",
        };
        let probe = format!("{state}/{}", probe_folder());
        let real_probe = || {
            bettermemory_core::fsdata::realpath::realpath(
                &StdFs,
                Flavour::Posix,
                probe.as_bytes(),
                b"/",
            )
            .unwrap()
        };
        let text = |b: Vec<u8>| label(root_text, &String::from_utf8(b).unwrap());
        match op[0].as_str().unwrap() {
            "sweep" => match place.sweep_probes(float_of(&op[1])) {
                Ok(()) => Value::Null,
                Err(e) => refused(e),
            },
            "clear" => match place.clear_probe(probe.as_bytes()) {
                Ok(()) => Value::Null,
                Err(e) => refused(e),
            },
            "holds" => match place.probe_holds(&real_probe()) {
                Ok(b) => b.into(),
                Err(e) => e.name().into(),
            },
            "stamps" => Value::Array(
                place
                    .probe_stamps(&real_probe())
                    .unwrap()
                    .into_iter()
                    .map(|s| match s {
                        None => Value::Null,
                        Some(s) => json!([s[2] as i64, s[4] as i64]),
                    })
                    .collect(),
            ),
            "repo" => match place.probe_repository(float_of(&op[1])).unwrap() {
                None => Value::Null,
                Some((real, before)) => {
                    let again = place.probe_stamps(&real).unwrap();
                    json!([text(real), before == again])
                }
            },
            "where" => match place.ask_folder().unwrap() {
                None => Value::Null,
                Some((real, ceiling)) => json!([text(real), text(ceiling)]),
            },
            other => panic!("an op {other}"),
        }
    }

    pub(crate) fn fixture(file: &str) -> (Fixture, Vec<Value>) {
        let mut lines = jsonl(file);
        let header = lines.remove(0);
        assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
        assert_eq!(header["probe"].as_str(), Some(probe_folder().as_str()));
        let folders: Vec<&str> = header["folders"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(folders, PROBE_FOLDERS);
        let files: Vec<(String, Vec<u8>)> = header["files"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| (p[0].as_str().unwrap().to_owned(), content(&p[1])))
            .collect();
        let ours: Vec<(String, Vec<u8>)> = PROBE_FILES
            .iter()
            .map(|(n, d)| ((*n).to_owned(), d.to_vec()))
            .collect();
        assert_eq!(files, ours);
        let fx = Fixture {
            probe: header["probe"].as_str().unwrap().to_owned(),
            outside: header["outside"].as_array().unwrap().clone(),
        };
        (fx, lines)
    }

    /// One case run in `dir`: what it answered and changed, as the
    /// fixture spells it.
    pub(crate) fn answer(fx: &Fixture, dir: &Path, case: &Value) -> Value {
        make(fx, dir, case["t"].as_array().unwrap());
        let before = walk(dir);
        let r = run(dir, case["o"].as_array().unwrap());
        let after = walk(dir);
        let mut line = serde_json::Map::new();
        line.insert("r".into(), r);
        diff(&before, &after, &mut line);
        Value::Object(line)
    }

    fn expected(case: &Value) -> Value {
        let mut line = serde_json::Map::new();
        for k in ["r", "g", "n"] {
            if let Some(v) = case.get(k) {
                line.insert(k.into(), v.clone());
            }
        }
        Value::Object(line)
    }

    #[test]
    fn gitsystem_probe_every_case_on_disk() {
        on_disk("golden_gitsystem_probe", FILE);
    }

    #[test]
    fn gitsystem_probe_python_every_case_on_disk() {
        on_disk("golden_gitsystem_probe_python", PYTHON_FILE);
    }

    fn on_disk(test: &str, file: &str) {
        let (fx, cases) = fixture(file);
        let ledger = Ledger::new(test, file);
        let base = Scratch(fresh_dir("gitsysprobe"));
        refuse_root(&base.0);
        let mut failures = Vec::new();
        for (i, case) in cases.iter().enumerate() {
            let dir = base.0.join(i.to_string());
            std::fs::create_dir(&dir).unwrap();
            set_mode(&dir, 0o755);
            let got = answer(&fx, &dir, case);
            let want = expected(case);
            if got != want {
                failures.push(format!("#{i} {}: native {got} reference {want}", case["o"]));
            }
        }
        drop(base);
        ledger.settle(cases.len(), &failures);
    }
}
