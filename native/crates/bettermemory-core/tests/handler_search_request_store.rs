//! Full signed records and the order of Python id binding in Store.load_many.
mod common;

use bettermemory_core::handlers::search_request::{Hydrated, Hydrator};
use bettermemory_core::records::{Memory, Origin};
use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::store::record_reads::{
    get_many, get_many_python_ids, get_memory, light_many, ReadError,
};
use bettermemory_core::store::writers::Put;
use bettermemory_core::unicode::cps;
use common::{opened, provenance, record_calls::Minting, stores, written};
use rusqlite::hooks::{AuthAction, AuthContext, Authorization};
use rusqlite::{Connection, ErrorCode, OpenFlags};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

#[test]
fn full_records_include_signed_fields_and_follow_the_requested_ids() {
    let scratch = stores::scratch("search-request-records");
    let (file, keys) = provenance::created_store(&scratch.0);
    let mut store = open_for_writing(&file, &keys, &opened::engine_provider()).unwrap();
    let record = |n, body| {
        let payload = written::memory_payload(&written::ulid(n, "01K"), body, &["global"], &[]);
        Memory::from_payload(payload.get("memory").unwrap()).unwrap()
    };
    let mut rich = record(1, "signed search result");
    let plain = record(2, "second result");
    let base = cps("café/東京/");
    rich.fields.origin = Some(Origin {
        cwd: Some(base.clone()),
        ..Origin::default()
    });
    rich.fields.verified_paths = vec![cps("present")];
    rich.fields.verified_absent_paths = vec![cps("absent")];
    let mut target = base;
    target.push(0xdcff);
    rich.fields.verified_path_targets = Some(vec![(cps("present"), Some(target.clone()))]);
    rich.fields.verified_absent_path_targets = Some(vec![(cps("absent"), Some(target))]);
    let at = written::instant("2001-02-03T11:00:00.000001Z");
    let mut clock = Minting {
        inner: written::clock(&[at; 4]),
        next: 0,
    };
    for memory in [&rich, &plain] {
        store
            .put_memory(&mut clock, memory, Put::default())
            .unwrap();
    }
    let identity = store.store_id().to_owned();
    drop(store);
    assert_eq!(written::native_verify(&scratch.0, &identity), ("ok", 2));

    let conn = Connection::open_with_flags(&file, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let ids = [
        written::ulid(2, "01K"),
        "missing\0🙂".to_owned(),
        written::ulid(1, "01K"),
        written::ulid(2, "01K"),
    ];
    let python_ids: Vec<_> = ids.iter().map(|id| cps(id)).collect();
    let found = get_many_python_ids(&conn, &python_ids).unwrap();
    assert_eq!(found, [plain.clone(), rich, plain]);
    assert_eq!(get_many(&conn, &ids).unwrap(), found);

    let rid = conn
        .query_row("SELECT rowid FROM memories WHERE id = ?", [&ids[2]], |r| {
            r.get(0)
        })
        .unwrap();
    let light = light_many(&conn, &[rid]).unwrap().pop().unwrap();
    assert!(light.fields.verified_path_targets.is_none());
    let ranked = light.to_search_record().unwrap();
    let mut missing = ranked.clone();
    missing.id = "missing candidate".to_owned();
    let candidates = [ranked.clone(), missing, ranked];
    let mut request = Hydrator::new(&conn);
    let hits = request.hydrate(&candidates).unwrap();
    let [Hydrated::Full(first), Hydrated::Original(absent), Hydrated::Full(repeated)] =
        hits.as_slice()
    else {
        panic!("the two ranked hits must hydrate and the missing candidate must survive");
    };
    assert_eq!(first.as_ref(), &found[1]);
    assert!(std::ptr::eq(*absent, &candidates[1]));
    assert!(Arc::ptr_eq(first, repeated));
    let again = request.hydrate(&candidates[..1]).unwrap();
    assert!(matches!(&again[0], Hydrated::Full(current) if Arc::ptr_eq(first, current)));

    let mut next_request = Hydrator::new(&conn);
    assert!(next_request.full.is_empty());
    let next = next_request.hydrate(&candidates[..1]).unwrap();
    assert!(
        matches!(&next[0], Hydrated::Full(current) if !Arc::ptr_eq(first, current) && current == first)
    );

    let mut invalid = light.clone();
    invalid.fields.id = vec![0xd800];
    let mut failed_request = Hydrator::new(&conn);
    assert!(matches!(
        failed_request.hydrate(&[light, invalid]),
        Err(ReadError::Raised {
            class: "UnicodeEncodeError",
            ..
        })
    ));
    assert!(failed_request.full.is_empty());

    let migration = Connection::open(&file).unwrap();
    let expected = get_memory(&migration, &ids[2]).unwrap();
    migration
        .execute_batch("BEGIN; ALTER TABLE memories ADD COLUMN future_field TEXT")
        .unwrap();
    assert_eq!(get_memory(&migration, &ids[2]).unwrap(), expected);
    assert_eq!(get_many(&migration, &ids[2..3]).unwrap(), vec![expected]);
    migration.execute_batch("ROLLBACK").unwrap();
}

#[test]
fn light_and_full_lookups_share_the_owned_store_statement_limit() {
    use bettermemory_core::store::StoreFile;
    fn fills_cache(conn: &Connection, shapes: i64) -> bool {
        assert!(get_many(conn, &["absent".into()]).unwrap().is_empty());
        for size in 1..=shapes {
            let rowids: Vec<_> = (1_000_000..1_000_000 + size).collect();
            assert!(light_many(conn, &rowids).unwrap().is_empty());
        }
        conn.execute_batch("SELECT * FROM nonexistent_source")
            .unwrap_err();
        let err = get_many_python_ids(conn, &[vec![0xd800]]).unwrap_err();
        match err {
            ReadError::Bind { .. } => false,
            ReadError::Raised {
                class: "UnicodeEncodeError",
                ..
            } => true,
            other => panic!("unexpected record-read error {other}"),
        }
    }
    let (scratch, _) = stores::case_store("intact");
    let db = scratch.0.join("memory.sqlite");
    for shapes in [127, 128] {
        let store = StoreFile::open(&db).unwrap();
        assert_eq!(fills_cache(store.connection(), shapes), shapes == 128);
    }
    let owner = stores::scratch("record-cache-owner");
    let (file, keys) = provenance::created_store(&owner.0);
    for shapes in [127, 128] {
        let store = open_for_writing(&file, &keys, &opened::engine_provider()).unwrap();
        assert_eq!(fills_cache(store.connection(), shapes), shapes == 128);
    }
    for (capacity, shapes, evicted) in [(0, 0, true), (3, 2, false), (3, 3, true)] {
        let conn = Connection::open(&db).unwrap();
        conn.set_prepared_statement_cache_capacity(capacity);
        assert_eq!(fills_cache(&conn, shapes), evicted);
    }
}

#[test]
fn missing_ids_execute_before_a_later_encoding_error() {
    let mut conn = common::store_reads::empty();
    let steps = Arc::new(AtomicUsize::new(0));
    let observed = steps.clone();
    conn.progress_handler(
        1,
        Some(move || {
            observed.fetch_add(1, Ordering::Relaxed);
            false
        }),
    )
    .unwrap();

    let bad = [cps("🙂a"), vec![0xd800, 0xdfff, 0xd800]].concat();
    let alone = get_many_python_ids(&conn, std::slice::from_ref(&bad)).unwrap_err();
    let first_steps = steps.swap(0, Ordering::Relaxed);
    let after_missing = get_many_python_ids(&conn, &[cps("missing"), bad]).unwrap_err();
    assert!(steps.load(Ordering::Relaxed) > first_steps);
    for error in [alone, after_missing] {
        assert_eq!(error.class(), "UnicodeEncodeError");
        assert_eq!(
            error.text(),
            Some(cps(
                "'utf-8' codec can't encode characters in position 2-4: surrogates not allowed"
            ))
        );
    }
    let single = get_many_python_ids(&conn, &[vec![0xdcff]]).unwrap_err();
    assert_eq!(
        single.text(),
        Some(cps(
            "'utf-8' codec can't encode character '\\udcff' in position 0: surrogates not allowed"
        ))
    );
}

#[test]
fn prepare_and_earlier_record_errors_keep_their_precedence() {
    let (scratch, _) = stores::case_store("intact");
    let file = scratch.0.join("memory.sqlite");
    let conn = Connection::open(&file).unwrap();
    let id: String = conn
        .query_row("SELECT id FROM memories LIMIT 1", [], |r| r.get(0))
        .unwrap();
    conn.execute(
        "UPDATE memories SET updated = 'broken-date' WHERE id = ?",
        [&id],
    )
    .unwrap();
    let bad = vec![0xd800];
    let error = get_many_python_ids(&conn, &[cps(&id), bad.clone()]).unwrap_err();
    assert!(matches!(error, ReadError::Record(_)));
    assert_eq!(error.class(), "ValueError");

    let mut conn = Connection::open(&file).unwrap();
    conn.authorizer(Some(|ctx: AuthContext<'_>| match ctx.action {
        AuthAction::Read {
            table_name: "memories",
            ..
        } => Authorization::Deny,
        _ => Authorization::Allow,
    }))
    .unwrap();
    assert!(get_many_python_ids(&conn, &[]).unwrap().is_empty());
    for ids in [vec![bad.clone()], vec![cps("absent"), bad]] {
        let ReadError::Sqlite(error) = get_many_python_ids(&conn, &ids).unwrap_err() else {
            panic!("SQLite must prepare the current SELECT before binding its id");
        };
        assert_eq!(
            error.sqlite_error_code(),
            Some(ErrorCode::AuthorizationForStatementDenied)
        );
    }
}

