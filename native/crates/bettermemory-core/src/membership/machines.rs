//! The machine rows of the model (`membership.py` 370-424): this
//! machine's row written, with the event that names a clone or a change
//! of facts, and every machine row read.

use super::{column, raise, str_of, Error, Membership, MembershipStore, Query, Result2};
use crate::location_witness::py_eq;
use crate::machine::MachineIdentity;
use crate::membership_lines::dict_of;
use crate::membership_records::records::Machine;
use crate::pyvalue::PyValue;

fn opt(v: Option<&[u32]>) -> PyValue {
    v.map_or(PyValue::None, |t| PyValue::Str(t.to_vec()))
}

impl<S: MembershipStore> Membership<S> {
    /// `record_machine(identity)` (370-411): the row written when the
    /// machine has none, with `machine_cloned` when it names a
    /// predecessor; else, when the stored facts differ by Python's `==`
    /// (a dict's keys in any order, `1 == 1.0 == True`), the row with the
    /// facts replaced in place and `machine_facts_updated`.
    pub fn record_machine(&mut self, identity: &MachineIdentity) -> Result2<(), S::Error> {
        let facts = identity.facts.to_dict();
        self.call(|m| {
            let rows = m.read(
                "machines",
                &Query {
                    where_: Some(vec![(
                        crate::unicode::cps("id"),
                        PyValue::Str(identity.id.clone()),
                    )]),
                    ..Query::default()
                },
            )?;
            let Some(row) = rows.into_iter().next() else {
                let minted = m.stamp()?;
                let row = PyValue::Dict(vec![
                    (PyValue::str("id"), PyValue::Str(identity.id.clone())),
                    (PyValue::str("facts_json"), facts.clone()),
                    (
                        PyValue::str("predecessor"),
                        opt(identity.predecessor.as_deref()),
                    ),
                    (PyValue::str("minted"), PyValue::Str(minted)),
                ]);
                m.put("machine", &row)?;
                if let Some(old) = &identity.predecessor {
                    let recorded = identity
                        .recorded
                        .as_ref()
                        .map_or(PyValue::None, |r| r.to_dict());
                    m.event(
                        "machine_cloned",
                        vec![
                            ("old", PyValue::Str(old.clone())),
                            ("new", PyValue::Str(identity.id.clone())),
                            ("recorded", recorded),
                            ("seen", facts.clone()),
                        ],
                    )?;
                }
                return Ok(());
            };
            let stored = column(&row, "facts_json")?.clone();
            if !py_eq(&stored, &facts) {
                let PyValue::Dict(items) = &row else {
                    return raise("TypeError", "a machine row that is not a dict".into());
                };
                let mut items = items.clone();
                for (k, v) in &mut items {
                    if matches!(k, PyValue::Str(t) if t.iter().copied().eq("facts_json".chars().map(u32::from)))
                    {
                        *v = facts.clone();
                    }
                }
                m.put("machine", &PyValue::Dict(items))?;
                m.event(
                    "machine_facts_updated",
                    vec![
                        ("machine", PyValue::Str(identity.id.clone())),
                        ("recorded", stored),
                        ("seen", facts.clone()),
                    ],
                )?;
            }
            Ok(())
        })
    }

    /// `machines()` (412-424): each row as a `Machine`, `facts` through
    /// `dict(... or {})`. A `predecessor` that is neither None nor a str
    /// declines (NotPorted): the record holds a str.
    pub fn machines(&self) -> Result2<Vec<Machine>, S::Error> {
        let mut out = Vec::new();
        for r in self.read("machines", &Query::default())? {
            let id = str_of(column(&r, "id")?)?;
            let facts = PyValue::Dict(dict_of(column(&r, "facts_json")?).map_err(Error::Raised)?);
            let predecessor = match column(&r, "predecessor")? {
                PyValue::None => None,
                PyValue::Str(t) => Some(t.clone()),
                _ => return raise("NotPorted", "a predecessor that is not a str".into()),
            };
            let minted = str_of(column(&r, "minted")?)?;
            out.push(Machine {
                id,
                facts,
                predecessor,
                minted,
            });
        }
        Ok(out)
    }
}
