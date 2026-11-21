//! Cases a plausible wrong `location_answer` fails: the evicted capture's
//! history read anyway, a walk answered once not read again, a re-mint
//! answered once not asked again, a raising view failing the round, a
//! slow view `unreachable`, the ancestry with no history kept to full
//! hashes, and an earlier answer's key keeping its place.

#[allow(dead_code)]
mod witness;

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::{Duration, Instant};

use bettermemory_core::capture::{Capture, State};
use bettermemory_core::location_answer as la;
use bettermemory_core::location_git::Reach;
use bettermemory_core::location_reads::{Board, Clock};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use witness::{checkout, cps, dumps, load, view};

#[derive(Default)]
struct Counted {
    parents: usize,
}

impl la::AnswerHistory for Counted {
    fn holds_objects(&mut self, ids: &[PyStr]) -> BTreeSet<PyStr> {
        ids.iter().cloned().collect()
    }
    fn fingerprint(&mut self, _: &[u32]) -> Option<PyStr> {
        Some(cps("fp"))
    }
    fn parents(&mut self, _: &[u32]) -> Vec<PyStr> {
        self.parents += 1;
        Vec::new()
    }
    fn reachable_among(&mut self, _: &[PyStr]) -> Reach {
        Reach {
            relations: Vec::new(),
            bounded: false,
            handled: 0,
        }
    }
    fn blobs(&mut self, _: &[u32]) -> Vec<PyStr> {
        Vec::new()
    }
    fn independent(&mut self, _: &[PyStr]) -> Option<Vec<String>> {
        None
    }
}

fn board() -> Board<String, PyStr, String> {
    let start = Instant::now();
    let clock: Clock = Arc::new(move || start.elapsed().as_secs_f64());
    Board::new(clock)
}

fn repo() -> Capture {
    let mut cap = Capture::new(cps("/r"), State::Repo);
    cap.common_dir = Some(cps("/r/.git"));
    cap.git_dir = Some(cps("/r/.git"));
    cap.head = Some(cps("c1"));
    cap
}

fn reader(answer: &'static str) -> la::ViewRead<String> {
    Arc::new(move |path: &[u32], _: &_| match answer {
        "slow" => {
            std::thread::sleep(Duration::from_millis(1500));
            Ok(cps("live"))
        }
        "raise-b" if path == cps("/b").as_slice() => Err("OSError".into()),
        "raise-all" => Err(format!("E{}", path.len())),
        _ => Ok(cps(answer)),
    })
}

struct Run {
    out: Result<PyValue, la::AnswerError<String>>,
    built: usize,
    reminted: usize,
}

fn run(q: &str, a: &str, cap: &Capture, read: &str) -> Run {
    let (mut built, mut reminted) = (0, 0);
    let out = la::answer(
        &load(&q.into()),
        &load(&a.into()),
        cap,
        &mut |_: &[u32]| {
            built += 1;
            Counted::default()
        },
        &mut |_: &[u32], _, _, _| {
            reminted += 1;
            (Some("T".into()), "written".into())
        },
        &board(),
        &reader(Box::leak(read.to_owned().into_boxed_str())),
    );
    Run {
        out,
        built,
        reminted,
    }
}

fn text(r: &Run) -> String {
    dumps(r.out.as_ref().unwrap())
}

#[test]
fn an_evicted_capture_is_still_read_by_answer_but_not_by_history_of() {
    let mut cap = repo();
    cap.evicted = true;
    let r = run("{}", "{}", &cap, "live");
    assert_eq!(r.built, 1);
    assert!(text(&r).contains("\"walk\": {\"c1\": [\"fp\", []]}"));
    assert!(la::history_of(&cap, &mut |_: &[u32]| ()).is_none());
}

