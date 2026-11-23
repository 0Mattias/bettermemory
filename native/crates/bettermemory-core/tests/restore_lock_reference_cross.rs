//! Real CPython restore-lock ownership overlaps an ordinary native reader
//! on Windows. On POSIX the reader's directory lock excludes the restore.
mod common;

use bettermemory_core::{
    pyvalue::PyValue,
    store::restore_lock::{acquire_in_record_mode, read_marker, reading, Refused},
};
use std::{
    io::{BufRead, BufReader, Write},
    path::Path,
    process::Stdio,
    time::Duration,
};

// Acknowledgements bracket each hold. The Python process has a separate
// handle table and uses the shipped restore owner to acquire and mark.
const REFERENCE: &str = r#"
import json
import os
import sys
import threading
from pathlib import Path
from bettermemory import restore_lock as locks
from bettermemory.store import Store

if not __debug__:
    raise RuntimeError("the cross-runtime reference requires active Python assertions")

def ack(expected):
    line = sys.stdin.readline()
    if line.strip() != expected:
        raise AssertionError((line, expected))

store = Path(sys.argv[1])
Store.create(store, keys_dir=store.parent / "keys").close()
hold = locks.acquire(store, wait=0)
assert hold is not None
hold.mark(stage="initial", bundle="fixture")
hold.release(clear=False)
# A broken handshake should finish the hosted job with an explicit error.
deadline = threading.Timer(90, lambda: os._exit(124))
deadline.daemon = True
deadline.start()
print("initial")

ack("reader-held")
hold = locks.acquire(store, wait=0)
if os.name == "nt":
    assert hold is not None
    print("restore-acquired")
else:
    assert hold is None
    print("restore-excluded")
    ack("reader-released")
    hold = locks.acquire(store, wait=0)
    assert hold is not None

assert hold.previous["stage"] == "initial" and hold.previous["bundle"] == "fixture"
hold.mark(stage="python-updated", bundle="reference-restore")
assert locks.read_marker(store)["bundle"] == "reference-restore"
print("python-updated")
ack("release")
hold.release(clear=False)
print("released")

ack("record-held")
marker = locks.read_marker(store)
assert marker["stage"] == "python-updated" and marker["bundle"] == "reference-restore"
if os.name == "nt":
    # Both implementations must reserve the same byte of the record.
    assert locks.acquire(store, wait=0) is None
# The record data remains accessible via a distinct handle during the
# native lease. A whole-file lock would forbid this write on Windows.
marker["stage"] = "separate-data-handle"
with locks.lock_path(store).open("r+b") as stream:
    if os.name == "nt":
        assert not locks._try_lock(stream.fileno(), shared=False)
    stream.seek(0)
    stream.write((json.dumps(marker) + "\n").encode())
    stream.truncate()
print("separate-data-handle")

ack("record-released")
hold = locks.acquire(store, wait=0)
assert hold is not None and hold.previous["stage"] == "separate-data-handle"
hold.mark(stage="finished")
hold.release(clear=True)
assert locks.read_marker(store) is None
deadline.cancel()
print("cleared")
"#;

fn stage(path: &Path, value: &str) {
    let marker = read_marker(path).unwrap().expect("restore marker");
    assert!(marker.contains(&(PyValue::str("stage"), PyValue::str(value))));
}

#[test]
fn a_real_python_restore_and_native_reader_observe_each_others_lock_and_marker() {
    const NAME: &str =
        "a_real_python_restore_and_native_reader_observe_each_others_lock_and_marker";
    let python = match common::created::reference_python() {
        Ok(python) => python,
        Err(reason) => {
            common::parity::record(
                NAME,
                "reference Python",
                "i",
                0,
                &["mixed owners".into()],
                &[reason],
            );
            return;
        }
    };
    let temp = common::stores::scratch("mixed-restore");
    let path = temp.0.join("memory.sqlite");
    let mut child = common::created::reference_command(&python, &temp.0.join("account"))
        .args(["-u", "-c", REFERENCE])
        .arg(&path)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("reference restore process");
    let mut input = child.stdin.take().unwrap();
    let mut output = BufReader::new(child.stdout.take().unwrap());
    let mut send = |command: &str| {
        writeln!(input, "{command}").unwrap();
        input.flush().unwrap();
    };
    let mut expect = |expected: &str| {
        let mut line = String::new();
        output.read_line(&mut line).unwrap();
        assert_eq!(line.trim_end(), expected);
    };

    // If a native assertion fails the child must lose its waiting stdin
    // and be reaped, so the disposable store can be removed on Windows.
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        expect("initial");
        let mut reader = Some(
            reading(&path, Duration::ZERO)
                .unwrap()
                .expect("native reader"),
        );
        stage(&path, "initial");
        send("reader-held");
        if cfg!(windows) {
            expect("restore-acquired");
        } else {
            expect("restore-excluded");
            drop(reader.take());
            send("reader-released");
        }
        expect("python-updated");
        stage(&path, "python-updated");
        assert!(matches!(
            reading(&path, Duration::ZERO),
            Err(Refused::Running)
        ));
        send("release");
        expect("released");
        stage(&path, "python-updated");
        drop(reader);

        let record = acquire_in_record_mode(&path, Duration::ZERO)
            .unwrap()
            .expect("native record lease");
        send("record-held");
        expect("separate-data-handle");
        stage(&path, "separate-data-handle");
        record.release(false);
        send("record-released");
        expect("cleared");
        assert!(read_marker(&path).unwrap().is_none());
    }));
    if let Err(failure) = result {
        let _ = child.kill();
        let _ = child.wait();
        std::panic::resume_unwind(failure);
    }
    assert!(child.wait().unwrap().success());
    common::parity::record(NAME, "reference Python", "i", 1, &[], &[]);
}
