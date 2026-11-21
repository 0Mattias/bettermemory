//! Every case of `location_view_posix.jsonl`: `view_state` over a tree
//! built in scratch, with `object_key`, `_at_dotgit`, the project file's
//! parse and the history's `holds` stubbed as the generator stubs them,
//! and every key recomputed from this machine's `stat`. POSIX only.

#![cfg(unix)]

mod common;

use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use bettermemory_core::capture::{Key, Kind};
use bettermemory_core::checkout_token::{link, render, Lineage};
use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::gitfile_rule::hostpath;
use bettermemory_core::githead;
use bettermemory_core::location_probe::StdHost;
use bettermemory_core::location_stamps::{self, init_stamps, Platform};
use bettermemory_core::location_view::{
    stamp_row, view_state, Declined, Effects, Machine, Recorded,
};
use bettermemory_core::pyvalue::PyValue;
use common::location_git::{disk_tree, entries};
use common::parity::Ledger;
use serde_json::Value;

const FILE: &str = "location_view_posix.jsonl";
const T: &str = "0123456789ABCDEFGHJKMNPQRS";
const U: &str = "ABCDEFGHJKMNPQRSTVWXYZ0123";
const NONCE: &str = "0000000000000";

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn platform() -> Platform {
    if cfg!(target_os = "macos") {
        Platform::Darwin
    } else {
        Platform::Linux
    }
}

fn stub_key(path: &[u8], birth: Option<&str>) -> Option<Key> {
    let st = std::fs::metadata(std::ffi::OsStr::from_bytes(path)).ok()?;
    Some((
        cps(&st.dev().to_string()),
        cps(&st.ino().to_string()),
        birth.map(cps),
    ))
}

fn token_bytes(which: &str, git_dir: &Path) -> Vec<u8> {
    match which {
        "T" => render(&cps(T)).unwrap(),
        "U" => render(&cps(U)).unwrap(),
        _ => {
            let st = std::fs::metadata(git_dir).unwrap();
            let ino = st.ino() + u64::from(which == "succx");
            let lin = Lineage {
                token: cps(T),
                device: cps(&st.dev().to_string()),
                inode: cps(&ino.to_string()),
            };
            let tail = link(&lin, &cps(NONCE)).unwrap();
            render(&cps(&format!("{NONCE}{tail}"))).unwrap()
        }
    }
}

fn kind(name: &str) -> Kind {
    *Kind::ALL.iter().find(|k| k.as_str() == name).unwrap()
}

fn at_dotgit(
    path: &[u8],
    dotgit: &[u8],
    st: &bettermemory_core::fsdata::StatResult,
) -> Result<Option<Vec<u8>>, Declined> {
    Ok(found_at(path, dotgit, st))
}

fn found_at(
    path: &[u8],
    dotgit: &[u8],
    st: &bettermemory_core::fsdata::StatResult,
) -> Option<Vec<u8>> {
    if st.kind != bettermemory_core::fsdata::Kind::Regular {
        return None;
    }
    let data = std::fs::read(std::ffi::OsStr::from_bytes(dotgit)).ok()?;
    let line = String::from_utf8(data).ok()?;
    let rest = line.trim().strip_prefix("gitdir: ")?;
    let found = hostpath::normpath(
        Flavour::Posix,
        &hostpath::join(Flavour::Posix, path, rest.as_bytes()),
    );
    Path::new(std::ffi::OsStr::from_bytes(&found))
        .is_dir()
        .then_some(found)
}

