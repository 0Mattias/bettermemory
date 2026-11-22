//! The two pure schema scrubs of `builder.py`: `_strip_titles` (294-332)
//! and `_fold_nullable` (335-378), each an in-place walk over a JSON tree
//! (see the README's "The server builder (`builder.py`)").
//!
//! Both walk with an explicit stack, not native recursion. Each node's
//! edit reads only that node, and the walk then visits what the node
//! holds after the edit, so the order of visits does not change the
//! answer.

use crate::pyvalue::PyValue;
use crate::unicode;

/// Whether `key` is the `str` `name`.
fn is_key(key: &PyValue, name: &str) -> bool {
    matches!(key, PyValue::Str(k) if *k == unicode::cps(name))
}

fn position(pairs: &[(PyValue, PyValue)], name: &str) -> Option<usize> {
    pairs.iter().position(|(k, _)| is_key(k, name))
}

/// The children a walk visits: a dict's values, except that a dict held
/// under `properties`, `$defs` or `definitions` is passed through to its
/// values; a list's items.
fn children<'a>(node: &'a mut PyValue, stack: &mut Vec<&'a mut PyValue>) {
    match node {
        PyValue::Dict(pairs) => {
            for (key, value) in pairs.iter_mut() {
                let named_map = ["properties", "$defs", "definitions"]
                    .iter()
                    .any(|name| is_key(key, name));
                if named_map && matches!(value, PyValue::Dict(_)) {
                    if let PyValue::Dict(subs) = value {
                        stack.extend(subs.iter_mut().map(|(_, sub)| sub));
                    }
                } else {
                    stack.push(value);
                }
            }
        }
        PyValue::List(items) => stack.extend(items.iter_mut()),
        _ => {}
    }
}

/// `_strip_titles(node)`: from every schema node, `title` popped and a
/// `default` of `None` deleted.
pub fn strip_titles(root: &mut PyValue) {
    let mut stack = vec![root];
    while let Some(node) = stack.pop() {
        if let PyValue::Dict(pairs) = node {
            if let Some(i) = position(pairs, "title") {
                pairs.remove(i);
            }
            if let Some(i) = position(pairs, "default") {
                if matches!(pairs[i].1, PyValue::None) {
                    pairs.remove(i);
                }
            }
        }
        children(node, &mut stack);
    }
}

/// `branch == {"type": "null"}`: a dict of the one pair `("type",
/// "null")`. One pair makes key order moot and a `str` value makes
/// numeric equality across types moot.
fn is_null_branch(branch: &PyValue) -> bool {
    match branch {
        PyValue::Dict(pairs) => {
            pairs.len() == 1
                && is_key(&pairs[0].0, "type")
                && matches!(&pairs[0].1, PyValue::Str(v) if *v == unicode::cps("null"))
        }
        _ => false,
    }
}

/// The edit `_fold_nullable` makes to one dict, if its shape folds.
fn fold_one(pairs: &mut Vec<(PyValue, PyValue)>) {
    let Some(at) = position(pairs, "anyOf") else {
        return;
    };
    let PyValue::List(branches) = &pairs[at].1 else {
        return;
    };
    if branches.len() != 2 || !branches.iter().any(is_null_branch) {
        return;
    }
    let other = if is_null_branch(&branches[1]) {
        &branches[0]
    } else {
        &branches[1]
    };
    let PyValue::Dict(other) = other else {
        return;
    };
    let ty = match position(other, "type").map(|i| &other[i].1) {
        Some(t @ PyValue::Str(_)) => t.clone(),
        _ => return,
    };
    if position(other, "anyOf").is_some()
        || position(other, "$ref").is_some()
        || position(pairs, "type").is_some()
    {
        return;
    }
    let mut folded: Vec<(PyValue, PyValue)> = other
        .iter()
        .filter(|(k, _)| !is_key(k, "type"))
        .cloned()
        .collect();
    folded.push((
        PyValue::str("type"),
        PyValue::List(vec![ty, PyValue::str("null")]),
    ));
    pairs.remove(at);
    for (key, value) in folded {
        match pairs.iter_mut().find(|(k, _)| *k == key) {
            Some(slot) => slot.1 = value,
            None => pairs.push((key, value)),
        }
    }
}

/// `_fold_nullable(node)`: every two-branch `anyOf` of a named type and
/// `{"type": "null"}` folded into a type list.
pub fn fold_nullable(root: &mut PyValue) {
    let mut stack = vec![root];
    while let Some(node) = stack.pop() {
        if let PyValue::Dict(pairs) = node {
            fold_one(pairs);
        }
        children(node, &mut stack);
    }
}
