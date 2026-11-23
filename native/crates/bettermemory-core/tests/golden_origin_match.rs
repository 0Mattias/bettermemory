//! Source-generated comparisons for caller URL spellings and linked checkout marker text.
mod common;
use bettermemory_core::{origin_match::OriginMatcher, pyvalue::PyStr};
use serde_json::Value;

fn points(raw: &Value) -> PyStr {
    raw.as_array()
        .unwrap()
        .iter()
        .map(|v| u32::try_from(v.as_u64().unwrap()).unwrap())
        .collect()
}

#[test]
fn caller_remote_spellings() {
    let file = "origin_match.jsonl";
    let rows = common::jsonl(file);
    let pool: Vec<_> = rows[0]["pool"]
        .as_array()
        .unwrap()
        .iter()
        .map(points)
        .collect();
    let values = |v: &Value| {
        v.as_array().map(|items| {
            items
                .iter()
                .map(|i| pool[i.as_u64().unwrap() as usize].clone())
                .collect::<Vec<_>>()
        })
    };
    let one = |v: &Value| v.as_u64().map(|i| pool[i as usize].as_slice());
    let matcher = OriginMatcher::new();
    for (n, row) in rows[1..].iter().enumerate() {
        match row[0].as_str().unwrap() {
            "set" => matcher.register_caller_alternates(
                one(&row[1]).unwrap().to_vec(),
                values(&row[2]).unwrap(),
            ),
            "match" => assert_eq!(
                matcher.repos_match(one(&row[1]), one(&row[2]), values(&row[3]).as_deref()),
                row[4].as_bool().unwrap(),
                "operation {n}"
            ),
            other => panic!("unexpected operation {other}"),
        }
    }
    common::assert_count(
        file,
        rows.len() - 1,
        usize::try_from(rows[0]["rows"].as_u64().unwrap()).unwrap(),
    );
}

#[cfg(unix)]
#[test]
fn physical_marker_text() {
    use bettermemory_core::{fsutil::fsdecode, origin_match::NativeWorktrees};
    use common::parity::Ledger;
    use std::{env, fs, os::unix::ffi::OsStrExt, path::Path, process::Command};
    // Relative gitdir markers resolve against the process cwd. Give the test
    // its own empty cwd and parent without changing other test threads' cwd.
    const CHILD: &str = "BM_ORIGIN_MATCH_GOLDEN_CHILD_CWD";
    let cwd = env::current_dir().unwrap();
    if env::var_os(CHILD).as_deref().map(Path::new) != Some(cwd.as_path()) {
        let private = common::stores::scratch("origin-match-caller");
        let work = private.0.join("cwd");
        fs::create_dir(&work).unwrap();
        let mut child = Command::new(env::current_exe().unwrap());
        child
            .args(["--exact", "physical_marker_text", "--nocapture"])
            .current_dir(&work)
            .env(CHILD, &work);
        for key in ["BM_GOLDEN_DIR", "BM_PARITY_LOG"] {
            if let Some(value) = env::var_os(key).filter(|path| !path.is_empty()) {
                child.env(key, cwd.join(value));
            }
        }
        assert!(child.status().unwrap().success());
        return;
    }
    let file = "origin_match_posix.jsonl";
    let rows = common::jsonl(file);
    let mut ledger = Ledger::new("physical_marker_text", file);
    let temp = common::stores::scratch("origin-match-golden");
    let root = fs::canonicalize(&temp.0).unwrap();
    let matcher = OriginMatcher::new();
    let mut host = NativeWorktrees::default();
    for (i, row) in rows[1..].iter().enumerate() {
        ledger.case(i, || {
            let folder = root.join(i.to_string());
            fs::create_dir(&folder).unwrap();
            let raw: Vec<u8> = serde_json::from_value(row[0].clone()).unwrap();
            let mut expanded = Vec::new();
            let mut pos = 0;
            while let Some(i) = raw[pos..].windows(6).position(|p| p == b"<ROOT>") {
                expanded.extend_from_slice(&raw[pos..pos + i]);
                expanded.extend_from_slice(root.as_os_str().as_bytes());
                pos += i + 6;
            }
            expanded.extend_from_slice(&raw[pos..]);
            fs::write(folder.join(".git"), expanded).unwrap();
            let got = matcher
                .primary_root_of(&fsdecode(folder.as_os_str()), &mut host)
                .unwrap();
            let expected = row[1].as_array().map(|_| {
                let mut text = points(&row[1]);
                for (token, path) in [
                    ("<ROOT>", root.as_path()),
                    ("<CWD>", cwd.as_path()),
                    ("<PARENT>", cwd.parent().unwrap_or(&cwd)),
                ] {
                    if let Some(tail) =
                        text.strip_prefix(bettermemory_core::unicode::cps(token).as_slice())
                    {
                        let mut full = fsdecode(path.as_os_str());
                        full.extend_from_slice(tail);
                        text = full;
                        break;
                    }
                }
                text
            });
            assert_eq!(got.as_deref(), expected.as_deref());
        });
    }
    ledger.finish(rows[0]["rows"].as_u64().unwrap() as usize);
}