#[test]
fn single_and_batch_reads_share_the_cached_statement_before_id_binding() {
    for warmup in ["none", "get_memory", "get_many"] {
        let conn = common::store_reads::empty();
        match warmup {
            "get_memory" => assert_eq!(
                get_memory(&conn, "missing").unwrap_err().class(),
                "NotFoundError"
            ),
            "get_many" => assert!(get_many(&conn, &["missing".into()]).unwrap().is_empty()),
            _ => (),
        }
        conn.execute("ALTER TABLE memories RENAME TO previous_memories", [])
            .unwrap();
        let invalid = vec![0xd800];
        let error = get_many_python_ids(&conn, std::slice::from_ref(&invalid)).unwrap_err();
        if warmup == "none" {
            assert!(matches!(error, ReadError::Sqlite(_)));
        } else {
            assert_eq!(error.class(), "UnicodeEncodeError");
        }
        let error = get_many_python_ids(&conn, &[cps("ordinary"), invalid]).unwrap_err();
        assert!(matches!(error, ReadError::Sqlite(_)));
    }
}

#[test]
fn failed_binding_preserves_the_connection_error_and_the_conversion_context() {
    use std::error::Error;

    let conn = common::store_reads::empty();
    let invalid = [vec![0xd800]];
    assert!(get_many(&conn, &["missing".into()]).unwrap().is_empty());
    conn.execute_batch(
        "BEGIN; CREATE TEMP TABLE labels (name TEXT UNIQUE); INSERT INTO labels VALUES ('a')",
    )
    .unwrap();
    for (query, class) in [
        ("SELECT x FROM no_such_table", "OperationalError"),
        ("INSERT INTO labels VALUES ('a')", "IntegrityError"),
    ] {
        let original = conn.execute_batch(query).unwrap_err();
        let original_code = original.sqlite_error().unwrap().extended_code;
        for _ in 0..2 {
            let error = get_many_python_ids(&conn, &invalid).unwrap_err();
            assert_eq!(error.class(), class);
            assert_eq!(error.text(), Some(cps(&original.to_string())));
            let ReadError::Bind {
                error: sql,
                context,
            } = &error
            else {
                panic!("expected a database error during parameter binding");
            };
            assert_eq!(sql.sqlite_error().unwrap().extended_code, original_code);
            assert_eq!(context.class(), "UnicodeEncodeError");
            assert_eq!(error.source().unwrap().to_string(), context.to_string());
            assert_eq!(context.text(), Some(cps("'utf-8' codec can't encode character '\\ud800' in position 0: surrogates not allowed")));
            assert!(!conn.is_autocommit());
        }
        conn.execute_batch("SELECT 1").unwrap();
        assert_eq!(
            get_many_python_ids(&conn, &invalid).unwrap_err().class(),
            "UnicodeEncodeError"
        );
    }
    conn.execute_batch("ROLLBACK").unwrap();
}