fn run(case: &Value, base: &Path) -> String {
    let items: Vec<Value> = case["t"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|i| i[0] != "tok")
        .cloned()
        .collect();
    disk_tree(&entries(&Value::Array(items)), base);
    for item in case["t"].as_array().unwrap() {
        if item[0] == "tok" {
            let g = base.join(item[1].as_str().unwrap());
            std::fs::write(
                g.join("info/bettermemory"),
                token_bytes(item[2].as_str().unwrap(), &g),
            )
            .unwrap();
        }
    }
    let at = |rel: &str| -> Vec<u8> { base.join(rel).as_os_str().as_bytes().to_vec() };
    let k = kind(case["k"].as_str().unwrap());
    let st_of = |rel: &str| std::fs::metadata(base.join(rel)).unwrap();
    let key: Option<Key> = case.get("kd").and_then(Value::as_str).map(|kd| {
        let st = st_of(kd);
        (
            cps(&st.dev().to_string()),
            cps(&st.ino().to_string()),
            case.get("rb").and_then(Value::as_str).map(cps),
        )
    });
    let fs = StdFs;
    let machine = Machine {
        ctx: Ctx {
            fs: &fs,
            flavour: Flavour::Posix,
            cwd: b"/",
        },
        refs: &githead::StdFs,
        host: &StdHost,
        platform: platform(),
        mountinfo: b"",
        common_env: None,
    };
    let stamps: Vec<Vec<PyValue>> = match case.get("st").and_then(Value::as_str) {
        Some("same") => {
            let m = location_stamps::Machine {
                fs: &fs,
                platform: platform(),
                mountinfo: b"",
            };
            init_stamps(
                &m,
                Flavour::Posix,
                &at(case["kd"].as_str().unwrap()),
                k == Kind::Linked,
            )
            .unwrap()
            .iter()
            .map(stamp_row)
            .collect()
        }
        Some(_) => vec![vec![
            PyValue::Str(cps("description")),
            PyValue::Int("1".into()),
            PyValue::Int("2".into()),
            PyValue::Int("3".into()),
            PyValue::Int("4".into()),
        ]],
        None => Vec::new(),
    };
    let common_key: Option<Key> = match case.get("ck").and_then(Value::as_str) {
        Some("common") => {
            let st = st_of(if k == Kind::Linked {
                "main/.git"
            } else {
                "p/.git"
            });
            Some((cps(&st.dev().to_string()), cps(&st.ino().to_string()), None))
        }
        Some(_) => Some((cps("0"), cps("0"), None)),
        None => None,
    };
    let hb = case.get("hb").and_then(Value::as_str).map(str::to_owned);
    let held = case.get("held").is_some();
    let token = case.get("tk").and_then(Value::as_str);
    let last = case.get("lh").and_then(Value::as_str);
    let got = view_state(
        &machine,
        Effects {
            object_key: &mut |p| Ok(stub_key(p, hb.as_deref())),
            at_dotgit: &mut at_dotgit,
            parse_project: &mut |p| {
                Ok(std::fs::read(std::ffi::OsStr::from_bytes(p)).is_ok_and(|d| d == b"ok\n"))
            },
            holds: &mut |_, commits| Ok(held && !commits.is_empty()),
            peel: &mut |_| None,
            pause: &mut |_| {},
        },
        &at(case["p"].as_str().unwrap()),
        k,
        Recorded {
            key: key.as_ref(),
            token,
            last_head: last,
            stamps: &stamps,
            common_key: common_key.as_ref(),
            superseded: case.get("sup").is_some(),
        },
    );
    match got {
        Ok(state) => state.to_owned(),
        Err(e) => format!("declined {e:?}"),
    }
}

struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        use std::os::unix::fs::PermissionsExt;
        fn open_up(dir: &Path) {
            let Ok(list) = std::fs::read_dir(dir) else {
                return;
            };
            for entry in list.flatten() {
                let path = entry.path();
                if std::fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_symlink()) {
                    continue;
                }
                let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700));
                if path.is_dir() {
                    open_up(&path);
                }
            }
        }
        open_up(&self.0);
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn location_view_posix_every_case() {
    let lines = common::jsonl(FILE);
    assert_eq!(lines[0]["cases"].as_u64(), Some(lines.len() as u64 - 1));
    let cases = &lines[1..];
    assert!(!cases.is_empty());
    let top = std::env::temp_dir().join(format!("bm-location-view-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&top);
    std::fs::create_dir_all(&top).unwrap();
    let scratch = Scratch(std::fs::canonicalize(&top).unwrap());
    let ledger = Ledger::new("golden_location_view_posix", FILE);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let base = scratch.0.join(i.to_string());
        let got = run(case, &base);
        if got != case["a"].as_str().unwrap() {
            failures.push(format!(
                "#{i} {}: native {got} reference {}",
                case["note"], case["a"]
            ));
        }
    }
    ledger.settle(cases.len(), &failures);
}

/// A fresh scratch folder for one of the tests below.
fn scratch(name: &str) -> Scratch {
    let top = std::env::temp_dir().join(format!("bm-location-view-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&top);
    std::fs::create_dir_all(&top).unwrap();
    Scratch(std::fs::canonicalize(&top).unwrap())
}

/// A primary repository's git directory at `git`.
fn bare_minimum(git: &Path) {
    std::fs::create_dir_all(git.join("refs/heads")).unwrap();
    std::fs::create_dir_all(git.join("objects")).unwrap();
    std::fs::create_dir_all(git.join("info")).unwrap();
    std::fs::write(git.join("HEAD"), b"ref: refs/heads/main\n").unwrap();
    std::fs::write(git.join("refs/heads/main"), [b'a'; 40].as_slice()).unwrap();
}

fn machine(fs: &StdFs) -> Machine<'_, StdFs, StdHost> {
    Machine {
        ctx: Ctx {
            fs,
            flavour: Flavour::Posix,
            cwd: b"/",
        },
        refs: &githead::StdFs,
        host: &StdHost,
        platform: platform(),
        mountinfo: b"",
        common_env: None,
    }
}

/// `_git_dir_at` catches only `GitDies` from `_at_dotgit`: an OSError or
/// a ValueError of `judge` or `_common_of` there propagates out of the
/// reference's `view_state` (and `path_liveness_many` reads the path
/// again). The port declines with the effect's error, never "replaced".
#[test]
fn a_raising_at_dotgit_declines() {
    let s = scratch("raise");
    let p = s.0.join("p");
    std::fs::create_dir_all(&p).unwrap();
    std::fs::write(p.join(".git"), b"gitdir: elsewhere\n").unwrap();
    let fs = StdFs;
    let got = view_state(
        &machine(&fs),
        Effects {
            object_key: &mut |q| Ok(stub_key(q, None)),
            at_dotgit: &mut |_, _, _| Err(Declined::Raised("PermissionError".into())),
            parse_project: &mut |_| Ok(false),
            holds: &mut |_, _| Ok(false),
            peel: &mut |_| None,
            pause: &mut |_| {},
        },
        p.as_os_str().as_bytes(),
        Kind::Primary,
        Recorded {
            key: Some(&(cps("1"), cps("2"), None)),
            token: None,
            last_head: None,
            stamps: &[],
            common_key: None,
            superseded: false,
        },
    );
    assert_eq!(got, Err(Declined::Raised("PermissionError".into())));
}

/// A git directory whose path is not UTF-8: the reference holds it with
/// surrogateescape and answers ("live" here, by its last HEAD); the port
/// declines (`Declined::NotText`), since `checkout_token::read` takes the
/// path as text. A stated gap on POSIX.
#[test]
fn a_git_directory_not_utf8_declines() {
    let s = scratch("nottext");
    let p = s.0.join(std::ffi::OsStr::from_bytes(b"\xff")).join("p");
    let made = std::fs::create_dir_all(&p);
    // APFS refuses a name that is not UTF-8 (EILSEQ); Linux takes it.
    #[cfg(target_os = "macos")]
    if made.is_err() {
        return;
    }
    made.unwrap();
    bare_minimum(&p.join(".git"));
    let git = p.join(".git");
    let key = stub_key(git.as_os_str().as_bytes(), None).unwrap();
    let fs = StdFs;
    let got = view_state(
        &machine(&fs),
        Effects {
            object_key: &mut |q| Ok(stub_key(q, None)),
            at_dotgit: &mut |_, _, _| Ok(None),
            parse_project: &mut |_| Ok(false),
            holds: &mut |_, _| Ok(false),
            peel: &mut |_| None,
            pause: &mut |_| {},
        },
        p.as_os_str().as_bytes(),
        Kind::Primary,
        Recorded {
            key: Some(&key),
            token: None,
            last_head: Some(&"a".repeat(40)),
            stamps: &[],
            common_key: None,
            superseded: false,
        },
    );
    assert_eq!(got, Err(Declined::NotText));
}
