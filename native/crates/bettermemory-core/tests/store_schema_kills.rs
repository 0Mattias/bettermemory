//! Cases a plausible wrong `store::schema` fails: an id drawn when nothing
//! is wanted, a column found by a comment, index names compared in the
//! wrong case, a backfill marked `pending` in the table and not only in
//! meta, the warning's words, the v8 name's newline and alphabet, Windows
//! drives in orphan names, and `absent_text`'s spelling of its arguments.

mod common;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::schema::{self, Absent, StepError};
use bettermemory_core::unicode;
use common::schema_steps::{base, master, meta_of, Draws};
use rusqlite::Connection;

fn store(setup: &[&str]) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    base(&conn);
    for sql in setup {
        conn.execute_batch(sql).unwrap();
    }
    conn
}

fn draws() -> Draws {
    Draws {
        minted: "ab".repeat(16),
        drawn: 0,
    }
}

const NO_ID: &str = "DELETE FROM meta WHERE key = 'membership_store_id'";

#[test]
fn nothing_wanted_draws_no_id_and_writes_nothing() {
    let conn = store(&[]);
    let mut meta = meta_of(&conn);
    let mut d = draws();
    let before = master(&conn);
    assert_eq!(
        schema::add_membership_tables(&conn, "S", &mut meta, &mut d).unwrap(),
        None
    );
    assert_eq!(d.drawn, 0);
    assert_eq!(master(&conn), before);
    assert!(schema::wanted(&before, &meta).is_empty());
}

#[test]
fn an_empty_id_is_wanted_but_the_table_keeps_it() {
    let conn = store(&["UPDATE meta SET value = '' WHERE key = 'membership_store_id'"]);
    let mut meta = meta_of(&conn);
    assert_eq!(schema::wanted(&master(&conn), &meta), ["the store id"]);
    let mut d = draws();
    schema::add_membership_tables(&conn, "S", &mut meta, &mut d).unwrap();
    assert_eq!(d.drawn, 1);
    assert_eq!(meta, [("membership_store_id".to_owned(), String::new())]);
}

#[test]
fn a_column_named_in_a_comment_counts_as_there() {
    let sql = "CREATE TABLE checkouts (id TEXT PRIMARY KEY -- unresolved_json\n)";
    let conn = store(&["DROP TABLE checkouts", sql, NO_ID]);
    let meta = meta_of(&conn);
    assert!(!schema::wanted(&master(&conn), &meta).contains(&"checkouts.unresolved_json".into()));
    let plain = store(&["DROP TABLE checkouts", "CREATE TABLE checkouts (id TEXT)"]);
    assert!(schema::wanted(&master(&plain), &meta_of(&plain))
        .contains(&"checkouts.unresolved_json".into()));
    let tail = store(&[
        "DROP TABLE checkouts",
        "CREATE TABLE checkouts (x unresolved_jsonx)",
    ]);
    assert!(schema::wanted(&master(&tail), &meta_of(&tail))
        .contains(&"checkouts.unresolved_json".into()));
}

#[test]
fn a_column_in_other_case_fails_the_alter_and_rolls_back() {
    let conn = store(&[
        "ALTER TABLE checkouts RENAME COLUMN unresolved_json TO Unresolved_JSON",
        NO_ID,
    ]);
    let mut meta = meta_of(&conn);
    let got = schema::add_membership_tables(&conn, "S", &mut meta, &mut draws());
    match got {
        Err(StepError::Sqlite { class, message }) => {
            assert_eq!(class, "OperationalError");
            assert_eq!(message, "duplicate column name: unresolved_json");
        }
        other => panic!("{other:?}"),
    }
    assert!(conn.is_autocommit());
    assert!(meta_of(&conn).is_empty());
    assert!(meta.is_empty());
}