#[test]
fn a_failed_bind_observes_an_active_cursor_without_consuming_it() {
    let conn = common::store_reads::empty();
    let invalid = [vec![0xd800]];
    assert!(get_many(&conn, &["missing".into()]).unwrap().is_empty());
    let mut query = conn.prepare("SELECT 1 UNION ALL SELECT 2").unwrap();
    let mut cursor = query.query([]).unwrap();
    assert_eq!(cursor.next().unwrap().unwrap().get::<_, i64>(0).unwrap(), 1);
    let err = get_many_python_ids(&conn, &invalid).unwrap_err();
    assert_eq!(err.class(), "DatabaseError");
    assert_eq!(err.text(), Some(cps("another row available")));
    let ReadError::Bind { error, context } = err else {
        panic!("active cursor result")
    };
    assert_eq!(
        error.sqlite_error().unwrap().extended_code,
        rusqlite::ffi::SQLITE_ROW
    );
    assert_eq!(context.class(), "UnicodeEncodeError");
    assert_eq!(cursor.next().unwrap().unwrap().get::<_, i64>(0).unwrap(), 2);
    assert!(cursor.next().unwrap().is_none());
    assert_eq!(
        get_many_python_ids(&conn, &invalid).unwrap_err().class(),
        "UnicodeEncodeError"
    );
}
