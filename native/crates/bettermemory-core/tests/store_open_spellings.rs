//! A differential over binding spellings: `<id>.path` naming this file, a
//! copy of this store, another store or nothing, each spelled a few dozen
//! ways (a trailing or doubled separator, `.` and `..` segments, the
//! scratch directory as given or resolved, another case, a trailing
//! space, a percent sign, non-ASCII, a NUL), opened for writing by the
//! reference and by native on twin cases. Each pair must agree on the
//! outcome (a writer's open, a reader's because the ring is bound
//! elsewhere, or the error's class) and leave the same `.path` bytes.
//!
//! Needs the reference (`BM_REFERENCE_PYTHON`); POSIX (the spellings are
//! POSIX paths).

#![cfg(unix)]

mod common;

use std::fs;
use std::path::{Path, PathBuf};

use bettermemory_core::store::open::{open_for_writing, NotPorted, OpenError};
use common::created::{reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::stores::scratch;
use serde_json::json;

const SCRIPT: &str = r#"
import json, sys
from pathlib import Path
from unittest.mock import patch
import bettermemory
from bettermemory.store import Store
out = []
for case in sys.argv[1:]:
    c = Path(case)
    try:
        with patch.object(bettermemory, "__version__", "9.0.0"):
            s = Store.open(c / "memory.sqlite", keys_dir=c / "keys", allow_rekey=True)
        out.append("elsewhere" if s.key_state.bound_to is not None else "writer")
        s.close()
    except Exception as exc:
        out.append(type(exc).__name__)
print(json.dumps(out))
"#;

fn copy_dir(from: &Path, to: &Path) {
    fs::create_dir_all(to).expect("dir");
    for e in fs::read_dir(from).expect("read") {
        let e = e.expect("entry");
        let t = to.join(e.file_name());
        if e.file_type().expect("type").is_dir() {
            copy_dir(&e.path(), &t);
        } else {
            fs::copy(e.path(), &t).expect("copy");
        }
    }
}

/// Spellings of `target` (an absolute path text under `prefix`, which is
/// the case directory as given; `resolved` is it resolved).
fn spellings(prefix: &str, resolved: &str, rel: &str) -> Vec<String> {
    let mut out = Vec::new();
    let (dir, name) = rel.rsplit_once('/').map_or(("", rel), |(d, n)| (d, n));
    let joined = |base: &str, middle: &str| {
        if dir.is_empty() {
            format!("{base}{middle}/{name}")
        } else {
            format!("{base}/{dir}{middle}/{name}")
        }
    };
    for base in [prefix, resolved] {
        let middles = ["", "/.", "//", "/./.", "/x/..", "/../c", "///", "/./x/.."];
        for m in middles {
            let p = joined(base, m);
            for tail in ["", "/", "//", "/.", "/./", " ", "%2F"] {
                out.push(format!("{p}{tail}"));
            }
        }
        out.push(format!("{}/{}", base.replace("/c", "/C"), rel));
        out.push(format!("{base}/{}", rel.to_uppercase()));
        out.push(format!("{base}/é/../{rel}"));
        out.push(format!("{base}/%2e/{rel}"));
        out.push(format!("{base}/{rel}\u{0}"));
        out.push(format!("/{base}/{rel}"));
        out.push(format!("//{base}/{rel}"));
    }
    out
}

const TEST: &str = "every_binding_spelling_settles_as_the_reference";

#[test]
fn every_binding_spelling_settles_as_the_reference() {
    let python = match reference_python() {
        Ok(p) => p,
        Err(why) => {
            record(TEST, "reference Python", "i", 0, &[TEST.to_owned()], &[why]);
            return;
        }
    };
    let id = common::stores::store_id();
    let dir = scratch("spell");
    let root = dir.0.clone();
    // The template: this store, a copy (same id), another store.
    let template = root.join("t");
    opened::made("intact", &template);
    opened::setup(
        &[
            json!({"copy_store": "other/memory.sqlite"}),
            json!({"copy_store": "another/memory.sqlite"}),
            json!({"sql_at": "another/memory.sqlite",
                   "sql": "UPDATE meta SET value = 'fedcba9876543210fedcba9876543210' \
                           WHERE key = 'store_id';"}),
        ],
        &template,
        &id,
    );
    let mut cases: Vec<(String, PathBuf, PathBuf)> = Vec::new();
    let mut n = 0;
    for rel in [
        "memory.sqlite",
        "other/memory.sqlite",
        "another/memory.sqlite",
        "gone/memory.sqlite",
    ] {
        // Each twin is `<root>/<n>/c`, so a spelling with `/c` in it
        // names the twin's own case.
        let probe = root.join("p").join("c");
        let shown = probe.to_str().expect("UTF-8").to_owned();
        let resolved_root = fs::canonicalize(&root).expect("root");
        let resolved = resolved_root.join("p").join("c");
        for spelled in spellings(&shown, resolved.to_str().expect("UTF-8"), rel) {
            n += 1;
            let mut pair = Vec::new();
            for side in ["r", "n"] {
                let c = root.join(format!("{side}{n}")).join("c");
                copy_dir(&template, &c);
                let raw = root.join("p").to_str().expect("UTF-8").to_owned();
                let rraw = resolved_root.join("p").to_str().expect("UTF-8").to_owned();
                let parent = c.parent().expect("parent");
                let here = spelled
                    .replace(
                        &rraw,
                        fs::canonicalize(parent)
                            .expect("c")
                            .to_str()
                            .expect("UTF-8"),
                    )
                    .replace(&raw, parent.to_str().expect("UTF-8"));
                let text = serde_json::to_string(&json!({ "path": here })).expect("json");
                fs::write(c.join("keys").join(format!("{id}.path")), text).expect("bind");
                pair.push(c);
            }
            cases.push((spelled.clone(), pair[0].clone(), pair[1].clone()));
        }
    }
    let args: Vec<&Path> = cases.iter().map(|(_, r, _)| r.as_path()).collect();
    let out = run_reference(&python, &root.join("home"), SCRIPT, &args);
    assert!(
        out.status.success(),
        "the reference failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let reference: Vec<String> =
        serde_json::from_slice(out.stdout.trim_ascii()).expect("the reference's JSON");
    let provider = opened::engine_provider();
    let mut differences = Vec::new();
    for ((spelled, r, c), want) in cases.iter().zip(&reference) {
        let got = match open_for_writing(&c.join("memory.sqlite"), &c.join("keys"), &provider) {
            Ok(s) => {
                s.close().expect("close");
                "writer".to_owned()
            }
            Err(OpenError::NotPorted(NotPorted::BoundElsewhere { .. })) => "elsewhere".to_owned(),
            Err(e) => e.class().to_owned(),
        };
        let binding = |c: &Path| {
            let text = fs::read(c.join("keys").join(format!("{id}.path"))).unwrap_or_default();
            let text = String::from_utf8_lossy(&text).into_owned();
            text.replace(
                fs::canonicalize(c).expect("c").to_str().expect("UTF-8"),
                "<c>",
            )
            .replace(c.to_str().expect("UTF-8"), "<c>")
            .replace(
                fs::canonicalize(c.parent().expect("p"))
                    .expect("p")
                    .to_str()
                    .expect("UTF-8"),
                "<p>",
            )
            .replace(c.parent().expect("p").to_str().expect("UTF-8"), "<p>")
        };
        let (wb, gb) = (binding(r), binding(c));
        if &got != want || wb != gb {
            differences.push(format!(
                "{spelled:?}: reference {want} {wb}, native {got} {gb}"
            ));
        }
    }
    eprintln!(
        "{} spellings compared, {} differ",
        cases.len(),
        differences.len()
    );
    assert!(differences.is_empty(), "{}", differences.join("\n"));
    record(TEST, "reference Python", "i", cases.len(), &[], &[]);
}
