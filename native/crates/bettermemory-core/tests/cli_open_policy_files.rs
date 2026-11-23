//! Real path admission, read-only SQLite ownership and restore exclusion.
mod common;

use bettermemory_core::argparse::ArgumentParser;
use bettermemory_core::cli::common::{open_read_only, ReadOnlyError, ReaderFailure};
use bettermemory_core::recall_prompt::PyException;
use bettermemory_core::unicode::cps;
use std::path::Path;

fn parser() -> ArgumentParser {
    ArgumentParser::new("bettermemory log", None, true)
}

fn no_reader(_: &Path) -> Result<(), ReaderFailure<PyException>> {
    panic!("reader was called after an early refusal")
}

#[test]
fn absent_and_nonregular_always_use_the_parser() {
    let tmp = common::stores::scratch("cli-early");
    let regular = tmp.0.join("ordinary");
    std::fs::write(&regular, "").unwrap();
    for path in [tmp.0.join("missing"), tmp.0.clone(), regular.join("child")] {
        match open_read_only(&parser(), &path, 75, no_reader) {
            Err(ReadOnlyError::Exit(exit)) => {
                assert_eq!(exit.code, 2);
                assert!(exit.stderr.starts_with(&cps("usage: bettermemory log")));
            }
            _ => panic!("missing or non-regular object was admitted"),
        }
    }
    if let Err(ReadOnlyError::Stat(raised)) =
        open_read_only(&parser(), Path::new("a\0b"), 1, no_reader)
    {
        assert_eq!(raised.class, "ValueError");
        assert_eq!(raised.message, cps("stat: embedded null character in path"));
    } else {
        panic!("stat ValueError was changed");
    }
}

#[cfg(unix)]
mod posix {
    use super::*;
    use std::fs;
    use std::os::unix::fs::symlink;
    use std::path::PathBuf;
    use std::time::Duration;

    use bettermemory_core::store::{restore_lock, StoreError, StoreFile};
    use bettermemory_core::store_refusal::BUNDLE_MANIFEST_NAME;

    fn exception(class: &str, message: String) -> PyException {
        PyException {
            class: class.into(),
            message: cps(&message),
        }
    }

    // Store.open selects the filename before StoreFile takes its one shared hold.
    fn reader(supplied: &Path) -> Result<StoreFile, ReaderFailure<PyException>> {
        let selected = if supplied.file_name() == Some("memory.sqlite".as_ref()) {
            supplied.to_owned()
        } else {
            supplied.join("memory.sqlite")
        };
        if !selected.is_file() {
            return Err(ReaderFailure::Unreadable(exception(
                "ValueError",
                format!("no store at {}", selected.display()),
            )));
        }
        StoreFile::open(&selected).map_err(|error| match error {
            StoreError::RestoreRunning => ReaderFailure::RestoreRunning(exception(
                "RestoreRunningError",
                format!(
                    "{} is not opened: {}",
                    selected.display(),
                    restore_lock::RESTORE_RUNNING_WHY
                ),
            )),
            StoreError::Sqlite(e) => {
                ReaderFailure::Unreadable(exception("DatabaseError", e.to_string()))
            }
            e => ReaderFailure::Raised(exception("StoreError", e.to_string())),
        })
    }

    fn tree_contents(dir: &Path) -> Vec<(PathBuf, Vec<u8>)> {
        let mut files: Vec<_> = fs::read_dir(dir)
            .unwrap()
            .map(|e| {
                let p = e.unwrap().path();
                (p.file_name().unwrap().into(), fs::read(p).unwrap())
            })
            .collect();
        files.sort();
        files
    }