#[test]
fn a_walk_answered_is_read_again_only_when_asked() {
    let r = run("{}", r#"{"walk": {"x": 1}}"#, &repo(), "live");
    assert!(text(&r).contains("\"walk\": {\"x\": 1}"));
    let r = run(
        r#"{"walk": true}"#,
        r#"{"walk": {"x": 1}}"#,
        &repo(),
        "live",
    );
    assert!(text(&r).contains("\"walk\": {\"x\": 1, \"c1\": [\"fp\", []]}"));
}

#[test]
fn a_remint_answered_is_not_asked_again() {
    assert_eq!(
        run(r#"{"remint": true}"#, "{}", &repo(), "live").reminted,
        1
    );
    let r = run(
        r#"{"remint": true}"#,
        r#"{"remint": null}"#,
        &repo(),
        "live",
    );
    assert_eq!(r.reminted, 0);
}

#[test]
fn a_raising_view_fails_the_round_with_the_first_error_in_order() {
    let q = r#"{"views": [{"path": "/a"}, {"path": "/b"}]}"#;
    let r = run(q, "{}", &repo(), "raise-b");
    assert_eq!(r.out, Err(la::AnswerError::View("OSError".into())));
    let q = r#"{"views": [{"path": "/bb"}, {"path": "/a"}]}"#;
    let r = run(q, "{}", &repo(), "raise-all");
    assert_eq!(r.out, Err(la::AnswerError::View("E3".into())));
}

#[test]
fn a_slow_view_is_unreachable() {
    let r = run(r#"{"views": [{"path": "/k-slow"}]}"#, "{}", &repo(), "slow");
    assert!(text(&r).ends_with("\"views\": {\"/k-slow\": \"unreachable\"}}"));
}

#[test]
fn ancestry_with_no_history_keeps_lowered_full_hashes() {
    let mut cap = repo();
    cap.state = State::Outside;
    let full = "A".repeat(40);
    let q = format!(r#"{{"ancestry": ["{full}", "abc", "{}"]}}"#, "g".repeat(40));
    let r = run(&q, "{}", &cap, "live");
    assert_eq!(r.built, 0);
    let want = format!(
        "\"ancestry_of\": [\"{}\"], \"ancestry\": [], ",
        "a".repeat(40)
    );
    assert!(text(&r).contains(&want), "{}", text(&r));
}

#[test]
fn an_earlier_key_keeps_its_place() {
    let r = run("{}", r#"{"views": 1, "remint_ok": false}"#, &repo(), "live");
    assert!(r.out.is_err());
    let r = run("{}", r#"{"z": 1, "remint_ok": false}"#, &repo(), "live");
    assert!(text(&r).starts_with("{\"z\": 1, \"remint_ok\": true, \"exchange\": null"));
}

#[test]
fn liveness_many_answers_none_where_a_read_raises() {
    let k = checkout(&serde_json::json!({"kind": "primary"}));
    let pairs = vec![(k.clone(), view("/a")), (k, view("/b"))];
    let got = la::path_liveness_many(&board(), &reader("raise-b"), &pairs);
    assert_eq!(got, vec![Some(cps("raise-b")), None]);
}

fn by_path() -> la::ViewRead<String> {
    Arc::new(|path: &[u32], _: &_| Ok(cps(if path == [0xDC80] { "live" } else { "gone" })))
}

fn view_at(path: &[u32]) -> bettermemory_core::membership_records::records::View {
    let mut v = view("/x");
    v.path = path.to_vec();
    v
}

#[test]
fn paths_that_differ_only_in_lone_surrogates_are_read_apart() {
    let k = checkout(&serde_json::json!({"kind": "primary"}));
    for other in [vec![0xDC81], vec![0xFFFD], cps("\\x{dc80}")] {
        let pairs = vec![
            (k.clone(), view_at(&[0xDC80])),
            (k.clone(), view_at(&other)),
        ];
        let got = la::path_liveness_many(&board(), &by_path(), &pairs);
        assert_eq!(got, vec![Some(cps("live")), Some(cps("gone"))]);
    }
}

#[test]
fn a_view_in_flight_does_not_answer_for_a_surrogate_neighbour() {
    let slow: la::ViewRead<String> = Arc::new(|path: &[u32], _: &_| {
        std::thread::sleep(Duration::from_millis(300));
        Ok(cps(if path == [0xDC80] { "live" } else { "gone" }))
    });
    let b = Arc::new(board());
    let k = checkout(&serde_json::json!({"kind": "primary"}));
    let (b1, s1, k1) = (Arc::clone(&b), Arc::clone(&slow), k.clone());
    let t = std::thread::spawn(move || la::path_liveness(&b1, &s1, &k1, &view_at(&[0xDC80])));
    std::thread::sleep(Duration::from_millis(50));
    let second = la::path_liveness(&b, &slow, &k, &view_at(&[0xDC81]));
    assert_eq!(t.join().unwrap(), Ok(cps("live")));
    assert_eq!(second, Ok(cps("gone")));
}

#[test]
fn non_ascii_text_whose_low_bytes_are_hex_is_no_full_hash() {
    let mut cap = repo();
    cap.state = State::Outside;
    let q = format!(r#"{{"ancestry": ["{}"]}}"#, "\u{161}".repeat(40));
    let r = run(&q, "{}", &cap, "live");
    assert!(text(&r).contains("\"ancestry_of\": [], "), "{}", text(&r));
}

#[test]
fn a_tuple_holding_a_list_in_asked_is_unhashable() {
    let int = |s: &str| PyValue::Int(s.into());
    let answers = |member: PyValue| {
        PyValue::Dict(vec![(
            PyValue::Str(cps("asked")),
            PyValue::List(vec![member]),
        )])
    };
    let go = |a: &PyValue| {
        la::answer(
            &load(&"{}".into()),
            a,
            &repo(),
            &mut |_: &[u32]| Counted::default(),
            &mut |_: &[u32], _, _, _| (Some("T".into()), "written".into()),
            &board(),
            &reader("live"),
        )
    };
    let bad = answers(PyValue::Tuple(vec![
        int("1"),
        PyValue::List(vec![int("2")]),
    ]));
    assert!(go(&bad).is_err());
    let good = answers(PyValue::Tuple(vec![int("1"), int("2")]));
    assert!(go(&good).is_ok());
}
