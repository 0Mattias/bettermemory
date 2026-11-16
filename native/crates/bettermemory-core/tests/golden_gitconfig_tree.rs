//! Every case of `gitconfig_tree_rules.jsonl` (text alone, both
//! flavours, every platform) and, on POSIX, of `gitconfig_tree.jsonl`
//! (real trees: in memory through `MemFs`, and written to a scratch
//! directory and read through `StdFs`). A tree holding an `includeIf`
//! the reference acts on is declined natively: only that is compared.

mod common;

use common::gitconfig_tree::rule_answer;
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;

fn cases(file: &str) -> Vec<Value> {
    let lines = jsonl(file);
    assert_eq!(lines[0]["cases"].as_u64(), Some(lines.len() as u64 - 1));
    lines[1..].to_vec()
}

fn shown(case: &Value) -> String {
    let mut c = case.clone();
    c["f"] = Value::Null;
    c["r"] = Value::Null;
    c.to_string().chars().take(300).collect()
}

#[test]
fn gitconfig_tree_rules_every_case() {
    let file = "gitconfig_tree_rules.jsonl";
    let cases = cases(file);
    let ledger = Ledger::new("golden_gitconfig_tree_rules", file);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let got = rule_answer(case);
        if got != case["r"] {
            failures.push(format!(
                "#{i} {}: native {got} reference {}",
                shown(case),
                case["r"]
            ));
        }
    }
    ledger.settle(cases.len(), &failures);
}

#[cfg(unix)]
mod posix {
    use super::*;
    use bettermemory_core::fsdata::{Flavour, StdFs};
    use common::gitconfig_file::{content, modes, world};
    use common::gitconfig_tree::tree_answer;

    /// The answer compared: a declined read only as declined.
    fn compared(got: &Value, want: &Value) -> bool {
        if !want["np"].is_null() {
            return got["np"] == want["np"];
        }
        got == want
    }

    const FILE: &str = "gitconfig_tree.jsonl";

    #[test]
    fn gitconfig_tree_every_case_in_memory() {
        let cases = cases(FILE);
        let ledger = Ledger::new("golden_gitconfig_tree", FILE);
        let mut failures = Vec::new();
        for (i, case) in cases.iter().enumerate() {
            let fs = world(case, Flavour::Posix);
            // The root is absolute, as a real scratch directory is.
            let got = tree_answer(&fs, case, "/<scratch>");
            if !compared(&got, &case["r"]) {
                failures.push(format!(
                    "#{i} {}: native {got} reference {}",
                    shown(case),
                    case["r"]
                ));
            }
        }
        ledger.settle(cases.len(), &failures);
    }

    struct Scratch(std::path::PathBuf);

    impl Drop for Scratch {
        fn drop(&mut self) {
            unlock(&self.0);
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn unlock(dir: &std::path::Path) {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(entries) = std::fs::read_dir(dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                let is_dir = std::fs::symlink_metadata(&path).is_ok_and(|m| m.is_dir());
                let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700));
                if is_dir {
                    unlock(&path);
                }
            }
        }
    }

    /// A new directory: the process id, a process-wide counter and the
    /// clock in its name, made with `create_dir`, which fails where the
    /// name is taken.
    fn fresh_dir() -> std::path::PathBuf {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("a clock")
            .as_nanos();
        let dir =
            std::env::temp_dir().join(format!("bm-gctree-{}-{n}-{nanos}", std::process::id()));
        std::fs::create_dir(&dir).expect("a new scratch directory");
        // As `realpath` gives it (macOS's temporary directory is under a
        // link), as the generator's is.
        std::fs::canonicalize(&dir).expect("a canonical scratch directory")
    }

    fn write_tree(case: &Value, root: &std::path::Path) -> bool {
        use std::os::unix::fs::{symlink, PermissionsExt};
        for d in case
            .get("d")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            std::fs::create_dir_all(root.join(d.as_str().unwrap())).unwrap();
        }
        for (rel, data) in case["f"].as_object().unwrap() {
            let path = root.join(rel);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, content(data)).unwrap();
        }
        if let Some(links) = case.get("l").and_then(Value::as_object) {
            for (rel, to) in links {
                let path = root.join(rel);
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                symlink(to.as_str().unwrap(), path).unwrap();
            }
        }
        for p in case
            .get("p")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let made = std::process::Command::new("mkfifo")
                .arg(root.join(p.as_str().unwrap()))
                .status()
                .is_ok_and(|s| s.success());
            if !made {
                return false;
            }
        }
        for (rel, mode) in modes(case).into_iter().rev() {
            std::fs::set_permissions(root.join(rel), std::fs::Permissions::from_mode(mode))
                .unwrap();
        }
        true
    }

    #[test]
    fn gitconfig_tree_every_case_on_disk() {
        let cases = cases(FILE);
        let ledger = Ledger::new("golden_gitconfig_tree_stdfs", FILE);
        let base = Scratch(fresh_dir());
        let mut failures = Vec::new();
        for (i, case) in cases.iter().enumerate() {
            let dir = Scratch(base.0.join(i.to_string()));
            std::fs::create_dir(&dir.0).unwrap();
            if !write_tree(case, &dir.0) {
                failures.push(format!("#{i}: mkfifo could not run"));
                continue;
            }
            let root = dir.0.to_str().expect("a UTF-8 scratch path").to_owned();
            let got = tree_answer(&StdFs, case, &root);
            if !compared(&got, &case["r"]) {
                failures.push(format!(
                    "#{i} {}: native {got} reference {}",
                    shown(case),
                    case["r"]
                ));
            }
        }
        drop(base);
        ledger.settle(cases.len(), &failures);
    }
}
