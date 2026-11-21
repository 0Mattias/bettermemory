//! `machine.py`, unit 1: the machine identity record. `MachineFacts`
//! (`to_dict`, `from_dict`), `MachineIdentity`, `_identity_from`,
//! `identity_to_json`, `new_machine_id` from given random bytes, and
//! `is_clone`. Reading the hardware id, the hostname, the id file's object
//! and the two files of the state directory are later units.
//!
//! The values these read are the Python values a JSON body or a facts
//! file yields ([`PyValue`]); `str()` of a value inside the id file's
//! triple is written here ([`py_text`]) with the reference's two raises:
//! ValueError for an `int` past 4,300 digits and RecursionError past
//! 9,999 nested objects, whichever a left-to-right walk meets first.

use crate::pyvalue::{self, PyError, PyStr, PyValue};

pub mod facts;

/// `MACHINE_ID_FILE`.
pub const MACHINE_ID_FILE: &str = "machine-id";
/// `MACHINE_FACTS_FILE`.
pub const MACHINE_FACTS_FILE: &str = "machine.json";
/// `_ID_HEADER`.
pub const ID_HEADER: &str = "bettermemory machine id v1";
/// `_CROCKFORD`.
pub const CROCKFORD: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";
/// The bytes `new_machine_id` draws (`secrets.token_bytes(17)`).
pub const ID_RANDOM_BYTES: usize = 17;
/// The characters of a machine id.
pub const ID_LENGTH: usize = 26;
/// `sys.get_int_max_str_digits()` at its default.
pub const INT_MAX_STR_DIGITS: usize = 4300;
/// The most objects `str()` of a nested value spells, counted from the
/// value itself, before RecursionError (CPython 3.13.13, called from
/// `MachineFacts.from_dict`).
pub const STR_MAX_DEPTH: usize = 9999;

/// The id file's object: (device, inode, birth time in ns or None).
pub type IdFile = (PyStr, PyStr, Option<PyStr>);

/// `MachineFacts`: the hardware id, the hostname, the id file's object.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MachineFacts {
    pub hardware: Option<PyStr>,
    pub hostname: Option<PyStr>,
    pub id_file: Option<IdFile>,
}

fn opt_str(value: &Option<PyStr>) -> PyValue {
    match value {
        None => PyValue::None,
        Some(s) => PyValue::Str(s.clone()),
    }
}

fn key(name: &str) -> PyValue {
    PyValue::str(name)
}

impl MachineFacts {
    /// `MachineFacts.to_dict`: the three keys in the reference's order,
    /// the id file as a list.
    pub fn to_dict(&self) -> PyValue {
        let id_file = match &self.id_file {
            None => PyValue::None,
            Some((device, inode, birth)) => PyValue::List(vec![
                PyValue::Str(device.clone()),
                PyValue::Str(inode.clone()),
                opt_str(birth),
            ]),
        };
        PyValue::Dict(vec![
            (key("hardware"), opt_str(&self.hardware)),
            (key("hostname"), opt_str(&self.hostname)),
            (key("id_file"), id_file),
        ])
    }

    /// `MachineFacts.from_dict`: None for a value that is not a dict; a
    /// field that is not a `str` is None; the id file is read only from a
    /// list of exactly three, each part through `str()` (the birth time
    /// kept None when it is None), which may raise.
    pub fn from_dict(raw: &PyValue) -> Result<Option<MachineFacts>, PyError> {
        if !matches!(raw, PyValue::Dict(_)) {
            return Ok(None);
        }
        let text = |name: &str| match raw.get(name) {
            Some(PyValue::Str(s)) => Some(s.clone()),
            _ => None,
        };
        let id_file = match raw.get("id_file") {
            Some(PyValue::List(items)) if items.len() == 3 => {
                let device = py_text(&items[0])?;
                let inode = py_text(&items[1])?;
                let birth = match &items[2] {
                    PyValue::None => None,
                    other => Some(py_text(other)?),
                };
                Some((device, inode, birth))
            }
            _ => None,
        };
        Ok(Some(MachineFacts {
            hardware: text("hardware"),
            hostname: text("hostname"),
            id_file,
        }))
    }
}

/// `MachineIdentity`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MachineIdentity {
    pub id: PyStr,
    pub facts: MachineFacts,
    pub predecessor: Option<PyStr>,
    pub minted: bool,
    pub cloned: bool,
    pub facts_updated: bool,
    pub recorded: Option<MachineFacts>,
}

/// `bool(value)`: Python truthiness.
pub fn truthy(value: &PyValue) -> bool {
    match value {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(text) => text != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(s) => !s.is_empty(),
        PyValue::Tuple(items) | PyValue::List(items) => !items.is_empty(),
        PyValue::Dict(items) => !items.is_empty(),
        // A datetime and a pure path define no falsehood.
        PyValue::DateTime(_) | PyValue::Path(_) => true,
    }
}

/// `_identity_from(raw)`: None unless `raw` is a mapping whose `id` is a
/// `str` and whose `facts` read as facts; `recorded` is read only then,
/// after `facts`, so a raise from it comes second.
pub fn identity_from(raw: &PyValue) -> Result<Option<MachineIdentity>, PyError> {
    if !matches!(raw, PyValue::Dict(_)) {
        return Ok(None);
    }
    let Some(PyValue::Str(id)) = raw.get("id") else {
        return Ok(None);
    };
    let Some(facts) = MachineFacts::from_dict(raw.get("facts").unwrap_or(&PyValue::None))? else {
        return Ok(None);
    };
    let predecessor = match raw.get("predecessor") {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    };
    let flag = |name: &str| raw.get(name).is_some_and(truthy);
    let recorded = MachineFacts::from_dict(raw.get("recorded").unwrap_or(&PyValue::None))?;
    Ok(Some(MachineIdentity {
        id: id.clone(),
        facts,
        predecessor,
        minted: flag("minted"),
        cloned: flag("cloned"),
        facts_updated: flag("facts_updated"),
        recorded,
    }))
}

