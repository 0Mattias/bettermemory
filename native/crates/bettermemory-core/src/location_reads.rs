//! The bounded reads of recorded paths (`location_reads.py`) as a timed
//! runtime: each read runs on a worker thread and is waited on for its
//! budget, a path that does not answer in time reads
//! [`Read::Unanswered`], and while that read is in flight every later
//! read of the path answers at once.
//!
//! The reference's process-wide board (`_FLIGHTS`, `_LANDED`, `_SLOTS`) is
//! a [`Board`] the caller holds and shares; the clock (`time.monotonic`)
//! is a function the caller supplies, read where the reference reads it.
//! The condition waits wait the durations computed from it on real time.
//! `gitfile_rule::bounded` keeps the same reads with the time bound as
//! data.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::hash::Hash;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

pub use crate::gitfile_rule::bounded::Read;

/// `READ_HANDOFF`: how long `bounded_reads` waits on one read before it
/// reads past it on another thread.
pub const READ_HANDOFF: f64 = 0.02;
/// `READ_LANES`: the threads `bounded_reads` reads a batch on at most.
pub const READ_LANES: usize = 8;
/// `READ_SLOTS`: the reads in flight and not stalled beyond which a batch
/// starts no read.
pub const READ_SLOTS: usize = 64;

/// The clock, in seconds (`time.monotonic`).
pub type Clock = Arc<dyn Fn() -> f64 + Send + Sync>;

/// A read: its value or its error; every waiter gets a clone.
pub type ReadFn<T, E> = Box<dyn FnOnce() -> Result<T, E> + Send + 'static>;

/// One job of [`bounded_reads`]: the path, what is read there, the read.
pub type Job<W, T, E> = (String, W, ReadFn<T, E>);

/// A read's outcome; `Err(())` where the read panicked.
type Outcome<T, E> = Result<Result<T, E>, ()>;

fn lock<X>(m: &Mutex<X>) -> MutexGuard<'_, X> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// The real time to wait for `seconds`, never negative; NaN waits none.
fn span(seconds: f64) -> Duration {
    let s = seconds.max(0.0);
    Duration::try_from_secs_f64(s).unwrap_or(Duration::from_secs(1 << 32))
}

struct FlightState<T, E> {
    overdue: f64,
    done: bool,
    outcome: Option<Outcome<T, E>>,
    landed: bool,
    queued: bool,
}

/// `_Flight`: one bounded read in flight. Its state is changed only under
/// the board's lock.
struct Flight<T, E> {
    started: f64,
    state: Mutex<FlightState<T, E>>,
}

type F<T, E> = Arc<Flight<T, E>>;

impl<T, E> Flight<T, E> {
    fn new(started: f64) -> Self {
        Flight {
            started,
            state: Mutex::new(FlightState {
                overdue: 0.0,
                done: false,
                outcome: None,
                landed: false,
                queued: false,
            }),
        }
    }
}

/// `_stalled`: past `budget` at `now`, by the clock or a waiter's give-up.
fn stalled<T, E>(flight: &Flight<T, E>, now: f64, budget: f64) -> bool {
    lock(&flight.state).overdue >= budget || now - flight.started >= budget
}

struct State<W, T, E> {
    flights: HashMap<String, HashMap<W, F<T, E>>>,
    /// `_Slots.queue` and `_Slots.landed`.
    queue: VecDeque<F<T, E>>,
    landed: usize,
}

impl<W, T, E> State<W, T, E> {
    /// `_Slots.taken`.
    fn taken(&mut self, now: f64, budget: f64) -> usize {
        while let Some(front) = self.queue.front() {
            if !(lock(&front.state).landed || stalled(front, now, budget)) {
                break;
            }
            if let Some(gone) = self.queue.pop_front() {
                let mut st = lock(&gone.state);
                st.queued = false;
                if st.landed {
                    self.landed -= 1;
                }
            }
        }
        self.queue.len() - self.landed
    }
}

struct Shared<W, T, E> {
    state: Mutex<State<W, T, E>>,
    landed: Condvar,
}

/// The board of reads in flight, with the clock and the limits it is read
/// by. Clones share the board.
pub struct Board<W, T, E> {
    shared: Arc<Shared<W, T, E>>,
    clock: Clock,
    handoff: f64,
    lanes: usize,
    slots: usize,
}

impl<W, T, E> Clone for Board<W, T, E> {
    fn clone(&self) -> Self {
        Board {
            shared: Arc::clone(&self.shared),
            clock: Arc::clone(&self.clock),
            handoff: self.handoff,
            lanes: self.lanes,
            slots: self.slots,
        }
    }
}

