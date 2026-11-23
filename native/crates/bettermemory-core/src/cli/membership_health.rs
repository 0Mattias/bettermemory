//! The membership section shared by CLI health and memory_admin health.
//! The caller supplies the membership query and the process machine opener.
use crate::location_caller::ProcessMachine;
use crate::machine::MachineIdentity;
use crate::membership::{Error, Membership, MembershipStore, Options};
use crate::membership_records::outcomes::Health;
use crate::printable::quoted_into;
use crate::pyvalue::{py_str, PyStr, PyValue};
use crate::recall_prompt::PyException;
use crate::store::home::{truthy, type_name};
use crate::toml::{INT_MAX_STR_DIGITS, INT_STR_LIMIT_MESSAGE};
use crate::unicode::cps;

/// Construct the read model for the cached process identity. This call never records the machine.
pub fn health_model<S: MembershipStore, E>(
    store: S,
    process: &ProcessMachine,
    open: impl FnOnce() -> Result<MachineIdentity, E>,
    options: Options,
) -> Result<Membership<S>, E> {
    Ok(Membership::new(
        store,
        process.process_machine(open)?.id,
        options,
    ))
}

/// An exception from the guarded checkout/newest-view read. A Python binding recognizes
/// KeyError subclasses in this region and passes other failures with their original text.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CheckoutError {
    Missing,
    Raised(PyException),
}
impl From<PyException> for CheckoutError {
    fn from(error: PyException) -> Self {
        if error.class == "KeyError" {
            Self::Missing
        } else {
            Self::Raised(error)
        }
    }
}

/// Supply `_where`'s lookup from a concrete membership model's checkout and newest view.
pub fn checkout_path<S: MembershipStore>(
    model: &Membership<S>,
    id: &[u32],
    store_error: impl FnOnce(S::Error) -> CheckoutError,
) -> Result<Option<PyStr>, CheckoutError> {
    let checkout = model.checkout(id).map_err(|error| match error {
        Error::Store(e) => store_error(e),
        Error::Raised(e) => PyException {
            class: e.class.to_owned(),
            message: cps(&e.message),
        }
        .into(),
    })?;
    Ok(checkout.newest_view().map(|view| view.path.clone()))
}

/// `_where`: the lookup covers checkout and newest_view, both inside the reference's KeyError guard.
pub fn where_path(
    id: Option<&[u32]>,
    lookup: &mut impl FnMut(&[u32]) -> Result<Option<PyStr>, CheckoutError>,
) -> Result<Option<PyStr>, PyException> {
    match id.map(lookup).transpose() {
        Ok(path) => Ok(path.flatten()),
        Err(CheckoutError::Missing) => Ok(None),
        Err(CheckoutError::Raised(e)) => Err(e),
    }
}

fn object<const N: usize>(items: [(&str, PyValue); N]) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}
fn optional(value: Option<PyStr>) -> PyValue {
    value.map_or(PyValue::None, PyValue::Str)
}

/// Signed SQLite counts use i64. A Python-sourced Health can supply its original integer
/// values, whose magnitude is unbounded, in every count field.
pub trait HealthCount {
    fn into_value(self) -> PyValue;
}
impl HealthCount for i64 {
    fn into_value(self) -> PyValue {
        PyValue::int(i128::from(self))
    }
}
impl HealthCount for PyValue {
    fn into_value(self) -> PyValue {
        self
    }
}

/// `health_dict`: read Health once, then project its ordered groups and resolve paths in encounter order.
pub fn health_dict<N: HealthCount>(
    read: impl FnOnce() -> Result<Health<N>, PyException>,
    mut lookup: impl FnMut(&[u32]) -> Result<Option<PyStr>, CheckoutError>,
) -> Result<PyValue, PyException> {
    let h = read()?;
    let mut proposals = Vec::with_capacity(h.open_proposals.len());
    for p in h.open_proposals {
        let path = where_path(p.checkout.as_deref(), &mut lookup)?;
        proposals.push(object([
            ("id", PyValue::Str(p.id)),
            ("checkout", optional(p.checkout)),
            ("path", optional(path)),
            ("kind", PyValue::Str(p.kind)),
            ("reason", PyValue::Str(p.reason)),
            ("line", PyValue::Str(p.line)),
            ("opened", PyValue::Str(p.opened)),
        ]));
    }
    let projects = h
        .provisional
        .into_iter()
        .map(|p| {
            object([
                ("id", PyValue::Str(p.id)),
                ("name", PyValue::Str(p.name)),
                ("reason", optional(p.provisional)),
            ])
        })
        .collect();
    let unresolved = h
        .unresolved
        .into_iter()
        .map(|u| {
            object([
                ("id", PyValue::Str(u.project)),
                ("name", PyValue::Str(u.name)),
                ("state", PyValue::Str(u.state)),
                ("checkout", optional(u.checkout)),
                ("path", optional(u.path)),
                ("memories", u.memories.into_value()),
                ("episodes", u.episodes.into_value()),
            ])
        })
        .collect();
    let mut turns = h.suppressed_turns;
    turns.sort_by(|a, b| a.0.cmp(&b.0));
    let turns = PyValue::Dict(
        turns
            .into_iter()
            .map(|(k, n)| (PyValue::Str(k), n.into_value()))
            .collect(),
    );
    let mut without = Vec::with_capacity(h.without_live_view.len());
    for id in h.without_live_view {
        let path = where_path(Some(&id), &mut lookup)?;
        without.push(object([
            ("checkout", PyValue::Str(id)),
            ("last_path", optional(path)),
        ]));
    }
    Ok(object([
        ("open_proposals", PyValue::List(proposals)),
        ("provisional_projects", PyValue::List(projects)),
        ("unresolved_projects", PyValue::List(unresolved)),
        ("suppressed_recall_turns", turns),
        ("indeterminate_captures", h.indeterminate.into_value()),
        ("deferred_captures", h.deferred.into_value()),
        ("checkouts_without_live_view", PyValue::List(without)),
    ]))
}

