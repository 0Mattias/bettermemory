//! Hostile states for the writing open, and two openers at once.
//!
//! Every hostile state ends in a typed error or an open, never a panic,
//! and a refusal writes nothing to the store or the key ring (compared
//! byte for byte). Two openers: the reference lets two writing opens of
//! one store proceed side by side, each write transaction waiting for
//! SQLite's write lock (`BEGIN IMMEDIATE` under a 5 s busy timeout), with
//! the binding as the one claim (written under its own lock); so does the
//! native open, which these tests hold it to: two native opens at once,
//! a native open while a reference writer holds the store open and
//! writes, and a reference open while a native connection holds the
//! write lock.

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use bettermemory_core::store::open::{open_for_writing, DigestOutcome, OpenError};
use common::created::{reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::stores::scratch;
use serde_json::{json, Value};

/// Every file under `dir` with its bytes; sidecars SQLite makes and
/// removes (`-wal`, `-shm`) left out.
fn snapshot(dir: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    let mut out = BTreeMap::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        let Ok(entries) = fs::read_dir(&d) else {
            continue;
        };
        for entry in entries.flatten() {
            let p = entry.path();
            let name = p.to_string_lossy().into_owned();
            if name.ends_with("-wal") || name.ends_with("-shm") {
                continue;
            }
            if p.is_dir() {
                stack.push(p);
            } else if let Ok(bytes) = fs::read(&p) {
                out.insert(p, bytes);
            }
        }
    }
    out
}

fn set_up(name: &str, base: &str, recipe: &[Value]) -> (common::stores::Scratch, PathBuf) {
    let dir = scratch(name);
    let c = dir.0.join("c");
    opened::made(base, &c);
    opened::setup(recipe, &c, &common::stores::store_id());
    (dir, c)
}

fn open(c: &Path) -> Result<DigestOutcome, OpenError> {
    let store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )?;
    let digest = store.digest().clone();
    store.close().expect("close");
    Ok(digest)
}

/// The open refuses with `class`, and nothing under the case changed.
fn refuses_untouched(c: &Path, class: &str) {
    let before = snapshot(c);
    let got = open(c);
    match got {
        Err(e) => assert_eq!(e.class(), class, "{e}"),
        Ok(d) => panic!("opened ({d:?}), expected {class}"),
    }
    assert!(snapshot(c) == before, "a refusal wrote");
}

#[test]
fn a_file_that_is_not_a_database_is_refused_untouched() {
    let (_d, c) = set_up("open-garbage", "intact", &[]);
    fs::write(c.join("memory.sqlite"), vec![b'x'; 8192]).expect("garbage");
    // The reference: `admit_store` refuses it (StoreRefusedError, "file is
    // not a database").
    refuses_untouched(&c, "StoreRefusedError");
}

#[test]
fn a_truncated_store_never_panics() {
    for at in [100u64, 4096, 8192, 20_000] {
        let (_d, c) = set_up("open-truncated", "intact", &[]);
        let file = fs::OpenOptions::new()
            .write(true)
            .open(c.join("memory.sqlite"))
            .expect("open");
        file.set_len(at).expect("truncate");
        drop(file);
        let before = snapshot(&c);
        if let Err(e) = open(&c) {
            eprintln!("truncated at {at}: {} {e}", e.class());
            assert!(
                snapshot(&c) == before,
                "a refusal wrote (truncated at {at})"
            );
        }
    }
}

#[test]
fn corrupted_bytes_never_panic() {
    let mut seed: u64 = 0x1234_5678_9abc_def1;
    for _ in 0..12 {
        let (_d, c) = set_up("open-corrupt", "intact", &[]);
        let path = c.join("memory.sqlite");
        let mut bytes = fs::read(&path).expect("read");
        for _ in 0..8 {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            let at = (seed as usize) % bytes.len();
            bytes[at] ^= (seed >> 32) as u8 | 1;
        }
        fs::write(&path, &bytes).expect("write");
        let _ = open(&c);
    }
}

#[test]
fn a_directory_at_the_store_name_is_no_store() {
    let dir = scratch("open-dir");
    let c = dir.0.join("c");
    fs::create_dir_all(c.join("memory.sqlite")).expect("dir");
    fs::create_dir_all(c.join("keys")).expect("keys");
    let got = open(&c).expect_err("refused");
    assert_eq!(got.class(), "FileNotFoundError");
}

