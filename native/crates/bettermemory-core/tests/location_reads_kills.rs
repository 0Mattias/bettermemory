//! Cases a plausible wrong `location_reads` fails: no give-up mark, a slot
//! bound missing or per call, healthy reads one thread each, the first
//! error to land rather than the first by job order, a panic lost, a
//! stall on one `what` seen only when every flight of the path stalled, a
//! slot bound off by one, a joined wait of the whole budget, a first
//! hand-off that starts more than one more lane, a landed read counted twice,
//! a lanes step that ignores a budget shorter than the hand-off, a slot wait
//! that spins.

mod common;

use std::collections::HashSet;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use bettermemory_core::location_reads::{
    bounded_read, bounded_reads, Board, Clock, Job, Read, READ_LANES,
};
use common::location_reads::{clock, Script, B};

fn fastest(runs: usize, mut f: impl FnMut() -> Duration) -> Duration {
    (0..runs).map(|_| f()).min().unwrap()
}

/// A clock that never moves: only `_give_up`'s mark stalls a flight.
#[test]
fn a_waiter_that_waited_the_budget_out_stalls_the_path_whatever_the_clock() {
    let frozen: Clock = Arc::new(|| 0.0);
    let board = B::new(frozen);
    let script = Script::default();
    let r = bounded_read(
        &board,
        "/a",
        "x".into(),
        script.make("/a", "x", "h:k"),
        0.05,
    );
    assert_eq!(r, Ok(Read::Unanswered));
    script.ran.lock().unwrap().clear();
    let r = bounded_read(
        &board,
        "/a",
        "y".into(),
        script.make("/a", "y", "v:1"),
        0.05,
    );
    assert_eq!(r, Ok(Read::Unanswered));
    assert!(
        script.ran.lock().unwrap().is_empty(),
        "a stalled path read again"
    );
    // A larger budget than the one waited out does not count it stalled.
    // The read answers at once; the budget only bounds a stalled worker.
    let r = bounded_read(
        &board,
        "/a",
        "y".into(),
        script.make("/a", "y", "v:2"),
        60.0,
    );
    assert_eq!(r, Ok(Read::Value("2".into())));
    script.release_all();
}

#[test]
fn hung_paths_cost_one_budget_per_slots_of_them() {
    let budget = 0.15;
    let took = fastest(3, || {
        let board = B::with_limits(clock(), 0.01, READ_LANES, 4);
        let script = Script::default();
        let jobs: Vec<Job<String, String, String>> = (0..12)
            .map(|i| {
                let p = format!("/h{i}");
                let read = script.make(&p, "x", &format!("h:k{i}"));
                (p, "x".to_owned(), read)
            })
            .collect();
        let t = Instant::now();
        let out = bounded_reads(&board, jobs, budget, false).unwrap();
        let took = t.elapsed();
        assert!(out.iter().all(|r| *r == Read::Unanswered));
        script.release_all();
        took
    });
    let budgets = took.as_secs_f64() / budget;
    // Three waves of four: at least three budgets; one budget each would
    // be twelve, no slot bound one or two.
    assert!(budgets >= 2.9, "{budgets} budgets: the slots are not held");
    assert!(budgets < 7.0, "{budgets} budgets: hung paths cost one each");
}

