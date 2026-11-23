//! Config validation precedes overrides; only load errors become a usage
//! refusal. A system-directory warning has a narrower catch of its own.
#![cfg(unix)]
mod common;
#[path = "common/read_only_path.rs"]
mod host;
use bettermemory_core::cli::common::read_only_path::{
    read_only_store_path_at, PathError, PathGuards, PathWarning, WarningError,
};
use bettermemory_core::unicode::{cps, to_string};
use std::path::Path;
use std::sync::Arc;
use std::{env, fs};

#[test]
fn semantic_pins_and_load_order() {
    host::isolated("semantic_pins_and_load_order", |root| {
        let user = root.join("user.toml");
        let managed = root.join("managed.toml");
        let mut guards = PathGuards::new();
        fs::write(&user, "[storage]\ndirectory=33").unwrap();
        fs::write(&managed, "[storage]\ndirectory='~/org'").unwrap();
        let mut warnings = Vec::new();
        let chosen =
            read_only_store_path_at(&host::parser(), 7, &user, &managed, &mut guards, &mut |w| {
                let PathWarning::Config(text) = w else {
                    panic!("wrong warning")
                };
                warnings.push(text.to_vec());
                Ok::<_, WarningError<()>>(())
            })
            .unwrap();
        assert_eq!(chosen, root.join("home/org/memory.sqlite"));
        assert_eq!(warnings.len(), 1);
        let spelled = read_only_store_path_at(
            &host::parser(),
            7,
            &user.join("."),
            &managed.join(""),
            &mut guards,
            &mut |_| Ok::<_, WarningError<()>>(()),
        );
        assert_eq!(spelled.unwrap(), chosen);

        env::set_var("BETTERMEMORY_DIR", root.join("env"));
        fs::write(&managed, "[[broken\n").unwrap();
        let Err(PathError::Exit(e)) = host::selected(root) else {
            panic!("expected refusal")
        };
        assert!(to_string(&e.stderr)
            .unwrap()
            .contains("malformed managed config in"));
        fs::write(&user, "[broken\n").unwrap();
        let Err(PathError::Exit(e)) = host::selected(root) else {
            panic!("expected refusal")
        };
        assert!(to_string(&e.stderr)
            .unwrap()
            .contains("malformed config in"));
        std::os::unix::fs::symlink(root.join("absent"), root.join("org-link")).unwrap();
        fs::write(&user, "").unwrap();
        let Err(PathError::Exit(e)) = read_only_store_path_at(
            &host::parser(),
            7,
            &user,
            &root.join("org-link"),
            &mut guards,
            &mut |_| Ok::<_, WarningError<()>>(()),
        ) else {
            panic!("dangling policy")
        };
        assert_eq!(e.code, 7);
        assert!(to_string(&e.stderr)
            .unwrap()
            .contains("is a symlink to a file that does not exist"));
    });
}

#[test]
fn warning_family_and_logging_arguments() {
    host::isolated("warning_family_and_logging_arguments", |root| {
        let user = root.join("user.toml");
        let managed = root.join("managed.toml");
        let original = Arc::new("shared reason");
        env::set_var("BETTERMEMORY_DIR", "/usr/readonly-test");
        let wanted = Path::new("/usr")
            .canonicalize()
            .unwrap()
            .join("readonly-test");
        let run = |family, n: &mut usize| {
            read_only_store_path_at(
                &host::parser(),
                7,
                &user,
                &managed,
                &mut PathGuards::new(),
                &mut |w| {
                    *n += 1;
                    match w {
                        PathWarning::System {
                            source,
                            directory,
                            prefix,
                        } => {
                            assert_eq!(source, "BETTERMEMORY_DIR");
                            assert_eq!(directory, wanted);
                            assert_eq!(prefix, Path::new("/usr"));
                        }
                        PathWarning::Config(_) => {}
                    }
                    match family {
                        0 => Ok(()),
                        1 => Err(WarningError::Value(original.clone(), cps("shared reason"))),
                        2 => Err(WarningError::Os(original.clone(), cps("shared reason"))),
                        _ => Err(WarningError::Other(original.clone())),
                    }
                },
            )
        };
        for k in 0..4 {
            let mut n = 0;
            match (k, run(k, &mut n)) {
                (0 | 1, Ok(p)) => assert_eq!(p, wanted.join("memory.sqlite")),
                (2 | 3, Err(PathError::Logger(e))) => assert!(Arc::ptr_eq(&original, &e)),
                (_, other) => panic!("{other:?}"),
            }
            assert_eq!(n, 1);
        }
        fs::write(&user, "[storage]\ndirectory='~/mine'").unwrap();
        fs::write(&managed, "[storage]\ndirectory='~/pinned'").unwrap();
        for k in 1..4 {
            let mut n = 0;
            match (k, run(k, &mut n)) {
                (1 | 2, Err(PathError::Exit(e))) => {
                    assert_eq!(e.code, 7);
                    assert_eq!(
                        e.stderr,
                        cps("bettermemory log: error: the config does not load: shared reason\n")
                    );
                }
                (3, Err(PathError::Logger(e))) => assert!(Arc::ptr_eq(&original, &e)),
                (_, result) => panic!("{result:?}"),
            }
            assert_eq!(n, 1);
        }
    });
}

