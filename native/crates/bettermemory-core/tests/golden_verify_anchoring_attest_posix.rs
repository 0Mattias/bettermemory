//! Every case of `verify_anchoring_attest_posix.jsonl` (POSIX only)
//! against `resolves_under` and `worktree_root_is_live` over the machine's
//! file system (`StdFs`): a folder, a file, a link to the folder and a
//! link loop in a scratch tree, `@` standing for it, and `expanduser`
//! mapping a leading `~` to `@/home`.

#![cfg(unix)]

mod common;

use std::os::unix::fs::symlink;

use bettermemory_core::attestation_paths::{Host, Raised};
use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::attest::{
    resolves_under, worktree_root_is_live, FlavouredFs,
};
use common::attestation_paths::{cps, spell, spell_bool, text};
use common::parity::Ledger;
use serde_json::json;

struct Home {
    home: PyStr,
}

impl Host for Home {
    fn nt(&self) -> bool {
        false
    }
    fn expanduser(&self, p: &[u32]) -> Result<PyStr, Raised> {
        if p.first() == Some(&u32::from(b'~')) && (p.len() == 1 || p[1] == u32::from(b'/')) {
            let mut out = self.home.clone();
            out.extend_from_slice(&p[1..]);
            return Ok(out);
        }
        Ok(p.to_vec())
    }
    fn lstat(&self, _: &[u32]) -> Result<(), Raised> {
        Err(Raised::NotPorted)
    }
}

const ROOTS: [&str; 6] = ["@/w", "@/link", "@/w/", "/", "@/home", "@/loop"];

#[test]
fn verify_anchoring_attest_posix_every_case() {
    let file = "verify_anchoring_attest_posix.jsonl";
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let base = std::env::temp_dir().join(format!("bm-vaat-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).unwrap();
    let root = base.canonicalize().unwrap().join("R");
    std::fs::create_dir(&root).unwrap();
    std::fs::create_dir(root.join("w")).unwrap();
    std::fs::create_dir(root.join("home")).unwrap();
    std::fs::write(root.join("file"), b"x").unwrap();
    symlink(root.join("w"), root.join("link")).unwrap();
    symlink(root.join("loop"), root.join("loop")).unwrap();
    let r = root.to_str().unwrap().to_owned();
    let at = |s: &str| cps(&s.replace('@', &r));
    let host = Home { home: at("@/home") };
    let fs = FlavouredFs {
        fs: &StdFs,
        flavour: Flavour::Posix,
    };
    let cwd = b"/".to_vec();
    let mut ledger = Ledger::new("verify_anchoring_attest_posix_every_case", file);
    for (i, row) in rows.iter().enumerate() {
        ledger.case(format!("line {}", i + 2), || {
            match row["k"].as_str().unwrap() {
                "res" => {
                    let p = at(row["s"].as_str().unwrap());
                    let got: Vec<String> = ROOTS
                        .iter()
                        .map(|root| {
                            text(&spell(
                                resolves_under(&host, &fs, &cwd, &p, &at(root)),
                                spell_bool,
                            ))
                        })
                        .collect();
                    assert_eq!(json!(got), row["o"]);
                }
                "live" => {
                    let got: Vec<String> = row["s"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|s| {
                            let s = at(s.as_str().unwrap());
                            text(&spell(worktree_root_is_live(&host, &fs, &s), spell_bool))
                        })
                        .collect();
                    assert_eq!(json!(got), row["o"]);
                }
                k => panic!("unknown row kind {k}"),
            }
        });
    }
    let _ = std::fs::remove_dir_all(&base);
    ledger.finish(rows.len());
}