/// POSIX: a path through a file is ENOTDIR, which `load_current` raises
/// as an unreadable key. Windows answers "path not found", a missing key.
#[cfg(unix)]
#[test]
fn a_keys_directory_that_is_a_file_is_an_unreadable_key() {
    let (_d, c) = set_up("open-keys-file", "intact", &[]);
    fs::remove_dir_all(c.join("keys")).expect("rm");
    fs::write(c.join("keys"), b"not a directory").expect("file");
    refuses_untouched(&c, "KeyUnreadableError");
}

#[test]
fn junk_in_the_ring_files_is_read_as_the_reference_reads_it() {
    let id = common::stores::store_id();
    // A binding with junk, or not UTF-8: no binding; the open binds.
    for junk in [
        &b"{\"path\": 5}"[..],
        b"\xff\xfe",
        b"[]",
        b"{\"path\": \"\"}",
    ] {
        let (_d, c) = set_up("open-junk-binding", "intact", &[]);
        fs::write(c.join("keys").join(format!("{id}.path")), junk).expect("junk");
        assert_eq!(open(&c).expect("opens"), DigestOutcome::Ingested);
        let text = fs::read_to_string(c.join("keys").join(format!("{id}.path"))).expect("bound");
        assert!(
            text.starts_with("{\"path\": \"/") || cfg!(windows),
            "{text}"
        );
    }
    // A head with junk and no binding: no head; the file fits and binds.
    let (_d, c) = set_up(
        "open-junk-head",
        "intact",
        &[json!({"write": ".head", "hex": "7b7b7b"})],
    );
    open(&c).expect("opens");
    // An origin with junk: an anchor that does not read is settled as the
    // reference settles it (`_settle_origin`): written over, on seq 1,
    // naming the key that verifies it.
    let (_d, c) = set_up(
        "open-junk-origin",
        "intact",
        &[
            json!({"write": ".origin", "hex": "6e6f"}),
            json!({"bind": "memory.sqlite"}),
        ],
    );
    open(&c).expect("opens");
    let anchor = fs::read_to_string(c.join("keys").join(format!("{id}.origin"))).expect("anchor");
    assert!(
        anchor.starts_with("{\"seq\": 1, ") && anchor.contains("\"key_fingerprint\""),
        "{anchor}"
    );
}

#[cfg(unix)]
mod unix {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn chmod(p: &Path, mode: u32) {
        fs::set_permissions(p, fs::Permissions::from_mode(mode)).expect("chmod");
    }

    fn root_can_read_anything() -> bool {
        // Mode bits do not stop the superuser.
        std::env::var("USER").as_deref() == Ok("root")
    }

    #[test]
    fn an_unreadable_key_is_refused_untouched() {
        if root_can_read_anything() {
            return;
        }
        let (_d, c) = set_up(
            "open-key-0000",
            "intact",
            &[json!({"bind": "memory.sqlite"})],
        );
        let key = c
            .join("keys")
            .join(format!("{}.key", common::stores::store_id()));
        chmod(&key, 0o000);
        let before = snapshot(&c);
        let got = open(&c);
        let after = snapshot(&c);
        chmod(&key, 0o600);
        assert_eq!(got.expect_err("refused").class(), "KeyUnreadableError");
        assert!(after == before, "a refusal wrote");
    }

    #[test]
    fn an_unwritable_keys_directory_opens_without_binding() {
        if root_can_read_anything() {
            return;
        }
        let (_d, c) = set_up("open-keys-0500", "intact", &[]);
        chmod(&c.join("keys"), 0o500);
        let got = open_for_writing(
            &c.join("memory.sqlite"),
            &c.join("keys"),
            &opened::engine_provider(),
        );
        chmod(&c.join("keys"), 0o700);
        let store = got.expect("the reference logs the binding it cannot write and opens");
        assert!(!store.bound());
        assert_eq!(store.digest(), &DigestOutcome::Ingested);
        store.close().expect("close");
        assert!(!c
            .join("keys")
            .join(format!("{}.path", common::stores::store_id()))
            .exists());
    }

    #[test]
    fn a_read_only_store_file_never_panics_and_keeps_its_bytes() {
        if root_can_read_anything() {
            return;
        }
        let (_d, c) = set_up(
            "open-store-0400",
            "intact",
            &[json!({"bind": "memory.sqlite"})],
        );
        let file = c.join("memory.sqlite");
        let before = fs::read(&file).expect("read");
        chmod(&file, 0o400);
        let got = open(&c);
        chmod(&file, 0o600);
        eprintln!("read-only store file: {got:?}");
        assert_eq!(fs::read(&file).expect("read"), before);
    }
}

