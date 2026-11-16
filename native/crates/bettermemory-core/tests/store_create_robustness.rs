//! Native create's refusals beyond the fixture's (what needs mode bits or
//! symbolic links to set up) and its robustness: each a typed error, no
//! panic, nothing left at the store path; and the system provider's draws.

mod common;

use std::fs;
use std::path::Path;

use bettermemory_core::store::create::{self, CreateError};
use common::created::{fixed, fixture};
use common::stores::scratch;

fn refused(store: &Path, keys: &Path) -> CreateError {
    let err = create::create(store, keys, &mut fixed(&fixture())).expect_err("refused");
    assert!(
        fs::symlink_metadata(create::store_path(store)).is_err(),
        "nothing at the store path after {err}"
    );
    err
}

#[cfg(unix)]
#[test]
fn a_dangling_keys_link_is_refused_naming_it() {
    let dir = scratch("dangling");
    let root = &dir.0;
    std::os::unix::fs::symlink(root.join("nowhere"), root.join("keys")).unwrap();
    let err = refused(&root.join("store"), &root.join("keys"));
    assert_eq!(err.class(), "KeysDirUnreadableError");
    assert_eq!(
        err.to_string(),
        format!(
            "the keys directory {k} cannot be used (it is a symbolic link to {t}, which is \
             not there); no store is created until it can keep the new store's key",
            k = root.join("keys").display(),
            t = root.join("nowhere").display()
        )
    );
    // Refused before anything is written: no store directory either.
    assert!(!root.join("store").exists());
    // A link to nothing on the way to the keys directory.
    let err = refused(&root.join("store"), &root.join("keys/sub"));
    assert_eq!(
        err.to_string(),
        format!(
            "the keys directory {k} cannot be used ({l}, on the way to it, is a symbolic \
             link to {t}, which is not there); no store is created until it can keep the new \
             store's key",
            k = root.join("keys/sub").display(),
            l = root.join("keys").display(),
            t = root.join("nowhere").display()
        )
    );
}

/// Directories this process cannot write (skipped where it can anyway:
/// a root user).
#[cfg(unix)]
#[test]
fn unwritable_directories_are_refused() {
    use std::os::unix::fs::PermissionsExt;
    let dir = scratch("unwritable");
    let root = &dir.0;
    fs::create_dir(root.join("store")).unwrap();
    fs::set_permissions(root.join("store"), fs::Permissions::from_mode(0o500)).unwrap();
    if fs::write(root.join("store/probe"), b"").is_ok() {
        eprintln!("not checked: this user writes a 0500 directory");
        return;
    }
    let err = refused(&root.join("store"), &root.join("keys"));
    assert_eq!(err.class(), "PermissionError", "{err}");
    assert!(!root.join("keys").exists());
    fs::set_permissions(root.join("store"), fs::Permissions::from_mode(0o700)).unwrap();
    fs::create_dir(root.join("keys")).unwrap();
    fs::set_permissions(root.join("keys"), fs::Permissions::from_mode(0o500)).unwrap();
    let err = refused(&root.join("store"), &root.join("keys"));
    assert_eq!(err.class(), "PermissionError", "{err}");
    // The reference leaves a store file here that neither open nor create
    // takes; native leaves the directory as it was.
    let left: Vec<_> = fs::read_dir(root.join("store")).unwrap().collect();
    assert!(left.is_empty(), "{left:?}");
    fs::set_permissions(root.join("keys"), fs::Permissions::from_mode(0o700)).unwrap();
    create::create(
        &root.join("store"),
        &root.join("keys"),
        &mut fixed(&fixture()),
    )
    .expect("created once the directories can be written");
}

#[test]
fn awkward_paths_are_typed_errors() {
    let dir = scratch("awkward");
    let root = &dir.0;
    // A path past every platform's limit.
    let mut long = root.clone();
    for _ in 0..30 {
        long.push("d".repeat(200));
    }
    let err = refused(&long, &root.join("keys"));
    // Windows takes the directories under the verbatim prefix and SQLite
    // then cannot open a file there: a typed error either way.
    assert!(
        matches!(err, CreateError::Io { .. })
            || (cfg!(windows) && matches!(err, CreateError::Sqlite(_))),
        "{err:?}"
    );
    // The keys directory is the store file.
    fs::create_dir(root.join("s")).unwrap();
    // (the keys directory, made after the schema, takes the store's name:
    // the link then finds it taken and the create ends Exists.)
    let err = create::create(
        &root.join("s"),
        &root.join("s/memory.sqlite"),
        &mut fixed(&fixture()),
    )
    .expect_err("keys at the store's path");
    assert!(matches!(err, CreateError::Exists(_)), "{err:?}");
    // A second create at the same path.
    create::create(&root.join("t"), &root.join("keys"), &mut fixed(&fixture())).expect("first");
    let err = create::create(&root.join("t"), &root.join("keys"), &mut fixed(&fixture()))
        .expect_err("second");
    assert!(matches!(err, CreateError::Exists(_)));
    // A provider whose store id would steer a key file out of the keys
    // directory.
    let mut bad = fixed(&fixture());
    bad.store_id = "../escape".to_owned();
    let err = create::create(&root.join("v"), &root.join("keys"), &mut bad).expect_err("bad id");
    assert_eq!(err.class(), "ValueError");
    assert!(!root.join("v/memory.sqlite").exists());
    assert!(!root.join("escape.key").exists());
}

