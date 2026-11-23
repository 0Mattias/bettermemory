//! The portable load refusal and the complete physical POSIX reference matrix.
mod common;
#[cfg(unix)]
#[path = "common/read_only_path.rs"]
mod host;

use bettermemory_core::argparse::ArgumentParser;
use bettermemory_core::cli::common::refuse;
use bettermemory_core::unicode::cps;

#[test]
fn portable_load_refusals() {
    let parser = ArgumentParser::new("bettermemory log", None, true);
    let rows = common::jsonl("cli_read_only_path.jsonl");
    assert_eq!(rows.len(), 3);
    for row in rows {
        let code = row["code"].as_i64().unwrap() as i32;
        let got = refuse(
            &parser,
            &cps("the config does not load: shared reason"),
            code,
        );
        assert_eq!(got.code, row["out"]["exit"].as_i64().unwrap() as i32);
        assert_eq!(got.stderr, common::text_field(&row["out"], "stderr"));
    }
}

#[cfg(unix)]
mod posix {
    use super::{common, host};
    use bettermemory_core::cli::common::read_only_path::{
        read_only_store_path_at, PathError, PathGuards, PathWarning,
    };
    use bettermemory_core::gitfile_rule::follow::fsencode;
    use bettermemory_core::gitsystem::record::surrogateescape;
    use bettermemory_core::pyvalue::PyStr;
    use bettermemory_core::unicode::cps;
    use std::ffi::OsString;
    use std::os::unix::{
        ffi::{OsStrExt, OsStringExt},
        fs::symlink,
    };
    use std::path::Path;
    use std::{env, fs};

    fn install(p: &Path, kind: &str, other: &Path) {
        if let Ok(m) = fs::symlink_metadata(p) {
            if m.is_dir() {
                fs::remove_dir(p).unwrap();
            } else {
                fs::remove_file(p).unwrap();
            }
        }
        match kind {
            "missing" => (),
            "directory" | "dir" => fs::create_dir(p).unwrap(),
            "dangling" => symlink(p.parent().unwrap().join("not-there"), p).unwrap(),
            "symlink" => symlink(other, p).unwrap(),
            text => {
                let body = match text {
                    "file" => "fixture",
                    "empty" => "",
                    "invalid" => "[broken\n",
                    "relative" => "[storage]\ndirectory=\"link/../chosen\"\n",
                    "homefallback" => "[storage]\ncwd_store=false\n",
                    "managed" => "[storage]\ndirectory=\"~/org\"\n",
                    "unknown" => {
                        "[storage]\ndirectory=\"~bettermemory_invalid_reference_user_x/alice\"\n"
                    }
                    _ => panic!("unknown fixture {text}"),
                };
                fs::write(p, body).unwrap();
            }
        }
    }
    fn normalize(text: &[u32], root: &Path) -> PyStr {
        let from = surrogateescape(root.as_os_str().as_bytes());
        let mut rest = text;
        let mut out = Vec::new();
        while let Some(i) = rest.windows(from.len()).position(|w| w == from) {
            out.extend_from_slice(&rest[..i]);
            out.extend(cps("<root>"));
            rest = &rest[i + from.len()..];
        }
        out.extend_from_slice(rest);
        out
    }

    #[test]
    fn physical_matrix() {
        host::isolated("posix::physical_matrix", |root| {
            let home = root.join("fixture-account");
            fs::rename(root.join("home"), &home).unwrap();
            env::set_var("HOME", home);
            let cwd = root.join("cwd");
            fs::create_dir(&cwd).unwrap();
            let other = root.join("actual/other");
            fs::create_dir_all(&other).unwrap();
            symlink(&other, cwd.join("link")).unwrap();
            env::set_current_dir(&cwd).unwrap();
            let user = root.join("user.toml");
            let managed = root.join("managed.toml");
            let rows = common::jsonl("cli_read_only_path_posix.jsonl");
            let head = &rows[0];
            let mut counts = [0usize; 3];
            let mut guards = PathGuards::new();
            for row in &rows[1..] {
                install(&user, row["user"].as_str().unwrap(), &other);
                install(&managed, row["managed"].as_str().unwrap(), &other);
                install(
                    &cwd.join(".claude-memory"),
                    row["project"].as_str().unwrap(),
                    &other,
                );
                let before = host::snapshot(root);
                let mut results = row["results"].as_array().unwrap().iter();
                for value in head["env"].as_array().unwrap() {
                    if value.is_null() {
                        env::remove_var("BETTERMEMORY_DIR");
                    } else {
                        env::set_var(
                            "BETTERMEMORY_DIR",
                            OsString::from_vec(
                                fsencode(&common::text_field(value, "value")).unwrap(),
                            ),
                        );
                    }
                    for code in head["codes"].as_array().unwrap() {
                        let expected =
                            &head["outcomes"][results.next().unwrap().as_u64().unwrap() as usize];
                        let mut warnings = Vec::new();
                        let got = read_only_store_path_at(
                            &host::parser(),
                            code.as_i64().unwrap() as i32,
                            &user,
                            &managed,
                            &mut guards,
                            &mut |w| {
                                let PathWarning::Config(t) = w else {
                                    panic!("unexpected system warning")
                                };
                                warnings.push(normalize(t, root));
                                Ok::<
                                    _,
                                    bettermemory_core::cli::common::read_only_path::WarningError<()>,
                                >(())
                            },
                        );
                        match got {
                            Ok(p) => {
                                counts[0] += 1;
                                assert_eq!(
                                    normalize(&surrogateescape(p.as_os_str().as_bytes()), root),
                                    common::text_field(expected, "path"),
                                    "{row}"
                                );
                            }
                            Err(PathError::Exit(e)) => {
                                counts[1] += 1;
                                assert_eq!(e.code as i64, expected["exit"].as_i64().unwrap());
                                assert_eq!(
                                    normalize(&e.stderr, root),
                                    common::text_field(expected, "stderr")
                                );
                            }
                            Err(PathError::Raised(e)) => {
                                counts[2] += 1;
                                assert_eq!(e.class, expected["raised"].as_str().unwrap());
                                assert_eq!(e.message, common::text_field(expected, "message"));
                            }
                            Err(PathError::Logger(())) => panic!("infallible sink"),
                        }
                        let want: Vec<_> = expected["warnings"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .map(|w| common::text_field(w, "text"))
                            .collect();
                        assert_eq!(warnings, want);
                        assert_eq!(before, host::snapshot(root));
                    }
                }
                assert!(results.next().is_none());
            }
            assert_eq!(counts, [580, 720, 140]);
        });
    }
}
#[cfg(not(unix))]
#[test]
fn physical_location_not_available_here() {
    common::parity::record(
        "cli_read_only_path_posix",
        "cli_read_only_path_posix.jsonl",
        "os",
        0,
        &["POSIX config location".into()],
        &["Windows managed and directory locations have separate owners".into()],
    );
}
