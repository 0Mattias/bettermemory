//! What the `settings_create*.jsonl` tests share: one case of the
//! first-run write run on the real file system in a scratch directory,
//! through `config::create::load_config_creating` and `SystemCreate`, and
//! compared with the row.

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};

use bettermemory_core::config::create::{created_notice, load_config_creating, SystemCreate};
use bettermemory_core::config::report::SystemFs;
use bettermemory_core::config::{
    Ctx, Env, Flavour, Guards, Locations, ManagedSource, Setting, World,
};
use serde_json::Value;
use sha2::{Digest, Sha256};

static N: AtomicUsize = AtomicUsize::new(0);

/// A fresh scratch directory, removed on drop.
pub struct Scratch(pub PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if let Ok(entries) = std::fs::read_dir(&self.0) {
                for e in entries.flatten() {
                    let _ =
                        std::fs::set_permissions(e.path(), std::fs::Permissions::from_mode(0o700));
                }
            }
        }
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

pub fn scratch() -> Scratch {
    let n = N.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bm-cfgcreate-{}-{n}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    Scratch(dir)
}

pub fn sha256_hex(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn under(base: &std::path::Path, rel: &str) -> PathBuf {
    rel.split('/')
        .fold(base.to_path_buf(), |p, part| p.join(part))
}

/// Runs one case row; returns what differs from it, empty when it agrees.
pub fn run_case(row: &Value) -> Vec<String> {
    let dir = scratch();
    let base = dir.0.clone();
    for d in row["dirs"].as_array().unwrap() {
        std::fs::create_dir_all(under(&base, d.as_str().unwrap())).unwrap();
    }
    if let Some(files) = row["files"].as_array() {
        for f in files {
            std::fs::write(under(&base, f.as_str().unwrap()), b"").unwrap();
        }
    }
    let rel = row["path"].as_str().unwrap();
    let target = under(&base, rel);
    if let Some(before) = row["before"].as_str() {
        std::fs::create_dir_all(target.parent().unwrap()).unwrap();
        std::fs::write(&target, before.as_bytes()).unwrap();
    }
    #[cfg(unix)]
    if let Some(mode) = row["parent_mode"].as_str() {
        use std::os::unix::fs::PermissionsExt;
        let bits = u32::from_str_radix(mode.trim_start_matches("0o"), 8).unwrap();
        std::fs::set_permissions(
            target.parent().unwrap(),
            std::fs::Permissions::from_mode(bits),
        )
        .unwrap();
        // An account that searches the folder anyway (root) is not the
        // account the row was made by: the generator refuses to run as root.
        let probe = std::fs::metadata(target.parent().unwrap().join("probe"));
        if !matches!(&probe, Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied) {
            let _ = std::fs::set_permissions(
                target.parent().unwrap(),
                std::fs::Permissions::from_mode(0o700),
            );
            eprintln!(
                "{}: skipped, this account searches a mode {mode} folder",
                row["case"]
            );
            return Vec::new();
        }
    }
    let path = target.to_string_lossy().into_owned();
    let prefix = format!("{}{}", base.to_string_lossy(), std::path::MAIN_SEPARATOR);
    let relative = |text: &str| text.replace(&prefix, "").replace('\\', "/");
    let fs = SystemFs {
        home: None,
        cwd: None,
    };
    let env = Env::new();
    let locations = Locations {
        user_config: path.clone(),
        managed: ManagedSource::File(base.join("no-managed.toml").to_string_lossy().into_owned()),
        default_keys_dir: String::new(),
        default_state_dir: String::new(),
        system_dirs: Vec::new(),
        flavour: if cfg!(windows) {
            Flavour::Windows
        } else {
            Flavour::Posix
        },
    };
    let world = World {
        fs: &fs,
        env: &env,
        locations: &locations,
    };
    let mut guards = Guards::new();
    let mut ctx = Ctx::new(&mut guards);
    let (made, loaded) = load_config_creating(&world, &SystemCreate, &path, &mut ctx);
    #[cfg(unix)]
    if row["parent_mode"].is_string() {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(
            target.parent().unwrap(),
            std::fs::Permissions::from_mode(0o700),
        );
    }
    let mut got = serde_json::Map::new();
    got.insert(
        "notice".into(),
        if made {
            Value::from(relative(&created_notice(&path)))
        } else {
            Value::Null
        },
    );
    match loaded {
        Ok(config) => {
            let Setting::Bool(b) = config.get("storage", "cwd_store") else {
                panic!("cwd_store is not a bool");
            };
            got.insert("cwd_store".into(), Value::from(*b));
        }
        Err(raised) => {
            got.insert("raised".into(), Value::from(raised.class));
            if row.get("message").is_some() {
                got.insert("message".into(), Value::from(relative(&raised.message)));
            }
        }
    }
    if target.is_file() {
        got.insert(
            "file_sha256".into(),
            Value::from(sha256_hex(&std::fs::read(&target).unwrap())),
        );
        #[cfg(unix)]
        if row.get("mode").is_some() {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&target).unwrap().permissions().mode() & 0o7777;
            got.insert("mode".into(), Value::from(format!("0o{mode:o}")));
        }
    }
    let mut diffs = Vec::new();
    for key in [
        "notice",
        "cwd_store",
        "raised",
        "message",
        "file_sha256",
        "mode",
    ] {
        let want = row.get(key).cloned().unwrap_or(Value::Null);
        let have = got.get(key).cloned().unwrap_or(Value::Null);
        if want != have {
            diffs.push(format!("{}: {key}: want {want}, got {have}", row["case"]));
        }
    }
    diffs
}

/// Every case row of `file`: the count checked and the failures.
pub fn run_file(file: &str) -> (usize, usize, Vec<String>) {
    let rows = super::jsonl(file);
    let cases: Vec<&Value> = rows.iter().filter(|r| r.get("case").is_some()).collect();
    let mut failures = Vec::new();
    for row in &cases {
        failures.extend(run_case(row));
    }
    (cases.len(), cases.len(), failures)
}