#[test]
fn a_failed_statement_s_text_is_sqlite_s_message_alone() {
    let conn = store(&["DROP TABLE checkouts", "CREATE TABLE checkouts (id TEXT)"]);
    let mut meta = meta_of(&conn);
    match schema::add_membership_tables(&conn, "S", &mut meta, &mut draws()) {
        Err(StepError::Sqlite { class, message }) => {
            assert_eq!(
                (class, message.as_str()),
                ("OperationalError", "no such column: machine")
            );
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn membership_names_are_exact_and_index_names_are_not() {
    let conn = store(&[
        "DROP INDEX log_corroborations",
        "CREATE INDEX LOG_CORROBORATIONS ON log(session) WHERE kind = 'memory_put'",
        "DROP INDEX log_by_session",
        "CREATE INDEX LOG_BY_SESSION ON log(session, seq)",
    ]);
    let meta = meta_of(&conn);
    assert_eq!(
        schema::wanted(&master(&conn), &meta),
        ["log_corroborations"]
    );
    let before = master(&conn);
    assert_eq!(schema::add_session_index(&conn, "S").unwrap(), None);
    assert_eq!(master(&conn), before);
}

#[test]
fn the_backfill_mark_is_pending_in_meta_whatever_the_table_holds() {
    let conn = store(&["INSERT INTO meta VALUES ('returned_backfill', 'done')"]);
    let mut meta = meta_of(&conn);
    let (w, got) = schema::add_returned_marks(&conn, "S", &mut meta, &mut draws());
    got.unwrap();
    assert!(w.is_empty());
    assert!(meta.contains(&("returned_backfill".to_owned(), "pending".to_owned())));
    let held: String = conn
        .query_row(
            "SELECT value FROM meta WHERE key = 'returned_backfill'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(held, "done");
}

#[cfg(unix)]
#[test]
fn a_read_only_store_is_named_with_what_it_lacks() {
    use std::os::unix::fs::PermissionsExt;
    let dir = std::path::Path::new(env!("CARGO_TARGET_TMPDIR"))
        .join(format!("schema-kills-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("s.sqlite");
    let conn = Connection::open(&path).unwrap();
    base(&conn);
    conn.execute_batch(NO_ID).unwrap();
    conn.execute_batch("DROP INDEX proposals_by_state").unwrap();
    drop(conn);
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o444)).unwrap();
    let conn = Connection::open(&path).unwrap();
    let mut meta = meta_of(&conn);
    let (w, got) = schema::add_proposals_index(&conn, "P", &mut meta, &mut draws());
    got.unwrap();
    let root = writes_read_only(&dir);
    if !root {
        assert_eq!(
            w,
            [
                "store P is read-only; not added: the store id",
                "store P is read-only; not added: proposals_by_state",
            ]
        );
    }
    drop(conn);
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    let _ = std::fs::remove_dir_all(&dir);
}

/// Whether this process writes a mode-0444 file anyway (root).
#[cfg(unix)]
fn writes_read_only(dir: &std::path::Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    let probe = dir.join("probe");
    std::fs::write(&probe, "").unwrap();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o444)).unwrap();
    std::fs::OpenOptions::new().write(true).open(&probe).is_ok()
}

const ULID: &str = "01HZY3K6V0A7Q8R9S0T1V2W3X4";

#[test]
fn v8_names() {
    let yes = |s: String| assert!(schema::is_v8_name(s.as_bytes()), "{s:?}");
    let no = |s: String| assert!(!schema::is_v8_name(s.as_bytes()), "{s:?}");
    yes(format!("2024-01-01-a-{ULID}.md"));
    yes(format!("-{ULID}.md"));
    yes(format!("a-b-{ULID}.md"));
    no(format!("a\nb-{ULID}.md"));
    no(format!("x-{ULID}.md\n"));
    no(format!("{ULID}.md"));
    no(format!("x-{}.md", ULID.to_lowercase()));
    for bad in ['I', 'L', 'O', 'U'] {
        no(format!("x-{}{bad}.md", &ULID[..25]));
    }
    no(format!("x-{ULID}.MD"));
    no(format!("x-{ULID}0.md"));
    yes(".events.x.jsonl".into());
    yes(".events..jsonl".into());
    no(".events.jsonl/".into());
    no(".tombstones2".into());
    no(".eventsX.jsonl".into());
    no(".events-3.jsonl".into());
}

#[test]
fn orphan_names_by_flavour() {
    let v = unicode::cps(r#"["c:", "c:x", "a\\b", ":", "ab:c", ".", "..", ""]"#);
    let posix: Vec<String> = schema::orphan_names(Some(&v), false)
        .iter()
        .map(|n| n.iter().map(|&c| char::from_u32(c).unwrap()).collect())
        .collect();
    let windows: Vec<String> = schema::orphan_names(Some(&v), true)
        .iter()
        .map(|n| n.iter().map(|&c| char::from_u32(c).unwrap()).collect())
        .collect();
    assert_eq!(posix, ["c:", "c:x", "a\\b", ":", "ab:c", ".."]);
    assert_eq!(windows, [":", "ab:c", ".."]);
    assert!(schema::orphan_names(None, false).is_empty());
    assert!(schema::orphan_names(Some(&unicode::cps("{\"a\": 1}")), false).is_empty());
}

#[test]
fn absent_text_spells_its_arguments() {
    let s = |t: &str| PyValue::Str(unicode::cps(t));
    let text = |kind, args: &[PyValue]| -> String {
        schema::absent_text(kind, args)
            .unwrap()
            .iter()
            .map(|&c| char::from_u32(c).unwrap())
            .collect()
    };
    assert_eq!(
        text(Absent::NotFound, &[s("k")]),
        "no record k in the store"
    );
    assert_eq!(text(Absent::NotFound, &[]), "no record () in the store");
    assert_eq!(text(Absent::Message, &[s("a"), s("b")]), "('a', 'b')");
    assert_eq!(text(Absent::Message, &[s("it's")]), "it's");
    assert_eq!(
        text(Absent::Message, &[PyValue::Tuple(vec![s("x")])]),
        "('x',)"
    );
}

/// A file system of named directories and regular files, nothing else.
#[cfg(unix)]
struct Tree {
    dirs: Vec<(&'static str, Vec<&'static str>)>,
    files: Vec<&'static str>,
}

#[cfg(unix)]
mod tree {
    use super::Tree;
    use bettermemory_core::fsdata::{Access, Fs, Kind, OsError, PyErr, Stat, StatResult};

    fn stat_of(kind: Kind) -> StatResult {
        StatResult {
            kind,
            mode: None,
            ino: None,
            dev: None,
            size: 0,
            mtime_ns: 0,
            ctime_ns: None,
            flags: 0,
            file_attributes: 0,
            reparse_tag: Some(0),
        }
    }

    impl Fs for Tree {
        type File = ();
        fn open(&self, _: &[u8]) -> Result<(), PyErr> {
            Err(PyErr::Os)
        }
        fn fstat(&self, _: &mut ()) -> Result<Stat, PyErr> {
            Err(PyErr::Os)
        }
        fn read(&self, _: &mut (), _: usize) -> Result<Vec<u8>, PyErr> {
            Err(PyErr::Os)
        }
        fn access(&self, _: &[u8], _: Access) -> Result<bool, PyErr> {
            Ok(false)
        }
        fn open_no_follow(&self, _: &[u8]) -> Result<(), OsError> {
            Err(OsError::of_class(PyErr::Os))
        }
        fn lstat(&self, _: &[u8]) -> Result<Kind, PyErr> {
            Err(PyErr::Os)
        }
        fn readlink(&self, _: &[u8]) -> Result<Vec<u8>, PyErr> {
            Err(PyErr::Os)
        }
        fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
            if self.dirs.iter().any(|(d, _)| d.as_bytes() == path) {
                Ok(stat_of(Kind::Directory))
            } else if self.files.iter().any(|f| f.as_bytes() == path) {
                Ok(stat_of(Kind::Regular))
            } else {
                Err(OsError::of_class(PyErr::FileNotFound))
            }
        }
        fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
            self.stat(path)
        }
        /// Lists a named directory, and also a named file as if it were
        /// one, so only the directory test keeps a file parent out.
        fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
            self.dirs
                .iter()
                .find(|(d, _)| d.as_bytes() == path)
                .map(|(_, names)| names.iter().map(|n| n.as_bytes().to_vec()).collect())
                .or_else(|| {
                    self.files
                        .iter()
                        .any(|f| f.as_bytes() == path)
                        .then(|| vec![b".events.jsonl".to_vec()])
                })
                .ok_or_else(|| OsError::of_class(PyErr::FileNotFound))
        }
    }
}