#[test]
fn physical_symlinks_after_missing_or_file_components() {
    use std::os::unix::fs::symlink;
    host::isolated(
        "physical_symlinks_after_missing_or_file_components",
        |root| {
            let other = root.join("elsewhere/sub");
            fs::create_dir_all(&other).unwrap();
            symlink(&other, root.join("jump")).unwrap();
            symlink("jump", root.join("chain")).unwrap();
            symlink("..", other.join("in")).unwrap();
            symlink("elsewhere/unmade", root.join("dead")).unwrap();
            symlink("loop", root.join("loop")).unwrap();
            fs::write(root.join("file"), b"x").unwrap();
            for (raw, want) in [
                (".", ""),
                ("future/sub", "future/sub"),
                ("jump/../chosen", "elsewhere/chosen"),
                ("chain/in/after", "elsewhere/after"),
                ("future/../jump/../chosen", "elsewhere/chosen"),
                ("future/../chain/in/after", "elsewhere/after"),
                ("dead", "elsewhere/unmade"),
                ("dead/../sub/in/after", "elsewhere/after"),
                ("file/junk/../../jump/../chosen", "elsewhere/chosen"),
                ("loop", "loop"),
                ("loop/child", "loop/child"),
                ("loop/../jump/../chosen", "elsewhere/chosen"),
            ] {
                env::set_var("BETTERMEMORY_DIR", raw);
                assert_eq!(
                    host::selected(root).unwrap(),
                    root.join(want).join("memory.sqlite"),
                    "{raw}"
                );
            }
            env::set_var("BETTERMEMORY_DIR", "//");
            assert_eq!(host::selected(root).unwrap(), Path::new("/memory.sqlite"));
            env::remove_var("BETTERMEMORY_DIR");
            fs::write(root.join("user.toml"), "[storage]\ndirectory=\"a\\u0000b\"").unwrap();
            let Err(PathError::Raised(e)) = host::selected(root) else {
                panic!("directory error crossed load catch")
            };
            assert_eq!(e.class, "ValueError");
            assert_eq!(e.message, cps("lstat: embedded null character in path"));
        },
    );
}

#[test]
fn report_explains_a_disabled_project_and_override_after_failed_load() {
    use bettermemory_core::config::{
        self,
        report::{self, SystemFs},
        Env, Guards, Locations, ManagedSource, World,
    };
    host::isolated(
        "report_explains_a_disabled_project_and_override_after_failed_load",
        |root| {
            let text = |p: &Path| p.to_str().unwrap().to_owned();
            let system = SystemFs {
                home: Some(text(&root.join("home"))),
                cwd: Some(text(root)),
            };
            let locations = Locations {
                user_config: text(&root.join("user.toml")),
                managed: ManagedSource::File(text(&root.join("managed.toml"))),
                default_keys_dir: text(&root.join("keys")),
                default_state_dir: text(&root.join("state")),
                system_dirs: Vec::new(),
                flavour: config::Flavour::Posix,
            };
            let report = |env: &Env| {
                let (s, _) = report::path(
                    &World {
                        fs: &system,
                        locations: &locations,
                        env,
                    },
                    &mut Guards::new(),
                )
                .unwrap();
                serde_json::from_str::<serde_json::Value>(&s).unwrap()
            };
            fs::create_dir(root.join(".claude-memory")).unwrap();
            fs::write(root.join("user.toml"), "[storage]\ncwd_store=false").unwrap();
            let doc = report(&Env::new());
            assert_eq!(doc["store"]["rule"], "home default");
            assert_eq!(doc["store"]["notes"].as_array().unwrap().len(), 1);
            assert_eq!(
                host::selected(root).unwrap(),
                root.join("home/.claude-memory/memory.sqlite")
            );
            fs::write(root.join("user.toml"), "[broken").unwrap();
            env::set_var("BETTERMEMORY_DIR", root.join("override"));
            let named = Env::from([("BETTERMEMORY_DIR".to_owned(), text(&root.join("override")))]);
            assert_eq!(
                report(&named)["store"]["path"],
                text(&root.join("override"))
            );
            assert!(matches!(host::selected(root), Err(PathError::Exit(_))));
        },
    );
}