/// The threads a batch of 200 healthy reads ran on, under a clock the
/// test sets, so no read is handed off unless the test moves the clock.
/// With `slow_first` the first read moves the clock past the hand-off and
/// every read then holds until three threads have read: the first lane and
/// the two lanes the one hand-off starts. Nothing is timed; the shared
/// deadline only ends a run where that never happens.
fn healthy_threads(slow_first: bool) -> usize {
    let (clk, at) = set_clock();
    let board = B::new(clk);
    let seen = Arc::new((Mutex::new(HashSet::new()), Condvar::new()));
    let end = Instant::now() + Duration::from_secs(10);
    let jobs: Vec<Job<String, String, String>> = (0..200)
        .map(|i| {
            let (seen, at) = (Arc::clone(&seen), Arc::clone(&at));
            let read: Box<dyn FnOnce() -> Result<String, String> + Send> = Box::new(move || {
                let (m, c) = &*seen;
                let mut g = m.lock().unwrap();
                g.insert(std::thread::current().id());
                c.notify_all();
                if slow_first {
                    if i == 0 {
                        at.store(1.0f64.to_bits(), Ordering::SeqCst);
                    }
                    while g.len() < 3 {
                        let left = end.saturating_duration_since(Instant::now());
                        if left.is_zero() {
                            break;
                        }
                        g = c.wait_timeout(g, left).unwrap().0;
                    }
                }
                Ok(i.to_string())
            });
            (format!("/v{i}"), "x".to_owned(), read)
        })
        .collect();
    let out = bounded_reads(&board, jobs, 5.0, false).unwrap();
    assert_eq!(out[0], Read::Value("0".into()));
    assert_eq!(out[199], Read::Value("199".into()));
    let n = seen.0.lock().unwrap().len();
    n
}

#[test]
fn healthy_reads_stay_on_one_thread() {
    // The clock stands still: no read is handed off, whatever the load.
    for _ in 0..5 {
        let n = healthy_threads(false);
        assert_eq!(n, 1, "healthy reads spread over {n} threads");
    }
}

#[test]
fn one_hand_off_reads_on_in_two_lanes() {
    // The handed-off lane keeps its thread; the reads go on in two
    // lanes (`min(READ_LANES, 1 + handed)` not counting it): three threads.
    // Each lane is started with a path, so each reads: no more than three.
    for _ in 0..5 {
        let n = healthy_threads(true);
        assert_eq!(n, 3, "one hand-off spread the batch over {n} threads");
    }
}

#[test]
fn a_budget_shorter_than_the_hand_off_answers_in_the_budget() {
    // The lanes step is `min(handoff, budget)`: a hung read under a
    // one-second hand-off still answers after the 0.05 s budget.
    let took = fastest(3, || {
        let board = B::with_limits(clock(), 1.0, READ_LANES, 64);
        let script = Script::default();
        let jobs: Vec<Job<String, String, String>> =
            vec![("/h".into(), "x".into(), script.make("/h", "x", "h:k"))];
        let t = Instant::now();
        let out = bounded_reads(&board, jobs, 0.05, false).unwrap();
        let took = t.elapsed();
        assert_eq!(out, vec![Read::Unanswered]);
        script.release_all();
        took
    });
    assert!(
        took < Duration::from_millis(600),
        "{took:?}: the caller waited the hand-off, not its budget"
    );
}

#[test]
fn a_read_waiting_for_a_slot_sleeps_until_the_oldest_flight_runs_out() {
    // One slot, two hung paths: the second waits for the first flight's
    // budget to run out, asleep, so the clock is read a handful of times.
    let calls = Arc::new(AtomicU64::new(0));
    let c = Arc::clone(&calls);
    let counting: Clock = Arc::new(move || {
        c.fetch_add(1, Ordering::SeqCst);
        bettermemory_core::store::create::system_monotonic()
    });
    let board = B::with_limits(counting, 0.01, READ_LANES, 1);
    let script = Script::default();
    let jobs: Vec<Job<String, String, String>> = (0..2)
        .map(|i| {
            let p = format!("/h{i}");
            let read = script.make(&p, "x", &format!("h:k{i}"));
            (p, "x".to_owned(), read)
        })
        .collect();
    let out = bounded_reads(&board, jobs, 0.2, false).unwrap();
    assert!(out.iter().all(|r| *r == Read::Unanswered));
    script.release_all();
    let n = calls.load(Ordering::SeqCst);
    assert!(
        n < 2000,
        "the clock was read {n} times: the slot wait spins"
    );
}

/// A clock the test sets, in seconds.
fn set_clock() -> (Clock, Arc<AtomicU64>) {
    let at = Arc::new(AtomicU64::new(0f64.to_bits()));
    let a = Arc::clone(&at);
    (
        Arc::new(move || f64::from_bits(a.load(Ordering::SeqCst))),
        at,
    )
}