#[test]
fn two_native_opens_at_once_both_open_and_agree() {
    let (_d, c) = set_up(
        "open-two",
        "fts",
        &[
            json!({"sql": "DROP TABLE digest_records; DROP TABLE digest_tokens; DROP TABLE digest_state;"}),
        ],
    );
    let outcomes: Vec<Result<DigestOutcome, String>> = std::thread::scope(|s| {
        let a = s.spawn(|| open(&c).map_err(|e| format!("{} {e}", e.class())));
        let b = s.spawn(|| open(&c).map_err(|e| format!("{} {e}", e.class())));
        vec![a.join().expect("a"), b.join().expect("b")]
    });
    for o in &outcomes {
        assert!(o.is_ok(), "{outcomes:?}");
    }
    eprintln!("two native opens: {outcomes:?}");
    // The digest is whole: a third open finds it current.
    assert_eq!(open(&c).expect("third"), DigestOutcome::Current);
    // And it is the reference's (the fixture is POSIX only).
    #[cfg(unix)]
    {
        let (header, cases) = opened::cases();
        let want = cases
            .iter()
            .find(|c| c["case"] == "digest_missing")
            .expect("case");
        let got = opened::state(
            &c,
            c.parent().expect("root"),
            header["store_id"].as_str().expect("id"),
            header["pragmas"].as_array().expect("pragmas"),
        );
        for part in ["digest_records", "digest_tokens", "digest_state", "tables"] {
            assert_eq!(got[part], want["after"][part], "{part}");
        }
    }
}

const WRITER: &str = r#"
import sys, time
from datetime import datetime, timezone
from pathlib import Path
from bettermemory.models import Confidence, Memory, Source
from bettermemory.store import Store

store_file, keys, flag, mode = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
if mode == "writer":
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    flag.write_text("open")
    now = datetime.now(timezone.utc)
    for n in range(20):
        store.put_memory(Memory(id=f"01K{n:023d}", created=now, updated=now, scopes=["two"],
                                confidence=Confidence.HIGH, source=Source.EXPLICIT,
                                body=f"Written while another opens, number {n}."))
        time.sleep(0.02)
    print(store.log_verify()["status"])
    store.close()
else:
    flag.write_text("opening")
    started = time.monotonic()
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    waited = time.monotonic() - started
    print(f"{store.log_verify()['status']} {waited:.2f}")
    store.close()
"#;

