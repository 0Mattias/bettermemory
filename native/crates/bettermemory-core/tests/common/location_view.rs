//! Shared by the `location_view` tests: `continuity` run over the
//! generator's argument list, its answer spelled as `_line` writes it,
//! and the cross product of `_AXES`.

use bettermemory_core::json::loads;
use bettermemory_core::location_view::{continuity, Asks, Declined, Side};
use bettermemory_core::pyvalue::PyValue;

/// The generator's `_AXES`, as JSON.
pub const AXES: &str = r#"[[null,"","0123456789ABCDEFGHJKMNPQRS","ABCDEFGHJKMNPQRSTVWXYZ0123"],
[null,"h","g"],
[[],[["d",1,2]],[["d",1.0,2]],[["e",9]]],
[null,"","0123456789ABCDEFGHJKMNPQRS","ABCDEFGHJKMNPQRSTVWXYZ0123"],
[null,"h"],
[[],[["d",true,2]]],
[false,true],
[null,false,true],
[false,true],
[null,false,true]]"#;

pub fn parse(text: &str) -> PyValue {
    loads(&text.chars().map(u32::from).collect::<Vec<_>>()).expect("JSON")
}

fn items(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(x) | PyValue::Tuple(x) => x,
        other => panic!("a list, not {other:?}"),
    }
}

fn text(v: &PyValue) -> Option<String> {
    match v {
        PyValue::None => None,
        PyValue::Str(s) => Some(s.iter().map(|&c| char::from_u32(c).unwrap()).collect()),
        other => panic!("a text, not {other:?}"),
    }
}

fn flag(v: &PyValue) -> Option<bool> {
    match v {
        PyValue::None => None,
        PyValue::Bool(b) => Some(*b),
        other => panic!("a flag, not {other:?}"),
    }
}

pub fn stamps(v: &PyValue) -> Vec<Vec<PyValue>> {
    items(v).iter().map(|s| items(s).to_vec()).collect()
}

/// A `continuity` under test, its witness by name.
pub type Run = dyn Fn(Side<'_>, Side<'_>, bool, Asks<'_>) -> Result<(bool, &'static str), Declined>;

/// `(continues, witness, calls)`, or `Err(calls)` for TypeError.
pub type Answer = Result<(bool, &'static str, String), String>;

/// The generator's `_continuity` over `args`, with `run` the function
/// under test (the crate's `continuity`, or a mutant).
pub fn answer_with(args: &[PyValue], run: &Run) -> Answer {
    let token = text(&args[0]);
    let last = text(&args[1]);
    let mine = stamps(&args[2]);
    let there = text(&args[3]);
    let head = text(&args[4]);
    let here = stamps(&args[5]);
    let held = flag(&args[6]).unwrap();
    let conf = flag(&args[7]);
    let sup = flag(&args[8]).unwrap();
    let rem = flag(&args[9]);
    let calls = std::cell::RefCell::new(String::new());
    let mut holds = |_: &[&str]| {
        calls.borrow_mut().push('h');
        Ok(held)
    };
    let mut confirms = || {
        calls.borrow_mut().push('c');
        Ok(conf.unwrap())
    };
    let mut reminted = || {
        calls.borrow_mut().push('r');
        Ok(rem.unwrap())
    };
    let got = run(
        Side {
            token: token.as_deref(),
            head: last.as_deref(),
            stamps: &mine,
        },
        Side {
            token: there.as_deref(),
            head: head.as_deref(),
            stamps: &here,
        },
        sup,
        Asks {
            holds: &mut holds,
            confirms: conf.map(|_| &mut confirms as &mut dyn FnMut() -> Result<bool, Declined>),
            reminted: rem.map(|_| &mut reminted as &mut dyn FnMut() -> Result<bool, Declined>),
        },
    );
    let calls = calls.into_inner();
    match got {
        Ok((ok, how)) => Ok((ok, how, calls)),
        Err(Declined::Unhashable) => Err(calls),
        Err(other) => panic!("declined {other:?}"),
    }
}

pub fn answer(args: &[PyValue]) -> Answer {
    answer_with(args, &|a, b, s, k| {
        continuity(a, b, s, k).map(|(ok, w)| (ok, w.name()))
    })
}

/// `_line`: the answer as compact JSON.
pub fn line(a: &Answer) -> String {
    match a {
        Ok((ok, how, calls)) => format!("[{ok},\"{how}\",\"{calls}\"]"),
        Err(calls) => format!("[\"err\",\"TypeError\",\"{calls}\"]"),
    }
}

/// Every argument list of the cross product, in `itertools.product`'s
/// order.
pub fn cross() -> Vec<Vec<PyValue>> {
    let axes = parse(AXES);
    let axes: Vec<&[PyValue]> = items(&axes).iter().map(items).collect();
    let mut out: Vec<Vec<PyValue>> = vec![Vec::new()];
    for axis in axes {
        out = out
            .into_iter()
            .flat_map(|prefix| {
                axis.iter().map(move |v| {
                    let mut p = prefix.clone();
                    p.push(v.clone());
                    p
                })
            })
            .collect();
    }
    out
}