#[test]
fn a_stall_on_one_what_answers_the_path_at_once_beside_a_live_read() {
    let (frozen, _) = set_clock();
    let board = B::new(frozen);
    let script = Script::default();
    // x waited out at 0.05: stalled for budgets up to 0.05.
    let r = bounded_read(
        &board,
        "/a",
        "x".into(),
        script.make("/a", "x", "h:k"),
        0.05,
    );
    assert_eq!(r, Ok(Read::Unanswered));
    // y in flight, waited on with a larger budget: not stalled. The
    // budget far exceeds the time to the release below.
    let (b2, s2) = (board.clone(), script.clone());
    let live = std::thread::spawn(move || {
        bounded_read(&b2, "/a", "y".into(), s2.make("/a", "y", "h:m"), 60.0)
    });
    while !board.in_flight("/a", &"y".to_owned()) {
        std::thread::sleep(Duration::from_millis(1));
    }
    let r = bounded_read(
        &board,
        "/a",
        "z".into(),
        script.make("/a", "z", "v:z"),
        0.05,
    );
    assert_eq!(r, Ok(Read::Unanswered));
    assert!(
        !script.ran.lock().unwrap().contains(&"v:z".to_owned()),
        "a read started on a path with a stalled flight"
    );
    script.release_all();
    assert_eq!(live.join().unwrap(), Ok(Read::Value("late:m".into())));
}

#[test]
fn a_waiter_joining_a_read_waits_only_what_is_left_of_its_budget() {
    let (clk, at) = set_clock();
    let board = B::new(clk);
    let script = Script::default();
    let (b2, s2) = (board.clone(), script.clone());
    let first = std::thread::spawn(move || {
        bounded_read(&b2, "/a", "x".into(), s2.make("/a", "x", "h:k"), 10.0)
    });
    while !board.in_flight("/a", &"x".to_owned()) {
        std::thread::sleep(Duration::from_millis(1));
    }
    at.store(9.8f64.to_bits(), Ordering::SeqCst);
    let t = Instant::now();
    let r = bounded_read(
        &board,
        "/a",
        "x".into(),
        script.make("/a", "x", "v:no"),
        10.0,
    );
    let took = t.elapsed().as_secs_f64();
    assert_eq!(r, Ok(Read::Unanswered));
    // 0.2 s left of the budget; the whole budget would be 10 s.
    assert!(took < 5.0, "the joined wait took {took} s");
    // The first waiter still holds most of its budget: the release lands.
    script.release_all();
    assert_eq!(first.join().unwrap(), Ok(Read::Value("late:k".into())));
}

#[test]
fn one_hung_path_more_than_the_slots_waits_a_second_budget() {
    let budget = 0.15;
    let board = B::with_limits(clock(), 0.01, READ_LANES, 4);
    let script = Script::default();
    let jobs: Vec<Job<String, String, String>> = (0..5)
        .map(|i| {
            let p = format!("/h{i}");
            let read = script.make(&p, "x", &format!("h:k{i}"));
            (p, "x".to_owned(), read)
        })
        .collect();
    let t = Instant::now();
    let out = bounded_reads(&board, jobs, budget, false).unwrap();
    let budgets = t.elapsed().as_secs_f64() / budget;
    script.release_all();
    assert!(out.iter().all(|r| *r == Read::Unanswered));
    // Four slots: the fifth read starts once the first four stalled.
    assert!(
        budgets >= 1.9,
        "{budgets} budgets: five reads held four slots"
    );
}

