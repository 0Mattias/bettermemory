//! Every case of `verify_candidates_keys.jsonl`,
//! `verify_candidates_keys_unicode.jsonl` and
//! `verify_candidates_keys_python.jsonl` against `verify_candidates::keys`:
//! each fixture's tree is built again under a scratch folder on this
//! machine's disk (`StdFs`) and in memory (`MemFs`), `@` standing for it,
//! and every answer and every `lstat`, `stat` and `readlink` that reached
//! the OS compared. A row without an answer is one whose answer is the
//! volume's (the case-swapped home probed): its calls up to that probe
//! are compared, and
//! `the_reference_probes_as_the_port_on_this_volume` asks the reference
//! itself on this machine's scratch volume (`BM_REFERENCE_PYTHON`).
//!
//! The fixtures follow `posixpath`, so they exist on POSIX only
//! (`verify_candidates_keys_not_posix`).

#![cfg(unix)]

mod common;
mod verify_candidates_keys_fs;

use std::os::unix::ffi::OsStrExt;

use bettermemory_core::fsdata::{Fs, StdFs};
use bettermemory_core::verify_candidates::keys::{
    claim_key, home_ignores_case, is_multi_segment_routelike, is_under_home, path_exists,
    ClaimKeys, Raised, RECURSION_LIMIT,
};
use bettermemory_core::verify_candidates::scan::UserHome;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use verify_candidates_keys_fs::{
    ascii_swapcase, at, build_in_memory, build_on_disk, call_bytes, cps, kept_calls, lookup, spell,
    text, Traced,
};

fn raised(e: Raised) -> Value {
    let name = match e {
        Raised::Value => "ValueError",
        Raised::Recursion => "RecursionError",
        Raised::Home(_) => "RuntimeError",
        Raised::NotPorted => panic!("declined on POSIX"),
    };
    json!({ "raise": name })
}

/// One file system with the fixture's tree under `root`.
struct Place<'a, F: Fs + ?Sized> {
    fs: Traced<'a, F>,
    root: Vec<u32>,
    flipped: Vec<u32>,
    root_bytes: Vec<u8>,
    users: &'a Value,
    /// Whether a call relative to the working folder is answered as the
    /// process's own (only on the disk).
    cwd: bool,
}

impl<F: Fs + ?Sized> Place<'_, F> {
    /// The calls made against the fixture's; `upto_probe` keeps only those
    /// up to the probe of the flipped home, after which the volume decides.
    fn calls_match(&self, want: &Value, upto_probe: bool) {
        let mut got = kept_calls(self.fs.take(), &self.root_bytes);
        if upto_probe {
            got.truncate(want.as_array().map_or(0, Vec::len));
        }
        let want: Vec<(String, Vec<u8>)> = want
            .as_array()
            .expect("calls")
            .iter()
            .map(|c| {
                (
                    c[0].as_str().unwrap().to_owned(),
                    call_bytes(&c[1], &self.root, &self.flipped),
                )
            })
            .collect();
        let got: Vec<(String, Vec<u8>)> = got.into_iter().map(|(n, p)| (n.to_owned(), p)).collect();
        assert_eq!(got, want, "calls");
    }

    fn spell_key(&self, r: Result<Vec<u32>, Raised>) -> Value {
        match r {
            Ok(k) => spell(&k, &self.root, &self.flipped),
            Err(e) => raised(e),
        }
    }

    fn check(&self, row: &Value) {
        let home = row["home"].as_str().map(|h| at(&cps(h), &self.root));
        let home = home.as_deref();
        let mut users = lookup(self.users, &self.root);
        let f = row["f"].as_str().unwrap();
        if f == "keys" {
            return self.check_read(row, home, &mut users);
        }
        let a = at(&text(&row["a"]), &self.root);
        if !self.cwd && row["calls"].to_string().contains("[\"stat\",\".\"]") {
            return;
        }
        let fs = &self.fs;
        let got = match f {
            "claim_key" => self.spell_key(claim_key(fs, &a, home, &mut users)),
            "under_home" => flag(is_under_home(fs, &a, home, &mut users)),
            "routelike" => flag(is_multi_segment_routelike(fs, &a, home, &mut users)),
            "exists" => flag(path_exists(fs, &a, home, &mut users)),
            "ignores_case" => flag(home_ignores_case(fs, &a)),
            other => panic!("unknown row {other}"),
        };
        if let Some(want) = row.get("r") {
            assert_eq!(&got, want);
        }
        self.calls_match(&row["calls"], row.get("r").is_none());
    }

    fn check_read(
        &self,
        row: &Value,
        home: Option<&[u32]>,
        users: &mut dyn FnMut(&[u32]) -> UserHome,
    ) {
        let room = row["room"].as_u64().map_or(RECURSION_LIMIT, |r| r as usize);
        let mut keys = ClaimKeys::new(room);
        let read = row["read"].as_array().unwrap();
        let answers = row["r"].as_array().unwrap();
        assert_eq!(read.len(), answers.len());
        for (path, want) in read.iter().zip(answers) {
            if row.get("room").is_some() {
                let spec = path.as_array().unwrap();
                let one = [
                    cps(spec[0].as_str().unwrap()),
                    cps(spec[1].as_str().unwrap()).repeat(spec[2].as_u64().unwrap() as usize),
                ]
                .concat();
                let got = match keys.key(&self.fs, &one, home, users) {
                    Ok(k) => {
                        let s: String = k.iter().map(|&c| char::from_u32(c).unwrap()).collect();
                        json!({ "sha256": hex(&Sha256::digest(s.as_bytes())) })
                    }
                    Err(e) => raised(e),
                };
                assert_eq!(&got, want, "{path}");
                self.fs.take();
                continue;
            }
            let p = at(&text(path), &self.root);
            let got = self.spell_key(keys.key(&self.fs, &p, home, users));
            assert_eq!(got, want[0], "{path}");
            self.calls_match(&want[1], false);
        }
    }
}