/// `identity_to_json(identity)`: the seven keys in the reference's order.
pub fn identity_to_json(identity: &MachineIdentity) -> PyValue {
    PyValue::Dict(vec![
        (key("id"), PyValue::Str(identity.id.clone())),
        (key("facts"), identity.facts.to_dict()),
        (key("predecessor"), opt_str(&identity.predecessor)),
        (key("minted"), PyValue::Bool(identity.minted)),
        (key("cloned"), PyValue::Bool(identity.cloned)),
        (key("facts_updated"), PyValue::Bool(identity.facts_updated)),
        (
            key("recorded"),
            identity
                .recorded
                .as_ref()
                .map_or(PyValue::None, MachineFacts::to_dict),
        ),
    ])
}

/// `new_machine_id()` with the 17 bytes `secrets.token_bytes` would
/// return: the bytes as a big-endian integer, its low 130 bits written
/// five at a time, the least significant first.
pub fn new_machine_id(urandom: &[u8; ID_RANDOM_BYTES]) -> String {
    let bit = |k: usize| (urandom[ID_RANDOM_BYTES - 1 - k / 8] >> (k % 8)) & 1;
    (0..ID_LENGTH)
        .map(|i| {
            let v = (0..5).fold(0usize, |v, j| v | usize::from(bit(5 * i + j)) << j);
            char::from(CROCKFORD[v])
        })
        .collect()
}

/// `is_clone(recorded, seen)`: the hardware ids decide where both are
/// non-empty; otherwise a clone needs both a different hostname and a
/// different id file object.
pub fn is_clone(recorded: &MachineFacts, seen: &MachineFacts) -> bool {
    let present = |h: &Option<PyStr>| h.as_ref().is_some_and(|s| !s.is_empty());
    if present(&recorded.hardware) && present(&seen.hardware) {
        return recorded.hardware != seen.hardware;
    }
    recorded.hostname != seen.hostname && recorded.id_file != seen.id_file
}

fn push_ascii(out: &mut PyStr, text: &str) {
    out.extend(text.bytes().map(u32::from));
}

/// `str(value)` as `from_dict` meets it: a scalar's own text, a
/// container's `repr`, walked without native recursion. ValueError for an
/// `int` of more than 4,300 digits (its sign not counted), RecursionError
/// for an object past [`STR_MAX_DEPTH`], the first met in the order `repr`
/// spells them.
pub fn py_text(value: &PyValue) -> Result<PyStr, PyError> {
    enum Piece<'a> {
        Value(&'a PyValue, usize),
        Text(&'static str),
    }
    let mut out = Vec::new();
    let mut work = vec![Piece::Value(value, 0)];
    while let Some(piece) = work.pop() {
        let (value, depth) = match piece {
            Piece::Text(text) => {
                push_ascii(&mut out, text);
                continue;
            }
            Piece::Value(value, depth) => (value, depth),
        };
        if depth + 1 > STR_MAX_DEPTH {
            return Err(PyError::RecursionError);
        }
        match value {
            PyValue::Int(text) => {
                if text.trim_start_matches('-').len() > INT_MAX_STR_DIGITS {
                    return Err(PyError::ValueError);
                }
                push_ascii(&mut out, text);
            }
            PyValue::Str(s) if depth == 0 => out.extend_from_slice(s),
            PyValue::Str(s) => out.extend(pyvalue::repr_str(s)),
            PyValue::List(items) | PyValue::Tuple(items) => {
                let list = matches!(value, PyValue::List(_));
                push_ascii(&mut out, if list { "[" } else { "(" });
                work.push(Piece::Text(if list { "]" } else { ")" }));
                if !list && items.len() == 1 {
                    work.push(Piece::Text(","));
                }
                for (i, item) in items.iter().enumerate().rev() {
                    work.push(Piece::Value(item, depth + 1));
                    if i > 0 {
                        work.push(Piece::Text(", "));
                    }
                }
            }
            PyValue::Dict(items) => {
                push_ascii(&mut out, "{");
                work.push(Piece::Text("}"));
                for (i, (k, v)) in items.iter().enumerate().rev() {
                    work.push(Piece::Value(v, depth + 1));
                    work.push(Piece::Text(": "));
                    work.push(Piece::Value(k, depth + 1));
                    if i > 0 {
                        work.push(Piece::Text(", "));
                    }
                }
            }
            PyValue::None | PyValue::Bool(_) | PyValue::Float(_) if depth == 0 => {
                out.extend(pyvalue::py_str(value)?)
            }
            PyValue::DateTime(_) | PyValue::Path(_) if depth == 0 => {
                out.extend(pyvalue::py_str(value)?)
            }
            _ => out.extend(pyvalue::repr(value)?),
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn id_of_zero_bytes_is_zeros() {
        assert_eq!(new_machine_id(&[0; 17]), "0".repeat(26));
        assert_eq!(new_machine_id(&[0xff; 17]), "Z".repeat(26));
    }
}
