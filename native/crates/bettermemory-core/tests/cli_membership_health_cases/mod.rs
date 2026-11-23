//! Replay the real Python model's Health, lookup sequence and public projection.
use super::common::membership_rows::{dec, enc, field, list, name};
use bettermemory_core::{
    cli::membership_health::{health_dict, health_lines, CheckoutError},
    membership_records::{
        outcomes::{Health, UnresolvedProject},
        records::{Project, Proposal},
    },
    pyvalue::{repr_str, PyStr, PyValue},
    recall_prompt::PyException,
    unicode::cps,
};
use std::cell::RefCell;
pub fn text(v: &PyValue) -> PyStr {
    if let PyValue::Str(s) = v {
        s.clone()
    } else {
        panic!("expected text: {v:?}")
    }
}
fn opt(v: &PyValue) -> Option<PyStr> {
    (v != &PyValue::None).then(|| text(v))
}
pub fn health(spec: &PyValue) -> Health<PyValue> {
    let proposals = list(field(spec, "proposals"))
        .iter()
        .map(|v| {
            let p = list(v);
            Proposal {
                id: text(&p[0]),
                checkout: opt(&p[1]),
                kind: text(&p[2]),
                reason: text(&p[3]),
                line: text(&p[4]),
                opened: text(&p[5]),
                candidates: vec![],
                evidence: PyValue::Dict(vec![]),
                state: cps("open"),
                answer: None,
                answered_by: None,
                signature: None,
            }
        })
        .collect();
    let projects = list(field(spec, "projects"))
        .iter()
        .map(|v| {
            let p = list(v);
            Project {
                id: text(&p[0]),
                name: text(&p[1]),
                provisional: opt(&p[2]),
                merged_into: None,
                parent: None,
                tenant: None,
                folder: false,
                boundary: false,
                minted: cps("born"),
            }
        })
        .collect();
    let mut h = Health::new(
        proposals,
        projects,
        list(field(spec, "turns"))
            .iter()
            .map(|v| {
                let r = list(v);
                (text(&r[0]), r[1].clone())
            })
            .collect(),
        field(spec, "indeterminate").clone(),
        field(spec, "deferred").clone(),
        list(field(spec, "without")).iter().map(text).collect(),
    );
    h.unresolved = list(field(spec, "unresolved"))
        .iter()
        .map(|v| {
            let r = list(v);
            UnresolvedProject {
                project: text(&r[0]),
                name: text(&r[1]),
                state: text(&r[2]),
                checkout: opt(&r[3]),
                path: opt(&r[4]),
                memories: r[5].clone(),
                episodes: r[6].clone(),
            }
        })
        .collect();
    h
}
pub fn exc(class: &str, message: PyStr) -> PyException {
    PyException {
        class: class.into(),
        message,
    }
}
fn expect<T>(result: &Result<T, PyException>, want: &PyValue, encode: impl FnOnce(&T) -> PyValue) {
    match result {
        Ok(value) => assert_eq!(encode(value), *field(want, "value")),
        Err(e) => assert_eq!(
            enc(&PyValue::List(vec![
                PyValue::str(&e.class),
                PyValue::Str(e.message.clone())
            ])),
            *field(want, "error")
        ),
    }
}
pub fn check(
    row: &PyValue,
    mut path: impl FnMut(&[u32]) -> Result<Option<PyStr>, CheckoutError>,
) -> bool {
    let calls = RefCell::new(Vec::new());
    let result = health_dict(
        || {
            calls
                .borrow_mut()
                .push(PyValue::List(vec![PyValue::str("health")]));
            if row.get("failure") == Some(&PyValue::Bool(true)) {
                return Err(exc("KeyError", cps("'top-health'")));
            }
            Ok(health(&dec(field(row, "spec"))))
        },
        |id| {
            calls.borrow_mut().push(PyValue::List(vec![
                PyValue::str("checkout"),
                PyValue::Str(id.to_vec()),
            ]));
            path(id)
        },
    );
    let matched_calls = enc(&PyValue::List(calls.into_inner())) == *field(row, "calls");
    expect(&result, field(row, "projection"), enc);
    if let Ok(mut data) = result {
        let lines_out = |v: &Vec<PyStr>| {
            enc(&PyValue::List(
                v.iter().cloned().map(PyValue::Str).collect(),
            ))
        };
        expect(&health_lines(&data), field(row, "lines"), lines_out);
        let PyValue::Dict(items) = &mut data else {
            unreachable!()
        };
        let (_, PyValue::Dict(turns)) = items
            .iter_mut()
            .find(|(k, _)| k == &PyValue::str("suppressed_recall_turns"))
            .unwrap()
        else {
            unreachable!()
        };
        turns.reverse();
        expect(
            &health_lines(&data),
            field(row, "supplied_lines"),
            lines_out,
        );
    }
    matched_calls
}
pub fn scripted(row: &PyValue) -> bool {
    let responses = dec(field(row, "responses"));
    let mut steps = list(&responses).iter();
    check(row, |_| {
        let args = list(steps.next().unwrap());
        match name(&args[0]).as_str() {
            "key" | "view-key" => Err(CheckoutError::Missing),
            "path-error" => Err(CheckoutError::Raised(exc(
                "KeyError",
                repr_str(&text(&args[1])),
            ))),
            "error" => Err(CheckoutError::Raised(exc("RuntimeError", text(&args[1])))),
            "no-view" => Ok(None),
            "view" => Ok(opt(&args[1])),
            other => panic!("unknown step: {other}"),
        }
    })
}
