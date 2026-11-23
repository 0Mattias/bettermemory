//! The ordered report, text and JSON returned by `bettermemory log verify`.

use crate::json::{self, DumpOptions};
use crate::log::{ReportDict, ReportValue};
use crate::printable::printable;
use crate::pyvalue::{self, PyError, PyValue};
use crate::store::fold::{Cell, Label, LogVerify, Problem, TableFold};

/// A SQLite bytes key can be printed as a row label, but Python's JSON
/// encoder rejects it. Keep this distinction until the output format is known.
pub struct Report {
    pub document: PyValue,
    has_bytes_label: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub enum ReportError {
    BytesLabel,
    IntegerDisplayLimit,
    MissingKey(&'static str),
    Value(PyError),
}

impl std::fmt::Display for ReportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::BytesLabel => f.write_str("Object of type bytes is not JSON serializable"),
            Self::IntegerDisplayLimit => f.write_str(
                "Exceeds the limit (4300 digits) for integer string conversion; \
                 use sys.set_int_max_str_digits() to increase the limit",
            ),
            Self::MissingKey(key) => write!(f, "'{key}'"),
            Self::Value(error) => f.write_str(error.name()),
        }
    }
}

impl std::error::Error for ReportError {}

impl From<PyError> for ReportError {
    fn from(error: PyError) -> Self {
        Self::Value(error)
    }
}

fn dict<'a>(items: impl IntoIterator<Item = (&'a str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

fn chain_dict(items: &ReportDict) -> PyValue {
    dict(items.iter().map(|(k, v)| {
        (
            *k,
            match v {
                ReportValue::Null => PyValue::None,
                ReportValue::Int(n) => PyValue::int((*n).into()),
                ReportValue::BigInt(n) => PyValue::Int(n.clone()),
                ReportValue::Str(s) => PyValue::Str(s.clone()),
            },
        )
    }))
}

fn cell_value(cell: &Cell) -> Option<PyValue> {
    Some(match cell {
        Cell::Null => PyValue::None,
        Cell::Int(n) => PyValue::int((*n).into()),
        Cell::Real(f) => PyValue::Float(*f),
        Cell::Text(s) => PyValue::str(s),
        Cell::Blob(_) => return None,
    })
}

fn labels(items: &[Label], has_bytes: &mut bool) -> PyValue {
    PyValue::List(
        items
            .iter()
            .map(|label| {
                let value = match label {
                    Label::One(cell) => cell_value(cell),
                    Label::Many(cells) => cells
                        .iter()
                        .map(cell_value)
                        .collect::<Option<Vec<_>>>()
                        .map(PyValue::List),
                };
                value.unwrap_or_else(|| {
                    *has_bytes = true;
                    PyValue::str(&label.py_str())
                })
            })
            .collect(),
    )
}

fn tables(items: &[TableFold], has_bytes: &mut bool) -> PyValue {
    dict(items.iter().map(|t| {
        (
            t.table,
            dict([
                ("unaccounted", labels(&t.unaccounted, has_bytes)),
                ("missing", labels(&t.missing, has_bytes)),
                ("rows", PyValue::int(t.rows as i128)),
            ]),
        )
    }))
}

impl Report {
    /// Materialize the fields in the order of `store_events.log_verify`.
    /// The caller supplies the actual display spelling of the store path.
    pub fn from_verify(verified: &LogVerify, path: &[u32]) -> Self {
        let chain = &verified.chain;
        let mut has_bytes_label = false;
        let problems = verified
            .problems
            .iter()
            .map(|problem| match problem {
                Problem::Chain(fields) => chain_dict(fields),
                Problem::Payload(p) => dict([
                    ("seq", PyValue::int(p.seq.into())),
                    ("kind", PyValue::str(&p.kind)),
                    ("problem", PyValue::str("payload_invalid")),
                    ("detail", PyValue::str(&p.detail)),
                ]),
            })
            .collect();
        let fold = dict([
            ("status", PyValue::str(verified.fold.status)),
            (
                "tables",
                tables(&verified.fold.tables, &mut has_bytes_label),
            ),
            (
                "membership",
                tables(&verified.fold.membership, &mut has_bytes_label),
            ),
        ]);
        let document = dict([
            ("status", PyValue::str(verified.status)),
            ("path", PyValue::Str(path.to_vec())),
            ("store_id", PyValue::str(&verified.store_id)),
            ("key_fingerprint", PyValue::str(&verified.key_fingerprint)),
            ("rows", PyValue::int(chain.rows as i128)),
            (
                "segments",
                PyValue::List(chain.segments.iter().map(chain_dict).collect()),
            ),
            ("head", chain_dict(&chain.head)),
            ("origin", chain_dict(&chain.origin)),
            ("problems", PyValue::List(problems)),
            ("fold", fold),
            (
                "unaccounted_ids",
                labels(verified.unaccounted_ids(), &mut has_bytes_label),
            ),
        ]);
        Self {
            document,
            has_bytes_label,
        }
    }