/// `Path("/memory.sqlite").parent` is `/`, and the message names it so.
#[cfg(unix)]
#[test]
fn a_store_at_the_root_names_the_root() {
    let fs = Tree {
        dirs: vec![("/", vec![".events.jsonl"])],
        files: vec![],
    };
    let got = schema::refuse_unmigrated_v8(&fs, "/memory.sqlite").unwrap();
    assert!(got.starts_with("/ holds a bettermemory 8 store"), "{got}");
}

/// A parent that is not a directory refuses nothing, even where listing
/// it would name a v8 file (`not path.parent.is_dir()` comes first).
#[cfg(unix)]
#[test]
fn a_parent_that_is_a_file_refuses_nothing() {
    let fs = Tree {
        dirs: vec![],
        files: vec!["/f"],
    };
    assert_eq!(schema::refuse_unmigrated_v8(&fs, "/f/memory.sqlite"), None);
}

/// The file test reads the path as `Path` spells it: a trailing `/` or
/// `/.` is dropped, so a store file there refuses nothing.
#[cfg(unix)]
#[test]
fn a_trailing_separator_still_finds_the_store_file() {
    let fs = Tree {
        dirs: vec![("/d", vec!["memory.sqlite", ".events.jsonl"])],
        files: vec!["/d/memory.sqlite"],
    };
    for path in [
        "/d/memory.sqlite/",
        "/d/memory.sqlite/.",
        "/d//memory.sqlite",
    ] {
        assert_eq!(schema::refuse_unmigrated_v8(&fs, path), None, "{path}");
    }
    assert!(schema::refuse_unmigrated_v8(&fs, "/d/other.sqlite/").is_some());
}

/// An error whose text spells "read only" with a space is the read-only
/// store's: the reference removes the spaces before it looks.
#[test]
fn read_only_is_found_across_a_space() {
    let conn = store(&[
        "CREATE TRIGGER meta_put AFTER INSERT ON meta BEGIN INSERT INTO \"read only\" VALUES (1); END",
    ]);
    let mut meta = meta_of(&conn);
    let (w, got) = schema::add_returned_marks(&conn, "S", &mut meta, &mut draws());
    got.unwrap();
    assert_eq!(w, ["store S is read-only; no backfill marked"]);
}