fn exception(class: &str, message: &str) -> PyException {
    PyException {
        class: class.to_owned(),
        message: cps(message),
    }
}
fn field<'a>(d: &'a PyValue, k: &str) -> Result<&'a PyValue, PyException> {
    d.get(k)
        .ok_or_else(|| exception("KeyError", &format!("'{k}'")))
}

fn check_integer_display(digits: &str) -> Result<(), PyException> {
    if digits.trim_start_matches('-').len() > INT_MAX_STR_DIGITS {
        return Err(exception("ValueError", INT_STR_LIMIT_MESSAGE));
    }
    Ok(())
}

struct Line(PyStr);
impl Line {
    fn new(prefix: &str) -> Self {
        Self(cps(prefix))
    }
    fn text(&mut self, s: &str) {
        self.0.extend(s.chars().map(u32::from));
    }
    fn value(&mut self, v: &PyValue) -> Result<(), PyException> {
        if let PyValue::Int(digits) = v {
            check_integer_display(digits)?;
        }
        if let PyValue::Str(s) = v {
            self.0.extend_from_slice(s);
        } else {
            self.0.extend(py_str(v).map_err(|e| {
                exception(
                    e.name(),
                    "maximum recursion depth exceeded while getting the repr of an object",
                )
            })?);
        }
        Ok(())
    }
    fn quoted(&mut self, v: &PyValue) -> Result<(), PyException> {
        let PyValue::Str(s) = v else {
            return Err(exception(
                "TypeError",
                &format!("'{}' object is not iterable", type_name(v)),
            ));
        };
        quoted_into(s, &mut self.0);
        Ok(())
    }
}

fn section(
    lines: &mut Vec<PyStr>,
    d: &PyValue,
    key: &str,
    title: &str,
    render: impl Fn(&PyValue, &mut Line) -> Result<(), PyException>,
) -> Result<(), PyException> {
    let values = field(d, key)?;
    let (PyValue::List(rows) | PyValue::Tuple(rows)) = values else {
        return Err(exception(
            "TypeError",
            &format!("object of type '{}' has no len()", type_name(values)),
        ));
    };
    lines.push(cps(&format!("Membership: {title} ({}):", rows.len())));
    if rows.is_empty() {
        lines.push(cps("  (none)"));
    }
    for row in rows {
        let mut s = Line::new("  ");
        render(row, &mut s)?;
        lines.push(s.0);
    }
    Ok(())
}

// Each total is little-endian base 10^9. Separating the two signs lets the
// renderer subtract once after adding all the values in the supplied mapping.
const BASE: u32 = 1_000_000_000;

fn add_magnitude(sum: &mut Vec<u32>, digits: &str) {
    let mut carry = 0;
    let mut pos = 0;
    for chunk in digits.as_bytes().rchunks(9) {
        let block = chunk
            .iter()
            .fold(0u32, |n, &b| n * 10 + u32::from(b - b'0'));
        if pos == sum.len() {
            sum.push(0);
        }
        let n = sum[pos] + block + carry;
        sum[pos] = n % BASE;
        carry = n / BASE;
        pos += 1;
    }
    while carry > 0 {
        if pos == sum.len() {
            sum.push(0);
        }
        let n = sum[pos] + carry;
        sum[pos] = n % BASE;
        carry = n / BASE;
        pos += 1;
    }
    while sum.last() == Some(&0) {
        sum.pop();
    }
}

