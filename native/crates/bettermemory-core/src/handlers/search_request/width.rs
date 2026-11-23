//! Request width and search mode as resolved by the search handler.
use std::borrow::Cow;
use std::marker::PhantomData;
use std::sync::LazyLock;

use crate::machine;
use crate::pyvalue::{self, PyStr, PyValue};
use crate::store::{caller::bytes_repr, fold::int_worded, pool};
use crate::unicode::cps;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestError {
    pub class: &'static str,
    pub message: PyStr,
}

#[derive(Clone, Copy, Debug)]
pub enum Width<'a> {
    Value(&'a PyValue),
    Bytes(&'a [u8]),
    ByteArray(&'a [u8]),
}

/// Operations a direct Python caller can customize. The integer conversion
/// implements Python's int(value) and supplies canonical signed decimal digits.
/// It may clip that integer to the search range before exporting it.
/// A mode handle is returned unchanged when an allowed search mode compares equal.
pub trait RequestProtocol {
    type Width;
    type Integer: AsRef<str>;
    type Mode;
    type Error: From<RequestError>;

    fn default_width(&mut self) -> Result<Self::Width, Self::Error>;
    fn integer(&mut self, value: Self::Width) -> Result<Self::Integer, Self::Error>;
    fn default_mode(&mut self) -> Result<Self::Mode, Self::Error>;
    fn hybrid(&self) -> Self::Mode;
    fn truth(&mut self, mode: &Self::Mode) -> Result<bool, Self::Error>;
    fn equals(&mut self, mode: &Self::Mode, allowed: &'static str) -> Result<bool, Self::Error>;
    fn repr(&mut self, mode: &Self::Mode) -> Result<PyStr, Self::Error>;
}

pub fn resolve_with<P: RequestProtocol>(
    host: &mut P,
    width: Option<P::Width>,
    mode: P::Mode,
) -> Result<(i64, P::Mode), P::Error> {
    let width = match width {
        Some(width) => width,
        None => host.default_width()?,
    };
    let integer = host.integer(width)?;
    let digits = integer.as_ref();
    let upper = pool::MAX_SEARCH_RESULTS;
    let width = pool::clamp_search_width(if digits.starts_with('-') {
        0
    } else if pyvalue::cmp_int_text(digits, &upper.to_string()).is_gt() {
        upper
    } else {
        digits.parse().expect("bounded canonical decimal integer")
    });

    let mut selected = mode;
    if !host.truth(&selected)? {
        selected = host.default_mode()?;
        if !host.truth(&selected)? {
            selected = host.hybrid();
        }
    }
    for name in ["keyword", "bm25", "hybrid"] {
        if host.equals(&selected, name)? {
            return Ok((width, selected));
        }
    }
    let mut message = cps("unknown search mode ");
    message.extend(host.repr(&selected)?);
    message.extend(cps("; must be one of: keyword, bm25, hybrid"));
    Err(RequestError {
        class: "ValueError",
        message,
    }
    .into())
}

/// Fully resolved built-in values from a normal tool invocation. The setting
/// callbacks run only where the Python handler reads the respective setting.
pub fn request_width_and_mode<'a>(
    width: Width<'a>,
    mode: &'a PyValue,
    default_width: impl FnOnce() -> Result<Width<'a>, RequestError>,
    default_mode: impl FnOnce() -> Result<&'a PyValue, RequestError>,
) -> Result<(i64, &'a PyValue), RequestError> {
    let mut host = Builtins {
        width: Some(default_width),
        mode: Some(default_mode),
        values: PhantomData,
    };
    let supplied = (!matches!(width, Width::Value(PyValue::None))).then_some(width);
    resolve_with(&mut host, supplied, mode)
}

struct Builtins<'a, W, M> {
    width: Option<W>,
    mode: Option<M>,
    values: PhantomData<&'a PyValue>,
}

impl<'a, W, M> RequestProtocol for Builtins<'a, W, M>
where
    W: FnOnce() -> Result<Width<'a>, RequestError>,
    M: FnOnce() -> Result<&'a PyValue, RequestError>,
{
    type Width = Width<'a>;
    type Integer = Cow<'a, str>;
    type Mode = &'a PyValue;
    type Error = RequestError;

    fn default_width(&mut self) -> Result<Self::Width, RequestError> {
        self.width.take().expect("one width setting read")()
    }

    fn default_mode(&mut self) -> Result<Self::Mode, RequestError> {
        self.mode.take().expect("one mode setting read")()
    }

    fn integer(&mut self, width: Width<'a>) -> Result<Self::Integer, RequestError> {
        match width {
            Width::Value(PyValue::Int(digits)) => Ok(Cow::Borrowed(digits)),
            Width::Value(PyValue::Float(f)) if f.is_finite() => {
                // Truncation and clipping commute at these integer limits.
                // A large exponent need not materialize hundreds of decimal digits.
                let clipped = f.clamp(1.0, pool::MAX_SEARCH_RESULTS as f64) as i64;
                Ok(Cow::Owned(clipped.to_string()))
            }
            Width::Value(v) => integer_text(v).map(Cow::Owned),
            Width::Bytes(raw) | Width::ByteArray(raw) => {
                // int(bytes) accepts only ASCII here, and also spells a
                // bytearray as bytes in its invalid-literal diagnostic.
                let text = raw
                    .iter()
                    .map(|&b| u32::from(if b.is_ascii() { b } else { b'?' }))
                    .collect();
                integer_text(&PyValue::Str(text))
                    .map(Cow::Owned)
                    .map_err(|mut error| {
                        const PREFIX: &str = "invalid literal for int() with base 10: ";
                        if error.message.starts_with(&cps(PREFIX)) {
                            // CPython clips the raw input before choosing repr's quotes.
                            let shown = bytes_repr(&raw[..raw.len().min(200)]);
                            error.message =
                                cps(&format!("{PREFIX}{}", &shown[..shown.len().min(200)]));
                        }
                        error
                    })
            }
        }
    }

    fn hybrid(&self) -> Self::Mode {
        static HYBRID: LazyLock<PyValue> = LazyLock::new(|| PyValue::str("hybrid"));
        &HYBRID
    }

    fn truth(&mut self, mode: &Self::Mode) -> Result<bool, RequestError> {
        Ok(machine::truthy(mode))
    }

    fn equals(&mut self, mode: &Self::Mode, allowed: &'static str) -> Result<bool, RequestError> {
        Ok(matches!(mode, PyValue::Str(s) if s.iter().copied().eq(allowed.bytes().map(u32::from))))
    }

    fn repr(&mut self, mode: &Self::Mode) -> Result<PyStr, RequestError> {
        // For containers and integers the bounded str() helper gives repr(),
        // including failures on oversized integers nested inside a container.
        use PyValue::{Dict, Int, List, Tuple};
        let result = match mode {
            Int(_) | Dict(_) | List(_) | Tuple(_) => machine::py_text(mode),
            other => pyvalue::repr(other),
        };
        result.map_err(|e| RequestError {
            class: e.name(),
            message: cps(if e == pyvalue::PyError::ValueError {
                "Exceeds the limit (4300 digits) for integer string conversion; use sys.set_int_max_str_digits() to increase the limit"
            } else {
                "maximum recursion depth exceeded while getting the repr of an object"
            }),
        })
    }
}

fn integer_text(value: &PyValue) -> Result<String, RequestError> {
    if let PyValue::Str(s) = value {
        // CPython first converts the C string up to NUL. Its digit-limit
        // exception wins before the later check for unconsumed input.
        if let Some(end) = s
            .iter()
            .position(|&cp| cp == 0)
            .filter(|&n| n > machine::INT_MAX_STR_DIGITS)
        {
            if let Err((class, Some(msg))) = int_worded(&PyValue::Str(s[..end].to_vec())) {
                if msg.starts_with("Exceeds the limit") {
                    return Err(RequestError {
                        class,
                        message: cps(&msg),
                    });
                }
            }
        }
    }
    let mut normalized = int_worded(value).map_err(|(class, text)| {
        let message = match class {
            "TypeError" => format!(
                "int() argument must be a string, a bytes-like object or a real number, not '{}'",
                pyvalue::type_name(value)
            ),
            "OverflowError" => "cannot convert float infinity to integer".to_owned(),
            _ => text.expect("worded integer conversion error"),
        };
        RequestError {
            class,
            message: cps(&message),
        }
    })?;
    let PyValue::Int(ref mut digits) = normalized else {
        unreachable!("int(value)")
    };
    Ok(std::mem::take(digits))
}