    #[test]
    fn pathlib_spelling_keeps_raw_names_and_symlink_parent_traversal() {
        use std::ffi::OsStr;
        use std::os::unix::ffi::OsStrExt;
        let temp = common::stores::scratch("cli-path-spelling");
        let root = &temp.0;
        let nested = root.join("actual/nested");
        fs::create_dir_all(&nested).unwrap();
        symlink(&nested, root.join("alias")).unwrap();
        let raw_name = OsStr::from_bytes(b"name-\xff");
        let name = match fs::write(root.join("actual").join(raw_name), b"through link") {
            Ok(()) => raw_name,
            // APFS refuses to create this name but still answers ENOENT on stat.
            Err(error)
                if cfg!(target_os = "macos")
                    && matches!(
                        error.raw_os_error().and_then(bettermemory_sys::errno_name),
                        Some("EPERM" | "EILSEQ")
                    ) =>
            {
                let missing = root.join("alias/..").join(raw_name).join(".");
                let Err(ReadOnlyError::Exit(exit)) =
                    open_read_only(&parser(), &missing, 17, no_reader)
                else {
                    panic!("raw missing name was lost")
                };
                assert_eq!(exit.code, 2);
                assert!(exit
                    .stderr
                    .ends_with(&[u32::from(b'-'), 0xdcff, u32::from(b'\n')]));
                let name = OsStr::new("name-π");
                fs::write(root.join("actual").join(name), b"through link").unwrap();
                name
            }
            Err(error) => panic!("creating raw filename: {error}"),
        };
        fs::write(root.join(name), b"wrong lexical parent").unwrap();
        let expected = root.join("alias/..").join(name);
        for (middle, tail) in [("/alias/../", "/"), ("/./alias//../", "/.")] {
            let mut input = root.as_os_str().as_bytes().to_vec();
            input.extend_from_slice(middle.as_bytes());
            input.extend_from_slice(name.as_bytes());
            input.extend_from_slice(tail.as_bytes());
            let got = open_read_only(&parser(), Path::new(OsStr::from_bytes(&input)), 1, |path| {
                assert_eq!(path.as_os_str().as_bytes(), expected.as_os_str().as_bytes());
                Ok::<_, ReaderFailure<PyException>>(fs::read(path).unwrap())
            })
            .unwrap();
            assert_eq!(got, b"through link");
        }
    }

    #[test]
    fn signed_reader_holds_restore_until_drop_and_releases_on_error() {
        let temp = common::stores::scratch("cli-owner");
        common::opened::made("created", &temp.0);
        let store = temp.0.join("memory.sqlite");
        let keys = temp.0.join("keys");
        let initial_bytes = fs::read(&store).unwrap();
        let initial_keys = tree_contents(&keys);
        let acquire = || restore_lock::acquire(&store, Duration::ZERO).unwrap();
        let owner = open_read_only(&parser(), &store, 1, reader).unwrap();
        assert!(owner
            .meta()
            .unwrap()
            .iter()
            .any(|(key, _)| key == "store_id"));
        assert!(acquire().is_none());
        assert!(owner
            .meta()
            .unwrap()
            .iter()
            .any(|(key, _)| key == "store_id"));
        drop(owner);
        let restore = acquire().expect("reader released exclusive access");
        std::thread::scope(|scope| {
            scope
                .spawn(|| match open_read_only(&parser(), &store, 2, reader) {
                    Err(ReadOnlyError::Exit(exit)) => {
                        assert_eq!(exit.code, 75);
                        assert!(exit.stderr.ends_with(&cps(
                            "a restore is running; try again when it has finished\n"
                        )));
                    }
                    _ => panic!("read admitted during restore"),
                })
                .join()
                .unwrap();
        });
        restore.release(true);
        let result = open_read_only(&parser(), &store, 1, |given| {
            let _owner = reader(given)?;
            Err::<(), _>(ReaderFailure::Raised(exception(
                "application",
                "later".into(),
            )))
        });
        assert!(matches!(result, Err(ReadOnlyError::Open(_))));
        acquire()
            .expect("constructor error released the read hold")
            .release(true);
        assert_eq!(fs::read(&store).unwrap(), initial_bytes);
        assert_eq!(tree_contents(&keys), initial_keys);

        let renamed = temp.0.join("other.sqlite");
        fs::rename(&store, &renamed).unwrap();
        match open_read_only(&parser(), &renamed, 1, reader) {
            Err(ReadOnlyError::Exit(exit)) => {
                assert_eq!(exit.code, 1);
                assert!(exit.stderr.ends_with(&cps("other.sqlite/memory.sqlite\n")));
            }
            _ => panic!("alternate name bypassed store_path"),
        }
    }