#[test]
fn the_system_provider_draws_fresh_values() {
    use create::Provider;
    let mut system = create::System {
        engine_version: "0".to_owned(),
    };
    let ids = [system.store_id().unwrap(), system.store_id().unwrap()];
    let keys = [system.key().unwrap(), system.key().unwrap()];
    assert!(ids.iter().all(|id| id.len() == 32
        && id
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())));
    assert_ne!(ids[0], ids[1]);
    assert!(keys.iter().all(|k| k.bytes().len() == 32));
    assert_ne!(keys[0], keys[1]);
    assert_ne!(system.membership_store_id().unwrap(), ids[0]);
}

fn listed(dir: &Path) -> Vec<String> {
    let mut out: Vec<String> = fs::read_dir(dir)
        .map(|r| {
            r.flatten()
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .collect()
        })
        .unwrap_or_default();
    out.sort();
    out
}

#[test]
fn a_fixed_clock_with_no_reading_is_an_error() {
    let dir = scratch("clock");
    let mut p = fixed(&fixture());
    p.clock.clear();
    let err =
        create::create(&dir.0.join("s2"), &dir.0.join("keys2"), &mut p).expect_err("no clock");
    assert!(matches!(err, CreateError::Io { .. }), "{err:?}");
    // Nothing of the failed create stays in its keys directory.
    assert_eq!(listed(&dir.0.join("keys2")), Vec::<String>::new());
}

#[test]
fn an_existing_key_ring_file_for_the_id_is_never_written_over() {
    let doc = fixture();
    let id = fixed(&doc).store_id;
    for suffix in [".key", ".head", ".origin", ".path"] {
        let dir = scratch("ring");
        let keys = dir.0.join("keys");
        fs::create_dir(&keys).unwrap();
        let there = keys.join(format!("{id}{suffix}"));
        fs::write(&there, b"another store's").unwrap();
        let err = refused(&dir.0.join("store"), &keys);
        assert!(matches!(err, CreateError::KeyRingExists(_)), "{err:?}");
        assert_eq!(err.class(), "FileExistsError");
        assert_eq!(fs::read(&there).unwrap(), b"another store's");
        assert_eq!(listed(&keys), vec![format!("{id}{suffix}")]);
    }
}

/// A create that loses its name to another at the last step removes every
/// key ring file it wrote.
#[test]
fn a_create_that_fails_after_its_key_removes_its_key_ring_files() {
    let dir = scratch("lost");
    let (store, keys) = (dir.0.join("store"), dir.0.join("keys"));
    let file = store.join("memory.sqlite");
    let err = create::create_with(&store, &keys, &mut fixed(&fixture()), &mut |b| {
        if b == create::Boundary::SidecarsCleared {
            fs::write(&file, b"another create's").unwrap();
        }
    })
    .expect_err("lost the name");
    assert!(matches!(err, CreateError::Exists(_)), "{err:?}");
    assert_eq!(listed(&keys), Vec::<String>::new(), "key ring files left");
    assert_eq!(listed(&store), vec!["memory.sqlite".to_owned()]);
    assert_eq!(fs::read(&file).unwrap(), b"another create's");
}

/// A dangling link at the store's name: the reference follows it and
/// creates the store at the link's target (pinned here by running it when
/// `BM_REFERENCE_PYTHON` imports `bettermemory`); this build refuses with
/// its own words. A deliberate difference.
#[cfg(unix)]
#[test]
fn a_dangling_link_at_the_store_name_is_refused_in_its_own_words() {
    let dir = scratch("storelink");
    let root = &dir.0;
    fs::create_dir(root.join("store")).unwrap();
    std::os::unix::fs::symlink(root.join("target.sqlite"), root.join("store/memory.sqlite"))
        .unwrap();
    let err = create::create(
        &root.join("store"),
        &root.join("keys"),
        &mut fixed(&fixture()),
    )
    .expect_err("refused");
    assert_eq!(err.class(), "DanglingStoreLink");
    assert_eq!(
        err.to_string(),
        format!(
            "{s} is a symbolic link to {t}, which is not there; no store is created through \
             it: remove the link, or create the store at {t}",
            s = root.join("store/memory.sqlite").display(),
            t = root.join("target.sqlite").display()
        )
    );
    assert!(!root.join("target.sqlite").exists());
    match common::created::reference_python() {
        Err(why) => eprintln!("the reference's side not run: {why}"),
        Ok(python) => {
            let out = common::created::run_reference(
                &python,
                &root.join("home"),
                "import sys\nfrom bettermemory.store import Store\n\
                 Store.create(sys.argv[1], keys_dir=sys.argv[2]).close()\n",
                &[&root.join("store"), &root.join("keys")],
            );
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
            assert!(
                root.join("target.sqlite").is_file(),
                "the reference creates at the target"
            );
        }
    }
}

