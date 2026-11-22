//! `policy.py` whole: the `[policy]` switches that forbid a write tool's
//! override flags, the refusal they raise and its audit row.
//!
//! `check_overrides` reads the switches off a [`Config`];
//! [`check_overrides_with`] takes them as values, with the managed test as
//! a closure, so a duck-typed config reaches the same rule. The refusal is
//! an error value whose `Display` is `str(PolicyRefusal(...))`; at the
//! handler boundary it is a `ValueError`.

use std::fmt;

use crate::config::{Config, Setting, ACKNOWLEDGE_NAMES};
use crate::pyvalue::PyValue;

/// `REFUSAL_STATUS`: the first word of every refusal's text.
pub const REFUSAL_STATUS: &str = "policy_refused";

const FORCE_KEY: &str = "policy.forbid_force";
const ACKNOWLEDGE_KEY: &str = "policy.forbid_acknowledge";

/// `_label(key)`: "section.key" as the config file spells it. Every key
/// the module passes holds a dot; a key without one panics, as the
/// reference's tuple unpacking raises ValueError.
pub fn label(key: &str) -> String {
    let (section, name) = key
        .split_once('.')
        .unwrap_or_else(|| panic!("not enough values to unpack: {key:?} holds no dot"));
    format!("[{section}] {name}")
}

/// `PolicyRefusal`: a call refused because it passed a flag `[policy]`
/// forbids.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PolicyRefusal {
    pub tool: String,
    /// The flags as the call passed them, in the order the check names
    /// them.
    pub flags: Vec<String>,
    /// The switches that forbid them, as "section.key".
    pub keys: Vec<String>,
    /// The subset of `keys` the managed file pins, sorted (a frozenset).
    pub managed: Vec<String>,
}

impl PolicyRefusal {
    /// `event_fields()`: the refusal's audit row.
    pub fn event_fields(&self) -> PyValue {
        let list =
            |items: &[String]| PyValue::List(items.iter().map(|s| PyValue::str(s)).collect());
        PyValue::Dict(vec![
            (PyValue::str("status"), PyValue::str(REFUSAL_STATUS)),
            (PyValue::str("policy_flags"), list(&self.flags)),
            (PyValue::str("policy_keys"), list(&self.keys)),
            (PyValue::str("policy_managed"), list(&self.managed)),
        ])
    }
}

impl fmt::Display for PolicyRefusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&message(&self.tool, &self.flags, &self.keys, &self.managed))
    }
}

impl std::error::Error for PolicyRefusal {}

/// `_message(tool, flags, keys, managed)`.
pub fn message(tool: &str, flags: &[String], keys: &[String], managed: &[String]) -> String {
    let passed = flags
        .iter()
        .map(|flag| format!("{flag}=True"))
        .collect::<Vec<_>>()
        .join(" and ");
    let switches = keys
        .iter()
        .map(|key| {
            let how = if managed.contains(key) {
                "(managed: pinned by the organisation's managed config, which the \
                 user's own config cannot change)"
            } else {
                "(set in the user's config)"
            };
            format!("{} {how}", label(key))
        })
        .collect::<Vec<_>>()
        .join("; ");
    let outcome = if tool == "memory_write" {
        "Nothing was written."
    } else {
        "Nothing was changed."
    };
    let mut remedy = String::from(if flags.len() == 1 {
        "Re-send the call without that flag. The gate it skips then answers as usual, \
         and what it refuses has to be fixed in the content itself."
    } else {
        "Re-send the call without those flags. The gates they skip then answer as usual, \
         and what they refuse has to be fixed in the content itself."
    });
    if flags.iter().any(|flag| flag == "force") {
        remedy.push_str(
            " A duplicate stays refused as one: update the existing memory with \
             memory_update instead.",
        );
    }
    format!(
        "{REFUSAL_STATUS}: {tool} was called with {passed}, which this install forbids: \
         {switches}. {outcome} {remedy}"
    )
}

/// `check_overrides` over the switches as values: `forbid_force` and
/// `forbid_acknowledge` as `config.policy` holds them, `is_managed` as
/// `key in config.managed_keys`, and `acknowledge` as `acknowledge.get(name,
/// False)` read as a truth value.
pub fn check_overrides_with(
    forbid_force: bool,
    forbid_acknowledge: &[String],
    is_managed: &dyn Fn(&str) -> bool,
    tool: &str,
    force: bool,
    acknowledge: &dyn Fn(&str) -> bool,
) -> Result<(), PolicyRefusal> {
    if !forbid_force && forbid_acknowledge.is_empty() {
        return Ok(());
    }
    let mut flags = Vec::new();
    let mut keys = Vec::new();
    if force && forbid_force {
        flags.push("force".to_owned());
        keys.push(FORCE_KEY.to_owned());
    }
    let acknowledged: Vec<&str> = ACKNOWLEDGE_NAMES
        .iter()
        .copied()
        .filter(|name| acknowledge(name) && forbid_acknowledge.iter().any(|f| f == name))
        .collect();
    if !acknowledged.is_empty() {
        flags.extend(
            acknowledged
                .iter()
                .map(|name| format!("acknowledge_{name}")),
        );
        keys.push(ACKNOWLEDGE_KEY.to_owned());
    }
    if flags.is_empty() {
        return Ok(());
    }
    let mut managed: Vec<String> = keys.iter().filter(|k| is_managed(k)).cloned().collect();
    managed.sort();
    Err(PolicyRefusal {
        tool: tool.to_owned(),
        flags,
        keys,
        managed,
    })
}

/// `check_overrides(config, tool, force=force, acknowledge=...)`.
pub fn check_overrides(
    config: &Config,
    tool: &str,
    force: bool,
    acknowledge: &dyn Fn(&str) -> bool,
) -> Result<(), PolicyRefusal> {
    let forbid_force = config.get("policy", "forbid_force").as_bool();
    let empty = Vec::new();
    let forbid_acknowledge = match config.get("policy", "forbid_acknowledge") {
        Setting::List(items) => items,
        _ => &empty,
    };
    check_overrides_with(
        forbid_force,
        forbid_acknowledge,
        &|key| config.is_managed(key),
        tool,
        force,
        acknowledge,
    )
}
