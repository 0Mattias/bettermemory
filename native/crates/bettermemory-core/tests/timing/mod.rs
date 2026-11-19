//! Time claims that hold on a busy machine.
//!
//! A robustness test that says "ten times the input takes at most thirty
//! times as long" times two sizes. Timed one after the other, a machine
//! that gets busy between the two fails the claim though the code is
//! linear. Here the two sizes are timed in turn and the least time of each
//! is kept: other work on the machine can only add to a time, so the least
//! of several runs is the nearest to the code's own cost. The claim is
//! read after `LEAST` rounds and again after each further round up to
//! `MOST`. Code that grows faster than the bound misses it in every
//! round; a busy machine does not. A claim missed by more than `HOPELESS`
//! times its bound fails without further rounds, so code that has turned
//! quadratic fails in three rounds and not twelve.
//!
//! The clock is the calling thread's own: [`once`] reads the processor
//! time the thread has used (`bettermemory_sys::thread_cpu_time`, on
//! Linux, macOS and the other unix systems) and the wall clock only where
//! there is no such clock (Windows). On a loaded machine the wall clock
//! counts the other processes too, and not in proportion to the work: a
//! small size of 10 ms runs in one turn on a processor, while a large one
//! of 400 ms is shared with every other process the whole way. Forty times
//! the work measured 88 to 236 times as long by the wall clock with load
//! averages of 30 to 40 on 12 cores, more than the least of twelve rounds
//! takes away. The thread's clock counts only the time the thread ran,
//! user and kernel mode alike. What it cannot see is time the thread
//! waits: on a sleep, on another thread, on a child process, on a device.
//! A claim whose timed part waits on any of those takes [`wall`] instead,
//! or the cost it is about would go uncounted and the claim would hold
//! whatever the code does: `gitsystem_probe_robustness` (files made,
//! listed and removed on disk) and `golden_search_store` (an SQLite file
//! read from disk). [`ends_within`] bounds the time a caller waits, which
//! is wall-clock time, and its runs take [`wall`] too.
//!
//! Claims in one test file take turns: the tests of a file run on threads
//! of one process, and ten claims timed at once make each other's large
//! sizes slower (a claim missed by 219 times with the ten claims of
//! `claims_robustness` at once on a loaded machine, met alone on the same
//! machine; that was by the wall clock, and the thread's clock still
//! counts the time lost to caches the other claims fill). Each claim
//! holds [`TURN`] while it is timed.
//!
//! A test file takes this with `mod timing;`.

#![allow(dead_code)]

use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

/// Held by a claim while it is timed, so the claims of one test process
/// are timed one at a time.
static TURN: Mutex<()> = Mutex::new(());

/// The turn, also after a claim that panicked while holding it (a claim
/// that fails panics, and the next claim still runs).
fn turn() -> MutexGuard<'static, ()> {
    TURN.lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// Rounds timed before the claim is first read.
pub const LEAST: usize = 3;
/// Rounds after which the claim fails.
pub const MOST: usize = 12;
/// Added to a growth bound, so a small size that takes microseconds does
/// not make the claim a test of the timer.
pub const SLACK: Duration = Duration::from_millis(50);
/// The factor for ten times the input. Linear code costs ten times and
/// quadratic code a hundred. Thirty was missed by linear code on a busy
/// machine: the larger input no longer fits the processor's cache and
/// each element then costs more (measured up to 4.8 times more, 48 times
/// in all, with thirty other processes running). Sixty leaves that room
/// and still fails quadratic code.
pub const TENFOLD: u32 = 60;
/// Past this many times its bound, on the least times of [`LEAST`]
/// rounds or more, a claim fails at once.
pub const HOPELESS: u32 = 3;

/// The processor time one call of `f` takes on the calling thread, or its
/// wall-clock time where the thread has no clock of its own (Windows).
/// Work `f` hands to another thread or process, and time it waits, are
/// not counted: such a call is timed by [`wall`]. What `f` returns is
/// dropped after the clock is read.
pub fn once<T>(f: impl FnOnce() -> T) -> Duration {
    let started = Instant::now();
    let thread_started = bettermemory_sys::thread_cpu_time();
    let out = f();
    let took = match (thread_started, bettermemory_sys::thread_cpu_time()) {
        (Some(from), Some(to)) => to.saturating_sub(from),
        _ => started.elapsed(),
    };
    drop(out);
    took
}

/// The wall-clock time one call of `f` takes, waits included: for a call
/// that waits on another thread, a child process, a sleep or a device, and
/// for [`ends_within`]. What `f` returns is dropped after the clock is
/// read.
pub fn wall<T>(f: impl FnOnce() -> T) -> Duration {
    let started = Instant::now();
    let out = f();
    let took = started.elapsed();
    drop(out);
    took
}

/// Whether `large` is within `factor` times `small` plus [`SLACK`].
pub fn within(small: Duration, large: Duration, factor: u32) -> bool {
    large < small.saturating_mul(factor).saturating_add(SLACK)
}

/// Panics unless the large size takes less than `factor` times the small
/// one plus [`SLACK`], on the least times seen. `small` and `large` each
/// run their size once and return the time of the part under test (what
/// they set up first is not timed), by [`once`], or by [`wall`] where that
/// part waits.
pub fn grows_within(
    name: &str,
    factor: u32,
    small: &mut dyn FnMut() -> Duration,
    large: &mut dyn FnMut() -> Duration,
) {
    let held = turn();
    let (mut s, mut l) = (Duration::MAX, Duration::MAX);
    let mut rounds = 0;
    while rounds < MOST {
        s = s.min(small());
        l = l.min(large());
        rounds += 1;
        if rounds < LEAST {
            continue;
        }
        if within(s, l, factor) {
            drop(held);
            return;
        }
        if !within(s, l, factor.saturating_mul(HOPELESS)) {
            break;
        }
    }
    drop(held);
    panic!(
        "{name}: the large size took {l:?} and the small one {s:?}, the least \
         of {rounds} rounds: more than {factor} times plus {SLACK:?}"
    );
}

/// Panics unless one of up to [`MOST`] runs ends within `bound`. `run`
/// returns the wall-clock time of one run ([`wall`]): the bound is on the
/// time a caller waits.
pub fn ends_within(name: &str, bound: Duration, run: &mut dyn FnMut() -> Duration) {
    let held = turn();
    let mut least = Duration::MAX;
    for _ in 0..MOST {
        least = least.min(run());
        if least < bound {
            drop(held);
            return;
        }
    }
    drop(held);
    panic!("{name}: the least of {MOST} runs took {least:?}, the bound is {bound:?}");
}
