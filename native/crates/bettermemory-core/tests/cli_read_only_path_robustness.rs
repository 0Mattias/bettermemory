//! Host acquisition, byte paths, passwd and signed-store selection on POSIX.
#![cfg(unix)]
mod common;
#[path = "common/read_only_path.rs"]
mod host;
use bettermemory_core::cli::common::read_only_path::{
    read_only_store_path, read_only_store_path_at, PathError, PathGuards, WarningError,
};
use bettermemory_core::store::{create, StoreFile};
use bettermemory_core::unicode::cps;
use std::ffi::OsString;
use std::os::unix::ffi::OsStringExt;
use std::path::Path;
use std::{env, fs};

#[test]
fn raw_xdg_home_is_distinct_from_its_replacement_spelling() {
    host::isolated(
        "raw_xdg_home_is_distinct_from_its_replacement_spelling",
        |root| {
            let visible = root.join("xdg-\u{fffd}");
            let folder = visible.join("bettermemory");
            fs::create_dir_all(&folder).unwrap();
            fs::write(
                folder.join("config.toml"),
                "[storage]\ndirectory='visible-replacement'",
            )
            .unwrap();
            let raw = root.join(OsString::from_vec(b"xdg-\xff".to_vec()));
            for (xdg, expected) in [
                (raw, "home/.claude-memory/memory.sqlite"),
                (visible, "visible-replacement/memory.sqlite"),
            ] {
                env::set_var("XDG_CONFIG_HOME", xdg);
                let selected = read_only_store_path(
                    &host::parser(),
                    None,
                    &mut PathGuards::new(),
                    &mut |_| Ok::<_, WarningError<()>>(()),
                )
                .unwrap();
                assert_eq!(selected, root.join(expected));
            }
        },
    );
}

#[test]
fn empty_storage_directory_falls_back_to_project_or_home() {
    host::isolated(
        "empty_storage_directory_falls_back_to_project_or_home",
        |root| {
            fs::create_dir(root.join(".claude-memory")).unwrap();
            for (directory, cwd_store, expected) in [
                ("", true, ".claude-memory/memory.sqlite"),
                ("", false, "home/.claude-memory/memory.sqlite"),
                (".", true, "memory.sqlite"),
            ] {
                fs::write(
                    root.join("user.toml"),
                    format!("[storage]\ndirectory='{directory}'\ncwd_store={cwd_store}"),
                )
                .unwrap();
                assert_eq!(host::selected(root).unwrap(), root.join(expected));
            }
        },
    );
}

#[test]
fn platform_config_and_passwd() {
    host::isolated("platform_config_and_passwd", |root| {
        let cfg = root.join("config/bettermemory");
        fs::create_dir_all(&cfg).unwrap();
        fs::write(cfg.join("config.toml"), "[storage]\ndirectory='~/selected'").unwrap();
        env::set_var(
            "XDG_CONFIG_HOME",
            format!("\u{2003}{} \t", root.join("config").display()),
        );
        let select = || {
            read_only_store_path(&host::parser(), None, &mut PathGuards::new(), &mut |_| {
                Ok::<_, WarningError<()>>(())
            })
        };
        assert_eq!(select().unwrap(), root.join("home/selected/memory.sqlite"));
        fs::write(cfg.join("config.toml"), "[broken").unwrap();
        assert!(matches!(select(),Err(PathError::Exit(e)) if e.code==2));
        let base = root
            .join("home")
            .join(if cfg!(target_os = "macos") {
                "Library/Application Support"
            } else {
                ".config"
            })
            .join("bettermemory");
        fs::create_dir_all(&base).unwrap();
        fs::write(
            base.join("config.toml"),
            "[storage]\ndirectory='~/native-default'",
        )
        .unwrap();
        env::set_var("XDG_CONFIG_HOME", "relative");
        assert_eq!(
            select().unwrap(),
            root.join("home/native-default/memory.sqlite")
        );
        let real = bettermemory_sys::account_home(None).unwrap().unwrap();
        let root_account = bettermemory_sys::account_home(Some(b"root"))
            .unwrap()
            .unwrap();
        env::set_var("BETTERMEMORY_DIR", "./~root/child");
        assert_eq!(
            host::selected(root).unwrap(),
            root_account
                .canonicalize()
                .unwrap_or(root_account)
                .join("child/memory.sqlite")
        );
        env::remove_var("BETTERMEMORY_DIR");
        env::remove_var("HOME");
        let expected = real
            .canonicalize()
            .unwrap_or(real)
            .join(".claude-memory/memory.sqlite");
        assert_eq!(host::selected(root).unwrap(), expected);
        env::set_var("HOME", "");
        assert_eq!(
            host::selected(root).unwrap(),
            Path::new("/.claude-memory/memory.sqlite")
        );
        env::set_var("HOME", "~");
        let Err(PathError::Raised(e)) = select() else {
            panic!("unavailable platformdirs home")
        };
        assert_eq!(e.class, "RuntimeError");
        assert!(e
            .message
            .starts_with(&cps("could not determine the home directory for '~")));
    });
}