impl<W, T, E> Board<W, T, E>
where
    W: Hash + Eq + Clone + Send + Sync + 'static,
    T: Clone + Send + 'static,
    E: Clone + Send + 'static,
{
    /// An empty board read by `clock`, at the reference's limits.
    pub fn new(clock: Clock) -> Self {
        Self::with_limits(clock, READ_HANDOFF, READ_LANES, READ_SLOTS)
    }

    /// An empty board with other limits (`READ_HANDOFF`, `READ_LANES`,
    /// `READ_SLOTS` patched). `slots` must be at least one.
    pub fn with_limits(clock: Clock, handoff: f64, lanes: usize, slots: usize) -> Self {
        assert!(slots >= 1, "READ_SLOTS below one");
        Board {
            shared: Arc::new(Shared {
                state: Mutex::new(State {
                    flights: HashMap::new(),
                    queue: VecDeque::new(),
                    landed: 0,
                }),
                landed: Condvar::new(),
            }),
            clock,
            handoff,
            lanes,
            slots,
        }
    }

    /// Whether a read of `path` and `what` is on the board.
    pub fn in_flight(&self, path: &str, what: &W) -> bool {
        let st = lock(&self.shared.state);
        st.flights.get(path).is_some_and(|m| m.contains_key(what))
    }

    /// `_board`.
    fn board(
        &self,
        path: &str,
        what: &W,
        mut now: f64,
        budget: f64,
        slot: bool,
    ) -> (Option<F<T, E>>, bool) {
        let mut st = lock(&self.shared.state);
        loop {
            if let Some(flights) = st.flights.get(path) {
                if let Some(f) = flights.get(what) {
                    return (Some(Arc::clone(f)), false);
                }
                if flights.values().any(|f| stalled(f, now, budget)) {
                    return (None, false);
                }
            }
            let taken = st.taken(now, budget);
            if slot && taken >= self.slots {
                let wait = st.queue.front().map_or(0.0, |f| f.started) + budget - now;
                st = self
                    .shared
                    .landed
                    .wait_timeout(st, span(wait))
                    .unwrap_or_else(|e| e.into_inner())
                    .0;
                now = (self.clock)();
                continue;
            }
            let flight = Arc::new(Flight::new(now));
            st.flights
                .entry(path.to_owned())
                .or_default()
                .insert(what.clone(), Arc::clone(&flight));
            lock(&flight.state).queued = true;
            st.queue.push_back(Arc::clone(&flight));
            return (Some(flight), true);
        }
    }

    /// `_give_up`.
    fn give_up(&self, flight: &Flight<T, E>, budget: f64) {
        let _st = lock(&self.shared.state);
        let mut fs = lock(&flight.state);
        fs.overdue = fs.overdue.max(budget);
    }

    /// `_fly`: run `read` and land the flight.
    fn fly(&self, path: &str, what: &W, flight: &F<T, E>, read: ReadFn<T, E>) {
        let outcome = catch_unwind(AssertUnwindSafe(read)).map_err(|_| ());
        let mut st = lock(&self.shared.state);
        {
            let mut fs = lock(&flight.state);
            fs.outcome = Some(outcome);
            fs.landed = true;
            if fs.queued {
                st.landed += 1;
            }
            fs.done = true;
        }
        let mut emptied = false;
        if let Some(mine) = st.flights.get_mut(path) {
            if mine.get(what).is_some_and(|f| Arc::ptr_eq(f, flight)) {
                mine.remove(what);
                emptied = mine.is_empty();
            }
        }
        if emptied {
            st.flights.remove(path);
        }
        self.shared.landed.notify_all();
    }

    /// `flight.done.wait(seconds)`: the outcome, or None at the timeout.
    fn wait_done(&self, flight: &Flight<T, E>, seconds: f64) -> Option<Outcome<T, E>> {
        let end = Instant::now().checked_add(span(seconds));
        let mut st = lock(&self.shared.state);
        loop {
            {
                let fs = lock(&flight.state);
                if fs.done {
                    return fs.outcome.clone();
                }
            }
            let left = match end {
                Some(end) => end.saturating_duration_since(Instant::now()),
                None => span(f64::MAX),
            };
            if left.is_zero() {
                return None;
            }
            st = self
                .shared
                .landed
                .wait_timeout(st, left)
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
    }
}

/// `bounded_read(path, what, read, budget)`: the value of `read` run on a
/// worker thread and waited on for `budget` seconds; [`Read::Unanswered`]
/// when it did not answer in time or `path` has a read in flight past the
/// budget; its error passed on. A read that panicked panics here, as the
/// reference raises what the read raised.
pub fn bounded_read<W, T, E>(
    board: &Board<W, T, E>,
    path: &str,
    what: W,
    read: impl FnOnce() -> Result<T, E> + Send + 'static,
    budget: f64,
) -> Result<Read<T>, E>
where
    W: Hash + Eq + Clone + Send + Sync + 'static,
    T: Clone + Send + 'static,
    E: Clone + Send + 'static,
{
    let now = (board.clock)();
    let (flight, mine) = board.board(path, &what, now, budget, false);
    let Some(flight) = flight else {
        return Ok(Read::Unanswered);
    };
    if mine {
        let (b, p, f) = (board.clone(), path.to_owned(), Arc::clone(&flight));
        let read: ReadFn<T, E> = Box::new(read);
        std::thread::Builder::new()
            .name("bettermemory-probe".into())
            .spawn(move || b.fly(&p, &what, &f, read))
            .expect("can't start new thread");
    }
    match board.wait_done(&flight, flight.started + budget - now) {
        None => {
            board.give_up(&flight, budget);
            Ok(Read::Unanswered)
        }
        Some(Ok(Ok(value))) => Ok(Read::Value(value)),
        Some(Ok(Err(error))) => Err(error),
        Some(Err(())) => panic!("the bounded read panicked"),
    }
}

/// `bounded_reads(jobs, budget, raises=...)`: each job's value;
/// [`Read::Unanswered`] where the read did not answer in time or its path
/// has a read in flight past the budget; [`Read::NotRead`] where it gave
/// an error, or with `raises` the first such error in job order once
/// every job is read. A read that panicked counts as one that raised; with
/// `raises`, when it is the first, this panics.
pub fn bounded_reads<W, T, E>(
    board: &Board<W, T, E>,
    jobs: Vec<Job<W, T, E>>,
    budget: f64,
    raises: bool,
) -> Result<Vec<Read<T>>, E>
where
    W: Hash + Eq + Clone + Send + Sync + 'static,
    T: Clone + Send + 'static,
    E: Clone + Send + 'static,
{
    let mut keys = Vec::with_capacity(jobs.len());
    let mut reads = Vec::with_capacity(jobs.len());
    for (path, what, read) in jobs {
        keys.push((path, what));
        reads.push(Mutex::new(Some(read)));
    }
    let n = keys.len();
    let jobs = Arc::new(Jobs { keys, reads });
    let mut out: Vec<Read<T>> = vec![Read::NotRead; n];
    let mut errors: BTreeMap<usize, Result<E, ()>> = BTreeMap::new();
    let mut todo: Vec<usize> = (0..n).collect();
    while !todo.is_empty() {
        let lanes = Lanes::new(board.clone(), Arc::clone(&jobs), budget, out, errors);
        lanes.read(&todo);
        let (slow, later);
        {
            let mut st = lock(&lanes.state);
            slow = std::mem::take(&mut st.slow);
            later = std::mem::take(&mut st.later);
            out = std::mem::take(&mut st.out);
            errors = std::mem::take(&mut st.errors);
        }
        for (i, flight) in slow {
            let left = flight.started + budget - (board.clock)();
            match board.wait_done(&flight, left) {
                Some(outcome) => land(&mut out, &mut errors, i, outcome),
                None => {
                    board.give_up(&flight, budget);
                    out[i] = Read::Unanswered;
                }
            }
        }
        todo = later;
    }
    match errors.into_iter().next() {
        Some((_, Ok(error))) if raises => Err(error),
        Some((_, Err(()))) if raises => panic!("the bounded read panicked"),
        _ => Ok(out),
    }
}

/// `_Lanes.land`.
fn land<T, E>(
    out: &mut [Read<T>],
    errors: &mut BTreeMap<usize, Result<E, ()>>,
    i: usize,
    outcome: Outcome<T, E>,
) {
    match outcome {
        Ok(Ok(value)) => out[i] = Read::Value(value),
        Ok(Err(error)) => {
            errors.insert(i, Ok(error));
        }
        Err(()) => {
            errors.insert(i, Err(()));
        }
    }
}

struct Jobs<W, T, E> {
    keys: Vec<(String, W)>,
    reads: Vec<Mutex<Option<ReadFn<T, E>>>>,
}

type Held<T, E> = (usize, F<T, E>, Vec<usize>);

struct LanesState<T, E> {
    out: Vec<Read<T>>,
    errors: BTreeMap<usize, Result<E, ()>>,
    slow: Vec<(usize, F<T, E>)>,
    later: Vec<usize>,
    paths: VecDeque<Vec<usize>>,
    reading: BTreeMap<usize, Option<Held<T, E>>>,
}

/// `_Lanes`: one pass of `bounded_reads` over some of its jobs.
struct Lanes<W, T, E> {
    board: Board<W, T, E>,
    jobs: Arc<Jobs<W, T, E>>,
    budget: f64,
    state: Mutex<LanesState<T, E>>,
    cond: Condvar,
}

impl<W, T, E> Lanes<W, T, E>
where
    W: Hash + Eq + Clone + Send + Sync + 'static,
    T: Clone + Send + 'static,
    E: Clone + Send + 'static,
{
    fn new(
        board: Board<W, T, E>,
        jobs: Arc<Jobs<W, T, E>>,
        budget: f64,
        out: Vec<Read<T>>,
        errors: BTreeMap<usize, Result<E, ()>>,
    ) -> Arc<Self> {
        Arc::new(Lanes {
            board,
            jobs,
            budget,
            state: Mutex::new(LanesState {
                out,
                errors,
                slow: Vec::new(),
                later: Vec::new(),
                paths: VecDeque::new(),
                reading: BTreeMap::new(),
            }),
            cond: Condvar::new(),
        })
    }

    /// `_Lanes.read`.
    fn read(self: &Arc<Self>, todo: &[usize]) {
        let mut by_path: Vec<Vec<usize>> = Vec::new();
        let mut at: HashMap<&str, usize> = HashMap::new();
        for &i in todo {
            let path = self.jobs.keys[i].0.as_str();
            let k = *at.entry(path).or_insert_with(|| {
                by_path.push(Vec::new());
                by_path.len() - 1
            });
            by_path[k].push(i);
        }
        let mut st = lock(&self.state);
        st.paths.extend(by_path);
        let step = self.board.handoff.min(self.budget);
        let (mut started, mut handed) = (0usize, 0usize);
        loop {
            let now = (self.board.clock)();
            let mut wait = step;
            let lanes: Vec<usize> = st.reading.keys().copied().collect();
            for lane in lanes {
                let Some(Some((_, flight, _))) = st.reading.get(&lane) else {
                    continue;
                };
                let left = flight.started + step - now;
                if left > 0.0 {
                    wait = wait.min(left);
                    continue;
                }
                let Some(Some((i, flight, rest))) = st.reading.remove(&lane) else {
                    continue;
                };
                handed += 1;
                st.later.extend(rest);
                if step < self.budget {
                    st.slow.push((i, flight));
                } else {
                    self.board.give_up(&flight, self.budget);
                    st.out[i] = Read::Unanswered;
                }
            }
            while !st.paths.is_empty() && st.reading.len() < self.board.lanes.min(1 + handed) {
                started += 1;
                st.reading.insert(started, None);
                let jobs = st.paths.pop_front().unwrap_or_default();
                let me = Arc::clone(self);
                let lane = started;
                std::thread::Builder::new()
                    .name("bettermemory-probes".into())
                    .spawn(move || me.lane(lane, jobs))
                    .expect("can't start new thread");
            }
            if st.reading.is_empty() {
                return;
            }
            st = self
                .cond
                .wait_timeout(st, span(wait))
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
    }

    /// `_Lanes._lane`.
    fn lane(&self, lane: usize, jobs: Vec<usize>) {
        self.lane_body(lane, jobs);
        let mut st = lock(&self.state);
        st.reading.remove(&lane);
        self.cond.notify_one();
    }

    fn lane_body(&self, lane: usize, mut jobs: Vec<usize>) {
        loop {
            for (k, &i) in jobs.iter().enumerate() {
                let (path, what) = &self.jobs.keys[i];
                let now = (self.board.clock)();
                let (flight, mine) = self.board.board(path, what, now, self.budget, true);
                let flight = {
                    let mut st = lock(&self.state);
                    let Some(flight) = flight else {
                        st.out[i] = Read::Unanswered;
                        continue;
                    };
                    if !mine {
                        st.slow.push((i, flight));
                        st.later.extend_from_slice(&jobs[k + 1..]);
                        break;
                    }
                    st.reading
                        .insert(lane, Some((i, Arc::clone(&flight), jobs[k + 1..].to_vec())));
                    flight
                };
                let read = lock(&self.jobs.reads[i]).take().expect("a job read twice");
                self.board.fly(path, what, &flight, read);
                let mut st = lock(&self.state);
                if !st.reading.contains_key(&lane) {
                    return;
                }
                st.reading.insert(lane, None);
                let outcome = lock(&flight.state).outcome.clone().expect("a landed read");
                let LanesState { out, errors, .. } = &mut *st;
                land(out, errors, i, outcome);
            }
            let mut st = lock(&self.state);
            match st.paths.pop_front() {
                Some(next) => jobs = next,
                None => return,
            }
        }
    }
}