fn flag(r: Result<bool, Raised>) -> Value {
    match r {
        Ok(b) => json!(b),
        Err(e) => raised(e),
    }
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn every_case(test: &str, file: &str) {
    let lines = common::jsonl(file);
    let header = &lines[0];
    let rows = &lines[1..];
    assert_eq!(header["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let base = std::env::temp_dir().join(format!("bm-vck-{}-{}", test, std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).unwrap();
    let disk_root = base.canonicalize().unwrap().join("R");
    std::fs::create_dir(&disk_root).unwrap();
    build_on_disk(&disk_root, &header["tree"]);
    let disk_bytes = disk_root.as_os_str().as_bytes().to_vec();
    let disk_text = cps(std::str::from_utf8(&disk_bytes).expect("a UTF-8 scratch root"));
    let mem_bytes = b"/mem/R".to_vec();
    let mem = build_in_memory(&mem_bytes, &header["tree"]);
    let mut ledger = Ledger::new(test, file);
    for (i, row) in rows.iter().enumerate() {
        ledger.case(format!("line {} {}", i + 2, row), || {
            let disk = Place {
                fs: Traced::new(&StdFs),
                flipped: ascii_swapcase(&disk_text),
                root: disk_text.clone(),
                root_bytes: disk_bytes.clone(),
                users: &header["users"],
                cwd: true,
            };
            disk.check(row);
            let memory = Place {
                fs: Traced::new(&mem),
                root: cps("/mem/R"),
                flipped: cps("/MEM/r"),
                root_bytes: mem_bytes.clone(),
                users: &header["users"],
                cwd: false,
            };
            memory.check(row);
        });
    }
    let _ = std::fs::remove_dir_all(&base);
    ledger.finish(rows.len());
}

#[test]
fn verify_candidates_keys_every_case() {
    every_case(
        "verify_candidates_keys_every_case",
        "verify_candidates_keys.jsonl",
    );
}

#[test]
fn verify_candidates_keys_unicode_every_case() {
    every_case(
        "verify_candidates_keys_unicode_every_case",
        "verify_candidates_keys_unicode.jsonl",
    );
}

#[test]
fn verify_candidates_keys_python_every_case() {
    every_case(
        "verify_candidates_keys_python_every_case",
        "verify_candidates_keys_python.jsonl",
    );
}

/// The probe's answers, which follow the volume: the reference and the
/// port ask the same scratch folder on this machine, a home spelled in
/// mixed case, and every spelling of a path under it.
#[test]
fn the_reference_probes_as_the_port_on_this_volume() {
    let test = "the_reference_probes_as_the_port_on_this_volume";
    let Some(python) = common::created::reference_python()
        .map_err(|why| {
            eprintln!("NOT RUN {test}: {why}");
            common::parity::record(test, "reference Python", "i", 0, &[test.to_owned()], &[why]);
        })
        .ok()
    else {
        return;
    };
    let base = std::env::temp_dir().join(format!("bm-vck-live-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).unwrap();
    let base = base.canonicalize().unwrap();
    let home = base.join("Hm");
    std::fs::create_dir_all(home.join("Proj")).unwrap();
    std::fs::create_dir_all(base.join("api").join("v1")).unwrap();
    let h = home.to_str().unwrap().to_owned();
    let b = base.to_str().unwrap().to_owned();
    let texts: Vec<String> = vec![
        h.clone(),
        format!("{h}/Proj/x"),
        h.to_uppercase(),
        h.to_lowercase(),
        format!("{}/proj/x", h.to_lowercase()),
        format!("{}/PROJ/gone/x", h.to_uppercase()),
        format!("{}/hm/gone/x", b),
        format!("{}/HM/gone/x", b.to_uppercase()),
        format!("{}/API/v1/users", b),
        "/api/v9/x".to_owned(),
    ];
    let homes: Vec<String> = vec![h.clone(), format!("{h}/Proj"), b.clone(), h.to_uppercase()];
    let script = "import json, sys\n\
        from bettermemory import verify_candidates as vc\n\
        texts, homes = json.loads(sys.argv[1]), json.loads(sys.argv[2])\n\
        print(json.dumps([[vc._is_under_home(t), vc._is_multi_segment_routelike(t)] for t in texts]\n\
            + [vc._home_ignores_case(x) for x in homes]))\n";
    let mut cmd = common::created::reference_command(&python, &home);
    cmd.arg("-c")
        .arg(script)
        .arg(serde_json::to_string(&texts).unwrap())
        .arg(serde_json::to_string(&homes).unwrap());
    let out = cmd.output().expect("the reference runs");
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let want: Value = serde_json::from_slice(&out.stdout).unwrap();
    let hc = cps(&h);
    let mut none = |_: &[u32]| UserHome::Unknown;
    let mut got: Vec<Value> = texts
        .iter()
        .map(|t| {
            let t = cps(t);
            json!([
                is_under_home(&StdFs, &t, Some(&hc), &mut none).unwrap(),
                is_multi_segment_routelike(&StdFs, &t, Some(&hc), &mut none).unwrap()
            ])
        })
        .collect();
    got.extend(
        homes
            .iter()
            .map(|x| json!(home_ignores_case(&StdFs, &cps(x)).unwrap())),
    );
    let _ = std::fs::remove_dir_all(&base);
    assert_eq!(Value::Array(got), want);
    common::parity::record(
        test,
        "reference Python",
        "i",
        texts.len() + homes.len(),
        &[],
        &[],
    );
}