/// The sweep opens only a regular file as a lock: a link planted under a
/// lock's name is passed over, and so is what it points at.
#[cfg(unix)]
#[test]
fn the_sweep_takes_no_planted_link_for_a_lock() {
    let dir = scratch("planted");
    let store = dir.0.join("store");
    fs::create_dir(&store).unwrap();
    fs::write(dir.0.join("elsewhere"), b"x").unwrap();
    std::os::unix::fs::symlink(
        dir.0.join("elsewhere"),
        store.join(".memory.sqlite.creating-1-1-1.lock"),
    )
    .unwrap();
    fs::write(store.join(".memory.sqlite.creating-1-1-1"), b"temp").unwrap();
    assert_eq!(create::sweep_temps(&store), 0);
    assert!(store.join(".memory.sqlite.creating-1-1-1").exists());
    assert!(dir.0.join("elsewhere").exists());
}

/// A dead create's key ring staging file older than an hour goes; a key
/// file never does, nor a staging file of another shape.
#[test]
fn only_old_staging_files_of_this_unit_are_swept_from_the_keys_directory() {
    let doc = fixture();
    let dir = scratch("ringtmp");
    let keys = dir.0.join("keys");
    fs::create_dir(&keys).unwrap();
    let id = "a".repeat(32);
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(7200);
    for name in [
        format!("{id}.key.12x3.tmp"),
        format!("{id}.key"),
        format!("{id}.key.12x3.tmp.bak"),
        format!("{id}.other.12x3.tmp"),
    ] {
        let f = fs::File::create(keys.join(&name)).unwrap();
        f.set_modified(old).unwrap();
    }
    fs::File::create(keys.join(format!("{id}.head.1x1.tmp"))).unwrap();
    create::create(&dir.0.join("store"), &keys, &mut fixed(&doc)).expect("created");
    let left = listed(&keys);
    assert!(!left.contains(&format!("{id}.key.12x3.tmp")), "{left:?}");
    for kept in [
        format!("{id}.key"),
        format!("{id}.key.12x3.tmp.bak"),
        format!("{id}.other.12x3.tmp"),
        format!("{id}.head.1x1.tmp"),
    ] {
        assert!(left.contains(&kept), "{kept} was removed: {left:?}");
    }
}

/// A kill between the link and the temporary name's removal leaves a
/// second name of the live store; the next create removes it before it
/// refuses.
#[test]
fn a_later_create_sweeps_a_dead_creates_second_name_before_refusing() {
    let dir = scratch("secondname");
    let store = dir.0.join("store");
    create::create(&store, &dir.0.join("keys"), &mut fixed(&fixture())).expect("created");
    fs::hard_link(
        store.join("memory.sqlite"),
        store.join(".memory.sqlite.creating-9-9-9"),
    )
    .unwrap();
    fs::write(store.join(".memory.sqlite.creating-9-9-9.lock"), b"").unwrap();
    let err =
        create::create(&store, &dir.0.join("keys"), &mut fixed(&fixture())).expect_err("exists");
    assert!(matches!(err, CreateError::Exists(_)));
    assert_eq!(listed(&store), vec!["memory.sqlite".to_owned()]);
}

/// A valid foreign WAL placed at the store's name after the first sidecar
/// pass is removed by the second, just before the link: the store that
/// opens is the new one.
#[test]
fn a_foreign_wal_placed_after_the_first_clear_does_not_replace_the_store() {
    let dir = scratch("foreignwal");
    let root = &dir.0;
    let store = root.join("store");
    let other = root.join("other.sqlite");
    let conn = rusqlite::Connection::open(&other).unwrap();
    conn.query_row("PRAGMA journal_mode = WAL", [], |_| Ok(()))
        .unwrap();
    conn.execute_batch(
        "PRAGMA wal_autocheckpoint = 0;
         CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
    )
    .unwrap();
    conn.execute("INSERT INTO meta VALUES ('store_id', ?)", ["f".repeat(32)])
        .unwrap();
    let made = create::create_with(
        &store,
        &root.join("keys"),
        &mut fixed(&fixture()),
        &mut |b| {
            if b == create::Boundary::SidecarsCleared {
                for suffix in ["-wal", "-shm"] {
                    fs::copy(
                        root.join(format!("other.sqlite{suffix}")),
                        store.join(format!("memory.sqlite{suffix}")),
                    )
                    .unwrap();
                }
            }
        },
    )
    .expect("created");
    drop(conn);
    assert_eq!(listed(&store), vec!["memory.sqlite".to_owned()]);
    let file = bettermemory_core::store::StoreFile::open(&made.path).expect("opens");
    let id = file
        .meta()
        .unwrap()
        .into_iter()
        .find(|(k, _)| k == "store_id")
        .map(|(_, v)| v);
    assert_eq!(id.as_deref(), Some(made.store_id.as_str()));
}