fn count_sum(entries: &[(PyValue, PyValue)]) -> Result<String, PyException> {
    use std::cmp::Ordering;
    use std::fmt::Write;
    let (mut positive, mut negative) = (Vec::new(), Vec::new());
    for (_, count) in entries {
        let digits = match count {
            PyValue::Int(n) => n.as_str(),
            PyValue::Bool(true) => "1",
            PyValue::Bool(false) => "0",
            other => {
                return Err(exception(
                    "TypeError",
                    &format!(
                        "unsupported operand type(s) for +: 'int' and '{}'",
                        type_name(other)
                    ),
                ))
            }
        };
        if let Some(magnitude) = digits.strip_prefix('-') {
            add_magnitude(&mut negative, magnitude);
        } else {
            add_magnitude(&mut positive, digits);
        }
    }
    let order = positive
        .len()
        .cmp(&negative.len())
        .then_with(|| positive.iter().rev().cmp(negative.iter().rev()));
    let (mut larger, smaller, sign) = match order {
        Ordering::Equal => return Ok("0".into()),
        Ordering::Greater => (positive, negative, ""),
        Ordering::Less => (negative, positive, "-"),
    };
    let mut borrow = 0;
    for (i, digit) in larger.iter_mut().enumerate() {
        let sub = smaller.get(i).copied().unwrap_or(0) + borrow;
        borrow = u32::from(*digit < sub);
        *digit = *digit + borrow * BASE - sub;
    }
    while larger.last() == Some(&0) {
        larger.pop();
    }
    let mut out = format!("{sign}{}", larger.pop().expect("unequal magnitudes"));
    for group in larger.iter().rev() {
        write!(&mut out, "{group:09}").expect("writing a string");
    }
    Ok(out)
}

/// Render the ordered dictionary produced by `health_dict` or an equivalent membership section.
/// Counts retain Python integer precision, including sums that exceed the SQLite count range.
pub fn health_lines(d: &PyValue) -> Result<Vec<PyStr>, PyException> {
    let mut lines = Vec::new();
    section(&mut lines, d, "open_proposals", "open questions", |p, s| {
        let path = field(p, "path")?;
        let mut place = Line::new("");
        if truthy(path) {
            place.text(" at ");
            place.quoted(path)?;
        }
        s.value(field(p, "id")?)?;
        s.text(" in checkout ");
        s.value(field(p, "checkout")?)?;
        s.0.extend(place.0);
        s.text(": ");
        s.value(field(p, "line")?)
    })?;
    section(
        &mut lines,
        d,
        "provisional_projects",
        "provisional projects",
        |p, s| {
            s.value(field(p, "id")?)?;
            s.text(" ");
            s.quoted(field(p, "name")?)?;
            s.text(": ");
            s.value(field(p, "reason")?)
        },
    )?;
    section(
        &mut lines,
        d,
        "unresolved_projects",
        "unresolved projects",
        |p, s| {
            s.value(field(p, "id")?)?;
            s.text(" ");
            s.quoted(field(p, "name")?)?;
            s.text(" (");
            s.value(field(p, "state")?)?;
            s.text(") at ");
            s.quoted(field(p, "path")?)?;
            s.text(": ");
            s.value(field(p, "memories")?)?;
            s.text(" memories, ");
            s.value(field(p, "episodes")?)?;
            s.text(" episodes");
            Ok(())
        },
    )?;
    let turns = field(d, "suppressed_recall_turns")?;
    let PyValue::Dict(entries) = turns else {
        return Err(exception(
            "AttributeError",
            &format!("'{}' object has no attribute 'values'", type_name(turns)),
        ));
    };
    let total = count_sum(entries)?;
    check_integer_display(&total)?;
    lines.push(cps(&format!(
        "Membership: suppressed recall turns ({total}):"
    )));
    for (checkout, count) in entries {
        let mut s = Line::new("  checkout ");
        s.value(checkout)?;
        s.text(": ");
        s.value(count)?;
        lines.push(s.0);
    }
    if entries.is_empty() {
        lines.push(cps("  (none)"));
    }
    let mut s = Line::new("Membership: captures ");
    s.value(field(d, "indeterminate_captures")?)?;
    s.text(" indeterminate, ");
    s.value(field(d, "deferred_captures")?)?;
    s.text(" deferred");
    lines.push(s.0);
    section(
        &mut lines,
        d,
        "checkouts_without_live_view",
        "checkouts with no live view",
        |p, s| {
            let path = field(p, "last_path")?;
            let mut place = Line::new("");
            if truthy(path) {
                place.quoted(path)?;
            } else {
                place.text("nowhere");
            }
            s.value(field(p, "checkout")?)?;
            s.text(", last seen at ");
            s.0.extend(place.0);
            Ok(())
        },
    )?;
    Ok(lines)
}
