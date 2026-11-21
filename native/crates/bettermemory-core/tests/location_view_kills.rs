//! Plausible wrong `continuity`s, each failed by some row of the cross
//! product the reference answers: the port's answers (calls included)
//! must differ from every mutant's somewhere.

mod common;

use bettermemory_core::location_view::{continuity, py_eq, Asks, Declined, Side};
use bettermemory_core::pyvalue::PyValue;
use common::location_view::{answer, answer_with, cross, Answer, Run};

fn killed(name: &str, mutant: &Run) {
    let all = cross();
    let differs = all
        .iter()
        .any(|args| answer(args) != answer_with(args, mutant));
    assert!(differs, "the mutant {name} survives the cross product");
}

fn real(a: Side<'_>, b: Side<'_>, s: bool, k: Asks<'_>) -> Result<(bool, &'static str), Declined> {
    continuity(a, b, s, k).map(|(ok, w)| (ok, w.name()))
}

#[test]
fn ignoring_superseded_is_killed() {
    killed("superseded ignored", &|a, b, _s, k| real(a, b, false, k));
}

#[test]
fn an_empty_token_read_as_a_token_is_killed() {
    killed("empty token kept", &|a, b, s, k| {
        let a = Side {
            token: a.token.map(|t| if t.is_empty() { "x" } else { t }),
            ..a
        };
        real(a, b, s, k)
    });
}

#[test]
fn asking_holds_first_is_killed() {
    killed("holds asked eagerly", &|a, b, s, k| {
        let Asks {
            holds,
            confirms,
            reminted,
        } = k;
        if let Some(h) = a.head {
            let _ = holds(&[h]);
        }
        real(
            a,
            b,
            s,
            Asks {
                holds,
                confirms,
                reminted,
            },
        )
    });
}

#[test]
fn never_asking_reminted_is_killed() {
    killed("no remint", &|a, b, s, k| {
        real(
            a,
            b,
            s,
            Asks {
                reminted: None,
                ..k
            },
        )
    });
}

#[test]
fn never_asking_confirms_is_killed() {
    killed("no confirm", &|a, b, s, k| {
        real(
            a,
            b,
            s,
            Asks {
                confirms: None,
                ..k
            },
        )
    });
}

#[test]
fn stamps_compared_strictly_are_killed() {
    let one = Answer::Ok((true, "init_stamps", String::new()));
    let args: Vec<PyValue> = common::location_view::parse(
        r#"[null,null,[["d",1.0,2]],null,null,[["d",true,2]],false,null,false,null]"#,
    )
    .into_list();
    assert_eq!(answer(&args), one);
    assert!(py_eq(&PyValue::Float(1.0), &PyValue::Bool(true)));
    assert!(!py_eq(&PyValue::Float(0.5), &PyValue::Int("0".into())));
    assert!(py_eq(&PyValue::Float(-0.0), &PyValue::Int("0".into())));
    assert!(!py_eq(&PyValue::Str(vec![49]), &PyValue::Int("1".into())));
}

/// A tuple holding a list is unhashable: the reference raises TypeError
/// building the set (`{tuple(s) for s in stamps}`), at any depth.
#[test]
fn a_tuple_holding_a_list_is_unhashable() {
    let stamps = vec![vec![PyValue::Tuple(vec![
        PyValue::Int("1".into()),
        PyValue::List(vec![PyValue::Int("2".into())]),
    ])]];
    let side = Side {
        token: None,
        head: None,
        stamps: &stamps,
    };
    let got = continuity(
        side,
        side,
        false,
        Asks {
            holds: &mut |_| Ok(false),
            confirms: None,
            reminted: None,
        },
    );
    assert_eq!(got, Err(Declined::Unhashable));
}

trait IntoList {
    fn into_list(self) -> Vec<PyValue>;
}

impl IntoList for PyValue {
    fn into_list(self) -> Vec<PyValue> {
        match &self {
            PyValue::List(x) => x.clone(),
            other => panic!("a list, not {other:?}"),
        }
    }
}