#[test]
fn raw_path_guards_and_deleted_project() {
    host::isolated("raw_path_guards_and_deleted_project", |root| {
        let raw = OsString::from_vec(b"raw-\xff".to_vec());
        env::set_var("BETTERMEMORY_DIR", &raw);
        assert_eq!(
            host::selected(root).unwrap(),
            root.join(&raw).join("memory.sqlite")
        );
        let mut guards = PathGuards::new();
        #[cfg(target_os = "linux")]
        {
            let mut warnings = Vec::new();
            for b in [0xff, 0xfe, 0xff] {
                let file = root.join(OsString::from_vec(vec![b]));
                fs::write(&file, b"[storage]\ndirectory='~/user'").unwrap();
                fs::write(root.join("managed.toml"), b"[storage]\ndirectory='~/org'").unwrap();
                read_only_store_path_at(
                    &host::parser(),
                    1,
                    &file,
                    &root.join("managed.toml"),
                    &mut guards,
                    &mut |w| {
                        if let bettermemory_core::cli::common::read_only_path::PathWarning::Config(
                            m,
                        ) = w
                        {
                            warnings.push(m.to_vec());
                        }
                        Ok::<_, WarningError<()>>(())
                    },
                )
                .unwrap();
            }
            assert_eq!(warnings.len(), 2);
            assert!(warnings[0].contains(&0xdcff));
            assert!(warnings[1].contains(&0xdcfe));
            fs::remove_file(root.join("managed.toml")).unwrap();
        }
        let cfg = root.join("cfg");
        fs::write(&cfg, "[storage]\ndirectory='~/user'").unwrap();
        let error_path = cfg.join(&raw);
        let Err(PathError::Exit(e)) = read_only_store_path_at(
            &host::parser(),
            1,
            &error_path,
            &root.join("absent"),
            &mut guards,
            &mut |_| Ok::<_, WarningError<()>>(()),
        ) else {
            panic!("expected OS refusal")
        };
        // Python OSError formats a filename with repr, escaping the original byte.
        assert!(e.stderr.windows(6).any(|w| w == cps("\\udcff")));
        env::remove_var("BETTERMEMORY_DIR");
        let doomed = root.join("doomed");
        fs::create_dir(&doomed).unwrap();
        env::set_current_dir(&doomed).unwrap();
        fs::remove_dir(&doomed).unwrap();
        for body in ["", "[storage]\ncwd_store=false"] {
            fs::write(root.join("user.toml"), body).unwrap();
            assert_eq!(
                host::selected(root).unwrap(),
                root.join("home/.claude-memory/memory.sqlite")
            );
        }
    });
}

#[test]
fn selecting_two_signed_stores_leaves_both_untouched() {
    host::isolated(
        "selecting_two_signed_stores_leaves_both_untouched",
        |root| {
            let mut ids = Vec::new();
            for (i, name) in ["one", "two"].iter().enumerate() {
                let mut provider = common::created::fixed(&common::created::fixture());
                provider.store_id = format!("{:032x}", i + 1);
                let created =
                    create::create(&root.join(name), &root.join("keys"), &mut provider).unwrap();
                ids.push(created.store_id);
            }
            fs::write(root.join("user.toml"), "[storage]\ndirectory='one'").unwrap();
            for (name, id) in ["one", "two"].into_iter().zip(ids) {
                if name == "two" {
                    fs::write(root.join("managed.toml"), "[storage]\ndirectory='two'").unwrap();
                }
                let before = host::snapshot(root);
                let p = host::selected(root).unwrap();
                assert_eq!(p, root.join(name).join("memory.sqlite"));
                let opened = StoreFile::open(&p).unwrap();
                let meta = opened.meta().unwrap();
                assert!(meta.iter().any(|(k, v)| k == "store_id" && v == &id));
                drop(opened);
                assert_eq!(before, host::snapshot(root));
            }
        },
    );
}