    #[test]
    fn live_and_orphan_wal_frames_remain_visible_through_the_owner() {
        let (temp, _) = common::stores::case_store("intact");
        let db = temp.0.join("memory.sqlite");
        let writer = rusqlite::Connection::open(&db).unwrap();
        writer.execute_batch("PRAGMA wal_autocheckpoint=0; INSERT INTO meta(key,value) VALUES ('cli_read_test','frame')").unwrap();
        let orphan = temp.0.join("orphan");
        fs::create_dir(&orphan).unwrap();
        for name in ["memory.sqlite", "memory.sqlite-wal"] {
            fs::copy(temp.0.join(name), orphan.join(name)).unwrap();
        }
        for directory in [&temp.0, &orphan] {
            let main = directory.join("memory.sqlite");
            let wal = directory.join("memory.sqlite-wal");
            let before = (fs::read(&main).unwrap(), fs::read(&wal).unwrap());
            let owner = open_read_only(&parser(), &main, 1, reader).unwrap();
            assert!(owner
                .meta()
                .unwrap()
                .contains(&("cli_read_test".into(), "frame".into())));
            drop(owner);
            assert_eq!((fs::read(main).unwrap(), fs::read(wal).unwrap()), before);
        }
        assert!(temp.0.join("memory.sqlite-shm").exists());
        assert!(!orphan.join("memory.sqlite-shm").exists());
        drop(writer);
    }

    #[test]
    fn fifo_is_refused_without_opening_it() {
        let temp = common::stores::scratch("cli-fifo");
        let fifo = temp.0.join("fifo");
        assert!(std::process::Command::new("mkfifo")
            .arg(&fifo)
            .status()
            .unwrap()
            .success());
        assert!(
            matches!(open_read_only(&parser(), &fifo, 1, no_reader), Err(ReadOnlyError::Exit(exit)) if exit.code==2)
        );
    }

    #[test]
    fn named_bundle_resolved_bundle_and_hard_link_precede_reader() {
        let temp = common::stores::scratch("cli-bundle");
        let bundle = temp.0.join("backup");
        fs::create_dir(&bundle).unwrap();
        let db = bundle.join("memory.sqlite");
        fs::write(&db, b"disposable").unwrap();
        let link = temp.0.join("alias");
        symlink(&db, &link).unwrap();
        for content in [
            br#"{"format":"bettermemory.backup"}"#.as_slice(),
            b"invalid",
        ] {
            fs::write(bundle.join(BUNDLE_MANIFEST_NAME), content).unwrap();
            for supplied in [&db, &link] {
                match open_read_only(&parser(), supplied, 17, no_reader) {
                    Err(ReadOnlyError::Exit(exit)) => {
                        assert_eq!(exit.code, 17);
                        let shown = format!("{} is in a backup bundle", supplied.display());
                        assert!(exit
                            .stderr
                            .windows(shown.len())
                            .any(|part| part == cps(&shown)));
                    }
                    _ => panic!("bundle was admitted"),
                }
            }
        }
        fs::write(bundle.join(BUNDLE_MANIFEST_NAME), br#"{"format":"other"}"#).unwrap();
        let token = Box::new(17);
        let address = std::ptr::from_ref(&*token);
        let got = open_read_only(&parser(), &link, 2, |given| {
            assert_eq!(given, link);
            Ok::<_, ReaderFailure<PyException>>(token)
        })
        .unwrap();
        assert_eq!(std::ptr::from_ref(&*got), address);
        let hard = temp.0.join("hard");
        fs::hard_link(&db, &hard).unwrap();
        match open_read_only(&parser(), &hard, -9, no_reader) {
            Err(ReadOnlyError::Exit(exit)) => {
                assert_eq!(exit.code, -9);
                assert!(exit
                    .stderr
                    .ends_with(&cps("and the copy opens for reading only)\n")));
            }
            _ => panic!("hard link admitted"),
        }
        fs::remove_file(db).unwrap();
        assert!(
            matches!(open_read_only(&parser(), &link,1,no_reader),Err(ReadOnlyError::Exit(e)) if e.code==2)
        );
    }
}