#[test]
fn a_read_landing_after_it_stalled_out_of_its_slot_frees_the_slots() {
    // A clock the test sets: /a's read moves it past the budget before it
    // blocks, so /a stalls by the clock; it then stands still, so /b and
    // /c are never handed off and answer whatever the load.
    let budget = 0.05;
    let (clk, at) = set_clock();
    let board = B::with_limits(clk, 0.01, READ_LANES, 1);
    let script = Script::default();
    let one = |p: &str, spec: &str| -> Vec<Job<String, String, String>> {
        vec![(p.to_owned(), "x".to_owned(), script.make(p, "x", spec))]
    };
    let hung = script.make("/a", "x", "h:k");
    let late: Box<dyn FnOnce() -> Result<String, String> + Send> = Box::new(move || {
        at.store(1.0f64.to_bits(), Ordering::SeqCst);
        hung()
    });
    let jobs = vec![("/a".to_owned(), "x".to_owned(), late)];
    let out = bounded_reads(&board, jobs, budget, false).unwrap();
    assert_eq!(out, vec![Read::Unanswered]);
    // /a stalled out of the queue, then lands.
    let out = bounded_reads(&board, one("/b", "v:b"), budget, false).unwrap();
    assert_eq!(out, vec![Read::Value("b".into())]);
    script.release("k");
    script.settle(&board, &["k".to_owned()]);
    let (tx, rx) = mpsc::channel();
    let jobs = one("/c", "v:c");
    let b3 = board.clone();
    std::thread::spawn(move || {
        let _ = tx.send(bounded_reads(&b3, jobs, 5.0, false));
    });
    let out = rx
        .recv_timeout(Duration::from_secs(30))
        .expect("no slot left after a stalled read landed");
    assert_eq!(out, Ok(vec![Read::Value("c".into())]));
}

#[test]
fn the_first_error_by_job_order_is_raised_not_the_first_to_land() {
    let board = B::new(clock());
    let script = Script::default();
    let gate = Script::default();
    // Job 0 errs only once released, after job 1 erred and landed.
    let slow: Box<dyn FnOnce() -> Result<String, String> + Send> = {
        let r = gate.make("/a", "x", "h:g");
        Box::new(move || r().and(Err("zero".into())))
    };
    let (g2, s2, b2) = (gate.clone(), script.clone(), board.clone());
    std::thread::spawn(move || {
        let end = Instant::now() + Duration::from_secs(30);
        let landed = || {
            s2.ran.lock().unwrap().contains(&"e:one".to_owned())
                && !b2.in_flight("/b", &"x".to_owned())
        };
        while !landed() && Instant::now() < end {
            std::thread::sleep(Duration::from_millis(1));
        }
        g2.release("g");
    });
    let jobs = vec![
        ("/a".to_owned(), "x".to_owned(), slow),
        (
            "/b".to_owned(),
            "x".to_owned(),
            script.make("/b", "x", "e:one"),
        ),
    ];
    assert_eq!(bounded_reads(&board, jobs, 60.0, true), Err("zero".into()));
}

#[test]
fn a_waiter_on_a_read_in_flight_gets_its_outcome_and_starts_none() {
    let board = B::new(clock());
    let script = Script::default();
    let b2 = board.clone();
    let s2 = script.clone();
    let first = std::thread::spawn(move || {
        bounded_read(&b2, "/a", "x".into(), s2.make("/a", "x", "h:k"), 60.0)
    });
    while !board.in_flight("/a", &"x".to_owned()) {
        std::thread::sleep(Duration::from_millis(1));
    }
    // The waiter must join before the release: nothing marks that it
    // joined, so the release waits far longer than joining takes, and
    // the budgets far longer than that.
    let s3 = script.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(500));
        s3.release("k");
    });
    let r = bounded_read(
        &board,
        "/a",
        "x".into(),
        script.make("/a", "x", "v:no"),
        60.0,
    );
    assert_eq!(r, Ok(Read::Value("late:k".into())));
    assert_eq!(first.join().unwrap(), Ok(Read::Value("late:k".into())));
    assert_eq!(*script.ran.lock().unwrap(), vec!["h:k".to_owned()]);
}

#[test]
fn a_read_that_panics_counts_as_one_that_raised() {
    let board: Board<String, String, String> = B::new(clock());
    let boom = || -> Job<String, String, String> {
        ("/p".into(), "x".into(), Box::new(|| panic!("read failed")))
    };
    let out = bounded_reads(&board, vec![boom()], 60.0, false).unwrap();
    assert_eq!(out, vec![Read::NotRead]);
    let raised = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        bounded_reads(&board, vec![boom()], 60.0, true)
    }));
    assert!(raised.is_err());
}