#[test]
fn a_native_open_beside_a_reference_writer() {
    const TEST: &str = "a_native_open_beside_a_reference_writer";
    let python = match reference_python() {
        Ok(p) => p,
        Err(why) => {
            eprintln!("NOT RUN {TEST}: {why}");
            record(TEST, "reference Python", "i", 0, &[TEST.to_owned()], &[why]);
            return;
        }
    };
    let (d, c) = set_up("open-beside", "intact", &[json!({"bind": "memory.sqlite"})]);
    let flag = d.0.join("flag");
    let home = d.0.join("home");
    std::thread::scope(|s| {
        let writer = s.spawn(|| {
            run_reference(
                &python,
                &home,
                WRITER,
                &[
                    &c.join("memory.sqlite"),
                    &c.join("keys"),
                    &flag,
                    Path::new("writer"),
                ],
            )
        });
        let deadline = Instant::now() + Duration::from_secs(20);
        while !flag.exists() {
            assert!(
                Instant::now() < deadline,
                "the reference writer never opened"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        let got = open(&c).expect("the native open proceeds beside a writer");
        eprintln!("native open beside the reference writer: {got:?}");
        let out = writer.join().expect("writer");
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "ok");
    });
    // The digest a later open leaves equals a fresh build's: both agree.
    assert!(matches!(
        open(&c).expect("again"),
        DigestOutcome::Ingested | DigestOutcome::Current
    ));
    record(TEST, "reference Python", "i", 1, &[], &[]);
}

#[test]
fn a_reference_open_waits_for_a_native_write_lock() {
    const TEST: &str = "a_reference_open_waits_for_a_native_write_lock";
    let python = match reference_python() {
        Ok(p) => p,
        Err(why) => {
            eprintln!("NOT RUN {TEST}: {why}");
            record(TEST, "reference Python", "i", 0, &[TEST.to_owned()], &[why]);
            return;
        }
    };
    // A digest behind, so the reference's open needs the write lock.
    let (d, c) = set_up("open-lock", "intact", &[json!({"bind": "memory.sqlite"})]);
    // A native open; then its digest set behind (so the reference's open
    // needs the write lock to bring it forward), then the write lock held.
    let held = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("native open");
    held.connection()
        .execute_batch(
            "UPDATE digest_state SET value = '0' WHERE key = 'seq'; \
             UPDATE digest_state SET value = '' WHERE key = 'mac';",
        )
        .expect("behind");
    held.connection()
        .execute_batch("BEGIN IMMEDIATE")
        .expect("lock");
    let home = d.0.join("home");
    let flag = d.0.join("flag");
    std::thread::scope(|s| {
        let reader = s.spawn(|| {
            run_reference(
                &python,
                &home,
                WRITER,
                &[
                    &c.join("memory.sqlite"),
                    &c.join("keys"),
                    &flag,
                    Path::new("open"),
                ],
            )
        });
        // The lock is held for a second and a half from the moment the
        // reference says it is about to open, not from its spawn: how long
        // the interpreter takes to start is then no part of the wait
        // asserted below (on a slow runner it was 0.55 s of the 1.5).
        let asked = std::time::Instant::now();
        while !flag.exists() && asked.elapsed() < Duration::from_secs(120) {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(flag.exists(), "the reference never reached its open");
        std::thread::sleep(Duration::from_millis(1500));
        held.connection().execute_batch("COMMIT").expect("unlock");
        let out = reader.join().expect("reader");
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        let said = String::from_utf8_lossy(&out.stdout).trim().to_owned();
        eprintln!("the reference's open beside a native write lock: {said}");
        let waited: f64 = said
            .strip_prefix("ok ")
            .and_then(|w| w.parse().ok())
            .unwrap_or_else(|| panic!("{said}"));
        assert!(
            waited >= 1.0,
            "the reference did not wait for the lock: {said}"
        );
    });
    held.close().expect("close");
    record(TEST, "reference Python", "i", 1, &[], &[]);
}

/// A lost binding race: between the open's decision (the ring bound to
/// no file, this file fits it) and its binding's write, a second opener
/// binds the ring to a copy that holds the store. The binding's locked
/// re-read finds it, and the open refuses as the reference's turns into
/// a reader's (`bind_store`'s ``elsewhere``): before the digest is
/// touched (it needs a build here), with the second opener's binding
/// kept and no store handed out.
#[test]
fn a_binding_won_meanwhile_by_a_copy_refuses_before_the_digest() {
    use bettermemory_core::store::open::{open_for_writing_with, Boundary, NotPorted};
    let id = common::stores::store_id();
    let (dir, c) = set_up(
        "race",
        "intact",
        &[
            json!({"sql": "DELETE FROM digest_state WHERE key IN ('seq', 'mac');"}),
            json!({"copy_store": "other/memory.sqlite"}),
        ],
    );
    let binding = c.join("keys").join(format!("{id}.path"));
    assert!(!binding.exists(), "the case starts unbound");
    let other = fs::canonicalize(c.join("other/memory.sqlite")).expect("other");
    let won = format!(
        "{{\"path\": {}}}",
        serde_json::to_string(other.to_str().expect("UTF-8")).expect("json")
    );
    let pragmas: Vec<Value> = Vec::new();
    let digest_of = |c: &Path| {
        let s = opened::state(c, &dir.0, &id, &pragmas);
        (
            s["digest_state"].clone(),
            s["digest_records"].clone(),
            s["digest_tokens"].clone(),
        )
    };
    let before = digest_of(&c);
    let mut hook = |b: Boundary| {
        if b == Boundary::EngineRecorded {
            fs::write(&binding, &won).expect("the second opener binds");
        }
    };
    let got = open_for_writing_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
        &mut hook,
    );
    match got {
        Err(OpenError::NotPorted(n @ NotPorted::BoundElsewhere { .. })) => {
            let text = OpenError::NotPorted(n).to_string();
            assert!(text.contains("bettermemory key adopt --yes"), "{text}");
            assert!(text.contains(other.to_str().expect("UTF-8")), "{text}");
        }
        Err(e) => panic!("{} {e}", e.class()),
        Ok(_) => panic!("opened past a binding won by a copy"),
    }
    assert_eq!(fs::read_to_string(&binding).expect("binding"), won);
    assert_eq!(digest_of(&c), before, "the digest was touched");
}
