//! `StdFs::holds_other_than` on a real folder (unix): it reads a crowded
//! folder up to its first name other than the one given and no further,
//! the half of the crowded-owner fix that production uses (`MemFs`'s half
//! is counted in `gitfile_discovery_robustness.rs`). No clock: each name
//! the standard library hands back is a new allocation, so the
//! allocations of this thread are counted, and a call on a folder of
//! 4,000 names makes no more of them than a call on a folder of two. A
//! listing of the folder whole makes one or more per name.

#![cfg(unix)]

use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;

use bettermemory_core::fsdata::{Fs, StdFs};

/// The system allocator, counting the allocations of the thread that
/// asks while its count is on.
struct Counting;

thread_local! {
    static ON: Cell<bool> = const { Cell::new(false) };
    static MADE: Cell<usize> = const { Cell::new(0) };
}

// SAFETY: every call is passed to `System` unchanged; the count is a
// thread-local `Cell` with a const initialiser and no destructor, so it
// never allocates.
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if ON.with(Cell::get) {
            MADE.with(|made| made.set(made.get() + 1));
        }
        // SAFETY: the caller's contract, passed on.
        unsafe { System.alloc(layout) }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        // SAFETY: the caller's contract, passed on.
        unsafe { System.dealloc(ptr, layout) }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        if ON.with(Cell::get) {
            MADE.with(|made| made.set(made.get() + 1));
        }
        // SAFETY: the caller's contract, passed on.
        unsafe { System.realloc(ptr, layout, size) }
    }
}

#[global_allocator]
static COUNTING: Counting = Counting;

/// The allocations `work` makes on this thread.
fn made<T>(work: impl FnOnce() -> T) -> (T, usize) {
    MADE.with(|made| made.set(0));
    ON.with(|on| on.set(true));
    let out = work();
    ON.with(|on| on.set(false));
    (out, MADE.with(Cell::get))
}

/// A new folder holding `.git` and `n` other names.
fn crowded(n: usize) -> std::path::PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "bm-crowded-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    std::fs::create_dir(dir.join(".git")).expect("its .git");
    for i in 0..n {
        std::fs::write(dir.join(format!("f{i:06}")), b"").expect("a name");
    }
    dir
}

#[test]
fn a_crowded_folder_on_disk_is_read_to_its_first_other_name() {
    use std::os::unix::ffi::OsStrExt;
    let fs = StdFs;
    let few = crowded(2);
    let many = crowded(4_000);
    let ask = |dir: &std::path::Path| {
        let path = dir.as_os_str().as_bytes().to_vec();
        made(|| fs.holds_other_than(&path, b".git"))
    };
    // Once each first, so nothing the first call alone makes is counted.
    let _ = ask(&few);
    let _ = ask(&many);
    let (on_few, few_made) = ask(&few);
    let (on_many, many_made) = ask(&many);
    assert_eq!(on_few, Ok(true));
    assert_eq!(on_many, Ok(true));
    // `.git` may come first, so the early stop reads at most two names on
    // either folder; a listing whole reads 4,001 on the larger.
    assert!(
        many_made <= few_made + 4,
        "{many_made} allocations on 4,000 names against {few_made} on two"
    );
    // A folder of `.git` alone holds no other name.
    let alone = crowded(0);
    let path = alone.as_os_str().as_bytes().to_vec();
    assert_eq!(fs.holds_other_than(&path, b".git"), Ok(false));
    for dir in [few, many, alone] {
        std::fs::remove_dir_all(dir).expect("removed");
    }
}
