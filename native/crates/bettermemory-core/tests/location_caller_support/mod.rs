//! Values the `location_caller` tests build: a location, a resolution,
//! a machine identity and a table written as the generator writes it.

#![allow(dead_code)]

use bettermemory_core::capture::location::{Location, ObjectKey};
use bettermemory_core::capture::State;
use bettermemory_core::machine::{MachineFacts, MachineIdentity};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::store::caller::Resolution;
use bettermemory_core::unicode;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

pub fn text(t: &str) -> PyStr {
    unicode::cps(t)
}

pub fn s(t: &[u32]) -> String {
    unicode::to_string(t).expect("text")
}

/// A location of machine `m` at `cwd`, outside a repository.
pub fn loc(
    cwd: &[u32],
    root: Option<PyStr>,
    key: Option<(PyStr, PyStr, Option<PyStr>)>,
) -> Location {
    Location {
        machine: text("m"),
        cwd: cwd.to_vec(),
        state: State::Outside,
        root,
        kind: None,
        git_dir: None,
        object_key: key.map(|(device, inode, birth)| ObjectKey {
            device,
            inode,
            birth,
        }),
        common_object_key: None,
        root_object_key: None,
        head: None,
        branch: None,
        ref_tips: Vec::new(),
        remote_keys: Default::default(),
        alias_keys: Default::default(),
        shallow: false,
        token: None,
        token_written: None,
        token_status: Vec::new(),
        stamps: Vec::new(),
        ticks: None,
        files: Vec::new(),
        file_tracked: None,
        evicted: false,
        detail: Vec::new(),
        manifest: None,
        session: None,
    }
}

/// A silent resolution of project `p` with `checkout` and `lines`.
pub fn res(checkout: Option<PyStr>, lines: Vec<PyStr>) -> Resolution {
    Resolution {
        outcome: text("silent"),
        project: Some(text("p")),
        checkout,
        rule: text("r"),
        proposal: None,
        candidates: Vec::new(),
        witnesses: Vec::new(),
        chain: Vec::new(),
        tenant: None,
        lines,
        rows: Vec::new(),
        remint_token: false,
    }
}

pub fn identity(id: &[u32]) -> MachineIdentity {
    MachineIdentity {
        id: id.to_vec(),
        facts: MachineFacts {
            hardware: None,
            hostname: None,
            id_file: None,
        },
        predecessor: None,
        minted: false,
        cloned: false,
        facts_updated: false,
        recorded: None,
    }
}

/// `_table`: the length, the first and last three entries and the
/// SHA-256 of every entry's line.
pub fn table(entries: Vec<String>) -> Value {
    let mut h = Sha256::new();
    for e in &entries {
        h.update(e.as_bytes());
        h.update(b"\n");
    }
    let sha: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
    let n = entries.len();
    json!({
        "n": n,
        "head": entries[..n.min(3)],
        "tail": entries[n.saturating_sub(3)..],
        "sha": sha,
    })
}
