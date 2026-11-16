//! A FIFO or a directory where a key file or the origin file should be:
//! the ring loads and every reader answers, nothing waits. A FIFO is the
//! case that waited (opening it blocks until a writer comes): each case
//! runs in a child process of this test binary, and the pass condition is
//! that the child returned with its answer; one that never returns is
//! killed and fails the test. The reference's own read of a FIFO waits;
//! native reads any name that is not a regular file as unreadable
//! (`store::keys`). POSIX only (FIFOs).

#![cfg(unix)]

mod common;

use std::process::{Command, Stdio};

use bettermemory_core::store::provenance::Provenance;
use bettermemory_core::store::StoreFile;
use common::provenance::{created_store, ring, scratch};

fn store_id() -> String {
    common::created::fixture()["fixed"]["store_id"]
        .as_str()
        .expect("id")
        .to_owned()
}

/// One case in this process: the named ring file replaced by a FIFO or a
/// directory, then the ring loaded and asked.
fn case(suffix: &str, fifo: bool) -> String {
    let dir = scratch(&format!("special-{suffix}"));
    let (file, keys) = created_store(&dir.0);
    let path = keys.join(format!("{}.{suffix}", store_id()));
    std::fs::remove_file(&path).expect("rm");
    if fifo {
        let made = Command::new("mkfifo").arg(&path).status().expect("mkfifo");
        assert!(made.success());
    } else {
        std::fs::create_dir(&path).expect("dir");
    }
    let store = StoreFile::open(&file).expect("open");
    let ring = ring(&keys, &store_id());
    let held = ring.current.is_some();
    let reader = Provenance::new(&store, &ring, held, false).expect("reader");
    let answer = reader.provenance_for(&["01J00000000000000000000001"]);
    format!("held={held} answer={answer:?}")
}

macro_rules! child {
    ($name:ident, $suffix:expr, $fifo:expr) => {
        #[test]
        #[ignore = "run by special_ring_files_never_wait as its child"]
        fn $name() {
            println!("RESULT {}", case($suffix, $fifo));
        }
    };
}

child!(child_key_fifo, "key", true);
child!(child_key_dir, "key", false);
child!(child_origin_fifo, "origin", true);
child!(child_origin_dir, "origin", false);

#[test]
fn special_ring_files_never_wait() {
    let exe = std::env::current_exe().expect("exe");
    let want = [
        ("child_key_fifo", "held=false answer=Ok([])"),
        ("child_key_dir", "held=false answer=Ok([])"),
        ("child_origin_fifo", "held=true answer=Ok([])"),
        ("child_origin_dir", "held=true answer=Ok([])"),
    ];
    for (name, expected) in want {
        let mut child = Command::new(&exe)
            .args([
                "--exact",
                name,
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("child");
        // A child that never returns is killed after a bound far above any
        // run's; the pass condition is its answer, not its time.
        let mut returned = None;
        for _ in 0..1200 {
            if let Some(status) = child.try_wait().expect("wait") {
                returned = Some(status);
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        if returned.is_none() {
            let _ = child.kill();
            let _ = child.wait();
            panic!("{name}: the reader never returned");
        }
        let out = child.wait_with_output().expect("output");
        let text = String::from_utf8_lossy(&out.stdout);
        let line = text
            .lines()
            .find_map(|l| l.split_once("RESULT ").map(|(_, r)| r))
            .unwrap_or_else(|| panic!("{name}: no answer: {text}"));
        assert_eq!(line, expected, "{name}");
    }
}