    /// Add the store's replacement history before merging external anchors.
    pub fn add_history(&mut self, history: Vec<Vec<(&'static str, PyValue)>>) {
        if !history.is_empty() {
            let PyValue::Dict(fields) = &mut self.document else {
                return;
            };
            fields.push((
                PyValue::str("history_replaced"),
                PyValue::List(history.into_iter().map(dict).collect()),
            ));
        }
    }

    /// Render and select the exit status after the store has been closed.
    pub fn output(&self, json_out: bool) -> Result<(i32, String), ReportError> {
        format_output(&self.document, self.has_bytes_label, json_out)
    }
}

/// The post-close output step. The byte-label flag carries SQL types that
/// have no JSON representation, even when their text happens to look like a string.
pub fn format_output(
    report: &PyValue,
    has_bytes_label: bool,
    json_out: bool,
) -> Result<(i32, String), ReportError> {
    let text = if json_out {
        if has_bytes_label {
            return Err(ReportError::BytesLabel);
        }
        let options = DumpOptions {
            sort_keys: false,
            ..DumpOptions::DOCUMENT_INDENT2
        };
        let mut text = json::utf8_text(&json::dumps(report, options)?);
        text.push('\n');
        text
    } else {
        render_report(report)?
    };
    let status = field(report, "status")?;
    Ok((i32::from(status != &PyValue::str("ok")), text))
}

fn field<'a>(v: &'a PyValue, key: &'static str) -> Result<&'a PyValue, ReportError> {
    v.get(key).ok_or(ReportError::MissingKey(key))
}

fn list(v: &PyValue) -> Result<&[PyValue], ReportError> {
    match v {
        PyValue::List(items) => Ok(items),
        _ => Err(PyError::TypeError.into()),
    }
}

fn entries(v: &PyValue) -> Result<&[(PyValue, PyValue)], ReportError> {
    match v {
        PyValue::Dict(items) => Ok(items),
        _ => Err(PyError::TypeError.into()),
    }
}

// printable quotes one code point at a time. Quoting each interpolated
// field gives the same completed line and keeps raw filename surrogates
// intact until they are escaped. The fixed portions are printable ASCII.
fn show(v: &PyValue) -> Result<String, ReportError> {
    Ok(json::utf8_text(&printable(&pyvalue::py_str(v)?)))
}

fn show_field(v: &PyValue, key: &'static str) -> Result<String, ReportError> {
    show(field(v, key)?)
}

fn status_at(v: &PyValue) -> Result<String, ReportError> {
    let mut line = show_field(v, "status")?;
    if let Some(seq) = v.get("seq") {
        line.push_str(&format!(" at seq {}", show(seq)?));
    }
    Ok(line)
}

fn table_lines(out: &mut Vec<String>, items: &[(PyValue, PyValue)]) -> Result<(), ReportError> {
    for (table, counts) in items {
        let unaccounted = list(field(counts, "unaccounted")?)?;
        let missing = list(field(counts, "missing")?)?;
        if unaccounted.is_empty() && missing.is_empty() {
            continue;
        }
        out.push(format!(
            "    {}: {} unaccounted, {} missing",
            show(table)?,
            unaccounted.len(),
            missing.len()
        ));
        for (name, keys) in [("unaccounted", unaccounted), ("missing", missing)] {
            for key in keys {
                out.push(format!("      {name} {}", show(key)?));
            }
        }
    }
    Ok(())
}

fn replaced_text(entry: &PyValue) -> Result<String, ReportError> {
    let kind = show_field(entry, "kind")?;
    let name = match kind.as_str() {
        "store_restored" => "a restore",
        "history_accepted" => "an accepted history",
        _ => return Err(ReportError::MissingKey("history kind")),
    };
    let none = PyValue::None;
    let to = entry.get("to_seq").unwrap_or(&none);
    let start = entry
        .get("from_seq")
        .filter(|v| !matches!(v, PyValue::None));
    let mut line = format!(
        "seq {} {kind}: {name} at {}",
        show_field(entry, "seq")?,
        show_field(entry, "ts")?
    );
    if kind == "store_restored" {
        line.push_str(&format!(" put the log back to seq {}", show(to)?));
        if let Some(v) = start {
            line.push_str(&format!(" from seq {}", show(v)?));
        }
    } else {
        line.push_str(&format!(
            " accepted the log as it stood at seq {}",
            show(to)?
        ));
        if let Some(v) = start.filter(|v| !crate::location_witness::py_eq(v, to)) {
            line.push_str(&format!(
                ", where the head checkpoint had said seq {}",
                show(v)?
            ));
        }
    }
    Ok(line)
}

/// `cli.log_cmd.render_report`, including exactly one final newline.
pub fn render_report(report: &PyValue) -> Result<String, ReportError> {
    let segments = list(field(report, "segments")?)?;
    let mut lines = vec![
        format!("bettermemory log verify: {}", show_field(report, "status")?),
        format!("  store {}", show_field(report, "path")?),
        format!("  id {}", show_field(report, "store_id")?),
        format!(
            "  rows {} in {} segment(s), head {}",
            show_field(report, "rows")?,
            segments.len(),
            status_at(field(report, "head")?)?
        ),
    ];
    if let Some(origin) = report.get("origin").filter(|v| !matches!(v, PyValue::None)) {
        let mut line = format!("  origin {}", status_at(origin)?);
        if let Some(reason) = origin.get("reason") {
            line.push_str(&format!(" ({})", show(reason)?));
        }
        lines.push(line);
    }
    for segment in segments {
        let PyValue::Str(fp) = field(segment, "key_fingerprint")? else {
            return Err(PyError::TypeError.into());
        };
        let prefix = json::utf8_text(&printable(&fp[..fp.len().min(16)]));
        lines.push(format!(
            "  segment seq {} to {}: {} (key {prefix})",
            show_field(segment, "first_seq")?,
            show_field(segment, "last_seq")?,
            show_field(segment, "status")?
        ));
    }
    let fold = field(report, "fold")?;
    lines.push(format!("  fold {}", show_field(fold, "status")?));
    table_lines(&mut lines, entries(field(fold, "tables")?)?)?;
    let membership = match fold.get("membership") {
        Some(v) if crate::events::truthy(v) => entries(v)?,
        _ => &[],
    };
    let mut rows = String::from("0");
    for (_, counts) in membership {
        let PyValue::Int(ref n) = pyvalue::py_int(field(counts, "rows")?)? else {
            return Err(PyError::TypeError.into());
        };
        rows = pyvalue::integer::sum(&rows, n);
    }
    // Python limits decimal conversion when the final total is printed.
    if rows.trim_start_matches('-').len() > 4300 {
        return Err(ReportError::IntegerDisplayLimit);
    }
    lines.push(format!(
        "  membership {rows} rows in {} tables",
        membership.len()
    ));
    table_lines(&mut lines, membership)?;
    if let Some(anchors) = report.get("anchors") {
        let anchors = list(anchors)?;
        lines.push(format!("  anchors {}", anchors.len()));
        for anchor in anchors {
            let mut status = show_field(anchor, "status")?;
            if let Some(reason) = anchor.get("reason") {
                status.push_str(&format!(" ({})", show(reason)?));
            }
            lines.push(format!(
                "    seq {}: {status} ({})",
                show_field(anchor, "seq")?,
                show_field(anchor, "file")?
            ));
        }
    }
    if let Some(history) = report
        .get("history_replaced")
        .filter(|h| crate::events::truthy(h))
    {
        let history = list(history)?;
        lines.push(format!("  history replaced {} time(s)", history.len()));
        for entry in history {
            lines.push(format!("    {}", replaced_text(entry)?));
        }
    }
    let problems = list(field(report, "problems")?)?;
    if !problems.is_empty() {
        lines.push("  problems".into());
        for problem in problems {
            lines.push(format!(
                "    seq {} ({}): {}",
                show_field(problem, "seq")?,
                show_field(problem, "kind")?,
                show_field(problem, "problem")?
            ));
        }
    }
    lines.push(String::new());
    Ok(lines.join("\n"))
}
