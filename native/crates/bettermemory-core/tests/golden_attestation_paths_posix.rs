//! Every case of `attestation_paths_posix.jsonl` (POSIX only) against
//! `attestation_paths` with the machine's `os.lstat` (`FsHost` over
//! `StdFs`): a file, a folder, a dangling link, a link loop and a mode-000
//! folder in a scratch tree, `@` standing for it.

#![cfg(unix)]

mod common;

use std::os::unix::fs::{symlink, PermissionsExt};

use bettermemory_core::attestation_paths::{
    compile_targets, interpret_path, joined_path, may_be_literal, placeholder_spelling, FsHost,
    Mode, Raised,
};
use bettermemory_core::fsdata::StdFs;
use bettermemory_core::pyvalue::PyStr;
use common::parity::Ledger;
use serde_json::json;

use common::attestation_paths::{cps, spell, spell_bool, spell_compiled, spell_opt, text};

#[test]
fn attestation_paths_posix_every_case() {
    let file = "attestation_paths_posix.jsonl";
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let base = std::env::temp_dir().join(format!("bm-ap-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).unwrap();
    let root = base.canonicalize().unwrap().join("R");
    std::fs::create_dir(&root).unwrap();
    std::fs::write(root.join("file"), b"x").unwrap();
    std::fs::create_dir(root.join("dir")).unwrap();
    symlink(root.join("nowhere"), root.join("dangling")).unwrap();
    symlink(root.join("loop"), root.join("loop")).unwrap();
    std::fs::create_dir(root.join("locked")).unwrap();
    std::fs::write(root.join("locked").join("inner"), b"").unwrap();
    std::fs::set_permissions(root.join("locked"), std::fs::Permissions::from_mode(0o000)).unwrap();
    let r = cps(root.to_str().unwrap());
    let rtext = text(&r);
    let never = |_: &[u32]| -> Result<PyStr, Raised> { Err(Raised::NotPorted) };
    let host = FsHost {
        fs: &StdFs,
        nt: false,
        expanduser: &never,
    };
    let at = |a: PyStr| text(&a).replace(&rtext, "@");
    let mut ledger = Ledger::new("attestation_paths_posix_every_case", file);
    for (i, row) in rows.iter().enumerate() {
        ledger.case(format!("line {} {}", i + 2, row), || {
            let s = cps(row["s"].as_str().unwrap());
            let mode = Mode::parse(row["mode"].as_str().unwrap()).unwrap();
            let mut full = r.clone();
            full.push(u32::from(b'/'));
            full.extend(&s);
            let got = vec![
                at(spell(interpret_path(&host, &s, mode, Some(&r)), |v| {
                    spell_opt(&v)
                })),
                at(spell(joined_path(&host, &r, &s, mode), |v| spell_opt(&v))),
                at(spell(
                    placeholder_spelling(&host, &s, mode, Some(&r)),
                    spell_bool,
                )),
                at(spell_compiled(compile_targets(
                    &host,
                    std::slice::from_ref(&s),
                    Some(&r),
                    mode,
                    false,
                ))),
                at(spell(interpret_path(&host, &full, mode, None), |v| {
                    spell_opt(&v)
                })),
            ];
            assert_eq!(json!(got), row["o"]);
        });
    }
    std::fs::set_permissions(root.join("locked"), std::fs::Permissions::from_mode(0o755)).unwrap();
    let _ = std::fs::remove_dir_all(&base);
    ledger.finish(rows.len());
}

#[test]
fn an_unencodable_or_nul_path_may_be_literal_over_the_machine() {
    // The reference on POSIX: os.lstat raises ValueError (UnicodeEncodeError
    // for a lone surrogate, "embedded null byte" for NUL), which
    // _may_be_literal answers True; interpret_path("/a\ud800",
    // mode="auto") is then "/a\ud800".
    let never = |_: &[u32]| -> Result<PyStr, Raised> { Err(Raised::NotPorted) };
    let host = FsHost {
        fs: &StdFs,
        nt: false,
        expanduser: &never,
    };
    let surrogate: PyStr = cps("/a").into_iter().chain([0xd800]).collect();
    assert_eq!(may_be_literal(&host, &surrogate), Ok(true));
    assert_eq!(
        interpret_path(&host, &surrogate, Mode::Auto, None),
        Ok(Some(surrogate.clone()))
    );
    assert_eq!(may_be_literal(&host, &cps("/a\0b")), Ok(true));
}
